"""
Journeys Test
Working out how long a journey takes and when to leave for it, with a fake Home Assistant which notes every
journey asked of it and answers as each test says, a fake calendar and a real ChoiceStore in a temporary
folder - so nothing ever asks Google. The places are made up: Work, Uni, Gran's and the Metrocentre.
"""

import json
import logging
from datetime import datetime

import pytest
import requests

from voice_assistant import journeys
from voice_assistant.actions import Action
from voice_assistant.choices import ChoiceStore
from voice_assistant.clock_words import format_duration
from voice_assistant.config import Config
from voice_assistant.google_calendar import SignInNeeded
from voice_assistant.home_assistant import CANT_REACH, NoJourneys, NotAllowed
from voice_assistant.journeys import JOURNEY_DEFAULTS, Journeys, OverCap

ZONES = [{"id": "zone.home", "name": "Home"}, {"id": "zone.work", "name": "Work"},
         {"id": "zone.uni", "name": "Uni"}, {"id": "zone.grans", "name": "Gran's"}]
# A drive of 24 minutes over 10,300 metres - 6.4 miles - which Google says is 20 minutes on a clear road.
DRIVE = {"seconds": 1440, "clear_seconds": 1200, "metres": 10300}
SETTINGS = {"buffer_minutes": 10, "reminder_minutes": 5, "check_every_minutes": 5, "watch_minutes": 45,
            "change_minutes": 5, "heavy_minutes": 5, "heavy_percent": 20, "monthly_requests": 3000,
            "distance_unit": "miles"}


def at(year, month, day, hour, minute=0, second=0):
    """A local time on the machine, which is Europe/London or UTC when the tests run."""
    return datetime(year, month, day, hour, minute, second).astimezone()


# A Wednesday morning.
EIGHT = at(2026, 11, 18, 8)
SEVEN = at(2026, 11, 18, 7)


class FakeHome:
    """Home Assistant as far as journeys goes: zones, people, and a journey which is whatever it's given."""

    def __init__(self, answer=DRIVE, zones=ZONES, people=None):
        self.answer = answer
        self.zone_list = zones
        self.people_list = [{"id": "person.sam", "name": "Sam", "state": "home", "devices": []}] if people is None \
            else people
        self.asked = []

    def zones(self):
        if isinstance(self.zone_list, Exception):
            raise self.zone_list
        return list(self.zone_list)

    def people(self):
        if isinstance(self.people_list, Exception):
            raise self.people_list
        return list(self.people_list)

    def journey(self, origin, destination, mode="driving", departure=None):
        self.asked.append((origin, destination, mode, departure))
        answers = self.answer if isinstance(self.answer, list) else [self.answer]
        answer = answers[min(len(self.asked), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeCalendar:
    """The calendar's activities, and each one's events as a test gives them."""

    def __init__(self, events=(), activities=("Work",), failure=None):
        self.event_list = list(events)
        self.names = activities
        self.failure = failure
        self.read = []

    def calendars(self):
        if self.failure:
            raise self.failure
        return []

    def activity_calendars(self):
        return {name.lower(): {"id": f"{name.lower()}-id", "name": name} for name in self.names}

    def activity_words(self):
        words = {name.lower(): name.lower() for name in self.names}
        words.update({"shift": "work", "shifts": "work"} if "Work" in self.names else {})
        return words

    def events(self, first, days, calendar_ids=None):
        self.read.append((first, days, calendar_ids))
        return list(self.event_list)


def event(start, all_day=False, summary="Shift"):
    return {"summary": summary, "start": start, "end": None, "all_day": all_day, "calendar": "Work",
            "calendar_id": "work-id"}


@pytest.fixture
def store(tmp_path):
    return ChoiceStore(str(tmp_path / "journeys.json"), JOURNEY_DEFAULTS)


def make(store, home=None, calendar=None, clock=True, settings=None):
    """Journeys over fakes; the clock is only ever asked whether there is one."""
    settings = dict(SETTINGS, **(settings or {}))
    return Journeys(home or FakeHome(), calendar, object() if clock else None, store, lambda: settings)


def ask(journey, place, mode="driving", now=EIGHT, **kwargs):
    return journey.ask(place, mode, now=now, **kwargs)


def work_at(start):
    return FakeCalendar([event(start)])


# --- The words of a journey time.


@pytest.mark.parametrize("mode,words", [
    ("driving", "The drive to Work is 24 minutes, 6.4 miles."),
    ("walking", "The walk to Work is 24 minutes, 6.4 miles."),
    ("cycling", "The cycle to Work is 24 minutes, 6.4 miles."),
])
def test_a_journey_time_says_the_way_the_time_and_the_distance(store, mode, words):
    home = FakeHome({"seconds": 1440, "clear_seconds": None, "metres": 10300})
    answer = ask(make(store, home), "work", mode)
    assert answer == {"success": True, "message": words}
    # Cycling is Home Assistant's bicycling.
    assert home.asked == [("zone.home", "zone.work", "bicycling" if mode == "cycling" else mode, None)]


def test_public_transport_gives_a_time_and_no_distance(store):
    home = FakeHome({"seconds": 2100, "clear_seconds": None, "metres": 9000})
    assert ask(make(store, home), "uni", "transit") == {"success": True, "message": "Public transport to Uni takes 35 minutes."}
    assert home.asked == [("zone.home", "zone.uni", "transit", None)]


def test_public_transport_asked_when_to_leave_says_it_cant(store):
    home = FakeHome({"seconds": 2100, "clear_seconds": None, "metres": 9000})
    calendar = work_at(at(2026, 11, 18, 9))
    answer = ask(make(store, home, calendar), "work", "transit", leave=True)
    assert answer == {"success": True, "message": "Public transport to Work takes 35 minutes. "
                                                  "I can't say which one to catch, so I can't give a time to leave."}
    # No arrival time is looked for and no reminder offered: there is no time to leave to remind of.
    assert calendar.read == []
    assert len(home.asked) == 1


@pytest.mark.parametrize("seconds,said", [(1440, "24 minutes"), (1441, "25 minutes"), (1380, "23 minutes"),
                                          (10, "1 minute"), (0, "1 minute"), (5400, "1 hour and 30 minutes"),
                                          (3601, "1 hour and 1 minute")])
def test_minutes_are_rounded_up_and_never_none(store, seconds, said):
    home = FakeHome({"seconds": seconds, "clear_seconds": None, "metres": 1000})
    assert ask(make(store, home), "work")["message"].startswith(f"The drive to Work is {said}, ")


@pytest.mark.parametrize("unit,metres,said", [
    ("miles", 10300, "6.4 miles"), ("km", 10300, "10 km"), ("km", 6449, "6.4 km"), ("miles", 22530, "14 miles"),
    ("miles", 1609, "1 mile"), ("km", 1000, "1 km"), ("km", 1040, "1 km"), ("miles", 4828, "3 miles"),
    ("miles", 16093, "10 miles"), ("miles", 16000, "9.9 miles"), ("km", 9960, "10 km"), ("km", 500, "0.5 km")])
def test_a_distance_is_said_in_the_unit_set(store, unit, metres, said):
    home = FakeHome({"seconds": 600, "clear_seconds": None, "metres": metres})
    answer = ask(make(store, home, settings={"distance_unit": unit}), "work")
    assert answer["message"] == f"The drive to Work is 10 minutes, {said}."


# --- Heavy traffic.


@pytest.mark.parametrize("seconds,clear,said", [
    (1800, 1500, " Traffic's heavy: that's 5 minutes more than on a clear road."),
    # At exactly both thresholds: 5 minutes, and a fifth of 1,500 seconds is 300.
    (1799, 1500, ""),
    # Over by 5 minutes, but not by a fifth: 5 of 60.
    (3900, 3600, ""),
    # Over by a fifth, but not by 5 minutes: 4 of 20.
    (1440, 1200, ""),
    (1500, 1200, " Traffic's heavy: that's 5 minutes more than on a clear road."),
    (1650, 1200, " Traffic's heavy: that's 8 minutes more than on a clear road."),
    (1540, 1200, " Traffic's heavy: that's 6 minutes more than on a clear road."),
    (1510, 1200, " Traffic's heavy: that's 5 minutes more than on a clear road."),
    (1200, 1200, ""), (900, 1200, ""),
])
def test_heavy_traffic_is_said_only_over_both_thresholds(store, seconds, clear, said):
    home = FakeHome({"seconds": seconds, "clear_seconds": clear, "metres": 10300})
    expected = f"The drive to Work is {format_duration(-(-seconds // 60) * 60)}, 6.4 miles.{said}"
    assert ask(make(store, home), "work")["message"] == expected


def test_heavy_traffic_says_one_minute_in_the_singular(store):
    home = FakeHome({"seconds": 660, "clear_seconds": 600, "metres": 10300})
    settings = {"heavy_minutes": 1, "heavy_percent": 0}
    assert ask(make(store, home, settings=settings), "work")["message"] == \
        "The drive to Work is 11 minutes, 6.4 miles. Traffic's heavy: that's 1 minute more than on a clear road."


def test_the_traffic_thresholds_are_the_settings(store):
    home = FakeHome({"seconds": 1440, "clear_seconds": 1200, "metres": 10300})
    settings = {"heavy_minutes": 4, "heavy_percent": 20}
    assert ask(make(store, home, settings=settings), "work")["message"].endswith(
        "Traffic's heavy: that's 4 minutes more than on a clear road.")


def test_heavy_traffic_isnt_said_without_a_clear_time_or_for_the_other_ways(store):
    unknown = FakeHome({"seconds": 3000, "clear_seconds": None, "metres": 10300})
    assert "heavy" not in ask(make(store, unknown), "work")["message"]
    walking = FakeHome({"seconds": 3000, "clear_seconds": 1200, "metres": 10300})
    assert "heavy" not in ask(make(store, walking), "work", "walking")["message"]


# --- The place and the origin.


@pytest.mark.parametrize("said", ["my gran's", "Gran's", "the gran's", "MY GRAN'S", "  my   gran's  "])
def test_a_zone_is_found_by_its_name_said_any_way_and_said_back_as_its_own(store, said):
    home = FakeHome()
    answer = ask(make(store, home), said)
    assert answer == {"success": True, "message": "The drive to Gran's is 24 minutes, 6.4 miles."}
    assert home.asked == [("zone.home", "zone.grans", "driving", None)]


def test_only_a_leading_my_or_the_is_dropped_so_another_name_goes_to_google_as_said(store):
    home = FakeHome()
    answer = ask(make(store, home), "my grans")
    assert answer["message"].startswith("The drive to my grans is 24 minutes")
    assert home.asked == [("zone.home", "my grans", "driving", None)]


def test_home_is_the_home_zone(store):
    home = FakeHome()
    assert ask(make(store, home), "home")["message"].startswith("The drive to Home is 24 minutes")
    assert home.asked[0][1] == "zone.home"


def test_words_which_arent_a_zone_go_to_google_as_said_and_are_said_back(store):
    home = FakeHome()
    answer = ask(make(store, home), "the Metrocentre")
    assert answer["message"].startswith("The drive to the Metrocentre is 24 minutes, 6.4 miles.")
    assert home.asked == [("zone.home", "the Metrocentre", "driving", None)]


def test_a_place_is_cut_down_to_what_may_go_to_google_and_said_back_as_sent(store):
    home = FakeHome()
    answer = ask(make(store, home), "  the   Metrocentre <b>!  ")
    assert home.asked[0][1] == "the Metrocentre b"
    assert answer["message"].startswith("The drive to the Metrocentre b is")


@pytest.mark.parametrize("said", ["", "   ", "!!!", "<>"])
def test_no_place_asks_where_to_and_asks_nothing(store, said):
    home = FakeHome()
    assert ask(make(store, home), said) == {"success": False, "message": "Where to?"}
    assert home.asked == []


def test_the_origin_is_home_for_a_satellite_which_stays_and_the_person_for_one_which_travels(store):
    home = FakeHome(people=[{"id": "person.sam", "name": "Sam", "state": "not_home", "devices": []},
                            {"id": "person.kit", "name": "Kit", "state": "home", "devices": []}])
    journey = make(store, home)
    ask(journey, "work", satellite="kitchen")
    assert home.asked[-1][0] == "zone.home"
    journey.set_travels("macbook", True)
    ask(journey, "work", satellite="macbook")
    ask(journey, "work", satellite="kitchen")
    ask(journey, "work")
    assert [asked[0] for asked in home.asked] == ["zone.home", "person.sam", "zone.home", "zone.home"]


@pytest.mark.parametrize("people", [requests.ConnectionError("no"), ValueError("odd"), KeyError("entity_id"), []])
def test_a_travelling_satellite_whose_person_cant_be_read_gets_no_journey(store, people):
    home = FakeHome(people=people)
    journey = make(store, home)
    journey.set_travels("macbook", True)
    answer = ask(journey, "work", satellite="macbook")
    assert answer == {"success": False, "message": "I can't tell where you are right now."}
    assert home.asked == []


def test_which_satellites_travel_is_kept_across_a_restart(store, tmp_path):
    journey = make(store)
    assert not journey.travels("macbook") and not journey.travels(None)
    journey.set_travels("macbook", True)
    journey.set_travels("macbook", True)
    journey.set_travels("study", True)
    assert json.loads((tmp_path / "journeys.json").read_text())["travelling"] == ["macbook", "study"]
    again = make(ChoiceStore(str(tmp_path / "journeys.json"), JOURNEY_DEFAULTS))
    assert again.travels("macbook") and again.travels("study") and not again.travels("kitchen")
    again.set_travels("macbook", False)
    assert not again.travels("macbook") and again.travels("study")


def test_zones_which_cant_be_read_give_no_journey(store):
    home = FakeHome(zones=requests.ConnectionError("down"))
    assert ask(make(store, home), "work") == {"success": False, "message": CANT_REACH}
    assert home.asked == []


# --- The arrival time and when to leave.


def test_the_time_to_leave_comes_from_the_works_next_event_less_the_drive_and_the_buffer(store):
    home = FakeHome({"seconds": 1440, "clear_seconds": None, "metres": 10300})
    calendar = work_at(at(2026, 11, 18, 9))
    answer = ask(make(store, home, calendar), "work", leave=True)
    assert answer["success"]
    assert answer["message"] == "Work starts at 9:00 AM. The drive is 24 minutes, so leave by 8:26 AM. Want a reminder?"
    assert home.asked == [("zone.home", "zone.work", "driving", None)]
    # The calendar is read over today and tomorrow, for the Work calendar alone.
    assert calendar.read == [(EIGHT.date(), 2, ["work-id"])]


def test_the_reminder_offered_carries_everything_the_yes_needs(store):
    home = FakeHome({"seconds": 1440, "clear_seconds": None, "metres": 10300})
    answer = ask(make(store, home, work_at(at(2026, 11, 18, 9))), "my work", leave=True, satellite="kitchen")
    assert answer["ask_first"] == Action("set_leave_reminder", {
        "place": "Work", "destination": "zone.work", "origin": "zone.home", "mode": "driving",
        "arrive": at(2026, 11, 18, 9).timestamp(), "leave_by": at(2026, 11, 18, 8, 26).timestamp(), "watch": True})


def test_an_activity_is_found_by_a_word_for_it(store):
    calendar = work_at(at(2026, 11, 18, 9))
    home = FakeHome({"seconds": 1440, "clear_seconds": None, "metres": 10300})
    # "shift" is Work, and isn't a zone, so it goes to Google as said and the activity is found by its word.
    answer = ask(make(store, home, calendar), "shift", leave=True)
    assert answer["message"].startswith("Work starts at 9:00 AM. The drive is 24 minutes")


def test_the_event_is_the_next_timed_one_not_yet_begun_never_the_one_in_progress(store):
    home = FakeHome({"seconds": 1440, "clear_seconds": None, "metres": 10300})
    now = at(2026, 11, 18, 9, 10)
    calendar = FakeCalendar([event(at(2026, 11, 18, 9)), event(at(2026, 11, 18, 17)),
                             event(at(2026, 11, 19, 9))])
    answer = ask(make(store, home, calendar), "work", now=now, leave=True)
    assert answer["message"] == "Work starts at 5:00 PM. The drive is about 24 minutes, so leave by 4:26 PM. " \
                                "Want a reminder?"
    assert home.asked[-1][3] == "16:26"


def test_an_event_tomorrow_says_tomorrow_for_the_start_and_the_time_to_leave(store):
    home = FakeHome({"seconds": 1440, "clear_seconds": None, "metres": 10300})
    now = at(2026, 11, 18, 20)
    calendar = FakeCalendar([event(at(2026, 11, 19, 9))])
    answer = ask(make(store, home, calendar), "work", now=now, leave=True)
    assert answer["message"] == ("Work starts at 9:00 AM tomorrow. The drive is about 24 minutes, "
                                 "so leave by 8:26 AM tomorrow. Want a reminder?")


def test_an_event_a_day_off_to_the_minute_is_in_the_next_day_and_one_a_minute_later_isnt(store):
    home = FakeHome()
    now = at(2026, 11, 18, 9, 10)
    in_progress, all_day = event(at(2026, 11, 18, 9)), event(EIGHT.date(), all_day=True, summary="Training day")
    calendar = FakeCalendar([in_progress, all_day, event(at(2026, 11, 19, 9, 10))])
    answer = ask(make(store, home, calendar), "work", now=now, leave=True)
    assert answer["message"] == ("Work starts at 9:10 AM tomorrow. The drive is about 24 minutes, "
                                 "so leave by 8:36 AM tomorrow. Want a reminder?")
    calendar = FakeCalendar([in_progress, all_day, event(at(2026, 11, 19, 9, 11))])
    answer = ask(make(store, home, calendar), "work", now=now, leave=True)
    assert answer == {"success": True, "message": "You've no work in the next day. "
                                                  "The drive to Work is 24 minutes, 6.4 miles."}


def test_an_event_in_progress_or_all_day_alone_is_no_event(store):
    home = FakeHome()
    calendar = FakeCalendar([event(at(2026, 11, 18, 7, 50)), event(EIGHT.date(), all_day=True),
                             event(at(2026, 11, 18, 8))])
    answer = ask(make(store, home, calendar), "work", leave=True)
    assert answer["message"].startswith("You've no work in the next day. The drive to Work is")
    assert "ask_first" not in answer


def test_with_no_calendar_or_no_such_activity_the_time_is_asked_for(store):
    home = FakeHome({"seconds": 1440, "clear_seconds": None, "metres": 10300})
    wanted = "The drive to Work is 24 minutes, 6.4 miles. When do you need to be there?"
    assert ask(make(store, home, None), "work", leave=True) == {"success": True, "message": wanted}
    # No Work calendar, though the place is a zone.
    assert ask(make(store, home, FakeCalendar(activities=("Uni",))), "work", leave=True) == \
        {"success": True, "message": wanted}
    assert "ask_first" not in ask(make(store, home, None), "work", leave=True)


def test_an_unreadable_calendar_is_said_and_the_journey_time_still_given(store):
    home = FakeHome({"seconds": 1440, "clear_seconds": None, "metres": 10300})
    for failure in (SignInNeeded("no"), requests.ConnectionError("down"), KeyError("start")):
        calendar = FakeCalendar([event(at(2026, 11, 18, 9))], failure=failure)
        answer = ask(make(store, home, calendar), "work", leave=True)
        assert answer == {"success": True, "message": "I can't read your calendar right now. "
                                                      "The drive to Work is 24 minutes, 6.4 miles."}
    # Events which aren't the shape of events are unreadable too.
    calendar = FakeCalendar([{"summary": "No times"}])
    answer = ask(make(store, home, calendar), "work", leave=True)
    assert answer["message"].startswith("I can't read your calendar right now. The drive to Work")


def test_a_time_said_wins_over_the_calendar(store):
    home = FakeHome({"seconds": 1440, "clear_seconds": None, "metres": 10300})
    calendar = work_at(at(2026, 11, 18, 9))
    now = at(2026, 11, 18, 17)
    answer = ask(make(store, home, calendar), "gran's", now=now, leave=True, arrive_at="18:00")
    assert answer["message"] == ("The drive to Gran's is 24 minutes, so leave by 5:26 PM to be there for 6:00 PM. "
                                 "Want a reminder?")
    assert calendar.read == []
    assert answer["ask_first"] == Action("set_leave_reminder", {
        "place": "Gran's", "destination": "zone.grans", "origin": "zone.home", "mode": "driving",
        "arrive": at(2026, 11, 18, 18).timestamp(), "leave_by": at(2026, 11, 18, 17, 26).timestamp(),
        "watch": True})


def test_a_time_of_day_already_gone_is_tomorrows(store):
    home = FakeHome({"seconds": 1440, "clear_seconds": None, "metres": 10300})
    answer = ask(make(store, home), "gran's", now=at(2026, 11, 18, 19), leave=True, arrive_at="18:00")
    assert answer["message"] == ("The drive to Gran's is about 24 minutes, so leave by 5:26 PM tomorrow to be there "
                                 "for 6:00 PM tomorrow. Want a reminder?")
    assert answer["ask_first"].params["arrive"] == at(2026, 11, 19, 18).timestamp()
    # Home Assistant is asked what the drive is like at that time of day.
    assert home.asked[-1] == ("zone.home", "zone.grans", "driving", "17:26")


def test_a_date_and_time_said_are_the_arrival_time(store):
    home = FakeHome({"seconds": 1440, "clear_seconds": None, "metres": 10300})
    answer = ask(make(store, home), "gran's", now=at(2026, 11, 18, 17), leave=True, arrive_at="2026-11-18 18:00")
    assert answer["message"].startswith("The drive to Gran's is 24 minutes, so leave by 5:26 PM to be there for 6:00 PM.")
    # Not at all near: a day further off is said by its name, not as tomorrow.
    answer = ask(make(store, home), "gran's", now=at(2026, 11, 18, 17), leave=True, arrive_at="2026-11-20 18:00")
    assert answer["message"].endswith("so leave by 5:26 PM on Friday to be there for 6:00 PM on Friday. "
                                      "Want a reminder?")


@pytest.mark.parametrize("said", ["six", "25:00", "2026-11-18", "tomorrow", "18:00:00", "2026-13-01 18:00"])
def test_an_arrival_time_which_isnt_one_asks_nothing(store, said):
    home = FakeHome()
    answer = ask(make(store, home), "gran's", leave=True, arrive_at=said)
    assert answer == {"success": False, "message": "I didn't catch what time you need to be there."}
    assert home.asked == []


def test_an_arrival_time_already_gone_gives_no_time_to_leave(store):
    home = FakeHome()
    answer = ask(make(store, home), "gran's", now=at(2026, 11, 18, 19), leave=True, arrive_at="2026-11-18 18:00")
    assert answer == {"success": False, "message": "That time's already gone."}
    assert home.asked == []


def test_the_walk_and_the_cycle_say_so_and_are_not_watched(store):
    home = FakeHome({"seconds": 1440, "clear_seconds": None, "metres": 2000})
    journey = make(store, home, work_at(at(2026, 11, 18, 9)))
    walk = ask(journey, "work", "walking", leave=True)
    assert walk["message"] == "Work starts at 9:00 AM. The walk is 24 minutes, so leave by 8:26 AM. Want a reminder?"
    assert walk["ask_first"].params["watch"] is False and walk["ask_first"].params["mode"] == "walking"
    cycle = ask(journey, "gran's", "cycling", leave=True, arrive_at="09:00")
    assert cycle["message"] == ("The cycle to Gran's is 24 minutes, so leave by 8:26 AM to be there for 9:00 AM. "
                                "Want a reminder?")
    assert cycle["ask_first"].params["mode"] == "cycling" and cycle["ask_first"].params["watch"] is False
    # Their times don't change with the hour, so they're never asked for later.
    far = ask(journey, "gran's", "walking", now=at(2026, 11, 18, 6), leave=True, arrive_at="09:00")
    assert "about" not in far["message"] and [asked[3] for asked in home.asked] == [None, None, None]


def test_a_drive_from_where_he_is_isnt_watched_and_one_with_no_clock_isnt_offered(store):
    home = FakeHome({"seconds": 1440, "clear_seconds": None, "metres": 10300})
    journey = make(store, home)
    journey.set_travels("macbook", True)
    answer = ask(journey, "gran's", leave=True, arrive_at="09:00", satellite="macbook")
    assert answer["ask_first"].params["origin"] == "person.sam" and answer["ask_first"].params["watch"] is False
    answer = ask(make(store, home, clock=False), "gran's", leave=True, arrive_at="09:00")
    assert answer == {"success": True, "message": "The drive to Gran's is 24 minutes, so leave by 8:26 AM to be there "
                                                  "for 9:00 AM."}


@pytest.mark.parametrize("now,leaves", [(at(2026, 11, 18, 8, 24), True), (at(2026, 11, 18, 8, 24, 1), False),
                                        (at(2026, 11, 18, 8, 26), False), (at(2026, 11, 18, 8, 40), False)])
def test_under_two_minutes_to_go_or_past_says_to_leave_now_with_no_offer(store, now, leaves):
    home = FakeHome({"seconds": 1440, "clear_seconds": None, "metres": 10300})
    answer = ask(make(store, home, work_at(at(2026, 11, 18, 9))), "work", now=now, leave=True)
    if leaves:
        assert answer["message"].endswith("so leave by 8:26 AM. Want a reminder?")
        assert "ask_first" in answer
    else:
        assert answer == {"success": True, "message": "Work starts at 9:00 AM. The drive is 24 minutes, "
                                                      "so you need to leave now."}


def test_leaving_now_for_a_time_said_has_the_same_first_sentence(store):
    home = FakeHome({"seconds": 1440, "clear_seconds": None, "metres": 10300})
    answer = ask(make(store, home), "gran's", now=at(2026, 11, 18, 17, 30), leave=True, arrive_at="18:00")
    assert answer == {"success": True, "message": "The drive to Gran's is 24 minutes, so you need to leave now."}


def test_heavy_traffic_is_said_between_the_time_to_leave_and_the_offer(store):
    home = FakeHome({"seconds": 1800, "clear_seconds": 1500, "metres": 10300})
    answer = ask(make(store, home, work_at(at(2026, 11, 18, 9))), "work", leave=True)
    assert answer["message"] == ("Work starts at 9:00 AM. The drive is 30 minutes, so leave by 8:20 AM. "
                                 "Traffic's heavy: that's 5 minutes more than on a clear road. Want a reminder?")


def test_the_time_to_leave_uses_the_buffer_setting(store):
    home = FakeHome({"seconds": 1440, "clear_seconds": None, "metres": 10300})
    answer = ask(make(store, home, work_at(at(2026, 11, 18, 9)), settings={"buffer_minutes": 0}), "work", leave=True)
    assert "so leave by 8:36 AM." in answer["message"]


# --- Asking for the journey at the time of leaving.


def test_a_time_to_leave_far_off_asks_google_again_for_that_time_and_says_about(store):
    home = FakeHome([DRIVE, {"seconds": 1560, "clear_seconds": 1200, "metres": 10300}])
    answer = ask(make(store, home, work_at(at(2026, 11, 18, 9))), "work", now=SEVEN, leave=True)
    # 9:00 less 24 minutes and the buffer is 8:26, which is 86 minutes away, so Google is asked at 8:26 - and what
    # it says then (26 minutes) gives the time to leave, with no word of heavy traffic, which is for now.
    assert home.asked == [("zone.home", "zone.work", "driving", None), ("zone.home", "zone.work", "driving", "08:26")]
    assert answer["message"] == ("Work starts at 9:00 AM. The drive is about 26 minutes, so leave by 8:24 AM. "
                                 "Want a reminder?")
    assert answer["ask_first"].params["leave_by"] == at(2026, 11, 18, 8, 24).timestamp()


def test_a_time_to_leave_within_the_watch_window_is_asked_for_once_as_now(store):
    home = FakeHome()
    # 8:26 is exactly 45 minutes from 7:41, which is far enough; from 7:42 it is 44.
    ask(make(store, home, work_at(at(2026, 11, 18, 9))), "work", now=at(2026, 11, 18, 7, 41), leave=True)
    assert len(home.asked) == 2
    home.asked.clear()
    ask(make(store, home, work_at(at(2026, 11, 18, 9))), "work", now=at(2026, 11, 18, 7, 42), leave=True)
    assert len(home.asked) == 1
    home.asked.clear()
    ask(make(store, home, work_at(at(2026, 11, 18, 9)), settings={"watch_minutes": 120}), "work", now=SEVEN, leave=True)
    assert len(home.asked) == 1


def test_a_forecast_which_finds_no_route_leaves_the_time_as_it_was(store):
    home = FakeHome([DRIVE, None])
    answer = ask(make(store, home, work_at(at(2026, 11, 18, 9))), "work", now=SEVEN, leave=True)
    assert answer["message"] == "Work starts at 9:00 AM. The drive is 24 minutes, so leave by 8:26 AM. Want a reminder?"


def test_a_forecast_which_fails_gives_no_time(store):
    home = FakeHome([DRIVE, requests.ConnectionError("down")])
    answer = ask(make(store, home, work_at(at(2026, 11, 18, 9))), "work", now=SEVEN, leave=True)
    assert answer == {"success": False, "message": CANT_REACH}


# --- The failures.


def test_no_route_is_said_and_gives_no_time(store):
    home = FakeHome(None)
    answer = ask(make(store, home, work_at(at(2026, 11, 18, 9))), "the Metrocentre", leave=True, arrive_at="09:00")
    assert answer == {"success": False, "message": "I couldn't find a way to the Metrocentre."}


@pytest.mark.parametrize("error,said", [
    (NoJourneys("no entry"), "Journeys need Google Maps Travel Time set up in Home Assistant."),
    (NotAllowed("refused"), "I couldn't get a journey time."),
    (ValueError("odd answer"), "I couldn't get a journey time."),
    (requests.ConnectionError("down"), CANT_REACH),
    (requests.HTTPError("bad", response=type("R", (), {"status_code": 401})()),
     "Home Assistant didn't accept my token. HA_TOKEN in the .env file needs checking."),
])
def test_a_failure_is_said_and_gives_no_time(store, error, said):
    home = FakeHome(error)
    answer = ask(make(store, home, work_at(at(2026, 11, 18, 9))), "work", leave=True)
    assert answer == {"success": False, "message": said}


def test_a_failure_is_logged_once_by_its_kind_and_never_with_the_place(store, caplog):
    home = FakeHome(requests.ConnectionError("http://ha.example/secret-place"))
    with caplog.at_level(logging.INFO):
        ask(make(store, home), "the Metrocentre")
    logged = [record for record in caplog.records if record.name == journeys.logger.name]
    assert len(logged) == 1 and "ConnectionError" in logged[0].getMessage()
    assert "Metrocentre" not in caplog.text and "secret-place" not in caplog.text


def test_at_the_cap_nothing_is_asked_and_it_is_said(store):
    home = FakeHome()
    answer = ask(make(store, home, settings={"monthly_requests": 0}), "work")
    assert answer == {"success": False, "message": "I've asked Google for all the journeys I'm allowed this month."}
    assert home.asked == []


# --- The count.


def test_each_ask_of_google_is_counted_before_it_is_made_by_month(store, monkeypatch):
    monkeypatch.setattr(journeys, "_month", lambda: "2026-11")
    seen = []

    class Counting(FakeHome):
        def journey(self, *args, **kwargs):
            seen.append(store.get("requests"))
            return super().journey(*args, **kwargs)

    journey = make(store, Counting())
    journey.look("zone.home", "zone.work", "driving")
    journey.look("zone.home", "zone.work", "cycling", "08:00")
    assert seen == [1, 2]
    assert store.get("requests") == 2 and store.get("month") == "2026-11"


def test_a_new_month_starts_the_count_again(store, monkeypatch):
    monkeypatch.setattr(journeys, "_month", lambda: "2026-11")
    journey = make(store, settings={"monthly_requests": 2})
    journey.look("a", "b", "driving")
    journey.look("a", "b", "driving")
    with pytest.raises(OverCap):
        journey.look("a", "b", "driving")
    monkeypatch.setattr(journeys, "_month", lambda: "2026-12")
    journey.look("a", "b", "driving")
    assert store.get("requests") == 1 and store.get("month") == "2026-12"


def test_the_cap_is_read_at_each_use_and_refuses_without_asking(store, monkeypatch):
    monkeypatch.setattr(journeys, "_month", lambda: "2026-11")
    home = FakeHome()
    settings = {"monthly_requests": 1}
    journey = Journeys(home, None, None, store, lambda: settings)
    journey.look("a", "b", "driving")
    with pytest.raises(OverCap):
        journey.look("a", "b", "driving")
    assert len(home.asked) == 1 and store.get("requests") == 1
    settings["monthly_requests"] = 2
    journey.look("a", "b", "driving")
    assert len(home.asked) == 2


@pytest.mark.parametrize("saved", [
    "not json at all", "[1, 2]", '{"requests": "many", "month": 5}', '{"requests": -50, "month": "2026-11"}',
    '{"requests": 2.5, "month": "2026-11"}', '{"requests": true, "month": "2026-11"}',
    '{"requests": null, "month": null}', '{"requests": 1e999, "month": "2026-11"}'])
def test_a_count_file_in_nonsense_reads_as_none_asked_never_as_unlimited(tmp_path, monkeypatch, saved):
    monkeypatch.setattr(journeys, "_month", lambda: "2026-11")
    path = tmp_path / "journeys.json"
    path.write_text(saved)
    store = ChoiceStore(str(path), JOURNEY_DEFAULTS)
    home = FakeHome()
    journey = make(store, home, settings={"monthly_requests": 3})
    for _ in range(3):
        journey.look("a", "b", "driving")
    with pytest.raises(OverCap):
        journey.look("a", "b", "driving")
    assert len(home.asked) == 3


def test_a_huge_count_for_this_month_is_over_the_cap_not_unlimited(tmp_path, monkeypatch):
    monkeypatch.setattr(journeys, "_month", lambda: "2026-11")
    (tmp_path / "journeys.json").write_text('{"requests": 99999999999999999999, "month": "2026-11"}')
    journey = make(ChoiceStore(str(tmp_path / "journeys.json"), JOURNEY_DEFAULTS))
    with pytest.raises(OverCap):
        journey.look("a", "b", "driving")


def test_the_count_is_kept_if_it_cant_be_saved_and_still_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(journeys, "_month", lambda: "2026-11")
    # A file in the place of the folder, so nothing can be saved.
    (tmp_path / "blocked").write_text("")
    store = ChoiceStore(str(tmp_path / "blocked" / "journeys.json"), JOURNEY_DEFAULTS)
    journey = make(store, settings={"monthly_requests": 1})
    journey.look("a", "b", "driving")
    with pytest.raises(OverCap):
        journey.look("a", "b", "driving")


def test_look_gives_homes_answer_in_homes_words(store):
    home = FakeHome()
    assert make(store, home).look("zone.home", "zone.work", "cycling") == DRIVE
    assert home.asked == [("zone.home", "zone.work", "bicycling", None)]
    home.answer = None
    assert make(store, home).look("zone.home", "zone.work", "driving", "08:00") is None


def test_the_count_counts_a_journey_which_fails_too(store, monkeypatch):
    monkeypatch.setattr(journeys, "_month", lambda: "2026-11")
    journey = make(store, FakeHome(requests.ConnectionError("down")))
    ask(journey, "work")
    assert store.get("requests") == 1


# --- The settings.


@pytest.mark.parametrize("bad", [None, "lots", -5, 61, True, 2.5, [], {}])
def test_a_buffer_which_is_unusable_is_the_default_of_ten_minutes(store, bad):
    settings = {"buffer_minutes": bad}
    journey = Journeys(FakeHome(), work_at(at(2026, 11, 18, 9)), object(), store, lambda: settings)
    assert "so leave by 8:26 AM." in ask(journey, "work", leave=True)["message"]


def test_a_missing_section_or_a_settings_which_fails_is_every_default(store):
    for settings in (lambda: {}, lambda: None, lambda: (_ for _ in ()).throw(KeyError("journeys"))):
        assert [journeys.setting(settings, name) for name in SETTINGS] == list(SETTINGS.values())


def test_the_unit_is_read_in_any_case_and_a_strange_one_is_miles(store):
    for said, unit in (("KM", "km"), ("Miles", "miles"), ("furlongs", "miles"), (5, "miles")):
        assert journeys.setting(lambda: {"distance_unit": said}, "distance_unit") == unit


def test_settings_are_read_at_each_use(store):
    settings = dict(SETTINGS)
    home = FakeHome({"seconds": 1440, "clear_seconds": None, "metres": 10300})
    journey = Journeys(home, None, None, store, lambda: settings)
    assert ask(journey, "work")["message"].endswith("6.4 miles.")
    settings["distance_unit"] = "km"
    assert ask(journey, "work")["message"].endswith("10 km.")


def test_the_defaults_are_those_in_config_yaml(tmp_path):
    assert dict(Config(path=str(tmp_path / "none.yaml")).section("journeys")) == journeys.SETTING_DEFAULTS
    assert journeys.SETTING_DEFAULTS == SETTINGS


def test_an_unknown_way_of_travelling_is_refused_without_asking(store):
    home = FakeHome()
    answer = ask(make(store, home), "work", "teleporting")
    assert not answer["success"] and home.asked == []
