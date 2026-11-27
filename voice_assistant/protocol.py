"""
Protocol Module.

How a satellite and Nova's brain talk - see docs/decisions/0006. One connection, kept
open, carrying messages which are each a line of JSON, followed by the audio it says
comes with it, if any: Wyoming's message shape, without needing its library.

After the handshake, what a satellite sends the brain:

    ping
    wake  {"talked_over": ..., "pre_roll": ..., "recording": ...}  (pre_roll: how many of
        the samples sent next are from before the wake word; recording: what the
        satellite calls the recording, e.g. "wake-3" - both optional; pressed: true
        when a push-to-talk satellite started it on a press, with no wake word and so
        no pre_roll to send - left out otherwise)
    audio  (payload: 16-bit samples at 16kHz)
    audio_end  {"reason": "limit"|"cancelled"|"pressed"}  (pressed: a push-to-talk
        satellite's recording ended on another press)
    played  {"id": ..., "stopped": ..., "position": ...}
    progress  {"id": ..., "position": ...}
    away  {"rang": [ids], "dismissed": [ids], "ringing": [ids]}
    note_saved  {"id": ..., "file": ...} or {"id": ..., "error": ...}  (the answer to a
        save_note: the name of the file made, or a sentence saying why not)
    media  {"action": "play"|"pause"|"next"|"previous"}  (a button of Windows' media
        controls, from a satellite which said it has them - see docs/decisions/0031)
    apps  {}  (payload: a JSON list of {"id": ..., "name": ..., "kind": ...}, what the
        satellite can open, kind one of APP_KINDS - from a satellite which said it opens things)
    windows  {}  (payload: a JSON list of the names of the windows open now)
    computer_done  {"id": ..., "said"?: ..., "error"?: ..., "level"?: ..., "muted"?: ...}  (the
        answer to a computer: a sentence to say, or one saying why not, or the volume now - or
        for "mic", whether the microphone is muted now)
    approval_answer  {"id": ..., "approved": true|false}  (the user's answer to an approval, once)
    shortcut_taken  {"taken": true|false}  (whether another program has the shortcut the satellite's
        app listens on, said each time that changes and as it connects)
    echo_ready  {"ready": true|false, "unheard"?: true}  (whether its echo canceller has lined its
        speaker up with its microphone and is taking Nova's own sound out - said as it connects, and
        each time that changes; false where it has none, or while a conversation isn't holding its
        devices open. unheard, only with ready, when there is nothing to take out: its microphone
        doesn't hear its speaker at all, as with headphones on)
    cut_in  {"recording": ...}  (a press over a reply while an open recording was being sent, which
        stopped the reply at once and left the recording going - see listen's open)

and what the brain sends a satellite:

    pong
    listen  {"recording": ..., "open"?: true}  (what the brain calls the answer, e.g. "listen-3" -
        optional; open: a conversation's recording, which has no limit on its length and ends only on
        stop_listening - sent only to a satellite which said conversations)
    conversation  {"on": true|false}  (a conversation has started or ended on this satellite: a
        push-to-talk one holds its microphone and speaker open and takes its own sound out of what it
        hears for as long as it lasts - sent only to a satellite which said conversations)
    stop_listening  {"recording": ...}  (which recording to stop - optional, for whichever
        is being sent)
    play_sound  {"id": ..., "name": ..., "volume": ..., "kind": ..., "start": ..., "length": ...}
    play_stream  {"id": ..., "rate": ..., "channels": ..., "volume": ..., "kind": ..., "paused": ...}
    stream_audio  {"id": ...}  (payload)
    stream_end  {"id": ...}
    set_playback  {"id": ..., "paused"?: ..., "volume"?: ...}
    stop  {"kinds": [...]|null} or {"id": ...}  (whole kinds of sound, or one playback)
    silence  {"kind": ...}
    allow  {"kind": ...}
    state  {"volumes": {"general": ..., "alarm": ..., "music": ...}, "muted": ..., "longest_request"?: seconds,
            "wake_confidence"?: 0-1, "voice_check"?: 0-0.99,
            "screen_orb"?: {"on": ..., "place": ..., "size": ...}  (a push-to-talk satellite only: whether
            and where on its screen to show Nova's orb, and how big),
            "app"?: {"shortcut": ..., "page": ..., "mic_mute_minutes": ...}  (a push-to-talk satellite
            only: the keys its app listens on as "ctrl+alt+n", "" for none; the settings page's address,
            "" for none; and how long its microphone stays muted)}
    alarms  {"entries": [{"id": ..., "due": ..., "kind": ..., "sound": ...}]}
    phrase  {"rate": ...}  (payload)
    away_settled  (once the brain has saved what away said)
    save_note  {"id": ..., "title": ...}  (payload: the note's text, as UTF-8 - a line of
        JSON is only MAX_LINE long, and a note may be far more)
    quiet  {"on": ...}  (Nova has started, or stopped, speaking on another satellite, so
        ignore the wake word meanwhile - see docs/decisions/0029)
    now_playing  {"state": "playing"|"paused"|"stopped", "track": ..., "artist": ...,
        "album": ..., "cover"?: ...}  (what the satellite's own music is doing, for a
        satellite which said it has Windows' media controls, sent when it changes;
        cover: true with the new cover as the payload, false for none now, left out
        when the cover is as it was - see docs/decisions/0031)
    computer  {"id": ..., "action": "open"|"open_url"|"close"|"close_front"|"hide"|"hide_front"|"volume"|
        "mic",
        "item"?: ..., "url"?: ..., "name"?: ..., "direction"?: ..., "step"?: ..., "level"?: ...,
        "mute"?: ...}  (something to do on the computer itself, answered by computer_done; "mic"
        mutes or unmutes Windows' own microphone, sent only to a satellite which said system_volume)
    approval  {"id": ..., "description": ...}  (ask for the user's OK to a big action, from
        a satellite which said it approves)
    approval_withdraw  {"id": ...}  (the request is settled or given up, so stop asking)
    restart

The handshake comes first, and proves the satellite's password to the brain and the
brain's to the satellite, without the password itself ever being sent:

    hello  {name, hostname, sample_rate, unclean, nonce, speaker?, push_to_talk?, machine?,
            saves_notes?, media_controls?, opens?, system_volume?, approves?,
            conversations?, version?, updates?, update_format?}    (satellite; version: the
            40-digit commit its code is, updates: true and update_format: "zip" or "bundle", said
            only together with a version)
    challenge  {nonce, proof}    (brain; or refused, and the connection is closed)
    proof  {proof}    (satellite, once the brain's proof is right)
    ready, or refused    (brain)

The update messages are FROZEN, for ever: the hello's version, updates and update_format, and

    update_offer  {"id": ..., "version": ..., "format": "zip"|"bundle", "size": ..., "chunks": ...,
        "proof": ..., "retry": ...}  (brain: an update to install; proof is an HMAC-SHA256 keyed with
        the satellite's password, over the archive and the commit id and bound to this connection's
        two handshake nonces)
    update_chunk  {"id": ..., "index": ...}  (brain; payload: the next UPDATE_CHUNK bytes of the archive)
    update_end  {"id": ...}  (brain: the last chunk has been sent)
    update_installing  {"version": ...}  (satellite: the proof is right and it is installing)
    update_result  {"version": ..., "ok": true|false, "error"?: ..., "needs_you"?: ...}  (satellite:
        how it went, once it is back on the brain)

They are frozen because the satellite which most needs updating is an old one, running code from
before whatever the brain has learnt since: any newer brain must always be able to update any older
satellite which can update at all, and so neither side may ever change what these mean. A test
(FROZEN_UPDATE_MESSAGES) pins them.
"""

import hmac
import json
import logging
import re
import secrets
import unicodedata
from typing import BinaryIO, Callable, Dict, Optional, Tuple

from voice_assistant.machine import MACHINES

logger = logging.getLogger(__name__)

# The longest a line of JSON may be, so neither side can be kept reading for ever.
MAX_LINE = 4096

# The most audio one message may carry - 10 seconds of it.
MAX_AUDIO = 10 * 16000 * 2

# The longest Spotify speaker name a hello may carry, e.g. "Bedroom Pi".
MAX_SPEAKER = 64

# The buttons of Windows' media controls a satellite passes on, as media's action.
MEDIA_ACTIONS = ("play", "pause", "next", "previous")

# The things a satellite can be told to do on its computer, as computer's action.
COMPUTER_ACTIONS = ("open", "open_url", "close", "close_front", "hide", "hide_front", "volume", "mic")

# The kinds of thing a satellite can open, as the kind of each entry in its apps.
APP_KINDS = ("app", "epic", "steam")

# What a nonce is: 32 hex digits, as secrets.token_hex(16) makes. Written out rather than
# tried with int(nonce, 16), which lets through digits of other scripts, and spaces.
NONCE = re.compile(r"[0-9a-fA-F]{32}")


# What a commit id is: git's 40 lowercase hex digits.
COMMIT = re.compile(r"[0-9a-f]{40}")

# The ways a satellite can be updated, as its hello's update_format.
UPDATE_FORMATS = ("zip", "bundle")

# How much of an update one update_chunk carries - well inside MAX_AUDIO, which caps every payload.
UPDATE_CHUNK = 256 * 1024

# The largest update archive either side will take, so neither can be made to store for ever.
UPDATE_MOST = 64 * 1024 * 1024

# The longest an update_result's error may be, in characters, so it fits a line.
MOST_UPDATE_ERROR = 300

# The update messages and their fields: "fields" always there, "optional" there sometimes,
# "payload" whether audio-style bytes follow. FROZEN: changing any of it breaks every satellite
# in the house, as an old one can only be updated by a brain which still means the same.
FROZEN_UPDATE_MESSAGES = {
    "hello": {"fields": (), "optional": ("version", "updates", "update_format"), "payload": False},
    "update_offer": {"fields": ("id", "version", "format", "size", "chunks", "proof", "retry"),
                     "optional": (), "payload": False},
    "update_chunk": {"fields": ("id", "index"), "optional": (), "payload": True},
    "update_end": {"fields": ("id",), "optional": (), "payload": False},
    "update_installing": {"fields": ("version",), "optional": (), "payload": False},
    "update_result": {"fields": ("version", "ok"), "optional": ("error", "needs_you"), "payload": False},
}


class ProtocolError(Exception):
    """Something arrived which isn't a message."""


def send(stream: BinaryIO, kind: str, payload: bytes = b"", /, **fields):
    """
    Sends one message.

    Args:
        stream: The connection, as a file - socket.makefile("rwb").
        kind: What it is, e.g. "audio".
        payload: Audio to go with it.
        fields: Anything else it carries, e.g. text="Pause". The arguments above are
            positional only, so a field may share a name with one - as play_stream's
            "kind" does.
    """
    message = {"type": kind, **fields}
    if payload:
        message["bytes"] = len(payload)
    stream.write(json.dumps(message).encode() + b"\n" + payload)
    stream.flush()


def receive(stream: BinaryIO) -> Tuple[Dict, bytes]:
    """
    Waits for the next message.

    Args:
        stream: The connection, as a file.

    Returns:
        The message, and the audio which came with it - empty if none did.

    Raises:
        ConnectionError: The other side has gone.
        ProtocolError: What arrived wasn't a message.
    """
    line = stream.readline(MAX_LINE + 1)
    if not line:
        raise ConnectionError("The connection was closed")
    if not line.endswith(b"\n"):
        raise ProtocolError("A message was too long, or cut short")
    try:
        message = json.loads(line)
    except ValueError as e:
        raise ProtocolError(f"Not a message: {line[:80]!r}") from e
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise ProtocolError(f"Not a message: {line[:80]!r}")
    size = message.get("bytes", 0)
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_AUDIO:
        raise ProtocolError(f"Can't take {size!r} bytes of audio in one message")
    payload = stream.read(size) if size else b""
    if len(payload) != size:
        raise ConnectionError("The connection was closed part way through a message")
    return message, payload


class Refused(Exception):
    """The other side couldn't prove the password, or the brain doesn't know the satellite."""


def proof(password: str, role: str, first: str, second: str) -> str:
    """
    Proves the password without sending it.

    Args:
        password: The satellite's password.
        role: Who is proving it, "brain" or "satellite". Part of what is signed, so
            one side's proof can't be replayed back to it as the other's.
        first: The nonce of whoever is being proved to.
        second: The nonce of whoever is proving.

    Returns:
        The HMAC-SHA256 of the three, in hex.
    """
    message = f"{role}|{first}|{second}".encode()
    return hmac.new(password.encode(), message, "sha256").hexdigest()


def update_proof(password: str, satellite_nonce: str, brain_nonce: str, version: str, format: str,
                 archive: bytes) -> str:
    """
    Proves an update archive came from the brain this satellite is connected to.

    Args:
        password: The satellite's password.
        satellite_nonce: The satellite's nonce from this connection's handshake.
        brain_nonce: The brain's nonce from the same handshake. Binding both means a proof
            recorded on one connection is no good on another.
        version: The commit id the archive brings the satellite to.
        format: The archive's format, one of UPDATE_FORMATS.
        archive: The archive's bytes.

    Returns:
        The HMAC-SHA256 in hex, keyed with the password, over the nonces, version, format
        and the archive's length, then the archive itself. It starts "update|", which a
        handshake proof never does, so neither can stand in for the other.
    """
    message = hmac.new(password.encode(), digestmod="sha256")
    message.update(f"update|{satellite_nonce}|{brain_nonce}|{version}|{format}|{len(archive)}|".encode())
    # Fed in pieces so a large archive isn't copied to make one message.
    view = memoryview(archive)
    for start in range(0, len(view), UPDATE_CHUNK):
        message.update(view[start:start + UPDATE_CHUNK])
    return message.hexdigest()


def _expect(stream: BinaryIO, kind: str, *fields: str) -> Dict:
    """Receives the next message, which has to be this kind with these fields as strings."""
    message, _ = receive(stream)
    if message["type"] == "refused":
        raise Refused("The brain refused the satellite")
    if message["type"] != kind or not all(isinstance(message.get(f), str) for f in fields):
        raise ProtocolError(f"Expected {kind}, not {message['type']}")
    return message


def greet(stream: BinaryIO, name: str, hostname: str, password: str, sample_rate: int, unclean: bool,
          speaker: Optional[str] = None, push_to_talk: bool = False, machine: Optional[str] = None,
          saves_notes: bool = False, media_controls: bool = False, opens: bool = False,
          system_volume: bool = False, approves: bool = False, conversations: bool = False,
          version: Optional[str] = None, update_format: Optional[str] = None) -> Tuple[str, str]:
    """
    The satellite's side of the handshake.

    Args:
        stream: The connection, as a file.
        name: This satellite's name, e.g. "bedroom".
        hostname: The machine it runs on.
        password: Its password.
        sample_rate: What its speaker plays at.
        unclean: Whether it restarted without being asked to.
        speaker: The Spotify Connect speaker it plays Spotify on, e.g. "Bedroom Pi" -
            left out of the hello if None.
        push_to_talk: Whether this satellite records on a press of a key, not on a wake
            word - left out of the hello if False.
        machine: What kind of computer it runs on, one of machine.MACHINES - left out
            of the hello if None.
        saves_notes: Whether it can save a note in a Downloads folder - left out of the
            hello if False.
        media_controls: Whether this satellite shows its music in Windows' media
            controls, and passes on their buttons - left out of the hello if False.
        opens: Whether it can open and close things on its computer - left out of the
            hello if False.
        system_volume: Whether it can set its computer's own volume - left out of the
            hello if False.
        approves: Whether it can ask the user's OK to a big action and send back the
            answer - left out of the hello if False.
        conversations: Whether it knows a conversation's messages - open recordings, the
            conversation on and off, and cut_in and echo_ready - left out of the hello if False.
        version: The commit its code is - left out of the hello, with updates and
            update_format, unless update_format is given too.
        update_format: How it can be updated, one of UPDATE_FORMATS - see version.

    Returns:
        The satellite's nonce and the brain's, in that order: this connection's, which an
        update's proof is bound to.

    Raises:
        Refused: The brain refused it, or couldn't prove it knows the password - in
            which case nothing of the satellite's has been given away.
        ProtocolError, ConnectionError: As receive does.
    """
    nonce = secrets.token_hex(16)
    # Only when there is one, so the hello is just what it was before speakers were said.
    extra = {"speaker": speaker} if speaker is not None else {}
    # Likewise, so the hello of a satellite which listens for a wake word is unchanged.
    if push_to_talk:
        extra["push_to_talk"] = True
    # And these six, so a brain from before them is sent the hello it always was.
    if machine is not None:
        extra["machine"] = machine
    if saves_notes:
        extra["saves_notes"] = True
    if media_controls:
        extra["media_controls"] = True
    if opens:
        extra["opens"] = True
    if system_volume:
        extra["system_volume"] = True
    if approves:
        extra["approves"] = True
    if conversations:
        extra["conversations"] = True
    # Only both together, as a version with no way to update is no use to the brain.
    if version is not None and update_format is not None:
        extra["version"] = version
        extra["updates"] = True
        extra["update_format"] = update_format
    send(stream, "hello", name=name, hostname=hostname, sample_rate=sample_rate,
         unclean=unclean, nonce=nonce, **extra)
    challenge = _expect(stream, "challenge", "nonce", "proof")
    expected = proof(password, "brain", nonce, challenge["nonce"])
    if not hmac.compare_digest(expected, challenge["proof"]):
        raise Refused("The brain couldn't prove it knows the password")
    send(stream, "proof", proof=proof(password, "satellite", challenge["nonce"], nonce))
    _expect(stream, "ready")
    return nonce, challenge["nonce"]


def admit(stream: BinaryIO, password_for: Callable[[str], Optional[str]]) -> Dict:
    """
    The brain's side of the handshake.

    Args:
        stream: The connection, as a file.
        password_for: Looks a satellite's password up by name - None if unknown.

    Returns:
        The hello's name, hostname, sample_rate and unclean, its speaker - None if
        it doesn't say one, as a satellite from before speakers were said doesn't -
        whether it is push-to-talk and whether it has Windows' media controls, each False
        if it doesn't say, its machine, one of machine.MACHINES, and whether it saves
        notes. The last two are None and False if the hello leaves them out or says what
        makes no sense, which never keeps a satellite out: they are extras. So are whether
        it opens things, whether it has system volume, whether it approves and whether it
        knows conversations, each False in the same cases. Also its version (None unless it
        is a commit id), whether it updates (False unless it has a version and an
        update_format), its update_format (one of UPDATE_FORMATS, else None) and the
        handshake's nonces, (satellite's, brain's).

    Raises:
        Refused: The satellite is unknown or got the password wrong. It has been told so.
        ProtocolError, ConnectionError: As receive does.
    """
    hello, _ = receive(stream)
    nonce = hello.get("nonce")
    name = hello.get("name")
    hostname = hello.get("hostname")
    sample_rate = hello.get("sample_rate")
    unclean = hello.get("unclean")
    push_to_talk = hello.get("push_to_talk", False)
    media_controls = hello.get("media_controls", False)
    if (hello["type"] != "hello" or not isinstance(name, str) or not isinstance(hostname, str)
            or isinstance(sample_rate, bool) or not isinstance(sample_rate, int)
            or not isinstance(unclean, bool) or not isinstance(push_to_talk, bool)
            or not isinstance(media_controls, bool)
            or not isinstance(nonce, str) or not NONCE.fullmatch(nonce)):
        raise ProtocolError("Expected a hello")
    password = password_for(name)
    if password is None:
        send(stream, "refused")
        raise Refused(f"No satellite called {name}")
    brain_nonce = secrets.token_hex(16)
    send(stream, "challenge", nonce=brain_nonce, proof=proof(password, "brain", nonce, brain_nonce))
    reply, _ = receive(stream)
    if (reply["type"] != "proof" or not isinstance(reply.get("proof"), str)
            or not hmac.compare_digest(proof(password, "satellite", brain_nonce, nonce), reply["proof"])):
        send(stream, "refused")
        raise Refused(f"{name} got the password wrong")
    send(stream, "ready")
    speaker = _speaker(hello.get("speaker"))
    if speaker is None and "speaker" in hello:
        logger.warning(f"{name} said a Spotify speaker which can't be one, so plays on the brain's "
                       f"own speaker name: {ascii(hello['speaker'])[:80]}")
    machine = hello.get("machine")
    if machine is not None and machine not in MACHINES:
        logger.warning(f"{name} said a kind of machine the brain doesn't know, so it's taken as unknown: "
                       f"{ascii(machine)[:80]}")
        machine = None
    saves_notes = hello.get("saves_notes", False)
    if not isinstance(saves_notes, bool):
        logger.warning(f"{name} said whether it saves notes in a way which makes no sense, so it's taken "
                       f"as not: {ascii(saves_notes)[:80]}")
        saves_notes = False
    opens = _flag(name, hello, "opens", "opens things")
    system_volume = _flag(name, hello, "system_volume", "has system volume")
    approves = _flag(name, hello, "approves", "approves")
    conversations = _flag(name, hello, "conversations", "knows conversations")
    version = hello.get("version")
    if version is not None and not (isinstance(version, str) and COMMIT.fullmatch(version)):
        logger.warning(f"{name} said a version which isn't a commit, so it's taken as unknown: "
                       f"{ascii(version)[:80]}")
        version = None
    update_format = hello.get("update_format")
    if update_format is not None and update_format not in UPDATE_FORMATS:
        logger.warning(f"{name} said a way of updating the brain doesn't know, so it's taken as none: "
                       f"{ascii(update_format)[:80]}")
        update_format = None
    updates = _flag(name, hello, "updates", "updates")
    if version is None or update_format is None:
        updates = False
    return {"name": name, "hostname": hostname, "sample_rate": sample_rate, "unclean": unclean,
            "speaker": speaker, "push_to_talk": push_to_talk, "machine": machine, "saves_notes": saves_notes,
            "media_controls": media_controls, "opens": opens, "system_volume": system_volume,
            "approves": approves, "conversations": conversations, "version": version,
            "updates": updates, "update_format": update_format, "nonces": (nonce, brain_nonce)}


def _flag(name: str, hello: Dict, key: str, meaning: str) -> bool:
    """
    A true-or-false extra of a hello, False if it's left out or isn't one, so a
    satellite is never kept out by it.

    Args:
        name: The satellite's name, for the warning.
        hello: The hello.
        key: The field's name.
        meaning: What it says, for the warning, e.g. "opens things".

    Returns:
        What the hello says, or False.
    """
    said = hello.get(key, False)
    if not isinstance(said, bool):
        logger.warning(f"{name} said whether it {meaning} in a way which makes no sense, so it's taken "
                       f"as not: {ascii(said)[:80]}")
        return False
    return said


def _speaker(said) -> Optional[str]:
    """
    The Spotify speaker a hello says, if it makes sense as one - else None, so a
    satellite is never kept out by what's only an extra, which is shown on the page.

    Args:
        said: The hello's speaker, which may be anything at all.

    Returns:
        It, if it's text of 1 to MAX_SPEAKER characters with no control characters or
        line breaks in it - a newline, say - else None.
    """
    if not isinstance(said, str) or not 1 <= len(said) <= MAX_SPEAKER:
        return None
    for character in said:
        category = unicodedata.category(character)
        # The line and paragraph separators (U+2028 and U+2029) are in Z, not C, but end
        # a line just as a newline does.
        if category.startswith("C") or category in ("Zl", "Zp"):
            return None
    return said
