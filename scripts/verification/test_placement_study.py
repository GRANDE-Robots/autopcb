"""Placement study: arm bookkeeping, verdict rules and equal-budget invocation (no KiCad needed)."""

from __future__ import annotations

import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from autopcb import placement_study as study


def geometry(*, size=(20, 10), parts=None, lengths=(5.0,), vias=0):
    parts = parts or {"R1": (1, 1, "F.Cu"), "R2": (5, 5, "F.Cu")}
    return {"bounds": [[0, 0], list(size)],
            "footprints": [{"ref": r, "at": [x, y], "layer": layer} for r, (x, y, layer) in parts.items()],
            "tracks": [{"via": False, "length_mm": n} for n in lengths] + [{"via": True, "length_mm": 0}] * vias}


def arm(name, *, opens, passed=True, applied=True, area=200.0, length=10.0, vias=0, moved=(), preview_opens=None):
    return {"arm": name, "applied": applied, "verified_opens": opens, "native_passed": passed,
            "preview_opens": preview_opens if preview_opens is not None else opens,
            "board_area_mm2": area, "track_length_mm": length, "via_count": vias,
            "moved_parts": [{"ref": r} for r in moved]}


ORIGINAL = {"board_area_mm2": 200.0, "track_length_mm": 4.0, "via_count": 0}


class MetricsTests(unittest.TestCase):
    def test_metrics_exclude_vias_from_length_and_count_them(self):
        metrics = study.board_metrics(geometry(lengths=(3.0, 2.5), vias=2))
        self.assertEqual((metrics["track_length_mm"], metrics["via_count"], metrics["board_area_mm2"]),
                         (5.5, 2, 200.0))

    def test_moved_parts_report_shift_and_side_changes_only(self):
        before = study.board_metrics(geometry())
        after = study.board_metrics(geometry(parts={"R1": (1, 4, "F.Cu"), "R2": (5, 5, "B.Cu")}))
        self.assertEqual(study.moved_parts(before, after),
                         [{"ref": "R1", "shift_mm": 3.0, "side_changed": False},
                          {"ref": "R2", "shift_mm": 0.0, "side_changed": True}])
        self.assertEqual(study.moved_parts(before, before), [])


class VerdictTests(unittest.TestCase):
    def test_fixed_stall_and_verified_move_meet_the_criterion(self):
        verdict = study.decide(arm("fixed", opens=3, applied=False), arm("moves", opens=0, length=12, vias=1,
                                                                        moved=("R1",)), ORIGINAL)
        self.assertEqual((verdict["kept"], verdict["criterion_met"], verdict["fixed_stalled"]),
                         ("moves", True, True))
        self.assertEqual(verdict["moves_added_vs_original"], {"track_length_mm": 8.0, "via_count": 1, "moved_parts": 1})
        self.assertIsNone(verdict["moves_vs_fixed"])

    def test_router_claim_without_native_pass_does_not_count(self):
        verdict = study.decide(arm("fixed", opens=2), arm("moves", opens=0, passed=False, moved=("R1",)), ORIGINAL)
        self.assertEqual((verdict["kept"], verdict["criterion_met"]), (None, False))

    def test_if_both_complete_prefer_smaller_then_fewer_moves(self):
        fixed = arm("fixed", opens=0, length=10, vias=0)
        moves = arm("moves", opens=0, length=9, vias=2, moved=("R1",))
        verdict = study.decide(fixed, moves, ORIGINAL)
        self.assertEqual((verdict["kept"], verdict["criterion_met"]), ("fixed", False))
        self.assertEqual(verdict["moves_vs_fixed"], {"track_length_mm": -1.0, "via_count": 2, "moved_parts": 1})
        smaller = study.decide(fixed, {**moves, "board_area_mm2": 150.0}, ORIGINAL)
        self.assertEqual(smaller["kept"], "moves")

    def test_unapplied_arm_never_passes(self):
        self.assertIsNone(study.decide(arm("fixed", opens=0, applied=False), arm("moves", opens=1), ORIGINAL)["kept"])


class DriverTests(unittest.TestCase):
    def run_arms(self, apply_error=None):
        calls = []

        def preview(native, project, **kwargs):
            calls.append(kwargs)
            return {"status": "preview", "run_id": "a" * 32,
                    "best_preview_opens": 0 if kwargs["allow_placement"] else 4}

        def apply(*args, **kwargs):
            if apply_error and not calls[-1]["allow_placement"]:
                raise RuntimeError(apply_error)
            return {"status": "saved_work_in_progress"}

        working = SimpleNamespace(root="/tmp/none")
        with (patch.object(study.jobs, "isolated", lambda *a, **k: contextlib.nullcontext(working)),
              patch.object(study, "_copy_evidence", return_value={}),
              patch.object(study.layout, "preview", preview), patch.object(study.layout, "apply", apply),
              patch.object(study.jobs, "inspect", lambda *a, **k: {"geometry": geometry(lengths=(7.0,))}),
              patch.object(study.jobs, "verify", lambda *a, **k: {"native": {"open_connections": 0, "passed": True}})):
            original = study.board_metrics(geometry())
            records = [study.run_arm(None, SimpleNamespace(root="/x"), None, SimpleNamespace(), name,
                                     rounds=7, seconds=11, memory_gib=2.5, original=original)
                       for name in study.ARMS]
        return calls, records

    def test_arms_share_the_budget_and_differ_only_in_placement(self):
        calls, _ = self.run_arms()
        self.assertEqual([{k: v for k, v in c.items() if k not in ("allow_placement", "storage")} for c in calls],
                         [{"rounds": 7, "seconds": 11, "memory_gib": 2.5}] * 2)
        self.assertEqual([c["allow_placement"] for c in calls], [False, True])

    def test_failed_apply_is_recorded_and_unverified(self):
        _, (fixed, moves) = self.run_arms(apply_error="stale preview")
        self.assertEqual((fixed["applied"], fixed["apply_error"], fixed["native_passed"]), (False, "stale preview", False))
        self.assertTrue(moves["applied"] and moves["native_passed"])


if __name__ == "__main__":
    unittest.main()
