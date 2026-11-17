"""
Clean Swap Test
Checks how a song is heard against its lyrics (Ears) and how a clean one is swapped (Swapper) -
decision 0022, sections 1 to 4 - with fakes in place of Navidrome, SoulSync, its admin, the
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

from voice_assistant import clean_store, clean_swap
from voice_assistant.clean_ear import Verdict
from voice_assistant.clean_store import CleanStore
from voice_assistant.clean_swap import Ears, Outcome, Swapper, listening_library
from voice_assistant.downloads import Wanted, swap_said
from voice_assistant.listener import ListenError
from voice_assistant.music_folder import HELD, MusicFolder, MusicFolderError
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


# The time every list entry is stamped with, so a list can be compared whole.
AT = 1763000000


@pytest.fixture
def store(tmp_path):
    return CleanStore(str(tmp_path / "clean_songs.json"), clock=lambda: AT)


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
                                       "song_id": "e1", "at": AT}]


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


# Swapping a clean song - decision 0022, section 4. The music folder is a real folder in the
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

    def find_songs(self, title, artist, also=None):
        if self.down:
            raise requests.ConnectionError("made-up refusal")
        if not self.stale:
            self.listed = self._read()
        # As the real library's: the title asked for, or the one SoulSync found for it.
        return [song for song in self.listed
                if song["title"] in (title, also) and artist in (None, song["artist"])]

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

    def __init__(self, root, clock, lands=(), find_fails=None, request_fails=None, finds=None):
        self.root = root
        # The title SoulSync finds whatever's asked for, if not the one asked for.
        self.finds = finds
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
        title = self.finds or title
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
    assert store.listed("swapped") == [{"name": "Gunna - endless", "reason": "", "song_id": f"id-{ENDLESS_REL}",
                                         "at": AT}]
    assert store.swaps() == [] and swapper.said == []


def test_a_wider_title_soulsync_finds_is_the_copy_watched_for(make_swapper, music, store):
    # Asked for as "Endless", SoulSync finds Gunna's "Endless Love", which is the song swapped:
    # its new copy is the one watched for, though its title has a word more than was asked for.
    love_rel = "Gunna/The Last Wun/endless love.mp3"
    put(music, love_rel, b"clean endless love")
    swapper = make_swapper()
    fakes = swapper.fakes
    fakes.library.tags[love_rel] = ("Endless Love", "Gunna", "The Last Wun (explicit)")
    fakes.library.tags[ENDLESS_REL] = ("Something Else", "Someone Else", "Another Album")
    fakes.soulsync.finds = "Endless Love"
    fakes.soulsync.lands = [(30, love_rel, b"explicit endless love")]
    song = next(song for song in fakes.library.find_songs("Endless Love", "Gunna"))

    outcome = swapper.swap(song, Wanted("Endless", "Gunna", "track"), None, quiet=True)

    assert outcome == "swapped"
    assert read(music, love_rel) == b"explicit endless love"
    assert fakes.ears.heard == [([f"id-{love_rel}"], False)]


FOURTH_HELD = f"{HELD}/Gunna/The Last Wun/endless (4).mp3"


def _every_try_clean(make_swapper):
    """A swapper whose three tries each bring a clean copy where the original was."""
    swapper = make_swapper(ears=FakeEars(["clean", "clean", "clean"]))
    swapper.fakes.soulsync.lands = [(30, ENDLESS_REL, f"clean try {number}".encode()) for number in (1, 2, 3)]
    return swapper


def test_every_try_coming_down_clean_puts_the_original_back_and_holds_every_copy(make_swapper, music, store):
    swapper = _every_try_clean(make_swapper)
    fakes = swapper.fakes

    outcome = swapper.swap(_endless(fakes.library), ENDLESS_WANTED, None, quiet=True)

    # Two tries more than the first fetch: three in all.
    assert outcome == "only_clean" and outcome.reason == "3 copies came down clean"
    assert fakes.soulsync.requested == ["deezer-1", "deezer-2", "deezer-3"]
    assert len(fakes.admin.forgot) == 3 and len(fakes.ears.heard) == 3
    # His own copy, and its lyrics, are back where they were (seen live on 17 Nov: his FLAC was left
    # held and the last try's MP3, as clean, kept in its place). Every copy the tries brought is held.
    assert read(music, ENDLESS_REL) == b"clean endless"
    assert read(music, "Gunna/The Last Wun/endless.lrc") == b"[00:01] lyrics"
    assert not os.path.exists(os.path.join(music, HELD, ENDLESS_REL))
    assert read(music, SECOND_HELD) == b"clean try 1"
    assert read(music, THIRD_HELD) == b"clean try 2"
    assert read(music, FOURTH_HELD) == b"clean try 3"
    assert [entry["name"] for entry in store.listed("only_clean")] == ["Gunna - endless"]
    # The last try's copy is listed by its id, for the library check.
    assert store.listed("only_clean")[0]["kept"] == [f"id-{ENDLESS_REL}"]
    assert store.swaps() == [] and store.listed("swapped") == []


def _copies(music):
    """Every song file in the music folder, .nova-held's too, by what's in it."""
    found = []
    for folder, _folders, names in os.walk(music):
        found += [read(folder, name) for name in names if name.endswith(".mp3")]
    return sorted(found)


def test_no_copy_is_lost_at_any_step_of_an_only_clean_ending(make_swapper, music, store, monkeypatch):
    swapper = _every_try_clean(make_swapper)
    seen = []
    real = store.save_swap

    def save_swap(swap):
        # Whatever has been in the music folder is still somewhere in it as each step is saved.
        now = _copies(music)
        assert set(seen) <= set(now)
        seen.extend(copy for copy in now if copy not in seen)
        real(swap)

    monkeypatch.setattr(store, "save_swap", save_swap)

    assert swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True) == "only_clean"

    assert _copies(music) == [b"clean endless", b"clean try 1", b"clean try 2", b"clean try 3"]
    assert read(music, ENDLESS_REL) == b"clean endless"


@pytest.mark.parametrize("stage", ["setting_aside", "putting_back"])
def test_a_restart_part_way_through_an_only_clean_ending_puts_the_original_back(make_swapper, music, store,
                                                                               monkeypatch, stage):
    # Nova stops just after the last try's copy is saved as to be set aside, or once it is.
    swapper = _every_try_clean(make_swapper)
    real = store.save_swap

    def save_swap(swap):
        real(swap)
        if swap.get("rejected") and swap["stage"] == stage:
            swapper.stop()

    monkeypatch.setattr(store, "save_swap", save_swap)

    assert swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True) == "stopped"
    [saved] = store.swaps()
    assert saved["rejected"]["kind"] == "only_clean"

    again = make_swapper(admin=None)
    again.resume()

    assert read(music, ENDLESS_REL) == b"clean endless" and read(music, FOURTH_HELD) == b"clean try 3"
    assert store.swaps() == [] and again.fakes.soulsync.requested == []
    assert store.listed("only_clean")[0]["kept"] == [f"id-{ENDLESS_REL}"]


class GoneWithTheThirdTry(FakeEars):
    """Hears as FakeEars does, but the original is emptied from .nova-held by hand as the third try is heard."""

    def __init__(self, music, kinds):
        super().__init__(kinds)
        self.music = music

    def hear_copies(self, copies, download=False):
        if len(self.heard) == 2:
            os.remove(os.path.join(self.music, HELD, ENDLESS_REL))
        return super().hear_copies(copies, download)


def test_an_original_gone_from_the_holding_folder_has_the_last_copy_set_aside_put_back_instead(
        make_swapper, music, store):
    # Setting the last try's copy aside to put the original back must never leave the library without
    # the song. The last try files its copy under a name of its own, so it isn't held under the
    # original's freed name.
    other = "Gunna/The Last Wun/endless 2.mp3"
    swapper = make_swapper(ears=GoneWithTheThirdTry(music, ["clean"]))
    swapper.fakes.soulsync.lands = [(30, ENDLESS_REL, b"clean try 1"), (30, ENDLESS_REL, b"clean try 2"),
                                    (30, other, b"clean try 3")]

    outcome = swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == "couldnt"
    assert outcome.reason == (f"3 copies came down clean, and the original has gone from {HELD}, so the copy "
                              "set aside last was put back")
    assert read(music, other) == b"clean try 3" and not os.path.exists(os.path.join(music, HELD, other))
    assert read(music, SECOND_HELD) == b"clean try 1" and read(music, THIRD_HELD) == b"clean try 2"
    assert store.swaps() == []


def test_a_restart_after_the_original_went_back_ends_as_put_back_and_keeps_the_later_copy_held(
        make_swapper, music, store, monkeypatch):
    # The original went back, but the swap couldn't be forgotten (the store, or Nova, stopped first): at
    # the next start the original isn't in .nova-held any more, as it's back in its place. The latest
    # copy set aside must stay held, not be put in the library beside it.
    other = "Gunna/The Last Wun/endless 2.mp3"
    swapper = make_swapper(ears=FakeEars(["clean"]))
    swapper.fakes.soulsync.lands = [(30, ENDLESS_REL, b"clean try 1"), (30, ENDLESS_REL, b"clean try 2"),
                                    (30, other, b"clean try 3")]
    real = store.end_swap
    ends = []

    def end_swap(key):
        ends.append(key)
        if len(ends) == 1:
            raise OSError("disk full")
        real(key)

    monkeypatch.setattr(store, "end_swap", end_swap)
    assert swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True) == "only_clean"
    [saved] = store.swaps()
    assert saved["stage"] == "putting_back" and read(music, ENDLESS_REL) == b"clean endless"

    again = make_swapper(admin=None)
    again.resume()

    assert read(music, ENDLESS_REL) == b"clean endless"
    assert read(music, f"{HELD}/{other}") == b"clean try 3" and not os.path.exists(os.path.join(music, other))
    assert store.swaps() == []
    assert [entry["name"] for entry in store.listed("only_clean")] == ["Gunna - endless"]
    assert store.listed("couldnt") == []


def test_a_later_clean_copy_which_cant_be_set_aside_has_the_original_put_back_beside_it(make_swapper, music, store,
                                                                                       monkeypatch):
    other = "Gunna/The Last Wun/endless 2.mp3"
    swapper = make_swapper(ears=FakeEars(["clean"]))
    swapper.fakes.soulsync.lands = [(30, other, b"clean try 1")]
    real = MusicFolder.set_aside

    def set_aside(self, rel):
        if rel == other:
            raise MusicFolderError("couldn't move the song: disk trouble")
        return real(self, rel)

    monkeypatch.setattr(MusicFolder, "set_aside", set_aside)

    outcome = swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == "couldnt" and outcome.reason == "couldn't move the song: disk trouble"
    assert read(music, ENDLESS_REL) == b"clean endless" and read(music, other) == b"clean try 1"
    assert not os.path.exists(os.path.join(music, HELD, ENDLESS_REL)) and store.swaps() == []


@pytest.mark.parametrize("ending", ["not in time", "not found"])
def test_a_later_try_which_ends_the_swap_puts_the_original_back(make_swapper, music, store, ending):
    # The first try came down clean, and the next didn't land or couldn't be asked for: his own copy
    # goes back, never the clean one the first try brought, which stays held.
    swapper = make_swapper(ears=FakeEars(["clean"]))
    fakes = swapper.fakes
    fakes.soulsync.lands = [(30, ENDLESS_REL, b"clean try 1"), None]
    if ending == "not found":
        fakes.admin.on_forget = lambda: len(fakes.admin.forgot) == 2 and setattr(
            fakes.soulsync, "find_fails", SoulSyncError(NOT_FOUND))

    swapper.swap(_endless(fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert read(music, ENDLESS_REL) == b"clean endless"
    assert read(music, "Gunna/The Last Wun/endless.lrc") == b"[00:01] lyrics"
    assert not os.path.exists(os.path.join(music, HELD, ENDLESS_REL))
    assert read(music, SECOND_HELD) == b"clean try 1"
    assert store.swaps() == []


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
    # Marked as put back, so the library check leaves it for seven days.
    assert store.listed("couldnt") == [{"name": "Gunna - endless", "song_id": f"id-{ENDLESS_REL}",
                                        "reason": "SoulSync didn't bring another copy in time", "at": AT,
                                        "original_back": True}]
    assert store.swaps() == []


def test_a_later_try_which_doesnt_land_after_clean_ones_ends_only_clean(make_swapper, music, store):
    # Seen live on 17 Nov: tries 1 and 2 came down clean and try 3 didn't land in time. Every copy
    # which landed was clean, so the song only comes down clean, and the check leaves it for good.
    swapper = make_swapper(ears=FakeEars(["clean"]))
    fakes = swapper.fakes
    fakes.soulsync.lands = [(30, ENDLESS_REL, b"clean try 1"), (30, ENDLESS_REL, b"clean try 2"), None]

    outcome = swapper.swap(_endless(fakes.library), ENDLESS_WANTED, "kitchen", quiet=False)

    assert outcome == "only_clean"
    assert outcome.reason == "2 copies came down clean, and SoulSync didn't bring another copy in time"
    assert fakes.soulsync.unwished == ["deezer-3"]
    assert read(music, ENDLESS_REL) == b"clean endless"
    assert read(music, SECOND_HELD) == b"clean try 1" and read(music, THIRD_HELD) == b"clean try 2"
    [entry] = store.listed("only_clean")
    assert entry["song_id"] == f"id-{ENDLESS_REL}" and entry["reason"] == outcome.reason
    assert store.listed("couldnt") == [] and store.swaps() == []


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
    assert "original_back" not in store.listed("couldnt")[0]
    # Both kept is where it ends: nothing is tried again.
    assert store.swaps() == []


def test_without_an_admin_profile_nothing_is_moved(make_swapper, music, store):
    swapper = make_swapper(admin=None)

    outcome = swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == "couldnt" and outcome.reason == "SoulSync admin profile isn't set up"
    assert store.listed("couldnt") == [{"name": "Gunna - endless", "song_id": f"id-{ENDLESS_REL}",
                                        "reason": "SoulSync admin profile isn't set up", "at": AT}]
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


SECOND_HELD = f"{HELD}/Gunna/The Last Wun/endless (2).mp3"
THIRD_HELD = f"{HELD}/Gunna/The Last Wun/endless (3).mp3"


@pytest.mark.parametrize("kind, listed", [("unsure", "unsure"), ("not_judged", "couldnt")])
def test_a_new_copy_not_heard_explicit_is_set_aside_and_the_original_put_back(make_swapper, music, store, kind,
                                                                           listed):
    swapper = make_swapper(ears=FakeEars([kind]))
    fakes = swapper.fakes
    fakes.soulsync.lands = [(30, ENDLESS_REL, b"new endless")]

    outcome = swapper.swap(_endless(fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == kind
    # Only an explicit copy is swapped in: this one is held beside the original, never deleted, and
    # the original clean copy, and its lyrics, are back where they were.
    assert read(music, ENDLESS_REL) == b"clean endless"
    assert read(music, "Gunna/The Last Wun/endless.lrc") == b"[00:01] lyrics"
    assert read(music, SECOND_HELD) == b"new endless"
    assert not os.path.exists(os.path.join(music, HELD, ENDLESS_REL))
    assert len(fakes.soulsync.requested) == 1 and store.swaps() == []
    # Navidrome is asked to scan, to list the original again.
    assert len(fakes.library.scans) >= 2
    # Listed with the original's id, and the ids of the copies set aside, so the library check doesn't swap
    # the song again every night (it skips them for thirty days).
    [entry] = store.listed(listed)
    assert entry["song_id"] == f"id-{ENDLESS_REL}" and entry["kept"] == [f"id-{ENDLESS_REL}"] and entry["at"] == AT
    if kind == "not_judged":
        assert outcome.reason == "listening took too long"
        assert entry["reason"] == "couldn't check the copy SoulSync brought: listening took too long"
    else:
        assert outcome.reason == "heard 2 of its 10 swears" and entry["reason"] == outcome.reason


def test_an_unsure_new_copy_after_a_clean_one_puts_the_original_back_and_keeps_the_clean_copy_held(
        make_swapper, music, store):
    swapper = make_swapper(ears=FakeEars(["clean", "unsure"]))
    fakes = swapper.fakes
    fakes.soulsync.lands = [(30, ENDLESS_REL, b"clean try 1"), (30, ENDLESS_REL, b"unsure try 2")]

    outcome = swapper.swap(_endless(fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == "unsure" and len(fakes.ears.heard) == 2
    # The original is the first set aside; earlier tries' clean copies stay held, as does the new one.
    assert read(music, ENDLESS_REL) == b"clean endless"
    assert read(music, SECOND_HELD) == b"clean try 1"
    assert read(music, THIRD_HELD) == b"unsure try 2"
    assert store.swaps() == []


def test_every_new_copy_a_try_brought_is_set_aside_when_none_is_explicit(make_swapper, music, store):
    swapper = make_swapper(ears=FakeEars(["unsure"]))
    fakes = swapper.fakes
    other = "Gunna/The Last Wun/endless 2.mp3"
    fakes.soulsync.lands = [[(30, ENDLESS_REL, b"unsure a"), (30, other, b"unsure b")]]

    outcome = swapper.swap(_endless(fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == "unsure"
    assert read(music, ENDLESS_REL) == b"clean endless"
    assert not os.path.exists(os.path.join(music, other))
    assert read(music, f"{HELD}/{other}") == b"unsure b"
    assert read(music, SECOND_HELD) == b"unsure a"
    assert store.swaps() == []


@pytest.mark.parametrize("kind", ["explicit", "too_few"])
def test_an_explicit_new_copy_is_still_swapped_in(make_swapper, music, store, kind):
    swapper = make_swapper(ears=FakeEars([kind]))
    swapper.fakes.soulsync.lands = [(30, ENDLESS_REL, b"new endless")]

    outcome = swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == "swapped" and read(music, ENDLESS_REL) == b"new endless"
    assert read(music, f"{HELD}/{ENDLESS_REL}") == b"clean endless"


def test_a_rejected_new_copy_is_saved_before_it_moves_and_so_is_the_original_going_back(
        make_swapper, music, store, monkeypatch):
    swapper = make_swapper(ears=FakeEars(["unsure"]))
    swapper.fakes.soulsync.lands = [(30, ENDLESS_REL, b"new endless")]
    seen = []
    real_set_aside, real_put_back = MusicFolder.set_aside, MusicFolder.put_back

    def set_aside(self, rel):
        [saved] = store.swaps()
        seen.append(("set aside", saved["stage"], saved["copy"]["rel"], bool(saved.get("rejected"))))
        return real_set_aside(self, rel)

    def put_back(self, held):
        [saved] = store.swaps()
        seen.append(("put back", saved["stage"], saved["held"][0]["original"], bool(saved.get("rejected"))))
        return real_put_back(self, held)

    monkeypatch.setattr(MusicFolder, "set_aside", set_aside)
    monkeypatch.setattr(MusicFolder, "put_back", put_back)

    swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)

    # The original, then the new copy (marked rejected as it goes), then the original back, saved as such.
    assert seen == [("set aside", "setting_aside", ENDLESS_REL, False),
                    ("set aside", "setting_aside", ENDLESS_REL, True),
                    ("put back", "putting_back", ENDLESS_REL, True)]


def _rejected_not_saved(make_swapper, store, monkeypatch, rel):
    """A swap whose new copy, filed at rel, is heard unsure and then can't be saved as being set aside."""
    swapper = make_swapper(ears=FakeEars(["unsure"]))
    swapper.fakes.soulsync.lands = [(30, rel, b"new endless")]
    real = store.save_swap

    def save_swap(swap):
        if swap.get("rejected"):
            raise OSError("disk full")
        real(swap)

    monkeypatch.setattr(store, "save_swap", save_swap)
    return swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)


def test_a_rejected_new_copy_which_cant_be_saved_where_the_original_was_keeps_both(make_swapper, music, store,
                                                                                  monkeypatch):
    outcome = _rejected_not_saved(make_swapper, store, monkeypatch, ENDLESS_REL)

    # Nothing of the new copy moved, and it's where the original was, so both are kept.
    assert outcome == "couldnt" and outcome.reason.startswith(clean_swap.NOT_SAVED + ", and a new file is where")
    assert read(music, ENDLESS_REL) == b"new endless" and read(music, f"{HELD}/{ENDLESS_REL}") == b"clean endless"
    assert store.swaps() == []


def test_a_rejected_new_copy_which_cant_be_saved_elsewhere_has_the_original_put_back(make_swapper, music, store,
                                                                                    monkeypatch):
    other = "Gunna/The Last Wun/endless 2.mp3"

    outcome = _rejected_not_saved(make_swapper, store, monkeypatch, other)

    # Nothing new moves, and the original goes back where it was: never left held for want of a save.
    assert outcome == "couldnt" and outcome.reason == clean_swap.NOT_SAVED
    assert read(music, ENDLESS_REL) == b"clean endless" and read(music, other) == b"new endless"
    assert not os.path.exists(os.path.join(music, HELD, ENDLESS_REL))
    [entry] = store.listed("couldnt")
    assert entry["kept"] == [f"id-{other}"] and store.swaps() == []


def test_an_original_which_cant_be_put_back_keeps_the_swap_and_is_put_back_at_the_next_try(
        make_swapper, music, store, monkeypatch):
    real_put_back = MusicFolder.put_back
    monkeypatch.setattr(MusicFolder, "put_back", lambda self, held: "couldn't move the song: disk trouble")
    swapper = make_swapper(ears=FakeEars(["unsure"]))
    swapper.fakes.soulsync.lands = [(30, ENDLESS_REL, b"new endless")]

    outcome = swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, "kitchen", quiet=False)

    assert outcome == "couldnt"
    assert outcome.reason == ("heard 2 of its 10 swears, and it couldn't be put back (couldn't move the song: disk "
                              f"trouble), so it's still in {HELD}")
    assert read(music, f"{HELD}/{ENDLESS_REL}") == b"clean endless" and read(music, SECOND_HELD) == b"new endless"
    [saved] = store.swaps()
    assert saved["stage"] == "putting_back" and saved["rejected"]["kind"] == "unsure"

    monkeypatch.setattr(MusicFolder, "put_back", real_put_back)
    again = make_swapper(admin=None)
    again.resume()

    # The original is back, and the swap ends as it was heard.
    assert read(music, ENDLESS_REL) == b"clean endless" and read(music, SECOND_HELD) == b"new endless"
    assert store.swaps() == []
    assert again.said == []


def test_two_rejected_copies_are_all_listed_for_the_library_check(make_swapper, music, store):
    swapper = make_swapper(ears=FakeEars(["unsure"]))
    other = "Gunna/The Last Wun/endless 2.mp3"
    swapper.fakes.soulsync.lands = [[(30, ENDLESS_REL, b"unsure a"), (30, other, b"unsure b")]]

    swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert sorted(store.listed("unsure")[0]["kept"]) == sorted([f"id-{ENDLESS_REL}", f"id-{other}"])


@pytest.mark.parametrize("ears, lands", [(["explicit"], [(30, ENDLESS_REL, b"explicit endless")]),
                                         (["clean"], [(30, ENDLESS_REL, b"clean try")] * 3),
                                         (["unsure"], [(30, ENDLESS_REL, b"unsure endless")])])
def test_a_songs_earlier_couldnt_entry_goes_when_its_swap_finally_ends(make_swapper, music, store, ears, lands):
    # A put-back which failed was listed at once; the entry mustn't stay once the swap has ended another way.
    swapper = make_swapper(ears=FakeEars(ears))
    swapper.fakes.soulsync.lands = list(lands)
    store.add("couldnt", "Gunna - endless", "it couldn't be put back", song_id=f"id-{ENDLESS_REL}")
    store.add("couldnt", "Other", "why", song_id="other-id")

    outcome = swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome != "couldnt"
    assert [entry["name"] for entry in store.listed("couldnt")] == ["Other"]


def test_a_rejected_copy_which_cant_be_set_aside_puts_the_original_back_when_its_path_is_free(
        make_swapper, music, store, monkeypatch):
    swapper = make_swapper(ears=FakeEars(["unsure"]))
    first, second = "Gunna/The Last Wun/endless 2.mp3", "Gunna/The Last Wun/endless 3.mp3"
    swapper.fakes.soulsync.lands = [[(30, first, b"unsure a"), (30, second, b"unsure b")]]
    real = MusicFolder.set_aside

    def set_aside(self, rel):
        if rel == second:
            raise MusicFolderError("couldn't move the song: disk trouble")
        return real(self, rel)

    monkeypatch.setattr(MusicFolder, "set_aside", set_aside)

    outcome = swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)

    # The first copy is held and the original's path is free, so the original goes back; the other copy
    # stays in the library, and both are listed for the library check.
    assert outcome == "couldnt" and outcome.reason == "couldn't move the song: disk trouble"
    assert read(music, ENDLESS_REL) == b"clean endless" and read(music, f"{HELD}/{first}") == b"unsure a"
    assert read(music, second) == b"unsure b" and store.swaps() == []
    assert sorted(store.listed("couldnt")[0]["kept"]) == sorted([f"id-{first}", f"id-{second}"])


def test_a_rejected_copy_which_cant_be_set_aside_keeps_both_when_a_new_file_is_in_the_originals_place(
        make_swapper, music, store, monkeypatch):
    swapper = make_swapper(ears=FakeEars(["unsure"]))
    swapper.fakes.soulsync.lands = [(30, ENDLESS_REL, b"unsure a")]
    real = MusicFolder.set_aside
    calls = []

    def set_aside(self, rel):
        calls.append(rel)
        if len(calls) == 2:
            raise MusicFolderError("couldn't move the song: disk trouble")
        return real(self, rel)

    monkeypatch.setattr(MusicFolder, "set_aside", set_aside)

    outcome = swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == "couldnt" and "a new file is where it was, so both are kept" in outcome.reason
    assert read(music, ENDLESS_REL) == b"unsure a" and read(music, f"{HELD}/{ENDLESS_REL}") == b"clean endless"
    assert store.swaps() == []


def test_a_rejected_copy_navidrome_cant_find_the_file_of_is_handled_as_one_which_cant_be_set_aside(
        make_swapper, music, store, monkeypatch):
    swapper = make_swapper(ears=FakeEars(["unsure"]))
    fakes = swapper.fakes
    fakes.soulsync.lands = [(30, ENDLESS_REL, b"unsure a")]
    real = fakes.library.song_file
    calls = []

    def song_file(song_id):
        calls.append(song_id)
        if len(calls) > 1:
            raise NavidromeError("Couldn't ask Navidrome for the song's file: HTTPError 500")
        return real(song_id)

    monkeypatch.setattr(fakes.library, "song_file", song_file)

    outcome = swapper.swap(_endless(fakes.library), ENDLESS_WANTED, None, quiet=True)

    # Nothing of the new copy moved, so it keeps its place, and both are kept, as the original can't go back.
    assert outcome == "couldnt" and outcome.reason.startswith("couldn't find the copy SoulSync brought")
    assert "a new file is where it was" in outcome.reason
    assert read(music, ENDLESS_REL) == b"unsure a" and read(music, f"{HELD}/{ENDLESS_REL}") == b"clean endless"
    assert store.swaps() == []


def test_a_restart_between_moving_a_rejected_copy_and_saving_where_keeps_the_copies_not_yet_moved_recorded(
        make_swapper, music, store):
    other = _rejected_saved(store, music)
    # The first copy moved, as set_aside would have, before the swap could save where.
    put(music, SECOND_HELD, read(music, ENDLESS_REL))
    os.remove(os.path.join(music, ENDLESS_REL))
    swapper = make_swapper()

    swapper.resume()

    # The original goes back; the copy which never moved stays in the library, and is on the list with the
    # first, so the library check leaves both alone.
    assert read(music, ENDLESS_REL) == b"clean endless" and read(music, other) == b"unsure b"
    assert store.swaps() == []
    [entry] = store.listed("couldnt")
    assert entry["reason"] == f"set aside, but where wasn't saved - look in {HELD}"
    assert sorted(entry["kept"]) == sorted([f"id-{ENDLESS_REL}", f"id-{other}"])


def test_a_rejected_unsure_swap_leaves_one_unsure_entry_even_if_ears_listed_the_copy_under_another_name(
        make_swapper, music, store):
    swapper = make_swapper(ears=FakeEars(["unsure"]))
    other = "Gunna/The Last Wun/endless 2.mp3"
    swapper.fakes.soulsync.lands = [(30, other, b"unsure a")]
    # As Ears lists a copy it heard unsure, by the copy's own name and id.
    store.add("unsure", "Gunna - endless (Deluxe)", "heard 2 of its 10 swears", song_id=f"id-{other}")

    swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)

    [entry] = store.listed("unsure")
    assert entry["name"] == "Gunna - endless" and entry["song_id"] == f"id-{ENDLESS_REL}"


@pytest.mark.parametrize("kinds, lands, ended", [
    (["explicit"], [(30, ENDLESS_REL, b"explicit endless")], "swapped"),
    (["explicit"], [None], "couldnt"),
    (["clean"], [(30, ENDLESS_REL, f"clean try {number}".encode()) for number in (1, 2, 3)], "only_clean"),
])
def test_a_swap_tried_again_after_a_rejected_one_takes_the_song_off_the_unsure_list(
        make_swapper, music, store, kinds, lands, ended):
    # Over 30 days after a swap whose new copy was heard unsure, the library check tries again; however
    # this one ends, the song mustn't still be on Unsure as well.
    store.add("unsure", "Gunna - endless (old name)", "heard 2 of its 10 swears", song_id=f"id-{ENDLESS_REL}",
              kept=[f"id-{ENDLESS_REL}"])
    swapper = make_swapper(ears=FakeEars(kinds))
    swapper.fakes.soulsync.lands = list(lands)

    assert swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True) == ended

    assert store.listed("unsure") == []
    assert [entry["name"] for entry in store.listed(ended)] == ["Gunna - endless"]


def _rejected_saved(store, music, second=False):
    """
    A swap stopped part way through setting aside the copies of a try heard unsure: the original
    is held, and so is the first new copy if second is given, as is the case when the restart
    comes between the two; the other new copy is where it landed.
    """
    other = "Gunna/The Last Wun/endless 2.mp3"
    put(music, f"{HELD}/{ENDLESS_REL}", read(music, ENDLESS_REL))
    os.remove(os.path.join(music, ENDLESS_REL))
    held = [{"original": ENDLESS_REL, "held": ENDLESS_REL, "sidecars": []}]
    if second:
        put(music, SECOND_HELD, b"unsure a")
        held.append({"original": ENDLESS_REL, "held": "Gunna/The Last Wun/endless (2).mp3", "sidecars": []})
    else:
        put(music, ENDLESS_REL, b"unsure a")
    put(music, other, b"unsure b")
    copies = [{"id": f"id-{ENDLESS_REL}", "title": "endless", "artist": "Gunna", "rel": ENDLESS_REL},
              {"id": f"id-{other}", "title": "endless", "artist": "Gunna", "rel": other}]
    store.save_swap(_saved(tries=1, track_id="deezer-1", held=held, quiet=True,
                           rejected={"kind": "unsure", "reason": "heard 2 of its 10 swears", "listed": None,
                                     "ids": [copy["id"] for copy in copies]},
                           copy=copies[1] if second else copies[0], pending=[] if second else copies[1:]))
    return other


@pytest.mark.parametrize("second", [False, True])
def test_a_restart_part_way_through_setting_rejected_copies_aside_carries_on(make_swapper, music, store, second):
    other = _rejected_saved(store, music, second)
    swapper = make_swapper(admin=None)

    swapper.resume()

    assert read(music, ENDLESS_REL) == b"clean endless"
    assert read(music, SECOND_HELD) == b"unsure a" and read(music, f"{HELD}/{other}") == b"unsure b"
    assert not os.path.exists(os.path.join(music, other))
    assert store.swaps() == [] and swapper.fakes.soulsync.requested == []


def test_a_restart_once_every_rejected_copy_is_aside_puts_the_original_back(make_swapper, music, store):
    put(music, f"{HELD}/{ENDLESS_REL}", read(music, ENDLESS_REL))
    put(music, SECOND_HELD, b"unsure a")
    os.remove(os.path.join(music, ENDLESS_REL))
    store.save_swap(_saved(
        stage="putting_back", tries=1, quiet=False, reason=None,
        held=[{"original": ENDLESS_REL, "held": ENDLESS_REL, "sidecars": []},
              {"original": ENDLESS_REL, "held": "Gunna/The Last Wun/endless (2).mp3", "sidecars": []}],
        rejected={"kind": "not_judged", "reason": "listening took too long",
                  "listed": "couldn't check the copy SoulSync brought: listening took too long"}))
    swapper = make_swapper()

    swapper.resume()

    assert read(music, ENDLESS_REL) == b"clean endless" and read(music, SECOND_HELD) == b"unsure a"
    assert store.swaps() == []
    assert store.listed("couldnt")[0]["reason"] == "couldn't check the copy SoulSync brought: listening took too long"
    assert swapper.said == [("kitchen", "endless by Gunna came down again, but I couldn't check the new copy, so "
                                        "you've still got the clean version. The new copy is set aside.")]


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


def test_a_store_that_couldnt_be_read_moves_nothing(swap_config, music, tmp_path, monkeypatch):
    # Its file may hold swaps with songs in .nova-held: nothing moves until it can be read.
    path = tmp_path / "clean_songs.json"
    path.write_text('{"swaps": []}')
    def unreadable_file(*args, **kwargs):
        raise PermissionError("Permission denied")
    monkeypatch.setattr(clean_store.storage, "load_json", unreadable_file)
    unreadable = CleanStore(str(path))
    monkeypatch.undo()
    clock = FakeTime()
    library = FakeNavidrome(music, clock)
    admin = FakeAdmin()
    swapper = Swapper(swap_config, FakeEars(), lambda: MusicFolder(music), lambda: FakeSoulSync(music, clock),
                      lambda: admin, lambda: library, unreadable, lambda satellite, message: None,
                      now=clock.now, wait=clock.wait)

    outcome = swapper.swap(_endless(library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == "couldnt" and outcome.reason == "Nova couldn't save the swap's progress"
    assert read(music, ENDLESS_REL) == b"clean endless" and not os.path.exists(os.path.join(music, HELD))
    assert admin.forgot == [] and path.read_text() == '{"swaps": []}'


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


def test_a_song_already_being_swapped_isnt_swapped_a_second_time(make_swapper, music, store):
    # A download's swap is watching for endless; the copy SoulSync brought has landed where the
    # old one was, with its id, and something asks to swap that id again before it's heard.
    _stopped_while_watching(make_swapper)
    put(music, ENDLESS_REL, b"explicit endless")
    swapper = make_swapper()

    outcome = swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == "couldnt" and outcome.reason == "Nova is already swapping it"
    assert read(music, ENDLESS_REL) == b"explicit endless"
    assert not os.path.exists(os.path.join(music, HELD, "Gunna/The Last Wun/endless (2).mp3"))
    assert swapper.fakes.admin.forgot == [] and len(store.swaps()) == 1
    assert store.listed("couldnt") == []


@pytest.mark.parametrize("where", ["copy", "pending"])
def test_a_later_tries_copy_being_swapped_isnt_swapped_a_second_time(make_swapper, music, store, where):
    other = {"id": "id-other", "title": "endless", "artist": "Gunna", "rel": "Gunna/other.mp3"}
    store.save_swap(_saved(song_id="id-first", copy=other if where == "copy" else _saved()["copy"],
                           pending=[other] if where == "pending" else []))
    put(music, "Gunna/other.mp3", b"clean try 1")
    swapper = make_swapper()

    outcome = swapper.swap({"id": "id-other", "title": "endless", "artist": "Gunna"}, ENDLESS_WANTED, None, True)

    assert outcome.reason == "Nova is already swapping it"
    assert read(music, "Gunna/other.mp3") == b"clean try 1"


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
    # It would need forgetting before another try, so it isn't moved at all; it's where the original was,
    # so the original stays held beside it.
    _stopped_while_watching(make_swapper)
    [saved] = store.swaps()

    clock = FakeTime(saved["asked_at"] + 300)
    swapper = make_swapper(clock=clock, admin=None, ears=FakeEars(["clean"]))
    clock.later.append((clock.time + 60, lambda: put(music, ENDLESS_REL, b"clean try 1")))
    swapper.resume()

    assert read(music, ENDLESS_REL) == b"clean try 1" and read(music, f"{HELD}/{ENDLESS_REL}") == b"clean endless"
    assert not os.path.exists(os.path.join(music, HELD, "Gunna/The Last Wun/endless (2).mp3"))
    assert store.swaps() == []
    assert store.listed("couldnt")[0]["reason"] == ("SoulSync admin profile isn't set up, and a new file is where it "
                                                    f"was, so both are kept: the new file, and the old copy in {HELD}")


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


def test_a_put_back_that_fails_keeps_the_swap_to_try_again(make_swapper, music, store, monkeypatch):
    real_put_back = MusicFolder.put_back
    monkeypatch.setattr(MusicFolder, "put_back", lambda self, held: "couldn't move the song: disk trouble")
    swapper = make_swapper()
    swapper.fakes.soulsync.lands = [None]

    outcome = swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, "kitchen", quiet=False)

    assert outcome == "couldnt"
    assert outcome.reason == ("SoulSync didn't bring another copy in time, and it couldn't be put back (couldn't "
                              f"move the song: disk trouble), so it's still in {HELD}")
    assert store.listed("couldnt")[0]["reason"] == outcome.reason
    # Kept, so the copy goes back at the next try rather than staying held for good.
    [saved] = store.swaps()
    assert saved["stage"] == "putting_back" and saved["reason"] == "SoulSync didn't bring another copy in time"
    assert read(music, f"{HELD}/{ENDLESS_REL}") == b"clean endless"

    monkeypatch.setattr(MusicFolder, "put_back", real_put_back)
    # Putting back needs no admin profile, and it's taken off the wishlist only the once.
    again = make_swapper(admin=None)
    again.resume()

    assert read(music, ENDLESS_REL) == b"clean endless"
    assert not os.path.exists(os.path.join(music, HELD, ENDLESS_REL))
    assert store.swaps() == [] and again.fakes.soulsync.unwished == []
    assert store.listed("couldnt")[0]["reason"] == "SoulSync didn't bring another copy in time"
    # Said once already, as it first failed.
    assert again.said == []


def test_a_held_copy_that_has_gone_ends_the_swap(make_swapper, music, store, monkeypatch):
    # Emptied by hand: trying again would never find it.
    monkeypatch.setattr(MusicFolder, "put_back", lambda self, held: "the held copy has gone")
    swapper = make_swapper()
    swapper.fakes.soulsync.lands = [None]

    outcome = swapper.swap(_endless(swapper.fakes.library), ENDLESS_WANTED, None, quiet=True)

    assert outcome == "couldnt" and outcome.reason.endswith(f"the copy set aside has gone from {HELD}")
    assert store.swaps() == []


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
    (Outcome("unsure"), "endless by Gunna came down again, but not plainly explicit, so you've still got the clean "
                        "version. The new copy is set aside."),
    (Outcome("not_judged", "listening took too long"),
     "endless by Gunna came down again, but I couldn't check the new copy, so you've still got the clean version. "
     "The new copy is set aside."),
    (Outcome("couldnt", "Navidrome still lists it"),
     "endless by Gunna has been downloaded, but it's the clean version, and I couldn't swap it: Navidrome still "
     "lists it."),
    (Outcome("couldnt", NOT_FOUND),
     "endless by Gunna has been downloaded, but it's the clean version, and I couldn't swap it: SoulSync couldn't "
     "find it."),
])
def test_a_swaps_outcome_is_said_as_the_spec_has_it(outcome, said):
    assert swap_said("endless by Gunna", outcome) == said
