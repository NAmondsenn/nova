"""
Screen Orb Module.

Nova's orb on the Windows PC's screen: the clock's orb, small, in a see-through window
of its own while Nova listens, thinks and speaks on the PC, and nothing at all the rest
of the time. The PC is a push-to-talk satellite, so what it does - a press, a recording
sent, a reply played - is what the orb follows, and it needs nothing more from the brain.
See docs/superpowers/specs/2026-11-20-pc-screen-indicator-design.md.

Nothing the satellite does ever waits on the orb or fails because of it, so everything
here is told what has happened and returns at once. What is drawn is in orb_drawing.py,
and none of this module's logic needs Windows, so it is tested anywhere.
"""

import threading
from typing import Callable


class OrbStates:
    """
    What the orb shows, worked out from what the PC satellite is doing.

    The satellite tells it each thing as it happens, from whichever thread it happens on.
    The orb shows "listening" if a recording is being sent; else "speaking" if any of
    Nova's spoken streams is playing; else "thinking" if a recording ended answered and
    neither speech nor the brain's `allow` has come since; else "hidden". So listening
    outranks speaking (a press over a reply is heard at once), and thinking fills the gap
    between a recording and the words, so the orb never blinks out between them.

    `on_change` is called with the new state, only when it changes, never while a lock
    is held, and one call at a time, in order. It must return at once and should not
    raise: if it does, the state is still right, but the exception reaches whoever told
    OrbStates what happened.
    """

    def __init__(self, on_change: Callable[[str], None]):
        """
        Args:
            on_change: Called with "listening", "thinking", "speaking" or "hidden" each
                time the state changes. It starts hidden, which is not called for.
        """
        self._on_change = on_change
        # What the state is made of, all under this lock.
        self._lock = threading.Lock()
        self._listening = False
        self._speaking = 0
        self._thinking = False
        # Held around each call to on_change, so they come one at a time, and around the
        # note of the state last told, which only those calls make.
        self._telling = threading.RLock()
        self._told = "hidden"

    @property
    def state(self) -> str:
        """What the orb shows now: "listening", "thinking", "speaking" or "hidden"."""
        with self._lock:
            return self._work_out()

    def listening_started(self) -> None:
        """A recording began being sent: a press, or the answer to a question Nova asked."""
        with self._lock:
            self._listening = True
        self._tell()

    def listening_ended(self, answered: bool) -> None:
        """
        The recording being sent ended.

        Args:
            answered: True if it ended as it should (the brain stopped it, a second
                press, or the limit), so Nova has something to work on; False if it was
                dropped or failed. Ignored if no recording is being sent, as one which
                ends after the brain is lost must not leave the orb thinking for ever.
        """
        with self._lock:
            if not self._listening:
                return
            self._listening = False
            if answered:
                self._thinking = True
        self._tell()

    def speaking_started(self) -> None:
        """One of Nova's spoken streams began playing: a reply or an announcement."""
        with self._lock:
            self._speaking += 1
            # Its words have come, so nothing is left to wait for.
            self._thinking = False
        self._tell()

    def speaking_ended(self) -> None:
        """One of Nova's spoken streams finished. Two at once need two of these."""
        with self._lock:
            # A stream which was playing when the brain was lost may end after lost() has
            # put the count back to nothing, and that must not owe the next stream a start.
            self._speaking = max(0, self._speaking - 1)
        self._tell()

    def turn_ended(self) -> None:
        """The brain's `allow` came, so a turn which said nothing is over."""
        with self._lock:
            self._thinking = False
        self._tell()

    def lost(self) -> None:
        """The brain is gone, so nothing it was doing will end: everything is off."""
        with self._lock:
            self._listening = False
            self._speaking = 0
            self._thinking = False
        self._tell()

    def _work_out(self) -> str:
        """The state the flags make. The caller holds the lock."""
        if self._listening:
            return "listening"
        if self._speaking:
            return "speaking"
        if self._thinking:
            return "thinking"
        return "hidden"

    def _tell(self) -> None:
        """
        Tells on_change the state if it has changed since it was last told.

        The state is read again once this thread has its turn, rather than taken from the
        change it has just made: if two threads change it at once, whichever tells second
        passes on the final state, so the last call is never an old one.
        """
        with self._telling:
            with self._lock:
                state = self._work_out()
            if state == self._told:
                return
            self._told = state
            self._on_change(state)
