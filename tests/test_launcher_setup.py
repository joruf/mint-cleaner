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

        self.assertIn("./run.py", content)
        self.assertIn("%k", content)
        self.assertNotIn("mint-cleaner.py", content)
        self.assertIn(f"StartupWMClass={window_icon.WM_CLASS_PUBLISHED}", content)
        icon_lines = [line for line in content.splitlines() if line.startswith("Icon=")]
        self.assertEqual(icon_lines, ["Icon=mint-cleaner"])

    def test_desktop_entry_uses_fallback_template_without_template_file(self):
        with mock.patch.object(desktop_setup, "DESKTOP_TEMPLATE", Path("/nonexistent.desktop")):
            content = desktop_setup.build_desktop_entry_content()

        self.assertTrue(content.startswith("[Desktop Entry]"))
        self.assertIn("./run.py", content)
        self.assertIn("%k", content)
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
            self.assertTrue(shortcut.is_symlink())
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
            self.assertTrue(path.is_symlink())
            self.assertTrue(os.access(path, os.X_OK))
            self.assertIn("run.py", path.read_text(encoding="utf-8"))


class DesktopShortcutToggleTests(unittest.TestCase):
    def test_install_query_and_remove_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            desktop_dir = Path(tmp_dir) / "Desktop"
            with mock.patch.object(desktop_setup, "user_desktop_dir", return_value=desktop_dir):
                self.assertFalse(desktop_setup.desktop_shortcut_installed())

                self.assertTrue(desktop_setup.install_desktop_shortcut()[0])
                self.assertTrue(desktop_setup.desktop_shortcut_installed())

                self.assertTrue(desktop_setup.remove_desktop_shortcut())
                self.assertFalse(desktop_setup.desktop_shortcut_installed())
                # Removing again is not an error.
                self.assertTrue(desktop_setup.remove_desktop_shortcut())


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


class NemoActionToggleTests(unittest.TestCase):
    def test_install_query_and_remove_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            actions_dir = Path(tmp_dir) / "nemo" / "actions"
            with mock.patch.object(nemo_setup, "NEMO_ACTIONS_DIR", actions_dir):
                self.assertFalse(nemo_setup.nemo_action_installed())

                self.assertTrue(nemo_setup.install_nemo_action())
                self.assertTrue(nemo_setup.nemo_action_installed())
                self.assertEqual(nemo_setup.nemo_action_path().parent, actions_dir)

                self.assertTrue(nemo_setup.remove_nemo_action())
                self.assertFalse(nemo_setup.nemo_action_installed())
                # Removing again is not an error.
                self.assertTrue(nemo_setup.remove_nemo_action())


class NoFirstRunPromptTests(unittest.TestCase):
    def test_first_run_prompts_are_gone(self):
        """The menu checkboxes are the only place that controls the integration."""
        self.assertFalse(hasattr(nemo_setup, "maybe_prompt_nemo_setup"))
        self.assertFalse(hasattr(desktop_setup, "maybe_prompt_desktop_setup"))


if __name__ == "__main__":
    unittest.main()
