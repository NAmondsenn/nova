"""
Music Resume Module.

Keeps the music each satellite was playing across an update, so it carries on where it
was once that satellite is back. An update restarts the brain, which holds every
satellite's music player, and a satellite's own restart ends its session, so the music
would stop for good. Before either, the brain has the player hand its music over - the
queue, the song and how far into it - and the state is kept here, in a file in the
update folder as the brain's own restart must not lose it. When the satellite is back
and caught up, the brain takes it out again and has the new player carry on.

Only Navidrome music is kept: Spotify plays on the satellite's own Spotify Connect
speaker, which neither restart stops.
"""

import logging
import os
import threading
import time
from typing import Dict, Optional

from voice_assistant import storage

logger = logging.getLogger(__name__)

# How long after it was kept music is still carried on, in seconds. Longer, and a
# satellite which comes back hours later would start playing out of the blue.
RESUME_WITHIN = 600


class MusicResume:
    """
    The music kept for each satellite across an update, in one small JSON file.

    Each entry is the state a player's hand_over gave and the wall-clock time it was kept -
    wall-clock, not monotonic, as the time must mean something after the brain restarts.
    """

    def __init__(self, path: str):
        """
        Args:
            path: The file to keep it in - update/music.json - made on the first keep.
        """
        self.path = path
        self._lock = threading.Lock()

    def keep(self, name: str, state: Dict) -> None:
        """
        Keeps a satellite's music, replacing any kept for it before.

        Args:
            name: The satellite's name.
            state: What its player's hand_over gave.

        Raises:
            OSError or TypeError if it couldn't be saved.
        """
        with self._lock:
            kept = self._read()
            kept[name] = {"state": state, "kept_at": time.time()}
            storage.private_folder(os.path.dirname(self.path))
            storage.save_json(self.path, kept)

    def take(self, name: str) -> Optional[Dict]:
        """
        Takes a satellite's kept music out, whether or not it is still wanted - so a
        satellite back hours later never plays it, and it is resumed once at most.

        Args:
            name: The satellite's name.

        Returns:
            The state to give its player's take_over, or None if nothing was kept for
            it or it was kept more than RESUME_WITHIN seconds ago.
        """
        with self._lock:
            kept = self._read()
            entry = kept.pop(name, None)
            if entry is None:
                return None
            try:
                storage.save_json(self.path, kept)
            except Exception as e:
                logger.warning(f"Couldn't take the {name} satellite's kept music out of {self.path}: {e}")
        try:
            age = time.time() - entry["kept_at"]
            state = entry["state"]
        except (KeyError, TypeError):
            return None
        if not isinstance(state, dict) or age > RESUME_WITHIN:
            return None
        return state

    def _read(self) -> Dict:
        """Returns everything kept, or nothing if the file is missing or broken - never raises."""
        try:
            kept = storage.load_json(self.path, {})
        except (OSError, ValueError) as e:
            logger.warning(f"Couldn't read the music kept in {self.path}, so none is carried on: {e}")
            return {}
        return kept if isinstance(kept, dict) else {}
