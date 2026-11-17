"""
Clean Check Test
Checks the library check (decision 0019, section 5): which songs it hears, that it swaps the
clean ones quietly, that it saves its progress after each song and carries on after a restart,
that it stops after the song it's on, and that a download's hearing goes first - with fakes in
place of Navidrome, the Ears and the Swapper, and the store and music folder in the test's own
folder. Nothing real is asked and nothing is said.
"""

import os
import threading
from types import SimpleNamespace

import pytest
import requests

from voice_assistant import clean_check
from voice_assistant.clean_check import CleanCheck
from voice_assistant.clean_ear import Verdict
from voice_assistant.clean_store import CleanStore
from voice_assistant.clean_swap import NO_ADMIN, Ears, Outcome, Swapper
from voice_assistant.downloads import Wanted
from voice_assistant.music_folder import HELD, MusicFolder
from voice_assistant.navidrome import NavidromeError, NavidromeLibrary

FILE = {"size": 4096, "updated": "2026-11-16T10:00:00Z"}

KNOWN = {"id": "a1", "title": "Known", "artist": "Artist", "album": "One"}
CHANGED = {"id": "b1", "title": "Changed", "artist": "Artist", "album": "One"}
ONLY_CLEAN = {"id": "c1", "title": "Only Clean", "artist": "Artist", "album": "One"}
SICKO = {"id": "d1", "title": "SICKO MODE (Clean)", "artist": "Travis Scott", "album": "ASTROWORLD (clean)",
         "duration": 312, "path": "Travis Scott/ASTROWORLD/SICKO MODE.mp3"}
ENDLESS = {"id": "e1", "title": "endless", "artist": "Gunna", "album": "The Last Wun", "duration": 120}

EXPLICIT = Verdict("explicit", 10, 5)
CLEAN = Verdict("clean", 10, 1)


class FakeLibrary:
    """Stands in for Navidrome: its albums, a page at a time, each album's songs, and each song's file."""

    def __init__(self, albums, files=None, fails=None, failures=1000):
        self.albums = albums
        self.files = files or {}
        self.fails = fails
        # How many times listing fails before it works.
        self.failures = failures
        self.pages = []
        self.audio_asked = []
        # Set by a test to run as an album's songs are asked for.
        self.on_album = None

    def _call(self, endpoint, **params):
        assert endpoint == "getAlbumList2" and params["type"] == "alphabeticalByName"
        self.pages.append(params["offset"])
        if self.fails and self.failures:
            self.failures -= 1
            raise self.fails
        names = sorted(self.albums)[params["offset"]:params["offset"] + params["size"]]
        return {"status": "ok", "albumList2": {"album": [{"id": name, "name": name} for name in names]}}

    def _album_songs(self, album_id):
        if self.on_album:
            self.on_album()
        return self.albums[album_id]

    def audio(self, song_id):
        self.audio_asked.append(song_id)
        return b"made-up audio"

    def song_file(self, song_id):
        return {"path": f"Artist/One/{song_id}.mp3", **self.files.get(song_id, FILE)}


class FakeEars:
    """Stands in for Ears: hears each song as told (explicit unless told otherwise), noting which."""

    def __init__(self, verdicts=None, busy=()):
        self.verdicts = verdicts or {}
        self.heard = []
        self.busy = list(busy)
        # Set by a test to run as a song is heard.
        self.on_hear = None

    def busy_with_download(self):
        return self.busy.pop(0) if self.busy else False

    def hear(self, song, download=False):
        assert not download
        self.heard.append(song["id"])
        if self.on_hear:
            self.on_hear(song)
        verdict = self.verdicts.get(song["id"], EXPLICIT)
        if isinstance(verdict, list):
            return verdict.pop(0) if len(verdict) > 1 else verdict[0]
        return verdict


class FakeSwapper:
    """Stands in for the Swapper: notes each swap, and says it was swapped."""

    def __init__(self):
        self.swaps = []

    def swap(self, song, wanted, satellite, quiet):
        self.swaps.append((song["id"], wanted, satellite, quiet))
        return Outcome("swapped")


@pytest.fixture
def store(tmp_path):
    return CleanStore(str(tmp_path / "clean_songs.json"))


@pytest.fixture
def music(tmp_path):
    folder = tmp_path / "music"
    folder.mkdir()
    return str(folder)


@pytest.fixture
def make_check(store, music):
    """A CleanCheck with the fakes given, or ones over one album of the five songs."""
    def make(library="five", ears=None, swapper=None, folder=None, waits=None):
        if library == "five":
            library = FakeLibrary({"One": [KNOWN, CHANGED, ONLY_CLEAN, SICKO, ENDLESS]})
        ears = ears or FakeEars()
        swapper = swapper or FakeSwapper()
        waits = [] if waits is None else waits

        def wait(seconds):
            waits.append(seconds)
            return False

        check = CleanCheck(ears, swapper, store, lambda: library, lambda: MusicFolder(folder or music),
                           wait=wait, now=lambda: 1763290000.0)
        check.fakes = SimpleNamespace(library=library, ears=ears, swapper=swapper, waits=waits)
        return check

    return make


def finished(check):
    """Waits for the check's thread to end."""
    thread = check._thread
    if thread is not None:
        thread.join(5)
        assert not thread.is_alive()
    return check.state()


def judged_before(store):
    """Three songs judged by an earlier check: one with the same file, one changed since, one only clean."""
    store.set_verdict("a1", 4096, "2026-11-16T10:00:00Z", EXPLICIT, "Artist - Known")
    store.set_verdict("b1", 2048, "2026-11-01T10:00:00Z", EXPLICIT, "Artist - Changed")
    store.set_verdict("c1", 4096, "2026-11-16T10:00:00Z", CLEAN, "Artist - Only Clean")
    store.add("only_clean", "Artist - Only Clean", "3 copies came down clean", song_id="c1")


# Which songs are heard, and what's done with them

def test_a_check_hears_only_new_or_changed_songs_and_quietly_swaps_a_clean_one(make_check, store):
    judged_before(store)
    check = make_check(ears=FakeEars({"d1": CLEAN}))

    assert check.start() == {"success": True, "message": "Checking the library."}
    state = finished(check)

    assert check.fakes.ears.heard == ["b1", "d1", "e1"]
    # Asked for again by its title without the clean note SoulSync would never find an
    # explicit copy under; quiet, and for no satellite.
    assert check.fakes.swapper.swaps == [("d1", Wanted("SICKO MODE", "Travis Scott", "track"), None, True)]
    assert state["running"] is False and state["done"] == 3 and state["total"] == 3
    assert state["current"] is None and state["last_finished"] == 1763290000.0


def test_a_song_with_too_few_swears_is_passed_over_without_listening(make_check, make_config, store):
    # Ears reads the lyrics before fetching any sound: one swear is nothing to bleep.
    library = FakeLibrary({"Gunna": [ENDLESS]})
    listener = SimpleNamespace(lock=threading.RLock(), transcribe=lambda *args, **kwargs: pytest.fail("listened"))
    ears = Ears(make_config(), lambda: library, listener,
                lambda _library: SimpleNamespace(for_song=lambda song: ("fuck this\nla la", "navidrome")), store)
    check = make_check(library=library, ears=ears)

    check.start()

    assert finished(check)["done"] == 1
    assert library.audio_asked == [] and check.fakes.swapper.swaps == []
    assert store.verdict("e1")["verdict"]["kind"] == "too_few"


@pytest.mark.parametrize("verdict", [Verdict("unsure", 10, 2), Verdict("not_judged", 0, 0, "no lyrics")])
def test_a_song_neither_explicit_nor_clean_is_never_swapped(make_check, verdict):
    check = make_check(ears=FakeEars({"e1": verdict}))
    check.start()
    assert finished(check)["done"] == 5
    assert check.fakes.swapper.swaps == []


def test_every_page_of_albums_is_listed_and_a_song_on_two_is_heard_once(make_check, monkeypatch):
    monkeypatch.setattr(clean_check, "PAGE", 2)
    library = FakeLibrary({"A": [KNOWN], "B": [CHANGED], "C": [ENDLESS, KNOWN]})
    check = make_check(library=library)

    check.start()
    finished(check)

    assert library.pages == [0, 2]
    assert check.fakes.ears.heard == ["a1", "b1", "e1"]


def test_progress_is_saved_after_each_song(make_check, store):
    saved = []
    check = make_check()
    check.fakes.ears.on_hear = lambda song: saved.append((store.check(), check.state()["current"]))

    check.start()
    finished(check)

    second, current = saved[1]
    assert second["running"] is True and second["done"] == 1 and second["total"] == 5
    # The song being heard is still the first to do, so a restart hears it again.
    assert [song["id"] for song in second["queue"]] == ["b1", "c1", "d1", "e1"]
    assert current == "Artist - Changed"
    assert store.check()["queue"] is None and store.check()["running"] is False


def test_the_page_sees_the_lists_and_how_many_songs_are_held(make_check, store, music):
    os.makedirs(os.path.join(music, HELD, "Gunna"))
    for name in ("endless.mp3", "endless.lrc"):
        with open(os.path.join(music, HELD, "Gunna", name), "w") as file:
            file.write("x")
    store.add("swapped", "Gunna - endless", "", song_id="e1")

    state = make_check().state()

    assert state["held"] == 1
    assert state["swapped"] == [{"song_id": "e1", "name": "Gunna - endless", "reason": ""}]
    assert state["only_clean"] == state["unsure"] == state["couldnt"] == []
    assert state["running"] is False and state["last_finished"] is None


def test_no_held_songs_are_counted_without_a_usable_music_folder(make_check, tmp_path):
    assert make_check(folder=str(tmp_path / "nowhere")).state()["held"] == 0


def test_a_song_heard_clean_but_not_swapped_is_swapped_by_the_next_check_without_hearing_it(make_check, store):
    # Its swap couldn't be made - with no music folder set, say - so it's tried again.
    store.set_verdict("d1", 4096, "2026-11-16T10:00:00Z", CLEAN, "Travis Scott - SICKO MODE (Clean)")
    store.add("couldnt", "Travis Scott - SICKO MODE (Clean)", "no music folder is set", song_id="d1")
    check = make_check(library=FakeLibrary({"Astro": [SICKO]}))

    check.start()
    finished(check)

    assert check.fakes.ears.heard == []
    assert check.fakes.swapper.swaps == [("d1", Wanted("SICKO MODE", "Travis Scott", "track"), None, True)]


def test_a_queued_swap_whose_file_has_changed_since_is_heard_rather_than_swapped(make_check, store):
    # Queued to swap, then a new copy landed in its place before the check reached it: the
    # copy there now has never been heard, so it's heard, never set aside unheard.
    store.set_verdict("d1", 4096, "2026-11-16T10:00:00Z", CLEAN, "Travis Scott - SICKO MODE (Clean)")
    store.save_check({"running": True, "done": 0, "total": 1, "current": None, "queue": [{**SICKO, "swap": True}],
                      "last_finished": None})
    library = FakeLibrary({"Astro": [SICKO]}, files={"d1": {"size": 9999, "updated": "2026-11-17T09:00:00Z"}})
    check = make_check(library=library)

    check.resume()

    assert finished(check)["done"] == 1
    assert check.fakes.ears.heard == ["d1"] and check.fakes.swapper.swaps == []


def test_a_queued_swap_which_since_came_down_only_clean_is_left(make_check, store):
    store.set_verdict("d1", 4096, "2026-11-16T10:00:00Z", CLEAN, "Travis Scott - SICKO MODE (Clean)")
    store.save_check({"running": True, "done": 0, "total": 1, "current": None, "queue": [{**SICKO, "swap": True}],
                      "last_finished": None})
    store.add("only_clean", "Travis Scott - SICKO MODE (Clean)", "3 copies came down clean", song_id="d1")
    check = make_check(library=FakeLibrary({"Astro": [SICKO]}))

    check.resume()

    assert finished(check)["done"] == 1
    assert check.fakes.ears.heard == [] and check.fakes.swapper.swaps == []


def test_a_copy_kept_as_only_clean_isnt_swapped_again(make_check, store):
    # SoulSync filed the last copy elsewhere, so it has an id of its own.
    store.set_verdict("d2", 4096, "2026-11-16T10:00:00Z", CLEAN, "Travis Scott - SICKO MODE (Clean)")
    store.add("only_clean", "Travis Scott - SICKO MODE (Clean)", "3 copies came down clean", song_id="d1",
              kept=["d2"])
    check = make_check(library=FakeLibrary({"Astro": [{**SICKO, "id": "d2"}]}))

    check.start()

    assert finished(check)["total"] == 0
    assert check.fakes.ears.heard == [] and check.fakes.swapper.swaps == []


@pytest.mark.parametrize("swap", [{"song_id": "d1"}, {"song_id": "x1", "copy": {"id": "d1"}},
                                  {"song_id": "x1", "copy": {"id": "x2"}, "pending": [{"id": "d1"}]}])
def test_a_song_a_saved_swap_is_handling_is_left_to_it(make_check, store, swap):
    store.save_swap({"key": "k1", "name": "Travis Scott - SICKO MODE", "stage": "watching", **swap})
    check = make_check(library=FakeLibrary({"Astro": [SICKO]}), ears=FakeEars({"d1": CLEAN}))

    check.start()

    assert finished(check)["done"] == 1
    assert check.fakes.ears.heard == [] and check.fakes.swapper.swaps == []


def test_the_page_sees_the_check_listing_then_checking(make_check):
    check = make_check()
    seen = []
    check.fakes.library.on_album = lambda: seen.append((check.state()["phase"], check.state()["current"]))
    check.fakes.ears.on_hear = lambda song: seen.append((check.state()["phase"], check.state()["current"]))

    check.start()
    state = finished(check)

    assert seen[0] == ("listing", None) and seen[1] == ("checking", "Artist - Known")
    assert state["phase"] is None


# Starting and stopping

def test_a_check_wont_start_while_one_is_running(make_check):
    release = threading.Event()
    check = make_check()
    check.fakes.ears.on_hear = lambda song: release.wait(5)

    check.start()
    try:
        assert check.start() == {"success": False, "message": "The library check is already running."}
    finally:
        release.set()
    finished(check)
    assert check.fakes.ears.heard == ["a1", "b1", "c1", "d1", "e1"]


def test_a_check_wont_start_without_navidrome(store, music):
    check = CleanCheck(FakeEars(), FakeSwapper(), store, lambda: None, lambda: MusicFolder(music))
    assert check.start() == {"success": False, "message": "Navidrome isn't set up, so the library can't be checked."}
    assert check._thread is None


def test_stop_ends_the_check_after_the_song_its_on(make_check, store):
    check = make_check()
    stopped = []
    check.fakes.ears.on_hear = lambda song: stopped.append(check.stop()) if not stopped else None

    check.start()
    state = finished(check)

    assert stopped == [{"success": True, "message": "The library check will stop after this song."}]
    assert check.fakes.ears.heard == ["a1"]
    assert state["running"] is False and state["done"] == 1 and state["last_finished"] is None
    # Stopped by the user, so the next start of Nova doesn't carry it on.
    assert store.check()["running"] is False and store.check()["queue"] is None
    assert check.stop() == {"success": False, "message": "The library check isn't running."}


def test_a_check_started_again_after_a_stop_runs_from_the_start(make_check):
    check = make_check()
    check.fakes.ears.on_hear = lambda song: check.stop()
    check.start()
    finished(check)

    check.fakes.ears.on_hear = None
    check.start()
    assert finished(check)["done"] == 5
    assert check.fakes.ears.heard == ["a1", "a1", "b1", "c1", "d1", "e1"]


def test_a_check_waits_while_a_download_is_heard(make_check):
    check = make_check(ears=FakeEars(busy=[True, True]))
    check.start()
    finished(check)
    assert check.fakes.waits == [5, 5]
    assert check.fakes.ears.heard == ["a1", "b1", "c1", "d1", "e1"]


def test_a_stop_while_waiting_for_a_download_ends_the_check_without_hearing_more(make_check):
    check = make_check(ears=FakeEars(busy=[True, True, True]))
    check.fakes.waits = waits = []

    def wait(seconds):
        waits.append(seconds)
        check.stop()
        return True

    check._wait = wait
    check.start()

    assert finished(check)["done"] == 0
    assert check.fakes.ears.heard == [] and waits == [5]


# Carrying on after a restart

def test_a_check_stopped_by_a_restart_carries_on_from_its_saved_queue(make_check, store):
    store.save_check({"running": True, "done": 3, "total": 5, "current": "Travis Scott - SICKO MODE (Clean)",
                      "queue": [SICKO, ENDLESS], "last_finished": 1763000000.0})
    check = make_check(ears=FakeEars({"d1": CLEAN}))

    check.resume()
    state = finished(check)

    assert check.fakes.ears.heard == ["d1", "e1"]
    assert check.fakes.library.pages == []
    assert [swap[0] for swap in check.fakes.swapper.swaps] == ["d1"]
    assert state["done"] == 5 and state["total"] == 5 and state["last_finished"] == 1763290000.0


def test_a_check_cut_short_while_listing_lists_again(make_check, store):
    store.save_check({"running": True, "done": 0, "total": 0, "current": None, "queue": None, "last_finished": None})
    check = make_check()
    check.resume()
    assert finished(check)["done"] == 5


@pytest.mark.parametrize("saved", [{}, {"running": False, "done": 5, "total": 5, "queue": None,
                                        "last_finished": 1763000000.0}])
def test_a_check_not_running_when_nova_stopped_isnt_carried_on(make_check, store, saved):
    if saved:
        store.save_check(saved)
    check = make_check()
    check.resume()
    assert check._thread is None and check.fakes.ears.heard == []


def test_as_nova_stops_the_check_is_kept_to_carry_on_and_the_song_cut_short_is_heard_again(make_check, store):
    # The worker is closed under the song being heard, which comes back not judged.
    ears = FakeEars({"a1": Verdict("not_judged", 0, 0, "the listening worker stopped")})
    check = make_check(ears=ears)
    ears.on_hear = lambda song: check.close()

    check.start()
    finished(check)

    saved = store.check()
    assert saved["running"] is True and saved["done"] == 0
    assert [song["id"] for song in saved["queue"]] == ["a1", "b1", "c1", "d1", "e1"]
    assert ears.heard == ["a1"]


def test_a_clean_song_heard_as_nova_stops_is_heard_again_rather_than_swapped(make_check, store):
    ears = FakeEars({"a1": CLEAN})
    check = make_check(ears=ears)
    ears.on_hear = lambda song: check.close()

    check.start()
    finished(check)

    assert check.fakes.swapper.swaps == []
    assert store.check()["queue"][0]["id"] == "a1"


def test_a_swap_stopped_as_nova_stops_leaves_its_song_to_be_heard_again(make_check, store):
    # Its saved swap is carried on, or ended if nothing had moved yet; heard again, the song
    # is either swapped afresh or, set aside already, no longer there to hear.
    swapper = FakeSwapper()
    check = make_check(ears=FakeEars({"a1": CLEAN}), swapper=swapper)

    def stopping(song, wanted, satellite, quiet):
        check.close()
        return Outcome("stopped")

    swapper.swap = stopping
    check.start()
    finished(check)

    assert store.check()["running"] is True and store.check()["queue"][0]["id"] == "a1"


# Failures

UNREACHABLE = Verdict("not_judged", 0, 0, "couldn't reach Navidrome (ConnectionError)")
PAUSED = "Navidrome couldn't be reached, so the check stopped here and carries on next time."


def test_a_library_out_of_reach_is_listed_again_each_minute_then_carried_on_next_time(make_check, store):
    library = FakeLibrary({"One": [ENDLESS]}, fails=requests.ConnectionError("http://secret?t=token"))
    check = make_check(library=library)

    check.start()
    state = finished(check)

    # Tried, and ten times more a minute apart.
    assert len(library.pages) == 11 and check.fakes.waits == [60] * 10
    assert state["running"] is False and state["last_finished"] is None and state["error"] == PAUSED
    assert state["paused"] is True and state["phase"] is None
    assert "token" not in str(store.check())

    # Carried on as Nova starts again, once Navidrome is there.
    library.failures = 0
    again = make_check(library=library)
    again.resume()
    assert finished(again)["done"] == 1 and again.fakes.ears.heard == ["e1"]


class FileOutOfReach(FakeLibrary):
    """Navidrome which can list its albums but, for a while, not give a song's file: as it starts after pve2 does."""

    def __init__(self, albums, failures):
        super().__init__(albums)
        self.file_failures = failures

    def song_file(self, song_id):
        if self.file_failures:
            self.file_failures -= 1
            raise NavidromeLibrary._cant_ask("the song's file", requests.ConnectionError("http://secret?t=token"))
        return super().song_file(song_id)


def test_a_songs_file_out_of_reach_while_listing_lists_again(make_check, store):
    # Taken for a song to hear, it would be heard as not judged; the listing waits instead.
    store.set_verdict("e1", 4096, "2026-11-16T10:00:00Z", EXPLICIT, "Gunna - endless")
    library = FileOutOfReach({"One": [ENDLESS]}, failures=1)
    check = make_check(library=library)

    check.start()
    state = finished(check)

    assert library.pages == [0, 0] and check.fakes.waits == [60]
    assert state["total"] == 0 and check.fakes.ears.heard == []


def test_a_queued_swaps_file_out_of_reach_is_tried_again(make_check, store):
    store.set_verdict("d1", 4096, "2026-11-16T10:00:00Z", CLEAN, "Travis Scott - SICKO MODE (Clean)")
    store.save_check({"running": True, "done": 0, "total": 1, "current": None, "queue": [{**SICKO, "swap": True}],
                      "last_finished": None})
    check = make_check(library=FileOutOfReach({"Astro": [SICKO]}, failures=1))

    check.resume()

    assert finished(check)["done"] == 1 and check.fakes.waits == [60]
    assert [swap[0] for swap in check.fakes.swapper.swaps] == ["d1"]


@pytest.mark.parametrize("what", ["the song's file", "the lyrics", "the song's audio"])
@pytest.mark.parametrize("error", ["ConnectionError", "ReadTimeout", "HTTPError 502"])
def test_navidrome_out_of_reach_is_known_by_the_librarys_own_words(what, error):
    if error == "HTTPError 502":
        response = requests.Response()
        response.status_code = 502
        failure = requests.HTTPError(response=response)
    else:
        failure = getattr(requests, error)("http://secret?t=token")

    assert clean_check.OUT_OF_REACH.match(str(NavidromeLibrary._cant_ask(what, failure)))


def test_a_library_which_answers_with_an_error_isnt_listed_again_till_next_time(make_check, store):
    check = make_check(library=FakeLibrary({}, fails=NavidromeError("Wrong username or password", 40)))
    check.start()
    state = finished(check)

    assert check.fakes.waits == [] and state["paused"] is True
    assert state["error"] == ("Navidrome couldn't list the songs (Wrong username or password), "
                              "so the check stopped here and carries on next time.")


def test_a_song_heard_while_navidrome_is_out_of_reach_is_heard_again_each_minute(make_check):
    check = make_check(ears=FakeEars({"a1": [UNREACHABLE, UNREACHABLE, EXPLICIT]}))

    check.start()
    state = finished(check)

    assert check.fakes.ears.heard == ["a1", "a1", "a1", "b1", "c1", "d1", "e1"]
    assert check.fakes.waits == [60, 60]
    assert state["done"] == 5 and state["last_finished"] == 1763290000.0 and state["error"] is None


def test_a_check_with_navidrome_gone_pauses_with_its_queue_and_carries_on_next_time(make_check, store):
    # As after pve2 restarts, with the brain up before Navidrome: no song is used up, and the
    # check isn't called finished.
    ears = FakeEars({song: UNREACHABLE for song in ("a1", "b1", "c1", "d1", "e1")})
    check = make_check(ears=ears)
    library = check.fakes.library

    check.start()
    state = finished(check)

    assert ears.heard == ["a1"] * 11 and check.fakes.waits == [60] * 10
    assert state["running"] is False and state["paused"] is True and state["error"] == PAUSED
    assert state["last_finished"] is None and state["done"] == 0 and state["total"] == 5
    assert [song["id"] for song in store.check()["queue"]] == ["a1", "b1", "c1", "d1", "e1"]

    # Carried on from its queue as Nova starts again, not listed afresh.
    again = make_check(library=library)
    again.resume()
    state = finished(again)
    assert again.fakes.ears.heard == ["a1", "b1", "c1", "d1", "e1"] and library.pages == [0]
    assert state["done"] == 5 and state["paused"] is False and state["error"] is None


def test_start_carries_a_paused_check_on_from_its_queue(make_check, store):
    store.save_check({"running": False, "paused": True, "done": 3, "total": 5, "current": None,
                      "queue": [SICKO, ENDLESS], "last_finished": None, "error": PAUSED})
    check = make_check()

    assert check.start()["success"] is True
    state = finished(check)

    assert check.fakes.ears.heard == ["d1", "e1"] and check.fakes.library.pages == []
    assert state["done"] == 5 and state["total"] == 5


def test_stop_drops_a_paused_check_so_the_next_starts_afresh(make_check, store):
    # The page's way to give up on a check paused for Navidrome, rather than carry it on.
    store.save_check({"running": False, "paused": True, "done": 3, "total": 5, "current": None,
                      "queue": [SICKO, ENDLESS], "last_finished": 1763000000.0, "error": PAUSED})
    check = make_check()

    assert check.stop() == {"success": True, "message": "The paused check is dropped."}

    state = check.state()
    assert state["paused"] is False and state["error"] is None and state["last_finished"] == 1763000000.0
    assert store.check()["paused"] is False and store.check()["queue"] is None
    # Nothing is left for Nova's next start to carry on, and Start checks the whole library.
    check.resume()
    assert check._thread is None
    assert check.stop() == {"success": False, "message": "The library check isn't running."}
    check.start()
    finished(check)
    assert check.fakes.ears.heard == ["a1", "b1", "c1", "d1", "e1"]


def test_a_stop_while_waiting_for_navidrome_ends_the_check(make_check, store):
    check = make_check(ears=FakeEars({"a1": UNREACHABLE}))

    def wait(seconds):
        check.stop()
        return True

    check._wait = wait
    check.start()
    state = finished(check)

    assert check.fakes.ears.heard == ["a1"]
    assert state["paused"] is False and store.check()["queue"] is None


def test_a_stop_during_the_last_try_for_a_song_ends_the_check_rather_than_pausing_it(make_check, store):
    ears = FakeEars({"a1": UNREACHABLE})
    check = make_check(ears=ears)
    ears.on_hear = lambda song: check.stop() if len(ears.heard) == 11 else None

    check.start()
    state = finished(check)

    assert ears.heard == ["a1"] * 11
    assert state["paused"] is False and state["error"] is None and store.check()["queue"] is None


def test_a_stop_during_the_last_try_at_listing_ends_the_check_rather_than_pausing_it(make_check, store):
    library = FakeLibrary({"One": [ENDLESS]}, fails=requests.ConnectionError("made-up refusal"))
    real_call = library._call

    def call(endpoint, **params):
        if len(library.pages) == 10:
            check.stop()
        return real_call(endpoint, **params)

    library._call = call
    check = make_check(library=library)

    check.start()
    state = finished(check)

    assert len(library.pages) == 11
    assert state["paused"] is False and state["error"] is None and store.check()["queue"] is None


def test_a_surprise_part_way_ends_the_check_with_a_sentence(make_check):
    check = make_check()

    def odd():
        raise RuntimeError("odd")

    check.fakes.library.on_album = odd
    check.start()

    assert finished(check)["error"] == "Something went wrong part way through the check, so it stopped."


def test_one_songs_surprise_doesnt_end_the_check(make_check):
    ears = FakeEars()

    def odd(song):
        if song["id"] == "b1":
            raise RuntimeError("odd")

    ears.on_hear = odd
    check = make_check(ears=ears)
    check.start()

    assert finished(check)["done"] == 5


# Without the music folder or SoulSync's admin profile

@pytest.mark.parametrize("folder_set, reason", [(False, "no music folder is set"), (True, NO_ADMIN)])
def test_without_the_folder_or_admin_a_clean_song_is_listed_unmoved_and_nothing_is_said(
        make_check, make_config, store, music, folder_set, reason):
    said = []
    swapper = Swapper(make_config(), FakeEars(), lambda: MusicFolder(music if folder_set else ""),
                      lambda: SimpleNamespace(), lambda: None, lambda: FakeLibrary({}), store,
                      lambda satellite, message: said.append(message))
    check = make_check(library=FakeLibrary({"Astro": [SICKO]}), ears=FakeEars({"d1": CLEAN}), swapper=swapper)

    check.start()
    state = finished(check)

    assert state["couldnt"] == [{"song_id": "d1", "name": "Travis Scott - SICKO MODE (Clean)", "reason": reason}]
    assert state["swapped"] == [] and store.swaps() == []
    assert said == [] and not os.path.exists(os.path.join(music, HELD))
