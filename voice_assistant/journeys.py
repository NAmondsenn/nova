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

A place is found among Home Assistant's zones by the zone's name, or by the calendar's word for the activity
the zone is named as ("shift" for a Work zone). A place which only a zone could be - "my gran's", or a word for
one of the calendar's activities - and which no zone is, is refused rather than sent to Google, and so is an
entity's id said as a place: a zone is only ever found by its name.

Nova never reads, keeps or logs a position. A place is a zone's entity id and name, or the
words the user said, and the user's own place is their person's entity id, which Home
Assistant turns into a position itself. A journey's answer holds a time and a distance, so
nothing here can leak one - though one from the user's own place says how far they are from
somewhere, so it is private, kept from Claude as where their phone is. The place isn't put in a
log line either: only the kind of error.

Its answers are {"success", "message"} dicts like pc.py's, with an "ask_first" Action for
"Want a reminder?", which only a yes said next runs. Public transport gets a journey time and
nothing more: Google doesn't say which one to catch, so no time to leave can be told from it.

Its settings are the journeys: section of config.yaml, read at each use through the settings
function it's given, a missing or unusable one being its default.

JourneyWatcher is the thread which looks at the traffic while a "Leave for ..." reminder waits,
moves the reminder when the time to leave changes, and says so.
"""

import logging
import math
import re
import threading
import time as clock_time
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, Optional, Tuple

import requests

from voice_assistant.actions import Action
from voice_assistant.batteries import HOME_FAILURES
from voice_assistant.choices import ChoiceStore
from voice_assistant.clock_words import format_duration, spoken_clock_time
from voice_assistant.config import CHOICES, limit_problem
from voice_assistant.google_calendar import READ_FAILURES, _as_said
from voice_assistant.home_assistant import ANY_ENTITY, HomeAssistant, NoJourneys, NotAllowed, journey_place

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
# Seconds between the watcher's passes over the reminders it watches. Each reminder is looked at for the traffic
# only as often as check_every_minutes says: this is only how soon after that one is noticed to be due.
PASS_EVERY = 60
# The soonest a moved reminder may ring, from the look which moved it.
MOVE_AT_LEAST = 60

WHERE_TO = "Where to?"
NOT_CAUGHT_WHERE = "I didn't catch where to."
ALREADY_THERE = "You're already there."
NO_DAY = "I didn't catch which day."
NEEDS_GOOGLE_MAPS = "Journeys need Google Maps Travel Time set up in Home Assistant."
OVER_CAP = "I've asked Google for all the journeys I'm allowed this month."
NO_JOURNEY_TIME = "I couldn't get a journey time."
WHERE_ARE_YOU = "I can't tell where you are right now."
NO_CALENDAR = "I can't read your calendar right now."
NO_TIME = "I didn't catch what time you need to be there."
TIME_GONE = "That time's already gone."
NO_TRANSIT_TIME = " I can't say which one to catch, so I can't give a time to leave."
WHEN_THERE = " When do you need to be there?"
OFFER = " Want a reminder?"
# What Claude's history has in place of a journey worked out from where the user is - see told_claude.
FROM_WHERE_YOU_ARE = "I said how long the journey takes from where you are."


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


def _asked(state: Dict, month: str) -> int:
    """
    How many journeys the store says were asked in a month.

    Args:
        state: The store's state, whose "month" and "requests" are read.
        month: The month, e.g. "2026-11".

    Returns:
        The count for that month, or 0 for a month which isn't the stored one. A count below 0 reads as 0,
        so a hand-edited file can only raise the count it reads, never lower what was spent.
    """
    return max(0, state["requests"]) if state["month"] == month else 0


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


def _unknown(words: str) -> str:
    """What's said of a place which only a zone could be, and which no zone is - the words as they were said."""
    return f"I don't know where {' '.join(words.split())} is. Add it as a zone in Home Assistant."


def _label(place: str) -> str:
    """What the reminder to leave for a place is called, which is how one already set is found."""
    return f"Leave for {place}"


def _minutes(seconds: int) -> int:
    """A journey's seconds as minutes, rounded up, and never less than one."""
    return max(1, -(-int(seconds) // 60))


def told_claude(said: str) -> str:
    """
    What Claude's history has in place of a journey worked out from where the user is, as
    devices.told_claude is for where a device is: that it was said, and the question it ended
    with, if it did, so that Claude can carry the answer to "When do you need to be there?".

    Args:
        said: The answer, as it was said.

    Returns:
        e.g. "I said how long the journey takes from where you are. When do you need to be there?"
    """
    asked = next((question for question in (WHEN_THERE, OFFER) if said.endswith(question.strip())), "")
    return FROM_WHERE_YOU_ARE + asked


def _day_said(day: date, now: datetime) -> str:
    """A day as it's said after an event or a time: "today", "tomorrow" or "on Friday"."""
    days = (day - now.date()).days
    if days <= 0:
        return "today"
    return "tomorrow" if days == 1 else f"on {WEEKDAYS[day.weekday()]}"


def _when(when: datetime, now: datetime) -> str:
    """A time as it's said, with its day if it isn't today."""
    said = spoken_clock_time(when.hour, when.minute)
    return said if when.date() <= now.date() else f"{said} {_day_said(when.date(), now)}"


def _day(said: str, now: datetime) -> Optional[date]:
    """
    The day asked about, from what the tool was given.

    Args:
        said: "YYYY-MM-DD".
        now: The time now, local.

    Returns:
        The date, or None if it isn't one, or it's before today.
    """
    said = said.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", said):
        return None
    try:
        day = datetime.strptime(said, "%Y-%m-%d").date()
    except ValueError:
        return None
    return day if day >= now.date() else None


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

    def used(self) -> Dict[str, int]:
        """
        How many journeys have been asked of Google this month, and how many may be.

        Reads the count the way look counts by it, and changes nothing.

        Returns:
            Dict with 'requests', the asks this month, and 'cap', the monthly_requests setting as it is now.
        """
        month, cap = _month(), self._setting("monthly_requests")
        with self.store.lock:
            return {"requests": _asked(self.store.state, month), "cap": cap}

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
            count = _asked(state, month)
            if count >= cap:
                raise OverCap()
            # Counted before it's asked, so an ask which fails or is never answered is still counted.
            self.store.keep(lambda new: new.update(month=month, requests=count + 1), "count of journeys asked")
        return self.home.journey(origin, destination, HOME_MODES.get(mode, mode), departure)

    def ask(self, place: str, mode: str = "driving", leave: bool = False, arrive_at: Optional[str] = None,
            satellite: Optional[str] = None, now: Optional[datetime] = None, day: Optional[str] = None) -> Dict:
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
            day: The day asked about, "YYYY-MM-DD", or None for the next day from now. Only used to
                find the place's activity on the calendar: with leave, and without arrive_at, which
                already says its day.

        Returns:
            Dict with 'success' and the 'message' to say - never a time to leave unless it was
            worked out - and 'ask_first', the Action which sets the reminder, if one is offered.
            'private' is True on any answer once it is worked out from where the user is.
        """
        now = (now or datetime.now()).astimezone()
        origin = None
        try:
            if mode not in HOME_MODES:
                raise _Refused("I can only work out journeys by car, on foot, by bike or by public transport.")
            destination, place_said, name = self._place(place or "")
            origin = self._origin(satellite)
            if destination == origin:
                # "How long to get home?" from home: there's nothing to ask Google.
                answer = {"success": True, "message": ALREADY_THERE}
            elif not leave or mode == "transit":
                found = self._google(origin, destination, mode, place_said)
                words = self._time_words(found, mode, place_said)
                answer = {"success": True, "message": words + (NO_TRANSIT_TIME if leave else "")}
            else:
                answer = self._leave(origin, destination, place_said, name, mode, arrive_at, day, satellite, now)
        except _Refused as refused:
            answer = refused.answer
        if origin is not None and origin.startswith("person."):
            # How far the user is from a place says where they are, which Claude's history - sent to Anthropic
            # with every later request - is never told: the answer is the user's alone, as where their phone is.
            answer = {**answer, "private": True}
        return answer

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

        An entity's id is refused before anything is looked up. Then a zone is found by its name, else by the
        calendar's word for the activity it is named as ("shift" is Work). Else words for one of the calendar's
        activities, and words which begin "my ", are refused, and any others go to Google as said.

        Returns:
            What to ask Google for - a zone's id, else the words cleaned of what mustn't go to it -
            what to say back, and the name to find the place's activity by.
        """
        if ANY_ENTITY.fullmatch(words.strip().lower()):
            # "person.kit" or "zone.work" from Claude: a zone is only ever found by its name, and a person - whose
            # id Home Assistant would turn into where their phone is - is never where to.
            raise _Refused(NOT_CAUGHT_WHERE)
        tidy = _zone_key(words)
        if tidy:
            try:
                zones = self.home.zones()
            except (requests.RequestException, ValueError) as e:
                raise self._failure(e) from e
            for zone in zones:
                if _zone_key(zone["name"]) == tidy:
                    return zone["id"], zone["name"], zone["name"]
            # No zone is named so: the calendar's word for an activity ("shift") is the zone named as that
            # activity ("Work"). A zone's own name was tried first, so it always wins over a word.
            activity = self._activity_for_word(words)
            if activity is not None:
                for zone in zones:
                    if _zone_key(zone["name"]) == _zone_key(activity):
                        return zone["id"], zone["name"], zone["name"]
                # The activity's calendar would still give its start, and sent to Google as words, "work" would be
                # a confident time to leave for wherever Google guessed, which never names the place.
                raise _Refused(_unknown(words))
        if _as_said(words).startswith("my "):
            # "My gran's" is a place of the user's own, which only a zone can be: sent to Google as words, a
            # misheard one - "my grans" - would be a confident drive to the wrong place, said back as the same.
            raise _Refused(_unknown(words))
        sent = journey_place(words)
        if not sent:
            raise _Refused(WHERE_TO)
        return sent, sent, sent

    def _activity_for_word(self, words: str) -> Optional[str]:
        """
        The activity which the words are the calendar's word for.

        Args:
            words: The place as said, e.g. "my shift".

        Returns:
            The activity, e.g. "work", or None if Nova has no calendar, it can't be read (that is no
            activity, and is said, if it matters, when the arrival time is looked for), or the words
            are no activity's word.
        """
        if self.calendar is None:
            return None
        try:
            # Google's list is read, if it's old, before it's looked in, as _next_start does.
            self.calendar.calendars()
            return self.calendar.activity_words().get(_tidy(words)) or None
        except READ_FAILURES as e:
            logger.warning(f"Couldn't read the calendar's words for a place: {type(e).__name__}")
            return None

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

    def _next_start(self, activity: str, now: datetime,
                    day: Optional[date] = None) -> Optional[Tuple[str, str, Optional[datetime]]]:
        """
        When the place's activity next starts, from its calendar.

        Args:
            activity: The name the place is known by, as said.
            now: The time now, local.
            day: The day asked about, or None for the next day from now.

        Returns:
            None if Nova has no calendar or it has no such activity. Else the activity's word,
            its calendar's name, and the start of its next timed event which begins after now and
            within a day - or, with a day, the first on that day which begins after now, however
            far off - or None if there is none. An event in progress, or all day, isn't it.

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
        first, days = (now.date(), 2) if day is None else (day, 1)
        events = self.calendar.events(first, days, calendar_ids=[calendar["id"]])
        starts = [event["start"].astimezone() for event in events
                  if not event["all_day"] and isinstance(event["start"], datetime) and now < event["start"]
                  # A day's read has an event which began the day before and runs into it: that isn't the day's.
                  and (event["start"] <= now + LOOK_AHEAD if day is None
                       else event["start"].astimezone().date() == day)]
        return word, calendar.get("name") or word.capitalize(), min(starts) if starts else None

    def _leave(self, origin: str, destination: str, place: str, name: str, mode: str, arrive_at: Optional[str],
               day: Optional[str], satellite: Optional[str], now: datetime) -> Dict:
        """When to leave: the arrival time less the journey and the buffer, and the offer of a reminder."""
        before, after, activity, arrival = "", "", None, None
        if arrive_at:
            arrival = _arrival(arrive_at, now)
            if arrival is None:
                raise _Refused(NO_TIME)
            if arrival <= now:
                raise _Refused(TIME_GONE)
        else:
            on = None
            if day is not None:
                on = _day(day, now)
                if on is None:
                    raise _Refused(NO_DAY)
            try:
                found = self._next_start(name, now, on)
            except READ_FAILURES as e:
                logger.warning(f"Couldn't read the calendar for a journey: {type(e).__name__}")
                before = NO_CALENDAR + " "
            else:
                if found is None:
                    after = WHEN_THERE
                elif found[2] is None:
                    before = f"You've no {found[0]} {'in the next day' if on is None else _day_said(on, now)}. "
                else:
                    activity, arrival = found[1], found[2]
        found = self._google(origin, destination, mode, place)
        if arrival is None:
            return {"success": True, "message": before + self._time_words(found, mode, place) + after}
        minutes, buffer = _minutes(found["seconds"]), timedelta(minutes=self._setting("buffer_minutes"))
        # Made local again after the sum, which keeps the arrival's offset: across a clock change that is an hour out
        # of what the clock will show, so the time said, and asked of Google, would be one which doesn't come.
        leave_by = (arrival - timedelta(minutes=minutes) - buffer).astimezone().replace(second=0, microsecond=0)
        about = ""
        if mode == "driving" and leave_by - now >= timedelta(minutes=self._setting("watch_minutes")):
            # Too far off for the traffic now to say much: how it's expected to be at the time of leaving.
            forecast = self._google(origin, destination, mode, place, f"{leave_by:%H:%M}", none_is_refused=False)
            if forecast is not None:
                found, about = forecast, "about "
                minutes = _minutes(found["seconds"])
                leave_by = (arrival - timedelta(minutes=minutes) - buffer).astimezone().replace(second=0, microsecond=0)
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
            # One reminder for one arrival: asked again, it's told when the one already set rings, not offered
            # another. One which rings after this arrival is for a later one.
            rings = self.clock.reminder_due(_label(place))
            if rings is not None and rings <= arrival.timestamp():
                set_for = _when(datetime.fromtimestamp(rings).astimezone(), now)
                answer["message"] += f" Your reminder's set for {set_for}."
                return answer
            params = {"place": place, "destination": destination, "origin": origin, "mode": mode,
                      "arrive": arrival.timestamp(), "leave_by": leave_by.timestamp(),
                      "watch": mode == "driving" and origin == "zone.home"}
            answer["message"] += OFFER
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
        reply = self.clock.set_timer(duration_seconds=seconds, label=_label(place), is_reminder=True,
                                     satellite=satellite)
        if not reply.get("success"):
            return reply
        if watch:
            # The journey was looked at for the ask a moment ago, so that is its last look: one set inside the watch
            # window isn't looked at again within the minute.
            watched = {"destination": destination, "origin": origin, "place": place, "arrive": arrive,
                       "leave_by": leave_by, "satellite": satellite, "seen_home": False, "looked": now.timestamp()}
            self.store.keep(lambda state: state["watches"].update({str(reply["id"]): watched}),
                            "journey being watched")
        ringing = datetime.fromtimestamp(now.timestamp() + seconds).astimezone()
        return {"success": True, "message": f"I'll remind you at {_when(ringing, now)}."}


class JourneyWatcher:
    """
    Watches the reminders to leave which Journeys.remind keeps: while one waits, looks at the traffic from
    time to time, moves it when the time to leave has changed enough to matter, and says so. It also drops
    one once Home Assistant says the user has left - at home inside the watch window, then away: they've gone,
    so the reminder is of no use - and forgets one which has rung or been cancelled.

    Each look is of one watch, and one watch which fails never stops the others, nor the thread.
    """

    def __init__(self, journeys: Journeys, say: Callable[[Optional[str], str], None],
                 home_now: Callable[[], Optional[bool]], settings: Callable[[], Dict]):
        """
        Args:
            journeys: The Journeys which keeps the watches, asks Google and holds the clock.
            say: Says a message on the satellite which set the reminder (None for the default one), and
                wherever else it should reach the user - see Brain._say_journey. It must not raise.
            home_now: Whether the user is at home now, None if that can't be told - see football_watch.user_home.
            settings: Gives the journeys: section as it is now, read at each use.
        """
        self._journeys = journeys
        self._say = say
        self._home_now = home_now
        self._settings = settings
        # The watches whose failure to look has been logged, so it is once for each and not every look.
        self._warned = set()
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _setting(self, name: str) -> Any:
        return setting(self._settings, name)

    def check(self, now: float) -> None:
        """
        Does one pass: for each watched reminder, whatever it is time for (see _watch).

        Args:
            now: The time now, in epoch seconds.
        """
        clock, store = self._journeys.clock, self._journeys.store
        watches = store.get("watches")
        if clock is None or not watches:
            return
        at_home = []
        for key, watch in watches.items():
            try:
                self._watch(clock, key, watch, now, at_home)
            except Exception:
                # One watch's trouble is its own: the rest are still looked at.
                logger.exception("Watching a reminder to leave failed")

    def _forget(self, key: str) -> None:
        """Stops watching a reminder."""
        self._warned.discard(key)
        self._journeys.store.keep(lambda state: state["watches"].pop(key, None), "journey no longer watched")

    def _update(self, key: str, **changes: Any) -> None:
        """Saves what has changed of a watch - if it is still there - before anything else is done about it."""
        def change(state: Dict):
            if key in state["watches"]:
                state["watches"][key].update(changes)
        self._journeys.store.keep(change, "journey being watched")

    def _at_home(self, at_home: list) -> Optional[bool]:
        """Whether the user is home, asked once in a pass - the list is that pass's note of the answer."""
        if not at_home:
            try:
                at_home.append(self._home_now())
            except Exception:
                logger.exception("Couldn't tell whether the user is home")
                at_home.append(None)
        return at_home[0]

    def _watch(self, clock, key: str, watch: Dict, now: float, at_home: list) -> None:
        """
        One look at one watched reminder, in this order: forget it if it's gone; leave it alone if its time has come;
        go no further before the watch window; see where the user is; go no further if it was looked at within
        check_every_minutes; look at the traffic and, if the time to leave has changed, move the reminder.

        Args:
            clock: The Clock which holds the reminder.
            key: The reminder's id, as a string, which the watch is kept by.
            watch: The watch, as Journeys.remind keeps it, with "looked", the time of the last look - the time it was
                set, until the first.
            now: The time now, in epoch seconds.
            at_home: This pass's note of whether the user is home, for _at_home.
        """
        try:
            due = clock.timer_due(int(key))
        except ValueError:
            due = None
        if due is None:
            # It has rung, or been cancelled or removed.
            self._forget(key)
            return
        if due <= now:
            # Due, but the clock hasn't rung it yet, and a reminder in that moment can't be moved: it is about
            # to ring, and the next look forgets it.
            return
        timer_id = int(key)
        if watch["leave_by"] - now >= self._setting("watch_minutes") * 60:
            # Before the window, where the user is says nothing of this journey: out at eleven for a time to leave
            # at five, they will be back. So it isn't asked, nor kept, nor acted on.
            return

        home = self._at_home(at_home)
        if home:
            if not watch.get("seen_home"):
                self._update(key, seen_home=True)
                watch["seen_home"] = True
        elif home is False and watch.get("seen_home"):
            # At home in the window and now away: they have left for it.
            clock.drop_timer(timer_id)
            self._forget(key)
            logger.info("Dropped the reminder to leave, as Home Assistant says the user has left")
            return

        if now - watch.get("looked", 0) < self._setting("check_every_minutes") * 60:
            return
        # Saved first, so a restart doesn't ask again at once, and a look which fails is tried again as often as
        # any other and not on every pass.
        self._update(key, looked=now)

        found = self._look(key, watch)
        if found is None:
            return
        self._warned.discard(key)

        minutes = _minutes(found["seconds"])
        # Aware, as _leave's is: a clock change between now and the arrival doesn't move the time to leave by an hour.
        leave_by = datetime.fromtimestamp(watch["arrive"]).astimezone() \
            - timedelta(minutes=minutes + self._setting("buffer_minutes"))
        new = leave_by.replace(second=0, microsecond=0).timestamp()
        if abs(new - watch["leave_by"]) < self._setting("change_minutes") * 60:
            return
        place, here = watch["place"], datetime.fromtimestamp(now).astimezone()
        if new - now < LEAVE_NOW.total_seconds():
            clock.drop_timer(timer_id)
            self._forget(key)
            self._say(watch.get("satellite"), f"Traffic to {place} is heavier now. You need to leave now.")
            return
        rings = max(new - self._setting("reminder_minutes") * 60, now + MOVE_AT_LEAST)
        if not clock.move_timer(timer_id, rings):
            # It rang, or was cancelled, just now.
            self._forget(key)
            return
        # Saved before it's said, so a restart doesn't say it twice.
        self._update(key, leave_by=new)
        earlier = new < watch["leave_by"]
        now_said = _when(datetime.fromtimestamp(new).astimezone(), here)
        was_said = _when(datetime.fromtimestamp(watch["leave_by"]).astimezone(), here)
        self._say(watch.get("satellite"), f"Traffic to {place} {'is heavier now' if earlier else 'has eased'}. "
                  f"Leave by {now_said}, not {was_said}.")

    def _look(self, key: str, watch: Dict) -> Optional[Dict]:
        """
        Looks at the drive.

        Returns:
            The journey, or None if it couldn't be had - then the reminder is left as it is, and the failure
            is logged, by its kind and never the place, once for the watch until a look works.
        """
        try:
            found = self._journeys.look(watch["origin"], watch["destination"], "driving")
        except Exception as e:
            reason = type(e).__name__
        else:
            if found is not None:
                return found
            reason = "no route"
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(f"Couldn't look at the traffic for a reminder to leave: {reason}")
        return None

    def start(self) -> None:
        """Starts the daemon thread which makes a pass every PASS_EVERY seconds."""
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="journeys")
        self._thread.start()

    def stop(self) -> None:
        """Stops the thread, waiting briefly for a pass already under way to end."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._pass()
            self._stopping.wait(PASS_EVERY)

    def _pass(self) -> None:
        """One pass of the thread, in which no error may end the thread."""
        try:
            self.check(clock_time.time())
        except Exception:
            logger.exception("Watching reminders to leave failed, will try again")
