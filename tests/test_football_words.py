"""Tests for the words Claude is given to answer football questions from."""

import logging
import re
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

from voice_assistant import football_words
from voice_assistant.football import Football, Match, Reading, Team
from voice_assistant.football_words import (
    LAG, NO_LINEUP_TEAM, describe, said_competition, said_events, said_followed, said_left, said_lineups, said_match,
    said_match_report, said_table, said_team)
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


# Spain 2, England 3 on Sunday 15 November 2026, with its six events as Home Assistant gives them: Spain at home.
SPAIN_ENGLAND = {
    "id": "401", "kickoff": "2026-11-15T19:45:00+00:00", "state": "post", "phase": "full_time", "clock": "FT",
    "home": "Spain", "home_id": "164", "away": "England", "away_id": "448", "home_score": 2, "away_score": 3,
    "league": "UEFA Nations League", "winner": "away", "events": [
        {"minute": "45'", "kind": "penalty", "side": "away", "player": "Harry Kane"},
        {"minute": "53'", "kind": "goal", "side": "away", "player": "Ezri Konsa"},
        {"minute": "57'", "kind": "goal", "side": "away", "player": "Ben White"},
        {"minute": "63'", "kind": "red_card", "side": "away", "player": "Elliot Anderson"},
        {"minute": "65'", "kind": "penalty", "side": "home", "player": "Mikel Oyarzabal"},
        {"minute": "75'", "kind": "goal", "side": "home", "player": "Nico Williams"},
    ]}

# Oxford City 1, South Shields 3 on Saturday 31 October 2026, a match from BBC Sport: South Shields keep their ESPN id.
# C. Sykes, a South Shields player, put the first goal into his own net, so it counts for Oxford City, the home side -
# BBC lists it under the side it counts for, and the goals only add up to the score that way.
OXFORD_SOUTH_SHIELDS = {
    "id": "bbc:7", "kickoff": "2026-10-31T15:00:00+00:00", "state": "post", "phase": "full_time", "clock": "FT",
    "home": "Oxford City", "home_id": "bbc:x", "away": "South Shields", "away_id": "18312", "home_score": 1, "away_score": 3,
    "league": "National League North", "winner": "away", "events": [
        {"minute": "32'", "kind": "own_goal", "side": "home", "player": "C. Sykes"},
        {"minute": "50'", "kind": "goal", "side": "away", "player": "J. Smith"},
        {"minute": "61'", "kind": "goal", "side": "away", "player": "T. Jones"},
        {"minute": "78'", "kind": "penalty", "side": "away", "player": "D. Brown"},
    ]}


def _after_the_match():
    """Monday 16 November 2026 at midday, local time: the day after Spain v England."""
    return datetime(2026, 11, 16, 12, 0).astimezone()


# Newcastle United 2, Liverpool 1 in the League Cup final on Sunday 16 March 2025: a match of an earlier year.
LEAGUE_CUP_FINAL = {
    "id": "402", "kickoff": "2025-03-16T16:30:00+00:00", "state": "post", "phase": "full_time", "clock": "FT",
    "home": "Newcastle United", "home_id": "361", "away": "Liverpool", "away_id": "364", "home_score": 2, "away_score": 1,
    "league": "Carabao Cup", "winner": "home", "venue": {"name": "Wembley Stadium", "city": "London"}}


def test_a_match_of_an_earlier_year_is_said_with_its_year(london):
    # Heard on 16 November 2026, "on Sunday the 16th of March" would be this March, which hasn't a match of that name.
    assert said_match(_m(**LEAGUE_CUP_FINAL), _after_the_match()) == (
        "Newcastle United 2, Liverpool 1, Carabao Cup, on Sunday the 16th of March 2025, at Wembley Stadium in London. "
        "Newcastle United won.")


def test_a_match_of_this_year_is_said_without_it(london):
    assert said_match(_m(**NEWCASTLE_LIVERPOOL), _after_the_match()).startswith(
        "Newcastle United 2, Liverpool 2, Premier League, on Sunday the 23rd of August, at ")


def test_a_year_is_said_for_a_day_before_today_in_an_earlier_year_only(london):
    # Eight days into January, a match from the last days of December is of an earlier year.
    january = datetime(2027, 1, 5, 12, 0).astimezone()
    december = _m(**{**LEAGUE_CUP_FINAL, "kickoff": "2026-12-28T15:00:00+00:00"})
    assert said_match(december, january).startswith("Newcastle United 2, Liverpool 1, Carabao Cup, on Monday the 28th of December 2026, ")
    # A fixture is said as it always was, in the new year too, and so is a result of the year it is said in.
    christmas = datetime(2026, 12, 28, 12, 0).astimezone()
    fixture = _m(kickoff="2027-01-20T15:00:00+00:00")
    assert said_match(fixture, christmas).startswith("Liverpool v Arsenal, Premier League, on Wednesday the 20th of January at ")
    earlier = _m(**{**LEAGUE_CUP_FINAL, "kickoff": "2026-12-20T15:00:00+00:00"})
    assert said_match(earlier, christmas).startswith("Newcastle United 2, Liverpool 1, Carabao Cup, on Sunday the 20th of December, ")


def test_a_competitions_days_of_an_earlier_year_are_said_with_it_too(london):
    answer = _competition_answer([_found_match(**LEAGUE_CUP_FINAL)], days=["20250316"])
    assert said_competition(answer, _after_the_match()).split("\n")[-1] == "Days with matches: Sunday the 16th of March 2025."


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
        "Stevenage 2, Fulham U21 2, EFL Trophy, on Tuesday the 10th of November. Stevenage won on penalties, 4 to 3.")
    assert said_match(_m(**shootout, winner="away", shootout=[3, 4]), AT) == (
        "Stevenage 2, Fulham U21 2, EFL Trophy, on Tuesday the 10th of November. Fulham U21 won on penalties, 4 to 3.")
    extra = {"state": "post", "kickoff": "2026-11-10T19:00:00+00:00", "home": "Plymouth Argyle", "away": "Stevenage",
             "home_score": 3, "away_score": 2, "league": "FA Cup", "extra_time": True}
    assert said_match(_m(**extra, winner="home"), AT) == (
        "Plymouth Argyle 3, Stevenage 2, FA Cup, on Tuesday the 10th of November, after extra time. Plymouth Argyle won.")
    # Extra time and then penalties are both said.
    assert said_match(_m(**{**extra, "home_score": 2}, winner="away", shootout=[2, 4]), AT) == (
        "Plymouth Argyle 2, Stevenage 2, FA Cup, on Tuesday the 10th of November, after extra time. Stevenage won on penalties, 4 to 2.")


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


def test_a_match_says_what_happened_in_order_with_the_score_after_each_goal(london):
    assert said_events(_m(**SPAIN_ENGLAND)) == [
        "45': a penalty for England, scored by Harry Kane. Spain 0, England 1.",
        "53': a goal for England, scored by Ezri Konsa. Spain 0, England 2.",
        "57': a goal for England, scored by Ben White. Spain 0, England 3.",
        "63': a red card for Elliot Anderson of England.",
        "65': a penalty for Spain, scored by Mikel Oyarzabal. Spain 1, England 3.",
        "75': a goal for Spain, scored by Nico Williams. Spain 2, England 3.",
    ]


def test_an_own_goal(london):
    assert said_events(_m(**OXFORD_SOUTH_SHIELDS)) == [
        "32': an own goal by C. Sykes, for Oxford City. Oxford City 1, South Shields 0.",
        "50': a goal for South Shields, scored by J. Smith. Oxford City 1, South Shields 1.",
        "61': a goal for South Shields, scored by T. Jones. Oxford City 1, South Shields 2.",
        "78': a penalty for South Shields, scored by D. Brown. Oxford City 1, South Shields 3.",
    ]


def test_an_own_goal_is_counted_for_the_side_it_is_for_and_not_the_side_of_the_player(london):
    match = _m(**{**SPAIN_ENGLAND, "home_score": 1, "away_score": 0, "winner": "home", "events": [
        {"minute": "12'", "kind": "own_goal", "side": "home", "player": "Harry Maguire"}]})
    assert said_events(match) == ["12': an own goal by Harry Maguire, for Spain. Spain 1, England 0."]


def test_a_penalty_missed_and_a_red_card_leave_the_score_as_it_was(london):
    match = _m(**{**SPAIN_ENGLAND, "home_score": 0, "away_score": 1, "winner": "away", "events": [
        {"minute": "20'", "kind": "penalty_missed", "side": "home", "player": "Mikel Oyarzabal"},
        {"minute": "55'", "kind": "red_card", "side": "home", "player": "Rodri"},
        {"minute": "80'", "kind": "goal", "side": "away", "player": "Harry Kane"}]})
    assert said_events(match) == [
        "20': a penalty for Spain, missed by Mikel Oyarzabal.",
        "55': a red card for Rodri of Spain.",
        "80': a goal for England, scored by Harry Kane. Spain 0, England 1.",
    ]


def test_an_event_with_no_player_says_who_it_was_for_alone(london):
    match = _m(**{**SPAIN_ENGLAND, "home_score": 1, "away_score": 2, "events": [
        {"minute": "10'", "kind": "goal", "side": "away"},
        {"minute": "20'", "kind": "penalty", "side": "home", "player": None},
        {"minute": "30'", "kind": "own_goal", "side": "away"},
        {"minute": "40'", "kind": "penalty_missed", "side": "home"},
        {"minute": "50'", "kind": "red_card", "side": "away"}]})
    assert said_events(match) == [
        "10': a goal for England. Spain 0, England 1.",
        "20': a penalty for Spain, scored. Spain 1, England 1.",
        "30': an own goal for England. Spain 1, England 2.",
        "40': a penalty for Spain, missed.",
        "50': a red card for England.",
    ]


def test_a_penalty_scored_with_no_player_says_it_was_scored(london):
    # "a penalty for Spain." alone would not say whether it went in.
    match = _m(**{**SPAIN_ENGLAND, "home_score": 1, "away_score": 0, "winner": "home", "events": [
        {"minute": "20'", "kind": "penalty", "side": "home"}]})
    assert said_events(match) == ["20': a penalty for Spain, scored. Spain 1, England 0."]
    # With no running score to give, it is still said to have been scored.
    short = _m(**{**SPAIN_ENGLAND, "home_score": 2, "away_score": 0, "winner": "home", "events": [
        {"minute": "20'", "kind": "penalty", "side": "home"}]})
    assert said_events(short)[0] == "20': a penalty for Spain, scored."


def test_a_match_with_no_events_says_nothing_more(london):
    assert said_events(_m(**{**SPAIN_ENGLAND, "events": []})) == []
    assert said_events(_m(**{k: v for k, v in SPAIN_ENGLAND.items() if k != "events"})) == []


def test_a_match_which_has_not_started_never_has_events_said(london):
    match = _m(**{**SPAIN_ENGLAND, "state": "pre", "phase": None, "clock": "", "home_score": None, "away_score": None, "winner": None})
    assert match.events != () and said_events(match) == []


def test_a_match_in_play_says_its_events_so_far(london):
    match = _m(**{**SPAIN_ENGLAND, "state": "in", "phase": "second_half", "clock": "66'", "home_score": 1, "away_score": 3,
                  "winner": None, "events": SPAIN_ENGLAND["events"][:5]})
    assert said_events(match) == [
        "45': a penalty for England, scored by Harry Kane. Spain 0, England 1.",
        "53': a goal for England, scored by Ezri Konsa. Spain 0, England 2.",
        "57': a goal for England, scored by Ben White. Spain 0, England 3.",
        "63': a red card for Elliot Anderson of England.",
        "65': a penalty for Spain, scored by Mikel Oyarzabal. Spain 1, England 3.",
    ]


@pytest.mark.parametrize("changes", [
    {"events": SPAIN_ENGLAND["events"][:-1]},
    {"events": SPAIN_ENGLAND["events"] + [{"minute": "88'", "kind": "goal", "side": "home", "player": "Lamine Yamal"}]},
    {"home_score": 3, "away_score": 2},
    {"home_score": None, "away_score": None},
    {"home_score": None},
    {"away_score": None},
], ids=["a goal short", "a goal too many", "the sides the other way round", "no score", "no home score", "no away score"])
def test_the_running_score_is_left_out_where_it_doesnt_end_at_the_matchs_own(london, changes):
    events = changes.get("events", SPAIN_ENGLAND["events"])
    # The events are all still said, and none has a score after it: nothing false is said. (A sentence may follow them,
    # which says why: see the next test.)
    said = said_events(_m(**{**SPAIN_ENGLAND, **changes}))[:len(events)]
    assert len(said) == len(events)
    assert said[0] == "45': a penalty for England, scored by Harry Kane."
    assert said[3] == "63': a red card for Elliot Anderson of England."
    assert not any(re.search(r"[A-Za-z] \d", sentence) for sentence in said)


DONT_ADD_UP = "The goals listed don't add up to the score."


@pytest.mark.parametrize("changes", [
    {"events": SPAIN_ENGLAND["events"][:-1]},
    {"events": SPAIN_ENGLAND["events"] + [{"minute": "88'", "kind": "goal", "side": "home", "player": "Lamine Yamal"}]},
    {"home_score": 3, "away_score": 2},
], ids=["a goal short", "a goal too many", "the sides the other way round"])
def test_a_running_score_left_out_is_said_to_be_left_out(london, changes):
    match = _m(**{**SPAIN_ENGLAND, **changes})
    said = said_events(match)
    # The events come as they are, without running scores, and one more sentence ends them.
    assert said[-1] == DONT_ADD_UP
    assert not any(re.search(r"[A-Za-z] \d", sentence) for sentence in said)
    assert len(said) == len(changes.get("events", SPAIN_ENGLAND["events"])) + 1
    assert said[0] == "45': a penalty for England, scored by Harry Kane."
    # The match report has it after the events, before any notes.
    lines = said_match_report(_match_answer(match=_found_match(**{**SPAIN_ENGLAND, **changes}), notes=[PENALTY_CONCEDED]),
                              _after_the_match()).split("\n")
    assert lines[-2:] == [DONT_ADD_UP, "Note at 41': Penalty conceded by Pau Cubarsí (Spain) after a foul in the penalty area."]


@pytest.mark.parametrize("changes", [{"home_score": None, "away_score": None}, {"home_score": None}, {"away_score": None}],
                         ids=["no score", "no home score", "no away score"])
def test_a_match_with_no_score_has_nothing_to_add_up_to(london, changes):
    # Its line says "no score listed", which is why none is said after each goal.
    said = said_events(_m(**{**SPAIN_ENGLAND, **changes}))
    assert DONT_ADD_UP not in said
    assert len(said) == len(SPAIN_ENGLAND["events"])


def test_the_goals_listed_adding_up_to_the_score_say_nothing_of_it_and_no_goals_listed_say_it_another_way(london):
    assert DONT_ADD_UP not in said_events(_m(**SPAIN_ENGLAND))
    # A red card alone, with goals on the score, is a match whose scorers aren't listed, not one whose goals don't add up.
    red_only = _m(**{**SPAIN_ENGLAND, "events": SPAIN_ENGLAND["events"][3:4]})
    assert said_events(red_only) == ["63': a red card for Elliot Anderson of England."]


def test_nothing_said_of_what_happened_has_a_score_as_digits_with_a_dash(london):
    AT = _after_the_match()
    for found in (SPAIN_ENGLAND, OXFORD_SOUTH_SHIELDS):
        said = (said_events(_m(**found)) + [said_competition(_competition_answer([_found_match(**found)]), AT)]
                + [said_match_report({"team": {"id": "448", "name": "England"}, "stale": False, "match": found}, AT)])
        assert not any(re.search(r"\d\s*[-\u2013]\s*\d", words) for words in said)


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
        "Last: Stevenage 2, Fulham U21 2, EFL Trophy, on Tuesday the 10th of November. Stevenage won on penalties, 4 to 3.")


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
              _found_match(id="13", kickoff="2026-11-28T15:00:00+00:00", home="Everton", away="Liverpool", no_odds=True)],
        last=[_found_match(**PALACE_LOSS)],
        tables=[{"league": "Premier League", "group": "", "rank": 4, "of": 20, "played": 10, "won": 5, "drawn": 3, "lost": 2,
                 "gd": 4, "points": 18}])
    assert said_team(answer, AT).split("\n") == [
        "Liverpool:",
        "Playing now: Liverpool 2, Manchester United 1, Premier League, in play, second half, 67'.",
        "Next: Arsenal v Liverpool, Premier League, on Sunday the 22nd of November at 4:30 PM, at Emirates Stadium in London. "
        "Bookmakers' odds from DraftKings, as percentages: Arsenal 30, draw 25, Liverpool 45.",
        "Later: Everton v Liverpool, Premier League, on Saturday the 28th of November at 3:00 PM. No odds are listed for it.",
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


def test_a_team_home_assistant_doesnt_cover(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    note = "Home Assistant's football doesn't seem to cover the league South Shields play in, so only their cup ties show."
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
    said = said_team(_team_answer(next=[_found_match(**SUNDAY_AWAY, no_odds=True)]), AT)
    assert said.split("\n")[1] == (
        "Next: Arsenal v Liverpool, Premier League, on Sunday the 22nd of November at 4:30 PM. No odds are listed for it.")
    # A match which isn't to come, or which is off, has no odds to list, whatever Home Assistant says of them.
    done = said_team(_team_answer(last=[_found_match(**{**PALACE_LOSS, "no_odds": True})],
                                  next=[_found_match(**{**SUNDAY_AWAY, "phase": "off", "no_odds": True})]), AT)
    assert "odds" not in done
    listed = {**SUNDAY_AWAY, "odds": {"home": 30, "draw": 25, "away": 45, "source": "DraftKings"}}
    assert "No odds are listed" not in said_team(_team_answer(next=[_found_match(**listed)]), AT)


def test_odds_are_said_only_of_the_fixtures_home_assistant_looked_up(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    days = ("2026-11-22", "2026-11-25", "2026-11-28", "2026-12-02", "2026-12-05")
    fixtures = [_found_match(id=str(place), kickoff=f"{day}T16:30:00+00:00", home="Arsenal", away="Liverpool")
                for place, day in enumerate(days)]
    fixtures[0]["odds"] = {"home": 30, "draw": 25, "away": 45, "source": "DraftKings"}
    fixtures[1]["no_odds"] = True
    lines = said_team(_team_answer(next=fixtures), AT).split("\n")
    assert lines[1].endswith("Bookmakers' odds from DraftKings, as percentages: Arsenal 30, draw 25, Liverpool 45.")
    assert lines[2] == ("Later: Arsenal v Liverpool, Premier League, on Wednesday the 25th of November at 4:30 PM. "
                        "No odds are listed for it.")
    # The three never looked up say nothing of their odds, one way or the other.
    for line, day in zip(lines[3:6], ("Saturday the 28th of November", "Wednesday the 2nd of December", "Saturday the 5th of December")):
        assert line == f"Later: Arsenal v Liverpool, Premier League, on {day} at 4:30 PM."
    assert sum("odds" in line.lower() for line in lines) == 2


def test_a_fixture_from_a_script_which_says_nothing_of_odds_has_none_said(london):
    # Home Assistant's script from before round five never wrote no_odds: what it didn't look up, or found none for, is
    # told no differently, so nothing is said of odds for it, in place of the sentence which was said of every one.
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    plain = _found_match(**SUNDAY_AWAY)
    assert "no_odds" not in plain
    assert said_team(_team_answer(next=[plain]), AT).split("\n")[1] == f"Next: {said_match(_m(**plain), AT)}"
    assert "odds" not in said_competition(_competition_answer([_found_match(**plain)]), AT)
    assert "odds" not in said_left(_left_answer(matches=[_found_match(**plain)]), AT)


def test_a_competitions_matches_say_no_odds_are_listed_only_where_home_assistant_does(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    looked = _found_match(id="1", kickoff="2026-11-24T20:00:00+00:00", home="Real Madrid", away="Arsenal",
                          league="UEFA Champions League", no_odds=True)
    unlooked = _found_match(id="2", kickoff="2026-11-25T20:00:00+00:00", home="Bayern Munich", away="Napoli",
                            league="UEFA Champions League")
    lines = said_competition(_competition_answer([looked, unlooked], days=["20261124", "20261125"]), AT).split("\n")
    assert lines[0].endswith("at 8:00 PM. No odds are listed for it.") and lines[1].endswith("at 8:00 PM.")
    # Who is left in a cup lists its matches the same way.
    lines = said_left(_left_answer(matches=[_found_match(**{**SUNDAY_AWAY, "league": "Carabao Cup", "no_odds": True}),
                                            _found_match(**{**SUNDAY_AWAY, "id": "11", "league": "Carabao Cup"})]), AT).split("\n")
    assert lines[1].endswith("No odds are listed for it.") and lines[2].endswith("at 4:30 PM.")


def test_the_overview_never_says_anything_of_odds(london):
    # The sensor never carries them, so what it gives is said as it always was, even with no_odds on a match.
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    match = _found_match(**SUNDAY_AWAY, no_odds=True)
    said = said_followed(_reading(_team("364", "Liverpool", next=[match])), AT)
    assert said == f"Liverpool: Next: {said_match(_m(**SUNDAY_AWAY), AT)}"
    assert "odds" not in said


@pytest.mark.parametrize("changes", [
    {"no_odds": "true"}, {"no_odds": 1}, {"no_odds": ["true"]},
    {"no_odds": True, "odds": {"home": 30, "draw": 25, "away": 45, "source": "DraftKings"}},
], ids=["text", "a number", "a list", "beside odds"])
def test_no_odds_of_the_wrong_shape_refuses_the_answer(london, changes):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    bad = _found_match(**SUNDAY_AWAY, **changes)
    answers = [("team", _team_answer(next=[bad])), ("competition", _competition_answer([bad])), ("left", _left_answer(matches=[bad]))]
    for what, answer in answers:
        with pytest.raises((KeyError, TypeError, ValueError)):
            {"team": said_team, "competition": said_competition, "left": said_left}[what](answer, AT)
        asked = {"team": {"team": "Liverpool"}, "competition": {"competition": "Champions League"},
                 "left": {"competition": "Carabao Cup", "about": "left"}}[what]
        assert _said(FakeHome(answer=answer), **asked) == NOT_GOT


def test_a_bbc_match_is_read_and_said(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    # As the Contract has one from BBC Sport: its own ids and slug, the followed side by its ESPN id, no ground and no odds.
    found = _found_match(id="bbc:abc", slug="bbc:national-league-north", league="National League North", home="Worksop Town",
                         home_id="bbc:x", away="South Shields", away_id="18312", kickoff="2026-11-21T15:00:00+00:00")
    match = Match.from_home(found)
    assert (match.id, match.home_id, match.away_id, match.venue, match.odds) == ("bbc:abc", "bbc:x", "18312", None, None)
    assert said_match(match, AT) == "Worksop Town v South Shields, National League North, on Saturday the 21st of November at 3:00 PM."
    # A match to come from a team's schedule alone says nothing of odds, which nobody looked up.
    south_shields = {"id": "18312", "name": "South Shields", "followed": True}
    assert said_team(_team_answer(team=south_shields, next=[found]), AT).split("\n")[1] == (
        "Next: Worksop Town v South Shields, National League North, on Saturday the 21st of November at 3:00 PM.")
    # In play and over, it is said by the score, as any other is.
    live = _m(**{**found, "kickoff": "2026-11-13T11:00:00+00:00", "state": "in", "phase": "second_half", "clock": "67'",
                 "home_score": 0, "away_score": 1})
    assert said_match(live, AT) == "Worksop Town 0, South Shields 1, National League North, in play, second half, 67'."
    over = _m(**{**found, "kickoff": "2026-11-07T15:00:00+00:00", "state": "post", "phase": "full_time", "clock": "FT",
                 "home_score": 0, "away_score": 2, "winner": "away"})
    assert said_match(over, AT) == "Worksop Town 0, South Shields 2, National League North, on Saturday the 7th of November. South Shields won."


def test_a_competitions_days(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    first = _found_match(id="1", kickoff="2026-11-24T20:00:00+00:00", home="Real Madrid", away="Arsenal",
                         league="UEFA Champions League", venue={"name": "Santiago Bernabeu", "city": "Madrid"},
                         odds={"home": 40, "draw": 25, "away": 35, "source": "DraftKings"})
    second = _found_match(id="2", kickoff="2026-11-25T20:00:00+00:00", home="Bayern Munich", away="Man City",
                          away_name="Manchester City", league="UEFA Champions League", no_odds=True)
    answer = _competition_answer([first, second], days=["20261124", "20261125"])
    assert said_competition(answer, AT).split("\n") == [
        "Real Madrid v Arsenal, UEFA Champions League, on Tuesday the 24th of November at 8:00 PM, at Santiago Bernabeu in Madrid. "
        "Bookmakers' odds from DraftKings, as percentages: Real Madrid 40, draw 25, Arsenal 35.",
        "Bayern Munich v Manchester City, UEFA Champions League, on Wednesday the 25th of November at 8:00 PM. "
        "No odds are listed for it.",
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
    match = _found_match(kickoff="2026-12-02T20:00:00+00:00", league="Carabao Cup", no_odds=True)
    answer = _competition_answer([match], days=["20261202"], competition={"slug": "eng.league_cup", "name": "Carabao Cup", "round": "Quarterfinals"})
    assert said_competition(answer, AT).split("\n") == [
        "The Carabao Cup is at the Quarterfinals.",
        "Liverpool v Arsenal, Carabao Cup, on Wednesday the 2nd of December at 8:00 PM. No odds are listed for it.",
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


NATIONS_LEAGUE = {"slug": "uefa.nations", "name": "UEFA Nations League"}


def test_a_round_up_says_each_started_matchs_goals_after_it_on_the_same_line(london):
    AT = _after_the_match()
    answer = _competition_answer([_found_match(**SPAIN_ENGLAND)], days=["20261115"], competition=NATIONS_LEAGUE)
    assert said_competition(answer, AT).split("\n") == [
        "Spain 2, England 3, UEFA Nations League, on Sunday the 15th of November. England won. "
        "Goals: Harry Kane 45' (pen), Ezri Konsa 53', Ben White 57' for England; "
        "Mikel Oyarzabal 65' (pen), Nico Williams 75' for Spain. Red card: Elliot Anderson (England) 63'.",
        "Days with matches: Sunday the 15th of November.",
    ]


def test_a_round_up_lists_an_own_goal_under_the_side_it_counts_for(london):
    answer = _competition_answer([_found_match(**OXFORD_SOUTH_SHIELDS)], days=["20261031"])
    assert said_competition(answer, _after_the_match()).split("\n")[0] == (
        "Oxford City 1, South Shields 3, National League North, on Saturday the 31st of October. South Shields won. "
        "Goals: C. Sykes 32' (og) for Oxford City; J. Smith 50', T. Jones 61', D. Brown 78' (pen) for South Shields.")


def test_a_round_up_says_what_there_is_to_say_of_a_match_and_nothing_more(london):
    AT = _after_the_match()
    no_goals = {**SPAIN_ENGLAND, "home_score": 0, "away_score": 0, "winner": None, "events": [
        {"minute": "63'", "kind": "red_card", "side": "away", "player": "Elliot Anderson"},
        {"minute": "70'", "kind": "red_card", "side": "home", "player": "Rodri"},
        {"minute": "80'", "kind": "penalty_missed", "side": "home", "player": "Mikel Oyarzabal"}]}
    # Red cards are said as they are, a penalty missed is not a goal, and with no goals there is no "Goals".
    assert said_competition(_competition_answer([_found_match(**no_goals)]), AT).split("\n")[0].endswith(
        "It was a draw. Red cards: Elliot Anderson (England) 63', Rodri (Spain) 70'.")
    # A match with no events and no goals is said as it was before this round.
    plain = _found_match(**{**SPAIN_ENGLAND, "home_score": 0, "away_score": 0, "winner": None, "events": []})
    assert said_competition(_competition_answer([plain]), AT).split("\n")[0] == said_match(_m(**plain), AT)
    # And one not started has none, whatever it is given.
    to_come = _found_match(**{**SPAIN_ENGLAND, "state": "pre", "phase": None, "clock": "", "home_score": None,
                              "away_score": None, "winner": None, "kickoff": "2026-11-24T19:45:00+00:00", "no_odds": True})
    said = said_competition(_competition_answer([to_come]), AT).split("\n")[0]
    assert said == said_match(_m(**to_come), AT) + " No odds are listed for it." and "Goals" not in said


def test_a_round_up_with_no_player_says_the_minute_alone(london):
    match = {**SPAIN_ENGLAND, "home_score": 1, "away_score": 1, "winner": None, "events": [
        {"minute": "10'", "kind": "goal", "side": "away"}, {"minute": "20'", "kind": "penalty", "side": "home"},
        {"minute": "30'", "kind": "red_card", "side": "away"}]}
    assert said_competition(_competition_answer([_found_match(**match)]), _after_the_match()).split("\n")[0].endswith(
        "It was a draw. Goals: 10' for England; 20' (pen) for Spain. Red card: England 30'.")


# What a match with goals on its score and none among its events is said to have: BBC's Buxton 0, South Shields 2 has no
# actions, and a board kept from before Home Assistant could give events has none for up to six hours.
NO_SCORERS = "Its scorers aren't listed."
RED_AND_MISSED = [{"minute": "63'", "kind": "red_card", "side": "away", "player": "Elliot Anderson"},
                  {"minute": "80'", "kind": "penalty_missed", "side": "home", "player": "Mikel Oyarzabal"}]


@pytest.mark.parametrize("events, after", [
    ([], ""), (None, ""), (RED_AND_MISSED, " Red card: Elliot Anderson (England) 63'."),
], ids=["an empty list", "no events key", "only a red card and a penalty missed"])
def test_a_round_up_says_the_scorers_of_a_match_with_goals_and_no_goal_listed_arent_listed(london, events, after):
    found = {key: value for key, value in SPAIN_ENGLAND.items() if key != "events"}
    if events is not None:
        found["events"] = events
    line = said_competition(_competition_answer([found], days=["20261115"], competition=NATIONS_LEAGUE), _after_the_match()).split("\n")[0]
    assert line == said_match(_m(**found), _after_the_match()) + after + " " + NO_SCORERS


def test_a_round_up_says_the_scorers_of_a_match_in_play_with_a_goal_and_none_listed_arent_listed(london):
    live = {**SPAIN_ENGLAND, "state": "in", "phase": "second_half", "clock": "66'", "home_score": 1, "away_score": 0,
            "winner": None, "events": []}
    lines = said_competition(_competition_answer([live], days=["20261115"]), _after_the_match()).split("\n")
    assert lines == [said_match(_m(**live), _after_the_match()) + " " + NO_SCORERS, "Days with matches: Sunday the 15th of November.", LAG]


def test_a_round_up_says_nothing_of_scorers_where_there_are_no_goals_or_the_goals_are_listed(london):
    AT = _after_the_match()
    goalless = {**SPAIN_ENGLAND, "home_score": 0, "away_score": 0, "winner": None}
    for events in ([], None, RED_AND_MISSED):
        found = {key: value for key, value in goalless.items() if key != "events"}
        if events is not None:
            found["events"] = events
        assert NO_SCORERS not in said_competition(_competition_answer([found]), AT)
    # Nor of a match whose goals are listed, nor of one still to come, which has no score.
    assert NO_SCORERS not in said_competition(_competition_answer([_found_match(**SPAIN_ENGLAND)]), AT)
    assert NO_SCORERS not in said_competition(_competition_answer([_found_match()]), AT)


@pytest.mark.parametrize("name", [
    "Harry Kane", "C. Sykes", "Rodri", "Virgil van Dijk", "Vinícius Júnior", "Son Heung-min", "João Pedro", "Pedro Neto",
])
def test_a_scorer_is_said_as_the_source_gives_the_name_in_a_round_up(london, name):
    # No rule finds a surname without a list of names, and "Vinícius Júnior" or "Son Heung-min" would be misnamed.
    match = {**SPAIN_ENGLAND, "home_score": 0, "away_score": 1, "events": [
        {"minute": "10'", "kind": "goal", "side": "away", "player": name},
        {"minute": "20'", "kind": "red_card", "side": "home", "player": name}]}
    assert said_competition(_competition_answer([_found_match(**match)]), _after_the_match()).split("\n")[0].endswith(
        f"Goals: {name} 10' for England. Red card: {name} (Spain) 20'.")


def test_a_round_up_with_events(london):
    AT = _after_the_match()
    pairs = [("Arsenal", "Chelsea"), ("Liverpool", "Everton"), ("Manchester City", "Manchester United"),
             ("Tottenham Hotspur", "West Ham United"), ("Newcastle United", "Aston Villa"), ("Brighton", "Fulham"),
             ("Brentford", "Wolves"), ("Crystal Palace", "Bournemouth"), ("Leeds United", "Burnley"),
             ("Sunderland", "Nottingham Forest"), ("Real Madrid", "Barcelona"), ("Atletico Madrid", "Sevilla"),
             ("Bayern Munich", "Borussia Dortmund"), ("Juventus", "AC Milan"), ("Inter Milan", "Napoli"),
             ("Paris Saint-Germain", "Marseille"), ("Ajax", "PSV"), ("Benfica", "Porto")]
    found, expected = [], []
    for place, (home, away) in enumerate(pairs):
        match = {"id": str(place), "kickoff": "2026-11-24T20:00:00+00:00", "state": "post", "phase": "full_time", "clock": "FT",
                 "home": home, "away": away, "home_id": f"h{place}", "away_id": f"a{place}", "league": "UEFA Champions League"}
        if place % 3 == 0:
            match.update(home_score=2, away_score=1, winner="home", events=[
                {"minute": "12'", "kind": "goal", "side": "home", "player": "Tom Hart"},
                {"minute": "40'", "kind": "penalty", "side": "away", "player": "Sam Lowe"},
                {"minute": "77'", "kind": "goal", "side": "home", "player": "Tom Hart"}])
            goals = f" Goals: Tom Hart 12', Tom Hart 77' for {home}; Sam Lowe 40' (pen) for {away}."
        elif place % 3 == 1:
            match.update(home_score=0, away_score=0, events=[])
            goals = ""
        else:
            match.update(home_score=1, away_score=0, winner="home", events=[
                {"minute": "5'", "kind": "goal", "side": "home", "player": "Joe Reed"},
                {"minute": "88'", "kind": "red_card", "side": "away", "player": "Max Dunn"}])
            goals = f" Goals: Joe Reed 5' for {home}. Red card: Max Dunn ({away}) 88'."
        found.append(match)
        expected.append(said_match(_m(**match), AT) + goals)
    lines = said_competition(_competition_answer(found, days=["20261124"]), AT).split("\n")
    # Every match is still one line: its result, then its goals; and the days come after all of them.
    assert lines[:18] == expected
    assert lines[18:] == ["Days with matches: Tuesday the 24th of November."]
    assert sum("Goals:" in line for line in lines) == 12


def test_a_round_up_with_a_match_in_play_has_its_goals_so_far_and_the_warning_last(london):
    live = {**SPAIN_ENGLAND, "state": "in", "phase": "second_half", "clock": "66'", "home_score": 1, "away_score": 3,
            "winner": None, "events": SPAIN_ENGLAND["events"][:5]}
    lines = said_competition(_competition_answer([_found_match(**live)], days=["20261115"]), _after_the_match()).split("\n")
    assert lines == [
        "Spain 1, England 3, UEFA Nations League, in play, second half, 66'. "
        "Goals: Harry Kane 45' (pen), Ezri Konsa 53', Ben White 57' for England; Mikel Oyarzabal 65' (pen) for Spain. "
        "Red card: Elliot Anderson (England) 63'.",
        "Days with matches: Sunday the 15th of November.",
        LAG,
    ]


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
    answer = _left_answer(teams=["Arsenal", "Liverpool"],
                          matches=[live, _found_match(**{**SUNDAY_AWAY, "league": "Carabao Cup", "no_odds": True})])
    assert said_left(answer, AT).split("\n") == [
        "Still in the Carabao Cup: Arsenal and Liverpool.",
        "Liverpool 2, Manchester United 1, Carabao Cup, in play, second half, 67'.",
        "Arsenal v Liverpool, Carabao Cup, on Sunday the 22nd of November at 4:30 PM. No odds are listed for it.",
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
    assert lines[1] == ("Liverpool's starting eleven (4 2 3 1): Alisson, Frimpong, Konate, Van Dijk, Kerkez, "
                        "Gravenberch, Mac Allister, Salah, Szoboszlai, Gakpo and Isak. Bench: Mamardashvili and Gomez.")
    assert lines[2].startswith("Arsenal's starting eleven: Raya,")
    assert lines[2].endswith("Bench: none listed.")


@pytest.mark.parametrize("formation, said", [
    ("4-2-3-1", "4 2 3 1"), ("3-4-2-1", "3 4 2 1"), ("4 \u2013 4 \u2013 2", "4 4 2"),
    # Only a dash between digits is swapped: anything else in the formation stays as it was.
    ("4-4-2 box-to-box", "4 4 2 box-to-box"), ("Back-3 (5-2-3)", "Back-3 (5 2 3)"), ("4231", "4231"),
], ids=["four digits", "four digits again", "spaced en dashes", "words with dashes", "a word and digits", "no dash"])
def test_a_formation_is_said_without_dashes_as_they_are_read_out(london, formation, said):
    side = {**LIVERPOOL_SIDE, "formation": formation}
    lines = said_lineups(_lineups_answer(match=_found_match(**SUNDAY_AWAY), sides=[side]), AT.astimezone()).split("\n")
    assert lines[1].startswith(f"Liverpool's starting eleven ({said}): Alisson,")


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
    assert lines[1].startswith("Liverpool's starting eleven (4 2 3 1): Alisson,")
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
    assert lines[1].startswith("Manchester United's starting eleven (3 4 3): Raya,")
    assert lines[2].startswith("Liverpool's starting eleven (4 2 3 1): Alisson,")
    # Where one side is out, the other is named as the match names it
    lines = said_lineups(_lineups_answer(team=team, match=match, sides=[LIVERPOOL_SIDE]), AT.astimezone()).split("\n")
    assert lines[-1] == "Manchester United's line-up isn't out yet."
    # A side is placed in the match by either of the names the match gives it
    short = {**UNITED_SIDE, "team_name": None}
    lines = said_lineups(_lineups_answer(team=team, match=match, sides=[short]), AT.astimezone()).split("\n")
    assert lines[1].startswith("Man Utd's starting eleven (3 4 3): Raya,")
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


def _match_answer(**fields):
    """A football_ask answer for a team's match, as the Contract has it, with no match unless a test gives one."""
    return {"team": {"id": "448", "name": "England", "followed": False}, "stale": False, **fields}


PENALTY_CONCEDED = {"minute": "41'", "text": "Penalty conceded by Pau Cubarsí (Spain) after a foul in the penalty area."}
VAR_NOTE = {"minute": "88'", "text": "VAR Decision: No Penalty England."}


def test_a_match_report_says_the_match_then_its_events_then_the_notes(london):
    answer = _match_answer(match=_found_match(**SPAIN_ENGLAND), notes=[PENALTY_CONCEDED, VAR_NOTE])
    assert said_match_report(answer, _after_the_match()).split("\n") == [
        "Spain 2, England 3, UEFA Nations League, on Sunday the 15th of November. England won.",
        "45': a penalty for England, scored by Harry Kane. Spain 0, England 1.",
        "53': a goal for England, scored by Ezri Konsa. Spain 0, England 2.",
        "57': a goal for England, scored by Ben White. Spain 0, England 3.",
        "63': a red card for Elliot Anderson of England.",
        "65': a penalty for Spain, scored by Mikel Oyarzabal. Spain 1, England 3.",
        "75': a goal for Spain, scored by Nico Williams. Spain 2, England 3.",
        "Note at 41': Penalty conceded by Pau Cubarsí (Spain) after a foul in the penalty area.",
        "Note at 88': VAR Decision: No Penalty England.",
    ]


def test_a_match_report_with_no_notes_or_events_is_the_match_alone(london):
    AT = _after_the_match()
    assert said_match_report(_match_answer(match=_found_match(**SPAIN_ENGLAND)), AT).split("\n")[-1] == (
        "75': a goal for Spain, scored by Nico Williams. Spain 2, England 3.")
    bare = _found_match(**{**SPAIN_ENGLAND, "home_score": 0, "away_score": 0, "winner": None, "events": []})
    assert said_match_report(_match_answer(match=bare, notes=[]), AT) == said_match(_m(**bare), AT)
    # A match which hasn't started is said as it is, with its odds if it has them, and never with events.
    coming = _found_match(**{**SPAIN_ENGLAND, "state": "pre", "phase": None, "clock": "", "home_score": None,
                             "away_score": None, "winner": None, "kickoff": "2026-11-24T19:45:00+00:00"})
    assert said_match_report(_match_answer(match=coming), AT) == said_match(_m(**coming), AT)


@pytest.mark.parametrize("events, then", [
    ([], []), (None, []), (RED_AND_MISSED, ["63': a red card for Elliot Anderson of England.", "80': a penalty for Spain, missed by Mikel Oyarzabal."]),
], ids=["an empty list", "no events key", "only a red card and a penalty missed"])
def test_a_match_report_says_the_scorers_of_a_match_with_goals_and_no_goal_listed_arent_listed(london, events, then):
    AT = _after_the_match()
    found = {key: value for key, value in SPAIN_ENGLAND.items() if key != "events"}
    if events is not None:
        found["events"] = events
    said = said_match_report(_match_answer(match=found, notes=[PENALTY_CONCEDED]), AT).split("\n")
    # After the match line, then what is listed, then the notes.
    assert said == [said_match(_m(**found), AT), NO_SCORERS, *then,
                    "Note at 41': Penalty conceded by Pau Cubarsí (Spain) after a foul in the penalty area."]


def test_a_match_report_says_nothing_of_scorers_for_a_goalless_match_or_one_whose_goals_are_listed(london):
    AT = _after_the_match()
    goalless = {**SPAIN_ENGLAND, "home_score": 0, "away_score": 0, "winner": None, "events": []}
    assert said_match_report(_match_answer(match=goalless), AT) == said_match(_m(**goalless), AT)
    assert NO_SCORERS not in said_match_report(_match_answer(match=SPAIN_ENGLAND), AT)


def test_a_match_report_of_a_match_which_has_not_started_says_no_notes(london):
    AT = _after_the_match()
    coming = _found_match(**{**SPAIN_ENGLAND, "state": "pre", "phase": None, "clock": "", "home_score": None,
                             "away_score": None, "winner": None, "kickoff": "2026-11-24T19:45:00+00:00"})
    answer = _match_answer(match=coming, notes=[PENALTY_CONCEDED])
    # As the events are not said, whatever it is given, nor are the notes: only the match is.
    assert said_match_report(answer, AT) == said_match(_m(**coming), AT)
    # Once it has started, they are.
    started = _found_match(**{**SPAIN_ENGLAND, "events": []})
    assert said_match_report({**answer, "match": started}, AT).split("\n")[-1] == (
        "Note at 41': Penalty conceded by Pau Cubarsí (Spain) after a foul in the penalty area.")


@pytest.mark.parametrize("state", ["in", "post"])
def test_a_match_report_of_a_match_which_is_off_says_no_events_or_notes(london, state):
    AT = _after_the_match()
    # Called off after kick-off, with what Home Assistant had of it: nothing happened in it which is said.
    off = _found_match(**{**SPAIN_ENGLAND, "state": state, "phase": "off", "clock": "Abandoned", "winner": None})
    assert off["events"] and said_events(_m(**off)) == []
    assert said_match_report(_match_answer(match=off, notes=[PENALTY_CONCEDED, VAR_NOTE]), AT) == said_match(_m(**off), AT)
    # Nor are its scorers said not to be listed, with goals on its score and none of them listed.
    bare = {**off, "events": []}
    assert said_match_report(_match_answer(match=bare, notes=[PENALTY_CONCEDED]), AT) == said_match(_m(**bare), AT)


@pytest.mark.parametrize("state", ["in", "post"])
def test_a_round_up_of_a_match_which_is_off_says_the_match_alone(london, state):
    AT = _after_the_match()
    off = {**SPAIN_ENGLAND, "state": state, "phase": "off", "clock": "Abandoned", "winner": None}
    for events in (SPAIN_ENGLAND["events"], []):
        found = _found_match(**{**off, "events": events})
        assert said_competition(_competition_answer([found], days=["20261115"]), AT).split("\n")[0] == said_match(_m(**found), AT)


def test_a_match_report_of_a_match_in_play_ends_with_the_warning(london):
    live = {**SPAIN_ENGLAND, "state": "in", "phase": "second_half", "clock": "66'", "home_score": 1, "away_score": 3,
            "winner": None, "events": SPAIN_ENGLAND["events"][:5]}
    lines = said_match_report(_match_answer(match=_found_match(**live), notes=[PENALTY_CONCEDED]), _after_the_match()).split("\n")
    assert lines[0] == "Spain 1, England 3, UEFA Nations League, in play, second half, 66'."
    assert lines[1:6] == said_events(_m(**live))
    assert lines[6:] == ["Note at 41': Penalty conceded by Pau Cubarsí (Spain) after a foul in the penalty area.", LAG]
    # Over, it has no warning.
    assert LAG not in said_match_report(_match_answer(match=_found_match(**SPAIN_ENGLAND)), _after_the_match())


def test_a_match_report_with_no_match(london):
    AT = _after_the_match()
    assert said_match_report(_match_answer(), AT) == "No match is listed for England."
    assert said_match_report(_match_answer(), AT, on_a_day=True) == "No match is listed for England on that day."
    team = {"id": "448", "name": "England", "full_name": "England Women", "followed": False}
    assert said_match_report(_match_answer(team=team, notes=[VAR_NOTE]), AT) == "No match is listed for England Women."


# Newcastle United 2, Liverpool 2 on Sunday 23 August 2026 at St James' Park: the last time the two met.
NEWCASTLE_LIVERPOOL = {
    "id": "501", "kickoff": "2026-08-23T15:30:00+00:00", "state": "post", "phase": "full_time", "clock": "FT",
    "home": "Newcastle United", "home_id": "361", "away": "Liverpool", "away_id": "364", "home_score": 2, "away_score": 2,
    "league": "Premier League", "venue": {"name": "St James' Park", "city": "Newcastle upon Tyne"}, "events": [
        {"minute": "12'", "kind": "goal", "side": "away", "player": "Mohamed Salah"},
        {"minute": "40'", "kind": "goal", "side": "home", "player": "Alexander Isak"},
        {"minute": "66'", "kind": "goal", "side": "home", "player": "Anthony Gordon"},
        {"minute": "88'", "kind": "goal", "side": "away", "player": "Cody Gakpo"},
    ]}


def _liverpool_answer(**fields):
    """A football_ask answer for Liverpool's match, as the Contract has it, with no match unless a test gives one."""
    return _match_answer(team={"id": "364", "name": "Liverpool", "followed": True}, **fields)


# The other team as Home Assistant found it, for a match asked against Newcastle.
NEWCASTLE = {"id": "361", "name": "Newcastle", "full_name": "Newcastle United", "followed": False}


def _met_answer(seasons, **fields):
    """A football_ask answer for Liverpool's match asked against Newcastle, with the seasons looked through."""
    return _liverpool_answer(seasons=seasons, against=NEWCASTLE, **fields)


def test_a_match_found_for_the_other_team_is_said_as_any_match_is(london):
    AT = _after_the_match()
    asked = _met_answer(1, match=_found_match(**NEWCASTLE_LIVERPOOL), notes=[PENALTY_CONCEDED])
    plain = {key: value for key, value in asked.items() if key not in ("seasons", "against")}
    said = said_match_report(asked, AT, against="Newcastle")
    assert said == said_match_report(plain, AT)
    assert said.split("\n")[0] == ("Newcastle United 2, Liverpool 2, Premier League, on Sunday the 23rd of August, "
                                   "at St James' Park in Newcastle upon Tyne. It was a draw.")
    assert said.split("\n")[1] == "12': a goal for Liverpool, scored by Mohamed Salah. Newcastle United 0, Liverpool 1."


@pytest.mark.parametrize("seasons, said", [
    (6, "No match between Liverpool and Newcastle United is listed in the last 6 seasons."),
    (3, "No match between Liverpool and Newcastle United is listed in the last 3 seasons."),
    (2, "No match between Liverpool and Newcastle United is listed in the last 2 seasons."),
    # A national side has one list with no seasons in it, so "the last 1 seasons" is never said.
    (1, "No match between Liverpool and Newcastle United is among ESPN's results for Liverpool."),
])
def test_no_match_for_the_other_team_says_how_far_back_it_was_looked_for(london, seasons, said):
    assert said_match_report(_met_answer(seasons), _after_the_match(), against="Newcastle") == said


def test_no_match_for_the_other_team_is_never_said_as_never_having_played(london):
    said = said_match_report(_met_answer(6), _after_the_match(), against="Newcastle")
    assert not re.search(r"never|haven't|have not|not played|no games", said, re.IGNORECASE)


def test_no_match_for_the_other_team_names_the_team_by_its_full_name(london):
    team = {"id": "364", "name": "Liverpool", "full_name": "Liverpool FC", "followed": True}
    assert said_match_report(_match_answer(team=team, seasons=4, against=NEWCASTLE), _after_the_match(),
                             against="Newcastle") == "No match between Liverpool FC and Newcastle United is listed in the last 4 seasons."
    # With no full name, the name, as the team's own line is.
    wales = {"id": "217", "name": "Wales", "followed": False}
    assert said_match_report(_match_answer(seasons=1, against=wales), _after_the_match(), against="Wales") == (
        "No match between England and Wales is among ESPN's results for England.")


def test_no_match_for_the_other_team_says_the_team_espn_found_not_the_name_asked(london):
    # ESPN's search lists Stafford Rangers before Rangers, so a name asked is not always the team found: said as asked,
    # the match looked for would be hidden as the wrong team's.
    found = {"id": "2999", "name": "Stafford Rangers", "followed": False}
    said = said_match_report(_liverpool_answer(seasons=6, against=found), _after_the_match(), against="Rangers")
    assert said == "No match between Liverpool and Stafford Rangers is listed in the last 6 seasons."


def test_the_other_team_may_be_one_only_bbc_sport_knows(london):
    # South Shields' league rivals are only in BBC's pages: their id begins "bbc:", as a match's may, and the one list
    # which is looked through is the results Home Assistant keeps.
    rival = {"id": "bbc:x", "name": "Oxford City", "followed": False}
    shields = {"id": "18312", "name": "South Shields", "followed": True}
    found = _match_answer(team=shields, seasons=1, against=rival, match=_found_match(**OXFORD_SOUTH_SHIELDS))
    said = said_match_report(found, _after_the_match(), against="Oxford City")
    assert said == said_match_report({key: value for key, value in found.items() if key not in ("seasons", "against")},
                                     _after_the_match())
    assert said.split("\n")[0].startswith("Oxford City 1, South Shields 3, National League North, on Saturday the 31st of October")
    none = _match_answer(team=shields, seasons=1, against=rival)
    assert said_match_report(none, _after_the_match(), against="Oxford City") == (
        "No match between South Shields and Oxford City is among ESPN's results for South Shields.")


def test_no_match_for_a_national_side_is_among_the_results_espn_has_for_it(london):
    # A national side's one list has no seasons in it, so what was looked through is ESPN's results for the side.
    brazil = {"id": "205", "name": "Brazil", "followed": False}
    assert said_match_report(_match_answer(seasons=1, against=brazil), _after_the_match(), against="Brazil") == (
        "No match between England and Brazil is among ESPN's results for England.")
    # It is the team's name which ends it, as the team's own lines say it, and the other team as found.
    england = {"id": "448", "name": "England", "full_name": "England Men", "followed": False}
    assert said_match_report(_match_answer(team=england, seasons=1, against=brazil), _after_the_match(), against="Brazil") == (
        "No match between England Men and Brazil is among ESPN's results for England Men.")


# Liverpool as ESPN names it: the same team, found again under the other name.
LIVERPOOL_FC = {"id": "364", "name": "Liverpool", "full_name": "Liverpool FC", "followed": False}


@pytest.mark.parametrize("seasons", [1, 6])
def test_a_team_asked_against_itself_under_another_name_is_said_to_be_the_same_team(london, seasons):
    # "Liverpool FC" is not "Liverpool" to the words, which keep no list of names: the two ids are what say it.
    answer = _liverpool_answer(seasons=seasons, against=LIVERPOOL_FC)
    assert said_match_report(answer, _after_the_match(), against="Liverpool FC") == "Liverpool and Liverpool FC are the same team."
    # Nothing of a match is said, even where Home Assistant gave one, as its own latest is no match against itself.
    found = _liverpool_answer(seasons=seasons, against=LIVERPOOL_FC, match=_found_match(**NEWCASTLE_LIVERPOOL),
                              notes=[PENALTY_CONCEDED])
    assert said_match_report(found, _after_the_match(), against="Liverpool FC") == "Liverpool and Liverpool FC are the same team."


def test_the_same_team_is_said_with_its_name_and_the_other_team_as_it_was_asked(london):
    AT = _after_the_match()
    # The team by its full name where it has one, unless that is just what the other team was asked as.
    team = {"id": "364", "name": "Liverpool", "full_name": "Liverpool FC", "followed": True}
    assert said_match_report(_match_answer(team=team, seasons=6, against=LIVERPOOL_FC), AT, against="Liverpool FC") == (
        "Liverpool and Liverpool FC are the same team.")
    assert said_match_report(_match_answer(team=team, seasons=6, against=LIVERPOOL_FC), AT, against="The Reds") == (
        "Liverpool FC and The Reds are the same team.")
    assert said_match_report(_match_answer(team=team, seasons=6, against=LIVERPOOL_FC), AT, against="liverpool") == (
        "Liverpool FC and liverpool are the same team.")
    # Where the other team was asked as every name the team has, there is no second name to say.
    manchester = {"id": "360", "name": "Manchester United", "followed": True}
    assert said_match_report(_match_answer(team=manchester, seasons=6, against={**manchester, "followed": False}), AT,
                             against="Manchester United") == "Those are the same team, Manchester United."


def test_two_teams_with_other_ids_are_not_the_same_team(london):
    # Names a little alike are another team where the ids differ, as a team of another name is.
    stafford = {"id": "2999", "name": "Stafford Rangers", "followed": False}
    assert said_match_report(_liverpool_answer(seasons=6, against=stafford), _after_the_match(), against="Rangers") == (
        "No match between Liverpool and Stafford Rangers is listed in the last 6 seasons.")
    # Nor are two ids which only BBC knows, unless they are the same.
    shields, rival = {"id": "bbc:x", "name": "Oxford City"}, {"id": "bbc:y", "name": "Oxford Town"}
    assert said_match_report(_match_answer(team=shields, seasons=1, against=rival), _after_the_match(), against="Oxford Town") == (
        "No match between Oxford City and Oxford Town is among ESPN's results for Oxford City.")
    assert said_match_report(_match_answer(team=shields, seasons=1, against=shields), _after_the_match(), against="Oxford City FC") == (
        "Oxford City and Oxford City FC are the same team.")


def test_two_teams_with_no_id_are_not_taken_for_one(london):
    # No id, or an empty one, on both sides says nothing of whether they are one team.
    for own, other in (({"name": "Liverpool"}, {"name": "Liverpool FC"}), ({"id": "", "name": "Liverpool"}, {"id": "", "name": "Liverpool FC"})):
        assert said_match_report(_match_answer(team=own, seasons=6, against=other), _after_the_match(), against="Liverpool FC") == (
            "No match between Liverpool and Liverpool FC is listed in the last 6 seasons.")


def test_the_same_team_is_said_only_for_a_match_asked_against_another_team(london):
    # With no other team asked for, the team's own answer has no "against" to compare with.
    assert said_match_report(_liverpool_answer(), _after_the_match()) == "No match is listed for Liverpool."
    assert said_match_report(_liverpool_answer(against=LIVERPOOL_FC), _after_the_match()) == "No match is listed for Liverpool."


def test_a_team_asked_against_itself_is_said_so_end_to_end(london):
    home = FakeHome(answer=_liverpool_answer(seasons=6, against=LIVERPOOL_FC, match=_found_match(**NEWCASTLE_LIVERPOOL)))
    assert _said(home, team="Liverpool", about="match", against="Liverpool FC") == {
        "success": True, "message": "Liverpool and Liverpool FC are the same team."}
    assert home.asked == [("match", "Liverpool", "", 1, "Liverpool FC")]


@pytest.mark.parametrize("found", [{"id": 364, "name": "Liverpool"}, {"id": ["364"], "name": "Liverpool"}],
                         ids=["a number", "a list"])
def test_an_id_which_isnt_text_is_refused_for_the_other_team(london, found):
    with pytest.raises((KeyError, TypeError, ValueError), match="id"):
        said_match_report(_liverpool_answer(seasons=6, against=found), _after_the_match(), against="Liverpool FC")


@pytest.mark.parametrize("changes", [
    {}, {"seasons": 0}, {"seasons": 7}, {"seasons": -1}, {"seasons": "3"}, {"seasons": 2.0}, {"seasons": True}, {"seasons": None},
    {"seasons": [6]},
], ids=["no seasons", "none looked through", "more than six", "negative", "text", "a float", "a flag", "null", "a list"])
def test_an_answer_for_the_other_team_must_say_how_many_seasons_were_looked_through(london, changes):
    # Without it the answer may be a script's own latest match for the team, which isn't the match of the two.
    # An answer which has them is said, so that it is only what is wrong with the seasons which is refused.
    assert said_match_report(_met_answer(6), _after_the_match(), against="Newcastle")
    for match in ({}, {"match": _found_match(**NEWCASTLE_LIVERPOOL)}):
        with pytest.raises((KeyError, TypeError, ValueError), match="seasons"):
            said_match_report(_liverpool_answer(against=NEWCASTLE, **match, **changes), _after_the_match(),
                              against="Newcastle")


@pytest.mark.parametrize("changes, word", [
    ({"against": None}, "against"), ({"against": "Newcastle"}, "against"), ({"against": [NEWCASTLE]}, "against"),
    ({"against": {"id": "361", "followed": False}}, "name"), ({"against": {"name": 5}}, "name"),
    ({"against": {"name": "", "full_name": ""}}, "against"),
], ids=["null", "text", "a list", "no name", "a name which isn't text", "an empty name"])
def test_an_answer_for_the_other_team_must_say_which_team_it_found(london, changes, word):
    # As for the seasons: a match of the two can't be said without the other team, so it is refused as the team's is.
    assert said_match_report(_met_answer(6), _after_the_match(), against="Newcastle")
    for match in ({}, {"match": _found_match(**NEWCASTLE_LIVERPOOL)}):
        with pytest.raises((KeyError, TypeError, ValueError), match=word):
            said_match_report(_liverpool_answer(seasons=6, **match, **changes), _after_the_match(), against="Newcastle")


def test_an_answer_for_the_other_team_with_no_against_at_all_is_refused(london):
    for match in ({}, {"match": _found_match(**NEWCASTLE_LIVERPOOL)}):
        with pytest.raises(KeyError, match="against"):
            said_match_report(_liverpool_answer(seasons=6, **match), _after_the_match(), against="Newcastle")


def test_seasons_are_not_asked_of_an_answer_with_no_other_team(london):
    # Asked of the team alone, an answer has none, and one which does is no different.
    assert said_match_report(_liverpool_answer(), _after_the_match()) == "No match is listed for Liverpool."
    assert said_match_report(_liverpool_answer(seasons=3), _after_the_match()) == "No match is listed for Liverpool."


@pytest.mark.parametrize("changes", [
    {"notes": "Penalty conceded"}, {"notes": ["Penalty conceded"]}, {"notes": [{"text": "A note."}]},
    {"notes": [{"minute": "41'"}]}, {"notes": [{"minute": 41, "text": "A note."}]}, {"notes": [{"minute": "41'", "text": 4}]},
    {"notes": [{"minute": "41'", "text": ""}]}, {"notes": [{"minute": "", "text": "A note."}]},
    {"match": "Spain v England"}, {"match": {"id": "1"}}, {"team": "England"},
    {"match": _found_match(events=[{"minute": "45'", "kind": "yellow_card", "side": "away"}])},
    {"match": _found_match(events="goal")},
], ids=["notes not a list", "a note not a dict", "note with no minute", "note with no text", "minute not text", "text not text",
        "text empty", "minute empty", "match not a dict", "match lacking keys", "team not a dict", "event kind unknown",
        "events not a list"])
def test_a_match_report_of_the_wrong_shape_is_refused(london, changes):
    answer = {**_match_answer(match=_found_match(**SPAIN_ENGLAND), notes=[PENALTY_CONCEDED]), **changes}
    with pytest.raises((KeyError, TypeError, ValueError)):
        said_match_report(answer, _after_the_match())
    # Notes of the wrong shape are refused even where there is no match to give them to.
    if "notes" in changes:
        with pytest.raises((KeyError, TypeError, ValueError)):
            said_match_report(_match_answer(**changes), _after_the_match())


@pytest.mark.parametrize("text, said", [
    ("VAR Decision: No Goal France 1-0 Türkiye.", "VAR Decision: No Goal France 1 to 0 Türkiye."),
    ("VAR Decision: No Goal France 2 - 1 Türkiye.", "VAR Decision: No Goal France 2 to 1 Türkiye."),
    ("VAR Decision: No Goal France 1\u20130 Türkiye.", "VAR Decision: No Goal France 1 to 0 Türkiye."),
    ("Own goal by Rodri (Spain). Spain 0-1 England, then 1-1.", "Own goal by Rodri (Spain). Spain 0 to 1 England, then 1 to 1."),
    # Only a score between two numbers changes: a dash between words, in a name or after a space is left as it was.
    ("Second yellow card - a long-range effort hit the post.", "Second yellow card - a long-range effort hit the post."),
    ("VAR Decision: Goal cancelled - offside, 2-1 stays.", "VAR Decision: Goal cancelled - offside, 2 to 1 stays."),
    ("Penalty conceded by Pau Cubarsí (Spain) after a foul in the penalty area.",
     "Penalty conceded by Pau Cubarsí (Spain) after a foul in the penalty area."),
], ids=["a plain dash", "a dash with spaces", "an en dash", "two scores", "dashes which aren't scores", "a score and a spaced dash",
        "no score"])
def test_a_score_in_a_note_is_said_with_to_and_nothing_else_in_the_note_changes(london, text, said):
    answer = _match_answer(match=_found_match(**SPAIN_ENGLAND), notes=[{"minute": "90'+3'", "text": text}])
    assert said_match_report(answer, _after_the_match()).split("\n")[-1] == f"Note at 90'+3': {said}"


# A shoot-out after extra time at 2, 2, as it is won by the home side, by the away side, and with no winner named.
SHOOTOUTS = [{"winner": "home", "shootout": [4, 3]}, {"winner": "away", "shootout": [3, 4]}, {"winner": None, "shootout": [3, 4]}]
# What ESPN's notes say of a score, as the VAR ones do.
SCORE_NOTES = [{"minute": "41'", "text": "VAR Decision: No Goal France 1-0 Türkiye."},
               {"minute": "88'", "text": "Penalty saved. Spain 2 - 2 England, after 2\u20132 at half time."}]


def _level_after_extra_time(**changes):
    """Spain 2, England 2 after extra time, with the goals which make it so and a red card, as the Contract has them."""
    return _found_match(**{**SPAIN_ENGLAND, "home_score": 2, "away_score": 2, "extra_time": True, "events": [
        {"minute": "12'", "kind": "goal", "side": "home", "player": "Nico Williams"},
        {"minute": "45'", "kind": "penalty", "side": "away", "player": "Harry Kane"},
        {"minute": "63'", "kind": "red_card", "side": "away", "player": "Elliot Anderson"},
        {"minute": "70'", "kind": "own_goal", "side": "away", "player": "Dani Carvajal"},
        {"minute": "90'+3'", "kind": "penalty_missed", "side": "home", "player": "Mikel Oyarzabal"},
        {"minute": "118'", "kind": "goal", "side": "home", "player": "Lamine Yamal"},
    ], **changes})


def test_no_sentence_nova_builds_has_a_score_as_digits_joined_by_a_dash(london):
    AT = _after_the_match()
    # A formation is digits joined by dashes too, so the line-ups have one.
    side = {"team": "Spain", "formation": "4-2-3-1", "starters": ["Simón", "Carvajal"], "bench": ["Remiro"]}
    spain = {"id": "164", "name": "Spain", "followed": False}
    row = {"league": "UEFA Nations League", "group": "", "rank": 1, "of": 4, "played": 3, "won": 1, "drawn": 1, "lost": 1,
           "gd": -2, "points": 4}
    group = {"name": "UEFA Nations League", "rows": [{"rank": 1, "team": "Spain", "played": 3, "won": 1, "drawn": 1, "lost": 1,
                                                        "gd": -2, "points": 4}]}
    said = []
    for shootout in SHOOTOUTS:
        found = _level_after_extra_time(**shootout)
        match = Match.from_home(found)
        team = _team("164", "Spain", now=found, next=[found], last=[found])
        said += [said_match(match, AT), *said_events(match),
                 said_followed(_reading(team), AT),
                 said_team(_team_answer(now=found, next=[found], last=[found], tables=[row]), AT),
                 said_competition(_competition_answer([found], days=["20261115"]), AT),
                 said_left(_left_answer(matches=[found], teams=["Spain", "England"]), AT),
                 said_lineups(_lineups_answer(team=spain, match=found, sides=[side]), AT),
                 said_match_report(_match_answer(match=found, notes=SCORE_NOTES), AT),
                 said_table({"competition": {"name": "UEFA Nations League"}, "groups": [group]})]
        # And through describe, which says each of them as Claude is given it.
        for given, answer in (({"team": "Spain"}, _team_answer(now=found, last=[found])),
                              ({"team": "Spain", "about": "lineups"}, _lineups_answer(team=spain, match=found, sides=[side])),
                              ({"team": "Spain", "about": "match"}, _match_answer(match=found, notes=SCORE_NOTES)),
                              ({"competition": "UEFA Nations League", "about": "match"}, _competition_answer([found]))):
            said.append(_said(FakeHome(answer=answer), now=AT, **given)["message"])
    assert any("France 1 to 0 Türkiye" in words for words in said)
    assert not [words for words in said if re.search(r"\d\s*[-\u2010-\u2015\u2212]\s*\d", words)]
    # The shoot-out is said in words, with its winner's penalties first.
    assert "Spain won on penalties, 4 to 3." in said_match(Match.from_home(_level_after_extra_time(**SHOOTOUTS[0])), AT)
    assert "England won on penalties, 4 to 3." in said_match(Match.from_home(_level_after_extra_time(**SHOOTOUTS[1])), AT)
    assert "The penalty shootout finished Spain 3, England 4." in said_match(Match.from_home(_level_after_extra_time(**SHOOTOUTS[2])), AT)


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

    def football_ask(self, what, name, date="", days=1, against=""):
        # The other team is noted only when there is one, so that a question without it reads as it always did.
        self.asked.append((what, name, date, days) + ((against,) if against else ()))
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
    shields = _team("18312", "South Shields", asked="18312", stale=True, now=_found_match(
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


def test_a_match_in_play_in_a_stale_teams_old_copy_is_neither_said_nor_warned_of_in_the_overview(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    stevenage = _team("395", "Stevenage", next=[_found_match(kickoff="2026-11-21T15:00:00+00:00", home="Notts County",
                                                             away="Stevenage", league="EFL League One")])
    # Nobody which isn't stale is in play: the one match in play is a stale team's old copy, which is not the match now.
    shields = _team("18312", "South Shields", asked="18312", stale=True, now=_found_match(
        id="21", state="in", phase="second_half", clock="67'", home="South Shields", away="Gateshead", home_score=1,
        away_score=0, league="Northern Premier League"))
    said = _said(FakeHome([stevenage, shields]))
    assert said == {"success": True, "message": said_followed(_reading(stevenage), AT) + "\n" + CANT_GET_SHIELDS}
    assert LAG not in said["message"] and "Gateshead" not in said["message"] and "South Shields:" not in said["message"]


def test_the_overview_names_each_team_it_cant_get_as_its_last_line_with_nothing_in_play(london):
    AT = datetime(2026, 11, 13, 12, 0).astimezone()
    liverpool = _team("364", "Liverpool", next=[_found_match(**SUNDAY_AWAY)])
    home = FakeHome([_team("395", "Stevenage", stale=True), liverpool, _team("18312", "South Shields", asked="18312", stale=True)])
    said = _said(home)
    assert said == {"success": True, "message": said_followed(_reading(liverpool), AT)
                    + "\nI can't get Stevenage and South Shields right now."}


def test_the_overview_names_the_stale_team_though_the_fresh_one_has_nothing_listed(london):
    # A fresh team with nothing listed is still a team, so this is not "No teams are followed."
    home = FakeHome([_team("395", "Stevenage"), _team("18312", "South Shields", asked="18312", stale=True)])
    assert _said(home) == {"success": True, "message": f"Stevenage: no fixtures listed.\n{CANT_GET_SHIELDS}"}


def test_the_overview_says_what_is_left_out_first_then_the_team_it_cant_get_then_the_warning(london):
    sentence = "Home Assistant is showing only some of the followed teams."
    home = FakeHome([_team("364", "Liverpool", now=_found_match(**LIVE)), _team("18312", "South Shields", asked="18312", stale=True)],
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
    ({"team": "England", "about": "match"}, ("match", "England", "", 1)),
    ({"team": "England", "about": " Match ", "date": "2026-11-15"}, ("match", "England", "20261115", 1)),
    # A team's match is asked of the team whatever competition is given with it, as its line-ups are.
    ({"team": "England", "competition": "UEFA Nations League", "about": "match"}, ("match", "England", "", 1)),
    ({"team": "England", "competition": "UEFA Nations League", "about": "match", "date": "2026-11-15"},
     ("match", "England", "20261115", 1)),
    # A competition alone is its matches, with their goals, on the day given.
    ({"competition": "Champions League", "about": "match", "date": "2026-11-15"}, ("competition", "Champions League", "20261115", 1)),
    ({"competition": "Champions League", "about": "match"}, ("competition", "Champions League", "", 1)),
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
    ("match", _competition_answer([_found_match(**SPAIN_ENGLAND)]), said_competition, "Goals: Harry Kane 45' (pen)"),
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

    for name in ("said_team", "said_competition", "said_table", "said_left", "said_lineups", "said_match_report"):
        monkeypatch.setattr(football_words, name, refused)
    answer = {"unknown": ["Arsnal", "Chelsee"], "stale": False}
    for given in ({"team": "Arsnal"}, {"competition": "x"}, {"competition": "x", "about": "table"},
                  {"competition": "x", "about": "left"}, {"team": "Arsnal", "about": "lineups"},
                  {"team": "Arsnal", "about": "match"}, {"competition": "x", "about": "match"}):
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
    ({"competition": "Premier League"}, _competition_answer([_found_match(events=[{"minute": "45'", "kind": "yellow_card", "side": "home"}])])),
    ({"competition": "Premier League"}, _competition_answer([_found_match(events="goal")])),
    ({"team": "England", "about": "match"}, _match_answer(notes="Penalty conceded")),
    ({"team": "Liverpool", "about": "match", "against": "Newcastle"}, _liverpool_answer(against=NEWCASTLE)),
    ({"team": "Liverpool", "about": "match", "against": "Newcastle"}, _met_answer(7)),
    ({"team": "Liverpool", "about": "match", "against": "Newcastle"}, _met_answer("6")),
    ({"team": "Liverpool", "about": "match", "against": "Newcastle"}, _liverpool_answer(seasons=6)),
    ({"team": "Liverpool", "about": "match", "against": "Newcastle"}, _liverpool_answer(seasons=6, against="Newcastle")),
    ({"team": "Liverpool", "about": "match", "against": "Newcastle"}, _met_answer(1, match=_found_match(events="goal"))),
    ({"team": "England", "about": "match"}, _match_answer(match=_found_match(events="goal"))),
    ({"competition": "Premier League", "about": "table"}, {"competition": {"name": "Premier League"}, "stale": False}),
    ({"competition": "Premier League", "about": "left"}, _left_answer(knockout="yes")),
    ({"competition": "Premier League", "about": "left"}, {"stale": False, "unknown": "Arsnal"}),
    ({"competition": "Premier League", "about": "left"}, {"stale": False, "unknown": [4]}),
], ids=["no team", "next not a list", "match lacking keys", "odds not numbers", "match state unknown", "day not a day",
        "event kind unknown", "events not a list", "match notes not a list", "other team with no seasons",
        "other team with seven seasons", "other team with seasons as text", "other team not found",
        "other team found as text", "other team with a match of the wrong shape",
        "match events not a list", "table with no groups",
        "knockout not a flag", "unknown not a list", "unknown not names"])
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
], ids=["no match today", "a match tomorrow"])
def test_lineups_with_no_team_and_nobody_playing(london, teams):
    home = FakeHome(teams)
    assert _said(home, about="lineups") == {"success": True, "message": NO_LINEUP_TEAM}
    assert home.asked == []


def test_lineups_with_no_team_and_no_team_followed_say_so_as_the_overview_does(london):
    # Nobody is followed, which is not that none of them is playing.
    home = FakeHome([])
    assert _said(home, about="lineups") == {"success": True, "message": "No teams are followed."}
    assert _said(FakeHome([])) == _said(home, about="lineups")
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


def _shields_match(**fields):
    """South Shields' match as it comes from BBC Sport, which never has line-ups: today at 15:00 unless a test says."""
    return _found_match(**{
        "id": "bbc:301", "slug": "bbc:national-league-north", "league": "National League N / S", "home": "South Shields",
        "home_id": "18312", "away": "Worksop Town", "away_id": "bbc:77", "kickoff": "2026-11-13T15:00:00+00:00", **fields})


def _stevenage_match(**fields):
    """Stevenage's match as it comes from ESPN, which has line-ups: today at 15:00 unless a test says."""
    return _found_match(**{
        "id": "30", "home": "Stevenage", "home_id": "395", "away": "Wigan", "away_id": "361",
        "kickoff": "2026-11-13T15:00:00+00:00", **fields})


@pytest.mark.parametrize("state, phase", [("in", "first_half"), ("pre", None), ("post", "full_time")],
                         ids=["in play", "to come today", "over today"])
def test_lineups_with_no_team_ask_for_a_match_which_can_have_them_before_one_from_bbc(london, state, phase):
    # South Shields come first in the box and are in the same state at the same time, but their match never has line-ups.
    mark = {"state": state, **({"phase": phase} if phase else {})}
    teams = [_team("18312", "South Shields", now=_shields_match(**mark)), _team("395", "Stevenage", now=_stevenage_match(**mark))]
    home = FakeHome(teams, answer=_lineups_answer(match=_found_match()))
    assert _said(home, about="lineups")["success"]
    assert home.asked == [("lineups", "Stevenage", "", 1)]


def test_lineups_with_no_team_ask_for_a_match_from_bbc_after_one_which_can_have_them_whatever_the_kick_offs(london):
    # The later kick-off is asked for: South Shields' earlier one has no line-ups to come out.
    teams = [_team("18312", "South Shields", now=_shields_match(kickoff="2026-11-13T12:30:00+00:00")),
             _team("395", "Stevenage", now=_stevenage_match(kickoff="2026-11-13T19:45:00+00:00"))]
    home = FakeHome(teams, answer=_lineups_answer(match=_found_match()))
    _said(home, about="lineups")
    assert home.asked == [("lineups", "Stevenage", "", 1)]


def test_lineups_with_no_team_ask_for_a_match_from_bbc_in_play_before_a_match_not_from_bbc_still_to_come(london):
    # The match in play comes first, whichever source it is from: the order is only within each.
    teams = [_team("395", "Stevenage", now=_stevenage_match(kickoff="2026-11-13T19:45:00+00:00")),
             _team("18312", "South Shields", now=_shields_match(state="in", phase="first_half"))]
    home = FakeHome(teams, answer=_lineups_answer(match=_found_match()))
    _said(home, about="lineups")
    assert home.asked == [("lineups", "South Shields", "", 1)]


def test_lineups_with_no_team_ask_for_south_shields_when_they_are_the_only_team_playing(london):
    for state in ("in", "pre", "post"):
        shields = {"id": "18312", "name": "South Shields", "followed": True}
        home = FakeHome([_team("18312", "South Shields", now=_shields_match(state=state))],
                        answer=_lineups_answer(team=shields, match=_shields_match()))
        assert _said(home, about="lineups")["success"]
        assert home.asked == [("lineups", "South Shields", "", 1)]


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
    assert home.asked == [("competition", "Premier League", "", 1)]


def test_lineups_of_a_team_are_asked_of_it_whatever_competition_is_given(london):
    # "Who's starting for Liverpool in the Champions League tonight?" is about the team, and a team's line-ups are asked
    # of it, where a table or who is left is only a competition's.
    home = FakeHome(answer={"stale": False, "unknown": ["x"]})
    _said(home, team="Liverpool", competition="UEFA Champions League", about=" Lineups ")
    _said(home, team="Liverpool", competition="UEFA Champions League", about="left")
    assert home.asked == [("lineups", "Liverpool", "", 1), ("left", "UEFA Champions League", "", 1)]


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


def test_a_teams_match_is_asked_of_home_assistant_and_said_as_what_happened(london):
    AT = _after_the_match()
    answer = _match_answer(match=_found_match(**SPAIN_ENGLAND), notes=[PENALTY_CONCEDED])
    home = FakeHome(answer=answer)
    said = describe(_football(home), team="England", about="match", now=AT)
    assert said == {"success": True, "message": said_match_report(answer, AT)}
    assert said["message"].split("\n")[1] == "45': a penalty for England, scored by Harry Kane. Spain 0, England 1."
    assert home.asked == [("match", "England", "", 1)]


def test_a_teams_match_on_a_day_is_asked_for_that_day_and_no_match_says_so_of_the_day(london):
    home = FakeHome(answer=_match_answer())
    assert _said(home, team="England", about="match", date="2026-11-15") == {
        "success": True, "message": "No match is listed for England on that day."}
    assert home.asked == [("match", "England", "20261115", 1)]
    # With no day asked for, it is only that none is listed; and text which isn't a day is no day, which Home Assistant isn't told of.
    for given in (None, "Sunday", "15/11/2026"):
        home = FakeHome(answer=_match_answer())
        assert _said(home, team="England", about="match", date=given)["message"] == "No match is listed for England."
        assert home.asked == [("match", "England", "", 1)]


def test_when_two_teams_last_played_is_asked_as_a_match_of_the_one_against_the_other(london):
    # "When did Liverpool last play Newcastle?": the team's match, never its matches or the team, with the other team.
    AT = _after_the_match()
    answer = _met_answer(1, match=_found_match(**NEWCASTLE_LIVERPOOL))
    home = FakeHome(answer=answer)
    said = describe(_football(home), team="Liverpool", about="match", against="Newcastle", now=AT)
    assert said == {"success": True, "message": said_match_report(answer, AT, against="Newcastle")}
    assert said["message"].split("\n")[0] == ("Newcastle United 2, Liverpool 2, Premier League, on Sunday the 23rd of August, "
                                             "at St James' Park in Newcastle upon Tyne. It was a draw.")
    assert home.asked == [("match", "Liverpool", "", 1, "Newcastle")]


def test_when_two_teams_last_played_says_how_far_back_it_was_looked_for_where_they_havent_met(london):
    home = FakeHome(answer=_met_answer(6))
    assert _said(home, team="Liverpool", about="match", against="Newcastle") == {
        "success": True, "message": "No match between Liverpool and Newcastle United is listed in the last 6 seasons."}
    # A national side has one list with no seasons, so that is not said as seasons.
    home = FakeHome(answer=_match_answer(seasons=1, against={"id": "217", "name": "Wales", "followed": False}))
    assert _said(home, team="England", about=" Match ", against=" Wales ") == {
        "success": True, "message": "No match between England and Wales is among ESPN's results for England."}
    assert home.asked == [("match", "England", "", 1, "Wales")]


def test_a_team_the_other_team_isnt_found_is_said_as_any_name_espn_doesnt_know_is(london):
    answer = {"unknown": ["Newcastel"], "stale": False}
    assert _said(FakeHome(answer=answer), team="Liverpool", about="match", against="Newcastel") == {
        "success": True, "message": "ESPN, where Home Assistant's football comes from, found nothing called Newcastel."}


@pytest.mark.parametrize("answer", [
    {"stale": True, "seasons": 6, "team": {}}, {"stale": False, "error": "OSError: down"},
    {"stale": True, "unknown": ["Newcastel"]},
    ValueError("Home Assistant's football command failed: 2, 'unrecognized arguments: --against=Newcastle'"),
    requests.Timeout("slow"),
], ids=["stale", "error", "stale and unknown", "a script which doesn't know the other team", "no answer in time"])
def test_an_old_or_failed_answer_for_the_other_team_cant_be_got(london, answer):
    home = FakeHome(answer=answer)
    assert _said(home, team="Liverpool", about="match", against="Newcastle") == NOT_GOT
    assert home.asked == [("match", "Liverpool", "", 1, "Newcastle")]


@pytest.mark.parametrize("given, asked", [
    # A day wins over the other team, which Home Assistant isn't told of, as the Contract says.
    ({"team": "Liverpool", "about": "match", "against": "Newcastle", "date": "2025-03-16"},
     ("match", "Liverpool", "20250316", 1)),
    # Text which isn't a day is no day, and the other team stands.
    ({"team": "Liverpool", "about": "match", "against": "Newcastle", "date": "last March"},
     ("match", "Liverpool", "", 1, "Newcastle")),
    # The team's own name, said another way round or with a space, is no other team: it is the team alone.
    ({"team": "Liverpool", "about": "match", "against": "Liverpool"}, ("match", "Liverpool", "", 1)),
    ({"team": " Liverpool ", "about": "match", "against": "  liverpool"}, ("match", "Liverpool", "", 1)),
    ({"team": "Man  Utd", "about": "match", "against": "MAN UTD"}, ("match", "Man  Utd", "", 1)),
    ({"team": "O'Higgins", "about": "match", "against": "OHiggins"}, ("match", "O'Higgins", "", 1)),
    # No other team, or one which is only blanks, or nothing but what is cut from a name, is the team alone as it always was.
    ({"team": "Liverpool", "about": "match", "against": "  "}, ("match", "Liverpool", "", 1)),
    ({"team": "Liverpool", "about": "match", "against": "';$()"}, ("match", "Liverpool", "", 1)),
    # What is asked is the name as it is cut down for a command line, so that this is decided in one place.
    ({"team": "Liverpool", "about": "match", "against": "  Newcastle;  rm -rf "}, ("match", "Liverpool", "", 1, "Newcastle rm -rf")),
    ({"team": "Liverpool", "about": "match", "against": "x" * 100}, ("match", "Liverpool", "", 1, "x" * 60)),
    ({"team": "Liverpool", "about": "match", "against": None}, ("match", "Liverpool", "", 1)),
    # Whatever competition comes with a team, as for its match alone.
    ({"team": "Liverpool", "competition": "Carabao Cup", "about": "match", "against": "Newcastle"},
     ("match", "Liverpool", "", 1, "Newcastle")),
    # Only a team's match has another team to be against.
    ({"team": "Liverpool", "about": "matches", "against": "Newcastle"}, ("team", "Liverpool", "", 1)),
    ({"team": "Liverpool", "against": "Newcastle"}, ("team", "Liverpool", "", 1)),
    ({"team": "Liverpool", "about": "lineups", "against": "Newcastle"}, ("lineups", "Liverpool", "", 1)),
    ({"team": "Liverpool", "about": "table", "against": "Newcastle"}, ("team", "Liverpool", "", 1)),
    ({"competition": "Carabao Cup", "about": "left", "against": "Newcastle"}, ("left", "Carabao Cup", "", 1)),
], ids=["a day wins", "not a day", "the same team", "the same team said another way", "the same name with spaces",
        "the same name cut down", "only blanks", "nothing left once cut down", "cut down", "cut short", "none",
        "with a competition", "matches", "no kind", "lineups", "table", "left"])
def test_the_other_team_is_asked_only_of_a_teams_match_with_no_day(london, given, asked):
    home = FakeHome(answer={"stale": False, "unknown": ["x"]})
    _said(home, **given)
    assert home.asked == [asked]


def test_a_match_on_a_day_with_the_other_team_given_says_the_day_not_the_seasons(london):
    home = FakeHome(answer=_liverpool_answer())
    assert _said(home, team="Liverpool", about="match", against="Newcastle", date="2025-03-16") == {
        "success": True, "message": "No match is listed for Liverpool on that day."}


def test_a_match_on_a_day_of_an_earlier_year_is_said_with_the_year(london):
    # "What happened in the Liverpool match on the 16th of March 2025?", heard in November 2026.
    home = FakeHome(answer=_liverpool_answer(match=_found_match(**LEAGUE_CUP_FINAL)))
    said = _said(home, now=_after_the_match(), team="Liverpool", about="match", date="2025-03-16", against="Newcastle")
    assert said["message"].split("\n")[0].startswith(
        "Newcastle United 2, Liverpool 1, Carabao Cup, on Sunday the 16th of March 2025, at Wembley Stadium in London.")
    assert home.asked == [("match", "Liverpool", "20250316", 1)]


@pytest.mark.parametrize("given", [
    {"about": "match", "against": "Newcastle"},
    {"about": " Match ", "against": "Newcastle", "team": "  "},
    # A competition alone isn't a team to be against another.
    {"about": "match", "against": "Newcastle", "competition": "Premier League"},
], ids=["no team", "a team of blanks", "a competition alone"])
def test_the_other_team_with_no_team_asks_which_team_without_asking_anyone(london, given):
    # The sensor isn't read either, so that a followed team's match is never said for the question of two others.
    home = FakeHome(answer=_liverpool_answer())
    home.sensor = requests.ConnectionError("refused")
    assert _said(home, **given) == {"success": True, "message": "Name a team for its match."}
    assert home.asked == []


def test_the_other_team_with_nothing_left_once_cut_down_is_no_other_team_with_no_team_too(london):
    # So that it is the followed team's match as it is with none given, and not "Name a team for its match.".
    teams = [_team("364", "Liverpool", now=_found_match(**LIVE))]
    home = FakeHome(teams, answer=_match_answer())
    _said(home, about="match", against="';$()")
    assert home.asked == [("match", "Liverpool", "", 1)]


def test_the_other_team_with_no_team_but_a_day_is_the_day_alone(london):
    # The day wins over the other team, so this is a match on the day as asked with no team named.
    teams = [_team("364", "Liverpool", now=_found_match(**{**SPAIN_ENGLAND, "id": "9", "home": "Liverpool"}))]
    home = FakeHome(teams, answer=_liverpool_answer())
    _said(home, now=_after_the_match(), about="match", against="Newcastle", date="2026-11-15")
    assert home.asked == [("match", "Liverpool", "20261115", 1)]


@pytest.mark.parametrize("against", [5, ["Newcastle"], {"name": "Newcastle"}, True])
def test_the_other_team_which_isnt_a_name_isnt_a_name(london, against):
    home = FakeHome(answer=_liverpool_answer(seasons=6))
    assert _said(home, team="Liverpool", about="match", against=against) == {
        "success": False, "message": "I didn't catch which team or competition."}
    assert home.asked == []


def _over(kickoff, **fields):
    """A match which kicked off at this time (UTC) and is over, for the match asked about with no team named."""
    return _found_match(**{"state": "post", "phase": "full_time", "clock": "FT", "home_score": 1, "away_score": 0,
                           "kickoff": kickoff, **fields})


@pytest.mark.parametrize("teams", [
    [_team("395", "Stevenage"), _team("364", "Liverpool", next=[_found_match(kickoff="2026-11-21T15:00:00+00:00")])],
    [_team("364", "Liverpool", now=_found_match(kickoff="2026-11-14T15:00:00+00:00"))],
    # A match still to come today has nothing which happened in it, so it isn't the one asked about.
    [_team("364", "Liverpool", now=_found_match(kickoff="2026-11-13T20:00:00+00:00"))],
    [_team("364", "Liverpool", now=_found_match(kickoff="2026-11-13T20:00:00+00:00", phase="off")),
     _team("395", "Stevenage", now=_stevenage_match(state="post", phase="off"))],
], ids=["no match today", "a match tomorrow", "a match still to come today", "matches which are off"])
def test_a_match_with_no_team_and_nobody_playing_or_done_playing_asks_which_team(london, teams):
    home = FakeHome(teams)
    assert _said(home, about="match") == {"success": True, "message": "Name a team for its match."}
    assert home.asked == []


def test_a_match_with_no_team_and_no_team_followed_says_so_as_the_overview_does(london):
    # Nobody is followed, which is not that none of them is playing.
    home = FakeHome([])
    assert _said(home, about="match") == {"success": True, "message": "No teams are followed."}
    assert home.asked == []


@pytest.mark.parametrize("teams, sensor", [
    ([_team("364", "Liverpool", now=_found_match(**LIVE), stale=True)], {}),
    ([_team("364", "Liverpool", now=_found_match(**LIVE), stale=True), _team("395", "Stevenage")], {}),
    ([_team("364", "Liverpool", stale=True), _team("395", "Stevenage", stale=True)], {}),
    ([_team("364", "Liverpool", now=_found_match(**LIVE)), _team("395", "Stevenage")], {"stale": True}),
    # The stale team's match may be the one over today: a fresh team with one still to come is not the answer.
    ([_team("364", "Liverpool", now=_over("2026-11-13T10:00:00+00:00"), stale=True),
      _team("395", "Stevenage", now=_stevenage_match(kickoff="2026-11-13T20:00:00+00:00"))], {}),
], ids=["a stale team playing", "a stale team playing and a fresh one not", "every team stale",
        "a stale sensor which marks no team", "a stale team's match over and a fresh one still to come"])
def test_a_match_with_no_team_cant_say_nobody_is_playing_while_a_team_is_stale(london, caplog, teams, sensor):
    home = FakeHome(teams, **sensor)
    with caplog.at_level(logging.WARNING, logger="voice_assistant.football"):
        assert _said(home, about="match") == NOT_GOT
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert home.asked == []


def test_a_match_with_no_team_asks_for_a_fresh_team_playing_though_another_is_stale(london):
    teams = [_team("364", "Liverpool", now=_found_match(**LIVE), stale=True),
             _team("395", "Stevenage", now=_stevenage_match(state="in", phase="first_half"))]
    home = FakeHome(teams, answer=_match_answer())
    assert _said(home, about="match")["success"]
    assert home.asked == [("match", "Stevenage", "", 1)]


def test_a_match_with_no_team_is_said_as_the_match_of_the_team_playing(london):
    teams = [_team("395", "Stevenage", now=_stevenage_match(state="post")), _team("364", "Liverpool", now=_found_match(**LIVE))]
    answer = _match_answer(team={"id": "364", "name": "Liverpool", "followed": True}, match=_found_match(**LIVE))
    home = FakeHome(teams, answer=answer)
    assert _said(home, about="match") == {"success": True, "message": said_match_report(answer, AT.astimezone())}
    assert home.asked == [("match", "Liverpool", "", 1)]


def test_a_match_with_no_team_asks_for_a_team_in_play_before_one_over(london):
    teams = [_team("395", "Stevenage", now=_stevenage_match(state="post")), _team("364", "Liverpool", now=_found_match(**LIVE))]
    home = FakeHome(teams, answer=_match_answer())
    _said(home, now=datetime(2026, 11, 13, 14, 0).astimezone(), about="match")
    assert home.asked == [("match", "Liverpool", "", 1)]


def test_a_match_with_no_team_asks_for_the_match_over_today_not_one_still_to_come(london):
    # At six in the evening, Stevenage's match at three is over and Liverpool's at eight is not begun: what happened
    # is Stevenage's, though Liverpool come first in the box and are the one a line-up is asked for.
    evening = datetime(2026, 11, 13, 18, 0).astimezone()
    teams = [_team("364", "Liverpool", now=_found_match(kickoff="2026-11-13T20:00:00+00:00")),
             _team("395", "Stevenage", now=_stevenage_match(state="post", kickoff="2026-11-13T15:00:00+00:00"))]
    home = FakeHome(teams, answer=_match_answer())
    _said(home, now=evening, about="match")
    assert home.asked == [("match", "Stevenage", "", 1)]
    home = FakeHome(teams, answer=_lineups_answer(match=_found_match()))
    _said(home, now=evening, about="lineups")
    assert home.asked == [("lineups", "Liverpool", "", 1)]


def test_a_match_with_no_team_asks_for_the_match_over_today_which_kicked_off_last(london):
    evening = datetime(2026, 11, 13, 18, 0).astimezone()
    earlier = _team("364", "Liverpool", now=_over("2026-11-13T10:00:00+00:00"))
    later = _team("395", "Stevenage", now=_stevenage_match(state="post", kickoff="2026-11-13T12:30:00+00:00"))
    for teams in ([earlier, later], [later, earlier]):
        home = FakeHome(teams, answer=_match_answer())
        _said(home, now=evening, about="match")
        assert home.asked == [("match", "Stevenage", "", 1)]


@pytest.mark.parametrize("state, phase", [("in", "first_half"), ("post", "full_time")], ids=["in play", "over today"])
def test_a_match_with_no_team_doesnt_rank_a_match_from_bbc_last_as_it_has_events(london, state, phase):
    # South Shields come first in the box and are in the same state at the same time: BBC's match is asked for, though
    # for line-ups it is not, as it never has any.
    mark = {"state": state, "phase": phase}
    teams = [_team("18312", "South Shields", now=_shields_match(**mark)), _team("395", "Stevenage", now=_stevenage_match(**mark))]
    home = FakeHome(teams, answer=_match_answer())
    assert _said(home, about="match")["success"]
    assert home.asked == [("match", "South Shields", "", 1)]
    home = FakeHome(teams, answer=_lineups_answer(match=_found_match()))
    assert _said(home, about="lineups")["success"]
    assert home.asked == [("lineups", "Stevenage", "", 1)]


def test_a_match_with_no_team_asks_for_the_later_kick_off_whether_or_not_it_is_from_bbc(london):
    early = {"state": "post", "phase": "full_time", "kickoff": "2026-11-13T10:30:00+00:00"}
    late = {"state": "post", "phase": "full_time", "kickoff": "2026-11-13T11:45:00+00:00"}
    for shields, stevenage, asked in ((early, late, "Stevenage"), (late, early, "South Shields")):
        teams = [_team("18312", "South Shields", now=_shields_match(**shields)), _team("395", "Stevenage", now=_stevenage_match(**stevenage))]
        for box in (teams, list(reversed(teams))):
            home = FakeHome(box, answer=_match_answer())
            _said(home, about="match")
            assert home.asked == [("match", asked, "", 1)]


def test_a_match_with_no_team_asks_for_the_team_by_the_entry_of_the_box_which_found_it(london):
    teams = [_team("360", "Man Utd", asked="Manchester United FC", now=_found_match(**LIVE))]
    home = FakeHome(teams, answer=_match_answer())
    _said(home, about="match")
    assert home.asked == [("match", "Manchester United FC", "", 1)]


def test_a_match_with_no_team_and_a_day_asks_for_the_followed_team_whose_match_was_on_it(london):
    # Yesterday, from the teams' results: the one which kicked off last, as for today. A match on another day isn't it.
    teams = [_team("364", "Liverpool", last=[_over("2026-11-12T15:00:00+00:00"), _over("2026-11-08T16:30:00+00:00")]),
             _team("395", "Stevenage", last=[_over("2026-11-12T19:45:00+00:00", home="Stevenage")]),
             _team("18312", "South Shields", last=[_over("2026-11-10T19:45:00+00:00")])]
    home = FakeHome(teams, answer=_match_answer())
    assert _said(home, about="match", date="2026-11-12")["message"] == "No match is listed for England on that day."
    assert home.asked == [("match", "Stevenage", "20261112", 1)]
    # And with the earlier one the only one on the day, that is.
    home = FakeHome(teams[:1] + teams[2:], answer=_match_answer())
    _said(home, about="match", date="2026-11-12")
    assert home.asked == [("match", "Liverpool", "20261112", 1)]


def test_a_match_with_no_team_and_a_day_finds_the_teams_match_in_its_now_as_well_as_its_last(london):
    # A match which finished after midnight is still the sensor's "now" the next morning.
    team = _team("364", "Liverpool", now=_over("2026-11-12T22:00:00+00:00"))
    home = FakeHome([team], answer=_match_answer())
    _said(home, about="match", date="2026-11-12")
    assert home.asked == [("match", "Liverpool", "20261112", 1)]


def test_a_match_with_no_team_and_today_as_the_day_is_as_with_no_day(london):
    # A match which kicked off before midnight and is still in play twenty minutes after it: today's, as the match in play
    # always is, though it isn't one of the day's kick-offs, which is what it would have to be for any other day.
    teams = [_team("364", "Liverpool", now=_found_match(**LIVE, kickoff="2026-11-12T23:30:00+00:00"))]
    just_after_midnight = datetime(2026, 11, 13, 0, 20).astimezone()
    home = FakeHome(teams, answer=_match_answer())
    _said(home, now=just_after_midnight, about="match", date="2026-11-13")
    assert home.asked == [("match", "Liverpool", "20261113", 1)]
    home = FakeHome(teams, answer=_match_answer())
    _said(home, now=just_after_midnight, about="match")
    assert home.asked == [("match", "Liverpool", "", 1)]
    # Whereas for yesterday the match is among the day's kick-offs, and for the day before it isn't.
    home = FakeHome(teams, answer=_match_answer())
    _said(home, now=just_after_midnight, about="match", date="2026-11-12")
    assert home.asked == [("match", "Liverpool", "20261112", 1)]
    home = FakeHome(teams)
    assert _said(home, now=just_after_midnight, about="match", date="2026-11-11") == {"success": True, "message": "Name a team for its match."}
    assert home.asked == []


@pytest.mark.parametrize("date, teams", [
    # What is in play now, or still to come, isn't what happened on another day.
    ("2026-11-12", [_team("364", "Liverpool", now=_found_match(**LIVE), last=[_over("2026-11-08T16:30:00+00:00")])]),
    ("2026-11-14", [_team("364", "Liverpool", next=[_found_match(kickoff="2026-11-14T15:00:00+00:00")])]),
    ("2026-11-12", [_team("364", "Liverpool", last=[_over("2026-11-12T15:00:00+00:00", state="pre")])]),
    ("2026-11-12", [_team("364", "Liverpool", last=[_over("2026-11-12T15:00:00+00:00", phase="off")])]),
], ids=["a match in play now", "a match tomorrow", "a match not begun", "a match which is off"])
def test_a_match_with_no_team_and_a_day_with_no_match_on_it_asks_which_team(london, date, teams):
    home = FakeHome(teams)
    assert _said(home, about="match", date=date) == {"success": True, "message": "Name a team for its match."}
    assert home.asked == []


def test_a_match_with_no_team_and_a_day_cant_say_none_was_played_while_a_team_is_stale(london, caplog):
    teams = [_team("364", "Liverpool", stale=True, last=[_over("2026-11-12T15:00:00+00:00")]), _team("395", "Stevenage")]
    home = FakeHome(teams)
    with caplog.at_level(logging.WARNING, logger="voice_assistant.football"):
        assert _said(home, about="match", date="2026-11-12") == NOT_GOT
    assert home.asked == []


@pytest.mark.parametrize("failure, said", [
    (requests.ConnectionError("refused"), "I can't reach Home Assistant right now."),
    (requests.HTTPError("404", response=SimpleNamespace(status_code=404)), "Football isn't set up in Home Assistant."),
])
def test_a_match_with_no_team_says_why_the_sensor_cant_be_read(london, failure, said):
    home = FakeHome()
    home.sensor = failure
    assert _said(home, about="match") == {"success": False, "message": said}
    assert _said(FakeHome(["Liverpool"]), about="match") == NOT_GOT
