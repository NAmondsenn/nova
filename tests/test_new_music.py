"""
New Music Test
Checks the watcher which keeps Nova's own new-music playlist in Navidrome, with a fake in place of
Navidrome's Subsonic API keeping albums and playlists as Navidrome does: createPlaylist with a playlist's
id replaces its songs, updatePlaylist adds songs and sets it public, and each playlist has the user who
made it as its owner. Every name and id is made up. The clock is set in local time, as Nova's is, so the
same tests hold in London and in UTC.
"""

import copy
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
import requests

from voice_assistant import network
from voice_assistant.navidrome import NavidromeLibrary
from voice_assistant.new_music import NewMusicPlaylist

# A Monday in the middle of November at 6 in the morning, as Nova's clock has it.
MORNING = datetime(2026, 11, 16, 6, 0)

# The parameters which sign each request in, left out of what the fake notes.
SIGN_IN = {"u", "t", "s", "v", "c", "f"}


def _created(when: datetime) -> str:
    """A time as Navidrome writes it: UTC, with nine digits of fractional seconds and a Z."""
    return datetime.fromtimestamp(when.timestamp(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def json(self):
        return copy.deepcopy(self.body)

    def raise_for_status(self):
        pass


class FakeNavidrome:
    """Stands in for the requests.Session Navidrome is asked through, keeping its albums and playlists."""

    def __init__(self):
        self.headers = {}
        # Each album: id, name, artist, created, and its songs, each an id and a created time.
        self.albums = []
        self.playlists = {}
        # Each request as (endpoint, params without the sign-in).
        self.asked = []
        self.down = False
        self._next = 0

    def album(self, n: int, arrived: datetime, songs: int = 3):
        self.albums.append({"id": f"al{n}", "name": f"Album {n}", "artist": f"Band {n}", "created": _created(arrived),
                            "songCount": songs,
                            "song": [{"id": f"al{n}-s{k}", "title": f"Song {k}", "created": _created(arrived)}
                                     for k in range(1, songs + 1)]})

    def playlist(self, name, owner="nova", songs=(), public=False):
        self._next += 1
        self.playlists[f"pl{self._next}"] = {"name": name, "owner": owner, "songs": list(songs), "public": public}
        return f"pl{self._next}"

    def get(self, url, params=None, timeout=None, stream=False):
        endpoint = url.rsplit("/rest/", 1)[1]
        params = dict(params or {})
        user = params.get("u")
        self.asked.append((endpoint, {key: value for key, value in params.items() if key not in SIGN_IN}))
        if self.down:
            raise requests.ConnectionError(f"{url}?t=secret-token")
        body = getattr(self, f"_{endpoint}")(params, user)
        return FakeResponse({"subsonic-response": {"status": "ok", **body}})

    def _getAlbumList2(self, params, user):
        newest = sorted(self.albums, key=lambda album: album["created"], reverse=True)
        start = int(params["offset"])
        return {"albumList2": {"album": [{key: value for key, value in album.items() if key != "song"}
                                         for album in newest[start:start + int(params["size"])]]}}

    def _getAlbum(self, params, user):
        return {"album": next(album for album in self.albums if album["id"] == params["id"])}

    def _getPlaylists(self, params, user):
        return {"playlists": {"playlist": [
            {"id": pid, "name": found["name"], "owner": found["owner"], "public": found["public"],
             "songCount": len(found["songs"])}
            for pid, found in self.playlists.items() if found["owner"] == user or found["public"]]}}

    def _createPlaylist(self, params, user):
        songs = params.get("songId") or []
        songs = list(songs) if isinstance(songs, list) else [songs]
        pid = params.get("playlistId")
        if pid:
            assert self.playlists[pid]["owner"] == user, "Nova replaced another user's playlist"
            self.playlists[pid]["songs"] = songs
        else:
            pid = self.playlist(params["name"], owner=user, songs=songs)
        return {"playlist": {"id": pid, "name": self.playlists[pid]["name"]}}

    def _updatePlaylist(self, params, user):
        found = self.playlists[params["playlistId"]]
        assert found["owner"] == user, "Nova changed another user's playlist"
        found["songs"] += list(params.get("songIdToAdd") or [])
        if "public" in params:
            found["public"] = params["public"] == "true"
        return {}

    def writes(self):
        return [(endpoint, params) for endpoint, params in self.asked if endpoint in ("createPlaylist", "updatePlaylist")]


class Setup:
    """A watcher over the test's folder, with Navidrome set up, the default settings, and a clock the test sets."""

    def __init__(self, tmp_path, navidrome):
        self.navidrome = navidrome
        self.settings = {"playlist": "New Music", "days": 14, "follow_top_artists": 25}
        self.set_up = True
        self.at = MORNING
        self.path = tmp_path / "new_music.json"
        self.watcher = NewMusicPlaylist(self.library, str(self.path), lambda: self.settings,
                                        clock=lambda: self.at.timestamp())

    def library(self):
        if not self.set_up:
            return None
        library = NavidromeLibrary("http://navidrome.local:4533", "nova", "secret")
        library._session = self.navidrome
        return library

    def named(self, name):
        return [found for found in self.navidrome.playlists.values() if found["name"] == name]

    def record(self):
        return json.loads(self.path.read_text()) if self.path.exists() else None


@pytest.fixture
def navidrome(monkeypatch):
    monkeypatch.setattr(network, "online", lambda: True)
    return FakeNavidrome()


@pytest.fixture
def setup(tmp_path, navidrome):
    return Setup(tmp_path, navidrome)


def test_the_playlist_is_made_newest_first_and_public(setup):
    # Two albums this week, one a fortnight and a day ago - past the 14 days - and one with an older song.
    setup.navidrome.album(1, MORNING - timedelta(days=5), songs=2)
    setup.navidrome.album(2, MORNING - timedelta(hours=3), songs=3)
    setup.navidrome.album(3, MORNING - timedelta(days=15), songs=4)
    setup.navidrome.album(4, MORNING - timedelta(days=1), songs=2)
    setup.navidrome.albums[-1]["song"][0]["created"] = _created(MORNING - timedelta(days=200))
    setup.watcher.look()

    [made] = setup.named("New Music")
    # Newest album first, each album's songs in its own order; not the old album, nor the older song of the newer one.
    assert made["songs"] == ["al2-s1", "al2-s2", "al2-s3", "al4-s2", "al1-s1", "al1-s2"]
    assert made["owner"] == "nova" and made["public"] is True
    assert setup.record() == {"day": "2026-11-16"}


def test_the_playlist_is_replaced_and_no_other_is_touched(setup):
    # Nova's own from before, under other capitals; his own of the same name, which Nova can see as it's public;
    # and another of Nova's.
    ours = setup.navidrome.playlist("new music!", songs=["gone-1", "gone-2"], public=True)
    his = setup.navidrome.playlist("New Music", owner="someone", songs=["his-1"], public=True)
    other = setup.navidrome.playlist("Gym", songs=["gym-1"])
    before = copy.deepcopy(setup.navidrome.playlists)
    setup.navidrome.album(1, MORNING - timedelta(days=2), songs=2)
    setup.watcher.look()

    # Replaced, not added to, keeping its id and name.
    assert setup.navidrome.playlists[ours] == {"name": "new music!", "owner": "nova", "songs": ["al1-s1", "al1-s2"],
                                               "public": True}
    assert setup.navidrome.playlists[his] == before[his] and setup.navidrome.playlists[other] == before[other]
    assert len(setup.navidrome.playlists) == 3
    assert {params["playlistId"] for _, params in setup.navidrome.writes()} == {ours}


def test_a_playlist_made_private_since_is_made_public_again(setup):
    ours = setup.navidrome.playlist("New Music", songs=["gone-1"])
    setup.navidrome.album(1, MORNING - timedelta(days=2), songs=1)
    setup.watcher.look()
    assert setup.navidrome.playlists[ours]["public"] is True


def test_nothing_new_leaves_the_playlist_as_it_was(setup, caplog):
    ours = setup.navidrome.playlist("New Music", songs=["kept-1", "kept-2"], public=True)
    setup.navidrome.album(1, MORNING - timedelta(days=20), songs=2)
    with caplog.at_level("INFO", logger="voice_assistant.new_music"):
        setup.watcher.look()
    assert setup.navidrome.playlists[ours]["songs"] == ["kept-1", "kept-2"]
    assert setup.navidrome.writes() == []
    assert "Nothing new in the last 14 days" in caplog.text
    # Done for the day all the same.
    assert setup.record() == {"day": "2026-11-16"}


def test_navidrome_down_is_tried_again_at_the_next_look(setup, caplog):
    setup.navidrome.album(1, MORNING - timedelta(days=1), songs=2)
    setup.navidrome.down = True
    with caplog.at_level("INFO", logger="voice_assistant.new_music"):
        setup.watcher.look()
    assert setup.record() is None
    # Only the kind of error is logged, never the address with its token.
    assert "ConnectionError" in caplog.text and "secret-token" not in caplog.text

    setup.navidrome.down = False
    setup.at = MORNING.replace(hour=7)
    setup.watcher.look()
    assert setup.named("New Music")[0]["songs"] == ["al1-s1", "al1-s2"]
    assert setup.record() == {"day": "2026-11-16"}


def test_once_a_day_from_six(setup):
    setup.navidrome.album(1, MORNING - timedelta(days=1), songs=1)
    # Not before 6 in the morning, local time.
    setup.at = MORNING.replace(hour=5, minute=59)
    setup.watcher.look()
    assert setup.navidrome.asked == [] and setup.record() is None

    setup.at = MORNING
    setup.watcher.look()
    assert setup.named("New Music")[0]["songs"] == ["al1-s1"]

    # Later that day, or after a restart, Navidrome isn't asked again.
    asked = len(setup.navidrome.asked)
    setup.at = MORNING.replace(hour=23)
    setup.watcher.look()
    NewMusicPlaylist(setup.library, str(setup.path), lambda: setup.settings,
                     clock=lambda: MORNING.replace(hour=9).timestamp()).look()
    assert len(setup.navidrome.asked) == asked

    # The next morning, with another album in, it's made again.
    setup.navidrome.album(2, MORNING + timedelta(hours=20), songs=1)
    setup.at = MORNING + timedelta(days=1)
    setup.watcher.look()
    assert setup.named("New Music")[0]["songs"] == ["al2-s1", "al1-s1"]
    assert setup.record() == {"day": "2026-11-17"}


def test_six_is_local_time_in_summer_too(setup):
    # In July, London's 6 in the morning is 5 in UTC: the playlist waits for the local hour wherever the clock is.
    july = datetime(2027, 7, 5, 6, 0)
    setup.navidrome.album(1, july - timedelta(days=1), songs=1)
    setup.at = july - timedelta(minutes=1)
    setup.watcher.look()
    assert setup.record() is None
    setup.at = july
    setup.watcher.look()
    assert setup.record() == {"day": "2027-07-05"}


def test_the_newest_500_songs_at_most_are_given_100_at_a_time(setup):
    for n in range(1, 61):
        setup.navidrome.album(n, MORNING - timedelta(minutes=n), songs=10)
    setup.watcher.look()
    [made] = setup.named("New Music")
    assert len(made["songs"]) == 500
    assert made["songs"][:2] == ["al1-s1", "al1-s2"] and made["songs"][-1] == "al50-s10"
    # Each request lists 100 songs at most, so its address stays short.
    writes = setup.navidrome.writes()
    assert [len(params.get("songId") or params.get("songIdToAdd") or []) for _, params in writes] == [100] * 5 + [0]
    assert writes[-1][1] == {"playlistId": writes[1][1]["playlistId"], "public": "true"}


def test_the_days_setting_is_read_at_each_making(setup):
    setup.navidrome.album(1, MORNING - timedelta(days=3), songs=1)
    setup.navidrome.album(2, MORNING - timedelta(days=25), songs=1)
    setup.settings["days"] = 2
    setup.watcher.look()
    assert setup.named("New Music") == []
    setup.settings["days"] = 31
    setup.at = MORNING + timedelta(days=1)
    setup.watcher.look()
    assert setup.named("New Music")[0]["songs"] == ["al1-s1", "al2-s1"]


@pytest.mark.parametrize("days", [0, 61, 2.5, True, "lots", None])
def test_days_not_a_whole_number_from_1_to_60_is_14(setup, days):
    setup.navidrome.album(1, MORNING - timedelta(days=13), songs=1)
    setup.navidrome.album(2, MORNING - timedelta(days=15), songs=1)
    setup.settings["days"] = days
    setup.watcher.look()
    assert setup.named("New Music")[0]["songs"] == ["al1-s1"]


def test_the_playlist_setting_names_it(setup):
    setup.navidrome.album(1, MORNING - timedelta(days=1), songs=1)
    setup.settings["playlist"] = "Fresh In"
    setup.watcher.look()
    assert setup.named("Fresh In")[0]["songs"] == ["al1-s1"] and setup.named("New Music") == []


def test_nothing_without_navidrome_or_a_playlist_name(setup):
    setup.navidrome.album(1, MORNING - timedelta(days=1), songs=1)
    setup.set_up = False
    setup.watcher.look()
    setup.set_up = True
    setup.settings["playlist"] = "  "
    setup.watcher.look()
    assert setup.navidrome.asked == [] and setup.record() is None


def test_logs_counts_only(setup, caplog):
    setup.navidrome.album(1, MORNING - timedelta(days=1), songs=2)
    with caplog.at_level("DEBUG"):
        setup.watcher.look()
    assert "Made the new-music playlist with 2 songs from the last 14 days" in caplog.text
    for secret in ("Band 1", "Album 1", "Song 1", "al1", "secret"):
        assert secret not in caplog.text


def test_the_thread_looks_at_once_and_stops(setup):
    looked = threading.Event()
    setup.watcher.look = looked.set
    setup.watcher.start()
    try:
        assert looked.wait(5)
        assert any(thread.name == "new-music" and thread.is_alive() for thread in threading.enumerate())
    finally:
        setup.watcher.stop()
    assert setup.watcher._thread is None
    assert not any(thread.name == "new-music" for thread in threading.enumerate())
