"""
Make Icons Test
Checks the tray icons drawn from the orb: that each picture is the size asked for, that the
small ones are solid enough to show on a taskbar of either colour, that muted has its red
slash and offline is grey, that an .ico holds every size in the form old Windows tools accept
(PNG only for 256), and that the committed files are what the script makes.
"""

import importlib.util
import math
import os
import struct

import pytest

np = pytest.importorskip("numpy")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICONS = os.path.join(ROOT, "scripts", "windows", "icons")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def load_make_icons():
    """Imports scripts/make_icons.py, which isn't in a package."""
    path = os.path.join(ROOT, "scripts", "make_icons.py")
    spec = importlib.util.spec_from_file_location("make_icons", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_icons = load_make_icons()
SIZES = make_icons.SIZES
STATES = make_icons.STATES


def _entries(data):
    """The directory entries of an .ico: (width byte, offset, length) for each."""
    count = struct.unpack_from("<H", data, 4)[0]
    entries = []
    for index in range(count):
        width, _, _, _, _, _, length, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * index)
        entries.append((width, offset, length))
    return entries


def _all_pictures(state):
    return [make_icons.picture(state, size) for size in SIZES]


@pytest.mark.parametrize("size", SIZES)
def test_a_picture_is_the_size_asked_for(size):
    picture = make_icons.picture("ready", size)
    assert picture.shape == (size, size, 4)
    assert picture.dtype == np.uint8


def test_the_small_orb_fills_its_box():
    small = make_icons.picture("ready", 16)
    assert (small[..., 3] > 128).mean() >= 0.45
    big = make_icons.picture("ready", 256)
    for row, column in ((0, 0), (0, 255), (255, 0), (255, 255)):
        assert big[row, column, 3] == 0


def test_the_small_orb_shows_on_a_light_taskbar():
    small = make_icons.picture("ready", 16).astype(np.float64)
    alpha = small[..., 3:4] / 255
    light = np.array([246, 241, 238], dtype=np.float64)
    composite = small[..., :3] * alpha + light * (1 - alpha)
    middle = composite[4:12, 4:12]
    assert np.abs(middle - light).mean() > 60


def _quadrant_shares(weights):
    """What fraction of the total weight lies in each quarter about the middle: TL, TR, BL, BR."""
    half = weights.shape[0] // 2
    total = weights.sum()
    return [weights[:half, :half].sum() / total, weights[:half, half:].sum() / total,
            weights[half:, :half].sum() / total, weights[half:, half:].sum() / total]


@pytest.mark.parametrize("size", [48, 256])
def test_the_orb_is_balanced_round_its_middle(size):
    # The orb on the screen drifts to one side at most moments, which in a picture looked
    # like the core pushed out of its own orb: the icon's is set evenly round the core.
    alpha = make_icons.picture("ready", size)[..., 3].astype(np.float64)
    rows, columns = np.mgrid[0:size, 0:size] + 0.5
    middle = size / 2
    assert abs((alpha * columns).sum() / alpha.sum() - middle) < 0.03 * size
    assert abs((alpha * rows).sum() / alpha.sum() - middle) < 0.03 * size
    assert all(0.21 < share < 0.29 for share in _quadrant_shares(alpha))


@pytest.mark.parametrize("size", [48, 256])
def test_the_core_is_in_the_middle(size):
    picture = make_icons.picture("ready", size).astype(np.float64)
    brightness = picture[..., :3].sum(axis=2) * picture[..., 3] / 255
    # The core is a flat patch of white, so the middle of the brightest pixels is its.
    brightest = brightness >= brightness.max() * 0.98
    rows, columns = np.mgrid[0:size, 0:size] + 0.5
    assert abs(columns[brightest].mean() - size / 2) < 0.04 * size
    assert abs(rows[brightest].mean() - size / 2) < 0.04 * size


def test_the_colour_reaches_as_far_in_every_direction():
    # The soft glow hides lopsidedness from the totals above, so this looks at the body
    # alone, where it is more than half solid: its quarters weigh the same, and along
    # eight rays from the middle it ends at much the same distance.
    size = 256
    solid = make_icons.picture("ready", size)[..., 3] > 128
    assert all(0.22 < share < 0.28 for share in _quadrant_shares(solid.astype(np.float64)))
    reaches = []
    for step in range(8):
        angle = step * math.pi / 4
        distance = 0.0
        while solid[int(size / 2 + distance * math.sin(angle)), int(size / 2 + distance * math.cos(angle))]:
            distance += 0.25
        reaches.append(distance)
    assert min(reaches) > 0.85 * max(reaches)


def _slash_pixels(picture):
    """How many red pixels lie within 3 px of the bottom-left to top-right diagonal."""
    size = picture.shape[0]
    blue, green, red = (picture[..., channel].astype(int) for channel in range(3))
    is_red = (red > 200) & (green < 120) & (blue < 140)
    rows, columns = np.mgrid[0:size, 0:size]
    # Pixel (row, column) is on the diagonal when row + column is size - 1.
    near = np.abs(rows + columns - (size - 1)) / np.sqrt(2) <= 3
    return int((is_red & near).sum())


def test_muted_has_a_red_slash():
    assert _slash_pixels(make_icons.picture("muted", 32)) >= 20
    assert _slash_pixels(make_icons.picture("ready", 32)) == 0


def test_offline_is_grey():
    picture = make_icons.picture("offline", 32).astype(int)
    solid = picture[picture[..., 3] > 128][:, :3]
    assert len(solid) > 0
    assert (solid.max(axis=1) - solid.min(axis=1) <= 40).all()


def test_an_ico_holds_every_size():
    data = make_icons.ico(_all_pictures("ready"))
    assert data[:4] == b"\x00\x00\x01\x00"
    assert struct.unpack_from("<H", data, 4)[0] == len(SIZES)
    entries = _entries(data)
    assert [width for width, _, _ in entries] == [0 if size == 256 else size for size in SIZES]
    for _, offset, length in entries:
        assert 0 < offset and offset + length <= len(data)


def test_the_256_entry_is_a_png_and_the_rest_are_bitmaps():
    data = make_icons.ico(_all_pictures("ready"))
    by_size = dict(zip(SIZES, _entries(data)))
    _, offset, _ = by_size[256]
    assert data[offset:offset + 8] == PNG_SIGNATURE
    _, offset, _ = by_size[16]
    header_size, _, height = struct.unpack_from("<Iii", data, offset)
    assert header_size == 40
    # A bitmap in an .ico counts the AND mask's rows too, so its height is doubled.
    assert height == 32


@pytest.mark.parametrize("state, name", [("ready", "nova.ico"), ("muted", "nova-muted.ico"),
                                         ("offline", "nova-offline.ico")])
def test_the_committed_icons_are_what_the_script_makes(state, name):
    with open(os.path.join(ICONS, name), "rb") as handle:
        assert handle.read() == make_icons.ico(_all_pictures(state))
