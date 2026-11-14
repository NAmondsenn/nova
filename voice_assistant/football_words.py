"""
Football Words Module
The text Claude answers football questions from, from decisions 4 and 5 of docs/decisions/0014.

Nova gives Claude plain text built from what Home Assistant returned, not Home Assistant's
JSON: the full names, the kick-off as a local day and time, the ground, the odds and who
won already worked out, so that Claude never has to convert a time zone or guess. This is
part of football (see football.py, which reads what Home Assistant says and knows when it
can't be trusted); only describe asks it anything, through Football. A stale answer, one with
an error or one which found nothing by the name asked for never gets to the said_ functions:
describe deals with each first (see football.CANT_GET). The words say only what the data holds:
no scorers, no result worked out from a score, no phase they don't know.
"""

import logging
import re
from dataclasses import replace
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Set, Tuple

from voice_assistant.clock import spoken_clock_time
# The underscored names are football.py's own checks and error, used here for the answers too.
from voice_assistant.football import (
    CANT_GET, Football, FootballUnavailable, Match, Reading, Team, _cant_get, _field, _is_whole)
from voice_assistant.google_calendar import _ordinal, spoken_day

logger = logging.getLogger(__name__)

# What is said at the end of any answer with a match in play in it, and of no other.
LAG = "Live scores can be up to a minute behind the match."

# What is said when the team or competition asked about isn't a name, as the model can give anything.
NOT_A_NAME = "I didn't catch which team or competition."

# What is said when line-ups are asked for with no team named and none of the followed teams has a match today.
NO_LINEUP_TEAM = "None of the followed teams is playing today. Name a team for its line-up."

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
        What said_match says, with " No odds are listed for it." after it for a match to come with no odds.

    Raises:
        KeyError: If the odds of the match lack a chance.
        TypeError: If any of them isn't a whole number.
    """
    said = said_match(match, now)
    return f"{said} No odds are listed for it." if _to_come(match) and not match.odds else said


def _with_lag(lines: List[str], matches: Sequence[Match]) -> List[str]:
    """Puts LAG as the last line if any of the matches is in play, as it is for no other answer."""
    return lines + [LAG] if any(_in_play(match) for match in matches) else lines


def _other_than(matches: Sequence[Match], current: Optional[Match]) -> List[Match]:
    """The matches without the team's match today, which Home Assistant may list in its fixtures or results too."""
    return [match for match in matches if current is None or match.id != current.id]


def said_followed(reading: Reading, now: datetime, cant_get: Sequence[str] = ()) -> str:
    """
    Says what each followed team is doing: its match today (or in play), its next fixture and its last result.

    Args:
        reading: What the sensor said, which Football.read() gave, with only the teams which
            aren't stale: whether a team is stale is the caller's to decide before this, and
            no line is said of one which is.
        now: The current time, aware and local.
        cant_get: The names of the teams the caller left out of the reading because they are
            stale. Never given with a reading of no teams, which would say "No teams are followed."
            while some are.

    Returns:
        One line for each team, starting with its name, e.g. "Stevenage: no fixtures listed.";
        then, if Home Assistant left some teams out, that only some are shown; then, if cant_get
        has names, that they can't be got ("I can't get South Shields right now."); and LAG as
        the last line if any match is in play. "No teams are followed." with none.

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
    if cant_get:
        lines.append(f"I can't get {_and_list(cant_get)} right now.")
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
        match today and no fixture to come, that Home Assistant's football doesn't seem to cover its
        league, so that only its cup ties show. A match to come has its odds, or that none are listed. LAG
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
        lines.append(f"Home Assistant's football doesn't seem to cover the league {name} play in, so only their cup ties show.")
    return "\n".join(_with_lag(lines, ([current] if current else []) + upcoming + results))


def said_competition(answer: Dict, now: datetime) -> str:
    """
    Says what football_ask answered for a competition's matches over some days.

    Args:
        answer: The answer, with the competition, its matches and the days with a match in them
            as the Contract has them. Its stale, error and unknown are the caller's.
        now: The current time, aware and local.

    Returns:
        A line naming the round of a cup if there is one; then, with matches, each on its own
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
    lines = []
    # The round is said even with no match in the days asked about, as a cup plays on few days and
    # "what round is it at?" is answered by it alone.
    stage = _field(competition, "round", str, None)
    if stage:
        lines.append(f"{_the(name, capital=True)} is at the {stage}.")
    if not matches:
        return "\n".join(lines + [f"No {name} matches in those days."])
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
    at = f", which is at the {stage}" if stage else ""

    if not knockout:
        # A round can be one nobody is knocked out of in a competition which is a knockout one.
        said = (f"Nobody is knocked out of {_the(name)} at the {stage}." if stage
                else f"{_the(name, capital=True)} isn't a knockout competition, so nobody is knocked out of it.")
    elif not teams:
        said = f"No teams are listed as still in {_the(name)}{at}."
    else:
        said = f"Still in {_the(name)}{at}: {_and_list(teams)}."
        if earlier:
            # Names of rounds in the plural ("Quarterfinals") go with "haven't", the rest with "hasn't".
            undrawn = f"The {stage}" if stage else "The next round"
            verb = "haven't" if stage and stage.endswith("s") else "hasn't"
            said += f" {undrawn} {verb} been drawn yet, so these are the winners of the {earlier}."
    return "\n".join(_with_lag([said] + [_asked_line(match, now) for match in matches], matches))


def _names(side: Dict, key: str) -> List[str]:
    """
    Reads a list of players' names from a side of a line-up.

    Args:
        side: The side.
        key: The key of the list: "starters" or "bench".

    Returns:
        The names, in the order Home Assistant gave them.

    Raises:
        KeyError: If the list is missing.
        TypeError: If it isn't a list of names.
    """
    names = _field(side, key, list)
    if not all(isinstance(name, str) and name for name in names):
        raise TypeError(f"{key} should be a list of names, not {names!r}")
    return names


def _possessive(name: str) -> str:
    """A name as it owns something: "Liverpool's", but "Wolves'" - a name ending in s takes the apostrophe alone."""
    return f"{name}'" if name.endswith(("s", "S")) else f"{name}'s"


def _said_side(side: Dict) -> Tuple[Set[str], str]:
    """
    Says one side's line-up, which is only listed once it is out.

    Args:
        side: One of an answer's "sides": team, team_name, formation, starters and bench.

    Returns:
        The names the side is known by (its short name and its full one), so that it can be
        placed in the match; and the words, e.g. "Liverpool's starting eleven (4-2-3-1): Alisson,
        Frimpong and so on. Bench: Mamardashvili and Gomez.", with no brackets where there is
        no formation and "none listed" for an empty bench.

    Raises:
        KeyError: If a key is missing.
        TypeError: If a value isn't of the type it should be.
        ValueError: If the side has no starters, as a side is only listed with a line-up out.
    """
    _dict(side, "a side")
    short = _field(side, "team", str)
    name = _field(side, "team_name", str, None) or short
    formation = _field(side, "formation", str, None)
    starters, bench = _names(side, "starters"), _names(side, "bench")
    if not starters:
        raise ValueError(f"a side is only listed once its line-up is out, but {name} has no starters")
    shape = f" ({formation})" if formation else ""
    bench_said = _and_list(bench) or "none listed"
    return {short, name}, f"{_possessive(name)} starting eleven{shape}: {_and_list(starters)}. Bench: {bench_said}."


def _asked_is_home(match: Match, team: Dict) -> bool:
    """
    Works out which side of a match is the team asked about.

    Args:
        match: The team's match.
        team: The answer's team, for its id.

    Returns:
        Whether it is the home side, by the team's id against the match's.

    Raises:
        KeyError: If the team has no id.
        TypeError: If the id isn't text.
        ValueError: If the id is on neither side of the match, which is then not the team's.
    """
    team_id = _field(team, "id", str)
    if team_id not in (match.home_id, match.away_id):
        raise ValueError(f"{match.home} v {match.away} isn't a match of the team with the id {team_id}")
    return team_id == match.home_id


def _is_home_side(names: Set[str], found: Dict) -> bool:
    """
    Works out which side of a match a line-up is, from the names it is known by.

    Args:
        names: The names _said_side gave: the side's short name and its full one.
        found: The match as Home Assistant gave it, as Match.from_home keeps only the full name
            of a side, and the side is named as the match names it, in either form.

    Returns:
        Whether it is the home side.

    Raises:
        KeyError: If the match lacks a side's name.
        TypeError: If a name isn't text.
        ValueError: If neither side of the match is called by any of them.
    """
    # A name which isn't there is None, which no side is called.
    if names & {_field(found, "home", str), _field(found, "home_name", str, None)}:
        return True
    if names & {_field(found, "away", str), _field(found, "away_name", str, None)}:
        return False
    raise ValueError(f"{sorted(names)} is no side of the match")


def said_lineups(answer: Dict, now: datetime) -> str:
    """
    Says what football_ask answered for a team's line-ups.

    Args:
        answer: The answer, with the team, its match and the sides whose line-up is out as the
            Contract has them. Its stale, error and unknown are the caller's to deal with before this.
        now: The current time, aware and local.

    Returns:
        Lines: the match, as said_match says it; each side whose line-up is out, the team
        asked about first, with its formation, starting eleven and bench; then, for a match
        still to come (and not from BBC Sport, which never has line-ups), which side's line-up
        isn't out yet, or that none are and when they usually come. For any other match - in
        play, over, off or from BBC - that the line-up of a side which isn't listed isn't
        listed, or that no line-ups are. LAG is the last line if the match is in play. With no
        match, only that none is listed for the team.

    Raises:
        KeyError: If a key is missing from the answer, a side or the match.
        TypeError: If a value isn't of the type it should be.
        ValueError: If the match or a side is of the wrong shape, the match isn't the team's (its
            id is on neither side), a side isn't a side of the match, or there are two for one.
    """
    _dict(answer, "an answer")
    team = _field(answer, "team", dict)
    name = _field(team, "full_name", str, None) or _field(team, "name", str)
    # The sides are read before the match is looked for, so that ones of the wrong shape are refused either way.
    sides = [_said_side(side) for side in _field(answer, "sides", list, [])]
    found = _field(answer, "match", dict, None)
    if found is None:
        return f"No match is listed for {name}."
    match = Match.from_home(found)
    asked_is_home = _asked_is_home(match, team)
    listed = [(_is_home_side(names, found), said) for names, said in sides]
    if len({at_home for at_home, _ in listed}) != len(listed):
        raise ValueError(f"there are {len(listed)} line-ups for the two sides of {match.home} v {match.away}")
    # The team asked about comes first, whichever order the sides are in.
    lines = ([said_match(match, now)] + [said for at_home, said in listed if at_home == asked_is_home]
             + [said for at_home, said in listed if at_home != asked_is_home])

    # Line-ups come out before kick-off, so only a match still to come is one they are waited for.
    waiting = _to_come(match) and not match.id.startswith("bbc:")
    if not listed:
        lines.append("Line-ups aren't out yet; they usually come about an hour before kick-off." if waiting
                     else "No line-ups are listed for it.")
    elif len(listed) == 1:
        # The side which isn't listed is named as the match names it, which is its full name where it has one.
        missing = match.away if listed[0][0] else match.home
        lines.append(f"{_possessive(missing)} line-up isn't {'out yet' if waiting else 'listed'}.")
    return "\n".join(_with_lag(lines, [match]))


def _asked_days(given) -> int:
    """
    Reads how many days the model asked for, which may be a number in any form.

    Args:
        given: What it gave: a whole number, one as a float ("2.0") or as text ("5"), or anything.

    Returns:
        The whole number of days, or 1 where it isn't one - a bool, a fraction, a word. Left to
        HomeAssistant.football_ask to bring between 1 and 31.
    """
    if _is_whole(given):
        return given
    if isinstance(given, float) and given.is_integer():
        return int(given)
    if isinstance(given, str) and re.fullmatch(r"[0-9]+", given.strip()):
        return int(given.strip())
    return 1


def _asked_date(given) -> Optional[str]:
    """
    Reads the first day the model asked for.

    Args:
        given: What it gave: a date as YYYY-MM-DD, or anything.

    Returns:
        The day as YYYYMMDD, which is how Home Assistant's command has it - or None if it isn't a
        real day written as YYYY-MM-DD, which is today.
    """
    text = given.strip() if isinstance(given, str) else ""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text.replace("-", "")


def _kind(about: object) -> str:
    """What the model asked about, in lower case without spaces round it - or "" where it isn't text."""
    return about.strip().lower() if isinstance(about, str) else ""


def _question(team: str, competition: str, about: object) -> Tuple[str, str]:
    """
    Picks which question to ask Home Assistant, and of whom.

    Args:
        team: The team asked about, or "" for none.
        competition: The competition asked about, or "" for none. Not both blank.
        about: "matches", "table", "left" or "lineups" - or anything, which is taken for matches.

    Returns:
        The question, one of football_ask's, and the name to ask it of. A team's line-ups are
        asked of the team, whatever competition is given with it, as they are of the team's match.
        Otherwise a competition wins over a team, and a table or who is left is only a question
        of a competition: a team alone is asked about as a team, whatever else is said of it.
        The line-ups of a competition alone are its matches.
    """
    kind = _kind(about)
    if team and kind == "lineups":
        return "lineups", team
    if competition:
        return (kind if kind in ("table", "left") else "competition"), competition
    return "team", team


def _said_answer(what: str, answer: Dict, now: datetime) -> str:
    """
    Puts an answer of Football.ask into words.

    Args:
        what: The question it answers.
        answer: The answer, not stale and with no error, but maybe one which found nothing.
        now: The current time, aware and local.

    Returns:
        What to say, which for an answer which found nothing by the name is that, without the
        said_ functions being used.

    Raises:
        KeyError: If a key is missing from the answer, a match or a table row.
        TypeError: If a value isn't of the type it should be.
        ValueError: If a match or a day is of the wrong shape.
    """
    unknown = answer.get("unknown")
    if unknown:
        if not isinstance(unknown, list) or not all(isinstance(name, str) and name for name in unknown):
            raise TypeError(f"unknown should be a list of names, not {unknown!r}")
        return f"ESPN, where Home Assistant's football comes from, found nothing called {_and_list(unknown)}."
    if what == "table":
        return said_table(answer)
    return {"team": said_team, "competition": said_competition, "left": said_left, "lineups": said_lineups}[what](answer, now)


def _team_playing(reading: Reading, now: datetime) -> Optional[Team]:
    """
    Picks the followed team whose line-up is most likely asked for with no team named.

    Args:
        reading: What the sensor said, which Football.read() gave.
        now: The current time, aware and local, for which day is today.

    Returns:
        The team whose match is in play; else the one whose match is still to come today, the
        earliest first, as its line-up is the one which is about to come out; else the one
        whose match was today and is over, the earliest first; or None where no team has a
        match today. Within each of those, a team whose match is from BBC Sport (its id begins
        "bbc:") comes after one whose isn't, however early its kick-off, as BBC's matches never
        have line-ups: South Shields, first in the box, don't win over Stevenage, whose line-up
        there is to say. With none but BBC's, one of those is picked as before. Only teams which
        aren't stale count, as a stale team's match is an old copy.

    Raises:
        FootballUnavailable: If no team which isn't stale has a match today but one which is stale
            may have, so that nobody playing can't be said.
    """
    fresh = [team for team in reading.teams if not team.stale]
    with_match = [team for team in fresh if team.now is not None]

    def from_bbc(team: Team) -> bool:
        return team.now.id.startswith("bbc:")

    playing = [team for team in with_match if _in_play(team.now)]
    if playing:
        # min takes the first of equals, so the box's order stands between two of the same source.
        return min(playing, key=from_bbc)
    today = [team for team in with_match if team.now.kickoff.astimezone().date() == now.astimezone().date()]
    if today:
        return min(today, key=lambda team: (not _to_come(team.now), from_bbc(team), team.now.kickoff))
    if len(fresh) < len(reading.teams):
        raise _cant_get("a followed team's matches are an old copy", logging.WARNING)
    return None


def describe(football: Football, team=None, competition=None, about=None, date=None, days=None,
             now: Optional[datetime] = None) -> Dict:
    """
    Answers a football question for Claude to reply from: asks for what it needs, and says
    in words what it got, or that it can't be got - so that only what Home Assistant said is
    ever said. What the question holds may be of any type, as it comes from the model.

    Args:
        football: Football, for the sensor of the followed teams and for questions.
        team: The team asked about. With no team and no competition, the followed teams are
            what is said: those which aren't stale in full, and those which are only by name, as
            ones which can't be got.
        competition: The league or cup asked about, which wins over a team, except for line-ups.
        about: "table" or "left" with a competition - its table or who is still in it; "lineups"
            with a team - its line-ups, whatever competition is given with it - or with neither a
            team nor a competition the followed team playing now or today; any other value, its
            matches.
        date: The first day of a competition's matches, as YYYY-MM-DD, or none for today.
        days: How many days from it, or none for one.
        now: The current time, aware and local, which the words are built for. Defaults to the
            time now, so a test is the only one to give it.

    Returns:
        {"success": True, "message": the words} - including where ESPN found nothing by the
        name asked for, which is said, and including an overview which names the followed teams
        it can't get while saying the rest. {"success": False, "message": what to say} where football
        can't be got: CANT_GET, also for any answer of the wrong shape (logged at warning level) and
        for an overview where every followed team is stale;
        or what Football says of Home Assistant not being reached, or not set up; or that the
        team or competition isn't a name.
    """
    if not all(isinstance(given, (str, type(None))) for given in (team, competition)):
        return {"success": False, "message": NOT_A_NAME}
    team, competition = ((given or "").strip() for given in (team, competition))
    if now is None:
        now = datetime.now().astimezone()
    try:
        if not team and not competition and _kind(about) == "lineups":
            # Line-ups asked for with no team are for the followed team playing, in the place of the overview below.
            playing = _team_playing(football.read(), now)
            if playing is None:
                return {"success": True, "message": NO_LINEUP_TEAM}
            # By the entry of the box which found it, the one name Home Assistant is known to find it by.
            team = playing.asked or playing.name
        if not team and not competition:
            reading = football.read()
            fresh = tuple(followed for followed in reading.teams if not followed.stale)
            stale = [followed.name for followed in reading.teams if followed.stale]
            # No line of a stale team is said, as it would pass for a current one: only that it can't be got.
            if stale and not fresh:
                raise _cant_get("a followed team's matches are an old copy", logging.WARNING)
            return {"success": True, "message": said_followed(replace(reading, teams=fresh), now, stale)}
        what, name = _question(team, competition, about)
        answer = football.ask(what, name, _asked_date(date), _asked_days(days))
        return {"success": True, "message": _said_answer(what, answer, now)}
    except FootballUnavailable as e:
        return {"success": False, "message": str(e)}
    except (KeyError, TypeError, ValueError) as e:
        logger.warning(f"What Home Assistant gave for football isn't shaped as expected: {e!r}")
        return {"success": False, "message": CANT_GET}
