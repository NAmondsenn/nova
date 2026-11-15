"""
Listening Module.

Keeps what SoulSync knows of what the user has listened to, as one snapshot for Nova to
answer from: the voice tool, the Music view in Home Assistant and the Wrapped all read
this and nothing else, so what Nova says, shows and recaps can't disagree.

SoulSync is the one source because it already records every play (it polls Navidrome),
its Stats page is the one he likes, and its history keeps a song's title and artist as
text, so it survives a clean-up of the library. Nova keeping a second copy of the plays
would be more code and a second set of numbers. Its stats come only in rolling windows -
the last 7 days, 30 days, 12 months, or all time - so a calendar "October" or "today"
isn't answerable from here, and nothing here pretends it is.

SoulSync rebuilds its stats every 30 minutes, so they are read as often, in the
background, and a question is answered from the snapshot at once and never waits on
SoulSync. A refresh which fails leaves the last good snapshot in place with the
reason beside it. The stats are SoulSync's internal API, not its versioned one, and
its image is :latest, so an update could change their shape: that is checked, and said
politely, rather than guessed at.

His listening is never logged: a failure says what went wrong, never an artist or a song.
"""

import logging
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Tuple

from voice_assistant.soulsync import SoulSync, SoulSyncError

# Logger setup
logger = logging.getLogger(__name__)

# The windows SoulSync gives, by the word the user would use, and what each is called when said.
WINDOWS = {"week": "7d", "month": "30d", "year": "12m", "all": "all"}
WINDOW_WORDS = {"7d": "the last 7 days", "30d": "the last 30 days", "12m": "the last 12 months", "all": "all time"}

# Seconds between refreshes: SoulSync's own worker rebuilds its stats every 30 minutes, so asking
# more often would only read the same numbers again.
REFRESH_EVERY = 30 * 60

# How many top artists of each list have their picture fetched, and how many discoveries: the Music
# view and the Wrapped show five, and a picture is a request to SoulSync for each.
PICTURES_OF = 5

# What is said when there is nothing to answer from.
NOT_YET = "I haven't got your listening stats from SoulSync yet."
NOT_SET_UP = "SoulSync isn't set up, so Nova can't read your listening stats."
SHAPE_CHANGED = "SoulSync's stats have changed shape, so Nova can't read them."
FAILED = "SoulSync couldn't give your listening stats."

# What a window's answer must have to be one Nova can read, and the year's.
WINDOW_NEEDS = ("overview", "top_artists")
YEAR_NEEDS = ("totals",)


class ListeningUnavailable(Exception):
    """There are no listening stats to answer from. Its message is a sentence to say."""


class _ShapeChanged(Exception):
    """An answer from SoulSync wasn't in the shape Nova reads."""


@dataclass(frozen=True)
class Snapshot:
    """
    What SoulSync said of the user's listening at one refresh.

    Attributes:
        windows: SoulSync's cached answer for each range ("7d", "30d", "12m", "all"), with the
            keys as it gives them: overview, previous (None for all time), top_artists,
            top_albums, top_tracks, timeline, genres, clock, rhythm. Its lists use play_count.
        year: Its "Your Year" answer: period, totals, months, top_artists, top_albums,
            top_tracks, discoveries, peak_day and top_hour. Its lists use plays.
        health: The library's health, from the cached answer: total_tracks, unplayed_count,
            unplayed_percentage, format_breakdown and total_duration_ms.
        disk: The library's size on disk, as SoulSync's library-disk-usage gives it.
        pictures: Each fetched picture's bytes and content type, by the image address the
            stats gave. Only the top artists' and the discoveries' are fetched.
        at: When the refresh was made, as the clock says, or None before one has worked.
        problem: Why the latest refresh failed, as a sentence to say - None if it worked.
    """

    windows: Dict[str, Dict] = field(default_factory=dict)
    year: Dict = field(default_factory=dict)
    health: Dict = field(default_factory=dict)
    disk: Dict = field(default_factory=dict)
    pictures: Dict[str, Tuple[bytes, str]] = field(default_factory=dict)
    at: Optional[float] = None
    problem: Optional[str] = None


class Listening:
    """Reads SoulSync's listening stats every half hour, and holds the latest as a Snapshot."""

    def __init__(self, config, key: Optional[str], clock: Callable[[], float] = time.time):
        """
        Args:
            config: Configs loaded from config.yaml - soulsync.url, read at each refresh so that
                an address changed on the settings page is used at once.
            key: SOULSYNC_API_KEY from .env.
            clock: Gives the current epoch time, for when a snapshot was made.
        """
        self._config = config
        self._key = key
        self._clock = clock
        # Replaced whole, never changed, so a reader always has one refresh's stats and never a mix.
        self._snapshot: Optional[Snapshot] = None
        # Why no refresh has worked yet, when none has.
        self._problem: Optional[str] = None
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def snapshot(self) -> Snapshot:
        """The latest snapshot - an empty one, with the problem if there is one, before any refresh has worked."""
        return self._snapshot or Snapshot(problem=self._problem)

    def require(self) -> Snapshot:
        """
        The latest snapshot, to answer from.

        Raises:
            ListeningUnavailable: If no refresh has ever worked, saying why if it is known.
        """
        if self._snapshot is None:
            raise ListeningUnavailable(self._problem or NOT_YET)
        return self._snapshot

    def refresh(self) -> None:
        """
        Reads everything from SoulSync - the four windows, the year, the library's size and the
        pictures - and swaps in a new snapshot in one assignment. If anything fails, that is logged
        once, the old snapshot is kept, and the reason is recorded as its problem. Raises nothing.
        """
        try:
            soulsync = SoulSync.from_config(self._config, self._key)
            if soulsync is None:
                # Nothing to log: not using SoulSync is a choice, and from_config says if only half is set.
                self._fail(NOT_SET_UP)
                return
            fresh = self._read_everything(soulsync)
        except _ShapeChanged:
            self._fail(SHAPE_CHANGED, "its stats changed shape")
        except SoulSyncError as e:
            self._fail(e.reason or FAILED, "SoulSync gave an error")
        except Exception:
            # A bug, with its traceback: it must not end the thread, nor leave a half-made snapshot.
            logger.exception("Reading the listening stats failed")
            self._fail(FAILED)
        else:
            if self._snapshot is None or self._problem:
                logger.info("SoulSync's listening stats were read")
            self._problem = None
            self._snapshot = fresh

    def _fail(self, problem: str, kind: Optional[str] = None) -> None:
        """
        Records why a refresh failed, keeping the last good snapshot with the reason beside it.

        Args:
            problem: The sentence to say. It can hold SoulSync's own words, so it is never logged.
            kind: What kind of failure to log, in words of ours - SoulSync's own error line says
                the rest - or None to log nothing.
        """
        if kind:
            logger.warning(f"Listening stats not refreshed: {kind}")
        self._problem = problem
        if self._snapshot is not None:
            self._snapshot = replace(self._snapshot, problem=problem)

    def _read_everything(self, soulsync: SoulSync) -> Snapshot:
        """
        One whole read of SoulSync's stats.

        Args:
            soulsync: The SoulSync to ask, made for this refresh from the address as it is now.

        Raises:
            SoulSyncError: If SoulSync couldn't be asked, or answered with an error.
            _ShapeChanged: If an answer isn't in the shape Nova reads.
        """
        windows = {}
        for window in WINDOWS.values():
            answer = soulsync.stats("/api/stats/cached", range=window)
            _check(answer, WINDOW_NEEDS)
            windows[window] = answer
        year = soulsync.stats("/api/stats/year")
        _check(year, YEAR_NEEDS)
        disk = soulsync.stats("/api/stats/library-disk-usage")

        # Each picture once, however many lists share the artist.
        wanted: List[str] = []
        for artists in [window["top_artists"] for window in windows.values()] + [year.get("top_artists")]:
            wanted += _image_urls(artists)
        wanted += _image_urls(year.get("discoveries"))
        pictures = {}
        for url in dict.fromkeys(wanted):
            picture = soulsync.picture(url)
            if picture is not None:
                pictures[url] = picture

        # Library health is the library's, the same in every window: the all-time answer's is taken.
        health = windows[WINDOWS["all"]].get("health")
        return Snapshot(windows=windows, year=year, health=health if isinstance(health, dict) else {}, disk=disk,
                        pictures=pictures, at=self._clock(), problem=None)

    def start(self) -> None:
        """Starts the daemon thread which refreshes at once, then every REFRESH_EVERY seconds."""
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="listening")
        self._thread.start()

    def stop(self) -> None:
        """Stops the thread, waiting briefly for a refresh already under way to end."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stopping.is_set():
            self.refresh()
            self._stopping.wait(REFRESH_EVERY)


def _check(answer: Dict, needs: Tuple[str, ...]) -> None:
    """Raises _ShapeChanged unless the answer is a dict with each of the keys it needs."""
    if not isinstance(answer, dict) or any(key not in answer for key in needs):
        raise _ShapeChanged()


def _image_urls(artists) -> List[str]:
    """The picture addresses of the first PICTURES_OF entries of a list, passing over any with none."""
    if not isinstance(artists, list):
        return []
    urls = [entry.get("image_url") for entry in artists[:PICTURES_OF] if isinstance(entry, dict)]
    return [url for url in urls if isinstance(url, str) and url]
