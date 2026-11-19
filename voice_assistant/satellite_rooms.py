"""
Satellite Rooms Module.

The brain keeps what it has learnt of each satellite - whether it records on a press
of a key or listens for a wake word, what machine it runs on and whether it saves
notes, and the room Home Assistant has it in - so a satellite is still known once it
has gone, and across a restart. All but the room come in the satellite's hello, and the
room from Home Assistant, which only a session's lookup finds out. It is kept in
satellite_rooms.json in Nova's data folder.

Every entry keeps push_to_talk and area, as the brain before the machine and notes
were kept needs to read the file - it throws the whole of it away otherwise.
"""

import logging
import threading
from typing import Dict, Iterable, List, Optional

from voice_assistant.machine import MACHINES
from voice_assistant.storage import load_json, save_json

logger = logging.getLogger(__name__)


class SatelliteRooms:
    """Each satellite's kind, machine, notes and room, kept in a file."""

    def __init__(self, path: str):
        """
        Reads the file once. One which can't be read, or isn't what was written, is
        not allowed to stop the brain starting: nothing is known until the satellites
        connect again, and the file is written over with what they say.

        Args:
            path: The file they are kept in.
        """
        self.path = path
        # Sessions each run on a thread of their own, and all say what they find.
        self._lock = threading.Lock()
        self._known: Dict[str, Dict] = {}
        try:
            stored = load_json(path, {})
            if not isinstance(stored, dict) or not all(
                    isinstance(name, str) and isinstance(each, dict) and isinstance(each.get("push_to_talk"), bool)
                    and (each.get("area") is None or isinstance(each["area"], str))
                    for name, each in stored.items()):
                raise ValueError("not each satellite's kind and room")
        except (OSError, ValueError) as e:
            logger.error(f"Could not read the satellites' rooms, so none are known until they connect "
                         f"({type(e).__name__}: {e})")
            return
        # The machine and notes are only extras, so a file from before them is read, and
        # one with something odd in them is read as not knowing, never thrown away.
        self._known = {name: {"push_to_talk": each["push_to_talk"], "area": each.get("area"),
                              "machine": each.get("machine") if each.get("machine") in MACHINES else None,
                              "saves_notes": each.get("saves_notes") is True}
                       for name, each in stored.items()}

    def _save(self):
        """Writes what's known. The caller holds the lock. One which can't be written is logged, and kept in memory."""
        try:
            save_json(self.path, self._known, indent=2)
        except (OSError, TypeError) as e:
            logger.error(f"Could not save the satellites' rooms: {e}")

    def seen(self, name: str, push_to_talk: bool, machine: Optional[str] = None, saves_notes: bool = False):
        """
        Notes what a satellite said it is as it connected. What it says now replaces
        what it said before, so a satellite which no longer says its machine is unknown.

        Args:
            name: The satellite's name.
            push_to_talk: Whether it records on a press of a key, not a wake word.
            machine: What kind of computer it runs on, one of machine.MACHINES - None if
                it doesn't say.
            saves_notes: Whether it saves notes in a Downloads folder.
        """
        said = {"push_to_talk": push_to_talk, "machine": machine, "saves_notes": saves_notes}
        with self._lock:
            each = self._known.get(name)
            if each is not None and all(each[key] == value for key, value in said.items()):
                return
            if each is None:
                each = self._known[name] = {"area": None}
            each.update(said)
            self._save()

    def set_area(self, name: str, area: Optional[str]):
        """
        Notes the room Home Assistant has a satellite in.

        Args:
            name: The satellite's name.
            area: The room, or None if it isn't in one, or isn't known - which leaves
                the room it was last known in, as Home Assistant being down shouldn't
                put it out of its room.
        """
        if area is None:
            return
        with self._lock:
            each = self._known.setdefault(name, {"push_to_talk": False, "area": None, "machine": None,
                                                 "saves_notes": False})
            if each["area"] == area:
                return
            each["area"] = area
            self._save()

    def push_to_talk(self, name: str) -> bool:
        """Returns whether a satellite records on a press of a key - False for one not known."""
        with self._lock:
            return self._known.get(name, {}).get("push_to_talk", False)

    def machine(self, name: str) -> Optional[str]:
        """Returns what kind of machine a satellite runs on, one of machine.MACHINES - None for one not known."""
        with self._lock:
            return self._known.get(name, {}).get("machine")

    def saves_notes(self, name: str) -> bool:
        """Returns whether a satellite saves notes in a Downloads folder - False for one not known."""
        with self._lock:
            return self._known.get(name, {}).get("saves_notes", False)

    def area(self, name: str) -> Optional[str]:
        """Returns the room a satellite is in, or None if it isn't known."""
        with self._lock:
            return self._known.get(name, {}).get("area")

    def room_of(self, name: str, among: Iterable[str]) -> List[str]:
        """
        Args:
            name: A satellite's name.
            among: The satellites to look through, e.g. every one which may connect.

        Returns:
            Every one of them in the same room as name, name first - or just name,
            if its room isn't known, which is a room of its own.
        """
        area = self.area(name)
        if area is None:
            return [name]
        return [name] + [other for other in among if other != name and self.area(other) == area]

    def always_on_in_room(self, name: str, among: Iterable[str]) -> Optional[str]:
        """
        Args:
            name: A satellite's name.
            among: The satellites to look through.

        Returns:
            The first other satellite in name's room which listens for a wake word, and
            so is always on - or None if there is none.
        """
        for other in self.room_of(name, among):
            if other != name and not self.push_to_talk(other):
                return other
        return None
