"""
Windows Media Test
Checks how the PC satellite shows Nova's music in Windows' media controls and passes on
their buttons - see docs/decisions/0031 - against a fake of the few parts of winrt it
uses, which notes every property set and the line it was set on. winrt itself is never
imported: it's only on the PC.
"""

import threading
import logging
import sys
from types import SimpleNamespace

import pytest

from voice_assistant import windows_media
from voice_assistant.windows_media import UNCHANGED, WindowsMedia


class Noted:
    """Notes each property set on it, as (name, value, the line of the caller which set it), in one shared list."""

    def __init__(self, log, name, **attributes):
        object.__setattr__(self, "_log", log)
        object.__setattr__(self, "_name", name)
        for key, value in attributes.items():
            object.__setattr__(self, key, value)

    def __setattr__(self, key, value):
        self._log.append((f"{self._name}.{key}", value, sys._getframe(1).f_lineno))
        object.__setattr__(self, key, value)


class FakeWinrt:
    """Stands in for windows_media's winrt backend: MediaPlayer, its transport controls, the enums and the cover."""

    MediaPlaybackStatus = SimpleNamespace(PLAYING="status-playing", PAUSED="status-paused", STOPPED="status-stopped",
                                          CLOSED="status-closed")
    MediaPlaybackType = SimpleNamespace(MUSIC="type-music")
    SystemMediaTransportControlsButton = SimpleNamespace(PLAY="button-play", PAUSE="button-pause",
                                                         NEXT="button-next", PREVIOUS="button-previous",
                                                         STOP="button-stop", FAST_FORWARD="button-ff")

    def __init__(self):
        self.log = []
        self.handlers = {}
        self.updates = 0
        self.cleared = 0
        self.failing = None
        log = self.log
        self.properties = Noted(log, "music")
        self.updater = Noted(log, "updater", music_properties=self.properties, update=self._update,
                             clear_all=self._clear_all)
        self.controls = Noted(log, "smtc", display_updater=self.updater, add_button_pressed=self._add,
                              remove_button_pressed=self._remove)
        self.players = []
        # Which thread made the player and registered the handler - see the thread test.
        self.made_on = None
        self.registered_on = None

    def set_app_id(self, app_id):
        self.log.append(("app_id", app_id, None))

    def MediaPlayer(self):
        self.log.append(("MediaPlayer()", None, None))
        self.made_on = threading.get_ident()
        player = SimpleNamespace(command_manager=Noted(self.log, "command_manager"),
                                 system_media_transport_controls=self.controls)
        self.players.append(player)
        return player

    def thumbnail(self, picture):
        if self.failing == "thumbnail":
            raise OSError("The stream couldn't be written")
        return ("thumbnail", picture)

    def _update(self):
        if self.failing == "update":
            raise OSError("The display couldn't be updated")
        self.updates += 1
        self.log.append(("update()", None, None))

    def _clear_all(self):
        self.cleared += 1
        self.log.append(("clear_all()", None, None))

    def _add(self, handler):
        self.registered_on = threading.get_ident()
        self.handlers[7] = handler
        return 7

    def _remove(self, token):
        self.log.append(("removed", token, None))
        del self.handlers[token]

    def press(self, button):
        """Windows calls the handler with the controls and the arguments, saying the button."""
        for handler in list(self.handlers.values()):
            handler(self.controls, SimpleNamespace(button=button))

    def sets(self, name):
        """Each value given to one property, in order."""
        return [value for key, value, line in self.log if key == name]

    def after(self, marker):
        """Everything noted since the last of this marker."""
        last = max(index for index, (key, _, _) in enumerate(self.log) if key == marker)
        return self.log[last + 1:]


@pytest.fixture
def winrt():
    return FakeWinrt()


@pytest.fixture
def media(winrt):
    pressed = []
    made = WindowsMedia(pressed.append, backend=winrt)
    made.pressed = pressed
    return made


BUTTON_FLAGS = ("smtc.is_play_enabled", "smtc.is_pause_enabled", "smtc.is_next_enabled", "smtc.is_previous_enabled")


def test_it_is_named_nova_and_registered_as_a_music_session_switched_off_until_there_is_music(media, winrt):
    assert media.available
    # The name first, as Windows takes it from the process when the controls are made.
    assert winrt.log[0] == ("app_id", "Nova.Satellite", None)
    assert winrt.log[1] == ("MediaPlayer()", None, None)
    # The player's own handling of the buttons is off, so they come here.
    assert winrt.sets("command_manager.is_enabled") == [False]
    assert winrt.sets("smtc.is_enabled") == [False]
    assert list(winrt.handlers) == [7]


def test_whats_playing_is_shown_with_every_button_on_each_set_on_a_line_of_its_own(media, winrt):
    media.show("playing", "Song", "Band", "Album")
    assert winrt.sets("updater.type") == ["type-music"]
    assert winrt.sets("music.title") == ["Song"]
    assert winrt.sets("music.artist") == ["Band"]
    assert winrt.sets("music.album_title") == ["Album"]
    assert winrt.updates == 1
    assert winrt.sets("smtc.playback_status") == ["status-playing"]
    assert winrt.sets("smtc.is_enabled") == [False, True]
    # Set together in one chained line, Windows left next and previous off without an
    # error (the test of 19 November) - so four separate sets, each on its own line.
    flags = [(key, value, line) for key, value, line in winrt.log if key in BUTTON_FLAGS]
    assert [(key, value) for key, value, _ in flags] == [(flag, True) for flag in BUTTON_FLAGS]
    assert len({line for _, _, line in flags}) == 4


def test_the_controls_are_switched_on_before_the_display_is_updated_as_on_the_pc(media, winrt):
    # The order the test on 19 November proved: on, then the buttons, then what's shown
    # and whether it plays.
    media.show("playing", "Song", "Band", "Album")
    keys = [key for key, _, _ in winrt.log]
    switched_on = [index for index, (key, value, _) in enumerate(winrt.log)
                   if (key == "smtc.is_enabled" and value is True) or key in BUTTON_FLAGS]
    assert [keys[index] for index in switched_on] == ["smtc.is_enabled", *BUTTON_FLAGS]
    assert max(switched_on) < keys.index("updater.type")
    assert max(switched_on) < keys.index("update()") < keys.index("smtc.playback_status")


def test_paused_it_says_so(media, winrt):
    media.show("playing", "Song", "Band", "Album")
    media.show("paused", "Song", "Band", "Album")
    assert winrt.sets("smtc.playback_status") == ["status-playing", "status-paused"]
    # Already on, so not switched on again.
    assert winrt.sets("smtc.is_enabled") == [False, True]
    assert winrt.updates == 2


def test_the_cover_is_set_only_when_given_and_taken_away_with_none(media, winrt):
    media.show("playing", "Song", "Band", "Album", cover=b"jpeg")
    media.show("paused", "Song", "Band", "Album")
    media.show("playing", "Song", "Band", "Album", cover=UNCHANGED)
    media.show("playing", "Bare", "Band", "Album", cover=None)
    assert winrt.sets("updater.thumbnail") == [("thumbnail", b"jpeg"), None]


def test_a_cover_which_cant_be_made_leaves_the_rest_shown(media, winrt, caplog):
    winrt.failing = "thumbnail"
    with caplog.at_level(logging.WARNING):
        media.show("playing", "Song", "Band", "Album", cover=b"jpeg")
    assert winrt.sets("updater.thumbnail") == [None]
    assert winrt.sets("music.title") == ["Song"] and winrt.updates == 1
    assert winrt.sets("smtc.is_enabled") == [False, True]
    assert any("cover" in record.getMessage() for record in caplog.records)


def test_stopped_it_is_cleared_and_switched_off_and_comes_back_whole(media, winrt):
    media.show("playing", "Song", "Band", "Album", cover=b"jpeg")
    media.show("stopped", "", "", "")
    assert winrt.cleared == 1
    assert winrt.sets("smtc.is_enabled") == [False, True, False]
    assert winrt.sets("smtc.playback_status")[-1] == "status-stopped"
    # Stopped again changes nothing.
    media.show("stopped", "", "", "")
    assert winrt.sets("smtc.is_enabled") == [False, True, False]

    media.show("playing", "Next", "Band", "Album")
    since = winrt.after("clear_all()")
    assert ("updater.type", "type-music") in [(key, value) for key, value, _ in since]
    assert [(key, value) for key, value, _ in since if key in BUTTON_FLAGS + ("smtc.is_enabled",)] == [
        ("smtc.is_enabled", True)] + [(flag, True) for flag in BUTTON_FLAGS]


@pytest.mark.parametrize("button, action", [("button-play", "play"), ("button-pause", "pause"),
                                            ("button-next", "next"), ("button-previous", "previous")])
def test_each_of_the_four_buttons_is_passed_on_by_name(media, winrt, button, action):
    winrt.press(button)
    assert media.pressed == [action]


def test_any_other_button_is_ignored(media, winrt):
    winrt.press("button-stop")
    winrt.press("button-ff")
    assert media.pressed == []


def test_a_button_whose_handler_fails_is_logged_and_windows_isnt_told(winrt, caplog):
    def failing(action):
        raise RuntimeError("No brain")

    WindowsMedia(failing, backend=winrt)
    with caplog.at_level(logging.ERROR):
        winrt.press("button-pause")
    assert any("button" in record.getMessage() and record.exc_info and "No brain" in str(record.exc_info[1])
               for record in caplog.records)


def test_a_display_which_fails_is_logged_once_and_never_raises(media, winrt, caplog):
    winrt.failing = "update"
    with caplog.at_level(logging.WARNING):
        media.show("playing", "Song", "Band", "Album")
        media.show("paused", "Song", "Band", "Album")
    assert len([record for record in caplog.records if "media controls" in record.getMessage()]) == 1
    # And logged again if it fails again after working.
    winrt.failing = None
    media.show("playing", "Song", "Band", "Album")
    winrt.failing = "update"
    with caplog.at_level(logging.WARNING):
        media.show("paused", "Song", "Band", "Album")
    assert len([record for record in caplog.records if "media controls" in record.getMessage()]) == 2


def test_closing_lets_go_of_the_buttons_and_switches_it_off(media, winrt):
    media.show("playing", "Song", "Band", "Album")
    media.close()
    assert ("removed", 7, None) in winrt.log
    assert winrt.sets("smtc.is_enabled")[-1] is False
    # Nothing shown after.
    media.show("playing", "Song", "Band", "Album")
    assert winrt.updates == 1
    media.close()


def test_a_backend_which_fails_to_set_up_leaves_it_unavailable(caplog):
    class Failing(FakeWinrt):
        def MediaPlayer(self):
            raise OSError("Class not registered")

    with caplog.at_level(logging.WARNING):
        made = WindowsMedia(lambda action: None, backend=Failing())
    assert not made.available
    assert any("Class not registered" in record.getMessage() for record in caplog.records)
    made.show("playing", "Song", "Band", "Album")
    made.close()


def test_without_its_name_it_still_works(caplog):
    class Unnamed(FakeWinrt):
        def set_app_id(self, app_id):
            raise OSError("SetCurrentProcessExplicitAppUserModelID failed")

    with caplog.at_level(logging.WARNING):
        made = WindowsMedia(lambda action: None, backend=Unnamed())
    assert made.available
    assert any("Nova.Satellite" in record.getMessage() for record in caplog.records)


def test_off_windows_it_is_unavailable_and_never_imports_winrt(monkeypatch):
    monkeypatch.setattr(windows_media.sys, "platform", "linux")
    made = WindowsMedia(lambda action: None)
    assert not made.available
    assert not [name for name in sys.modules if name == "winrt" or name.startswith("winrt.")]
    made.show("playing", "Song", "Band", "Album")
    made.close()


def test_on_windows_without_winrt_it_is_unavailable(monkeypatch, caplog):
    # As on a PC whose packages are from before winrt was added.
    monkeypatch.setattr(windows_media.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winrt", None)
    with caplog.at_level(logging.INFO):
        made = WindowsMedia(lambda action: None)
    assert not made.available
    assert any("winrt" in record.getMessage() for record in caplog.records)


def test_covers_which_keep_failing_are_logged_once_until_one_works(media, winrt, caplog):
    def cover_warnings():
        return len([record for record in caplog.records if "cover" in record.getMessage()])

    winrt.failing = "thumbnail"
    with caplog.at_level(logging.WARNING):
        media.show("playing", "One", "Band", "Album", cover=b"one")
        media.show("playing", "Two", "Band", "Album", cover=b"two")
        assert cover_warnings() == 1
        winrt.failing = None
        media.show("playing", "Three", "Band", "Album", cover=b"three")
        winrt.failing = "thumbnail"
        media.show("playing", "Four", "Band", "Album", cover=b"four")
        assert cover_warnings() == 2


def test_the_controls_are_made_on_a_fresh_thread_so_windows_buttons_arrive():
    # On the PC the satellite's main thread has started PortAudio, which leaves it in a
    # single-threaded apartment with no message pump: a handler registered there only got
    # Windows' buttons when the thread ended (19 November). Made on a fresh thread, they come at once.
    winrt = FakeWinrt()
    WindowsMedia(lambda action: None, backend=winrt)
    assert winrt.made_on is not None and winrt.made_on != threading.get_ident()
    assert winrt.registered_on == winrt.made_on
