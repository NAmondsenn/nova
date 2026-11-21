"""
Devices Module
Where the user's phone, tablet or Mac is, and ringing it, through the Home Assistant app -
or through Google's Find My Device, which Home Assistant has as well (FindMyDevice).

Home Assistant is the only source of these, as of the batteries (see
HomeAssistant.app_devices): every device with the app on it, its tracker, the place its
geocoded location sensor names, and its notify service. What kind of device each is,
and which one "my phone" means, are batteries.py's own rules, so "my phone" is the same
device for both.

Google's Find My Device rings a phone louder and more surely than the app's notification,
and can stop it again, so a device it has is rung through it first (see same_device and
ActionExecutor.find_device). It has things the app isn't on, too - a tracker tag in a
wallet - found by their own names. Which devices it has is read from Home Assistant at
run time, as the app's are, never kept in code.

Where a device is never goes to Claude, whoever asked - see Conversation.handle, which
gives Claude's history only that it was said. The place is the user's, and Claude's
history goes to Anthropic with every later request.
"""

import re
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple, Union

from voice_assistant.batteries import is_mac, is_phone, kind_of, spoken_name
from voice_assistant.clock import format_duration

# What a tracker says when it can't tell where its device is.
UNKNOWN_STATES = ("unavailable", "unknown", "")
# Seconds after Nova rang something through Find My that a bare "stop ringing" or "found it"
# still means it - long enough to walk round the house looking.
RANG_LATELY = 600.0
# What a part of a place is when it isn't there - the Android app gives a town it hasn't
# got as the word "null" (live, 25 Oct), which would be "near Example Road, null".
MISSING_PARTS = ("null", "none", "unknown", "unavailable", "")


@dataclass
class AppDevice:
    """
    One device with the Home Assistant app on it, with what Nova works out from it.

    Attributes:
        id: The Home Assistant device id.
        name: The spoken name, e.g. "S25 Ultra".
        kind: "mobile" or "other", as batteries.kind_of sorts it.
        phone: Whether it's a phone, rather than a tablet.
        computer: Whether it's a Mac.
        apple: Whether Apple made it, whose app rings another way.
        notify: Its notify service, e.g. "mobile_app_s25_ultra", or None if it hasn't got one.
        tracker: Its tracker's state, e.g. "home", "not_home" or a zone's name, or None
            with no tracker.
        reported: When its tracker last reported, in epoch seconds, or None.
        place: Where its geocoded location sensor says it is, e.g. "Example Road, Sometown",
            or None.
    """

    id: str
    name: str
    kind: str
    phone: bool
    computer: bool
    apple: bool
    notify: Optional[str]
    tracker: Optional[str]
    reported: Optional[float]
    place: Optional[str]

    @classmethod
    def from_home(cls, found: Dict) -> "AppDevice":
        """
        Builds a device from one of HomeAssistant.app_devices()'s dicts.

        Args:
            found: The dict, with device, name, maker, model, notify, tracker and place.

        Returns:
            The device. Everything it has is on the app, so a Mac is the only one that isn't mobile.
        """
        maker, model = found.get("maker"), found.get("model")
        kind = kind_of(maker, model, True)
        tracker = found.get("tracker") or {}
        return cls(
            id=found["device"],
            name=spoken_name(found["name"]),
            kind=kind,
            phone=kind == "mobile" and is_phone(maker, model),
            computer=is_mac(maker, model),
            apple=(maker or "").lower() == "apple",
            notify=found.get("notify"),
            tracker=tracker.get("state"),
            reported=tracker.get("reported"),
            place=_place(found.get("place")),
        )


@dataclass
class FindMyDevice:
    """
    One device Google's Find My Device has, as Home Assistant's googlefindmy integration
    gives it - a phone or tablet the app may be on as well, or a tracker tag.

    Its kind, phone, computer and apple are those of a thing batteries.find can't tell
    the kind of, so "phone" never finds one by itself: the phone is the app's device,
    which same_device ties to its Find My one.

    Attributes:
        id: The Home Assistant device id.
        name: Its name in Home Assistant, e.g. "Galaxy S25 Ultra" or "Sam's Wallet".
        said: What it's called aloud: the name without an owner's name before it, e.g. "Wallet".
        play: Its Play sound button, e.g. "button.galaxy_s25_ultra_play_sound", or None.
        stop: Its Stop sound button, or None.
        tracker: Its tracker's state, e.g. "home", or None with no tracker, or one which
            can't tell.
        reported: When Google last saw it, in epoch seconds, or None.
        place: Always None - see from_home - so where says only how long ago it was seen.
    """

    id: str
    name: str
    said: str
    play: Optional[str]
    stop: Optional[str]
    tracker: Optional[str]
    reported: Optional[float]
    place: Optional[str] = None
    kind: str = "other"
    phone: bool = False
    computer: bool = False
    apple: bool = False

    @classmethod
    def from_home(cls, found: Dict) -> "FindMyDevice":
        """
        Builds a device from one of HomeAssistant.find_my_devices()'s dicts.

        Args:
            found: The dict, with device, name, play, stop and tracker.

        Returns:
            The device. Never a place: Find My's tracker gives coordinates, which are the
            user's alone, and no street.
        """
        tracker = found.get("tracker") or {}
        name = str(found.get("name") or "").strip()
        return cls(id=found["device"], name=name, said=_said_name(name), play=found.get("play"),
                   stop=found.get("stop"), tracker=tracker.get("state"), reported=tracker.get("reported"))


def _said_name(name: str) -> str:
    """A Find My device's name without an owner's before it - "Wallet" for "Sam's Wallet" - or the name as it is."""
    words = name.split()
    while len(words) > 1 and re.search(r"['’]s$", words[0].lower()):
        words = words[1:]
    return " ".join(words) or name


def _plain_words(name: str) -> List[str]:
    """A name's words in lower case, letters and digits only - "Galaxy S25 Ultra" is ["galaxy", "s25", "ultra"]."""
    return re.findall(r"[a-z0-9]+", name.lower())


def same_device(app: "AppDevice", find_my: FindMyDevice) -> bool:
    """
    Whether a device of the Home Assistant app's and one of Find My's are the same
    phone or tablet - when every word of one's name is in the other's, as "S25 Ultra" is
    in "Galaxy S25 Ultra". Never the Mac, which Google doesn't find.
    """
    if app.computer:
        return False
    ours, theirs = set(_plain_words(app.name)), set(_plain_words(find_my.name))
    return bool(ours and theirs) and (ours <= theirs or theirs <= ours)


def find_my_for(app: "AppDevice", find_mys: Sequence[FindMyDevice]) -> Optional[FindMyDevice]:
    """
    The Find My device which is the same as an app's device, or None if there's none - or
    several, and none of them shares more words of its name than the rest.
    """
    fits = [find_my for find_my in find_mys if same_device(app, find_my)]
    if len(fits) <= 1:
        return fits[0] if fits else None
    ours = set(_plain_words(app.name))
    shared = [len(ours & set(_plain_words(find_my.name))) for find_my in fits]
    best = max(shared)
    return fits[shared.index(best)] if shared.count(best) == 1 else None


class RangLately:
    """
    What Nova rang through Find My in the last RANG_LATELY seconds, for a bare "stop
    ringing" or "found it" - one for the whole house, as a phone rung at one satellite
    may be stopped at another.
    """

    def __init__(self, clock=time.time):
        """
        Args:
            clock: Gives the time now, in epoch seconds.
        """
        self._clock = clock
        self._lock = threading.Lock()
        # Stop button to what was rung, and when.
        self._rung: Dict[str, Tuple[FindMyDevice, float]] = {}

    def rang(self, device: FindMyDevice):
        """Notes that a device was rung, now - if it has a Stop sound button to stop it by."""
        if device.stop:
            with self._lock:
                self._rung[device.stop] = (device, self._clock())

    def stopped(self, device: FindMyDevice):
        """Notes that a device has been stopped."""
        with self._lock:
            self._rung.pop(device.stop, None)

    def lately(self) -> List[FindMyDevice]:
        """What was rung in the last RANG_LATELY seconds and not stopped since, the earliest first."""
        now = self._clock()
        with self._lock:
            return [device for device, when in sorted(self._rung.values(), key=lambda kept: kept[1])
                    if now - when <= RANG_LATELY]


def _place(place: Optional[Dict]) -> Optional[str]:
    """
    The place a geocoded location sensor names, e.g. "Example Road, Sometown" - its street and
    town, or whichever of them it has, else the first two parts of its state, which is the
    whole address.
    """
    if not place:
        return None
    parts = [_part(place.get("thoroughfare")), _part(place.get("locality"))]
    if not any(parts):
        parts = [_part(part) for part in str(place.get("state") or "").split(",")[:2]]
    return ", ".join(part for part in parts if part) or None


def _part(value) -> Optional[str]:
    """One part of a place as text, e.g. "Sometown" - or None for one that isn't there, such as "null"."""
    text = "" if value is None else str(value).strip()
    return None if text.lower() in MISSING_PARTS else text


def said_as(device: Union[AppDevice, FindMyDevice]) -> str:
    """
    What a device is called aloud: "phone", "tablet" or "Mac", or its name if it's none of
    those - for one of Find My's alone, its name without its owner's, e.g. "Wallet".
    """
    if isinstance(device, FindMyDevice):
        return device.said
    if device.phone:
        return "phone"
    if device.computer:
        return "Mac"
    return "tablet" if device.kind == "mobile" else device.name


def where(device: Union[AppDevice, FindMyDevice], now: float) -> str:
    """
    Says where a device is.

    Args:
        device: The device.
        now: The current epoch time.

    Returns:
        "Your phone's at home.", "Your phone's at Work." in another zone, "Your phone's
        near Example Road, Sometown, as of 10 minutes ago." away with a place, "Your phone's
        away from home, as of 10 minutes ago." without one, or "I can't tell where your
        phone is." with no tracker, or one that can't tell.
    """
    word = said_as(device)
    state = (device.tracker or "").strip()
    if state.lower() in UNKNOWN_STATES:
        return f"I can't tell where your {word} is."
    if state == "home":
        return f"Your {word}'s at home."
    if state != "not_home":
        return f"Your {word}'s at {state}."
    # Away, it may have moved since, so how old the place is goes with it.
    age = _age(device.reported, now)
    if device.place:
        return f"Your {word}'s near {device.place}{age}."
    return f"Your {word}'s away from home{age}."


def _age(reported: Optional[float], now: float) -> str:
    """", as of 10 minutes ago" - to the minute - or "" if when it reported isn't known."""
    if reported is None:
        return ""
    minutes = round(max(0.0, now - reported) / 60)
    return ", as of just now" if not minutes else f", as of {format_duration(minutes * 60)} ago"


def ring_call(device: AppDevice) -> Dict:
    """
    What's sent to a device's notify service to ring it, loud enough to find it by.

    Args:
        device: The device.

    Returns:
        The message and its data. Android's app says it aloud on the alarm stream, which
        is heard on silent; an iPhone or iPad gets a critical alert, which is too; a Mac
        a notification with its sound.
    """
    if not device.apple:
        return {"message": "TTS", "data": {"tts_text": f"Nova's looking for your {said_as(device)}.",
                                           "media_stream": "alarm_stream_max", "priority": "high", "ttl": 0}}
    if device.computer:
        return {"message": "Nova's looking for your Mac.", "data": {"push": {"sound": "default"}}}
    said = "iPhone" if device.phone else "iPad"
    return {"message": f"Nova's looking for your {said}.",
            "data": {"push": {"sound": {"name": "default", "critical": 1, "volume": 1.0}}}}


def told_claude(asked: str) -> str:
    """
    What Claude's history has in place of where a device is, when that was asked locally.

    Args:
        asked: The device, as the local rule had it, e.g. "phone" or "mac".

    Returns:
        e.g. "I said where your phone is."
    """
    return f"I said where your {'Mac' if asked.lower() == 'mac' else asked.lower()} is."
