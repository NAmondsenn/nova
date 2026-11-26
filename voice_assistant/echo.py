"""
Echo Module.

Takes what the assistant itself is playing - music from Navidrome, its own voice,
timers - back out of what the microphone hears, so "Hey Nova" can still be heard
over the music. The microphone was hearing the music louder than the user from
across the room, and the wake word was often missed under it.

The speaker keeps a copy of everything it plays (PlayedAudio), and the microphone
passes what it hears through speexdsp's echo canceller (EchoCanceller), the one
Linux voice software has long used, with that copy lined up against it. setup.sh
installs speexdsp (libspeexdsp1) rather than counting on the Pi having it; without
it, the microphone is simply left as it was. Windows has no such package, so the PC's
install (scripts/windows/install.ps1) puts speexdsp.dll in the lib folder of the
assistant's data, and that is the one loaded there. The PC's only takes anything out
while a conversation holds its speaker and microphone open (see EchoCanceller.engaged):
between presses they're closed, and an ordinary press hears the microphone as it is.

Lining them up is the hard part, for two reasons found on the Pi:

- The speaker and microphone are separate USB devices, each with a clock of its
  own, and they don't agree: over 40 seconds the speaker ran 47 parts per million
  slow and the microphone 2,876 fast, so the two drift apart by about 46 samples a
  second. Each one's real rate is worked out from PortAudio's timestamps - steady
  for the speaker, jittering by a few milliseconds for the microphone, so fitted
  over the last ten minutes, which a fit over 20 seconds wandered too much for -
  and what was played is stretched onto the microphone's timeline before it's
  taken out.
- The gap between a sound being played and being heard was 55ms one run and
  145ms the next, as the shared output and input's periods - 125ms each, then -
  fell differently each time they were opened. With 21ms ones it's 20-40ms, but
  still not the same twice. So it's measured as the assistant runs, by matching
  what was played against what was heard, every couple of seconds - and since one
  measurement can be a few milliseconds out, the middle of the last few is used,
  once enough of them agree.
"""

import ctypes
import ctypes.util
import logging
import os
import threading
from collections import deque
from typing import Optional, Tuple

import numpy as np
import soxr

from voice_assistant import paths

# Logger setup
logger = logging.getLogger(__name__)

RATE = 16000
# The canceller works through 16ms at a time, and models 128ms of the room's echo
# after the sound first arrives - the gap before that is lined up separately.
FRAME = 256
FILTER = 2048

# How much of what was played, and heard, is kept to line them up against.
HISTORY_SECONDS = 4.0
# The longest gap between playing and hearing that's looked for.
MOST_DELAY_SECONDS = 0.4
# How often the gap is measured again, and over how much audio.
MEASURE_EVERY_SECONDS = 2.0
MEASURE_OVER_SECONDS = 1.5
# The gap is the middle of the last few measurements, once this many of them lie
# within AGREEMENT of it. In a real room they differed by a few milliseconds - the
# room's reflections competing with the sound straight from the speaker - so the
# canceller is started MARGIN early, and models the 128ms from there, which takes in
# the whole echo whichever was measured.
MEASUREMENTS_KEPT = 5
MEASUREMENTS_AGREEING = 3
AGREEMENT = 80
MARGIN = 160
# What's played has to be at least this loud to measure the gap by - silence lines
# up with anything.
QUIETEST_TO_MEASURE = 300
# How much sharper the best match has to be than a typical one to be believed. A
# real echo stood out a few hundred times over in tests, and 20 to 40 times in a
# real room; two unrelated stretches of music with the same rhythm, around 8 times.
# The same gap has to be found twice running as well.
CLEAR_MATCH = 12.0
# How many measurements running, of something loud enough to measure by, have to find no
# match at all before the microphone is taken not to hear the speaker - headphones, most
# likely - so there's nothing to take out and nothing to wait for.
UNHEARD_AFTER = 3
# How long each stream's timestamps are fitted over, and how much of them is needed
# before they're trusted.
CLOCK_SECONDS = 600.0
LEAST_CLOCK_SECONDS = 5.0
# A timestamp this far from where the fit puts it means the stream was reopened,
# and its clock is taken afresh.
CLOCK_JUMP_SECONDS = 0.05

# What the PC's install calls the library it ships, in the lib folder of the data folder.
WINDOWS_LIBRARIES = ("speexdsp.dll", "libspeexdsp-1.dll")

# From speexdsp's speex_echo.h and speex_preprocess.h.
SPEEX_ECHO_SET_SAMPLING_RATE = 24
SPEEX_PREPROCESS_SET_DENOISE = 0
SPEEX_PREPROCESS_SET_ECHO_STATE = 24


def _on_windows() -> bool:
    """Whether this is Windows, where the PC's satellite runs."""
    return os.name == "nt"


def library_path() -> Optional[str]:
    """
    Where speexdsp is: on Windows, the one the install shipped, since the system has
    none to find; elsewhere, wherever the system keeps it.

    Returns:
        The path, or None if it isn't there.
    """
    if not _on_windows():
        return ctypes.util.find_library("speexdsp")
    for name in WINDOWS_LIBRARIES:
        path = os.path.join(paths.DATA_DIR, "lib", name)
        if os.path.isfile(path):
            return path
    return None


class Samples:
    """
    The last few seconds of a stream of 16kHz samples, read back by position - the
    number of samples since the stream started - and the stream's own clock: which
    position was played or heard at any moment, fitted to PortAudio's timestamps.
    """

    def __init__(self, seconds: float = HISTORY_SECONDS):
        self._buffer = np.zeros(int(seconds * RATE), dtype=np.int16)
        self.total = 0
        # (timestamp, position) for each block, over the last CLOCK_SECONDS.
        self._anchors: deque = deque()
        # (reference time, samples a second, position at the reference time).
        self.clock: Optional[Tuple[float, float, float]] = None
        # Nothing before this position is read back - see forget().
        self._floor = 0
        # Bumped by forget(), so a fit of the clock which began before it isn't kept.
        self._generation = 0
        self._lock = threading.Lock()

    def add(self, samples: np.ndarray, start_time: Optional[float]):
        """
        Keeps some samples.

        Args:
            samples: int16 samples, following on from the last.
            start_time: When the first of them was played or heard, by PortAudio's
                clock, or None if it didn't say.
        """
        with self._lock:
            if start_time:
                clock = self.clock
                if clock and abs(self._position_at(clock, start_time) - self.total) > CLOCK_JUMP_SECONDS * RATE:
                    # Reopened: the old timestamps no longer fit.
                    self._anchors.clear()
                    self.clock = None
                self._anchors.append((start_time, self.total))
                while self._anchors and start_time - self._anchors[0][0] > CLOCK_SECONDS:
                    self._anchors.popleft()
            capacity = len(self._buffer)
            samples = samples[-capacity:]
            start = self.total % capacity
            first = min(len(samples), capacity - start)
            self._buffer[start:start + first] = samples[:first]
            self._buffer[:len(samples) - first] = samples[first:]
            self.total += len(samples)

    def forget(self):
        """
        Lets go of everything kept so far, and the clock fitted to it, for when the stream
        is opened afresh. Positions carry on, so whatever counts along with them stays right.
        """
        with self._lock:
            self._anchors.clear()
            self.clock = None
            self._floor = self.total
            self._generation += 1

    def fit_clock(self):
        """Fits the stream's clock to its recent timestamps - see the top of this module."""
        with self._lock:
            anchors = np.array(self._anchors, dtype=np.float64)
            generation = self._generation
        if len(anchors) < 4 or anchors[-1, 0] - anchors[0, 0] < LEAST_CLOCK_SECONDS:
            return
        reference = anchors[-1, 0]
        rate, position = np.polyfit(anchors[:, 0] - reference, anchors[:, 1], 1)
        with self._lock:
            # Not if the stream was forgotten meanwhile: that fit is of the old one.
            if generation == self._generation:
                self.clock = (reference, rate, position)

    @staticmethod
    def _position_at(clock, when):
        reference, rate, position = clock
        return position + rate * (np.asarray(when) - reference)

    def position_at(self, when):
        """Which position was played or heard at a time, or times - None until the clock is fitted."""
        clock = self.clock
        return None if clock is None else self._position_at(clock, when)

    def time_at(self, position):
        """When a position, or positions, was played or heard - None until the clock is fitted."""
        clock = self.clock
        if clock is None:
            return None
        reference, rate, at = clock
        return reference + (np.asarray(position, dtype=np.float64) - at) / rate

    def read(self, position: int, count: int) -> np.ndarray:
        """Samples from a position on - silence for any not kept, or not there yet."""
        out = np.zeros(count, dtype=np.int16)
        with self._lock:
            oldest = max(0, self.total - len(self._buffer), self._floor)
            begin, end = max(position, oldest), min(position + count, self.total)
            for index in range(begin, end, len(self._buffer)):
                stop = min(end, index + len(self._buffer))
                ring = index % len(self._buffer)
                length = min(stop - index, len(self._buffer) - ring)
                out[index - position:index - position + length] = self._buffer[ring:ring + length]
                if length < stop - index:
                    out[index - position + length:stop - position] = self._buffer[:stop - index - length]
        return out

    def read_at(self, positions: np.ndarray) -> np.ndarray:
        """
        Samples at positions which needn't be whole numbers, interpolated - for
        stretching one stream onto another's timeline.
        """
        first = int(np.floor(positions.min()))
        samples = self.read(first, int(np.ceil(positions.max())) - first + 2).astype(np.float64)
        return np.interp(positions - first, np.arange(len(samples)), samples)


class PlayedAudio(Samples):
    """What the speaker has played lately, at 16kHz - see Speaker._fill."""

    def __init__(self, speaker_rate: int = 48000):
        super().__init__()
        self._resampler = soxr.ResampleStream(speaker_rate, RATE, 1, dtype="float32")
        # Whether what's played is kept. The echo canceller switches it off while it
        # isn't taking anything out, so a speaker which plays all day costs nothing.
        self.keeping = True

    def played(self, audio: np.ndarray, dac_time: Optional[float]):
        """
        Keeps a block the speaker has just been given.

        Args:
            audio: float32 mono samples at the speaker's rate.
            dac_time: When its first sample leaves the speaker, by PortAudio's clock.
        """
        if not self.keeping:
            return
        resampled = self._resampler.resample_chunk(np.asarray(audio, dtype=np.float32))
        self.add((np.clip(resampled, -1.0, 1.0) * 32767).astype(np.int16), dac_time)


class Speex:
    """speexdsp's echo canceller, and the residual echo suppression which goes with it."""

    def __init__(self, frame: int = FRAME, filter_length: int = FILTER, rate: int = RATE):
        """
        Raises:
            OSError: If speexdsp isn't installed.
        """
        name = library_path()
        if name is None:
            if _on_windows():
                # Says where the library should be, rather than trying a Linux name.
                raise OSError(f"{WINDOWS_LIBRARIES[0]} isn't in {os.path.join(paths.DATA_DIR, 'lib')}")
            name = "libspeexdsp.so.1"
        self._lib = lib = ctypes.CDLL(name)
        samples = ctypes.POINTER(ctypes.c_int16)
        lib.speex_echo_state_init.restype = ctypes.c_void_p
        lib.speex_echo_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.speex_echo_cancellation.argtypes = [ctypes.c_void_p, samples, samples, samples]
        lib.speex_echo_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        lib.speex_echo_state_reset.argtypes = [ctypes.c_void_p]
        lib.speex_echo_state_destroy.argtypes = [ctypes.c_void_p]
        lib.speex_preprocess_state_init.restype = ctypes.c_void_p
        lib.speex_preprocess_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.speex_preprocess_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        lib.speex_preprocess_run.argtypes = [ctypes.c_void_p, samples]
        lib.speex_preprocess_state_destroy.argtypes = [ctypes.c_void_p]

        self.frame = frame
        self._echo = lib.speex_echo_state_init(frame, filter_length)
        lib.speex_echo_ctl(self._echo, SPEEX_ECHO_SET_SAMPLING_RATE, ctypes.byref(ctypes.c_int(rate)))
        self._suppress = lib.speex_preprocess_state_init(frame, rate)
        lib.speex_preprocess_ctl(self._suppress, SPEEX_PREPROCESS_SET_ECHO_STATE, ctypes.c_void_p(self._echo))
        # Only the echo is taken out - not background noise, which the wake word and
        # Whisper were trained to cope with as it is.
        lib.speex_preprocess_ctl(self._suppress, SPEEX_PREPROCESS_SET_DENOISE, ctypes.byref(ctypes.c_int(0)))
        self._out = np.zeros(frame, dtype=np.int16)

    def cancel(self, heard: np.ndarray, played: np.ndarray) -> np.ndarray:
        """One frame heard, with what was played that it's an echo of taken out."""
        heard = np.ascontiguousarray(heard, dtype=np.int16)
        played = np.ascontiguousarray(played, dtype=np.int16)
        pointer = ctypes.POINTER(ctypes.c_int16)
        self._lib.speex_echo_cancellation(self._echo, heard.ctypes.data_as(pointer),
                                          played.ctypes.data_as(pointer), self._out.ctypes.data_as(pointer))
        self._lib.speex_preprocess_run(self._suppress, self._out.ctypes.data_as(pointer))
        return self._out.copy()

    def reset(self):
        """Forgets the room's echo, for when what's played and heard are lined up afresh."""
        self._lib.speex_echo_state_reset(self._echo)


def measure_delay(heard: np.ndarray, played: np.ndarray, most: int) -> Optional[int]:
    """
    How many samples later something played turns up in what was heard.

    Args:
        heard: Samples heard.
        played: What was played over the same stretch, starting `most` samples
            earlier, so a delay of up to `most` can be found.
        most: The longest delay looked for.

    Returns:
        The delay, or None if no match stands out clearly enough to believe.
    """
    if np.sqrt(np.mean(played.astype(np.float64) ** 2)) < QUIETEST_TO_MEASURE:
        return None
    size = 1 << int(np.ceil(np.log2(len(played) + len(heard))))
    spectrum = np.fft.rfft(played.astype(np.float64), size) * np.conj(np.fft.rfft(heard.astype(np.float64), size))
    # Weighted by phase alone (GCC-PHAT), which gives a sharp peak at the true delay
    # even for music, whose loudest notes would otherwise smear it out.
    correlation = np.abs(np.fft.irfft(spectrum / (np.abs(spectrum) + 1e-9), size))[:most + 1]
    best = int(np.argmax(correlation))
    if correlation[best] < CLEAR_MATCH * np.median(correlation):
        return None
    return most - best


class EchoCanceller:
    """Takes what was played back out of what's heard - see the top of this module."""

    def __init__(self, played: PlayedAudio, speex: Optional[Speex] = None, engaged: bool = True):
        """
        Args:
            played: What the speaker has played, which it keeps for this.
            speex: The canceller - made here unless given, as the tests do.
            engaged: Whether it takes anything out from the start - see `engaged`.

        Raises:
            OSError: If speexdsp isn't installed.
        """
        self.played = played
        self.heard = Samples()
        self.speex = speex or Speex()
        self.frame = self.speex.frame
        # Samples between being played and being heard, once measured.
        self.delay: Optional[int] = None
        self._measured: deque = deque(maxlen=MEASUREMENTS_KEPT)
        # Whether the microphone doesn't hear what's played at all, and how many measurements
        # running have found no match in something loud. Kept through a reset, so headphones
        # needn't be found again each time the devices are opened, and dropped by the first match.
        self.unheard = False
        self._unmatched = 0
        self._pending = np.zeros(0, dtype=np.int16)
        self._done = 0
        # Bumped by every reset, so a measurement which began before one isn't kept.
        self._generation = 0
        self._engaged = engaged
        self.played.keeping = engaged
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        threading.Thread(target=self._keep_measuring, daemon=True, name="echo-delay").start()

    @property
    def engaged(self) -> bool:
        """
        Whether it is taking anything out. A wake word satellite's always is. A push-to-talk
        satellite's is only while its speaker and microphone are held open for a
        conversation: engaging it resets it, as they were opened afresh, and until then
        what's heard is passed on exactly as it came and nothing played is kept.
        """
        return self._engaged

    @engaged.setter
    def engaged(self, on: bool):
        with self._lock:
            if on == self._engaged:
                return
            self._engaged = on
            self.played.keeping = on
            self._forget()

    @property
    def ready(self) -> bool:
        """
        Whether Nova's own sound is out of what's heard - engaged, and either the speaker is lined
        up with the microphone and being taken out (the gap measured and agreed), or the
        microphone doesn't hear the speaker at all, as with headphones.
        """
        return self._engaged and (self.delay is not None or self.unheard)

    def reset(self):
        """
        Forgets what was lined up, for when the speaker and microphone are opened afresh
        and the gap between them, which differs each time, has to be found again. What
        was measured before is dropped too, or it would be agreed with at once, and so is
        what was heard and played, or the old gap would be found again in it.
        """
        with self._lock:
            self._forget()

    def _forget(self):
        """Does reset's work, with the lock held."""
        self._generation += 1
        self.delay = None
        self._measured.clear()
        self._unmatched = 0
        self.heard.forget()
        self.played.forget()
        # What was waiting for a whole frame is of the old stream too, and what's heard next
        # is the first to be taken out.
        self._pending = np.zeros(0, dtype=np.int16)
        self._done = self.heard.total
        self.speex.reset()

    def process(self, samples: np.ndarray, adc_time: Optional[float]) -> np.ndarray:
        """
        Cleans a block the microphone has just heard. Called on the microphone's own
        thread. Works a frame at a time, so a little may be held back until the next
        block, which only ever delays what's heard by up to 16ms.

        Args:
            samples: int16 samples at 16kHz.
            adc_time: When the first of them was heard, by PortAudio's clock.

        Returns:
            The cleaned samples - or the same ones, if it isn't engaged.
        """
        with self._lock:
            if not self._engaged:
                return samples
            self.heard.add(samples, adc_time)
            self._pending = np.concatenate((self._pending, samples))
            delay, frames = self.delay, len(self._pending) // self.frame
            out = np.empty(frames * self.frame, dtype=np.int16)
            for index in range(frames):
                heard = self._pending[index * self.frame:(index + 1) * self.frame]
                played = self._played_for(self._done, self.frame, delay)
                out[index * self.frame:(index + 1) * self.frame] = self.speex.cancel(heard, played)
                self._done += self.frame
            self._pending = self._pending[frames * self.frame:]
        return out

    def _played_for(self, position: int, count: int, delay: Optional[int]) -> np.ndarray:
        """
        What was played that's heard from a position on, stretched onto the
        microphone's timeline - silence until both clocks are known and the gap measured.
        """
        played = self._played_positions(position, count, delay or 0)
        if delay is None or played is None:
            return np.zeros(count, dtype=np.int16)
        return np.clip(np.round(self.played.read_at(played)), -32768, 32767).astype(np.int16)

    def _played_positions(self, position: int, count: int, delay: int) -> Optional[np.ndarray]:
        """
        Where in what was played each heard sample from a position on comes from,
        given the gap - or None until both clocks are known.
        """
        times = self.heard.time_at(np.arange(position, position + count) - delay)
        return None if times is None else self.played.position_at(times)

    def _keep_measuring(self):
        while not self._stopping.wait(MEASURE_EVERY_SECONDS):
            if not self._engaged:
                continue
            try:
                self.played.fit_clock()
                self.heard.fit_clock()
                self.measure()
            except Exception as e:
                logger.warning(f"Couldn't measure the echo's delay: {e}")

    def measure(self):
        """Measures the gap between playing and hearing again, adopting it once it's seen twice."""
        generation = self._generation
        most = int(MOST_DELAY_SECONDS * RATE)
        count = int(MEASURE_OVER_SECONDS * RATE)
        end = self.heard.total
        # What was played over the same stretch, on the microphone's timeline, from
        # `most` samples before it, so a gap of up to that can be found.
        played_positions = self._played_positions(end - count - most, count + most, 0)
        if played_positions is None:
            return
        heard = self.heard.read(end - count, count)
        played = self.played.read_at(played_positions)
        found = measure_delay(heard, played, most)
        if found is None:
            if np.sqrt(np.mean(played.astype(np.float64) ** 2)) >= QUIETEST_TO_MEASURE:
                self._note_unmatched(generation)
            return
        # From the first note of it to the last, under the lock which reset() takes, so a
        # reset can't come between them, and a measurement made of what was there before
        # one isn't kept after it.
        with self._lock:
            if generation != self._generation:
                return
            self._measured.append(found)
            # The speaker is heard after all - headphones swapped for a loudspeaker, say.
            self._unmatched = 0
            self.unheard = False
            middle = int(np.median(self._measured))
            if sum(abs(measured - middle) <= AGREEMENT for measured in self._measured) < MEASUREMENTS_AGREEING:
                return
            delay = max(0, middle - MARGIN)
            if self.delay is not None and abs(delay - self.delay) <= AGREEMENT:
                return
            self.delay = delay
            self.speex.reset()
        logger.info(f"Echo cancelling: what's played is heard {1000 * middle / RATE:.0f}ms later")

    def _note_unmatched(self, generation: int):
        """
        Notes a measurement which found nothing of something loud in what was heard, and takes
        the microphone not to hear the speaker once enough have running, while no gap is agreed.

        Args:
            generation: The generation the measurement began in - one from before a reset isn't kept.
        """
        with self._lock:
            if generation != self._generation:
                return
            self._unmatched += 1
            if self.unheard or self.delay is not None or self._unmatched < UNHEARD_AFTER:
                return
            # A stray match from before these, which nothing agreed with, goes.
            self._measured.clear()
            self.unheard = True
        logger.info("Echo cancelling: the microphone doesn't hear what's played - headphones, most likely - "
                    "so there's nothing to take out")

    def close(self):
        self._stopping.set()


def make_echo_canceller(config, speaker_rate: int, engaged: bool = True) -> Optional[EchoCanceller]:
    """
    The echo canceller audio.echo_cancellation in config.yaml asks for, or None if
    it's switched off or speexdsp isn't installed.

    Args:
        config: The settings.
        speaker_rate: What the speaker plays at.
        engaged: Whether it takes anything out from the start - see EchoCanceller.engaged.
    """
    if not config.section("audio").get("echo_cancellation", True):
        return None
    try:
        canceller = EchoCanceller(PlayedAudio(speaker_rate), engaged=engaged)
    except OSError as e:
        logger.warning(f"No echo cancelling - speexdsp isn't installed ({e}). "
                       f"Music the assistant plays will be harder to talk over.")
        return None
    logger.info("Echo cancelling on")
    return canceller
