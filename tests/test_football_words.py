"""Tests for the words Claude is given to answer football questions from."""

import logging
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

from voice_assistant import football_words
from voice_assistant.football import Football, Match, Reading, Team
from voice_assistant.football_words import (
    LAG, NO_LINEUP_TEAM, describe, said_competition, said_followed, said_left, said_lineups, said_match, said_table,
    said_team)
from voice_assistant.home_assistant import FOOTBALL_TEAMS

# An instant well away from any change of clock, so that a test passes in whichever time zone it runs.
NOW = 1_800_000_000.0


def _iso(epoch):
    """An epoch time as Home Assistant writes one, e.g. "2026-11-13T02:42:14.233328+00:00"."""
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _found_match(**fields):
    """A match as the Contract has it, with only the keys it must have unless a test gives more."""
    found = {
        "id": "1", "kickoff": _iso(NOW + 3600), "state": "pre", "home": "Liverpool", "away": "Arsenal",
        "home_id": "364", "away_id": "359", "league": "Premier League", "slug": "eng.1",
    }
    found.update(fields)
    return found


def _team(team_id, name, **fields):
    """A followed team as the Contract has it, with no match and no marks unless a test gives them."""
    found = {"id": team_id, "name": name, "next": [], "last": []}
    found.update(fields)
    return found


@pytest.fixture
def london(monkeypatch):
    """Runs a test on UK time, whatever the machine's own time zone."""
    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _m(**changes):
    """A match built from the Contract's keys by Match.from_home, as Home Assistant's answers are read."""
    return Match.from_home(_found_match(**changes))


def _reading(*teams, trimmed=False):
    """A reading of the sensor with these teams, given as the Contract's dicts."""
    return Reading(teams=tuple(Team.from_home(team) for team in teams), unknown=(), trimmed=trimmed)


def _team_answer(**fields):
    """A football_ask answer for a team, as the Contract has it, with nothing to say unless a test gives it."""
    answer = {"team": {"id": "364", "name": "Liverpool", "followed": True}, "next": [], "last": [], "tables": [], "stale": False}
    answer.update(fields)
    return answer


def _competition_answer(matches, days=(), **fields):
    """A football_ask answer for a competition's matches, as the Contract has it."""
    answer = {"competition": {"slug": "uefa.champions", "name": "UEFA Champions League"}, "from": "20261124", "days": 2,
              "matches": matches, "match_days": list(days), "stale": False}
    answer.update(fields)
    return answer


def _left_answer(name="Carabao Cup", **fields):
    """A football_ask answer for who is left in a competition, as the Contract has it."""
    answer = {"competition": {"slug": "eng.league_cup", "name": name}, "knockout": True, "stale": False}
    answer.update(fields)
    return answer


def _lineups_answer(**fields):
    """A football_ask answer for a team's line-ups, as the Contract has it, with no match unless a test gives one."""
    return {"team": {"id": "364", "name": "Liverpool", "followed": True}, "stale": False, **fields}


# Match days used below, in the same week as 13 November 2026 (a Friday) or a few weeks off.
LIVE = {"id": "9", "state": "in", "phase": "second_half", "clock": "67'", "home": "Liverpool", "away": "Man Utd",
        "away_name": "Manchester United", "away_id": "360", "home_score": 2, "away_score": 1}
SUNDAY_AWAY = {"id": "10", "kickoff": "2026-11-22T16:30:00+00:00", "home": "Arsenal", "home_id": "359", "away": "Liverpool",
               "away_id": "364"}
PALACE_LOSS = {"id": "8", "state": "post", "phase": "full_time", "clock": "FT", "kickoff": "2026-11-08T16:30:00+00:00",
               "home": "Crystal Palace", "home_id": "384", "away": "Liverpool", "away_id": "364", "home_score": 1, "away_score": 3,
               "winner": "away"}

AT = datetime(2026, 11, 13, 12, 0)
LIVERPOOL_SIDE = {"team": "Liverpool", "formation": "4-2-3-1", "starters": ["Alisson", "Frimpong", "Konate", "Van Dijk",
                  "Kerkez", "Gravenberch", "Mac Allister", "Salah", "Szoboszlai", "Gakpo", "Isak"], "bench": ["Mamardashvili", "Gomez"]}
ARSENAL_SIDE = {"team": "Arsenal", "starters": ["Raya", "White", "Saliba", "Gabriel", "Calafiori", "Rice", "Zubimendi",
                "Odegaard", "Saka", "Martinelli", "Gyokeres"], "bench": []}


def test_a_match_to_come(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    match = _m(kickoff="2026-11-22T16:30:00+00:00", state="pre", home="Liverpool", away="Man Utd", away_name="Manchester United",
               league="Premier League", venue={"name": "Anfield", "city": "Liverpool"},
               odds={"home": 55, "draw": 23, "away": 22, "source": "DraftKings"})
    assert said_match(match, AT) == (
        "Liverpool v Manchester United, Premier League, on Sunday the 22nd of November at 4:30 PM, at Anfield in Liverpool. "
        "Bookmakers' odds from DraftKings, as percentages: Liverpool 55, draw 23, Manchester United 22.")


def test_a_match_to_come_with_a_ground_but_no_city_or_odds(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    match = _m(kickoff="2026-11-22T16:30:00+00:00", venue={"name": "Anfield"})
    assert said_match(match, AT) == "Liverpool v Arsenal, Premier League, on Sunday the 22nd of November at 4:30 PM, at Anfield."
    # With no bookmaker named, the odds are still said, but not as from one.
    match = _m(kickoff="2026-11-22T16:30:00+00:00", odds={"home": 50, "draw": 30, "away": 20})
    assert said_match(match, AT) == (
        "Liverpool v Arsenal, Premier League, on Sunday the 22nd of November at 4:30 PM. "
        "Bookmakers' odds, as percentages: Liverpool 50, draw 30, Arsenal 20.")


def test_matches_today_and_tomorrow(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    assert said_match(_m(kickoff="2026-11-13T19:45:00+00:00"), AT) == "Liverpool v Arsenal, Premier League, today at 7:45 PM."
    assert said_match(_m(kickoff="2026-11-14T15:00:00+00:00"), AT) == "Liverpool v Arsenal, Premier League, tomorrow at 3:00 PM."
    # Midday is said as noon, as the clock says it.
    assert said_match(_m(kickoff="2026-11-13T12:00:00+00:00"), AT) == "Liverpool v Arsenal, Premier League, today at noon."


def test_kick_off_times_are_local(london):
    at = datetime(2026, 10, 20, 12, 0).astimezone()
    # British Summer Time ends on the night of 24 October: the same 12:30 on the clock, an hour apart in UTC.
    assert said_match(_m(kickoff="2026-10-24T11:30:00+00:00"), at) == "Liverpool v Arsenal, Premier League, on Saturday at 12:30 PM."
    assert said_match(_m(kickoff="2026-10-25T12:30:00+00:00"), at) == "Liverpool v Arsenal, Premier League, on Sunday at 12:30 PM."
    # Late in the evening in UTC is already tomorrow in the summer.
    at = datetime(2026, 6, 13, 12, 0).astimezone()
    assert said_match(_m(kickoff="2026-06-13T23:30:00+00:00"), at) == "Liverpool v Arsenal, Premier League, tomorrow at 12:30 AM."


@pytest.mark.parametrize("phase, clock, tail", [
    ("first_half", "23'", "in play, first half, 23'."),
    ("half_time", "HT", "in play, half time."),
    ("second_half", "67'", "in play, second half, 67'."),
    ("extra_time", "105'", "in play, extra time, 105'."),
    ("penalties", "120'", "in play, penalties."),
    (None, "67'", "in play."),
    # A phase Home Assistant's side may add one day is never put into words, nor is a clock which may belong to it.
    ("added_time", "67'", "in play."),
])
def test_a_match_in_play(london, phase, clock, tail):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    match = _m(state="in", phase=phase, clock=clock, home="Notts County", away="Stevenage", home_score=0, away_score=1,
               league="EFL League One")
    assert said_match(match, AT) == f"Notts County 0, Stevenage 1, EFL League One, {tail}"


def test_a_match_in_play_says_the_ground_but_not_the_odds(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    match = _m(state="in", phase="second_half", clock="67'", home_score=0, away_score=1, venue={"name": "Meadow Lane", "city": "Nottingham"},
               odds={"home": 55, "draw": 23, "away": 22, "source": "DraftKings"})
    assert said_match(match, AT) == "Liverpool 0, Arsenal 1, Premier League, in play, second half, 67', at Meadow Lane in Nottingham."


def test_results(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    assert said_match(_m(**PALACE_LOSS), AT) == (
        "Crystal Palace 1, Liverpool 3, Premier League, on Sunday the 8th of November. Liverpool won.")
    assert said_match(_m(**{**PALACE_LOSS, "home_score": 2, "away_score": 2, "winner": None}), AT) == (
        "Crystal Palace 2, Liverpool 2, Premier League, on Sunday the 8th of November. It was a draw.")
    # The side with the most penalties is said first whichever side it was.
    shootout = {"state": "post", "kickoff": "2026-11-10T19:00:00+00:00", "home": "Stevenage", "away": "Fulham U21",
                "home_score": 2, "away_score": 2, "league": "EFL Trophy"}
    assert said_match(_m(**shootout, winner="home", shootout=[4, 3]), AT) == (
        "Stevenage 2, Fulham U21 2, EFL Trophy, on Tuesday the 10th of November. Stevenage won 4-3 on penalties.")
    assert said_match(_m(**shootout, winner="away", shootout=[3, 4]), AT) == (
        "Stevenage 2, Fulham U21 2, EFL Trophy, on Tuesday the 10th of November. Fulham U21 won 4-3 on penalties.")
    extra = {"state": "post", "kickoff": "2026-11-10T19:00:00+00:00", "home": "Plymouth Argyle", "away": "Stevenage",
             "home_score": 3, "away_score": 2, "league": "FA Cup", "extra_time": True}
    assert said_match(_m(**extra, winner="home"), AT) == (
        "Plymouth Argyle 3, Stevenage 2, FA Cup, on Tuesday the 10th of November, after extra time. Plymouth Argyle won.")
    # Extra time and then penalties are both said.
    assert said_match(_m(**{**extra, "home_score": 2}, winner="away", shootout=[2, 4]), AT) == (
        "Plymouth Argyle 2, Stevenage 2, FA Cup, on Tuesday the 10th of November, after extra time. Stevenage won 4-2 on penalties.")


def test_a_result_is_only_said_from_what_home_assistant_gave(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    # No winner is given and the scores differ: who won is not worked out here.
    assert said_match(_m(**{**PALACE_LOSS, "winner": None}), AT) == "Crystal Palace 1, Liverpool 3, Premier League, on Sunday the 8th of November."
    # Penalties with nobody given as the winner are said as they came out, not as a win.
    assert said_match(_m(**{**PALACE_LOSS, "home_score": 2, "away_score": 2, "winner": None, "shootout": [4, 3]}), AT) == (
        "Crystal Palace 2, Liverpool 2, Premier League, on Sunday the 8th of November. "
        "The penalty shootout finished Crystal Palace 4, Liverpool 3.")
    # A result from earlier today is today's.
    assert said_match(_m(**{**PALACE_LOSS, "kickoff": "2026-11-13T10:00:00+00:00"}), AT) == (
        "Crystal Palace 1, Liverpool 3, Premier League, today. Liverpool won.")


def test_a_match_with_no_score_says_so_rather_than_one(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    assert said_match(_m(state="in", phase="first_half", clock="12'"), AT) == (
        "Liverpool v Arsenal, Premier League, no score listed, in play, first half, 12'.")
    assert said_match(_m(**{**PALACE_LOSS, "home_score": None, "away_score": None, "winner": None}), AT) == (
        "Crystal Palace v Liverpool, Premier League, no score listed, on Sunday the 8th of November.")


@pytest.mark.parametrize("state", ["pre", "post"])
def test_a_match_which_is_off(london, state):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    match = _m(state=state, phase="off", clock="Postponed", home="Notts County", away="Stevenage", league="EFL League One",
               kickoff="2026-11-21T15:00:00+00:00", odds={"home": 55, "draw": 23, "away": 22, "source": "DraftKings"})
    # Neither its odds nor a time for it are said: it isn't being played.
    assert said_match(match, AT) == "Notts County v Stevenage, EFL League One, on Saturday the 21st of November: off (Postponed)."
    assert said_match(_m(state=state, phase="off", kickoff="2026-11-14T15:00:00+00:00"), AT) == (
        "Liverpool v Arsenal, Premier League, tomorrow: off.")


def test_odds_which_dont_add_up_to_percentages_are_refused(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    for odds in ({"home": 55, "away": 22, "source": "DraftKings"}, {"home": "55", "draw": 23, "away": 22},
                 {"home": 55, "draw": 23.5, "away": 22}, {"home": 55, "draw": 23, "away": 22, "source": 5}):
        with pytest.raises((KeyError, TypeError)):
            said_match(_m(kickoff="2026-11-22T16:30:00+00:00", odds=odds), AT)


def test_the_followed_teams_overview(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    reading = _reading(_team("364", "Liverpool", now=_found_match(**LIVE), next=[_found_match(**SUNDAY_AWAY)],
                             last=[_found_match(**PALACE_LOSS)]),
                       _team("395", "Stevenage"))
    lines = said_followed(reading, AT).split("\n")
    # A team playing now has its match as said_match says it, then its next fixture and its last result.
    assert lines[0] == (
        "Liverpool: Playing now: Liverpool 2, Manchester United 1, Premier League, in play, second half, 67'. "
        "Next: Arsenal v Liverpool, Premier League, on Sunday the 22nd of November at 4:30 PM. "
        "Last: Crystal Palace 1, Liverpool 3, Premier League, on Sunday the 8th of November. Liverpool won.")
    assert said_match(_m(**LIVE), AT) in lines[0]
    assert lines[1] == "Stevenage: no fixtures listed."
    # A match in play makes the last line the warning that its score may be a little behind.
    assert lines[2:] == [LAG] and LAG == "Live scores can be up to a minute behind the match."


def test_the_overview_with_nobody_playing_has_no_warning(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    reading = _reading(_team("364", "Liverpool", next=[_found_match(**SUNDAY_AWAY)]), _team("395", "Stevenage"))
    assert said_followed(reading, AT) == (
        "Liverpool: Next: Arsenal v Liverpool, Premier League, on Sunday the 22nd of November at 4:30 PM.\n"
        "Stevenage: no fixtures listed.")


def test_a_match_today_is_not_said_twice_in_the_overview(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    tonight = _found_match(id="11", kickoff="2026-11-13T19:45:00+00:00", home="Liverpool", away="Arsenal")
    reading = _reading(_team("364", "Liverpool", now=tonight, next=[tonight, _found_match(**SUNDAY_AWAY)]))
    assert said_followed(reading, AT) == (
        "Liverpool: Today: Liverpool v Arsenal, Premier League, today at 7:45 PM. "
        "Next: Arsenal v Liverpool, Premier League, on Sunday the 22nd of November at 4:30 PM.")


def test_a_team_with_results_but_no_fixtures_says_both(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    cup = _found_match(id="12", state="post", kickoff="2026-11-10T19:00:00+00:00", home="Stevenage", away="Fulham U21",
                       home_score=2, away_score=2, league="EFL Trophy", winner="home", shootout=[4, 3])
    assert said_followed(_reading(_team("395", "Stevenage", last=[cup])), AT) == (
        "Stevenage: no fixtures listed. "
        "Last: Stevenage 2, Fulham U21 2, EFL Trophy, on Tuesday the 10th of November. Stevenage won 4-3 on penalties.")


def test_the_overview_with_no_teams_and_with_some_left_out(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    assert said_followed(_reading(), AT) == "No teams are followed."
    sentence = "Home Assistant is showing only some of the followed teams."
    assert said_followed(_reading(_team("395", "Stevenage"), trimmed=True), AT) == f"Stevenage: no fixtures listed.\n{sentence}"
    # The warning is still the last line, so a trimmed reading with a match in play ends on it.
    lines = said_followed(_reading(_team("364", "Liverpool", now=_found_match(**LIVE)), trimmed=True), AT).split("\n")
    assert lines[-2:] == [sentence, LAG]


def test_a_team_in_full(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    answer = _team_answer(
        team={"id": "364", "name": "Liverpool", "followed": True},
        now=_found_match(**LIVE),
        next=[_found_match(**SUNDAY_AWAY, venue={"name": "Emirates Stadium", "city": "London"},
                           odds={"home": 30, "draw": 25, "away": 45, "source": "DraftKings"}),
              _found_match(id="13", kickoff="2026-11-28T15:00:00+00:00", home="Everton", away="Liverpool")],
        last=[_found_match(**PALACE_LOSS)],
        tables=[{"league": "Premier League", "group": "", "rank": 4, "of": 20, "played": 10, "won": 5, "drawn": 3, "lost": 2,
                 "gd": 4, "points": 18}])
    assert said_team(answer, AT).split("\n") == [
        "Liverpool:",
        "Playing now: Liverpool 2, Manchester United 1, Premier League, in play, second half, 67'.",
        "Next: Arsenal v Liverpool, Premier League, on Sunday the 22nd of November at 4:30 PM, at Emirates Stadium in London. "
        "Bookmakers' odds from DraftKings, as percentages: Arsenal 30, draw 25, Liverpool 45.",
        "Later: Everton v Liverpool, Premier League, on Saturday the 28th of November at 3:00 PM. ESPN lists no odds for it.",
        "Last: Crystal Palace 1, Liverpool 3, Premier League, on Sunday the 8th of November. Liverpool won.",
        "Premier League: 4th of 20, played 10, won 5, drawn 3, lost 2, goal difference 4, 18 points.",
        LAG,
    ]


def test_a_team_is_called_by_its_full_name(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    answer = _team_answer(team={"id": "360", "name": "Man Utd", "full_name": "Manchester United", "followed": False})
    assert said_team(answer, AT).split("\n")[0] == "Manchester United:"


def test_a_team_with_nothing_listed_says_so(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    assert said_team(_team_answer(), AT).split("\n")[:4] == [
        "Liverpool:", "No fixtures listed.", "No results listed.", "No league table listed."]
    # No warning without a match in play.
    assert LAG not in said_team(_team_answer(), AT)


def test_a_team_espn_doesnt_cover(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    note = ("ESPN, where Home Assistant's football comes from, doesn't seem to cover the league South Shields play in, "
            "so only their cup ties show.")
    south_shields = {"id": "1", "name": "South Shields", "followed": True}
    assert note in said_team(_team_answer(team=south_shields), AT).split("\n")
    # The note is only for a team with no table, no match today and no fixture to come: any one of them and it isn't said.
    row = {"league": "National League North", "group": "", "rank": 4, "of": 20, "played": 10, "won": 5, "drawn": 3, "lost": 2,
           "gd": 4, "points": 18}
    assert note not in said_team(_team_answer(team=south_shields, tables=[row]), AT)
    assert note not in said_team(_team_answer(team=south_shields, next=[_found_match(**SUNDAY_AWAY)]), AT)
    assert note not in said_team(_team_answer(team=south_shields, now=_found_match(**LIVE)), AT)
    # Cup results alone don't make it covered: they are the cup ties which show.
    assert note in said_team(_team_answer(team=south_shields, last=[_found_match(**PALACE_LOSS)]), AT)


def test_a_teams_table_row(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    row = {"league": "Premier League", "group": "", "rank": 4, "of": 20, "played": 10, "won": 5, "drawn": 3, "lost": 2,
           "gd": 4, "points": 18}
    said = said_team(_team_answer(tables=[row]), AT).split("\n")
    assert "Premier League: 4th of 20, played 10, won 5, drawn 3, lost 2, goal difference 4, 18 points." in said
    assert "No league table listed." not in said
    # A group is named, as is a single point, and a table in another competition has its own row.
    group = {**row, "league": "UEFA Champions League", "group": "League Phase", "rank": 1, "of": 36, "gd": -2, "points": 1}
    said = said_team(_team_answer(tables=[row, group]), AT).split("\n")
    assert "UEFA Champions League, League Phase: 1st of 36, played 10, won 5, drawn 3, lost 2, goal difference -2, 1 point." in said
    assert said[-2].startswith("Premier League: 4th")


def test_no_odds_listed(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    said = said_team(_team_answer(next=[_found_match(**SUNDAY_AWAY)]), AT)
    assert said.split("\n")[1] == (
        "Next: Arsenal v Liverpool, Premier League, on Sunday the 22nd of November at 4:30 PM. ESPN lists no odds for it.")
    # A match which isn't to come, or which is off, has no odds to list; nor has one with odds.
    done = said_team(_team_answer(last=[_found_match(**PALACE_LOSS)], next=[_found_match(**{**SUNDAY_AWAY, "phase": "off"})]), AT)
    assert "odds" not in done
    listed = {**SUNDAY_AWAY, "odds": {"home": 30, "draw": 25, "away": 45, "source": "DraftKings"}}
    assert "no odds" not in said_team(_team_answer(next=[_found_match(**listed)]), AT)


def test_a_competitions_days(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    first = _found_match(id="1", kickoff="2026-11-24T20:00:00+00:00", home="Real Madrid", away="Arsenal",
                         league="UEFA Champions League", venue={"name": "Santiago Bernabeu", "city": "Madrid"},
                         odds={"home": 40, "draw": 25, "away": 35, "source": "DraftKings"})
    second = _found_match(id="2", kickoff="2026-11-25T20:00:00+00:00", home="Bayern Munich", away="Man City",
                          away_name="Manchester City", league="UEFA Champions League")
    answer = _competition_answer([first, second], days=["20261124", "20261125"])
    assert said_competition(answer, AT).split("\n") == [
        "Real Madrid v Arsenal, UEFA Champions League, on Tuesday the 24th of November at 8:00 PM, at Santiago Bernabeu in Madrid. "
        "Bookmakers' odds from DraftKings, as percentages: Real Madrid 40, draw 25, Arsenal 35.",
        "Bayern Munich v Manchester City, UEFA Champions League, on Wednesday the 25th of November at 8:00 PM. "
        "ESPN lists no odds for it.",
        "Days with matches: Tuesday the 24th of November, Wednesday the 25th of November.",
    ]
    # Some left out by Home Assistant, which is said after the days, as they are all of them.
    assert said_competition({**answer, "trimmed": True}, AT).split("\n")[-2:] == [
        "Days with matches: Tuesday the 24th of November, Wednesday the 25th of November.",
        "Only some of the matches are listed."]


def test_a_competition_with_no_matches(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    assert said_competition(_competition_answer([]), AT) == "No UEFA Champions League matches in those days."


def test_a_cup_says_its_round(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    match = _found_match(kickoff="2026-12-02T20:00:00+00:00", league="Carabao Cup")
    answer = _competition_answer([match], days=["20261202"], competition={"slug": "eng.league_cup", "name": "Carabao Cup", "round": "Quarterfinals"})
    assert said_competition(answer, AT).split("\n") == [
        "The Carabao Cup is at the Quarterfinals.",
        "Liverpool v Arsenal, Carabao Cup, on Wednesday the 2nd of December at 8:00 PM. ESPN lists no odds for it.",
        "Days with matches: Wednesday the 2nd of December.",
    ]


def test_a_cup_says_its_round_even_with_no_matches(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    cup = {"slug": "eng.fa", "name": "FA Cup", "round": "Quarterfinals"}
    assert said_competition(_competition_answer([], competition=cup), AT).split("\n") == [
        "The FA Cup is at the Quarterfinals.", "No FA Cup matches in those days."]


def test_a_competition_with_a_match_in_play_ends_with_the_warning(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    answer = _competition_answer([_found_match(**LIVE)], days=["20261113"])
    assert said_competition(answer, AT).split("\n")[-2:] == ["Days with matches: today.", LAG]


@pytest.mark.parametrize("days", [["tomorrow"], ["2026-11-24"], [20261124]])
def test_a_day_which_isnt_a_day_is_refused(london, days):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    with pytest.raises((TypeError, ValueError)):
        said_competition(_competition_answer([_found_match()], days=days), AT)


def test_a_table(london):
    rows = [{"rank": 1, "team": "Arsenal", "played": 10, "won": 9, "drawn": 0, "lost": 1, "gd": 10, "points": 27},
            {"rank": 2, "team": "Man City", "team_name": "Manchester City", "played": 10, "won": 6, "drawn": 3, "lost": 1,
             "gd": -3, "points": 21}]
    answer = {"competition": {"slug": "eng.1", "name": "Premier League"}, "groups": [{"name": "", "rows": rows}], "stale": False}
    assert said_table(answer).split("\n") == [
        "Premier League:",
        "1. Arsenal, played 10, 27 points, goal difference 10.",
        "2. Manchester City, played 10, 21 points, goal difference -3.",
    ]


def test_a_table_with_groups_says_each_under_its_name(london):
    row = {"rank": 1, "team": "Arsenal", "played": 2, "won": 2, "drawn": 0, "lost": 0, "gd": 5, "points": 1}
    answer = {"competition": {"slug": "uefa.champions", "name": "UEFA Champions League"},
              "groups": [{"name": "Group A", "rows": [row]}, {"name": "Group B", "rows": [{**row, "team": "Bayern Munich", "points": 6}]}]}
    assert said_table(answer).split("\n") == [
        "UEFA Champions League, Group A:", "1. Arsenal, played 2, 1 point, goal difference 5.",
        "UEFA Champions League, Group B:", "1. Bayern Munich, played 2, 6 points, goal difference 5."]
    # A group named as its competition is, isn't said twice.
    named = {"competition": {"slug": "eng.1", "name": "Premier League"}, "groups": [{"name": "Premier League", "rows": [row]}]}
    assert said_table(named).split("\n")[0] == "Premier League:"


def test_a_competition_with_no_table(london):
    answer = {"competition": {"slug": "eng.league_cup", "name": "Carabao Cup"}, "groups": []}
    assert said_table(answer) == "No table is listed for the Carabao Cup."


def test_whos_left(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    league = _left_answer(name="Premier League", knockout=False)
    assert said_left(league, AT) == "The Premier League isn't a knockout competition, so nobody is knocked out of it."
    cup = _left_answer(knockout=True, teams=["Arsenal", "Liverpool", "Everton"],
                       competition={"slug": "eng.league_cup", "name": "Carabao Cup", "round": "Quarterfinals"})
    assert said_left(cup, AT) == "Still in the Carabao Cup, which is at the Quarterfinals: Arsenal, Liverpool and Everton."
    drawn_later = {**cup, "from_round": "Fourth Round"}
    assert said_left(drawn_later, AT) == (
        "Still in the Carabao Cup, which is at the Quarterfinals: Arsenal, Liverpool and Everton. "
        "The Quarterfinals haven't been drawn yet, so these are the winners of the Fourth Round.")


def test_whos_left_in_the_other_cases(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    assert said_left(_left_answer(teams=["Arsenal", "Liverpool"]), AT) == "Still in the Carabao Cup: Arsenal and Liverpool."
    final = _left_answer(teams=["Arsenal"], competition={"slug": "x", "name": "Carabao Cup", "round": "Final"}, from_round="Semifinals")
    assert said_left(final, AT) == (
        "Still in the Carabao Cup, which is at the Final: Arsenal. The Final hasn't been drawn yet, so these are the winners of the Semifinals.")
    # With no round of its own named, "the next round" stands in for it.
    assert said_left(_left_answer(teams=["Arsenal", "Liverpool"], from_round="Fourth Round"), AT) == (
        "Still in the Carabao Cup: Arsenal and Liverpool. The next round hasn't been drawn yet, so these are the winners of the Fourth Round.")
    assert said_left(_left_answer(teams=[]), AT) == "No teams are listed as still in the Carabao Cup."
    assert said_left(_left_answer(), AT) == "No teams are listed as still in the Carabao Cup."
    # A cup's round is said with none left to list, as it is the answer to "what round is it at?".
    quarterfinals = {"slug": "eng.league_cup", "name": "Carabao Cup", "round": "Quarterfinals"}
    assert said_left(_left_answer(teams=[], competition=quarterfinals), AT) == (
        "No teams are listed as still in the Carabao Cup, which is at the Quarterfinals.")
    # A round nobody is knocked out of, in a competition which is a knockout one.
    phase = _left_answer(knockout=False, competition={"slug": "uefa.champions", "name": "UEFA Champions League", "round": "League Phase"})
    assert said_left(phase, AT) == "Nobody is knocked out of the UEFA Champions League at the League Phase."


def test_whos_left_lists_the_matches_of_the_round(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    live = _found_match(**{**LIVE, "league": "Carabao Cup"})
    answer = _left_answer(teams=["Arsenal", "Liverpool"], matches=[live, _found_match(**{**SUNDAY_AWAY, "league": "Carabao Cup"})])
    assert said_left(answer, AT).split("\n") == [
        "Still in the Carabao Cup: Arsenal and Liverpool.",
        "Liverpool 2, Manchester United 1, Carabao Cup, in play, second half, 67'.",
        "Arsenal v Liverpool, Carabao Cup, on Sunday the 22nd of November at 4:30 PM. ESPN lists no odds for it.",
        LAG,
    ]


@pytest.mark.parametrize("changes", [{"knockout": "false"}, {"teams": "Arsenal"}, {"teams": [1]}, {"matches": [{"id": "1"}]}, {"from_round": 4}])
def test_an_answer_of_the_wrong_shape_is_refused(london, changes):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    with pytest.raises((KeyError, TypeError, ValueError)):
        said_left(_left_answer(**changes), AT)


def test_both_sides(london):
    lines = said_lineups(_lineups_answer(match=_found_match(**SUNDAY_AWAY), sides=[ARSENAL_SIDE, LIVERPOOL_SIDE]),
                         AT.astimezone()).split("\n")
    assert len(lines) == 3
    assert lines[0] == said_match(_m(**SUNDAY_AWAY), AT.astimezone())
    # The team asked about comes first, whatever order the answer has
    assert lines[1] == ("Liverpool's starting eleven (4-2-3-1): Alisson, Frimpong, Konate, Van Dijk, Kerkez, "
                        "Gravenberch, Mac Allister, Salah, Szoboszlai, Gakpo and Isak. Bench: Mamardashvili and Gomez.")
    assert lines[2].startswith("Arsenal's starting eleven: Raya,")
    assert lines[2].endswith("Bench: none listed.")


def test_one_side_out(london):
    said = said_lineups(_lineups_answer(match=_found_match(**SUNDAY_AWAY), sides=[LIVERPOOL_SIDE]), AT.astimezone())
    assert said.split("\n")[-1] == "Arsenal's line-up isn't out yet."


def test_the_other_side_out_and_not_the_team_asked_about(london):
    lines = said_lineups(_lineups_answer(match=_found_match(**SUNDAY_AWAY), sides=[ARSENAL_SIDE]), AT.astimezone()).split("\n")
    assert len(lines) == 3
    assert lines[1].startswith("Arsenal's starting eleven: Raya,")
    assert lines[2] == "Liverpool's line-up isn't out yet."


def test_the_team_asked_about_at_home(london):
    # Liverpool are the home side in the Contract's default match, so the other way round from SUNDAY_AWAY.
    lines = said_lineups(_lineups_answer(match=_found_match(), sides=[ARSENAL_SIDE, LIVERPOOL_SIDE]), AT.astimezone()).split("\n")
    assert lines[1].startswith("Liverpool's starting eleven (4-2-3-1): Alisson,")
    assert lines[2].startswith("Arsenal's starting eleven: Raya,")
    assert said_lineups(_lineups_answer(match=_found_match(), sides=[LIVERPOOL_SIDE]), AT.astimezone()).split("\n")[-1] == (
        "Arsenal's line-up isn't out yet.")
    assert said_lineups(_lineups_answer(match=_found_match(), sides=[ARSENAL_SIDE]), AT.astimezone()).split("\n")[-1] == (
        "Liverpool's line-up isn't out yet.")


def test_none_out_yet(london):
    said = said_lineups(_lineups_answer(match=_found_match(**SUNDAY_AWAY)), AT.astimezone())
    assert said.split("\n")[-1] == "Line-ups aren't out yet; they usually come about an hour before kick-off."


@pytest.mark.parametrize("match", [PALACE_LOSS, {"id": "bbc:4412", "slug": "bbc:eng.5", "away_id": "bbc:77",
                                                 "kickoff": "2026-11-22T16:30:00+00:00"},
                                   {"state": "pre", "phase": "off", "clock": "Postponed"}],
                         ids=["a match over", "a match from BBC", "a match which is off"])
def test_no_lineups_for_a_match_over_or_from_bbc(london, match):
    said = said_lineups(_lineups_answer(match=_found_match(**match)), AT.astimezone())
    assert said.split("\n")[-1] == "No line-ups are listed for it."
    assert "isn't out yet" not in said and "aren't out yet" not in said


def test_a_match_in_play_with_no_lineups_says_none_are_listed(london):
    # Line-ups come out before kick-off, so for a match under way none are coming.
    lines = said_lineups(_lineups_answer(match=_found_match(**LIVE)), AT.astimezone()).split("\n")
    assert lines[-2:] == ["No line-ups are listed for it.", LAG]
    assert "out yet" not in "\n".join(lines)


UNITED_SIDE = {**ARSENAL_SIDE, "team": "Man Utd", "team_name": "Manchester United", "formation": "3-4-3"}


@pytest.mark.parametrize("match, side, missing", [
    (LIVE, LIVERPOOL_SIDE, "Manchester United"),
    (LIVE, UNITED_SIDE, "Liverpool"),
    (PALACE_LOSS, LIVERPOOL_SIDE, "Crystal Palace"),
    (PALACE_LOSS, {**ARSENAL_SIDE, "team": "Crystal Palace"}, "Liverpool"),
    ({"id": "bbc:4412", "slug": "bbc:eng.5", "away_id": "bbc:77"}, LIVERPOOL_SIDE, "Arsenal"),
    ({"state": "pre", "phase": "off", "clock": "Postponed"}, LIVERPOOL_SIDE, "Arsenal"),
], ids=["in play, the other not listed", "in play, the team asked about not listed", "over, the other not listed",
        "over, the team asked about not listed", "from BBC", "off"])
def test_a_side_missing_from_a_match_which_isnt_to_come_isnt_listed(london, match, side, missing):
    lines = said_lineups(_lineups_answer(match=_found_match(**match), sides=[side]), AT.astimezone()).split("\n")
    # LAG always ends the answer for a match in play, so the line is before it there.
    assert (lines[-1] == LAG) == (match is LIVE)
    before_lag = lines[:-1] if lines[-1] == LAG else lines
    assert before_lag[-1] == f"{missing}'s line-up isn't listed."
    assert "out yet" not in "\n".join(lines)


def test_the_sides_are_said_by_their_full_names(london):
    match = _found_match(**{**LIVE, "state": "pre", "phase": None, "home_score": None, "away_score": None})
    team = {"id": "360", "name": "Man Utd", "full_name": "Manchester United", "followed": True}
    lines = said_lineups(_lineups_answer(team=team, match=match, sides=[LIVERPOOL_SIDE, UNITED_SIDE]), AT.astimezone()).split("\n")
    assert lines[1].startswith("Manchester United's starting eleven (3-4-3): Raya,")
    assert lines[2].startswith("Liverpool's starting eleven (4-2-3-1): Alisson,")
    # Where one side is out, the other is named as the match names it
    lines = said_lineups(_lineups_answer(team=team, match=match, sides=[LIVERPOOL_SIDE]), AT.astimezone()).split("\n")
    assert lines[-1] == "Manchester United's line-up isn't out yet."
    # A side is placed in the match by either of the names the match gives it
    short = {**UNITED_SIDE, "team_name": None}
    lines = said_lineups(_lineups_answer(team=team, match=match, sides=[short]), AT.astimezone()).split("\n")
    assert lines[1].startswith("Man Utd's starting eleven (3-4-3): Raya,")
    assert lines[-1] == "Liverpool's line-up isn't out yet."


def test_a_name_ending_in_s_takes_the_apostrophe_alone(london):
    match = _found_match(home="Wolves", home_id="380", away="Spurs", away_id="367")
    team = {"id": "380", "name": "Wolves", "followed": False}
    wolves = {**ARSENAL_SIDE, "team": "Wolves"}
    lines = said_lineups(_lineups_answer(team=team, match=match, sides=[wolves]), AT.astimezone()).split("\n")
    assert lines[1].startswith("Wolves' starting eleven: Raya,")
    assert lines[2] == "Spurs' line-up isn't out yet."
    over = _found_match(home="Wolves", home_id="380", away="Spurs", away_id="367", state="post")
    lines = said_lineups(_lineups_answer(team=team, match=over, sides=[wolves]), AT.astimezone()).split("\n")
    assert lines[2] == "Spurs' line-up isn't listed."
    spurs = {**ARSENAL_SIDE, "team": "Spurs"}
    assert said_lineups(_lineups_answer(team=team, match=over, sides=[spurs]), AT.astimezone()).endswith(
        "Wolves' line-up isn't listed.")


def test_a_match_in_play_ends_with_the_warning(london):
    said = said_lineups(_lineups_answer(match=_found_match(**LIVE), sides=[LIVERPOOL_SIDE, UNITED_SIDE]), AT.astimezone())
    assert said.split("\n")[-1] == LAG
    assert LAG not in said_lineups(_lineups_answer(match=_found_match(**SUNDAY_AWAY), sides=[LIVERPOOL_SIDE]), AT.astimezone())


def test_no_match_at_all(london):
    assert said_lineups(_lineups_answer(), AT.astimezone()) == "No match is listed for Liverpool."
    team = {"id": "364", "name": "Liverpool", "full_name": "Liverpool FC", "followed": True}
    assert said_lineups(_lineups_answer(team=team), AT.astimezone()) == "No match is listed for Liverpool FC."


class FakeHome:
    """
    Gives the sensor as Home Assistant does and answers football_ask() with what a test sets - or raises
    what it sets - noting what was asked.
    """

    def __init__(self, teams=(), answer=None, **sensor):
        self.sensor = {"entity_id": FOOTBALL_TEAMS, "state": "1", "attributes": {"teams": list(teams), **sensor},
                       "last_reported": _iso(NOW - 10)}
        self.answer = answer
        self.asked = []

    def state(self, entity_id, timeout=None):
        assert entity_id == FOOTBALL_TEAMS
        if isinstance(self.sensor, Exception):
            raise self.sensor
        return self.sensor

    def football_ask(self, what, name, date="", days=1):
        self.asked.append((what, name, date, days))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _football(home):
    return Football(home, dict, now=lambda: NOW)


def _said(home, now=None, **asked):
    """What describe says of a question, with the words built for a Friday midday in London unless a test gives a time."""
    return describe(_football(home), now=now or datetime(2026, 11, 13, 12, 0).astimezone(), **asked)


NOT_GOT = {"success": False, "message": "I can't get football right now."}


def test_asked_of_nobody_it_reads_the_sensor(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    teams = [_team("395", "Stevenage", next=[_found_match(kickoff="2026-11-21T15:00:00+00:00", home="Notts County",
                                                          away="Stevenage", league="EFL League One")]),
             _team("364", "Liverpool", now=_found_match(**LIVE))]
    home = FakeHome(teams)
    said = describe(_football(home), now=AT)
    assert said == {"success": True, "message": said_followed(_reading(*teams), AT)}
    assert "Stevenage: Next: Notts County v Stevenage" in said["message"] and said["message"].endswith(LAG)
    assert home.asked == []


def test_asked_of_nobody_with_no_teams_followed_says_so(london):
    assert _said(FakeHome([])) == {"success": True, "message": "No teams are followed."}


CANT_GET_SHIELDS = "I can't get South Shields right now."


def test_the_overview_names_a_team_it_cant_get(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    liverpool = _team("364", "Liverpool", now=_found_match(**LIVE))
    # The stale team's match is an old copy, so nothing of it is said, only that it can't be got.
    shields = _team("396", "South Shields", stale=True, now=_found_match(
        id="21", home="South Shields", away="Gateshead", league="Northern Premier League"))
    home = FakeHome([liverpool, shields], stale=True)
    said = _said(home)
    assert said["success"] is True
    # The fresh team is said as the overview says it, and the line goes before the warning, which stays last.
    fresh = said_followed(_reading(liverpool), AT).split("\n")
    assert fresh[-1] == LAG
    assert said["message"].split("\n") == fresh[:-1] + [CANT_GET_SHIELDS, LAG]
    assert "Gateshead" not in said["message"] and "South Shields:" not in said["message"]
    assert home.asked == []


def test_the_overview_names_each_team_it_cant_get_as_its_last_line_with_nothing_in_play(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    liverpool = _team("364", "Liverpool", next=[_found_match(**SUNDAY_AWAY)])
    home = FakeHome([_team("395", "Stevenage", stale=True), liverpool, _team("396", "South Shields", stale=True)])
    said = _said(home)
    assert said == {"success": True, "message": said_followed(_reading(liverpool), AT)
                    + "\nI can't get Stevenage and South Shields right now."}


def test_the_overview_names_the_stale_team_though_the_fresh_one_has_nothing_listed(london):
    # A fresh team with nothing listed is still a team, so this is not "No teams are followed."
    home = FakeHome([_team("395", "Stevenage"), _team("396", "South Shields", stale=True)])
    assert _said(home) == {"success": True, "message": f"Stevenage: no fixtures listed.\n{CANT_GET_SHIELDS}"}


def test_the_overview_says_what_is_left_out_first_then_the_team_it_cant_get_then_the_warning(london):
    sentence = "Home Assistant is showing only some of the followed teams."
    home = FakeHome([_team("364", "Liverpool", now=_found_match(**LIVE)), _team("396", "South Shields", stale=True)],
                    trimmed=True)
    assert _said(home)["message"].split("\n")[-3:] == [sentence, CANT_GET_SHIELDS, LAG]


@pytest.mark.parametrize("teams, sensor", [
    ([_team("395", "Stevenage", stale=True), _team("364", "Liverpool", stale=True)], {}),
    ([_team("364", "Liverpool", stale=True)], {}),
    # An older Home Assistant doesn't mark teams, so with the sensor stale all of them are.
    ([_team("395", "Stevenage"), _team("364", "Liverpool")], {"stale": True}),
], ids=["every team stale", "the only team stale", "a stale sensor which marks no team"])
def test_the_overview_with_every_team_stale(london, caplog, teams, sensor):
    home = FakeHome(teams, **sensor)
    with caplog.at_level(logging.WARNING, logger="voice_assistant.football"):
        assert _said(home) == NOT_GOT
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert home.asked == []


def test_a_sensor_which_is_stale_with_no_team_marked_stops_the_overview(london):
    # An older Home Assistant doesn't mark teams, so with the sensor stale all of them are.
    assert _said(FakeHome([_team("364", "Liverpool")], stale=True)) == NOT_GOT


@pytest.mark.parametrize("failure, said", [
    (requests.ConnectionError("refused"), "I can't reach Home Assistant right now."),
    (requests.HTTPError("404", response=SimpleNamespace(status_code=404)), "Football isn't set up in Home Assistant."),
])
def test_a_sensor_which_cant_be_read_says_why(london, failure, said):
    home = FakeHome()
    home.sensor = failure
    assert _said(home) == {"success": False, "message": said}


def test_a_sensor_of_the_wrong_shape_cant_be_got(london):
    assert _said(FakeHome(["Liverpool"])) == NOT_GOT


def test_a_team_is_asked_of_home_assistant(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    answer = _team_answer(next=[_found_match(kickoff="2026-11-22T16:30:00+00:00", home="Arsenal", away="Liverpool")])
    home = FakeHome(answer=answer)
    assert describe(_football(home), team="Arsenal", now=AT) == {"success": True, "message": said_team(answer, AT)}
    assert home.asked == [("team", "Arsenal", "", 1)]


@pytest.mark.parametrize("given, asked", [
    ({"competition": "Champions League", "date": "2026-11-24", "days": 2}, ("competition", "Champions League", "20261124", 2)),
    ({"competition": "Premier League", "about": "table"}, ("table", "Premier League", "", 1)),
    ({"competition": "Carabao Cup", "about": "left"}, ("left", "Carabao Cup", "", 1)),
    ({"team": "Liverpool", "competition": "Carabao Cup", "about": "left"}, ("left", "Carabao Cup", "", 1)),
    ({"team": "Liverpool", "about": "table"}, ("team", "Liverpool", "", 1)),
    ({"team": "Liverpool", "competition": "Carabao Cup"}, ("competition", "Carabao Cup", "", 1)),
    ({"competition": "Carabao Cup", "about": "matches"}, ("competition", "Carabao Cup", "", 1)),
    ({"team": "Liverpool", "about": "matches"}, ("team", "Liverpool", "", 1)),
    ({"team": "Liverpool", "about": "left"}, ("team", "Liverpool", "", 1)),
    ({"team": "Liverpool", "about": " Lineups "}, ("lineups", "Liverpool", "", 1)),
    ({"competition": "Premier League", "about": " Table "}, ("table", "Premier League", "", 1)),
    ({"competition": "Premier League", "about": "standings"}, ("competition", "Premier League", "", 1)),
    ({"team": "  Liverpool ", "competition": "  "}, ("team", "Liverpool", "", 1)),
])
def test_each_question_goes_to_the_right_ask(london, given, asked):
    home = FakeHome(answer={"stale": False, "unknown": ["x"]})
    _said(home, **given)
    assert home.asked == [asked]


@pytest.mark.parametrize("about, answer, said_by, words", [
    ("matches", _competition_answer([_found_match(**LIVE)]), said_competition, "in play, second half, 67'"),
    ("table", {"competition": {"slug": "eng.1", "name": "Premier League"}, "stale": False,
               "groups": [{"name": "Premier League", "rows": [
                   {"rank": 1, "team": "Arsenal", "played": 10, "won": 8, "drawn": 1, "lost": 1, "gd": 14, "points": 25}]}]},
     lambda answer, now: said_table(answer), "1. Arsenal, played 10, 25 points"),
    ("left", _left_answer(teams=["Arsenal", "Liverpool"]), said_left, "Still in the Carabao Cup: Arsenal and Liverpool."),
])
def test_each_answer_is_said_by_its_own_words(london, about, answer, said_by, words):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    said = describe(_football(FakeHome(answer=answer)), competition="Anything", about=about, now=AT)
    assert said == {"success": True, "message": said_by(answer, AT)}
    assert words in said["message"]


@pytest.mark.parametrize("answer", [{"stale": True, "team": {}}, {"stale": False, "error": "OSError: down"}])
def test_an_old_or_failed_answer_cant_be_got(london, answer):
    assert _said(FakeHome(answer=answer), team="Arsenal") == NOT_GOT


@pytest.mark.parametrize("failure", [ValueError("not json"), requests.Timeout("slow")])
def test_an_answer_which_cant_be_read_or_didnt_come_cant_be_got(london, failure):
    assert _said(FakeHome(answer=failure), team="Arsenal") == NOT_GOT


def test_home_assistant_being_out_of_reach_is_said_so(london):
    said = _said(FakeHome(answer=requests.ConnectionError("refused")), team="Arsenal")
    assert said == {"success": False, "message": "I can't reach Home Assistant right now."}


def test_a_name_espn_doesnt_know(london):
    answer = {"unknown": ["Arsnal"], "stale": False}
    assert _said(FakeHome(answer=answer), team="Arsnal") == {
        "success": True, "message": "ESPN, where Home Assistant's football comes from, found nothing called Arsnal."}


def test_names_espn_doesnt_know_are_all_said_and_no_words_are_built_for_them(london, monkeypatch):
    def refused(*_):
        raise AssertionError("said_* was called for an answer which found nothing")

    for name in ("said_team", "said_competition", "said_table", "said_left", "said_lineups"):
        monkeypatch.setattr(football_words, name, refused)
    answer = {"unknown": ["Arsnal", "Chelsee"], "stale": False}
    for given in ({"team": "Arsnal"}, {"competition": "x"}, {"competition": "x", "about": "table"},
                  {"competition": "x", "about": "left"}, {"team": "Arsnal", "about": "lineups"}):
        assert _said(FakeHome(answer=answer), **given) == {
            "success": True,
            "message": "ESPN, where Home Assistant's football comes from, found nothing called Arsnal and Chelsee."}


def test_an_old_answer_is_old_even_if_it_found_nothing(london):
    assert _said(FakeHome(answer={"unknown": ["Arsnal"], "stale": True}), team="Arsnal") == NOT_GOT


@pytest.mark.parametrize("given, answer", [
    ({"team": "Arsenal"}, {"stale": False}),
    ({"team": "Arsenal"}, _team_answer(next="soon")),
    ({"team": "Arsenal"}, _team_answer(next=[{"id": "1"}])),
    ({"team": "Arsenal"}, _team_answer(next=[_found_match(odds={"home": "55", "draw": 23, "away": 22})])),
    ({"competition": "Premier League"}, _competition_answer([_found_match(state="live")])),
    ({"competition": "Premier League"}, _competition_answer([_found_match()], days=["next week"])),
    ({"competition": "Premier League", "about": "table"}, {"competition": {"name": "Premier League"}, "stale": False}),
    ({"competition": "Premier League", "about": "left"}, _left_answer(knockout="yes")),
    ({"competition": "Premier League", "about": "left"}, {"stale": False, "unknown": "Arsnal"}),
    ({"competition": "Premier League", "about": "left"}, {"stale": False, "unknown": [4]}),
], ids=["no team", "next not a list", "match lacking keys", "odds not numbers", "match state unknown", "day not a day",
        "table with no groups", "knockout not a flag", "unknown not a list", "unknown not names"])
def test_an_answer_of_the_wrong_shape_cant_be_got_and_is_logged(london, caplog, given, answer):
    with caplog.at_level(logging.WARNING, logger="voice_assistant.football_words"):
        assert _said(FakeHome(answer=answer), **given) == NOT_GOT
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert "isn't shaped as expected" in caplog.records[0].getMessage()


def test_a_wrong_shaped_sensor_odds_cant_be_got_and_is_logged(london, caplog):
    teams = [_team("364", "Liverpool", next=[_found_match(odds={"home": 55})])]
    with caplog.at_level(logging.WARNING, logger="voice_assistant.football_words"):
        assert _said(FakeHome(teams)) == NOT_GOT
    assert len(caplog.records) == 1


@pytest.mark.parametrize("given, asked", [
    (5, 5), ("5", 5), (" 5 ", 5), (2.0, 2), (31, 31), (40, 40), (0, 0),
    (None, 1), ("abc", 1), ("", 1), (2.5, 1), ("2.5", 1), (True, 1), ([3], 1), ({"days": 3}, 1), (float("nan"), 1), (float("inf"), 1),
], ids=repr)
def test_days_are_a_whole_number_or_one(london, given, asked):
    # Home Assistant's own call keeps it between 1 and 31; this only makes sure it is a number it can be given.
    home = FakeHome(answer={"stale": False, "unknown": ["x"]})
    _said(home, competition="Premier League", days=given)
    assert home.asked == [("competition", "Premier League", "", asked)]


@pytest.mark.parametrize("given, asked", [
    ("2026-11-24", "20261124"), (" 2026-11-24 ", "20261124"), ("2026-02-28", "20260228"),
    ("20261124", ""), ("24/11/2026", ""), ("2026-11-24T12:00", ""), ("2026-13-45", ""), ("2026-02-30", ""), ("tomorrow", ""),
    ("", ""), (None, ""), (20261124, ""), (["2026-11-24"], ""), ("٢٠٢٦-١١-٢٤", ""),
], ids=repr)
def test_a_date_is_sent_on_only_as_yyyy_mm_dd(london, given, asked):
    home = FakeHome(answer={"stale": False, "unknown": ["x"]})
    _said(home, competition="Premier League", date=given)
    assert home.asked == [("competition", "Premier League", asked, 1)]


@pytest.mark.parametrize("given", [{"team": 5}, {"competition": ["Premier League"]}, {"team": "Arsenal", "competition": {}}])
def test_a_team_or_competition_which_isnt_a_name_isnt_guessed_at(london, given):
    home = FakeHome(answer={"stale": False, "unknown": ["x"]})
    assert _said(home, **given) == {"success": False, "message": "I didn't catch which team or competition."}
    assert home.asked == []


def test_it_is_said_for_the_time_now_unless_a_time_is_given(london):
    # With no time given, the words are built for the machine's own clock - "today" is today there.
    today = datetime.now().astimezone()
    kickoff = today.replace(hour=23, minute=59).astimezone(timezone.utc).isoformat()
    answer = _team_answer(next=[_found_match(kickoff=kickoff)])
    said = describe(_football(FakeHome(answer=answer)), team="Liverpool")
    assert said["success"] and "Liverpool v Arsenal, Premier League, today at" in said["message"]


def test_lineups_are_asked_of_home_assistant(london):
    answer = _lineups_answer(match=_found_match(**SUNDAY_AWAY), sides=[LIVERPOOL_SIDE])
    home = FakeHome(answer=answer)
    said = describe(_football(home), team="Liverpool", about="lineups", now=AT.astimezone())
    assert said == {"success": True, "message": said_lineups(answer, AT.astimezone())}
    assert home.asked == [("lineups", "Liverpool", "", 1)]


@pytest.mark.parametrize("teams", [
    [_team("395", "Stevenage"), _team("364", "Liverpool", next=[_found_match(kickoff="2026-11-21T15:00:00+00:00")])],
    [_team("364", "Liverpool", now=_found_match(kickoff="2026-11-14T15:00:00+00:00"))],
    [],
], ids=["no match today", "a match tomorrow", "no teams"])
def test_lineups_with_no_team_and_nobody_playing(london, teams):
    home = FakeHome(teams)
    assert _said(home, about="lineups") == {"success": True, "message": NO_LINEUP_TEAM}
    assert home.asked == []


@pytest.mark.parametrize("teams, sensor", [
    ([_team("364", "Liverpool", now=_found_match(**LIVE), stale=True)], {}),
    ([_team("364", "Liverpool", now=_found_match(**LIVE), stale=True), _team("395", "Stevenage")], {}),
    ([_team("364", "Liverpool", stale=True), _team("395", "Stevenage", stale=True)], {}),
    ([_team("364", "Liverpool", now=_found_match(**LIVE)), _team("395", "Stevenage")], {"stale": True}),
], ids=["a stale team playing", "a stale team playing and a fresh one not", "every team stale",
        "a stale sensor which marks no team"])
def test_lineups_with_no_team_cant_say_nobody_is_playing_while_a_team_is_stale(london, caplog, teams, sensor):
    # A stale team's match is an old copy, so it can't be said that nobody is playing.
    home = FakeHome(teams, **sensor)
    with caplog.at_level(logging.WARNING, logger="voice_assistant.football"):
        assert _said(home, about="lineups") == NOT_GOT
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert home.asked == []


def test_lineups_with_no_team_ask_for_a_fresh_team_playing_though_another_is_stale(london):
    teams = [_team("364", "Liverpool", now=_found_match(**LIVE), stale=True),
             _team("395", "Stevenage", now=_found_match(kickoff="2026-11-13T19:45:00+00:00", home="Stevenage", away="Wigan"))]
    home = FakeHome(teams, answer=_lineups_answer(match=_found_match()))
    assert _said(home, about="lineups")["success"]
    assert home.asked == [("lineups", "Stevenage", "", 1)]


def test_lineups_with_no_team_ask_for_the_team_playing(london):
    # Liverpool is asked for though Stevenage comes first in the box, and though Stevenage is the one with a match later today.
    teams = [_team("395", "Stevenage", now=_found_match(kickoff="2026-11-13T19:45:00+00:00", home="Stevenage", away="Wigan")),
             _team("364", "Liverpool", now=_found_match(**LIVE))]
    answer = _lineups_answer(match=_found_match(**LIVE), sides=[LIVERPOOL_SIDE])
    home = FakeHome(teams, answer=answer)
    assert _said(home, about="lineups") == {"success": True, "message": said_lineups(answer, AT.astimezone())}
    assert home.asked == [("lineups", "Liverpool", "", 1)]


def test_lineups_with_no_team_ask_for_the_team_kicking_off_first_today(london):
    teams = [_team("395", "Stevenage", now=_found_match(kickoff="2026-11-13T19:45:00+00:00", home="Stevenage", away="Wigan")),
             _team("364", "Liverpool", now=_found_match(kickoff="2026-11-13T15:00:00+00:00"))]
    home = FakeHome(teams, answer=_lineups_answer(match=_found_match()))
    assert _said(home, about="lineups")["success"]
    assert home.asked == [("lineups", "Liverpool", "", 1)]


def test_lineups_with_no_team_ask_for_a_match_still_to_come_before_one_over(london):
    # At six in the evening, the eight o'clock match whose line-ups come out at seven is the one asked for, not the earlier one played.
    evening = datetime(2026, 11, 13, 18, 0).astimezone()
    played = _team("395", "Stevenage", now=_found_match(kickoff="2026-11-13T12:30:00+00:00", state="post", home="Stevenage"))
    coming = _team("364", "Liverpool", now=_found_match(kickoff="2026-11-13T20:00:00+00:00"))
    home = FakeHome([played, coming], answer=_lineups_answer(match=_found_match()))
    _said(home, now=evening, about="lineups")
    assert home.asked == [("lineups", "Liverpool", "", 1)]
    # With none still to come, the one played today is asked for
    home = FakeHome([played, _team("364", "Liverpool")], answer=_lineups_answer(match=_found_match()))
    _said(home, now=evening, about="lineups")
    assert home.asked == [("lineups", "Stevenage", "", 1)]


def test_lineups_with_no_team_ask_for_a_team_in_play_before_one_to_come(london):
    teams = [_team("395", "Stevenage", now=_found_match(kickoff="2026-11-13T15:00:00+00:00", home="Stevenage")),
             _team("364", "Liverpool", now=_found_match(**LIVE))]
    home = FakeHome(teams, answer=_lineups_answer(match=_found_match()))
    _said(home, now=datetime(2026, 11, 13, 14, 0).astimezone(), about="lineups")
    assert home.asked == [("lineups", "Liverpool", "", 1)]


def test_lineups_with_no_team_ask_for_the_team_by_the_entry_of_the_box_which_found_it(london):
    # Home Assistant is known to find a team by that, which its short name from ESPN may not be.
    teams = [_team("360", "Man Utd", asked="Manchester United FC", now=_found_match(**LIVE))]
    home = FakeHome(teams, answer=_lineups_answer(match=_found_match()))
    _said(home, about="lineups")
    assert home.asked == [("lineups", "Manchester United FC", "", 1)]


def test_today_is_by_the_local_clock(london):
    # British Summer Time is an hour ahead of UTC: 23:30 UTC on 12 June is 00:30 on the 13th here, and 23:30 UTC on the 13th is the 14th.
    summer = datetime(2026, 6, 13, 12, 0).astimezone()
    after_midnight = _team("364", "Liverpool", now=_found_match(kickoff="2026-06-12T23:30:00+00:00"))
    next_day = _team("395", "Stevenage", now=_found_match(kickoff="2026-06-13T23:30:00+00:00"))
    home = FakeHome([next_day, after_midnight], answer=_lineups_answer(match=_found_match()))
    describe(_football(home), about="lineups", now=summer)
    assert home.asked == [("lineups", "Liverpool", "", 1)]
    home = FakeHome([next_day])
    assert describe(_football(home), about="lineups", now=summer) == {"success": True, "message": NO_LINEUP_TEAM}
    assert home.asked == []


@pytest.mark.parametrize("failure, said", [
    (requests.ConnectionError("refused"), "I can't reach Home Assistant right now."),
    (requests.HTTPError("404", response=SimpleNamespace(status_code=404)), "Football isn't set up in Home Assistant."),
])
def test_lineups_with_no_team_say_why_the_sensor_cant_be_read(london, failure, said):
    home = FakeHome()
    home.sensor = failure
    assert _said(home, about="lineups") == {"success": False, "message": said}
    assert _said(FakeHome(["Liverpool"]), about="lineups") == NOT_GOT


def test_lineups_of_a_competition_is_its_matches(london):
    home = FakeHome(answer={"stale": False, "unknown": ["x"]})
    _said(home, competition="Premier League", about="lineups")
    _said(home, team="Liverpool", competition="Premier League", about=" Lineups ")
    assert home.asked == [("competition", "Premier League", "", 1), ("competition", "Premier League", "", 1)]


@pytest.mark.parametrize("changes", [
    {"sides": "Liverpool"}, {"sides": [{"team": "Liverpool", "starters": [7], "bench": []}]},
    {"sides": [{"team": "Liverpool", "starters": []}]}, {"match": "today"},
    {"sides": [{"team": "Liverpool", "starters": [], "bench": []}]},
    {"sides": [{"team": "Liverpool", "starters": ["Alisson"], "bench": "Gomez"}]},
    {"sides": ["Liverpool"]}, {"sides": [{"starters": ["Alisson"], "bench": []}]},
    {"sides": [{"team": "Liverpool", "starters": ["Alisson"]}]},
    {"sides": [LIVERPOOL_SIDE], "match": {"id": "1"}},
    {"sides": [LIVERPOOL_SIDE], "match": _found_match(home="Arsenal", away="Chelsea", home_id="1", away_id="2")},
    {"match": _found_match(home="Arsenal", away="Chelsea", home_id="1", away_id="2")},
    {"sides": [{**LIVERPOOL_SIDE, "team": "Wolves"}]},
    {"sides": [LIVERPOOL_SIDE, {**ARSENAL_SIDE, "team": "Liverpool"}]},
], ids=["sides not a list", "a starter not a name", "no bench and no starters", "match not a match", "no starters",
        "bench not a list", "a side not a dict", "a side with no name", "starters but no bench", "match lacking keys",
        "a match which isn't the team's, with a side", "a match which isn't the team's, with none",
        "a side which is no side of the match", "two line-ups for one side"])
def test_a_lineups_answer_of_the_wrong_shape_is_refused(london, changes):
    answer = _lineups_answer(**{"match": _found_match(**SUNDAY_AWAY), **changes})
    assert _said(FakeHome(answer=answer), team="Liverpool", about="lineups") == NOT_GOT
