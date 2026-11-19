"""Tests for how a satellite is named in words: what matches "my Pi", and what it's called in a reply."""

import pytest

from voice_assistant import satellite_names
from voice_assistant.machine import MACHINES
from voice_assistant.satellite_names import listed, matching, plain, spoken


@pytest.mark.parametrize("said", ["pc", "PC", "The PC", "my pc", "the PC satellite", "My PC Satellite", "p.c.", "the p.c.",
                                  "this PC", "That pc", "this PC satellite"])
def test_plain_drops_articles_and_satellite(said):
    assert plain(said) == "pc"


def test_plain_reads_a_name_as_words():
    assert plain("home-assistant") == "home assistant"
    assert plain("Home_Assistant") == "home assistant"
    assert plain("  the   Living  Room!  ") == "living room"


def test_plain_keeps_a_last_word_that_is_only_an_article():
    # Nothing is left to name otherwise, so a satellite called "the" or "satellite" can still be said.
    assert plain("the") == "the" and plain("this") == "this" and plain("that") == "that"
    assert plain("my satellite") == "satellite"
    assert plain("") == "" and plain("the ...") == "the"


@pytest.mark.parametrize("said", ["here", "Here", "this device", "this one", "that one", "this speaker",
                                  "this satellite"])
def test_the_words_for_the_satellite_spoken_to(said):
    assert satellite_names.means_here(said)


@pytest.mark.parametrize("said", ["the PC", "this PC", "my Pi", "there", "the bedroom", "", "this", "desk"])
def test_other_words_arent_for_the_satellite_spoken_to(said):
    assert not satellite_names.means_here(said)


def test_machine_words_cover_every_machine_and_are_plain():
    assert set(satellite_names.MACHINE_WORDS) == set(MACHINES)
    assert satellite_names.MACHINE_WORDS["linux"] == ()
    assert satellite_names.MACHINE_WORDS["pi"] == ("pi", "pie", "raspberry pi", "raspberry pie")
    assert satellite_names.MACHINE_WORDS["pc"] == ("pc", "computer", "desktop")
    assert satellite_names.MACHINE_WORDS["mac"] == ("mac", "macbook", "laptop", "computer")
    for words in satellite_names.MACHINE_WORDS.values():
        assert all(word == plain(word) for word in words)


def test_machine_words_find_it():
    satellites = {"home-assistant": ("pi", []), "pc": ("pc", [])}
    assert matching("my pi", satellites) == ["home-assistant"]
    assert matching("pie", satellites) == ["home-assistant"]
    assert matching("the Raspberry Pi", satellites) == ["home-assistant"]
    assert matching("the computer", satellites) == ["pc"]
    assert matching("my desktop satellite", satellites) == ["pc"]


def test_its_own_name_finds_it():
    satellites = {"home-assistant": ("pi", []), "pc": ("pc", []), "study": ("linux", [])}
    assert matching("home assistant", satellites) == ["home-assistant"]
    assert matching("The Home-Assistant satellite", satellites) == ["home-assistant"]
    # A machine that has no words of its own is still found by its name.
    assert matching("study", satellites) == ["study"]
    # And one which isn't known to be anything.
    assert matching("kitchen", {"kitchen": (None, [])}) == ["kitchen"]


def test_a_page_name_finds_it():
    satellites = {"home-assistant": ("pi", ["Desk", "the pi in the study"]), "pc": ("pc", [])}
    assert matching("my desk", satellites) == ["home-assistant"]
    assert matching("the Pi in the study", satellites) == ["home-assistant"]


def test_page_names_win_over_machine_words():
    satellites = {"home-assistant": ("pi", []), "pc": ("pc", ["computer"]), "mac": ("mac", [])}
    # The PC is called "computer" on the page, so it means that one alone and not the Mac too.
    assert matching("computer", satellites) == ["pc"]
    # Its other machine words still work for the Mac.
    assert matching("laptop", satellites) == ["mac"]


def test_a_satellites_own_name_wins_over_machine_words():
    # One named "computer" is that one alone, though the PC is a computer too.
    satellites = {"computer": ("linux", []), "pc": ("pc", [])}
    assert matching("computer", satellites) == ["computer"]


def test_two_matches_are_both_given():
    satellites = {"home-assistant": ("pi", []), "pc": ("pc", []), "mac": ("mac", [])}
    assert matching("computer", satellites) == ["pc", "mac"]
    # In the order the satellites were given, whatever order they match in.
    assert matching("computer", {"mac": ("mac", []), "pc": ("pc", [])}) == ["mac", "pc"]


def test_a_name_two_satellites_share_gives_both():
    satellites = {"pc": ("pc", ["desk"]), "mac": ("mac", ["Desk"])}
    assert matching("the desk", satellites) == ["pc", "mac"]


def test_nothing_matches():
    satellites = {"home-assistant": ("pi", []), "pc": ("pc", [])}
    assert matching("phone", satellites) == []
    assert matching("", satellites) == [] and matching("the", satellites) == []
    assert matching("pi", {}) == []
    # A machine word only finds the machine it belongs to.
    assert matching("mac", satellites) == []


def test_a_blank_page_name_matches_nothing():
    assert matching("", {"pc": ("pc", [""])}) == []
    assert matching("the", {"pc": ("pc", ["..."])}) == []


def test_spoken():
    assert spoken("home-assistant", "pi", []) == "the Pi"
    assert spoken("pc", "pc", []) == "the PC"
    assert spoken("mac", "mac", []) == "the Mac"
    # A name set on the page is used before the machine word, as it's what he calls it.
    assert spoken("pc", "pc", ["Desk"]) == "Desk"
    assert spoken("pc", "pc", ["Desk", "the computer"]) == "Desk"
    # A machine with no word to say, or an unknown one, is called by its own name.
    assert spoken("study", "linux", []) == "study"
    assert spoken("kitchen", None, []) == "kitchen"
    assert spoken("study", "linux", ["Workshop"]) == "Workshop"


def test_spoken_skips_a_name_which_is_one_of_the_machines_own_words():
    # "computer" on the PC settles what "the computer" means, but it's no name to call the PC by in a reply.
    assert spoken("pc", "pc", ["computer"]) == "the PC"
    assert spoken("pc", "pc", ["The Computer"]) == "the PC"
    assert spoken("home-assistant", "pi", ["Pie"]) == "the Pi"
    # The first which isn't one is used, wherever it comes.
    assert spoken("pc", "pc", ["computer", "Desk", "Study"]) == "Desk"
    assert spoken("pc", "pc", ["my PC", "desktop", "Desk"]) == "Desk"
    # A word for another kind of machine, or any name where the machine isn't known, is a name like any.
    assert spoken("pc", "pc", ["laptop"]) == "laptop"
    assert spoken("study", "linux", ["computer"]) == "computer"
    assert spoken("kitchen", None, ["computer"]) == "computer"


def test_listed():
    assert listed([], "and") == ""
    assert listed(["the PC"], "or") == "the PC"
    assert listed(["the PC", "the Mac"], "or") == "the PC or the Mac"
    assert listed(["the PC", "the Mac", "Desk"], "or") == "the PC, the Mac or Desk"
    assert listed(["the Pi", "the PC", "Desk", "kitchen"], "and") == "the Pi, the PC, Desk and kitchen"
