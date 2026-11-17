"""
Listener Module.

Hears the words in a whole song, to judge it clean or explicit (decision 0019, section 2).
The listening is done by a worker process of its own (listen_worker), at the lowest
priority, with its own copy of Nemotron and two threads: the brain's speech to text keeps
its model, and what the user says to Nova is never slowed by a song being heard.

The worker is started when there's a song to hear, takes one at a time, and is closed
once it's had nothing to do for a while - its model holds about 1 GB while it runs.

A listen that fails, or stops part way, raises ListenError rather than giving back what
was heard so far: a short transcript would sound like a song with its swears taken out,
and a song judged clean is moved out of the library.
"""

import json
import logging
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
from typing import List, Optional

import numpy as np

from voice_assistant.nemotron import RATE
from voice_assistant.paths import PROJECT_DIRECTORY

# Logger setup
logger = logging.getLogger(__name__)

# The song, whatever it's in, down-mixed to the mono 16kHz float32 Nemotron hears.
DECODE = ["ffmpeg", "-v", "error", "-i", "pipe:0", "-ac", "1", "-ar", str(RATE),
          "-f", "f32le", "pipe:1"]

# What of Nova's environment the worker is given: enough to run Python, and none of the
# keys and passwords Nova reads into its own (NAVIDROME_PASSWORD, say). MALLOC_ARENA_MAX
# too, the brain's unit's cap on glibc's arenas, so the worker's model is held to it as well.
WORKER_ENVIRONMENT = ("PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "MALLOC_ARENA_MAX")

# onnxruntime looking for a GPU as the worker's model loads - pve2 has none, so it's said
# every time: '[W:onnxruntime:Default, device_discovery.cc:285 GetGpuDevices] Failed to
# detect devices under "/sys/class/drm/card1": ...'. Logged as info, not as a warning.
EXPECTED = re.compile(r"GetGpuDevices|Failed to detect devices under")

# Seconds ffmpeg has to decode a song. It takes a second or two for a long one; this is
# only so a stuck ffmpeg can't hold up every song after it.
DECODE_TIMEOUT = 300


class ListenError(Exception):
    """A song couldn't be heard. Its message is the reason, plain enough to be said."""


def decode(audio: bytes) -> np.ndarray:
    """
    A song as samples to listen to, decoded in memory.

    Args:
        audio: The song's file, as Navidrome streams it.

    Returns:
        Its float32 samples, mono at 16kHz.

    Raises:
        ListenError: ffmpeg couldn't decode it.
    """
    try:
        done = subprocess.run(_niced(DECODE), input=audio, capture_output=True,
                              timeout=DECODE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning(f"Couldn't decode a song to listen to: {type(e).__name__}")
        raise ListenError("couldn't decode the song") from e
    if done.returncode:
        logger.warning(f"Couldn't decode a song to listen to: "
                       f"{done.stderr[:2000].decode(errors='replace').strip()}")
        raise ListenError("couldn't decode the song")
    data = done.stdout
    return np.frombuffer(data[:len(data) - len(data) % 4], dtype="<f4")


def _niced(command: List[str], worker: bool = False) -> List[str]:
    """
    A command run under "nice -n 19", the lowest priority, where there's a nice to run it.

    Args:
        command: What to run.
        worker: Whether it's the listening worker, which lowers its own priority without nice.
    """
    nice = shutil.which("nice")
    if nice is None:
        if worker:
            logger.warning("There's no nice to start the listening worker with, so it lowers its own priority")
        else:
            logger.warning(f"There's no nice to run {command[0]} with, so it runs at the usual priority")
        return command
    return [nice, "-n", "19"] + command


def worker_command(folder: str, threads: int) -> List[str]:
    """
    How the worker is run: by the brain's own Python, at the lowest priority.

    Args:
        folder: Where the model is.
        threads: How many processor threads it works with.
    """
    return _niced([sys.executable, "-m", "voice_assistant.listen_worker", folder, str(threads)], worker=True)


def _log_complaints(stderr):
    """Logs everything the worker writes to stderr, a line at a time, until it ends."""
    with stderr:
        for line in iter(stderr.readline, b""):
            line = line.decode(errors="replace").rstrip()
            if line:
                level = logging.INFO if EXPECTED.search(line) else logging.WARNING
                logger.log(level, f"Listening worker: {line}")


class Listener:
    """The worker songs are heard in, started and closed as needed."""

    def __init__(self, model_folder: str, threads: int = 2, idle: float = 300,
                 command: Optional[List[str]] = None):
        """
        Args:
            model_folder: Where Nemotron was unpacked (stt.model, from the project folder).
            threads: How many processor threads the worker's model works with.
            idle: Seconds without a song before the worker is closed.
            command: What starts the worker, in place of worker_command's - for the tests.
        """
        self.model_folder = model_folder
        self.threads = threads
        self.idle = idle
        self._command = command
        # One song at a time. Reentrant, so whoever's judging a song can hold it from
        # fetching the song to storing its verdict, and still call transcribe.
        self.lock = threading.RLock()
        # Guards the worker and the idle timer, which close() and the timer itself change
        # from other threads, and without waiting for a song to finish.
        self._guard = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._timer: Optional[threading.Timer] = None

    def transcribe(self, samples: np.ndarray, timeout: float) -> str:
        """
        The words heard in a whole song.

        Args:
            samples: The song, from decode.
            timeout: Seconds to give it before the worker is killed - three times the
                song's length and a minute, from whoever knows its length.

        Returns:
            What the worker heard, as it gave it - empty if it heard no words.

        Raises:
            ListenError: It couldn't be heard to the end - the worker failed, stopped or
                took too long, and is started afresh for the next song.
        """
        data = np.asarray(samples, dtype="<f4").tobytes()
        if not data:
            raise ListenError("there was no sound to listen to")
        with self.lock:
            self._stop_timer()
            process = self._running()
            try:
                return self._ask(process, data, timeout)
            except ListenError:
                self._end(process)
                raise
            finally:
                self._start_timer()

    def close(self):
        """Closes the worker, if there is one, straight away - even in the middle of a song."""
        with self._guard:
            process, self._process = self._process, None
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        if process is not None:
            self._end(process)

    def _running(self) -> subprocess.Popen:
        """The worker, started if it isn't running."""
        with self._guard:
            process = self._process
            if process is not None and process.poll() is None:
                return process
        if process is not None:
            self._end(process)
        command = self._command or worker_command(self.model_folder, self.threads)
        with self._guard:
            try:
                # Started from the project folder, for "-m voice_assistant.listen_worker".
                # Its stderr is only ever logged: stdout is for its answers alone.
                self._process = subprocess.Popen(
                    command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, cwd=PROJECT_DIRECTORY,
                    env={name: os.environ[name] for name in WORKER_ENVIRONMENT
                         if name in os.environ})
            except OSError as e:
                logger.warning(f"Couldn't start the listening worker: {type(e).__name__}")
                raise ListenError("couldn't start listening") from e
            process = self._process
        threading.Thread(target=_log_complaints, args=(process.stderr,), daemon=True,
                         name="listener-log").start()
        return process

    def _ask(self, process: subprocess.Popen, data: bytes, timeout: float) -> str:
        """
        Sends the worker a song and waits for its answer.

        The sending is on a thread of its own: a song is megabytes, far more than a pipe
        holds, so a worker that stopped reading would otherwise hold Nova there for good.
        """
        answers: "queue.Queue" = queue.Queue()

        def talk():
            try:
                process.stdin.write(struct.pack("<I", len(data)))
                process.stdin.write(data)
                process.stdin.flush()
                answers.put(process.stdout.readline())
            # A worker that's died, or been killed and its pipes closed - or anything else,
            # so the song fails now rather than once its whole timeout is up.
            except Exception as e:
                answers.put(e)

        threading.Thread(target=talk, daemon=True, name="listener-song").start()
        try:
            line = answers.get(timeout=timeout)
        except queue.Empty:
            logger.warning(f"Listening to a song took over {timeout:.0f}s, "
                           f"so its worker was stopped")
            raise ListenError("listening took too long")
        if isinstance(line, Exception) and not isinstance(line, (OSError, ValueError)):
            logger.warning(f"Couldn't send a song to the listening worker: {type(line).__name__}")
        if isinstance(line, Exception) or not line:
            raise ListenError("the listening stopped part way through")
        try:
            answer = json.loads(line)
        except ValueError:
            answer = None
        if isinstance(answer, dict) and isinstance(answer.get("error"), str):
            raise ListenError(answer["error"])
        if not (isinstance(answer, dict) and isinstance(answer.get("text"), str)):
            raise ListenError("the listening gave an answer that couldn't be read")
        return answer["text"]

    def _end(self, process: subprocess.Popen):
        """Kills a worker, if it's still going, and lets go of it."""
        with self._guard:
            if self._process is process:
                self._process = None
        if process.poll() is None:
            process.kill()
        process.wait()
        for pipe in (process.stdin, process.stdout):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass

    def _start_timer(self):
        """Closes the worker once it's been idle for long enough, unless a song comes first."""
        with self._guard:
            if self._process is None:
                return
            timer = threading.Timer(self.idle, lambda: self._idle_close(timer))
            timer.daemon = True
            earlier, self._timer = self._timer, timer
        if earlier is not None:
            earlier.cancel()
        timer.start()

    def _stop_timer(self):
        with self._guard:
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _idle_close(self, timer: threading.Timer):
        with self._guard:
            if self._timer is not timer:
                return
        # A song is being judged - perhaps one that never reaches transcribe, and so
        # never sets the timer again itself - so the worker is looked at again later.
        if not self.lock.acquire(blocking=False):
            self._start_timer()
            return
        try:
            with self._guard:
                if self._timer is not timer:
                    return
                self._timer = None
            self.close()
        finally:
            self.lock.release()
