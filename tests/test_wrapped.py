"""
Tests for the Wrapped: making one from the listening snapshot, keeping them, and the watcher which makes each
when its time comes and tells the phone. The data is SoulSync's answers from tests/fixtures, with made-up names.
"""

import copy
import json
import os
import re
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from voice_assistant import wrapped as wrapped_module
from voice_assistant.listening import Snapshot, ListeningUnavailable
from voice_assistant.wrapped import WrappedStore, WrappedWatcher, make, make_with_pictures, time_of_day

ARTIST_ONE = "/api/navidrome/cover/artist-one"


def _fixture(name):
    with open(os.path.join(os.path.dirname(__file__), "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


def _snapshot(pictures=None):
    """A snapshot as SoulSync's fixtures give it, the 30 days different from the 12 months where it matters."""
    cached = _fixture("soulsync_stats_cached.json")
    windows = {window: copy.deepcopy(cached) for window in ("7d", "30d", "12m", "all")}
    month = windows["30d"]
    month["overview"].update(total_plays=480, total_time_ms=26_400_000, unique_artists=14, unique_albums=18,
                             unique_tracks=60)
    month["clock"]["peak"]["hour"] = 3
    month["rhythm"].update(longest_streak=9, active_days=21, busiest_day={"date": "2026-11-20", "plays": 41})
    month["genres"] = [{"genre": "Jazz", "play_count": 90, "percentage": 70.0}, {"genre": "Pop", "play_count": 40,
                                                                                "percentage": 30.0}]
    windows["12m"]["rhythm"].update(longest_streak=31)
    return Snapshot(windows=windows, year=_fixture("soulsync_stats_year.json"), pictures=pictures or {}, at=1.0)


# The very start of 1 December, just after SoulSync's last rebuild of November.
DECEMBER = datetime(2026, 12, 1, 0, 10)


def test_make_year_from_fixture():
    snapshot = _snapshot({ARTIST_ONE: (b"one", "image/jpeg"),
                          "/api/navidrome/cover/artist-seven": (b"seven", "image/png")})
    wrapped = make(snapshot, "year", DECEMBER)

    assert wrapped["key"] == "2026" and wrapped["kind"] == "year"
    assert wrapped["made"] == "2026-12-01T00:10:00"
    assert wrapped["period"] == {"start": "2025-12-01", "end": "2026-11-15"}
    assert (wrapped["minutes"], wrapped["plays"], wrapped["artists"], wrapped["albums"], wrapped["tracks"],
            wrapped["active_days"]) == (3000, 900, 30, 40, 200, 90)
    assert [(a["name"], a["plays"]) for a in wrapped["top_artists"]] == [
        ("Artist One", 200), ("Artist Two", 170), ("Artist Three", 140), ("Artist Four", 110), ("Artist Five", 80)]
    # Only the pictures the snapshot has are saved, in the order they are found.
    assert [a["picture"] for a in wrapped["top_artists"]] == [0, None, None, None, None]
    assert wrapped["top_tracks"][0] == {"name": "Song A", "artist": "Artist One", "plays": 40}
    assert wrapped["top_albums"][0] == {"name": "Album One", "artist": "Artist One", "plays": 80}
    assert len(wrapped["top_tracks"]) == len(wrapped["top_albums"]) == 5
    assert len(wrapped["discoveries"]) == 5
    assert wrapped["discoveries"][0] == {"name": "Artist Seven", "plays": 20, "picture": 1}
    assert wrapped["months"] == [{"label": "Nov 2026", "minutes": 900, "top_artist": "Artist One"},
                                 {"label": "Oct 2026", "minutes": 700, "top_artist": None}]
    # The 12 months' genres and rhythm, and the year's own peak hour and day.
    assert wrapped["top_genre"] == {"name": "Pop", "percentage": 58.3}
    assert wrapped["peak_hour"] == 18 and wrapped["time_of_day"] == "an evening listener"
    assert wrapped["longest_streak"] == 31
    assert wrapped["busiest_day"] == {"date": "2026-11-13", "plays": 30}
    json.dumps(wrapped)


def test_the_pictures_come_with_the_wrapped_in_the_order_its_indexes_say():
    snapshot = _snapshot({ARTIST_ONE: (b"one", "image/jpeg"),
                          "/api/navidrome/cover/artist-seven": (b"seven", "image/png")})
    wrapped, pictures = make_with_pictures(snapshot, "year", DECEMBER)
    assert pictures == [(b"one", "image/jpeg"), (b"seven", "image/png")]
    assert pictures[wrapped["top_artists"][0]["picture"]] == (b"one", "image/jpeg")
    # The same picture for an artist who is both a top artist and a discovery is kept once.
    snapshot.year["discoveries"][0]["image_url"] = ARTIST_ONE
    wrapped, pictures = make_with_pictures(snapshot, "year", DECEMBER)
    assert pictures == [(b"one", "image/jpeg")]
    assert wrapped["discoveries"][0]["picture"] == 0


def test_make_month_covers_last_30_days():
    snapshot = _snapshot({ARTIST_ONE: (b"one", "image/jpeg")})
    wrapped = make(snapshot, "month", DECEMBER)

    # The month being covered, not the one it's made in, and the real dates of the 30 days.
    assert wrapped["key"] == "2026-11" and wrapped["kind"] == "month"
    assert wrapped["period"] == {"start": "2026-11-01", "end": "2026-11-30"}
    assert (wrapped["minutes"], wrapped["plays"], wrapped["artists"], wrapped["albums"], wrapped["tracks"],
            wrapped["active_days"]) == (440, 480, 14, 18, 60, 21)
    assert [a["plays"] for a in wrapped["top_artists"]] == [30, 26, 22, 18, 14]
    assert wrapped["top_artists"][0]["picture"] == 0 and wrapped["top_artists"][1]["picture"] is None
    # The month's list is SoulSync's cached one, which has fewer than five tracks and albums to give.
    assert wrapped["top_tracks"][0] == {"name": "Song A", "artist": "Artist One", "plays": 12}
    assert wrapped["top_albums"][0] == {"name": "Album One", "artist": "Artist One", "plays": 20}
    assert wrapped["top_genre"] == {"name": "Jazz", "percentage": 70.0}
    assert wrapped["peak_hour"] == 3 and wrapped["time_of_day"] == "a night owl"
    assert wrapped["longest_streak"] == 9
    assert wrapped["busiest_day"] == {"date": "2026-11-20", "plays": 41}
    assert "months" not in wrapped and "discoveries" not in wrapped

    # Made in the middle of a month, it is the month before's, with the 30 days back from then.
    late = make(snapshot, "month", datetime(2026, 12, 9, 12, 0))
    assert late["key"] == "2026-11" and late["period"] == {"start": "2026-11-09", "end": "2026-12-08"}
    # And in January it is December's, of the year before.
    assert make(snapshot, "month", datetime(2027, 1, 1, 0, 10))["key"] == "2026-12"


def test_the_year_is_named_for_the_year_it_covers():
    snapshot = _snapshot()
    assert make(snapshot, "year", datetime(2027, 1, 1, 0, 10))["key"] == "2026"
    assert make(snapshot, "year", datetime(2026, 12, 1, 0, 10))["key"] == "2026"


@pytest.mark.parametrize("hour, said", [
    (4, "a night owl"), (5, "an early bird"), (11, "an early bird"), (12, "an afternoon listener"),
    (17, "an afternoon listener"), (18, "an evening listener"), (22, "an evening listener"), (23, "a night owl"),
    (0, "a night owl")])
def test_time_of_day_edges(hour, said):
    assert time_of_day(hour) == said


def test_no_peak_hour_says_nothing_of_the_time_of_day():
    snapshot = _snapshot()
    snapshot.windows["30d"]["clock"]["peak"] = None
    wrapped = make(snapshot, "month", DECEMBER)
    assert wrapped["peak_hour"] is None and wrapped["time_of_day"] is None


def test_empty_month_not_made():
    snapshot = _snapshot()
    snapshot.windows["30d"]["overview"]["total_plays"] = 0
    assert make(snapshot, "month", DECEMBER) is None
    snapshot.year["totals"]["plays"] = 0
    assert make(snapshot, "year", DECEMBER) is None
    assert make(Snapshot(), "year", DECEMBER) is None and make(Snapshot(), "month", DECEMBER) is None


# The store

def _wrapped(key, kind=None):
    return {"key": key, "kind": kind or ("year" if len(key) == 4 else "month"), "plays": 1}


def test_store_round_trip_and_key_checked(tmp_path):
    store = WrappedStore(str(tmp_path / "wrapped"))
    assert store.load("2026-11") is None and store.keys() == []
    store.save(_wrapped("2026-11"), [(b"jpeg bytes", "image/jpeg"), (b"png bytes", "image/png"),
                                     (b"webp bytes", "image/webp")])

    assert store.load("2026-11") == _wrapped("2026-11")
    assert store.keys() == ["2026-11"]
    assert store.picture("2026-11", 0) == (b"jpeg bytes", "image/jpeg")
    assert store.picture("2026-11", 1) == (b"png bytes", "image/png")
    assert store.picture("2026-11", 2) == (b"webp bytes", "image/webp")
    assert store.picture("2026-11", 3) is None and store.picture("2026-10", 0) is None
    assert sorted(os.listdir(tmp_path / "wrapped")) == [
        "2026-11-0.jpg", "2026-11-1.png", "2026-11-2.webp", "2026-11.json"]

    # Nothing outside the folder, and nothing which isn't a key, is ever read or written.
    (tmp_path / "x.json").write_text('{"key": "x"}')
    for bad in ("../x", "..", "", "2026-1", "2026-11.json", "20261", "2026-11/../x", "2026-11\n", None):
        assert store.load(bad) is None and store.picture(bad, 0) is None
        with pytest.raises(ValueError):
            store.save({"key": bad}, [])
    assert store.picture("2026-11", -1) is None and store.picture("2026-11", "0") is None
    assert store.keys() == ["2026-11"]


def test_store_saves_again_over_the_same_key(tmp_path):
    store = WrappedStore(str(tmp_path))
    store.save(_wrapped("2026-11"), [(b"a", "image/jpeg"), (b"b", "image/jpeg")])
    store.save({**_wrapped("2026-11"), "plays": 2}, [(b"c", "image/jpeg")])
    assert store.load("2026-11")["plays"] == 2
    assert store.picture("2026-11", 0) == (b"c", "image/jpeg") and store.picture("2026-11", 1) is None


def test_store_newest_first_and_a_year_before_its_months(tmp_path):
    store = WrappedStore(str(tmp_path))
    for key in ("2025", "2026-01", "2026-12", "2026", "2025-12", "2026-02"):
        store.save(_wrapped(key), [])
    assert store.keys() == ["2026", "2026-12", "2026-02", "2026-01", "2025", "2025-12"]


def test_store_ignores_files_which_arent_a_wrapped(tmp_path):
    store = WrappedStore(str(tmp_path))
    store.save(_wrapped("2026-11"), [])
    (tmp_path / "notes.json").write_text("{}")
    (tmp_path / "2026-10.json").write_text("not json")
    (tmp_path / ".2026-09-abc.tmp").write_text("{")
    assert store.keys() == ["2026-11"]
    assert store.load("2026-10") is None


def test_store_keeps_24_months_and_every_year(tmp_path):
    assert wrapped_module.KEEP_MONTHS == 24
    store = WrappedStore(str(tmp_path))
    months = [f"{year}-{month:02d}" for year in (2023, 2024, 2025, 2026) for month in range(1, 13)]
    for key in ("2022", "2023", "2024", "2025"):
        store.save(_wrapped(key), [(b"y", "image/jpeg")])
    for key in months:
        store.save(_wrapped(key), [(b"m", "image/jpeg")])

    kept = [key for key in store.keys() if len(key) == 7]
    assert kept == sorted(months, reverse=True)[:24] and kept[-1] == "2025-01"
    assert [key for key in store.keys() if len(key) == 4] == ["2025", "2024", "2023", "2022"]
    # What went took its pictures with it.
    assert not [name for name in os.listdir(tmp_path) if re.match(r"2024-[0-9]{2}", name)]
    assert os.path.exists(tmp_path / "2024-0.jpg")
    assert os.path.exists(tmp_path / "2025-01-0.jpg") and os.path.exists(tmp_path / "2022-0.jpg")


# The watcher

class FakeListening:
    def __init__(self, snapshot=None, now=lambda: 0.0):
        self.snapshot = snapshot or _snapshot()
        self.fail = None
        self.asked = 0
        # When the snapshot was taken, or None for just now, as the test's clock has it.
        self.at = None
        self._now = now

    def require(self):
        self.asked += 1
        if self.fail:
            raise self.fail
        return replace(self.snapshot, at=self._now() if self.at is None else self.at)


class FakeNtfy:
    def __init__(self, phone="the-phone"):
        self.phone = phone
        self.sent = []
        self.works = True
        # Whether it is the user's night, as the settings page sets it.
        self.night = False

    def quiet(self):
        return self.night

    def send(self, topic, message, title="", priority=0, click=""):
        self.sent.append({"topic": topic, "message": message, "title": title, "click": click})
        return self.works


SETTINGS = {"enabled": True, "monthly": True, "yearly_month": 12, "link": "https://nova.example.com"}


class Setup:
    """A watcher over a store in the test's folder, with a clock the test sets."""

    def __init__(self, tmp_path, at=DECEMBER, **settings):
        # Its snapshot, unless a test says otherwise, is Nova's read just after the month's last rebuild.
        self.listening = FakeListening(
            now=lambda: max(self.at, self.at.replace(day=1, hour=0, minute=40)).timestamp())
        self.ntfy = FakeNtfy()
        self.store = WrappedStore(str(tmp_path / "wrapped"))
        self.settings = dict(SETTINGS, **settings)
        self.at = at
        self.watcher = self.new()

    def new(self):
        return WrappedWatcher(self.listening, self.store, self.ntfy, lambda: self.settings,
                              clock=lambda: self.at.timestamp())

    def messages(self):
        return [(sent["message"], sent["click"]) for sent in self.ntfy.sent]


@pytest.fixture
def setup(tmp_path):
    return Setup(tmp_path)


def test_made_when_the_month_is_over_and_the_phone_told(setup):
    setup.watcher.look()
    assert setup.store.keys() == ["2026", "2026-11"]
    assert setup.store.load("2026-11")["kind"] == "month" and setup.store.load("2026")["kind"] == "year"
    assert setup.ntfy.sent == [
        {"topic": "the-phone", "message": "Your November Wrapped is ready.", "title": "Wrapped",
         "click": "https://nova.example.com/wrapped?key=2026-11"},
        {"topic": "the-phone", "message": "Your 2026 Wrapped is ready.", "title": "Wrapped",
         "click": "https://nova.example.com/wrapped?key=2026"}]


def test_the_pictures_are_saved_with_it(setup):
    setup.listening.snapshot = _snapshot({ARTIST_ONE: (b"one", "image/jpeg")})
    setup.watcher.look()
    assert setup.store.picture("2026-11", 0) == (b"one", "image/jpeg")
    assert setup.store.load("2026-11")["top_artists"][0]["picture"] == 0


def test_a_link_with_a_slash_on_the_end_still_makes_one_slash(setup):
    setup.settings["link"] = "https://nova.example.com/"
    setup.watcher.look()
    assert setup.messages()[0][1] == "https://nova.example.com/wrapped?key=2026-11"


def test_nothing_before_five_past_midnight_on_the_1st(setup):
    setup.at = datetime(2026, 12, 1, 0, 4, 59)
    setup.watcher.look()
    assert setup.store.keys() == [] and setup.ntfy.sent == [] and setup.listening.asked == 0
    setup.at = datetime(2026, 12, 1, 0, 5)
    setup.watcher.look()
    assert setup.store.keys() == ["2026", "2026-11"]


def test_made_once_after_restart(setup):
    setup.watcher.look()
    sent = list(setup.ntfy.sent)
    # Nova restarted: a new watcher over the same store, on the same day.
    setup.at = datetime(2026, 12, 1, 9, 0)
    setup.new().look()
    assert setup.ntfy.sent == sent and setup.store.keys() == ["2026", "2026-11"]


def test_made_late_if_missed(setup):
    setup.at = datetime(2026, 12, 9, 12, 0)
    setup.watcher.look()
    assert setup.store.keys() == ["2026", "2026-11"]
    assert len(setup.ntfy.sent) == 2


def test_not_made_in_a_later_month_for_a_month_which_has_gone(setup):
    # On 2 January the month to make is December's, not the November which wasn't made.
    setup.at = datetime(2027, 1, 2, 12, 0)
    setup.watcher.look()
    assert setup.store.keys() == ["2026-12"]


def test_retries_after_failure(setup):
    setup.listening.fail = ListeningUnavailable("SoulSync can't be reached.")
    setup.watcher.look()
    assert setup.store.keys() == [] and setup.ntfy.sent == []
    setup.listening.fail = None
    setup.at = datetime(2026, 12, 1, 1, 10)
    setup.watcher.look()
    assert setup.store.keys() == ["2026", "2026-11"] and len(setup.ntfy.sent) == 2


def test_a_snapshot_from_before_the_months_last_rebuild_isnt_used(setup):
    # SoulSync's last rebuild of the month is at 00:30 at the latest, and Nova reads it by 00:35.
    assert wrapped_module.SNAPSHOT_READY_AFTER == timedelta(minutes=35)
    setup.listening.at = datetime(2026, 11, 28, 12, 0).timestamp()
    for hour, minute in ((0, 10), (3, 0)):
        setup.at = datetime(2026, 12, 1, hour, minute)
        setup.watcher.look()
        assert setup.store.keys() == [] and setup.ntfy.sent == []
    assert setup.listening.asked == 2
    # Once Nova has read SoulSync again, the next look makes it.
    setup.listening.at = datetime(2026, 12, 1, 3, 20).timestamp()
    setup.at = datetime(2026, 12, 1, 4, 0)
    setup.watcher.look()
    assert setup.store.keys() == ["2026", "2026-11"] and len(setup.ntfy.sent) == 2


def test_the_snapshot_must_be_from_after_00_35_on_the_1st(setup):
    setup.listening.at = datetime(2026, 11, 30, 23, 50).timestamp()
    setup.at = datetime(2026, 12, 1, 0, 10)
    setup.watcher.look()
    assert setup.store.keys() == []
    setup.listening.at = datetime(2026, 12, 1, 0, 34, 59).timestamp()
    setup.watcher.look()
    assert setup.store.keys() == []
    setup.listening.at = datetime(2026, 12, 1, 0, 40).timestamp()
    setup.at = datetime(2026, 12, 1, 1, 5)
    setup.watcher.look()
    assert setup.store.keys() == ["2026", "2026-11"]


def test_a_snapshot_with_no_time_isnt_used(setup):
    setup.listening.snapshot = replace(setup.listening.snapshot, at=None)
    setup.listening.require = lambda: setup.listening.snapshot
    setup.watcher.look()
    assert setup.store.keys() == []


def test_made_late_from_a_fresh_snapshot(setup):
    setup.at = datetime(2026, 12, 9, 12, 0)
    setup.listening.at = datetime(2026, 12, 9, 11, 40).timestamp()
    setup.watcher.look()
    assert setup.store.keys() == ["2026", "2026-11"]


def test_a_failure_to_make_is_tried_again_and_never_ends_the_look(setup, monkeypatch):
    calls = []

    def broken(*args):
        calls.append(args)
        raise KeyError("odd shape")

    monkeypatch.setattr(wrapped_module, "make_with_pictures", broken)
    setup.watcher.look()
    assert setup.store.keys() == [] and len(calls) == 2


def test_nothing_made_when_there_were_no_plays(setup):
    setup.listening.snapshot.windows["30d"]["overview"]["total_plays"] = 0
    setup.listening.snapshot.year["totals"]["plays"] = 0
    setup.watcher.look()
    assert setup.store.keys() == [] and setup.ntfy.sent == []


def test_not_made_when_off(tmp_path):
    off = Setup(tmp_path, enabled=False)
    off.watcher.look()
    assert off.store.keys() == [] and off.ntfy.sent == [] and off.listening.asked == 0

    # Monthly off skips the months only.
    no_months = Setup(tmp_path / "b", monthly=False)
    no_months.watcher.look()
    assert no_months.store.keys() == ["2026"]
    assert no_months.messages() == [("Your 2026 Wrapped is ready.", "https://nova.example.com/wrapped?key=2026")]


def test_a_change_of_settings_is_used_at_the_next_look(setup):
    setup.settings["enabled"] = False
    setup.watcher.look()
    assert setup.store.keys() == []
    setup.settings["enabled"] = True
    setup.watcher.look()
    assert setup.store.keys() == ["2026", "2026-11"]


def test_year_only_in_yearly_month(tmp_path):
    november = Setup(tmp_path, at=datetime(2026, 11, 1, 0, 10))
    november.watcher.look()
    assert november.store.keys() == ["2026-10"]

    # Moved to November on the settings page, the year is made then.
    moved = Setup(tmp_path / "b", at=datetime(2026, 11, 1, 0, 10), yearly_month=11)
    moved.watcher.look()
    assert moved.store.keys() == ["2026", "2026-10"]

    # In January the year is the one which has just ended.
    january = Setup(tmp_path / "c", at=datetime(2027, 1, 1, 0, 10), yearly_month=1)
    january.watcher.look()
    assert january.store.keys() == ["2026", "2026-12"]


def test_a_bad_yearly_month_makes_no_year_and_still_makes_the_month(tmp_path):
    odd = Setup(tmp_path, yearly_month="soon")
    odd.watcher.look()
    assert odd.store.keys() == ["2026-11"]


def test_with_no_phone_topic_it_is_made_and_nothing_is_sent(setup):
    setup.ntfy.phone = ""
    setup.watcher.look()
    assert setup.store.keys() == ["2026", "2026-11"] and setup.ntfy.sent == []


def test_with_no_ntfy_it_is_made(tmp_path):
    made = Setup(tmp_path)
    made.watcher = WrappedWatcher(made.listening, made.store, None, lambda: made.settings,
                                  clock=lambda: made.at.timestamp())
    made.watcher.look()
    assert made.store.keys() == ["2026", "2026-11"]


def test_no_link_no_click(setup):
    setup.settings["link"] = ""
    setup.watcher.look()
    assert setup.messages() == [("Your November Wrapped is ready.", ""), ("Your 2026 Wrapped is ready.", "")]


def test_a_notification_which_failed_is_sent_again_at_the_next_look_and_after_a_restart(setup):
    setup.ntfy.works = False
    setup.watcher.look()
    assert len(setup.ntfy.sent) == 2
    # Still untold, as the file says, so a restart doesn't lose it.
    assert setup.store.load("2026-11")["told"] is False
    setup.ntfy.works = True
    setup.at = datetime(2026, 12, 1, 1, 10)
    setup.new().look()
    assert len(setup.ntfy.sent) == 4 and setup.ntfy.sent[2:] == setup.ntfy.sent[:2]
    assert setup.store.load("2026-11")["told"] is True and setup.store.load("2026")["told"] is True
    setup.watcher.look()
    setup.new().look()
    assert len(setup.ntfy.sent) == 4


def test_made_in_quiet_hours_is_told_at_the_first_look_outside_them(setup):
    setup.ntfy.night = True
    setup.at = datetime(2026, 12, 1, 0, 50)
    setup.watcher.look()
    assert setup.store.keys() == ["2026", "2026-11"] and setup.ntfy.sent == []
    assert setup.store.load("2026-11")["told"] is False
    setup.at = datetime(2026, 12, 1, 3, 5)
    setup.watcher.look()
    assert setup.ntfy.sent == []
    setup.ntfy.night = False
    setup.at = datetime(2026, 12, 1, 8, 0)
    setup.watcher.look()
    assert [message for message, _ in setup.messages()] == ["Your November Wrapped is ready.",
                                                            "Your 2026 Wrapped is ready."]
    setup.at = datetime(2026, 12, 1, 9, 0)
    setup.watcher.look()
    assert len(setup.ntfy.sent) == 2


def test_a_restart_between_making_it_and_telling_still_tells_once(setup):
    setup.ntfy.night = True
    setup.at = datetime(2026, 12, 1, 0, 50)
    setup.watcher.look()
    setup.ntfy.night = False
    setup.at = datetime(2026, 12, 1, 8, 0)
    restarted = setup.new()
    restarted.look()
    assert len(setup.ntfy.sent) == 2
    setup.new().look()
    assert len(setup.ntfy.sent) == 2


def test_an_untold_one_from_an_earlier_month_is_left_alone(setup):
    setup.store.save({"key": "2026-10", "kind": "month", "plays": 1, "told": False}, [])
    # And one this month's from before there was a told field is taken as told.
    setup.store.save({"key": "2026-11", "kind": "month", "plays": 1}, [])
    setup.at = datetime(2026, 12, 9, 12, 0)
    setup.watcher.look()
    assert [message for message, _ in setup.messages()] == ["Your 2026 Wrapped is ready."]
    assert setup.store.load("2026-10")["told"] is False


def test_what_the_store_says_of_being_told_is_kept_with_the_rest(setup):
    setup.store.save({"key": "2026-11", "kind": "month", "plays": 7, "told": False}, [(b"x", "image/png")])
    setup.store.mark_told("2026-11")
    assert setup.store.load("2026-11") == {"key": "2026-11", "kind": "month", "plays": 7, "told": True}
    assert setup.store.picture("2026-11", 0) == (b"x", "image/png")
    # One which isn't there, or isn't a key, is left alone.
    setup.store.mark_told("2026-10")
    setup.store.mark_told("../x")
    assert setup.store.keys() == ["2026-11"]


def test_the_thread_looks_at_once_and_stops(setup):
    looked = threading.Event()
    setup.watcher.look = looked.set
    setup.watcher.start()
    try:
        assert looked.wait(5)
        assert [t for t in threading.enumerate() if t.name == "wrapped" and t.daemon]
    finally:
        setup.watcher.stop()
    assert not [t for t in threading.enumerate() if t.name == "wrapped"]
    assert wrapped_module.LOOK_EVERY == 3600


def test_a_look_which_fails_doesnt_end_the_thread(setup, monkeypatch):
    looks = []

    def look():
        looks.append(1)
        raise RuntimeError("boom")

    setup.watcher.look = look
    monkeypatch.setattr(wrapped_module, "LOOK_EVERY", 0.01)
    setup.watcher.start()
    try:
        give_up = time.monotonic() + 5
        while len(looks) < 3 and time.monotonic() < give_up:
            time.sleep(0.01)
        assert len(looks) >= 3
    finally:
        setup.watcher.stop()
