"""test_autoroute_lookahead.py: tests of the disposable neutral-preview lookahead.

A neutral preview is disposable until a better board passes every check.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class PreviewCandidates:
    """Builds validated-preview dicts like those the neutral lookahead consumes."""

    @staticmethod
    def digest(value: bytes) -> str:
        """Return the SHA-256 hex digest of the bytes."""
        return hashlib.sha256(value).hexdigest()

    @classmethod
    def candidate(cls, source: bytes, board: bytes, opens: int, **validation):
        """Build a validated-preview dict for a candidate board.

        Inputs:
            source: Bytes of the source board.
            board: Bytes of the candidate board, carried as the one change.
            opens: Open connections left on the candidate.
            validation: Entries that override or extend the validation dict.
        Outputs:
            The preview dict with the source and candidate digests.
        """
        return {
            "status": "validated_preview",
            "source_sha256": cls.digest(source),
            "candidate_sha256": cls.digest(board),
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
    """Stand-in for the runner's Snapshot that fails on exit when the source changed."""

    def __init__(self, source):
        """Remember the source board.

        Inputs:
            source: Board file to copy.
        """
        self.source = Path(source)

    def __enter__(self):
        """Copy the board and a protected schematic into a temporary folder.

        Outputs:
            This snapshot, with ``directory`` and ``board_path`` set.
        """
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.board_path = self.directory / "v4.1.kicad_pcb"
        self.original = self.source.read_bytes()
        self.board_path.write_bytes(self.original)
        (self.directory / "v4.1.kicad_sch").write_bytes(b"protected schematic")
        return self

    def check(self):
        """Raise RuntimeError when the source board no longer holds its original bytes."""
        if self.source.read_bytes() != self.original:
            raise RuntimeError("Routing sources changed")

    def __exit__(self, exc_type, exc, tb):
        """Check the source after a clean block, then remove the temporary folder."""
        try:
            if exc_type is None:
                self.check()
        finally:
            self.temp.cleanup()


class AutoLookaheadTests(unittest.TestCase):
    """Tests of the neutral lookahead and its protection of the original board."""

    def test_only_checked_neutral_previews_can_be_stepping_stones(self):
        from pcb.net_routing.runner import PreviewJudge

        good = PreviewCandidates.candidate(b"best", b"neutral", 10)
        self.assertTrue(PreviewJudge.neutral_eligible(good, 10))
        for bad in (
            PreviewCandidates.candidate(b"best", b"x", 9),
            PreviewCandidates.candidate(b"best", b"x", 11),
            PreviewCandidates.candidate(
                b"best", b"x", 10, schematic_parity_checked=False
            ),
            PreviewCandidates.candidate(
                b"best", b"x", 10, reference_comparison={"status": "degraded"}
            ),
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
        """Run the runner's neutral lookahead with scripted stubs.

        Inputs:
            mode: ``improved``, ``neutral``, ``degraded``, ``exception``, ``interrupt``,
                ``source_change``, ``alternatives`` or ``unrelated``; it sets how the
                stubbed follow-up round behaves.
        Outputs:
            The lookahead result, the final bytes of the source board and the stages the
            stubs saw.
        """
        from pcb.net_routing import runner

        seen = []
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "v4.1.kicad_pcb"
            source.write_bytes(b"best")
            (source.parent / "v4.1.kicad_sch").write_bytes(b"protected schematic")
            outer = SimpleNamespace(board_path=source, directory=source.parent)
            neutral = PreviewCandidates.candidate(b"best", b"neutral", 10)
            if mode == "alternatives":
                neutral["nets"] = ["A", "B"]
            request = {"nets": [{"name": "A"}], "search": {"seconds": 5}}

            def replan(*args, **kwargs):
                """Stub for replanning: one round with the request for net A.

                In the ``alternatives`` and ``unrelated`` modes the round also holds a
                request for net B.
                """
                other = {"nets": [{"name": "B"}], "search": {"seconds": 5}}
                return {
                    "rounds": [
                        [request, other]
                        if mode in {"alternatives", "unrelated"}
                        else [request]
                    ]
                }

            def planner(directory):
                """Stub planner factory: return an object with a plan method."""

                def plan(kind, changes):
                    """Return a plan whose result is the bytes of the first change."""
                    image = changes[0]["bytes"]
                    return SimpleNamespace(
                        after=image, result_sha256=PreviewCandidates.digest(image)
                    )

                return SimpleNamespace(plan=plan)

            def native(function, *args, **kwargs):
                """Stub for the native runner, scripted by the mode.

                Answer a follow-up preview round or a cumulative reference check as the
                mode says.

                Inputs:
                    function: Runner function being called; any other raises
                        AssertionError.
                """
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
                    return PreviewCandidates.candidate(
                        b"neutral", b"improved", 10 if mode == "neutral" else 9
                    )
                if function is runner._verify_cumulative_reference_native:
                    seen.append("cumulative reference verification")
                    return {
                        "status": "degraded" if mode == "degraded" else "unverified"
                    }
                raise AssertionError(function)

            def verify(branch, project, expected_opens, *, progress=None):
                """Stub for the final board verification.

                Check that the source is still the best board and that nine open
                connections are expected, then report a clean result with nine open
                connections.
                """
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
