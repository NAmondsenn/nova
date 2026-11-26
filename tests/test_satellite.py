"""
Satellite Test
Checks the Pi's own program - hearing the wake word, sending what's said, and playing
and stopping what the brain sends - against a fake brain over a socketpair, with
fakes in place of the microphone, speaker and wake word, so nothing is opened or
loaded. Needs numpy installed (as it is on the Pi), and is skipped anywhere it isn't.
"""

import datetime
import json
import logging
import os
import queue
import socket
import threading
import time
import types
import wave
from functools import partial

import pytest

np = pytest.importorskip("numpy")

import satellite
from voice_assistant import fallback, paths, protocol, satellite_link, storage, wake_clips
from voice_assistant.fallback import AlarmCopy, Phrase, StateCopy
from voice_assistant.notes import NoteRefused
from voice_assistant.press import PressListener
from voice_assistant.screen_orb import LINGER
from voice_assistant.shortcut import Shortcut
from voice_assistant.update_install import InstallRefused
from voice_assistant.update_receive import NOT_FROM_THE_BRAIN, WRONG_FORMAT
from voice_assistant.update_state import UpdateState
from voice_assistant.wake_clips import WakeClips
from voice_assistant.windows_media import UNCHANGED

PASSWORD = "the-bedroom-satellites-password"
RATE = 16000
# The fake microphone hears this many times faster than real time, so a 10 second
# recording takes one.
SPEED = 10
PHRASE = "/sounds/cant_reach_server.wav"


def samples(start, end):
    """What the fake microphone hears between two positions: each sample is its own position."""
    return (np.arange(start, end) % 32768).astype(np.int16)


def wait_for(condition, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.005)
    return False


def connected(made):
    """
    Whether the satellite has the brain - not just whether the brain has its hello,
    which comes first. A wake word said in between is sent nowhere, and gets the phrase.
    """
    return made.link is not None and made.link.connected


class FakeBrain:
    """Admits the satellite over a socketpair, and keeps everything it sends but pings."""

    def __init__(self):
        self.up = True
        self.hellos = []
        self.received = []
        self.sockets = []
        self.stream = None
        self.lock = threading.Lock()

    def connect(self, address, timeout=None):
        """Stands in for socket.create_connection."""
        if not self.up:
            raise ConnectionRefusedError("The brain is down")
        ours, theirs = socket.socketpair()
        theirs.settimeout(timeout)
        self.sockets.append(ours)
        threading.Thread(target=self._serve, args=(ours,), daemon=True).start()
        return theirs

    def _serve(self, sock):
        stream = sock.makefile("rwb")
        try:
            hello = protocol.admit(stream, lambda name: PASSWORD)
            self.stream = stream
            self.hellos.append(hello)
            while True:
                message, payload = protocol.receive(stream)
                if message["type"] == "ping":
                    self.say("pong")
                else:
                    self.received.append((message, payload))
        except (OSError, protocol.ProtocolError, protocol.Refused):
            pass

    def say(self, message_type, payload=b"", **fields):
        with self.lock:
            protocol.send(self.stream, message_type, payload, **fields)

    def drop(self):
        """Goes down part way through, and stays down."""
        self.up = False
        self.sockets[-1].shutdown(socket.SHUT_RDWR)

    def kinds(self):
        return [message["type"] for message, _ in self.received]

    def messages(self, kind):
        return [message for message, _ in self.received if message["type"] == kind]

    def recordings(self):
        return [np.frombuffer(payload, np.int16) for message, payload in self.received if message["type"] == "audio"]

    def close(self):
        for sock in self.sockets:
            sock.close()


class FakeReader:
    def __init__(self, microphone, position):
        self.microphone = microphone
        self.position = position

    def read(self, count):
        # The PC's microphone is only open while it's held - see Microphone.hold.
        if not self.microphone.always_open and not self.microphone.holds:
            self.microphone.unheld_reads += 1
        while self.microphone.position < self.position + count:
            time.sleep(0.001)
        heard = samples(self.position, self.position + count)
        self.position += count
        return heard


class FakeMicrophone:
    """Has been listening for a second when it starts, and hears SPEED times faster than real time."""

    def __init__(self, sample_rate=48000, echo=None, always_open=True):
        self.started = time.monotonic()
        self.closed = False
        self.always_open = always_open
        self.holds = 0
        self.unheld_reads = 0

    def hold(self):
        self.holds += 1

    def release(self):
        assert self.holds > 0
        self.holds -= 1

    @property
    def position(self):
        return RATE + int((time.monotonic() - self.started) * RATE * SPEED)

    def listen(self, start=None):
        return FakeReader(self, self.position if start is None else start)

    def close(self):
        self.closed = True


class FakeWake:
    """Hears the wake word whenever the test says it - as long as it's listening for it then."""

    def __init__(self, config=None, microphone=None, playing=None):
        self.microphone = microphone
        # What the satellite says whether it's making a sound of its own with.
        self.playing = playing
        self.muted = False
        self.sensitivity = 0.5
        # A trained check, as the user's Pi has - None for none.
        self.voice_check = "a trained check"
        self.voice_check_threshold = 0.2
        self.heard_at = None
        self.listening = False
        self._said = queue.Queue()

    def set_muted(self, muted):
        self.muted = muted

    def say(self):
        """The user says the wake word. Returns the microphone position it's said at."""
        position = self.microphone.position
        self._said.put(position)
        return position

    def listen_once(self, until=None):
        # As the real one, only what's said from now on can wake it.
        now = self.microphone.position
        self.listening = True
        try:
            while True:
                if until is not None and until():
                    return None
                try:
                    position = self._said.get(timeout=0.005)
                except queue.Empty:
                    continue
                if position >= now and not self.muted:
                    self.heard_at = position
                    return 0.9
        finally:
            self.listening = False


class FakePlayback:
    """Plays until stopped, or until the test - or the end of its stream - finishes it."""

    def __init__(self, kind, volume, paused=False, **details):
        self.kind = kind
        self.volume = volume
        self.paused = paused
        self.stopped = False
        self.position = 0
        self.pieces = []
        self.filename = None
        # The fades it was asked for, as (volume, seconds).
        self.fades = []
        self.__dict__.update(details)
        self._finished = threading.Event()

    def fade_to(self, volume, seconds):
        self.fades.append((volume, seconds))

    @property
    def done(self):
        return self._finished.is_set()

    def wait(self, timeout=None):
        return self._finished.wait(timeout)

    def stop(self):
        self.stopped = True
        self._finished.set()

    def finish(self):
        self._finished.set()


class FakeSpeaker:
    def __init__(self, sample_rate=48000, echo_reference=None, always_open=True):
        self.sample_rate = sample_rate
        self.always_open = always_open
        self.playing = []
        self.silenced = set()
        self.closed = False
        # How many are holding the PC's speaker open - see Speaker.hold.
        self.holds = 0

    def hold(self):
        self.holds += 1

    def release(self):
        assert self.holds > 0
        self.holds -= 1

    def _start(self, playback):
        if playback.kind in self.silenced:
            playback.finish()
        self.playing.append(playback)
        return playback

    def play_file(self, filename, volume=1.0, kind="reply", cache=True, start=0.0, length=None):
        return self._start(FakePlayback(kind, volume, filename=filename, start=start, length=length))

    def play_stream(self, pieces, sample_rate=None, volume=1.0, kind="reply", paused=False, most_ahead=None):
        playback = self._start(FakePlayback(kind, volume, paused, sample_rate=sample_rate))

        def feed():
            for piece in pieces:
                if playback.done:
                    break
                playback.pieces.append(piece)
                playback.position += len(piece)
            pieces.close()
            # Played out as soon as the last of it has arrived.
            playback.finish()

        threading.Thread(target=feed, daemon=True).start()
        return playback

    def sounded_within(self, seconds):
        # Only what's playing now - the real one also counts what stopped up to so many
        # seconds ago, which needs a real output stream.
        self.lingers = seconds
        return any(not playback.done and not playback.paused for playback in self.playing)

    def files(self):
        return [playback.filename for playback in self.playing if playback.filename]

    def streams(self):
        return [playback for playback in self.playing if not playback.filename]

    def stop(self, kinds=None):
        for playback in list(self.playing):
            if (kinds is None or playback.kind in kinds) and not playback.done:
                playback.stop()

    def silence(self, kind):
        self.silenced.add(kind)
        self.stop([kind])

    def allow(self, kind):
        self.silenced.discard(kind)

    def close(self):
        self.stop()
        self.closed = True


class FakePress:
    """Stands in for the press listener, which would take a real port of this machine."""

    def __init__(self, port, on_press, on_open=None):
        self.port = port
        self.on_press = on_press
        self.on_open = on_open
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(satellite_link, "SOCKET_TIMEOUT", 0.05)
    monkeypatch.setattr(satellite_link, "RETRY_FIRST", 0.02)
    monkeypatch.setattr(satellite_link, "RETRY_LONGEST", 0.05)
    monkeypatch.setattr(satellite, "FALLBACK_CHECK", 0.02)
    monkeypatch.setattr(satellite, "PROGRESS_EVERY", 0.02)


@pytest.fixture
def brain(monkeypatch):
    brain = FakeBrain()
    monkeypatch.setattr(satellite_link.socket, "create_connection", brain.connect)
    yield brain
    brain.close()


@pytest.fixture
def make(tmp_path, monkeypatch, make_config):
    monkeypatch.setenv("NOVA_BACKEND_TOKEN", PASSWORD)
    # Never the real data folder: running the satellite leaves a mark there.
    monkeypatch.setattr(satellite, "RUNNING_MARK", str(tmp_path / "running"))
    monkeypatch.setattr(satellite, "MIC_MUTE_FILE", str(tmp_path / "mic_muted_until.json"))
    monkeypatch.setattr(satellite, "UPDATE_FOLDER", str(tmp_path / "update"))
    # Not this checkout's own commit, which would change with every commit and say it
    # updates as a bundle: a satellite which can't update, unless a test says otherwise.
    monkeypatch.setattr(satellite.code_version, "running_version", lambda: None)
    monkeypatch.setattr(satellite.code_version, "update_format", lambda: None)
    monkeypatch.setattr(satellite, "Microphone", FakeMicrophone)
    monkeypatch.setattr(satellite, "Speaker", FakeSpeaker)
    monkeypatch.setattr(satellite, "WakeWordDetector", FakeWake)
    monkeypatch.setattr(satellite, "PressListener", FakePress)
    monkeypatch.setattr(satellite, "make_echo_canceller", lambda config, rate, engaged=True: None)
    # A Pi, as most of these tests are of one, and never the real machine's Downloads folder.
    monkeypatch.setattr(satellite, "machine_kind", lambda: "pi")
    monkeypatch.setattr(satellite, "downloads_folder", lambda: None)
    monkeypatch.setattr(satellite, "AlarmCopy", partial(AlarmCopy, str(tmp_path / "alarm_copy.json")))
    monkeypatch.setattr(satellite, "StateCopy", partial(StateCopy, str(tmp_path / "state.json")))
    monkeypatch.setattr(satellite, "Phrase", partial(Phrase, str(tmp_path / "phrase.wav"), PHRASE))
    monkeypatch.setattr(satellite, "WakeClips", partial(WakeClips, folder=str(tmp_path / "clips")))
    running = []

    def begin(made):
        def run():
            try:
                made.run()
            except SystemExit as e:
                made.exited.append(e.code)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        running.append((made, thread))

    def make(name="bedroom", start=True, tray=None, **sections):
        config = make_config(audio={"sample_rate": 48000, "pre_roll": 0.3},
                             backend={"address": "nova:10400", "satellite": name}, **sections)
        made = satellite.Satellite(config, tray=tray)
        made.latency_file = str(tmp_path / "latency.jsonl")
        # Filled with the exit code once run() ends.
        made.exited = []
        if start:
            begin(made)
        return made

    make.begin = begin
    yield make
    for made, thread in running:
        made.stop()
        thread.join(timeout=5)
    # One which didn't stop would ring, connect and log on into the tests after it.
    assert not [made for made, thread in running if thread.is_alive()], "A satellite didn't stop"


def entry(due):
    return {"id": "alarm-1@1729587600", "due": due, "kind": "alarm", "sound": "alarm_sound.wav"}


def test_the_wake_word_sends_the_pre_roll_then_the_recording(brain, make, tmp_path):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    heard_at = made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 3)
    brain.say("stop_listening")

    # Saying how much of the recording is from before the wake word - see audio.pre_roll.
    assert brain.messages("wake") == [{"type": "wake", "talked_over": False, "pre_roll": 4800, "recording": "wake-1"}]
    assert brain.kinds().index("wake") < brain.kinds().index("audio")
    pre_roll, first, second = brain.recordings()[:3]
    assert np.array_equal(pre_roll, samples(heard_at - 4800, heard_at))
    assert np.array_equal(first, samples(heard_at, heard_at + 1600))
    assert np.array_equal(second, samples(heard_at + 1600, heard_at + 3200))

    # Timed from the wake word to the reply's first sound.
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=1.0, kind="reply", paused=False)
    brain.say("stream_audio", np.array([1, 2, 3], np.int16).tobytes(), id="reply-1")
    latency = tmp_path / "latency.jsonl"
    assert wait_for(lambda: saved_whole(latency))
    (line,) = [json.loads(text) for text in latency.read_text().splitlines()]
    assert set(line["marks_ms"]) == {"wake", "reply_audio"}
    assert line["marks_ms"]["wake"] <= line["marks_ms"]["reply_audio"]


def latency_lines(tmp_path):
    return [json.loads(text) for text in (tmp_path / "latency.jsonl").read_text().splitlines()]


def saved_whole(path):
    """
    Whether a wake's timing is in its file whole - which is there a moment before its line is,
    so a test which read it as soon as it was there could find it empty.
    """
    return path.exists() and path.read_text().endswith("\n")


def test_a_reply_is_timed_from_when_it_starts_to_play(brain, make, tmp_path):
    # Not from when its first audio arrives, which can be a while before the speaker
    # plays it.
    made = make()
    let_play = threading.Event()
    play_stream = made.speaker.play_stream

    def play_stream_once_let(pieces, **details):
        def held():
            let_play.wait(5)
            yield from pieces
        return play_stream(held(), **details)

    made.speaker.play_stream = play_stream_once_let
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=1.0, kind="reply", paused=False)
    brain.say("stream_audio", np.array([1, 2, 3], np.int16).tobytes(), id="reply-1")
    try:
        # Arrived, and waiting for the speaker.
        assert wait_for(lambda: "reply-1" in made._playing and made._playing["reply-1"].pieces.qsize())
        assert not (tmp_path / "latency.jsonl").exists()
    finally:
        let_play.set()

    assert wait_for(lambda: saved_whole(tmp_path / "latency.jsonl"))
    (line,) = latency_lines(tmp_path)
    assert set(line["marks_ms"]) == {"wake", "reply_audio"}


class _HeardAsTheNextWakeComes:
    """A reply which starts to play as the wake word is said again, while its start is looked for."""

    def __init__(self, made):
        self.made = made
        self.position = 0

    def wait(self, timeout=None):
        with self.made._lock:
            self.made._start_timeline()
        self.position = 1024
        return False


def test_a_reply_starting_as_the_next_wake_comes_doesnt_time_that_wake(make, tmp_path):
    # It marked whatever wake was being timed once it started, so the next wake was given
    # the last one's reply - in latency.jsonl, as an answer far quicker than any.
    made = make(start=False)
    with made._lock:
        made._start_timeline()
    made._time_reply(_HeardAsTheNextWakeComes(made))

    # The last wake's timing was saved as the next began, without the reply.
    (line,) = latency_lines(tmp_path)
    assert set(line["marks_ms"]) == {"wake"}
    with made._lock:
        assert made._timeline is not None and set(made._timeline.marks) == {"wake"}


def test_a_wake_with_no_reply_is_timed_as_its_turn_ends(brain, make, tmp_path):
    # Rather than only at the next wake, which may be hours later.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")
    brain.say("allow", kind="announcement")

    assert wait_for(lambda: saved_whole(tmp_path / "latency.jsonl"), seconds=2)
    (line,) = latency_lines(tmp_path)
    assert set(line["marks_ms"]) == {"wake"}


def test_the_end_of_the_turn_before_leaves_a_wake_still_being_heard_untimed(brain, make, tmp_path):
    # The brain ended its last turn just as the wake word was heard, so that turn's end
    # arrives while this wake is being heard - and this wake's reply is still to come.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("allow", kind="announcement")
    assert wait_for(lambda: "announcement" not in made.speaker.silenced)
    brain.say("stop_listening")
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=1.0, kind="reply", paused=False)
    brain.say("stream_audio", np.array([1, 2, 3], np.int16).tobytes(), id="reply-1")

    assert wait_for(lambda: saved_whole(tmp_path / "latency.jsonl"))
    (line,) = latency_lines(tmp_path)
    assert set(line["marks_ms"]) == {"wake", "reply_audio"}


def test_the_recording_stops_when_the_brain_says(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 3)
    brain.say("stop_listening")

    assert wait_for(lambda: made.wake.listening)
    sent = len(brain.recordings())
    time.sleep(0.1)
    assert len(brain.recordings()) == sent
    assert sum(len(piece) for piece in brain.recordings()) < 10 * RATE
    assert brain.messages("audio_end") == []


def test_the_satellite_records_for_as_long_as_the_brain_says(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("state", volumes={}, muted=False, longest_request=15)
    assert wait_for(lambda: made.longest_request == 15)
    made.wake.say()
    assert wait_for(lambda: brain.messages("audio_end"), seconds=10)

    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "limit"}]
    assert sum(len(piece) for piece in brain.recordings()) == 15 * RATE
    # Each message still carries no more than the protocol allows.
    assert all(len(payload) <= protocol.MAX_AUDIO for message, payload in brain.received)


def test_the_satellites_own_most_is_thirty_seconds(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("state", volumes={}, muted=False, longest_request=90)
    assert wait_for(lambda: made.longest_request == 30)
    made.wake.say()
    assert wait_for(lambda: brain.messages("audio_end"), seconds=15)

    assert sum(len(piece) for piece in brain.recordings()) == 30 * RATE


@pytest.mark.parametrize("sent", [{}, {"longest_request": "15"}, {"longest_request": True},
                                  {"longest_request": float("nan")}, {"longest_request": float("inf")},
                                  {"longest_request": 0}, {"longest_request": 1e-9}, {"longest_request": 0.5},
                                  {"longest_request": -5}, {"longest_request": None}],
                         ids=["none", "text", "bool", "nan", "inf", "zero", "tiny", "half", "negative", "null"])
def test_an_old_brain_leaves_the_satellite_at_ten_seconds(brain, make, sent):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("state", volumes={"general": 0.5}, muted=True, **sent)
    assert wait_for(lambda: made.wake.muted is True)

    assert made.longest_request == 10
    # And a good one afterwards still counts, so the bad one didn't wedge the state.
    brain.say("state", volumes={}, muted=False, longest_request=12)
    assert wait_for(lambda: made.longest_request == 12)
    brain.say("state", volumes={}, muted=False, **sent)
    assert wait_for(lambda: made.wake.muted is False)
    assert made.longest_request == 12


def test_a_longest_request_which_makes_no_sense_is_logged_once(brain, make, caplog):
    # The brain repeats its state at every change of volume, and the log shouldn't fill
    # with the same value.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    with caplog.at_level("WARNING", logger="satellite"):
        brain.say("state", volumes={}, muted=False, longest_request="15")
        brain.say("state", volumes={}, muted=True, longest_request="15")
        assert wait_for(lambda: made.wake.muted is True)
        # An old brain, which doesn't send it, is nothing to say.
        brain.say("state", volumes={}, muted=False)
        assert wait_for(lambda: made.wake.muted is False)
        brain.say("state", volumes={}, muted=True, longest_request=float("nan"))
        assert wait_for(lambda: made.wake.muted is True)
        brain.say("state", volumes={}, muted=False, longest_request=float("nan"))
        assert wait_for(lambda: made.wake.muted is False)

    said = [record.getMessage() for record in caplog.records if "longest_request" in record.getMessage()]
    assert len(said) == 2
    assert "'15'" in said[0] and "nan" in said[1]
    assert made.longest_request == 10


def test_the_satellite_listens_as_surely_as_the_brain_says(brain, make, caplog):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    assert made.wake.sensitivity == 0.5
    with caplog.at_level("INFO", logger="satellite"):
        brain.say("state", volumes={}, muted=False, wake_confidence=0.75)
        assert wait_for(lambda: made.wake.sensitivity == 0.75)
        # The brain repeats its state often, and the log shouldn't fill with the same value.
        brain.say("state", volumes={}, muted=False, wake_confidence=0.75)
        brain.say("state", volumes={}, muted=True, wake_confidence=0.75)
        assert wait_for(lambda: made.wake.muted is True)
    assert len([record for record in caplog.records
                if "Listening for the wake word at 0.75 now" in record.getMessage()]) == 1

    # A state without it changes nothing: an old brain.
    brain.say("state", volumes={}, muted=False)
    assert wait_for(lambda: made.wake.muted is False)
    assert made.wake.sensitivity == 0.75


@pytest.mark.parametrize("edge", [0.05, 0.99])
def test_a_wake_confidence_at_either_end_of_the_range_is_taken(brain, make, edge):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("state", volumes={}, muted=False, wake_confidence=edge)
    assert wait_for(lambda: made.wake.sensitivity == edge)


@pytest.mark.parametrize("sent", [{"wake_confidence": "0.75"}, {"wake_confidence": True},
                                  {"wake_confidence": float("nan")}, {"wake_confidence": float("inf")},
                                  {"wake_confidence": 0}, {"wake_confidence": -0.1}, {"wake_confidence": 1.5},
                                  {"wake_confidence": None}, {"wake_confidence": 0.04}, {"wake_confidence": 1}],
                         ids=["text", "bool", "nan", "inf", "zero", "negative", "over-one", "null", "below-0.05",
                              "one"])
def test_a_wake_confidence_which_makes_no_sense_is_ignored(brain, make, sent):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("state", volumes={}, muted=False, wake_confidence=0.75)
    assert wait_for(lambda: made.wake.sensitivity == 0.75)
    brain.say("state", volumes={}, muted=True, **sent)
    assert wait_for(lambda: made.wake.muted is True)

    assert made.wake.sensitivity == 0.75
    # And a good one afterwards still counts, so the bad one didn't wedge the state.
    brain.say("state", volumes={}, muted=False, wake_confidence=0.6)
    assert wait_for(lambda: made.wake.sensitivity == 0.6)


def test_the_satellite_checks_the_voice_as_surely_as_the_brain_says(brain, make, caplog):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    assert made.wake.voice_check_threshold == 0.2
    with caplog.at_level("INFO", logger="satellite"):
        brain.say("state", volumes={}, muted=False, voice_check=0.35)
        assert wait_for(lambda: made.wake.voice_check_threshold == 0.35)
        # The brain repeats its state often, and the log shouldn't fill with the same value.
        brain.say("state", volumes={}, muted=False, voice_check=0.35)
        brain.say("state", volumes={}, muted=True, voice_check=0.35)
        assert wait_for(lambda: made.wake.muted is True)
    assert len([record for record in caplog.records
                if "Checking it's your voice at 0.35 now" in record.getMessage()]) == 1

    # A state without it changes nothing: an old brain.
    brain.say("state", volumes={}, muted=False)
    assert wait_for(lambda: made.wake.muted is False)
    assert made.wake.voice_check_threshold == 0.35


@pytest.mark.parametrize("edge", [0, 0.0, 0.99, 0.5], ids=["int-zero", "zero", "top", "half"])
def test_a_voice_check_from_0_to_0_99_is_taken(brain, make, edge):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("state", volumes={}, muted=False, voice_check=edge)
    assert wait_for(lambda: made.wake.voice_check_threshold == edge)


def test_a_voice_check_of_0_says_its_off(brain, make, caplog):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    with caplog.at_level("INFO", logger="satellite"):
        brain.say("state", volumes={}, muted=False, voice_check=0)
        assert wait_for(lambda: made.wake.voice_check_threshold == 0)
    assert any("Not checking it's your voice now" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("sent", [{"voice_check": "0.3"}, {"voice_check": True}, {"voice_check": False},
                                  {"voice_check": float("nan")}, {"voice_check": float("inf")},
                                  {"voice_check": -0.1}, {"voice_check": 1}, {"voice_check": 1.5},
                                  {"voice_check": None}],
                         ids=["text", "true", "false", "nan", "inf", "negative", "one", "over-one", "null"])
def test_a_voice_check_which_makes_no_sense_is_ignored(brain, make, sent):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("state", volumes={}, muted=False, voice_check=0.4)
    assert wait_for(lambda: made.wake.voice_check_threshold == 0.4)
    brain.say("state", volumes={}, muted=True, **sent)
    assert wait_for(lambda: made.wake.muted is True)

    assert made.wake.voice_check_threshold == 0.4
    # And a good one afterwards still counts, so the bad one didn't wedge the state.
    brain.say("state", volumes={}, muted=False, voice_check=0.3)
    assert wait_for(lambda: made.wake.voice_check_threshold == 0.3)


def test_before_a_check_is_trained_its_setting_says_so(brain, make, caplog):
    # Not that a check is in force, which would mislead the first look at the log.
    made = make()
    made.wake.voice_check = None
    assert wait_for(lambda: connected(made) and made.wake.listening)
    with caplog.at_level("INFO", logger="satellite"):
        brain.say("state", volumes={}, muted=False, voice_check=0.35)
        assert wait_for(lambda: made.wake.voice_check_threshold == 0.35)
    said = [record.getMessage() for record in caplog.records if "your voice" in record.getMessage()]
    assert said == ["Checking it's your voice at 0.35 once a voice check's been trained"]


def test_the_voice_check_setting_lasts_through_a_restart_while_the_brain_is_away(brain, make, tmp_path):
    # 0 is the way out if the check stops the user, and the brain being away is when the
    # wake word alone dismisses an alarm.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("state", volumes={}, muted=False, voice_check=0)
    assert wait_for(lambda: made.wake.voice_check_threshold == 0)
    assert StateCopy(str(tmp_path / "state.json")).voice_check == 0

    brain.up = False
    restarted = make(start=False)
    assert restarted.wake.voice_check_threshold == 0


def test_whats_playing_is_told_to_the_wake_word(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    assert made.wake.playing() is False
    # Over the voice check's window, and a little more, after it has stopped.
    assert made.speaker.lingers == satellite.OWN_SOUND_LINGERS

    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=1.0, kind="reply", paused=False)
    assert wait_for(lambda: made.speaker.streams())
    reply = made.speaker.streams()[0]
    assert made.wake.playing() is True
    reply.finish()
    assert made.wake.playing() is False

    # Paused music makes no sound.
    brain.say("play_stream", id="song-1", rate=44100, channels=2, volume=1.0, kind="music", paused=True)
    assert wait_for(lambda: len(made.speaker.streams()) == 2)
    assert made.wake.playing() is False


def test_an_alarm_ringing_from_the_copy_counts_as_its_own_sound(brain, make, tmp_path):
    # With the brain away, the wake word is the only way to dismiss it - so it's known
    # from the ringer itself, as well as from the speaker.
    brain.up = False
    AlarmCopy(str(tmp_path / "alarm_copy.json")).replace([entry(time.time() - 1)])
    made = make()
    assert wait_for(lambda: made.ringer.ringing)
    made.speaker.sounded_within = lambda seconds: False
    assert made.wake.playing() is True


def test_whats_playing_is_told_without_waiting_for_the_satellites_lock(brain, make):
    # The lock is held while the brain is sent to, which can take seconds - and the
    # wake word is asked on the listening thread, with the user waiting.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    told = []
    with made._lock:
        asking = threading.Thread(target=lambda: told.append(made.wake.playing()), daemon=True)
        asking.start()
        asking.join(timeout=1)
        assert told == [False]


def test_an_old_satellite_ignores_the_longest_request(tmp_path):
    copy = StateCopy(str(tmp_path / "state.json"))
    copy.update({"volumes": {"general": 0.5}, "muted": True, "longest_request": 20})

    assert copy.volume("general") == 0.5 and copy.muted is True
    assert StateCopy(str(tmp_path / "state.json")).volume("general") == 0.5


def test_the_recording_stops_after_10_seconds_until_the_brain_says_otherwise(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: brain.messages("audio_end"))

    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "limit"}]
    assert sum(len(piece) for piece in brain.recordings()) == 10 * RATE
    assert wait_for(lambda: made.wake.listening)
    time.sleep(0.1)
    assert brain.kinds()[-1] == "audio_end"


def test_a_satellite_stopped_part_way_through_a_recording_tells_the_brain(brain, make):
    # So the brain isn't left waiting for the rest.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    made.stop()

    assert wait_for(lambda: made.exited and brain.messages("audio_end"))
    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "cancelled"}]


def test_a_dropped_connection_mid_recording_says_the_phrase(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.drop()

    assert wait_for(lambda: PHRASE in made.speaker.files())
    (phrase,) = [playback for playback in made.speaker.playing if playback.filename == PHRASE]
    assert phrase.kind == "reply"
    # Nothing else will let the alarms ring again now.
    assert "announcement" not in made.speaker.silenced
    assert wait_for(lambda: made.wake.listening)


def noting_when_the_brain_is_forgotten(made):
    """An Event set once the satellite has done all it does on losing the brain."""
    forgotten = threading.Event()
    forget = made._forget_the_brain

    def forget_and_note():
        forget()
        forgotten.set()

    made._forget_the_brain = forget_and_note
    return forgotten


def test_losing_the_brain_stops_what_it_was_playing(brain, make):
    # Nothing is left to finish or stop it - and a stream would wait for more for good.
    made = make()
    forgotten = noting_when_the_brain_is_forgotten(made)
    assert wait_for(lambda: connected(made))
    brain.say("play_stream", id="music-1", rate=44100, channels=2, volume=0.5, kind="music", paused=False)
    brain.say("play_sound", id="chime-1", name="listening.wav", volume=0.4, kind="reply", start=0.0, length=None)
    assert wait_for(lambda: made.speaker.streams() and made.speaker.files())
    (music,) = made.speaker.streams()
    (chime,) = [playback for playback in made.speaker.playing if playback.filename]
    brain.drop()

    assert forgotten.wait(5)
    assert music.stopped and chime.stopped
    assert made._playing == {}


def test_losing_the_brain_after_the_recording_says_the_phrase(brain, make):
    # The brain had heard the user, and went before it answered - which was silence.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")
    assert wait_for(lambda: made.wake.listening)
    brain.drop()

    assert wait_for(lambda: PHRASE in made.speaker.files())
    assert "announcement" not in made.speaker.silenced


def test_losing_the_brain_once_it_has_answered_says_nothing(brain, make):
    made = make()
    forgotten = noting_when_the_brain_is_forgotten(made)
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")
    # The end of the turn, which lets back what the wake word silenced.
    brain.say("allow", kind="announcement")
    assert wait_for(lambda: made.wake.listening and "announcement" not in made.speaker.silenced)
    brain.drop()

    assert forgotten.wait(5)
    assert PHRASE not in made.speaker.files()


def test_a_turn_the_brain_ended_as_the_recording_hit_its_limit_stays_ended(brain, make):
    # The brain says the user has stopped, and ends its turn with nothing to say, all
    # while the last piece before the limit is being sent - so the recording's own end
    # comes after the brain's.
    made = make()
    forgotten = noting_when_the_brain_is_forgotten(made)
    assert wait_for(lambda: connected(made) and made.wake.listening)
    send = made.link.send
    limit = round(made.longest_request * RATE)
    sent = 0

    def send_ending_the_turn_first(kind, payload=b"", /, **fields):
        nonlocal sent
        if kind == "audio":
            sent += len(payload) // 2
            if sent >= limit:
                brain.say("stop_listening")
                brain.say("allow", kind="announcement")
                assert wait_for(lambda: "announcement" not in made.speaker.silenced)
        return send(kind, payload, **fields)

    made.link.send = send_ending_the_turn_first
    made.wake.say()
    assert wait_for(lambda: brain.messages("audio_end"))
    # Ended at the limit, so it's that branch which left the brain's turn alone.
    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "limit"}]
    assert wait_for(lambda: made.wake.listening)
    brain.drop()

    assert forgotten.wait(5)
    assert PHRASE not in made.speaker.files()


def test_a_recording_cut_off_at_its_limit_waits_for_the_brains_answer(brain, make):
    # Nobody said the user had stopped, so the turn is the satellite's to open - and the
    # brain going before it answers is silence, which gets the phrase.
    made = make()
    forgotten = noting_when_the_brain_is_forgotten(made)
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: brain.messages("audio_end"))
    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "limit"}]
    assert wait_for(lambda: made.wake.listening)
    brain.drop()

    assert forgotten.wait(5)
    assert PHRASE in made.speaker.files()


def test_losing_the_brain_during_a_follow_up_says_the_phrase_once(brain, make):
    made = make()
    forgotten = noting_when_the_brain_is_forgotten(made)
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")
    assert wait_for(lambda: made.wake.listening)
    # A question, and the answer to it on its way.
    sent = len(brain.recordings())
    brain.say("listen")
    assert wait_for(lambda: len(brain.recordings()) >= sent + 2)
    brain.drop()

    assert forgotten.wait(5)
    assert wait_for(lambda: PHRASE in made.speaker.files() and made.wake.listening)
    assert made.speaker.files().count(PHRASE) == 1


def test_the_wake_word_over_a_reply_stops_it_and_says_so(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("play_stream", id="music-1", rate=44100, channels=2, volume=0.5, kind="music", paused=False)
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=0.8, kind="reply", paused=False)
    brain.say("stream_audio", np.array([1, 2, 3], np.int16).tobytes(), id="reply-1")
    assert wait_for(lambda: len(made.speaker.streams()) == 2 and made.speaker.streams()[1].pieces)
    music, reply = made.speaker.streams()

    made.wake.say()
    assert wait_for(lambda: brain.messages("played"))
    assert reply.stopped
    assert music.paused and not music.done
    assert "announcement" in made.speaker.silenced
    assert brain.messages("wake") == [{"type": "wake", "talked_over": True, "pre_roll": 4800, "recording": "wake-1"}]
    # The brain hears of the wake before the end of the reply it stopped.
    assert brain.kinds().index("wake") < brain.kinds().index("played")
    assert brain.messages("played") == [{"type": "played", "id": "reply-1", "stopped": True, "position": 3}]


def test_the_wake_word_stops_whats_playing_without_waiting_for_the_satellites_lock(brain, make):
    # The lock is held while the brain is sent to, which takes up to 2 seconds on a full
    # socket - and the reply, what rang and the music went on all that while.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("play_stream", id="music-1", rate=44100, channels=2, volume=0.5, kind="music", paused=False)
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=0.8, kind="reply", paused=False)
    brain.say("stream_audio", np.array([1, 2, 3], np.int16).tobytes(), id="reply-1")
    brain.say("play_sound", id="alarm-1", name="alarm_sound.wav", volume=1.0, kind="announcement",
              start=0.0, length=None)
    assert wait_for(lambda: len(made.speaker.streams()) == 2 and made.speaker.streams()[1].pieces
                    and made.speaker.files())
    music, reply = made.speaker.streams()
    (alarm,) = [playback for playback in made.speaker.playing if playback.filename]

    with made._lock:
        made.wake.say()
        assert wait_for(lambda: reply.stopped and alarm.stopped and music.paused, seconds=1)
        assert "announcement" in made.speaker.silenced

    # The brain still hears of the wake before the end of what it stopped.
    assert wait_for(lambda: len(brain.messages("played")) == 2)
    assert brain.messages("wake") == [{"type": "wake", "talked_over": True, "pre_roll": 4800, "recording": "wake-1"}]
    assert brain.kinds().index("wake") < brain.kinds().index("played")
    brain.say("stop_listening")


def test_what_the_wake_word_stops_is_told_after_the_wake_however_long_stopping_takes(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=0.8, kind="reply", paused=False)
    brain.say("stream_audio", np.array([1, 2, 3], np.int16).tobytes(), id="reply-1")
    assert wait_for(lambda: made.speaker.streams() and made.speaker.streams()[0].pieces)
    silence = made.speaker.silence

    def slowly(kind):
        silence(kind)
        # Long enough for the end of the reply, stopped just before, to be told first,
        # were nothing holding it back.
        time.sleep(0.2)

    made.speaker.silence = slowly
    made.wake.say()

    assert wait_for(lambda: brain.messages("played"))
    assert brain.kinds().index("wake") < brain.kinds().index("played")
    brain.say("stop_listening")


def test_the_brain_asking_to_hear_an_answer_during_a_wake_sends_nothing_more(brain, make):
    # The brain finished asking a question as the wake word was said, so asked to hear
    # the answer while the wake's recording was being sent. That recording is all it
    # gets: sent again as a follow-up, it was 10 seconds of nobody talking, while the
    # wake word went unheard.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 3)
    brain.say("listen")
    brain.say("stop_listening")

    assert wait_for(lambda: made.wake.listening)
    sent = len(brain.recordings())
    time.sleep(0.1)
    assert len(brain.recordings()) == sent
    assert brain.messages("audio_end") == []


@pytest.mark.parametrize("stopped", ["listen-1", "wake-1"])
def test_a_wake_being_sent_as_the_brain_asks_for_an_answer_stops_as_either(brain, make, stopped):
    # Its recording is the answer, as above, so the brain stops it as the answer it asked
    # for, if that was listened to - or as the wake's, if the wake was.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 3)
    brain.say("listen", recording="listen-1")
    brain.say("stop_listening", recording=stopped)

    assert wait_for(lambda: made.wake.listening)
    sent = len(brain.recordings())
    time.sleep(0.1)
    assert len(brain.recordings()) == sent
    assert brain.messages("audio_end") == []


def test_a_stop_meant_for_a_recording_which_ran_out_leaves_the_next_one_going(brain, make):
    # The brain stopped listening to the first just as it reached its limit, and the
    # wake word was said again while that was on its way.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: brain.messages("audio_end"))
    # Longer, so the second can't reach its limit before the brain stops it.
    brain.say("state", volumes={}, muted=False, longest_request=30)
    assert wait_for(lambda: made.longest_request == 30 and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.messages("wake")) == 2)
    sent = len(brain.recordings())
    brain.say("stop_listening", recording="wake-1")

    assert wait_for(lambda: len(brain.recordings()) >= sent + 5)
    assert not made.wake.listening
    brain.say("stop_listening", recording="wake-2")
    assert wait_for(lambda: made.wake.listening)
    assert len(brain.messages("audio_end")) == 1
    assert [wake["recording"] for wake in brain.messages("wake")] == ["wake-1", "wake-2"]


def test_a_follow_up_is_known_by_the_number_the_brain_gave_it(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("listen", recording="listen-4")
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    # Meant for another recording, which has ended.
    brain.say("stop_listening", recording="wake-9")
    sent = len(brain.recordings())
    assert wait_for(lambda: len(brain.recordings()) >= sent + 3)

    brain.say("stop_listening", recording="listen-4")
    assert wait_for(lambda: made.wake.listening)
    assert brain.messages("audio_end") == []


def test_a_brain_which_doesnt_say_which_recording_stops_whichever_is_being_sent(brain, make):
    # Today's brain, whose listen and stop_listening carry no number.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")
    assert wait_for(lambda: made.wake.listening)

    sent = len(brain.recordings())
    brain.say("listen")
    assert wait_for(lambda: len(brain.recordings()) >= sent + 2)
    brain.say("stop_listening")
    assert wait_for(lambda: made.wake.listening)
    assert brain.messages("audio_end") == []


def test_a_recording_numbered_other_than_in_words_is_taken_as_unnumbered(brain, make):
    # A brain whose number isn't words is treated as one which doesn't number them, so
    # its stop_listening stops whichever recording is being sent rather than none.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening", recording=7)
    assert wait_for(lambda: made.wake.listening)

    sent = len(brain.recordings())
    brain.say("listen", recording=7)
    assert wait_for(lambda: len(brain.recordings()) >= sent + 2)
    brain.say("stop_listening", recording=7)
    assert wait_for(lambda: made.wake.listening)
    assert brain.messages("audio_end") == []


def test_a_wake_while_streaming_starts_nothing_new(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    heard_at = made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    assert not made.wake.listening
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 5)
    brain.say("stop_listening")
    assert wait_for(lambda: made.wake.listening)
    time.sleep(0.1)

    assert len(brain.messages("wake")) == 1
    # One recording, unbroken.
    recorded = np.concatenate(brain.recordings())
    assert np.array_equal(recorded, samples(heard_at - 4800, heard_at - 4800 + len(recorded)))


def test_a_stream_from_the_brain_plays_and_is_reported_done(brain, make):
    made = make()
    assert wait_for(lambda: brain.hellos)
    stereo = np.array([[1000, -1000], [2000, -2000], [3000, -3000], [4000, -4000]], np.int16)
    brain.say("play_stream", id="reply-1", rate=22050, channels=2, volume=0.5, kind="reply", paused=False)
    brain.say("stream_audio", stereo[:2].tobytes(), id="reply-1")
    brain.say("stream_audio", stereo[2:].tobytes(), id="reply-1")
    assert wait_for(lambda: made.speaker.streams() and len(made.speaker.streams()[0].pieces) == 2)
    reply = made.speaker.streams()[0]
    assert (reply.kind, reply.volume, reply.sample_rate, reply.paused) == ("reply", 0.5, 22050, False)
    assert reply.pieces[0].dtype == np.float32 and reply.pieces[0].shape == (2, 2)
    assert np.allclose(np.concatenate(reply.pieces), stereo / 32768)

    # It plays on until the brain says that's the end of it.
    time.sleep(0.05)
    assert not reply.done and brain.messages("played") == []
    brain.say("stream_end", id="reply-1")
    assert wait_for(lambda: brain.messages("played"))
    assert brain.messages("played") == [{"type": "played", "id": "reply-1", "stopped": False, "position": 4}]

    # A sound, by name, from the satellite's own.
    brain.say("play_sound", id="chime-1", name="listening.wav", volume=0.4, kind="reply", start=0.0, length=None)
    assert wait_for(lambda: made.speaker.files())
    (chime,) = [playback for playback in made.speaker.playing if playback.filename]
    assert (chime.filename, chime.volume) == (os.path.join(paths.SOUNDS_DIR, "listening.wav"), 0.4)
    chime.finish()
    assert wait_for(lambda: len(brain.messages("played")) == 2)
    assert brain.messages("played")[1] == {"type": "played", "id": "chime-1", "stopped": False, "position": 0}

    # Music says how far it's got as it plays, and is paused, turned down and stopped
    # as the brain says.
    brain.say("play_stream", id="music-1", rate=44100, channels=2, volume=0.6, kind="music", paused=False)
    assert wait_for(lambda: len(brain.messages("progress")) >= 2)
    assert brain.messages("progress")[0] == {"type": "progress", "id": "music-1", "position": 0}
    music = made.speaker.streams()[1]
    brain.say("set_playback", id="music-1", paused=True, volume=0.2)
    assert wait_for(lambda: music.paused and music.volume == 0.2)
    brain.say("stop", kinds=["music"])
    assert wait_for(lambda: len(brain.messages("played")) == 3)
    assert brain.messages("played")[2] == {"type": "played", "id": "music-1", "stopped": True, "position": 0}

    # One which has ended is the satellite's to forget - the brain's word on it is ignored.
    brain.say("set_playback", id="music-1", paused=False)
    time.sleep(0.05)
    assert music.paused


def test_set_playback_without_a_fade_jumps_as_before(brain, make):
    made = make()
    assert wait_for(lambda: brain.hellos)
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=1.0, kind="reply", paused=False)
    assert wait_for(lambda: made.speaker.streams())
    (reply,) = made.speaker.streams()

    brain.say("set_playback", id="reply-1", volume=0.3)
    assert wait_for(lambda: reply.volume == 0.3)
    assert reply.fades == []

    # A fade which isn't a number is as good as none, and a fade is only to a volume.
    for fade in ("slow", True, None, float("inf")):
        brain.say("set_playback", id="reply-1", volume=0.6, fade=fade)
        assert wait_for(lambda: reply.volume == 0.6)
        brain.say("set_playback", id="reply-1", volume=0.2)
        assert wait_for(lambda: reply.volume == 0.2)
    assert reply.fades == []

    brain.say("set_playback", id="reply-1", volume=0.9, fade=0.05)
    assert wait_for(lambda: reply.fades)
    assert reply.fades == [(0.9, 0.05)]
    assert reply.volume == 0.2


def test_the_brain_can_stop_one_playback_and_leave_the_rest_of_its_kind(brain, make):
    made = make()
    assert wait_for(lambda: brain.hellos)
    brain.say("play_sound", id="beep-1", name="volume_beep.wav", volume=0.5, kind="reply", start=0.0, length=None)
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=1.0, kind="reply", paused=False)
    assert wait_for(lambda: made.speaker.files() and made.speaker.streams())
    (beep,) = [playback for playback in made.speaker.playing if playback.filename]
    (reply,) = made.speaker.streams()

    brain.say("stop", id="beep-1")
    assert wait_for(lambda: brain.messages("played"))
    assert brain.messages("played") == [{"type": "played", "id": "beep-1", "stopped": True, "position": 0}]
    assert beep.stopped
    assert not reply.done

    # One which has already ended, or never played, is left as it is.
    brain.say("stop", id="beep-1")
    brain.say("stop", id="never-played")
    time.sleep(0.05)
    assert not reply.done
    assert len(brain.messages("played")) == 1


def test_the_brain_can_silence_a_kind_of_sound_and_let_it_back(brain, make):
    made = make()
    assert wait_for(lambda: connected(made))
    brain.say("play_sound", id="alarm-1", name="alarm_sound.wav", volume=1.0, kind="announcement",
              start=0.0, length=None)
    assert wait_for(lambda: made.speaker.files())
    (alarm,) = [playback for playback in made.speaker.playing if playback.filename]

    brain.say("silence", kind="announcement")
    assert wait_for(lambda: alarm.stopped and "announcement" in made.speaker.silenced)
    brain.say("allow", kind="announcement")
    assert wait_for(lambda: "announcement" not in made.speaker.silenced)


def test_a_sound_which_cant_be_played_is_told_played_at_once(brain, make):
    # So the brain doesn't wait for it to finish.
    made = make()
    assert wait_for(lambda: connected(made))
    play_file = made.speaker.play_file
    made.speaker.play_file = lambda filename, **details: (None if filename.endswith("missing.wav")
                                                          else play_file(filename, **details))
    brain.say("play_sound", id="sound-1", name="missing.wav", volume=1.0, kind="reply", start=0.0, length=None)

    assert wait_for(lambda: brain.messages("played"))
    assert brain.messages("played") == [{"type": "played", "id": "sound-1", "stopped": False, "position": 0}]
    assert made._playing == {}


def test_with_no_brain_the_wake_word_gets_the_phrase(brain, make):
    brain.up = False
    made = make()
    assert wait_for(lambda: made.wake.listening)
    made.wake.say()

    assert wait_for(lambda: PHRASE in made.speaker.files())
    assert brain.hellos == []
    assert "announcement" not in made.speaker.silenced


def test_with_no_brain_due_alarms_ring_and_the_wake_word_dismisses_them(brain, make, tmp_path):
    brain.up = False
    AlarmCopy(str(tmp_path / "alarm_copy.json")).replace([entry(time.time() - 1)])
    made = make()
    alarm = os.path.join(paths.SOUNDS_DIR, "alarm_sound.wav")
    assert wait_for(lambda: alarm in made.speaker.files())
    assert made.ringer.ringing
    assert wait_for(lambda: made.wake.listening)
    made.wake.say()

    # Listening again, as the satellite does once it has done all the wake word does -
    # it lets the announcements back only after the ringing has stopped.
    assert wait_for(lambda: not made.ringer.ringing and made.wake.listening)
    assert made.alarms.away_report() == {"rang": ["alarm-1@1729587600"], "dismissed": ["alarm-1@1729587600"]}
    # Dismissing is all the wake word does.
    assert PHRASE not in made.speaker.files()
    assert "announcement" not in made.speaker.silenced


def test_a_check_of_the_alarm_copy_which_fails_is_logged_with_where_it_failed(brain, make, caplog):
    brain.up = False
    made = make(start=False)

    def broken(now):
        raise RuntimeError("The copy can't be read")

    made.ringer.check = broken
    with caplog.at_level(logging.ERROR, logger="satellite"):
        make.begin(made)
        assert wait_for(lambda: any("Couldn't check the alarm copy" in record.getMessage()
                                    for record in caplog.records))

    record = next(record for record in caplog.records if "Couldn't check the alarm copy" in record.getMessage())
    assert record.exc_info is not None and "The copy can't be read" in caplog.text


def test_telling_the_brain_the_echo_canceller_is_ready_failing_is_logged_as_that(brain, make, caplog):
    # On the same thread, each second, as the check of the alarm copy - which still goes on.
    brain.up = False
    made = make(start=False)
    checked = []
    made.ringer.check = checked.append

    def broken():
        raise RuntimeError("no canceller")

    made._tell_echo_ready = broken
    with caplog.at_level(logging.ERROR, logger="satellite"):
        make.begin(made)
        assert wait_for(lambda: "Couldn't tell the brain whether the echo canceller is ready" in caplog.text)
    assert "Couldn't check the alarm copy" not in caplog.text
    assert wait_for(lambda: checked)


def test_the_brains_copy_of_the_alarms_is_kept(brain, make, tmp_path):
    made = make()
    assert wait_for(lambda: connected(made))
    brain.say("alarms", entries=[entry(1729587600.0)])

    assert wait_for(lambda: made.alarms.due(1729587601.0))
    assert AlarmCopy(str(tmp_path / "alarm_copy.json")).due(1729587601.0) == [entry(1729587600.0)]


def test_the_brains_phrase_is_kept_and_said_once_the_brain_is_gone(brain, make, tmp_path):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("phrase", b"\x00\x00" * 100, rate=22050)
    assert wait_for(lambda: (tmp_path / "phrase.wav").exists())
    brain.drop()
    assert wait_for(lambda: not made.link.connected and made.wake.listening)

    made.wake.say()
    assert wait_for(lambda: str(tmp_path / "phrase.wav") in made.speaker.files())
    assert PHRASE not in made.speaker.files()


def test_the_away_report_is_sent_on_connecting(brain, make, tmp_path):
    brain.up = False
    # The last run never stopped properly.
    (tmp_path / "running").write_text("{}")
    AlarmCopy(str(tmp_path / "alarm_copy.json")).replace([entry(time.time() - 1)])
    made = make()
    assert wait_for(lambda: made.ringer.ringing)

    brain.up = True
    assert wait_for(lambda: brain.messages("away"))
    # Not dismissed, and still ringing - so the brain takes the ringing over.
    assert brain.messages("away") == [{"type": "away", "rang": ["alarm-1@1729587600"], "dismissed": [],
                                       "ringing": ["alarm-1@1729587600"]}]
    nonces = brain.hellos[0].pop("nonces")
    assert len(nonces) == 2 and nonces[0] != nonces[1] and all(isinstance(n, str) for n in nonces)
    assert brain.hellos == [{"name": "bedroom", "hostname": socket.gethostname(), "sample_rate": 48000,
                             "unclean": True, "speaker": None,
                             "push_to_talk": False, "machine": "pi", "saves_notes": False,
                             "media_controls": False, "opens": False, "system_volume": False,
                             "approves": False, "conversations": True,
                             "version": None, "updates": False, "update_format": None}]


def test_the_satellite_tells_the_brain_its_spotify_speaker(brain, make):
    # The name its setup gave raspotify, from its own settings - so the brain plays
    # its Spotify there, rather than on the brain's own speaker name.
    make(spotify={"device_name": "Bedroom Pi"})
    assert wait_for(lambda: brain.hellos)
    assert brain.hellos[0]["speaker"] == "Bedroom Pi"


@pytest.mark.parametrize("device_name", [datetime.date(2026, 10, 26), {"Bedroom Pi"}, 7, "x" * 65],
                         ids=["date", "set", "number", "too-long"])
def test_a_speaker_the_brain_cant_take_is_left_out(brain, make, caplog, device_name):
    # A hand edit such as an unquoted device_name: 2026-10-26, which YAML reads as a
    # date - which would stop the hello being sent at all, and so stop every connect.
    with caplog.at_level("WARNING", logger="satellite"):
        made = make(spotify={"device_name": device_name})
    assert wait_for(lambda: brain.hellos)

    assert brain.hellos[0]["speaker"] is None
    assert made.spotify_speaker is None
    assert len([record for record in caplog.records if "device_name" in record.getMessage()]) == 1
    # Up to 64 characters is sent as it is.
    assert make(name="kitchen", spotify={"device_name": "x" * 64}).spotify_speaker == "x" * 64


def ringing_as_the_brain_comes_back(brain, make, tmp_path):
    """
    A satellite ringing an alarm from its copy as it reaches the brain again, once it
    has told the brain so and done all it does on connecting.
    """
    brain.up = False
    AlarmCopy(str(tmp_path / "alarm_copy.json")).replace([entry(time.time() - 1)])
    made = make()
    assert wait_for(lambda: made.ringer.ringing)
    brain.up = True
    assert wait_for(lambda: brain.messages("away"))
    # It tells the brain under the lock, so has finished connecting once it's free.
    with made._lock:
        pass
    return made


def test_whats_ringing_is_only_handed_over_once_the_brain_says_it_has_it(brain, make, tmp_path):
    # Quietened and forgotten as soon as the brain had been told, an alarm stopped early
    # if the brain was lost before it had saved what it was told - as the next brain
    # never heard it was still ringing.
    made = ringing_as_the_brain_comes_back(brain, make, tmp_path)
    assert made.ringer.ringing
    assert made.alarms.away_report() == {"rang": ["alarm-1@1729587600"], "dismissed": []}

    brain.say("away_settled")
    assert wait_for(lambda: not made.ringer.ringing)
    assert made.alarms.away_report() == {"rang": [], "dismissed": []}


def test_only_what_the_brain_was_told_is_forgotten_once_it_has_it(brain, make, tmp_path):
    made = ringing_as_the_brain_comes_back(brain, make, tmp_path)
    # Dismissed before the brain says it has it, so after the brain was told.
    assert wait_for(lambda: made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: made.alarms.away_report()["dismissed"])

    brain.say("away_settled")
    assert wait_for(lambda: not made.alarms.away_report()["rang"])
    assert made.alarms.away_report() == {"rang": [], "dismissed": ["alarm-1@1729587600"]}


def test_a_brain_lost_before_it_says_it_has_it_leaves_the_satellite_ringing(brain, make, tmp_path):
    made = ringing_as_the_brain_comes_back(brain, make, tmp_path)
    told = brain.messages("away")[0]
    brain.drop()
    assert wait_for(lambda: not made.link.connected)

    # As while the brain is away - so the next brain is told it all again.
    assert made.ringer.ringing
    assert made.alarms.away_report() == {"rang": ["alarm-1@1729587600"], "dismissed": []}
    brain.up = True
    assert wait_for(lambda: len(brain.messages("away")) == 2)
    assert brain.messages("away")[1] == told


def test_a_brain_which_never_says_it_has_it_is_given_up_on(brain, make, tmp_path, monkeypatch):
    # One whose wait for the report ran out before it arrived never applies it, and
    # one stuck part way through still answers pings - so neither is noticed otherwise.
    monkeypatch.setattr(satellite, "SETTLE_WAIT", 0.2)
    made = ringing_as_the_brain_comes_back(brain, make, tmp_path)
    told = brain.messages("away")[0]

    # Dropped, and connected again, ringing on meanwhile - and the brain told it all again.
    assert wait_for(lambda: len(brain.messages("away")) >= 2)
    assert len(brain.hellos) >= 2
    assert made.ringer.ringing
    assert brain.messages("away")[1] == told


def test_what_the_brain_took_over_rings_from_the_copy_again_if_the_brain_goes(brain, make, tmp_path):
    # The brain was ringing it too, so its copy still lists it - which kept it marked as
    # rung, and a brain lost while it rang it left it silent on both sides.
    made = ringing_as_the_brain_comes_back(brain, make, tmp_path)
    brain.say("alarms", entries=[entry(time.time() - 1)])
    brain.say("away_settled")
    assert wait_for(lambda: not made.ringer.ringing)

    brain.drop()
    assert wait_for(lambda: made.ringer.ringing_ids() == ["alarm-1@1729587600"])


def test_a_satellite_restarted_while_ringing_rings_on_and_says_so(brain, make, tmp_path):
    # Its watchdog, a crash or the power, with the brain away. It knew only that the
    # alarm had rung, so rang it no more - and the brain, told just that, neither rang
    # it nor said it had been missed.
    brain.up = False
    AlarmCopy(str(tmp_path / "alarm_copy.json")).replace([entry(time.time() - 1)])
    made = make()
    assert wait_for(lambda: made.ringer.ringing)
    made.stop()
    assert wait_for(lambda: made.exited)

    again = make()
    assert wait_for(lambda: again.ringer.ringing_ids() == ["alarm-1@1729587600"])
    brain.up = True
    assert wait_for(lambda: brain.messages("away"))
    assert brain.messages("away")[0]["ringing"] == ["alarm-1@1729587600"]


def test_a_satellite_restarted_once_its_ring_would_have_ended_doesnt_ring_again(brain, make, tmp_path):
    brain.up = False
    due = time.time() - fallback.RING_LIMIT - 1
    copy = AlarmCopy(str(tmp_path / "alarm_copy.json"))
    copy.replace([entry(due)])
    # Left going as the satellite stopped, that long ago.
    copy.rang(entry(due), due)

    made = make()
    brain.up = True
    assert wait_for(lambda: brain.messages("away"))
    assert brain.messages("away")[0]["ringing"] == [] and not made.ringer.ringing


def test_mute_from_the_brain_stops_the_wake_word(brain, make, tmp_path):
    StateCopy(str(tmp_path / "state.json")).update({"volumes": {}, "muted": True})
    made = make(start=False)
    # Muted from the start, before there's any brain to say so.
    assert made.wake.muted is True
    make.begin(made)
    assert wait_for(lambda: brain.hellos)

    brain.say("state", volumes={"general": 0.5, "alarm": 0.6, "music": 0.7}, muted=False)
    assert wait_for(lambda: made.wake.muted is False)
    brain.say("state", volumes={"general": 0.5, "alarm": 0.6, "music": 0.7}, muted=True)
    assert wait_for(lambda: made.wake.muted is True)
    assert StateCopy(str(tmp_path / "state.json")).volume("general") == 0.5

    assert wait_for(lambda: made.wake.listening)
    made.wake.say()
    time.sleep(0.1)
    assert brain.messages("wake") == []


class FakeClips:
    """Keeps what it's handed - or, once told to, raises as it's handed it."""

    def __init__(self, microphone, muted, **settings):
        self.microphone = microphone
        self.muted = muted
        self.settings = settings
        self.kept = []
        self.failure = None

    def keep(self, heard_at, score):
        self.kept.append((heard_at, score))
        if self.failure is not None:
            raise self.failure


def test_a_wake_hands_its_position_and_score_over_to_be_kept(brain, make, monkeypatch):
    monkeypatch.setattr(satellite, "WakeClips", FakeClips)
    made = make(wake_clips={"keep": True, "most": 7})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    heard_at = made.wake.say()
    assert wait_for(lambda: made.clips.kept)
    brain.say("stop_listening")

    assert made.clips.kept == [(heard_at, 0.9)]
    assert made.clips.microphone is made.microphone
    assert made.clips.settings == {"most": 7}


@pytest.mark.parametrize("sections", [{}, {"wake_clips": {"keep": False, "most": 400}}])
def test_with_clips_switched_off_nothing_is_kept(brain, make, tmp_path, sections):
    made = make(**sections)
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: brain.recordings())
    brain.say("stop_listening")
    # Long enough for a clip to have been finished, at the fake microphone's speed.
    time.sleep(0.2)

    assert made.clips is None
    assert not (tmp_path / "clips").exists()


def test_a_wake_keeps_a_clip_with_nothing_but_those_seconds_in_it(brain, make, tmp_path):
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    # Past the first two seconds, so there's all of a clip to keep.
    assert wait_for(lambda: made.microphone.position > 3 * RATE)
    heard_at = made.wake.say()
    assert wait_for(lambda: (tmp_path / "clips").exists() and os.listdir(tmp_path / "clips"))
    brain.say("stop_listening")
    assert wait_for(lambda: not made.clips._keeping.locked())

    (name,) = os.listdir(tmp_path / "clips")
    assert name.endswith("-0.90.wav")
    with wave.open(str(tmp_path / "clips" / name), "rb") as file:
        audio = np.frombuffer(file.readframes(file.getnframes()), np.int16)
    assert np.array_equal(audio, samples(heard_at - 2 * RATE, heard_at + RATE))


def test_a_muted_satellite_keeps_nothing(brain, make, tmp_path):
    StateCopy(str(tmp_path / "state.json")).update({"volumes": {}, "muted": True})
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: made.wake.listening)
    made.wake.say()
    time.sleep(0.2)

    assert not (tmp_path / "clips").exists()


@pytest.fixture
def real_time(monkeypatch):
    """Has the fake microphone hear at the speed the real one does, so a second lasts one."""
    monkeypatch.setitem(globals(), "SPEED", 1)


def wake_clip_files(tmp_path):
    return [name for name in os.listdir(tmp_path / "clips") if name.endswith(".wav")]


def set_mute(brain, made, muted):
    brain.say("state", volumes={}, muted=muted)
    assert wait_for(lambda: made.wake.muted is muted)


def test_a_wake_just_after_unmuting_keeps_nothing_and_goes_on_as_usual(brain, make, monkeypatch, caplog, real_time):
    monkeypatch.setattr(satellite, "WakeClips", FakeClips)
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    set_mute(brain, made, True)
    set_mute(brain, made, False)
    with caplog.at_level(logging.INFO, logger="satellite"):
        made.wake.say()
        assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")

    assert made.clips.kept == []
    assert "Wake clip not kept, muted" in [record.getMessage() for record in caplog.records]


def test_a_wake_long_enough_after_unmuting_is_kept(brain, make, monkeypatch):
    monkeypatch.setattr(satellite, "WakeClips", FakeClips)
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    set_mute(brain, made, True)
    set_mute(brain, made, False)
    unmuted_at = made.microphone.position
    # The 2 seconds before the wake, the margin an unmute is given, and a moment.
    assert wait_for(lambda: made.microphone.position > unmuted_at + 2 * RATE + 4800 + RATE // 10)
    heard_at = made.wake.say()
    assert wait_for(lambda: made.clips.kept)
    brain.say("stop_listening")

    assert made.clips.kept == [(heard_at, 0.9)]


def test_an_unmute_is_noted_0_3_s_late_for_the_audio_still_on_its_way(brain, make, real_time):
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    set_mute(brain, made, True)
    before = made.microphone.position
    set_mute(brain, made, False)
    after = made.microphone.position

    assert before + 4800 <= made._unmuted_at <= after + 4800


def test_what_counts_as_muted_ends_0_3_s_past_the_position_of_the_unmute(brain, make):
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    assert made.clips.muted(0) is False
    set_mute(brain, made, True)
    assert made.clips.muted(made.microphone.position + RATE) is True
    set_mute(brain, made, False)
    unmuted_at = made._unmuted_at

    assert unmuted_at > 0
    assert made.clips.muted(unmuted_at - 1) is True
    assert made.clips.muted(unmuted_at) is False


def test_a_state_which_doesnt_unmute_moves_nothing(brain, make):
    # The brain sends its state whole, for a volume as much as for a mute.
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("state", volumes={}, muted=False, longest_request=12)
    assert wait_for(lambda: made.longest_request == 12)
    assert made._unmuted_at == 0

    set_mute(brain, made, True)
    set_mute(brain, made, False)
    unmuted_at = made._unmuted_at
    brain.say("state", volumes={}, muted=False, longest_request=13)
    assert wait_for(lambda: made.longest_request == 13)

    assert made._unmuted_at == unmuted_at


def test_a_mute_in_the_second_after_a_wake_keeps_nothing(brain, make, tmp_path, real_time):
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: made.clips._keeping.locked())
    set_mute(brain, made, True)
    assert wait_for(lambda: not made.clips._keeping.locked())
    brain.say("stop_listening")

    assert wake_clip_files(tmp_path) == []


def test_a_mute_whose_message_lands_just_after_the_clip_was_heard_keeps_nothing(brain, make, tmp_path, monkeypatch):
    # The clip's wait for news of a mute lasts until the mute has landed, rather than the
    # mute having to land inside its half a second.
    made = make(wake_clips={"keep": True, "most": 400})
    waiting, landed = threading.Event(), threading.Event()
    real_sleep = time.sleep

    def sleep(seconds):
        if threading.current_thread().name == "wake-clip" and seconds == wake_clips.MUTE_NEWS_SECONDS:
            waiting.set()
            landed.wait(timeout=5)
        else:
            real_sleep(seconds)

    monkeypatch.setattr(wake_clips.time, "sleep", sleep)
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    # All of the clip heard and read by now.
    assert waiting.wait(timeout=5)
    set_mute(brain, made, True)
    landed.set()
    assert wait_for(lambda: not made.clips._keeping.locked())
    brain.say("stop_listening")

    assert wake_clip_files(tmp_path) == []


def test_a_satellite_keeping_clips_says_so_as_it_starts(brain, make, tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="voice_assistant.wake_clips"):
        make(start=False, wake_clips={"keep": True, "most": 400})

    assert str(tmp_path / "clips") in caplog.text


def test_a_satellite_not_keeping_clips_says_nothing_of_them(brain, make, caplog):
    with caplog.at_level(logging.INFO, logger="voice_assistant.wake_clips"):
        make(start=False)

    assert caplog.records == []


def test_a_mute_and_unmute_both_in_the_second_after_a_wake_keep_nothing(brain, make, tmp_path, real_time):
    # Unmuted again by the time the clip looks, so asking whether it's muted now isn't enough.
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: made.clips._keeping.locked())
    set_mute(brain, made, True)
    set_mute(brain, made, False)
    assert made.clips._keeping.locked()
    assert wait_for(lambda: not made.clips._keeping.locked())
    brain.say("stop_listening")

    assert wake_clip_files(tmp_path) == []


def test_clips_which_raise_leave_the_wake_going(brain, make, monkeypatch, caplog):
    monkeypatch.setattr(satellite, "WakeClips", FakeClips)
    made = make(wake_clips={"keep": True, "most": 400})
    made.clips.failure = RuntimeError("Nothing can be kept")
    assert wait_for(lambda: connected(made) and made.wake.listening)
    heard_at = made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")

    assert brain.messages("wake") == [{"type": "wake", "talked_over": False, "pre_roll": 4800, "recording": "wake-1"}]
    assert np.array_equal(brain.recordings()[0], samples(heard_at - 4800, heard_at))
    assert "Nothing can be kept" in caplog.text


def test_a_restart_from_the_brain_exits_for_systemd_to_start_it_again(brain, make, tmp_path):
    made = make(name="")
    assert wait_for(lambda: brain.hellos)
    assert brain.hellos[0]["name"] == socket.gethostname()
    brain.say("restart")

    assert wait_for(lambda: made.exited)
    assert made.exited == [1]
    # Asked to, so it stopped properly.
    assert not (tmp_path / "running").exists()


def test_a_restart_from_the_brain_is_heard_while_listening_keeps_failing(brain, make):
    # Which is when Home Assistant's Restart is needed most - and it was only looked
    # for once listening had worked.
    made = make(start=False)

    def broken(until=None):
        raise RuntimeError("The microphone has gone")

    made.wake.listen_once = broken
    make.begin(made)
    assert wait_for(lambda: brain.hellos)
    brain.say("restart")

    assert wait_for(lambda: made.exited)
    assert made.exited == [1]


def test_push_to_talk_makes_no_wake_word_or_clips(brain, make, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Made for a push-to-talk satellite")

    monkeypatch.setattr(satellite, "WakeWordDetector", refuse)
    monkeypatch.setattr(satellite, "WakeClips", refuse)
    made = make(satellite={"trigger": "push_to_talk"}, wake_clips={"keep": True})
    assert made.push_to_talk is True
    assert made.wake is None and made.clips is None
    assert wait_for(lambda: brain.hellos)
    assert brain.hellos[0]["push_to_talk"] is True


def test_push_to_talk_takes_state_without_a_wake_word(brain, make, caplog):
    made = make(satellite={"trigger": "push_to_talk"})
    assert made.wake is None
    assert wait_for(lambda: connected(made))
    with caplog.at_level("ERROR"):
        brain.say("state", volumes={"general": 0.5}, muted=True, wake_confidence=0.75, voice_check=0.6)
        assert wait_for(lambda: made.state.muted is True)
        assert made.state.volume("general") == 0.5
        # Unmuted again, which a satellite with a wake word notes the position of.
        brain.say("state", volumes={"general": 0.25}, muted=False, wake_confidence=0.75, voice_check=0.6)
        assert wait_for(lambda: made.state.volume("general") == 0.25)
        assert made.state.muted is False
        # The link logs a handler which fails and carries on, with the state already stored
        # - so one more state, handled after both of those, shows they went through whole.
        brain.say("state", volumes={"general": 0.75}, muted=False)
        assert wait_for(lambda: made.state.volume("general") == 0.75)
    assert not [record.getMessage() for record in caplog.records if "failed" in record.getMessage()]


def test_a_push_to_talk_satellite_sends_the_follow_up_the_brain_asks_for(brain, make):
    made = make(satellite={"trigger": "push_to_talk"})
    assert made.wake is None
    assert wait_for(lambda: connected(made))
    brain.say("listen", recording="listen-1")
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening", recording="listen-1")


def pressable(brain, make, **sections):
    """A push-to-talk satellite which has reached the brain."""
    made = make(satellite={"trigger": "push_to_talk"}, **sections)
    assert made.wake is None
    assert wait_for(lambda: connected(made))
    return made


def test_a_press_starts_a_recording(brain, make):
    made = pressable(brain, make)
    brain.say("state", volumes={"general": 0.5}, muted=False)
    assert wait_for(lambda: made.state.volume("general") == 0.5)
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")

    # Nothing from before the press, and saying it was one.
    assert brain.messages("wake") == [{"type": "wake", "talked_over": False, "pre_roll": 0, "recording": "wake-1",
                                       "pressed": True}]
    assert brain.kinds().index("wake") < brain.kinds().index("audio")
    # The press's own sound, at once and from here, at the general volume.
    assert made.speaker.files() == [os.path.join(made.sounds_folder, "listening.wav")]
    assert made.speaker.playing[0].volume == 0.5
    # Not the short piece of audio before a wake word's recording.
    assert all(len(piece) == 1600 for piece in brain.recordings()[:2])


def test_a_press_makes_a_recording_each_time(brain, make):
    made = pressable(brain, make)
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 1)
    brain.say("stop_listening", recording="wake-1")
    assert wait_for(lambda: made._recording is None)
    made._pressed()
    assert wait_for(lambda: len(brain.messages("wake")) == 2)
    assert [message["recording"] for message in brain.messages("wake")] == ["wake-1", "wake-2"]
    assert all(message["pressed"] is True for message in brain.messages("wake"))
    brain.say("stop_listening", recording="wake-2")


def test_a_second_press_ends_the_recording(brain, make):
    made = pressable(brain, make)
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    made._pressed()

    assert wait_for(lambda: brain.messages("audio_end"))
    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "pressed"}]
    # Told after every piece of audio, and no more comes.
    assert brain.kinds()[-1] == "audio_end"
    time.sleep(0.1)
    assert brain.kinds()[-1] == "audio_end"
    assert brain.messages("wake") != [] and len(brain.messages("wake")) == 1
    # The brain can't be waiting on audio which isn't coming: it's over until the next press.
    assert wait_for(lambda: made._recording is None)
    made._pressed()
    assert wait_for(lambda: len(brain.messages("wake")) == 2)
    brain.say("stop_listening")


def test_a_press_ends_the_answer_to_a_question_too(brain, make):
    made = pressable(brain, make)
    brain.say("listen", recording="listen-1")
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    made._pressed()

    assert wait_for(lambda: brain.messages("audio_end"))
    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "pressed"}]
    # Not a recording of its own.
    assert brain.messages("wake") == []


def test_a_reply_which_starts_while_a_press_is_listening_isnt_played(brain, make):
    # Pressed while the brain is still working on the last request: its reply would play
    # into the open microphone, which on a push-to-talk satellite has no echo cancelling,
    # and be heard as the user's next request. So it's stopped as it starts, as a press
    # over it would have done.
    made = pressable(brain, make)
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 1)
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=0.8, kind="reply", paused=False)
    brain.say("stream_audio", np.array([1, 2, 3], np.int16).tobytes(), id="reply-1")

    assert wait_for(lambda: brain.messages("played"))
    (reply,) = made.speaker.streams()
    assert reply.stopped
    assert brain.messages("played")[0]["id"] == "reply-1" and brain.messages("played")[0]["stopped"]
    # Still listening to the user.
    count = len(brain.recordings())
    assert wait_for(lambda: len(brain.recordings()) > count)
    brain.say("stop_listening")


def test_a_reply_sound_while_a_press_is_listening_isnt_played_either(brain, make):
    made = pressable(brain, make)
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 1)
    brain.say("play_sound", id="sound-1", name="not_heard.wav", volume=1.0, kind="reply")

    assert wait_for(lambda: brain.messages("played"))
    (played,) = brain.messages("played")
    assert played["id"] == "sound-1" and played["stopped"]
    brain.say("stop_listening")


def test_the_chime_for_an_answer_the_brain_asked_for_still_plays(brain, make):
    # Only what a press started stops a reply: the brain's own listen, for the answer to a
    # question it asked, keeps its chime, the user's cue that Nova is waiting.
    made = pressable(brain, make)
    brain.say("listen", recording="listen-1")
    assert wait_for(lambda: len(brain.recordings()) >= 1)
    brain.say("play_sound", id="chime-1", name="listening.wav", volume=0.4, kind="reply")

    assert wait_for(lambda: made.speaker.files())
    (chime,) = [playback for playback in made.speaker.playing if playback.filename]
    assert not chime.stopped
    chime.finish()
    assert wait_for(lambda: brain.messages("played"))
    assert not brain.messages("played")[0]["stopped"]
    brain.say("stop_listening", recording="listen-1")


def test_after_the_press_s_recording_ends_a_reply_plays(brain, make):
    made = pressable(brain, make)
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 1)
    brain.say("stop_listening")
    assert wait_for(lambda: brain.messages("audio_end") or made._recording is None)
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=0.8, kind="reply", paused=False)
    brain.say("stream_audio", np.array([1, 2, 3], np.int16).tobytes(), id="reply-1")

    assert wait_for(lambda: made.speaker.streams() and made.speaker.streams()[0].pieces)
    assert not made.speaker.streams()[0].stopped


def test_a_wake_word_satellite_plays_a_reply_over_its_recording_as_before(brain, make):
    # The Pi cancels its own echo, so a reply which starts while it listens plays as it always has.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=0.8, kind="reply", paused=False)
    brain.say("stream_audio", np.array([1, 2, 3], np.int16).tobytes(), id="reply-1")

    assert wait_for(lambda: made.speaker.streams() and made.speaker.streams()[0].pieces)
    assert not made.speaker.streams()[0].stopped
    brain.say("stop_listening")


def test_a_press_over_a_reply_stops_it_and_listens(brain, make):
    made = pressable(brain, make)
    brain.say("play_stream", id="music-1", rate=44100, channels=2, volume=0.5, kind="music", paused=False)
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=0.8, kind="reply", paused=False)
    brain.say("stream_audio", np.array([1, 2, 3], np.int16).tobytes(), id="reply-1")
    assert wait_for(lambda: len(made.speaker.streams()) == 2 and made.speaker.streams()[1].pieces)
    music, reply = made.speaker.streams()
    made._pressed()

    assert wait_for(lambda: brain.messages("played"))
    assert reply.stopped
    assert music.paused and not music.done
    assert brain.messages("wake") == [{"type": "wake", "talked_over": True, "pre_roll": 0, "recording": "wake-1",
                                       "pressed": True}]
    # Told of the press before the end of the reply it stopped, as for the wake word.
    assert brain.kinds().index("wake") < brain.kinds().index("played")
    # Heard to be listening, though the reply was stopped: the sound isn't stopped with it.
    listening = os.path.join(made.sounds_folder, "listening.wav")
    assert made.speaker.files() == [listening]
    assert not made.speaker.playing[-1].stopped
    brain.say("stop_listening")


def test_a_press_while_muted_still_listens(brain, make):
    made = pressable(brain, make)
    brain.say("state", volumes={}, muted=True)
    assert wait_for(lambda: made.state.muted is True)
    made._pressed()

    assert wait_for(lambda: len(brain.recordings()) >= 1)
    assert brain.messages("wake")[0]["pressed"] is True
    brain.say("stop_listening")


def test_a_press_with_no_brain_gets_the_phrase(brain, make):
    brain.up = False
    made = make(satellite={"trigger": "push_to_talk"})
    made._pressed()

    assert wait_for(lambda: PHRASE in made.speaker.files())
    # Only the phrase: no sound for a recording which wasn't made.
    assert made.speaker.files() == [PHRASE]
    # Not stuck: it's waiting for the next press, which gets the phrase again.
    assert wait_for(lambda: made._recording is None and not made._press_pending)
    made.speaker.playing.clear()
    made._pressed()
    assert wait_for(lambda: PHRASE in made.speaker.files())


def test_losing_the_brain_during_a_pressed_recording_says_the_phrase(brain, make):
    made = pressable(brain, make)
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.drop()

    assert wait_for(lambda: PHRASE in made.speaker.files())
    assert wait_for(lambda: made._recording is None)


def test_a_press_while_the_fallback_rings_dismisses_it(brain, make, tmp_path):
    brain.up = False
    AlarmCopy(str(tmp_path / "alarm_copy.json")).replace([entry(time.time() - 1)])
    made = make(satellite={"trigger": "push_to_talk"})
    alarm = os.path.join(paths.SOUNDS_DIR, "alarm_sound.wav")
    assert wait_for(lambda: alarm in made.speaker.files())
    assert made.ringer.ringing
    made._pressed()

    assert wait_for(lambda: not made.ringer.ringing)
    assert made.alarms.away_report() == {"rang": ["alarm-1@1729587600"], "dismissed": ["alarm-1@1729587600"]}
    # Dismissing is all it does, as for the wake word: no recording, no sound, no phrase.
    assert wait_for(lambda: "announcement" not in made.speaker.silenced)
    assert made.speaker.files() == [alarm]
    assert PHRASE not in made.speaker.files()


def test_push_to_talk_opens_the_microphone_only_while_recording(brain, make):
    # So the PC can sleep, and its microphone light is only on while it listens.
    made = pressable(brain, make)
    assert made.microphone.always_open is False and made.speaker.always_open is False
    assert made.microphone.holds == 0

    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    assert made.microphone.holds == 1
    brain.say("stop_listening")
    assert wait_for(lambda: made.microphone.holds == 0)

    # And for the answer to a question.
    brain.say("listen", recording="listen-1")
    assert wait_for(lambda: len(brain.recordings()) >= 4)
    assert made.microphone.holds == 1
    brain.say("stop_listening", recording="listen-1")
    assert wait_for(lambda: made.microphone.holds == 0)
    assert made.microphone.unheld_reads == 0

    # The Pi's are open throughout, as they always were.
    wake_word = make()
    assert wake_word.microphone.always_open is True and wake_word.speaker.always_open is True


def test_a_push_to_talk_satellite_tells_the_watchdog_while_it_waits(brain, make, monkeypatch):
    # Its microphone is closed while it waits, so nothing else would - and systemd
    # restarts a satellite which hasn't for 150 seconds.
    monkeypatch.setattr(satellite, "PRESS_WAIT", 0.02)
    beats = []
    monkeypatch.setattr(satellite.watchdog, "beat", lambda at=None: beats.append(at))
    pressable(brain, make)
    assert wait_for(lambda: len(beats) >= 5)


def test_a_press_wakes_the_wait_at_once(brain, make, monkeypatch):
    # Not left until the next look, a second on.
    monkeypatch.setattr(satellite, "PRESS_WAIT", 30)
    made = pressable(brain, make)
    time.sleep(0.1)
    made._pressed()
    assert wait_for(lambda: brain.messages("wake"), seconds=2)
    brain.say("stop_listening")


def test_the_listener_is_made_only_for_a_push_to_talk_satellite_and_closed_with_it(brain, make):
    made = pressable(brain, make)
    assert wait_for(lambda: made.press_listener is not None and made.press_listener.started)
    assert made.press_listener.port == 10401
    assert made.press_listener.on_press == made._pressed
    # What a second launch of the app asks for: the settings page.
    assert made.press_listener.on_open == made._open_page
    listener = made.press_listener
    made.stop()
    assert wait_for(lambda: made.exited)
    assert listener.closed

    wake_word = make()
    assert wait_for(lambda: connected(wake_word))
    assert wake_word.press_listener is None


def test_the_running_mark_says_the_press_port(brain, make, tmp_path):
    # So a second launcher can find the running one and ask it to open its page.
    made = pressable(brain, make)
    mark = json.loads((tmp_path / "running").read_text())
    assert mark["press_port"] == made.press_listener.port == 10401
    assert isinstance(mark["started_at"], float)


def test_a_wake_word_satellites_mark_has_no_port(brain, make, tmp_path):
    made = make()
    assert wait_for(lambda: connected(made))
    mark = json.loads((tmp_path / "running").read_text())
    assert "press_port" not in mark and "started_at" in mark


def test_a_satellite_which_cant_have_its_press_port_stops_before_reaching_the_brain(brain, make, monkeypatch):
    # Bound before the link, so one which can't listen for presses is started again by
    # the launcher without the brain hearing of it each time.
    class Taken(FakePress):
        def start(self):
            raise OSError("Address already in use")

    monkeypatch.setattr(satellite, "PressListener", Taken)
    made = make(start=False, satellite={"trigger": "push_to_talk"})
    with pytest.raises(OSError):
        made.run()
    assert made.link is None and brain.sockets == []


def test_a_press_through_the_real_listener_starts_a_recording(brain, make, monkeypatch):
    # Port 0, as the setting can't be, so the test takes whichever port is free.
    monkeypatch.setattr(satellite, "PressListener",
                        lambda port, on_press, on_open=None: PressListener(0, on_press, on_open))
    made = pressable(brain, make)
    assert wait_for(lambda: made.press_listener is not None and made.press_listener.port)
    port = made.press_listener.port
    # A plain socket, as the fake brain stands in for socket.create_connection.
    with socket.socket() as sock:
        sock.settimeout(5)
        sock.connect(("127.0.0.1", port))
        sock.sendall(f"POST /press HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Nova-Press: 1\r\n"
                     f"Content-Length: 0\r\n\r\n".encode())
        assert sock.recv(100).startswith(b"HTTP/1.0 204")

    assert wait_for(lambda: len(brain.recordings()) >= 1)
    assert brain.messages("wake")[0]["pressed"] is True
    brain.say("stop_listening")


def test_a_push_to_talk_satellite_stops_when_asked(brain, make):
    made = make(satellite={"trigger": "push_to_talk"})
    assert made.wake is None
    assert wait_for(lambda: connected(made))
    made.stop()
    assert wait_for(lambda: made.exited)


def test_wake_word_satellite_hello_is_unchanged(brain, make):
    make()
    assert wait_for(lambda: brain.hellos)
    assert brain.hellos[0]["push_to_talk"] is False


# The machine, and notes saved in its Downloads folder

@pytest.fixture
def downloads(tmp_path, monkeypatch):
    """A PC with a Downloads folder of its own, which notes are saved in."""
    folder = tmp_path / "Downloads"
    folder.mkdir()
    monkeypatch.setattr(satellite, "machine_kind", lambda: "pc")
    monkeypatch.setattr(satellite, "downloads_folder", lambda: str(folder))
    return folder


def note_answers(brain):
    return brain.messages("note_saved")


def test_hello_carries_machine_and_notes(brain, make, downloads):
    make()
    assert wait_for(lambda: brain.hellos)
    assert brain.hellos[0]["machine"] == "pc" and brain.hellos[0]["saves_notes"] is True


def test_hello_says_no_notes_where_there_is_no_downloads_folder(brain, make, monkeypatch):
    # The Pi: it says what it is, and has no notes tool.
    monkeypatch.setattr(satellite, "machine_kind", lambda: "pi")
    monkeypatch.setattr(satellite, "downloads_folder", lambda: None)
    make()
    assert wait_for(lambda: brain.hellos)
    assert brain.hellos[0]["machine"] == "pi" and brain.hellos[0]["saves_notes"] is False


def test_a_downloads_lookup_which_fails_gives_a_hello_which_saves_no_notes(brain, make, monkeypatch, caplog):
    # The Windows lookup has never run on real Windows: whatever it raises, the satellite
    # still starts and connects, and says it doesn't save notes.
    def fails():
        raise ValueError("Windows said something odd")

    monkeypatch.setattr(satellite, "machine_kind", lambda: "pc")
    monkeypatch.setattr(satellite, "downloads_folder", fails)
    with caplog.at_level(logging.WARNING):
        make()
        assert wait_for(lambda: brain.hellos)
    assert brain.hellos[0]["machine"] == "pc" and brain.hellos[0]["saves_notes"] is False
    assert caplog.text.count("Windows said something odd") == 1


def test_a_machine_which_cant_be_told_is_unknown_not_a_stopped_satellite(brain, make, monkeypatch, caplog):
    def fails():
        raise OSError("no such thing")

    monkeypatch.setattr(satellite, "machine_kind", fails)
    monkeypatch.setattr(satellite, "downloads_folder", fails)
    with caplog.at_level(logging.WARNING):
        made = make()
        assert wait_for(lambda: connected(made))
    assert brain.hellos[0]["machine"] is None and brain.hellos[0]["saves_notes"] is False


def test_save_note_writes_and_answers(brain, make, downloads):
    made = make()
    assert wait_for(lambda: connected(made))

    brain.say("save_note", "Bins: green on Tuesday\nblue on Friday".encode(), id="note-7", title="Bins")

    assert wait_for(lambda: note_answers(brain))
    assert note_answers(brain) == [{"type": "note_saved", "id": "note-7", "file": "Bins.txt"}]
    assert (downloads / "Bins.txt").read_bytes() == b"Bins: green on Tuesday\nblue on Friday"


def test_a_taken_name_is_numbered_not_overwritten(brain, make, downloads):
    (downloads / "Bins.txt").write_text("mine")
    made = make()
    assert wait_for(lambda: connected(made))

    brain.say("save_note", b"new", id="note-1", title="Bins")

    assert wait_for(lambda: note_answers(brain))
    assert note_answers(brain)[0]["file"] == "Bins (2).txt"
    assert (downloads / "Bins.txt").read_text() == "mine"


def test_the_longest_note_arrives_whole(brain, make, downloads):
    made = make()
    assert wait_for(lambda: connected(made))
    # 50,000 characters of two and three bytes each: far more than a line of JSON may hold.
    text = "\u00e9\u4e2d" * 25_000

    brain.say("save_note", text.encode(), id="note-1", title="Long")

    assert wait_for(lambda: note_answers(brain))
    assert note_answers(brain)[0]["file"] == "Long.txt"
    assert (downloads / "Long.txt").read_bytes().decode() == text


def test_save_note_ends_lines_as_the_machine_does(brain, make, downloads, monkeypatch):
    # Windows' Notepad wants \r\n. Only the satellite's own sys is changed, so nothing else notices.
    monkeypatch.setattr(satellite, "sys", types.SimpleNamespace(platform="win32"))
    made = make()
    assert wait_for(lambda: connected(made))

    brain.say("save_note", b"one\ntwo", id="note-1", title="Lines")

    assert wait_for(lambda: note_answers(brain))
    assert (downloads / "Lines.txt").read_bytes() == b"one\r\ntwo"


def test_save_note_refusal_is_answered(brain, make, downloads, monkeypatch):
    def refuse(folder, title, text, windows):
        raise NoteRefused("That note's too long to save.")

    monkeypatch.setattr(satellite, "save_note", refuse)
    made = make()
    assert wait_for(lambda: connected(made))

    brain.say("save_note", b"x", id="note-1", title="Bins")

    assert wait_for(lambda: note_answers(brain))
    assert note_answers(brain) == [{"type": "note_saved", "id": "note-1", "error": "That note's too long to save."}]


def test_save_note_without_downloads_answers_an_error(brain, make, monkeypatch):
    monkeypatch.setattr(satellite, "machine_kind", lambda: "pi")
    monkeypatch.setattr(satellite, "downloads_folder", lambda: None)
    made = make()
    assert wait_for(lambda: connected(made))

    brain.say("save_note", b"x", id="note-1", title="Bins")

    assert wait_for(lambda: note_answers(brain))
    assert note_answers(brain) == [{"type": "note_saved", "id": "note-1",
                                    "error": "There's no Downloads folder to save it in."}]


def test_save_note_looks_for_the_folder_when_asked_not_when_it_started(brain, make, tmp_path, monkeypatch):
    # It was there for the hello, and has been moved since - or wasn't, and has been made.
    folder = {"now": str(tmp_path)}
    monkeypatch.setattr(satellite, "machine_kind", lambda: "pc")
    monkeypatch.setattr(satellite, "downloads_folder", lambda: folder["now"])
    made = make()
    assert wait_for(lambda: connected(made))
    assert brain.hellos[0]["saves_notes"] is True

    folder["now"] = None
    brain.say("save_note", b"x", id="note-1", title="Gone")
    assert wait_for(lambda: len(note_answers(brain)) == 1)
    assert note_answers(brain)[0]["error"] == "There's no Downloads folder to save it in."
    assert not (tmp_path / "Gone.txt").exists()

    folder["now"] = str(tmp_path)
    brain.say("save_note", b"x", id="note-2", title="Back")
    assert wait_for(lambda: len(note_answers(brain)) == 2)
    assert note_answers(brain)[1] == {"type": "note_saved", "id": "note-2", "file": "Back.txt"}


def test_a_downloads_lookup_which_fails_when_a_note_comes_is_answered(brain, make, downloads, monkeypatch):
    def fails():
        raise OSError("Windows said something odd")

    made = make()
    assert wait_for(lambda: connected(made))
    monkeypatch.setattr(satellite, "downloads_folder", fails)

    brain.say("save_note", b"x", id="note-1", title="Bins")

    assert wait_for(lambda: note_answers(brain))
    assert note_answers(brain) == [{"type": "note_saved", "id": "note-1",
                                    "error": "There's no Downloads folder to save it in."}]


def test_a_note_which_fails_unexpectedly_is_answered(brain, make, downloads, monkeypatch):
    def fails(folder, title, text, windows):
        raise RuntimeError("something nobody planned for")

    monkeypatch.setattr(satellite, "save_note", fails)
    made = make()
    assert wait_for(lambda: connected(made))

    brain.say("save_note", b"x", id="note-1", title="Bins")

    assert wait_for(lambda: note_answers(brain))
    assert note_answers(brain) == [{"type": "note_saved", "id": "note-1", "error": "The file couldn't be written."}]


def test_a_note_which_isnt_text_is_answered(brain, make, downloads):
    made = make()
    assert wait_for(lambda: connected(made))

    brain.say("save_note", b"\xff\xfe", id="note-1", title="Bins")

    assert wait_for(lambda: note_answers(brain))
    assert note_answers(brain) == [{"type": "note_saved", "id": "note-1", "error": "The file couldn't be written."}]
    assert list(downloads.iterdir()) == []


def test_a_note_without_an_id_is_dropped_and_the_satellite_goes_on(brain, make, downloads, caplog):
    made = make()
    assert wait_for(lambda: connected(made))

    with caplog.at_level(logging.WARNING):
        brain.say("save_note", b"x", title="Bins")
        brain.say("save_note", b"y", id="note-2", title="Next")
        assert wait_for(lambda: note_answers(brain))
    # Nobody could be told, so nothing was written for the first - and the next is saved.
    assert note_answers(brain) == [{"type": "note_saved", "id": "note-2", "file": "Next.txt"}]
    assert not (downloads / "Bins.txt").exists()
    assert "without an id" in caplog.text


def test_save_note_is_done_off_the_links_own_thread(brain, make, downloads, monkeypatch):
    # The link's reader is what hears the brain, and answers its pings: it never waits on the disk.
    ran_on = []

    def save(folder, title, text, windows):
        ran_on.append(threading.current_thread().name)
        return "Bins.txt"

    monkeypatch.setattr(satellite, "save_note", save)
    made = make()
    assert wait_for(lambda: connected(made))

    brain.say("save_note", b"x", id="note-1", title="Bins")

    assert wait_for(lambda: note_answers(brain))
    assert ran_on and ran_on[0] != "satellite-link"


@pytest.mark.parametrize("how", ["saves", "refuses", "fails"])
def test_save_note_never_logs_the_text(brain, make, downloads, monkeypatch, caplog, how):
    secret = "my-bank-pin-is-4921"
    if how == "refuses":
        def save(folder, title, text, windows):
            raise NoteRefused("That note's too long to save.")

        monkeypatch.setattr(satellite, "save_note", save)
    elif how == "fails":
        def save(folder, title, text, windows):
            # As an exception can, by carrying what it was handed.
            raise RuntimeError(f"couldn't write {text}")

        monkeypatch.setattr(satellite, "save_note", save)
    made = make()
    assert wait_for(lambda: connected(made))

    with caplog.at_level(logging.DEBUG):
        brain.say("save_note", secret.encode(), id="note-1", title="Pin")
        assert wait_for(lambda: note_answers(brain))
        # The answer is logged just after it's sent.
        time.sleep(0.1)

    assert secret not in caplog.text
    if how == "saves":
        assert "Saved a note as Pin.txt" in caplog.text


def quiet_until_said(brain, made, on):
    brain.say("quiet", on=on)
    if on:
        assert wait_for(lambda: made._quiet_until > time.monotonic())


def test_the_wake_word_is_ignored_while_nova_speaks_on_another_satellite(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("play_stream", id="music-1", rate=44100, channels=2, volume=0.5, kind="music", paused=False)
    assert wait_for(lambda: made.speaker.streams())
    quiet_until_said(brain, made, True)
    made.wake.say()
    # Heard, and let go: no wake for the brain, and nothing here stopped or paused.
    assert wait_for(lambda: made.wake.listening)
    time.sleep(0.2)
    assert brain.messages("wake") == []
    assert not made.speaker.streams()[0].paused


def test_the_wake_word_works_again_just_after_the_other_satellite_stops(brain, make, monkeypatch):
    monkeypatch.setattr(satellite, "QUIET_AFTER", 0.2)
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    quiet_until_said(brain, made, True)
    brain.say("quiet", on=False)
    # Still quiet for a moment, for the last words still leaving the other's speakers.
    assert wait_for(lambda: made._quiet_until <= time.monotonic() + 0.2)
    time.sleep(0.3)
    made.wake.say()
    assert wait_for(lambda: brain.messages("wake"))
    brain.say("stop_listening")


def test_being_quiet_ends_by_itself_if_never_lifted(brain, make, monkeypatch):
    monkeypatch.setattr(satellite, "QUIET_MOST", 0.3)
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    quiet_until_said(brain, made, True)
    time.sleep(0.4)
    made.wake.say()
    assert wait_for(lambda: brain.messages("wake"))
    brain.say("stop_listening")


def test_losing_the_brain_ends_being_quiet(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    quiet_until_said(brain, made, True)
    brain.drop()
    assert wait_for(lambda: made._quiet_until == 0)


def test_a_press_still_works_while_nova_speaks_on_another_satellite(brain, make):
    made = pressable(brain, make)
    quiet_until_said(brain, made, True)
    made._pressed()
    assert wait_for(lambda: brain.messages("wake"))
    brain.say("stop_listening")


# Windows' media controls - see docs/decisions/0031

class FakeWindowsMedia:
    """Stands in for WindowsMedia, keeping what it was asked to show - available unless the test says not."""

    available = True
    made = []

    def __init__(self, on_button):
        self.on_button = on_button
        self.shown = []
        self.closed = False
        FakeWindowsMedia.made.append(self)

    def show(self, state, track, artist, album, cover=UNCHANGED):
        self.shown.append((state, track, artist, album, "unchanged" if cover is UNCHANGED else cover))

    def close(self):
        self.closed = True


class Unavailable(FakeWindowsMedia):
    available = False


@pytest.fixture
def windows(monkeypatch):
    """Has satellites make a fake WindowsMedia, and gives the list of those made."""
    FakeWindowsMedia.made = []
    monkeypatch.setattr(satellite, "WindowsMedia", FakeWindowsMedia)
    return FakeWindowsMedia.made


def test_a_push_to_talk_satellite_with_media_controls_says_so(brain, make, windows):
    made = pressable(brain, make)
    assert made.media is windows[0]
    assert brain.hellos[0]["media_controls"] is True


def test_without_media_controls_available_the_hello_says_none(brain, make, monkeypatch):
    monkeypatch.setattr(satellite, "WindowsMedia", Unavailable)
    made = pressable(brain, make)
    assert made.media is None
    assert brain.hellos[0]["media_controls"] is False
    # And what the brain says plays is let go.
    brain.say("now_playing", state="playing", track="Song", artist="Band", album="Album")
    brain.say("ping")
    time.sleep(0.2)
    assert connected(made)


def test_a_wake_word_satellite_never_makes_media_controls(brain, make, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Made for a wake word satellite")

    monkeypatch.setattr(satellite, "WindowsMedia", refuse)
    made = make()
    assert wait_for(lambda: connected(made))
    assert made.media is None
    assert brain.hellos[0]["media_controls"] is False


def test_what_the_brain_says_plays_is_shown_with_the_cover_only_when_it_changes(brain, make, windows):
    made = pressable(brain, make)
    brain.say("now_playing", b"jpeg", state="playing", track="Song", artist="Band", album="Album", cover=True)
    brain.say("now_playing", state="paused", track="Song", artist="Band", album="Album")
    brain.say("now_playing", state="playing", track="Bare", artist="Band", album="Album", cover=False)
    brain.say("now_playing", state="stopped", track="", artist="", album="")
    assert wait_for(lambda: len(made.media.shown) == 4)
    assert made.media.shown == [("playing", "Song", "Band", "Album", b"jpeg"),
                                ("paused", "Song", "Band", "Album", "unchanged"),
                                ("playing", "Bare", "Band", "Album", None),
                                ("stopped", "", "", "", "unchanged")]


def test_a_now_playing_which_makes_no_sense_is_let_go(brain, make, windows, caplog):
    made = pressable(brain, make)
    with caplog.at_level(logging.WARNING):
        brain.say("now_playing", state="rewinding", track="Song", artist="Band", album="Album")
        brain.say("now_playing", track="Song")
        # Names which aren't text are shown as nothing.
        brain.say("now_playing", state="paused", track=7, artist=None, album=["x"])
        assert wait_for(lambda: made.media.shown)
    assert made.media.shown == [("paused", "", "", "", "unchanged")]
    assert len([record for record in caplog.records if "now_playing" in record.getMessage()]) == 2


@pytest.mark.parametrize("action", ["play", "pause", "next", "previous"])
def test_a_button_of_windows_media_controls_goes_to_the_brain(brain, make, windows, action):
    made = pressable(brain, make)
    made.media.on_button(action)
    assert wait_for(lambda: brain.messages("media"))
    assert brain.messages("media") == [{"type": "media", "action": action}]


def test_a_button_with_no_brain_goes_nowhere(brain, make, windows):
    made = pressable(brain, make)
    brain.drop()
    assert wait_for(lambda: not connected(made))
    made.media.on_button("pause")
    assert brain.messages("media") == []


def test_losing_the_brain_clears_the_media_controls(brain, make, windows):
    # The music stops with it, as nothing more of it arrives.
    made = pressable(brain, make)
    brain.say("now_playing", state="playing", track="Song", artist="Band", album="Album")
    assert wait_for(lambda: made.media.shown)
    brain.drop()
    assert wait_for(lambda: made.media.shown[-1][0] == "stopped")


def test_the_media_controls_are_closed_with_the_satellite(brain, make, windows):
    made = pressable(brain, make)
    media = made.media
    made.stop()
    assert wait_for(lambda: made.exited)
    assert media.closed


# Opening and closing things on the PC, and its own volume

class FakePcWindows:
    """Stands in for pc_windows: some programs open, and what was opened, closed and brought to the front."""

    def __init__(self, loads=True):
        self.loads = loads
        self.open = ["Discord", "Notepad"]
        self.front = "Notepad"
        self.started = []
        self.fronted = []
        self.closed = []
        self.fronts_closed = 0
        self.hidden = []
        self.fronts_hidden = 0
        self.installed = None

    def load(self):
        return self.loads

    def set_installed(self, items):
        self.installed = [dict(item) for item in items]

    def open_windows(self):
        return list(self.open)

    def close(self, name):
        found = next((each for each in self.open if each.lower() == name.lower()), None)
        if found is not None:
            self.closed.append(found)
        return found

    def close_front(self):
        self.fronts_closed += 1
        return self.front

    def hide(self, name):
        found = next((each for each in self.open if each.lower() == name.lower()), None)
        if found is not None:
            self.hidden.append(found)
        return found

    def hide_front(self):
        self.fronts_hidden += 1
        return self.front

    def ready_to_open(self):
        return frozenset({1})

    def start(self, target):
        self.started.append(target)

    def bring_to_front(self, before, browser=False):
        self.fronted.append((before, browser))


class FakeSystemVolume:
    """Stands in for SystemVolume, keeping each change asked of it."""

    available = True

    def __init__(self):
        self.changes = []
        self.closed = False
        self.muted = False
        self.unmutes = 0
        self.fail = False
        self.log = None
        self.mic_muted = False
        self.mic_unmutes = 0
        self.mic_fail = False
        self.mic_changes = []
        # Counts the audio the brain has had, when given, so a test can see the microphone is unmuted before any.
        self.recordings = None
        self.audio_before_mic_unmute = None

    def unmute_if_muted(self):
        if self.fail:
            raise TimeoutError("slow")
        if not self.muted:
            return False
        self.muted = False
        self.unmutes += 1
        if self.log is not None:
            self.log.append("unmute")
        return True

    def unmute_microphone_if_muted(self):
        if self.mic_fail:
            raise OSError("no microphone")
        if not self.mic_muted:
            return False
        self.mic_muted = False
        self.mic_unmutes += 1
        if self.recordings is not None:
            self.audio_before_mic_unmute = len(self.recordings())
        if self.log is not None:
            self.log.append("mic unmute")
        return True

    def microphone_muted(self):
        if self.mic_fail:
            raise OSError("no microphone")
        return self.mic_muted

    def mute_microphone(self, mute):
        if self.mic_fail:
            raise OSError("no microphone")
        self.mic_changes.append(mute)
        self.mic_muted = mute
        if self.log is not None:
            self.log.append("mic mute" if mute else "mic on")
        return mute

    def change(self, **change):
        self.changes.append(change)
        return {"level": 30, "muted": change.get("mute") is True}

    def close(self):
        self.closed = True


class NoSystemVolume(FakeSystemVolume):
    available = False


class MutedMicVolume(FakeSystemVolume):
    """Windows' volume with its microphone muted, as Windows keeps it across a restart of the satellite."""

    def __init__(self):
        super().__init__()
        self.mic_muted = True


PC_APPS = [{"id": "aaaaaaaaaaaa", "name": "Fortnite", "kind": "epic",
            "target": "com.epicgames.launcher://apps/fn%3Ax%3AFortnite?action=launch&silent=true",
            "folder": "D:\\Gaming\\Fortnite"},
           {"id": "bbbbbbbbbbbb", "name": "Notepad", "kind": "app", "target": "shell:AppsFolder\\notepad"}]


@pytest.fixture
def pc(monkeypatch, downloads):
    """Has satellites take this for Windows, with fakes of its windows, its apps and its volume."""
    fake = FakePcWindows()
    monkeypatch.setattr(satellite, "_on_windows", lambda: True)
    monkeypatch.setattr(satellite, "pc_windows", fake)
    monkeypatch.setattr(satellite, "pc_apps", types.SimpleNamespace(catalogue=lambda: [dict(app) for app in PC_APPS]))
    monkeypatch.setattr(satellite, "SystemVolume", FakeSystemVolume)
    return fake


def computer_answers(brain):
    return brain.messages("computer_done")


def ask_computer(brain, id, **fields):
    brain.say("computer", id=id, **fields)
    assert wait_for(lambda: any(answer["id"] == id for answer in computer_answers(brain)))
    return next(answer for answer in computer_answers(brain) if answer["id"] == id)


def sent_lists(brain, kind):
    return [json.loads(payload) for message, payload in brain.received if message["type"] == kind]


def test_hello_flags_only_on_windows(brain, make, monkeypatch, downloads):
    # Not on Windows: neither, even with the Windows parts to hand.
    monkeypatch.setattr(satellite, "pc_windows", FakePcWindows())
    monkeypatch.setattr(satellite, "SystemVolume", FakeSystemVolume)
    made = pressable(brain, make)
    assert brain.hellos[-1]["opens"] is False and brain.hellos[-1]["system_volume"] is False
    assert made.system_volume is None and brain.messages("apps") == []
    made.stop()
    assert wait_for(lambda: made.exited)

    # On Windows, but with neither part loading: neither.
    monkeypatch.setattr(satellite, "_on_windows", lambda: True)
    monkeypatch.setattr(satellite, "pc_windows", FakePcWindows(loads=False))
    monkeypatch.setattr(satellite, "SystemVolume", NoSystemVolume)
    made = pressable(brain, make)
    assert brain.hellos[-1]["opens"] is False and brain.hellos[-1]["system_volume"] is False
    made.stop()
    assert wait_for(lambda: made.exited)

    # On Windows with both: both.
    monkeypatch.setattr(satellite, "pc_windows", FakePcWindows())
    monkeypatch.setattr(satellite, "SystemVolume", FakeSystemVolume)
    pressable(brain, make)
    assert brain.hellos[-1]["opens"] is True and brain.hellos[-1]["system_volume"] is True


def test_targets_never_sent(brain, make, pc):
    pressable(brain, make)
    assert wait_for(lambda: sent_lists(brain, "apps"))
    assert sent_lists(brain, "apps")[0] == [{"id": "aaaaaaaaaaaa", "name": "Fortnite", "kind": "epic"},
                                            {"id": "bbbbbbbbbbbb", "name": "Notepad", "kind": "app"}]
    assert not [payload for message, payload in brain.received if b"target" in payload or b"launcher" in payload]
    # Nor where a game is installed, which is kept here, for its windows to be named by.
    assert not [payload for message, payload in brain.received if b"folder" in payload or b"Gaming" in payload]
    assert wait_for(lambda: pc.installed is not None)
    assert pc.installed[0]["folder"] == "D:\\Gaming\\Fortnite"


def test_the_open_windows_are_sent_when_they_change(brain, make, pc, monkeypatch):
    monkeypatch.setattr(satellite, "WINDOWS_EVERY", 0.02)
    pressable(brain, make)
    assert wait_for(lambda: sent_lists(brain, "windows"))
    time.sleep(0.1)
    # Looked at again and again, but sent once, as nothing changed.
    assert sent_lists(brain, "windows") == [["Discord", "Notepad"]]
    pc.open = ["Discord"]
    assert wait_for(lambda: len(sent_lists(brain, "windows")) == 2)
    assert sent_lists(brain, "windows")[1] == ["Discord"]


def test_both_lists_are_sent_again_to_a_brain_reached_again(brain, make, pc):
    made = pressable(brain, make)
    assert wait_for(lambda: sent_lists(brain, "apps") and sent_lists(brain, "windows"))
    brain.up = False
    brain.sockets[-1].shutdown(socket.SHUT_RDWR)
    assert wait_for(lambda: not connected(made))
    brain.up = True
    assert wait_for(lambda: len(sent_lists(brain, "apps")) == 2 and len(sent_lists(brain, "windows")) == 2)


def test_open_unknown_id_refused(brain, make, pc):
    pressable(brain, make)
    assert ask_computer(brain, "computer-1", action="open", item="cccccccccccc") == {
        "type": "computer_done", "id": "computer-1", "error": "That's not on this PC any more."}
    # Not even what launches one, given as its id.
    assert ask_computer(brain, "computer-2", action="open", item=PC_APPS[1]["target"])["error"] == \
        "That's not on this PC any more."
    assert pc.started == []


def test_open_launches_from_the_list_then_brings_it_to_the_front(brain, make, pc):
    made = pressable(brain, make)
    assert wait_for(lambda: made._apps)
    assert ask_computer(brain, "computer-1", action="open", item="aaaaaaaaaaaa") == {
        "type": "computer_done", "id": "computer-1", "said": "Opening Fortnite."}
    assert pc.started == [PC_APPS[0]["target"]]
    assert wait_for(lambda: pc.fronted == [(frozenset({1}), False)])


@pytest.mark.parametrize("url", ["http://example.com", "file:///C:/Windows/System32/cmd.exe",
                                 "https://user:password@example.com/", "C:\\Windows\\notepad.exe",
                                 "https://example.com/" + "a" * 2000, "javascript:alert(1)", 7])
def test_open_url_rechecked(brain, make, pc, url):
    pressable(brain, make)
    assert ask_computer(brain, "computer-1", action="open_url", url=url)["error"] == \
        "That isn't a web address I can open."
    assert pc.started == []


def test_an_https_address_opens_in_the_browser_which_is_brought_to_the_front(brain, make, pc, caplog):
    pressable(brain, make)
    with caplog.at_level(logging.INFO):
        answer = ask_computer(brain, "computer-1", action="open_url", url="https://www.bbc.co.uk/news?private=1")
    assert answer["said"] == "Opening www.bbc.co.uk."
    assert pc.started == ["https://www.bbc.co.uk/news?private=1"]
    assert wait_for(lambda: pc.fronted == [(frozenset({1}), True)])
    # Only the host is logged.
    assert "private" not in caplog.text


def test_close_answers_with_the_name_closed_or_that_it_isnt_open(brain, make, pc):
    pressable(brain, make)
    assert ask_computer(brain, "computer-1", action="close", name="discord")["said"] == "Closing Discord."
    assert ask_computer(brain, "computer-2", action="close", name="Steam")["error"] == "Steam isn't open."
    assert pc.closed == ["Discord"]


def test_close_front_answers_with_the_program_or_nothing_to_close(brain, make, pc):
    pressable(brain, make)
    assert ask_computer(brain, "computer-1", action="close_front")["said"] == "Closing Notepad."
    pc.front = None
    assert ask_computer(brain, "computer-2", action="close_front")["error"] == "There's nothing to close."


def test_hide_answers_with_the_name_hidden_or_that_it_isnt_open(brain, make, pc):
    pressable(brain, make)
    assert ask_computer(brain, "computer-1", action="hide", name="discord")["said"] == "Hiding Discord."
    assert ask_computer(brain, "computer-2", action="hide", name="Steam")["error"] == "Steam isn't open."
    assert pc.hidden == ["Discord"]
    assert pc.closed == []


def test_hide_front_answers_with_the_program_or_nothing_to_hide(brain, make, pc):
    pressable(brain, make)
    assert ask_computer(brain, "computer-1", action="hide_front")["said"] == "Hiding Notepad."
    pc.front = None
    assert ask_computer(brain, "computer-2", action="hide_front")["error"] == "There's nothing to hide."
    assert pc.fronts_closed == 0


def test_an_action_it_doesnt_know_closes_nothing(brain, make, pc, monkeypatch):
    monkeypatch.setattr(satellite.protocol, "COMPUTER_ACTIONS", satellite.protocol.COMPUTER_ACTIONS + ("tidy",))
    pressable(brain, make)
    assert ask_computer(brain, "computer-1", action="tidy")["error"] == "I can't do that on this computer."
    assert pc.closed == [] and pc.fronts_closed == 0 and pc.hidden == [] and pc.fronts_hidden == 0


def test_volume_answers_with_the_level_and_mute(brain, make, pc):
    made = pressable(brain, make)
    assert ask_computer(brain, "computer-1", action="volume", direction="up", step=5) == {
        "type": "computer_done", "id": "computer-1", "said": "Volume 30 percent.", "level": 30, "muted": False}
    assert ask_computer(brain, "computer-2", action="volume", mute=True)["muted"] is True
    # What makes no sense is left out: a level of true, a step which is text.
    ask_computer(brain, "computer-3", action="volume", level=True, direction="down", step="lots")
    assert made.system_volume.changes == [{"direction": "up", "step": 5}, {"mute": True}, {"direction": "down"}]


def test_muting_windows_by_voice_plays_mic_off_first_and_unmuting_plays_nothing(brain, make, pc):
    made = pressable(brain, make)
    order = made.system_volume.log = []
    made.speaker.play_file = lambda path, **kwargs: (order.append(os.path.basename(path)),
                                                     types.SimpleNamespace(wait=lambda timeout=None: order.append(f"wait {timeout}")))[1]
    real_change = made.system_volume.change
    made.system_volume.change = lambda **change: (order.append(f"change {change}"), real_change(**change))[1]
    ask_computer(brain, "computer-1", action="volume", mute=True)
    # Heard to the end - for at most 1.5 s - before Windows mutes.
    assert order == ["mic_off.wav", "wait 1.5", "change {'mute': True}"]
    order.clear()
    ask_computer(brain, "computer-2", action="volume", mute=False)
    assert order == ["change {'mute': False}"]
    order.clear()
    ask_computer(brain, "computer-3", action="volume", level=20)
    assert order == ["change {'level': 20}"]


def test_a_pc_which_opens_nothing_refuses_to(brain, make, pc, monkeypatch):
    monkeypatch.setattr(satellite, "pc_windows", FakePcWindows(loads=False))
    monkeypatch.setattr(satellite, "SystemVolume", NoSystemVolume)
    pressable(brain, make)
    assert ask_computer(brain, "computer-1", action="close_front")["error"] == "I can't do that on this computer."
    assert ask_computer(brain, "computer-3", action="hide_front")["error"] == "I can't do that on this computer."
    assert ask_computer(brain, "computer-2", action="volume", level=10)["error"] == \
        "This computer's volume can't be changed."


def press_on_pc(brain, make, muted=False, fail=False, mic_muted=False, mic_fail=False):
    made = pressable(brain, make)
    made.system_volume.muted, made.system_volume.fail = muted, fail
    made.system_volume.mic_muted, made.system_volume.mic_fail = mic_muted, mic_fail
    made.system_volume.log = order = []
    made.system_volume.recordings = brain.recordings
    made.speaker.play_file = lambda *args, **kwargs: order.append("chime")
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")
    return made, order


def test_a_press_unmutes_a_muted_windows_before_the_chime(brain, make, pc, caplog):
    caplog.set_level("INFO")
    made, order = press_on_pc(brain, make, muted=True)
    assert order == ["unmute", "chime"]
    assert made.system_volume.changes == []
    assert "Windows was muted, so it's unmuted for the press" in caplog.text


def test_a_press_leaves_windows_alone_when_it_is_not_muted(brain, make, pc):
    made, order = press_on_pc(brain, make)
    assert order == ["chime"] and made.system_volume.unmutes == 0 and made.system_volume.changes == []


def test_a_press_which_ends_a_recording_does_not_touch_the_volume(brain, make, pc):
    made = pressable(brain, make)
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    made.system_volume.muted = True
    made._pressed()
    assert wait_for(lambda: brain.messages("audio_end"))
    assert made.system_volume.unmutes == 0 and made.system_volume.muted is True


def test_a_press_works_without_a_system_volume(brain, make, monkeypatch, downloads):
    monkeypatch.setattr(satellite, "_on_windows", lambda: True)
    monkeypatch.setattr(satellite, "pc_windows", FakePcWindows())
    monkeypatch.setattr(satellite, "SystemVolume", NoSystemVolume)
    made = pressable(brain, make)
    assert made.system_volume is None
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")
    assert made.speaker.files() == [os.path.join(made.sounds_folder, "listening.wav")]


def test_a_press_still_chimes_when_windows_does_not_answer(brain, make, pc, caplog):
    made, order = press_on_pc(brain, make, muted=True, fail=True)
    assert order == ["chime"]
    assert "Couldn't check whether Windows is muted (TimeoutError)" in caplog.text


def test_mic_mutes_and_unmutes_windows_microphone(brain, make, pc):
    made = pressable(brain, make)
    assert ask_computer(brain, "computer-1", action="mic", mute=True, minutes=60) == {
        "type": "computer_done", "id": "computer-1", "said": "Microphone off.", "muted": True}
    assert ask_computer(brain, "computer-2", action="mic", mute=False) == {
        "type": "computer_done", "id": "computer-2", "said": "Microphone on.", "muted": False}
    # Only a true or false, and minutes which are a whole number above 0: anything else is
    # refused, and nothing changes.
    for index, fields in enumerate(({"mute": "yes"}, {"mute": 1}, {"mute": None}, {"mute": True, "minutes": 0},
                                    {"mute": True, "minutes": 2.5}, {"mute": True, "minutes": True})):
        assert ask_computer(brain, f"computer-{index + 3}", action="mic", **fields)["error"] == \
            "I can't do that on this computer."
    assert made.system_volume.mic_changes == [True, False]
    # The output's volume is left alone.
    assert made.system_volume.changes == []


def test_mic_says_why_not_when_windows_microphone_cant_be_reached(brain, make, pc, monkeypatch):
    made = pressable(brain, make)
    made.system_volume.mic_fail = True
    assert ask_computer(brain, "computer-1", action="mic", mute=True)["error"] == \
        "This computer's microphone can't be changed."
    assert made._mic_until is None
    monkeypatch.setattr(satellite, "SystemVolume", NoSystemVolume)
    pressable(brain, make)
    assert ask_computer(brain, "computer-2", action="mic", mute=True)["error"] == \
        "This computer's microphone can't be changed."


# His ruling of 20 Nov: a mute by Nova lasts computer.mic_mute_minutes, an hour by default.

def mic_on_sounds(made):
    return [name for name in made.speaker.files() if name.endswith("mic_on.wav")]


def test_a_mute_by_nova_runs_out_and_the_microphone_comes_back_on_with_a_sound(brain, make, pc, monkeypatch,
                                                                                caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(satellite, "MINUTE", 0.1)
    made = pressable(brain, make)
    ask_computer(brain, "computer-1", action="mic", mute=True, minutes=2)
    assert made.system_volume.mic_muted is True and os.path.exists(satellite.MIC_MUTE_FILE)
    time.sleep(0.1)
    # Not yet.
    assert made.system_volume.mic_muted is True and mic_on_sounds(made) == []
    assert wait_for(lambda: mic_on_sounds(made))
    assert made.system_volume.mic_muted is False and made.system_volume.mic_unmutes == 1
    assert mic_on_sounds(made) == [os.path.join(made.sounds_folder, "mic_on.wav")]
    assert not os.path.exists(satellite.MIC_MUTE_FILE)
    assert "Nova's mute of Windows' microphone ran out, so it's back on" in caplog.text


def test_a_mute_run_out_after_a_hand_unmute_does_nothing(brain, make, pc, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(satellite, "MINUTE", 0.05)
    made = pressable(brain, make)
    ask_computer(brain, "computer-1", action="mic", mute=True, minutes=1)
    # His mic's own touch button, or Windows.
    made.system_volume.mic_muted = False
    assert wait_for(lambda: made._mic_until is None)
    time.sleep(0.1)
    assert mic_on_sounds(made) == [] and made.system_volume.mic_unmutes == 0
    # Found turned on by hand as the wait looked, or as the hour ran out - whichever came first.
    assert ("ran out, but it was already on" in caplog.text
            or "turned back on by hand, so Nova's mute of it is over" in caplog.text)


def test_unmuting_by_voice_ends_the_mute_and_muting_again_starts_it_again(brain, make, pc, monkeypatch):
    monkeypatch.setattr(satellite, "MINUTE", 0.1)
    made = pressable(brain, make)
    ask_computer(brain, "computer-1", action="mic", mute=True, minutes=1)
    ask_computer(brain, "computer-2", action="mic", mute=False)
    assert made._mic_until is None and not os.path.exists(satellite.MIC_MUTE_FILE)
    time.sleep(0.2)
    assert mic_on_sounds(made) == []

    # A second mute starts the time again, from then.
    ask_computer(brain, "computer-3", action="mic", mute=True, minutes=3)
    first = made._mic_until
    time.sleep(0.15)
    ask_computer(brain, "computer-4", action="mic", mute=True, minutes=3)
    assert made._mic_until >= first + 0.15
    time.sleep(0.2)
    # Past when the first would have run out, still muted.
    assert made.system_volume.mic_muted is True and mic_on_sounds(made) == []
    assert wait_for(lambda: mic_on_sounds(made))
    assert made.system_volume.mic_muted is False


def test_a_mute_by_nova_outlives_a_restart_of_the_satellite(brain, make, pc, monkeypatch):
    monkeypatch.setattr(satellite, "MINUTE", 1)
    made = pressable(brain, make)
    ask_computer(brain, "computer-1", action="mic", mute=True, minutes=1)
    until = made._mic_until
    made.stop()
    assert wait_for(lambda: made.exited)
    assert storage.load_json(satellite.MIC_MUTE_FILE) == {"until": until}

    # Windows has kept it muted meanwhile.
    monkeypatch.setattr(satellite, "SystemVolume", MutedMicVolume)
    again = pressable(brain, make)
    assert again._mic_until == until and mic_on_sounds(again) == []
    assert wait_for(lambda: mic_on_sounds(again))
    assert again.system_volume.mic_muted is False and not os.path.exists(satellite.MIC_MUTE_FILE)


def test_a_mute_which_ran_out_while_the_satellite_was_off_ends_as_it_starts(brain, make, pc, monkeypatch):
    storage.save_json(satellite.MIC_MUTE_FILE, {"until": time.time() - 5})
    monkeypatch.setattr(satellite, "SystemVolume", MutedMicVolume)
    made = make(satellite={"trigger": "push_to_talk"})
    assert wait_for(lambda: mic_on_sounds(made))
    assert made.system_volume.mic_muted is False


# A press while the microphone is muted: on for the turn, muted again when the brain ends it.

def end_turn(brain):
    """The brain's end of a turn, after any question and its answer."""
    brain.say("allow", kind="announcement")


def test_a_press_unmutes_a_muted_microphone_for_the_turn_only(brain, make, pc, caplog):
    caplog.set_level("INFO")
    made, order = press_on_pc(brain, make, mic_muted=True)
    # On before the press sound, which comes before any of the recording is sent.
    assert order == ["mic unmute", "chime"]
    assert made.system_volume.audio_before_mic_unmute == 0
    assert "Windows' microphone was muted, so it's unmuted for this turn" in caplog.text
    # Still on once the recording has ended: the turn isn't over until the brain says.
    time.sleep(0.2)
    assert made.system_volume.mic_muted is False
    end_turn(brain)
    assert wait_for(lambda: "mic mute" in order)
    assert order == ["mic unmute", "chime", "mic mute"] and made.system_volume.mic_muted is True
    assert "Windows' microphone is muted again, as it was before the press" in caplog.text


def test_the_microphone_stays_on_for_the_answer_to_a_question(brain, make, pc):
    # "The mic or the audio?", or "Shut down the PC?": heard without another press.
    made, order = press_on_pc(brain, make, mic_muted=True)
    assert wait_for(lambda: made._recording is None)
    sent = len(brain.recordings())
    brain.say("listen")
    assert wait_for(lambda: len(brain.recordings()) >= sent + 2)
    assert made.system_volume.mic_muted is False
    brain.say("stop_listening")
    time.sleep(0.2)
    assert made.system_volume.mic_muted is False and made.system_volume.mic_changes == []
    end_turn(brain)
    assert wait_for(lambda: made.system_volume.mic_muted is True)
    assert made.system_volume.mic_changes == [True]


def test_losing_the_brain_mid_turn_mutes_the_microphone_again(brain, make, pc):
    made, order = press_on_pc(brain, make, mic_muted=True)
    assert made.system_volume.mic_muted is False
    brain.drop()
    assert wait_for(lambda: made.system_volume.mic_muted is True)
    assert made.system_volume.mic_changes == [True]


def test_stopping_the_satellite_mid_turn_mutes_the_microphone_again(brain, make, pc):
    made, order = press_on_pc(brain, make, mic_muted=True)
    volume = made.system_volume
    made.stop()
    assert wait_for(lambda: made.exited)
    assert volume.mic_muted is True and volume.closed


def test_the_end_of_the_turn_before_doesnt_mute_a_press_still_recording(brain, make, pc):
    made = pressable(brain, make)
    made.longest_request = 30
    made.system_volume.mic_muted = True
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    # The allow of the turn before, arriving as this one began.
    end_turn(brain)
    time.sleep(0.2)
    assert made.system_volume.mic_muted is False
    brain.say("stop_listening")
    end_turn(brain)
    assert wait_for(lambda: made.system_volume.mic_muted is True)


@pytest.mark.parametrize("ends", ["key", "limit"])
def test_however_the_recording_ends_the_turns_end_mutes_the_microphone_again(brain, make, pc, ends):
    made = pressable(brain, make)
    made.longest_request = 0.3 if ends == "limit" else 30
    made.system_volume.mic_muted = True
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    if ends == "key":
        made._pressed()
    assert wait_for(lambda: brain.messages("audio_end") == [{"type": "audio_end", "reason":
                                                             "pressed" if ends == "key" else "limit"}])
    time.sleep(0.2)
    assert made.system_volume.mic_muted is False
    end_turn(brain)
    assert wait_for(lambda: made.system_volume.mic_muted is True)
    assert made.system_volume.mic_unmutes == 1 and made.system_volume.mic_changes == [True]


def test_a_second_press_in_the_turn_still_leaves_the_mute_owed(brain, make, pc):
    # Pressed again before the turn's end - over a reply, or while the brain thinks - finds the
    # microphone on; the turn's end must still mute it.
    made, order = press_on_pc(brain, make, mic_muted=True)
    assert wait_for(lambda: made._recording is None)
    sent = len(brain.recordings())
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= sent + 2)
    brain.say("stop_listening")
    end_turn(brain)
    assert wait_for(lambda: made.system_volume.mic_muted is True)


def test_a_press_the_brain_never_hears_mutes_the_microphone_again(brain, make, pc, monkeypatch):
    # Lost just as the press began: no turn, and maybe no disconnect, follows to mute it.
    made = pressable(brain, make)
    made.system_volume.mic_muted = True
    sending = made.link.send
    monkeypatch.setattr(made.link, "send", lambda kind, *args, **fields: False if kind == "audio"
                        else sending(kind, *args, **fields))
    made._pressed()
    assert wait_for(lambda: made.system_volume.mic_unmutes == 1)
    assert wait_for(lambda: made.system_volume.mic_muted is True)
    assert made.system_volume.mic_changes == [True]


def test_a_hand_mute_during_a_press_is_undone_when_the_hour_runs_out(brain, make, pc, monkeypatch):
    monkeypatch.setattr(satellite, "MINUTE", 0.6)
    made = pressable(brain, make)
    ask_computer(brain, "computer-1", action="mic", mute=True, minutes=1)
    made.longest_request = 30
    made._pressed()
    assert wait_for(lambda: made._mic_held_open)
    # His mic's touch button, during the turn.
    made.system_volume.mic_muted = True
    assert wait_for(lambda: mic_on_sounds(made))
    # The sound says it's on, so it is.
    assert made.system_volume.mic_muted is False
    brain.say("stop_listening")


def test_a_hand_mute_after_a_hand_unmute_is_never_undone_by_the_hour(brain, make, pc, monkeypatch, caplog):
    # Nova mutes at 10:00, he unmutes by touch at 10:05 and mutes by touch at 10:30: at 11:00 his
    # own mute stands, and nothing plays.
    caplog.set_level("INFO")
    monkeypatch.setattr(satellite, "MINUTE", 0.6)
    monkeypatch.setattr(satellite, "MIC_LOOK_EVERY", 0.02)
    made = pressable(brain, make)
    ask_computer(brain, "computer-1", action="mic", mute=True, minutes=1)
    time.sleep(0.05)
    made.system_volume.mic_muted = False
    assert wait_for(lambda: made._mic_until is None)
    assert not os.path.exists(satellite.MIC_MUTE_FILE)
    assert "turned back on by hand, so Nova's mute of it is over" in caplog.text
    made.system_volume.mic_muted = True
    time.sleep(0.8)
    assert made.system_volume.mic_muted is True and made.system_volume.mic_unmutes == 0
    assert mic_on_sounds(made) == []


def test_a_press_holding_the_microphone_open_doesnt_end_the_hour(brain, make, pc, monkeypatch):
    monkeypatch.setattr(satellite, "MINUTE", 60)
    monkeypatch.setattr(satellite, "MIC_LOOK_EVERY", 0.02)
    made = pressable(brain, make)
    ask_computer(brain, "computer-1", action="mic", mute=True, minutes=60)
    until = made._mic_until
    made.longest_request = 30
    made._pressed()
    assert wait_for(lambda: made._mic_held_open and made.system_volume.mic_muted is False)
    time.sleep(0.2)
    assert made._mic_until == until
    brain.say("stop_listening")
    end_turn(brain)
    assert wait_for(lambda: made.system_volume.mic_muted is True)
    time.sleep(0.1)
    assert made._mic_until == until


def test_a_microphone_which_cant_be_looked_at_is_logged_once(brain, make, pc, monkeypatch, caplog):
    monkeypatch.setattr(satellite, "MIC_LOOK_EVERY", 0.02)
    made = pressable(brain, make)
    ask_computer(brain, "computer-1", action="mic", mute=True, minutes=60)
    until = made._mic_until
    made.system_volume.mic_fail = True
    time.sleep(0.2)
    assert caplog.text.count("Couldn't look at whether Windows' microphone is muted (OSError)") == 1
    assert made._mic_until == until


def test_the_wait_for_the_hour_looks_at_the_clock_again(brain, make, pc, monkeypatch):
    # Windows counts a wait's time only while the PC is awake, so after a sleep the clock has
    # moved on further than the wait knows: it's looked at again every MIC_LOOK_EVERY.
    monkeypatch.setattr(satellite, "MIC_LOOK_EVERY", 0.05)
    made = pressable(brain, make)
    ask_computer(brain, "computer-1", action="mic", mute=True, minutes=60)
    time.sleep(0.1)
    # The PC slept past it: the clock says it's due, with nothing to wake the wait.
    with made._mic_lock:
        made._mic_until = time.time() - 1
    assert wait_for(lambda: mic_on_sounds(made))
    assert made.system_volume.mic_muted is False


def test_a_press_keeps_the_hour_of_a_mute_by_nova(brain, make, pc, monkeypatch):
    monkeypatch.setattr(satellite, "MINUTE", 60)
    made = pressable(brain, make)
    ask_computer(brain, "computer-1", action="mic", mute=True, minutes=60)
    until = made._mic_until
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")
    end_turn(brain)
    assert wait_for(lambda: made.system_volume.mic_changes == [True, True])
    assert made.system_volume.mic_muted is True and made._mic_until == until


def test_a_mute_running_out_during_a_press_leaves_the_microphone_on(brain, make, pc, monkeypatch):
    monkeypatch.setattr(satellite, "MINUTE", 0.6)
    made = pressable(brain, make)
    ask_computer(brain, "computer-1", action="mic", mute=True, minutes=1)
    # Long enough that it's the brain which ends it.
    made.longest_request = 30
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    assert made._mic_held_open
    # The hour runs out while he talks: its sound plays, and the turn's end leaves it on.
    assert wait_for(lambda: mic_on_sounds(made))
    brain.say("stop_listening")
    end_turn(brain)
    time.sleep(0.2)
    assert made.system_volume.mic_muted is False and made.system_volume.mic_changes == [True]


def test_unmuting_by_voice_after_a_press_leaves_the_microphone_on(brain, make, pc):
    made = pressable(brain, make)
    made.system_volume.mic_muted = True
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")
    # "Unmute the mic", said in the turn: the press's mute again is let go.
    ask_computer(brain, "computer-1", action="mic", mute=False)
    end_turn(brain)
    time.sleep(0.2)
    assert made.system_volume.mic_muted is False and made.system_volume.mic_changes == [False]


def test_a_press_leaves_the_microphone_alone_when_it_is_not_muted(brain, make, pc):
    made, order = press_on_pc(brain, make)
    time.sleep(0.1)
    assert order == ["chime"] and made.system_volume.mic_unmutes == 0 and made.system_volume.mic_changes == []


def test_a_press_goes_on_when_the_microphone_cant_be_reached(brain, make, pc, caplog):
    made, order = press_on_pc(brain, make, muted=True, mic_fail=True)
    assert order == ["unmute", "chime"]
    assert "Couldn't check whether Windows' microphone is muted (OSError)" in caplog.text


def test_the_system_volume_is_closed_with_the_satellite(brain, make, pc):
    made = pressable(brain, make)
    volume = made.system_volume
    made.stop()
    assert wait_for(lambda: made.exited)
    assert volume.closed


# Nova's orb on the PC's screen - see voice_assistant/screen_orb.py

class FakeScreenOrb:
    """
    Stands in for ScreenOrb, keeping what it was asked to show, and when, and to configure -
    available unless a test says not.
    """

    available = True
    made = []

    def __init__(self):
        self.shown = []
        self.shown_at = []
        self.configured = []
        self.closed = False
        FakeScreenOrb.made.append(self)

    def show(self, state):
        self.shown_at.append(time.monotonic())
        self.shown.append(state)

    def configure(self, on, place, size):
        self.configured.append((on, place, size))

    def close(self):
        self.closed = True


class UnavailableScreenOrb(FakeScreenOrb):
    available = False


@pytest.fixture
def orbs(monkeypatch):
    """Has satellites make a fake ScreenOrb, and gives the list of those made."""
    FakeScreenOrb.made = []
    monkeypatch.setattr(satellite, "ScreenOrb", FakeScreenOrb)
    return FakeScreenOrb.made


def say_a_reply(brain, id="reply-1", kind="reply"):
    """The brain starts streaming some words, of a kind - which end with stream_end."""
    brain.say("play_stream", id=id, rate=22050, channels=1, volume=1.0, kind=kind, paused=False)
    brain.say("stream_audio", np.array([1, 2, 3], np.int16).tobytes(), id=id)


def test_a_press_shows_listening_thinking_speaking_then_hides(brain, make, pc, orbs):
    made = pressable(brain, make)
    orb = made.orb
    assert orb is orbs[0]
    made._pressed()
    assert wait_for(lambda: orb.shown == ["listening"])
    brain.say("stop_listening")
    assert wait_for(lambda: orb.shown == ["listening", "thinking"])
    say_a_reply(brain)
    assert wait_for(lambda: orb.shown == ["listening", "thinking", "speaking"])
    brain.say("stream_end", id="reply-1")
    assert wait_for(lambda: orb.shown == ["listening", "thinking", "speaking", "hidden"])
    brain.say("allow", kind="reply")
    # Nothing more to show: the turn's end finds the orb already hidden.
    time.sleep(0.2)
    assert orb.shown == ["listening", "thinking", "speaking", "hidden"]


def test_the_press_chime_and_music_never_show_speaking(brain, make, pc, orbs):
    made = pressable(brain, make)
    orb = made.orb
    made._pressed()
    assert wait_for(lambda: orb.shown == ["listening"])
    # The press's own chime, from the satellite itself, which never reaches the orb.
    assert made.speaker.files() == [os.path.join(made.sounds_folder, "listening.wav")]
    brain.say("stop_listening")
    assert wait_for(lambda: orb.shown == ["listening", "thinking"])

    # Sent while the orb thinks, and not while it listens, which outranks speaking and
    # would hide them: a sound which counted as Nova's words would show speaking now, and
    # end the thinking. An announcement's chime is a sound; music is a stream, but not words.
    brain.say("play_sound", id="chime-1", name="listening.wav", volume=1.0, kind="announcement")
    say_a_reply(brain, id="music-1", kind="music")
    brain.say("stream_end", id="music-1")
    assert wait_for(lambda: {message["id"] for message in brain.messages("played")} == {"chime-1", "music-1"})
    assert orb.shown == ["listening", "thinking"]

    brain.say("allow", kind="announcement")
    assert wait_for(lambda: orb.shown == ["listening", "thinking", "hidden"])
    time.sleep(0.2)
    assert orb.shown == ["listening", "thinking", "hidden"]


def test_a_chime_and_music_with_no_press_show_nothing(brain, make, pc, orbs):
    made = pressable(brain, make)
    orb = made.orb
    brain.say("play_sound", id="chime-1", name="listening.wav", volume=1.0, kind="announcement")
    say_a_reply(brain, id="music-1", kind="music")
    assert wait_for(lambda: len(made.speaker.playing) == 2 and made.speaker.streams()[0].pieces)
    # Played out, as they would be - each reported to the brain once it has.
    for playback in made.speaker.playing:
        playback.finish()
    assert wait_for(lambda: {message["id"] for message in brain.messages("played")} == {"chime-1", "music-1"})
    assert orb.shown == []


def test_a_spoken_announcement_shows_speaking(brain, make, pc, orbs):
    made = pressable(brain, make)
    orb = made.orb
    say_a_reply(brain, id="announcement-1", kind="announcement")
    assert wait_for(lambda: orb.shown == ["speaking"])
    brain.say("stream_end", id="announcement-1")
    assert wait_for(lambda: orb.shown == ["speaking", "hidden"])


def test_a_spoken_stream_refused_at_once_neither_speaks_nor_ends_thinking(brain, make, pc, orbs):
    # A press silences announcements until the brain's allow, so one sent while Nova works
    # on the press is over before it starts - as is any stream while the speaker can't
    # open. No words have come, so the orb thinks on.
    made = pressable(brain, make)
    orb = made.orb
    made._pressed()
    assert wait_for(lambda: orb.shown == ["listening"])
    brain.say("stop_listening")
    assert wait_for(lambda: orb.shown == ["listening", "thinking"])
    say_a_reply(brain, id="announcement-1", kind="announcement")
    assert wait_for(lambda: [message["id"] for message in brain.messages("played")] == ["announcement-1"])
    assert orb.shown == ["listening", "thinking"]
    brain.say("allow", kind="announcement")
    assert wait_for(lambda: orb.shown == ["listening", "thinking", "hidden"])


def test_the_orb_stops_speaking_before_the_brain_hears_the_reply_has_played(brain, make, pc, orbs):
    # The brain answers "played" with whatever comes next, so the orb must already be told.
    made = pressable(brain, make)
    orb = made.orb
    seen = []
    send = made.link.send

    def send_noting_the_orb(message_type, *args, **fields):
        if message_type == "played":
            seen.append(list(orb.shown))
        return send(message_type, *args, **fields)

    made.link.send = send_noting_the_orb
    say_a_reply(brain)
    assert wait_for(lambda: orb.shown == ["speaking"])
    brain.say("stream_end", id="reply-1")
    assert wait_for(lambda: seen)
    assert seen == [["speaking", "hidden"]]


def test_a_press_over_a_reply_goes_from_speaking_to_listening_without_a_blink(brain, make, pc, orbs):
    made = pressable(brain, make)
    orb = made.orb
    say_a_reply(brain)
    assert wait_for(lambda: orb.shown == ["speaking"])
    made._pressed()
    assert wait_for(lambda: orb.shown[-1] == "listening")
    # The reply, stopped by the press, has ended by now, and changes nothing more.
    assert wait_for(lambda: [message["stopped"] for message in brain.messages("played")] == [True])
    time.sleep(0.1)
    # The reply's end is told from its own thread once the press has let go of _waking, and
    # the listening from the main loop just after, so either may come first. Listening
    # outranks speaking, so the orb goes straight to it; or it is hidden for the moment
    # between, which the ScreenOrb's linger covers, so nothing blinks.
    assert orb.shown in (["speaking", "listening"], ["speaking", "hidden", "listening"])
    if orb.shown[1] == "hidden":
        assert orb.shown_at[2] - orb.shown_at[1] < LINGER


def test_a_follow_up_shows_listening(brain, make, pc, orbs):
    made = pressable(brain, make)
    orb = made.orb
    made._pressed()
    assert wait_for(lambda: orb.shown == ["listening"])
    brain.say("stop_listening")
    say_a_reply(brain)
    brain.say("stream_end", id="reply-1")
    assert wait_for(lambda: orb.shown == ["listening", "thinking", "speaking", "hidden"])
    assert wait_for(lambda: made._recording is None)

    # Nova asks a question, and the answer is heard.
    brain.say("listen", recording="listen-1")
    assert wait_for(lambda: orb.shown[-1] == "listening")
    assert orb.shown == ["listening", "thinking", "speaking", "hidden", "listening"]
    brain.say("stop_listening", recording="listen-1")
    assert wait_for(lambda: orb.shown[-1] == "thinking")


def test_a_second_press_shows_thinking(brain, make, pc, orbs):
    made = pressable(brain, make)
    orb = made.orb
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    made._pressed()
    assert wait_for(lambda: orb.shown == ["listening", "thinking"])
    assert wait_for(lambda: made._recording is None)
    # The recording's end, which the main loop sees later, doesn't change it.
    time.sleep(0.1)
    assert orb.shown == ["listening", "thinking"]


def test_a_recording_which_reaches_its_limit_shows_thinking(brain, make, pc, orbs):
    made = pressable(brain, make)
    orb = made.orb
    made.longest_request = 1
    made._pressed()
    assert wait_for(lambda: brain.messages("audio_end"))
    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "limit"}]
    assert wait_for(lambda: made._recording is None)
    assert orb.shown == ["listening", "thinking"]


def test_a_turn_with_nothing_to_say_does_not_leave_the_orb_thinking(brain, make, pc, orbs):
    # The brain ends such a turn with an allow straight after its stop_listening, on the
    # same thread - which can come before the main loop has even seen the recording is over.
    # So the recording's end is told where it's set, not once the main loop notices.
    made = pressable(brain, make)
    orb = made.orb
    holding, blocked, let_go = threading.Event(), threading.Event(), threading.Event()
    listen = made.microphone.listen

    def listen_holding(start=None):
        reader = listen(start)
        read = reader.read

        def read_holding(count):
            audio = read(count)
            if holding.is_set():
                blocked.set()
                let_go.wait(5)
            return audio

        reader.read = read_holding
        return reader

    made.microphone.listen = listen_holding
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    holding.set()
    try:
        # The main loop is now stuck, with the recording set but not yet seen to be over.
        assert blocked.wait(5)
        brain.say("stop_listening")
        brain.say("allow", kind="reply")
        assert wait_for(lambda: orb.shown == ["listening", "thinking", "hidden"])
    finally:
        let_go.set()
    assert wait_for(lambda: made._recording is None and not made.microphone.holds)
    time.sleep(0.1)
    assert orb.shown == ["listening", "thinking", "hidden"]


def test_the_screen_orb_setting_reaches_the_orb(brain, make, pc, orbs, caplog):
    made = pressable(brain, make)
    with caplog.at_level("WARNING", logger="satellite"):
        brain.say("state", volumes={}, muted=False, screen_orb={"on": False, "place": "top left", "size": "small"})
        assert wait_for(lambda: made.orb.configured == [(False, "top left", "small")])
        assert not [record for record in caplog.records if "screen_orb" in record.getMessage()]

        # One which makes no sense is refused, and said once however often the brain repeats it.
        for muted in (True, False, True):
            brain.say("state", volumes={}, muted=muted, screen_orb={"on": True, "place": "middle", "size": "small"})
            assert wait_for(lambda: made.state.muted is muted)

    assert made.orb.configured == [(False, "top left", "small")]
    said = [record.getMessage() for record in caplog.records if "screen_orb" in record.getMessage()]
    assert len(said) == 1 and "middle" in said[0]


@pytest.mark.parametrize("sent", [{"screen_orb": "on"}, {"screen_orb": None}, {"screen_orb": []}, {"screen_orb": {}},
                                  {"screen_orb": {"on": "yes", "place": "top left", "size": "small"}},
                                  {"screen_orb": {"on": 1, "place": "top left", "size": "small"}},
                                  {"screen_orb": {"on": True, "place": "top left"}},
                                  {"screen_orb": {"on": True, "place": "top left", "size": "huge"}},
                                  {"screen_orb": {"on": True, "place": "top left", "size": ["small"]}},
                                  {"screen_orb": {"on": True, "place": 3, "size": "small"}},
                                  {}],
                         ids=["text", "null", "list", "empty", "text_on", "number_on", "no_size", "bad_size",
                              "list_size", "number_place", "none"])
def test_a_screen_orb_setting_which_makes_no_sense_is_ignored(brain, make, pc, orbs, sent):
    made = pressable(brain, make)
    brain.say("state", volumes={}, muted=True, **sent)
    assert wait_for(lambda: made.state.muted is True)
    assert made.orb.configured == []
    # And a good one afterwards still counts, so the bad one didn't wedge the state.
    brain.say("state", volumes={}, muted=False, screen_orb={"on": True, "place": "top right", "size": "large"})
    assert wait_for(lambda: made.orb.configured == [(True, "top right", "large")])


def test_losing_the_brain_hides_the_orb(brain, make, pc, orbs):
    made = pressable(brain, make)
    orb = made.orb
    made._pressed()
    assert wait_for(lambda: orb.shown == ["listening"])
    brain.drop()
    assert wait_for(lambda: orb.shown == ["listening", "hidden"])
    # The recording which was being sent ends too, and has nothing more to say.
    assert wait_for(lambda: made._recording is None)
    time.sleep(0.1)
    assert orb.shown == ["listening", "hidden"]


def test_losing_the_brain_while_nova_thinks_or_speaks_hides_the_orb(brain, make, pc, orbs):
    made = pressable(brain, make)
    orb = made.orb
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")
    say_a_reply(brain)
    assert wait_for(lambda: orb.shown == ["listening", "thinking", "speaking"])
    brain.drop()
    assert wait_for(lambda: orb.shown[-1] == "hidden")
    time.sleep(0.1)
    assert orb.shown == ["listening", "thinking", "speaking", "hidden"]


def test_no_orb_on_a_wake_word_satellite_or_off_windows(brain, make, pc, orbs, monkeypatch):
    # The wake word, on Windows: not the PC, which is pressed.
    made = make(start=False)
    assert made.wake is not None and made.orb is None and orbs == []
    # Pressed, but not on Windows.
    monkeypatch.setattr(satellite, "_on_windows", lambda: False)
    made = make(start=False, satellite={"trigger": "push_to_talk"})
    assert made.wake is None and made.orb is None and orbs == []
    # And on Windows, pressed, it has one.
    monkeypatch.setattr(satellite, "_on_windows", lambda: True)
    made = make(start=False, satellite={"trigger": "push_to_talk"})
    assert made.orb is orbs[0] and len(orbs) == 1


def test_a_wake_word_satellite_ignores_the_screen_orb_setting(brain, make, pc, orbs, caplog):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    with caplog.at_level("WARNING", logger="satellite"):
        brain.say("state", volumes={}, muted=True, screen_orb={"on": True, "place": "middle", "size": "small"})
        assert wait_for(lambda: made.wake.muted is True)
        brain.say("state", volumes={}, muted=False, screen_orb={"on": True, "place": "middle", "size": "small"})
        assert wait_for(lambda: made.wake.muted is False)
    assert not [record for record in caplog.records if "screen_orb" in record.getMessage()]
    # Its recordings go as ever, with nothing to show them on.
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")


def test_an_unavailable_orb_changes_nothing(brain, make, pc, monkeypatch):
    FakeScreenOrb.made = []
    monkeypatch.setattr(satellite, "ScreenOrb", UnavailableScreenOrb)
    made = pressable(brain, make)
    (orb,) = FakeScreenOrb.made
    assert made.orb is None

    brain.say("state", volumes={}, muted=False, screen_orb={"on": True, "place": "top left", "size": "small"})
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")
    say_a_reply(brain)
    assert wait_for(lambda: made.speaker.streams() and made.speaker.streams()[0].pieces)
    brain.say("stream_end", id="reply-1")
    assert wait_for(lambda: brain.messages("played"))
    assert not brain.messages("played")[0]["stopped"]
    brain.say("allow", kind="reply")
    assert wait_for(lambda: made._recording is None)
    assert orb.shown == [] and orb.configured == []


def test_the_orb_is_closed_with_the_satellite(brain, make, pc, orbs):
    made = pressable(brain, make)
    orb = made.orb
    made._pressed()
    assert wait_for(lambda: orb.shown == ["listening"])
    made.stop()
    assert wait_for(lambda: made.exited)
    assert orb.closed
    # Given up on part way, the recording leaves nothing showing.
    assert orb.shown[-1] == "hidden"


class FakeWindowsHello:
    """Stands in for WindowsHello: says whether it's available, and keeps what it's asked and cancelled."""

    available = True
    made = []

    def __init__(self):
        self.asked = []
        self.cancelled = []
        self.closed = False
        self.answer = True
        FakeWindowsHello.made.append(self)

    def ask(self, id, description):
        self.asked.append((id, description))
        return self.answer

    def cancel(self, id):
        self.cancelled.append(id)

    def withdrawn(self, id):
        return id in self.cancelled

    def close(self):
        self.closed = True


class NoWindowsHello(FakeWindowsHello):
    available = False


@pytest.fixture
def hello(monkeypatch, pc):
    """Has satellites take this for Windows with Hello, and gives the list of the fakes made."""
    FakeWindowsHello.made = []
    monkeypatch.setattr(satellite, "WindowsHello", FakeWindowsHello)
    return FakeWindowsHello.made


def test_the_hello_says_approves_with_hello_only(brain, make, monkeypatch, pc):
    FakeWindowsHello.made = []
    monkeypatch.setattr(satellite, "WindowsHello", NoWindowsHello)
    made = pressable(brain, make)
    assert brain.hellos[-1]["approves"] is False and made.hello is None
    made.stop()
    assert wait_for(lambda: made.exited)

    monkeypatch.setattr(satellite, "WindowsHello", FakeWindowsHello)
    made = pressable(brain, make)
    assert brain.hellos[-1]["approves"] is True and made.hello is FakeWindowsHello.made[-1]
    made.stop()
    assert wait_for(lambda: made.exited)
    assert FakeWindowsHello.made[-1].closed

    # Not on Windows: no Hello is even looked for.
    FakeWindowsHello.made = []
    monkeypatch.setattr(satellite, "_on_windows", lambda: False)
    pressable(brain, make)
    assert brain.hellos[-1]["approves"] is False and FakeWindowsHello.made == []


def test_an_approval_is_asked_and_answered(brain, make, hello):
    made = pressable(brain, make)
    brain.say("approval", id="approval-1", description="Remove 'Dentist', Tue 25 Nov 10:00")
    assert wait_for(lambda: brain.messages("approval_answer"))
    assert made.hello.asked == [("approval-1", "Remove 'Dentist', Tue 25 Nov 10:00")]
    assert [{key: answer[key] for key in ("id", "approved")} for answer in brain.messages("approval_answer")] == \
        [{"id": "approval-1", "approved": True}]

    # The same approval again is never asked or answered twice.
    brain.say("approval", id="approval-1", description="Remove 'Dentist', Tue 25 Nov 10:00")
    made.hello.answer = False
    brain.say("approval", id="approval-2", description="Remove 'Gym'")
    assert wait_for(lambda: len(brain.messages("approval_answer")) == 2)
    time.sleep(0.2)
    assert [answer["id"] for answer in brain.messages("approval_answer")] == ["approval-1", "approval-2"]
    assert brain.messages("approval_answer")[1]["approved"] is False
    assert made.hello.asked == [("approval-1", "Remove 'Dentist', Tue 25 Nov 10:00"), ("approval-2", "Remove 'Gym'")]


def test_no_answer_from_hello_sends_nothing(brain, make, hello):
    # Hello busy, its reader missing, a fault: nothing is sent, so the brain goes on
    # waiting for the phone rather than taking it as a refusal.
    made = pressable(brain, make)
    made.hello.answer = None
    brain.say("approval", id="approval-1", description="Remove 'Dentist'")
    assert wait_for(lambda: made.hello.asked)
    time.sleep(0.2)
    assert brain.messages("approval_answer") == []
    made.hello.answer = False
    brain.say("approval", id="approval-2", description="Remove 'Gym'")
    assert wait_for(lambda: brain.messages("approval_answer"))
    time.sleep(0.2)
    assert [(answer["id"], answer["approved"]) for answer in brain.messages("approval_answer")] == \
        [("approval-2", False)]


def test_withdraw_cancels(brain, make, hello):
    made = pressable(brain, make)
    brain.say("approval_withdraw", id="approval-1")
    assert wait_for(lambda: made.hello.cancelled == ["approval-1"])
    # Its approval arriving after is answered with nothing: the brain has stopped waiting.
    brain.say("approval", id="approval-1", description="Remove 'Dentist'")
    brain.say("approval", id="approval-2", description="Remove 'Gym'")
    assert wait_for(lambda: brain.messages("approval_answer"))
    time.sleep(0.2)
    assert [answer["id"] for answer in brain.messages("approval_answer")] == ["approval-2"]


def test_no_hello_answers_nothing(brain, make, monkeypatch, pc, caplog):
    monkeypatch.setattr(satellite, "WindowsHello", NoWindowsHello)
    made = pressable(brain, make)
    with caplog.at_level(logging.WARNING, logger=satellite.__name__):
        brain.say("approval", id="approval-1", description="Remove 'Dentist'")
        brain.say("approval_withdraw", id="approval-1")
        assert wait_for(lambda: "approval" in caplog.text)
    time.sleep(0.2)
    assert brain.messages("approval_answer") == [] and made.hello is None
    assert len([record for record in caplog.records if "approval" in record.getMessage()]) == 1


def test_an_approval_without_an_id_answers_nothing(brain, make, hello, caplog):
    made = pressable(brain, make)
    with caplog.at_level(logging.WARNING, logger=satellite.__name__):
        brain.say("approval", description="Remove 'Dentist'")
        assert wait_for(lambda: "approval" in caplog.text)
    time.sleep(0.2)
    assert brain.messages("approval_answer") == [] and made.hello.asked == []


# Nova's tray icon, its menu and its shortcut - see voice_assistant/tray.py

class FakeTray:
    """
    Stands in for Tray, keeping what it was told. Like the real one it reads the state as soon
    as it exists, so a satellite which makes it before it has the state to give fails here.
    """

    available = True
    made = []

    def __init__(self, read_state, on_choice, on_shortcut, on_taken, icons):
        self.read_state = read_state
        self.on_choice = on_choice
        self.on_shortcut = on_shortcut
        self.on_taken = on_taken
        self.icons = icons
        self.shortcuts = []
        self.changes = []
        self.closes = 0
        self.first_state = read_state(False)
        FakeTray.made.append(self)

    def changed(self):
        self.changes.append(time.monotonic())

    def set_shortcut(self, shortcut):
        self.shortcuts.append(shortcut)

    def close(self):
        self.closes += 1


class UnavailableTray(FakeTray):
    available = False


def tray_pressable(brain, make, **sections):
    """A push-to-talk satellite with a fake tray, which has reached the brain."""
    made = pressable(brain, make, tray=FakeTray, **sections)
    assert made.tray is not None
    return made


def app_warnings(caplog, field):
    return [record for record in caplog.records if f"app {field}" in record.getMessage()]


def test_the_tray_is_made_with_the_icons_and_reads_the_state_as_it_is_made(brain, make):
    made = tray_pressable(brain, make)
    assert made.tray.icons == satellite.ICONS_FOLDER
    assert os.path.isfile(os.path.join(made.tray.icons, "nova.ico"))
    assert made.tray.on_shortcut == made._pressed and made.tray.on_choice == made._menu_chosen
    # Read before the brain was reached, with nothing known yet.
    assert made.tray.first_state.connected is False and made.tray.first_state.playing is None


def test_an_unavailable_tray_is_dropped(brain, make):
    made = pressable(brain, make, tray=UnavailableTray)
    assert made.tray is None


def test_a_wake_word_satellite_makes_no_tray(brain, make):
    FakeTray.made = []
    made = make(tray=FakeTray)
    assert wait_for(lambda: connected(made))
    assert made.tray is None and FakeTray.made == []


def test_a_push_to_talk_satellite_off_windows_makes_none_unless_given_one(brain, make):
    made = pressable(brain, make)
    assert made.tray is None


def test_the_tray_is_told_when_the_brain_is_reached_and_lost(brain, make):
    made = tray_pressable(brain, make)
    tray = made.tray
    assert wait_for(lambda: tray.changes)
    assert made._app_state().connected is True
    told = len(tray.changes)
    brain.drop()
    assert wait_for(lambda: len(tray.changes) > told)
    assert made._app_state().connected is False


def test_now_playing_sets_playing_even_without_media_controls(brain, make):
    made = tray_pressable(brain, make)
    assert made.media is None
    assert made._app_state().playing is None
    for state, playing in (("playing", True), ("paused", False), ("playing", True), ("stopped", False)):
        told = len(made.tray.changes)
        brain.say("now_playing", state=state)
        assert wait_for(lambda: made._app_state().playing is playing and len(made.tray.changes) > told)


def test_the_state_says_whether_there_are_media_controls(brain, make, windows):
    made = tray_pressable(brain, make)
    assert made.media is not None and made._app_state().music is True


def test_the_state_says_there_is_no_music_without_media_controls(brain, make):
    # The brain drops Play / Pause and Next from a satellite without them, so the menu greys them.
    made = tray_pressable(brain, make)
    assert made.media is None and made._app_state().music is False


def test_a_now_playing_which_makes_no_sense_leaves_playing_alone(brain, make):
    made = tray_pressable(brain, make)
    brain.say("now_playing", state="playing")
    assert wait_for(lambda: made._app_state().playing is True)
    brain.say("now_playing", state="rewinding")
    brain.say("state", volumes={}, muted=False)
    assert wait_for(lambda: made.state.muted is False)
    assert made._app_state().playing is True


def test_the_state_is_muted_while_windows_microphone_is(brain, make, pc, monkeypatch):
    monkeypatch.setattr(satellite, "SystemVolume", MutedMicVolume)
    made = tray_pressable(brain, make)
    assert made._app_state().mic_muted is True
    made.system_volume.mic_muted = False
    assert made._app_state(True).mic_muted is False


def test_the_state_is_not_muted_while_windows_microphone_is_on(brain, make, pc):
    made = tray_pressable(brain, make)
    assert made._app_state().mic_muted is False


def test_a_press_holding_the_microphone_open_still_counts_as_muted(brain, make, pc, monkeypatch):
    monkeypatch.setattr(satellite, "SystemVolume", MutedMicVolume)
    made = tray_pressable(brain, make)
    made._pressed()
    assert wait_for(lambda: made._mic_held_open)
    # On for the turn, and muted again at its end.
    assert made.system_volume.mic_muted is False
    assert made._app_state().mic_muted is True
    assert made._app_state(True).mic_muted is True
    brain.say("stop_listening")
    brain.say("allow", kind="reply")
    assert wait_for(lambda: not made._mic_held_open)
    assert made._app_state().mic_muted is True
    assert made.system_volume.mic_muted is True


def test_the_state_stays_muted_while_the_microphone_is_being_muted_again(brain, make, pc, monkeypatch):
    # A tray refresh while Windows is asked to mute it again must not draw "ready": nothing
    # after would put it right, as the icon was last told muted all along.
    monkeypatch.setattr(satellite, "SystemVolume", MutedMicVolume)
    made = tray_pressable(brain, make)
    made._pressed()
    assert wait_for(lambda: made._mic_held_open)
    volume = made.system_volume
    inside, release = threading.Event(), threading.Event()
    mute = volume.mute_microphone

    def slow_mute(muted):
        inside.set()
        release.wait(5)
        return mute(muted)

    volume.mute_microphone = slow_mute
    again = threading.Thread(target=made._mute_mic_again, daemon=True)
    again.start()
    try:
        assert inside.wait(5)
        assert made._app_state().mic_muted is True
    finally:
        release.set()
        again.join(5)
    assert made._app_state().mic_muted is True and not made._mic_held_open


def test_the_watch_tells_the_tray_on_every_look(brain, make, pc, monkeypatch):
    # The tray skips a look it already shows, so telling it each time heals any change missed.
    monkeypatch.setattr(satellite, "MIC_WATCH", 0.02)
    made = tray_pressable(brain, make)
    assert wait_for(lambda: made.tray.changes)
    told = len(made.tray.changes)
    assert wait_for(lambda: len(made.tray.changes) >= told + 3)
    assert made._app_state().mic_muted is False


def test_the_watch_backs_off_after_a_failed_look(brain, make, pc, monkeypatch):
    # A stuck volume thread mustn't have the lock held most of the time: after a look which
    # failed, the next is MIC_LOOK_EVERY away, until one succeeds.
    monkeypatch.setattr(satellite, "MIC_WATCH", 0.02)
    monkeypatch.setattr(satellite, "MIC_LOOK_EVERY", 0.5)
    made = tray_pressable(brain, make)
    looks = []
    volume = made.system_volume

    def look():
        looks.append(time.monotonic())
        if volume.mic_fail:
            raise TimeoutError("stuck")
        return volume.mic_muted

    # Failing before the look is swapped in, so every look counted here fails.
    volume.mic_fail = True
    volume.microphone_muted = look
    assert wait_for(lambda: looks)
    failed = len(looks)
    time.sleep(0.3)
    assert len(looks) == failed
    # Once a look succeeds it's back to every MIC_WATCH.
    volume.mic_fail = False
    assert wait_for(lambda: len(looks) >= failed + 6)


def test_a_mute_by_hand_reaches_the_tray_within_the_watch(brain, make, pc, monkeypatch):
    monkeypatch.setattr(satellite, "MIC_WATCH", 0.02)
    made = tray_pressable(brain, make)
    assert made._app_state().mic_muted is False
    told = len(made.tray.changes)
    made.system_volume.mic_muted = True
    assert wait_for(lambda: made._app_state().mic_muted is True and len(made.tray.changes) > told)
    # And back, the same way.
    told = len(made.tray.changes)
    made.system_volume.mic_muted = False
    assert wait_for(lambda: made._app_state().mic_muted is False and len(made.tray.changes) > told)


def test_a_mute_by_voice_reaches_the_tray_at_once(brain, make, pc, monkeypatch):
    # The watch is far off, so only the mute itself can have told it.
    monkeypatch.setattr(satellite, "MIC_WATCH", 1000)
    made = tray_pressable(brain, make)
    told = len(made.tray.changes)
    assert ask_computer(brain, "m1", action="mic", mute=True)["muted"] is True
    assert made._app_state().mic_muted is True and len(made.tray.changes) > told


def test_the_watch_looks_only_when_there_is_a_tray(brain, make, pc, monkeypatch):
    monkeypatch.setattr(satellite, "MIC_WATCH", 0.02)
    made = pressable(brain, make)
    assert made.tray is None
    looks = []
    made.system_volume.microphone_muted = lambda: looks.append(1) or False
    time.sleep(0.2)
    assert looks == []


def test_opening_the_menu_looks_at_the_microphone_afresh(brain, make, pc, monkeypatch):
    monkeypatch.setattr(satellite, "MIC_WATCH", 1000)
    made = tray_pressable(brain, make)
    looks = []
    volume = made.system_volume
    volume.microphone_muted = lambda: looks.append(1) or volume.mic_muted
    volume.mic_muted = True
    # Taken from the last look, which was before it was muted: nothing is asked.
    assert made._app_state().mic_muted is False and looks == []
    told = len(made.tray.changes)
    assert made._app_state(True).mic_muted is True and looks == [1]
    # That look is now what it shows, and the icon is told.
    assert made._app_state().mic_muted is True and looks == [1]
    assert len(made.tray.changes) > told


def test_the_state_never_waits_on_the_microphone_lock(brain, make, pc, monkeypatch):
    # The lock is held across Windows calls, which can take seconds when its thread is stuck;
    # the tray's thread reads the state, and must stay free to act on a press or a click.
    monkeypatch.setattr(satellite, "SystemVolume", MutedMicVolume)
    made = tray_pressable(brain, make)
    answers = []
    reader = threading.Thread(target=lambda: answers.append(made._app_state().mic_muted), daemon=True)
    with made._mic_lock:
        reader.start()
        reader.join(1)
        answered = bool(answers)
    reader.join(5)
    assert answered and answers == [True]


def test_a_microphone_that_cant_be_read_is_none(brain, make, pc, caplog):
    made = tray_pressable(brain, make)
    made.system_volume.mic_fail = True
    with caplog.at_level(logging.WARNING):
        assert made._app_state(True).mic_muted is None
        assert made._app_state(True).mic_muted is None
    assert made._app_state().mic_muted is None
    # Once, however often it's looked at.
    assert len([record for record in caplog.records if "Couldn't look at whether" in record.getMessage()]) == 1


def test_no_windows_volume_means_the_microphone_is_none(brain, make):
    made = tray_pressable(brain, make)
    assert made.system_volume is None
    assert made._app_state().mic_muted is None and made._app_state(True).mic_muted is None


def test_the_watch_stops_with_the_satellite(brain, make, pc, monkeypatch):
    monkeypatch.setattr(satellite, "MIC_WATCH", 0.02)
    made = tray_pressable(brain, make)
    made.stop()
    assert wait_for(lambda: made.exited)
    assert wait_for(lambda: not [thread for thread in threading.enumerate() if thread.name == "tray-mic"])


def test_the_default_shortcut_is_registered_at_the_start(brain, make):
    made = tray_pressable(brain, make)
    assert made.tray.shortcuts == [Shortcut(("ctrl", "alt"), "n")]
    assert made._app_state().shortcut == "Ctrl+Alt+N"


def test_app_from_the_brain_sets_the_shortcut_page_and_minutes(brain, make, pc):
    made = tray_pressable(brain, make)
    assert made._app_state().page is False
    told = len(made.tray.changes)
    brain.say("state", volumes={}, muted=False,
              app={"shortcut": "Ctrl+Shift+F5", "page": "https://nova.example", "mic_mute_minutes": 30})
    assert wait_for(lambda: made._page is not None)
    assert made.tray.shortcuts[-1] == Shortcut(("ctrl", "shift"), "f5")
    assert made._page == "https://nova.example" and made._mic_minutes == 30
    state = made._app_state()
    assert state.shortcut == "Ctrl+Shift+F5" and state.page is True
    assert len(made.tray.changes) > told
    brain.say("state", volumes={}, muted=False, app={"shortcut": " ctrl + alt + n ", "page": "bbc.co.uk"})
    assert wait_for(lambda: made._page == "https://bbc.co.uk")
    assert made.tray.shortcuts[-1] == Shortcut(("ctrl", "alt"), "n")
    assert made._app_state().shortcut == "Ctrl+Alt+N" and made._mic_minutes == 30


def test_one_bad_field_of_app_leaves_the_others_taken(brain, make, caplog):
    made = tray_pressable(brain, make)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            brain.say("state", volumes={}, muted=False,
                      app={"shortcut": 5, "page": "https://nova.example", "mic_mute_minutes": 30})
        assert wait_for(lambda: made._mic_minutes == 30)
        brain.say("state", volumes={}, muted=False, app={"shortcut": "Ctrl+A+B"})
        brain.say("state", volumes={}, muted=False, app={"mic_mute_minutes": 0, "page": "https://nova.example"})
        brain.say("state", volumes={}, muted=False, app={"mic_mute_minutes": True})
        brain.say("state", volumes={}, muted=False)
        assert wait_for(lambda: len(app_warnings(caplog, "mic_mute_minutes")) == 2)
    assert made._page == "https://nova.example" and made._mic_minutes == 30
    # Left as it was, and logged once for each value.
    assert made.tray.shortcuts == [Shortcut(("ctrl", "alt"), "n")]
    assert made._app_state().shortcut == "Ctrl+Alt+N"
    assert len(app_warnings(caplog, "shortcut")) == 2
    assert app_warnings(caplog, "page") == []


@pytest.mark.parametrize("page", ["file:///c:/x", "javascript:alert(1)", "http://nova.example", "https://a b", 5, ["https://x.y"]])
def test_a_page_which_isnt_a_web_address_is_refused(brain, make, pc, caplog, page):
    made = tray_pressable(brain, make)
    with caplog.at_level(logging.WARNING):
        brain.say("state", volumes={}, muted=False, app={"page": page, "mic_mute_minutes": 45})
        assert wait_for(lambda: made._mic_minutes == 45)
        assert made._page is None and made._app_state().page is False
        assert len(app_warnings(caplog, "page")) == 1
        brain.say("state", volumes={}, muted=False, app={"page": "https://nova.example"})
        assert wait_for(lambda: made._page is not None)
        brain.say("state", volumes={}, muted=False, app={"page": page, "mic_mute_minutes": 46})
        assert wait_for(lambda: made._mic_minutes == 46)
    # Left as it was.
    assert made._page == "https://nova.example" and made._app_state().page is True


def test_an_empty_page_clears_it(brain, make, pc):
    made = tray_pressable(brain, make)
    brain.say("state", volumes={}, muted=False, app={"page": "https://nova.example"})
    assert wait_for(lambda: made._app_state().page)
    brain.say("state", volumes={}, muted=False, app={"page": ""})
    assert wait_for(lambda: not made._app_state().page)
    assert made._page is None


def test_an_empty_shortcut_from_the_brain_means_none(brain, make):
    made = tray_pressable(brain, make)
    brain.say("state", volumes={}, muted=False, app={"shortcut": ""})
    assert wait_for(lambda: made.tray.shortcuts[-1] is None)
    assert made._app_state().shortcut is None


def test_no_app_from_an_old_brain_leaves_the_default_shortcut(brain, make):
    made = tray_pressable(brain, make)
    brain.say("state", volumes={}, muted=False)
    assert wait_for(lambda: made.state.muted is False)
    assert made.tray.shortcuts == [Shortcut(("ctrl", "alt"), "n")]
    assert made._app_state().shortcut == "Ctrl+Alt+N" and made._app_state().page is False


def test_an_app_which_isnt_an_object_is_refused_once(brain, make, caplog):
    made = tray_pressable(brain, make)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            brain.say("state", volumes={}, muted=False, app="on")
        brain.say("state", volumes={}, muted=False, app={"mic_mute_minutes": 7})
        assert wait_for(lambda: made._mic_minutes == 7)
    assert len(app_warnings(caplog, "app")) == 1


def test_the_shortcut_is_a_press(brain, make):
    made = tray_pressable(brain, make)
    made.tray.on_shortcut()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    assert brain.messages("wake")[0]["pressed"] is True
    assert made.speaker.files() == [os.path.join(made.sounds_folder, "listening.wav")]
    brain.say("stop_listening")


def test_taken_reaches_the_state_and_the_brain(brain, make):
    made = tray_pressable(brain, make)
    assert made._app_state().shortcut == "Ctrl+Alt+N"
    # Said as the brain was reached, so what follows is what changed.
    assert wait_for(lambda: brain.messages("shortcut_taken") == [{"type": "shortcut_taken", "taken": False}])
    told = len(made.tray.changes)
    made.tray.on_taken(True)
    assert made._app_state().shortcut is None
    assert wait_for(lambda: brain.messages("shortcut_taken")[1:] == [{"type": "shortcut_taken", "taken": True}])
    assert len(made.tray.changes) > told
    made.tray.on_taken(False)
    assert made._app_state().shortcut == "Ctrl+Alt+N"
    assert wait_for(lambda: [message["taken"] for message in brain.messages("shortcut_taken")] == [False, True, False])


def test_taken_said_again_is_not_sent_again(brain, make):
    made = tray_pressable(brain, make)
    assert wait_for(lambda: brain.messages("shortcut_taken"))
    made.tray.on_taken(True)
    made.tray.on_taken(True)
    assert wait_for(lambda: len(brain.messages("shortcut_taken")) == 2)
    time.sleep(0.1)
    # The one from connecting, and the one change.
    assert [message["taken"] for message in brain.messages("shortcut_taken")] == [False, True]


def test_taken_is_told_to_the_brain_again_on_each_connection(brain, make):
    made = tray_pressable(brain, make)
    # Whether taken or not: the first connection says it is not.
    assert wait_for(lambda: brain.messages("shortcut_taken") == [{"type": "shortcut_taken", "taken": False}])
    made.tray.on_taken(True)
    assert wait_for(lambda: len(brain.messages("shortcut_taken")) == 2)
    # The brain goes, and comes back as a new one which has heard nothing.
    brain.received.clear()
    brain.sockets[-1].shutdown(socket.SHUT_RDWR)
    assert wait_for(lambda: len(brain.hellos) == 2 and connected(made))
    assert wait_for(lambda: brain.messages("shortcut_taken") == [{"type": "shortcut_taken", "taken": True}])


def test_taken_reaches_the_brain_in_the_order_it_changed(brain, make, monkeypatch):
    # A connection's tell, on the link's thread, read the old value and is slow to send it;
    # the tray's change meanwhile must not reach the brain first, or the old one would stand.
    made = tray_pressable(brain, make)
    assert wait_for(lambda: brain.messages("shortcut_taken"))
    sent, entered, release = [], threading.Event(), threading.Event()
    sending = made.link.send

    def slow_send(kind, *args, **fields):
        if kind == "shortcut_taken" and not entered.is_set():
            entered.set()
            release.wait(5)
        if kind == "shortcut_taken":
            sent.append(fields["taken"])
        return sending(kind, *args, **fields)

    monkeypatch.setattr(made.link, "send", slow_send)
    connecting = threading.Thread(target=made._tell_brain_shortcut_taken, daemon=True)
    connecting.start()
    assert entered.wait(5)
    changing = threading.Thread(target=made.tray.on_taken, args=(True,), daemon=True)
    changing.start()
    time.sleep(0.1)
    release.set()
    connecting.join(5)
    changing.join(5)
    assert sent == [False, True]


def test_a_satellite_with_no_tray_says_nothing_of_the_shortcut(brain, make):
    made = pressable(brain, make)
    brain.say("state", volumes={}, muted=False, app={"shortcut": "Ctrl+Alt+M"})
    assert wait_for(lambda: made._app_state().shortcut == "Ctrl+Alt+M")
    assert brain.messages("shortcut_taken") == []


def test_at_logon_follows_the_mark(brain, make, tmp_path, monkeypatch):
    mark = tmp_path / "no_logon_start"
    monkeypatch.setattr(satellite, "NO_LOGON_MARK", str(mark))
    made = tray_pressable(brain, make)
    assert made._app_state().at_logon is True
    mark.write_text("")
    assert made._app_state().at_logon is False
    mark.unlink()
    assert made._app_state().at_logon is True


# What each item of the tray's menu does

def media_sent(brain):
    return [message["action"] for message in brain.messages("media")]


def test_listen_is_a_press(brain, make):
    made = tray_pressable(brain, make)
    made.tray.on_choice("listen")
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    assert brain.messages("wake")[0]["pressed"] is True
    brain.say("stop_listening")


@pytest.mark.parametrize("state,action", [("playing", "pause"), ("paused", "play"), ("stopped", "play")])
def test_play_pause_sends_pause_while_playing_and_play_otherwise(brain, make, state, action):
    made = tray_pressable(brain, make)
    brain.say("now_playing", state=state)
    assert wait_for(lambda: made._music_playing is (state == "playing"))
    made.tray.on_choice("play_pause")
    assert wait_for(lambda: media_sent(brain))
    assert brain.messages("media") == [{"type": "media", "action": action}]


def test_play_pause_sends_play_when_the_brain_never_said(brain, make):
    made = tray_pressable(brain, make)
    assert made._music_playing is None
    made.tray.on_choice("play_pause")
    assert wait_for(lambda: media_sent(brain))
    assert media_sent(brain) == ["play"]


def test_next_sends_next(brain, make):
    made = tray_pressable(brain, make)
    made.tray.on_choice("next")
    assert wait_for(lambda: media_sent(brain))
    assert brain.messages("media") == [{"type": "media", "action": "next"}]


def test_losing_the_brain_forgets_what_was_playing(brain, make):
    made = tray_pressable(brain, make)
    brain.say("now_playing", state="playing")
    assert wait_for(lambda: made._app_state().playing is True)
    brain.drop()
    assert wait_for(lambda: made._app_state().playing is None)


def menu_mic_sounds(made):
    return [os.path.basename(name) for name in made.speaker.files()]


def test_mute_from_the_menu_mutes_windows_microphone_for_the_minutes(brain, make, pc):
    made = tray_pressable(brain, make)
    brain.say("state", volumes={"general": 0.5}, muted=False, app={"mic_mute_minutes": 30})
    assert wait_for(lambda: made._mic_minutes == 30 and made.state.volume("general") == 0.5)
    sent = brain.kinds()
    told = len(made.tray.changes)
    made.tray.on_choice("mute")
    assert made.system_volume.mic_muted is True and made.system_volume.mic_changes == [True]
    assert abs(made._mic_until - (time.time() + 30 * 60)) < 5
    assert made.speaker.files() == [os.path.join(made.sounds_folder, "mic_off.wav")]
    assert made.speaker.playing[0].volume == 0.5
    assert len(made.tray.changes) > told
    # Nothing is sent to the brain, which learns of it as it learns of any change by hand.
    time.sleep(0.1)
    assert brain.kinds() == sent


def test_mute_from_the_menu_works_with_the_brain_away(brain, make, pc):
    made = tray_pressable(brain, make)
    brain.drop()
    assert wait_for(lambda: made._app_state().connected is False)
    made.tray.on_choice("mute")
    assert made.system_volume.mic_muted is True
    assert menu_mic_sounds(made) == ["mic_off.wav"]


def test_unmute_from_the_menu_plays_mic_on_and_ends_the_time(brain, make, pc):
    made = tray_pressable(brain, make)
    made.tray.on_choice("mute")
    assert made._mic_until is not None and os.path.exists(satellite.MIC_MUTE_FILE)
    made.tray.on_choice("mute")
    assert made.system_volume.mic_muted is False and made.system_volume.mic_changes == [True, False]
    assert made._mic_until is None and not os.path.exists(satellite.MIC_MUTE_FILE)
    assert menu_mic_sounds(made) == ["mic_off.wav", "mic_on.wav"]


def test_mute_from_the_menu_which_fails_plays_nothing(brain, make, pc, caplog):
    made = tray_pressable(brain, make)

    def fail(mute):
        raise OSError("no microphone")

    made.system_volume.mute_microphone = fail
    with caplog.at_level(logging.INFO):
        made.tray.on_choice("mute")
    assert made.speaker.files() == [] and made._mic_until is None
    assert "Couldn't change Windows' microphone" in caplog.text


def test_mute_from_the_menu_does_nothing_with_no_microphone_to_reach(brain, make, pc):
    made = tray_pressable(brain, make)
    made.system_volume.mic_fail = True
    assert made._app_state(True).mic_muted is None
    made.tray.on_choice("mute")
    assert made.speaker.files() == [] and made.system_volume.mic_changes == []


def test_settings_opens_the_page_at_satellites(brain, make, pc, monkeypatch):
    made = tray_pressable(brain, make)
    opened = []
    monkeypatch.setattr(made, "_open_in_browser", opened.append)
    brain.say("state", volumes={}, muted=False, app={"page": "https://nova.example"})
    assert wait_for(lambda: made._page is not None)
    made.tray.on_choice("settings")
    assert opened == ["https://nova.example#satellites"]
    brain.say("state", volumes={}, muted=False, app={"page": "https://nova.example/"})
    assert wait_for(lambda: made._page == "https://nova.example/")
    made.tray.on_choice("settings")
    assert opened[1:] == ["https://nova.example/#satellites"]


def test_settings_with_no_address_does_nothing(brain, make, monkeypatch, caplog):
    made = tray_pressable(brain, make)
    opened = []
    monkeypatch.setattr(made, "_open_in_browser", opened.append)
    with caplog.at_level(logging.DEBUG):
        made.tray.on_choice("settings")
    assert opened == []
    assert "no address" in caplog.text


def test_the_page_is_only_offered_where_it_can_be_opened(brain, make):
    # Here, as where Windows' parts couldn't be loaded, Nova can't open anything.
    made = tray_pressable(brain, make)
    assert made.opens is False
    brain.say("state", volumes={}, muted=False, app={"page": "https://nova.example"})
    assert wait_for(lambda: made._page is not None)
    assert made._app_state().page is False


def test_the_start_menu_ask_does_nothing_where_a_page_cant_be_opened(brain, make, pc, monkeypatch, caplog):
    # Answered all the same, so a second launch of the app isn't told of a failure.
    pc.loads = False
    monkeypatch.setattr(satellite, "PressListener",
                        lambda port, on_press, on_open=None: PressListener(0, on_press, on_open))
    made = tray_pressable(brain, make)
    assert made.opens is False
    brain.say("state", volumes={}, muted=False, app={"page": "https://nova.example"})
    assert wait_for(lambda: made._page is not None)
    assert made._app_state().page is False
    assert wait_for(lambda: made.press_listener is not None and made.press_listener.port)
    port = made.press_listener.port
    with caplog.at_level(logging.DEBUG):
        with socket.socket() as sock:
            sock.settimeout(5)
            sock.connect(("127.0.0.1", port))
            sock.sendall(f"POST /open HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Nova-Press: 1\r\n"
                         f"Content-Length: 0\r\n\r\n".encode())
            assert sock.recv(100).startswith(b"HTTP/1.0 204")
        made.tray.on_choice("settings")
    assert pc.started == []
    assert "can't open pages" in caplog.text


def test_the_page_is_opened_in_the_browser_and_brought_to_the_front(brain, make, pc):
    made = tray_pressable(brain, make)
    made._open_in_browser("https://nova.example#satellites")
    assert pc.started == ["https://nova.example#satellites"]
    assert wait_for(lambda: pc.fronted == [(frozenset({1}), True)])


def test_a_page_windows_cant_open_is_logged_and_goes_no_further(brain, make, pc, caplog):
    made = tray_pressable(brain, make)

    def refuse(target):
        raise OSError("no browser")

    pc.start = refuse
    made._open_in_browser("https://nova.example#satellites")
    assert "Windows couldn't open it" in caplog.text
    assert pc.fronted == []


def test_the_start_menu_ask_opens_the_page(brain, make, pc, monkeypatch):
    # A second launch of the app posts /open, which the real listener answers.
    monkeypatch.setattr(satellite, "PressListener",
                        lambda port, on_press, on_open=None: PressListener(0, on_press, on_open))
    made = pressable(brain, make)
    opened = []
    monkeypatch.setattr(made, "_open_in_browser", opened.append)
    brain.say("state", volumes={}, muted=False, app={"page": "https://nova.example"})
    assert wait_for(lambda: made._page is not None)
    assert wait_for(lambda: made.press_listener is not None and made.press_listener.port)
    port = made.press_listener.port
    with socket.socket() as sock:
        sock.settimeout(5)
        sock.connect(("127.0.0.1", port))
        sock.sendall(f"POST /open HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Nova-Press: 1\r\n"
                     f"Content-Length: 0\r\n\r\n".encode())
        assert sock.recv(100).startswith(b"HTTP/1.0 204")
    assert opened == ["https://nova.example#satellites"]


def test_start_with_windows_makes_and_removes_the_mark(brain, make, tmp_path, monkeypatch):
    mark = tmp_path / "no_logon_start"
    monkeypatch.setattr(satellite, "NO_LOGON_MARK", str(mark))
    made = tray_pressable(brain, make)
    assert made._app_state().at_logon is True
    told = len(made.tray.changes)
    # Ticked now: choosing it stops Nova starting, which the mark says.
    made.tray.on_choice("at_logon")
    assert mark.exists() and made._app_state().at_logon is False
    assert len(made.tray.changes) > told
    told = len(made.tray.changes)
    made.tray.on_choice("at_logon")
    assert not mark.exists() and made._app_state().at_logon is True
    assert len(made.tray.changes) > told


def test_start_with_windows_which_cant_be_saved_is_logged(brain, make, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(satellite, "NO_LOGON_MARK", str(tmp_path / "missing folder" / "no_logon_start"))
    made = tray_pressable(brain, make)
    with caplog.at_level(logging.WARNING):
        made.tray.on_choice("at_logon")
    assert "Couldn't change whether Nova starts with Windows" in caplog.text
    assert made._app_state().at_logon is True


@pytest.mark.parametrize("choice,code", [("restart", 11), ("quit", 10)])
def test_restart_and_quit_stop_with_their_codes(brain, make, choice, code):
    made = tray_pressable(brain, make)
    tray = made.tray
    made.tray.on_choice(choice)
    assert wait_for(lambda: made.exited)
    assert made.exited == [code]
    # Closed at once by the choice, so the icon goes, and again by the clean up.
    assert tray.closes == 2 and made.speaker.closed


def test_quit_while_recording_cleans_up_and_exits_with_quit_code(brain, make, pc, tmp_path):
    made = tray_pressable(brain, make)
    made.system_volume.mic_muted = True
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    assert made.system_volume.mic_muted is False
    assert os.path.exists(satellite.RUNNING_MARK)
    made.tray.on_choice("quit")
    assert wait_for(lambda: made.exited)
    assert made.exited == [10]
    # As it was before the press, and nothing left to say the satellite is running.
    assert made.system_volume.mic_muted is True and made.system_volume.closed
    assert not os.path.exists(satellite.RUNNING_MARK)


class WaitingWindowsHello(FakeWindowsHello):
    """Holds each prompt open until closed, which ends it with no, as the real WindowsHello's close does."""

    def __init__(self):
        super().__init__()
        self.waiting = threading.Event()
        self._released = threading.Event()

    def ask(self, id, description):
        self.asked.append((id, description))
        self.waiting.set()
        self._released.wait(5)
        return False

    def close(self):
        self.closed = True
        self._released.set()


def test_quit_while_an_approval_waits_exits_with_the_quit_code_and_sends_no_answer(brain, make, pc, monkeypatch):
    monkeypatch.setattr(satellite, "WindowsHello", WaitingWindowsHello)
    made = tray_pressable(brain, make)
    brain.say("approval", id="approval-1", description="Remove 'Dentist'")
    assert wait_for(lambda: made.hello.waiting.is_set())
    made.tray.on_choice("quit")
    assert wait_for(lambda: made.exited)
    assert made.exited == [satellite.QUIT_CODE]
    # Cleaned up: Hello closed, which ends the prompt, and nothing left to say the satellite is running.
    assert made.hello.closed and made.speaker.closed and not os.path.exists(satellite.RUNNING_MARK)
    # The link was stopped before Hello was closed, so the prompt's ending never reaches the brain as an answer.
    time.sleep(0.2)
    assert brain.messages("approval_answer") == []


def test_a_choice_nova_doesnt_know_is_ignored(brain, make, caplog):
    made = tray_pressable(brain, make)
    sent = brain.kinds()
    with caplog.at_level(logging.DEBUG):
        made.tray.on_choice("banana")
    assert len([record for record in caplog.records if "banana" in record.getMessage()]) == 1
    assert made.exited == [] and made.tray.closes == 0 and made.speaker.files() == []
    assert brain.kinds() == sent


def test_a_choice_which_goes_wrong_is_logged_and_never_reaches_the_tray(brain, make, caplog):
    made = tray_pressable(brain, make)

    def broken():
        raise RuntimeError("boom")

    made._pressed = broken
    with caplog.at_level(logging.ERROR):
        # Raises nothing into the tray's thread.
        made.tray.on_choice("listen")
    assert "Menu item listen failed" in caplog.text and "boom" in caplog.text


def test_the_tray_is_closed_as_the_satellite_stops(brain, make):
    made = tray_pressable(brain, make)
    tray = made.tray
    # As Quit does from the tray's own thread: closed already, and closed again by the clean up.
    tray.close()
    made.stop()
    assert wait_for(lambda: made.exited)
    assert tray.closes == 2


def test_the_tray_is_closed_after_the_link_and_before_the_speaker(brain, make):
    made = tray_pressable(brain, make)
    order = []
    link_stop, speaker_close = made.link.stop, made.speaker.close
    made.link.stop = lambda: (order.append("link"), link_stop())
    made.tray.close = lambda: order.append("tray")
    made.speaker.close = lambda: (order.append("speaker"), speaker_close())
    made.stop()
    assert wait_for(lambda: made.exited)
    assert order == ["link", "tray", "speaker"]


# Conversations - see docs/superpowers/specs/2026-11-21-conversation-mode-design.md

class FakeEcho:
    """
    Stands in for the echo canceller: engaged or not, and lined up when the test says. As the
    real one, engaging it starts it from nothing, and it is only ready while it is engaged.
    """

    def __init__(self, engaged=True):
        self._engaged = engaged
        # Each time it was switched, in order - starting as it was made.
        self.switched = []
        # Whether the gap between the speaker and the microphone has been found, which its
        # own thread does some seconds after it starts.
        self.lined_up = False
        self.played = types.SimpleNamespace()

    @property
    def engaged(self):
        return self._engaged

    @engaged.setter
    def engaged(self, on):
        self._engaged = on
        self.switched.append(on)
        self.lined_up = False

    @property
    def ready(self):
        return self._engaged and self.lined_up


@pytest.fixture
def echoes(make, monkeypatch):
    """Has satellites make a FakeEcho, and gives the list of those made."""
    made = []

    def make_one(config, rate, engaged=True):
        made.append(FakeEcho(engaged))
        return made[-1]

    monkeypatch.setattr(satellite, "make_echo_canceller", make_one)
    return made


def audio_sent(brain, since=0):
    """How many samples of audio the brain has had, from the piece after `since` pieces."""
    return sum(len(piece) for piece in brain.recordings()[since:])


def conversation(brain, on=True):
    brain.say("conversation", on=on)


def start_conversation(brain, made):
    """The brain says a conversation has started, and the satellite has its devices open for it."""
    conversation(brain)
    assert wait_for(lambda: made.microphone.holds == 1 and made.speaker.holds == 1)


def open_listen(brain, made, recording="listen-1"):
    """The brain asks for an open recording, and the satellite is sending it."""
    brain.say("listen", recording=recording, open=True)
    assert wait_for(lambda: len(brain.recordings()) >= 2)


def test_an_open_recording_runs_past_the_cap(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    # An ordinary answer ends at the cap.
    brain.say("listen", recording="listen-1")
    assert wait_for(lambda: brain.messages("audio_end"))
    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "limit"}]
    assert audio_sent(brain) == satellite.LIMIT_SECONDS * RATE
    sent = len(brain.recordings())
    assert wait_for(lambda: made._recording is None)

    # An open one goes on past that, and past the most one may ever be told to last.
    brain.say("listen", recording="listen-2", open=True)
    assert wait_for(lambda: audio_sent(brain, sent) >= 45 * RATE, seconds=15)
    assert len(brain.messages("audio_end")) == 1
    assert made._recording is not None and not made._recording.is_set()
    brain.say("stop_listening", recording="listen-2")
    assert wait_for(lambda: made._recording is None)
    # Ended by the brain, which is told nothing of it: it said so.
    assert len(brain.messages("audio_end")) == 1


@pytest.mark.parametrize("pressed", [False, True])
def test_in_a_conversation_a_recording_begun_by_the_wake_word_or_a_press_runs_past_the_cap(brain, make, pressed):
    made = pressable(brain, make) if pressed else make()

    def begin():
        if pressed:
            made._pressed()
        else:
            assert wait_for(lambda: connected(made) and made.wake.listening)
            made.wake.say()

    # Outside a conversation it ends at the cap, as ever.
    begin()
    assert wait_for(lambda: brain.messages("audio_end"))
    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "limit"}]
    assert audio_sent(brain) == satellite.LIMIT_SECONDS * RATE
    sent = len(brain.recordings())
    assert wait_for(lambda: made._recording is None)

    # In one here it is a turn of the conversation, which the brain gives the conversation's longest
    # turn, so it goes on past the cap as an open listen does, until the brain stops it.
    conversation(brain)
    assert wait_for(lambda: made._conversing)
    begin()
    assert wait_for(lambda: audio_sent(brain, sent) >= 2 * satellite.LIMIT_SECONDS * RATE, seconds=15)
    assert len(brain.messages("audio_end")) == 1
    assert made._recording is not None and made._recording.uncapped and not made._recording.is_set()
    # Uncapped, not open: it ends as any wake's or press's does.
    assert not made._recording.open
    brain.say("stop_listening", recording=brain.messages("wake")[-1]["recording"])
    assert wait_for(lambda: made._recording is None)
    assert len(brain.messages("audio_end")) == 1

    # And once it has ended, at the cap again.
    conversation(brain, on=False)
    assert wait_for(lambda: not made._conversing)
    sent = len(brain.recordings())
    begin()
    assert wait_for(lambda: len(brain.messages("audio_end")) == 2)
    assert brain.messages("audio_end")[-1] == {"type": "audio_end", "reason": "limit"}
    assert audio_sent(brain, sent) == satellite.LIMIT_SECONDS * RATE


def test_in_a_conversation_a_press_while_nova_thinks_is_an_ordinary_press(brain, make):
    # With the voice cut-in off - before the echo canceller is ready, say - a press made while Nova is thinking
    # is the user's turn as any press is, only uncapped: a line Nova says without listening ("Let me think about
    # that.") is stopped by it rather than said over them, and a second press ends the turn rather than cutting in.
    made = pressable(brain, make)
    start_conversation(brain, made)
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 1)
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=0.8, kind="reply", paused=False)
    brain.say("stream_audio", np.array([1, 2, 3], np.int16).tobytes(), id="reply-1")
    assert wait_for(lambda: brain.messages("played"))
    assert brain.messages("played")[0]["id"] == "reply-1" and brain.messages("played")[0]["stopped"]
    made._pressed()
    assert wait_for(lambda: brain.messages("audio_end"))
    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "pressed"}]
    assert brain.messages("cut_in") == []


def test_a_conversations_end_crossing_a_wake_leaves_its_recording_for_the_brain_to_end(brain, make):
    # The wake word said just as the conversation ended elsewhere is still a request, which the brain answers - not
    # a recording of the conversation's own, ended with nothing said.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    conversation(brain)
    assert wait_for(lambda: made._conversing)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 1)
    conversation(brain, on=False)
    assert wait_for(lambda: not made._conversing)
    time.sleep(0.2)
    assert made._recording is not None and not made._recording.is_set()
    brain.say("stop_listening", recording=brain.messages("wake")[-1]["recording"])
    assert wait_for(lambda: made._recording is None)


def test_an_open_listen_stops_nothing_and_plays_nothing(brain, make):
    made = pressable(brain, make)
    brain.say("play_stream", id="music-1", rate=44100, channels=2, volume=0.5, kind="music", paused=False)
    say_a_reply(brain)
    assert wait_for(lambda: len(made.speaker.streams()) == 2 and made.speaker.streams()[1].pieces)
    music, reply = made.speaker.streams()

    brain.say("listen", recording="listen-1", open=True)
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    # Not what a wake word or a press does: the reply carries on, the music isn't paused,
    # nothing is played for it, and the brain isn't told of any of it.
    assert not reply.done and not music.paused and not music.done
    assert made.speaker.files() == []
    assert brain.messages("played") == [] and brain.messages("wake") == []
    brain.say("stop_listening", recording="listen-1")


def test_a_computer_holds_its_devices_for_a_conversation(brain, make, echoes):
    made = pressable(brain, make)
    (echo,) = echoes
    assert echo.engaged is False and echo.switched == []
    assert made.microphone.holds == 0 and made.speaker.holds == 0

    conversation(brain)
    assert wait_for(lambda: made.microphone.holds == 1 and made.speaker.holds == 1)
    # Engaged once they were open, as it's their being opened afresh which has it start again.
    assert echo.engaged is True and echo.switched == [True]

    # Between recordings they stay open: a press's own recording holds the microphone as it
    # always did, and when it ends, the conversation's hold remains.
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    assert made.microphone.holds == 2
    brain.say("stop_listening")
    assert wait_for(lambda: made._recording is None)
    assert made.microphone.holds == 1 and made.speaker.holds == 1 and echo.engaged is True

    # Said again, it isn't held twice - nor reset, which would drop what the canceller had found.
    conversation(brain)
    time.sleep(0.05)
    assert made.microphone.holds == 1 and echo.switched == [True]

    conversation(brain, on=False)
    assert wait_for(lambda: made.microphone.holds == 0 and made.speaker.holds == 0)
    assert echo.engaged is False and echo.switched == [True, False]
    assert made.microphone.unheld_reads == 0


def test_a_wake_word_satellite_only_notes_a_conversation(brain, make, echoes):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    (echo,) = echoes
    # Always engaged, as it has always been: the speaker and microphone are open throughout.
    assert echo.engaged is True
    conversation(brain)
    assert wait_for(lambda: made._conversing)
    assert made.microphone.holds == 0 and made.speaker.holds == 0 and echo.switched == []
    conversation(brain, on=False)
    assert wait_for(lambda: not made._conversing)
    assert echo.engaged is True and echo.switched == []
    # And it hears the wake word as ever.
    made.wake.say()
    assert wait_for(lambda: brain.messages("wake"))
    brain.say("stop_listening")


def test_losing_the_brain_ends_the_open_devices(brain, make, echoes):
    made = pressable(brain, make)
    (echo,) = echoes
    start_conversation(brain, made)
    assert echo.engaged is True

    brain.drop()
    assert wait_for(lambda: made.microphone.holds == 0 and made.speaker.holds == 0)
    assert echo.engaged is False and made._conversing is False
    # And a conversation which the brain starts on its next connection starts from nothing.
    brain.up = True
    assert wait_for(lambda: len(brain.hellos) == 2 and connected(made))
    conversation(brain)
    assert wait_for(lambda: made.microphone.holds == 1 and made.speaker.holds == 1)
    assert echo.switched == [True, False, True]


def test_starting_a_conversation_unmutes_windows(brain, make, pc, caplog):
    caplog.set_level("INFO")
    made = pressable(brain, make)
    volume = made.system_volume
    volume.muted = volume.mic_muted = True
    conversation(brain)
    assert wait_for(lambda: volume.mic_muted is False and volume.muted is False)
    assert volume.mic_unmutes == 1 and volume.unmutes == 1
    assert "Windows' microphone was muted, so it's unmuted for this turn" in caplog.text

    # A turn's end doesn't mute it again, as it would after a press: it stays on for the
    # whole conversation...
    end_turn(brain)
    time.sleep(0.2)
    assert volume.mic_muted is False and volume.mic_changes == []
    # ...and is as a press leaves it once the conversation is over.
    conversation(brain, on=False)
    assert wait_for(lambda: volume.mic_muted is True)
    assert volume.mic_changes == [True]


def test_a_conversation_which_ends_with_the_brain_lost_mutes_the_microphone_again(brain, make, pc):
    made = pressable(brain, make)
    made.system_volume.mic_muted = True
    conversation(brain)
    assert wait_for(lambda: made.system_volume.mic_muted is False)
    brain.drop()
    assert wait_for(lambda: made.system_volume.mic_muted is True)
    assert made.system_volume.mic_changes == [True]


def test_a_conversation_which_isnt_on_or_off_is_ignored(brain, make, caplog):
    made = pressable(brain, make)
    with caplog.at_level("WARNING"):
        brain.say("conversation", on="yes")
        brain.say("conversation")
        brain.say("conversation", on=1)
        # Ahead of the next message, which the link handles after these.
        brain.say("state", volumes={"general": 0.25}, muted=False)
        assert wait_for(lambda: made.state.volume("general") == 0.25)
    assert made.microphone.holds == 0 and made.speaker.holds == 0 and made._conversing is False
    assert caplog.text.count("isn't on or off") == 3


def test_a_brain_which_knows_nothing_of_conversations_leaves_the_satellite_as_it_was(brain, make, echoes):
    made = pressable(brain, make)
    (echo,) = echoes
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    # Only the recording's own hold, and nothing engaged or asked of the canceller.
    assert made.microphone.holds == 1 and made.speaker.holds == 0
    assert echo.engaged is False and echo.switched == []
    brain.say("stop_listening")
    assert wait_for(lambda: made.microphone.holds == 0)
    # An ordinary listen is capped at its limit, as ever.
    brain.say("listen", recording="listen-1")
    assert wait_for(lambda: brain.messages("audio_end"))
    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "limit"}]
    assert brain.messages("cut_in") == []


def test_the_satellite_says_it_knows_conversations(brain, make):
    pressable(brain, make)
    assert brain.hellos[0]["conversations"] is True
    make("kitchen")
    assert wait_for(lambda: len(brain.hellos) == 2)
    assert brain.hellos[1]["conversations"] is True


def test_a_press_over_a_reply_in_an_open_recording_cuts_in(brain, make):
    made = pressable(brain, make)
    start_conversation(brain, made)
    open_listen(brain, made)
    brain.say("play_stream", id="music-1", rate=44100, channels=2, volume=0.5, kind="music", paused=False)
    say_a_reply(brain)
    assert wait_for(lambda: len(made.speaker.streams()) == 2 and made.speaker.streams()[1].pieces)
    music, reply = made.speaker.streams()
    made._pressed()

    assert wait_for(lambda: brain.messages("cut_in"))
    assert brain.messages("cut_in") == [{"type": "cut_in", "recording": "listen-1"}]
    # The reply stopped here at once, and nothing else with it.
    assert reply.stopped and not music.paused and not music.done
    # Told to the brain before the end of the reply it stopped, as for the wake word.
    assert wait_for(lambda: brain.messages("played"))
    assert brain.kinds().index("cut_in") < brain.kinds().index("played")
    # The press's own sound, which is heard, though the reply was stopped.
    assert made.speaker.files() == [os.path.join(made.sounds_folder, "listening.wav")]
    assert not made.speaker.playing[-1].stopped

    # And the recording goes on: it isn't ended, nor is another made of the press.
    sent = len(brain.recordings())
    assert wait_for(lambda: len(brain.recordings()) > sent + 1)
    assert brain.messages("audio_end") == [] and brain.messages("wake") == []
    assert made._recording is not None and not made._recording.is_set()
    # A second press with nothing playing now ends it.
    made._pressed()
    assert wait_for(lambda: brain.messages("audio_end"))
    brain.say("stop_listening", recording="listen-1")


def test_a_press_cutting_in_unmutes_windows_before_its_sound_as_any_press_does(brain, make, pc):
    made = pressable(brain, make)
    start_conversation(brain, made)
    open_listen(brain, made)
    # Muted by hand since the conversation began.
    volume = made.system_volume
    volume.muted = True
    volume.log = order = []
    made.speaker.play_file = lambda *args, **kwargs: order.append("chime")
    say_a_reply(brain)
    assert wait_for(lambda: made.speaker.streams() and made.speaker.streams()[0].pieces)
    made._pressed()
    assert wait_for(lambda: brain.messages("cut_in"))
    assert wait_for(lambda: order == ["unmute", "chime"])
    assert volume.muted is False


def test_a_press_with_nothing_playing_ends_the_turn(brain, make):
    made = pressable(brain, make)
    start_conversation(brain, made)
    open_listen(brain, made)
    made._pressed()

    assert wait_for(lambda: brain.messages("audio_end"))
    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "pressed"}]
    assert brain.messages("cut_in") == [] and brain.messages("wake") == []
    # A reply which has finished playing isn't one to cut in on either.
    assert wait_for(lambda: made._recording is None)


def test_a_press_over_a_reply_which_has_finished_ends_the_open_recording(brain, make):
    made = pressable(brain, make)
    start_conversation(brain, made)
    open_listen(brain, made)
    say_a_reply(brain)
    assert wait_for(lambda: made.speaker.streams() and made.speaker.streams()[0].pieces)
    made.speaker.streams()[0].finish()
    made._pressed()
    assert wait_for(lambda: brain.messages("audio_end"))
    assert brain.messages("cut_in") == []


def test_a_press_over_a_reply_in_an_ordinary_recording_ends_it_as_before(brain, make):
    made = pressable(brain, make)
    brain.say("listen", recording="listen-1")
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    say_a_reply(brain)
    assert wait_for(lambda: made.speaker.streams() and made.speaker.streams()[0].pieces)
    (reply,) = made.speaker.streams()
    made._pressed()
    assert wait_for(lambda: brain.messages("audio_end"))
    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "pressed"}]
    assert brain.messages("cut_in") == [] and not reply.stopped


def test_a_press_recording_which_the_brain_makes_open_isnt_cut_short_by_replies(brain, make):
    # Pressed to start a turn in a conversation, which the brain then asks to be open: its reply
    # plays over it, as one does over any open recording, rather than being stopped as it starts.
    made = pressable(brain, make)
    made._pressed()
    assert wait_for(lambda: len(brain.recordings()) >= 1)
    brain.say("listen", recording="listen-1", open=True)
    assert wait_for(lambda: made._recording is not None and made._recording.open)
    say_a_reply(brain)
    assert wait_for(lambda: made.speaker.streams() and made.speaker.streams()[0].pieces)
    (reply,) = made.speaker.streams()
    assert not reply.stopped

    # A press cuts in on it, and the brain knows the recording by the number it gave.
    made._pressed()
    assert wait_for(lambda: brain.messages("cut_in"))
    assert brain.messages("cut_in") == [{"type": "cut_in", "recording": "listen-1"}]
    assert reply.stopped


def test_a_listen_which_is_open_lifts_the_cap_of_the_recording_it_is_the_answer_to(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("listen", recording="listen-1", open=True)
    assert wait_for(lambda: audio_sent(brain) >= 15 * RATE, seconds=10)
    assert brain.messages("audio_end") == []
    brain.say("stop_listening", recording="listen-1")
    assert wait_for(lambda: made._recording is None)


def test_the_satellite_says_when_its_echo_canceller_is_ready(brain, make, echoes):
    made = pressable(brain, make)
    (echo,) = echoes
    # Once, as it connects, and not ready: nothing is engaged between conversations.
    assert wait_for(lambda: brain.messages("echo_ready"))
    assert brain.messages("echo_ready") == [{"type": "echo_ready", "ready": False}]

    start_conversation(brain, made)
    # Not ready yet: it has a gap to find, which takes some seconds of sound.
    time.sleep(0.1)
    assert brain.messages("echo_ready") == [{"type": "echo_ready", "ready": False}]
    echo.lined_up = True
    assert wait_for(lambda: len(brain.messages("echo_ready")) == 2)
    assert brain.messages("echo_ready")[1] == {"type": "echo_ready", "ready": True}
    # Said when it changes, not on every look.
    time.sleep(0.15)
    assert len(brain.messages("echo_ready")) == 2

    # The conversation ends: not ready, said at once.
    conversation(brain, on=False)
    assert wait_for(lambda: len(brain.messages("echo_ready")) == 3)
    assert brain.messages("echo_ready")[2] == {"type": "echo_ready", "ready": False}


def test_a_wake_word_satellites_canceller_is_ready_once_it_has_found_the_gap(brain, make, echoes):
    make()
    (echo,) = echoes
    assert wait_for(lambda: brain.messages("echo_ready"))
    assert brain.messages("echo_ready") == [{"type": "echo_ready", "ready": False}]
    echo.lined_up = True
    assert wait_for(lambda: len(brain.messages("echo_ready")) == 2)
    assert brain.messages("echo_ready")[1]["ready"] is True


def test_a_satellite_with_no_canceller_says_so_each_time_it_connects(brain, make):
    made = pressable(brain, make)
    assert wait_for(lambda: brain.messages("echo_ready"))
    conversation(brain)
    assert wait_for(lambda: made.microphone.holds == 1)
    time.sleep(0.1)
    assert brain.messages("echo_ready") == [{"type": "echo_ready", "ready": False}]

    # A brain which has just started hears it again.
    brain.sockets[-1].shutdown(socket.SHUT_RDWR)
    assert wait_for(lambda: len(brain.messages("echo_ready")) == 2)
    assert brain.messages("echo_ready")[1] == {"type": "echo_ready", "ready": False}


def test_the_end_of_a_conversation_ends_an_open_recording_and_leaves_the_orb_hidden(brain, make, pc, orbs):
    made = pressable(brain, make)
    orb = made.orb
    start_conversation(brain, made)
    open_listen(brain, made)
    assert wait_for(lambda: orb.shown == ["listening"])

    conversation(brain, on=False)
    assert wait_for(lambda: made._recording is None)
    # Hidden, not thinking: there is nothing for Nova to answer.
    assert wait_for(lambda: orb.shown == ["listening", "hidden"])
    time.sleep(0.1)
    assert orb.shown == ["listening", "hidden"]
    assert made.microphone.holds == 0


def test_an_open_recording_shows_listening_through_a_reply_and_a_press_ends_it(brain, make, pc, orbs):
    made = pressable(brain, make)
    orb = made.orb
    start_conversation(brain, made)
    open_listen(brain, made)
    say_a_reply(brain)
    assert wait_for(lambda: made.speaker.streams() and made.speaker.streams()[0].pieces)
    # Listening outranks speaking, as it does for any recording.
    assert orb.shown == ["listening"]
    made._pressed()
    assert wait_for(lambda: brain.messages("cut_in"))
    assert orb.shown == ["listening"]
    made._pressed()
    assert wait_for(lambda: orb.shown[-1] == "thinking")
    brain.say("stop_listening", recording="listen-1")


def test_losing_the_brain_during_an_open_recording_leaves_the_orb_hidden(brain, make, pc, orbs):
    made = pressable(brain, make)
    orb = made.orb
    start_conversation(brain, made)
    open_listen(brain, made)
    assert wait_for(lambda: orb.shown == ["listening"])
    brain.drop()
    assert wait_for(lambda: orb.shown[-1] == "hidden")
    assert wait_for(lambda: made._recording is None and made.microphone.holds == 0)
    # The microphone closing is never silent: the brain can't be reached is said, once - not
    # by which of the recording's own check and the link being lost came first.
    assert wait_for(lambda: PHRASE in made.speaker.files())
    time.sleep(0.3)
    assert made.speaker.files().count(PHRASE) == 1


def test_an_open_listen_after_the_stop_of_the_last_starts_from_where_it_was_asked(brain, make):
    # The brain stops one open recording and asks for the next at once, before the main
    # loop has seen the stop and finished sending the first.
    made = pressable(brain, make)
    start_conversation(brain, made)
    open_listen(brain, made, "listen-1")
    first = made._recording
    sent = len(brain.recordings())
    # Both handled in a moment, on this thread, as the link's would be one after the other.
    made._stop_listening({"recording": "listen-1"}, b"")
    asked_at = made.microphone.position
    made._listen({"recording": "listen-2", "open": True}, b"")
    assert first.is_set()
    # A recording of its own, under its own number.
    assert wait_for(lambda: made._recording is not None and made._recording is not first)
    second = made._recording
    assert second.ids == {"listen-2"} and second.open and second.latest == "listen-2"
    assert wait_for(lambda: len(brain.recordings()) >= sent + 3)
    # From where it was asked for, and so nothing of the first's is sent again.
    pieces = brain.recordings()[sent:]
    # (Each sample is its own position, modulo 32768.) Not before it was asked for, and soon after.
    assert 0 <= (int(pieces[0][0]) - asked_at) % 32768 < RATE
    # And carrying on from there, with no piece of the first's repeated.
    assert all(int(after[0]) == (int(before[0]) + len(before)) % 32768 for before, after in zip(pieces, pieces[1:]))

    # Stopping the first again, late, doesn't end the second.
    brain.say("stop_listening", recording="listen-1")
    time.sleep(0.1)
    assert not second.is_set() and brain.messages("audio_end") == []
    brain.say("stop_listening", recording="listen-2")
    assert wait_for(lambda: second.is_set())
    assert wait_for(lambda: made._recording is None)


def test_an_open_listen_while_one_is_going_carries_it_on_and_sends_nothing_twice(brain, make):
    made = pressable(brain, make)
    start_conversation(brain, made)
    open_listen(brain, made, "listen-1")
    first = made._recording
    brain.say("listen", recording="listen-2", open=True)
    assert wait_for(lambda: first.ids == {"listen-1", "listen-2"})
    # The same recording, which the brain stops by either number, and no other is waiting for its turn.
    assert made._listen_from is None
    brain.say("stop_listening", recording="listen-2")
    assert wait_for(lambda: made._recording is None)
    sent = len(brain.recordings())
    time.sleep(0.3)
    assert len(brain.recordings()) == sent and made._recording is None


# Updates - see docs/superpowers/plans for "update once, everything updates". The running
# commit, the one the brain offers and one after that.
OLD = "1" * 40
NEW = "2" * 40
NEWER = "3" * 40


class FakeInstalls(list):
    """Stands in for install_bundle and install_zip: keeps what each was given, as it ran."""

    def __init__(self, brain):
        super().__init__()
        self.brain = brain
        # Raised by the next install, if set.
        self.refuse = None

    def _ran(self, entry, folder, version):
        # What the brain had been told, and what was noted as begun, by the time it ran.
        entry["told"] = wait_for(lambda: self.brain.messages("update_installing"), 1)
        entry["pending"] = UpdateState(folder)._read("pending.json")
        self.append(entry)
        if self.refuse is not None:
            raise self.refuse

    def bundle(self, project, folder, version, archive, run=None):
        self._ran({"format": "bundle", "folder": folder, "version": version, "archive": archive}, folder, version)

    def zip(self, project, folder, version, previous, archive, run=None):
        self._ran({"format": "zip", "folder": folder, "version": version, "previous": previous,
                   "archive": archive}, folder, version)


@pytest.fixture
def updates(brain, monkeypatch):
    """Has satellites run commit OLD and update by bundle, and gives what they installed."""
    installs = FakeInstalls(brain)
    monkeypatch.setattr(satellite.code_version, "running_version", lambda: OLD)
    monkeypatch.setattr(satellite.code_version, "update_format", lambda: "bundle")
    monkeypatch.setattr(satellite, "install_bundle", installs.bundle)
    monkeypatch.setattr(satellite, "install_zip", installs.zip)
    monkeypatch.setattr(satellite, "UPDATE_LOOK", 0.02)
    return installs


def archive_of(size, seed=1):
    return bytes((i * 7 + seed) % 256 for i in range(size))


def send_update(brain, version, archive, update_id="u1", retry=False, fmt="bundle", upto=None, tamper=False,
                end=True):
    """Sends an update as the brain does, proved for the satellite's latest connection."""
    satellite_nonce, brain_nonce = brain.hellos[-1]["nonces"]
    proof = protocol.update_proof(PASSWORD, satellite_nonce, brain_nonce, version, fmt, archive)
    if tamper:
        archive = bytes([archive[0] ^ 1]) + archive[1:]
    pieces = [archive[i:i + protocol.UPDATE_CHUNK] for i in range(0, len(archive), protocol.UPDATE_CHUNK)]
    brain.say("update_offer", id=update_id, version=version, format=fmt, size=len(archive), chunks=len(pieces),
              proof=proof, retry=retry)
    for index, piece in enumerate(pieces[:upto]):
        brain.say("update_chunk", piece, id=update_id, index=index)
    if end:
        brain.say("update_end", id=update_id)


def updatable(brain, make, **sections):
    made = make(**sections)
    assert wait_for(lambda: connected(made))
    return made


def results(brain):
    return [{key: value for key, value in message.items() if key != "type"} for message in brain.messages("update_result")]


def test_the_update_folder_is_in_the_data_folder():
    # The folder UpdateState, the installers and their helpers all share - install.ps1's $data\update on the PC.
    assert satellite.UPDATE_FOLDER == os.path.join(paths.DATA_DIR, "update")


def test_the_hello_says_the_satellites_commit(brain, make, updates):
    updatable(brain, make)
    hello = brain.hellos[0]
    assert (hello["version"], hello["updates"], hello["update_format"]) == (OLD, True, "bundle")


def test_a_verified_update_installs_once_free(brain, make, updates, tmp_path):
    made = updatable(brain, make)
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=1.0, kind="reply", paused=False)
    assert wait_for(lambda: made._busy() == "speaking")
    archive = archive_of(protocol.UPDATE_CHUNK + 10)
    send_update(brain, NEW, archive)
    time.sleep(0.3)
    # Not while Nova speaks.
    assert updates == [] and brain.messages("update_installing") == []
    brain.say("stream_end", id="reply-1")
    assert wait_for(lambda: updates)
    assert brain.messages("update_installing") == [{"type": "update_installing", "version": NEW}]
    [installed] = updates
    # The brain told it was installing, and the update noted as begun, before the installer ran.
    assert installed["told"] is True
    assert installed["pending"]["version"] == NEW and installed["pending"]["previous"] == OLD
    assert (installed["format"], installed["version"], installed["archive"]) == ("bundle", NEW, archive)
    assert installed["folder"] == str(tmp_path / "update")
    assert results(brain) == []


def test_a_zip_update_is_given_the_version_it_replaces(brain, make, updates, monkeypatch):
    monkeypatch.setattr(satellite.code_version, "update_format", lambda: "zip")
    updatable(brain, make)
    send_update(brain, NEW, archive_of(100), fmt="zip")
    assert wait_for(lambda: updates)
    assert (updates[0]["format"], updates[0]["version"], updates[0]["previous"]) == ("zip", NEW, OLD)


def test_music_doesnt_hold_an_update_back(brain, make, updates):
    made = updatable(brain, make)
    brain.say("play_stream", id="song-1", rate=44100, channels=2, volume=1.0, kind="music", paused=False)
    assert wait_for(lambda: "song-1" in made._playing)
    send_update(brain, NEW, archive_of(100))
    assert wait_for(lambda: updates)
    assert made._busy() is None


def test_what_holds_an_update_back(make, updates):
    made = make(start=False)
    assert made._busy() is None
    made._recording = satellite._Recording("wake-1")
    assert made._busy() == "recording"
    made._recording = None
    # The brain working on what was said, before the reply comes.
    made._turn_open = True
    assert made._busy() == "thinking"
    made._turn_open = False
    made._conversing = True
    assert made._busy() == "conversation"
    made._conversing = False
    made._approving = 1
    assert made._busy() == "approval"
    made._approving = 0
    made.ringer = types.SimpleNamespace(ringing=True)
    assert made._busy() == "ringing"


def test_an_approval_holds_an_update_back_while_it_is_asked(brain, make, hello, updates):
    made = pressable(brain, make)
    seen = []
    made.hello.ask = lambda id, description: seen.append(made._busy()) or True
    brain.say("approval", id="approval-1", description="Remove 'Gym'")
    assert wait_for(lambda: brain.messages("approval_answer"))
    assert seen == ["approval"] and made._busy() is None


def test_a_refused_update_is_reported_and_not_installed(brain, make, updates):
    updatable(brain, make)
    send_update(brain, NEW, archive_of(100), tamper=True)
    assert wait_for(lambda: results(brain))
    assert results(brain) == [{"version": NEW, "ok": False, "error": NOT_FROM_THE_BRAIN}]
    time.sleep(0.1)
    assert updates == [] and brain.messages("update_installing") == []


def test_an_offer_to_a_satellite_which_cant_update_is_answered(brain, make, updates, monkeypatch):
    monkeypatch.setattr(satellite.code_version, "update_format", lambda: None)
    updatable(brain, make)
    send_update(brain, NEW, archive_of(100))
    assert wait_for(lambda: results(brain))
    assert results(brain) == [{"version": NEW, "ok": False, "error": WRONG_FORMAT}]
    assert updates == []


def failed_before(tmp_path, needs_you=True):
    """Writes failed.json for NEW as the installers' scripts do: the whole result."""
    result = {"version": NEW, "ok": False, "error": "There are changes on the Pi that aren't in git."}
    if needs_you:
        result["needs_you"] = [{"why": "Local changes.", "command": "git -C ~/Home-Assistant status"}]
    (tmp_path / "update").mkdir(exist_ok=True)
    (tmp_path / "update" / "failed.json").write_text(json.dumps(result))
    return result


def test_a_version_that_failed_before_is_answered_not_installed(brain, make, updates, tmp_path):
    failed = failed_before(tmp_path)
    updatable(brain, make)
    send_update(brain, NEW, archive_of(100))
    assert wait_for(lambda: results(brain))
    # The whole result, so a needs-you keeps its command.
    assert results(brain) == [failed]
    time.sleep(0.1)
    assert updates == []


def test_a_retry_offer_installs_a_version_that_failed_before(brain, make, updates, tmp_path):
    failed_before(tmp_path)
    updatable(brain, make)
    send_update(brain, NEW, archive_of(100), retry=True)
    assert wait_for(lambda: updates)
    assert updates[0]["version"] == NEW and results(brain) == []


def test_a_transfer_cut_off_installs_nothing(brain, make, updates):
    made = updatable(brain, make)
    first = archive_of(protocol.UPDATE_CHUNK + 10, seed=1)
    send_update(brain, NEW, first, update_id="u1", upto=1, end=False)
    assert wait_for(lambda: made._receiver is not None)
    brain.drop()
    assert wait_for(lambda: not connected(made))
    brain.up = True
    assert wait_for(lambda: connected(made) and len(brain.hellos) == 2)
    # The end of the first, late, on the new connection is no transfer of its own.
    brain.say("update_chunk", first[protocol.UPDATE_CHUNK:], id="u1", index=1)
    brain.say("update_end", id="u1")
    second = archive_of(protocol.UPDATE_CHUNK + 10, seed=2)
    send_update(brain, NEW, second, update_id="u2")
    assert wait_for(lambda: updates)
    time.sleep(0.1)
    assert [installed["archive"] for installed in updates] == [second]
    assert results(brain) == []


def test_an_offer_while_one_installs_is_ignored(brain, make, updates):
    made = updatable(brain, make)
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=1.0, kind="reply", paused=False)
    assert wait_for(lambda: made._busy() == "speaking")
    send_update(brain, NEW, archive_of(100), update_id="u1")
    assert wait_for(lambda: made._installing)
    send_update(brain, NEWER, archive_of(100, seed=3), update_id="u2")
    time.sleep(0.1)
    brain.say("stream_end", id="reply-1")
    assert wait_for(lambda: updates)
    time.sleep(0.2)
    assert [installed["version"] for installed in updates] == [NEW]


def test_an_install_refused_is_noted_and_reported(brain, make, updates, tmp_path):
    updatable(brain, make)
    needs = {"why": "The PC has no update task yet, so it needs one deploy by hand.",
             "command": "bash scripts/deploy_pc.sh"}
    updates.refuse = InstallRefused("The update task couldn't be started.", needs)
    send_update(brain, NEW, archive_of(100))
    assert wait_for(lambda: results(brain))
    expected = {"version": NEW, "ok": False, "error": "The update task couldn't be started.", "needs_you": [needs]}
    assert results(brain) == [expected]
    state = UpdateState(str(tmp_path / "update"))
    assert wait_for(lambda: state.result_to_send() is None)
    assert state._read("pending.json") is None
    assert state.failed_result(NEW) == expected


def test_an_install_which_breaks_is_reported_and_the_next_can_go(brain, make, updates, caplog):
    updatable(brain, make)
    updates.refuse = RuntimeError("disk on fire")
    send_update(brain, NEW, archive_of(100), update_id="u1")
    assert wait_for(lambda: results(brain))
    [result] = results(brain)
    assert result["version"] == NEW and result["ok"] is False and "RuntimeError" in result["error"]
    assert "disk on fire" in caplog.text
    updates.refuse = None
    send_update(brain, NEWER, archive_of(100), update_id="u2", retry=True)
    assert wait_for(lambda: len(updates) == 2)


def test_the_new_code_confirms_and_reports_as_it_connects(brain, make, updates, tmp_path):
    state = UpdateState(str(tmp_path / "update"))
    state.begin(OLD, "0" * 40)
    updatable(brain, make)
    assert wait_for(lambda: results(brain))
    assert results(brain) == [{"version": OLD, "ok": True, "needs_you": []}]
    assert (tmp_path / "update" / "confirmed").read_text() == OLD
    assert wait_for(lambda: state.result_to_send() is None)


def test_a_result_unsent_is_sent_on_the_next_connection(brain, make, updates, tmp_path, monkeypatch):
    state = UpdateState(str(tmp_path / "update"))
    state.note_result({"version": OLD, "ok": True, "needs_you": []})
    refused = []

    class FirstResultLost(satellite.SatelliteLink):
        """A link on which the first update_result doesn't go."""

        def send(self, kind, payload=b"", /, **fields):
            if kind == "update_result" and not refused:
                refused.append(fields)
                return False
            return super().send(kind, payload, **fields)

    monkeypatch.setattr(satellite, "SatelliteLink", FirstResultLost)
    made = updatable(brain, make)
    assert wait_for(lambda: refused)
    time.sleep(0.1)
    # Kept, as the brain never had it.
    assert state.result_to_send() == {"version": OLD, "ok": True, "needs_you": []}
    brain.drop()
    assert wait_for(lambda: not connected(made))
    brain.up = True
    assert wait_for(lambda: results(brain))
    assert results(brain) == [{"version": OLD, "ok": True, "needs_you": []}]
    assert wait_for(lambda: state.result_to_send() is None)


def test_a_second_offer_after_the_hand_off_installs_nothing(brain, make, updates, tmp_path):
    made = updatable(brain, make)
    send_update(brain, NEW, archive_of(100), update_id="u1")
    assert wait_for(lambda: updates)
    # The brain offering it again - from a reconnect before the installer stopped the
    # satellite, say - mustn't start a second install, whose refusal would undo the first.
    send_update(brain, NEW, archive_of(100), update_id="u2")
    time.sleep(0.3)
    assert len(updates) == 1 and results(brain) == [] and made._installing
    assert UpdateState(str(tmp_path / "update"))._read("pending.json")["version"] == NEW


def test_the_satellite_holds_the_installer_off_while_busy_after_the_hand_off(brain, make, updates, tmp_path):
    hold = tmp_path / "update" / "hold"
    # One left by a satellite which stopped while busy is no reason to wait.
    hold.parent.mkdir()
    hold.write_text("speaking")
    made = updatable(brain, make)
    send_update(brain, NEW, archive_of(100))
    assert wait_for(lambda: updates)
    assert wait_for(lambda: not hold.exists())
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=1.0, kind="reply", paused=False)
    assert wait_for(lambda: hold.exists())
    assert hold.read_text() == "speaking"
    brain.say("stream_end", id="reply-1")
    assert wait_for(lambda: not hold.exists())
    assert made._busy() is None


def test_a_hand_off_whose_installer_never_restarts_the_satellite_is_given_up(brain, make, updates, tmp_path,
                                                                             monkeypatch, caplog):
    # The installer took the update over but never stopped the satellite - its helper broke
    # before it did, say - so after the longest an installer can take, the satellite stops
    # waiting for it, tells the brain, and can be updated again.
    monkeypatch.setattr(satellite, "HANDOFF_LONGEST", 0.3, raising=False)
    made = updatable(brain, make)
    send_update(brain, NEW, archive_of(100), update_id="u1")
    assert wait_for(lambda: updates)
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=1.0, kind="reply", paused=False)
    hold = tmp_path / "update" / "hold"
    assert wait_for(lambda: hold.exists())
    assert wait_for(lambda: results(brain), 2)
    expected = {"version": NEW, "ok": False,
                "error": "The installer didn't restart the satellite, so the update didn't happen."}
    assert results(brain) == [expected]
    assert wait_for(lambda: not made._installing)
    # Even while busy, as the installer's own wait for the hold is long over.
    assert not hold.exists()
    state = UpdateState(str(tmp_path / "update"))
    assert state._read("pending.json") is None and state.failed_result(NEW) == expected
    assert "didn't restart the satellite" in caplog.text
    brain.say("stream_end", id="reply-1")
    send_update(brain, NEWER, archive_of(100), update_id="u2")
    assert wait_for(lambda: len(updates) == 2)


def test_a_hand_off_given_up_sends_the_installers_own_result(brain, make, updates, tmp_path, monkeypatch):
    monkeypatch.setattr(satellite, "HANDOFF_LONGEST", 0.3, raising=False)
    made = updatable(brain, make)
    send_update(brain, NEW, archive_of(100))
    assert wait_for(lambda: updates)
    # What an installer which failed, but couldn't restart the satellite, left.
    own = {"version": NEW, "ok": False, "error": "The new code's packages wouldn't install."}
    UpdateState(str(tmp_path / "update")).note_result(own)
    assert wait_for(lambda: results(brain), 2)
    assert results(brain) == [own]
    assert wait_for(lambda: not made._installing)
    state = UpdateState(str(tmp_path / "update"))
    assert state._read("pending.json") is None and state.result_to_send() is None


def test_an_update_waiting_is_given_up_when_the_satellite_stops(brain, make, updates, caplog):
    caplog.set_level(logging.INFO, logger="satellite")
    made = updatable(brain, make)
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=1.0, kind="reply", paused=False)
    assert wait_for(lambda: made._busy() == "speaking")
    send_update(brain, NEW, archive_of(100))
    assert wait_for(lambda: made._installing)
    made.stop()
    assert wait_for(lambda: made.exited)
    assert wait_for(lambda: not made._installing)
    assert updates == [] and "stopping, so update 2222222 wasn't installed" in caplog.text


def test_only_a_results_own_fields_are_sent(brain, make, updates, tmp_path):
    (tmp_path / "update").mkdir()
    (tmp_path / "update" / "result.json").write_text(json.dumps(
        {"version": OLD, "ok": True, "type": "ping", "extra": 1,
         "needs_you": [{"why": "w", "command": "c"}, 5]}))
    updatable(brain, make)
    assert wait_for(lambda: results(brain))
    assert results(brain) == [{"version": OLD, "ok": True, "needs_you": [{"why": "w", "command": "c"}]}]


def test_a_result_which_makes_no_sense_is_dropped_not_sent(brain, make, updates, tmp_path):
    (tmp_path / "update").mkdir()
    (tmp_path / "update" / "result.json").write_text(json.dumps({"ok": "yes"}))
    updatable(brain, make)
    assert wait_for(lambda: not (tmp_path / "update" / "result.json").exists())
    time.sleep(0.1)
    assert results(brain) == []
