"""Focused checks for the saved-board routing progress recorder."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from review import routing_progress as progress


def point(
    time: str, count: int, digest: str, signature: str = ""
) -> progress.ProgressPoint:
    return progress.ProgressPoint(
        time, count, 0, 3, 0, digest * 64, signature * 64, "Test change", "Verified"
    )


class RoutingProgressTests(unittest.TestCase):
    def test_scope_change_marks_the_last_saved_design_change_not_preview_history(self):
        points = [
            point("2026-10-29T09:00:00-07:00", 189, "a"),
            progress.replace(
                point("2026-10-29T10:00:00-07:00", 177, "b"),
                change="IMU power and data moved off carrier; scope change",
            ),
            progress.replace(
                point("2026-10-29T10:05:00-07:00", 177, "c"),
                change="Preview scope change",
            ),
        ]
        self.assertEqual(
            progress.ProgressPace.latest_scope_change(points, 2),
            (1, "IMU power and data moved off carrier"),
        )
        self.assertIsNone(progress.ProgressPace.latest_scope_change(points[:1], None))

    def test_design_rebase_breaks_plot_lines_and_marks_scope(self):
        points = [
            point("2026-11-03T09:00:00-08:00", 186, "a"),
            progress.replace(
                point("2026-11-04T09:00:00-08:00", 140, "b"),
                change="Imported v4.2 baseline; scope change",
            ),
            point("2026-11-04T09:01:00-08:00", 139, "c"),
        ]
        helper = getattr(progress.ProgressChart, "series_runs", None)
        self.assertTrue(callable(helper), "scope-aware plot segmentation is missing")
        self.assertEqual(
            progress.ProgressPace.latest_scope_change(points, 2),
            (1, "Imported v4.2 baseline"),
        )
        self.assertEqual(
            helper([(0, 186), (1, 140), (2, 139)], {1, 2}),
            [[(0, 186)], [(1, 140)], [(2, 139)]],
        )

    def test_preview_pace_uses_elapsed_wall_time_between_measured_changes(self):
        points = [
            point("2026-10-29T09:00:00-07:00", 285, "a"),
            point("2026-10-29T10:00:00-07:00", 262, "b"),
            point("2026-10-29T10:02:00-07:00", 250, "c"),
        ]
        self.assertEqual(
            progress.ProgressPace.latest_preview_pace(points, 1), (12, 120.0, 6.0)
        )
        self.assertIsNone(progress.ProgressPace.latest_preview_pace(points[:2], 1))

    def test_recorded_run_pace_excludes_idle_time_and_requires_a_valid_counter(self):
        points = [
            point("2026-10-29T09:00:00-07:00", 285, "a"),
            progress.replace(
                point("2026-10-29T10:00:00-07:00", 262, "b"), routing_run_seconds=100
            ),
            progress.replace(
                point("2026-10-29T10:32:00-07:00", 250, "c"), routing_run_seconds=130
            ),
        ]
        self.assertEqual(
            progress.ProgressPace.latest_recorded_run_pace(points, 1), (12, 30, 24.0)
        )
        points[-1] = progress.replace(points[-1], routing_run_seconds=90)
        self.assertIsNone(progress.ProgressPace.latest_recorded_run_pace(points, 1))
        points[-1] = progress.replace(points[-1], routing_run_seconds=None)
        self.assertIsNone(progress.ProgressPace.latest_recorded_run_pace(points, 1))

    def test_stalled_preview_reports_zero_rate_instead_of_hiding_speed(self):
        points = [
            point("2026-10-29T09:00:00-07:00", 215, "a"),
            progress.replace(
                point("2026-10-29T10:00:00-07:00", 215, "b"), routing_run_seconds=0
            ),
            progress.replace(
                point("2026-10-29T10:02:00-07:00", 215, "b"), routing_run_seconds=60
            ),
        ]
        self.assertEqual(
            progress.ProgressPace.latest_preview_pace(points, 1), (0, 120.0, 0.0)
        )
        self.assertEqual(
            progress.ProgressPace.latest_recorded_run_pace(points, 1), (0, 60.0, 0.0)
        )

    def test_preview_is_separate_and_disappears_when_sources_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review = root / "deliverables" / "review"
            review.mkdir(parents=True)
            board = root / "v4.1.kicad_pcb"
            board.write_bytes(b"saved board")
            digest = hashlib.sha256(board.read_bytes()).hexdigest()
            saved = progress.replace(
                point("2026-10-29T09:00:00-07:00", 285, "a"), pcb_sha256=digest
            )
            preview = point("2026-10-29T10:00:00-07:00", 281, "b")
            history = progress.encode_history([saved])
            (review / progress.CSV_NAME).write_bytes(history)
            progress.publish_preview(
                root, {"v4.1.kicad_pcb": digest}, "test", [asdict(preview)]
            )
            points, boundary = progress.PreviewSeries.review_points([saved], review)
            self.assertEqual((points[-1].open_connections, boundary), (281, 1))
            self.assertEqual((review / progress.CSV_NAME).read_bytes(), history)
            board.write_bytes(b"edited board")
            self.assertEqual(
                progress.PreviewSeries.review_points([saved], review), ([saved], None)
            )
            with self.assertRaisesRegex(RuntimeError, "changed"):
                progress.publish_preview(
                    root, {"v4.1.kicad_pcb": digest}, "test", [asdict(preview)]
                )

    def test_record_publishes_source_bound_run_metrics_and_rejects_stale_ones(self):
        self.assertTrue(hasattr(progress, "RunMetrics"))
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            review = project / "deliverables" / "review"
            review.mkdir(parents=True)
            current = point("2026-10-28T13:12:00-07:00", 381, "a", "b")
            metrics = progress.RunMetrics(
                "a" * 64, "b" * 64, 1.5, 120.0, "run evidence"
            )
            with (
                mock.patch.object(
                    progress, "check_saved_board", return_value=(current, {})
                ),
                mock.patch.object(
                    progress, "_fingerprint", return_value=([], {}, "ignored")
                ),
            ):
                result = progress.ProgressRecorder.record(
                    project, review, Path("kicad-python"), "Check", run_metrics=metrics
                )
                self.assertEqual(result["status"], "recorded")
                last = progress.read_history(review / progress.CSV_NAME)[-1]
                self.assertEqual(
                    (last.symmetry_score, last.routing_run_seconds), (1.5, 120.0)
                )
                before = (review / progress.CSV_NAME).read_bytes()
                stale = progress.RunMetrics(
                    "c" * 64, "b" * 64, 1.5, 120.0, "run evidence"
                )
                with self.assertRaisesRegex(ValueError, "source"):
                    progress.ProgressRecorder.record(
                        project,
                        review,
                        Path("kicad-python"),
                        "Check",
                        run_metrics=stale,
                    )
                self.assertEqual((review / progress.CSV_NAME).read_bytes(), before)

    def test_report_counts_and_rejects_incomplete_checks(self):
        report = {
            "unconnected_items": [{}, {}],
            "violations": [{"severity": "warning"}, {"severity": "error"}],
            "schematic_parity": [],
            "ignored_checks": [],
            "included_severities": ["error", "warning"],
        }
        self.assertEqual(
            progress.SavedBoardCheck.summarize_report(report), (2, 1, 1, 0)
        )
        with self.assertRaisesRegex(ValueError, "ignored"):
            progress.SavedBoardCheck.summarize_report(
                {**report, "ignored_checks": ["clearance"]}
            )
        with self.assertRaisesRegex(ValueError, "both errors and warnings"):
            progress.SavedBoardCheck.summarize_report(
                {**report, "included_severities": ["warning"]}
            )
        with self.assertRaisesRegex(ValueError, "unconnected"):
            progress.SavedBoardCheck.summarize_report(
                {**report, "unconnected_items": None}
            )

    def test_history_round_trip_and_timestamp_guard(self):
        points = [
            point("2026-10-27T19:00:00-07:00", 464, "a"),
            point("2026-10-28T13:12:00-07:00", 381, "b", "c"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.csv"
            path.write_bytes(progress.encode_history(points))
            self.assertEqual(progress.read_history(path), points)
            with path.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            rows[1]["checked_at"] = "2026-10-28T13:12:00"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=progress.FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, "time zone"):
                progress.read_history(path)

    def test_plot_is_a_readable_png(self):
        points = [
            point("2026-10-27T19:00:00-07:00", 464, "a"),
            point("2026-10-28T13:12:00-07:00", 381, "b"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.png"
            progress.ProgressChart.draw(points, path)
            with Image.open(path) as image:
                self.assertEqual(image.size, (1600, 820))
                image.verify()

    def test_repeated_unchanged_source_does_not_rewrite_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            review = project / "deliverables" / "review"
            review.mkdir(parents=True)
            current = point("2026-10-28T13:12:00-07:00", 381, "a", "b")
            with (
                mock.patch.object(
                    progress, "check_saved_board", return_value=(current, {})
                ),
                mock.patch.object(
                    progress, "_fingerprint", return_value=([], {}, "ignored")
                ),
            ):
                first = progress.ProgressRecorder.record(
                    project, review, Path("kicad-python"), "Check"
                )
                table = review / progress.CSV_NAME
                plot = review / progress.PNG_NAME
                before = (
                    table.read_bytes(),
                    plot.read_bytes(),
                    table.stat().st_mtime_ns,
                    plot.stat().st_mtime_ns,
                )
                second = progress.ProgressRecorder.record(
                    project, review, Path("kicad-python"), "Check"
                )
                after = (
                    table.read_bytes(),
                    plot.read_bytes(),
                    table.stat().st_mtime_ns,
                    plot.stat().st_mtime_ns,
                )
            self.assertEqual(first["status"], "recorded")
            self.assertEqual(second["status"], "unchanged")
            self.assertEqual(before, after)

    def test_failed_snapshot_check_does_not_write_history(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            review = project / "deliverables" / "review"
            review.mkdir(parents=True)
            table = review / progress.CSV_NAME
            table.write_bytes(b"existing data")
            with (
                mock.patch.object(progress, "read_history", return_value=[]),
                mock.patch.object(
                    progress,
                    "check_saved_board",
                    side_effect=RuntimeError("Saved design inputs changed"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "changed"):
                    progress.ProgressRecorder.record(
                        project, review, Path("kicad-python"), "Check"
                    )
            self.assertEqual(table.read_bytes(), b"existing data")
            self.assertFalse((review / progress.PNG_NAME).exists())

    def test_preview_runtime_preserves_counts_and_export_failure_stops_with_checkpoint(
        self,
    ):
        from dataclasses import asdict
        from pcb.net_routing.runner import (
            _progress_elapsed,
            _publish_progress,
            RunState,
            _save,
        )

        sample = asdict(point("2026-10-28T13:12:00-07:00", 281, "a", "b"))
        sample["routing_run_seconds"] = 10
        state = {
            "run_id": "a" * 32,
            "elapsed_seconds": 70,
            "progress_samples": [sample],
            "checkpoint": {"retained": True},
        }
        _progress_elapsed(state, sample["pcb_sha256"])
        self.assertEqual(state["progress_samples"][-1]["open_connections"], 281)
        self.assertEqual(state["progress_samples"][-1]["routing_run_seconds"], 70)
        with self.assertRaisesRegex(RuntimeError, "retained preview"):
            _progress_elapsed(state, "changed")
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "run.json"
            _save(state_path, state)
            with mock.patch.object(
                progress, "publish_preview", side_effect=OSError("export failed")
            ):
                with self.assertRaisesRegex(OSError, "export failed"):
                    with RunState.record_failures(state_path, state):
                        _publish_progress(Path("."), {}, state)
            saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["checkpoint"], {"retained": True})
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["progress_export_error"], "export failed")


if __name__ == "__main__":
    unittest.main()
