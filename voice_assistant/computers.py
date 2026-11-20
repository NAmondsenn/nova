"""
Computers Module.

What the brain knows of each computer satellite's things to open: the apps it has
installed, kept even once it has gone so they can still be talked about, and the
windows open on it, which are only true while it is connected. Also how a name he said
is matched to one of them, whatever the marks and capitals of the real name, which web
addresses may be opened, and ComputerControl, which has a computer open, search and close
things for a session.

Only the standard library is imported at module level, so the satellite can import
parts of this too.
"""

import difflib
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlsplit

logger = logging.getLogger(__name__)

# Said, but not part of an app's name.
IGNORED_WORDS = {"the", "app", "game"}
# The most names given back as choices when what he said fits several.
MOST_CHOICES = 4
# Marks in real names, such as "EA SPORTS FC™ 25", which he doesn't say.
_MARKS = "™®©�"
# The longest web address a computer is asked to open, as the satellite checks again.
MOST_URL = 2000
# What a web address said without its https:// looks like: "bbc.co.uk", "youtube.com/watch?v=x".
_BARE_DOMAIN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:[/?#]\S*)?", re.IGNORECASE)
# Where each search site, as computer.search_site has it, takes the words searched for.
SEARCH_SITES = {
    "google": "https://www.google.com/search?q=",
    "duckduckgo": "https://duckduckgo.com/?q=",
    "bing": "https://www.bing.com/search?q=",
}
# The most of the nearest names given back when what was said matches none.
MOST_CLOSEST = 3
# Said when a web address is refused.
NOT_A_WEB_ADDRESS = "That isn't a web address I can open."
# Said when there's no computer to do something on.
NO_COMPUTER = "There's no computer here to open things on."


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


def named_exactly(said: str, names: List[str], aliases: Optional[Dict[str, str]] = None) -> bool:
    """
    Whether what he said is one of the names in full, or one of his own short names for one -
    not just some of its words, as "google" is of "Google Chrome".

    Args:
        said: The name as he said it.
        names: The apps' or windows' names.
        aliases: His own short names for apps, as said name to app name, or None.
    """
    if any(plain(alias) == plain(said) for alias in (aliases or {})):
        return True
    wanted = {plain(said), " ".join(_words_in(said))}
    return any(plain(name) in wanted for name in names)


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


def safe_url(url: str) -> Optional[str]:
    """
    Checks a web address is one a computer may be asked to open.

    Args:
        url: The address, as given - "bbc.co.uk" or "https://www.bbc.co.uk/news".

    Returns:
        It, with https:// before it if it was a bare domain - or None for any other
        scheme, a user or password in it, a space or control character, or one longer
        than MOST_URL.
    """
    if not isinstance(url, str) or any(character.isspace() or ord(character) < 32 or ord(character) == 127
                                       for character in url):
        return None
    if "://" not in url:
        # Without a scheme it must look like a domain, so "javascript:..." or a path never is one.
        if not _BARE_DOMAIN.fullmatch(url):
            return None
        url = "https://" + url
    if len(url) > MOST_URL:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not parts.hostname or "@" in parts.netloc:
        return None
    return url


def search_url(site: str, words: str) -> str:
    """
    Args:
        site: One of SEARCH_SITES - Google for any other.
        words: What to search for.

    Returns:
        The address of that search on the site, e.g. https://www.google.com/search?q=rocket+league.
    """
    return SEARCH_SITES.get(site, SEARCH_SITES["google"]) + quote_plus(words)


def url_host(url: str) -> str:
    """The host of a web address, which is all of one that's logged - "?" if it has none."""
    try:
        return urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


def _listed(names: List[str], last: str) -> str:
    """Joins names to be said: "A, B or C"."""
    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} {last} {names[-1]}"


def _capital(sentence: str) -> str:
    """A name said mid-sentence as it starts one: "the PC" as "The PC"."""
    return sentence[:1].upper() + sentence[1:]


@dataclass
class ComputerHooks:
    """
    What the local rules ask of the computers, for one session - see intents.py. Each says
    whether a request is a computer's, so one which isn't ("open the garage") falls through
    to the rules after unchanged.
    """
    # Whether the satellite spoken at is a PC, whose bare power words ("go to sleep") are its own.
    at_pc: Callable[[], bool]
    # Whether some words, with the words for where or None, name an app on the computer meant.
    names_app: Callable[[str, Optional[str]], bool]
    # Whether they name a window open on it.
    names_window: Callable[[str, Optional[str]], bool]
    # Whether, with no computer named, there is one to open things on.
    opens_somewhere: Callable[[], bool]
    # Whether the satellite spoken at is itself a computer which opens things.
    opens_here: Callable[[], bool]
    # Whether some words, with no computer named, are an app's whole name or his own name for one -
    # so "go to google.com" isn't taken for Google Chrome.
    app_called: Callable[[str], bool]

    @classmethod
    def for_satellite(cls, brain: Any, satellite: str) -> "ComputerHooks":
        """
        Args:
            brain: The Brain, for which computer is meant (Brain.computer_meant, which needs no
                connection and never asks Home Assistant, as every request is matched against
                these), its apps, windows and his names for them, and the satellites' names and
                machines.
            satellite: The satellite the user is talking to.

        Returns:
            The hooks for that satellite's session. With a satellite named, the request is
            that computer's whatever is said, so ComputerControl says "I can't find X on your
            PC." or "Your PC's off.". With none, only an app or window's whole name, or his own
            name for an app - or, said at the computer itself, the one name with every word
            said. Several it might be, or some words of a name said elsewhere ("open the
            windows" at the Pi), fall through to Assist or Claude, whose tool asks which. A
            computer which is asleep is still matched against its last list, so "open Fortnite"
            at the Pi is said to be off rather than asked of Claude.
        """
        def target(where: Optional[str]) -> Optional[str]:
            name, _ = brain.computer_meant(satellite, where)
            return name

        def names_app(said: str, where: Optional[str]) -> bool:
            if where:
                return brain.names_satellite(where)
            name = target(None)
            if name is None:
                return False
            items, aliases = brain.computers.apps(name), brain.rooms.app_names(name)
            if named_exactly(said, [item["name"] for item in items], aliases):
                return True
            return name == satellite and match_app(said, items, aliases).item is not None

        def names_window(said: str, where: Optional[str]) -> bool:
            if where:
                return brain.names_satellite(where)
            name = target(None)
            if name is None:
                return False
            windows = brain.computers.windows(name)
            if named_exactly(said, windows):
                return True
            return name == satellite and match_window(said, windows) is not None

        def opens_somewhere() -> bool:
            # Even one which isn't connected, so "Your PC's off." is said rather than Claude asked.
            return target(None) is not None

        def opens_here() -> bool:
            return target(None) == satellite

        def app_called(said: str) -> bool:
            name = target(None)
            return name is not None and named_exactly(
                said, [item["name"] for item in brain.computers.apps(name)], brain.rooms.app_names(name))

        return cls(at_pc=lambda: brain.rooms.machine(satellite) == "pc", names_app=names_app,
                   names_window=names_window, opens_somewhere=opens_somewhere, opens_here=opens_here,
                   app_called=app_called)


class ComputerControl:
    """
    Has a computer satellite open, search and close things, and set its own volume, for
    one session - the satellite the user is talking to, which may be the computer or not.
    The brain decides which computer is meant (Brain.computer_for); only an app's id, a
    checked web address or a window's name is ever sent.
    """

    def __init__(self, brain: Any, satellite: str):
        """
        Args:
            brain: The Brain, for which computer, its apps, windows and names, and its session.
            satellite: The satellite the user is talking to.
        """
        self.brain = brain
        self.satellite = satellite

    def offered(self) -> bool:
        """Whether any computer seen since the brain started opens things - so the tools are offered."""
        return bool(self._opening())

    def _opening(self) -> List[str]:
        """The computers known which he lets open things."""
        return [name for name in self.brain.computers.known() if self.brain.rooms.opens_things(name)]

    def _target(self, where: Optional[str]) -> Tuple[Optional[str], Any, Optional[Dict]]:
        """
        Finds the computer meant, and its connection.

        Args:
            where: The words for where, as said, or None.

        Returns:
            (its name, its RemoteSatellite, None) - or (None, None, the reply to give instead).
        """
        name, problem = self.brain.computer_for(self.satellite, where)
        if name is None:
            if problem is None:
                opening = self._opening()
                spoken = [self.brain.spoken_name(each) for each in opening]
                problem = f"Which one: {_listed(spoken, 'or')}?" if len(opening) > 1 else NO_COMPUTER
            return None, None, {"success": False, "message": problem}
        session = self.brain.session_for(name)
        if session is None:
            # Gone since it was chosen.
            return None, None, {"success": False,
                                "message": f"Nova isn't running on {self.brain.spoken_name(name)}."}
        return name, session.satellite, None

    def _there(self, sentence: str, name: str) -> str:
        """A sentence ending with a full stop, with " on {name}" before it when that isn't where the user is."""
        if name == self.satellite:
            return sentence
        return f"{sentence[:-1]} on {self.brain.spoken_name(name)}."

    def _ask(self, target: str, computer: Any, done: Optional[str], action: str, **fields) -> Dict:
        """
        Asks the computer, and gives what to say of it.

        Args:
            target: The computer's satellite name.
            computer: Its RemoteSatellite.
            done: What to say when it has done it, or None for the sentence it says itself.
            action: One of protocol.COMPUTER_ACTIONS.
            **fields: What the action needs.

        Returns:
            {"success", "message"}, with "level" and "muted" when it gave them.
        """
        answer = computer.ask_computer(action, **fields)
        if answer is None:
            return {"success": False, "message": f"I couldn't do that on {self.brain.spoken_name(target)}."}
        if answer.get("error"):
            return {"success": False, "message": answer["error"]}
        said = done or answer.get("said") or "Done."
        reply = {"success": True, "message": said}
        for each in ("level", "muted"):
            if each in answer:
                reply[each] = answer[each]
        return reply

    def open(self, name: str, where: Optional[str] = None) -> Dict:
        """
        Opens an app, game or the like on a computer, by its id in the computer's own list.

        Args:
            name: Its name, as said.
            where: The words for which computer, or None.

        Returns:
            {"success", "message"}: "Opening Fortnite.", which one he means, or why not -
            with the nearest names, when none fits, for Claude to choose from or ask about.
        """
        target, computer, refused = self._target(where)
        if refused:
            return refused
        items = self.brain.computers.apps(target)
        found = match_app(name, items, self.brain.rooms.app_names(target))
        if found.choices:
            return {"success": False, "message": f"Which one: {_listed(found.choices, 'or')}?"}
        spoken = self.brain.spoken_name(target)
        if found.item is None:
            message = f"I can't find {name.strip()} on {spoken}."
            names = {plain(item["name"]): item["name"] for item in items}
            closest = difflib.get_close_matches(plain(name), list(names), n=MOST_CLOSEST, cutoff=0.6)
            if closest:
                message += f" Closest: {_listed([names[each] for each in closest], 'and')}."
            return {"success": False, "message": message}
        app = found.item["name"]
        logger.info(f"Opening {app} on the {target} satellite")
        return self._ask(target, computer, self._there(f"Opening {app}.", target), "open", item=found.item["id"])

    def open_url(self, url: str, where: Optional[str] = None) -> Dict:
        """
        Opens a web page in the computer's own browser.

        Args:
            url: Its address, with or without https://.
            where: The words for which computer, or None.

        Returns:
            {"success", "message"}: "Opening bbc.co.uk.", or why not.
        """
        checked = safe_url(url)
        if checked is None:
            return {"success": False, "message": NOT_A_WEB_ADDRESS}
        target, computer, refused = self._target(where)
        if refused:
            return refused
        host = url_host(checked)
        # The host alone: the rest of an address can hold what's his.
        logger.info(f"Opening a page on {host} on the {target} satellite")
        return self._ask(target, computer, self._there(f"Opening {host}.", target), "open_url", url=checked)

    def search(self, words: str, where: Optional[str] = None) -> Dict:
        """
        Searches the web on the computer, on the search site chosen on the settings page.

        Args:
            words: What to search for.
            where: The words for which computer, or None.

        Returns:
            {"success", "message"}: "Searching for rocket league.", or why not.
        """
        words = words.strip() if isinstance(words, str) else ""
        if not words:
            return {"success": False, "message": "I didn't catch what to search for."}
        target, computer, refused = self._target(where)
        if refused:
            return refused
        config = getattr(self.brain, "config", None)
        site = config.section("computer").get("search_site", "google") if config is not None else "google"
        url = search_url(site, words)
        # Never the words, which are in the address.
        logger.info(f"Searching on {url_host(url)} on the {target} satellite")
        return self._ask(target, computer, self._there(f"Searching for {words}.", target), "open_url", url=url)

    def close(self, name: str, where: Optional[str] = None) -> Dict:
        """
        Closes a program's windows on the computer - or, for "this", the one in front.

        Args:
            name: The program, as said, or "this".
            where: The words for which computer, or None.

        Returns:
            {"success", "message"}: "Closing Discord.", "Steam isn't open.", or why not.
        """
        target, computer, refused = self._target(where)
        if refused:
            return refused
        if plain(name) == "this":
            # Which program it is only the computer knows, so it says so itself.
            logger.info(f"Closing the window in front on the {target} satellite")
            return self._ask(target, computer, None, "close_front")
        window = match_window(name, self.brain.computers.windows(target))
        if window is None:
            return {"success": False, "message": f"{_capital(name.strip())} isn't open."}
        logger.info(f"Closing {window} on the {target} satellite")
        return self._ask(target, computer, self._there(f"Closing {window}.", target), "close", name=window)

    def volume(self, **change) -> Dict:
        """
        Changes the computer the user is talking to's own volume, as Windows' volume keys do.
        Another satellite named is changed by its own session's (see ActionExecutor.set_volume).

        Args:
            **change: direction and step, level, or mute - as protocol's computer has them.

        Returns:
            {"success", "message"} with "level" and "muted" as the computer has them now -
            "Volume 40 percent.", "Muted." or "Unmuted." - or why not.
        """
        target = self.satellite
        session = self.brain.session_for(target)
        if session is None:
            return {"success": False, "message": self.brain.not_connected(target)}
        computer = session.satellite
        if not getattr(computer, "system_volume", False):
            return {"success": False,
                    "message": f"{_capital(self.brain.spoken_name(target))} can't change its own volume."}
        reply = self._ask(target, computer, None, "volume", **change)
        if reply["success"]:
            reply["message"] = ("Muted." if reply.get("muted")
                                else "Unmuted." if change.get("mute") is False
                                else f"Volume {reply['level']} percent." if "level" in reply else reply["message"])
        return reply
