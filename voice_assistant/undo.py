"""
Undo Module.

Two things kept so that something done can be taken back, both in memory only, never
written down, so a restart forgets them and what was done before it stays done.

UndoShelf keeps what was just removed on the settings page - an alarm, a timer or a
reminder - for a minute, so the page's Undo can put it back (see 0009, decision 4). Each
is kept behind a token of its own, which the page is given in place of what was
removed, and which works once it has put it back - one refused, as it would clash
with something set since, can be tried again until its time is up. The page offers
Undo for ten seconds (UNDO_SHOWN, in web/settings.js), and the brain keeps each for
UNDO_KEPT.

LastChange keeps the last playlist or calendar change Nova made by voice, for ten
minutes, so "undo that" can take it back - but only while it's still the last thing
Nova did.
"""

import secrets
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple, Union

# How long what was removed can be put back, in seconds.
UNDO_KEPT = 60.0
# The most kept at once - far more than anyone removes in a minute, but a limit, so the
# page can't grow it without end.
MOST_KEPT = 50

# How long the last playlist or calendar change can be taken back by "undo that", in seconds.
UNDO_CHANGE_KEPT = 600.0
# What's said when "undo that" can't, and why.
NOTHING_TO_UNDO = "There's nothing to undo."
SOMETHING_ELSE_SINCE = "The last thing I did wasn't a playlist or calendar change, so I've left it."
TOO_LATE = "That was too long ago to undo."


class UndoShelf:
    """What was just removed, by the token which puts it back."""

    def __init__(self, keep_for: float = UNDO_KEPT, clock: Callable[[], float] = time.monotonic):
        """
        Args:
            keep_for: How long each is kept, in seconds.
            clock: What tells the time, in seconds - steady, so the wall clock being
                set while one is kept can't keep it longer, or lose it sooner.
        """
        self.keep_for = keep_for
        self._clock = clock
        # Each token, to the kind of thing kept, the thing itself and when it was put.
        self._kept: Dict[str, Tuple[str, Any, float]] = {}
        # The page answers each request on a thread of its own, so two taps of Undo
        # could otherwise both find what one was meant to take.
        self._lock = threading.Lock()

    def put(self, kind: str, item: Any) -> str:
        """
        Keeps what was removed, letting go of anything kept too long already.

        Args:
            kind: What it is, e.g. "alarm" or "timer".
            item: It, as it was.

        Returns:
            The token which takes it back, unguessable.
        """
        token = secrets.token_urlsafe(16)
        with self._lock:
            now = self._forget_old()
            self._kept[token] = (kind, item, now)
            while len(self._kept) > MOST_KEPT:
                del self._kept[min(self._kept, key=lambda kept: self._kept[kept][2])]
        return token

    def use(self, token: Any, undo: Callable[[str, Any], Dict]) -> Optional[Dict]:
        """
        Puts back what a token keeps, with undo - using the token up only if that
        worked. Meanwhile it's taken off the shelf, so a second tap at the same moment
        finds nothing rather than putting it back twice.

        Args:
            token: As the page sent it - anything at all.
            undo: Called with the kind and the item, answering with 'success' and a
                'message'.

        Returns:
            What undo answered, or None if the token isn't one, has been used, or is
            too old.
        """
        claimed = self._claim(token)
        if claimed is None:
            return None
        answer = None
        try:
            answer = undo(claimed[0], claimed[1])
            return answer
        finally:
            if not (answer and answer.get("success")):
                with self._lock:
                    if self._clock() - claimed[2] <= self.keep_for:
                        self._kept[token] = claimed

    def _forget_old(self) -> float:
        """
        Lets go of anything kept too long, so nothing outlasts its minute on the shelf
        whatever the page does next. Called with the lock held.

        Returns:
            The time now, as the clock says.
        """
        now = self._clock()
        self._kept = {kept: entry for kept, entry in self._kept.items() if now - entry[2] <= self.keep_for}
        return now

    def _claim(self, token: Any) -> Optional[Tuple[str, Any, float]]:
        """Takes a token's entry off the shelf, if it has one still in time."""
        with self._lock:
            self._forget_old()
            if not isinstance(token, str):
                return None
            given = token.encode()
            # Each compared in the same time, however much of it is right, so the page's
            # answers can't be timed to find one.
            found = None
            for kept in self._kept:
                if secrets.compare_digest(kept.encode(), given):
                    found = kept
            return None if found is None else self._kept.pop(found)


class LastChange:
    """The last playlist or calendar change, as the way to take it back."""

    def __init__(self, keep_for: float = UNDO_CHANGE_KEPT, clock: Callable[[], float] = time.monotonic):
        """
        Args:
            keep_for: How long the change is kept, in seconds.
            clock: What tells the time, in seconds - steady, so the wall clock being
                set while one is kept can't keep it longer, or lose it sooner.
        """
        self.keep_for = keep_for
        self._clock = clock
        # The way to take it back, when it was kept, and whether anything else has been
        # done since - "put milk on the list" after "add this to Gym" means "undo that" is
        # no longer about the song.
        self._undo: Optional[Callable[[], Dict]] = None
        self._kept_at = 0.0
        self._else_since = False
        # Kept and taken from whichever thread a request is answered on, so two "undo that"s
        # at once can't both take the one change.
        self._lock = threading.Lock()

    def keep(self, undo: Callable[[], Dict]) -> None:
        """
        Keeps a change just made, in place of the last, as the last thing done.

        Args:
            undo: Takes it back, answering with 'success' and a 'message'.
        """
        with self._lock:
            self._undo = undo
            self._kept_at = self._clock()
            self._else_since = False

    def note_other(self) -> None:
        """Notes that something which wasn't a change was done, so the change kept is no longer the last thing."""
        with self._lock:
            self._else_since = True

    def take(self) -> Union[Callable[[], Dict], str]:
        """
        Takes the way to undo the change kept - which empties the slot, so it's only
        ever given once - unless it can't be, in time or in sense.

        Returns:
            The undo, or what to say of why there isn't one: NOTHING_TO_UNDO, TOO_LATE
            (which lets go of the change) or SOMETHING_ELSE_SINCE (which leaves it, as it
            wasn't undone).
        """
        with self._lock:
            if self._undo is None:
                return NOTHING_TO_UNDO
            if self._clock() - self._kept_at > self.keep_for:
                self._undo = None
                return TOO_LATE
            if self._else_since:
                return SOMETHING_ELSE_SINCE
            undo, self._undo = self._undo, None
            return undo
