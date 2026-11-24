"""A neutral preview is disposable until a better board passes every check."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def candidate(source: bytes, board: bytes, opens: int, **validation):
    return {
        "status": "validated_preview",
        "source_sha256": digest(source),
        "candidate_sha256": digest(board),
        "changes": [{"bytes": board}],
        "new_style_issues": (),
        "candidate_reference": {"high_speed_nets": ["USB"]},
        "candidate_geometry": {},
        "nets": ["A"],
        "attempts": [],
        "validation": {
            "status": "no_new_drc_violations",
            "schematic_parity_checked": True,
            "candidate_unconnected": opens,
            "reference_comparison": {"status": "unverified"},
            **validation,
        },
    }


class FakeSnapshot:
    def __init__(self, source):
        self.source = Path(source)

    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.board_path = self.directory / "v4.1.kicad_pcb"
        self.original = self.source.read_bytes()
        self.board_path.write_bytes(self.original)
        (self.directory / "v4.1.kicad_sch").write_bytes(b"protected schematic")
        return self

    def check(self):
        if self.source.read_bytes() != self.original:
            raise RuntimeError("Routing sources changed")

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.check()
        finally:
            self.temp.cleanup()


class AutoLookaheadTests(unittest.TestCase):
    def test_only_checked_neutral_previews_can_be_stepping_stones(self):
        from pcb.net_routing.runner import PreviewJudge

        good = candidate(b"best", b"neutral", 10)
        self.assertTrue(PreviewJudge.neutral_eligible(good, 10))
        for bad in (
            candidate(b"best", b"x", 9),
            candidate(b"best", b"x", 11),
            candidate(b"best", b"x", 10, schematic_parity_checked=False),
            candidate(b"best", b"x", 10, reference_comparison={"status": "degraded"}),
            {**good, "new_style_issues": ("staircases",)},
            {**good, "status": "rejected"},
        ):
            self.assertFalse(PreviewJudge.neutral_eligible(bad, 10))

    def test_neutral_branch_promotes_only_a_fully_rechecked_reduction(self):
        result, original, seen = self._exercise("improved")
        self.assertEqual(original, b"best")
        self.assertEqual(result["board"], b"improved")
        self.assertEqual(result["validation"]["candidate_unconnected"], 9)
        self.assertIn("native final verification", seen)
        self.assertIn("cumulative reference verification", seen)

    def test_neutral_branch_is_discarded_when_no_followup_improves(self):
        result, original, seen = self._exercise("neutral")
        self.assertIsNone(result)
        self.assertEqual(original, b"best")
        self.assertNotIn("native final verification", seen)

    def test_degraded_cumulative_reference_cannot_be_promoted(self):
        result, original, seen = self._exercise("degraded")
        self.assertIsNone(result)
        self.assertEqual(original, b"best")
        self.assertIn("cumulative reference verification", seen)

    def test_exception_never_changes_the_original_checkpoint(self):
        with self.assertRaisesRegex(RuntimeError, "native test failure"):
            self._exercise("exception")

    def test_interruption_never_changes_the_original_checkpoint(self):
        with self.assertRaises(KeyboardInterrupt):
            self._exercise("interrupt")

    def test_user_edit_invalidates_the_branch_and_is_not_overwritten(self):
        with self.assertRaisesRegex(RuntimeError, "sources changed"):
            self._exercise("source_change")

    def test_alternative_followups_start_from_the_same_neutral_board(self):
        result, original, seen = self._exercise("alternatives")
        self.assertEqual(original, b"best")
        self.assertEqual(result["board"], b"improved")
        self.assertEqual(seen.count("follow-up from neutral"), 2)

    def test_lookahead_does_not_spend_a_batch_on_an_unrelated_net(self):
        result, original, seen = self._exercise("unrelated")
        self.assertIsNone(result)
        self.assertEqual(original, b"best")
        self.assertEqual(seen.count("follow-up from neutral"), 1)

    def _exercise(self, mode):
        from pcb.net_routing import runner

        seen = []
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "v4.1.kicad_pcb"
            source.write_bytes(b"best")
            (source.parent / "v4.1.kicad_sch").write_bytes(b"protected schematic")
            outer = SimpleNamespace(board_path=source, directory=source.parent)
            neutral = candidate(b"best", b"neutral", 10)
            if mode == "alternatives":
                neutral["nets"] = ["A", "B"]
            request = {"nets": [{"name": "A"}], "search": {"seconds": 5}}

            def replan(*args, **kwargs):
                other = {"nets": [{"name": "B"}], "search": {"seconds": 5}}
                return {
                    "rounds": [
                        [request, other]
                        if mode in {"alternatives", "unrelated"}
                        else [request]
                    ]
                }

            def planner(directory):
                def plan(kind, changes):
                    image = changes[0]["bytes"]
                    return SimpleNamespace(after=image, result_sha256=digest(image))

                return SimpleNamespace(plan=plan)

            def native(function, *args, **kwargs):
                if function is runner.NativePreview.preview_round:
                    self.assertEqual(
                        (args[0] / "v4.1.kicad_pcb").read_bytes(), b"neutral"
                    )
                    seen.append("follow-up from neutral")
                    if mode == "exception":
                        raise RuntimeError("native test failure")
                    if mode == "interrupt":
                        raise KeyboardInterrupt()
                    if mode == "source_change":
                        source.write_bytes(b"user edit")
                        return {"status": "no_candidate_found"}
                    if (
                        mode == "alternatives"
                        and seen.count("follow-up from neutral") == 1
                    ):
                        return {"status": "no_candidate_found"}
                    if mode == "unrelated":
                        return {"status": "no_candidate_found"}
                    return candidate(
                        b"neutral", b"improved", 10 if mode == "neutral" else 9
                    )
                if function is runner._verify_cumulative_reference_native:
                    seen.append("cumulative reference verification")
                    return {
                        "status": "degraded" if mode == "degraded" else "unverified"
                    }
                raise AssertionError(function)

            def verify(branch, project, expected_opens, *, progress=None):
                self.assertEqual(source.read_bytes(), b"best")
                self.assertEqual(expected_opens, 9)
                seen.append("native final verification")
                return {
                    "drc": {
                        "open_connections": 9,
                        "errors": 0,
                        "warnings": 0,
                        "parity_findings": 0,
                    },
                    "geometry": {},
                    "reference_copper": {"high_speed_nets": ["USB"]},
                }

            protected = SimpleNamespace(differences=lambda *args: ())
            with (
                patch.object(runner, "Snapshot", FakeSnapshot),
                patch.object(runner, "_replan", side_effect=replan),
                patch.object(runner, "run_native", side_effect=native),
                patch.object(runner, "verify_resume_board", side_effect=verify),
                patch.object(runner, "CampaignPlanner", side_effect=planner),
                patch.object(runner.ProtectedState, "capture", return_value=protected),
            ):
                try:
                    result = runner._neutral_lookahead(
                        outer,
                        b"neutral",
                        neutral,
                        {},
                        best_opens=10,
                        preferred_nets=("A",),
                        workers=1,
                        seconds_per_batch=5,
                    )
                finally:
                    self.assertEqual(
                        source.read_bytes(),
                        b"user edit" if mode == "source_change" else b"best",
                    )
            return result, source.read_bytes(), seen


if __name__ == "__main__":
    unittest.main()
