"""
Navidrome Test
Checks how music is found on Navidrome and how the queue plays, using a fake in
place of Navidrome's API and of the speaker, so nothing is sent or played. The one
test which decodes real audio needs ffmpeg, and is skipped anywhere it isn't.
"""

import copy
import hashlib
import io
import logging
import os
import shutil
import subprocess
import threading
import time
import wave
from datetime import datetime, timezone

import pytest
import requests

np = pytest.importorskip("numpy")

from voice_assistant import network
from voice_assistant import volume as volume_module
from voice_assistant.navidrome import (CLEAN_VERSIONS, VERSIONS, WRONG_LOGIN, NavidromeError, NavidromeLibrary,
                                       NavidromePlayer, best, is_clean, likeness, marked_clean, split_by, version_of)
from voice_assistant.volume import VolumeControl


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


class FakeNavidrome:
    """Stands in for Navidrome's Subsonic API, answering each endpoint from a dict."""

    def __init__(self, answers):
        self.answers = answers
        self.headers = {}
        self.requests = []

    def get(self, url, params=None, timeout=None, stream=False):
        endpoint = url.rsplit("/rest/", 1)[1]
        self.requests.append((endpoint, dict(params or {})))
        answer = self.answers.get(endpoint, {})
        if isinstance(answer, Exception):
            raise answer
        # A copy, as from a real server, since the player shuffles what it's given.
        body = copy.deepcopy(answer(params) if callable(answer) else answer)
        return FakeResponse({"subsonic-response": {"status": "ok", **body}})


class FakePlayback:
    """One song 'playing' - finished when the test says, or when stopped."""

    # What a RemotePlayback says its position is counted in.
    sample_rate = 48000

    def __init__(self, song, volume, paused):
        self.song = song
        self.volume = volume
        self.paused = paused
        self.position = 0
        self.duration = 200.0
        self.stopped = False
        self._done = threading.Event()

    def wait(self, timeout=None):
        return self._done.wait(timeout)

    def stop(self):
        self.stopped = True
        self._done.set()

    def finish(self):
        self._done.set()


class FakeSpeaker:
    sample_rate = 48000

    def __init__(self):
        self.started = []

    def play_stream(self, song, volume=1.0, kind="reply", paused=False, most_ahead=None):
        self.started.append(FakePlayback(song, volume, paused))
        return self.started[-1]


def _until(check, timeout=2.0):
    """Waits for the player's own thread to catch up."""
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


SONGS = [{"id": str(n), "title": f"Song {n}", "artist": "Band"} for n in range(1, 5)]


@pytest.fixture
def make_library():
    def factory(answers=None):
        library = NavidromeLibrary("http://navidrome.local:4533/", "nova", "secret")
        library._session = FakeNavidrome(answers or {})
        return library

    return factory


@pytest.fixture
def make_player(tmp_path, monkeypatch):
    monkeypatch.setattr(volume_module, "VOLUMES_FILE", str(tmp_path / "volumes.json"))

    def factory(answers=None):
        speaker = FakeSpeaker()
        player = NavidromePlayer("http://navidrome.local:4533/", "nova", "secret", speaker, VolumeControl())
        player._session = FakeNavidrome(answers or {})
        # The song itself stands in for its audio, so the fake speaker knows which it is.
        player._decode = lambda song, start=0.0: song
        player.speaker_started = speaker.started
        return player

    return factory


def _playing(player):
    _until(lambda: player.speaker_started and not player.speaker_started[-1].stopped)
    return player.speaker_started[-1]


# Finding music

def test_names_are_matched_however_theyre_written():
    assert likeness("Beyoncé - Halo", "beyonce halo") == 3
    assert likeness("The Chronic (Remastered)", "the chronic") == 2
    assert likeness("Something Else", "the chronic") == 0
    assert split_by("Gooba by 6ix9ine") == ("Gooba", "6ix9ine")
    assert split_by("Stand by Me") == ("Stand", "Me")
    assert split_by("Drake") == ("Drake", None)


def test_the_best_match_wins_and_the_artist_breaks_a_tie():
    found = [{"title": "Gooba (Remix)", "artist": "Someone"}, {"title": "Gooba", "artist": "Other"},
             {"title": "Gooba", "artist": "6ix9ine"}]
    assert best(found, "title", "Gooba", "6ix9ine") == found[2]
    assert best(found, "title", "Nothing like it", None) is None


def test_the_password_is_never_sent_only_a_salted_hash(make_player):
    player = make_player()
    first, second = player._sign_in(), player._sign_in()
    assert "secret" not in first.values()
    assert first["t"] == hashlib.md5(("secret" + first["s"]).encode()).hexdigest()
    assert first["s"] != second["s"]


def test_a_song_is_played_then_more_by_the_artist(make_player):
    player = make_player({"search3": lambda params: {"searchResult3": {"song": [
        {"id": "9", "title": "Gooba (Remix)", "artist": "Someone"},
        {"id": "1", "title": "Gooba", "artist": "6ix9ine"},
        {"id": "2", "title": "Fefe", "artist": "6ix9ine"}]}}})
    result = player.play("Gooba by 6ix9ine", search_type="track")

    assert result == {"success": True, "message": "Playing Gooba by 6ix9ine"}
    assert _playing(player).song["id"] == "1"
    assert player._session.requests[0][1]["query"] == "Gooba 6ix9ine"


def test_an_album_plays_in_order(make_player):
    player = make_player({
        "search3": {"searchResult3": {"album": [{"id": "a1", "name": "The Chronic", "artist": "Dr. Dre"}]}},
        "getAlbum": {"album": {"song": SONGS}},
    })
    assert player.play("The Chronic by Dr. Dre", search_type="album")["message"] == "Playing The Chronic by Dr. Dre"
    assert [song["id"] for song in player._queue] == ["1", "2", "3", "4"]


def test_a_playlist_is_found_by_part_of_its_name(make_player):
    player = make_player({
        "getPlaylists": {"playlists": {"playlist": [{"id": "p1", "name": "Chill"}, {"id": "p2", "name": "Workout Mix"}]}},
        "getPlaylist": lambda params: {"playlist": {"entry": SONGS if params["id"] == "p2" else []}},
    })
    assert player.play_playlist("workout") == {"success": True, "message": "Playing Workout Mix"}
    assert sorted(song["id"] for song in player._queue) == ["1", "2", "3", "4"]


def test_liked_songs_are_the_starred_ones(make_player):
    player = make_player({"getStarred2": {"starred2": {"song": SONGS[:2]}}})
    assert player.play_playlist("liked songs")["message"] == "Playing your liked songs"
    assert player.play()["message"] in ("Resumed playback", "Playing your liked songs")


def test_something_not_in_the_library_says_so(make_player):
    player = make_player({"search3": {"searchResult3": {}}})
    assert player.play("Gooba by 6ix9ine") == {"success": False, "missing": True,
                                               "message": "I couldn't find Gooba by 6ix9ine on Navidrome."}


def test_a_wrong_password_says_which_setting_to_check(make_player):
    player = make_player()
    player._session.answers["search3"] = {}
    player._session.get = lambda *args, **kwargs: FakeResponse({"subsonic-response": {
        "status": "failed", "error": {"code": 40, "message": "Wrong username or password"}}})
    assert "NAVIDROME_PASSWORD" in player.play("Drake")["message"]


def test_a_failed_request_never_logs_the_token(make_player, caplog):
    # A requests error names the address it was asking, which holds the token.
    player = make_player({"search3": requests.ConnectionError("http://nd/rest/search3?t=abc123token")})
    assert player.play("Drake")["message"] == "I can't reach Navidrome right now."
    assert "abc123token" not in caplog.text


def test_with_no_network_it_isnt_asked_at_all(make_player, monkeypatch):
    monkeypatch.setattr(network, "online", lambda: False)
    player = make_player()
    assert player.play("Drake")["message"] == "I can't reach Navidrome right now."
    assert player._session.requests == []


def test_something_missing_is_marked_so_a_download_can_be_offered(make_player):
    assert make_player().play("Nothing like it", search_type="track") == {
        "success": False, "message": "I couldn't find Nothing like it on Navidrome.", "missing": True}


def test_with_nothing_asked_for_nothing_is_missing(make_player):
    # Nothing was asked for, so there's nothing to download.
    result = make_player({"getStarred2": {}, "getRandomSongs": {}}).play()
    assert result == {"success": False, "message": "I couldn't find anything to play on Navidrome."}


# Finding what's there, for downloads

# What a search for "Gooba" finds: the song as explicit and as clean, the same title by
# someone else, and a near miss.
GOOBA = [
    {"id": "1", "title": "Gooba", "artist": "6ix9ine", "album": "Day69", "explicitStatus": "explicit"},
    {"id": "2", "title": "Gooba (Clean)", "artist": "6ix9ine", "album": "Day69", "explicitStatus": "clean"},
    {"id": "3", "title": "Gooba", "artist": "Other", "album": "Covers"},
    {"id": "4", "title": "Goo", "artist": "6ix9ine", "album": "Day69"},
]


def _ids(items):
    return [item["id"] for item in items]


def _songs_found(songs):
    return {"search3": {"searchResult3": {"song": songs}}}


def test_a_version_is_named_in_brackets_or_after_a_dash():
    assert version_of("Gooba (Clean)") == "clean"
    assert version_of("Gooba [Radio Edit]") == "radio edit"
    assert version_of("Gooba (feat. Someone) [Slowed]") == "slowed"
    assert version_of("Gooba - Live at Wembley") == "live"
    assert version_of("Gooba - Sped Up") == "sped up"
    assert version_of("Day69 [Edited Version]") == "edited"
    assert version_of("Gooba") is None and version_of("") is None
    # An en dash or an em dash is a dash too, and a bracket after the dashed part is a part of its own.
    assert version_of("Gooba \u2013 Radio Edit") == "radio edit"
    assert version_of("Gooba \u2014 Clean") == "clean"
    assert version_of("Gooba - Radio Edit (feat. X)") == "radio edit"
    assert version_of("Gooba - Clean (Remastered)") == "clean"


def test_a_version_is_only_where_a_version_goes_and_a_whole_word():
    # A title is its title: only a bracketed part or one after " - " names a version.
    assert version_of("Clean") is None and version_of("Edited Highlights") is None
    assert version_of("Live and Let Die") is None and version_of("Gooba-Live") is None
    # Whole words: "Alive" isn't "live", nor "Unedited" "edited".
    assert version_of("Gooba (Alive)") is None and version_of("Gooba (Unedited)") is None
    # "Radio" and "Edit" in two brackets are no radio edit.
    assert version_of("Gooba (Radio) (Edit)") is None


def test_a_clean_version_says_so_and_nothing_more():
    # "Version" or "edit" after the word is still the clean version.
    for name in ("Gooba (Clean)", "Gooba (Clean Version)", "Gooba (Clean Edit)", "Gooba [Edited Version]",
                 "Gooba (Radio Edit)", "Gooba - Clean", "Gooba - Radio Edit", "Gooba (Live - Radio Edit)"):
        assert marked_clean(name), name
    assert version_of("Gooba (Live - Radio Edit)") == "radio edit"
    assert version_of("Gooba (Clean Radio Edit)") == "radio edit"
    # So are "album" and "edition", as the labels tag them, and a censored version is a clean one.
    for name in ("Gooba (Clean Album Version)", "Gooba (Edited Album Version)", "Gooba (Clean Edition)",
                 "Gooba (Censored)"):
        assert marked_clean(name), name
    assert version_of("Gooba (Censored)") == "censored"
    # As are the labels' "clean lyrics" and "clean advance copy".
    for name in ("Gooba (Clean Lyrics)", "Gooba [Clean Advance Copy]", "Gooba - Clean Lyrics"):
        assert marked_clean(name), name
    assert version_of("Gooba (clean lyrics)") == "clean"
    assert is_clean({"title": "Gooba", "album": "Day69 (Clean Advance Copy)"})
    assert not marked_clean("Gooba (Clean Lyrics Bandit)") and not marked_clean("Gooba (Clean Copy Cat)")
    # Still not a credit to Clean Bandit, nor their remix.
    assert not marked_clean("Gooba (feat. Clean Bandit)") and not marked_clean("Gooba (Clean Bandit Remix)")


def test_a_credit_to_an_artist_called_clean_isnt_a_clean_version():
    # Clean Bandit are credited on other people's records; the song is not a clean one for that.
    assert version_of("Gooba (feat. Clean Bandit)") is None
    assert version_of("Gooba - feat. Clean Bandit") is None
    assert version_of("Gooba (Edited by Clean Bandit)") is None
    assert version_of("Gooba (Clean Bandit Remix)") == "remix"
    assert version_of("Gooba [Clean Bandit Remix]") == "remix"
    assert version_of("Gooba - Clean Bandit Remix") == "remix"
    # Whichever of its words comes last still counts: this one is a clean version, by Clean Bandit.
    assert version_of("Gooba (Clean Bandit - Clean)") == "clean"
    assert version_of("Gooba [Clean Bandit Clean Edit]") == "clean"


def test_a_credit_to_an_artist_called_clean_is_no_clean_track():
    assert not is_clean({"title": "Gooba (feat. Clean Bandit)"})
    assert not is_clean({"title": "Gooba (Clean Bandit Remix)"})
    assert not is_clean({"title": "Gooba - Clean Bandit Remix"})
    assert not is_clean({"title": "Gooba", "album": "Hits [feat. Clean Bandit]"})
    assert is_clean({"title": "Gooba (feat. Clean Bandit)", "explicitStatus": "clean"})
    assert not is_clean({"title": "Gooba (Clean)", "album": "Hits", "explicitStatus": "explicit"})


def test_only_some_versions_are_clean_ones():
    assert CLEAN_VERSIONS == ("clean", "edited", "radio edit", "censored")
    assert all(version in VERSIONS for version in CLEAN_VERSIONS)
    assert marked_clean("Gooba (Clean)") and marked_clean("Gooba - Radio Edit")
    assert not marked_clean("Gooba (Live)") and not marked_clean("Gooba - Remix") and not marked_clean("Gooba")


def test_clean_is_navidromes_mark_or_a_title_that_says_so():
    assert is_clean({"title": "Gooba", "explicitStatus": "clean"})
    assert not is_clean({"title": "Gooba (Clean)", "explicitStatus": "explicit"})
    assert is_clean({"title": "Gooba (Clean)"}) and is_clean({"title": "Gooba - Radio Edit", "explicitStatus": ""})
    assert is_clean({"title": "Gooba", "album": "Day69 [Edited Version]"})
    assert not is_clean({"title": "Gooba", "album": "Day69"})
    # A song called Clean is only its title.
    assert not is_clean({"title": "Clean", "album": "1989"}) and not is_clean({"title": "Edited Highlights"})


def test_a_live_or_remixed_track_isnt_a_clean_one_and_no_mark_is_no_mark():
    assert not is_clean({"title": "Gooba (Live)"}) and not is_clean({"title": "Gooba", "album": "Day69 (Remixes)"})
    # Missing fields and a mark Navidrome might one day add are no mark.
    assert not is_clean({}) and not is_clean({"title": None, "album": None, "explicitStatus": None})
    assert is_clean({"title": "Gooba (Clean)", "explicitStatus": "unheard-of"})


def test_songs_are_found_by_every_word_of_the_title_and_the_artist(make_library):
    library = make_library(_songs_found(GOOBA))
    assert _ids(library.find_songs("Gooba", "6ix9ine")) == ["1", "2"]
    assert _ids(library.find_songs("Gooba", None)) == ["1", "2", "3"]
    assert library.find_songs("Nothing like it", None) == []


def test_songs_are_searched_for_as_they_are_when_played(make_library):
    library = make_library(_songs_found(GOOBA))
    library.find_songs("Gooba", "6ix9ine")
    library.find_songs("Gooba", None)
    assert [params["query"] for _, params in library._session.requests] == ["Gooba 6ix9ine", "Gooba"]
    assert all(endpoint == "search3" and params["songCount"] == 20 for endpoint, params in library._session.requests)


def test_the_title_alone_is_searched_when_title_and_artist_find_nothing(make_library):
    # The library spells the artist differently from how it was said, so the two together find nothing.
    library = make_library({"search3": lambda params: {"searchResult3": {
        "song": GOOBA[:1] if params["query"] == "Gooba" else []}}})
    assert _ids(library.find_songs("Gooba", "6ix9ine")) == ["1"]
    assert [params["query"] for _, params in library._session.requests] == ["Gooba 6ix9ine", "Gooba"]


def test_the_title_alone_is_searched_when_only_others_come_of_title_and_artist(make_library):
    library = make_library({"search3": lambda params: {"searchResult3": {
        "song": GOOBA[2:3] if params["query"] != "Gooba" else GOOBA[:1]}}})
    assert _ids(library.find_songs("Gooba", "6ix9ine")) == ["1"]


def test_a_title_alone_is_searched_once(make_library):
    library = make_library(_songs_found([]))
    assert library.find_songs("Gooba", None) == []
    assert len(library._session.requests) == 1


def test_a_live_version_isnt_the_song_asked_for(make_library):
    library = make_library(_songs_found([
        {"id": "5", "title": "Dreams (Live)", "artist": "Fleetwood Mac", "album": "The Dance"},
        {"id": "6", "title": "Dreams - Remix", "artist": "Fleetwood Mac", "album": "Remixes"},
    ]))
    assert library.find_songs("Dreams", "Fleetwood Mac") == []
    assert _ids(library.find_songs("Dreams Live", "Fleetwood Mac")) == ["5"]
    assert _ids(library.find_songs("Dreams Remix", "Fleetwood Mac")) == ["6"]


def test_a_demo_is_a_version_of_a_song(make_library):
    assert version_of("Dreams (Demo)") == "demo" and version_of("Dreams - Demo Version") == "demo"
    assert version_of("Demolition Man") is None and version_of("Dreams (Demon)") is None
    # Not the song asked for, so a demo in the library doesn't count as having the song
    # for a download, unless the demo itself was asked for.
    library = make_library(_songs_found([
        {"id": "5", "title": "Dreams (Demo)", "artist": "Fleetwood Mac", "album": "Rumours Deluxe"},
    ]))
    assert library.find_songs("Dreams", "Fleetwood Mac") == []
    assert _ids(library.find_songs("Dreams Demo", "Fleetwood Mac")) == ["5"]


def test_play_takes_the_plain_song_over_its_demo(make_player):
    player = make_player({"search3": lambda params: {"searchResult3": {"song": [
        {"id": "5", "title": "Dreams (Demo)", "artist": "Fleetwood Mac"},
        {"id": "6", "title": "Dreams", "artist": "Fleetwood Mac"}]}}})
    assert player.play("Dreams by Fleetwood Mac", search_type="track")["message"] == "Playing Dreams by Fleetwood Mac"
    assert _playing(player).song["id"] == "6"


def test_play_still_plays_a_demo_when_its_all_there_is(make_player):
    player = make_player({"search3": lambda params: {"searchResult3": {"song": [
        {"id": "5", "title": "Dreams (Demo)", "artist": "Fleetwood Mac"}]}}})
    assert player.play("Dreams by Fleetwood Mac", search_type="track")["success"] is True
    assert _playing(player).song["id"] == "5"


def test_a_song_with_more_words_to_its_title_isnt_the_song_asked_for(make_library):
    library = make_library(_songs_found([
        {"id": "20", "title": "Endless Love", "artist": "Gunna"},
        {"id": "21", "title": "Endless (feat. Someone)", "artist": "Gunna"},
        {"id": "22", "title": "Endless - 2011 Remaster", "artist": "Gunna"},
    ]))
    # "Endless Love" is another song, but a credit or a remaster note is the same one.
    assert _ids(library.find_songs("Endless", "Gunna")) == ["21", "22"]
    assert _ids(library.find_songs("Endless Love", "Gunna")) == ["20"]


def test_a_dash_inside_brackets_doesnt_cut_a_title_short(make_library):
    # "(Live - Radio Edit)" is one bracketed part, a radio edit: no words of the song's own.
    library = make_library(_songs_found([{"id": "30", "title": "Gooba (Live - Radio Edit)", "artist": "6ix9ine"}]))
    assert _ids(library.find_songs("Gooba", "6ix9ine")) == ["30"]


def test_a_credit_with_no_brackets_isnt_part_of_a_title(make_library):
    library = make_library(_songs_found([
        {"id": "31", "title": "Money In The Grave feat. Rick Ross", "artist": "Drake"},
        {"id": "32", "title": "Money In The Grave ft Rick Ross", "artist": "Drake"},
        {"id": "33", "title": "Stay With Me", "artist": "Drake"},
    ]))
    assert _ids(library.find_songs("Money In The Grave", "Drake")) == ["31", "32"]
    # "with" is only a credit in brackets: "Stay With Me" is no "Stay".
    assert library.find_songs("Stay", "Drake") == []


def test_the_title_soulsync_found_may_be_wider_than_the_one_asked_for(make_library):
    library = make_library(_songs_found([{"id": "20", "title": "Endless Love", "artist": "Gunna"}]))
    assert library.find_songs("Endless", "Gunna") == []
    assert library.find_songs("Endless", "Gunna", also="endless") == []
    assert _ids(library.find_songs("Endless", "Gunna", also="Endless Love")) == ["20"]


def test_a_bracketed_end_to_a_title_is_still_its_title_when_the_rest_is_asked_for(make_library):
    library = make_library(_songs_found([
        {"id": "23", "title": "Sweet Dreams (Are Made of This)", "artist": "Eurythmics"}]))
    assert _ids(library.find_songs("Sweet Dreams", "Eurythmics")) == ["23"]
    assert _ids(library.find_songs("Sweet Dreams Are Made of This", "Eurythmics")) == ["23"]


def test_play_still_finds_a_song_by_part_of_its_title(make_player):
    player = make_player({"search3": lambda params: {"searchResult3": {"song": [
        {"id": "23", "title": "Sweet Dreams (Are Made of This)", "artist": "Eurythmics"},
        {"id": "20", "title": "Endless Love", "artist": "Gunna"}]}}})
    assert player.play("Sweet Dreams", search_type="track")["message"] == (
        "Playing Sweet Dreams (Are Made of This) by Eurythmics")
    assert _playing(player).song["id"] == "23"
    player = make_player({"search3": lambda params: {"searchResult3": {"song": [
        {"id": "20", "title": "Endless Love", "artist": "Gunna"}]}}})
    assert player.play("Endless", search_type="track")["message"] == "Playing Endless Love by Gunna"


def test_a_clean_version_is_always_one_of_the_songs_found(make_library):
    library = make_library(_songs_found([
        {"id": "7", "title": "Dreams - Radio Edit", "artist": "Fleetwood Mac"},
        {"id": "8", "title": "Dreams [Edited]", "artist": "Fleetwood Mac"},
        {"id": "9", "title": "Dreams (Acoustic)", "artist": "Fleetwood Mac"},
    ]))
    assert _ids(library.find_songs("Dreams", "Fleetwood Mac")) == ["7", "8"]


def test_a_remix_by_an_artist_called_clean_isnt_the_song_asked_for(make_library):
    library = make_library(_songs_found([
        {"id": "12", "title": "Dreams (feat. Clean Bandit)", "artist": "Fleetwood Mac"},
        {"id": "13", "title": "Dreams (Clean Bandit Remix)", "artist": "Fleetwood Mac"},
        {"id": "14", "title": "Dreams - Clean Bandit Remix", "artist": "Fleetwood Mac"},
    ]))
    # The credit is on the song itself, but isn't a clean version of it; the remixes aren't the song.
    found = library.find_songs("Dreams", "Fleetwood Mac")
    assert _ids(found) == ["12"] and not is_clean(found[0])
    assert _ids(library.find_songs("Dreams Remix", "Fleetwood Mac")) == ["13", "14"]
    assert not any(is_clean(song) for song in library.find_songs("Dreams Remix", "Fleetwood Mac"))


def test_a_real_title_with_clean_or_edited_in_it_is_its_title(make_library):
    library = make_library(_songs_found([
        {"id": "10", "title": "Clean", "artist": "Taylor Swift", "album": "1989"},
        {"id": "11", "title": "Edited Highlights", "artist": "Taylor Swift", "album": "Highlights"},
    ]))
    found = library.find_songs("Clean", "Taylor Swift")
    assert _ids(found) == ["10"] and not is_clean(found[0])
    found = library.find_songs("Edited Highlights", None)
    assert _ids(found) == ["11"] and not is_clean(found[0])


def test_an_artist_asked_for_with_and_is_found_with_an_ampersand_or_a_plus(make_library):
    # Claude writes "and" where the library has "&" or "+".
    library = make_library(_songs_found([
        {"id": "20", "title": "Otis", "artist": "JAY-Z & Kanye West", "album": "Watch the Throne"},
        {"id": "21", "title": "Otis", "artist": "Kanye West", "album": "Made Up"},
        {"id": "22", "title": "The Boxer", "artist": "Simon & Garfunkel", "album": "Bridge over Troubled Water"},
    ]))
    assert _ids(library.find_songs("Otis", "Jay-Z and Kanye West")) == ["20"]
    assert _ids(library.find_songs("The Boxer", "Simon and Garfunkel")) == ["22"]
    library = make_library({
        "search3": {"searchResult3": {"album": [{"id": "f1", "name": "Lungs", "artist": "Florence + the Machine"}]}},
        "getAlbum": lambda params: {"album": {"song": [{"id": "f1t1", "title": "Dog Days Are Over"}]}},
    })
    assert _ids(library.find_album_tracks("Lungs", "Florence and the Machine")) == ["f1t1"]


def test_an_artist_is_still_every_word_of_their_name(make_library):
    library = make_library(_songs_found([{"id": "23", "title": "Dreams", "artist": "Mac Miller"}]))
    assert library.find_songs("Dreams", "Fleetwood Mac") == []


def test_a_song_isnt_found_when_navidrome_cant_be_asked(make_library):
    library = make_library({"search3": requests.ConnectionError("http://nd/rest/search3?t=abc123token")})
    with pytest.raises(requests.RequestException):
        library.find_songs("Gooba", "6ix9ine")


# Rumours: the album, the live one, one by someone else, and an edited one.
ALBUMS = {
    "a1": ("Rumours", "Fleetwood Mac", [{"id": "t1", "title": "Second Hand News"}, {"id": "t2", "title": "Dreams"},
                                         {"id": "t3", "title": "Never Going Back Again"}]),
    "a2": ("Rumours (Live)", "Fleetwood Mac", [{"id": "t4", "title": "Dreams"}]),
    "a3": ("Rumours", "Other", [{"id": "t5", "title": "Dreams"}]),
    "a4": ("Rumours [Edited Version]", "Fleetwood Mac", [{"id": "t6", "title": "Dreams"}]),
}


def _albums_found(*ids):
    return {
        "search3": {"searchResult3": {"album": [{"id": id, "name": ALBUMS[id][0], "artist": ALBUMS[id][1]}
                                                for id in ids]}},
        "getAlbum": lambda params: {"album": {"song": ALBUMS[params["id"]][2]}},
    }


def test_an_albums_tracks_are_every_track_of_each_matching_album(make_library):
    library = make_library(_albums_found("a1", "a2", "a3"))
    tracks = library.find_album_tracks("Rumours", "Fleetwood Mac")
    assert _ids(tracks) == ["t1", "t2", "t3"] and {track["album"] for track in tracks} == {"Rumours"}
    assert _ids(library.find_album_tracks("Rumours", None)) == ["t1", "t2", "t3", "t5"]


def test_each_albums_tracks_carry_its_id_and_artist(make_library):
    # So two albums called Rumours are told apart, and named by the album's artist, not a track's.
    library = make_library(_albums_found("a1", "a3"))
    tracks = library.find_album_tracks("Rumours", None)
    assert [(track["albumId"], track["albumArtist"]) for track in tracks] == (
        [("a1", "Fleetwood Mac")] * 3 + [("a3", "Other")])


def test_a_live_album_isnt_the_album_asked_for(make_library):
    library = make_library(_albums_found("a1", "a2"))
    assert _ids(library.find_album_tracks("Rumours", "Fleetwood Mac")) == ["t1", "t2", "t3"]
    tracks = library.find_album_tracks("Rumours Live", "Fleetwood Mac")
    assert _ids(tracks) == ["t4"] and {track["album"] for track in tracks} == {"Rumours (Live)"}


def test_an_edited_album_is_one_of_the_albums_found_and_its_tracks_say_so(make_library):
    library = make_library(_albums_found("a1", "a4"))
    tracks = library.find_album_tracks("Rumours", "Fleetwood Mac")
    assert _ids(tracks) == ["t1", "t2", "t3", "t6"]
    assert [is_clean(track) for track in tracks] == [False, False, False, True]


def test_albums_are_searched_for_as_they_are_when_played(make_library):
    library = make_library(_albums_found("a1"))
    library.find_album_tracks("Rumours", "Fleetwood Mac")
    endpoint, params = library._session.requests[0]
    assert (endpoint, params["query"], params["albumCount"], params["songCount"]) == ("search3", "Rumours Fleetwood Mac", 10, 0)


def test_the_title_alone_is_searched_when_title_and_artist_find_no_album(make_library):
    library = make_library({**_albums_found("a1"), "search3": lambda params: {"searchResult3": {
        "album": [{"id": "a1", "name": "Rumours", "artist": "Fleetwood Mac"}] if params["query"] == "Rumours" else []}}})
    assert _ids(library.find_album_tracks("Rumours", "Fleetwood Mac")) == ["t1", "t2", "t3"]
    assert [params["query"] for endpoint, params in library._session.requests if endpoint == "search3"] == [
        "Rumours Fleetwood Mac", "Rumours"]


def test_no_album_is_no_tracks(make_library):
    library = make_library({"search3": {"searchResult3": {}}})
    assert library.find_album_tracks("Rumours", "Fleetwood Mac") == []
    assert [endpoint for endpoint, _ in library._session.requests] == ["search3", "search3"]


def test_the_new_music_playlist_is_only_ever_the_exact_name(make_player):
    # A near miss shares a word with the name but isn't it: it must not play as the new music.
    answers = {
        "getPlaylists": {"playlists": {"playlist": [{"id": "p1", "name": "Gym Shuffle"},
                                                    {"id": "p2", "name": "Discovery Weekly"}]}},
        "getPlaylist": {"playlist": {"entry": SONGS}},
    }
    player = make_player(answers)
    result = player.play_playlist("Discovery Shuffle", exact=True)
    assert result["success"] is False and result["missing"] is True and player._queue == []
    # Said aloud it still finds the closest, as it always has.
    assert make_player(answers).play_playlist("discovery shuffle")["success"] is True

    # The exact one plays, however it's cased or punctuated, and only its own songs.
    answers["getPlaylists"]["playlists"]["playlist"].append({"id": "p3", "name": "discovery  shuffle!"})
    exact = make_player(answers)
    assert exact.play_playlist("Discovery Shuffle", exact=True) == {"success": True,
                                                                    "message": "Playing discovery  shuffle!"}
    # Liked songs are no playlist, so a setting naming them finds none.
    assert make_player({"getStarred2": {"starred2": {"song": SONGS}}, "getPlaylists": {}}).play_playlist(
        "liked songs", exact=True)["missing"] is True


def test_the_new_music_playlist_nova_makes_comes_before_anothers_of_the_name(make_player):
    # Another user's public one of the same name is listed first, as Navidrome may: Nova's own plays.
    answers = {
        "getPlaylists": {"playlists": {"playlist": [{"id": "p1", "name": "New Music", "owner": "someone"},
                                                    {"id": "p2", "name": "New Music", "owner": "Nova"}]}},
        "getPlaylist": lambda params: {"playlist": {"entry": SONGS if params["id"] == "p2" else []}},
    }
    player = make_player(answers)
    assert player.play_playlist("New Music", exact=True)["success"] is True
    assert [asked["id"] for endpoint, asked in player._session.requests if endpoint == "getPlaylist"] == ["p2"]


def test_the_new_music_playlist_nova_made_plays_by_its_id_and_never_another_of_the_name(make_player):
    # His own of the same name, the same owner, is listed first: only the recorded id plays.
    answers = {
        "getPlaylists": {"playlists": {"playlist": [{"id": "p1", "name": "New Music", "owner": "Nova"},
                                                    {"id": "p2", "name": "new music!", "owner": "Nova"}]}},
        "getPlaylist": lambda params: {"playlist": {"entry": SONGS if params["id"] == "p2" else []}},
    }
    player = make_player(answers)
    assert player.play_playlist("New Music", exact=True, playlist_id="p2") == {"success": True,
                                                                              "message": "Playing new music!"}
    assert [asked["id"] for endpoint, asked in player._session.requests if endpoint == "getPlaylist"] == ["p2"]

    # Gone, it is missing, and the one of the same name isn't played in its place.
    gone = make_player(answers)
    result = gone.play_playlist("New Music", exact=True, playlist_id="p9")
    assert result["success"] is False and result["missing"] is True and gone._queue == []
    assert not [1 for endpoint, _ in gone._session.requests if endpoint == "getPlaylist"]


def test_newest_albums_since(make_library):
    # Newest first, as Navidrome lists them, with the fractional seconds and Z it writes - and one a
    # little before the cut off, which isn't kept. Offsets are the same moment wherever the clock is.
    since = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc).timestamp()
    library = make_library({"getAlbumList2": {"albumList2": {"album": [
        {"id": "a1", "name": "Blue Hours", "artist": "Band One", "songCount": 10, "created": "2026-11-15T20:01:02.123456789Z"},
        {"id": "a2", "name": "Low Tide", "artist": "Band Two", "songCount": 8, "created": "2026-11-09T12:00:00Z"},
        {"id": "a3", "name": "Plain Sight", "artist": "Band Two", "songCount": 6, "created": "2026-11-09T13:30:00+01:00"},
        {"id": "a4", "name": "Old News", "artist": "Band Three", "songCount": 12, "created": "2026-11-09T11:59:59Z"},
        {"id": "a5", "name": "No Date", "artist": "Band Four", "songCount": 3}]}}})
    found, whole = library.newest_albums(since)
    assert [(album["id"], album["name"], album["artist"], album["songCount"]) for album in found] == [
        ("a1", "Blue Hours", "Band One", 10), ("a2", "Low Tide", "Band Two", 8), ("a3", "Plain Sight", "Band Two", 6)]
    assert whole is True
    # One page was enough, as it reached back past the time.
    assert [(endpoint, asked["type"], asked["size"], asked["offset"])
            for endpoint, asked in library._session.requests] == [("getAlbumList2", "newest", 200, 0)]


def test_newest_albums_with_none_in_the_library(make_library):
    assert make_library({"getAlbumList2": {"albumList2": {}}}).newest_albums(0) == ([], True)


def _newest_pages(total, newer):
    """Navidrome's newest albums, a page at a time: total of them, the first newer of which came in this week."""
    def answer(params):
        start, size = int(params["offset"]), int(params["size"])
        return {"albumList2": {"album": [
            {"id": f"n{n}", "name": f"Album {n}", "artist": f"Band {n % 7}", "songCount": 2,
             "created": "2026-11-15T10:00:00Z" if n < newer else "2026-10-01T10:00:00Z"}
            for n in range(start, min(start + size, total))]}}
    return answer


def test_newest_albums_are_read_page_by_page_past_the_first(make_library):
    # 450 came in this week: past the first page and the second, so all three pages are read, and no more.
    since = datetime(2026, 11, 9, tzinfo=timezone.utc).timestamp()
    library = make_library({"getAlbumList2": _newest_pages(1000, 450)})
    found, whole = library.newest_albums(since)
    assert (len(found), whole) == (450, True)
    assert [album["id"] for album in found[:2]] == ["n0", "n1"] and found[-1]["id"] == "n449"
    assert [asked["offset"] for _, asked in library._session.requests] == [0, 200, 400]
    # A library which runs out on a short page is all read too, with nothing older in it.
    found, whole = make_library({"getAlbumList2": _newest_pages(250, 250)}).newest_albums(since)
    assert (len(found), whole) == (250, True)


def test_newest_albums_stop_after_the_most_pages_and_say_theres_more(make_library):
    since = datetime(2026, 11, 9, tzinfo=timezone.utc).timestamp()
    library = make_library({"getAlbumList2": _newest_pages(10_000, 10_000)})
    found, whole = library.newest_albums(since)
    assert (len(found), whole) == (20 * 200, False)
    assert len(library._session.requests) == 20


def test_a_playlist_which_isnt_there_is_marked_missing(make_player):
    player = make_player({"getPlaylists": {"playlists": {"playlist": [{"id": "p1", "name": "Chill"}]}}})
    result = player.play_playlist("Discovery Shuffle")
    assert result["success"] is False and result["missing"] is True
    assert result["message"] == "I couldn't find a playlist called Discovery Shuffle."


def test_a_scan_is_asked_for_and_a_refusal_is_logged(make_library, caplog):
    assert make_library({"startScan": {"scanStatus": {"scanning": True}}}).start_scan() is True
    refused = make_library({"startScan": {"status": "failed", "error": {"code": 50, "message": "not authorized"}}})
    assert refused.start_scan() is False and "admin" in caplog.text


def test_a_refused_scan_is_logged_once_as_a_warning_about_the_admin(make_library, caplog):
    library = make_library({"startScan": {"status": "failed", "error": {"code": 50, "message": "not authorized"}}})
    with caplog.at_level(logging.DEBUG):
        assert library.start_scan() is False
    assert [(record.levelno, "admin" in record.getMessage()) for record in caplog.records] == [(logging.WARNING, True)]


def test_a_scan_which_cant_be_asked_for_is_false_and_never_logs_the_token(make_library, caplog):
    library = make_library({"startScan": requests.ConnectionError("http://nd/rest/startScan?t=abc123token")})
    assert library.start_scan() is False
    assert "abc123token" not in caplog.text and "admin" not in caplog.text
    assert "ConnectionError" in caplog.text
    library = make_library({"startScan": {"status": "failed", "error": {"code": 40, "message": "Wrong password"}}})
    assert library.start_scan() is False and "admin" not in caplog.text


@pytest.mark.parametrize("body", [[], ["subsonic-response"], {"subsonic-response": []},
                                  {"subsonic-response": "ok"}])
def test_an_answer_that_isnt_a_dictionary_is_a_valueerror(make_library, body):
    # Callers already handle ValueError as an answer they couldn't read; AttributeError they don't.
    library = make_library()
    library._session.get = lambda url, params=None, timeout=None, stream=False: FakeResponse(body)

    with pytest.raises(ValueError):
        library._call("ping")


def test_a_scan_which_wasnt_answered_with_json_is_false(make_library):
    class NotJson(FakeResponse):
        def json(self):
            raise ValueError("no json")

    library = make_library()
    library._session.get = lambda *args, **kwargs: NotJson(None)
    assert library.start_scan() is False


def test_a_scan_asked_for_with_no_network_isnt_sent(make_library, monkeypatch):
    monkeypatch.setattr(network, "online", lambda: False)
    library = make_library()
    assert library.start_scan() is False and library._session.requests == []


def test_the_scan_is_the_subsonic_one(make_library):
    library = make_library({"startScan": {"scanStatus": {"scanning": True}}})
    library.start_scan()
    assert [endpoint for endpoint, _ in library._session.requests] == ["startScan"]


def test_the_player_is_a_library_which_signs_in_as_it_did(make_player):
    player = make_player()
    assert isinstance(player, NavidromeLibrary)
    assert player.url == "http://navidrome.local:4533" and player.username == "nova"
    signed = player._sign_in()
    assert signed["t"] == hashlib.md5(("secret" + signed["s"]).encode()).hexdigest()


# Playing the queue

def test_the_next_song_plays_when_one_finishes(make_player):
    player = make_player()
    player._start(SONGS)
    _playing(player).finish()
    _until(lambda: len(player.speaker_started) == 2)
    assert player.speaker_started[1].song["id"] == "2"


def test_skipping_moves_on_once_not_twice(make_player):
    player = make_player()
    player._start(SONGS)
    first = _playing(player)
    assert player.skip()["silent"]
    _until(lambda: len(player.speaker_started) == 2)
    time.sleep(0.05)
    assert first.stopped and [p.song["id"] for p in player.speaker_started] == ["1", "2"]


def test_going_back_soon_after_a_song_starts_goes_to_the_one_before(make_player):
    player = make_player()
    player._start(SONGS)
    _playing(player)
    player.skip()
    _until(lambda: len(player.speaker_started) == 2)
    assert player.previous()["message"] == "Previous track"
    _until(lambda: len(player.speaker_started) == 3)
    assert player.speaker_started[2].song["id"] == "1"

    # Further in, it starts the song again instead.
    player.speaker_started[2].position = 10 * FakeSpeaker.sample_rate
    assert player.previous()["message"] == "Starting it again"


def test_repeating_a_track_plays_it_again(make_player):
    player = make_player()
    player._start(SONGS)
    player.repeat("track")
    _playing(player).finish()
    _until(lambda: len(player.speaker_started) == 2)
    assert player.speaker_started[1].song["id"] == "1"


def test_the_end_of_the_queue_is_the_end(make_player):
    player = make_player()
    player._start(SONGS[:1])
    _playing(player).finish()
    time.sleep(0.1)
    assert len(player.speaker_started) == 1
    assert player.current_track() == {"success": False, "message": "Nothing playing"}


def test_songs_which_wont_play_stop_the_queue(make_player):
    player = make_player()
    player.repeat("all")
    player._start(SONGS)
    for _ in range(3):
        playback = _playing(player)
        playback.duration = 0
        playback.finish()
        _until(lambda: playback is not player.speaker_started[-1] or not player._queue)
    _until(lambda: not player._queue)


# Making way for the assistant

def test_the_music_pauses_while_the_assistant_talks_and_comes_back(make_player):
    player = make_player()
    player._start(SONGS)
    playing = _playing(player)

    player.pause_for_speech()
    assert playing.paused and player.was_playing()
    player.resume_after_speech()
    assert not playing.paused


def test_a_song_asked_for_waits_until_the_reply_is_over(make_player):
    # Spotify starts the new song straight away, under the reply saying what it is.
    player = make_player({"getStarred2": {"starred2": {"song": SONGS}}})
    player.pause_for_speech()
    assert not player.was_playing()
    player.play_playlist("liked songs")
    assert _playing(player).paused
    player.resume_after_speech()
    assert not player.speaker_started[-1].paused


def test_paused_by_request_it_stays_paused(make_player):
    player = make_player()
    player._start(SONGS)
    playing = _playing(player)
    player.pause_for_speech()
    player.pause()
    player.cancel_resume()
    player.resume_after_speech()
    assert playing.paused


def test_the_volume_changes_the_song_playing(make_player):
    player = make_player()
    player._start(SONGS)
    playing = _playing(player)
    assert player.set_volume(20)["success"]
    assert playing.volume == pytest.approx(0.2) and player.get_volume() == 20


# Handing the music to another player

def _state(**changes):
    """What hand_over gives, for a test which hasn't a player to hand it over."""
    return {"queue": list(SONGS), "unshuffled": list(SONGS), "index": 0, "position": 0.0,
            "repeat": "off", "shuffle": False, **changes}


class FakeFfmpeg:
    """Stands in for ffmpeg, which hears what it was asked and 'decodes' one frame of silence."""

    asked: list = []

    def __init__(self, command, **kwargs):
        FakeFfmpeg.asked.append(command)
        read, write = os.pipe()
        os.write(write, bytes(8))
        os.close(write)
        self.stdin, self.stdout, self.stderr = io.BytesIO(), os.fdopen(read, "rb"), io.BytesIO()

    def poll(self):
        return 0

    def wait(self):
        return 0


def _decoding(player, monkeypatch):
    """
    Has the player decode for real, but through a fake ffmpeg, and returns how ffmpeg is
    asked to start each song, and which song each is - see _decoded.
    """

    class Download:
        def iter_content(self, size):
            return iter(())

        def close(self):
            pass

    FakeFfmpeg.asked = []
    opened = []
    del player._decode
    monkeypatch.setattr(player, "_open_song", lambda song_id: opened.append(song_id) or Download())
    monkeypatch.setattr(subprocess, "Popen", FakeFfmpeg)
    return opened


def _decoded(player, count):
    """Waits for this many songs to have started, and has the speaker decode them, as it plays them."""
    _until(lambda: len(player.speaker_started) >= count)
    for playback in player.speaker_started[:count]:
        if not getattr(playback, "decoded", False):
            playback.decoded = True
            list(playback.song)
    return FakeFfmpeg.asked


def _seeks(command):
    """The seconds a decode was asked to start at - after the input - or None, from its start."""
    after = command[command.index("pipe:0") + 1:]
    return float(after[1]) if after[:1] == ["-ss"] else None


def test_has_music_and_is_playing(make_player):
    player = make_player()
    assert not player.has_music() and not player.is_playing()

    player._start(SONGS[:1])
    playing = _playing(player)
    assert player.has_music() and player.is_playing()

    player.pause()
    assert player.has_music() and not player.is_playing()

    # Held for the assistant to talk, music which was playing still is, as it carries on after.
    player.play()
    player.pause_for_speech()
    assert playing.paused and player.is_playing()
    player.resume_after_speech()

    player.pause()
    player.pause_for_speech()
    assert player.has_music() and not player.is_playing()
    player.resume_after_speech()

    player.play()
    playing.finish()
    _until(lambda: not player.has_music())
    assert not player.is_playing()


def test_hand_over_gives_queue_place_and_modes(make_player):
    player = make_player()
    player._start(SONGS)
    _playing(player)
    player.skip()
    _until(lambda: len(player.speaker_started) == 2)
    player.shuffle(True)
    player.repeat("track")
    shuffled = list(player._queue)
    player.speaker_started[1].position = int(12.5 * FakeSpeaker.sample_rate)

    state = player.hand_over()
    assert state == {"queue": shuffled, "unshuffled": SONGS, "index": 1, "position": 12.5,
                     "repeat": "track", "shuffle": True}
    assert shuffled[:2] == [SONGS[0], SONGS[1]]


def test_hand_over_with_nothing_is_none(make_player):
    player = make_player()
    assert player.hand_over() is None

    # Nor has a queue which has run out any music to hand over.
    player._start(SONGS[:1])
    _playing(player).finish()
    _until(lambda: not player.has_music())
    assert player.hand_over() is None


def test_hand_over_empties_and_stops_this_player(make_player):
    player = make_player()
    player._start(SONGS)
    playing = _playing(player)
    player.pause_for_speech()

    assert player.hand_over() is not None
    assert playing.stopped and not player.has_music() and not player.is_playing() and not player.was_playing()
    assert player.now_playing() is None

    # Carrying on after the reply has nothing to play, nor does anything start by itself.
    player.resume_after_speech()
    time.sleep(0.1)
    assert len(player.speaker_started) == 1 and not player.has_music()


def test_take_over_starts_at_the_place(make_player, monkeypatch):
    player = make_player()
    opened = _decoding(player, monkeypatch)
    state = _state(index=2, position=61.5)

    assert player.take_over(state) == {"success": True, "message": "Playing"}
    asked = _decoded(player, 1)
    assert opened == ["3"] and _seeks(asked[0]) == pytest.approx(61.5)
    assert asked[0][asked[0].index("pipe:0") - 1:][:3] == ["-i", "pipe:0", "-ss"]

    # The songs after it start at their beginning, and the lists are the player's own.
    player.speaker_started[0].finish()
    asked = _decoded(player, 2)
    assert opened == ["3", "4"] and _seeks(asked[1]) is None
    assert player._queue is not state["queue"] and player._unshuffled is not state["unshuffled"]


def test_a_song_taken_over_and_repeated_starts_again_from_the_beginning(make_player, monkeypatch):
    player = make_player()
    opened = _decoding(player, monkeypatch)
    player.take_over(_state(index=1, position=30.0, repeat="track"))
    _decoded(player, 1)
    player.speaker_started[0].finish()
    asked = _decoded(player, 2)
    assert opened == ["2", "2"] and _seeks(asked[0]) == pytest.approx(30.0) and _seeks(asked[1]) is None


def test_hand_over_twice_counts_the_first_offset(make_player):
    # Moved at 60 seconds, then moved again 10 seconds on: the song is 70 seconds in.
    first, second = make_player(), make_player()
    first.take_over(_state(position=60.0))
    _playing(first).position = 10 * FakeSpeaker.sample_rate
    state = first.hand_over()
    assert state["position"] == pytest.approx(70.0)

    second.take_over(state)
    _playing(second).position = 5 * FakeSpeaker.sample_rate
    assert second.hand_over()["position"] == pytest.approx(75.0)


def test_hand_over_before_the_song_has_started_gives_where_it_will_start(make_player):
    # Held, the player's own thread can't have started the song yet, and what the old
    # one had played is no part of where this one will be.
    player = make_player()
    player._start(SONGS)
    _playing(player).position = 30 * FakeSpeaker.sample_rate
    with player._lock:
        player.take_over(_state(index=1, position=60.0))
        assert player.hand_over()["position"] == pytest.approx(60.0)


def test_the_next_song_starts_with_nothing_played(make_player):
    # Between one song finishing and the next starting, how far the first got is no part of the next.
    player = make_player()
    places = []
    player._decode = lambda song, start=0.0: places.append(player.now_playing()["position"]) or song
    player._start(SONGS)
    first = _playing(player)
    first.position = 150 * FakeSpeaker.sample_rate
    first.finish()
    _until(lambda: len(places) == 2)
    assert places == [0.0, 0.0]


def test_a_speaker_which_cant_say_how_far_it_got_counts_nothing_played(make_player):
    player = make_player()
    player._start(SONGS)
    playing = _playing(player)
    del playing.position
    assert player.hand_over()["position"] == 0.0


def test_take_over_during_a_reply_waits(make_player):
    player = make_player()
    player.pause_for_speech()
    assert player.take_over(_state(position=20.0))["success"]
    playing = _playing(player)
    assert playing.paused

    player.resume_after_speech()
    assert not playing.paused


def test_paused_music_plays_once_taken_over(make_player):
    first, second = make_player(), make_player()
    first._start(SONGS)
    _playing(first)
    first.pause()
    second._start(SONGS[:1])
    waiting = _playing(second)
    second.pause()

    second.take_over(first.hand_over())
    _until(lambda: len(second.speaker_started) == 2)
    assert waiting.stopped and not second.speaker_started[1].paused and second.is_playing()


def test_skip_after_take_over_starts_the_next_at_zero(make_player, monkeypatch):
    player = make_player()
    opened = _decoding(player, monkeypatch)
    player.take_over(_state(position=45.0))
    _decoded(player, 1)
    player.skip()
    asked = _decoded(player, 2)
    assert opened == ["1", "2"] and _seeks(asked[0]) == pytest.approx(45.0) and _seeks(asked[1]) is None


def test_a_jump_before_the_song_has_started_drops_the_offset(make_player, monkeypatch):
    player = make_player()
    opened = _decoding(player, monkeypatch)
    with player._lock:
        player.take_over(_state(position=45.0))
        player.skip()
    asked = _decoded(player, 1)
    assert opened == ["2"] and _seeks(asked[0]) is None
    assert player.hand_over()["position"] == 0.0


# Real audio

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_song_is_decoded_to_stereo_at_the_speakers_rate(make_player, monkeypatch):
    sound = io.BytesIO()
    with wave.open(sound, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(22050)
        file.writeframes((np.sin(np.arange(22050) / 10) * 10000).astype("<i2").tobytes())

    class Download:
        def iter_content(self, size):
            data = sound.getvalue()
            for start in range(0, len(data), size):
                yield data[start:start + size]

        def close(self):
            pass

    player = make_player()
    monkeypatch.setattr(player, "_open_song", lambda song_id: Download())
    pieces = list(NavidromePlayer._decode(player, {"id": "1", "title": "Tone"}))
    audio = np.concatenate(pieces)
    assert audio.shape[1] == 2 and abs(len(audio) - 48000) < 2000
    assert np.allclose(audio[:, 0], audio[:, 1])


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_song_is_decoded_from_where_it_was_taken_over(make_player, monkeypatch):
    sound = io.BytesIO()
    with wave.open(sound, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(22050)
        file.writeframes((np.sin(np.arange(22050) / 10) * 10000).astype("<i2").tobytes())

    class Download:
        def iter_content(self, size):
            yield sound.getvalue()

        def close(self):
            pass

    player = make_player()
    monkeypatch.setattr(player, "_open_song", lambda song_id: Download())
    audio = np.concatenate(list(NavidromePlayer._decode(player, {"id": "1", "title": "Tone"}, 0.75)))
    assert abs(len(audio) - 12000) < 2000


def test_music_stopped_before_talking_stays_stopped(make_player):
    # Stopped, then the next thing said to the assistant started it again, since
    # every conversation ended by un-pausing it.
    player = make_player()
    player._start(SONGS)
    playing = _playing(player)
    player.pause()
    player.pause_for_speech()
    player.resume_after_speech()
    assert playing.paused and not player.was_playing()


def test_resume_asked_for_while_talking_carries_on_after(make_player):
    player = make_player()
    player._start(SONGS)
    playing = _playing(player)
    player.pause()
    player.pause_for_speech()
    player.play()
    assert playing.paused
    player.resume_after_speech()
    assert not playing.paused


def test_sharing_only_songs_isnt_a_playlist_match(make_player):
    # "Light songs" - Whisper's "liked songs" - played "Car Songs".
    player = make_player({"getPlaylists": {"playlists": {"playlist": [{"id": "p1", "name": "Car Songs"}]}},
                          "getPlaylist": {"playlist": {"entry": SONGS}},
                          "getSongsByGenre": {"songsByGenre": {"song": []}}})
    assert player.play("rainy day songs", search_type="playlist")["success"] is False
    assert player.play_playlist("car songs")["message"] == "Playing Car Songs"


# For the settings page

RECORD = [{"id": "1", "title": "Song 1", "artist": "Band", "album": "Record", "duration": 200, "coverArt": "al-1"},
          {"id": "2", "title": "Song 2", "artist": "Band", "album": "Record", "duration": 180, "coverArt": "al-1"}]


class FakeImage:
    headers = {"Content-Type": "image/jpeg"}
    content = b"a cover"

    def raise_for_status(self):
        pass


def test_the_page_is_told_whats_playing_and_how_far_through(make_player):
    player = make_player()
    assert player.now_playing() is None
    player._start(RECORD)
    _playing(player).position = 48000 * 30
    assert player.now_playing() == {"track": "Song 1", "artist": "Band", "album": "Record", "playing": True,
                                    "position": 30.0, "duration": 200, "cover": "al-1"}

    # Paused while the assistant talks, it's still playing, as it carries on after -
    # but not once it's been asked to pause.
    player.pause_for_speech()
    assert player.now_playing()["playing"]
    player.pause()
    player.cancel_resume()
    player.resume_after_speech()
    assert not player.now_playing()["playing"]


def test_a_cover_is_fetched_once_for_a_whole_album(make_player):
    player = make_player()
    player._start(RECORD)
    _playing(player)
    asked = []
    player._session.get = lambda url, params=None, timeout=None: asked.append(params) or FakeImage()

    assert player.cover() == (b"a cover", "image/jpeg")
    player.skip()
    assert player.cover() == (b"a cover", "image/jpeg")
    assert len(asked) == 1 and asked[0]["id"] == "al-1" and "p" not in asked[0]


def test_the_page_is_offered_the_playlists_by_name(make_player):
    player = make_player({"getPlaylists": {"playlists": {"playlist": [{"id": "p2", "name": "workout"},
                                                                      {"id": "p1", "name": "Chill"}]}}})
    assert player.playlists() == ["Chill", "workout"]
    player._session.answers["getPlaylists"] = requests.ConnectionError("down")
    assert player.playlists() == []


# What Navidrome says of a song's file, sound and lyrics, for listening (decision 0022)

class FakeHttp:
    """One answer from Navidrome's native API or its stream: a status, and JSON or bytes."""

    def __init__(self, status=200, body=None, content=b"", kind="application/json"):
        self.status_code = status
        self.body = body
        self.content = content
        self.headers = {"Content-Type": kind}

    def json(self):
        if self.body is None:
            raise ValueError("no json")
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            # As requests words it, with the whole signed address.
            raise requests.HTTPError(f"{self.status_code} Error for url: http://navidrome.local:4533/rest/stream"
                                     f"?u=nova&t=abc123token&s=salt", response=self)


class FakeNative:
    """
    Stands in for Navidrome's native API: a sign-in which hands out a token, and the
    songs, answered only for the token handed out last.
    """

    def __init__(self, songs=None, password="secret"):
        self.songs = songs or {}
        self.password = password
        self.token = None
        self.signed_in = 0
        self.asked = []

    def post(self, url, json=None, timeout=None):
        assert url == "http://navidrome.local:4533/auth/login" and timeout
        if json != {"username": "nova", "password": self.password}:
            return FakeHttp(401, {"error": "Invalid username or password"})
        self.signed_in += 1
        self.token = f"tok-{self.signed_in}"
        return FakeHttp(200, {"id": "u1", "name": "Nova", "token": self.token})

    def get(self, url, headers=None, timeout=None, params=None):
        assert timeout and params is None
        song_id = url.rsplit("/api/song/", 1)[1]
        self.asked.append((song_id, headers))
        if headers != {"x-nd-authorization": f"Bearer {self.token}"}:
            return FakeHttp(401, {"error": "Not authenticated"})
        answer = self.songs.get(song_id)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return FakeHttp(404, {"error": "data not found"})
        return answer if isinstance(answer, FakeHttp) else FakeHttp(200, answer)


def _song_answer(path, size=1234, updated="2026-11-15T10:00:00Z"):
    return {"id": "s", "title": "Endless", "path": path, "size": size, "updatedAt": updated,
            "libraryPath": "/music"}


def _native_library(native):
    library = NavidromeLibrary("http://navidrome.local:4533/", "nova", "secret")
    library._session = native
    return library


def test_a_songs_file_is_read_from_the_native_api_signed_in_once():
    native = FakeNative({"a1": _song_answer("Gunna/The Last Wun/endless.mp3"),
                         "b2": _song_answer("Drake/Song.flac", 99, "2026-11-16T09:30:00.123Z")})
    library = _native_library(native)
    assert library.song_file("a1") == {"path": "Gunna/The Last Wun/endless.mp3", "size": 1234,
                                       "updated": "2026-11-15T10:00:00Z"}
    assert library.song_file("b2") == {"path": "Drake/Song.flac", "size": 99, "updated": "2026-11-16T09:30:00.123Z"}
    assert native.signed_in == 1
    assert native.asked[-1] == ("b2", {"x-nd-authorization": "Bearer tok-1"})


def test_a_songs_file_signs_in_again_when_the_token_has_run_out():
    native = FakeNative({"a1": _song_answer("A/a.mp3")})
    library = _native_library(native)
    library.song_file("a1")
    # Navidrome forgets the token - a restart of it, or the session timing out.
    native.token = "tok-gone"
    assert library.song_file("a1")["path"] == "A/a.mp3"
    assert native.signed_in == 2
    assert [headers["x-nd-authorization"] for _, headers in native.asked] == ["Bearer tok-1", "Bearer tok-1",
                                                                             "Bearer tok-2"]


def test_two_songs_asked_for_at_once_with_a_stale_token_sign_in_again_once(monkeypatch):
    # Listening and a swap share one library; Navidrome limits how often it can be signed in to.
    native = FakeNative({"a1": _song_answer("A/a.mp3"), "b2": _song_answer("B/b.mp3")})
    library = _native_library(native)
    library.song_file("a1")
    native.token = "tok-gone"
    # Both threads are refused the stale token before either signs in again.
    both_refused = threading.Barrier(2)
    get = native.get

    def get_together(url, headers=None, timeout=None, params=None):
        answer = get(url, headers=headers, timeout=timeout, params=params)
        if answer.status_code == 401:
            both_refused.wait(5)
        return answer

    native.get = get_together
    found = []
    threads = [threading.Thread(target=lambda song=song: found.append(library.song_file(song)["path"]))
               for song in ("a1", "b2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert sorted(found) == ["A/a.mp3", "B/b.mp3"]
    assert native.signed_in == 2


def test_a_songs_id_cant_reach_another_address():
    native = FakeNative({"..%2Fauth%2Flogin": _song_answer("A/a.mp3")})
    assert _native_library(native).song_file("../auth/login")["path"] == "A/a.mp3"


@pytest.mark.parametrize("native, says", [
    (FakeNative(password="other"), "accept"),
    (FakeNative({"a1": FakeHttp(500, {"error": "boom"})}), "500"),
    (FakeNative({}), "404"),
    (FakeNative({"a1": FakeHttp(200, None)}), "read"),
    (FakeNative({"a1": FakeHttp(200, {"size": 3})}), "path"),
    (FakeNative({"a1": requests.ConnectionError(
        "HTTPConnectionPool(host='navidrome.local', port=4533): /api/song/a1 x-nd-authorization: Bearer tok-1")}),
     "ConnectionError"),
])
def test_a_songs_file_which_cant_be_read_is_an_error_naming_no_address_token_or_password(native, says):
    library = _native_library(native)
    with pytest.raises(NavidromeError) as raised:
        library.song_file("a1")
    text = str(raised.value)
    assert says in text
    for secret in ("secret", "tok-1", "navidrome.local", "4533", "http"):
        assert secret not in text
    # Nothing behind it to show the address in a traceback either.
    assert raised.value.__cause__ is None and (raised.value.__context__ is None or raised.value.__suppress_context__)


def test_a_token_refused_straight_after_signing_in_is_an_error_not_a_loop():
    native = FakeNative({"a1": _song_answer("A/a.mp3")})
    # Navidrome hands out tokens it then won't take - a clock far off, say.
    native.get = lambda url, headers=None, timeout=None, params=None: FakeHttp(401, {"error": "Not authenticated"})
    with pytest.raises(NavidromeError) as raised:
        _native_library(native).song_file("a1")
    assert "401" in str(raised.value) and "tok-" not in str(raised.value)
    assert native.signed_in == 2


def test_a_wrong_password_for_the_native_api_is_the_wrong_login_code():
    with pytest.raises(NavidromeError) as raised:
        _native_library(FakeNative(password="other")).song_file("a1")
    assert raised.value.code == WRONG_LOGIN


def test_a_songs_file_isnt_asked_for_with_no_network(monkeypatch):
    monkeypatch.setattr(network, "online", lambda: False)
    native = FakeNative({"a1": _song_answer("A/a.mp3")})
    with pytest.raises(NavidromeError):
        _native_library(native).song_file("a1")
    assert native.signed_in == 0 and native.asked == []


class FakeStream:
    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def get(self, url, params=None, timeout=None):
        self.asked.append((url, dict(params), timeout))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_a_songs_sound_is_its_file_as_stored(make_library):
    library = make_library()
    library._session = FakeStream(FakeHttp(content=b"fLaC...", kind="audio/flac"))
    assert library.audio("a1") == b"fLaC..."
    url, params, timeout = library._session.asked[0]
    assert url == "http://navidrome.local:4533/rest/stream" and timeout == 120
    assert params["id"] == "a1" and params["format"] == "raw" and "maxBitRate" not in params
    # Signed as every other request: a salted hash, never the password.
    assert params["u"] == "nova" and params["t"] == hashlib.md5(("secret" + params["s"]).encode()).hexdigest()
    assert "p" not in params and "secret" not in str(params)


@pytest.mark.parametrize("answer, says", [
    (FakeHttp(200, {"subsonic-response": {"status": "failed"}}), "audio"),
    (FakeHttp(404, content=b""), "404"),
    (requests.Timeout("Read timed out: http://navidrome.local:4533/rest/stream?t=abc123token"), "Timeout"),
])
def test_a_songs_sound_which_cant_be_had_is_an_error_naming_no_address_or_token(make_library, answer, says):
    library = make_library()
    library._session = FakeStream(answer)
    with pytest.raises(NavidromeError) as raised:
        library.audio("a1")
    assert says in str(raised.value)
    assert "abc123token" not in str(raised.value) and "navidrome.local" not in str(raised.value)
    assert raised.value.__cause__ is None and (raised.value.__context__ is None or raised.value.__suppress_context__)


def _lyrics_answer(*entries):
    return {"lyricsList": {"structuredLyrics": [
        {"displayArtist": "Gunna", "displayTitle": "Endless", "lang": "eng", "synced": False,
         "line": [{"value": value} for value in lines]} for lines in entries]}}


def test_a_songs_lyrics_are_its_lines(make_library):
    library = make_library({"getLyricsBySongId": _lyrics_answer(["Oh my God", "", "what the fuck"])})
    assert library.lyrics("a1") == "Oh my God\n\nwhat the fuck"
    assert library._session.requests[0][0] == "getLyricsBySongId"
    assert library._session.requests[0][1]["id"] == "a1"


def test_only_one_copy_of_a_songs_lyrics_is_read(make_library):
    # A .lrc and a .txt both beside a song would count every swear twice.
    library = make_library({"getLyricsBySongId": _lyrics_answer([], ["first copy"], ["second copy"])})
    assert library.lyrics("a1") == "first copy"


@pytest.mark.parametrize("answer", [
    {"lyricsList": {"structuredLyrics": []}},
    {"lyricsList": {}},
    _lyrics_answer(["", "  "]),
    {"status": "failed", "error": {"code": 70, "message": "Song not found"}},
])
def test_a_song_without_lyrics_has_none(make_library, answer):
    assert make_library({"getLyricsBySongId": answer}).lyrics("a1") is None


@pytest.mark.parametrize("answer", [
    {"lyricsList": ["not", "a", "dict"]},
    {"lyricsList": {"structuredLyrics": {"line": []}}},
    {"lyricsList": {"structuredLyrics": ["not a dict", {"line": "not a list"}]}},
    {"lyricsList": {"structuredLyrics": [{"line": ["not a dict", {"value": 7}]}]}},
])
def test_a_malformed_lyrics_answer_is_no_lyrics(make_library, answer):
    assert make_library({"getLyricsBySongId": answer}).lyrics("a1") is None


def test_a_malformed_lyrics_answer_keeps_the_lines_it_can_read(make_library):
    answer = {"lyricsList": {"structuredLyrics": [{"line": ["not a dict", {"value": "what the fuck"}]}]}}
    assert make_library({"getLyricsBySongId": answer}).lyrics("a1") == "what the fuck"


def test_a_wrong_password_asking_for_lyrics_is_an_error_not_no_lyrics(make_library):
    refused = {"status": "failed", "error": {"code": WRONG_LOGIN, "message": "Wrong username or password"}}
    library = make_library({"getLyricsBySongId": refused})
    with pytest.raises(NavidromeError) as raised:
        library.lyrics("a1")
    assert raised.value.code == WRONG_LOGIN and "secret" not in str(raised.value)


def test_lyrics_which_cant_be_asked_for_are_an_error_naming_no_token(make_library):
    library = make_library({"getLyricsBySongId": requests.ConnectionError("http://nd/rest/x?t=abc123token")})
    with pytest.raises(NavidromeError) as raised:
        library.lyrics("a1")
    assert "abc123token" not in str(raised.value) and "ConnectionError" in str(raised.value)
