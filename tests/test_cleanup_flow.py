import importlib.util
import os
import queue
import tempfile
import unittest
from unittest import mock


SCRIPT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "run.py")
)

SPEC = importlib.util.spec_from_file_location("mint_cleaner_flow", SCRIPT_PATH)
MINT_CLEANER = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MINT_CLEANER)

GIB = 1024 * 1024 * 1024
MIB = 1024 * 1024


class DummyVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class SizeFormattingTests(unittest.TestCase):
    def test_human_size_switches_from_mb_to_gb(self):
        self.assertEqual(MINT_CLEANER.human_size(0), "0.0 MB")
        self.assertEqual(MINT_CLEANER.human_size(512 * MIB), "512.0 MB")
        self.assertEqual(MINT_CLEANER.human_size(2 * GIB), "2.00 GB")

    def test_human_delta_shows_sign(self):
        self.assertEqual(MINT_CLEANER.human_delta(0), "±0 MB")
        self.assertEqual(MINT_CLEANER.human_delta(3 * GIB), "+3.00 GB")
        self.assertEqual(MINT_CLEANER.human_delta(-40 * MIB), "-40.0 MB")

    def test_primary_free_bytes_and_disk_line(self):
        snapshot = {"System (/)": (10 * GIB, 100 * GIB), "Home (/home/x)": (5 * GIB, 50 * GIB)}

        self.assertEqual(MINT_CLEANER.primary_free_bytes(snapshot), 10 * GIB)
        line = MINT_CLEANER.format_disk_line(snapshot)
        self.assertIn("System (/): 10.00 GB free of 100.00 GB", line)
        self.assertIn("Home (/home/x): 5.00 GB free of 50.00 GB", line)

    def test_primary_free_bytes_of_empty_snapshot(self):
        self.assertEqual(MINT_CLEANER.primary_free_bytes({}), 0)
        self.assertEqual(MINT_CLEANER.format_disk_line({}), "")

    def test_disk_snapshot_reports_root_filesystem(self):
        snapshot = MINT_CLEANER.disk_snapshot()

        self.assertIn("System (/)", snapshot)
        free, total = snapshot["System (/)"]
        self.assertGreater(total, 0)
        self.assertGreaterEqual(total, free)


class ResultTableTests(unittest.TestCase):
    def test_cleanup_table_row_orders_before_freed_after(self):
        result = {
            "disk_before": {"System (/)": (118 * GIB, 468 * GIB)},
            "disk_after": {"System (/)": (124 * GIB, 468 * GIB)},
            "reclaimed_total": 6 * GIB,
        }

        row = MINT_CLEANER.cleanup_table_row(result)

        self.assertEqual(row, {"before": "118.00 GB", "freed": "6.00 GB", "after": "124.00 GB"})
        self.assertEqual(
            [key for _caption, key, _color in MINT_CLEANER.RESULT_TABLE_COLUMNS],
            ["before", "freed", "after"],
        )

    def test_cleanup_table_row_uses_the_real_disk_difference(self):
        # Deleted volume and disk difference disagree, for example because files
        # went to the Trash. The table must still add up.
        result = {
            "disk_before": {"System (/)": (int(3.91 * GIB), 100 * GIB)},
            "disk_after": {"System (/)": (int(9.76 * GIB), 100 * GIB)},
            "reclaimed_total": int(7.86 * GIB),
        }

        row = MINT_CLEANER.cleanup_table_row(result)

        self.assertEqual(row["before"], "3.91 GB")
        self.assertEqual(row["freed"], "5.85 GB")
        self.assertEqual(row["after"], "9.76 GB")

    def test_cleanup_table_row_reports_a_shrinking_disk_with_a_sign(self):
        result = {
            "disk_before": {"System (/)": (10 * GIB, 100 * GIB)},
            "disk_after": {"System (/)": (10 * GIB - 40 * MIB, 100 * GIB)},
            "reclaimed_total": 500 * MIB,
        }

        self.assertEqual(MINT_CLEANER.cleanup_table_row(result)["freed"], "-40.0 MB")

    def test_cleanup_table_row_tolerates_missing_values(self):
        row = MINT_CLEANER.cleanup_table_row({})

        self.assertEqual(row, {"before": "0.00 GB", "freed": "0.0 MB", "after": "0.00 GB"})

    def test_selected_potential_bytes_sums_only_selected_categories(self):
        sizes = {"tmp": 100 * MIB, "user_cache": 200 * MIB, "trash": 50 * MIB}
        selection = {"tmp": True, "user_cache": True, "trash": False}

        self.assertEqual(MINT_CLEANER.selected_potential_bytes(sizes, selection), 300 * MIB)

    def test_selected_potential_bytes_ignores_unknown_and_unmeasurable_keys(self):
        sizes = {"tmp": 100 * MIB, "not_a_category": 999 * MIB}
        selection = {"tmp": True, "not_a_category": True, "old_kernels": True}

        self.assertEqual(MINT_CLEANER.selected_potential_bytes(sizes, selection), 100 * MIB)

    def test_selected_potential_bytes_without_selection(self):
        self.assertEqual(MINT_CLEANER.selected_potential_bytes({"tmp": 5 * MIB}, {}), 0)

    def test_projection_table_row_adds_potential_to_free_space(self):
        row = MINT_CLEANER.projection_table_row(10 * GIB, 2 * GIB)

        self.assertEqual(row, {"before": "10.00 GB", "freed": "2.00 GB", "after": "12.00 GB"})
        self.assertEqual(sorted(row), sorted(MINT_CLEANER.cleanup_table_row({})))

    def test_projection_and_result_tables_share_the_same_columns(self):
        self.assertEqual(len(MINT_CLEANER.PREVIEW_TABLE_CAPTIONS),
                         len(MINT_CLEANER.RESULT_TABLE_COLUMNS))
        self.assertEqual(len(MINT_CLEANER.PREVIEW_TABLE_LOG_HEADERS),
                         len(MINT_CLEANER.RESULT_TABLE_COLUMNS))

    def test_projection_log_table_fits_the_activity_log_width(self):
        row = MINT_CLEANER.projection_table_row(1234 * GIB, 12 * GIB)
        lines = MINT_CLEANER.format_text_table(
            MINT_CLEANER.PREVIEW_TABLE_LOG_HEADERS,
            [[row[key] for _caption, key, _color in MINT_CLEANER.RESULT_TABLE_COLUMNS]],
        )

        self.assertLessEqual(max(len(line) for line in lines), MINT_CLEANER.LOG_WIDTH_CHARS)

    def test_trash_mode_delays_space_only_for_user_space_selections(self):
        self.assertTrue(
            MINT_CLEANER.trash_mode_delays_space({"user_cache": True}, "trash")
        )
        # Immediate deletion frees the space right away.
        self.assertFalse(
            MINT_CLEANER.trash_mode_delays_space({"user_cache": True}, "delete")
        )
        # Root paths are removed by the helper, they never go to the Trash.
        self.assertFalse(MINT_CLEANER.trash_mode_delays_space({"tmp": True}, "trash"))
        # Trash contents themselves are always deleted immediately.
        self.assertFalse(MINT_CLEANER.trash_mode_delays_space({"trash": True}, "trash"))
        self.assertFalse(MINT_CLEANER.trash_mode_delays_space({}, "trash"))

    def test_format_text_table_aligns_columns(self):
        lines = MINT_CLEANER.format_text_table(
            ["Free space before cleanup", "Space freed", "Free space available now"],
            [["118.00 GB", "6.00 GB", "124.00 GB"]],
        )

        self.assertEqual(len(lines), 5)
        self.assertEqual(len(set(len(line) for line in lines)), 1)
        self.assertTrue(lines[0].startswith("+-") and lines[0].endswith("-+"))
        self.assertIn("| Free space before cleanup | Space freed | Free space available now |", lines[1])
        self.assertIn("| 118.00 GB                 | 6.00 GB     | 124.00 GB                |", lines[3])

    def test_log_table_fits_the_activity_log_width(self):
        row = MINT_CLEANER.cleanup_table_row({
            "disk_before": {"System (/)": (1234 * GIB, 4000 * GIB)},
            "disk_after": {"System (/)": (1240 * GIB, 4000 * GIB)},
            "reclaimed_total": 6 * GIB,
        })
        lines = MINT_CLEANER.format_text_table(
            MINT_CLEANER.RESULT_TABLE_LOG_HEADERS,
            [[row[key] for _caption, key, _color in MINT_CLEANER.RESULT_TABLE_COLUMNS]],
        )

        self.assertEqual(len(MINT_CLEANER.RESULT_TABLE_LOG_HEADERS),
                         len(MINT_CLEANER.RESULT_TABLE_COLUMNS))
        self.assertLessEqual(max(len(line) for line in lines), MINT_CLEANER.LOG_WIDTH_CHARS)

    def test_format_text_table_widens_for_long_values(self):
        lines = MINT_CLEANER.format_text_table(["a"], [["value-longer-than-header"]])

        self.assertEqual(lines[1], "| a                        |")
        self.assertEqual(lines[3], "| value-longer-than-header |")


class PlanHelperTests(unittest.TestCase):
    def test_plan_is_empty_detects_actions(self):
        empty = {"user_py_delete": [], "user_cmds": [], "root_rm_patterns": [], "root_cmds": []}

        self.assertTrue(MINT_CLEANER.plan_is_empty(empty))
        self.assertFalse(MINT_CLEANER.plan_is_empty({**empty, "root_cmds": ["apt clean"]}))

    def test_split_user_targets_keeps_trash_contents_on_delete(self):
        with tempfile.TemporaryDirectory() as tmp_home:
            with mock.patch.dict(os.environ, {"HOME": tmp_home}, clear=False):
                to_trash, to_delete = MINT_CLEANER.split_user_targets(
                    ["~/.thumbnails/*", "~/.local/share/Trash/*"], "trash"
                )

            self.assertEqual(to_trash, ["~/.thumbnails/*"])
            self.assertEqual(to_delete, ["~/.local/share/Trash/*"])

    def test_split_user_targets_deletes_everything_in_delete_mode(self):
        with tempfile.TemporaryDirectory() as tmp_home:
            with mock.patch.dict(os.environ, {"HOME": tmp_home}, clear=False):
                to_trash, to_delete = MINT_CLEANER.split_user_targets(
                    ["~/.thumbnails/*", "~/.cache/*"], "delete"
                )

            self.assertEqual(to_trash, [])
            self.assertEqual(to_delete, ["~/.thumbnails/*", "~/.cache/*"])

    def test_selection_from_vars_reads_all_known_names(self):
        source = type("Source", (), {})()
        for name in MINT_CLEANER.SELECTION_NAMES:
            setattr(source, f"var_{name}", DummyVar(name == "thumbnails"))

        selection = MINT_CLEANER.selection_from_vars(source)

        self.assertEqual(set(selection), set(MINT_CLEANER.SELECTION_NAMES))
        self.assertTrue(selection["thumbnails"])
        self.assertFalse(selection["user_cache"])

    def test_selection_from_vars_defaults_missing_vars_to_false(self):
        selection = MINT_CLEANER.selection_from_vars(type("Empty", (), {})())

        self.assertFalse(any(selection.values()))

    def test_build_cleanup_plan_without_rediscovery_uses_known_paths(self):
        patterns = {"python_artifacts": ["/home/x/proj/__pycache__"], "local_history": []}
        selection = {"python_artifacts": True}

        with mock.patch.object(MINT_CLEANER, "find_python_artifact_dirs") as finder:
            plan, discovered, notes = MINT_CLEANER.build_cleanup_plan(
                selection, patterns, rediscover=False
            )

        finder.assert_not_called()
        self.assertEqual(plan["user_py_delete"], ["/home/x/proj/__pycache__"])
        self.assertEqual(discovered["python_artifacts"], ["/home/x/proj/__pycache__"])
        self.assertEqual(notes, [])

    def test_build_cleanup_plan_notes_missing_flatpak(self):
        with mock.patch.object(MINT_CLEANER, "exists_in_path", return_value=False):
            plan, _discovered, notes = MINT_CLEANER.build_cleanup_plan(
                {"flatpak_user": True}, {}
            )

        self.assertEqual(plan["user_cmds"], [])
        self.assertEqual(len(notes), 1)
        self.assertIn("flatpak not found", notes[0])

    def test_build_cleanup_plan_quotes_journal_retention(self):
        plan, _discovered, _notes = MINT_CLEANER.build_cleanup_plan(
            {"journal": True}, {}, journal_retention="7d"
        )

        self.assertEqual(plan["root_cmds"], ["journalctl --vacuum-time=7d"])

    def test_measurable_keys_all_have_labels_and_selection_names(self):
        for key in MINT_CLEANER.MEASURABLE_KEYS:
            self.assertIn(key, MINT_CLEANER.CATEGORY_LABELS)
            self.assertIn(key, MINT_CLEANER.SELECTION_NAMES)
        self.assertTrue(MINT_CLEANER.ROOT_MEASURABLE_KEYS.issubset(set(MINT_CLEANER.MEASURABLE_KEYS)))


class JobReporterTests(unittest.TestCase):
    def test_reporter_publishes_ordered_messages(self):
        updates: queue.Queue = queue.Queue()
        reporter = MINT_CLEANER.JobReporter(updates)

        reporter.add_steps(["a", "b"])
        first = reporter.begin("a", "(note)")
        reporter.end("12.0 MB")
        second = reporter.begin("b")
        reporter.end("failed", failed=True)
        reporter.log("hello")
        reporter.subtitle("working")

        messages = []
        while not updates.empty():
            messages.append(updates.get_nowait())

        self.assertEqual((first, second), (0, 1))
        self.assertEqual(messages[0], ("add_steps", ["a", "b"], None))
        self.assertEqual(messages[1], ("begin", 0, ("a", "(note)")))
        self.assertEqual(messages[2], ("end", 0, ("12.0 MB", False)))
        self.assertEqual(messages[3], ("begin", 1, ("b", "")))
        self.assertEqual(messages[4], ("end", 1, ("failed", True)))
        self.assertEqual(messages[5], ("log", "hello", None))
        self.assertEqual(messages[6], ("subtitle", "working", None))
        self.assertEqual(reporter.index, 1)


class LiveScanTests(unittest.TestCase):
    """The analysis publishes results while it is still running."""

    def test_reporter_live_copies_the_payload(self):
        updates: queue.Queue = queue.Queue()
        reporter = MINT_CLEANER.JobReporter(updates)
        payload = {"sizes": {"tmp": 1}}

        reporter.live(payload)
        payload["sizes"] = {"tmp": 999}

        kind, published, extra = updates.get_nowait()
        self.assertEqual(kind, "live")
        self.assertEqual(published, {"sizes": {"tmp": 1}})
        self.assertIsNone(extra)

    def test_scan_worker_publishes_disk_first_then_every_category(self):
        sizes = {key: 7 * MIB for key in MINT_CLEANER.MEASURABLE_KEYS}

        class FakeScanApp:
            patterns = {key: ["/nonexistent"] for key in MINT_CLEANER.MEASURABLE_KEYS}
            _scan_worker = MINT_CLEANER.MintCleanerApp._scan_worker

            def _measure_key(self, key, patterns):
                return sizes[key]

        updates: queue.Queue = queue.Queue()
        with mock.patch.dict(MINT_CLEANER.DISCOVERY_FINDERS, {}, clear=True):
            result = FakeScanApp()._scan_worker(MINT_CLEANER.JobReporter(updates))

        messages = []
        while not updates.empty():
            messages.append(updates.get_nowait())

        live = [payload for kind, payload, _extra in messages if kind == "live"]
        # Free space is known before the first category is measured.
        self.assertIn("disk", live[0])
        self.assertEqual(messages[0][0], "begin")
        self.assertEqual(messages[0][2][0], "Read disk usage")
        # One live update per measured category, so the total can count up.
        reported = [payload["sizes"] for payload in live if "sizes" in payload]
        self.assertEqual(len(reported), len(MINT_CLEANER.MEASURABLE_KEYS))
        self.assertEqual(
            [next(iter(entry)) for entry in reported],
            list(MINT_CLEANER.MEASURABLE_KEYS),
        )
        self.assertEqual(result["sizes"], sizes)

    def test_scan_worker_reports_each_size_before_finishing_its_step(self):
        class FakeScanApp:
            patterns = {}
            _scan_worker = MINT_CLEANER.MintCleanerApp._scan_worker

            def _measure_key(self, key, patterns):
                return 5 * MIB

        updates: queue.Queue = queue.Queue()
        with mock.patch.dict(MINT_CLEANER.DISCOVERY_FINDERS, {}, clear=True):
            FakeScanApp()._scan_worker(MINT_CLEANER.JobReporter(updates))

        order = []
        while not updates.empty():
            kind, payload, _extra = updates.get_nowait()
            if kind in ("live", "end") and not (kind == "live" and "disk" in payload):
                order.append(kind)

        # Every step publishes its value first and is only then marked done.
        self.assertEqual(order[:4], ["end", "live", "end", "live"])


class _FakeApp:
    """Minimal stand-in exposing the app methods the cleanup worker needs."""

    _measure_key = MINT_CLEANER.MintCleanerApp._measure_key
    _cleanup_worker = MINT_CLEANER.MintCleanerApp._cleanup_worker


class CleanupWorkerTests(unittest.TestCase):
    def _run_worker(self, tmp_home, mode):
        """Run the cleanup worker for ~/.thumbnails/* in the given deletion mode."""
        thumbnails = os.path.join(tmp_home, ".thumbnails")
        os.makedirs(thumbnails)
        for name in ("a.png", "b.png"):
            with open(os.path.join(thumbnails, name), "wb") as handle:
                handle.write(b"x" * 4096)

        selection = {name: False for name in MINT_CLEANER.SELECTION_NAMES}
        selection["thumbnails"] = True
        context = {
            "selection": selection,
            "selected_keys": ["thumbnails"],
            "patterns": {"thumbnails": ["~/.thumbnails/*"]},
            "retention": "3d",
            "mode": mode,
        }
        initial_steps = ["Prepare cleanup plan", "Read disk usage before cleanup",
                         "Measure before: " + MINT_CLEANER.CATEGORY_LABELS["thumbnails"]]

        updates: queue.Queue = queue.Queue()
        reporter = MINT_CLEANER.JobReporter(updates)
        with mock.patch.dict(os.environ, {"HOME": tmp_home}, clear=False):
            with mock.patch.object(MINT_CLEANER, "exists_in_path", return_value=False):
                result = _FakeApp()._cleanup_worker(reporter, context)

        messages = []
        while not updates.empty():
            messages.append(updates.get_nowait())
        return result, messages, initial_steps

    def test_cleanup_worker_deletes_and_reports_reclaimed_bytes(self):
        with tempfile.TemporaryDirectory() as tmp_home:
            result, messages, _steps = self._run_worker(tmp_home, "delete")

            self.assertEqual(result["selected_keys"], ["thumbnails"])
            self.assertEqual(result["sizes_before"]["thumbnails"], 2 * 4096)
            self.assertEqual(result["sizes_after"]["thumbnails"], 0)
            self.assertEqual(result["reclaimed_total"], 2 * 4096)
            self.assertFalse(result["trash_used"])
            self.assertIn("System (/)", result["disk_before"])
            self.assertIn("System (/)", result["disk_after"])
            self.assertEqual(os.listdir(os.path.join(tmp_home, ".thumbnails")), [])
            self.assertTrue(any(kind == "log" and "Cleanup finished" in payload
                                for kind, payload, _extra in messages))

    def test_cleanup_worker_moves_to_trash_in_trash_mode(self):
        with tempfile.TemporaryDirectory() as tmp_home:
            result, _messages, _steps = self._run_worker(tmp_home, "trash")

            self.assertTrue(result["trash_used"])
            trashed = os.listdir(os.path.join(tmp_home, ".local/share/Trash/files"))
            self.assertEqual(sorted(trashed), ["a.png", "b.png"])
            self.assertEqual(result["reclaimed_total"], 2 * 4096)

    def test_cleanup_worker_empties_the_trash_before_filling_it(self):
        """Regression: emptying the Trash must not wipe the files just moved in."""
        with tempfile.TemporaryDirectory() as tmp_home:
            thumbnails = os.path.join(tmp_home, ".thumbnails")
            trash_files = os.path.join(tmp_home, ".local/share/Trash/files")
            os.makedirs(thumbnails)
            os.makedirs(trash_files)
            with open(os.path.join(thumbnails, "pic.png"), "wb") as handle:
                handle.write(b"x" * 4096)
            with open(os.path.join(trash_files, "old.txt"), "wb") as handle:
                handle.write(b"y" * 2048)

            selection = {name: False for name in MINT_CLEANER.SELECTION_NAMES}
            selection["thumbnails"] = True
            selection["trash"] = True
            context = {
                "selection": selection,
                "selected_keys": ["thumbnails", "trash"],
                "patterns": {
                    "thumbnails": ["~/.thumbnails/*"],
                    "trash": ["~/.local/share/Trash/*"],
                },
                "retention": "3d",
                "mode": "trash",
            }

            updates: queue.Queue = queue.Queue()
            with mock.patch.dict(os.environ, {"HOME": tmp_home}, clear=False):
                with mock.patch.object(MINT_CLEANER, "exists_in_path", return_value=False):
                    result = _FakeApp()._cleanup_worker(MINT_CLEANER.JobReporter(updates), context)

            self.assertEqual(os.listdir(thumbnails), [])
            # The thumbnail survived in the Trash, the old content is gone.
            self.assertEqual(os.listdir(trash_files), ["pic.png"])
            self.assertTrue(result["trash_used"])

    def test_cleanup_worker_step_labels_match_begin_order(self):
        with tempfile.TemporaryDirectory() as tmp_home:
            _result, messages, initial_steps = self._run_worker(tmp_home, "delete")

            labels = list(initial_steps)
            begins = []
            for kind, payload, extra in messages:
                if kind == "add_steps":
                    labels.extend(payload)
                elif kind == "begin":
                    begins.append((payload, extra[0]))

            # Every started step exists in the checklist at exactly its index.
            self.assertEqual(len(begins), len(labels))
            for index, label in begins:
                self.assertEqual(labels[index], label)
            self.assertEqual([index for index, _ in begins], list(range(len(labels))))

            ended = [payload for kind, payload, _extra in messages if kind == "end"]
            self.assertEqual(ended, list(range(len(labels))))


if __name__ == "__main__":
    unittest.main()
