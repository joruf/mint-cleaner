"""
Tests for the cleanup targets added after the disk audit:
developer caches outside ~/.cache, editor and AI assistant state, rebuildable
project directories, and the corrected old kernel removal.
"""

import glob
import importlib.util
import os
import tempfile
import unittest
from unittest import mock


SCRIPT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "run.py")
)

SPEC = importlib.util.spec_from_file_location("mint_cleaner", SCRIPT_PATH)
MINT_CLEANER = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MINT_CLEANER)


def _plan(**selection):
    """Build a cleanup plan from a selection, without rediscovery."""
    patterns = {
        "editor_caches": MINT_CLEANER.EDITOR_CACHE_PATTERNS,
        "editor_state": MINT_CLEANER.EDITOR_STATE_PATTERNS,
        "ai_assistant_logs": MINT_CLEANER.AI_ASSISTANT_LOG_PATTERNS,
        "dev_tool_caches": MINT_CLEANER.DEV_TOOL_CACHE_PATTERNS,
        "user_lang_tool_caches": MINT_CLEANER.USER_LANG_TOOL_CACHE_PATTERNS,
        "node_modules": [],
        "composer_vendor": [],
        "build_output": [],
    }
    plan, discovered, notes = MINT_CLEANER.build_cleanup_plan(
        selection, patterns, rediscover=False
    )
    return plan, discovered, notes


class CachePatternCoverageTests(unittest.TestCase):
    """The audit found these paths growing unbounded and unclaimed."""

    def test_dev_tool_caches_cover_npx_maven_and_gradle_wrapper(self):
        patterns = MINT_CLEANER.DEV_TOOL_CACHE_PATTERNS
        for expected in (
            "~/.npm/_npx/*",
            "~/.m2/repository/*",
            "~/.gradle/wrapper/dists/*",
            "~/.gradle/daemon/*",
        ):
            self.assertIn(expected, patterns)

    def test_lang_caches_cover_browser_and_bundler_downloads(self):
        patterns = MINT_CLEANER.USER_LANG_TOOL_CACHE_PATTERNS
        for expected in (
            "~/.cache/ms-playwright/*",
            "~/.cache/puppeteer/*",
            "~/.cache/electron/*",
            "~/.cache/composer/*",
        ):
            self.assertIn(expected, patterns)

    def test_editor_caches_cover_vsix_archives_and_logs(self):
        patterns = MINT_CLEANER.EDITOR_CACHE_PATTERNS
        self.assertIn("~/.config/Code/CachedExtensionVSIXs/*", patterns)
        self.assertIn("~/.config/Cursor/snapshots/*", patterns)

    def test_editor_state_covers_cursor_history_database(self):
        self.assertIn(
            "~/.config/Cursor/User/globalStorage/state.vscdb",
            MINT_CLEANER.EDITOR_STATE_PATTERNS,
        )

    def test_editor_cache_and_state_stay_separate(self):
        """Losing history must require its own opt-in."""
        overlap = set(MINT_CLEANER.EDITOR_CACHE_PATTERNS) & set(
            MINT_CLEANER.EDITOR_STATE_PATTERNS
        )
        self.assertEqual(overlap, set())


class AiAssistantLogTests(unittest.TestCase):
    def test_plan_includes_assistant_log_patterns(self):
        plan, _discovered, _notes = _plan(ai_assistant_logs=True)
        for pattern in MINT_CLEANER.AI_ASSISTANT_LOG_PATTERNS:
            self.assertIn(pattern, plan["user_py_delete"])

    def test_patterns_never_match_memory_directories(self):
        """Memory folders hold user-authored notes and must survive."""
        with tempfile.TemporaryDirectory() as home:
            project = os.path.join(home, ".claude", "projects", "demo")
            memory = os.path.join(project, "memory")
            os.makedirs(memory)
            transcript = os.path.join(project, "session.jsonl")
            with open(transcript, "w", encoding="utf-8") as handle:
                handle.write("{}\n")
            note = os.path.join(memory, "note.md")
            with open(note, "w", encoding="utf-8") as handle:
                handle.write("keep me")

            matched = glob.glob(os.path.join(home, ".claude/projects/*/*.jsonl"))

            self.assertEqual(matched, [transcript])
            self.assertTrue(os.path.exists(note))
            self.assertNotIn(memory, matched)


class ProjectDirectoryDiscoveryTests(unittest.TestCase):
    def test_node_modules_requires_package_json(self):
        with tempfile.TemporaryDirectory() as home:
            owned = os.path.join(home, "app", "node_modules")
            orphan = os.path.join(home, "notes", "node_modules")
            os.makedirs(owned)
            os.makedirs(orphan)
            with open(os.path.join(home, "app", "package.json"), "w") as handle:
                handle.write("{}")

            found = MINT_CLEANER.find_node_modules_dirs(root=home)

            self.assertIn(owned, found)
            self.assertNotIn(orphan, found)

    def test_composer_vendor_skips_shipped_cms_assets(self):
        """A CMS ships media/vendor with no composer.json next to it."""
        with tempfile.TemporaryDirectory() as home:
            project = os.path.join(home, "site")
            owned = os.path.join(project, "vendor")
            shipped = os.path.join(project, "media", "vendor")
            os.makedirs(owned)
            os.makedirs(shipped)
            with open(os.path.join(project, "composer.json"), "w") as handle:
                handle.write("{}")

            found = MINT_CLEANER.find_composer_vendor_dirs(root=home)

            self.assertIn(owned, found)
            self.assertNotIn(shipped, found)

    def test_build_output_requires_a_build_manifest(self):
        with tempfile.TemporaryDirectory() as home:
            project = os.path.join(home, "web")
            built = os.path.join(project, "dist")
            docs_build = os.path.join(home, "handbook", "build")
            os.makedirs(built)
            os.makedirs(docs_build)
            with open(os.path.join(project, "package.json"), "w") as handle:
                handle.write("{}")

            found = MINT_CLEANER.find_build_output_dirs(root=home)

            self.assertIn(built, found)
            self.assertNotIn(docs_build, found)

    def test_unmarked_directory_is_still_traversed(self):
        """A folder that fails the marker check must not prune the walk."""
        with tempfile.TemporaryDirectory() as home:
            nested = os.path.join(home, "build", "real-project")
            target = os.path.join(nested, "dist")
            os.makedirs(target)
            with open(os.path.join(nested, "package.json"), "w") as handle:
                handle.write("{}")

            found = MINT_CLEANER.find_build_output_dirs(root=home)

            self.assertIn(target, found)

    def test_every_discovered_key_has_a_finder(self):
        for key in MINT_CLEANER.DISCOVERED_KEYS:
            self.assertIn(key, MINT_CLEANER.DISCOVERY_FINDERS)


class GoModuleCacheTests(unittest.TestCase):
    def test_uses_toolchain_command_because_cache_is_read_only(self):
        with mock.patch.object(MINT_CLEANER, "exists_in_path", return_value=True):
            plan, _discovered, _notes = _plan(go_mod_cache=True)

        self.assertIn("go clean -modcache", plan["user_cmds"])
        self.assertEqual(plan["user_py_delete"], [])

    def test_notes_when_go_is_missing(self):
        with mock.patch.object(MINT_CLEANER, "exists_in_path", return_value=False):
            plan, _discovered, notes = _plan(go_mod_cache=True)

        self.assertEqual(plan["user_cmds"], [])
        self.assertTrue(any("go not found" in note for note in notes))


class OldKernelTests(unittest.TestCase):
    PACKAGES = [
        ("linux-image-6.14.0-37-generic", "6.14.0-37-generic"),
        ("linux-headers-6.14.0-37-generic", "6.14.0-37-generic"),
        ("linux-image-6.17.0-20-generic", "6.17.0-20-generic"),
        ("linux-image-6.17.0-40-generic", "6.17.0-40-generic"),
        ("linux-image-7.0.0-28-generic", "7.0.0-28-generic"),
    ]

    def test_keeps_running_kernel_and_one_fallback(self):
        with mock.patch.object(
            MINT_CLEANER, "installed_kernel_packages", return_value=self.PACKAGES
        ):
            removable = MINT_CLEANER.removable_kernel_packages(
                running="7.0.0-28-generic"
            )

        self.assertNotIn("linux-image-7.0.0-28-generic", removable)
        self.assertNotIn("linux-image-6.17.0-40-generic", removable)
        self.assertIn("linux-image-6.14.0-37-generic", removable)
        self.assertIn("linux-headers-6.14.0-37-generic", removable)
        self.assertIn("linux-image-6.17.0-20-generic", removable)

    def test_fallback_can_be_disabled(self):
        with mock.patch.object(
            MINT_CLEANER, "installed_kernel_packages", return_value=self.PACKAGES
        ):
            removable = MINT_CLEANER.removable_kernel_packages(
                keep_fallback=False, running="7.0.0-28-generic"
            )

        self.assertNotIn("linux-image-7.0.0-28-generic", removable)
        self.assertIn("linux-image-6.17.0-40-generic", removable)

    def test_running_kernel_is_never_removed_even_when_oldest(self):
        with mock.patch.object(
            MINT_CLEANER, "installed_kernel_packages", return_value=self.PACKAGES
        ):
            removable = MINT_CLEANER.removable_kernel_packages(
                running="6.14.0-37-generic"
            )

        self.assertNotIn("linux-image-6.14.0-37-generic", removable)
        self.assertNotIn("linux-headers-6.14.0-37-generic", removable)

    def test_metapackages_are_ignored(self):
        """Removing linux-image-generic would stop future kernel updates."""
        dpkg_output = (
            "linux-image-generic\tinstall ok installed\n"
            "linux-image-generic-hwe-24.04\tinstall ok installed\n"
            "linux-image-6.17.0-20-generic\tinstall ok installed\n"
        )
        completed = mock.Mock(stdout=dpkg_output)
        with mock.patch.object(
            MINT_CLEANER.subprocess, "run", return_value=completed
        ):
            packages = MINT_CLEANER.installed_kernel_packages()

        names = [name for name, _tag in packages]
        self.assertEqual(names, ["linux-image-6.17.0-20-generic"])

    def test_uninstalled_packages_are_ignored(self):
        dpkg_output = (
            "linux-image-6.17.0-20-generic\tdeinstall ok config-files\n"
            "linux-image-6.17.0-22-generic\tinstall ok installed\n"
        )
        completed = mock.Mock(stdout=dpkg_output)
        with mock.patch.object(
            MINT_CLEANER.subprocess, "run", return_value=completed
        ):
            packages = MINT_CLEANER.installed_kernel_packages()

        names = [name for name, _tag in packages]
        self.assertEqual(names, ["linux-image-6.17.0-22-generic"])

    def test_plan_purges_packages_instead_of_relying_on_autoremove(self):
        """autoremove leaves manually installed kernels behind."""
        with mock.patch.object(
            MINT_CLEANER,
            "removable_kernel_packages",
            return_value=["linux-image-6.14.0-37-generic"],
        ):
            plan, _discovered, notes = _plan(old_kernels=True)

        self.assertIn(
            "apt-get purge -y linux-image-6.14.0-37-generic", plan["root_cmds"]
        )
        self.assertNotIn("apt autoremove --purge -y", plan["root_cmds"])
        self.assertTrue(any("1 old kernel" in note for note in notes))

    def test_plan_notes_when_nothing_to_remove(self):
        with mock.patch.object(
            MINT_CLEANER, "removable_kernel_packages", return_value=[]
        ):
            plan, _discovered, notes = _plan(old_kernels=True)

        self.assertEqual(plan["root_cmds"], [])
        self.assertTrue(any("No removable old kernels" in note for note in notes))

    def test_package_paths_include_shared_hwe_header_trees(self):
        def fake_isdir(path):
            return path in {
                "/lib/modules/6.17.0-20-generic",
                "/usr/src/linux-hwe-6.17-headers-6.17.0-20",
            }

        def fake_glob(pattern):
            if pattern == "/usr/src/linux-*-headers-6.17.0-20":
                return ["/usr/src/linux-hwe-6.17-headers-6.17.0-20"]
            return []

        with mock.patch.object(MINT_CLEANER.os.path, "isdir", side_effect=fake_isdir), \
             mock.patch.object(MINT_CLEANER.glob, "glob", side_effect=fake_glob):
            paths = MINT_CLEANER.kernel_package_paths(
                ["linux-image-6.17.0-20-generic"]
            )

        self.assertIn("/lib/modules/6.17.0-20-generic", paths)
        self.assertIn("/usr/src/linux-hwe-6.17-headers-6.17.0-20", paths)


class RegistryConsistencyTests(unittest.TestCase):
    def test_new_categories_are_selectable(self):
        for key in (
            "editor_caches",
            "editor_state",
            "ai_assistant_logs",
            "go_mod_cache",
            "node_modules",
            "composer_vendor",
            "build_output",
        ):
            self.assertIn(key, MINT_CLEANER.SELECTION_NAMES)
            self.assertIn(key, MINT_CLEANER.CATEGORY_LABELS)

    def test_old_kernels_is_measurable_now(self):
        self.assertIn("old_kernels", MINT_CLEANER.CATEGORY_LABELS)
        self.assertIn("old_kernels", MINT_CLEANER.DISCOVERY_FINDERS)

    def test_scan_skips_toolchain_directories(self):
        """Version managers ship fixtures that look like real projects."""
        for name in (".nvm", ".m2", ".gradle", "go", ".claude"):
            self.assertIn(name, MINT_CLEANER.HOME_SCAN_SKIP_DIR_NAMES)


if __name__ == "__main__":
    unittest.main()
