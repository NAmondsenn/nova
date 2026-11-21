"""
Tests for cut_in.py: what a voice heard over a reply means - when it dips the reply, when
its words stop it, and when it comes back. The speech detector is the recorder's fake, so
nothing here needs a model.
"""

import numpy as np
import pytest
from test_recording import FakeDetector

from voice_assistant.cut_in import CUT_IN_WAIT, DIP_AFTER, LISTENING_NOISES, CutIn, only_listening_noises, own_words
from voice_assistant.recording import FRAME, RATE

SAYING = "The weather today is dry and bright"

# A frame is 30 ms, so these are the frames in DIP_AFTER and in CUT_IN_WAIT.
DIP_FRAMES = round(DIP_AFTER * RATE / FRAME)
WAIT_FRAMES = 34


def loud(level=5000):
    return np.full(FRAME, level, dtype=np.int16)


def quiet():
    return np.zeros(FRAME, dtype=np.int16)


class GradedDetector(FakeDetector):
    """Gives a frame the probability of its loudness out of 10000, so a test can set how sure it is."""

    def speech_probability(self, frame):
        return float(np.abs(frame).mean()) / 10000


def feed(cut_in, frames, words=""):
    """Gives the frames one at a time with the same words, and returns what each said."""
    return [cut_in.hear(frame, words) for frame in frames]


def dipped(saying=SAYING, **kwargs):
    """A CutIn which has just been dipped by a voice."""
    cut_in = CutIn(FakeDetector(), saying, **kwargs)
    assert feed(cut_in, [loud()] * DIP_FRAMES)[-1] == "dip"
    return cut_in


def test_voice_dips_after_dip_after():
    cut_in = CutIn(FakeDetector(), SAYING)
    said = feed(cut_in, [loud()] * DIP_FRAMES)
    assert said == [None] * (DIP_FRAMES - 1) + ["dip"]
    # Once only, however long the voice goes on.
    assert feed(cut_in, [loud()] * 10) == [None] * 10


def test_a_single_loud_frame_doesnt_dip_it():
    cut_in = CutIn(FakeDetector(), SAYING)
    # Short runs, each broken by a quiet frame, never add up.
    frames = ([loud()] * (DIP_FRAMES - 1) + [quiet()]) * 3 + [loud(), quiet()]
    assert feed(cut_in, frames) == [None] * len(frames)


def test_the_voice_begins_at_the_first_frame_of_the_run_which_dipped_it():
    cut_in = CutIn(FakeDetector(), SAYING)
    assert cut_in.voice_from is None
    # A run too short to count, then quiet, then the voice from frame 7.
    feed(cut_in, [loud(), loud(), quiet(), quiet(), quiet(), quiet(), quiet()])
    assert feed(cut_in, [loud()] * DIP_FRAMES)[-1] == "dip"
    assert cut_in.voice_from == 7


def test_the_detector_is_reset_at_the_start():
    class Counting(FakeDetector):
        resets = 0

        def reset(self):
            self.resets += 1

    detector = Counting()
    CutIn(detector, SAYING)
    assert detector.resets == 1


def test_words_after_a_dip_stop_it():
    cut_in = dipped()
    assert cut_in.hear(loud(), "") is None
    assert cut_in.hear(loud(), "turn the lights off") == "stop"
    # Once only, and nothing after it - not even an undip.
    assert feed(cut_in, [loud()] * 3, "turn the lights off now") == [None] * 3
    assert feed(cut_in, [quiet()] * (WAIT_FRAMES + 5), "turn the lights off now") == [None] * (WAIT_FRAMES + 5)


def test_words_before_a_dip_dont_stop_it():
    cut_in = CutIn(FakeDetector(), SAYING)
    assert cut_in.hear(quiet(), "turn the lights off") is None


def test_no_words_undips_after_the_wait():
    cut_in = dipped()
    said = feed(cut_in, [quiet()] * WAIT_FRAMES)
    assert said == [None] * (WAIT_FRAMES - 1) + ["undip"]
    assert cut_in.voice_from is None
    assert WAIT_FRAMES * FRAME / RATE >= CUT_IN_WAIT > (WAIT_FRAMES - 1) * FRAME / RATE
    assert feed(cut_in, [quiet()] * 5) == [None] * 5


def test_speech_without_words_keeps_it_dipped():
    cut_in = dipped()
    # A long "mmmm", or the reply's own echo reading as speech: no undip while it goes on.
    assert feed(cut_in, [loud()] * (WAIT_FRAMES * 3)) == [None] * (WAIT_FRAMES * 3)
    # Then the wait runs from the last speech frame.
    assert feed(cut_in, [quiet()] * WAIT_FRAMES)[-1] == "undip"


def test_a_pause_in_the_voice_starts_the_wait_again():
    cut_in = dipped()
    assert feed(cut_in, [quiet()] * (WAIT_FRAMES - 1)) == [None] * (WAIT_FRAMES - 1)
    cut_in.hear(loud(), "")
    assert feed(cut_in, [quiet()] * (WAIT_FRAMES - 1)) == [None] * (WAIT_FRAMES - 1)
    assert cut_in.hear(quiet(), "") == "undip"


def test_it_can_dip_again_after_an_undip():
    cut_in = dipped()
    feed(cut_in, [quiet()] * WAIT_FRAMES, "")
    assert cut_in.voice_from is None
    frames_so_far = DIP_FRAMES + WAIT_FRAMES
    assert feed(cut_in, [loud()] * DIP_FRAMES)[-1] == "dip"
    assert cut_in.voice_from == frames_so_far
    # Its words are judged afresh: the first voice's words don't carry over.
    assert cut_in.hear(loud(), "") is None
    assert cut_in.hear(loud(), "what time is it") == "stop"


def test_the_replys_own_words_dont_stop_it():
    cut_in = dipped()
    assert feed(cut_in, [loud()] * 20, "weather today is dry") == [None] * 20
    assert feed(cut_in, [quiet()] * WAIT_FRAMES, "weather today is dry")[-1] == "undip"


def test_the_replys_own_words_never_stop_it_however_long_they_go_on():
    cut_in = dipped()
    words = ""
    for word in SAYING.split():
        words = f"{words} {word}".strip()
        assert cut_in.hear(loud(), words) is None
    # Same words again, many times, as speech-to-text holds them.
    assert feed(cut_in, [loud()] * 30, words) == [None] * 30


def test_more_words_which_make_the_whole_neither_stop_it():
    cut_in = dipped()
    assert cut_in.hear(loud(), "weather today is dry") is None
    assert cut_in.hear(loud(), "weather today is dry but what about tomorrow") == "stop"


def test_stop_always_stops_it():
    assert dipped().hear(loud(), "Stop!") == "stop"
    assert dipped().hear(loud(), "please stop") == "stop"
    # Even when the reply says it itself.
    assert dipped("I'll stop the music when you like").hear(loud(), "stop the music") == "stop"
    assert dipped("I'll stop the music when you like").hear(loud(), "stop") == "stop"
    # Even in the middle of the reply's own words.
    assert dipped("A bus stop is near").hear(loud(), "bus stop") == "stop"


def test_listening_noises_dont_stop_it():
    cut_in = dipped()
    assert cut_in.hear(loud(), "yeah") is None
    assert cut_in.hear(loud(), "Yeah, mm-hm okay") is None
    assert feed(cut_in, [quiet()] * WAIT_FRAMES, "yeah mm-hm okay")[-1] == "undip"


def test_a_noise_then_more_stops_it():
    cut_in = dipped()
    assert cut_in.hear(loud(), "okay") is None
    assert cut_in.hear(loud(), "okay but what about") == "stop"


def test_a_raised_threshold_is_used():
    # 0.6 a frame: speech by the detector's own 0.5, not by 0.7.
    frames = [loud(6000)] * DIP_FRAMES
    assert feed(CutIn(GradedDetector(), SAYING), frames)[-1] == "dip"
    assert feed(CutIn(GradedDetector(), SAYING, threshold=0.7), frames) == [None] * DIP_FRAMES
    assert feed(CutIn(GradedDetector(), SAYING, threshold=0.7), [loud(8000)] * DIP_FRAMES)[-1] == "dip"


def test_the_times_can_be_set():
    cut_in = CutIn(FakeDetector(), SAYING, dip_after=0.3, wait=0.3)
    assert feed(cut_in, [loud()] * 10)[-1] == "dip"
    assert feed(cut_in, [quiet()] * 10)[-1] == "undip"


# own_words

def test_words_are_the_replys_own_when_they_are_a_run_of_what_it_said():
    said = "The weather today is dry, and bright!"
    assert own_words("weather today is dry", said)
    assert own_words("Dry and bright.", said)
    assert own_words("the weather today is dry and bright", said)
    assert not own_words("weather is dry", said)
    assert not own_words("stop the music", said)
    assert not own_words("the weather today is dry and bright and warm", said)
    assert not own_words("", said)
    assert not own_words("weather", "")


def test_an_apostrophe_stays_in_a_word():
    assert own_words("that's all", "Well that's all I know.")
    assert not own_words("thats all", "Well that's all I know.")


def test_own_words_allows_one_wrong_in_four():
    said = "The forecast for the weekend is mostly dry with some bright spells"
    # Four words allow one wrong, but not two.
    assert own_words("forecast for a weekend", said)
    assert not own_words("forecast of a weekend", said)
    # Seven allow one, still, and eight allow two.
    assert own_words("forecast for the weekend was mostly dry", said)
    assert not own_words("forecast for the weekend was mostly wet", said)
    assert own_words("the weekend was mostly dry with sum bright", said)
    assert not own_words("the weekend was mostly wet with sum bright", said)


def test_two_words_must_match_exactly():
    said = "The weather today is dry"
    assert own_words("weather today", said)
    assert not own_words("weather tonight", said)
    assert not own_words("whether today", said)
    # Three words still allow none wrong.
    assert own_words("weather today is", said)
    assert not own_words("weather tonight is", said)


def test_a_wrong_word_is_a_substitution_not_a_gap():
    # A missing word shifts everything after it, so it counts as more than one wrong.
    assert not own_words("the weather is dry and", "the weather today is dry and bright")
    assert own_words("the weather tomorrow is dry", "the weather today is dry and bright")


REPLY = "The temperature today is twelve degrees, with a light breeze from the north."


def test_an_unfinished_last_word_is_still_the_replys_own():
    assert own_words("the temperature tod", REPLY)
    assert own_words("the temperature today is twelve deg", REPLY)
    # The same, as the whole of what a cut-in hears.
    assert dipped(REPLY).hear(loud(), "the temperature tod") is None


def test_a_run_starting_part_way_through_a_word_is_still_the_replys_own():
    assert own_words("rature today is", REPLY)
    assert own_words("perature today is twelve", REPLY)


def test_a_single_fragment_is_the_replys_own_from_either_end():
    assert own_words("tod", REPLY)
    assert own_words("rature", REPLY)
    assert not own_words("t", REPLY)
    assert not own_words("e", REPLY)


def test_a_fragment_must_be_at_the_reply_words_own_end():
    # "ratu" is neither the start nor the end of "temperature", and a start isn't wanted at the front.
    assert not own_words("ratu today is", REPLY)
    assert not own_words("temp today is", REPLY)
    # And an end isn't wanted at the back.
    assert not own_words("the temperature ay", REPLY)


def test_a_different_whole_word_is_still_not_the_replys_own():
    assert not own_words("the weather", REPLY)
    assert not own_words("the temperature tomorrow", REPLY)


def test_a_fragment_in_the_middle_of_a_run_is_a_wrong_word():
    # Four words allow one wrong: a piece of a word in the middle is that one, not a free match.
    assert own_words("the temp today is", REPLY)
    assert not own_words("the temp tod is", REPLY)
    # A word cut short at the end doesn't use the allowance up, so one in the middle can have it.
    assert own_words("the temperature tod is twelve deg", REPLY)


def test_a_curly_apostrophe_is_a_straight_one():
    assert own_words("it's twelve", "It\u2019s twelve")
    assert own_words("it\u2019s twelve", "It's twelve")
    assert own_words("it\u2018s twelve", "It's twelve")


# would_stop

def test_would_stop_is_what_hear_judges_words_by():
    cut_in = CutIn(FakeDetector(), SAYING)
    # Before any dip, and without any effect on it.
    assert cut_in.would_stop("turn the lights off")
    assert cut_in.would_stop("stop")
    assert not cut_in.would_stop("")
    assert not cut_in.would_stop("weather today is dry")
    assert not cut_in.would_stop("Yeah, mm-hm")
    assert not cut_in.would_stop("weather tod")
    assert cut_in.would_stop("okay but what about")
    assert cut_in.voice_from is None
    assert feed(cut_in, [loud()] * DIP_FRAMES)[-1] == "dip"


def test_would_stop_holds_stop_even_when_the_reply_says_it():
    assert CutIn(FakeDetector(), "I'll stop the music when you like").would_stop("stop the music")


def test_would_stop_after_a_stop_leaves_it_stopped_and_judges_on():
    cut_in = dipped()
    assert cut_in.hear(loud(), "what time is it") == "stop"
    assert not cut_in.would_stop("weather today is dry")
    assert cut_in.hear(loud(), "weather today is dry") is None


def test_the_replys_text_is_tidied_once():
    cut_in = CutIn(FakeDetector(), "The weather, TODAY!")
    assert cut_in._saying_words == ["the", "weather", "today"]


# only_listening_noises

def test_listening_noises_are_read_with_hyphens_as_spaces():
    for heard in ("yeah", "Yes.", "yep", "mm", "mhm", "mm-hm", "mm hm", "uh-huh", "uh huh", "okay", "OK", "right",
                  "sure", "I see", "got it"):
        assert only_listening_noises(heard), heard
    assert LISTENING_NOISES == {"yeah", "yes", "yep", "mm", "mhm", "mm hm", "uh huh", "okay", "ok", "right",
                                "sure", "i see", "got it"}


def test_a_run_of_listening_noises_is_one_too():
    assert only_listening_noises("mm-hm okay")
    assert only_listening_noises("Yeah, yeah, I see. Got it!")
    assert only_listening_noises("mm hm mm hm")


@pytest.mark.parametrize("heard", ["", "  ", "hm", "huh", "yeah but", "okay what about", "i", "see", "stop", "got"])
def test_anything_else_is_not_only_listening_noises(heard):
    assert not only_listening_noises(heard)
