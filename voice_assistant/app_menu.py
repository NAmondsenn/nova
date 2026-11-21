"""
App Menu Module.

What the app's tray icon shows, worked out from what the app knows: the menu, which icon, and
the text when the pointer rests on it. Nothing here draws anything or reaches the satellite, so
the PC and the Mac each give it their state and put the answer on their own tray.
"""

from typing import List, NamedTuple, Optional


class AppState(NamedTuple):
    """
    What the app knows when it works out its menu.

    Attributes:
        connected: Whether the satellite is connected to the brain.
        mic_muted: Whether the microphone is muted, or None when it can't be reached.
        playing: Whether music is playing, or None when the app was never told.
        shortcut: The shortcut as shown ("Ctrl+Alt+N"), or None when there is none or it's taken.
        page: Whether there's an address for the settings page.
        at_logon: Whether the app starts when he signs in.
        music: Whether the app has media controls, without which the brain ignores a press of
            Play / Pause or Next from it.
    """

    connected: bool
    mic_muted: Optional[bool]
    playing: Optional[bool]
    shortcut: Optional[str]
    page: bool
    at_logon: bool
    music: bool = True


class Item(NamedTuple):
    """
    One line of the menu.

    Attributes:
        id: What the app does when it's chosen; empty for a separator.
        label: The words on the line; empty for a separator.
        keys: The shortcut shown beside the label, if any.
        ticked: Whether the line has a tick.
        enabled: Whether it can be chosen; a greyed line can't.
    """

    id: str
    label: str
    keys: str = ""
    ticked: bool = False
    enabled: bool = True


SEPARATOR = Item("", "")


def items(state: AppState) -> List[Item]:
    """
    Works out the menu.

    Args:
        state: What the app knows.

    Returns:
        The lines in order: listen, play_pause, next, mute, a separator, settings, at_logon,
        restart and quit. The music is greyed while not connected or without media controls,
        mute while the microphone can't be reached, and settings while there's no address for
        the page.
    """
    if state.playing is None:
        play_pause = "Play / Pause"
    else:
        play_pause = "Pause" if state.playing else "Play"
    music = state.connected and state.music
    return [
        Item("listen", "Listen", keys=state.shortcut or ""),
        Item("play_pause", play_pause, enabled=music),
        Item("next", "Next", enabled=music),
        Item("mute", "Mute microphone", ticked=state.mic_muted is True, enabled=state.mic_muted is not None),
        SEPARATOR,
        Item("settings", "Settings", enabled=state.page),
        Item("at_logon", "Start with Windows", ticked=state.at_logon),
        Item("restart", "Restart Nova"),
        Item("quit", "Quit"),
    ]


def icon(state: AppState) -> str:
    """
    Picks the icon.

    Args:
        state: What the app knows.

    Returns:
        "offline" when not connected, else "muted" when the microphone is muted, else "ready".
    """
    if not state.connected:
        return "offline"
    return "muted" if state.mic_muted else "ready"


def hover(state: AppState) -> str:
    """
    Works out the text shown when the pointer rests on the icon.

    Args:
        state: What the app knows.

    Returns:
        "Nova: not connected", "Nova: microphone muted" or "Nova", by the same rule as icon().
    """
    return {"offline": "Nova: not connected", "muted": "Nova: microphone muted", "ready": "Nova"}[icon(state)]
