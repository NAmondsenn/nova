"""
SoulSync Test
Checks how Nova asks SoulSync for a song or an album, with a fake in place of
SoulSync's API and its MCP server - answering in the shapes the live checks of 9
and 12 November recorded, with made-up ids - so nothing is ever sent to the real one.
"""

import copy
import logging

import pytest
import requests

from voice_assistant import network
from voice_assistant.soulsync import (KEY_REFUSED, MCP_TIMEOUT, PICTURE_MOST, REQUEST_TIMEOUT, SEARCH_TIMEOUT,
                                      AlbumRequest, SongRequest, SoulSync, SoulSyncAdmin, SoulSyncError, pick_album,
                                      pick_track)

URL = "http://soulsync.local:8008"
RECORDED_TRACK_COUNT = 11

SEARCH = f"{URL}/api/v1/search/tracks"
WISHLIST = f"{URL}/api/v1/wishlist"
PROCESS = f"{URL}/api/v1/wishlist/process"


def _envelope(data, status=200):
    """An answer in SoulSync's REST envelope, as its API sends every one."""
    return status, {"success": True, "data": data, "error": None, "pagination": None}


def _refusal(status, code, message):
    return status, {"success": False, "data": None, "error": {"code": code, "message": message}, "pagination": None}


def _track(name, artist, album="Made Up Album", **more):
    """A track as POST /api/v1/search/tracks lists it from Deezer, with its album a plain string."""
    return {"id": f"dz-{name}-{artist}".lower().replace(" ", "-"), "name": name, "artists": [artist], "album": album,
            "duration_ms": 213000, "release_date": "1979-09-07", "image_url": None, "popularity": 0,
            "preview_url": None, **more}


def _found(*tracks):
    return _envelope({"source": "deezer", "tracks": list(tracks)})


GOOBA_FOUND = _track("GOOBA", "6ix9ine", "GOOBA")
SONG_FOUND = _found(GOOBA_FOUND)
WISHLIST_ADDED = _envelope({"created": True}, 201)
# What POST /api/v1/wishlist answers for a track already on it, which writes nothing.
ALREADY_WISHED_FOR = _refusal(409, "CONFLICT", "Track is already in the wishlist")
WISHLIST_PROCESSING = (200, {"message": "Wishlist processing started."})
NO_KEY = _refusal(401, "AUTH_REQUIRED", "An API key is needed")
# What POST /mcp answers while SoulSync's MCP server is switched off in its settings.
MCP_OFF = (404, {"error": "not found"})


def _candidate(title, artist, album_id=None, **more):
    """An album as find_album lists it."""
    return {"album_id": album_id or f"mb-{title}-{artist}".lower().replace(" ", "-"), "title": title,
            "artists": [artist], "release_date": "1977-02-04", "tracks": RECORDED_TRACK_COUNT, "type": "album",
            "secondary_types": [], "disambiguation": "", **more}


def _tool_result(content, text="made-up summary"):
    return {"content": [{"type": "text", "text": text}], "isError": False, "structuredContent": content}


ALBUM_FOUND = _tool_result({"albums": [_candidate("Rumours", "Fleetwood Mac", "mb-rumours"),
                                       _candidate("Rumours (Live)", "Fleetwood Mac", "mb-rumours-live")]})
ALBUM_REQUESTED = _tool_result({"album": "Rumours", "artist": "Fleetwood Mac", "tracks": RECORDED_TRACK_COUNT,
                                "queued": RECORDED_TRACK_COUNT, "note": "Queued on the wishlist"})
NO_ALBUM_FOUND = _tool_result({"albums": []})
WISHLIST_STARTED = _tool_result({"started": True})


def _tool_error(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


class FakeResponse:
    def __init__(self, status_code, body, headers=None, content=b""):
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}
        self.content = content

    def iter_content(self, chunk_size):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]

    def close(self):
        pass

    def json(self):
        if self.body is None:
            raise ValueError("not JSON")
        # A copy, as from a real server.
        return copy.deepcopy(self.body)


class FakeSoulSync:
    """
    Stands in for the requests.Session SoulSync is asked through: its REST API
    answers from the recorded shapes, by address, and its MCP server each tool from
    a dict. Its stats and pictures are read with a GET: each is answered from a dict by
    address, as a FakeResponse.
    """

    def __init__(self):
        self.headers = {}
        # Each REST POST as (address, body, headers).
        self.posted = []
        # Each JSON-RPC call posted to /mcp, and every request's timeout.
        self.mcp_calls = []
        self.timeouts = []
        self.answers = {SEARCH: SONG_FOUND, WISHLIST: WISHLIST_ADDED, PROCESS: WISHLIST_PROCESSING}
        self.tools = {"find_album": ALBUM_FOUND, "request_album": ALBUM_REQUESTED,
                      "process_wishlist": WISHLIST_STARTED}
        # An answer to every REST request, or every MCP call, instead of its own - such as NO_KEY.
        self.rest_answer = None
        self.mcp_answer = None
        # An exception every request raises instead of being answered.
        self.fail = None
        # Each GET as (address, params, headers, how it was asked), and what each address answers.
        self.fetched = []
        self.pages = {}
        # Each DELETE as (address, anything sent with it, headers), and what each address answers.
        self.deleted = []
        self.removals = {}
        # Each PATCH as (address, body, headers), and what each address answers.
        self.patched = []
        self.patches = {}

    def get(self, url, params=None, timeout=None, stream=False, allow_redirects=True):
        self.timeouts.append(timeout)
        if self.fail:
            raise self.fail
        self.fetched.append((url, params, dict(self.headers), {"stream": stream, "redirects": allow_redirects}))
        page = self.pages.get(url)
        # A page made at the time it's asked for, as SoulSync's library is after a row is removed.
        return (page() if callable(page) else page) or FakeResponse(404, {"error": "not found"})

    def delete(self, url, timeout=None, **more):
        self.timeouts.append(timeout)
        if self.fail:
            raise self.fail
        self.deleted.append((url, more, dict(self.headers)))
        return FakeResponse(*self.removals.get(url, (404, {"success": False, "error": {"code": "NOT_FOUND"}})))

    def patch(self, url, json=None, timeout=None):
        self.timeouts.append(timeout)
        if self.fail:
            raise self.fail
        self.patched.append((url, copy.deepcopy(json), dict(self.headers)))
        return FakeResponse(*self.patches.get(url, (200, {"success": True})))

    def post(self, url, json=None, timeout=None):
        self.timeouts.append(timeout)
        if self.fail:
            raise self.fail
        if url == f"{URL}/mcp":
            self.mcp_calls.append(json)
            if self.mcp_answer:
                return FakeResponse(*self.mcp_answer)
            return FakeResponse(200, {"jsonrpc": "2.0", "id": json["id"],
                                      "result": self.tools[json["params"]["name"]]})
        self.posted.append((url, copy.deepcopy(json), dict(self.headers)))
        return FakeResponse(*(self.rest_answer or self.answers[url]))


@pytest.fixture
def http(monkeypatch):
    """The fake, as the session SoulSync makes - with a network to ask it over."""
    fake = FakeSoulSync()
    monkeypatch.setattr(requests, "Session", lambda: fake)
    monkeypatch.setattr(network, "online", lambda: True)
    return fake


@pytest.fixture
def soulsync(http):
    return SoulSync(URL, "the-key")


# A song

def test_a_song_is_searched_for_as_artist_dash_title_with_the_key(soulsync, http):
    song = soulsync.find_song("Gooba", "6ix9ine")
    # Named as SoulSync has it, which is how the watch looks for it and says it.
    assert song == SongRequest("GOOBA", "6ix9ine", GOOBA_FOUND)
    assert http.posted == [(SEARCH, {"query": "6ix9ine - Gooba", "limit": 10}, {"Authorization": "Bearer the-key"})]
    # It searches Deezer, beyond the local network, so it's given longer.
    assert http.timeouts == [SEARCH_TIMEOUT]


def test_a_title_alone_is_searched_for_as_it_is(soulsync, http):
    soulsync.find_song("Gooba", None)
    assert http.posted[0][1] == {"query": "Gooba", "limit": 10}


def test_the_song_found_goes_on_the_wishlist_which_is_started(soulsync, http, caplog):
    studio = _track("M.E.", "Gary Numan", "The Pleasure Principle")
    http.answers[SEARCH] = _found(studio)

    soulsync.request_song(soulsync.find_song("M.E.", "Gary Numan"))

    # The wishlist, as SoulSync's own requests go, files what it gets into the library:
    # its /api/v1/request only downloads, live on 11 November, and nothing reached Navidrome.
    assert [url for url, _, _ in http.posted] == [SEARCH, WISHLIST, PROCESS]
    # Its album as a name: given Deezer's plain string, SoulSync tagged the album as
    # the song's own title, "M.E.", live on 12 November.
    assert http.posted[1][1] == {"track_data": {**studio, "album": {"name": "The Pleasure Principle"}},
                                 "source_type": "api", "failure_reason": "Asked for by voice through Nova"}
    assert all(headers == {"Authorization": "Bearer the-key"} for _, _, headers in http.posted)
    assert http.timeouts == [SEARCH_TIMEOUT, REQUEST_TIMEOUT, REQUEST_TIMEOUT]
    assert "couldn't be started" not in caplog.text


def test_an_album_given_as_more_than_a_name_goes_on_the_wishlist_as_it_is(soulsync, http):
    album = {"name": "GOOBA", "id": "made-up-album"}
    http.answers[SEARCH] = _found(_track("GOOBA", "6ix9ine", album))
    soulsync.request_song(soulsync.find_song("Gooba", "6ix9ine"))
    assert http.posted[1][1]["track_data"]["album"] == album


def test_a_song_already_on_the_wishlist_is_still_requested(soulsync, http, caplog):
    caplog.set_level(logging.INFO)
    http.answers[WISHLIST] = ALREADY_WISHED_FOR

    soulsync.request_song(soulsync.find_song("Gooba", "6ix9ine"))

    # Started all the same, so it comes down now rather than at the wishlist's next run.
    assert [url for url, _, _ in http.posted] == [SEARCH, WISHLIST, PROCESS]
    assert "already on SoulSync's wishlist" in caplog.text


def test_a_song_still_comes_down_when_the_wishlist_couldnt_be_started(soulsync, http, caplog):
    # It's on SoulSync's wishlist either way, so it comes down at the wishlist's next run.
    http.answers[PROCESS] = _refusal(500, "INTERNAL_ERROR", "The wishlist is already being processed")
    soulsync.request_song(soulsync.find_song("Gooba", "6ix9ine"))
    assert "SoulSync's wishlist couldn't be started, so the song waits for its next run" in caplog.text


@pytest.mark.parametrize("found", [
    _found(), _envelope({"source": "deezer"}),
    # Only a clean version, and only another artist's.
    _found(_track("Gooba (Clean)", "6ix9ine"), _track("Gooba", "Someone Else"))])
def test_no_song_found_that_will_do_says_so(soulsync, http, found):
    http.answers[SEARCH] = found
    with pytest.raises(SoulSyncError) as error:
        soulsync.find_song("Gooba", "6ix9ine")
    assert error.value.reason == "SoulSync couldn't find it."
    assert [url for url, _, _ in http.posted] == [SEARCH]


def test_a_song_on_the_album_named_is_preferred(soulsync, http):
    # Swapping a clean track of an album: the explicit one on the same album, though
    # SoulSync lists the single first.
    single, on_album = _track("SICKO MODE", "Travis Scott", "SICKO MODE"), _track("SICKO MODE", "Travis Scott",
                                                                                  "ASTROWORLD")
    http.answers[SEARCH] = _found(single, on_album)
    assert soulsync.find_song("SICKO MODE", "Travis Scott", "Astroworld").track == on_album
    # Without an album, SoulSync's order stands; the search is asked for the same way either way.
    assert soulsync.find_song("SICKO MODE", "Travis Scott").track == single
    assert http.posted[0][1] == http.posted[1][1] == {"query": "Travis Scott - SICKO MODE", "limit": 10}


def test_an_album_of_nothing_but_marks_doesnt_narrow_the_pick(soulsync, http):
    # "!!!" normalises to nothing, as does an album SoulSync lists as "???": not the same album.
    single, unnamed = _track("SICKO MODE", "Travis Scott", "SICKO MODE"), _track("SICKO MODE", "Travis Scott", "???")
    http.answers[SEARCH] = _found(single, unnamed)
    assert soulsync.find_song("SICKO MODE", "Travis Scott", "!!!").track == single


def test_a_song_on_no_album_named_is_still_found_on_another(soulsync, http):
    # Only on the single, and a clean version on the album, which is never taken.
    single = _track("SICKO MODE", "Travis Scott", "SICKO MODE")
    http.answers[SEARCH] = _found(_track("SICKO MODE (Clean)", "Travis Scott", "ASTROWORLD"), single)
    assert soulsync.find_song("SICKO MODE", "Travis Scott", "Astroworld").track == single


def test_a_misheard_artist_spelt_like_the_one_found_is_taken_for_exactly_the_title():
    # "Gunner - Endless", live on 12 November - speech to text's mishearing of Gunna -
    # found Gunna's "endless" first.
    endless = _track("endless", "Gunna")
    assert pick_track([endless], "Endless", "Gunner") is endless
    # Not another title, though, nor an artist only a little like it.
    assert pick_track([_track("Endless Love", "Gunna")], "Endless", "Gunner") is None
    assert pick_track([_track("Endless", "Guns N' Roses")], "Endless", "Gunner") is None
    assert pick_track([_track("Endless Love", "Gunna"), endless], "Endless", "Gunner") is endless


def test_the_named_artists_own_song_comes_before_a_close_spelling_and_the_exact_title_before_more_words():
    # A spelling close to the artist asked for is only a guess at a mishearing, so the
    # artist's own song is taken first, wherever SoulSync lists it.
    bac, mac = _track("Dreams", "Fleetwood Bac"), _track("Dreams", "Fleetwood Mac")
    assert pick_track([bac, mac], "Dreams", "Fleetwood Mac") is mac
    assert pick_track([bac], "Dreams", "Fleetwood Mac") is bac
    # And the title asked for exactly before one with more words to it.
    love, endless = _track("Endless Love", "Gunna"), _track("endless", "Gunna")
    assert pick_track([love, endless], "Endless", "Gunna") is endless
    assert pick_track([love], "Endless", "Gunna") is love


def test_the_studio_song_is_picked_over_live_and_demo_ones():
    # "Gary Numan - M.E.", live on 12 November, found it, then live and demo versions.
    live, demo = _track("M.E. (Live 79)", "Gary Numan"), _track("M.E. (Demo Version)", "Gary Numan")
    studio = _track("M.E.", "Gary Numan", "The Pleasure Principle")
    assert pick_track([live, demo, studio], "M.E.", "Gary Numan") is studio
    assert pick_track([live, demo], "M.E.", "Gary Numan") is None
    # Unless it's the version asked for.
    assert pick_track([demo, studio, live], "M.E. live", "Gary Numan") is live


def test_a_song_from_a_live_album_is_a_live_version():
    # Called plain "M.E." on an album whose name says it's live.
    live = _track("M.E.", "Gary Numan", "Living Ornaments '79 (Live)")
    unplugged = _track("M.E.", "Gary Numan", {"name": "M.E. - Unplugged"})
    studio = _track("M.E.", "Gary Numan", "The Pleasure Principle")
    assert pick_track([live, unplugged, studio], "M.E.", "Gary Numan") is studio
    assert pick_track([live, unplugged], "M.E.", "Gary Numan") is None


def test_a_mix_isnt_picked_unless_its_the_one_asked_for():
    extended, club = _track("Blue Monday (Extended Mix)", "New Order"), _track("Blue Monday - Club Mix", "New Order")
    single = _track("Blue Monday", "New Order")
    assert pick_track([extended, club, single], "Blue Monday", "New Order") is single
    assert pick_track([club, extended, single], "Blue Monday extended mix", "New Order") is extended
    # A remix by name is still a remix, which "remix" asks for.
    remix = _track("Gooba (Made Up Remix)", "6ix9ine")
    assert pick_track([remix], "Gooba remix", "6ix9ine") is remix


def test_a_credit_is_never_read_as_a_version():
    # Little Mix ends in "mix", and is no mix of the song.
    with_them = _track("Kiss My (Uh Oh) (with Little Mix)", "Anne-Marie")
    assert pick_track([with_them], "Kiss My (Uh Oh)", "Anne-Marie") is with_them
    for credit in ("Gooba (feat. Live Wire)", "Gooba [ft. The Remix Crew]", "Gooba - featuring Acoustic Ladyland"):
        candidate = _track(credit, "6ix9ine")
        assert pick_track([candidate], "Gooba", "6ix9ine") is candidate
    # A version dashed on after a credit is still read, each dashed part on its own.
    for name in ("Gooba - feat. Made Up - Live", "Gooba - with Made Up - Remix"):
        for artist in ("6ix9ine", None):
            assert pick_track([_track(name, "6ix9ine")], "Gooba", artist) is None


def test_a_dashed_part_is_read_without_its_own_brackets():
    # "Extended Mix (feat. X)" ends in its credit, which would hide the mix it is.
    extended = _track("Song - Extended Mix (feat. X)", "Made Up")
    assert pick_track([extended], "Song", "Made Up") is None
    assert pick_track([extended], "Song Extended Mix", "Made Up") is extended
    # The credit is still a part of its own, and a version in brackets still a version.
    assert pick_track([_track("Song - Live (feat. X)", "Made Up")], "Song", "Made Up") is None
    plain = _track("Song - feat. X (Original Mix)", "Made Up")
    assert pick_track([plain], "Song", "Made Up") is plain


def test_an_original_mix_is_the_song_itself():
    # On an electronic release, the original mix is the original.
    original = _track("Strobe (Original Mix)", "deadmau5")
    assert pick_track([_track("Strobe (Club Mix)", "deadmau5"), original], "Strobe", "deadmau5") is original
    version = _track("Strobe - Original Version", "deadmau5")
    assert pick_track([version], "Strobe", "deadmau5") is version


@pytest.mark.parametrize("name", ["Gooba (Remix)", "Gooba - Acoustic", "Gooba [Instrumental]", "Gooba (Sped Up)",
                                  "Gooba (Slowed)", "Gooba (Karaoke Version)", "Gooba - Live at Wembley",
                                  "Gooba (Extended Mix)", "Gooba - Club Mix", "Gooba (Megamix)", "Gooba (Unplugged)"])
def test_another_version_of_a_song_isnt_picked(name):
    assert pick_track([_track(name, "6ix9ine")], "Gooba", "6ix9ine") is None


@pytest.mark.parametrize("name, album", [
    ("Gooba (Clean)", "GOOBA"), ("Gooba - Radio Edit", "GOOBA"), ("Gooba [Edited]", "GOOBA"),
    ("Gooba (Censored Version)", "GOOBA"), ("Gooba", "GOOBA (Clean)"), ("Gooba", {"name": "GOOBA - Clean Version"}),
    ("Gooba (Clean Lyrics)", "GOOBA"), ("Gooba", "GOOBA (Clean Advance Copy)"),
    ("Gooba [Clean Lyrics Version]", "GOOBA"), ("Gooba", {"name": "GOOBA - Clean Advance Copy"})])
def test_a_clean_version_of_a_song_is_never_picked(name, album):
    assert pick_track([_track(name, "6ix9ine", album)], "Gooba", "6ix9ine") is None


def test_a_song_called_clean_is_its_title_not_a_version():
    # Only a bracketed or dashed part saying so makes it one, as navidrome.is_clean has it.
    clean = _track("Clean", "Taylor Swift", "1989")
    assert pick_track([clean], "Clean", "Taylor Swift") is clean
    credit = _track("Gooba (feat. Clean Bandit)", "6ix9ine")
    assert pick_track([credit], "Gooba", "6ix9ine") is credit


def test_a_song_is_by_the_artist_only_with_every_word_of_their_name():
    # Mac Miller isn't Fleetwood Mac, nor spelt enough like it to be a mishearing.
    assert pick_track([_track("Dreams", "Mac Miller")], "Dreams", "Fleetwood Mac") is None
    featuring = _track("Dreams", "Someone Else", artists=["Someone Else", "Fleetwood Mac"])
    assert pick_track([featuring], "Dreams", "Fleetwood Mac") is featuring
    together = _track("No Church in the Wild", "Jay-Z", artists=["Jay-Z", "Kanye West"])
    assert pick_track([together], "No Church in the Wild", "Jay-Z and Kanye West") is together


def test_the_first_song_which_will_do_is_picked_with_every_word_of_the_title():
    # SoulSync lists its best guesses first.
    first, second = _track("Gooba", "6ix9ine"), _track("Gooba", "Someone Else")
    assert pick_track([first, second], "Gooba", None) is first
    assert pick_track([second, first], "Gooba", None) is second
    longer = _track("Gooba Gooba", "6ix9ine")
    assert pick_track([longer], "Gooba", "6ix9ine") is longer
    # With an artist asked for, their song with exactly the title asked for comes first.
    assert pick_track([longer, first], "Gooba", "6ix9ine") is first
    # With only a title, SoulSync's order decides: an exact title further down may be
    # a cover, or another act's song.
    assert pick_track([longer, second], "Gooba", None) is longer
    assert pick_track([_track("Goo", "6ix9ine")], "Gooba", "6ix9ine") is None


# An album

def test_an_album_is_found_then_requested_through_mcp(soulsync, http):
    request = soulsync.request_album("Rumours", "Fleetwood Mac")
    # process_wishlist starts the download now, not at the wishlist's next run.
    assert [call["params"]["name"] for call in http.mcp_calls] == ["find_album", "request_album",
                                                                    "process_wishlist"]
    assert [call["method"] for call in http.mcp_calls] == ["tools/call"] * 3
    # As many editions as SoulSync gives, for pick_album to choose past the clean and deluxe ones.
    assert http.mcp_calls[0]["params"]["arguments"] == {"query": "Fleetwood Mac - Rumours", "limit": 25}
    assert http.mcp_calls[1]["params"]["arguments"] == {"album_id": "mb-rumours"}
    assert http.headers["Authorization"] == "Bearer the-key"
    # find_album searches beyond the local network, so it's given longer.
    assert http.timeouts == [MCP_TIMEOUT] * 3
    # SoulSync gives an album's track count but not its titles, and no id to follow it by.
    assert request == AlbumRequest(None, [], RECORDED_TRACK_COUNT, RECORDED_TRACK_COUNT)


def test_an_album_partly_had_is_followed_by_the_tracks_soulsync_queued(soulsync, http):
    # Two of its eleven tracks already in the library, so SoulSync queues the other nine.
    http.tools["request_album"] = _tool_result({"album": "Rumours", "artist": "Fleetwood Mac",
                                                "tracks": RECORDED_TRACK_COUNT, "queued": 9,
                                                "note": "Queued on the wishlist"})
    request = soulsync.request_album("Rumours", "Fleetwood Mac")
    assert (request.track_count, request.queued) == (RECORDED_TRACK_COUNT, 9)

    # None queued is none, and an answer which doesn't say is taken as the whole album.
    http.tools["request_album"] = _tool_result({"album": "Rumours", "tracks": RECORDED_TRACK_COUNT, "queued": 0})
    assert soulsync.request_album("Rumours", "Fleetwood Mac").queued == 0
    http.tools["request_album"] = _tool_result({"album": "Rumours", "tracks": RECORDED_TRACK_COUNT})
    assert soulsync.request_album("Rumours", "Fleetwood Mac").queued == RECORDED_TRACK_COUNT


def test_the_wishlist_isnt_started_when_nothing_was_queued(soulsync, http):
    # Every track already had or on its way: nothing for process_wishlist to start.
    http.tools["request_album"] = _tool_result({"album": "Rumours", "tracks": RECORDED_TRACK_COUNT, "queued": 0})
    assert soulsync.request_album("Rumours", "Fleetwood Mac").queued == 0
    assert [call["params"]["name"] for call in http.mcp_calls] == ["find_album", "request_album"]

    # Some queued, or an answer which doesn't say, still starts it.
    http.mcp_calls.clear()
    http.tools["request_album"] = _tool_result({"album": "Rumours", "tracks": RECORDED_TRACK_COUNT, "queued": 9})
    soulsync.request_album("Rumours", "Fleetwood Mac")
    assert [call["params"]["name"] for call in http.mcp_calls][-1] == "process_wishlist"
    http.mcp_calls.clear()
    http.tools["request_album"] = _tool_result({"album": "Rumours", "tracks": RECORDED_TRACK_COUNT})
    soulsync.request_album("Rumours", "Fleetwood Mac")
    assert [call["params"]["name"] for call in http.mcp_calls][-1] == "process_wishlist"


def test_an_album_title_alone_is_searched_for_as_it_is(soulsync, http):
    soulsync.request_album("Rumours", None)
    assert http.mcp_calls[0]["params"]["arguments"] == {"query": "Rumours", "limit": 25}


def test_an_album_still_comes_down_when_the_wishlist_couldnt_be_started(soulsync, http, caplog):
    # It's on SoulSync's wishlist either way, so it comes down at the wishlist's next run.
    http.tools["process_wishlist"] = _tool_error("The wishlist is already being processed")
    assert soulsync.request_album("Rumours", "Fleetwood Mac").track_count == RECORDED_TRACK_COUNT
    assert "wishlist" in caplog.text


def test_the_album_picked_is_the_closest_and_never_a_clean_edition():
    candidates = [_candidate("Rumours (Clean)", "Fleetwood Mac"), _candidate("Rumours (Live)", "Fleetwood Mac"),
                  _candidate("Rumours", "Someone Else"), _candidate("Rumours", "Fleetwood Mac")]
    assert pick_album(candidates, "Rumours", "Fleetwood Mac") is candidates[3]
    assert pick_album(candidates, "Rumours Live", "Fleetwood Mac") is candidates[1]
    assert pick_album(candidates[:1], "Rumours", "Fleetwood Mac") is None


def test_an_edition_musicbrainz_calls_clean_or_live_isnt_the_album_asked_for():
    # MusicBrainz says which edition a release is in its disambiguation, and a live
    # album in its secondary types, rather than in the title.
    clean = _candidate("good kid, m.A.A.d city", "Kendrick Lamar", disambiguation="clean")
    live = _candidate("good kid, m.A.A.d city", "Kendrick Lamar", secondary_types=["Live"])
    deluxe = _candidate("good kid, m.A.A.d city", "Kendrick Lamar", disambiguation="deluxe edition")
    assert pick_album([clean, live, deluxe], "good kid maad city", "Kendrick Lamar") is deluxe
    assert pick_album([clean, live], "good kid maad city", "Kendrick Lamar") is None


@pytest.mark.parametrize("title, disambiguation", [
    ("Rumours", "Clean Edition"), ("Rumours", "clean, deluxe edition"), ("Rumours", "censored"),
    ("Rumours", "clean / Target exclusive"), ("Rumours", "clean (US)"), ("Rumours", "radio edit"),
    ("Rumours (Clean Edition)", ""), ("Rumours (Censored)", ""), ("Rumours [Edited]", ""),
    ("Rumours - Clean Version", ""), ("Rumours \u2013 Clean Edition", ""), ("Rumours \u2014 Censored", "")])
def test_a_clean_edition_however_its_noted_is_never_picked(title, disambiguation):
    # A disambiguation is a free note on the edition, so a clean word anywhere in it
    # counts - unlike in a track's title, where "(feat. Clean Bandit)" is a credit.
    candidate = _candidate(title, "Fleetwood Mac", disambiguation=disambiguation)
    assert pick_album([candidate], "Rumours", "Fleetwood Mac") is None


def test_an_album_called_clean_is_its_title_not_an_edition():
    # Only a note on the edition counts, as a track's title does in navidrome.version_of.
    album = _candidate("Clean", "Soccer Mommy")
    assert pick_album([album], "Clean", "Soccer Mommy") is album


def test_a_demo_edition_isnt_the_album_asked_for_unless_its_named():
    # A demo is another version, in the title, a note on the edition or MusicBrainz's secondary types.
    studio = _candidate("Rumours", "Fleetwood Mac")
    for demo in (_candidate("Rumours (Demo)", "Fleetwood Mac"), _candidate("Rumours - Demo", "Fleetwood Mac"),
                 _candidate("Rumours", "Fleetwood Mac", disambiguation="demo"),
                 _candidate("Rumours", "Fleetwood Mac", secondary_types=["Demo"])):
        assert pick_album([demo], "Rumours", "Fleetwood Mac") is None
        assert pick_album([demo, studio], "Rumours", "Fleetwood Mac") is studio
        assert pick_album([demo], "Rumours Demo", "Fleetwood Mac") is demo


def test_the_edition_with_the_swearing_left_in_is_picked_over_one_without():
    clean_deluxe = _candidate("Rumours", "Fleetwood Mac", disambiguation="clean, deluxe edition")
    deluxe = _candidate("Rumours", "Fleetwood Mac", disambiguation="deluxe edition")
    censored = _candidate("Rumours", "Fleetwood Mac", disambiguation="censored")
    plain = _candidate("Rumours", "Fleetwood Mac")
    assert pick_album([clean_deluxe, deluxe], "Rumours", "Fleetwood Mac") is deluxe
    assert pick_album([censored, plain], "Rumours", "Fleetwood Mac") is plain


def test_an_edition_musicbrainz_calls_explicit_wins_among_equals():
    plain = _candidate("Rumours", "Fleetwood Mac")
    explicit = _candidate("Rumours", "Fleetwood Mac", disambiguation="explicit")
    assert pick_album([plain, explicit], "Rumours", "Fleetwood Mac") is explicit
    # Only as a whole word, and never over a closer match.
    inexplicit = _candidate("Rumours", "Fleetwood Mac", disambiguation="inexplicit")
    assert pick_album([plain, inexplicit], "Rumours", "Fleetwood Mac") is plain
    closer = _candidate("Rumours", "Fleetwood Mac")
    further = _candidate("Rumours Deluxe", "Fleetwood Mac", disambiguation="explicit")
    assert pick_album([further, closer], "Rumours", "Fleetwood Mac") is closer


def test_an_album_by_someone_else_entirely_isnt_picked():
    # Downloading it would put an album nobody asked for into the library for good.
    assert pick_album([_candidate("Rumours", "Someone Else")], "Rumours", "Fleetwood Mac") is None
    assert pick_album([_candidate("Rumours", "Someone Else")], "Rumours", None) is not None


def test_an_album_is_by_the_artist_only_with_every_word_of_their_name():
    # Sharing a word isn't enough: Mac Miller isn't Fleetwood Mac.
    assert pick_album([_candidate("Rumours", "Mac Miller")], "Rumours", "Fleetwood Mac") is None
    assert pick_album([_candidate("Tha Carter", "Lil Uzi Vert")], "Tha Carter", "Lil Wayne") is None
    featuring = _candidate("Rumours", "Fleetwood Mac", artists=["Someone Else", "Fleetwood Mac"])
    assert pick_album([featuring], "Rumours", "Fleetwood Mac") is featuring


def test_an_artist_named_with_and_is_the_same_as_with_an_ampersand_or_as_two():
    # Claude writes "and" where the release has "&" or "+", or lists each artist on their own.
    together = _candidate("Watch the Throne", "Jay-Z", artists=["Jay-Z", "Kanye West"])
    assert pick_album([together], "Watch the Throne", "Jay-Z and Kanye West") is together
    assert pick_album([together], "Watch the Throne", "Kanye West and Jay-Z") is together
    ampersand = _candidate("Bookends", "Simon & Garfunkel")
    assert pick_album([ampersand], "Bookends", "Simon and Garfunkel") is ampersand
    plus = _candidate("Lungs", "Florence + the Machine")
    assert pick_album([plus], "Lungs", "Florence and the Machine") is plus
    # Still every word: Mac Miller isn't Fleetwood Mac, nor Jay-Z alone Jay-Z and Kanye West.
    assert pick_album([_candidate("Rumours", "Mac Miller")], "Rumours", "Fleetwood Mac") is None
    assert pick_album([_candidate("Watch the Throne", "Jay-Z")], "Watch the Throne", "Jay-Z and Kanye West") is None


def test_the_album_is_picked_over_a_single_of_the_same_name():
    # find_album "Michael Jackson - Thriller", live on 9 November: eight releases of
    # the album and two of the single, all called Thriller.
    single = _candidate("Thriller", "Michael Jackson", album_id="mb-single", type="single", tracks=2)
    album = _candidate("Thriller", "Michael Jackson", album_id="mb-album", type="album", tracks=9)
    candidates = [single] + [dict(album, album_id=f"mb-album-{n}") for n in range(8)] + [dict(single)]
    assert pick_album(candidates, "Thriller", "Michael Jackson") is candidates[1]
    # Anything else is still picked when it's all there is - an EP asked for by name.
    ep = _candidate("Paper Walls", "Made Up Band", type="ep", tracks=4)
    assert pick_album([ep], "Paper Walls", "Made Up Band") is ep


def test_no_album_found_says_so(soulsync, http):
    http.tools["find_album"] = NO_ALBUM_FOUND
    with pytest.raises(SoulSyncError) as error:
        soulsync.request_album("Nothing like it", None)
    assert error.value.reason == "SoulSync couldn't find it."
    assert [call["params"]["name"] for call in http.mcp_calls] == ["find_album"]


def test_only_clean_editions_found_is_nothing_found(soulsync, http):
    http.tools["find_album"] = _tool_result({"albums": [_candidate("Rumours (Clean)", "Fleetwood Mac")]})
    with pytest.raises(SoulSyncError) as error:
        soulsync.request_album("Rumours", "Fleetwood Mac")
    assert error.value.reason == "SoulSync couldn't find it."


def test_mcp_switched_off_says_where_to_turn_it_on(soulsync, http):
    http.mcp_answer = MCP_OFF
    with pytest.raises(SoulSyncError) as error:
        soulsync.request_album("Rumours", "Fleetwood Mac")
    assert error.value.reason == ("SoulSync's MCP is off, so it can't download albums. "
                                  "Turn it on in SoulSync's settings.")


def test_a_tool_soulsync_says_failed_gives_its_own_words(soulsync, http):
    http.tools["request_album"] = _tool_error("This needs an admin key")
    with pytest.raises(SoulSyncError) as error:
        soulsync.request_album("Rumours", "Fleetwood Mac")
    assert error.value.reason == "SoulSync said: This needs an admin key."


# When it goes wrong

def test_an_error_soulsync_answers_with_gives_its_own_words(soulsync, http):
    song = soulsync.find_song("Gooba", "6ix9ine")
    http.answers[SEARCH] = _refusal(400, "BAD_REQUEST", "query is required")
    with pytest.raises(SoulSyncError) as error:
        soulsync.find_song("Gooba", None)
    assert error.value.reason == "SoulSync said: query is required."

    http.answers[WISHLIST] = _refusal(400, "BAD_REQUEST", "track_data is required")
    with pytest.raises(SoulSyncError) as error:
        soulsync.request_song(song)
    assert error.value.reason == "SoulSync said: track_data is required."


@pytest.mark.parametrize("refusal", [
    NO_KEY,
    # A key it knows but won't let request anything - one which can only read - is the same key to check.
    _refusal(403, "FORBIDDEN", "This key can't do that")])
def test_a_refused_key_says_which_key_to_check(soulsync, http, refusal):
    song = soulsync.find_song("Gooba", "6ix9ine")
    http.rest_answer = http.mcp_answer = refusal
    for ask in (lambda: soulsync.find_song("Gooba", "6ix9ine"), lambda: soulsync.request_song(song),
                lambda: soulsync.request_album("Rumours", "Fleetwood Mac")):
        with pytest.raises(SoulSyncError) as error:
            ask()
        assert error.value.reason == "SoulSync didn't accept its key, so SOULSYNC_API_KEY under Keys needs checking."


def test_soulsync_out_of_reach_is_said_plainly_and_the_key_never_logged(soulsync, http, caplog):
    # The fake raises requests.ConnectionError - with the key in it, to show its words aren't logged.
    http.fail = requests.ConnectionError("Authorization: Bearer the-key")
    song = SongRequest("GOOBA", "6ix9ine", GOOBA_FOUND)
    for ask in (lambda: soulsync.find_song("Gooba", "6ix9ine"), lambda: soulsync.request_song(song),
                lambda: soulsync.request_album("Rumours", "Fleetwood Mac")):
        with pytest.raises(SoulSyncError) as error:
            ask()
        assert error.value.reason == "SoulSync isn't answering." and "the-key" not in caplog.text
    assert "ConnectionError" in caplog.text


def test_soulsync_too_slow_to_answer_isnt_answering(soulsync, http):
    http.fail = requests.Timeout("Read timed out")
    with pytest.raises(SoulSyncError) as error:
        soulsync.find_song("Gooba", "6ix9ine")
    assert error.value.reason == "SoulSync isn't answering."


def test_an_answer_which_isnt_json_is_an_error_with_no_reason(soulsync, http, caplog):
    http.answers[SEARCH] = (502, None)
    with pytest.raises(SoulSyncError) as error:
        soulsync.find_song("Gooba", "6ix9ine")
    assert error.value.reason is None and "502" in caplog.text


def test_with_no_network_soulsync_isnt_asked_at_all(soulsync, http, monkeypatch):
    monkeypatch.setattr(network, "online", lambda: False)
    with pytest.raises(SoulSyncError) as error:
        soulsync.find_song("Gooba", "6ix9ine")
    assert error.value.reason == "SoulSync isn't answering." and http.timeouts == []


# Set up or not

def test_it_is_made_only_with_an_address_and_a_key(make_config):
    assert SoulSync.from_config(make_config(soulsync={"url": ""}), "the-key") is None
    assert SoulSync.from_config(make_config(soulsync={"url": "http://soulsync.local:8008"}), None) is None
    assert SoulSync.from_config(make_config(soulsync={"url": "http://soulsync.local:8008/"}), "the-key").url \
        == "http://soulsync.local:8008"


def test_half_set_up_is_logged_without_the_key_and_not_set_up_at_all_isnt(make_config, caplog):
    with caplog.at_level(logging.WARNING):
        SoulSync.from_config(make_config(soulsync={"url": ""}), "the-key")
        SoulSync.from_config(make_config(soulsync={"url": "http://soulsync.local:8008"}), "")
        SoulSync.from_config(make_config(), None)
    assert len(caplog.records) == 2 and "the-key" not in caplog.text


# Its stats and pictures

COVER = b"\xff\xd8made-up-jpeg"


def _picture(content=COVER, kind="image/jpeg", status=200):
    return FakeResponse(status, None, {"Content-Type": kind}, content)


def test_stats_get_sends_key_and_params(soulsync, http):
    http.pages[f"{URL}/api/stats/cached"] = FakeResponse(200, {"success": True, "overview": {}})
    assert soulsync.stats("/api/stats/cached", range="7d") == {"success": True, "overview": {}}
    # A GET with the query as params, which requests puts after the "?", with the key as for a POST.
    assert http.fetched == [(f"{URL}/api/stats/cached", {"range": "7d"}, {"Authorization": "Bearer the-key"},
                             {"stream": False, "redirects": True})]
    assert http.timeouts == [REQUEST_TIMEOUT]


def test_stats_errors_are_read_as_every_other_answer_is(soulsync, http, caplog):
    http.pages[f"{URL}/api/stats/year"] = FakeResponse(403, {"error": "forbidden"})
    with pytest.raises(SoulSyncError) as refused:
        soulsync.stats("/api/stats/year")
    assert "key" in refused.value.reason
    http.fail = requests.ConnectionError("http://soulsync.local:8008/?secret=the-key")
    with pytest.raises(SoulSyncError) as down:
        soulsync.stats("/api/stats/year")
    assert down.value.reason == "SoulSync isn't answering."
    # The kind of error only, which could hold the request and with it the key.
    assert "ConnectionError" in caplog.text and "the-key" not in caplog.text


def test_stats_without_a_network_is_not_answering(soulsync, http, monkeypatch):
    monkeypatch.setattr(network, "online", lambda: False)
    with pytest.raises(SoulSyncError) as down:
        soulsync.stats("/api/stats/year")
    assert down.value.reason == "SoulSync isn't answering."
    assert http.fetched == []


def test_picture_from_soulsync_path(soulsync, http):
    http.pages[f"{URL}/api/navidrome/cover/abc"] = _picture()
    assert soulsync.picture("/api/navidrome/cover/abc") == (COVER, "image/jpeg")
    # With the key, which a cover needs; never following a redirect, which could take the key elsewhere.
    assert http.fetched == [(f"{URL}/api/navidrome/cover/abc", None, {"Authorization": "Bearer the-key"},
                             {"stream": True, "redirects": False})]


def test_picture_from_soulsync_absolute_address(soulsync, http):
    http.pages[f"{URL}/api/navidrome/cover/abc"] = _picture(kind="image/png; charset=binary")
    assert soulsync.picture(f"{URL}/api/navidrome/cover/abc") == (COVER, "image/png")


@pytest.mark.parametrize("url", [
    "http://other.example/api/navidrome/cover/abc",
    # The same host on another port, or another scheme, isn't SoulSync.
    "http://soulsync.local:9999/api/navidrome/cover/abc",
    "https://soulsync.local:8008/api/navidrome/cover/abc",
    "//other.example/cover", "cover/abc", "", None])
def test_picture_refused_for_another_address(soulsync, http, url):
    http.pages[url] = _picture()
    assert soulsync.picture(url) is None
    assert http.fetched == []


@pytest.mark.parametrize("answer", [
    FakeResponse(404, None, {"Content-Type": "image/jpeg"}, COVER),
    FakeResponse(302, None, {"Content-Type": "image/jpeg"}, COVER),
    _picture(kind="text/html"),
    _picture(kind="image/svg+xml"),
    FakeResponse(200, None, {}, COVER),
    _picture(content=b"x" * (PICTURE_MOST + 1)),
], ids=["404", "redirect", "html", "svg", "no type", "600 KB"])
def test_picture_refused(soulsync, http, answer):
    http.pages[f"{URL}/api/navidrome/cover/abc"] = answer
    assert soulsync.picture("/api/navidrome/cover/abc") is None


def test_picture_at_the_most_is_taken(soulsync, http):
    http.pages[f"{URL}/api/navidrome/cover/abc"] = _picture(content=b"x" * PICTURE_MOST, kind="image/webp")
    assert soulsync.picture("/api/navidrome/cover/abc") == (b"x" * PICTURE_MOST, "image/webp")


def test_picture_never_raises(soulsync, http, monkeypatch):
    http.fail = requests.Timeout("slow")
    assert soulsync.picture("/api/navidrome/cover/abc") is None
    http.fail = None
    monkeypatch.setattr(network, "online", lambda: False)
    assert soulsync.picture("/api/navidrome/cover/abc") is None


# Taking a wish back

def test_a_wish_is_taken_back_by_the_id_it_was_made_with(soulsync, http):
    http.removals[f"{URL}/api/v1/wishlist/3135556"] = _envelope({"removed": True})
    assert soulsync.unwish("3135556") is True
    assert http.deleted == [(f"{URL}/api/v1/wishlist/3135556", {}, {"Authorization": "Bearer the-key"})]
    assert http.timeouts == [REQUEST_TIMEOUT]


def test_a_wish_that_isnt_there_or_cant_be_taken_back_is_false_and_never_raises(soulsync, http, caplog):
    # Nothing at that id: SoulSync answers 404.
    assert soulsync.unwish("3135556") is False
    http.fail = requests.ConnectionError("made-up")
    assert soulsync.unwish("3135556") is False
    assert soulsync.unwish("") is False
    assert caplog.text.count("couldn't be taken off SoulSync's wishlist") == 3
    assert len(http.deleted) == 1


def test_an_id_is_sent_as_one_part_of_the_address(soulsync, http):
    soulsync.unwish("../library/track/7")
    assert http.deleted[0][0] == f"{URL}/api/v1/wishlist/..%2Flibrary%2Ftrack%2F7"


# Forgetting a song set aside

LIBRARY = f"{URL}/api/v1/library/tracks"
LOGIN = f"{URL}/api/auth/login"
PASSWORD = "made-up-password"
REL = "Travis Scott/ASTROWORLD/03 SICKO MODE.flac"


def _row(row_id, rel):
    """A row as GET /api/v1/library/tracks lists it: its file under SoulSync's /app/Transfer."""
    return {"id": row_id, "title": "SICKO MODE", "artist_name": "Travis Scott", "album_title": "ASTROWORLD",
            "file_path": f"/app/Transfer/{rel}"}


class FakeAdmin:
    """
    Stands in for the admin's requests.Session: its sign-in sets the "soulsync_session"
    cookie, and a DELETE with it takes the row from the library the key's GET lists.
    """

    def __init__(self, rows):
        self.rows = rows
        self.cookies = {}
        self.logins = []
        self.deleted = []
        # What sign-ins answer, in turn, and what deletes answer before their own, in turn.
        self.login_answers = []
        self.delete_answers = []

    def post(self, url, json=None, timeout=None):
        self.logins.append((url, copy.deepcopy(json)))
        status, body = self.login_answers.pop(0) if self.login_answers else (200, {"success": True, "profile": {}})
        if status == 200:
            self.cookies["soulsync_session"] = f"made-up-session-{len(self.logins)}"
        return FakeResponse(status, body)

    def delete(self, url, timeout=None, **more):
        self.deleted.append((url, more, dict(self.cookies)))
        if self.delete_answers:
            return FakeResponse(*self.delete_answers.pop(0))
        if not self.cookies:
            return FakeResponse(401, {"success": False, "error": "Not signed in"})
        row_id = url.rsplit("/", 1)[1]
        self.rows[:] = [row for row in self.rows if str(row["id"]) != row_id]
        return FakeResponse(200, {"success": True})


@pytest.fixture
def library(http):
    """SoulSync's own rows, listed by the key's GET as they are when it's asked."""
    rows = [_row(41, REL), _row(42, "Travis Scott/SICKO MODE/01 SICKO MODE.flac")]
    http.pages[LIBRARY] = lambda: FakeResponse(*_envelope({"tracks": copy.deepcopy(rows)}))
    return rows


@pytest.fixture
def admin_http(library):
    return FakeAdmin(library)


@pytest.fixture
def admin(admin_http):
    return SoulSyncAdmin(URL + "/", "nova", PASSWORD, session=admin_http)


def test_forgetting_signs_in_once_and_removes_only_the_row_for_that_file(soulsync, http, admin, admin_http,
                                                                          library):
    assert admin.forget(soulsync, "SICKO MODE", "Travis Scott", REL) is True

    # Found with the key, by title and artist, and looked at again once it's gone.
    assert [(url, params, headers) for url, params, headers, _ in http.fetched] == \
        [(LIBRARY, {"title": "SICKO MODE", "artist": "Travis Scott"}, {"Authorization": "Bearer the-key"})] * 2
    assert admin_http.logins == [(LOGIN, {"username": "nova", "password": PASSWORD})]
    # Only the row: nothing sent with the DELETE, so never delete_file or blacklist,
    # and it went with the session's cookie.
    assert admin_http.deleted == [(f"{URL}/api/library/track/41", {}, {"soulsync_session": "made-up-session-1"})]
    assert "delete_file" not in str(admin_http.deleted) and "blacklist" not in str(admin_http.deleted)
    assert [row["id"] for row in library] == [42]

    # Signed in already, so the next song needs no sign-in.
    library.append(_row(43, "Gunna/one of wun/endless.flac"))
    assert admin.forget(soulsync, "endless", "Gunna", "Gunna/one of wun/endless.flac") is True
    assert len(admin_http.logins) == 1 and len(admin_http.deleted) == 2


def test_a_row_already_gone_is_forgotten_without_signing_in(soulsync, http, admin, admin_http, library):
    library.pop(0)
    assert admin.forget(soulsync, "SICKO MODE", "Travis Scott", REL) is True
    assert admin_http.logins == [] and admin_http.deleted == []


@pytest.mark.parametrize("rel, matches", [
    # Matched however the path is written, once normalised.
    ("Travis Scott/./ASTROWORLD//03 SICKO MODE.flac", True),
    # A file whose name only ends the same isn't that file.
    ("3 SICKO MODE.flac", False),
    ("SICKO MODE.flac", False)])
def test_a_row_is_matched_by_its_whole_path_from_a_folder(soulsync, admin, admin_http, rel, matches):
    assert admin.forget(soulsync, "SICKO MODE", "Travis Scott", rel) is True
    assert len(admin_http.deleted) == (1 if matches else 0)


@pytest.mark.parametrize("rel", ["", "/app/Transfer/" + REL, "../Transfer/" + REL, "."])
def test_a_path_from_the_root_or_out_of_the_music_folder_is_refused_before_anything_is_asked(soulsync, http, admin,
                                                                                         admin_http, rel):
    with pytest.raises(SoulSyncError) as error:
        admin.forget(soulsync, "SICKO MODE", "Travis Scott", rel)
    assert error.value.reason
    assert http.fetched == [] and admin_http.logins == [] and admin_http.deleted == []


def test_a_refused_sign_in_says_which_key_to_check_and_never_shows_the_password(soulsync, admin, admin_http,
                                                                                caplog, library):
    admin_http.login_answers = [(401, {"success": False, "error": "Invalid username or password"})]
    with pytest.raises(SoulSyncError) as error:
        admin.forget(soulsync, "SICKO MODE", "Travis Scott", REL)
    assert error.value.reason == ("SoulSync didn't accept Nova's admin profile, so SOULSYNC_ADMIN_PASSWORD under "
                                  "Keys needs checking.")
    assert admin_http.deleted == [] and len(library) == 2
    for text in (str(error.value), repr(error.value), repr(admin), str(admin), caplog.text):
        assert PASSWORD not in text


def test_a_refused_delete_signs_in_again_once(soulsync, admin, admin_http, library):
    # Its session ran out between songs.
    admin_http.delete_answers = [(403, {"success": False, "error": "Not allowed"})]
    assert admin.forget(soulsync, "SICKO MODE", "Travis Scott", REL) is True
    assert len(admin_http.logins) == 2 and len(admin_http.deleted) == 2
    assert [row["id"] for row in library] == [42]


def test_a_delete_refused_after_signing_in_again_raises(soulsync, admin, admin_http, library):
    admin_http.delete_answers = [(403, {"success": False, "error": "Admin only"})] * 2
    with pytest.raises(SoulSyncError) as error:
        admin.forget(soulsync, "SICKO MODE", "Travis Scott", REL)
    assert error.value.reason
    assert len(admin_http.logins) == 2 and len(admin_http.deleted) == 2 and len(library) == 2


@pytest.mark.parametrize("answer", [(500, {"success": False, "error": "Database is locked"}), (500, None)])
def test_a_failed_delete_raises(soulsync, admin, admin_http, answer, library):
    admin_http.delete_answers = [answer]
    with pytest.raises(SoulSyncError):
        admin.forget(soulsync, "SICKO MODE", "Travis Scott", REL)
    assert len(admin_http.logins) == 1 and len(library) == 2


def test_a_row_still_listed_after_its_delete_is_false(soulsync, admin, admin_http, library):
    # SoulSync said it was removed, but still lists it.
    admin_http.delete_answers = [(200, {"success": True})]
    assert admin.forget(soulsync, "SICKO MODE", "Travis Scott", REL) is False


def test_soulsync_out_of_reach_for_the_admin_isnt_answering(soulsync, admin, admin_http, monkeypatch, caplog):
    def unreachable(*args, **kwargs):
        raise requests.ConnectionError(f"made-up {PASSWORD}")
    monkeypatch.setattr(admin_http, "post", unreachable)
    with pytest.raises(SoulSyncError) as error:
        admin.forget(soulsync, "SICKO MODE", "Travis Scott", REL)
    assert error.value.reason == "SoulSync isn't answering."
    assert PASSWORD not in caplog.text and PASSWORD not in str(error.value)


def test_the_admin_is_made_only_with_an_address_a_profile_and_a_password(make_config):
    url = {"url": "http://soulsync.local:8008/"}
    assert SoulSyncAdmin.from_config(make_config(soulsync=url, clean_check={"soulsync_admin": ""}), PASSWORD) is None
    assert SoulSyncAdmin.from_config(make_config(soulsync=url, clean_check={"soulsync_admin": "nova"}), "") is None
    assert SoulSyncAdmin.from_config(make_config(soulsync={"url": ""}, clean_check={"soulsync_admin": "nova"}),
                                     PASSWORD) is None
    assert SoulSyncAdmin.from_config(make_config(soulsync=url), PASSWORD) is None
    admin = SoulSyncAdmin.from_config(make_config(soulsync=url, clean_check={"soulsync_admin": " nova "}), PASSWORD)
    assert (admin.url, admin.profile) == ("http://soulsync.local:8008", "nova")


# The watchlist, which following the top artists uses - in the shapes the live check of 16 November recorded

WATCHED = f"{URL}/api/watchlist/artists"
WATCH_ADD = f"{URL}/api/watchlist/add"
WATCH_REMOVE = f"{URL}/api/watchlist/remove"


def _watched_row(row_id, name, **ids):
    """A row as GET /api/watchlist/artists lists it, with no provider id but those given."""
    row = {"id": row_id, "artist_name": name, "spotify_artist_id": None, "itunes_artist_id": None,
           "deezer_artist_id": None, "discogs_artist_id": None, "musicbrainz_artist_id": None,
           "amazon_artist_id": None, "include_albums": True}
    return dict(row, **ids)


LIBRARY_ARTIST = {"id": "abcDEF123ghiJKL456mnoP", "name": "Made Up Artist", "spotify_artist_id": None,
                  "itunes_artist_id": "1001", "deezer_id": "1002", "discogs_id": None, "musicbrainz_id": "mb-made-up",
                  "amazon_id": "am-made-up"}


def test_watched_artists_are_read_from_the_watchlist(soulsync, http):
    rows = [_watched_row(7, "Made Up Artist", deezer_artist_id="1002"), "not a row"]
    http.pages[WATCHED] = FakeResponse(200, {"success": True, "artists": rows})
    assert soulsync.watched_artists() == rows[:1]
    assert http.fetched == [(WATCHED, {}, {"Authorization": "Bearer the-key"}, {"stream": False, "redirects": True})]
    assert http.timeouts == [REQUEST_TIMEOUT]


@pytest.mark.parametrize("answer", [FakeResponse(200, {"success": True}), FakeResponse(500, {"success": False,
                                                                                             "error": "made up"})])
def test_a_watchlist_with_no_artists_list_is_an_error(soulsync, http, answer):
    http.pages[WATCHED] = answer
    with pytest.raises(SoulSyncError):
        soulsync.watched_artists()


def test_a_library_artist_is_read_by_its_id_and_none_when_its_not_there(soulsync, http):
    http.pages[f"{URL}/api/v1/library/artists/abcDEF123ghiJKL456mnoP"] = FakeResponse(
        *_envelope({"artist": LIBRARY_ARTIST, "albums": []}))
    assert soulsync.library_artist("abcDEF123ghiJKL456mnoP") == LIBRARY_ARTIST
    # An id is one part of the address, whatever it holds.
    assert soulsync.library_artist("a/b c") is None
    assert http.fetched[-1][0] == f"{URL}/api/v1/library/artists/a%2Fb%20c"


def test_follow_adds_by_the_first_provider_id_with_its_source_then_links_the_rest(soulsync, http):
    link = f"{URL}/api/watchlist/artist/1001/link-provider"
    http.answers.update({WATCH_ADD: (200, {"success": True, "message": "Added"}),
                         link: (200, {"success": True, "message": "Linked"})})
    row = _watched_row(8, "Made Up Artist", itunes_artist_id="1001", deezer_artist_id="1002")
    http.pages[WATCHED] = FakeResponse(200, {"success": True, "artists": [_watched_row(7, "Another"), row]})

    assert soulsync.follow(LIBRARY_ARTIST) == row
    # SoulSync's own order, Spotify then iTunes, Deezer, Discogs and MusicBrainz, with the source named, as a bare
    # number is read as the primary source's id. Amazon's isn't one its scan uses.
    assert [(url, body) for url, body, _ in http.posted] == [
        (WATCH_ADD, {"artist_id": "1001", "artist_name": "Made Up Artist", "source": "itunes"}),
        (link, {"provider": "deezer", "provider_id": "1002"}),
        (link, {"provider": "musicbrainz", "provider_id": "mb-made-up"})]
    assert set(http.timeouts) == {REQUEST_TIMEOUT}


def test_follow_still_gives_the_row_when_a_link_is_refused(soulsync, http, caplog):
    link = f"{URL}/api/watchlist/artist/1001/link-provider"
    http.answers.update({WATCH_ADD: (200, {"success": True}),
                         link: (409, {"success": False, "error": "Another watchlist artist ('Someone') has it"})})
    row = _watched_row(8, "Made Up Artist", itunes_artist_id="1001")
    http.pages[WATCHED] = FakeResponse(200, {"success": True, "artists": [row]})
    assert soulsync.follow(LIBRARY_ARTIST) == row
    assert "Someone" not in caplog.text and "Made Up" not in caplog.text


def test_follow_with_no_provider_id_asks_nothing(soulsync, http):
    assert soulsync.follow(dict(LIBRARY_ARTIST, itunes_artist_id=None, deezer_id="", musicbrainz_id=None)) is None
    assert http.posted == [] and http.fetched == []


def test_follow_refused_is_an_error(soulsync, http):
    http.answers[WATCH_ADD] = (500, {"success": False, "error": "Failed to add artist to watchlist"})
    with pytest.raises(SoulSyncError):
        soulsync.follow(LIBRARY_ARTIST)


def test_unfollow_removes_the_row_by_one_of_its_provider_ids(soulsync, http):
    http.answers[WATCH_REMOVE] = (200, {"success": True, "message": "Removed artist from watchlist"})
    soulsync.unfollow(_watched_row(8, "Made Up Artist", deezer_artist_id="1002", musicbrainz_artist_id="mb-x"))
    assert [(url, body) for url, body, _ in http.posted] == [(WATCH_REMOVE, {"artist_id": "1002"})]
    http.answers[WATCH_REMOVE] = (500, {"success": False, "error": "Failed to remove artist from watchlist"})
    with pytest.raises(SoulSyncError):
        soulsync.unfollow(_watched_row(8, "Made Up Artist", deezer_artist_id="1002"))


# The playlists SoulSync remakes and mirrors, which Nova leaves alone or switches to append only before adding

REMADE = f"{URL}/api/personalized/playlists"
MIRRORS = f"{URL}/api/mirrored-playlists"
MIRROR_PREFERENCES = f"{URL}/api/mirrored-playlists/3/preferences"
BEARER = {"Authorization": "Bearer the-key"}


def _mirror(mirror_id, name, server_id=None, mode=None, **more):
    """A mirror as GET /api/mirrored-playlists lists it, with a few of its many other keys."""
    return {"id": mirror_id, "source": "spotify", "source_playlist_id": f"src-{mirror_id}", "name": f"{name} (source)",
            "track_count": 12, "server_playlist_id": server_id, "sync_name": name, "sync_mode": mode, **more}


def test_the_playlists_soulsync_remakes_are_named(soulsync, http):
    http.pages[REMADE] = FakeResponse(200, {"playlists": [
        {"id": "daily_mix_1", "kind": "daily_mix", "variant": "1", "name": "Daily Mix 1", "track_count": 50},
        {"name": "Hidden Gems"}]})
    assert soulsync.remade_playlists() == ["Daily Mix 1", "Hidden Gems"]
    assert http.fetched == [(REMADE, {}, BEARER, {"stream": False, "redirects": True})]
    assert http.timeouts == [REQUEST_TIMEOUT]


def test_the_mirrors_say_which_navidrome_playlist_each_writes(soulsync, http):
    http.pages[MIRRORS] = FakeResponse(200, [_mirror(3, "Gym", "p1"), _mirror(4, "Road Trip", None, "append")])
    assert soulsync.mirrors() == [{"id": 3, "server_playlist_id": "p1", "sync_name": "Gym", "sync_mode": None},
                                  {"id": 4, "server_playlist_id": None, "sync_name": "Road Trip",
                                   "sync_mode": "append"}]
    assert http.fetched == [(MIRRORS, {}, BEARER, {"stream": False, "redirects": True})]
    assert http.timeouts == [REQUEST_TIMEOUT]


def test_no_mirrors_is_an_empty_list(soulsync, http):
    http.pages[MIRRORS] = FakeResponse(200, [])
    assert soulsync.mirrors() == []


def test_a_mirror_is_switched_to_append_only(soulsync, http):
    soulsync.set_append(3)
    assert http.patched == [(MIRROR_PREFERENCES, {"sync_mode": "append"}, BEARER)]
    assert http.timeouts == [REQUEST_TIMEOUT]


@pytest.mark.parametrize("answer", [(204, None), (200, {}), (200, {"success": True, "message": "Updated"})])
def test_an_append_answered_with_any_2xx_is_done(soulsync, http, answer):
    # The shape of its answer isn't known, so only an error in it, or a status, is taken for failing.
    http.patches[MIRROR_PREFERENCES] = answer
    soulsync.set_append(3)
    assert len(http.patched) == 1


@pytest.mark.parametrize("answer", [(500, {"success": False, "error": "made up"}), (404, None),
                                    (200, {"success": False, "error": "Mirrored playlist not found"})])
def test_an_append_which_failed_is_an_error(soulsync, http, answer):
    http.patches[MIRROR_PREFERENCES] = answer
    with pytest.raises(SoulSyncError):
        soulsync.set_append(3)


def test_a_refused_key_or_an_odd_answer_is_an_error(soulsync, http):
    http.pages[REMADE] = FakeResponse(401, NO_KEY[1])
    http.pages[MIRRORS] = FakeResponse(401, NO_KEY[1])
    http.patches[MIRROR_PREFERENCES] = NO_KEY
    for ask in (soulsync.remade_playlists, soulsync.mirrors, lambda: soulsync.set_append(3)):
        with pytest.raises(SoulSyncError) as error:
            ask()
        assert error.value.reason == KEY_REFUSED

    # A key it won't let read is the same key to check, even with no JSON to say so.
    http.pages[MIRRORS] = FakeResponse(403, None)
    with pytest.raises(SoulSyncError) as error:
        soulsync.mirrors()
    assert error.value.reason == KEY_REFUSED

    # A dict where the list of mirrors should be, and a list, or a dict with no playlists, where the other is.
    http.pages[MIRRORS] = FakeResponse(200, {"mirrors": []})
    with pytest.raises(SoulSyncError):
        soulsync.mirrors()
    for odd in ([], {"success": True}, {"playlists": "Daily Mix 1"}):
        http.pages[REMADE] = FakeResponse(200, odd)
        with pytest.raises(SoulSyncError):
            soulsync.remade_playlists()


@pytest.mark.parametrize("answer", [FakeResponse(500, [_mirror(3, "Gym")]), FakeResponse(200, None),
                                    FakeResponse(200, {"success": False, "error": "made up"}),
                                    # A row which isn't a mirror, or has no id to switch it by.
                                    FakeResponse(200, [_mirror(3, "Gym"), "not a mirror"]),
                                    FakeResponse(200, [{"sync_name": "Gym", "sync_mode": None}])])
def test_mirrors_which_cant_be_read_as_a_list_of_mirrors_are_an_error(soulsync, http, answer):
    http.pages[MIRRORS] = answer
    with pytest.raises(SoulSyncError):
        soulsync.mirrors()


def test_soulsync_out_of_reach_is_an_error_for_each(soulsync, http, caplog):
    http.fail = requests.ConnectionError("Authorization: Bearer the-key")
    for ask in (soulsync.remade_playlists, soulsync.mirrors, lambda: soulsync.set_append(3)):
        with pytest.raises(SoulSyncError) as error:
            ask()
        assert error.value.reason == "SoulSync isn't answering." and "the-key" not in caplog.text
