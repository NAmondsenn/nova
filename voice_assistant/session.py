"""
Session Module.

One satellite's Nova, on the brain - see docs/decisions/0006. A session answers each
wake word its satellite hears: it records what the user says from the audio the
satellite sends, hears it with the brain's speech-to-text, has the conversation work
out the answer (see conversation.py), and says it through the satellite's speaker. It
also says what goes off by itself there - the timers and alarms set from it, Home
Assistant's messages and the user's routines.

This is what the Pi ran, when it was the whole of Nova. Now the Pi is a
satellite (see satellite.py), and each satellite connected to the brain has a session
of its own, with its own volumes, mute, music, room in Home Assistant and
conversation. What they all share - the clock, memory, routines, ntfy, the weather,
Piper and Nemotron - is the brain's.
"""

import logging
import math
import os
import queue
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from voice_assistant import paths, storage
from voice_assistant.actions import UNAVAILABLE, ActionExecutor
from voice_assistant.clock_ringing import heard_in
from voice_assistant.computers import MIC_MUTE_MINUTES, ComputerControl, ComputerHooks
from voice_assistant.config import CHOICES
from voice_assistant.conversation import NOT_CAUGHT, Conversation
from voice_assistant.cut_in import CutIn
from voice_assistant.ha_device import DeviceControls, NovaDevice
from voice_assistant.home_assistant import HomeAssistant
from voice_assistant.intents import LocalIntents
from voice_assistant.llm import LLMHandler
from voice_assistant.long_recordings import note_long_recording
from voice_assistant.media_controls import EVERY as MEDIA_EVERY
from voice_assistant.media_controls import NowPlayingFeed
from voice_assistant.logging_setup import transcripts_logged
from voice_assistant.music import MusicHold, make_player
from voice_assistant.network import online
from voice_assistant.notes import MOST_TEXT, TOO_LONG
from voice_assistant.recording import FRAME, RATE, AudioFeed, record_until_silence
from voice_assistant.remote import RemoteSatellite
from voice_assistant.settings_page import change_volume, volume_levels
from voice_assistant.spotify_controller import brains_speaker
from voice_assistant.timeline import Timeline
from voice_assistant.vad import make_detector
from voice_assistant.voice_check import DEFAULT_THRESHOLD as DEFAULT_VOICE_CHECK
from voice_assistant.volume import VolumeControl
from voice_assistant.wake_phrase import DEFAULT_WAKE_WORD, strip_wake_word

logger = logging.getLogger(__name__)

# What the PC's orb is shown as if config.yaml has been left without its settings, the same
# as config.yaml starts them.
DEFAULT_SCREEN_ORB = {"on": True, "place": "bottom centre", "size": "medium"}

# What a recording at a computer ends on if config.yaml has been left without these two
# settings, the same as config.yaml starts them.
DEFAULT_PRESS_VOICE_DROP_DB = 18
DEFAULT_PRESS_NO_WORDS_SECONDS = 1.5

# How a turn of an open conversation ends if config.yaml has been left without these two
# settings, the same as config.yaml starts them: after this many seconds of quiet, or once it
# has gone on this many.
DEFAULT_PAUSE_SECONDS = 1.5
DEFAULT_LONGEST_TURN = 90

# How a voice over a reply in a conversation is taken if config.yaml has been left without these
# two settings, the same as config.yaml starts them: it may cut in only at a computer, and dips
# the reply to this much of its volume while it's heard.
DEFAULT_TALK_OVER = "voice_at_computers"
DEFAULT_DIP_LEVEL = 0.3

# How long a reply takes to dip when a voice is heard over it, and to come back, in seconds - a
# fade so quick it's heard as at once, rather than a jump, which would click.
DIP_FADE = 0.05

# How much from before a voice heard over a reply goes to speech-to-text with it, and starts the
# user's turn if it cuts in, in seconds - as the detector only takes it for a voice a moment after
# it began, and the first sound of their first word would otherwise be lost.
CUT_IN_PRE_ROLL = 0.5

# What's said as an open conversation ends without the user asking - its quiet spell, turns Claude
# took for speech from what's playing, or a turn which went wrong - so the open microphone never
# closes silently. One the user asked to end has its "Okay." said by what ended it (see actions.py);
# one ended from Home Assistant's switch or by a mute was the user's own doing, and has only the
# falling sound.
CONVERSATION_LEFT = "I'll leave it there."
UNASKED_ENDS = ("idle", "not for nova", "failed")
# What Home Assistant's switch has the session take as typed, so a conversation is started by
# the one piece of code which starts one for the phrase - with its refusals, its words and its
# sound - rather than a second copy of them.
START_PHRASE = "let's talk"
# What the switch's request is kept as in the typed queue, rather than as its words: nothing the user
# types can be it, so what's taken is known to be the switch's and can be judged when it is taken -
# see _switch_start_taken.
SWITCH_START = object()

# How long a conversation's wait for the user's next turn must have heard no speech before it's a gap,
# in which what goes off by itself is said, in seconds - so not as the user is just starting, and not
# straight after a reply, where they may be about to answer it.
GAP_QUIET = 1.0

# How long after an announcement's words a conversation's wait starts listening again, in seconds -
# for what's left of them reaching the satellite's microphone late, as the echo canceller lets some
# through. As long as a turn keeps from before the user starts (recording.OPEN_PRE_ROLL), so none of
# them goes to speech-to-text with what the user says next.
ANNOUNCED_TAIL = 0.5

# How often an announcement waiting for a gap in a conversation, and a session waiting for one said in
# a gap, look again, in seconds - for the conversation ending, and for the watchdog.
GAP_CHECK = 0.1

# How loud music asked for during a conversation plays until it ends, as a share of the music volume -
# so Nova is still heard over it, without its being paused for every reply.
MUSIC_SHARE = 0.3

# How long a session waits for a wake word before checking its satellite is still
# there, in seconds - the satellite's wakes never say it has gone.
WAKE_CHECK = 0.5

# How often a reply checks for a wake word said over it, in seconds - often, since the
# user is waiting for it to stop.
TALKED_OVER_CHECK = 0.05

# How much longer than the audio sent for it a sound may go on before the satellite is
# taken never to say it has finished, in seconds - when it's stopped, rather than the
# session waiting for it for good. None of Nova's own sounds is longer than the alarm's
# 34 seconds, and Piper gives up on a sentence after 10 (see text_to_speech.py), so
# one which goes on a minute past the last of its audio has been lost track of.
PLAYBACK_SLACK = 60.0

# How often waiting for a sound checks whether it's been lost track of, in seconds.
PLAYBACK_CHECK = 0.1

# How often Home Assistant is asked which device is this satellite's, and which room
# it's in, in seconds: every half a minute until it's found - it isn't until Home
# Assistant has taken in the device's MQTT announcement - then every five minutes,
# since the user can move it to another room whenever they like.
DEVICE_LOOKUP = 30
DEVICE_LOOKUP_FOUND = 300

# What the settings page's "Hear it" says, at the speed on its slider. Always this,
# so the page can never have Nova say anything else.
HEAR_IT = "This is how fast I'll talk."

# How long Nova waits for a typed answer when its reply to something typed in Home
# Assistant asks a question, in seconds - the microphone stays shut, so it's the only
# way to answer, and typing one takes longer than saying it. See docs/decisions/0027.
TYPED_ANSWER_WAIT = 60

# How many characters of a typed request are kept - as many as Home Assistant's Ask
# Nova box takes, so only something else sending to its topic is ever cut short.
ASK_MOST = 255

# The most characters a note may have to be put on the notepad list, as one thing, when its
# computer is off. A longer one is said not to fit, not cut short.
LIST_ITEM_MOST = 255


# The longest timer the Nova view's Add timer box sets, in seconds.
LONGEST_TIMER = 24 * 3600

_TIMER_UNITS = {"h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
                "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
                "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1}


def timer_seconds(text: str) -> Optional[int]:
    """
    A length of time typed or tapped into the Nova view's Add timer box, in seconds - or
    None if it isn't one, or is nothing or more than LONGEST_TIMER.

    A bare number is minutes ("10"); hours and minutes can be "1:20"; otherwise numbers with
    units, where a number left over after hours is minutes ("1h 20", "1 hour 20 minutes", "90s").
    """
    text = (text or "").strip().lower()
    if re.fullmatch(r"\d+", text):
        seconds = int(text) * 60
    elif re.fullmatch(r"\d{1,2}:[0-5]\d", text):
        hours, minutes = text.split(":")
        seconds = int(hours) * 3600 + int(minutes) * 60
    else:
        parts = re.findall(r"(\d+)\s*([a-z]*)", text)
        if not parts or re.sub(r"[\d\s]|" + "|".join(sorted(_TIMER_UNITS, key=len, reverse=True)) + r"|and|,", "", text):
            return None
        seconds, last = 0, None
        for number, unit in parts:
            if unit and unit not in _TIMER_UNITS:
                return None
            # A number with no unit after hours is minutes ("1h 20"); on its own it was handled above.
            size = _TIMER_UNITS[unit] if unit else (60 if last == 3600 else None)
            if size is None:
                return None
            seconds += int(number) * size
            last = size
    return seconds if 0 < seconds <= LONGEST_TIMER else None


def _kept_for(what: str, satellite: str) -> str:
    """
    Where one of a satellite's own settings is kept in the data folder, e.g.
    "volume-bedroom.json". Anything in its name which couldn't be in a file's name is
    replaced, so a name can't lead anywhere else.
    """
    return os.path.join(paths.DATA_DIR, f"{what}-{re.sub(r'[^A-Za-z0-9_.-]', '_', satellite)}.json")


def _number(value, default: float) -> float:
    """
    A setting as a number, or its default when it isn't one - left empty in config.yaml
    by hand, say, which the settings page would never save.

    Args:
        value: The setting as read.
        default: What to use in its place.

    Returns:
        The value if it's a finite int or float (not a bool), else the default.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return value


def _orb_choice(computer: Dict, name: str) -> str:
    """
    Where the PC's orb sits, or how big it is, as one of the settings page's own words.

    Args:
        computer: The computer section of the settings, as it is now.
        name: "place" or "size", for screen_orb_place or screen_orb_size.

    Returns:
        The setting in lower case, without spaces round it, if that is one of its choices -
        else its default, for one left empty or anything config.yaml would have refused.
    """
    value = str(computer.get(f"screen_orb_{name}")).strip().lower()
    return value if value in CHOICES[("computer", f"screen_orb_{name}")] else DEFAULT_SCREEN_ORB[name]


class _Announced(Exception):
    """
    Raised in a conversation's wait for the user's next turn as an announcement's words begin in its gap, so
    the wait stops listening until they're over - see Session._waited.
    """


class _Moving:
    """
    A speech detector which notes the session getting on with each frame it decides on - for a
    turn of an open conversation, which can wait as long as its quiet spell for the user to start
    (conversation.idle_minutes, up to 30) and then last conversation.longest_turn: far past the
    watchdog's allowance for a session which is still (see Brain.last_moved), though it's moving
    for as long as the satellite's audio keeps coming. It also says, with each frame, for how long
    it has heard no speech, which is how the session knows a gap in the conversation.
    """

    def __init__(self, detector, moved: Callable[[], None], waited: Callable[[float], None]):
        """
        Args:
            detector: The session's own detector, which does the deciding - see vad.py.
            moved: Called with nothing for each frame - Session._moved.
            waited: Called, once each frame is decided, with how many seconds of audio in a row up
                to it have been no speech - Session._waited, which may raise _Announced instead of
                the frame being given back.
        """
        self._detector = detector
        self._moved = moved
        self._waited = waited
        # The frames in a row decided not to be speech, since the recording began.
        self._quiet = 0

    @property
    def threshold(self) -> float:
        return self._detector.threshold

    def reset(self):
        self._detector.reset()
        self._quiet = 0

    def speech_probability(self, frame) -> float:
        self._moved()
        probability = self._detector.speech_probability(frame)
        self._quiet = 0 if probability >= self._detector.threshold else self._quiet + 1
        self._waited(self._quiet * FRAME / RATE)
        return probability


class Session:
    """One satellite's Nova, for as long as the satellite stays connected."""

    def __init__(self, brain, satellite: RemoteSatellite):
        """
        Args:
            brain: What every session shares - its config, clock, memory, routines,
                ntfy, weather, tts (Piper), stt (a NemotronSpeechToText) and
                sounds_folder.
            satellite: The satellite, just connected.
        """
        self.brain = brain
        self.satellite = satellite
        self.config = config = brain.config

        assistant_config = config.section("assistant")
        self.assistant_name = assistant_config.get("name", "Assistant")
        # What wakes the satellite, which the recording sometimes catches the end of -
        # see strip_wake_word.
        self.wake_word = assistant_config.get("wake_word", DEFAULT_WAKE_WORD)

        # How long each stage of every interaction took, one line per interaction,
        # for scripts/latency_report.py to summarise.
        self.latency_file = os.path.join(paths.LOGS_DIR, "latency.jsonl")

        # The newest push-to-talk recordings whose speech ran long, for seeing whether the
        # ways of ending a recording work - see long_recordings.py.
        self.long_recordings_file = os.path.join(paths.LOGS_DIR, "long-recordings.jsonl")

        # Set while the assistant is idle, cleared while it's dealing with the user.
        # Timer announcements wait on this, so they don't talk over a conversation.
        self._idle = threading.Event()
        self._idle.set()
        # Held while something which went off is being announced. The clock rings
        # each timer on a thread of its own, so two going off together would
        # otherwise talk over each other.
        self._announcing = threading.Lock()
        # Held while the conversation works out a reply - to the user, or for a routine
        # running by itself, which mustn't do so at the same time. See run_routine.
        self._conversation_lock = threading.Lock()
        # How many times Stop has been pressed here - Home Assistant's or the settings
        # page's - so an alert or a battery warning can tell it was stopped during its
        # sound. See _announce_in_turn.
        self._stops = 0

        # What the assistant is doing - "idle", "listening", "thinking" or "speaking" -
        # for the settings page's star and the clock displays. Only ever read for show.
        self._activity = "idle"
        # What it last heard the user say in this conversation - "What time is it?" -
        # for a clock display to show. Empty from the moment it starts listening again.
        self._heard = ""
        # Told of each change to either, for a clock display waiting on one - see watch.
        self._watchers: List[Callable[[], None]] = []
        self._watchers_lock = threading.Lock()
        # The feed a finger held on a clock display opened, so lifting it ends that
        # recording - see listen_now. Forgotten once let go, or once its conversation is over.
        self._held: Optional[AudioFeed] = None
        self._held_lock = threading.Lock()
        # While a turn answers something typed in Home Assistant, rather than said - see _here.
        self._typing = False
        # The sessions of the user's room, this one first, whose music a conversation
        # here holds - just this one outside a conversation. See _converse.
        self._room_held: List["Session"] = [self]
        # What's been typed in Home Assistant's Ask Nova box, waiting its turn - a queue of
        # its own, as one put with the satellite's wakes would be taken for a wake word said
        # over the reply before it, and stop it. Each is kept with when it arrived, by
        # time.monotonic(), so it can be told whether it answers a typed question. See ask.
        self._typed: "queue.Queue[Tuple[Any, float]]" = queue.Queue()
        # Whether Home Assistant's Conversation switch has a start waiting in the queue, under
        # _typed_lock: at most one at a time, and turning the switch off cancels it. See
        # _conversation_from_home_assistant.
        self._switch_start_pending = False
        # While a turn waits for the answer to a question it's just asked - from the moment
        # it knows it's asking, until the answer is heard or given up on - the first thing
        # typed is that answer rather than a fresh request, with the spoken listen for it,
        # if there is one, to end. All three are kept under the one lock, so the answer
        # can't arrive between the listen starting and being noted. See _open_answer.
        self._typed_lock = threading.Lock()
        self._answer_open = False
        self._typed_answer: Optional[str] = None
        self._answer_feed: Optional[AudioFeed] = None
        # A question asked in reply to something typed, whose answer can still be typed,
        # once its turn has ended - as (since, until), by time.monotonic(): what's typed
        # between the two is its answer. None without one. Under the same lock. See
        # _await_typed_answer.
        self._typed_question: Optional[Tuple[float, float]] = None
        # Whether the satellite has been told an open conversation is on here, and so is to be
        # told when it ends - only ever read and changed on the session's own thread, which
        # tells it both. See _conversation_begun and _conversation_goes_on.
        self._conversing = False
        # Whether the last reply asked something, so the next turn of a conversation is its answer.
        self._asked = False
        # The listen of a conversation's turn, while it waits for the user or hears them, and why
        # the conversation was ended by end_conversation, for the session's own thread to say so.
        # Both under the one lock, with ending it, so an end can't fall between a turn's listen
        # starting and being noted, and leave it waiting out the quiet spell. See end_conversation.
        self._open_lock = threading.Lock()
        self._open_feed: Optional[AudioFeed] = None
        self._ended_by: Optional[str] = None
        # Where the next turn starts in _open_feed, when it's the listen a reply was heard over and
        # left open for that turn - see _cut_in_over. None otherwise. Under the same lock.
        self._open_from: Optional[int] = None
        # Hears a voice over a reply in a conversation - one of its own, as CutIn resets it and gives
        # it what's heard over the reply, not a recording. Made the first time it's needed, as a
        # satellite with no conversation never needs it. See _cut_in_over.
        self._cut_in_detector = None
        # The gaps in an open conversation here, in which what goes off by itself is said rather than
        # waiting for it to end - see _take_gap. Kept under the one condition: whether the session's
        # own thread is waiting for the user's next turn and has heard no speech for GAP_QUIET, which only it
        # changes (see _waited); and whether an announcement has taken the gap, and is saying words,
        # which only the announcing thread changes. The session's thread says nothing while one has
        # (see _after_announcement), and listens to none of its words.
        self._gap = threading.Condition()
        self._in_gap = False
        self._gap_taken = False
        self._gap_words = False
        # The thread which took it, so a routine run in it can play its own sounds there.
        self._gap_taker: Optional[int] = None
        # Whether the user has started their turn in the wait the session's thread is in, after which it's
        # no gap until the turn is over, however long they pause. Only ever read and changed on that thread.
        self._turn_begun = False
        # Whether that wait has been a gap at all, in which what came due will have rung, and what was
        # ringing as the user then began their turn - for the turn to silence, as what they heard. See
        # _begun. Both only ever read and changed on that thread.
        self._gap_had = False
        self._ringing_begun: Optional[Dict] = None
        # The sessions of the room whose music the conversation here has let go of, for music asked for
        # in it to play before it ends, each with the player it let play - see _let_music_play. Only on
        # the session's own thread.
        self._let_play: Dict["Session", object] = {}
        # The last volume beep the settings page played, stopped as the next starts,
        # so dragging a slider doesn't pile them up - see boop.
        self._last_boop = None
        self._boop_lock = threading.Lock()
        # The last "Hear it" the settings page played, so another isn't said over it -
        # see hear_voice.
        self._last_hearing = None
        self._hearing_lock = threading.Lock()
        # Held while the satellite's state is read and sent, as the media watcher, the
        # settings page, the device lookup and satellites joining each send it from a thread
        # of their own - so the later of two sends always reads later and arrives last. One
        # read just before a film started, landing after the one read just after, would
        # leave the usual wake word on through the film. See send_state.
        self._sending_state = threading.Lock()
        # Whether anything played in the satellite's room as its state was last sent - see send_state.
        self._something_played = False
        # Set once the session is closed, which ends run() - see close.
        self._closed = threading.Event()
        self._closing = threading.Lock()
        # When the session's own thread was last seen getting on, by time.monotonic():
        # between wake words, and as a reply plays, however long it is - and as each of
        # Claude's replies and each step of a routine comes in, on whichever thread runs
        # it (see Conversation's progress), so a routine of slow steps doesn't look
        # stuck, nor a wake word waiting on a routine run by itself. The brain stops
        # telling systemd it's working once a session has been still too long - stuck
        # part way through a turn, with its satellite still connected and pinging, so
        # that nothing else would notice. See Brain.last_moved.
        self.moved_at = time.monotonic()

        logger.info(f"Starting the {satellite.name} satellite's session...")

        # Everything played goes to the satellite's speaker - see remote.py.
        self.speaker = satellite.speaker
        # Decides which parts of a recording are speech - see vad.py. One for each
        # session, since it carries what it last heard from one frame to the next.
        self.speech_detector = make_detector(config)
        # Seconds from before the wake word was detected which the satellite sends
        # first, for a satellite which doesn't say how many with each wake - see
        # _handle_interaction.
        self.pre_roll = config.section("audio").get("pre_roll", 0.3)

        # Whether the satellite's microphone is muted - from Home Assistant or the
        # settings page - kept so it stays muted however often either end restarts.
        self._muted_file = _kept_for("microphone", satellite.name)
        self._muted = self._load_muted()

        # The satellite's own volumes, saved so they survive a restart, and sent to it
        # whenever one changes, for it to ring by while it can't reach the brain.
        self.volume = VolumeControl(config=config, path=_kept_for("volume", satellite.name),
                                    on_change=self.send_state)

        # Navidrome or Spotify, whichever music.player says - or None, and the
        # assistant carries on without music. See music.py. Spotify plays on the
        # satellite's own speaker, if it said one as it connected.
        self.music_player = make_player(config, self.speaker, self.volume, spotify_speaker=satellite.speaker_name)

        # Keeps the music paused while there's something to say - see MusicHold.
        self.music = MusicHold(self.music_player, online=online)

        # Home Assistant, for the lights - None until it's set up. One for each session,
        # since it keeps the room its satellite is in.
        self.home = HomeAssistant.from_config(config, os.getenv("HA_TOKEN"), registry=brain.registry)

        # Everything the assistant can do for the user - see actions.py. The lights are
        # only there once Home Assistant is set up, and the calendar once Google is, and
        # timers and alarms ring on the satellite they were set from, as a download is
        # said there - or on its room's always-on one, if it's a push-to-talk satellite. Any
        # satellite can open things on a computer, the one it's on or another.
        self.actions = ActionExecutor(music=self.music_player, clock=brain.clock, volume=self.volume,
                                      weather=brain.weather, home=self.home, memory=brain.memory,
                                      ntfy=brain.ntfy, satellite=satellite.name, batteries=brain.batteries,
                                      lists=brain.lists, pc=brain.pc, calendar=brain.calendar,
                                      downloads=brain.downloads, football=brain.football,
                                      listening=brain.listening, wrapped=brain.wrapped_store, config=config,
                                      new_music=brain.new_music_playlist, journeys=brain.journeys,
                                      playlist_changes=brain.playlist_changes, ring_on=brain.ring_on,
                                      room_of=brain.room_of, room_players=self._room_players,
                                      last_change=brain.last_change,
                                      mover=lambda said: brain.move_music(self.satellite.name, said),
                                      elsewhere=self._elsewhere,
                                      spoken_name=lambda name: brain.spoken_name(name),
                                      approvals=brain.approvals,
                                      settled=lambda: self.music.wait(),
                                      notes=self._save_note, notes_offered=lambda: bool(brain.notes_saved_on()),
                                      computer=ComputerControl(brain, satellite.name), device=self._device,
                                      at_pc=lambda: brain.rooms.machine(satellite.name) == "pc",
                                      pc_name=lambda: brain.pc_called(satellite.name),
                                      microphone=self._mute_microphone, rang=brain.find_my_rang,
                                      conversation=brain.open_conversation,
                                      # A computer unmutes Windows' microphone for a conversation itself, as
                                      # for a press, so only a wake-word satellite's mute keeps one from starting.
                                      muted=lambda: self.muted and not self.satellite.push_to_talk,
                                      # One not updated since the brain was would have its turns as ordinary
                                      # listens, so none starts there - nor at one whose session has been let
                                      # go of, still answering a turn, where nothing would hear or end it.
                                      converses=lambda: self.satellite.can_converse and not self._closed.is_set(),
                                      # How an approval asked for here ended, for Claude with the next turn.
                                      noted=lambda text: self.llm.note(text))

        # The everyday commands handled without Claude - see intents.py. The activities are
        # the calendars' names, so "when's gym tomorrow?" is heard once there's a Gym calendar.
        # The teams followed are given by every name they have, as they are left to Claude, not Assist.
        # Opening, closing and searching are only a computer's when the brain finds what's said there.
        # Find My's devices are heard by their names in Home Assistant now, and a bare "found it" only
        # once something was rung through it, anywhere in the house.
        self.intents = LocalIntents(clock=brain.clock, ntfy=brain.ntfy, home=self.home,
                                    activities=brain.calendar.activity_words if brain.calendar else None,
                                    football=brain.football.known_as if brain.football else None,
                                    wake_word=self.wake_word, names_satellite=brain.names_satellite,
                                    computer=ComputerHooks.for_satellite(brain, satellite.name),
                                    find_my_names=self.home.find_my_names if self.home else None,
                                    rang_lately=lambda: bool(brain.find_my_rang.lately()),
                                    conversation=brain.open_conversation, satellite=satellite.name)

        # The satellite as a device of its own in Home Assistant, over MQTT, or None
        # until mqtt.host is set - see ha_device.py. It's named after the satellite's
        # own machine, so the Pi keeps the device, and the room, it had. Which device it
        # is there is looked up while the session runs - see _look_up_device.
        self.device = NovaDevice.from_config(config, os.getenv("MQTT_PASSWORD"), self._device_controls(),
                                             hostname=satellite.hostname)
        self._stop_looking_up = threading.Event()

        # What the satellite's own music is doing, sent to it as it changes, for Windows'
        # media controls to show - only to a satellite which said it has them, and only
        # with music. Their buttons act on this satellite's music alone. See
        # _keep_media_controls and docs/decisions/0031.
        self._now_playing = (NowPlayingFeed(satellite.send_now_playing)
                             if satellite.media_controls and self.music_player else None)

        # Claude, offered whichever of those tools can actually work here, and told
        # what's known about the user. Each of its responses is recorded in the brain's usage log.
        self.llm = LLMHandler(config=config, tools=self.actions.tool_schemas(),
                              read_tools=self.actions.read_tools(), memory=brain.memory,
                              usage=brain.usage, open_conversation=brain.open_conversation,
                              satellite=satellite.name, say_first=self._say_first)

        # How each thing the user says is answered - everything between hearing it and
        # saying the reply. See conversation.py.
        self.conversation = Conversation(brain.clock, self.intents, self.llm, self.actions, self.music,
                                         chime_enabled=lambda: self.chime_enabled,
                                         max_follow_ups=self.thresholds.get("max_follow_ups", 3),
                                         routines=brain.routines, satellite=satellite.name,
                                         playing=self.playing_here, progress=self._moved,
                                         room=lambda: brain.room_of(satellite.name), here=self._here,
                                         conversing=self._conversation_here, say_first=self._say_first)

    @property
    def thresholds(self) -> Dict:
        """
        config.yaml's thresholds as they are now - read at each recording, so a silence
        changed on the settings page is used from the next, on a session already started.
        """
        return self.brain.config.section("thresholds")

    @property
    def chime_enabled(self) -> bool:
        """Whether a success is confirmed with the chime, as the settings are now."""
        return self.brain.config.section("sounds").get("chime_enabled", True)

    @property
    def reminder_sound_enabled(self) -> bool:
        """Whether a reminder is announced with its sound, as the settings are now."""
        return self.brain.config.section("sounds").get("reminder_enabled", True)

    @property
    def listening_sound_enabled(self) -> bool:
        """Whether the blip says Nova's listening, as the settings are now."""
        return self.brain.config.section("sounds").get("listening_enabled", True)

    # The satellite's mute and volumes

    @property
    def muted(self) -> bool:
        """Whether the satellite's microphone is muted, so it doesn't hear the wake word."""
        return self._muted

    def set_muted(self, muted: bool):
        """
        Mutes or unmutes the satellite's microphone, straight away - from Home
        Assistant or the settings page. Alarms and timers still ring, and Home
        Assistant's messages are still said. A mute ends a conversation going on here.

        Args:
            muted: True to stop the satellite hearing the wake word.
        """
        changed = bool(muted) != self._muted
        self._muted = bool(muted)
        if changed:
            # So a clock display's muted mark comes and goes at once - see watch.
            self._tell_watchers()
        try:
            storage.save_json(self._muted_file, {"muted": self._muted})
        except OSError as e:
            # It's still muted or unmuted until the brain restarts, just not after.
            logger.error(f"Couldn't save whether the {self.satellite.name} satellite is muted: {e}")
        self.send_state()
        if self._muted:
            # Turning the listening off ends a conversation here, with the falling sound and nothing
            # said, as it was the user's doing - rather than its open microphone hearing nothing until its
            # quiet spell is up.
            self.end_conversation("muted")

    def _load_muted(self) -> bool:
        """Whether the satellite was left muted - not, if that can't be told."""
        try:
            saved = storage.load_json(self._muted_file, default={})
        except Exception as e:
            logger.error(f"Couldn't read whether the {self.satellite.name} satellite is muted, so it isn't: {e}")
            return False
        return isinstance(saved, dict) and saved.get("muted") is True

    def send_state(self) -> bool:
        """
        Sends the satellite its volumes and its mute, which the brain owns: whenever
        either changes, and as it connects. It mutes its wake word by them, and rings
        by them while it can't reach the brain. With them go the longest request and
        how sure to be of its wake word and of the user's voice, which aren't the
        satellite's to keep but are sent here so a save on the settings page can reach
        it in the same way - see Brain.apply_settings - as can something starting or
        stopping in its room. A push-to-talk satellite is also sent how to show Nova's
        orb on its screen, the only kind which has one, and what its app needs: its
        shortcut, this page's address and how long its microphone stays muted.

        Returns:
            Whether it was sent.
        """
        # Never called with the brain's lock held, which playing_here takes while this is held.
        with self._sending_state:
            volumes = {category: self.volume.get(category) for category in VolumeControl.CATEGORIES}
            playing = self.playing_here()
            wake_confidence = self._wake_confidence(playing)
            # Its default when it's been left empty in config.yaml by hand, as the wake setting falls back to its own.
            voice_check = _number(self.thresholds.get("voice_check"), DEFAULT_VOICE_CHECK)
            # Said each time it starts and stops, so a satellite that was hard to wake can be
            # traced to what Home Assistant said was playing - a browser tab left on, say.
            if bool(playing) != self._something_played:
                self._something_played = bool(playing)
                if playing:
                    logger.info(f"Something's playing in the {self.satellite.name} satellite's room "
                                f"({', '.join(playing)}), so its wake word needs {wake_confidence:g}")
                else:
                    logger.info(f"Nothing's playing in the {self.satellite.name} satellite's room now, "
                                f"so its wake word needs {wake_confidence:g}")
            return self.satellite.set_state(volumes, self._muted, self.thresholds.get("recording_timeout", 15.0),
                                            wake_confidence=wake_confidence, voice_check=voice_check,
                                            screen_orb=self._screen_orb(), app=self._app())

    def _screen_orb(self) -> Optional[Dict]:
        """
        Returns:
            How the satellite should show Nova's orb on its screen - {"on", "place",
            "size"}, as the settings are now so a save reaches it with no restart - or
            None for a satellite which listens for a wake word, which has no screen
            to show it on.
        """
        if not self.satellite.push_to_talk:
            return None
        computer = self.brain.config.section("computer")
        # Sent as the satellite takes them, which is only as the settings page writes them.
        # config.yaml takes a choice in any case, so "Top Left" written by hand is sent as
        # "top left" - the satellite would refuse the whole setting for it, its "on" with
        # it, and a "screen_orb: false" beside it would go unheeded. An "on" left empty is
        # no answer, so it's the default rather than off.
        on = computer.get("screen_orb")
        return {"on": on if isinstance(on, bool) else DEFAULT_SCREEN_ORB["on"],
                "place": _orb_choice(computer, "place"), "size": _orb_choice(computer, "size")}

    def _app(self) -> Optional[Dict]:
        """
        Returns:
            What the satellite's app needs - {"shortcut", "page", "mic_mute_minutes"} - as the
            settings are now so a save reaches it with no restart, or None for a satellite
            which listens for a wake word, which has no app. The shortcut is as shortcut.written
            gives it, "" for none, and the page is "" while no address is set.
        """
        if not self.satellite.push_to_talk:
            return None
        page = self.brain.config.section("wrapped").get("link")
        minutes = self.brain.config.section("computer").get("mic_mute_minutes")
        # config.yaml takes a whole number from 1 up, but one written by hand or left empty is the default.
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1:
            minutes = MIC_MUTE_MINUTES
        return {"shortcut": self.brain.rooms.shortcut(self.satellite.name),
                "page": page.strip() if isinstance(page, str) else "", "mic_mute_minutes": minutes}

    @property
    def shortcut_taken(self) -> bool:
        """Whether the satellite said another program has its shortcut, on this connection."""
        return self.satellite.shortcut_taken

    def _wake_confidence(self, playing: List[str]) -> float:
        """
        Args:
            playing: What's playing in the satellite's room - see playing_here.

        Returns:
            How sure, from 0 to 1, the satellite must be that it heard its wake word: the
            usual setting, or the media one while anything plays in its room - but never
            less sure then, so a media setting saved below the usual one changes nothing.
        """
        thresholds = self.thresholds
        usual = _number(thresholds.get("wake_word_confidence"), 0.5)
        if not playing:
            return usual
        return max(usual, _number(thresholds.get("media_wake_word_confidence"), 0.75))

    def _here(self) -> Optional[str]:
        """
        Which satellite the user is speaking through, for Claude: its spoken name and how
        it listens, and the others in its room, which may hear too - so "what device are
        you?" has an answer, and "here" means this one.

        Returns:
            e.g. "You're speaking through Nathaniel's PC (push-to-talk). Also in this room:
            the bedroom Pi." - or, for something typed in Home Assistant, that it was typed
            and where the reply is said. None if it can't be worked out.
        """
        try:
            name = self.satellite.name
            kind = "push-to-talk" if self.brain.rooms.push_to_talk(name) else "wake word"
            device = f"{self.brain.spoken_name(name)} ({kind})"
            if self._typing:
                here = f"The user typed this in Home Assistant's Ask Nova box, and the reply is said through {device}."
            else:
                here = f"You're speaking through {device}."
            others = [self.brain.spoken_name(other) for other in self.brain.room_of(name) if other != name]
            if others:
                here += f" Also in this room: {', '.join(others)}."
            return here
        except Exception as e:
            # Only the sentence is lost: the request itself goes on.
            logger.error(f"Couldn't say which device the user is speaking through: {e}")
            return None

    def playing_here(self) -> List[str]:
        """
        What's playing in this satellite's room, as the brain's media watcher last read it -
        see media_watch.py.

        Returns:
            The players' names, in order, e.g. ["Bedroom TV"]. Nova's own music - on any
            satellite's device, or Spotify on any satellite's speaker - isn't counted. Empty
            with no watcher, no Home Assistant, or while this satellite's room isn't known.
        """
        if self.brain.media is None or self.home is None:
            return []
        # This one too, in case it asks while the brain is still letting it in.
        sessions = {*self.brain.connected_sessions(), self}
        devices = [session.home.device_id for session in sessions if session.home]
        # One which doesn't say plays on the brain's own - see SpotifyController.
        speakers = [session.satellite.speaker_name or brains_speaker(self.config) for session in sessions]
        return self.brain.media.playing_in(self.home.area, devices, speakers)

    # For a clock display - see docs/decisions/0013

    @property
    def activity(self) -> str:
        """What the assistant is doing: "idle", "listening", "thinking" or "speaking"."""
        return self._activity

    @activity.setter
    def activity(self, activity: str):
        # Only a real change is told, as every request sets "listening" twice - once as
        # the conversation starts and again as its recording does. Told on this thread,
        # perhaps with the announcement lock held - see watch.
        if activity != self._activity:
            self._activity = activity
            self._tell_watchers()

    @property
    def heard(self) -> str:
        """What the assistant last heard the user say in this conversation, or "" while it listens."""
        return self._heard

    @heard.setter
    def heard(self, heard: str):
        if heard != self._heard:
            self._heard = heard
            self._tell_watchers()

    def watch(self, watcher: Callable[[], None]):
        """
        Has a watcher told each time activity, heard or muted changes. It's called from the
        thread which made the change, sometimes with the announcement lock held - an
        announcement or a routine says "speaking" as it starts - so a watcher must only
        take note, as a clock display's long poll does: never block, and never call
        announce, run_routine or listen_now.

        Args:
            watcher: Called with nothing. One which raises is logged, and the rest are
                still told.
        """
        with self._watchers_lock:
            self._watchers.append(watcher)

    def unwatch(self, watcher: Callable[[], None]):
        """
        Stops telling a watcher. Nothing happens for one which wasn't watching.

        Args:
            watcher: As given to watch.
        """
        with self._watchers_lock:
            if watcher in self._watchers:
                self._watchers.remove(watcher)

    def _tell_watchers(self):
        # Called on a copy, outside the lock, so a watcher which unwatches - or watches
        # again - as it's told can't deadlock or change the list being gone through.
        with self._watchers_lock:
            watchers = list(self._watchers)
        for watcher in watchers:
            try:
                watcher()
            except Exception as e:
                # A display gone wrong mustn't stop the conversation it was watching.
                logger.warning(f"A watcher of the {self.satellite.name} satellite failed: {e}", exc_info=True)

    def listen_now(self) -> bool:
        """
        Starts a conversation as if the wake word had been heard - for a finger held on a
        clock display. Muted or not makes no difference: muting stops the wake word, not
        a hold made on purpose.

        Returns:
            True once the satellite has been asked to listen. False while a conversation
            is going, a hold is already open, or the satellite has gone.
        """
        with self._held_lock:
            if (self._held is not None or not self._idle.is_set() or self._closed.is_set()
                    or not self.satellite.connected):
                return False
            # Nothing from before, as there's no wake word at the start of it to leave out.
            self._held = self.satellite.listen()
            self.satellite.wakes.put({"talked_over": False, "pre_roll": 0, "feed": self._held})
        return True

    def stop_held(self):
        """
        Ends the recording a hold started, as the finger is lifted - what was said so far
        is what's answered. Nothing happens without one, or once its recording has ended
        by itself, at the end of the user's speech.
        """
        with self._held_lock:
            feed, self._held = self._held, None
        if feed is not None:
            self.satellite.stop_listening(feed)

    # Asked by typing - see docs/decisions/0027

    def ask(self, text: str) -> None:
        """
        Takes something typed in Home Assistant's Ask Nova box, as if it had been said
        and heard: it goes through the conversation as speech-to-text's words do, and
        the reply is said out loud on the satellite, never sent back. Muted or not makes
        no difference, as typing uses no microphone.

        While a turn waits for the answer to a question it's asked - or, in an open
        conversation, waits for its next turn - it's that answer, or that turn: ending the
        listen for it, if there is one, so what was typed is used rather than whatever the
        microphone caught. Otherwise it waits its turn, as Speak does, once
        any conversation going on has finished: as the answer to a typed question still
        open (see _await_typed_answer), or else as a fresh request.

        Called on the device's thread for Home Assistant's commands, which has already
        logged what was typed as heard text is logged. So this returns at once, and never
        raises: that thread logs what goes wrong with its message, which here could hold
        what was typed, and nothing else is ever logged of it.

        Args:
            text: What was typed. Cut to ASK_MOST characters, and dropped if blank.
        """
        try:
            text = str(text).strip()[:ASK_MOST].strip()
            if not text or self._closed.is_set():
                return
            with self._typed_lock:
                if not self._answer_open or self._typed_answer is not None:
                    # Anything typed after the answer waits its turn again.
                    self._typed.put((text, time.monotonic()))
                    return
                self._typed_answer = text
                feed = self._answer_feed
            if feed is not None:
                # What the microphone has caught of an answer is dropped for this.
                self.satellite.stop_listening(feed)
        except Exception as e:
            logger.error(f"Couldn't take a request typed in Home Assistant ({type(e).__name__})")

    def _open_answer(self):
        """
        Notes that Nova has asked the user something, so what's typed from now on is the
        answer - even while the question is still being said, as a long one can be typed
        back before it's finished. Ended by _close_answer or _await_typed_answer.
        """
        with self._typed_lock:
            self._answer_open = True
            self._typed_answer = None
            self._answer_feed = None

    def _listening_for_answer(self, feed: AudioFeed):
        """
        Notes the spoken listen for the answer to Nova's question, so an answer typed
        while it's open ends it - and ends it now, as ask would have, if one was typed
        between the listen starting and this.

        Args:
            feed: The listen, just started.
        """
        with self._typed_lock:
            self._answer_feed = feed
            typed = self._typed_answer is not None
        if typed:
            self.satellite.stop_listening(feed)

    def _typed_answer_given(self) -> bool:
        """Whether the answer to Nova's question has been typed already."""
        with self._typed_lock:
            return self._typed_answer is not None

    def _close_answer(self, keep: bool = False) -> Optional[str]:
        """
        Stops taking what's typed as the answer to Nova's question - from now on it waits
        its turn again.

        Args:
            keep: True when the question is being left unanswered - talked over, say -
                so an answer already typed is kept as a fresh request rather than lost.

        Returns:
            The answer typed, if there was one - None when it was kept.
        """
        with self._typed_lock:
            answer = self._typed_answer
            self._answer_open = False
            self._typed_answer = None
            self._answer_feed = None
        if keep and answer is not None:
            # As arriving before any typed question, so it's never taken for an answer.
            self._typed.put((answer, -math.inf))
            return None
        return answer

    def _await_typed_answer(self) -> Optional[str]:
        """
        For a question asked in reply to something typed, with the microphone shut: the
        answer if it was typed while the question was said, or else leaves the question
        open for TYPED_ANSWER_WAIT seconds, so the turn can end there.

        The turn ends rather than waiting, so that what goes off by itself - a timer, an
        alarm, Speak - isn't held back for up to a minute while nobody types: Nova is
        idle meanwhile, as it is between turns. What's typed in that time is answered in
        a turn of its own as the answer, within the same conversation (see run). A wake
        word, a hold, a routine or anything typed later starts afresh and drops it - see
        _drop_typed_question.

        Returns:
            The answer already typed, or None once the question is left open.
        """
        with self._typed_lock:
            answer = self._typed_answer
            self._answer_open = False
            self._typed_answer = None
            self._answer_feed = None
            if answer is None:
                # From now: anything typed before is a request of its own, already waiting.
                now = time.monotonic()
                self._typed_question = (now, now + TYPED_ANSWER_WAIT)
        if answer is None:
            logger.info("Question asked, its answer can be typed for a while")
        return answer

    def _drop_typed_question(self):
        """Gives up on a typed question still open, as something else starts a turn."""
        with self._typed_lock:
            self._typed_question = None

    def _typed_turn(self) -> Optional[Dict]:
        """
        The next thing typed, to be answered now - unless something is being announced:
        Speak, an alert, a routine, or a round of a timer or an alarm. Answering would
        silence it part way, which it would take as heard - and a battery warning, sent
        again until it's said, would be lost. So it waits for the announcement to finish,
        as a typed request waits for a conversation. A timer or an alarm left ringing
        lets go between its rounds, so a typed "stop" still gets its turn.

        Taken with the announcements' lock held, and the session marked busy before it's
        let go, so an announcement starting just then waits for this turn rather than
        being silenced by it.

        Returns:
            As _next_typed - or None while something is announced, or nothing is typed.
        """
        if not self._announcing.acquire(blocking=False):
            return None
        try:
            wake = self._next_typed()
            if wake is not None:
                self._idle.clear()
            return wake
        finally:
            self._announcing.release()

    def _get_typed(self) -> Optional[Tuple[str, float]]:
        """
        Takes the next thing typed off the queue, judging the switch's start as it's taken.

        Returns:
            What was typed, with when it arrived - or None with nothing typed, or only starts of
            the switch's which are dropped.
        """
        while True:
            try:
                text, arrived = self._typed.get_nowait()
            except queue.Empty:
                return None
            if text is not SWITCH_START:
                return text, arrived
            if self._switch_start_taken():
                return START_PHRASE, arrived

    def _switch_start_taken(self) -> bool:
        """
        Judges a start of Home Assistant's Conversation switch as its turn comes, not only as it was
        asked for - the session may have been busy for a long while, and meanwhile the switch may
        have been turned off, tapped again, or a conversation started some other way.

        Returns:
            True if it is to be answered as "let's talk" is - False if it is dropped, saying nothing:
            it was cancelled by the switch being turned off, or a conversation is on here already. One
            for conversations which are switched off since is refused aloud, not left to Claude as chat.
        """
        with self._typed_lock:
            pending, self._switch_start_pending = self._switch_start_pending, False
        if not pending or self._conversation_here():
            return False
        if not self._conversations_enabled():
            self._speak_from_home_assistant(UNAVAILABLE["conversation"])
            return False
        return True

    def _next_typed(self) -> Optional[Dict]:
        """
        The next thing typed, as run answers it, and whether it answers the typed
        question left open - so it's that only if it was typed while the question was
        open. Either way the question is over with it. With nothing typed, a question
        whose time is up is given up on.

        Returns:
            {"typed": text, "follow_up": whether it answers the question, "talked_over":
            False, "feed": None} - or None, with nothing typed.
        """
        typed = self._get_typed()
        if typed is None:
            with self._typed_lock:
                question = self._typed_question
                if question is not None and time.monotonic() > question[1]:
                    self._typed_question = None
                else:
                    question = None
            if question is not None:
                logger.info("No typed answer in time, so the question's left")
            return None
        text, arrived = typed
        with self._typed_lock:
            question, self._typed_question = self._typed_question, None
        follow_up = question is not None and question[0] <= arrived <= question[1]
        return {"typed": text, "follow_up": follow_up, "talked_over": False, "feed": None}

    # Running

    def run(self):
        """
        Answers each wake word the satellite hears, one at a time, until it goes or the
        session is closed - and then closes the session.
        """
        # Checked and started together, with what close() checks it by: a session the
        # brain closed before it ran - replaced by its satellite connecting again, say
        # - never starts its device, which would otherwise stay connected for good, with
        # the same MQTT client id as the new session's. One closed after is stopped.
        with self._closing:
            if self._closed.is_set():
                return
            if self.device:
                self.device.start()
                if self.home:
                    threading.Thread(target=self._look_up_device, daemon=True,
                                     name=f"ha-device-lookup-{self.satellite.name}").start()
            if self._now_playing is not None:
                threading.Thread(target=self._keep_media_controls, daemon=True,
                                 name=f"media-controls-{self.satellite.name}").start()
        try:
            while self.satellite.connected and not self._closed.is_set():
                # Waiting for the wake word is never being stuck.
                self._moved()
                try:
                    wake = self.satellite.wakes.get(timeout=WAKE_CHECK)
                except queue.Empty:
                    wake = None
                try:
                    if wake is None:
                        # Something typed in Home Assistant is answered only once no wake word
                        # is waiting: one said is always answered first, as the satellite is
                        # already sending what the user says after it. Taken in here, as it
                        # reads the settings, so going wrong skips it rather than closing the
                        # session while its satellite stays connected.
                        wake = self._typed_turn()
                        if wake is None:
                            continue
                    self._converse(wake)
                # Logged, and the next wake word answered as usual - after a moment,
                # in case it would only go wrong again straight away.
                except Exception as e:
                    logger.error(f"Error: {e}", exc_info=True)
                    time.sleep(1)
        finally:
            self.close()

    def close(self):
        """
        Lets the session go, once its satellite has gone or the brain is stopping: Home
        Assistant shows its device as unavailable, its music stops, and so does anything
        still playing on the satellite. run() ends at its next check. Safe to call more
        than once, from any thread.
        """
        with self._closing:
            if self._closed.is_set():
                return
            self._closed.set()
        self._stop_looking_up.set()
        if self.device:
            self.device.stop()
        if self.music_player:
            self.music_player.close()
        self.speaker.close()

    # Answering the user

    def _converse(self, wake: Dict):
        """
        Everything from the wake word until the assistant has nothing left to say:
        one request, or several, when the user says the wake word over a reply - or, in
        a conversation, cuts in on one - each of which starts straight away, as if the
        wake word had just been heard.

        Args:
            wake: The wake word, as the satellite's wakes have it - or something typed in
                Home Assistant, as _next_typed gives it.
        """
        # Every satellite's music in the room is held, as the room acts as one - and the
        # same ones are let go of after, however the room changes meanwhile. Looked up
        # before the assistant is marked busy, so a failure here can't leave it so.
        self._room_held = self._room_now()
        # Marks the assistant as busy, so a timer going off waits its turn rather than interrupting.
        self._idle.clear()
        # Anything but the answer to a typed question starts afresh, so that question's over.
        if not wake.get("follow_up"):
            self._drop_typed_question()
        # Something typed is already words, so there's nothing to listen to.
        self.activity = "thinking" if "typed" in wake else "listening"
        # Pauses the music for the whole interaction (speaking and playback) to avoid audio conflicts.
        # The pause goes through Spotify's servers, so it runs alongside the
        # recording rather than holding it up - see MusicHold. Held across every
        # request, so it doesn't come back for a moment when a reply is talked over.
        # Holds are counted, so one here and the satellite's own from its own wake both stand.
        for session in self._room_held:
            session.music.hold()
        try:
            # Not talked over, even if the satellite says it was - over a chime, say. Only
            # a wake word said over a reply is that - see _say.
            heard_over = self._answer(wake)
            while True:
                while heard_over is not None:
                    if heard_over.get("cut_in"):
                        # The user cut in on a reply in a conversation: what they're saying is their
                        # next turn, already in the listen the reply was heard over - see _cut_in_over.
                        heard_over = self._open_turn(False, cut_in=heard_over)
                    else:
                        heard_over = self._answer(heard_over, talked_over=heard_over["talked_over"])
                # In an open conversation every reply - and every turn with nothing in it - is
                # followed by listening again, with no wake word, until it ends. A loop, rather
                # than each turn starting the next, as a conversation can run to hundreds.
                begun = self._conversation_begun()
                if not self._conversation_goes_on():
                    break
                # Music asked for in the turn just answered plays from now, rather than once it's over.
                self._let_music_play()
                # Only the turn straight after a question is its answer.
                follow_up, self._asked = self._asked, False
                heard_over = self._open_turn(follow_up, listening_sound=begun)
        finally:
            # A listen the last reply left open, for a turn which won't now come, is stopped.
            self._let_go_of_open_feed()
            # Not left on once its turns are over, however they ended.
            self._leave_conversation()
            # The satellite silences anything going off when it hears the wake word, and
            # leaves it to the brain to let it back.
            self.speaker.allow("announcement")
            # A hold which started this is over with it, so the next can start another,
            # rather than waiting for a finger already lifted to be let go of. So is one
            # whose recording has ended: held just as the wake word started this, its
            # wake was answered part way through as talked over, and kept open, it would
            # refuse every later hold until a lift which may never come.
            # (A typed request's feed is None, which no hold can be.)
            with self._held_lock:
                if self._held is not None and (self._held is wake["feed"] or self._held.ended):
                    self._held = None
            # A question left open by something going wrong part way - anything typed for
            # it is kept as a fresh request.
            self._close_answer(keep=True)
            self.activity = "idle"
            self._idle.set()
            room, self._room_held = self._room_held, [self]
            playing, self._let_play = self._let_play, {}
            for session in room:
                if session in playing:
                    # Let go of already, for music asked for in a conversation, which plays at all of its
                    # volume now that it's over - the player it let play, whatever the session has now.
                    playing[session].set_share(1.0)
                else:
                    session.music.release()

    # An open conversation - see OpenConversation

    def _conversations_enabled(self) -> bool:
        """Whether conversations can be started at all, as the setting is now - False without any."""
        talk = self.brain.open_conversation
        return talk is not None and talk.enabled

    def _conversation_from_home_assistant(self, on: bool) -> None:
        """
        Starts a conversation here, or ends the one on here, as Home Assistant's Conversation
        switch is turned on or off.

        On, it is as if "let's talk" had been typed into Ask Nova - through the same checks, with
        the same words said here ("Go ahead.", or why not: one elsewhere, conversations switched
        off, the microphone muted), and on a start the listening sound and the open listen, as
        after the phrase said. It's handed to the session's own thread, which does the speaking
        and the listening, and waits its turn behind any request, as a typed one does. Being a
        request, it forgets the model's stale history first, as at any wake, before the
        conversation is switched on. Whether it still stands is judged when it's taken, not only when it was
        asked for - see _switch_start_taken - and there's at most one waiting. Off, it's ended with the
        falling sound and nothing said, and a start still waiting is cancelled.

        Args:
            on: True to start one, False to end the one on here.
        """
        if not on:
            # A start still waiting its turn is cancelled too: it mustn't begin one the user has turned off.
            with self._typed_lock:
                self._switch_start_pending = False
            self.end_conversation("switch")
            return
        if self._closed.is_set():
            return
        if not self._conversations_enabled():
            # A switch Home Assistant still shows for a moment after the setting was turned off.
            # Typed, the phrase would be left to Claude, so the refusal is said as it would be.
            self._speak_from_home_assistant(UNAVAILABLE["conversation"])
            return
        if self._conversation_here():
            # Already on, which the switch shows - nothing to start or say again.
            return
        with self._typed_lock:
            if self._switch_start_pending:
                # Home Assistant shows the switch off until the start is taken, so a second tap
                # while the session is busy asks for what is already waiting.
                return
            self._switch_start_pending = True
        # As arriving before any typed question, so it's never taken for its answer - which would
        # have Claude answer "let's talk" and start nothing.
        self._typed.put((SWITCH_START, -math.inf))

    def end_conversation(self, reason: str) -> None:
        """
        Ends the conversation on at this satellite, from any thread - Home Assistant's switch, a
        mute, its quiet spell - and stops the listen its turn waits in, so the turns end at once.
        What the end says and plays is left to the session's own thread, which does the speaking
        (see _conversation_goes_on): a reply already being worked out or said is finished, and
        nothing more is listened for. Nothing happens with none on here.

        Args:
            reason: Why, as OpenConversation.end has it.
        """
        talk = self.brain.open_conversation
        # Nor for a session the brain has let go of, which may have been replaced by now: one on at the
        # satellite is the new session's to end.
        if talk is None or self._closed.is_set():
            return
        with self._open_lock:
            if not talk.end(reason, satellite=self.satellite.name):
                return
            self._ended_by = reason
            feed = self._open_feed
        logger.info(f"The conversation on the {self.satellite.name} satellite was ended ({reason})")
        if feed is not None:
            self.satellite.stop_listening(feed)

    def _conversation_here(self) -> bool:
        """Whether an open conversation is on at this satellite."""
        talk = self.brain.open_conversation
        return talk is not None and talk.on_at(self.satellite.name)

    def _voice_may_cut_in(self) -> bool:
        """
        Whether the user's voice may cut in on a reply here, so what the satellite hears is
        listened to as it plays - see _cut_in_over. Otherwise only the wake word or a press
        stops a reply, as outside a conversation.

        Returns:
            True in a conversation under way here (the satellite told - not the reply which
            started it), while conversation.talk_over is "voice", or is "voice_at_computers"
            and this satellite is a computer's, pressed to talk - at a loudspeaker next to its
            microphone Nova's own voice drowns the user's - once the satellite's echo canceller
            is taking Nova's own voice out of what it hears, and while nothing else plays in its
            room - the test which makes its wake word harder to set off - so a film's dialogue
            can't stop a reply.
        """
        if not self._conversing or not self._conversation_here() or not self.satellite.echo_ready:
            return False
        talk_over = self.brain.config.section("conversation").get("talk_over", DEFAULT_TALK_OVER)
        if talk_over == "voice_at_computers" and not self.satellite.push_to_talk:
            return False
        if talk_over not in ("voice", "voice_at_computers"):
            return False
        return not self.playing_here()

    def _conversation_begun(self) -> bool:
        """
        Tells the satellite a conversation has begun here, once the turn which started it has
        been answered - so a computer holds its microphone and speaker open for it.

        Returns:
            True if one has just begun, whose first turn plays the listening sound.
        """
        if self._conversing or not self._conversation_here() or not self.satellite.connected:
            return False
        with self._open_lock:
            # An end from before this one began isn't this one's.
            self._ended_by = None
        self._conversing = True
        logger.info(f"A conversation has begun on the {self.satellite.name} satellite, so it listens after "
                    f"every reply")
        self.satellite.set_conversation(True)
        return True

    def _conversation_goes_on(self) -> bool:
        """
        Whether a conversation here goes on to another turn - ending it once its quiet spell is
        up. One which has ended is closed off: the satellite told, "I'll leave it there." said if
        the user didn't ask for the end, and the falling sound played.

        Returns:
            False outside one, as for any request - and once it has ended, or its satellite has
            gone or the session been let go, when the brain ends it and there's nobody to tell.
        """
        talk = self.brain.open_conversation
        if not self._conversing or not self.satellite.connected or self._closed.is_set():
            return False
        if talk.on_at(self.satellite.name) and talk.idle_in() <= 0:
            self.end_conversation("idle")
        with self._open_lock:
            if talk.on_at(self.satellite.name):
                return True
            ended_by, self._ended_by = self._ended_by, None
        self._conversing = False
        # A question asked in its last turn ("Shut down the PC?") goes with it, so nothing said
        # once it's over can answer it.
        with self._conversation_lock:
            self.conversation.asked_first = None
        logger.info(f"The conversation on the {self.satellite.name} satellite is over")
        self.satellite.set_conversation(False)
        if ended_by in UNASKED_ENDS:
            self._say(CONVERSATION_LEFT)
        self._not_heard()
        return False

    def _leave_conversation(self):
        """
        Ends a conversation which is still on here once its turns are over - which only happens
        when its satellite has gone or the session been let go, as the brain ends it too, or
        something went wrong part way - so the microphone is never left open with nothing
        listening to it.
        """
        if not self._conversing and not self._conversation_here():
            return
        self._conversing = False
        if self._closed.is_set():
            # Let go of by the brain, which ends its conversation itself (see Brain._let_go) - and which
            # may have replaced it with a new session for the same satellite by now, whose own
            # conversation this mustn't end: that one's open listen would be left holding the microphone.
            # Only one this session's own turn started after it was let go of - its start already past
            # the check which refuses it now - is ended here, as nothing else would: unless a newer
            # session has begun it as its own, asked for there again meanwhile.
            newer = self.brain.session_for(self.satellite.name)
            if newer is None or newer is self or not newer._conversing:
                self.brain.open_conversation.end("gone", satellite=self.satellite.name, by=self.actions)
            return
        self.brain.open_conversation.end("gone", satellite=self.satellite.name)
        if self.satellite.connected and not self._closed.is_set():
            try:
                self.satellite.set_conversation(False)
                self._not_heard()
            except Exception as e:
                logger.error(f"Couldn't tell the {self.satellite.name} satellite its conversation is over: {e}")

    def _let_music_play(self):
        """
        Lets music asked for during a conversation play now, between its turns, rather than once it ends,
        which could be half an hour: the conversation's hold on each player of the room which has had
        music asked of it is let go of, turned down to MUSIC_SHARE of its volume until the end (see
        _converse), so Nova is still heard over it without its being paused for every reply. Holds are
        counted, so an announcement's still keeps it quiet. Music which was playing as the conversation
        began stays held, as for any request. Spotify takes no share, and what's asked of it is kept
        paused under the hold until the end instead (see ActionExecutor._until_the_conversation_ends).
        """
        for session in self._room_held:
            player = session.music_player
            if session in self._let_play or not getattr(player, "asked_for", lambda: False)():
                continue
            # Turned down first, so it's never heard for a moment at all of its volume.
            player.set_share(MUSIC_SHARE)
            self._let_play[session] = player
            session.music.release()

    def _room_now(self) -> List["Session"]:
        """
        Returns:
            The connected sessions of this satellite's room, this one first - or just
            this one if they can't be looked up, so what it's doing goes ahead alone.
        """
        try:
            return [self] + [session for session in self.brain.room_sessions(self.satellite.name)
                             if session is not self]
        except Exception as e:
            logger.error(f"Couldn't look up the room, so acting alone: {e}")
            return [self]

    def _elsewhere(self, said: str) -> Tuple[Optional[ActionExecutor], Optional[str]]:
        """
        Finds the actions of the satellite some words name, for playing something there.

        Args:
            said: The words for it, e.g. "the PC" - or "here", for this session's own satellite.

        Returns:
            Its session's actions and None - this session's own, if it's the one named; or
            None and what to say, if the words name none or more than one, or the one named
            isn't connected.
        """
        name, problem = self.brain.find_satellite(said, here=self.satellite.name)
        if name is None:
            return None, problem
        session = self.brain.session_for(name)
        if session is None:
            away = self.brain.spoken_name(name)
            return None, f"{away[:1].upper() + away[1:]} isn't connected."
        return session.actions, None

    def _device(self, said: Optional[str]) -> Tuple[Optional["Session"], Optional[str]]:
        """
        Finds the session of the satellite some words name, for its volume.

        Args:
            said: The words for it, e.g. "the PC", or None for this one.

        Returns:
            Its session and None - this one, with none named, or if it's the one named; or
            None and what to say, if the words name none or more than one, or the one named
            isn't connected ("Your PC's off.").
        """
        if not said:
            return self, None
        # "Here" and "this device" are this one.
        name, problem = self.brain.find_satellite(said, here=self.satellite.name)
        if name is None:
            return None, problem
        session = self.brain.session_for(name)
        if session is None:
            return None, self.brain.not_connected(name)
        return session, None

    def _mute_microphone(self, muted: bool, where: Optional[str] = None) -> Dict:
        """
        Turns a satellite's listening off or on by voice - the mute the settings page and Home
        Assistant's switch have, see set_muted.

        Args:
            muted: True to stop it hearing the wake word, False to hear it again.
            where: The words for the satellite, e.g. "the Pi", or None for none said: this one if it
                listens for a wake word, else its room's always-on one, as a timer set here rings on
                (see Brain.ring_on) - the PC only listens while its key is held.

        Returns:
            Dict with 'success' and a 'message'. A success is confirmed by a sound, mic_off.wav
            or mic_on.wav, played where it was spoken, and its message is only logged:
            "Microphone off.", "The Pi's microphone is off." for another, after "Stopped,
            and" if what rang there was stopped first. A failure says why not, when the
            words name none, or one which isn't connected or which only listens on a press
            of its key.
        """
        here = self.satellite.name
        if where:
            name, problem = self.brain.find_satellite(where, here=here)
            if name is None:
                return {"success": False, "message": problem}
        else:
            name = self.brain.ring_on(here)
        # As said mid-sentence ("the Pi"), and to start one ("The Pi").
        spoken = self.brain.spoken_name(name)
        starting = spoken[:1].upper() + spoken[1:]
        if self.brain.rooms.push_to_talk(name):
            # A press works whatever the mute (0026), so muting it would change nothing.
            return {"success": False, "message": f"{starting} only listens while you hold the key, so there's "
                                                 f"nothing to {'mute' if muted else 'unmute'}."}
        session = self.brain.session_for(name)
        if session is None:
            return {"success": False, "message": self.brain.not_connected(name)}
        # Muted while something rings there, it couldn't hear "stop", and a timer would climb
        # to full volume - so what rings is stopped first.
        stopped = muted and self.brain.clock.is_ringing(satellite=name)
        if stopped:
            self.brain.clock.dismiss(satellite=name)
        session.set_muted(muted)
        state = "off" if muted else "on"
        if name == here:
            said = f"microphone {state}." if stopped else f"Microphone {state}."
        else:
            said = f"{spoken if stopped else starting}'s microphone is {state}."
        return {"success": True, "message": f"Stopped, and {said}" if stopped else said,
                "sound": f"mic_{state}.wav", "sound_category": "general"}

    def _save_note(self, title: str, text: str, where: Optional[str] = None) -> Dict:
        """
        Has a satellite save a note in its Downloads folder - the one the user is at if it saves
        notes, else the one named or the one which does, as the brain chooses.

        Args:
            title: What to call the note.
            text: What it says. Never logged.
            where: The words for which computer, as the user said them, or None for none said.

        Returns:
            Dict with 'success' and the 'message' to say - "Saved to your Downloads as Bins." or, on
            another satellite, "Saved to the Downloads on the PC as Bins.", or why it wasn't saved. For
            a computer which isn't connected, the note goes on the notepad list instead, and that's said.
        """
        name, problem = self.brain.notes_for(self.satellite.name, where)
        if name is None:
            return {"success": False, "message": problem}
        if name == self.satellite.name:
            satellite = self.satellite
        else:
            session = self.brain.session_for(name)
            if session is None:
                # Asleep, or Nova isn't running there. Not for a computer which was asked and didn't
                # answer: its file may yet land, and a copy on the list as well would be a duplicate.
                return self._note_on_the_list(text, self.brain.not_connected(name))
            satellite = session.satellite
        # The satellite refuses it too, but a runaway reply shouldn't have to cross the link first.
        if len(text) > MOST_TEXT:
            return self._note_not_saved(TOO_LONG, name)
        saved = satellite.save_note(title, text)
        if saved is None:
            there = self.brain.spoken_name(name)
            return {"success": False, "message": f"{there[:1].upper() + there[1:]} didn't answer, so the note "
                                                  f"wasn't saved."}
        if "error" in saved:
            return self._note_not_saved(saved["error"], name)
        file = saved["file"]
        logger.info(f"Saved the note {title!r} on the {name} satellite as {file}")
        # As a browser says a download: the name it's known by, which has no ".txt" to say.
        saved_as = file.removesuffix(".txt")
        if name == self.satellite.name:
            return {"success": True, "message": f"Saved to your Downloads as {saved_as}."}
        # Said where it went, as it isn't where the user is to see it.
        return {"success": True, "message": f"Saved to the Downloads on {self.brain.spoken_name(name)} as {saved_as}."}

    def _note_on_the_list(self, text: str, why: str) -> Dict:
        """
        Puts a note on the notepad list - the one "notes" means to the lists - for a computer which
        can't be asked to save it, so it isn't lost.

        Args:
            text: What the note says, which goes on as one line. Never logged.
            why: Why it can't be saved on the computer, e.g. "Your PC's off.".

        Returns:
            Dict with 'success' True only if it went on the list, and the 'message' to say, which
            starts with why. A failure may be unsure, if Home Assistant was too slow to say.
        """
        said = why.removesuffix(".")
        # A list's thing is a line, which a note's lines would otherwise run on from.
        item = " ".join(line.strip() for line in text.splitlines() if line.strip())
        if len(item) > LIST_ITEM_MOST:
            return {"success": False,
                    "message": f"{said}, and that note's too long for your notepad list, so it wasn't saved."}
        lists = self.brain.lists
        added = lists.add([item], "notes") if lists is not None else {}
        if added.get("unsure"):
            # Home Assistant may well have added it, so "couldn't" would be wrong.
            return {"success": False,
                    "message": f"{said}, and Home Assistant was slow, so I'm not sure it went on your notepad "
                               f"list. Have a look."}
        if not added.get("success"):
            return {"success": False, "message": f"{said}, and I couldn't put it on your notepad list either."}
        # Lists says "Added <item> to your notepad list.", which names the list as it's called in Home Assistant.
        _, found, name = added["message"].removesuffix(".").rpartition(" to your ")
        where = f"your {name}" if found else "your notes list"
        return {"success": True, "message": f"{said}, so I've put it on {where} instead."}

    def _note_not_saved(self, why: str, name: str) -> Dict:
        """
        Args:
            why: The sentence to say for it, e.g. "There's no Downloads folder to save it in."
            name: The satellite the note was to be saved on.

        Returns:
            Dict with 'success' False and the 'message' to say, with where it was to be saved.
        """
        return {"success": False, "message": f"I couldn't save it on {self.brain.spoken_name(name)}: {why}"}

    def _room_players(self) -> List:
        """
        The music players of the user's room - see ActionExecutor - once the pause of
        each of the others has landed, as Conversation._run waits for this one's.

        Returns:
            Those of the sessions whose music this conversation holds, this one's first.
        """
        room = list(self._room_held)
        for session in room[1:]:
            session.music.wait()
        return [session.music_player for session in room]

    def _answer(self, wake: Dict, talked_over: bool = False) -> Optional[Dict]:
        """
        Answers one request, from the moment the wake word was heard.

        Args:
            wake: The wake word, with the feed of what the user said after it - or what
                was typed in Home Assistant, as its "typed", with "follow_up" True when
                it answers a typed question left open - see _await_typed_answer.
            talked_over: True when the wake word was said over the last reply - see
                Conversation.start.

        Returns:
            The wake word heard over this reply too, so the next request has already
            begun - or None.
        """
        typed = wake.get("typed")
        follow_up = typed is not None and bool(wake.get("follow_up"))
        # Nothing it asks is answered until this request's reply has been said.
        self._asked = False
        # Times the interaction from this moment on - see Timeline.
        timeline = Timeline(follow_up=follow_up)
        timeline.mark("wake")
        if typed is None:
            print("Listening...")

        # What was audibly going off at the moment the wake word fired,
        # read before the interaction quietens anything, since what
        # matters is what the user could hear when they started talking -
        # which is only ever what rings on this satellite, or in its room. Not for the
        # answer to a typed question, which carries on its conversation - see below.
        ringing = None if follow_up else self.brain.clock.ringing_state(
            satellite=heard_in(self.brain.room_of(self.satellite.name)))

        # Anything going off stops the moment the wake word is heard, rather than
        # playing on over the user - and into the microphone - until its sound
        # ends. It carries on afterwards unless the conversation dealt with it. The same
        # for something typed, so its reply isn't said over it, and a typed "stop" stops
        # what was ringing as it was sent, as a spoken one does.
        self.speaker.silence("announcement")
        try:
            if follow_up:
                # The answer to a typed question carries on the conversation which asked
                # it, rather than starting another - start would drop what a yes to it
                # would do (Conversation.asked_first), and the count of questions asked.
                # A timer, an alarm, an alert or Speak said in between doesn't change
                # that: none of them goes through the conversation, and the user answers
                # the question they were asked, which still stands. A routine does go
                # through it, starting a conversation of its own, so it drops the question
                # (see run_routine). What was ringing is still as the question's turn
                # found it, which matters only to a "stop" said over something ringing.
                return self._handle_typed(typed, follow_up=True, timeline=timeline)
            with self._conversation_lock:
                self.conversation.start(ringing, talked_over=talked_over, pressed=wake.get("pressed", False))
            if typed is not None:
                return self._handle_typed(typed, timeline=timeline)
            return self._handle_interaction(wake["feed"], timeline=timeline, pre_roll=wake.get("pre_roll"),
                                            pressed=wake.get("pressed", False))
        except Exception as e:
            # Said out loud, rather than only logged, so it isn't taken for the
            # assistant not having heard. Only for something which went wrong
            # during a conversation.
            logger.error(f"Interaction failed: {e}", exc_info=True)
            self._apologise()
            return None
        finally:
            timeline.save(self.latency_file)

    def _open_turn(self, follow_up: bool, listening_sound: bool = False,
                   cut_in: Optional[Dict] = None) -> Optional[Dict]:
        """
        One turn of an open conversation: whatever the user says next, with no wake word or
        press, answered as a fresh request - so the everyday commands, routines and dismissals
        all work - or, straight after a question, as its answer. Something typed in Home
        Assistant is the turn instead, whether it was typed while the reply before was said or
        while this waits. It listens in the listen the reply before was heard over, if that was
        left open for it (see _cut_in_over), or asks for one of its own.

        Args:
            follow_up: True when the last reply asked something.
            listening_sound: True for the conversation's first turn, which plays the listening
                sound as it starts - never between turns, where it would play dozens of times.
            cut_in: The user cutting in on the reply before, as _cut_in gives it: the turn is
                what they're saying, from where it gives - a fresh request said over a reply,
                so "stop" only stops Nova talking (see Conversation.handle), and begun on a
                press if it was one, as a pressed wake is.

        Returns:
            As _answer.
        """
        timeline = Timeline(follow_up=follow_up, open_turn=True)
        feed = None
        pressed = cut_in is not None and cut_in["cut_in"] == "press"
        try:
            listen_from = None
            if cut_in is not None:
                # They're already talking, so nothing typed comes first. What's going off is read
                # now, as they started a moment ago.
                ringing = self.brain.clock.ringing_state(
                    satellite=heard_in(self.brain.room_of(self.satellite.name)))
                with self._conversation_lock:
                    self.conversation.start(ringing, talked_over=True, pressed=pressed)
                self._open_answer()
                feed, listen_from = cut_in["feed"], cut_in["from"]
            else:
                if not follow_up:
                    taken = self._get_typed()
                    typed = taken[0] if taken is not None else None
                    # Afresh, as a wake word starts a request - what's going off read as it starts.
                    ringing = self.brain.clock.ringing_state(
                        satellite=heard_in(self.brain.room_of(self.satellite.name)))
                    with self._conversation_lock:
                        self.conversation.start(ringing)
                    if typed is not None:
                        # The listen the reply before left open isn't wanted for it.
                        self._let_go_of_open_feed()
                        return self._handle_typed(typed, timeline=timeline)
                    # From now, what's typed is this turn, as the answer to a question is the next.
                    self._open_answer()
                feed, listen_from = self._carried() or (self._open_listen(), None)
                if feed is None:
                    return None
            self._listening_for_answer(feed)
            if listening_sound and self.listening_sound_enabled:
                self._play_sound("listening.wav")
            return self._handle_interaction(feed, follow_up=follow_up, timeline=timeline, open_turn=True,
                                            listen_from=listen_from, pressed=pressed)
        except Exception as e:
            # Said, as for any request, so it isn't taken for not having been heard - and the
            # conversation ended, rather than every turn after it going wrong the same way at once.
            logger.error(f"A turn of the conversation failed: {e}", exc_info=True)
            self._apologise()
            self.end_conversation("failed")
            return None
        finally:
            with self._open_lock:
                # Only this turn's own: its reply may have left the next turn's listen open.
                if self._open_feed is feed:
                    self._open_feed = None
            timeline.save(self.latency_file)

    def _carried(self) -> Optional[Tuple[AudioFeed, int]]:
        """
        Returns:
            The listen the reply before was heard over and left open for this turn, with where
            in it the turn starts - see _cut_in_over - or None, when the turn asks for one of its
            own. One which has ended since, the satellite having stopped sending, or which has
            nowhere to start, is let go of.
        """
        with self._open_lock:
            feed, start, self._open_from = self._open_feed, self._open_from, None
        if feed is None:
            return None
        if start is None or feed.ended:
            # One with nowhere to start was left by something going wrong, and is stopped
            # rather than left sending under the turn's own listen.
            self._let_go_of_open_feed(feed)
            return None
        return feed, start

    def _let_go_of_open_feed(self, feed: Optional[AudioFeed] = None):
        """
        Stops a listen noted as _open_feed which no turn will use, so the satellite isn't left
        sending what it hears.

        Args:
            feed: This one - forgotten only if it's still the one noted. Whichever is noted if None.
        """
        with self._open_lock:
            if feed is None:
                feed = self._open_feed
            if feed is not None and self._open_feed is feed:
                self._open_feed = self._open_from = None
        if feed is not None:
            # Nothing, should the satellite have moved on to another recording already.
            self.satellite.stop_listening(feed)

    def _open_listen(self) -> Optional[AudioFeed]:
        """
        Waiting for the user to start speaking: has the satellite send what it hears for a turn of
        the conversation, for as long as they take, noted so that ending the conversation stops it.

        Returns:
            The feed - or None if the conversation has ended meanwhile, when it's stopped at once.
        """
        feed = self.satellite.listen(open=True)
        with self._open_lock:
            if self._conversation_here():
                # Its turn starts wherever it does: no start left for another listen is its own.
                self._open_feed, self._open_from = feed, None
                return feed
        self.satellite.stop_listening(feed)
        return None

    def _open_wait(self) -> float:
        """
        Returns:
            How long a turn of a conversation waits for the user to start, in seconds: until the quiet spell
            would end the conversation - not at all once that's due, or once the conversation has ended
            meanwhile, when there's no spell left.
        """
        wait = self.brain.open_conversation.idle_in()
        return max(0.0, wait) if math.isfinite(wait) else 0.0

    # The gaps in a conversation, in which what goes off by itself is said

    def _waited(self, quiet: float):
        """
        Notes one frame of a conversation's wait for the user's next turn, as its detector heard it, on
        the session's own thread: the wait is a gap once it has heard no speech for GAP_QUIET, until the
        user speaks - and none at all once their turn has begun, however long they pause in it.

        Args:
            quiet: How many seconds of audio in a row up to the frame have been no speech.

        Raises:
            _Announced: When an announcement's words have begun in the gap, none of which is to be
                listened to - see _after_announcement. Once the user has begun their turn they're heard
                with it, rather than it being lost.
        """
        gap = not self._turn_begun and quiet >= GAP_QUIET
        self._gap_had = self._gap_had or gap
        with self._gap:
            if self._gap_words and not self._turn_begun:
                self._in_gap = False
                raise _Announced()
            if gap != self._in_gap:
                self._in_gap = gap
                self._gap.notify_all()

    def _begun(self, heard: Callable[[np.ndarray], None]) -> Callable[[np.ndarray], None]:
        """
        Args:
            heard: What's given a turn of a conversation as it's recorded - speech-to-text's add.

        Returns:
            The same, which first ends the wait's gap for the rest of the user's turn: the recording gives
            it nothing until they have started talking. If the wait has been a gap, it reads then what is
            ringing, for the turn to silence (see _handle_interaction): anything which came due in the
            minutes the wait can last rang in its gap, and the user talking means they've heard it, as
            for an ordinary request. Not without a gap, when what came due since the wait began hasn't
            rung yet, and mustn't be silenced unheard.
        """
        def begun(audio: np.ndarray):
            if not self._turn_begun:
                self._turn_begun = True
                self._close_gap()
                if self._gap_had:
                    self._ringing_begun = self.brain.clock.ringing_state(
                        satellite=heard_in(self.brain.room_of(self.satellite.name)))
            heard(audio)
        return begun

    def _close_gap(self):
        """Ends a gap in a conversation here, as the user begins their turn or the wait for it is over."""
        with self._gap:
            self._in_gap = False

    def _after_announcement(self, feed: AudioFeed) -> int:
        """
        Waits, on the session's own thread, for an announcement's words which began in the gap it was
        waiting in to end - listening to none of them, as what's left of them in what the satellite hears
        could be taken for the user's turn.

        Args:
            feed: The wait's listen.

        Returns:
            Where in it the wait starts again: ANNOUNCED_TAIL after where it had got to as they ended.
        """
        self._announcement_over()
        return feed.position + round(ANNOUNCED_TAIL * RATE)

    def _announcement_over(self):
        """
        Waits, on the session's own thread, for an announcement said in a gap of the conversation here to
        finish - so a reply or a sound is never played over it. Returns at once outside a conversation,
        where none is said in a gap.
        """
        with self._gap:
            # Not for a routine which took the gap, whose own sounds are played in it.
            while self._gap_taken and self._gap_taker != threading.get_ident():
                # Getting on, as the announcement ends however its satellite answers - see _play_announcement.
                self._moved()
                self._gap.wait(GAP_CHECK)

    def _take_gap(self, words: bool, still_wanted: Optional[Callable[[], bool]] = None) -> bool:
        """
        Waits for a gap in a conversation here, and takes it for an announcement or a routine: until it's
        let go of (see _let_gap_go), the session's thread says nothing, and with words listens to nothing.
        The satellite is let play announcements again, which it silences as it hears the wake word that
        starts the conversation.

        Args:
            words: Whether what's to be played is words, rather than an announcement's sound - which is
                listened through, as that's when the user would say "stop" to it.
            still_wanted: The clock's check of what rings, given up on as soon as it's False - a timer
                dismissed while it waits takes no gap, so nothing the user starts saying is lost to its words.

        Returns:
            True once it's taken, or given up on. False at once with no conversation on here, and once the
            conversation has ended or the session is idle - when it waits its turn as outside one.
        """
        with self._gap:
            while True:
                if self._idle.is_set() or not self._conversation_here():
                    return False
                if still_wanted is not None and not still_wanted():
                    return True
                if self._in_gap:
                    break
                self._gap.wait(GAP_CHECK)
            self._gap_taken, self._gap_words, self._gap_taker = True, words, threading.get_ident()
        self.speaker.allow("announcement")
        return True

    def _let_gap_go(self):
        """Lets go of a gap an announcement took, if it took one, so the conversation can carry on."""
        with self._gap:
            if self._gap_taken:
                self._gap_taken = self._gap_words = False
                self._gap.notify_all()

    def _handle_interaction(self, feed, follow_up: bool = False, timeline: Optional[Timeline] = None,
                            pre_roll: Optional[int] = None, pressed: bool = False, open_turn: bool = False,
                            listen_from: Optional[int] = None) -> Optional[Dict]:
        """
        Handles one full interaction after the wake word: records the query from what
        the satellite sends, transcribes it, has the conversation work out the reply
        (see conversation.py), and plays it.

        Args:
            feed: The AudioFeed of what the user says, as the satellite sends it.
            follow_up: True when the assistant has just asked the user a question
                       and is listening for the answer, which allows a little
                       longer to start speaking than a fresh request does.
            timeline: The Timeline to mark each stage on. The caller saves it.
            pre_roll: How many samples at the start of the feed are from before the
                wake word, as the satellite said - or None, when audio.pre_roll's
                worth are taken to be. Never any for a follow-up.
            pressed: True when the recording began on a press of a key, which the
                satellite has already answered with a sound of its own - so the brain
                plays neither the listening sound nor the falling one for a recording with
                no speech in it. One the satellite ended on a press plays no falling one
                either - see AudioFeed.pressed.
            open_turn: True for a turn of an open conversation (see _open_turn), from its
                open listen: it waits for the user to start for as long as the conversation's
                quiet spell has left, with no listening sound and nothing from before kept, and
                is as patient as the conversation's settings say. Silence, noise and words
                imagined in noise say and play nothing, as the next turn simply begins.
            listen_from: For an open turn, where in the feed it starts - its start if None.

        Returns:
            The wake word heard over the reply, which the next request starts from - see
            _say. None otherwise.
        """
        # A throwaway one when none is given, so the marks below never need checking for.
        timeline = timeline or Timeline()

        # Read once, so the whole of a recording is by the same settings.
        thresholds = self.thresholds
        silence_duration = thresholds.get("silence_duration", 0.6)
        timeout = thresholds.get("recording_timeout", 15.0)
        speech_timeout = thresholds.get("speech_timeout", 3.0)
        if follow_up:
            # Answering a question takes a moment's thought, so there's more time to
            # start talking than when the user has just said the wake word.
            speech_timeout = thresholds.get("follow_up_timeout", speech_timeout * 2)
        open_wait = None
        # A turn of a conversation under way here - an open one, or one begun by the wake word or a
        # press over a reply in it (see _converse), which is no less the user's turn. Not the request
        # which starts one, nor any outside one.
        conversing = open_turn or (self._conversing and self._conversation_here())
        if conversing:
            # A conversation's own patience: people pause longer between thoughts in one
            # than when asking for something, and talk for longer.
            talk = self.brain.config.section("conversation")
            silence_duration = _number(talk.get("pause_seconds"), DEFAULT_PAUSE_SECONDS)
            timeout = _number(talk.get("longest_turn"), DEFAULT_LONGEST_TURN)
        if open_turn:
            open_wait = self._open_wait()

        self.activity = "listening"
        self.heard = ""

        # A short sound to say it's listening, since on a headless Pi there's otherwise no
        # way to tell the wake word was heard, or when to start talking. The satellite
        # is already sending what its microphone hears, so nothing said while the sound
        # plays goes missing, and it's far too short to count as the user starting to speak.
        # Not between a conversation's turns, where it would play dozens of times.
        if self.listening_sound_enabled and not pressed and not open_turn:
            self._play_sound("listening.wav")

        # Speech-to-text is given the recording as it's made, so it can work out the
        # words while the user is still saying them - see speech_to_text.py.
        listening = self.brain.stt.listen()

        # Two more ways to end a recording, for a computer only: its microphone is close, and
        # a game or a video playing nearby can be taken for speech by the voice activity model
        # for as long as it plays, so the quiet which ends a recording may never come. A wake
        # word satellite's recordings go on as they always have.
        voice_drop = words_quiet = None
        if self.satellite.push_to_talk:
            drop_db = _number(thresholds.get("press_voice_drop_db"), DEFAULT_PRESS_VOICE_DROP_DB)
            voice_drop = 10 ** (-drop_db / 20)
            # A listener which can't say how long its words have been still - a stand-in for
            # Nemotron's in a test - leaves the rule about the words off.
            words_quiet = getattr(listening, "quiet_words", None)
        no_words_after = _number(thresholds.get("press_no_words_seconds"), DEFAULT_PRESS_NO_WORDS_SECONDS)
        if conversing:
            # Nothing may end a turn of a conversation sooner than its pause.
            no_words_after = max(no_words_after, silence_duration)
        # Filled in as the recording ends, for what ended it and how loud the user was.
        report: Dict = {}

        # Records audio until silence / a timeout occurs. For a fresh request, the
        # satellite sent the moment from before the wake word was detected first, since
        # "Hey Nova what time is it" said in one breath has often started on "what" by
        # then. That's kept, but not listened to: it often holds the end of "Nova" too,
        # which would otherwise start the recording, and the pause for the listening
        # sound then end it. An answer comes with nothing from before - and a turn of a
        # conversation with only what the recording keeps of its own wait.
        detector = self.speech_detector
        heard = listening.add
        if open_turn:
            before = 0
            start = 0 if listen_from is None else listen_from
            # Waiting for the user to start can go on for minutes, during which the session is moving - and
            # is a gap whenever it has heard no speech for a while, until they begin (see _waited).
            detector = _Moving(detector, self._moved, self._waited)
            heard = self._begun(listening.add)
            self._turn_begun = self._gap_had = False
            self._ringing_begun = None
        elif follow_up:
            before = start = 0
        elif pre_roll is None:
            before = start = round(self.pre_roll * RATE)
        else:
            before = start = pre_roll
        try:
            while True:
                try:
                    audio_data = record_until_silence(
                        feed, detector, listen_from=start, keep_before=before / RATE,
                        silence_duration=silence_duration,
                        speech_timeout=speech_timeout,
                        timeout=timeout,
                        timeline=timeline, heard=heard, voice_drop=voice_drop,
                        words_quiet=words_quiet, no_words_after=no_words_after, report=report,
                        open_wait=open_wait)
                    break
                except _Announced:
                    # An announcement's words began in the wait's gap, and aren't the user's, so the wait starts
                    # again once they're over. Nothing had been given to speech-to-text, as they hadn't begun.
                    start = self._after_announcement(feed)
                    open_wait = self._open_wait()
        except BaseException:
            # Otherwise it would wait on for the rest of a recording which isn't coming.
            listening.cancel()
            raise
        finally:
            if open_turn:
                # Whatever ended the wait, it's no gap now.
                self._close_gap()
            # The user has stopped, or isn't going to start - so the satellite stops sending.
            self.satellite.stop_listening(feed)
        # A recording the satellite ended on a press of its key ended, as far as the recorder
        # can tell, when its feed did - the press is what makes that worth saying.
        if report.get("ended_by") == "feed ended" and feed.pressed:
            report["ended_by"] = "pressed"
        # So the log of timings says which of the ways to end a recording did.
        timeline.note(ended_by=report.get("ended_by"))
        # An answer the user ended with a press, having said nothing, is their changing
        # their mind, as one begun on a press is.
        pressed = pressed or feed.pressed

        if open_turn and not self._conversation_here():
            # Ended meanwhile - from Home Assistant, say - so half a turn isn't acted on, and
            # anything typed for it is kept as a fresh request.
            listening.cancel()
            self._close_answer(keep=True)
            timeline.note(outcome="conversation_ended")
            return None

        # Anything typed from now on is a fresh request - and an answer typed while this
        # was listening, which ended it, is used instead of what was heard - or, for a turn
        # of a conversation, the turn typed. (Should the recording fail, _converse keeps that
        # answer as a fresh request.)
        typed = self._close_answer() if follow_up or open_turn else None

        if not self.satellite.connected:
            # Gone part way through, so there's nobody to answer - and half a request
            # isn't acted on. An answer typed into this listen goes too, as the session
            # is closing.
            listening.cancel()
            timeline.note(outcome="satellite_gone")
            return None

        if typed is not None:
            listening.cancel()
            return self._handle_typed(typed, follow_up=follow_up, timeline=timeline)

        if audio_data is None or len(audio_data) == 0:
            listening.cancel()
            timeline.note(outcome="no_speech")
            # Between a conversation's turns, nothing heard is said nothing of: the next begins.
            if not open_turn:
                self._not_heard(pressed)
            return None

        # The words, once speech-to-text has caught up with the end of the recording,
        # carrying on listening if no text comes back. The audio is already at the
        # 16kHz speech-to-text expects, since the satellite sends it at that.
        self.activity = "thinking"
        transcription = listening.finish(audio_data, timeline=timeline, quiet_after=silence_duration,
                                         wake_word=self.wake_word)
        timeline.mark("stt_done")
        timeline.note(audio_seconds=round(len(audio_data) / RATE, 2))

        # Retrieves the transcribed text, without the end of the wake word which the
        # recording sometimes catches - see strip_wake_word.
        text = strip_wake_word((transcription or {}).get("text", ""), self.wake_word)
        if self.satellite.push_to_talk:
            self._note_if_long(report, timeline, text, stt_error="error" in (transcription or {}))
        if not text.strip():
            timeline.note(outcome="nothing_transcribed")
            if open_turn:
                # Between a conversation's turns, sound heard as nothing - a cough, a door, words
                # imagined in noise - is said nothing of, as the next turn simply begins. Only
                # Nemotron failing is said.
                if "error" in (transcription or {}):
                    self._apologise()
            elif (transcription or {}).get("imagined"):
                # Words imagined in noise - a cough heard as "Thank you." - are most
                # likely a false wake word, so aren't apologised for. A press has no
                # false wake word, so is told nothing.
                self._not_heard(pressed)
            elif "error" in (transcription or {}):
                # Nemotron failed, as it was made and heard again whole - not the user
                # not being caught.
                self._apologise()
            else:
                # The user spoke - the recording found them talking - so is told rather
                # than left waiting, as they were after a lone "Pause." which Nemotron
                # heard as nothing.
                self._say(NOT_CAUGHT)
            return None

        if open_turn and self._ringing_begun is not None:
            # What was ringing as the user began, rather than as the wait for them did - see _begun.
            with self._conversation_lock:
                self.conversation.ringing = self._ringing_begun
        # Looked at first, as the conversation may be ended from elsewhere while the reply is worked out or
        # said - a mute, or Home Assistant's switch - after which nothing more is listened for.
        conversation_on = self._conversation_here()
        heard_over, asked = self._reply_to(text, follow_up=follow_up, timeline=timeline)
        # In a conversation, its own next turn is the answer - see _converse.
        if heard_over is not None or not asked or conversation_on or self._conversation_here():
            return heard_over

        # The assistant asked something, so it listens straight away for the answer.
        # The answer is timed on its own, from when listening starts again.
        follow_up_timeline = Timeline(follow_up=True)
        try:
            if self._typed_answer_given():
                # Typed back while the question was being said, so there's nothing to listen for.
                return self._handle_typed(self._close_answer(), follow_up=True, timeline=follow_up_timeline)
            logger.info("Question asked, listening for a reply")
            print("Listening...")
            # Asked for before the listening sound, so an answer started over it isn't
            # lost - and recorded from this feed, which is the one the satellite sends
            # it to. Noted, so an answer typed meanwhile ends it.
            answer = self.satellite.listen()
            self._listening_for_answer(answer)
            return self._handle_interaction(answer, follow_up=True, timeline=follow_up_timeline)
        finally:
            follow_up_timeline.save(self.latency_file)

    def _note_if_long(self, report: Dict, timeline: Timeline, text: str, stt_error: bool = False):
        """
        Keeps a note of a recording whose speech ran long, for seeing how well the ways of
        ending a recording work - see long_recordings.py. Done once the words are known, so
        they can go in it, and even when there are none: loud sound which wasn't speech is
        the case it's most wanted for.

        Args:
            report: What record_until_silence said about the recording.
            timeline: The recording's Timeline, whose marks say when the speech started
                and ended.
            text: What was heard.
            stt_error: Whether speech-to-text failed, which is said in the note, as nothing
                heard would otherwise look like loud sound in which there was no word.
        """
        started, ended = timeline.marks.get("speech_started"), timeline.marks.get("speech_ended")
        if started is None or ended is None:
            return
        if stt_error:
            report = {**report, "stt_error": True}
        # The words are only kept as they are in the logs: while transcripts are.
        note_long_recording(self.long_recordings_file, datetime.now(), (ended - started) / 1000, report,
                            words=text if transcripts_logged() else None)

    def _handle_typed(self, text: str, follow_up: bool = False, timeline: Optional[Timeline] = None) -> Optional[Dict]:
        """
        Answers something typed in Home Assistant's Ask Nova box, as _handle_interaction
        answers what's said once speech-to-text has the words: with no listening sound,
        and without the satellite being asked to listen, muted or not. A question asked
        back is answered by typing too - see _await_typed_answer.

        What was typed was logged by the device as heard text is, and is shown here only
        as heard text is, so the transcripts setting decides whether it's kept.

        Args:
            text: What was typed, already trimmed and not blank - see ask.
            follow_up: True when it answers a question the assistant just asked.
            timeline: The Timeline to mark each stage on. The caller saves it.

        Returns:
            The wake word heard over the reply, which the next request starts from. None
            otherwise.
        """
        timeline = timeline or Timeline()
        timeline.note(typed=True)
        self.activity = "thinking"
        logger.info("Answering what was typed in Home Assistant")

        # Looked at first, as for a turn heard - see _handle_interaction.
        conversation_on = self._conversation_here()
        # So Claude is told it was typed, not said - see _here.
        self._typing = True
        try:
            heard_over, asked = self._reply_to(text, follow_up=follow_up, timeline=timeline)
        finally:
            self._typing = False
        # In a conversation, its own next turn is the answer, spoken or typed - see _converse.
        if heard_over is not None or not asked or conversation_on or self._conversation_here():
            return heard_over

        # The microphone stays shut, as the user chose to type - and it may be muted. An
        # answer typed back while the question was said is answered now; otherwise the
        # turn ends, with the question left open for one to be typed.
        answer = self._await_typed_answer()
        if answer is None:
            return None
        follow_up_timeline = Timeline(follow_up=True)
        try:
            return self._handle_typed(answer, follow_up=True, timeline=follow_up_timeline)
        finally:
            follow_up_timeline.save(self.latency_file)

    def _reply_to(self, text: str, follow_up: bool, timeline: Timeline) -> Tuple[Optional[Dict], bool]:
        """
        Everything in a turn once the user's words are known, heard or typed: has the
        conversation work out the reply (see conversation.py), plays its sounds and says
        it, stopping if the wake word is said over it.

        Args:
            text: What the user said or typed.
            follow_up: True when it answers a question the assistant just asked.
            timeline: The Timeline to mark each stage on.

        Returns:
            (the wake word heard over the reply, or None) and whether the reply asked
            the user something - in which case what's typed from now on is the answer,
            until the caller has it or gives up on it.
        """
        timeline.note(text_chars=len(text))
        self.heard = text
        self._show(f"You: {text}")

        # Whether it was heard in an open conversation - not the turn which started one.
        conversing = self._conversation_here()
        # Everything between hearing the request and answering it - see conversation.py.
        with self._conversation_lock:
            reply = self.conversation.handle(text, follow_up=follow_up, timeline=timeline)

        if reply.listen_again:
            # Known to be a question before it's said, so an answer typed back while it
            # is - a long one - still counts.
            self._open_answer()
        for sound, category in reply.sounds:
            self._play_sound(sound, volume=self.volume.get(category))
        if reply.shown:
            self._show(f"{self.assistant_name}: {reply.shown}")
        heard_over = None
        if reply.say:
            heard_over = self._say(reply.say, timeline=timeline, listen=True)
        # Once it's said, talked over or not - or when there was nothing to say.
        self._run_after(reply.after)
        if conversing:
            self._noted_in_conversation(text, reply)
        # A question talked over goes unanswered.
        self._asked = bool(reply.listen_again) and heard_over is None
        if heard_over is not None:
            # Talked over, so the question goes unanswered - and anything typed for
            # it is a fresh request, after the one just begun.
            self._close_answer(keep=True)
            return heard_over, bool(reply.listen_again)
        return None, bool(reply.listen_again)

    def _noted_in_conversation(self, text: str, reply) -> None:
        """
        Notes a turn of an open conversation once its reply has been said: one Nova answered
        puts off the quiet spell, and is kept for the conversation's end; one Claude took for
        speech from what's playing doesn't, and enough of those in a row end it.

        Args:
            text: What the user said or typed.
            reply: The Reply to it.
        """
        talk = self.brain.open_conversation
        here = self.satellite.name
        if self._closed.is_set():
            # A turn finished by a session the brain has let go of, perhaps replaced by now: one on at the
            # satellite is the new session's, which this turn was never in.
            return
        if reply.not_for_me:
            if talk.not_for_nova(satellite=here):
                self.end_conversation("not for nova")
            return
        model = talk.model(self.llm.model_name) if reply.claude_answered else "local"
        talk.answered(text, reply.say, model, satellite=here)

    @staticmethod
    def _run_after(after: List[Callable[[], Dict]]) -> None:
        """
        Does what was left until a reply had been said, each in turn, whatever happens to
        the one before - logging a failure by its type, never raising it.

        Args:
            after: Each gives {"success", "message"}, which is logged only if it failed.
        """
        for step in after:
            try:
                result = step()
                if not result.get("success"):
                    logger.warning(f"Left until the reply was said, and failed: {result.get('message')}")
            except Exception as e:
                logger.warning(f"Left until the reply was said, and failed: {type(e).__name__}")

    def _say(self, message: str, timeline: Optional[Timeline] = None, listen: bool = False) -> Optional[Dict]:
        """
        Speaks a reply to the user through the satellite, starting as soon as its
        first sentence is ready.

        Args:
            message: What to say.
            timeline: Optional Timeline to mark when the speech started and when it finished.
            listen: Stop the moment the wake word is heard over it. A reply couldn't be
                talked over at all, so the user had to sit through every word of it -
                three and a half minutes, once, for a fortnight of their calendar. In a
                conversation the user's voice may cut in on it too - see _cut_in_over.

        Returns:
            With listen, the wake word heard over it - or before it started - which the
            next request starts from, or the user cutting in on it, as _cut_in_over gives
            it. None if there wasn't one.
        """
        if listen:
            # Woken again while the reply was being worked out - or over a chime - so the
            # satellite is already sending what the user says next. That's answered
            # instead, rather than this being said over them. It counts as talking over
            # the reply, though it never started: "Hey Nova, stop" here means don't say
            # it, and taken as a command it would pause the music for good, when it
            # was only paused while Nova answered - see Conversation.handle.
            try:
                waiting = self.satellite.wakes.get_nowait()
                logger.info("Woken again before the reply was said, so it isn't")
                return dict(waiting, talked_over=True)
            except queue.Empty:
                pass

        # An announcement begun in a gap of a conversation here finishes first, rather than being talked over.
        self._announcement_over()
        self._show(f"{self.assistant_name}: {message}")
        self.activity = "speaking"
        started = None
        if timeline:
            def started():
                timeline.mark("tts_done")
                timeline.note(reply_chars=len(message))
        tts = self.brain.tts
        # In a conversation the user's voice may cut in, so what the satellite hears is listened to
        # from as the reply starts, rather than after it - noted, so an end from elsewhere stops it.
        # None otherwise, or if the conversation has just ended.
        feed = self._open_listen() if listen and self._voice_may_cut_in() else None
        playback = self.speaker.play_stream(tts.stream(message, on_start=started), tts.sample_rate,
                                            volume=self.volume.get("general"))
        if not listen:
            self._wait_for(playback)
            heard_over = None
        elif feed is not None:
            heard_over = self._cut_in_over(playback, feed, message, timeline)
        else:
            heard_over = self._heard_over(playback)
        # Only a wake word heard while a reply was playing on the satellite stops it as
        # talking over it - one it heard just before the reply reached it still stops
        # it, so it isn't said over the user, but starts the next request afresh.
        talked_over = heard_over is not None and heard_over["talked_over"]
        if heard_over is not None:
            logger.info("Talked over, so stopped part way" if talked_over
                        else "Woken again as the reply started, so it's stopped")
            playback.stop()
        if timeline:
            timeline.mark("playback_done")
            if talked_over:
                timeline.note(talked_over=True)
        return heard_over

    def _say_first(self, message: str) -> None:
        """
        Says a line before a slow request, and returns once it has been said - so it's heard before the
        answer is waited for, not after: "Let me think about that." for the bigger model. It runs on the
        thread which is answering, which has nothing else to do until the request comes back.

        Args:
            message: What to say.
        """
        # Not over the user, who has begun their next request by the wake word or a press while this
        # was being worked out - whose reply won't be said either (see _say). Not stopped by it at the
        # Pi, as a press's recording stops a reply at a computer, it would be said over their words.
        if not self.satellite.wakes.empty():
            logger.info("Woken again before a line was said first, so it isn't")
            return
        self._say(message)
        # The answer is still being waited for.
        self.activity = "thinking"

    def _heard_over(self, playback) -> Optional[Dict]:
        """
        Waits for a reply to finish, or for the wake word to be heard over it.

        Args:
            playback: The reply, playing on the satellite.

        Returns:
            The wake word, as the satellite's wakes have it - or None once the reply
            has finished, as it does when the satellite goes.
        """
        started = time.monotonic()
        while not playback.done:
            # A reply can rightly play for minutes, so this is getting on - for as long
            # as it could still be playing, after which it's stopped.
            self._moved()
            if self._overdue(playback, started):
                break
            try:
                return self.satellite.wakes.get(timeout=TALKED_OVER_CHECK)
            except queue.Empty:
                pass
        # One last look, for a wake word said just as the reply ended - a question,
        # whose answer would otherwise be listened for next, and the new request taken
        # for it.
        try:
            return self.satellite.wakes.get_nowait()
        except queue.Empty:
            return None

    def _cut_in_over(self, playback, feed: AudioFeed, saying: str, timeline: Optional[Timeline]) -> Optional[Dict]:
        """
        Waits for a reply in a conversation to finish, hearing what's said over it as it plays,
        for the user cutting in (see CutIn): a voice dips the reply at once; real words stop it,
        and are the start of their next turn; and with no words it comes back up and carries on.
        A press over it cuts in too, and a wake word stops it as ever (see _heard_over).

        Speech-to-text only hears what's said over a reply once a voice has been heard, from just
        before it began - it's the slowest thing the brain runs, with no GPU to run it on, and a
        reply can go on for minutes. Should the listen end or stall, the conversation be ended
        from elsewhere, or anything here go wrong, the reply is finished as _heard_over finishes
        one, at its full volume, and nothing more is listened to over it.

        Args:
            playback: The reply, playing on the satellite.
            feed: What the satellite hears, from as the reply started - noted as _open_feed.
            saying: The reply's text, which heard back is only Nova's own voice.
            timeline: The turn's Timeline, which notes a cut-in and how many dips came back up,
                or None.

        Returns:
            A wake word, as _heard_over gives it - or the user cutting in, as _cut_in gives it.
            None once the reply has finished, with the feed left open for the next turn, which
            starts in it where the reply ended - or before a voice which still had it dipped,
            if that voice's words are the user's own (see _carried).
        """
        started = time.monotonic()
        # What the reply comes back up to, read before anything dips it.
        full = playback.volume
        dip_level = _number(self.brain.config.section("conversation").get("dip_level"), DEFAULT_DIP_LEVEL)
        # While a voice has the reply dipped: speech-to-text of it, and where in the feed a turn
        # it starts would start - CUT_IN_PRE_ROLL before it began. Kept here, as CutIn forgets
        # the voice as it says "undip".
        listening = began = None
        came_back = 0
        # Where the user's turn starts and how they cut in, once they have.
        cut = None
        try:
            try:
                # In here, so a detector which fails to load is gone wrong as anything else here is.
                if self._cut_in_detector is None:
                    self._cut_in_detector = make_detector(self.config)
                voice = CutIn(self._cut_in_detector, saying)
                # From the feed's start, which is where CutIn counts frames from.
                reader = feed.listen(start=0)
                while cut is None and not playback.done:
                    # A reply can rightly play for minutes, so this is getting on - see _heard_over.
                    self._moved()
                    if self._overdue(playback, started):
                        break
                    if feed.cut_in.is_set():
                        # The satellite has stopped the reply and played its own sound for the press.
                        cut = (feed.position, "press")
                        break
                    wake = self._wake_waiting()
                    frame = reader.read(FRAME) if wake is None and self._conversation_here() else None
                    if frame is None or len(frame) < FRAME:
                        # A wake word, the conversation ended from elsewhere, or the listen ended or
                        # stalled - the satellite gone, say - so there's nothing more to hear over it.
                        return self._stop_hearing_over(playback, feed, wake, full if began is not None else None)
                    if listening is not None:
                        listening.add(frame.astype(np.float32) / 32768.0)
                    heard = voice.hear(frame, listening.words() if listening is not None else "")
                    if heard == "dip":
                        playback.fade_to(full * dip_level, DIP_FADE)
                        began = max(0, voice.voice_from * FRAME - round(CUT_IN_PRE_ROLL * RATE))
                        listening = self.brain.stt.listen()
                        so_far = feed.listen(start=began).read(reader.position - began)
                        listening.add(so_far.astype(np.float32) / 32768.0)
                    elif heard == "stop":
                        cut = (began, "voice")
                    elif heard == "undip":
                        # The last word of a short "wait" may only come once speech-to-text has heard
                        # the quiet after it - so it's judged once more before the reply comes back.
                        stops, listening = self._judge_voice(listening, voice), None
                        if stops:
                            cut = (began, "voice")
                        else:
                            playback.fade_to(full, DIP_FADE)
                            came_back += 1
                            began = None
                if cut is None and feed.cut_in.is_set():
                    # Pressed just as it ended: the satellite says so before it says the reply stopped.
                    cut = (feed.position, "press")
                if cut is not None:
                    return self._cut_in(playback, feed, cut, timeline)
                # One last look, as _heard_over has, for a wake word said just as the reply ended.
                wake = self._wake_waiting()
                if wake is not None:
                    self._let_go_of_open_feed(feed)
                    return wake
                start = reader.position
                if began is not None:
                    # A voice still had it dipped as it ended: the next turn starts before it only if
                    # its words are the user's - not Nova's own echo, nor only listening noises.
                    stops, listening = self._judge_voice(listening, voice), None
                    if stops:
                        start = began
                with self._open_lock:
                    if self._open_feed is feed:
                        self._open_from = start
                return None
            except Exception as e:
                # Never left playing, perhaps dipped, under an apology for a turn which went wrong,
                # with its listen left open: a reply heard over goes on as any other would.
                logger.error(f"Couldn't hear what was said over the reply, so it carries on as usual "
                             f"({type(e).__name__})")
                return self._stop_hearing_over(playback, feed, None, full if began is not None else None)
        finally:
            # The turn which follows has its own, which hears it all again from the pre-roll.
            if listening is not None:
                listening.cancel()
            if timeline:
                # So the timings show how often a reply is dipped for nothing.
                timeline.note(dips_back=came_back)

    def _wake_waiting(self, wait: float = 0.0) -> Optional[Dict]:
        """
        Args:
            wait: How long to wait for one, in seconds - none if 0.

        Returns:
            The next wake word the satellite heard, as its wakes have it - or None.
        """
        try:
            return self.satellite.wakes.get(timeout=wait) if wait else self.satellite.wakes.get_nowait()
        except queue.Empty:
            return None

    def _stop_hearing_over(self, playback, feed: AudioFeed, wake: Optional[Dict],
                           full: Optional[float]) -> Optional[Dict]:
        """
        Stops hearing what's said over a reply, and lets go of its listen - so the reply is
        finished as _heard_over finishes one, if there's no wake word to stop it.

        Args:
            playback: The reply, playing on the satellite.
            feed: Its listen.
            wake: A wake word already taken from the satellite's wakes, or None.
            full: The reply's own volume, to bring it back up to while it's dipped - None if it
                isn't.

        Returns:
            As _heard_over.
        """
        if wake is None:
            # The satellite ends the listen a moment before the wake word which ended it arrives -
            # see RemoteSatellite._woken - so it's waited for, briefly, rather than the reply
            # being brought back up only to be stopped.
            wake = self._wake_waiting(TALKED_OVER_CHECK)
        if full is not None and wake is None:
            playback.fade_to(full, DIP_FADE)
        self._let_go_of_open_feed(feed)
        return wake if wake is not None else self._heard_over(playback)

    def _cut_in(self, playback, feed: AudioFeed, cut: Tuple[int, str], timeline: Optional[Timeline]) -> Dict:
        """
        Stops a reply the user has cut in on - before anything else, as they're talking over it.

        Args:
            playback: The reply, playing on the satellite.
            feed: The listen it was heard over, which their turn goes on in.
            cut: Where in it their turn starts, and how they cut in - "voice" or "press".
            timeline: The turn's Timeline, noting how, or None.

        Returns:
            {"talked_over": True, "feed": the feed, "from": where in it their turn starts,
            "cut_in": "voice" or "press"}.
        """
        start, how = cut
        playback.stop()
        logger.info(f"Cut in on by {how}")
        if timeline:
            timeline.note(cut_in=how)
        return {"talked_over": True, "feed": feed, "from": start, "cut_in": how}

    @staticmethod
    def _judge_voice(listening, voice: CutIn) -> bool:
        """
        Whether a voice heard over a reply is the user's - its words a stop, by CutIn's own
        judgement - once speech-to-text has heard the last of it, as its words lag the audio.
        Only Nemotron's own words, and only so long (see NemotronTranscription.settle), as the
        reply stays dipped meanwhile; the feed keeps what's said.

        Args:
            listening: Speech-to-text of the voice, which is finished by this.
            voice: The reply's CutIn.
        """
        return voice.would_stop(listening.settle())

    def _show(self, line):
        """
        Prints a line of the conversation. The console is the system journal when
        running as a service, so this is left out along with the logged transcripts
        when logging.log_transcripts is off.
        """
        if transcripts_logged():
            print(line)

    def _play_sound(self, name, volume=None):
        """
        Plays one of the assistant's sound effects on the satellite, waiting for it to finish.

        Args:
            name: Filename within the sounds folder, e.g. "success_chime.wav".
            volume: How loud, defaulting to the general volume.
        """
        if volume is None:
            volume = self.volume.get("general")
        # Never over an announcement begun in a gap of a conversation here - see _say.
        self._announcement_over()
        self._wait_for(self.speaker.play_file(str(self.brain.sounds_folder / name), volume=volume))

    def _wait_for(self, playback):
        """Waits for a sound to finish - or stops it, once it's plainly never going to say it has."""
        started = time.monotonic()
        while not playback.wait(timeout=PLAYBACK_CHECK):
            if self._overdue(playback, started):
                return

    def _overdue(self, playback, started: float) -> bool:
        """
        Whether a sound has gone on so much longer than the audio sent for it that its
        satellite will never say it has finished - see PLAYBACK_SLACK - in which case
        it's stopped, so nothing is left waiting for it.

        Args:
            playback: The sound, playing on the satellite.
            started: When it was started, by time.monotonic().
        """
        if time.monotonic() - started <= playback.duration + PLAYBACK_SLACK:
            return False
        logger.warning(f"The {self.satellite.name} satellite never said a {playback.kind} sound had finished, "
                       f"so it's stopped")
        playback.stop()
        return True

    def _moved(self):
        """
        Notes that the session's own thread is getting on - see moved_at. Not while
        Nemotron looks to have hung for good (see NemotronSpeechToText.hung), when every
        request would only be apologised for: the session is still then, for the
        watchdog to restart the brain, which loads it again.
        """
        if not self.brain.stt.hung:
            self.moved_at = time.monotonic()

    def _not_heard(self, pressed: bool = False):
        """
        Plays the listening sound backwards - a short falling blip - when nothing was
        heard or understood. Giving up used to be silent, which was easy to take for
        the assistant still listening, or still thinking.

        Args:
            pressed: True for a recording begun on a press of a key, where nothing said
                is the user changing their mind - so it stays quiet.
        """
        if self.listening_sound_enabled and not pressed:
            self._play_sound("not_heard.wav")

    def _apologise(self):
        """Says something went wrong, without letting that fail in turn."""
        try:
            self._say("Sorry, something went wrong there.")
        except Exception as e:
            logger.error(f"Couldn't even say so: {e}")

    # What goes off by itself

    def announce(self, message, sound="announcement_sound.wav", boost=False, still_wanted=None,
                 start=0.0, length=None, level=1.0, ringing=False):
        """
        Speaks a message which the user has scheduled, e.g. a timer, on the satellite -
        what the brain's clock calls for the timers and alarms set from it, and what
        its alerts and Home Assistant's messages are said with.

        Played as an "announcement", which the wake word silences straight away -
        see _answer(). It waits for the user to finish with Nova - or, in an open
        conversation here, which could go on for half an hour, for a gap in it (see
        _take_gap), its sound and then its words each taking one.

        Args:
            message: What to say. None plays just the sound, with no speech - an
                     alarm uses this for every loop after its first, so nothing
                     waits on speech synthesis while it's trying to be loud.
            sound: The sound to play first, from the sounds folder.
            boost: True to play at full volume regardless of the configured alarm
                   level. An alarm always sets this; a plain timer only once it's
                   been ringing unanswered a while - see Clock._sound_off.
            still_wanted: Called before anything plays, so a timer or alarm the
                          user dismissed while this was waiting stays quiet.
            start: Seconds into the sound to start from - an alarm's bursts carry on
                   through its sound. See Clock._ring_alarm.
            length: Seconds of the sound to play, or None for all of it.
            level: How loud the sound plays, as a share of the volume - an alarm
                   builds up from quiet. The message itself is always at full volume.
            ringing: True for what the clock rings - a timer, a reminder or an alarm -
                     whose sound is never switched off. Otherwise the sound before an
                     alert or a battery warning goes if sounds.reminder_enabled is off:
                     an alarm silenced by a sound switch would be a missed wake-up. And
                     Stop during that sound stops its words too, as still_wanted does for
                     what the clock rings - the wake word leaves them for after the
                     conversation, as it does a reminder's.

        Returns:
            Whether it was said - not if still_wanted turned False first, or it failed, or
            the satellite went before it had finished, or refused it as it went ahead over
            a conversation which outlasted its wait. One the user silenced themselves,
            with the wake word or Home Assistant's Stop, was heard, so it counts as said, as
            does one given up on because its satellite never said it had finished. A
            caller which must not lose it, like a battery warning, sends or holds it
            instead when it wasn't.
        """
        # The music is held from now, before waiting for any conversation to finish,
        # so it doesn't come back in the gap between the two. Every satellite's in the
        # room, as a conversation holds them, so a timer on the Pi isn't lost under music
        # on the PC beside it - and the same ones are let go of, however the room changes.
        room = self._room_now()
        for session in room:
            session.music.hold()
        if not ringing and not self.reminder_sound_enabled:
            sound = None
        try:
            with self._announcing:
                try:
                    return self._announce_in_turn(message, sound, boost, still_wanted, start, length, level,
                                                  ringing)
                finally:
                    # However it ended, a gap of a conversation it took is let go of, so the conversation
                    # carries on.
                    self._let_gap_go()
        finally:
            # Let back a few seconds later rather than straight away, since a timer's
            # next round comes three seconds after this one.
            for session in room:
                session.music.release(after=5)

    def _announce_in_turn(self, message, sound, boost, still_wanted, start, length, level, ringing):
        """The body of announce, run once any other announcement has finished. Returns what it does."""
        # Waits for any conversation to finish before making any announcements - or for a gap in an
        # open one here.
        if not self._wait_for_turn(still_wanted, gap="sound"):
            return False
        # Noted as its sound starts, so a Stop during it is told apart from one in the
        # conversation it waited for - see below. Not over one which outlasted the wait,
        # whose sound is refused, so a Stop then is that conversation's. One in a gap
        # has the satellite's leave, and the gap to itself.
        stops = self._stops if self._idle.is_set() or self._gap_taken else None

        # Anything which goes off by itself uses the alarm volume, so it can stay
        # loud even when the assistant's voice is turned down - and once boosted,
        # it jumps straight to full regardless of that level.
        volume = 1.0 if boost else self.volume.get("alarm")

        try:
            # An alarm's later bursts aren't printed, since there's one every few seconds.
            if message or not start:
                self._show(f"\n{self.assistant_name}: {message}" if message
                           else f"\n{self.assistant_name}: [{sound}]")

            # A sound first, so the user knows the assistant is about to speak.
            if sound:
                chimed = self._play_announcement(self.speaker.play_file(
                    str(self.brain.sounds_folder / sound), volume=volume * level, kind="announcement",
                    start=start, length=length), still_wanted)
            else:
                chimed = True
            # Whether Stop was pressed during its sound - decided as the sound ends, so a
            # Stop in a conversation the wake word started over the sound, which the words
            # then wait for, is that conversation's.
            stopped = not ringing and stops is not None and self._stops != stops

            # Its sound is over, so a reply the user started over it in a conversation can be said.
            self._let_gap_go()
            if not message:
                return chimed

            # The sound lasts a few seconds, long enough for the user to start talking
            # to the assistant - or to dismiss this - before it's finished, so the
            # message waits its turn again rather than talking over them: in a
            # conversation, for a gap of its own, in which nothing is listened to until
            # it's been said, as what's left of it reaching the microphone isn't the user.
            if not self._wait_for_turn(still_wanted, gap="words"):
                return False
            # An alert or a battery warning has no still_wanted to stop its words, as what
            # the clock rings has, so a Stop during its sound does. The user heard it start
            # and stopped it, so it counts as said - as stopped part way through its words,
            # rather than being said again or held. The wake word isn't counted: talking to
            # Nova may be about anything else, so its words are said once that's over.
            if stopped:
                logger.info("Stopped during its sound, so its words aren't said")
                return True

            # Shown as speaking only between conversations - in one, the conversation
            # itself says what the assistant's doing.
            if self._idle.is_set():
                self.activity = "speaking"
            # Gone ahead over a conversation which outlasted the wait, the words reach a
            # satellite which still has announcements silenced - see _answer. Not in a gap.
            over_a_conversation = not self._idle.is_set() and not self._gap_taken
            tts = self.brain.tts
            words = self.speaker.play_stream(tts.stream(message), tts.sample_rate, volume=volume,
                                             kind="announcement")
            said = self._play_announcement(words, still_wanted)
            if self._idle.is_set():
                self.activity = "idle"
            if over_a_conversation and words.position == 0:
                # Refused there, so ended at once with none of it played - which isn't said.
                # One the satellite took, as the conversation ended just then, says how far
                # it got.
                logger.warning("An announcement was refused as it went ahead over a long conversation, "
                               "so it wasn't said")
                return False
            # A satellite which went part way through ends its playbacks too, so they
            # look finished - it isn't said unless it's still there at the end.
            return said and self.satellite.connected
        except Exception as e:
            logger.error(f"Announcement failed: {e}")
            return False

    def _wait_for_turn(self, still_wanted=None, gap: Optional[str] = None):
        """
        Waits for any conversation to finish, then checks the announcement waiting
        on it is still wanted.

        Args:
            still_wanted: Optional check from the Clock - False once the timer or
                          alarm has been dismissed.
            gap: For an announcement, what it's about to play - "sound" or "words" - which
                in an open conversation here waits for the next gap in it and takes it (see
                _take_gap), rather than for the conversation to end, which could be half an
                hour. A routine's words are "words". None waits for the conversation to end
                whatever it is.

        Returns:
            True if the announcement should go ahead.
        """
        waited = gap is not None and self._take_gap(gap == "words", still_wanted)
        if not waited and not self._idle.wait(timeout=60):
            # One which became an open conversation meanwhile has gaps to wait for instead.
            if gap is None or not self._take_gap(gap == "words", still_wanted):
                logger.warning("Announcing over an unusually long interaction")

        # Talking to the assistant is often how something ringing gets dismissed, so
        # by the time the conversation it was waiting on has finished, it may well
        # have been dealt with - and playing it again then would be wrong.
        if still_wanted is not None and not still_wanted():
            logger.info("Dismissed while waiting its turn, so it isn't announced")
            # Nor does it keep a gap it took.
            self._let_gap_go()
            return False
        return True

    def _play_announcement(self, playback, still_wanted=None):
        """
        Waits for one sound for something which went off by itself to finish - or
        stops it the moment it's dismissed, rather than once the whole sound has
        played. An alarm's sound is 34 seconds long.

        Args:
            playback: The sound, already started.
            still_wanted: Checked while it plays - False once it's been dismissed.

        Returns:
            False if it was dismissed - still_wanted turned False - and True otherwise,
            even if it was stopped by the wake word or Home Assistant's Stop, or given up
            on as the satellite never said it had finished: the user heard what there was.
        """
        # Not counted as the session getting on (see moved_at), as this is never on
        # its own thread - so it can't hide a turn which is stuck.
        started = time.monotonic()
        while not playback.wait(timeout=PLAYBACK_CHECK):
            if still_wanted is not None and not still_wanted():
                logger.info("Dismissed part way through, so it's stopped")
                playback.stop()
                return False
            if self._overdue(playback, started):
                break
        return True

    def alert(self, alert):
        """
        Says an alert from ntfy out loud as it arrives - see ntfy.py. Like a timer, it
        waits for any conversation to finish, and pauses the music.

        Args:
            alert: The ntfy Alert.

        Returns:
            Whether it was said, as announce has it.
        """
        # "Send that to my phone" straight afterwards sends the whole of it.
        self.actions.last_answer = (alert.title or f"From {alert.topic}", alert.details)
        return self.announce(f"{alert.topic.capitalize()}: {alert.headline}")

    def run_routine(self, routine):
        """
        Runs one of the user's routines by itself - at its time, or from the settings
        page - once any conversation has finished, as an announcement waits. Its steps
        go through the conversation as if the user had said them (see routines.py),
        and what they say is said as an announcement is, so the wake word stops it.

        In an open conversation here, which could go on for half an hour, it runs in
        the next gap instead, taken as an announcement's words are (see _take_gap): it
        isn't said over a reply, and what's left of it reaching the microphone isn't
        taken for the user's turn. It leaves the conversation's turn as it found it, so a
        question Nova had just asked can still be answered.

        Args:
            routine: The routine, as Routines has it.
        """
        # The music is held from now, so a step which plays some starts it once
        # everything's been said - as when the user asks for it. In an open conversation
        # here only once it has its gap, so music let play quietly in the conversation
        # isn't paused while it waits for one.
        held = not self._conversation_here()
        if held:
            self.music.hold()
        in_gap = False
        try:
            with self._announcing:
                try:
                    if not self._wait_for_turn(gap="words"):
                        return
                    in_gap = self._gap_taken
                    if not held:
                        self.music.hold()
                        held = True
                    self.activity = "thinking"
                    # A conversation of its own, which drops what a typed question left open
                    # would do - so what's typed next is a fresh request, not its answer.
                    self._drop_typed_question()
                    # What's held for Claude with the user's next message waits for it, rather than heading a step.
                    with self._conversation_lock, self.llm.notes_held():
                        if in_gap:
                            with self.conversation.aside():
                                reply = self.conversation.run_routine(routine)
                        else:
                            self.conversation.start()
                            reply = self.conversation.run_routine(routine)
                    for sound, category in reply.sounds:
                        self._play_sound(sound, volume=self.volume.get(category))
                    if reply.shown:
                        self._show(f"{self.assistant_name}: {reply.shown}")
                    if reply.say:
                        self._show(f"{self.assistant_name}: {reply.say}")
                        self.activity = "speaking"
                        tts = self.brain.tts
                        self._play_announcement(self.speaker.play_stream(
                            tts.stream(reply.say), tts.sample_rate, volume=self.volume.get("general"),
                            kind="announcement"))
                    # Once it's said, or when nothing was - as for a reply to the user.
                    self._run_after(reply.after)
                finally:
                    # The conversation carries on, listening as it was.
                    self._let_gap_go()
        except Exception as e:
            logger.error(f"A routine failed: {e}", exc_info=True)
        finally:
            if self._idle.is_set():
                self.activity = "idle"
            elif in_gap:
                self.activity = "listening"
            if held:
                self.music.release()

    # For the settings page and Home Assistant

    def boop(self, level):
        """
        Plays the volume beep at a level, for the settings page's sliders - stopping
        the last one if it's still going, so dragging a slider doesn't pile them up.

        Args:
            level: How loud, from 0 to 1.
        """
        with self._boop_lock:
            if self._last_boop is not None:
                self._last_boop.stop()
            self._last_boop = self.speaker.play_file(str(self.brain.sounds_folder / "volume_beep.wav"), volume=level)

    def hear_voice(self, speed: float) -> bool:
        """
        Says HEAR_IT on the satellite at a speed, for the settings page's "Hear it" -
        played as a reply is, but with nothing listened for after it, and not waited
        for, as the page is.

        Args:
            speed: How fast, e.g. 1.25 - the slider's, saved or not.

        Returns:
            Whether it's being said - False while the satellite is saying anything else,
            which it would talk over: a conversation, an announcement or a routine,
            something ringing - never an alarm - or the last "Hear it".
        """
        with self._hearing_lock:
            hearing = self._last_hearing
            if (not self._idle.is_set() or self._announcing.locked()
                    or self.brain.clock.is_ringing(satellite=self.satellite.name)
                    or (hearing is not None and not hearing.done)):
                return False
            tts = self.brain.tts
            self._last_hearing = self.speaker.play_stream(tts.stream(HEAR_IT, speed=speed), tts.sample_rate,
                                                          volume=self.volume.get("general"))
            return True

    def _device_controls(self) -> DeviceControls:
        """
        What Home Assistant sees and does of the satellite - see ha_device.py. The music
        is only there with a player: without one, Home Assistant is given no player to
        show, and none of its controls.
        """
        player = self.music_player
        return DeviceControls(
            status=lambda: self.activity,
            volumes=lambda: volume_levels(self.volume, self.music_player),
            set_volume=lambda category, percent: change_volume(self.volume, self.music_player, category, percent),
            # Only what rings on this satellite - each has its own device and sensors.
            clock=lambda: self.brain.clock.overview(satellite=self.satellite.name),
            # Whether the brain has its speech-to-text to hear the satellite with.
            backend=lambda: self.brain.stt.loaded,
            muted=lambda: self.muted,
            set_muted=self.set_muted,
            stop=self.stop_what_rings_and_speaks,
            restart=self.satellite.restart,
            speak=self._speak_from_home_assistant,
            say=self._say_from_home_assistant,
            add_timer=self._add_timer_from_home_assistant,
            add_alarm=self._add_alarm_from_home_assistant,
            # The Conversation switch: on while one is running here, and withdrawn while conversations are
            # switched off - and never there at a satellite not updated for them, where none can start.
            conversations=lambda: self.satellite.can_converse and self._conversations_enabled(),
            in_conversation=self._conversation_here,
            set_conversation=self._conversation_from_home_assistant,
            # Read by the device on a thread of its own, never the one that publishes the
            # state every second, as the player may be asking Navidrome or Spotify.
            now_playing=(lambda: self.music_player.now_playing()) if player else None,
            cover=(lambda: self.music_player.cover()) if player else None,
            playlists=(lambda: self.music_player.playlists()) if player else None,
            playlist_covers=((lambda names: self.music_player.playlist_covers(names))
                             if player and hasattr(player, "playlist_covers") else None),
            # Only a wake-word satellite - the room's own speaker, whose Nova view shows them - gets
            # playlist tiles; a push-to-talk one like the PC doesn't, so the two devices, both named
            # Nova, never race for a new tile's entity id.
            playlist_tiles=not self.satellite.push_to_talk,
            playback=self._playback_from_home_assistant if player else None,
            play_playlist=self._playlist_from_home_assistant if player else None,
            # The Ask Nova box, whose replies are said here and never sent back.
            ask=self.ask)

    def _playback_from_home_assistant(self, action):
        """
        Plays, pauses, skips or goes back for Home Assistant's buttons, through the
        actions, as asking out loud does - so that a pause from Home Assistant isn't
        undone once Nova finishes talking, as with the settings page's play(). What's
        playing isn't logged, as what's said isn't unless the user wants it.

        Args:
            action: "play_pause" - which pauses what's playing, and plays, as "play some
                music" does, when it's paused or nothing is - "next" or "previous".
        """
        if action == "play_pause":
            logger.info("Play/pause from Home Assistant")
            playing = self.music_player.now_playing()
            if playing and playing.get("playing"):
                self.actions.control_playback("pause")
            else:
                # Without the offer to download anything: Home Assistant can't hear a yes.
                self.actions.play_without_offer(None)
        elif action == "next":
            logger.info("Next from Home Assistant")
            self.actions.control_playback("skip")
        elif action == "previous":
            logger.info("Previous from Home Assistant")
            self.actions.control_playback("previous")
        else:
            logger.warning(f"Home Assistant asked the music to do something unknown: {action!r}")

    def _keep_media_controls(self):
        """
        Keeps the satellite's media controls showing what its music is doing, and acts on
        their buttons, on a thread of its own until the session ends: reads the player
        every MEDIA_EVERY seconds, and straight after each button. One which fails is
        logged once, until it works again. What's playing is never logged.
        """
        failing = False
        pressed: Optional[str] = None
        while self.satellite.connected and not self._closed.is_set():
            try:
                if pressed is not None:
                    self._media_from_windows(pressed)
                self._now_playing.update(self.music_player.now_playing(), self.music_player.cover,
                                         force=pressed is not None)
                failing = False
            except Exception as e:
                if not failing:
                    logger.error(f"Couldn't keep Windows' media controls up to date on the "
                                 f"{self.satellite.name} satellite: {e}")
                failing = True
            try:
                pressed = self.satellite.media_presses.get(timeout=MEDIA_EVERY)
            except queue.Empty:
                pressed = None

    def _media_from_windows(self, action: str):
        """
        Acts on a button of Windows' media controls, on this satellite's own music only -
        never the rest of the room's, as a pause said out loud does (0026). What's playing
        isn't logged.

        Args:
            action: "pause", which keeps it paused once Nova finishes talking, as asking
                does; "play", which carries on what's paused here, or plays as Home
                Assistant's play button does with nothing to carry on; "next" or "previous".
        """
        logger.info(f"{action.capitalize()} from Windows")
        player = self.music_player
        # After any pause or resume for Nova talking which is still on its way, so it
        # can't land after this and undo it.
        self.music.wait()
        if action == "pause":
            player.pause()
            # Whether or not the pause worked, so a resume after Nova talks never undoes it.
            player.cancel_resume()
        elif action == "play":
            # Without the offer to download anything: nobody is listening for a yes.
            self.actions.play_without_offer(None)
        elif action == "next":
            player.skip()
        elif action == "previous":
            player.previous()

    def _playlist_from_home_assistant(self, name):
        """
        Plays one of the user's playlists for Home Assistant's playlist choice, through
        the actions, as saying its name does. Which playlist isn't logged.

        Args:
            name: The playlist's name, as the player listed it.
        """
        logger.info("Playlist from Home Assistant")
        self.actions.play_playlist(name)

    def stop_what_rings_and_speaks(self):
        """
        Stop, from Home Assistant or the settings page: silences anything ringing on
        the satellite, and stops Nova talking. What rings is dismissed, never deleted,
        so a repeating alarm rings again tomorrow - and the music plays on.
        """
        if self.brain.clock.is_ringing(satellite=self.satellite.name):
            self.brain.clock.dismiss(satellite=self.satellite.name)
        # Counted first, so an announcement whose sound this stops sees it.
        self._stops += 1
        self.speaker.stop(["reply", "announcement"])

    def _speak_from_home_assistant(self, message):
        """
        Says a message from Home Assistant - an automation's "the washing's done" -
        once any conversation has finished, as a timer is, muted or not. Handed to a
        thread of its own, since Home Assistant's commands are carried out one at a
        time, and a Stop pressed meanwhile mustn't wait until this has been said.

        Args:
            message: What to say.
        """
        threading.Thread(target=self.announce, args=(message,), daemon=True, name="ha-speak").start()

    def _say_from_home_assistant(self, message):
        """
        Says what was typed into the Nova view's Say on Nova box, as Speak does but with
        no sound first: the person typing it knows it's coming, and a chime before every
        line typed just sounds like a notification. Automations keep Speak and its sound.

        Args:
            message: What to say.
        """
        threading.Thread(target=self.announce, args=(message,), kwargs={"sound": None},
                         daemon=True, name="ha-say").start()

    def _add_timer_from_home_assistant(self, text: str):
        """
        Sets a timer on this satellite from the Nova view's Add timer box, silently - the
        page shows it, so nothing is said. Something which isn't a length of time is logged
        and left. It rings on this satellite, whose view it was set from, never handed on as
        a spoken one can be (see Brain.ring_on): the box belongs to this satellite's device.

        Args:
            text: What was typed or tapped: "10" (minutes), "1:20", "1h 20m", "90 min".
        """
        seconds = timer_seconds(text)
        if seconds is None:
            logger.warning("Home Assistant's Add timer wasn't a length of time")
            return
        result = self.brain.clock.set_timer(duration_seconds=seconds, satellite=self.satellite.name)
        if not (result or {}).get("success"):
            logger.warning(f"Add timer from Home Assistant: {(result or {}).get('message')}")

    def _add_alarm_from_home_assistant(self, text: str):
        """
        Sets a one-off alarm on this satellite from the Nova view's Add alarm box, silently.
        Repeating alarms are still set by voice or on the settings page.

        Args:
            text: The time, "07:00" or "7:00", as the view's time picker sends it.
        """
        found = re.fullmatch(r"\s*(\d{1,2}):(\d\d)(?::\d\d)?\s*", text or "")
        if not found or int(found[1]) > 23 or int(found[2]) > 59:
            logger.warning("Home Assistant's Add alarm wasn't a time")
            return
        result = self.brain.clock.set_alarm(int(found[1]), int(found[2]), None, satellite=self.satellite.name)
        if not (result or {}).get("success"):
            logger.warning(f"Add alarm from Home Assistant: {(result or {}).get('message')}")

    def _room(self) -> Optional[Tuple[Optional[str], Optional[str]]]:
        """
        Returns:
            This satellite's room and device in Home Assistant, as (area, device id) - or
            None with no media watcher, as nothing else sent to the satellite depends on them.
        """
        if self.brain.media is None:
            return None
        return self.home.area, self.home.device_id

    def _look_up_device(self):
        """
        Keeps up with which device in Home Assistant is this satellite's, and the room
        it's in - which Assist goes by for "turn on the lights", and what's playing here
        by, so the satellite is sent its state again when either changes - until the
        session closes. HomeAssistant.find_device logs anything that goes wrong.
        """
        while True:
            before = self._room()
            try:
                found = self.home.find_device(self.device.identifier)
            except Exception as e:
                logger.error(f"Couldn't look for the {self.satellite.name} satellite's device in Home Assistant: {e}")
                found = False
            # Kept, so the brain knows which satellites share a room - and still does
            # once this one has gone, or after a restart.
            self.brain.rooms.set_area(self.satellite.name, self.home.area)
            if self._room() != before:
                # What's playing here depends on the room, and nothing else would tell the satellite: a film
                # already on when the brain restarts doesn't change what's playing, only the room found after.
                try:
                    self.send_state()
                except Exception:
                    logger.exception(f"The {self.satellite.name} satellite couldn't be sent its state for its room")
            if self._stop_looking_up.wait(DEVICE_LOOKUP_FOUND if found else DEVICE_LOOKUP):
                return
