"""
Following Test
Checks the watcher which follows the user's top artists on SoulSync's watchlist once a month, with a fake in
place of SoulSync - keeping a library and a watchlist as its own routes do, in the shapes the live check of 16
November recorded - and a fake of the listening stats. Every name and id is made up.
"""

import copy
import json
import threading
from datetime import datetime
from urllib.parse import unquote

import pytest
import requests

from voice_assistant import network
from voice_assistant.following import FollowWatcher
from voice_assistant.listening import ListeningUnavailable, Snapshot
from voice_assistant.soulsync import SoulSync

URL = "http://soulsync.local:8008"
WATCHLIST = f"{URL}/api/watchlist/artists"
LIBRARY = f"{URL}/api/v1/library/artists/"
ADD = f"{URL}/api/watchlist/add"
REMOVE = f"{URL}/api/watchlist/remove"
LINK = f"{URL}/api/watchlist/artist/"

# The watchlist's column for each provider, as SoulSync names them.
COLUMNS = {"spotify": "spotify_artist_id", "itunes": "itunes_artist_id", "deezer": "deezer_artist_id",
           "discogs": "discogs_artist_id", "musicbrainz": "musicbrainz_artist_id"}

# A Monday in the middle of November, just after 3 in the morning, as Nova's clock has it.
NOVEMBER = datetime(2026, 11, 16, 3, 0)


def _name(n):
    return f"Made Up Artist {n}"


def _library_id(n):
    """A library artist's id as the stats give it: 22 letters and digits, never only digits."""
    return f"lib{n:02d}AbCdEfGhIjKlMnOpQr"


def _library_artist(n):
    """
    A library artist as GET /api/v1/library/artists/<id> gives it: with no Spotify id, as SoulSync has
    no Spotify linked, and the four others, as 18 of the 19 found on 16 November had.
    """
    return {"id": _library_id(n), "name": _name(n), "spotify_artist_id": None, "itunes_artist_id": f"{n}001",
            "deezer_id": f"{n}002", "discogs_id": f"{n}003", "musicbrainz_id": f"mb-{n}", "amazon_id": None}


def _top(n, plays=100):
    """A top artist as the 12-month stats list one found in the library."""
    return {"id": _library_id(n), "name": _name(n), "play_count": plays, "image_url": None, "soul_id": f"soul-{n}",
            "global_listeners": None, "global_playcount": None}


def _row(row_id, name, **ids):
    """A watchlist row as GET /api/watchlist/artists gives it."""
    row = {"id": row_id, "artist_name": name, "amazon_artist_id": None, "image_url": None, "date_added": None}
    row.update({column: None for column in COLUMNS.values()})
    row.update(ids)
    return row


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body

    def json(self):
        return copy.deepcopy(self.body)


class FakeSoulSync:
    """
    Stands in for the requests.Session SoulSync is asked through, keeping a library and a watchlist as
    SoulSync's own routes do: an add files the id under its source's column, a link adds another
    provider's id to the row with the first, and a remove takes every row holding the id.
    """

    def __init__(self):
        self.headers = {}
        self.library = {}
        self.watchlist = []
        # Each request as (method, address, body).
        self.asked = []
        self.down = False
        # A provider id SoulSync files under another name than the one sent, as an id read as someone else's would be.
        self.stored_as = {}
        # Provider ids whose add SoulSync answers with an error.
        self.refused = set()
        self.remove_refused = False
        self._next_row = 100

    def _answer(self, method, url, body):
        self.asked.append((method, url, copy.deepcopy(body)))
        if self.down:
            raise requests.ConnectionError(f"{url}?key=the-key")

    def get(self, url, params=None, timeout=None, stream=False, allow_redirects=True):
        self._answer("GET", url, None)
        if url == WATCHLIST:
            return FakeResponse(200, {"success": True, "artists": self.watchlist})
        if url.startswith(LIBRARY):
            artist = self.library.get(unquote(url[len(LIBRARY):]))
            if artist is None:
                return FakeResponse(404, {"success": False, "data": None, "pagination": None,
                                          "error": {"code": "NOT_FOUND", "message": "Artist not found."}})
            return FakeResponse(200, {"success": True, "data": {"artist": artist, "albums": []}, "error": None,
                                      "pagination": None})
        return FakeResponse(404, {"error": "not found"})

    def post(self, url, json=None, timeout=None):
        self._answer("POST", url, json)
        if url == ADD:
            if json["artist_id"] in self.refused:
                return FakeResponse(500, {"success": False, "error": "Failed to add artist to watchlist"})
            self._next_row += 1
            self.watchlist.append(_row(self._next_row, self.stored_as.get(json["artist_id"], json["artist_name"]),
                                       **{COLUMNS[json["source"]]: json["artist_id"]}))
            return FakeResponse(200, {"success": True, "message": "Added to watchlist"})
        if url.startswith(LINK) and url.endswith("/link-provider"):
            holding = unquote(url[len(LINK):-len("/link-provider")])
            row = next(row for row in self.watchlist if holding in [row[column] for column in COLUMNS.values()])
            row[COLUMNS[json["provider"]]] = json["provider_id"]
            return FakeResponse(200, {"success": True, "message": "Linked"})
        if url == REMOVE:
            if self.remove_refused:
                return FakeResponse(500, {"success": False, "error": "Failed to remove artist from watchlist"})
            self.watchlist = [row for row in self.watchlist
                              if json["artist_id"] not in [row[column] for column in COLUMNS.values()]]
            return FakeResponse(200, {"success": True, "message": "Removed artist from watchlist"})
        return FakeResponse(404, {"error": "not found"})

    def posted(self, url=None):
        return [body for method, asked, body in self.asked if method == "POST" and (url is None or asked == url)]


class FakeListening:
    def __init__(self, top):
        self.top = top
        self.fail = None

    def require(self):
        if self.fail:
            raise self.fail
        return Snapshot(windows={"12m": {"overview": {}, "top_artists": copy.deepcopy(self.top)}}, at=1.0)


class Setup:
    """A watcher over the test's folder, with SoulSync's library holding artists 1 to 6, and a clock the test sets."""

    def __init__(self, tmp_path, http, top=None, **settings):
        self.http = http
        self.http.library = {_library_id(n): _library_artist(n) for n in range(1, 7)}
        self.listening = FakeListening(top if top is not None else [_top(n, 100 - n) for n in range(1, 6)])
        self.settings = {"follow_top_artists": 25, "playlist": "Discovery Shuffle", **settings}
        self.set_up = True
        self.at = NOVEMBER
        self.path = tmp_path / "following.json"
        self.watcher = self.new()

    def new(self):
        return FollowWatcher(self.listening, lambda: SoulSync(URL, "the-key") if self.set_up else None,
                             str(self.path), lambda: self.settings, clock=lambda: self.at.timestamp())

    def record(self):
        return json.loads(self.path.read_text()) if self.path.exists() else None

    def watched(self):
        return sorted(row["artist_name"] for row in self.http.watchlist)


@pytest.fixture
def http(monkeypatch):
    fake = FakeSoulSync()
    monkeypatch.setattr(requests, "Session", lambda: fake)
    monkeypatch.setattr(network, "online", lambda: True)
    return fake


@pytest.fixture
def setup(tmp_path, http):
    return Setup(tmp_path, http)


def test_follows_top_n_once(setup):
    setup.settings["follow_top_artists"] = 3
    setup.watcher.look()

    # The top three of the 12 months, each by SoulSync's own name for the library artist, and by the first of its
    # provider ids in SoulSync's own order with its source named, as a bare id would be read as another provider's.
    assert setup.watched() == [_name(1), _name(2), _name(3)]
    assert setup.http.posted(ADD) == [{"artist_id": f"{n}001", "artist_name": _name(n), "source": "itunes"}
                                      for n in (1, 2, 3)]
    # Its other ids are linked to the same row, so SoulSync's scan never has to find the artist by name alone.
    row = setup.http.watchlist[0]
    assert (row["itunes_artist_id"], row["deezer_artist_id"], row["discogs_artist_id"],
            row["musicbrainz_artist_id"], row["spotify_artist_id"]) == ("1001", "1002", "1003", "mb-1", None)
    assert setup.record()["month"] == "2026-11"

    # Done for the month: a look an hour later asks SoulSync nothing at all.
    asked = len(setup.http.asked)
    setup.at = NOVEMBER.replace(hour=4)
    setup.watcher.look()
    assert len(setup.http.asked) == asked


def test_already_watched_left_alone(setup):
    # One watched by the same name in other capitals, and one under another name but holding a top artist's id.
    by_name = _row(1, _name(1).upper(), spotify_artist_id="sp-made-up")
    by_id = _row(2, "Another Name Entirely", deezer_artist_id="2002")
    setup.http.watchlist = [copy.deepcopy(by_name), copy.deepcopy(by_id)]
    setup.watcher.look()

    assert setup.http.watchlist[:2] == [by_name, by_id]
    # Neither added again nor given another id: only the others are asked for.
    assert [body["artist_name"] for body in setup.http.posted(ADD)] == [_name(3), _name(4), _name(5)]
    assert all(f"{LINK}sp-made-up" not in url and f"{LINK}2002" not in url for _, url, _ in setup.http.asked)
    assert setup.record()["month"] == "2026-11"


def test_wrong_artist_is_removed(setup, caplog):
    # SoulSync files artist 2's id under someone else's name: the add is undone.
    setup.http.stored_as["2001"] = "Someone Else Entirely"
    with caplog.at_level("INFO", logger="voice_assistant.following"):
        setup.watcher.look()

    assert setup.watched() == [_name(1), _name(3), _name(4), _name(5)]
    assert setup.http.posted(REMOVE) == [{"artist_id": "2001"}]
    assert setup.record() == {"month": "2026-11", "undo": []}
    assert "1 undone" in caplog.text and "Someone Else" not in caplog.text


def test_a_wrong_artist_not_removed_is_removed_at_the_next_look(setup):
    setup.http.stored_as["2001"] = "Someone Else Entirely"
    setup.http.remove_refused = True
    setup.watcher.look()
    # Kept to undo, so it isn't left in whatever restarts in between.
    assert "Someone Else Entirely" in setup.watched()
    assert setup.record()["undo"] == [{"itunes_artist_id": "2001", "deezer_artist_id": "2002",
                                       "discogs_artist_id": "2003", "musicbrainz_artist_id": "mb-2"}]

    setup.http.remove_refused = False
    setup.at = NOVEMBER.replace(hour=4)
    setup.new().look()
    assert "Someone Else Entirely" not in setup.watched()
    assert setup.record() == {"month": "2026-11", "undo": []}


def test_retries_until_done(setup):
    # SoulSync down: nothing is recorded, and the next look tries again.
    setup.http.down = True
    setup.watcher.look()
    assert setup.record() is None

    # Then the stats not read yet, after a restart.
    setup.http.down = False
    setup.listening.fail = ListeningUnavailable("I haven't got your listening stats from SoulSync yet.")
    setup.at = NOVEMBER.replace(hour=4)
    setup.watcher.look()
    assert setup.record() is None and setup.watched() == []

    setup.listening.fail = None
    setup.at = NOVEMBER.replace(hour=5)
    setup.watcher.look()
    assert setup.watched() == [_name(n) for n in range(1, 6)]
    assert setup.record()["month"] == "2026-11"


def test_soulsync_going_down_part_way_is_carried_on_from_at_the_next_look(setup):
    adds = []

    def down_after_two(url, json=None, timeout=None):
        if url == ADD:
            adds.append(json)
            setup.http.down = len(adds) > 2
        return post(url, json, timeout)

    post, setup.http.post = setup.http.post, down_after_two
    setup.watcher.look()
    assert setup.watched() == [_name(1), _name(2)] and setup.record() is None

    setup.http.post, setup.http.down = post, False
    setup.at = NOVEMBER.replace(hour=4)
    setup.watcher.look()
    # The two already followed are left alone, and the rest followed.
    assert setup.watched() == [_name(n) for n in range(1, 6)]
    # The third's add, which SoulSync never answered, then the three not yet followed.
    assert [body["artist_name"] for body in setup.http.posted(ADD)][3:] == [_name(n) for n in range(3, 6)]
    assert setup.record()["month"] == "2026-11"


def test_soulsync_not_set_up_records_nothing(setup):
    setup.set_up = False
    setup.watcher.look()
    assert setup.record() is None and setup.http.asked == []


def test_once_a_month(setup):
    setup.watcher.look()
    asked = len(setup.http.asked)

    # A restart later the same month does nothing.
    setup.at = datetime(2026, 11, 30, 23, 0)
    setup.new().look()
    assert len(setup.http.asked) == asked

    # December's first look after 3 runs again, following whoever has joined the top.
    setup.listening.top = [_top(n) for n in range(1, 7)]
    setup.at = datetime(2026, 12, 1, 3, 5)
    setup.new().look()
    assert [body["artist_name"] for body in setup.http.posted(ADD)][-1] == _name(6)
    assert setup.record()["month"] == "2026-12"


def test_not_before_three(setup):
    # On any day, a look before 3 in the morning leaves it for a later one.
    for at in (datetime(2026, 12, 1, 0, 30), datetime(2026, 11, 16, 2, 59)):
        setup.at = at
        setup.watcher.look()
        assert setup.http.asked == [] and setup.record() is None
    setup.at = datetime(2026, 11, 16, 3, 0)
    setup.watcher.look()
    assert setup.record()["month"] == "2026-11"


def test_zero_switches_off(setup):
    setup.settings["follow_top_artists"] = 0
    setup.watcher.look()
    assert setup.http.asked == [] and setup.record() is None


def test_an_artist_not_in_the_library_or_naming_someone_else_now_isnt_followed(setup):
    # Six of the 25 on 16 November had no library artist of their name, so no id; and an id the library now
    # gives to another artist isn't the one listened to.
    setup.http.library[_library_id(3)] = dict(_library_artist(3), name="Someone Else Entirely")
    no_id = {"name": "Made Up Duo", "play_count": 90}
    setup.listening.top = [_top(1), no_id, _top(3), _top(4)]
    setup.watcher.look()
    assert setup.watched() == [_name(1), _name(4)]
    assert setup.record()["month"] == "2026-11"


def test_one_add_soulsync_refuses_leaves_the_rest_followed(setup):
    setup.http.refused.add("2001")
    setup.watcher.look()
    assert setup.watched() == [_name(1), _name(3), _name(4), _name(5)]
    assert setup.record()["month"] == "2026-11"


def test_logs_no_names(setup, caplog):
    setup.http.watchlist = [_row(1, _name(1))]
    setup.http.stored_as["2001"] = "Someone Else Entirely"
    setup.listening.top = [_top(1), _top(2), _top(3), {"name": "Made Up Duo", "play_count": 90}]
    with caplog.at_level("DEBUG"):
        setup.watcher.look()
    # Counts only: his listening is never logged, nor the key, nor SoulSync's own words.
    assert "Followed 1 of your top 4 artists" in caplog.text
    for secret in ("Made Up", "Someone Else", "the-key", "lib0", "Failed to"):
        assert secret not in caplog.text


def test_the_thread_looks_at_once_and_stops(setup):
    looked = threading.Event()
    setup.watcher.look = looked.set
    setup.watcher.start()
    try:
        assert looked.wait(5)
        assert any(thread.name == "following" and thread.is_alive() for thread in threading.enumerate())
    finally:
        setup.watcher.stop()
    assert setup.watcher._thread is None
    assert not any(thread.name == "following" for thread in threading.enumerate())
