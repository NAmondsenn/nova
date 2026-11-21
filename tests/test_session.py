"""
Session Test
Checks one satellite's Nova on the brain: each wake word the satellite hears recorded,
heard and answered through it, its mute and volumes kept and sent to it, and its own
device in Home Assistant - against a fake Pi over a socketpair, which reads what the
brain sends with the real protocol, and stand-ins for Piper and Nemotron. Needs numpy
and soxr installed (as they are on the Pi), and is skipped anywhere they aren't.
"""

import importlib
import json
import logging
import os
import queue
import socket
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("soxr")

from voice_assistant import clock as clock_module
from voice_assistant import ha_device, music, network, notes, paths, protocol, satellite_link, spotify_controller
from voice_assistant import session as session_module
from voice_assistant.actions import Action
from voice_assistant.brain import Brain
from voice_assistant.choices import ChoiceStore
from voice_assistant.clock import Clock
from voice_assistant.config import Config
from voice_assistant.computers import ComputerControl, Computers
from voice_assistant.conversation import Reply
from voice_assistant.lists import Lists
from voice_assistant.media_watch import MediaWatcher
from voice_assistant.navidrome import NavidromePlayer
from voice_assistant.open_conversation import OpenConversation
from voice_assistant.recording import FRAME, RATE, AudioFeed
from voice_assistant.remote import RemoteSatellite
from voice_assistant.routines import Routines
from voice_assistant.satellite_rooms import SatelliteRooms
from voice_assistant.undo import LastChange
from voice_assistant.vad import LoudnessDetector


def wait_for(condition, seconds=5):
    for _ in range(int(seconds / 0.005)):
        if condition():
            return True
        time.sleep(0.005)
    return False


def said(seconds=0.5, before=0.3, after=1.0):
    """
    What a satellite sends for one request, as 16-bit samples: the moment before it was
    woken, the user talking, then quiet.
    """
    return np.concatenate([np.zeros(round(before * RATE)), np.full(round(seconds * RATE), 8000),
                           np.zeros(round(after * RATE))]).astype(np.int16)


class Pi:
    """
    The satellite's end of the connection: keeps everything the brain sends, and plays
    as a satellite would - each of its own sounds finished at once, and each stream
    once it has all been sent, unless the test holds it playing, saying how far it got.
    """

    def __init__(self, sock):
        self.socket = sock
        self.stream = sock.makefile("rwb")
        self.received = []
        self.lock = threading.Lock()
        # Which of the brain's streams, counted from 1, play on until they're stopped -
        # or, with finishes_nothing, every sound and stream, as a satellite which has
        # lost track of them would.
        self.held = set()
        # Which of the satellite's own sounds, by name, play on until they're stopped.
        self.held_sounds = set()
        self.finishes_nothing = False
        self._held_ids = set()
        self._streams = 0
        # Whether announcements are refused, as they are while the satellite has them
        # silenced: each ends at once, with none of it played.
        self.refusing = False
        self._refused = set()
        # How much of each stream has arrived, by its id.
        self._samples = {}
        # What the user says each time the brain asks to hear their answer.
        self.answers = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                message, payload = protocol.receive(self.stream)
                self.received.append((message, payload))
                if message["type"] == "stream_audio":
                    self._samples[message["id"]] = self._samples.get(message["id"], 0) + len(payload) // 2
                self._play(message)
        except (OSError, protocol.ProtocolError):
            pass

    def _play(self, message):
        kind = message["type"]
        if self.finishes_nothing and kind in ("play_sound", "stream_end"):
            return
        if kind in ("play_sound", "play_stream") and self.refusing and message["kind"] == "announcement":
            self._refused.add(message["id"])
            self.say("played", id=message["id"], stopped=False, position=0)
        elif kind == "play_sound" and message["name"] not in self.held_sounds:
            self.say("played", id=message["id"])
        elif kind == "play_stream":
            self._streams += 1
            if self._streams in self.held:
                self._held_ids.add(message["id"])
        elif kind == "stream_end" and message["id"] not in self._held_ids | self._refused:
            self.say("played", id=message["id"], stopped=False, position=self._samples.get(message["id"], 0))
        elif kind == "listen" and self.answers:
            self.send_audio(self.answers.pop(0))

    def say(self, kind, payload=b"", **fields):
        with self.lock:
            protocol.send(self.stream, kind, payload, **fields)

    def wake(self, audio, talked_over=False, pressed=False):
        """Hears the wake word, and sends what's said after it - the moment before it first."""
        self.say("wake", talked_over=talked_over, **({"pressed": True} if pressed else {}))
        self.send_audio(audio)

    def send_audio(self, audio):
        for begin in range(0, len(audio), 1600):
            self.say("audio", audio[begin:begin + 1600].astype("<i2").tobytes())

    def messages(self, kind):
        return [message for message, _ in self.received if message["type"] == kind]

    def go(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()


class FakeTranscription:
    """Stands in for speech-to-text listening to one recording, noting what it was given."""

    def __init__(self, text="", imagined=False):
        self.text = text
        self.imagined = imagined
        self.added = []
        self.finished_with = None
        self.finished_how = None
        self.cancelled = False
        self.settled = False

    def add(self, audio):
        self.added.append(audio)

    def finish(self, audio, **how):
        self.finished_with = audio
        self.finished_how = how
        return {"text": self.text, "imagined": self.imagined}

    def words(self):
        # Its text, once it has been given anything to hear - as Nemotron has words only once it has heard some.
        return self.text if self.added else ""

    def settle(self, timeout=None):
        self.settled = True
        return self.text if self.added else ""

    def cancel(self):
        self.cancelled = True


class FakeSpeechToText:
    """Stands in for the brain's Nemotron, hearing in each recording the next of the texts the test gives."""

    loaded = True
    hung = False

    def __init__(self):
        # Each a text - or a FakeTranscription of its own, for words imagined in noise.
        self.texts = []
        self.made = []

    def listen(self):
        text = self.texts.pop(0) if self.texts else ""
        self.made.append(text if isinstance(text, FakeTranscription) else FakeTranscription(text))
        return self.made[-1]


class FakePiper:
    """Stands in for Piper, keeping what it was asked to say."""

    sample_rate = 22050

    def __init__(self):
        self.said = []
        # The speed each was said at, None for Nova's own.
        self.speeds = []

    def stream(self, message, on_start=None, speed=None):
        self.said.append(message)
        self.speeds.append(speed)

        def pieces():
            if on_start:
                on_start()
            yield np.zeros(2205, np.float32)
        return pieces()


class FakeTimeline:
    def __init__(self):
        self.notes = {}
        self.marks = {}

    def mark(self, stage, at=None):
        pass

    def note(self, **notes):
        self.notes.update(notes)


class ScriptedConversation:
    """Stands in for the conversation, answering each thing heard with the next of the replies the test gives."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.started = []
        self.pressed = []
        self.heard = []

    def start(self, ringing=None, talked_over=False, pressed=False):
        self.started.append(talked_over)
        self.pressed.append(pressed)

    def handle(self, text, follow_up=False, timeline=None):
        self.heard.append((text, follow_up))
        return self.replies.pop(0)


class Device:
    """Stands in for the satellite's device in Home Assistant, noting when it's started and stopped."""

    identifier = "nova_bedroom_pi"

    def __init__(self, happened):
        self.happened = happened

    def start(self):
        self.happened.append("started")

    def stop(self):
        self.happened.append("stopped")


@pytest.fixture
def brain(tmp_path, monkeypatch, make_config):
    """What every session shares, with stand-ins for Piper and Nemotron, keeping everything in a temporary folder."""
    monkeypatch.setattr(paths, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(paths, "LOGS_DIR", str(tmp_path))
    monkeypatch.setattr(clock_module, "TIMERS_FILE", str(tmp_path / "timers.json"))
    monkeypatch.setattr(clock_module, "ALARMS_FILE", str(tmp_path / "alarms.json"))
    # Speech told apart by how loud it is, which the made-up recordings here are made for.
    monkeypatch.setattr(session_module, "make_detector", lambda config: LoudnessDetector())
    # No music, unless a test gives some.
    monkeypatch.setattr(session_module, "make_player", lambda config, speaker, volume, spotify_speaker=None: None)
    config = make_config(assistant={"name": "Nova", "wake_word": "Hey Nova"})
    # A satellite rings where it was set, and is in a room of its own, as the Brain's are until
    # Home Assistant says otherwise.
    return SimpleNamespace(config=config, clock=Clock(config=config, start=False), memory=None,
                           rooms=SatelliteRooms(str(tmp_path / "satellite_rooms.json")), computers=Computers(),
                           ring_on=lambda name: name, room_of=lambda name: [name], room_sessions=lambda name: [],
                           spoken_name=lambda name: name, notes_saved_on=lambda: [],
                           names_satellite=lambda said: False,
                           session_for=lambda name: None,
                           routines=Routines(path=str(tmp_path / "routines.json")), ntfy=None, weather=None,
                           batteries=None, lists=None, pc=None, registry=None, calendar=None, media=None,
                           downloads=None, football=None, listening=None, wrapped_store=None, usage=None, new_music_playlist=None,
                           playlist_changes=None, journeys=None, last_change=LastChange(), open_conversation=None,
                           tts=FakePiper(), stt=FakeSpeechToText(), sounds_folder=Path(paths.SOUNDS_DIR))


@pytest.fixture
def connect():
    """Connects fake Pis, each a satellite the brain has just let in, and lets them all go after the test."""
    made = []

    def connect(name="bedroom", hostname="bedroom-pi.local", speaker=None, media_controls=False,
                conversations=False):
        ours, theirs = socket.socketpair()
        # Short, so the brain's reading wakes often to check on the satellite.
        ours.settimeout(0.05)
        # One which knows conversations is sent their messages - open listens, and conversation on and off.
        hello = {"name": name, "hostname": hostname, "sample_rate": 48000, "unclean": False, "speaker": speaker,
                 "media_controls": media_controls, "conversations": conversations}
        satellite = RemoteSatellite(satellite_link.Stream(ours), hello)
        pi = Pi(theirs)
        threading.Thread(target=satellite.serve, daemon=True).start()
        made.append((satellite, pi))
        return satellite, pi

    yield connect
    for satellite, pi in made:
        satellite.close()
        pi.go()


@pytest.fixture
def make_session(brain, connect):
    """Makes sessions for the brain - for a new satellite unless one is given - closing them after the test."""
    made = []

    def make(satellite=None):
        made.append(session_module.Session(brain, satellite or connect()[0]))
        return made[-1]

    yield make
    for session in made:
        session.close()


def running(session):
    """Runs a session on a thread of its own, as the brain does."""
    thread = threading.Thread(target=session.run, daemon=True)
    thread.start()
    return thread


# On the brain

def test_the_session_needs_none_of_the_satellites_audio_libraries(monkeypatch):
    # The brain has no microphone or speaker, so no PortAudio for sounddevice to load -
    # and the session used to need it, through the wake word's detector. Imported
    # afresh, with every module of Nova's put back as it was afterwards.
    before = set(sys.modules)
    for name in [name for name in sys.modules if name.partition(".")[0] == "voice_assistant"]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    try:
        importlib.import_module("voice_assistant.session")
    finally:
        for name in set(sys.modules) - before:
            if name.partition(".")[0] == "voice_assistant":
                del sys.modules[name]


# Answering the satellite

def test_a_wake_is_recorded_heard_and_answered_through_the_satellite(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    brain.stt.texts = ["What time is it?"]
    thread = running(session)

    pi.wake(said())
    assert wait_for(lambda: pi.messages("allow"))

    # Heard from what the satellite sent, given to speech-to-text as it was recorded.
    transcription = brain.stt.made[0]
    assert len(transcription.added) > 1
    assert np.array_equal(np.concatenate(transcription.added), transcription.finished_with)
    # Answered by the everyday commands, and said through the satellite.
    assert len(brain.tts.said) == 1 and brain.tts.said[0].startswith("It's ")
    kinds = [message["type"] for message, _ in pi.received if message["type"] != "stream_audio"]
    assert kinds == ["silence", "play_sound", "stop_listening", "play_stream", "stream_end", "allow"]
    assert pi.messages("silence")[0]["kind"] == pi.messages("allow")[0]["kind"] == "announcement"
    assert pi.messages("play_sound")[0]["name"] == "listening.wav"
    assert pi.messages("play_stream")[0]["kind"] == "reply" and pi.messages("stream_audio")

    pi.go()
    thread.join(2)
    assert not thread.is_alive()


def test_every_session_changes_playlists_with_the_brains_one(brain, connect, make_session):
    brain.playlist_changes = object()
    assert make_session().actions.playlist_changes is brain.playlist_changes


def test_every_session_undoes_from_the_brains_one_slot(brain, connect, make_session):
    # One "undo that" for the house: a change by voice at one satellite is the last thing done at every other.
    assert make_session().actions.last_change is brain.last_change
    assert make_session().actions.last_change is make_session().actions.last_change


def test_a_timer_set_through_a_satellite_rings_on_it(brain, connect, make_session):
    satellite, pi = connect()
    brain.stt.texts = ["Set a timer for ten minutes."]
    running(make_session(satellite))

    pi.wake(said())
    assert wait_for(lambda: pi.messages("allow"))
    assert len(brain.clock.overview(satellite="bedroom")["timers"]) == 1
    assert brain.clock.overview(satellite="kitchen")["timers"] == []


def test_talking_over_a_follow_up_starts_the_next_turn(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="Which room?", listen_again=True),
                                                Reply(say="The kitchen lights are on, and the hall's..."),
                                                Reply(say="Okay."))
    brain.stt.texts = ["Turn the lights on.", "The kitchen.", "Stop."]
    # The answer to the question, said once the brain asks to hear it - and the reply
    # to that plays on until it's talked over.
    pi.answers = [said()]
    pi.held = {2}
    thread = running(session)

    pi.wake(said())
    assert wait_for(lambda: len(pi.messages("play_stream")) == 2)
    pi.wake(said(), talked_over=True)
    assert wait_for(lambda: len(pi.messages("play_stream")) == 3 and pi.messages("allow"))

    assert session.conversation.heard == [("Turn the lights on.", False), ("The kitchen.", True), ("Stop.", False)]
    # The next request knows the last reply was talked over.
    assert session.conversation.started == [False, True]
    # Which was stopped there and then, by its id.
    talked_over = pi.messages("play_stream")[1]["id"]
    assert any(message.get("id") == talked_over for message in pi.messages("stop"))
    assert len(pi.messages("listen")) == 1 and len(pi.messages("allow")) == 1
    pi.go()
    thread.join(2)


def test_a_turn_whose_satellite_goes_ends_quietly(brain, connect, make_session, caplog):
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="Okay."))
    brain.stt.texts = ["Turn the"]
    happened = []
    session.device = Device(happened)
    thread = running(session)

    # Gone part way through the request.
    pi.wake(said(after=0))
    assert wait_for(lambda: brain.stt.made and brain.stt.made[0].added)
    pi.go()
    thread.join(3)

    assert not thread.is_alive()
    # Half a request isn't acted on, and nobody's told sorry - there's nobody there.
    assert brain.stt.made[0].cancelled
    assert session.conversation.heard == [] and brain.tts.said == []
    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []
    # And Home Assistant shows its device as unavailable.
    assert happened == ["started", "stopped"]


def test_giving_up_without_hearing_anything_is_audible(brain, connect, make_session):
    # Giving up used to be silent, which was easy to take for the assistant still
    # listening, or still thinking.
    satellite, pi = connect()
    session = make_session(satellite)
    # One the satellite is sending, as only that has a recording to stop.
    feed = satellite.listen()
    feed.feed(np.zeros(4 * RATE, np.int16))

    assert session._handle_interaction(feed, timeline=FakeTimeline()) is None
    assert [message["name"] for message in pi.messages("play_sound")] == ["listening.wav", "not_heard.wav"]
    # A false wake word isn't apologised for, only noted.
    assert brain.tts.said == []
    # Speech-to-text, which was ready to listen, is told nothing's coming - and the
    # satellite stops sending.
    assert brain.stt.made[0].cancelled
    assert wait_for(lambda: pi.messages("stop_listening"))


def test_speech_with_no_words_heard_in_it_is_said_so(brain, connect, make_session):
    # Nemotron hears many a lone "Pause." as nothing, and Nova used to say nothing back.
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation()
    brain.stt.texts = [""]
    timeline = FakeTimeline()
    feed = AudioFeed()
    feed.feed(said())
    feed.end()

    assert session._handle_interaction(feed, timeline=timeline) is None
    assert brain.tts.said == ["Sorry, I didn't catch that."]
    assert timeline.notes["outcome"] == "nothing_transcribed"
    assert [message["name"] for message in pi.messages("play_sound")] == ["listening.wav"]
    # Told how the recording ended, so it can hear the words again without the
    # quiet after them - and what to time that on.
    how = brain.stt.made[0].finished_how
    assert how == {"timeline": timeline, "quiet_after": session.thresholds.get("silence_duration", 0.6),
                   "wake_word": "Hey Nova"}


def test_only_the_wake_word_heard_is_said_so_rather_than_asked_of_claude(brain, connect, make_session):
    # All Nemotron sometimes hears of "Nova, pause.", even heard again - which
    # Claude could only guess at.
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation()
    brain.stt.texts = ["Nova."]
    feed = AudioFeed()
    feed.feed(said())
    feed.end()

    assert session._handle_interaction(feed, timeline=FakeTimeline()) is None
    assert session.conversation.heard == []
    assert brain.tts.said == ["Sorry, I didn't catch that."]


def test_speech_to_text_failing_is_said_to_have_gone_wrong(brain, connect, make_session):
    # Nemotron failed while listening and again on the whole recording - which isn't
    # the user not being caught, so isn't said to be.
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation()
    failed = FakeTranscription("")
    failed.finish = lambda audio, **how: {"text": "", "error": "Failed to decode"}
    brain.stt = SimpleNamespace(listen=lambda: failed)
    timeline = FakeTimeline()
    feed = AudioFeed()
    feed.feed(said())
    feed.end()

    assert session._handle_interaction(feed, timeline=timeline) is None
    assert session.conversation.heard == []
    assert brain.tts.said == ["Sorry, something went wrong there."]
    assert timeline.notes["outcome"] == "nothing_transcribed"


def test_words_imagined_in_noise_arent_apologised_for(brain, connect, make_session):
    # A cough or the television after a false wake word, heard as "Thank you." -
    # which stays as quiet as a false wake word with nothing in it.
    satellite, pi = connect()
    session = make_session(satellite)
    brain.stt = SimpleNamespace(listen=lambda: FakeTranscription("", imagined=True))
    timeline = FakeTimeline()
    feed = AudioFeed()
    feed.feed(said())
    feed.end()

    assert session._handle_interaction(feed, timeline=timeline) is None
    assert brain.tts.said == []
    assert timeline.notes["outcome"] == "nothing_transcribed"
    assert [message["name"] for message in pi.messages("play_sound")] == ["listening.wav", "not_heard.wav"]


def test_a_pressed_recording_of_words_imagined_in_noise_says_nothing(brain, connect, make_session):
    # A press has no false wake word to blame, and noise heard as words is nothing said.
    satellite, pi = connect()
    session = make_session(satellite)
    brain.stt = SimpleNamespace(listen=lambda: FakeTranscription("", imagined=True))
    timeline = FakeTimeline()
    feed = AudioFeed()
    feed.feed(said())
    feed.end()

    assert session._handle_interaction(feed, timeline=timeline, pressed=True) is None
    assert pi.messages("play_sound") == [] and brain.tts.said == []
    assert timeline.notes["outcome"] == "nothing_transcribed"


def test_speech_to_text_is_given_the_recording_as_its_made(brain, make_session):
    # It used to be given the whole recording once it was over, and on the Pi
    # Moonshine then took 0.83s to work out the words.
    session = make_session()
    session.conversation = ScriptedConversation(Reply())
    brain.stt.texts = ["What time is it?"]
    feed = AudioFeed()
    feed.feed(said())
    feed.end()

    assert session._handle_interaction(feed, timeline=FakeTimeline()) is None
    transcription = brain.stt.made[0]
    assert len(transcription.added) > 1
    # The whole recording too, for it to fall back on.
    assert np.array_equal(np.concatenate(transcription.added), transcription.finished_with)
    assert session.conversation.heard == [("What time is it?", False)]


def test_the_end_of_the_wake_word_sent_before_it_isnt_taken_for_the_user_talking(brain, make_session):
    # The satellite sends the moment before it heard the wake word first, which often
    # holds the end of "Nova". Listened to, that started the recording - and the pause
    # for the listening sound then ended it, with nothing in it but "Nova".
    session = make_session()
    end_of_nova = np.concatenate([np.zeros(round(0.1 * RATE)), np.full(round(0.2 * RATE), 8000)])
    feed = AudioFeed()
    feed.feed(np.concatenate([end_of_nova, said(before=0.8)]).astype(np.int16))
    feed.end()

    session._handle_interaction(feed, timeline=FakeTimeline())
    # All of what the user said, and none of "Nova".
    assert np.count_nonzero(brain.stt.made[0].finished_with) == round(0.5 * RATE)


def test_a_request_run_on_from_the_wake_word_is_heard_whole(brain, make_session):
    # "Hey Nova what time is it" said in one breath has often started on "what" by the
    # time the wake word is heard.
    session = make_session()
    feed = AudioFeed()
    feed.feed(said(seconds=0.8, before=0))
    feed.end()

    session._handle_interaction(feed, timeline=FakeTimeline())
    assert np.count_nonzero(brain.stt.made[0].finished_with) == round(0.8 * RATE)


def test_the_satellite_says_how_much_was_from_before_the_wake_word(brain, connect, make_session):
    # Each end had its own setting, which could drift apart - and a push-to-talk client
    # sends nothing from before. Going by the brain's 0.3 seconds, a quick "Yes" at the
    # very start wasn't listened to at all.
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply())
    brain.stt.texts = ["Yes."]
    running(session)

    pi.say("wake", talked_over=False, pre_roll=0)
    pi.send_audio(said(seconds=0.25, before=0))
    assert wait_for(lambda: pi.messages("allow"))
    assert session.conversation.heard == [("Yes.", False)]


def test_an_answer_begun_over_the_listening_sound_is_heard(brain, make_session):
    # The satellite sends an answer from the moment the brain asks for it, with nothing
    # from before - so there's nothing to skip.
    session = make_session()
    session.conversation = ScriptedConversation(Reply())
    brain.stt.texts = ["Yes."]
    feed = AudioFeed()
    feed.feed(said(seconds=0.2, before=0))
    feed.end()

    session._handle_interaction(feed, follow_up=True, timeline=FakeTimeline())
    assert session.conversation.heard == [("Yes.", True)]


def test_speech_to_text_is_let_go_if_the_recording_fails(brain, connect, make_session, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("feed gone")

    satellite, pi = connect()
    session = make_session(satellite)
    monkeypatch.setattr(session_module, "record_until_silence", broken)

    with pytest.raises(RuntimeError):
        # One the satellite is sending, as only that has a recording to stop.
        session._handle_interaction(satellite.listen(), timeline=FakeTimeline())
    assert brain.stt.made[0].cancelled
    assert wait_for(lambda: pi.messages("stop_listening"))


def test_saying_sorry_can_never_fail_in_turn(make_session):
    def broken_speech(message, timeline=None):
        raise RuntimeError("satellite gone")

    session = make_session()
    session._say = broken_speech
    session._apologise()


def test_saying_the_wake_word_over_a_reply_stops_it(connect, make_session):
    # A reply couldn't be talked over, so a fortnight of the calendar - three and a
    # half minutes of it - had to be sat through.
    satellite, pi = connect()
    session = make_session(satellite)
    pi.held = {1}
    timeline, heard_over = FakeTimeline(), []
    saying = threading.Thread(target=lambda: heard_over.append(
        session._say("Today, you've got...", timeline=timeline, listen=True)))
    saying.start()
    assert wait_for(lambda: pi.messages("play_stream"))

    pi.wake(said(), talked_over=True)
    saying.join(2)
    # The next request starts from what was said over it.
    assert heard_over[0]["talked_over"] and isinstance(heard_over[0]["feed"], AudioFeed)
    reply = pi.messages("play_stream")[0]["id"]
    assert wait_for(lambda: any(message.get("id") == reply for message in pi.messages("stop")))
    assert timeline.notes["talked_over"] is True


def test_what_is_left_until_after_the_reply_is_done_once_it_is_said(connect, make_session, monkeypatch):
    session = make_session(connect()[0])
    order = []
    reply = Reply(say="Putting your PC to sleep.", after=[lambda: order.append("press") or {"success": True}])
    monkeypatch.setattr(session, "_say", lambda message, timeline=None, listen=False: order.append("say"))
    session.conversation = ScriptedConversation(reply)
    session._reply_to("go to sleep", follow_up=False, timeline=FakeTimeline())
    assert order == ["say", "press"]


def test_claude_says_a_line_first_through_the_session_and_waits_for_it(connect, make_session, monkeypatch):
    session = make_session(connect()[0])
    spoken = []
    monkeypatch.setattr(session, "_say", lambda message, timeline=None, listen=False: spoken.append((message, listen)))
    # Said without listening for the wake word over it, which returns once it's been said - so the request which
    # follows isn't made until it has - and the answer is still being waited for.
    session.llm.say_first("Let me think about that.")
    assert spoken == [("Let me think about that.", False)] and session.activity == "thinking"
    # And so does the conversation, for "think harder" said in his own words.
    session.conversation.say_first("Let me think about that.")
    assert spoken[1:] == [("Let me think about that.", False)]


def test_after_runs_when_talked_over_or_nothing_is_said(connect, make_session, monkeypatch):
    session = make_session(connect()[0])
    ran = []
    monkeypatch.setattr(session, "_say", lambda message, timeline=None, listen=False: {"talked_over": True})
    after = [lambda: ran.append("press") or {"success": True}]
    session.conversation = ScriptedConversation(Reply(say="Locking.", after=after), Reply(after=after))
    session._reply_to("lock", follow_up=False, timeline=FakeTimeline())
    session._reply_to("lock", follow_up=False, timeline=FakeTimeline())
    assert ran == ["press", "press"]


def test_after_failure_logged_not_raised(connect, make_session, monkeypatch, caplog):
    session = make_session(connect()[0])
    ran = []

    def broken():
        raise RuntimeError("secret detail")

    monkeypatch.setattr(session, "_say", lambda message, timeline=None, listen=False: None)
    session.conversation = ScriptedConversation(Reply(say="Locking.", after=[
        broken, lambda: ran.append("next") or {"success": False, "message": "Couldn't."}]))
    with caplog.at_level(logging.WARNING):
        session._reply_to("lock", follow_up=False, timeline=FakeTimeline())
    assert ran == ["next"]
    assert "RuntimeError" in caplog.text and "secret detail" not in caplog.text


def test_a_wake_word_heard_just_as_the_reply_ends_starts_the_next_request(connect, make_session):
    # Missed as a question finished, the recording of the new request was taken for
    # the answer.
    satellite, pi = connect()
    session = make_session(satellite)
    satellite.wakes.put({"talked_over": False, "feed": AudioFeed()})
    finished = SimpleNamespace(done=True, duration=0.0)

    heard_over = session._heard_over(finished)
    assert heard_over is not None and isinstance(heard_over["feed"], AudioFeed)
    assert satellite.wakes.empty()


def test_what_the_satellite_never_says_has_finished_is_given_up_on(brain, connect, make_session, monkeypatch):
    # The session waited for good - stuck, with its satellite still connected and
    # pinging, so every wake word there got silence.
    monkeypatch.setattr(session_module, "PLAYBACK_SLACK", 0.2)
    satellite, pi = connect()
    pi.finishes_nothing = True
    session = make_session(satellite)
    done = []

    def wait_on_each():
        session._play_sound("listening.wav")
        done.append(session._say("It's sunny.", listen=True))
        session._say("Sorry, something went wrong there.")
        session._play_announcement(session.speaker.play_file(str(brain.sounds_folder / "announcement_sound.wav"),
                                                             kind="announcement"))
        done.append("all")

    waiting = threading.Thread(target=wait_on_each, daemon=True)
    waiting.start()
    waiting.join(10)
    assert done == [None, "all"]
    # Each is stopped, rather than left playing on the satellite.
    started = {message["id"] for message in pi.messages("play_sound") + pi.messages("play_stream")}
    assert len(started) == 4
    assert wait_for(lambda: started <= {message.get("id") for message in pi.messages("stop")})


def test_a_session_between_wakes_or_playing_a_long_reply_is_moving(brain, connect, make_session):
    # The brain stops telling systemd it's working once a session stops moving - see
    # Brain.last_moved - so one waiting for the next wake word, or saying three and a
    # half minutes of the calendar, mustn't look stuck.
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="Today, you've got..."))
    brain.stt.texts = ["What's on today?"]
    pi.held = {1}
    made_at = session.moved_at
    running(session)
    assert wait_for(lambda: session.moved_at > made_at)

    pi.wake(said())
    assert wait_for(lambda: pi.messages("play_stream"))
    playing = session.moved_at
    assert wait_for(lambda: session.moved_at > playing)
    pi.say("played", id=pi.messages("play_stream")[0]["id"])
    assert wait_for(lambda: pi.messages("allow"))


def test_a_session_stuck_in_a_turn_stops_moving(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    stuck, let_go = threading.Event(), threading.Event()

    class Stuck(ScriptedConversation):
        def handle(self, text, follow_up=False, timeline=None):
            stuck.set()
            let_go.wait(5)
            return Reply()

    session.conversation = Stuck()
    brain.stt.texts = ["What's on today?"]
    running(session)
    pi.wake(said())
    try:
        assert stuck.wait(5)
        moved_at = session.moved_at
        time.sleep(2 * session_module.WAKE_CHECK)
        assert session.moved_at == moved_at
    finally:
        let_go.set()


def test_a_session_stops_moving_while_speech_to_text_has_hung(brain, connect, make_session):
    # Every request was only apologised for, after FINISH_LIMIT, and nothing restarted the
    # brain, which loads it again - see NemotronSpeechToText.hung.
    satellite, pi = connect()
    session = make_session(satellite)
    brain.stt.hung = True
    moved_at = session.moved_at
    running(session)
    time.sleep(3 * session_module.WAKE_CHECK)
    assert session.moved_at == moved_at

    # One finished in the end, so it's getting on again.
    brain.stt.hung = False
    assert wait_for(lambda: session.moved_at > moved_at)


def test_a_long_turn_moves_as_each_of_claudes_replies_comes(make_session, monkeypatch):
    # A routine of slow steps, said to the satellite, looked stuck once it outlasted the
    # watchdog's allowance - see Conversation's progress.
    session = make_session()
    monkeypatch.setattr(session.llm, "process_query",
                        lambda text, **kwargs: {"response": "Paris.", "actions": [], "success": True})
    session.moved_at = moved_at = time.monotonic() - 100
    assert session.conversation.handle("what's the capital of France").say == "Paris."
    assert session.moved_at > moved_at


def test_a_reply_nobody_talks_over_plays_to_the_end(connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    assert session._say("It's sunny.", listen=True) is None
    assert pi.messages("stream_end") and pi.messages("stop") == []


def test_a_wake_word_heard_while_the_reply_was_worked_out_is_answered_instead(brain, connect, make_session):
    # The satellite is already sending what the user says next, so the reply mustn't
    # be said over them - and it counts as talking over it, so "Hey Nova, stop" means
    # don't say it, rather than pausing the music for good.
    satellite, pi = connect()
    session = make_session(satellite)
    pi.wake(said(), talked_over=False)
    assert wait_for(lambda: not satellite.wakes.empty())

    heard_over = session._say("It's sunny.", listen=True)
    assert heard_over["talked_over"] is True and isinstance(heard_over["feed"], AudioFeed)
    assert brain.tts.said == [] and pi.messages("play_stream") == []


def test_talking_over_a_reply_starts_the_next_request_with_the_music_still_held(make_session):
    session = make_session()
    held = []
    session.music = SimpleNamespace(hold=lambda: held.append("hold"), release=lambda: held.append("release"))
    session.conversation = ScriptedConversation()
    heard_over = [{"talked_over": True, "feed": AudioFeed()}, None]
    session._handle_interaction = lambda feed, timeline, pre_roll=None, pressed=False: heard_over.pop(0)

    # Woken over a chime, which isn't talking over a reply.
    session._converse({"talked_over": True, "feed": AudioFeed()})
    # The second request knows the first reply was talked over.
    assert session.conversation.started == [False, True]
    # The music stays paused between the two, rather than coming back for a moment.
    assert held == ["hold", "release"]
    assert session._idle.is_set() and session.activity == "idle"


def _noting_holds(held, *named):
    """Gives each (name, session) music which notes, in held, each hold and release of it."""
    for name, session in named:
        session.music = SimpleNamespace(hold=lambda name=name: held.append(("hold", name)),
                                        release=lambda name=name: held.append(("release", name)))


def test_a_wake_holds_the_music_on_the_whole_room(make_session, brain):
    # Spoken to at the PC, the Pi beside it is quiet too while he's heard.
    pc, pi = make_session(), make_session()
    held = []
    _noting_holds(held, ("pc", pc), ("pi", pi))
    brain.room_sessions = lambda name: [pc, pi]
    pc.conversation = ScriptedConversation()

    def answer(feed, timeline, pre_roll=None, pressed=False):
        # The room changing part way through doesn't change what's let go of.
        brain.room_sessions = lambda name: [pc]
        return None

    pc._handle_interaction = answer
    pc._converse({"talked_over": False, "feed": AudioFeed()})
    assert held == [("hold", "pc"), ("hold", "pi"), ("release", "pc"), ("release", "pi")]


@pytest.mark.parametrize("ringing", [True, False])
def test_what_goes_off_holds_the_music_on_the_whole_room(make_session, brain, ringing):
    # A timer ringing on the Pi isn't lost under music playing on the PC beside it.
    pi, pc = make_session(), make_session()
    held = []
    for name, session in (("pi", pi), ("pc", pc)):
        session.music = SimpleNamespace(hold=lambda name=name: held.append(("hold", name)),
                                        release=lambda after=0.0, name=name: held.append(("release", name, after)))
    brain.room_sessions = lambda name: [pi, pc]

    def ring(*args):
        # The room changing part way through doesn't change what's let go of.
        brain.room_sessions = lambda name: [pi]
        return True

    pi._announce_in_turn = ring
    assert pi.announce("Your timer is done.", ringing=ringing) is True
    assert held == [("hold", "pi"), ("hold", "pc"), ("release", "pi", 5), ("release", "pc", 5)]

    # Alone in its room, only its own, as before.
    held.clear()
    pi.announce("Your timer is done.", ringing=ringing)
    assert held == [("hold", "pi"), ("release", "pi", 5)]


def test_a_room_which_cant_be_looked_up_still_has_the_wake_answered_and_let_go(make_session, brain):
    # Held and let go alone, rather than the session being left busy for good.
    pi = make_session()
    held = []
    _noting_holds(held, ("pi", pi))

    def unreadable(name):
        raise OSError("disk gone")

    brain.room_sessions = unreadable
    pi.conversation = ScriptedConversation()
    answered = []
    pi._handle_interaction = lambda feed, timeline, pre_roll=None, pressed=False: answered.append(feed)
    pi._converse({"talked_over": False, "feed": AudioFeed()})
    assert len(answered) == 1
    assert held == [("hold", "pi"), ("release", "pi")]
    assert pi._idle.is_set() and pi.activity == "idle"


def test_a_satellite_alone_holds_only_its_own(make_session, brain):
    pi, kitchen = make_session(), make_session()
    held = []
    _noting_holds(held, ("pi", pi), ("kitchen", kitchen))
    brain.room_sessions = lambda name: [pi]
    pi.conversation = ScriptedConversation()
    pi._handle_interaction = lambda feed, timeline, pre_roll=None, pressed=False: None
    pi._converse({"talked_over": False, "feed": AudioFeed()})
    assert held == [("hold", "pi"), ("release", "pi")]


# Moving the music to another satellite, and playing something there

class MovingPlayback:
    """One song 'playing' on a satellite, paused or not until it's stopped."""

    sample_rate = 48000

    def __init__(self, paused, volume=1.0):
        self.paused = paused
        self.volume = volume
        self.stopped = False
        self.position = 0
        self.duration = 200.0
        self._done = threading.Event()

    def wait(self, timeout=None):
        return self._done.wait(timeout)

    def stop(self):
        self.stopped = True
        self._done.set()


class MovingSpeaker:
    """A satellite's speaker, keeping each song it was asked to play."""

    sample_rate = 48000

    def __init__(self):
        self.started = []

    def play_stream(self, song, volume=1.0, kind="reply", paused=False, most_ahead=None):
        self.started.append(MovingPlayback(paused, volume))
        return self.started[-1]


def _with_navidrome(session):
    """Gives a session a real Navidrome player on a speaker of its own, returning the speaker."""
    speaker = MovingSpeaker()
    player = NavidromePlayer("http://navidrome.local:4533", "nova", "secret", speaker, session.volume)
    # The song itself stands in for its audio.
    player._decode = lambda song, start=0.0: song
    session.music_player = session.actions.music = player
    session.music = music.MusicHold(player)
    return speaker


def test_move_during_a_reply_waits_for_it_and_the_source_stays_quiet(brain, connect, make_session):
    # Talking to the Pi, with the PC in the same room - both held while he's heard.
    pi, pc = make_session(connect("bedroom")[0]), make_session(connect("pc")[0])
    pi_speaker, pc_speaker = _with_navidrome(pi), _with_navidrome(pc)
    sessions, spoken = {"bedroom": pi, "pc": pc}, {"bedroom": "the Pi", "pc": "the PC"}
    brain.room_sessions = lambda name: [pi, pc] if name == "bedroom" else [pc, pi]
    brain.connected_sessions = lambda: [pi, pc]
    brain.session_for = sessions.get
    brain.find_satellite = lambda said, here=None: ("pc", None)
    brain.spoken_name = spoken.get
    brain.move_music = MethodType(Brain.move_music, brain)

    songs = [{"id": str(number), "title": f"Song {number}", "artist": "Band"} for number in range(1, 4)]
    pi.music_player._start(songs)
    assert wait_for(lambda: pi_speaker.started and not pi_speaker.started[0].paused)
    pi.conversation = ScriptedConversation()
    moved, during = [], []

    def answer(feed, timeline, pre_roll=None, pressed=False):
        moved.append(pi.actions.execute(Action("move_music", {"to": "the PC"})))
        # The reply isn't over, so the PC has the song ready but is not playing it.
        during.append(wait_for(lambda: pc_speaker.started and pc_speaker.started[-1].paused))
        return None

    pi._handle_interaction = answer
    pi._converse({"talked_over": False, "feed": AudioFeed()})

    assert [(result.ok, result.say) for result in moved] == [(True, "Moved to the PC.")]
    assert during == [True]
    # Let go at the end of the conversation, the PC plays on from the first song, and the Pi has none.
    pi.music.wait(), pc.music.wait()
    assert wait_for(lambda: not pc_speaker.started[-1].paused)
    assert len(pc_speaker.started) == 1 and pc.music_player.is_playing()
    assert not pi.music_player.has_music() and not pi.music_player.is_playing()
    assert len(pi_speaker.started) == 1 and pi_speaker.started[0].stopped


def test_elsewhere_gives_the_named_sessions_executor(brain, connect, make_session):
    pi, pc = make_session(connect("bedroom")[0]), make_session(connect("pc")[0])
    named = {"the Pi": "bedroom", "the PC": "pc", "the Mac": "mac"}
    brain.find_satellite = lambda said, here=None: (named[said], None) if said in named else (None, f"Which one: {said}?")
    brain.session_for = {"bedroom": pi, "pc": pc}.get
    brain.spoken_name = {"mac": "the Mac"}.get
    asked = []
    brain.move_music = lambda spoken_at, said: asked.append((spoken_at, said)) or {"success": True, "message": "Moved."}

    assert pi.actions.elsewhere("the PC") == (pc.actions, None)
    # Itself too, which the executor takes for "here".
    assert pi.actions.elsewhere("the Pi") == (pi.actions, None)
    # One which is away, or no one at all, as the brain says it.
    assert pi.actions.elsewhere("the Mac") == (None, "The Mac isn't connected.")
    assert pi.actions.elsewhere("the phone") == (None, "Which one: the phone?")
    # A move is asked of the brain from the satellite spoken to.
    assert pi.actions.mover("the PC") == {"success": True, "message": "Moved."}
    assert asked == [("bedroom", "the PC")]


def test_elsewhere_says_which_satellite_is_here(brain, connect, make_session):
    pc = make_session(connect("pc")[0])
    heard = []
    brain.find_satellite = lambda said, here=None: heard.append((said, here)) or ("pc", None)
    brain.session_for = {"pc": pc}.get
    # "On this PC" said at the PC is the PC's own name for itself, which only the session knows.
    assert pc.actions.elsewhere("this device") == (pc.actions, None)
    assert heard == [("this device", "pc")]


def test_a_sessions_actions_know_how_a_satellite_is_called_and_how_to_wait_for_its_music(brain, make_session):
    session = make_session()
    brain.spoken_name = {"bedroom": "the Pi", "pc": "the PC"}.get
    assert session.actions.spoken_name("pc") == "the PC"
    waited = []
    session.music = SimpleNamespace(wait=lambda: waited.append("waited"))
    session.actions.settled()
    assert waited == ["waited"]


@pytest.mark.parametrize("in_one_room", [True, False])
def test_playing_on_another_satellite_pauses_the_rooms_own_music_for_good_when_it_shares_the_room(
        brain, connect, make_session, in_one_room):
    # At the Pi, with Drake asked for on the PC: in the same room, the Pi's own song is paused, as "pause"
    # would, and stays so once the reply is over - and in another room it carries on, as it did.
    pi, pc = make_session(connect("bedroom")[0]), make_session(connect("pc")[0])
    pi_speaker, pc_speaker = _with_navidrome(pi), _with_navidrome(pc)
    sessions, spoken = {"bedroom": pi, "pc": pc}, {"bedroom": "the Pi", "pc": "the PC"}
    brain.room_sessions = lambda name: [pi, pc] if in_one_room else [pi]
    brain.session_for = sessions.get
    brain.find_satellite = lambda said, here=None: ("pc", None)
    brain.spoken_name = spoken.get
    songs = [{"id": str(number), "title": f"Song {number}", "artist": "Band"} for number in range(1, 4)]

    def play(query=None, search_type=None):
        pc.music_player._start(songs)
        return {"success": True, "message": f"Playing {query}"}

    pc.music_player.play = play
    pi.music_player._start(songs)
    assert wait_for(lambda: pi_speaker.started and not pi_speaker.started[0].paused)
    pi.conversation = ScriptedConversation()
    played = []

    def answer(feed, timeline, pre_roll=None, pressed=False):
        # As the conversation does before it runs what was asked for.
        pi.music.wait()
        played.append(pi.actions.execute(Action("play_music", {"query": "Drake", "on": "the PC"})))
        return None

    pi._handle_interaction = answer
    pi._converse({"talked_over": False, "feed": AudioFeed()})

    assert [(result.ok, result.say) for result in played] == [(True, "Playing Drake on the PC.")]
    pi.music.wait(), pc.music.wait()
    assert wait_for(lambda: pc_speaker.started and not pc_speaker.started[-1].paused)
    if in_one_room:
        # Still queued, so it can be carried on, but paused for good.
        assert pi.music_player.has_music() and not pi.music_player.is_playing()
        assert pi_speaker.started[0].paused and not pi_speaker.started[0].stopped
    else:
        assert wait_for(lambda: pi.music_player.is_playing() and not pi_speaker.started[0].paused)
    assert len(pi_speaker.started) == 1


# Notes saved at a computer

def _brain_choosing_notes(brain, sessions, saving=("pc",)):
    """
    Has the brain here choose the computer a note is saved on, and name it, as the real Brain does,
    over a Pi ("bedroom", the default) and a PC ("pc") - the PC saying it saves notes unless saving says otherwise.

    Args:
        sessions: The sessions connected, by satellite name - what Brain.session_for finds.
        saving: The satellites which say they save notes.
    """
    brain.rooms.seen("bedroom", False, "pi", "bedroom" in saving)
    brain.rooms.seen("pc", True, "pc", "pc" in saving)
    brain.satellites = lambda: ["bedroom", "pc"]
    brain.session_for = sessions.get
    for method in ("_named_satellites", "find_satellite", "spoken_name", "not_connected", "notes_for",
                   "notes_saved_on"):
        setattr(brain, method, MethodType(getattr(Brain, method), brain))


def _noting(satellite, answers=None):
    """Has a satellite answer each note it's asked to save from answers, else as Bins.txt, and returns what it was asked."""
    asked = []

    def save_note(title, text, wait=5.0):
        asked.append((title, text))
        return answers.pop(0) if answers else {"file": "Bins.txt"}

    satellite.save_note = save_note
    return asked


def _at_the_pc(brain, connect, make_session, answers=None, saves_notes=True):
    """
    A session for the PC, which says it saves notes unless told it doesn't, with the notes
    its satellite was asked to save - each a title and text - which it answers from answers.

    Returns:
        The session, and the notes the satellite was asked to save.
    """
    satellite, _ = connect("pc")
    satellite.saves_notes = saves_notes
    asked = _noting(satellite, answers)
    session = make_session(satellite)
    _brain_choosing_notes(brain, {"pc": session}, saving=("pc",) if saves_notes else ())
    return session, asked


def _at_the_pi(brain, connect, make_session, answers=None, pc_connected=True, saving=("pc",)):
    """
    A session for the Pi, which saves no notes, and one for the PC, which is called Nathaniel's PC on the settings page.

    Returns:
        The Pi's session, the PC's session, the notes the PC was asked to save and those the Pi was.
    """
    pi_satellite, pc_satellite = connect("bedroom")[0], connect("pc")[0]
    pc_satellite.saves_notes = "pc" in saving
    pc_asked, pi_asked = _noting(pc_satellite, answers), _noting(pi_satellite)
    pi, pc = make_session(pi_satellite), make_session(pc_satellite)
    _brain_choosing_notes(brain, {"bedroom": pi, **({"pc": pc} if pc_connected else {})}, saving=saving)
    brain.rooms.set_called("pc", ["Nathaniel's PC"])
    return pi, pc, pc_asked, pi_asked


def _note(session, title="Bins", text="Green on Tuesday.", **params):
    return session.actions.execute(Action("save_note", {"title": title, "text": text, **params}, from_model=True))


def test_save_note_reply_names_the_file_without_extension(brain, connect, make_session):
    session, asked = _at_the_pc(brain, connect, make_session, [{"file": "Bins.txt"}, {"file": "Bins (2).txt"},
                                                                {"file": "Bins.txt.txt"}, {"file": "Notes"}])
    assert "save_note" in {tool["name"] for tool in session.actions.tool_schemas()}
    result = _note(session)
    assert (result.ok, result.say, result.confirm) == (True, "Saved to your Downloads as Bins.", "speak")
    assert asked == [("Bins", "Green on Tuesday.")]
    # Only a final ".txt" goes: the " (2)" is part of the name.
    assert _note(session).say == "Saved to your Downloads as Bins (2)."
    assert _note(session).say == "Saved to your Downloads as Bins.txt."
    assert _note(session).say == "Saved to your Downloads as Notes."


def test_save_note_error_is_said(brain, connect, make_session):
    session, _ = _at_the_pc(brain, connect, make_session,
                            [{"error": "There's no Downloads folder to save it in."}])
    result = _note(session)
    assert (result.ok, result.say) == (
        False, "I couldn't save it on the PC: There's no Downloads folder to save it in.")


def test_save_note_no_answer(brain, connect, make_session):
    # None is no answer in time, or the satellite going.
    session, asked = _at_the_pc(brain, connect, make_session, [None])
    result = _note(session)
    assert (result.ok, result.say) == (False, "The PC didn't answer, so the note wasn't saved.")
    assert asked == [("Bins", "Green on Tuesday.")]


def test_a_note_too_long_is_refused_before_anything_is_sent(brain, connect, make_session):
    session, asked = _at_the_pc(brain, connect, make_session)
    result = _note(session, text="x" * (notes.MOST_TEXT + 1))
    assert (result.ok, result.say) == (False, "I couldn't save it on the PC: That note's too long to save.")
    assert asked == []
    # The most there is, is sent.
    assert _note(session, text="x" * notes.MOST_TEXT).ok and len(asked) == 1


def test_a_note_which_cant_be_looked_up_by_name_is_still_saved(brain, connect, make_session):
    # The name is only wanted to say it couldn't, so a note saved here isn't reported lost over it.
    session, asked = _at_the_pc(brain, connect, make_session)

    def broken(name):
        raise OSError("the satellites can't be read")

    brain.spoken_name = broken
    result = _note(session)
    assert (result.ok, result.say) == (True, "Saved to your Downloads as Bins.")
    assert len(asked) == 1


def test_a_note_is_saved_through_the_satellite_and_its_text_is_never_logged(brain, connect, make_session, caplog):
    satellite, pi = connect("pc")
    satellite.saves_notes = True
    session = make_session(satellite)
    _brain_choosing_notes(brain, {"pc": session})
    done = []
    caplog.set_level(logging.DEBUG)
    thread = threading.Thread(target=lambda: done.append(_note(session, "Bins", "Green on Tuesday, secret.")))
    thread.start()
    assert wait_for(lambda: pi.messages("save_note"))
    # As the PC's satellite answers, with the file as it was made.
    pi.say("note_saved", id=pi.messages("save_note")[0]["id"], file="Bins (2).txt")
    thread.join(5)

    assert [(result.ok, result.say) for result in done] == [(True, "Saved to your Downloads as Bins (2).")]
    sent = [(message, payload) for message, payload in pi.received if message["type"] == "save_note"]
    assert [(message["title"], payload) for message, payload in sent] == [("Bins", b"Green on Tuesday, secret.")]
    # The title and the file are fine to log - never what it says.
    assert "secret" not in caplog.text
    assert "Bins (2).txt" in caplog.text


def test_a_note_at_the_pi_is_offered_while_a_computer_saves_notes(brain, connect, make_session):
    pi, pc, _, _ = _at_the_pi(brain, connect, make_session)
    assert pi.satellite.saves_notes is False
    assert "save_note" in {tool["name"] for tool in pi.actions.tool_schemas()}
    # Nor does it depend on the PC being connected: it's the one which saves them, asleep or not.
    asleep, _, _, _ = _at_the_pi(brain, connect, make_session, pc_connected=False)
    assert "save_note" in {tool["name"] for tool in asleep.actions.tool_schemas()}


def test_a_note_is_not_offered_when_no_satellite_saves_notes(brain, connect, make_session):
    pi, pc, _, _ = _at_the_pi(brain, connect, make_session, saving=())
    for session in (pi, pc):
        assert "save_note" not in {tool["name"] for tool in session.actions.tool_schemas()}
    # Asked for by name anyway, it's said rather than sent anywhere.
    result = _note(pi)
    assert (result.ok, result.say) == (False, "None of your computers can save notes.")


def test_a_note_named_for_the_pc_at_the_pi_is_saved_on_the_pc(brain, connect, make_session):
    pi, pc, pc_asked, pi_asked = _at_the_pi(brain, connect, make_session, [{"file": "Swimming.txt"}])
    result = _note(pi, "Swimming", "I'm going swimming.", where="my PC")
    assert (result.ok, result.say, result.confirm) == (
        True, "Saved to the Downloads on Nathaniel's PC as Swimming.", "speak")
    assert pc_asked == [("Swimming", "I'm going swimming.")] and pi_asked == []


def test_a_note_without_a_computer_named_at_the_pi_goes_to_the_one_which_saves_notes(brain, connect, make_session):
    pi, pc, pc_asked, pi_asked = _at_the_pi(brain, connect, make_session, [{"file": "Swimming.txt"}])
    # Said for the user to hear where it went, as it isn't where they are.
    result = _note(pi, "Swimming", "I'm going swimming.")
    assert (result.ok, result.say) == (True, "Saved to the Downloads on Nathaniel's PC as Swimming.")
    assert pc_asked == [("Swimming", "I'm going swimming.")] and pi_asked == []


def test_a_note_at_the_pc_is_said_to_be_saved_there_named_or_not(brain, connect, make_session):
    # With or without the PC named, at the PC itself it's saved there, as the user at it hears.
    pc, asked = _at_the_pc(brain, connect, make_session)
    assert _note(pc).say == "Saved to your Downloads as Bins."
    assert _note(pc, where="this PC").say == "Saved to your Downloads as Bins."
    assert _note(pc, where="").say == "Saved to your Downloads as Bins."
    # A model which gives something which isn't words is taken as none.
    assert _note(pc, where=5).say == "Saved to your Downloads as Bins."
    assert len(asked) == 4


def test_a_note_named_for_a_satellite_which_cant_save_notes_says_so(brain, connect, make_session):
    pi, pc, pc_asked, pi_asked = _at_the_pi(brain, connect, make_session)
    result = _note(pc, where="the Pi")
    assert (result.ok, result.say) == (False, "The Pi can't save notes.")
    assert pc_asked == [] and pi_asked == []


class FakeLists:
    """Stands in for Lists, saying what it was asked to add and, for each, what Lists says of the list it went on."""

    def __init__(self, reply=None):
        self.added = []
        self.reply = reply

    def add(self, items, said):
        self.added.append((items, said))
        if self.reply is not None:
            return self.reply
        return {"success": True, "message": f"Added {items[0]} to your notepad list."}


def _pc_asleep(brain, connect, make_session, lists=None, off=True):
    """
    The Pi's session with the PC not connected, and the lists to put notes on.

    Args:
        lists: The lists, or None for FakeLists.
        off: Whether Home Assistant says the PC is off ("Your PC's off.") - else only that Nova isn't running on it.

    Returns:
        The Pi's session, the lists, and the notes the PC was asked to save (none, while it's off).
    """
    pi, pc, pc_asked, _ = _at_the_pi(brain, connect, make_session, pc_connected=False)
    if off:
        brain.not_connected = lambda name: "Your PC's off."
    brain.lists = FakeLists() if lists is None else lists
    return pi, brain.lists, pc_asked


def test_a_note_for_a_pc_which_is_asleep_goes_on_the_notepad_list(brain, connect, make_session):
    pi, lists, pc_asked = _pc_asleep(brain, connect, make_session)
    # Named or not: the PC is the one which would have saved it.
    for params in ({"where": "my PC"}, {}):
        result = _note(pi, "Swimming", "I'm going swimming.", **params)
        assert (result.ok, result.say, result.confirm) == (
            True, "Your PC's off, so I've put it on your notepad list instead.", "speak")
    # The list Nova means by "notes", as manage_list adds to it.
    assert lists.added == [(["I'm going swimming."], "notes")] * 2
    assert pc_asked == []


def test_a_note_for_a_pc_with_no_nova_running_goes_on_the_list_too(brain, connect, make_session):
    pi, lists, _ = _pc_asleep(brain, connect, make_session, off=False)
    result = _note(pi, "Swimming", "I'm going swimming.", where="my PC")
    assert (result.ok, result.say) == (
        True, "Nova isn't running on Nathaniel's PC, so I've put it on your notepad list instead.")
    assert lists.added == [(["I'm going swimming."], "notes")]


def test_a_note_put_on_the_list_says_the_name_the_list_has(brain, connect, make_session, tmp_path):
    # As the real Lists words it, for a list Home Assistant calls "Notes".
    class FakeHome:
        added = []

        def todo_lists(self):
            return [{"id": "todo.notes", "name": "Notes", "count": 0, "aliases": []}]

        def add_item(self, list_id, item):
            self.added.append((list_id, item))

    home = FakeHome()
    lists = Lists(home, ChoiceStore(str(tmp_path / "lists.json"), {"main": None}))
    pi, _, _ = _pc_asleep(brain, connect, make_session, lists=lists)
    # The note's own "to your" isn't taken for where the list's name starts.
    result = _note(pi, "Gift", "Send it to your mum.", where="my PC")
    assert (result.ok, result.say) == (True, "Your PC's off, so I've put it on your notes list instead.")
    assert home.added == [("todo.notes", "Send it to your mum.")]


def test_a_note_put_on_the_list_is_one_line(brain, connect, make_session):
    pi, lists, _ = _pc_asleep(brain, connect, make_session)
    assert _note(pi, "Shopping", "  Milk\r\n eggs \n\n\nbread\n").ok
    assert lists.added == [(["Milk eggs bread"], "notes")]


def test_a_note_put_on_the_list_is_added_up_to_255_characters(brain, connect, make_session):
    pi, lists, _ = _pc_asleep(brain, connect, make_session)
    assert _note(pi, text="x" * 255).ok
    assert lists.added == [(["x" * 255], "notes")]
    # Counted as it goes on the list, not as it was said: a last newline is no more.
    assert _note(pi, text="x" * 255 + "\n").ok
    assert lists.added[-1] == (["x" * 255], "notes")
    result = _note(pi, text="x" * 256)
    assert (result.ok, result.say) == (
        False, "Your PC's off, and that note's too long for your notepad list, so it wasn't saved.")
    assert len(lists.added) == 2


def test_a_note_which_the_list_wont_take_says_so(brain, connect, make_session):
    for reply in ({"success": False, "message": "I couldn't add that to your notepad list."},
                  {"success": False, "message": "I haven't got a list called that."}):
        pi, lists, _ = _pc_asleep(brain, connect, make_session, lists=FakeLists(reply))
        result = _note(pi, where="my PC")
        assert (result.ok, result.say) == (False, "Your PC's off, and I couldn't put it on your notepad list either.")
        assert len(lists.added) == 1
    # With no Home Assistant there are no lists at all.
    pi, lists, _ = _pc_asleep(brain, connect, make_session)
    brain.lists = None
    result = _note(pi, where="my PC")
    assert (result.ok, result.say) == (False, "Your PC's off, and I couldn't put it on your notepad list either.")


def test_a_note_which_the_list_was_slow_to_take_says_nova_is_unsure(brain, connect, make_session):
    # Home Assistant may well have added it, so it isn't said to have failed.
    reply = {"success": False, "unsure": True, "message": "Home Assistant was slow, so I'm not sure ... Have a look."}
    pi, lists, _ = _pc_asleep(brain, connect, make_session, lists=FakeLists(reply))
    result = _note(pi, where="my PC")
    assert (result.ok, result.say) == (
        False, "Your PC's off, and Home Assistant was slow, so I'm not sure it went on your notepad list. Have a look.")
    assert len(lists.added) == 1


def test_a_note_put_on_the_list_never_logs_its_text(brain, connect, make_session, caplog):
    pi, lists, _ = _pc_asleep(brain, connect, make_session)
    caplog.set_level(logging.DEBUG)
    assert _note(pi, "Bins", "Green on Tuesday, secret.", where="my PC").ok
    assert "secret" not in caplog.text


def test_a_note_is_not_put_on_the_list_for_any_other_reason(brain, connect, make_session):
    # Where it could still be, or wasn't asked for, or can't be helped: said as it was before.
    pi, pc, pc_asked, pi_asked = _at_the_pi(brain, connect, make_session, [None, {"error": "The folder's full."}])
    brain.lists = lists = FakeLists()
    # The PC didn't answer in time, so the file may yet land.
    assert _note(pi, where="my PC").say == "Nathaniel's PC didn't answer, so the note wasn't saved."
    # The PC answered with an error.
    assert _note(pi, where="my PC").say == "I couldn't save it on Nathaniel's PC: The folder's full."
    # The text is over what a note may be, which the brain refuses before it is sent.
    assert _note(pi, text="x" * (notes.MOST_TEXT + 1)).say == (
        "I couldn't save it on Nathaniel's PC: That note's too long to save.")
    # A satellite which can't save notes was named.
    assert _note(pc, where="the Pi").say == "The Pi can't save notes."
    assert lists.added == []
    # Two computers could have saved it: which one is asked.
    brain.rooms.seen("mac", True, "mac", True)
    brain.satellites = lambda: ["bedroom", "pc", "mac"]
    assert _note(pi).say == "Which one: Nathaniel's PC or the Mac?"
    assert lists.added == []


def test_a_note_at_a_computer_which_is_connected_is_not_put_on_the_list(brain, connect, make_session):
    pi, pc, pc_asked, _ = _at_the_pi(brain, connect, make_session)
    brain.lists = lists = FakeLists()
    assert _note(pi, where="my PC").say == "Saved to the Downloads on Nathaniel's PC as Bins."
    assert _note(pc).say == "Saved to your Downloads as Bins."
    assert lists.added == [] and len(pc_asked) == 2


def test_a_note_which_goes_wrong_on_another_satellite_names_it(brain, connect, make_session):
    pi, pc, pc_asked, _ = _at_the_pi(brain, connect, make_session, [None, {"error": "The folder's full."}])
    result = _note(pi, where="my PC")
    assert (result.ok, result.say) == (False, "Nathaniel's PC didn't answer, so the note wasn't saved.")
    result = _note(pi, where="my PC")
    assert (result.ok, result.say) == (False, "I couldn't save it on Nathaniel's PC: The folder's full.")
    assert len(pc_asked) == 2


def test_a_note_too_long_for_another_satellite_is_refused_before_anything_is_sent(brain, connect, make_session):
    pi, pc, pc_asked, _ = _at_the_pi(brain, connect, make_session)
    result = _note(pi, text="x" * (notes.MOST_TEXT + 1), where="my PC")
    assert (result.ok, result.say) == (
        False, "I couldn't save it on Nathaniel's PC: That note's too long to save.")
    assert pc_asked == []


def test_a_note_saved_on_another_satellite_never_logs_its_text(brain, connect, make_session, caplog):
    pi, pc, _, _ = _at_the_pi(brain, connect, make_session)
    caplog.set_level(logging.DEBUG)
    assert _note(pi, "Bins", "Green on Tuesday, secret.", where="my PC").ok
    assert "secret" not in caplog.text
    # What's logged says where it went.
    assert "Bins.txt" in caplog.text and "pc satellite" in caplog.text


def test_every_session_can_open_things_on_a_computer(brain, connect, make_session):
    session = make_session()
    computer = session.actions.computer
    assert isinstance(computer, ComputerControl) and computer.brain is brain and computer.satellite == "bedroom"
    # Offered once a computer which opens things has been seen, wherever the user is.
    assert "open_on_computer" not in {tool["name"] for tool in session.actions.tool_schemas()}
    brain.computers.set_apps("pc", [])
    assert "open_on_computer" in {tool["name"] for tool in session.actions.tool_schemas()}


def test_the_pi_is_offered_notes_but_saves_none_itself(brain, connect, make_session):
    # A satellite which doesn't say it saves notes - the Pi, or one from before there were any - is never
    # asked to save one: its notes go to a computer which does.
    pi, pc, pc_asked, pi_asked = _at_the_pi(brain, connect, make_session)
    assert pi.actions.notes is not None
    assert _note(pi).ok and _note(pi, where="my PC").ok
    assert pi_asked == [] and len(pc_asked) == 2
    # And with no computer which does, it's said, not sent.
    alone, _ = _at_the_pc(brain, connect, make_session, saves_notes=False)
    assert "save_note" not in {tool["name"] for tool in alone.actions.tool_schemas()}
    result = _note(alone)
    assert (result.ok, result.say) == (False, "None of your computers can save notes.")


def test_add_to_my_notes_is_still_the_list_at_the_pc(brain, connect, make_session):
    session, asked = _at_the_pc(brain, connect, make_session)
    # As it is once Home Assistant is set up: the lists are its.
    session.intents.home = object()
    for said in ("Add milk to my notes.", "Add milk to notes.", "Put milk on my notes list."):
        result = session.intents.match(said)
        assert [action.tool for action in result["actions"]] == ["add_to_list"], said
        assert result["actions"][0].params["item"] == "milk"
    assert asked == []


def test_closing_lets_go_of_the_device_the_music_and_the_satellites_sounds_once_in_order(make_session):
    class Recorder:
        def __init__(self, name, closed):
            self.name, self.closed = name, closed

        def stop(self):
            self.closed.append(self.name)

        def close(self):
            self.closed.append(self.name)

    session = make_session()
    closed = []
    session.device = Recorder("device", closed)
    session.music_player = Recorder("music", closed)
    session.speaker = Recorder("speaker", closed)

    session.close()
    session.close()
    assert closed == ["device", "music", "speaker"]


# Each satellite's own

def test_the_session_sends_its_satellite_the_longest_request(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    session.send_state()
    assert wait_for(lambda: pi.messages("state"))
    assert pi.messages("state")[-1]["longest_request"] == 15.0

    # Read as it is sent, so a save reaches it with no restart.
    brain.config.apply({("thresholds", "recording_timeout"): 22.0})
    session.send_state()
    assert wait_for(lambda: len(pi.messages("state")) == 2)
    assert pi.messages("state")[-1]["longest_request"] == 22.0


def test_a_push_to_talk_satellite_is_sent_its_screen_orb(brain, connect, make_session):
    pc, pc_link = connect("pc", "pc.local")
    pc.push_to_talk = True
    session = make_session(pc)
    session.send_state()
    assert wait_for(lambda: pc_link.messages("state"))
    assert pc_link.messages("state")[-1]["screen_orb"] == {"on": True, "place": "bottom centre", "size": "medium"}

    # Read as it is sent, so a save reaches the PC with no restart.
    brain.config.apply({("computer", "screen_orb_size"): "large"})
    session.send_state()
    assert wait_for(lambda: len(pc_link.messages("state")) == 2)
    assert pc_link.messages("state")[-1]["screen_orb"] == {"on": True, "place": "bottom centre", "size": "large"}

    # Only a PC with a press to listen for has an orb to show.
    bedroom, bedroom_pi = connect()
    make_session(bedroom).send_state()
    assert wait_for(lambda: bedroom_pi.messages("state"))
    assert "screen_orb" not in bedroom_pi.messages("state")[-1]


@pytest.mark.parametrize("written, sent", [
    # config.yaml takes a choice in any case, but the satellite takes only the page's own
    # words - and refuses the whole setting, its "on" with it, for one it doesn't know.
    ({"screen_orb_place": "Top Left"}, {"on": True, "place": "top left", "size": "medium"}),
    ({"screen_orb": False, "screen_orb_place": " Top Left "}, {"on": False, "place": "top left", "size": "medium"}),
    ({"screen_orb_size": "enormous"}, {"on": True, "place": "bottom centre", "size": "medium"}),
    # Left empty by hand, which is no answer, rather than off.
    ({"screen_orb": None}, {"on": True, "place": "bottom centre", "size": "medium"}),
])
def test_a_screen_orb_written_by_hand_is_sent_as_the_satellite_takes_it(brain, connect, make_session, written, sent):
    pc, pc_link = connect("pc", "pc.local")
    pc.push_to_talk = True
    session = make_session(pc)
    brain.config.apply({("computer", name): value for name, value in written.items()})
    session.send_state()
    assert wait_for(lambda: pc_link.messages("state"))
    assert pc_link.messages("state")[-1]["screen_orb"] == sent


def test_a_push_to_talk_satellite_is_sent_its_app(brain, connect, make_session):
    pc, pc_link = connect("pc", "pc.local")
    pc.push_to_talk = True
    session = make_session(pc)
    brain.config.apply({("wrapped", "link"): "https://nova.example", ("computer", "mic_mute_minutes"): 45})
    session.send_state()
    assert wait_for(lambda: pc_link.messages("state"))
    # The shortcut is the default until he saves another.
    assert pc_link.messages("state")[-1]["app"] == {"shortcut": "ctrl+alt+n", "page": "https://nova.example",
                                                    "mic_mute_minutes": 45}

    # Each read as it is sent, so a save reaches the PC with no restart.
    brain.rooms.set_shortcut("pc", "ctrl+shift+f5")
    session.send_state()
    assert wait_for(lambda: len(pc_link.messages("state")) == 2)
    assert pc_link.messages("state")[-1]["app"]["shortcut"] == "ctrl+shift+f5"
    # None at all is said as nothing, so the app can tell it from one it wasn't told.
    brain.rooms.set_shortcut("pc", "")
    brain.config.apply({("wrapped", "link"): ""})
    session.send_state()
    assert wait_for(lambda: len(pc_link.messages("state")) == 3)
    assert pc_link.messages("state")[-1]["app"] == {"shortcut": "", "page": "", "mic_mute_minutes": 45}


def test_a_wake_word_satellite_is_sent_no_app(brain, connect, make_session):
    bedroom, bedroom_pi = connect()
    make_session(bedroom).send_state()
    assert wait_for(lambda: bedroom_pi.messages("state"))
    assert "app" not in bedroom_pi.messages("state")[-1]


@pytest.mark.parametrize("written", [None, 0, -5, 2.5, True, "45", []])
def test_minutes_left_empty_in_the_settings_are_the_default(brain, connect, make_session, written):
    pc, pc_link = connect("pc", "pc.local")
    pc.push_to_talk = True
    session = make_session(pc)
    # What config.yaml would refuse, written by hand, or left empty.
    brain.config.apply({("computer", "mic_mute_minutes"): written})
    session.send_state()
    assert wait_for(lambda: pc_link.messages("state"))
    assert pc_link.messages("state")[-1]["app"]["mic_mute_minutes"] == 60


def test_a_new_session_does_not_inherit_a_taken_shortcut(connect, make_session):
    first, _ = connect("pc", "pc.local")
    session = make_session(first)
    assert session.shortcut_taken is False
    # What the satellite said - see RemoteSatellite - is what the session says, from the moment it does.
    first.shortcut_taken = True
    assert session.shortcut_taken is True
    # A reconnect is a new satellite, with a new session over it, which hasn't been told it again yet.
    again, _ = connect("pc", "pc.local")
    assert make_session(again).shortcut_taken is False


def test_mute_is_kept_per_satellite_and_sent_to_it(brain, connect, make_session):
    bedroom, bedroom_pi = connect("bedroom", "bedroom-pi")
    kitchen, kitchen_pi = connect("kitchen", "kitchen-pi")
    in_bedroom, in_kitchen = make_session(bedroom), make_session(kitchen)

    in_bedroom.set_muted(True)
    assert in_bedroom.muted and not in_kitchen.muted
    # Sent with the volumes, for the satellite to mute its wake word by.
    assert wait_for(lambda: bedroom_pi.messages("state"))
    assert bedroom_pi.messages("state")[-1]["muted"] is True
    assert bedroom_pi.messages("state")[-1]["volumes"] == {"general": 0.7, "alarm": 1.0, "music": 0.5}
    assert kitchen_pi.messages("state") == []

    # Home Assistant's switch is the satellite's own too.
    in_kitchen._device_controls().set_muted(True)
    assert in_kitchen.muted
    assert wait_for(lambda: kitchen_pi.messages("state") and kitchen_pi.messages("state")[-1]["muted"] is True)
    in_kitchen.set_muted(False)

    # Kept, so the bedroom is still muted when it connects again.
    again, _ = connect("bedroom", "bedroom-pi")
    assert make_session(again).muted
    assert not make_session(connect("kitchen", "kitchen-pi")[0]).muted


def test_volume_named_at_another_satellite_is_changed_and_beeped_there(brain, connect, make_session):
    pi, pc = make_session(connect("bedroom")[0]), make_session(connect("pc")[0])
    named = {"the PC": "pc", "the Mac": "mac"}
    asked_here = []

    def find_satellite(said, here=None):
        asked_here.append(here)
        if said == "this device":
            return here, None
        return (named[said], None) if said in named else (None, "Which one: the phone?")

    brain.find_satellite = find_satellite
    brain.session_for = {"bedroom": pi, "pc": pc}.get
    brain.not_connected = lambda name: "Your Mac's off."
    # Itself with none named; else the one named, or what to say.
    assert pi.actions.device(None) == (pi, None)
    assert pi.actions.device("the PC") == (pc, None)
    assert pi.actions.device("the Mac") == (None, "Your Mac's off.")
    assert pi.actions.device("the phone") == (None, "Which one: the phone?")
    # "This device" is the one spoken at, as the brain is told.
    assert pi.actions.device("this device") == (pi, None) and pc.actions.device("this device") == (pc, None)
    assert set(asked_here) == {"bedroom", "pc"}

    beeps = []
    pc.speaker = SimpleNamespace(play_file=lambda path, volume: beeps.append((Path(path).name, volume)),
                                 close=lambda: None)
    result = pi.actions.execute(Action("set_volume", {"level": 40, "where": "the PC"}))
    assert result.ok and result.say and result.confirm == "speak"
    assert pc.volume.percent("general") == 40 and pi.volume.percent("general") == 70
    assert beeps == [("volume_beep.wav", 0.4)]
    # A spoken mute is the music's, never the microphone's.
    pi.actions.execute(Action("set_volume", {"mute": True}))
    assert not pi.muted


# "Mute" means the microphone (20 Nov)

@pytest.fixture
def pi_and_pc(brain, connect, make_session):
    """
    The Pi ("bedroom", listening for its wake word) and the PC (push-to-talk), both connected, with the
    brain finding, naming and ringing on them as the real Brain does - the PC's room's always-on one
    being the Pi, the default.
    """
    pi, pc = make_session(connect("bedroom")[0]), make_session(connect("pc")[0])
    sessions = {"bedroom": pi, "pc": pc}
    _brain_choosing_notes(brain, sessions)
    brain.default_satellite = lambda: "bedroom"
    brain.ring_on = MethodType(Brain.ring_on, brain)
    return SimpleNamespace(pi=pi, pc=pc, sessions=sessions)


def test_mute_at_the_pi_mutes_the_pi(pi_and_pc):
    pi, pc = pi_and_pc.pi, pi_and_pc.pc
    result = pi.actions.execute(Action("mute_microphone", {"muted": True}))
    # Said with a sound, not words: the message is only logged.
    assert (result.ok, result.say, result.confirm, result.sound) == (True, "Microphone off.", "sound", "mic_off.wav")
    assert pi.muted and not pc.muted
    # The settings page's and Home Assistant's mute, so its switch shows it.
    assert pi._device_controls().muted()
    on = pi.actions.execute(Action("mute_microphone", {"muted": False}))
    assert (on.say, on.confirm, on.sound) == ("Microphone on.", "sound", "mic_on.wav")
    assert not pi.muted
    # Spoken words reach it the same way.
    pi.actions.execute(pi.intents.match("Stop listening.")["actions"][0])
    assert pi.muted


def test_mute_at_the_pc_mutes_the_rooms_always_on_pi(pi_and_pc, brain):
    pi, pc = pi_and_pc.pi, pi_and_pc.pc
    result = pc.actions.execute(Action("mute_microphone", {"muted": True}))
    assert (result.ok, result.say, result.confirm) == (True, "The Pi's microphone is off.", "sound")
    assert result.sound == "mic_off.wav" and pi.muted and not pc.muted
    # Back on from the PC, by name.
    result = pc.actions.execute(Action("mute_microphone", {"muted": False, "where": "the Pi"}))
    assert (result.ok, result.say, result.confirm) == (True, "The Pi's microphone is on.", "sound")
    assert result.sound == "mic_on.wav" and not pi.muted
    # A name he gave it is said as he wrote it, capitalised to start the sentence.
    brain.rooms.set_called("bedroom", ["bedroom Pi"])
    assert pc.actions.execute(Action("mute_microphone", {"muted": True})).say == "Bedroom Pi's microphone is off."
    assert pi.muted


def test_mute_the_pc_says_it_only_listens_on_the_key(pi_and_pc, brain):
    pi, pc = pi_and_pc.pi, pi_and_pc.pc
    for session in (pi, pc):
        result = session.actions.execute(Action("mute_microphone", {"muted": True, "where": "the PC"}))
        assert not result.ok
        assert result.say == "The PC only listens while you hold the key, so there's nothing to mute."
    assert not pi.muted and not pc.muted
    # And with no always-on satellite to stand in for it, the PC's own plain "mute" says the same.
    brain.default_satellite = lambda: "pc"
    result = pc.actions.execute(Action("mute_microphone", {"muted": True}))
    assert result.say == "The PC only listens while you hold the key, so there's nothing to mute."
    assert not pi.muted and not pc.muted


@pytest.fixture
def pi_ringing(brain, monkeypatch):
    """An alarm ringing on the Pi ("bedroom"), which the clock dismisses where it's asked to."""
    ringing = {"bedroom"}

    def dismiss(satellite=None):
        ringing.discard(satellite)
        return {"success": True, "message": "Okay."}

    monkeypatch.setattr(brain.clock, "is_ringing", lambda satellite=None: satellite in ringing)
    monkeypatch.setattr(brain.clock, "dismiss", dismiss)
    return ringing


def test_stop_listening_while_the_pi_rings_stops_it_first(pi_and_pc, pi_ringing):
    # Muted with the alarm still going, the Pi couldn't hear "Nova, stop", and it would climb to full volume.
    pi = pi_and_pc.pi
    result = pi.actions.execute(pi.intents.match("Stop listening.")["actions"][0])
    assert (result.ok, result.say, result.confirm) == (True, "Stopped, and microphone off.", "sound")
    assert result.sound == "mic_off.wav"
    assert pi_ringing == set() and pi.muted


def test_mute_at_the_pc_while_the_pi_rings_stops_it_first(pi_and_pc, pi_ringing, brain):
    pi, pc = pi_and_pc.pi, pi_and_pc.pc
    # "Stop listening": a plain "mute" at a PC which can reach Windows' sound asks which (20 Nov).
    result = pc.actions.execute(pc.intents.match("Stop listening.")["actions"][0])
    assert (result.ok, result.say, result.confirm) == (True, "Stopped, and the Pi's microphone is off.", "sound")
    assert result.sound == "mic_off.wav"
    assert pi_ringing == set() and pi.muted and not pc.muted
    # A name he gave it keeps its own case mid-sentence.
    pi.set_muted(False)
    pi_ringing.add("bedroom")
    brain.rooms.set_called("bedroom", ["Bedroom Pi"])
    assert pc.actions.execute(Action("mute_microphone", {"muted": True})).say == \
        "Stopped, and Bedroom Pi's microphone is off."


# The PC's own microphone in Windows (his rulings, 20 Nov)

@pytest.fixture
def windows_pc(pi_and_pc, monkeypatch):
    """The PC of pi_and_pc saying it can reach Windows' sound and microphone, which answer as Windows would."""
    pc = pi_and_pc.pc
    windows = SimpleNamespace(asked=[], mic_muted=False, muted=False)

    def ask_computer(action, **fields):
        windows.asked.append((action, fields))
        if action == "mic":
            windows.mic_muted = fields["mute"]
            return {"said": "Microphone off." if fields["mute"] else "Microphone on.", "muted": fields["mute"]}
        windows.muted = fields.get("mute", windows.muted)
        return {"level": 40, "muted": windows.muted}

    monkeypatch.setattr(pc.satellite, "system_volume", True)
    monkeypatch.setattr(pc.satellite, "ask_computer", ask_computer)
    # "The Pi" names a satellite, as the brain would say.
    for session in (pi_and_pc.pi, pc):
        monkeypatch.setattr(session.intents, "names_satellite", lambda said: said == "the pi")
    # Claude, if asked, says only this.
    monkeypatch.setattr(pc.llm, "process_query", lambda text, **kwargs: {"response": "Claude's answer.",
                                                                          "actions": [], "success": True})
    pi_and_pc.windows = windows
    return pi_and_pc


def test_mute_the_mic_at_the_pc_mutes_windows_microphone_with_a_sound(windows_pc):
    pi, pc, windows = windows_pc.pi, windows_pc.pc, windows_pc.windows
    reply = pc.conversation.handle("Mute the mic.")
    assert (reply.say, reply.sounds) == ("", [("mic_off.wav", "general")])
    assert windows.asked == [("mic", {"mute": True, "minutes": 60})] and windows.mic_muted
    # Nova's own listening, on the Pi and the PC, is left alone.
    assert not pi.muted and not pc.muted
    reply = pc.conversation.handle("Unmute the microphone.")
    assert (reply.say, reply.sounds) == ("", [("mic_on.wav", "general")])
    assert windows.asked[-1] == ("mic", {"mute": False}) and not windows.mic_muted


@pytest.mark.parametrize("answer", ["The mic.", "mic", "Microphone.", "the microphone please", "Nova, mute the mic."])
def test_plain_mute_at_the_pc_asks_and_the_mic_answer_mutes_windows_microphone(windows_pc, answer):
    pc, windows = windows_pc.pc, windows_pc.windows
    asked = pc.conversation.handle("Mute.")
    assert asked.say == "The mic or the audio?" and asked.listen_again
    assert windows.asked == []
    # The answer, heard without the wake word.
    reply = pc.conversation.handle(answer, follow_up=True)
    assert (reply.say, reply.sounds) == ("", [("mic_off.wav", "general")])
    assert windows.asked == [("mic", {"mute": True, "minutes": 60})]
    assert not windows_pc.pi.muted


@pytest.mark.parametrize("answer", ["The audio.", "sound", "Audio please.", "the sound"])
def test_plain_mute_at_the_pc_asks_and_the_audio_answer_mutes_windows_sound(windows_pc, answer):
    pc, windows = windows_pc.pc, windows_pc.windows
    pc.conversation.handle("Mute.")
    reply = pc.conversation.handle(answer, follow_up=True)
    # The satellite sounds it before Windows mutes, so the brain adds neither words nor a sound.
    assert (reply.say, reply.sounds) == ("", []) and windows.asked == [("volume", {"mute": True})]


def test_mute_the_sound_at_the_pc_leaves_the_sound_to_the_satellite_and_unmute_plays_mic_on(windows_pc):
    pc, windows = windows_pc.pc, windows_pc.windows
    reply = pc.conversation.handle("Mute the sound.")
    assert (reply.say, reply.sounds) == ("", []) and windows.asked == [("volume", {"mute": True})]
    reply = pc.conversation.handle("Unmute the audio.")
    assert (reply.say, reply.sounds) == ("", [("mic_on.wav", "general")])
    assert windows.asked[-1] == ("volume", {"mute": False})


def test_mute_the_sound_at_the_pi_sounds_as_the_microphone_does(windows_pc):
    pi = windows_pc.pi
    level = []
    pi.actions.music = SimpleNamespace(get_volume=lambda: 60, is_playing=lambda: False,
                                       set_volume=lambda value: level.append(value) or {"success": True, "message": "ok"})
    reply = pi.conversation.handle("Mute the sound.")
    assert reply.sounds == [("mic_off.wav", "general")] and reply.say == ""
    reply = pi.conversation.handle("Unmute the sound.")
    assert reply.sounds == [("mic_on.wav", "general")] and reply.say == ""


@pytest.mark.parametrize("said", ["Unmute everything.", "Unmute all.", "Unmute both."])
def test_unmute_everything_at_the_pc_unmutes_the_mic_and_the_sound_with_one_sound(windows_pc, said):
    pc, windows = windows_pc.pc, windows_pc.windows
    reply = pc.conversation.handle(said)
    assert windows.asked == [("mic", {"mute": False}), ("volume", {"mute": False})]
    assert (reply.say, reply.sounds) == ("", [("mic_on.wav", "general")])


def test_everything_answers_the_question_of_a_plain_unmute_but_not_a_mute(windows_pc):
    pc, windows = windows_pc.pc, windows_pc.windows
    assert pc.conversation.handle("Unmute.").say == "The mic or the audio?"
    reply = pc.conversation.handle("both", follow_up=True)
    assert windows.asked == [("mic", {"mute": False}), ("volume", {"mute": False})]
    assert (reply.say, reply.sounds) == ("", [("mic_on.wav", "general")])
    # No "mute everything": it isn't an answer to "Mute.".
    windows.asked.clear()
    assert pc.conversation.handle("Mute.").say == "The mic or the audio?"
    pc.conversation.handle("everything", follow_up=True)
    assert ("mic", {"mute": True, "minutes": 60}) not in windows.asked


def test_unmute_everything_says_which_half_failed(windows_pc, monkeypatch):
    pc = windows_pc.pc
    real = pc.satellite.ask_computer
    broken = {"mic"}
    monkeypatch.setattr(pc.satellite, "ask_computer",
                        lambda action, **fields: {"error": "No."} if action in broken else real(action, **fields))
    reply = pc.conversation.handle("Unmute everything.")
    assert reply.sounds == [] and "couldn't unmute the mic" in reply.say and "audio is on" in reply.say
    broken.clear()
    broken.add("volume")
    reply = pc.conversation.handle("Unmute everything.")
    assert reply.sounds == [] and "couldn't unmute the audio" in reply.say and "mic is on" in reply.say
    broken.add("mic")
    assert "mic or the audio" in pc.conversation.handle("Unmute everything.").say


def test_unmute_everything_is_not_a_phrase_away_from_the_pc(windows_pc):
    # At the Pi there's no Windows to unmute: it isn't taken for the local rule.
    actions = windows_pc.pi.intents.match("Unmute everything.")
    assert actions is None or all(action.tool != "unmute_everything" for action in actions["actions"])


def test_plain_unmute_at_the_pc_asks_too(windows_pc):
    pc, windows = windows_pc.pc, windows_pc.windows
    assert pc.conversation.handle("Unmute.").say == "The mic or the audio?"
    reply = pc.conversation.handle("the mic", follow_up=True)
    assert reply.sounds == [("mic_on.wav", "general")] and windows.asked == [("mic", {"mute": False})]
    assert pc.conversation.handle("Unmute.").say == "The mic or the audio?"
    reply = pc.conversation.handle("audio", follow_up=True)
    assert windows.asked[-1] == ("volume", {"mute": False})
    assert (reply.say, reply.sounds) == ("", [("mic_on.wav", "general")])


def test_anything_else_said_back_to_which_is_a_fresh_request(windows_pc):
    pc, windows = windows_pc.pc, windows_pc.windows
    pc.conversation.handle("Mute.")
    assert pc.conversation.handle("What's the capital of France?", follow_up=True).say == "Claude's answer."
    assert windows.asked == []
    # And it's gone: "the mic" now is no answer to anything.
    pc.conversation.handle("the mic", follow_up=True)
    assert windows.asked == []


def test_stop_listening_and_a_device_named_at_the_pc_are_unchanged(windows_pc):
    pi, pc, windows = windows_pc.pi, windows_pc.pc, windows_pc.windows
    reply = pc.conversation.handle("Stop listening.")
    assert reply.sounds == [("mic_off.wav", "general")] and pi.muted
    pc.conversation.handle("Unmute the Pi.")
    assert not pi.muted
    pc.conversation.handle("Mute the Pi.")
    assert pi.muted
    assert windows.asked == []


def test_plain_mute_at_the_pc_stops_the_pi_ringing_in_its_room_rather_than_asking(windows_pc, brain, monkeypatch):
    pi, pc, windows = windows_pc.pi, windows_pc.pc, windows_pc.windows
    ringing = {"bedroom"}
    monkeypatch.setattr(brain, "room_of", lambda name: [name] + [other for other in ("pc", "bedroom") if other != name])
    monkeypatch.setattr(brain.clock, "is_ringing", lambda satellite=None: bool(ringing & set(
        satellite if isinstance(satellite, list) else [satellite])))

    def dismiss(satellite=None):
        ringing.clear()
        return {"success": True, "message": "Okay."}

    monkeypatch.setattr(brain.clock, "dismiss", dismiss)
    reply = pc.conversation.handle("Mute.")
    assert reply.say != "The mic or the audio?" and not reply.listen_again
    assert ringing == set() and windows.asked == [] and not pi.muted


def test_plain_mute_at_the_pi_still_mutes_the_pi(windows_pc):
    pi, windows = windows_pc.pi, windows_pc.windows
    reply = pi.conversation.handle("Mute.")
    assert reply.sounds == [("mic_off.wav", "general")] and pi.muted and windows.asked == []


def test_an_older_pc_satellite_which_doesnt_know_mic_says_so(windows_pc, monkeypatch):
    pc = windows_pc.pc
    monkeypatch.setattr(pc.satellite, "ask_computer", lambda action, **fields: {
        "error": "I can't do that on this computer."})
    assert pc.conversation.handle("Mute the mic.").say == "I can't do that on this computer."


def test_unmute_leaves_a_ring_alone(pi_and_pc, pi_ringing):
    pi_and_pc.pi.set_muted(True)
    result = pi_and_pc.pc.actions.execute(Action("mute_microphone", {"muted": False, "where": "the Pi"}))
    assert (result.say, result.sound) == ("The Pi's microphone is on.", "mic_on.wav") and pi_ringing == {"bedroom"}


def test_mute_a_satellite_not_connected_says_so(pi_and_pc, brain):
    pi, pc = pi_and_pc.pi, pi_and_pc.pc
    brain.session_for = {"pc": pc}.get
    result = pc.actions.execute(Action("mute_microphone", {"muted": True}))
    assert (result.ok, result.say) == (False, "Nova isn't running on the Pi.")
    result = pc.actions.execute(Action("mute_microphone", {"muted": False, "where": "the Pi"}))
    assert (result.ok, result.say) == (False, "Nova isn't running on the Pi.")
    # Words naming no satellite say what there is.
    result = pc.actions.execute(Action("mute_microphone", {"muted": True, "where": "the toaster"}))
    assert not result.ok and result.say.startswith("There's no satellite called the toaster.")
    assert not pi.muted


# Muting during a conversation

@pytest.mark.parametrize("how", ["switch", "voice", "from the pc"])
def test_muting_ends_the_conversation_at_the_pi(pi_and_pc, brain, how):
    # Nova's own mute, however it's done - Home Assistant's switch and the settings page, "stop listening"
    # there, or the Pi named from the PC - ends one there, as it was his doing.
    pi, pc = pi_and_pc.pi, pi_and_pc.pc
    brain.open_conversation = OpenConversation(lambda: {"enabled": True})
    brain.open_conversation.start("bedroom")
    if how == "switch":
        pi._device_controls().set_muted(True)
    elif how == "voice":
        pi.actions.execute(Action("mute_microphone", {"muted": True}))
    else:
        pc.actions.execute(Action("mute_microphone", {"muted": True, "where": "the Pi"}))
    assert pi.muted and brain.open_conversation.satellite is None and pi._ended_by == "muted"
    # Unmuting starts nothing, and a mute where none is on ends nothing elsewhere.
    pi.set_muted(False)
    brain.open_conversation.start("pc")
    pi.set_muted(True)
    assert brain.open_conversation.on_at("pc")


def test_muting_ends_the_conversation_at_the_pc(windows_pc, brain):
    # At a computer it's Windows' own microphone which hears him.
    pc, windows = windows_pc.pc, windows_pc.windows
    brain.open_conversation = OpenConversation(lambda: {"enabled": True})
    brain.open_conversation.start("pc")
    pc.conversation.handle("Mute the mic.")
    assert windows.mic_muted and brain.open_conversation.satellite is None and pc._ended_by == "muted"
    # Unmuting it ends nothing.
    brain.open_conversation.start("pc")
    pc.conversation.handle("Unmute the microphone.")
    assert not windows.mic_muted and brain.open_conversation.on_at("pc")


def test_a_muted_pi_refuses_a_conversation_and_a_muted_pc_doesnt(brain, connect, make_session, monkeypatch):
    # Only typing in Home Assistant can ask the Pi for one while it's muted, and nothing would hear him. The
    # PC unmutes Windows' microphone for one itself, as it does for a press.
    brain.open_conversation = OpenConversation(lambda: {"enabled": True})
    pi, pc = make_session(connect("bedroom")[0]), make_session(connect("pc")[0])
    monkeypatch.setattr(pc.satellite, "push_to_talk", True)
    pi.set_muted(True)
    assert pi.conversation.handle("Let's talk.").say == "The microphone's muted."
    assert brain.open_conversation.satellite is None
    pc.set_muted(True)
    assert pc.conversation.handle("Let's talk.").say == "Go ahead."
    assert brain.open_conversation.on_at("pc")


def test_muting_a_running_conversation_ends_it_with_the_falling_sound_and_nothing_said(brain, connect,
                                                                                       make_session):
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Okay."))
    brain.stt.texts = ["Hello."]
    _sending_quiet(pi)
    running(session)

    pi.wake(said())
    assert wait_for(lambda: pi.messages("listen"))
    session.set_muted(True)
    assert wait_for(lambda: session._idle.is_set())
    recording = pi.messages("listen")[0]["recording"]
    assert any(message.get("recording") == recording for message in pi.messages("stop_listening"))
    assert brain.tts.said == ["Okay."] and _sounds(pi)[-1] == "not_heard.wav"
    assert [message["on"] for message in pi.messages("conversation")] == [True, False]


def test_volumes_are_kept_per_satellite_and_sent_to_it(brain, connect, make_session):
    bedroom, bedroom_pi = connect("bedroom", "bedroom-pi")
    kitchen, kitchen_pi = connect("kitchen", "kitchen-pi")
    in_bedroom, in_kitchen = make_session(bedroom), make_session(kitchen)

    # Changed by voice, and from Home Assistant.
    in_kitchen.actions.execute(Action("set_volume", {"category": "alarm", "level": 30}))
    in_kitchen._device_controls().set_volume("general", 40)

    assert (in_kitchen.volume.percent("general"), in_kitchen.volume.percent("alarm")) == (40, 30)
    assert (in_bedroom.volume.percent("general"), in_bedroom.volume.percent("alarm")) == (70, 100)
    assert wait_for(lambda: len(kitchen_pi.messages("state")) == 2)
    assert kitchen_pi.messages("state")[-1]["volumes"] == {"general": 0.4, "alarm": 0.3, "music": 0.5}
    assert bedroom_pi.messages("state") == []
    assert make_session(connect("kitchen", "kitchen-pi")[0]).volume.percent("alarm") == 30
    assert os.path.exists(os.path.join(paths.DATA_DIR, "volume-kitchen.json"))


def test_only_whats_ringing_on_a_satellite_is_heard_or_stopped_there(brain, connect, make_session):
    # With a second satellite, saying "stop" in the kitchen dismissed the bedroom's
    # timer, which nobody there could hear - as did asking it anything at all, and its
    # Stop in Home Assistant.
    bedroom, _ = connect("bedroom", "bedroom-pi")
    kitchen, kitchen_pi = connect("kitchen", "kitchen-pi")
    make_session(bedroom)
    in_kitchen = make_session(kitchen)
    brain.clock.set_timer(60, satellite="bedroom")
    brain.clock._tick(time.time() + 61)
    running(in_kitchen)

    # The bedroom's timer is the only thing ringing, so still ringing means it is.
    brain.stt.texts = ["Stop.", "What time is it?"]
    kitchen_pi.wake(said())
    assert wait_for(lambda: len(kitchen_pi.messages("allow")) == 1)
    assert brain.clock.is_ringing()
    kitchen_pi.wake(said())
    assert wait_for(lambda: len(kitchen_pi.messages("allow")) == 2)
    assert brain.clock.is_ringing()
    in_kitchen._device_controls().stop()
    assert brain.clock.is_ringing()


def test_the_device_is_named_from_the_satellites_hostname(brain, connect, make_session, monkeypatch):
    # The brain runs it now, and the Pi's device - and with it the room Home Assistant
    # has it in - has to stay the one it always was.
    monkeypatch.setattr(ha_device.socket, "gethostname", lambda: "nova")
    brain.config.data["mqtt"] = {"host": "192.168.1.20", "port": 1883, "username": "nova", "name": "Nova"}
    satellite, _ = connect("bedroom", "home-assistant.local")
    assert make_session(satellite).device.identifier == "nova_home_assistant"


# Things which go off by themselves

def test_an_alert_is_said_like_a_timer_and_can_then_be_sent_on(make_session):
    from voice_assistant.ntfy import Alert

    session = make_session()
    announced = []
    session.announce = lambda message, **kwargs: announced.append(message)
    session.alert(Alert("homelab", "Backup failed", "Error 5."))

    assert announced == ["Homelab: Backup failed"]
    assert session.actions.last_answer == ("Backup failed", "Backup failed. Error 5.")


def test_a_routine_run_by_itself_waits_its_turn_and_is_said_as_an_announcement(brain, make_session):
    # So the wake word stops it, as it does a timer's.
    session = make_session()
    played, sounds, happened = [], [], []
    session.music = SimpleNamespace(hold=lambda: happened.append("held"),
                                    release=lambda after=0: happened.append("let go"))
    session.conversation = SimpleNamespace(
        start=lambda: happened.append("started"),
        run_routine=lambda routine: Reply(say="It's sunny.", sounds=[("success_chime.wav", "general")]))
    session._play_sound = lambda sound, volume=None: sounds.append(sound)
    session._play_announcement = lambda playback, still_wanted=None: None
    session.speaker = SimpleNamespace(play_stream=lambda stream, rate, volume, kind: played.append(kind),
                                      close=lambda: None)
    brain.tts = SimpleNamespace(stream=lambda message: played.append(message), sample_rate=22050)

    session.run_routine({"name": "Good morning", "steps": ["what's the weather"]})

    assert sounds == ["success_chime.wav"] and played == ["It's sunny.", "announcement"]
    assert happened == ["held", "started", "let go"] and session.activity == "idle"


# For the settings page

def test_a_volume_beep_from_the_settings_page_stops_the_last_one(make_session):
    # Dragging a slider plays one at every step, which mustn't pile up.
    class Beep:
        def __init__(self, level):
            self.level, self.stopped = level, False

        def stop(self):
            self.stopped = True

    session = make_session()
    played = []
    session.speaker = SimpleNamespace(play_file=lambda path, volume: played.append(Beep(volume)) or played[-1],
                                      close=lambda: None)

    session.boop(0.3)
    session.boop(0.6)

    assert [beep.level for beep in played] == [0.3, 0.6]
    assert played[0].stopped and not played[1].stopped


def test_hear_it_is_said_at_the_speed_given_unless_nova_is_in_a_conversation(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    assert session.hear_voice(1.25)
    assert wait_for(lambda: pi.messages("stream_end"))
    assert brain.tts.said == [session_module.HEAR_IT] and brain.tts.speeds == [1.25]
    assert [message["kind"] for message in pi.messages("play_stream")] == ["reply"]
    # Nothing is listened for after it, as it isn't a question.
    assert not pi.messages("listen")

    session._idle.clear()
    assert not session.hear_voice(1.0)
    assert brain.tts.said == [session_module.HEAR_IT]


def test_hear_it_isnt_said_over_an_announcement(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    # Held playing, as a satellite part way through it would be.
    pi.held = {1}
    threading.Thread(target=session.announce, args=("Your phone's battery is down to 5%.",), daemon=True).start()
    assert wait_for(lambda: pi.messages("stream_end"))
    assert not session.hear_voice(1.25)
    assert session_module.HEAR_IT not in brain.tts.said


def test_hear_it_isnt_said_over_a_routine(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = SimpleNamespace(start=lambda: None, run_routine=lambda routine: Reply(say="It's sunny."))
    pi.held = {1}
    threading.Thread(target=session.run_routine, args=({"name": "Good morning", "steps": []},), daemon=True).start()
    assert wait_for(lambda: pi.messages("stream_end"))
    assert not session.hear_voice(1.25)
    assert session_module.HEAR_IT not in brain.tts.said


def test_hear_it_isnt_said_over_anything_ringing(brain, connect, make_session, monkeypatch):
    # Least of all an alarm, between its bursts as much as during them.
    satellite, pi = connect()
    session = make_session(satellite)
    ringing = ["kitchen"]
    monkeypatch.setattr(brain.clock, "is_ringing", lambda satellite=None: satellite in ringing)
    assert session.hear_voice(1.25)
    assert wait_for(lambda: pi.messages("stream_end"))

    ringing.append("bedroom")
    assert not session.hear_voice(1.25)
    assert brain.tts.said == [session_module.HEAR_IT]


def test_hear_it_isnt_said_over_the_last_one(brain, connect, make_session):
    # Pressed again while it's still being said.
    satellite, pi = connect()
    session = make_session(satellite)
    pi.held = {1}
    assert session.hear_voice(1.25)
    assert wait_for(lambda: pi.messages("stream_end"))
    assert not session.hear_voice(1.0)

    pi.say("played", id=pi.messages("play_stream")[0]["id"])
    assert wait_for(lambda: session._last_hearing.done)
    assert session.hear_voice(1.0)
    assert brain.tts.speeds == [1.25, 1.0]


def test_what_nova_is_doing_is_kept_for_the_settings_page(brain, make_session, monkeypatch):
    session = make_session()
    seen = {}

    class Watching(FakeTranscription):
        def finish(self, audio, **how):
            seen["working out the words"] = session.activity
            return super().finish(audio, **how)

    def record_until_silence(source, detector, heard=None, **kwargs):
        seen["recording"] = session.activity
        return np.zeros(480, np.float32)

    monkeypatch.setattr(session_module, "record_until_silence", record_until_silence)
    brain.stt = SimpleNamespace(listen=lambda: Watching("What time is it?"))
    session.conversation = ScriptedConversation(Reply())
    session._handle_interaction(AudioFeed(), timeline=FakeTimeline())

    assert seen == {"recording": "listening", "working out the words": "thinking"}


def test_a_session_started_before_the_save_uses_the_new_value(brain, connect, make_session, monkeypatch, tmp_path):
    # The silence setting is tuned by trying, so a save on the settings page reaches every
    # satellite already connected, at its next recording - not a copy kept from its start.
    brain.config = Config(path=str(tmp_path / "config.yaml"))
    sessions = [make_session(connect("bedroom")[0]), make_session(connect("kitchen", "kitchen-pi.local")[0])]
    durations = []

    def record_until_silence(source, detector, heard=None, **kwargs):
        durations.append(kwargs["silence_duration"])
        return np.zeros(480, np.float32)

    monkeypatch.setattr(session_module, "record_until_silence", record_until_silence)
    Brain.apply_settings(brain, {("thresholds", "silence_duration"): 1.2})
    for session in sessions:
        session.conversation = ScriptedConversation(Reply())
        session._handle_interaction(AudioFeed(), timeline=FakeTimeline())

    assert durations == [1.2, 1.2]


class QuietWordsTranscription(FakeTranscription):
    """As FakeTranscription, for the real Nemotron's listener, which can say how long its words have been still."""

    def quiet_words(self):
        return None


def _recordings(monkeypatch, ended_by="silence", speech=None, **report):
    """
    Replaces record_until_silence with a spy keeping what each recording was asked, and ending as given -
    with the user having spoken for this many seconds, if it says, and the rest of the report as given.
    """
    asked = []

    def record_until_silence(source, detector, heard=None, **kwargs):
        asked.append(kwargs)
        kwargs["report"].update(ended_by=ended_by, **report)
        if speech is not None:
            kwargs["timeline"].marks.update(speech_started=1200, speech_ended=1200 + round(speech * 1000))
        return np.zeros(480, np.float32)

    monkeypatch.setattr(session_module, "record_until_silence", record_until_silence)
    return asked


def _record(session, brain, listener, feed=None):
    brain.stt = SimpleNamespace(listen=lambda: listener)
    session.conversation = ScriptedConversation(Reply())
    timeline = FakeTimeline()
    session._handle_interaction(AudioFeed() if feed is None else feed, timeline=timeline)
    return timeline


def test_a_push_to_talk_satellite_gets_both_rules(brain, connect, make_session, monkeypatch):
    # The PC's microphone is close, so his voice is far louder than a game nearby and the
    # words stopping are a better sign he has finished than the quiet - see recording.py.
    asked = _recordings(monkeypatch)
    satellite, _ = connect(name="pc", hostname="pc.local")
    satellite.push_to_talk = True
    listener = QuietWordsTranscription("What time is it?")
    _record(make_session(satellite), brain, listener)

    (kwargs,) = asked
    assert kwargs["voice_drop"] == pytest.approx(0.126, abs=0.001)
    assert kwargs["words_quiet"] == listener.quiet_words
    assert kwargs["no_words_after"] == 1.5
    assert kwargs["report"] == {"ended_by": "silence"}


def test_a_saved_setting_is_used_by_the_next_recording(brain, connect, make_session, monkeypatch, tmp_path):
    brain.config = Config(path=str(tmp_path / "config.yaml"))
    asked = _recordings(monkeypatch)
    satellite, _ = connect(name="pc", hostname="pc.local")
    satellite.push_to_talk = True
    session = make_session(satellite)
    _record(session, brain, QuietWordsTranscription())
    Brain.apply_settings(brain, {("thresholds", "press_voice_drop_db"): 12,
                                 ("thresholds", "press_no_words_seconds"): 2.5})
    _record(session, brain, QuietWordsTranscription())

    # What config.yaml ships, then what was saved - neither a copy kept from the session's start.
    assert [kwargs["voice_drop"] for kwargs in asked] == [pytest.approx(0.126, abs=0.001),
                                                         pytest.approx(0.251, abs=0.001)]
    assert [kwargs["no_words_after"] for kwargs in asked] == [1.5, 2.5]


def test_a_wake_word_satellite_gets_neither_rule(brain, make_session, monkeypatch):
    asked = _recordings(monkeypatch)
    _record(make_session(), brain, QuietWordsTranscription("What time is it?"))

    (kwargs,) = asked
    assert kwargs["voice_drop"] is None
    assert kwargs["words_quiet"] is None


def test_a_listener_without_quiet_words_gets_only_the_voice_drop(brain, connect, make_session, monkeypatch):
    # A listener which can't say how long its words have been still, as the stand-ins
    # for it here can't - the recording then needs no word before the voice drop counts.
    asked = _recordings(monkeypatch)
    satellite, _ = connect(name="pc", hostname="pc.local")
    satellite.push_to_talk = True
    _record(make_session(satellite), brain, FakeTranscription("What time is it?"))

    (kwargs,) = asked
    assert kwargs["voice_drop"] == pytest.approx(0.126, abs=0.001)
    assert kwargs["words_quiet"] is None


def test_the_timeline_notes_what_ended_it(brain, connect, make_session, monkeypatch):
    # Whichever kind of satellite it was, so latency.jsonl can be read for what ended each recording.
    _recordings(monkeypatch, ended_by="voice drop")
    satellite, _ = connect(name="pc", hostname="pc.local")
    satellite.push_to_talk = True

    assert _record(make_session(satellite), brain, QuietWordsTranscription()).notes["ended_by"] == "voice drop"
    assert _record(make_session(), brain, QuietWordsTranscription()).notes["ended_by"] == "voice drop"


def test_a_recording_ended_on_a_press_is_noted_as_pressed(brain, connect, make_session, monkeypatch):
    # The recorder only knows the feed ended, but the satellite says whether that was his key.
    _recordings(monkeypatch, ended_by="feed ended")
    satellite, _ = connect(name="pc", hostname="pc.local")
    satellite.push_to_talk = True
    session = make_session(satellite)
    pressed, let_go = AudioFeed(), AudioFeed()
    pressed.end(pressed=True)
    let_go.end()

    assert _record(session, brain, QuietWordsTranscription(), feed=pressed).notes["ended_by"] == "pressed"
    # A feed which ended without a press - the satellite going, say - isn't one.
    assert _record(session, brain, QuietWordsTranscription(), feed=let_go).notes["ended_by"] == "feed ended"


def test_only_a_feed_ended_is_changed_to_pressed(brain, connect, make_session, monkeypatch):
    # A recording the quiet or the limit ended had finished before the press, whatever the feed did after.
    _recordings(monkeypatch, ended_by="silence")
    satellite, _ = connect(name="pc", hostname="pc.local")
    satellite.push_to_talk = True
    feed = AudioFeed()
    feed.end(pressed=True)

    assert _record(make_session(satellite), brain, QuietWordsTranscription(), feed=feed).notes["ended_by"] == "silence"


# The last 50 long recordings, kept for seeing whether the ends work - see long_recordings.py

def _long_recordings(tmp_path):
    path = tmp_path / "long-recordings.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_long_push_to_talk_recording_is_noted_with_its_words(brain, connect, make_session, monkeypatch, tmp_path):
    _recordings(monkeypatch, ended_by="no new words", speech=6.5, loudness=0.3, levels=[0.3, 0.2])
    monkeypatch.setattr(session_module, "transcripts_logged", lambda: True)
    satellite, _ = connect(name="pc", hostname="pc.local")
    satellite.push_to_talk = True
    before = datetime.now().replace(microsecond=0)
    _record(make_session(satellite), brain, QuietWordsTranscription("What time is it?"))

    (line,) = _long_recordings(tmp_path)
    # When it was heard, then the numbers the recorder saw and the words.
    assert before <= datetime.fromisoformat(line.pop("at")) <= datetime.now()
    assert line == {"speech_seconds": 6.5, "ended_by": "no new words", "loudness": 0.3, "levels": [0.3, 0.2],
                    "words": "What time is it?"}


def test_a_long_recording_is_noted_without_its_words_while_transcripts_are_off(brain, connect, make_session,
                                                                              monkeypatch, tmp_path):
    _recordings(monkeypatch, speech=6.5)
    monkeypatch.setattr(session_module, "transcripts_logged", lambda: False)
    satellite, _ = connect(name="pc", hostname="pc.local")
    satellite.push_to_talk = True
    _record(make_session(satellite), brain, QuietWordsTranscription("What time is it?"))

    (line,) = _long_recordings(tmp_path)
    assert line["speech_seconds"] == 6.5
    assert "words" not in line
    assert "What time is it?" not in (tmp_path / "long-recordings.jsonl").read_text()


def test_a_long_recording_with_no_words_heard_is_still_noted(brain, connect, make_session, monkeypatch, tmp_path):
    # Loud sound which wasn't speech is the case the notes are partly for.
    _recordings(monkeypatch, ended_by="limit", speech=9.0)
    monkeypatch.setattr(session_module, "transcripts_logged", lambda: True)
    satellite, _ = connect(name="pc", hostname="pc.local")
    satellite.push_to_talk = True
    _record(make_session(satellite), brain, QuietWordsTranscription("", imagined=True))

    (line,) = _long_recordings(tmp_path)
    assert line["ended_by"] == "limit" and line["words"] == ""


def test_a_long_recording_speech_to_text_failed_on_says_so(brain, connect, make_session, monkeypatch, tmp_path):
    # Otherwise it would look, in the note, like loud sound in which no word was heard.
    _recordings(monkeypatch, ended_by="limit", speech=9.0)
    monkeypatch.setattr(session_module, "transcripts_logged", lambda: True)
    satellite, _ = connect(name="pc", hostname="pc.local")
    satellite.push_to_talk = True
    failed = QuietWordsTranscription()
    failed.finish = lambda audio, **how: {"text": "", "error": "Failed to decode"}
    _record(make_session(satellite), brain, failed)

    (line,) = _long_recordings(tmp_path)
    assert line["stt_error"] is True and line["words"] == ""


def test_a_short_push_to_talk_recording_is_not_noted(brain, connect, make_session, monkeypatch, tmp_path):
    _recordings(monkeypatch, speech=4.0)
    satellite, _ = connect(name="pc", hostname="pc.local")
    satellite.push_to_talk = True
    _record(make_session(satellite), brain, QuietWordsTranscription("What time is it?"))

    assert _long_recordings(tmp_path) == []


def test_a_recording_with_no_speech_marks_is_not_noted(brain, connect, make_session, monkeypatch, tmp_path):
    # Speech which never started has no length to be long.
    _recordings(monkeypatch)
    satellite, _ = connect(name="pc", hostname="pc.local")
    satellite.push_to_talk = True
    _record(make_session(satellite), brain, QuietWordsTranscription("What time is it?"))

    assert _long_recordings(tmp_path) == []


def test_a_long_wake_word_recording_is_not_noted(brain, make_session, monkeypatch, tmp_path):
    # The Pi's recordings behave as they always have.
    _recordings(monkeypatch, speech=9.0)
    _record(make_session(), brain, QuietWordsTranscription("What time is it?"))

    assert _long_recordings(tmp_path) == []


def test_a_recording_ended_on_a_press_is_noted_as_pressed_in_the_long_ones(brain, connect, make_session,
                                                                         monkeypatch, tmp_path):
    _recordings(monkeypatch, ended_by="feed ended", speech=8.0)
    satellite, _ = connect(name="pc", hostname="pc.local")
    satellite.push_to_talk = True
    feed = AudioFeed()
    feed.end(pressed=True)
    _record(make_session(satellite), brain, QuietWordsTranscription("What time is it?"), feed=feed)

    (line,) = _long_recordings(tmp_path)
    assert line["ended_by"] == "pressed"


def test_a_note_which_cannot_be_kept_does_not_stop_the_reply(brain, connect, make_session, monkeypatch, tmp_path):
    _recordings(monkeypatch, speech=8.0)
    satellite, _ = connect(name="pc", hostname="pc.local")
    satellite.push_to_talk = True
    session = make_session(satellite)
    session.long_recordings_file = str(tmp_path / "missing" / "long-recordings.jsonl")
    _record(session, brain, QuietWordsTranscription("What time is it?"))

    assert session.conversation.heard == [("What time is it?", False)]


# For a clock display - see docs/decisions/0013

def test_a_hold_starts_a_conversation_like_a_wake(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="Okay."))
    brain.stt.texts = ["What time is it?"]
    pi.answers = [said(before=0)]
    heard = []
    session.watch(lambda: heard.append(session.heard))
    thread = running(session)

    assert session.listen_now()
    assert wait_for(lambda: pi.messages("allow"))
    assert len(pi.messages("listen")) == 1
    # Heard and answered as a fresh request, not as the answer to a question.
    assert session.conversation.heard == [("What time is it?", False)]
    assert session.heard == "What time is it?" and "What time is it?" in heard
    assert brain.tts.said == ["Okay."]

    # Over once its conversation is, so the next hold starts another without the first
    # being let go of - and letting go of that one ends only its own recording.
    assert wait_for(lambda: session._idle.is_set())
    assert session.listen_now()
    assert wait_for(lambda: len(pi.messages("listen")) == 2)
    session.stop_held()
    assert wait_for(lambda: len(pi.messages("stop_listening")) == 2)
    assert pi.messages("stop_listening")[1]["recording"] == pi.messages("listen")[1]["recording"]
    pi.go()
    thread.join(5)


def test_a_hold_taken_up_part_way_through_a_conversation_is_over_with_it(make_session):
    # Held just as a wake word started a conversation, the hold's wake waits in the queue
    # and is answered there as talked over - so it isn't what the conversation started
    # from. Kept open after it, every later hold would be refused until a lift which may
    # never come.
    session = make_session()
    session.conversation = ScriptedConversation()
    assert session.listen_now()
    held = session.satellite.wakes.get_nowait()
    after = [dict(held, talked_over=True), None]

    def handle(feed, timeline, pre_roll=None, pressed=False):
        # As the end of each recording does - see Session._handle_interaction.
        session.satellite.stop_listening(feed)
        return after.pop(0)

    session._handle_interaction = handle
    session._converse({"talked_over": False, "feed": AudioFeed()})
    assert after == []

    assert session.listen_now()


def test_a_hold_works_while_muted(connect, make_session):
    # Muting stops the wake word, not a finger held on the clock on purpose - "when nova
    # muted, the mic should still work".
    satellite, pi = connect()
    session = make_session(satellite)
    session.set_muted(True)

    assert session.listen_now()
    assert wait_for(lambda: pi.messages("listen"))
    assert not satellite.wakes.empty()


def test_no_second_hold_during_a_conversation(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    # A second hold before the first has been answered isn't another.
    assert session.listen_now()
    assert not session.listen_now()
    assert satellite.wakes.qsize() == 1
    session.stop_held()
    satellite.wakes.get_nowait()

    session.conversation = ScriptedConversation(Reply(say="Today, you've got..."))
    brain.stt.texts = ["What's on today?"]
    # The reply plays on, so the conversation is still going.
    pi.held = {1}
    thread = running(session)
    pi.wake(said())
    assert wait_for(lambda: pi.messages("play_stream"))

    assert not session.listen_now()
    # Letting go of a hold which never was does nothing to the conversation.
    session.stop_held()
    time.sleep(0.1)
    assert len(pi.messages("listen")) == 1 and satellite.wakes.empty()
    assert len(pi.messages("stop_listening")) == 2 and session.activity == "speaking"
    pi.go()
    thread.join(5)


def test_letting_go_ends_the_recording(connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    # Without a hold, letting go does nothing.
    session.stop_held()

    assert session.listen_now()
    feed = satellite.wakes.get_nowait()["feed"]
    session.stop_held()
    session.stop_held()
    assert wait_for(lambda: pi.messages("stop_listening"))
    time.sleep(0.1)
    # Only the once, for that recording - which ends what's recorded of it.
    assert len(pi.messages("stop_listening")) == 1
    assert pi.messages("stop_listening")[0]["recording"] == pi.messages("listen")[0]["recording"]
    assert len(feed.listen(0).read(RATE)) == 0


def test_a_hold_on_a_satellite_which_has_gone_is_refused(connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    pi.go()
    assert wait_for(lambda: not satellite.connected)

    assert not session.listen_now()
    assert satellite.wakes.empty()


def test_watchers_hear_a_mute_change(connect, make_session):
    satellite, _ = connect()
    session = make_session(satellite)
    seen = []
    session.watch(lambda: seen.append(session.muted))
    session.set_muted(True)
    session.set_muted(True)
    session.set_muted(False)
    # Told only of a change, after it's made - so a clock's muted mark comes and goes at once.
    assert seen == [True, False]


def test_watchers_hear_each_change(brain, connect, make_session, caplog):
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="Okay."))
    brain.stt.texts = ["Turn the lights on."]
    seen = []

    def watcher():
        seen.append(session.activity)

    def broken():
        raise RuntimeError("The display has gone")

    session.watch(watcher)
    session.watch(broken)
    thread = running(session)
    pi.wake(said())
    assert wait_for(lambda: pi.messages("allow") and session._idle.is_set())

    # One which fails is logged, and the conversation carries on regardless.
    assert brain.tts.said == ["Okay."]
    assert "listening" in seen and seen[-1] == "idle"
    assert seen.index("listening") < len(seen) - 1
    assert any("The display has gone" in record.getMessage() for record in caplog.records)

    # Told only of a change, and not once it's stopped watching.
    told = len(seen)
    session.activity = "idle"
    assert len(seen) == told
    session.unwatch(watcher)
    session.unwatch(broken)
    session.activity = "thinking"
    assert len(seen) == told
    pi.go()
    thread.join(5)


# Asked by typing, in Home Assistant - see docs/decisions/0027

def _waiting_for_a_typed_answer(session):
    """Whether a typed question is left open for its answer, with its turn over - so nothing waits on it."""
    return session._typed_question is not None and session._idle.is_set() and session.activity == "idle"


def test_a_typed_request_is_answered_without_the_microphone(brain, connect, make_session, caplog):
    caplog.set_level(logging.DEBUG)
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="It's ten o'clock."))
    seen = []
    session.watch(lambda: seen.append(session.activity))
    thread = running(session)

    session.ask("What time is it?")
    assert wait_for(lambda: pi.messages("allow") and session._idle.is_set())

    # Heard as speech is, after speech-to-text, and said out loud - with no listening
    # sound, and the satellite never asked to listen.
    assert session.conversation.heard == [("What time is it?", False)]
    assert session.heard == "What time is it?"
    assert brain.tts.said == ["It's ten o'clock."] and len(pi.messages("play_stream")) == 1
    assert pi.messages("listen") == [] and pi.messages("play_sound") == [] and brain.stt.made == []
    # Thinking from the start, as there's nothing to listen to.
    assert seen[0] == "thinking" and "listening" not in seen
    # What was typed is logged only as what's heard is, so the transcripts setting decides.
    typed = [record for record in caplog.records if "What time is it?" in record.getMessage()]
    assert all(getattr(record, "transcript", False) for record in typed)
    pi.go()
    thread.join(5)


def test_a_typed_request_is_cut_to_the_longest_and_a_blank_one_dropped(make_session):
    session = make_session()
    session.ask("   ")
    session.ask("")
    session.ask("  " + "a" * (session_module.ASK_MOST + 20) + "  ")
    assert session._typed.get_nowait()[0] == "a" * session_module.ASK_MOST
    assert session._typed.empty()


def test_a_typed_request_waits_for_the_turn_in_progress(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="Today, you've got..."), Reply(say="Okay."))
    brain.stt.texts = ["What's on today?"]
    # The first reply plays on until the test lets it finish.
    pi.held = {1}
    thread = running(session)

    pi.wake(said())
    assert wait_for(lambda: pi.messages("play_stream"))
    session.ask("Next one.")
    time.sleep(3 * session_module.WAKE_CHECK)
    # Not answered over the reply, nor does it stop it.
    held = pi.messages("play_stream")[0]["id"]
    assert session.conversation.heard == [("What's on today?", False)]
    assert not any(message.get("id") == held for message in pi.messages("stop"))

    pi.say("played", id=held)
    assert wait_for(lambda: len(session.conversation.heard) == 2 and session._idle.is_set())
    assert session.conversation.heard == [("What's on today?", False), ("Next one.", False)]
    assert brain.tts.said == ["Today, you've got...", "Okay."]
    assert not any(message.get("id") == held for message in pi.messages("stop"))
    pi.go()
    thread.join(5)


def test_a_question_after_a_typed_request_takes_a_typed_answer(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="Which room?", listen_again=True),
                                                Reply(say="The kitchen lights are on."))
    thread = running(session)

    session.ask("Turn the lights on.")
    assert wait_for(lambda: _waiting_for_a_typed_answer(session))
    session.ask("The kitchen.")
    assert wait_for(lambda: len(brain.tts.said) == 2 and session._idle.is_set())

    # The answer, in the same conversation, which isn't started again for it - so a typed
    # "yes" confirms - and the microphone stays shut throughout.
    assert session.conversation.heard == [("Turn the lights on.", False), ("The kitchen.", True)]
    assert session.conversation.started == [False]
    assert pi.messages("listen") == [] and pi.messages("play_sound") == []
    assert session._typed_question is None
    pi.go()
    thread.join(5)


def test_an_answer_typed_while_the_question_is_said_is_taken(brain, connect, make_session):
    # A long question - typed straight back before it has finished, the answer still counts.
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="I've found three lights. Which room?", listen_again=True),
                                                Reply(say="The kitchen lights are on."))
    pi.held = {1}
    thread = running(session)

    session.ask("Turn the lights on.")
    assert wait_for(lambda: pi.messages("play_stream"))
    session.ask("The kitchen.")
    time.sleep(0.2)
    question = pi.messages("play_stream")[0]["id"]
    # The question isn't cut short for it.
    assert not any(message.get("id") == question for message in pi.messages("stop"))
    pi.say("played", id=question)

    assert wait_for(lambda: len(brain.tts.said) == 2 and session._idle.is_set())
    assert session.conversation.heard == [("Turn the lights on.", False), ("The kitchen.", True)]
    assert pi.messages("listen") == []
    pi.go()
    thread.join(5)


def test_a_typed_answer_ends_a_spoken_follow_up(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="Which room?", listen_again=True),
                                                Reply(say="The kitchen lights are on."))
    # What speech-to-text would have made of the answer, had it been listened to the end.
    brain.stt.texts = ["Turn the lights on.", "The hall."]
    thread = running(session)

    pi.wake(said())
    # Nothing is said back, so the listen stays open until the typed answer ends it.
    assert wait_for(lambda: pi.messages("listen"))
    session.ask("The kitchen.")
    assert wait_for(lambda: len(brain.tts.said) == 2 and session._idle.is_set())

    assert session.conversation.heard == [("Turn the lights on.", False), ("The kitchen.", True)]
    # That listen was ended there and then, and its transcription let go of.
    recording = pi.messages("listen")[0]["recording"]
    assert any(message.get("recording") == recording for message in pi.messages("stop_listening"))
    assert brain.stt.made[1].cancelled and brain.stt.made[1].finished_with is None
    assert len(pi.messages("listen")) == 1
    pi.go()
    thread.join(5)


def test_typed_requests_work_while_muted(brain, connect, make_session):
    # Typing uses no microphone, so mute - "Nova doesn't listen" - doesn't stop it.
    satellite, pi = connect()
    session = make_session(satellite)
    session.set_muted(True)
    session.conversation = ScriptedConversation(Reply(say="It's ten o'clock."))
    thread = running(session)

    session.ask("What time is it?")
    assert wait_for(lambda: pi.messages("allow") and session._idle.is_set())
    assert session.conversation.heard == [("What time is it?", False)]
    assert brain.tts.said == ["It's ten o'clock."] and len(pi.messages("play_stream")) == 1
    assert pi.messages("listen") == [] and pi.messages("play_sound") == []
    assert session.muted
    pi.go()
    thread.join(5)


def test_no_typed_answer_ends_the_turn_quietly(brain, connect, make_session, monkeypatch):
    monkeypatch.setattr(session_module, "TYPED_ANSWER_WAIT", 0.2)
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="Which room?", listen_again=True), Reply(say="Okay."))
    thread = running(session)

    session.ask("Turn the lights on.")
    # Left open, then given up on once its time is up.
    assert wait_for(lambda: session._typed_question is not None)
    assert wait_for(lambda: pi.messages("allow") and session._idle.is_set())
    assert wait_for(lambda: session._typed_question is None)
    assert session.conversation.heard == [("Turn the lights on.", False)]
    # Nothing more is said, nor any sound played, and nothing listened for.
    assert brain.tts.said == ["Which room?"]
    assert pi.messages("play_sound") == [] and pi.messages("listen") == []
    assert session.activity == "idle"

    # And something typed after it is a fresh request, in a conversation of its own.
    session.ask("Never mind.")
    assert wait_for(lambda: len(session.conversation.heard) == 2)
    assert session.conversation.heard[1] == ("Never mind.", False)
    assert session.conversation.started == [False, False]
    pi.go()
    thread.join(5)


def test_a_typed_answer_wait_ends_when_the_satellite_goes(brain, connect, make_session, caplog):
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="Which room?", listen_again=True))
    thread = running(session)

    session.ask("Turn the lights on.")
    assert wait_for(lambda: _waiting_for_a_typed_answer(session))
    pi.go()
    thread.join(2)

    assert not thread.is_alive()
    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []


def test_a_timer_rings_while_a_typed_answer_is_awaited(brain, connect, make_session):
    # Waiting in the turn, a timer went off up to a minute late while nobody typed.
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="Shut down the PC?", listen_again=True),
                                                Reply(say="Shutting it down."))
    thread = running(session)

    session.ask("Shut down my PC.")
    assert wait_for(lambda: _waiting_for_a_typed_answer(session))
    rang = []
    ringing = threading.Thread(target=lambda: rang.append(session.announce("Your timer's done.", ringing=True)),
                               daemon=True)
    ringing.start()
    ringing.join(3)
    assert rang == [True] and "Your timer's done." in brain.tts.said

    # The answer still counts, in the conversation which asked.
    session.ask("Yes.")
    assert wait_for(lambda: len(session.conversation.heard) == 2 and session._idle.is_set())
    assert session.conversation.heard == [("Shut down my PC.", False), ("Yes.", True)]
    assert session.conversation.started == [False]
    assert brain.tts.said[-1] == "Shutting it down."
    pi.go()
    thread.join(5)


def test_a_wake_word_clears_a_typed_question(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="Which room?", listen_again=True), Reply(say="Okay."),
                                                Reply(say="Sorry?"))
    brain.stt.texts = ["Stop."]
    thread = running(session)

    session.ask("Turn the lights on.")
    assert wait_for(lambda: _waiting_for_a_typed_answer(session))
    pi.wake(said())
    assert wait_for(lambda: len(brain.tts.said) == 2 and session._idle.is_set())
    assert session.conversation.heard == [("Turn the lights on.", False), ("Stop.", False)]
    assert session._typed_question is None

    # So what's typed next is a fresh request, not that question's answer.
    session.ask("The kitchen.")
    assert wait_for(lambda: len(brain.tts.said) == 3 and session._idle.is_set())
    assert session.conversation.heard[2] == ("The kitchen.", False)
    assert session.conversation.started == [False, False, False]
    pi.go()
    thread.join(5)


def test_a_routine_clears_a_typed_question(brain, make_session):
    # Its conversation of its own drops what a yes to the question would have done.
    session = make_session()
    session.music = SimpleNamespace(hold=lambda: None, release=lambda after=0: None)
    session.conversation = SimpleNamespace(start=lambda: None, run_routine=lambda routine: Reply())
    session._typed_question = (time.monotonic(), time.monotonic() + 60)

    session.run_routine({"name": "Good morning", "steps": ["what's the weather"]})
    assert session._typed_question is None


def test_a_routine_does_what_is_left_until_after_it_is_said(brain, make_session):
    # A routine's "lock my PC" at a PC is pressed once the routine's words are said, or when it says nothing.
    session = make_session()
    order = []
    session.music = SimpleNamespace(hold=lambda: None, release=lambda after=0: None)
    session._play_announcement = lambda playback, still_wanted=None: order.append("said")
    session.speaker = SimpleNamespace(play_stream=lambda *args, **kwargs: None, close=lambda: None)
    brain.tts = SimpleNamespace(stream=lambda message: None, sample_rate=22050)
    after = [lambda: order.append("press") or {"success": True}]
    session.conversation = SimpleNamespace(start=lambda: None, run_routine=lambda routine: Reply(say="Night.", after=after))
    session.run_routine({"name": "Night", "steps": ["lock my pc"]})
    session.conversation.run_routine = lambda routine: Reply(after=after)
    session.run_routine({"name": "Night", "steps": ["lock my pc"]})
    assert order == ["said", "press", "press"]


def test_an_answer_typed_before_its_question_is_talked_over_is_answered_after(brain, connect, make_session):
    # Kept as a fresh request rather than lost, once the wake word said over the question is answered.
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="I've found three lights. Which room?", listen_again=True),
                                                Reply(say="Okay."), Reply(say="Sorry?"))
    brain.stt.texts = ["Stop."]
    pi.held = {1}
    thread = running(session)

    session.ask("Turn the lights on.")
    assert wait_for(lambda: pi.messages("play_stream"))
    session.ask("The kitchen.")
    assert wait_for(lambda: session._typed_answer == "The kitchen.")
    pi.wake(said(), talked_over=True)
    assert wait_for(lambda: len(session.conversation.heard) == 3 and session._idle.is_set())

    assert session.conversation.heard == [("Turn the lights on.", False), ("Stop.", False), ("The kitchen.", False)]
    assert session.conversation.started == [False, True, False]
    pi.go()
    thread.join(5)


def test_something_typed_before_a_typed_question_isnt_its_answer(brain, connect, make_session):
    # Typed while the first request was still being worked out, before Nova knew it would ask.
    satellite, pi = connect()
    session = make_session(satellite)
    working, let_go = threading.Event(), threading.Event()

    class Slow(ScriptedConversation):
        def handle(self, text, follow_up=False, timeline=None):
            if not self.heard:
                working.set()
                let_go.wait(5)
            return super().handle(text, follow_up=follow_up, timeline=timeline)

    session.conversation = Slow(Reply(say="Which room?", listen_again=True), Reply(say="Okay."))
    thread = running(session)

    session.ask("Turn the lights on.")
    assert working.wait(5)
    session.ask("What time is it?")
    let_go.set()
    assert wait_for(lambda: len(session.conversation.heard) == 2 and session._idle.is_set())

    # A fresh request, which drops the question.
    assert session.conversation.heard == [("Turn the lights on.", False), ("What time is it?", False)]
    assert session.conversation.started == [False, False]
    assert session._typed_question is None
    pi.go()
    thread.join(5)


def test_a_typed_request_waits_for_an_announcement_being_said(brain, connect, make_session):
    # Answered over it, it silenced a Speak, an alert or a routine part way - which then
    # counted as said, so a battery warning, sent again until it is, was lost.
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="It's ten o'clock."))
    pi.held = {1}
    thread = running(session)
    said_out_loud = []
    announcing = threading.Thread(target=lambda: said_out_loud.append(session.announce("The washing's done.")),
                                  daemon=True)
    announcing.start()
    assert wait_for(lambda: pi.messages("play_stream"))

    session.ask("What time is it?")
    time.sleep(3 * session_module.WAKE_CHECK)
    assert pi.messages("silence") == [] and session.conversation.heard == []

    pi.say("played", id=pi.messages("play_stream")[0]["id"])
    announcing.join(3)
    assert said_out_loud == [True]
    assert wait_for(lambda: session.conversation.heard == [("What time is it?", False)] and session._idle.is_set())
    assert brain.tts.said == ["The washing's done.", "It's ten o'clock."]
    pi.go()
    thread.join(5)


def test_only_a_wake_word_satellite_gets_playlist_tiles(make_session, connect):
    assert make_session()._device_controls().playlist_tiles is True
    satellite, _ = connect(name="pc", hostname="pc.local")
    satellite.push_to_talk = True
    assert make_session(satellite)._device_controls().playlist_tiles is False


def test_the_device_is_given_ask(make_session):
    session = make_session()
    assert session._device_controls().ask == session.ask


# The satellite's device in Home Assistant

def test_home_assistant_sees_and_changes_nova_as_the_settings_page_does(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    changed, played_at = [], []

    class Spotify:
        # Read every second, so Spotify's servers are never asked - Nova's own note of
        # the music's volume is used instead, as on the settings page.
        needs_internet = True

        def get_volume(self):
            raise AssertionError("Spotify was asked for its volume")

        def set_volume(self, percent):
            played_at.append(percent)
            return {"success": True, "message": "Done."}

        def close(self):
            pass

    levels = {"general": 60, "alarm": 80, "music": 40}
    session.volume = SimpleNamespace(
        CATEGORIES=("general", "alarm", "music"), percent=levels.get, get=lambda category: levels[category] / 100,
        set=lambda category, percent: changed.append((category, percent)) or {"success": True})
    session.music_player = Spotify()
    controls = session._device_controls()

    session.activity = "thinking"
    assert controls.status() == "thinking"
    assert controls.volumes() == {"general": 60, "alarm": 80, "music": 40}
    # The music's volume through the player, as the settings page changes it - and
    # Nova's note of Spotify's follows, so Home Assistant's slider doesn't spring back.
    controls.set_volume("music", 30)
    controls.set_volume("alarm", 90)
    assert played_at == [30] and changed == [("music", 30), ("alarm", 90)]

    # Whether the brain's speech-to-text is loaded.
    assert controls.backend() is True
    brain.stt = SimpleNamespace(loaded=False)
    assert controls.backend() is False

    controls.set_muted(True)
    assert session.muted and controls.muted()
    # Restart restarts the satellite.
    controls.restart()
    assert wait_for(lambda: pi.messages("restart"))


def test_home_assistant_sees_only_the_satellites_own_timers_and_alarms(brain, connect, make_session):
    # Each satellite's clock sensors showed every satellite's.
    in_bedroom = make_session(connect("bedroom", "bedroom-pi")[0])
    in_kitchen = make_session(connect("kitchen", "kitchen-pi")[0])
    brain.clock.set_timer(600, satellite="bedroom")
    brain.clock.set_timer(1200, label="pizza", satellite="kitchen")
    brain.clock.set_alarm(7, 0, satellite="kitchen")

    bedroom, kitchen = in_bedroom._device_controls().clock(), in_kitchen._device_controls().clock()
    assert [timer["name"] for timer in bedroom["timers"]] == ["10 minute timer"] and bedroom["alarms"] == []
    assert [timer["name"] for timer in kitchen["timers"]] == ["pizza reminder"] and len(kitchen["alarms"]) == 1


def test_speak_is_said_in_turn_even_when_muted(make_session):
    # Mute only stops Nova hearing "Hey Nova", so an automation's "the washing's done"
    # is still said. It's handed off at once, so a Stop pressed in Home Assistant
    # meanwhile isn't kept waiting behind it.
    let_go, said_out_loud = threading.Event(), []

    def announce(message):
        let_go.wait(2)
        said_out_loud.append(message)

    session = make_session()
    session.set_muted(True)
    session.announce = announce
    session._device_controls().speak("The washing's done")
    assert said_out_loud == []

    let_go.set()
    assert wait_for(lambda: said_out_loud, seconds=2)
    assert said_out_loud == ["The washing's done"]


@pytest.mark.parametrize("typed, seconds", [
    ("10", 600), ("1:20", 4800), ("1h 20m", 4800), ("1h 20", 4800), ("1 hour 20 minutes", 4800),
    ("90 min", 5400), ("30s", 30), ("2 hours", 7200), ("1 hour and 5 minutes", 3900),
    ("1h", 3600), ("45", 2700), ("20 mins", 1200), ("10m30s", 630), ("1H 20M", 4800), (" 1h 20m ", 4800),
    ("", None), ("0", None), ("soon", None), ("25 hours", None), ("10 bananas", None), ("20 5", None),
    ("1:60", None), ("1:99", None)])
def test_a_length_of_time_typed_for_a_timer(typed, seconds):
    assert session_module.timer_seconds(typed) == seconds


def test_timers_and_alarms_from_home_assistant_are_set_silently(make_session, brain, caplog):
    set_up = []
    brain.clock = SimpleNamespace(
        set_timer=lambda duration_seconds=None, satellite=None: set_up.append(("timer", duration_seconds, satellite))
        or {"success": True},
        set_alarm=lambda hour, minute, repeat=None, satellite=None: set_up.append(("alarm", hour, minute, repeat, satellite))
        or {"success": hour != 8, "message": "There's already an alarm at 8 AM."},
        overview=lambda satellite=None: {})
    session = make_session()
    said = []
    session.announce = lambda *args, **kwargs: said.append(args)
    controls = session._device_controls()
    controls.add_timer("1h 20")
    controls.add_alarm("07:05")
    controls.add_alarm("7:5")
    controls.add_alarm("25:00")
    controls.add_timer("whenever")
    with caplog.at_level(logging.WARNING):
        controls.add_alarm("08:00")
    name = session.satellite.name
    assert set_up == [("timer", 4800, name), ("alarm", 7, 5, None, name), ("alarm", 8, 0, None, name)]
    assert said == []
    # One the clock refused is logged with why.
    assert any("already an alarm" in record.getMessage() for record in caplog.records)


def test_say_on_nova_is_said_in_turn_without_a_sound(make_session):
    # Typed on the Nova view to be said out loud, so no chime first: the person typing
    # knows it's coming. Speak, which automations use, keeps its sound.
    let_go, said_out_loud = threading.Event(), []

    def announce(message, sound="announcement_sound.wav"):
        let_go.wait(2)
        said_out_loud.append((message, sound))

    session = make_session()
    session.set_muted(True)
    session.announce = announce
    session._device_controls().say("Dinner's ready")
    assert said_out_loud == []

    let_go.set()
    assert wait_for(lambda: said_out_loud, seconds=2)
    assert said_out_loud == [("Dinner's ready", None)]


def test_stop_silences_what_rings_and_what_nova_is_saying(brain, make_session):
    session = make_session()
    happened, ringing = [], [True]
    brain.clock = SimpleNamespace(is_ringing=lambda satellite=None: ringing[0],
                                  dismiss=lambda satellite=None: happened.append(("dismissed", satellite))
                                  or {"success": True, "message": "Okay."})
    session.speaker = SimpleNamespace(stop=lambda kinds: happened.append(("stopped", list(kinds))), close=lambda: None)
    controls = session._device_controls()

    controls.stop()
    # Only what rings on this satellite.
    assert happened == [("dismissed", "bedroom"), ("stopped", ["reply", "announcement"])]

    # With nothing ringing, there's nothing to dismiss.
    ringing[0] = False
    happened.clear()
    controls.stop()
    assert happened == [("stopped", ["reply", "announcement"])]


def test_home_assistants_stop_is_the_same_code(make_session):
    # The settings page's Stop goes through stop_what_rings_and_speaks too, so the two
    # can never drift apart.
    session = make_session()
    assert session._device_controls().stop == session.stop_what_rings_and_speaks


class StubPlayer:
    """Stands in for a music player: says what it is playing, and has the one cover and the playlists given."""

    def __init__(self, playing=None):
        self.playing = playing
        self.closed = False

    def now_playing(self):
        return self.playing

    def cover(self):
        return b"jpeg", "image/jpeg"

    def playlists(self):
        return ["Gym", "Evening"]

    def close(self):
        self.closed = True


class RecordingActions:
    """Stands in for the session's actions, keeping what Home Assistant had them do."""

    def __init__(self):
        self.calls = []

    def control_playback(self, action=None):
        self.calls.append(("control_playback", action))
        return {"success": True, "message": "Okay."}

    def play_without_offer(self, query=None, search_type=None):
        self.calls.append(("play_without_offer", query))
        return {"success": True, "message": "Playing."}

    def play_playlist(self, name=""):
        self.calls.append(("play_playlist", name))
        return {"success": True, "message": "Playing."}


@pytest.fixture
def with_music(brain, monkeypatch, make_session):
    """Makes a session with a stub player saying what it is playing, and actions that keep what they were asked."""
    def make(playing=None):
        player = StubPlayer(playing)
        monkeypatch.setattr(session_module, "make_player", lambda config, speaker, volume, spotify_speaker=None: player)
        session = make_session()
        session.actions = RecordingActions()
        return session, player

    return make


def test_without_a_player_the_device_has_no_music(make_session):
    controls = make_session()._device_controls()
    assert controls.now_playing is None and controls.cover is None and controls.playlists is None
    assert controls.playback is None and controls.play_playlist is None


def test_with_a_player_the_device_reads_the_music_from_it(with_music):
    session, player = with_music({"playing": True, "title": "Song", "artist": "Band"})
    controls = session._device_controls()
    assert controls.now_playing() == {"playing": True, "title": "Song", "artist": "Band"}
    assert controls.cover() == (b"jpeg", "image/jpeg")
    assert controls.playlists() == ["Gym", "Evening"]


def test_play_pause_pauses_what_plays_and_plays_what_does_not(with_music):
    session, player = with_music({"playing": True})
    controls = session._device_controls()
    controls.playback("play_pause")
    assert session.actions.calls == [("control_playback", "pause")]

    # Paused, or nothing playing at all, it plays - as "play some music" does.
    for playing in ({"playing": False}, None):
        player.playing = playing
        session.actions.calls.clear()
        controls.playback("play_pause")
        assert session.actions.calls == [("play_without_offer", None)]


def test_next_and_previous_go_through_the_spoken_actions(with_music):
    session, _ = with_music({"playing": True})
    controls = session._device_controls()
    controls.playback("next")
    controls.playback("previous")
    assert session.actions.calls == [("control_playback", "skip"), ("control_playback", "previous")]


def test_something_else_from_home_assistant_does_nothing(with_music):
    session, _ = with_music({"playing": True})
    session._device_controls().playback("rewind")
    assert session.actions.calls == []


def test_a_playlist_from_home_assistant_plays_through_the_actions(with_music):
    session, _ = with_music()
    session._device_controls().play_playlist("Gym")
    assert session.actions.calls == [("play_playlist", "Gym")]


def test_what_home_assistant_does_to_the_music_is_logged_without_what_plays(with_music, caplog):
    session, _ = with_music({"playing": True, "title": "Secret Song"})
    controls = session._device_controls()
    with caplog.at_level(logging.INFO):
        controls.playback("play_pause")
        controls.playback("next")
        controls.playback("previous")
        controls.play_playlist("Private Gym")
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "Play/pause from Home Assistant" in logged
    assert "Next from Home Assistant" in logged and "Previous from Home Assistant" in logged
    assert "Secret Song" not in logged and "Private Gym" not in logged


# Windows' media controls - see docs/decisions/0031

class ButtonPlayer(StubPlayer):
    """A stub player which also keeps what the buttons of Windows' media controls had it do."""

    def __init__(self, playing=None):
        super().__init__(playing)
        self.done = []

    def pause(self):
        self.done.append("pause")
        if self.playing:
            self.playing = dict(self.playing, playing=False)
        return {"success": True, "message": "Paused"}

    def cancel_resume(self):
        self.done.append("cancel_resume")

    def skip(self):
        self.done.append("skip")
        return {"success": True, "message": "Skipped"}

    def previous(self):
        self.done.append("previous")
        return {"success": True, "message": "Previous track"}

    def was_playing(self):
        return False


@pytest.fixture
def with_media_controls(brain, connect, monkeypatch, make_session):
    """
    Makes a running session for a satellite with Windows' media controls - or without, if
    asked - with a player saying what it plays, and actions which keep what they're asked.
    """
    monkeypatch.setattr(session_module, "MEDIA_EVERY", 0.05)

    def make(playing=None, media_controls=True, player=True, name="pc"):
        made = ButtonPlayer(playing) if player else None
        monkeypatch.setattr(session_module, "make_player",
                            lambda config, speaker, volume, spotify_speaker=None: made)
        satellite, pi = connect(name, f"{name}.local", media_controls=media_controls)
        session = make_session(satellite)
        session.actions = RecordingActions()
        running(session)
        return session, made, pi

    return make


def now_playing(pi):
    return [(message, payload) for message, payload in pi.received if message["type"] == "now_playing"]


SONG = {"track": "Song", "artist": "Band", "album": "Album", "playing": True, "position": 3.0,
        "duration": 200, "cover": "al-1"}


def test_a_satellite_with_media_controls_is_told_what_its_music_is_doing(with_media_controls):
    session, player, pi = with_media_controls(dict(SONG))
    assert wait_for(lambda: now_playing(pi))
    assert now_playing(pi) == [({"type": "now_playing", "state": "playing", "track": "Song", "artist": "Band",
                                 "album": "Album", "cover": True, "bytes": 4}, b"jpeg")]
    # Read again and again, but sent only when it changes - and the cover only with a new one.
    time.sleep(0.3)
    assert len(now_playing(pi)) == 1
    player.playing = dict(SONG, playing=False, position=4.0)
    assert wait_for(lambda: len(now_playing(pi)) == 2)
    assert now_playing(pi)[1] == ({"type": "now_playing", "state": "paused", "track": "Song", "artist": "Band",
                                   "album": "Album"}, b"")
    player.playing = None
    assert wait_for(lambda: len(now_playing(pi)) == 3)
    assert now_playing(pi)[2][0] == {"type": "now_playing", "state": "stopped", "track": "", "artist": "",
                                     "album": ""}


def test_spotify_playing_elsewhere_is_stopped_on_the_satellite(with_media_controls):
    session, player, pi = with_media_controls(dict(SONG, here=False))
    assert wait_for(lambda: now_playing(pi))
    assert now_playing(pi)[0][0]["state"] == "stopped"


def test_a_satellite_without_media_controls_is_never_told(with_media_controls):
    session, player, pi = with_media_controls(dict(SONG), media_controls=False)
    pi.say("media", action="pause")
    time.sleep(0.3)
    assert now_playing(pi) == []
    assert player.done == []


def test_without_music_a_satellite_with_media_controls_is_never_told(with_media_controls):
    session, player, pi = with_media_controls(media_controls=True, player=False)
    time.sleep(0.3)
    assert now_playing(pi) == []


def test_pause_from_windows_pauses_this_satellites_own_music_and_keeps_it_paused(with_media_controls):
    session, player, pi = with_media_controls(dict(SONG))
    assert wait_for(lambda: now_playing(pi))
    pi.say("media", action="pause")
    assert wait_for(lambda: len(now_playing(pi)) == 2)
    # Paused here, and not brought back once Nova finishes talking.
    assert player.done == ["pause", "cancel_resume"]
    assert now_playing(pi)[1][0]["state"] == "paused"
    # Never through the actions, which pause the whole room.
    assert session.actions.calls == []


def test_a_pause_from_windows_which_fails_still_keeps_the_music_from_coming_back(with_media_controls):
    # Spotify's pause can fail, say, while the music is paused for Nova talking: the
    # user still asked for it paused, so it isn't brought back once Nova finishes.
    session, player, pi = with_media_controls(dict(SONG))
    player.pause = lambda: player.done.append("pause") or {"success": False, "message": "No"}
    assert wait_for(lambda: now_playing(pi))
    pi.say("media", action="pause")
    assert wait_for(lambda: len(now_playing(pi)) == 2)
    assert player.done == ["pause", "cancel_resume"]


@pytest.mark.parametrize("action, done", [("next", ["skip"]), ("previous", ["previous"])])
def test_next_and_previous_from_windows_skip_this_satellites_own_music(with_media_controls, action, done):
    session, player, pi = with_media_controls(dict(SONG))
    assert wait_for(lambda: now_playing(pi))
    pi.say("media", action=action)
    # Sent again at once, even unchanged, so the satellite shows what the brain made of it.
    assert wait_for(lambda: len(now_playing(pi)) == 2)
    assert player.done == done
    assert session.actions.calls == []


@pytest.mark.parametrize("playing", [dict(SONG, playing=False), None], ids=["paused", "nothing"])
def test_play_from_windows_plays_as_home_assistants_play_does(with_media_controls, playing):
    # Carries on what's paused here, or with nothing loaded plays as "play some music" does.
    session, player, pi = with_media_controls(playing)
    assert wait_for(lambda: now_playing(pi))
    pi.say("media", action="play")
    assert wait_for(lambda: len(now_playing(pi)) == 2)
    assert session.actions.calls == [("play_without_offer", None)]
    assert player.done == []


def test_pause_from_windows_leaves_the_rest_of_the_room_playing(brain, with_media_controls):
    pc, pc_player, pi = with_media_controls(dict(SONG))
    other, other_player, _ = with_media_controls(dict(SONG), media_controls=False, name="bedroom")
    brain.room_sessions = lambda name: [pc, other]
    assert wait_for(lambda: now_playing(pi))
    pi.say("media", action="pause")
    assert wait_for(lambda: len(now_playing(pi)) == 2)
    assert pc_player.done == ["pause", "cancel_resume"]
    assert other_player.done == []


def test_what_windows_does_to_the_music_is_logged_without_what_plays(with_media_controls, caplog):
    session, player, pi = with_media_controls(dict(SONG, track="Secret Song"))
    assert wait_for(lambda: now_playing(pi))
    with caplog.at_level(logging.INFO):
        for action in ("pause", "play", "next", "previous"):
            pi.say("media", action=action)
        assert wait_for(lambda: len(now_playing(pi)) == 5)
    logged = " ".join(record.getMessage() for record in caplog.records)
    for said in ("Pause from Windows", "Play from Windows", "Next from Windows", "Previous from Windows"):
        assert said in logged
    assert "Secret Song" not in logged


def test_a_player_which_fails_is_logged_once_and_tried_again(with_media_controls, caplog):
    session, player, pi = with_media_controls(dict(SONG))
    failing = [True]

    def now():
        if failing[0]:
            raise RuntimeError("Navidrome went away")
        return dict(SONG, track="Back")

    with caplog.at_level(logging.ERROR):
        player.now_playing = now
        time.sleep(0.3)
        failing[0] = False
        assert wait_for(lambda: [message for message, _ in now_playing(pi) if message["track"] == "Back"])
    assert len([record for record in caplog.records if "Navidrome went away" in record.getMessage()]) == 1


def test_stop_never_deletes_a_repeating_alarm(brain, make_session):
    # A daily alarm stopped as it rings has only this morning's silenced - it rings
    # again the next day, as saying "stop" leaves it.
    session = make_session()
    clock = brain.clock
    clock.alarm_ring_duration = 0
    clock.set_alarm(7, 0, "daily", satellite="bedroom")
    [alarm] = clock._alarms.values()
    # The 7am before the next one, which has just come due.
    ringing = datetime.fromtimestamp(alarm["due_at"]) - timedelta(days=1)
    alarm["due_at"] = ringing.timestamp()
    clock._tick(time.time())
    assert clock.is_ringing(satellite="bedroom")

    session.stop_what_rings_and_speaks()

    assert not clock.is_ringing()
    [kept] = clock.overview()["alarms"]
    assert kept["id"] == alarm["id"]
    assert datetime.fromtimestamp(kept["due_at"]) == ringing + timedelta(days=1)


def test_stop_leaves_the_music_playing(brain, make_session):
    # Stop is for what rings and what Nova says - pressed while a song plays under an
    # announcement, the song carries on.
    session = make_session()
    stopped = []
    session.speaker = SimpleNamespace(stop=lambda kinds: stopped.append(list(kinds)), close=lambda: None)
    brain.clock.set_timer(60, satellite="bedroom")
    brain.clock._tick(time.time() + 61)
    session.activity = "speaking"

    session.stop_what_rings_and_speaks()

    assert stopped == [["reply", "announcement"]]
    assert not brain.clock.is_ringing()


def test_the_device_is_looked_up_until_found_then_now_and_then(make_session):
    # Often until Home Assistant has taken in the device's announcement, then now and
    # then, in case the user has moved it to another room.
    found, asked, waited = [False, False, True, True], [], []

    class Stopping:
        """Stands in for the session closing, which comes after the fourth wait."""

        def wait(self, timeout):
            waited.append(timeout)
            return len(waited) == 4

        def set(self):
            pass

    session = make_session()
    session.home = SimpleNamespace(find_device=lambda identifier: asked.append(identifier) or found.pop(0), area=None)
    session.device = SimpleNamespace(identifier="nova_home_assistant", stop=lambda: None)
    session._stop_looking_up = Stopping()
    session._look_up_device()

    assert asked == ["nova_home_assistant"] * 4
    assert waited == [30, 30, 300, 300]


def test_the_room_found_in_home_assistant_is_kept(brain, make_session):
    # So the brain knows which satellites share a room, and still does after a restart.
    session = make_session()
    _looked_up(session, ("Study", "devbedroom"), (None, None))

    assert brain.rooms.area("bedroom") == "Study"
    assert SatelliteRooms(str(brain.rooms.path)).area("bedroom") == "Study"


def test_a_session_closed_before_it_runs_never_starts_its_device(connect, make_session):
    # Replaced by its satellite connecting again before it ran - the brain closes it
    # first. Its device started regardless, and stayed connected for good, with the
    # same MQTT client id as the new session's.
    satellite, pi = connect()
    session = make_session(satellite)
    happened = []
    session.device = Device(happened)
    session.home = SimpleNamespace(find_device=lambda identifier: True, area=None)
    session.close()
    session.run()

    assert happened == ["stopped"]
    assert not any(thread.name.startswith("ha-device-lookup") for thread in threading.enumerate())


@pytest.fixture
def spotify(monkeypatch):
    """Spotify as each session's player, with its keys set and never signed in, so nothing goes to Spotify."""
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "client-id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    monkeypatch.setattr(spotify_controller, "spotify_oauth", lambda **kwargs: None)
    monkeypatch.setattr(spotify_controller.SpotifyController, "_authenticate", lambda self: False)
    monkeypatch.setattr(session_module, "make_player", music.make_player)


def test_each_session_plays_on_its_own_satellites_speaker(brain, connect, make_session, spotify):
    bedroom = make_session(connect("bedroom", speaker="Bedroom Pi")[0])
    kitchen = make_session(connect("kitchen", speaker="Kitchen Pi")[0])

    assert isinstance(bedroom.music_player, spotify_controller.SpotifyController)
    assert bedroom.music_player.device_name == "Bedroom Pi"
    assert kitchen.music_player.device_name == "Kitchen Pi"


def test_a_satellite_that_doesnt_say_uses_the_brains_name(brain, connect, make_session, spotify):
    # As one from before satellites said - which plays where every satellite did then.
    brain.config.apply({("spotify", "device_name"): "Living Room"})
    assert make_session(connect("bedroom")[0]).music_player.device_name == "Living Room"


# What's playing in the satellite's room

def _playing(*players):
    """A media watcher which has read these players, each as (entity, name, area, device, source)."""
    rows = [dict(zip(("entity_id", "name", "area", "device", "source"), player)) for player in players]
    watcher = MediaWatcher(SimpleNamespace(playing_media=lambda: rows), on_change=lambda: None)
    watcher.check()
    return watcher


def _in_room(session, area, device_id):
    """Puts a session's satellite in a room, on its own device in Home Assistant."""
    session.home = SimpleNamespace(area=area, device_id=device_id)
    return session


def test_each_satellite_hears_whats_playing_in_its_own_room(brain, connect, make_session):
    bedroom = _in_room(make_session(connect("bedroom")[0]), "Bedroom", "devbedroom")
    kitchen = _in_room(make_session(connect("kitchen", "kitchen-pi.local")[0]), "Kitchen", "devkitchen")
    brain.connected_sessions = lambda: [bedroom, kitchen]
    brain.media = _playing(("media_player.bedroom_tv", "Bedroom TV", "Bedroom", "devtv", None),
                           ("media_player.radio", "Radio", "Kitchen", "devradio", None))
    assert bedroom.playing_here() == ["Bedroom TV"]
    assert kitchen.playing_here() == ["Radio"]


def test_nothing_on_any_connected_satellites_device_or_speaker_counts(brain, connect, make_session):
    brain.config.apply({("spotify", "device_name"): "Voice-Assistant"})
    bedroom = _in_room(make_session(connect("bedroom")[0]), "Bedroom", "devbedroom")
    kitchen = _in_room(make_session(connect("kitchen", "kitchen-pi.local", speaker="Kitchen Pi")[0]),
                       "Kitchen", "devkitchen")
    brain.connected_sessions = lambda: [bedroom, kitchen]
    brain.media = _playing(
        # The kitchen satellite's own device, though it's in the bedroom's room.
        ("media_player.kitchen_pi", "Kitchen Pi", "Bedroom", "devkitchen", None),
        # Spotify on the kitchen satellite's speaker, and on the brain's, which the bedroom one plays on.
        ("media_player.spotify_a", "Spotify A", "Bedroom", None, " kitchen pi"),
        ("media_player.spotify_b", "Spotify B", "Bedroom", None, "voice-assistant "),
        ("media_player.bedroom_tv", "Bedroom TV", "Bedroom", "devtv", "HDMI 1"))
    assert bedroom.playing_here() == ["Bedroom TV"]


def test_a_session_counts_its_own_device_before_the_brain_has_it(brain, connect, make_session):
    bedroom = _in_room(make_session(connect("bedroom")[0]), "Bedroom", "devbedroom")
    brain.connected_sessions = lambda: []
    brain.media = _playing(("media_player.bedroom_pi", "Bedroom Pi", "Bedroom", "devbedroom", None))
    assert bedroom.playing_here() == []


def test_nothing_plays_here_without_a_watcher_home_assistant_or_a_room(brain, connect, make_session):
    session = make_session(connect("bedroom")[0])
    brain.connected_sessions = lambda: [session]
    brain.media = _playing(("media_player.bedroom_tv", "Bedroom TV", "Bedroom", "devtv", None))
    # No Home Assistant set up here.
    assert session.home is None and session.playing_here() == []
    _in_room(session, None, None)
    assert session.playing_here() == []
    _in_room(session, "Bedroom", "devbedroom")
    assert session.playing_here() == ["Bedroom TV"]
    brain.media = None
    assert session.playing_here() == []


class _Claude:
    """Stands in for the Anthropic client, answering every request with these content blocks."""

    def __init__(self, *content):
        self.content = list(content)
        self.requests = []
        self.messages = self

    def create(self, **request):
        self.requests.append(request)
        return SimpleNamespace(content=self.content, stop_reason="tool_use")


def _film_heard(brain, connect, make_session, monkeypatch, playing):
    """
    A session in the Bedroom hearing "Turn the lights off, Jack." with these players there, each as
    _playing has them, which Claude takes for speech from what's playing - beside working the lights.
    """
    monkeypatch.setattr(network, "online", lambda: True)
    satellite, pi = connect()
    session = _in_room(make_session(satellite), "Bedroom", "devbedroom")
    brain.connected_sessions = lambda: [session]
    brain.media = _playing(*playing)
    session.llm.client = _Claude(SimpleNamespace(type="tool_use", id="t1", name="not_for_me", input={}),
                                 SimpleNamespace(type="tool_use", id="t2", name="set_lights",
                                                 input={"on": False, "light": "light.lamp"}))
    run, held = [], []
    session.actions.execute = run.append
    session.music = session.conversation.music = SimpleNamespace(
        hold=lambda: held.append("hold"), release=lambda: held.append("release"), wait=lambda: None)
    brain.stt.texts = ["Turn the lights off, Jack."]
    feed = AudioFeed()
    feed.feed(said())
    feed.end()
    session._converse({"talked_over": False, "feed": feed})
    return session, pi, run, held


def test_speech_from_whats_playing_here_is_ignored_and_the_music_comes_back(brain, connect, make_session,
                                                                            monkeypatch):
    session, pi, run, held = _film_heard(brain, connect, make_session, monkeypatch,
                                         [("media_player.bedroom_tv", "Bedroom TV", "Bedroom", "devtv", None)])
    assert "Playing in this room: Bedroom TV.] Turn the lights off, Jack." in \
        session.llm.client.requests[0]["messages"][-1]["content"]
    # Nothing said, played, run or listened for - and the music back as after any reply.
    assert run == [] and brain.tts.said == [] and pi.messages("play_stream") == []
    assert [message["name"] for message in pi.messages("play_sound")] == ["listening.wav"]
    assert pi.messages("listen") == [] and session.llm.history == []
    assert held == ["hold", "release"] and session.activity == "idle"


def test_not_for_me_with_nothing_playing_here_is_said_to_be_not_caught(brain, connect, make_session, monkeypatch):
    # Playing in the kitchen, which isn't where this satellite is.
    session, _, run, held = _film_heard(brain, connect, make_session, monkeypatch,
                                        [("media_player.radio", "Radio", "Kitchen", "devradio", None)])
    assert "Playing" not in session.llm.client.requests[0]["messages"][-1]["content"]
    assert run == [] and brain.tts.said == ["Sorry, I didn't catch that."]
    assert held == ["hold", "release"]


class _Finding:
    """Stands in for a session's Home Assistant, finding its satellite in each (room, device) given in turn."""

    def __init__(self, *found):
        self.found = list(found)
        self.area = self.device_id = None

    def find_device(self, identifier):
        self.area, self.device_id = self.found.pop(0)
        return self.device_id is not None


def _looked_up(session, *found):
    """Has the session look its device up once for each (room, device) given, then close."""
    session.home = _Finding(*found)
    session.device = SimpleNamespace(identifier="nova_bedroom_pi", stop=lambda: None)
    waits = []
    session._stop_looking_up = SimpleNamespace(wait=lambda timeout: waits.append(timeout) or len(waits) == len(found),
                                               set=lambda: None)
    session._look_up_device()


def test_a_satellite_whose_room_becomes_known_while_a_film_plays_is_sent_its_state(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    brain.connected_sessions = lambda: [session]
    brain.media = _playing(("media_player.bedroom_tv", "Bedroom TV", "Bedroom", "devtv", None))
    # As after a restart: the satellite was sent its state as it connected, before its room was known.
    _looked_up(session, (None, None), ("Bedroom", "devbedroom"))
    assert session.playing_here() == ["Bedroom TV"]
    assert wait_for(lambda: len(pi.messages("state")) == 1)


def test_a_satellite_is_sent_its_state_each_time_its_room_or_device_changes(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    brain.connected_sessions = lambda: [session]
    brain.media = _playing()
    _looked_up(session, ("Bedroom", "devbedroom"), ("Bedroom", "devbedroom"), ("Kitchen", "devbedroom"),
               ("Kitchen", "devother"), ("Kitchen", "devother"))
    assert wait_for(lambda: len(pi.messages("state")) == 3)
    time.sleep(0.2)
    assert len(pi.messages("state")) == 3


def test_a_room_found_without_a_media_watcher_sends_nothing(brain, connect, make_session):
    satellite, pi = connect()
    _looked_up(make_session(satellite), ("Bedroom", "devbedroom"))
    time.sleep(0.2)
    assert pi.messages("state") == []


# How sure the satellite must be of its wake word

def _wake_confidence_sent(session, pi):
    """Has the session send its state, and returns the wake confidence the satellite got."""
    before = len(pi.messages("state"))
    assert session.send_state()
    assert wait_for(lambda: len(pi.messages("state")) == before + 1)
    return pi.messages("state")[-1]["wake_confidence"]


def test_the_satellite_is_sent_a_surer_wake_word_while_something_plays_in_its_room(brain, connect, make_session):
    satellite, pi = connect()
    session = _in_room(make_session(satellite), "Bedroom", "devbedroom")
    brain.connected_sessions = lambda: [session]
    brain.media = _playing()
    assert _wake_confidence_sent(session, pi) == 0.5
    brain.media = _playing(("media_player.bedroom_tv", "Bedroom TV", "Bedroom", "devtv", None))
    assert _wake_confidence_sent(session, pi) == 0.75
    # Something playing in another room doesn't count here.
    brain.media = _playing(("media_player.radio", "Radio", "Kitchen", "devradio", None))
    assert _wake_confidence_sent(session, pi) == 0.5


def test_the_wake_confidence_is_never_lower_while_something_plays(brain, connect, make_session):
    satellite, pi = connect()
    session = _in_room(make_session(satellite), "Bedroom", "devbedroom")
    brain.connected_sessions = lambda: [session]
    brain.media = _playing(("media_player.bedroom_tv", "Bedroom TV", "Bedroom", "devtv", None))
    # Read as it's sent, as the longest request is, so a save reaches it with no restart.
    brain.config.apply({("thresholds", "wake_word_confidence"): 0.6,
                        ("thresholds", "media_wake_word_confidence"): 0.4})
    assert _wake_confidence_sent(session, pi) == 0.6


def test_without_a_media_watcher_the_satellite_is_sent_the_normal_wake_confidence(brain, connect, make_session):
    satellite, pi = connect()
    session = _in_room(make_session(satellite), "Bedroom", "devbedroom")
    # No Home Assistant, so no watcher.
    assert brain.media is None
    assert _wake_confidence_sent(session, pi) == 0.5
    brain.config.apply({("thresholds", "wake_word_confidence"): 0.7})
    assert _wake_confidence_sent(session, pi) == 0.7


@pytest.mark.parametrize("left", [None, True, "0.6", float("nan")])
def test_a_wake_setting_thats_not_a_number_is_taken_as_its_default(brain, connect, make_session, left):
    # Left empty in config.yaml by hand, max() would raise as a film played - and with it
    # the catch-up of a satellite connecting, which couldn't then reach the brain at all.
    satellite, pi = connect()
    session = _in_room(make_session(satellite), "Bedroom", "devbedroom")
    brain.connected_sessions = lambda: [session]
    film = _playing(("media_player.bedroom_tv", "Bedroom TV", "Bedroom", "devtv", None))

    brain.config.apply({("thresholds", "wake_word_confidence"): left,
                        ("thresholds", "media_wake_word_confidence"): 0.8})
    brain.media = _playing()
    assert _wake_confidence_sent(session, pi) == 0.5
    brain.media = film
    assert _wake_confidence_sent(session, pi) == 0.8

    brain.config.apply({("thresholds", "wake_word_confidence"): 0.6,
                        ("thresholds", "media_wake_word_confidence"): left})
    assert _wake_confidence_sent(session, pi) == 0.75
    brain.media = _playing()
    assert _wake_confidence_sent(session, pi) == 0.6


def test_what_plays_in_a_satellites_room_is_logged_as_it_starts_and_stops(brain, connect, make_session, caplog):
    # So a satellite that was hard to wake can be traced to what Home Assistant said was
    # playing - a browser tab left on, say.
    caplog.set_level(logging.INFO, logger="voice_assistant.session")
    satellite, pi = connect()
    session = _in_room(make_session(satellite), "Bedroom", "devbedroom")
    brain.connected_sessions = lambda: [session]

    def logged():
        return [message for message in caplog.messages if "playing in the" in message]

    brain.media = _playing()
    _wake_confidence_sent(session, pi)
    assert logged() == []
    brain.media = _playing(("media_player.firefox", "Firefox", "Bedroom", "devpc", None),
                           ("media_player.bedroom_tv", "Bedroom TV", "Bedroom", "devtv", None))
    _wake_confidence_sent(session, pi)
    _wake_confidence_sent(session, pi)
    brain.media = _playing(("media_player.firefox", "Firefox", "Bedroom", "devpc", None))
    _wake_confidence_sent(session, pi)
    brain.media = _playing()
    _wake_confidence_sent(session, pi)
    _wake_confidence_sent(session, pi)
    assert logged() == [
        "Something's playing in the bedroom satellite's room (Bedroom TV, Firefox), so its wake word needs 0.75",
        "Nothing's playing in the bedroom satellite's room now, so its wake word needs 0.5"]


# How sure the satellite must be it's the user's voice

def _voice_check_sent(session, pi):
    """Has the session send its state, and returns the voice check the satellite got."""
    before = len(pi.messages("state"))
    assert session.send_state()
    assert wait_for(lambda: len(pi.messages("state")) == before + 1)
    return pi.messages("state")[-1]["voice_check"]


def test_the_satellite_is_sent_the_voice_check_as_it_is_set_each_time(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    brain.connected_sessions = lambda: [session]
    assert _voice_check_sent(session, pi) == 0.2
    # Read as it's sent, so a save reaches it with no restart - and 0, no check, is sent as 0.
    brain.config.apply({("thresholds", "voice_check"): 0.6})
    assert _voice_check_sent(session, pi) == 0.6
    brain.config.apply({("thresholds", "voice_check"): 0})
    assert _voice_check_sent(session, pi) == 0


@pytest.mark.parametrize("left", [None, True, "0.6", float("nan")])
def test_a_voice_check_thats_not_a_number_is_taken_as_its_default(brain, connect, make_session, left):
    # Left empty in config.yaml by hand, it would otherwise stop the state from being sent at all.
    satellite, pi = connect()
    session = make_session(satellite)
    brain.config.apply({("thresholds", "voice_check"): left})
    assert _voice_check_sent(session, pi) == 0.2


def test_two_states_sent_at_once_arrive_in_the_order_they_were_read(brain, connect, make_session):
    # The media watcher, the settings page and the device lookup each send on a thread of their own. Were
    # one's older state to arrive after another's newer one, the normal wake word would stay on for a film.
    satellite, pi = connect()
    session = _in_room(make_session(satellite), "Bedroom", "devbedroom")
    brain.connected_sessions = lambda: [session]
    brain.media = _playing()
    sent, sending, carry_on = [], threading.Event(), threading.Event()

    def set_state(volumes, muted, longest_request=None, wake_confidence=None, voice_check=None, screen_orb=None,
                  app=None):
        # The first send stalls on the way, as one to a satellite slow to read can.
        if not sending.is_set():
            sending.set()
            carry_on.wait(5)
        sent.append(wake_confidence)
        return True

    satellite.set_state = set_state
    first = threading.Thread(target=session.send_state)
    first.start()
    assert sending.wait(5)
    # A film starts while the first is on its way.
    brain.media = _playing(("media_player.bedroom_tv", "Bedroom TV", "Bedroom", "devtv", None))
    second = threading.Thread(target=session.send_state)
    second.start()
    # Long enough for the second to have been sent, had it not waited for the first.
    second.join(0.3)
    carry_on.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert sent == [0.5, 0.75]


def test_closing_a_session_ends_every_thread_it_started(connect, make_session, monkeypatch):
    # A satellite which connects again gets a new session, and the old one's threads
    # held the whole of it - Navidrome's waited for something to play for ever.
    satellite, pi = connect()
    monkeypatch.setattr(session_module, "make_player", lambda config, speaker, volume, spotify_speaker=None: NavidromePlayer(
        "http://navidrome.local:4533", "nova", "secret", speaker, volume))
    before = set(threading.enumerate())
    session = make_session(satellite)
    session.device = Device([])
    session.home = SimpleNamespace(find_device=lambda identifier: True, area=None)
    running(session)
    assert wait_for(lambda: any(lookup.name.startswith("ha-device-lookup") for lookup in threading.enumerate()))
    started = [started for started in threading.enumerate() if started not in before]
    assert "navidrome" in [started.name for started in started]

    session.close()
    for left in started:
        left.join(2)
    assert [left.name for left in started if left.is_alive()] == []


def test_the_satellite_is_in_home_assistant_only_while_its_session_runs(connect, make_session):
    # And the device is looked up from the start, until the satellite goes.
    satellite, pi = connect()
    session = make_session(satellite)
    happened, looked_up = [], threading.Event()

    def find_device(identifier):
        looked_up.set()
        return True

    session.device = Device(happened)
    session.home = SimpleNamespace(find_device=find_device, area=None)
    thread = running(session)
    assert looked_up.wait(2)
    pi.go()
    thread.join(2)

    assert not thread.is_alive() and happened == ["started", "stopped"]
    # The lookup ends straight away, rather than at its next check minutes later.
    for lookup in threading.enumerate():
        if lookup.name.startswith("ha-device-lookup"):
            lookup.join(1)
            assert not lookup.is_alive()


def test_a_sessions_actions_read_batteries_from_the_brains_watcher(brain, make_session):
    # One watcher for the brain, so a spoken answer and a warning are the same reading.
    brain.batteries = object()
    assert make_session().actions.batteries is brain.batteries


def test_a_sessions_actions_read_football_from_the_brain(brain, make_session):
    # A session names the followed teams to Claude as it starts, so it asks for them.
    asked = []
    brain.football = SimpleNamespace(names=lambda: asked.append("names") or ["Stevenage"], known_as=lambda: [])
    session = make_session()
    assert session.actions.football is brain.football
    tools = {tool["name"]: tool for tool in session.llm.tools}
    assert asked and tools["get_football"]["description"].endswith(" The user follows: Stevenage.")


def test_a_sessions_actions_read_the_brains_calendar(brain, make_session):
    # One Google calendar for the brain, rather than one for each satellite - and its
    # activities are what the local rules hear.
    words = {"gym": "gym", "gyms": "gym"}
    brain.calendar = SimpleNamespace(activity_words=lambda: words, activity_calendars=dict)
    session = make_session()
    assert session.actions.calendar is brain.calendar and session.intents.activities() is words


def test_a_sessions_local_rules_leave_football_to_claude(brain, make_session):
    # Every name of the teams the brain follows - not only the short ones its tools are told - so that
    # "is Nottingham Forest playing?" isn't taken for a media player.
    known_as = ["Nott'm Forest", "Nottingham Forest"]
    brain.football = SimpleNamespace(names=lambda: ["Nott'm Forest"], known_as=lambda: known_as)
    session = make_session()
    assert session.intents.football() == known_as
    assert session.intents.match("Is Nottingham Forest playing?") is None


def test_a_session_without_football_has_no_teams_for_its_local_rules(brain, make_session):
    assert make_session().intents.football is None


def test_a_sessions_actions_read_listening_stats_from_the_brain(brain, make_session):
    # One for the brain, so every satellite answers from the same snapshot of SoulSync's stats.
    assert "get_listening" not in {tool["name"] for tool in make_session().llm.tools}
    brain.listening = object()
    session = make_session()
    assert session.actions.listening is brain.listening
    assert "get_listening" in {tool["name"] for tool in session.llm.tools}


def test_a_sessions_actions_download_through_the_brain(brain, make_session):
    # One for the brain, so the same thing asked for on two satellites is requested once.
    brain.downloads = object()
    assert make_session().actions.downloads is brain.downloads


def test_a_sessions_actions_read_the_settings_as_they_are_then(brain, make_session):
    # So "find me something new" plays the playlist the settings page names at the time.
    assert make_session().actions.config is brain.config


def test_a_sessions_claude_records_into_the_brains_usage_log(brain, make_session):
    # One log for the brain, so what every satellite's calls cost is added up together.
    brain.usage = object()
    assert make_session().llm.usage is brain.usage


def test_a_sessions_local_rules_know_its_wake_word(brain, make_session):
    # So "Jarvispause." is heard as "pause" by a satellite woken by "Hey Jarvis".
    brain.config.data["assistant"]["wake_word"] = "Hey Jarvis"
    session = make_session()
    assert session.intents.match("Jarvispause.")["actions"][0].tool == "control_playback"


def _stand_in_brain(session):
    """
    A brain with only what saying a battery warning or an alert needs: this real session as the
    default satellite's, and the brain's own way of announcing on it, bound to the stand-in so
    that the tests go through the real thing and not a copy of it.
    """
    brain = SimpleNamespace(session_for=lambda satellite: session)
    brain._announce_on_default = MethodType(Brain._announce_on_default, brain)
    return brain


def _the_brain_says(session, message):
    """
    What Brain._say_battery and Brain._alert return with this real session as the default
    satellite's - they need nothing else of a brain than that and its own way of
    announcing, which _stand_in_brain gives the stand-in.
    """
    from voice_assistant.brain import Brain
    from voice_assistant.ntfy import Alert

    brain = _stand_in_brain(session)
    return Brain._say_battery(brain, message), Brain._alert(brain, Alert("homelab", message, ""))


def test_an_announcement_says_whether_it_was_said_to_the_end(connect, make_session):
    session = make_session(connect()[0])
    assert session.announce("Your phone's battery is down to 5%.") is True


def test_an_announcement_whose_playback_failed_says_it_wasnt_said(connect, make_session):
    session = make_session(connect()[0])

    def fail(*args, **kwargs):
        raise OSError("couldn't play it")

    session.speaker.play_stream = fail
    assert session.announce("Your phone's battery is down to 5%.") is False
    # So a battery warning is held for later, and an alert is kept for "any notifications?".
    assert _the_brain_says(session, "Your phone's battery is down to 5%.") == (False, False)


def test_an_announcement_cut_off_by_the_satellite_going_says_it_wasnt_said(connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    # Held playing, as a satellite part way through a sentence would be.
    pi.held = {1}
    results = []
    saying = threading.Thread(target=lambda: results.append(
        (session.announce("Your phone's battery is down to 5%."),)), daemon=True)
    saying.start()
    assert wait_for(lambda: pi.messages("stream_end"))
    pi.go()
    saying.join(10)
    assert results == [(False,)]
    assert _the_brain_says(session, "Again.") == (False, False)


def test_an_overdue_chime_doesnt_lose_the_words_and_an_overdue_speech_counts_as_said(
        connect, make_session, monkeypatch):
    # A satellite which never says it has finished: each sound is given up on after the slack, and
    # the words after the chime are still said - once. Said once, a battery warning isn't held and retried.
    monkeypatch.setattr(session_module, "PLAYBACK_SLACK", 0.1)
    satellite, pi = connect()
    pi.finishes_nothing = True
    session = make_session(satellite)
    assert session.announce("Your reminder: take the bins out.") is True
    assert len(pi.messages("play_sound")) == 1 and len(pi.messages("play_stream")) == 1
    from voice_assistant.brain import Brain
    brain = _stand_in_brain(session)
    assert Brain._say_battery(brain, "Your phone's battery is down to 5%.") is True
    assert len(pi.messages("play_stream")) == 2


def test_a_stop_during_a_battery_warning_still_counts_as_said(connect, make_session):
    # Home Assistant's Stop, like the wake word, is the user hearing it and silencing it - saying it
    # again would only nag.
    satellite, pi = connect()
    session = make_session(satellite)
    pi.held = {1}
    from voice_assistant.brain import Brain
    brain = _stand_in_brain(session)
    results = []
    saying = threading.Thread(target=lambda: results.append(
        Brain._say_battery(brain, "Your phone's battery is down to 5%.")), daemon=True)
    saying.start()
    assert wait_for(lambda: pi.messages("stream_end"))
    session.stop_what_rings_and_speaks()
    saying.join(10)
    assert results == [True]


class _Talking(ScriptedConversation):
    """A conversation the user is still in, until the test lets it go."""

    def __init__(self):
        super().__init__()
        self.talking, self.let_go = threading.Event(), threading.Event()

    def handle(self, text, follow_up=False, timeline=None):
        self.talking.set()
        self.let_go.wait(5)
        return Reply()


def _sound_then(session, then):
    """Has the session's speaker do then as soon as an announcement's sound starts - once."""
    play_file = session.speaker.play_file
    done = []

    def playing(*args, **kwargs):
        playback = play_file(*args, **kwargs)
        if kwargs.get("kind") == "announcement" and not done:
            done.append(True)
            then()
        return playback

    session.speaker.play_file = playing


def test_a_stop_during_a_warnings_sound_stops_its_words_too(connect, make_session):
    # Only the sound stopped, and the words came straight after. The user heard it start
    # and stopped it, so it counts as said, as stopped part way through its words.
    satellite, pi = connect()
    session = make_session(satellite)
    _sound_then(session, session.stop_what_rings_and_speaks)
    assert session.announce("Your phone's battery is down to 5%.") is True
    assert pi.messages("play_stream") == []


def test_the_wake_word_during_an_alerts_sound_leaves_its_words_for_after_the_conversation(brain, connect,
                                                                                          make_session):
    # As a reminder's are. The user may well be asking about something else, and words
    # skipped and counted as said were lost for good: an alert wasn't kept for "any
    # notifications?", nor a battery warning sent to the phone.
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = _Talking()
    brain.stt.texts = ["What's the weather?"]
    running(session)

    def woken():
        pi.wake(said())
        assert session.conversation.talking.wait(5)

    _sound_then(session, woken)
    results = []
    saying = threading.Thread(target=lambda: results.append(session.announce("Homelab: the backup failed.")),
                              daemon=True)
    saying.start()
    assert session.conversation.talking.wait(5)
    session.conversation.let_go.set()
    saying.join(10)
    assert results == [True]
    assert [message["kind"] for message in pi.messages("play_stream")] == ["announcement"]

    # Only a Stop during its sound stops them.
    _sound_then(session, session.stop_what_rings_and_speaks)
    assert session.announce("Homelab: the backup failed again.") is True
    assert [message["kind"] for message in pi.messages("play_stream")] == ["announcement"]


def test_a_stop_in_the_conversation_a_wake_word_over_an_alerts_sound_started_is_that_conversations(
        brain, connect, make_session):
    # Not the alert's: its words waited for the conversation, and a Stop in it - of the
    # reply, say - dropped them, though only a Stop during its sound is meant to.
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = _Talking()
    brain.stt.texts = ["What's the weather?"]
    running(session)

    def woken():
        pi.wake(said())
        assert session.conversation.talking.wait(5)

    _sound_then(session, woken)
    turns = []
    wait_for_turn = session._wait_for_turn

    def waiting_for_turn(still_wanted=None, gap=None):
        turns.append(True)
        return wait_for_turn(still_wanted, gap)

    session._wait_for_turn = waiting_for_turn
    results = []
    saying = threading.Thread(target=lambda: results.append(session.announce("Homelab: the backup failed.")),
                              daemon=True)
    saying.start()
    # Its sound has played, and its words wait for the conversation.
    assert wait_for(lambda: len(turns) == 2)
    session.stop_what_rings_and_speaks()
    session.conversation.let_go.set()
    saying.join(10)
    assert results == [True]
    assert [message["kind"] for message in pi.messages("play_stream")] == ["announcement"]


def test_an_alert_waiting_on_a_conversation_is_said_after_it_whatever_was_stopped_in_it(brain, connect,
                                                                                      make_session):
    # Only a Stop once its sound has started is the user stopping it.
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = _Talking()
    brain.stt.texts = ["What's the weather?"]
    running(session)
    pi.wake(said())
    assert session.conversation.talking.wait(5)
    results = []
    saying = threading.Thread(target=lambda: results.append(session.announce("Homelab: the backup failed.")),
                              daemon=True)
    saying.start()
    time.sleep(0.1)
    session.stop_what_rings_and_speaks()
    session.conversation.let_go.set()
    saying.join(10)
    assert results == [True]
    assert [message["kind"] for message in pi.messages("play_stream")] == ["announcement"]


def test_the_wake_word_during_what_the_clock_rings_leaves_its_words_to_the_clock(brain, connect, make_session):
    # A reminder still ringing once the conversation is over carries on - it's dismissed
    # by the conversation, or Stop, through still_wanted.
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = _Talking()
    brain.stt.texts = ["What's the weather?"]
    running(session)

    def woken():
        pi.wake(said())
        assert session.conversation.talking.wait(5)

    _sound_then(session, woken)
    results = []
    saying = threading.Thread(target=lambda: results.append(session.announce(
        "Your reminder: take the bins out.", still_wanted=lambda: True, ringing=True)), daemon=True)
    saying.start()
    assert session.conversation.talking.wait(5)
    session.conversation.let_go.set()
    saying.join(10)
    assert results == [True]
    assert [message["kind"] for message in pi.messages("play_stream")] == ["announcement"]


class _OutlastedWait(threading.Event):
    """
    A conversation going on longer than an announcement waits for one - which runs out
    at once here, rather than after a minute.
    """

    def wait(self, timeout=None):
        return self.is_set()


def test_a_warning_refused_over_a_long_conversation_isnt_counted_as_said(connect, make_session):
    # It went ahead once the wait ran out, while the satellite still had announcements
    # silenced, so its words were refused - and counted as said, so a battery warning
    # was neither said again nor held, and an alert wasn't kept.
    satellite, pi = connect()
    session = make_session(satellite)
    session._idle = _OutlastedWait()
    pi.refusing = True
    assert _the_brain_says(session, "Your phone's battery is down to 5%.") == (False, False)
    assert [message["kind"] for message in pi.messages("play_stream")] == ["announcement", "announcement"]


def test_a_warning_over_a_long_conversation_the_satellite_took_counts_as_said(connect, make_session):
    # The conversation ended just as it went ahead, so the satellite had announcements back.
    satellite, pi = connect()
    session = make_session(satellite)
    session._idle = _OutlastedWait()
    assert _the_brain_says(session, "Your phone's battery is down to 5%.") == (True, True)


# Settings saved while a session is already running

def test_a_pressed_wake_plays_no_listening_sound(brain, connect, make_session):
    # The satellite chimes on the press itself, at once - not after the round trip here.
    satellite, pi = connect()
    session = make_session(satellite)
    brain.stt.texts = ["What time is it?"]
    thread = running(session)

    pi.wake(said(), pressed=True)
    assert wait_for(lambda: brain.tts.said)
    assert pi.messages("play_sound") == []
    session.close()
    thread.join(3)


def test_a_pressed_wake_with_nothing_said_says_nothing(brain, connect, make_session):
    # Pressed and let go with nothing said is the user changing their mind, not a false wake word.
    satellite, pi = connect()
    session = make_session(satellite)
    feed = satellite.listen()
    feed.feed(np.zeros(4 * RATE, np.int16))

    assert session._handle_interaction(feed, timeline=FakeTimeline(), pressed=True) is None
    assert pi.messages("play_sound") == [] and brain.tts.said == []
    assert brain.stt.made[0].cancelled


def test_an_answer_ended_on_a_press_with_nothing_said_says_nothing(brain, connect, make_session):
    # The user changing their mind, as with a press which began a recording.
    satellite, pi = connect()
    session = make_session(satellite)
    feed = satellite.listen()
    feed.feed(np.zeros(RATE // 2, np.int16))
    pi.say("audio_end", reason="pressed")
    assert wait_for(lambda: feed.ended)

    assert session._handle_interaction(feed, follow_up=True, timeline=FakeTimeline()) is None
    assert [message["name"] for message in pi.messages("play_sound")] == ["listening.wav"]
    assert brain.tts.said == []


def test_a_pressed_wake_is_handed_to_the_interaction(make_session, monkeypatch):
    session = make_session()
    seen = []

    def interaction(feed, follow_up=False, timeline=None, pre_roll=None, pressed=False):
        seen.append(pressed)

    monkeypatch.setattr(session, "_handle_interaction", interaction)
    session._answer({"feed": AudioFeed(), "pressed": True})
    session._answer({"feed": AudioFeed()})
    assert seen == [True, False]


def test_a_pressed_wake_starts_a_pressed_conversation(make_session, monkeypatch):
    # So what's said at a press is never dropped as speech from the TV - see Conversation.start.
    session = make_session()
    started = []
    session.conversation = SimpleNamespace(
        start=lambda ringing=None, talked_over=False, pressed=False: started.append(pressed))
    monkeypatch.setattr(session, "_handle_interaction", lambda feed, **kwargs: None)
    session._answer({"feed": AudioFeed(), "pressed": True})
    session._answer({"feed": AudioFeed()})
    assert started == [True, False]


def test_the_listening_blip_is_switched_off_straight_away(brain, connect, make_session):
    # Made before the save, as every satellite's session is when the settings page is used.
    satellite, pi = connect()
    session = make_session(satellite)
    brain.config.apply({("sounds", "listening_enabled"): False})
    feed = AudioFeed()
    feed.feed(np.zeros(4 * RATE, np.int16))

    session._handle_interaction(feed, timeline=FakeTimeline())
    # Neither the blip nor its falling twin for not hearing anything.
    assert pi.messages("play_sound") == []


def test_the_done_chime_is_switched_off_straight_away(brain, connect, make_session, monkeypatch):
    session = make_session(connect()[0])
    # Something ringing which "stop" dismisses, confirmed by the chime.
    monkeypatch.setattr(session.conversation.clock, "is_ringing", lambda satellite=None: True)
    monkeypatch.setattr(session.conversation.clock, "dismiss", lambda satellite=None: {"message": "Okay, stopped."})
    assert session.conversation.handle("stop").sounds == [("success_chime.wav", "general")]

    brain.config.apply({("sounds", "chime_enabled"): False})
    assert session.conversation.handle("stop").sounds == []


def test_the_reminder_sound_is_switched_off_straight_away(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    brain.config.apply({("sounds", "reminder_enabled"): False})

    assert session.announce("Your reminder: take the bins out.") is True
    assert pi.messages("play_sound") == [] and len(pi.messages("play_stream")) == 1


@pytest.mark.parametrize("sound, start", [("alarm_sound.wav", 4.0), ("timer_beep.wav", 0.0),
                                           ("announcement_sound.wav", 0.0)],
                         ids=["an alarm's later burst", "a timer's beep", "a labelled timer's reminder"])
def test_the_reminder_sound_switch_never_silences_what_the_clock_rings(brain, connect, make_session, sound, start):
    # A wake-up alarm which rang silently after its first line would be a missed wake-up.
    satellite, pi = connect()
    session = make_session(satellite)
    brain.config.apply({("sounds", "reminder_enabled"): False})

    session.announce(None, sound, boost=True, start=start, length=2.0, ringing=True)
    assert [message["name"] for message in pi.messages("play_sound")] == [sound]


# The device spoken through

def test_the_device_is_said_with_its_kind_and_the_others_in_its_room(brain, make_session, connect):
    session = make_session(connect(name="pc")[0])
    brain.spoken_name = {"pc": "Nathaniel's PC", "home-assistant": "the bedroom Pi"}.get
    brain.rooms.seen("pc", True)
    brain.room_of = lambda name: ["pc", "home-assistant"]
    assert session._here() == "You're speaking through Nathaniel's PC (push-to-talk). Also in this room: the bedroom Pi."


def test_a_wake_word_satellite_alone_is_said_so(brain, make_session, connect):
    session = make_session(connect(name="home-assistant")[0])
    brain.spoken_name = {"home-assistant": "the bedroom Pi"}.get
    assert session._here() == "You're speaking through the bedroom Pi (wake word)."


def test_something_typed_is_said_to_have_been_typed(brain, make_session, connect):
    session = make_session(connect(name="home-assistant")[0])
    brain.spoken_name = {"home-assistant": "the bedroom Pi"}.get
    session._typing = True
    assert session._here() == ("The user typed this in Home Assistant's Ask Nova box, "
                               "and the reply is said through the bedroom Pi (wake word).")


def test_the_device_failing_to_be_worked_out_costs_only_the_sentence(brain, make_session, connect):
    session = make_session(connect(name="pc")[0])

    def broken(name):
        raise RuntimeError("no rooms")
    brain.room_of = broken
    assert session._here() is None


# Starting and ending an open conversation, by the brain's one OpenConversation

def test_a_session_starts_and_ends_a_conversation_at_its_satellite_by_voice(brain, make_session):
    brain.open_conversation = OpenConversation(lambda: {"enabled": True})
    session = make_session()
    here = session.satellite.name
    # Said, with no chime: the session's own sound for it comes later.
    reply = session.conversation.handle("Let's talk.")
    assert (reply.say, reply.sounds) == ("Go ahead.", [])
    assert brain.open_conversation.on_at(here)
    reply = session.conversation.handle("That's all.")
    assert (reply.say, reply.sounds) == ("Okay.", [])
    assert not brain.open_conversation.on_at(here) and brain.open_conversation.satellite is None


def test_a_sessions_language_model_is_told_of_the_conversation_and_its_satellite(brain, make_session):
    brain.open_conversation = OpenConversation(lambda: {"enabled": True})
    session = make_session()
    assert session.llm.open_conversation is brain.open_conversation
    assert session.llm.satellite == session.satellite.name


def test_a_session_with_no_conversations_leaves_the_phrases_to_claude(make_session, monkeypatch):
    # The fixture's brain has none, as a test's executor has none.
    session = make_session()
    asked = []
    monkeypatch.setattr(session.llm, "process_query", lambda text, **kwargs: asked.append(text) or {
        "response": "Sorry.", "actions": [], "success": True})
    assert session.conversation.handle("Let's talk.").say == "Sorry."
    assert asked == ["Let's talk."]


# An open conversation, turn after turn

def _in_a_conversation(brain, connect, make_session, *replies, ended=None, **settings):
    """
    A session at a satellite which knows conversations, answering with the replies given, with one
    already on there - as "let's talk" or Home Assistant's switch leaves it.
    """
    brain.open_conversation = OpenConversation(lambda: {"enabled": True, **settings}, ended=ended)
    satellite, pi = connect(conversations=True)
    session = make_session(satellite)
    session.conversation = ScriptedConversation(*replies)
    assert brain.open_conversation.start(satellite.name) == "started"
    return session, pi


def _ended(session):
    """Ends the conversation as Home Assistant's switch does, once the session has finished with it."""
    session.end_conversation("switch")
    assert wait_for(lambda: session._idle.is_set())


def _sending_quiet(pi):
    """Has the satellite send quiet without end, as its open microphone does, until it goes."""
    def send():
        try:
            while True:
                pi.send_audio(np.zeros(800, np.int16))
                time.sleep(0.05)
        except (OSError, ValueError):
            pass
    threading.Thread(target=send, daemon=True).start()


def _sounds(pi):
    return [message["name"] for message in pi.messages("play_sound")]


def test_a_reply_in_a_conversation_is_followed_by_listening(brain, connect, make_session):
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Today, you've got..."))
    brain.stt.texts = ["What's on today?"]
    running(session)

    pi.wake(said())
    # Listened for straight after, with no wake word: an open listen, as long as he might take.
    assert wait_for(lambda: pi.messages("listen"))
    assert pi.messages("listen")[0].get("open") is True
    assert brain.tts.said == ["Today, you've got..."] and not session._idle.is_set()
    _ended(session)


def test_outside_a_conversation_a_reply_isnt_followed_by_listening(brain, connect, make_session):
    # Conversations switched on, at a satellite which knows them - but the one on is elsewhere.
    brain.open_conversation = OpenConversation(lambda: {"enabled": True})
    brain.open_conversation.start("kitchen")
    satellite, pi = connect(conversations=True)
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="Which room?", listen_again=True), Reply(say="Okay."))
    brain.stt.texts = ["Turn the lights on.", "The kitchen."]
    pi.answers = [said(before=0)]
    running(session)

    pi.wake(said())
    assert wait_for(lambda: pi.messages("allow") and session._idle.is_set())
    # Only a question's answer is listened for, as ever: an ordinary listen, after the listening sound.
    assert [message.get("open") for message in pi.messages("listen")] == [None]
    assert session.conversation.heard == [("Turn the lights on.", False), ("The kitchen.", True)]
    assert _sounds(pi) == ["listening.wav", "listening.wav"] and pi.messages("conversation") == []


def test_more_than_three_turns(brain, connect, make_session):
    # No limit on turns, nor on questions in a row - and the turn after a question is its answer.
    replies = [Reply(say="One?", listen_again=True), Reply(say="Two."), Reply(say="Three?", listen_again=True),
               Reply(say="Four?", listen_again=True), Reply(say="Five?", listen_again=True), Reply(say="Six.")]
    session, pi = _in_a_conversation(brain, connect, make_session, *replies)
    brain.stt.texts = ["First.", "Second.", "Third.", "Fourth.", "Fifth.", "Sixth."]
    pi.answers = [said(before=0, after=2) for _ in range(5)]
    # The last reply plays on, so no turn after it has begun.
    pi.held = {6}
    running(session)

    pi.wake(said())
    assert wait_for(lambda: len(brain.tts.said) == 6)
    assert session.conversation.heard == [("First.", False), ("Second.", True), ("Third.", False),
                                          ("Fourth.", True), ("Fifth.", True), ("Sixth.", True)]
    # Each turn not after a question starts afresh, as a wake word does.
    assert len(session.conversation.started) == 2
    assert all(message.get("open") for message in pi.messages("listen"))
    pi.say("played", id=pi.messages("play_stream")[5]["id"])
    _ended(session)


def test_a_turn_uses_the_conversations_pause_and_longest_turn(brain, connect, make_session, monkeypatch):
    given = []
    real = session_module.record_until_silence

    def recording(*args, **kwargs):
        given.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(session_module, "record_until_silence", recording)
    brain.config.data["thresholds"] = {"press_no_words_seconds": 1.0}
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Okay."), Reply(say="Right."))
    brain.stt.texts = ["Hello.", "And again."]
    pi.answers = [said(before=0, after=2)]
    # The second reply plays on until the test lets it finish.
    pi.held = {2}
    running(session)

    pi.wake(said())
    assert wait_for(lambda: len(pi.messages("play_stream")) == 2)
    ordinary, turn = given
    assert (ordinary["silence_duration"], ordinary["timeout"], ordinary.get("open_wait")) == (0.6, 15.0, None)
    assert ordinary["no_words_after"] == 1.0
    # His pause, his longest turn and the press rules by the conversation's settings, nothing from
    # before the turn, and as long to start as the quiet spell has left.
    assert (turn["silence_duration"], turn["timeout"], turn["no_words_after"], turn["keep_before"]) == (1.5, 90, 1.5, 0)
    assert 290 < turn["open_wait"] <= 300

    # Read as each turn starts, so a change on the settings page is used from the next.
    brain.config.data["conversation"] = {"pause_seconds": 2.5, "longest_turn": 120}
    pi.say("played", id=pi.messages("play_stream")[1]["id"])
    assert wait_for(lambda: len(given) == 3)
    assert (given[2]["silence_duration"], given[2]["timeout"]) == (2.5, 120)
    _ended(session)


def test_no_listening_sound_between_turns(brain, connect, make_session):
    session, pi = _in_a_conversation(brain, connect, make_session, *[Reply(say="Okay.")] * 4)
    brain.stt.texts = ["One.", "Two.", "Three.", "Four."]
    pi.answers = [said(before=0, after=2) for _ in range(3)]
    running(session)

    pi.wake(said())
    assert wait_for(lambda: len(brain.tts.said) == 4 and len(pi.messages("listen")) == 4)
    # The wake word's, and the conversation's own once as its turns begin - none between them.
    assert _sounds(pi) == ["listening.wav", "listening.wav"]
    _ended(session)
    assert _sounds(pi) == ["listening.wav", "listening.wav", "not_heard.wav"]


def test_starting_plays_it_once_and_says_go_ahead(brain, connect, make_session):
    brain.open_conversation = OpenConversation(lambda: {"enabled": True})
    satellite, pi = connect(conversations=True)
    session = make_session(satellite)
    brain.stt.texts = ["Let's talk.", "What time is it?", "What time is it?"]
    pi.answers = [said(before=0, after=2), said(before=0, after=2)]
    running(session)

    pi.wake(said())
    assert wait_for(lambda: len(brain.tts.said) == 3)
    # Said once, by the start itself, and the satellite told before its first open listen, which the
    # listening sound follows - once.
    assert brain.tts.said[0] == "Go ahead." and brain.tts.said.count("Go ahead.") == 1
    assert [message["on"] for message in pi.messages("conversation")] == [True]
    assert _sounds(pi) == ["listening.wav", "listening.wav"]
    kinds = [message["type"] for message, _ in pi.received if message["type"] in ("conversation", "listen", "play_sound")]
    assert kinds[:4] == ["play_sound", "conversation", "listen", "play_sound"]
    _ended(session)
    assert [message["on"] for message in pi.messages("conversation")] == [True, False]


def test_silence_and_noise_between_turns_say_nothing(brain, connect, make_session):
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Okay."), Reply(say="Right."))
    # Quiet, until the satellite's sending is given up on; a cough heard as nothing; words imagined in
    # noise; and then something said.
    brain.stt.texts = ["Hello.", "", "", FakeTranscription("", imagined=True), "Again."]
    pi.answers = [np.zeros(RATE, np.int16)] + [said(before=0, after=2) for _ in range(3)]
    running(session)

    pi.wake(said())
    assert wait_for(lambda: len(brain.tts.said) == 2, seconds=10)
    assert brain.tts.said == ["Okay.", "Right."]
    assert session.conversation.heard == [("Hello.", False), ("Again.", False)]
    # No falling sound, and the next turn listens.
    assert _sounds(pi) == ["listening.wav", "listening.wav"]
    assert wait_for(lambda: len(pi.messages("listen")) == 5)
    assert brain.open_conversation.on_at(session.satellite.name)
    _ended(session)


def test_a_command_mid_conversation_runs_locally(brain, connect, make_session, monkeypatch):
    brain.open_conversation = OpenConversation(lambda: {"enabled": True})
    satellite, pi = connect(conversations=True)
    session = make_session(satellite)
    asked = []
    monkeypatch.setattr(session.llm, "process_query", lambda text, **kwargs: asked.append(text) or {
        "response": "Sure.", "actions": [], "success": True})
    brain.stt.texts = ["Let's talk.", "Pause the music."]
    pi.answers = [said(before=0, after=2)]
    running(session)

    pi.wake(said())
    # Heard as a fresh request, so the everyday command is Nova's own, not Claude's.
    assert wait_for(lambda: len(pi.messages("listen")) == 2)
    assert brain.stt.made[1].finished_with is not None
    assert asked == []
    _ended(session)


def test_a_turn_after_a_question_is_its_answer(brain, connect, make_session):
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Which room?", listen_again=True),
                                     Reply(say="The kitchen lights are on."))
    brain.stt.texts = ["Turn the lights on.", "The kitchen."]
    pi.answers = [said(before=0, after=2)]
    # The answer's reply plays on, so no turn after it has begun.
    pi.held = {2}
    running(session)

    pi.wake(said())
    assert wait_for(lambda: len(brain.tts.said) == 2)
    # Its answer - with the conversation not started again for it, so a yes to it confirms.
    assert session.conversation.heard == [("Turn the lights on.", False), ("The kitchen.", True)]
    assert session.conversation.started == [False]
    assert pi.messages("listen")[0].get("open") is True
    pi.say("played", id=pi.messages("play_stream")[1]["id"])
    _ended(session)


def test_the_quiet_spell_ends_it_and_says_so(brain, connect, make_session):
    # A quiet spell of a little over a second.
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Okay."), idle_minutes=0.02)
    brain.stt.texts = ["Hello."]
    _sending_quiet(pi)
    running(session)

    pi.wake(said())
    assert wait_for(lambda: len(brain.tts.said) == 2 and session._idle.is_set(), seconds=10)
    assert brain.tts.said == ["Okay.", "I'll leave it there."]
    assert [message["on"] for message in pi.messages("conversation")] == [True, False]
    assert _sounds(pi) == ["listening.wav", "listening.wav", "not_heard.wav"]
    assert brain.open_conversation.satellite is None


def test_three_turns_not_for_nova_end_it(brain, connect, make_session):
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Okay."),
                                     *[Reply(not_for_me=True)] * 3)
    brain.stt.texts = ["Hello.", "A line of a film.", "Another.", "And another."]
    pi.answers = [said(before=0, after=2) for _ in range(3)]
    running(session)

    pi.wake(said())
    assert wait_for(lambda: len(brain.tts.said) == 2 and session._idle.is_set())
    assert brain.tts.said == ["Okay.", "I'll leave it there."]
    assert len(session.conversation.heard) == 4
    assert [message["on"] for message in pi.messages("conversation")] == [True, False]
    assert _sounds(pi)[-1] == "not_heard.wav"
    assert brain.open_conversation.satellite is None


def test_an_end_phrase_ends_it_with_okay(brain, connect, make_session):
    brain.open_conversation = OpenConversation(lambda: {"enabled": True})
    satellite, pi = connect(conversations=True)
    session = make_session(satellite)
    brain.stt.texts = ["Let's talk.", "That's all."]
    pi.answers = [said(before=0, after=2)]
    running(session)

    pi.wake(said())
    assert wait_for(lambda: len(brain.tts.said) == 2 and session._idle.is_set())
    # "Okay." from the end itself, and nothing more said: he asked.
    assert brain.tts.said == ["Go ahead.", "Okay."]
    assert [message["on"] for message in pi.messages("conversation")] == [True, False]
    assert _sounds(pi) == ["listening.wav", "listening.wav", "not_heard.wav"]
    assert len(pi.messages("listen")) == 1 and brain.open_conversation.satellite is None


def test_a_conversation_whose_satellite_goes_ends_quietly(brain, connect, make_session, caplog):
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Okay."))
    brain.stt.texts = ["Hello."]
    thread = running(session)

    pi.wake(said())
    assert wait_for(lambda: pi.messages("listen"))
    pi.go()
    thread.join(5)

    # Nobody's there to be told anything, and nothing is left on.
    assert not thread.is_alive()
    assert brain.tts.said == ["Okay."] and brain.open_conversation.satellite is None
    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []


def test_a_turn_which_goes_wrong_ends_the_conversation_and_says_so(brain, connect, make_session, monkeypatch):
    # Rather than every turn after it going wrong the same way, one straight after another.
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Okay."))
    brain.stt.texts = ["Hello."]
    listen = session.satellite.listen

    def broken(open=False):
        if open:
            raise RuntimeError("no microphone")
        return listen()

    monkeypatch.setattr(session.satellite, "listen", broken)
    running(session)

    pi.wake(said())
    assert wait_for(lambda: len(brain.tts.said) == 3 and session._idle.is_set())
    assert brain.tts.said == ["Okay.", "Sorry, something went wrong there.", "I'll leave it there."]
    assert [message["on"] for message in pi.messages("conversation")] == [True, False]
    assert _sounds(pi)[-1] == "not_heard.wav" and brain.open_conversation.satellite is None


def test_ending_from_another_thread_stops_the_listen(brain, connect, make_session):
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Okay."))
    brain.stt.texts = ["Hello."]
    _sending_quiet(pi)
    running(session)

    pi.wake(said())
    assert wait_for(lambda: pi.messages("listen"))
    time.sleep(0.2)
    session.end_conversation("switch")
    # The listen is stopped there and then, and the turns fall out - with the falling sound, and
    # nothing said, as it was his doing.
    assert wait_for(lambda: session._idle.is_set())
    recording = pi.messages("listen")[0]["recording"]
    assert any(message.get("recording") == recording for message in pi.messages("stop_listening"))
    assert brain.tts.said == ["Okay."] and len(pi.messages("listen")) == 1
    assert [message["on"] for message in pi.messages("conversation")] == [True, False]
    assert _sounds(pi)[-1] == "not_heard.wav"
    # One on at another satellite isn't this session's to end.
    brain.open_conversation.start("kitchen")
    session.end_conversation("switch")
    assert brain.open_conversation.on_at("kitchen")


def test_ended_while_nova_speaks_its_reply_finishes_and_nothing_more_is_listened_for(brain, connect,
                                                                                        make_session):
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Okay."),
                                     Reply(say="Today, you've got..."))
    brain.stt.texts = ["Hello.", "What's on today?"]
    pi.answers = [said(before=0, after=2)]
    pi.held = {2}
    running(session)

    pi.wake(said())
    assert wait_for(lambda: len(pi.messages("play_stream")) == 2)
    session.end_conversation("switch")
    time.sleep(0.2)
    reply = pi.messages("play_stream")[1]["id"]
    assert not any(message.get("id") == reply for message in pi.messages("stop"))
    pi.say("played", id=reply)
    assert wait_for(lambda: session._idle.is_set())
    assert len(pi.messages("listen")) == 1
    assert [message["on"] for message in pi.messages("conversation")] == [True, False]


def test_ended_is_given_every_exchange(brain, connect, make_session):
    given = []
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="One."), Reply(not_for_me=True),
                                     Reply(say="Three.", claude_answered=True), ended=given.append)
    brain.stt.texts = ["First.", "Second.", "Third."]
    pi.answers = [said(before=0, after=2), said(before=0, after=2)]
    running(session)

    pi.wake(said())
    assert wait_for(lambda: len(pi.messages("listen")) == 3)
    _ended(session)
    # Each turn Nova answered, and by what - not the one taken for speech from what's playing.
    assert [(exchange["user"], exchange["assistant"], exchange["model"]) for exchange in given[0]] == [
        ("First.", "One.", "local"), ("Third.", "Three.", session.llm.model_name)]


def test_something_typed_mid_conversation_is_a_turn(brain, connect, make_session):
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Okay."),
                                     Reply(say="Lunch, then the gym."), Reply(say="Then tea."))
    brain.stt.texts = ["Hello."]
    _sending_quiet(pi)
    pi.held = {2}
    running(session)

    pi.wake(said())
    assert wait_for(lambda: pi.messages("listen"))
    # Typed while it waits: the listen is ended for it, and it's the next turn.
    session.ask("What's next?")
    assert wait_for(lambda: len(pi.messages("play_stream")) == 2)
    recording = pi.messages("listen")[0]["recording"]
    assert any(message.get("recording") == recording for message in pi.messages("stop_listening"))
    # Typed while a reply is said: the turn after it.
    session.ask("And then?")
    pi.say("played", id=pi.messages("play_stream")[1]["id"])
    assert wait_for(lambda: len(brain.tts.said) == 3)
    assert session.conversation.heard == [("Hello.", False), ("What's next?", False), ("And then?", False)]
    # And it carries on.
    assert wait_for(lambda: len(pi.messages("listen")) == 2)
    assert brain.open_conversation.on_at(session.satellite.name)
    _ended(session)


def test_a_session_waiting_for_a_turn_is_still_moving(brain, connect, make_session):
    # Waiting for him to start can go on for the whole quiet spell, far past the watchdog's allowance
    # for a session which is still - see Brain.last_moved.
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Okay."))
    brain.stt.texts = ["Hello."]
    _sending_quiet(pi)
    running(session)

    pi.wake(said())
    assert wait_for(lambda: pi.messages("listen"))
    time.sleep(1.5)
    assert session.activity == "listening" and time.monotonic() - session.moved_at < 0.5
    _ended(session)


# Home Assistant's Conversation switch

def _a_session_with_conversations(brain, connect, make_session, **settings):
    """A session at a satellite which knows conversations, with none on, and the real conversation answering it."""
    brain.open_conversation = OpenConversation(lambda: {"enabled": True, **settings})
    satellite, pi = connect(conversations=True)
    return make_session(satellite), pi


def test_turning_it_on_starts_one_and_listens(brain, connect, make_session, monkeypatch):
    session, pi = _a_session_with_conversations(brain, connect, make_session)
    here = session.satellite.name
    # What the model's history is, as the conversation is begun: the stale is forgotten as at any wake, which
    # does nothing once a conversation is on.
    on_when_forgotten = []
    monkeypatch.setattr(session.llm, "forget_stale_history",
                        lambda: on_when_forgotten.append(brain.open_conversation.on_at(here)))
    _sending_quiet(pi)
    running(session)

    session._device_controls().set_conversation(True)

    assert wait_for(lambda: pi.messages("listen"))
    # The same words as the phrase, said here, then the open listen and its listening sound, as after "let's talk".
    assert brain.tts.said == ["Go ahead."] and _sounds(pi) == ["listening.wav"]
    assert pi.messages("listen")[0].get("open") is True
    kinds = [message["type"] for message, _ in pi.received if message["type"] in ("play_stream", "play_sound", "listen")]
    assert kinds == ["play_stream", "listen", "play_sound"]
    assert brain.open_conversation.on_at(here) and session._device_controls().in_conversation()
    assert [message["on"] for message in pi.messages("conversation")] == [True]
    # Before it was on, as for the phrase said - the open turn's own start after is one for a conversation which
    # is on, and forgets nothing.
    assert on_when_forgotten[0] is False
    _ended(session)


def test_turning_it_off_ends_it_with_the_falling_sound_and_nothing_said(brain, connect, make_session):
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Okay."))
    brain.stt.texts = ["Hello."]
    _sending_quiet(pi)
    running(session)
    pi.wake(said())
    assert wait_for(lambda: pi.messages("listen"))
    assert session._device_controls().in_conversation()

    session._device_controls().set_conversation(False)

    assert wait_for(lambda: session._idle.is_set())
    assert not session._device_controls().in_conversation()
    recording = pi.messages("listen")[0]["recording"]
    assert any(message.get("recording") == recording for message in pi.messages("stop_listening"))
    # Nothing but his reply to "Hello." said: it was his doing, so no "I'll leave it there.".
    assert brain.tts.said == ["Okay."] and _sounds(pi)[-1] == "not_heard.wav"
    assert [message["on"] for message in pi.messages("conversation")] == [True, False]
    # Off with none on does nothing.
    session._device_controls().set_conversation(False)
    assert brain.tts.said == ["Okay."]


def test_turning_it_on_while_one_runs_elsewhere_stays_off_and_says_so(brain, connect, make_session):
    session, pi = _a_session_with_conversations(brain, connect, make_session)
    brain.spoken_name = {"pc": "the PC"}.get
    assert brain.open_conversation.start("pc") == "started"
    running(session)

    session._device_controls().set_conversation(True)

    assert wait_for(lambda: brain.tts.said and session._idle.is_set())
    assert brain.tts.said == ["We're already talking at the PC."]
    # Nothing started here, nor listened for, and the one at the PC carries on.
    assert pi.messages("listen") == [] and _sounds(pi) == [] and pi.messages("conversation") == []
    assert not session._device_controls().in_conversation() and brain.open_conversation.on_at("pc")


def test_turning_it_on_at_a_muted_pi_is_refused_as_the_phrase_is(brain, connect, make_session):
    session, pi = _a_session_with_conversations(brain, connect, make_session)
    session.set_muted(True)
    running(session)

    session._device_controls().set_conversation(True)

    assert wait_for(lambda: brain.tts.said and session._idle.is_set())
    assert brain.tts.said == ["The microphone's muted."]
    assert brain.open_conversation.satellite is None and pi.messages("listen") == []


def test_turning_it_on_while_conversations_are_off_says_so(brain, connect, make_session):
    # A switch Home Assistant still showed for a moment after the setting was turned off. Typed, the phrase would
    # be left to Claude, who would answer it as chat.
    session, pi = _a_session_with_conversations(brain, connect, make_session, enabled=False)
    controls = session._device_controls()
    assert controls.conversations() is False
    running(session)

    controls.set_conversation(True)

    assert wait_for(lambda: brain.tts.said)
    assert brain.tts.said == ["Conversations are switched off."]
    assert brain.open_conversation.satellite is None and pi.messages("listen") == []


def test_the_switch_is_only_there_with_conversations(brain, make_session):
    # The fixture's brain has none.
    controls = make_session()._device_controls()
    assert controls.conversations() is False and controls.in_conversation() is False
    controls.set_conversation(False)


def test_turning_it_on_where_one_is_already_running_starts_nothing_more(brain, connect, make_session):
    session, pi = _in_a_conversation(brain, connect, make_session)
    session._device_controls().set_conversation(True)
    assert session._typed.empty()


def test_turning_it_on_is_never_taken_for_the_answer_to_a_typed_question(brain, connect, make_session):
    session, pi = _a_session_with_conversations(brain, connect, make_session)
    now = time.monotonic()
    # A question asked of something typed is open, so what's typed now would be its answer.
    session._typed_question = (now - 1, now + 60)

    session._device_controls().set_conversation(True)

    wake = session._next_typed()
    assert wake["typed"] == "let's talk" and wake["follow_up"] is False


# What goes off by itself during a conversation

def _announcing(session, *args, **kwargs):
    """Announces on a thread of its own, as the clock and the brain's watchers do - giving it, and a list of what it gives."""
    results = []
    thread = threading.Thread(target=lambda: results.append(session.announce(*args, **kwargs)), daemon=True)
    thread.start()
    return thread, results


def _announced(pi):
    """What was played as an announcement, in order: each sound by its name, and "words" for each stream."""
    return [message.get("name", "words") for message, _ in pi.received
            if message["type"] in ("play_sound", "play_stream") and message["kind"] == "announcement"]


def _played(pi):
    """Every sound and stream the brain started, in order: each sound by its name, and each stream by its kind."""
    return [message.get("name", message["kind"]) for message, _ in pi.received
            if message["type"] in ("play_sound", "play_stream")]


def test_a_timer_due_mid_conversation_rings_at_the_next_gap(brain, connect, make_session):
    # Not held until the conversation ends, which could be half an hour - nor said while Nova speaks, or while
    # he does: in the next gap, once a second has gone by with no speech in it.
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Okay."), Reply(say="Right."))
    brain.stt.texts = ["Hello.", "What about Tuesday?"]
    # The first reply plays on until the test lets it finish.
    pi.held = {1}
    heard = _open_microphone(pi, after=1)
    running(session)

    pi.wake(said())
    assert wait_for(lambda: pi.messages("play_stream"))
    first, rang = _announcing(session, "Timer for five minutes.", sound="timer_beep.wav", still_wanted=lambda: True,
                              ringing=True)
    time.sleep(0.5)
    assert _announced(pi) == []
    pi.say("played", id=pi.messages("play_stream")[0]["id"])
    ended = time.monotonic()
    first.join(10)
    assert rang == [True] and time.monotonic() - ended >= 0.9
    assert _announced(pi) == ["timer_beep.wav", "words"] and brain.tts.said == ["Okay.", "Timer for five minutes."]
    assert brain.open_conversation.on_at(session.satellite.name) and not session._idle.is_set()

    # Asked for while he talks, it waits - through his pause before his turn ends, too - for the gap after the reply.
    heard.put(said(seconds=1.0, before=0, after=0))
    time.sleep(0.3)
    second, rang = _announcing(session, "Timer for ten minutes.", sound="timer_beep.wav", still_wanted=lambda: True,
                               ringing=True)
    second.join(15)
    assert rang == [True]
    assert brain.tts.said == ["Okay.", "Timer for five minutes.", "Right.", "Timer for ten minutes."]
    assert _played(pi)[-3:] == ["reply", "timer_beep.wav", "announcement"]
    assert session.conversation.heard == [("Hello.", False), ("What about Tuesday?", False)]
    _ended(session)


def test_stop_dismisses_it_without_the_wake_word(brain, connect, make_session, monkeypatch, pi_ringing):
    # He's being listened to already, so "stop" said over what rings is a turn like any other - its sound is
    # listened through, as that's the time to say it.
    brain.open_conversation = OpenConversation(lambda: {"enabled": True})
    satellite, pi = connect(conversations=True)
    session = make_session(satellite)
    monkeypatch.setattr(session.llm, "process_query", lambda text, **kwargs: {
        "response": "Hi.", "actions": [], "success": True})
    assert brain.open_conversation.start(satellite.name) == "started"
    brain.stt.texts = ["Hello.", "Stop."]
    pi.held_sounds = {"timer_beep.wav"}
    heard = _open_microphone(pi, after=1)
    _sound_then(session, lambda: heard.put(said(seconds=0.5, before=0, after=0)))
    running(session)

    pi.wake(said())
    assert wait_for(lambda: pi.messages("listen"))
    ringing, rang = _announcing(session, "Timer for five minutes.", sound="timer_beep.wav",
                                still_wanted=lambda: "bedroom" in pi_ringing, ringing=True)
    ringing.join(10)
    # Dismissed, with its sound stopped there and then, its words never said, and the chime - and the
    # conversation goes on.
    assert rang == [False] and pi_ringing == set()
    beep = next(message["id"] for message in pi.messages("play_sound") if message["name"] == "timer_beep.wav")
    assert _stopped(pi, beep)
    assert brain.tts.said == ["Hi."] and "success_chime.wav" in _sounds(pi)
    assert brain.open_conversation.on_at(satellite.name)
    _ended(session)


def test_an_announcements_own_words_are_not_taken_for_his_turn(brain, connect, make_session):
    # What's left of them reaching the open microphone would read as speech - so from as they start until just
    # after they end, nothing is listened to.
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Okay."))
    brain.stt.texts = ["Hello."]
    # Its words, the second stream, play on until the test lets them finish.
    pi.held = {2}
    heard = _open_microphone(pi, after=1)
    running(session)

    pi.wake(said())
    assert wait_for(lambda: pi.messages("listen"))
    saying, results = _announcing(session, "Homelab: the backup failed.")
    assert wait_for(lambda: len(pi.messages("play_stream")) == 2, seconds=10)
    # Their echo, as loud as he is.
    heard.put(said(seconds=0.5, before=0, after=0))
    time.sleep(0.5)
    pi.say("played", id=pi.messages("play_stream")[1]["id"])
    saying.join(5)
    assert results == [True]
    # Long enough for a turn to have been heard and answered.
    time.sleep(2.5)
    assert session.conversation.heard == [("Hello.", False)]
    assert all(made.finished_with is None for made in brain.stt.made[1:])
    _ended(session)


def test_his_turn_over_an_announcements_sound_is_answered_once_it_ends(brain, connect, make_session):
    # Begun in a gap, it finishes before a reply is said, so the two never talk over each other - and its words
    # wait for the next gap.
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Okay."), Reply(say="It's sunny."))
    brain.stt.texts = ["Hello.", "What's the weather?"]
    pi.held_sounds = {"announcement_sound.wav"}
    heard = _open_microphone(pi, after=1)
    _sound_then(session, lambda: heard.put(said(seconds=0.5, before=0, after=0)))
    running(session)

    pi.wake(said())
    assert wait_for(lambda: pi.messages("listen"))
    saying, results = _announcing(session, "Homelab: the backup failed.")
    assert wait_for(lambda: len(session.conversation.heard) == 2, seconds=10)
    time.sleep(0.3)
    assert brain.tts.said == ["Okay."]
    sound = next(message["id"] for message in pi.messages("play_sound") if message["name"] == "announcement_sound.wav")
    pi.say("played", id=sound)
    saying.join(10)
    assert results == [True]
    assert brain.tts.said == ["Okay.", "It's sunny.", "Homelab: the backup failed."]
    _ended(session)


# Music during a conversation

def _doing(conversation, text, do):
    """Has a scripted conversation do something as it hears some words - as running what they ask for does."""
    handle = conversation.handle

    def handling(heard, follow_up=False, timeline=None):
        if heard == text:
            do()
        return handle(heard, follow_up=follow_up, timeline=timeline)

    conversation.handle = handling


def _songs():
    return [{"id": str(number), "title": f"Song {number}", "artist": "Band"} for number in range(1, 4)]


def _music_asked_for(brain, connect, make_session, *then):
    """
    A conversation at a satellite with a real Navidrome player, in which "Play some Drake." starts some, and then
    each of then - (words, what they do, given the session) - is said. Its last reply plays on until the test
    lets it finish.

    Returns:
        The session, the fake Pi and the player's speaker, once that last reply has started.
    """
    replies = [Reply(say="Okay."), Reply(say="Playing Drake.")] + [Reply(say="Sure.") for _ in then]
    session, pi = _in_a_conversation(brain, connect, make_session, *replies)
    speaker = _with_navidrome(session)
    _doing(session.conversation, "Play some Drake.", lambda: session.music_player._start(_songs()))
    for words, do in then:
        _doing(session.conversation, words, lambda do=do: do(session))
    brain.stt.texts = ["Hello.", "Play some Drake.", *[words for words, _ in then]]
    pi.answers = [said(before=0, after=2) for _ in range(1 + len(then))]
    pi.held = {len(replies)}
    running(session)
    pi.wake(said())
    assert wait_for(lambda: len(pi.messages("play_stream")) == len(replies), seconds=10)
    return session, pi, speaker


def test_music_asked_for_plays_quietly_until_the_end(brain, connect, make_session):
    # Not held until the conversation's over, which could be half an hour: it plays from the reply on, at a share
    # of the music volume (0.5) - not paused for the replies after - and at all of it once it ends.
    session, pi, speaker = _music_asked_for(brain, connect, make_session, ("Thanks.", lambda session: None))
    assert wait_for(lambda: speaker.started and not speaker.started[-1].paused)
    assert speaker.started[-1].volume == pytest.approx(0.15)
    assert brain.open_conversation.on_at(session.satellite.name)
    pi.say("played", id=pi.messages("play_stream")[-1]["id"])
    _ended(session)
    assert wait_for(lambda: speaker.started[-1].volume == pytest.approx(0.5))
    assert not speaker.started[-1].paused and len(speaker.started) == 1


def test_turning_the_music_up_mid_conversation_keeps_it_at_its_share(brain, connect, make_session):
    def louder(session):
        session.actions.execute(Action("set_volume", {"category": "music", "level": 80}))

    session, pi, speaker = _music_asked_for(brain, connect, make_session, ("Turn it up.", louder))
    # What he asked for is the music's volume, which comes back once it's over.
    assert session.volume.percent("music") == 80
    assert wait_for(lambda: speaker.started[-1].volume == pytest.approx(0.24))
    pi.say("played", id=pi.messages("play_stream")[-1]["id"])
    _ended(session)
    assert wait_for(lambda: speaker.started[-1].volume == pytest.approx(0.8))


def test_music_from_before_comes_back_when_it_ends(brain, connect, make_session):
    # Held, as for any request, through every turn - one which asked for nothing included - and let go at the end.
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Okay."), Reply(say="Right."))
    speaker = _with_navidrome(session)
    session.music_player._start(_songs())
    assert wait_for(lambda: speaker.started and not speaker.started[0].paused)
    brain.stt.texts = ["Hello.", "What's on today?"]
    pi.answers = [said(before=0, after=2)]
    pi.held = {2}
    running(session)

    pi.wake(said())
    assert wait_for(lambda: len(pi.messages("play_stream")) == 2)
    assert speaker.started[0].paused
    pi.say("played", id=pi.messages("play_stream")[1]["id"])
    _ended(session)
    session.music.wait()
    assert wait_for(lambda: not speaker.started[0].paused)
    assert speaker.started[0].volume == pytest.approx(0.5) and len(speaker.started) == 1


# Cutting in on a reply

# The conversation's second reply, which these tests talk over.
TODAY = "Today, you've got lunch, then the gym."


def _over_a_reply(brain, connect, make_session, *texts, ready=True, setup=None):
    """
    A conversation under way at a satellite whose echo canceller is ready, unless ready is False: "Hello." woke
    it, "What's on today?" is its first open turn, and its reply - TODAY, the second - plays on until the test
    lets it finish, with "Tuesday's clear." the reply after. Speech-to-text hears the texts given after those
    two. setup, if given, is called with the session before it runs.
    """
    session, pi = _in_a_conversation(brain, connect, make_session, Reply(say="Okay."), Reply(say=TODAY),
                                     Reply(say="Tuesday's clear."))
    session.satellite.echo_ready = ready
    if setup is not None:
        setup(session)
    brain.stt.texts = ["Hello.", "What's on today?", *texts]
    pi.answers = [said(before=0, after=2)]
    pi.held = {2}
    running(session)
    pi.wake(said())
    assert wait_for(lambda: len(pi.messages("play_stream")) == 2)
    return session, pi


def _open_microphone(pi, after=2):
    """
    Has the satellite send what its open microphone hears, into whichever listen is the brain's, from once
    the brain has asked it to listen `after` times: quiet without end, and whatever the test puts in the
    queue returned, in its place, as it comes.
    """
    heard = queue.Queue()

    def send():
        try:
            wait_for(lambda: len(pi.messages("listen")) >= after, seconds=10)
            while True:
                try:
                    pi.send_audio(heard.get_nowait())
                except queue.Empty:
                    pi.send_audio(np.zeros(800, np.int16))
                    time.sleep(0.05)
        except (OSError, ValueError):
            pass
    threading.Thread(target=send, daemon=True).start()
    return heard


def _fades(pi, playback):
    """What a playback's volume was faded to, in order, each with how long its fade took."""
    return [(round(message["volume"], 3), message.get("fade")) for message in pi.messages("set_playback")
            if message["id"] == playback]


def _stopped(pi, playback):
    return any(message.get("id") == playback for message in pi.messages("stop"))


def _listens_and_replies(pi):
    """The order the brain asked the satellite to listen, and started each reply."""
    return [message["type"] for message, _ in pi.received if message["type"] in ("listen", "play_stream")]


def _turns_logged():
    with open(os.path.join(paths.LOGS_DIR, "latency.jsonl")) as log:
        return [json.loads(line) for line in log]


def test_a_voice_dips_the_reply_and_words_stop_it(brain, connect, make_session):
    session, pi = _over_a_reply(brain, connect, make_session, "Hang on, what about Tuesday?",
                                "Hang on, what about Tuesday?")
    heard = _open_microphone(pi)
    reply = pi.messages("play_stream")[1]["id"]
    # His voice, from a frame's edge almost a second into the reply, for a second.
    heard.put(said(seconds=1.0, before=0.96, after=0))
    assert wait_for(lambda: len(pi.messages("play_stream")) == 3, seconds=10)
    # Down to the dip level of its volume (0.7) at once, then stopped.
    assert _fades(pi, reply) == [(0.21, 0.05)] and _stopped(pi, reply)
    # His words are his next turn, said over a reply, in the listen the reply was heard over - no other asked for.
    assert session.conversation.heard[2] == ("Hang on, what about Tuesday?", False)
    assert session.conversation.started == [False, False, True]
    assert _listens_and_replies(pi)[:6] == ["play_stream", "listen", "listen", "play_stream", "listen",
                                            "play_stream"]
    # Its recording starts half a second before his voice began - by the frame it was heard in - so his first
    # word is in it, whole.
    recording = brain.stt.made[3].finished_with
    began = np.flatnonzero(recording)[0]
    assert 8000 <= began < 8000 + FRAME and (recording[began:began + RATE] > 0).all()
    _ended(session)


def test_a_cough_dips_it_and_it_carries_on(brain, connect, make_session):
    session, pi = _over_a_reply(brain, connect, make_session, "", "And Tuesday?")
    heard = _open_microphone(pi)
    reply = pi.messages("play_stream")[1]["id"]
    heard.put(said(seconds=0.3, before=0.5, after=0))
    # Down, and back up once no words have come - with speech-to-text let go of.
    assert wait_for(lambda: len(_fades(pi, reply)) == 2)
    assert _fades(pi, reply) == [(0.21, 0.05), (0.7, 0.05)] and brain.stt.made[2].settled
    time.sleep(0.2)
    pi.say("played", id=reply)
    # Played to the end, and the next turn hears what he says after it in the same listen.
    heard.put(said(seconds=0.5, before=0, after=0))
    assert wait_for(lambda: len(pi.messages("play_stream")) == 3, seconds=10)
    assert not _stopped(pi, reply)
    assert session.conversation.heard[2] == ("And Tuesday?", False)
    assert session.conversation.started == [False, False, False]
    assert _listens_and_replies(pi)[:6] == ["play_stream", "listen", "listen", "play_stream", "listen",
                                            "play_stream"]
    # The dip which came back is counted, with no cut-in, in the timings of the turn whose reply it was.
    assert wait_for(lambda: any("dips_back" in turn for turn in _turns_logged()))
    turn = next(turn for turn in _turns_logged() if "dips_back" in turn)
    assert turn["dips_back"] == 1 and "cut_in" not in turn and "talked_over" not in turn
    _ended(session)


def test_nova_hearing_its_own_reply_doesnt_stop_it(brain, connect, make_session):
    # What got past the echo canceller, heard as a run of the reply's own words.
    session, pi = _over_a_reply(brain, connect, make_session, "you've got lunch then the gym")
    heard = _open_microphone(pi)
    reply = pi.messages("play_stream")[1]["id"]
    heard.put(said(seconds=0.5, before=0.5, after=0))
    assert wait_for(lambda: len(_fades(pi, reply)) == 2)
    assert _fades(pi, reply) == [(0.21, 0.05), (0.7, 0.05)]
    assert not _stopped(pi, reply) and len(session.conversation.heard) == 2
    pi.say("played", id=reply)
    _ended(session)


def test_a_press_cuts_in(brain, connect, make_session):
    session, pi = _over_a_reply(brain, connect, make_session, "What about Tuesday?")
    heard = _open_microphone(pi)
    reply = pi.messages("play_stream")[1]["id"]
    assert wait_for(lambda: len(pi.messages("listen")) == 2)
    # The satellite has stopped the reply itself, and played its press sound.
    pi.say("cut_in", recording=pi.messages("listen")[1]["recording"])
    assert wait_for(lambda: _stopped(pi, reply))
    heard.put(said(seconds=0.5, before=0.2, after=0))
    assert wait_for(lambda: len(pi.messages("play_stream")) == 3, seconds=10)
    # His turn begins there and then, in the same listen, said over a reply - with nothing for speech-to-text
    # to hear over the reply first.
    assert session.conversation.heard[2] == ("What about Tuesday?", False)
    assert session.conversation.started == [False, False, True]
    # A turn begun on a press, as one woken by a press is.
    assert session.conversation.pressed[2] is True
    assert brain.stt.made[2].finished_with is not None
    assert _listens_and_replies(pi)[:6] == ["play_stream", "listen", "listen", "play_stream", "listen",
                                            "play_stream"]
    assert wait_for(lambda: any(turn.get("cut_in") == "press" for turn in _turns_logged()))
    _ended(session)


def test_hey_nova_over_a_reply_stops_it_and_is_taken_off_the_turn(brain, connect, make_session):
    session, pi = _over_a_reply(brain, connect, make_session, "Hey Nova, what about Tuesday",
                                "Hey Nova, what about Tuesday")
    heard = _open_microphone(pi)
    reply = pi.messages("play_stream")[1]["id"]
    heard.put(said(seconds=1.0, before=0.5, after=0))
    assert wait_for(lambda: len(pi.messages("play_stream")) == 3, seconds=10)
    assert _stopped(pi, reply)
    assert session.conversation.heard[2] == ("What about Tuesday", False)
    _ended(session)


def _only_the_wake_word_cuts_in(session, pi, then):
    """
    Checks nothing is listened to while the second reply plays, and that the wake word said over it still
    stops it - and that once `then` has been called, as it plays, the reply after is listened over.
    """
    reply = pi.messages("play_stream")[1]["id"]
    time.sleep(0.3)
    assert len(pi.messages("listen")) == 1
    then()
    # The reply after plays on until it's let go, so only a listen over it is asked for while it does.
    pi.held = {2, 3}
    pi.wake(said(), talked_over=True)
    assert wait_for(lambda: _stopped(pi, reply))
    assert wait_for(lambda: len(pi.messages("play_stream")) == 3, seconds=10)
    assert session.conversation.heard[2] == ("What about Tuesday?", False)
    assert session.conversation.started == [False, False, True]
    assert wait_for(lambda: len(pi.messages("listen")) == 2)
    assert _listens_and_replies(pi)[-2:] == ["listen", "play_stream"]
    pi.say("played", id=pi.messages("play_stream")[2]["id"])
    _ended(session)


def test_no_voice_cut_in_until_the_echo_canceller_is_ready(brain, connect, make_session):
    session, pi = _over_a_reply(brain, connect, make_session, "What about Tuesday?", ready=False)
    _only_the_wake_word_cuts_in(session, pi, then=lambda: setattr(session.satellite, "echo_ready", True))


def test_no_voice_cut_in_while_something_else_plays_in_the_room(brain, connect, make_session):
    # What makes the satellite's wake word harder to set off: a film's dialogue mustn't stop a reply either.
    brain.media = _playing(("media_player.bedroom_tv", "Bedroom TV", "Bedroom", "devtv", None))
    brain.connected_sessions = lambda: []
    made = make_session

    def in_the_bedroom(satellite=None):
        return _in_room(made(satellite), "Bedroom", "devbedroom")

    session, pi = _over_a_reply(brain, connect, in_the_bedroom, "What about Tuesday?")
    assert session.playing_here() == ["Bedroom TV"]
    _only_the_wake_word_cuts_in(session, pi, then=lambda: setattr(brain, "media", _playing()))


def test_talk_over_set_to_the_wake_word_or_a_press(brain, connect, make_session):
    brain.config.data["conversation"] = {"talk_over": "wake_word_or_press"}
    session, pi = _over_a_reply(brain, connect, make_session, "What about Tuesday?")
    # Read as each reply starts, so a change on the settings page is used from the next.
    _only_the_wake_word_cuts_in(session, pi,
                                then=lambda: brain.config.data.update(conversation={"talk_over": "voice"}))


def test_outside_a_conversation_nothing_listens_during_a_reply(brain, connect, make_session):
    brain.open_conversation = OpenConversation(lambda: {"enabled": True})
    satellite, pi = connect(conversations=True)
    satellite.echo_ready = True
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say=TODAY), Reply(say="Okay."), Reply(say=TODAY))
    brain.stt.texts = ["What's on today?", "Hello.", "What's on today?"]
    pi.held = {1, 3}
    running(session)

    pi.wake(said())
    assert wait_for(lambda: pi.messages("play_stream"))
    time.sleep(0.3)
    # Only the wake word or a press stops it, as ever.
    assert pi.messages("listen") == []
    pi.say("played", id=pi.messages("play_stream")[0]["id"])
    assert wait_for(lambda: session._idle.is_set())
    assert pi.messages("listen") == []

    # The same satellite, once a conversation is under way there, listens over its replies.
    brain.open_conversation.start(satellite.name)
    pi.answers = [said(before=0, after=2)]
    pi.wake(said())
    assert wait_for(lambda: len(pi.messages("play_stream")) == 3, seconds=10)
    assert wait_for(lambda: len(pi.messages("listen")) == 2)
    assert _listens_and_replies(pi)[-2:] == ["listen", "play_stream"]
    pi.say("played", id=pi.messages("play_stream")[2]["id"])
    _ended(session)


def test_nemotron_only_hears_once_a_voice_has(brain, connect, make_session):
    session, pi = _over_a_reply(brain, connect, make_session, "Hang on.", "Hang on.")
    heard = _open_microphone(pi)
    assert wait_for(lambda: len(pi.messages("listen")) == 2)
    # A second of the reply with only quiet heard over it: nothing for speech-to-text, though the session is
    # getting on all the while.
    moved_at = session.moved_at
    time.sleep(1.0)
    assert len(brain.stt.made) == 2 and session.moved_at > moved_at
    # Then a voice - which speech-to-text is given from half a second before it began.
    heard.put(said(seconds=0.5, before=0.96, after=0))
    assert wait_for(lambda: len(brain.stt.made) >= 3 and brain.stt.made[2].added)
    first = brain.stt.made[2].added[0]
    pre_roll = round(session_module.CUT_IN_PRE_ROLL * RATE)
    assert pre_roll <= np.flatnonzero(first)[0] < pre_roll + FRAME
    assert wait_for(lambda: len(pi.messages("play_stream")) == 3, seconds=10)
    _ended(session)


def test_a_cut_in_reply_is_marked_cut_short(brain, connect, make_session):
    # As a reply the wake word stops is: so his turn is taken as said over a reply, and the timings say so.
    session, pi = _over_a_reply(brain, connect, make_session, "Stop.", "Stop.")
    heard = _open_microphone(pi)
    heard.put(said(seconds=0.5, before=0.5, after=0))
    assert wait_for(lambda: len(session.conversation.heard) == 3, seconds=10)
    assert session.conversation.started[2] is True
    assert wait_for(lambda: any(turn.get("cut_in") for turn in _turns_logged()))
    turn = next(turn for turn in _turns_logged() if turn.get("cut_in"))
    assert (turn["cut_in"], turn["talked_over"], turn["dips_back"]) == ("voice", True, 0)
    _ended(session)


def test_ended_while_a_reply_is_listened_over_stops_the_listen_and_the_reply_finishes(brain, connect,
                                                                                    make_session):
    session, pi = _over_a_reply(brain, connect, make_session, "")
    heard = _open_microphone(pi)
    reply = pi.messages("play_stream")[1]["id"]
    heard.put(said(seconds=0.3, before=0.5, after=0))
    assert wait_for(lambda: _fades(pi, reply))
    session.end_conversation("switch")
    # The listen over it is stopped there and then, and the reply comes back up and is said to its end.
    recording = pi.messages("listen")[1]["recording"]
    assert wait_for(lambda: any(message.get("recording") == recording for message in pi.messages("stop_listening")))
    assert wait_for(lambda: len(_fades(pi, reply)) == 2) and _fades(pi, reply)[1] == (0.7, 0.05)
    time.sleep(0.2)
    assert not _stopped(pi, reply) and not session._idle.is_set()
    pi.say("played", id=reply)
    # Nothing more is listened for.
    assert wait_for(lambda: session._idle.is_set())
    assert len(pi.messages("listen")) == 2 and len(session.conversation.heard) == 2
    assert [message["on"] for message in pi.messages("conversation")] == [True, False]


def test_a_wake_word_over_a_reply_listened_over_still_stops_it(brain, connect, make_session):
    session, pi = _over_a_reply(brain, connect, make_session, "What about Tuesday?")
    reply = pi.messages("play_stream")[1]["id"]
    assert wait_for(lambda: len(pi.messages("listen")) == 2)
    # From a satellite which can still hear its wake word while it sends what it hears.
    pi.wake(said(), talked_over=True)
    assert wait_for(lambda: _stopped(pi, reply))
    assert wait_for(lambda: len(session.conversation.heard) == 3, seconds=10)
    assert session.conversation.heard[2] == ("What about Tuesday?", False)
    assert session.conversation.started == [False, False, True]
    _ended(session)


class _LateWords(FakeTranscription):
    """Speech-to-text whose words only come once it has heard the last of what it was given, as a short word's can."""

    def words(self):
        return ""


def test_a_voice_still_going_as_the_reply_ends_starts_the_next_turn(brain, connect, make_session):
    # His words, still to be heard as the reply ends - so it stays dipped and carries on to its end.
    session, pi = _over_a_reply(brain, connect, make_session, _LateWords("Hang on"), "Hang on, and Tuesday?")
    heard = _open_microphone(pi)
    reply = pi.messages("play_stream")[1]["id"]
    heard.put(said(seconds=1.0, before=0.96, after=0))
    assert wait_for(lambda: _fades(pi, reply))
    pi.say("played", id=reply)
    assert wait_for(lambda: len(pi.messages("play_stream")) == 3, seconds=10)
    # Never stopped, nor brought back up once it had finished.
    assert not _stopped(pi, reply) and _fades(pi, reply) == [(0.21, 0.05)]
    # The next turn is in the same listen, from half a second before his voice began, so all of it is heard.
    assert session.conversation.heard[2] == ("Hang on, and Tuesday?", False)
    assert session.conversation.started == [False, False, False]
    recording = brain.stt.made[3].finished_with
    began = np.flatnonzero(recording)[0]
    assert 8000 <= began < 8000 + FRAME and (recording[began:began + RATE] > 0).all()
    _ended(session)


@pytest.mark.parametrize("over", ["Yeah", "you've got lunch then the gym"])
def test_listening_noises_or_novas_own_echo_as_the_reply_ends_arent_the_next_turn(brain, connect, make_session,
                                                                                  over):
    # "Yeah" only says he's listening, and a run of the reply's own words is its echo: either keeps the reply
    # dipped to its end, and neither is anything he said to it.
    session, pi = _over_a_reply(brain, connect, make_session, over, "And Tuesday?")
    heard = _open_microphone(pi)
    reply = pi.messages("play_stream")[1]["id"]
    heard.put(said(seconds=1.0, before=0.5, after=0))
    assert wait_for(lambda: _fades(pi, reply))
    time.sleep(0.3)
    pi.say("played", id=reply)
    # What he says after the reply, quieter than what was heard over it.
    heard.put(np.full(round(0.5 * RATE), 4000, np.int16))
    assert wait_for(lambda: len(pi.messages("play_stream")) == 3, seconds=10)
    assert session.conversation.heard[2] == ("And Tuesday?", False)
    # The next turn starts where the reply ended, so none of what was heard over it is in it.
    recording = brain.stt.made[3].finished_with
    assert recording.max() == np.float32(4000 / 32768.0)
    _ended(session)


def test_something_typed_while_a_reply_is_listened_over_is_the_turn_after_it(brain, connect, make_session):
    session, pi = _over_a_reply(brain, connect, make_session)
    _open_microphone(pi)
    reply = pi.messages("play_stream")[1]["id"]
    assert wait_for(lambda: len(pi.messages("listen")) == 2)
    session.ask("And then?")
    pi.say("played", id=reply)
    assert wait_for(lambda: len(pi.messages("play_stream")) == 3, seconds=10)
    assert session.conversation.heard[2] == ("And then?", False)
    # The listen the reply was heard over isn't left sending what the satellite hears.
    recording = pi.messages("listen")[1]["recording"]
    assert any(message.get("recording") == recording for message in pi.messages("stop_listening"))
    _ended(session)


class _BreaksOnSpeech(LoudnessDetector):
    """Hears speech by how loud it is, then raises at its sixth frame of it - once a reply has dipped."""

    def __init__(self):
        super().__init__()
        self.speech = 0

    def speech_probability(self, frame):
        heard = super().speech_probability(frame)
        self.speech += heard > 0
        if self.speech > 5:
            raise RuntimeError("the model failed")
        return heard


def test_a_failure_hearing_over_a_reply_lets_it_finish_at_full_volume(brain, connect, make_session, caplog):
    # Rather than the reply playing on, dipped, under an apology, with its listen never let go of.
    session, pi = _over_a_reply(brain, connect, make_session, "",
                                setup=lambda session: setattr(session, "_cut_in_detector", _BreaksOnSpeech()))
    heard = _open_microphone(pi)
    reply = pi.messages("play_stream")[1]["id"]
    heard.put(said(seconds=1.0, before=0.5, after=0))
    assert wait_for(lambda: len(_fades(pi, reply)) == 2)
    assert _fades(pi, reply) == [(0.21, 0.05), (0.7, 0.05)]
    recording = pi.messages("listen")[1]["recording"]
    assert wait_for(lambda: any(message.get("recording") == recording for message in pi.messages("stop_listening")))
    time.sleep(0.2)
    assert not _stopped(pi, reply) and brain.tts.said == ["Okay.", TODAY]
    pi.say("played", id=reply)
    # The conversation goes on, with the one listen of its next turn - nothing said, and nothing left open.
    assert wait_for(lambda: len(pi.messages("listen")) == 3)
    time.sleep(0.3)
    assert len(pi.messages("listen")) == 3 and brain.tts.said == ["Okay.", TODAY]
    third = pi.messages("listen")[2]["recording"]
    assert not any(message.get("recording") == third for message in pi.messages("stop_listening"))
    assert "RuntimeError" in caplog.text and "the model failed" not in caplog.text
    _ended(session)


class _NotingCancel(FakeTranscription):
    """Speech-to-text which notes, in order, when it's let go of."""

    def __init__(self, text, order):
        super().__init__(text)
        self.order = order

    def cancel(self):
        self.order.append("cancel")
        super().cancel()


def test_words_stop_the_reply_before_speech_to_text_is_let_go_of(brain, connect, make_session):
    # Letting it go waits for Nemotron to finish what it has, which the reply mustn't be said over him for.
    order = []

    def noting_stops(session):
        play_stream = session.speaker.play_stream

        def playing(*args, **kwargs):
            playback = play_stream(*args, **kwargs)
            stop = playback.stop
            playback.stop = lambda: order.append("stop") or stop()
            return playback
        session.speaker.play_stream = playing

    session, pi = _over_a_reply(brain, connect, make_session, _NotingCancel("Hang on, what about Tuesday?", order),
                                "Hang on, what about Tuesday?", setup=noting_stops)
    heard = _open_microphone(pi)
    heard.put(said(seconds=1.0, before=0.5, after=0))
    assert wait_for(lambda: "cancel" in order, seconds=10)
    assert "stop" in order and order.index("stop") < order.index("cancel")
    _ended(session)


class _SlowWakes(queue.Queue):
    """The satellite's wakes, each arriving a moment after the listen it ends - as RemoteSatellite._woken has it."""

    def put(self, item, block=True, timeout=None):
        time.sleep(0.02)
        super().put(item, block, timeout)


def test_a_wake_word_while_the_reply_is_dipped_doesnt_bring_it_back_up(brain, connect, make_session):
    session, pi = _over_a_reply(brain, connect, make_session, "", "What about Tuesday?",
                                setup=lambda session: setattr(session.satellite, "wakes", _SlowWakes()))
    heard = _open_microphone(pi)
    reply = pi.messages("play_stream")[1]["id"]
    heard.put(said(seconds=0.3, before=0.5, after=0))
    assert wait_for(lambda: _fades(pi, reply))
    pi.wake(said(), talked_over=True)
    assert wait_for(lambda: _stopped(pi, reply))
    time.sleep(0.2)
    # Stopped where it was, not swelling back up first.
    assert _fades(pi, reply) == [(0.21, 0.05)]
    assert wait_for(lambda: len(session.conversation.heard) == 3, seconds=10)
    assert session.conversation.heard[2] == ("What about Tuesday?", False)
    # The turn after it may have begun by now.
    assert session.conversation.started[:3] == [False, False, True]
    _ended(session)


def test_a_short_word_heard_only_once_the_voice_has_stopped_stops_the_reply(brain, connect, make_session):
    # Nemotron's words come a moment behind the audio, and the last of a short "wait" only once it has heard the
    # quiet after - by which time the reply would be coming back up.
    session, pi = _over_a_reply(brain, connect, make_session, _LateWords("Wait."), "Wait.")
    heard = _open_microphone(pi)
    reply = pi.messages("play_stream")[1]["id"]
    heard.put(said(seconds=0.3, before=0.96, after=0))
    assert wait_for(lambda: len(pi.messages("play_stream")) == 3, seconds=10)
    assert _stopped(pi, reply) and _fades(pi, reply) == [(0.21, 0.05)]
    assert brain.stt.made[2].settled
    assert session.conversation.heard[2] == ("Wait.", False)
    # The turn after it may have begun by now.
    assert session.conversation.started[:3] == [False, False, True]
    # His turn, from half a second before the word.
    recording = brain.stt.made[3].finished_with
    assert 8000 <= np.flatnonzero(recording)[0] < 8000 + FRAME
    _ended(session)


def test_a_listen_which_stalls_over_a_reply_is_let_go_of_and_the_reply_finishes(brain, connect, make_session):
    session, pi = _over_a_reply(brain, connect, make_session)
    reply = pi.messages("play_stream")[1]["id"]
    assert wait_for(lambda: len(pi.messages("listen")) == 2)
    recording = pi.messages("listen")[1]["recording"]
    # Nothing comes from the satellite, so the listen is taken to have ended - and is stopped there too.
    assert wait_for(lambda: any(message.get("recording") == recording for message in pi.messages("stop_listening")))
    assert not _stopped(pi, reply) and not session._idle.is_set()
    pi.say("played", id=reply)
    # The next turn asks for a listen of its own.
    assert wait_for(lambda: len(pi.messages("listen")) == 3)
    _ended(session)


def test_a_reply_never_said_to_have_finished_is_given_up_on_and_the_next_turn_listens_on(brain, connect,
                                                                                         make_session, monkeypatch):
    monkeypatch.setattr(session_module, "PLAYBACK_SLACK", 0.5)
    session, pi = _over_a_reply(brain, connect, make_session, "And Tuesday?")
    heard = _open_microphone(pi)
    reply = pi.messages("play_stream")[1]["id"]
    assert wait_for(lambda: _stopped(pi, reply))
    heard.put(said(seconds=0.5, before=0, after=0))
    assert wait_for(lambda: len(pi.messages("play_stream")) == 3, seconds=10)
    assert session.conversation.heard[2] == ("And Tuesday?", False)
    assert session.conversation.started == [False, False, False]
    assert _listens_and_replies(pi)[:6] == ["play_stream", "listen", "listen", "play_stream", "listen",
                                            "play_stream"]
    _ended(session)


def test_a_listen_noted_without_a_start_isnt_carried_but_stopped(connect, make_session):
    satellite, pi = connect(conversations=True)
    session = make_session(satellite)
    feed = satellite.listen(open=True)
    session._open_feed = feed
    assert session._carried() is None
    assert feed.ended and session._open_feed is None
    assert wait_for(lambda: pi.messages("listen"))
    recording = pi.messages("listen")[0]["recording"]
    assert wait_for(lambda: any(message.get("recording") == recording for message in pi.messages("stop_listening")))
