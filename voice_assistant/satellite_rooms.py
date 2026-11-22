"""
Satellite Rooms Module.

The brain keeps what it has learnt of each satellite - whether it records on a press
of a key or listens for a wake word, what machine it runs on and whether it saves
notes, and the room Home Assistant has it in - so a satellite is still known once it
has gone, and across a restart. All but the room come in the satellite's hello, and the
room from Home Assistant, which only a session's lookup finds out. It is kept in
satellite_rooms.json in Nova's data folder. The other names he has given a satellite on
the settings page are kept with them: they aren't the satellite's to say.

Every entry keeps push_to_talk and area, as the brain before the machine, notes and
names were kept needs to read the file - it throws the whole of it away otherwise.
"""

import logging
import threading
from typing import Any, Dict, Iterable, List, Optional

from voice_assistant import shortcut
from voice_assistant.machine import MACHINES
from voice_assistant.storage import load_json, save_json

logger = logging.getLogger(__name__)

# The machines with a screen and a sensor he is at, which are let approve by default -
# but only as the first way set up: see seen.
APPROVING_MACHINES = ("pc", "mac")


def _blank() -> Dict:
    """Returns the entry of a satellite nothing is known of, with every field the file keeps."""
    return {"push_to_talk": False, "area": None, "machine": None, "saves_notes": False, "called": [],
            "opens_things": True, "app_names": {}, "pc_device": None,
            "approves": False, "can_approve": None, "shortcut": shortcut.DEFAULT}


def _shortcut(stored: Any) -> str:
    """
    Returns the shortcut read from the file, as shortcut.written gives it - the default if what's there
    isn't one, as for a file from before shortcuts or one edited by hand. An empty one stays empty: that
    is his choice of none.
    """
    try:
        return shortcut.written(shortcut.read(stored))
    except shortcut.ShortcutError:
        return shortcut.DEFAULT


def _names(stored: Any) -> List[str]:
    """Returns the names read from the file, or none if what's there isn't a list of names."""
    if isinstance(stored, list) and all(isinstance(each, str) for each in stored):
        return list(stored)
    return []


def _aliases(stored: Any) -> Dict[str, str]:
    """Returns the app names read from the file, or none if what's there isn't text for text."""
    if isinstance(stored, dict) and all(isinstance(key, str) and isinstance(each, str) for key, each in stored.items()):
        return dict(stored)
    return {}


class SatelliteRooms:
    """Each satellite's kind, machine, notes, room and other names, kept in a file."""

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
        # The machine, notes and names are only extras, so a file from before them is read, and
        # one with something odd in them is read as not knowing, never thrown away.
        self._known = {name: {"push_to_talk": each["push_to_talk"], "area": each.get("area"),
                              "machine": each.get("machine") if each.get("machine") in MACHINES else None,
                              "saves_notes": each.get("saves_notes") is True,
                              "called": _names(each.get("called")),
                              # On unless he turned it off, so a file from before has it on.
                              "opens_things": each.get("opens_things") is not False,
                              "app_names": _aliases(each.get("app_names")),
                              "pc_device": each.get("pc_device") if isinstance(each.get("pc_device"), str) else None,
                              "approves": each.get("approves") is True,
                              # None is no switch stored yet: no, until its hello stores one.
                              "can_approve": each["can_approve"] if isinstance(each.get("can_approve"), bool) else None,
                              # Not there before shortcuts, which is the default rather than none.
                              "shortcut": _shortcut(each["shortcut"]) if "shortcut" in each else shortcut.DEFAULT}
                       for name, each in stored.items()}

    def _save(self):
        """Writes what's known. The caller holds the lock. One which can't be written is logged, and kept in memory."""
        try:
            save_json(self.path, self._known, indent=2)
        except (OSError, TypeError) as e:
            logger.error(f"Could not save the satellites' rooms: {e}")

    def seen(self, name: str, push_to_talk: bool, machine: Optional[str] = None, saves_notes: bool = False,
             approves: bool = False, guarded: bool = True):
        """
        Notes what a satellite said it is as it connected. What it says now replaces
        what it said before, so a satellite which no longer says its machine is unknown.

        The first time a PC or Mac says it approves - or one already known says it's
        now a PC or Mac - its "Can approve" is stored: on only if no way to approve was
        set up then, as the first way he sets up; else off until he switches it on with
        an approval. Otherwise anything with a satellite's password could say it's the
        PC and be asked, and answer, for itself.

        Args:
            name: The satellite's name.
            push_to_talk: Whether it records on a press of a key, not a wake word.
            machine: What kind of computer it runs on, one of machine.MACHINES - None if
                it doesn't say.
            saves_notes: Whether it saves notes in a Downloads folder.
            approves: Whether it can ask for a fingerprint to approve a big action.
            guarded: Whether a way to approve is set up already - see Approvals.guarded.
                Taken as yes unless the caller says, so a "Can approve" is never on by mistake.
        """
        said = {"push_to_talk": push_to_talk, "machine": machine, "saves_notes": saves_notes,
                "approves": approves}
        with self._lock:
            each = self._known.get(name)
            first = approves and machine in APPROVING_MACHINES and (
                each is None or not isinstance(each.get("can_approve"), bool)
                or each.get("machine") not in APPROVING_MACHINES)
            if each is not None and not first and all(each[key] == value for key, value in said.items()):
                return
            if each is None:
                each = self._known[name] = _blank()
            each.update(said)
            if first:
                each["can_approve"] = not guarded
                logger.info(f"The {name} satellite {'may' if not guarded else 'may not yet'} approve")
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
            each = self._known.setdefault(name, _blank())
            if each["area"] == area:
                return
            each["area"] = area
            self._save()

    def set_called(self, name: str, called: List[str]):
        """
        Notes the other names he has given a satellite, as the settings page saves them.

        Args:
            name: The satellite's name. One not seen yet is made an entry of its own, as
                seen would, which its hello then fills in.
            called: The names, as the page checked them - none to clear them.

        Raises:
            OSError: If they couldn't be saved. They aren't kept in memory either then,
                as the page tells him it failed.
        """
        with self._lock:
            updated = {**self._known.get(name, _blank()), "called": list(called)}
            save_json(self.path, {**self._known, name: updated}, indent=2)
            self._known[name] = updated

    def _set_field(self, name: str, field: str, value: Any):
        """
        Saves one field of a satellite, as set_called does.

        Raises:
            OSError: If it couldn't be saved. It isn't kept in memory either then.
        """
        with self._lock:
            updated = {**self._known.get(name, _blank()), field: value}
            save_json(self.path, {**self._known, name: updated}, indent=2)
            self._known[name] = updated

    def set_opens_things(self, name: str, on: bool):
        """
        Notes whether he lets a satellite open apps and pages, as the settings page saves it.

        Args:
            name: The satellite's name.
            on: Whether it may.

        Raises:
            OSError: If it couldn't be saved.
        """
        self._set_field(name, "opens_things", on)

    def set_can_approve(self, name: str, on: bool):
        """
        Notes whether he lets a satellite ask for his approval, as the settings page saves it.

        Args:
            name: The satellite's name.
            on: Whether it may.

        Raises:
            OSError: If it couldn't be saved.
        """
        self._set_field(name, "can_approve", on)

    def forget_approval(self, name: str, guarded: Optional[bool]):
        """
        Undoes a satellite's "Can approve" as its password is renewed, or it's added or
        removed: a new password is a new satellite, which mustn't inherit the PC's
        switch, or anyone who could make one could connect a fake saying it's the PC
        and approves, and be asked, and answer, for itself. As seen's first time, it's
        on only if no way to approve is set up at that moment, else off until he
        switches it on with an approval - and only for a PC or Mac, as seen has it: any
        other is left with none stored, a no. A removed one forgets it entirely, so it's
        stored afresh by its next hello. One nothing is known of is left alone - its
        hello stores it. Memory is changed before the file, so a satellite the file
        can't be written for is still not asked while the brain runs.

        Args:
            name: The satellite's name.
            guarded: Whether a way to approve is set up now - see Approvals.guarded - or
                None if it's being removed.

        Raises:
            OSError: If it couldn't be saved, so the caller can stop short of re-keying.
        """
        with self._lock:
            each = self._known.get(name)
            if each is None:
                return
            if guarded is None:
                each.update(approves=False, can_approve=None)
            elif each.get("machine") in APPROVING_MACHINES:
                each["can_approve"] = not guarded
            else:
                # Not a PC or Mac, so never on by the first set-up rule: none stored, a no.
                each["can_approve"] = None
            logger.info(f"The {name} satellite {'may' if each['can_approve'] else 'may not'} approve "
                        f"until it's switched")
            save_json(self.path, self._known, indent=2)

    def set_app_names(self, name: str, names: Dict[str, str]):
        """
        Notes the short names he has given a satellite's apps, as the settings page saves them.

        Args:
            name: The satellite's name.
            names: Each name as plain gives it, to the app's name as the satellite lists it - none to clear them.

        Raises:
            OSError: If they couldn't be saved.
        """
        self._set_field(name, "app_names", dict(names))

    def set_pc_device(self, name: str, device: Optional[str]):
        """
        Notes which HASS.Agent PC a satellite is, as the settings page saves it.

        Args:
            name: The satellite's name.
            device: The PC's Home Assistant device id, or None for none chosen.

        Raises:
            OSError: If it couldn't be saved.
        """
        self._set_field(name, "pc_device", device)

    def set_shortcut(self, name: str, text: str):
        """
        Notes the keys a computer listens on for a press, as the settings page saves them.

        Args:
            name: The satellite's name.
            text: The shortcut as shortcut.written gives it, or "" for none.

        Raises:
            OSError: If it couldn't be saved.
        """
        self._set_field(name, "shortcut", text)

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

    def called(self, name: str) -> List[str]:
        """Returns the other names he has given a satellite - none for one not known or never given any."""
        with self._lock:
            return list(self._known.get(name, {}).get("called", []))

    def opens_things(self, name: str) -> bool:
        """Returns whether he lets a satellite open apps and pages - True unless he turned it off, and for one not known."""
        with self._lock:
            return self._known.get(name, {}).get("opens_things", True)

    def approves(self, name: str) -> bool:
        """Returns whether a satellite said it can ask for approval - False for one not known."""
        with self._lock:
            return self._known.get(name, {}).get("approves", False)

    def can_approve(self, name: str) -> bool:
        """
        Returns whether a satellite is asked to approve big actions: its switch, as stored
        when it first said it's a PC or Mac which approves, or as he set it on the settings
        page - else no, for one never stored either way.
        """
        with self._lock:
            return self._known.get(name, {}).get("can_approve") is True

    def approving(self, among: Iterable[str]) -> List[str]:
        """
        Args:
            among: The satellites to look through, e.g. every one which may connect.

        Returns:
            Those which said they can approve and which he lets, connected or not - each
            is a way to approve, even asleep, so a change to how approval works needs one.
        """
        return [name for name in among if self.approves(name) and self.can_approve(name)]

    def app_names(self, name: str) -> Dict[str, str]:
        """Returns the short names he has given a satellite's apps, as plain name to app name - none if never given any."""
        with self._lock:
            return dict(self._known.get(name, {}).get("app_names", {}))

    def pc_device(self, name: str) -> Optional[str]:
        """Returns the device id of the HASS.Agent PC he chose for a satellite, or None if he hasn't."""
        with self._lock:
            return self._known.get(name, {}).get("pc_device")

    def shortcut(self, name: str) -> str:
        """Returns the keys a computer listens on, as shortcut.written gives them - "" for none, the default if never chosen."""
        with self._lock:
            return self._known.get(name, {}).get("shortcut", shortcut.DEFAULT)

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
