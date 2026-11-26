"""
Remote Satellite Test
Checks the brain's side of one satellite - playing, stopping and hearing through it -
against a fake satellite over a socketpair, which reads what the brain sends with the
real protocol. Needs numpy installed (as it is on the Pi), and is skipped anywhere it isn't.
"""

import itertools
import json
import logging
import select
import socket
import struct
import threading
import time

import pytest

np = pytest.importorskip("numpy")

from voice_assistant import protocol, recording, remote, satellite_link
from voice_assistant.config import LIMITS
from voice_assistant.remote import RemoteSatellite

HELLO = {"name": "bedroom", "hostname": "pi", "sample_rate": 48000, "unclean": False}


def wait_for(condition, seconds=5):
    # Counted in loops, not read from a clock, as a test may patch the brain's clock.
    for _ in range(int(seconds / 0.005)):
        if condition():
            return True
        time.sleep(0.005)
    return False


def ends_at_once(feed):
    """Whether reading on from the end of a feed returns straight away, as it does once the feed has ended."""
    started = time.monotonic()
    feed.listen().read(100)
    return time.monotonic() - started < 1.0


class FakeSatellite:
    """The Pi's end of the connection: keeps everything the brain sends, and says what it's told to."""

    def __init__(self, sock):
        self.socket = sock
        self.stream = sock.makefile("rwb")
        self.received = []
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                self.received.append(protocol.receive(self.stream))
        except (OSError, protocol.ProtocolError):
            pass

    def say(self, kind, payload=b"", **fields):
        with self.lock:
            protocol.send(self.stream, kind, payload, **fields)

    def messages(self, kind):
        return [message for message, _ in self.received if message["type"] == kind]

    def payloads(self, kind):
        return [payload for message, payload in self.received if message["type"] == kind]

    def go(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()


def connect(stream_class=satellite_link.Stream, **said):
    """
    The brain's view of a satellite which has just been admitted, serving on a thread of
    its own - with anything else its hello said, e.g. media_controls=True.
    """
    ours, theirs = socket.socketpair()
    # Short, so the brain's reading wakes often to check on the satellite.
    ours.settimeout(0.05)
    satellite = RemoteSatellite(stream_class(ours), dict(HELLO, **said))
    fake = FakeSatellite(theirs)
    serving = threading.Thread(target=satellite.serve, daemon=True)
    serving.start()
    return satellite, fake, serving


def disconnect(satellite, fake, serving):
    satellite.close()
    fake.go()
    serving.join(2)


@pytest.fixture
def pair():
    made = connect()
    yield made
    disconnect(*made)


class Pieces:
    """A stream's pieces, which notes being closed - as a generator making speech would be."""

    def __init__(self, pieces):
        self.pieces = iter(pieces)
        self.closed = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.pieces)

    def close(self):
        self.closed.set()


class Failing(Pieces):
    """A stream's pieces which fail once the ones given are used up, as speech can part way through a reply."""

    def __next__(self):
        try:
            return super().__next__()
        except StopIteration:
            raise RuntimeError("the voice failed") from None


class Chunky(satellite_link.Stream):
    """
    A stream which writes in small pieces, as it does a big message on a busy connection,
    giving another thread the chance to write in between.
    """

    def write(self, data):
        for begin in range(0, len(data), 100):
            super().write(data[begin:begin + 100])
            time.sleep(0)


class Breakable:
    """A stream which takes what's written until it's broken, then fails as a hung-up connection does."""

    def __init__(self):
        self.broken = False

    def write(self, data):
        if self.broken:
            raise BrokenPipeError("hung up")

    def flush(self):
        pass


def test_the_satellite_is_known_by_its_hello(pair):
    satellite, fake, serving = pair
    assert (satellite.name, satellite.hostname, satellite.sample_rate) == ("bedroom", "pi", 48000)
    assert satellite.speaker.sample_rate == 48000
    assert satellite.connected


def test_a_stream_reaches_the_satellite_as_16_bit_pieces(pair):
    satellite, fake, serving = pair
    speech = np.linspace(-1, 1, 5000, dtype=np.float32)
    playback = satellite.speaker.play_stream(iter([speech[:3000], speech[3000:]]), sample_rate=22050, volume=0.5)

    assert wait_for(lambda: fake.messages("stream_end"))
    assert fake.messages("play_stream") == [{"type": "play_stream", "id": playback.id, "rate": 22050, "channels": 1,
                                             "volume": 0.5, "kind": "reply", "paused": False}]
    kinds = [message["type"] for message, _ in fake.received]
    assert kinds[0] == "play_stream" and kinds[-1] == "stream_end"
    assert set(kinds[1:-1]) == {"stream_audio"}
    assert all(message["id"] == playback.id for message, _ in fake.received)
    # Each piece is at most 100ms of it.
    pieces = fake.payloads("stream_audio")
    assert all(len(piece) <= 2205 * 2 for piece in pieces)
    sent = np.concatenate([np.frombuffer(piece, "<i2") for piece in pieces])
    assert len(sent) == 5000
    assert np.abs(sent / 32768 - speech).max() < 1 / 16384
    assert playback.duration == pytest.approx(5000 / 22050)


def test_music_goes_in_stereo_and_keeps_close_to_what_has_played(pair):
    satellite, fake, serving = pair
    song = np.tile(np.array([[0.25, -0.25]], dtype=np.float32), (500, 1))
    playback = satellite.speaker.play_stream((song for _ in range(10)), sample_rate=1000, kind="music",
                                             most_ahead=1.0)

    # A second of it, and the piece which went past that, then nothing more until
    # the satellite has played some.
    assert wait_for(lambda: len(fake.messages("stream_audio")) == 11)
    time.sleep(0.2)
    assert len(fake.messages("stream_audio")) == 11
    assert fake.messages("play_stream")[0]["channels"] == 2
    first = np.frombuffer(fake.payloads("stream_audio")[0], "<i2").reshape(-1, 2)
    assert len(first) == 100
    assert (first[:, 0] == 8192).all() and (first[:, 1] == -8192).all()

    # A second played, at the satellite's own rate.
    fake.say("progress", id=playback.id, position=48000)
    assert wait_for(lambda: len(fake.messages("stream_audio")) == 21)
    time.sleep(0.2)
    assert len(fake.messages("stream_audio")) == 21
    assert playback.position == 48000


def test_waiting_ends_when_the_satellite_says_played(pair):
    satellite, fake, serving = pair
    playback = satellite.speaker.play_file("/home/nova/sounds/timer.wav", volume=0.4, kind="announcement",
                                           start=2.0, length=3.0)

    assert wait_for(lambda: fake.messages("play_sound"))
    assert fake.messages("play_sound") == [{"type": "play_sound", "id": playback.id, "name": "timer.wav",
                                            "volume": 0.4, "kind": "announcement", "start": 2.0, "length": 3.0}]
    assert playback.wait(0.1) is False
    assert not playback.done

    fake.say("played", id=playback.id, stopped=False, position=144000)
    assert playback.wait(2) is True
    assert playback.done
    assert playback.position == 144000


def test_a_wake_brings_a_feed_of_the_recording(pair):
    satellite, fake, serving = pair
    heard = (np.arange(4800) % 1000).astype(np.int16)

    fake.say("wake", talked_over=True)
    for piece in np.split(heard, 3):
        fake.say("audio", piece.tobytes())
    fake.say("audio_end", reason="limit")

    wake = satellite.wakes.get(timeout=2)
    assert wake["talked_over"] is True
    assert np.array_equal(wake["feed"].listen(start=0).read(4800), heard)
    assert ends_at_once(wake["feed"])


def test_a_recording_the_satellite_ended_on_a_press_says_so(pair):
    satellite, fake, serving = pair
    feed = satellite.listen()
    fake.say("audio_end", reason="pressed")
    assert wait_for(lambda: feed.ended) and feed.pressed

    # Not at its limit, nor when the brain stops it.
    feed = satellite.listen()
    fake.say("audio_end", reason="limit")
    assert wait_for(lambda: feed.ended) and not feed.pressed
    feed = satellite.listen()
    satellite.stop_listening(feed)
    assert feed.ended and not feed.pressed


def test_a_wake_says_how_much_of_its_recording_is_from_before_it(pair):
    # In samples - or nothing, from a satellite which doesn't say, when the session
    # goes by its own setting.
    satellite, fake, serving = pair
    fake.say("wake", talked_over=False, pre_roll=4800)
    fake.say("wake", talked_over=False)
    fake.say("wake", talked_over=False, pre_roll=-1)
    fake.say("wake", talked_over=False, pre_roll=True)
    assert [satellite.wakes.get(timeout=2)["pre_roll"] for _ in range(4)] == [4800, None, None, None]


def test_a_pressed_wake_is_marked(pair):
    # A push-to-talk satellite says a recording started on a press, not on a wake word,
    # and has no pre-roll to send. An ordinary wake says nothing of it.
    satellite, fake, serving = pair
    fake.say("wake", talked_over=False, pressed=True, pre_roll=0)
    fake.say("wake", talked_over=False, pre_roll=4800)
    fake.say("wake", talked_over=False, pressed=False)
    fake.say("wake", talked_over=False, pressed="yes")
    pressed, ordinary, unpressed, odd = (satellite.wakes.get(timeout=2) for _ in range(4))
    assert pressed["pressed"] is True
    assert pressed["pre_roll"] == 0
    for wake in (ordinary, unpressed, odd):
        assert "pressed" not in wake


def test_the_satellite_says_it_is_push_to_talk():
    ours, theirs = socket.socketpair()
    try:
        assert RemoteSatellite(satellite_link.Stream(ours), dict(HELLO, push_to_talk=True)).push_to_talk is True
        assert RemoteSatellite(satellite_link.Stream(ours), dict(HELLO)).push_to_talk is False
    finally:
        ours.close()
        theirs.close()


def test_hello_fields_kept():
    ours, theirs = socket.socketpair()
    try:
        said = RemoteSatellite(satellite_link.Stream(ours), dict(HELLO, machine="pc", saves_notes=True))
        assert said.machine == "pc" and said.saves_notes is True
        silent = RemoteSatellite(satellite_link.Stream(ours), dict(HELLO))
        assert silent.machine is None and silent.saves_notes is False
    finally:
        ours.close()
        theirs.close()


def test_the_satellite_says_it_has_media_controls():
    ours, theirs = socket.socketpair()
    try:
        assert RemoteSatellite(satellite_link.Stream(ours), dict(HELLO, media_controls=True)).media_controls is True
        assert RemoteSatellite(satellite_link.Stream(ours), dict(HELLO)).media_controls is False
    finally:
        ours.close()
        theirs.close()


@pytest.fixture
def answer_notes():
    """
    Gives a test a way to have a fake satellite answer each save_note it's sent, with the same id and
    the answer given. The answering ends with the test - left to poll for ever, each would wake 200
    times a second through every test which follows.
    """
    over = threading.Event()
    threads = []

    def answer(fake, **answer):
        def answering():
            seen = 0
            while not over.is_set():
                if len(fake.messages("save_note")) > seen:
                    seen += 1
                    try:
                        fake.say("note_saved", id=fake.messages("save_note")[seen - 1]["id"], **answer)
                    except OSError:
                        return
                over.wait(0.005)

        threads.append(threading.Thread(target=answering, daemon=True, name="answering"))
        threads[-1].start()

    yield answer
    over.set()
    for thread in threads:
        thread.join(2)
    assert not any(thread.is_alive() for thread in threads)


def test_save_note_sends_and_returns_the_answer(pair, answer_notes):
    satellite, fake, serving = pair
    answer_notes(fake, file="Bins.txt")

    assert satellite.save_note("Bins", "green on Tuesday") == {"file": "Bins.txt"}

    (message,), (text,) = fake.messages("save_note"), fake.payloads("save_note")
    assert message["title"] == "Bins" and isinstance(message["id"], str)
    # The text goes as the message's payload, as a line of JSON is only 4096 bytes long.
    assert text.decode() == "green on Tuesday"
    assert "text" not in message


def test_save_note_returns_the_satellites_reason(pair, answer_notes):
    satellite, fake, serving = pair
    answer_notes(fake, error="There's no Downloads folder to save it in.")

    assert satellite.save_note("Bins", "x") == {"error": "There's no Downloads folder to save it in."}


def test_the_longest_note_and_a_hostile_title_still_make_a_message_the_satellite_can_read(pair, answer_notes):
    # A note's text is up to 50,000 characters - 200,000 bytes of UTF-8 at worst - and
    # a title may be anything, but a line of JSON may not be over protocol.MAX_LINE. An
    # emoji is the worst, as it's 12 bytes there.
    satellite, fake, serving = pair
    answer_notes(fake, file="x.txt")
    text = "\u00e9\u4e2d" * 25_000

    assert satellite.save_note("\U0001F600" * 500, text) == {"file": "x.txt"}

    (message,), (payload,) = fake.messages("save_note"), fake.payloads("save_note")
    assert payload.decode() == text
    assert len(message["title"]) <= remote.MOST_TITLE_SENT


def test_a_note_with_a_character_which_cant_be_encoded_still_reaches_the_satellite_to_refuse(pair, answer_notes):
    # A lone surrogate from a broken reply: it's the satellite, with notes.save_note,
    # which says it can't be written, as it says it for anything else.
    satellite, fake, serving = pair
    answer_notes(fake, error="The file couldn't be written.")

    assert satellite.save_note("Bins", "a\ud800b") == {"error": "The file couldn't be written."}
    assert fake.payloads("save_note")[0].decode("utf-8", "surrogatepass") == "a\ud800b"


def test_save_note_gives_up_after_the_wait(pair, caplog):
    satellite, fake, serving = pair
    started = time.monotonic()

    assert satellite.save_note("Bins", "x", wait=0.2) is None
    assert 0.2 <= time.monotonic() - started < 2
    # An answer which comes too late is ignored, and neither an error nor kept.
    id = fake.messages("save_note")[0]["id"]
    with caplog.at_level(logging.WARNING):
        fake.say("note_saved", id=id, file="Bins.txt")
        fake.say("ping")
        assert wait_for(lambda: fake.messages("pong"))
    assert caplog.records == []
    assert satellite._note_waits == {}


def test_notes_are_told_apart_by_their_ids(pair):
    satellite, fake, serving = pair
    results = {}

    def ask(title):
        results[title] = satellite.save_note(title, "x", wait=5)

    threads = [threading.Thread(target=ask, args=(title,)) for title in ("one", "two")]
    for thread in threads:
        thread.start()
    assert wait_for(lambda: len(fake.messages("save_note")) == 2)
    # Answered the other way round to how they were asked.
    for message in reversed(fake.messages("save_note")):
        fake.say("note_saved", id=message["id"], file=f"{message['title']}.txt")
    for thread in threads:
        thread.join(5)
    assert results == {"one": {"file": "one.txt"}, "two": {"file": "two.txt"}}


def test_an_answer_for_another_note_doesnt_end_the_wait(pair):
    satellite, fake, serving = pair
    results = []
    thread = threading.Thread(target=lambda: results.append(satellite.save_note("Bins", "x", wait=0.3)))
    thread.start()
    assert wait_for(lambda: fake.messages("save_note"))
    fake.say("note_saved", id="note-nobody-asked-for", file="Other.txt")
    thread.join(5)
    assert results == [None]


def test_an_answer_which_says_nothing_is_ignored(pair, caplog):
    satellite, fake, serving = pair
    results = []
    thread = threading.Thread(target=lambda: results.append(satellite.save_note("Bins", "x", wait=0.3)))
    thread.start()
    assert wait_for(lambda: fake.messages("save_note"))
    with caplog.at_level(logging.WARNING):
        fake.say("note_saved", id=fake.messages("save_note")[0]["id"], file=5)
        thread.join(5)
    assert results == [None]
    assert "without saying" in caplog.text


def test_save_note_ends_when_the_satellite_goes(pair):
    satellite, fake, serving = pair
    results = []
    started = time.monotonic()
    thread = threading.Thread(target=lambda: results.append(satellite.save_note("Bins", "x", wait=30)))
    thread.start()
    assert wait_for(lambda: fake.messages("save_note"))

    fake.go()
    thread.join(5)

    assert results == [None]
    assert time.monotonic() - started < 5
    assert satellite._note_waits == {}


def test_a_note_for_a_satellite_which_has_gone_isnt_waited_for(pair):
    satellite, fake, serving = pair
    satellite.close()
    started = time.monotonic()

    assert satellite.save_note("Bins", "x", wait=30) is None
    assert time.monotonic() - started < 2
    assert fake.messages("save_note") == []


def test_the_buttons_of_windows_media_controls_are_passed_on():
    satellite, fake, serving = made = connect(media_controls=True)
    try:
        for action in ("play", "pause", "next", "previous"):
            fake.say("media", action=action)
        assert [satellite.media_presses.get(timeout=2) for _ in range(4)] == ["play", "pause", "next", "previous"]
    finally:
        disconnect(*made)


def test_a_media_button_which_isnt_one_of_the_four_is_dropped(caplog):
    satellite, fake, serving = made = connect(media_controls=True)
    try:
        with caplog.at_level(logging.WARNING):
            for action in ("stop", "PLAY", 3, None, ["pause"]):
                fake.say("media", action=action)
            fake.say("media")
            fake.say("media", action="next")
            assert satellite.media_presses.get(timeout=2) == "next"
        assert satellite.media_presses.empty()
        assert len([record for record in caplog.records if "media" in record.getMessage()]) == 6
        # The connection is kept.
        assert satellite.connected
    finally:
        disconnect(*made)


def test_media_buttons_from_a_satellite_which_didnt_say_it_has_them_are_dropped(pair):
    satellite, fake, serving = pair
    fake.say("media", action="pause")
    fake.say("ping")
    assert wait_for(lambda: fake.messages("pong"))
    assert satellite.media_presses.empty()


def test_what_plays_reaches_the_satellite_with_its_cover_only_when_given(pair):
    satellite, fake, serving = pair
    assert satellite.send_now_playing("playing", "Song", "Band", "Album", cover=b"jpeg")
    assert satellite.send_now_playing("paused", "Song", "Band", "Album")
    assert satellite.send_now_playing("paused", "Song", "Band", "Album", cover=None)
    assert satellite.send_now_playing("stopped", "", "", "")
    assert wait_for(lambda: len(fake.messages("now_playing")) == 4)
    assert [message for message, _ in fake.received if message["type"] == "now_playing"] == [
        {"type": "now_playing", "state": "playing", "track": "Song", "artist": "Band", "album": "Album",
         "cover": True, "bytes": 4},
        {"type": "now_playing", "state": "paused", "track": "Song", "artist": "Band", "album": "Album"},
        {"type": "now_playing", "state": "paused", "track": "Song", "artist": "Band", "album": "Album",
         "cover": False},
        {"type": "now_playing", "state": "stopped", "track": "", "artist": "", "album": ""}]
    assert fake.payloads("now_playing") == [b"jpeg", b"", b"", b""]


def test_long_names_in_any_script_are_cut_to_fit_one_line(pair):
    # A character outside the Basic Multilingual Plane is 12 bytes of JSON, as a pair of
    # \u escapes - so 500 of each, uncut, would be far past the line the satellite reads,
    # and it would drop the connection.
    satellite, fake, serving = pair
    note = "\U0001F3B5"
    assert satellite.send_now_playing("playing", note * 500, note * 500, note * 500, cover=b"jpeg")
    assert wait_for(lambda: fake.messages("now_playing"))
    [message] = fake.messages("now_playing")
    assert message["track"] == message["artist"] == message["album"] == note * remote.MOST_TEXT
    assert len(json.dumps(message).encode()) + 1 <= protocol.MAX_LINE
    assert fake.payloads("now_playing") == [b"jpeg"]
    # And the connection carries on.
    fake.say("ping")
    assert wait_for(lambda: fake.messages("pong"))
    assert satellite.connected


def test_a_follow_up_is_heard_until_the_brain_stops_listening(pair):
    satellite, fake, serving = pair
    answer = np.arange(1600, dtype=np.int16)

    feed = satellite.listen()
    assert wait_for(lambda: fake.messages("listen"))
    fake.say("audio", answer.tobytes())
    assert np.array_equal(feed.listen(start=0).read(1600), answer)

    satellite.stop_listening(feed)
    assert wait_for(lambda: fake.messages("stop_listening"))
    # Already on its way as the brain stopped listening, so it goes nowhere.
    fake.say("audio", answer.tobytes())
    fake.say("ping")
    assert wait_for(lambda: fake.messages("pong"))
    assert feed.position == 1600
    assert ends_at_once(feed)


@pytest.fixture
def talker():
    """A satellite which said it knows conversations."""
    made = connect(conversations=True)
    yield made
    disconnect(*made)


def test_an_open_listen_is_asked_for_open_and_its_feed_has_no_limit(talker):
    satellite, fake, serving = talker
    assert satellite.can_converse
    feed = satellite.listen(open=True)
    assert wait_for(lambda: fake.messages("listen"))
    (listen,) = fake.messages("listen")
    assert listen["open"] is True and listen["recording"].startswith("listen-")

    # More than the 40 seconds an ordinary feed keeps, and then some, in the biggest messages there are.
    piece = np.full(protocol.MAX_AUDIO // 2, 7, dtype=np.int16)
    for _ in range(9):
        fake.say("audio", piece.tobytes())
    assert wait_for(lambda: feed.position == 9 * len(piece))
    assert 9 * len(piece) > recording.MAX_FEED_SECONDS * 16000
    assert not feed.ended and feed.open
    # What it holds doesn't go on growing with it - see AudioFeed.
    assert len(feed._samples) <= 2 * recording.OPEN_KEEP_SECONDS * 16000

    satellite.stop_listening(feed)
    assert wait_for(lambda: fake.messages("stop_listening"))
    assert fake.messages("stop_listening") == [{"type": "stop_listening", "recording": listen["recording"]}]


def test_an_ordinary_listen_is_still_asked_for_as_it_always_was(talker):
    satellite, fake, serving = talker
    feed = satellite.listen()
    assert wait_for(lambda: fake.messages("listen"))
    assert "open" not in fake.messages("listen")[0] and not feed.open
    piece = np.full(protocol.MAX_AUDIO // 2, 7, dtype=np.int16)
    for _ in range(5):
        fake.say("audio", piece.tobytes())
    fake.say("ping")
    assert wait_for(lambda: fake.messages("pong"))
    # Dropped past its cap, as it always was.
    assert feed.position == round(recording.MAX_FEED_SECONDS * 16000)


def test_a_wakes_feed_has_no_limit_only_in_a_conversation_there(talker):
    satellite, fake, serving = talker
    piece = np.full(protocol.MAX_AUDIO // 2, 7, dtype=np.int16)
    wakes = itertools.count(1)

    def wake_and_send():
        made = next(wakes)
        fake.say("wake", talked_over=True, recording=f"wake-{made}")
        wake = satellite.wakes.get(timeout=2)
        for _ in range(9):
            fake.say("audio", piece.tobytes())
        # Everything sent before it has been taken once it is answered.
        fake.say("ping")
        assert wait_for(lambda: len(fake.messages("pong")) == made)
        return wake["feed"]

    # Capped as it always was, outside a conversation.
    feed = wake_and_send()
    assert not feed.open and feed.position == round(recording.MAX_FEED_SECONDS * 16000)

    # In one there, a turn the wake word or a press begins - over a reply, say - is as long as the satellite,
    # told it is on, lets it be, up to the longest turn which can be set. It isn't open, which keeps only the
    # last of what it's fed: its start is still there however long it waits to be read, through a long think.
    assert satellite.set_conversation(True) is True
    feed = wake_and_send()
    assert not feed.open and not feed.ended and feed.position == 9 * len(piece)
    assert 9 * len(piece) > 2 * recording.OPEN_KEEP_SECONDS * 16000
    assert (feed.listen(start=0).read(len(piece)) == 7).all()
    longest = LIMITS[("conversation", "longest_turn")].high
    assert recording.MAX_TURN_FEED_SECONDS * 16000 >= longest * 16000 + protocol.MAX_AUDIO // 2

    # And capped again once it has ended.
    assert satellite.set_conversation(False) is True
    feed = wake_and_send()
    assert not feed.open and feed.position == round(recording.MAX_FEED_SECONDS * 16000)


def test_a_satellite_which_doesnt_know_conversations_is_never_sent_them(pair):
    satellite, fake, serving = pair
    assert not satellite.can_converse
    feed = satellite.listen(open=True)
    assert wait_for(lambda: fake.messages("listen"))
    # An ordinary listen, in the feed and as it is asked for.
    assert fake.messages("listen")[0].keys() == {"type", "recording"} and not feed.open
    assert satellite.set_conversation(True) is False
    fake.say("ping")
    assert wait_for(lambda: fake.messages("pong"))
    assert fake.messages("conversation") == []
    assert satellite.connected


def test_the_brain_tells_a_satellite_a_conversation_has_started_and_ended(talker):
    satellite, fake, serving = talker
    assert satellite.set_conversation(True) is True
    assert satellite.set_conversation(False) is True
    assert wait_for(lambda: len(fake.messages("conversation")) == 2)
    assert fake.messages("conversation") == [{"type": "conversation", "on": True},
                                             {"type": "conversation", "on": False}]
    fake.go()
    assert wait_for(lambda: not satellite.connected)
    assert satellite.set_conversation(True) is False


def test_cut_in_sets_the_feeds_event(talker):
    satellite, fake, serving = talker
    feed = satellite.listen(open=True)
    assert wait_for(lambda: fake.messages("listen"))
    recording = fake.messages("listen")[0]["recording"]
    assert not feed.cut_in.is_set()
    # One for a recording which has been replaced isn't this one's.
    fake.say("cut_in", recording="listen-0")
    fake.say("ping")
    assert wait_for(lambda: len(fake.messages("pong")) == 1)
    assert not feed.cut_in.is_set()

    fake.say("cut_in", recording=recording)
    assert feed.cut_in.wait(2)


def test_a_cut_in_which_names_no_recording_is_for_the_one_being_sent(talker):
    satellite, fake, serving = talker
    feed = satellite.listen(open=True)
    fake.say("cut_in")
    assert feed.cut_in.wait(2)


def test_a_cut_in_with_nothing_listening_is_ignored(talker):
    satellite, fake, serving = talker
    fake.say("cut_in", recording="listen-1")
    fake.say("ping")
    assert wait_for(lambda: fake.messages("pong"))
    assert satellite.connected


def test_echo_ready_is_false_until_said(talker):
    satellite, fake, serving = talker
    assert satellite.echo_ready is False
    fake.say("echo_ready", ready=True)
    assert wait_for(lambda: satellite.echo_ready is True)
    fake.say("echo_ready", ready=False)
    assert wait_for(lambda: satellite.echo_ready is False)


def test_an_echo_ready_which_isnt_true_or_false_is_left_alone(talker, caplog):
    satellite, fake, serving = talker
    fake.say("echo_ready", ready=True)
    assert wait_for(lambda: satellite.echo_ready is True)
    with caplog.at_level(logging.WARNING):
        fake.say("echo_ready", ready="yes")
        fake.say("echo_ready")
        fake.say("ping")
        assert wait_for(lambda: fake.messages("pong"))
    assert satellite.echo_ready is True and caplog.text.count("echo_ready without true or false") == 2


def test_a_follow_up_is_numbered_and_stopping_it_says_which(pair):
    satellite, fake, serving = pair
    feed = satellite.listen()
    assert wait_for(lambda: fake.messages("listen"))
    (listen,) = fake.messages("listen")
    # Never a number the satellite gives a wake's recording, "wake-1" on, so a stop for
    # one can't be taken for the other.
    assert listen["recording"].startswith("listen-")

    satellite.stop_listening(feed)
    assert wait_for(lambda: fake.messages("stop_listening"))
    assert fake.messages("stop_listening") == [{"type": "stop_listening", "recording": listen["recording"]}]
    satellite.listen()
    assert wait_for(lambda: len(fake.messages("listen")) == 2)
    assert fake.messages("listen")[1]["recording"] != listen["recording"]


def test_stopping_a_wake_says_which_recording_the_satellite_said_it_was(pair):
    satellite, fake, serving = pair
    fake.say("wake", talked_over=False, recording="wake-7")
    feed = satellite.wakes.get(timeout=2)["feed"]

    satellite.stop_listening(feed)
    assert wait_for(lambda: fake.messages("stop_listening"))
    assert fake.messages("stop_listening") == [{"type": "stop_listening", "recording": "wake-7"}]
    assert ends_at_once(feed)


@pytest.mark.parametrize("said", [{}, {"recording": 7}, {"recording": ["wake-7"]}])
def test_a_satellite_which_doesnt_number_its_recordings_is_stopped_as_it_always_was(pair, said):
    # Today's Pi says no number with its wake, so is sent nothing it hasn't been before.
    satellite, fake, serving = pair
    fake.say("wake", talked_over=False, **said)
    feed = satellite.wakes.get(timeout=2)["feed"]

    satellite.stop_listening(feed)
    assert wait_for(lambda: fake.messages("stop_listening"))
    assert fake.messages("stop_listening") == [{"type": "stop_listening"}]


@pytest.mark.parametrize("numbered", [True, False])
def test_stopping_a_recording_the_satellite_ended_itself_leaves_its_next_one_going(pair, numbered):
    # The first ran to its limit, and the wake word was said again before the brain
    # stopped listening to it - so stopping it then ended the second, on both sides.
    satellite, fake, serving = pair
    first = {"recording": "wake-1"} if numbered else {}
    second = {"recording": "wake-2"} if numbered else {}
    fake.say("wake", talked_over=False, **first)
    fake.say("audio", np.ones(1600, np.int16).tobytes())
    fake.say("audio_end", reason="limit")
    fake.say("wake", talked_over=False, **second)
    earlier = satellite.wakes.get(timeout=2)["feed"]
    later = satellite.wakes.get(timeout=2)["feed"]

    satellite.stop_listening(earlier)
    fake.say("audio", np.ones(1600, np.int16).tobytes())
    fake.say("ping")
    assert wait_for(lambda: fake.messages("pong"))
    assert later.position == 1600
    # Nothing to stop: the satellite ended it, and a stop now could only end the next.
    assert fake.messages("stop_listening") == []

    satellite.stop_listening(later)
    assert wait_for(lambda: fake.messages("stop_listening"))
    assert fake.messages("stop_listening") == [{"type": "stop_listening", **second}]
    assert ends_at_once(later)


def test_the_brain_sends_the_fade(pair):
    satellite, fake, serving = pair
    song = np.zeros((100, 2), dtype=np.float32)
    playback = satellite.speaker.play_stream(iter([song]), kind="reply")
    assert wait_for(lambda: fake.messages("play_stream"))

    playback.fade_to(0.3, 0.05)

    assert wait_for(lambda: fake.messages("set_playback"))
    assert fake.messages("set_playback") == [{"type": "set_playback", "id": playback.id, "volume": 0.3,
                                              "fade": 0.05}]
    # The brain's own record is where it is going.
    assert playback.volume == 0.3


def test_pausing_music_tells_the_satellite(pair):
    satellite, fake, serving = pair
    song = np.zeros((100, 2), dtype=np.float32)
    playback = satellite.speaker.play_stream(iter([song]), kind="music", paused=True)
    assert wait_for(lambda: fake.messages("play_stream"))
    assert fake.messages("play_stream")[0]["paused"] is True

    playback.paused = False
    playback.volume = 0.3
    playback.paused = True

    assert wait_for(lambda: len(fake.messages("set_playback")) == 3)
    assert fake.messages("set_playback") == [{"type": "set_playback", "id": playback.id, "paused": False},
                                             {"type": "set_playback", "id": playback.id, "volume": 0.3},
                                             {"type": "set_playback", "id": playback.id, "paused": True}]
    assert playback.paused is True
    assert playback.volume == 0.3


def test_a_late_played_from_an_old_connection_finishes_nothing_on_the_new_one():
    # The satellite reports a sound's end on whichever link it has by then, so an id which
    # the new connection reused would end the new connection's sound.
    old_connection = connect()
    old = old_connection[0].speaker.play_file("chime.wav")
    disconnect(*old_connection)

    satellite, fake, serving = connect()
    try:
        new = satellite.speaker.play_file("chime.wav")
        assert new.id != old.id

        fake.say("played", id=old.id, stopped=False, position=100)
        fake.say("ping")
        assert wait_for(lambda: fake.messages("pong"))
        assert not new.done

        fake.say("played", id=new.id, stopped=False, position=200)
        assert new.wait(2)
    finally:
        disconnect(satellite, fake, serving)


def test_stopping_one_sound_stops_only_that_one(pair):
    satellite, fake, serving = pair
    beep = satellite.speaker.play_file("volume_beep.wav", volume=0.5)
    reply = satellite.speaker.play_file("reply.wav")

    beep.stop()
    assert beep.done and not reply.done
    # Already stopped, so there's nothing more to say.
    beep.stop()

    assert wait_for(lambda: fake.messages("stop"))
    time.sleep(0.05)
    assert fake.messages("stop") == [{"type": "stop", "id": beep.id}]


def test_stopping_tells_the_satellite_what_to_stop(pair):
    satellite, fake, serving = pair
    speaker = satellite.speaker
    reply = speaker.play_file("reply.wav")
    alarm = speaker.play_file("alarm.wav", kind="announcement")

    speaker.stop(["reply"])
    assert reply.done and not alarm.done
    speaker.silence("announcement")
    assert alarm.done
    speaker.allow("announcement")
    speaker.stop()

    assert wait_for(lambda: len(fake.received) == 6)
    assert [message for message, _ in fake.received[2:]] == [{"type": "stop", "kinds": ["reply"]},
                                                              {"type": "silence", "kind": "announcement"},
                                                              {"type": "allow", "kind": "announcement"},
                                                              {"type": "stop", "kinds": None}]


STOPPING = pytest.mark.parametrize("stopping", [
    lambda speaker: speaker.stop(),
    lambda speaker: speaker.stop(["reply"]),
    lambda speaker: speaker.silence("reply"),
], ids=["everything", "a kind", "silence"])


@STOPPING
def test_a_sound_started_while_a_stop_is_sent_isnt_finished_by_it(stopping):
    # Another thread's reply starts after the satellite was told to stop, so it plays
    # there - and mustn't be taken as stopped here, where it would be waited on no more.
    started = []

    def send(kind, payload=b"", /, **fields):
        if kind in ("stop", "silence") and not started:
            started.append(speaker.play_file("late.wav"))
        return True

    speaker = remote.RemoteSpeaker(send, 48000)
    earlier = speaker.play_file("earlier.wav")

    stopping(speaker)

    assert earlier.done
    assert not started[0].done


@STOPPING
def test_a_sound_whose_play_is_still_being_sent_isnt_finished_by_a_stop_which_gets_there_first(stopping):
    # Another thread's reply is kept by its id, and its play message is queued behind a
    # stop from a thread which wins the satellite's lock first: the stop reaches the
    # satellite before the play, so the sound plays there - or, after a silence, is refused
    # there and reported finished - and mustn't be taken here as stopped.
    wire = []
    started = []

    def send(kind, payload=b"", /, **fields):
        if kind == "play_sound" and not started:
            started.append(True)
            # The stop gets in between the sound being kept and its message being sent.
            stopping(speaker)
        wire.append(kind)
        return True

    speaker = remote.RemoteSpeaker(send, 48000)

    late = speaker.play_file("late.wav")

    assert wire in (["stop", "play_sound"], ["silence", "play_sound"])
    assert not late.done


def test_state_alarms_the_phrase_and_restarts_reach_the_satellite(pair):
    satellite, fake, serving = pair
    volumes = {"general": 0.5, "alarm": 1.0, "music": 0.3}
    entries = [{"id": "kitchen@1700000000", "due": 1700000000.0, "kind": "timer", "sound": "timer.wav"}]
    phrase = np.arange(100, dtype=np.int16).tobytes()

    satellite.set_state(volumes, True)
    satellite.send_alarms(entries)
    satellite.send_phrase(phrase, 22050)
    satellite.restart()

    assert wait_for(lambda: len(fake.received) == 4)
    assert fake.received == [({"type": "state", "volumes": volumes, "muted": True}, b""),
                             ({"type": "alarms", "entries": entries}, b""),
                             ({"type": "phrase", "rate": 22050, "bytes": 200}, phrase),
                             ({"type": "restart"}, b"")]


def test_the_longest_request_goes_with_the_state_only_when_given(pair):
    satellite, fake, serving = pair
    volumes = {"general": 0.5, "alarm": 1.0, "music": 0.3}

    satellite.set_state(volumes, False)
    satellite.set_state(volumes, False, 20.0)

    assert wait_for(lambda: len(fake.received) == 2)
    assert fake.received == [({"type": "state", "volumes": volumes, "muted": False}, b""),
                             ({"type": "state", "volumes": volumes, "muted": False, "longest_request": 20.0}, b"")]


def test_the_wake_confidence_goes_with_the_state_only_when_given(pair):
    # Just another field, so an older satellite's state is as it was, and one which doesn't know it ignores it.
    satellite, fake, serving = pair
    volumes = {"general": 0.5, "alarm": 1.0, "music": 0.3}

    satellite.set_state(volumes, False, 20.0)
    satellite.set_state(volumes, False, 20.0, wake_confidence=0.75)
    satellite.set_state(volumes, True, wake_confidence=0.5)

    assert wait_for(lambda: len(fake.received) == 3)
    assert fake.received == [
        ({"type": "state", "volumes": volumes, "muted": False, "longest_request": 20.0}, b""),
        ({"type": "state", "volumes": volumes, "muted": False, "longest_request": 20.0, "wake_confidence": 0.75}, b""),
        ({"type": "state", "volumes": volumes, "muted": True, "wake_confidence": 0.5}, b"")]


def test_the_voice_check_goes_with_the_state_only_when_given(pair):
    # Just another field, as the wake confidence is, so an older satellite ignores it.
    satellite, fake, serving = pair
    volumes = {"general": 0.5, "alarm": 1.0, "music": 0.3}

    satellite.set_state(volumes, False, 20.0, wake_confidence=0.75)
    satellite.set_state(volumes, False, 20.0, wake_confidence=0.75, voice_check=0.2)
    satellite.set_state(volumes, True, voice_check=0)

    assert wait_for(lambda: len(fake.received) == 3)
    assert fake.received == [
        ({"type": "state", "volumes": volumes, "muted": False, "longest_request": 20.0, "wake_confidence": 0.75}, b""),
        ({"type": "state", "volumes": volumes, "muted": False, "longest_request": 20.0, "wake_confidence": 0.75,
          "voice_check": 0.2}, b""),
        ({"type": "state", "volumes": volumes, "muted": True, "voice_check": 0}, b"")]


def test_the_screen_orb_goes_with_the_state_only_when_given(pair):
    # Just another field, so a satellite which doesn't show one - or an older one - is sent its state as it was.
    satellite, fake, serving = pair
    volumes = {"general": 0.5, "alarm": 1.0, "music": 0.3}
    orb = {"on": True, "place": "top left", "size": "small"}

    satellite.set_state(volumes, False)
    satellite.set_state(volumes, False, screen_orb=orb)

    assert wait_for(lambda: len(fake.received) == 2)
    assert fake.received == [({"type": "state", "volumes": volumes, "muted": False}, b""),
                             ({"type": "state", "volumes": volumes, "muted": False, "screen_orb": orb}, b"")]


def test_app_goes_in_the_state_message_only_when_given(pair):
    # Just another field, so a satellite which has no app - or an older one - is sent its state as it was.
    satellite, fake, serving = pair
    volumes = {"general": 0.5, "alarm": 1.0, "music": 0.3}
    app = {"shortcut": "ctrl+alt+n", "page": "", "mic_mute_minutes": 60}

    satellite.set_state(volumes, False)
    satellite.set_state(volumes, False, app=app)

    assert wait_for(lambda: len(fake.received) == 2)
    assert fake.received == [({"type": "state", "volumes": volumes, "muted": False}, b""),
                             ({"type": "state", "volumes": volumes, "muted": False, "app": app}, b"")]


def test_shortcut_taken_is_passed_to_the_session(pair, caplog):
    # The satellite keeps it, one for each connection, and the session says what its satellite does.
    satellite, fake, serving = pair
    assert satellite.shortcut_taken is False
    with caplog.at_level(logging.WARNING):
        fake.say("shortcut_taken", taken=True)
        assert wait_for(lambda: satellite.shortcut_taken)
        # Not a yes or no, so it's not believed, and the last one stands.
        fake.say("shortcut_taken", taken="yes")
        fake.say("shortcut_taken")
        fake.say("ping")
        assert wait_for(lambda: fake.messages("pong"))
        assert satellite.shortcut_taken is True
        assert len([record for record in caplog.records if "shortcut" in record.getMessage()]) == 2
        fake.say("shortcut_taken", taken=False)
        assert wait_for(lambda: not satellite.shortcut_taken)
    assert satellite.connected


def test_messages_sent_from_several_threads_arrive_whole_and_in_order():
    # Music, speech and the session's own messages are all sent from threads of their own.
    satellite, fake, serving = connect(Chunky)
    sent = []

    def send_many(number):
        for n in range(20):
            sent.append(satellite.send("stream_audio", bytes([number]) * 1500, id=str(number), n=n))

    try:
        threads = [threading.Thread(target=send_many, args=(number,)) for number in range(1, 5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        assert len(sent) == 80 and all(sent)
        assert wait_for(lambda: len(fake.received) == 80)
        for number in range(1, 5):
            mine = [(message, payload) for message, payload in fake.received if message["id"] == str(number)]
            assert [message["n"] for message, _ in mine] == list(range(20))
            assert all(payload == bytes([number]) * 1500 for _, payload in mine)
    finally:
        disconnect(satellite, fake, serving)


def test_a_send_which_fails_ends_what_was_playing_and_whatever_is_started_after():
    stream = Breakable()
    satellite = RemoteSatellite(stream, dict(HELLO))
    speaker = satellite.speaker
    # Endless, and held back by most_ahead, so only the satellite going can end it.
    song = Pieces(np.zeros((100, 2), dtype=np.float32) for _ in iter(int, 1))
    music = speaker.play_stream(song, sample_rate=1000, kind="music", most_ahead=0.5)
    chime = speaker.play_file("chime.wav")
    assert satellite.connected and not music.done and not chime.done

    stream.broken = True
    later = speaker.play_file("later.wav")

    # The failed send is the brain finding the satellite gone: nothing waits on it any more.
    assert not satellite.connected
    assert later.done and chime.done and music.wait(2)
    assert song.closed.wait(2)
    assert satellite.send("restart") is False


def test_a_stream_whose_pieces_fail_part_way_still_ends_where_it_got_to(pair, caplog):
    satellite, fake, serving = pair
    pieces = Failing([np.zeros(2205, dtype=np.float32)])

    with caplog.at_level(logging.ERROR, logger=remote.__name__):
        playback = satellite.speaker.play_stream(pieces, sample_rate=22050)
        assert wait_for(lambda: fake.messages("stream_end"))

    assert [message["type"] for message, _ in fake.received] == ["play_stream", "stream_audio", "stream_end"]
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.ERROR, "A streamed reply sound failed part way: the voice failed")]
    assert pieces.closed.is_set()
    # Not lost with it: the satellite plays what it has, and says so.
    assert satellite.connected and not playback.done
    fake.say("played", id=playback.id, stopped=False, position=2205)
    assert playback.wait(2)


def test_audio_too_long_for_the_satellite_isnt_sent(pair):
    satellite, fake, serving = pair
    # The satellite would drop the connection over it.
    assert satellite.send_phrase(b"\x00" * (protocol.MAX_AUDIO + 2), 22050) is False
    satellite.restart()
    assert wait_for(lambda: fake.received)
    assert [message["type"] for message, _ in fake.received] == ["restart"]
    assert satellite.connected


def test_what_rang_while_away_is_passed_on(pair):
    satellite, fake, serving = pair
    fake.say("away", rang=["kitchen@1700000000", "wake-up@1700003600"], dismissed=["wake-up@1700003600"],
             ringing=["kitchen@1700000000"])
    # Or without what's still ringing, from a satellite which doesn't say.
    fake.say("away", rang=["kitchen@1700000000"], dismissed=[])
    assert satellite.away_reports.get(timeout=2) == {"rang": ["kitchen@1700000000", "wake-up@1700003600"],
                                                     "dismissed": ["wake-up@1700003600"],
                                                     "ringing": ["kitchen@1700000000"]}
    assert satellite.away_reports.get(timeout=2) == {"rang": ["kitchen@1700000000"], "dismissed": [], "ringing": []}


def test_pings_are_answered(pair):
    satellite, fake, serving = pair
    fake.say("ping")
    assert wait_for(lambda: fake.messages("pong"))


def closed(ours, theirs):
    theirs.close()


def reset(ours, theirs):
    # Closing with no time to linger resets the connection rather than closing it.
    theirs.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    theirs.close()
    # The reset has arrived once there's something to read.
    assert select.select([ours], [], [], 5)[0]


@pytest.mark.parametrize("hang_up, family", [(closed, socket.AF_UNIX), (reset, socket.AF_INET)],
                         ids=["broken pipe", "reset"])
def test_a_send_which_finds_the_satellite_already_gone_says_so_plainly(hang_up, family, caplog):
    # As after the brain was paused: the satellite gave up on it and hung up, leaving
    # pings for the brain to answer once it carried on - with a pipe broken, or reset.
    caplog.set_level(logging.INFO, logger=remote.__name__)
    if family == socket.AF_UNIX:
        ours, theirs = socket.socketpair()
    else:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            theirs = socket.create_connection(listener.getsockname(), timeout=5)
            ours, _ = listener.accept()
    ours.settimeout(5)
    satellite = RemoteSatellite(satellite_link.Stream(ours), dict(HELLO))
    hang_up(ours, theirs)
    try:
        assert satellite.send("pong") is False
    finally:
        ours.close()
    assert not satellite.connected
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.INFO, "The bedroom satellite had already hung up, so 'pong' wasn't sent")]


def test_a_send_which_fails_otherwise_is_a_warning(caplog):
    class Stalled:
        def write(self, data):
            raise TimeoutError("timed out")

        def flush(self):
            pass

    satellite = RemoteSatellite(Stalled(), dict(HELLO))
    assert satellite.send("restart") is False
    assert not satellite.connected
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.WARNING, "Couldn't send 'restart' to the bedroom satellite (timed out)")]


def test_when_the_satellite_goes_every_playback_and_feed_ends(pair):
    satellite, fake, serving = pair
    speaker = satellite.speaker
    # Endless, and held back by most_ahead, so only the satellite going can end it.
    song = Pieces(np.zeros((100, 2), dtype=np.float32) for _ in iter(int, 1))
    music = speaker.play_stream(song, sample_rate=1000, kind="music", most_ahead=0.5)
    chime = speaker.play_file("chime.wav")
    fake.say("wake", talked_over=False)
    feed = satellite.wakes.get(timeout=2)["feed"]
    assert wait_for(lambda: fake.messages("play_sound"))

    fake.go()

    serving.join(2)
    assert not serving.is_alive()
    assert not satellite.connected
    assert music.done and chime.done
    assert music.wait(0) and chime.wait(0)
    assert song.closed.wait(2)
    assert ends_at_once(feed)


def test_nothing_heard_for_15_seconds_counts_as_the_satellite_gone(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(remote, "monotonic", lambda: now[0])
    satellite, fake, serving = connect()
    try:
        now[0] += 14.0
        fake.say("ping")
        assert wait_for(lambda: fake.messages("pong"))
        # Fourteen seconds on from the ping, which was the last heard.
        now[0] += 14.0
        time.sleep(0.2)
        assert satellite.connected
        now[0] += 1.5
        serving.join(2)
        assert not serving.is_alive()
        assert not satellite.connected
    finally:
        disconnect(satellite, fake, serving)


def test_closing_lets_the_satellite_go(pair):
    satellite, fake, serving = pair
    satellite.close()
    serving.join(2)
    assert not serving.is_alive()
    assert not satellite.connected


def test_nothing_plays_on_a_satellite_which_isnt_connected(pair):
    satellite, fake, serving = pair
    speaker = satellite.speaker
    satellite.close()
    serving.join(2)

    speech = Pieces([np.zeros(100, dtype=np.float32)])
    played = [speaker.play(np.zeros(100, dtype=np.float32)), speaker.play_stream(speech),
              speaker.play_file("chime.wav")]
    # Already finished, as Speaker's are with no speaker to play on, so whatever
    # waits on one doesn't need to check for None first.
    assert all(playback.done and playback.wait(0) and playback.duration == 0 for playback in played)
    # Closed, so whatever was making it stops.
    assert speech.closed.is_set()
    for playback in played:
        playback.paused = True
        playback.stop()
    assert satellite.send("restart") is False
    assert ends_at_once(satellite.listen())
    time.sleep(0.1)
    assert fake.received == []


def test_the_speaker_says_when_it_starts_and_stops_speaking():
    sent = []
    speaker = remote.RemoteSpeaker(lambda *args, **fields: sent.append((args, fields)) or True, 48000)
    heard = []
    speaker.on_speech = heard.append

    reply = speaker.play_stream(iter([]), kind="reply")
    assert heard == [True] and speaker.speaking
    # A second while the first goes on changes nothing; music and the satellite's own sounds never count.
    speaker.play_stream(iter([]), kind="announcement")
    speaker.play_stream(iter([]), kind="music")
    speaker.play_file("listening.wav")
    assert heard == [True]
    speaker._played(reply.id, None)
    assert heard == [True]
    for playback in list(speaker._playing.values()):
        speaker._played(playback.id, None)
    assert heard == [True, False]


def test_the_speaker_stops_speaking_when_the_satellite_goes():
    speaker = remote.RemoteSpeaker(lambda *args, **fields: True, 48000)
    heard = []
    speaker.on_speech = heard.append
    speaker.play_stream(iter([]), kind="reply")
    speaker._gone()
    assert heard == [True, False]


def test_speech_the_satellite_couldnt_be_sent_never_counts():
    speaker = remote.RemoteSpeaker(lambda *args, **fields: False, 48000)
    heard = []
    speaker.on_speech = heard.append
    speaker.play_stream(iter([]), kind="reply")
    assert heard == []
    assert not speaker.speaking


def say_json(fake, kind, value):
    fake.say(kind, json.dumps(value).encode())


def test_apps_passed_on_validated(caplog):
    made = connect(opens=True)
    satellite, fake, serving = made
    try:
        got = []
        satellite.on_apps = lambda name, apps: got.append((name, apps))
        good = {"id": "firefox", "name": "Firefox", "kind": "app"}
        with caplog.at_level(logging.WARNING):
            say_json(fake, "apps", [good, {"id": "x", "name": "X", "kind": "exe"}])
            assert wait_for(lambda: got)
        assert got == [("bedroom", [good])]
        assert caplog.text.count("dropped") == 1
    finally:
        disconnect(*made)


def test_apps_that_are_not_a_list_are_ignored(caplog):
    made = connect(opens=True)
    satellite, fake, serving = made
    try:
        got = []
        satellite.on_apps = lambda name, apps: got.append(apps)
        say_json(fake, "apps", {"id": "x"})
        fake.say("apps", b"not json")
        fake.say("ping")
        assert wait_for(lambda: fake.messages("pong"))
        assert got == []
    finally:
        disconnect(*made)


def test_apps_ignored_without_opens(pair, caplog):
    satellite, fake, serving = pair
    got = []
    satellite.on_apps = lambda name, apps: got.append(apps)
    with caplog.at_level(logging.WARNING):
        say_json(fake, "apps", [{"id": "firefox", "name": "Firefox", "kind": "app"}])
        fake.say("ping")
        assert wait_for(lambda: fake.messages("pong"))
    assert got == []
    assert "without saying it opens" in caplog.text


def test_windows_passed_on_validated():
    made = connect(opens=True)
    satellite, fake, serving = made
    try:
        got = []
        satellite.on_windows = lambda name, windows: got.append((name, windows))
        say_json(fake, "windows", ["Calculator", 5, "x" * 201, "Notepad"])
        assert wait_for(lambda: got)
        assert got == [("bedroom", ["Calculator", "Notepad"])]
    finally:
        disconnect(*made)


def test_windows_ignored_without_opens(pair):
    satellite, fake, serving = pair
    got = []
    satellite.on_windows = lambda name, windows: got.append(windows)
    say_json(fake, "windows", ["Calculator"])
    fake.say("ping")
    assert wait_for(lambda: fake.messages("pong"))
    assert got == []


def ask_in_thread(satellite, results, *args, **kwargs):
    thread = threading.Thread(target=lambda: results.append(satellite.ask_computer(*args, **kwargs)))
    thread.start()
    return thread


def test_ask_computer_answered(pair):
    satellite, fake, serving = pair
    results = []
    thread = ask_in_thread(satellite, results, "open", wait=5, app="fortnite")
    assert wait_for(lambda: fake.messages("computer"))
    [message] = fake.messages("computer")
    assert message["action"] == "open" and message["app"] == "fortnite"
    fake.say("computer_done", id=message["id"], said="Opening Fortnite.")
    thread.join(5)
    assert results == [{"said": "Opening Fortnite."}]
    assert satellite._note_waits == {}


def test_ask_computer_passes_on_the_volume_and_error_fields(pair):
    satellite, fake, serving = pair
    results = []
    thread = ask_in_thread(satellite, results, "volume", wait=5, level=30)
    assert wait_for(lambda: fake.messages("computer"))
    fake.say("computer_done", id=fake.messages("computer")[0]["id"], level=30, muted=False, error="Nope.")
    thread.join(5)
    assert results == [{"level": 30, "muted": False, "error": "Nope."}]


def test_ask_ends_when_lost(pair):
    satellite, fake, serving = pair
    results = []
    started = time.monotonic()
    thread = ask_in_thread(satellite, results, "close_front", wait=30)
    assert wait_for(lambda: fake.messages("computer"))
    satellite._lost()
    thread.join(5)
    assert results == [None]
    assert time.monotonic() - started < 1


def test_ask_computer_late_answer_ignored(pair):
    satellite, fake, serving = pair
    assert satellite.ask_computer("close_front", wait=0.2) is None
    id = fake.messages("computer")[0]["id"]
    fake.say("computer_done", id=id, said="Closed.")
    fake.say("ping")
    assert wait_for(lambda: fake.messages("pong"))
    assert satellite._note_waits == {}


def test_computer_and_note_waits_are_told_apart(pair):
    satellite, fake, serving = pair
    results = []
    thread = ask_in_thread(satellite, results, "close_front", wait=0.3)
    assert wait_for(lambda: fake.messages("computer"))
    # A note's answer is no answer to the computer request.
    fake.say("note_saved", id=fake.messages("computer")[0]["id"], file="x.txt")
    thread.join(5)
    assert results == [None]


def test_ask_computer_for_a_satellite_which_has_gone_isnt_waited_for(pair):
    satellite, fake, serving = pair
    satellite.close()
    started = time.monotonic()
    assert satellite.ask_computer("close_front", wait=30) is None
    assert time.monotonic() - started < 2
    assert fake.messages("computer") == []


def _ask_approval_on_thread(satellite, id="a1", description="Remove the dentist", wait=5):
    """Asks for an approval on a thread of its own, returning the thread and the list its result goes in."""
    results = []
    thread = threading.Thread(target=lambda: results.append(satellite.ask_approval(id, description, wait)))
    thread.start()
    return thread, results


def test_the_satellite_says_it_approves():
    made = connect(approves=True)
    try:
        assert made[0].approves is True
    finally:
        disconnect(*made)
    made = connect()
    try:
        assert made[0].approves is False
    finally:
        disconnect(*made)


@pytest.mark.parametrize("approved", [True, False])
def test_ask_approval_sends_the_description_and_gives_the_answer(pair, approved):
    satellite, fake, serving = pair
    thread, results = _ask_approval_on_thread(satellite)
    assert wait_for(lambda: fake.messages("approval"))

    (message,) = fake.messages("approval")
    assert message["id"] == "approval-a1" and message["description"] == "Remove the dentist"
    fake.say("approval_answer", id="approval-a1", approved=approved)
    thread.join(5)

    assert results == [approved]
    assert satellite._note_waits == {}


def test_an_answer_for_an_id_never_asked_is_dropped(pair, caplog):
    satellite, fake, serving = pair
    thread, results = _ask_approval_on_thread(satellite, wait=0.3)
    assert wait_for(lambda: fake.messages("approval"))
    with caplog.at_level(logging.WARNING):
        fake.say("approval_answer", id="approval-other", approved=True)
        # A note's or computer's id isn't an approval's either.
        fake.say("approval_answer", id="note-1", approved=True)
        thread.join(5)
        fake.say("ping")
        assert wait_for(lambda: fake.messages("pong"))
    assert results == [None]
    assert len([r for r in caplog.records if "approval" in r.getMessage()]) == 2


def test_an_answer_counts_once(pair, caplog):
    satellite, fake, serving = pair
    thread, results = _ask_approval_on_thread(satellite)
    assert wait_for(lambda: fake.messages("approval"))
    fake.say("approval_answer", id="approval-a1", approved=True)
    thread.join(5)
    with caplog.at_level(logging.WARNING):
        fake.say("approval_answer", id="approval-a1", approved=False)
        fake.say("ping")
        assert wait_for(lambda: fake.messages("pong"))
    assert results == [True]
    assert len([r for r in caplog.records if "approval" in r.getMessage()]) == 1


def test_a_withdrawal_ends_the_wait_at_once(pair, caplog):
    satellite, fake, serving = pair
    started = time.monotonic()
    thread, results = _ask_approval_on_thread(satellite, wait=30)
    assert wait_for(lambda: fake.messages("approval"))

    satellite.withdraw_approval("a1")
    thread.join(5)

    assert results == [None] and time.monotonic() - started < 1
    assert satellite._note_waits == {}
    with caplog.at_level(logging.WARNING):
        fake.say("approval_answer", id="approval-a1", approved=True)
        fake.say("ping")
        assert wait_for(lambda: fake.messages("pong"))
    assert len([r for r in caplog.records if "approval" in r.getMessage()]) == 1


def test_no_answer_in_time_or_gone_gives_none(pair):
    satellite, fake, serving = pair
    started = time.monotonic()
    assert satellite.ask_approval("a1", "Remove it", 0.2) is None
    assert 0.2 <= time.monotonic() - started < 2

    thread, results = _ask_approval_on_thread(satellite, id="a2", wait=30)
    assert wait_for(lambda: len(fake.messages("approval")) == 2)
    satellite.close()
    thread.join(5)
    assert results == [None]
    assert time.monotonic() - started < 5
    assert satellite._note_waits == {}


def test_withdraw_sends_the_id(pair):
    satellite, fake, serving = pair
    assert satellite.withdraw_approval("a1") is True
    assert wait_for(lambda: fake.messages("approval_withdraw"))
    assert fake.messages("approval_withdraw") == [{"type": "approval_withdraw", "id": "approval-a1"}]


def test_withdraw_never_waits(pair):
    satellite, fake, serving = pair
    started = time.monotonic()
    satellite.withdraw_approval("a1")
    assert time.monotonic() - started < 1


def test_an_approval_already_withdrawn_isnt_sent(pair):
    satellite, fake, serving = pair
    satellite.withdraw_approval("a1")
    started = time.monotonic()

    assert satellite.ask_approval("a1", "Remove it", 30) is None

    assert time.monotonic() - started < 2
    assert fake.messages("approval") == []


def test_only_the_last_fifty_withdrawn_are_remembered(pair):
    satellite, fake, serving = pair
    for number in range(60):
        satellite.withdraw_approval(f"w{number}")
    assert len(satellite._withdrawn) == 50
    # The oldest is forgotten, so is asked again; the newest isn't.
    assert satellite.ask_approval("w0", "x", 0.05) is None
    assert len(fake.messages("approval")) == 1
    assert satellite.ask_approval("w59", "x", 0.05) is None
    assert len(fake.messages("approval")) == 1


@pytest.mark.parametrize("approved", ["yes", 1, 0, None])
def test_an_answer_which_isnt_true_or_false_is_dropped(pair, caplog, approved):
    satellite, fake, serving = pair
    thread, results = _ask_approval_on_thread(satellite, wait=0.3)
    assert wait_for(lambda: fake.messages("approval"))
    with caplog.at_level(logging.WARNING):
        fake.say("approval_answer", id="approval-a1", approved=approved)
        thread.join(5)
    assert results == [None]
    assert "approval" in caplog.text


def test_a_pc_prompt_left_at_a_timeout_is_withdrawn(pair, tmp_path):
    """
    End to end, with the real clock: the PC's own wait and the service's waiter both end at
    the request's expiry, and when the PC's ends first no settle withdraws its prompt - so the
    prompt's own thread does. Nothing waits on the request here until after, so it's always so.
    """
    from voice_assistant.approvals import Approvals
    from voice_assistant.passkeys import PasskeyStore

    satellite, fake, serving = pair
    service = Approvals(lambda: {}, PasskeyStore(str(tmp_path / "passkeys.json")), approvers=lambda: [satellite])
    # A wait of under a second, not the settings' whole minutes.
    service._minutes = lambda name, default: 0.01 if name == "wait_minutes" else default
    try:
        request = service.request("Remove 'Dentist'")
        assert request.asked == ("bedroom",)
        assert wait_for(lambda: fake.messages("approval"))
        [asked] = fake.messages("approval")
        assert wait_for(lambda: fake.messages("approval_withdraw"))
        assert [each["id"] for each in fake.messages("approval_withdraw")] == [asked["id"]]
        assert request.outcome is None
        assert service.wait(request) == "timeout"
    finally:
        service.stop()
