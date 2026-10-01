"""
The single-file executable: its paths, its launchers, its child processes, its build.

The executable itself is never started here - ``build-exe.py`` does that after
every build. These tests pretend to be frozen and check that nothing in that
mode reaches for a Python interpreter that is not there (in the executable
``sys.executable`` is the program itself) or writes into the folder it unpacks
itself into.
"""

import importlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bootstrap_ui
import paths
import version
from services import dependencies
from ui import desktop_setup, nemo_setup, window_icon

ROOT = Path(__file__).resolve().parent.parent

SPEC = importlib.util.spec_from_file_location("mint_cleaner_exe", str(ROOT / "run.py"))
MINT_CLEANER = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MINT_CLEANER)


def _build_script():
    """Import ``build-exe.py`` (its name is not a module name)."""
    spec = importlib.util.spec_from_file_location("build_exe", str(ROOT / "build-exe.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FrozenTestCase(unittest.TestCase):
    """Pretend to run as the executable at ``self.executable``."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.bundle = self.tmp / "_MEIabc"
        self.bundle.mkdir()
        self.executable = self.tmp / "bin" / "mint-cleaner-linux-x86_64-0.1.9-build24"
        self.executable.parent.mkdir()
        self.executable.write_bytes(b"exe")
        for patcher in (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "_MEIPASS", str(self.bundle), create=True),
            mock.patch.object(sys, "executable", str(self.executable)),
            mock.patch.object(paths, "IS_FROZEN", True),
            mock.patch.object(desktop_setup, "IS_FROZEN", True),
            mock.patch.object(nemo_setup, "IS_FROZEN", True),
            mock.patch.object(window_icon, "IS_FROZEN", True),
            mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.tmp / "data")}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


# ----------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------
class PathTests(unittest.TestCase):
    def test_a_checkout_finds_its_files_next_to_paths_py(self):
        self.assertEqual(paths._project_root(), ROOT)
        self.assertEqual(paths.INIT_FILE, ROOT / ".initialized")

    def test_the_executable_reads_its_data_from_the_unpacked_folder_and_writes_elsewhere(self):
        # Runs after the patches below are undone: back to the checkout's values.
        self.addCleanup(importlib.reload, paths)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", tmp, create=True), \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(Path(tmp) / "data")}):
            frozen = importlib.reload(paths)
            self.assertTrue(frozen.IS_FROZEN)
            self.assertEqual(frozen.PROJECT_ROOT, Path(tmp))
            self.assertEqual(frozen.RESOURCES_DIR, Path(tmp) / "resources")
            self.assertEqual(frozen.INIT_FILE, Path(tmp) / "data" / "mint-cleaner" / ".initialized")

    def test_the_executable_is_named_after_machine_and_version(self):
        for machine, expected in (
            ("x86_64", "mint-cleaner-linux-x86_64-0.3.4-build31"),
            ("AMD64", "mint-cleaner-linux-x86_64-0.3.4-build31"),
            ("aarch64", "mint-cleaner-linux-aarch64-0.3.4-build31"),
            ("arm64", "mint-cleaner-linux-aarch64-0.3.4-build31"),
        ):
            with mock.patch.object(paths.platform, "machine", return_value=machine):
                self.assertEqual(paths.executable_name("0.3.4", 31), expected)

    def test_the_executable_name_defaults_to_this_version(self):
        self.addCleanup(version.forget)
        with mock.patch.dict(os.environ, {version.ENVIRONMENT_VARIABLE: "0.3.4 31 3b12058 2026-09-29"}), \
                mock.patch.object(paths.platform, "machine", return_value="x86_64"):
            version.forget()
            self.assertEqual(paths.executable_name(), "mint-cleaner-linux-x86_64-0.3.4-build31")


# ----------------------------------------------------------------------
# Version
# ----------------------------------------------------------------------
class FrozenVersionTests(unittest.TestCase):
    def test_the_executable_reads_the_bundled_file_and_never_git_nor_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            stored = "0.1.9 24 1287719 2026-09-29 1790677091\n"
            (root / "VERSION").write_text(stored, encoding="utf-8")
            environment = {k: v for k, v in os.environ.items() if k != version.ENVIRONMENT_VARIABLE}
            with mock.patch.object(version, "FROZEN", True), \
                    mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(version, "_git", side_effect=AssertionError("git at run time")), \
                    mock.patch.object(version, "_write", side_effect=AssertionError("writes VERSION")):
                found = version._resolve(root)
            self.assertEqual(found.label, "0.1.9 (24) · 1287719 · 29.09.2026")
            self.assertEqual((root / "VERSION").read_text(encoding="utf-8"), stored)

    def test_version_flag_answers_without_tkinter_or_display(self):
        environment = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        environment[version.ENVIRONMENT_VARIABLE] = "1.2.3 45 abcdef0 2026-01-02"
        done = subprocess.run(
            [sys.executable, "-X", "importtime", str(ROOT / "run.py"), "--version"],
            capture_output=True, text=True, env=environment, timeout=60, check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Mint Cleaner 1.2.3 (45)", done.stdout)
        self.assertNotIn("tkinter", done.stderr)


# ----------------------------------------------------------------------
# No interpreter calls when frozen
# ----------------------------------------------------------------------
class NoInterpreterTests(FrozenTestCase):
    def test_the_tkinter_check_never_starts_the_program_again(self):
        with mock.patch.object(dependencies.subprocess, "run",
                               side_effect=AssertionError("would start Mint Cleaner")):
            self.assertTrue(dependencies._check_tkinter())

    def test_pip_is_never_run_through_the_executable(self):
        progress = mock.Mock()
        with mock.patch.object(bootstrap_ui, "_run", side_effect=AssertionError("pip via the program")):
            self.assertFalse(bootstrap_ui._fetch(progress, bootstrap_ui.Need(label="x", pip=("x",))))

    def test_the_root_helper_is_the_executable_itself(self):
        self.assertEqual(MINT_CLEANER.root_helper_command(),
                         ["pkexec", str(self.executable.resolve()), "--helper"])

    def test_a_checkout_starts_the_helper_through_python(self):
        with mock.patch.object(paths, "IS_FROZEN", False):
            command = MINT_CLEANER.root_helper_command()
        self.assertEqual(command[:3], ["pkexec", str(self.executable), "-u"])
        self.assertTrue(command[3].endswith("run.py"))
        self.assertEqual(command[4], "--helper")

    def test_the_cleanup_never_deletes_a_running_executable(self):
        """Mint Cleaner and its root helper run from /tmp/_MEI* folders."""
        self.assertTrue(MINT_CLEANER.is_protected_path("/tmp/_MEIx7Yz12"))
        self.assertTrue(MINT_CLEANER.is_protected_path("/tmp/_MEIx7Yz12/libtk8.6.so"))
        self.assertFalse(MINT_CLEANER.is_protected_path("/tmp/other"))


# ----------------------------------------------------------------------
# Launchers and icons
# ----------------------------------------------------------------------
class FrozenLauncherTests(FrozenTestCase):
    def test_the_desktop_entry_starts_the_executable(self):
        content = desktop_setup.build_desktop_entry_content()
        self.assertIn(f'Exec="{self.executable.resolve()}"\n', content)
        self.assertNotIn("run.py", content)
        self.assertNotIn("python3", content)
        self.assertNotIn(str(self.bundle), content)
        icon = [line for line in content.splitlines() if line.startswith("Icon=")]
        self.assertEqual(icon, [f"Icon={desktop_setup._theme_icon_path()}"])

    def test_exec_quoting_follows_the_specification(self):
        self.assertEqual(desktop_setup._exec_quote('/a b/$x"%'), '"/a b/\\\\$x\\\\"%%"')

    def test_the_shortcut_is_a_real_executable_file(self):
        desktop = self.tmp / "Desktop"
        with mock.patch.object(desktop_setup, "user_desktop_dir", return_value=desktop), \
                mock.patch.object(desktop_setup, "_install_theme_icon"):
            ok, shortcut = desktop_setup.install_desktop_shortcut()
            self.assertTrue(ok)
            self.assertFalse(shortcut.is_symlink())
            self.assertTrue(os.access(shortcut, os.X_OK))
            self.assertIn(str(self.executable.resolve()), shortcut.read_text(encoding="utf-8"))
            self.assertFalse(desktop_setup.refresh_desktop_shortcut(), "already current")

    def test_a_checkout_symlink_is_replaced_without_touching_its_template(self):
        desktop = self.tmp / "Desktop"
        desktop.mkdir()
        template = self.tmp / "checkout" / "Mint-Cleaner.desktop"
        template.parent.mkdir()
        template.write_text("[Desktop Entry]\nExec=python3 ./run.py\n", encoding="utf-8")
        shortcut = desktop / desktop_setup.DESKTOP_FILENAME
        shortcut.symlink_to(template)
        with mock.patch.object(desktop_setup, "user_desktop_dir", return_value=desktop), \
                mock.patch.object(desktop_setup, "_install_theme_icon"):
            self.assertTrue(desktop_setup.refresh_desktop_shortcut())
        self.assertFalse(shortcut.is_symlink())
        self.assertIn(str(self.executable.resolve()), shortcut.read_text(encoding="utf-8"))
        self.assertEqual(template.read_text(encoding="utf-8"), "[Desktop Entry]\nExec=python3 ./run.py\n")

    def test_the_nemo_action_starts_the_executable(self):
        content = nemo_setup.build_nemo_action_content()
        self.assertIn(f"Exec={self.executable.resolve()}\n", content)
        self.assertNotIn("python3", content)
        self.assertNotIn(str(self.bundle), content)

    def test_icons_are_rendered_outside_the_unpacked_folder(self):
        self.assertEqual(window_icon.icon_directories(), [self.tmp / "data" / "mint-cleaner" / "icons"])
        written = window_icon.ensure_icon_files(sizes=(16,))
        self.assertEqual([path.parent for path in written], [self.tmp / "data" / "mint-cleaner" / "icons"])
        self.assertEqual(list(self.bundle.iterdir()), [])


# ----------------------------------------------------------------------
# Child processes
# ----------------------------------------------------------------------
class ChildEnvironmentTests(unittest.TestCase):
    def test_children_get_no_paths_into_the_unpacked_files(self):
        """Otherwise apt, pkexec or gio load the program's libraries."""
        env = {
            "LD_LIBRARY_PATH": "/tmp/_MEIabc",
            "LD_LIBRARY_PATH_ORIG": "/opt/lib",
            "TCL_LIBRARY": "/tmp/_MEIabc/_tcl_data",
            "XDG_DATA_DIRS": "/tmp/_MEIabc/share:/usr/share",
            "_PYI_ARCHIVE_FILE": "/home/me/mint-cleaner",
            "HOME": "/home/me",
        }
        with mock.patch.object(paths, "IS_FROZEN", True), \
                mock.patch.object(sys, "_MEIPASS", "/tmp/_MEIabc", create=True):
            self.assertEqual(paths.child_environment(env), {
                "LD_LIBRARY_PATH": "/opt/lib",
                "XDG_DATA_DIRS": "/usr/share",
                "HOME": "/home/me",
            })
            self.assertNotIn("LD_LIBRARY_PATH",
                             paths.child_environment({"LD_LIBRARY_PATH": "/tmp/_MEIabc"}))

    def test_a_checkout_hands_its_environment_on_unchanged(self):
        env = {"LD_LIBRARY_PATH": "/x", "_PYI_X": "y"}
        self.assertEqual(paths.child_environment(env), env)

    def test_every_popen_gets_the_clean_environment(self):
        with mock.patch.object(subprocess, "Popen", subprocess.Popen), \
                mock.patch.object(paths, "IS_FROZEN", True), \
                mock.patch.object(sys, "_MEIPASS", "/tmp/_MEIabc", create=True), \
                mock.patch.dict(os.environ, {"MINT_CLEANER_PROBE": "/tmp/_MEIabc/lib"}):
            paths.use_system_environment_for_children()
            paths.use_system_environment_for_children()
            output = subprocess.run(
                [sys.executable, "-c", "import os; print(os.environ.get('MINT_CLEANER_PROBE'))"],
                stdout=subprocess.PIPE, check=True,
            ).stdout.decode().strip()
            self.assertEqual(output, "None")
            self.assertFalse(getattr(subprocess.Popen.__bases__[0], "_mint_cleaner_clean_env", False),
                             "patched only once")


# ----------------------------------------------------------------------
# The build script and the workflow
# ----------------------------------------------------------------------
class BuildScriptTests(unittest.TestCase):
    def test_only_pyinstaller_is_installed(self):
        self.assertEqual([name.split(">")[0] for name in _build_script().bundled_packages()], ["pyinstaller"])

    def test_the_spec_file_compiles_and_names_the_platform(self):
        build = _build_script()
        text = build.spec_text(Path("/stage"))
        compile(text, "mint-cleaner.spec", "exec")
        self.assertIn("name={0!r}".format(paths.executable_name()), text)
        self.assertIn("[('/stage', '.')]", text)
        self.assertIn("'ui'", text)
        self.assertIn("console=False", text)

    def test_the_stage_carries_the_icon_and_the_version_in_the_checkout_layout(self):
        build = _build_script()
        with tempfile.TemporaryDirectory() as tmp:
            build.STAGE_DIR = Path(tmp) / "stage"
            staged = build.stage_data(version.Version("0.1.9", "24", "1287719", "2026-09-29"))
            self.assertTrue((staged / "resources" / "mint-cleaner.svg").is_file())
            self.assertEqual(version.from_file(staged).label, "0.1.9 (24) · 1287719 · 29.09.2026")

    def test_build_output_is_ignored(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
        self.assertIn("/build/", ignored)
        self.assertIn("/dist/", ignored)

    def test_the_workflow_builds_on_the_oldest_ubuntu_with_the_full_history(self):
        workflow = (ROOT / ".github" / "workflows" / "release-exe.yml").read_text(encoding="utf-8")
        self.assertIn('TAG="v${VERSION}-build${BUILD}"', workflow)
        self.assertIn("runs-on: ubuntu-22.04", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertNotIn("windows-latest", workflow)


if __name__ == "__main__":
    unittest.main()
