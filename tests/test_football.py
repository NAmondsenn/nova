"""Tests for football: reading the followed teams - what Home Assistant's sensor says, and when football can't be got - and
the words Claude is given to answer from."""

import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

from voice_assistant.football import (
    LAG, NOT_SET_UP, Football, FootballUnavailable, Match, Reading, Team, said_competition, said_followed,
    said_left, said_match, said_table, said_team)
from voice_assistant.home_assistant import FOOTBALL_TEAMS

# An instant well away from any change of clock, so that a test passes in whichever time zone it runs.
NOW = 1_800_000_000.0


def _iso(epoch):
    """An epoch time as Home Assistant writes one, e.g. "2026-11-13T02:42:14.233328+00:00"."""
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class FakeHome:
    """Answers state() as Home Assistant does, with what a test sets for each entity, or raises what it sets."""

    def __init__(self, states):
        self.states = dict(states)

    def state(self, entity_id, timeout=None):
        found = self.states[entity_id]
        if isinstance(found, Exception):
            raise found
        return found


def _sensor(teams, age=10, state=None, **attributes):
    """The sensor's state as /api/states/<id> has it, last written by Home Assistant age seconds before NOW."""
    return {
        "entity_id": FOOTBALL_TEAMS,
        "state": "1" if state is None else state,
        "attributes": {"teams": teams, **attributes},
        "last_changed": _iso(NOW - 3600),
        "last_updated": _iso(NOW - age),
        "last_reported": _iso(NOW - age),
    }


def _without_teams():
    """A sensor with no teams attribute at all."""
    sensor = _sensor([])
    del sensor["attributes"]["teams"]
    return sensor


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


def _football(home, stale_after=300):
    return Football(home, lambda: {"stale_after": stale_after}, now=lambda: NOW)


def test_the_teams_and_their_matches_are_read():
    home = FakeHome({FOOTBALL_TEAMS: _sensor([_team("364", "Liverpool", asked="Liverpool", now=_found_match(
        id="9", state="in", phase="second_half", clock="67'", home="Liverpool", away="Man Utd",
        away_name="Manchester United", home_score=2, away_score=1))])})
    reading = _football(home).read()
    team = reading.teams[0]
    assert (team.id, team.name, team.asked, team.stale) == ("364", "Liverpool", "Liverpool", False)
    assert (team.now.home, team.now.away, team.now.home_score, team.now.phase) == ("Liverpool", "Manchester United", 2, "second_half")
    assert team.now.kickoff.tzinfo is not None


@pytest.mark.parametrize("sensor", [
    _sensor([], error="OSError: down"), _sensor([], state="unavailable"), _sensor([], state="unknown"),
    _sensor([], age=301), _sensor("not a list"), _sensor([{"id": "364", "name": "Liverpool", "next": [{"id": "1"}], "last": []}]),
])
def test_football_cant_be_got(sensor):
    with pytest.raises(FootballUnavailable) as caught:
        _football(FakeHome({FOOTBALL_TEAMS: sensor})).read()
    assert str(caught.value) == "I can't get football right now."


def test_a_reading_five_minutes_old_is_still_good():
    assert _football(FakeHome({FOOTBALL_TEAMS: _sensor([], age=300)})).read().teams == ()


def test_no_sensor_says_football_isnt_set_up():
    missing = requests.HTTPError("404", response=SimpleNamespace(status_code=404))
    with pytest.raises(FootballUnavailable) as caught:
        _football(FakeHome({FOOTBALL_TEAMS: missing})).read()
    assert str(caught.value) == NOT_SET_UP == "Football isn't set up in Home Assistant."


def test_home_assistant_down_says_so():
    with pytest.raises(FootballUnavailable) as caught:
        _football(FakeHome({FOOTBALL_TEAMS: requests.ConnectionError("refused")})).read()
    assert str(caught.value) == "I can't reach Home Assistant right now."


@pytest.mark.parametrize("failure, said", [
    (requests.HTTPError("500", response=SimpleNamespace(status_code=500)), "Home Assistant had a problem with that."),
    (requests.HTTPError("401", response=SimpleNamespace(status_code=401)),
     "Home Assistant didn't accept my token. HA_TOKEN in the .env file needs checking."),
    (ValueError("not a state"), "Home Assistant had a problem with that."),
])
def test_any_other_failure_is_said_as_nova_says_it_elsewhere(failure, said):
    with pytest.raises(FootballUnavailable) as caught:
        _football(FakeHome({FOOTBALL_TEAMS: failure})).read()
    assert str(caught.value) == said


def test_one_team_marked_stale():
    stevenage = _team("395", "Stevenage", stale=True)
    liverpool = _team("364", "Liverpool")
    home = FakeHome({FOOTBALL_TEAMS: _sensor([stevenage, liverpool], stale=True)})
    teams = {team.name: team for team in _football(home).read().teams}
    assert teams["Stevenage"].stale is True
    assert teams["Liverpool"].stale is False


def test_an_older_home_assistant_marks_every_team():
    # It doesn't mark teams yet, so with the sensor stale nobody can say whose data is old: all of it is.
    older = _found_match(home="Man Utd", away="Chelsea")
    teams = [_team("360", "Manchester United", next=[older]), _team("364", "Liverpool")]
    home = FakeHome({FOOTBALL_TEAMS: _sensor(teams, stale=True)})
    reading = _football(home).read()
    assert [team.stale for team in reading.teams] == [True, True]
    assert reading.teams[0].next[0].phase is None
    assert reading.teams[0].asked is None
    assert reading.teams[0].next[0].home == "Man Utd"


def test_a_sensor_that_isnt_stale_marks_no_team_it_doesnt_say_is():
    home = FakeHome({FOOTBALL_TEAMS: _sensor([_team("364", "Liverpool")], stale=False)})
    assert _football(home).read().teams[0].stale is False


def test_names_come_from_the_last_good_read():
    home = FakeHome({FOOTBALL_TEAMS: _sensor([_team("395", "Stevenage"), _team("364", "Liverpool")])})
    football = _football(home)
    assert football.names() == []
    football.read()
    assert football.names() == ["Stevenage", "Liverpool"]
    home.states[FOOTBALL_TEAMS] = requests.ConnectionError("refused")
    with pytest.raises(FootballUnavailable):
        football.read()
    assert football.names() == ["Stevenage", "Liverpool"]


def test_the_unknown_names_and_the_trimmed_flag_are_read():
    home = FakeHome({FOOTBALL_TEAMS: _sensor([], unknown=["Nowhere FC"], trimmed=True)})
    reading = _football(home).read()
    assert (reading.unknown, reading.trimmed) == (("Nowhere FC",), True)
    # Both are left out when there is nothing to say.
    plain = _football(FakeHome({FOOTBALL_TEAMS: _sensor([])})).read()
    assert (plain.unknown, plain.trimmed) == ((), False)


def test_how_old_a_reading_may_be_is_a_setting_read_each_time():
    home = FakeHome({FOOTBALL_TEAMS: _sensor([], age=100)})
    stale_after = {"stale_after": 60}
    football = Football(home, lambda: stale_after, now=lambda: NOW)
    with pytest.raises(FootballUnavailable):
        football.read()
    stale_after["stale_after"] = 120
    assert football.read().teams == ()
    # With no setting, five minutes.
    assert Football(FakeHome({FOOTBALL_TEAMS: _sensor([], age=300)}), dict, now=lambda: NOW).read().teams == ()
    with pytest.raises(FootballUnavailable):
        Football(FakeHome({FOOTBALL_TEAMS: _sensor([], age=301)}), dict, now=lambda: NOW).read()


def test_a_reading_is_as_old_as_it_was_last_reported_not_last_changed():
    # Home Assistant only changes a state when it differs, but reports it each time the script runs.
    sensor = _sensor([], age=10)
    sensor["last_changed"] = _iso(NOW - 86400)
    sensor["last_updated"] = _iso(NOW - 86400)
    assert _football(FakeHome({FOOTBALL_TEAMS: sensor})).read().teams == ()


def test_a_home_assistant_with_no_last_reported_is_read_by_last_updated():
    sensor = _sensor([], age=10)
    del sensor["last_reported"]
    assert _football(FakeHome({FOOTBALL_TEAMS: sensor})).read().teams == ()
    sensor["last_updated"] = _iso(NOW - 301)
    with pytest.raises(FootballUnavailable):
        _football(FakeHome({FOOTBALL_TEAMS: sensor})).read()


@pytest.mark.parametrize("sensor", [
    _without_teams(),
    {**_sensor([]), "last_reported": None, "last_updated": None},
    {**_sensor([]), "last_reported": "yesterday"},
    {**_sensor([]), "last_reported": "2026-11-13T02:42:14"},
    {**_sensor([]), "state": None},
    _sensor(["Liverpool"]),
    _sensor([_team("364", "Liverpool", stale="false")]),
    _sensor([], unknown="Nowhere FC"),
    _sensor([], trimmed="yes"),
    _sensor([_team("364", "Liverpool", next=None)]),
    _sensor([_team("364", "Liverpool", now=_found_match(state="live"))]),
], ids=["no teams key", "no time", "time not a time", "time with no zone", "no state", "team not a dict",
        "stale not a flag", "unknown not a list", "trimmed not a flag", "next not a list", "match state unknown"])
def test_a_reading_of_the_wrong_shape_cant_be_got(sensor):
    with pytest.raises(FootballUnavailable) as caught:
        _football(FakeHome({FOOTBALL_TEAMS: sensor})).read()
    assert str(caught.value) == "I can't get football right now."


def test_a_match_has_what_the_contract_gives_it():
    found = _found_match(
        id="9", state="post", phase="full_time", clock="FT", home="Man Utd", away="Chelsea", home_name="Manchester United",
        home_score=1, away_score=1, winner="away", shootout=[3, 4], extra_time=True,
        venue={"name": "Stamford Bridge", "city": "London"}, odds={"home": 55, "draw": 23, "away": 22, "source": "DraftKings"})
    match = Match.from_home(found)
    assert (match.id, match.state, match.phase, match.clock) == ("9", "post", "full_time", "FT")
    assert (match.home, match.away, match.home_id, match.away_id) == ("Manchester United", "Chelsea", "364", "359")
    assert (match.home_score, match.away_score, match.winner) == (1, 1, "away")
    assert (match.shootout, match.extra_time, match.league) == ((3, 4), True, "Premier League")
    assert match.venue == "Stamford Bridge in London"
    assert match.odds == {"home": 55, "draw": 23, "away": 22, "source": "DraftKings"}


def test_a_match_with_nothing_more_to_say_has_nothing_more():
    match = Match.from_home(_found_match())
    assert (match.phase, match.clock, match.home_score, match.away_score) == (None, "", None, None)
    assert (match.winner, match.shootout, match.extra_time, match.venue, match.odds) == (None, None, False, None, None)


def test_the_kick_off_is_the_same_instant_whichever_way_home_assistant_writes_it():
    # The instant is what matters, so the same one in London time is the same kick-off.
    utc = Match.from_home(_found_match(kickoff="2026-11-22T16:30:00+00:00"))
    summer = Match.from_home(_found_match(kickoff="2026-07-22T17:30:00+01:00"))
    assert utc.kickoff == datetime(2026, 11, 22, 16, 30, tzinfo=timezone.utc)
    assert summer.kickoff == datetime(2026, 7, 22, 16, 30, tzinfo=timezone.utc)
    assert utc.kickoff.utcoffset().total_seconds() == summer.kickoff.utcoffset().total_seconds() == 0


def test_a_ground_with_no_city_is_just_its_name():
    assert Match.from_home(_found_match(venue={"name": "Anfield"})).venue == "Anfield"
    assert Match.from_home(_found_match(venue={"name": "Anfield", "city": ""})).venue == "Anfield"
    assert Match.from_home(_found_match(venue={"name": "Anfield", "city": "Liverpool"})).venue == "Anfield in Liverpool"
    # A city on its own says nothing about where the match is.
    assert Match.from_home(_found_match(venue={"city": "Liverpool"})).venue is None


def test_a_full_name_is_said_in_place_of_the_short_one_on_both_sides():
    match = Match.from_home(_found_match(home="Man Utd", away="Spurs", away_name="Tottenham Hotspur"))
    assert (match.home, match.away) == ("Man Utd", "Tottenham Hotspur")


@pytest.mark.parametrize("fields", [
    {"id": 9}, {"kickoff": "tomorrow"}, {"kickoff": "2026-11-22T16:30:00"}, {"state": "live"}, {"home_score": "2"},
    {"home_score": True}, {"winner": "draw"}, {"shootout": [3]}, {"shootout": ["3", "4"]}, {"extra_time": "true"},
    {"venue": "Anfield"}, {"odds": [55, 23, 22]}, {"home": None},
], ids=lambda fields: next(iter(fields)) + "=" + repr(next(iter(fields.values()))))
def test_a_match_of_the_wrong_shape_is_refused(fields):
    with pytest.raises((KeyError, TypeError, ValueError)):
        Match.from_home(_found_match(**fields))


@pytest.mark.parametrize("missing", ["id", "kickoff", "state", "home", "away", "home_id", "away_id", "league"])
def test_a_match_must_have_what_the_contract_says_it_always_has(missing):
    found = _found_match()
    del found[missing]
    with pytest.raises(KeyError):
        Match.from_home(found)


# The words Claude is given

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


# Match days used below, in the same week as 13 November 2026 (a Friday) or a few weeks off.
LIVE = {"id": "9", "state": "in", "phase": "second_half", "clock": "67'", "home": "Liverpool", "away": "Man Utd",
        "away_name": "Manchester United", "home_score": 2, "away_score": 1}
SUNDAY_AWAY = {"id": "10", "kickoff": "2026-11-22T16:30:00+00:00", "home": "Arsenal", "away": "Liverpool"}
PALACE_LOSS = {"id": "8", "state": "post", "phase": "full_time", "clock": "FT", "kickoff": "2026-11-08T16:30:00+00:00",
               "home": "Crystal Palace", "away": "Liverpool", "home_score": 1, "away_score": 3, "winner": "away"}


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
    # With a place in a table, a fixture to come or a match today, its league is covered.
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
