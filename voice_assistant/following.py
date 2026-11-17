"""
Following Module.

Follows the user's top artists of the last 12 months on SoulSync's watchlist, once a month, so that
SoulSync's daily watchlist scan downloads their new releases. Only ever adds: an artist he stops
playing stays followed, and one already watched - by name, in any capitals, or by any of its ids - is
left as it is. A newly followed artist brings their last month's releases, as SoulSync looks back that
far for an artist it has never scanned, then each new one; never their back catalogue.

The stats give each top artist's SoulSync library id, which the watchlist can't take as it is, so the
library is asked for the artist's provider ids and the artist added by those (see SoulSync.follow).
Those are SoulSync's own ids for that library artist, as its own watch button uses, which is what makes
the right artist followed. Each new watchlist row's name is also read back, and one which isn't the
artist listened to is taken off again - kept in the record until it is - though as SoulSync stores the
name sent, this is only a defence, and can't catch a wrong id.

FollowWatcher, a thread, looks once an hour and once as it starts, and follows at the first look of
each calendar month from 3 in the morning - on any day of it, so it also runs the day it's set up.
Done, the month is noted in a small record in the data folder, so a restart neither runs it twice nor
loses it. SoulSync not set up or not answering, or the stats not read yet, leaves the month to the
next look.

His listening is never logged: what is said is counts, never an artist.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set

from voice_assistant import storage
from voice_assistant.listening import ListeningUnavailable
from voice_assistant.navidrome import normalise
from voice_assistant.soulsync import KEY_REFUSED, NOT_ANSWERING, PROVIDERS, SoulSync, SoulSyncError

# Logger setup
logger = logging.getLogger(__name__)

# Seconds between the watcher's looks at whether this month's following is due.
LOOK_EVERY = 3600

# The hour of the morning, local time, from which a month's following is done: after SoulSync's own
# nightly work, and well before he's up to want anything of it.
FROM_HOUR = 3

# The window of the listening stats whose top artists are followed.
WINDOW = "12m"

# The watchlist row's columns holding a provider's id, and the library artist's.
ROW_COLUMNS = tuple(column for _, column, _ in PROVIDERS)
LIBRARY_COLUMNS = tuple(column for _, _, column in PROVIDERS)


class _Gone(Exception):
    """SoulSync stopped answering, or stopped taking its key, part way: the month is left to the next look."""


def _ids(item: Dict, columns) -> Set[str]:
    """The provider ids a watchlist row or a library artist holds, in the given columns."""
    return {str(item[column]).strip() for column in columns if str(item.get(column) or "").strip()}


def _folded(name) -> str:
    """A name as SoulSync's watchlist compares them: in any capitals."""
    return str(name or "").strip().casefold()


class FollowWatcher:
    """
    Looks once an hour, and once as it starts, at whether this month's top artists have been followed,
    and follows them if not - see the module's docstring.
    """

    def __init__(self, listening, soulsync_factory: Callable[[], Optional[SoulSync]], store_path: str,
                 settings: Callable[[], Dict], clock: Callable[[], float] = time.time):
        """
        Args:
            listening: Gives the snapshot whose top artists are followed: its require() raises
                ListeningUnavailable until SoulSync has been read, when the look is tried again an hour later.
            soulsync_factory: Gives the SoulSync to ask, made at each look from the settings as they are now,
                or None while it isn't set up.
            store_path: The record of the month done and any artist still to take off again,
                following.json in the data folder.
            settings: Gives the new_music section of the settings as they are now, read at each look:
                follow_top_artists, how many of the top artists to follow, 0 for none. A change on the
                settings page is used at the next following - this month's if it isn't done yet, or else
                next month's, so a rise after this month's waits for next month.
            clock: Gives the current epoch time, only replaced by the tests.
        """
        self._listening = listening
        self._soulsync = soulsync_factory
        self._path = store_path
        self._settings = settings
        self._clock = clock
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Starts the daemon thread, which looks at once and then every LOOK_EVERY seconds."""
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="following")
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
                logger.exception("Following the top artists failed, will try again")
            self._stopping.wait(LOOK_EVERY)

    # The record

    def _load(self) -> Dict:
        """The record: {"month": the last month done, e.g. "2026-11", "undo": rows still to take off}."""
        try:
            record = storage.load_json(self._path, {})
        except (OSError, ValueError):
            # Read as nothing done: following again only finds them followed already.
            logger.warning("The following record couldn't be read, so this month's is done again")
            record = {}
        record = record if isinstance(record, dict) else {}
        undo = record.get("undo")
        return {"month": record.get("month"), "undo": [row for row in undo if isinstance(row, dict)]
                if isinstance(undo, list) else []}

    def _save(self, record: Dict):
        storage.save_json(self._path, record)

    # A look

    def look(self):
        """
        One look: takes off any wrong artist left from before, then follows this month's top artists if
        they're due and not done yet.
        """
        # The local time as the system's zone has it - 3 in the morning is the user's, not UTC's.
        now = datetime.fromtimestamp(self._clock())
        month = f"{now.year}-{now.month:02d}"
        record = self._load()
        if record["undo"]:
            self._undo(record)
        wanted = (self._settings() or {}).get("follow_top_artists")
        wanted = wanted if isinstance(wanted, int) and not isinstance(wanted, bool) else 0
        if wanted <= 0 or record["month"] == month or now.hour < FROM_HOUR:
            return
        soulsync = self._soulsync()
        if soulsync is None:
            return
        try:
            snapshot = self._listening.require()
        except ListeningUnavailable:
            # Not why: that can be SoulSync's own words, and those are never logged.
            logger.info("Top artists not followed yet, listening isn't available")
            return
        top = (snapshot.windows.get(WINDOW) or {}).get("top_artists")
        top = [artist for artist in top if isinstance(artist, dict)][:wanted] if isinstance(top, list) else []
        try:
            self._follow(soulsync, top, record)
        except _Gone:
            logger.info("Top artists not all followed yet, will try again at the next look")
            return
        record["month"] = month
        self._save(record)

    def _undo(self, record: Dict):
        """Takes off each wrong artist left in the record, keeping any SoulSync still won't take off."""
        soulsync = self._soulsync()
        if soulsync is None:
            return
        left = []
        for row in record["undo"]:
            try:
                soulsync.unfollow(row)
            except SoulSyncError:
                left.append(row)
        if len(left) < len(record["undo"]):
            logger.info(f"Took {len(record['undo']) - len(left)} wrong artists off SoulSync's watchlist")
            record["undo"] = left
            self._save(record)

    def _follow(self, soulsync: SoulSync, top: List[Dict], record: Dict):
        """
        Follows each of the top artists not yet watched, and logs how many of each kind there were.

        Raises:
            _Gone: If SoulSync stopped answering, or taking its key, part way - those followed already
                are found watched at the next look.
        """
        try:
            watched = soulsync.watched_artists()
        except SoulSyncError:
            raise _Gone() from None
        names = {_folded(row.get("artist_name")) for row in watched}
        ids = set().union(*(_ids(row, ROW_COLUMNS) for row in watched))
        rows_before = {row.get("id") for row in watched}
        counts = {"followed": 0, "already": 0, "missing": 0, "undone": 0, "refused": 0}
        for artist in top:
            if _folded(artist.get("name")) in names:
                counts["already"] += 1
                continue
            try:
                kind = self._follow_one(soulsync, artist, names, ids, rows_before, record)
            except SoulSyncError as e:
                if e.reason in (NOT_ANSWERING, KEY_REFUSED):
                    raise _Gone() from None
                # SoulSync refused this one: tried again next month.
                kind = "refused"
            counts[kind] += 1
        logger.info(f"Followed {counts['followed']} of your top {len(top)} artists on SoulSync's watchlist: "
                    f"{counts['already']} already watched, {counts['missing']} not found, "
                    f"{counts['undone']} undone, {counts['refused']} refused")

    def _follow_one(self, soulsync: SoulSync, artist: Dict, names: Set[str], ids: Set[str], rows_before: Set,
                    record: Dict) -> str:
        """
        Follows one top artist, unless it's watched by one of its ids, adding it to names and ids once
        it's followed.

        Returns:
            Which it was: "followed", "already" watched, "missing" - not in the library, naming another
            artist there now, or with no provider id - or "undone", followed as another artist and taken
            off again.
        """
        # The stats give no id for an artist with no library artist of the name: a collaboration, say.
        library_id = artist.get("id")
        found = soulsync.library_artist(str(library_id)) if library_id not in (None, "") else None
        # The stats are up to half an hour old, so the id is checked to be the same artist's still.
        if found is None or normalise(found.get("name") or "") != normalise(artist.get("name") or ""):
            return "missing"
        if _ids(found, LIBRARY_COLUMNS) & ids:
            return "already"
        row = soulsync.follow(found)
        if row is None:
            return "missing"
        if row.get("id") in rows_before:
            # Added to a row which was there before, under another name: his, so left as it is.
            return "already"
        if normalise(row.get("artist_name") or "") == normalise(artist.get("name") or ""):
            names.add(_folded(row.get("artist_name")))
            ids.update(_ids(row, ROW_COLUMNS))
            return "followed"
        # One SoulSync won't take off now is kept in the record, so a later look takes it off, whatever restarts.
        undo = {column: row[column] for column in ROW_COLUMNS if row.get(column)}
        try:
            soulsync.unfollow(undo)
        except SoulSyncError:
            record["undo"].append(undo)
            self._save(record)
        return "undone"
