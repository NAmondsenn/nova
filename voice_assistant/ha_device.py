"""
Home Assistant Device Module.

Nova as a device of its own in Home Assistant, the way the Windows PC already is
through HASS.Agent: announced to Home Assistant's MQTT broker - the Mosquitto add-on -
through MQTT discovery, with eleven controls: what Nova's doing, its three volumes,
the next alarm, the timers running, Stop, Restart, Speak for automations ("the
washing's done"), whether it's reaching its backend, and a microphone mute. A device
which has a player also shows its music, and one which can be asked things has an Ask
Nova box to type into, and a Say on Nova box whose words it says out loud, with no
sound first. Its room is whichever area it's given in Home Assistant - see
docs/decisions/0005.

Home Assistant is told how Nova is set and what it's doing, and nothing else - never
anything said to it, nor its answers. What's typed into Ask Nova or Say on Nova comes in
on its command topic only: each box's state topic only ever says a fixed placeholder, which its
discovery turns into an empty state, and the text is logged only as heard text is. The
whole device shows as unavailable while Nova isn't running: the broker says "offline"
for it if its connection drops.

It logs in as a Home Assistant user of its own, with MQTT_PASSWORD from
~/.config/nova/.env. paho-mqtt does the talking, and is only imported once it's
needed, so Nova runs without it until mqtt.host is set.
"""

import json
import logging
import math
import queue
import re
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple, Union

from voice_assistant.logging_setup import TRANSCRIPT

# Logger setup
logger = logging.getLogger(__name__)

# Where Home Assistant looks for devices announcing themselves, and where it says
# it's back after a restart - when the announcement is sent again.
DISCOVERY_PREFIX = "homeassistant"
HA_STATUS = "homeassistant/status"

# Home Assistant's own words for each state, and "None" for one which isn't known -
# a backend that isn't set up, or no alarm.
ONLINE, OFFLINE = "online", "offline"
ON, OFF = "ON", "OFF"
UNKNOWN = "None"

# What the Ask Nova box's state topic says, always. A retained message with no payload
# is how the broker is told to forget one, so an empty state could never be kept for
# Home Assistant to read after a restart; this is, and the box's value template turns
# it into the empty state it shows.
ASK_PLACEHOLDER = "-"
# The same, doubled - what a box's state topic says after every other thing typed into it,
# which its value template turns into a zero-width space. Home Assistant only redraws a box
# when its state changes, so a box whose state were always empty would keep showing what
# was just sent until the page was reloaded; flipping between "" and the zero-width space
# clears it. Not a plain space: Home Assistant strips whitespace from what a template gives.
ASK_FLIPPED = "--"
ZERO_WIDTH = "\u200b"
BOX_TEMPLATE = "{{ '" + ZERO_WIDTH + "' if value == '--' else '' }}"

# How many playlists get a tile of their own - a play button and a cover - on the Nova view.
PLAYLIST_TILES = 12

# What Nova can be doing, as each satellite's session has it - see session.py.
STATUSES = ("idle", "listening", "thinking", "speaking")

# Nova's volumes, each with its control's id, its name in Home Assistant and its icon.
VOLUMES = (("general", "volume", "Volume", "mdi:volume-high"),
           ("alarm", "alarm_volume", "Alarm volume", "mdi:alarm"),
           ("music", "music_volume", "Music volume", "mdi:music"))

# How often what Nova's doing is looked at, in seconds. Only what's changed is sent.
PUBLISH_EVERY = 1

# How often what Nova's playing is looked at, in seconds, on a thread of its own - never
# the state thread's, which must stay quick - and how often the playlists on offer are
# read again. The cover is fetched only when the song's cover changes.
MUSIC_EVERY = 5
PLAYLISTS_EVERY = 600

# What the cover's content type is until a picture says otherwise - what the covers are,
# as in music_device.DEFAULT_TYPE.
DEFAULT_COVER_TYPE = "image/jpeg"

# Everything's sent until the broker says it has it, rather than once and hoped for -
# a Stop pressed in Home Assistant mustn't go missing.
QOS = 1


@dataclass
class DeviceControls:
    """
    What Home Assistant can see and do of Nova, each a function a satellite's session
    gives it.

    The ones which read how Nova is - status, volumes, clock, backend and muted - are
    called every second on the device's own state thread, and on paho's as it connects,
    so they must return at once. The commands - set_volume, set_muted, stop, restart
    and speak - are carried out one at a time, in the order Home Assistant sent them,
    on a thread of their own. A slow one holds up only the commands behind it, never
    the connection - but it does hold them up, so a speak which waited until the words
    had been said would keep a Stop pressed meanwhile waiting too.

    The music ones - now_playing, cover and playlists - are never called by the state
    thread, since the player may take its time: they're read on a thread of their own,
    every MUSIC_EVERY seconds, and the playlists every PLAYLISTS_EVERY. Each is None
    for a Nova with no player, and the music is then left out of Home Assistant.
    """
    # What Nova's doing: one of STATUSES.
    status: Callable[[], str]
    # Each volume's level, as a percentage, by its category - "general", "alarm" and "music".
    volumes: Callable[[], Dict[str, int]]
    set_volume: Callable[[str, int], None]
    # The timers and alarms set, as Clock.overview has them.
    clock: Callable[[], Dict]
    # Whether Nova's reaching its backend, or None if it doesn't use one.
    backend: Callable[[], Optional[bool]]
    muted: Callable[[], bool]
    set_muted: Callable[[bool], None]
    # What "Hey Nova, stop" does: silences anything ringing, and stops Nova talking.
    stop: Callable[[], None]
    restart: Callable[[], None]
    # Says a message out loud, once any conversation has finished.
    speak: Callable[[str], None]
    # The music, which is only shown when now_playing is set: what the player's playing
    # now, as its now_playing() has it - track, artist, album, playing, position, duration
    # and cover - or None when nothing is.
    now_playing: Optional[Callable[[], Optional[Dict]]] = None
    # The picture for the cover id now_playing gave, with its content type - or None.
    cover: Optional[Callable[[], Optional[Tuple[bytes, str]]]] = None
    # The names of the playlists which can be played - a network call, so it's only made
    # on the music thread, never as the device is announced.
    playlists: Optional[Callable[[], List[str]]] = None
    # "play_pause", "next" or "previous", for the player.
    playback: Optional[Callable[[str], None]] = None
    # Plays the playlist of that name.
    play_playlist: Optional[Callable[[str], None]] = None
    # Puts what was typed into Ask Nova to Nova, as if it had been said - the answer is
    # spoken, never sent back. Only shown when set.
    ask: Optional[Callable[[str], None]] = None
    # Says out loud what was typed into Say on Nova, word for word and with no sound
    # first - the person typing knows it's coming. Only shown when set.
    say: Optional[Callable[[str], None]] = None
    # Sets a timer, silently, from what was typed into Add timer - "10", "1:20", "1h 20m".
    add_timer: Optional[Callable[[str], None]] = None
    # Sets a one-off alarm, silently, from what was typed into Add alarm - "07:00".
    add_alarm: Optional[Callable[[str], None]] = None
    # The covers of the playlists named, by name: each picture with its content type, or None
    # for one which has none. One left out couldn't be read. Network calls, so only made on
    # the music thread.
    playlist_covers: Optional[Callable[[List[str]], Dict[str, Optional[Tuple[bytes, str]]]]] = None


def _slug(text: str) -> str:
    """An MQTT id made from text - "Nova Kitchen_kitchen-pi" as "nova_kitchen_kitchen_pi"."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _time(timestamp: float) -> str:
    """A time as Home Assistant's timestamp sensors take it - with its time zone."""
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def _percent(payload: str) -> Optional[int]:
    """A volume Home Assistant sent, between 0 and 100, or None if it isn't a number."""
    try:
        value = float(payload)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return max(0, min(100, round(value)))


def _paho_client(client_id: str):
    """A paho-mqtt client, with the callbacks of its version 2."""
    # Imported only here, so Nova - and its tests - run without paho-mqtt until MQTT is
    # set up.
    import paho.mqtt.client as mqtt
    return mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)


class NovaDevice:
    """Nova's device in Home Assistant, kept up to date over MQTT."""

    def __init__(self, host: str, port: int, username: str, password: str, controls: DeviceControls,
                 name: str = "Nova", node_id: Optional[str] = None, client_factory=None,
                 hostname: Optional[str] = None):
        """
        Args:
            host: Home Assistant's broker - its own address on the home network, e.g.
                "192.168.1.20". Its web address only passes on web requests.
            port: The broker's port, MQTT's usual 1883.
            username: Nova's Home Assistant user, or nothing for a broker which lets
                anyone in.
            password: MQTT_PASSWORD.
            controls: What Home Assistant sees and does of Nova.
            name: What the device is called in Home Assistant.
            node_id: Its MQTT id - from name and the machine's own name unless given,
                so a second Nova is a second device rather than taking over the first.
            client_factory: Makes the MQTT client from its client id - only replaced by
                the tests.
            hostname: The machine whose name goes in the id, if it isn't this one - a
                satellite's, for the device the brain runs for it.
        """
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.controls = controls
        self.name = name
        self.node_id = node_id or _slug(f"{name}_{(hostname or socket.gethostname()).split('.')[0]}")
        self._client_factory = client_factory

        base = f"nova/{self.node_id}"
        self._availability = f"{base}/availability"
        self._status = f"{base}/status"
        self._volume = {category: f"{base}/volume/{category}" for category, _, _, _ in VOLUMES}
        self._next_alarm = f"{base}/next_alarm"
        self._timers = f"{base}/timers"
        self._timers_attributes = f"{base}/timers/attributes"
        self._backend = f"{base}/backend"
        self._muted = f"{base}/muted"
        self._stop, self._restart, self._speak = f"{base}/stop", f"{base}/restart", f"{base}/speak"
        self._playing = f"{base}/playing"
        self._playing_attributes = f"{base}/playing/attributes"
        self._playing_cover = f"{base}/playing/cover"
        self._playback = {action: f"{base}/playing/{action}" for action in ("play_pause", "next", "previous")}
        self._playlist = f"{base}/playlist"
        self._playlist_set = f"{base}/playlist/set"
        self._ask = f"{base}/ask"
        self._ask_set = f"{base}/ask/set"
        self._say = f"{base}/say"
        self._say_set = f"{base}/say/set"
        self._add_timer, self._add_alarm = f"{base}/add_timer", f"{base}/add_alarm"
        # Which text boxes show their flipped placeholder now - see ASK_FLIPPED.
        self._flipped: Dict[str, bool] = {"ask": False, "say": False, "add_timer": False, "add_alarm": False}
        # Each playlist tile's play button and cover, by its number from 1.
        self._tile_play = {n: f"{base}/playlist/{n}/play" for n in range(1, PLAYLIST_TILES + 1)}
        self._tile_cover = {n: f"{base}/playlist/{n}/cover" for n in range(1, PLAYLIST_TILES + 1)}
        # The tiles' covers as last read, by playlist name, and each cover's content type as announced.
        self._tile_pictures: Dict[str, Tuple[bytes, str]] = {}
        self._announced_tile_types: List[str] = []
        # How many tiles Home Assistant was last told of, so ones gone since can be removed.
        self._announced_tile_count = 0
        self._discovery_topic = f"{DISCOVERY_PREFIX}/device/{self.node_id}/config"

        self._client = None
        self._connected = False
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Home Assistant's commands, waiting their turn - see _queue().
        self._commands = queue.Queue()
        # What each state topic was last sent, so only what's changed is sent again.
        self._lock = threading.Lock()
        self._announcing = threading.Lock()
        self._published: Dict[str, Union[str, bytes]] = {}
        # Whether a problem has been logged since things last worked, so an outage is
        # logged once rather than at every try.
        self._complained = False
        self._state_failing = False
        # The music is read on its own thread, one read at a time. The playlists are those
        # it last read, which is all the announcement ever uses - announcing runs on
        # paho's thread, and reading them is a network call.
        self._music_lock = threading.Lock()
        self._playlists: List[str] = []
        # The cover is fetched once for each cover id, and kept until the id changes. Its
        # content type is the latest picture's, and the announcement says what it was
        # announced with, so a change is noticed.
        self._cover_id: Optional[str] = None
        self._cover = b""
        self._cover_type = DEFAULT_COVER_TYPE
        self._announced_cover_type = DEFAULT_COVER_TYPE
        # The playlists the announcement had, which may be the list before the music thread
        # first read them if the broker answered first - so it's announced again once read.
        self._announced_playlists: List[str] = []
        self._music_failing = False
        self._music_loop_failing = False
        self._playlists_failing = False

    @classmethod
    def from_config(cls, config, password: Optional[str], controls: DeviceControls,
                    hostname: Optional[str] = None) -> Optional["NovaDevice"]:
        """
        The device mqtt.host in config.yaml sets up, or None if it doesn't name a broker.

        Args:
            config: Configs loaded from config.yaml.
            password: MQTT_PASSWORD.
            controls: What Home Assistant sees and does of Nova.
            hostname: The satellite's own hostname, which names the device in place of
                this machine's - so the Pi keeps the device it had, and with it its room.
        """
        settings = config.section("mqtt")
        host = (settings.get("host") or "").strip()
        if not host:
            return None
        if not password:
            logger.warning("mqtt.host is set but MQTT_PASSWORD isn't, in ~/.config/nova/.env, "
                           "so Home Assistant's broker will most likely turn Nova away")
        return cls(host, int(settings.get("port") or 1883), (settings.get("username") or "").strip(),
                   password or "", controls, name=(settings.get("name") or "").strip() or "Nova",
                   hostname=hostname)

    @property
    def identifier(self) -> str:
        """What Home Assistant knows Nova's device by, among its MQTT identifiers."""
        return self.node_id

    def discovery(self) -> Dict:
        """
        The announcement which makes Nova a device in Home Assistant, with its controls.

        Returns:
            Home Assistant's device-based discovery message, with each control by its id.
        """
        controls = {
            "status": {"p": "sensor", "name": "Status", "state_topic": self._status, "device_class": "enum",
                       "options": list(STATUSES), "icon": "mdi:account-voice"},
        }
        for category, control, name, icon in VOLUMES:
            controls[control] = {"p": "number", "name": name, "state_topic": self._volume[category],
                                 "command_topic": f"{self._volume[category]}/set", "min": 0, "max": 100, "step": 1,
                                 "mode": "slider", "unit_of_measurement": "%", "icon": icon}
        controls.update({
            "next_alarm": {"p": "sensor", "name": "Next alarm", "state_topic": self._next_alarm,
                           "device_class": "timestamp", "icon": "mdi:alarm"},
            "timers": {"p": "sensor", "name": "Timers", "state_topic": self._timers,
                       "json_attributes_topic": self._timers_attributes, "icon": "mdi:timer-outline"},
            "stop": {"p": "button", "name": "Stop", "command_topic": self._stop, "icon": "mdi:stop"},
            "restart": {"p": "button", "name": "Restart", "command_topic": self._restart, "device_class": "restart",
                        "entity_category": "config"},
            "speak": {"p": "notify", "name": "Speak", "command_topic": self._speak},
            "backend": {"p": "binary_sensor", "name": "Backend", "state_topic": self._backend,
                        "device_class": "connectivity", "payload_on": ON, "payload_off": OFF,
                        "entity_category": "diagnostic"},
            "muted": {"p": "switch", "name": "Microphone muted", "state_topic": self._muted,
                      "command_topic": f"{self._muted}/set", "payload_on": ON, "payload_off": OFF,
                      "icon": "mdi:microphone-off"},
        })
        if self._has_music():
            controls.update(self._music_discovery())
        if self._has_ask():
            # A box to type into. Its state topic only ever says ASK_PLACEHOLDER, which
            # the value template turns into "": the box is always empty, so what was typed
            # is neither shown nor kept by Home Assistant - see _state.
            controls["ask"] = {"p": "text", "name": "Ask Nova", "command_topic": self._ask_set,
                               "state_topic": self._ask, "value_template": BOX_TEMPLATE, "max": 255,
                               "icon": "mdi:keyboard-outline",
                               "default_entity_id": f"text.{_slug(self.name)}_ask"}
        if self._has_say():
            # The same kind of box as Ask Nova, kept as empty the same way.
            controls["say"] = {"p": "text", "name": "Say on Nova", "command_topic": self._say_set,
                               "state_topic": self._say, "value_template": BOX_TEMPLATE, "max": 255,
                               "icon": "mdi:bullhorn-outline",
                               "default_entity_id": f"text.{_slug(self.name)}_say"}
        if self._has_clock_boxes():
            # Timers and alarms set from the Nova view, silently: the page shows them.
            for key, name, icon in (("add_timer", "Add timer", "mdi:timer-plus-outline"),
                                    ("add_alarm", "Add alarm", "mdi:alarm-plus")):
                topic = getattr(self, f"_{key}")
                controls[key] = {"p": "text", "name": name, "command_topic": f"{topic}/set", "state_topic": topic,
                                 "value_template": BOX_TEMPLATE, "max": 40, "icon": icon,
                                 "default_entity_id": f"text.{_slug(self.name)}_{key}"}
        for control, settings in controls.items():
            # A component sent as its platform alone is being removed, and must stay so.
            if len(settings) == 1:
                continue
            settings["unique_id"] = f"{self.node_id}_{control}"
            settings["qos"] = QOS
        return {"dev": {"ids": [self.node_id], "name": self.name, "mf": "Nova", "mdl": "Raspberry Pi"},
                "o": {"name": "Nova"},
                "availability_topic": self._availability,
                "cmps": controls}

    def _has_music(self) -> bool:
        """Whether Nova has a player to show - a device with no controls, like the brain's, has none."""
        return self.controls is not None and self.controls.now_playing is not None

    def _has_ask(self) -> bool:
        """Whether Nova can be asked things from Home Assistant - the brain's usage device can't."""
        return self.controls is not None and self.controls.ask is not None

    def _has_clock_boxes(self) -> bool:
        """Whether timers and alarms can be added from Home Assistant."""
        return self.controls is not None and self.controls.add_timer is not None and self.controls.add_alarm is not None

    def _box_state(self, key: str) -> str:
        """What a text box's state topic says now: its placeholder, flipped or not."""
        return ASK_FLIPPED if self._flipped[key] else ASK_PLACEHOLDER

    def _sent_from_box(self, key: str):
        """Flips a box's placeholder once something typed into it has been taken, so Home Assistant clears it."""
        self._flipped[key] = not self._flipped[key]
        self.publish_state()

    def _has_say(self) -> bool:
        """Whether Nova can be given words to say from Home Assistant's Say on Nova box."""
        return self.controls is not None and self.controls.say is not None

    def _music_discovery(self) -> Dict[str, Dict]:
        """
        The music's controls: what's playing, its cover, the three buttons and, if any
        playlists were read, the playlist to play. Each has its entity id set here, from the
        device's name, since the Nova view is built on them - and none is "nova_music_...",
        which is the Music device's.
        """
        slug = _slug(self.name)
        # Each read once, so what's announced is what's recorded as announced even if the
        # music thread changes them meanwhile.
        cover_type, playlists = self._cover_type, list(self._playlists)
        controls = {
            "playing": {"p": "sensor", "name": "Playing", "state_topic": self._playing,
                        "json_attributes_topic": self._playing_attributes, "device_class": "enum",
                        "options": ["playing", "paused", "idle"], "icon": "mdi:music",
                        "default_entity_id": f"sensor.{slug}_playing"},
            "cover": {"p": "image", "name": "Cover", "image_topic": self._playing_cover,
                      "content_type": cover_type, "default_entity_id": f"image.{slug}_playing_cover"},
            "play_pause": {"p": "button", "name": "Play or pause", "command_topic": self._playback["play_pause"],
                           "icon": "mdi:play-pause", "default_entity_id": f"button.{slug}_play_pause"},
            "next_track": {"p": "button", "name": "Next track", "command_topic": self._playback["next"],
                           "icon": "mdi:skip-next", "default_entity_id": f"button.{slug}_next_track"},
            "previous_track": {"p": "button", "name": "Previous track",
                               "command_topic": self._playback["previous"], "icon": "mdi:skip-previous",
                               "default_entity_id": f"button.{slug}_previous_track"},
        }
        if playlists:
            # Always "None" on its state topic, which Home Assistant takes as no option
            # chosen: which playlist was last chosen says nothing of what's playing, so the
            # select is a way to pick one and no more - and the same one can be picked twice
            # in a row, as the frontend ignores choosing the option a select already has.
            controls["playlist"] = {"p": "select", "name": "Playlist", "command_topic": self._playlist_set,
                                    "state_topic": self._playlist, "options": playlists,
                                    "icon": "mdi:playlist-music", "default_entity_id": f"select.{slug}_playlist"}
        tile_types = [self._tile_pictures.get(name, (b"", DEFAULT_COVER_TYPE))[1] for name in playlists[:PLAYLIST_TILES]]
        for n, (name, kind) in enumerate(zip(playlists, tile_types), start=1):
            # A tile per playlist: its cover, and a button which plays it - both named after
            # the playlist, so the view shows its name without knowing the list.
            # The button sends the playlist's own name, so a press plays what its tile was
            # named after even if the list has moved on since Home Assistant was told it.
            controls[f"playlist_{n}"] = {"p": "button", "name": name, "command_topic": self._tile_play[n],
                                         "payload_press": name, "icon": "mdi:playlist-play",
                                         "default_entity_id": f"button.{slug}_playlist_{n}"}
            controls[f"playlist_{n}_cover"] = {"p": "image", "name": name, "image_topic": self._tile_cover[n],
                                               "content_type": kind,
                                               "default_entity_id": f"image.{slug}_playlist_{n}"}
        # Tiles there were before and aren't now: Home Assistant only removes a component of a
        # device which it's sent as its platform alone, once, before it's left out.
        for n in range(len(tile_types) + 1, self._announced_tile_count + 1):
            controls[f"playlist_{n}"] = {"p": "button"}
            controls[f"playlist_{n}_cover"] = {"p": "image"}
        self._announced_cover_type = cover_type
        self._announced_playlists = playlists
        self._announced_tile_types = tile_types
        self._announced_tile_count = len(tile_types)
        return controls

    # Connecting

    def start(self):
        """
        Connects to the broker in the background, and keeps Home Assistant up to date
        from then on. A broker which can't be reached is tried again and again, without
        holding Nova up.
        """
        if self._client is not None:
            return
        try:
            client = (self._client_factory or _paho_client)(self.node_id)
        except ImportError as e:
            logger.error(f"Nova can't be in Home Assistant without paho-mqtt ({e}) - "
                         f"install requirements.txt again")
            return
        if self.username:
            client.username_pw_set(self.username, self.password or None)
        # Sent by the broker for Nova if its connection drops, so the device shows as
        # unavailable rather than idle when Nova's gone.
        client.will_set(self._availability, OFFLINE, qos=QOS, retain=True)
        client.on_connect = self._on_connect
        client.on_connect_fail = self._on_connect_fail
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        # Kept before connecting, since a broker close by can answer before
        # loop_start() has even returned, and the announcement goes through it.
        self._client = client
        try:
            client.connect_async(self.host, self.port)
        except (ValueError, OSError) as e:
            # paho checks the address before it tries in the background - a port of 0
            # or below, say - so trying again wouldn't help, and Nova carries on without
            # its device rather than stopping as it starts.
            logger.error(f"Nova can't be in Home Assistant with its broker at {self.host}:{self.port} ({e}) - "
                         f"mqtt in config.yaml needs checking")
            self._client = None
            return
        # paho's own thread, which connects - and connects again whenever the
        # connection's lost.
        client.loop_start()
        self._thread = threading.Thread(target=self._keep_publishing, name="ha-device", daemon=True)
        self._thread.start()
        threading.Thread(target=self._carry_out, name="ha-device-commands", daemon=True).start()
        if self._has_music():
            threading.Thread(target=self._keep_music, name="ha-device-music", daemon=True).start()

    def stop(self):
        """Says Nova's offline, and disconnects."""
        self._stopping.set()
        client, self._client = self._client, None
        if client is None:
            return
        # Ends the commands' thread once it's carried out those already waiting - not
        # waited for, since one may be a message still to be said.
        self._commands.put(None)
        connected, self._connected = self._connected, False
        try:
            # The broker only sends the last will for a connection which drops, not
            # one closed like this.
            if connected:
                client.publish(self._availability, OFFLINE, qos=QOS, retain=True)
            client.disconnect()
            # Waits for "offline" to be sent. Without a connection there's nothing to
            # wait for, and waiting could take up to paho's 5 seconds for a broker which
            # isn't answering - its thread finishes by itself once disconnected.
            if connected:
                client.loop_stop()
        except Exception as e:
            logger.warning(f"Couldn't disconnect from Home Assistant's broker cleanly: {e}")

    def _complain(self, message: str):
        """Logs a problem with the connection, once until it's working again."""
        if not self._complained:
            logger.warning(message)
        self._complained = True

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        """paho's callback for the broker answering a connection - accepted or turned away."""
        try:
            # Too late - "offline" may already have been sent.
            if self._stopping.is_set():
                return
            if reason_code.is_failure:
                self._complain(f"Home Assistant's broker turned Nova away ({reason_code}), so trying again")
                return
            logger.info(f"Nova's in Home Assistant, through its broker at {self.host}")
            self._complained = False
            client.subscribe(self._subscriptions())
            self._announce()
            # Everything's sent again, since the broker may have restarted and lost
            # what it was keeping - and only then is Nova shown as there.
            with self._lock:
                self._published.clear()
                self._connected = True
            self.publish_state()
            client.publish(self._availability, ONLINE, qos=QOS, retain=True)
        except Exception as e:
            logger.error(f"Couldn't set Nova up in Home Assistant: {e}")

    def _on_connect_fail(self, client, userdata):
        """paho's callback for a broker which couldn't be reached at all."""
        self._complain(f"Couldn't reach Home Assistant's broker at {self.host}:{self.port}, so trying again")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        """paho's callback for the connection ending - lost, or closed by stop()."""
        self._connected = False
        if not self._stopping.is_set():
            self._complain(f"Lost Home Assistant's broker ({reason_code}), so trying again")

    def _subscriptions(self) -> List[Tuple[str, int]]:
        """
        The topics to listen on once connected, each with its QoS: Home Assistant's
        commands for each control, and its saying it's back after a restart. A device
        with no commands of its own, like the brain's usage one, replaces this.
        """
        topics = ([(f"{self._volume[category]}/set", QOS) for category, _, _, _ in VOLUMES] +
                  [(f"{self._muted}/set", QOS), (self._stop, QOS), (self._restart, QOS),
                   (self._speak, QOS), (HA_STATUS, QOS)])
        if self._has_music():
            topics += [(topic, QOS) for topic in (*self._playback.values(), self._playlist_set)]
        if self._has_ask():
            topics.append((self._ask_set, QOS))
        if self._has_say():
            topics.append((self._say_set, QOS))
        if self._has_clock_boxes():
            topics += [(f"{self._add_timer}/set", QOS), (f"{self._add_alarm}/set", QOS)]
        if self._has_music():
            topics += [(topic, QOS) for topic in self._tile_play.values()]
        return topics

    def _announce(self):
        """
        Tells Home Assistant about Nova's device and its controls. One at a time: paho's
        thread and the music thread both announce, and an older announcement sent after a
        newer one would bring back tiles the newer one removed.
        """
        client = self._client
        if client is not None:
            with self._announcing:
                client.publish(self._discovery_topic, json.dumps(self.discovery()), qos=QOS, retain=True)

    # Nova's state, to Home Assistant

    def _state(self) -> Dict[str, str]:
        """What each state topic should say now."""
        controls = self.controls
        status = controls.status()
        state = {self._status: status if status in STATUSES else UNKNOWN}
        volumes = controls.volumes()
        for category, _, _, _ in VOLUMES:
            if category in volumes:
                state[self._volume[category]] = str(round(volumes[category]))
        overview = controls.clock()
        alarms = overview.get("alarms") or []
        state[self._next_alarm] = _time(min(alarm["due_at"] for alarm in alarms)) if alarms else UNKNOWN
        timers = overview.get("timers") or []
        state[self._timers] = str(len(timers))
        # When each one ends, and not its name: a reminder's is the user's own words -
        # "take my tablets reminder" - which would be kept by the broker and in Home
        # Assistant's history. Home Assistant takes attributes only as a JSON object,
        # so the list is in one.
        state[self._timers_attributes] = json.dumps({"timers": [{"ends": _time(timer["due_at"])} for timer in timers]})
        backend = controls.backend()
        state[self._backend] = UNKNOWN if backend is None else (ON if backend else OFF)
        state[self._muted] = ON if controls.muted() else OFF
        if self._has_ask():
            # Always the placeholder, which the box shows as empty: the box is only for
            # sending, and what was typed on it - which is what the user said - is never
            # sent back to be shown or kept. Not "", which the broker would take as
            # forgetting the message, leaving the box "unknown" after Home Assistant restarts.
            state[self._ask] = self._box_state("ask")
        if self._has_say():
            # Kept empty as Ask Nova's is, for the same reasons.
            state[self._say] = self._box_state("say")
        if self._has_clock_boxes():
            state[self._add_timer] = self._box_state("add_timer")
            state[self._add_alarm] = self._box_state("add_alarm")
        if self._has_music():
            # Always "None": see the playlist select in _music_discovery.
            state[self._playlist] = UNKNOWN
        return state

    def publish_state(self):
        """
        Sends Home Assistant whatever's changed since it was last sent - each kept by
        the broker, so Home Assistant has it however long ago that was.
        """
        client = self._client
        if client is None or not self._connected:
            return
        with self._lock:
            try:
                state = self._state()
            except Exception as e:
                if not self._state_failing:
                    logger.error(f"Couldn't tell Home Assistant how Nova is: {e}")
                self._state_failing = True
                return
            self._state_failing = False
            for topic, payload in state.items():
                if self._published.get(topic) != payload:
                    client.publish(topic, payload, qos=QOS, retain=True)
                    self._published[topic] = payload

    def _keep_publishing(self):
        """Keeps Home Assistant up to date, until stopped."""
        while not self._stopping.wait(PUBLISH_EVERY):
            self.publish_state()

    # What Nova's playing, to Home Assistant

    def refresh_playlists(self):
        """
        Reads the playlists on offer again, and announces the device again if they've
        changed - the music thread's job, at its start and every PLAYLISTS_EVERY seconds,
        since reading them is a network call. Home Assistant is only ever announced the
        list this last read.
        """
        if not self._has_music() or self.controls.playlists is None:
            return
        with self._music_lock:
            try:
                playlists = [str(name) for name in self.controls.playlists()]
            except Exception as e:
                if not self._playlists_failing:
                    logger.error(f"Couldn't read the playlists for Home Assistant: {e}")
                self._playlists_failing = True
                return
            self._playlists_failing = False
            # An empty list after a full one is what the player gives when it couldn't be
            # asked, so the old one is kept until a read which has some.
            if playlists or not self._playlists:
                self._playlists = playlists
            # Only after a read which worked: an empty one means the player couldn't be asked,
            # and the covers wouldn't be had either.
            if playlists:
                self._read_tile_pictures()
        self._announce_if_stale()

    def _read_tile_pictures(self):
        """
        Reads the cover of each playlist with a tile, again at every refresh, since a
        playlist's cover follows what's in it. One which can't be had keeps the last.
        """
        if self.controls.playlist_covers is None:
            return
        names = self._playlists[:PLAYLIST_TILES]
        try:
            covers = self.controls.playlist_covers(list(names))
        except Exception as e:
            logger.warning(f"Couldn't read the playlists' covers for Home Assistant: {e}")
            return
        for name, picture in covers.items():
            if picture:
                self._tile_pictures[name] = (picture[0], picture[1] or DEFAULT_COVER_TYPE)
            else:
                # It has none now - unlike one which couldn't be read, which isn't in covers.
                self._tile_pictures.pop(name, None)
        # Only the playlists which have tiles are kept, so renamed or deleted ones go.
        for name in set(self._tile_pictures) - set(names):
            del self._tile_pictures[name]

    def _announce_if_stale(self):
        """Announces the device again if the playlists or the cover's type aren't what it was last announced with."""
        tile_types = [self._tile_pictures.get(name, (b"", DEFAULT_COVER_TYPE))[1]
                      for name in self._playlists[:PLAYLIST_TILES]]
        if self._connected and (self._playlists != self._announced_playlists
                                or self._cover_type != self._announced_cover_type
                                or tile_types != self._announced_tile_types):
            self._announce()

    def _send(self, topic: str, payload):
        """Sends one topic, kept by the broker, unless it's what was last sent."""
        client = self._client
        if client is None:
            return
        with self._lock:
            if self._published.get(topic) != payload:
                client.publish(topic, payload, qos=QOS, retain=True)
                self._published[topic] = payload

    def publish_music(self):
        """
        Sends Home Assistant what Nova's playing, whatever's changed: whether it's playing,
        paused or nothing, the track, artist, album and length, and its cover - fetched
        once for each cover id rather than every time. The position isn't sent, since it
        would change every time. Never called by the state thread.
        """
        if not self._has_music() or self._client is None or not self._connected:
            return
        with self._music_lock:
            try:
                playing = self.controls.now_playing()
                # Spotify's reports the account's playback wherever it is, with "here" saying
                # whether it's on this satellite; the player's own, like Navidrome's, has no
                # such key and is always here. Music elsewhere is as good as none.
                if playing and not playing.get("here", True):
                    playing = None
                if playing:
                    state = "playing" if playing.get("playing") else "paused"
                    attributes = {"track": playing.get("track"), "artist": playing.get("artist"),
                                  "album": playing.get("album"), "duration": playing.get("duration")}
                    self._read_cover(playing.get("cover"))
                else:
                    state, attributes = "idle", {}
                    self._read_cover(None)
            except Exception as e:
                if not self._music_failing:
                    logger.error(f"Couldn't tell Home Assistant what Nova's playing: {e}")
                self._music_failing = True
                # The tiles' covers don't depend on what's playing, so they're still sent.
                self._send_tiles()
                return
            self._music_failing = False
            self._send(self._playing, state)
            self._send(self._playing_attributes, json.dumps(attributes))
            # Nothing is sent as an empty payload, so Home Assistant shows no picture
            # rather than the last song's.
            self._send(self._playing_cover, self._cover)
            self._send_tiles()
        # The type is only known from the picture, which comes after the announcement.
        self._announce_if_stale()

    def _send_tiles(self):
        """
        Sends each tile's cover - unchanged ones aren't sent again, so this costs nothing
        between the refreshes which read them - and an empty picture for a tile number no
        playlist has now, so a removed tile's cover isn't kept by the broker.
        """
        names = self._playlists[:PLAYLIST_TILES]
        for n in range(1, PLAYLIST_TILES + 1):
            picture = self._tile_pictures.get(names[n - 1], (b"", DEFAULT_COVER_TYPE)) if n <= len(names) else (b"", "")
            self._send(self._tile_cover[n], picture[0])

    def _read_cover(self, cover_id):
        """Fetches the cover unless its id is the one already held, and keeps it with its type."""
        if cover_id == self._cover_id:
            return
        picture = self.controls.cover() if cover_id and self.controls.cover is not None else None
        self._cover = picture[0] if picture else b""
        # A picture which couldn't be had - the player gives none when it couldn't be asked - isn't
        # taken as done, so the next round asks again rather than the song going without one.
        self._cover_id = cover_id if picture or not cover_id else None
        if picture:
            self._cover_type = picture[1] or DEFAULT_COVER_TYPE

    def _keep_music(self):
        """Keeps what Nova's playing and the playlists up to date, on a thread of their own, until stopped."""
        self._music_step(self.refresh_playlists)
        playlists_due = time.monotonic() + PLAYLISTS_EVERY

        def round_():
            nonlocal playlists_due
            if time.monotonic() >= playlists_due:
                self.refresh_playlists()
                playlists_due = time.monotonic() + PLAYLISTS_EVERY
            self.publish_music()
            # Also when publishing failed or there was nothing to send: the playlists may
            # have been read after the announcement went out with the old ones.
            self._announce_if_stale()

        while not self._stopping.wait(MUSIC_EVERY):
            self._music_step(round_)

    def _music_step(self, step: Callable[[], None]):
        """
        Does one step of the music thread, so that nothing going wrong in it ends the
        thread - which would leave the music silently never updated again. A failure is
        logged once, until a step goes right.
        """
        try:
            step()
        except Exception as e:
            if not self._music_loop_failing:
                logger.error(f"Couldn't keep Home Assistant's music up to date: {e}")
            self._music_loop_failing = True
        else:
            self._music_loop_failing = False

    # Home Assistant's commands, to Nova

    def _on_message(self, client, userdata, message):
        """paho's callback for a message from Home Assistant - a command, or it being back."""
        try:
            # Without the zero-width space a box may still hold from its flipped state - see ASK_FLIPPED.
            payload = message.payload.decode("utf-8", "replace").replace(ZERO_WIDTH, "").strip()
            self._command(message.topic, payload, message.retain)
        except Exception as e:
            logger.error(f"Couldn't do what Home Assistant asked, on {message.topic}: {e}")

    def _command(self, topic: str, payload: str, retained: bool):
        """
        Checks one of Home Assistant's commands, and queues it to be carried out.

        Args:
            topic: Where it came in.
            payload: What it said.
            retained: Whether the broker had kept it, rather than it being sent just now.
        """
        if topic == HA_STATUS:
            if payload == ONLINE:
                self._announce()
            return
        # Home Assistant never has the broker keep a command, so one that's kept is left
        # over from something else - and would act every time Nova connected, which
        # for Restart would be over and over.
        if retained:
            return

        if topic == self._stop:
            logger.info("Stop, from Home Assistant")
            self._queue("Stop", self.controls.stop)
        elif topic == self._restart:
            logger.info("Restart, from Home Assistant")
            self._queue("Restart", self.controls.restart)
        elif topic == self._speak:
            if payload:
                logger.info(f"Home Assistant asked to say: '{payload}'", extra=TRANSCRIPT)
                self._queue("Speak", self.controls.speak, payload)
        elif self._has_ask() and topic == self._ask_set:
            if payload:
                # Logged as heard text is, so the transcripts setting decides if it's kept.
                logger.info(f"Home Assistant asked: '{payload}'", extra=TRANSCRIPT)
                self._queue("Ask Nova", self.controls.ask, payload)
                self._sent_from_box("ask")
        elif self._has_say() and topic == self._say_set:
            if payload:
                logger.info(f"Home Assistant asked to say aloud: '{payload}'", extra=TRANSCRIPT)
                self._queue("Say on Nova", self.controls.say, payload)
                self._sent_from_box("say")
        elif self._has_clock_boxes() and topic in (f"{self._add_timer}/set", f"{self._add_alarm}/set"):
            if payload:
                key = "add_timer" if topic.startswith(self._add_timer) else "add_alarm"
                logger.info(f"{'Timer' if key == 'add_timer' else 'Alarm'} '{payload}', from Home Assistant")
                self._queue("Add timer" if key == "add_timer" else "Add alarm", getattr(self.controls, key), payload)
                self._sent_from_box(key)
        elif self._has_music() and topic in self._tile_play.values():
            # The press carries the playlist's name; only one which is still offered is played.
            if payload in self._playlists and self.controls.play_playlist is not None:
                logger.info("Playing a playlist tile, from Home Assistant")
                self._queue("Playlist", self.controls.play_playlist, payload)
        elif topic == f"{self._muted}/set":
            if payload in (ON, OFF):
                logger.info(f"Microphone {'muted' if payload == ON else 'unmuted'} from Home Assistant")
                self._queue("Microphone muted", self._set_muted, payload == ON)
        elif self._has_music() and topic in self._playback.values():
            # A button's payload is only Home Assistant's "PRESS", so it isn't looked at.
            action = next(action for action, where in self._playback.items() if where == topic)
            if self.controls.playback is not None:
                logger.info(f"Music '{action}', from Home Assistant")
                self._queue("Music", self.controls.playback, action)
        elif self._has_music() and topic == self._playlist_set:
            # Only a playlist which was offered: the select sends whatever it's given, and
            # anything on the broker could make that up.
            if payload in self._playlists and self.controls.play_playlist is not None:
                logger.info("Playing a playlist, from Home Assistant")
                self._queue("Playlist", self.controls.play_playlist, payload)
        else:
            for category, _, name, _ in VOLUMES:
                if topic == f"{self._volume[category]}/set":
                    percent = _percent(payload)
                    if percent is not None:
                        self._queue(name, self._set_volume, category, percent)

    def _queue(self, what: str, action: Callable, *args):
        """
        Hands a command to the commands' own thread, rather than carrying it out on
        paho's, which keeps the connection alive - the music's volume goes through
        Spotify, which can take seconds.

        Args:
            what: The control it came from, as Home Assistant names it.
            action: What carries it out.
            *args: What action is given.
        """
        self._commands.put((what, action, args))

    def _carry_out(self):
        """Carries out Home Assistant's commands one at a time, in the order they came, until stopped."""
        while True:
            command = self._commands.get()
            if command is None:
                return
            what, action, args = command
            try:
                action(*args)
            except Exception as e:
                logger.error(f"Home Assistant's {what} failed: {e}")

    def _set_volume(self, category: str, percent: int):
        # Sent straight back, so Home Assistant's slider doesn't wait to catch up.
        self.controls.set_volume(category, percent)
        self.publish_state()

    def _set_muted(self, muted: bool):
        self.controls.set_muted(muted)
        self.publish_state()
