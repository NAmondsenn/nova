"""
Orb Drawing Module.

The clock display's orb - five coloured lobes turning round a white core, with two soft
auras breathing behind - drawn in numpy, so the Windows PC's screen can show it in a
see-through window of its own (screen_orb.py). It is the same picture as
voice_assistant/web/display.css draws (lines 402-606), with the same colours, sizes,
motion and speeds for listening, thinking and speaking, but made frame by frame: an
OrbPainter hands back each frame as premultiplied BGRA bytes, which is what Windows'
UpdateLayeredWindow wants.

There is nothing from Windows in here and nothing else to install: it is numpy and the
standard library only, so it runs, and is tested, anywhere. See
docs/superpowers/specs/2026-11-20-pc-screen-indicator-design.md.

A frame is cheap on purpose, as it is made about thirty times a second: float32 and
vectorised throughout, every layer a lookup into a small table of its gradient's colours.
"""

import math
from typing import Dict, List, NamedTuple, Sequence, Tuple

import numpy as np

# The settings page's choices for the orb's size: the width in pixels, at 100% scaling,
# of its bright body - the clock's 300 px blob, which the aura's glow fades out around.
SIZES: Dict[str, int] = {"small": 48, "medium": 64, "large": 96}

# The clock's own sizes, in its pixels: the body, and the outermost aura which the
# window is as big as.
_BODY = 300
_AURA = 520

# How many entries each gradient's table of colours has: more than a gradient's longest
# fade has pixels or 8-bit colour has levels, so there is no banding or stepping.
_STEPS = 1024

# CSS sizes a radial gradient with none given to its farthest corner, so its radius is
# this much more than half the box's width and height.
_FARTHEST_CORNER = math.sqrt(2)


def window_side(body_px: int) -> int:
    """
    How big the orb's window is.

    Args:
        body_px: The bright body's width in pixels, already scaled for the screen.

    Returns:
        The side in pixels of the square window, as big as the clock's 520 px aura is
        beside its 300 px body, rounded up.
    """
    return math.ceil(_AURA * body_px / _BODY)


class _Gradient:
    """
    A CSS radial-gradient, as a table of its colours by distance from its centre.

    CSS mixes the colours of a gradient's stops with their alpha already multiplied in
    ("premultiplied"), so a stop which fades to transparent doesn't pull its neighbour
    towards black, and this does the same. The table is in the order a frame is: blue,
    green, red, alpha.
    """

    def __init__(self, width: float, height: float,
                 stops: Sequence[Tuple[float, Tuple[int, int, int, float]]]):
        """
        Args:
            width: The box the gradient fills, in the clock's pixels.
            height: The same.
            stops: Where each colour is, as a fraction of the gradient's radius, and the
                colour as CSS has it: red, green and blue out of 255, and alpha.
        """
        positions = [position for position, _ in stops]
        colours = np.array([(blue / 255 * alpha, green / 255 * alpha, red / 255 * alpha, alpha)
                            for _, (red, green, blue, alpha) in stops])
        # Each entry is the colour halfway through its step, so a lookup which rounds
        # down is as likely to be too far in as too far out.
        along = (np.arange(_STEPS) + 0.5) / _STEPS * positions[-1]
        self.table = np.empty((4, _STEPS + 1), dtype=np.float32)
        for channel in range(4):
            self.table[channel, :_STEPS] = np.interp(along, positions, colours[:, channel])
            # Past the last stop CSS carries on with its colour (here always clear).
            self.table[channel, _STEPS] = colours[-1, channel]
        # How far out, in the clock's pixels, the last stop is across and up and down.
        self.reach_x = width / 2 * _FARTHEST_CORNER * positions[-1]
        self.reach_y = height / 2 * _FARTHEST_CORNER * positions[-1]


class _Lobe(NamedTuple):
    """One lobe: its colour, and how its keyframes move it from 0% to 50% (display.css, w1-w5)."""

    gradient: _Gradient
    shift_from: Tuple[float, float]
    shift_to: Tuple[float, float]
    turn_from: float
    turn_to: float
    stretch_to: Tuple[float, float]


def _lobe(colour: Tuple[int, int, int], alphas: Tuple[float, float], stops: Tuple[float, float],
          **keyframes) -> _Lobe:
    """
    A 176 x 146 lobe, whose colour is solid in the middle and fades out to nothing.

    Args:
        colour: Red, green and blue out of 255.
        alphas: The alpha at its middle, and at the first stop.
        stops: Where the first stop is, and where it has faded out, as fractions of the
            gradient's radius.
        **keyframes: The shift, turn and stretch to _Lobe.
    """
    red, green, blue = colour
    centre, edge = alphas
    first, end = stops
    gradient = _Gradient(176, 146, ((0, (red, green, blue, centre)), (first, (red, green, blue, edge)),
                                    (end, (red, green, blue, 0))))
    return _Lobe(gradient=gradient, **keyframes)


# The five lobes, in the order they are drawn: electric blue, violet, cyan, indigo and
# pink-violet. Each hangs on an orbit which turns round the orb's middle, and wobbles in
# its own way between its 0% keyframe (which all start at scale 1) and its 50% one, as
# display.css's w1 to w5 do. Shifts are in the clock's pixels, turns in degrees.
_LOBES: List[_Lobe] = [
    _lobe((47, 139, 255), (1, 0.85), (0.32, 0.68),
          shift_from=(32, 0), shift_to=(46, 0), turn_from=0, turn_to=0, stretch_to=(1.18, 0.84)),
    _lobe((162, 77, 255), (1, 0.85), (0.32, 0.68),
          shift_from=(-30, 0), shift_to=(-44, 0), turn_from=70, turn_to=95, stretch_to=(0.84, 1.18)),
    _lobe((111, 208, 255), (0.95, 0.7), (0.3, 0.66),
          shift_from=(0, 28), shift_to=(0, 42), turn_from=-40, turn_to=-15, stretch_to=(1.14, 0.88)),
    _lobe((80, 60, 230), (1, 0.8), (0.32, 0.68),
          shift_from=(0, -28), shift_to=(0, -40), turn_from=20, turn_to=45, stretch_to=(0.88, 1.12)),
    _lobe((200, 90, 255), (0.9, 0.65), (0.3, 0.66),
          shift_from=(20, 20), shift_to=(30, 30), turn_from=130, turn_to=160, stretch_to=(1.12, 0.9)),
]

# The outer aura (520 px) and the inner one (330 px), which are drawn over each other the
# ordinary way; and the white core (100 px), which is lit over everything else. The CSS
# rounds each of their boxes to a circle, but every gradient has faded out before the
# circle's edge at every size it breathes to, so there is nothing for it to clip.
_OUTER_AURA = _Gradient(520, 520, ((0, (90, 110, 255, 0.34)), (0.28, (140, 80, 255, 0.2)),
                                   (0.48, (70, 60, 200, 0.08)), (0.66, (0, 0, 0, 0))))
_INNER_AURA = _Gradient(330, 330, ((0, (120, 190, 255, 0.38)), (0.4, (150, 90, 255, 0.22)),
                                   (0.7, (0, 0, 0, 0))))
_CORE = _Gradient(100, 100, ((0, (242, 251, 255, 1)), (0.35, (200, 235, 255, 0.6)),
                             (0.7, (200, 235, 255, 0))))

# Every animation keeps a place in its cycle, from 0 to 1, in one list: first the five
# orbits' spins, then the five lobes' wobbles, then the outer aura's breath, the inner
# aura's and the core's hum.
_SPINS = slice(0, 5)
_WOBBLES = slice(5, 10)
_OUTER, _INNER, _HUM = 10, 11, 12


def _periods(spins: Sequence[float], wobbles: Sequence[float], outer: float, inner: float,
             hum: float) -> np.ndarray:
    """The seconds each animation takes for a cycle, in the order of the list above."""
    return np.array([*spins, *wobbles, outer, inner, hum], dtype=np.float64)


_LISTENING_WOBBLES = (2.8, 3.4, 4, 3.1, 3.7)

# Seconds a cycle takes in each state, from display.css's ".nova.listening" and the rest:
# a slow drift when listening, quicker and smaller (the scale is the caller's) when
# thinking, and quick ripples with the core and auras pulsing when speaking.
_PERIODS: Dict[str, np.ndarray] = {
    "listening": _periods((8, 10, 12, 9, 14), _LISTENING_WOBBLES, 4.5, 3, 3),
    "thinking": _periods((1.4, 1.8, 1.4, 6, 11), _LISTENING_WOBBLES, 1.6, 3, 3),
    "speaking": _periods((2.4, 3, 3.6, 2.7, 4), (0.6,) * 5, 0.9, 0.6, 0.4),
}

# The second and fourth orbits turn the other way. The auras' breaths and the wobbles
# are the same going forwards or back, so the CSS's "reverse" on those changes nothing.
_SPIN_DIRECTIONS = (1, -1, 1, -1, 1)


def _swing(phase: float) -> float:
    """
    How far through a "0%, 100%" to "50%" keyframe pair an animation is.

    Args:
        phase: How far through its cycle the animation is, from 0 to 1.

    Returns:
        0 at the start of the cycle, 1 halfway and back to 0 at the end, eased in and out
        on each half as the CSS's ease-in-out is (a smoothstep is close enough).
    """
    there = 1 - abs(1 - 2 * phase)
    return there * there * (3 - 2 * there)


def _mix(start: float, end: float, amount: float) -> float:
    """A value that far between start and end."""
    return start + (end - start) * amount


def _lobe_pose(lobe: _Lobe, spin: float,
               swing: float) -> Tuple[Tuple[float, float], float, Tuple[float, float]]:
    """
    Where a lobe is, which way it is turned and how it is stretched.

    Args:
        lobe: The lobe.
        spin: How far its orbit has turned, in radians.
        swing: How far its wobble is between its 0% and 50% keyframes, from 0 to 1.

    Returns:
        Its middle from the orb's middle in the clock's pixels, its turn in radians and
        its stretch across and up, as display.css's transforms put them: the lobe is
        stretched, then turned, then shifted from the middle of its orbit, which then
        turns it all round the orb.
    """
    shift_x = _mix(lobe.shift_from[0], lobe.shift_to[0], swing)
    shift_y = _mix(lobe.shift_from[1], lobe.shift_to[1], swing)
    cosine, sine = math.cos(spin), math.sin(spin)
    centre = (shift_x * cosine - shift_y * sine, shift_x * sine + shift_y * cosine)
    turn = spin + math.radians(_mix(lobe.turn_from, lobe.turn_to, swing))
    stretch = (_mix(1, lobe.stretch_to[0], swing), _mix(1, lobe.stretch_to[1], swing))
    return centre, turn, stretch


class OrbPainter:
    """
    Draws the orb a frame at a time, picking its motion up from where the last frame
    left it.

    Each animation keeps its own place in its cycle and a frame moves each on by the time
    that has passed over its period, so changing state changes how fast everything goes
    and nothing jumps.
    """

    def __init__(self, body_px: int):
        """
        Args:
            body_px: The bright body's width in pixels, already scaled for the screen.
        """
        self.side = window_side(body_px)
        self._unit = body_px / _BODY
        # How far each pixel's middle is from the window's middle: along a row for the
        # columns, down a column for the rows.
        offsets = np.arange(self.side, dtype=np.float32) + 0.5 - self.side / 2
        self._columns = offsets[np.newaxis, :]
        self._rows = offsets[:, np.newaxis]
        self._phases = np.zeros(13, dtype=np.float64)

    def frame(self, state: str, dt: float, opacity: float, scale: float) -> np.ndarray:
        """
        Draws the next frame.

        Args:
            state: "listening", "thinking" or "speaking", which sets how fast each part moves.
            dt: Seconds since the last frame, which moves every animation on.
            opacity: 0 for nothing drawn, to 1 for all of it.
            scale: How big the whole orb is, about its middle: 1 as the clock's, less for
                the smaller orb of thinking, and growing from nothing as it comes in.

        Returns:
            The frame, a (side, side, 4) array of uint8 in blue, green, red and alpha, the
            colours already multiplied by the alpha.

        Raises:
            ValueError: If the state isn't one of those.
        """
        if state not in _PERIODS:
            raise ValueError(f"unknown orb state {state!r}, wanted one of {sorted(_PERIODS)}")
        self._phases = (self._phases + dt / _PERIODS[state]) % 1.0
        opacity = min(max(opacity, 0.0), 1.0)
        if opacity <= 0 or scale <= 0:
            return np.zeros((self.side, self.side, 4), dtype=np.uint8)
        unit = self._unit * scale
        phases = [float(phase) for phase in self._phases]

        # The two auras and the five lobes go on the ordinary way: what is already there
        # shows through whatever is clear of the new layer. The CSS gives the lobes
        # mix-blend-mode: screen too, but each lobe's orbit is animated, which makes it a
        # group of its own, and inside it the lobe has only clear to blend with - so the
        # clock lays the lobes over each other plainly. (Lit over one another they come out
        # washed to white; drawn plainly they match a Chromium render of display.css to
        # within a level in 255, on average.) The list holds each one's arguments to
        # _layer after the unit.
        plain = []
        for gradient, phase in ((_OUTER_AURA, phases[_OUTER]), (_INNER_AURA, phases[_INNER])):
            swing = _swing(phase)
            breath = _mix(0.9, 1.08, swing)
            plain.append((gradient, (0, 0), 0, (breath, breath), _mix(0.75, 1, swing)))
        spins, wobbles = phases[_SPINS], phases[_WOBBLES]
        for lobe, direction, spin, wobble in zip(_LOBES, _SPIN_DIRECTIONS, spins, wobbles):
            pose = _lobe_pose(lobe, direction * 2 * math.pi * spin, _swing(wobble))
            plain.append((lobe.gradient, *pose, 1))

        # Blue, green, red and alpha for every pixel, in 0-1 and premultiplied: what is
        # drawn so far.
        pixels = np.zeros((4, self.side, self.side), dtype=np.float32)
        for arguments in plain:
            layer = self._layer(unit, *arguments)
            pixels *= 1 - layer[3]
            pixels += layer

        # The core is lit over all of that ("screen"): a colour which is already bright
        # gets less brighter, so the white melts into the lobes.
        hum = _mix(0.9, 1.06, _swing(phases[_HUM]))
        layer = self._layer(unit, _CORE, (0, 0), 0, (hum, hum), 1)
        pixels += layer * (1 - pixels)

        # A colour above its alpha would draw brighter than the window is opaque, and
        # rounding could tip it over, so it's held to it first.
        pixels *= opacity
        np.minimum(pixels[:3], pixels[3], out=pixels[:3])
        pixels *= 255
        pixels += 0.5
        frame = np.empty((self.side, self.side, 4), dtype=np.uint8)
        for channel in range(4):
            frame[..., channel] = pixels[channel]
        return frame

    def _layer(self, unit: float, gradient: _Gradient, centre: Tuple[float, float], angle: float,
               stretch: Tuple[float, float], opacity: float) -> np.ndarray:
        """
        Draws one layer, its colours already multiplied by its alpha.

        Args:
            unit: How many pixels one of the clock's is, with the whole orb's scale.
            gradient: What colours it is.
            centre: Where its middle is from the orb's middle, in the clock's pixels.
            angle: How far it is turned, in radians.
            stretch: How much wider and taller than the clock's box it is, before it is turned.
            opacity: How much of it shows, from 0 to 1.

        Returns:
            A (4, side, side) array of blue, green, red and alpha, in 0-1.
        """
        cosine, sine = math.cos(angle), math.sin(angle)
        # Takes a pixel back into the layer's own box, which is as many steps of the
        # gradient's table out as the pixel is far from its middle: the last stop, which
        # is reach out, is _STEPS steps.
        across = _STEPS / (gradient.reach_x * stretch[0])
        down = _STEPS / (gradient.reach_y * stretch[1])
        u_from_x, u_from_y = across * cosine / unit, across * sine / unit
        v_from_x, v_from_y = -down * sine / unit, down * cosine / unit
        u_offset = -across * (cosine * centre[0] + sine * centre[1])
        v_offset = -down * (-sine * centre[0] + cosine * centre[1])

        u = (u_from_x * self._columns + u_offset) + u_from_y * self._rows
        v = (v_from_x * self._columns + v_offset) + v_from_y * self._rows
        u *= u
        v *= v
        u += v
        np.sqrt(u, out=u)
        np.minimum(u, _STEPS, out=u)
        table = gradient.table if opacity == 1 else gradient.table * opacity
        return np.take(table, u.astype(np.intp), axis=1)
