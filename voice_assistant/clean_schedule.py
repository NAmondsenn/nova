"""
Clean Schedule Module
The library check started every night, and a short note of clean songs sent to the phone once a
week (decision 0022, section 8). SoulSync's own automations bring hundreds of songs a week which
aren't Nova's downloads, so only a check hears them.

Every night at the settings page's "Nightly check at" (05:30 by default, after SoulSync's 04:00
daily mixes and Monday's 03:00 discovery) the check is started as the page's Check library button
starts it: one already running is left to run, and one paused is carried on. A night missed while
Nova was off isn't made up: started after the night's time, Nova counts that night as done.

Once a week, from the page's day and time (Monday 09:30 by default), one message goes to ntfy's
phone topic saying how many songs went on each of the page's lists since the last note, and how
many files wait in .nova-held - only if any went on a list. A note due in the quiet hours waits
for them to end; one due while Nova was off goes at the next look that week.

What's been done - the night's date, the week's number and when its note went - is kept in the
clean songs file, so a restart neither does a night twice nor sends a week's note twice. It is
held here too, so a save which fails can't have every minute's look start the check or send the
note again.

A look is once a minute, on the local time as the system's zone has it: 05:30 is the user's,
not UTC's.
"""

import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional

from voice_assistant.clean_store import KINDS, CleanStore
from voice_assistant.config import WEEKDAYS

logger = logging.getLogger(__name__)

# Seconds between looks, so the night's check starts within a minute of its time.
LOOK_EVERY = 60

# What a setting is read as when it's missing or isn't one - config.yaml's defaults.
NIGHTLY_TIME = "05:30"
WEEKLY_DAY = "monday"
WEEKLY_TIME = "09:30"

# The first note counts the lists' entries of the last week.
FIRST_NOTE_COUNTS = 7 * 24 * 3600

TITLE = "Clean songs"


def _time(value, default: str) -> str:
    """A time of day as the settings hold it, "05:30" - or the default, if it isn't one."""
    return value if isinstance(value, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value) else default


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _many(number: int, one: str, more: str) -> str:
    """A count with its word: "1 file waits", "4 files wait"."""
    return f"{number} {one if number == 1 else more}"


def note(counts: Dict[str, int], held: int) -> str:
    """
    The weekly note, leaving out each list nothing went on, and the held files if there are none.

    Args:
        counts: How many songs went on each list since the last note, by kind: swapped,
            only_clean, unsure and couldnt. At least one is above 0.
        held: How many files wait in .nova-held now.

    Returns:
        e.g. "This week Nova swapped 3 clean songs for explicit ones; 1 only comes down clean, 2
        are unsure, 1 couldn't be swapped. 4 files wait in .nova-held. See the settings page."
    """
    swapped = counts.get("swapped", 0)
    rest = [(count, verb) for count, verb in (
        (counts.get("only_clean", 0), ("only comes down clean", "only come down clean")),
        (counts.get("unsure", 0), ("is unsure", "are unsure")),
        (counts.get("couldnt", 0), ("couldn't be swapped", "couldn't be swapped"))) if count]
    said = [_many(count, *verb) for count, verb in rest]
    if swapped:
        head = (f"Nova swapped {_many(swapped, 'clean song', 'clean songs')} for "
                f"{'an explicit one' if swapped == 1 else 'explicit ones'}")
        sentence = head + ("; " + ", ".join(said) if said else "")
    else:
        # With nothing swapped the first of the rest says what it counts: "2 songs only come down clean".
        count, (one, more) = rest[0]
        said[0] = f"{_many(count, 'song', 'songs')} {one if count == 1 else more}"
        sentence = ", ".join(said)
    text = f"This week {sentence}."
    if held > 0:
        text += f" {_many(held, 'file waits', 'files wait')} in .nova-held."
    return text + " See the settings page."


class CleanSchedule:
    """
    Looks once a minute at whether the night's library check or the week's note is due - see the
    module's docstring.
    """

    def __init__(self, clean_check, store: CleanStore, ntfy, settings: Callable[[], Dict],
                 page_url: Callable[[], str], clock: Callable[[], float] = time.time):
        """
        Args:
            clean_check: The library check: start() as the page's button starts it, and state(),
                the page's lists and how many files are held.
            store: The clean songs file, where what's been done is kept.
            ntfy: Sends the note - its phone topic, quiet() for the user's night and
                send(topic, message, title, click=) - or None with no ntfy, when no note goes.
            settings: Gives the clean_check section of the settings as they are now, read at each
                look so a change on the settings page is used straight away: nightly,
                nightly_time, weekly_note, weekly_day and weekly_time.
            page_url: Gives the settings page's address as it is now, for the note's link, or
                nothing when it isn't known.
            clock: Gives the current epoch time, only replaced by the tests.
        """
        self._check = clean_check
        self._store = store
        self._ntfy = ntfy
        self._settings = settings
        self._page_url = page_url
        self._clock = clock
        self._record = store.schedule()
        # The first look after Nova starts: a night whose time has gone is counted as done.
        self._first_look = True
        # So a note waiting out the night is logged once, not every minute.
        self._waiting = None
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Starts the daemon thread, which looks at once and then every LOOK_EVERY seconds."""
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="clean-schedule")
        self._thread.start()

    def stop(self):
        """Stops the thread, waiting briefly for a look already under way to end."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self):
        while not self._stopping.is_set():
            try:
                self.look()
            except Exception:
                logger.exception("Looking at whether the clean songs' night or week is due failed, will try again")
            self._stopping.wait(LOOK_EVERY)

    def look(self):
        """One look: starts the night's check if it's due, then sends the week's note if that is."""
        now = datetime.fromtimestamp(self._clock())
        settings = self._settings() or {}
        first, self._first_look = self._first_look, False
        self._night(now, settings, first)
        self._week(now, settings)

    def _keep(self, **changes):
        """Notes what's been done, here and in the store; a save which fails is logged, and holds until a restart."""
        self._record = {**self._record, **changes}
        try:
            self._store.save_schedule(self._record)
        except (OSError, TypeError) as e:
            logger.warning(f"Couldn't save what the clean songs' schedule has done ({type(e).__name__}), "
                           f"so it holds until Nova restarts")

    def _night(self, now: datetime, settings: Dict, first: bool):
        """Starts the library check once a night, at its time."""
        if settings.get("nightly", True) is False:
            return
        today = now.date().isoformat()
        if now.strftime("%H:%M") < _time(settings.get("nightly_time"), NIGHTLY_TIME) \
                or self._record.get("night_done") == today:
            return
        # Kept before the check is started, so nothing can have it started twice in a night.
        self._keep(night_done=today)
        if first:
            logger.info("Nova started after the night's library check was due, so the next is tomorrow night's")
            return
        try:
            result = self._check.start()
        except Exception:
            logger.exception("The night's library check couldn't be started")
            return
        logger.info(f"The night's library check: {(result or {}).get('message')}")

    def _week(self, now: datetime, settings: Dict):
        """Sends the week's note, once, from its day and time until the week ends."""
        if settings.get("weekly_note", True) is False:
            return
        year, number, _ = now.isocalendar()
        week = f"{year}-W{number:02d}"
        if self._record.get("week_sent") == week:
            return
        day = settings.get("weekly_day")
        day = day.strip().casefold() if isinstance(day, str) else ""
        due = now.date() - timedelta(days=now.weekday()) + timedelta(
            days=WEEKDAYS.index(day if day in WEEKDAYS else WEEKLY_DAY))
        if (now.date(), now.strftime("%H:%M")) < (due, _time(settings.get("weekly_time"), WEEKLY_TIME)):
            return
        sent_at = self._clock()
        # Read once, so a topic cleared on the settings page meanwhile is never sent to as "".
        phone = getattr(self._ntfy, "phone", "") if self._ntfy else ""
        if not phone:
            logger.info("The week's clean songs note has no phone topic to go to, so none is sent this week")
            self._keep(week_sent=week, note_at=sent_at)
            return
        if self._ntfy.quiet():
            if self._waiting != week:
                self._waiting = week
                logger.info("The week's clean songs note waits for the night to end")
            return
        state = self._check.state()
        since = self._record.get("note_at")
        since = since if _number(since) else sent_at - FIRST_NOTE_COUNTS
        # An entry with no time was listed before entries had one, so it's old news.
        counts = {kind: sum(1 for entry in state.get(kind) or []
                            if isinstance(entry, dict) and _number(entry.get("at")) and entry["at"] > since)
                  for kind in KINDS}
        if any(counts.values()):
            held = state.get("held")
            link = self._page_url()
            link = link.strip().rstrip("/") if isinstance(link, str) else ""
            if not self._ntfy.send(phone, note(counts, held if _number(held) else 0), title=TITLE,
                                   click=f"{link}/" if link else ""):
                # Not tried again: a note every minute while ntfy is down would be worse than none.
                logger.warning("The week's clean songs note couldn't be sent, so none goes this week")
        else:
            logger.info("Nothing went on the clean songs lists this week, so no note was sent")
        self._keep(week_sent=week, note_at=sent_at)
