"""Tests for reading the followed football teams - what Home Assistant's sensor says, and when football can't be got."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

from voice_assistant.football import NOT_SET_UP, Football, FootballUnavailable, Match
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
