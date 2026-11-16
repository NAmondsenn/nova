"""
Lyrics Test
Checks where a song's lyrics are found for listening to it (decision 0019): Navidrome
first, then LRCLIB's search. Fakes stand in for both, so nothing is ever sent to either.
"""

import logging

import pytest
import requests

from voice_assistant import network
from voice_assistant.lyrics import LRCLIB_SEARCH, LRCLIB_TIMEOUT, USER_AGENT, Lyrics
from voice_assistant.navidrome import NavidromeError

ENDLESS = {"id": "a1", "title": "Endless", "artist": "Gunna", "duration": 150,
           "artists": [{"id": "r1", "name": "Gunna"}]}


class FakeLibrary:
    """Stands in for NavidromeLibrary's lyrics: an answer for every song, or an error."""

    def __init__(self, answer=None):
        self.answer = answer
        self.asked = []

    def lyrics(self, song_id):
        self.asked.append(song_id)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


class FakeLrclib:
    """Stands in for LRCLIB's search: one answer, or an error, for anything asked."""

    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.asked.append((url, params, headers, timeout))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _found(*results):
    """LRCLIB's answer: a list of its records, each with a length in seconds."""
    return FakeResponse([{"id": n, "trackName": "Endless", "artistName": "Gunna", "instrumental": False,
                          "syncedLyrics": None, **result} for n, result in enumerate(results)])


@pytest.fixture(autouse=True)
def _online(monkeypatch):
    monkeypatch.setattr(network, "online", lambda: True)


def test_navidromes_lyrics_come_first():
    lrclib = FakeLrclib(_found({"duration": 150, "plainLyrics": "other words"}))
    lyrics = Lyrics(FakeLibrary("Oh my God\nwhat the fuck"), session=lrclib)
    assert lyrics.for_song(ENDLESS) == ("Oh my God\nwhat the fuck", "navidrome")
    assert lrclib.asked == []


def test_lrclib_is_searched_by_title_and_first_artist_when_navidrome_has_none():
    lrclib = FakeLrclib(_found({"duration": 150.2, "plainLyrics": "shit"}))
    library = FakeLibrary(None)
    song = {**ENDLESS, "title": "SICKO MODE", "artist": "Travis Scott • Drake",
            "artists": [{"id": "r2", "name": "Travis Scott"}, {"id": "r3", "name": "Drake"}]}
    assert Lyrics(library, session=lrclib).for_song(song) == ("shit", "lrclib")
    assert library.asked == ["a1"]
    url, params, headers, timeout = lrclib.asked[0]
    assert url == LRCLIB_SEARCH == "https://lrclib.net/api/search"
    assert params == {"track_name": "SICKO MODE", "artist_name": "Travis Scott"}
    assert headers == {"User-Agent": USER_AGENT} and USER_AGENT == "Nova (https://github.com/NAmondsenn/nova)"
    assert timeout == LRCLIB_TIMEOUT == 15


def test_a_song_without_its_artists_listed_is_searched_by_its_artist():
    lrclib = FakeLrclib(_found({"duration": 150, "plainLyrics": "words"}))
    song = {key: value for key, value in ENDLESS.items() if key != "artists"}
    Lyrics(FakeLibrary(None), session=lrclib).for_song(song)
    assert lrclib.asked[0][1]["artist_name"] == "Gunna"


def test_the_lrclib_result_nearest_the_songs_length_is_taken():
    # A live take and a sped-up edit beside the album version, as LRCLIB lists them.
    lrclib = FakeLrclib(_found({"duration": 158, "plainLyrics": "live"}, {"duration": 147, "plainLyrics": "album"},
                               {"duration": 120, "plainLyrics": "sped up"}))
    assert Lyrics(FakeLibrary(None), session=lrclib).for_song(ENDLESS) == ("album", "lrclib")


def test_an_lrclib_result_without_plain_lyrics_isnt_taken():
    lrclib = FakeLrclib(_found({"duration": 150, "plainLyrics": None, "instrumental": True},
                               {"duration": 150, "plainLyrics": "  "}, {"duration": 154, "plainLyrics": "words"}))
    assert Lyrics(FakeLibrary(None), session=lrclib).for_song(ENDLESS) == ("words", "lrclib")


@pytest.mark.parametrize("duration", [161, 139])
def test_an_lrclib_result_11_seconds_off_isnt_the_song(duration):
    lrclib = FakeLrclib(_found({"duration": duration, "plainLyrics": "another song's words"}))
    assert Lyrics(FakeLibrary(None), session=lrclib).for_song(ENDLESS) == (None, "none")


def test_an_lrclib_result_10_seconds_off_is_the_song():
    lrclib = FakeLrclib(_found({"duration": 160, "plainLyrics": "words"}))
    assert Lyrics(FakeLibrary(None), session=lrclib).for_song(ENDLESS) == ("words", "lrclib")


def test_a_song_of_no_known_length_takes_nothing_from_lrclib():
    lrclib = FakeLrclib(_found({"duration": 150, "plainLyrics": "words"}))
    song = {key: value for key, value in ENDLESS.items() if key != "duration"}
    assert Lyrics(FakeLibrary(None), session=lrclib).for_song(song) == (None, "none")


@pytest.mark.parametrize("answer", [
    requests.Timeout("Read timed out. (read timeout=15)"),
    requests.ConnectionError("lrclib.net down"),
    FakeResponse({"message": "Internal error"}, status=500),
    FakeResponse(ValueError("not json")),
    FakeResponse({"code": 404, "name": "TrackNotFound"}),
    FakeResponse([]),
    FakeResponse(["not a record", {"duration": "long", "plainLyrics": "words"}]),
])
def test_lrclib_failing_gives_no_lyrics_rather_than_an_error(answer, caplog):
    lrclib = FakeLrclib(answer)
    with caplog.at_level(logging.WARNING):
        assert Lyrics(FakeLibrary(None), session=lrclib).for_song(ENDLESS) == (None, "none")
    assert "lrclib.net down" not in caplog.text


def test_lrclib_isnt_asked_with_no_network(monkeypatch):
    monkeypatch.setattr(network, "online", lambda: False)
    lrclib = FakeLrclib(_found({"duration": 150, "plainLyrics": "words"}))
    assert Lyrics(FakeLibrary(None), session=lrclib).for_song(ENDLESS) == (None, "none")
    assert lrclib.asked == []


def test_navidrome_out_of_reach_is_an_error_not_an_lrclib_search():
    # The song's sound comes from Navidrome too, so it can't be listened to anyway; the
    # error says why, where LRCLIB's lyrics would only hide it.
    lrclib = FakeLrclib(_found({"duration": 150, "plainLyrics": "words"}))
    with pytest.raises(NavidromeError):
        Lyrics(FakeLibrary(NavidromeError("Couldn't ask Navidrome for the lyrics: ConnectionError")),
               session=lrclib).for_song(ENDLESS)
    assert lrclib.asked == []
