"""External results stay private and do not overwrite prior run state."""

import unittest
from contextlib import nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from pcb.net_routing.external import (
    _compare_checked_board,
    _saved_checks,
    accepted_state,
    qualifies_via_reduction,
    qualifies_warning_reduction,
    removes_only_copper,
    retain_candidate,
)
from pcb.freeroute_delta import Trace, Via
from pcb.net_routing.runner import _verify_resume_connectivity


class ExternalRunStateTests(unittest.TestCase):
    def test_multi_router_provenance_requires_the_actual_final_tool(self):
        from pcb.net_routing.external import router_stage_evidence

        fingerprint = "a" * 64
        stages = (("fastroute", None), ("krt", fingerprint))
        evidence = router_stage_evidence(stages, "krt", fingerprint)
        self.assertEqual([item["engine"] for item in evidence], ["fastroute", "krt"])
        self.assertEqual(evidence[-1]["router_fingerprint"], fingerprint)
        with self.assertRaises(ValueError):
            router_stage_evidence(stages, "fastroute", None)
        with self.assertRaises(ValueError):
            router_stage_evidence(
                (("fastroute", None), ("krt", "invalid")), "krt", "invalid"
            )

    def test_keeps_prior_state_and_never_claims_completion(self):
        old = {
            "run_id": "a" * 32,
            "best_preview_opens": 262,
            "accepted_preview_batches": 22,
            "elapsed_seconds": 100.0,
            "progress_samples": [],
            "queue": [["old"]],
            "publication_enabled": False,
        }
        plan = {
            "rounds": [["new"]],
            "blocked_nets": {"USB": "unverified"},
            "island_counts": {},
        }
        state = accepted_state(
            old, "b" * 32, 250, plan, {"status": "sampled_unverified"}, [], 45.0
        )
        self.assertEqual(old["best_preview_opens"], 262)
        self.assertEqual(old["queue"], [["old"]])
        self.assertEqual(state["best_preview_opens"], 250)
        self.assertEqual(state["accepted_preview_batches"], 23)
        self.assertEqual(state["status"], "paused")
        self.assertFalse(state["publication_enabled"])
        self.assertEqual(state["renewed_from"], old["run_id"])
        optimized = accepted_state(
            old,
            "c" * 32,
            262,
            plan,
            {"status": "sampled_unverified"},
            [],
            10.0,
            allow_equal=True,
        )
        self.assertEqual(optimized["best_preview_opens"], 262)
        self.assertEqual(optimized["status"], "paused")

    def test_requires_strict_improvement(self):
        old = {"run_id": "a" * 32, "best_preview_opens": 262}
        plan = {"rounds": [], "blocked_nets": {}, "island_counts": {}}
        with self.assertRaisesRegex(ValueError, "improvement"):
            accepted_state(old, "b" * 32, 262, plan, {}, [], 0.0)

    def test_via_optimization_requires_no_connectivity_or_length_regression(self):
        before = {"via_count": 10, "track_length_mm": 100.0, "degree_two_joints": 20}
        good = {"via_count": 9, "track_length_mm": 99.0, "degree_two_joints": 20}
        self.assertTrue(qualifies_via_reduction(10, 10, before, good))
        self.assertFalse(qualifies_via_reduction(10, 11, before, good))
        self.assertFalse(
            qualifies_via_reduction(10, 10, before, {**good, "via_count": 10})
        )
        self.assertFalse(
            qualifies_via_reduction(10, 10, before, {**good, "track_length_mm": 101.0})
        )
        self.assertFalse(
            qualifies_via_reduction(10, 10, before, {**good, "degree_two_joints": 21})
        )
        for engine, objective in (
            ("fastroute", "minimize-vias"),
            ("native-via-prune", "reduce-opens"),
        ):
            with (
                self.subTest(engine=engine),
                self.assertRaisesRegex(ValueError, "Via pruning"),
            ):
                retain_candidate(
                    Path("missing"),
                    "a" * 32,
                    Path("missing"),
                    engine=engine,
                    objective=objective,
                )

    def test_via_pruning_cannot_sneak_in_other_copper(self):
        trace = Trace("N", "B.Cu", 200_000, (0, 0), (1_000_000, 0))
        via = Via("N", (1_000_000, 0), 600_000, 300_000)
        new_trace = Trace("N", "B.Cu", 200_000, (0, 0), (0, 1_000_000))
        self.assertTrue(removes_only_copper([trace, via], [trace]))
        self.assertFalse(removes_only_copper([trace, via], [trace, new_trace]))

    def test_dangling_cleanup_requires_fewer_warnings_and_removal_only(self):
        first = Trace("A", "B.Cu", 200_000, (0, 0), (1_000_000, 0))
        second = Trace("B", "F.Cu", 200_000, (0, 0), (0, 1_000_000))
        via = Via("B", (0, 1_000_000), 600_000, 300_000)
        before_copper, after_copper = [first, second, via], [second, via]
        before_drc, after_drc = (
            {"warnings": 5, "errors": 0},
            {"warnings": 4, "errors": 0},
        )
        before_geometry = {"via_count": 1, "track_length_mm": 2.0}
        after_geometry = {"via_count": 1, "track_length_mm": 1.0}

        def qualifies(opens, drc, geometry, copper):
            return qualifies_warning_reduction(
                3,
                opens,
                before_drc,
                drc,
                before_geometry,
                geometry,
                before_copper,
                copper,
            )

        self.assertTrue(qualifies(3, after_drc, after_geometry, after_copper))
        self.assertFalse(qualifies(4, after_drc, after_geometry, after_copper))
        self.assertFalse(
            qualifies(3, {"warnings": 5, "errors": 0}, after_geometry, after_copper)
        )
        self.assertFalse(
            qualifies(3, {"warnings": 4, "errors": 1}, after_geometry, after_copper)
        )
        self.assertFalse(
            qualifies(
                3, after_drc, {"via_count": 1, "track_length_mm": 3.0}, after_copper
            )
        )
        self.assertFalse(qualifies(3, after_drc, after_geometry, [second]))
        self.assertFalse(qualifies(3, after_drc, after_geometry, [second, via, first]))
        added = Trace("C", "B.Cu", 200_000, (0, 0), (0, 1_000_000))
        self.assertFalse(qualifies(3, after_drc, after_geometry, [second, via, added]))

    def test_dangling_cleanup_cannot_use_the_routing_or_via_objective(self):
        for engine, objective in (
            ("native-dangling-prune", "reduce-opens"),
            ("fastroute", "reduce-warnings"),
        ):
            with (
                self.subTest(engine=engine),
                self.assertRaisesRegex(ValueError, "Dangling"),
            ):
                retain_candidate(
                    Path("missing"),
                    "a" * 32,
                    Path("missing"),
                    engine=engine,
                    objective=objective,
                )

    def test_staircase_cleanup_requires_matching_provenance(self):
        for engine, objective in (
            ("native-staircase-repair", "reduce-opens"),
            ("fastroute", "repair-staircase"),
        ):
            with (
                self.subTest(engine=engine),
                self.assertRaisesRegex(ValueError, "Staircase"),
            ):
                retain_candidate(
                    Path("missing"),
                    "a" * 32,
                    Path("missing"),
                    engine=engine,
                    objective=objective,
                )

    def test_junction_cleanup_requires_the_exact_local_chamfer_and_fewer_warnings(self):
        from pcb.net_routing.external import qualifies_corner_cleanup

        def trace(start, end, *, net="A", layer="B.Cu", width=200_000):
            return Trace(net, layer, width, start, end)

        trunk = trace((0, 0), (4_000_000, 0))
        branch = trace((2_000_000, 0), (2_000_000, 2_000_000))
        spur = trace((4_000_000, 0), (5_000_000, 0))
        shortened = trace((0, 0), (1_750_000, 0))
        clipped = trace((2_000_000, 250_000), (2_000_000, 2_000_000))
        diagonal = trace((1_750_000, 0), (2_000_000, 250_000))
        via = Via("A", (0, 0), 600_000, 300_000)
        before, after = [trunk, branch, spur, via], [shortened, clipped, diagonal, via]
        old_drc, new_drc = {"warnings": 5, "errors": 0}, {"warnings": 4, "errors": 0}
        old_geometry = {"via_count": 1, "track_length_mm": 7.0}
        new_geometry = {"via_count": 1, "track_length_mm": 4.0}

        def qualifies(candidate, *, opens=3, drc=new_drc, geometry=new_geometry):
            return qualifies_corner_cleanup(
                3, opens, old_drc, drc, old_geometry, geometry, before, candidate
            )

        self.assertTrue(qualifies(after))
        self.assertFalse(qualifies(after, opens=4))
        self.assertFalse(qualifies(after, drc=old_drc))
        self.assertFalse(qualifies(after, geometry={**new_geometry, "via_count": 0}))
        self.assertFalse(
            qualifies(after, geometry={**new_geometry, "track_length_mm": 8.0})
        )
        self.assertFalse(qualifies([shortened, clipped, diagonal]))
        bad_diagonal = trace((1_750_000, 0), (2_000_000, 300_000))
        self.assertFalse(qualifies([shortened, clipped, bad_diagonal, via]))
        bad_width = trace(diagonal.start, diagonal.end, width=150_000)
        self.assertFalse(qualifies([shortened, clipped, bad_width, via]))
        unrelated = trace((1_750_000, 0), (2_000_000, 250_000), net="B")
        self.assertFalse(qualifies([shortened, clipped, unrelated, via]))
        longer_a = trace((0, 0), (1_700_000, 0))
        longer_b = trace((2_000_000, 300_000), (2_000_000, 2_000_000))
        longer_diagonal = trace((1_700_000, 0), (2_000_000, 300_000))
        self.assertFalse(qualifies([longer_a, longer_b, longer_diagonal, via]))

    def test_via_removal_can_reclassify_an_existing_junction(self):
        before = {"via_count": 10, "track_length_mm": 100.0, "degree_two_joints": 20}
        after = {"via_count": 9, "track_length_mm": 99.0, "degree_two_joints": 21}
        trace = Trace("N", "B.Cu", 200_000, (0, 0), (1_000_000, 0))
        via = Via("N", (1_000_000, 0), 600_000, 300_000)
        added = Trace("N", "B.Cu", 200_000, (1_000_000, 0), (2_000_000, 0))
        self.assertFalse(qualifies_via_reduction(10, 10, before, after))
        self.assertTrue(
            qualifies_via_reduction(
                10, 10, before, after, before_copper=[trace, via], after_copper=[trace]
            )
        )
        self.assertFalse(
            qualifies_via_reduction(
                10,
                10,
                before,
                after,
                before_copper=[trace, via],
                after_copper=[trace, added],
            )
        )

    def test_rejects_unknown_router_provenance_before_reading_inputs(self):
        with self.assertRaisesRegex(ValueError, "provenance"):
            retain_candidate(
                Path("missing"), "a" * 32, Path("missing"), engine="unknown"
            )

    def test_krt_provenance_records_equal_layer_costs_and_requires_identity(self):
        from pcb.net_routing import external

        self.assertEqual(
            external.layer_cost_evidence("krt"), "native_router_equal_costs_configured"
        )
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            retain_candidate(Path("missing"), "a" * 32, Path("missing"), engine="krt")

    def test_krt_retention_cli_binds_the_verified_installation_identity(self):
        from pcb.net_routing import external

        outcome = {
            "saved_board_opens": 189,
            "preview_opens": 188,
            "errors": 2,
            "warnings": 26,
            "run_id": "b" * 32,
        }
        with (
            patch(
                "pcb.net_routing.krt_adapter.validate_installation",
                return_value="a" * 64,
            ),
            patch.object(
                external, "quiet_duplicate_image_handlers", return_value=nullcontext()
            ),
            patch.object(external, "retain_candidate", return_value=outcome) as retain,
            redirect_stdout(StringIO()),
        ):
            status = external.main(
                [
                    "--project",
                    ".",
                    "--run",
                    "a" * 32,
                    "--candidate",
                    "candidate.kicad_pcb",
                    "--engine",
                    "krt",
                ]
            )
        self.assertEqual(status, 0)
        self.assertEqual(retain.call_args.kwargs["router_fingerprint"], "a" * 64)

    def test_tracemaker_requires_identity_without_claiming_layer_cost_proof(self):
        from pcb.net_routing import external

        self.assertEqual(
            external.layer_cost_evidence("TraceMaker"),
            "session_layer_costs_not_independently_verified",
        )
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            retain_candidate(
                Path("missing"), "a" * 32, Path("missing"), engine="TraceMaker"
            )
        stages = (("fastroute", None), ("TraceMaker", "c" * 64))
        self.assertEqual(
            external.router_stage_evidence(stages, "TraceMaker", "c" * 64)[-1][
                "engine"
            ],
            "TraceMaker",
        )
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            external.router_stage_evidence(
                (("fastroute", None), ("TraceMaker", None)), "TraceMaker", None
            )

    def test_tracemaker_cli_binds_the_binary_used(self):
        from pcb.net_routing import external

        outcome = {
            "saved_board_opens": 134,
            "preview_opens": 133,
            "errors": 212,
            "warnings": 69,
            "run_id": "b" * 32,
        }
        with TemporaryDirectory() as directory:
            binary = Path(directory) / "tracemaker"
            binary.write_bytes(b"example router binary")
            with (
                patch.object(
                    external,
                    "quiet_duplicate_image_handlers",
                    return_value=nullcontext(),
                ),
                patch.object(
                    external, "retain_candidate", return_value=outcome
                ) as retain,
                redirect_stdout(StringIO()),
            ):
                status = external.main(
                    [
                        "--project",
                        ".",
                        "--run",
                        "a" * 32,
                        "--candidate",
                        "candidate.kicad_pcb",
                        "--engine",
                        "TraceMaker",
                        "--router",
                        str(binary),
                    ]
                )
            self.assertEqual(status, 0)
            self.assertEqual(
                retain.call_args.kwargs["router_fingerprint"], external.sha(binary)
            )

    def test_native_zone_stitch_has_its_own_provenance(self):
        try:
            retain_candidate(
                Path("missing"), "a" * 32, Path("missing"), engine="native-zone-stitch"
            )
        except Exception as error:
            self.assertIsInstance(error, FileNotFoundError)
        else:
            self.fail("Missing inputs must not be accepted")

    def test_reused_native_reports_still_require_connectivity_and_no_new_violations(
        self,
    ):
        snapshot = MagicMock()
        snapshot.directory = Path("private")
        baseline = {
            "drc": {
                "open_connections": 5,
                "ignored_checks": [],
                "violation_keys": [],
                "parity_keys": [],
            }
        }
        candidate = {"drc": {**baseline["drc"], "open_connections": 4}}
        with (
            patch("pcb.net_routing.external.run_native") as native,
            patch("pcb.net_routing.external.stable_connection_items", return_value={}),
        ):
            _compare_checked_board(snapshot, baseline, candidate, 4, b"saved")
        native.assert_called_once_with(
            _verify_resume_connectivity, snapshot.directory, b"saved"
        )
        snapshot.check.assert_called_once()

        candidate["drc"]["violation_keys"] = [("shorting_items", "error", ("new",))]
        with (
            patch("pcb.net_routing.external.run_native") as native,
            patch("pcb.net_routing.external.stable_connection_items", return_value={}),
        ):
            with self.assertRaisesRegex(RuntimeError, "violations"):
                _compare_checked_board(snapshot, baseline, candidate, 4, b"saved")
        native.assert_not_called()

    def test_saved_drc_cache_is_reused_only_for_identical_sources_and_tools(self):
        cache = {}
        reports = [{"drc": {"errors": n}} for n in range(4)]
        with patch(
            "pcb.net_routing.external.run_native", side_effect=reports
        ) as native:
            first = _saved_checks(
                Path("project"), {"board": "v1"}, {"cli": "v1"}, "policy", cache
            )
            first["drc"]["errors"] = 99
            second = _saved_checks(
                Path("project"), {"board": "v1"}, {"cli": "v1"}, "policy", cache
            )
            changed = _saved_checks(
                Path("project"), {"board": "v2"}, {"cli": "v1"}, "policy", cache
            )
            changed_tool = _saved_checks(
                Path("project"), {"board": "v2"}, {"cli": "v2"}, "policy", cache
            )
            changed_policy = _saved_checks(
                Path("project"), {"board": "v2"}, {"cli": "v2"}, "new", cache
            )
        self.assertEqual(second["drc"]["errors"], 0)
        self.assertEqual(changed["drc"]["errors"], 1)
        self.assertEqual(changed_tool["drc"]["errors"], 2)
        self.assertEqual(changed_policy["drc"]["errors"], 3)
        self.assertEqual(native.call_count, 4)


if __name__ == "__main__":
    unittest.main()
