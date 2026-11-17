"""
Listener Test
Checks the worker a song is listened to in (decision 0019, section 2): how it's started,
spoken to, restarted after a failure and closed. Small fake workers written into tmp_path
stand in for it, so the real model is never loaded.
"""

import io
import json
import logging
import os
import struct
import subprocess
import sys
import textwrap
import threading
import time

import numpy as np
import pytest

from voice_assistant import listen_worker, listener
from voice_assistant.listener import ListenError, Listener, decode

# Reads each request as the real worker does, and runs the test's own lines on it with
# 'count' (how many samples came) and 'answer' (to reply with) in hand. It writes its
# process id into the file named by its first argument as it starts.
FAKE = """\
import json, os, struct, sys, time
here = sys.argv[1]
with open(os.path.join(here, "pids"), "a") as pids:
    pids.write(f"{{os.getpid()}}\\n")

def answer(reply):
    sys.stdout.write(json.dumps(reply) + "\\n")
    sys.stdout.flush()

while True:
    header = sys.stdin.buffer.read(4)
    if len(header) < 4:
        break
    size = struct.unpack("<I", header)[0]
    data = sys.stdin.buffer.read(size)
    count = len(data) // 4
{body}
"""

SECOND = np.zeros(16000, dtype=np.float32)


def fake(tmp_path, body, source=None, **kwargs) -> Listener:
    """
    A Listener whose worker is a fake.

    Args:
        body: Lines the fake above runs on each request.
        source: A whole worker of the test's own instead.
    """
    script = tmp_path / "worker.py"
    script.write_text(source or FAKE.format(body=textwrap.indent(textwrap.dedent(body), "    ")))
    command = [sys.executable, str(script), str(tmp_path)]
    return Listener("models/nemotron", command=command, **kwargs)


def pids(tmp_path):
    """The process id of every worker started so far, oldest first."""
    try:
        return [int(line) for line in (tmp_path / "pids").read_text().split()]
    except FileNotFoundError:
        return []


def gone(pid) -> bool:
    """Whether a worker has ended and been reaped."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def soon(check, within=5.0) -> bool:
    """Whether check() comes true within a few seconds, for what happens on another thread."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return check()


@pytest.fixture
def listeners():
    """Closes every Listener a test makes, so no fake worker outlives it."""
    made = []
    yield made
    for each in made:
        each.close()


def test_transcribe_returns_the_workers_text(tmp_path, listeners):
    heard = fake(tmp_path, 'answer({"text": f"{count} samples"})')
    listeners.append(heard)

    assert heard.transcribe(np.zeros(48000, dtype=np.float32), timeout=10) == "48000 samples"
    # The worker is kept for the next song, not started again.
    assert heard.transcribe(SECOND, timeout=10) == "16000 samples"
    assert len(pids(tmp_path)) == 1


def test_samples_are_sent_as_float32_whatever_they_were(tmp_path, listeners):
    heard = fake(tmp_path, 'answer({"text": str(struct.unpack("<2f", data[:8]))})')
    listeners.append(heard)

    assert heard.transcribe(np.array([0.5, -0.25], dtype=np.float64), timeout=10) == "(0.5, -0.25)"


def test_a_worker_that_dies_mid_song_raises_and_the_next_song_gets_a_fresh_one(tmp_path, listeners):
    # The first worker dies with the song half read; the one after it answers.
    heard = fake(tmp_path, """\
        if not os.path.exists(os.path.join(here, "died")):
            open(os.path.join(here, "died"), "w").close()
            os._exit(1)
        answer({"text": "made it"})
    """)
    listeners.append(heard)

    with pytest.raises(ListenError):
        heard.transcribe(SECOND, timeout=10)
    assert heard.transcribe(SECOND, timeout=10) == "made it"
    first, second = pids(tmp_path)
    assert first != second and gone(first)


def test_a_worker_that_dies_before_reading_the_song_raises(tmp_path, listeners):
    # Its end of the pipe closes with most of a minute's song still to send.
    heard = fake(tmp_path, None, source=FAKE.format(body="").split("while True")[0]
                 + "sys.stdin.buffer.read(4)\nos._exit(1)\n")
    listeners.append(heard)

    started = time.monotonic()
    with pytest.raises(ListenError):
        heard.transcribe(np.zeros(16000 * 60, dtype=np.float32), timeout=30)
    assert time.monotonic() - started < 10
    assert gone(pids(tmp_path)[0])


def test_a_failure_sending_the_song_fails_at_once(tmp_path, listeners, monkeypatch):
    class Broken:
        @staticmethod
        def pack(*args):
            raise RuntimeError("something unforeseen")

    monkeypatch.setattr(listener, "struct", Broken)
    heard = fake(tmp_path, 'answer({"text": "hi"})')
    listeners.append(heard)

    started = time.monotonic()
    with pytest.raises(ListenError):
        heard.transcribe(SECOND, timeout=30)
    assert time.monotonic() - started < 10
    # Killed so fast it may not have got as far as noting its process id.
    assert heard._process is None


def test_the_worker_gets_none_of_novas_keys(tmp_path, listeners, monkeypatch):
    monkeypatch.setenv("NAVIDROME_PASSWORD", "not for the worker")
    monkeypatch.setenv("LANG", "en_GB.UTF-8")
    # The brain's unit caps glibc's arenas, and the worker's model wants the same cap.
    monkeypatch.setenv("MALLOC_ARENA_MAX", "2")
    heard = fake(tmp_path, 'answer({"text": json.dumps(dict(os.environ))})')
    listeners.append(heard)

    given = json.loads(heard.transcribe(SECOND, timeout=10))
    assert "NAVIDROME_PASSWORD" not in given
    assert given["PATH"] == os.environ["PATH"] and given["LANG"] == "en_GB.UTF-8"
    assert given["MALLOC_ARENA_MAX"] == "2"
    # Python may add LC_CTYPE itself, in a C locale.
    assert set(given) <= {"PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "LC_CTYPE", "MALLOC_ARENA_MAX"}


def test_a_worker_that_hangs_raises_after_the_timeout_and_is_killed(tmp_path, listeners):
    heard = fake(tmp_path, "time.sleep(3600)")
    listeners.append(heard)

    started = time.monotonic()
    with pytest.raises(ListenError):
        heard.transcribe(SECOND, timeout=2)
    assert time.monotonic() - started < 10
    assert gone(pids(tmp_path)[0])


def test_a_hung_worker_that_never_reads_the_song_still_times_out(tmp_path, listeners):
    # A whole song is megabytes, far more than a pipe holds, so a worker that never reads
    # must not leave Nova stuck writing it.
    asleep = FAKE.format(body="").split("while True")[0] + "time.sleep(3600)\n"
    heard = fake(tmp_path, None, source=asleep)
    listeners.append(heard)

    with pytest.raises(ListenError):
        heard.transcribe(np.zeros(16000 * 60, dtype=np.float32), timeout=2)
    assert gone(pids(tmp_path)[0])


def test_a_workers_error_is_raised_with_its_reason_and_it_is_restarted(tmp_path, listeners):
    heard = fake(tmp_path, """\
        if not os.path.exists(os.path.join(here, "erred")):
            open(os.path.join(here, "erred"), "w").close()
            answer({"error": "couldn't hear the song"})
        else:
            answer({"text": "fine"})
    """)
    listeners.append(heard)

    with pytest.raises(ListenError, match="^couldn't hear the song$"):
        heard.transcribe(SECOND, timeout=10)
    assert heard.transcribe(SECOND, timeout=10) == "fine"
    first, second = pids(tmp_path)
    assert first != second and gone(first)


@pytest.mark.parametrize("reply", ['print("not json", flush=True)', 'answer({"text": 5})',
                                   'answer(["text"])', 'answer({})'])
def test_an_answer_that_cant_be_read_raises(tmp_path, listeners, reply):
    heard = fake(tmp_path, reply)
    listeners.append(heard)

    with pytest.raises(ListenError):
        heard.transcribe(SECOND, timeout=10)
    assert gone(pids(tmp_path)[0])


def test_an_empty_transcript_is_returned_as_it_is(tmp_path, listeners):
    # Whether too few words were heard is for whoever judges the song.
    heard = fake(tmp_path, 'answer({"text": ""})')
    listeners.append(heard)

    assert heard.transcribe(SECOND, timeout=10) == ""


def test_no_sound_at_all_raises_without_starting_a_worker(tmp_path, listeners):
    heard = fake(tmp_path, 'answer({"text": "something"})')
    listeners.append(heard)

    with pytest.raises(ListenError):
        heard.transcribe(np.zeros(0, dtype=np.float32), timeout=10)
    assert pids(tmp_path) == []


def test_close_kills_the_worker(tmp_path, listeners):
    heard = fake(tmp_path, 'answer({"text": "hi"})')
    listeners.append(heard)

    heard.transcribe(SECOND, timeout=10)
    heard.close()
    assert gone(pids(tmp_path)[0])
    # A Listener closed can still be used: the next song starts a new worker.
    assert heard.transcribe(SECOND, timeout=10) == "hi"
    assert len(pids(tmp_path)) == 2


def test_close_in_the_middle_of_a_song_fails_it_and_leaves_no_timer(tmp_path, listeners):
    heard = fake(tmp_path, "time.sleep(3600)", idle=0.2)
    listeners.append(heard)
    failed = []

    def song():
        try:
            heard.transcribe(SECOND, timeout=30)
        except ListenError as e:
            failed.append(e)

    singing = threading.Thread(target=song)
    started = time.monotonic()
    singing.start()
    assert soon(lambda: pids(tmp_path))
    heard.close()
    singing.join(10)
    assert failed and time.monotonic() - started < 10
    assert gone(pids(tmp_path)[0]) and heard._timer is None


def test_close_with_no_worker_is_fine(tmp_path):
    fake(tmp_path, 'answer({"text": "hi"})').close()


def test_the_worker_is_closed_once_idle(tmp_path, listeners):
    heard = fake(tmp_path, 'answer({"text": "hi"})', idle=0.2)
    listeners.append(heard)

    heard.transcribe(SECOND, timeout=10)
    assert soon(lambda: gone(pids(tmp_path)[0]))


def test_onnxruntimes_gpu_search_is_only_logged_as_info(tmp_path, listeners, caplog):
    # pve2 has no GPU, so onnxruntime says so each time the worker starts. That's expected.
    gpu = ('[W:onnxruntime:Default, device_discovery.cc:285 GetGpuDevices] Failed to detect '
           'devices under "/sys/class/drm/card1": device_discovery.cc:94 ReadFileContents '
           'Failed to open file: "/sys/class/drm/card1/device/vendor"')
    heard = fake(tmp_path, f"""\
        sys.stderr.write({gpu!r} + "\\nout of memory\\n")
        sys.stderr.flush()
        answer({{"text": "hi"}})
    """)
    listeners.append(heard)

    with caplog.at_level(logging.INFO, logger="voice_assistant.listener"):
        heard.transcribe(SECOND, timeout=10)
        assert soon(lambda: sum("out of memory" in r.getMessage() for r in caplog.records) == 1)
    lines = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "voice_assistant.listener"]
    assert [level for level, _ in lines] == [logging.INFO, logging.WARNING]
    assert "Failed to detect devices" in lines[0][1] and "out of memory" in lines[1][1]


def test_the_idle_timer_waits_while_a_song_is_being_judged(tmp_path, listeners):
    # Whoever judges a song holds the lock from fetching it to storing its verdict, and
    # some songs never reach transcribe. The worker is closed once they let go.
    heard = fake(tmp_path, 'answer({"text": "hi"})', idle=0.2)
    listeners.append(heard)
    heard.transcribe(SECOND, timeout=10)
    holding, release = threading.Event(), threading.Event()

    def judge():
        with heard.lock:
            holding.set()
            release.wait(10)

    judging = threading.Thread(target=judge)
    judging.start()
    holding.wait(10)
    time.sleep(0.6)
    assert not gone(pids(tmp_path)[0])
    release.set()
    judging.join(10)
    assert soon(lambda: gone(pids(tmp_path)[0]))


def test_a_song_longer_than_the_idle_time_keeps_its_worker(tmp_path, listeners):
    heard = fake(tmp_path, 'time.sleep(0.6); answer({"text": "hi"})', idle=0.2)
    listeners.append(heard)

    assert heard.transcribe(SECOND, timeout=10) == "hi"
    assert heard.transcribe(SECOND, timeout=10) == "hi"
    assert len(pids(tmp_path)) == 1


def test_the_workers_complaints_go_to_the_log_as_warnings(tmp_path, listeners, caplog):
    heard = fake(tmp_path, """\
        sys.stderr.write("model loaded\\nsecond line\\n")
        sys.stderr.flush()
        answer({"text": "hi"})
    """)
    listeners.append(heard)

    with caplog.at_level(logging.WARNING, logger="voice_assistant.listener"):
        assert heard.transcribe(SECOND, timeout=10) == "hi"
        assert soon(lambda: sum("second line" in r.getMessage() for r in caplog.records) == 1)
    lines = [r for r in caplog.records if r.name == "voice_assistant.listener"]
    assert [r.levelno for r in lines] == [logging.WARNING, logging.WARNING]
    assert "model loaded" in lines[0].getMessage() and "second line" in lines[1].getMessage()


def test_the_worker_is_run_by_nova_at_the_lowest_priority(monkeypatch):
    monkeypatch.setattr(listener.shutil, "which", lambda name: "/usr/bin/nice" if name == "nice" else None)

    assert listener.worker_command("/m/nemotron", 2) == [
        "/usr/bin/nice", "-n", "19", sys.executable, "-m", "voice_assistant.listen_worker",
        "/m/nemotron", "2"]


def test_without_nice_the_worker_runs_anyway_with_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(listener.shutil, "which", lambda name: None)

    with caplog.at_level(logging.WARNING):
        command = listener.worker_command("/m/nemotron", 3)
    assert command == [sys.executable, "-m", "voice_assistant.listen_worker", "/m/nemotron", "3"]
    # The worker lowers its own priority, so it doesn't run at the usual one.
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "There's no nice to start the listening worker with, so it lowers its own priority"]


def test_decode_asks_ffmpeg_for_mono_16khz_float(monkeypatch):
    asked = {}

    def run(command, **kwargs):
        asked.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, struct.pack("<3f", 0.5, -0.5, 0.25), b"")

    monkeypatch.setattr(listener.subprocess, "run", run)
    monkeypatch.setattr(listener.shutil, "which", lambda name: "/usr/bin/nice" if name == "nice" else None)

    samples = decode(b"an mp3")
    # At the lowest priority too: a first library check decodes hundreds of songs.
    assert asked["command"] == ["/usr/bin/nice", "-n", "19", "ffmpeg", "-v", "error", "-i", "pipe:0",
                                "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"]
    assert asked["input"] == b"an mp3"
    assert samples.dtype == np.float32 and samples.tolist() == [0.5, -0.5, 0.25]


def test_decode_without_nice_runs_ffmpeg_anyway(monkeypatch, caplog):
    asked = []

    def run(command, **kwargs):
        asked.append(command)
        return subprocess.CompletedProcess(command, 0, struct.pack("<f", 0.5), b"")

    monkeypatch.setattr(listener.subprocess, "run", run)
    monkeypatch.setattr(listener.shutil, "which", lambda name: None)

    with caplog.at_level(logging.WARNING):
        assert decode(b"an mp3").tolist() == [0.5]
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "There's no nice to run ffmpeg with, so it runs at the usual priority"]
    assert asked == [["ffmpeg", "-v", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000",
                      "-f", "f32le", "pipe:1"]]


def test_decode_that_ffmpeg_fails_raises(monkeypatch):
    monkeypatch.setattr(listener.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(
        command, 1, b"", b"pipe:0: Invalid data found when processing input"))

    with pytest.raises(ListenError, match="^couldn't decode the song$"):
        decode(b"not a song")


def test_decode_without_ffmpeg_raises(monkeypatch):
    def run(command, **kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(listener.subprocess, "run", run)

    with pytest.raises(ListenError, match="^couldn't decode the song$"):
        decode(b"an mp3")


class FakeNemotron:
    """Stands in for Nemotron in the worker: notes every piece it's given."""

    def __init__(self, fail=False):
        self.pieces = []
        self.fail = fail

    def start(self):
        return "stream"

    def add(self, stream, audio):
        if self.fail:
            raise RuntimeError("out of memory")
        self.pieces.append(len(audio))

    def finish(self, stream):
        return f"{len(self.pieces)} pieces"


def request(samples) -> bytes:
    data = np.asarray(samples, dtype="<f4").tobytes()
    return struct.pack("<I", len(data)) + data


def test_the_worker_hears_a_song_in_one_second_pieces_start_to_end():
    model = FakeNemotron()

    assert listen_worker.hear(model, np.zeros(16000 * 2 + 500, dtype=np.float32)) == "3 pieces"
    assert model.pieces == [16000, 16000, 500]


def test_the_worker_answers_each_song_with_a_line_of_json_until_its_input_ends():
    model = FakeNemotron()
    out = io.BytesIO()

    listen_worker.serve(model, io.BytesIO(request(SECOND) + request(np.zeros(20000))), out)
    assert [json.loads(line) for line in out.getvalue().splitlines()] == [
        {"text": "1 pieces"}, {"text": "3 pieces"}]


def test_the_worker_answers_a_failure_with_a_plain_error(capsys):
    out = io.BytesIO()

    listen_worker.serve(FakeNemotron(fail=True), io.BytesIO(request(SECOND)), out)
    answer = json.loads(out.getvalue())
    assert answer == {"error": "couldn't hear the song"}
    # The detail goes to its stderr, which Nova logs.
    assert "out of memory" in capsys.readouterr().err


def test_the_worker_stops_on_a_request_cut_short():
    out = io.BytesIO()

    listen_worker.serve(FakeNemotron(), io.BytesIO(request(SECOND)[:100]), out)
    assert out.getvalue() == b""
