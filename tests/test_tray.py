"""
Tray Test
Checks Nova's tray icon on the computer: the icon and its hover text follow the state, a
click draws the menu from the state read afresh and passes on what is chosen, the shortcut
is registered and let go and a taken one reported, the icon is put back when the taskbar is
made again, and none of it ever keeps the satellite waiting or fails it. None of it needs
Windows: the icon, the menu and the shortcut are a fake here.
"""

import logging
import os
import sys
import threading
import time

import pytest

from voice_assistant import tray as tray_module
from voice_assistant.app_menu import AppState, Item, items
from voice_assistant.shortcut import KEYS, Shortcut
from voice_assistant.tray import Tray, _hotkey_modifiers, _menu_text, _virtual_key

ICONS = os.path.join("nova", "icons")
READY = AppState(connected=True, mic_muted=False, playing=False, shortcut="Ctrl+Alt+N", page=True, at_logon=True)
MUTED = READY._replace(mic_muted=True)
CTRL_ALT_N = Shortcut(("ctrl", "alt"), "n")
F9 = Shortcut((), "f9")


class FakeTray:
    """
    Stands in for the icon, its menu and the shortcut, keeping every call in the order it
    came, in a log shared with the satellite's side, and handing `wait` the events a test
    feeds it as if they were clicks, a press of the shortcut or a new taskbar.
    """

    def __init__(self, log):
        self.log = log
        self.choices = []
        self.refused = set()
        # While set, the menu stays up until this is set.
        self.menu_gate = None
        self.menu_up = threading.Event()
        self._condition = threading.Condition()
        self._pending = []
        self._woken = False

    def feed(self, *events):
        with self._condition:
            self._pending.extend(events)
            self._condition.notify_all()

    def calls(self, name):
        return [entry for entry in self.log if entry[0] == name]

    def show(self, icon_path, hover):
        self.log.append(("show", icon_path, hover))

    def menu(self, menu_items):
        self.log.append(("menu", menu_items))
        self.menu_up.set()
        if self.menu_gate is not None:
            self.menu_gate.wait(5)
        return self.choices.pop(0) if self.choices else None

    def register(self, modifiers, key):
        self.log.append(("register", modifiers, key))
        return key not in self.refused

    def unregister(self):
        self.log.append(("unregister",))

    def wait(self, seconds):
        with self._condition:
            self._condition.wait_for(lambda: self._pending or self._woken, seconds)
            events, self._pending = self._pending, []
            self._woken = False
            return events

    def wake(self):
        with self._condition:
            self._woken = True
            self._condition.notify_all()

    def destroy(self):
        self.log.append(("destroy",))


class Satellite:
    """The satellite's side: the state it reports and what it is told, in the shared log."""

    def __init__(self, log, state=READY):
        self.log = log
        self.state = state

    def read_state(self, opening):
        self.log.append(("read_state", opening))
        return self.state

    def on_choice(self, choice):
        self.log.append(("on_choice", choice))

    def on_shortcut(self):
        self.log.append(("on_shortcut",))

    def on_taken(self, taken):
        self.log.append(("on_taken", taken))


def _until(happened, seconds=1):
    """Waits up to that long for something to happen, and says whether it did."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if happened():
            return True
        time.sleep(0.01)
    return happened()


def _tray_threads():
    """The trays' own threads which are running."""
    return [thread for thread in threading.enumerate() if thread.name == "tray"]


def _warnings(caplog):
    """The warnings the tray has logged."""
    return [record for record in caplog.records
            if record.name == "voice_assistant.tray" and record.levelno == logging.WARNING]


@pytest.fixture
def trays(monkeypatch):
    """
    Makes Trays on fake icons, each with its satellite and their shared log, and closes
    each once the test is done. A click straight after the menu closes is taken as any
    other here; the test of the gap sets it itself.
    """
    monkeypatch.setattr(tray_module, "REOPEN_GAP", 0.0)
    made = []

    def make(state=READY, satellite=None, backend=None):
        log = []
        satellite = satellite or Satellite(log, state)
        satellite.log = log
        backend = backend or FakeTray(log)
        backend.log = log
        tray = Tray(satellite.read_state, satellite.on_choice, satellite.on_shortcut, satellite.on_taken, ICONS,
                    backend=lambda: backend)
        made.append(tray)
        return tray, backend, satellite

    yield make
    for tray in made:
        tray.close()


def test_the_icon_and_hover_follow_the_state(trays):
    tray, backend, satellite = trays()
    assert tray.available
    assert _until(lambda: backend.calls("show"))
    assert backend.calls("show") == [("show", os.path.join(ICONS, "nova.ico"), "Nova")]
    satellite.state = MUTED
    tray.changed()
    assert _until(lambda: len(backend.calls("show")) == 2)
    assert backend.calls("show")[-1] == ("show", os.path.join(ICONS, "nova-muted.ico"), "Nova: microphone muted")
    tray.changed()
    assert _until(lambda: backend.log.count(("read_state", False)) == 3)
    time.sleep(0.1)
    assert len(backend.calls("show")) == 2


def test_a_click_reads_the_state_afresh_and_draws_the_menu(trays):
    tray, backend, satellite = trays()
    assert _until(lambda: backend.calls("show"))
    satellite.state = MUTED
    backend.feed("click")
    assert _until(lambda: backend.calls("menu"))
    reading = backend.log.index(("read_state", True))
    drawing = backend.log.index(("menu", items(MUTED)))
    assert reading < drawing


def test_a_choice_is_passed_on(trays):
    tray, backend, satellite = trays()
    backend.choices = ["mute", None]
    backend.feed("click")
    assert _until(lambda: backend.calls("on_choice"))
    backend.feed("click")
    assert _until(lambda: len(backend.calls("menu")) == 2)
    time.sleep(0.1)
    assert backend.calls("on_choice") == [("on_choice", "mute")]


def test_a_choice_which_fails_is_logged_and_the_tray_carries_on(trays, caplog):
    caplog.set_level(logging.WARNING)
    log = []
    satellite = Satellite(log)
    chosen = []

    def failing(choice):
        chosen.append(choice)
        raise RuntimeError("the satellite broke")

    satellite.on_choice = failing
    tray, backend, _ = trays(satellite=satellite)
    backend.choices = ["quit", "next"]
    backend.feed("click")
    assert _until(lambda: chosen == ["quit"])
    backend.feed("click")
    assert _until(lambda: chosen == ["quit", "next"])
    assert _tray_threads()
    assert len(_warnings(caplog)) == 1
    assert "RuntimeError" in _warnings(caplog)[0].getMessage()


def test_the_shortcut_calls_on_shortcut(trays):
    tray, backend, satellite = trays()
    backend.feed("shortcut")
    assert _until(lambda: backend.calls("on_shortcut") == [("on_shortcut",)])


def test_a_shortcut_is_registered_and_the_old_one_let_go(trays):
    tray, backend, satellite = trays()
    tray.set_shortcut(CTRL_ALT_N)
    tray.set_shortcut(F9)
    assert _until(lambda: len(backend.calls("on_taken")) == 2)
    hotkeys = [entry for entry in backend.log if entry[0] in ("register", "unregister", "on_taken")]
    assert hotkeys == [("register", ("ctrl", "alt"), "n"), ("on_taken", False), ("unregister",),
                       ("register", (), "f9"), ("on_taken", False)]
    tray.set_shortcut(None)
    assert _until(lambda: len(backend.calls("on_taken")) == 3)
    hotkeys = [entry for entry in backend.log if entry[0] in ("register", "unregister", "on_taken")]
    assert hotkeys[-2:] == [("unregister",), ("on_taken", False)]
    assert len(backend.calls("register")) == 2


def test_the_shortcut_already_held_is_kept(trays):
    # The satellite may pass the same shortcut on with every state it is sent, and letting
    # it go each time would leave a moment in which another program could take it.
    tray, backend, satellite = trays()
    tray.set_shortcut(CTRL_ALT_N)
    assert _until(lambda: backend.calls("on_taken"))
    tray.set_shortcut(CTRL_ALT_N)
    tray.changed()
    assert _until(lambda: backend.log.count(("read_state", False)) == 2)
    assert backend.calls("register") == [("register", ("ctrl", "alt"), "n")]
    assert backend.calls("unregister") == []


def test_a_taken_shortcut_is_reported_and_a_free_one_clears_it(trays):
    tray, backend, satellite = trays()
    backend.refused = {"n"}
    tray.set_shortcut(CTRL_ALT_N)
    assert _until(lambda: backend.calls("on_taken") == [("on_taken", True)])
    # Nothing was registered, so nothing is let go before the next one.
    tray.set_shortcut(F9)
    assert _until(lambda: len(backend.calls("on_taken")) == 2)
    assert backend.calls("on_taken")[-1] == ("on_taken", False)
    assert backend.calls("unregister") == []
    # A taken shortcut set again is tried again, as the other program may have let it go.
    backend.refused = set()
    tray.set_shortcut(CTRL_ALT_N)
    assert _until(lambda: len(backend.calls("on_taken")) == 3)
    assert backend.calls("register")[-1] == ("register", ("ctrl", "alt"), "n")
    assert backend.calls("on_taken")[-1] == ("on_taken", False)


def test_the_icon_is_put_back_when_the_taskbar_is_made_again(trays):
    tray, backend, satellite = trays(state=MUTED)
    assert _until(lambda: backend.calls("show"))
    backend.feed("taskbar")
    assert _until(lambda: len(backend.calls("show")) == 2)
    muted = ("show", os.path.join(ICONS, "nova-muted.ico"), "Nova: microphone muted")
    assert backend.calls("show") == [muted, muted]


class RefusedAtFirst(FakeTray):
    """An icon Windows refuses until the taskbar is there, as at logon before Explorer is up."""

    def __init__(self, log):
        super().__init__(log)
        self.refusing = True

    def show(self, icon_path, hover):
        if self.refusing:
            self.log.append(("refused",))
            raise OSError("no taskbar yet")
        super().show(icon_path, hover)


def test_an_icon_refused_before_the_taskbar_is_there_is_shown_once_it_is(trays, caplog):
    caplog.set_level(logging.WARNING)
    tray, backend, satellite = trays(backend=RefusedAtFirst([]))
    assert _until(lambda: backend.calls("refused"))
    backend.refusing = False
    backend.feed("taskbar")
    assert _until(lambda: backend.calls("show"))
    assert backend.calls("show") == [("show", os.path.join(ICONS, "nova.ico"), "Nova")]
    assert len(_warnings(caplog)) == 1
    assert "OSError" in _warnings(caplog)[0].getMessage()


def test_shortcuts_and_menu_lines_in_windows_terms():
    assert _virtual_key("n") == 0x4E
    assert _virtual_key("a") == 0x41
    assert _virtual_key("0") == 0x30
    assert _virtual_key("9") == 0x39
    assert _virtual_key("f1") == 0x70
    assert _virtual_key("f24") == 0x87
    assert _virtual_key("space") == 0x20
    assert len({_virtual_key(key) for key in KEYS}) == len(KEYS)
    with pytest.raises(ValueError):
        _virtual_key("numpad3")
    with pytest.raises(ValueError):
        _virtual_key("f25")
    # Every shortcut holds its keys down as one press, never a press for each repeat.
    assert _hotkey_modifiers(()) == 0x4000
    assert _hotkey_modifiers(("ctrl", "alt")) == 0x4003
    assert _hotkey_modifiers(("ctrl", "alt", "shift", "win")) == 0x400F
    with pytest.raises(ValueError):
        _hotkey_modifiers(("hyper",))
    assert _menu_text(Item("listen", "Listen", keys="Ctrl+Alt+N")) == "Listen\tCtrl+Alt+N"
    assert _menu_text(Item("quit", "Quit")) == "Quit"


def test_a_backend_which_cant_be_made_is_logged_once_and_goes_quiet(caplog):
    caplog.set_level(logging.WARNING)
    before = _tray_threads()
    log = []
    satellite = Satellite(log)

    def refused():
        raise OSError("no desktop")

    tray = Tray(satellite.read_state, satellite.on_choice, satellite.on_shortcut, satellite.on_taken, ICONS,
                backend=refused)
    assert tray.available is False
    start = time.monotonic()
    tray.changed()
    tray.set_shortcut(CTRL_ALT_N)
    tray.close()
    assert time.monotonic() - start < 0.05
    assert _until(lambda: _tray_threads() == before)
    assert log == []
    assert len(_warnings(caplog)) == 1
    assert "OSError" in _warnings(caplog)[0].getMessage()


def test_a_backend_made_after_the_tray_gave_up_shows_nothing(monkeypatch, caplog):
    # Windows made the icon's window only after the satellite stopped waiting: the tray has
    # said there isn't one, so it's taken away without ever being shown.
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(tray_module, "SET_UP_WAIT", 0.05)
    before = _tray_threads()
    log = []
    satellite = Satellite(log)
    backend = FakeTray(log)
    let_go = threading.Event()

    def late():
        let_go.wait(5)
        return backend

    tray = Tray(satellite.read_state, satellite.on_choice, satellite.on_shortcut, satellite.on_taken, ICONS,
                backend=late)
    assert tray.available is False
    let_go.set()
    assert _until(lambda: _tray_threads() == before)
    assert backend.calls("show") == [] and log.count(("read_state", False)) == 0
    assert backend.calls("destroy") == [("destroy",)]
    assert len(_warnings(caplog)) == 1


class FailingWait(FakeTray):
    """An icon whose messages Windows stops giving, once it has been shown."""

    def wait(self, seconds):
        raise OSError("the queue is gone")


def test_a_backend_which_fails_later_goes_quiet(trays, caplog):
    caplog.set_level(logging.WARNING)
    before = _tray_threads()
    tray, backend, satellite = trays(backend=FailingWait([]))
    assert _until(lambda: _tray_threads() == before)
    # Destroyed on the way out, so no icon is left in the tray.
    assert backend.calls("destroy") == [("destroy",)]
    assert len(_warnings(caplog)) == 1
    assert "OSError" in _warnings(caplog)[0].getMessage()
    shown = len(backend.log)
    start = time.monotonic()
    tray.changed()
    tray.set_shortcut(CTRL_ALT_N)
    tray.close()
    assert time.monotonic() - start < 0.05
    time.sleep(0.1)
    assert len(backend.log) == shown


def test_close_takes_the_icon_away(trays):
    before = _tray_threads()
    tray, backend, satellite = trays()
    assert _until(lambda: backend.calls("show"))
    start = time.monotonic()
    tray.close()
    assert time.monotonic() - start < 2
    assert backend.log[-1] == ("destroy",)
    assert _tray_threads() == before
    tray.changed()
    tray.set_shortcut(F9)
    tray.close()
    assert backend.calls("destroy") == [("destroy",)]


def test_quit_chosen_from_the_menu_can_close_the_tray(trays, caplog):
    # Quit is chosen on the tray's own thread, and the satellite closes the tray from
    # there, which must neither wait on itself nor fail.
    caplog.set_level(logging.WARNING)
    before = _tray_threads()
    log = []
    satellite = Satellite(log)
    holder = []
    satellite.on_choice = lambda choice: holder[0].close()
    tray, backend, _ = trays(satellite=satellite)
    holder.append(tray)
    backend.choices = ["quit"]
    backend.feed("click")
    assert _until(lambda: _tray_threads() == before)
    assert backend.calls("destroy") == [("destroy",)]
    assert _warnings(caplog) == []


def test_off_windows_there_is_no_tray(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    before = _tray_threads()
    log = []
    satellite = Satellite(log)
    tray = Tray(satellite.read_state, satellite.on_choice, satellite.on_shortcut, satellite.on_taken, ICONS)
    assert tray.available is False
    assert _tray_threads() == before
    tray.changed()
    tray.set_shortcut(CTRL_ALT_N)
    tray.close()
    assert log == []


def test_changed_never_waits_while_the_menu_is_up(trays):
    tray, backend, satellite = trays()
    backend.menu_gate = threading.Event()
    backend.feed("click")
    assert backend.menu_up.wait(1)
    satellite.state = MUTED
    for told in (tray.changed, lambda: tray.set_shortcut(F9)):
        start = time.monotonic()
        told()
        assert time.monotonic() - start < 0.05
    # What was told while the menu was up is taken once it closes.
    time.sleep(0.1)
    assert backend.calls("register") == []
    backend.menu_gate.set()
    assert _until(lambda: backend.calls("register") == [("register", (), "f9")])
    assert _until(lambda: backend.calls("show")[-1][1] == os.path.join(ICONS, "nova-muted.ico"))


def test_a_click_which_closed_the_menu_doesnt_open_it_again(trays, monkeypatch):
    # A click on the icon while the menu is up closes the menu, and Windows passes that
    # same click on to the icon a moment later; and Enter on the icon can come twice.
    monkeypatch.setattr(tray_module, "REOPEN_GAP", 0.3)
    tray, backend, satellite = trays()
    backend.feed("click", "click")
    assert _until(lambda: backend.calls("menu"))
    time.sleep(0.1)
    assert len(backend.calls("menu")) == 1
    time.sleep(0.3)
    backend.feed("click")
    assert _until(lambda: len(backend.calls("menu")) == 2)
