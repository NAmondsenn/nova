"""
Clean Schedule Test
Checks the nightly library check and the weekly note of clean songs (decision 0022, section 8):
when each is due, that a night or a week is done once whatever restarts, that a note waits out
the quiet hours, and what the note says - with fakes in place of the library check and ntfy, and
the store in the test's own folder. The times are local, as the system has them, so the tests
say the same in London and in UTC.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime

import pytest

from voice_assistant import clean_schedule
from voice_assistant.clean_schedule import CleanSchedule, note
from voice_assistant.clean_store import KINDS, CleanStore

# Monday 16 November 2026 is in ISO week 47; Monday the 23rd starts week 48.


def at(day, hour, minute=0):
    """Epoch seconds for that day of November 2026 at that local time."""
    return datetime(2026, 11, day, hour, minute).timestamp()


class Clock:
    """The time a look sees, set by each test."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeCheck:
    """Stands in for the library check: notes each start, and shows the store's lists as the page does."""

    def __init__(self, store, answer=None, held=0):
        self.store = store
        self.answer = answer or {"success": True, "message": "Checking the library."}
        self.held = held
        self.started = 0

    def start(self):
        self.started += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def state(self):
        return {**{kind: self.store.listed(kind) for kind in KINDS}, "held": self.held}


class FakeNtfy:
    """Stands in for ntfy: its phone topic, whether it's night, and each message sent."""

    def __init__(self, phone="phone-topic", quiet=False, takes=True):
        self.phone = phone
        self.is_quiet = quiet
        self.takes = takes
        self.sent = []

    def quiet(self):
        return self.is_quiet

    def send(self, topic, message, title="", priority=0, click=""):
        self.sent.append({"topic": topic, "message": message, "title": title, "click": click})
        return self.takes


DEFAULTS = {"nightly": True, "nightly_time": "05:30", "weekly_note": True, "weekly_day": "Monday",
            "weekly_time": "09:30"}


@pytest.fixture
def store(tmp_path):
    return CleanStore(str(tmp_path / "clean_songs.json"))


@pytest.fixture
def make(store):
    """Makes a schedule over the store, with the settings, page address and ntfy given, looking at the clock's time."""

    def made(now, settings=None, ntfy="default", link="https://nova.example.com", check=None):
        clock = Clock(now)
        settings = settings if settings is not None else dict(DEFAULTS)
        ntfy = FakeNtfy() if ntfy == "default" else ntfy
        check = check or FakeCheck(store)
        schedule = CleanSchedule(check, store, ntfy, lambda: settings, lambda: link, clock=clock)
        return schedule, clock, check, ntfy, settings

    return made


# The nightly check

def test_the_check_starts_once_at_the_nightly_time(make, store):
    schedule, clock, check, _, _ = make(at(17, 5, 0))
    schedule.look()
    clock.now = at(17, 5, 29)
    schedule.look()
    assert check.started == 0

    clock.now = at(17, 5, 30)
    schedule.look()
    clock.now = at(17, 5, 31)
    schedule.look()
    clock.now = at(17, 23, 59)
    schedule.look()

    assert check.started == 1
    assert store.schedule()["night_done"] == "2026-11-17"
    clock.now = at(18, 5, 30)
    schedule.look()
    assert check.started == 2
    assert store.schedule()["night_done"] == "2026-11-18"


def test_a_night_already_checked_isnt_checked_again_after_a_restart(make, store):
    first, clock, check, _, _ = make(at(17, 5, 0))
    first.look()
    clock.now = at(17, 5, 30)
    first.look()
    assert check.started == 1

    again, clock, check, _, _ = make(at(17, 5, 31))
    again.look()
    assert check.started == 0


@pytest.mark.parametrize("answer", [{"success": False, "message": "The library check is already running."},
                                    {"success": True, "message": "Carrying on the library check."}])
def test_a_check_running_or_paused_at_the_nights_time_is_carried_on_not_started_again(make, store, answer, caplog):
    # Review focus 1: start() carries a paused check on, and refuses a second while one runs; either way
    # the night is done, so the schedule never asks again that night.
    caplog.set_level(logging.INFO)
    schedule, clock, check, _, _ = make(at(17, 5, 0), check=FakeCheck(store, answer=answer))
    schedule.look()
    for minute in (30, 31, 32):
        clock.now = at(17, 5, minute)
        schedule.look()

    assert check.started == 1
    assert store.schedule()["night_done"] == "2026-11-17"
    assert answer["message"] in caplog.text


def test_a_check_which_couldnt_start_is_logged_and_the_night_still_done(make, store, caplog):
    schedule, clock, check, _, _ = make(at(17, 5, 0), check=FakeCheck(store, answer=RuntimeError("boom")))
    schedule.look()
    clock.now = at(17, 5, 30)
    schedule.look()
    clock.now = at(17, 5, 31)
    schedule.look()

    assert check.started == 1
    assert store.schedule()["night_done"] == "2026-11-17"
    assert "boom" in caplog.text


def test_nova_started_after_the_nights_time_doesnt_make_it_up(make, store, caplog):
    # Review focus 2: off at 05:30, started at 07:00 - nothing until the next night.
    caplog.set_level(logging.INFO)
    schedule, clock, check, _, _ = make(at(17, 7, 0))
    schedule.look()
    clock.now = at(17, 7, 1)
    schedule.look()

    assert check.started == 0
    assert store.schedule()["night_done"] == "2026-11-17"
    clock.now = at(18, 5, 30)
    schedule.look()
    assert check.started == 1


def test_switched_off_there_is_no_nightly_check(make, store):
    settings = {**DEFAULTS, "nightly": False}
    schedule, clock, check, _, _ = make(at(17, 5, 0), settings=settings)
    schedule.look()
    clock.now = at(17, 5, 30)
    schedule.look()

    assert check.started == 0
    # The night is done all the same, so switching it on in the day doesn't start a check then.
    assert store.schedule()["night_done"] == "2026-11-17"


def test_switched_on_in_the_day_the_check_waits_for_the_next_night(make, store):
    settings = {**DEFAULTS, "nightly": False}
    schedule, clock, check, _, _ = make(at(17, 5, 0), settings=settings)
    schedule.look()
    clock.now = at(17, 5, 30)
    schedule.look()
    settings["nightly"] = True
    for hour, minute in ((14, 0), (14, 1), (23, 59)):
        clock.now = at(17, hour, minute)
        schedule.look()
    assert check.started == 0

    clock.now = at(18, 5, 30)
    schedule.look()
    assert check.started == 1


def test_the_nights_time_is_read_from_the_settings_at_each_look(make):
    schedule, clock, check, _, settings = make(at(17, 5, 0))
    schedule.look()
    settings["nightly_time"] = "06:15"
    clock.now = at(17, 5, 30)
    schedule.look()
    assert check.started == 0

    clock.now = at(17, 6, 15)
    schedule.look()
    assert check.started == 1


@pytest.mark.parametrize("changed_at, starts", [((14, 0), False), ((13, 59), True)])
def test_moving_the_nights_time_to_one_gone_by_starts_no_check_in_the_day(make, store, changed_at, starts):
    # At 23:00 tonight, then set to 13:00 at 14:00: the night is over an hour past, so it's done, not
    # started in the day. Set to 13:00 at 13:59, it's a look in its first hour, which starts it.
    schedule, clock, check, _, settings = make(at(17, 12, 0), settings={**DEFAULTS, "nightly_time": "23:00"})
    schedule.look()
    settings["nightly_time"] = "13:00"
    for hour, minute in (changed_at, (14, 30), (23, 0)):
        clock.now = at(17, hour, minute)
        schedule.look()

    assert check.started == (1 if starts else 0)
    assert store.schedule()["night_done"] == "2026-11-17"
    clock.now = at(18, 12, 59)
    schedule.look()
    clock.now = at(18, 13, 0)
    schedule.look()
    assert check.started == (2 if starts else 1)


def test_a_time_which_isnt_one_is_read_as_the_default(make):
    schedule, clock, check, _, settings = make(at(17, 5, 0), settings={**DEFAULTS, "nightly_time": "5.30"})
    schedule.look()
    clock.now = at(17, 5, 29)
    schedule.look()
    assert check.started == 0
    clock.now = at(17, 5, 30)
    schedule.look()
    assert check.started == 1


# The weekly note

def entries(store, kind, *times):
    for number, when in enumerate(times):
        store.add(kind, f"{kind} {number}", at=when)


def test_the_note_goes_once_on_its_day_and_time_with_the_weeks_counts(make, store):
    # Last week's went, so nothing is due until this week's time.
    store.save_schedule({"week_sent": "2026-11-09", "note_at": at(9, 9, 30)})
    entries(store, "swapped", at(14, 12), at(15, 12), at(16, 3))
    entries(store, "only_clean", at(13, 12))
    entries(store, "unsure", at(12, 12), at(15, 1))
    entries(store, "couldnt", at(16, 6))
    check = FakeCheck(store, held=4)
    schedule, clock, _, ntfy, _ = make(at(16, 9, 0), check=check)
    schedule.look()
    clock.now = at(16, 9, 29)
    schedule.look()
    assert ntfy.sent == []

    clock.now = at(16, 9, 30)
    schedule.look()
    clock.now = at(16, 9, 31)
    schedule.look()

    assert ntfy.sent == [{"topic": "phone-topic", "title": "Clean songs", "click": "https://nova.example.com/",
                          "message": "This week Nova swapped 3 clean songs for explicit ones; 1 only comes down "
                                     "clean, 2 are unsure, 1 couldn't be swapped. 4 files wait in .nova-held. "
                                     "See the settings page."}]
    assert store.schedule()["week_sent"] == "2026-11-16"
    assert store.schedule()["note_at"] == at(16, 9, 30)


def test_the_first_note_counts_only_the_last_seven_days(make, store):
    entries(store, "swapped", at(9, 9, 29), at(9, 9, 31))
    schedule, clock, _, ntfy, _ = make(at(16, 9, 30))
    schedule.look()

    assert [sent["message"] for sent in ntfy.sent] == [
        "This week Nova swapped 1 clean song for an explicit one. See the settings page."]


def test_a_later_note_counts_only_what_came_after_the_last(make, store):
    store.save_schedule({"week_sent": "2026-11-09", "note_at": at(9, 9, 30)})
    entries(store, "unsure", at(9, 9, 0), at(10, 12))
    schedule, clock, _, ntfy, _ = make(at(16, 9, 30))
    schedule.look()

    assert [sent["message"] for sent in ntfy.sent] == ["This week 1 song is unsure. See the settings page."]


def test_entries_saved_without_a_time_count_as_old(tmp_path):
    # Listed before entries said when they were added.
    path = tmp_path / "clean_songs.json"
    path.write_text(json.dumps({"lists": {"swapped": [{"name": "Old Song", "reason": "", "song_id": "a1"}]}}))
    store = CleanStore(str(path))
    ntfy = FakeNtfy()
    schedule = CleanSchedule(FakeCheck(store), store, ntfy, lambda: dict(DEFAULTS), lambda: "",
                             clock=Clock(at(16, 9, 30)))
    schedule.look()

    assert ntfy.sent == []
    assert store.schedule()["week_sent"] == "2026-11-16"


def test_a_week_with_nothing_new_sends_nothing_and_is_done(make, store, caplog):
    # Review focus 5.
    caplog.set_level(logging.INFO)
    entries(store, "swapped", at(1, 12))
    schedule, clock, _, ntfy, _ = make(at(16, 9, 30), check=FakeCheck(store, held=3))
    schedule.look()
    clock.now = at(16, 9, 31)
    schedule.look()

    assert ntfy.sent == []
    assert store.schedule()["week_sent"] == "2026-11-16"
    assert store.schedule()["note_at"] == at(16, 9, 30)


def test_a_note_due_in_quiet_hours_waits_for_them_to_end_and_goes_once(make, store):
    # Review focus 3.
    entries(store, "swapped", at(15, 12))
    ntfy = FakeNtfy(quiet=True)
    schedule, clock, _, _, _ = make(at(16, 9, 30), ntfy=ntfy)
    schedule.look()
    clock.now = at(16, 9, 59)
    schedule.look()
    assert ntfy.sent == []
    assert "week_sent" not in store.schedule()

    ntfy.is_quiet = False
    clock.now = at(16, 10, 0)
    schedule.look()
    clock.now = at(16, 10, 1)
    schedule.look()

    assert len(ntfy.sent) == 1
    assert store.schedule()["week_sent"] == "2026-11-16"


def test_a_note_whose_quiet_hours_run_past_midnight_goes_the_next_morning(make, store):
    entries(store, "swapped", at(15, 12))
    ntfy = FakeNtfy(quiet=True)
    schedule, clock, _, _, _ = make(at(16, 23, 30), settings={**DEFAULTS, "weekly_time": "23:30"}, ntfy=ntfy)
    schedule.look()
    clock.now = at(17, 6, 59)
    schedule.look()
    assert ntfy.sent == []

    ntfy.is_quiet = False
    clock.now = at(17, 7, 0)
    schedule.look()

    assert len(ntfy.sent) == 1
    assert store.schedule()["week_sent"] == "2026-11-16"


def test_a_note_due_late_on_sunday_in_quiet_hours_goes_on_monday_morning_once(make, store):
    entries(store, "swapped", at(20, 12))
    ntfy = FakeNtfy(quiet=True)
    settings = {**DEFAULTS, "weekly_day": "Sunday", "weekly_time": "23:00"}
    schedule, clock, _, _, _ = make(at(22, 22, 59), settings=settings, ntfy=ntfy)
    for day, hour, minute in ((22, 22, 59), (22, 23, 0), (22, 23, 59), (23, 0, 0), (23, 6, 59)):
        clock.now = at(day, hour, minute)
        schedule.look()
    assert ntfy.sent == []

    ntfy.is_quiet = False
    for minute in (0, 1, 2):
        clock.now = at(23, 7, minute)
        schedule.look()

    assert len(ntfy.sent) == 1
    assert store.schedule()["week_sent"] == "2026-11-22"


def test_a_note_due_while_nova_was_off_goes_at_the_next_look_once(make, store):
    store.save_schedule({"week_sent": "2026-11-16", "note_at": at(16, 9, 30)})
    entries(store, "unsure", at(20, 12))
    schedule, clock, _, ntfy, _ = make(at(23, 12, 0))
    schedule.look()
    clock.now = at(23, 12, 1)
    schedule.look()

    assert [sent["message"] for sent in ntfy.sent] == ["This week 1 song is unsure. See the settings page."]
    assert store.schedule()["week_sent"] == "2026-11-23"


def test_started_mid_week_the_first_note_goes_once_with_the_last_seven_days(make, store):
    entries(store, "swapped", at(11, 11, 59), at(12, 12))
    schedule, clock, _, ntfy, _ = make(at(18, 12))
    schedule.look()
    clock.now = at(18, 12, 1)
    schedule.look()

    assert [sent["message"] for sent in ntfy.sent] == [
        "This week Nova swapped 1 clean song for an explicit one. See the settings page."]
    assert store.schedule()["week_sent"] == "2026-11-16"


def test_a_note_not_sent_while_nova_was_off_goes_later_that_week_but_not_the_next(make, store):
    entries(store, "swapped", at(15, 12))
    schedule, clock, _, ntfy, _ = make(at(18, 12))
    schedule.look()
    assert len(ntfy.sent) == 1

    entries(store, "unsure", at(19, 12))
    clock.now = at(22, 23, 59)
    schedule.look()
    assert len(ntfy.sent) == 1
    clock.now = at(23, 9, 30)
    schedule.look()
    assert [sent["message"] for sent in ntfy.sent][1] == "This week 1 song is unsure. See the settings page."


def test_before_its_day_in_the_week_no_note_goes(make, store):
    store.save_schedule({"week_sent": "2026-11-11", "note_at": at(11, 9, 30)})
    entries(store, "swapped", at(15, 12))
    schedule, clock, _, ntfy, _ = make(at(17, 12), settings={**DEFAULTS, "weekly_day": "Wednesday"})
    schedule.look()
    assert ntfy.sent == []

    clock.now = at(18, 9, 30)
    schedule.look()
    assert len(ntfy.sent) == 1


def test_the_day_is_read_in_any_capitals(make, store):
    entries(store, "swapped", at(15, 12))
    schedule, _, _, ntfy, _ = make(at(17, 9, 30), settings={**DEFAULTS, "weekly_day": " tuesday "})
    schedule.look()
    assert len(ntfy.sent) == 1


def test_switched_off_there_is_no_weekly_note(make, store):
    entries(store, "swapped", at(15, 12))
    schedule, _, _, ntfy, _ = make(at(16, 9, 30), settings={**DEFAULTS, "weekly_note": False})
    schedule.look()

    assert ntfy.sent == []
    assert "week_sent" not in store.schedule()


def test_the_notes_day_and_time_are_read_from_the_settings_at_each_look(make, store):
    store.save_schedule({"week_sent": "2026-11-09", "note_at": at(9, 9, 30)})
    entries(store, "swapped", at(15, 12))
    schedule, clock, _, ntfy, settings = make(at(16, 9, 0))
    schedule.look()
    settings["weekly_time"] = "11:00"
    clock.now = at(16, 9, 30)
    schedule.look()
    assert ntfy.sent == []

    clock.now = at(16, 11, 0)
    schedule.look()
    assert len(ntfy.sent) == 1


def test_moving_the_notes_day_after_it_went_sends_no_extra_note(make, store):
    # Monday's note went; on Tuesday the day is set to Wednesday, whose last was the 11th - before it.
    store.save_schedule({"week_sent": "2026-11-16", "note_at": at(16, 9, 30)})
    entries(store, "unsure", at(16, 12))
    schedule, clock, _, ntfy, settings = make(at(17, 12, 0))
    settings["weekly_day"] = "Wednesday"
    schedule.look()
    clock.now = at(18, 9, 29)
    schedule.look()
    assert ntfy.sent == []

    clock.now = at(18, 9, 30)
    schedule.look()
    assert [sent["message"] for sent in ntfy.sent] == ["This week 1 song is unsure. See the settings page."]
    assert store.schedule()["week_sent"] == "2026-11-18"


def test_moving_the_notes_time_later_after_it_went_sends_no_second_that_day(make, store):
    store.save_schedule({"week_sent": "2026-11-09", "note_at": at(9, 9, 30)})
    entries(store, "swapped", at(15, 12))
    schedule, clock, _, ntfy, settings = make(at(16, 9, 30))
    schedule.look()
    assert len(ntfy.sent) == 1

    entries(store, "unsure", at(16, 9, 45))
    settings["weekly_time"] = "11:00"
    for hour, minute in ((10, 0), (11, 0), (23, 59)):
        clock.now = at(16, hour, minute)
        schedule.look()
    assert len(ntfy.sent) == 1

    clock.now = at(23, 11, 0)
    schedule.look()
    assert [sent["message"] for sent in ntfy.sent][1] == "This week 1 song is unsure. See the settings page."


def test_a_last_note_whose_date_isnt_one_is_read_as_none_sent(make, store):
    # Hand-edited: "soon" would be after every date, and no note would ever go again.
    store.save_schedule({"week_sent": "soon", "note_at": at(9, 9, 30)})
    entries(store, "swapped", at(15, 12))
    schedule, _, _, ntfy, _ = make(at(16, 9, 30))
    schedule.look()

    assert len(ntfy.sent) == 1
    assert store.schedule()["week_sent"] == "2026-11-16"


def test_a_song_listed_as_the_note_is_made_is_in_the_next_note_not_this_one(make, store):
    # Listed by the library check after the note took its time, but before it read the lists.
    store.save_schedule({"week_sent": "2026-11-09", "note_at": at(9, 9, 30)})
    entries(store, "swapped", at(15, 12))

    class ListingMeanwhile(FakeCheck):
        def state(self):
            if not store.listed("unsure"):
                store.add("unsure", "Late Song", "heard 2 of its 10 swears", at=at(16, 9, 30) + 1)
            return super().state()

    schedule, clock, _, ntfy, _ = make(at(16, 9, 30), check=ListingMeanwhile(store))
    schedule.look()
    clock.now = at(23, 9, 30)
    schedule.look()

    assert [sent["message"] for sent in ntfy.sent] == [
        "This week Nova swapped 1 clean song for an explicit one. See the settings page.",
        "This week 1 song is unsure. See the settings page."]


def test_a_note_ntfy_wont_take_is_logged_and_the_week_still_done(make, store, caplog):
    entries(store, "swapped", at(15, 12))
    ntfy = FakeNtfy(takes=False)
    schedule, clock, _, _, _ = make(at(16, 9, 30), ntfy=ntfy)
    schedule.look()
    clock.now = at(16, 9, 31)
    schedule.look()

    assert len(ntfy.sent) == 1
    assert "couldn't be sent" in caplog.text
    assert store.schedule()["week_sent"] == "2026-11-16"


def test_a_send_which_raises_is_logged_and_the_week_still_done(make, store, caplog):
    entries(store, "swapped", at(15, 12))
    ntfy = FakeNtfy()

    def send(*args, **kwargs):
        ntfy.sent.append(args)
        raise RuntimeError("boom")

    ntfy.send = send
    schedule, clock, _, _, _ = make(at(16, 9, 30), ntfy=ntfy)
    schedule.look()
    clock.now = at(16, 9, 31)
    schedule.look()

    assert len(ntfy.sent) == 1
    assert "RuntimeError" in caplog.text
    assert store.schedule()["week_sent"] == "2026-11-16"


@pytest.mark.parametrize("ntfy", [None, FakeNtfy(phone="")])
def test_with_nowhere_to_send_the_week_is_done_and_logged(make, store, ntfy, caplog):
    caplog.set_level(logging.INFO)
    entries(store, "swapped", at(15, 12))
    schedule, _, _, _, _ = make(at(16, 9, 30), ntfy=ntfy)
    schedule.look()

    if ntfy is not None:
        assert ntfy.sent == []
    assert store.schedule()["week_sent"] == "2026-11-16"
    assert "no phone topic" in caplog.text


@pytest.mark.parametrize("link, click", [("", ""), ("https://nova.example.com/ ", "https://nova.example.com/"),
                                         (None, "")])
def test_the_notes_link_is_the_pages_address_when_its_known(make, store, link, click):
    entries(store, "swapped", at(15, 12))
    schedule, _, _, ntfy, _ = make(at(16, 9, 30), link=link)
    schedule.look()

    assert ntfy.sent[0]["click"] == click


def test_a_record_which_cant_be_saved_still_holds_for_this_run(make, store, monkeypatch, caplog):
    # Or every minute's look would start the check, or send the note, again.
    entries(store, "swapped", at(15, 12))

    def refuse(record):
        raise OSError("disk full")

    monkeypatch.setattr(store, "save_schedule", refuse)
    schedule, clock, check, ntfy, _ = make(at(16, 5, 0))
    schedule.look()
    for hour, minute in ((5, 30), (5, 31), (9, 30), (9, 31)):
        clock.now = at(16, hour, minute)
        schedule.look()

    assert check.started == 1
    assert len(ntfy.sent) == 1
    assert "OSError" in caplog.text


# What the note says

@pytest.mark.parametrize("counts, held, said", [
    ({"swapped": 3, "only_clean": 1, "unsure": 2, "couldnt": 1}, 4,
     "This week Nova swapped 3 clean songs for explicit ones; 1 only comes down clean, 2 are unsure, 1 couldn't "
     "be swapped. 4 files wait in .nova-held. See the settings page."),
    ({"swapped": 1, "only_clean": 0, "unsure": 0, "couldnt": 0}, 0,
     "This week Nova swapped 1 clean song for an explicit one. See the settings page."),
    ({"swapped": 2, "only_clean": 0, "unsure": 1, "couldnt": 0}, 1,
     "This week Nova swapped 2 clean songs for explicit ones; 1 is unsure. 1 file waits in .nova-held. "
     "See the settings page."),
    ({"swapped": 0, "only_clean": 2, "unsure": 0, "couldnt": 3}, 0,
     "This week 2 songs only come down clean, 3 couldn't be swapped. See the settings page."),
    ({"swapped": 0, "only_clean": 0, "unsure": 0, "couldnt": 1}, 2,
     "This week 1 song couldn't be swapped. 2 files wait in .nova-held. See the settings page."),
])
def test_the_note_leaves_out_what_didnt_happen(counts, held, said):
    assert note(counts, held) == said


def test_a_note_after_more_than_eight_days_says_since_the_last_note(make, store):
    store.save_schedule({"week_sent": "2026-11-02", "note_at": at(2, 9, 30)})
    entries(store, "swapped", at(5, 12))
    entries(store, "unsure", at(10, 12))
    schedule, _, _, ntfy, _ = make(at(16, 9, 30))
    schedule.look()

    assert [sent["message"] for sent in ntfy.sent] == [
        "Since the last note, Nova swapped 1 clean song for an explicit one; 1 is unsure. See the settings page."]


def test_a_note_eight_days_after_the_last_still_says_this_week(make, store):
    # A note held a day by quiet hours or Nova being off is still the week's.
    store.save_schedule({"week_sent": "2026-11-09", "note_at": at(8, 9, 30)})
    entries(store, "unsure", at(10, 12))
    schedule, _, _, ntfy, _ = make(at(16, 9, 30))
    schedule.look()

    assert [sent["message"] for sent in ntfy.sent] == ["This week 1 song is unsure. See the settings page."]


# The clocks going back

@pytest.fixture
def london():
    """The system's zone as London's, where 01:00 to 02:00 comes twice on 25 October 2026."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/London"
    time.tzset()
    yield
    if before is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = before
    time.tzset()


def test_when_the_clocks_go_back_the_night_and_the_note_are_done_once(london, store):
    check, ntfy = FakeCheck(store), FakeNtfy()
    # Last week's note went, so the look at midnight, before this week's time, sends none.
    store.save_schedule({"week_sent": "2026-10-18", "note_at": datetime(2026, 10, 18, 1, 30).timestamp()})
    entries(store, "swapped", datetime(2026, 10, 24, 12).timestamp())
    settings = {**DEFAULTS, "nightly_time": "01:30", "weekly_day": "Sunday", "weekly_time": "01:30"}
    start = datetime(2026, 10, 25, 0, 0).timestamp()
    clock = Clock(start)
    schedule = CleanSchedule(check, store, ntfy, lambda: settings, lambda: "", clock=clock)
    # Every minute of the five hours from midnight, which the clock shows as midnight to four.
    for minute in range(5 * 60):
        clock.now = start + minute * 60
        schedule.look()

    assert datetime.fromtimestamp(clock.now).hour == 3
    assert check.started == 1
    assert len(ntfy.sent) == 1
    assert store.schedule()["night_done"] == store.schedule()["week_sent"] == "2026-10-25"
    # Sent at the first 01:30, and nothing done at the second.
    assert store.schedule()["note_at"] == datetime(2026, 10, 25, 1, 30, fold=0).timestamp()


# The thread

def test_each_look_waits_until_the_next_minute_starts(store):
    # Sixty seconds after a look which started late would step over a minute, and a 23:59 with it.
    clock = Clock(at(17, 23, 58) + 20.5)
    schedule = CleanSchedule(FakeCheck(store), store, None, lambda: dict(DEFAULTS), lambda: "", clock=clock)

    assert schedule._until_next_look() == pytest.approx(39.5)
    clock.now = at(17, 23, 59)
    assert schedule._until_next_look() == pytest.approx(60)


def test_the_thread_looks_at_once_and_stops_promptly(store, monkeypatch):
    schedule = CleanSchedule(FakeCheck(store), store, None, lambda: dict(DEFAULTS), lambda: "")
    looked = threading.Event()
    monkeypatch.setattr(schedule, "look", looked.set)
    assert clean_schedule.LOOK_EVERY == 60

    schedule.start()
    assert looked.wait(5)
    assert any(thread.name == "clean-schedule" and thread.daemon for thread in threading.enumerate())
    stopped_at = datetime.now()
    schedule.stop()

    assert (datetime.now() - stopped_at).total_seconds() < 2
    assert not any(thread.name == "clean-schedule" for thread in threading.enumerate())


def test_a_look_which_fails_is_logged_and_the_thread_carries_on(store, monkeypatch, caplog):
    schedule = CleanSchedule(FakeCheck(store), store, None, lambda: dict(DEFAULTS), lambda: "")
    looks = []
    second = threading.Event()

    def look():
        looks.append(1)
        if len(looks) == 1:
            raise RuntimeError("boom")
        second.set()

    monkeypatch.setattr(schedule, "look", look)
    monkeypatch.setattr(clean_schedule, "LOOK_EVERY", 0.01)
    schedule.start()
    try:
        assert second.wait(5)
    finally:
        schedule.stop()
    assert "boom" in caplog.text
