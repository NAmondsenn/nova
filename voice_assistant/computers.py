"""
Computers Module.

What the brain knows of each computer satellite's things to open: the apps it has
installed, kept even once it has gone so they can still be talked about, and the
windows open on it, which are only true while it is connected. Also how a name he said
is matched to one of them, whatever the marks and capitals of the real name.

Only the standard library is imported at module level, so the satellite can import
parts of this too.
"""

import re
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Said, but not part of an app's name.
IGNORED_WORDS = {"the", "app", "game"}
# The most names given back as choices when what he said fits several.
MOST_CHOICES = 4
# Marks in real names, such as "EA SPORTS FC™ 25", which he doesn't say.
_MARKS = "™®©�"


def plain(name: str) -> str:
    """
    Lowers a name to the words in it.

    Args:
        name: An app or window name, or what was said.

    Returns:
        It lower-case, without trademark marks, punctuation as spaces and one space
        between words: "EA SPORTS FC™ 25" is "ea sports fc 25".
    """
    for mark in _MARKS:
        name = name.replace(mark, "")
    return " ".join(re.sub(r"[^\w\s]|_", " ", name.lower()).split())


@dataclass
class Match:
    """What a spoken name matched: one app, or its names if it fits several, or neither."""
    item: Optional[Dict] = None
    choices: List[str] = field(default_factory=list)


def _words_in(said: str) -> List[str]:
    """The words of what was said which count towards a match."""
    return [word for word in plain(said).split() if word not in IGNORED_WORDS]


def _find(said: str, names: List[str]) -> Match:
    """
    Finds which of the names was said, as the index of one or the names if several fit.

    Returns:
        A Match whose item holds the index, as {"index": n}, or choices.
    """
    words = _words_in(said)
    # Both ways, so a name with "the" or "game" in it still matches as said in full.
    wanted = {plain(said), " ".join(words)}
    for index, name in enumerate(names):
        if plain(name) in wanted:
            return Match(item={"index": index})
    if not words:
        return Match()
    fits = [index for index, name in enumerate(names) if set(words) <= set(plain(name).split())]
    if len(fits) == 1:
        return Match(item={"index": fits[0]})
    if fits:
        return Match(choices=sorted({names[index] for index in fits})[:MOST_CHOICES])
    return Match()


def match_app(said: str, items: List[Dict], aliases: Dict[str, str]) -> Match:
    """
    Matches what he said to one of a computer's apps.

    Args:
        said: The name as he said it.
        items: The computer's apps, each with id, name and kind.
        aliases: His own short names for apps, as said name to app name.

    Returns:
        The app if one fits - an alias first, then the exact name, then the one app
        whose name has every word said - or the names of those which fit if several do,
        or an empty Match.
    """
    names = [item["name"] for item in items]
    for alias, target in aliases.items():
        if plain(alias) == plain(said):
            said = target
            break
    found = _find(said, names)
    if found.item is not None:
        return Match(item=items[found.item["index"]])
    return found


def match_window(said: str, names: List[str]) -> Optional[str]:
    """
    Matches what he said to one of a computer's open windows.

    Args:
        said: The name as he said it.
        names: The windows' names.

    Returns:
        The one window which fits, by the same rules as an app, or None if none does or
        several do.
    """
    found = _find(said, names)
    return names[found.item["index"]] if found.item is not None else None


class Computers:
    """The apps and windows of each computer satellite, as the brain last heard them."""

    def __init__(self):
        self._lock = threading.Lock()
        self._apps: Dict[str, List[Dict]] = {}
        self._windows: Dict[str, List[str]] = {}

    def set_apps(self, name: str, items: List[Dict]):
        """Keeps a satellite's apps, in place of any it had."""
        with self._lock:
            self._apps[name] = list(items)

    def apps(self, name: str) -> List[Dict]:
        """Returns a satellite's apps as last heard, even if it has gone, or none."""
        with self._lock:
            return list(self._apps.get(name, []))

    def set_windows(self, name: str, names: List[str]):
        """Keeps a satellite's open windows, in place of any it had."""
        with self._lock:
            self._windows[name] = list(names)

    def windows(self, name: str) -> List[str]:
        """Returns a satellite's open windows, or none if it has gone or said none."""
        with self._lock:
            return list(self._windows.get(name, []))

    def gone(self, name: str):
        """Drops a satellite's windows, which can't be true any more. Its apps are kept."""
        with self._lock:
            self._windows.pop(name, None)

    def known(self) -> List[str]:
        """Returns the satellites whose apps have been set since the brain started."""
        with self._lock:
            return list(self._apps)
