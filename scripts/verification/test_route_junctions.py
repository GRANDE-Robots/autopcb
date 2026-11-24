"""Verify that proposed branches have explicit, safe KiCad junctions."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pcbnew

from pcb.route_junctions import junction_changes
from pcb.route_optimizer import Point, Route, Segment, Via, mm
from pcb.route_validation import RouteContract, validate_routes
from pcb import checkpoints
from pcb.net_routing.model import NetPlan
from pcb.net_routing.schedule import BoardPlan
from pcb.net_routing import workflow
from verification import test_net_routing as net_tests
from verification import test_route_optimizer as route_tests


def point(x: float, y: float) -> Point:
    return Point(mm(x), mm(y))


def route(*segments: Segment) -> Route:
    return Route(tuple(segments), (), (), 0, 0, 0, 0)


class JunctionTests(unittest.TestCase):
    def board_with_dangling_track(self, folder: str, *, intermediate_pad: bool = False):
        source = route_tests.RouteOptimizerTests.make_board(Path(folder))
        board = pcbnew.LoadBoard(str(source))
        net = board.FindNet("SIGNAL")
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I(mm(2), mm(2)))
        track.SetEnd(pcbnew.VECTOR2I(mm(6), mm(2)))
        track.SetLayer(pcbnew.F_Cu)
        track.SetWidth(mm(0.4))
        track.SetNetCode(net.GetNetCode())
        board.Add(track)
        if intermediate_pad:
            net_tests.NativeNetRoutingTests.add_pad(
                board, "J3", (5, 2), net_name="SIGNAL"
            )
        pcbnew.SaveBoard(str(source), board)
        return source, board, track.m_Uuid.AsString()

    def test_split_removes_only_an_unattached_dangling_tail(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-dangling-junction-") as folder:
            source, board, track_id = self.board_with_dangling_track(folder)
            original = source.read_bytes()
            proposed = route(
                Segment(point(4, 2), point(7, 5), "F.Cu"),
                Segment(point(7, 5), point(8, 5), "F.Cu"),
            )
            changes, removals = junction_changes(
                board, original, proposed, "SIGNAL", mm(0.2), mm(0.6), mm(0.3)
            )
            self.assertEqual(removals, [{"kind": "segment", "uuid": track_id}])
            self.assertEqual(
                sum("(width 0.4)" in c.get("expression", "") for c in changes), 1
            )
            evidence = validate_routes(
                source,
                hashlib.sha256(original).hexdigest(),
                changes,
                [
                    RouteContract(
                        "SIGNAL",
                        point(2, 2),
                        "F.Cu",
                        point(8, 5),
                        "F.Cu",
                        tuple(removals),
                    )
                ],
            )
            self.assertEqual(evidence["status"], "no_new_drc_violations", evidence)
            self.assertFalse(evidence["schematic_parity_checked"])
            self.assertIsNone(evidence["candidate_schematic_parity"])
            self.assertLess(
                evidence["candidate_violations"], evidence["baseline_violations"]
            )
            self.assertLess(
                evidence["candidate_unconnected"], evidence["baseline_unconnected"]
            )
            self.assertEqual(source.read_bytes(), original)

    def test_native_parity_is_checked_and_new_findings_reject_candidate(self):
        from pcb import route_validation

        with tempfile.TemporaryDirectory(prefix="autopcb-parity-junction-") as folder:
            source, board, _ = self.board_with_dangling_track(folder)
            schematic = source.with_suffix(".kicad_sch")
            schematic.write_text("(kicad_sch (lib_symbols))", encoding="utf-8")
            original, schematic_bytes = source.read_bytes(), schematic.read_bytes()
            proposed = route(
                Segment(point(4, 2), point(7, 5), "F.Cu"),
                Segment(point(7, 5), point(8, 5), "F.Cu"),
            )
            changes, removals = junction_changes(
                board, original, proposed, "SIGNAL", mm(0.2), mm(0.6), mm(0.3)
            )
            contract = RouteContract(
                "SIGNAL", point(2, 2), "F.Cu", point(8, 5), "F.Cu", tuple(removals)
            )
            real_drc = route_validation._drc

            def add_candidate_parity(*args, **kwargs):
                report = real_drc(*args, **kwargs)
                if args[1].parent.name == "after":
                    report["schematic_parity"].append(
                        {"type": "test_new_parity", "severity": "error", "items": []}
                    )
                return report

            with mock.patch.object(
                route_validation, "_drc", side_effect=add_candidate_parity
            ):
                evidence = validate_routes(
                    source, hashlib.sha256(original).hexdigest(), changes, [contract]
                )
            self.assertTrue(evidence["schematic_parity_checked"])
            self.assertEqual(evidence["status"], "rejected", evidence)
            self.assertEqual(
                evidence["new_schematic_parity"][0]["type"], "test_new_parity"
            )
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(schematic.read_bytes(), schematic_bytes)

    def test_tail_with_an_intermediate_same_net_pad_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-connected-tail-") as folder:
            source, board, track_id = self.board_with_dangling_track(
                folder, intermediate_pad=True
            )
            proposed = route(Segment(point(4, 2), point(7, 5), "F.Cu"))
            changes, removals = junction_changes(
                board,
                source.read_bytes(),
                proposed,
                "SIGNAL",
                mm(0.2),
                mm(0.6),
                mm(0.3),
            )
            self.assertEqual(removals, [{"kind": "segment", "uuid": track_id}])
            self.assertEqual(
                sum("(width 0.4)" in c.get("expression", "") for c in changes), 2
            )

    def test_tail_cleanup_preserves_a_required_contract_endpoint(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-contract-tail-") as folder:
            source, board, track_id = self.board_with_dangling_track(folder)
            original = source.read_bytes()
            proposed = route(
                Segment(point(4, 2), point(7, 5), "F.Cu"),
                Segment(point(7, 5), point(8, 5), "F.Cu"),
            )
            changes, removals = junction_changes(
                board,
                original,
                proposed,
                "SIGNAL",
                mm(0.2),
                mm(0.6),
                mm(0.3),
                protected_sites=((point(6, 2), "F.Cu"),),
            )
            self.assertEqual(removals, [{"kind": "segment", "uuid": track_id}])
            self.assertEqual(
                sum("(width 0.4)" in c.get("expression", "") for c in changes), 2
            )
            evidence = validate_routes(
                source,
                hashlib.sha256(original).hexdigest(),
                changes,
                [
                    RouteContract(
                        "SIGNAL",
                        point(6, 2),
                        "F.Cu",
                        point(8, 5),
                        "F.Cu",
                        tuple(removals),
                    )
                ],
            )
            self.assertEqual(evidence["status"], "no_new_drc_violations", evidence)
            self.assertEqual(source.read_bytes(), original)

    def test_tail_cleanup_preserves_anchor_within_trace_width(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-offset-anchor-") as folder:
            source, board, track_id = self.board_with_dangling_track(folder)
            original = source.read_bytes()
            proposed = route(
                Segment(point(4, 2), point(7, 5), "F.Cu"),
                Segment(point(7, 5), point(8, 5), "F.Cu"),
            )
            anchor = point(6, 2.15)
            changes, removals = junction_changes(
                board,
                original,
                proposed,
                "SIGNAL",
                mm(0.2),
                mm(0.6),
                mm(0.3),
                protected_sites=((anchor, "F.Cu"),),
            )
            self.assertEqual(removals, [{"kind": "segment", "uuid": track_id}])
            self.assertEqual(
                sum("(width 0.4)" in c.get("expression", "") for c in changes), 2
            )
            evidence = validate_routes(
                source,
                hashlib.sha256(original).hexdigest(),
                changes,
                [
                    RouteContract(
                        "SIGNAL", anchor, "F.Cu", point(8, 5), "F.Cu", tuple(removals)
                    )
                ],
            )
            self.assertEqual(evidence["status"], "no_new_drc_violations", evidence)

    def test_tail_cleanup_can_be_disabled_for_moving_components(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-moving-tail-") as folder:
            source, board, _ = self.board_with_dangling_track(folder)
            proposed = route(Segment(point(4, 2), point(7, 5), "F.Cu"))
            changes, _ = junction_changes(
                board,
                source.read_bytes(),
                proposed,
                "SIGNAL",
                mm(0.2),
                mm(0.6),
                mm(0.3),
                prune_dangling_tails=False,
            )
            self.assertEqual(
                sum("(width 0.4)" in c.get("expression", "") for c in changes), 2
            )

    def test_workflow_prices_tail_removal_before_selecting_a_candidate(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-tail-cost-") as folder:
            source, _, _ = self.board_with_dangling_track(folder)
            original = source.read_bytes()
            pruning = replace(
                route(
                    Segment(point(4, 2), point(7, 5), "F.Cu"),
                    Segment(point(7, 5), point(8, 5), "F.Cu"),
                ),
                cost=1.0,
            )
            retaining = replace(
                route(
                    Segment(point(6, 2), point(8, 4), "F.Cu"),
                    Segment(point(8, 4), point(8, 5), "F.Cu"),
                ),
                cost=2.0,
            )
            choices = (
                BoardPlan((NetPlan("SIGNAL", pruning),)),
                BoardPlan((NetPlan("SIGNAL", retaining),)),
            )
            with mock.patch.object(
                workflow, "search_orders", return_value=(choices, ())
            ):
                result = workflow.plan_nets(
                    net_tests.NativeNetRoutingTests.config(),
                    Path(folder),
                    validate=False,
                    include_changes=True,
                )
            self.assertEqual(result["status"], "geometric_candidate_only", result)
            self.assertEqual(result["routing_cost"], 2.0)
            self.assertEqual(result["rework_cost"], 0.0)
            self.assertFalse(
                any(
                    change["op"] == "remove"
                    for change in result["unvalidated_campaign_changes"]
                )
            )
            self.assertEqual(source.read_bytes(), original)

    def test_workflow_propagates_native_validation_failures(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-drc-failure-") as folder:
            source, _, _ = self.board_with_dangling_track(folder)
            original = source.read_bytes()
            candidate = NetPlan(
                "SIGNAL",
                route(
                    Segment(point(4, 2), point(7, 5), "F.Cu"),
                    Segment(point(7, 5), point(8, 5), "F.Cu"),
                ),
            )
            with (
                mock.patch.object(
                    workflow,
                    "search_orders",
                    return_value=((BoardPlan((candidate,)),), ()),
                ),
                mock.patch.object(
                    workflow,
                    "validate_routes",
                    side_effect=RuntimeError("native DRC failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "native DRC failed"),
            ):
                workflow.plan_nets(
                    net_tests.NativeNetRoutingTests.config(),
                    Path(folder),
                    validate=True,
                )
            self.assertEqual(source.read_bytes(), original)

    def test_workflow_reports_tail_removal_as_rework(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-tail-accounting-") as folder:
            source, _, _ = self.board_with_dangling_track(folder)
            original = source.read_bytes()
            candidate = NetPlan(
                "SIGNAL",
                replace(
                    route(
                        Segment(point(4, 2), point(7, 5), "F.Cu"),
                        Segment(point(7, 5), point(8, 5), "F.Cu"),
                    ),
                    cost=1.0,
                ),
            )
            with mock.patch.object(
                workflow, "search_orders", return_value=((BoardPlan((candidate,)),), ())
            ):
                result = workflow.plan_nets(
                    net_tests.NativeNetRoutingTests.config(),
                    Path(folder),
                    validate=False,
                    include_changes=True,
                )
            self.assertEqual(result["status"], "geometric_candidate_only", result)
            self.assertAlmostEqual(result["rework_cost"], 3.0)
            self.assertAlmostEqual(result["cost"], 4.0)
            self.assertEqual(source.read_bytes(), original)

    def test_tail_with_other_same_net_copper_or_zone_is_preserved(self):
        for kind in ("track", "via", "zone"):
            with (
                self.subTest(kind=kind),
                tempfile.TemporaryDirectory(prefix="autopcb-tail-items-") as folder,
            ):
                source, board, track_id = self.board_with_dangling_track(folder)
                net = board.FindNet("SIGNAL")
                if kind == "track":
                    other = pcbnew.PCB_TRACK(board)
                    other.SetStart(pcbnew.VECTOR2I(mm(5), mm(2)))
                    other.SetEnd(pcbnew.VECTOR2I(mm(5), mm(3)))
                    other.SetLayer(pcbnew.F_Cu)
                    other.SetWidth(mm(0.2))
                elif kind == "via":
                    other = pcbnew.PCB_VIA(board)
                    other.SetPosition(pcbnew.VECTOR2I(mm(5), mm(2)))
                    other.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
                    other.SetWidth(mm(0.6))
                    other.SetDrill(mm(0.3))
                else:
                    other = pcbnew.ZONE(board)
                    other.SetLayer(pcbnew.F_Cu)
                    outline = other.Outline()
                    outline.NewOutline()
                    for x, y in ((4.7, 1.7), (5.3, 1.7), (5.3, 2.3), (4.7, 2.3)):
                        outline.Append(mm(x), mm(y))
                other.SetNetCode(net.GetNetCode())
                board.Add(other)
                pcbnew.SaveBoard(str(source), board)
                changes, removals = junction_changes(
                    board,
                    source.read_bytes(),
                    route(Segment(point(4, 2), point(7, 5), "F.Cu")),
                    "SIGNAL",
                    mm(0.2),
                    mm(0.6),
                    mm(0.3),
                )
                self.assertEqual(removals, [{"kind": "segment", "uuid": track_id}])
                self.assertEqual(
                    sum("(width 0.4)" in c.get("expression", "") for c in changes), 2
                )

    def test_tail_needed_by_a_proposed_via_or_segment_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-tail-proposal-") as folder:
            source, board, _ = self.board_with_dangling_track(folder)
            for proposal in (
                Route(
                    (Segment(point(4, 2), point(7, 5), "F.Cu"),),
                    (Via(point(5, 2), "F.Cu", "B.Cu"),),
                    (),
                    0,
                    0,
                    0,
                    0,
                ),
                route(
                    Segment(point(4, 2), point(7, 5), "F.Cu"),
                    Segment(point(6, 2), point(7, 3), "F.Cu"),
                ),
            ):
                with self.subTest(proposal=proposal):
                    changes, _ = junction_changes(
                        board,
                        source.read_bytes(),
                        proposal,
                        "SIGNAL",
                        mm(0.2),
                        mm(0.6),
                        mm(0.3),
                    )
                    self.assertEqual(
                        sum("(width 0.4)" in c.get("expression", "") for c in changes),
                        2,
                    )

    def board_with_track(self, folder: str, *, locked: bool = False):
        source = route_tests.RouteOptimizerTests.make_board(Path(folder))
        board = pcbnew.LoadBoard(str(source))
        net = board.FindNet("SIGNAL")
        footprint = pcbnew.FOOTPRINT(board)
        footprint.SetReference("J3")
        position = pcbnew.VECTOR2I(mm(6), mm(2))
        footprint.SetPosition(position)
        pad = pcbnew.PAD(footprint)
        pad.SetNumber("1")
        pad.SetSize(pcbnew.VECTOR2I(mm(1), mm(1)))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(pcbnew.LSET.FrontMask())
        pad.SetPosition(position)
        pad.SetNetCode(net.GetNetCode())
        footprint.Add(pad)
        board.Add(footprint)
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I(mm(2), mm(2)))
        track.SetEnd(pcbnew.VECTOR2I(mm(6), mm(2)))
        track.SetLayer(pcbnew.F_Cu)
        track.SetWidth(mm(0.4))
        track.SetNetCode(net.GetNetCode())
        track.SetLocked(locked)
        board.Add(track)
        pcbnew.SaveBoard(str(source), board)
        return source, board, track.m_Uuid.AsString()

    def test_existing_track_is_split_and_the_native_candidate_connects(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-junction-") as folder:
            source, board, track_id = self.board_with_track(folder)
            original = source.read_bytes()
            proposed = route(
                Segment(point(4, 2), point(7, 5), "F.Cu"),
                Segment(point(7, 5), point(8, 5), "F.Cu"),
            )
            changes, removals = junction_changes(
                board, original, proposed, "SIGNAL", mm(0.2), mm(0.6), mm(0.3)
            )
            self.assertEqual(removals, [{"kind": "segment", "uuid": track_id}])
            self.assertEqual(len(changes), 5)
            # The original 0.4 mm width must be preserved on both halves.
            self.assertEqual(
                sum("(width 0.4)" in c.get("expression", "") for c in changes), 2
            )
            evidence = validate_routes(
                source,
                hashlib.sha256(original).hexdigest(),
                changes,
                [
                    RouteContract(
                        "SIGNAL",
                        point(2, 2),
                        "F.Cu",
                        point(8, 5),
                        "F.Cu",
                        tuple(removals),
                    )
                ],
            )
            self.assertEqual(evidence["status"], "no_new_drc_violations", evidence)
            self.assertLess(
                evidence["candidate_unconnected"], evidence["baseline_unconnected"]
            )
            self.assertEqual(source.read_bytes(), original)

    def test_collinear_extension_merges_an_unanchored_same_width_join(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-merge-joint-") as folder:
            source, board, track_id = self.board_with_dangling_track(folder)
            original = source.read_bytes()
            proposed = route(Segment(point(6, 2), point(8, 2), "F.Cu"))
            changes, removals = junction_changes(
                board, original, proposed, "SIGNAL", mm(0.4), mm(0.6), mm(0.3)
            )
            self.assertEqual(removals, [{"kind": "segment", "uuid": track_id}])
            insertions = [
                item["expression"] for item in changes if item["op"] == "insert"
            ]
            self.assertEqual(len(insertions), 1)
            self.assertIn("(start 2 2)", insertions[0])
            self.assertIn("(end 8 2)", insertions[0])
            self.assertIn("(width 0.4)", insertions[0])
            self.assertEqual(source.read_bytes(), original)

    def test_collinear_extension_preserves_branch_and_width_transitions(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-keep-joint-") as folder:
            source, board, _ = self.board_with_dangling_track(folder)
            original = source.read_bytes()
            straight = Segment(point(6, 2), point(8, 2), "F.Cu")
            branch = Segment(point(6, 2), point(7, 3), "F.Cu")
            for proposed, width in (
                (route(straight, branch), mm(0.4)),
                (route(straight), mm(0.2)),
            ):
                _, removals = junction_changes(
                    board, original, proposed, "SIGNAL", width, mm(0.6), mm(0.3)
                )
                self.assertEqual(removals, [])

    def test_collinear_extension_does_not_move_locked_source_copper(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-locked-joint-") as folder:
            source, board, track_id = self.board_with_dangling_track(folder)
            track = next(
                item for item in board.GetTracks() if item.m_Uuid.AsString() == track_id
            )
            track.SetLocked(True)
            pcbnew.SaveBoard(str(source), board)
            proposed = route(Segment(point(6, 2), point(8, 2), "F.Cu"))
            _, removals = junction_changes(
                board,
                source.read_bytes(),
                proposed,
                "SIGNAL",
                mm(0.4),
                mm(0.6),
                mm(0.3),
            )
            self.assertEqual(removals, [])

    def test_collinear_extension_preserves_a_pad_anchor(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-anchored-joint-") as folder:
            source, board, _ = self.board_with_track(folder)
            proposed = route(Segment(point(6, 2), point(8, 2), "F.Cu"))
            _, removals = junction_changes(
                board,
                source.read_bytes(),
                proposed,
                "SIGNAL",
                mm(0.4),
                mm(0.6),
                mm(0.3),
            )
            self.assertEqual(removals, [])

    def test_collinear_extension_preserves_an_existing_split_branch(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-split-branch-") as folder:
            source, board, original_id = self.board_with_dangling_track(folder)
            branch = pcbnew.PCB_TRACK(board)
            branch.SetStart(pcbnew.VECTOR2I(mm(6), mm(2)))
            branch.SetEnd(pcbnew.VECTOR2I(mm(6), mm(4)))
            branch.SetWidth(mm(0.4))
            branch.SetLayer(pcbnew.F_Cu)
            branch.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            board.Add(branch)
            branch_id = branch.m_Uuid.AsString()
            pcbnew.SaveBoard(str(source), board)
            proposed = route(
                Segment(point(6, 2), point(8, 2), "F.Cu"),
                Segment(point(6, 3), point(7, 3), "F.Cu"),
            )
            _, removals = junction_changes(
                board,
                source.read_bytes(),
                proposed,
                "SIGNAL",
                mm(0.4),
                mm(0.6),
                mm(0.3),
                prune_dangling_tails=False,
            )
            identifiers = {item["uuid"] for item in removals}
            self.assertIn(branch_id, identifiers)
            self.assertNotIn(original_id, identifiers)

    def test_collinear_extension_preserves_a_via_anchor(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-via-joint-") as folder:
            source, board, _ = self.board_with_dangling_track(folder)
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(pcbnew.VECTOR2I(mm(6), mm(2)))
            via.SetViaType(pcbnew.VIATYPE_THROUGH)
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetWidth(mm(0.6))
            via.SetDrill(mm(0.3))
            via.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            board.Add(via)
            pcbnew.SaveBoard(str(source), board)
            proposed = route(Segment(point(6, 2), point(8, 2), "F.Cu"))
            _, removals = junction_changes(
                board,
                source.read_bytes(),
                proposed,
                "SIGNAL",
                mm(0.4),
                mm(0.6),
                mm(0.3),
            )
            self.assertEqual(removals, [])

    def test_collinear_extension_can_defer_during_placement_changes(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-moving-joint-") as folder:
            source, board, _ = self.board_with_dangling_track(folder)
            proposed = route(Segment(point(6, 2), point(8, 2), "F.Cu"))
            _, removals = junction_changes(
                board,
                source.read_bytes(),
                proposed,
                "SIGNAL",
                mm(0.4),
                mm(0.6),
                mm(0.3),
                merge_collinear=False,
            )
            self.assertEqual(removals, [])

    def test_locked_existing_copper_is_not_split(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-junction-") as folder:
            source, board, _ = self.board_with_track(folder, locked=True)
            proposed = route(Segment(point(4, 2), point(7, 5), "F.Cu"))
            with self.assertRaisesRegex(ValueError, "locked or grouped"):
                junction_changes(
                    board,
                    source.read_bytes(),
                    proposed,
                    "SIGNAL",
                    mm(0.2),
                    mm(0.6),
                    mm(0.3),
                )

    def test_new_branches_are_split_at_the_same_point(self):
        board = pcbnew.BOARD()
        proposed = route(
            Segment(point(2, 2), point(6, 2), "F.Cu"),
            Segment(point(4, 2), point(7, 5), "F.Cu"),
        )
        changes, removals = junction_changes(
            board, b"(kicad_pcb)", proposed, "SIGNAL", mm(0.2), mm(0.6), mm(0.3)
        )
        self.assertEqual(removals, [])
        self.assertEqual(len(changes), 3)
        self.assertTrue(all(c["op"] == "insert" for c in changes))

    def test_fractional_nanometre_junction_is_not_rounded(self):
        proposed = route(
            Segment(Point(0, 0), Point(1, 1), "F.Cu"),
            Segment(Point(0, 1), Point(1, 0), "F.Cu"),
        )
        with self.assertRaisesRegex(ValueError, "between native integer coordinates"):
            junction_changes(
                pcbnew.BOARD(),
                b"(kicad_pcb)",
                proposed,
                "SIGNAL",
                mm(0.2),
                mm(0.6),
                mm(0.3),
            )

    def test_nonstandard_existing_copper_is_not_replaced(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-junction-") as folder:
            source, board, _ = self.board_with_track(folder)
            original = source.read_bytes()
            altered = original.replace(b"(width 0.4)", b"(width 0.4) (custom extra)", 1)
            self.assertNotEqual(altered, original)
            proposed = route(Segment(point(4, 2), point(7, 5), "F.Cu"))
            with self.assertRaisesRegex(ValueError, "nonstandard"):
                junction_changes(
                    board, altered, proposed, "SIGNAL", mm(0.2), mm(0.6), mm(0.3)
                )
            self.assertEqual(source.read_bytes(), original)

    def test_checkpoint_validates_a_branch_on_an_existing_track(self):
        with tempfile.TemporaryDirectory(
            prefix="autopcb-junction-checkpoint-"
        ) as folder:
            source, _, _ = self.board_with_track(folder)
            original = source.read_bytes()
            project = Path(folder)
            options = net_tests.NativeNetRoutingTests.config()
            config = project / "config" / "autopcb.json"
            config.parent.mkdir()
            config.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "max_pins": 3,
                        "include_nets": ["SIGNAL"],
                        "route": options["shared"],
                        "search": options["search"],
                    }
                ),
                encoding="utf-8",
            )
            plan, items = checkpoints.create_plan(project, config)
            path = (
                checkpoints.save_plan(project, plan, items).parent
                / "checkpoints"
                / "0001.json"
            )
            candidate = NetPlan(
                "SIGNAL",
                route(
                    Segment(point(4, 2), point(7, 5), "F.Cu"),
                    Segment(point(7, 5), point(8, 5), "F.Cu"),
                ),
            )
            with mock.patch.object(checkpoints, "whole_net", return_value=(candidate,)):
                result = checkpoints.attempt(project, path)
            self.assertEqual(result["status"], "validated_candidate", result)
            self.assertLess(
                result["validation"]["candidate_unconnected"],
                result["validation"]["baseline_unconnected"],
            )
            self.assertEqual(source.read_bytes(), original)

    def test_checkpoint_prices_removed_tails_before_selecting(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-checkpoint-rework-") as folder:
            source, _, _ = self.board_with_dangling_track(folder)
            original = source.read_bytes()
            project = Path(folder)
            options = net_tests.NativeNetRoutingTests.config()
            config = project / "config" / "autopcb.json"
            config.parent.mkdir()
            config.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "max_pins": 3,
                        "include_nets": ["SIGNAL"],
                        "route": options["shared"],
                        "search": options["search"],
                    }
                ),
                encoding="utf-8",
            )
            plan, items = checkpoints.create_plan(project, config)
            path = (
                checkpoints.save_plan(project, plan, items).parent
                / "checkpoints"
                / "0001.json"
            )
            pruning = NetPlan(
                "SIGNAL",
                replace(
                    route(
                        Segment(point(4, 2), point(7, 5), "F.Cu"),
                        Segment(point(7, 5), point(8, 5), "F.Cu"),
                    ),
                    cost=1.0,
                ),
            )
            retaining = NetPlan(
                "SIGNAL",
                replace(
                    route(
                        Segment(point(6, 2), point(8, 4), "F.Cu"),
                        Segment(point(8, 4), point(8, 5), "F.Cu"),
                    ),
                    cost=2.0,
                ),
            )
            with (
                mock.patch.object(
                    checkpoints, "whole_net", return_value=(pruning, retaining)
                ),
                mock.patch.object(
                    checkpoints.NativeBackend,
                    "refine_complete",
                    side_effect=lambda net, prior, candidate: candidate,
                ),
            ):
                result = checkpoints.attempt(project, path)
            self.assertEqual(result["status"], "validated_candidate", result)
            self.assertEqual(result["route"]["cost"], 2.0)
            self.assertEqual(result["rework_cost"], 0.0)
            self.assertEqual(source.read_bytes(), original)

    def test_whole_net_workflow_validates_an_existing_track_branch(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-junction-net-") as folder:
            source, _, _ = self.board_with_track(folder)
            original = source.read_bytes()
            candidate = NetPlan(
                "SIGNAL",
                route(
                    Segment(point(4, 2), point(7, 5), "F.Cu"),
                    Segment(point(7, 5), point(8, 5), "F.Cu"),
                ),
            )
            with mock.patch.object(
                workflow, "search_orders", return_value=((BoardPlan((candidate,)),), ())
            ):
                result = workflow.plan_nets(
                    net_tests.NativeNetRoutingTests.config(),
                    Path(folder),
                    validate=True,
                    include_changes=True,
                )
            self.assertEqual(result["status"], "validated_sampled_candidate", result)
            self.assertEqual(
                result["validation"]["whole_net_connectivity"],
                "all_requested_pads_connected",
            )
            self.assertEqual(source.read_bytes(), original)

    def test_single_pin_checkpoint_keeps_a_surviving_island_anchor(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-junction-one-pin-") as folder:
            source, _, _ = self.board_with_track(folder)
            original = source.read_bytes()
            project = Path(folder)
            options = net_tests.NativeNetRoutingTests.config()
            config = project / "config" / "autopcb.json"
            config.parent.mkdir()
            config.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "max_pins": 1,
                        "include_nets": ["SIGNAL"],
                        "route": options["shared"],
                        "search": options["search"],
                    }
                ),
                encoding="utf-8",
            )
            plan, items = checkpoints.create_plan(project, config)
            path = (
                checkpoints.save_plan(project, plan, items).parent
                / "checkpoints"
                / "0001.json"
            )
            item = json.loads(path.read_text(encoding="utf-8"))
            item["anchor"]["position_mm"] = [4, 2]
            path.write_text(json.dumps(item), encoding="utf-8")
            candidate = NetPlan(
                "SIGNAL",
                route(
                    Segment(point(4, 2), point(7, 5), "F.Cu"),
                    Segment(point(7, 5), point(8, 5), "F.Cu"),
                ),
            )
            with mock.patch.object(checkpoints, "whole_net", return_value=(candidate,)):
                result = checkpoints.attempt(project, path)
            self.assertEqual(result["status"], "validated_candidate", result)
            self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
