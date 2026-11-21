"""
Cut-in Module.

Decides, while a reply plays in a conversation, what a voice heard over it means - see
docs/superpowers/specs/2026-11-21-conversation-mode-design.md, "Talking over a reply".
The satellite sends the brain what its microphone hears with Nova's own sound taken out;
CutIn is given that a frame at a time, with the words speech-to-text has made of it, and
says when the reply's volume should dip (a voice has started), when it should stop (real
words have come: he is cutting in) and when it should come back (no words came).

It stops for words, not for sound, because the echo canceller never takes out all of
Nova's own voice: what's left reads as speech, and would stop every reply the moment it
began. So words which are a run of the reply's own (own_words), or only listening noises
such as "yeah" and "mm-hm" (only_listening_noises), never stop it - but "stop" always does.

It is pure logic, with no audio device, network or thread of its own: the session feeds
it, and acts on what it says.
"""

import math
import re
from typing import List, Optional

import numpy as np

from voice_assistant.recording import FRAME, RATE

# How long, in seconds, a voice has to go on for the reply to dip.
DIP_AFTER = 0.15

# How long, in seconds, after the voice ends, with no real words, before the reply comes
# back to full volume.
CUT_IN_WAIT = 1.0

# Said while Nova talks, to show he's listening rather than to take over, in the form
# _tidy gives them - hyphens are spaces, so "mm-hm" and "uh-huh" are here as two words.
LISTENING_NOISES = frozenset({"yeah", "yes", "yep", "mm", "mhm", "mm hm", "uh huh", "okay", "ok", "right",
                              "sure", "i see", "got it"})

# The word which always stops a reply, even when the reply itself says it.
STOP_WORD = "stop"


def _tidy(text: str) -> List[str]:
    """
    Args:
        text: Words as speech-to-text or a reply writes them.

    Returns:
        The words, lower-cased and without punctuation, hyphens counting as spaces. An
        apostrophe inside a word, as in "it's", stays, and a curly one is a straight one.
    """
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    return [word for word in (found.strip("'") for found in re.findall(r"[\w']+", text.lower())) if word]


def _is_own(heard: List[str], saying: List[str]) -> bool:
    """
    Args:
        heard: The words heard, tidied.
        saying: The words of the reply, tidied.

    Returns:
        Whether own_words says the words heard are the reply's own.
    """
    length = len(heard)
    if not length:
        return False
    allowed = 0 if length <= 2 else length // 4
    for start in range(len(saying) - length + 1):
        wrong = 0
        for position, (said, was) in enumerate(zip(saying[start:start + length], heard)):
            if was == said:
                continue
            # Speech-to-text hasn't finished the last word it has so far, and a run may begin
            # part way through a word, so those two edges match a piece of the reply's word
            # without counting as wrong.
            if len(was) >= 2 and ((position == length - 1 and said.startswith(was))
                                  or (position == 0 and said.endswith(was))):
                continue
            wrong += 1
        if wrong <= allowed:
            return True
    return False


def own_words(heard: str, saying: str) -> bool:
    """
    The allowance is for speech-to-text mishearing a word in a longer run. It doesn't match
    a number heard as digits against the reply's spoken one ("12" against "twelve"), nor a
    contraction written another way: those change which words there are, and so aren't
    matched.

    Args:
        heard: The words speech-to-text heard.
        saying: The text of the reply being said.

    Returns:
        Whether the words heard are a run of consecutive words of the reply, with up to
        one wrong in every four of them (a substitution at its position) - none when
        there are one or two. The last word heard also matches if it is the start of the
        reply's word there, and the first if it is the end of it (each at least two
        letters), as speech-to-text hasn't finished the last word, and a run may begin
        part way through one; those aren't counted as wrong. False if nothing was heard.
    """
    return _is_own(_tidy(heard), _tidy(saying))


def only_listening_noises(heard: str) -> bool:
    """
    Args:
        heard: The words speech-to-text heard.

    Returns:
        Whether all of it can be read as one or more of LISTENING_NOISES in a row. False
        if nothing was heard.
    """
    words = _tidy(heard)
    if not words:
        return False
    # reads[n] is whether the first n words can be read as noises in a row.
    reads = [True] + [False] * len(words)
    for end in range(1, len(words) + 1):
        reads[end] = any(reads[start] and " ".join(words[start:end]) in LISTENING_NOISES
                         for start in range(max(0, end - 2), end))
    return reads[len(words)]


class CutIn:
    """What a voice heard over a reply means, from the frames and words it's given."""

    def __init__(self, detector, saying: str, threshold: Optional[float] = None, dip_after: float = DIP_AFTER,
                 wait: float = CUT_IN_WAIT):
        """
        Args:
            detector: Says how likely each frame is to be speech - see vad.py. It has to
                be one made for this, not the one recordings use, as it's reset here and
                fed a different stream.
            saying: The text of the reply being said.
            threshold: How likely a frame has to be for speech, or the detector's own if
                None.
            dip_after: Seconds of speech which dip the reply.
            wait: Seconds without speech after a dip, and without real words, which bring
                the reply back.
        """
        self.detector = detector
        self.saying = saying
        # Tidied once here, not for every frame.
        self._saying_words = _tidy(saying)
        self.threshold = detector.threshold if threshold is None else threshold
        # Counted in frames, with a little allowance so a whole number of frames which is
        # exactly the time asked for isn't pushed up by floating point.
        self._dip_frames = max(1, math.ceil(dip_after * RATE / FRAME - 1e-9))
        self._wait_frames = max(1, math.ceil(wait * RATE / FRAME - 1e-9))
        detector.reset()
        # Frames since the start, the first being 0.
        self._frames = 0
        # The frame at which the voice that dipped the reply began, or None before a dip.
        self.voice_from: Optional[int] = None
        self._dipped = False
        self._stopped = False
        self._speech_run = 0
        self._quiet_run = 0

    def hear(self, frame: np.ndarray, words: str) -> Optional[str]:
        """
        Args:
            frame: The next recording.FRAME samples of what the microphone hears.
            words: The words speech-to-text has so far for the audio since the voice
                began, or "" for none yet. It's judged on its own each time, whether or
                not it has changed: the same words may be given many times as they grow.

        Returns:
            "dip" once a voice has gone on for dip_after; then "stop" once, as soon as
            the words hold "stop" or are neither the reply's own nor only listening
            noises, after which None always; or "undip" once, when wait has passed since
            the voice ended with no such words, after which a later voice can dip it
            again. None otherwise.
        """
        index = self._frames
        self._frames += 1
        if self._stopped:
            return None
        speech = self.detector.speech_probability(frame) >= self.threshold

        if not self._dipped:
            self._speech_run = self._speech_run + 1 if speech else 0
            if self._speech_run < self._dip_frames:
                return None
            self._dipped = True
            self._quiet_run = 0
            self.voice_from = index - self._speech_run + 1
            return "dip"

        if self.would_stop(words):
            self._stopped = True
            return "stop"
        # Speech which goes on without real words, a long "mmmm" or Nova's own echo still
        # reading as speech, keeps the reply dipped.
        self._quiet_run = 0 if speech else self._quiet_run + 1
        if self._quiet_run < self._wait_frames:
            return None
        self._dipped = False
        self.voice_from = None
        self._speech_run = 0
        return "undip"

    def would_stop(self, words: str) -> bool:
        """
        It has no effect on what hear does, and can be asked at any time, such as of the
        final words of a transcription.

        Args:
            words: The words heard since the voice began.

        Returns:
            Whether they would stop the reply: they hold "stop", or are neither the
            reply's own nor only listening noises. False for nothing.
        """
        tidied = _tidy(words)
        if not tidied:
            return False
        if STOP_WORD in tidied:
            return True
        return not _is_own(tidied, self._saying_words) and not only_listening_noises(words)
