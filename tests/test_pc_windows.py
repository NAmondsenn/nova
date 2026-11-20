"""
PC Windows Test
Checks which windows the PC satellite lists, closes and brings to the front, against a
fake of the few Windows calls it makes - Windows itself is never asked.
"""

import logging
import os

import pytest

from voice_assistant import pc_windows

SELF = os.getpid()


class FakeWindows:
    """Stands in for pc_windows._Win32: some windows, each with its class, process and exe, front first."""

    def __init__(self, windows, front=0):
        # hwnd: (class, pid, exe path)
        self.shown = dict(windows)
        self.front = front
        self.descriptions = {}
        self.closed = []
        self.raised = []
        self.restored = []
        self.attached = []
        self.allowed = 0
        self.iconic = set()
        # Whether SetForegroundWindow works without the input attached, and with it.
        self.refuse_alone = False
        self.joined = False
        self.browser = None
        self.started = []
        # Windows which appear once this many looks have been had, as (hwnd, entry).
        self.later = []
        self.looks = 0

    def windows(self):
        self.looks += 1
        for after, hwnd, entry in list(self.later):
            if self.looks > after:
                self.shown[hwnd] = entry
                self.later.remove((after, hwnd, entry))
        return list(self.shown)

    def window_class(self, hwnd):
        return self.shown.get(hwnd, ("Desktop", 0, ""))[0]

    def pid(self, hwnd):
        return self.shown.get(hwnd, ("", 0, ""))[1]

    def exe(self, pid):
        return next((path for _, each, path in self.shown.values() if each == pid), None)

    def description(self, path):
        return self.descriptions.get(path)

    def foreground(self):
        return self.front

    def post_close(self, hwnd):
        self.closed.append(hwnd)
        return True

    def allow_foreground(self):
        self.allowed += 1

    def is_iconic(self, hwnd):
        return hwnd in self.iconic

    def restore(self, hwnd):
        self.restored.append(hwnd)

    def set_foreground(self, hwnd):
        if self.refuse_alone and not self.joined:
            return False
        self.raised.append(hwnd)
        return True

    def thread_of(self, hwnd):
        return 500 if hwnd else 0

    def own_thread(self):
        return 7

    def attach(self, attach, to, on):
        self.attached.append((attach, to, on))
        self.joined = on
        return True

    def start(self, target):
        self.started.append(target)

    def default_browser(self):
        return self.browser


@pytest.fixture
def windows(monkeypatch):
    """Gives pc_windows a fake Windows holding the windows a test puts in it."""
    def make(entries, front=0):
        fake = FakeWindows(entries, front)
        monkeypatch.setattr(pc_windows, "_api", fake)
        monkeypatch.setattr(pc_windows, "_names", {})
        monkeypatch.setattr(pc_windows, "_installed", [])
        monkeypatch.setattr(pc_windows, "FRONT_EVERY", 0)
        monkeypatch.setattr(pc_windows, "BROWSER_SETTLE", 0)
        return fake
    return make


DESKTOP = (10, ("Progman", 4000, r"C:\Windows\explorer.exe"))
TASKBAR = (11, ("Shell_TrayWnd", 4000, r"C:\Windows\explorer.exe"))
FOLDER = (12, ("CabinetWClass", 4000, r"C:\Windows\explorer.exe"))
START = (13, ("Windows.UI.Core.CoreWindow", 4100, r"C:\Windows\SystemApps\StartMenuExperienceHost.exe"))
NOVA = (14, ("Nova", SELF, r"C:\Nova\venv\Scripts\pythonw.exe"))
DISCORD = (20, ("Chrome_WidgetWin_1", 5000, r"C:\Users\x\AppData\Local\Discord\app-1\Discord.exe"))
DISCORD_TOO = (21, ("Chrome_WidgetWin_1", 5000, r"C:\Users\x\AppData\Local\Discord\app-1\Discord.exe"))
NOTEPAD = (30, ("Notepad", 6000, r"C:\Windows\notepad.exe"))
FIREFOX = (40, ("MozillaWindowClass", 7000, r"C:\Program Files\Mozilla Firefox\firefox.exe"))


def test_open_windows_names_programs_once_never_the_shell_or_nova(windows):
    fake = windows(dict([DESKTOP, TASKBAR, FOLDER, START, NOVA, DISCORD, DISCORD_TOO, NOTEPAD]))
    fake.descriptions[r"C:\Windows\notepad.exe"] = "Notepad"
    # Discord's exe says nothing of itself, so it's named after the exe.
    assert pc_windows.open_windows() == ["Discord", "Notepad"]


def test_close_never_explorer_or_self(windows):
    fake = windows(dict([DESKTOP, TASKBAR, FOLDER, START, NOVA, DISCORD, DISCORD_TOO]))
    assert pc_windows.close("explorer") is None
    assert pc_windows.close("Windows Explorer") is None
    assert pc_windows.close("pythonw") is None
    assert pc_windows.close("StartMenuExperienceHost") is None
    assert fake.closed == []
    # Every window of the program asked for, and only as its X would.
    assert pc_windows.close("discord") == "Discord"
    assert fake.closed == [20, 21]


def test_close_matches_the_exe_name_as_well_as_the_description(windows):
    fake = windows(dict([NOTEPAD]))
    fake.descriptions[r"C:\Windows\notepad.exe"] = "Notepad Editor"
    assert pc_windows.close("notepad") == "Notepad Editor"
    assert fake.closed == [30]


FORTNITE = (50, ("UnrealWindow", 8000, r"D:\Gaming\Fortnite\FortniteGame\Binaries\Win64"
                                          r"\FortniteClient-Win64-Shipping.exe"))


def test_a_games_window_is_named_and_closed_by_its_name_in_the_list(windows):
    fake = windows(dict([FORTNITE, NOTEPAD]))
    fake.descriptions[FORTNITE[1][2]] = "FortniteClient-Win64-Shipping"
    pc_windows.set_installed([{"id": "a", "name": "Fortnite", "kind": "epic", "folder": "d:/gaming/fortnite"},
                              {"id": "b", "name": "Notepad", "kind": "app"},
                              # A folder only starting the same isn't the game's.
                              {"id": "c", "name": "Fort", "kind": "steam", "folder": r"D:\Gaming\Fort"}])
    assert pc_windows.open_windows() == ["Fortnite", "notepad"]
    assert pc_windows.close("fortnite") == "Fortnite"
    assert fake.closed == [50]
    # Its own name still closes it too.
    assert pc_windows.close("FortniteClient-Win64-Shipping") == "Fortnite"
    assert fake.closed == [50, 50]


def test_close_of_something_not_open_closes_nothing(windows):
    fake = windows(dict([NOTEPAD]))
    assert pc_windows.close("Steam") is None
    assert fake.closed == []


@pytest.mark.parametrize("front", [0, 10, 11, 12, 13, 14])
def test_close_front_never_desktop(windows, front):
    # Nothing in front, the desktop, the taskbar, a folder, the Start menu, or Nova itself.
    fake = windows(dict([DESKTOP, TASKBAR, FOLDER, START, NOVA, NOTEPAD]), front=front)
    assert pc_windows.close_front() is None
    assert fake.closed == []


def test_close_front_closes_the_program_in_front(windows):
    fake = windows(dict([DESKTOP, NOTEPAD, DISCORD]), front=30)
    assert pc_windows.close_front() == "notepad"
    assert fake.closed == [30]


def test_ready_to_open_lets_what_opens_take_the_front(windows):
    fake = windows(dict([NOTEPAD]))
    assert pc_windows.ready_to_open() == frozenset({30})
    assert fake.allowed == 1


def test_a_new_window_is_brought_to_the_front_restored(windows):
    fake = windows(dict([NOTEPAD]), front=30)
    before = pc_windows.ready_to_open()
    fake.later.append((2, 20, DISCORD[1]))
    fake.iconic.add(20)
    assert pc_windows.bring_to_front(before) is True
    assert fake.restored == [20] and fake.raised == [20]
    assert fake.attached == []


def test_a_refused_front_is_tried_again_with_the_input_joined_then_let_go(windows):
    fake = windows(dict([NOTEPAD]), front=30)
    before = pc_windows.ready_to_open()
    fake.later.append((0, 20, DISCORD[1]))
    fake.refuse_alone = True
    assert pc_windows.bring_to_front(before) is True
    assert fake.raised == [20]
    assert fake.attached == [(500, 7, True), (500, 7, False)]


def test_a_browser_already_open_has_its_window_raised(windows):
    fake = windows(dict([NOTEPAD, FIREFOX]), front=30)
    fake.browser = "firefox.exe"
    before = pc_windows.ready_to_open()
    assert pc_windows.bring_to_front(before, browser=True, wait=5) is True
    assert fake.raised == [40]


def test_nothing_new_raises_nothing_and_gives_up(windows, caplog):
    fake = windows(dict([NOTEPAD]))
    before = pc_windows.ready_to_open()
    with caplog.at_level(logging.INFO):
        assert pc_windows.bring_to_front(before, wait=0) is False
    assert fake.raised == []


def test_bringing_to_the_front_never_raises(windows, caplog):
    fake = windows(dict([NOTEPAD]))

    def fails():
        raise OSError(r"C:\Users\someone")

    fake.windows = fails
    with caplog.at_level(logging.WARNING):
        assert pc_windows.bring_to_front(frozenset()) is False
    assert "OSError" in caplog.text and "someone" not in caplog.text


def test_off_windows_nothing_loads(monkeypatch):
    monkeypatch.setattr(pc_windows, "_api", None)
    monkeypatch.setattr(pc_windows.sys, "platform", "linux")
    assert pc_windows.load() is False
