"""Tests for recording until the user stops, from a feed of audio."""

import logging
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from voice_assistant import config, protocol, recording
from voice_assistant.recording import FRAME, RATE, AudioFeed, record_until_silence


class FakeDetector:
    """Calls a frame speech if it's loud, which the tests make it or not."""

    threshold = 0.5

    def reset(self):
        pass

    def speech_probability(self, frame):
        return 1.0 if np.abs(frame).mean() > 1000 else 0.0


def seconds(length, value):
    return np.full(int(length * RATE) // FRAME * FRAME, value, dtype=np.int16)


def record(feed, **kwargs):
    settings = dict(silence_duration=0.6, speech_timeout=0.9, timeout=5.0)
    settings.update(kwargs)
    return record_until_silence(feed, FakeDetector(), **settings)


def test_a_fed_recording_stops_once_the_user_goes_quiet():
    feed = AudioFeed()
    feed.feed(seconds(0.5, 2000))
    feed.feed(seconds(1.0, 0))

    audio = record(feed, listen_from=0)

    assert abs(len(audio) / RATE - 1.1) < 0.05


def test_a_feed_which_ends_part_way_gives_what_was_heard():
    feed = AudioFeed()
    feed.feed(seconds(0.5, 2000))
    feed.feed(np.full(100, 2000, dtype=np.int16))
    feed.end()

    audio = record(feed, listen_from=0)

    assert len(audio) == 0.5 * RATE // FRAME * FRAME + 100
    assert np.all(audio > 0.05)


def test_a_feed_with_no_speech_gives_nothing():
    feed = AudioFeed()
    feed.feed(seconds(2.0, 0))

    audio = record(feed, listen_from=0)

    assert len(audio) == 0
    assert audio.dtype == np.float32


def test_a_recording_follows_audio_fed_from_another_thread():
    feed = AudioFeed()

    def feeder():
        for value in [2000] * 5 + [0] * 10:
            feed.feed(seconds(0.1, value))
            time.sleep(0.01)

    thread = threading.Thread(target=feeder)
    thread.start()
    audio = record(feed, listen_from=0)
    thread.join()

    assert 0.5 * RATE < len(audio) < 1.5 * RATE
    assert np.all(audio[:int(0.4 * RATE)] > 0.05)


def test_end_wakes_a_reader_already_waiting():
    feed = AudioFeed()
    feed.feed(seconds(0.5, 2000))
    threading.Timer(0.2, feed.end).start()

    started = time.monotonic()
    audio = record(feed, listen_from=0)

    assert time.monotonic() - started < 1.5
    assert len(audio) == 0.5 * RATE // FRAME * FRAME


def test_a_feed_which_stalls_counts_as_ended():
    feed = AudioFeed(stall_seconds=0.2)
    feed.feed(seconds(0.5, 2000))

    started = time.monotonic()
    audio = record(feed, listen_from=0)

    assert time.monotonic() - started < 1.0
    assert len(audio) == 0.5 * RATE // FRAME * FRAME


def test_a_feed_drops_what_comes_past_its_cap(caplog):
    # A sender which doesn't stop mustn't grow the feed for ever - the satellite's own
    # limit is the only other guard.
    feed = AudioFeed(max_seconds=1.0)
    with caplog.at_level(logging.WARNING, logger=recording.__name__):
        feed.feed(np.full(int(0.6 * RATE), 1, dtype=np.int16))
        feed.feed(np.full(int(0.6 * RATE), 2, dtype=np.int16))
        feed.feed(np.full(int(0.6 * RATE), 3, dtype=np.int16))
    feed.end()

    assert feed.position == RATE
    kept = feed.listen(start=0).read(2 * RATE)
    assert len(kept) == RATE
    # The piece which crossed the cap keeps what fits, and the one after it is dropped.
    assert (kept[:int(0.6 * RATE)] == 1).all() and (kept[int(0.6 * RATE):] == 2).all()
    # Said once, not for each piece.
    assert [record.levelno for record in caplog.records] == [logging.WARNING]


def test_the_cap_leaves_room_for_the_longest_request_and_its_pre_roll():
    # A feed holds a request which is the longest the settings allow, with the most
    # pre-roll a wake may say is at its start (in samples: a message's audio is 16-bit).
    longest = config.LIMITS[("thresholds", "recording_timeout")].high
    assert recording.MAX_FEED_SECONDS >= longest + protocol.MAX_AUDIO // 2 / RATE


def _counting(start, end):
    """Samples which are each their own position (kept off 0, which is what silence reads as)."""
    return (np.arange(start, end) % 30000 + 1).astype(np.int16)


def test_an_open_feed_has_no_cap_and_keeps_a_bounded_amount():
    feed = AudioFeed(open=True)
    seconds = 3 * recording.MAX_FEED_SECONDS
    for start in range(0, int(seconds * RATE), RATE // 10):
        feed.feed(_counting(start, start + RATE // 10))
    # None of it dropped as past a cap, and positions go on counting from the first sample fed.
    assert feed.position == int(seconds * RATE)
    # Only so much is held, however long it goes on.
    assert len(feed._samples) <= 2 * recording.OPEN_KEEP_SECONDS * RATE
    assert len(feed._arrivals) <= 2 * recording.OPEN_KEEP_SECONDS * 10 + 1


def test_an_open_feed_keeps_at_least_the_last_ten_seconds_and_reads_older_audio_as_silence():
    assert recording.OPEN_KEEP_SECONDS >= 10
    feed = AudioFeed(open=True)
    total = 100 * RATE
    for start in range(0, total, RATE // 10):
        feed.feed(_counting(start, start + RATE // 10))
    # What's kept reads back by its position - from a second back, or from the first of the last ten.
    assert np.array_equal(feed.listen(start=total - RATE).read(RATE), _counting(total - RATE, total))
    assert np.array_equal(feed.listen(start=total - 10 * RATE).read(10 * RATE), _counting(total - 10 * RATE, total))
    # What was let go of is as many samples of silence, so a position still means what it did.
    old = feed.listen(start=0)
    assert not np.any(old.read(RATE)) and old.position == RATE
    edge = feed._dropped
    assert edge > 0
    across = feed.listen(start=edge - 5).read(10)
    assert list(across[:5]) == [0] * 5 and np.array_equal(across[5:], _counting(edge, edge + 5))
    # And it is still when its audio arrived which is asked for, not a position that isn't there.
    assert feed.time_at(total) >= feed.time_at(0)


def test_a_span_before_what_an_open_feed_keeps_is_silence_of_the_length_asked_for():
    feed = AudioFeed(open=True)
    total = 100 * RATE
    for start in range(0, total, RATE // 10):
        feed.feed(_counting(start, start + RATE // 10))
    edge = feed._dropped
    assert edge > 3 * RATE and len(feed._samples) > 3 * RATE
    # Ending a few seconds before what's kept, which numpy would take as a place counted
    # back from the end of it, and answer with nearly all of it.
    before = feed.listen(start=edge - 3 * RATE)
    assert not np.any(before.read(RATE)) and len(before.read(RATE)) == RATE
    assert before.position == edge - RATE
    # Right up to the edge, and with its end exactly on it.
    assert len(feed.listen(start=edge - RATE).read(RATE)) == RATE
    assert not np.any(feed.listen(start=edge - RATE).read(RATE))
    # Straddling it: silence, then what's kept, in the one read.
    across = feed.listen(start=edge - RATE).read(2 * RATE)
    assert len(across) == 2 * RATE and not np.any(across[:RATE])
    assert np.array_equal(across[RATE:], _counting(edge, edge + RATE))


def test_an_open_feed_still_ends_when_the_satellite_stops_sending():
    feed = AudioFeed(stall_seconds=0.05, open=True)
    feed.feed(_counting(0, 1600))
    assert len(feed.listen(start=0).read(1600)) == 1600
    assert len(feed.listen(start=1600).read(1600)) == 0
    assert feed.ended


def test_a_feed_has_an_event_for_a_cut_in():
    feed = AudioFeed()
    assert not feed.cut_in.is_set()
    feed.cut_in.set()
    assert feed.cut_in.is_set()


def test_audio_fed_after_the_feed_ended_is_ignored():
    feed = AudioFeed()
    feed.feed(np.full(1600, 1, dtype=np.int16))
    feed.end()
    feed.feed(np.full(1600, 2, dtype=np.int16))

    assert feed.position == 1600
    assert (feed.listen(start=0).read(3200) == 1).all()


def test_audio_fed_after_the_feed_stalled_is_ignored():
    # A stalled feed counts as ended, and a reader has already been told so.
    feed = AudioFeed(stall_seconds=0.05)
    feed.feed(np.full(1600, 1, dtype=np.int16))
    reader = feed.listen(start=0)
    assert len(reader.read(3200)) == 1600

    feed.feed(np.full(1600, 2, dtype=np.int16))

    assert feed.position == 1600


def test_time_at_counts_back_from_the_piece_a_position_is_in(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(recording, "time", SimpleNamespace(monotonic=lambda: now[0]))
    feed = AudioFeed()
    # With nothing fed there's no arrival to go by, so it's now.
    assert feed.time_at(0) == 100.0

    now[0] = 101.0
    feed.feed(np.zeros(8000, dtype=np.int16))
    now[0] = 102.0
    feed.feed(np.zeros(8000, dtype=np.int16))

    # The whole of a piece was heard before it was sent, so a position in it is counted back from its arrival.
    assert feed.time_at(8000) == 101.0
    assert feed.time_at(4000) == pytest.approx(100.75)
    assert feed.time_at(12000) == pytest.approx(101.75)
    # One not yet fed, such as a mark made as the feed ends, goes by the last arrival.
    assert feed.time_at(20000) == 102.0


def test_a_feed_hands_the_recording_over_as_its_made():
    feed = AudioFeed()
    feed.feed(seconds(0.3, 0))
    feed.feed(seconds(0.6, 2000))
    feed.feed(seconds(1.0, 0))
    heard = []

    audio = record(feed, listen_from=0, heard=heard.append)

    # Everything up to the moment the user was found to be talking, then each frame as it
    # comes - adding up to exactly the recording.
    assert len(heard[0]) > FRAME and all(len(piece) == FRAME for piece in heard[1:])
    assert np.array_equal(np.concatenate(heard), audio)

    # And nothing at all when they never talk.
    silent = AudioFeed()
    silent.feed(seconds(2.0, 0))
    nothing = []
    assert len(record(silent, listen_from=0, heard=nothing.append)) == 0
    assert nothing == []


def test_a_recording_from_a_feed_marks_the_timeline_by_when_the_audio_arrived(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(recording, "time", SimpleNamespace(monotonic=lambda: now[0]))
    marks = {}

    class Timeline:
        def mark(self, stage, at=None):
            marks[stage] = at

    # Quiet, then speech which starts 0.1s before the end of the first piece, and stops at the end of the second.
    feed = AudioFeed()
    feed.feed(np.concatenate([seconds(0.3, 0), np.full(1600, 2000, dtype=np.int16)]))
    now[0] = 11.0
    feed.feed(np.full(8000, 2000, dtype=np.int16))
    now[0] = 12.0
    feed.feed(seconds(1.0, 0))

    record(feed, listen_from=0, timeline=Timeline())

    assert set(marks) == {"recording_started", "speech_started", "speech_ended", "recording_stopped"}
    assert marks["speech_started"] == pytest.approx(9.9)
    assert marks["speech_ended"] == pytest.approx(11.0)


class ChimeDetector(FakeDetector):
    """Calls the first frames quiet however loud they are, as Silero does for a chime."""

    def __init__(self, quiet_frames):
        self.quiet_frames = quiet_frames
        self.seen = 0

    def reset(self):
        self.seen = 0

    def speech_probability(self, frame):
        self.seen += 1
        if self.seen <= self.quiet_frames:
            return 0.0
        return super().speech_probability(frame)


class FakeWords:
    """Stands in for NemotronTranscription: it only knows how much audio it has been given."""

    def __init__(self, real_from, still_from=None):
        # Seconds of audio at which the first real word has been heard, and - if the
        # words stop changing - the seconds of audio at which they last changed.
        self.real_from = real_from
        self.still_from = still_from
        self.given = 0

    def heard(self, audio):
        self.given += len(audio)

    def quiet_words(self):
        heard = self.given / RATE
        if heard < self.real_from:
            return None
        if self.still_from is None:
            return 0.1
        return max(0.0, heard - self.still_from)


# 12 dB below his own loudness: the tests' 8000 against 1500 is only 14.5 dB down.
TWELVE_DB = 10 ** (-12 / 20)
ENDED_BY = {"silence", "voice drop", "no new words", "limit", "feed ended", "no speech"}


def test_a_quieter_background_ends_it_with_the_voice_drop():
    def loud_then_background():
        feed = AudioFeed()
        feed.feed(seconds(1.0, 8000))
        feed.feed(seconds(3.0, 1500))
        return feed

    words, report = FakeWords(0.3), {}
    audio = record(loud_then_background(), listen_from=0, timeout=3.0, voice_drop=TWELVE_DB,
                   words_quiet=words.quiet_words, heard=words.heard, report=report)

    # The loud part ends at 1.0s; it takes a 0.15s window of the background to show, then 0.6s of it.
    assert 1.6 <= len(audio) / RATE <= 1.8
    assert report["ended_by"] == "voice drop"

    # Off, the background is speech to the detector and it runs to the timeout.
    words, report = FakeWords(0.3), {}
    audio = record(loud_then_background(), listen_from=0, timeout=3.0, voice_drop=None,
                   words_quiet=words.quiet_words, heard=words.heard, report=report)

    assert len(audio) / RATE == pytest.approx(3.0)
    assert report["ended_by"] == "limit"


def test_the_voice_drop_has_no_word_to_wait_for_without_words_quiet():
    # The session only gives voice_drop without words_quiet when the speech-to-text can't say its words.
    feed = AudioFeed()
    feed.feed(seconds(1.0, 8000))
    feed.feed(seconds(3.0, 1500))
    report = {}

    audio = record(feed, listen_from=0, timeout=3.0, voice_drop=TWELVE_DB, report=report)

    assert 1.6 <= len(audio) / RATE <= 1.8
    assert report["ended_by"] == "voice drop"
    assert report["words_quiet"] is None


def test_a_short_quiet_dip_mid_sentence_doesnt_end_it():
    feed = AudioFeed()
    for length, value in [(0.5, 8000), (0.3, 1500), (0.5, 8000), (1.0, 0)]:
        feed.feed(seconds(length, value))
    words, report = FakeWords(0.3), {}

    audio = record(feed, listen_from=0, voice_drop=TWELVE_DB,
                   words_quiet=words.quiet_words, heard=words.heard, report=report)

    # 0.5 + 0.3 + 0.5s of speech, then Silero's own 0.6s of quiet.
    assert len(audio) / RATE == pytest.approx(1.9, abs=0.1)
    assert report["ended_by"] == "silence"


def test_neither_rule_ends_it_before_a_real_word():
    # A loud burst Silero calls speech - a voice in a video, say - sets how loud "he" is from
    # the press, and the background after it is far quieter, so without the wait for a word
    # the voice drop would end it at about 1s. He starts at 2.0s; his first word is heard at 2.2s.
    feed = AudioFeed()
    for length, value in [(0.3, 8000), (1.7, 1500), (0.5, 8000), (3.0, 1500)]:
        feed.feed(seconds(length, value))
    words, report = FakeWords(2.2), {}

    audio = record(feed, listen_from=0, voice_drop=TWELVE_DB,
                   words_quiet=words.quiet_words, heard=words.heard, report=report)

    # Ended once he has finished: 0.15s of the background to show, then 0.6s of it.
    assert 3.1 <= len(audio) / RATE <= 3.3
    assert report["ended_by"] == "voice drop"


def test_the_voice_drop_waits_for_a_real_word_before_it_counts_the_quiet():
    # He's loud for 1s, then the background (which the detector calls speech) is far quieter:
    # without the wait it would end at about 1.7s. His first word is only heard at 2.0s.
    feed = AudioFeed()
    feed.feed(seconds(1.0, 8000))
    feed.feed(seconds(3.0, 1500))
    words, report = FakeWords(2.0), {}

    audio = record(feed, listen_from=0, timeout=3.5, voice_drop=TWELVE_DB,
                   words_quiet=words.quiet_words, heard=words.heard, report=report)

    # Quiet is counted from the first word, not backdated: 2.0s and then 0.6s.
    assert len(audio) / RATE == pytest.approx(2.6, abs=0.1)
    assert report["ended_by"] == "voice drop"


def test_loud_sound_silero_doesnt_call_speech_never_sets_the_loudness():
    # The press chime heard by the microphone: 30000, and not speech to Silero.
    feed = AudioFeed()
    for length, value in [(0.3, 30000), (1.0, 8000), (3.0, 1500)]:
        feed.feed(seconds(length, value))
    words, report = FakeWords(0.5), {}

    audio = record_until_silence(feed, ChimeDetector(quiet_frames=10), listen_from=0,
                                 silence_duration=0.6, speech_timeout=0.9, timeout=5.0,
                                 voice_drop=TWELVE_DB, words_quiet=words.quiet_words,
                                 heard=words.heard, report=report)

    assert report["loudness"] == pytest.approx(8000 / 32768, abs=0.005)
    assert report["ended_by"] == "voice drop"
    assert len(audio) / RATE < 2.2


def test_the_chime_cant_set_the_loudness_when_speech_is_found_at_once():
    # With a short min_speech the first window is made as soon as speech is found, and
    # would still hold the end of the chime if it counted frames from before the speech.
    feed = AudioFeed()
    for length, value in [(0.3, 30000), (1.0, 8000), (3.0, 1500)]:
        feed.feed(seconds(length, value))
    words, report = FakeWords(0.5), {}

    record_until_silence(feed, ChimeDetector(quiet_frames=10), listen_from=0, min_speech=0.03,
                         silence_duration=0.6, speech_timeout=0.9, timeout=5.0,
                         voice_drop=TWELVE_DB, words_quiet=words.quiet_words,
                         heard=words.heard, report=report)

    assert report["loudness"] == pytest.approx(8000 / 32768, abs=0.005)
    assert report["ended_by"] == "voice drop"


def test_a_loud_burst_in_the_middle_that_silero_doesnt_call_speech_doesnt_set_the_loudness():
    class BangDetector(FakeDetector):
        def speech_probability(self, frame):
            return 0.0 if np.abs(frame).mean() > 20000 else super().speech_probability(frame)

    feed = AudioFeed()
    for length, value in [(1.0, 8000), (0.3, 30000), (1.0, 0)]:
        feed.feed(seconds(length, value))
    words, report = FakeWords(0.3), {}

    record_until_silence(feed, BangDetector(), listen_from=0, silence_duration=0.6,
                         speech_timeout=0.9, timeout=5.0, voice_drop=TWELVE_DB,
                         words_quiet=words.quiet_words, heard=words.heard, report=report)

    assert report["loudness"] == pytest.approx(8000 / 32768, abs=0.005)


def test_words_which_stop_being_known_stop_no_new_words_but_not_the_voice_drop():
    # NemotronTranscription.quiet_words gives None for good if Nemotron can't say its words,
    # which then can't be taken to have stopped - but a real word was heard, so he is talking.
    def known_for_a_while(words):
        steps = iter(range(10 ** 6))
        return lambda: words.quiet_words() if next(steps) < 20 else None

    # The voice drop carries on, and ends it as it would have.
    feed = AudioFeed()
    feed.feed(seconds(1.0, 8000))
    feed.feed(seconds(3.0, 1500))
    words, report = FakeWords(0.3), {}

    audio = record(feed, listen_from=0, timeout=3.0, voice_drop=TWELVE_DB,
                   words_quiet=known_for_a_while(words), heard=words.heard, report=report)

    assert 1.6 <= len(audio) / RATE <= 1.8
    assert report["ended_by"] == "voice drop"

    # But words which would have been still for 1.5s at 1.8s, over a background too loud for
    # the voice drop, no longer end it.
    feed = AudioFeed()
    feed.feed(seconds(1.0, 8000))
    feed.feed(seconds(3.0, 3000))
    words, report = FakeWords(0.3, still_from=0.3), {}

    audio = record(feed, listen_from=0, timeout=3.0, voice_drop=TWELVE_DB,
                   words_quiet=known_for_a_while(words), heard=words.heard, report=report)

    assert len(audio) / RATE == pytest.approx(3.0, abs=0.05)
    assert report["ended_by"] == "limit"


def test_words_still_for_long_enough_end_it():
    def voice_then_background():
        # His voice for 1s, then a background well below it which Silero still calls speech.
        feed = AudioFeed()
        feed.feed(seconds(1.0, 8000))
        feed.feed(seconds(5.0, 3000))
        return feed

    # Silero never ends it; the words are real from 0.5s and still from his last at 1.0s,
    # so they have been quiet for 1.5s when the audio reaches 2.5s.
    words, report = FakeWords(0.5, still_from=1.0), {}
    audio = record(voice_then_background(), listen_from=0, words_quiet=words.quiet_words, heard=words.heard,
                   report=report)

    assert len(audio) / RATE == pytest.approx(2.5, abs=0.1)
    assert report["ended_by"] == "no new words"
    assert report["words_quiet"] == pytest.approx(1.5, abs=0.1)

    # A shorter wait for it, when the caller asks.
    words, report = FakeWords(0.5, still_from=1.0), {}
    audio = record(voice_then_background(), listen_from=0, no_words_after=1.0, words_quiet=words.quiet_words,
                   heard=words.heard, report=report)

    assert len(audio) / RATE == pytest.approx(2.0, abs=0.1)

    # Words which keep changing keep it going to the limit.
    words, report = FakeWords(0.5), {}
    audio = record(voice_then_background(), listen_from=0, words_quiet=words.quiet_words, heard=words.heard,
                   report=report)

    assert len(audio) / RATE == pytest.approx(5.0, abs=0.05)
    assert report["ended_by"] == "limit"

    # And so do none yet - nothing is ended by words nobody has heard.
    report = {}
    audio = record(voice_then_background(), listen_from=0, words_quiet=lambda: None, report=report)

    assert len(audio) / RATE == pytest.approx(5.0, abs=0.05)
    assert report["ended_by"] == "limit"
    assert report["words_quiet"] is None

    # Nor does it need a report, or the voice drop, to know how loud it is.
    words = FakeWords(0.5, still_from=1.0)
    audio = record(voice_then_background(), listen_from=0, words_quiet=words.quiet_words, heard=words.heard)

    assert len(audio) / RATE == pytest.approx(2.5, abs=0.1)


# The settings' default, which the background below is within: 3000 is 8.5 dB below 8000.
EIGHTEEN_DB = 10 ** (-18 / 20)


@pytest.mark.parametrize("filler", [8000, 5000])
def test_no_new_words_doesnt_end_his_own_long_filler(filler):
    # "Remind me to... ummm... call the dentist" in a quiet room: Silero calls the "ummm" speech
    # and Nemotron finds no word in it, but it's as loud as the rest of what he says (or 4 dB
    # below, for 5000), so it is him still talking.
    feed = AudioFeed()
    for length, value in [(1.0, 8000), (2.5, filler), (1.0, 0)]:
        feed.feed(seconds(length, value))
    words, report = FakeWords(0.3, still_from=1.0), {}

    audio = record(feed, listen_from=0, voice_drop=EIGHTEEN_DB, words_quiet=words.quiet_words,
                   heard=words.heard, report=report)

    # Silero's own 0.6s of quiet once he's finished, as today - not 1.5s into the "ummm".
    assert len(audio) / RATE == pytest.approx(4.1, abs=0.1)
    assert report["ended_by"] == "silence"
    assert report["words_quiet"] >= 1.5


def test_a_short_pause_after_his_long_filler_doesnt_end_it():
    # "Remind me to... ummm... call the dentist", with a 0.3s pause after the "ummm": by then
    # the words have been still for over 1.5s, but the quiet is no longer than a pause he
    # could make in a quiet room today. Nemotron only finds "call" 0.5s after he says it.
    feed = AudioFeed()
    for length, value in [(1.0, 8000), (1.8, 8000), (0.3, 0), (1.0, 8000), (1.0, 0)]:
        feed.feed(seconds(length, value))
    words, report = FakeWords(0.3, still_from=1.0), {}

    def still_until_call():
        quiet = words.quiet_words()
        return 0.1 if quiet is not None and words.given / RATE >= 3.6 else quiet

    audio = record(feed, listen_from=0, timeout=6.0, voice_drop=EIGHTEEN_DB, words_quiet=still_until_call,
                   heard=words.heard, report=report)

    assert len(audio) / RATE == pytest.approx(4.7, abs=0.1)
    assert report["ended_by"] == "silence"


@pytest.mark.parametrize("background, ended_by, length", [(3000, "no new words", 2.5), (6000, "limit", 4.0)])
def test_no_new_words_ends_only_a_background_well_below_his_voice(background, ended_by, length):
    # 3000 is 8.5 dB below his 8000 - quiet enough for the rule, though not for the voice drop
    # at 18 dB - and 6000, 2.5 dB below, sounds as loud as him, so runs to the limit as it
    # did before the rule.
    assert recording.NO_WORDS_BELOW_DB == 6
    feed = AudioFeed()
    feed.feed(seconds(1.0, 8000))
    feed.feed(seconds(3.0, background))
    words, report = FakeWords(0.3, still_from=1.0), {}

    audio = record(feed, listen_from=0, timeout=4.0, voice_drop=EIGHTEEN_DB, words_quiet=words.quiet_words,
                   heard=words.heard, report=report)

    assert len(audio) / RATE == pytest.approx(length, abs=0.1)
    assert report["ended_by"] == ended_by


class MarginDetector(FakeDetector):
    """Sure of a loud frame, and only unsure - within Silero's margin - of one less loud."""

    def speech_probability(self, frame):
        level = np.abs(frame).mean()
        return 1.0 if level > 5000 else 0.4 if level > 1000 else 0.0


def test_no_new_words_cant_end_it_before_his_loudness_is_known():
    # Speech is found on its first 0.12s, but every 0.15s window after it is only maybe
    # speech, so how loud he is never gets set - and without it there's nothing to be below.
    feed = AudioFeed()
    feed.feed(np.full(4 * FRAME, 8000, dtype=np.int16))
    feed.feed(seconds(4.0, 3000))
    words, report = FakeWords(0.1, still_from=0.1), {}

    audio = record_until_silence(feed, MarginDetector(), listen_from=0, silence_duration=0.6,
                                 speech_timeout=0.9, timeout=3.0, words_quiet=words.quiet_words,
                                 heard=words.heard, report=report)

    assert report["loudness"] is None
    assert report["words_quiet"] > 2.5
    assert len(audio) / RATE == pytest.approx(3.0, abs=0.05)
    assert report["ended_by"] == "limit"


def test_a_background_silero_is_unsure_of_counts_as_quiet_only_once_the_voice_has_dropped():
    # Silero's margin: 0.4 is neither speech, for how loud he is, nor quiet, for its own rule.
    def heard(*parts, voice_drop=TWELVE_DB):
        feed = AudioFeed()
        for length, value in parts:
            feed.feed(seconds(length, value))
        words, report = FakeWords(0.3), {}
        audio = record_until_silence(feed, MarginDetector(), listen_from=0, silence_duration=0.6,
                                     speech_timeout=0.9, timeout=4.0, voice_drop=voice_drop,
                                     words_quiet=words.quiet_words, heard=words.heard, report=report)
        return len(audio) / RATE, report["ended_by"]

    # Below the voice drop's line it ends the recording - by the voice drop, not as silence.
    length, ended_by = heard((1.0, 8000), (3.0, 1500))
    assert 1.6 <= length <= 1.8 and ended_by == "voice drop"

    # Without the voice drop it's never quiet enough for Silero's own rule.
    length, ended_by = heard((1.0, 8000), (3.0, 1500), voice_drop=None)
    assert length == pytest.approx(4.0, abs=0.05) and ended_by == "limit"

    # And above the line it starts the quiet again, so the quiet counts from the last of it:
    # the 4000 ends at 1.7s, then 0.15s of the background to show and 0.6s of it.
    length, ended_by = heard((1.0, 8000), (0.4, 1500), (0.3, 4000), (3.0, 1500))
    assert length == pytest.approx(2.42, abs=0.06) and ended_by == "voice drop"


def test_a_voice_which_starts_quiet_and_grows_isnt_ended_by_its_quiet_start():
    # How loud he is so far is the loudest he has been yet, so his first, quiet words never
    # count as a drop from how loud he gets later. Each step is 6 dB up.
    feed = AudioFeed()
    for length, value in [(0.4, 1500), (0.4, 3000), (0.4, 6000), (0.6, 12000), (1.0, 0)]:
        feed.feed(seconds(length, value))
    words, report = FakeWords(0.2), {}

    audio = record(feed, listen_from=0, voice_drop=TWELVE_DB, words_quiet=words.quiet_words,
                   heard=words.heard, report=report)

    # 1.8s of him, then Silero's own 0.6s of quiet.
    assert len(audio) / RATE == pytest.approx(2.4, abs=0.1)
    assert report["ended_by"] == "silence"
    assert report["loudness"] == pytest.approx(12000 / 32768, abs=0.005)


def test_the_report_is_numbers_only():
    feed = AudioFeed()
    feed.feed(seconds(1.0, 8000))
    feed.feed(seconds(3.0, 1500))
    words, report = FakeWords(0.3, still_from=0.3), {}

    record(feed, listen_from=0, timeout=3.0, voice_drop=TWELVE_DB,
           words_quiet=words.quiet_words, heard=words.heard, report=report)

    assert set(report) == {"ended_by", "loudness", "levels", "speech", "words_quiet"}
    assert report["ended_by"] in ENDED_BY
    for key in ("loudness", "words_quiet"):
        assert report[key] is None or isinstance(report[key], float)
    for key in ("levels", "speech"):
        assert report[key] and all(isinstance(value, float) for value in report[key])
    # One of each for every 0.15s window of the speech, rounded.
    assert len(report["levels"]) == len(report["speech"])
    assert all(value == round(value, 4) for value in report["levels"])
    assert all(value == round(value, 2) for value in report["speech"])
    assert report["levels"][0] == pytest.approx(8000 / 32768, abs=0.001)
    assert report["levels"][-1] == pytest.approx(1500 / 32768, abs=0.001)
    assert set(report["speech"]) == {1.0}


def test_without_the_new_arguments_nothing_changes():
    def make_feeds():
        stalled = AudioFeed(stall_seconds=0.2)
        stalled.feed(seconds(0.5, 2000))
        part_way = AudioFeed()
        part_way.feed(seconds(0.5, 2000))
        part_way.feed(np.full(100, 2000, dtype=np.int16))
        part_way.end()
        quiet = AudioFeed()
        quiet.feed(seconds(0.5, 2000))
        quiet.feed(seconds(1.0, 0))
        silent = AudioFeed()
        silent.feed(seconds(2.0, 0))
        ended_early = AudioFeed()
        ended_early.feed(seconds(0.2, 0))
        ended_early.end()
        endless = AudioFeed()
        endless.feed(seconds(4.0, 2000))
        return {"silence": quiet, "feed ended": part_way, "no speech": silent, "limit": endless,
                "stalled": stalled, "ended early": ended_early}

    expected = {"silence": ("silence", 1.1), "feed ended": ("feed ended", 0.5),
                "no speech": ("no speech", 0.0), "limit": ("limit", 2.0),
                "stalled": ("feed ended", 0.5), "ended early": ("no speech", 0.0)}
    plain, reported = make_feeds(), make_feeds()

    for name, (ended_by, length) in expected.items():
        report = {}
        audio = record(reported[name], listen_from=0, timeout=2.0, report=report)

        assert report["ended_by"] == ended_by, name
        assert len(audio) / RATE == pytest.approx(length, abs=0.05), name
        # Asking for a report changes nothing about the recording.
        assert np.array_equal(audio, record(plain[name], listen_from=0, timeout=2.0)), name
        assert report["words_quiet"] is None, name


# Listening which waits for the user to start, as in a conversation.

def a_wait_then_speech(wait, talk, tail=1.0, value=2000, max_seconds=400.0):
    feed = AudioFeed(max_seconds=max_seconds)
    feed.feed(seconds(wait, 0))
    feed.feed(seconds(talk, value))
    feed.feed(seconds(tail, 0))
    return feed


def test_an_open_wait_outlasts_the_speech_timeout():
    report = {}

    audio = record(a_wait_then_speech(20.0, 1.0), listen_from=0, open_wait=60, speech_timeout=3, report=report)

    assert np.count_nonzero(audio > 0.05) == seconds(1.0, 0).size
    assert report["ended_by"] == "silence"

    # Without it the same recording is given up on after the speech timeout.
    audio = record(a_wait_then_speech(20.0, 1.0), listen_from=0, speech_timeout=3, report=report)

    assert len(audio) == 0
    assert report["ended_by"] == "no speech"


def test_a_long_wait_then_a_long_turn_isnt_cut_off():
    # The timeout is how long a turn may be, however long the wait was before it.
    report = {}

    audio = record(a_wait_then_speech(100.0, 30.0), listen_from=0, timeout=90, open_wait=300, report=report)

    assert np.count_nonzero(audio > 0.05) == seconds(30.0, 0).size
    assert report["ended_by"] == "silence"


def test_the_limit_counts_from_the_speech_start_with_an_open_wait():
    report = {}

    audio = record(a_wait_then_speech(3.0, 10.0), listen_from=0, timeout=2.0, open_wait=10, report=report)

    assert report["ended_by"] == "limit"
    assert np.count_nonzero(audio > 0.05) == seconds(2.0, 0).size


def test_the_wait_isnt_kept_or_heard():
    heard = []

    audio = record(a_wait_then_speech(20.0, 1.0), listen_from=0, open_wait=60, heard=heard.append)

    # What's kept starts OPEN_PRE_ROLL before the speech, and what speech-to-text was given is the same audio, in
    # order, with nothing twice or missing between the pre-roll and the speech.
    assert np.argmax(audio > 0.05) == int(recording.OPEN_PRE_ROLL * RATE)
    assert len(audio) / RATE == pytest.approx(recording.OPEN_PRE_ROLL + 1.0 + 0.6, abs=0.1)
    assert np.array_equal(np.concatenate(heard), audio)
    assert len(heard[0]) < len(audio) and all(len(piece) == FRAME for piece in heard[1:])


def test_an_open_wait_keeps_whatever_is_less_than_the_pre_roll_when_speech_comes_at_once():
    feed = a_wait_then_speech(0.2, 1.0)

    audio = record(feed, listen_from=0, open_wait=60)

    assert np.argmax(audio > 0.05) == seconds(0.2, 0).size


def test_keep_before_still_reaches_back_from_where_listening_starts():
    # Speech which began a moment before listen_from is kept, as it is today - it's within the pre-roll - though
    # the recording only hears speech once it's listening.
    before = seconds(1.0, 0).size
    feed = AudioFeed()
    feed.feed(seconds(1.0, 0))
    feed.feed(seconds(0.5, 2000))
    feed.feed(seconds(1.0, 0))

    audio = record(feed, listen_from=before + seconds(0.2, 0).size, keep_before=0.3, open_wait=60)

    assert np.argmax(audio > 0.05) == int(0.3 * RATE) - seconds(0.2, 0).size
    assert np.count_nonzero(audio > 0.05) == seconds(0.5, 0).size


def test_an_open_wait_that_runs_out_says_no_speech():
    feed = AudioFeed()
    feed.feed(seconds(5.0, 0))
    heard, report, marks = [], {}, {}

    class Timeline:
        def mark(self, stage, at=None):
            marks[stage] = at

    audio = record(feed, listen_from=0, open_wait=2.0, heard=heard.append, report=report, timeline=Timeline())

    # What an ordinary recording of no speech returns, and nothing was given to speech-to-text.
    assert len(audio) == 0 and audio.dtype == np.float32
    assert report["ended_by"] == "no speech"
    assert heard == []
    assert set(marks) == {"recording_started"}

    # Speech which starts only after the wait is too late.
    late = a_wait_then_speech(3.0, 1.0)
    assert len(record(late, listen_from=0, open_wait=2.0)) == 0


def test_the_timeline_marks_the_speech_not_the_pre_roll(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(recording, "time", SimpleNamespace(monotonic=lambda: now[0]))
    marks = {}

    class Timeline:
        def mark(self, stage, at=None):
            marks[stage] = at

    feed = AudioFeed()
    feed.feed(seconds(5.0, 0))
    now[0] = 15.0
    feed.feed(np.concatenate([seconds(0.3, 0), seconds(1.0, 2000)]))
    now[0] = 16.0
    feed.feed(seconds(1.0, 0))

    record(feed, listen_from=0, open_wait=60, timeline=Timeline())

    # Speech started when the audio of it arrived, not half a second before, where what's kept starts.
    assert set(marks) == {"recording_started", "speech_started", "speech_ended", "recording_stopped"}
    assert marks["speech_started"] == pytest.approx(14.0, abs=0.05)
    assert marks["speech_ended"] == pytest.approx(15.0, abs=0.05)


def test_the_press_ends_rules_are_judged_from_the_speech_with_an_open_wait():
    feed = AudioFeed()
    feed.feed(seconds(10.0, 0))
    feed.feed(seconds(1.0, 8000))
    feed.feed(seconds(3.0, 1500))
    # The words are heard 2.0s into the speech, which is 2.5s of audio given, with the pre-roll.
    words, report = FakeWords(2.0 + recording.OPEN_PRE_ROLL), {}

    audio = record(feed, listen_from=0, timeout=3.5, open_wait=60, voice_drop=TWELVE_DB,
                   words_quiet=words.quiet_words, heard=words.heard, report=report)

    assert report["ended_by"] == "voice drop"
    assert len(audio) / RATE == pytest.approx(recording.OPEN_PRE_ROLL + 2.0 + 0.6, abs=0.15)
    # How loud he is comes from his speech, not from the quieter wait before it.
    assert report["loudness"] == pytest.approx(8000 / 32768, abs=0.001)
