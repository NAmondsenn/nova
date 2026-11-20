"""Tests for matching what's said to a computer's apps and windows, and keeping its lists."""

from voice_assistant.computers import Computers, match_app, match_window, plain


def apps(*names):
    return [{"id": name, "name": name, "kind": "exe"} for name in names]


def test_plain_drops_marks():
    assert plain("EA SPORTS FC™ 25") == "ea sports fc 25"
    assert plain("Rocket League®") == "rocket league"
    assert plain("  Spider-Man:  Remastered ") == "spider man remastered"


def test_exact_beats_partial():
    found = match_app("overwatch", apps("Overwatch 2", "Overwatch"), {})
    assert found.item["name"] == "Overwatch" and found.choices == []


def test_filler_words_do_not_defeat_an_exact_match():
    found = match_app("the overwatch game", apps("Overwatch 2", "Overwatch"), {})
    assert found.item["name"] == "Overwatch" and found.choices == []
    assert match_window("the steam app", ["Steam VR", "Steam"]) == "Steam"
    # A name with a filler word in it still matches as said in full.
    found = match_app("the game room", apps("The Game Room", "Game Room Deluxe"), {})
    assert found.item["name"] == "The Game Room"


def test_every_word_one_match():
    found = match_app("rocket league", apps("Rocket League®", "Steam"), {})
    assert found.item["name"] == "Rocket League®"
    # Filler words are ignored.
    assert match_app("the steam app", apps("Steam"), {}).item["name"] == "Steam"


def test_ambiguous_gives_choices():
    found = match_app("fortnite", apps("Fortnite Festival", "Fortnite Creative"), {})
    assert found.item is None and found.choices == ["Fortnite Creative", "Fortnite Festival"]


def test_alias():
    items = apps("Grand Theft Auto V Enhanced", "Steam")
    found = match_app("GTA", items, {"gta": "Grand Theft Auto V Enhanced"})
    assert found.item["name"] == "Grand Theft Auto V Enhanced"


def test_no_match():
    found = match_app("calculator", apps("Steam"), {})
    assert found.item is None and found.choices == []
    assert match_app("the game", apps("Steam"), {}).item is None
    assert match_window("discord", ["Steam"]) is None


def test_window_match():
    assert match_window("discord", ["Firefox", "Discord | #general"]) == "Discord | #general"
    assert match_window("fire", ["Firefox"]) is None
    assert match_window("firefox", ["Firefox", "Firefox Private"]) == "Firefox"


def test_apps_kept_windows_dropped_on_gone():
    computers = Computers()
    assert computers.known() == []
    computers.set_apps("pc", apps("Steam"))
    computers.set_windows("pc", ["Steam"])
    assert computers.known() == ["pc"]
    computers.gone("pc")
    assert computers.apps("pc") == apps("Steam") and computers.windows("pc") == []
    assert computers.apps("mac") == [] and computers.known() == ["pc"]
