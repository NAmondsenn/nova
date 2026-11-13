"""
Football Module
What football is, from decisions 2 and 5 of docs/decisions/0014, and the words Claude is
given to answer from, from decisions 4 and 5.

Home Assistant is the only source of football (see HomeAssistant.state): Nova never
calls ESPN and keeps no list of teams, leagues or abbreviations, so everything here
is read from what Home Assistant's sensor of the followed teams says. A match, a team
or a reading of the sensor is only ever as good as that - when the sensor is down,
old, has an error or isn't shaped as expected, football can't be got, and the one
sentence for that is said instead of any fixture, score or result.

The said_ functions at the end turn what Home Assistant returned into plain text, with
the full names, the kick-off as a local day and time, the ground, the odds and who won
already worked out, so that Claude never has to convert a time zone or guess. They say
only what the data holds: no scorers, no result worked out from a score, no phase they
don't know.
"""

import logging
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from voice_assistant.batteries import HOME_FAILURES
from voice_assistant.clock import spoken_clock_time
from voice_assistant.google_calendar import _ordinal, spoken_day
from voice_assistant.home_assistant import FOOTBALL_TEAMS, HomeAssistant

logger = logging.getLogger(__name__)

# What is said whenever football can't be read for any reason but Home Assistant being down.
CANT_GET = "I can't get football right now."
NOT_SET_UP = "Football isn't set up in Home Assistant."

# Seconds old the sensor may be and still count as current. Home Assistant's script runs
# every minute, so a reading five minutes old means it has stopped - which its own "stale"
# flag can't say, as that only reports ESPN's old copies, and not the script not running at all.
STALE_AFTER = 300

# What is said at the end of any answer with a match in play in it, and of no other.
LAG = "Live scores can be up to a minute behind the match."

# The states a match is in, as the Contract has them: not started, in play, over.
_MATCH_STATES = ("pre", "in", "post")

# How each phase of a match which has started is said. "off" (postponed, abandoned, cancelled)
# is said another way, and a phase which isn't here is one Home Assistant's side has added since:
# it is left out, as what it means isn't known.
_PHASE_WORDS = {
    "first_half": "first half", "half_time": "half time", "second_half": "second half",
    "extra_time": "extra time", "penalties": "penalties", "full_time": "full time",
}
_OFF = "off"

# Only in these phases is ESPN's clock a minute ("67'"). In the others it is a word for the card
# ("HT"), which isn't said as the phase is.
_MINUTE_PHASES = ("first_half", "second_half", "extra_time")

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


# The words Claude is given. None of these asks Home Assistant for anything: they turn what it
# returned into text. A stale answer, one with an error or one which found nothing by the name
# asked for is the caller's to deal with before it gets here (see CANT_GET).

def _dict(found, what: str) -> Dict:
    """
    Checks that something Home Assistant gave is a dict, as it is read key by key.

    Args:
        found: The value.
        what: What it is, for the error.

    Returns:
        The same value.

    Raises:
        TypeError: If it isn't a dict.
    """
    if not isinstance(found, dict):
        raise TypeError(f"{what} should be a dict, not {found!r}")
    return found


def _matches(found: Dict, key: str) -> List[Match]:
    """
    Reads a list of matches from an answer.

    Args:
        found: The answer.
        key: The key of the list; a list left out is an empty one.

    Returns:
        The matches, in the order Home Assistant gave them.

    Raises:
        TypeError: If the list isn't one, or a match in it isn't of the shape it should be.
        KeyError: If a match lacks a key it must have.
        ValueError: If a match in it has a wrong state, winner, shoot-out or kick-off.
    """
    return [Match.from_home(one) for one in _field(found, key, list, [])]


def _phase(match: Match) -> Optional[str]:
    """A match's phase if it is one of the Contract's, or None - which is what an unknown one is."""
    return match.phase if match.phase in _PHASE_WORDS or match.phase == _OFF else None


def _is_off(match: Match) -> bool:
    """Whether a match is postponed, abandoned or cancelled, whatever its state."""
    return _phase(match) == _OFF


def _in_play(match: Match) -> bool:
    """Whether a match is being played just now."""
    return match.state == "in" and not _is_off(match)


def _to_come(match: Match) -> bool:
    """Whether a match is still to be played, which is the only time its odds mean anything."""
    return match.state == "pre" and not _is_off(match)


def _on(day: date, now: datetime) -> str:
    """A day as it's said of a match: "today", "tomorrow", or "on Sunday the 22nd of November"."""
    said = spoken_day(day, now.astimezone().date())
    return said if said in ("today", "tomorrow") else f"on {said}"


def _the(name: str, capital: bool = False) -> str:
    """A competition's name with "the" in front, e.g. "the Carabao Cup", unless it has one already."""
    said = name if name.lower().startswith("the ") else f"the {name}"
    return said[0].upper() + said[1:] if capital else said


def _and_list(names: Sequence[str]) -> str:
    """Names as a list is said: "Arsenal, Liverpool and Everton"."""
    return ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else "".join(names)


def _points(points: int) -> str:
    """Points as they're said, e.g. "18 points" but "1 point"."""
    return f"{points} point" if points == 1 else f"{points} points"


def _odds(match: Match) -> str:
    """
    Says a match's odds, which are bookmakers' prices already turned into whole percentages.

    Args:
        match: A match with odds.

    Returns:
        e.g. "Bookmakers' odds from DraftKings, as percentages: Liverpool 55, draw 23, Manchester United 22."

    Raises:
        KeyError: If the odds lack the home side, the draw or the away side.
        TypeError: If any of those isn't a whole number, or the bookmaker isn't text.
    """
    chances = [(match.home, _field(match.odds, "home", int)), ("draw", _field(match.odds, "draw", int)),
               (match.away, _field(match.odds, "away", int))]
    source = _field(match.odds, "source", str, None)
    given = f" from {source}" if source else ""
    return f"Bookmakers' odds{given}, as percentages: " + ", ".join(f"{name} {chance}" for name, chance in chances) + "."


def _outcome(match: Match) -> str:
    """
    Says who won a match which is over, only as Home Assistant says it: a winner it didn't name
    isn't worked out from the score, though equal scores with none are a draw.

    Args:
        match: A match which is over.

    Returns:
        A sentence with a space in front, e.g. " Liverpool won.", or "" with nothing to say.
    """
    winner = {"home": match.home, "away": match.away}.get(match.winner)
    if match.shootout:
        home_goals, away_goals = match.shootout
        if winner is None:
            return f" The penalty shootout finished {match.home} {home_goals}, {match.away} {away_goals}."
        # The winner's penalties come first, however the sides were listed.
        won, lost = (home_goals, away_goals) if match.winner == "home" else (away_goals, home_goals)
        return f" {winner} won {won}-{lost} on penalties."
    if winner:
        return f" {winner} won."
    if match.home_score is not None and match.home_score == match.away_score:
        return " It was a draw."
    return ""


def said_match(match: Match, now: datetime) -> str:
    """
    Says one match, whatever state it is in.

    Args:
        match: The match.
        now: The current time, aware and local: the kick-off is said as a day and time on the
            machine's own clock, and a day as "today" or "tomorrow" if it is.

    Returns:
        e.g. "Liverpool v Manchester United, Premier League, on Sunday the 22nd of November at
        4:30 PM, at Anfield in Liverpool. Bookmakers' odds from DraftKings, as percentages:
        Liverpool 55, draw 23, Manchester United 22." to come, "Notts County 0, Stevenage 1, EFL
        League One, in play, second half, 67'." in play, "Crystal Palace 1, Liverpool 3, Premier
        League, on Sunday the 8th of November. Liverpool won." when over, or "Notts County v
        Stevenage, EFL League One, on Saturday the 21st of November: off (Postponed)." The
        odds are said only before kick-off, and the ground wherever there is one.

    Raises:
        KeyError: If the odds of a match to come lack a chance.
        TypeError: If any of them isn't a whole number.
    """
    local = match.kickoff.astimezone()
    day = _on(local.date(), now)
    if _is_off(match):
        off = f"{match.home} v {match.away}, {match.league}, {day}: off"
        return f"{off} ({match.clock})." if match.clock else f"{off}."
    where = f", at {match.venue}" if match.venue else ""
    if _to_come(match):
        text = f"{match.home} v {match.away}, {match.league}, {day} at {spoken_clock_time(local.hour, local.minute)}{where}."
        return f"{text} {_odds(match)}" if match.odds else text
    scored = match.home_score is not None and match.away_score is not None
    sides = f"{match.home} {match.home_score}, {match.away} {match.away_score}" if scored else f"{match.home} v {match.away}"
    unscored = "" if scored else ", no score listed"
    if match.state == "in":
        phase = _phase(match)
        said = f", {_PHASE_WORDS[phase]}" if phase else ""
        minute = f", {match.clock}" if phase in _MINUTE_PHASES and match.clock else ""
        return f"{sides}, {match.league}{unscored}, in play{said}{minute}{where}."
    after = ", after extra time" if match.extra_time else ""
    return f"{sides}, {match.league}{unscored}, {day}{after}{where}.{_outcome(match)}"


def _asked_line(match: Match, now: datetime) -> str:
    """
    Says a match from an answer to a question, which is where odds are given: with none listed
    for a match to come, that is said, so that Claude doesn't guess at any.

    Args:
        match: The match.
        now: The current time, aware and local.

    Returns:
        What said_match says, with " ESPN lists no odds for it." after it for a match to come with no odds.

    Raises:
        KeyError: If the odds of the match lack a chance.
        TypeError: If any of them isn't a whole number.
    """
    said = said_match(match, now)
    return f"{said} ESPN lists no odds for it." if _to_come(match) and not match.odds else said


def _with_lag(lines: List[str], matches: Sequence[Match]) -> List[str]:
    """Puts LAG as the last line if any of the matches is in play, as it is for no other answer."""
    return lines + [LAG] if any(_in_play(match) for match in matches) else lines


def _other_than(matches: Sequence[Match], current: Optional[Match]) -> List[Match]:
    """The matches without the team's match today, which Home Assistant may list in its fixtures or results too."""
    return [match for match in matches if current is None or match.id != current.id]


def said_followed(reading: Reading, now: datetime) -> str:
    """
    Says what each followed team is doing: its match today (or in play), its next fixture and its last result.

    Args:
        reading: What the sensor said, which Football.read() gave. Whether its teams are stale
            is the caller's to decide before this.
        now: The current time, aware and local.

    Returns:
        One line for each team, starting with its name, e.g. "Stevenage: no fixtures listed.";
        LAG as a last line if any match is in play; and, before that, that only some of the
        teams are shown if Home Assistant left some out. "No teams are followed." with none.

    Raises:
        KeyError: If the odds of a match lack a chance.
        TypeError: If any of them isn't a whole number.
    """
    if not reading.teams:
        return "No teams are followed."
    lines: List[str] = []
    shown: List[Match] = []
    for team in reading.teams:
        parts: List[str] = []
        later = _other_than(team.next, team.now)[:1]
        past = _other_than(team.last, team.now)[:1]
        if team.now:
            parts.append(("Playing now: " if _in_play(team.now) else "Today: ") + said_match(team.now, now))
        if later:
            parts.append("Next: " + said_match(later[0], now))
        elif team.now is None:
            parts.append("no fixtures listed.")
        if past:
            parts.append("Last: " + said_match(past[0], now))
        shown += ([team.now] if team.now else []) + later + past
        lines.append(f"{team.name}: " + " ".join(parts))
    if reading.trimmed:
        lines.append("Home Assistant is showing only some of the followed teams.")
    return "\n".join(_with_lag(lines, shown))


def _standing(row: Dict) -> str:
    """
    Says a team's place in one table.

    Args:
        row: One of an answer's "tables": league, group, rank, of, played, won, drawn, lost, gd and points.

    Returns:
        e.g. "Premier League: 4th of 20, played 10, won 5, drawn 3, lost 2, goal difference 4, 18 points."

    Raises:
        KeyError: If the row lacks a key.
        TypeError: If a value isn't of the type it should be.
    """
    _dict(row, "a table row")
    league = _field(row, "league", str)
    group = _field(row, "group", str, "")
    heading = f"{league}, {group}" if group and group != league else league
    rank, of, points = _field(row, "rank", int), _field(row, "of", int), _field(row, "points", int)
    played, won, drawn, lost = (_field(row, key, int) for key in ("played", "won", "drawn", "lost"))
    return (f"{heading}: {_ordinal(rank)} of {of}, played {played}, won {won}, drawn {drawn}, lost {lost}, "
            f"goal difference {_field(row, 'gd', int)}, {_points(points)}.")


def said_team(answer: Dict, now: datetime) -> str:
    """
    Says what football_ask answered for a team.

    Args:
        answer: The answer, with the team, its next fixtures and last results, and its tables
            as the Contract has them, and its match in play or today if it has one. Its
            stale, error and unknown are the caller's to deal with before this.
        now: The current time, aware and local.

    Returns:
        Lines: the team's full name, its match today, its next fixtures, its last results and
        its place in each table, with what there is none of said so. Where it has no table, no
        match today and no fixture to come, that ESPN doesn't seem to cover its league, so that
        only its cup ties show. A match to come has its odds, or that ESPN lists none. LAG
        is the last line if a match is in play.

    Raises:
        KeyError: If a key is missing from the answer, a match or a table row.
        TypeError: If a value isn't of the type it should be.
        ValueError: If a match is of the wrong shape.
    """
    _dict(answer, "an answer")
    team = _field(answer, "team", dict)
    name = _field(team, "full_name", str, None) or _field(team, "name", str)
    found = _field(answer, "now", dict, None)
    current = Match.from_home(found) if found is not None else None
    upcoming = _other_than(_matches(answer, "next"), current)
    results = _other_than(_matches(answer, "last"), current)
    tables = _field(answer, "tables", list, [])

    lines = [f"{name}:"]
    if current:
        lines.append(("Playing now: " if _in_play(current) else "Today: ") + _asked_line(current, now))
    lines += [("Next: " if place == 0 else "Later: ") + _asked_line(match, now) for place, match in enumerate(upcoming)]
    if not current and not upcoming:
        lines.append("No fixtures listed.")
    lines += [("Last: " if place == 0 else "Before that: ") + said_match(match, now) for place, match in enumerate(results)]
    if not results:
        lines.append("No results listed.")
    lines += [_standing(row) for row in tables]
    if not tables:
        lines.append("No league table listed.")
    if not current and not upcoming and not tables:
        lines.append(f"ESPN, where Home Assistant's football comes from, doesn't seem to cover the league {name} play in, "
                     "so only their cup ties show.")
    return "\n".join(_with_lag(lines, ([current] if current else []) + upcoming + results))


def said_competition(answer: Dict, now: datetime) -> str:
    """
    Says what football_ask answered for a competition's matches over some days.

    Args:
        answer: The answer, with the competition, its matches and the days with a match in them
            as the Contract has them. Its stale, error and unknown are the caller's.
        now: The current time, aware and local.

    Returns:
        With matches: a line naming the round of a cup if there is one, each match on its own
        line, the days with matches (all of them, even where some matches were left out), that
        only some of the matches are listed if Home Assistant trimmed them, and LAG if a match
        is in play. With none, "No UEFA Champions League matches in those days."

    Raises:
        KeyError: If a key is missing from the answer or a match.
        TypeError: If a value isn't of the type it should be.
        ValueError: If a match is of the wrong shape, or a day isn't a YYYYMMDD one.
    """
    _dict(answer, "an answer")
    competition = _field(answer, "competition", dict)
    name = _field(competition, "name", str)
    matches = _matches(answer, "matches")
    if not matches:
        return f"No {name} matches in those days."
    lines = []
    stage = _field(competition, "round", str, None)
    if stage:
        lines.append(f"{_the(name, capital=True)} is at the {stage}.")
    lines += [_asked_line(match, now) for match in matches]
    today = now.astimezone().date()
    days = [spoken_day(datetime.strptime(day, "%Y%m%d").date(), today) for day in _field(answer, "match_days", list, [])]
    if days:
        lines.append("Days with matches: " + ", ".join(days) + ".")
    if _field(answer, "trimmed", bool, False):
        lines.append("Only some of the matches are listed.")
    return "\n".join(_with_lag(lines, matches))


def said_table(answer: Dict) -> str:
    """
    Says what football_ask answered for a competition's table.

    Args:
        answer: The answer, with the competition and its groups, each with its rows, as the
            Contract has them. Its stale, error and unknown are the caller's.

    Returns:
        Each group under its name (the competition's, followed by the group's where it has
        one), with a row for each team, e.g. "1. Arsenal, played 10, 27 points, goal difference
        10." Or that no table is listed, with none.

    Raises:
        KeyError: If a key is missing from the answer or a row.
        TypeError: If a value isn't of the type it should be.
    """
    _dict(answer, "an answer")
    competition = _field(answer, "competition", dict)
    name = _field(competition, "name", str)
    lines: List[str] = []
    for group in _field(answer, "groups", list):
        _dict(group, "a group")
        rows = _field(group, "rows", list)
        if not rows:
            continue
        title = _field(group, "name", str, "")
        lines.append(f"{name}, {title}:" if title and title != name else f"{name}:")
        for row in rows:
            _dict(row, "a table row")
            team = _field(row, "team_name", str, None) or _field(row, "team", str)
            lines.append(f"{_field(row, 'rank', int)}. {team}, played {_field(row, 'played', int)}, "
                         f"{_points(_field(row, 'points', int))}, goal difference {_field(row, 'gd', int)}.")
    return "\n".join(lines) if lines else f"No table is listed for {_the(name)}."


def said_left(answer: Dict, now: datetime) -> str:
    """
    Says what football_ask answered about who is still in a competition.

    Args:
        answer: The answer, with the competition, whether it is a knockout, and its teams
            and matches and the round the teams are the winners of, as the Contract has
            them. Its stale, error and unknown are the caller's.
        now: The current time, aware and local.

    Returns:
        e.g. "Still in the Carabao Cup, which is at the Quarterfinals: Arsenal, Liverpool and
        Everton.", with that the round hasn't been drawn yet and these are the winners of the
        one before where Home Assistant says so; or that nobody is knocked out of it. The matches
        come after, each on its own line, and LAG last if one is in play.

    Raises:
        KeyError: If a key is missing from the answer or a match.
        TypeError: If a value isn't of the type it should be.
        ValueError: If a match is of the wrong shape.
    """
    _dict(answer, "an answer")
    competition = _field(answer, "competition", dict)
    name = _field(competition, "name", str)
    stage = _field(competition, "round", str, None) or None
    knockout = _field(answer, "knockout", bool)
    teams = _field(answer, "teams", list, [])
    if not all(isinstance(team, str) for team in teams):
        raise TypeError(f"teams should be a list of names, not {teams!r}")
    earlier = _field(answer, "from_round", str, None)
    matches = _matches(answer, "matches")

    if not knockout:
        # A round can be one nobody is knocked out of in a competition which is a knockout one.
        said = (f"Nobody is knocked out of {_the(name)} at the {stage}." if stage
                else f"{_the(name, capital=True)} isn't a knockout competition, so nobody is knocked out of it.")
    elif not teams:
        said = f"No teams are listed as still in {_the(name)}."
    else:
        at = f", which is at the {stage}" if stage else ""
        said = f"Still in {_the(name)}{at}: {_and_list(teams)}."
        if earlier:
            # Names of rounds in the plural ("Quarterfinals") go with "haven't", the rest with "hasn't".
            undrawn = f"The {stage}" if stage else "The next round"
            verb = "haven't" if stage and stage.endswith("s") else "hasn't"
            said += f" {undrawn} {verb} been drawn yet, so these are the winners of the {earlier}."
    return "\n".join(_with_lag([said] + [_asked_line(match, now) for match in matches], matches))
