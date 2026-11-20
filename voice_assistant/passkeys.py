"""
Passkeys Module.

The passkeys he has added for approving big actions, kept in passkeys.json in Nova's
data folder: each one's credential id, public key, signature counter, name and the
origin it was made for. Only the public half is ever kept, so the file can't approve
anything by itself, but it is still private to this user and never logged.
"""

import logging
import threading
from dataclasses import asdict, dataclass
from typing import List, Optional

from voice_assistant.storage import load_json, save_json

logger = logging.getLogger(__name__)

# Enough for a phone, a PC and a spare, so a lost one can be replaced without a locked-out moment.
MOST_PASSKEYS = 3
# Long enough for "Nathaniel's Pixel", short enough to fit a row of the settings page.
LONGEST_NAME = 30


@dataclass
class Passkey:
    """One passkey: the credential and what is needed to check what it signs."""

    id: str  # base64url credential id
    name: str
    public_key: str  # base64url SPKI DER
    algorithm: int  # COSE algorithm, -7 (ES256) or -257 (RS256)
    counter: int  # the last signature counter seen
    origin: str
    added: float  # epoch seconds


class PasskeyStore:
    """The passkeys, kept in a file and safe to use from any thread."""

    def __init__(self, path: str):
        """
        Args:
            path: The file they are kept in. It is read whenever it is needed, so a
                file written by another store is seen.
        """
        self.path = path
        self._lock = threading.Lock()
        self._warned = False

    def _read(self) -> List[Passkey]:
        """Returns the passkeys in the file, oldest first. The caller holds the lock.

        A file which can't be read is logged once, without its contents, and read as none:
        no passkeys means nothing can be approved, which is the safe way to fail.
        """
        try:
            stored = load_json(self.path, [])
            if not isinstance(stored, list):
                raise ValueError("not a list of passkeys")
            keys = [Passkey(**each) for each in stored]
        except (OSError, ValueError, TypeError) as e:
            if not self._warned:
                self._warned = True
                logger.error(f"Could not read the passkeys, so none are known ({type(e).__name__})")
            return []
        self._warned = False
        return keys

    def _write(self, keys: List[Passkey]):
        """Saves the passkeys, private to this user. The caller holds the lock.

        Raises:
            OSError: If they couldn't be saved.
        """
        save_json(self.path, [asdict(each) for each in keys], indent=2)

    def all(self) -> List[Passkey]:
        """Returns every passkey, oldest first."""
        with self._lock:
            return self._read()

    def get(self, id: str) -> Optional[Passkey]:
        """Returns the passkey with this credential id, or None."""
        with self._lock:
            return next((each for each in self._read() if each.id == id), None)

    def add(self, passkey: Passkey) -> Optional[str]:
        """
        Args:
            passkey: The one to keep.

        Returns:
            None once it is saved, else what to tell him.

        Raises:
            OSError: If it couldn't be saved.
        """
        with self._lock:
            keys = self._read()
            if any(each.id == passkey.id for each in keys):
                return "That passkey is already added."
            if len(keys) >= MOST_PASSKEYS:
                return f"Nova keeps at most {MOST_PASSKEYS} passkeys."
            self._write(keys + [passkey])
            return None

    def remove(self, id: str) -> bool:
        """Removes a passkey. Returns whether there was one with this credential id."""
        with self._lock:
            keys = self._read()
            left = [each for each in keys if each.id != id]
            if len(left) == len(keys):
                return False
            self._write(left)
            return True

    def set_counter(self, id: str, counter: int) -> None:
        """Keeps the signature counter last seen from a passkey, so an older one is refused. Does nothing if unknown."""
        with self._lock:
            keys = self._read()
            for each in keys:
                if each.id == id:
                    each.counter = counter
                    self._write(keys)
                    return
