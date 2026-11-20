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
        answer to a computer: a sentence to say, or one saying why not, or the volume now)

and what the brain sends a satellite:

    pong
    listen  {"recording": ...}  (what the brain calls the answer, e.g. "listen-3" - optional)
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
            and where on its screen to show Nova's orb, and how big)}
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
    computer  {"id": ..., "action": "open"|"open_url"|"close"|"close_front"|"volume",
        "item"?: ..., "url"?: ..., "name"?: ..., "direction"?: ..., "step"?: ..., "level"?: ...,
        "mute"?: ...}  (something to do on the computer itself, answered by computer_done)
    restart

The handshake comes first, and proves the satellite's password to the brain and the
brain's to the satellite, without the password itself ever being sent:

    hello  {name, hostname, sample_rate, unclean, nonce, speaker?, push_to_talk?, machine?,
            saves_notes?, media_controls?, opens?, system_volume?}    (satellite)
    challenge  {nonce, proof}    (brain; or refused, and the connection is closed)
    proof  {proof}    (satellite, once the brain's proof is right)
    ready, or refused    (brain)
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
COMPUTER_ACTIONS = ("open", "open_url", "close", "close_front", "volume")

# The kinds of thing a satellite can open, as the kind of each entry in its apps.
APP_KINDS = ("app", "epic", "steam")

# What a nonce is: 32 hex digits, as secrets.token_hex(16) makes. Written out rather than
# tried with int(nonce, 16), which lets through digits of other scripts, and spaces.
NONCE = re.compile(r"[0-9a-fA-F]{32}")


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
          system_volume: bool = False):
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
    # And these five, so a brain from before them is sent the hello it always was.
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
    send(stream, "hello", name=name, hostname=hostname, sample_rate=sample_rate,
         unclean=unclean, nonce=nonce, **extra)
    challenge = _expect(stream, "challenge", "nonce", "proof")
    expected = proof(password, "brain", nonce, challenge["nonce"])
    if not hmac.compare_digest(expected, challenge["proof"]):
        raise Refused("The brain couldn't prove it knows the password")
    send(stream, "proof", proof=proof(password, "satellite", challenge["nonce"], nonce))
    _expect(stream, "ready")


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
        it opens things and whether it has system volume, each False in the same cases.

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
    return {"name": name, "hostname": hostname, "sample_rate": sample_rate, "unclean": unclean,
            "speaker": speaker, "push_to_talk": push_to_talk, "machine": machine, "saves_notes": saves_notes,
            "media_controls": media_controls, "opens": opens, "system_volume": system_volume}


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
