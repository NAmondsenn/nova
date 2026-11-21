"""
Shortcut Module.

A keyboard shortcut as text: reading what's typed on the settings page ("ctrl+alt+n"), writing
it back in one fixed form, and showing it the way a menu does ("Ctrl+Alt+N"). Nothing here knows
which system will listen for the keys or whether it can, so the same text serves the PC and the
Mac; whoever registers the shortcut says if it's taken.
"""

from typing import NamedTuple, Optional, Tuple

# What the settings page holds until he changes it.
DEFAULT = "ctrl+alt+n"

# The modifiers, in the order a shortcut is always written and shown.
MODIFIERS = ("ctrl", "alt", "shift", "win")

# Other names people type for a modifier, so "option+n" on a Mac's keyboard still reads.
_MODIFIER_NAMES = {
    "control": "ctrl",
    "windows": "win",
    "option": "alt",
}

# The keys a shortcut can have: letters, digits, F1 to F24 and the space bar.
KEYS = (
    tuple("abcdefghijklmnopqrstuvwxyz0123456789")
    + tuple(f"f{number}" for number in range(1, 25))
    + ("space",)
)


class Shortcut(NamedTuple):
    """
    A shortcut that can be listened for.

    Attributes:
        modifiers: Which of MODIFIERS are held, in MODIFIERS' order.
        key: The one key pressed with them, one of KEYS.
    """

    modifiers: Tuple[str, ...]
    key: str


class ShortcutError(ValueError):
    """Why some text isn't a shortcut; the text is worded to be said on the settings page."""


def _is_function_key(key: str) -> bool:
    return key.startswith("f") and key[1:].isdigit()


def _count(number: int) -> str:
    """A number of keys as a word, as a sentence says it: "two", "three" - or in figures past nine."""
    words = ("two", "three", "four", "five", "six", "seven", "eight", "nine")
    return words[number - 2] if 2 <= number <= 9 else str(number)


def _listed(keys) -> str:
    """Keys named in a sentence: "'a' and 'b'", or "'a', 'b' and 'c'"."""
    quoted = [f"'{key}'" for key in keys]
    return ", ".join(quoted[:-1]) + " and " + quoted[-1]


def read(text: str) -> Optional[Shortcut]:
    """
    Reads a shortcut from what was typed.

    Case, spaces round the plus signs and the order of the modifiers don't matter, and a
    modifier typed twice counts once.

    Args:
        text: The shortcut as typed, e.g. "Ctrl + Alt + N".

    Returns:
        The shortcut, or None when the text is empty or only spaces (no shortcut).

    Raises:
        ShortcutError: If the text isn't a string, or isn't a usable shortcut; its text says why.
    """
    if not isinstance(text, str):
        raise ShortcutError("That didn't make sense.")
    words = [word.strip().lower() for word in text.split("+") if word.strip()]
    if not words:
        return None
    words = [_MODIFIER_NAMES.get(word, word) for word in words]
    held = {word for word in words if word in MODIFIERS}
    keys = [word for word in words if word not in MODIFIERS]
    if not keys:
        raise ShortcutError("A shortcut needs a key as well as Ctrl, Alt, Shift or Win.")
    for key in keys:
        if key not in KEYS:
            raise ShortcutError(f"Nova doesn't know the key '{key}'.")
    if len(keys) > 1:
        raise ShortcutError(f"A shortcut has one key, and that had {_count(len(keys))}: {_listed(keys)}.")
    # A bare letter would fire whenever he typed it; a function key is rare enough to stand alone.
    if not held and not _is_function_key(keys[0]):
        raise ShortcutError("A shortcut needs Ctrl, Alt, Shift or Win with the key, unless it's a function key.")
    # Registered, Shift with a letter, digit or the space bar would swallow that capital or
    # symbol in every program; with Ctrl, Alt or Win as well it's no longer typing.
    if held == {"shift"} and not _is_function_key(keys[0]):
        raise ShortcutError("Shift alone with that key would stop you typing it. Add Ctrl, Alt or Win.")
    return Shortcut(tuple(name for name in MODIFIERS if name in held), keys[0])


def written(shortcut: Optional[Shortcut]) -> str:
    """
    Writes a shortcut in the one form that read() gives back unchanged.

    Args:
        shortcut: The shortcut, or None for none.

    Returns:
        The text, e.g. "ctrl+alt+n", or "" for None.
    """
    if shortcut is None:
        return ""
    return "+".join(shortcut.modifiers + (shortcut.key,))


def shown(shortcut: Shortcut) -> str:
    """
    Writes a shortcut the way a menu shows it.

    Args:
        shortcut: The shortcut.

    Returns:
        The text, e.g. "Ctrl+Alt+N", "Ctrl+F5" or "Win+Space".
    """
    key = "Space" if shortcut.key == "space" else shortcut.key.upper()
    return "+".join([name.capitalize() for name in shortcut.modifiers] + [key])
