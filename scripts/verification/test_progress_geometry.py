"""Exercise geometry measurements and source-bound progress history."""

from __future__ import annotations

import csv
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pcbnew
from PIL import Image

from review import routing_progress as progress
from review.geometry_metrics import GeometryMetrics, measure_board


def progress_point(digest: str) -> progress.ProgressPoint:
    return progress.ProgressPoint(
        "2026-10-28T12:00:00-07:00", 3, 0, 1, 0, digest, "", "test", "DRC"
    )


class GeometryProgressTests(unittest.TestCase):
    def test_symmetry_and_runtime_are_plotted_when_measured(self):
        self.assertIn("symmetry_score", progress.FIELDS)
        self.assertIn("routing_run_seconds", progress.FIELDS)
        point = replace(progress_point("a" * 64), symmetry_score=1.5, routing_run_seconds=123.0, optimization_evidence="verified run")
        fields = [item[1] for item in progress.chart_series([point])]
        self.assertIn("symmetry_score", fields)
        self.assertIn("routing_run_seconds", fields)
        self.assertNotIn("schematic_parity", fields)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plot.png"
            progress.draw_chart([point], path)
            with Image.open(path) as image:
                image.verify()

    def test_previous_history_keeps_optimization_metrics_unknown(self):
        self.assertTrue(hasattr(progress, "PREVIOUS_FIELDS"))
        point = progress.attach_geometry(progress_point("a" * 64), GeometryMetrics(1, 2, 3), "source")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=progress.PREVIOUS_FIELDS)
                writer.writeheader()
                writer.writerow({field: point.as_row()[field] for field in progress.PREVIOUS_FIELDS})
            restored = progress.read_history(path)[0]
            self.assertIsNone(restored.symmetry_score)
            self.assertIsNone(restored.routing_run_seconds)

    def test_run_metrics_require_the_matching_saved_board_and_source(self):
        self.assertTrue(hasattr(progress, "attach_run_metrics"))
        point = replace(progress_point("a" * 64), source_signature="b" * 64)
        with self.assertRaisesRegex(ValueError, "source"):
            progress.attach_run_metrics(point, "c" * 64, "b" * 64, 0.5, 10, "run")
        with self.assertRaisesRegex(ValueError, "evidence"):
            progress.attach_run_metrics(point, "a" * 64, "b" * 64, 0.5, 10, "")
        enriched = progress.attach_run_metrics(point, "a" * 64, "b" * 64, 0.5, 10, "run")
        self.assertEqual((enriched.symmetry_score, enriched.routing_run_seconds), (0.5, 10))

    def test_parity_is_recorded_but_not_plotted(self):
        point = progress_point("a" * 64)
        self.assertEqual(point.schematic_parity, 0)
        self.assertNotIn(
            "schematic_parity", [item[1] for item in progress.chart_series([point])]
        )

    def test_real_kicad_tracks_and_via(self):
        board = pcbnew.BOARD()
        for a, z in (((0, 0), (1, 0)), ((1, 0), (2, 1))):
            trace = pcbnew.PCB_TRACK(board)
            trace.SetStart(pcbnew.VECTOR2I(*(pcbnew.FromMM(n) for n in a)))
            trace.SetEnd(pcbnew.VECTOR2I(*(pcbnew.FromMM(n) for n in z)))
            trace.SetLayer(pcbnew.B_Cu)
            trace.SetWidth(pcbnew.FromMM(0.25))
            board.Add(trace)
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(9), pcbnew.FromMM(9)))
        board.Add(via)
        measured = measure_board(board)
        self.assertAlmostEqual(measured.track_length_mm, 2.414214, places=6)
        self.assertEqual(measured.via_count, 1)
        self.assertEqual(measured.degree_two_joints, 1)

    def test_legacy_history_preserves_unknown_geometry(self):
        point = progress_point("a" * 64)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=progress.LEGACY_FIELDS)
                writer.writeheader()
                writer.writerow(
                    {field: point.as_row()[field] for field in progress.LEGACY_FIELDS}
                )
            restored = progress.read_history(path)
            self.assertEqual(restored, [point])
            path.write_bytes(progress.encode_history(restored))
            self.assertEqual(progress.read_history(path), [point])

    def test_backfill_uses_only_exact_board_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = root / "project"
            review = project / "deliverables" / "review"
            review.mkdir(parents=True)
            snapshots = root / "snapshots"
            matched = snapshots / "one" / "v4.1.kicad_pcb"
            matched.parent.mkdir(parents=True)
            matched.write_bytes(b"exact board")
            unknown = snapshots / "two" / "v4.1.kicad_pcb"
            unknown.parent.mkdir()
            unknown.write_bytes(b"different board")
            digest = hashlib.sha256(matched.read_bytes()).hexdigest()
            history = [progress_point(digest), progress_point("b" * 64)]
            (review / progress.CSV_NAME).write_bytes(progress.encode_history(history))
            metrics = GeometryMetrics(22.5, 3, 4)
            with mock.patch.object(
                progress.pcbnew, "LoadBoard", return_value=object()
            ) as load:
                with mock.patch.object(progress, "measure_board", return_value=metrics):
                    result = progress.backfill(project, review, snapshots)
            self.assertEqual(result["rows_updated"], 1)
            self.assertEqual(result["unmatched_hashes"], 1)
            load.assert_called_once_with(str(matched))
            enriched = progress.read_history(review / progress.CSV_NAME)
            self.assertEqual(enriched[0].track_length_mm, 22.5)
            self.assertEqual(enriched[0].via_count, 3)
            self.assertEqual(enriched[0].degree_two_joints, 4)
            self.assertIn(str(matched), enriched[0].geometry_evidence)
            self.assertIsNone(enriched[1].track_length_mm)
            with Image.open(review / progress.PNG_NAME) as image:
                image.verify()

    def test_disagreement_does_not_replace_history(self):
        old = progress.attach_geometry(
            progress_point("a" * 64), GeometryMetrics(1.0, 2, 3), "saved"
        )
        with self.assertRaisesRegex(ValueError, "disagrees"):
            progress.attach_geometry(old, GeometryMetrics(2.0, 2, 3), "new")


if __name__ == "__main__":
    unittest.main()
