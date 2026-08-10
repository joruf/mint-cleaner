#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Application icon for Mint Cleaner.

The icon is rendered from signed distance fields and written as PNG with the
Python standard library only, so no image library and no binary asset checked
into the repository are required. Rendered files are cached in ``resources/`` and
reused on later starts.

The module also applies the icon to Tk windows (``_NET_WM_ICON``), which is what
Cinnamon, GNOME, KDE and Xfce panels use to show a taskbar icon for a running
window.
"""

from __future__ import annotations

import math
import os
import struct
import zlib
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from paths import ICON_BASENAME, RESOURCES_DIR

# Rendered icons are written next to the .desktop template that references them.
ICONS_DIR = RESOURCES_DIR

# Sizes written to resources/. The first entry is the primary icon used by the
# .desktop entry and the Nemo action.
ICON_SIZES: Tuple[int, ...] = (256, 128, 64, 48)
PRIMARY_ICON_SIZE: int = ICON_SIZES[0]

# Sizes handed to Tk for _NET_WM_ICON. Tk writes all of them in a single
# XChangeProperty request, and X rejects requests above XMaxRequestSize
# (65535 words of 4 bytes). A 256x256 icon alone already needs 65538 words, so
# including it silently leaves the property empty and the taskbar without any
# icon. 128 + 64 + 48 needs about 89 KB and covers every panel size.
WINDOW_ICON_SIZES: Tuple[int, ...] = (128, 64, 48)

# Maximum number of 32 bit words an X property request may carry.
X_MAX_REQUEST_WORDS = 65535

# Class name handed to tk.Tk(className=...). Tk publishes it as WM_CLASS.
WM_CLASS_NAME = "Mint-Cleaner"

# Tk normalizes the class part of WM_CLASS to "first letter upper, rest lower",
# so the window really reports "Mint-cleaner". StartupWMClass in .desktop files
# must use exactly that value, otherwise the panel cannot map the running window
# to the launcher and shows a second, iconless taskbar entry.
WM_CLASS_PUBLISHED = WM_CLASS_NAME[:1].upper() + WM_CLASS_NAME[1:].lower()

# Palette: Linux Mint style green gradient with a white broom.
BACKGROUND_TOP: Tuple[int, int, int] = (0x63, 0xDC, 0x8F)
BACKGROUND_BOTTOM: Tuple[int, int, int] = (0x10, 0x83, 0x50)
FOREGROUND: Tuple[int, int, int] = (0xFF, 0xFF, 0xFF)
ACCENT_DARK: Tuple[int, int, int] = (0x0C, 0x62, 0x3C)

# Geometry in unit coordinates (0..1, y axis pointing down).
_HANDLE_START: Tuple[float, float] = (0.685, 0.150)
_HANDLE_END: Tuple[float, float] = (0.470, 0.505)
_HANDLE_RADIUS = 0.042

_HEAD_QUAD: Tuple[Tuple[float, float], ...] = (
    (0.374, 0.467),
    (0.546, 0.568),
    (0.472, 0.895),
    (0.126, 0.693),
)
_HEAD_CORNER_RADIUS = 0.014

_FERRULE_START: Tuple[float, float] = (0.322, 0.506)
_FERRULE_END: Tuple[float, float] = (0.538, 0.632)
_FERRULE_RADIUS = 0.030

_BRISTLE_LINES: Tuple[Tuple[Tuple[float, float], Tuple[float, float]], ...] = (
    ((0.346, 0.613), (0.229, 0.730)),
    ((0.389, 0.638), (0.309, 0.777)),
    ((0.432, 0.663), (0.389, 0.824)),
)
_BRISTLE_RADIUS = 0.009

_SPARKLES: Tuple[Tuple[float, float, float, float], ...] = (
    (0.745, 0.700, 0.050, 0.95),
    (0.868, 0.545, 0.028, 0.80),
    (0.660, 0.880, 0.024, 0.70),
)


# ----------------------------- Signed distance fields -----------------------------

def _clamp01(value: float) -> float:
    """
    Clamp a value into the range 0..1.

    @param value Input value
    @return float Clamped value
    """
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _coverage(distance: float, edge: float) -> float:
    """
    Convert a signed distance into an anti-aliased coverage value.

    @param distance Signed distance, negative inside the shape
    @param edge Width of the anti-aliasing band in unit coordinates
    @return float Coverage between 0 and 1
    """
    return _clamp01(0.5 - distance / edge)


def _sdf_rounded_rect(
    x: float,
    y: float,
    center_x: float,
    center_y: float,
    half_width: float,
    half_height: float,
    radius: float,
) -> float:
    """
    Signed distance to an axis-aligned rounded rectangle.

    @param x Sample x coordinate
    @param y Sample y coordinate
    @param center_x Rectangle center x
    @param center_y Rectangle center y
    @param half_width Half width including the corner radius
    @param half_height Half height including the corner radius
    @param radius Corner radius
    @return float Signed distance, negative inside
    """
    dx = abs(x - center_x) - (half_width - radius)
    dy = abs(y - center_y) - (half_height - radius)
    outside = math.hypot(max(dx, 0.0), max(dy, 0.0))
    return outside + min(max(dx, dy), 0.0) - radius


def _sdf_capsule(
    x: float,
    y: float,
    start: Tuple[float, float],
    end: Tuple[float, float],
    radius: float,
) -> float:
    """
    Signed distance to a capsule (thick line with round caps).

    @param x Sample x coordinate
    @param y Sample y coordinate
    @param start Segment start point
    @param end Segment end point
    @param radius Capsule radius
    @return float Signed distance, negative inside
    """
    ax, ay = start
    bx, by = end
    ex, ey = bx - ax, by - ay
    wx, wy = x - ax, y - ay
    length_squared = ex * ex + ey * ey
    if length_squared <= 0.0:
        return math.hypot(wx, wy) - radius
    t = _clamp01((wx * ex + wy * ey) / length_squared)
    return math.hypot(wx - t * ex, wy - t * ey) - radius


def _sdf_circle(x: float, y: float, center: Tuple[float, float], radius: float) -> float:
    """
    Signed distance to a circle.

    @param x Sample x coordinate
    @param y Sample y coordinate
    @param center Circle center
    @param radius Circle radius
    @return float Signed distance, negative inside
    """
    return math.hypot(x - center[0], y - center[1]) - radius


def _sdf_convex_polygon(
    x: float,
    y: float,
    points: Sequence[Tuple[float, float]],
) -> float:
    """
    Distance field of a convex polygon wound clockwise in screen coordinates.

    Exact along the edges and conservative near the vertices, which is precise
    enough for anti-aliasing straight edged shapes.

    @param x Sample x coordinate
    @param y Sample y coordinate
    @param points Polygon corners, clockwise with the y axis pointing down
    @return float Signed distance, negative inside
    """
    worst = -1.0e9
    count = len(points)
    for index in range(count):
        ax, ay = points[index]
        bx, by = points[(index + 1) % count]
        ex, ey = bx - ax, by - ay
        length = math.hypot(ex, ey) or 1.0
        normal_x, normal_y = ey / length, -ex / length
        worst = max(worst, (x - ax) * normal_x + (y - ay) * normal_y)
    return worst


def _mix(
    color_a: Tuple[int, int, int],
    color_b: Tuple[int, int, int],
    amount: float,
) -> Tuple[float, float, float]:
    """
    Linearly interpolate between two RGB colors.

    @param color_a Color at amount 0
    @param color_b Color at amount 1
    @param amount Blend factor between 0 and 1
    @return tuple[float, float, float] Blended color
    """
    return (
        color_a[0] + (color_b[0] - color_a[0]) * amount,
        color_a[1] + (color_b[1] - color_a[1]) * amount,
        color_a[2] + (color_b[2] - color_a[2]) * amount,
    )


# ----------------------------- Rendering -----------------------------

Layer = Tuple[Callable[[float, float], float], Tuple[int, int, int], float]


def _icon_layers() -> List[Layer]:
    """
    Build the drawing layers of the icon in painting order.

    @return list[Layer] Layers as (distance function, color, alpha)
    """
    layers: List[Layer] = []

    layers.append((
        lambda x, y: _sdf_convex_polygon(x, y, _HEAD_QUAD) - _HEAD_CORNER_RADIUS,
        FOREGROUND,
        1.0,
    ))
    layers.append((
        lambda x, y: _sdf_capsule(x, y, _HANDLE_START, _HANDLE_END, _HANDLE_RADIUS),
        FOREGROUND,
        1.0,
    ))
    for start, end in _BRISTLE_LINES:
        layers.append((
            lambda x, y, a=start, b=end: _sdf_capsule(x, y, a, b, _BRISTLE_RADIUS),
            ACCENT_DARK,
            0.45,
        ))
    layers.append((
        lambda x, y: _sdf_capsule(x, y, _FERRULE_START, _FERRULE_END, _FERRULE_RADIUS),
        ACCENT_DARK,
        0.85,
    ))
    for center_x, center_y, radius, alpha in _SPARKLES:
        layers.append((
            lambda x, y, cx=center_x, cy=center_y, r=radius: _sdf_circle(x, y, (cx, cy), r),
            FOREGROUND,
            alpha,
        ))
    return layers


def render_icon_rows(size: int) -> List[bytes]:
    """
    Render the icon into raw RGBA scanlines.

    @param size Edge length of the square icon in pixels
    @return list[bytes] One RGBA row per pixel line
    """
    if size < 8:
        raise ValueError("icon size must be at least 8 pixels")

    edge = 1.5 / size
    layers = _icon_layers()
    rows: List[bytes] = []

    for pixel_y in range(size):
        y = (pixel_y + 0.5) / size
        row = bytearray()
        for pixel_x in range(size):
            x = (pixel_x + 0.5) / size
            background = _coverage(
                _sdf_rounded_rect(x, y, 0.5, 0.5, 0.48, 0.48, 0.22), edge
            )
            if background <= 0.0:
                row += b"\x00\x00\x00\x00"
                continue

            red, green, blue = _mix(
                BACKGROUND_TOP, BACKGROUND_BOTTOM, _clamp01(x * 0.35 + y * 0.65)
            )
            for distance_fn, color, alpha in layers:
                weight = _coverage(distance_fn(x, y), edge) * alpha
                if weight <= 0.0:
                    continue
                red, green, blue = _mix((red, green, blue), color, weight)

            row += bytes((
                int(red + 0.5),
                int(green + 0.5),
                int(blue + 0.5),
                int(background * 255.0 + 0.5),
            ))
        rows.append(bytes(row))
    return rows


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    """
    Build a single PNG chunk including length and CRC.

    @param tag Four byte chunk type
    @param data Chunk payload
    @return bytes Encoded chunk
    """
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def encode_png(rows: Sequence[bytes], width: int, height: int) -> bytes:
    """
    Encode RGBA scanlines as an 8 bit RGBA PNG file.

    @param rows RGBA scanlines, each width * 4 bytes long
    @param width Image width in pixels
    @param height Image height in pixels
    @return bytes Complete PNG file content
    """
    raw = b"".join(b"\x00" + row for row in rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


def render_icon_png(size: int) -> bytes:
    """
    Render the icon and return it as PNG bytes.

    @param size Edge length of the square icon in pixels
    @return bytes PNG file content
    """
    return encode_png(render_icon_rows(size), size, size)


# ----------------------------- Asset handling -----------------------------

def icon_directories() -> List[Path]:
    """
    Return the directories searched for cached icon files, preferred first.

    resources/ next to the program is used normally. The user cache directory is
    the fallback for read-only installations.

    @return list[Path] Candidate directories
    """
    cache_home = os.environ.get("XDG_CACHE_HOME")
    cache_base = Path(cache_home) if cache_home else Path.home() / ".cache"
    return [ICONS_DIR, cache_base / "mint-cleaner"]


def icon_path(size: int = PRIMARY_ICON_SIZE, directory: Optional[Path] = None) -> Path:
    """
    Return the file path of the icon for a given size.

    @param size Edge length of the square icon in pixels
    @param directory Directory to use, defaults to resources/
    @return Path Icon file path
    """
    base = ICONS_DIR if directory is None else directory
    return base / f"{ICON_BASENAME}-{size}.png"


def find_icon_file(size: int) -> Optional[Path]:
    """
    Return an already rendered icon file for a size, if there is one.

    @param size Edge length of the square icon in pixels
    @return Path | None Existing icon path
    """
    for directory in icon_directories():
        candidate = icon_path(size, directory)
        if candidate.is_file():
            return candidate
    return None


def ensure_icon_files(sizes: Sequence[int] = ICON_SIZES, force: bool = False) -> List[Path]:
    """
    Make sure the icon PNG files exist and return the available ones.

    Rendering happens only for missing files, so normal starts do no work.

    @param sizes Icon sizes to provide
    @param force Re-render even when a file already exists
    @return list[Path] Paths of all available icon files
    """
    available: List[Path] = []
    for size in sizes:
        existing = None if force else find_icon_file(size)
        if existing is not None:
            available.append(existing)
            continue

        data: Optional[bytes] = None
        for directory in icon_directories():
            try:
                if data is None:
                    data = render_icon_png(size)
                directory.mkdir(parents=True, exist_ok=True)
                target = icon_path(size, directory)
                target.write_bytes(data)
                available.append(target)
                break
            except OSError:
                continue
    return available


def primary_icon_file() -> Optional[Path]:
    """
    Return the path of the primary icon, rendering it when missing.

    @return Path | None Icon path, or None when it could not be created
    """
    paths = ensure_icon_files()
    for path in paths:
        if path.name == f"{ICON_BASENAME}-{PRIMARY_ICON_SIZE}.png":
            return path
    return paths[0] if paths else None


def desktop_icon_value(fallback: str = "edit-clear-symbolic") -> str:
    """
    Return the value for the Icon= key of .desktop files and Nemo actions.

    Uses the absolute path of the generated icon, which needs no icon theme
    installation, and falls back to a stock theme icon name.

    @param fallback Icon name used when the file cannot be created
    @return str Icon path or icon name
    """
    path = primary_icon_file()
    return str(path) if path is not None else fallback


def window_icon_property_words(sizes: Sequence[int] = WINDOW_ICON_SIZES) -> int:
    """
    Return the number of 32 bit words _NET_WM_ICON needs for the given sizes.

    Two words of width and height precede the pixels of every image.

    @param sizes Icon sizes handed to Tk
    @return int Required property size in words
    """
    return sum(size * size + 2 for size in sizes)


def apply_window_icon(window) -> bool:
    """
    Set the window icon so the panel and window list show a Mint Cleaner icon.

    Hands the panel sized icons to Tk, which publishes them as _NET_WM_ICON.
    References are kept on the window to survive garbage collection.

    @param window Tk root or Toplevel window
    @return bool True when at least one icon size was applied
    """
    import tkinter as tk

    paths = ensure_icon_files(sizes=WINDOW_ICON_SIZES)
    if not paths:
        return False

    images = []
    for path in paths:
        try:
            images.append(tk.PhotoImage(master=window, file=str(path)))
        except Exception:
            continue
    if not images:
        return False

    try:
        window.iconphoto(True, *images)
    except Exception:
        try:
            window.iconphoto(True, images[0])
        except Exception:
            return False

    # Keep a reference, Tk does not own the images.
    window._mint_cleaner_icons = images  # type: ignore[attr-defined]
    return True


if __name__ == "__main__":
    for created in ensure_icon_files(force=True):
        print(created)
