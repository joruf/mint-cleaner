import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ui import window_icon
from ui import desktop_setup
from ui import nemo_setup


class DesktopEntryTests(unittest.TestCase):
    def test_desktop_entry_points_to_run_py_with_icon_and_wm_class(self):
        content = desktop_setup.build_desktop_entry_content()

        self.assertIn(f"Exec=python3 {desktop_setup.MINT_CLEANER_SCRIPT}", content)
        self.assertTrue(str(desktop_setup.MINT_CLEANER_SCRIPT).endswith("/run.py"))
        self.assertNotIn("mint-cleaner.py", content)
        self.assertIn(f"StartupWMClass={window_icon.WM_CLASS_PUBLISHED}", content)
        icon_lines = [line for line in content.splitlines() if line.startswith("Icon=")]
        self.assertEqual(len(icon_lines), 1)
        self.assertNotEqual(icon_lines[0], "Icon=edit-clear-symbolic")

    def test_desktop_entry_uses_fallback_template_without_template_file(self):
        with mock.patch.object(desktop_setup, "DESKTOP_TEMPLATE", Path("/nonexistent.desktop")):
            content = desktop_setup.build_desktop_entry_content()

        self.assertTrue(content.startswith("[Desktop Entry]"))
        self.assertIn(f"Exec=python3 {desktop_setup.MINT_CLEANER_SCRIPT}", content)
        self.assertIn(f"StartupWMClass={window_icon.WM_CLASS_PUBLISHED}", content)

    def test_refresh_desktop_shortcut_updates_legacy_entry(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            desktop_dir = Path(tmp_dir)
            shortcut = desktop_dir / desktop_setup.DESKTOP_FILENAME
            shortcut.write_text(
                "[Desktop Entry]\nName=Mint Cleaner\n"
                "Exec=python3 /opt/mint-cleaner/mint-cleaner.py\n"
                "Icon=edit-clear-symbolic\n",
                encoding="utf-8",
            )

            with mock.patch.object(desktop_setup, "user_desktop_dir", return_value=desktop_dir):
                changed = desktop_setup.refresh_desktop_shortcut()
                unchanged = desktop_setup.refresh_desktop_shortcut()

            self.assertTrue(changed)
            self.assertFalse(unchanged)
            content = shortcut.read_text(encoding="utf-8")
            self.assertNotIn("mint-cleaner.py", content)
            self.assertIn("run.py", content)
            self.assertTrue(os.access(shortcut, os.X_OK))

    def test_refresh_desktop_shortcut_ignores_missing_shortcut(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with mock.patch.object(
                desktop_setup, "user_desktop_dir", return_value=Path(tmp_dir)
            ):
                self.assertFalse(desktop_setup.refresh_desktop_shortcut())

    def test_install_desktop_shortcut_writes_executable_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            desktop_dir = Path(tmp_dir) / "Desktop"
            with mock.patch.object(desktop_setup, "user_desktop_dir", return_value=desktop_dir):
                success, path = desktop_setup.install_desktop_shortcut()

            self.assertTrue(success)
            self.assertIsNotNone(path)
            self.assertTrue(os.access(path, os.X_OK))
            self.assertIn("run.py", path.read_text(encoding="utf-8"))


class NemoActionTests(unittest.TestCase):
    def test_nemo_action_points_to_run_py(self):
        content = nemo_setup.build_nemo_action_content()

        self.assertIn(f"Exec=python3 {nemo_setup.MINT_CLEANER_SCRIPT}", content)
        self.assertTrue(str(nemo_setup.MINT_CLEANER_SCRIPT).endswith("/run.py"))
        self.assertNotIn("mint-cleaner.py", content)
        self.assertIn("Icon=", content)

    def test_refresh_nemo_action_updates_legacy_action(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            actions_dir = Path(tmp_dir)
            action = actions_dir / nemo_setup.ACTION_FILENAME
            action.write_text(
                "[Nemo Action]\nName=Mint Cleaner\n"
                "Exec=python3 /opt/mint-cleaner/mint-cleaner.py\n",
                encoding="utf-8",
            )

            with mock.patch.object(nemo_setup, "NEMO_ACTIONS_DIR", actions_dir):
                changed = nemo_setup.refresh_nemo_action()
                unchanged = nemo_setup.refresh_nemo_action()

            self.assertTrue(changed)
            self.assertFalse(unchanged)
            self.assertIn("run.py", action.read_text(encoding="utf-8"))

    def test_refresh_nemo_action_ignores_missing_action(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with mock.patch.object(nemo_setup, "NEMO_ACTIONS_DIR", Path(tmp_dir)):
                self.assertFalse(nemo_setup.refresh_nemo_action())

    def test_install_nemo_action_creates_directory_and_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            actions_dir = Path(tmp_dir) / "nemo" / "actions"
            with mock.patch.object(nemo_setup, "NEMO_ACTIONS_DIR", actions_dir):
                self.assertTrue(nemo_setup.install_nemo_action())

            action = actions_dir / nemo_setup.ACTION_FILENAME
            self.assertTrue(action.is_file())
            self.assertIn("run.py", action.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
