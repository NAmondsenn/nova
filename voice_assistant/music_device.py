"""
Music Device Module.

What the user has listened to, as a device of its own in Home Assistant: the brain's, like
the usage device, since the brain is the one machine which reads SoulSync's stats. It is
announced the same way - MQTT discovery, through the broker the Mosquitto add-on runs - and
is a NovaDevice in all but what it shows, so it connects, reconnects, says it's offline when
Nova stops and sends only what's changed in just the same way.

It has five sensors, all read from the listening snapshot (see listening.py): the plays
in each of SoulSync's four windows, with the top lists, the clock and the rest of that window
as attributes, and the library, with its health and size. And twenty images, the pictures
of each window's five top artists, which the Music view shows. Home Assistant can only read
them - there's nothing to press or set, so it listens for nothing but its own restart,
after which the announcement is sent again.

Only the snapshot is read, never SoulSync: the state is looked at every second, and the
snapshot is the stats as they were at the last refresh, which is what Nova says too, so
what's shown and what's said can't disagree.
"""

import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from voice_assistant.ha_device import HA_STATUS, QOS, UNKNOWN, NovaDevice
from voice_assistant.listening import Listening

# Logger setup
logger = logging.getLogger(__name__)

# The device's name, and so the start of each entity id in Home Assistant.
NAME = "Nova music"

# SoulSync's windows, each with its key in the snapshot, what it's called in a sensor's name and
# the word in its entity id. The entity ids are set here rather than left for Home Assistant to
# work out from the names: renaming one later would otherwise change it and leave its history
# behind, and the Music view is built on these.
WINDOWS = (("7d", "7 days", "7_days"), ("30d", "30 days", "30_days"),
           ("12m", "12 months", "12_months"), ("all", "all time", "all_time"))

# How many artists have a picture in each window, and how long the top lists and the genres are.
PICTURES_OF = 5
TOP_OF = 10
GENRES_OF = 5

# What an image's content type is until its picture says otherwise - what SoulSync's covers are.
DEFAULT_TYPE = "image/jpeg"

# Home Assistant's recorder drops a sensor's attributes over 16 KB, so they're kept under that,
# with some to spare. Only the timeline is long enough to need it.
ATTRIBUTES_LIMIT = 15 * 1024

# The five figures of a window's overview, by their name here and the key SoulSync gives each.
# The minutes are worked out from the time, not given.
FIGURES = (("plays", "total_plays"), ("minutes", "total_time_ms"), ("artists", "unique_artists"),
           ("albums", "unique_albums"), ("tracks", "unique_tracks"))


def _figures(overview) -> Dict[str, Optional[int]]:
    """The five figures of an overview, None for one it hasn't got."""
    overview = overview if isinstance(overview, dict) else {}
    figures = {name: overview.get(key) for name, key in FIGURES}
    if figures["minutes"] is not None:
        figures["minutes"] = round(figures["minutes"] / 60000)
    return figures


def _change(now: Dict, before: Dict) -> Dict[str, Optional[float]]:
    """Each figure's change since the window before, in percent - None where there's nothing to compare."""
    change = {}
    for name in now:
        current, previous = now[name], before.get(name)
        change[name] = None if current is None or not previous else round((current - previous) / previous * 100, 1)
    return change


def _top(entries) -> List[Dict]:
    """The first TOP_OF of a list of SoulSync's, with only what's shown: name, artist if it has one, plays."""
    top = []
    for entry in (entries or [])[:TOP_OF]:
        item = {"name": entry.get("name")}
        if entry.get("artist"):
            item["artist"] = entry["artist"]
        item["plays"] = entry.get("play_count")
        top.append(item)
    return top


def _fit(attributes: Dict) -> Tuple[Dict, int]:
    """
    The attributes, with the timeline cut from its oldest end until they're small enough to keep.

    Returns:
        The attributes, and their size in bytes - still over ATTRIBUTES_LIMIT if they're too big
        with no timeline at all, which is left as it is.
    """
    timeline = attributes.get("timeline") or []
    size = len(json.dumps(attributes).encode())
    while timeline and size > ATTRIBUTES_LIMIT:
        # Rounded up, so a timeline of one is emptied rather than left as it was.
        timeline = timeline[(len(timeline) + 1) // 2:]
        attributes = {**attributes, "timeline": timeline}
        size = len(json.dumps(attributes).encode())
    return attributes, size


class MusicDevice(NovaDevice):
    """The brain's device in Home Assistant, showing what the user has listened to."""

    def __init__(self, host: str, port: int, username: str, password: str, listening: Listening,
                 client_factory=None, hostname: Optional[str] = None):
        """
        Args:
            host: Home Assistant's broker - its own address on the home network.
            port: The broker's port, MQTT's usual 1883.
            username: Nova's Home Assistant user, or nothing for a broker which lets
                anyone in.
            password: MQTT_PASSWORD.
            listening: The listening stats, whose snapshot the sensors and images show.
            client_factory: Makes the MQTT client from its client id - only replaced by
                the tests.
            hostname: The machine whose name goes in the id, if it isn't this one.
        """
        # No controls: nothing about how Nova is, and nothing Home Assistant can do.
        super().__init__(host, port, username, password, None, name=NAME, client_factory=client_factory,
                         hostname=hostname)
        self.listening = listening
        base = f"nova/{self.node_id}"
        self._sensors = {f"plays_{window}": (f"Plays {words}", f"sensor.nova_music_plays_{slug}", "mdi:music-note")
                         for window, words, slug in WINDOWS}
        self._sensors["library"] = ("Library", "sensor.nova_music_library", "mdi:music-box-multiple")
        self._images = {f"artist_{window}_{n}": (f"Top artist {n}, {words}", f"image.nova_music_artist_{slug}_{n}")
                        for window, words, slug in WINDOWS for n in range(1, PICTURES_OF + 1)}
        self._topics = {control: f"{base}/{control}" for control in [*self._sensors, *self._images]}
        # Each image's content type as the latest picture of it says, which is what's announced.
        self._types = {control: DEFAULT_TYPE for control in self._images}
        self._announced_types = dict(self._types)
        # The windows whose attributes are too big even without a timeline, so each is logged once.
        self._oversize = set()

    @classmethod
    def from_config(cls, config, password: Optional[str], listening: Listening) -> Optional["MusicDevice"]:
        """
        The device mqtt.host in config.yaml sets up, or None if it doesn't name a broker.

        Args:
            config: Configs loaded from config.yaml.
            password: MQTT_PASSWORD.
            listening: The listening stats, whose snapshot the sensors and images show.
        """
        settings = config.section("mqtt")
        host = (settings.get("host") or "").strip()
        if not host:
            return None
        if not password:
            logger.warning("mqtt.host is set but MQTT_PASSWORD isn't, in ~/.config/nova/.env, "
                           "so Home Assistant's broker will most likely turn Nova's music away")
        return cls(host, int(settings.get("port") or 1883), (settings.get("username") or "").strip(),
                   password or "", listening)

    def discovery(self) -> Dict:
        """
        The announcement which makes the music device in Home Assistant, with its sensors and images.

        Returns:
            Home Assistant's device-based discovery message, with each by its id.
        """
        controls = {}
        for control, (name, entity_id, icon) in self._sensors.items():
            controls[control] = {"p": "sensor", "name": name, "state_topic": self._topics[control],
                                 "json_attributes_topic": f"{self._topics[control]}/attributes",
                                 "default_entity_id": entity_id, "icon": icon,
                                 "unique_id": f"{self.node_id}_{control}", "qos": QOS}
        for control, (name, entity_id) in self._images.items():
            controls[control] = {"p": "image", "name": name, "image_topic": self._topics[control],
                                 "content_type": self._types[control], "default_entity_id": entity_id,
                                 "unique_id": f"{self.node_id}_{control}", "qos": QOS}
        self._announced_types = dict(self._types)
        return {"dev": {"ids": [self.node_id], "name": self.name, "mf": "Nova", "mdl": "Nova brain"},
                "o": {"name": "Nova"},
                "availability_topic": self._availability,
                "cmps": controls}

    def _subscriptions(self) -> List[Tuple[str, int]]:
        """Only Home Assistant saying it's back, so the announcement is sent again."""
        return [(HA_STATUS, QOS)]

    def _on_message(self, client, userdata, message):
        """paho's callback for a message - only Home Assistant being back means anything."""
        if message.topic == HA_STATUS:
            super()._on_message(client, userdata, message)

    def publish_state(self):
        """Sends what's changed, and announces the images again if a picture's type isn't what they were announced with."""
        super().publish_state()
        # A picture's type is only known from the snapshot, which may have come after the announcement.
        if self._connected and self._types != self._announced_types:
            self._announce()

    def _window_state(self, key: str, window: Dict, pictures: List[bytes]) -> Tuple[str, Dict]:
        """
        A plays sensor's state, and its attributes, from one window ("7d", ...) of the snapshot.

        Args:
            key: The window's key, "7d" and so on.
            window: SoulSync's answer for it.
            pictures: Each of its first PICTURES_OF top artists' picture as its image sends it, b"" for none.
        """
        figures = _figures(window.get("overview"))
        previous = window.get("previous")
        change = _change(figures, _figures(previous) if isinstance(previous, dict) else {})
        clock = window.get("clock") or {}
        attributes = {
            "minutes": figures["minutes"], "artists": figures["artists"], "albums": figures["albums"],
            "tracks": figures["tracks"], "change": change,
            "top_artists": _top(window.get("top_artists")), "top_albums": _top(window.get("top_albums")),
            "top_tracks": _top(window.get("top_tracks")), "timeline": window.get("timeline"),
            "clock": {"grid": clock.get("grid"), "peak": clock.get("peak")}, "rhythm": window.get("rhythm"),
            "genres": (window.get("genres") or [])[:GENRES_OF],
        }
        # Home Assistant gives an image an address whether it has a picture or not, so the Music view is
        # told which have one, and shows a plain disc for the rest rather than a broken picture.
        for artist, picture in zip(attributes["top_artists"], pictures):
            artist["picture"] = bool(picture)
        attributes, size = _fit(attributes)
        # Sent as they are even then - the recorder, which would drop them, is told to leave them out -
        # but said once, with only the window and the size: never anything of what he listens to.
        if size <= ATTRIBUTES_LIMIT:
            self._oversize.discard(key)
        elif key not in self._oversize:
            self._oversize.add(key)
            logger.warning(f"The {key} listening stats are {size} bytes in Home Assistant even without "
                           "the timeline, over its 16 KB for attributes")
        plays = figures["plays"]
        return (UNKNOWN if plays is None else str(plays)), attributes

    def _state(self) -> Dict[str, object]:
        """What each state topic, each one's attributes and each image's picture should say now."""
        # Only read: snapshot() gives the latest one at once, and never asks SoulSync, which this
        # runs too often to do.
        snapshot = self.listening.snapshot()
        if snapshot.at is None:
            # Not read yet, as after every restart: nothing is sent, so the stats and pictures the broker kept
            # stay on the Music view, rather than being blanked until SoulSync answers.
            return {}
        state = {}
        for window, _, _ in WINDOWS:
            data = snapshot.windows.get(window)
            artists = (data or {}).get("top_artists") or []
            sent = []
            for n in range(1, PICTURES_OF + 1):
                control = f"artist_{window}_{n}"
                url = artists[n - 1].get("image_url") if n <= len(artists) else None
                picture = snapshot.pictures.get(url) if url else None
                # Nothing is sent as an empty payload, so Home Assistant shows none rather than a stale one.
                sent.append(picture[0] if picture else b"")
                state[self._topics[control]] = sent[-1]
                if picture:
                    self._types[control] = picture[1] or DEFAULT_TYPE
            value, attributes = self._window_state(window, data, sent) if isinstance(data, dict) else (UNKNOWN, {})
            control = f"plays_{window}"
            state[self._topics[control]] = value
            state[f"{self._topics[control]}/attributes"] = json.dumps(attributes)

        health, disk = snapshot.health or {}, snapshot.disk or {}
        duration = health.get("total_duration_ms")
        has_disk = bool(disk.get("has_data"))
        library = {
            "unplayed_count": health.get("unplayed_count"), "unplayed_percentage": health.get("unplayed_percentage"),
            "hours": None if duration is None else round(duration / 3600000, 1),
            "formats": health.get("format_breakdown"),
            "disk_bytes": disk.get("total_bytes") if has_disk else None,
            "disk_formats": disk.get("by_format") if has_disk else None,
            "refreshed": None if snapshot.at is None else datetime.fromtimestamp(snapshot.at).astimezone().isoformat(),
            "problem": snapshot.problem,
        }
        total = health.get("total_tracks")
        state[self._topics["library"]] = UNKNOWN if total is None else str(total)
        state[f"{self._topics['library']}/attributes"] = json.dumps(library)
        return state
