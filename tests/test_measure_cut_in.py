"""
Measure Cut-in Test
Checks the parts of scripts/manual/measure_cut_in.py, score_cut_in.py and
measure_voice_load.py which need neither a speaker, a microphone nor a model: that a
recording is never written into the repository, that nothing plays at volumes it couldn't
find or on a speaker which didn't open, what's recorded before and after the echo canceller is
ready, how a recording is scored, how the voice's speed is worked out, and that the brain's two
scripts can be run from standard input. The speaker, microphone, echo canceller and speech
detector are stood in for, so nothing here makes a sound.
"""

import importlib.util
import json
import os
import subprocess
import sys

import numpy as np
import pytest
import soundfile as sf

from voice_assistant import nemotron, recording

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
    """Stands in for a Playback: not done until it's told it is."""

    done = False


class Speaker:
    """Stands in for the Speaker: notes what it was asked to play, and whether it was stopped."""

    def __init__(self, missing=(), finished=()):
        self.played = []
        self.stopped = False
        self.missing = missing
        self.finished = finished
        self.playbacks = []

    def play_file(self, filename, volume=1.0, kind="reply", cache=True, start=0.0, length=None):
        self.played.append((filename, volume, kind, length))
        playback = Playback()
        playback.done = filename in self.finished
        self.playbacks.append(playback)
        return None if filename in self.missing else playback

    def stop(self, kinds=None):
        self.stopped = True


class Microphone:
    """Stands in for the Microphone: silence, a block at a time, and a note of whether it's held."""

    def __init__(self, on_read=None):
        self.held = 0
        self.reads = 0
        self.on_read = on_read or (lambda: None)

    def hold(self):
        self.held += 1

    def release(self):
        self.held -= 1

    def listen(self):
        return self

    def read(self, count):
        self.reads += 1
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
    heard, ready = measure_cut_in.measure(speaker, microphone, None, "voice.wav", 2.0, volume=0.8,
                                          music="music.wav", music_volume=0.24)
    assert speaker.played == [("voice.wav", 0.8, "reply", None), ("music.wav", 0.24, "music", 2.0)]
    assert len(heard) == 2 * RATE
    assert heard.dtype == np.int16
    assert ready == {"ready_after": None, "recorded_from": 0.0}
    assert speaker.stopped and microphone.held == 0


def test_it_plays_no_music_unless_given_some():
    speaker = Speaker()
    measure_cut_in.measure(speaker, Microphone(), None, "voice.wav", 0.5, volume=1.0)
    assert [kind for _, _, kind, _ in speaker.played] == ["reply"]


def test_it_says_how_long_the_echo_canceller_took_to_be_ready():
    echo = Echo(ready_after=5)
    microphone = Microphone(on_read=lambda: setattr(echo, "reads", echo.reads + 1))
    _, ready = measure_cut_in.measure(Speaker(), microphone, echo, "voice.wav", 1.0, volume=1.0)
    assert ready["ready_after"] is not None and ready["ready_after"] >= 0
    # The whole sound is recorded, so the canceller's warm-up is in it: score_cut_in.py needs to know.
    assert ready["recorded_from"] == 0.0

    echo = Echo(ready_after=None)
    _, never = measure_cut_in.measure(Speaker(), Microphone(), echo, "voice.wav", 1.0, volume=1.0)
    assert never["ready_after"] is None


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


# Before the echo canceller is ready

def _warming(echo, microphone_reads_to_ready):
    """A microphone which has the echo canceller ready once it has been read this many times."""
    echo.ready_after = microphone_reads_to_ready
    return Microphone(on_read=lambda: setattr(echo, "reads", echo.reads + 1))


def test_with_a_warm_up_nothing_is_recorded_until_the_echo_canceller_is_ready(capsys):
    echo = Echo()
    microphone = _warming(echo, 5)
    heard, ready = measure_cut_in.measure(Speaker(), microphone, echo, "voice.wav", 1.0, volume=1.0, warm_up=True)
    # Five blocks heard and thrown away, then the second asked for.
    assert microphone.reads == 5 + 10
    assert len(heard) == RATE
    assert ready["warmed_up"] is True
    assert ready["ready_after"] == ready["recorded_from"] >= 0


def test_a_warm_up_which_never_ends_gives_up_and_says_so(capsys):
    speaker = Speaker()
    heard, ready = measure_cut_in.measure(speaker, Microphone(), Echo(), "voice.wav", 1.0, volume=1.0,
                                          warm_up=True, warm_up_most=0.05)
    assert len(heard) == 0
    assert ready == {"ready_after": None, "recorded_from": None, "warmed_up": False}
    assert "wasn't ready" in capsys.readouterr().err
    assert speaker.stopped


def test_with_a_warm_up_a_sound_which_ends_first_stops_the_recording_and_says_so(capsys):
    echo = Echo()
    speaker = Speaker()
    microphone = _warming(echo, 2)
    # The sound is over after a second of recording, of the five asked for.
    microphone.on_read = lambda: (setattr(echo, "reads", echo.reads + 1),
                                  setattr(speaker.playbacks[0], "done", microphone.reads >= 12))
    heard, ready = measure_cut_in.measure(speaker, microphone, echo, "voice.wav", 5.0, volume=1.0, warm_up=True)
    assert 0 < len(heard) < 5 * RATE
    assert "sound ended" in capsys.readouterr().err
    assert ready["warmed_up"] is True


def test_without_a_warm_up_a_sound_which_ends_first_still_records_all(capsys):
    speaker = Speaker()
    microphone = Microphone(on_read=lambda: setattr(speaker.playbacks[0], "done", microphone.reads >= 3))
    heard, _ = measure_cut_in.measure(speaker, microphone, None, "voice.wav", 1.0, volume=1.0)
    assert len(heard) == RATE


def test_the_music_plays_through_the_warm_up_too():
    speaker = Speaker()
    measure_cut_in.measure(speaker, Microphone(), Echo(ready_after=0), "voice.wav", 2.0, volume=1.0,
                           music="music.wav", music_volume=0.3, warm_up=True, warm_up_most=7.0)
    assert speaker.played[1][3] == 9.0


def test_the_part_after_the_echo_canceller_was_ready_is_scored_on_its_own():
    class Nemotron:
        def __init__(self):
            self.lengths = []

        def start(self):
            return object()

        def add(self, stream, audio):
            self.lengths.append(len(audio))

        def finish(self, stream):
            return "weather today"

    # Frames 0 to 1 quiet and 2 to 4 speech, with the canceller ready from the third.
    audio = frames(0, 0, 9, 9, 9)
    heard = Nemotron()
    whole = score_cut_in.report(audio, Detector(), heard, "the weather today is dry")
    after = score_cut_in.report(audio, Detector(), heard, "the weather today is dry", start=2 * FRAME / RATE)
    assert whole["speech_share"] == 0.6 and whole["first_speech_at"] == round(2 * FRAME / RATE, 3)
    assert after["speech_share"] == 1.0
    # Still in seconds into the whole recording.
    assert after["first_speech_at"] == whole["first_speech_at"]
    assert heard.lengths == [5 * FRAME, 3 * FRAME]
    assert after["words"] == "weather today" and after["own_words"] is True


def test_the_time_the_echo_canceller_was_ready_comes_from_ready_json(tmp_path):
    assert score_cut_in.ready_from({"ready_after": 6.0, "recorded_from": 0.0}) == 6.0
    # Recorded from when it was ready, it was ready from the start.
    assert score_cut_in.ready_from({"ready_after": 6.0, "recorded_from": 6.0}) == 0.0
    assert score_cut_in.ready_from({"ready_after": None, "recorded_from": 0.0}) is None
    assert score_cut_in.ready_json(str(tmp_path / "heard.wav")) is None
    (tmp_path / "ready.json").write_text(json.dumps({"ready_after": 2.5, "recorded_from": 0.0}))
    assert score_cut_in.ready_json(str(tmp_path / "heard.wav")) == {"ready_after": 2.5, "recorded_from": 0.0}


# The volumes, and where they came from

def test_saved_volumes_are_read_from_the_saved_state(tmp_path):
    path = tmp_path / "satellite_state.json"
    assert measure_cut_in.saved_volumes(str(path)) == {}
    path.write_text("not json")
    assert measure_cut_in.saved_volumes(str(path)) == {}
    path.write_text(json.dumps({"muted": False}))
    assert measure_cut_in.saved_volumes(str(path)) == {}
    path.write_text(json.dumps({"volumes": {"general": 0.4, "music": 0.5}, "muted": False}))
    assert measure_cut_in.saved_volumes(str(path)) == {"general": 0.4, "music": 0.5}


def test_nothing_plays_where_no_volumes_are_saved():
    with pytest.raises(SystemExit) as refused:
        measure_cut_in.chosen_volumes({}, None, False, 0.3, "/somewhere/satellite_state.json")
    assert "/somewhere/satellite_state.json" in str(refused.value)
    with pytest.raises(SystemExit):
        measure_cut_in.chosen_volumes({"general": 0.4}, None, True, 0.3)


def test_the_saved_volumes_are_used_and_the_musics_is_a_share_of_its_own():
    assert measure_cut_in.chosen_volumes({"general": 0.4, "music": 0.5}, None, True, 0.3) == (0.4, pytest.approx(0.15))


def test_a_volume_given_is_used_for_the_voice_and_a_share_of_it_for_the_music():
    assert measure_cut_in.chosen_volumes({}, 0.5, True, 0.3) == (0.5, pytest.approx(0.15))
    assert measure_cut_in.volume_between_0_and_1("0.5") == 0.5
    with pytest.raises(Exception):
        measure_cut_in.volume_between_0_and_1("1.5")


def test_it_says_what_it_found_before_playing():
    said = measure_cut_in.describe("/cfg", "/data", True, False, 0.4, 0.12)
    assert "/cfg" in said and "/data" in said
    assert "push to talk" in said
    assert "Echo canceller: no" in said
    assert "0.40" in said and "0.12" in said
    assert "wake word" in measure_cut_in.describe("/cfg", "/data", False, True, 0.4, 0.12)


# A speaker which didn't open

def test_a_sound_over_as_soon_as_it_starts_is_refused_not_recorded_as_silence():
    speaker, microphone = Speaker(finished=("voice.wav",)), Microphone()
    with pytest.raises(SystemExit) as refused:
        measure_cut_in.measure(speaker, microphone, None, "voice.wav", 1.0, volume=1.0)
    assert "Nothing is playing" in str(refused.value)
    assert microphone.reads == 0
    assert speaker.stopped and microphone.held == 0


def test_music_over_as_soon_as_it_starts_is_refused_too():
    with pytest.raises(SystemExit):
        measure_cut_in.measure(Speaker(finished=("music.wav",)), Microphone(), None, "voice.wav", 1.0,
                               volume=1.0, music="music.wav", music_volume=0.3)


# Running on the brain from standard input

def test_the_constants_written_out_for_running_before_the_tree_is_on_the_path_are_still_right():
    assert score_cut_in.FRAME == recording.FRAME and score_cut_in.RATE == recording.RATE
    assert measure_voice_load.RATE == nemotron.RATE


@pytest.mark.parametrize("script", [score_cut_in, measure_voice_load])
def test_the_tree_to_import_from_is_the_argument_else_the_scripts_own(script, tmp_path):
    assert script.tree_to_import(str(tmp_path), "<stdin>") == str(tmp_path)
    real = os.path.join(SCRIPTS, script.__name__ + ".py")
    assert script.tree_to_import(None, real) == os.path.dirname(os.path.dirname(os.path.abspath(SCRIPTS)))
    with pytest.raises(SystemExit) as refused:
        script.tree_to_import(None, "<stdin>")
    assert "--tree" in str(refused.value)


def _from_stdin(script, *args):
    """Runs a script as the brain's read-only check command does: isolated, its text on standard input."""
    with open(os.path.join(SCRIPTS, script)) as file:
        return subprocess.run([sys.executable, "-I", "-", *args], input=file.read(), capture_output=True, text=True)


def test_run_from_standard_input_without_a_tree_they_say_to_pass_one(tmp_path):
    for script, args in (("score_cut_in.py", ["heard.wav"]), ("measure_voice_load.py", ["--seconds", "1"])):
        ran = _from_stdin(script, *args)
        assert ran.returncode != 0
        assert "--tree" in ran.stderr
        assert "ModuleNotFoundError" not in ran.stderr


def test_run_from_standard_input_with_a_tree_they_get_as_far_as_the_recording(tmp_path):
    wrong = tmp_path / "heard.wav"
    sf.write(str(wrong), np.zeros(800, dtype=np.int16), 8000, subtype="PCM_16")
    ran = _from_stdin("score_cut_in.py", str(wrong), "--tree", measure_cut_in.ROOT)
    assert "16000Hz mono" in ran.stderr
