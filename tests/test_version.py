"""
Tests for the version number derived from the history.

Nobody types this number, which is the whole point, and also why nothing but a
test notices when the derivation goes wrong. A rule that counts the wrong
commits produces a number that looks every bit as plausible as the right one.

The rule:

* minor counts commits in which a cleanup category arrived
* patch counts commits since the last of those
* build counts every commit

And one principle that matters more than any of the digits: an unknown version
says "unknown". A made-up 0.0.0 looks real and sends a bug report the wrong way.
"""

import os
import shutil
import subprocess
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

import version

requires_git = unittest.skipUnless(shutil.which("git"), "git is not installed")


def main_script(categories: int) -> str:
    """Build a main script that declares the given number of categories."""
    keys = "\n".join(f'    "cat_{index}": "Category {index}",' for index in range(categories))
    return f'CATEGORY_LABELS = {{\n{keys}\n}}\n'


class Repo:
    """A throwaway repository to build histories in."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.categories = 0
        self.env = dict(os.environ)
        self.env.update({
            "GIT_CONFIG_GLOBAL": str(root / ".gitconfig"),
            "GIT_CONFIG_SYSTEM": str(root / ".gitconfig-system"),
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        })
        self.git("init", "-b", "main")

    def git(self, *args: str) -> None:
        """Run one git command."""
        subprocess.run(["git", *args], cwd=self.root, env=self.env, check=True,
                       capture_output=True)

    def commit(self, *paths: str, message: str = "change") -> None:
        """Append to files and commit them."""
        for relative in paths:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            previous = target.read_text(encoding="utf-8") if target.exists() else ""
            target.write_text(previous + "x\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-m", message)

    def add_category(self, script: str = "run.py", message: str = "add category") -> None:
        """Commit a main script with one more cleanup category than before."""
        self.categories += 1
        (self.root / script).write_text(main_script(self.categories), encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-m", message)

    def touch_script(self, script: str = "run.py") -> None:
        """Commit a change to the main script that adds no category."""
        path = self.root / script
        path.write_text(path.read_text(encoding="utf-8") + "# note\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-m", "edit script")


def derive(root: Path):
    """Derive the version of a repository, bypassing every cache."""
    return version.from_git(root, version.history_marker(root))


class CategoryCountTests(unittest.TestCase):
    """Reading the category table out of one revision of the script."""

    def test_counts_the_keys_of_the_table(self):
        self.assertEqual(version.category_count(main_script(4)), 4)

    def test_counts_the_real_main_script(self):
        text = (Path(version.ROOT) / "run.py").read_text(encoding="utf-8")

        # The running program must be readable by the same rule as the history.
        self.assertGreater(version.category_count(text), 10)

    def test_a_script_without_the_table_counts_zero(self):
        self.assertEqual(version.category_count("print('hello')\n"), 0)


@requires_git
class DerivationTests(unittest.TestCase):
    """How the three numbers come about."""

    def test_every_commit_counts_towards_the_build(self):
        with TemporaryDirectory() as base:
            repo = Repo(Path(base))
            for _index in range(5):
                repo.commit("notes.txt")

            self.assertEqual(derive(repo.root).build, "5")

    def test_a_new_category_raises_the_minor_and_resets_the_patch(self):
        with TemporaryDirectory() as base:
            repo = Repo(Path(base))
            repo.add_category()
            repo.commit("notes.txt")
            repo.commit("notes.txt")
            self.assertEqual(derive(repo.root).name, "0.1.2")

            repo.add_category()
            self.assertEqual(derive(repo.root).name, "0.2.0")

    def test_editing_the_script_without_a_new_category_only_raises_the_patch(self):
        with TemporaryDirectory() as base:
            repo = Repo(Path(base))
            repo.add_category()
            repo.touch_script()
            repo.touch_script()

            self.assertEqual(derive(repo.root).name, "0.1.2")

    def test_categories_under_the_former_script_name_count_too(self):
        with TemporaryDirectory() as base:
            repo = Repo(Path(base))
            repo.add_category(script="mint-cleaner.py")

            self.assertEqual(derive(repo.root).name, "0.1.0")

    def test_a_history_without_any_category_stays_on_the_patch(self):
        with TemporaryDirectory() as base:
            repo = Repo(Path(base))
            repo.commit("notes.txt")
            repo.commit("notes.txt")

            self.assertEqual(derive(repo.root).name, "0.0.2")

    def test_head_commit_and_date_come_from_the_same_history(self):
        with TemporaryDirectory() as base:
            repo = Repo(Path(base))
            repo.commit("notes.txt")
            found = derive(repo.root)

            self.assertRegex(found.commit, r"^[0-9a-f]{7,}$")
            self.assertRegex(found.date, r"^\d{4}-\d{2}-\d{2}$")
            self.assertTrue(found.label.startswith(f"{found.name} ({found.build}) · "))


@requires_git
class SourceTests(unittest.TestCase):
    """Which source answers, and in what order."""

    def setUp(self):
        version.forget()

    def tearDown(self):
        version.forget()

    def test_the_environment_variable_wins(self):
        with TemporaryDirectory() as base:
            repo = Repo(Path(base))
            repo.add_category()

            with mock.patch.dict(
                os.environ, {version.ENVIRONMENT_VARIABLE: "9.9.9"}, clear=False
            ):
                self.assertEqual(version.current(repo.root).name, "9.9.9")

    def test_reading_the_history_writes_the_version_file(self):
        with TemporaryDirectory() as base:
            repo = Repo(Path(base))
            repo.add_category()

            found = version.current(repo.root)
            stored = version.from_file(repo.root)

            self.assertEqual(stored.name, found.name)
            self.assertEqual(stored.marker, version.history_marker(repo.root))

    def test_an_installation_without_git_reads_the_file(self):
        with TemporaryDirectory() as base:
            root = Path(base)
            (root / "VERSION").write_text("0.4.2 17 abc1234 2026-09-29 55\n", encoding="utf-8")

            found = version.current(root)

            self.assertEqual(found.name, "0.4.2")
            self.assertEqual(found.label, "0.4.2 (17) · abc1234 · 29.09.2026")

    def test_without_history_and_without_file_the_version_is_unknown(self):
        with TemporaryDirectory() as base:
            found = version.current(Path(base))

            self.assertFalse(found.is_known)
            self.assertEqual(found.label, "unknown")

    def test_write_file_is_what_the_commit_hook_calls(self):
        with TemporaryDirectory() as base:
            repo = Repo(Path(base))
            repo.add_category()

            written = version.write_file(repo.root)

            self.assertIsNotNone(written)
            self.assertEqual((repo.root / "VERSION").read_text(encoding="utf-8").strip(),
                             written.line())


class HookTests(unittest.TestCase):
    """The hook that keeps the file current is present and runnable."""

    def test_post_commit_hook_is_executable_and_calls_version_py(self):
        hook = Path(version.ROOT) / ".githooks" / "post-commit"

        self.assertTrue(hook.is_file())
        self.assertTrue(os.access(hook, os.X_OK))
        self.assertIn("version.py", hook.read_text(encoding="utf-8"))

    def test_the_version_file_is_not_checked_in(self):
        ignore = (Path(version.ROOT) / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("/VERSION", ignore.splitlines())


if __name__ == "__main__":
    unittest.main()
