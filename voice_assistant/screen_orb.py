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
from typing import Callable, Optional, Tuple

# How long a hide waits before the orb starts to fade, in seconds, in case something
# follows it: a reply's words, a follow-up question, a second press.
LINGER = 0.6

# How long the orb takes to fade in or out, and to grow in or change size, in seconds.
# The clock's own orb takes 0.4 s over its opacity and 0.7 s over its transform.
FADE = 0.4
GROW = 0.7

# The size, as a part of the full size, which the orb shrinks to while Nova thinks, and the
# size it grows in from.
THINKING_SCALE = 0.82
_GROW_FROM = 0.6

# How long, in seconds, thinking may go on before the orb is hidden anyway: no turn takes
# this long, so one that has is lost, and the orb must not be left on the screen for ever.
THINKING_MOST = 30

# The gap in pixels, at 100% scaling, between the orb's window and the edge of the work
# area it is placed in, and the places the settings page offers for it.
MARGIN = 12
PLACES = (
    "bottom centre", "bottom left", "bottom right",
    "top centre", "top left", "top right",
)

# The clock's easings, as the four numbers of a CSS cubic-bezier(): `ease`, which is what
# its opacity transition has by default, and the one its blob grows in with, which
# overshoots a little before it settles.
_FADE_EASE = (0.25, 0.1, 0.25, 1.0)
_GROW_EASE = (0.2, 0.9, 0.3, 1.2)


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


def place(work_area: Tuple[int, int, int, int], side: int, where: str,
          margin: int) -> Tuple[int, int]:
    """
    Where the orb's window goes on the screen.

    Args:
        work_area: The screen less the taskbar, as (left, top, right, bottom) in pixels.
            On a screen to the left of or above the main one these are negative.
        side: The window's side in pixels.
        where: One of PLACES; anything else is "bottom centre".
        margin: The gap in pixels between the window and the work area's edges.

    Returns:
        The window's top-left corner, in the same pixels as the work area.
    """
    left, top, right, bottom = work_area
    if where not in PLACES:
        where = "bottom centre"
    vertical, horizontal = where.split()
    if horizontal == "left":
        x = left + margin
    elif horizontal == "right":
        x = right - margin - side
    else:
        x = (left + right - side) // 2
    y = top + margin if vertical == "top" else bottom - margin - side
    return x, y


def _bezier(curve: Tuple[float, float, float, float], progress: float) -> float:
    """
    A CSS cubic-bezier() easing.

    Args:
        curve: The curve's two control points, as CSS writes them: x1, y1, x2, y2.
        progress: How far through the time the animation is, from 0 to 1.

    Returns:
        How far through the change it is: 0 at the start and 1 at the end, and beyond 1
        for a moment in between if the curve overshoots.
    """
    if progress <= 0:
        return 0.0
    if progress >= 1:
        return 1.0
    x1, y1, x2, y2 = curve

    def point(first: float, second: float, along: float) -> float:
        """The curve's x or y that far along it, from its start (0, 0) to its end (1, 1)."""
        behind = 1 - along
        return (3 * behind * behind * along * first + 3 * behind * along * along * second
                + along * along * along)

    # The curve is given by how y goes with x, but is walked by a third number, so the
    # one which has x at this progress is found by halving: x only ever increases.
    low, high = 0.0, 1.0
    for _ in range(24):
        middle = (low + high) / 2
        if point(x1, x2, middle) < progress:
            low = middle
        else:
            high = middle
    return point(y1, y2, (low + high) / 2)


class _Ramp:
    """A number easing from one value to another, starting at a given time."""

    def __init__(self, start_time: float, start: float, end: float, duration: float,
                 curve: Tuple[float, float, float, float]):
        """
        Args:
            start_time: When it starts, on the clock the caller uses.
            start: Its value then.
            end: Its value once it has taken `duration`, and for ever after.
            duration: How long it takes, in seconds.
            curve: The easing, as for _bezier.
        """
        self.start_time = start_time
        self.start = start
        self.end = end
        self.duration = duration
        self._curve = curve

    def value(self, now: float) -> float:
        """Its value at that time; its start before it starts and its end once done."""
        if self.done(now):
            return self.end
        progress = (now - self.start_time) / self.duration
        return self.start + (self.end - self.start) * _bezier(self._curve, progress)

    def done(self, now: float) -> bool:
        """Whether it has reached its end by that time."""
        return now >= self.start_time + self.duration


class Fade:
    """
    How the orb fades and grows in and out over time, from what it is asked to show.

    It is told what is wanted as it changes, with the time, and asked what to draw at
    the times it draws. A shown state appears at once, fading in over FADE as it grows
    from 0.6 of its size over GROW, with the little overshoot the clock's orb has. Hiding
    waits LINGER with the last look still up, so something following on (thinking into
    speaking, a follow-up question, a second press) never makes it blink, and only then
    fades out over FADE. Anything shown during the linger or the fade takes over from
    wherever the opacity and the size have got to, with no new grow-in. Thinking shrinks
    the orb to THINKING_SCALE, and if it lasts longer than THINKING_MOST it is hidden as
    if it had been told to be. The times are the caller's own clock, which must not go
    backwards, and the look changes the moment a different state is wanted: the painter
    keeps the motion smooth, so only the opacity and the size are eased here.

    Every call is safe from any thread and returns at once.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._wanted = "hidden"
        # The state whose look is drawn: None when nothing is, which costs nothing.
        self._look: Optional[str] = None
        self._opacity = _Ramp(0.0, 0.0, 0.0, FADE, _FADE_EASE)
        self._scale = _Ramp(0.0, 1.0, 1.0, GROW, _GROW_EASE)
        # When the opacity is next due to start fading out, if it is: after the linger of
        # a hide, or thinking's longest. It starts there rather than when this notices, so
        # the fade is the same however seldom this is called.
        self._fade_out_at: Optional[float] = None

    def want(self, state: str, now: float) -> None:
        """
        Says what the orb should now be showing.

        Args:
            state: "listening", "thinking" or "speaking" to show that, or "hidden". The
                state already wanted is ignored, so thinking is timed from when it began.
            now: The time, in seconds, on the same clock as step.
        """
        with self._lock:
            self._settle(now)
            if state == self._wanted:
                return
            self._wanted = state
            if state == "hidden":
                self._hide(now + LINGER)
            else:
                self._show(state, now)

    def step(self, now: float) -> Optional[Tuple[str, float, float]]:
        """
        Works out what to draw.

        Args:
            now: The time, in seconds, on the same clock as want.

        Returns:
            (look, opacity, scale): the state whose motion to draw, then how opaque (0 to
            1) and how big (1 is full size, and it can pass that a little) to draw it. None
            if there is nothing to draw, before anything has been shown and once it has
            faded out, when the window can go.
        """
        with self._lock:
            self._settle(now)
            if self._look is None:
                return None
            return self._look, self._opacity.value(now), self._scale.value(now)

    def _settle(self, now: float) -> None:
        """Starts a fade-out whose time has come, and drops the look once it is over."""
        if self._fade_out_at is not None and now >= self._fade_out_at:
            start = self._fade_out_at
            self._opacity = _Ramp(start, self._opacity.value(start), 0.0, FADE, _FADE_EASE)
            self._fade_out_at = None
        if (self._look is not None and self._opacity.end == 0.0
                and self._opacity.done(now)):
            self._look = None

    def _hide(self, fade_out_at: float) -> None:
        """Hides, if anything is shown and not already on its way out, at that time."""
        if self._look is None or self._opacity.end == 0.0:
            return
        # An earlier time already set, which is thinking's limit, still stands.
        if self._fade_out_at is None or fade_out_at < self._fade_out_at:
            self._fade_out_at = fade_out_at

    def _show(self, state: str, now: float) -> None:
        """Shows that state from this time, from wherever the orb has got to."""
        self._fade_out_at = None
        scale = THINKING_SCALE if state == "thinking" else 1.0
        if self._look is None:
            self._opacity = _Ramp(now, 0.0, 1.0, FADE, _FADE_EASE)
            self._scale = _Ramp(now, _GROW_FROM, scale, GROW, _GROW_EASE)
        else:
            if self._opacity.end == 0.0:
                self._opacity = _Ramp(now, self._opacity.value(now), 1.0, FADE, _FADE_EASE)
            # One already on its way to this size carries on, so a grow is never restarted.
            if self._scale.end != scale:
                self._scale = _Ramp(now, self._scale.value(now), scale, GROW, _GROW_EASE)
        self._look = state
        if state == "thinking":
            self._fade_out_at = now + THINKING_MOST + LINGER
