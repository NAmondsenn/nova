"""Tests for the app's menu: what the tray icon offers, and how it looks, from the app's state."""

from voice_assistant.app_menu import AppState, SEPARATOR, hover, icon, items

ready = AppState(True, False, None, "Ctrl+Alt+N", True, True)


def by_id(state):
    return {item.id: item for item in items(state)}


def test_the_menu_as_he_approved_it():
    menu = items(ready)
    assert [item.label for item in menu] == ["Listen", "Play / Pause", "Next", "Mute microphone", "", "Settings",
                                             "Start with Windows", "Restart Nova", "Quit"]
    assert [item.id for item in menu] == ["listen", "play_pause", "next", "mute", "", "settings", "at_logon",
                                          "restart", "quit"]
    assert menu[4] == SEPARATOR
    assert by_id(ready)["listen"].keys == "Ctrl+Alt+N"
    assert by_id(ready)["at_logon"].ticked is True
    assert all(item.enabled for item in menu)


def test_play_pause_reads_what_it_will_do():
    assert by_id(ready._replace(playing=True))["play_pause"].label == "Pause"
    assert by_id(ready._replace(playing=False))["play_pause"].label == "Play"
    assert by_id(ready._replace(playing=None))["play_pause"].label == "Play / Pause"


def test_not_connected_greys_only_the_music():
    greyed = [item.id for item in items(ready._replace(connected=False)) if item.enabled is False]
    assert greyed == ["play_pause", "next"]


def test_without_media_controls_the_music_is_greyed():
    # The brain drops a media press from a satellite which has said it has no media controls.
    assert ready.music is True
    greyed = [item.id for item in items(ready._replace(music=False)) if item.enabled is False]
    assert greyed == ["play_pause", "next"]
    greyed = [item.id for item in items(ready._replace(music=False, connected=False)) if item.enabled is False]
    assert greyed == ["play_pause", "next"]


def test_mute_is_ticked_when_muted_and_greyed_when_unreachable():
    assert by_id(ready._replace(mic_muted=True))["mute"].ticked is True
    assert by_id(ready)["mute"].ticked is False
    unreachable = by_id(ready._replace(mic_muted=None))["mute"]
    assert unreachable.enabled is False
    assert unreachable.ticked is False


def test_settings_is_greyed_with_no_address():
    assert by_id(ready._replace(page=False))["settings"].enabled is False


def test_no_shortcut_shows_none():
    assert by_id(ready._replace(shortcut=None))["listen"].keys == ""


def test_the_icon_and_hover():
    assert (icon(ready), hover(ready)) == ("ready", "Nova")
    muted = ready._replace(mic_muted=True)
    assert (icon(muted), hover(muted)) == ("muted", "Nova: microphone muted")
    for state in (ready._replace(connected=False), muted._replace(connected=False)):
        assert (icon(state), hover(state)) == ("offline", "Nova: not connected")
