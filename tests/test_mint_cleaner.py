import importlib.util
import os
import tempfile
import unittest
from unittest import mock


SCRIPT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "mint-cleaner.py")
)

SPEC = importlib.util.spec_from_file_location("mint_cleaner", SCRIPT_PATH)
MINT_CLEANER = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MINT_CLEANER)


class DummyVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _make_plan_app(**overrides):
    """Build a minimal DummyApp with all build_plan checkbox vars."""
    defaults = {
        "var_user_cache": False,
        "var_thumbnails": False,
        "var_trash": False,
        "var_firefox": False,
        "var_chrome": False,
        "var_flatpak_app_cache": False,
        "var_config_app_caches": False,
        "var_dev_tool_caches": False,
        "var_user_lang_tool_caches": False,
        "var_python_artifacts": False,
        "var_local_history": False,
        "var_flatpak_user": False,
        "var_flatpak_repair_user": False,
        "var_tmp": False,
        "var_flatpak_syscache": False,
        "var_apt_cache": False,
        "var_system_misc_caches": False,
        "var_system_extra_caches": False,
        "var_flatpak_repair_system": False,
        "var_apt": False,
        "var_journal": False,
        "var_old_kernels": False,
        "journal_retention": "3d",
    }
    defaults.update(overrides)
    app = type("DummyApp", (), {})()
    for key, value in defaults.items():
        setattr(app, key, DummyVar(value))
    app.patterns = {
        "config_app_caches": MINT_CLEANER.CONFIG_CACHE_PATTERNS,
        "dev_tool_caches": MINT_CLEANER.DEV_TOOL_CACHE_PATTERNS,
        "user_lang_tool_caches": MINT_CLEANER.USER_LANG_TOOL_CACHE_PATTERNS,
        "python_artifacts": [],
        "local_history": [],
        "system_misc_caches": MINT_CLEANER.SYSTEM_MISC_CACHE_PATTERNS,
        "system_extra_caches": MINT_CLEANER.SYSTEM_EXTRA_CACHE_PATTERNS,
    }
    app.log = None
    return app


class BuildPlanConfigCacheTests(unittest.TestCase):
    def test_build_plan_includes_config_cache_patterns(self):
        app = _make_plan_app(var_config_app_caches=True)

        plan = MINT_CLEANER.MintCleanerApp.build_plan(app)

        self.assertEqual(plan["user_cmds"], [])
        self.assertEqual(plan["root_rm_patterns"], [])
        self.assertEqual(plan["root_cmds"], [])
        for pattern in MINT_CLEANER.CONFIG_CACHE_PATTERNS:
            self.assertIn(pattern, plan["user_py_delete"])

    def test_build_plan_includes_general_linux_cache_patterns(self):
        app = _make_plan_app(var_dev_tool_caches=True, var_system_misc_caches=True)

        plan = MINT_CLEANER.MintCleanerApp.build_plan(app)

        for pattern in MINT_CLEANER.DEV_TOOL_CACHE_PATTERNS:
            self.assertIn(pattern, plan["user_py_delete"])
        for pattern in MINT_CLEANER.SYSTEM_MISC_CACHE_PATTERNS:
            self.assertIn(pattern, plan["root_rm_patterns"])

    def test_build_plan_includes_new_user_and_system_extra_caches(self):
        app = _make_plan_app(var_user_lang_tool_caches=True, var_system_extra_caches=True)

        plan = MINT_CLEANER.MintCleanerApp.build_plan(app)

        for pattern in MINT_CLEANER.USER_LANG_TOOL_CACHE_PATTERNS:
            self.assertIn(pattern, plan["user_py_delete"])
        for pattern in MINT_CLEANER.SYSTEM_EXTRA_CACHE_PATTERNS:
            self.assertIn(pattern, plan["root_rm_patterns"])

    def test_build_plan_includes_python_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp_home:
            project = os.path.join(tmp_home, "Dokumente", "GitHub", "demo")
            pycache = os.path.join(project, "__pycache__")
            venv = os.path.join(project, ".venv")
            os.makedirs(pycache)
            os.makedirs(venv)
            with open(os.path.join(pycache, "mod.pyc"), "wb") as handle:
                handle.write(b"\0")

            app = _make_plan_app(var_python_artifacts=True)
            with mock.patch.object(
                MINT_CLEANER,
                "find_python_artifact_dirs",
                return_value=[pycache, venv],
            ):
                plan = MINT_CLEANER.MintCleanerApp.build_plan(app)

            self.assertIn(pycache, plan["user_py_delete"])
            self.assertIn(venv, plan["user_py_delete"])
            self.assertEqual(app.patterns["python_artifacts"], [pycache, venv])

    def test_build_plan_includes_local_history(self):
        history_dir = "/tmp/fake-project/.history"
        app = _make_plan_app(var_local_history=True)
        with mock.patch.object(
            MINT_CLEANER,
            "find_local_history_dirs",
            return_value=[history_dir],
        ):
            plan = MINT_CLEANER.MintCleanerApp.build_plan(app)

        self.assertIn(history_dir, plan["user_py_delete"])
        self.assertEqual(app.patterns["local_history"], [history_dir])


class FindPythonArtifactDirsTests(unittest.TestCase):
    def test_finds_pycache_and_venv_under_projects(self):
        with tempfile.TemporaryDirectory() as tmp_home:
            project = os.path.join(tmp_home, "Dokumente", "GitHub", "demo")
            pycache = os.path.join(project, "__pycache__")
            venv = os.path.join(project, ".venv", "lib")
            os.makedirs(pycache)
            os.makedirs(venv)
            nested_skip = os.path.join(tmp_home, ".cache", "proj", "__pycache__")
            os.makedirs(nested_skip)

            found = MINT_CLEANER.find_python_artifact_dirs(root=tmp_home)

            self.assertIn(pycache, found)
            self.assertIn(os.path.join(project, ".venv"), found)
            self.assertNotIn(nested_skip, found)

    def test_skips_active_sys_prefix_venv(self):
        with tempfile.TemporaryDirectory() as tmp_home:
            project = os.path.join(tmp_home, "proj")
            active_venv = os.path.join(project, ".venv")
            other_venv = os.path.join(tmp_home, "other", ".venv")
            os.makedirs(active_venv)
            os.makedirs(other_venv)

            with mock.patch.object(MINT_CLEANER.sys, "prefix", active_venv):
                found = MINT_CLEANER.find_python_artifact_dirs(root=tmp_home)

            self.assertNotIn(active_venv, found)
            self.assertIn(other_venv, found)


class FindLocalHistoryDirsTests(unittest.TestCase):
    def test_finds_history_dirs_and_skips_cache_trees(self):
        with tempfile.TemporaryDirectory() as tmp_home:
            project = os.path.join(tmp_home, "Dokumente", "GitHub", "demo")
            history = os.path.join(project, ".history")
            os.makedirs(history)
            with open(os.path.join(history, "file_20260101010101.py"), "w", encoding="utf-8") as handle:
                handle.write("old")
            nested_skip = os.path.join(tmp_home, ".cache", "proj", ".history")
            os.makedirs(nested_skip)

            found = MINT_CLEANER.find_local_history_dirs(root=tmp_home)

            self.assertIn(history, found)
            self.assertNotIn(nested_skip, found)


class TrashPathsTests(unittest.TestCase):
    def test_trash_paths_fallback_moves_file_and_writes_trashinfo(self):
        with tempfile.TemporaryDirectory() as tmp_home:
            file_path = os.path.join(tmp_home, "to-trash.txt")
            with open(file_path, "w", encoding="utf-8") as file_handle:
                file_handle.write("content")

            with mock.patch.dict(os.environ, {"HOME": tmp_home}, clear=False):
                with mock.patch.object(MINT_CLEANER, "exists_in_path", return_value=False):
                    moved, logs = MINT_CLEANER.trash_paths([file_path])

            self.assertEqual(moved, 1)
            self.assertIn("Moved to Trash:", logs)
            self.assertFalse(os.path.exists(file_path))

            trashed_file = os.path.join(tmp_home, ".local/share/Trash/files", "to-trash.txt")
            info_file = os.path.join(tmp_home, ".local/share/Trash/info", "to-trash.txt.trashinfo")
            self.assertTrue(os.path.isfile(trashed_file))
            self.assertTrue(os.path.isfile(info_file))

            with open(info_file, "r", encoding="utf-8") as info_handle:
                info_content = info_handle.read()
            self.assertIn("[Trash Info]", info_content)
            self.assertIn("Path=", info_content)
            self.assertIn("DeletionDate=", info_content)

    def test_trash_paths_skips_entries_inside_trash_root(self):
        with tempfile.TemporaryDirectory() as tmp_home:
            trash_files_dir = os.path.join(tmp_home, ".local/share/Trash/files")
            os.makedirs(trash_files_dir, exist_ok=True)
            in_trash_file = os.path.join(trash_files_dir, "already-there.txt")
            with open(in_trash_file, "w", encoding="utf-8") as file_handle:
                file_handle.write("content")

            with mock.patch.dict(os.environ, {"HOME": tmp_home}, clear=False):
                moved, logs = MINT_CLEANER.trash_paths([in_trash_file])

            self.assertEqual(moved, 0)
            self.assertEqual(logs, "")
            self.assertTrue(os.path.exists(in_trash_file))

    def test_trash_paths_uses_gio_when_available(self):
        with tempfile.TemporaryDirectory() as tmp_home:
            file_path = os.path.join(tmp_home, "gio-trash.txt")
            with open(file_path, "w", encoding="utf-8") as file_handle:
                file_handle.write("content")

            completed = mock.Mock()
            completed.returncode = 0
            completed.stdout = ""

            with mock.patch.dict(os.environ, {"HOME": tmp_home}, clear=False):
                with mock.patch.object(MINT_CLEANER, "exists_in_path", return_value=True):
                    with mock.patch.object(MINT_CLEANER.subprocess, "run", return_value=completed) as run_mock:
                        moved, logs = MINT_CLEANER.trash_paths([file_path])

            self.assertEqual(moved, 1)
            self.assertIn("Trashed:", logs)
            run_mock.assert_called_once_with(
                ["gio", "trash", file_path],
                text=True,
                stdout=mock.ANY,
                stderr=mock.ANY,
            )


class ProtectedPathTests(unittest.TestCase):
    def test_is_protected_path_for_dconf_and_icons(self):
        self.assertTrue(MINT_CLEANER.is_protected_path("/home/joruf/.cache/dconf"))
        self.assertTrue(MINT_CLEANER.is_protected_path("/home/joruf/.cache/dconf/user"))
        self.assertTrue(MINT_CLEANER.is_protected_path("/home/joruf/.icons/Windows-10-master"))
        self.assertTrue(MINT_CLEANER.is_protected_path("/home/joruf/.local/share/icons/hicolor"))
        self.assertTrue(MINT_CLEANER.is_protected_path("/tmp/.X11-unix"))
        self.assertTrue(MINT_CLEANER.is_protected_path("/tmp/pulse-PKdhtXMmr18n"))
        self.assertFalse(MINT_CLEANER.is_protected_path("/home/joruf/.cache/mozilla"))
        self.assertFalse(MINT_CLEANER.is_protected_path("/tmp/something-safe"))

    def test_rm_paths_skips_protected_dconf(self):
        with tempfile.TemporaryDirectory() as tmp_home:
            dconf_dir = os.path.join(tmp_home, ".cache", "dconf")
            other_dir = os.path.join(tmp_home, ".cache", "mozilla")
            os.makedirs(dconf_dir, exist_ok=True)
            os.makedirs(other_dir, exist_ok=True)
            with open(os.path.join(dconf_dir, "user"), "w", encoding="utf-8") as handle:
                handle.write("x")
            with open(os.path.join(other_dir, "cache.db"), "w", encoding="utf-8") as handle:
                handle.write("y")

            with mock.patch.dict(os.environ, {"HOME": tmp_home}, clear=False):
                removed, logs = MINT_CLEANER.rm_paths([os.path.join(tmp_home, ".cache", "*")])

            self.assertEqual(removed, 1)
            self.assertTrue(os.path.isdir(dconf_dir))
            self.assertFalse(os.path.exists(other_dir))
            self.assertIn("Skipped protected path:", logs)

    def test_sanitize_helper_environment_clears_session_vars(self):
        original = dict(os.environ)
        try:
            os.environ["XDG_RUNTIME_DIR"] = "/run/user/1000"
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/user/1000/bus"
            os.environ["DISPLAY"] = ":0"
            os.environ["HOME"] = "/home/joruf"

            MINT_CLEANER.sanitize_helper_environment()

            self.assertNotIn("XDG_RUNTIME_DIR", os.environ)
            self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", os.environ)
            self.assertNotIn("DISPLAY", os.environ)
            self.assertEqual(os.environ.get("HOME"), "/root")
            self.assertEqual(os.environ.get("XDG_CACHE_HOME"), "/root/.cache")
        finally:
            os.environ.clear()
            os.environ.update(original)

    def test_repair_user_hicolor_shadow_removes_incomplete_index(self):
        with tempfile.TemporaryDirectory() as tmp_home:
            hicolor = os.path.join(tmp_home, ".local/share/icons/hicolor")
            apps = os.path.join(hicolor, "32x32/apps")
            os.makedirs(apps, exist_ok=True)
            index_path = os.path.join(hicolor, "index.theme")
            with open(index_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "[Icon Theme]\nName=Hicolor\nDirectories=32x32/apps\n"
                    "[32x32/apps]\nSize=32\nType=Fixed\n"
                )
            cache_path = os.path.join(hicolor, "icon-theme.cache")
            with open(cache_path, "wb") as handle:
                handle.write(b"x")
            app_icon = os.path.join(apps, "agentforge.png")
            with open(app_icon, "wb") as handle:
                handle.write(b"png")

            with mock.patch.dict(os.environ, {"HOME": tmp_home}, clear=False):
                self.assertTrue(MINT_CLEANER.user_hicolor_shadows_system_icons())
                changed, message = MINT_CLEANER.repair_user_hicolor_shadow()

            self.assertTrue(changed)
            self.assertIn("Repaired incomplete", message)
            self.assertFalse(os.path.exists(index_path))
            self.assertFalse(os.path.exists(cache_path))
            self.assertTrue(os.path.exists(index_path + ".mint-cleaner-backup"))
            self.assertTrue(os.path.exists(app_icon))


if __name__ == "__main__":
    unittest.main()
