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
from voice_assistant.clean_swap import NO_ADMIN, Outcome, Swapper
from voice_assistant.downloads import Wanted
from voice_assistant.music_folder import HELD, MusicFolder

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

    def __init__(self, albums, files=None, fails=None):
        self.albums = albums
        self.files = files or {}
        self.fails = fails
        self.pages = []

    def _call(self, endpoint, **params):
        assert endpoint == "getAlbumList2" and params["type"] == "alphabeticalByName"
        self.pages.append(params["offset"])
        if self.fails:
            raise self.fails
        names = sorted(self.albums)[params["offset"]:params["offset"] + params["size"]]
        return {"status": "ok", "albumList2": {"album": [{"id": name, "name": name} for name in names]}}

    def _album_songs(self, album_id):
        return self.albums[album_id]

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
        return self.verdicts.get(song["id"], EXPLICIT)


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


def test_a_songs_lyrics_are_left_to_ears_so_a_song_with_too_few_swears_is_passed_over(make_check):
    # Ears reads the lyrics before fetching any sound: the check just moves on.
    check = make_check(ears=FakeEars({"e1": Verdict("too_few", 2, 0)}))
    check.start()
    finished(check)
    assert check.fakes.swapper.swaps == []


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

def test_a_library_which_cant_be_listed_ends_the_check_with_why(make_check, store):
    check = make_check(library=FakeLibrary({}, fails=requests.ConnectionError("http://secret?t=token")))

    check.start()
    state = finished(check)

    assert state["running"] is False and state["last_finished"] is None
    assert state["error"] == "couldn't list the songs in Navidrome (ConnectionError)"
    assert "token" not in str(store.check())


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
