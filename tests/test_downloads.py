"""
Downloads Test
Checks the watch on a download - asked of SoulSync, said to be requested, then
followed until Navidrome has it or its time is up, and what landed heard and a clean
copy swapped - with fakes in place of SoulSync, Navidrome, the ears and the swapper,
and a fake clock which only moves on when the watch waits, so nothing real is asked
and no test waits out a poll.
"""

import functools
import logging
import threading
from types import SimpleNamespace

import pytest
import requests

from tests.fake_approvals import FakeApprovals
from voice_assistant.actions import Action
from voice_assistant.clean_ear import Verdict
from voice_assistant.clean_store import CleanStore
from voice_assistant.clean_swap import Ears, Outcome
from voice_assistant.downloads import NOT_SET_UP, POLL_EVERY, SCAN_AFTER, Downloads, Wanted, _bare_title
from voice_assistant.navidrome import NavidromeLibrary
from voice_assistant.soulsync import (KEY_REFUSED, MCP_OFF, NOT_ANSWERING, NOT_FOUND, AlbumRequest, SongRequest,
                                      SoulSync, SoulSyncError)

GOOBA = {"id": "1", "title": "Gooba", "artist": "6ix9ine", "album": "Gooba", "explicitStatus": "explicit"}
GOOBA_CLEAN = {"id": "2", "title": "Gooba (Clean)", "artist": "6ix9ine", "album": "Gooba", "explicitStatus": ""}
GOOBA_WANTED = Wanted("Gooba", "6ix9ine", "track")
RUMOURS_WANTED = Wanted("Rumours", "Fleetwood Mac", "album")
RUMOURS_TITLES = ["Dreams", "Go Your Own Way", "The Chain"]

GOOBA_REQUESTED = ("kitchen", "Gooba by 6ix9ine requested.")
GOOBA_DOWNLOADED = ("kitchen", "Gooba by 6ix9ine has been downloaded.")
GOOBA_UNSURE = ("kitchen", "Gooba by 6ix9ine has been downloaded, but I couldn't tell if it's the clean version.")
GOOBA_UNCHECKED = ("kitchen", "Gooba by 6ix9ine has been downloaded, but I couldn't check it for a clean version.")

# What Claude is told after "Asked SoulSync for X." - never said, as the outcome is said
# later - for a song, and for an album, which takes longer.
SONG_ON_ITS_WAY = "It usually takes a few minutes; Nova will say when it's in the library, or that it couldn't get it."
ALBUM_ON_ITS_WAY = ("An album takes longer than a song; Nova will say when it's in the library, or how much of it "
                    "came down.")

# "Endless by Gunner", as speech to text heard Gunna's "endless" live on 12 November,
# and the song SoulSync found for it.
ENDLESS_ASKED = Wanted("Endless", "Gunner", "track")
ENDLESS_FOUND = SongRequest("endless", "Gunna", {"id": "made-up-track", "name": "endless", "artists": ["Gunna"]})
ENDLESS = {"id": "e1", "title": "endless", "artist": "Gunna", "album": "made-up album", "explicitStatus": "explicit"}
ENDLESS_CLEAN = {"id": "e2", "title": "endless (Clean)", "artist": "Gunna", "album": "made-up album",
                 "explicitStatus": ""}
# Where Navidrome is looked in for it: under the name asked for, then by the title asked
# for and SoulSync's artist.
ENDLESS_NAMES = (("Endless", "Gunner"), ("Endless", "Gunna"))


def _track(number, title, clean=False, album="Rumours", album_id=None):
    """
    One of Rumours' tracks as Navidrome describes it - marked clean, or unmarked as most
    are - with its album's name, id and artist, as find_album_tracks gives them.
    """
    return {"id": f"r{number}", "title": title, "artist": "Fleetwood Mac", "album": album,
            "albumId": album_id or album, "albumArtist": "Fleetwood Mac", "explicitStatus": "clean" if clean else ""}


DREAMS, GO_YOUR_OWN_WAY, THE_CHAIN = (_track(number, title) for number, title in enumerate(RUMOURS_TITLES, 1))
# A bonus track of a deluxe edition, which isn't one SoulSync named.
SILVER_SPRINGS = _track(4, "Silver Springs")
RUMOURS_ALL = ["Second Hand News", "Dreams", "Never Going Back Again", "Don't Stop", "Go Your Own Way", "Songbird",
               "The Chain", "You Make Loving Fun", "I Don't Want to Know", "Oh Daddy", "Gold Dust Woman"]


def _answer(answers):
    """
    A fake's next answer - the last one again once they run out. One which is an
    error is raised, and one which is a function is called for the answer.
    """
    answer = answers.pop(0) if len(answers) > 1 else answers[0]
    if callable(answer):
        answer = answer()
    if isinstance(answer, Exception):
        raise answer
    return answer


class FakeTime:
    """The clock the watch reads, which moves on only when the watch waits."""

    def __init__(self):
        self.time = 0.0
        # When each message was said, by this clock.
        self.said_at = []
        # Things to do once the clock reaches a time, as (time, what).
        self.later = []

    def now(self):
        return self.time

    def wait(self, seconds):
        self.time += seconds
        for when, then in [item for item in self.later if item[0] <= self.time]:
            self.later.remove((when, then))
            then()
        return False

    @property
    def given_up_at(self):
        return self.said_at[-1]


class FakeSoulSync:
    """Stands in for SoulSync: records what it searches for and what it's asked for."""

    def __init__(self, album=None, fail=None, song=None, wishlist_fail=None):
        self.album = album or AlbumRequest(None, [], 3, 3)
        self.fail = fail
        # The song it finds - by default the one asked for, named as it was asked for.
        self.song = song
        self.wishlist_fail = wishlist_fail
        self.found = None
        self.searched = []
        self.requested = []

    def _request(self, title, artist, answer):
        self.requested.append((title, artist))
        if self.fail:
            raise self.fail
        return answer

    def find_song(self, title, artist):
        self.searched.append((title, artist))
        if self.fail:
            raise self.fail
        self.found = self.song or SongRequest(title, artist, {"id": "made-up-track", "name": title})
        return self.found

    def request_song(self, song):
        assert song is self.found
        self.requested.append((song.title, song.artist))
        if self.wishlist_fail:
            raise self.wishlist_fail

    def request_album(self, title, artist):
        return self._request(title, artist, self.album)


class FakeLibrary:
    """
    Stands in for Navidrome: each find_* call is answered by the next of its list -
    the first being what it had before the request - and each scan is noted by the
    fake clock's time. A song is only ever looked for under one of the names given.
    """

    def __init__(self, clock, songs=([],), tracks=([],), scans_allowed=True, names=(("Gooba", "6ix9ine"),),
                 albums=(("Rumours", "Fleetwood Mac"),)):
        self.clock = clock
        self.songs = list(songs)
        self.tracks = list(tracks)
        self.scans_allowed = scans_allowed
        self.names = names
        self.albums = albums
        self.looked_for = []
        self.scans = []

    def find_songs(self, title, artist, also=None):
        assert (title, artist) in self.names
        self.looked_for.append((title, artist))
        return list(_answer(self.songs))

    def find_album_tracks(self, title, artist):
        assert (title, artist) in self.albums
        return list(_answer(self.tracks))

    def start_scan(self):
        self.scans.append(self.clock.now())
        return self.scans_allowed


class FakeEars(Ears):
    """
    Stands in for Ears: hears each copy as the kind given for its id - explicit unless
    told otherwise - with the real Ears' way of choosing among several copies.
    """

    HEARD = {"explicit": 5, "clean": 1, "unsure": 2}

    def __init__(self):
        # Made as an Ears is, with nothing behind it: hear is the fake's own.
        super().__init__(None, lambda: None, SimpleNamespace(lock=threading.RLock()), lambda library: None, None)
        self.kinds = {}
        self.heard = []

    def hear(self, song, download=False):
        self.heard.append((song["id"], download))
        kind = self.kinds.get(song["id"], "explicit")
        if isinstance(kind, Exception):
            raise kind
        return Verdict(kind, 10, self.HEARD.get(kind, 0), "listening took too long" if kind == "not_judged" else "")


class FakeSwapper:
    """Stands in for the Swapper: each swap ends as given for the copy's id - swapped unless told otherwise."""

    def __init__(self):
        self.outcomes = {}
        self.swaps = []
        self.stopped = False
        self.resumed = []
        # What a resume waits for before it starts; the saved swaps it carries on; and what
        # each waits for before it ends.
        self.hold = threading.Event()
        self.hold.set()
        self.saved = []
        self.release = threading.Event()
        self.release.set()

    def swap(self, song, wanted, satellite, quiet):
        self.swaps.append((song["id"], wanted, satellite, quiet))
        return self.outcomes.get(song["id"], Outcome("swapped"))

    def stop(self):
        self.stopped = True

    def resume(self, begin=None, end=None):
        self.resumed.append(threading.current_thread().name)
        self.hold.wait(5)
        for swap in self.saved:
            begin(swap)
            self.release.wait(5)
            end(swap)


def heard_clean(store, *tracks):
    """Keeps a clean verdict for each track, as Ears would once it had heard it clean."""
    for track in tracks:
        store.set_verdict(track["id"], 4096, "2026-11-16T10:00:00Z", Verdict("clean", 10, 1), track["title"])


@pytest.fixture
def clock():
    return FakeTime()


@pytest.fixture
def ears():
    return FakeEars()


@pytest.fixture
def swapper():
    return FakeSwapper()


@pytest.fixture
def store(tmp_path):
    return CleanStore(str(tmp_path / "clean_songs.json"))


@pytest.fixture
def said():
    """What the watch said, as (satellite, message)."""
    return []


@pytest.fixture
def config(make_config):
    return make_config(music={"song_download_minutes": 10, "album_download_minutes": 30})


@pytest.fixture
def set_up(make_config, monkeypatch):
    """Settings with SoulSync and Navidrome set up - made up - so a download can be asked for."""
    monkeypatch.setenv("SOULSYNC_API_KEY", "made-up-key")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "made-up-password")
    return make_config(music={"song_download_minutes": 10, "album_download_minutes": 30},
                       soulsync={"url": "http://soulsync.local:8008"},
                       navidrome={"url": "http://navidrome.local:4533", "username": "nova"})


@pytest.fixture
def watching(config, clock, said, ears, swapper, store):
    """
    Makes Downloads which asks the fakes given, on the fake clock - with the settings given,
    if any - and hears and swaps with the test's ears and swapper, or without a swapper.
    """
    def make(soulsync, library, settings=None, swapping=True):
        def say(satellite, message):
            said.append((satellite, message))
            clock.said_at.append(clock.time)

        return Downloads(settings or config, say, soulsync=lambda: soulsync, library=lambda: library, now=clock.now,
                         wait=clock.wait, ears=ears, swapper=swapper if swapping else None, store=store)

    return make


def _watches():
    return [thread for thread in threading.enumerate() if thread.name == "download"]


def _all_ended():
    """Waits a few seconds at most for every watch's thread to end, and says whether they all did."""
    watches = _watches()
    for thread in watches:
        thread.join(timeout=5)
    return not any(thread.is_alive() for thread in watches)


@pytest.fixture
def running(config, clock, said, ears, swapper, store):
    """
    Makes Downloads whose watches run on threads of their own and wait between polls
    as Nova's do, on the stop event - so each waits until stop, which ends every one
    at once. Anything a test leaves running is stopped afterwards.
    """
    made = []

    def make(soulsync, library, settings=None, **more):
        downloads = Downloads(settings or config, lambda satellite, message: said.append((satellite, message)),
                              soulsync=lambda: soulsync, library=lambda: library, now=clock.now, ears=ears,
                              swapper=swapper, store=store, **more)
        made.append(downloads)
        return downloads

    yield make
    for downloads in made:
        downloads.stop()
    _all_ended()


# What's asked for

def test_a_query_is_a_song_unless_its_an_album():
    assert Wanted.from_query("Rumours by Fleetwood Mac", "album") == RUMOURS_WANTED
    assert Wanted.from_query("Gooba by 6ix9ine", "track") == GOOBA_WANTED
    assert Wanted.from_query("Gooba", None) == Wanted("Gooba", None, "track")


def test_its_named_as_asked_for_and_told_apart_however_its_spelt():
    assert GOOBA_WANTED.name == "Gooba by 6ix9ine"
    assert Wanted("Gooba", None, "track").name == "Gooba"
    assert Wanted("gooba", "6IX9INE", "track").key == GOOBA_WANTED.key
    assert Wanted("Gooba", "6ix9ine", "album").key != GOOBA_WANTED.key
    assert Wanted("Gooba", None, "track").key != GOOBA_WANTED.key


# A song

def test_a_song_is_requested_then_said_downloaded_once_navidrome_has_it(watching, clock, said, ears, swapper):
    # Navidrome: nothing before, nothing, then Gooba - heard explicit.
    soulsync = FakeSoulSync()
    downloads = watching(soulsync, FakeLibrary(clock, songs=[[], [], [GOOBA]]))

    assert downloads._watch(GOOBA_WANTED, "kitchen") == "Gooba by 6ix9ine has been downloaded."
    assert said == [("kitchen", "Gooba by 6ix9ine requested."), ("kitchen", "Gooba by 6ix9ine has been downloaded.")]
    assert soulsync.requested == [("Gooba", "6ix9ine")]
    # Heard before it was said, as a download's - and, explicit, never swapped.
    assert ears.heard == [("1", True)] and swapper.swaps == []


# What landed, heard (decision 0022, section 3)

def test_a_copy_named_clean_but_heard_explicit_is_downloaded(watching, clock, said, ears, swapper):
    # SoulSync's tagging calls explicit songs clean, so a name is never believed.
    downloads = watching(FakeSoulSync(), FakeLibrary(clock, songs=[[], [GOOBA_CLEAN]]))

    assert downloads._watch(GOOBA_WANTED, "kitchen") == "Gooba by 6ix9ine has been downloaded."
    assert ears.heard == [("2", True)] and swapper.swaps == []


@pytest.mark.parametrize("kind, outcome", [("unsure", GOOBA_UNSURE), ("not_judged", GOOBA_UNCHECKED),
                                           ("too_few", GOOBA_DOWNLOADED)])
def test_a_song_heard_neither_way_says_so_and_is_never_swapped(watching, clock, said, ears, swapper, kind, outcome):
    ears.kinds["1"] = kind
    downloads = watching(FakeSoulSync(), FakeLibrary(clock, songs=[[], [GOOBA]]))

    assert downloads._watch(GOOBA_WANTED, "kitchen") == outcome[1]
    assert said == [GOOBA_REQUESTED, outcome] and swapper.swaps == []


@pytest.mark.parametrize("swapped, outcome", [
    (Outcome("swapped"), "Gooba by 6ix9ine has been downloaded."),
    (Outcome("only_clean", "3 copies came down clean"), "Gooba by 6ix9ine only came down clean, so I've kept it."),
    (Outcome("couldnt", "Navidrome still lists it"),
     "Gooba by 6ix9ine has been downloaded, but it's the clean version, and I couldn't swap it: Navidrome still "
     "lists it."),
    (Outcome("couldnt", "SoulSync admin profile isn't set up"),
     "Gooba by 6ix9ine has been downloaded, but it's the clean version, and I couldn't swap it: SoulSync admin "
     "profile isn't set up."),
    # The new copy was set aside and the clean original put back: nothing has been downloaded.
    (Outcome("unsure", "heard 2 of its 10 swears"),
     "Gooba by 6ix9ine came down again, but not plainly explicit, so you've still got the clean version. The new "
     "copy is set aside."),
    (Outcome("not_judged", "listening took too long"),
     "Gooba by 6ix9ine came down again, but I couldn't check the new copy, so you've still got the clean version. "
     "The new copy is set aside."),
])
def test_a_song_heard_clean_is_swapped_before_anything_is_said(watching, clock, said, ears, swapper, swapped,
                                                               outcome):
    ears.kinds["1"] = "clean"
    swapper.outcomes["1"] = swapped
    downloads = watching(FakeSoulSync(), FakeLibrary(clock, songs=[[], [GOOBA]]))

    assert downloads._watch(GOOBA_WANTED, "kitchen") == outcome
    assert said == [GOOBA_REQUESTED, ("kitchen", outcome)]
    # Swapped as a download's, not quietly, told which satellite asked.
    assert swapper.swaps == [("1", GOOBA_WANTED, "kitchen", False)]


def test_a_clean_song_is_swapped_by_the_title_asked_for_and_soulsyncs_artist(watching, clock, said, ears, swapper):
    # "Endless by Gunner": named as SoulSync found it, asked for again as Navidrome is searched.
    ears.kinds["e1"] = "clean"
    library = FakeLibrary(clock, songs=[[], [ENDLESS]], names=ENDLESS_NAMES)

    watching(FakeSoulSync(song=ENDLESS_FOUND), library)._watch(ENDLESS_ASKED, "kitchen")

    assert swapper.swaps == [("e1", Wanted("Endless", "Gunna", "track"), "kitchen", False)]
    assert said[-1] == ("kitchen", "endless by Gunna has been downloaded.")


def test_of_two_copies_landed_the_one_heard_explicit_is_the_download(watching, clock, said, ears, swapper):
    ears.kinds["2"] = "clean"
    downloads = watching(FakeSoulSync(), FakeLibrary(clock, songs=[[], [GOOBA_CLEAN, GOOBA]]))

    assert downloads._watch(GOOBA_WANTED, "kitchen") == "Gooba by 6ix9ine has been downloaded."
    assert ears.heard == [("2", True), ("1", True)] and swapper.swaps == []


def test_hearing_that_fails_unforeseen_still_says_its_downloaded_unchecked(watching, clock, said, ears, caplog):
    ears.kinds["1"] = RuntimeError("made-up bug")
    downloads = watching(FakeSoulSync(), FakeLibrary(clock, songs=[[], [GOOBA]]))

    assert downloads._watch(GOOBA_WANTED, "kitchen") == GOOBA_UNCHECKED[1]
    assert "made-up bug" in caplog.text


def test_listening_that_fails_for_one_download_is_said_and_the_next_is_heard(watching, clock, said, ears):
    # Review Focus 4: the worker failing mid-song, then the next song.
    ears.kinds["1"] = "not_judged"
    watching(FakeSoulSync(), FakeLibrary(clock, songs=[[], [GOOBA]]))._watch(GOOBA_WANTED, "kitchen")
    assert said[-1] == GOOBA_UNCHECKED

    watching(FakeSoulSync(song=ENDLESS_FOUND), FakeLibrary(clock, songs=[[], [ENDLESS]], names=ENDLESS_NAMES))._watch(
        ENDLESS_ASKED, "kitchen")
    assert said[-1] == ("kitchen", "endless by Gunna has been downloaded.")
    assert ears.heard == [("1", True), ("e1", True)]


def test_a_song_found_at_the_last_look_is_heard_too(watching, clock, said, ears, swapper):
    # Only in Navidrome at its time limit, and heard clean: swapped, never called downloaded unheard.
    ears.kinds["1"] = "clean"
    swapper.outcomes["1"] = Outcome("only_clean")
    library = FakeLibrary(clock)
    library.songs = [[], lambda: [GOOBA] if clock.time >= 600 else []]

    assert watching(FakeSoulSync(), library)._watch(GOOBA_WANTED, "kitchen") == (
        "Gooba by 6ix9ine only came down clean, so I've kept it.")


def test_a_swap_nova_stopped_says_nothing(watching, clock, said, ears, swapper):
    ears.kinds["1"] = "clean"
    downloads = watching(FakeSoulSync(), FakeLibrary(clock, songs=[[], [GOOBA]]))

    def stopped(song, wanted, satellite, quiet):
        downloads.stop()
        return Outcome("stopped")

    swapper.swap = stopped
    assert downloads._watch(GOOBA_WANTED, "kitchen") == ""
    assert said == [GOOBA_REQUESTED]


def test_an_old_clean_copy_isnt_taken_for_the_download(watching, clock, said, store):
    # Navidrome: [GOOBA_CLEAN] before, heard clean, the same at the first poll, then [GOOBA_CLEAN, GOOBA].
    # Without a swapper, so it's requested rather than swapped.
    heard_clean(store, GOOBA_CLEAN)
    library = FakeLibrary(clock, songs=[[GOOBA_CLEAN], [GOOBA_CLEAN], [GOOBA_CLEAN, GOOBA]])
    downloads = watching(FakeSoulSync(), library, swapping=False)

    downloads._watch(GOOBA_WANTED, "kitchen")

    assert said == [GOOBA_REQUESTED, GOOBA_DOWNLOADED]
    assert clock.said_at[-1] == 2 * POLL_EVERY


def test_a_song_is_given_up_on_after_ten_minutes(watching, clock, said):
    library = FakeLibrary(clock)
    downloads = watching(FakeSoulSync(), library)

    assert downloads._watch(GOOBA_WANTED, "kitchen") == "I couldn't download Gooba by 6ix9ine."
    assert said[-1] == ("kitchen", "I couldn't download Gooba by 6ix9ine.")
    assert 600 <= clock.given_up_at < 600 + POLL_EVERY
    # SoulSync never saying it's done, Navidrome is asked to scan once, in time for the last look.
    assert library.scans == [600 - SCAN_AFTER - POLL_EVERY]


def test_a_song_soulsync_never_says_is_done_is_found_by_the_scan_before_its_time_is_up(watching, clock, said):
    # Left "downloading" by SoulSync, and only in Navidrome once it has scanned.
    library = FakeLibrary(clock)
    library.songs = [[], lambda: [GOOBA] if library.scans else []]
    downloads = watching(FakeSoulSync(), library)

    downloads._watch(GOOBA_WANTED, "kitchen")

    assert said == [GOOBA_REQUESTED, GOOBA_DOWNLOADED]
    assert clock.said_at[-1] == 600 - SCAN_AFTER  # the poll after the scan
    assert library.scans == [600 - SCAN_AFTER - POLL_EVERY]


def test_there_is_only_ever_one_scan(watching, config, clock, said):
    # Scanned as its ten minutes ran out, then the limit is raised to an hour - with
    # Navidrome still without it, and not scanned again.
    clock.later.append((530, lambda: config.apply({("music", "song_download_minutes"): 60})))
    library = FakeLibrary(clock)
    downloads = watching(FakeSoulSync(), library)

    downloads._watch(GOOBA_WANTED, "kitchen")

    assert library.scans == [600 - SCAN_AFTER - POLL_EVERY]
    assert 3600 <= clock.given_up_at < 3600 + POLL_EVERY


def test_the_time_limits_are_read_as_they_are_now(watching, config, clock, said):
    # music.song_download_minutes changed from 10 to 2 a minute in.
    clock.later.append((60, lambda: config.apply({("music", "song_download_minutes"): 2})))
    downloads = watching(FakeSoulSync(), FakeLibrary(clock))

    downloads._watch(GOOBA_WANTED, "kitchen")

    assert said[-1] == ("kitchen", "I couldn't download Gooba by 6ix9ine.")
    assert 120 <= clock.given_up_at < 120 + POLL_EVERY


def test_a_song_is_looked_for_and_named_as_soulsync_found_it(watching, clock, said):
    # A misheard "Endless by Gunner": Navidrome has nothing of Gunna's before, then endless.
    soulsync = FakeSoulSync(song=ENDLESS_FOUND)
    library = FakeLibrary(clock, songs=[[], [], [ENDLESS]], names=ENDLESS_NAMES)
    downloads = watching(soulsync, library)

    assert downloads._watch(ENDLESS_ASKED, "kitchen") == "endless by Gunna has been downloaded."
    assert said == [("kitchen", "endless by Gunna requested."), ("kitchen", "endless by Gunna has been downloaded.")]
    assert soulsync.searched == [("Endless", "Gunner")] and soulsync.requested == [("endless", "Gunna")]
    assert set(library.looked_for) == {("Endless", "Gunna")}

    # And given up on by that name too.
    watching(FakeSoulSync(song=ENDLESS_FOUND), FakeLibrary(clock, names=ENDLESS_NAMES))._watch(ENDLESS_ASKED,
                                                                                               "kitchen")
    assert said[-1] == ("kitchen", "I couldn't download endless by Gunna.")


def test_a_song_is_found_however_its_import_tagged_it(watching, clock, said):
    # SoulSync found "endless (feat. Made Up)", and filed it as plain "endless" - so
    # it's looked for by the title asked for, though named as SoulSync found it.
    featuring = SongRequest("endless (feat. Made Up)", "Gunna", {"id": "made-up-track"})
    library = FakeLibrary(clock, songs=[[], [ENDLESS]], names=(("Endless", "Gunna"),))
    downloads = watching(FakeSoulSync(song=featuring), library)

    assert downloads._watch(ENDLESS_ASKED, "kitchen") == "endless (feat. Made Up) by Gunna has been downloaded."
    assert said[0] == ("kitchen", "endless (feat. Made Up) by Gunna requested.")
    assert clock.said_at[-1] == POLL_EVERY


def test_a_song_navidrome_has_by_the_title_asked_for_and_soulsyncs_artist_isnt_requested(watching, clock, said,
                                                                                        store):
    # Named by the copy that isn't clean.
    heard_clean(store, ENDLESS_CLEAN)
    soulsync = FakeSoulSync(song=ENDLESS_FOUND)
    downloads = watching(soulsync, FakeLibrary(clock, songs=[[ENDLESS_CLEAN, ENDLESS]], names=ENDLESS_NAMES))

    assert downloads._watch(ENDLESS_ASKED, "kitchen") == "You've already got endless by Gunna."
    assert said == [("kitchen", "You've already got endless by Gunna.")]
    assert soulsync.requested == [] and clock.time == 0


def test_a_song_had_only_clean_by_the_title_asked_for_and_soulsyncs_artist_is_requested(watching, clock, said,
                                                                                      store):
    # Heard clean before. And the clean copy it had isn't taken for the download. Without a
    # swapper, which would swap it - see the next test.
    heard_clean(store, ENDLESS_CLEAN)
    soulsync = FakeSoulSync(song=ENDLESS_FOUND)
    library = FakeLibrary(clock, songs=[[ENDLESS_CLEAN], [ENDLESS_CLEAN], [ENDLESS_CLEAN, ENDLESS]],
                          names=ENDLESS_NAMES)

    watching(soulsync, library, swapping=False)._watch(ENDLESS_ASKED, "kitchen")

    assert said == [("kitchen", "endless by Gunna requested."), ("kitchen", "endless by Gunna has been downloaded.")]
    assert soulsync.requested == [("endless", "Gunna")]
    assert clock.said_at[-1] == 2 * POLL_EVERY


@pytest.mark.parametrize("heard, swapped, outcome", [
    ("clean", Outcome("swapped"), "endless by Gunna has been downloaded."),
    ("clean", Outcome("only_clean"), "endless by Gunna only came down clean, so I've kept it."),
    ("clean", Outcome("couldnt", "Navidrome still lists it"),
     "I couldn't download endless by Gunna, so you've still got the clean version: Navidrome still lists it."),
    # Heard again first, in case its file changed since: never swapped unless it's still clean.
    ("clean", Outcome("unsure", "heard 2 of its 10 swears"),
     "endless by Gunna came down again, but not plainly explicit, so you've still got the clean version. The new "
     "copy is set aside."),
    ("clean", Outcome("not_judged", "listening took too long"),
     "endless by Gunna came down again, but I couldn't check the new copy, so you've still got the clean version. "
     "The new copy is set aside."),
    ("explicit", None, "You've already got endless by Gunna."),
    ("unsure", None, "You've already got endless by Gunna, but I couldn't tell if it's the clean version."),
    ("not_judged", None, "You've already got endless by Gunna, but I couldn't check it for a clean version."),
])
def test_a_yes_to_the_explicit_one_swaps_the_copy_heard_clean(watching, clock, said, store, ears, swapper, heard,
                                                              swapped, outcome):
    # SoulSync's wishlist would drop a plain request as "already owned", so the clean copy is swapped.
    heard_clean(store, ENDLESS_CLEAN)
    ears.kinds["e2"] = heard
    if swapped:
        swapper.outcomes["e2"] = swapped
    soulsync = FakeSoulSync(song=ENDLESS_FOUND)

    watching(soulsync, FakeLibrary(clock, songs=[[ENDLESS_CLEAN]], names=ENDLESS_NAMES))._watch(ENDLESS_ASKED,
                                                                                                "kitchen")

    assert said == [("kitchen", "endless by Gunna requested."), ("kitchen", outcome)]
    assert soulsync.requested == [] and ears.heard == [("e2", True)]
    assert swapper.swaps == ([("e2", Wanted("Endless", "Gunna", "track"), "kitchen", False)] if swapped else [])


def test_a_song_soulsync_found_nothing_for_is_named_as_asked_for(watching, clock, said):
    library = FakeLibrary(clock, names=())
    watching(FakeSoulSync(fail=SoulSyncError(NOT_FOUND)), library)._watch(ENDLESS_ASKED, "kitchen")

    assert said == [("kitchen", "I couldn't download Endless by Gunner. SoulSync couldn't find it.")]
    assert library.looked_for == []


def test_a_song_soulsync_found_but_couldnt_request_is_named_as_found(watching, clock, said):
    soulsync = FakeSoulSync(song=ENDLESS_FOUND, wishlist_fail=SoulSyncError(KEY_REFUSED))
    watching(soulsync, FakeLibrary(clock, names=ENDLESS_NAMES))._watch(ENDLESS_ASKED, "kitchen")

    assert said == [("kitchen", f"I couldn't download endless by Gunna. {KEY_REFUSED}")]


# An album

def test_an_album_is_downloaded_once_every_track_soulsync_named_is_in(watching, clock, said):
    # AlbumRequest(id, ["Dreams", "Go Your Own Way", "The Chain"], 3, 3); Navidrome's grow by one a
    # poll, with a bonus track SoulSync didn't name among them, which isn't one of the three.
    soulsync = FakeSoulSync(album=AlbumRequest("made-up-album", RUMOURS_TITLES, 3, 3))
    library = FakeLibrary(clock, tracks=[[], [DREAMS, SILVER_SPRINGS], [DREAMS, SILVER_SPRINGS, GO_YOUR_OWN_WAY],
                                         [DREAMS, SILVER_SPRINGS, GO_YOUR_OWN_WAY, THE_CHAIN]])
    downloads = watching(soulsync, library)

    assert downloads._watch(RUMOURS_WANTED, "kitchen") == "Rumours by Fleetwood Mac has been downloaded."
    assert said == [("kitchen", "Rumours by Fleetwood Mac requested."),
                    ("kitchen", "Rumours by Fleetwood Mac has been downloaded.")]
    assert clock.said_at[-1] == 3 * POLL_EVERY
    assert soulsync.requested == [("Rumours", "Fleetwood Mac")]


def test_an_album_soulsync_only_counted_is_downloaded_once_that_many_new_tracks_are_in(watching, clock, said):
    # As SoulSync answers: the album's track count, how many it queued, and no titles. An
    # old clean copy of one track was in Navidrome before, and is neither one of the
    # three nor clean.
    old = _track(0, "Dreams", clean=True, album="Rumours (Clean)")
    library = FakeLibrary(clock, tracks=[[old], [old, DREAMS], [old, DREAMS, GO_YOUR_OWN_WAY],
                                         [old, DREAMS, GO_YOUR_OWN_WAY, THE_CHAIN]])
    downloads = watching(FakeSoulSync(album=AlbumRequest(None, [], 3, 3)), library)

    downloads._watch(RUMOURS_WANTED, "kitchen")

    assert said[-1] == ("kitchen", "Rumours by Fleetwood Mac has been downloaded.")
    assert clock.said_at[-1] == 3 * POLL_EVERY


def test_a_part_owned_album_is_downloaded_once_what_soulsync_queued_has_landed(watching, clock, said):
    # Two of its eleven tracks were in Navidrome already, so SoulSync queued nine.
    tracks = [_track(number, title) for number, title in enumerate(RUMOURS_ALL, 1)]
    had = [track for track in tracks if track["title"] in ("Dreams", "The Chain")]
    queued = [track for track in tracks if track not in had]
    library = FakeLibrary(clock, tracks=[had, had + queued[:5], had + queued])
    downloads = watching(FakeSoulSync(album=AlbumRequest(None, [], 11, 9)), library)

    assert downloads._watch(RUMOURS_WANTED, "kitchen") == "Rumours by Fleetwood Mac has been downloaded."
    assert clock.said_at[-1] == 2 * POLL_EVERY  # long before its time limit

    # With only five of the nine in by its time limit, it's out of the nine.
    library = FakeLibrary(clock, tracks=[had, had + queued[:5]])
    watching(FakeSoulSync(album=AlbumRequest(None, [], 11, 9)), library)._watch(RUMOURS_WANTED, "kitchen")
    assert said[-1] == ("kitchen", "Only 5 of the 9 tracks of Rumours by Fleetwood Mac came down.")


def test_an_album_soulsync_queued_none_of_is_said_at_once(watching, clock, said):
    downloads = watching(FakeSoulSync(album=AlbumRequest(None, [], 11, 0)), FakeLibrary(clock))
    message = ("I couldn't download Rumours by Fleetwood Mac. SoulSync says it already has every track, "
               "or is already getting them.")

    assert downloads._watch(RUMOURS_WANTED, "kitchen") == message
    assert said == [("kitchen", message)]
    assert clock.time == 0  # not waited for at all


def test_a_clean_copy_and_the_explicit_one_of_a_track_count_once(watching, clock, said):
    # Dreams twice, once clean, and Go Your Own Way - but not The Chain.
    landed = [DREAMS, _track(5, "Dreams (Clean)"), GO_YOUR_OWN_WAY]
    downloads = watching(FakeSoulSync(album=AlbumRequest(None, [], 3, 3)), FakeLibrary(clock, tracks=[[], landed]))

    downloads._watch(RUMOURS_WANTED, "kitchen")

    assert said[-1] == ("kitchen", "Only 2 of the 3 tracks of Rumours by Fleetwood Mac came down.")
    assert 1800 <= clock.given_up_at < 1800 + POLL_EVERY


def test_only_a_part_saying_clean_is_taken_off_a_title():
    # The clean bracket goes first, so the dashed part before it is read for what it is:
    # a live take or a remaster stays a track of its own, rather than merging with Dreams.
    assert _bare_title("Dreams - Live at the Forum (Clean)") == _bare_title("Dreams - Live at the Forum")
    assert _bare_title("Dreams - 2004 Remaster (Clean)") == _bare_title("Dreams - 2004 Remaster")
    assert _bare_title("Dreams - Live at the Forum (Clean)") != "dreams"
    # A clean part after an en dash, or before a credit, is still one.
    assert _bare_title("Dreams \u2013 Radio Edit") == _bare_title("Dreams - Radio Edit (feat. X)") == "dreams"


def test_a_live_take_of_a_track_is_a_track_of_its_own(watching, clock, said):
    # A deluxe edition's live Dreams beside the studio one.
    landed = [DREAMS, _track(5, "Dreams (Live)"), THE_CHAIN]
    downloads = watching(FakeSoulSync(album=AlbumRequest(None, [], 3, 3)), FakeLibrary(clock, tracks=[[], landed]))

    downloads._watch(RUMOURS_WANTED, "kitchen")

    assert said[-1] == ("kitchen", "Rumours by Fleetwood Mac has been downloaded.")
    assert clock.said_at[-1] == POLL_EVERY

    # A radio edit, though, is a clean copy of Dreams, so it and Dreams are one track.
    landed = [DREAMS, _track(6, "Dreams - Radio Edit"), _track(5, "Dreams (Live)"), THE_CHAIN]
    watching(FakeSoulSync(album=AlbumRequest(None, [], 4, 4)),
             FakeLibrary(clock, tracks=[[], landed]))._watch(RUMOURS_WANTED, "kitchen")
    assert said[-1] == ("kitchen", "Only 3 of the 4 tracks of Rumours by Fleetwood Mac came down.")


def test_each_track_of_an_album_is_heard_and_the_clean_ones_swapped_one_at_a_time(watching, clock, said, ears,
                                                                                   swapper):
    # Marked as Navidrome marks them, which counts for nothing: Dreams is marked clean and heard
    # explicit; Go Your Own Way and The Chain are heard clean.
    tracks = [_track(1, "Dreams", clean=True), _track(2, "Go Your Own Way"), _track(3, "The Chain")]
    ears.kinds.update({"r2": "clean", "r3": "clean"})
    watching(FakeSoulSync(), FakeLibrary(clock, tracks=[[], tracks]))._watch(RUMOURS_WANTED, "kitchen")

    assert ears.heard == [("r1", True), ("r2", True), ("r3", True)]
    # Each asked for again as a song, by its own title and artist, as a download's.
    assert swapper.swaps == [("r2", Wanted("Go Your Own Way", "Fleetwood Mac", "track"), "kitchen", False),
                             ("r3", Wanted("The Chain", "Fleetwood Mac", "track"), "kitchen", False)]
    assert said[-1] == ("kitchen", "Rumours by Fleetwood Mac has been downloaded. 2 of its tracks came down clean, "
                                   "so I swapped them for the explicit ones.")


@pytest.mark.parametrize("tagged, title", [("The Chain (Clean)", "The Chain"), ("The Chain - Radio Edit", "The Chain"),
                                          ("The Chain [Clean Version] (feat. X)", "The Chain (feat. X)"),
                                          ("The Chain (Live)", "The Chain (Live)")])
def test_a_clean_album_track_is_swapped_without_its_clean_note(watching, clock, said, ears, swapper, tagged, title):
    # SoulSync would never find an explicit "The Chain (Clean)", nor Navidrome list one.
    tracks = [DREAMS, GO_YOUR_OWN_WAY, _track(3, tagged)]
    ears.kinds["r3"] = "clean"
    watching(FakeSoulSync(), FakeLibrary(clock, tracks=[[], tracks]))._watch(RUMOURS_WANTED, "kitchen")

    assert swapper.swaps == [("r3", Wanted(title, "Fleetwood Mac", "track"), "kitchen", False)]


def test_an_album_says_how_many_were_swapped_and_how_many_werent(watching, clock, said, ears, swapper):
    tracks = [_track(number, title) for number, title in enumerate(RUMOURS_ALL[:7], 1)]
    ears.kinds.update({"r1": "clean", "r2": "clean", "r3": "clean", "r4": "clean", "r5": "unsure", "r6": "not_judged"})
    swapper.outcomes.update({"r2": Outcome("only_clean"), "r3": Outcome("couldnt", "Navidrome still lists it"),
                             "r4": Outcome("only_clean")})
    watching(FakeSoulSync(album=AlbumRequest(None, [], 11, 7)),
             FakeLibrary(clock, tracks=[[], tracks]))._watch(RUMOURS_WANTED, "kitchen")

    assert said[-1] == ("kitchen", "Rumours by Fleetwood Mac has been downloaded. One of its tracks came down "
                                   "clean, so I swapped it for the explicit one. 2 of its tracks only came down "
                                   "clean, so I've kept them. One of its tracks came down clean, and I couldn't swap "
                                   "it. I couldn't tell if one of its tracks is the clean version. I couldn't check "
                                   "one of its tracks for a clean version.")


@pytest.mark.parametrize("outcomes, message", [
    ({"r1": Outcome("unsure"), "r2": Outcome("unsure")},
     "Rumours by Fleetwood Mac has been downloaded. 2 of its tracks came down again but not plainly explicit, so "
     "they're still the clean versions."),
    ({"r1": Outcome("unsure")},
     "Rumours by Fleetwood Mac has been downloaded. One of its tracks came down again but not plainly explicit, so "
     "it's still the clean version."),
    ({"r1": Outcome("not_judged", "listening took too long"), "r2": Outcome("not_judged", "no lyrics")},
     "Rumours by Fleetwood Mac has been downloaded. 2 of its tracks came down again, but I couldn't check the new copies, so "
     "they're still the clean versions."),
    ({"r1": Outcome("not_judged", "no lyrics")},
     "Rumours by Fleetwood Mac has been downloaded. One of its tracks came down again, but I couldn't check the new copy, so "
     "it's still the clean version."),
])
def test_an_albums_tracks_whose_new_copy_was_set_aside_are_said_to_be_still_clean(watching, clock, said, ears,
                                                                               swapper, outcomes, message):
    # Not the clauses for a track heard unsure on landing: that one is kept, as it came.
    tracks = [DREAMS, GO_YOUR_OWN_WAY][:len(outcomes)]
    ears.kinds.update({"r1": "clean", "r2": "clean"})
    swapper.outcomes.update(outcomes)
    watching(FakeSoulSync(album=AlbumRequest(None, [], 11, len(tracks))),
             FakeLibrary(clock, tracks=[[], tracks]))._watch(RUMOURS_WANTED, "kitchen")

    assert said[-1] == ("kitchen", message)


def test_part_of_an_album_by_its_time_limit_is_heard_too(watching, clock, said, ears, swapper):
    ears.kinds["r1"] = "clean"
    watching(FakeSoulSync(), FakeLibrary(clock, tracks=[[], [DREAMS, GO_YOUR_OWN_WAY]]))._watch(RUMOURS_WANTED,
                                                                                               "kitchen")

    assert said[-1] == ("kitchen", "Only 2 of the 3 tracks of Rumours by Fleetwood Mac came down. One of its tracks "
                                   "came down clean, so I swapped it for the explicit one.")
    assert [swap[0] for swap in swapper.swaps] == ["r1"]


@pytest.mark.parametrize("album", [AlbumRequest("made-up-album", RUMOURS_TITLES, 3, 3),
                                   AlbumRequest(None, [], 3, 3)], ids=["named", "counted"])
def test_a_track_which_came_down_both_clean_and_not_isnt_counted_clean(watching, clock, said, ears, swapper, album):
    # Two copies of The Chain landed, the one heard clean first - the album has the explicit one,
    # so nothing is swapped.
    both = [DREAMS, GO_YOUR_OWN_WAY, _track(5, "The Chain (Clean)"), THE_CHAIN]
    ears.kinds["r5"] = "clean"
    downloads = watching(FakeSoulSync(album=album), FakeLibrary(clock, tracks=[[], both]))

    downloads._watch(RUMOURS_WANTED, "kitchen")

    assert said[-1] == ("kitchen", "Rumours by Fleetwood Mac has been downloaded.")
    assert ("r5", True) in ears.heard and ("r3", True) in ears.heard and swapper.swaps == []


def test_part_of_an_album_by_the_time_limit_says_how_much(watching, clock, said):
    downloads = watching(FakeSoulSync(), FakeLibrary(clock, tracks=[[], [DREAMS], [DREAMS, GO_YOUR_OWN_WAY]]))

    only_two = "Only 2 of the 3 tracks of Rumours by Fleetwood Mac came down."
    assert downloads._watch(RUMOURS_WANTED, "kitchen") == only_two
    assert said[-1] == ("kitchen", only_two)
    assert 1800 <= clock.given_up_at < 1800 + POLL_EVERY

    # And with none of it by then:
    watching(FakeSoulSync(), FakeLibrary(clock))._watch(RUMOURS_WANTED, "kitchen")
    assert said[-1] == ("kitchen", "I couldn't download Rumours by Fleetwood Mac.")


def test_an_album_with_nothing_new_by_then_is_scanned_once_before_its_time_is_up(watching, clock, said):
    # SoulSync never says an album is done, as it gives nothing to follow it by.
    library = FakeLibrary(clock)
    downloads = watching(FakeSoulSync(), library)

    downloads._watch(RUMOURS_WANTED, "kitchen")

    assert library.scans == [1800 - SCAN_AFTER - POLL_EVERY]
    assert said[-1] == ("kitchen", "I couldn't download Rumours by Fleetwood Mac.")


# When it goes wrong

def test_a_failure_is_said_with_soulsyncs_reason(watching, clock, said):
    # find_song raises SoulSyncError("SoulSync isn't answering."), so nothing is found or requested.
    soulsync = FakeSoulSync(fail=SoulSyncError(NOT_ANSWERING))
    assert (watching(soulsync, FakeLibrary(clock))._watch(GOOBA_WANTED, "kitchen")
            == "I couldn't download Gooba by 6ix9ine. SoulSync isn't answering.")
    assert said == [("kitchen", "I couldn't download Gooba by 6ix9ine. SoulSync isn't answering.")]

    # And an album SoulSync can't be asked for.
    watching(FakeSoulSync(fail=SoulSyncError(MCP_OFF)), FakeLibrary(clock))._watch(RUMOURS_WANTED, "bedroom")
    assert said[-1] == ("bedroom", f"I couldn't download Rumours by Fleetwood Mac. {MCP_OFF}")


def test_a_poll_that_fails_is_logged_and_the_watch_carries_on(watching, clock, said, caplog):
    caplog.set_level(logging.INFO)
    # find_songs raises requests.ConnectionError once, then all is well.
    error = requests.ConnectionError("http://navidrome.local:4533/rest/search3?t=made-up-token")
    library = FakeLibrary(clock, songs=[[], error, [], [GOOBA]])

    downloads = watching(FakeSoulSync(), library)
    downloads._watch(GOOBA_WANTED, "kitchen")

    assert said == [GOOBA_REQUESTED, GOOBA_DOWNLOADED]
    assert "Navidrome request failed: ConnectionError" in caplog.text
    # Only the kind of error, never the request, which holds the signed-in token.
    assert "made-up-token" not in caplog.text


def test_a_scan_navidrome_refuses_is_tried_once_and_the_watch_carries_on(watching, clock, said):
    # Refused because Nova's user isn't an admin - which the library logs.
    library = FakeLibrary(clock, songs=[[]] * 40 + [[GOOBA]], scans_allowed=False)
    downloads = watching(FakeSoulSync(), library)

    downloads._watch(GOOBA_WANTED, "kitchen")

    assert library.scans == [600 - SCAN_AFTER - POLL_EVERY]
    assert said == [GOOBA_REQUESTED, GOOBA_DOWNLOADED]


def test_without_navidrome_before_the_request_anything_found_counts(watching, clock, said, caplog):
    caplog.set_level(logging.INFO)
    library = FakeLibrary(clock, songs=[requests.ConnectionError(), [GOOBA]])
    downloads = watching(FakeSoulSync(), library)

    downloads._watch(GOOBA_WANTED, "kitchen")

    assert said == [GOOBA_REQUESTED, GOOBA_DOWNLOADED]
    assert "Couldn't see what Navidrome had of Gooba by 6ix9ine" in caplog.text


def test_something_unforeseen_still_ends_in_an_answer(watching, clock, said, caplog):
    caplog.set_level(logging.INFO)
    downloads = watching(FakeSoulSync(), FakeLibrary(clock, songs=[[], RuntimeError("made-up bug")]))

    assert downloads._watch(GOOBA_WANTED, "kitchen") == "I couldn't download Gooba by 6ix9ine."
    assert said == [GOOBA_REQUESTED, ("kitchen", "I couldn't download Gooba by 6ix9ine.")]
    assert "made-up bug" in caplog.text


def test_something_unforeseen_names_the_song_as_soulsync_found_it_once_it_has(watching, clock, said):
    soulsync = FakeSoulSync(song=ENDLESS_FOUND)
    watching(soulsync, FakeLibrary(clock, songs=[[], RuntimeError("made-up bug")], names=ENDLESS_NAMES))._watch(
        ENDLESS_ASKED, "kitchen")
    assert said == [("kitchen", "endless by Gunna requested."), ("kitchen", "I couldn't download endless by Gunna.")]

    # Before SoulSync has found anything, it's named as it was asked for.
    said.clear()
    watching(FakeSoulSync(fail=RuntimeError("made-up bug")), FakeLibrary(clock, names=()))._watch(ENDLESS_ASKED,
                                                                                                   "kitchen")
    assert said == [("kitchen", "I couldn't download Endless by Gunner.")]


def test_set_up_when_asked_but_not_by_the_watch_says_why_it_couldnt(make_config, clock, said):
    # soulsync.url emptied on the page between the request and its watch.
    downloads = Downloads(make_config(soulsync={"url": ""}),
                          lambda satellite, message: said.append((satellite, message)),
                          soulsync=lambda: None, library=lambda: FakeLibrary(clock), now=clock.now, wait=clock.wait)

    downloads._watch(GOOBA_WANTED, "kitchen")

    assert said == [("kitchen", f"I couldn't download Gooba by 6ix9ine. {NOT_SET_UP}")]


# Several at once, and stopping

def test_several_are_watched_at_once_and_the_same_one_once(running, clock, said):
    soulsync = FakeSoulSync()
    downloads = running(soulsync, FakeLibrary(clock))

    assert downloads.start(GOOBA_WANTED, "kitchen") and downloads.start(RUMOURS_WANTED, "bedroom")
    assert downloads.start(Wanted("gooba", "6IX9INE", "track"), "kitchen") is False
    assert sorted(downloads.waiting()) == ["Gooba by 6ix9ine", "Rumours by Fleetwood Mac"]
    # Each on a thread of its own, which never holds Nova's shutdown up.
    assert len(_watches()) == 2 and all(thread.daemon for thread in _watches())

    downloads.stop()
    assert _all_ended()
    # One request to SoulSync each.
    assert sorted(soulsync.requested) == [("Gooba", "6ix9ine"), ("Rumours", "Fleetwood Mac")]


def test_once_its_landed_it_can_be_asked_for_again(watching, clock, said):
    downloads = watching(FakeSoulSync(), FakeLibrary(clock, songs=[[], [GOOBA]]))

    assert downloads.start(GOOBA_WANTED, "kitchen")
    assert _all_ended()

    assert said == [GOOBA_REQUESTED, GOOBA_DOWNLOADED]
    assert downloads.waiting() == []
    assert downloads.start(GOOBA_WANTED, "kitchen")
    assert _all_ended()


def test_stopping_forgets_whats_waiting_logs_it_and_says_nothing_more(running, clock, said, caplog):
    caplog.set_level(logging.INFO)
    downloads = running(FakeSoulSync(), FakeLibrary(clock))
    downloads.start(GOOBA_WANTED, "kitchen")
    downloads.start(RUMOURS_WANTED, "bedroom")

    downloads.stop()

    assert _all_ended()
    assert ("Forgot 2 downloads still waiting, as Nova is stopping: Gooba by 6ix9ine, Rumours by Fleetwood Mac"
            in caplog.text)
    assert [message for _, message in said if not message.endswith("requested.")] == []
    assert downloads.waiting() == []


def test_stopping_stops_the_swaps_too(running, clock, swapper):
    running(FakeSoulSync(), FakeLibrary(clock)).stop()
    assert swapper.stopped


def _resumes_ended():
    for thread in [thread for thread in threading.enumerate() if thread.name == "swap-resume"]:
        thread.join(5)


def test_saved_swaps_are_carried_on_on_a_thread_of_their_own_one_resume_at_a_time(running, clock, swapper):
    downloads = running(FakeSoulSync(), FakeLibrary(clock))
    swapper.hold.clear()

    downloads.resume_swaps()
    downloads.resume_swaps()
    swapper.hold.set()
    _resumes_ended()
    assert swapper.resumed == ["swap-resume"]

    # Once that one's over, another may run.
    downloads.resume_swaps()
    _resumes_ended()
    assert swapper.resumed == ["swap-resume", "swap-resume"]


def test_saved_swaps_are_carried_on_when_a_download_starts(running, clock, swapper):
    # Not only as Nova starts: a swap left for later - the music folder away, say - goes on.
    downloads = running(FakeSoulSync(), FakeLibrary(clock))

    assert downloads.start(GOOBA_WANTED, "kitchen")
    _resumes_ended()

    assert swapper.resumed == ["swap-resume"]


def test_a_downloads_swap_carried_on_is_waiting_while_it_runs(running, set_up, clock, swapper):
    # A restart forgot the download's watch; its swap, carried on, is still getting the song.
    # SoulSync was asked two and a half minutes ago, by the wall clock a swap is saved by.
    swapper.saved = [{"key": "made-up", "title": "Endless", "artist": "Gunna", "named": "endless by Gunna",
                      "quiet": False, "asked_at": 1_800_000_000.0},
                     {"key": "quiet", "title": "Dreams", "artist": "Fleetwood Mac", "named": "Dreams",
                      "quiet": True}]
    swapper.release.clear()
    clock.time = 1000.0
    downloads = running(FakeSoulSync(), FakeLibrary(clock), set_up, wall=lambda: 1_800_000_150.0)
    downloads.resume_swaps()
    for _ in range(500):
        if downloads.waiting():
            break
        threading.Event().wait(0.01)

    assert downloads.download("Endless by Gunna", "track", "bedroom") == {
        "success": True, "message": "I'm already getting Endless by Gunna."}
    assert downloads.describe_waiting() == "endless by Gunna, asked for 2 minutes ago."

    swapper.release.set()
    _resumes_ended()
    # The library check's swaps aren't downloads, and every one is gone once over.
    assert downloads.waiting() == []


def test_stopping_says_one_download_for_one_and_nothing_with_none(running, clock, caplog):
    caplog.set_level(logging.INFO)
    running(FakeSoulSync(), FakeLibrary(clock)).stop()
    assert "Forgot" not in caplog.text

    downloads = running(FakeSoulSync(), FakeLibrary(clock))
    downloads.start(GOOBA_WANTED, "kitchen")
    downloads.stop()

    assert "Forgot 1 download still waiting, as Nova is stopping: Gooba by 6ix9ine" in caplog.text


def test_an_outcome_reached_as_nova_stops_isnt_said(watching, clock, said):
    library = FakeLibrary(clock)
    downloads = watching(FakeSoulSync(), library)

    def stopped_then_landed():
        downloads.stop()
        return [GOOBA]

    library.songs = [[], stopped_then_landed]

    assert downloads._watch(GOOBA_WANTED, "kitchen") == ""
    assert said == [GOOBA_REQUESTED]


# What's still on its way, for Claude to answer "what's downloading?" from

def test_with_nothing_waiting_nothing_is_downloading(running, clock):
    assert running(FakeSoulSync(), FakeLibrary(clock)).describe_waiting() == "Nothing is downloading."


def test_whats_waiting_is_named_with_how_long_ago_it_was_asked_for(running, clock):
    downloads = running(FakeSoulSync(), FakeLibrary(clock))
    downloads.start(GOOBA_WANTED, "kitchen")
    clock.time = 150.0
    downloads.start(RUMOURS_WANTED, "bedroom")

    clock.time = 170.0
    assert downloads.describe_waiting() == ("Gooba by 6ix9ine, asked for 2 minutes ago. "
                                            "Rumours by Fleetwood Mac, asked for less than a minute ago.")
    clock.time = 215.0
    assert downloads.describe_waiting() == ("Gooba by 6ix9ine, asked for 3 minutes ago. "
                                            "Rumours by Fleetwood Mac, asked for 1 minute ago.")

    # Gone once Nova stops watching, as each is once it lands or is given up on.
    downloads.stop()
    assert _all_ended()
    assert downloads.describe_waiting() == "Nothing is downloading."


def test_a_song_waiting_is_named_as_soulsync_found_it_once_it_has(watching, clock, said):
    # "endless by Gunna requested." is said for "Endless by Gunner", so "what's downloading?"
    # names it the same once SoulSync has found it - and as it was asked for before.
    seen = []
    soulsync = FakeSoulSync(song=ENDLESS_FOUND)
    downloads = watching(soulsync, FakeLibrary(clock, songs=[[], [ENDLESS]], names=ENDLESS_NAMES))
    find_song = soulsync.find_song
    soulsync.find_song = lambda title, artist: seen.append(downloads.describe_waiting()) or find_song(title, artist)
    # Read as the watch waits for its first poll, after the pick.
    clock.later.append((POLL_EVERY, lambda: seen.append(downloads.describe_waiting())))

    assert downloads.start(ENDLESS_ASKED, "kitchen")
    assert _all_ended()

    assert seen == ["Endless by Gunner, asked for less than a minute ago.",
                    "endless by Gunna, asked for less than a minute ago."]
    assert said == [("kitchen", "endless by Gunna requested."), ("kitchen", "endless by Gunna has been downloaded.")]
    assert downloads.describe_waiting() == "Nothing is downloading."


def test_one_which_has_landed_isnt_downloading(watching, clock):
    downloads = watching(FakeSoulSync(), FakeLibrary(clock, songs=[[], [GOOBA]]))

    assert downloads.start(GOOBA_WANTED, "kitchen")
    assert _all_ended()

    assert downloads.describe_waiting() == "Nothing is downloading."


# Set up or not

def test_without_soulsync_or_navidrome_it_isnt_set_up(make_config, monkeypatch):
    assert NOT_SET_UP == ("Downloading music isn't set up. SoulSync's address goes in Settings, under Music, "
                          "and its key under Keys.")
    monkeypatch.setenv("SOULSYNC_API_KEY", "made-up-key")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "made-up-password")
    config = make_config(soulsync={"url": "http://soulsync.local:8008"},
                         navidrome={"url": "http://navidrome.local:4533", "username": "nova"})
    downloads = Downloads(config, lambda satellite, message: None)
    assert downloads.problem() is None

    # Each missing in turn, and read as it is now.
    config.apply({("soulsync", "url"): ""})
    assert downloads.problem() == NOT_SET_UP
    config.apply({("soulsync", "url"): "http://soulsync.local:8008"})
    monkeypatch.delenv("SOULSYNC_API_KEY")
    assert downloads.problem() == NOT_SET_UP
    monkeypatch.setenv("SOULSYNC_API_KEY", "made-up-key")

    config.apply({("navidrome", "url"): ""})
    assert downloads.problem() == "Navidrome's address isn't set. Add it in Settings, under Music."
    config.apply({("navidrome", "url"): "http://navidrome.local:4533", ("navidrome", "username"): ""})
    assert downloads.problem() == "Navidrome's user isn't set. Add it in Settings, under Music."
    config.apply({("navidrome", "username"): "nova"})
    monkeypatch.delenv("NAVIDROME_PASSWORD")
    assert downloads.problem() == "Navidrome's password isn't set. Add it under Keys, then restart Nova."


def test_soulsync_and_navidrome_are_made_from_the_settings_as_they_are_when_used(make_config, monkeypatch):
    monkeypatch.setenv("SOULSYNC_API_KEY", "made-up-key")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "made-up-password")
    config = make_config(soulsync={"url": "http://soulsync.local:8008"},
                         navidrome={"url": "http://navidrome.local:4533", "username": "nova"})
    downloads = Downloads(config, lambda satellite, message: None)

    config.apply({("soulsync", "url"): "http://music.local:8008", ("navidrome", "url"): "http://music.local:4533"})
    soulsync, library = downloads._soulsync(), downloads._library()

    assert isinstance(soulsync, SoulSync) and soulsync.url == "http://music.local:8008"
    # A library of its own, never the player's, whose session is busy playing.
    assert type(library) is NavidromeLibrary
    assert (library.url, library.username) == ("http://music.local:4533", "nova")
    assert downloads._library() is not library

    monkeypatch.delenv("SOULSYNC_API_KEY")
    monkeypatch.delenv("NAVIDROME_PASSWORD")
    assert downloads._soulsync() is None and downloads._library() is None


# Asked for by voice - "download X", and the offer after "play X" finds nothing

GOOBA_REQUEST = Action("request_download", {"query": "Gooba by 6ix9ine", "search_type": "track"})


def test_downloading_with_nothing_set_up_says_so(make_config, clock):
    soulsync = FakeSoulSync()
    downloads = Downloads(make_config(), lambda satellite, message: None, soulsync=lambda: soulsync,
                          library=lambda: FakeLibrary(clock), now=clock.now, wait=clock.wait)

    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen") == {"success": False, "message": NOT_SET_UP}
    assert downloads.request("Gooba by 6ix9ine", "track", "kitchen") == {"success": False, "message": NOT_SET_UP}
    assert soulsync.requested == [] and _watches() == []


def test_a_whole_artist_is_never_downloaded(watching, set_up, clock):
    soulsync = FakeSoulSync()
    downloads = watching(soulsync, FakeLibrary(clock), set_up)

    for ask in (downloads.download, downloads.request):
        assert ask("Fleetwood Mac", "artist", "kitchen") == {
            "success": False, "message": "I only download songs and albums, not a whole artist."}
    assert soulsync.requested == [] and _watches() == []


@pytest.mark.parametrize("query", [None, "", "  "])
def test_nothing_to_download_is_asked_for(watching, set_up, clock, query):
    downloads = watching(FakeSoulSync(), FakeLibrary(clock), set_up)
    assert downloads.download(query, "track", "kitchen") == {
        "success": False, "message": "What would you like me to download?"}


@pytest.mark.parametrize("had", [[GOOBA], [GOOBA_CLEAN, GOOBA]])
def test_something_already_had_is_never_requested(watching, set_up, clock, store, had):
    heard_clean(store, GOOBA_CLEAN)
    soulsync = FakeSoulSync()
    downloads = watching(soulsync, FakeLibrary(clock, songs=[had]), set_up)

    assert downloads.download("Gooba by 6ix9ine", None, "kitchen") == {
        "success": True, "message": "You've already got Gooba by 6ix9ine."}
    assert soulsync.requested == [] and _watches() == []


def test_a_song_with_a_wider_title_isnt_had_so_the_one_asked_for_is_requested(watching, set_up, clock, said, store):
    # Navidrome, really asked, has only "Endless Love" - another song, and no reason to say
    # "Endless" is already got.
    class Session:
        def get(self, url, params=None, timeout=None, stream=False):
            songs = [{"id": "20", "title": "Endless Love", "artist": "Gunna", "album": "Made Up"}]
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"subsonic-response": {
                "status": "ok", "searchResult3": {"song": songs}}})

    library = NavidromeLibrary("http://navidrome.local:4533", "nova", "made-up-password")
    library._session = Session()
    soulsync = FakeSoulSync()
    downloads = watching(soulsync, library, set_up)

    assert downloads.download("Endless by Gunna", "track", "kitchen") == {
        "success": True, "message": f"Asked SoulSync for Endless by Gunna. {SONG_ON_ITS_WAY}", "later": True}
    assert _all_ended()
    assert soulsync.requested == [("Endless", "Gunna")]
    assert said[0] == ("kitchen", "Endless by Gunna requested.")


def test_a_wider_title_soulsync_found_is_what_lands(watching, clock, said, ears):
    # "Endless by Gunna" for which SoulSync, picking Gunna's own song with more words to it,
    # found "Endless Love": it's the one waited for, heard and said downloaded.
    arrived = []
    clock.later.append((2 * POLL_EVERY, lambda: arrived.append(True)))

    class Session:
        def get(self, url, params=None, timeout=None, stream=False):
            songs = [{"id": "l1", "title": "Endless Love", "artist": "Gunna", "album": "Made Up"}] if arrived else []
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"subsonic-response": {
                "status": "ok", "searchResult3": {"song": songs}}})

    library = NavidromeLibrary("http://navidrome.local:4533", "nova", "made-up-password")
    library._session = Session()
    found = SongRequest("Endless Love", "Gunna", {"id": "made-up-track", "name": "Endless Love", "artists": ["Gunna"]})

    result = watching(FakeSoulSync(song=found), library)._watch(Wanted("Endless", "Gunna", "track"), "kitchen")

    assert result == "Endless Love by Gunna has been downloaded."
    assert said == [("kitchen", "Endless Love by Gunna requested."),
                    ("kitchen", "Endless Love by Gunna has been downloaded.")]
    assert ears.heard == [("l1", True)]


def test_a_wider_title_already_there_is_only_had_when_soulsync_found_it(watching, clock, said):
    class Session:
        def get(self, url, params=None, timeout=None, stream=False):
            songs = [{"id": "l1", "title": "Endless Love", "artist": "Gunna", "album": "Made Up"}]
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"subsonic-response": {
                "status": "ok", "searchResult3": {"song": songs}}})

    library = NavidromeLibrary("http://navidrome.local:4533", "nova", "made-up-password")
    library._session = Session()
    love = SongRequest("Endless Love", "Gunna", {"id": "made-up-track", "name": "Endless Love", "artists": ["Gunna"]})

    assert watching(FakeSoulSync(song=love), library)._watch(Wanted("Endless", "Gunna", "track"), "kitchen") == (
        "You've already got Endless Love by Gunna.")
    # SoulSync finding the shorter one, Endless Love is another song, and "endless" is requested.
    soulsync = FakeSoulSync(song=SongRequest("endless", "Gunna", {"id": "made-up-track", "name": "endless"}))
    said.clear()
    watching(soulsync, library)._watch(Wanted("Endless", "Gunna", "track"), "kitchen")
    assert said[0] == ("kitchen", "endless by Gunna requested.") and soulsync.requested == [("endless", "Gunna")]


def test_an_album_had_is_asked_for_and_named_as_navidrome_has_it_when_none_is_queued(watching, set_up, said):
    # "download rumours", which Navidrome has: SoulSync is asked all the same, as Navidrome
    # may have only some of it. Queuing none, it's had - named as Navidrome had it, not as
    # it was asked for, and by the album's artist, not its first track's.
    class Library:
        def find_album_tracks(self, title, artist):
            assert (title, artist) == ("rumours", None)
            return [{**DREAMS, "artist": "Stevie Nicks"}, GO_YOUR_OWN_WAY, THE_CHAIN]

    soulsync = FakeSoulSync(album=AlbumRequest(None, [], 11, 0))
    downloads = watching(soulsync, Library(), set_up)

    assert downloads.download("rumours", "album", "kitchen") == {
        "success": True, "message": f"Asked SoulSync for rumours. {ALBUM_ON_ITS_WAY}", "later": True}
    assert _all_ended()
    assert said == [("kitchen", "You've already got Rumours by Fleetwood Mac.")]
    assert soulsync.requested == [("rumours", None)]


def test_a_part_owned_album_asked_for_is_requested_and_lands(watching, set_up, clock, said):
    # Three of Rumours' eleven in Navidrome, so SoulSync queues the other eight - got, not refused as had.
    tracks = [_track(number, title) for number, title in enumerate(RUMOURS_ALL, 1)]
    had, rest = tracks[:3], tracks[3:]
    soulsync = FakeSoulSync(album=AlbumRequest(None, [], 11, 8))
    # Navidrome: what it has when asked, the same as the watch starts, then the rest as well.
    downloads = watching(soulsync, FakeLibrary(clock, tracks=[had, had, had + rest]), set_up)

    assert downloads.download("Rumours by Fleetwood Mac", "album", "kitchen") == {
        "success": True, "message": f"Asked SoulSync for Rumours by Fleetwood Mac. {ALBUM_ON_ITS_WAY}", "later": True}
    assert _all_ended()
    assert said == [("kitchen", "Rumours by Fleetwood Mac requested."),
                    ("kitchen", "Rumours by Fleetwood Mac has been downloaded.")]
    assert soulsync.requested == [("Rumours", "Fleetwood Mac")]


def test_an_album_had_only_clean_with_none_queued_isnt_said_to_be_got(watching, set_up, clock, said, store):
    # A yes to the explicit one, of which SoulSync queued nothing: all Navidrome has is
    # clean, so "You've already got" it would be wrong. Without a swapper, which would swap
    # the clean tracks instead - see the next test.
    clean = [DREAMS, _track(5, "Gold Dust Woman")]
    heard_clean(store, clean[1])
    downloads = watching(FakeSoulSync(album=AlbumRequest(None, [], 11, 0)), FakeLibrary(clock, tracks=[clean]),
                         set_up, swapping=False)

    assert downloads.request("Rumours by Fleetwood Mac", "album", "kitchen")["later"]
    assert _all_ended()
    assert said == [("kitchen", "I couldn't download Rumours by Fleetwood Mac. SoulSync says it already has "
                                "every track, or is already getting them.")]


@pytest.mark.parametrize("kinds, outcomes, message", [
    # Had, not just downloaded: its tracks "are" clean, rather than "came down" clean.
    ({"r5": "clean", "r6": "clean"}, {"r6": Outcome("only_clean")},
     "Rumours by Fleetwood Mac has been downloaded. One of its tracks is clean, so I swapped it for the "
     "explicit one. One of its tracks only came down clean, so I've kept it."),
    # Heard again first: tracks no longer clean aren't swapped, and with none, it's had.
    ({"r5": "explicit", "r6": "explicit"}, {}, "You've already got Rumours by Fleetwood Mac."),
    # Nothing swapped is never "downloaded": the library is as clean as it was.
    ({"r5": "clean", "r6": "clean"},
     {"r5": Outcome("couldnt", "SoulSync admin profile isn't set up"),
      "r6": Outcome("couldnt", "SoulSync admin profile isn't set up")},
     "I couldn't download Rumours by Fleetwood Mac, so you've still got the clean version: SoulSync admin profile "
     "isn't set up. 2 of its tracks are clean, and I couldn't swap them."),
    ({"r5": "clean", "r6": "clean"}, {"r5": Outcome("only_clean", "3 copies came down clean"),
                                      "r6": Outcome("only_clean", "3 copies came down clean")},
     "I couldn't download Rumours by Fleetwood Mac, so you've still got the clean version: SoulSync only brought "
     "clean copies. 2 of its tracks only came down clean, so I've kept them."),
    ({"r5": "unsure", "r6": "unsure"}, {},
     "You've already got Rumours by Fleetwood Mac. I couldn't tell if 2 of its tracks are clean versions."),
    ({"r5": "clean", "r6": "clean"}, {"r6": Outcome("couldnt", "Navidrome still lists it")},
     "Rumours by Fleetwood Mac has been downloaded. One of its tracks is clean, so I swapped it for the "
     "explicit one. One of its tracks is clean, and I couldn't swap it."),
    ({"r5": "clean", "r6": "clean"}, {},
     "Rumours by Fleetwood Mac has been downloaded. 2 of its tracks are clean, so I swapped them for the "
     "explicit ones."),
])
def test_a_yes_to_an_albums_explicit_tracks_swaps_each_track_heard_clean(watching, set_up, clock, said, store, ears,
                                                                         swapper, kinds, outcomes, message):
    # SoulSync would skip the tracks it has rows for, so each clean one is swapped instead.
    clean = [DREAMS, _track(5, "Gold Dust Woman (Clean)"), _track(6, "Songbird")]
    heard_clean(store, clean[1], clean[2])
    ears.kinds.update(kinds)
    swapper.outcomes.update(outcomes)
    soulsync = FakeSoulSync(album=AlbumRequest(None, [], 11, 0))
    downloads = watching(soulsync, FakeLibrary(clock, tracks=[clean]), set_up)

    assert downloads.request("Rumours by Fleetwood Mac", "album", "kitchen")["later"]
    assert _all_ended()

    assert said == [("kitchen", "Rumours by Fleetwood Mac requested."), ("kitchen", message)]
    assert soulsync.requested == [] and ears.heard == [("r5", True), ("r6", True)]
    # One at a time, as songs, without the clean note in the title.
    expected = [("r5", Wanted("Gold Dust Woman", "Fleetwood Mac", "track"), "kitchen", False),
                ("r6", Wanted("Songbird", "Fleetwood Mac", "track"), "kitchen", False)]
    assert swapper.swaps == (expected if kinds["r5"] == "clean" else [])
    assert "has been downloaded" not in message or "so I swapped" in message


def test_an_album_had_but_not_asked_for_in_words_is_only_said_to_be_got(watching, set_up, clock):
    # "I haven't got X. Do you want me to download it?" would be untrue of an album Navidrome has some of.
    soulsync = FakeSoulSync()
    downloads = watching(soulsync, FakeLibrary(clock, tracks=[[DREAMS, GO_YOUR_OWN_WAY]]), set_up)

    assert downloads.download("Rumours by Fleetwood Mac", "album", "kitchen", asked=False) == {
        "success": True, "message": "You've already got Rumours by Fleetwood Mac."}
    assert soulsync.requested == [] and _watches() == []


def test_only_a_clean_copy_had_offers_the_explicit_one(watching, set_up, clock, store):
    heard_clean(store, GOOBA_CLEAN)
    soulsync = FakeSoulSync()
    downloads = watching(soulsync, FakeLibrary(clock, songs=[[GOOBA_CLEAN]]), set_up)

    reply = downloads.download("Gooba by 6ix9ine", "track", "kitchen")

    assert reply == {"success": True, "message": "You've only got a clean version of Gooba by 6ix9ine. "
                                                "Do you want me to download the explicit one?",
                     "ask_first": GOOBA_REQUEST}
    assert soulsync.requested == [] and _watches() == []


SICKO_MODE = {"id": "s1", "title": "SICKO MODE", "artist": "Travis Scott", "album": "ASTROWORLD (clean)",
              "explicitStatus": "clean"}


@pytest.mark.parametrize("kind, reply", [
    ("explicit", {"success": True, "message": "You've already got SICKO MODE by Travis Scott."}),
    # Never heard: had, whatever its album calls it.
    (None, {"success": True, "message": "You've already got SICKO MODE by Travis Scott."}),
    ("clean", {"success": True, "message": "You've only got a clean version of SICKO MODE by Travis Scott. "
                                           "Do you want me to download the explicit one?",
               "ask_first": Action("request_download", {"query": "SICKO MODE by Travis Scott", "search_type": None})}),
])
def test_whats_had_is_judged_by_what_was_heard_not_by_its_name(watching, set_up, clock, store, kind, reply):
    # SoulSync's tagging put "(clean)" on ASTROWORLD, and Navidrome marks it clean, though it's explicit.
    if kind:
        store.set_verdict("s1", 4096, "2026-11-16T10:00:00Z", Verdict(kind, 14, 9 if kind == "explicit" else 1),
                          "Travis Scott - SICKO MODE")
    soulsync = FakeSoulSync()
    downloads = watching(soulsync, FakeLibrary(clock, songs=[[SICKO_MODE]], names=(("SICKO MODE", "Travis Scott"),)),
                         set_up)

    assert downloads.download("SICKO MODE by Travis Scott", None, "kitchen") == reply
    assert soulsync.requested == [] and _watches() == []


def test_a_copy_heard_clean_whose_file_has_changed_since_is_had(watching, set_up, clock, store):
    # Navidrome gives a new size for it: a new file, not the one heard.
    heard_clean(store, GOOBA_CLEAN)
    changed = {**GOOBA_CLEAN, "size": 9999}
    downloads = watching(FakeSoulSync(), FakeLibrary(clock, songs=[[changed]]), set_up)

    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen")["message"] == (
        "You've already got Gooba (Clean) by 6ix9ine.")


@pytest.mark.parametrize("editions", [
    # One album: a clean edition has only the tracks it bleeped heard clean - the rest have too
    # few swears to judge.
    [DREAMS, _track(5, "Gold Dust Woman")],
    # Two, each with a track heard clean.
    [DREAMS, _track(5, "Gold Dust Woman"),
     _track(6, "Dreams", album="Rumours (Clean)"), _track(7, "Gold Dust Woman", album="Rumours (Clean)")],
])
def test_an_album_with_a_clean_track_in_every_edition_offers_the_explicit_one(watching, set_up, clock, store,
                                                                              editions):
    heard_clean(store, *[track for track in editions if track["title"] == "Gold Dust Woman"])
    library = FakeLibrary(clock, tracks=[editions])
    downloads = watching(FakeSoulSync(), library, set_up)

    reply = downloads.download("Rumours by Fleetwood Mac", "album", "kitchen")

    assert reply["message"] == ("You've only got a clean version of Rumours by Fleetwood Mac. "
                                "Do you want me to download the explicit one?")
    assert reply["ask_first"] == Action("request_download",
                                        {"query": "Rumours by Fleetwood Mac", "search_type": "album"})


@pytest.mark.parametrize("clean_edition", [
    [_track(5, "Dreams", album="Rumours (Clean)"), _track(6, "Gold Dust Woman", album="Rumours (Clean)")],
    # Called the same, told apart by Navidrome's id.
    [_track(5, "Dreams", album_id="another"), _track(6, "Gold Dust Woman", album_id="another")],
])
@pytest.mark.parametrize("clean_first", [True, False])
def test_an_album_had_explicit_beside_a_clean_edition_is_already_got(watching, set_up, clock, said, store,
                                                                     clean_edition, clean_first):
    # Not offered the explicit one, which it has: asked for, and with none queued, it's had.
    heard_clean(store, clean_edition[1])
    explicit = [DREAMS, GO_YOUR_OWN_WAY, THE_CHAIN]
    soulsync = FakeSoulSync(album=AlbumRequest(None, [], 11, 0))
    library = FakeLibrary(clock, tracks=[clean_edition + explicit if clean_first else explicit + clean_edition])
    downloads = watching(soulsync, library, set_up)

    assert downloads.download("Rumours by Fleetwood Mac", "album", "kitchen") == {
        "success": True, "message": f"Asked SoulSync for Rumours by Fleetwood Mac. {ALBUM_ON_ITS_WAY}", "later": True}
    assert _all_ended()
    assert said == [("kitchen", "You've already got Rumours by Fleetwood Mac.")]


def test_a_download_is_started_and_nothing_said_until_soulsync_answers(watching, set_up, clock, said):
    # Navidrome: nothing when asked, nothing when the watch starts, then Gooba.
    soulsync = FakeSoulSync()
    downloads = watching(soulsync, FakeLibrary(clock, songs=[[], [], [GOOBA]]), set_up)

    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen") == {
        "success": True, "message": f"Asked SoulSync for Gooba by 6ix9ine. {SONG_ON_ITS_WAY}", "later": True}

    # The rest is the watch's, said to the satellite that asked.
    assert _all_ended()
    assert said == [GOOBA_REQUESTED, GOOBA_DOWNLOADED]
    assert soulsync.requested == [("Gooba", "6ix9ine")]


def test_a_misheard_song_asked_for_is_named_as_soulsync_found_it_from_then_on(watching, set_up, clock, said):
    # Navidrome: nothing under the name asked for, nothing by the title asked for and
    # SoulSync's artist, then endless.
    library = FakeLibrary(clock, songs=[[], [], [ENDLESS]], names=ENDLESS_NAMES)
    downloads = watching(FakeSoulSync(song=ENDLESS_FOUND), library, set_up)

    # Kept in Claude's history as it was asked for, as SoulSync hasn't been asked yet.
    assert downloads.download("Endless by Gunner", "track", "kitchen") == {
        "success": True, "message": f"Asked SoulSync for Endless by Gunner. {SONG_ON_ITS_WAY}", "later": True}

    assert _all_ended()
    assert said == [("kitchen", "endless by Gunna requested."), ("kitchen", "endless by Gunna has been downloaded.")]
    assert library.looked_for[:2] == list(ENDLESS_NAMES)


def test_a_yes_requests_without_looking_in_navidrome_again(watching, set_up, clock, said, store, ears, swapper):
    # Only the copy heard clean, which the offer was about - and which, heard clean again by
    # the watch, is swapped.
    heard_clean(store, GOOBA_CLEAN)
    ears.kinds["2"] = "clean"
    library = FakeLibrary(clock, songs=[[GOOBA_CLEAN]])
    soulsync = FakeSoulSync()
    downloads = watching(soulsync, library, set_up)

    assert downloads.request("Gooba by 6ix9ine", "track", "kitchen") == {
        "success": True, "message": f"Asked SoulSync for Gooba by 6ix9ine. {SONG_ON_ITS_WAY}", "later": True}

    assert _all_ended()
    assert said == [GOOBA_REQUESTED, GOOBA_DOWNLOADED]
    assert library.looked_for == [("Gooba", "6ix9ine")] and soulsync.requested == []
    assert [swap[0] for swap in swapper.swaps] == ["2"]


def test_the_same_thing_asked_for_while_it_waits_is_requested_once(running, set_up, clock):
    soulsync = FakeSoulSync()
    downloads = running(soulsync, FakeLibrary(clock), set_up)
    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen")["later"]

    # Named as it's asked for this time.
    for ask in (downloads.download, downloads.request, functools.partial(downloads.download, asked=False)):
        assert ask("gooba by 6ix9ine", "track", "bedroom") == {
            "success": True, "message": "I'm already getting gooba by 6ix9ine."}
    # And "play" it says so, rather than that Navidrome hasn't got it.
    assert downloads.offer("gooba by 6ix9ine", "track") == {
        "success": True, "message": "I'm already getting gooba by 6ix9ine."}
    assert downloads.waiting() == ["Gooba by 6ix9ine"]

    downloads.stop()
    assert _all_ended()
    assert soulsync.requested == [("Gooba", "6ix9ine")]


def test_a_download_not_asked_for_in_words_is_offered_instead(watching, set_up, clock, store):
    # Navidrome: nothing, then only a copy heard clean, then the explicit one.
    heard_clean(store, GOOBA_CLEAN)
    soulsync = FakeSoulSync()
    downloads = watching(soulsync, FakeLibrary(clock, songs=[[], [GOOBA_CLEAN], [GOOBA]]), set_up)

    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen", asked=False) == {
        "success": True, "message": "I haven't got Gooba by 6ix9ine. Do you want me to download it?",
        "ask_first": GOOBA_REQUEST}
    # Looked for first, as for "download X".
    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen", asked=False)["message"] == (
        "You've only got a clean version of Gooba by 6ix9ine. Do you want me to download the explicit one?")
    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen", asked=False)["message"] == (
        "You've already got Gooba by 6ix9ine.")
    assert soulsync.requested == [] and _watches() == []


def test_navidrome_out_of_reach_requests_nothing(watching, set_up, clock):
    soulsync = FakeSoulSync()
    library = FakeLibrary(clock, songs=[requests.ConnectionError("made-up refusal")])
    downloads = watching(soulsync, library, set_up)

    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen") == {
        "success": False, "message": "I can't reach Navidrome right now."}
    assert soulsync.requested == [] and _watches() == []


def test_the_offer_is_only_for_a_song_or_album_and_can_be_switched_off(watching, set_up, clock):
    downloads = watching(FakeSoulSync(), FakeLibrary(clock), set_up)

    assert downloads.offer("Gooba by 6ix9ine", "track") == {
        "success": True, "message": "I haven't got Gooba by 6ix9ine. Do you want me to download it?",
        "ask_first": GOOBA_REQUEST}
    assert downloads.offer("Rumours by Fleetwood Mac", "album")["ask_first"] == Action(
        "request_download", {"query": "Rumours by Fleetwood Mac", "search_type": "album"})
    assert downloads.offer("Gooba", None)["message"] == "I haven't got Gooba. Do you want me to download it?"
    assert downloads.offer("Fleetwood Mac", "artist") is None and downloads.offer("jazz", "playlist") is None
    assert downloads.offer(None, None) is None

    # music.offer_downloads off on the page, used straight away.
    set_up.apply({("music", "offer_downloads"): False})
    assert downloads.offer("Gooba by 6ix9ine", "track") is None
    set_up.apply({("music", "offer_downloads"): True})
    assert downloads.offer("Gooba by 6ix9ine", "track") is not None

    # Or SoulSync not set up.
    set_up.apply({("soulsync", "url"): ""})
    assert downloads.offer("Gooba by 6ix9ine", "track") is None
    assert _watches() == []


def test_the_library_is_navidrome_as_set_up_now(set_up, make_config, said):
    # For "what's new", which looks in Navidrome as a download does: a new one each time, from the settings then.
    downloads = Downloads(set_up, lambda satellite, message: None)
    first = downloads.library()
    assert (first.url, first.username) == ("http://navidrome.local:4533", "nova")
    assert downloads.library() is not first
    assert Downloads(make_config(), lambda satellite, message: None).library() is None


# Large downloads need approval

BIG_ALBUM = "Download the album Rumours by Fleetwood Mac (3 albums in 5 minutes)"


@pytest.fixture
def approving(config, clock, said, ears, swapper, store):
    """
    Makes Downloads with a FakeApprovals, as answered, and the settings given if any - whose
    watches wait on the stop event, so a request stays waiting until the test ends.
    """
    made = []

    def make(soulsync, library, answer="wait", settings=None, approvals=...):
        fake = FakeApprovals(answer) if approvals is ... else approvals
        downloads = Downloads(settings or config, lambda satellite, message: said.append((satellite, message)),
                              soulsync=lambda: soulsync, library=lambda: library, now=clock.now,
                              ears=ears, swapper=swapper, store=store, approvals=fake)
        made.append(downloads)
        return downloads, fake

    yield make
    for downloads in made:
        downloads.stop()
    _all_ended()


def _two_albums_before(downloads, count=2):
    """Notes albums started just now, without watching any - which the fake library knows none of."""
    downloads._album_starts.extend([downloads._now()] * count)


def test_two_albums_go_ahead_and_the_third_asks(approving, clock, set_up):
    downloads, fake = approving(FakeSoulSync(), FakeLibrary(clock), settings=set_up)
    _two_albums_before(downloads)
    assert fake.asked == []

    reply = downloads.download("Rumours by Fleetwood Mac", "album", "kitchen")

    assert fake.asked == [(BIG_ALBUM, "kitchen")]
    assert "Rumours by Fleetwood Mac" not in downloads.waiting()
    assert reply["message"].startswith("That needs your OK")


def test_albums_further_apart_than_the_trust_minutes_dont_add_up(approving, clock, set_up):
    downloads, fake = approving(FakeSoulSync(), FakeLibrary(clock), settings=set_up)
    _two_albums_before(downloads)
    clock.time += 301

    reply = downloads.download("Rumours by Fleetwood Mac", "album", "kitchen")

    assert fake.asked == [] and reply["later"]


def test_songs_never_count(approving, clock, set_up):
    downloads, fake = approving(FakeSoulSync(), FakeLibrary(clock), settings=set_up)
    _two_albums_before(downloads)
    # A song is neither counted nor asked for, whatever albums came before.
    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen")["later"]
    assert fake.asked == [] and len(downloads._album_starts) == 2


def test_the_album_count_is_the_setting(approving, clock, make_config, monkeypatch):
    monkeypatch.setenv("SOULSYNC_API_KEY", "made-up-key")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "made-up-password")
    settings = make_config(approvals={"album_count": 5}, soulsync={"url": "http://soulsync.local:8008"},
                           navidrome={"url": "http://navidrome.local:4533", "username": "nova"},
                           music={"song_download_minutes": 10, "album_download_minutes": 30})
    downloads, fake = approving(FakeSoulSync(), FakeLibrary(clock), settings=settings)
    _two_albums_before(downloads, 4)

    downloads.download("Rumours by Fleetwood Mac", "album", "kitchen")

    assert fake.asked == [("Download the album Rumours by Fleetwood Mac (5 albums in 5 minutes)", "kitchen")]


def _with_trust_minutes(make_config, monkeypatch, minutes):
    monkeypatch.setenv("SOULSYNC_API_KEY", "made-up-key")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "made-up-password")
    return make_config(approvals={"trust_minutes": minutes}, soulsync={"url": "http://soulsync.local:8008"},
                       navidrome={"url": "http://navidrome.local:4533", "username": "nova"},
                       music={"song_download_minutes": 10, "album_download_minutes": 30})


def test_a_trust_window_turned_down_never_makes_albums_free(approving, clock, make_config, monkeypatch):
    # No trust window at all still counts albums over the default five minutes, so the third still asks.
    downloads, fake = approving(FakeSoulSync(), FakeLibrary(clock), settings=_with_trust_minutes(
        make_config, monkeypatch, 0))
    _two_albums_before(downloads)
    clock.time += 60

    downloads.download("Rumours by Fleetwood Mac", "album", "kitchen")

    assert fake.asked == [(BIG_ALBUM, "kitchen")]


def test_a_longer_trust_window_counts_albums_over_it(approving, clock, make_config, monkeypatch):
    downloads, fake = approving(FakeSoulSync(), FakeLibrary(clock), settings=_with_trust_minutes(
        make_config, monkeypatch, 20))
    _two_albums_before(downloads)
    clock.time += 15 * 60

    downloads.download("Rumours by Fleetwood Mac", "album", "kitchen")

    assert fake.asked == [("Download the album Rumours by Fleetwood Mac (3 albums in 20 minutes)", "kitchen")]


def test_an_approved_album_starts_and_says_downloading(approving, clock, set_up):
    downloads, fake = approving(FakeSoulSync(), FakeLibrary(clock), settings=set_up)
    _two_albums_before(downloads)
    downloads.download("Rumours by Fleetwood Mac", "album", "kitchen")
    assert "Rumours by Fleetwood Mac" not in downloads.waiting()

    [reply] = fake.approve_waiting()

    assert reply == {"success": True, "message": "Downloading Rumours by Fleetwood Mac.", "later": True}
    assert "Rumours by Fleetwood Mac" in downloads.waiting()


def test_an_approved_album_already_asked_for_meanwhile_says_so(approving, clock, set_up):
    downloads, fake = approving(FakeSoulSync(), FakeLibrary(clock), settings=set_up)
    _two_albums_before(downloads)
    downloads.download("Rumours by Fleetwood Mac", "album", "kitchen")
    assert downloads.start(RUMOURS_WANTED, "lounge")

    [reply] = fake.approve_waiting()

    assert reply == {"success": True, "message": "I'm already getting Rumours by Fleetwood Mac."}


def test_with_no_approvals_a_large_download_is_refused(approving, clock, set_up):
    downloads, _ = approving(FakeSoulSync(), FakeLibrary(clock), settings=set_up, approvals=None)
    _two_albums_before(downloads)

    reply = downloads.download("Rumours by Fleetwood Mac", "album", "kitchen")

    assert reply["success"] is False and reply["message"].startswith("That needs your OK, but there's no way")
    assert "Rumours by Fleetwood Mac" not in downloads.waiting()


def test_already_had_or_waiting_is_answered_without_asking(approving, clock, set_up):
    # Navidrome has a song, and an album which is already on its way: both are answered first.
    downloads, fake = approving(FakeSoulSync(), FakeLibrary(clock, songs=([GOOBA],)), settings=set_up)
    _two_albums_before(downloads)
    assert downloads.start(RUMOURS_WANTED, "kitchen")
    _two = downloads._album_starts[:]

    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen")["message"].startswith("You've already got")
    assert downloads.download("Rumours by Fleetwood Mac", "album", "kitchen")["message"] == (
        "I'm already getting Rumours by Fleetwood Mac.")
    assert fake.asked == [] and downloads._album_starts == _two


def test_albums_really_started_add_up_and_the_third_asks(approving, clock, set_up):
    library = FakeLibrary(clock, albums=(("Rumours", "Fleetwood Mac"), ("Tusk", "Fleetwood Mac"),
                                         ("Mirage", "Fleetwood Mac")))
    downloads, fake = approving(FakeSoulSync(), library, settings=set_up)
    for name in ("Tusk", "Mirage"):
        assert downloads.download(f"{name} by Fleetwood Mac", "album", "kitchen")["later"]
    assert fake.asked == []

    downloads.download("Rumours by Fleetwood Mac", "album", "kitchen")

    assert fake.asked == [(BIG_ALBUM, "kitchen")]


def test_two_albums_asked_at_once_cant_both_slip_in_below_the_limit(approving, clock, set_up):
    library = FakeLibrary(clock, albums=(("Rumours", "Fleetwood Mac"), ("Tusk", "Fleetwood Mac")))
    downloads, fake = approving(FakeSoulSync(), library, settings=set_up)
    _two_albums_before(downloads, 1)
    barrier = threading.Barrier(2)
    replies = []

    def ask(name):
        barrier.wait(5)
        replies.append(downloads.download(f"{name} by Fleetwood Mac", "album", "kitchen"))

    threads = [threading.Thread(target=ask, args=(name,)) for name in ("Rumours", "Tusk")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    # One started unasked; the other, which would have been the third, went to the gate.
    assert len(fake.asked) == 1 and len(downloads._album_starts) == 2
    assert sorted(bool(reply.get("later")) for reply in replies) == [False, True]
