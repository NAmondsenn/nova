"""
Measure Cut-in Test
Checks the parts of scripts/manual/measure_cut_in.py, score_cut_in.py and
measure_voice_load.py which need neither a speaker, a microphone nor a model: that a
recording is never written into the repository, how a recording is scored, what counts as
the reply's own words, and how the voice's speed is worked out. The speaker, microphone, echo
canceller and speech detector are stood in for, so nothing here makes a sound.
"""

import importlib.util
import os

import numpy as np
import pytest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "scripts", "manual")


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


measure_cut_in = load("measure_cut_in")
score_cut_in = load("score_cut_in")
measure_voice_load = load("measure_voice_load")

FRAME = score_cut_in.FRAME
RATE = score_cut_in.RATE


class Detector:
    """Stands in for Silero: reads each frame's loudness as its probability of being speech, in tenths."""

    def __init__(self):
        self.resets = 0

    def reset(self):
        self.resets += 1

    def speech_probability(self, frame):
        return int(frame[0]) / 10


def frames(*levels):
    """Audio of one frame for each level, which Detector takes as that many tenths."""
    return np.concatenate([np.full(FRAME, level, dtype=np.int16) for level in levels])


# The recording

def test_an_out_folder_inside_the_repository_is_refused():
    inside = os.path.join(measure_cut_in.ROOT, "tests", "recording")
    with pytest.raises(SystemExit) as refused:
        measure_cut_in.checked_out_folder(inside)
    assert "inside the repository" in str(refused.value)
    with pytest.raises(SystemExit):
        measure_cut_in.checked_out_folder(measure_cut_in.ROOT)
    assert not os.path.exists(inside)


def test_a_link_which_leads_into_the_repository_is_refused(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(os.path.join(measure_cut_in.ROOT, "tests"))
    with pytest.raises(SystemExit):
        measure_cut_in.checked_out_folder(str(link))
    # Not made yet, but under a link which leads in.
    with pytest.raises(SystemExit):
        measure_cut_in.checked_out_folder(str(link / "recording"))


def test_an_out_folder_outside_the_repository_is_used(tmp_path):
    assert measure_cut_in.checked_out_folder(str(tmp_path / "recording")) == str(tmp_path.resolve() / "recording")


class Playback:
    pass


class Speaker:
    """Stands in for the Speaker: notes what it was asked to play, and whether it was stopped."""

    def __init__(self, missing=()):
        self.played = []
        self.stopped = False
        self.missing = missing

    def play_file(self, filename, volume=1.0, kind="reply", cache=True, start=0.0, length=None):
        self.played.append((filename, volume, kind, length))
        return None if filename in self.missing else Playback()

    def stop(self, kinds=None):
        self.stopped = True


class Microphone:
    """Stands in for the Microphone: silence, a block at a time, and a note of whether it's held."""

    def __init__(self, on_read=None):
        self.held = 0
        self.on_read = on_read or (lambda: None)

    def hold(self):
        self.held += 1

    def release(self):
        self.held -= 1

    def listen(self):
        return self

    def read(self, count):
        self.on_read()
        return np.ones(count, dtype=np.int16)


class Echo:
    """Stands in for the EchoCanceller, ready after this many reads."""

    def __init__(self, ready_after=None):
        self.reads = 0
        self.ready_after = ready_after

    @property
    def ready(self):
        return self.ready_after is not None and self.reads >= self.ready_after


def test_it_plays_the_voice_and_music_under_it_and_records_for_as_long_as_asked():
    speaker, microphone = Speaker(), Microphone()
    heard, ready_after = measure_cut_in.measure(speaker, microphone, None, "voice.wav", 2.0, volume=0.8,
                                                music="music.wav", music_volume=0.24)
    assert speaker.played == [("voice.wav", 0.8, "reply", None), ("music.wav", 0.24, "music", 2.0)]
    assert len(heard) == 2 * RATE
    assert heard.dtype == np.int16
    assert ready_after is None
    assert speaker.stopped and microphone.held == 0


def test_it_plays_no_music_unless_given_some():
    speaker = Speaker()
    measure_cut_in.measure(speaker, Microphone(), None, "voice.wav", 0.5, volume=1.0)
    assert [kind for _, _, kind, _ in speaker.played] == ["reply"]


def test_it_says_how_long_the_echo_canceller_took_to_be_ready():
    echo = Echo(ready_after=5)
    microphone = Microphone(on_read=lambda: setattr(echo, "reads", echo.reads + 1))
    _, ready_after = measure_cut_in.measure(Speaker(), microphone, echo, "voice.wav", 1.0, volume=1.0)
    assert ready_after is not None and ready_after >= 0

    echo = Echo(ready_after=None)
    _, never = measure_cut_in.measure(Speaker(), Microphone(), echo, "voice.wav", 1.0, volume=1.0)
    assert never is None


def test_what_cant_be_played_stops_it_and_lets_go_of_the_microphone():
    speaker, microphone = Speaker(missing=("voice.wav",)), Microphone()
    with pytest.raises(SystemExit):
        measure_cut_in.measure(speaker, microphone, None, "voice.wav", 1.0, volume=1.0)
    assert speaker.stopped and microphone.held == 0


# The scoring

def test_the_share_and_longest_run_of_speech_are_counted_in_frames():
    detector = Detector()
    # Speech (5 tenths or more) in frames 1 and 2, 4, and 6 to 8: ten frames, six of speech.
    audio = frames(0, 5, 9, 4, 5, 0, 5, 6, 7, 0)
    scored = score_cut_in.score(audio, detector)
    assert scored["speech_share"] == 0.6
    assert scored["longest_speech"] == round(3 * FRAME / RATE, 3)
    assert scored["first_speech_at"] == round(FRAME / RATE, 3)
    assert detector.resets == 1


def test_a_frame_just_below_the_line_is_not_speech():
    scored = score_cut_in.score(frames(4, 4, 4), Detector())
    assert scored == {"speech_share": 0.0, "longest_speech": 0.0, "first_speech_at": None}


def test_a_part_frame_at_the_end_is_left_out():
    audio = np.concatenate([frames(5, 0), np.full(FRAME - 1, 9, dtype=np.int16)])
    assert score_cut_in.score(audio, Detector())["speech_share"] == 0.5


def test_nothing_recorded_scores_nothing():
    assert score_cut_in.score(np.zeros(0, dtype=np.int16), Detector())["speech_share"] == 0.0


def test_words_are_the_replys_own_when_they_are_a_run_of_what_it_said():
    said = "The weather today is dry, and bright!"
    assert score_cut_in.own_words("weather today is dry", said)
    assert score_cut_in.own_words("Dry and bright.", said)
    assert score_cut_in.own_words("the weather today is dry and bright", said)
    assert not score_cut_in.own_words("weather is dry", said)
    assert not score_cut_in.own_words("stop the music", said)
    assert not score_cut_in.own_words("the weather today is dry and bright and warm", said)


def test_an_apostrophe_stays_in_a_word():
    assert score_cut_in.own_words("that's all", "Well that's all I know.")
    assert not score_cut_in.own_words("thats all", "Well that's all I know.")


# The voice's speed

def test_the_longest_gap_is_the_longest_between_two_pieces():
    assert measure_voice_load.longest_gap([0.0, 0.1, 0.5, 0.6]) == pytest.approx(0.4)
    assert measure_voice_load.longest_gap([1.0]) == 0.0
    assert measure_voice_load.longest_gap([]) == 0.0


def test_the_real_time_factor_is_the_time_taken_over_the_speech_made():
    assert measure_voice_load.real_time_factor(3.0, 12.0) == 0.25
    assert measure_voice_load.real_time_factor(3.0, 0.0) == 0.0


class Voice:
    """Stands in for Piper: a quarter of a second of speech for each sentence, in two pieces."""

    sample_rate = 22050

    def stream(self, text, remember=True):
        for _ in range(2):
            yield np.zeros(self.sample_rate // 8, dtype=np.float32)


def test_it_makes_speech_until_there_is_enough():
    _, made, gap = measure_voice_load.synthesise(Voice(), 1.0)
    # A sentence at a time, so it ends within a sentence of what was asked.
    assert 1.0 <= made < 1.25 + 0.01
    assert gap >= 0.0


def test_the_clip_nemotron_hears_is_cut_into_pieces_at_its_rate():
    pieces = measure_voice_load.clip_pieces(Voice())
    size = round(measure_voice_load.CLIP_PIECE * RATE)
    assert all(len(piece) == size for piece in pieces[:-1])
    assert sum(len(piece) for piece in pieces) == pytest.approx(
        len(measure_voice_load.SENTENCES) * 0.25 * RATE, abs=10)
