"""
Listening Test
Checks how Nova reads SoulSync's listening stats and keeps them as a snapshot, with a fake
in place of SoulSync - answering in the shapes recorded live on 15 November (see
tests/fixtures, with made-up names) - so nothing is ever sent to the real one.
"""

import copy
import json
import os
import threading
import time

import pytest
import requests

from voice_assistant import network
from voice_assistant.listening import (NOT_YET, REFRESH_EVERY, SHAPE_CHANGED, WINDOW_WORDS, WINDOWS, Listening,
                                       ListeningUnavailable)
from voice_assistant.soulsync import NOT_ANSWERING

URL = "http://soulsync.local:8008"
CACHED = f"{URL}/api/stats/cached"
YEAR = f"{URL}/api/stats/year"
DISK = f"{URL}/api/stats/library-disk-usage"
COVER = b"\xff\xd8made-up-jpeg"


def _fixture(name):
    with open(os.path.join(os.path.dirname(__file__), "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


class FakeResponse:
    def __init__(self, status_code, body, headers=None, content=b""):
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}
        self.content = content

    def json(self):
        if self.body is None:
            raise ValueError("not JSON")
        return copy.deepcopy(self.body)

    def iter_content(self, chunk_size):
        yield self.content

    def close(self):
        pass


class FakeSoulSync:
    """Stands in for the requests.Session: each stats address answers from the fixtures, a cached one by range."""

    def __init__(self):
        self.headers = {}
        # Every GET as (address, params).
        self.fetched = []
        cached = _fixture("soulsync_stats_cached.json")
        self.cached = {window: copy.deepcopy(cached) for window in WINDOWS.values()}
        self.cached["30d"]["overview"]["total_plays"] = 480
        self.year = _fixture("soulsync_stats_year.json")
        self.disk = _fixture("soulsync_disk_usage.json")
        # An exception every request raises, or an answer to every one, instead of its own.
        self.fail = None
        self.answer = None

    def get(self, url, params=None, timeout=None, stream=False, allow_redirects=True):
        if self.fail:
            raise self.fail
        self.fetched.append((url, params))
        if self.answer:
            return FakeResponse(*self.answer)
        # Answered by its path alone, so that an address moved on the settings page is answered too.
        path = "/" + url.split("//", 1)[1].split("/", 1)[1]
        if path.startswith("/api/navidrome/cover/"):
            return FakeResponse(200, None, {"Content-Type": "image/jpeg"}, COVER)
        if path == "/api/stats/cached":
            return FakeResponse(200, self.cached[params["range"]])
        return FakeResponse(200, {"/api/stats/year": self.year, "/api/stats/library-disk-usage": self.disk}[path])

    def covers(self):
        return [url for url, _ in self.fetched if "/api/navidrome/cover/" in url]


class FakeConfig:
    def __init__(self, url=URL):
        self.url = url

    def section(self, name):
        return {"url": self.url} if name == "soulsync" else {}


@pytest.fixture
def http(monkeypatch):
    fake = FakeSoulSync()
    monkeypatch.setattr(requests, "Session", lambda: fake)
    monkeypatch.setattr(network, "online", lambda: True)
    return fake


@pytest.fixture
def config():
    return FakeConfig()


@pytest.fixture
def listening(http, config):
    return Listening(config, "the-key", clock=lambda: 1000.0)


def test_windows_are_soulsyncs_ranges_in_words():
    assert WINDOWS == {"week": "7d", "month": "30d", "year": "12m", "all": "all"}
    assert WINDOW_WORDS == {"7d": "the last 7 days", "30d": "the last 30 days", "12m": "the last 12 months",
                            "all": "all time"}


def test_refresh_reads_all_windows(listening, http):
    listening.refresh()
    assert [params for url, params in http.fetched if url == CACHED] == [
        {"range": "7d"}, {"range": "30d"}, {"range": "12m"}, {"range": "all"}]
    assert [url for url, _ in http.fetched if url != CACHED and "/cover/" not in url] == [YEAR, DISK]
    snapshot = listening.snapshot()
    assert set(snapshot.windows) == {"7d", "30d", "12m", "all"}
    assert snapshot.windows["30d"]["overview"]["total_plays"] == 480
    assert snapshot.windows["7d"]["overview"]["total_plays"] == 120
    assert snapshot.year["totals"]["plays"] == 900
    assert snapshot.health["total_tracks"] == 800
    assert snapshot.disk["total_bytes"] == 9900000000
    assert snapshot.at == 1000.0
    assert snapshot.problem is None
    assert listening.require() is snapshot


def test_the_windows_keep_what_soulsync_gives_each(listening):
    listening.refresh()
    window = listening.snapshot().windows["7d"]
    for key in ("overview", "previous", "top_artists", "top_albums", "top_tracks", "timeline", "genres", "clock",
                "rhythm"):
        assert key in window


def test_the_key_goes_with_every_request(listening, http):
    listening.refresh()
    assert http.headers == {"Authorization": "Bearer the-key"}


def test_pictures_are_the_top_five_artists_and_discoveries(listening, http):
    listening.refresh()
    snapshot = listening.snapshot()
    # The cached windows' top five (of six listed), the year's top five, and its five discoveries (of six).
    expected = {f"/api/navidrome/cover/artist-{name}" for name in ("one", "two", "three", "four", "five")}
    expected |= {f"/api/navidrome/cover/artist-{name}" for name in ("seven", "eight", "nine", "ten", "eleven")}
    assert set(snapshot.pictures) == expected
    assert snapshot.pictures["/api/navidrome/cover/artist-one"] == (COVER, "image/jpeg")


def test_pictures_fetched_once_per_url(listening, http):
    # Every window has the same six artists, and the year shares five of them.
    listening.refresh()
    covers = http.covers()
    assert len(covers) == len(set(covers)) == 10


def test_a_picture_which_cant_be_fetched_is_left_out_not_a_failure(listening, http):
    original = http.get

    def get(url, **kwargs):
        if url.endswith("artist-one"):
            return FakeResponse(404, None)
        return original(url, **kwargs)

    http.get = get
    listening.refresh()
    snapshot = listening.snapshot()
    assert "/api/navidrome/cover/artist-one" not in snapshot.pictures
    assert "/api/navidrome/cover/artist-two" in snapshot.pictures
    assert snapshot.problem is None


def test_a_missing_picture_address_is_passed_over(listening, http):
    for window in http.cached.values():
        window["top_artists"][0]["image_url"] = ""
        del window["top_artists"][1]["image_url"]
    http.year["top_artists"][0]["image_url"] = None
    listening.refresh()
    assert listening.snapshot().problem is None


def test_refresh_keeps_last_good(listening, http):
    listening.refresh()
    good = listening.snapshot()
    http.fail = requests.ConnectionError("down")
    listening.refresh()
    kept = listening.snapshot()
    assert kept.problem == "SoulSync isn't answering." == NOT_ANSWERING
    assert kept.windows == good.windows and kept.year == good.year and kept.pictures == good.pictures
    assert kept.at == good.at
    # Still there to answer from, with the problem to say if it's wanted.
    assert listening.require().windows == good.windows
    # And a good refresh clears it.
    http.fail = None
    listening.refresh()
    assert listening.snapshot().problem is None


def test_a_failure_part_way_leaves_the_old_snapshot_whole(listening, http):
    listening.refresh()
    http.cached["7d"]["overview"]["total_plays"] = 999
    http.disk = {"success": False, "error": "nope"}
    listening.refresh()
    snapshot = listening.snapshot()
    assert snapshot.windows["7d"]["overview"]["total_plays"] == 120
    assert snapshot.problem == "SoulSync said: nope."


def test_refresh_logs_one_line_for_a_failure_without_any_names(listening, http, caplog):
    http.fail = requests.ConnectionError("http://soulsync.local:8008/?key=the-key")
    listening.refresh()
    warnings = [r for r in caplog.records if r.name == "voice_assistant.listening"]
    assert len(warnings) == 1
    assert "the-key" not in caplog.text


def test_soulsyncs_own_words_are_said_but_never_logged(listening, http, caplog):
    # Its error text is free text, which a :latest SoulSync can fill with anything, so only the kind is logged.
    http.disk = {"success": False, "error": "Artist One the-key"}
    listening.refresh()
    assert listening.snapshot().problem == "SoulSync said: Artist One the-key."
    assert "Artist One" not in caplog.text and "the-key" not in caplog.text
    assert [r.name for r in caplog.records if r.levelname == "WARNING"] == ["voice_assistant.listening"]


@pytest.mark.parametrize("break_it", [
    lambda http: http.cached["7d"].pop("overview"),
    lambda http: http.cached["all"].pop("top_artists"),
    lambda http: http.year.pop("totals"),
], ids=["no overview", "no top artists", "no totals"])
def test_unknown_shape_is_unavailable(listening, http, break_it):
    break_it(http)
    listening.refresh()
    with pytest.raises(ListeningUnavailable) as unavailable:
        listening.require()
    assert str(unavailable.value) == "SoulSync's stats have changed shape, so Nova can't read them." == SHAPE_CHANGED


def test_an_old_snapshot_survives_a_change_of_shape(listening, http):
    listening.refresh()
    http.year.pop("totals")
    listening.refresh()
    assert listening.snapshot().problem == SHAPE_CHANGED
    assert listening.require().year["totals"]["plays"] == 900


def test_require_before_any_refresh_says_not_yet(listening):
    with pytest.raises(ListeningUnavailable) as unavailable:
        listening.require()
    assert str(unavailable.value) == NOT_YET == "I haven't got your listening stats from SoulSync yet."
    snapshot = listening.snapshot()
    assert snapshot.at is None and snapshot.windows == {}


def test_require_says_why_when_no_refresh_has_worked(listening, http):
    http.answer = (401, {"error": "no"})
    listening.refresh()
    with pytest.raises(ListeningUnavailable) as unavailable:
        listening.require()
    assert "key" in str(unavailable.value)


def test_not_set_up_is_said_not_raised(http, config):
    config.url = ""
    listening = Listening(config, "the-key")
    listening.refresh()
    with pytest.raises(ListeningUnavailable) as unavailable:
        listening.require()
    assert "isn't set up" in str(unavailable.value)
    assert http.fetched == []


def test_new_address_used(listening, http, config):
    listening.refresh()
    assert all(url.startswith(URL) for url, _ in http.fetched)
    http.fetched.clear()
    # Changed on the settings page between two refreshes: the new one is asked at once.
    config.url = "http://moved.local:8080"
    listening.refresh()
    assert http.fetched and all(url.startswith("http://moved.local:8080/") for url, _ in http.fetched)
    assert listening.snapshot().problem is None


def test_a_bug_in_a_refresh_is_logged_not_raised(listening, monkeypatch, caplog):
    monkeypatch.setattr(listening, "_read_everything", lambda soulsync: 1 / 0)
    listening.refresh()
    assert "ZeroDivisionError" in caplog.text
    assert listening.snapshot().problem


def test_the_thread_refreshes_at_once_then_every_half_hour(listening, monkeypatch):
    assert REFRESH_EVERY == 30 * 60
    waits = []
    refreshed = threading.Event()
    listening.refresh = refreshed.set
    real_wait = listening._stopping.wait
    monkeypatch.setattr(listening._stopping, "wait", lambda seconds: waits.append(seconds) or real_wait(0.05))
    listening.start()
    assert refreshed.wait(5)
    deadline = time.time() + 5
    while not waits and time.time() < deadline:
        time.sleep(0.01)
    listening.stop()
    assert waits and waits[0] == REFRESH_EVERY
    names = [thread.name for thread in threading.enumerate()]
    assert "listening" not in names
