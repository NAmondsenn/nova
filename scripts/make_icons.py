"""
Make Icons.

Draws Nova's tray icons - the orb, ready, with a red slash when the microphone is muted,
and grey when the brain isn't reachable - as Windows .ico files, from the same code which
draws the orb on the screen (voice_assistant/orb_drawing.py). The files are committed in
scripts/windows/icons/, and the build only copies them, so this is run by hand when the
look changes:

    python scripts/make_icons.py scripts/windows/icons

It needs numpy (as the orb does) and nothing else: the PNGs are written with zlib and
struct, so it runs on the Windows PC as well as anywhere else. Next to the icons it writes
preview.png, every state at every size on a dark and a light strip, to judge them by.
"""

import functools
import math
import os
import struct
import sys
import zlib
from typing import List, Sequence, Tuple

import numpy as np

# Add the project root to the path so the voice_assistant package imports work no matter
# where the script is run from, as the other scripts do.
PROJECT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIRECTORY)

from voice_assistant.orb_drawing import OrbPainter

# The sizes Windows asks an icon for: 16 and 20 to 32 for the tray at 100% to 200% scaling,
# 48 and 256 for the shell's larger views.
SIZES = (16, 20, 24, 32, 48, 256)

# What the icons show: the orb as it is, with the microphone muted, and with no brain.
STATES = ("ready", "muted", "offline")

# The icon file each state is written to.
FILES = {"ready": "nova.ico", "muted": "nova-muted.ico", "offline": "nova-offline.ico"}

# The orb is drawn this big (its body's width in pixels) and reduced from there.
_BODY = 384

# The moment of the orb's drift which is drawn: this many frames of 0.1 s of "listening".
# It is fixed here, so the files never change between runs. At two seconds in the lobes are
# spread round the core, with the blue and violet both showing, so the orb reads as lopsided
# colour at 256 and as a violet disc with a bright middle when small.
_MOMENT_FRAMES = 20
_MOMENT_STEP = 0.1

# Up to this many pixels the picture is cropped to the body, and made more solid, because
# the orb's aura - nearly three quarters of the window - would be a faint smudge. (Seen at
# 32 and 48 without it the orb was a third of the box, small beside the 16, which fills
# its own.)
_SMALL = 48

# How much of the large picture a small one shows, as a multiple of the body's width, which
# leaves the body's soft edge in and the aura out.
_SMALL_WINDOW = 1.05

# A small picture is faded out to nothing between these distances from its middle, as a
# fraction of its side, so the aura's raised tail doesn't leave a square: the picture is a
# round orb, not a tinted box.
_SMALL_FADE = (0.4, 0.5)

# A small picture's alpha is raised to this power, which brings the soft edge in towards a
# solid one, so the orb is a shape on the taskbar rather than a glow.
_SMALL_ALPHA_POWER = 0.5

# The slash on a muted icon: red, over a dark outline which is twice as wide so it shows on
# a red-ish orb and on a light taskbar (colours are red, green, blue).
_SLASH = (255, 90, 106)
_OUTLINE = (20, 21, 26)
_SLASH_WIDTH = 1 / 9

# The strips of preview.png (red, green, blue), and the gap round each picture.
_DARK = (31, 32, 35)
_LIGHT = (238, 241, 246)
_GAP = 16

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _weights(count: int, start: float, stop: float, size: int) -> np.ndarray:
    """
    The weights which reduce part of a row of pixels to fewer by averaging their area.

    Args:
        count: How many pixels the row has.
        start: Where in the row the part begins, in pixels (it needn't be a whole one).
        stop: Where it ends.
        size: How many pixels the part is reduced to.

    Returns:
        A (size, count) array: row i holds how much of each source pixel lies in the i-th
        output pixel, as a fraction of the output pixel, so each row adds up to 1 (so far as
        the part lies in the row).
    """
    step = (stop - start) / size
    low = (start + step * np.arange(size))[:, np.newaxis]
    high = low + step
    left = np.arange(count)[np.newaxis, :]
    overlap = np.minimum(high, left + 1) - np.maximum(low, left)
    return np.clip(overlap, 0, None) / step


@functools.lru_cache(maxsize=1)
def _orb() -> np.ndarray:
    """The orb at its fixed moment: premultiplied BGRA, uint8, drawn large."""
    painter = OrbPainter(_BODY)
    frame = painter.frame("listening", 0, 1, 1)
    for _ in range(_MOMENT_FRAMES):
        frame = painter.frame("listening", _MOMENT_STEP, 1, 1)
    return frame


def _ready(size: int) -> np.ndarray:
    """The orb at a size, straight (not premultiplied) BGRA, as floats from 0 to 255."""
    large = _orb().astype(np.float64)
    side = large.shape[0]
    small = size <= _SMALL
    if small:
        half = _BODY * _SMALL_WINDOW / 2
        start, stop = side / 2 - half, side / 2 + half
    else:
        start, stop = 0, side
    weights = _weights(side, start, stop, size)
    # Averaging has to be done on premultiplied colour, or the clear pixels round the edge,
    # which have no colour of their own, would drag the orb's edge towards black.
    rows = np.tensordot(weights, large, axes=(1, 0))
    reduced = np.einsum("jl,ilc->ijc", weights, rows)
    alpha = reduced[..., 3:4]
    straight = np.where(alpha > 0, reduced[..., :3] / np.maximum(alpha, 1e-9) * 255, 0)
    alpha = alpha / 255
    if small:
        centre = (np.arange(size) + 0.5) / size - 0.5
        radius = np.hypot(centre[:, np.newaxis], centre[np.newaxis, :])
        inner, outer = _SMALL_FADE
        fade = np.clip((outer - radius) / (outer - inner), 0, 1)
        alpha = (alpha * (fade * fade * (3 - 2 * fade))[..., np.newaxis]) ** _SMALL_ALPHA_POWER
    return np.concatenate([np.clip(straight, 0, 255), alpha * 255], axis=2)


def _over(picture: np.ndarray, colour: Tuple[int, int, int], coverage: np.ndarray) -> np.ndarray:
    """
    Lays a colour over a picture where the coverage says.

    Args:
        picture: Straight BGRA floats from 0 to 255.
        colour: Red, green and blue out of 255.
        coverage: How much of each pixel the colour covers, from 0 to 1, (size, size).

    Returns:
        The picture with the colour on it, in the same form.
    """
    red, green, blue = colour
    new = np.array([blue, green, red], dtype=np.float64)
    cover = coverage[..., np.newaxis]
    old_alpha = picture[..., 3:4] / 255
    alpha = cover + old_alpha * (1 - cover)
    mixed = new * cover + picture[..., :3] * old_alpha * (1 - cover)
    straight = np.where(alpha > 0, mixed / np.maximum(alpha, 1e-9), 0)
    return np.concatenate([straight, alpha * 255], axis=2)


def _slash(picture: np.ndarray) -> np.ndarray:
    """A picture with the muted slash from its bottom-left to its top-right."""
    size = picture.shape[0]
    # Distance from each pixel's middle to the diagonal, which is where row + column is
    # size. The slash runs from 12% to 88% of the way across, so its round ends stay in
    # the box, and is measured from the nearest point on that segment.
    rows, columns = np.mgrid[0:size, 0:size] + 0.5
    along = (columns - rows) / math.sqrt(2)
    across = np.abs(rows + columns - size) / math.sqrt(2)
    reach = size * 0.76 / math.sqrt(2)
    beyond = np.maximum(np.abs(along) - reach, 0)
    distance = np.hypot(across, beyond)
    width = size * _SLASH_WIDTH
    for colour, half in ((_OUTLINE, width), (_SLASH, width / 2)):
        # Half a pixel either side of the edge is blended, which anti-aliases it.
        coverage = np.clip(half + 0.5 - distance, 0, 1)
        picture = _over(picture, colour, coverage)
    return picture


def picture(state: str, size: int) -> np.ndarray:
    """
    The icon's picture.

    Args:
        state: One of STATES.
        size: The side in pixels.

    Returns:
        A (size, size, 4) array of uint8, blue, green, red and alpha, not premultiplied.

    Raises:
        ValueError: If the state isn't one of those.
    """
    if state not in STATES:
        raise ValueError(f"unknown icon state {state!r}, wanted one of {STATES}")
    ready = _ready(size)
    if state == "muted":
        ready = _slash(ready)
    elif state == "offline":
        blue, green, red = ready[..., 0], ready[..., 1], ready[..., 2]
        grey = 0.114 * blue + 0.587 * green + 0.299 * red
        ready = np.concatenate([np.repeat(grey[..., np.newaxis], 3, axis=2), ready[..., 3:4]],
                               axis=2)
    return np.clip(np.floor(ready + 0.5), 0, 255).astype(np.uint8)


def _chunk(kind: bytes, data: bytes) -> bytes:
    """One chunk of a PNG file: its length, kind, data and checksum."""
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def png(rgba: np.ndarray) -> bytes:
    """
    A PNG file, written with zlib and struct only.

    Args:
        rgba: A (height, width, 4) uint8 array of red, green, blue and alpha.

    Returns:
        The file's bytes.
    """
    height, width = rgba.shape[:2]
    # Every row begins with a zero, which is the filter "none".
    rows = np.concatenate([np.zeros((height, 1), dtype=np.uint8), rgba.reshape(height, width * 4)],
                          axis=1)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (_PNG_SIGNATURE + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(rows.tobytes(), 9)) + _chunk(b"IEND", b""))


def _bitmap(bgra: np.ndarray) -> bytes:
    """
    The inside of a small .ico entry: a 32-bit bitmap, then the AND mask.

    Args:
        bgra: A (size, size, 4) uint8 array, straight.

    Returns:
        The BITMAPINFOHEADER, the pixels bottom row first, and the mask. The mask is set
        where the picture is clear, as old tools which ignore the alpha look at it.
    """
    size = bgra.shape[0]
    # The height is doubled: the bitmap is followed by a mask of the same height.
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    pixels = bgra[::-1].tobytes()
    # Each mask row is one bit a pixel, padded to a whole number of 32-bit words.
    clear = np.packbits(bgra[::-1, :, 3] == 0, axis=1)
    padded = np.zeros((size, (size + 31) // 32 * 4), dtype=np.uint8)
    padded[:, :clear.shape[1]] = clear
    return header + pixels + padded.tobytes()


def ico(pictures: Sequence[np.ndarray]) -> bytes:
    """
    A Windows .ico file.

    Args:
        pictures: Straight BGRA pictures, as picture() returns, each square.

    Returns:
        The file's bytes. The 256 picture is a PNG, the others 32-bit bitmaps with an AND
        mask: the old C# compiler, which builds Nova.exe, refuses a PNG in a small entry.
    """
    datas: List[bytes] = []
    for bgra in pictures:
        if bgra.shape[0] >= 256:
            rgba = bgra[..., [2, 1, 0, 3]]
            datas.append(png(np.ascontiguousarray(rgba)))
        else:
            datas.append(_bitmap(bgra))
    out = struct.pack("<HHH", 0, 1, len(pictures))
    offset = 6 + 16 * len(pictures)
    for bgra, data in zip(pictures, datas):
        size = bgra.shape[0]
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(datas)


def _on(bgra: np.ndarray, background: Tuple[int, int, int]) -> np.ndarray:
    """A picture over a plain background, as opaque red, green, blue floats."""
    alpha = bgra[..., 3:4].astype(np.float64) / 255
    colour = bgra[..., [2, 1, 0]].astype(np.float64)
    return colour * alpha + np.array(background, dtype=np.float64) * (1 - alpha)


def preview() -> np.ndarray:
    """
    Every state at every size on a dark strip and a light one, to judge the icons by.

    Returns:
        A PNG-ready (height, width, 4) uint8 array. Each strip has a row for each state:
        the sizes side by side, then the 16 magnified four times with its pixels showing.
    """
    rows = []
    for background in (_DARK, _LIGHT):
        for state in STATES:
            tiles = []
            for size in SIZES:
                tiles.append(_on(picture(state, size), background))
            small = _on(picture(state, 16), background)
            tiles.append(np.repeat(np.repeat(small, 4, axis=0), 4, axis=1))
            height = max(tile.shape[0] for tile in tiles) + 2 * _GAP
            strip = np.empty((height, _GAP, 3))
            strip[:] = background
            for tile in tiles:
                top = (height - tile.shape[0]) // 2
                column = np.empty((height, tile.shape[1], 3))
                column[:] = background
                column[top:top + tile.shape[0]] = tile
                gap = np.empty((height, _GAP, 3))
                gap[:] = background
                strip = np.concatenate([strip, column, gap], axis=1)
            rows.append(strip)
    image = np.concatenate(rows, axis=0)
    rgba = np.empty(image.shape[:2] + (4,), dtype=np.uint8)
    rgba[..., :3] = np.clip(np.floor(image + 0.5), 0, 255)
    rgba[..., 3] = 255
    return rgba


def main(folder: str) -> None:
    """Writes the three icons and preview.png into the folder."""
    os.makedirs(folder, exist_ok=True)
    for state, name in FILES.items():
        with open(os.path.join(folder, name), "wb") as handle:
            handle.write(ico([picture(state, size) for size in SIZES]))
    with open(os.path.join(folder, "preview.png"), "wb") as handle:
        handle.write(png(preview()))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/make_icons.py <folder>")
    main(sys.argv[1])
