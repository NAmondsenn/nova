"""Tests for what Nova says as a football match goes on, and for the switches and the record kept for each team and match."""

import json
import logging
import threading
import time
from datetime import datetime, timezone

import pytest
import requests

from voice_assistant import football_watch, storage
from voice_assistant.football import CANT_GET, NOT_SET_UP, FootballUnavailable, Match, Reading, Team
from voice_assistant.football_watch import GAP, KINDS, FootballChoices, FootballWatcher, events, user_home

# An instant well away from any change of clock, so that a test passes in whichever time zone it runs.
NOW = 1_800_000_000.0


def _match(state="pre", phase=None, home_score=None, away_score=None, minutes=None, **more):
    """Notts County (340) at home to Stevenage (285), kicking off minutes from NOW - two hours ago if not given."""
    kickoff = NOW + (minutes if minutes is not None else -120) * 60
    found = dict(
        id="401", kickoff=datetime.fromtimestamp(kickoff, timezone.utc), state=state, phase=phase, clock="",
        home="Notts County", away="Stevenage", home_id="340", away_id="285",
        home_score=home_score, away_score=away_score, league="EFL League One",
        winner=None, shootout=None, extra_time=False, venue=None, odds=None)
    found.update(more)
    return Match(**found)


def _events(before, match, lead=15, followed=("285",)):
    """What events says as (kind, message) pairs, with the record to keep. The lead is in minutes, as it is said."""
    said, record = events(before, match, NOW, lead * 60, set(followed))
    return [(event.kind, event.message) for event in said], record


def test_starting_is_said_once_with_the_minutes_left():
    said, record = _events(None, _match(minutes=15))
    assert said == [("starting", "Stevenage play Notts County in 15 minutes.")]
    assert _events(record, _match(minutes=14))[0] == []


def test_starting_first_seen_late_says_what_is_left():
    assert _events(None, _match(minutes=4.5))[0] == [("starting", "Stevenage play Notts County in 5 minutes.")]
    assert _events(None, _match(minutes=0.5))[0] == [("starting", "Stevenage play Notts County in 1 minute.")]


def test_nothing_is_said_too_early_or_for_a_match_which_is_off():
    assert _events(None, _match(minutes=16))[0] == []
    assert _events(None, _match(minutes=10, phase="off"))[0] == []
    assert _events(None, _match(minutes=10, clock="Delayed"))[0] == []


def test_starting_isnt_said_for_a_match_whose_kickoff_has_passed():
    assert _events(None, _match(minutes=-1))[0] == []
    assert _events(None, _match(minutes=0))[0] == []


def test_starting_is_said_even_after_a_gap_in_looking():
    _, seen = _events(None, _match(minutes=40))
    assert _events({**seen, "at": NOW - 3600}, _match(minutes=10))[0] == [
        ("starting", "Stevenage play Notts County in 10 minutes.")]


def test_the_followed_team_is_named_first():
    assert _events(None, _match(minutes=15), followed=("340",))[0] == [("starting", "Notts County play Stevenage in 15 minutes.")]
    assert _events(None, _match(minutes=15), followed=("340", "285"))[0] == [("starting", "Notts County play Stevenage in 15 minutes.")]


def test_goals():
    _, seen = _events(None, _match("in", "first_half", 0, 0))
    said, seen = _events(seen, _match("in", "first_half", 0, 1))
    assert said == [("goals", "Goal for Stevenage. Notts County 0, Stevenage 1.")]
    said, seen = _events(seen, _match("in", "first_half", 1, 1))
    assert said == [("goals", "Goal for Notts County. Notts County 1, Stevenage 1.")]
    said, seen = _events(seen, _match("in", "first_half", 2, 2))
    assert said == [("goals", "Goals for both sides. Notts County 2, Stevenage 2.")]
    # A score which goes down may be an old copy, so it is said only when the next look shows it again.
    said, seen = _events(seen, _match("in", "first_half", 2, 1))
    assert said == []
    said, seen = _events(seen, _match("in", "first_half", 2, 1))
    assert said == [("goals", "Goal ruled out. Notts County 2, Stevenage 1.")]


def test_a_goal_in_the_same_look_as_one_ruled_out_is_said_as_the_goal():
    _, seen = _events(None, _match("in", "second_half", 1, 0))
    assert _events(seen, _match("in", "second_half", 0, 1))[0] == [("goals", "Goal for Stevenage. Notts County 0, Stevenage 1.")]


def test_half_time_and_the_second_half():
    _, seen = _events(None, _match("in", "first_half", 0, 1))
    said, seen = _events(seen, _match("in", "half_time", 0, 1))
    assert said == [("half_time", "Half time. Notts County 0, Stevenage 1.")]
    said, seen = _events(seen, _match("in", "second_half", 0, 1))
    assert said == [("second_half", "The second half has started. Notts County 0, Stevenage 1.")]
    assert _events(seen, _match("in", "second_half", 0, 1))[0] == []


def test_a_missed_half_time_still_gets_the_second_half():
    _, seen = _events(None, _match("in", "first_half", 0, 0))
    assert _events(seen, _match("in", "second_half", 0, 0))[0] == [
        ("second_half", "The second half has started. Notts County 0, Stevenage 0.")]


def test_full_time():
    _, seen = _events(None, _match("in", "second_half", 1, 2))
    assert _events(seen, _match("post", "full_time", 1, 2, winner="away"))[0] == [("full_time", "Full time. Notts County 1, Stevenage 2.")]
    assert _events(seen, _match("post", "full_time", 1, 2, winner="away", extra_time=True))[0] == [
        ("full_time", "Full time, after extra time. Notts County 1, Stevenage 2.")]
    assert _events(seen, _match("post", "full_time", 2, 2, winner="away", shootout=(3, 4)))[0] == [
        ("goals", "Goal for Notts County. Notts County 2, Stevenage 2."),
        ("full_time", "Full time. Notts County 2, Stevenage 2. Stevenage won 4-3 on penalties.")]


def test_a_shootout_is_won_by_the_side_home_assistant_names_with_its_own_score_first():
    _, seen = _events(None, _match("in", "penalties", 1, 1))
    assert _events(seen, _match("post", "full_time", 1, 1, winner="home", shootout=(5, 4), extra_time=True))[0] == [
        ("full_time", "Full time, after extra time. Notts County 1, Stevenage 1. Notts County won 5-4 on penalties.")]
    assert _events(seen, _match("post", "full_time", 1, 1, shootout=(5, 4)))[0] == [
        ("full_time", "Full time. Notts County 1, Stevenage 1. The penalty shootout finished Notts County 5, Stevenage 4.")]


def test_full_time_is_said_once():
    _, seen = _events(None, _match("in", "second_half", 1, 2))
    _, seen = _events(seen, _match("post", "full_time", 1, 2, winner="away"))
    assert _events(seen, _match("post", "full_time", 1, 2, winner="away"))[0] == []
    # Even if the score is corrected afterwards: it is over, and the next look is of a match which was.
    assert _events(seen, _match("post", "full_time", 1, 3, winner="away"))[0] == []


def test_a_match_first_seen_in_play_is_taken_as_it_stands():
    said, seen = _events(None, _match("in", "second_half", 2, 1))
    assert said == []
    assert _events(seen, _match("in", "second_half", 2, 2))[0] == [("goals", "Goal for Stevenage. Notts County 2, Stevenage 2.")]


def test_nothing_is_made_up_for_after_a_gap():
    _, seen = _events(None, _match("in", "first_half", 0, 0))
    said, seen = _events({**seen, "at": NOW - 301}, _match("post", "full_time", 0, 2, winner="away"))
    assert said == [] and seen["state"] == "post" and seen["away"] == 2


def test_a_look_exactly_the_gap_after_the_last_still_compares():
    _, seen = _events(None, _match("in", "first_half", 0, 0))
    said, _ = _events({**seen, "at": NOW - GAP}, _match("in", "first_half", 0, 1))
    assert said == [("goals", "Goal for Stevenage. Notts County 0, Stevenage 1.")]


def test_a_goal_straight_after_the_warning():
    _, seen = _events(None, _match(minutes=1))
    assert _events(seen, _match("in", "first_half", 0, 1))[0] == [("goals", "Goal for Stevenage. Notts County 0, Stevenage 1.")]
    assert _events(seen, _match("in", "first_half", 0, 0))[0] == []


def test_a_match_called_off_is_never_full_time():
    _, seen = _events(None, _match("in", "first_half", 0, 0))
    assert _events(seen, _match("post", "off", 0, 0))[0] == []


def test_no_phase_from_an_older_home_assistant():
    _, seen = _events(None, _match("in", None, 0, 0))
    said, seen = _events(seen, _match("in", None, 0, 1))
    assert said == [("goals", "Goal for Stevenage. Notts County 0, Stevenage 1.")]
    assert _events(seen, _match("post", None, 0, 1))[0] == []


def test_a_phase_home_assistant_has_added_since_counts_as_none():
    said, seen = _events(None, _match("in", "stoppage_time", 0, 0))
    assert said == [] and seen["phase"] is None
    assert _events(seen, _match("in", "stoppage_time", 0, 1))[0] == [("goals", "Goal for Stevenage. Notts County 0, Stevenage 1.")]
    # Counted as none, a match about to start with one is still about to start.
    assert _events(None, _match(minutes=10, phase="stoppage_time"))[0] == [("starting", "Stevenage play Notts County in 10 minutes.")]


def test_the_record_is_the_match_as_it_is_now():
    _, record = _events(None, _match("in", "second_half", 1, 2, minutes=-60))
    assert record == {"home": 1, "away": 2, "waiting": None, "phase": "second_half", "state": "in", "starting": False,
                      "at": NOW, "kickoff": NOW - 3600}
    assert _events(None, _match(minutes=10))[1] == {
        "home": None, "away": None, "waiting": None, "phase": None, "state": "pre", "starting": True,
        "at": NOW, "kickoff": NOW + 600}


def test_a_goal_which_flickers_away_and_back_is_said_once():
    _, seen = _events(None, _match("in", "first_half", 0, 0))
    said, seen = _events(seen, _match("in", "first_half", 0, 1))
    assert said == [("goals", "Goal for Stevenage. Notts County 0, Stevenage 1.")]
    # One look at the score as it was is an old copy: not a goal ruled out, and not the goal again when it is back.
    said, seen = _events(seen, _match("in", "first_half", 0, 0))
    assert said == []
    assert _events(seen, _match("in", "first_half", 0, 1))[0] == []


def test_a_goal_ruled_out_is_said_once_it_is_seen_on_two_looks_running():
    _, seen = _events(None, _match("in", "first_half", 2, 2))
    said, seen = _events(seen, _match("in", "first_half", 2, 1))
    assert said == []
    # The higher score is kept meanwhile, and the lower one is noted as waiting.
    assert (seen["home"], seen["away"], seen["waiting"]) == (2, 2, {"home": 2, "away": 1})
    said, seen = _events(seen, _match("in", "first_half", 2, 1))
    assert said == [("goals", "Goal ruled out. Notts County 2, Stevenage 1.")]
    assert (seen["home"], seen["away"], seen["waiting"]) == (2, 1, None)
    assert _events(seen, _match("in", "first_half", 2, 1))[0] == []


def test_one_lower_look_and_then_the_old_score_again_says_nothing():
    _, seen = _events(None, _match("in", "first_half", 2, 2))
    _, seen = _events(seen, _match("in", "first_half", 2, 1))
    said, seen = _events(seen, _match("in", "first_half", 2, 2))
    assert said == [] and seen["waiting"] is None
    assert _events(seen, _match("in", "first_half", 2, 2))[0] == []


def test_a_lower_score_waiting_is_dropped_when_the_next_look_is_something_else():
    _, seen = _events(None, _match("in", "first_half", 2, 2))
    _, seen = _events(seen, _match("in", "first_half", 2, 1))
    # Another lower score waits in its turn, and is said only when seen again.
    said, seen = _events(seen, _match("in", "first_half", 1, 1))
    assert said == [] and seen["waiting"] == {"home": 1, "away": 1}
    # A goal is compared with the score kept, not with the one which waited.
    said, seen = _events(seen, _match("in", "first_half", 3, 2))
    assert said == [("goals", "Goal for Notts County. Notts County 3, Stevenage 2.")]
    assert seen["waiting"] is None


def test_an_older_pre_look_in_the_middle_of_a_match_is_an_old_copy():
    _, seen = _events(None, _match("in", "first_half", 1, 0))
    # Scores not given and, once, a kick-off in ten minutes: neither "Goal ruled out" nor "starting".
    for old_copy in (_match(), _match(minutes=10)):
        said, seen = _events(seen, old_copy)
        assert said == [] and seen["state"] == "in" and (seen["home"], seen["away"]) == (1, 0)
    assert _events(seen, _match("in", "first_half", 1, 0))[0] == []
    assert _events(seen, _match("in", "first_half", 2, 0))[0] == [("goals", "Goal for Notts County. Notts County 2, Stevenage 0.")]


def test_a_look_without_the_scores_the_record_has_is_an_old_copy():
    _, seen = _events(None, _match("in", "second_half", 1, 2))
    assert _events(seen, _match("in", "second_half"))[0] == []
    assert _events(seen, _match("in", "second_half", 1, None))[0] == []


def test_an_old_copy_changes_nothing_in_the_record_but_when_it_was_made():
    _, seen = _events(None, _match("in", "second_half", 1, 2, minutes=-60))
    old_copy = _match("in", "first_half", 0, 0, minutes=-60)
    said, record = events(seen, old_copy, NOW + 30, 900, {"285"})
    assert said == [] and record == {**seen, "at": NOW + 30}


def test_after_a_gap_an_older_look_is_the_match_as_it_is_not_an_old_copy():
    _, seen = _events(None, _match("in", "second_half", 1, 2))
    said, seen = _events({**seen, "at": NOW - 301}, _match())
    assert said == [] and seen["state"] == "pre" and seen["home"] is None


def test_a_phase_which_flickers_is_said_once():
    _, seen = _events(None, _match("in", "second_half", 0, 1))
    said, seen = _events(seen, _match("in", "half_time", 0, 1))
    assert said == [] and seen["phase"] == "second_half"
    assert _events(seen, _match("in", "second_half", 0, 1))[0] == []

    _, seen = _events(None, _match("in", "first_half", 0, 1))
    said, seen = _events(seen, _match("in", "half_time", 0, 1))
    assert said == [("half_time", "Half time. Notts County 0, Stevenage 1.")]
    said, seen = _events(seen, _match("in", "first_half", 0, 1))
    assert said == []
    assert _events(seen, _match("in", "half_time", 0, 1))[0] == []


def test_a_look_with_no_phase_known_never_takes_the_phase_away():
    _, seen = _events(None, _match("in", "second_half", 0, 1))
    # The state and the scores move on as usual; the phase stays.
    said, seen = _events(seen, _match("in", "stoppage_time", 0, 2))
    assert said == [("goals", "Goal for Stevenage. Notts County 0, Stevenage 2.")]
    assert seen["phase"] == "second_half" and seen["away"] == 2
    assert _events(seen, _match("in", "second_half", 0, 2))[0] == []
    said, seen = _events(seen, _match("in", None, 0, 2))
    assert said == [] and seen["phase"] == "second_half"
    assert _events(seen, _match("in", "second_half", 0, 2))[0] == []


def test_a_full_time_which_flickers_is_said_once():
    _, seen = _events(None, _match("in", "second_half", 1, 2))
    said, seen = _events(seen, _match("post", "full_time", 1, 2, winner="away"))
    assert said == [("full_time", "Full time. Notts County 1, Stevenage 2.")]
    said, seen = _events(seen, _match("in", "second_half", 1, 2))
    assert said == [] and seen["state"] == "post" and seen["phase"] == "full_time"
    assert _events(seen, _match("post", "full_time", 1, 2, winner="away"))[0] == []


def test_a_match_which_is_off_says_nothing_whatever_its_score_does():
    _, seen = _events(None, _match("in", "first_half", 1, 0))
    said, off = _events(seen, _match("post", "off", 0, 0))
    assert said == []
    assert (off["state"], off["phase"], off["home"], off["away"]) == ("post", "off", 0, 0)
    assert _events(seen, _match("post", "off", 2, 0))[0] == []
    # Scores Home Assistant can no longer read arrive as none, which isn't 0-0.
    assert _events(seen, _match("post", "off"))[0] == []


def test_a_match_which_comes_back_from_being_off_is_taken_as_it_stands():
    _, seen = _events(None, _match("in", "first_half", 1, 0))
    _, seen = _events(seen, _match("post", "off", 1, 0))
    said, seen = _events(seen, _match("in", "first_half", 1, 0))
    assert said == [] and seen["state"] == "in"
    assert _events(seen, _match("in", "first_half", 1, 1))[0] == [("goals", "Goal for Stevenage. Notts County 1, Stevenage 1.")]


def test_a_match_which_is_off_in_play_and_then_back_is_taken_as_it_stands():
    # Off and still "in", with no scores: the goal is not 0-0 to 1-0 when the match is back, whatever its state was.
    _, seen = _events(None, _match("in", "first_half", 1, 0))
    said, seen = _events(seen, _match("in", "off"))
    assert said == [] and seen["phase"] == "off"
    said, seen = _events(seen, _match("in", "first_half", 1, 0))
    assert said == [] and (seen["home"], seen["away"], seen["phase"]) == (1, 0, "first_half")
    assert _events(seen, _match("in", "first_half", 1, 1))[0] == [("goals", "Goal for Stevenage. Notts County 1, Stevenage 1.")]


def test_a_look_with_no_phase_known_keeps_the_phase_of_a_record_however_old():
    _, seen = _events(None, _match("in", "second_half", 0, 1))
    said, seen = _events({**seen, "at": NOW - GAP - 1}, _match("in", "stoppage_time", 0, 1))
    assert said == [] and seen["phase"] == "second_half"
    # So the second half isn't said to have started when Home Assistant knows the phase again.
    assert _events(seen, _match("in", "second_half", 0, 1))[0] == []


def test_events_works_from_the_time_it_is_given():
    said, _ = events(None, _match(minutes=15), NOW, 900, {"285"})
    assert [event.kind for event in said] == ["starting"]
    # A day earlier by the time given, the same match is a day and 15 minutes away.
    assert events(None, _match(minutes=15), NOW - 86400, 900, {"285"})[0] == []


def test_switches_start_on_and_are_kept(tmp_path):
    choices = FootballChoices(str(tmp_path / "football.json"))
    assert choices.switches("364") == dict.fromkeys(KINDS, True)
    choices.set("364", "Liverpool", "goals", False)
    again = FootballChoices(str(tmp_path / "football.json"))
    assert again.switches("364") == {**dict.fromkeys(KINDS, True), "goals": False}
    assert again.enabled("285", "goals") and not again.enabled("364", "goals")
    with pytest.raises(ValueError):
        again.set("364", "Liverpool", "corners", True)


def test_a_switch_can_be_put_back_on_and_the_team_is_named_in_the_file(tmp_path):
    path = tmp_path / "football.json"
    choices = FootballChoices(str(path))
    choices.set("364", "Liverpool", "goals", False)
    choices.set("364", "Liverpool", "goals", True)
    assert choices.switches("364") == dict.fromkeys(KINDS, True)
    assert json.loads(path.read_text())["teams"]["364"]["name"] == "Liverpool"


def test_an_unknown_kind_is_refused_when_asked_about_too(tmp_path):
    choices = FootballChoices(str(tmp_path / "football.json"))
    with pytest.raises(ValueError):
        choices.enabled("364", "corners")


def test_a_failed_save_of_a_switch_changes_nothing(tmp_path, monkeypatch):
    choices = FootballChoices(str(tmp_path / "football.json"))

    def fail(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "save_json", fail)
    with pytest.raises(OSError):
        choices.set("364", "Liverpool", "goals", False)
    assert choices.enabled("364", "goals")


def test_a_switch_which_isnt_on_or_off_in_the_file_counts_as_on(tmp_path):
    path = tmp_path / "football.json"
    path.write_text(json.dumps({"teams": {"364": {"name": "Liverpool", "switches": {"goals": "no", "full_time": 0}},
                                          "285": {"switches": "off"}, "340": 7}}))
    choices = FootballChoices(str(path))
    assert all(choices.switches(team) == dict.fromkeys(KINDS, True) for team in ("364", "285", "340"))


def test_what_was_seen_is_kept_and_old_matches_are_dropped(tmp_path, monkeypatch):
    path = str(tmp_path / "football.json")
    choices = FootballChoices(path)
    assert choices.seen("1") is None
    _, record = _events(None, _match("in", "first_half", 0, 1))
    choices.remember("1", record)
    assert FootballChoices(path).seen("1") == record
    # The one who looks is handed a copy, so changing it changes nothing kept.
    choices.seen("1")["home"] = 9
    assert choices.seen("1") == record

    choices.forget_before(record["kickoff"])
    assert choices.seen("1") == record
    choices.forget_before(record["kickoff"] + 1)
    assert choices.seen("1") is None
    assert FootballChoices(path).seen("1") is None

    def fail(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "save_json", fail)
    choices.remember("2", record)
    assert choices.seen("2") == record


def test_a_caller_changing_a_record_it_gave_or_was_given_changes_nothing_kept(tmp_path):
    choices = FootballChoices(str(tmp_path / "football.json"))
    _, record = _events(None, _match("in", "first_half", 2, 2))
    _, record = _events(record, _match("in", "first_half", 2, 1))
    waiting = {"home": 2, "away": 1}
    assert record["waiting"] == waiting
    choices.remember("1", record)
    record["waiting"]["home"] = 9
    assert choices.seen("1")["waiting"] == waiting
    choices.seen("1")["waiting"]["away"] = 9
    assert choices.seen("1")["waiting"] == waiting


def _counting_saves(monkeypatch, fail_first=0):
    """Counts the saves of football.json, failing the first fail_first of them; returns the list of paths saved."""
    saved = []
    real = storage.save_json

    def save(path, data, indent=None):
        if len(saved) < fail_first:
            saved.append(None)
            raise OSError("disk full")
        saved.append(path)
        real(path, data, indent)

    monkeypatch.setattr(storage, "save_json", save)
    return saved


def test_a_record_which_differs_only_in_when_it_was_made_isnt_saved(tmp_path, monkeypatch):
    path = str(tmp_path / "football.json")
    choices = FootballChoices(path)
    _, first = _events(None, _match("in", "first_half", 0, 1))
    second = {**first, "at": first["at"] + 30}
    saved = _counting_saves(monkeypatch)
    choices.remember("1", first)
    choices.remember("1", second)
    assert len(saved) == 1
    # What is asked for is the newest, and what a restart finds is what was saved.
    assert choices.seen("1") == second
    assert FootballChoices(path).seen("1") == first
    # Nor is one which differs only so from what the file held at the start.
    again = FootballChoices(path)
    again.remember("1", {**first, "at": first["at"] + 60})
    assert len(saved) == 1 and again.seen("1")["at"] == first["at"] + 60


def test_a_record_which_differs_in_anything_else_is_saved(tmp_path, monkeypatch):
    path = str(tmp_path / "football.json")
    choices = FootballChoices(path)
    _, first = _events(None, _match("in", "first_half", 0, 0))
    _, goal = _events(first, _match("in", "first_half", 0, 1))
    saved = _counting_saves(monkeypatch)
    choices.remember("1", first)
    choices.remember("1", goal)
    assert len(saved) == 2
    assert FootballChoices(path).seen("1") == goal


def test_a_save_which_failed_is_tried_again_at_the_next_look(tmp_path, monkeypatch):
    path = str(tmp_path / "football.json")
    choices = FootballChoices(path)
    _, first = _events(None, _match("in", "first_half", 0, 1))
    second = {**first, "at": first["at"] + 30}
    saved = _counting_saves(monkeypatch, fail_first=1)
    choices.remember("1", first)
    assert choices.seen("1") == first
    choices.remember("1", second)
    assert len(saved) == 2
    assert FootballChoices(path).seen("1") == second


def test_forgetting_a_match_forgets_the_newest_record_of_it_too(tmp_path):
    path = str(tmp_path / "football.json")
    choices = FootballChoices(path)
    _, first = _events(None, _match("in", "first_half", 0, 1))
    choices.remember("1", first)
    choices.remember("1", {**first, "at": first["at"] + 30})
    choices.forget_before(first["kickoff"] + 1)
    assert choices.seen("1") is None


def test_forgetting_old_matches_leaves_the_switches(tmp_path):
    path = str(tmp_path / "football.json")
    choices = FootballChoices(path)
    choices.set("364", "Liverpool", "goals", False)
    _, record = _events(None, _match("in", "first_half", 0, 1))
    choices.remember("1", record)
    choices.forget_before(record["kickoff"] + 1)
    assert not FootballChoices(path).enabled("364", "goals")


def test_a_match_record_the_file_holds_wrongly_is_taken_as_never_seen_and_forgotten(tmp_path):
    path = tmp_path / "football.json"
    good = _events(None, _match("in", "first_half", 0, 1))[1]
    path.write_text(json.dumps({"seen": {
        "1": {"state": "in"}, "2": "in", "3": {**good, "home": "0"}, "4": {**good, "state": "live"},
        "6": {**good, "waiting": {"home": "1", "away": 0}}, "5": good}}))
    choices = FootballChoices(str(path))
    assert [choices.seen(match_id) for match_id in "12346"] == [None] * 5
    assert choices.seen("5") == good
    choices.forget_before(good["kickoff"])
    assert list(json.loads(path.read_text())["seen"]) == ["5"]


def _team(team_id, name, now=None, next=(), stale=False):
    """A followed team as Football.read() gives it: asked nothing, and no results."""
    return Team(id=team_id, name=name, asked=None, stale=stale, now=now, next=tuple(next), last=())


def _liverpool(goals=0):
    """Liverpool (364) at home to Arsenal (359), in play: a match of another team than Stevenage's."""
    return _match("in", "first_half", goals, 0, id="402", home="Liverpool", away="Arsenal", home_id="364", away_id="359")


def _unavailable(sentence=CANT_GET, why=None):
    """What Football.read() raises when it can't be got: the sentence to say, and why where there is a reason."""
    error = FootballUnavailable(sentence)
    error.__cause__ = why
    return error


class FakeFootball:
    """Gives read() what a test sets - a reading, or an error to raise - and counts the reads."""

    def __init__(self):
        self.reading = Reading(teams=(), unknown=(), trimmed=False)
        self.reads = 0

    def read(self):
        self.reads += 1
        if isinstance(self.reading, Exception):
            raise self.reading
        return self.reading


class Rig:
    """A watcher with a fake football, and recording fakes for saying, quiet hours and whether the user is out."""

    def __init__(self, tmp_path, quiet=False, at_home=True, can_say=True):
        self.said = []
        self.quiet, self.at_home, self.can_say = quiet, at_home, can_say
        self.asked_home = 0
        self.settings = {}
        self.football = FakeFootball()
        self.choices = FootballChoices(str(tmp_path / "football.json"))
        self.watcher = FootballWatcher(
            self.football, self.choices, self._say, lambda: self.quiet, self._home_now, lambda: self.settings)

    def _say(self, message):
        if self.can_say:
            self.said.append(message)
        return self.can_say

    def _home_now(self):
        self.asked_home += 1
        return self.at_home

    def look(self, *teams, at=NOW):
        self.football.reading = Reading(teams=teams, unknown=(), trimmed=False)
        self.watcher.check(at)


GOAL = "Goal for Stevenage. Notts County 0, Stevenage 1."


def _scored(tmp_path, **rig):
    rig = Rig(tmp_path, **rig)
    rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 0)))
    rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 1)), at=NOW + 30)
    return rig


def test_a_goal_is_said(tmp_path):
    assert _scored(tmp_path).said == [GOAL]


def test_not_at_night_and_not_said_in_the_morning(tmp_path):
    rig = _scored(tmp_path, quiet=True)
    rig.quiet = False
    rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 1)), at=NOW + 60)
    assert rig.said == []


def test_not_when_the_user_is_out_but_said_when_home_assistant_cant_say(tmp_path):
    assert _scored(tmp_path, at_home=False).said == []
    assert _scored(tmp_path / "b", at_home=None).said == [GOAL]


def test_a_switch_turned_off(tmp_path):
    rig = Rig(tmp_path)
    rig.choices.set("285", "Stevenage", "goals", False)
    rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 0)))
    rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 1)), at=NOW + 30)
    assert rig.said == []
    rig.look(_team("285", "Stevenage", now=_match("in", "half_time", 0, 1)), at=NOW + 60)
    assert rig.said == ["Half time. Notts County 0, Stevenage 1."]


def test_one_that_cant_be_said_is_dropped(tmp_path):
    rig = _scored(tmp_path, can_say=False)
    assert rig.said == []
    rig.can_say = True
    rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 1)), at=NOW + 60)
    assert rig.said == []
    rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 2)), at=NOW + 90)
    assert rig.said == ["Goal for Stevenage. Notts County 0, Stevenage 2."]


def _both_follow(tmp_path, off=()):
    """Stevenage and Notts County, who play each other, both followed; the goal is Stevenage's, with goals off for the teams in off."""
    rig = Rig(tmp_path)
    for team_id, name in off:
        rig.choices.set(team_id, name, "goals", False)

    def teams(away_score):
        match = _match("in", "first_half", 0, away_score)
        return _team("285", "Stevenage", now=match), _team("340", "Notts County", now=match)

    rig.look(*teams(0))
    rig.look(*teams(1), at=NOW + 30)
    return rig


def test_two_followed_teams_in_one_match(tmp_path):
    assert _both_follow(tmp_path / "both").said == [GOAL]
    # Said once if either has it on: the first team's switch being off doesn't lose it for the second.
    assert _both_follow(tmp_path / "stevenage", off=[("285", "Stevenage")]).said == [GOAL]
    assert _both_follow(tmp_path / "notts", off=[("340", "Notts County")]).said == [GOAL]
    assert _both_follow(tmp_path / "neither", off=[("285", "Stevenage"), ("340", "Notts County")]).said == []


def test_a_match_is_looked_at_once_however_many_followed_teams_it_is_in(tmp_path, monkeypatch):
    calls = []
    real = football_watch.events

    def counting(before, match, now, lead, followed):
        calls.append((match.id, followed))
        return real(before, match, now, lead, followed)

    monkeypatch.setattr(football_watch, "events", counting)
    rig = Rig(tmp_path)
    match = _match("in", "first_half", 0, 0)
    # Each one's "now"; then one's "now" and the other's "next"; neither is looked at twice.
    rig.look(_team("285", "Stevenage", now=match), _team("340", "Notts County", now=match))
    rig.look(_team("285", "Stevenage", now=match), _team("340", "Notts County", next=(match,)), at=NOW + 30)
    assert calls == [("401", {"285", "340"}), ("401", {"285", "340"})]
    # Said once: the match is the "next" of the one and the "now" of the other.
    goal = _match("in", "first_half", 0, 1)
    rig.look(_team("285", "Stevenage", now=goal), _team("340", "Notts County", next=(goal,)), at=NOW + 60)
    assert rig.said == [GOAL]


def test_every_match_of_every_team_is_looked_at_in_the_same_look(tmp_path):
    rig = Rig(tmp_path)
    rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 0)), _team("364", "Liverpool", now=_liverpool(0)))
    rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 1)), _team("364", "Liverpool", now=_liverpool(1)), at=NOW + 30)
    assert rig.said == [GOAL, "Goal for Liverpool. Liverpool 1, Arsenal 0."]


def test_a_team_with_a_match_in_play_and_another_to_come_is_watched_for_both(tmp_path):
    rig = Rig(tmp_path)
    later = _match(minutes=10, id="403")
    rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 0), next=(later, _match(minutes=5, id="404"))))
    # Only the next one, not the one after it.
    assert rig.said == ["Stevenage play Notts County in 10 minutes."]


def test_a_stale_team_is_passed_over_and_the_others_carry_on(tmp_path):
    rig = Rig(tmp_path)

    def stevenage(goals, stale=False):
        return _team("285", "Stevenage", now=_match("in", "first_half", 0, goals), stale=stale)

    rig.look(stevenage(0), _team("364", "Liverpool", now=_liverpool(0)))
    rig.look(stevenage(1, stale=True), _team("364", "Liverpool", now=_liverpool(1)), at=NOW + 30)
    assert rig.said == ["Goal for Liverpool. Liverpool 1, Arsenal 0."]
    # Passed over entirely: its record is as it was, so the goal shown within five minutes of the last good look is said.
    assert rig.choices.seen("401")["at"] == NOW
    rig.look(stevenage(1), _team("364", "Liverpool", now=_liverpool(1)), at=NOW + 60)
    assert rig.said == ["Goal for Liverpool. Liverpool 1, Arsenal 0.", GOAL]


def test_a_team_stale_for_longer_than_the_gap_says_nothing_of_what_it_missed(tmp_path):
    rig = Rig(tmp_path)
    rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 0)))
    for later in (30, 150, 270):
        rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 1), stale=True), at=NOW + later)
    rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 1)), at=NOW + GAP + 60)
    assert rig.said == []
    rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 2)), at=NOW + GAP + 90)
    assert rig.said == ["Goal for Stevenage. Notts County 0, Stevenage 2."]


def test_football_down_is_logged_once(tmp_path, caplog):
    rig = Rig(tmp_path)
    rig.football.reading = _unavailable(CANT_GET, KeyError("teams"))
    with caplog.at_level(logging.INFO):
        rig.watcher.check(NOW)
        rig.watcher.check(NOW + 30)
        warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
        assert rig.said == [] and len(warnings) == 1
        assert CANT_GET in warnings[0] and "KeyError('teams')" in warnings[0]
        rig.look(_team("285", "Stevenage"), at=NOW + 60)
        rig.look(_team("285", "Stevenage"), at=NOW + 90)
        assert len([record for record in caplog.records if "back" in record.getMessage()]) == 1
        # Down again is down afresh, and where there is no cause the sentence stands alone.
        rig.football.reading = _unavailable(NOT_SET_UP)
        rig.watcher.check(NOW + 120)
        warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 2 and NOT_SET_UP in warnings[1] and "None" not in warnings[1]


def test_nothing_is_looked_at_while_football_is_down(tmp_path):
    rig = _scored(tmp_path)
    rig.football.reading = _unavailable()
    rig.watcher.check(NOW + 2 * 86400)
    # Not even forgetting: that is for a look at the matches.
    assert rig.choices.seen("401") is not None


def test_a_restart_says_nothing_twice(tmp_path):
    rig = Rig(tmp_path)
    rig.look(_team("285", "Stevenage", now=_match(minutes=10)))
    assert rig.said == ["Stevenage play Notts County in 10 minutes."]
    again = Rig(tmp_path)
    again.look(_team("285", "Stevenage", now=_match(minutes=10)), at=NOW + 60)
    assert again.said == []
    again.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 1)), at=NOW + 90)
    assert again.said == [GOAL]


def test_a_match_tomorrow_night_is_warned_of_from_next(tmp_path):
    rig = Rig(tmp_path)
    rig.look(_team("285", "Stevenage", next=(_match(minutes=10),)))
    assert rig.said == ["Stevenage play Notts County in 10 minutes."]


def test_the_lead_is_read_each_time(tmp_path):
    rig = Rig(tmp_path)
    rig.settings["kickoff_minutes"] = 30
    rig.look(_team("285", "Stevenage", next=(_match(minutes=25),)))
    assert rig.said == ["Stevenage play Notts County in 25 minutes."]
    rig.settings["kickoff_minutes"] = 15
    # The new match is 24 and a half minutes off at this look.
    rig.look(_team("285", "Stevenage", next=(_match(minutes=25, id="402"),)), at=NOW + 30)
    assert len(rig.said) == 1


@pytest.mark.parametrize("bad", ["soon", None, True, float("nan"), float("inf"), 0, -15, [15]])
def test_a_lead_which_isnt_a_number_falls_back_on_the_default(tmp_path, caplog, bad):
    rig = Rig(tmp_path)
    rig.settings["kickoff_minutes"] = bad
    with caplog.at_level(logging.WARNING):
        rig.look(_team("285", "Stevenage", next=(_match(minutes=10),)))
        rig.look(_team("285", "Stevenage", next=(_match(minutes=10),)), at=NOW + 30)
    assert rig.said == ["Stevenage play Notts County in 10 minutes."]
    # Once, not at every look.
    assert len([record for record in caplog.records if "kickoff_minutes" in record.getMessage()]) == 1


@pytest.mark.parametrize("bad", ["soon", None, True, float("nan"), float("inf"), 0, -30, [30]])
def test_a_check_every_which_isnt_a_number_neither_ends_the_thread_nor_busies_it(tmp_path, caplog, bad):
    rig = Rig(tmp_path)
    rig.settings["check_every"] = bad
    with caplog.at_level(logging.WARNING):
        rig.watcher.start()
        deadline = time.time() + 5
        while "check_every" not in caplog.text and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)
        thread = rig.watcher._thread
        alive, reads = thread.is_alive(), rig.football.reads
        rig.watcher.stop()
    assert "check_every" in caplog.text
    # The default of 30 seconds is waited, so it looked once and is waiting.
    assert alive and reads == 1
    assert not thread.is_alive()


def test_old_matches_are_forgotten(tmp_path):
    rig = Rig(tmp_path)
    rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 0, minutes=0)))
    assert rig.choices.seen("401") is not None
    # A match is kept for a day after it kicks off.
    rig.look(at=NOW + 86400)
    assert rig.choices.seen("401") is not None
    rig.look(at=NOW + 86401)
    assert rig.choices.seen("401") is None


def test_what_isnt_said_is_logged_with_why(tmp_path, caplog):
    cases = {
        "quiet": ({"quiet": True}, "quiet hours"),
        "out": ({"at_home": False}, "the user is out"),
        "away": ({"can_say": False}, "couldn't be said"),
    }
    for name, (rig_args, why) in cases.items():
        caplog.clear()
        with caplog.at_level(logging.INFO):
            _scored(tmp_path / name, **rig_args)
        dropped = [record.getMessage() for record in caplog.records if GOAL in record.getMessage()]
        assert len(dropped) == 1 and why in dropped[0], name
    caplog.clear()
    rig = Rig(tmp_path / "off")
    rig.choices.set("285", "Stevenage", "goals", False)
    with caplog.at_level(logging.INFO):
        rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 0)))
        rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 1)), at=NOW + 30)
    dropped = [record.getMessage() for record in caplog.records if GOAL in record.getMessage()]
    assert len(dropped) == 1 and "switched off" in dropped[0]


def test_home_assistant_is_asked_where_the_user_is_only_when_something_is_to_be_said_and_once_a_look(tmp_path):
    rig = Rig(tmp_path)
    rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 0)))
    rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 0)), at=NOW + 30)
    assert rig.asked_home == 0
    # A goal and half time in one look.
    rig.look(_team("285", "Stevenage", now=_match("in", "half_time", 0, 1)), at=NOW + 60)
    assert len(rig.said) == 2 and rig.asked_home == 1
    # Nor when it is night: nothing to ask.
    rig.quiet = True
    rig.look(_team("285", "Stevenage", now=_match("in", "second_half", 0, 1)), at=NOW + 90)
    assert len(rig.said) == 2 and rig.asked_home == 1


def test_a_bug_with_one_match_doesnt_lose_the_others(tmp_path, caplog):
    rig = Rig(tmp_path)
    real = rig.choices.remember

    def flaky(match_id, record):
        if match_id == "401":
            raise TypeError("boom")
        real(match_id, record)

    rig.choices.remember = flaky
    rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 0)), _team("364", "Liverpool", now=_liverpool(0)))
    with caplog.at_level(logging.WARNING):
        rig.look(_team("285", "Stevenage", now=_match("in", "first_half", 0, 1)), _team("364", "Liverpool", now=_liverpool(1)), at=NOW + 30)
    assert rig.said == ["Goal for Liverpool. Liverpool 1, Arsenal 0."]
    assert "boom" in caplog.text


def test_the_thread_starts_and_stops(tmp_path):
    rig = Rig(tmp_path)
    assert rig.watcher._thread is None
    rig.settings["check_every"] = 0.01
    rig.watcher.start()
    deadline = time.time() + 5
    while rig.football.reads < 3 and time.time() < deadline:
        time.sleep(0.01)
    thread = rig.watcher._thread
    assert rig.football.reads >= 3
    assert thread.name == "football" and thread.daemon and thread.is_alive()
    started = time.time()
    rig.watcher.stop()
    assert time.time() - started < 5
    assert not thread.is_alive() and rig.watcher._thread is None
    assert not [t for t in threading.enumerate() if t.name == "football"]


def test_a_look_which_fails_doesnt_end_the_thread(tmp_path, caplog):
    rig = Rig(tmp_path)
    rig.settings["check_every"] = 0.01
    rig.football.reading = RuntimeError("boom")
    with caplog.at_level(logging.WARNING):
        rig.watcher.start()
        deadline = time.time() + 5
        while rig.football.reads < 3 and time.time() < deadline:
            time.sleep(0.01)
        rig.watcher.stop()
    assert rig.football.reads >= 3 and "boom" in caplog.text


class FakePeople:
    """Answers people() as Home Assistant does, with what a test sets, or raises it."""

    def __init__(self, found):
        self.found = found

    def people(self, timeout=None):
        if isinstance(self.found, Exception):
            raise self.found
        return self.found


def _person(state, person_id="person.n"):
    return {"id": person_id, "name": "N", "state": state, "devices": []}


@pytest.mark.parametrize("found, expected", [
    ([_person("home")], True),
    ([_person("not_home")], False),
    ([_person("Work")], False),
    ([_person("unknown")], None),
    ([_person("unavailable")], None),
    ([], None),
    ([_person("home"), _person("not_home", "person.m")], True),
    ([_person("not_home"), _person("home", "person.m")], False),
    ([{"id": "person.n"}], None),
    (requests.ConnectionError("down"), None),
    (ValueError("not json"), None),
    (KeyError("entity_id"), None),
], ids=["home", "not home", "a zone", "unknown", "unavailable", "nobody", "the first is home", "the first is out",
        "no state", "unreachable", "not json", "wrong shape"])
def test_user_home(found, expected):
    assert user_home(FakePeople(found)) is expected
