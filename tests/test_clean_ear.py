"""Tests for the rules which judge a song clean or explicit from how many of its lyric swears are heard (decision 0019)."""

import logging

import pytest

from voice_assistant import clean_ear
from voice_assistant.clean_ear import Thresholds, Verdict, count_swears, judge

T = Thresholds()
LYRICS_6 = "fuck shit bitch fuck shit bitch"


def _lyrics(expected):
    return " ".join(["fuck"] * expected)


def _heard(heard):
    # What the model gave back: some of the words, among others it heard right.
    return " ".join(["fuck"] * heard + ["and"])


def test_swears_are_counted_by_word_family():
    assert count_swears("Oh my God, what the fuck? Why these niggas") == 2
    assert count_swears("Pass the class") == 0
    # "asshole" isn't "ass": the word boundary stops the short word matching inside the longer one.
    assert count_swears("hoes and a hoe, asshole") == 2
    assert count_swears("FUCKING Shit, motherfucker") == 3
    assert count_swears("") == 0


@pytest.mark.parametrize("heard, kind", [(3, "explicit"), (2, "unsure"), (1, "unsure"), (0, "clean")])
def test_a_song_is_judged_by_the_share_of_its_swears_heard(heard, kind):
    transcript = " ".join(["fuck"] * heard)
    verdict = judge(LYRICS_6, transcript, T)
    assert verdict == Verdict(kind, 6, heard)


def test_exactly_at_a_threshold_counts_for_that_side():
    assert judge(_lyrics(20), _heard(7), T).kind == "explicit"   # 35%
    assert judge(_lyrics(20), _heard(3), T).kind == "clean"      # 15%
    assert judge(_lyrics(20), _heard(6), T).kind == "unsure"
    assert judge(_lyrics(20), _heard(4), T).kind == "unsure"


@pytest.mark.parametrize("expected, heard, kind", [(16, 1, "clean"), (14, 7, "explicit"), (72, 49, "explicit")])
def test_the_spikes_songs_are_judged_as_it_found(expected, heard, kind):
    # Endless, fukumean and Over Hoes & Bitches, by the counts the test on the brain gave.
    assert judge(_lyrics(expected), _heard(heard), T) == Verdict(kind, expected, heard)


def test_heard_is_not_capped_at_the_lyrics_count():
    assert judge(_lyrics(4), _heard(9), T) == Verdict("explicit", 4, 9)


def test_too_few_swears_in_the_lyrics_is_not_judged():
    verdict = judge("fuck shit", "", T)
    assert verdict.kind == "too_few" and verdict.expected == 2
    # Nothing to censor, so there is no need to have heard the song.
    assert judge("fuck shit", None, T).kind == "too_few"


def test_no_lyrics_or_nothing_heard_is_not_judged():
    assert judge(None, "x", T) == Verdict("not_judged", 0, 0, "no lyrics")
    assert judge(LYRICS_6, None, T) == Verdict("not_judged", 6, 0, "not heard")


def test_the_thresholds_are_the_settings_or_the_defaults():
    assert Thresholds() == Thresholds(3, 0.35, 0.15)
    assert Thresholds.from_config(_Config({})) == Thresholds()
    own = {"lyric_minimum": 5, "explicit_share": 0.5, "clean_share": 0.1}
    assert Thresholds.from_config(_Config(own)) == Thresholds(5, 0.5, 0.1)
    # A setting left out takes its default and not the others'.
    assert Thresholds.from_config(_Config({"lyric_minimum": 4})) == Thresholds(4, 0.35, 0.15)


def test_a_threshold_left_blank_takes_its_default():
    # "clean_share:" with nothing after it reads as None from config.yaml.
    blank = {"lyric_minimum": None, "explicit_share": None, "clean_share": None}
    assert Thresholds.from_config(_Config(blank)) == Thresholds()


@pytest.mark.parametrize("shares", [(0.2, 0.3), (0.3, 0.3)])
def test_shares_the_wrong_way_round_take_both_defaults_with_a_warning(shares, caplog, monkeypatch):
    monkeypatch.setattr(clean_ear, "_warned_of", None)
    explicit, clean = shares
    with caplog.at_level(logging.WARNING):
        thresholds = Thresholds.from_config(_Config({"lyric_minimum": 4, "explicit_share": explicit,
                                                     "clean_share": clean}))

    assert thresholds == Thresholds(4, 0.35, 0.15)
    assert "clean_share" in caplog.text
    # Read for every song, so said once, not each time.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        Thresholds.from_config(_Config({"explicit_share": explicit, "clean_share": clean}))
    assert caplog.text == ""


def test_the_module_names_its_section_of_the_decision():
    assert "section 1 of decision 0019" in clean_ear.__doc__


def test_the_thresholds_are_those_the_judging_uses():
    strict = Thresholds(minimum=2, explicit_share=0.5, clean_share=0.0)
    assert judge("fuck shit", "fuck", strict).kind == "explicit"
    assert judge("fuck shit", "", strict).kind == "clean"


class _Config:
    def __init__(self, section):
        self._section = section

    def section(self, name):
        return self._section if name == "clean_check" else {}
