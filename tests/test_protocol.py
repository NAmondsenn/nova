"""
Protocol Test
Checks messages between a Pi and the backend arrive as they were sent, and that
anything which isn't a message is refused rather than read on for ever.
"""

import io
import json
import logging
import socket
import threading

import pytest

from voice_assistant import protocol
from voice_assistant.machine import MACHINES


def test_a_message_and_its_audio_arrive_as_sent():
    stream = io.BytesIO()
    protocol.send(stream, "audio", b"\x01\x02\x03\x04")
    protocol.send(stream, "text", text="Pause")
    stream.seek(0)

    assert protocol.receive(stream) == ({"type": "audio", "bytes": 4}, b"\x01\x02\x03\x04")
    assert protocol.receive(stream) == ({"type": "text", "text": "Pause"}, b"")


def test_a_message_can_carry_a_field_named_kind():
    # As play_stream, play_sound, silence and allow all do.
    stream = io.BytesIO()
    protocol.send(stream, "silence", kind="announcement")
    stream.seek(0)

    assert protocol.receive(stream) == ({"type": "silence", "kind": "announcement"}, b"")


@pytest.mark.parametrize("sent", [
    b'{"type": "ping"',
    b"not a message\n",
    b'["ping"]\n',
    b'{"kind": "ping"}\n',
    b'{"type": "audio", "bytes": 999999999}\n',
    b'{"type": "audio", "bytes": -1}\n',
    b'{"type": "audio", "bytes": true}\n',
])
def test_what_isnt_a_message_is_refused(sent):
    with pytest.raises(protocol.ProtocolError):
        protocol.receive(io.BytesIO(sent))


def test_a_line_too_long_to_be_a_message_isnt_read_to_the_end():
    stream = io.BytesIO(b"x" * 100000 + b"\n")
    with pytest.raises(protocol.ProtocolError):
        protocol.receive(stream)
    assert stream.tell() <= protocol.MAX_LINE + 1


def test_the_other_side_going_is_told_apart_from_a_bad_message():
    with pytest.raises(ConnectionError):
        protocol.receive(io.BytesIO(b""))
    # Gone part way through the audio it said was coming.
    with pytest.raises(ConnectionError):
        protocol.receive(io.BytesIO(b'{"type": "audio", "bytes": 4}\n\x01\x02'))


def _handshake(satellite_password, brain_passwords, name="bedroom", **speaker):
    """Runs both sides over a socket pair, returning what each gave or raised, and what the brain sent."""
    near, far = socket.socketpair()
    near.settimeout(5)
    far.settimeout(5)
    satellite_stream = near.makefile("rwb")
    brain_stream = far.makefile("rwb")
    outcome = {}
    sent = []

    class Recording:
        """Notes each message the brain sends."""

        def write(self, data):
            sent.append(json.loads(data.split(b"\n")[0])["type"])
            return brain_stream.write(data)

        def __getattr__(self, attribute):
            return getattr(brain_stream, attribute)

    def run(side, function):
        try:
            outcome[side] = function()
        except Exception as e:
            outcome[side] = e
        finally:
            # Hangs up as a caller would, since the makefile keeps the socket open otherwise.
            (near if side == "satellite" else far).shutdown(socket.SHUT_RDWR)

    threads = [
        threading.Thread(target=run, args=("satellite", lambda: protocol.greet(
            satellite_stream, name, "home-assistant", satellite_password, 48000, False, **speaker))),
        threading.Thread(target=run, args=("brain", lambda: protocol.admit(Recording(), brain_passwords.get))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    return outcome, sent


PASSWORD = "correct-horse-battery-staple"


def test_the_handshake_admits_a_satellite_which_knows_its_password():
    outcome, sent = _handshake(PASSWORD, {"bedroom": PASSWORD})

    assert outcome["brain"] == {"name": "bedroom", "hostname": "home-assistant",
                                "sample_rate": 48000, "unclean": False, "speaker": None,
                                "push_to_talk": False, "machine": None, "saves_notes": False,
                                "media_controls": False, "opens": False, "system_volume": False,
                                "approves": False, "conversations": False,
                                "version": None, "updates": False, "update_format": None,
                                "nonces": outcome["brain"]["nonces"]}
    assert len(outcome["brain"]["nonces"]) == 2
    assert outcome["satellite"] == outcome["brain"]["nonces"]
    assert sent == ["challenge", "ready"]


def _admitted(**fields):
    """
    What admit gives back for a hello of the bedroom's with these fields as well, sent
    by hand - for what no satellite of today's would send. The password is right.
    """
    near, far = socket.socketpair()
    near.settimeout(5)
    far.settimeout(5)
    brain_stream, satellite_stream = far.makefile("rwb"), near.makefile("rwb")
    outcome = {}

    def brain():
        try:
            outcome["brain"] = protocol.admit(brain_stream, {"bedroom": PASSWORD}.get)
        except Exception as e:
            outcome["brain"] = e

    thread = threading.Thread(target=brain)
    thread.start()
    nonce = "0" * 32
    protocol.send(satellite_stream, "hello", name="bedroom", hostname="h", sample_rate=48000,
                  unclean=False, nonce=nonce, **fields)
    challenge, _ = protocol.receive(satellite_stream)
    protocol.send(satellite_stream, "proof",
                  proof=protocol.proof(PASSWORD, "satellite", challenge["nonce"], nonce))
    assert protocol.receive(satellite_stream)[0]["type"] == "ready"
    thread.join(5)
    near.close()
    far.close()
    return outcome["brain"]


def _hello_sent(**fields):
    """The hello greet sends, which nothing answers, so greet goes on to fail when its socket closes."""
    near, far = socket.socketpair()
    near.settimeout(5)
    far.settimeout(5)
    satellite_stream, brain_stream = near.makefile("rwb"), far.makefile("rwb")

    def satellite():
        try:
            protocol.greet(satellite_stream, "pc", "desktop", PASSWORD, 48000, False, **fields)
        except (OSError, protocol.ProtocolError):
            pass

    thread = threading.Thread(target=satellite)
    thread.start()
    try:
        return protocol.receive(brain_stream)[0]
    finally:
        near.shutdown(socket.SHUT_RDWR)
        thread.join(5)
        for closing in (brain_stream, satellite_stream, near, far):
            closing.close()


def test_hello_says_push_to_talk_only_when_it_is():
    assert _hello_sent(push_to_talk=True)["push_to_talk"] is True
    assert "push_to_talk" not in _hello_sent()
    assert "push_to_talk" not in _hello_sent(push_to_talk=False)


def test_admit_reads_push_to_talk():
    assert _admitted(push_to_talk=True)["push_to_talk"] is True
    assert _admitted()["push_to_talk"] is False
    assert _admitted(push_to_talk=False)["push_to_talk"] is False


def test_hello_says_the_machine_and_notes():
    hello = _hello_sent(machine="pc", saves_notes=True)
    assert hello["machine"] == "pc" and hello["saves_notes"] is True
    admitted = _admitted(machine="pc", saves_notes=True)
    assert admitted["machine"] == "pc" and admitted["saves_notes"] is True


def test_hello_leaves_them_out_when_unknown():
    # So an old brain, which has never heard of either, is sent just the hello it always was.
    for hello in (_hello_sent(), _hello_sent(machine=None, saves_notes=False)):
        assert "machine" not in hello and "saves_notes" not in hello
    # A machine which saves no notes still says what it is.
    hello = _hello_sent(machine="pi")
    assert hello["machine"] == "pi" and "saves_notes" not in hello


@pytest.mark.parametrize("machine", [5, "toaster", "", None, True, ["pc"], "PC"],
                         ids=["number", "unknown kind", "empty", "null", "true", "list", "capitals"])
def test_odd_machine_or_notes_values_are_unknown_not_refused(machine):
    # An extra, like the speaker: never the reason a satellite can't connect.
    admitted = _admitted(machine=machine, saves_notes="yes")
    assert admitted["machine"] is None and admitted["saves_notes"] is False
    for saves_notes in (1, 0, None, "true", ["x"]):
        assert _admitted(saves_notes=saves_notes)["saves_notes"] is False


def test_every_machine_the_brain_knows_is_admitted():
    for machine in MACHINES:
        assert _admitted(machine=machine)["machine"] == machine


def test_old_hello_is_still_admitted():
    admitted = _admitted()
    assert admitted["machine"] is None and admitted["saves_notes"] is False


@pytest.mark.parametrize("said", ["yes", 1, 0, None, ["x"]], ids=["text", "one", "zero", "null", "list"])
def test_a_hello_whose_push_to_talk_isnt_true_or_false_is_refused(said):
    near, far = socket.socketpair()
    far.settimeout(1)
    satellite_stream, brain_stream = near.makefile("rwb"), far.makefile("rwb")
    try:
        protocol.send(satellite_stream, "hello", name="bedroom", hostname="h", sample_rate=48000,
                      unclean=False, nonce="0" * 32, push_to_talk=said)
        with pytest.raises(protocol.ProtocolError):
            protocol.admit(brain_stream, {"bedroom": PASSWORD}.get)
    finally:
        for closing in (brain_stream, satellite_stream, near, far):
            closing.close()


def test_hello_says_media_controls_only_when_there_are_some():
    assert _hello_sent(media_controls=True)["media_controls"] is True
    assert "media_controls" not in _hello_sent()
    assert "media_controls" not in _hello_sent(media_controls=False)


def test_admit_reads_media_controls():
    assert _admitted(media_controls=True)["media_controls"] is True
    assert _admitted()["media_controls"] is False
    assert _admitted(media_controls=False)["media_controls"] is False


@pytest.mark.parametrize("said", ["yes", 1, 0, None, ["x"]], ids=["text", "one", "zero", "null", "list"])
def test_a_hello_whose_media_controls_isnt_true_or_false_is_refused(said):
    near, far = socket.socketpair()
    far.settimeout(1)
    satellite_stream, brain_stream = near.makefile("rwb"), far.makefile("rwb")
    try:
        protocol.send(satellite_stream, "hello", name="bedroom", hostname="h", sample_rate=48000,
                      unclean=False, nonce="0" * 32, media_controls=said)
        with pytest.raises(protocol.ProtocolError):
            protocol.admit(brain_stream, {"bedroom": PASSWORD}.get)
    finally:
        for closing in (brain_stream, satellite_stream, near, far):
            closing.close()


@pytest.mark.parametrize("nonce", ["z" * 32, "0" * 31 + "g", " " * 32, "0x" + "0" * 30, "\u0660" * 32, "0" * 31, "0" * 33],
                         ids=["letters", "one letter", "spaces", "0x", "arabic-indic digits", "short", "long"])
def test_a_hello_whose_nonce_isnt_32_hex_digits_is_refused(nonce):
    near, far = socket.socketpair()
    far.settimeout(1)
    satellite_stream, brain_stream = near.makefile("rwb"), far.makefile("rwb")
    try:
        protocol.send(satellite_stream, "hello", name="bedroom", hostname="h", sample_rate=48000,
                      unclean=False, nonce=nonce)
        with pytest.raises(protocol.ProtocolError):
            protocol.admit(brain_stream, {"bedroom": PASSWORD}.get)
    finally:
        for closing in (brain_stream, satellite_stream, near, far):
            closing.close()


def test_a_satellite_says_its_spotify_speaker():
    outcome, sent = _handshake(PASSWORD, {"bedroom": PASSWORD}, speaker="Bedroom Pi")

    assert outcome["brain"]["speaker"] == "Bedroom Pi"
    assert outcome["satellite"] == outcome["brain"]["nonces"]
    assert sent == ["challenge", "ready"]


def test_hello_opens_and_volume_sent_only_when_true():
    hello = _hello_sent(opens=True, system_volume=True)
    assert hello["opens"] is True and hello["system_volume"] is True
    for hello in (_hello_sent(), _hello_sent(opens=False, system_volume=False)):
        assert "opens" not in hello and "system_volume" not in hello


def test_admit_reads_opens_and_volume():
    admitted = _admitted(opens=True, system_volume=True)
    assert admitted["opens"] is True and admitted["system_volume"] is True
    admitted = _admitted()
    assert admitted["opens"] is False and admitted["system_volume"] is False


def test_approves_goes_in_the_hello_only_when_true():
    assert _hello_sent(approves=True)["approves"] is True
    for hello in (_hello_sent(), _hello_sent(approves=False)):
        assert "approves" not in hello


def test_admit_reads_approves_and_a_nonsense_one_as_false(caplog):
    assert _admitted(approves=True)["approves"] is True
    assert _admitted()["approves"] is False
    with caplog.at_level(logging.WARNING):
        admitted = _admitted(approves="yes")
    # The satellite is let in all the same.
    assert admitted["approves"] is False and admitted["name"] == "bedroom"
    assert len([r for r in caplog.records if "approves" in r.getMessage()]) == 1


def test_admit_bad_opens_is_false_not_refused():
    # An extra, like saves_notes: never the reason a satellite can't connect.
    for said in ("yes", 1, 0, None, ["x"]):
        admitted = _admitted(opens=said, system_volume=said)
        assert admitted["opens"] is False and admitted["system_volume"] is False


def test_hello_conversations_sent_only_when_true():
    assert _hello_sent(conversations=True)["conversations"] is True
    for hello in (_hello_sent(), _hello_sent(conversations=False)):
        assert "conversations" not in hello


def test_admit_reads_conversations_and_a_bad_one_is_false_not_refused():
    assert _admitted(conversations=True)["conversations"] is True
    assert _admitted()["conversations"] is False
    for said in ("yes", 1, 0, None, ["x"]):
        assert _admitted(conversations=said)["conversations"] is False


def test_a_hello_without_a_speaker_still_connects():
    # As an older satellite's has none - and one which doesn't make sense is only
    # left out, never the reason a satellite can't connect.
    assert _admitted()["speaker"] is None
    for speaker in ("x" * 200, 7, "", None, True, ["Bedroom Pi"], "Bedroom\nPi", "Bedroom\x00Pi"):
        admitted = _admitted(speaker=speaker)
        # The nonces are fresh each time, and have their own test.
        assert len(admitted.pop("nonces")) == 2
        assert admitted == {"name": "bedroom", "hostname": "h", "sample_rate": 48000,
                                              "unclean": False, "speaker": None, "push_to_talk": False,
                                              "machine": None, "saves_notes": False,
                                              "media_controls": False, "opens": False,
                                              "system_volume": False, "approves": False,
                                              "conversations": False, "version": None,
                                              "updates": False, "update_format": None}
    # Up to 64 characters, of any language.
    assert _admitted(speaker="x" * 64)["speaker"] == "x" * 64
    assert _admitted(speaker="Cuisine de Zoë")["speaker"] == "Cuisine de Zoë"


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"], ids=["line separator", "paragraph separator"])
def test_a_speaker_with_a_line_or_paragraph_separator_is_left_out(separator):
    # Neither is a control character, but each ends a line as a newline does.
    assert _admitted(speaker=f"Bedroom{separator}Pi")["speaker"] is None
    # While the spaces which don't end a line still do for a name: "Bedroom Pi", and a non-breaking one.
    assert _admitted(speaker="Bedroom\u00a0Pi")["speaker"] == "Bedroom\u00a0Pi"


def test_a_speaker_left_out_is_logged(caplog):
    # So falling back to the brain's own speaker name can be told from the journal,
    # not only heard as "I can't find the Voice-Assistant speaker" - shown as ascii()
    # and cut short, so a hello can't write lines of its own into the journal.
    with caplog.at_level("WARNING", logger="voice_assistant.protocol"):
        _admitted(speaker="Bedroom\nPi" + "x" * 200)
    (record,) = caplog.records
    assert "Bedroom\\nPi" in record.getMessage() and "\n" not in record.getMessage()
    assert "x" * 100 not in record.getMessage()

    caplog.clear()
    with caplog.at_level("WARNING", logger="voice_assistant.protocol"):
        _admitted()
        _admitted(speaker="Bedroom Pi")
    # Nothing for a hello without one, as an older satellite's, or a good one.
    assert caplog.records == []


def test_an_old_brain_ignores_the_speaker():
    # A brain from before speakers were said lets in a satellite which says one, as
    # admit has always let in a hello with fields it doesn't know - which this checks
    # still holds, so either end can be updated first.
    admitted = _admitted(speaker="Bedroom Pi", colour="blue")
    assert admitted["name"] == "bedroom" and "colour" not in admitted


def test_a_wrong_password_is_refused():
    outcome, sent = _handshake("some-other-password-entirely", {"bedroom": PASSWORD})

    # The satellite finds the brain's proof wrong, and hangs up before giving one of its own.
    assert isinstance(outcome["satellite"], protocol.Refused)
    assert not isinstance(outcome["brain"], dict)
    assert sent == ["challenge"]


def test_the_brain_refuses_a_proof_which_is_wrong():
    near, far = socket.socketpair()
    near.settimeout(5)
    far.settimeout(5)
    brain_stream, satellite_stream = far.makefile("rwb"), near.makefile("rwb")
    outcome = []

    def brain():
        # Kept, as nothing raised in a thread is seen by the test.
        try:
            outcome.append(protocol.admit(brain_stream, {"bedroom": PASSWORD}.get))
        except Exception as e:
            outcome.append(e)

    thread = threading.Thread(target=brain)
    try:
        protocol.send(satellite_stream, "hello", name="bedroom", hostname="h", sample_rate=48000,
                      unclean=True, nonce="0" * 32)
        thread.start()
        challenge, _ = protocol.receive(satellite_stream)
        # Replies with the proof the brain itself gave, which is for the wrong role.
        protocol.send(satellite_stream, "proof", proof=challenge["proof"])

        assert protocol.receive(satellite_stream)[0]["type"] == "refused"
        thread.join(5)
        assert not thread.is_alive()
        assert len(outcome) == 1 and isinstance(outcome[0], protocol.Refused)
    finally:
        for closing in (brain_stream, satellite_stream, near, far):
            closing.close()


def test_a_brain_which_cant_prove_the_password_is_refused():
    outcome, sent = _handshake(PASSWORD, {"bedroom": "some-other-password-entirely"})

    assert isinstance(outcome["satellite"], protocol.Refused)
    # The satellite hung up without proving anything, so the brain never got a proof.
    assert not isinstance(outcome["brain"], dict)
    assert sent == ["challenge"]


def test_an_unknown_satellite_is_refused():
    outcome, sent = _handshake(PASSWORD, {"bedroom": PASSWORD}, name="kitchen")

    assert isinstance(outcome["brain"], protocol.Refused)
    assert isinstance(outcome["satellite"], protocol.Refused)
    assert sent == ["refused"]


def test_a_proof_depends_on_who_gives_it():
    assert protocol.proof(PASSWORD, "brain", "a", "b") != protocol.proof(PASSWORD, "satellite", "a", "b")
    assert protocol.proof(PASSWORD, "brain", "a", "b") != protocol.proof(PASSWORD, "brain", "b", "a")


def test_hello_says_the_version_and_updates_only_with_both():
    hello = _hello_sent(version="a" * 40, update_format="bundle")
    assert hello["version"] == "a" * 40
    assert hello["updates"] is True
    assert hello["update_format"] == "bundle"
    # So a brain from before updates is sent the hello it always was.
    for hello in (_hello_sent(version="a" * 40), _hello_sent(update_format="zip"), _hello_sent()):
        assert "version" not in hello and "updates" not in hello and "update_format" not in hello


def test_admit_reads_the_version_and_format():
    admitted = _admitted(version="b" * 40, updates=True, update_format="zip")
    assert admitted["version"] == "b" * 40
    assert admitted["updates"] is True
    assert admitted["update_format"] == "zip"
    admitted = _admitted()
    assert admitted["version"] is None and admitted["updates"] is False
    assert admitted["update_format"] is None


@pytest.mark.parametrize("fields, version, updates", [
    ({"version": "A" * 40, "updates": True, "update_format": "zip"}, None, False),
    ({"version": "a" * 39, "updates": True, "update_format": "zip"}, None, False),
    ({"version": 123, "updates": True, "update_format": "zip"}, None, False),
    ({"version": "a" * 40, "updates": True, "update_format": "tar"}, "a" * 40, False),
], ids=["capitals", "too short", "number", "unknown format"])
def test_admit_takes_a_nonsense_version_or_format_as_none_not_refused(fields, version, updates):
    admitted = _admitted(**fields)
    assert admitted["version"] == version
    assert admitted["updates"] is updates
    assert admitted["update_format"] == (None if fields["update_format"] == "tar" else "zip")


def test_both_sides_get_the_same_nonces():
    outcome, _ = _handshake(PASSWORD, {"bedroom": PASSWORD})
    satellite_nonces = outcome["satellite"]
    assert satellite_nonces == outcome["brain"]["nonces"]
    first, second = satellite_nonces
    assert first != second
    assert protocol.NONCE.fullmatch(first) and protocol.NONCE.fullmatch(second)


def test_the_update_messages_are_frozen():
    # Changing any of this breaks every satellite in the house: an old satellite can only
    # be updated by a newer brain if both still mean the same by these messages.
    assert protocol.FROZEN_UPDATE_MESSAGES == {
        "hello": {"fields": (), "optional": ("version", "updates", "update_format"), "payload": False},
        "update_offer": {"fields": ("id", "version", "format", "size", "chunks", "proof", "retry"),
                         "optional": (), "payload": False},
        "update_chunk": {"fields": ("id", "index"), "optional": (), "payload": True},
        "update_end": {"fields": ("id",), "optional": (), "payload": False},
        "update_installing": {"fields": ("version",), "optional": (), "payload": False},
        "update_result": {"fields": ("version", "ok"), "optional": ("error", "needs_you"), "payload": False},
    }
    assert protocol.COMMIT.fullmatch("0123456789abcdef" * 2 + "01234567")
    assert protocol.UPDATE_FORMATS == ("zip", "bundle")
    assert protocol.UPDATE_CHUNK == 256 * 1024 <= protocol.MAX_AUDIO
    assert protocol.UPDATE_MOST == 64 * 1024 * 1024
    assert protocol.MOST_UPDATE_ERROR == 300


ARCHIVE = bytes(range(256)) * 8
COMMIT_ID = "0123456789abcdef" * 2 + "01234567"


def _update(archive=ARCHIVE, password="secret", first="a" * 32, second="b" * 32, version=COMMIT_ID, format="zip"):
    return protocol.update_proof(password, first, second, version, format, archive)


def test_an_update_proof_checks_with_the_same_everything():
    assert _update() == _update()
    assert len(_update()) == 64


def test_a_tampered_archive_doesnt_prove():
    middle = len(ARCHIVE) // 2
    changed = []
    for index in (0, middle, len(ARCHIVE) - 1):
        data = bytearray(ARCHIVE)
        data[index] ^= 1
        changed.append(bytes(data))
    changed += [ARCHIVE + b"x", ARCHIVE[:-1]]
    for archive in changed:
        assert _update(archive) != _update()


def test_another_commit_doesnt_prove():
    assert _update(version="f" * 40) != _update()


def test_another_format_doesnt_prove():
    assert _update(format="bundle") != _update()


def test_a_proof_from_another_connection_doesnt_prove():
    outcome, _ = _handshake("pw", {"bedroom": "pw"})
    first, second = outcome["brain"]["nonces"]
    other, _ = _handshake("pw", {"bedroom": "pw"})
    third, fourth = other["brain"]["nonces"]
    here = _update(password="pw", first=first, second=second)
    assert _update(password="pw", first=third, second=fourth) != here
    assert _update(password="pw", first=second, second=first) != here


def test_an_update_proof_is_never_a_handshake_proof():
    update = _update(archive=b"")
    assert update != protocol.proof("secret", "brain", "a" * 32, "b" * 32)
    assert update != protocol.proof("secret", "satellite", "a" * 32, "b" * 32)


def test_another_password_doesnt_prove():
    assert _update(password="other") != _update()
