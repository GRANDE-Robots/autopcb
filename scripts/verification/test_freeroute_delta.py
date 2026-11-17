"""Check that external-router proposals do not replace saved copper."""

import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pcbnew

from pcb.freeroute_delta import (
    Trace,
    Via,
    Delta,
    additive_delta,
    chain_endpoints,
    chamfer_right_angles,
    connected_trace_component,
    dangling_only_rejections,
    dangling_trace_remainder,
    deferred_class,
    checked_improvement,
    drc_rejections,
    endpoint_directions,
    initial_heading,
    proposed_component,
    restore_raw_nets,
    router_class,
    simplify_terminal_chains,
    style_rejections,
    without_nets,
)
from pcb.net_routing.style import Staircase, StyleReport


class FreerouteDeltaTests(unittest.TestCase):
    def test_only_new_copper_fully_covered_by_a_same_net_convex_smd_pad_is_removed(self):
        import tempfile
        from pcb.freeroute_delta import without_in_pad_segments
        from verification.test_route_optimizer import RouteOptimizerTests
        with tempfile.TemporaryDirectory() as folder:
            board = pcbnew.LoadBoard(str(RouteOptimizerTests.make_board(Path(folder))))
            pad = next(iter(board.FindFootprintByReference('J1').Pads()))
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            internal = (Trace('SIGNAL', 'F.Cu', 200000, (2000000, 2000000), (2100000, 2000000)),
                        Trace('SIGNAL', 'F.Cu', 200000, (2100000, 2000000), (2200000, 2100000)))
            outgoing = Trace('SIGNAL', 'F.Cu', 200000, (2200000, 2100000), (3000000, 2900000))
            foreign = Trace('OTHER', 'F.Cu', 200000, (2000000, 2000000), (2100000, 2000000))
            back = Trace('SIGNAL', 'B.Cu', 200000, (2000000, 2000000), (2100000, 2000000))
            delta = Delta((*internal, outgoing, foreign, back), (), (), 0)
            result = without_in_pad_segments(delta, board)
            self.assertEqual(result.traces, (outgoing, foreign, back))
            pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
            corner = Trace('SIGNAL', 'F.Cu', 100000, (2400000, 2400000), (2450000, 2400000))
            self.assertEqual(without_in_pad_segments(Delta((corner,), (), (), 0), board).traces, (corner,))

    def test_unattributed_native_violation_rejects_candidate_without_crashing_or_returning_a_board(self):
        import copy
        import tempfile
        from pcb.freeroute_delta import filter_proposal
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory(prefix="autopcb-native-rejection-") as folder:
            project = Path(folder)
            source = RouteOptimizerTests.make_board(project)
            (project / "v4.1.kicad_sch").write_text("(kicad_sch (lib_symbols))", encoding="utf-8")
            (project / "v4.1.kicad_pro").write_text('{"net_settings": {}}', encoding="utf-8")
            (project / "v4.1.kicad_dru").write_text("(version 1)", encoding="utf-8")
            originals = {p.name: p.read_bytes() for p in project.iterdir() if p.is_file()}
            board = pcbnew.LoadBoard(str(source))
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(2_000_000, 2_000_000))
            track.SetEnd(pcbnew.VECTOR2I(3_000_000, 2_000_000))
            track.SetWidth(200_000)
            track.SetLayer(pcbnew.F_Cu)
            track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            board.Add(track)
            routed = project / "candidate.kicad_pcb"
            pcbnew.SaveBoard(str(routed), board)
            baseline = {"violations": [], "unconnected_items": [], "schematic_parity": [],
                        "included_severities": ["error", "warning", "exclusion"], "ignored_checks": []}
            report = copy.deepcopy(baseline)
            report["violations"] = [{
                "type": "connection_width", "severity": "error",
                "description": "Copper connection too narrow: minimum 0.20 mm, actual 0.09 mm",
                "items": [{"uuid": "existing-zone", "description": "Zone [GND]"}],
            }]
            with patch("pcb.route_validation._drc", side_effect=(baseline, report)):
                try:
                    result = filter_proposal(project, source, routed)
                except RuntimeError as error:
                    self.fail(f"An identified native candidate violation must be a rejected result: {error}")
            self.assertEqual(result["status"], "candidate_rejected")
            self.assertIn("Copper connection too narrow", result["reason"])
            self.assertNotIn("board", result)
            for name, original in originals.items():
                self.assertEqual((project / name).read_bytes(), original)

    def test_shortcuts_can_cross_width_transitions_only_with_saved_rule_materialization(self):
        from pcb.freeroute_delta import materialize_saved_widths, _segment
        from verification.test_autoroute_rules import compiler
        rules = compiler()
        points = ((1_500_000, 1_500_000), (2_000_000, 2_000_000),
                  (2_500_000, 2_000_000), (3_000_000, 2_500_000),
                  (4_000_000, 2_500_000), (4_500_000, 3_000_000))
        path = tuple(Trace("S", "F.Cu", width, a, b)
                     for width, a, b in zip((150_000, 200_000, 150_000, 250_000, 250_000),
                                             points, points[1:]))
        def profile(item):
            return rules.profile(item.net, item.width, 0)
        def materialize(items):
            result, _, blocked = materialize_saved_widths(Delta(tuple(items), (), (), 0), profile)
            self.assertFalse(blocked)
            return result.traces
        def allowed(items):
            return all(item.width >= profile(item).width_at(_segment(item)) for item in items)
        self.assertEqual(simplify_terminal_chains(path, [], lambda *_: False), tuple(sorted(path)))
        result = simplify_terminal_chains(path, [], lambda *_: False,
                                          shortcut_allowed=allowed, materialize=materialize)
        self.assertLess(len(result), len(path))
        self.assertEqual(set(chain_endpoints(result, variable_width=True)), {points[0], points[-1]})
        self.assertTrue(allowed(result))
        anchor = points[2]
        self.assertEqual(simplify_terminal_chains(path, [],
                         lambda _net, _layer, p: p == anchor,
                         shortcut_allowed=allowed, materialize=materialize), tuple(sorted(path)))
        self.assertEqual(simplify_terminal_chains(path, [], lambda *_: False,
                         materialize=lambda _items: (Trace("FOREIGN", "F.Cu", 250_000,
                                                          points[0], points[-1]),)), tuple(sorted(path)))

    def test_rule_width_materialization_restores_trunks_and_keeps_legitimate_necks(self):
        from pcb.freeroute_delta import materialize_saved_widths, _segment
        from verification.test_autoroute_rules import compiler, RULES
        source = RULES + '''
          (rule "local power neck" (condition "A.NetClass == 'POWER' && A.intersectsCourtyard('U1')")
            (constraint track_width (min 0.30mm)))'''
        rules = compiler(source)
        inside = Trace("P", "F.Cu", 300_000, (1_500_000, 2_000_000), (2_500_000, 2_000_000))
        trunk = Trace("P", "F.Cu", 375_000, (2_000_000, 2_000_000), (5_000_000, 2_000_000))
        logic = Trace("S", "F.Cu", 150_000, (1_500_000, 1_500_000), (2_500_000, 1_500_000))
        via = Via("P", (5_000_000, 2_000_000), 800_000, 400_000)
        original = Delta((inside, trunk, logic), (via,), ("PROTECTED",), 0)
        def profile(item):
            return rules.profile(item.net, item.width, 0)
        result, changed, blocked = materialize_saved_widths(original, profile)
        self.assertEqual(blocked, {})
        self.assertGreater(changed, 0)
        self.assertIn(inside, result.traces)
        self.assertIn(logic, result.traces)
        self.assertTrue(any(item.net == "P" and item.width == 500_000 for item in result.traces))
        self.assertTrue(all(item.width >= profile(item).width_at(_segment(item)) for item in result.traces))
        self.assertTrue(all(item.width >= 375_000 for item in result.traces
                            if item.net == "P" and item != inside))
        self.assertEqual(result.vias, (via,))
        self.assertEqual(result.excluded_nets, ("PROTECTED",))
        self.assertEqual(original.traces, (inside, trunk, logic))

    def test_width_materialization_does_not_hide_unsupported_rules_or_exceed_budget(self):
        from pcb.freeroute_delta import materialize_saved_widths
        from verification.test_autoroute_rules import compiler
        item = Trace("S", "F.Cu", 200_000, (4_000_000, 2_000_000), (5_000_000, 2_000_000))
        delta = Delta((item,), (), (), 0)
        def unsupported(_item):
            raise ValueError("Unsupported saved rule")
        result, changed, blocked = materialize_saved_widths(delta, unsupported)
        self.assertEqual(result, delta)
        self.assertEqual(changed, 0)
        self.assertIn("Unsupported", blocked["S"])
        rules = compiler()
        second = Trace("S", "F.Cu", 200_000, (6_000_000, 2_000_000), (7_000_000, 2_000_000))
        with self.assertRaisesRegex(ValueError, "segment limit"):
            materialize_saved_widths(Delta((item, second), (), (), 0),
                                    lambda t: rules.profile(t.net, t.width, 0), max_traces=1)

    def test_relocated_width_warning_needs_complete_same_net_proof_and_matching_count(self):
        from pcb.route_validation import added_violation_keys
        old_key = ("connection_width", "warning", ("a", "b"))
        new_key = ("connection_width", "warning", ("c", "d"))
        old, new = Counter({old_key: 1}), Counter({new_key: 1})
        proof = dict.fromkeys(("a", "b", "c", "d"), "SAVED")
        self.assertEqual(added_violation_keys(old, new, proof), {})
        self.assertEqual(added_violation_keys(old, new), new)
        self.assertEqual(added_violation_keys(old, new, {**proof, "d": "CHANGED"}), new)
        self.assertEqual(added_violation_keys(old, Counter({new_key: 2}), proof), new)
        error = ("connection_width", "error", ("c", "d"))
        self.assertEqual(added_violation_keys(old, Counter({error: 1}), proof), Counter({error: 1}))
        self.assertEqual(old, Counter({old_key: 1}))
        self.assertEqual(new, Counter({new_key: 1}))

    def test_off_angle_recovery_preserves_endpoints_and_tries_both_exact_paths(self):
        from pcb.freeroute_delta import octilinear_alternatives, recover_off_angle_additions

        for end in ((4_000_000, 1_000_000), (4_000_000, -1_000_000),
                    (1_000_000, 4_000_000), (1_000_000, -4_000_000)):
            original = Trace("SIG", "In1.Cu", 250_000, (0, 0), end)
            options = octilinear_alternatives(original)
            self.assertEqual(len(options), 2)
            for path in options:
                self.assertEqual(len(path), 2)
                self.assertEqual(chain_endpoints(path), (original.start, original.end))
                self.assertTrue(all(x.net == original.net and x.layer == original.layer
                                    and x.width == original.width for x in path))
                for item in path:
                    dx = abs(item.end[0] - item.start[0])
                    dy = abs(item.end[1] - item.start[1])
                    self.assertTrue((dx or dy) and (not dx or not dy or dx == dy))
            calls = []
            def clear(path):
                calls.append(path)
                return len(calls) == 2
            amended, repaired = recover_off_angle_additions([], [original], set(), clear)
            self.assertEqual(repaired, 1)
            self.assertEqual(set(amended), set(options[1]))
            self.assertEqual(len(calls), 2)
            delta = additive_delta([], [], amended)
            self.assertEqual(delta.unsupported, 0)
            self.assertEqual(set(delta.traces), set(options[1]))

    def test_off_angle_recovery_never_rewrites_existing_or_excluded_copper(self):
        from pcb.freeroute_delta import recover_off_angle_additions

        old = Trace("SAVED", "F.Cu", 200_000, (0, 0), (2_000_000, 500_000))
        denied = Trace("PROTECTED", "B.Cu", 200_000, (0, 0), (2_000_000, 500_000))
        blocked = Trace("SIG", "B.Cu", 200_000, (0, 0), (2_000_000, 500_000))
        original = [old, denied, blocked]
        result, count = recover_off_angle_additions([old], original, {"PROTECTED"}, lambda _: False)
        self.assertEqual(result, original)
        self.assertEqual(count, 0)
        result, count = recover_off_angle_additions([old], original, {"PROTECTED"}, lambda _: True,
                                                    may_continue=lambda: False)
        self.assertEqual(result, original)
        self.assertEqual(count, 0)
        self.assertEqual(original, [old, denied, blocked])
        result, count = recover_off_angle_additions([], [blocked, blocked], frozenset(), lambda _: True)
        self.assertEqual(result, [blocked, blocked])
        self.assertEqual(count, 0)

    def test_off_angle_recovery_checks_both_paths_against_obstacles(self):
        from pcb.freeroute_delta import recover_off_angle_additions, shortcut_clearance_checker
        from pcb.route_optimizer import Obstacle, Point

        trace = Trace("SIG", "F.Cu", 200_000, (0, 0), (4_000_000, 1_000_000))
        bottom = Obstacle(Point(2_000_000, -100_000), Point(2_200_000, 100_000),
                          ("F.Cu",), "OTHER")
        top = Obstacle(Point(2_000_000, 900_000), Point(2_200_000, 1_100_000),
                       ("F.Cu",), "OTHER")
        result, count = recover_off_angle_additions([], [trace], set(), shortcut_clearance_checker((bottom,)))
        self.assertEqual(count, 1)
        self.assertTrue(any(item.start == (1_000_000, 1_000_000) for item in result))
        result, count = recover_off_angle_additions([], [trace], set(), shortcut_clearance_checker((bottom, top)))
        self.assertEqual(result, [trace])
        self.assertEqual(count, 0)

    def test_new_native_findings_are_attributed_only_to_proposed_nets(self):
        from pcb.freeroute_delta import newly_implicated_nets, retryable_native_rejection

        old = {"type": "track_width", "severity": "error", "items": [{"uuid": "old"}]}
        warning = {"type": "connection_width", "severity": "warning",
                   "items": [{"uuid": "prior"}, {"uuid": "added"}]}
        unrelated = {"type": "clearance", "severity": "error", "items": [{"uuid": "other"}]}
        self.assertEqual(newly_implicated_nets(
            {"violations": [old]}, {"violations": [old, warning, unrelated]},
            {}, {}, {"prior": "SIG", "added": "SIG", "other": "SAVED"}, {"SIG"}
        ), ("SIG",))
        self.assertEqual(newly_implicated_nets(
            {"violations": [old]}, {"violations": [old, warning]},
            {}, {}, {"prior": "SAVED", "added": "SAVED"}, {"SIG"}
        ), ())
        self.assertTrue(retryable_native_rejection(RuntimeError("Resumed candidate introduces native violations")))
        self.assertTrue(retryable_native_rejection(
            RuntimeError("Resumed candidate introduces native violations: 1 warning connection_width")))
        self.assertFalse(retryable_native_rejection(RuntimeError("PCB source changed during validation")))
        self.assertFalse(retryable_native_rejection(RuntimeError("Incomplete native error evidence")))

    def test_ambiguous_geometry_retry_requires_every_blocker_to_match_a_proposed_net(self):
        from pcb.freeroute_delta import attributable_style_blocker_nets

        finding = "New ambiguous geometry: SIG on F.Cu: overlapping collinear tracks"
        self.assertEqual(attributable_style_blocker_nets((finding,), {"SIG", "OTHER"}), ("SIG",))
        self.assertIsNone(attributable_style_blocker_nets((finding,), {"OTHER"}))
        self.assertIsNone(attributable_style_blocker_nets(
            (finding, "New invalid_bends has no identifiable proposed item"), {"SIG"}
        ))
        self.assertIsNone(attributable_style_blocker_nets((), {"SIG"}))

    def test_local_repair_checks_staircases_against_existing_copper(self):
        from pcb.freeroute_delta import local_context_checker
        from pcb.route_optimizer import Point, Route, Segment

        old = (Trace("SIG", "F.Cu", 200_000, (0, 0), (1_000_000, 1_000_000)),
               Trace("SIG", "F.Cu", 200_000, (1_000_000, 1_000_000), (2_000_000, 1_000_000)))
        check = local_context_checker("SIG", old, lambda *_: False)
        def candidate(end):
            segment = Segment(Point(2_000_000, 1_000_000), Point(*end), "F.Cu")
            return Route((segment,), (), (), 1, 0, 0, 0)
        self.assertFalse(check(candidate((3_000_000, 2_000_000))))
        self.assertTrue(check(candidate((3_000_000, 0))))

    def test_collinear_proposal_cleanup_preserves_anchors_and_branches(self):
        from pcb.freeroute_delta import merge_proposed_collinear

        first = Trace("SIG", "F.Cu", 200_000, (0, 0), (1_000_000, 0))
        second = Trace("SIG", "F.Cu", 200_000, (1_000_000, 0), (2_000_000, 0))
        expected = (Trace("SIG", "F.Cu", 200_000, (0, 0), (2_000_000, 0)),)
        self.assertEqual(merge_proposed_collinear((first, second), [], lambda *_: False), expected)
        self.assertEqual(merge_proposed_collinear((first, second), [], lambda *_: True), (first, second))
        branch = Trace("SIG", "F.Cu", 200_000, (1_000_000, -1_000_000), (1_000_000, 1_000_000))
        self.assertEqual(set(merge_proposed_collinear((first, second, branch), [], lambda *_: False)),
                         {first, second, branch})
        self.assertEqual(set(merge_proposed_collinear((first, second), [branch], lambda *_: False)),
                         {first, second})
        wider = Trace("SIG", "F.Cu", 300_000, (1_000_000, 0), (2_000_000, 0))
        self.assertEqual(set(merge_proposed_collinear((first, wider), [], lambda *_: False)), {first, wider})

    def test_repair_audit_tracks_the_merged_replacement(self):
        from pcb.freeroute_delta import normalize_repaired_trial, replacement_style_clear

        earlier = Trace("SIG", "F.Cu", 200_000, (0, 0), (1_000_000, 0))
        rejected = Trace("SIG", "F.Cu", 200_000, (1_000_000, 0), (2_000_000, 1_000_000))
        replacement = Trace("SIG", "F.Cu", 200_000, (1_000_000, 0), (2_000_000, 0))
        prior = Delta((earlier, rejected), (), (), 0)
        trial = Delta((earlier, replacement), (), (), 0)
        normalized, checked = normalize_repaired_trial(trial, prior, (replacement,), [], lambda *_: False)
        merged = Trace("SIG", "F.Cu", 200_000, (0, 0), (2_000_000, 0))
        self.assertEqual(normalized.traces, (merged,))
        self.assertEqual(checked, (merged,))
        self.assertFalse(replacement_style_clear({"bad"}, {"bad": merged}, checked, ()))

    def test_repair_order_restarts_once_only_after_a_partial_repair(self):
        from pcb.freeroute_delta import RepairOrdering

        long = Trace("N", "F.Cu", 200_000, (0, 0), (4_000_000, 0))
        short = Trace("N", "F.Cu", 200_000, (5_000_000, 0), (6_000_000, 0))
        initial = Delta((long, short), (), (), 0)
        ordering = RepairOrdering(initial)
        self.assertEqual(ordering.seeds({long, short}), [long, short])
        self.assertIsNone(ordering.restart(may_continue=True))
        ordering.record_repair()
        self.assertIsNone(ordering.restart(may_continue=False))
        self.assertEqual(ordering.restart(may_continue=True), initial)
        self.assertEqual(ordering.seeds({long, short}), [short, long])
        self.assertIsNone(ordering.restart(may_continue=True))

    def test_failed_local_repair_is_retried_after_proposal_changes(self):
        from pcb.freeroute_delta import RepairAttemptMemory

        first = Trace("N", "F.Cu", 200_000, (0, 0), (1_000_000, 0))
        second = Trace("N", "F.Cu", 200_000, (1_000_000, 0), (2_000_000, 0))
        via = Via("N", (2_000_000, 1_000_000), 600_000, 300_000)
        original = Delta((first,), (), (), 0)
        identical = Delta((first,), (), (), 0)
        changed_tracks = Delta((first, second), (), (), 0)
        changed_vias = Delta((first, second), (via,), (), 0)
        ends = ((0, 0), (1_000_000, 0))
        attempts = RepairAttemptMemory()
        self.assertTrue(attempts.claim(original, "N", "F.Cu", ends))
        self.assertFalse(attempts.claim(identical, "N", "F.Cu", ends))
        self.assertTrue(attempts.claim(changed_tracks, "N", "F.Cu", ends))
        self.assertFalse(attempts.claim(changed_tracks, "N", "F.Cu", ends))
        self.assertTrue(attempts.claim(changed_vias, "N", "F.Cu", ends))

    def test_temporary_disconnect_is_only_deferred_with_explicit_opt_in(self):
        from pcb.freeroute_delta import check_intermediate_connectivity

        reason = "Resumed candidate broke an existing copper connection"
        with patch("pcb.net_routing.runner._verify_resume_connectivity", side_effect=RuntimeError(reason)):
            with self.assertRaisesRegex(RuntimeError, "broke an existing"):
                check_intermediate_connectivity(Path("."), b"board")
            self.assertFalse(check_intermediate_connectivity(Path("."), b"board", allow_temporary_disconnect=True))
        with patch("pcb.net_routing.runner._verify_resume_connectivity", side_effect=RuntimeError("Source changed")):
            with self.assertRaisesRegex(RuntimeError, "Source changed"):
                check_intermediate_connectivity(Path("."), b"board", allow_temporary_disconnect=True)
        with patch("pcb.net_routing.runner._verify_resume_connectivity"):
            self.assertTrue(check_intermediate_connectivity(Path("."), b"board", allow_temporary_disconnect=True))
        with self.assertRaises(ValueError):
            check_intermediate_connectivity(Path("."), b"board", allow_temporary_disconnect=1)

    def test_missing_connections_are_counted_only_with_unambiguous_net_identity(self):
        from pcb.freeroute_delta import unconnected_by_net

        report = {"unconnected_items": [
            {"items": [{"uuid": "a"}, {"uuid": "b"}]},
            {"items": [{"uuid": "a"}, {"uuid": "missing"}]},
            {"items": [{"uuid": "a"}, {"uuid": "c"}]},
        ]}
        self.assertEqual(unconnected_by_net(report, {"a": "A", "b": "A", "c": "B"}),
                         {"A": 2, "<unknown>": 1})

    def test_new_islands_are_removed_without_discarding_other_nets(self):
        from pcb.freeroute_delta import regressed_open_nets

        baseline = {"A": 1, "B": 1}
        candidate = {"B": 2}
        self.assertEqual(regressed_open_nets(baseline, candidate, {"A", "B"}), ("B",))
        with self.assertRaisesRegex(ValueError, "unidentified"):
            regressed_open_nets(baseline, {"<unknown>": 1}, {"A"})

    def test_unproductive_pruned_nets_are_deferred_without_deferring_improvements(self):
        from pcb.freeroute_delta import unproductive_pruned_nets

        before = {"A": 1, "B": 2}
        after = {"A": 1, "B": 1, "C": 1}
        self.assertEqual(unproductive_pruned_nets(before, after, {"A", "B", "C"}), {"A"})
        self.assertEqual(unproductive_pruned_nets(before, {"<unknown>": 1}, {"A"}), set())

    def test_new_copper_can_align_with_one_nanometre_via_rounding(self):
        from pcb.freeroute_delta import align_proposal_to_existing_vias

        track = Trace("N", "In1.Cu", 200_000, (1_000_001, 1_000_000), (2_000_001, 2_000_000))
        added_via = Via("N", (2_000_001, 2_000_000), 600_000, 300_000)
        original = [Via("N", (1_000_000, 1_000_000), 600_000, 300_000)]
        aligned, offsets = align_proposal_to_existing_vias(Delta((track,), (added_via,), (), 0), original)
        self.assertEqual(offsets, {"N": (-1, 0)})
        self.assertEqual(aligned.traces[0].start, (1_000_000, 1_000_000))
        self.assertEqual(aligned.traces[0].end, (2_000_000, 2_000_000))
        self.assertEqual(aligned.vias[0].at, (2_000_000, 2_000_000))
        self.assertEqual(original[0].at, (1_000_000, 1_000_000))

    def test_via_alignment_does_not_shift_conflicting_or_exact_anchors(self):
        from pcb.freeroute_delta import align_proposal_to_existing_vias

        track = Trace("N", "B.Cu", 200_000, (1_000_001, 1_000_000), (2_000_001, 1_000_000))
        delta = Delta((track,), (), (), 0)
        original = [Via("N", (1_000_000, 1_000_000), 600_000, 300_000),
                    Via("N", (2_000_001, 1_000_000), 600_000, 300_000)]
        self.assertEqual(align_proposal_to_existing_vias(delta, original), (delta, {}))

    def test_via_escape_snap_keeps_both_segments_octilinear(self):
        from pcb.freeroute_delta import snap_proposal_via_escapes

        first = Trace("N", "F.Cu", 200_000, (0, 0), (1_000_000, 0))
        diagonal = Trace("N", "F.Cu", 200_000, (1_000_000, 0), (2_000_000, 1_000_000))
        via = Via("N", (2_000_000, 1_000_100), 600_000, 300_000)
        delta = Delta((first, diagonal), (), (), 0)
        snapped, nets = snap_proposal_via_escapes(delta, [via], lambda *_: False)
        self.assertEqual(nets, {"N"})
        self.assertEqual(set(snapped.traces), {
            Trace("N", "F.Cu", 200_000, (0, 0), (999_900, 0)),
            Trace("N", "F.Cu", 200_000, (999_900, 0), via.at),
        })
        self.assertEqual(delta.traces, (first, diagonal))

    def test_via_escape_snap_refuses_ambiguous_or_pinned_joints(self):
        from pcb.freeroute_delta import snap_proposal_via_escapes

        first = Trace("N", "F.Cu", 200_000, (0, 0), (1_000_000, 0))
        diagonal = Trace("N", "F.Cu", 200_000, (1_000_000, 0), (2_000_000, 1_000_000))
        vias = [Via("N", (2_000_000, 1_000_100), 600_000, 300_000),
                Via("N", (2_000_000, 999_900), 600_000, 300_000)]
        delta = Delta((first, diagonal), (), (), 0)
        self.assertEqual(snap_proposal_via_escapes(delta, vias, lambda *_: False), (delta, set()))
        self.assertEqual(snap_proposal_via_escapes(delta, vias[:1],
                                                   lambda _net, _layer, point: point == (1_000_000, 0)),
                         (delta, set()))
        old_contact = Trace("N", "F.Cu", 200_000, (500_000, 0), (500_000, 1_000_000))
        self.assertEqual(snap_proposal_via_escapes(delta, [vias[0], old_contact], lambda *_: False),
                         (delta, set()))
        branch = Trace("N", "F.Cu", 200_000, (1_000_000, 0), (1_000_000, 1_000_000))
        branched = Delta((first, diagonal, branch), (), (), 0)
        result, aligned = snap_proposal_via_escapes(branched, vias[:1], lambda *_: False)
        self.assertEqual(set(result.traces), set(branched.traces))
        self.assertFalse(aligned)
        off_angle = Trace("N", "F.Cu", 200_000, (1_000_000, 0), (2_000_000, 1_000_001))
        unsupported = Delta((first, off_angle), (), (), 0)
        result, aligned = snap_proposal_via_escapes(unsupported, vias[:1], lambda *_: False)
        self.assertEqual(set(result.traces), set(unsupported.traces))
        self.assertFalse(aligned)

    def test_via_escape_can_center_an_endpoint_inside_the_same_net_via(self):
        from pcb.freeroute_delta import snap_proposal_via_escapes

        first = Trace("N", "F.Cu", 200_000, (0, 0), (1_000_000, 0))
        diagonal = Trace("N", "F.Cu", 200_000, (1_000_000, 0), (2_000_000, 1_000_000))
        via = Via("N", (2_000_000, 1_200_000), 600_000, 300_000)
        delta = Delta((first, diagonal), (), (), 0)
        snapped, nets = snap_proposal_via_escapes(delta, [via], lambda *_: False,
                                                  capture_via_copper=True)
        self.assertEqual(nets, {"N"})
        self.assertEqual(set(snapped.traces), {
            Trace("N", "F.Cu", 200_000, (0, 0), (800_000, 0)),
            Trace("N", "F.Cu", 200_000, (800_000, 0), via.at),
        })
        # Both copper radii matter; a point outside the combined circular
        # envelope must not be snapped even inside its bounding square.
        outside = Via("N", (2_300_000, 1_300_000), 600_000, 300_000)
        self.assertEqual(snap_proposal_via_escapes(delta, [outside], lambda *_: False,
                                                  capture_via_copper=True), (delta, set()))
        another = Via("N", (2_000_000, 800_000), 600_000, 300_000)
        self.assertEqual(snap_proposal_via_escapes(delta, [via, another], lambda *_: False,
                                                  capture_via_copper=True), (delta, set()))
        self.assertEqual(snap_proposal_via_escapes(delta, [via], lambda *_: False,
                                                  capture_via_copper=True,
                                                  shortcut_allowed=lambda _tracks: False), (delta, set()))

    def test_via_capture_includes_the_track_end_cap_outside_the_via_centerline_radius(self):
        from pcb.freeroute_delta import snap_proposal_via_escapes
        via = Via('N', (0, 0), 600_000, 300_000)
        first = Trace('N', 'B.Cu', 200_000, (320_000, 0), (1_320_000, 0))
        last = Trace('N', 'B.Cu', 200_000, (1_320_000, 0), (2_320_000, 1_000_000))
        original = Delta((first, last), (), (), 0)
        result, aligned = snap_proposal_via_escapes(
            original, [via], lambda *_: False, capture_via_copper=True,
            shortcut_allowed=lambda _tracks: True)
        self.assertEqual(aligned, {'N'})
        self.assertEqual(set(result.traces), {
            Trace('N', 'B.Cu', 200_000, via.at, (1_320_000, 0)), last})
        self.assertEqual(result.vias, ())
        self.assertEqual(original.traces, (first, last))

    def test_single_pad_to_via_escape_is_centered_without_losing_the_pad_anchor(self):
        from pcb.freeroute_delta import snap_proposal_via_escapes

        pad = (63_250_000, 51_200_000)
        off_center = (64_050_000, 51_200_000)
        via = Via("N", (64_250_000, 51_400_000), 600_000, 300_000)
        delta = Delta((Trace("N", "B.Cu", 200_000, pad, off_center),), (via,), (), 0)
        result, aligned = snap_proposal_via_escapes(
            delta, [], lambda _net, _layer, point: point == pad,
            capture_via_copper=True, shortcut_allowed=lambda _tracks: True,
            protected_endpoint=lambda _net, _layer, point: point == pad,
        )
        self.assertEqual(aligned, {"N"})
        self.assertEqual(set(result.traces), {
            Trace("N", "B.Cu", 200_000, pad, (63_450_000, 51_400_000)),
            Trace("N", "B.Cu", 200_000, (63_450_000, 51_400_000), via.at),
        })
        self.assertEqual(result.vias, delta.vias)

    def test_new_via_centers_on_existing_trace_without_losing_its_proposed_escape(self):
        from pcb.freeroute_delta import snap_new_vias_to_existing_endpoints

        old = Trace("N", "In2.Cu", 200_000, (62_750_000, 49_900_000), (64_250_000, 51_400_000))
        via = Via("N", (64_500_000, 51_450_000), 600_000, 300_000)
        first = Trace("N", "B.Cu", 200_000, (63_150_000, 51_300_000), (63_300_000, 51_450_000))
        last = Trace("N", "B.Cu", 200_000, (63_300_000, 51_450_000), via.at)
        delta = Delta((first, last), (via,), (), 0)

        def anchors(vias):
            return lambda net, _layer, point: any(
                item.net == net and sum((point[i] - item.at[i]) ** 2 for i in (0, 1)) <= (item.size // 2) ** 2
                for item in vias
            )

        result, changed = snap_new_vias_to_existing_endpoints(
            delta, [old], anchors, lambda *_: True, lambda *_: True, lambda *_: False
        )
        self.assertEqual(changed, {"N"})
        self.assertEqual(result.vias, (Via("N", old.end, via.size, via.drill),))
        self.assertEqual(set(result.traces), {
            Trace("N", "B.Cu", 200_000, first.start, (63_250_000, 51_400_000)),
            Trace("N", "B.Cu", 200_000, (63_250_000, 51_400_000), old.end),
        })

    def test_new_via_relocation_refuses_ambiguous_or_unprotected_contacts(self):
        from pcb.freeroute_delta import snap_new_vias_to_existing_endpoints

        old = Trace("N", "In2.Cu", 200_000, (62_750_000, 49_900_000), (64_250_000, 51_400_000))
        via = Via("N", (64_500_000, 51_450_000), 600_000, 300_000)
        first = Trace("N", "B.Cu", 200_000, (63_150_000, 51_300_000), (63_300_000, 51_450_000))
        last = Trace("N", "B.Cu", 200_000, (63_300_000, 51_450_000), via.at)
        delta = Delta((first, last), (via,), (), 0)
        def anchors(_vias):
            return lambda *_: False

        def call(original, via_safe):
            return snap_new_vias_to_existing_endpoints(
                delta, original, anchors, via_safe, lambda *_: True, lambda *_: False
            )
        self.assertEqual(call([old], lambda *_: False), (delta, set()))
        touching = Trace("N", "F.Cu", 200_000, (64_850_000, 51_000_000), (64_850_000, 51_900_000))
        self.assertEqual(call([old, touching], lambda *_: True), (delta, set()))
        ambiguous = Trace("N", "F.Cu", 200_000, (64_260_000, 51_400_000), (64_260_000, 50_400_000))
        self.assertEqual(call([old, ambiguous], lambda *_: True), (delta, set()))
        other_via = Via("N", (64_800_000, 51_450_000), 600_000, 300_000)
        self.assertEqual(call([old, other_via], lambda *_: True), (delta, set()))

    def test_via_relocation_replaces_an_anchored_chain_with_no_bend_inside_the_via(self):
        from pcb.freeroute_delta import snap_new_vias_to_existing_endpoints

        old = Trace("N", "In2.Cu", 200_000, (62_750_000, 49_900_000), (64_250_000, 51_400_000))
        via = Via("N", (64_500_000, 51_450_000), 600_000, 300_000)
        traces = (
            Trace("N", "B.Cu", 200_000, (63_150_000, 51_300_000), (63_900_000, 51_300_000)),
            Trace("N", "B.Cu", 200_000, (63_900_000, 51_300_000), (64_050_000, 51_450_000)),
            Trace("N", "B.Cu", 200_000, (64_050_000, 51_450_000), via.at),
        )
        delta = Delta(traces, (via,), (), 0)
        pad = (63_150_000, 51_300_000)

        def anchors(vias):
            return lambda net, _layer, point: point == pad or any(
                item.net == net and sum((point[i] - item.at[i]) ** 2 for i in (0, 1)) <= (item.size // 2) ** 2
                for item in vias
            )

        result, changed = snap_new_vias_to_existing_endpoints(
            delta, [old], anchors, lambda *_: True, lambda *_: True,
            lambda _net, _layer, point: point == pad,
        )
        self.assertEqual(changed, {"N"})
        self.assertEqual(result.vias, (Via("N", old.end, via.size, via.drill),))
        self.assertEqual(set(result.traces), {
            Trace("N", "B.Cu", 200_000, pad, (63_250_000, 51_400_000)),
            Trace("N", "B.Cu", 200_000, (63_250_000, 51_400_000), old.end),
        })

    def test_native_via_relocation_rejects_pad_copper_at_either_position(self):
        import pcbnew
        from pcb.freeroute_delta import native_via_move_checker

        board = pcbnew.BOARD()
        part = pcbnew.FOOTPRINT(board)
        pad = pcbnew.PAD(part)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.VECTOR2I(1_000_000, 1_000_000))
        pad.SetPosition(pcbnew.VECTOR2I(2_000_000, 2_000_000))
        pad.SetLayerSet(pcbnew.LSET.FrontMask())
        part.Add(pad)
        board.Add(part)
        allowed = native_via_move_checker(board)
        far = Via("N", (4_000_000, 4_000_000), 600_000, 300_000)
        near = Via("N", (2_700_000, 2_000_000), 600_000, 300_000)
        self.assertFalse(allowed(far, near))
        self.assertFalse(allowed(near, far))
        self.assertTrue(allowed(far, Via("N", (4_200_000, 4_200_000), 600_000, 300_000)))

    def test_single_via_escape_refuses_unprotected_or_conflicting_changes(self):
        from pcb.freeroute_delta import snap_proposal_via_escapes

        pad, end = (0, 0), (800_000, 0)
        via = Via("N", (1_000_000, 200_000), 600_000, 300_000)
        trace = Trace("N", "B.Cu", 200_000, pad, end)
        delta = Delta((trace,), (via,), (), 0)
        kwargs = {"capture_via_copper": True, "shortcut_allowed": lambda _tracks: True,
                  "protected_endpoint": lambda _net, _layer, point: point == pad}
        self.assertEqual(snap_proposal_via_escapes(delta, [], lambda *_: False, **kwargs), (delta, set()))
        self.assertEqual(snap_proposal_via_escapes(delta, [], lambda *_: True, **{
            **kwargs, "protected_endpoint": lambda *_: True,
        }), (delta, set()))
        self.assertEqual(snap_proposal_via_escapes(delta, [], lambda *_: True, **{
            **kwargs, "shortcut_allowed": lambda _tracks: False,
        }), (delta, set()))
        contact = Trace("N", "B.Cu", 200_000, (400_000, 0), (400_000, 300_000))
        self.assertEqual(snap_proposal_via_escapes(delta, [contact], lambda *_: True, **kwargs), (delta, set()))
        intermediate_via = Via("N", (400_000, 0), 400_000, 200_000)
        with_contact = Delta((trace,), (via, intermediate_via), (), 0)
        self.assertEqual(snap_proposal_via_escapes(with_contact, [], lambda *_: True, **kwargs),
                         (with_contact, set()))

    def test_local_repair_can_use_a_faster_search_order_without_weakening_rules(self):
        from pathlib import Path
        from unittest.mock import MagicMock, patch
        from pcb.freeroute_delta import local_chain_repair

        board = MagicMock()
        stack = (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.In3_Cu, pcbnew.In4_Cu, pcbnew.B_Cu)
        board.GetEnabledLayers.return_value.CuStack.return_value = stack
        board.GetLayerName.side_effect = dict(zip(stack, ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"))).__getitem__
        board.FindNet.return_value.GetNetClassName.return_value = "SIGNAL"
        limits = MagicMock()
        limits.GetClearance.return_value = 200_000
        limits.GetViaDiameter.return_value = 600_000
        limits.GetViaDrill.return_value = 300_000
        board.GetAllNetClasses.return_value = {"SIGNAL": limits}
        original = (Trace("N", "B.Cu", 200_000, (0, 0), (1_000_000, 0)),)
        result = {"segments": [{"layer": "B.Cu", "width_mm": 0.2,
                                "from_mm": [0, 0], "to_mm": [1, 0]}], "vias": []}
        def deadline():
            return None
        with patch("pcb.route_cli.plan", return_value=result) as plan:
            local_chain_repair(Path("."), original, [], board, lambda *_: True,
                               max_expansions=100_000, heuristic_weight=2.0,
                               check_budget=deadline)
        config = plan.call_args.args[0]
        self.assertIs(plan.call_args.kwargs["check_budget"], deadline)
        self.assertEqual(config["heuristic_weight"], 2.0)
        self.assertTrue(config["forbid_staircase"])
        self.assertTrue(config["first_feasible"])
        self.assertEqual(set(config["layers"]), {"F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"})
        with patch("pcb.route_cli.plan", return_value=result) as plan:
            local_chain_repair(Path("."), original, [], board, lambda *_: True, same_layer_only=True)
        self.assertEqual(plan.call_args.args[0]["layers"], ["B.Cu"])

    def test_local_repair_searches_around_vias_already_in_the_proposal(self):
        from unittest.mock import MagicMock, patch
        from pcb.freeroute_delta import local_chain_repair

        board = MagicMock()
        stack = (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.In3_Cu, pcbnew.In4_Cu, pcbnew.B_Cu)
        board.GetEnabledLayers.return_value.CuStack.return_value = stack
        board.GetLayerName.side_effect = dict(zip(stack, ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"))).__getitem__
        board.FindNet.return_value.GetNetClassName.return_value = "SIGNAL"
        limits = MagicMock()
        limits.GetClearance.return_value = 200_000
        limits.GetViaDiameter.return_value = 600_000
        limits.GetViaDrill.return_value = 300_000
        board.GetAllNetClasses.return_value = {"SIGNAL": limits}
        board.GetDesignSettings.return_value.m_HoleClearance = 500_000
        board.GetDesignSettings.return_value.m_HoleToHoleMin = 800_000
        chain = (Trace("N", "B.Cu", 200_000, (0, 0), (1_000_000, 0)),)
        proposed = (Via("N", (500_000, 1_000_000), 600_000, 300_000),)
        result = {"segments": [{"layer": "B.Cu", "width_mm": 0.2,
                                "from_mm": [0, 0], "to_mm": [1, 0]}], "vias": []}
        with patch("pcb.route_cli.plan", return_value=result) as plan:
            local_chain_repair(Path("."), chain, [], board, lambda *_: True,
                               proposed_vias=proposed)
        obstacle = plan.call_args.args[0]["obstacles"][0]
        self.assertEqual(obstacle["net"], "N")
        self.assertEqual(obstacle["kind"], "via")
        self.assertEqual(obstacle["min_via_clearance_mm"], .65)
        self.assertEqual(set(obstacle["layers"]), {"F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"})

    def test_local_repair_includes_other_proposed_tracks_as_obstacles(self):
        from unittest.mock import MagicMock, patch
        from pcb.freeroute_delta import local_chain_repair
        from pcb.route_cli import _obstacle

        board = MagicMock()
        board.FindNet.return_value.GetNetClassName.return_value = "SIGNAL"
        limits = MagicMock()
        limits.GetClearance.return_value = 200_000
        limits.GetViaDiameter.return_value = 600_000
        limits.GetViaDrill.return_value = 300_000
        board.GetAllNetClasses.return_value = {"SIGNAL": limits}
        chain = (Trace("N", "B.Cu", 200_000, (0, 0), (1_000_000, 0)),)
        foreign = Trace("OTHER", "B.Cu", 300_000, (0, 500_000), (1_000_000, 500_000))
        result = {"segments": [{"layer": "B.Cu", "width_mm": .2,
                                "from_mm": [0, 0], "to_mm": [1, 0]}], "vias": []}
        with patch("pcb.route_cli.plan", return_value=result) as plan:
            local_chain_repair(Path("."), chain, [], board, lambda *_: True,
                               proposed_traces=(foreign,))
        obstacle = _obstacle(plan.call_args.args[0]["obstacles"][0])
        self.assertEqual(obstacle.net, "OTHER")
        self.assertEqual(obstacle.layers, ("B.Cu",))
        self.assertGreaterEqual(obstacle.copper_radius, 150_000)
        self.assertTrue(callable(plan.call_args.kwargs["candidate_check"]))

    def test_proposed_via_obstacle_matches_native_via_clearances(self):
        from pcb.freeroute_delta import proposed_via_obstacles
        from pcb.route_cli import _obstacle
        from pcb.route_native import _via_obstacle
        from pcb.route_optimizer import COPPER_LAYERS, GridRouter, Point, Weights, mm

        board = pcbnew.BOARD()
        board.SetCopperLayerCount(4)
        board.GetDesignSettings().m_HoleClearance = mm(.5)
        board.GetDesignSettings().m_HoleToHoleMin = mm(.8)
        net = pcbnew.NETINFO_ITEM(board, "N")
        board.Add(net)
        native = pcbnew.PCB_VIA(board)
        native.SetPosition(pcbnew.VECTOR2I(mm(4), mm(4)))
        native.SetWidth(mm(.6))
        native.SetDrill(mm(.3))
        native.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        native.SetNetCode(net.GetNetCode())
        board.Add(native)
        expected = _via_obstacle(board, native, tuple(board.GetLayerName(layer)
                                                    for layer in board.GetEnabledLayers().CuStack()))
        actual = _obstacle(proposed_via_obstacles((Via("N", (mm(4), mm(4)), mm(.6), mm(.3)),), board)[0])
        self.assertEqual(actual, expected)
        native_model = GridRouter(Point(0, 0), Point(mm(8), mm(8)), mm(.1), COPPER_LAYERS,
                                  "N", mm(.2), mm(.2), mm(.6), mm(.3), (expected,), Weights())
        self.assertIn(native_model._index(Point(mm(4), mm(4))), native_model.existing_vias)
        model = GridRouter(Point(0, 0), Point(mm(8), mm(8)), mm(.1), COPPER_LAYERS,
                           "N", mm(.2), mm(.2), mm(.6), mm(.3), (actual,), Weights())
        self.assertIn(model._index(Point(mm(4), mm(2.9))), model.via_blocked)
        self.assertIn(model._index(Point(mm(4), mm(4))), model.existing_vias)

    def test_local_repair_falls_back_to_all_layers_after_no_via_search(self):
        from pcb.freeroute_delta import prefer_no_via_repair
        from pcb.route_optimizer import NoGridRoute, SearchBudgetExceeded, SearchLimitReached

        for failure in (NoGridRoute("no route", expanded=0), SearchLimitReached("limited", expanded=0)):
            calls = []
            def search(same_layer):
                calls.append(same_layer)
                if same_layer:
                    raise failure
                return ("all-layer candidate", ())
            self.assertEqual(prefer_no_via_repair(search), ("all-layer candidate", ()))
            self.assertEqual(calls, [True, False])
            with self.assertRaises(SearchBudgetExceeded):
                prefer_no_via_repair(search, may_retry=lambda: False)
        calls = []
        self.assertEqual(prefer_no_via_repair(lambda same: calls.append(same) or ("same layer", ())),
                         ("same layer", ()))
        self.assertEqual(calls, [True])

    def test_new_repair_attempts_stop_at_the_cleanup_deadline(self):
        from pcb.freeroute_delta import repair_budget_available

        self.assertTrue(repair_budget_available(59.9, 60.0))
        self.assertFalse(repair_budget_available(60.0, 60.0))
        self.assertTrue(repair_budget_available(1000.0, None))

    def test_reference_regions_block_crossing_tracks_and_through_vias_only_locally(self):
        from pcb.freeroute_delta import Delta, Trace, Via, screen_reference_regions
        from pcb.route_optimizer import Obstacle, Point

        region = Obstacle(Point(900_000, 900_000), Point(1_100_000, 1_100_000),
                          ("In1.Cu",), "", "keepout")
        blocked_track = Trace("TRACK", "In1.Cu", 200_000, (0, 1_000_000), (2_000_000, 1_000_000))
        blocked_via = Via("VIA", (1_000_000, 1_000_000), 600_000, 300_000)
        safe_other_layer = Trace("SAFE", "B.Cu", 200_000, (0, 1_000_000), (2_000_000, 1_000_000))
        safe_remote = Trace("REMOTE", "In1.Cu", 200_000, (0, 3_000_000), (2_000_000, 3_000_000))
        delta = Delta((blocked_track, safe_other_layer, safe_remote), (blocked_via,), (), 0)
        screened, blocked = screen_reference_regions(delta, (region,))
        self.assertEqual(blocked, ("TRACK", "VIA"))
        self.assertEqual(set(screened.traces), {safe_other_layer, safe_remote})
        self.assertEqual(screened.vias, ())

    def test_failed_proposal_is_discarded_and_improvements_still_require_native_checks(self):
        baseline = {"open_connections": 217, "ignored_checks": [],
                    "violation_keys": [], "parity_keys": []}
        worse = {**baseline, "open_connections": 218}
        self.assertFalse(checked_improvement(baseline, worse))
        self.assertFalse(checked_improvement(baseline, dict(baseline)))
        better = {**baseline, "open_connections": 216}
        self.assertTrue(checked_improvement(baseline, better))
        better["violation_keys"] = [("shorting_items", "error", ("new",))]
        with self.assertRaisesRegex(RuntimeError, "violations"):
            checked_improvement(baseline, better)

    def test_terminal_rejections_defer_specific_nets_and_keep_prior_repair_evidence(self):
        from pcb.freeroute_delta import remember_blocked_nets

        blockers = {"power": "width unavailable"}
        remember_blocked_nets(blockers, {"ground", "power"}, "cleanup failed")
        self.assertEqual(blockers, {"ground": "cleanup failed", "power": "width unavailable"})

    def test_cleanup_budget_only_defers_after_a_real_unresolved_check(self):
        from pcb.freeroute_delta import cleanup_remainder, cleanup_timed_out

        self.assertFalse(cleanup_timed_out(0, 61.0, 60.0))
        self.assertFalse(cleanup_timed_out(1, 59.0, 60.0))
        self.assertTrue(cleanup_timed_out(1, 60.0, 60.0))
        self.assertFalse(cleanup_timed_out(50, 1000.0, None))
        bad = Trace("bad", "F.Cu", 200_000, (0, 0), (1_000_000, 0))
        good = Trace("good", "B.Cu", 200_000, (0, 0), (1_000_000, 0))
        delta = Delta((bad, good), (), (), 0)
        self.assertEqual(cleanup_remainder(delta, {"bad"}).traces, (good,))
        self.assertIsNone(cleanup_remainder(delta, {"bad", "good"}))
        self.assertIsNone(cleanup_remainder(delta, {"absent"}))

    def test_speculative_net_pruning_keeps_other_proposals_and_evidence(self):
        a = Trace("A", "F.Cu", 200_000, (0, 0), (1_000_000, 0))
        b = Trace("B", "B.Cu", 200_000, (0, 0), (0, 1_000_000))
        via = Via("A", (0, 0), 600_000, 300_000)
        delta = Delta((a, b), (via,), ("USB",), 2)
        self.assertEqual(without_nets(delta, {"A"}), Delta((b,), (), ("USB",), 2))
        self.assertEqual(delta.traces, (a, b))

    def test_disconnected_batch_retries_are_bounded_and_only_remove_new_copper(self):
        from pcb.freeroute_delta import ConnectivityRetries

        tracks = tuple(Trace(name, "B.Cu", 200_000, (0, i), (1_000_000, i))
                       for i, name in enumerate(("C", "A", "B")))
        via = Via("A", (2_000_000, 0), 600_000, 300_000)
        original = Delta(tracks, (via,), ("USB",), 1)
        retries = ConnectivityRetries(original, max_trials=4)
        candidates = [retries.next() for _ in range(5)]
        self.assertEqual([trial[1] for trial in candidates[:-1]],
                         [("A",), ("B",), ("C",), ("A", "B")])
        self.assertIsNone(candidates[-1])
        self.assertEqual({item.net for item in candidates[0][0].traces}, {"B", "C"})
        self.assertEqual(candidates[0][0].vias, ())
        self.assertEqual(candidates[0][0].excluded_nets, ("USB",))
        self.assertEqual(original.traces, tracks)

    def test_disconnected_batch_retry_never_returns_empty_or_unbounded_work(self):
        from pcb.freeroute_delta import ConnectivityRetries

        only = Trace("A", "B.Cu", 200_000, (0, 0), (1_000_000, 0))
        self.assertIsNone(ConnectivityRetries(Delta((only,), (), (), 0)).next())
        with self.assertRaises(ValueError):
            ConnectivityRetries(Delta((only,), (), (), 0), max_trials=-1)
        with self.assertRaises(ValueError):
            ConnectivityRetries(Delta((only,), (), (), 0), max_trials=True)

    def test_large_batch_isolates_multiple_conflicts_with_bounded_validation(self):
        from pcb.freeroute_delta import ConnectivityRetries

        tracks = tuple(Trace(chr(65 + i), "B.Cu", 200_000,
                             (0, i * 1_000_000), (1_000_000, i * 1_000_000))
                       for i in range(16))
        original = Delta(tracks, (), ("USB",), 0)
        retry = ConnectivityRetries(original, max_trials=8)
        bad = {"A", "O"}
        found = False
        seen = set()
        for _ in range(8):
            trial = retry.next()
            self.assertIsNotNone(trial)
            candidate, omitted = trial
            names = frozenset(item.net for item in candidate.traces)
            self.assertTrue(names)
            self.assertNotIn(names, seen)
            seen.add(names)
            self.assertEqual(names, frozenset(item.net for item in tracks) - set(omitted))
            self.assertEqual(candidate.excluded_nets, ("USB",))
            if names.isdisjoint(bad):
                found = True
                break
        self.assertTrue(found, "Multiple conflicts must not discard every clean route")
        self.assertEqual(original.traces, tracks)

    def test_pruning_remembers_the_resulting_copper_not_only_the_removed_group(self):
        from pcb.freeroute_delta import FailedPruneOutcomes, without_items

        tracks = tuple(Trace(name, "F.Cu", 200_000, (0, i * 100_000), (1_000_000, i * 100_000))
                       for i, name in enumerate(("A", "B", "C", "D")))
        original = Delta(tracks, (), (), 0)
        rejected = without_items(original, {tracks[1], tracks[2]})
        memory = FailedPruneOutcomes()
        memory.add(rejected)
        later = without_items(without_items(original, {tracks[1]}), {tracks[2]})
        self.assertTrue(memory.contains(later))
        self.assertFalse(memory.contains(without_items(original, {tracks[1]})))

    def test_timeout_checks_an_already_pending_prune(self):
        from pcb.freeroute_delta import timeout_remainder

        kept = Trace("A", "F.Cu", 200_000, (0, 0), (1_000_000, 0))
        delta = Delta((kept,), (), (), 0)
        # The rejected B net was already removed in the pending candidate.
        self.assertEqual(timeout_remainder(delta, {"B"}, pending=True), delta)
        self.assertIsNone(timeout_remainder(delta, {"B"}, pending=False))
        self.assertIsNone(timeout_remainder(delta, {"A"}, pending=False))

    def test_component_pruning_does_not_traverse_existing_copper(self):
        board = pcbnew.BOARD()
        net = pcbnew.NETINFO_ITEM(board, "A")
        board.Add(net)
        proposed = {}

        def track(first, last, is_new):
            item = pcbnew.PCB_TRACK(board)
            item.SetStart(pcbnew.VECTOR2I(first, 0))
            item.SetEnd(pcbnew.VECTOR2I(last, 0))
            item.SetWidth(200_000)
            item.SetLayer(pcbnew.F_Cu)
            item.SetNetCode(net.GetNetCode())
            board.Add(item)
            record = Trace("A", "F.Cu", 200_000, (first, 0), (last, 0))
            if is_new:
                proposed[item.m_Uuid.AsString()] = record
            return item, record

        first, first_record = track(0, 1_000_000, True)
        _, second_record = track(1_000_000, 2_000_000, True)
        track(2_000_000, 3_000_000, False)
        _, separated_record = track(3_000_000, 4_000_000, True)
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(2_000_000, 0))
        via.SetWidth(600_000)
        via.SetDrill(300_000)
        via.SetNetCode(net.GetNetCode())
        board.Add(via)
        via_record = Via("A", (2_000_000, 0), 600_000, 300_000)
        proposed[via.m_Uuid.AsString()] = via_record
        component = proposed_component(board, proposed, first.m_Uuid.AsString())
        self.assertEqual(component, {first_record, second_record, via_record})
        self.assertNotIn(separated_record, component)

    def test_dangling_branch_peels_across_a_via_without_touching_saved_copper(self):
        from pcb.freeroute_delta import dangling_proposed_branches

        board = pcbnew.BOARD()
        net = pcbnew.NETINFO_ITEM(board, "A")
        board.Add(net)
        proposed = {}

        def track(start, end, layer, is_new):
            item = pcbnew.PCB_TRACK(board)
            item.SetStart(pcbnew.VECTOR2I(start, 0))
            item.SetEnd(pcbnew.VECTOR2I(end, 0))
            item.SetWidth(200_000)
            item.SetLayer(layer)
            item.SetNetCode(net.GetNetCode())
            board.Add(item)
            record = Trace("A", board.GetLayerName(layer), 200_000, (start, 0), (end, 0))
            if is_new:
                proposed[item.m_Uuid.AsString()] = record
            return item, record

        seed, seed_record = track(0, 1_000_000, pcbnew.F_Cu, True)
        _, inner_record = track(1_000_000, 2_000_000, pcbnew.B_Cu, True)
        track(2_000_000, 3_000_000, pcbnew.B_Cu, False)
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(1_000_000, 0))
        via.SetWidth(600_000)
        via.SetDrill(300_000)
        via.SetNetCode(net.GetNetCode())
        board.Add(via)
        via_record = Via("A", (1_000_000, 0), 600_000, 300_000)
        proposed[via.m_Uuid.AsString()] = via_record

        removed = dangling_proposed_branches(board, proposed, {seed.m_Uuid.AsString()})
        self.assertEqual(removed, {seed_record, via_record})
        self.assertNotIn(inner_record, removed)

        # The seed also touches a real pad: it is no longer a disposable leaf.
        footprint = pcbnew.FOOTPRINT(board)
        board.Add(footprint)
        pad = pcbnew.PAD(footprint)
        pad.SetPosition(pcbnew.VECTOR2I(0, 0))
        pad.SetSize(pcbnew.VECTOR2I(400_000, 400_000))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(pcbnew.LSET.FrontMask())
        pad.SetNetCode(net.GetNetCode())
        footprint.Add(pad)
        self.assertFalse(dangling_proposed_branches(board, proposed, {seed.m_Uuid.AsString()}))

    def test_dangling_branch_does_not_assume_zone_connections(self):
        from pcb.freeroute_delta import dangling_proposed_branches

        board = pcbnew.BOARD()
        net = pcbnew.NETINFO_ITEM(board, "A")
        board.Add(net)
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I(0, 0))
        track.SetEnd(pcbnew.VECTOR2I(1_000_000, 0))
        track.SetWidth(200_000)
        track.SetLayer(pcbnew.F_Cu)
        track.SetNetCode(net.GetNetCode())
        board.Add(track)
        zone = pcbnew.ZONE(board)
        board.Add(zone)
        zone.SetNet(net)
        record = Trace("A", "F.Cu", 200_000, (0, 0), (1_000_000, 0))
        self.assertFalse(dangling_proposed_branches(board, {track.m_Uuid.AsString(): record},
                                                    {track.m_Uuid.AsString()}))

    def test_dangling_branch_stops_before_a_multi_layer_junction(self):
        from pcb.freeroute_delta import dangling_proposed_branches

        board = pcbnew.BOARD()
        net = pcbnew.NETINFO_ITEM(board, "A")
        board.Add(net)
        proposed = {}
        for end in ((2_000_000, 0), (1_000_000, 1_000_000)):
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(1_000_000, 0))
            track.SetEnd(pcbnew.VECTOR2I(*end))
            track.SetWidth(200_000)
            track.SetLayer(pcbnew.B_Cu)
            track.SetNetCode(net.GetNetCode())
            board.Add(track)
            proposed[track.m_Uuid.AsString()] = Trace("A", "B.Cu", 200_000, (1_000_000, 0), end)
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(1_000_000, 0))
        via.SetWidth(600_000)
        via.SetDrill(300_000)
        via.SetNetCode(net.GetNetCode())
        board.Add(via)
        proposed[via.m_Uuid.AsString()] = Via("A", (1_000_000, 0), 600_000, 300_000)
        seed = pcbnew.PCB_TRACK(board)
        seed.SetStart(pcbnew.VECTOR2I(0, 0))
        seed.SetEnd(pcbnew.VECTOR2I(1_000_000, 0))
        seed.SetWidth(200_000)
        seed.SetLayer(pcbnew.F_Cu)
        seed.SetNetCode(net.GetNetCode())
        board.Add(seed)
        record = Trace("A", "F.Cu", 200_000, (0, 0), (1_000_000, 0))
        proposed[seed.m_Uuid.AsString()] = record

        self.assertEqual(dangling_proposed_branches(board, proposed, {seed.m_Uuid.AsString()}), {record})

    def test_unsafe_shortcut_fallback_preserves_other_nets_and_vias(self):
        shortcut = Trace("A", "F.Cu", 200_000, (0, 0), (2_000_000, 0))
        original = (Trace("A", "F.Cu", 200_000, (0, 0), (1_000_000, 1_000_000)),
                    Trace("A", "F.Cu", 200_000, (1_000_000, 1_000_000), (2_000_000, 0)))
        other = Trace("B", "B.Cu", 200_000, (0, 0), (0, 1_000_000))
        via = Via("B", (0, 0), 600_000, 300_000)
        delta = Delta((shortcut, other), (via,), (), 0)
        restored = restore_raw_nets(delta, (*original, other), {"A"})
        self.assertEqual(set(restored.traces), {*original, other})
        self.assertEqual(restored.vias, (via,))

    def test_subtracts_saved_copper_even_if_router_merges_it(self):
        saved = [Trace("GND", "B.Cu", 500_000, (0, 0), (10_000_000, 0))]
        exported = [
            Trace("GND", "B.Cu", 500_000, (0, 0), (4_000_000, 0)),
            Trace("GND", "B.Cu", 500_000, (4_000_000, 0), (10_000_000, 0)),
        ]
        routed = [Trace("GND", "B.Cu", 500_000, (0, 0), (15_000_000, 0))]
        delta = additive_delta(saved, exported, routed)
        self.assertEqual(
            delta.traces,
            (Trace("GND", "B.Cu", 500_000, (10_000_000, 0), (15_000_000, 0)),),
        )

    def test_preserves_old_vias_and_excludes_critical_nets(self):
        old = Via("GND", (1_000_000, 2_000_000), 600_000, 300_000)
        # Specctra may round an existing centre by a fraction of a micrometre.
        changed_size = Via("GND", (old.at[0] + 500, old.at[1]), 800_000, 400_000)
        added = Via("GND", (4_000_000, 5_000_000), 800_000, 400_000)
        critical = Trace("USB_DP", "F.Cu", 200_000, (0, 0), (1_000_000, 0))
        delta = additive_delta([old], [old], [changed_size, added, critical], excluded={"USB_DP"})
        self.assertEqual(delta.vias, (added,))
        self.assertEqual(delta.traces, ())
        self.assertIn("USB_DP", delta.excluded_nets)

    def test_excluded_narrowed_net_cannot_contribute_traces_or_vias(self):
        old = Trace("A", "B.Cu", 250_000, (0, 0), (1_000_000, 0))
        narrowed = Trace("A", "B.Cu", 200_000, (1_000_000, 0), (2_000_000, 0))
        via = Via("A", (2_000_000, 0), 600_000, 300_000)
        good = Trace("B", "In1.Cu", 250_000, (0, 0), (0, 1_000_000))
        delta = additive_delta([old], [old], [old, narrowed, via, good], excluded={"A"})
        self.assertEqual(delta.traces, (good,))
        self.assertEqual(delta.vias, ())
        self.assertEqual(delta.excluded_nets, ("A",))

    def test_rejects_non_octilinear_and_zero_length(self):
        candidate = [
            Trace("A", "In1.Cu", 200_000, (0, 0), (2_000_000, 1_000_000)),
            Trace("A", "In2.Cu", 200_000, (0, 0), (0, 0)),
            Trace("A", "F.Cu", 200_000, (0, 0), (1_000_000, 1_000_000)),
        ]
        delta = additive_delta([], [], candidate)
        self.assertEqual(delta.traces, (candidate[2],))
        self.assertEqual(delta.unsupported, 2)

    def test_chamfers_new_right_angles_without_moving_endpoints(self):
        tracks = (
            Trace("GND", "B.Cu", 500_000, (0, 0), (1_000_000, 0)),
            Trace("GND", "B.Cu", 500_000, (1_000_000, 0), (1_000_000, 1_000_000)),
        )
        result = chamfer_right_angles(tracks, [], lambda *_: False)
        self.assertEqual(set(result), {
            Trace("GND", "B.Cu", 500_000, (0, 0), (750_000, 0)),
            Trace("GND", "B.Cu", 500_000, (750_000, 0), (1_000_000, 250_000)),
            Trace("GND", "B.Cu", 500_000, (1_000_000, 250_000), (1_000_000, 1_000_000)),
        })
        self.assertEqual(chamfer_right_angles(tracks, [], lambda *_: True), tracks)

    def test_never_chamfers_a_branch_attached_to_old_copper(self):
        tracks = (
            Trace("A", "F.Cu", 200_000, (0, 0), (1_000_000, 0)),
            Trace("A", "F.Cu", 200_000, (1_000_000, 0), (1_000_000, 1_000_000)),
        )
        old = [Trace("A", "F.Cu", 200_000, (1_000_000, -1_000_000), (1_000_000, 0))]
        self.assertEqual(chamfer_right_angles(tracks, old, lambda *_: False), tracks)

    def test_drc_pruning_only_targets_proposed_copper(self):
        report = {"violations": [
            {"severity": "error", "items": [{"uuid": "new"}, {"uuid": "old"}]},
            {"severity": "warning", "items": [{"uuid": "old"}]},
        ]}
        removed, blockers = drc_rejections(report, {"new"})
        self.assertEqual(removed, {"new"})
        self.assertEqual(blockers, ())
        report["violations"][0]["items"] = [{"uuid": "old"}]
        removed, blockers = drc_rejections(report, {"new"})
        self.assertEqual(removed, set())
        self.assertEqual(len(blockers), 1)

    def test_external_filter_preserves_only_identical_preexisting_errors(self):
        old = {
            "type": "copper_edge_clearance",
            "severity": "error",
            "description": "required 0.5000 mm; actual 0.4214 mm",
            "items": [
                {"uuid": "connector-pad", "pos": {"x": 1.0, "y": 2.0}},
                {"uuid": "locator-hole", "pos": {"x": 3.0, "y": 4.0}},
            ],
        }
        new = {
            "type": "shorting_items",
            "severity": "error",
            "description": "short",
            "items": [{"uuid": "new-track"}, {"uuid": "old-track"}],
        }
        baseline = {"violations": [old]}
        report = {"violations": [old, new]}
        try:
            rejected, blockers = drc_rejections(report, {"new-track"}, baseline_report=baseline)
        except TypeError as error:
            self.fail(f"Existing baseline errors cannot currently be compared: {error}")
        self.assertEqual(rejected, {"new-track"})
        self.assertEqual(blockers, ())

    def test_new_zone_neck_warning_blocks_even_without_a_proposed_item_id(self):
        warning = {
            "type": "connection_width", "severity": "warning",
            "description": "minimum 0.20 mm; actual 0.18 mm",
            "items": [{"uuid": "existing-pad"}, {"uuid": "existing-zone"}],
        }
        rejected, blockers = drc_rejections(
            {"violations": [warning]}, {"new-track"}, baseline_report={"violations": []})
        self.assertEqual(rejected, set())
        self.assertEqual(blockers, (warning["description"],))
        self.assertEqual(drc_rejections(
            {"violations": [warning]}, {"new-track"},
            baseline_report={"violations": [warning]}), (set(), ()))
        worse = {**warning, "description": "minimum 0.20 mm; actual 0.12 mm"}
        self.assertEqual(len(drc_rejections(
            {"violations": [worse]}, {"new-track"},
            baseline_report={"violations": [warning]})[1]), 1)

    def test_relocated_width_markers_need_proof_of_an_unchanged_complete_net(self):
        old = {"type": "connection_width", "severity": "warning", "description": "narrow",
               "items": [{"uuid": "pad-a"}, {"uuid": "track-a"}]}
        moved = {**old, "items": [{"uuid": "pad-b"}, {"uuid": "track-b"}]}
        stable = {name: "POWER" for name in ("pad-a", "track-a", "pad-b", "track-b")}
        self.assertEqual(drc_rejections(
            {"violations": [moved]}, {"proposal"}, baseline_report={"violations": [old]},
            stable_items=stable), (set(), ()))
        for proof in (None, {"pad-b": "POWER", "track-b": "OTHER"}):
            self.assertTrue(drc_rejections(
                {"violations": [moved]}, {"proposal"}, baseline_report={"violations": [old]},
                stable_items=proof)[1])
        self.assertTrue(drc_rejections(
            {"violations": [moved, moved]}, {"proposal"}, baseline_report={"violations": [old]},
            stable_items=stable)[1])

    def test_external_filter_blocks_worsened_or_duplicated_old_errors(self):
        old = {
            "type": "copper_edge_clearance",
            "severity": "error",
            "description": "required 0.5000 mm; actual 0.4214 mm",
            "items": [{"uuid": "pad"}, {"uuid": "hole"}],
        }
        baseline = {"violations": [old]}
        worse = {**old, "description": "required 0.5000 mm; actual 0.3000 mm"}
        for findings in ([worse], [old, old]):
            with self.subTest(findings=findings):
                try:
                    rejected, blockers = drc_rejections(
                        {"violations": findings}, set(), baseline_report=baseline
                    )
                except TypeError as error:
                    self.fail(f"Existing baseline errors cannot currently be compared: {error}")
                self.assertEqual(rejected, set())
                self.assertEqual(len(blockers), 1)

    def test_external_filter_refuses_unidentified_baseline_errors(self):
        baseline = {"violations": [{"severity": "error", "description": "unknown"}]}
        with self.assertRaisesRegex(ValueError, "unidentified baseline"):
            drc_rejections({"violations": []}, set(), baseline_report=baseline)

    def test_external_filter_never_exempts_proposed_copper(self):
        old = {
            "type": "shorting_items", "severity": "error", "description": "short",
            "items": [{"uuid": "existing"}, {"uuid": "proposed"}],
        }
        rejected, blockers = drc_rejections(
            {"violations": [old]}, {"proposed"}, baseline_report={"violations": [old]},
        )
        self.assertEqual(rejected, {"proposed"})
        self.assertEqual(blockers, ())

    def test_style_pruning_discards_only_proposed_items_in_new_staircase(self):
        empty = StyleReport((), (), (), (), (), (), (), ())
        stair = Staircase("GND", "B.Cu", ((0, 0), (1, 0)), (1, 0, 1), ("old", "new"))
        after = StyleReport((), (), (), (), (stair,), (), (), ())
        reasons = Counter()
        rejected, blockers = style_rejections(empty, after, {}, {}, {"new"}, reasons=reasons)
        self.assertEqual(reasons, {"staircases": 1})
        self.assertEqual(rejected, {"new"})
        self.assertEqual(blockers, ())
        rejected, blockers = style_rejections(empty, after, {}, {}, {"other"})
        self.assertEqual(rejected, set())
        self.assertEqual(len(blockers), 1)

    def test_dangling_only_items_are_pruned_without_hiding_other_problems(self):
        report = {"violations": [
            {"type": "track_dangling", "items": [{"uuid": "dangling"}, {"uuid": "short"}]},
            {"type": "shorting_items", "items": [{"uuid": "short"}]},
            {"type": "via_dangling", "items": [{"uuid": "via"}]},
        ]}
        self.assertEqual(dangling_only_rejections(report, {"dangling", "short", "via"}, {"via"}),
                         {"dangling"})

    def test_default_class_uses_the_specctra_class_name(self):
        self.assertEqual(router_class("Default"), "kicad_default")
        self.assertEqual(router_class("GROUND"), "GROUND")

    def test_defers_the_class_causing_rejections_before_the_largest_proposal_class(self):
        proposed = Counter({"kicad_default": 9, "GROUND": 5})
        self.assertEqual(deferred_class(proposed, Counter({"GROUND": 2})), "GROUND")
        self.assertEqual(deferred_class(proposed, Counter()), "kicad_default")
        self.assertIsNone(deferred_class(Counter(), Counter()))

    def test_simplifies_a_staircase_between_existing_copper_contacts(self):
        old = [
            Trace("A", "F.Cu", 200_000, (0, 200_000), (200_000, 0)),
            Trace("A", "F.Cu", 200_000, (4_700_000, -4_400_000), (4_700_000, -3_200_000)),
        ]
        path = (
            Trace("A", "F.Cu", 200_000, (200_000, 0), (1_538_000, 0)),
            Trace("A", "F.Cu", 200_000, (1_538_000, 0), (1_924_800, -386_800)),
            Trace("A", "F.Cu", 200_000, (1_924_800, -386_800), (2_810_800, -386_800)),
            Trace("A", "F.Cu", 200_000, (2_810_800, -386_800), (4_700_000, -2_276_000)),
            Trace("A", "F.Cu", 200_000, (4_700_000, -2_276_000), (4_700_000, -3_200_000)),
        )
        result = simplify_terminal_chains(path, old, lambda *_: False)
        self.assertEqual(set(result), {
            Trace("A", "F.Cu", 200_000, (200_000, 0), (1_500_000, 0)),
            Trace("A", "F.Cu", 200_000, (1_500_000, 0), (4_700_000, -3_200_000)),
        })
        self.assertEqual(simplify_terminal_chains(path, old, lambda *_: True), tuple(sorted(path)))

    def test_shortcut_avoids_foreign_copper_and_uses_clear_alternative(self):
        from pcb import freeroute_delta as delta
        from pcb.route_optimizer import Obstacle, Point

        self.assertTrue(callable(getattr(delta, "shortcut_clearance_checker", None)))
        blocker = Obstacle(Point(800_000, -100_000), Point(1_000_000, 100_000),
                           ("F.Cu",), "B", "pad")
        check = delta.shortcut_clearance_checker((blocker,))
        path = (
            Trace("A", "F.Cu", 200_000, (200_000, 0), (1_538_000, 0)),
            Trace("A", "F.Cu", 200_000, (1_538_000, 0), (1_924_800, -386_800)),
            Trace("A", "F.Cu", 200_000, (1_924_800, -386_800), (2_810_800, -386_800)),
            Trace("A", "F.Cu", 200_000, (2_810_800, -386_800), (4_700_000, -2_276_000)),
            Trace("A", "F.Cu", 200_000, (4_700_000, -2_276_000), (4_700_000, -3_200_000)),
        )
        result = delta.simplify_terminal_chains(path, [], lambda *_: False, shortcut_allowed=check)
        self.assertEqual(set(result), {
            Trace("A", "F.Cu", 200_000, (200_000, 0), (3_400_000, -3_200_000)),
            Trace("A", "F.Cu", 200_000, (3_400_000, -3_200_000), (4_700_000, -3_200_000)),
        })
        self.assertEqual(delta.simplify_terminal_chains(path, [], lambda *_: False,
                                                        shortcut_allowed=lambda _: False),
                         tuple(sorted(path)))

    def test_shortcut_avoids_overlapping_untouched_same_net_copper(self):
        points = ((0, 0), (1_000_000, 1_000_000),
                  (2_000_000, 1_000_000), (3_000_000, 2_000_000))
        path = tuple(Trace("A", "F.Cu", 200_000, a, b) for a, b in zip(points, points[1:]))
        existing = [Trace("A", "F.Cu", 200_000, (1_200_000, 200_000), (1_800_000, 800_000))]
        result = simplify_terminal_chains(path, existing, lambda *_: False)
        self.assertEqual(set(result), {
            Trace("A", "F.Cu", 200_000, (0, 0), (2_000_000, 2_000_000)),
            Trace("A", "F.Cu", 200_000, (2_000_000, 2_000_000), (3_000_000, 2_000_000)),
        })

    def test_shortcut_shifts_a_chamfer_between_two_blocked_extremes(self):
        from pcb import freeroute_delta as delta
        from pcb.route_optimizer import Obstacle, Point

        blockers = (
            Obstacle(Point(450_000, 450_000), Point(550_000, 550_000), ("F.Cu",), "B", "pad"),
            Obstacle(Point(2_450_000, -50_000), Point(2_550_000, 50_000), ("F.Cu",), "B", "pad"),
        )
        points = ((0, 0), (900_000, 0), (1_900_000, 1_000_000),
                  (2_800_000, 1_000_000), (3_800_000, 2_000_000), (5_000_000, 2_000_000))
        path = tuple(Trace("A", "F.Cu", 200_000, a, b) for a, b in zip(points, points[1:]))
        check = delta.shortcut_clearance_checker(blockers)
        result = delta.simplify_terminal_chains(path, [], lambda *_: False, shortcut_allowed=check)
        self.assertEqual(len(result), 3)
        self.assertTrue(check(result))
        self.assertTrue(all(delta._octilinear(item) for item in result))

    def test_shortcut_refines_a_gap_between_coarse_chamfer_samples(self):
        points = ((0, 0), (900_000, 0), (1_900_000, 1_000_000),
                  (2_800_000, 1_000_000), (3_800_000, 2_000_000), (5_000_000, 2_000_000))
        path = tuple(Trace("A", "F.Cu", 200_000, a, b) for a, b in zip(points, points[1:]))

        def allowed(pieces):
            # The first 16-way samples are 187,500 nm apart.  This gap is
            # halfway between them and cannot be found by the coarse pass.
            return len(pieces) == 3 and pieces[0].end == (93_750, 0)

        result = simplify_terminal_chains(path, [], lambda *_: False, shortcut_allowed=allowed)
        self.assertEqual(len(result), 3)
        self.assertTrue(allowed(result))
        self.assertEqual(set(chain_endpoints(result)), {points[0], points[-1]})

    def test_shortcut_respects_an_expired_search_budget(self):
        points = ((0, 0), (900_000, 0), (1_900_000, 1_000_000),
                  (2_800_000, 1_000_000), (3_800_000, 2_000_000), (5_000_000, 2_000_000))
        path = tuple(Trace("A", "F.Cu", 200_000, a, b) for a, b in zip(points, points[1:]))
        result = simplify_terminal_chains(path, [], lambda *_: False,
                                          shortcut_allowed=lambda _: True,
                                          may_continue=lambda: False)
        self.assertEqual(result, tuple(sorted(path)))

    def test_local_shortcuts_can_simplify_when_the_whole_chain_is_blocked(self):
        from pcb.freeroute_delta import simplify_local_windows

        points = ((0, 0), (1_000_000, 0), (2_000_000, 1_000_000),
                  (3_000_000, 1_000_000), (4_000_000, 2_000_000),
                  (5_000_000, 2_000_000), (6_000_000, 3_000_000))
        path = tuple(Trace("A", "F.Cu", 200_000, a, b) for a, b in zip(points, points[1:]))
        # Represent a clearance screen that permits only short local changes.
        def allowed(pieces):
            return all(max(abs(t.end[0] - t.start[0]), abs(t.end[1] - t.start[1]))
                       <= 2_000_000 for t in pieces)
        self.assertEqual(simplify_terminal_chains(path, [], lambda *_: False, shortcut_allowed=allowed),
                         tuple(sorted(path)))
        result = simplify_local_windows(path, [], lambda *_: False, shortcut_allowed=allowed)
        self.assertLess(len(result), len(path))
        self.assertEqual(set(chain_endpoints(result)), {points[0], points[-1]})
        self.assertTrue(all(item.net == "A" and item.layer == "F.Cu" for item in result))
        self.assertEqual(simplify_local_windows(path, [], lambda *_: False,
                                                shortcut_allowed=lambda _: False), tuple(sorted(path)))

    def test_local_shortcuts_keep_pinned_contacts(self):
        from pcb.freeroute_delta import simplify_local_windows

        points = ((0, 0), (1_000_000, 0), (2_000_000, 1_000_000),
                  (3_000_000, 1_000_000), (4_000_000, 2_000_000),
                  (5_000_000, 2_000_000), (6_000_000, 3_000_000))
        path = tuple(Trace("A", "F.Cu", 200_000, a, b) for a, b in zip(points, points[1:]))
        anchor = points[3]
        result = simplify_local_windows(path, [], lambda _net, _layer, point: point == anchor)
        self.assertTrue(any(anchor in (item.start, item.end) for item in result))

    def test_shortcut_screen_respects_layers_and_same_net_contacts(self):
        from pcb import freeroute_delta as delta
        from pcb.route_optimizer import Obstacle, Point

        self.assertTrue(callable(getattr(delta, "shortcut_clearance_checker", None)))
        obstacles = (
            Obstacle(Point(400_000, -100_000), Point(600_000, 100_000), ("F.Cu",), "B", "pad"),
            Obstacle(Point(400_000, -100_000), Point(600_000, 100_000), ("B.Cu",), "A", "copper"),
        )
        check = delta.shortcut_clearance_checker(obstacles)
        top = Trace("A", "F.Cu", 200_000, (0, 0), (1_000_000, 0))
        bottom = Trace("A", "B.Cu", 200_000, (0, 0), (1_000_000, 0))
        self.assertFalse(check((top,)))
        self.assertTrue(check((bottom,)))

    def test_native_shortcut_screen_covers_drilled_holes_on_all_copper_layers(self):
        from pcb import freeroute_delta as delta

        self.assertTrue(callable(getattr(delta, "native_shortcut_obstacles", None)))
        board = pcbnew.BOARD()
        board.SetCopperLayerCount(4)
        footprint = pcbnew.FOOTPRINT(board)
        pad = pcbnew.PAD(footprint)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetLayerSet(pcbnew.LSET.AllCuMask())
        pad.SetSize(pcbnew.VECTOR2I(800_000, 800_000))
        pad.SetDrillSize(pcbnew.VECTOR2I(800_000, 800_000))
        pad.SetPosition(pcbnew.VECTOR2I(500_000, 0))
        footprint.Add(pad)
        board.Add(footprint)
        check = delta.shortcut_clearance_checker(delta.native_shortcut_obstacles(board))
        for layer in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"):
            with self.subTest(layer=layer):
                self.assertFalse(check((Trace("A", layer, 200_000, (0, 0), (1_000_000, 0)),)))

    def test_search_limit_retries_on_a_larger_real_grid_budget(self):
        from pcb import freeroute_delta as delta
        from pcb.route_optimizer import GridRouter, Point, mm

        self.assertTrue(callable(getattr(delta, "retry_limited_search", None)))
        calls = []
        def search(limit):
            calls.append(limit)
            model = GridRouter(Point.from_mm([-2, -2]), Point.from_mm([12, 12]),
                               mm(1), ("F.Cu", "B.Cu"), "SIGNAL", mm(.2), mm(.2),
                               mm(.6), mm(.3), max_expansions=limit)
            return model.route(Point.from_mm([0, 0]), "F.Cu", Point.from_mm([9, 9]), "F.Cu")

        result = delta.retry_limited_search(search, first_limit=1, retry_limit=500)
        self.assertTrue(result.segments)
        self.assertEqual(calls, [1, 500])

    def test_local_repair_tries_finer_grids_when_coarse_access_is_blocked(self):
        from pcb.freeroute_delta import retry_local_grids
        from pcb.route_optimizer import NoGridRoute

        calls = []

        def search(step, limit):
            calls.append((step, limit))
            if step == .2:
                raise NoGridRoute("blocked on coarse grid")
            return "candidate"

        self.assertEqual(retry_local_grids(search), "candidate")
        self.assertEqual(calls, [(.2, 15_000), (.1, 15_000)])

    def test_local_grid_limits_remain_unknown_and_unexpected_errors_propagate(self):
        from pcb.freeroute_delta import retry_local_grids
        from pcb.route_optimizer import NoGridRoute, SearchLimitReached

        def mixed(step, _limit):
            if step == .2:
                raise SearchLimitReached("limit", expanded=5)
            raise NoGridRoute("blocked")

        with self.assertRaises(SearchLimitReached):
            retry_local_grids(mixed)
        def eventually_exhausted(step, limit):
            if step == .2 and limit == 15_000:
                raise SearchLimitReached("pilot limit", expanded=5)
            raise NoGridRoute("blocked")

        with self.assertRaises(NoGridRoute):
            retry_local_grids(eventually_exhausted)
        with self.assertRaisesRegex(ValueError, "unexpected"):
            retry_local_grids(lambda *_: (_ for _ in ()).throw(ValueError("unexpected")))

    def test_local_grid_can_escalate_only_limited_searches(self):
        from pcb.freeroute_delta import retry_local_grids
        from pcb.route_optimizer import NoGridRoute, SearchLimitReached

        calls = []
        def search(step, limit):
            calls.append((step, limit))
            if step != .2:
                raise NoGridRoute("grid exhausted")
            if limit < 400_000:
                raise SearchLimitReached("bounded", expanded=limit)
            return "candidate"

        self.assertEqual(retry_local_grids(search), "candidate")
        self.assertEqual(calls[-1], (.2, 400_000))
        self.assertEqual(sum(step != .2 for step, _ in calls), 2)
        with self.assertRaises(ValueError):
            retry_local_grids(search, expansion_limits=(150_000, 15_000))

    def test_exhausted_cleanup_budget_does_not_trigger_larger_search(self):
        from pcb import freeroute_delta as delta
        from pcb.route_optimizer import GridRouter, Point, SearchLimitReached, mm

        self.assertTrue(callable(getattr(delta, "retry_limited_search", None)))
        calls = []
        def search(limit):
            calls.append(limit)
            model = GridRouter(Point.from_mm([-2, -2]), Point.from_mm([12, 12]),
                               mm(1), ("F.Cu", "B.Cu"), "SIGNAL", mm(.2), mm(.2),
                               mm(.6), mm(.3), max_expansions=limit)
            return model.route(Point.from_mm([0, 0]), "F.Cu", Point.from_mm([9, 9]), "F.Cu")

        with self.assertRaises(SearchLimitReached):
            delta.retry_limited_search(search, first_limit=1, retry_limit=500, may_retry=lambda: False)
        self.assertEqual(calls, [1])

    def test_no_route_result_is_not_retried_as_a_search_limit(self):
        from pcb import freeroute_delta as delta
        from pcb.route_optimizer import NoGridRoute

        calls = []
        def search(limit):
            calls.append(limit)
            raise NoGridRoute("no path in this grid")

        with self.assertRaises(NoGridRoute):
            delta.retry_limited_search(search, first_limit=10, retry_limit=100)
        self.assertEqual(calls, [10])

    def test_repair_keeps_unrelated_proposals_in_the_same_batch(self):
        from pcb import freeroute_delta as delta

        self.assertTrue(callable(getattr(delta, "replace_repaired_chain", None)))
        old_a = Trace("A", "F.Cu", 200_000, (0, 0), (1_000_000, 0))
        old_b = Trace("B", "B.Cu", 200_000, (0, 1_000_000), (1_000_000, 1_000_000))
        via_b = Via("B", (1_000_000, 1_000_000), 600_000, 300_000)
        new_a = Trace("A", "F.Cu", 200_000, (0, 0), (1_000_000, 1_000_000))
        new_via_a = Via("A", (1_000_000, 1_000_000), 600_000, 300_000)
        prior = Delta((old_a, old_b), (via_b,), ("USB",), 0)
        result = delta.replace_repaired_chain(prior, (old_a,), (new_a,), (new_via_a,))
        self.assertEqual(set(result.traces), {new_a, old_b})
        self.assertEqual(set(result.vias), {via_b, new_via_a})
        self.assertEqual(result.excluded_nets, ("USB",))
        self.assertEqual(set(prior.traces), {old_a, old_b})

    def test_other_unrepaired_style_findings_do_not_hide_a_clean_repair(self):
        from pcb import freeroute_delta as delta

        self.assertTrue(callable(getattr(delta, "replacement_style_clear", None)))
        fixed = Trace("A", "F.Cu", 200_000, (0, 0), (1_000_000, 0))
        other = Trace("B", "B.Cu", 200_000, (0, 1_000_000), (1_000_000, 1_000_000))
        records = {"fixed": fixed, "other": other}
        self.assertTrue(delta.replacement_style_clear({"other"}, records, (fixed,), ()))
        self.assertFalse(delta.replacement_style_clear({"fixed", "other"}, records, (fixed,), ()))
        import inspect
        self.assertIn("blockers", inspect.signature(delta.replacement_style_clear).parameters)
        self.assertFalse(delta.replacement_style_clear(set(), records, (fixed,), (),
                                                      blockers=("New ambiguous geometry",)))

    def test_final_salvage_can_recheck_other_nets_after_multiple_conflicts(self):
        from pcb import freeroute_delta as delta

        self.assertTrue(callable(getattr(delta, "salvage_remainder", None)))
        tracks = tuple(Trace(name, "F.Cu", 200_000, (0, n * 1_000_000), (1_000_000, n * 1_000_000))
                       for n, name in enumerate(("A", "B", "C")))
        proposal = Delta(tracks, (), (), 0)
        first = delta.salvage_remainder(proposal, {"A"}, attempts=0, max_attempts=2)
        self.assertEqual({item.net for item in first.traces}, {"B", "C"})
        second = delta.salvage_remainder(first, {"B"}, attempts=1, max_attempts=2)
        self.assertEqual({item.net for item in second.traces}, {"C"})
        self.assertIsNone(delta.salvage_remainder(second, {"C"}, attempts=2, max_attempts=2))
        self.assertIsNone(delta.salvage_remainder(second, {"absent"}, attempts=1, max_attempts=2))

    def test_local_repair_requires_one_unbranched_chain_and_safe_headings(self):
        first = Trace("A", "F.Cu", 200_000, (0, 0), (1_000_000, 0))
        second = Trace("A", "F.Cu", 200_000, (1_000_000, 0), (2_000_000, -1_000_000))
        self.assertEqual(chain_endpoints((first, second)), ((0, 0), (2_000_000, -1_000_000)))
        branch = Trace("A", "F.Cu", 200_000, (1_000_000, 0), (1_000_000, 1_000_000))
        self.assertIsNone(chain_endpoints((first, second, branch)))
        old = [Trace("A", "F.Cu", 200_000, (-1_000_000, 1_000_000), (0, 0))]
        self.assertEqual(set(endpoint_directions("A", "F.Cu", (0, 0), old, False)),
                         {"E", "S", "W", "N", "NE"})
        self.assertIn("SW", endpoint_directions("A", "F.Cu", (0, 0), old, True))
        horizontal = [Trace("A", "F.Cu", 200_000, (-1_000_000, 0), (0, 0))]
        self.assertEqual(set(endpoint_directions("A", "F.Cu", (0, 0), horizontal, False)),
                         {"E", "NE", "NW", "SE", "SW"})
        unrelated = Trace("A", "F.Cu", 200_000, (5_000_000, 0), (6_000_000, 0))
        self.assertEqual(set(connected_trace_component(first, (first, second, unrelated))), {first, second})
        self.assertEqual(initial_heading((first, second), (0, 0), "F.Cu"), "E")
        self.assertEqual(initial_heading((first, second), (2_000_000, -1_000_000), "F.Cu"), "SW")
        with self.assertRaises(ValueError):
            initial_heading((first, second, branch), (1_000_000, 0), "F.Cu")

    def test_prunes_dangling_chains_but_keeps_existing_contacts_and_branches(self):
        first = Trace("A", "F.Cu", 200_000, (0, 0), (1_000_000, 0))
        second = Trace("A", "F.Cu", 200_000, (1_000_000, 0), (2_000_000, 0))
        old = [Trace("A", "F.Cu", 200_000, (-1_000_000, 0), (0, 0))]
        self.assertEqual(set(dangling_trace_remainder(second, (first, second), old, lambda *_: False)),
                         {first, second})
        self.assertEqual(dangling_trace_remainder(second, (first, second), old,
                                                  lambda _n, _l, p: p == (1_000_000, 0)), (second,))
        branch = Trace("A", "F.Cu", 200_000, (1_000_000, 0), (1_000_000, 1_000_000))
        self.assertEqual(dangling_trace_remainder(second, (first, second, branch), old, lambda *_: False),
                         (second,))

    def test_repair_subchain_excludes_good_neighbors_and_preserves_contacts(self):
        from pcb.freeroute_delta import repairable_subchain

        points = ((0, 0), (1_000_000, 0), (2_000_000, 1_000_000),
                  (3_000_000, 1_000_000), (4_000_000, 2_000_000), (5_000_000, 2_000_000))
        path = tuple(Trace("A", "F.Cu", 200_000, a, b) for a, b in zip(points, points[1:]))
        bad = set(path[1:4])
        self.assertEqual(set(repairable_subchain(path[2], path, bad, [], lambda *_: False)), bad)
        # An unselected branch touching the interior cannot move with the repair.
        branch = Trace("A", "F.Cu", 200_000, points[2], (2_000_000, 2_000_000))
        self.assertEqual(repairable_subchain(path[2], (*path, branch), bad, [], lambda *_: False),
                         (path[2],))
        # A via or pad inside the defective section is also a fixed anchor.
        self.assertEqual(repairable_subchain(path[2], path, bad, [],
                                            lambda _net, _layer, at: at == points[2]), (path[2],))

    def test_repair_window_can_expand_over_unanchored_proposal_neighbors(self):
        from pcb.freeroute_delta import expand_repairable_subchain

        points = ((0, 0), (1_000_000, 0), (2_000_000, 1_000_000),
                  (3_000_000, 1_000_000), (4_000_000, 2_000_000), (5_000_000, 2_000_000))
        path = tuple(Trace("A", "F.Cu", 200_000, a, b) for a, b in zip(points, points[1:]))
        rejected = set(path[1:4])
        self.assertEqual(set(expand_repairable_subchain(path[2], path, rejected, [],
                                                       lambda *_: False)), set(path))
        # A pad/via anchor at the left boundary must remain a fixed endpoint.
        anchored = expand_repairable_subchain(path[2], path, rejected, [],
                                              lambda _n, _l, p: p == points[1])
        self.assertEqual(set(anchored), set(path[1:]))
        # A branch at the right boundary prevents consuming either branch.
        branch = Trace("A", "F.Cu", 200_000, points[4], (4_000_000, 3_000_000))
        expanded = expand_repairable_subchain(path[2], (*path, branch), rejected, [], lambda *_: False)
        self.assertEqual(set(expanded), set(path[:4]))

    def test_wider_repair_is_tried_before_unrelated_windows_consume_budget(self):
        from collections import deque
        from pcb.freeroute_delta import prioritize_expanded_repair

        seed = Trace("A", "F.Cu", 200_000, (1, 0), (2, 1))
        neighbor = Trace("A", "F.Cu", 200_000, (0, 0), (1, 0))
        other = Trace("B", "F.Cu", 200_000, (0, 0), (1, 0))
        queue = deque([(other, False)])
        prioritize_expanded_repair(queue, seed, (seed,), (seed, neighbor))
        self.assertEqual(queue.popleft(), (seed, True))
        self.assertEqual(queue.popleft(), (other, False))
        prioritize_expanded_repair(queue, seed, (seed,), (seed,))
        self.assertFalse(queue)

    def test_repair_window_does_not_consume_other_widths_or_old_contacts(self):
        from pcb.freeroute_delta import expand_repairable_subchain

        first = Trace("A", "F.Cu", 200_000, (0, 0), (1_000_000, 0))
        bad = Trace("A", "F.Cu", 200_000, (1_000_000, 0), (2_000_000, 1_000_000))
        other_width = Trace("A", "F.Cu", 300_000, (2_000_000, 1_000_000), (3_000_000, 1_000_000))
        old = [Trace("A", "F.Cu", 200_000, (0, -1_000_000), (0, 0))]
        expanded = expand_repairable_subchain(bad, (first, bad, other_width), {bad}, old, lambda *_: False)
        self.assertEqual(set(expanded), {first, bad})


if __name__ == "__main__":
    unittest.main()
