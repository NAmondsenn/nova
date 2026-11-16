"""
Clean Swap Test
Checks how a song is heard against its lyrics (Ears) and how a clean one is swapped (Swapper) -
decision 0019, sections 1 to 4 - with fakes in place of Navidrome, SoulSync, its admin, the
lyrics, ffmpeg and the listening worker, the music folder and the store in the test's own
folder. The model is never loaded and nothing real is asked.
"""

import logging
import os
import threading
from types import SimpleNamespace

import numpy as np
import pytest
import requests

from voice_assistant import clean_swap
from voice_assistant.clean_ear import Verdict
from voice_assistant.clean_store import CleanStore
from voice_assistant.clean_swap import Ears, Outcome, Swapper, listening_library
from voice_assistant.downloads import Wanted, swap_said
from voice_assistant.listener import ListenError
from voice_assistant.music_folder import HELD, MusicFolder
from voice_assistant.navidrome import NavidromeError, NavidromeLibrary
from voice_assistant.soulsync import (ADMIN_REFUSED, KEY_REFUSED, NOT_FORGOTTEN, NOT_FOUND, SongRequest,
                                      SoulSyncError)

# Ten swears in the lyrics, so a song is judged by how many of them are heard.
LYRICS = "\n".join(["fuck this", "shit that", "bitch please", "fuck it", "shit no"] * 2)
# Thirty words a transcript has besides any swears, so it counts as heard.
WORDS = " ".join(["la"] * 30)
EXPLICIT_HEARD = f"{WORDS} fuck shit bitch fuck shit"  # 5 of 10
CLEAN_HEARD = f"{WORDS} fuck"  # 1 of 10
UNSURE_HEARD = f"{WORDS} fuck shit"  # 2 of 10

ENDLESS = {"id": "e1", "title": "endless", "artist": "Gunna", "album": "The Last Wun (explicit)", "duration": 120}
SICKO = {"id": "s1", "title": "SICKO MODE", "artist": "Travis Scott", "album": "ASTROWORLD (clean)", "duration": 312}

# A second of sound at 16 kHz, as decode gives it.
RATE = 16000


class FakeLibrary:
    """Stands in for Navidrome: each song's file, and its audio as bytes - or an error to raise."""

    def __init__(self, files=None, audio_fails=None):
        self.files = files or {}
        self.audio_fails = audio_fails
        self.audio_asked = []
        # Set by a test to run as the song's file is asked for.
        self.on_file = None

    def song_file(self, song_id):
        if self.on_file:
            self.on_file()
        found = self.files.get(song_id, {"path": f"Artist/Album/{song_id}.mp3", "size": 4096,
                                         "updated": "2026-11-16T10:00:00Z"})
        if isinstance(found, Exception):
            raise found
        return found

    def audio(self, song_id):
        self.audio_asked.append(song_id)
        if self.audio_fails:
            raise self.audio_fails
        return b"made-up audio of " + song_id.encode()


class FakeLyrics:
    """Stands in for Lyrics: the same lyrics for every song unless told otherwise, or an error."""

    def __init__(self, by_id):
        self.by_id = by_id

    def for_song(self, song):
        found = self.by_id.get(song["id"], LYRICS)
        if isinstance(found, Exception):
            raise found
        return found, "navidrome"


class FakeListener:
    """Stands in for the listening worker: hears each song as told, in turn, or raises."""

    def __init__(self, heard=(EXPLICIT_HEARD,)):
        self.heard = list(heard)
        self.lock = threading.RLock()
        self.timeouts = []
        # Set by a test to run as a song is heard.
        self.on_hear = None

    def transcribe(self, samples, timeout):
        self.timeouts.append(timeout)
        if self.on_hear:
            self.on_hear()
        answer = self.heard.pop(0) if len(self.heard) > 1 else self.heard[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def decoded(monkeypatch):
    """ffmpeg's part: every song decodes to two minutes of silence, and what was decoded is noted."""
    seen = []

    def decode(audio):
        seen.append(audio)
        return np.zeros(120 * RATE, dtype=np.float32)

    monkeypatch.setattr(clean_swap, "decode", decode)
    return seen


@pytest.fixture
def store(tmp_path):
    return CleanStore(str(tmp_path / "clean_songs.json"))


@pytest.fixture
def make_ears(make_config, store):
    """Ears with the fakes given, or the default ones."""
    def make(library=None, listener=None, lyrics=None, config=None):
        library = library if library is not None else FakeLibrary()
        listener = listener or FakeListener()
        ears = Ears(config or make_config(), lambda: library, listener, lambda _library: FakeLyrics(lyrics or {}),
                    store)
        ears.fakes = (library, listener)
        return ears

    return make


# Hearing a song

def test_a_song_heard_saying_enough_of_its_swears_is_explicit_and_kept(make_ears, store, decoded):
    ears = make_ears()

    assert ears.hear(ENDLESS) == Verdict("explicit", 10, 5)
    assert store.verdict("e1") == {"size": 4096, "updated": "2026-11-16T10:00:00Z", "name": "Gunna - endless",
                                   "verdict": {"kind": "explicit", "expected": 10, "heard": 5, "reason": ""}}
    assert store.known("e1", 4096, "2026-11-16T10:00:00Z")
    assert decoded == [b"made-up audio of e1"]


def test_a_song_is_given_three_times_its_length_and_a_minute_to_be_heard(make_ears):
    ears = make_ears()
    ears.hear(ENDLESS)
    # Two minutes of sound, from what was decoded rather than what Navidrome says.
    assert ears.fakes[1].timeouts == [3 * 120 + 60]


def test_a_song_heard_saying_few_of_its_swears_is_clean(make_ears, store):
    ears = make_ears(listener=FakeListener([CLEAN_HEARD]))
    assert ears.hear(ENDLESS) == Verdict("clean", 10, 1)
    assert store.verdict("e1")["verdict"]["kind"] == "clean"


def test_a_song_in_between_is_unsure_and_listed(make_ears, store):
    ears = make_ears(listener=FakeListener([UNSURE_HEARD]))

    assert ears.hear(ENDLESS) == Verdict("unsure", 10, 2)
    assert store.listed("unsure") == [{"name": "Gunna - endless", "reason": "heard 2 of its 10 swears",
                                       "song_id": "e1"}]


def test_a_song_with_too_few_swears_isnt_listened_to(make_ears, store, decoded):
    ears = make_ears(lyrics={"e1": "what the fuck, oh shit"})

    assert ears.hear(ENDLESS) == Verdict("too_few", 2, 0)
    assert decoded == [] and ears.fakes[0].audio_asked == [] and ears.fakes[1].timeouts == []
    # Judged all the same, so a check passes it over until its file changes.
    assert store.verdict("e1")["verdict"]["kind"] == "too_few"


def test_a_song_without_lyrics_isnt_judged_or_kept(make_ears, store, decoded):
    ears = make_ears(lyrics={"e1": None})

    assert ears.hear(ENDLESS) == Verdict("not_judged", 0, 0, "no lyrics")
    assert decoded == []
    # Not kept, so it's tried again: LRCLIB may have them next time.
    assert store.verdict("e1") is None


def test_a_song_heard_saying_too_little_isnt_taken_for_clean(make_ears, store):
    # Twenty-nine words, none of them swears: the listening went wrong, not the song's swears.
    ears = make_ears(listener=FakeListener([" ".join(["la"] * 29)]))

    assert ears.hear(ENDLESS) == Verdict("not_judged", 10, 0, "heard too little")
    assert store.verdict("e1") is None


@pytest.mark.parametrize("failure, reason", [
    (ListenError("the listening stopped part way through"), "the listening stopped part way through"),
    (ListenError("listening took too long"), "listening took too long"),
    (ListenError("couldn't decode the song"), "couldn't decode the song"),
])
def test_listening_that_fails_part_way_isnt_judged_and_the_next_song_is_heard(make_ears, store, failure, reason):
    ears = make_ears(listener=FakeListener([failure, EXPLICIT_HEARD]))

    assert ears.hear(ENDLESS) == Verdict("not_judged", 10, 0, reason)
    assert store.verdict("e1") is None
    assert ears.hear(SICKO) == Verdict("explicit", 10, 5)


def test_a_decode_that_fails_isnt_judged(make_ears, monkeypatch):
    def decode(audio):
        raise ListenError("couldn't decode the song")

    monkeypatch.setattr(clean_swap, "decode", decode)
    assert make_ears().hear(ENDLESS) == Verdict("not_judged", 10, 0, "couldn't decode the song")


def test_navidrome_out_of_reach_isnt_judged_and_says_so_plainly(make_ears, store, caplog):
    caplog.set_level(logging.INFO)
    error = NavidromeError("Couldn't ask Navidrome for the song's file: ConnectionError")
    ears = make_ears(library=FakeLibrary(files={"e1": error}))
    assert ears.hear(ENDLESS) == Verdict("not_judged", 0, 0,
                                         "Couldn't ask Navidrome for the song's file: ConnectionError")

    # A requests error is named by its kind alone: it can hold a signed address.
    signed = requests.ConnectionError("http://navidrome.local:4533/rest/stream?t=made-up-token")
    ears = make_ears(library=FakeLibrary(audio_fails=signed))
    verdict = ears.hear(ENDLESS)
    assert verdict.kind == "not_judged" and verdict.reason == "couldn't reach Navidrome (ConnectionError)"
    assert "made-up-token" not in caplog.text
    assert store.verdict("e1") is None


def test_lyrics_navidrome_couldnt_give_arent_judged(make_ears):
    ears = make_ears(lyrics={"e1": NavidromeError("Couldn't ask Navidrome for the lyrics: Timeout")})
    assert ears.hear(ENDLESS) == Verdict("not_judged", 0, 0, "Couldn't ask Navidrome for the lyrics: Timeout")


def test_without_navidrome_nothing_is_judged(make_config, store):
    ears = Ears(make_config(), lambda: None, FakeListener(), lambda _library: FakeLyrics({}), store)
    assert ears.hear(ENDLESS) == Verdict("not_judged", 0, 0, "Navidrome isn't set up")


def test_a_verdict_that_cant_be_kept_is_still_given(make_ears, store, monkeypatch, caplog):
    def full(*args, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(store, "set_verdict", full)
    assert make_ears().hear(ENDLESS).kind == "explicit"
    assert "Couldn't keep what was heard of Gunna - endless" in caplog.text


def test_one_song_is_heard_at_a_time_from_its_file_to_its_verdict(make_ears):
    # The worker's lock is held from the moment the song's file is asked for, so a library
    # check can't slip a song in between a download's file and its listening.
    ears = make_ears()
    library, listener = ears.fakes
    free = []

    def try_the_lock():
        def other():
            free.append(listener.lock.acquire(blocking=False))

        thread = threading.Thread(target=other)
        thread.start()
        thread.join()

    library.on_file = try_the_lock
    listener.on_hear = try_the_lock
    ears.hear(ENDLESS)
    assert free == [False, False]


def test_a_downloads_hearing_is_known_to_be_going_on(make_ears):
    ears = make_ears()
    busy = []
    ears.fakes[1].on_hear = lambda: busy.append(ears.busy_with_download())

    ears.hear(ENDLESS, download=True)
    ears.hear(SICKO)

    assert busy == [True, False]
    assert ears.busy_with_download() is False


def test_a_download_waiting_for_another_song_to_be_heard_is_already_known(make_ears):
    # Another song holds the worker; a download's hearing, waiting for it, already counts,
    # so a library check sees it before taking its next song.
    ears = make_ears()
    listener = ears.fakes[1]
    listener.lock.acquire()
    try:
        download = threading.Thread(target=ears.hear, args=(ENDLESS,), kwargs={"download": True})
        download.start()
        seen = threading.Event()
        for _ in range(500):
            if ears.busy_with_download():
                seen.set()
                break
            threading.Event().wait(0.01)
        assert seen.is_set() and download.is_alive()
    finally:
        listener.lock.release()
    download.join(5)
    assert not download.is_alive() and ears.busy_with_download() is False


# Hearing several copies, for a download

def test_of_several_copies_the_first_heard_explicit_decides(make_ears):
    ears = make_ears(listener=FakeListener([CLEAN_HEARD, EXPLICIT_HEARD, CLEAN_HEARD]))
    copies = [ENDLESS, SICKO, {**ENDLESS, "id": "e2"}]

    verdict, copy = ears.hear_copies(copies, download=True)

    assert (verdict.kind, copy) == ("explicit", SICKO)
    assert ears.fakes[1].timeouts == [420, 420]  # the third isn't heard


def test_of_several_copies_all_clean_the_first_is_given(make_ears):
    ears = make_ears(listener=FakeListener([CLEAN_HEARD]))
    verdict, copy = ears.hear_copies([ENDLESS, SICKO], download=True)
    assert (verdict.kind, copy) == ("clean", ENDLESS)


def test_of_several_copies_one_unsure_isnt_taken_for_clean(make_ears):
    ears = make_ears(listener=FakeListener([CLEAN_HEARD, UNSURE_HEARD]))
    verdict, copy = ears.hear_copies([ENDLESS, SICKO], download=True)
    assert (verdict.kind, copy) == ("unsure", SICKO)


# The library heard through

def test_one_library_is_kept_for_listening_until_its_settings_change(make_config, monkeypatch):
    # Navidrome limits sign-ins, so one is kept rather than one for each song.
    monkeypatch.setenv("NAVIDROME_PASSWORD", "made-up-password")
    config = make_config(navidrome={"url": "http://navidrome.local:4533", "username": "nova"})
    library = listening_library(config)

    first = library()
    assert isinstance(first, NavidromeLibrary) and library() is first

    config.apply({("navidrome", "url"): "http://music.local:4533"})
    second = library()
    assert second is not first and second.url == "http://music.local:4533" and library() is second

    monkeypatch.delenv("NAVIDROME_PASSWORD")
    assert library() is None


# Swapping a clean song - decision 0019, section 4. The music folder is a real folder in the
# test's own, and the fake Navidrome lists what's in it, as Navidrome does once its watcher or
# a scan has seen it.

T0 = 1_800_000_000.0
ENDLESS_REL = "Gunna/The Last Wun/endless.mp3"
ENDLESS_TAGS = ("endless", "Gunna", "The Last Wun (explicit)")
ENDLESS_WANTED = Wanted("endless", "Gunna", "track")


class FakeTime:
    """The clock a swap reads - the wall clock, as a swap outlives a restart - moved on only by waiting."""

    def __init__(self, start=T0):
        self.time = start
        self.stopped = False
        # Things to do once the clock reaches a time, as (time, what).
        self.later = []

    def now(self):
        return self.time

    def wait(self, seconds):
        self.time += seconds
        for when, then in [item for item in self.later if item[0] <= self.time]:
            self.later.remove((when, then))
            then()
        return self.stopped


def put(root, rel, data):
    """Writes a file into the music folder, with its folders."""
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as file:
        file.write(data)


def read(root, rel):
    with open(os.path.join(root, rel), "rb") as file:
        return file.read()


class FakeNavidrome:
    """
    Stands in for Navidrome: lists the songs in the music folder, never .nova-held, each with
    an id from its path - so a new copy filed where the old one was has the old one's id, as
    Navidrome's ids do. Stale, it goes on listing what it listed, as one that never scans would.
    """

    def __init__(self, root, clock, tags=None):
        self.root = root
        self.clock = clock
        self.tags = dict(tags or {})
        # Files there which it hasn't picked up yet.
        self.hidden = set()
        self.stale = False
        # Out of reach: every look raises, as with the network down.
        self.down = False
        self.scans = []
        self.listed = self._read()

    def _read(self):
        songs = []
        for folder, folders, names in os.walk(self.root):
            folders[:] = [name for name in folders if name != HELD]
            for name in sorted(names):
                rel = os.path.relpath(os.path.join(folder, name), self.root)
                if name.endswith(".mp3") and rel not in self.hidden:
                    title, artist, album = self.tags.get(rel, ENDLESS_TAGS)
                    songs.append({"id": f"id-{rel}", "title": title, "artist": artist, "album": album})
        return songs

    def find_songs(self, title, artist):
        if self.down:
            raise requests.ConnectionError("made-up refusal")
        if not self.stale:
            self.listed = self._read()
        return [song for song in self.listed if song["title"] == title and artist in (None, song["artist"])]

    def song_file(self, song_id):
        rel = song_id[len("id-"):]
        path = os.path.join(self.root, rel)
        if not os.path.isfile(path):
            raise NavidromeError("Couldn't ask Navidrome for the song's file: HTTPError 404")
        return {"path": rel, "size": os.path.getsize(path), "updated": "2026-11-16T10:00:00Z"}

    def start_scan(self):
        self.scans.append(self.clock.now())
        return True


class FakeSoulSync:
    """
    Stands in for SoulSync: finds whatever's asked for, and on each request files a new copy
    into the music folder the given seconds later - or several, given a list - or never, for None.
    """

    def __init__(self, root, clock, lands=(), find_fails=None, request_fails=None):
        self.root = root
        self.clock = clock
        self.lands = list(lands)
        self.find_fails = find_fails
        self.request_fails = request_fails
        self.found = []
        self.requested = []
        self.unwished = []

    def find_song(self, title, artist, album=None):
        self.found.append((title, artist, album))
        if self.find_fails:
            raise self.find_fails
        return SongRequest(title, artist, {"id": f"deezer-{len(self.found)}", "name": title})

    def request_song(self, request):
        self.requested.append(request.track["id"])
        if self.request_fails:
            raise self.request_fails
        landing = self.lands.pop(0) if self.lands else None
        for after, rel, data in [] if landing is None else landing if isinstance(landing, list) else [landing]:
            self.clock.later.append((self.clock.now() + after,
                                     lambda rel=rel, data=data: put(self.root, rel, data)))

    def unwish(self, track_id):
        self.unwished.append(track_id)
        return True


class FakeAdmin:
    """Stands in for SoulSyncAdmin: forgets each file it's told of, or doesn't, or raises."""

    def __init__(self, forgets=True, on_forget=None):
        self.forgets = forgets
        self.on_forget = on_forget
        self.forgot = []

    def forget(self, soulsync, title, artist, rel_path):
        self.forgot.append((title, artist, rel_path))
        if self.on_forget:
            self.on_forget()
        if isinstance(self.forgets, Exception):
            raise self.forgets
        return self.forgets


class FakeEars:
    """Stands in for Ears: hears each set of new copies as the next kind given."""

    HEARD = {"explicit": 5, "clean": 1, "unsure": 2}

    def __init__(self, kinds=("explicit",)):
        self.kinds = list(kinds)
        self.heard = []

    def hear_copies(self, copies, download=False):
        self.heard.append(([copy["id"] for copy in copies], download))
        kind = self.kinds.pop(0) if len(self.kinds) > 1 else self.kinds[0]
        reason = "listening took too long" if kind == "not_judged" else ""
        return Verdict(kind, 10, self.HEARD.get(kind, 0), reason), copies[0]


@pytest.fixture
def music(tmp_path):
    """The music folder, with Gunna's clean endless and its lyrics in it."""
    root = str(tmp_path / "music")
    put(root, ENDLESS_REL, b"clean endless")
    put(root, "Gunna/The Last Wun/endless.lrc", b"[00:01] lyrics")
    return root


@pytest.fixture
def swap_config(make_config):
    return make_config(clean_check={"swap_tries": 2}, music={"song_download_minutes": 10})


@pytest.fixture
def make_swapper(swap_config, store, music):
    """
    A Swapper over the music folder with the fakes given - each made afresh unless given -
    on a fake clock. What it says is kept in its 'said'.
    """
    def make(clock=None, soulsync=None, admin="default", ears=None, library=None, folder=None, config=None):
        clock = clock or FakeTime()
        library = library or FakeNavidrome(music, clock)
        soulsync = soulsync or FakeSoulSync(music, clock)
        admin = FakeAdmin() if admin == "default" else admin
        ears = ears or FakeEars()
        said = []
        swapper = Swapper(config or swap_config, ears, lambda: MusicFolder(folder or music), lambda: soulsync,
                          lambda: admin, lambda: library, store, lambda satellite, message: said.append(
                              (satellite, message)), now=clock.now, wait=clock.wait)
        swapper.fakes = SimpleNamespace(clock=clock, library=library, soulsync=soulsync, admin=admin, ears=ears)
        swapper.said = said
        return swapper

    return make


def _endless(library):
    return next(song for song in library.find_songs("endless", "Gunna"))


def test_a_clean_song_is_set_aside_forgotten_fetched_again_and_swapped(make_swapper, music, store):
    swapper = make_swapper()
    fakes = swapper.fakes
    # The explicit copy is filed where the clean one was, half a minute after it's asked for.
    fakes.soulsync.lands = [(30, ENDLESS_REL, b"explicit endless")]

    outcome = swapper.swap(_endless(fakes.library), ENDLESS_WANTED, "kitchen", quiet=False)

    assert outcome == "swapped"
    # The clean copy and its lyrics wait in .nova-held; the explicit one is in the library.
    assert read(music, f"{HELD}/{ENDLESS_REL}") == b"clean endless"
    assert read(music, f"{HELD}/Gunna/The Last Wun/endless.lrc") == b"[00:01] lyrics"
    assert read(music, ENDLESS_REL) == b"explicit endless"
    # Scanned as it was set aside; forgotten by its file; asked for on its album, without
    # the album's version note.
    assert fakes.library.scans[0] == T0
    assert fakes.admin.forgot == [("endless", "Gunna", ENDLESS_REL)]
    assert fakes.soulsync.found == [("endless", "Gunna", "The Last Wun")]
    assert fakes.soulsync.requested == ["deezer-1"] and fakes.soulsync.unwished == []
    # The new copy has the old one's id, as it's at the same path - and is heard, as a download's.
    assert fakes.ears.heard == [([f"id-{ENDLESS_REL}"], True)]
    assert store.listed("swapped") == [{"name": "Gunna - endless", "reason": "", "song_id": f"id-{ENDLESS_REL}"}]
    assert store.swaps() == [] and swapper.said == []


def test_every_try_coming_down_clean_keeps_the_last_and_holds_the_rest(make_swapper, music, store):
    swapper = make_swapper(ears=FakeEars(["clean", "clean", "clean"]))
    fakes = swapper.fakes
    fakes.soulsync.lands = [(30, ENDLESS_REL, f"clean try {number}".encode()) for number in (1, 2, 3)]

    outcome = swapper.swap(_endless(fakes.library), ENDLESS_WANTED, None, quiet=True)

    # Two tries more than the first fetch: three in all.
    assert outcome == "only_clean"
    assert fakes.soulsync.requested == ["deezer-1", "deezer-2", "deezer-3"]
    assert len(fakes.admin.forgot) == 3 and len(fakes.ears.heard) == 3
    # The copy each earlier try brought stays held, numbered beside the first; the last is kept.
    assert read(music, f"{HELD}/{ENDLESS_REL}") == b"clean endless"
    assert read(music, f"{HELD}/Gunna/The Last Wun/endless (2).mp3") == b"clean try 1"
    assert read(music, f"{HELD}/Gunna/The Last Wun/endless (3).mp3") == b"clean try 2"
    assert read(music, ENDLESS_REL) == b"clean try 3"
    assert [entry["name"] for entry in store.listed("only_clean")] == ["Gunna - endless"]
    assert store.swaps() == [] and store.listed("swapped") == []


def test_no_tries_more_is_one_fetch(make_swapper, make_config, music):
    swapper = make_swapper(ears=FakeEars(["clean"]),
                           config=make_config(clean_check={"swap_tries": 0}, music={"song_download_minutes": 10}))
    swapper.fakes.soulsync.lands = [(30, ENDLESS_REL, b"clean again")]

    assert swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True) == "only_clean"
    assert swapper.fakes.soulsync.requested == ["deezer-1"]


def test_a_fetch_that_never_lands_is_taken_off_the_wishlist_and_the_song_put_back(make_swapper, music, store):
    swapper = make_swapper()
    fakes = swapper.fakes
    fakes.soulsync.lands = [None]

    outcome = swapper.swap(_endless(fakes.library), ENDLESS_WANTED, "kitchen", quiet=False)

    assert outcome == "couldnt" and outcome.reason == "SoulSync didn't bring another copy in time"
    assert fakes.soulsync.unwished == ["deezer-1"]
    # Back where it was, with its lyrics, and Navidrome asked to scan for it.
    assert read(music, ENDLESS_REL) == b"clean endless"
    assert read(music, "Gunna/The Last Wun/endless.lrc") == b"[00:01] lyrics"
    assert not os.path.exists(os.path.join(music, HELD, ENDLESS_REL))
    assert fakes.library.scans[-1] >= T0 + 600
    assert store.listed("couldnt") == [{"name": "Gunna - endless", "song_id": f"id-{ENDLESS_REL}",
                                        "reason": "SoulSync didn't bring another copy in time"}]
    assert store.swaps() == []


def test_a_new_file_where_the_song_was_is_kept_beside_the_held_copy(make_swapper, music, store):
    # Something filed where it was, which Navidrome hasn't picked up by the time limit.
    swapper = make_swapper()
    fakes = swapper.fakes
    song = _endless(fakes.library)
    fakes.library.hidden.add(ENDLESS_REL)
    fakes.soulsync.lands = [(500, ENDLESS_REL, b"something new")]

    outcome = swapper.swap(song, ENDLESS_WANTED, None, quiet=True)

    assert outcome == "couldnt"
    assert outcome.reason == ("SoulSync didn't bring another copy in time, and a new file is where it was, "
                              f"so both are kept: the new file, and the old copy in {HELD}")
    assert read(music, ENDLESS_REL) == b"something new"
    assert read(music, f"{HELD}/{ENDLESS_REL}") == b"clean endless"
    assert store.listed("couldnt")[0]["reason"] == outcome.reason


def test_without_an_admin_profile_nothing_is_moved(make_swapper, music, store):
    swapper = make_swapper(admin=None)

    outcome = swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == "couldnt" and outcome.reason == "SoulSync admin profile isn't set up"
    assert store.listed("couldnt") == [{"name": "Gunna - endless", "song_id": f"id-{ENDLESS_REL}",
                                        "reason": "SoulSync admin profile isn't set up"}]
    assert read(music, ENDLESS_REL) == b"clean endless" and not os.path.exists(os.path.join(music, HELD))
    assert swapper.fakes.soulsync.found == [] and store.swaps() == []


def test_without_a_music_folder_nothing_is_moved(make_swapper, music, tmp_path, store):
    swapper = make_swapper(folder=str(tmp_path / "not-there"))

    outcome = swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == "couldnt" and outcome.reason == "the music folder doesn't exist"
    assert read(music, ENDLESS_REL) == b"clean endless" and swapper.fakes.admin.forgot == []


def test_a_song_navidrome_still_lists_is_put_back_and_never_forgotten(make_swapper, music, store):
    swapper = make_swapper()
    fakes = swapper.fakes
    fakes.library.stale = True

    outcome = swapper.swap(_endless(fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == "couldnt" and outcome.reason == "Navidrome still lists it"
    assert read(music, ENDLESS_REL) == b"clean endless"
    # Given two minutes after it was asked to scan.
    assert fakes.clock.time == T0 + 120
    assert fakes.admin.forgot == [] and fakes.soulsync.found == []


@pytest.mark.parametrize("forgets, reason", [
    (False, NOT_FORGOTTEN),
    (SoulSyncError(ADMIN_REFUSED), ADMIN_REFUSED),
])
def test_a_song_soulsync_wont_forget_is_put_back_and_not_fetched(make_swapper, music, store, forgets, reason):
    swapper = make_swapper(admin=FakeAdmin(forgets))

    outcome = swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == "couldnt" and outcome.reason == reason
    assert read(music, ENDLESS_REL) == b"clean endless" and swapper.fakes.soulsync.found == []


def test_a_song_soulsync_cant_find_again_is_put_back(make_swapper, music, store):
    swapper = make_swapper()
    fakes = swapper.fakes
    fakes.soulsync.find_fails = SoulSyncError(NOT_FOUND)

    outcome = swapper.swap(_endless(fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == "couldnt" and outcome.reason == NOT_FOUND
    assert read(music, ENDLESS_REL) == b"clean endless"
    # Nothing was put on the wishlist, so there's nothing to take off it.
    assert fakes.soulsync.unwished == []


def test_a_request_soulsync_refuses_is_taken_off_the_wishlist_and_the_song_put_back(make_swapper, music):
    swapper = make_swapper()
    fakes = swapper.fakes
    fakes.soulsync.request_fails = SoulSyncError(KEY_REFUSED)

    outcome = swapper.swap(_endless(fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == "couldnt" and outcome.reason == KEY_REFUSED
    assert fakes.soulsync.unwished == ["deezer-1"] and read(music, ENDLESS_REL) == b"clean endless"


@pytest.mark.parametrize("kind, outcome_kind, listed", [("unsure", "unsure", None),
                                                        ("not_judged", "not_judged", "couldnt")])
def test_a_new_copy_not_heard_clean_or_explicit_is_kept(make_swapper, music, store, kind, outcome_kind, listed):
    swapper = make_swapper(ears=FakeEars([kind]))
    swapper.fakes.soulsync.lands = [(30, ENDLESS_REL, b"new endless")]

    outcome = swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == outcome_kind
    assert read(music, ENDLESS_REL) == b"new endless" and read(music, f"{HELD}/{ENDLESS_REL}") == b"clean endless"
    assert len(swapper.fakes.soulsync.requested) == 1 and store.swaps() == []
    if listed:
        assert store.listed(listed)[0]["reason"] == "couldn't check the copy SoulSync brought: listening took too long"


def test_navidrome_out_of_reach_before_asking_again_puts_the_song_back(make_swapper, music, store):
    # Without what Navidrome listed before the request, an old copy could be taken for the new one.
    swapper = make_swapper()
    fakes = swapper.fakes
    fakes.admin.on_forget = lambda: setattr(fakes.library, "down", True)

    outcome = swapper.swap(_endless(fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == "couldnt" and outcome.reason == "Navidrome couldn't be asked"
    assert read(music, ENDLESS_REL) == b"clean endless"
    assert fakes.soulsync.requested == [] and fakes.soulsync.unwished == [] and store.swaps() == []


def test_every_clean_copy_a_try_brought_is_set_aside(make_swapper, music, store):
    # Two copies land for one request, both clean; the next try brings an explicit one.
    swapper = make_swapper(ears=FakeEars(["clean", "explicit"]))
    fakes = swapper.fakes
    other = "Gunna/The Last Wun/endless 2.mp3"
    fakes.soulsync.lands = [[(30, ENDLESS_REL, b"clean a"), (30, other, b"clean b")],
                            (30, ENDLESS_REL, b"explicit endless")]

    assert swapper.swap(_endless(fakes.library), ENDLESS_WANTED, None, quiet=True) == "swapped"

    assert read(music, ENDLESS_REL) == b"explicit endless"
    assert not os.path.exists(os.path.join(music, other))
    assert read(music, f"{HELD}/{other}") == b"clean b"
    assert read(music, f"{HELD}/Gunna/The Last Wun/endless (2).mp3") == b"clean a"
    # The original, then both of the first try's copies, in the order Navidrome listed them.
    assert sorted(rel for _, _, rel in fakes.admin.forgot) == [other, ENDLESS_REL, ENDLESS_REL]
    assert fakes.soulsync.requested == ["deezer-1", "deezer-2"]


def test_an_albums_version_note_is_left_off_when_soulsync_is_asked(make_swapper, music):
    swapper = make_swapper()
    fakes = swapper.fakes
    fakes.library.tags[ENDLESS_REL] = ("SICKO MODE", "Travis Scott", "ASTROWORLD (clean)")
    fakes.soulsync.lands = [(30, ENDLESS_REL, b"explicit")]
    song = fakes.library.find_songs("SICKO MODE", "Travis Scott")[0]

    swapper.swap(song, Wanted("SICKO MODE", "Travis Scott", "track"), None, quiet=True)

    assert fakes.soulsync.found == [("SICKO MODE", "Travis Scott", "ASTROWORLD")]


# Saved as it goes, and carried on after a restart

def test_a_swap_is_saved_before_anything_moves(make_swapper, music, store, monkeypatch):
    seen = []
    save_swap = store.save_swap

    def saving(swap):
        seen.append((swap["stage"], os.path.exists(os.path.join(music, ENDLESS_REL))))
        save_swap(swap)

    monkeypatch.setattr(store, "save_swap", saving)
    swapper = make_swapper()
    swapper.fakes.soulsync.lands = [(30, ENDLESS_REL, b"explicit")]
    swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert seen[0] == ("setting_aside", True)
    assert seen[1] == ("dropping", False)
    assert [stage for stage, _ in seen] == ["setting_aside", "dropping", "forgetting", "fetching", "watching"]


def test_a_swap_which_cant_be_saved_before_moving_moves_nothing(make_swapper, music, store, monkeypatch):
    def full(swap):
        raise OSError("No space left on device")

    monkeypatch.setattr(store, "save_swap", full)
    swapper = make_swapper()

    outcome = swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == "couldnt" and outcome.reason == "Nova couldn't save the swap's progress"
    assert read(music, ENDLESS_REL) == b"clean endless" and not os.path.exists(os.path.join(music, HELD))


def test_a_swap_which_cant_be_saved_once_moved_carries_on(make_swapper, music, store, monkeypatch, caplog):
    save_swap = store.save_swap
    saves = []

    def first_only(swap):
        saves.append(swap["stage"])
        if len(saves) > 1:
            raise OSError("No space left on device")
        save_swap(swap)

    monkeypatch.setattr(store, "save_swap", first_only)
    swapper = make_swapper()
    swapper.fakes.soulsync.lands = [(30, ENDLESS_REL, b"explicit")]

    assert swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True) == "swapped"
    assert "Couldn't save the swap of Gunna - endless" in caplog.text
    assert store.swaps() == []


def _stopped_while_watching(make_swapper, quiet=False):
    """A swap stopped by Nova stopping a minute after SoulSync was asked, as a restart would."""
    swapper = make_swapper()
    fakes = swapper.fakes
    fakes.soulsync.lands = [None]
    fakes.clock.later.append((T0 + 200, lambda: setattr(fakes.clock, "stopped", True)))
    outcome = swapper.swap(_endless(fakes.library), ENDLESS_WANTED, "kitchen", quiet=quiet)
    return swapper, outcome


def test_a_swap_stopped_part_way_is_kept_to_carry_on(make_swapper, music, store):
    swapper, outcome = _stopped_while_watching(make_swapper)

    assert outcome == "stopped"
    [saved] = store.swaps()
    assert saved["stage"] == "watching" and saved["track_id"] == "deezer-1"
    assert read(music, f"{HELD}/{ENDLESS_REL}") == b"clean endless"
    assert store.listed("couldnt") == [] and swapper.said == []


def test_a_restart_inside_the_limit_watches_on_to_the_end(make_swapper, music, store):
    _stopped_while_watching(make_swapper)
    [saved] = store.swaps()

    # Started again a few minutes later; the copy asked for lands a minute after that.
    clock = FakeTime(saved["asked_at"] + 300)
    swapper = make_swapper(clock=clock)
    clock.later.append((clock.time + 60, lambda: put(music, ENDLESS_REL, b"explicit endless")))
    swapper.resume()

    assert read(music, ENDLESS_REL) == b"explicit endless"
    assert swapper.fakes.soulsync.found == [] and swapper.fakes.soulsync.unwished == []
    assert store.swaps() == [] and store.listed("swapped")[0]["name"] == "Gunna - endless"
    # A download's swap says how it ended, as the download would have.
    assert swapper.said == [("kitchen", "endless by Gunna has been downloaded.")]


def test_a_restart_past_the_limit_puts_the_song_back(make_swapper, music, store):
    _stopped_while_watching(make_swapper)
    [saved] = store.swaps()

    clock = FakeTime(saved["asked_at"] + 700)
    swapper = make_swapper(clock=clock)
    swapper.resume()

    assert swapper.fakes.soulsync.unwished == ["deezer-1"]
    assert read(music, ENDLESS_REL) == b"clean endless"
    assert not os.path.exists(os.path.join(music, HELD, ENDLESS_REL))
    assert swapper.fakes.library.scans
    assert store.swaps() == [] and store.listed("couldnt")[0]["reason"] == "SoulSync didn't bring another copy in time"
    assert swapper.said == [("kitchen", "endless by Gunna has been downloaded, but it's the clean version, and I "
                                        "couldn't swap it: SoulSync didn't bring another copy in time.")]


def test_each_swap_carried_on_is_told_of_as_it_begins_and_ends(make_swapper, music, store):
    _stopped_while_watching(make_swapper)
    [saved] = store.swaps()
    swapper = make_swapper(clock=FakeTime(saved["asked_at"] + 700))
    seen = []

    swapper.resume(begin=lambda swap: seen.append(("begin", swap["key"], store.swaps() != [])),
                   end=lambda swap: seen.append(("end", swap["key"], store.swaps() != [])))

    assert seen == [("begin", saved["key"], True), ("end", saved["key"], False)]


def test_swaps_left_for_later_arent_told_of(make_swapper, store, tmp_path):
    _stopped_while_watching(make_swapper)
    seen = []
    make_swapper(folder=str(tmp_path / "not-mounted")).resume(begin=seen.append, end=seen.append)
    assert seen == []


def test_a_quiet_swap_carried_on_says_nothing(make_swapper, store):
    _stopped_while_watching(make_swapper, quiet=True)
    [saved] = store.swaps()

    swapper = make_swapper(clock=FakeTime(saved["asked_at"] + 700))
    swapper.resume()

    assert swapper.said == [] and store.swaps() == []


def test_a_swap_running_now_isnt_carried_on_a_second_time(make_swapper, music, store):
    # Saved swaps are read as Nova starts, on a thread of their own; a download's swap may
    # already be under way by then.
    swapper = make_swapper()
    fakes = swapper.fakes
    fakes.soulsync.lands = [(60, ENDLESS_REL, b"explicit endless")]
    fakes.clock.later.append((T0 + 45, swapper.resume))

    assert swapper.swap(_endless(fakes.library), ENDLESS_WANTED, "kitchen", quiet=False) == "swapped"
    assert fakes.soulsync.requested == ["deezer-1"] and len(fakes.ears.heard) == 1 and swapper.said == []


def test_a_restart_before_anything_moved_ends_the_swap_quietly(make_swapper, music, store):
    store.save_swap({"key": "made-up", "stage": "setting_aside", "name": "Gunna - endless", "named": "endless by Gunna",
                     "satellite": "kitchen", "quiet": False, "held": [],
                     "copy": {"id": f"id-{ENDLESS_REL}", "title": "endless", "artist": "Gunna", "rel": ENDLESS_REL}})
    swapper = make_swapper()

    swapper.resume()

    assert store.swaps() == [] and store.listed("couldnt") == [] and swapper.said == []
    assert read(music, ENDLESS_REL) == b"clean endless"


def test_a_restart_between_moving_and_saving_where_says_where_to_look(make_swapper, music, store):
    # Moved by hand here, as set_aside would have before the swap could save where.
    put(music, f"{HELD}/{ENDLESS_REL}", read(music, ENDLESS_REL))
    os.remove(os.path.join(music, ENDLESS_REL))
    store.save_swap({"key": "made-up", "stage": "setting_aside", "name": "Gunna - endless", "named": "endless by Gunna",
                     "satellite": None, "quiet": True, "held": [],
                     "copy": {"id": f"id-{ENDLESS_REL}", "title": "endless", "artist": "Gunna", "rel": ENDLESS_REL}})
    swapper = make_swapper()

    swapper.resume()

    assert store.swaps() == []
    assert store.listed("couldnt")[0]["reason"] == f"set aside, but where wasn't saved - look in {HELD}"
    # Never guessed at: nothing is moved.
    assert read(music, f"{HELD}/{ENDLESS_REL}") == b"clean endless"
    assert not os.path.exists(os.path.join(music, ENDLESS_REL))


def test_swaps_are_left_for_later_without_a_music_folder(make_swapper, store, tmp_path):
    _stopped_while_watching(make_swapper)

    swapper = make_swapper(clock=FakeTime(T0 + 5000), folder=str(tmp_path / "not-mounted"))
    swapper.resume()

    assert len(store.swaps()) == 1 and swapper.fakes.soulsync.unwished == []


def test_a_swap_watching_is_carried_on_without_an_admin_profile(make_swapper, music, store):
    # Watching needs no admin: the copy asked for is still waited for, and heard.
    _stopped_while_watching(make_swapper)
    [saved] = store.swaps()

    clock = FakeTime(saved["asked_at"] + 300)
    swapper = make_swapper(clock=clock, admin=None)
    clock.later.append((clock.time + 60, lambda: put(music, ENDLESS_REL, b"explicit endless")))
    swapper.resume()

    assert read(music, ENDLESS_REL) == b"explicit endless" and store.listed("swapped")
    assert store.swaps() == [] and swapper.fakes.soulsync.unwished == []


def test_a_clean_copy_found_without_an_admin_profile_is_left_in_the_library(make_swapper, music, store):
    # It would need forgetting before another try, so it isn't moved at all.
    _stopped_while_watching(make_swapper)
    [saved] = store.swaps()

    clock = FakeTime(saved["asked_at"] + 300)
    swapper = make_swapper(clock=clock, admin=None, ears=FakeEars(["clean"]))
    clock.later.append((clock.time + 60, lambda: put(music, ENDLESS_REL, b"clean try 1")))
    swapper.resume()

    assert read(music, ENDLESS_REL) == b"clean try 1" and read(music, f"{HELD}/{ENDLESS_REL}") == b"clean endless"
    assert not os.path.exists(os.path.join(music, HELD, "Gunna/The Last Wun/endless (2).mp3"))
    assert store.swaps() == [] and store.listed("couldnt")[0]["reason"] == "SoulSync admin profile isn't set up"


def test_swaps_left_for_later_are_logged_once_until_that_changes(make_swapper, store, music, tmp_path, caplog):
    # Resumed as each download starts, so the same reason isn't logged every time.
    caplog.set_level(logging.INFO)
    _stopped_while_watching(make_swapper)
    swapper = make_swapper(clock=FakeTime(T0 + 5000), folder=str(tmp_path / "not-mounted"))

    swapper.resume()
    swapper.resume()

    assert caplog.text.count("1 swap left for later, as the music folder doesn't exist") == 1
    assert "next start" not in caplog.text


def test_a_swap_carried_on_part_way_without_an_admin_profile_is_put_back(make_swapper, music, store):
    # Set aside, but not yet forgotten: that needs the admin.
    put(music, f"{HELD}/{ENDLESS_REL}", read(music, ENDLESS_REL))
    os.remove(os.path.join(music, ENDLESS_REL))
    store.save_swap(_saved(stage="forgetting", held=[{"original": ENDLESS_REL, "held": ENDLESS_REL,
                                                       "sidecars": []}]))
    swapper = make_swapper(admin=None)

    swapper.resume()

    assert read(music, ENDLESS_REL) == b"clean endless" and swapper.fakes.soulsync.unwished == []
    assert store.swaps() == [] and store.listed("couldnt")[0]["reason"] == "SoulSync admin profile isn't set up"


def _saved(**changes):
    """A saved swap of endless, as the Swapper saves one, with the changes given."""
    swap = {"key": "made-up", "stage": "setting_aside", "song_id": f"id-{ENDLESS_REL}", "name": "Gunna - endless",
            "named": "endless by Gunna", "satellite": "kitchen", "quiet": False, "title": "endless",
            "artist": "Gunna", "album": "The Last Wun", "held": [], "tries": 0,
            "copy": {"id": f"id-{ENDLESS_REL}", "title": "endless", "artist": "Gunna", "rel": ENDLESS_REL}}
    swap.update(changes)
    return swap


def _second_try_saved(store, music):
    """
    A swap stopped as it was about to set aside the clean copy its first try brought: the
    original is held, and the first try's copy is where the original was.
    """
    put(music, f"{HELD}/{ENDLESS_REL}", read(music, ENDLESS_REL))
    put(music, ENDLESS_REL, b"clean try 1")
    store.save_swap(_saved(tries=1, track_id="deezer-1", sought_artist="Gunna", before=[], asked_at=T0,
                           limit=600, held=[{"original": ENDLESS_REL, "held": ENDLESS_REL, "sidecars": []}]))


def test_a_restart_before_a_later_try_set_its_copy_aside_carries_on_from_that_try(make_swapper, music, store):
    _second_try_saved(store, music)
    swapper = make_swapper()
    swapper.fakes.soulsync.lands = [(30, ENDLESS_REL, b"explicit endless")]

    swapper.resume()

    assert read(music, ENDLESS_REL) == b"explicit endless"
    assert read(music, f"{HELD}/{ENDLESS_REL}") == b"clean endless"
    assert read(music, f"{HELD}/Gunna/The Last Wun/endless (2).mp3") == b"clean try 1"
    assert swapper.fakes.soulsync.requested == ["deezer-1"] and store.swaps() == []
    assert swapper.said == [("kitchen", "endless by Gunna has been downloaded.")]


def test_a_restart_between_moving_a_later_copy_and_saving_it_puts_the_last_saved_one_back(make_swapper, music,
                                                                                            store):
    _second_try_saved(store, music)
    # Moved, as set_aside would have, before the swap could save where.
    put(music, f"{HELD}/Gunna/The Last Wun/endless (2).mp3", read(music, ENDLESS_REL))
    os.remove(os.path.join(music, ENDLESS_REL))
    swapper = make_swapper()

    swapper.resume()

    # The original goes back where it was, so there's a copy in the library; the unsaved one stays held.
    assert read(music, ENDLESS_REL) == b"clean endless"
    assert read(music, f"{HELD}/Gunna/The Last Wun/endless (2).mp3") == b"clean try 1"
    assert store.swaps() == []
    assert store.listed("couldnt")[0]["reason"] == f"set aside, but where wasn't saved - look in {HELD}"
    assert swapper.said == [("kitchen", "endless by Gunna has been downloaded, but it's the clean version, and I "
                                        f"couldn't swap it: set aside, but where wasn't saved - look in {HELD}.")]


# What's said of a download's swap

@pytest.mark.parametrize("outcome, said", [
    (Outcome("swapped"), "endless by Gunna has been downloaded."),
    (Outcome("only_clean"), "endless by Gunna only came down clean, so I've kept it."),
    (Outcome("unsure"), "endless by Gunna has been downloaded, but I couldn't tell if it's the clean version."),
    (Outcome("not_judged", "listening took too long"),
     "endless by Gunna has been downloaded, but I couldn't check it for a clean version."),
    (Outcome("couldnt", "Navidrome still lists it"),
     "endless by Gunna has been downloaded, but it's the clean version, and I couldn't swap it: Navidrome still "
     "lists it."),
    (Outcome("couldnt", NOT_FOUND),
     "endless by Gunna has been downloaded, but it's the clean version, and I couldn't swap it: SoulSync couldn't "
     "find it."),
])
def test_a_swaps_outcome_is_said_as_the_spec_has_it(outcome, said):
    assert swap_said("endless by Gunna", outcome) == said
