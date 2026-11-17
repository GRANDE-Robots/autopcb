"""Recovery changes the search problem while preserving the design constraints."""

import copy
import io
import unittest

from core.console import Console
from pcb.net_routing.checkpoint import search_window
from pcb.net_routing.model import Budget, Island, Limits, Net
from pcb.net_routing.recovery import RecoveryPolicy, next_stage, search_key
from pcb.net_routing.runner import _split_failed, summarize_attempts
from pcb.route_optimizer import GridRouter, NoGridRoute, Obstacle, Point, mm
from verification import test_net_routing as fixtures


def request():
    return {
        "shared": {"bounds_mm": [0, 0, 20, 20], "layers": ["F.Cu"],
                   "step_mm": 1, "width_mm": 0.2, "clearance_mm": 0.2,
                   "via": {"size_mm": 0.6, "drill_mm": 0.3},
                   "gentle_turns": True, "forbid_staircase": True,
                   "current_model": {"unchanged": "evidence"}},
        "nets": [{"name": "SIGNAL"}], "search": {"seconds": 30},
        "max_islands": 3, "checkpoint_offset": 0, "recovery_stage": 0,
        "recovery_bounds_mm": [-10, -10, 40, 40],
    }


class RecoveryTests(unittest.TestCase):
    def test_renewal_rebuilds_rejected_checkpoint_from_current_policy(self):
        from pcb.net_routing import runner

        previous = {
            "outcome": "rejected",
            "attempts": [
                {"status": "geometric_candidate_only", "checkpoints": [
                    {"net": "SIGNAL", "stage": 3, "index": 1,
                     "bounds_mm": [99, 99, 100, 100]},
                ]},
                {"status": "native_validation_rejected"},
            ],
        }
        retries = runner._rejected_checkpoint_retries([[request()]], previous, RecoveryPolicy())
        self.assertEqual(len(retries), 1)
        rebuilt = retries[0][0]
        self.assertEqual(rebuilt["recovery_stage"], 3)
        self.assertEqual(rebuilt["checkpoint_offset"], 1)
        self.assertEqual(rebuilt["shared"]["bounds_mm"], [-10, -10, 40, 40])
        self.assertNotIn("99", str(rebuilt))
        self.assertEqual(runner._rejected_checkpoint_retries(
            [[request()]], {**previous, "outcome": "no_candidate_found"}, RecoveryPolicy()
        ), [])

    def test_renewed_repair_prefers_the_pending_kind_without_replaying_geometry(self):
        from pcb.net_routing.recovery import renew_queue, prioritize_recovery_requests

        old = copy.deepcopy(request())
        old.update(
            recovery_stage=4,
            nets=[{"name": "SIGNAL"}, {"name": "GND"}],
            rip_up={"GND": [{"kind": "segment", "uuid": "obsolete"}]},
            movable=[{"reference": "R1", "positions_mm": [[99, 99]]}],
        )
        marker = renew_queue([[request()]], [[old]], RecoveryPolicy())[0][0]
        self.assertEqual(marker["recovery_stage"], 4)
        self.assertNotIn("rip_up", marker)
        self.assertNotIn("movable", marker)
        self.assertNotIn("obsolete", str(marker))
        self.assertNotIn("99", str(marker))
        fresh_copper = {**copy.deepcopy(request()), "nets": [{"name": "SIGNAL"}, {"name": "GND"}],
                        "rip_up": {"GND": [{"kind": "segment", "uuid": "new-a"}]}}
        fresh_move = {**copy.deepcopy(fresh_copper),
                      "movable": [{"reference": "R1", "positions_mm": [[2, 3]]}]}
        ordered = prioritize_recovery_requests(
            [fresh_copper, fresh_move], marker["recovery_preference"]
        )
        self.assertIs(ordered[0], fresh_move)
        self.assertEqual(ordered[0]["movable"][0]["positions_mm"], [[2, 3]])
        self.assertEqual(ordered[0]["rip_up"]["GND"][0]["uuid"], "new-a")

    def test_failed_transaction_explains_which_order_stopped_where(self):
        from pcb.net_routing.presentation import _attempts

        report = summarize_attempts({"attempts": [{
            "status": "no_complete_candidate_found",
            "search": {"path_calls": 2, "elapsed_seconds": 3},
            "trials": [{"orders": [{
                "order": ["GND", "TPS_SS"], "nets_reached": 1,
                "searches": [
                    {"net": "GND", "candidates": 1},
                    {"net": "TPS_SS", "candidates": 0},
                ],
            }]}],
        }]})
        output = io.StringIO()
        _attempts(Console(stream=output, verbose=True), report)
        self.assertIn("GND", output.getvalue())
        self.assertIn("TPS_SS", output.getvalue())
        self.assertIn("stopped", output.getvalue().lower())

    def test_failed_transaction_reports_ignored_zone_connectivity(self):
        from pcb.net_routing.presentation import _attempts

        report = summarize_attempts({"attempts": [{
            "status": "no_complete_candidate_found",
            "search": {"path_calls": 2, "elapsed_seconds": 3},
            "trials": [{"ignored_zone_count": 2, "orders": []}],
        }]})
        output = io.StringIO()
        _attempts(Console(stream=output, verbose=True), report)
        self.assertIn("zone", output.getvalue().lower())
        self.assertIn("2", output.getvalue())

    def test_refilled_zone_search_is_not_reported_as_uncredited(self):
        from pcb.net_routing.presentation import _attempts

        report = summarize_attempts({"attempts": [{
            "status": "no_complete_candidate_found",
            "search": {"path_calls": 1, "elapsed_seconds": 1},
            "trials": [{"ignored_zone_count": 2, "zone_connectivity": "refilled", "orders": []}],
        }]})
        output = io.StringIO()
        _attempts(Console(stream=output, verbose=True), report)
        self.assertIn("refilled", output.getvalue().lower())
        self.assertNotIn("not credited", output.getvalue().lower())

    def test_console_explains_search_outcomes_without_claiming_impossibility(self):
        from pcb.net_routing.presentation import _attempts

        output = io.StringIO()
        _attempts(
            Console(stream=output, verbose=True),
            [{
                "limits_hit": ["net_seconds", "path_expansions"],
                "failures": {"no_path_in_grid": 2},
                "failure_details": {},
            }],
        )
        text = output.getvalue()
        self.assertIn("Net time slice reached", text)
        self.assertIn("Path-search expansion limit reached", text)
        self.assertIn("No route found in tested grid", text)
        self.assertNotIn("no_path_in_grid", text)

    def test_native_rejections_keep_counts_and_bounded_human_readable_evidence(self):
        result = {"status": "rejected", "attempts": [], "validation": {
            "new_violations": [{"severity": "warning", "type": "track_dangling",
                                "description": "Unconnected end at X 5 mm", "items": [{"uuid": "t"}]}] * 20,
            "broken_existing_connections": [{"net": "N"}],
            "baseline_unconnected": 3, "candidate_unconnected": 4,
            "reference_comparison": {"status": "degraded"},
        }}
        report = summarize_attempts(result)[0]
        self.assertIn("20 new warning track_dangling", report["reason"])
        self.assertIn("1 broken existing connections", report["reason"])
        self.assertIn("3 to 4", report["reason"])
        self.assertIn("reference copper degraded", report["reason"])
        self.assertEqual(len(report["rejected_candidates"]), 2)
        self.assertIn("track_dangling", report["rejected_candidates"][0]["reason"])
        self.assertIn("Existing connection on N", report["rejected_candidates"][1]["reason"])
        output = io.StringIO()
        from pcb.net_routing.presentation import _attempts
        _attempts(Console(stream=output, verbose=True), [report])
        self.assertIn("Unconnected end at X 5 mm", output.getvalue())

    def test_stages_preserve_physical_rules_and_do_not_mutate_the_request(self):
        initial = request()
        preserved = copy.deepcopy(initial)
        current = initial
        for stage in (1, 2, 3):
            current = next_stage(current, RecoveryPolicy())
            self.assertEqual(current["recovery_stage"], stage)
            self.assertEqual(current["max_islands"], 2)
            self.assertEqual(current["search"]["seconds"], 30)
            for key in ("layers", "width_mm", "clearance_mm", "via", "current_model", "gentle_turns", "forbid_staircase"):
                self.assertEqual(current["shared"][key], initial["shared"][key])
        self.assertEqual(initial, preserved)
        self.assertEqual(current["shared"]["bounds_mm"], initial["recovery_bounds_mm"])
        reroute = next_stage(current, RecoveryPolicy())
        self.assertEqual(reroute["recovery_stage"], 4)
        self.assertIsNone(next_stage(reroute, RecoveryPolicy()))
        self.assertIsNone(next_stage(current, RecoveryPolicy(rip_up_candidates=0, placement_candidates=0)))
        self.assertEqual(next_stage(current, RecoveryPolicy(rip_up_candidates=0))["recovery_stage"], 4)

    def test_repair_fragments_are_never_dropped_to_make_a_pair(self):
        repair = {**request(), "rip_up": {"SIGNAL": [{"uuid": "old-copper", "kind": "segment"}]}}
        result = next_stage(repair, RecoveryPolicy())
        self.assertEqual(result["max_islands"], 3)
        self.assertEqual(result["rip_up"], repair["rip_up"])

    def test_actual_unique_choice_count_controls_retry_and_escalation_is_deferred(self):
        current = request()
        queue = [[{"next_net": "OTHER"}]]
        result = {"attempts": [{"checkpoints": [{"net": "SIGNAL", "count": 1}]}]}
        _split_failed(queue, [current], {"SIGNAL": 3}, result)
        self.assertEqual(queue[0], [{"next_net": "OTHER"}])
        self.assertEqual(queue[1][0]["recovery_stage"], 1)
        self.assertEqual(queue[1][0]["checkpoint_offset"], 0)

    def test_exhausted_and_unsupported_requests_do_not_spin(self):
        current = request()
        queue = []
        _split_failed(queue, [current], {"SIGNAL": 100},
                      {"attempts": [{"status": "checkpoint_exhausted"}]})
        self.assertEqual(queue[0][0]["recovery_stage"], 1)
        queue = []
        _split_failed(queue, [current], {"SIGNAL": 100},
                      {"attempts": [{"status": "unsupported_request"}]})
        self.assertFalse(queue)

    def test_recovery_does_not_wait_behind_the_entire_board(self):
        queue = [[{"pending": index}] for index in range(20)]
        result = {"attempts": [{"checkpoints": [{"net": "SIGNAL", "count": 1}]}]}
        _split_failed(queue, [request()], {"SIGNAL": 3}, result)
        self.assertEqual(queue[0], [{"pending": 0}])
        self.assertEqual(queue[1], [{"pending": 1}])
        self.assertEqual(queue[2][0]["recovery_stage"], 1)
        self.assertEqual(queue[-1], [{"pending": 19}])

    def test_early_rework_is_optional_and_keeps_wider_search_in_the_queue(self):
        from pcb.net_routing.runner import _pop_fair_round

        other = [{"nets": [{"name": "OTHER"}], "recovery_stage": 0}]
        queue = [other]
        result = {"attempts": [{"checkpoints": [{"net": "SIGNAL", "count": 1}]}]}
        _split_failed(queue, [request()], {"SIGNAL": 3}, result,
                      RecoveryPolicy(early_rework=True))
        self.assertEqual({batch[0].get("recovery_stage") for batch in queue}, {0, 1, 4})
        chosen = _pop_fair_round(queue, {"SIGNAL": 1}, {})
        self.assertEqual(chosen[0]["recovery_stage"], 4)
        self.assertTrue(chosen[0]["early_rework"])
        self.assertTrue(any(batch[0].get("recovery_stage") == 1 for batch in queue))
        with self.assertRaisesRegex(ValueError, "early_rework"):
            RecoveryPolicy.from_config({"early_rework": "yes"})

    def test_early_rework_recompiles_failed_concrete_repairs(self):
        for field, value in (
            ("rip_up", {"SIGNAL": [{"kind": "segment", "uuid": "old"}]}),
            ("movable", [{"reference": "R1", "positions_mm": [[3, 4]]}]),
        ):
            with self.subTest(field=field):
                failed = {**request(), field: value}
                queue = []
                result = {"attempts": [{"checkpoints": [{"net": "SIGNAL", "count": 1}]}]}
                _split_failed(queue, [failed], {"SIGNAL": 2}, result,
                              RecoveryPolicy(early_rework=True))
                marker = next(batch[0] for batch in queue if batch[0].get("early_rework"))
                self.assertNotIn("rip_up", marker)
                self.assertNotIn("movable", marker)
                self.assertNotIn("old", str(marker))
                self.assertNotIn("3, 4", str(marker))
                self.assertEqual(marker["recovery_preference"],
                                 {"neighbors": [], "moving": ["R1"] if field == "movable" else []})

    def test_renewal_preserves_early_rework_and_wider_search_without_old_geometry(self):
        from pcb.net_routing.recovery import renew_queue

        early = {**copy.deepcopy(request()), "recovery_stage": 4, "early_rework": True,
                 "rip_up": {"SIGNAL": [{"kind": "segment", "uuid": "obsolete"}]}}
        wider = next_stage(request(), RecoveryPolicy())
        rebuilt = renew_queue([[request()]], [[early], [wider]], RecoveryPolicy(early_rework=True))
        self.assertEqual({batch[0]["recovery_stage"] for batch in rebuilt}, {1, 4})
        marker = next(batch[0] for batch in rebuilt if batch[0]["recovery_stage"] == 4)
        self.assertTrue(marker["early_rework"])
        self.assertNotIn("rip_up", marker)
        self.assertNotIn("obsolete", str(rebuilt))

    def test_renewal_strips_concrete_geometry_from_fresh_repair_markers(self):
        from pcb.net_routing.recovery import renew_queue

        early = {**copy.deepcopy(request()), "recovery_stage": 4, "early_rework": True}
        fresh = {**copy.deepcopy(request()),
                 "rip_up": {"SIGNAL": [{"kind": "segment", "uuid": "fresh-track"}]},
                 "movable": [{"reference": "R1", "positions_mm": [[3, 4]]}]}
        rebuilt = renew_queue([[fresh]], [[early]], RecoveryPolicy(early_rework=True))
        marker = next(batch[0] for batch in rebuilt if batch[0].get("early_rework"))
        self.assertNotIn("rip_up", marker)
        self.assertNotIn("movable", marker)
        self.assertNotIn("fresh-track", str(marker))

    def test_renewal_keeps_geometry_free_early_repair_preference(self):
        from pcb.net_routing.recovery import renew_queue

        early = {**copy.deepcopy(request()), "recovery_stage": 4, "early_rework": True,
                 "recovery_preference": {"neighbors": [], "moving": ["R1"]}}
        rebuilt = renew_queue([[request()]], [[early]], RecoveryPolicy(early_rework=True))
        marker = next(batch[0] for batch in rebuilt if batch[0].get("early_rework"))
        self.assertEqual(marker["recovery_preference"], {"neighbors": [], "moving": ["R1"]})

    def test_repair_alternatives_are_interleaved_with_other_pending_nets(self):
        from pcb.net_routing import runner

        self.assertTrue(hasattr(runner, "_defer_repair_alternatives"))
        queue = [[{"pending": index}] for index in range(6)]
        candidates = [{**request(), "candidate": index, "recovery_stage": 4}
                      for index in range(3)]
        runner._defer_repair_alternatives(queue, candidates, defer_rounds=2)
        self.assertEqual(
            [(item[0].get("pending"), item[0].get("candidate")) for item in queue],
            [(0, None), (1, None), (None, 0), (2, None), (3, None),
             (None, 1), (4, None), (5, None), (None, 2)],
        )

    def test_repair_deferral_counts_distinct_other_targets(self):
        from pcb.net_routing import runner

        def pending(name):
            return [{"nets": [{"name": name}]}]

        queue = [pending(name) for name in ("A", "A", "B", "B", "C")]
        alternative = {**request(), "nets": [{"name": "A"}], "candidate": "repair"}
        runner._defer_repair_alternatives(queue, [alternative], defer_rounds=2)
        self.assertEqual(
            [(batch[0]["nets"][0]["name"], batch[0].get("candidate")) for batch in queue],
            [("A", None), ("A", None), ("B", None), ("B", None),
             ("C", None), ("A", "repair")],
        )

    def test_escalation_waits_for_distinct_other_targets(self):
        queue = [[{"nets": [{"name": name}]}] for name in ("A", "A", "B", "B", "C")]
        current = {**request(), "nets": [{"name": "A"}]}
        result = {"attempts": [{"checkpoints": [{"net": "A", "count": 1}]}]}
        _split_failed(queue, [current], {"A": 2}, result)
        self.assertEqual(
            [(batch[0]["nets"][0]["name"], batch[0].get("recovery_stage")) for batch in queue],
            [("A", None), ("A", None), ("B", None), ("B", None), ("C", None), ("A", 1)],
        )

    def test_board_change_recompiles_pending_repairs_without_old_geometry(self):
        from pcb.net_routing import runner

        self.assertTrue(hasattr(runner, "_invalidate_compiled_repairs"))
        first = {**copy.deepcopy(request()), "recovery_stage": 4,
                 "nets": [{"name": "SIGNAL"}, {"name": "GND"}],
                 "rip_up": {"GND": [{"kind": "segment", "uuid": "old"}]},
                 "movable": [{"reference": "R1", "positions_mm": [[2, 3]]}],
                 "recovery_preference": {"neighbors": ["GND"], "moving": ["R1"]},
                 "orders": [["SIGNAL", "GND"]]}
        second = copy.deepcopy(first)
        other = {**copy.deepcopy(first), "nets": [{"name": "OTHER"}, {"name": "GND"}]}
        queue = [[first], [{"pending": 1}], [second], [other]]
        runner._invalidate_compiled_repairs(queue)
        self.assertEqual(len(queue), 3)
        self.assertEqual(queue[0][0]["nets"], [{"name": "SIGNAL"}])
        self.assertEqual(queue[2][0]["nets"], [{"name": "OTHER"}])
        for batch in (queue[0], queue[2]):
            self.assertNotIn("rip_up", batch[0])
            self.assertNotIn("movable", batch[0])
            self.assertNotIn("recovery_preference", batch[0])
            self.assertNotIn("orders", batch[0])
            self.assertEqual(batch[0]["recovery_stage"], 4)
            self.assertEqual(batch[0]["shared"]["clearance_mm"], 0.2)
        self.assertIn("rip_up", first)

    def test_search_identity_is_stable_but_changes_with_budget_or_recovery(self):
        original = request()
        self.assertEqual(search_key(original), search_key(copy.deepcopy(original)))
        self.assertNotEqual(search_key(original), search_key(next_stage(original, RecoveryPolicy())))
        changed = copy.deepcopy(original)
        changed["search"]["seconds"] = 60
        self.assertNotEqual(search_key(original), search_key(changed))

    def test_wider_recovery_finds_a_detour_outside_the_initial_local_window(self):
        anchors = (fixtures.site(2, 7), fixtures.site(8, 7))
        blocked = Obstacle(Point.from_mm([4, 0]), Point.from_mm([5, 14]), ("F.Cu",), "FOREIGN")

        def solve(margin):
            window, step = search_window(anchors, (0., 0., 20., 20.), margin, 1., 1, 10000)
            router = GridRouter(Point.from_mm(window[:2]), Point.from_mm(window[2:]),
                                mm(step), ("F.Cu",), "SIGNAL", mm(.2), mm(.2),
                                mm(.6), mm(.3), (blocked,), max_expansions=100000,
                                forbid_staircase=True, gentle_turns=True)
            return router.route(anchors[0].point, "F.Cu", anchors[1].point, "F.Cu")

        with self.assertRaises(NoGridRoute):
            solve(4)
        recovered = solve(next_stage(request(), RecoveryPolicy())["checkpoint_margin_mm"])
        self.assertTrue(recovered.segments)
        self.assertGreater(max(max(s.start.y, s.end.y) for s in recovered.segments), mm(14))

    def test_endpoint_headings_exclude_right_angle_end_joins_but_allow_branches(self):
        net = Net("SIGNAL", (Island("a", (fixtures.site(4, 3),)), Island("b", (fixtures.site(8, 5),))),
                  existing=(fixtures.segment((1, 3), (4, 3)),))
        backend = fixtures.synthetic_backend(net, Budget(Limits(seconds=3, smoothing_trials=0)))
        empty = fixtures.route()
        directions = backend.endpoint_headings(net, empty, fixtures.site(4, 3), arriving=False)
        self.assertNotIn(2, directions)
        self.assertNotIn(6, directions)
        self.assertIn(0, directions)
        self.assertIsNone(backend.endpoint_headings(net, empty, fixtures.site(2, 3), arriving=False))
        router = backend.settings[net.name].router(())
        try:
            backend._path(router, net, empty, fixtures.site(4, 3), fixtures.site(8, 5))
        finally:
            self.assertIsNone(router.initial_directions)
            self.assertIsNone(router.final_directions)

    def test_legacy_off_angle_copper_is_a_recoverable_endpoint_blocker(self):
        from pcb.net_routing.geometry import RouteGeometryError

        old = fixtures.segment((1, 1), (4, 2))
        end = fixtures.site(4, 2)
        net = Net(
            "SIGNAL",
            (Island("a", (end,)), Island("b", (fixtures.site(8, 5),))),
            existing=(old,),
        )
        backend = fixtures.synthetic_backend(net, Budget(Limits(seconds=3)))
        try:
            backend.endpoint_headings(net, fixtures.route(), end, arriving=False)
        except (ValueError, NoGridRoute) as error:
            self.assertIsInstance(error, NoGridRoute)
            self.assertIn("off-angle", str(error))
        else:
            self.fail("A legacy off-angle copper end cannot satisfy the bend rule")

        new = fixtures.segment((4, 2), (5, 2))
        try:
            fixtures.combine(fixtures.route(new), existing=(old,))
        except ValueError as error:
            self.assertIsInstance(error, RouteGeometryError)
            self.assertIn("off-angle", str(error))
        else:
            self.fail("A new route must not create a bend on off-angle copper")

        # A pad is a terminal rather than a two-segment copper bend.
        pad_net = Net("SIGNAL", net.islands, existing=(old,), pad_sites=frozenset({end}))
        self.assertIsNone(backend.endpoint_headings(pad_net, fixtures.route(), end, arriving=False))
        combined = fixtures.combine(fixtures.route(new), existing=(old,), pad_sites=frozenset({end}))
        self.assertTrue(combined.segments)

    def test_failure_details_are_bounded_and_survive_the_round_summary(self):
        budget = Budget(Limits())
        for index in range(20):
            budget.failure("combined_geometry", f"bend {index}")
        report = budget.report()
        self.assertEqual(report["failures"]["combined_geometry"], 20)
        self.assertEqual(len(report["failure_details"]["combined_geometry"]), 3)
        summary = summarize_attempts({"attempts": [{"search": report}]})
        self.assertEqual(summary[0]["failure_details"], report["failure_details"])

    def test_terminal_commands_remain_one_copyable_line(self):
        stream = io.StringIO()
        command = "python run.py pcb autoroute run --resume " + "a" * 32 + " --memory-gib 3"
        Console(stream=stream, width=36).command(command)
        self.assertEqual(stream.getvalue(), command + "\n")
