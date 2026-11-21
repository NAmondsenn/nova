"""
Recording Module.

Records the user's query until they stop talking, from any source of audio: the
local microphone, or - on the brain - a feed of audio which a satellite sent over the
network. Both are read the same way, so the same tested algorithm, and the same
handling of pauses and false wake words, serves them.
"""

import logging
import math
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

# Logger setup
logger = logging.getLogger(__name__)

# The rate everything downstream works at.
RATE = 16000

# The recorder checks for speech 30ms at a time, the frame length openWakeWord's
# copy of Silero is built around.
FRAME = 480

# Once the user has started speaking, a frame only counts towards the silence which
# ends the recording when it's this far below the speech threshold - the same margin
# Silero's own tools use, so a word trailing off doesn't flicker in and out of
# "silence" and end the recording early.
SILENCE_MARGIN = 0.15

# A pause at least this long before the user starts talking means the moment kept
# from before listening started is no longer wanted - see record_until_silence.
PAUSE_SECONDS = 0.2

# How much is kept from just before the user started talking, once anything earlier
# has been dropped, so the first sound of their first word isn't clipped.
LEAD_IN_SECONDS = 0.15

# The voice-drop rule judges how loud it is over this many frames (0.15s) rather than
# one: short enough to follow his voice dying away, long enough that a single click
# doesn't set how loud he is, and the dips between syllables don't count as quiet.
VOICE_WINDOW = 5

# "No new words" only ends a recording once the sound has been at least this far below the
# loudest the user has been, in decibels, for as long as the quiet which ends one. His own
# drawn-out "ummm", a hum or a laugh has no new word in it either, but is about as loud as
# the rest of what he says, so it is never taken for him having stopped; a background which
# isn't speech, at a microphone close to him, is usually well below his voice. And the
# quiet has to last, as a pause no longer than one he could make in a quiet room today -
# straight after a long "ummm", when the words have long been still - mustn't end it either.
NO_WORDS_BELOW_DB = 6


def record_until_silence(source, detector, listen_from: Optional[int] = None,
                         keep_before: float = 0.0, timeout: float = 10.0,
                         silence_duration: float = 0.6, speech_timeout: float = 3.0,
                         min_speech: float = 0.12, timeline=None,
                         heard: Optional[Callable[[np.ndarray], None]] = None,
                         voice_drop: Optional[float] = None,
                         words_quiet: Optional[Callable[[], Optional[float]]] = None,
                         no_words_after: float = 1.5,
                         report: Optional[Dict] = None) -> np.ndarray:
    """
    Records until the user has spoken and then gone quiet, or a timeout is reached.

    Args:
        source: Where the audio comes from - a Microphone, or an AudioFeed. It needs
            position, listen(start) and time_at(position).
        detector: Says how likely each frame is to be speech - see vad.py.
        listen_from: Position to start listening from (see position), such as
            the moment the wake word was heard. Now if None.
        keep_before: Seconds of audio from before listen_from to keep as well.
            It isn't listened to for speech, but goes to speech-to-text with the rest,
            so words said straight after the wake word - which can come before
            it's even been detected - aren't cut off.
        timeout: Maximum recording duration in seconds, from listen_from.
        silence_duration: How long the user has to be quiet, once they've
            started talking, for the recording to stop.
        speech_timeout: How long to wait for the user to start talking before
            giving up, so a false wake word doesn't sit recording silence.
        min_speech: Seconds of continuous speech which count as the user
            starting to talk. Anything shorter - the end of the wake word, or
            the listening sound, which the microphone hears too - doesn't.
        timeline: Optional Timeline to mark when listening started, when speech
            started and ended, and when the recording stopped.
        heard: Given the recording as it's made, so speech-to-text can work out
            the words while the user is still saying them: everything up to the
            moment they're found to be talking, then each 30ms after it, as
            float32 samples which add up to exactly what's returned. Nothing is
            given until the user starts talking, so a false wake word costs
            nothing.
        voice_drop: Off if None. Otherwise a ratio, such as 0.126 for 18 dB down:
            once speech-to-text has heard a real word (see words_quiet), a frame also
            counts towards the silence when the last 0.15s of sound is below the
            loudest the user has been, times this. It's for a user close to a
            microphone, who is much louder there than a game or a video playing
            nearby, which Silero can still take for speech. How loud they have been
            comes only from 0.15s windows whose newest frame Silero calls speech, so
            a loud sound it doesn't - such as the listening chime - can't set it.
            Without words_quiet there is no word to wait for, so it counts as soon
            as the user is talking.
        words_quiet: Off if None. Says how long the user's words have been still:
            None until speech-to-text has heard a real word in the recording, then
            the seconds of audio since the words last changed - see
            NemotronTranscription.quiet_words. The recording ends when that reaches
            no_words_after and the last 0.15s has been at least NO_WORDS_BELOW_DB
            below the loudest the user has been (see voice_drop) for silence_duration:
            a backstop for loud sound which isn't speech, which the user's own
            drawn-out "ummm", as loud as the rest of what they say, doesn't set off.
            Until it gives a number neither this nor voice_drop applies, so a recording
            kept going by background sound before the user speaks isn't ended before
            they've said anything.
        no_words_after: Seconds of audio without a new word which end the recording.
        report: Optional dict, filled in when the recording ends with how it did.
            ended_by: What ended it - "silence", "voice drop" (a quiet run which
                voice_drop helped count), "no new words", "limit" (the timeout),
                "feed ended" (the source stopped after the user had started), or
                "no speech" (it was given up on before the user started, however
                that came about).
            loudness: How loud the user was, from 0 to 1, or None if not known.
            levels: How loud each 0.15s window was from when speech started, from 0
                to 1, to four places.
            speech: Silero's mean probability of speech in each of those windows, to
                two places.
            words_quiet: The last number words_quiet gave, or None if it never did.
            Only numbers and the name of what ended it - never audio or words.

    Returns:
        The recording as float32 samples at 16kHz, or an empty array if the user
        never started talking.
    """
    logger.info("Recording until silence...")
    if listen_from is None:
        listen_from = source.position
    if timeline:
        timeline.mark("recording_started")

    reader = source.listen(start=max(0, listen_from - int(keep_before * RATE)))
    kept = []
    if reader.position < listen_from:
        kept.append(reader.read(listen_from - reader.position))

    detector.reset()
    max_frames = int(timeout * RATE / FRAME)
    speech_timeout_frames = int(speech_timeout * RATE / FRAME)
    frames_to_start = max(1, round(min_speech * RATE / FRAME))
    frames_to_stop = max(1, round(silence_duration * RATE / FRAME))

    speech_frames = 0
    silent_frames = 0
    speech_started = False
    # Where the user last made a sound - the moment they actually stopped talking,
    # as opposed to when the silence after it was long enough to stop recording.
    speech_ended_at = None
    # Whether there was a pause before the user started talking - see below.
    quiet_frames = 0
    paused = False

    # How loud the last VOICE_WINDOW frames were, kept as running sums over two small
    # rings of the frames' own numbers - a sum of squares and Silero's probability
    # each - so sliding the window on costs one frame's arithmetic and no copy of the
    # frames before it. Only worked out when something will use it.
    measuring = voice_drop is not None or words_quiet is not None or report is not None
    square_ring = [0.0] * VOICE_WINDOW
    speech_ring = [0.0] * VOICE_WINDOW
    window_squares = 0.0
    window_speech = 0.0
    # Frames since the user started talking, counted from the first of the run of speech
    # which started it. A window only counts once it is made wholly of those, so
    # whatever sounded before - the listening chime, which the microphone hears too and
    # which Silero doesn't call speech - can't make the user seem louder than they are.
    since_start = 0
    loudness = None
    levels: List[float] = []
    speeches: List[float] = []
    last_still = None
    # Whether the quiet run so far has needed the voice drop to count a frame.
    voice_only = False
    # How many frames in a row have been well below the user's voice - see NO_WORDS_BELOW_DB.
    no_words_below = 10 ** (-NO_WORDS_BELOW_DB / 20)
    below_frames = 0
    ended_by = "limit"

    def note(by: str):
        """Says how the recording ended, in the report if there is one."""
        if report is not None:
            report.update(ended_by=by,
                          loudness=None if loudness is None else round(loudness, 4),
                          levels=levels, speech=speeches,
                          words_quiet=None if last_still is None else round(last_still, 2))

    for i in range(max_frames):
        frame = reader.read(FRAME)
        kept.append(frame)
        if speech_started and heard:
            heard(frame.astype(np.float32) / 32768.0)
        if len(frame) < FRAME:
            # A source which has ended (see AudioFeed.end) gives what's left, which
            # is too short for the detector. Nothing more is coming, so whatever
            # was said is the recording - unless the user hadn't started yet.
            if not speech_started:
                logger.info("The audio ended before any speech, cancelling")
                note("no speech")
                return np.array([], dtype=np.float32)
            ended_by = "feed ended"
            break
        probability = detector.speech_probability(frame)

        if measuring:
            samples = frame.astype(np.float32)
            squares = float(np.dot(samples, samples))
            chance = float(probability)
            slot = i % VOICE_WINDOW
            window_squares += squares - square_ring[slot]
            window_speech += chance - speech_ring[slot]
            square_ring[slot] = squares
            speech_ring[slot] = chance

        if not speech_started:
            # Gives up early if the user never started talking, so a false wake
            # word doesn't leave the assistant recording silence.
            if i >= speech_timeout_frames:
                logger.info(f"No speech within {speech_timeout:.1f}s, cancelling")
                note("no speech")
                return np.array([], dtype=np.float32)

            speech_frames = speech_frames + 1 if probability >= detector.threshold else 0
            quiet_frames = 0 if speech_frames else quiet_frames + 1
            paused = paused or quiet_frames * FRAME >= PAUSE_SECONDS * RATE
            if speech_frames >= frames_to_start:
                speech_started = True
                speech_ended_at = reader.position
                since_start = speech_frames
                # The moment kept from before listening started is there for "Hey
                # Nova what time is it" said in one breath. When the user paused
                # after the wake word instead - for the listening sound, usually -
                # it only holds the end of "Nova", which Whisper hears as "Turn
                # over," or "Get over." and which then stops a plain "set a timer"
                # being recognised. So everything before the pause is dropped -
                # now, before any of it is handed over, since it can't be taken
                # back once speech-to-text has heard it.
                if paused:
                    keep_from = reader.position - speech_frames * FRAME - int(LEAD_IN_SECONDS * RATE)
                    so_far = np.concatenate(kept)
                    kept = [so_far[max(0, keep_from - (reader.position - len(so_far))):]]
                if heard:
                    heard(np.concatenate(kept).astype(np.float32) / 32768.0)
                logger.info("Speech detected, listening for the end of the query...")
                if timeline:
                    timeline.mark("speech_started",
                                  at=source.time_at(reader.position - speech_frames * FRAME))
        else:
            level = None
            if measuring:
                since_start += 1
                if since_start >= VOICE_WINDOW:
                    level = math.sqrt(max(window_squares, 0.0) / (VOICE_WINDOW * FRAME)) / 32768.0
                    if since_start % VOICE_WINDOW == 0:
                        levels.append(round(level, 4))
                        speeches.append(round(max(window_speech, 0.0) / VOICE_WINDOW, 2))
                    # Only a window whose newest frame Silero calls speech says how loud
                    # the user is.
                    if probability >= detector.threshold and (loudness is None or level > loudness):
                        loudness = level

            # How long the words have been still - a number only once a real word has
            # been heard. Until then neither new rule applies, as the background sound
            # they're for can be called speech from the moment of the press, and the
            # first word only follows a moment later.
            still = words_quiet() if words_quiet is not None else None
            if still is not None:
                last_still = still
            # With no words_quiet to ask there's no word to wait for.
            dropped = (voice_drop is not None and (words_quiet is None or still is not None)
                       and level is not None and loudness is not None
                       and level < loudness * voice_drop)

            if probability < detector.threshold - SILENCE_MARGIN or dropped:
                silent_frames += 1
                # A frame Silero calls speech which counts only because of the voice drop.
                voice_only = voice_only or probability >= detector.threshold - SILENCE_MARGIN
                if silent_frames >= frames_to_stop:
                    ended_by = "voice drop" if voice_only else "silence"
                    logger.info(f"{'Voice dropped away' if voice_only else 'Silence detected'} after "
                                f"{(reader.position - listen_from) / RATE:.1f}s")
                    break
            else:
                silent_frames = 0
                voice_only = False
                speech_ended_at = reader.position

            below = level is not None and loudness is not None and level < loudness * no_words_below
            below_frames = below_frames + 1 if below else 0
            if still is not None and still >= no_words_after and below_frames >= frames_to_stop:
                ended_by = "no new words"
                logger.info(f"No new words for {still:.1f}s, stopping after "
                            f"{(reader.position - listen_from) / RATE:.1f}s")
                break

    if timeline:
        if speech_started:
            timeline.mark("speech_ended", at=source.time_at(speech_ended_at))
        timeline.mark("recording_stopped")

    note(ended_by)
    audio = np.concatenate(kept).astype(np.float32) / 32768.0
    logger.info(f"Recorded {len(audio)} samples")
    return audio


# How long a feed may go without any audio, while something is waiting for it, before
# it counts as ended - the same as the microphone's stall timeout. A satellite which
# drops off the network without a clean disconnect (a Wi-Fi drop, say) never says it
# has finished, and nothing would otherwise wake the recording waiting on it.
STALL_SECONDS = 2.0

# The most a feed keeps, in seconds - the longest request which can be set (30, see
# config.LIMITS) and the most pre-roll a wake may say its recording starts with (10,
# protocol.MAX_AUDIO), so a satellite which doesn't stop sending can't grow it for
# ever. In practice the satellite stops at the request's own limit, pre-roll included.
MAX_FEED_SECONDS = 40.0


class AudioFeed:
    """
    A source of audio fed from elsewhere, such as a satellite's microphone arriving
    over the network, which record_until_silence reads just as it reads a Microphone.

    Positions are counted in samples at 16kHz since the first one was fed. Everything
    fed is kept, since one feed is only ever one query's worth of audio - up to a cap.
    """

    def __init__(self, stall_seconds: float = STALL_SECONDS, max_seconds: float = MAX_FEED_SECONDS):
        """
        Args:
            stall_seconds: How long a reader waits with no new audio before the feed
                is taken to have ended.
            max_seconds: The most audio it keeps - anything fed after that is dropped.
        """
        self.stall_seconds = stall_seconds
        self._max_samples = round(max_seconds * RATE)
        self._over_cap = False
        self._samples = np.zeros(0, dtype=np.int16)
        # The position each piece fed ends at, and when it arrived, for time_at.
        self._arrivals: List[Tuple[int, float]] = []
        self._ended = False
        self._pressed = False
        self._condition = threading.Condition()

    def feed(self, samples: np.ndarray):
        """
        Adds newly arrived audio, and wakes anything waiting for it. Audio fed once the
        feed has ended - by end(), or by a reader finding it stalled - is ignored, as
        whatever was reading it has already been told there's no more. So is whatever
        comes after the cap (see __init__), of which the first time is logged.

        Args:
            samples: 16-bit samples at 16kHz.
        """
        with self._condition:
            if self._ended:
                return
            room = self._max_samples - len(self._samples)
            if len(samples) > room:
                if not self._over_cap:
                    self._over_cap = True
                    logger.warning(f"More than {self._max_samples / RATE:.0f}s of audio fed into one recording, "
                                   f"dropping the rest")
                samples = samples[:room]
                if len(samples) == 0:
                    return
            self._samples = np.concatenate([self._samples, samples.astype(np.int16)])
            self._arrivals.append((len(self._samples), time.monotonic()))
            self._condition.notify_all()

    def end(self, pressed: bool = False):
        """
        Says nothing more is coming. A reader which is waiting gets what's left, and
        so the recording stops.

        Args:
            pressed: True when the satellite ended it on a press of its key.
        """
        with self._condition:
            if not self._ended:
                self._pressed = pressed
            self._ended = True
            self._condition.notify_all()

    @property
    def ended(self) -> bool:
        """Whether nothing more is coming - by end(), or a reader finding it stalled."""
        with self._condition:
            return self._ended

    @property
    def pressed(self) -> bool:
        """Whether it was ended on a press of the satellite's key - see end()."""
        with self._condition:
            return self._pressed

    @property
    def position(self) -> int:
        """How many samples have been fed so far - the position of "now"."""
        with self._condition:
            return len(self._samples)

    def listen(self, start: Optional[int] = None) -> "AudioFeedReader":
        """
        Starts reading the audio from a position onwards.

        Args:
            start: Where to start - now if None.

        Returns:
            A reader which keeps its own place.
        """
        return AudioFeedReader(self, self.position if start is None else start)

    def time_at(self, position: int) -> float:
        """
        When a position arrived, as a time.monotonic() value - for timing marks.
        Within a piece it's counted back from the piece's arrival, since the piece
        was all heard before it was sent.

        Args:
            position: A position, as from the position property.
        """
        with self._condition:
            for end, arrived in self._arrivals:
                if position <= end:
                    return arrived - (end - position) / RATE
            return self._arrivals[-1][1] if self._arrivals else time.monotonic()

    def _read(self, start: int, count: int) -> np.ndarray:
        """
        Waits until `count` samples from `start` have been fed, or the feed has
        ended, then returns them.

        Returns:
            The samples - fewer than asked for, or none, only if the feed ended or
            stalled.
        """
        with self._condition:
            while not self._ended and len(self._samples) < start + count:
                heard = len(self._samples)
                if not self._condition.wait_for(
                        lambda: self._ended or len(self._samples) != heard,
                        timeout=self.stall_seconds):
                    logger.warning(f"No audio for {self.stall_seconds:.1f}s, "
                                   f"taking the feed to have ended")
                    self._ended = True
            return self._samples[start:start + count].copy()


class AudioFeedReader:
    """One reader's place in an AudioFeed, which moves on as it reads."""

    def __init__(self, feed: AudioFeed, position: int):
        """
        Args:
            feed: The feed to read from.
            position: Where to start reading - see AudioFeed.position.
        """
        self.feed = feed
        self.position = position

    def read(self, count: int) -> np.ndarray:
        """
        The next samples, waiting for them to be fed if need be.

        Args:
            count: How many samples to read.

        Returns:
            16-bit samples at 16kHz - fewer than `count` only once the feed has ended.
        """
        samples = self.feed._read(self.position, count)
        self.position += len(samples)
        return samples
