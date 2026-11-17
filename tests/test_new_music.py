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
        # Whether createPlaylist leaves out the id of what it made, as an old server may, and whether
        # getPlaylists then leaves the new playlists out too, so they can't be found.
        self.no_id = False
        self.hide_new = False
        self.hidden = set()
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
            for pid, found in self.playlists.items() if (found["owner"] == user or found["public"])
            and pid not in self.hidden]}}

    def _createPlaylist(self, params, user):
        songs = params.get("songId") or []
        songs = list(songs) if isinstance(songs, list) else [songs]
        pid = params.get("playlistId")
        if pid:
            assert self.playlists[pid]["owner"] == user, "Nova replaced another user's playlist"
            self.playlists[pid]["songs"] = songs
        else:
            pid = self.playlist(params["name"], owner=user, songs=songs)
            if self.hide_new:
                self.hidden.add(pid)
            if self.no_id:
                return {}
        return {"playlist": {"id": pid, "name": self.playlists[pid]["name"]}}

    def _updatePlaylist(self, params, user):
        found = self.playlists[params["playlistId"]]
        assert found["owner"] == user, "Nova changed another user's playlist"
        found["songs"] += list(params.get("songIdToAdd") or [])
        if "public" in params:
            found["public"] = params["public"] == "true"
        return {}

    def created(self):
        """The playlists made, by name, as asked of createPlaylist."""
        return [params["name"] for endpoint, params in self.asked if endpoint == "createPlaylist" and "name" in params]

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

    def made(self, pid, name="new music", day="2026-11-15"):
        """The record of a playlist Nova made on an earlier day: its id, and the normalised name it was made under."""
        self.path.write_text(json.dumps({"day": day, "id": pid, "name": name}))


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
    # Its id is kept, with the name it was made under, so the next morning replaces this one and no other.
    assert setup.record() == {"day": "2026-11-16", "id": "pl1", "name": "new music"}


def test_the_playlist_nova_made_is_replaced_and_no_other_is_touched(setup):
    # Nova's own from before, under other capitals, which the record has the id of; his own of the same name,
    # another user's, which Nova can see as it's public; and another of Nova's.
    ours = setup.navidrome.playlist("new music!", songs=["gone-1", "gone-2"], public=True)
    his = setup.navidrome.playlist("New Music", owner="someone", songs=["his-1"], public=True)
    other = setup.navidrome.playlist("Gym", songs=["gym-1"])
    setup.made(ours)
    before = copy.deepcopy(setup.navidrome.playlists)
    setup.navidrome.album(1, MORNING - timedelta(days=2), songs=2)
    setup.watcher.look()

    # Replaced, not added to, keeping its id and name.
    assert setup.navidrome.playlists[ours] == {"name": "new music!", "owner": "nova", "songs": ["al1-s1", "al1-s2"],
                                               "public": True}
    assert setup.navidrome.playlists[his] == before[his] and setup.navidrome.playlists[other] == before[other]
    assert len(setup.navidrome.playlists) == 3
    assert {params["playlistId"] for _, params in setup.navidrome.writes()} == {ours}
    assert setup.navidrome.created() == []
    assert setup.record() == {"day": "2026-11-16", "id": ours, "name": "new music"}


def test_his_own_playlist_of_the_name_is_never_written_though_he_is_the_same_user(setup):
    # Nova signs in as the user who owns his playlists, so a playlist of the name he made himself is owned by
    # the same user. With no record of an id, it is no proof the playlist is Nova's: a new one is made.
    his = setup.navidrome.playlist("New Music", songs=["his-1", "his-2"], public=True)
    setup.navidrome.album(1, MORNING - timedelta(days=2), songs=2)
    setup.watcher.look()

    assert setup.navidrome.playlists[his]["songs"] == ["his-1", "his-2"]
    assert len(setup.navidrome.playlists) == 2
    [new] = [pid for pid in setup.navidrome.playlists if pid != his]
    assert setup.navidrome.playlists[new]["songs"] == ["al1-s1", "al1-s2"] and setup.navidrome.playlists[new]["public"]
    assert {params["playlistId"] for _, params in setup.navidrome.writes() if "playlistId" in params} == {new}
    assert setup.record() == {"day": "2026-11-16", "id": new, "name": "new music"}

    # And the next morning too: the recorded one is replaced, his still left, and no third is made.
    setup.navidrome.album(2, MORNING + timedelta(hours=20), songs=1)
    setup.at = MORNING + timedelta(days=1)
    setup.watcher.look()
    assert setup.navidrome.playlists[his]["songs"] == ["his-1", "his-2"]
    assert len(setup.navidrome.playlists) == 2 and setup.navidrome.playlists[new]["songs"][0] == "al2-s1"


def test_a_record_from_before_ids_were_kept_makes_a_new_playlist(setup):
    # The old record held only the day: the playlist of the name, though Nova made it, isn't picked by name.
    old = setup.navidrome.playlist("New Music", songs=["old-1"], public=True)
    setup.path.write_text(json.dumps({"day": "2026-11-15"}))
    setup.navidrome.album(1, MORNING - timedelta(days=2), songs=1)
    setup.watcher.look()
    assert setup.navidrome.playlists[old]["songs"] == ["old-1"] and len(setup.navidrome.playlists) == 2


def test_a_playlist_he_deleted_is_made_again_and_the_new_id_kept(setup):
    gone = setup.navidrome.playlist("New Music", songs=["x"], public=True)
    setup.made(gone)
    del setup.navidrome.playlists[gone]
    setup.navidrome.album(1, MORNING - timedelta(days=2), songs=2)
    setup.watcher.look()

    [(new, made)] = setup.navidrome.playlists.items()
    assert new != gone and made["name"] == "New Music" and made["songs"] == ["al1-s1", "al1-s2"] and made["public"]
    assert setup.record() == {"day": "2026-11-16", "id": new, "name": "new music"}
    assert setup.navidrome.created() == ["New Music"]
    assert setup.watcher.playlist_id("New Music") == new


def test_a_renamed_setting_makes_a_new_playlist_and_leaves_the_old(setup):
    old = setup.navidrome.playlist("New Music", songs=["old-1"], public=True)
    setup.made(old)
    setup.settings["playlist"] = "Fresh In"
    setup.navidrome.album(1, MORNING - timedelta(days=2), songs=1)
    setup.watcher.look()

    assert setup.navidrome.playlists[old]["songs"] == ["old-1"]
    [new] = [pid for pid in setup.navidrome.playlists if pid != old]
    assert setup.navidrome.playlists[new]["name"] == "Fresh In" and setup.navidrome.playlists[new]["songs"] == ["al1-s1"]
    assert setup.record() == {"day": "2026-11-16", "id": new, "name": "fresh in"}

    # The next morning replaces the new one, and makes no other.
    setup.navidrome.album(2, MORNING + timedelta(hours=20), songs=1)
    setup.at = MORNING + timedelta(days=1)
    setup.watcher.look()
    assert len(setup.navidrome.playlists) == 2 and setup.navidrome.playlists[old]["songs"] == ["old-1"]
    assert setup.navidrome.playlists[new]["songs"] == ["al2-s1", "al1-s1"]


def test_a_server_which_gives_no_id_is_asked_for_what_it_made_among_the_new(setup):
    # His own of the name is there before, and isn't taken for the new one.
    his = setup.navidrome.playlist("New Music", songs=["his-1"], public=True)
    setup.navidrome.no_id = True
    setup.navidrome.album(1, MORNING - timedelta(days=2), songs=1)
    setup.watcher.look()

    assert setup.navidrome.created() == ["New Music"]
    assert setup.navidrome.playlists[his]["songs"] == ["his-1"]
    [new] = [pid for pid in setup.navidrome.playlists if pid != his]
    assert setup.navidrome.playlists[new]["songs"] == ["al1-s1"]
    assert setup.record() == {"day": "2026-11-16", "id": new, "name": "new music"}


def test_a_server_which_gives_no_id_and_hides_the_new_one_makes_at_most_one_per_look(setup, caplog):
    setup.navidrome.no_id = True
    setup.navidrome.hide_new = True
    setup.navidrome.album(1, MORNING - timedelta(days=2), songs=1)
    with caplog.at_level("INFO", logger="voice_assistant.new_music"):
        setup.watcher.look()
    # One made, none filled or noted, the day not recorded so it is tried again at the next look.
    assert setup.navidrome.created() == ["New Music"] and len(setup.navidrome.playlists) == 1
    assert setup.navidrome.writes() == [("createPlaylist", {"name": "New Music"})]
    assert setup.record() is None
    assert "NavidromeError" in caplog.text


def test_a_failure_part_way_fills_the_same_playlist_at_the_next_look(setup):
    for n in range(1, 21):
        setup.navidrome.album(n, MORNING - timedelta(minutes=n), songs=10)
    updates = setup.navidrome._updatePlaylist

    def fails(params, user):
        raise requests.ConnectionError("down")
    setup.navidrome._updatePlaylist = fails
    setup.watcher.look()
    [(pid, _)] = setup.navidrome.playlists.items()
    # Its id is kept, though the day isn't, so the next look fills it rather than making another.
    assert setup.record() == {"id": pid, "name": "new music"}

    setup.navidrome._updatePlaylist = updates
    setup.at = MORNING.replace(hour=7)
    setup.watcher.look()
    assert list(setup.navidrome.playlists) == [pid] and len(setup.navidrome.playlists[pid]["songs"]) == 200
    assert setup.navidrome.created() == ["New Music"]


@pytest.mark.parametrize("name", ["!!!", "\U0001F3B5\U0001F3B6", " - ", "\u30cb\u30e5\u30fc"])
def test_a_name_with_no_letters_or_digits_is_skipped_with_one_line(setup, caplog, name):
    setup.navidrome.album(1, MORNING - timedelta(days=1), songs=1)
    setup.settings["playlist"] = name
    with caplog.at_level("INFO", logger="voice_assistant.new_music"):
        setup.watcher.look()
    assert setup.navidrome.asked == [] and setup.record() is None
    assert [record.getMessage() for record in caplog.records] == [
        "The new-music playlist's name has no letters or digits, so it isn't made"]


def test_the_recorded_id_is_only_for_the_name_it_was_made_under(setup):
    setup.made("pl9")
    assert setup.watcher.playlist_id("New Music") == "pl9" and setup.watcher.playlist_id(" new  MUSIC! ") == "pl9"
    assert setup.watcher.playlist_id("Fresh In") is None and setup.watcher.playlist_id("") is None
    setup.path.write_text(json.dumps({"day": "2026-11-15"}))
    assert setup.watcher.playlist_id("New Music") is None
    setup.path.write_text("not json")
    assert setup.watcher.playlist_id("New Music") is None


def test_a_playlist_made_private_since_is_made_public_again(setup):
    ours = setup.navidrome.playlist("New Music", songs=["gone-1"])
    setup.made(ours)
    setup.navidrome.album(1, MORNING - timedelta(days=2), songs=1)
    setup.watcher.look()
    assert setup.navidrome.playlists[ours]["public"] is True


def test_nothing_new_leaves_the_playlist_as_it_was(setup, caplog):
    ours = setup.navidrome.playlist("New Music", songs=["kept-1", "kept-2"], public=True)
    setup.made(ours)
    setup.navidrome.album(1, MORNING - timedelta(days=20), songs=2)
    with caplog.at_level("INFO", logger="voice_assistant.new_music"):
        setup.watcher.look()
    assert setup.navidrome.playlists[ours]["songs"] == ["kept-1", "kept-2"]
    assert setup.navidrome.writes() == []
    assert "Nothing new in the last 14 days" in caplog.text
    # Done for the day all the same, with the playlist it holds still noted.
    assert setup.record() == {"day": "2026-11-16", "id": ours, "name": "new music"}


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
    assert setup.record() == {"day": "2026-11-16", "id": "pl1", "name": "new music"}


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
    assert setup.record() == {"day": "2026-11-17", "id": "pl1", "name": "new music"}
    assert setup.navidrome.created() == ["New Music"]


def test_six_is_local_time_in_summer_too(setup):
    # In July, London's 6 in the morning is 5 in UTC: the playlist waits for the local hour wherever the clock is.
    july = datetime(2027, 7, 5, 6, 0)
    setup.navidrome.album(1, july - timedelta(days=1), songs=1)
    setup.at = july - timedelta(minutes=1)
    setup.watcher.look()
    assert setup.record() is None
    setup.at = july
    setup.watcher.look()
    assert setup.record()["day"] == "2027-07-05"


def test_the_newest_500_songs_at_most_are_given_100_at_a_time(setup):
    for n in range(1, 61):
        setup.navidrome.album(n, MORNING - timedelta(minutes=n), songs=10)
    setup.watcher.look()
    [made] = setup.named("New Music")
    assert len(made["songs"]) == 500
    assert made["songs"][:2] == ["al1-s1", "al1-s2"] and made["songs"][-1] == "al50-s10"
    # Each request lists 100 songs at most, so its address stays short.
    writes = setup.navidrome.writes()
    assert [len(params.get("songId") or params.get("songIdToAdd") or []) for _, params in writes] == [0] + [100] * 5 + [0]
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
