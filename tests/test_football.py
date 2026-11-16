"""Tests for reading the followed football teams - what Home Assistant's sensor says, and when football can't be got."""

import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

from voice_assistant.football import (
    BOX_HOLDS, LOOK_TIMEOUT, NOT_SET_UP, WAIT_FOR_SENSOR, Football, FootballUnavailable, Match, MatchEvent)
from voice_assistant.home_assistant import FOOTBALL_BOX, FOOTBALL_TEAMS, REQUEST_TIMEOUT

# An instant well away from any change of clock, so that a test passes in whichever time zone it runs.
NOW = 1_800_000_000.0


def _iso(epoch):
    """An epoch time as Home Assistant writes one, e.g. "2026-11-13T02:42:14.233328+00:00"."""
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class FakeHome:
    """
    Answers state() as Home Assistant does, with what a test sets for each entity, or raises what it sets.

    The teams box is kept as text (or an error to raise), and football_ask answers with answer (or raises it).
    Each value written to the box is noted in written, and then given to after_write along with this home, to
    change the sensor as Home Assistant would; write_fails is raised in place of writing. The timeout each
    look at the sensor was given is noted in sensor_timeouts.
    """

    def __init__(self, states, box="", answer=None, after_write=None, write_fails=None):
        self.states = dict(states)
        self.box = box
        self.answer = answer
        self.after_write = after_write
        self.write_fails = write_fails
        self.written = []
        self.asked = []
        self.sensor_timeouts = []

    def state(self, entity_id, timeout=None):
        if entity_id == FOOTBALL_TEAMS:
            self.sensor_timeouts.append(timeout)
        if entity_id == FOOTBALL_BOX:
            found = {"state": self.box, "attributes": {}} if not isinstance(self.box, Exception) else self.box
        else:
            found = self.states[entity_id]
        if isinstance(found, Exception):
            raise found
        return found

    def football_ask(self, what, name, date="", days=1):
        self.asked.append((what, name, date, days))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def set_football_teams(self, value):
        if self.write_fails is not None:
            raise self.write_fails
        self.written.append(value)
        self.box = value
        if self.after_write is not None:
            self.after_write(self, value)


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


def _football(home, stale_after=300, sleep=None):
    # Nothing waits for real: sleep is what counts the seconds, or does nothing.
    return Football(home, lambda: {"stale_after": stale_after}, now=lambda: NOW, sleep=sleep or (lambda seconds: None))


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


@pytest.mark.parametrize("sensor, why", [
    (_sensor([], error="OSError: down"), "the sensor has an error: 'OSError: down'"),
    (_sensor([], state="unavailable"), "the sensor is unavailable"),
    (_sensor([], age=301), "the sensor was last written 301 seconds ago"),
    (_sensor("not a list"), "the sensor isn't shaped as expected"),
], ids=["an error", "unavailable", "too old", "wrong shape"])
def test_why_football_cant_be_got_is_kept_on_the_error_but_never_said(sensor, why):
    with pytest.raises(FootballUnavailable) as caught:
        _football(FakeHome({FOOTBALL_TEAMS: sensor})).read()
    assert caught.value.why == why
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
    liverpool = _team("364", "Liverpool", stale=False)
    home = FakeHome({FOOTBALL_TEAMS: _sensor([stevenage, liverpool], stale=True)})
    teams = {team.name: team for team in _football(home).read().teams}
    assert teams["Stevenage"].stale is True
    assert teams["Liverpool"].stale is False


def test_a_team_marked_stale_is_stale_whatever_the_sensor_says():
    home = FakeHome({FOOTBALL_TEAMS: _sensor([_team("395", "Stevenage", stale=True), _team("364", "Liverpool", stale=False)], stale=False)})
    assert [team.stale for team in _football(home).read().teams] == [True, False]


def test_a_stale_sensor_with_every_team_saying_it_isnt_leaves_every_team_fresh():
    # An entry nobody follows went wrong, so the sensor is stale; each team says its own data is fine, and that stands.
    teams = [_team("395", "Stevenage", stale=False), _team("364", "Liverpool", stale=False)]
    home = FakeHome({FOOTBALL_TEAMS: _sensor(teams, stale=True)})
    assert [team.stale for team in _football(home).read().teams] == [False, False]


def test_an_older_home_assistant_marks_every_team():
    # It doesn't mark teams yet - no team has a "stale" at all - so with the sensor stale nobody can say whose
    # data is old: all of it is.
    older = _found_match(home="Man Utd", away="Chelsea")
    teams = [_team("360", "Manchester United", next=[older]), _team("364", "Liverpool")]
    home = FakeHome({FOOTBALL_TEAMS: _sensor(teams, stale=True)})
    reading = _football(home).read()
    assert [team.stale for team in reading.teams] == [True, True]
    assert reading.teams[0].next[0].phase is None
    assert reading.teams[0].asked is None
    assert reading.teams[0].next[0].home == "Man Utd"


def test_a_stale_sensor_with_one_team_marked_and_another_with_no_mark_leaves_the_other_fresh():
    # Only a Home Assistant which marks no team at all leaves who is stale to the sensor.
    teams = [_team("395", "Stevenage", stale=True), _team("364", "Liverpool")]
    home = FakeHome({FOOTBALL_TEAMS: _sensor(teams, stale=True)})
    assert [team.stale for team in _football(home).read().teams] == [True, False]


def test_a_team_whose_stale_is_null_counts_as_having_no_mark():
    # With no team marked otherwise the sensor decides, as for a team which leaves the key out; beside a team which says it isn't, it doesn't.
    nobody = [_team("395", "Stevenage", stale=None), _team("364", "Liverpool", stale=None)]
    home = FakeHome({FOOTBALL_TEAMS: _sensor(nobody, stale=True)})
    assert [team.stale for team in _football(home).read().teams] == [True, True]
    one = [_team("395", "Stevenage", stale=False), _team("364", "Liverpool", stale=None)]
    home = FakeHome({FOOTBALL_TEAMS: _sensor(one, stale=True)})
    assert [team.stale for team in _football(home).read().teams] == [False, False]


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


def test_every_name_a_team_is_known_by_comes_from_the_last_good_read():
    # Its short name, the full name its match gives it, and the entry of the box which found it - each said
    # without repeating one, and "Liverpool" is all three.
    forest = _team("393", "Nott'm Forest", asked="Forest", next=[_found_match(
        home="Nott'm Forest", home_name="Nottingham Forest", home_id="393", away="Arsenal", away_id="359")])
    home = FakeHome({FOOTBALL_TEAMS: _sensor([forest, _team("364", "Liverpool", asked="Liverpool")])})
    football = _football(home)
    assert football.known_as() == []
    football.read()
    assert football.known_as() == ["Nott'm Forest", "Nottingham Forest", "Forest", "Liverpool"]
    assert football.names() == ["Nott'm Forest", "Liverpool"]
    home.states[FOOTBALL_TEAMS] = requests.ConnectionError("refused")
    with pytest.raises(FootballUnavailable):
        football.read()
    assert football.known_as() == ["Nott'm Forest", "Nottingham Forest", "Forest", "Liverpool"]


def test_a_team_without_a_match_or_an_entry_is_only_known_by_its_name():
    # An older Home Assistant gives no entry for a team, and a team may have no match to give a full name.
    home = FakeHome({FOOTBALL_TEAMS: _sensor([_team("395", "Stevenage")])})
    football = _football(home)
    football.read()
    assert football.known_as() == ["Stevenage"]


def test_the_id_which_stands_in_for_a_name_is_not_a_name_a_team_is_known_by():
    # After a follow by id the entry which found Rangers is "257", and nobody asks for a team by that: a request
    # with the number in it would be taken to be about football, and go to Claude and not to Assist.
    rangers = _team("257", "Rangers", asked="257")
    football = _football(FakeHome({FOOTBALL_TEAMS: _sensor([rangers, _team("364", "Liverpool", asked="Liverpool")])}))
    football.read()
    assert football.known_as() == ["Rangers", "Liverpool"]
    # It can still be unfollowed by that number, as it is what the box holds.
    home = FakeHome({FOOTBALL_TEAMS: _sensor([rangers])}, box="Stevenage, 257")
    assert _football(home).unfollow("257") == {"success": True, "message": "You've stopped following Rangers."}
    assert home.written == ["Stevenage"]


def test_an_entry_no_team_was_found_by_is_not_a_name_a_team_is_known_by():
    home = FakeHome({FOOTBALL_TEAMS: _sensor([_team("395", "Stevenage")], unknown=["Nowhere FC"])})
    football = _football(home)
    football.read()
    assert football.known_as() == ["Stevenage"]


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


def test_a_reading_says_when_home_assistant_wrote_the_sensor_as_an_epoch_time():
    # What tells one reading from the same one looked at again: it is later each time the script runs.
    sensor = _sensor([], age=10)
    sensor["last_changed"] = _iso(NOW - 86400)
    sensor["last_updated"] = _iso(NOW - 3600)
    assert _football(FakeHome({FOOTBALL_TEAMS: sensor})).read().reported == NOW - 10
    # A Home Assistant with no last_reported has only last_updated.
    del sensor["last_reported"]
    sensor["last_updated"] = _iso(NOW - 20)
    assert _football(FakeHome({FOOTBALL_TEAMS: sensor})).read().reported == NOW - 20


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


def test_a_match_has_its_events_in_the_order_they_happened():
    match = Match.from_home(_found_match(state="post", home_score=1, away_score=2, events=[
        {"minute": "45'", "kind": "penalty", "side": "away", "player": "Harry Kane"},
        {"minute": "63'", "kind": "red_card", "side": "away"},
        {"minute": "90'+3'", "kind": "own_goal", "side": "home", "player": None},
    ]))
    assert match.events == (
        MatchEvent("45'", "penalty", "away", "Harry Kane"),
        MatchEvent("63'", "red_card", "away", None),
        MatchEvent("90'+3'", "own_goal", "home", None),
    )


@pytest.mark.parametrize("kind", ["goal", "penalty", "own_goal", "penalty_missed", "red_card"])
def test_each_kind_of_event_the_contract_has_is_read(kind):
    match = Match.from_home(_found_match(events=[{"minute": "12'", "kind": kind, "side": "home", "player": "Harry Kane"}]))
    assert match.events == (MatchEvent("12'", kind, "home", "Harry Kane"),)


@pytest.mark.parametrize("events", [None, []], ids=["null", "empty"])
def test_a_match_given_no_events_has_none(events):
    assert Match.from_home(_found_match(events=events)).events == ()


def test_a_match_with_no_events_key_has_none():
    assert "events" not in _found_match()
    assert Match.from_home(_found_match()).events == ()


@pytest.mark.parametrize("events", [
    "goal", {"minute": "12'", "kind": "goal", "side": "home"}, ["goal"],
    [{"kind": "goal", "side": "home"}], [{"minute": "12'", "side": "home"}], [{"minute": "12'", "kind": "goal"}],
    [{"minute": 12, "kind": "goal", "side": "home"}], [{"minute": "", "kind": "goal", "side": "home"}],
    [{"minute": "12'", "kind": None, "side": "home"}], [{"minute": "12'", "kind": "goal", "side": 1}],
    [{"minute": "12'", "kind": "goal", "side": "home", "player": 9}],
    [{"minute": "12'", "kind": "goal", "side": "home", "player": ["Harry Kane"]}],
], ids=["not a list", "a dict", "an event not a dict", "no minute", "no kind", "no side", "minute not text",
        "minute empty", "kind not text", "side not text", "player a number", "player a list"])
def test_events_of_the_wrong_shape_are_refused(events):
    with pytest.raises((KeyError, TypeError, ValueError)):
        Match.from_home(_found_match(events=events))


@pytest.mark.parametrize("change", [
    {"kind": "yellow_card"}, {"kind": "Goal"}, {"kind": "penalty_saved"}, {"kind": ""},
    {"side": "both"}, {"side": "Home"}, {"side": "England"}, {"side": ""},
], ids=lambda change: next(iter(change)) + "=" + repr(next(iter(change.values()))))
def test_an_event_of_a_kind_or_side_outside_the_contract_is_refused(change):
    event = {"minute": "12'", "kind": "goal", "side": "home", **change}
    with pytest.raises(ValueError):
        Match.from_home(_found_match(events=[event]))


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


class FakeAsking:
    """Answers football_ask() as Home Assistant does: with what a test sets, or raising it - and notes what it was asked."""

    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def football_ask(self, what, name, date="", days=1, against=""):
        # The other team is noted only when there is one, so that a question without it reads as it always did.
        self.asked.append((what, name, date, days) + ((against,) if against else ()))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_a_question_is_asked_of_home_assistant_as_given():
    home = FakeAsking({"team": {"id": "359", "name": "Arsenal"}, "stale": False})
    answer = _football(home).ask("competition", "Champions League", "20261124", 2)
    assert answer == {"team": {"id": "359", "name": "Arsenal"}, "stale": False}
    assert home.asked == [("competition", "Champions League", "20261124", 2)]


def test_a_question_with_no_date_or_days_is_asked_for_today_and_one_day():
    home = FakeAsking({"stale": False})
    _football(home).ask("team", "Arsenal")
    assert home.asked == [("team", "Arsenal", "", 1)]


def test_the_other_team_is_asked_of_home_assistant_with_a_match():
    home = FakeAsking({"team": {"id": "364", "name": "Liverpool"}, "seasons": 2, "stale": False})
    answer = _football(home).ask("match", "Liverpool", against="Newcastle")
    assert answer == {"team": {"id": "364", "name": "Liverpool"}, "seasons": 2, "stale": False}
    assert home.asked == [("match", "Liverpool", "", 1, "Newcastle")]


@pytest.mark.parametrize("against", [None, ""])
def test_no_other_team_is_asked_as_before(against):
    home = FakeAsking({"stale": False})
    _football(home).ask("match", "Liverpool", "20250316", against=against)
    assert home.asked == [("match", "Liverpool", "20250316", 1)]


@pytest.mark.parametrize("answer", [
    {"stale": True, "seasons": 6, "team": {}}, {"stale": False, "error": "OSError: down"},
    {"stale": True, "unknown": ["Newcastel"]}, {"seasons": 6, "team": {}},
], ids=["stale", "error", "stale and unknown", "no stale key"])
def test_an_old_or_failed_answer_for_the_other_team_cant_be_got(answer):
    with pytest.raises(FootballUnavailable) as caught:
        _football(FakeAsking(answer)).ask("match", "Liverpool", against="Newcastle")
    assert str(caught.value) == "I can't get football right now."


def test_a_command_which_doesnt_know_the_other_team_cant_be_got():
    # Home Assistant's command fails where its script is from before --against, as its error says.
    failure = ValueError("Home Assistant's football command failed: 2, 'unrecognized arguments: --against=Newcastle'")
    with pytest.raises(FootballUnavailable) as caught:
        _football(FakeAsking(failure)).ask("match", "Liverpool", against="Newcastle")
    assert str(caught.value) == "I can't get football right now."


def test_a_name_espn_doesnt_know_is_an_answer_not_a_failure():
    answer = {"unknown": ["Arsnal"], "stale": False}
    assert _football(FakeAsking(answer)).ask("team", "Arsnal") == answer


@pytest.mark.parametrize("answer", [
    {"stale": True, "team": {}}, {"stale": False, "error": "OSError: down"}, {"stale": True, "error": "OSError: down"},
    {"team": {}}, {"stale": "false", "team": {}}, {"stale": None}, {"stale": True, "unknown": ["Arsnal"]}, ["stale"], None,
], ids=["stale", "error", "stale and error", "no stale key", "stale not a flag", "stale empty", "stale and unknown",
        "not a dict", "nothing"])
def test_an_old_or_failed_or_unreadable_answer_cant_be_got(answer):
    with pytest.raises(FootballUnavailable) as caught:
        _football(FakeAsking(answer)).ask("team", "Arsenal")
    assert str(caught.value) == "I can't get football right now."


@pytest.mark.parametrize("failure", [
    ValueError("Home Assistant's football command failed: 1, 'boom'"), requests.Timeout("slow"), requests.ReadTimeout("slow"),
    requests.exceptions.JSONDecodeError("not json", "", 0),
], ids=["unreadable", "timeout", "read timeout", "not json"])
def test_an_answer_which_cant_be_read_or_didnt_come_in_time_cant_be_got(failure):
    # A timeout is football not being got in time, not Home Assistant being out of reach as it would be said elsewhere.
    with pytest.raises(FootballUnavailable) as caught:
        _football(FakeAsking(failure)).ask("team", "Arsenal")
    assert str(caught.value) == "I can't get football right now."


@pytest.mark.parametrize("failure, said", [
    (requests.ConnectionError("refused"), "I can't reach Home Assistant right now."),
    (requests.HTTPError("500", response=SimpleNamespace(status_code=500)), "Home Assistant had a problem with that."),
    (requests.HTTPError("401", response=SimpleNamespace(status_code=401)),
     "Home Assistant didn't accept my token. HA_TOKEN in the .env file needs checking."),
])
def test_home_assistant_not_being_reached_is_said_as_nova_says_it_elsewhere(failure, said):
    with pytest.raises(FootballUnavailable) as caught:
        _football(FakeAsking(failure)).ask("team", "Arsenal")
    assert str(caught.value) == said


def test_a_football_command_home_assistant_hasnt_got_is_said_as_football_not_being_set_up():
    # Home Assistant answers 400 for a service it doesn't have, or one which can't return a response, which is
    # football not being set up there - as the overview says of a sensor it hasn't got - not "a problem with that".
    failure = requests.HTTPError("400", response=SimpleNamespace(status_code=400))
    with pytest.raises(FootballUnavailable) as caught:
        _football(FakeAsking(failure)).ask("team", "Arsenal")
    assert str(caught.value) == NOT_SET_UP
    # So is a follow, which asks first, and nothing is written.
    home = FakeHome({FOOTBALL_TEAMS: _sensor([])}, box="Stevenage", answer=failure)
    assert _football(home).follow("Arsenal") == {"success": False, "message": NOT_SET_UP}
    assert home.written == []


# Following and unfollowing: the teams box is what is written, and the sensor is what shows it took.

def _stevenage_and_liverpool():
    """The two teams most of these tests start with, as the sensor has them."""
    return [_team("395", "Stevenage", asked="Stevenage"), _team("364", "Liverpool", asked="Liverpool")]


def _home_with_box(box="Stevenage, Liverpool", **kept):
    """A Home Assistant whose box holds box, whose sensor shows Stevenage and Liverpool, and which acts as kept says."""
    return FakeHome({FOOTBALL_TEAMS: _sensor(_stevenage_and_liverpool())}, box=box, **kept)


def _asked_of(team_id, name, followed=False, **fields):
    """Home Assistant's answer to a question about one team, with only what following needs of the Contract's."""
    return {"team": {"id": team_id, "name": name, "followed": followed, **fields}, "stale": False}


def _shows(*teams, unknown=()):
    """What Home Assistant does once the box is written: its sensor shows these teams."""
    def after_write(home, value):
        home.states[FOOTBALL_TEAMS] = _sensor(list(teams), unknown=list(unknown))
    return after_write


ARSENAL = _team("359", "Arsenal", asked="Arsenal")


def test_following_a_team():
    home = _home_with_box(answer=_asked_of("359", "Arsenal"), after_write=_shows(*_stevenage_and_liverpool(), ARSENAL))
    assert _football(home).follow("Arsenal") == {"success": True, "message": "You're now following Arsenal."}
    assert home.written == ["Stevenage, Liverpool, Arsenal"]
    assert home.asked == [("team", "Arsenal", "", 1)]


def test_following_says_the_full_name():
    newcastle = _team("361", "Newcastle", asked="Newcastle")
    home = _home_with_box(answer=_asked_of("361", "Newcastle", full_name="Newcastle United"),
                          after_write=_shows(*_stevenage_and_liverpool(), newcastle))
    assert _football(home).follow("Newcastle") == {"success": True, "message": "You're now following Newcastle United."}
    # The name is what Home Assistant finds the team by again, so that is what is written.
    assert home.written == ["Stevenage, Liverpool, Newcastle"]


def test_what_is_written_is_what_home_assistant_found_not_what_was_said():
    home = _home_with_box(answer=_asked_of("359", "Arsenal"), after_write=_shows(*_stevenage_and_liverpool(), ARSENAL))
    _football(home).follow("  the Gunners ")
    assert home.asked == [("team", "the Gunners", "", 1)]
    assert home.written == ["Stevenage, Liverpool, Arsenal"]


@pytest.mark.parametrize("box, written", [
    ("", "Arsenal"),
    ("  stevenage ,Liverpool,, ", "stevenage, Liverpool, Arsenal"),
])
def test_the_box_is_written_back_as_clean_entries(box, written):
    home = _home_with_box(box, answer=_asked_of("359", "Arsenal"), after_write=_shows(ARSENAL))
    _football(home).follow("Arsenal")
    assert home.written == [written]


def test_it_waits_a_second_at_a_time_until_the_sensor_shows_the_team():
    home = _home_with_box(answer=_asked_of("359", "Arsenal"))
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 3:
            home.states[FOOTBALL_TEAMS] = _sensor([*_stevenage_and_liverpool(), ARSENAL])

    assert _football(home, sleep=sleep).follow("Arsenal")["message"] == "You're now following Arsenal."
    assert slept == [1, 1, 1]


def test_already_followed():
    home = _home_with_box(answer=_asked_of("359", "Arsenal", followed=True))
    assert _football(home).follow("Arsenal") == {"success": True, "message": "You're already following Arsenal."}
    assert home.written == []


def test_nothing_by_that_name_is_followed():
    home = _home_with_box(answer={"unknown": ["Arsnal"], "stale": False})
    assert _football(home).follow("Arsnal") == {"success": False, "message": "I couldn't find a team called Arsnal."}
    assert home.written == []


@pytest.mark.parametrize("answer", [
    {"stale": False}, {"team": "Arsenal", "stale": False}, {"team": {"name": "Arsenal", "followed": False}, "stale": False},
    {"team": {"id": 359, "name": "Arsenal", "followed": False}, "stale": False},
    {"team": {"id": "359", "name": "", "followed": False}, "stale": False},
    {"team": {"id": "359", "name": "Arsenal", "followed": "no"}, "stale": False},
], ids=["no team", "team not a dict", "no id", "id not text", "no name", "followed not a flag"])
def test_an_answer_which_isnt_a_team_is_not_followed(answer):
    home = _home_with_box(answer=answer)
    assert _football(home).follow("Arsenal") == {"success": False, "message": "I can't get football right now."}
    assert home.written == []


@pytest.mark.parametrize("shown_first", [
    lambda: _sensor([*_stevenage_and_liverpool(), _team("334", "Queens Park Rangers", asked="Rangers")]),
    lambda: _sensor(_stevenage_and_liverpool(), unknown=["rangers"]),
], ids=["another team", "nothing"])
def test_a_name_which_finds_another_team_is_followed_by_id(shown_first):
    # "Rangers" alone finds Queens Park Rangers, or nothing, while ESPN's id 257 is the club which was asked about.
    rangers = _team("257", "Rangers", asked="257")

    def after_write(home, value):
        home.states[FOOTBALL_TEAMS] = shown_first() if value.endswith(", Rangers") else _sensor([*_stevenage_and_liverpool(), rangers])

    home = _home_with_box("Stevenage", answer=_asked_of("257", "Rangers"), after_write=after_write)
    assert _football(home).follow("Rangers") == {"success": True, "message": "You're now following Rangers."}
    assert home.written == ["Stevenage, Rangers", "Stevenage, 257"]


def test_a_name_with_an_apostrophe_which_finds_another_team_is_followed_by_id():
    # The box has "Nott'm Forest" but Home Assistant strips the quote before its script looks, so the sensor says
    # "Nottm Forest" found nothing - which only counts as the entry written if the quote is ignored in comparing.
    forest = _team("393", "Nott'm Forest", asked="393")

    def after_write(home, value):
        found = [*_stevenage_and_liverpool(), forest] if value.endswith(", 393") else _stevenage_and_liverpool()
        home.states[FOOTBALL_TEAMS] = _sensor(found, unknown=[] if value.endswith(", 393") else ["Nottm Forest"])

    home = _home_with_box("Stevenage", answer=_asked_of("393", "Nott'm Forest", full_name="Nottingham Forest"), after_write=after_write)
    assert _football(home).follow("Nottingham Forest") == {"success": True, "message": "You're now following Nottingham Forest."}
    assert home.written == ["Stevenage, Nott'm Forest", "Stevenage, 393"]


def test_a_team_in_the_box_without_the_apostrophe_of_its_name_isnt_added_again():
    # Typed by hand without it, "Nottm Forest" is the entry which "Nott'm Forest" would be to Home Assistant.
    home = _home_with_box("Stevenage, Nottm Forest", answer=_asked_of("393", "Nott'm Forest"))
    _football(home).follow("Nottingham Forest")
    assert home.written == []


def test_the_box_is_full():
    home = _home_with_box("x" * 250, answer=_asked_of("359", "Arsenal"))
    assert _football(home).follow("Arsenal") == {"success": False, "message": "Home Assistant's list of teams is full."}
    assert home.written == []


@pytest.mark.parametrize("length, fits", [(BOX_HOLDS, True), (BOX_HOLDS + 1, False)])
def test_a_value_exactly_as_long_as_the_box_holds_is_written_and_one_more_is_not(length, fits):
    # Home Assistant drops a value which is too long without a word, so Nova is the only one to say no.
    box = "x" * (length - len(", Arsenal"))
    home = _home_with_box(box, answer=_asked_of("359", "Arsenal"), after_write=_shows(ARSENAL))
    result = _football(home).follow("Arsenal")
    assert result["success"] is fits
    assert home.written == ([f"{box}, Arsenal"] if fits else [])
    assert all(len(value) <= BOX_HOLDS for value in home.written)


def test_the_id_which_may_stand_in_for_a_name_has_to_fit_too():
    # An id is longer than a short name, and writing it in place of the name later mustn't overflow the box.
    box = "x" * (BOX_HOLDS - len(", Gent"))
    home = _home_with_box(box, answer=_asked_of("123456", "Gent"))
    assert _football(home).follow("Gent") == {"success": False, "message": "Home Assistant's list of teams is full."}
    assert home.written == []


def test_home_assistant_never_shows_it():
    home = _home_with_box(answer=_asked_of("359", "Arsenal"))
    slept = []
    result = _football(home, sleep=slept.append).follow("Arsenal")
    assert result == {"success": True, "message": "I've added Arsenal, but Home Assistant hasn't shown it yet."}
    assert sum(slept) == WAIT_FOR_SENSOR
    assert home.written == ["Stevenage, Liverpool, Arsenal"]


def test_a_sensor_which_cant_be_read_for_a_moment_is_waited_through():
    home = _home_with_box(answer=_asked_of("359", "Arsenal"))
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 2:
            home.states[FOOTBALL_TEAMS] = _sensor([*_stevenage_and_liverpool(), ARSENAL])

    def after_write(home, value):
        home.states[FOOTBALL_TEAMS] = requests.ConnectionError("restarting")

    home.after_write = after_write
    assert _football(home, sleep=sleep).follow("Arsenal")["message"] == "You're now following Arsenal."
    assert slept == [1, 1]


def test_a_team_already_in_the_box_which_hasnt_shown_yet_isnt_added_again():
    # A follow which timed out, asked again: the entry is there, so only the wait is left to do.
    home = _home_with_box("Stevenage, Liverpool, arsenal", answer=_asked_of("359", "Arsenal"))
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        home.states[FOOTBALL_TEAMS] = _sensor([*_stevenage_and_liverpool(), ARSENAL])

    assert _football(home, sleep=sleep).follow("Arsenal")["message"] == "You're now following Arsenal."
    assert home.written == []


def test_football_cant_be_got_so_nothing_is_followed():
    home = _home_with_box(answer=requests.Timeout("slow"))
    assert _football(home).follow("Arsenal") == {"success": False, "message": "I can't get football right now."}
    assert home.written == []


@pytest.mark.parametrize("how", ["follow", "unfollow"])
@pytest.mark.parametrize("box, said", [
    (requests.ConnectionError("refused"), "I can't reach Home Assistant right now."),
    (requests.HTTPError("404", response=SimpleNamespace(status_code=404)), "Football isn't set up in Home Assistant."),
    ("unavailable", "I can't get football right now."),
    ("unknown", "I can't get football right now."),
], ids=["down", "no box", "unavailable", "unknown"])
def test_a_box_which_cant_be_read_is_never_written(how, box, said):
    # "unavailable" is Home Assistant's word for a box it can't read, not a team, so it is never kept in the list.
    home = _home_with_box(box, answer=_asked_of("359", "Arsenal"))
    assert getattr(_football(home), how)("Arsenal") == {"success": False, "message": said}
    assert home.written == []


@pytest.mark.parametrize("how", ["follow", "unfollow"])
def test_a_write_home_assistant_refuses_says_so(how):
    home = _home_with_box(answer=_asked_of("359", "Arsenal"), write_fails=requests.ConnectionError("refused"))
    result = getattr(_football(home), how)("Liverpool" if how == "unfollow" else "Arsenal")
    assert result == {"success": False, "message": "I can't reach Home Assistant right now."}


@pytest.mark.parametrize("how", ["follow", "unfollow"])
@pytest.mark.parametrize("said", ["", "   ", "..."])
def test_words_with_no_team_in_them_are_not_a_team(how, said):
    home = _home_with_box(answer=_asked_of("359", "Arsenal"))
    assert getattr(_football(home), how)(said) == {"success": False, "message": "I didn't catch which team."}
    assert home.asked == home.written == []


def test_reading_isnt_held_up_while_a_follow_waits():
    # The watcher reads every 30 seconds and a session asks for the names as it builds its tools, so
    # neither may wait for a follow's seconds - nor can a follow's own lock be the one reads use.
    home = _home_with_box(answer=_asked_of("359", "Arsenal"), after_write=_shows(*_stevenage_and_liverpool(), ARSENAL))
    held_up = []

    def sleep(seconds):
        reader = threading.Thread(target=lambda: (football.read(), football.names()), daemon=True)
        reader.start()
        reader.join(2)
        held_up.append(reader.is_alive())

    football = _football(home, sleep=sleep)
    football.follow("Arsenal")
    assert held_up == [False]


def test_a_follow_and_an_unfollow_never_overlap():
    # The settings page's thread and a voice session's can both ask, and each reads the box and writes it back
    # whole, so one running in the middle of the other would lose its change.
    home = _home_with_box(answer=_asked_of("359", "Arsenal"), after_write=_shows(*_stevenage_and_liverpool(), ARSENAL))
    others, finished_in_time = [], []

    def sleep(seconds):
        other = threading.Thread(target=lambda: football.unfollow("Liverpool"), daemon=True)
        other.start()
        other.join(0.3)
        others.append(other)
        finished_in_time.append(not other.is_alive())

    football = _football(home, sleep=sleep)
    football.follow("Arsenal")
    others[0].join(5)
    assert finished_in_time == [False]
    assert home.box == "Stevenage, Arsenal"


def _four_teams():
    """Teams as the sensor has them when the box was typed by hand, each marked with the entry which found it."""
    return [_team("395", "Stevenage", asked="stevenage"), _team("334", "QPR", asked="Queens Park Rangers"),
            _team("257", "Rangers", asked="Rangers"), _team("4521", "South Shields", asked="South Shields FC")]


MESSY_BOX = "  stevenage ,Queens Park Rangers,  Rangers , South Shields FC"


@pytest.mark.parametrize("said, written, says", [
    ("Rangers", "stevenage, Queens Park Rangers, South Shields FC", "You've stopped following Rangers."),
    ("south shields", "stevenage, Queens Park Rangers, Rangers", "You've stopped following South Shields."),
    ("QPR", "stevenage, Rangers, South Shields FC", "You've stopped following QPR."),
    ("queens park rangers", "stevenage, Rangers, South Shields FC", "You've stopped following QPR."),
    ("Stevenage", "Queens Park Rangers, Rangers, South Shields FC", "You've stopped following Stevenage."),
    ("shields", "stevenage, Queens Park Rangers, Rangers", "You've stopped following South Shields."),
])
def test_unfollowing_takes_the_right_entry(said, written, says):
    # "Rangers" is inside Queens Park Rangers, whose entry has to stay; the box's stray spaces and capitals go with it.
    home = FakeHome({FOOTBALL_TEAMS: _sensor(_four_teams())}, box=MESSY_BOX)
    assert _football(home).unfollow(said) == {"success": True, "message": says}
    assert home.written == [written]


def test_unfollowing_without_asked():
    # An older Home Assistant doesn't say which entry found a team, so the entry which is its name is the one.
    teams = [_team("395", "Stevenage"), _team("257", "Rangers")]
    home = FakeHome({FOOTBALL_TEAMS: _sensor(teams)}, box="Stevenage, RANGERS")
    assert _football(home).unfollow("Rangers") == {"success": True, "message": "You've stopped following Rangers."}
    assert home.written == ["Stevenage"]
    # With no entry which is its name there is nothing to take out, and nothing is guessed at.
    home = FakeHome({FOOTBALL_TEAMS: _sensor(teams)}, box="Stevenage, Glasgow Rangers")
    assert _football(home).unfollow("Rangers") == {"success": False, "message": "I couldn't take Rangers off Home Assistant's list."}
    assert home.written == []


def test_an_entry_which_is_not_in_the_box_any_more_cant_be_taken_out():
    home = FakeHome({FOOTBALL_TEAMS: _sensor(_four_teams())}, box="stevenage, Rangers")
    assert _football(home).unfollow("QPR") == {"success": False, "message": "I couldn't take QPR off Home Assistant's list."}
    assert home.written == []


def test_unfollowing_a_team_nobody_follows():
    home = _home_with_box()
    assert _football(home).unfollow("Chelsea") == {"success": False, "message": "You're not following Chelsea."}
    assert home.written == []


def test_an_entry_home_assistant_couldnt_find_can_be_taken_off():
    # Typed twice, it is listed twice and is still one thing to take out, both copies of it.
    home = FakeHome({FOOTBALL_TEAMS: _sensor(_stevenage_and_liverpool(), unknown=["Stevnage", "stevnage"])},
                    box="Stevenage, Stevnage, Liverpool, stevnage")
    assert _football(home).unfollow("Stevnage") == {"success": True, "message": "Taken Stevnage off the list."}
    assert home.written == ["Stevenage, Liverpool"]


def test_a_team_whose_name_has_an_apostrophe_can_be_unfollowed():
    # Nova writes ESPN's short name in the box, "Nott'm Forest", and Home Assistant takes the quote out before its script
    # sees it, so the sensor says the entry which found the team was "Nottm Forest".
    match = _found_match(home="Nott'm Forest", home_name="Nottingham Forest", home_id="393")
    forest = _team("393", "Nott'm Forest", asked="Nottm Forest", next=[match])
    home = FakeHome({FOOTBALL_TEAMS: _sensor([_team("395", "Stevenage", asked="Stevenage"), forest])}, box="Stevenage, Nott'm Forest")
    assert _football(home).unfollow("Nottingham Forest") == {"success": True, "message": "You've stopped following Nottingham Forest."}
    assert home.written == ["Stevenage"]


def test_an_entry_with_an_apostrophe_which_found_no_team_can_be_cleared():
    # The sensor lists it without the quote, as Home Assistant took it out, and the box still has it.
    home = FakeHome({FOOTBALL_TEAMS: _sensor(_stevenage_and_liverpool(), unknown=["OHiggins"])},
                    box="Stevenage, O'Higgins, Liverpool")
    assert _football(home).unfollow("O'Higgins") == {"success": True, "message": "Taken OHiggins off the list."}
    assert home.written == ["Stevenage, Liverpool"]


def test_the_last_team_can_be_unfollowed():
    home = FakeHome({FOOTBALL_TEAMS: _sensor([_team("395", "Stevenage", asked="Stevenage")])}, box="Stevenage")
    assert _football(home).unfollow("Stevenage")["success"] is True
    assert home.written == [""]


def test_a_team_is_found_by_the_full_name_its_matches_give_it():
    # The sensor's name is ESPN's short one, and the full name is only to be had from the sides of its matches.
    next_match = _found_match(home="Newcastle", home_name="Newcastle United", home_id="361", away="Arsenal", away_id="359")
    teams = [_team("361", "Newcastle", asked="Newcastle", next=[next_match]), _team("359", "Arsenal", asked="Arsenal", next=[next_match])]
    for said in ("Newcastle United", "newcastle", "NewcastleUnited"):
        home = FakeHome({FOOTBALL_TEAMS: _sensor(teams)}, box="Newcastle, Arsenal")
        assert _football(home).unfollow(said) == {"success": True, "message": "You've stopped following Newcastle United."}
        assert home.written == ["Arsenal"]


def test_two_teams_which_fit_the_words_equally_are_not_guessed_between():
    teams = [_team("382", "Manchester City", asked="Man City"), _team("375", "Leicester City", asked="Leicester City")]
    home = FakeHome({FOOTBALL_TEAMS: _sensor(teams)}, box="Man City, Leicester City")
    assert _football(home).unfollow("city") == {
        "success": False, "message": "Which one do you mean, Manchester City or Leicester City?"}
    assert home.written == []
    assert _football(home).unfollow("manchester")["message"] == "You've stopped following Manchester City."
    assert home.written == ["Leicester City"]


def test_football_cant_be_got_so_nothing_is_unfollowed():
    home = FakeHome({FOOTBALL_TEAMS: _sensor(_stevenage_and_liverpool(), age=301)}, box="Stevenage, Liverpool")
    assert _football(home).unfollow("Liverpool") == {"success": False, "message": "I can't get football right now."}
    assert home.written == []


def _bare_comma_box(*entries):
    """The box as it is typed by hand, with bare commas and no spaces - which holds more than ", " would."""
    return ",".join(entries)


def test_taking_an_entry_out_never_writes_more_than_the_box_holds():
    # Written back with ", " the 245 characters of names and six separators which are left would be 257, and Home
    # Assistant would drop that without a word while Nova said the team was gone.
    others = ["A" * 34 + str(number) for number in range(7)]
    box = _bare_comma_box("QPR", *others)
    assert len(box) == BOX_HOLDS
    teams = [_team("334", "QPR", asked="QPR")] + [_team(str(number), name, asked=name) for number, name in enumerate(others)]
    home = FakeHome({FOOTBALL_TEAMS: _sensor(teams)}, box=box)
    assert _football(home).unfollow("QPR") == {"success": True, "message": "You've stopped following QPR."}
    assert home.written == [",".join(others)]
    assert all(len(value) <= BOX_HOLDS for value in home.written)


def test_taking_an_entry_out_keeps_the_usual_spaces_where_they_fit():
    others = ["A" * 34 + str(number) for number in range(6)]
    teams = [_team("334", "QPR", asked="QPR")] + [_team(str(number), name, asked=name) for number, name in enumerate(others)]
    home = FakeHome({FOOTBALL_TEAMS: _sensor(teams)}, box=_bare_comma_box("QPR", *others))
    _football(home).unfollow("QPR")
    assert home.written == [", ".join(others)]


def test_a_value_which_wont_fit_even_so_is_never_written():
    # A box which holds more than 255 characters can't be written back as one that does.
    others = ["A" * 34 + str(number) for number in range(8)]
    teams = [_team("334", "QPR", asked="QPR")] + [_team(str(number), name, asked=name) for number, name in enumerate(others)]
    home = FakeHome({FOOTBALL_TEAMS: _sensor(teams)}, box=_bare_comma_box("QPR", *others))
    assert _football(home).unfollow("QPR") == {"success": False, "message": "Home Assistant's list of teams is full."}
    assert home.written == []


def test_the_wait_ends_by_the_clock_not_by_the_number_of_looks():
    # A slow Home Assistant makes each look take seconds, and the user waits in silence meanwhile: here each
    # pass of the wait moves the clock four seconds, so fifteen seconds are gone after four looks and not fifteen.
    home = _home_with_box(answer=_asked_of("359", "Arsenal"))
    clock = [NOW]

    def sleep(seconds):
        clock[0] += 4

    football = Football(home, dict, now=lambda: clock[0], sleep=sleep)
    assert football.follow("Arsenal") == {
        "success": True, "message": "I've added Arsenal, but Home Assistant hasn't shown it yet."}
    looks = -(-WAIT_FOR_SENSOR // 4)
    assert len(home.sensor_timeouts) == looks == 4
    assert WAIT_FOR_SENSOR <= clock[0] - NOW < WAIT_FOR_SENSOR + 4


def test_each_look_at_the_sensor_is_given_a_short_timeout():
    # So that one slow answer can't use up the whole wait.
    home = _home_with_box(answer=_asked_of("359", "Arsenal"))
    _football(home).follow("Arsenal")
    assert home.sensor_timeouts == [LOOK_TIMEOUT] * WAIT_FOR_SENSOR
    assert 0 < LOOK_TIMEOUT < REQUEST_TIMEOUT
