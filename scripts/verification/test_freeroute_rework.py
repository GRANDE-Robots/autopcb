"""Tests for coordinated external repair safeguards."""

from contextlib import redirect_stderr, redirect_stdout
import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from pcb.freeroute_delta import Trace, Via
from pcb.net_routing import fast_rework as api
from core.compat import strict_zip


class CoordinatedRepairTests(unittest.TestCase):
    def test_local_copper_ground_repair_is_followed_by_an_independent_check(self):
        for policy in (None, {"working_scope": {}}):
            calls = []
            def native(function, *_args, **_kwargs):
                calls.append(function)
                if function is api.prepare_ground_access:
                    return {"proposed_stitches": 1, "before": 2, "after": 0}
                if function is api.prepare_project:
                    return {"drc": {"open_connections": 0}}
                self.fail(f"Unexpected operation: {function}")
            with patch.object(api, "run_native", side_effect=native):
                checked = api.checked_copper_repair(Path("."), ground_policy=policy,
                                                    progress=lambda _: None)
            self.assertEqual(checked["drc"]["open_connections"], 0)
            self.assertEqual(calls, ([api.prepare_ground_access] if policy is not None else [])
                             + [api.prepare_project])

    def test_external_optimizer_selection_still_runs_the_proposal_filter(self):
        for optimize in (False, True):
            options = []
            calls = []
            def native(function, *args, **kwargs):
                calls.append(function)
                if function is api.preview:
                    options.append(kwargs)
                    return {"trial_board": "candidate.kicad_pcb"}
                if function is api._roundtrip:
                    return Path("comparison.kicad_pcb")
                if function is api.filter_proposal:
                    return {"status": "checked"}
                self.fail(f"Unexpected operation: {function}")
            with patch.object(api, "run_native", side_effect=native):
                result = api._candidate_batch_owned(
                    Path("."), Path("router"), 60, 30, (), lambda _: None,
                    engine="fastroute", targets=("POWER",), router_python="auto", grid_mm=None,
                    expected_tool=None, minimum_widths=None, trial_root=Path("."), optimize=optimize)
            self.assertEqual(result, {"status": "checked"})
            self.assertEqual(options[0]["skip_optimizer"], not optimize)
            self.assertIn(api.filter_proposal, calls)

    def test_ground_addition_permission_never_allows_old_ground_or_protected_copper_to_change(self):
        old = Trace("GND", "B.Cu", 500_000, (0, 0), (1_000_000, 0))
        join = Via("GND", (1_000_000, 0), 600_000, 300_000)
        protected = Trace("USB_DP", "F.Cu", 200_000, (0, 1_000_000), (2_000_000, 1_000_000))
        before = [old, protected]
        excluded = ("GND", "USB_DP")
        self.assertFalse(api.rework_copper_preserved(before, [*before, join], excluded))
        self.assertTrue(api.rework_copper_preserved(before, [*before, join], excluded,
                                                  additive_ground=True))
        changed = Trace("GND", "B.Cu", 600_000, old.start, old.end)
        for after in ([protected, join], [changed, protected, join], [old, join]):
            self.assertFalse(api.rework_copper_preserved(before, after, excluded,
                                                       additive_ground=True))
        with self.assertRaisesRegex(ValueError, "boolean"):
            api.rework_copper_preserved(before, before, excluded, additive_ground=1)

    def test_ground_reconnection_after_tree_restoration_requires_a_fresh_native_check(self):
        for stitches in (0, 1):
            with self.subTest(stitches=stitches):
                calls = []
                before = {"drc": {"open_connections": 4}}
                after = {"drc": {"open_connections": 3}}
                def native(function, *args, **kwargs):
                    calls.append(function)
                    if function is api.restore_unimproved_trees:
                        return ()
                    if function is api.prepare_ground_access:
                        return {"proposed_stitches": stitches, "before": 4, "after": 4 - stitches}
                    if function is api.prepare_project:
                        return after if len([f for f in calls if f is api.prepare_project]) > 1 else before
                    self.fail(f"Unexpected operation: {function}")
                with (patch.object(api, "run_native", side_effect=native),
                      patch.object(api, "interim_drc_acceptable", return_value=True)):
                    result = api.checked_tree_fallback(
                        Path("."), Path("prior"), ("SIGNAL",), {}, ground_policy={"working_scope": {}},
                        progress=lambda _message: None)
                self.assertEqual(result, after if stitches else before)
                self.assertIn(api.prepare_ground_access, calls)
                self.assertEqual(calls.count(api.prepare_project), 2 if stitches else 1)

    def test_ground_first_reconnects_a_native_island_without_moving_footprints(self):
        import pcbnew
        from core.kicad_sexpr import parse
        from pcb.route_validation import _canonical
        from pcb.route_optimizer import mm

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "v4.1.kicad_pcb"
            board = pcbnew.BOARD()
            board.SetCopperLayerCount(6)
            for name in ("GND", "USB_DP"):
                board.Add(pcbnew.NETINFO_ITEM(board, name))
            for ref, x, y, net, through in (("J1", 5, 5, "GND", False),
                                           ("J2", 10, 5, "GND", True),
                                           ("J3", 15, 15, "USB_DP", False)):
                fp = pcbnew.FOOTPRINT(board)
                fp.SetReference(ref)
                pad = pcbnew.PAD(fp)
                pad.SetNumber("1")
                pad.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
                pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
                pad.SetSize(pcbnew.VECTOR2I(mm(1.5 if through else 1), mm(1.5 if through else 1)))
                pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH if through else pcbnew.PAD_ATTRIB_SMD)
                pad.SetLayerSet(pcbnew.LSET.AllCuMask() if through else pcbnew.LSET.FrontMask())
                if through:
                    pad.SetDrillSize(pcbnew.VECTOR2I(mm(.8), mm(.8)))
                pad.SetNetCode(board.FindNet(net).GetNetCode())
                fp.Add(pad)
                board.Add(fp)
            corners = ((0, 0), (20, 0), (20, 20), (0, 20))
            for first, last in zip(corners, (*corners[1:], corners[0])):
                edge = pcbnew.PCB_SHAPE(board)
                edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
                edge.SetLayer(pcbnew.Edge_Cuts)
                edge.SetStart(pcbnew.VECTOR2I(mm(first[0]), mm(first[1])))
                edge.SetEnd(pcbnew.VECTOR2I(mm(last[0]), mm(last[1])))
                board.Add(edge)
            zone = pcbnew.ZONE(board)
            zone.SetLayer(pcbnew.In1_Cu)
            zone.SetNetCode(board.FindNet("GND").GetNetCode())
            zone.Outline().NewOutline()
            for x, y in corners:
                zone.Outline().Append(mm(x), mm(y))
            board.Add(zone)
            pcbnew.SaveBoard(str(path), board)
            path.with_suffix(".kicad_pro").write_text(json.dumps({"net_settings": {
                "netclass_patterns": [{"netclass": "USB_DATA", "pattern": "USB_DP"}]}}))
            footprints = tuple(_canonical(fp) for fp in parse(path.read_text()).children("footprint"))
            policy = {"working_scope": {"kind": "connectivity", "classes": ["Default"],
                                         "manufacturing_approved": False}}
            result = api.prepare_ground_access(root, policy)
            self.assertGreater(result["before"], result["after"])
            self.assertGreater(result["proposed_stitches"], 0)
            self.assertFalse(result["electrically_verified"])
            self.assertEqual(footprints,
                             tuple(_canonical(fp) for fp in parse(path.read_text()).children("footprint")))
            with self.assertRaisesRegex(ValueError, "explicit working"):
                api.prepare_ground_access(root, {})

    def setUp(self):
        # Pipeline tests use byte sentinels and mock native DRC. Whole-net
        # stability itself has independent real-file tests in test_autoroute_resume.
        proof = patch.object(api, "stable_connection_items", return_value={})
        self.addCleanup(proof.stop)
        proof.start()

    def test_tree_renewal_is_private_and_preserves_complete_footprints(self):
        import pcbnew
        from pcb.net_routing.protection import ProtectedState
        from verification.test_route_optimizer import RouteOptimizerTests
        from pcb.route_optimizer import mm

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = RouteOptimizerTests.make_board(root, crossing=True)
            board = pcbnew.LoadBoard(str(source))
            track = pcbnew.PCB_TRACK(board)
            track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            track.SetLayer(pcbnew.F_Cu)
            track.SetWidth(mm(.2))
            track.SetStart(pcbnew.VECTOR2I(mm(2), mm(2)))
            track.SetEnd(pcbnew.VECTOR2I(mm(3), mm(2)))
            board.Add(track)
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            result = api.compile_tree_renewal(root, ("SIGNAL",), ("Default",), (), 0, 8)
            self.assertIsNotNone(result)
            self.assertEqual(result.affected, ("SIGNAL",))
            self.assertEqual(result.items, 1)
            self.assertIsNone(api.compile_tree_renewal(
                root, ("SIGNAL",), ("Default",), (), 0, 8,
                tried_plans=(result.attempt_key,)))
            guard = ProtectedState.capture(before, b"sch")
            self.assertEqual(guard.differences(result.board, b"sch"), ())
            self.assertEqual(source.read_bytes(), before)
            self.assertIsNone(api.compile_tree_renewal(root, ("SIGNAL",), ("Default",), ("SIGNAL",), 0, 8))
            board.Add(pcbnew.NETINFO_ITEM(board, "NEIGHBOR"))
            neighbor = pcbnew.PCB_TRACK(board)
            neighbor.SetNetCode(board.FindNet("NEIGHBOR").GetNetCode())
            neighbor.SetLayer(pcbnew.In1_Cu)
            neighbor.SetWidth(mm(.2))
            neighbor.SetStart(pcbnew.VECTOR2I(mm(4), mm(2)))
            neighbor.SetEnd(pcbnew.VECTOR2I(mm(5), mm(2)))
            board.Add(neighbor)
            pcbnew.SaveBoard(str(source), board)
            broadened = api.compile_tree_renewal(root, ("SIGNAL",), ("Default",), (), 0, 8,
                                                include_connected=True)
            self.assertIn("NEIGHBOR", broadened.affected)
            self.assertEqual(guard.differences(broadened.board, b"sch"), ())
            protected_neighbor = api.compile_tree_renewal(
                root, ("SIGNAL",), ("Default",), ("NEIGHBOR",), 0, 8, include_connected=True)
            self.assertNotIn("NEIGHBOR", protected_neighbor.affected)
            track.SetLocked(True)
            pcbnew.SaveBoard(str(source), board)
            self.assertIsNone(api.compile_tree_renewal(root, ("SIGNAL",), ("Default",), (), 0, 8))

    def test_tree_neighbors_use_actual_same_layer_copper_not_overlapping_diagonal_boxes(self):
        import pcbnew
        from verification.test_route_optimizer import RouteOptimizerTests
        from pcb.route_optimizer import mm
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = RouteOptimizerTests.make_board(root)
            board = pcbnew.LoadBoard(str(source))
            for name, layer, start, end in (
                ('SIGNAL', pcbnew.F_Cu, (1, 1), (9, 9)),
                ('A_FAR', pcbnew.F_Cu, (1, 7), (3, 9)),
                ('B_OTHER_LAYER', pcbnew.B_Cu, (2, 2), (8, 8)),
                ('Z_NEAR', pcbnew.F_Cu, (4, 4.7), (4.2, 4.7)),
            ):
                net = board.FindNet(name)
                if net is None:
                    net = pcbnew.NETINFO_ITEM(board, name)
                    board.Add(net)
                track = pcbnew.PCB_TRACK(board)
                track.SetNetCode(net.GetNetCode())
                track.SetLayer(layer)
                track.SetWidth(mm(.2))
                track.SetStart(pcbnew.VECTOR2I(mm(start[0]), mm(start[1])))
                track.SetEnd(pcbnew.VECTOR2I(mm(end[0]), mm(end[1])))
                board.Add(track)
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            plan = api.compile_tree_renewal(root, ('SIGNAL',), ('Default',), (), 0, 2,
                                            include_connected=True)
            self.assertEqual(plan.affected, ('SIGNAL', 'Z_NEAR'))
            self.assertEqual(source.read_bytes(), original)

    def test_failed_tree_is_restored_but_a_completed_tree_survives(self):
        import pcbnew
        from pcb.freeroute_delta import read_copper
        from pcb.route_optimizer import mm
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = RouteOptimizerTests.make_board(root)
            board = pcbnew.LoadBoard(str(source))
            def add(a, b):
                track = pcbnew.PCB_TRACK(board)
                track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
                track.SetLayer(pcbnew.F_Cu)
                track.SetWidth(mm(.2))
                track.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1])))
                track.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
                board.Add(track)
                return track
            old = add((2, 2), (3, 2))
            pcbnew.SaveBoard(str(source), board)
            prior = root / "prior.kicad_pcb"
            prior.write_bytes(source.read_bytes())
            board.Remove(old)
            partial = add((2, 2), (4, 2))
            pcbnew.SaveBoard(str(source), board)
            neutral = source.read_bytes()
            self.assertEqual(api.restore_unimproved_trees(root, prior, ("SIGNAL",)), ("SIGNAL",))
            self.assertEqual(read_copper(source), read_copper(prior))
            source.write_bytes(neutral)
            self.assertEqual(api.restore_unimproved_trees(root, prior, ("SIGNAL",), keep_neutral=True), ())
            self.assertEqual(source.read_bytes(), neutral)
            board.Remove(partial)
            add((2, 2), (5, 2))
            add((5, 2), (8, 5))
            pcbnew.SaveBoard(str(source), board)
            completed = source.read_bytes()
            self.assertEqual(api.restore_unimproved_trees(root, prior, ("SIGNAL",)), ())
            self.assertEqual(source.read_bytes(), completed)

    def test_new_native_error_attribution_cannot_select_a_protected_net(self):
        import pcbnew
        from verification.test_route_optimizer import RouteOptimizerTests
        with tempfile.TemporaryDirectory() as folder:
            source = RouteOptimizerTests.make_board(Path(folder), crossing=True)
            board = pcbnew.LoadBoard(str(source))
            item = list(board.GetTracks())[0]
            uid = item.m_Uuid.AsString()
            baseline = {"violation_keys": []}
            current = {"violation_keys": [["clearance", "error", [uid, "unknown"]]]}
            self.assertEqual(api.conflicting_replacement_nets(source, baseline, current, ("OTHER",)), ("OTHER",))
            self.assertEqual(api.conflicting_replacement_nets(source, baseline, current, ("SIGNAL",)), ())
            self.assertEqual(api.conflicting_replacement_nets(source, current, current, ("OTHER",)), ())

    def test_private_protected_anchor_needs_the_exact_unmoved_pad_pose(self):
        import pcbnew
        from pcb.net_routing.relocation import protected_anchor_pose
        from pcb.route_optimizer import mm
        from verification import test_net_routing as fixture
        from verification import test_route_optimizer as boards

        with tempfile.TemporaryDirectory() as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            board.Add(pcbnew.NETINFO_ITEM(board, "PROTECTED"))
            part = fixture.NativeNetRoutingTests.add_pad(board, "R50", (3, 3), bottom=True)
            anchor = pcbnew.PAD(part)
            anchor.SetNumber("2")
            anchor.SetSize(pcbnew.VECTOR2I(mm(.5), mm(.5)))
            anchor.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            anchor.SetLayerSet(pcbnew.LSET.BackMask())
            anchor.SetPosition(pcbnew.VECTOR2I(mm(4), mm(3)))
            anchor.SetNetCode(board.FindNet("PROTECTED").GetNetCode())
            part.Add(anchor)
            pcbnew.SaveBoard(str(source), board)
            pose = protected_anchor_pose(part, {"PROTECTED"})
            candidate = {"nets": [{"name": "SIGNAL"}], "movable": [{"reference": "R50",
                "positions_mm": [[pose.x_mm, pose.y_mm]], "rotations_deg": [pose.angle_deg]}]}
            self.assertNotIn("R50", api.eligible_signal_resistors(board, {"kicad_default"}, {"PROTECTED"}))
            self.assertIn("R50", api.eligible_signal_resistors(
                board, {"kicad_default"}, {"PROTECTED"}, allow_protected_anchor=True))
            self.assertIsNotNone(api.placement_poses(
                board, candidate, {"kicad_default"}, {"PROTECTED"}, allow_protected_anchor=True))
            candidate["movable"][0]["positions_mm"][0][0] += .001
            self.assertIsNone(api.placement_poses(
                board, candidate, {"kicad_default"}, {"PROTECTED"}, allow_protected_anchor=True))
            self.assertIsNone(api.placement_poses(
                board, candidate, {"kicad_default"}, {"PROTECTED", "SIGNAL"},
                allow_protected_anchor=True))

    def test_cli_passes_the_selected_krt_release_to_rework(self):
        with (patch.object(api, "run_one", return_value={"status": "no_rework_candidate"}) as run,
              patch.object(api, "record_unimproved_batch"), redirect_stdout(io.StringIO())):
            code = api.main(["--project", ".", "--run", "a" * 32, "--batches", "1",
                             "--focus-open-class", "SIGNAL", "--krt-router", "my-krt"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs["krt_router"], Path("my-krt"))

    def test_rework_policy_can_enable_bounded_obstruction_diagnosis(self):
        source = {"recovery": {"obstruction_trials": 2, "obstruction_seconds": 3.0}}
        fast = api.rework_policy(source, bottom_placement=False, diagnose_obstructions=False)
        diagnosed = api.rework_policy(source, bottom_placement=False, diagnose_obstructions=True)
        default = api.rework_policy({}, bottom_placement=False, diagnose_obstructions=True)
        placement = api.rework_policy(source, bottom_placement=True, diagnose_obstructions=True)
        self.assertEqual(fast["recovery"]["obstruction_trials"], 0)
        self.assertEqual(diagnosed["recovery"]["obstruction_trials"], 2)
        self.assertEqual(diagnosed["recovery"]["obstruction_seconds"], 3.0)
        self.assertNotIn("obstruction_trials", default["recovery"])
        self.assertEqual(placement["recovery"]["rip_up_candidates"], 0)
        self.assertEqual(source, {"recovery": {"obstruction_trials": 2, "obstruction_seconds": 3.0}})

    def test_cli_passes_obstruction_diagnosis_to_each_batch(self):
        with (patch.object(api, "run_one", return_value={"status": "no_rework_candidate"}) as run,
              patch.object(api, "record_unimproved_batch"), redirect_stdout(io.StringIO())):
            code = api.main(["--project", ".", "--run", "a" * 32, "--batches", "1",
                             "--focus-open-class", "SIGNAL", "--diagnose-obstructions",
                             "--followup-engine", "krt"])
        self.assertEqual(code, 0)
        self.assertTrue(run.call_args.kwargs["diagnose_obstructions"])
        self.assertEqual(run.call_args.kwargs["followup_engine"], "krt")

    def test_cli_passes_explicit_package_escape_search(self):
        with (patch.object(api, "run_one", return_value={"status": "no_rework_candidate"}) as run,
              patch.object(api, "record_unimproved_batch"), redirect_stdout(io.StringIO())):
            code = api.main(["--project", ".", "--run", "a" * 32, "--batches", "1",
                             "--focus-open-class", "SIGNAL", "--search-pad-neckdown"])
        self.assertEqual(code, 0)
        self.assertTrue(run.call_args.kwargs["search_pad_neckdown"])

    def test_cli_accepts_freerouting_for_coordinated_followup(self):
        with (patch.object(api, "run_one", return_value={"status": "no_rework_candidate"}) as run,
              patch.object(api, "record_unimproved_batch"), redirect_stdout(io.StringIO())):
            code = api.main(["--project", ".", "--run", "a" * 32, "--batches", "1",
                             "--focus-open-class", "SIGNAL", "--followup-engine", "Freerouting"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs["followup_engine"], "Freerouting")

    def test_cli_passes_a_bounded_followup_count(self):
        with (patch.object(api, "run_one", return_value={"status": "no_rework_candidate"}) as run,
              patch.object(api, "record_unimproved_batch"), redirect_stdout(io.StringIO())):
            code = api.main(["--project", ".", "--run", "a" * 32, "--batches", "1",
                             "--focus-open-class", "SIGNAL", "--followup-rounds", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs["followup_rounds"], 2)

    def test_failed_repair_keeps_specific_reasons_for_human_progress(self):
        failure = api._failed("a" * 32, "followup", {
            "status": "filter_stalled", "repair_blockers": {"SIG": "local search exhausted"},
        })
        self.assertEqual(failure["repair_blockers"], ("SIG",))
        self.assertEqual(failure["repair_reasons"], {"SIG": "local search exhausted"})

    def test_candidate_summary_reports_affected_nets_and_rule_types(self):
        event = {"event": "pass", "opens": 188, "rejected_items": 3,
                 "affected_nets": {"SIG": 3}, "drc_reasons": {"clearance": 2}, "style_items": 1,
                 "style_reasons": {"staircases": 1}}
        self.assertEqual(api.candidate_progress(event),
                         "Checking candidate: 188 opens; 3 proposed items need repair "
                         "(nets: SIG; DRC: clearance=2; geometry: staircases=1)")

    def test_bottom_ground_anchor_requires_exact_protected_pad_pose(self):
        import pcbnew
        from pcb.net_routing.relocation import ground_anchor_pose
        from pcb.route_optimizer import mm
        from verification import test_net_routing as fixture
        from verification import test_route_optimizer as boards

        with tempfile.TemporaryDirectory() as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            board.Add(pcbnew.NETINFO_ITEM(board, "GND"))
            part = fixture.NativeNetRoutingTests.add_pad(board, "R50", (3, 3), bottom=True)
            ground = pcbnew.PAD(part)
            ground.SetNumber("2")
            ground.SetSize(pcbnew.VECTOR2I(mm(.5), mm(.5)))
            ground.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            ground.SetLayerSet(pcbnew.LSET.BackMask())
            ground.SetPosition(pcbnew.VECTOR2I(mm(4), mm(3)))
            ground.SetNetCode(board.FindNet("GND").GetNetCode())
            part.Add(ground)
            pose = ground_anchor_pose(part, {"GND"})
            candidate = {"nets": [{"name": "SIGNAL"}],
                         "movable": [{"reference": "R50",
                                      "positions_mm": [[pose.x_mm, pose.y_mm]],
                                      "rotations_deg": [pose.angle_deg]}]}
            self.assertNotIn("R50", api.eligible_signal_resistors(board, {"kicad_default"}, {"GND"}))
            self.assertIn("R50", api.eligible_signal_resistors(
                board, {"kicad_default"}, {"GND"}, allow_ground_anchor=True))
            self.assertIsNotNone(api.placement_poses(
                board, candidate, {"kicad_default"}, {"GND"}, allow_ground_anchor=True))
            candidate["movable"][0]["positions_mm"][0][0] += .001
            self.assertIsNone(api.placement_poses(
                board, candidate, {"kicad_default"}, {"GND"}, allow_ground_anchor=True))
            candidate["movable"][0]["positions_mm"][0][0] = pose.x_mm
            group = pcbnew.PCB_GROUP(board)
            group.SetName("single resistor")
            board.Add(group)
            group.AddItem(part)
            pcbnew.SaveBoard(str(source), board)
            self.assertIn("R50", api.eligible_signal_resistors(
                board, {"kicad_default"}, {"GND"}, allow_ground_anchor=True))
            self.assertIsNotNone(api.placement_poses(
                board, candidate, {"kicad_default"}, {"GND"}, allow_ground_anchor=True))
            group.AddItem(board.FindFootprintByReference("J1"))
            pcbnew.SaveBoard(str(source), board)
            self.assertNotIn("R50", api.eligible_signal_resistors(
                board, {"kicad_default"}, {"GND"}, allow_ground_anchor=True))
            self.assertIsNone(api.placement_poses(
                board, candidate, {"kicad_default"}, {"GND"}, allow_ground_anchor=True))

    def test_interim_rework_accepts_only_identical_existing_native_errors(self):
        def finding(actual):
            return json.dumps({
                "type": "copper_edge_clearance", "severity": "error",
                "description": f"required 0.5 mm; actual {actual} mm",
                "items": [{"uuid": "pad"}, {"uuid": "hole"}],
            }, sort_keys=True)

        baseline = {"errors": 1, "exclusions": 0, "parity_findings": 0,
                    "violation_keys": [["copper_edge_clearance", "error", ["pad", "hole"]]],
                    "error_details": [finding("0.4214")]}
        self.assertTrue(api.interim_drc_acceptable(baseline, dict(baseline)))
        self.assertFalse(api.interim_drc_acceptable(
            baseline, {**baseline, "error_details": [finding("0.3000")]},
        ))
        self.assertFalse(api.interim_drc_acceptable(baseline, {**baseline, "exclusions": 1}))
        self.assertFalse(api.interim_drc_acceptable(baseline, {**baseline, "error_details": []}))
        self.assertFalse(api.interim_drc_acceptable(baseline, {**baseline, "ignored_checks": ["clearance"]}))
        self.assertFalse(api.interim_drc_acceptable(baseline, {**baseline, "errors": 0}))

    def test_interim_rework_allows_identified_unchanged_parity_only(self):
        keys = [["net_conflict", "warning", ["pad-1"]]]
        baseline = {"errors": 0, "exclusions": 0, "ignored_checks": [],
                    "violation_keys": [], "error_details": [],
                    "parity_findings": 1, "parity_keys": keys,
                    "parity_blocked_nets": ["ACTUAL", "EXPECTED"]}
        self.assertTrue(api.interim_drc_acceptable(baseline, dict(baseline)))
        self.assertEqual(api.parity_protected_nets(baseline), ("ACTUAL", "EXPECTED"))
        self.assertFalse(api.interim_drc_acceptable(
            baseline, {**baseline, "parity_keys": [["net_conflict", "warning", ["other"]]]}))
        self.assertFalse(api.interim_drc_acceptable(
            baseline, {**baseline, "parity_blocked_nets": ["ACTUAL"]}))
        self.assertFalse(api.interim_drc_acceptable(
            {**baseline, "parity_blocked_nets": None}, dict(baseline)))
        self.assertFalse(api.interim_drc_acceptable(
            baseline, {**baseline, "parity_findings": 0}))
        with self.assertRaises(RuntimeError):
            api.parity_protected_nets({**baseline, "parity_keys": []})

    def test_power_mux_configuration_nets_are_protected(self):
        board, mux = MagicMock(), MagicMock()
        mux.GetReference.return_value = "U5"
        mux.GetValue.return_value = "TPS2121RUXR | POWER MUX"
        mux.GetFieldsText.return_value = {"Description": "Power-path selector"}
        pads = [MagicMock(), MagicMock()]
        pads[0].GetNetname.return_value = "TPS_ILM"
        pads[1].GetNetname.return_value = "TPS_SS"
        mux.Pads.return_value = pads
        board.GetFootprints.return_value = [mux]
        board.GetNetsByName.return_value = {}
        self.assertEqual(api.protection_nets(board), {"TPS_ILM", "TPS_SS"})

    def test_power_monitor_regulator_and_motor_enable_nets_are_protected(self):
        for value, description, net in (
            ("TLV75801PDBVR", "500 mA adjustable timing LDO", "TIMING_LDO_FB"),
            ("TPS3702AX33DDCR", "3V3 window monitor", "FIELD_VALID"),
            ("SN74AHCT1G126DCKR", "Independent motor L_EN buffer", "FIELD_VALID"),
            ("TLV76033DBZR", "USB-derived control supply", "USB_CTRL_3V3"),
            ("INA180A2IDBVR", "Current Sense Amplifier", "SENSE_RAW"),
            ("SN74AHCT244PWR | 8-CH 5V DRIVER", "Timing buffer", "MTI_CLOCKSYNC_DRV"),
            ("SN74AHCT126PWR | 4-CH 5V DRIVER", "Timing buffer", "MTI_SYNCIN_DRV"),
            ("PART | 3.3 V DRIVER", "Field buffer", "TIMING_OUT"),
            ("TUSB320IRWBR | CC SINK", "USB-C UFP controller", "USB_VBUS_DET"),
            ("TS3USB221ARSER", "480 Mbps USB2 data disconnect", "USB_HOST_OE_N"),
            ("TMUX1511PWR", "four SPST switches", "BTS_R_IS_FILT"),
        ):
            with self.subTest(description=description):
                board, part, pad = MagicMock(), MagicMock(), MagicMock()
                part.GetReference.return_value = "U99"
                part.GetValue.return_value = value
                part.GetFieldsText.return_value = {"Description": description}
                pad.GetNetname.return_value = net
                part.Pads.return_value = [pad]
                board.GetFootprints.return_value = [part]
                board.GetNetsByName.return_value = {}
                self.assertIn(net, api.protection_nets(board))

    def test_motor_encoder_feedback_nets_are_protected(self):
        board = MagicMock()
        board.GetFootprints.return_value = []
        board.GetNetsByName.return_value = {"ENC_A_MCU": None, "ENC_B_RAW": None}
        self.assertEqual(api.protection_nets(board), {"ENC_A_MCU", "ENC_B_RAW"})

    def test_transistor_terminal_nets_are_protected_without_relying_on_their_names(self):
        board, transistor = MagicMock(), MagicMock()
        transistor.GetReference.return_value = "Q1"
        transistor.GetValue.return_value = "BSS138K"
        transistor.GetFieldsText.return_value = {"Description": "N-channel MOSFET"}
        pads = [MagicMock(), MagicMock(), MagicMock()]
        for pad, name in strict_zip(pads, ("FIELD_VALID", "GND", "U3_OE_N")):
            pad.GetNetname.return_value = name
        transistor.Pads.return_value = pads
        board.GetFootprints.return_value = [transistor]
        board.GetNetsByName.return_value = {}
        self.assertEqual(api.protection_nets(board), {"FIELD_VALID", "GND", "U3_OE_N"})

    def test_preparation_cache_reuses_only_matching_checked_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            board = directory / "v4.1.kicad_pcb"
            board.write_bytes(b"candidate")
            cache = api.RepairPreparationCache()
            state = {"blocked_nets": {"PAIR": "deferred"}}
            key = api.preparation_key({"board": "hash"}, {"tool": "hash"}, "policy",
                                      b"candidate", ("SIGNAL",), state)
            calls = []

            def native(function, *_args, **_kwargs):
                calls.append(function)
                if function is api._focus_deferred:
                    board.write_bytes(b"refilled")
                    return (("TARGET",), ("OTHER",))
                if function is api.prepare_project:
                    return {"drc": {"open_connections": 3}}
                self.fail("Unexpected native operation")

            with patch.object(api, "run_native", side_effect=native):
                first = api.prepare_repair(directory, state, ("SIGNAL",), key, cache, lambda _: None)
                board.write_bytes(b"discarded private trial")
                second = api.prepare_repair(directory, state, ("SIGNAL",), key, cache, lambda _: None)
                self.assertEqual(board.read_bytes(), b"refilled")
                self.assertEqual(first, second)
                self.assertEqual(len(calls), 2)
                changed = api.preparation_key({"board": "changed"}, {"tool": "hash"}, "policy",
                                              b"candidate", ("SIGNAL",), state)
                api.prepare_repair(directory, state, ("SIGNAL",), changed, cache, lambda _: None)
                self.assertEqual(len(calls), 4)

    def test_preparation_key_changes_with_tools_policy_preview_and_scope(self):
        def key(inputs=None, tools=None, policy="p", board=b"a", focus=("SIGNAL",), blocked=None):
            return api.preparation_key(inputs or {"input": "a"}, tools or {"tool": "a"},
                                       policy, board, focus, {"blocked_nets": blocked or {}})

        baseline = key()
        for changed in (key(inputs={"input": "b"}), key(tools={"tool": "b"}),
                        key(policy="q"), key(board=b"b"), key(focus=("FIELD_SIGNAL",)),
                        key(blocked={"NET": "reason"})):
            self.assertNotEqual(baseline, changed)

    def test_orders_failed_nets_first_and_splits_without_mutating_queue(self):
        queue = [[{"shared": {}, "nets": [{"name": "A"}, {"name": "B"}]}],
                 [{"shared": {}, "nets": [{"name": "A"}, {"name": "C"}]}],
                 [{"shared": {}, "nets": [{"name": "D"}], "rip_up": {"A": []}}]]
        result = api.ordered_requests(queue, ("A", "B", "C", "D"), ("C",))
        self.assertEqual([item["nets"][0]["name"] for item in result], ["C", "A", "B"])
        self.assertEqual(len(queue[0][0]["nets"]), 2)

    def test_repair_queue_recovers_unfinished_nets_after_queue_is_partly_consumed(self):
        preparation = {
            "unqualified_nets": ["POWER"],
            "reference_copper": {"high_speed_nets": ["PAIR"]},
            "work": {"items": [
                {"name": name, "priority": 1, "window": [0, 0, 4, 4], "island_count": 2}
                for name in ("A", "B", "POWER", "PAIR")
            ]},
        }
        old = [[{"shared": {}, "nets": [{"name": "B"}, {"name": "POWER"}]}]]
        original = json.loads(json.dumps(old))
        queue = api.repair_work_queue(old, preparation, {})
        ordered = api.ordered_requests(queue, ("A", "B", "POWER", "PAIR"), ())
        self.assertEqual([item["nets"][0]["name"] for item in ordered], ["B", "A"])
        self.assertEqual(old, original)

    def test_replacement_cannot_narrow_selected_tracks_or_vias(self):
        old = [Trace("A", "F.Cu", 250000, (0, 0), (1000000, 0))]
        floors = {"A": (250000, 600000, 300000)}
        wider = [*old, Trace("A", "B.Cu", 300000, (0, 0), (0, 1000000)),
                 Via("A", (0, 0), 600000, 300000)]
        self.assertTrue(api.dimensions_preserved(old, wider, floors))
        self.assertFalse(api.dimensions_preserved(old, [*old, Trace("A", "B.Cu", 200000,
                                                                   (0, 0), (0, 1000000))], floors))
        self.assertFalse(api.dimensions_preserved(old, [*old, Via("A", (0, 0), 550000, 300000)], floors))
        self.assertFalse(api.dimensions_preserved(old, [*old, Via("A", (0, 0), 600000, 250000)], floors))
        self.assertTrue(api.dimensions_preserved(old, old, floors))

    def test_unchanged_copper_detection_ignores_order_but_counts_duplicates(self):
        trace = Trace("A", "F.Cu", 250000, (0, 0), (1000000, 0))
        via = Via("A", (0, 0), 600000, 300000)
        self.assertTrue(api.same_copper([trace, via], [via, trace]))
        self.assertFalse(api.same_copper([trace, via], [trace, via, via]))
        self.assertFalse(api.same_copper([trace], [Trace("A", "F.Cu", 300000,
                                                          (0, 0), (1000000, 0))]))

    def test_compiler_skips_ground_and_outside_classes_then_builds_checked_plan(self):
        import pcbnew

        board, item = MagicMock(), MagicMock()
        board.GetTracks.return_value = [item]
        item.m_Uuid.AsString.return_value = "track-a"
        item.GetNetname.return_value = "A"
        item.GetLayer.return_value = pcbnew.F_Cu
        board.FindNet.side_effect = lambda name: (
            MagicMock(GetNetClassName=lambda: {"A": "SIGNAL", "GND": "GROUND", "TARGET": "SIGNAL"}[name])
            if name in {"A", "GND", "TARGET"} else None
        )
        board.GetLayerID.return_value = pcbnew.F_Cu
        rejected = {"rip_up": {"GND": [{"uuid": "track-a"}]}}
        accepted = {"rip_up": {"A": [{"uuid": "track-a"}]}}
        compiled = {"requests": [rejected, accepted],
                    "candidates": [{"estimated_rework_cost": 1.0}, {"estimated_rework_cost": 2.0}]}
        planner = MagicMock()
        planner.plan.return_value.after = b"candidate"
        queue = [[{"shared": {}, "nets": [{"name": "TARGET"}]}]]
        with (patch.object(pcbnew, "LoadBoard", return_value=board),
              patch.object(api, "compile_reroutes", return_value=compiled),
              patch.object(api, "copper_width", return_value=250000),
              patch.object(api, "copper_removal_changes", return_value=[]),
              patch.object(api, "CampaignPlanner", return_value=planner)):
            plan = api.compile_candidate(Path("."), queue, {}, ("TARGET",), (), ("SIGNAL",), (), 0)
        self.assertEqual(plan.target, "TARGET")
        self.assertEqual(plan.affected, ("A", "TARGET"))
        self.assertEqual(plan.floors, {"A": (250000, 0, 0)})
        self.assertEqual(plan.board, b"candidate")

    def test_explicit_working_power_scope_allows_copper_repair_without_a_blanket_trunk_floor(self):
        import pcbnew
        board, trunk, neck = MagicMock(), MagicMock(), MagicMock()
        board.GetTracks.return_value = [trunk, neck]
        board.GetFootprints.return_value = []
        board.GetNetsByName.return_value = {'POWER': 1, 'GND': 2}
        board.FindNet.side_effect = lambda name: (
            MagicMock(GetNetClassName=lambda: {'POWER': 'CORE_POWER', 'GND': 'GROUND'}[name])
            if name in {'POWER', 'GND'} else None)
        for item, uid in ((trunk, 'trunk'), (neck, 'neck')):
            item.m_Uuid.AsString.return_value = uid
            item.GetNetname.return_value = 'POWER'
            item.GetLayer.return_value = pcbnew.B_Cu
        compiled = {'requests': [{'rip_up': {'POWER': [{'uuid': 'trunk'}, {'uuid': 'neck'}]}}],
                    'candidates': [{'estimated_rework_cost': 2.0}]}
        planner = MagicMock()
        planner.plan.return_value.after = b'private-copper-only'
        queue = [[{'shared': {}, 'nets': [{'name': 'POWER'}]}]]
        with (patch.object(pcbnew, 'LoadBoard', return_value=board),
              patch.object(api, 'working_connectivity_classes', create=True,
                           return_value=frozenset({'CORE_POWER', 'GROUND'})),
              patch.object(api, 'protection_nets', return_value=set()),
              patch.object(api, 'compile_reroutes', return_value=compiled) as compile_routes,
              patch.object(api, 'copper_width', side_effect=lambda item, **_: 500000 if item is trunk else 300000),
              patch.object(api, 'copper_removal_changes', return_value=[]),
              patch.object(api, 'CampaignPlanner', return_value=planner)):
            plan = api.compile_candidate(Path('.'), queue, {}, ('POWER',), (), ('CORE_POWER',), (), 0)
            self.assertIsNotNone(plan)
            self.assertEqual(plan.floors, {'POWER': (0, 0, 0)})
            self.assertEqual(plan.moves, ())
            explicit = {'net_overrides': {'POWER': {'electrical': {'current_a': .65},
                                                     'width_mm': .75,
                                                     'via': {'size_mm': .8, 'drill_mm': .4}}}}
            qualified = api.compile_candidate(Path('.'), queue, explicit, ('POWER',), (),
                                               ('CORE_POWER',), (), 0)
            self.assertEqual(qualified.floors, {'POWER': (750000, 800000, 400000)})
            compile_routes.reset_mock()
            self.assertIsNone(api.compile_candidate(Path('.'), queue, {}, ('POWER',), (),
                                                     ('CORE_POWER',), ('POWER',), 0))
            compile_routes.assert_not_called()

    def test_bottom_placement_includes_every_affected_signal_net_and_native_pose(self):
        import pcbnew

        board, track, part = MagicMock(), MagicMock(), MagicMock()
        board.GetTracks.return_value = [track]
        track.m_Uuid.AsString.return_value = "track-a"
        track.GetNetname.return_value = "A"
        track.GetLayer.return_value = pcbnew.B_Cu
        track.GetLength.return_value = 1_000_000
        board.GetLayerID.return_value = pcbnew.B_Cu
        board.FindNet.return_value.GetNetClassName.return_value = "SIGNAL"
        board.FindFootprintByReference.return_value = part
        part.GetLayer.return_value = pcbnew.B_Cu
        part.IsLocked.return_value = False
        pads = []
        for name in ("A", "B"):
            pad = MagicMock()
            pad.GetNetname.return_value = name
            pad.GetAttribute.return_value = pcbnew.PAD_ATTRIB_SMD
            pad.IsOnLayer.side_effect = lambda layer: layer == pcbnew.B_Cu
            pads.append(pad)
        part.Pads.return_value = pads
        candidate = {"rip_up": {"A": [{"uuid": "track-a"}]},
                     "nets": [{"name": "TARGET"}, {"name": "A"}, {"name": "B"}],
                     "movable": [{"reference": "R50", "positions_mm": [[5.5, 10.0]],
                                  "rotations_deg": [90]}]}
        compiled = {"requests": [candidate], "candidates": [{"selection": "placement_hypothesis"}]}
        planner = MagicMock()
        planner.plan.return_value.after = b"moved-private-board"
        queue = [[{"shared": {}, "nets": [{"name": "TARGET"}]}]]
        with (patch.object(pcbnew, "LoadBoard", return_value=board),
              patch.object(api, "compile_reroutes", return_value=compiled),
              patch.object(api, "eligible_signal_resistors", return_value=("R50",)),
              patch.object(api, "copper_width", return_value=250000),
              patch.object(api, "copper_removal_changes", return_value=[{"op": "remove"}]),
              patch.object(api, "native_pose_change", return_value={"op": "pose"}) as pose,
              patch.object(api, "placement_cost", return_value=1.5),
              patch.object(api, "CampaignPlanner", return_value=planner)):
            plan = api.compile_candidate(Path("."), queue, {}, ("TARGET",), (),
                                         ("SIGNAL",), (), 0, bottom_placement=True)
        self.assertEqual(plan.moves, ("R50",))
        self.assertEqual(plan.affected, ("A", "B", "TARGET"))
        self.assertEqual(plan.board, b"moved-private-board")
        self.assertEqual(len(planner.plan.call_args.args[1]), 2)
        pose.assert_called_once()

    def test_bottom_placement_rejects_ground_and_protection_parts(self):
        import pcbnew

        board, part, pad = MagicMock(), MagicMock(), MagicMock()
        board.FindFootprintByReference.return_value = part
        board.FindNet.return_value.GetNetClassName.return_value = "SIGNAL"
        part.GetLayer.return_value = pcbnew.B_Cu
        part.IsLocked.return_value = False
        part.Pads.return_value = [pad]
        pad.GetAttribute.return_value = pcbnew.PAD_ATTRIB_SMD
        pad.IsOnLayer.side_effect = lambda layer: layer == pcbnew.B_Cu
        pad.GetNetname.return_value = "GND"
        candidate = {"nets": [{"name": "GND"}], "movable": [
            {"reference": "R10", "positions_mm": [[1.0, 1.0]], "rotations_deg": [0]}]}
        self.assertIsNone(api.placement_poses(board, candidate, {"SIGNAL"}, {"GND"}))
        pad.GetNetname.return_value = "A"
        candidate["nets"] = [{"name": "A"}]
        candidate["movable"][0]["reference"] = "D10"
        self.assertIsNone(api.placement_poses(board, candidate, {"SIGNAL"}, {"GND"}))

    def test_native_bottom_pose_is_private_and_preserves_top_geometry(self):
        import pcbnew

        from pcb.move_scope import MoveScope
        from pcb.net_routing.protection import ProtectedState
        from verification import test_net_routing as fixture
        from verification import test_route_optimizer as boards

        with tempfile.TemporaryDirectory(prefix="autopcb-fast-pose-") as folder:
            root = Path(folder)
            source = boards.RouteOptimizerTests.make_board(root)
            board = pcbnew.LoadBoard(str(source))
            board.SetCopperLayerCount(4)
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            fixture.NativeNetRoutingTests.add_pad(board, "R50", (3, 3), bottom=True, net_name="OTHER")
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            request = fixture.NativeNetRoutingTests.config()
            request.update(max_islands=2, recovery_stage=4)
            policy = {"route": request["shared"], "max_pins": 2}
            # The synthetic board has only KiCad's default class; production
            # trials require explicit signal classes before calling the compiler.
            with patch.object(api, "placement_signal_classes", return_value={"kicad_default"}):
                plan = api.compile_candidate(root, [[request]], policy, ("SIGNAL",), (),
                                             ("SIGNAL",), (), 0, bottom_placement=True)
            self.assertIsInstance(plan, api.ReworkPlan)
            self.assertEqual(plan.moves, ("R50",))
            self.assertEqual(source.read_bytes(), before)
            bottom_only = MoveScope.from_config({"sides": ["bottom"]})
            self.assertEqual(ProtectedState.capture(before, b"sch", movable=bottom_only)
                             .differences(plan.board, b"sch"), ())
            candidate = root / "candidate.kicad_pcb"
            candidate.write_bytes(plan.board)
            after = pcbnew.LoadBoard(str(candidate))
            self.assertNotEqual(after.FindFootprintByReference("R50").GetPosition(),
                                board.FindFootprintByReference("R50").GetPosition())

    def test_fast_placement_excludes_nets_touching_esd_or_efuse_devices(self):
        board = MagicMock()
        diode, efuse, supervisor, ordinary = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        diode.GetReference.return_value, diode.GetValue.return_value = "D5", "ESD"
        efuse.GetReference.return_value, efuse.GetValue.return_value = "U1", "INPUT EFUSE"
        supervisor.GetReference.return_value, supervisor.GetValue.return_value = "U29", "TPS3702"
        supervisor.GetFieldsText.return_value = {"Description": "Host VBUS monitor"}
        ordinary.GetReference.return_value, ordinary.GetValue.return_value = "U2", "LOGIC DRIVER"
        for part, net in ((diode, "CAM3_OPTOIN"), (efuse, "EFUSE_OVLO"),
                          (supervisor, "USB_HOST_OE_N"), (ordinary, "OTHER")):
            pad = MagicMock()
            pad.GetNetname.return_value = net
            part.Pads.return_value = [pad]
        board.GetFootprints.return_value = [diode, efuse, supervisor, ordinary]
        board.GetNetsByName.return_value = {"BTS_R_EN": 1, "FRONT_PWM_IN": 2,
                                           "LIMIT_MCU_INV": 3, "ORDINARY_SIGNAL": 4,
                                           "TOP_CURRENT_RAW": 5}
        self.assertEqual(api.protection_nets(board), {"CAM3_OPTOIN", "EFUSE_OVLO", "USB_HOST_OE_N",
                                                       "BTS_R_EN", "FRONT_PWM_IN", "LIMIT_MCU_INV",
                                                       "TOP_CURRENT_RAW"})

    def test_followup_never_reintroduces_protected_nets(self):
        self.assertEqual(api.followup_exclusions(("FAILED",), ("TARGET",), ("PROTECTED",)),
                         ("FAILED", "PROTECTED"))
        with self.assertRaisesRegex(ValueError, "protected"):
            api.followup_exclusions((), ("PROTECTED",), ("PROTECTED",))

    def test_protected_copper_comparison_detects_additions_and_removals(self):
        first = Trace("PROTECTED", "F.Cu", 200000, (0, 0), (1000000, 0))
        added = Trace("PROTECTED", "F.Cu", 200000, (1000000, 0), (2000000, 0))
        signal = Trace("SIGNAL", "F.Cu", 200000, (0, 0), (0, 1000000))
        self.assertTrue(api.protected_copper_unchanged([first], [first, signal], ("PROTECTED",)))
        self.assertFalse(api.protected_copper_unchanged([first], [first, added], ("PROTECTED",)))
        self.assertFalse(api.protected_copper_unchanged([first], [], ("PROTECTED",)))

    def test_placement_candidate_compiler_uses_a_bounded_target_window(self):
        import pcbnew

        board = MagicMock()
        board.GetTracks.return_value = []
        board.GetFootprints.return_value = []
        board.FindNet.return_value.GetNetClassName.return_value = "SIGNAL"
        queue = [[{"shared": {}, "nets": [{"name": name} for name in ("A", "B", "C", "D")]}]]
        empty = {"requests": [], "candidates": []}
        with (patch.object(pcbnew, "LoadBoard", return_value=board),
              patch.object(api, "eligible_signal_resistors", return_value=("R1",)),
              patch.object(api, "compile_reroutes", return_value=empty) as compile_reroutes):
            result = api.compile_candidate(Path("."), queue, {}, ("A", "B", "C", "D"), (),
                                           ("SIGNAL",), (), 0, bottom_placement=True,
                                           target_offset=1, target_limit=2)
        self.assertIsNone(result)
        self.assertEqual([call.args[1]["nets"][0]["name"] for call in compile_reroutes.call_args_list],
                         ["B", "C"])

    def test_placement_skips_protected_targets_before_expensive_compilation(self):
        import pcbnew

        board = MagicMock()
        board.GetTracks.return_value = []
        board.GetFootprints.return_value = []
        board.GetNetsByName.return_value = {"BTS_R_EN": 1, "DENIED": 2, "SAFE": 3}
        board.FindNet.return_value.GetNetClassName.return_value = "SIGNAL"
        queue = [[{"shared": {}, "nets": [{"name": "BTS_R_EN"}, {"name": "DENIED"}, {"name": "SAFE"}]}]]
        with (patch.object(pcbnew, "LoadBoard", return_value=board),
              patch.object(api, "eligible_signal_resistors", return_value=("R1",)),
              patch.object(api, "compile_reroutes", return_value={"requests": [], "candidates": []}) as compile_reroutes):
            api.compile_candidate(Path("."), queue, {"exclude_nets": ["DENIED"]}, ("BTS_R_EN", "DENIED", "SAFE"), (),
                                  ("SIGNAL",), (), 0, bottom_placement=True)
        self.assertEqual([call.args[1]["nets"][0]["name"] for call in compile_reroutes.call_args_list], ["SAFE"])
        self.assertIn("BTS_R_EN", compile_reroutes.call_args.args[3])
        self.assertIn("DENIED", compile_reroutes.call_args.args[3])

    def test_copper_rework_keeps_protection_and_unclassified_nets_out_of_followup_scope(self):
        import pcbnew

        board, track = MagicMock(), MagicMock()
        board.GetTracks.return_value = [track]
        board.GetFootprints.return_value = []
        board.GetNetsByName.return_value = {"TARGET": 1, "A": 2, "BTS_R_EN": 3, "OTHER": 4}
        classes = {"TARGET": "SIGNAL", "A": "SIGNAL", "BTS_R_EN": "SIGNAL", "OTHER": "Default"}
        board.FindNet.side_effect = lambda name: (MagicMock(GetNetClassName=lambda: classes[name])
                                                 if name in classes else None)
        board.GetLayerID.return_value = pcbnew.F_Cu
        track.m_Uuid.AsString.return_value = "track-a"
        track.GetNetname.return_value = "A"
        track.GetLayer.return_value = pcbnew.F_Cu
        candidate = {"rip_up": {"A": [{"uuid": "track-a"}]}}
        compiled = {"requests": [candidate], "candidates": [{"estimated_rework_cost": 1.0}]}
        planner = MagicMock()
        planner.plan.return_value.after = b"candidate"
        queue = [[{"shared": {}, "nets": [{"name": "TARGET"}]}]]
        with (patch.object(pcbnew, "LoadBoard", return_value=board),
              patch.object(api, "compile_reroutes", return_value=compiled),
              patch.object(api, "copper_width", return_value=250000),
              patch.object(api, "copper_removal_changes", return_value=[]),
              patch.object(api, "CampaignPlanner", return_value=planner)):
            plan = api.compile_candidate(Path("."), queue, {}, ("TARGET",), (), ("SIGNAL",), (), 0)
        self.assertIn("BTS_R_EN", plan.excluded)
        self.assertIn("OTHER", plan.excluded)

    def test_empty_placement_inventory_stops_before_compiling_targets(self):
        import pcbnew

        board = MagicMock()
        board.GetFootprints.return_value = []
        board.GetNetsByName.return_value = {}
        queue = [[{"shared": {}, "nets": [{"name": "SAFE"}]}]]
        with (patch.object(pcbnew, "LoadBoard", return_value=board),
              patch.object(api, "compile_reroutes") as compile_reroutes):
            result = api.compile_candidate(Path("."), queue, {}, ("SAFE",), (),
                                           ("SIGNAL",), (), 0, bottom_placement=True)
        self.assertEqual(result, {"status": "no_eligible_bottom_resistors"})
        compile_reroutes.assert_not_called()

    def test_unclassified_or_power_net_classes_cannot_authorize_placement(self):
        self.assertEqual(api.placement_signal_classes(("CORE_SIGNAL", "FIELD_SIGNAL", "kicad_default", "CORE_POWER")),
                         {"CORE_SIGNAL", "FIELD_SIGNAL"})
        board = MagicMock()
        board.GetNetsByName.return_value = {"A": 1, "B": 2, "C": 3}
        board.FindNet.side_effect = lambda name: MagicMock(GetNetClassName=lambda: {
            "A": "CORE_SIGNAL", "B": "Default", "C": "CORE_POWER"}[name])
        self.assertEqual(api.placement_outside_classes(board, {"CORE_SIGNAL"}), {"B", "C"})

    def test_eligible_resistor_inventory_respects_protection_and_side(self):
        import pcbnew

        board = MagicMock()
        board.FindNet.return_value.GetNetClassName.return_value = "CORE_SIGNAL"
        safe, protected, top = MagicMock(), MagicMock(), MagicMock()
        for part, ref, layer, name in ((safe, "R1", pcbnew.B_Cu, "SIGNAL"),
                                       (protected, "R2", pcbnew.B_Cu, "PROTECTED"),
                                       (top, "R3", pcbnew.F_Cu, "SIGNAL")):
            part.GetReference.return_value = ref
            part.GetLayer.return_value = layer
            part.IsLocked.return_value = False
            pad = MagicMock()
            pad.GetNetname.return_value = name
            pad.GetAttribute.return_value = pcbnew.PAD_ATTRIB_SMD
            pad.IsOnLayer.side_effect = lambda active: active == pcbnew.B_Cu
            part.Pads.return_value = [pad]
        board.GetFootprints.return_value = [safe, protected, top]
        self.assertEqual(api.eligible_signal_resistors(board, {"CORE_SIGNAL"}, {"PROTECTED"}), ("R1",))

    def test_placement_search_continues_with_the_next_target_window(self):
        no_candidate = {"status": "no_rework_candidate", "run_id": "a" * 32}
        with (patch.object(api, "run_one", return_value=no_candidate) as run,
              patch.object(api, "record_unimproved_batch")):
            result = api.main(["--project", ".", "--run", "a" * 32, "--batches", "2",
                               "--bottom-placement", "--target-window", "3",
                               "--focus-open-class", "SIGNAL"])
        self.assertEqual(result, 0)
        self.assertEqual([call.kwargs["target_offset"] for call in run.call_args_list], [0, 3])

    def test_repair_reports_the_check_that_rejected_a_candidate(self):
        outcome = {"status": "no_safe_improvement", "run_id": "a" * 32,
                   "stage": "preservation", "reason": "An existing connection was interrupted"}
        output = io.StringIO()
        with (patch.object(api, "run_one", return_value=outcome),
              patch.object(api, "record_unimproved_batch"), redirect_stdout(output)):
            result = api.main(["--project", ".", "--run", "a" * 32, "--batches", "1",
                               "--focus-open-class", "SIGNAL"])
        self.assertEqual(result, 0)
        self.assertIn("preservation", output.getvalue())
        self.assertIn("existing connection", output.getvalue())

    def test_same_obstruction_is_not_deduplicated_across_targets(self):
        import pcbnew

        board, item = MagicMock(), MagicMock()
        board.GetTracks.return_value = [item]
        item.m_Uuid.AsString.return_value = "track-a"
        item.GetNetname.return_value = "A"
        item.GetLayer.return_value = pcbnew.F_Cu
        board.FindNet.return_value.GetNetClassName.return_value = "SIGNAL"
        candidate = {"rip_up": {"A": [{"uuid": "track-a"}]}}
        compiled = {"requests": [candidate], "candidates": [{"estimated_rework_cost": 2.0}]}
        planner = MagicMock()
        planner.plan.return_value.after = b"candidate"
        queue = [[{"shared": {}, "nets": [{"name": "FIRST"}, {"name": "SECOND"}]}]]
        with (patch.object(pcbnew, "LoadBoard", return_value=board),
              patch.object(api, "compile_reroutes", return_value=compiled),
              patch.object(api, "copper_width", return_value=250000),
              patch.object(api, "copper_removal_changes", return_value=[]),
              patch.object(api, "CampaignPlanner", return_value=planner)):
            plan = api.compile_candidate(Path("."), queue, {}, ("FIRST", "SECOND"), (),
                                         ("SIGNAL",), (), 1)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.target, "SECOND")

    def test_trials_visit_other_targets_before_repeating_neighborhoods(self):
        import pcbnew

        board = MagicMock()
        items = [MagicMock(), MagicMock()]
        for item, uid in strict_zip(items, ("track-a", "track-b")):
            item.m_Uuid.AsString.return_value = uid
            item.GetNetname.return_value = "A"
            item.GetLayer.return_value = pcbnew.F_Cu
        board.GetTracks.return_value = items
        board.FindNet.return_value.GetNetClassName.return_value = "SIGNAL"
        compiled = {
            "requests": [
                {"rip_up": {"A": [{"uuid": uid}]}}
                for uid in ("track-a", "track-b")
            ],
            "candidates": [{"estimated_rework_cost": 2.0}, {"estimated_rework_cost": 3.0}],
        }
        planner = MagicMock()
        planner.plan.return_value.after = b"candidate"
        queue = [[{"shared": {}, "nets": [{"name": "FIRST"}, {"name": "SECOND"}]}]]
        with (patch.object(pcbnew, "LoadBoard", return_value=board),
              patch.object(api, "compile_reroutes", return_value=compiled),
              patch.object(api, "copper_width", return_value=250000),
              patch.object(api, "copper_removal_changes", return_value=[]),
              patch.object(api, "CampaignPlanner", return_value=planner)):
            second_target = api.compile_candidate(Path("."), queue, {}, ("FIRST", "SECOND"), (),
                                                  ("SIGNAL",), (), 1)
            second_neighborhood = api.compile_candidate(Path("."), queue, {}, ("FIRST", "SECOND"), (),
                                                        ("SIGNAL",), (), 2)
            resumed = api.compile_candidate(Path("."), queue, {}, ("FIRST", "SECOND"), (),
                                             ("SIGNAL",), (), 0,
                                             tried_plans=(api.repair_identity("FIRST", frozenset({"track-a"})),))
            next_batch = api.compile_candidate(Path("."), queue, {}, ("FIRST", "SECOND"), (),
                                                ("SIGNAL",), (), 2,
                                                tried_plans=(api.repair_identity("FIRST", frozenset({"track-a"})),))
        self.assertEqual(second_target.target, "SECOND")
        self.assertEqual(second_target.cost, 2.0)
        self.assertEqual(resumed.target, "FIRST")
        self.assertEqual(resumed.cost, 3.0)
        self.assertEqual(resumed.attempt_key, api.repair_identity("FIRST", frozenset({"track-b"})))
        self.assertEqual(second_neighborhood.target, "FIRST")
        self.assertEqual(second_neighborhood.cost, 3.0)
        self.assertEqual(next_batch.target, "FIRST")
        self.assertEqual(next_batch.cost, 3.0)

    def test_failed_neighbor_is_deferred_without_hiding_other_repair_plans(self):
        import pcbnew

        board = MagicMock()
        items = [MagicMock(), MagicMock()]
        for item, uid, net in strict_zip(items, ("track-a", "track-b"), ("A", "B")):
            item.m_Uuid.AsString.return_value = uid
            item.GetNetname.return_value = net
            item.GetLayer.return_value = pcbnew.F_Cu
        board.GetTracks.return_value = items
        board.FindNet.return_value.GetNetClassName.return_value = "SIGNAL"
        compiled = {"requests": [{"rip_up": {net: [{"uuid": uid}]}}
                                 for uid, net in (("track-a", "A"), ("track-b", "B"))],
                    "candidates": [{"estimated_rework_cost": 1.0}, {"estimated_rework_cost": 2.0}]}
        planner = MagicMock()
        planner.plan.return_value.after = b"candidate"
        queue = [[{"shared": {}, "nets": [{"name": "TARGET"}]}]]
        with (patch.object(pcbnew, "LoadBoard", return_value=board),
              patch.object(api, "compile_reroutes", return_value=compiled),
              patch.object(api, "copper_width", return_value=250000),
              patch.object(api, "copper_removal_changes", return_value=[]),
              patch.object(api, "CampaignPlanner", return_value=planner)):
            plan = api.compile_candidate(Path("."), queue, {}, ("TARGET",), (), ("SIGNAL",), (), 0,
                                         defer_affected=("A",))
        self.assertEqual(plan.affected, ("B", "TARGET"))
        self.assertEqual(plan.cost, 2.0)

    def test_invalid_proposal_is_rejected_without_stopping_the_batch(self):
        reason = "Resumed candidate broke an existing copper connection"

        def native(function, *_args, **_kwargs):
            if function is api.preview:
                return {"trial_board": "trial/candidate.kicad_pcb"}
            if function is api._roundtrip:
                return {}
            if function is api.filter_proposal:
                raise RuntimeError(reason)
            self.fail(f"Unexpected operation: {function}")

        with patch.object(api, "run_native", side_effect=native):
            result = api._candidate_batch(Path("."), Path("router"), 10, 10, (), lambda _: None)
        self.assertEqual(result["status"], "candidate_rejected")
        self.assertEqual(result["reason"], reason)
        self.assertEqual(api._failed("a" * 32, "followup", result)["reason"], reason)

    def test_unexpected_native_failure_still_stops_the_batch(self):
        with patch.object(api, "run_native", side_effect=RuntimeError("The source changed during filtering")):
            with self.assertRaisesRegex(RuntimeError, "source changed"):
                api._candidate_batch(Path("."), Path("router"), 10, 10, (), lambda _: None)

    def test_rejected_backend_tries_explicit_alternate_on_the_unchanged_private_board(self):
        for status in ("candidate_rejected", "filter_stalled", "no_safe_proposal"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                board = Path(folder) / "v4.1.kicad_pcb"
                board.write_bytes(b"stripped private board")
                calls, messages = [], []

                def attempt(engine):
                    calls.append(engine)
                    self.assertEqual(board.read_bytes(), b"stripped private board")
                    return ({"status": status, "reason": "unsafe proposal"} if engine == "fastroute" else
                            {"status": "filtered_preview", "board": b"checked candidate"})

                result, tried = api.try_repair_backends(
                    board, ("fastroute", "Freerouting"), attempt, messages.append)
                self.assertEqual(result["status"], "filtered_preview")
                self.assertEqual(tried, ("fastroute", "Freerouting"))
                self.assertEqual(calls, list(tried))
                self.assertEqual(board.read_bytes(), b"stripped private board")
                self.assertTrue(any("Freerouting" in message for message in messages))

    def test_alternate_backend_does_not_hide_input_changes_or_execution_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            board = Path(folder) / "v4.1.kicad_pcb"
            board.write_bytes(b"private board")
            for result in ({"status": "candidate_rejected"}, {"status": "filtered_preview", "board": b"candidate"}):
                with self.subTest(result=result):
                    def mutate(_engine):
                        board.write_bytes(b"unexpected mutation")
                        return result
                    with self.assertRaisesRegex(RuntimeError, "private repair board changed"):
                        api.try_repair_backends(board, ("fastroute", "Freerouting"), mutate, lambda _: None)
                    board.write_bytes(b"private board")
            for failure in (RuntimeError("worker crashed"), ValueError("invalid native report")):
                attempt = MagicMock(side_effect=failure)
                with self.subTest(failure=failure), self.assertRaisesRegex(type(failure), str(failure)):
                    api.try_repair_backends(board, ("fastroute", "Freerouting"), attempt, lambda _: None)
                attempt.assert_called_once_with("fastroute")

    def test_backend_retry_is_bounded_and_never_returns_a_rejected_board(self):
        with tempfile.TemporaryDirectory() as folder:
            board = Path(folder) / "v4.1.kicad_pcb"
            board.write_bytes(b"private board")
            attempt = MagicMock(return_value={"status": "candidate_rejected", "reason": "unsafe"})
            result, tried = api.try_repair_backends(board, ("fastroute", "fastroute"), attempt, lambda _: None)
            self.assertEqual(tried, ("fastroute",))
            self.assertNotIn("board", result)
            attempt.assert_called_once()
            attempt.reset_mock()
            attempt.return_value = {"status": "input_incompatible"}
            result, tried = api.try_repair_backends(board, ("fastroute", "Freerouting"), attempt, lambda _: None)
            self.assertEqual(tried, ("fastroute",))
            self.assertEqual(result["status"], "input_incompatible")

    def test_freerouting_rework_filters_candidates_and_checks_router_identity(self):
        fingerprint = "a" * 64
        calls = []
        messages = []

        def native(function, *args, **kwargs):
            calls.append((function, args, kwargs))
            if function is api.preview:
                return {"trial_board": "trial/candidate.kicad_pcb",
                        "freerouting_jar_sha256": fingerprint}
            if function is api._roundtrip:
                return Path("trial/roundtrip.kicad_pcb")
            if function is api.filter_proposal:
                self.assertEqual(kwargs["deferred_nets"], ("PROTECTED",))
                self.assertTrue(kwargs["allow_temporary_disconnect"])
                kwargs["progress"]({"event": "off_angle_repair", "segments": 2})
                kwargs["progress"]({"event": "native_violation_retry", "nets": ["SIG"]})
                kwargs["progress"]({"event": "style_blocker_retry", "nets": ["SIG"]})
                return {"status": "filtered_preview", "board": b"checked"}
            self.fail(f"Unexpected operation: {function}")

        with patch.object(api, "run_native", side_effect=native):
            result = api._candidate_batch(Path("."), Path("router.jar"), 60, 30,
                                          ("PROTECTED",), messages.append,
                                          engine="Freerouting", expected_tool=fingerprint)
        self.assertEqual(result["board"], b"checked")
        self.assertIn("Replaced 2 off-angle traces with 45-degree candidates", messages)
        self.assertIn("Retrying without nets implicated in native violations: SIG", messages)
        self.assertIn("Retrying without nets with ambiguous geometry: SIG", messages)
        self.assertEqual(calls[0][2]["engine"], "Freerouting")
        self.assertEqual(calls[0][1][3], 4)
        self.assertEqual(calls[0][2]["starts"], 1)
        self.assertEqual(calls[1][1][-1], "Freerouting")
        with (patch.object(api, "run_native", return_value={"trial_board": "trial/candidate.kicad_pcb",
                                                           "freerouting_jar_sha256": "b" * 64}),
              self.assertRaisesRegex(RuntimeError, "router.*changed")):
            api._candidate_batch(Path("."), Path("router.jar"), 60, 30, (), lambda _: None,
                                 engine="Freerouting", expected_tool=fingerprint)

    def test_freerouting_timeout_is_an_unresolved_search_not_a_crash(self):
        message = "Freerouting timed out after 60 seconds without a complete checkpoint; try a smaller --max-items."
        with patch.object(api, "run_native", side_effect=RuntimeError(message)):
            result = api._candidate_batch(Path("."), Path("router.jar"), 60, 30, (), lambda _: None,
                                          engine="Freerouting", expected_tool="a" * 64)
        self.assertEqual(result["status"], "no_safe_proposal")
        self.assertIn("timed out", result["reason"])

    def test_krt_rework_targets_only_allowed_nets_and_filters_private_candidate(self):
        from pcb.net_routing import krt_adapter

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder).resolve()
            baseline = directory / "v4.1.kicad_pcb"
            baseline.write_bytes(b"private baseline")
            fingerprint = "a" * 64
            calls = []

            def native(function, *args, **kwargs):
                calls.append((function, args, kwargs))
                if function is krt_adapter.preview:
                    self.assertEqual(args[3], ("TARGET", "NEIGHBOR"))
                    return {"board": b"proposal", "tool": fingerprint}
                if function is api.filter_proposal:
                    self.assertEqual(args[1], baseline)
                    self.assertEqual(args[2].read_bytes(), b"proposal")
                    self.assertEqual(kwargs["deferred_nets"], ("PROTECTED",))
                    self.assertTrue(kwargs["allow_temporary_disconnect"])
                    return {"status": "filtered_preview", "board": b"checked"}
                self.fail(f"Unexpected operation: {function}")

            with patch.object(api, "run_native", side_effect=native):
                result = api._candidate_batch(directory, Path("router"), 60, 30, ("PROTECTED",),
                                              lambda _: None, engine="krt", targets=("TARGET", "NEIGHBOR"),
                                              expected_tool=fingerprint, grid_mm=0.2)
            self.assertEqual(result["board"], b"checked")
            self.assertEqual(result["router_fingerprint"], fingerprint)
            self.assertEqual(calls[0][2]["grid_mm"], 0.2)
            self.assertEqual(baseline.read_bytes(), b"private baseline")

    def test_krt_rework_rejects_protected_targets_and_changed_router(self):
        from pcb.net_routing import krt_adapter

        with self.assertRaisesRegex(ValueError, "target"):
            api._candidate_batch(Path("."), Path("router"), 60, 30, ("PROTECTED",),
                                 lambda _: None, engine="krt", targets=("PROTECTED",),
                                 expected_tool="a" * 64)
        with (patch.object(api, "run_native", return_value={"board": b"x", "tool": "b" * 64}) as native,
              self.assertRaisesRegex(RuntimeError, "router.*changed")):
            api._candidate_batch(Path("."), Path("router"), 60, 30, (), lambda _: None,
                                 engine="krt", targets=("TARGET",), expected_tool="a" * 64)
        self.assertIs(native.call_args.args[0], krt_adapter.preview)

    def test_krt_strict_size_rejection_never_filters_or_retains_the_candidate(self):
        from pcb.net_routing import krt_adapter

        raw = {"status": "strict_size_rejected", "failed_nets": ("TARGET",),
               "reason": "strict size rules", "tool": "a" * 64}
        with patch.object(api, "run_native", return_value=raw) as native:
            result = api._candidate_batch(Path("."), Path("router"), 60, 30, (), lambda _: None,
                                          engine="krt", targets=("TARGET",), expected_tool="a" * 64)
        native.assert_called_once()
        self.assertIs(native.call_args.args[0], krt_adapter.preview)
        self.assertEqual(result["status"], "no_safe_proposal")
        self.assertEqual(result["repair_blockers"], {"TARGET": "strict size rules"})
        self.assertNotIn("board", result)

    def test_rework_size_salvage_discards_every_identified_narrowed_tree(self):
        from pcb.net_routing import krt_adapter

        raw = {"status": "strict_size_rejected", "failed_nets": ("NARROW",),
               "narrowed_nets": ("NARROW",), "salvage_board": b"partly narrowed proposal",
               "reason": "strict size rules", "tool": "a" * 64}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            baseline = root / "v4.1.kicad_pcb"
            baseline.write_bytes(b"private pruned baseline")

            def native(function, *args, **kwargs):
                if function is krt_adapter.preview:
                    self.assertTrue(kwargs["salvage_strict"])
                    return dict(raw)
                self.assertIs(function, api.filter_proposal)
                self.assertEqual(kwargs["deferred_nets"], ("NARROW", "PROTECTED"))
                self.assertTrue(kwargs["allow_temporary_disconnect"])
                self.assertEqual(args[2].read_bytes(), raw["salvage_board"])
                return {"status": "filtered_preview", "board": b"safe remainder"}

            with patch.object(api, "run_native", side_effect=native):
                result = api._candidate_batch(root, Path("router"), 60, 30, ("PROTECTED",),
                                              lambda _: None, engine="krt", targets=("TARGET", "NARROW"),
                                              expected_tool="a" * 64)
            self.assertEqual(result["board"], b"safe remainder")
            self.assertEqual(baseline.read_bytes(), b"private pruned baseline")
            with (patch.object(api, "run_native", return_value=dict(raw)),
                  self.assertRaisesRegex(RuntimeError, "unexpected net")):
                api._candidate_batch(root, Path("router"), 60, 30, (), lambda _: None,
                                     engine="krt", targets=("TARGET",), expected_tool="a" * 64)

    def test_krt_timeout_never_filters_or_retains_a_partial_candidate(self):
        raw = {"status": "search_timeout", "reason": "Search reached its time limit", "tool": "a" * 64}
        with patch.object(api, "run_native", return_value=raw) as native:
            result = api._candidate_batch(Path("."), Path("router"), 60, 30, (), lambda _: None,
                                          engine="krt", targets=("TARGET",), expected_tool="a" * 64)
        native.assert_called_once()
        self.assertEqual(result["status"], "no_safe_proposal")
        self.assertIn("time limit", result["reason"])
        self.assertNotIn("board", result)

    def test_failed_batch_records_elapsed_time_before_reporting_error(self):
        with (patch.object(api, "run_one", side_effect=RuntimeError("native failure")),
              patch.object(api, "record_unimproved_batch") as record,
              redirect_stderr(io.StringIO())):
            result = api.main(["--project", ".", "--run", "a" * 32, "--batches", "1",
                               "--focus-open-class", "SIGNAL"])
        self.assertEqual(result, 1)
        record.assert_called_once()

    def test_failed_net_deferrals_never_hide_a_required_repair_neighbor(self):
        self.assertEqual(api.followup_exclusions(("FAILED", "TARGET", "NEIGHBOR"),
                                                ("TARGET", "NEIGHBOR")), ("FAILED",))
        self.assertEqual(api.followup_exclusions((), ("TARGET",)), ())

    def test_batch_sequence_temporarily_defers_unsuccessful_nets(self):
        results = [{"status": "no_safe_improvement", "repair_blockers": ("FAILED",),
                    "unchanged_followup_scope": ()},
                   {"status": "no_rework_candidate"}]
        with (patch.object(api, "run_one", side_effect=results) as run,
              patch.object(api, "record_unimproved_batch")):
            result = api.main(["--project", ".", "--run", "a" * 32, "--batches", "2",
                               "--focus-open-class", "SIGNAL"])
        self.assertEqual(result, 0)
        self.assertEqual(run.call_args_list[0].kwargs["avoid"], ())
        self.assertEqual(run.call_args_list[1].kwargs["avoid"], ("FAILED",))
        self.assertEqual(run.call_args_list[0].kwargs["tried_unchanged"], ())
        self.assertEqual(run.call_args_list[1].kwargs["tried_unchanged"], ((),))

    def test_temporary_candidate_must_restore_the_original_connections_before_retention(self):
        for broken in (True, False):
            with self.subTest(broken=broken), tempfile.TemporaryDirectory() as folder:
                project = Path(folder)
                runs = project / "autopcb" / "results" / "autoroute"
                runs.mkdir(parents=True)
                run_id = "a" * 32
                state = {"schema": 1, "run_id": run_id, "checkpoint": {"candidate_sha256": hashlib.sha256(b"retained").hexdigest()}, "queue": [],
                         "best_preview_opens": 5, "blocked_nets": {}}
                (runs / f"{run_id}.json").write_text(json.dumps(state), encoding="utf-8")
                scratch = project / "scratch"
                scratch.mkdir()
                (scratch / "v4.1.kicad_sch").write_bytes(b"schematic")
                snapshot = MagicMock()
                snapshot.__enter__.return_value = snapshot
                snapshot.directory, snapshot.board_path = scratch, scratch / "v4.1.kicad_pcb"
                snapshot.original, snapshot.tool_hashes = b"saved", {}
                guard = MagicMock()
                guard.differences.return_value = []
                baseline = {"drc": {"open_connections": 5, "parity_findings": 0, "exclusions": 0,
                                    "violation_keys": [], "error_details": []},
                            "reference_copper": {"high_speed_nets": []}}
                prepared = api.RepairPreparation(("TARGET",), (), baseline, b"retained")
                plan = api.ReworkPlan("TARGET", ("TARGET",), 1, 1.0, b"stripped", {})
                candidate = {"status": "deferred_connectivity_preview", "board": b"candidate"}
                calls = []

                def native(function, *args, **_kwargs):
                    calls.append((function, args))
                    if function is api.compile_candidate:
                        return plan
                    if function is api.prepare_project:
                        return {"drc": {"open_connections": 4, "errors": 0,
                                        "exclusions": 0, "parity_findings": 0,
                                        "violation_keys": [], "error_details": []}}
                    if function is api._dimensions_preserved_files:
                        return True
                    if function is api._verify_resume_connectivity:
                        self.assertEqual(args[1], b"retained")
                        if broken:
                            raise RuntimeError("Resumed candidate broke an existing copper connection")
                        return None
                    if function is api.retain_candidate:
                        return {"status": "private_preview_retained", "preview_opens": 4}
                    self.fail(f"Unexpected native operation: {function}")

                with (patch.object(api, "Snapshot", return_value=snapshot),
                      patch.object(api, "read_config", return_value=({}, "policy")),
                      patch.object(api, "_source_hashes", return_value={}),
                      patch.object(api, "decode_checkpoint", return_value=b"retained"),
                      patch.object(api.ProtectedState, "capture", return_value=guard),
                      patch.object(api, "prepare_repair", return_value=prepared),
                      patch.object(api, "repair_work_queue", return_value=[]),
                      patch.object(api, "_candidate_batch", return_value=candidate),
                      patch.object(api, "verify_resume_reports"),
                      patch.object(api, "run_native", side_effect=native)):
                    result = api.run_one(project, run_id, ordinal=0, timeout=60, cleanup=30,
                                         focus=("SIGNAL",), progress=lambda _message: None)
                if broken:
                    self.assertEqual(result["status"], "no_safe_improvement")
                    self.assertEqual(result["stage"], "preservation")
                    self.assertNotIn(api.retain_candidate, [call[0] for call in calls])
                else:
                    self.assertEqual(result["status"], "private_preview_retained")
                    self.assertLess([call[0] for call in calls].index(api._verify_resume_connectivity),
                                    [call[0] for call in calls].index(api.retain_candidate))

    def test_krt_rework_checks_tool_again_before_retaining(self):
        from pcb.net_routing import krt_adapter

        for changed in (False, True):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as folder:
                project = Path(folder)
                runs = project / "autopcb" / "results" / "autoroute"
                runs.mkdir(parents=True)
                run_id = "a" * 32
                state = {"schema": 1, "run_id": run_id, "checkpoint": {"candidate_sha256": hashlib.sha256(b"retained").hexdigest()}, "queue": [],
                         "best_preview_opens": 5, "blocked_nets": {}}
                (runs / f"{run_id}.json").write_text(json.dumps(state), encoding="utf-8")
                scratch = project / "scratch"
                scratch.mkdir()
                (scratch / "v4.1.kicad_sch").write_bytes(b"schematic")
                snapshot = MagicMock()
                snapshot.__enter__.return_value = snapshot
                snapshot.directory, snapshot.board_path = scratch, scratch / "v4.1.kicad_pcb"
                snapshot.original, snapshot.tool_hashes = b"saved", {}
                guard = MagicMock()
                guard.differences.return_value = []
                baseline = {"drc": {"open_connections": 5, "parity_findings": 0, "exclusions": 0,
                                    "violation_keys": [], "error_details": []},
                            "reference_copper": {"high_speed_nets": []}}
                prepared = api.RepairPreparation(("TARGET",), (), baseline, b"retained")
                plan = api.ReworkPlan("TARGET", ("TARGET",), 1, 1.0, b"stripped", {})
                fingerprint = "a" * 64
                fingerprints = iter((fingerprint, "b" * 64 if changed else fingerprint))
                retained = []

                def native(function, *args, **kwargs):
                    if function is krt_adapter.validate_installation:
                        return next(fingerprints)
                    if function is api.compile_candidate:
                        return plan
                    if function is api.prepare_project:
                        return {"drc": {"open_connections": 4, "errors": 0, "exclusions": 0,
                                        "parity_findings": 0, "violation_keys": [], "error_details": []}}
                    if function is api._dimensions_preserved_files:
                        return True
                    if function is api._verify_resume_connectivity:
                        return None
                    if function is api.retain_candidate:
                        retained.append(kwargs)
                        return {"status": "private_preview_retained", "preview_opens": 4}
                    self.fail(f"Unexpected native operation: {function}")

                with (patch.object(api, "Snapshot", return_value=snapshot),
                      patch.object(api, "read_config", return_value=({}, "policy")),
                      patch.object(api, "_source_hashes", return_value={}),
                      patch.object(api, "decode_checkpoint", return_value=b"retained"),
                      patch.object(api.ProtectedState, "capture", return_value=guard),
                      patch.object(api, "prepare_repair", return_value=prepared),
                      patch.object(api, "repair_work_queue", return_value=[]),
                      patch.object(api, "_candidate_batch", return_value={"status": "filtered_preview",
                                                                         "board": b"candidate"}) as route,
                      patch.object(api, "verify_resume_reports"),
                      patch.object(api, "run_native", side_effect=native)):
                    kwargs = dict(ordinal=0, timeout=60, cleanup=30, focus=("SIGNAL",), engine="krt",
                                  grid_mm=0.2, progress=lambda _message: None)
                    if changed:
                        with self.assertRaisesRegex(RuntimeError, "router.*changed"):
                            api.run_one(project, run_id, **kwargs)
                    else:
                        result = api.run_one(project, run_id, **kwargs)
                        self.assertEqual(result["status"], "private_preview_retained")
                self.assertEqual(route.call_args.kwargs["targets"], ("TARGET",))
                self.assertEqual(route.call_args.kwargs["expected_tool"], fingerprint)
                self.assertEqual(bool(retained), not changed)
                if retained:
                    self.assertEqual(retained[0]["engine"], "krt")
                    self.assertEqual(retained[0]["router_fingerprint"], fingerprint)

    def test_changed_intermediate_layout_gets_another_bounded_followup(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            runs = project / "autopcb" / "results" / "autoroute"
            runs.mkdir(parents=True)
            run_id = "a" * 32
            state = {"schema": 1, "run_id": run_id, "checkpoint": {"candidate_sha256": hashlib.sha256(b"retained").hexdigest()}, "queue": [],
                     "best_preview_opens": 5, "blocked_nets": {}}
            (runs / f"{run_id}.json").write_text(json.dumps(state), encoding="utf-8")
            scratch = project / "scratch"
            scratch.mkdir()
            (scratch / "v4.1.kicad_sch").write_bytes(b"schematic")
            snapshot = MagicMock()
            snapshot.__enter__.return_value = snapshot
            snapshot.directory, snapshot.board_path = scratch, scratch / "v4.1.kicad_pcb"
            snapshot.original, snapshot.tool_hashes = b"saved", {}
            guard = MagicMock()
            guard.differences.return_value = []
            baseline = {"drc": {"open_connections": 5, "parity_findings": 0},
                        "reference_copper": {"high_speed_nets": []}}
            prepared = api.RepairPreparation(("TARGET",), (), baseline, b"retained")
            plan = api.ReworkPlan("TARGET", ("TARGET",), 1, 1.0, b"stripped", {})
            preparations = iter(({"drc": {"open_connections": 5}}, {"drc": {"open_connections": 5}},
                                 {"drc": {"open_connections": 4}}))
            retained = []

            def native(function, *args, **kwargs):
                if function is api.compile_candidate:
                    return plan
                if function is api.prepare_project:
                    return next(preparations)
                if function is api._focus_deferred:
                    return (("TARGET",), ())
                if function is api._same_copper_files:
                    return False
                if function is api._dimensions_preserved_files:
                    return True
                if function is api._verify_resume_connectivity:
                    return None
                if function is api.retain_candidate:
                    retained.append(kwargs)
                    return {"status": "private_preview_retained", "preview_opens": 4}
                self.fail(f"Unexpected native operation: {function}")

            batches = [{"status": "filtered_preview", "board": value}
                       for value in (b"first", b"changed", b"candidate")]
            with (patch.object(api, "Snapshot", return_value=snapshot),
                  patch.object(api, "read_config", return_value=({}, "policy")),
                  patch.object(api, "_source_hashes", return_value={}),
                  patch.object(api, "decode_checkpoint", return_value=b"retained"),
                  patch.object(api.ProtectedState, "capture", return_value=guard),
                  patch.object(api, "prepare_repair", return_value=prepared),
                  patch.object(api, "repair_work_queue", return_value=[]),
                  patch.object(api, "interim_drc_acceptable", return_value=True),
                  patch.object(api, "verify_resume_reports"),
                  patch.object(api, "_candidate_batch", side_effect=batches) as route,
                  patch.object(api, "run_native", side_effect=native)):
                result = api.run_one(project, run_id, ordinal=0, timeout=60, cleanup=30,
                                     focus=("SIGNAL",), followup_rounds=2, progress=lambda _: None)
            self.assertEqual(result["status"], "private_preview_retained")
            self.assertEqual(route.call_count, 3)
            self.assertEqual(retained[0]["router_stages"], (("fastroute", None),) * 3)

    def test_mixed_router_improvement_retains_both_stages(self):
        for rejected_initial in (False, True):
            with self.subTest(rejected_initial=rejected_initial):
                self._check_mixed_router_improvement(rejected_initial)

    def _check_mixed_router_improvement(self, rejected_initial):
        from pcb.net_routing import krt_adapter

        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            runs = project / "autopcb" / "results" / "autoroute"
            runs.mkdir(parents=True)
            run_id = "a" * 32
            state = {"schema": 1, "run_id": run_id, "checkpoint": {"candidate_sha256": hashlib.sha256(b"retained").hexdigest()}, "queue": [],
                     "best_preview_opens": 5, "blocked_nets": {}}
            (runs / f"{run_id}.json").write_text(json.dumps(state), encoding="utf-8")
            scratch = project / "scratch"
            scratch.mkdir()
            (scratch / "v4.1.kicad_sch").write_bytes(b"schematic")
            snapshot = MagicMock()
            snapshot.__enter__.return_value = snapshot
            snapshot.directory, snapshot.board_path = scratch, scratch / "v4.1.kicad_pcb"
            snapshot.original, snapshot.tool_hashes = b"saved", {}
            guard = MagicMock()
            guard.differences.return_value = []
            baseline = {"drc": {"open_connections": 5, "parity_findings": 0},
                        "reference_copper": {"high_speed_nets": []}}
            prepared = api.RepairPreparation(("TARGET",), (), baseline, b"retained")
            plan = api.ReworkPlan("TARGET", ("TARGET",), 1, 1.0, b"stripped", {})
            fingerprint = "a" * 64
            first = {"drc": {"open_connections": 5}}
            if rejected_initial:
                first["drc"]["violation_keys"] = [["shorting_items", "error", ["first", "other"]]]
            preparations = iter((first, {"drc": {"open_connections": 4}}))
            retained = []

            def native(function, *args, **kwargs):
                if function is krt_adapter.validate_installation:
                    return fingerprint
                if function is api.compile_candidate:
                    return plan
                if function is api.prepare_project:
                    return next(preparations)
                if function is api._focus_deferred:
                    return (("TARGET",), ())
                if function is api._same_copper_files:
                    return False
                if function is api._dimensions_preserved_files:
                    return True
                if function is api._verify_resume_connectivity:
                    return None
                if function is api.retain_candidate:
                    retained.append(kwargs)
                    return {"status": "private_preview_retained", "preview_opens": 4}
                self.fail(f"Unexpected native operation: {function}")

            batches = [{"status": "filtered_preview", "board": b"intermediate"},
                       {"status": "filtered_preview", "board": b"candidate"}]
            with (patch.object(api, "Snapshot", return_value=snapshot),
                  patch.object(api, "read_config", return_value=({}, "policy")),
                  patch.object(api, "_source_hashes", return_value={}),
                  patch.object(api, "decode_checkpoint", return_value=b"retained"),
                  patch.object(api.ProtectedState, "capture", return_value=guard),
                  patch.object(api, "prepare_repair", return_value=prepared),
                  patch.object(api, "repair_work_queue", return_value=[]),
                  patch.object(api, "interim_drc_acceptable", side_effect=(not rejected_initial, True)),
                  patch.object(api, "verify_resume_reports"),
                  patch.object(api, "_candidate_batch", side_effect=batches) as route,
                  patch.object(api, "run_native", side_effect=native)):
                result = api.run_one(project, run_id, ordinal=0, timeout=60, cleanup=30,
                                     focus=("SIGNAL",), engine="fastroute", followup_engine="krt",
                                     progress=lambda _message: None)
            self.assertEqual(result["status"], "private_preview_retained")
            self.assertEqual([call.kwargs["engine"] for call in route.call_args_list], ["fastroute", "krt"])
            self.assertEqual(retained[0]["router_stages"], (("fastroute", None), ("krt", fingerprint)))
            self.assertEqual(retained[0]["router_fingerprint"], fingerprint)

    def test_followup_reinventories_interrupted_nets_and_skips_unchanged_copper(self):
        from pcb.net_routing import krt_adapter

        cases = ((True, 191, (), (), 2, (), None),
                 (True, 191, ((),), (), 1, (), None),
                 (True, 191, ((),), ("FAILED",), 2, (), None),
                 (False, 192, (), (), 2, (), None),
                 (False, 192, (), ("FAILED",), 2, ("PROTECTED",), None),
                 (False, 192, (), (), 2, (), "krt"))
        for unchanged, intermediate_opens, tried, avoid, expected_calls, protected, followup in cases:
            with self.subTest(unchanged=unchanged, tried=tried, avoid=avoid,
                              protected=protected), tempfile.TemporaryDirectory() as folder:
                project = Path(folder)
                assets = project / "autopcb"
                runs = assets / "results" / "autoroute"
                runs.mkdir(parents=True)
                run_id = "a" * 32
                state = {"schema": 1, "run_id": run_id, "checkpoint": {"candidate_sha256": hashlib.sha256(b"retained").hexdigest()}, "queue": [],
                         "best_preview_opens": 191, "blocked_nets": {},
                         "fast_retry": {"deferred_nets": ["STALE"]}}
                (runs / f"{run_id}.json").write_text(json.dumps(state), encoding="utf-8")
                scratch = project / "scratch"
                scratch.mkdir()
                (scratch / "v4.1.kicad_sch").write_bytes(b"schematic")
                snapshot = MagicMock()
                snapshot.__enter__.return_value = snapshot
                snapshot.directory = scratch
                snapshot.board_path = scratch / "v4.1.kicad_pcb"
                snapshot.original = b"saved"
                snapshot.tool_hashes = {}
                guard = MagicMock()
                guard.differences.return_value = []
                plan = api.ReworkPlan("TARGET", ("TARGET", "NEIGHBOR"), 1, 1.0, b"stripped", {},
                                      excluded=protected)
                def drc(opens):
                    return {"open_connections": opens, "errors": 0,
                            "exclusions": 0, "parity_findings": 0,
                            "violation_keys": [], "error_details": []}

                preparations = iter(({"drc": drc(191), "reference_copper": {"high_speed_nets": []},
                                      "unqualified_nets": [], "work": {"items": [
                                          {"name": name, "priority": 1, "window": [0, 0, 4, 4],
                                           "island_count": 2} for name in ("TARGET", "NEIGHBOR")]}},
                                     {"drc": drc(intermediate_opens)}))
                focus_calls = []
                focus_exclusions = []

                def native(function, *args, **_kwargs):
                    if function is krt_adapter.validate_installation:
                        return "a" * 64
                    if function is api._focus_deferred:
                        focus_calls.append(snapshot.board_path.read_bytes())
                        focus_exclusions.append(args[3])
                        return (("TARGET", "NEIGHBOR"), tuple(sorted(("OTHER", *protected))))
                    if function is api.prepare_project:
                        return next(preparations)
                    if function is api.compile_candidate:
                        self.assertEqual(args[4], ())
                        return plan
                    if function is api._same_copper_files:
                        return unchanged
                    self.fail(f"Unexpected native operation: {function}")

                batches = [{"status": "filtered_preview", "board": b"intermediate"},
                           {"status": "no_improvement"}]
                with (patch.object(api, "Snapshot", return_value=snapshot),
                      patch.object(api, "read_config", return_value=({}, "policy")),
                      patch.object(api, "_source_hashes", return_value={}),
                      patch.object(api, "decode_checkpoint", return_value=b"retained"),
                      patch.object(api.ProtectedState, "capture", return_value=guard),
                      patch.object(api, "run_native", side_effect=native),
                      patch.object(api, "_candidate_batch", side_effect=batches) as route):
                    result = api.run_one(project, run_id, ordinal=0, timeout=60, cleanup=30,
                                         focus=("SIGNAL",), avoid=avoid, tried_unchanged=tried,
                                         followup_engine=followup,
                                         progress=lambda _message: None)
                self.assertEqual(result["status"], "no_safe_improvement")
                self.assertEqual(route.call_count, expected_calls)
                if expected_calls == 1:
                    self.assertEqual(result["stage"], "unchanged_repair")
                    self.assertEqual(len(focus_calls), 1)
                else:
                    self.assertEqual(result["stage"], "followup")
                    self.assertEqual(focus_calls[-1], b"intermediate")
                    self.assertEqual(route.call_args_list[1].args[4], tuple(sorted(("OTHER", *protected))))
                    if followup:
                        self.assertEqual(route.call_args_list[1].kwargs["engine"], followup)
                        self.assertEqual(route.call_args_list[1].kwargs["expected_tool"], "a" * 64)
                    self.assertEqual(focus_exclusions[-1], tuple(sorted((*avoid, *protected))))
                    if unchanged:
                        self.assertEqual(result["unchanged_followup_scope"], avoid)


if __name__ == "__main__":
    unittest.main()
