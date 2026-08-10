import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from ui import window_icon


def _parse_png(data: bytes) -> dict:
    """Parse a PNG into its signature, IHDR values and concatenated IDAT payload."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "missing PNG signature"
    offset = 8
    header: dict = {"chunks": [], "idat": b""}
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        tag = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        crc = struct.unpack(">I", data[offset + 8 + length:offset + 12 + length])[0]
        assert crc == zlib.crc32(tag + payload) & 0xFFFFFFFF, f"bad CRC in {tag!r}"
        header["chunks"].append(tag)
        if tag == b"IHDR":
            (header["width"], header["height"], header["depth"], header["color_type"],
             _compression, _filter, _interlace) = struct.unpack(">IIBBBBB", payload)
        elif tag == b"IDAT":
            header["idat"] += payload
        offset += 12 + length
    return header


class RenderIconTests(unittest.TestCase):
    def test_render_icon_png_is_valid_rgba_png(self):
        size = 32

        png = window_icon.render_icon_png(size)
        parsed = _parse_png(png)

        self.assertEqual(parsed["width"], size)
        self.assertEqual(parsed["height"], size)
        self.assertEqual(parsed["depth"], 8)
        self.assertEqual(parsed["color_type"], 6)  # RGBA
        self.assertEqual(parsed["chunks"], [b"IHDR", b"IDAT", b"IEND"])
        # One filter byte plus four channels per pixel and row.
        self.assertEqual(len(zlib.decompress(parsed["idat"])), size * (1 + size * 4))

    def test_render_icon_rows_have_transparent_corner_and_opaque_center(self):
        size = 32

        rows = window_icon.render_icon_rows(size)

        self.assertEqual(len(rows), size)
        self.assertEqual(len(rows[0]), size * 4)
        # Rounded corners are cut away, the middle of the tile is fully painted.
        self.assertEqual(rows[0][3], 0)
        self.assertEqual(rows[size // 2][(size // 2) * 4 + 3], 255)

    def test_render_icon_rejects_tiny_sizes(self):
        with self.assertRaises(ValueError):
            window_icon.render_icon_rows(4)


class WindowIconLimitTests(unittest.TestCase):
    def test_window_icon_sizes_fit_into_one_x_property_request(self):
        words = window_icon.window_icon_property_words()

        self.assertLess(words, window_icon.X_MAX_REQUEST_WORDS)
        # The primary size is deliberately excluded, it alone exceeds the limit.
        self.assertNotIn(window_icon.PRIMARY_ICON_SIZE, window_icon.WINDOW_ICON_SIZES)
        self.assertGreater(
            window_icon.window_icon_property_words((window_icon.PRIMARY_ICON_SIZE,)),
            window_icon.X_MAX_REQUEST_WORDS,
        )

    def test_window_icon_sizes_are_rendered_files(self):
        for size in window_icon.WINDOW_ICON_SIZES:
            self.assertIn(size, window_icon.ICON_SIZES)


class WmClassTests(unittest.TestCase):
    def test_published_wm_class_matches_tk_normalization(self):
        # Tk uppercases the first letter of className and lowercases the rest.
        self.assertEqual(window_icon.WM_CLASS_NAME, "Mint-Cleaner")
        self.assertEqual(window_icon.WM_CLASS_PUBLISHED, "Mint-cleaner")


class EnsureIconFilesTests(unittest.TestCase):
    def test_ensure_icon_files_creates_missing_files_only_once(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            assets = Path(tmp_dir) / "resources"
            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": tmp_dir}, clear=False), \
                    mock.patch.object(window_icon, "ICONS_DIR", assets):
                created = window_icon.ensure_icon_files(sizes=(16, 24))

                self.assertEqual([path.name for path in created],
                                 ["mint-cleaner-16.png", "mint-cleaner-24.png"])
                self.assertTrue((assets / "mint-cleaner-16.png").is_file())

                marker = assets / "mint-cleaner-16.png"
                marker.write_bytes(b"untouched")
                window_icon.ensure_icon_files(sizes=(16, 24))

                self.assertEqual(marker.read_bytes(), b"untouched")

    def test_ensure_icon_files_rerenders_with_force(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            assets = Path(tmp_dir) / "resources"
            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": tmp_dir}, clear=False), \
                    mock.patch.object(window_icon, "ICONS_DIR", assets):
                window_icon.ensure_icon_files(sizes=(16,))
                target = assets / "mint-cleaner-16.png"
                target.write_bytes(b"stale")

                window_icon.ensure_icon_files(sizes=(16,), force=True)

                self.assertNotEqual(target.read_bytes(), b"stale")
                self.assertEqual(_parse_png(target.read_bytes())["width"], 16)

    def test_desktop_icon_value_returns_absolute_icon_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            assets = Path(tmp_dir) / "resources"
            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": tmp_dir}, clear=False), \
                    mock.patch.object(window_icon, "ICONS_DIR", assets):
                value = window_icon.desktop_icon_value()

            self.assertTrue(value.startswith(str(assets)), value)
            self.assertTrue(value.endswith(f"mint-cleaner-{window_icon.PRIMARY_ICON_SIZE}.png"))

    def test_desktop_icon_value_falls_back_when_rendering_fails(self):
        with mock.patch.object(window_icon, "ensure_icon_files", return_value=[]):
            self.assertEqual(window_icon.desktop_icon_value(), "edit-clear-symbolic")

    def test_ensure_icon_files_falls_back_to_cache_directory(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            unwritable = Path("/proc/mint-cleaner-assets")
            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": tmp_dir}, clear=False), \
                    mock.patch.object(window_icon, "ICONS_DIR", unwritable):
                created = window_icon.ensure_icon_files(sizes=(16,))

            self.assertEqual(len(created), 1)
            self.assertEqual(created[0], Path(tmp_dir) / "mint-cleaner" / "mint-cleaner-16.png")
            self.assertTrue(created[0].is_file())


if __name__ == "__main__":
    unittest.main()
