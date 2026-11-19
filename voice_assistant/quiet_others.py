"""
Quiet Others Module.

Which satellites stop listening for the wake word while Nova speaks on another - see
docs/decisions/0029. A reply said by one satellite reaches the others' microphones, and
"I'm Nova" was enough to wake the Pi from the PC's speakers on 18 November. Each
satellite cancels only its own echo, so while one speaks every other one ignores the wake
word, and the one speaking keeps listening, to be talked over as ever.

QuietOthers only works out who's to be told what; the brain sends it - see
Brain._quiet_others. Each satellite is told only when its own state changes.
"""

import threading
from typing import Dict, Iterable, List, Optional, Set, Tuple


class QuietOthers:
    """Keeps which satellites are speaking, and what each connected one has been told."""

    def __init__(self):
        self._lock = threading.Lock()
        self._speaking: Set[str] = set()
        # True for each satellite told to be quiet; one not here hears.
        self._told: Dict[str, bool] = {}

    def update(self, connected: Iterable[str], satellite: Optional[str] = None,
               speaking: Optional[bool] = None) -> List[Tuple[str, bool]]:
        """
        Notes that a satellite started or stopped speaking, if one did, and works out
        which satellites to tell.

        Args:
            connected: Every satellite connected now. One which isn't counts as neither
                speaking nor told, so it's told afresh if it comes back.
            satellite: The one which started or stopped speaking - None just to bring the
                connected ones up to date, as one joins or goes.
            speaking: Whether it's speaking now.

        Returns:
            (satellite, quiet) for each one whose state has changed, to be told.
        """
        connected = list(connected)
        with self._lock:
            if satellite is not None:
                if speaking:
                    self._speaking.add(satellite)
                else:
                    self._speaking.discard(satellite)
            here = set(connected)
            self._speaking &= here
            self._told = {name: told for name, told in self._told.items() if name in here}
            changes = []
            for name in connected:
                quiet = any(other != name for other in self._speaking)
                if self._told.get(name, False) != quiet:
                    self._told[name] = quiet
                    changes.append((name, quiet))
            return changes
