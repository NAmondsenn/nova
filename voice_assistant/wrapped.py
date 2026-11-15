"""
Wrapped Module.

Makes the user's Wrapped - a recap of their listening, one for each month and one for the
year - saves it, and tells their phone when it's ready. The story page which shows it, and
Nova reading it out, are made from what is saved here.

Each is made from the listening snapshot (see listening.py) and kept as it was, with the
pictures it shows, so it stays the same after SoulSync's stats move on. A monthly one is the
last 30 days as SoulSync counts them, with the real dates shown, and the yearly one is its
"Your Year" and its last 12 months: SoulSync's stats come only in rolling windows, so a
calendar month isn't there to ask for. Made at the start of the 1st, a month's window ends
with the month before's last day, which is why a monthly one is named for that month.

make is pure: it is given the snapshot and the time, reads no clock, and says the same in
any time zone. WrappedStore keeps them; WrappedWatcher, a thread, makes each when its time
comes.

His listening is never logged: a failure says what went wrong, never an artist or a song.
"""

import logging
import os
import re
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from voice_assistant import storage
from voice_assistant.listening import ListeningUnavailable

# Logger setup
logger = logging.getLogger(__name__)

# Seconds between the watcher's looks at whether a Wrapped is due.
LOOK_EVERY = 3600

# The most monthly Wrappeds kept. Every yearly one is kept: there is one a year.
KEEP_MONTHS = 24

# How many of each list the Wrapped holds.
TOP = 5

# What the picture files are called, by the content type they came with.
PICTURE_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_EXTENSION_TYPES = {extension: kind for kind, extension in PICTURE_EXTENSIONS.items()}

# The days of the window a monthly one covers: SoulSync's "30d".
MONTH_DAYS = 30

# The watcher waits until just after midnight on the 1st, so that SoulSync's last half-hour rebuild
# of the month it has just ended is in.
FIRST_LOOK = (0, 5)

# How long after midnight on the 1st the listening snapshot must have been taken for a Wrapped to be made
# from it. SoulSync rebuilds its stats every 30 minutes, so its last rebuild with the month's final plays
# is by 00:30, and Nova reads them as often, so the read after that is by 00:35 at the latest. A snapshot
# from before would miss the end of the month, or, if SoulSync was down, be days old.
SNAPSHOT_READY_AFTER = timedelta(minutes=35)

# Said in the notification, as strftime's would be in the system's language.
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
               "October", "November", "December"]

# A Wrapped's name: "2026" for a year, "2026-11" for a month. Digits only, so it can't be a path.
_KEY = re.compile(r"[0-9]{4}(-[0-9]{2})?")


def valid_key(key) -> bool:
    """Whether something is a Wrapped's name - which is all that is ever made into a file name."""
    return isinstance(key, str) and _KEY.fullmatch(key) is not None


def _last_of_previous_month(now: datetime) -> date:
    """The day before the 1st of now's month: the last day of the month a Wrapped made now is for."""
    return now.date().replace(day=1) - timedelta(days=1)


def month_key(now: datetime) -> str:
    """The key of the monthly Wrapped made at this time: the month just ended, e.g. "2026-11" on 1 December."""
    last = _last_of_previous_month(now)
    return f"{last.year}-{last.month:02d}"


def year_key(now: datetime) -> str:
    """The key of the yearly Wrapped made at this time: the year of the month just ended, e.g. "2026"."""
    return str(_last_of_previous_month(now).year)


def time_of_day(hour: Optional[int]) -> Optional[str]:
    """
    Says what sort of listener the hour they listen most at makes them.

    Args:
        hour: 0 to 23, local time, or None.

    Returns:
        e.g. "a night owl", or None with no hour.
    """
    if hour is None:
        return None
    if 5 <= hour <= 11:
        return "an early bird"
    if 12 <= hour <= 17:
        return "an afternoon listener"
    if 18 <= hour <= 22:
        return "an evening listener"
    return "a night owl"


def _dict(value) -> Dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> List:
    return value if isinstance(value, list) else []


class _Pictures:
    """The pictures a Wrapped uses, numbered as they are first wanted, each once."""

    def __init__(self, available: Dict[str, Tuple[bytes, str]]):
        self._available = available
        self._numbers: Dict[str, int] = {}
        self.saved: List[Tuple[bytes, str]] = []

    def number(self, url) -> Optional[int]:
        """
        The picture's number in the Wrapped, or None if the snapshot hasn't got it, or it isn't a
        picture the page could show.
        """
        found = self._available.get(url) if isinstance(url, str) else None
        if not found:
            return None
        if url not in self._numbers:
            data, content_type = found
            content_type = str(content_type).split(";")[0].strip().lower()
            if not isinstance(data, bytes) or content_type not in PICTURE_EXTENSIONS:
                return None
            self._numbers[url] = len(self.saved)
            self.saved.append((data, content_type))
        return self._numbers[url]


def _artists(items, plays: str, pictures: _Pictures) -> List[Dict]:
    return [{"name": item.get("name"), "plays": item.get(plays), "picture": pictures.number(item.get("image_url"))}
            for item in map(_dict, _list(items)[:TOP])]


def _with_artist(items, plays: str) -> List[Dict]:
    return [{"name": item.get("name"), "artist": item.get("artist"), "plays": item.get(plays)}
            for item in map(_dict, _list(items)[:TOP])]


def _top_genre(window: Dict) -> Optional[Dict]:
    """The genre most of the plays were in, as {"name", "percentage"}, or None if there is none."""
    genres = [genre for genre in map(_dict, _list(window.get("genres")))
              if genre.get("genre") and isinstance(genre.get("percentage"), (int, float))]
    if not genres:
        return None
    top = max(genres, key=lambda genre: genre["percentage"])
    return {"name": top["genre"], "percentage": top["percentage"]}


def _busiest_day(day) -> Optional[Dict]:
    day = _dict(day)
    return {"date": day["date"], "plays": day.get("plays")} if day.get("date") else None


def _hour(hour) -> Optional[int]:
    return hour if isinstance(hour, int) and not isinstance(hour, bool) and 0 <= hour <= 23 else None


def make_with_pictures(snapshot, kind: str, now: datetime) -> Optional[Tuple[Dict, List[Tuple[bytes, str]]]]:
    """
    Makes a Wrapped, and the pictures it shows, from the listening snapshot.

    Args:
        snapshot: The listening Snapshot.
        kind: "year" or "month".
        now: The local time it is made, which names the month or year it is for and, for a month,
            the 30 days it covers.

    Returns:
        The Wrapped (see make) and its pictures, each as (bytes, content type), numbered as the
        Wrapped's "picture" says. None if the period had no plays.

    Raises:
        ValueError: If the kind isn't "year" or "month".
    """
    if kind not in ("year", "month"):
        raise ValueError(f"A Wrapped is of a year or a month, not '{kind}'.")
    pictures = _Pictures(snapshot.pictures)
    if kind == "year":
        year = _dict(snapshot.year)
        totals = _dict(year.get("totals"))
        if not totals.get("plays"):
            return None
        window = _dict(snapshot.windows.get("12m"))
        period = _dict(year.get("period"))
        peak_hour = _hour(_dict(year.get("top_hour")).get("hour"))
        wrapped = {
            "key": year_key(now), "kind": kind, "made": now.isoformat(timespec="seconds"),
            "period": {"start": period.get("start"), "end": period.get("end")},
            "minutes": totals.get("minutes"), "plays": totals.get("plays"), "artists": totals.get("artists"),
            "albums": totals.get("albums"), "tracks": totals.get("tracks"),
            "active_days": totals.get("active_days"),
            "top_artists": _artists(year.get("top_artists"), "plays", pictures),
            "top_tracks": _with_artist(year.get("top_tracks"), "plays"),
            "top_albums": _with_artist(year.get("top_albums"), "plays"),
            "top_genre": _top_genre(window), "time_of_day": time_of_day(peak_hour), "peak_hour": peak_hour,
            "longest_streak": _dict(window.get("rhythm")).get("longest_streak"),
            "busiest_day": _busiest_day(year.get("peak_day")),
            "months": [{"label": month.get("label"), "minutes": month.get("minutes"),
                        "top_artist": month.get("top_artist")} for month in map(_dict, _list(year.get("months")))],
            "discoveries": _artists(year.get("discoveries"), "plays", pictures),
        }
        return wrapped, pictures.saved

    window = _dict(snapshot.windows.get("30d"))
    overview = _dict(window.get("overview"))
    if not overview.get("total_plays"):
        return None
    rhythm = _dict(window.get("rhythm"))
    peak_hour = _hour(_dict(_dict(window.get("clock")).get("peak")).get("hour"))
    wrapped = {
        "key": month_key(now), "kind": kind, "made": now.isoformat(timespec="seconds"),
        "period": {"start": (now - timedelta(days=MONTH_DAYS)).date().isoformat(),
                   "end": (now.date() - timedelta(days=1)).isoformat()},
        "minutes": round((overview.get("total_time_ms") or 0) / 60000), "plays": overview.get("total_plays"),
        "artists": overview.get("unique_artists"), "albums": overview.get("unique_albums"),
        "tracks": overview.get("unique_tracks"), "active_days": rhythm.get("active_days"),
        "top_artists": _artists(window.get("top_artists"), "play_count", pictures),
        "top_tracks": _with_artist(window.get("top_tracks"), "play_count"),
        "top_albums": _with_artist(window.get("top_albums"), "play_count"),
        "top_genre": _top_genre(window), "time_of_day": time_of_day(peak_hour), "peak_hour": peak_hour,
        "longest_streak": rhythm.get("longest_streak"), "busiest_day": _busiest_day(rhythm.get("busiest_day")),
    }
    return wrapped, pictures.saved


def make(snapshot, kind: str, now: datetime) -> Optional[Dict]:
    """
    Makes a Wrapped from the listening snapshot.

    Args:
        snapshot: The listening Snapshot.
        kind: "year" or "month".
        now: The local time it is made - see make_with_pictures.

    Returns:
        The Wrapped as a dict of what the story shows: key ("2026" or "2026-11", the year or
        month it covers), kind, made, period (start and end dates), minutes, plays, artists,
        albums, tracks, active_days, top_artists (name, plays, and picture - the number of its
        saved picture, or None), top_tracks and top_albums (name, artist, plays), top_genre
        (name, percentage), time_of_day, peak_hour, longest_streak and busiest_day (date, plays).
        A year's has months (label, minutes, top_artist) and discoveries as top_artists is, too.
        None if the period had no plays.
    """
    made = make_with_pictures(snapshot, kind, now)
    return made[0] if made else None


def _sort_key(key: str) -> Tuple[int, int]:
    """Orders keys by when they cover: a year after all its months, so, newest first, before them."""
    return int(key[:4]), int(key[5:]) if len(key) > 4 else 13


class WrappedStore:
    """The saved Wrappeds, as a file for each and one for each of its pictures, in one folder."""

    def __init__(self, folder: str):
        """
        Args:
            folder: Where they are kept, e.g. wrapped inside Nova's data folder. Made when the first is saved.
        """
        self._folder = folder
        self._lock = threading.Lock()

    def _path(self, key: str) -> str:
        return os.path.join(self._folder, f"{key}.json")

    def _picture_files(self, key: str) -> Dict[int, str]:
        """Each saved picture's number and file name for a key."""
        found = {}
        try:
            names = os.listdir(self._folder)
        except OSError:
            return found
        for name in names:
            matched = re.fullmatch(re.escape(key) + r"-([0-9]+)\.(jpg|png|webp)", name)
            if matched:
                found[int(matched[1])] = name
        return found

    def save(self, wrapped: Dict, pictures: List[Tuple[bytes, str]]):
        """
        Saves a Wrapped with its pictures, each file atomically, and so that the Wrapped itself
        - which is what makes it count as there - is written last. Replaces one with the same key.
        Then forgets the monthly ones beyond the newest KEEP_MONTHS, with their pictures.

        Args:
            wrapped: As make gives it.
            pictures: Its pictures in order, each as (bytes, content type): the one numbered n is
                saved as <key>-<n>.jpg, png or webp.

        Raises:
            ValueError: If the key isn't one, or a picture is of a type which isn't kept.
            OSError: If it couldn't be written.
        """
        key = wrapped.get("key")
        if not valid_key(key):
            raise ValueError("A Wrapped needs a key like 2026 or 2026-11.")
        extensions = []
        for _, content_type in pictures:
            extension = PICTURE_EXTENSIONS.get(str(content_type).split(";")[0].strip().lower())
            if extension is None:
                raise ValueError(f"A picture of type {content_type} can't be kept.")
            extensions.append(extension)
        with self._lock:
            storage.private_folder(self._folder)
            for number, ((data, _), extension) in enumerate(zip(pictures, extensions)):
                self._write(os.path.join(self._folder, f"{key}-{number}.{extension}"), data)
            # Any left from one which this replaces, with its own pictures.
            wanted = {f"{key}-{number}.{extension}" for number, extension in enumerate(extensions)}
            for name in self._picture_files(key).values():
                if name not in wanted:
                    self._remove(os.path.join(self._folder, name))
            storage.save_json(self._path(key), wrapped)
            self._prune()

    def load(self, key) -> Optional[Dict]:
        """A saved Wrapped, or None if there isn't one by that key - or it can't be read, or isn't a key."""
        if not valid_key(key):
            return None
        try:
            return _dict(storage.load_json(self._path(key), None)) or None
        except (OSError, ValueError):
            return None

    def mark_told(self, key):
        """
        Notes that the phone was told of a Wrapped: its "told" becomes true, the rest and its pictures
        as they were, written atomically. Nothing is done for one which isn't there.
        """
        with self._lock:
            wrapped = self.load(key)
            if wrapped is not None:
                storage.save_json(self._path(key), {**wrapped, "told": True})

    def keys(self) -> List[str]:
        """The keys of the Wrappeds which can be read, newest first: a year before its months."""
        try:
            names = os.listdir(self._folder)
        except OSError:
            return []
        keys = [name[:-5] for name in names if name.endswith(".json") and valid_key(name[:-5])]
        return [key for key in sorted(keys, key=_sort_key, reverse=True) if self.load(key) is not None]

    def picture(self, key, number) -> Optional[Tuple[bytes, str]]:
        """
        A saved Wrapped's picture.

        Args:
            key: The Wrapped's.
            number: Its place in the Wrapped's "picture", from 0.

        Returns:
            The bytes and the content type, or None if there is none.
        """
        if not valid_key(key) or isinstance(number, bool) or not isinstance(number, int) or number < 0:
            return None
        name = self._picture_files(key).get(number)
        if name is None:
            return None
        try:
            with open(os.path.join(self._folder, name), "rb") as file:
                return file.read(), _EXTENSION_TYPES[name.rsplit(".", 1)[1]]
        except OSError:
            return None

    def _prune(self):
        """Forgets the monthly Wrappeds past the newest KEEP_MONTHS. Under the lock."""
        try:
            names = os.listdir(self._folder)
        except OSError:
            return
        months = sorted((name[:-5] for name in names if name.endswith(".json") and valid_key(name[:-5])
                         and len(name) == len("2026-11.json")), key=_sort_key, reverse=True)
        for key in months[KEEP_MONTHS:]:
            for name in self._picture_files(key).values():
                self._remove(os.path.join(self._folder, name))
            self._remove(self._path(key))

    @staticmethod
    def _remove(path: str):
        try:
            os.remove(path)
        except OSError:
            logger.warning(f"Couldn't remove {os.path.basename(path)} from the Wrapped folder")

    @staticmethod
    def _write(path: str, data: bytes):
        """Writes bytes to a file atomically, readable by this user only - as storage.save_text does text."""
        handle, temporary = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".picture-", suffix=".tmp")
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise


class WrappedWatcher:
    """
    Looks once an hour, and once as it starts, at whether a Wrapped is due: the month just ended's,
    and in the month the user chose, the year's. A due one which isn't in the store is made, saved
    and the phone told. Each is made once, in the month it is due in - a look later in the month,
    after Nova was off at midnight, still makes it - and a month which went by without one is not
    made after. Whether the phone has been told is kept in the saved Wrapped, so a restart neither
    loses a notification which waited or was refused nor sends one twice.
    """

    def __init__(self, listening, store: WrappedStore, ntfy, settings: Callable[[], Dict],
                 clock: Callable[[], float] = time.time):
        """
        Args:
            listening: Gives the snapshot to make from: its require() raises ListeningUnavailable
                until SoulSync has been read, when the look is tried again an hour later.
            store: Where they are kept.
            ntfy: Tells the phone - its phone topic, quiet() for the user's night and
                send(topic, message, title, click=) - or None with no ntfy, when the Wrapped is made
                and nobody told.
            settings: Gives the wrapped section of the settings as they are now, read at each look
                so that a change on the settings page is used straight away: enabled, monthly,
                yearly_month and link.
            clock: Gives the current epoch time, only replaced by the tests.
        """
        self._listening = listening
        self._store = store
        self._ntfy = ntfy
        self._settings = settings
        self._clock = clock
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Starts the daemon thread, which looks at once and then every LOOK_EVERY seconds."""
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="wrapped")
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
                logger.exception("Looking for a Wrapped to make failed, will try again")
            self._stopping.wait(LOOK_EVERY)

    def look(self):
        """
        One look: makes whichever Wrapped is due and not yet made, then tells the phone of each of this
        month's which it hasn't been told of - which isn't done at night, so one made just after
        midnight waits for the first look outside quiet hours.
        """
        # The local time as the system's zone has it - the 1st is the user's, not UTC's.
        now = datetime.fromtimestamp(self._clock())
        settings = _dict(self._settings())
        if settings.get("enabled", True) is False:
            return
        if now.day == 1 and (now.hour, now.minute) < FIRST_LOOK:
            return
        wanted = []
        if settings.get("monthly", True) is not False:
            wanted.append(("month", month_key(now)))
        if self._yearly_month(settings) == now.month:
            wanted.append(("year", year_key(now)))
        self._make([(kind, key) for kind, key in wanted if self._store.load(key) is None], now)
        # Only this month's: one from an earlier month which was never told of is stale news.
        for _, key in wanted:
            wrapped = self._store.load(key)
            # Only a Wrapped which says it wasn't told of: one with no note was made before there was one.
            if wrapped is not None and wrapped.get("told") is False:
                try:
                    self._tell(key, settings)
                except Exception:
                    logger.exception(f"Telling the phone of the Wrapped {key} failed, will try again")

    def _make(self, due: List[Tuple[str, str]], now: datetime):
        """Makes and saves each Wrapped in due, as not yet told of. One not made is tried again at the next look."""
        if not due:
            return
        try:
            snapshot = self._listening.require()
        except ListeningUnavailable as e:
            logger.info(f"No Wrapped made yet, listening isn't available: {e}")
            return
        # Made now or made late, the numbers must hold the whole month: Listening keeps its last good snapshot
        # however old, so this is the watcher's to check. Tried again at the next look.
        ready = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + SNAPSHOT_READY_AFTER
        if snapshot.at is None or datetime.fromtimestamp(snapshot.at) < ready:
            logger.info("No Wrapped made yet, the listening stats are from before the month's last rebuild")
            return
        for kind, key in due:
            try:
                made = make_with_pictures(snapshot, kind, now)
                if made is None:
                    logger.info(f"No Wrapped for {key}: there were no plays")
                    continue
                wrapped, pictures = made
                self._store.save({**wrapped, "told": False}, pictures)
                logger.info(f"Made the {kind}'s Wrapped {key}")
            except Exception:
                logger.exception(f"Making the Wrapped {key} failed, will try again")

    @staticmethod
    def _yearly_month(settings: Dict) -> Optional[int]:
        month = settings.get("yearly_month", 12)
        return month if isinstance(month, int) and not isinstance(month, bool) else None

    @staticmethod
    def _message(key: str) -> str:
        what = MONTH_NAMES[int(key[5:]) - 1] if len(key) > 4 and 1 <= int(key[5:]) <= 12 else key[:4]
        return f"Your {what} Wrapped is ready."

    def _tell(self, key: str, settings: Dict):
        """
        Tells the phone a Wrapped is ready, and notes in the store that it has been, so that it is told
        once, whatever restarts. Left for the next look if there is nowhere to tell, it is night, or ntfy
        doesn't take it.
        """
        # Read once, so a topic cleared on the settings page meanwhile is never sent to as "".
        phone = getattr(self._ntfy, "phone", "") if self._ntfy else ""
        if not phone:
            # With no ntfy, or no topic for the phone, there's nowhere to say it: a topic set later
            # in the month is told then.
            logger.info(f"The Wrapped {key} is ready, with no phone topic to tell")
            return
        if self._ntfy.quiet():
            logger.info(f"The Wrapped {key} is ready, and will be told of once it isn't night")
            return
        link = settings.get("link")
        link = link.strip().rstrip("/") if isinstance(link, str) else ""
        click = f"{link}/wrapped?key={key}" if link else ""
        if self._ntfy.send(phone, self._message(key), title="Wrapped", click=click):
            self._store.mark_told(key)
