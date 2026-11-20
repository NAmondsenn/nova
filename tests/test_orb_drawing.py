"""
Orb Drawing Test
Checks the orb the PC's screen shows while Nova listens, thinks and speaks: that each
frame is a square of premultiplied BGRA, that it is clear at the corners and bright in
the middle, that the colours are the clock's blue and violet, and that time and the
state move it - without any Windows, as the drawing is plain numpy.
"""

import pytest

np = pytest.importorskip("numpy")

from voice_assistant.orb_drawing import SIZES, OrbPainter, window_side

STATES = ("listening", "thinking", "speaking")


def _mean_difference(a, b):
    """How far apart two frames are, as the mean absolute difference per value."""
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


def _ten_frames(state):
    """What a fresh painter draws at the end of ten 0.1 second frames of one state."""
    painter = OrbPainter(64)
    frame = None
    for _ in range(10):
        frame = painter.frame(state, 0.1, 1, 1)
    return frame


def test_the_sizes_are_the_settings_choices():
    assert SIZES == {"small": 48, "medium": 64, "large": 96}


def test_the_window_is_the_clocks_aura_in_size():
    # The aura is 520 px beside the 300 px body, rounded up to a whole pixel.
    assert window_side(64) == 111
    assert window_side(300) == 520
    assert window_side(48) == 84
    assert window_side(96) == 167


def test_a_frame_is_square_bgra_bytes():
    painter = OrbPainter(64)
    frame = painter.frame("listening", 0.03, 1, 1)
    assert painter.side == window_side(64)
    assert frame.shape == (window_side(64), window_side(64), 4)
    assert frame.dtype == np.uint8


@pytest.mark.parametrize("state", STATES)
def test_colour_never_exceeds_alpha(state):
    # Premultiplied alpha, which UpdateLayeredWindow needs: a colour above its alpha
    # would draw a brighter pixel than the window is opaque.
    painter = OrbPainter(64)
    for _ in range(5):
        frame = painter.frame(state, 0.1, 0.7, 1)
        assert (frame[..., :3] <= frame[..., 3:4]).all()


def test_the_corners_are_clear_and_the_middle_is_solid():
    frame = OrbPainter(64).frame("listening", 0.03, 1, 1)
    for corner in (frame[0, 0], frame[0, -1], frame[-1, 0], frame[-1, -1]):
        assert corner[3] == 0
    middle = frame[frame.shape[0] // 2, frame.shape[1] // 2]
    assert middle[3] >= 230
    # The white core, which comes out B, G, R, A.
    assert (middle[:3] >= 200).all()


def test_it_is_blue_and_violet_off_centre():
    frame = OrbPainter(64).frame("listening", 0.03, 1, 1).astype(np.int16)
    side = frame.shape[0]
    rows, columns = np.indices((side, side))
    near = np.hypot(rows - (side - 1) / 2, columns - (side - 1) / 2) <= 0.4 * side
    blue, green, red = frame[..., 0], frame[..., 1], frame[..., 2]
    assert (near & (blue > red + 40)).any()
    assert (near & (red > green + 40) & (blue > green + 40)).any()


@pytest.mark.parametrize("state", STATES)
def test_the_lobes_are_not_washed_to_white(state):
    # The clock lays its lobes over each other plainly, so only the core is white and the
    # blue and violet show round it; lit over each other they melt to white instead.
    painter = OrbPainter(64)
    for _ in range(10):
        frame = painter.frame(state, 0.3, 1, 1)
        solid = frame[..., 3] > 128
        white = solid & (frame[..., :3].min(axis=2) >= 225)
        assert white.sum() < 0.04 * solid.sum()


@pytest.mark.parametrize("state", STATES)
def test_opacity_nothing_draws_nothing(state):
    frame = OrbPainter(64).frame(state, 0.03, 0, 1)
    assert frame.shape == (111, 111, 4)
    assert not frame.any()


def test_a_half_faded_orb_is_dimmer_but_the_same_shape():
    full = OrbPainter(64).frame("listening", 0.03, 1, 1).astype(np.int16)
    half = OrbPainter(64).frame("listening", 0.03, 0.5, 1).astype(np.int16)
    assert ((full > 0) == (half > 0)).mean() > 0.95
    assert abs(half[..., 3].astype(float).mean() / full[..., 3].mean() - 0.5) < 0.02


def test_time_moves_it():
    painter = OrbPainter(64)
    first = painter.frame("listening", 0.03, 1, 1)
    later = painter.frame("listening", 0.5, 1, 1)
    assert _mean_difference(first, later) > 1


def test_no_time_passing_keeps_it_still():
    painter = OrbPainter(64)
    first = painter.frame("speaking", 0.3, 1, 1)
    assert np.array_equal(first, painter.frame("speaking", 0, 1, 1))


def test_each_state_moves_differently():
    listening = _ten_frames("listening")
    speaking = _ten_frames("speaking")
    thinking = _ten_frames("thinking")
    assert not np.array_equal(listening, speaking)
    assert not np.array_equal(listening, thinking)
    assert not np.array_equal(thinking, speaking)


def test_changing_state_doesnt_jump():
    # Every animation keeps its own phase, so a new state only changes how fast it goes.
    painter = OrbPainter(64)
    last = None
    for _ in range(10):
        last = painter.frame("listening", 0.03, 1, 1)
    after = painter.frame("speaking", 0.03, 1, 1)
    assert _mean_difference(last, after) < 6


def test_scale_shrinks_it():
    painter = OrbPainter(64)
    full = painter.frame("thinking", 0.03, 1, 1)
    small = OrbPainter(64).frame("thinking", 0.03, 1, 0.82)
    assert (small[..., 3] > 128).sum() < (full[..., 3] > 128).sum()


def test_nothing_scale_draws_nothing():
    assert not OrbPainter(64).frame("listening", 0.03, 1, 0).any()


def test_a_bigger_body_is_a_bigger_orb_of_the_same_look():
    small = OrbPainter(48)
    large = OrbPainter(96)
    first = small.frame("listening", 0.03, 1, 1)
    second = large.frame("listening", 0.03, 1, 1)
    assert first.shape[0] == small.side < large.side == second.shape[0]
    # The same fraction of the window is the lit part of the orb, give or take rounding.
    lit_small = (first[..., 3] > 128).mean()
    lit_large = (second[..., 3] > 128).mean()
    assert abs(lit_small - lit_large) < 0.02


def test_an_unknown_state_is_refused():
    with pytest.raises(ValueError):
        OrbPainter(64).frame("dancing", 0.03, 1, 1)
