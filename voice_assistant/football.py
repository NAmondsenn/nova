"""
Football Module
What football is, from decisions 2 and 5 of docs/decisions/0014.

Home Assistant is the only source of football (see HomeAssistant.state): Nova never
calls ESPN and keeps no list of teams, leagues or abbreviations, so everything here
is read from what Home Assistant's sensor of the followed teams says. A match, a team
or a reading of the sensor is only ever as good as that - when the sensor is down,
old, has an error or isn't shaped as expected, football can't be got, and the one
sentence for that is said instead of any fixture, score or result.
"""

import logging
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from voice_assistant.batteries import HOME_FAILURES
from voice_assistant.home_assistant import FOOTBALL_TEAMS, HomeAssistant

logger = logging.getLogger(__name__)

# What is said whenever football can't be read for any reason but Home Assistant being down.
CANT_GET = "I can't get football right now."
NOT_SET_UP = "Football isn't set up in Home Assistant."

# Seconds old the sensor may be and still count as current. Home Assistant's script runs
# every minute, so a reading five minutes old means it has stopped - which its own "stale"
# flag can't say, as that only reports ESPN's old copies, and not the script not running at all.
STALE_AFTER = 300

# The states a match is in, as the Contract has them: not started, in play, over.
_MATCH_STATES = ("pre", "in", "post")

# The default of a key which has none, so it must be there. None can't be this, as most keys
# which may be left out are given None.
_REQUIRED = object()


class FootballUnavailable(Exception):
    """Football can't be read just now. str() of it is what to say."""


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


def _cant_get(why: str) -> FootballUnavailable:
    """
    Makes the error for football which can't be got.

    Args:
        why: The reason, which is logged at debug level only: the watcher reads every 30 seconds.

    Returns:
        The error, whose text is CANT_GET whatever the reason.
    """
    logger.debug(f"Football can't be got: {why}")
    return FootballUnavailable(CANT_GET)


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
            couldn't be asked just now.
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
    """

    teams: Tuple[Team, ...]
    unknown: Tuple[str, ...]
    trimmed: bool


class Football:
    """
    Reads the followed teams from Home Assistant, and knows when it can't be trusted.

    The watcher's thread reads every 30 seconds while a session's reads when asked, so the
    names kept from the last good read are guarded by a lock.
    """

    def __init__(self, home: HomeAssistant, settings: Callable[[], Dict], now: Callable[[], float] = time.time):
        """
        Args:
            home: Home Assistant, for the sensor.
            settings: Gives the football section of the settings as they are now, read at
                each reading so that a change on the settings page is used straight away.
                "stale_after" is how many seconds old a reading may be; without it, STALE_AFTER.
            now: Gives the current epoch time.
        """
        self._home = home
        self._settings = settings
        self._now = now
        self._lock = threading.Lock()
        self._names: List[str] = []

    def read(self) -> Reading:
        """
        Reads the sensor of the followed teams, afresh.

        Returns:
            The teams. A team is stale when Home Assistant says it is, or when the sensor is
            stale and no team says so, as a Home Assistant which doesn't mark teams can't tell
            whose data is old, so all of it is.

        Raises:
            FootballUnavailable: Whose text is what to say, if Home Assistant couldn't be asked
                (as HomeAssistant.failure_message says), has no football sensor (NOT_SET_UP), or
                the sensor is unavailable, has an error, was last written more than the
                stale_after setting ago, or isn't shaped as the Contract has it (CANT_GET).
        """
        try:
            sensor = self._home.state(FOOTBALL_TEAMS)
        except HOME_FAILURES as e:
            if getattr(getattr(e, "response", None), "status_code", None) == 404:
                raise FootballUnavailable(NOT_SET_UP) from e
            raise FootballUnavailable(HomeAssistant.failure_message(e)) from e
        stale_after = float(self._settings().get("stale_after", STALE_AFTER))
        try:
            reading = self._reading(sensor, stale_after)
        except (KeyError, TypeError, ValueError) as e:
            # Not logged above debug: the watcher reads every 30 seconds, and says it once itself.
            logger.debug(f"The football sensor isn't shaped as expected: {e!r}")
            raise FootballUnavailable(CANT_GET) from e
        with self._lock:
            self._names = [team.name for team in reading.teams]
        return reading

    def names(self) -> List[str]:
        """
        Returns the followed teams' names from the last good read, asking nothing - so that
        Claude can be told them without a call to Home Assistant, and a read which failed
        doesn't empty them. Empty before any read.
        """
        with self._lock:
            return list(self._names)

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
        reported = sensor.get("last_reported") or sensor.get("last_updated")
        age = self._now() - _instant(reported).timestamp()
        if age > stale_after:
            raise _cant_get(f"the sensor was last written {age:.0f} seconds ago")

        teams = [Team.from_home(found) for found in _field(attributes, "teams", list)]
        if _field(attributes, "stale", bool, False) and not any(team.stale for team in teams):
            teams = [replace(team, stale=True) for team in teams]
        unknown = _field(attributes, "unknown", list, [])
        if not all(isinstance(name, str) for name in unknown):
            raise TypeError(f"unknown should be a list of names, not {unknown!r}")
        return Reading(
            teams=tuple(teams),
            unknown=tuple(unknown),
            trimmed=_field(attributes, "trimmed", bool, False),
        )
