"""
Football Module
What football is, and how a team is followed, from decisions 2, 5 and 6 of docs/decisions/0014.

Home Assistant is the only source of football (see HomeAssistant.state): Nova never
calls ESPN and keeps no list of teams, leagues or abbreviations, so everything here
is read from what Home Assistant's sensor of the followed teams says. A match, a team
or a reading of the sensor is only ever as good as that - when the sensor is down,
old, has an error or isn't shaped as expected, football can't be got, and the one
sentence for that is said instead of any fixture, score or result.

The followed teams are a text box in Home Assistant, and following or unfollowing one is
writing that box - only ever with a name or an id Home Assistant gave for the team, never
the words that were said.
"""

import logging
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple

import requests

from voice_assistant.batteries import HOME_FAILURES
from voice_assistant.home_assistant import FOOTBALL_BOX, FOOTBALL_TEAMS, HomeAssistant

logger = logging.getLogger(__name__)

# What is said whenever football can't be read for any reason but Home Assistant being down.
CANT_GET = "I can't get football right now."
NOT_SET_UP = "Football isn't set up in Home Assistant."
# What following and unfollowing say when the words have no team in them, or when the box has no room.
NO_TEAM = "I didn't catch which team."
BOX_FULL = "Home Assistant's list of teams is full."

# The most characters the teams box holds. Home Assistant doesn't refuse a longer value: it drops
# it without a word and answers as if it had been kept, so this check is the only guard, and
# nothing is ever written beyond it.
BOX_HOLDS = 255

# Seconds to wait for the sensor to show a team which was just followed. Home Assistant runs it
# again a few seconds after the box changes.
WAIT_FOR_SENSOR = 15

# Seconds one look at the sensor may take while waiting for it - shorter than a request to Home
# Assistant is usually given, so that one slow answer can't use up the wait.
LOOK_TIMEOUT = 3

# Seconds old the sensor may be and still count as current. Home Assistant's script runs
# every minute, so a reading five minutes old means it has stopped - which its own "stale"
# flag can't say, as that only reports ESPN's old copies, and not the script not running at all.
STALE_AFTER = 300

# The states a match is in, as the Contract has them: not started, in play, over.
_MATCH_STATES = ("pre", "in", "post")

# How a wait for the sensor to show a team just followed ends.
_SHOWN = "shown"
_FOUND_ELSEWHERE = "found elsewhere"
_NOT_YET = "not yet"

# The default of a key which has none, so it must be there. None can't be this, as most keys
# which may be left out are given None.
_REQUIRED = object()


class FootballUnavailable(Exception):
    """
    Football can't be read just now. str() of it is what to say.

    Attributes:
        why: What went wrong, for the log and never to be said - or None where the error's
            __cause__ says it, as for Home Assistant not answering.
    """

    def __init__(self, sentence: str, why: Optional[str] = None):
        """
        Args:
            sentence: What to say.
            why: What went wrong, if the sentence doesn't tell it.
        """
        super().__init__(sentence)
        self.why = why


def _is_whole(value) -> bool:
    """Whether a value is a whole number - not a bool, which Python counts as one."""
    return isinstance(value, int) and not isinstance(value, bool)


def _field(found: Dict, key: str, kind: type, default=_REQUIRED):
    """
    Reads one key of a dict Home Assistant gave, checking its type, so that a wrong shape
    is refused here rather than said - "false" as text is not False.

    Args:
        found: The dict.
        key: The key.
        kind: The type it must have: str, int, bool, list or dict. A bool isn't an int.
        default: What to give if the key is left out or None. Without one, the key is required.

    Returns:
        The value, or the default.

    Raises:
        KeyError: If a required key is missing.
        TypeError: If it isn't a kind.
    """
    value = found[key] if default is _REQUIRED else found.get(key)
    if value is None and default is not _REQUIRED:
        return default
    if not isinstance(value, kind) or (kind is int and not _is_whole(value)):
        raise TypeError(f"{key} should be a {kind.__name__}, not {value!r}")
    return value


def _instant(text: str) -> datetime:
    """
    Reads a time Home Assistant wrote, e.g. "2026-11-22T16:30:00+00:00".

    Args:
        text: The ISO time.

    Returns:
        It as an aware time in UTC.

    Raises:
        ValueError: If it isn't a time, or has no time zone - which would be taken for the
            machine's own, and be an hour out in summer.
        TypeError: If it isn't text.
    """
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        raise ValueError(f"{text!r} has no time zone")
    return when.astimezone(timezone.utc)


def _ground(venue: Optional[Dict]) -> Optional[str]:
    """
    Says where a match is played.

    Args:
        venue: The match's venue, with its name and city, or None.

    Returns:
        "Anfield in Liverpool", or just the name where there is no city; None with no name.

    Raises:
        TypeError: If the venue isn't a dict, or its name or city isn't text.
    """
    if venue is None:
        return None
    if not isinstance(venue, dict):
        raise TypeError(f"venue should be a dict, not {venue!r}")
    name = _field(venue, "name", str, None)
    city = _field(venue, "city", str, None)
    if not name:
        return None
    return f"{name} in {city}" if city else name


def _cant_get(why: str, level: int = logging.DEBUG) -> FootballUnavailable:
    """
    Makes the error for football which can't be got.

    Args:
        why: The reason, which is logged.
        level: At what level. The default is debug, as the watcher reads every 30 seconds; a
            question someone asked is worth a warning.

    Returns:
        The error, whose text is CANT_GET whatever the reason, and which carries the reason as
        its "why", so that whoever catches it - the watcher, which says it once - can log it.
    """
    logger.log(level, f"Football can't be got: {why}")
    return FootballUnavailable(CANT_GET, why)


@dataclass(frozen=True)
class Match:
    """
    One match, as Home Assistant gives it.

    Attributes:
        id: ESPN's id for the match.
        kickoff: When it starts, as an aware time in UTC.
        state: "pre" (not started), "in" (in play) or "post" (over).
        phase: Where a match which has started is - "first_half", "half_time", "second_half",
            "extra_time", "penalties", "full_time", or "off" for one postponed, abandoned or
            cancelled - or None where Home Assistant doesn't say.
        clock: ESPN's own word for the minute, e.g. "67'" or "HT", or "" if it gave none.
        home: The name to say for the home side - the full one, where Home Assistant gave it.
        away: The same for the away side.
        home_id: The home side's id.
        away_id: The away side's id.
        home_score: The home side's goals, or None before it starts.
        away_score: The away side's goals, or None before it starts.
        league: The competition's name.
        winner: "home" or "away" once there is one, None for a draw or a match not over.
        shootout: The penalties scored by each side, if it went to penalties.
        extra_time: Whether it ended after extra time.
        venue: Where it's played, e.g. "Anfield in Liverpool", or just the name with no city.
        odds: The bookmaker's chances as whole percentages ("home", "draw", "away") and its
            "source", or None. Only in answers to a question, and only before kick-off.
    """

    id: str
    kickoff: datetime
    state: str
    phase: Optional[str]
    clock: str
    home: str
    away: str
    home_id: str
    away_id: str
    home_score: Optional[int]
    away_score: Optional[int]
    league: str
    winner: Optional[str]
    shootout: Optional[Tuple[int, int]]
    extra_time: bool
    venue: Optional[str]
    odds: Optional[Dict]

    @classmethod
    def from_home(cls, found: Dict) -> "Match":
        """
        Builds a match from one of Home Assistant's dicts, shaped as the Contract in
        docs/superpowers/plans/2026-11-13-football.md has it.

        Args:
            found: The dict, with id, kickoff, state, home, away, home_id, away_id and
                league, and any of the others where Home Assistant has something to say.

        Returns:
            The match.

        Raises:
            KeyError: If a key a match always has is missing.
            TypeError: If a value isn't of the type it should be.
            ValueError: If the kick-off isn't a time with a time zone, or the state, the
                winner or the shoot-out isn't one of the Contract's.
        """
        if not isinstance(found, dict):
            raise TypeError(f"a match should be a dict, not {found!r}")
        state = _field(found, "state", str)
        if state not in _MATCH_STATES:
            raise ValueError(f"state should be one of {_MATCH_STATES}, not {state!r}")
        winner = _field(found, "winner", str, None)
        if winner not in (None, "home", "away"):
            raise ValueError(f"winner should be home or away, not {winner!r}")
        shootout = _field(found, "shootout", list, None)
        if shootout is not None:
            if len(shootout) != 2 or not all(_is_whole(goals) for goals in shootout):
                raise ValueError(f"shootout should be two whole numbers, not {shootout!r}")
            shootout = (shootout[0], shootout[1])
        odds = _field(found, "odds", dict, None)
        return cls(
            id=_field(found, "id", str),
            kickoff=_instant(_field(found, "kickoff", str)),
            state=state,
            phase=_field(found, "phase", str, None),
            clock=_field(found, "clock", str, ""),
            # The full name is only there where it differs from the short one, and is what to say.
            home=_field(found, "home_name", str, None) or _field(found, "home", str),
            away=_field(found, "away_name", str, None) or _field(found, "away", str),
            home_id=_field(found, "home_id", str),
            away_id=_field(found, "away_id", str),
            home_score=_field(found, "home_score", int, None),
            away_score=_field(found, "away_score", int, None),
            league=_field(found, "league", str),
            winner=winner,
            shootout=shootout,
            extra_time=_field(found, "extra_time", bool, False),
            venue=_ground(found.get("venue")),
            odds=dict(odds) if odds is not None else None,
        )


@dataclass(frozen=True)
class Team:
    """
    One followed team, as Home Assistant gives it.

    Attributes:
        id: ESPN's id for the team.
        name: The team's name, as ESPN has it.
        asked: The entry in Home Assistant's teams box which found it, e.g. "South Shields FC",
            so that unfollowing takes out exactly that - or None where Home Assistant doesn't say.
        stale: Whether Home Assistant is giving an old copy of this team's matches, as ESPN
            couldn't be asked just now - what it says of the team, or, from a Home Assistant which
            doesn't say (see Football.read), whatever the sensor does.
        now: Its match in play or today, if any.
        next: Its next fixtures, soonest first.
        last: Its last results, latest first.
    """

    id: str
    name: str
    asked: Optional[str]
    stale: bool
    now: Optional[Match]
    next: Tuple[Match, ...]
    last: Tuple[Match, ...]

    @classmethod
    def from_home(cls, found: Dict) -> "Team":
        """
        Builds a team from one of the dicts in the sensor's "teams".

        Args:
            found: The dict, with id, name, next and last, and asked, stale and now where there are any.

        Returns:
            The team. Its stale is only what Home Assistant says of it; Football.read() decides
            for a Home Assistant which doesn't mark teams.

        Raises:
            KeyError: If a key a team always has is missing.
            TypeError: If a value isn't of the type it should be.
            ValueError: If one of its matches is the wrong shape.
        """
        if not isinstance(found, dict):
            raise TypeError(f"a team should be a dict, not {found!r}")
        now = _field(found, "now", dict, None)
        return cls(
            id=_field(found, "id", str),
            name=_field(found, "name", str),
            asked=_field(found, "asked", str, None),
            stale=_field(found, "stale", bool, False),
            now=Match.from_home(now) if now is not None else None,
            next=tuple(Match.from_home(match) for match in _field(found, "next", list)),
            last=tuple(Match.from_home(match) for match in _field(found, "last", list)),
        )


@dataclass(frozen=True)
class Reading:
    """
    What Home Assistant's sensor said of the followed teams.

    Attributes:
        teams: The teams, in the order of the box.
        unknown: The entries in the box which ESPN found no team by.
        trimmed: Whether Home Assistant left some teams out to keep the sensor small.
        reported: When Home Assistant last wrote the sensor, in epoch seconds - later at each run of
            its script, so what tells a reading from the same one looked at again. Always there in
            one which read() gave; None only in one built by hand, which then can't be told apart.
    """

    teams: Tuple[Team, ...]
    unknown: Tuple[str, ...]
    trimmed: bool
    reported: Optional[float] = None


def _key(text: str) -> str:
    """A name with its case, spaces and punctuation left out, so "Man  City" and "man city." are the same."""
    return "".join(char for char in text.casefold() if char.isalnum())


def _words(text: str) -> Set[str]:
    """The words of a name, each as _key has it: "Queens Park Rangers" has "rangers", but "Rang" is not a word of it."""
    return {word for word in map(_key, text.split()) if word}


def _same_entry(one: str, other: str) -> bool:
    """
    Whether two entries of the teams box are the one, ignoring case and the spaces round them, as it is typed
    by hand - and the apostrophe, which Home Assistant takes out of the box before its script sees it. So the
    entry the sensor says found a team or found none, "Nottm Forest", is the box's "Nott'm Forest".
    """
    return one.replace("'", "").strip().casefold() == other.replace("'", "").strip().casefold()


def _joined(entries: List[str]) -> str:
    """
    Joins entries as the teams box has them, by ", " - or by "," where that wouldn't fit. A box typed by
    hand with bare commas holds more than the same entries in ", " would, and taking one out of it
    mustn't be what overfills it.

    Args:
        entries: The entries to keep.

    Returns:
        The value to write, which is longer than BOX_HOLDS only if even the bare commas don't fit.
    """
    value = ", ".join(entries)
    return value if len(value) <= BOX_HOLDS else ",".join(entries)


def _full_name(team: Team) -> Optional[str]:
    """
    Finds a team's full name, which the sensor only gives for the sides of its matches.

    Args:
        team: The team.

    Returns:
        The name its soonest match calls it by, or None where it has no match.
    """
    for match in (team.now, *team.next, *team.last):
        if match is None:
            continue
        if match.home_id == team.id:
            return match.home
        if match.away_id == team.id:
            return match.away
    return None


@dataclass(frozen=True)
class _Followed:
    """
    One thing in the teams box which unfollowing may be asked to take out.

    Attributes:
        entry: The entry of the box to take out.
        called: What to call it when saying what was done.
        names: Everything it can be asked for by.
        found: Whether Home Assistant found a team by the entry; False for one it couldn't.
    """

    entry: str
    called: str
    names: Tuple[str, ...]
    found: bool


def _followed(reading: Reading) -> List[_Followed]:
    """
    Lists what is in the teams box as unfollowing sees it, teams first and then the entries no team was found by.

    Args:
        reading: What the sensor says.

    Returns:
        Each, in the order of the box. A team's entry is the one which found it, or - for a Home Assistant
        which doesn't say - its name, so it is only ever taken out if the box has exactly that.
    """
    listed = []
    for team in reading.teams:
        full = _full_name(team)
        listed.append(_Followed(entry=team.asked or team.name, called=full or team.name,
                                names=tuple(name for name in (team.name, full, team.asked) if name), found=True))
    for entry in reading.unknown:
        # Home Assistant lists an entry typed twice twice, and it is one thing to take out.
        if not any(_same_entry(entry, one.entry) for one in listed):
            listed.append(_Followed(entry=entry, called=entry, names=(entry,), found=False))
    return listed


def _known_as(reading: Reading) -> List[str]:
    """
    Lists every name the followed teams can be asked for by, from the same names unfollowing goes by.

    Args:
        reading: What the sensor says.

    Returns:
        Each team's name, its full name where a match gives one, and the entry of the box which found it,
        in the order of the box and without one said twice. The entries no team was found by are left out,
        and so is an entry which is all digits: ESPN's id, which a follow writes in place of a name which
        found another team, is no name anyone asks for it by, and a request with the number in it would
        be taken to be about football.
    """
    known: List[str] = []
    for one in _followed(reading):
        if one.found:
            known.extend(name for name in one.names
                         if not name.isdigit() and not any(_key(name) == _key(seen) for seen in known))
    return known


def _meant(said: str, listed: List[_Followed]) -> List[_Followed]:
    """
    Works out which of the followed were meant: those called exactly what was said, if there are any, else
    those with every word said in one of their names. So "Rangers" is Rangers while Rangers is followed -
    never also Queens Park Rangers - and is Queens Park Rangers only where nothing is called Rangers.

    Args:
        said: The words, which have a word in them.
        listed: What _followed gave.

    Returns:
        The ones meant: none if there is nothing like it, and more than one if it can't be told which.
    """
    exact = [one for one in listed if any(_key(name) == _key(said) for name in one.names)]
    if exact:
        return exact
    words = _words(said)
    return [one for one in listed if any(words <= _words(name) for name in one.names)]


class Football:
    """
    Reads the followed teams from Home Assistant, and knows when it can't be trusted.

    The watcher's thread reads every 30 seconds while a session's reads when asked, so the
    names kept from the last good read are guarded by a lock. Following and unfollowing, which
    a session's thread and the settings page's can both ask for, have a lock of their own: a
    follow waits seconds for the sensor, and nothing which only reads may wait with it.
    """

    def __init__(self, home: HomeAssistant, settings: Callable[[], Dict], now: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        """
        Args:
            home: Home Assistant, for the sensor.
            settings: Gives the football section of the settings as they are now, read at
                each reading so that a change on the settings page is used straight away.
                "stale_after" is how many seconds old a reading may be; without it, STALE_AFTER.
            now: Gives the current epoch time.
            sleep: Waits that many seconds - what a follow waits for the sensor with.
        """
        self._home = home
        self._settings = settings
        self._now = now
        self._sleep = sleep
        self._lock = threading.Lock()
        self._changing = threading.Lock()
        self._names: List[str] = []
        self._known_as: List[str] = []

    def read(self) -> Reading:
        """
        Reads the sensor of the followed teams, afresh.

        Returns:
            The teams. A team is stale when it says so itself. Only where the sensor is stale and
            no team has a "stale" at all - a Home Assistant from before teams were marked, which
            can't tell whose data is old - is every team. A sensor which is stale while every
            team says it isn't leaves every team fresh: an entry nobody follows went wrong. Its
            reported is when Home Assistant last wrote the sensor.

        Raises:
            FootballUnavailable: Whose text is what to say, if Home Assistant couldn't be asked
                (as HomeAssistant.failure_message says), has no football sensor (NOT_SET_UP), or
                the sensor is unavailable, has an error, was last written more than the
                stale_after setting ago, or isn't shaped as the Contract has it (CANT_GET, with
                its "why" saying which).
        """
        sensor = self._state(FOOTBALL_TEAMS)
        stale_after = float(self._settings().get("stale_after", STALE_AFTER))
        try:
            reading = self._reading(sensor, stale_after)
        except (KeyError, TypeError, ValueError) as e:
            # Not logged above debug: the watcher reads every 30 seconds, and says it once itself.
            logger.debug(f"The football sensor isn't shaped as expected: {e!r}")
            raise FootballUnavailable(CANT_GET, "the sensor isn't shaped as expected") from e
        with self._lock:
            self._names = [team.name for team in reading.teams]
            self._known_as = _known_as(reading)
        return reading

    def names(self) -> List[str]:
        """
        Returns the followed teams' names from the last good read, asking nothing - so that
        Claude can be told them without a call to Home Assistant, and a read which failed
        doesn't empty them. Empty before any read.
        """
        with self._lock:
            return list(self._names)

    def known_as(self) -> List[str]:
        """
        Returns every name the followed teams are known by from the last good read, asking nothing -
        each team's name, its full name where a match gives one, and its entry in Home Assistant's
        teams box (unless that is only ESPN's id) - so that a request can be told to be about one of
        them however it is said: "Nott'm Forest" is also "Nottingham Forest". A read which failed
        doesn't empty them. Empty before any read.
        """
        with self._lock:
            return list(self._known_as)

    def ask(self, what: str, name: str, date: Optional[str] = None, days: Optional[int] = None) -> Dict:
        """
        Asks Home Assistant a question about football which the followed teams' sensor can't
        answer - any team or competition - and checks that the answer can be trusted.

        Args:
            what: The question: "team", "competition", "table", "left" or "lineups".
            name: The team or competition asked about.
            date: The first day, as YYYYMMDD, or None for today.
            days: How many days from it, or None for one.

        Returns:
            The answer, as the Contract has it for the question - or, where ESPN found nothing
            by the name, one with "unknown" listing it, which is for the caller to say.

        Raises:
            FootballUnavailable: Whose text is CANT_GET if the answer is old, has an error, isn't
                shaped as an answer, can't be read or didn't come in time; NOT_SET_UP if Home
                Assistant answered 400, as it does for a football command it hasn't got - or what to
                say, as HomeAssistant.failure_message has it, if Home Assistant couldn't be asked at all.
        """
        try:
            answer = self._home.football_ask(what, name, date or "", 1 if days is None else days)
        except requests.Timeout as e:
            # Said before any other failure, as failure_message would call it Home Assistant being out of reach.
            raise _cant_get(f"Home Assistant didn't answer in time: {e!r}", logging.WARNING) from e
        except ValueError as e:
            # Also what requests raises for an answer which isn't JSON, and what Nova's own refusal of a
            # service call is, so both are caught here and not as a failure to reach Home Assistant.
            raise _cant_get(f"Home Assistant's answer couldn't be read: {e!r}", logging.WARNING) from e
        except requests.RequestException as e:
            logger.warning(f"Football couldn't be asked of Home Assistant: {e!r}")
            # Home Assistant answers 400 for a service it hasn't got (or one which can't return a response), which
            # is football not being set up there, as a missing sensor is for the overview.
            if getattr(getattr(e, "response", None), "status_code", None) == 400:
                raise FootballUnavailable(NOT_SET_UP) from e
            raise FootballUnavailable(HomeAssistant.failure_message(e)) from e
        if not isinstance(answer, dict):
            raise _cant_get(f"Home Assistant's answer isn't a dict: {answer!r}", logging.WARNING)
        if "error" in answer:
            raise _cant_get(f"Home Assistant's football command had an error: {answer['error']!r}", logging.WARNING)
        # Every answer says whether it is old, so one which doesn't can't be vouched for.
        if not isinstance(answer.get("stale"), bool):
            raise _cant_get(f"Home Assistant's answer doesn't say whether it is old: {answer.get('stale')!r}", logging.WARNING)
        if answer["stale"]:
            raise _cant_get("Home Assistant's answer is an old copy", logging.WARNING)
        return answer

    def follow(self, name: str) -> Dict:
        """
        Follows a team: asks Home Assistant what it makes of the name, adds the team it found to the
        teams box, and waits for the sensor to show it. What is added is only ever what Home Assistant
        gave for the team - its name, or its id where the name finds another team - and never the words
        said, which go to ask() and nowhere else.

        Waits up to WAIT_FOR_SENSOR seconds (twice that if the name had to be put right), holding the
        lock of following and unfollowing, so that two changes never write over each other.

        Args:
            name: The team, as it was said.

        Returns:
            {"success": bool, "message": str}: what to say, and whether a team was followed. Success is
            True too where the team was added but the sensor hadn't shown it in time, which the message says.
        """
        said = name.strip()
        if not _words(said):
            return {"success": False, "message": NO_TEAM}
        with self._changing:
            try:
                return self._follow(said)
            except FootballUnavailable as e:
                return {"success": False, "message": str(e)}

    def unfollow(self, name: str) -> Dict:
        """
        Unfollows a team: finds which of the followed teams is meant, and takes the entry which found
        it out of the teams box - or an entry no team was found by, which is there to be cleared.

        Args:
            name: The team, as it was said. Which is meant is the one whose name, full name or entry
                in the box is that, ignoring case and spaces; failing that, the one with every word said
                in one of them. Where that is more than one, none is unfollowed.

        Returns:
            {"success": bool, "message": str}: what to say, and whether an entry was taken out.
        """
        said = name.strip()
        if not _words(said):
            return {"success": False, "message": NO_TEAM}
        with self._changing:
            try:
                return self._unfollow(said)
            except FootballUnavailable as e:
                return {"success": False, "message": str(e)}

    def _unfollow(self, said: str) -> Dict:
        """
        Does unfollow() once it holds the lock, with the words known to have a team in them.

        Args:
            said: The team, as it was said, without spaces round it.

        Returns:
            What unfollow() returns.

        Raises:
            FootballUnavailable: Whose text is what to say, if football can't be got, or the box
                can't be read or written.
        """
        reading = self.read()
        entries = self._entries()
        meant = _meant(said, _followed(reading))
        if not meant:
            return {"success": False, "message": f"You're not following {said}."}
        if len(meant) > 1:
            options = ", ".join(one.called for one in meant[:-1]) + " or " + meant[-1].called
            return {"success": False, "message": f"Which one do you mean, {options}?"}
        chosen = meant[0]
        # Every one which is the entry goes, as one typed twice was meant to be followed once.
        kept = [entry for entry in entries if not _same_entry(entry, chosen.entry)]
        if len(kept) == len(entries):
            return {"success": False, "message": f"I couldn't take {chosen.called} off Home Assistant's list."}
        self._write(_joined(kept))
        if chosen.found:
            return {"success": True, "message": f"You've stopped following {chosen.called}."}
        return {"success": True, "message": f"Taken {chosen.entry} off the list."}

    def _follow(self, said: str) -> Dict:
        """
        Does follow() once it holds the lock, with the words known to have a team in them.

        Args:
            said: The team, as it was said, without spaces round it.

        Returns:
            What follow() returns.

        Raises:
            FootballUnavailable: Whose text is what to say, if football can't be got, or the box
                can't be read or written.
        """
        answer = self.ask("team", said)
        if "team" not in answer and answer.get("unknown"):
            return {"success": False, "message": f"I couldn't find a team called {said}."}
        try:
            team = _field(answer, "team", dict)
            team_id = _field(team, "id", str)
            team_name = _field(team, "name", str).strip()
            full_name = (_field(team, "full_name", str, None) or team_name).strip()
            followed = _field(team, "followed", bool)
            if not team_id or not team_name:
                raise ValueError("a team needs an id and a name")
        except (KeyError, TypeError, ValueError) as e:
            raise _cant_get(f"Home Assistant's answer isn't a team: {e!r}", logging.WARNING) from e
        if followed:
            return {"success": True, "message": f"You're already following {full_name}."}

        entries = self._entries()
        # A follow which timed out may be asked again, and the entry is in the box already then.
        present = any(_same_entry(entry, team_name) for entry in entries)
        added = entries if present else entries + [team_name]
        value = ", ".join(added)
        # The id may have to stand in for the name, so the box has to hold either: one which didn't fit
        # would be dropped by Home Assistant without a word, and leave the wrong team in the box.
        by_id = ", ".join(team_id if _same_entry(entry, team_name) else entry for entry in added)
        if max(len(value), len(by_id)) > BOX_HOLDS:
            return {"success": False, "message": BOX_FULL}
        if not present:
            self._write(value)
        shown = self._wait_for(team_id, team_name)
        if shown == _FOUND_ELSEWHERE:
            # The name finds another team (or none), so the team ESPN's id is for is asked for by that instead.
            logger.info(f"The football teams box entry {team_name!r} found another team, so {team_id} is used")
            self._write(by_id)
            shown = self._wait_for(team_id, team_id)
        if shown == _SHOWN:
            return {"success": True, "message": f"You're now following {full_name}."}
        logger.warning(f"The football sensor hadn't shown {team_name!r} after {WAIT_FOR_SENSOR} seconds")
        return {"success": True, "message": f"I've added {full_name}, but Home Assistant hasn't shown it yet."}

    def _wait_for(self, team_id: str, entry: str) -> str:
        """
        Waits for the sensor to show a team which was just added to the box, a second at a time.

        Reads the sensor itself and not read(), as one which is stale or has an error still shows who is
        followed - and a failure to read it for a moment is waited through, not given up on. It ends by
        the clock as well as by the count of looks, as a slow Home Assistant makes each look take seconds
        and the user waits in silence; a look already under way is let finish, within LOOK_TIMEOUT.

        Args:
            team_id: The team's id, which the sensor should show a team of.
            entry: The entry written, which the sensor says another team or none was found by if it
                isn't the one meant.

        Returns:
            _SHOWN, _FOUND_ELSEWHERE or _NOT_YET, the last once WAIT_FOR_SENSOR seconds have gone.
        """
        deadline = self._now() + WAIT_FOR_SENSOR
        for _ in range(WAIT_FOR_SENSOR):
            if self._now() >= deadline:
                break
            self._sleep(1)
            try:
                attributes = self._home.state(FOOTBALL_TEAMS, timeout=LOOK_TIMEOUT)["attributes"]
            except HOME_FAILURES as e:
                logger.debug(f"The football sensor couldn't be read while waiting for it: {e!r}")
                continue
            teams, unknown = attributes.get("teams"), attributes.get("unknown")
            teams = [team for team in teams if isinstance(team, dict)] if isinstance(teams, list) else []
            unknown = [name for name in unknown if isinstance(name, str)] if isinstance(unknown, list) else []
            if any(team.get("id") == team_id for team in teams):
                return _SHOWN
            asked = [team["asked"] for team in teams if isinstance(team.get("asked"), str)]
            if any(_same_entry(name, entry) for name in asked + unknown):
                return _FOUND_ELSEWHERE
        return _NOT_YET

    def _state(self, entity_id: str) -> Dict:
        """
        Reads one of football's entities from Home Assistant, afresh.

        Args:
            entity_id: The entity, e.g. FOOTBALL_TEAMS.

        Returns:
            It, as HomeAssistant.state gives it.

        Raises:
            FootballUnavailable: Whose text is what to say, if Home Assistant couldn't be asked
                (as HomeAssistant.failure_message says) or hasn't got the entity (NOT_SET_UP).
        """
        try:
            return self._home.state(entity_id)
        except HOME_FAILURES as e:
            if getattr(getattr(e, "response", None), "status_code", None) == 404:
                raise FootballUnavailable(NOT_SET_UP) from e
            raise FootballUnavailable(HomeAssistant.failure_message(e)) from e

    def _entries(self) -> List[str]:
        """
        Reads the teams box, afresh.

        Returns:
            Its entries, which are split on commas, each without the spaces round it - and none empty.

        Raises:
            FootballUnavailable: With CANT_GET if the box is unavailable, which is Home Assistant's word
                for a box it can't read and not a team to keep; or as _state says.
        """
        text = self._state(FOOTBALL_BOX).get("state")
        if not isinstance(text, str) or text in ("unavailable", "unknown"):
            raise _cant_get(f"the teams box is {text!r}", logging.WARNING)
        return [entry.strip() for entry in text.split(",") if entry.strip()]

    def _write(self, value: str) -> None:
        """
        Writes the teams box.

        Args:
            value: The entries, joined by commas.

        Raises:
            FootballUnavailable: With BOX_FULL, writing nothing, if the value is longer than BOX_HOLDS -
                the one guard every write goes through, as Home Assistant would drop it without a word.
                Otherwise whose text is what to say, as HomeAssistant.failure_message has it, if Home
                Assistant couldn't be asked.
        """
        if len(value) > BOX_HOLDS:
            logger.warning(f"The football teams box wasn't written, as {len(value)} characters is more than it holds")
            raise FootballUnavailable(BOX_FULL)
        try:
            self._home.set_football_teams(value)
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"The football teams box couldn't be written: {e!r}")
            raise FootballUnavailable(HomeAssistant.failure_message(e)) from e

    def _reading(self, sensor: Dict, stale_after: float) -> Reading:
        """
        Turns the sensor's state into a reading, if it can be trusted.

        Args:
            sensor: The sensor, as HomeAssistant.state gave it.
            stale_after: Seconds old it may be.

        Raises:
            FootballUnavailable: With CANT_GET, if it is unavailable, has an error or is too old.
            KeyError: If it lacks something it must have.
            TypeError: If anything in it isn't of the type it should be.
            ValueError: If a time or a match in it is the wrong shape.
        """
        state = _field(sensor, "state", str)
        attributes = _field(sensor, "attributes", dict)
        if state in ("unavailable", "unknown"):
            raise _cant_get(f"the sensor is {state}")
        if "error" in attributes:
            raise _cant_get(f"the sensor has an error: {attributes['error']!r}")
        # last_reported is when Home Assistant last heard from the script, even when nothing changed,
        # so it's the age of the reading; older versions only have last_updated.
        reported = _instant(sensor.get("last_reported") or sensor.get("last_updated")).timestamp()
        age = self._now() - reported
        if age > stale_after:
            raise _cant_get(f"the sensor was last written {age:.0f} seconds ago")

        found_teams = _field(attributes, "teams", list)
        teams = [Team.from_home(found) for found in found_teams]
        # "stale": null is as good as left out, as _field has it.
        if _field(attributes, "stale", bool, False) and all(found.get("stale") is None for found in found_teams):
            teams = [replace(team, stale=True) for team in teams]
        unknown = _field(attributes, "unknown", list, [])
        if not all(isinstance(name, str) for name in unknown):
            raise TypeError(f"unknown should be a list of names, not {unknown!r}")
        return Reading(
            teams=tuple(teams),
            unknown=tuple(unknown),
            trimmed=_field(attributes, "trimmed", bool, False),
            reported=reported,
        )
