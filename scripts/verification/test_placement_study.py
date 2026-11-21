"""Placement study verdicts and equal-budget arms (no KiCad needed)."""

from __future__ import annotations

import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from autopcb import placement_study as study


def geometry(*, parts=None, lengths=(5.0,), vias=0):
    parts = parts or {"R1": (1, 1, "F.Cu"), "R2": (5, 5, "F.Cu")}
    return {"bounds": [[0, 0], [20, 10]],
            "footprints": [{"ref": r, "at": [x, y], "layer": s} for r, (x, y, s) in parts.items()],
            "tracks": [{"via": False, "length_mm": n} for n in lengths] + [{"via": True, "length_mm": 0}] * vias}


def arm(name, *, opens, passed=True, applied=True, area=200.0, length=10.0, vias=0, moved=(), preview=None):
    metrics = study.BoardMetrics(area, length, vias)
    return study.ArmResult(name, preview or {"best_preview_opens": opens}, metrics,
                           [{"ref": r} for r in moved], applied, opens, passed, 0.0)


ORIGINAL = study.BoardMetrics(200.0, 4.0, 0)


def verdict(fixed, moves):
    return study.Verdict(fixed, moves, ORIGINAL).as_dict()


class MetricsTests(unittest.TestCase):
    def test_vias_count_but_add_no_length(self):
        metrics = study.BoardMetrics.from_geometry(geometry(lengths=(3.0, 2.5), vias=2))
        self.assertEqual((metrics.track_length_mm, metrics.via_count, metrics.area_mm2), (5.5, 2, 200.0))

    def test_moves_report_shift_and_side_change(self):
        before = study.BoardMetrics.from_geometry(geometry())
        after = study.BoardMetrics.from_geometry(geometry(parts={"R1": (1, 4, "F.Cu"), "R2": (5, 5, "B.Cu")}))
        self.assertEqual(after.moved_from(before), [{"ref": "R1", "shift_mm": 3.0, "side_changed": False},
                                                    {"ref": "R2", "shift_mm": 0.0, "side_changed": True}])
        self.assertEqual(before.moved_from(before), [])


class VerdictTests(unittest.TestCase):
    def test_fixed_stall_and_verified_move_meet_the_criterion(self):
        result = verdict(arm("fixed", opens=3, applied=False),
                         arm("moves", opens=0, length=12, vias=1, moved=("R1",)))
        self.assertEqual((result["kept"], result["criterion_met"], result["fixed_stalled"]),
                         ("moves", True, True))
        self.assertEqual(result["moves_added_vs_original"],
                         {"track_length_mm": 8.0, "via_count": 1, "moved_parts": 1})
        self.assertIsNone(result["moves_vs_fixed"])

    def test_router_claim_without_native_pass_does_not_count(self):
        result = verdict(arm("fixed", opens=2), arm("moves", opens=0, passed=False, moved=("R1",)))
        self.assertEqual((result["kept"], result["criterion_met"]), (None, False))

    def test_both_complete_prefers_fixed_unless_moves_is_smaller(self):
        fixed, moves = arm("fixed", opens=0), arm("moves", opens=0, length=9, vias=2, moved=("R1",))
        result = verdict(fixed, moves)
        self.assertEqual((result["kept"], result["criterion_met"]), ("fixed", False))
        self.assertEqual(result["moves_vs_fixed"], {"track_length_mm": -1.0, "via_count": 2, "moved_parts": 1})
        self.assertEqual(verdict(fixed, arm("moves", opens=0, area=150.0))["kept"], "moves")


class ArmTests(unittest.TestCase):
    def run_arms(self, apply_error=None):
        calls = []

        def preview(self, **kwargs):
            calls.append(kwargs)
            return {"status": "preview", "run_id": "a" * 32,
                    "best_preview_opens": 0 if kwargs["allow_placement"] else 4}

        def apply(self, run_id):
            if apply_error and not calls[-1]["allow_placement"]:
                raise RuntimeError(apply_error)

        original = study.BoardMetrics.from_geometry(geometry())
        with (patch.object(study.jobs, "isolated", lambda *a: contextlib.nullcontext(SimpleNamespace(root="/x"))),
              patch.object(study.StudyArm, "copy_policy"),
              patch.object(study.Layout, "preview", preview), patch.object(study.Layout, "apply", apply),
              patch.object(study.jobs, "inspect", lambda *a: {"geometry": geometry(lengths=(7.0,))}),
              patch.object(study.jobs, "verify", lambda *a, **k: {"native": {"open_connections": 0, "passed": True}})):
            results = [study.StudyArm(None, SimpleNamespace(root="/x"), None, SimpleNamespace(),
                                      study.Budget(7, 11, 2.5), allow_placement=allow)(original)
                       for allow in (False, True)]
        return calls, results

    def test_arms_share_the_budget_and_differ_only_in_placement(self):
        calls, _ = self.run_arms()
        self.assertEqual([(c["rounds"], c["seconds"], c["memory_gib"]) for c in calls], [(7, 11, 2.5)] * 2)
        self.assertEqual([c["allow_placement"] for c in calls], [False, True])

    def test_failed_apply_is_recorded_and_unverified(self):
        _, (fixed, moves) = self.run_arms("stale preview")
        self.assertEqual((fixed.applied, fixed.preview["apply_error"], fixed.native_passed),
                         (False, "stale preview", False))
        self.assertTrue(moves.passes)


if __name__ == "__main__":
    unittest.main()
