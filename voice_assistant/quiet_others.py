"""
Quiet Others Module.

Which satellites stop listening for the wake word while Nova speaks on another - see
docs/decisions/0029. A reply said by one satellite reaches the others' microphones, and
"I'm Nova" was enough to wake the Pi from the PC's speakers on 18 November. Each
satellite cancels only its own echo, so while one speaks every other one ignores the wake
word, and the one speaking keeps listening, to be talked over as ever.

In an open conversation the others in the same room stay quiet for the whole of it, not
only while Nova speaks, so "Hey Nova" said from habit at one of them can't start a second
turn while he is talking to the satellite next to it. The brain says which those are.

QuietOthers only works out who's to be told what, from who is speaking now; the brain
asks it on a thread of its own, whenever a satellite starts or stops speaking and every
QUIET_RENEW seconds besides, and sends what it says - see Brain._quiet_others_forever.
Working from who is speaking now, rather than from each change as it's heard of, means
two changes heard in the wrong order can't leave a satellite deaf, or listening while
another speaks.
"""

from typing import AbstractSet, Dict, Hashable, List, Tuple

# How often, in seconds, a satellite which is to stay quiet is told so again, so speech
# longer than the minute a satellite stays quiet by itself (satellite.QUIET_MOST) still
# keeps it quiet to the end.
QUIET_RENEW = 20


class QuietOthers:
    """What each connected satellite has been told, and when."""

    def __init__(self):
        # (quiet, when it was told) for each satellite, by its connection.
        self._told: Dict[Hashable, Tuple[bool, float]] = {}

    def tell(self, speaking: Dict[Hashable, bool], now: float,
             also_quiet: AbstractSet[Hashable] = frozenset()) -> List[Tuple[Hashable, bool]]:
        """
        Works out which satellites to tell, and what.

        Args:
            speaking: Whether each satellite connected now is speaking, by its
                connection - so one which connects again, under the same name, is a new
                one, told afresh. One not here is forgotten.
            now: The time, by time.monotonic().
            also_quiet: The connections of the satellites to keep quiet whoever is speaking
                - those in the room of an open conversation, other than the one it is on
                at. One which isn't in speaking is left out, as it isn't connected.

        Returns:
            (connection, quiet) for each to tell: one whose state has changed, one new
            which is to be quiet, and one staying quiet which was last told QUIET_RENEW
            seconds ago.
        """
        self._told = {key: told for key, told in self._told.items() if key in speaking}
        changes = []
        for key in speaking:
            quiet = key in also_quiet or any(on for other, on in speaking.items() if other is not key)
            told = self._told.get(key)
            if told is None:
                due = quiet
            else:
                due = told[0] != quiet or (quiet and now - told[1] >= QUIET_RENEW)
            if due:
                self._told[key] = (quiet, now)
                changes.append((key, quiet))
        return changes
