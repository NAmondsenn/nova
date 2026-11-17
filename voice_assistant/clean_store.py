"""
Clean Store Module
What Nova has heard of each song, the lists the settings page shows, the swaps in progress and
the library check's progress and what the nightly check and weekly note have done, kept in one JSON file in the data folder (decision 0022,
sections 4 and 5).

A swap's saved state says which file was moved where, so a restart can carry it on or put the
song back. Losing it could leave a song in the holding folder for good, so every save is atomic
(storage.save_json) and a file which can't be read is set aside, never trusted and never
allowed to stop Nova starting.

Verdicts and Held are stored as plain dicts (dataclasses.asdict); turning them back is the
caller's job.
"""

import copy
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from voice_assistant import storage
from voice_assistant.clean_ear import Verdict

logger = logging.getLogger(__name__)

# The lists the settings page shows: songs swapped for an explicit copy, songs which only come
# down clean, songs it couldn't decide on, and songs it couldn't swap.
KINDS = ("swapped", "only_clean", "unsure", "couldnt")
# A list never grows past this; the oldest entries fall off the end.
LIST_LIMIT = 200

# The type each field of the check's saved progress must have; one that hasn't is left out,
# so a hand-edited "done": "3" can't break the check.
CHECK_TYPES = {"running": (bool,), "paused": (bool,), "done": (int,), "total": (int,), "current": (str, type(None)),
               "queue": (list, type(None)), "last_finished": (int, float, type(None)), "error": (str, type(None))}
# The same for the nightly check's and weekly note's record (see clean_schedule.py), so a hand-edited
# "night_done": 20261117 can't break them.
SCHEDULE_TYPES = {"night_done": (str,), "week_sent": (str,), "note_at": (int, float)}


class CleanStore:
    """
    Thread-safe. A change is made to a copy and saved before it is kept, so a save which fails
    leaves the store as it was; the state is never changed in place, and everything handed
    out is a copy.
    """

    def __init__(self, path: str, clock: Optional[Callable[[], float]] = None):
        """
        Args:
            path: The file to keep it all in, e.g. clean_songs.json in the data folder.
            clock: Gives the time in epoch seconds, to stamp each list entry with when it was
                added; the wall clock by default.
        """
        self.path = path
        self._clock = clock or time.time
        self._lock = threading.RLock()
        # Set when the file is there but couldn't be read or set aside: it may hold swaps with
        # songs in .nova-held, so it's never written over, and nothing is saved until a restart.
        self._no_saves = False
        self._state = self._load()

    def verdict(self, song_id: str) -> Optional[Dict]:
        """
        Args:
            song_id: Navidrome's id for the song.

        Returns:
            What was heard of it, e.g. {"size": 4096, "updated": "2026-11-10T09:30:00Z",
            "name": "Gunna - Endless", "verdict": {"kind": "clean", "expected": 12, "heard": 1,
            "reason": ""}}, or None if it hasn't been judged.
        """
        with self._lock:
            return copy.deepcopy(self._state["verdicts"].get(song_id))

    def set_verdict(self, song_id: str, size: int, updated: str, verdict: Verdict, name: str):
        """
        Keeps what was heard of a song, replacing any earlier verdict.

        Args:
            song_id: Navidrome's id for the song.
            size: The file's size in bytes, from Navidrome's native API.
            updated: Navidrome's updatedAt for it, as given, e.g. "2026-11-10T09:30:00.123456789Z".
            verdict: What was made of it.
            name: What the song is called, e.g. "Gunna - Endless".

        Raises:
            OSError if it couldn't be saved; the store is as it was.
        """
        record = {"size": size, "updated": updated, "name": name, "verdict": _plain(verdict)}
        with self._lock:
            verdicts = dict(self._state["verdicts"])
            verdicts[song_id] = record
            self._keep("verdicts", verdicts)

    def known(self, song_id: str, size: int, updated: str) -> bool:
        """
        Args:
            song_id: Navidrome's id for the song.
            size: The file's size now.
            updated: Navidrome's updatedAt for it now.

        Returns:
            True if it was judged with exactly this size and updated, so the file is the one
            which was heard. A changed file is not known.
        """
        with self._lock:
            record = self._state["verdicts"].get(song_id)
        return record is not None and record.get("size") == size and record.get("updated") == updated

    def listed(self, kind: str) -> List[Dict]:
        """
        Args:
            kind: "swapped", "only_clean", "unsure" or "couldnt".

        Returns:
            That list, newest first, e.g. [{"name": "Gunna - Endless", "reason": ""}].

        Raises:
            ValueError for any other kind.
        """
        _check_kind(kind)
        with self._lock:
            return copy.deepcopy(self._state["lists"][kind])

    def add(self, kind: str, name: str, reason: str = "", **extra):
        """
        Puts a song at the front of a list. A name is on a list once: adding it again moves it
        to the front with the new reason (and the new extra), and the oldest entries past 200
        fall off.

        Args:
            kind: "swapped", "only_clean", "unsure" or "couldnt".
            name: What the song is called, e.g. "Gunna - Endless".
            reason: Why, shown on the page, e.g. "SoulSync found no explicit copy".
            **extra: Anything else the page shows, e.g. song_id. The entry also gets "at", the
                time it was added in epoch seconds, unless one is given here: the weekly note
                counts the week's entries by it. Entries saved before this have none, and count
                as old.

        Raises:
            ValueError for any other kind. OSError if it couldn't be saved; the store is as it was.
        """
        _check_kind(kind)
        entry = {"at": self._clock(), **extra, "name": name, "reason": reason}
        with self._lock:
            entries = [entry] + [e for e in self._state["lists"][kind] if e.get("name") != name]
            lists = dict(self._state["lists"])
            lists[kind] = copy.deepcopy(entries[:LIST_LIMIT])
            self._keep("lists", lists)

    def discard(self, kind: str, song_id: str):
        """
        Takes a song off a list by its Navidrome id, so a song which has since ended another way
        isn't on two. One not there is no error.

        Args:
            kind: "swapped", "only_clean", "unsure" or "couldnt".
            song_id: The id the entry was added with.

        Raises:
            ValueError for any other kind. OSError if it couldn't be saved; the store is as it was.
        """
        _check_kind(kind)
        with self._lock:
            entries = [e for e in self._state["lists"][kind] if e.get("song_id") != song_id]
            if len(entries) != len(self._state["lists"][kind]):
                lists = dict(self._state["lists"])
                lists[kind] = entries
                self._keep("lists", lists)

    def swaps(self) -> List[Dict]:
        """
        Returns:
            The swaps in progress, oldest first. Each has key, name, title, artist, album, held
            (a Held as a dict), tries, track_id, asked_at and limit.
        """
        with self._lock:
            return copy.deepcopy(self._state["swaps"])

    def save_swap(self, swap: Dict):
        """
        Keeps a swap's progress, replacing the swap with the same key, or adding it.

        Args:
            swap: The swap, with the key the caller chose for it (a string).

        Raises:
            ValueError if it has no key. OSError or TypeError if it couldn't be saved; the
            store is as it was.
        """
        if not isinstance(swap, dict) or not isinstance(swap.get("key"), str):
            raise ValueError("A swap needs a string key")
        swap = copy.deepcopy(swap)
        with self._lock:
            swaps = list(self._state["swaps"])
            for position, saved in enumerate(swaps):
                if saved.get("key") == swap["key"]:
                    swaps[position] = swap
                    break
            else:
                swaps.append(swap)
            self._keep("swaps", swaps)

    def end_swap(self, key: str):
        """
        Forgets a swap which is over, finished or put back. One not there is no error.

        Args:
            key: The swap's key.

        Raises:
            OSError if it couldn't be saved; the store is as it was.
        """
        with self._lock:
            swaps = [s for s in self._state["swaps"] if s.get("key") != key]
            if len(swaps) != len(self._state["swaps"]):
                self._keep("swaps", swaps)

    def check(self) -> Dict:
        """
        Returns:
            The library check's progress as last saved: running, done, total, current, queue
            (song ids still to do) and last_finished; empty if there has been none.
        """
        with self._lock:
            return copy.deepcopy(self._state["check"])

    def save_check(self, state: Dict):
        """
        Keeps the library check's progress.

        Args:
            state: running, done, total, current, queue and last_finished.

        Raises:
            OSError or TypeError if it couldn't be saved; the store is as it was.
        """
        with self._lock:
            self._keep("check", copy.deepcopy(state))

    def schedule(self) -> Dict:
        """
        Returns:
            What the nightly check and the weekly note have done, as last saved: night_done, the
            day of the last night's check ("2026-11-17"), week_sent, the week of the last note
            ("2026-W47"), and note_at, when that was (epoch seconds); empty if nothing yet.
        """
        with self._lock:
            return copy.deepcopy(self._state["schedule"])

    def save_schedule(self, record: Dict):
        """
        Keeps what the nightly check and the weekly note have done.

        Args:
            record: night_done, week_sent and note_at.

        Raises:
            OSError or TypeError if it couldn't be saved; the store is as it was.
        """
        with self._lock:
            self._keep("schedule", copy.deepcopy(record))

    def _keep(self, part: str, value: Any):
        """
        Saves the whole file with one part changed, and only then keeps the change.

        Raises:
            OSError if it couldn't be saved, or the file couldn't be read as Nova started.
        """
        if self._no_saves:
            raise OSError(f"{self.path} couldn't be read, so it isn't written over")
        new = dict(self._state)
        # Through JSON, so what is held is what a reload would give: a Held's sidecar
        # tuples are lists then, and should be before as well.
        new[part] = json.loads(json.dumps(value))
        storage.save_json(self.path, new)
        self._state = new

    def _load(self) -> Dict:
        """
        Returns:
            What the file holds, with each part of the right type: a part which isn't starts
            empty with a warning. A file which isn't JSON, or isn't a dictionary, is set aside
            and everything starts empty. One which can't be read at all starts empty too, but
            is left as it is and never saved over.
        """
        state = {"verdicts": {}, "lists": {kind: [] for kind in KINDS}, "swaps": [], "check": {}, "schedule": {}}
        try:
            saved = storage.load_json(self.path, {})
        except ValueError as e:
            logger.warning(f"Couldn't read {self.path}, starting empty: {e}")
            self._set_aside()
            return state
        except OSError as e:
            # Not corrupt but unreadable for now, e.g. PermissionError: its swaps may be the only
            # record of where songs are held, so it's left whole for the next start to read.
            logger.error(f"Couldn't read {self.path} ({type(e).__name__}), so nothing is saved to it "
                         f"until Nova restarts")
            self._no_saves = True
            return state
        if not isinstance(saved, dict):
            logger.warning(f"{self.path} isn't a dictionary, starting empty")
            self._set_aside()
            return state

        for part in ("verdicts", "check", "schedule"):
            if part in saved:
                if isinstance(saved[part], dict):
                    state[part] = saved[part]
                else:
                    logger.warning(f"{self.path} has a '{part}' which isn't a dictionary, so it starts empty")
        # A verdict that isn't a dictionary, or whose verdict isn't, would break known() and the check.
        verdicts = {song_id: record for song_id, record in state["verdicts"].items()
                    if isinstance(record, dict) and isinstance(record.get("verdict", {}), dict)}
        if len(verdicts) != len(state["verdicts"]):
            logger.warning(f"{self.path} had {len(state['verdicts']) - len(verdicts)} unusable verdicts, left out")
        state["verdicts"] = verdicts
        state["check"] = self._typed(state["check"], CHECK_TYPES, "check")
        state["schedule"] = self._typed(state["schedule"], SCHEDULE_TYPES, "schedule")
        if "lists" in saved:
            if isinstance(saved["lists"], dict):
                for kind in KINDS:
                    state["lists"][kind] = self._entries(saved["lists"].get(kind, []), kind)
            else:
                logger.warning(f"{self.path} has 'lists' which isn't a dictionary, so they start empty")
        if "swaps" in saved:
            # A swap with no key can't be found again to be ended, so it isn't carried on.
            state["swaps"] = self._entries(saved["swaps"], "swaps", needs="key")
        return state

    def _typed(self, fields: Dict, types: Dict, what: str) -> Dict:
        """
        The fields of a saved part which are of their type - a bool is no number - with any
        others left out, and a warning.
        """
        kept = {field: value for field, value in fields.items()
                if field not in types or (isinstance(value, types[field])
                                          and not (isinstance(value, bool) and bool not in types[field]))}
        if len(kept) != len(fields):
            logger.warning(f"{self.path} had {what} fields of the wrong type, left out: "
                           f"{sorted(set(fields) - set(kept))}")
        return kept

    def _entries(self, found: Any, what: str, needs: str = "name") -> List[Dict]:
        """The dicts in a saved list which have the key they need; anything else is dropped with a warning."""
        if not isinstance(found, list):
            logger.warning(f"{self.path} has '{what}' which isn't a list, so it starts empty")
            return []
        kept = [e for e in found if isinstance(e, dict) and needs in e]
        if len(kept) != len(found):
            logger.warning(f"{self.path} had {len(found) - len(kept)} unusable entries in '{what}', left out")
        return kept

    def _set_aside(self):
        """
        Renames an unreadable file to clean_songs.json.bad, or .bad.1, .bad.2 and so on if one is
        already there, so a file is never overwritten and can still be looked at. If it can't be
        renamed it is left, and nothing is saved over it until a restart.
        """
        target = self.path + ".bad"
        number = 0
        while os.path.exists(target):
            number += 1
            target = f"{self.path}.bad.{number}"
        try:
            os.rename(self.path, target)
        except OSError as e:
            logger.error(f"Couldn't set {self.path} aside, so nothing is saved to it until Nova restarts: {e}")
            self._no_saves = True
        else:
            logger.warning(f"Kept the unreadable file as {target}")


def _check_kind(kind: str):
    if kind not in KINDS:
        raise ValueError(f"Not a list: {kind!r}")


def _plain(verdict: Any) -> Dict:
    """A Verdict as a plain dict, or a dict as it is."""
    if isinstance(verdict, dict):
        return dict(verdict)
    return {"kind": verdict.kind, "expected": verdict.expected, "heard": verdict.heard,
            "reason": verdict.reason}
