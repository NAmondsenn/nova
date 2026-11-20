"""
Satellite.

What runs on each Pi now that Nova's brain is on PVE2 - see docs/decisions/0006. The
satellite hears the wake word, sends the brain what the user says, and plays, pauses
and stops whatever the brain sends - speech, chimes and music - as the one owner of
its speaker. Everything from the words to the answer is worked out on the brain.

Stopping stays here, so it stays instant: the moment the wake word is heard, a reply
stops, what rings goes quiet and the music pauses, before the brain has even been
told. While the brain can't be reached, the satellite rings the alarms from its copy,
and answers the wake word with the recorded "I can't reach the server".
"""

import itertools
import json
import logging
import math
import os
import queue
import signal
import socket
import sys
import threading
import time
from typing import Callable, Dict, Iterator, List, Optional, Tuple

import numpy as np

from voice_assistant import paths, pc_apps, pc_windows, protocol, storage, watchdog
from voice_assistant.computers import MIC_MUTE_MINUTES, NOT_A_WEB_ADDRESS, safe_url, url_host
from voice_assistant.config import Config, ConfigError
from voice_assistant.echo import make_echo_canceller
from voice_assistant.fallback import AlarmCopy, FallbackRinger, Phrase, StateCopy
from voice_assistant.logging_setup import setup_logging
from voice_assistant.machine import downloads_folder, machine_kind
from voice_assistant.microphone import RATE, Microphone
from voice_assistant.notes import NoteRefused, save_note
from voice_assistant.orb_drawing import SIZES
from voice_assistant.pc_volume import SystemVolume
from voice_assistant.press import PressListener
from voice_assistant.satellite_link import SILENT_FOR, SatelliteLink
from voice_assistant.screen_orb import PLACES, OrbStates, ScreenOrb
from voice_assistant.speaker import BLOCK_SECONDS, Playback, Speaker
from voice_assistant.timeline import Timeline
from voice_assistant.wake_clips import WakeClips, clip_start
from voice_assistant.wake_word import WakeWordDetector
from voice_assistant.windows_media import UNCHANGED, WindowsMedia

logger = logging.getLogger(__name__)

# Left while the satellite runs, and taken away when it stops properly - so finding it
# when starting means the last run didn't, which the brain is told as it connects. The
# same mark the Pi left before it was a satellite.
RUNNING_MARK = os.path.join(paths.DATA_DIR, "running")

# When the PC's own microphone, muted by Nova, comes back on by itself - {"until": epoch
# seconds} - kept so a restart of the satellite keeps the hour. See
# docs/superpowers/specs/2026-11-20-mute-means-the-microphone-design.md.
MIC_MUTE_FILE = os.path.join(paths.DATA_DIR, "mic_muted_until.json")

# The seconds in each of the minutes a mute of the PC's microphone lasts - computer.mic_mute_minutes,
# or computers.MIC_MUTE_MINUTES when the brain doesn't say - a constant so the tests can shorten it.
MINUTE = 60

# The longest the wait for a mute to run out goes without looking at the clock again: Windows
# counts a wait's time only while the PC is awake, so one long wait would end late by however
# long the PC slept.
MIC_LOOK_EVERY = 30

# Played when the PC's microphone comes back on by itself.
MIC_ON_SOUND = "mic_on.wav"

# The pieces a recording is sent in, in seconds of it - small, so the brain hears the
# user as they speak, and can tell the moment they've stopped.
PIECE_SECONDS = 0.1

# The most of one recording which is sent, in seconds, until the brain says how long
# one may be - so a brain which never says the user has stopped can't keep the
# microphone open.
LIMIT_SECONDS = 10

# The most of one recording which is sent, in seconds, whatever the brain says - so a
# setting of 90 on the brain can't keep the microphone open for a minute and a half.
MOST_SECONDS = 30

# How often the alarm copy is checked for what's due while the brain is away, and how
# often the brain is told how far the music has got, in seconds.
FALLBACK_CHECK = 1.0
PROGRESS_EVERY = 1.0

# The longest a push-to-talk satellite waits for a press, a follow-up the brain asks for
# or a stop before looking again, in seconds. A press, a follow-up and a stop each wake it
# at once, so this is only how often it tells the watchdog it's still working while
# nothing happens - the microphone is closed then, and is no other sign of it.
PRESS_WAIT = 1.0

# While Nova speaks on another satellite, this one ignores the wake word - see
# docs/decisions/0029. Never for longer than QUIET_MOST seconds without being told again,
# so a brain which goes before saying it has stopped can't leave it deaf, and for
# QUIET_AFTER seconds after it's told the other has stopped, for the last words still
# leaving that satellite's speakers.
QUIET_MOST = 60
QUIET_AFTER = 0.5

# The sound a press plays, from the satellite itself and straight away, whatever the
# brain's setting for its own listening sound.
PRESS_SOUND = "listening.wav"

# How far past an unmute a clip has to start, in seconds. Sound heard while muted can
# still be on its way to the microphone's buffer when the unmute is noted: up to about
# 0.25 s between ALSA, the stream's blocks, the resampler and the echo canceller, since
# any more than that overflows and is lost.
IN_FLIGHT_SECONDS = 0.3

# How long the brain has to say it has what the satellite told it on connecting
# (away_settled), in seconds, before the satellite gives up on the connection as it
# would on a silent brain. Until then the satellite rings on from its copy, though
# connected, so the wake word only dismisses what rings - and the brain only hears of
# that once the satellite next connects.
#
# A healthy brain says so within milliseconds of starting the satellite's session.
# Even with every wait in its catch-up run to its limit, it's 12 seconds: brain.AWAY_WAIT,
# 2 seconds, for the report, and just under satellite_link.SOCKET_TIMEOUT, 2 seconds,
# for each message around it - the state, the copy and the phrase, the copy again
# once apply_away has changed it, and away_settled itself. Starting the session comes
# first, and isn't in that, so the brain logs how long it all took from the
# satellite's hello. A brain which hasn't said so by then is stuck part way through -
# while still answering pings, so SILENT_FOR never notices. (One whose AWAY_WAIT ran
# out hangs up by itself.) So it's SILENT_FOR: clear of the 12, and no longer than the
# satellite already waits on a brain which says nothing at all.
SETTLE_WAIT = SILENT_FOR

# How long the satellite's own sound counts as still being heard once it has stopped,
# in seconds. The voice check judges about the last 2 seconds - 16 of openWakeWord's
# 80ms frames, each made from 775ms of audio - so "Hey Nova" said in the 3-second gap
# between a timer's rings still has the ring in it. The rest is the microphone's own
# delay, and the echo canceller's.
OWN_SOUND_LINGERS = 2.5

# How often a satellite which opens things builds its list of them again, and how often
# it looks at which programs have a window open, in seconds - each sent to the brain
# only when it has changed, or the brain is new.
APPS_EVERY = 600
WINDOWS_EVERY = 3

# The most apps and window names sent, as many as the brain takes (remote.MOST_APPS and
# MOST_WINDOWS): more than a person has, so only a guard.
MOST_APPS = 3000
MOST_WINDOWS = 200

# Said back when something asked of the computer can't be done.
NOT_HERE = "That's not on this PC any more."
COULDNT_OPEN = "That couldn't be opened."
NOTHING_TO_CLOSE = "There's nothing to close."
NO_VOLUME = "This computer's volume can't be changed."
NO_MICROPHONE = "This computer's microphone can't be changed."
CANT_DO = "I can't do that on this computer."


class _FromBrain:
    """One of the brain's playbacks, which it knows by its id."""

    def __init__(self, playback: Playback, kind: str, channels: int = 1,
                 pieces: Optional[queue.Queue] = None):
        """
        Args:
            playback: Where it's playing.
            kind: What sort of sound it is, e.g. "reply" or "music".
            channels: How many channels a stream's audio has.
            pieces: Where a stream's audio waits for the speaker, or None for a sound.
        """
        self.playback = playback
        self.kind = kind
        self.channels = channels
        self.pieces = pieces
        # Whether the orb was told Nova started speaking with it, so its end is told only
        # then - see Satellite._follow.
        self.counted = False

    @property
    def spoken(self) -> bool:
        """Whether it is Nova's own voice - a streamed reply or announcement, not a chime or music."""
        return self.kind in ("reply", "announcement") and self.pieces is not None

    def let_go(self):
        """Ends a stream's pieces, which would otherwise wait for more for good."""
        if self.pieces is not None:
            self.pieces.put(None)


class _Recording(threading.Event):
    """One recording being sent to the brain - set once the brain says the user has stopped."""

    def __init__(self, recording_id: Optional[str]):
        """
        Args:
            recording_id: What the brain knows it by: "wake-3", say, for one the wake word
                started, or the number the brain gave an answer it asked for, e.g.
                "listen-3" - None for an answer to a brain which doesn't number them.
        """
        super().__init__()
        # Every number the brain may stop it by. A listen which comes while it's being
        # sent adds its own, as this recording is that answer - see Satellite._begin.
        self.ids = set() if recording_id is None else {recording_id}
        # Whether it was a press, rather than the brain, which said the user has stopped.
        self.pressed = False
        # Whether a press started it, rather than the wake word or the brain's listen.
        self.from_press = False


def _pieces(waiting: queue.Queue) -> Iterator[np.ndarray]:
    """A stream's audio as it arrives from the brain, until None says that's all."""
    while True:
        piece = waiting.get()
        if piece is None:
            return
        yield piece


def _speaker(device_name) -> Optional[str]:
    """
    The Spotify speaker to say in the hello, if the brain can take it.

    Args:
        device_name: spotify.device_name from the settings, which a hand edit can make
            anything - an unquoted 2026-10-26 is a date to YAML.

    Returns:
        It, if it's text of 1 to protocol.MAX_SPEAKER characters, else None - so the
        brain falls back to its own speaker name. A date can't be sent at all, and
        would stop the satellite ever connecting.
    """
    if not device_name:
        return None
    if isinstance(device_name, str) and len(device_name) <= protocol.MAX_SPEAKER:
        return device_name
    logger.warning(f"spotify.device_name isn't text of up to {protocol.MAX_SPEAKER} characters, "
                   f"so the brain plays Spotify on its own speaker name: {ascii(device_name)[:80]}")
    return None


def _on_windows() -> bool:
    """Whether the satellite runs on Windows, the one system it opens things and sets the volume on."""
    return sys.platform == "win32"


def _whole(value) -> bool:
    """Whether a value from the brain is a whole number - JSON's true and false aren't."""
    return isinstance(value, int) and not isinstance(value, bool)


def _machine_and_notes() -> Tuple[Optional[str], bool]:
    """
    What to say in the hello of this machine, and whether it saves notes.

    Neither lookup may stop the satellite. The Windows one in particular has only ever
    been written, never run on real Windows, and a satellite which can't start over it
    would be one with no voice at all - so what it raises is logged, and this machine
    is taken as unknown and as saving no notes.

    Returns:
        Its kind, one of machine.MACHINES - or None if that couldn't be told - and
        whether it has a Downloads folder to save notes in.
    """
    try:
        machine = machine_kind()
    except Exception as e:
        logger.warning(f"Couldn't tell what kind of machine this is, so the brain isn't told "
                       f"({type(e).__name__}: {e})")
        machine = None
    try:
        saves_notes = downloads_folder() is not None
    except Exception as e:
        logger.warning(f"Couldn't look for a Downloads folder, so this satellite saves no notes "
                       f"({type(e).__name__}: {e})")
        saves_notes = False
    return machine, saves_notes


class Satellite:
    """One Pi's microphone and speaker, for a brain elsewhere."""

    def __init__(self, config: Config):
        """
        Args:
            config: The settings.

        Raises:
            ConfigError: There's no brain to connect to, as backend.address or the
                password, NOVA_BACKEND_TOKEN, isn't set.
        """
        audio_config = config.section("audio")
        backend_config = config.section("backend")
        self.address = backend_config.get("address", "")
        self.password = os.getenv("NOVA_BACKEND_TOKEN", "")
        if not self.address or not self.password:
            raise ConfigError("A satellite needs its brain's address, as backend.address in config.yaml, "
                              "and its password, as NOVA_BACKEND_TOKEN in ~/.config/nova/.env")
        self.hostname = socket.gethostname()
        self.name = backend_config.get("satellite") or self.hostname
        self.sample_rate = audio_config.get("sample_rate", 48000)
        # The name its setup gave raspotify, said to the brain as it connects so the
        # brain plays this satellite's Spotify here - changed with the setup, on the Pi.
        self.spotify_speaker = _speaker(config.section("spotify").get("device_name"))
        # Seconds of audio from just before the wake word was detected which are sent
        # first, so "Hey Nova what time is it", said in one breath, isn't cut short. The
        # brain is told how much with each wake, so this is the only setting for it.
        self.pre_roll = audio_config.get("pre_roll", 0.3)
        self.sounds_folder = paths.SOUNDS_DIR
        # Where a push-to-talk satellite is pressed from, on this machine - see press.py.
        self.press_port = config.section("satellite").get("press_port", 10401)
        self.latency_file = os.path.join(paths.LOGS_DIR, "latency.jsonl")

        # A push-to-talk satellite, such as the PC, starts a recording from a key press
        # and so has no wake word to load - nor any clips of it. Its mute is then kept in
        # the state copy alone.
        self.push_to_talk = config.section("satellite").get("trigger", "wake_word") == "push_to_talk"
        # Said in every hello, so the brain can name this satellite by its machine and
        # offer a notes tool only where there's a Downloads folder to save one in.
        self.machine, self.saves_notes = _machine_and_notes()

        # The one speaker and the one microphone, with what's played taken back out of
        # what's heard - see echo.py. Push-to-talk, each is open only when needed, so
        # the PC can go to sleep and its microphone light is off between presses.
        self.echo = make_echo_canceller(config, self.sample_rate)
        self.speaker = Speaker(sample_rate=self.sample_rate, echo_reference=self.echo.played if self.echo else None,
                               always_open=not self.push_to_talk)
        self.microphone = Microphone(sample_rate=self.sample_rate, echo=self.echo,
                                     always_open=not self.push_to_talk)
        self.wake: Optional[WakeWordDetector] = None
        self.clips: Optional[WakeClips] = None
        if not self.push_to_talk:
            self.wake = WakeWordDetector(config=config, microphone=self.microphone, playing=self._making_sound)
            # Only made when switched on, as it keeps the user's voice - see wake_clips.py.
            clips_config = config.section("wake_clips")
            self.clips = (WakeClips(self.microphone, self._muted_since, most=clips_config.get("most", 400))
                          if clips_config.get("keep") else None)
        # Just past the microphone position of the last unmute, so a clip can tell it
        # doesn't reach back into what was said while muted. The audio flows on through
        # a mute. See IN_FLIGHT_SECONDS.
        self._unmuted_at = 0

        # What the brain last sent, for while it can't be reached - see fallback.py.
        self.alarms = AlarmCopy()
        self.state = StateCopy()
        # The most seconds one recording may last - 10 until the brain says,
        # as it does whenever it connects. Not saved, as the brain owns it.
        self.longest_request: float = LIMIT_SECONDS
        # The last longest_request from the brain which made no sense, as it was logged.
        self._refused_longest: Optional[str] = None
        self.phrase = Phrase()
        self.ringer = FallbackRinger(self.speaker, self.sounds_folder, self.alarms, self.state)
        # The brain owns the mute, but a satellite which was muted when the brain went
        # down stays muted until it's back to say otherwise. The same goes for how sure
        # the voice check must be.
        if self.wake is not None:
            self.wake.set_muted(self.state.muted)
        self._check_voice_at(self.state.voice_check)

        # Nova's music in Windows' media controls, whose buttons go to the brain - only on
        # a push-to-talk satellite, which only the PC is, and only where winrt can be had,
        # else None and the hello says there are none. See docs/decisions/0031.
        self.media: Optional[WindowsMedia] = None
        if self.push_to_talk:
            media = WindowsMedia(self._media_pressed)
            self.media = media if media.available else None

        # Opening and closing things, and Windows' own volume - only on Windows, and only
        # where the Windows parts of each can be had, else the hello says neither.
        self.opens = False
        self.system_volume: Optional[SystemVolume] = None
        if _on_windows():
            self.opens = pc_windows.load()
            volume = SystemVolume()
            self.system_volume = volume if volume.available else None
        # Windows' microphone, muted by Nova until a time, when it comes back on by itself - or
        # None. Held open says a press has unmuted it for its recording, to be muted again when
        # the recording ends. Both under _mic_lock, held while Windows is asked, so the press,
        # the brain and the time running out never cross. _mic_changed wakes the thread
        # which waits for the time.
        self._mic_lock = threading.Lock()
        self._mic_until: Optional[float] = self._load_mic_mute() if self.system_volume is not None else None
        self._mic_held_open = False
        self._mic_changed = threading.Event()
        # Whether looking at Windows' microphone has failed, so it's only logged the once.
        self._mic_look_failed = False
        # Nova's orb on the PC's screen, while it listens, thinks and speaks there - only
        # on a push-to-talk satellite on Windows, which is the PC, and only where its
        # window can be made, else None and nothing is shown. What it shows is worked out
        # by _orb_states from what the satellite tells it as each thing happens, and
        # nothing the satellite does waits on the orb or fails because of it.
        self.orb: Optional[ScreenOrb] = None
        if self.push_to_talk and _on_windows():
            orb = ScreenOrb()
            self.orb = orb if orb.available else None
        self._orb_states = OrbStates(self._show_orb)
        # The last screen_orb from the brain which made no sense, as it was logged.
        self._refused_orb: Optional[str] = None
        # The things this computer can open, by id, with what launches each - which never
        # leaves it. Built on a thread of its own, and read by the computer requests'.
        self._apps: Dict[str, Dict] = {}
        self._apps_lock = threading.Lock()
        # Counted up each time the brain is reached, so a list sent to the last one is
        # sent again to the next - and what was last sent of each list, with the count it
        # was sent at. Set wakes each list's thread to send it.
        self._connection = 0
        self._sent: Dict[str, Tuple[int, bytes]] = {}
        self._apps_wanted = threading.Event()
        self._windows_wanted = threading.Event()

        # Made in run(), once it's known whether the last run stopped properly.
        self.link: Optional[SatelliteLink] = None
        # Made there too, if this satellite is pressed.
        self.press_listener: Optional[PressListener] = None
        # Guards what the link's thread and the main loop share, below. Also held while
        # the brain is told of a wake.
        self._lock = threading.Lock()
        # Held by the wake word from stopping what plays until the brain has been told
        # of it, so the brain hears of the wake before the end of a reply it stopped -
        # see _report. The stops come before the lock above is taken, as it's held
        # while the brain is sent to.
        self._waking = threading.Lock()
        self._playing: Dict[str, _FromBrain] = {}
        # Where the follow-up the brain asked to hear starts, until the main loop sends it.
        self._listen_from: Optional[int] = None
        # The number the brain gave that follow-up, if it gave one. Set with _listen_from,
        # and only read with it.
        self._listen_recording: Optional[str] = None
        # Set by the brain's stop_listening - one for each recording, while it's sent.
        self._recording: Optional[_Recording] = None
        # Numbers for the recordings the wake word starts, said with each wake so the
        # brain can say which one it's stopping. Never "listen-", as the brain's are.
        self._wake_ids = itertools.count(1)
        # Whether the brain has had all of a recording and not yet answered it - from
        # the recording's end until its allow, at the end of the turn. Losing the brain
        # then says the phrase, as losing it part way through a recording does.
        self._turn_open = False
        # The last wake's timing, until its reply starts to play or its turn ends.
        self._timeline: Optional[Timeline] = None
        # The report of what happened while the brain was away, and what was still
        # ringing, as they were sent on connecting - until the brain says it has them,
        # or is lost. See _on_connected.
        self._handing_over: Optional[Dict[str, List[str]]] = None

        # A press waiting for the main loop, which owns the microphone and so starts the
        # recording - set, with the lock held, by a press while nothing is recording.
        self._press_pending = False
        # Until when, by time.monotonic(), the wake word is ignored - 0 for not at all.
        self._quiet_until = 0.0
        # Set to wake the main loop from waiting for a press: by a press, by the brain
        # asking to hear a follow-up, and by stop().
        self._nudged = threading.Event()

        # What to exit with, once the main loop has been asked to stop - see stop().
        self._exit_code: Optional[int] = None
        # Set once shutting down, so a second signal can't cut the cleanup short.
        self._stopping = False
        self._finished = threading.Event()

        self._handlers: Dict[str, Callable[[Dict, bytes], None]] = {
            "listen": self._listen,
            "stop_listening": self._stop_listening,
            "play_sound": self._play_sound,
            "play_stream": self._play_stream,
            "stream_audio": self._stream_audio,
            "stream_end": self._stream_end,
            "set_playback": self._set_playback,
            "stop": self._stop,
            "silence": lambda message, payload: self.speaker.silence(message["kind"]),
            "allow": self._allow,
            "state": self._take_state,
            "quiet": self._take_quiet,
            "now_playing": self._take_now_playing,
            "alarms": lambda message, payload: self.alarms.replace(message["entries"]),
            "phrase": lambda message, payload: self.phrase.replace(payload, message["rate"]),
            "away_settled": self._away_settled,
            "save_note": self._save_note,
            "computer": self._computer,
            "restart": self._restart,
        }

    def run(self):
        """
        Connects to the brain, and listens for the wake word, until stopped.

        Raises:
            SystemExit: Once stopped - with 1 when the brain asked for a restart.
        """
        # Tells systemd it has started, and from here on that it's still working.
        watchdog.start()
        # However the loop ends, everything is closed down here, once.
        try:
            # Before the brain is reached, so it's told what still rings, and takes it over.
            self.ringer.resume(time.time())
            if self.push_to_talk:
                # Before the link, so one which can't have its port stops before the brain
                # hears of it. A press meanwhile waits for the main loop, after the link.
                self.press_listener = PressListener(self.press_port, self._pressed)
                self.press_listener.start()
            self.link = SatelliteLink(self.address, self.name, self.hostname, self.password, self.sample_rate,
                                      self._note_start(), self._on_message, self._on_connected,
                                      speaker=self.spotify_speaker, push_to_talk=self.push_to_talk,
                                      machine=self.machine, saves_notes=self.saves_notes,
                                      media_controls=self.media is not None, opens=self.opens,
                                      system_volume=self.system_volume is not None)
            self.link.start()
            threading.Thread(target=self._ring_when_due, daemon=True, name="fallback").start()
            if self.system_volume is not None:
                threading.Thread(target=self._unmute_mic_when_due, daemon=True, name="microphone").start()
            if self.opens:
                threading.Thread(target=self._keep_apps, daemon=True, name="apps").start()
                threading.Thread(target=self._keep_windows, daemon=True, name="windows").start()
            logger.info(f"Satellite '{self.name}' ready, for its brain at {self.address}")
            self._listen_forever()
        finally:
            self._clean_up()

    def stop(self, code: int = 0):
        """
        Has the main loop stop, from any thread, once it's next between pieces of audio.

        Args:
            code: What the program exits with. 1 has systemd start it again - see
                voice-assistant.service.
        """
        self._exit_code = code
        self._nudged.set()

    def shutdown(self, *args):
        """
        Ctrl+C or systemd's stop: raises SystemExit in whatever the main loop was doing,
        and run() cleans up once that has unwound. A second signal while it's shutting
        down is ignored, so it can't cut the cleanup short.
        """
        if self._stopping:
            return
        self._stopping = True
        logger.info("Shutting down...")
        raise SystemExit(0)

    def _interrupted(self) -> bool:
        """Whether listening for the wake word has to stop - for a follow-up, or to stop altogether."""
        return self._listen_from is not None or self._exit_code is not None

    def _exit_if_asked(self):
        if self._exit_code is not None:
            raise SystemExit(self._exit_code)

    def _listen_forever(self):
        """
        Listens for the wake word - or, push-to-talk, waits for a press - and sends each
        follow-up the brain asks to hear. The one loop owns the microphone, so while a
        recording is being sent, the wake word can't start another.
        """
        while True:
            try:
                # First, as well as once listening returns, since listening which keeps
                # going wrong never returns - which is when the brain's Restart, from
                # Home Assistant, is needed most.
                self._exit_if_asked()
                if self.wake is None:
                    pressed = self._wait_for_presses()
                    self._exit_if_asked()
                    if pressed:
                        # From where the microphone has got to now, as there's no wake
                        # word a moment before it, and nothing is kept from before.
                        position = self.microphone.position
                        self._begin(position, position, pressed=True)
                    else:
                        self._send_follow_up()
                    continue
                heard = self.wake.listen_once(until=self._interrupted)
                self._exit_if_asked()
                if heard is not None and time.monotonic() < self._quiet_until:
                    # Most likely Nova's own voice, from another satellite's speakers.
                    logger.info("Heard the wake word while Nova speaks on another satellite, so it's ignored")
                    continue
                if heard is not None:
                    self._keep_clip(heard)
                    heard_at = self.wake.heard_at
                    # The recording starts a moment before the wake word was heard - see pre_roll.
                    self._begin(heard_at, max(0, heard_at - round(self.pre_roll * RATE)))
                else:
                    self._send_follow_up()
            # One thing going wrong mustn't stop the satellite for good - but it waits a
            # moment, in case it would only go wrong again straight away.
            except Exception as e:
                logger.error(f"Error: {e}", exc_info=True)
                time.sleep(1)

    def _wait_for_presses(self) -> bool:
        """
        What a push-to-talk satellite does in the wake word's place: waits until it's
        pressed, the brain asks to hear a follow-up, or the satellite is to stop. Tells
        the watchdog it's still working as it waits, which nothing else does while the
        microphone is closed.

        Returns:
            True for a press, which the caller starts a recording for - False when
            interrupted, as the wake word's listening returns None.
        """
        while True:
            watchdog.beat()
            with self._lock:
                interrupted = self._interrupted()
                pressed, self._press_pending = self._press_pending and not interrupted, False
            if interrupted or pressed:
                # A press which came as the brain asked to hear an answer is that answer
                # starting, which the follow-up is sent for - not a recording of its own.
                return pressed
            self._nudged.wait(PRESS_WAIT)
            # Before looking again, so a nudge during that look isn't lost.
            self._nudged.clear()

    def _pressed(self):
        """
        Acts on a press of the key, from the press listener's thread. While a recording is
        being sent it ends it, as the brain's saying the user has stopped does, and the
        recording loop tells the brain. Otherwise it hands the press to the main loop,
        which owns the microphone and starts the recording - see _begin. Mute makes no
        difference: a press is deliberate.
        """
        with self._lock:
            recording = self._recording
            if recording is not None and not recording.is_set():
                recording.pressed = True
                # Before it is set, so the orb is thinking before the main loop sends the
                # recording's end, and so before the brain can answer it.
                self._orb_states.listening_ended(True)
                recording.set()
                # As _stop_listening does, so losing the brain now says it can't be reached.
                self._turn_open = True
                logger.info("Pressed: the recording ends")
                return
            self._press_pending = True
        logger.info("Pressed: listening")
        self._nudged.set()

    def _unmute_windows(self):
        """
        Unmutes Windows as a press starts listening, so its sound and Nova's reply are
        heard - and its microphone, for this turn only, so Nova hears him and his answer to
        any question she asks (see _mute_mic_again). Before the sound and the recording, and
        never in their way: a failure is logged and the press goes on.
        """
        if self.system_volume is None:
            return
        try:
            if self.system_volume.unmute_if_muted():
                logger.info("Windows was muted, so it's unmuted for the press")
        except Exception as e:
            logger.warning(f"Couldn't check whether Windows is muted ({type(e).__name__})")
            if isinstance(e, TimeoutError):
                # Its thread is stuck, so the microphone would only wait as long again.
                return
        # Muted by Nova or by hand, it's the same: on for the turn, and muted again after. Only
        # ever set here, never cleared: a second press in the same turn finds it on, and the
        # turn's end still owes it the mute.
        with self._mic_lock:
            try:
                opened = self.system_volume.unmute_microphone_if_muted()
            except Exception as e:
                logger.warning(f"Couldn't check whether Windows' microphone is muted ({type(e).__name__})")
                return
            if opened:
                self._mic_held_open = True
        if opened:
            logger.info("Windows' microphone was muted, so it's unmuted for this turn")

    def _mute_mic_again(self):
        """
        Mutes Windows' microphone again once a press's turn is over - when the brain lets
        announcements back (allow), after any question and its answer, or when the brain is
        lost or the satellite stops - if the press unmuted it, the hour of a mute by Nova
        carrying on as it was. Not if the hour ran out meanwhile, or the brain has since been
        told to mute or unmute it, which have each let it go.
        """
        if self.system_volume is None:
            return
        with self._mic_lock:
            if not self._mic_held_open:
                return
            self._mic_held_open = False
            try:
                self.system_volume.mute_microphone(True)
            except Exception as e:
                logger.warning(f"Couldn't mute Windows' microphone again after the recording ({type(e).__name__})")
                return
        logger.info("Windows' microphone is muted again, as it was before the press")

    def _keep_clip(self, score: float):
        """
        Hands the wake over to be kept, if that's switched on. A problem with a clip
        mustn't cost the wake itself, which would wait a second and hear nothing.

        Args:
            score: How sure the wake word detector was.
        """
        if self.clips is None or self.wake is None:
            return
        try:
            heard_at = self.wake.heard_at
            if self._muted_since(clip_start(heard_at)):
                logger.info("Wake clip not kept, muted")
                return
            self.clips.keep(heard_at, score)
        except Exception as e:
            logger.error(f"Couldn't keep a clip of a wake: {e}")

    def _making_sound(self) -> bool:
        """
        Whether the satellite is making a sound of its own, or just was - for the wake
        word, which then counts without the voice check. Called on the listening thread,
        so it never takes the satellite's lock, which is held while the brain is sent to
        and while an alarm starts ringing. The speaker's answer takes no lock, and the
        ringer's only one held for a moment.

        Returns:
            True while anything plays - an alarm, a reply, music, a chime, from the brain
            or from the copy - or did within OWN_SOUND_LINGERS, or the copy is ringing
            an alarm, as it is right through its ring.
        """
        return self.speaker.sounded_within(OWN_SOUND_LINGERS) or self.ringer.ringing

    def _muted_since(self, position: int) -> bool:
        """
        Whether any of the audio from a position on was heard muted, or may have been -
        muted now, or unmuted after it or too soon after it to be sure (see
        IN_FLIGHT_SECONDS). The second covers a mute and an unmute which both came and
        went since then, as long as _take_state notes the position first.

        Args:
            position: A microphone position.

        Returns:
            True if a clip from there would take in some of a mute, or might.
        """
        muted = self.wake.muted if self.wake is not None else self.state.muted
        return muted or position < self._unmuted_at

    def _begin(self, heard_at: int, start: int, pressed: bool = False):
        """
        Acts on the wake word, or a press, at once - stopping, pausing and dismissing, as
        Nova always has - then tells the brain, and sends it what the user says.

        Args:
            heard_at: The microphone position it was heard or pressed at.
            start: Where the recording starts, a moment earlier for the wake word - see
                pre_roll - and the same for a press.
            pressed: Whether it was a press, which also plays the satellite's own sound
                for it, and says so to the brain.
        """
        recording = None
        with self._waking:
            # Without waiting for the lock, which is held while the brain is sent to - up
            # to 2 seconds on a full socket, with the reply, what rings and the music
            # going on all that while. What the brain is playing is copied in one go, so
            # the link's thread starting or ending one meanwhile can't upset it.
            playing = list(self._playing.values())
            talked_over = any(each.kind == "reply" and not each.playback.done for each in playing)
            self.speaker.stop(["reply"])
            self.speaker.silence("announcement")
            for each in playing:
                if each.kind == "music":
                    each.playback.paused = True
            with self._lock:
                self._start_timeline()
                dismissing = self.ringer.ringing
                if dismissing:
                    # While the brain is away, the wake word dismisses what rings, and that's all.
                    self.ringer.dismiss()
                    self._save_timeline(outcome="dismissed")
                else:
                    # Made before the brain hears of the wake, which it could answer with
                    # stop_listening straight away.
                    recording_id = f"wake-{next(self._wake_ids)}"
                    recording = self._recording = _Recording(recording_id)
                    recording.from_press = pressed
                    self._turn_open = False
                    # Any press which came while this was being set up is this one's, so
                    # it doesn't go on to end the recording just begun, or start another.
                    self._press_pending = False
                    # Saying how much of the recording is from before the wake word, so the
                    # brain knows without a setting of its own, which could differ. Saying
                    # it was a press only when it was, so a wake word's message is as it was.
                    fields = {"pressed": True} if pressed else {}
                    if not self.link.send("wake", talked_over=talked_over, pre_roll=heard_at - start,
                                          recording=recording_id, **fields):
                        recording = self._recording = None
        if dismissing:
            self.speaker.allow("announcement")
            return
        if pressed and recording is not None:
            # After the reply was stopped, which would have stopped this with it, as it's
            # the same kind of sound. Not when the brain can't be reached, which is told
            # the phrase and nothing else.
            self._unmute_windows()
            self.speaker.play_file(os.path.join(self.sounds_folder, PRESS_SOUND),
                                   volume=self.state.volume("general"))
        ended = "dropped" if recording is None else self._send_recording(start, heard_at - start, recording)
        with self._lock:
            # The brain asked to hear an answer while this was being sent, having
            # finished a question just as the wake word was said - and this recording,
            # which it has had, is what came next. Sent again as a follow-up, it was
            # ten seconds of nobody talking, with the wake word going unheard.
            self._listen_from = None
            if ended == "dropped":
                self._save_timeline(outcome="no_brain")
        if ended == "dropped":
            # No turn follows, nor perhaps a disconnect, if the brain went just as this began.
            self._mute_mic_again()
            self._say_cant_reach()

    def _send_follow_up(self):
        """Sends the brain the user's answer to its question, from the moment it asked to hear it."""
        with self._lock:
            start, self._listen_from = self._listen_from, None
            if start is None:
                return
            recording = self._recording = _Recording(self._listen_recording)
            self._turn_open = False
        if self._send_recording(start, 0, recording) == "dropped":
            self._say_cant_reach()

    def _send_recording(self, start: int, pre_roll: int, stopped: _Recording) -> str:
        """
        Sends the brain what the microphone hears, until it says the user has stopped.

        Args:
            start: The microphone position to send from.
            pre_roll: How many samples from there were heard before the wake word,
                which go first, as a piece of their own - 0 for a follow-up.
            stopped: Set once the brain says the user has stopped.

        Returns:
            Why it ended: "stopped" by the brain, "pressed" by the key, at the "limit", or
            "dropped" with the connection.
        """
        # Opened for the recording, push-to-talk, and closed again once it's over.
        self.microphone.hold()
        limit = round(self.longest_request * RATE)
        sizes = itertools.chain([pre_roll] if pre_roll else [], itertools.repeat(round(PIECE_SECONDS * RATE)))
        sent = 0
        try:
            self._orb_states.listening_started()
            if self.push_to_talk:
                # Nothing from before the microphone opened: what it still holds from
                # then is the last recording's.
                start = max(start, self.microphone.position)
            reader = self.microphone.listen(start=start)
            for size in sizes:
                audio = reader.read(min(size, limit - sent))
                self._exit_if_asked()
                if stopped.is_set():
                    if not stopped.pressed:
                        return "stopped"
                    # Told from here, with the pieces, so no piece reaches the brain after it.
                    self.link.send("audio_end", reason="pressed")
                    return "pressed"
                if not self.link.send("audio", audio.tobytes()):
                    logger.warning("Lost the brain part way through a recording")
                    return "dropped"
                sent += len(audio)
                if sent >= limit:
                    with self._lock:
                        # Unless the brain said the user had stopped while this last
                        # piece was being sent - when it opened the turn itself, and
                        # may have ended it since, with nothing to say.
                        if not stopped.is_set():
                            self._turn_open = True
                            self._orb_states.listening_ended(True)
                    self.link.send("audio_end", reason="limit")
                    return "limit"
        except BaseException:
            # Given up on part way, as the satellite stops, so the brain isn't left
            # waiting for the rest.
            self.link.send("audio_end", reason="cancelled")
            raise
        finally:
            # A recording which ended as it should - the brain's stop, a second press or the
            # limit - was told to the orb where it was ended, so this changes nothing for
            # it. It ends one which was dropped or given up on, which leaves Nova nothing
            # to work on. First, so whatever fails below, the orb isn't left listening.
            self._orb_states.listening_ended(False)
            self.microphone.release()
            with self._lock:
                if self._recording is stopped:
                    self._recording = None

    def _say_cant_reach(self):
        """
        Says the brain can't be reached, in the brain's own recorded voice, and lets
        what rings by itself ring again - which the brain would have, had it answered.
        """
        self.phrase.play(self.speaker, self.state.volume("general"))
        self.speaker.allow("announcement")

    def _start_timeline(self):
        """Times a wake from now. Called with the lock held."""
        # One still waiting for its reply never got one - nothing was heard, most likely.
        self._save_timeline()
        self._timeline = Timeline()
        self._timeline.mark("wake")

    def _save_timeline(self, **notes):
        """
        Saves the last wake's timing, if it hasn't been already. Called with the lock held.

        Args:
            notes: Anything which explains the times, e.g. outcome="dismissed".
        """
        if self._timeline is not None:
            self._timeline.note(**notes)
            self._timeline.save(self.latency_file)
            self._timeline = None

    def _on_message(self, message: Dict, payload: bytes):
        """Does what the brain asks. Called on the link's own thread."""
        handler = self._handlers.get(message["type"])
        if handler is None:
            logger.warning(f"The brain sent '{message['type']}', which this satellite doesn't know")
            return
        handler(message, payload)

    def _listen(self, message: Dict, payload: bytes):
        recording = message.get("recording")
        recording = recording if isinstance(recording, str) else None
        # From the moment it's asked for - before the brain's listening sound - so an
        # answer started over the sound isn't lost.
        with self._lock:
            self._listen_from = self.microphone.position
            self._listen_recording = recording
            self._nudged.set()
            # A recording being sent now is the answer, rather than one after it - see
            # _begin - so the brain may stop it as that.
            if self._recording is not None and recording is not None:
                self._recording.ids.add(recording)

    def _stop_listening(self, message: Dict, payload: bytes):
        # Only ever the recording being sent now. One which has already ended by itself,
        # at the limit, is over whatever the brain says - and a stop meant for it, which
        # crossed the next wake on its way, leaves that next one going. A brain which
        # doesn't say which recording means whichever is being sent.
        said = message.get("recording")
        said = said if isinstance(said, str) else None
        with self._lock:
            if self._recording is not None and (said is None or said in self._recording.ids):
                # Before it is set, and here rather than once the main loop sees it's
                # over: the brain's allow, for a turn with nothing to say, follows on this
                # thread within milliseconds, and the main loop can be a piece of audio
                # behind. Told after it, the orb would be left thinking.
                self._orb_states.listening_ended(True)
                self._recording.set()
                # Here rather than once the main loop sees it's stopped, which can be
                # after the brain's allow has arrived, when it has nothing to say.
                self._turn_open = True

    def _allow(self, message: Dict, payload: bytes):
        # Sent as the brain's turn ends, whatever came of it.
        with self._lock:
            # A wake with no reply is timed now, rather than at the next wake - unless its
            # recording is still going, when this is the end of the turn before, which
            # the brain ended just as the wake word was heard.
            if self._turn_open:
                self._save_timeline()
            self._turn_open = False
            # The end of the turn before, which came as a press began this one: its
            # recording still has the microphone, and this turn's own end mutes it.
            recording = self._recording
            stale = recording is not None and not recording.is_set()
        # A turn which said nothing is over, so the orb isn't left waiting for its words.
        self._orb_states.turn_ended()
        self.speaker.allow(message["kind"])
        if not stale:
            self._mute_mic_again()

    def _play_sound(self, message: Dict, payload: bytes):
        kind = message.get("kind", "reply")
        # Only ever one of the satellite's own sounds, whatever the name says.
        filename = os.path.join(self.sounds_folder, os.path.basename(message["name"]))
        playback = self.speaker.play_file(filename, volume=message.get("volume", 1.0), kind=kind,
                                          start=message.get("start") or 0.0, length=message.get("length"))
        if playback is None:
            # It can't be played, so the brain mustn't wait for it to finish.
            self.link.send("played", id=message["id"], stopped=False, position=0)
            return
        self._follow(message["id"], _FromBrain(playback, kind))
        self._stop_if_listening(playback, kind)

    def _play_stream(self, message: Dict, payload: bytes):
        kind = message.get("kind", "reply")
        waiting = queue.Queue()
        playback = self.speaker.play_stream(_pieces(waiting), sample_rate=message.get("rate"),
                                            volume=message.get("volume", 1.0), kind=kind,
                                            paused=bool(message.get("paused", False)))
        self._follow(message["id"], _FromBrain(playback, kind, message.get("channels", 1), waiting))
        self._stop_if_listening(playback, kind)

    def _stop_if_listening(self, playback: Playback, kind: str):
        """
        Stops a reply which starts while a press's recording is open - pressed while the
        brain was still working on the last request, say. It would play into the open
        microphone, which has no echo cancelling here, and come back as the user's next
        request. So it goes as one a press over it would have, and the brain is told it was
        stopped. Only a press's recording: the brain's own listen, for the answer to a
        question, keeps the chime it plays. The Pi cancels its own echo, so its replies
        play as they always have.

        Args:
            playback: The reply, just started.
            kind: What the brain says it is.
        """
        if not self.push_to_talk or kind != "reply":
            return
        # _waking too, so a press still setting its recording up is waited for rather than
        # missed - in the same order _begin takes them.
        with self._waking, self._lock:
            recording = self._recording
            listening = recording is not None and recording.from_press and not recording.is_set()
        if listening:
            logger.info("A reply came while listening after a press, so it isn't played")
            playback.stop()

    def _stream_audio(self, message: Dict, payload: bytes):
        with self._lock:
            playing = self._playing.get(message.get("id"))
            # One the satellite has stopped, which the brain hasn't heard about yet.
            if playing is None or playing.pieces is None or playing.playback.done:
                return
        audio = np.frombuffer(payload, np.int16).astype(np.float32) / 32768
        if playing.channels > 1:
            audio = audio.reshape(-1, playing.channels)
        playing.pieces.put(audio)

    def _stream_end(self, message: Dict, payload: bytes):
        with self._lock:
            playing = self._playing.get(message.get("id"))
        if playing is not None:
            playing.let_go()

    def _set_playback(self, message: Dict, payload: bytes):
        with self._lock:
            playing = self._playing.get(message.get("id"))
        # The satellite owns what plays, so one which has already ended stays ended.
        if playing is None:
            return
        if "paused" in message:
            playing.playback.paused = bool(message["paused"])
        if "volume" in message:
            playing.playback.volume = max(0.0, min(1.0, float(message["volume"])))

    def _stop(self, message: Dict, payload: bytes):
        # One of the brain's playbacks by its id - a volume beep, say, without the reply
        # playing alongside it - or whole kinds of sound.
        if "id" not in message:
            self.speaker.stop(message.get("kinds"))
            return
        with self._lock:
            playing = self._playing.get(message["id"])
        # One which has already ended has been reported, or is about to be.
        if playing is not None:
            playing.playback.stop()

    def _take_state(self, message: Dict, payload: bytes):
        self.state.update(message)
        longest = message.get("longest_request")
        # Anything else is left alone, so an old or confused brain can't make a
        # recording of no length or one which never ends: "15" as text, True, NaN, 0,
        # or 1e-9, which is no audio at all. The brain's own setting is at least 5.
        if isinstance(longest, (int, float)) and not isinstance(longest, bool) and 1 <= longest < math.inf:
            self.longest_request = min(longest, MOST_SECONDS)
        elif longest is not None and ascii(longest) != self._refused_longest:
            # Once for each such value, as the brain sends it with every state - kept as
            # text, as NaN is never equal to itself. One missing is from an old brain.
            self._refused_longest = ascii(longest)
            logger.warning(f"The brain's longest_request makes no sense, so a recording still lasts up to "
                           f"{self.longest_request:g} seconds: {ascii(longest)[:80]}")
        # The orb's settings come with the state of a push-to-talk satellite, as the longest
        # request does - and one the orb can't take is left, the same way.
        if self.orb is not None:
            self._take_orb_setting(message.get("screen_orb"))
        # A push-to-talk satellite has no wake word to tune, to check a voice for or to
        # note an unmute for: only the mute, which the state copy has just taken.
        if self.wake is None:
            return
        confidence = message.get("wake_confidence")
        # Held to the same care, as the Pi has nobody at the keyboard to notice. Only 0.05
        # to 0.99 is taken: near 0 the wake word fires on any noise, and at 1 it all but
        # never fires - a score of exactly 1.00 came in 2 of about 500 wakes here. Text
        # such as "0.75", True or NaN is left alone too.
        if (isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                and 0.05 <= confidence <= 0.99 and confidence != self.wake.sensitivity):
            # A plain assignment is enough: the listening thread only reads it, one
            # score at a time, and a change taking effect on the next one is fine.
            self.wake.sensitivity = confidence
            logger.info(f"Listening for the wake word at {confidence} now")
        # From the copy, which has just taken the message's, if it made sense.
        self._check_voice_at(self.state.voice_check)
        # Before the unmute takes effect, so no wake or clip can ever see it unmuted with
        # this not yet noted. The brain sends this with every state, so only a real change counts.
        if self.wake.muted and not self.state.muted:
            self._unmuted_at = self.microphone.position + round(IN_FLIGHT_SECONDS * RATE)
        self.wake.set_muted(self.state.muted)

    def _take_orb_setting(self, setting):
        """
        Passes the orb the settings the brain sent for it, if they make sense.

        Args:
            setting: The state's screen_orb, which should be {"on": a bool, "place": one of
                PLACES, "size": one of SIZES}. None when the brain doesn't send it, which
                is nothing to say. Anything else is left alone and logged, once for each
                such value, as the brain repeats it with every state.
        """
        if setting is None:
            return
        if (isinstance(setting, dict) and isinstance(setting.get("on"), bool)
                and isinstance(setting.get("place"), str) and setting["place"] in PLACES
                and isinstance(setting.get("size"), str) and setting["size"] in SIZES):
            self.orb.configure(setting["on"], setting["place"], setting["size"])
        elif ascii(setting) != self._refused_orb:
            self._refused_orb = ascii(setting)
            logger.warning(f"The brain's screen_orb makes no sense, so the orb stays as it was: "
                           f"{ascii(setting)[:80]}")

    def _show_orb(self, state: str):
        """Has the orb show a state, which OrbStates works out. Never waits - see ScreenOrb."""
        orb = self.orb
        if orb is not None:
            orb.show(state)

    def _check_voice_at(self, sureness: Optional[float]):
        """
        Has the wake word count only when the voice check is this sure it's the user's voice.

        Args:
            sureness: From 0, for no check, to 0.99, as the state copy keeps it - having
                left out anything else. None, before the brain has ever said, changes nothing.
        """
        if self.wake is None or sureness is None or sureness == self.wake.voice_check_threshold:
            return
        # A plain assignment, as with the sensitivity.
        self.wake.voice_check_threshold = sureness
        if not sureness:
            logger.info("Not checking it's your voice now")
        elif self.wake.voice_check is None:
            # Not "now", which would mislead the first look at the log.
            logger.info(f"Checking it's your voice at {sureness} once a voice check's been trained")
        else:
            logger.info(f"Checking it's your voice at {sureness} now")

    def _away_settled(self, message: Dict, payload: bytes):
        # The brain has saved what it was told as the satellite connected, and rings on
        # whatever was still ringing here - so the satellite forgets it, and stops. What
        # it stops may ring from the copy again, if the brain is lost while ringing it.
        with self._lock:
            sent, self._handing_over = self._handing_over, None
            if sent is None:
                return
            self.alarms.clear_report(sent)
            self.ringer.quiet()
            self.alarms.taken_over(sent["ringing"])

    def _save_note(self, message: Dict, payload: bytes):
        id = message.get("id")
        if not isinstance(id, str):
            # Nothing could be answered, and the brain has given up waiting by now.
            logger.warning("The brain asked for a note to be saved without an id, so it wasn't")
            return
        title = message.get("title")
        # On a thread of its own, as this is the link's reader: the disk, or Windows
        # looking for the folder, never holds up hearing the brain or answering its pings.
        threading.Thread(target=self._write_note, args=(id, title if isinstance(title, str) else "", payload),
                         daemon=True, name="save-note").start()

    def _write_note(self, id: str, title: str, payload: bytes):
        """
        Saves a note in the Downloads folder and tells the brain how it went.

        What the note says is never logged, here or in anything which fails, as it may
        be anything the user said.

        Args:
            id: What the brain knows the note by, to say in the answer.
            title: What to call it.
            payload: What it says, as UTF-8.
        """
        try:
            # Looked for now, not when the satellite started: it may have been made or
            # moved since.
            try:
                folder = downloads_folder()
            except Exception as e:
                logger.warning(f"Couldn't look for a Downloads folder ({type(e).__name__}: {e})")
                folder = None
            if folder is None:
                raise NoteRefused("There's no Downloads folder to save it in.")
            try:
                # surrogatepass, as the brain's encoding of a broken reply's lone
                # surrogate is, for save_note to refuse it as it refuses any other.
                text = payload.decode("utf-8", "surrogatepass")
            except UnicodeDecodeError as e:
                raise NoteRefused("The file couldn't be written.") from e
            file = save_note(folder, title, text, sys.platform == "win32")
        except NoteRefused as e:
            logger.info(f"Didn't save a note: {e}")
            self.link.send("note_saved", id=id, error=str(e))
        except Exception as e:
            # Only what kind of failure, as some exceptions carry what they were handed.
            logger.error(f"Saving a note failed ({type(e).__name__})")
            self.link.send("note_saved", id=id, error="The file couldn't be written.")
        else:
            logger.info(f"Saved a note as {file}")
            self.link.send("note_saved", id=id, file=file)

    def _send_list(self, kind: str, items: list):
        """
        Sends the brain one of the lists, apps or windows, as a JSON payload - unless this
        brain already has it as it is.
        """
        payload = json.dumps(items, ensure_ascii=False).encode("utf-8")
        if len(payload) > protocol.MAX_AUDIO:
            logger.warning(f"The {kind} list is too long to send, so the brain keeps the last one")
            return
        connection = self._connection
        if self._sent.get(kind) == (connection, payload) or self.link is None:
            return
        if self.link.send(kind, payload):
            self._sent[kind] = (connection, payload)

    def _keep_apps(self):
        """
        Builds the list of things this computer can open, as it starts and every
        APPS_EVERY seconds after, and sends it to the brain - without what launches each.
        """
        build_at = 0.0
        while not self._finished.is_set():
            self._apps_wanted.clear()
            if time.monotonic() >= build_at:
                try:
                    items = pc_apps.catalogue()
                except Exception as e:
                    logger.warning(f"Couldn't list what this computer can open ({type(e).__name__})")
                else:
                    with self._apps_lock:
                        self._apps = {item["id"]: item for item in items}
                    # So a game's window is known by its name here, as "close Fortnite" says it.
                    pc_windows.set_installed(items)
                    logger.info(f"Found {len(items)} things this computer can open")
                build_at = time.monotonic() + APPS_EVERY
            with self._apps_lock:
                apps = list(self._apps.values())
            self._send_list("apps", [{key: app[key] for key in ("id", "name", "kind")} for app in apps[:MOST_APPS]])
            self._apps_wanted.wait(max(0.0, build_at - time.monotonic()))

    def _keep_windows(self):
        """Looks at which programs have a window open every WINDOWS_EVERY seconds, and sends the brain their names."""
        failing = False
        while not self._finished.is_set():
            self._windows_wanted.clear()
            try:
                names = pc_windows.open_windows()
            except Exception as e:
                # Once until it works again, as it's looked at every few seconds.
                if not failing:
                    logger.warning(f"Couldn't list the open windows ({type(e).__name__})")
                failing = True
            else:
                failing = False
                self._send_list("windows", names[:MOST_WINDOWS])
            self._windows_wanted.wait(WINDOWS_EVERY)

    def _computer(self, message: Dict, payload: bytes):
        id = message.get("id")
        if not isinstance(id, str):
            # Nothing could be answered, and the brain has given up waiting by now.
            logger.warning("The brain asked for something on this computer without an id, so it wasn't done")
            return
        # On a thread of its own, as this is the link's reader: Windows opening something,
        # or bringing it to the front, never holds up hearing the brain.
        threading.Thread(target=self._do_on_computer, args=(id, message), daemon=True, name="computer").start()

    def _do_on_computer(self, id: str, message: Dict):
        """
        Does what the brain asked on this computer and tells it how it went - then, for
        something opened, brings it to the front.

        Args:
            id: What the brain knows the request by, to say in the answer.
            message: The computer message, with its action and what that needs.
        """
        action = message.get("action")
        then = None
        try:
            answer, then = self._on_computer(action, message)
        except Exception as e:
            # Only what kind of failure, as some exceptions carry what they were handed.
            logger.error(f"Something asked of this computer failed ({type(e).__name__})")
            answer = {"error": CANT_DO}
        if not self.link.send("computer_done", id=id, **answer):
            logger.info("The brain can't be reached to say how something asked of this computer went")
        if then is not None:
            then()

    def _on_computer(self, action, message: Dict) -> Tuple[Dict, Optional[Callable[[], object]]]:
        """
        Does one action of protocol.COMPUTER_ACTIONS.

        Returns:
            The answer for computer_done - {"said": ...} or {"error": ...}, and for volume
            the level and mute, for the microphone its mute - and what to do once it's sent,
            if anything.
        """
        if action == "volume":
            return self._change_volume(message), None
        if action == "mic":
            return self._mute_microphone(message), None
        if not self.opens or action not in protocol.COMPUTER_ACTIONS:
            return {"error": CANT_DO}, None
        if action == "open":
            # Only ever something from this computer's own list, by the id it gave it.
            item = message.get("item")
            with self._apps_lock:
                app = self._apps.get(item) if isinstance(item, str) else None
            if app is None:
                logger.info("Asked to open something which isn't in this computer's list")
                return {"error": NOT_HERE}, None
            logger.info(f"Opening {app['name']}")
            return self._open(app["target"], f"Opening {app['name']}.", browser=False)
        if action == "open_url":
            # Checked again here, so nothing but an https:// address is ever opened.
            url = message.get("url")
            checked = safe_url(url) if isinstance(url, str) else None
            if checked is None:
                logger.info("Refused to open something which isn't a web address it may")
                return {"error": NOT_A_WEB_ADDRESS}, None
            # The host alone: the rest of an address can hold what's his.
            logger.info(f"Opening a page on {url_host(checked)}")
            return self._open(checked, f"Opening {url_host(checked)}.", browser=True)
        if action == "close":
            name = message.get("name")
            if not isinstance(name, str) or not name.strip():
                return {"error": CANT_DO}, None
            closed = pc_windows.close(name)
            if closed is None:
                return {"error": f"{name.strip()} isn't open."}, None
            logger.info(f"Closing {closed}")
            return {"said": f"Closing {closed}."}, None
        if action == "close_front":
            closed = pc_windows.close_front()
            if closed is None:
                return {"error": NOTHING_TO_CLOSE}, None
            logger.info(f"Closing {closed}, which was in front")
            return {"said": f"Closing {closed}."}, None
        # One added to the protocol which this satellite doesn't know yet.
        return {"error": CANT_DO}, None

    def _open(self, target: str, said: str, browser: bool) -> Tuple[Dict, Optional[Callable[[], object]]]:
        """Opens an app's target or a web address, then has it brought to the front once the brain has its answer."""
        before = pc_windows.ready_to_open()
        try:
            pc_windows.start(target)
        except OSError as e:
            logger.warning(f"Windows couldn't open it ({type(e).__name__})")
            return {"error": COULDNT_OPEN}, None
        return {"said": said}, lambda: pc_windows.bring_to_front(before, browser=browser)

    def _change_volume(self, message: Dict) -> Dict:
        """Changes Windows' own volume as the brain asked, and says what it is now."""
        if self.system_volume is None:
            return {"error": NO_VOLUME}
        change = {}
        direction, step, level, mute = (message.get(key) for key in ("direction", "step", "level", "mute"))
        if direction in ("up", "down"):
            change["direction"] = direction
            if _whole(step) and 0 < step <= 100:
                change["step"] = step
        if _whole(level):
            change["level"] = level
        if isinstance(mute, bool):
            change["mute"] = mute
        try:
            now = self.system_volume.change(**change)
        except Exception as e:
            logger.warning(f"Couldn't change Windows' volume ({type(e).__name__})")
            return {"error": NO_VOLUME}
        logger.info(f"Windows' volume is at {now['level']} percent{', muted' if now['muted'] else ''}")
        return {"said": "Muted." if now["muted"] else f"Volume {now['level']} percent.", **now}

    def _mute_microphone(self, message: Dict) -> Dict:
        """
        Mutes or unmutes Windows' own microphone as the brain asked, and says whether it's muted
        now. A mute lasts the minutes the brain sent (MIC_MUTE_MINUTES if none, from an older
        brain), each mute starting them again; an unmute ends them.
        """
        mute, minutes = message.get("mute"), message.get("minutes", MIC_MUTE_MINUTES)
        if not isinstance(mute, bool) or not (_whole(minutes) and minutes > 0):
            return {"error": CANT_DO}
        if self.system_volume is None:
            return {"error": NO_MICROPHONE}
        with self._mic_lock:
            try:
                muted = self.system_volume.mute_microphone(mute)
            except Exception as e:
                logger.warning(f"Couldn't change Windows' microphone ({type(e).__name__})")
                return {"error": NO_MICROPHONE}
            # Said by voice, so a press's recording ending doesn't change it back.
            self._mic_held_open = False
            self._keep_mic_mute(time.time() + minutes * MINUTE if muted else None)
        self._mic_changed.set()
        logger.info(f"Windows' microphone is {f'muted for {minutes} minutes' if muted else 'on'}")
        return {"said": "Microphone off." if muted else "Microphone on.", "muted": muted}

    def _load_mic_mute(self) -> Optional[float]:
        """When a mute of Windows' microphone by Nova from before a restart ends - or None for none."""
        try:
            kept = storage.load_json(MIC_MUTE_FILE)
        except (OSError, ValueError) as e:
            logger.warning(f"Couldn't read when Windows' microphone comes back on ({type(e).__name__})")
            return None
        until = kept.get("until") if isinstance(kept, dict) else None
        return float(until) if isinstance(until, (int, float)) and not isinstance(until, bool) else None

    def _keep_mic_mute(self, until: Optional[float]):
        """Notes when Windows' microphone comes back on, or that it won't, here and in MIC_MUTE_FILE. Call with _mic_lock held."""
        self._mic_until = until
        try:
            if until is None:
                if os.path.exists(MIC_MUTE_FILE):
                    os.remove(MIC_MUTE_FILE)
            else:
                storage.save_json(MIC_MUTE_FILE, {"until": until})
        except OSError as e:
            # Kept here all the same: only a restart before it's due would lose it.
            logger.warning(f"Couldn't keep when Windows' microphone comes back on ({type(e).__name__})")

    def _unmute_mic_when_due(self):
        """Waits for a mute of Windows' microphone by Nova to run out, and turns it back on, until the satellite stops."""
        while not self._finished.is_set():
            # Cleared before looking, so a change made while looking isn't missed.
            self._mic_changed.clear()
            with self._mic_lock:
                until = self._mic_until
            if until is not None and time.time() >= until:
                self._mic_mute_ran_out(until)
                continue
            if until is not None and self._mic_on_by_hand(until):
                continue
            # In slices, looking at the clock again after each, as the PC may sleep meanwhile.
            self._mic_changed.wait(None if until is None else min(until - time.time(), MIC_LOOK_EVERY))

    def _mic_on_by_hand(self, until: float) -> bool:
        """
        Ends Nova's mute of Windows' microphone, with nothing played, if he has turned the
        microphone back on himself (his mic's touch button, or Windows) - so a mute of his own
        after that isn't undone when the hour would have run out. Not while a press's turn
        holds it open. Looked at once each slice of the wait for the hour.

        Args:
            until: When the mute is due to run out, so one changed since is left alone.

        Returns:
            Whether the mute was ended.
        """
        with self._mic_lock:
            if self._mic_until != until or self._mic_held_open:
                return False
            try:
                muted = self.system_volume.microphone_muted()
            except Exception as e:
                # Once, rather than every 30 seconds; the hour runs out as it would have.
                if not self._mic_look_failed:
                    logger.warning(f"Couldn't look at whether Windows' microphone is muted ({type(e).__name__})")
                    self._mic_look_failed = True
                return False
            if muted:
                return False
            self._keep_mic_mute(None)
        logger.info("Windows' microphone was turned back on by hand, so Nova's mute of it is over")
        return True

    def _mic_mute_ran_out(self, until: float):
        """
        Turns Windows' microphone back on as Nova's mute runs out, with mic_on.wav - only if it's
        still muted (even by hand during a press's turn), or a press's turn has it open, which
        then leaves it on. One turned back on by hand meanwhile is left as it is, and nothing plays.

        Args:
            until: When it was due, so a mute or unmute since is left alone.
        """
        with self._mic_lock:
            if self._mic_until != until:
                return
            self._keep_mic_mute(None)
            held, self._mic_held_open = self._mic_held_open, False
            try:
                # Asked whatever the press did: he may have muted it by hand during the turn.
                back_on = self.system_volume.unmute_microphone_if_muted() or held
            except Exception as e:
                logger.warning(f"Couldn't turn Windows' microphone back on ({type(e).__name__})")
                return
        if not back_on:
            logger.info("Nova's mute of Windows' microphone ran out, but it was already on")
            return
        logger.info("Nova's mute of Windows' microphone ran out, so it's back on")
        self.speaker.play_file(os.path.join(self.sounds_folder, MIC_ON_SOUND), volume=self.state.volume("general"))

    def _restart(self, message: Dict, payload: bytes):
        logger.info("Restarting, as the brain asked")
        self.stop(1)

    def _follow(self, id: str, playing: _FromBrain):
        """Keeps one of the brain's playbacks by its id, and reports on it until it ends."""
        with self._lock:
            self._playing[id] = playing
        # Here, on the link's thread and before the report's thread can end it, so it
        # starts before it ends, and before whatever the brain sends next. Not for a stream
        # the speaker refused at once - an announcement while a press has them silenced,
        # or any while the speaker can't open - which says nothing, so mustn't end the
        # orb's thinking. One already done here was refused: a stream's end comes later,
        # on this same thread. Worked out once, so its end is told only if its start was.
        playing.counted = playing.spoken and not playing.playback.done
        if playing.counted:
            self._orb_states.speaking_started()
        threading.Thread(target=self._report, args=(id, playing), daemon=True, name=f"played-{id}").start()

    def _report(self, id: str, playing: _FromBrain):
        """
        Tells the brain how far its music has got each second, and when any of its playbacks
        ends - and times a reply's start, for the wake it answers (see _time_reply).
        """
        playback = playing.playback
        # Nova's spoken reply is streamed, where its chimes are sounds.
        if playing.kind == "reply" and playing.pieces is not None:
            self._time_reply(playback)
        while not playback.wait(PROGRESS_EVERY):
            if playing.kind == "music":
                self.link.send("progress", id=id, position=playback.position)
        playing.let_go()
        # Not before the brain has heard of a wake which stopped it. Told the other
        # way round, the brain would take a reply talked over for one which had
        # finished, and the wake for a new request.
        with self._waking:
            pass
        # Before the brain is told, which answers with whatever comes next - a question to
        # listen to, say - and the orb must not still be speaking then.
        if playing.counted:
            self._orb_states.speaking_ended()
        with self._lock:
            if self._playing.get(id) is playing:
                del self._playing[id]
            self.link.send("played", id=id, stopped=playback.stopped, position=playback.position)

    def _time_reply(self, playback: Playback):
        """
        Marks the last wake's reply as the speaker starts to play it, which is when the
        user starts to hear the answer - not as its first audio arrives, which can be a
        while before.

        Args:
            playback: The reply.
        """
        # The wake it's the reply to, so a wake word said again as it starts isn't given it.
        with self._lock:
            timeline = self._timeline
        # Looked at as often as the speaker takes more audio.
        while not playback.position and not playback.wait(BLOCK_SECONDS):
            pass
        # Over before any of it played - talked over, most likely.
        if not playback.position:
            return
        heard = time.monotonic()
        with self._lock:
            if timeline is not None and self._timeline is timeline:
                self._timeline.mark("reply_audio", at=heard)
                self._save_timeline()

    def _on_connected(self, connected: bool):
        """Called on the link's own thread each time the brain is reached, or lost."""
        if not connected:
            # Nothing the brain was doing will end now, so the orb can't wait for it to.
            self._orb_states.lost()
            self._forget_the_brain()
            return
        # A new brain, or the same one started again, has neither list yet.
        self._connection += 1
        self._apps_wanted.set()
        self._windows_wanted.set()
        with self._lock:
            # The brain takes over whatever is still ringing, which it hears is - having
            # been off as it came due, it wouldn't know to ring it otherwise. It's only
            # quietened, and the report forgotten, once the brain says it has saved
            # them (see _away_settled): a brain lost before then may never have, and the
            # next would never hear of them, so an alarm would stop early. The brain
            # starts ringing it before it says so, so for that moment both ring it.
            report = {**self.alarms.away_report(), "ringing": self.ringer.ringing_ids()}
            if self.link.send("away", **report):
                self._handing_over = report
                self.link.expect("away_settled", SETTLE_WAIT)

    def _take_quiet(self, message: Dict, payload: bytes):
        """
        The brain says Nova has started, or stopped, speaking on another satellite, so
        this one ignores the wake word meanwhile - see QUIET_MOST and QUIET_AFTER. A press
        is deliberate, so it still works.
        """
        now = time.monotonic()
        if message.get("on") is True:
            self._quiet_until = now + QUIET_MOST
        else:
            self._quiet_until = min(self._quiet_until, now + QUIET_AFTER)

    def _take_now_playing(self, message: Dict, payload: bytes):
        """
        The brain says what this satellite's music is doing, for Windows' media controls
        to show: the cover with it only when it's a new one, as the payload, or none now.
        """
        if self.media is None:
            return
        state = message.get("state")
        if state not in ("playing", "paused", "stopped"):
            logger.warning(f"The brain sent a now_playing which isn't playing, paused or stopped: "
                           f"{ascii(state)[:40]}")
            return
        names = [message.get(name) for name in ("track", "artist", "album")]
        names = [name if isinstance(name, str) else "" for name in names]
        if "cover" not in message:
            cover = UNCHANGED
        else:
            cover = payload if message["cover"] is True and payload else None
        self.media.show(state, *names, cover=cover)

    def _media_pressed(self, action: str):
        """A button of Windows' media controls, on a thread of Windows': passed on to the brain to act on."""
        logger.info(f"Media button: {action}")
        link = self.link
        if link is None or not link.send("media", action=action):
            logger.info("The brain can't be reached, so the media button does nothing")

    def _forget_the_brain(self):
        """
        Stops what the brain was playing, which nothing will now come to finish or
        stop - and says the brain can't be reached, if it went with the user waiting
        for its answer.
        """
        # Nothing is left to say the other satellite has stopped speaking.
        self._quiet_until = 0.0
        # Nor to play the music, which stops below with the rest.
        if self.media is not None:
            self.media.show("stopped", "", "", "")
        with self._lock:
            playing, self._playing = list(self._playing.values()), {}
            # Nobody is left to hear a follow-up.
            self._listen_from = None
            # Nor to say it has what it was told on connecting, which it may never have
            # saved - so what rings from the copy rings on, and the report is kept, for
            # the next brain.
            self._handing_over = None
            waiting, self._turn_open = self._turn_open and not self._stopping, False
            if waiting:
                self._save_timeline(outcome="no_brain")
        for each in playing:
            each.playback.stop()
            each.let_go()
        # Nobody is left to end the turn which a press opened the microphone for.
        self._mute_mic_again()
        if waiting:
            # After the brain's own sounds have stopped, so it isn't stopped with them.
            # It lets the alarms ring again too.
            self._say_cant_reach()
            return
        # The brain may have silenced the alarms while it talked to the user, and can't
        # let them ring again now - when the satellite's own copy may need to.
        self.speaker.allow("announcement")

    def _ring_when_due(self):
        """Rings what falls due in the alarm copy, for as long as the brain can't be reached to ring it."""
        while not self._finished.wait(FALLBACK_CHECK):
            try:
                # Under the lock, so nothing starts ringing just as the brain is reached.
                with self._lock:
                    if not self.link.connected:
                        self.ringer.check(time.time())
            except Exception:
                logger.exception("Couldn't check the alarm copy")

    def _note_start(self) -> bool:
        """
        Leaves RUNNING_MARK, which _clean_up takes away.

        Returns:
            Whether the last run left one too, so never stopped properly - which the
            brain is told, and tells the user's phone.
        """
        try:
            unclean = storage.load_json(RUNNING_MARK) is not None
        except (OSError, ValueError):
            # Half written as the power went, most likely - which is a restart too.
            unclean = True
        try:
            storage.save_json(RUNNING_MARK, {"started_at": time.time()})
        except OSError as e:
            logger.warning(f"Couldn't note that the satellite is running: {e}")
        return unclean

    def _clean_up(self):
        """
        Closes everything down in order: the brain first, so nothing more arrives to
        play, then what rings, and only then the speaker and the microphone.
        """
        self._stopping = True
        self._finished.set()
        self._mic_changed.set()
        # So the list threads see it now, not at their next look.
        self._apps_wanted.set()
        self._windows_wanted.set()
        if self.press_listener is not None:
            # First, so no press arrives to start a recording of a satellite shutting down.
            self.press_listener.close()
        if self.link is not None:
            self.link.stop()
        if self.media is not None:
            self.media.close()
        if self.system_volume is not None:
            # Never left open by a press past the satellite's own end.
            self._mute_mic_again()
            self.system_volume.close()
        if self.orb is not None:
            self.orb.close()
        self.ringer.quiet()
        with self._lock:
            self._save_timeline()
        self.speaker.close()
        self.microphone.close()
        # Stopped properly, so the next start isn't taken for a restart after a problem.
        try:
            os.remove(RUNNING_MARK)
        except OSError:
            pass
        logger.info("Shut down cleanly")


if __name__ == "__main__":
    paths.make_folders()
    # The log the README and the soak monitor point to.
    setup_logging(os.path.join(paths.LOGS_DIR, "smart_assistant.log"))
    # The satellite's password, the one secret it keeps - see paths.py.
    paths.load_secrets()
    satellite = Satellite(Config())
    # Ctrl+C and systemd's stop both end the main loop - see Satellite.shutdown.
    signal.signal(signal.SIGINT, satellite.shutdown)
    signal.signal(signal.SIGTERM, satellite.shutdown)
    satellite.run()
