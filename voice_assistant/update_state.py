"""
Update State Module.

What a satellite remembers about its updates, in one folder: the update it has begun and
is waiting to see running (pending.json), the file the installer's helper waits for
before it keeps the new code (confirmed), the result still to tell the brain
(result.json), and the latest failure (failed.json), so the same broken commit isn't
fetched and tried again and again.

Every write is atomic (storage.save_json), and reading a missing or broken file gives
nothing rather than an error - a satellite which can't read its notes carries on as if
there were none.
"""

import logging
import os
import time
from typing import Dict, Optional

from voice_assistant import storage

logger = logging.getLogger(__name__)

# Most "needs you" lines kept, so one result stays small.
_NEEDS_YOU_MOST = 3


class UpdateState:
    """
    The update records in one folder.

    Args:
        folder: Where the files live; made on the first write.
    """

    def __init__(self, folder: str):
        self._folder = folder

    def _path(self, name: str) -> str:
        return os.path.join(self._folder, name)

    def _read(self, name: str) -> Optional[Dict]:
        """A JSON file's dict, or None if it is missing, broken or not a dict."""
        try:
            data = storage.load_json(self._path(name))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _remove(self, name: str):
        try:
            os.remove(self._path(name))
        except FileNotFoundError:
            pass

    def failed_before(self, version: str) -> Optional[str]:
        """The error noted for this version in failed.json, else None."""
        failed = self._read("failed.json")
        if failed and failed.get("version") == version and isinstance(failed.get("error"), str):
            return failed["error"]
        return None

    def begin(self, version: str, previous: Optional[str]) -> None:
        """Notes an update begun: the version going in and the one it replaces."""
        storage.save_json(self._path("pending.json"),
                          {"version": version, "previous": previous, "at": time.time()})

    def _needs_you(self) -> list:
        """The notes file's lines as {why, command}, at most _NEEDS_YOU_MOST."""
        try:
            with open(self._path("notes")) as file:
                lines = file.read().splitlines()
        except OSError:
            return []
        found = []
        for line in lines:
            if not line.strip():
                continue
            why, _, command = line.partition("\t")
            found.append({"why": why, "command": command})
        return found[:_NEEDS_YOU_MOST]

    def confirm(self, running: Optional[str]) -> bool:
        """
        Confirms the pending update if the satellite is running it.

        Args:
            running: The version now running.

        Returns:
            True if pending.json named it: then confirmed, result.json are written and
            pending.json and notes removed. False, with nothing changed, otherwise.
        """
        pending = self._read("pending.json")
        if not pending or running is None or pending.get("version") != running:
            return False
        needs_you = self._needs_you()
        # The helper keeps the new code only once it sees this file.
        storage.save_text(self._path("confirmed"), running)
        storage.save_json(self._path("result.json"),
                          {"version": running, "ok": True, "needs_you": needs_you})
        self._remove("pending.json")
        self._remove("notes")
        return True

    def note_result(self, result: Dict) -> None:
        """Keeps a result to send; a failure is also kept as the latest in failed.json."""
        storage.save_json(self._path("result.json"), result)
        if not result.get("ok"):
            storage.save_json(self._path("failed.json"),
                              {"version": result.get("version"), "error": result.get("error")})

    def result_to_send(self) -> Optional[Dict]:
        """The result the brain hasn't been told yet, else None."""
        return self._read("result.json")

    def sent(self) -> None:
        """The brain has the result: forgets it."""
        self._remove("result.json")

    def clear_pending(self) -> None:
        """Forgets a pending update."""
        self._remove("pending.json")
