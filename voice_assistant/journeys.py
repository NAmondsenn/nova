"""
Journeys Module
How long a journey takes, and when to leave for it - "what time do I leave for work?" and
"how long's the drive to my gran's?" - from the design in
docs/superpowers/specs/2026-11-17-leaving-time-design.md.

Everything about a journey comes from Home Assistant: its zones are the places Nova knows by
name, its Google Maps Travel Time integration works out the time (see HomeAssistant.journey),
and Google's own calendars, which Nova already reads, say when the user needs to be somewhere -
an activity's next event starts when its calendar says. Nova asks Google through Home
Assistant and never directly, and counts each ask by calendar month so that a month's free
requests are never passed.

Nova never reads, keeps or logs a position. A place is a zone's entity id and name, or the
words the user said, and the user's own place is their person's entity id, which Home
Assistant turns into a position itself. A journey's answer holds a time and a distance, so
nothing here can leak one. The place isn't put in a log line either: only the kind of error.

Its answers are {"success", "message"} dicts like pc.py's, with an "ask_first" Action for
"Want a reminder?", which only a yes said next runs. Public transport gets a journey time and
nothing more: Google doesn't say which one to catch, so no time to leave can be told from it.

Its settings are the journeys: section of config.yaml, read at each use through the settings
function it's given, a missing or unusable one being its default.
"""

import logging
import math
import re
from datetime import datetime, time, timedelta
from typing import Any, Callable, Dict, Optional, Tuple

import requests

from voice_assistant.actions import Action
from voice_assistant.batteries import HOME_FAILURES
from voice_assistant.choices import ChoiceStore
from voice_assistant.clock_words import format_duration, spoken_clock_time
from voice_assistant.config import CHOICES, limit_problem
from voice_assistant.google_calendar import READ_FAILURES, _as_said
from voice_assistant.home_assistant import HomeAssistant, NoJourneys, NotAllowed, journey_place

# Logger setup
logger = logging.getLogger(__name__)

# What journeys.json holds, by key: the month the count is of, the asks of Google made in it, the
# watched reminders (see the watcher) and the satellites which travel with the user.
JOURNEY_DEFAULTS: Dict[str, Any] = {"month": "", "requests": 0, "watches": {}, "travelling": []}

# Config.yaml's journeys: section, as a setting falls back to it.
SETTING_DEFAULTS: Dict[str, Any] = {
    "buffer_minutes": 10, "reminder_minutes": 5, "check_every_minutes": 5, "watch_minutes": 45,
    "change_minutes": 5, "heavy_minutes": 5, "heavy_percent": 20, "monthly_requests": 3000,
    "distance_unit": "miles"}

# Each way of travelling Nova says, and Home Assistant's word for it.
HOME_MODES = {"driving": "driving", "walking": "walking", "cycling": "bicycling", "transit": "transit"}
# What each is called in "The drive to Work is...".
NOUNS = {"driving": "drive", "walking": "walk", "cycling": "cycle"}
# What a unit is called, in the singular and the plural, and how many metres it is.
UNITS = {"miles": ("mile", "miles", 1609.344), "km": ("km", "km", 1000)}
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# A time to leave this near, or past, is a time to go, not one to set a reminder for.
LEAVE_NOW = timedelta(minutes=2)
# How far ahead an activity's next event may be found.
LOOK_AHEAD = timedelta(hours=24)

WHERE_TO = "Where to?"
NEEDS_GOOGLE_MAPS = "Journeys need Google Maps Travel Time set up in Home Assistant."
OVER_CAP = "I've asked Google for all the journeys I'm allowed this month."
NO_JOURNEY_TIME = "I couldn't get a journey time."
WHERE_ARE_YOU = "I can't tell where you are right now."
NO_CALENDAR = "I can't read your calendar right now."
NO_TIME = "I didn't catch what time you need to be there."
TIME_GONE = "That time's already gone."
NO_TRANSIT_TIME = " I can't say which one to catch, so I can't give a time to leave."
WHEN_THERE = " When do you need to be there?"


class OverCap(Exception):
    """Nova has asked Google as many times as the month's setting allows."""


class _Refused(Exception):
    """Carries the answer to give instead of a journey, from wherever it's found out it can't be."""

    def __init__(self, message: str, success: bool = False):
        super().__init__(message)
        self.answer = {"success": success, "message": message}


def setting(settings: Callable[[], Dict], name: str) -> Any:
    """
    One of the journeys: settings, as it is now.

    Args:
        settings: Gives the section as it stands, e.g. lambda: config.section("journeys").
        name: The setting, e.g. "buffer_minutes".

    Returns:
        What it is, or its default if the section can't be read, or it's missing or isn't
        something config.yaml and the settings page would have kept.
    """
    default = SETTING_DEFAULTS[name]
    try:
        value = (settings() or {}).get(name)
    except (KeyError, TypeError, AttributeError):
        return default
    if value is None:
        return default
    choices = CHOICES.get(("journeys", name))
    if choices:
        return str(value).lower() if str(value).lower() in choices else default
    if limit_problem("journeys", name, value):
        return default
    return int(value)


def _month() -> str:
    """The calendar month it is here, e.g. "2026-11" - the one the count of asks is of."""
    return datetime.now().strftime("%Y-%m")


def _tidy(words: str) -> str:
    """
    Words as a place or an activity is matched: as a request is tidied, with a leading "my " or
    "the " dropped, and a curly apostrophe - which a phone's keyboard puts in a zone's name - made straight.
    """
    said = _as_said(words.replace("\u2019", "'").replace("\u2018", "'"))
    for lead in ("my ", "the "):
        if said.startswith(lead):
            return said[len(lead):]
    return said


def _zone_key(words: str) -> str:
    """
    A place's words as they're compared with a zone's name, with no apostrophes at all: speech to text drops
    them, so "my grans" is "Gran's". Only for comparing - the place said back is the zone's own name.
    """
    return _tidy(words).replace("'", "")


def _minutes(seconds: int) -> int:
    """A journey's seconds as minutes, rounded up, and never less than one."""
    return max(1, -(-int(seconds) // 60))


def _when(when: datetime, now: datetime) -> str:
    """A time as it's said, with its day if it isn't today."""
    said = spoken_clock_time(when.hour, when.minute)
    days = (when.date() - now.date()).days
    if days <= 0:
        return said
    return f"{said} tomorrow" if days == 1 else f"{said} on {WEEKDAYS[when.weekday()]}"


def _arrival(said: str, now: datetime) -> Optional[datetime]:
    """
    The time to be there, from what the tool was given.

    Args:
        said: "HH:MM", meaning its next occurrence, or "YYYY-MM-DD HH:MM", both local.
        now: The time now, local.

    Returns:
        A local datetime, or None if it isn't either.
    """
    said = said.strip()
    try:
        if re.fullmatch(r"\d{1,2}:\d{2}", said):
            clock = time(*(int(part) for part in said.split(":")))
            when = datetime.combine(now.date(), clock).astimezone()
            # A time already gone today is the next day's.
            return when if when > now else datetime.combine(now.date() + timedelta(days=1), clock).astimezone()
        return datetime.strptime(said, "%Y-%m-%d %H:%M").astimezone()
    except ValueError:
        return None


class Journeys:
    """
    Works out a journey, and when to leave for it, for the tools which answer the user.

    Attributes:
        home: HomeAssistant, or anything with its zones, people and journey.
        calendar: GoogleCalendar, or None if Nova hasn't one - then only a time said can be arrived by.
        clock: The Clock which will set the reminder, or None - then none is offered.
        store: journeys.json, as JOURNEY_DEFAULTS.
    """

    def __init__(self, home, calendar, clock, store: ChoiceStore, settings: Callable[[], Dict]):
        """
        Args:
            home: HomeAssistant.
            calendar: GoogleCalendar, or None.
            clock: Clock, or None.
            store: The ChoiceStore of journeys.json.
            settings: Gives the journeys: section as it is now, so a change on the settings page
                is used at the next ask.
        """
        self.home = home
        self.calendar = calendar
        self.clock = clock
        self.store = store
        self.settings = settings

    def _setting(self, name: str) -> Any:
        return setting(self.settings, name)

    def travels(self, satellite: Optional[str]) -> bool:
        """Whether a satellite goes where the user goes, so its journeys start from the user, not from home."""
        return bool(satellite) and satellite in self.store.get("travelling")

    def set_travels(self, satellite: str, on: bool) -> None:
        """
        Marks a satellite as one which travels with the user, or not.

        Raises:
            OSError: If it can't be saved. Nothing is changed.
        """
        def change(state: Dict):
            names = [name for name in state["travelling"] if name != satellite]
            state["travelling"] = names + [satellite] if on else names
        self.store.change(change)

    def look(self, origin: str, destination: str, mode: str, departure: Optional[str] = None) -> Optional[Dict]:
        """
        Asks Home Assistant for a journey, which asks Google, and counts it.

        Args:
            origin: Where from, as HomeAssistant.journey has it.
            destination: Where to.
            mode: "driving", "walking", "cycling" or "transit".
            departure: "HH:MM" to leave at - driving only - or None for now.

        Returns:
            HomeAssistant.journey's answer: seconds, clear_seconds and metres, or None for no route.

        Raises:
            OverCap: If this month's asks have reached monthly_requests. Nothing is asked.
            NoJourneys, NotAllowed, ValueError, requests.RequestException: As HomeAssistant.journey.
        """
        month, cap = _month(), self._setting("monthly_requests")
        with self.store.lock:
            state = self.store.state
            # A month which isn't this one starts again at 0, and so does a count which can't be one: whatever
            # a hand-edited file holds, it can only make the count more than 0, never less than what was asked.
            count = max(0, state["requests"]) if state["month"] == month else 0
            if count >= cap:
                raise OverCap()
            # Counted before it's asked, so an ask which fails or is never answered is still counted.
            self.store.keep(lambda new: new.update(month=month, requests=count + 1), "count of journeys asked")
        return self.home.journey(origin, destination, HOME_MODES.get(mode, mode), departure)

    def ask(self, place: str, mode: str = "driving", leave: bool = False, arrive_at: Optional[str] = None,
            satellite: Optional[str] = None, now: Optional[datetime] = None) -> Dict:
        """
        How long a journey takes, or when to leave for it.

        Args:
            place: Where to, as it was said - a zone's name, or any place.
            mode: "driving", "walking", "cycling" or "transit".
            leave: Whether the time to leave was asked for, and not just how long it takes.
            arrive_at: When to be there, local, "HH:MM" or "YYYY-MM-DD HH:MM" - or None to use the
                place's activity on the calendar.
            satellite: The satellite it was asked on, which says where from.
            now: The time now, local - only given by the tests.

        Returns:
            Dict with 'success' and the 'message' to say - never a time to leave unless it was
            worked out - and 'ask_first', the Action which sets the reminder, if one is offered.
        """
        now = (now or datetime.now()).astimezone()
        try:
            if mode not in HOME_MODES:
                raise _Refused("I can only work out journeys by car, on foot, by bike or by public transport.")
            destination, place_said, name = self._place(place or "")
            origin = self._origin(satellite)
            if not leave or mode == "transit":
                found = self._google(origin, destination, mode, place_said)
                words = self._time_words(found, mode, place_said)
                return {"success": True, "message": words + (NO_TRANSIT_TIME if leave else "")}
            return self._leave(origin, destination, place_said, name, mode, arrive_at, satellite, now)
        except _Refused as refused:
            return refused.answer

    def _failure(self, error: Exception) -> _Refused:
        """The answer for a journey Home Assistant couldn't give, with the error's kind logged once - never the place."""
        if isinstance(error, OverCap):
            return _Refused(OVER_CAP)
        if isinstance(error, NoJourneys):
            return _Refused(NEEDS_GOOGLE_MAPS)
        logger.warning(f"Couldn't get a journey: {type(error).__name__}")
        if isinstance(error, (NotAllowed, ValueError)):
            return _Refused(NO_JOURNEY_TIME)
        return _Refused(HomeAssistant.failure_message(error))

    def _place(self, words: str) -> Tuple[str, str, str]:
        """
        Finds the place said, among Home Assistant's zones by name.

        Returns:
            What to ask Google for - a zone's id, else the words cleaned of what mustn't go to it -
            what to say back, and the name to find the place's activity by.
        """
        tidy = _zone_key(words)
        if tidy:
            try:
                zones = self.home.zones()
            except (requests.RequestException, ValueError) as e:
                raise self._failure(e) from e
            for zone in zones:
                if _zone_key(zone["name"]) == tidy:
                    return zone["id"], zone["name"], zone["name"]
        if _as_said(words).startswith("my "):
            # "My gran's" is a place of the user's own, which only a zone can be: sent to Google as words, a
            # misheard one - "my grans" - would be a confident drive to the wrong place, said back as the same.
            raise _Refused(f"I don't know where {' '.join(words.split())} is. Add it as a zone in Home Assistant.")
        sent = journey_place(words)
        if not sent:
            raise _Refused(WHERE_TO)
        return sent, sent, sent

    def _origin(self, satellite: Optional[str]) -> str:
        """Where the journey starts: home, or the user - their person's id - for a satellite which travels."""
        if not self.travels(satellite):
            return "zone.home"
        try:
            return self.home.people()[0]["id"]
        except HOME_FAILURES + (IndexError,) as e:
            logger.warning(f"Couldn't tell where the user is: {type(e).__name__}")
            raise _Refused(WHERE_ARE_YOU) from e

    def _google(self, origin: str, destination: str, mode: str, place: str, departure: Optional[str] = None,
                none_is_refused: bool = True) -> Optional[Dict]:
        """A journey through look, its failures made into the answers which say so."""
        try:
            found = self.look(origin, destination, mode, departure)
        except (OverCap, NoJourneys, NotAllowed, ValueError, requests.RequestException) as e:
            raise self._failure(e) from e
        if found is None and none_is_refused:
            raise _Refused(f"I couldn't find a way to {place}.")
        return found

    def _distance(self, metres: int) -> str:
        """A distance as it's said: to a tenth under 10, else whole, in the unit chosen."""
        singular, plural, size = UNITS[self._setting("distance_unit")]
        far = metres / size
        far = round(far) if far >= 10 else round(far, 1)
        # Nine point nine five is ten, and six point oh isn't something to say.
        number = f"{far:.0f}" if far == int(far) else f"{far:.1f}"
        return f"{number} {singular if far == 1 else plural}"

    def _time_words(self, found: Dict, mode: str, place: str) -> str:
        """A journey's time, its distance, and if the traffic's heavy, which it was said as it is now."""
        duration = format_duration(_minutes(found["seconds"]) * 60)
        if mode == "transit":
            return f"Public transport to {place} takes {duration}."
        return f"The {NOUNS[mode]} to {place} is {duration}, {self._distance(found['metres'])}." \
            + self._heavy(found, mode)

    def _heavy(self, found: Dict, mode: str) -> str:
        """Says so if a drive is over its time on a clear road by both of heavy_minutes and heavy_percent."""
        clear = found["clear_seconds"]
        if mode != "driving" or clear is None:
            return ""
        over = found["seconds"] - clear
        if over < self._setting("heavy_minutes") * 60 or over < clear * self._setting("heavy_percent") / 100:
            return ""
        minutes = int(over / 60 + 0.5)
        return f" Traffic's heavy: that's {minutes} minute{'' if minutes == 1 else 's'} more than on a clear road."

    def _next_start(self, activity: str, now: datetime) -> Optional[Tuple[str, str, Optional[datetime]]]:
        """
        When the place's activity next starts, from its calendar.

        Args:
            activity: The name the place is known by, as said.
            now: The time now, local.

        Returns:
            None if Nova has no calendar or it has no such activity. Else the activity's word,
            its calendar's name, and the start of its next timed event which begins after now and
            within a day - or None if there is none. An event in progress, or all day, isn't it.

        Raises:
            READ_FAILURES: If the calendar can't be read.
        """
        if self.calendar is None:
            return None
        # Google's list is read, if it's old, before it's looked in.
        self.calendar.calendars()
        activities = self.calendar.activity_calendars()
        said = _tidy(activity)
        word = self.calendar.activity_words().get(said) or said
        if word not in activities:
            return None
        calendar = activities[word]
        events = self.calendar.events(now.date(), 2, calendar_ids=[calendar["id"]])
        starts = [event["start"].astimezone() for event in events
                  if not event["all_day"] and isinstance(event["start"], datetime)
                  and now < event["start"] <= now + LOOK_AHEAD]
        return word, calendar.get("name") or word.capitalize(), min(starts) if starts else None

    def _leave(self, origin: str, destination: str, place: str, name: str, mode: str, arrive_at: Optional[str],
               satellite: Optional[str], now: datetime) -> Dict:
        """When to leave: the arrival time less the journey and the buffer, and the offer of a reminder."""
        before, after, activity, arrival = "", "", None, None
        if arrive_at:
            arrival = _arrival(arrive_at, now)
            if arrival is None:
                raise _Refused(NO_TIME)
            if arrival <= now:
                raise _Refused(TIME_GONE)
        else:
            try:
                found = self._next_start(name, now)
            except READ_FAILURES as e:
                logger.warning(f"Couldn't read the calendar for a journey: {type(e).__name__}")
                before = NO_CALENDAR + " "
            else:
                if found is None:
                    after = WHEN_THERE
                elif found[2] is None:
                    before = f"You've no {found[0]} in the next day. "
                else:
                    activity, arrival = found[1], found[2]
        found = self._google(origin, destination, mode, place)
        if arrival is None:
            return {"success": True, "message": before + self._time_words(found, mode, place) + after}
        minutes, buffer = _minutes(found["seconds"]), timedelta(minutes=self._setting("buffer_minutes"))
        leave_by = (arrival - timedelta(minutes=minutes) - buffer).replace(second=0, microsecond=0)
        about = ""
        if mode == "driving" and leave_by - now >= timedelta(minutes=self._setting("watch_minutes")):
            # Too far off for the traffic now to say much: how it's expected to be at the time of leaving.
            forecast = self._google(origin, destination, mode, place, f"{leave_by:%H:%M}", none_is_refused=False)
            if forecast is not None:
                found, about = forecast, "about "
                minutes = _minutes(found["seconds"])
                leave_by = (arrival - timedelta(minutes=minutes) - buffer).replace(second=0, microsecond=0)
        duration = about + format_duration(minutes * 60)
        sentence = f"{activity} starts at {_when(arrival, now)}. The {NOUNS[mode]} is {duration}, " if activity \
            else f"The {NOUNS[mode]} to {place} is {duration}, "
        offer = leave_by - now >= LEAVE_NOW
        if not offer:
            sentence += "so you need to leave now."
        else:
            sentence += f"so leave by {_when(leave_by, now)}"
            sentence += "." if activity else f" to be there for {_when(arrival, now)}."
        # What the traffic is like now is no use for a time of leaving which was asked for later.
        message = sentence + (self._heavy(found, mode) if not about else "")
        answer = {"success": True, "message": message}
        if offer and self.clock is not None:
            params = {"place": place, "destination": destination, "origin": origin, "mode": mode,
                      "arrive": arrival.timestamp(), "leave_by": leave_by.timestamp(),
                      "watch": mode == "driving" and origin == "zone.home"}
            answer["message"] += " Want a reminder?"
            answer["ask_first"] = Action("set_leave_reminder", params)
        return answer

    def remind(self, place: str, destination: str, origin: str, mode: str, arrive: float, leave_by: float,
               watch: bool, satellite: Optional[str] = None, now: Optional[datetime] = None) -> Dict:
        """
        Sets the reminder to leave, on the user's yes to "Want a reminder?" - the params of the
        Action ask offered - and, for a drive from home, keeps it to be watched for the traffic.

        Args:
            place: What the place is called, for "Leave for Work" and for saying it back.
            destination: What was asked of Google - a zone's id or the words.
            origin: Where from - zone.home, or the user's person.
            mode: How they travel.
            arrive: When they need to be there, in epoch seconds.
            leave_by: When they need to leave, in epoch seconds.
            watch: Whether the traffic is looked at as the time to leave nears - see the watcher.
            satellite: The satellite it rings on, the one it was asked on.
            now: The time now, local - only given by the tests.

        Returns:
            Dict with 'success' and the 'message' to say: "I'll remind you at 8:15." - or the
            clock's own refusal, a clash or a satellite which is gone, as it was given.
        """
        now = (now or datetime.now()).astimezone()
        rings = leave_by - self._setting("reminder_minutes") * 60
        # A moment which is here or all but is a minute on, so it still rings - the clock won't set one gone.
        # Rounded up: the time to leave is on a whole minute and now isn't, so rounding down could ring a moment
        # before the minute, and say the one before.
        seconds = max(60, math.ceil(rings - now.timestamp()))
        reply = self.clock.set_timer(duration_seconds=seconds, label=f"Leave for {place}", is_reminder=True,
                                     satellite=satellite)
        if not reply.get("success"):
            return reply
        if watch:
            watched = {"destination": destination, "origin": origin, "place": place, "arrive": arrive,
                       "leave_by": leave_by, "satellite": satellite, "seen_home": False}
            self.store.keep(lambda state: state["watches"].update({str(reply["id"]): watched}),
                            "journey being watched")
        ringing = datetime.fromtimestamp(now.timestamp() + seconds).astimezone()
        return {"success": True, "message": f"I'll remind you at {_when(ringing, now)}."}
