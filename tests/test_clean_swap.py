"""
Clean Swap Test
Checks how a song is heard against its lyrics (Ears) - decision 0019, sections 1 to 3 - with
fakes in place of Navidrome, the lyrics, ffmpeg and the listening worker, and the store kept in
the test's own folder. The model is never loaded and nothing real is asked.
"""

import logging
import threading

import numpy as np
import pytest
import requests

from voice_assistant import clean_swap
from voice_assistant.clean_ear import Verdict
from voice_assistant.clean_store import CleanStore
from voice_assistant.clean_swap import Ears, listening_library
from voice_assistant.listener import ListenError
from voice_assistant.navidrome import NavidromeError, NavidromeLibrary

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
