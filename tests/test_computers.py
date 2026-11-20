"""Tests for matching what's said to a computer's apps and windows, and keeping its lists."""

import logging
from types import SimpleNamespace

from voice_assistant.computers import (ComputerHooks, Computers, ComputerControl, match_app, match_window, plain, safe_url,
                                       search_url)


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


# Web addresses and searches

def test_safe_url():
    assert safe_url("bbc.co.uk") == "https://bbc.co.uk"
    assert safe_url("youtube.com/watch?v=abc") == "https://youtube.com/watch?v=abc"
    assert safe_url("https://www.bbc.co.uk/news") == "https://www.bbc.co.uk/news"
    long = "https://example.com/" + "a" * 1981
    assert len(long) == 2001 and safe_url(long) is None
    assert safe_url(long[:-1]) == long[:-1]
    for refused in ("http://x", "file:///c", "https://u:p@x", "https://u@x.com", "javascript:", "javascript:alert(1)",
                    "steam://rungameid/1", "https://exa mple.com", "https://example.com/\x07", "bbc co uk", "", "https://",
                    "C:\\Windows\\notepad.exe", "../../x"):
        assert safe_url(refused) is None, refused


def test_search_url_each_site():
    assert search_url("google", "rocket league ranks") == "https://www.google.com/search?q=rocket+league+ranks"
    assert search_url("duckduckgo", "a&b") == "https://duckduckgo.com/?q=a%26b"
    assert search_url("bing", "café?") == "https://www.bing.com/search?q=caf%C3%A9%3F"
    # A site which isn't one of the three is Google's, the default.
    assert search_url("altavista", "x") == "https://www.google.com/search?q=x"


# Doing things on a computer

class FakeComputer:
    """A computer satellite which keeps what it was asked, and answers as it's told."""

    def __init__(self, answer=None):
        self.asked = []
        self.answer = {"said": "Done."} if answer is None else answer

    def ask_computer(self, action, **fields):
        self.asked.append((action, fields))
        return self.answer


class FakeBrain:
    """The parts of the brain ComputerControl asks: which computer, and its apps, windows and names."""

    def __init__(self, target="pc", problem=None, search_site="google", meant=None):
        self.computers = Computers()
        self.computer = FakeComputer()
        self.target, self.problem = target, problem
        # The computer meant, connected or not - the one targeted, unless one asleep is given.
        self.meant = meant or target
        self.aliases = {}
        self.wheres = []
        self.rooms = SimpleNamespace(app_names=lambda name: dict(self.aliases), opens_things=lambda name: True)
        self.config = SimpleNamespace(section=lambda name: {"search_site": search_site} if name == "computer" else {})

    def computer_for(self, spoken_at, where):
        self.wheres.append((spoken_at, where))
        return (self.target, None) if self.target else (None, self.problem)

    def computer_meant(self, spoken_at, where):
        return self.meant, None

    def spoken_name(self, name):
        return {"pc": "Nathaniel's PC", "mac": "the Mac"}.get(name, name)

    def session_for(self, name):
        return SimpleNamespace(satellite=self.computer) if name == self.target else None


def at(brain, spoken_at="pc"):
    return ComputerControl(brain, spoken_at)


def test_open_sends_id_never_target():
    brain = FakeBrain()
    brain.computers.set_apps("pc", [{"id": "a1b2c3", "name": "Fortnite", "kind": "epic"},
                                    {"id": "d4e5f6", "name": "Steam", "kind": "app"}])
    assert at(brain).open("fortnite") == {"success": True, "message": "Opening Fortnite."}
    # Only the app's id goes: the brain never has where it's launched from.
    assert brain.computer.asked == [("open", {"item": "a1b2c3"})]
    # From another satellite, it says where.
    assert at(brain, "bedroom").open("Steam", "my PC") == {"success": True, "message": "Opening Steam on Nathaniel's PC."}
    assert brain.wheres[-1] == ("bedroom", "my PC")
    # His own short name for one.
    brain.computers.set_apps("pc", [{"id": "g7", "name": "Grand Theft Auto V Enhanced", "kind": "steam"}])
    brain.aliases = {"gta": "Grand Theft Auto V Enhanced"}
    assert at(brain).open("GTA")["message"] == "Opening Grand Theft Auto V Enhanced."
    assert brain.computer.asked[-1] == ("open", {"item": "g7"})


def test_open_ambiguous_asks():
    brain = FakeBrain()
    brain.computers.set_apps("pc", [{"id": str(n), "name": name, "kind": "app"}
                                    for n, name in enumerate(["Fortnite Festival", "Fortnite Creative", "Steam"])])
    assert at(brain).open("fortnite") == {"success": False, "message": "Which one: Fortnite Creative or Fortnite Festival?"}
    assert brain.computer.asked == []


def test_open_unknown_gives_the_closest():
    brain = FakeBrain()
    brain.computers.set_apps("pc", [{"id": str(n), "name": name, "kind": "app"}
                                    for n, name in enumerate(["Rocket League", "Overwatch", "Notepad", "Steam"])])
    result = at(brain, "bedroom").open("rocket leeg")
    assert result == {"success": False, "message": "I can't find rocket leeg on Nathaniel's PC. Closest: Rocket League."}
    # Nothing near it: no closest at all.
    assert at(brain).open("zzz") == {"success": False, "message": "I can't find zzz on Nathaniel's PC."}
    assert brain.computer.asked == []


def test_where_problem_is_said_and_nowhere_says_so():
    brain = FakeBrain(target=None, problem="Your PC's off.")
    assert at(brain).open("Steam") == {"success": False, "message": "Your PC's off."}
    assert at(brain).search("x") == {"success": False, "message": "Your PC's off."}
    nowhere = FakeBrain(target=None)
    assert at(nowhere, "bedroom").open("Steam") == {"success": False,
                                                    "message": "There's no computer here to open things on."}


def test_satellite_answers():
    brain = FakeBrain()
    brain.computers.set_apps("pc", [{"id": "s", "name": "Steam", "kind": "app"}])
    brain.computer.answer = {"error": "That's not on this PC any more."}
    assert at(brain).open("steam") == {"success": False, "message": "That's not on this PC any more."}
    brain.computer.answer = None
    assert at(brain, "bedroom").open("steam") == {"success": False, "message": "I couldn't do that on Nathaniel's PC."}


def test_open_url():
    brain = FakeBrain()
    assert at(brain).open_url("bbc.co.uk") == {"success": True, "message": "Opening bbc.co.uk."}
    assert brain.computer.asked == [("open_url", {"url": "https://bbc.co.uk"})]
    assert at(brain).open_url("file:///c") == {"success": False, "message": "That isn't a web address I can open."}
    assert len(brain.computer.asked) == 1


def test_search_uses_the_site_setting():
    brain = FakeBrain(search_site="duckduckgo")
    assert at(brain).search("rocket league") == {"success": True, "message": "Searching for rocket league."}
    assert brain.computer.asked == [("open_url", {"url": "https://duckduckgo.com/?q=rocket+league"})]
    assert at(brain).search("  ") == {"success": False, "message": "I didn't catch what to search for."}


def test_url_logged_by_host_only(caplog):
    brain = FakeBrain()
    with caplog.at_level(logging.DEBUG):
        at(brain).open_url("https://example.com/private/path?token=secret")
        at(brain).search("my secret words")
    logged = caplog.text
    assert "example.com" in logged and "www.google.com" in logged
    for hidden in ("private", "token", "secret"):
        assert hidden not in logged


def test_close_matches_open_windows():
    brain = FakeBrain()
    brain.computers.set_windows("pc", ["Discord", "Firefox"])
    assert at(brain).close("discord") == {"success": True, "message": "Closing Discord."}
    assert brain.computer.asked == [("close", {"name": "Discord"})]
    assert at(brain).close("steam") == {"success": False, "message": "Steam isn't open."}
    assert len(brain.computer.asked) == 1
    brain.computer.answer = {"error": "Steam isn't open."}
    assert at(brain).close("firefox") == {"success": False, "message": "Steam isn't open."}


def test_close_this_closes_the_front_window():
    brain = FakeBrain()
    brain.computer.answer = {"said": "Closing Notepad."}
    assert at(brain).close("this") == {"success": True, "message": "Closing Notepad."}
    assert brain.computer.asked == [("close_front", {})]


def test_offered_only_while_a_computer_opens_things():
    brain = FakeBrain()
    assert not at(brain).offered()
    brain.computers.set_apps("pc", [])
    assert at(brain).offered()
    brain.rooms.opens_things = lambda name: False
    assert not at(brain).offered()


def test_with_several_computers_and_none_named_asks_which():
    brain = FakeBrain(target=None)
    brain.computers.set_apps("pc", [])
    brain.computers.set_apps("mac", [])
    assert at(brain, "bedroom").open("Steam") == {"success": False, "message": "Which one: Nathaniel's PC or the Mac?"}


def test_volume_on_the_computer():
    brain = FakeBrain()
    brain.computer.system_volume = True
    brain.computer.answer = {"level": 40, "muted": False}
    brain.not_connected = lambda name: "Your PC's off."
    assert at(brain).volume(direction="up", step=10) == {"success": True, "message": "Volume 40 percent.",
                                                         "level": 40, "muted": False}
    assert brain.computer.asked == [("volume", {"direction": "up", "step": 10})]
    brain.computer.answer = {"level": 40, "muted": True}
    assert at(brain).volume(mute=True)["message"] == "Muted."
    brain.computer.answer = {"level": 40, "muted": False}
    assert at(brain).volume(mute=False)["message"] == "Unmuted."
    # One which can't, or isn't there.
    brain.computer.system_volume = False
    assert at(brain).volume(level=30) == {"success": False, "message": "Nathaniel's PC can't change its own volume."}
    assert at(brain, "bedroom").volume(level=30) == {"success": False, "message": "Your PC's off."}


def hooks(brain, spoken_at="pc", machine="pc"):
    """ComputerHooks for a session at a satellite, with the brain naming only "the pc" as a satellite."""
    brain.names_satellite = lambda said: said == "the pc"
    brain.rooms.machine = lambda name: machine if name == spoken_at else "pc"
    return ComputerHooks.for_satellite(brain, spoken_at)


def test_hooks_name_an_app_or_window_on_the_computer_meant():
    brain = FakeBrain()
    brain.computers.set_apps("pc", apps("Fortnite", "Overwatch", "Overwatch 2", "Rocket League", "Rocket Arena"))
    brain.computers.set_windows("pc", ["Discord"])
    found = hooks(brain)
    assert found.names_app("fortnite", None) and found.names_app("overwatch", None)
    # Several it might be is left to Claude, whose tool asks which; one with every word said is
    # the computer's when said at it, but not from the Pi.
    assert not found.names_app("rocket", None)
    assert found.names_app("league", None) and not found.names_app("league", None, False)
    assert not hooks(brain, spoken_at="pi", machine="pi").names_app("league", None)
    assert hooks(brain, spoken_at="pi", machine="pi").names_app("rocket league", None)
    # The app called exactly that, or his own name for one, for a web address's name.
    brain.aliases = {"rl": "Rocket League"}
    assert found.app_called("rl") and found.app_called("fortnite") and not found.app_called("rocket")
    assert not found.names_app("the garage", None)
    assert found.names_window("discord", None) and not found.names_window("steam", None)
    # With the PC named, whatever is said is the PC's; with words which name no satellite, it isn't.
    assert found.names_app("the garage", "the pc") and found.names_window("steam", "the pc")
    assert not found.names_app("fortnite", "the left")
    assert found.opens_somewhere() and found.at_pc() and found.opens_here()


def test_hooks_without_a_computer_fall_through():
    brain = FakeBrain(target=None)
    brain.computers.set_apps("pc", apps("Fortnite"))
    found = hooks(brain, spoken_at="pi", machine="pi")
    assert not found.names_app("fortnite", None) and not found.names_window("fortnite", None)
    assert not found.opens_somewhere() and not found.at_pc()
    # A computer which is off is still somewhere, to say so - and its apps are still its.
    asleep = FakeBrain(target=None, problem="Your PC's off.", meant="pc")
    asleep.computers.set_apps("pc", apps("Fortnite"))
    found = hooks(asleep, spoken_at="pi", machine="pi")
    assert found.opens_somewhere() and found.names_app("fortnite", None)
    # The Pi itself doesn't open things.
    assert not found.opens_here()
    assert at(asleep, "pi").open("fortnite") == {"success": False, "message": "Your PC's off."}
