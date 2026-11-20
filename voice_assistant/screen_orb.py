"""
Screen Orb Module.

Nova's orb on the Windows PC's screen: the clock's orb, small, in a see-through window
of its own while Nova listens, thinks and speaks on the PC, and nothing at all the rest
of the time. The PC is a push-to-talk satellite, so what it does - a press, a recording
sent, a reply played - is what the orb follows, and it needs nothing more from the brain.
See docs/superpowers/specs/2026-11-20-pc-screen-indicator-design.md.

Nothing the satellite does ever waits on the orb or fails because of it, so everything
here is told what has happened and returns at once. What is drawn is in orb_drawing.py.
The window itself (_Win32Window) is the only part which needs Windows, and is made only
there: the rest is plain Python, tested anywhere with a fake window.
"""

import itertools
import logging
import queue
import sys
import threading
import time
from typing import Callable, Optional, Tuple

import numpy as np

from voice_assistant.orb_drawing import SIZES, OrbPainter

logger = logging.getLogger(__name__)

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

# Where the orb sits and how big it is until the settings say otherwise, and for any
# place or size which isn't one of the settings page's.
DEFAULT_PLACE = "bottom centre"
DEFAULT_SIZE = "medium"

# How often, in seconds, a frame is drawn while the orb shows: about thirty a second, as
# smooth as the clock's. While it doesn't, nothing is drawn, but the window's messages are
# still seen to, no more than this many seconds apart, as Windows expects every window to
# answer them.
FRAME = 1 / 30
IDLE_PUMP = 0.5

# The most seconds the satellite waits for the orb's window to be made as it starts, and
# for the orb's thread to finish as it stops.
SET_UP_WAIT = 5
CLOSE_WAIT = 2

# What the orb can be asked to show.
STATES = ("listening", "thinking", "speaking", "hidden")

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


# The note which tells the orb's thread to destroy the window and finish.
_CLOSE = object()


class ScreenOrb:
    """
    Nova's orb on the PC's screen, in a window owned by one thread of its own for as long
    as the satellite runs.

    The satellite tells it what to show and how, from whichever thread it is on, and each
    call only leaves a note for the orb's thread: nothing the satellite does ever waits on
    the window. The thread draws about thirty frames a second while the orb shows, and
    sleeps on its notes, drawing nothing, while it doesn't. If anything to do with the
    window fails, that is logged once and the orb goes quiet for the rest of the run.
    """

    def __init__(self, backend: Optional[Callable[[], object]] = None):
        """
        Args:
            backend: Makes the window, on the orb's own thread - _Win32Window, which is
                used when this is left out on Windows. Off Windows, left out, there's
                none, and every call does nothing. Tests give a fake.
        """
        self._make = backend if backend is not None else (_Win32Window if sys.platform == "win32" else None)
        self._notes: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._available = False
        # Set, from any thread, once the orb has nothing more to do: there's no window,
        # it failed, or it was closed. Every call then returns without leaving a note.
        self._quiet = self._make is None
        # The orb's thread's own: what it shows and how, and what is on the screen.
        # Nothing else touches these.
        self._fade = Fade()
        self._on = True
        self._where = DEFAULT_PLACE
        self._size = DEFAULT_SIZE
        self._painter: Optional[OrbPainter] = None
        self._body = 0
        # Where the window is while the orb shows; None when it is to be worked out
        # afresh, as the orb appears or its place or size changes.
        self._spot: Optional[Tuple[int, int]] = None
        # Whether a frame is up on the screen, and when it was drawn.
        self._up = False
        self._drawn_at = 0.0
        if self._make is None:
            return
        made = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(made,), daemon=True, name="screen-orb")
        self._thread.start()
        if made.wait(SET_UP_WAIT):
            self._available = not self._quiet
        else:
            self._go_quiet("Windows took too long to make the window for Nova's orb, so it isn't shown")
            self._notes.put(_CLOSE)

    @property
    def available(self) -> bool:
        """Whether the window could be made, so the orb can be shown."""
        return self._available

    def show(self, state: str) -> None:
        """
        Shows a state, or hides the orb. Never waits and never raises.

        Args:
            state: "listening", "thinking", "speaking" or "hidden". Anything else is
                ignored.
        """
        if not self._quiet and state in STATES:
            self._notes.put(("show", state))

    def configure(self, on: bool, place: str, size: str) -> None:
        """
        Changes the orb's settings. Never waits and never raises.

        Args:
            on: False hides the orb at once, with no linger, and shows nothing until
                it is on again; whatever is shown once it is, is shown from then.
            place: One of PLACES; anything else is DEFAULT_PLACE.
            size: One of orb_drawing.SIZES; anything else is DEFAULT_SIZE.
        """
        if self._quiet:
            return
        # Checked here rather than on the orb's thread, so nothing it is given can fail it.
        where = place if place in PLACES else DEFAULT_PLACE
        size = size if isinstance(size, str) and size in SIZES else DEFAULT_SIZE
        self._notes.put(("configure", bool(on), where, size))

    def close(self) -> None:
        """Destroys the window and lets the orb's thread go, waiting at most CLOSE_WAIT seconds."""
        if self._thread is None:
            return
        self._quiet = True
        self._notes.put(_CLOSE)
        self._thread.join(CLOSE_WAIT)

    def _go_quiet(self, why: str) -> None:
        """Stops the orb for the rest of the run, logging why the first time."""
        if not self._quiet:
            self._quiet = True
            logger.warning(why)

    def _run(self, made: threading.Event) -> None:
        """The orb's thread: makes the window, draws in it until closed, then destroys it."""
        try:
            backend = self._make()
        except Exception as e:
            self._go_quiet(f"Couldn't make the window for Nova's orb, so it isn't shown ({type(e).__name__})")
            made.set()
            return
        made.set()
        try:
            self._serve(backend)
        except Exception as e:
            self._go_quiet(f"Nova's orb on the screen failed, so it's off until the satellite restarts "
                           f"({type(e).__name__})")
            logger.debug("How Nova's orb on the screen failed", exc_info=True)
        finally:
            # Destroying the window also takes it off the screen, so a frame left up by a
            # failure isn't left there for good.
            try:
                backend.destroy()
            except Exception:
                logger.debug("Couldn't destroy the window for Nova's orb", exc_info=True)

    def _serve(self, backend) -> None:
        """
        Takes the notes left for the orb, drawing a frame every FRAME seconds while it
        shows and blocking on the notes while it doesn't, until told to close.
        """
        # When the next frame is due, while the orb shows.
        due: Optional[float] = None
        while True:
            if due is None:
                try:
                    note = self._notes.get(timeout=IDLE_PUMP)
                except queue.Empty:
                    backend.pump()
                    continue
            else:
                try:
                    note = self._notes.get(timeout=max(0.0, due - time.monotonic()))
                except queue.Empty:
                    note = None
            # Everything waiting is taken at once, so a burst of notes costs one frame.
            while note is not None:
                if note is _CLOSE:
                    return
                self._take(note, backend, time.monotonic())
                try:
                    note = self._notes.get_nowait()
                except queue.Empty:
                    note = None
            now = time.monotonic()
            # A note which came between frames is drawn by the next one, when it's due.
            if due is not None and now < due:
                continue
            backend.pump()
            due = now + FRAME if self._draw(backend, now) else None

    def _take(self, note: tuple, backend, now: float) -> None:
        """Acts on one note from show or configure."""
        if note[0] == "show":
            # While it is off, what is shown is not even remembered.
            if self._on:
                self._fade.want(note[1], now)
            return
        _, on, where, size = note
        if (where, size) != (self._where, self._size):
            self._where, self._size = where, size
            self._spot = None
        if self._on and not on:
            # Switched off, it goes at once rather than lingering and fading, and forgets
            # what it was showing, so nothing comes back until something new is shown.
            self._fade = Fade()
            self._hide(backend)
        self._on = on

    def _draw(self, backend, now: float) -> bool:
        """
        Draws the frame due now, or takes the window down if there's nothing to draw.

        Returns:
            Whether a frame was drawn, and so whether another is due.
        """
        step = self._fade.step(now)
        if step is None:
            self._hide(backend)
            return False
        look, opacity, scale = step
        if self._spot is None:
            # Worked out afresh as the orb appears, so a change of resolution, a moved
            # taskbar, a different main screen or a new scaling is picked up.
            dpi_scale = backend.dpi_scale()
            body = round(SIZES[self._size] * dpi_scale)
            if self._painter is None or body != self._body:
                self._painter, self._body = OrbPainter(body), body
            self._spot = place(backend.work_area(), self._painter.side, self._where,
                               round(MARGIN * dpi_scale))
        frame = self._painter.frame(look, now - self._drawn_at if self._up else 0.0, opacity, scale)
        backend.put(frame, *self._spot)
        self._up = True
        self._drawn_at = now
        return True

    def _hide(self, backend) -> None:
        """Takes the window down, if a frame is up."""
        if self._up:
            backend.hide()
            self._up = False
        self._spot = None


class _Win32Window:
    """
    The orb's window on Windows, through Win32 by ctypes: see-through, always on top and
    click-through, never taking the keyboard, with no title, no taskbar button and no
    place in Alt-Tab. Made, used and destroyed only on ScreenOrb's own thread, as Windows
    gives a window's messages to the thread which made it, and only that thread may
    destroy it.

    Making it raises OSError if Windows refuses (AttributeError on a Windows too old to
    have what it needs), and so does put.
    """

    # Windows' own numbers for what is asked of it (winuser.h, wingdi.h, shellscalingapi.h).
    WS_POPUP = 0x80000000
    WS_EX_TOPMOST = 0x00000008
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_LAYERED = 0x00080000
    WS_EX_NOACTIVATE = 0x08000000
    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
    HWND_TOPMOST = -1
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    ULW_ALPHA = 0x00000002
    AC_SRC_OVER = 0x00
    AC_SRC_ALPHA = 0x01
    BI_RGB = 0
    DIB_RGB_COLORS = 0
    PM_REMOVE = 0x0001
    WM_CLOSE = 0x0010
    MONITOR_DEFAULTTOPRIMARY = 0x00000001
    MDT_EFFECTIVE_DPI = 0
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
    # The DPI Windows calls 100% scaling.
    BASE_DPI = 96

    # Each window's class has a name of its own, so a window made again in the same run
    # never finds a class left registered with an earlier window's procedure.
    _numbers = itertools.count(1)

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        c_int, c_void_p, POINTER = ctypes.c_int, ctypes.c_void_p, ctypes.POINTER
        # Every handle is pointer-sized, so none is cut to 32 bits on 64-bit Windows.
        HANDLE = wintypes.HANDLE
        HWND, HDC = wintypes.HWND, wintypes.HDC
        BOOL, UINT, DWORD, LONG, WORD = wintypes.BOOL, wintypes.UINT, wintypes.DWORD, wintypes.LONG, wintypes.WORD
        BYTE = ctypes.c_ubyte
        WPARAM, LPARAM, LPCWSTR = wintypes.WPARAM, wintypes.LPARAM, wintypes.LPCWSTR
        # LRESULT is pointer-sized and signed, as LPARAM is.
        LRESULT = LPARAM

        class POINT(ctypes.Structure):
            _fields_ = [("x", LONG), ("y", LONG)]

        class SIZE(ctypes.Structure):
            _fields_ = [("cx", LONG), ("cy", LONG)]

        class RECT(ctypes.Structure):
            _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", DWORD)]

        class MSG(ctypes.Structure):
            _fields_ = [("hwnd", HWND), ("message", UINT), ("wParam", WPARAM), ("lParam", LPARAM),
                        ("time", DWORD), ("pt", POINT), ("lPrivate", DWORD)]

        class BLENDFUNCTION(ctypes.Structure):
            _fields_ = [("BlendOp", BYTE), ("BlendFlags", BYTE), ("SourceConstantAlpha", BYTE),
                        ("AlphaFormat", BYTE)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", DWORD), ("biWidth", LONG), ("biHeight", LONG), ("biPlanes", WORD),
                        ("biBitCount", WORD), ("biCompression", DWORD), ("biSizeImage", DWORD),
                        ("biXPelsPerMeter", LONG), ("biYPelsPerMeter", LONG), ("biClrUsed", DWORD),
                        ("biClrImportant", DWORD)]

        # BITMAPINFO's colours are unused for 32-bit pixels, but it is passed whole, as
        # Windows expects, rather than a header alone.
        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", DWORD * 3)]

        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", UINT), ("style", UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", c_int),
                        ("cbWndExtra", c_int), ("hInstance", HANDLE), ("hIcon", HANDLE), ("hCursor", HANDLE),
                        ("hbrBackground", HANDLE), ("lpszMenuName", LPCWSTR), ("lpszClassName", LPCWSTR),
                        ("hIconSm", HANDLE)]

        # Libraries of its own, rather than ctypes.windll's shared ones, so the types set
        # here never clash with another module's for the same function.
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shcore = ctypes.WinDLL("shcore")
        signatures = (
            (user32.SetThreadDpiAwarenessContext, [HANDLE], HANDLE),
            (user32.RegisterClassExW, [POINTER(WNDCLASSEXW)], WORD),
            (user32.UnregisterClassW, [LPCWSTR, HANDLE], BOOL),
            (user32.CreateWindowExW, [DWORD, LPCWSTR, LPCWSTR, DWORD, c_int, c_int, c_int, c_int, HWND, HANDLE,
                                      HANDLE, c_void_p], HWND),
            (user32.DestroyWindow, [HWND], BOOL),
            (user32.DefWindowProcW, [HWND, UINT, WPARAM, LPARAM], LRESULT),
            (user32.ShowWindow, [HWND, c_int], BOOL),
            (user32.SetWindowPos, [HWND, HWND, c_int, c_int, c_int, c_int, UINT], BOOL),
            (user32.UpdateLayeredWindow, [HWND, HDC, POINTER(POINT), POINTER(SIZE), HDC, POINTER(POINT), DWORD,
                                          POINTER(BLENDFUNCTION), DWORD], BOOL),
            (user32.MonitorFromPoint, [POINT, DWORD], HANDLE),
            (user32.GetMonitorInfoW, [HANDLE, POINTER(MONITORINFO)], BOOL),
            (user32.PeekMessageW, [POINTER(MSG), HWND, UINT, UINT, UINT], BOOL),
            (user32.DispatchMessageW, [POINTER(MSG)], LRESULT),
            (gdi32.CreateCompatibleDC, [HDC], HDC),
            (gdi32.DeleteDC, [HDC], BOOL),
            (gdi32.CreateDIBSection, [HDC, POINTER(BITMAPINFO), UINT, POINTER(c_void_p), HANDLE, DWORD], HANDLE),
            (gdi32.SelectObject, [HDC, HANDLE], HANDLE),
            (gdi32.DeleteObject, [HANDLE], BOOL),
            (kernel32.GetModuleHandleW, [LPCWSTR], HANDLE),
            # An HRESULT result makes ctypes raise OSError for a failure.
            (shcore.GetDpiForMonitor, [HANDLE, c_int, POINTER(UINT), POINTER(UINT)], ctypes.HRESULT),
        )
        for function, arguments, result in signatures:
            function.argtypes = arguments
            function.restype = result
        self._user32, self._gdi32, self._shcore = user32, gdi32, shcore
        self._POINT, self._SIZE, self._MONITORINFO, self._UINT = POINT, SIZE, MONITORINFO, UINT
        self._BITMAPINFO, self._BITMAPINFOHEADER = BITMAPINFO, BITMAPINFOHEADER
        self._blend = BLENDFUNCTION(self.AC_SRC_OVER, 0, 255, self.AC_SRC_ALPHA)
        self._message = MSG()

        # What destroy lets go of, as far as it has been made.
        self._hwnd = None
        self._dc = None
        self._bitmap = None
        self._bits = None
        self._bitmap_size = (0, 0)
        self._original = None
        self._registered = False
        # Whether the window is on the screen.
        self._shown = False

        # Only the window's own thread ever closes it, so a close asked for from outside,
        # by a tool which closes every window, say, is turned down; everything else is
        # left to Windows. Kept for as long as the class is registered, as Windows calls it.
        close, default = self.WM_CLOSE, user32.DefWindowProcW

        def procedure(hwnd, message, wparam, lparam):
            if message == close:
                return 0
            return default(hwnd, message, wparam, lparam)

        self._procedure = WNDPROC(procedure)
        try:
            # This thread alone is made aware of each screen's own scaling, so Windows
            # gives it the screens' real pixels and leaves its window unstretched: the
            # orb is drawn for the scaling itself, sharp on a 4K screen at 150%. A window
            # keeps its thread's awareness from when it is made, so this comes first. On
            # a Windows too old for it, Windows stretches the window itself, and the orb
            # is drawn for 100%.
            self._aware = bool(user32.SetThreadDpiAwarenessContext(self.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
            self._instance = kernel32.GetModuleHandleW(None)
            self._class_name = f"NovaScreenOrb{next(self._numbers)}"
            window_class = WNDCLASSEXW()
            window_class.cbSize = ctypes.sizeof(WNDCLASSEXW)
            window_class.lpfnWndProc = self._procedure
            window_class.hInstance = self._instance
            window_class.lpszClassName = self._class_name
            self._check(user32.RegisterClassExW(ctypes.byref(window_class)), "RegisterClassExW")
            self._registered = True
            # Layered for its see-through pixels, and with them transparent, so clicks
            # go through it; topmost; a tool window, so no taskbar button and no place in
            # Alt-Tab; and never activated, so it never takes the keyboard from a game or
            # a document. A popup, so no frame and no title; it's sized by each frame.
            styles = (self.WS_EX_LAYERED | self.WS_EX_TRANSPARENT | self.WS_EX_TOPMOST | self.WS_EX_TOOLWINDOW
                      | self.WS_EX_NOACTIVATE)
            self._hwnd = self._check(user32.CreateWindowExW(styles, self._class_name, None, self.WS_POPUP, 0, 0, 0,
                                                            0, None, None, self._instance, None),
                                     "CreateWindowExW")
            self._dc = self._check(gdi32.CreateCompatibleDC(None), "CreateCompatibleDC")
        except BaseException:
            self.destroy()
            raise

    def _check(self, result, what: str):
        """
        Returns a Win32 call's result, or raises OSError if it says it failed.

        Args:
            result: What the call returned: nothing (0 or None) for a failure.
            what: The call's name, for the error.
        """
        if not result:
            ctypes = self._ctypes
            raise ctypes.WinError(ctypes.get_last_error(), f"{what} failed")
        return result

    def _primary(self):
        """The main screen: the one whose top-left corner is the desktop's (0, 0)."""
        return self._user32.MonitorFromPoint(self._POINT(0, 0), self.MONITOR_DEFAULTTOPRIMARY)

    def work_area(self) -> Tuple[int, int, int, int]:
        """The main screen less the taskbar, as (left, top, right, bottom) in its own pixels."""
        info = self._MONITORINFO()
        info.cbSize = self._ctypes.sizeof(self._MONITORINFO)
        self._check(self._user32.GetMonitorInfoW(self._primary(), self._ctypes.byref(info)), "GetMonitorInfoW")
        area = info.rcWork
        return area.left, area.top, area.right, area.bottom

    def dpi_scale(self) -> float:
        """The main screen's scaling, 1.0 for 100%."""
        if not self._aware:
            return 1.0
        x, y = self._UINT(), self._UINT()
        byref = self._ctypes.byref
        self._shcore.GetDpiForMonitor(self._primary(), self.MDT_EFFECTIVE_DPI, byref(x), byref(y))
        return x.value / self.BASE_DPI

    def put(self, frame: np.ndarray, x: int, y: int) -> None:
        """
        Shows a frame with its top-left corner there, on top of everything, without
        taking the keyboard.

        Args:
            frame: A (height, width, 4) array of uint8: blue, green, red and alpha, the
                colours already multiplied by the alpha.
            x: Where its left edge goes, in the screen's pixels.
            y: Where its top edge goes.
        """
        ctypes = self._ctypes
        height, width = frame.shape[0], frame.shape[1]
        pixels = np.ascontiguousarray(frame, dtype=np.uint8)
        if pixels.shape != (height, width, 4):
            raise ValueError(f"a frame should be (height, width, 4), not {frame.shape}")
        self._bitmap_for(width, height)
        ctypes.memmove(self._bits, pixels.ctypes.data, pixels.nbytes)
        byref = ctypes.byref
        self._check(self._user32.UpdateLayeredWindow(
            self._hwnd, None, byref(self._POINT(x, y)), byref(self._SIZE(width, height)), self._dc,
            byref(self._POINT(0, 0)), 0, byref(self._blend), self.ULW_ALPHA), "UpdateLayeredWindow")
        if not self._shown:
            # Shown only once its frame is in, so it never flashes up what it last showed;
            # and put back on top of everything each time, as a game or another topmost
            # window may have come over it while it was hidden.
            self._user32.ShowWindow(self._hwnd, self.SW_SHOWNOACTIVATE)
            self._check(self._user32.SetWindowPos(self._hwnd, self.HWND_TOPMOST, 0, 0, 0, 0,
                                                  self.SWP_NOACTIVATE | self.SWP_NOMOVE | self.SWP_NOSIZE),
                        "SetWindowPos")
            self._shown = True

    def _bitmap_for(self, width: int, height: int) -> None:
        """Makes the bitmap frames are copied into that size, unless it already is."""
        if (width, height) == self._bitmap_size:
            return
        self._free_bitmap()
        info = self._BITMAPINFO()
        header = info.bmiHeader
        header.biSize = self._ctypes.sizeof(self._BITMAPINFOHEADER)
        header.biWidth = width
        # A negative height has the rows run from the top down, as a frame's do.
        header.biHeight = -height
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = self.BI_RGB
        bits = self._ctypes.c_void_p()
        byref = self._ctypes.byref
        bitmap = self._check(self._gdi32.CreateDIBSection(self._dc, byref(info), self.DIB_RGB_COLORS, byref(bits),
                                                          None, 0), "CreateDIBSection")
        original = self._gdi32.SelectObject(self._dc, bitmap)
        if not original:
            self._gdi32.DeleteObject(bitmap)
            self._check(original, "SelectObject")
        self._bitmap, self._bits, self._original = bitmap, bits.value, original
        self._bitmap_size = (width, height)

    def _free_bitmap(self) -> None:
        """Lets go of the bitmap, if there is one, putting back the one it replaced."""
        if self._bitmap:
            self._gdi32.SelectObject(self._dc, self._original)
            self._gdi32.DeleteObject(self._bitmap)
        self._bitmap = self._bits = self._original = None
        self._bitmap_size = (0, 0)

    def hide(self) -> None:
        """Takes the window off the screen."""
        if self._shown:
            self._user32.ShowWindow(self._hwnd, self.SW_HIDE)
            self._shown = False

    def pump(self) -> None:
        """Hands the window every message waiting for it."""
        byref = self._ctypes.byref
        while self._user32.PeekMessageW(byref(self._message), None, 0, 0, self.PM_REMOVE):
            self._user32.DispatchMessageW(byref(self._message))

    def destroy(self) -> None:
        """Destroys the window and lets go of everything made for it, as far as it was made."""
        if self._hwnd:
            self._user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        self._free_bitmap()
        if self._dc:
            self._gdi32.DeleteDC(self._dc)
            self._dc = None
        # Only once the window is gone, as a class can't be unregistered while it has one.
        if self._registered:
            self._user32.UnregisterClassW(self._class_name, self._instance)
            self._registered = False
