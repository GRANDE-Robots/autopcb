"""Native witnesses for obstacle diagnosis and transactional bottom movement."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pcbnew

from pcb.net_routing.batch import preview_round
from pcb.net_routing.model import Site
from pcb.net_routing.obstructions import diagnose, local_escape_witnesses, obstruction_hits
from pcb.net_routing.recovery import RecoveryPolicy
from pcb.net_routing.relocation import placement_requests
from pcb.net_routing.reroute import Obstruction
from pcb.net_routing.rework import ReworkWeights, placement_cost
from pcb.route_optimizer import GridRouter, Obstacle, Point, Route, SearchBudgetExceeded, Segment, Via, mm
from verification import test_net_routing as fixture
from verification import test_route_optimizer as boards


class ObstructionHitsTests(unittest.TestCase):
    def test_same_net_copper_is_not_mistaken_for_a_blocking_trace(self):
        class Profile:
            def pieces(self, segment):
                return ((segment, mm(.2)),)

            def clearance_at(self, segment, shape, *, width):
                return mm(.2)

        route = Route((Segment(Point.from_mm((1, 1)), Point.from_mm((3, 1)), "F.Cu"),),
                      (), (), 2, 0, 0, 0)
        same = Obstacle(Point.from_mm((1.5, .9)), Point.from_mm((2, 1.1)), ("F.Cu",), "TARGET")
        other = Obstacle(Point.from_mm((2, .9)), Point.from_mm((2.5, 1.1)), ("F.Cu",), "OTHER")
        soft = {"same": (same,), "other": (other,)}
        self.assertEqual(obstruction_hits(route, soft, Profile(), "TARGET", mm(.6), mm(.2)), {"other"})

    def test_new_vias_still_consider_nearby_existing_same_net_vias(self):
        class Profile:
            def pieces(self, segment):
                return ((segment, mm(.2)),)

            def clearance_at(self, segment, shape, *, width):
                return mm(.2)

        route = Route((), (Via(Point.from_mm((2, 2)), "F.Cu", "B.Cu"),), (), 0, 0, 0, 0)
        same_copper = Obstacle(Point.from_mm((1.9, 1.9)), Point.from_mm((2.1, 2.1)),
                               ("F.Cu",), "TARGET")
        same_via = Obstacle(Point.from_mm((2.2, 1.9)), Point.from_mm((2.4, 2.1)),
                            ("F.Cu", "B.Cu"), "TARGET", "via")
        self.assertEqual(obstruction_hits(route, {"track": (same_copper,), "via": (same_via,)},
                                          Profile(), "TARGET", mm(.6), mm(.2)), {"via"})


def track(board, net, start, end, *, bottom=False, width=0.25):
    item = pcbnew.PCB_TRACK(board)
    item.SetStart(pcbnew.VECTOR2I(mm(start[0]), mm(start[1])))
    item.SetEnd(pcbnew.VECTOR2I(mm(end[0]), mm(end[1])))
    item.SetLayer(pcbnew.B_Cu if bottom else pcbnew.F_Cu)
    item.SetWidth(mm(width))
    item.SetNetCode(board.FindNet(net).GetNetCode())
    board.Add(item)
    return item


def add_multinet_component(board):
    component = fixture.NativeNetRoutingTests.add_pad(
        board, "U50", (3, 3), bottom=True
    )
    names = {"SIGNAL"}
    for index in range(1, 8):
        name = f"INCIDENT_{index}"
        names.add(name)
        board.Add(pcbnew.NETINFO_ITEM(board, name))
        pad = pcbnew.PAD(component)
        pad.SetNumber(str(index + 1))
        pad.SetSize(pcbnew.VECTOR2I(mm(0.3), mm(0.3)))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(pcbnew.LSET.BackMask())
        pad.SetPosition(pcbnew.VECTOR2I(mm(round(3 + 0.65 * index, 2)), mm(3)))
        pad.SetNetCode(board.FindNet(name).GetNetCode())
        component.Add(pad)
    return names


class RelocationTests(unittest.TestCase):
    def test_protected_anchor_candidates_leave_protected_pad_and_copper_untouched(self):
        from pcb.net_routing.relocation import protected_anchor_pose, protected_anchor_requests

        with tempfile.TemporaryDirectory() as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            board.Add(pcbnew.NETINFO_ITEM(board, "PROTECTED"))
            part = fixture.NativeNetRoutingTests.add_pad(board, "R50", (3, 3), bottom=True)
            anchor = pcbnew.PAD(part)
            anchor.SetNumber("2")
            anchor.SetSize(pcbnew.VECTOR2I(mm(.5), mm(.75)))
            anchor.SetShape(pcbnew.PAD_SHAPE_ROUNDRECT)
            anchor.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            anchor.SetLayerSet(pcbnew.LSET.BackMask())
            anchor.SetPosition(pcbnew.VECTOR2I(mm(4), mm(3)))
            anchor.SetNetCode(board.FindNet("PROTECTED").GetNetCode())
            part.Add(anchor)
            signal_track = track(board, "SIGNAL", (3, 3), (3, 4), bottom=True)
            protected_track = track(board, "PROTECTED", (4, 3), (4, 2.5), bottom=True)
            pcbnew.SaveBoard(str(source), board)
            pose = protected_anchor_pose(part, {"PROTECTED"})
            self.assertIsNotNone(pose)
            self.assertEqual(pose.anchor_net, "PROTECTED")
            clone = pcbnew.FOOTPRINT(part)
            clone.SetOrientationDegrees(pose.angle_deg)
            clone.SetPosition(pcbnew.VECTOR2I(mm(pose.x_mm), mm(pose.y_mm)))
            rotated = next(pad for pad in clone.Pads() if pad.GetNumber() == "2")
            self.assertEqual(rotated.GetPosition(), anchor.GetPosition())
            self.assertEqual(rotated.GetBoundingBox().GetX(), anchor.GetBoundingBox().GetX())
            request = fixture.NativeNetRoutingTests.config()
            policy = {"route": request["shared"], "max_pins": 2}
            anchors = (Site(Point.from_mm((2, 2)), "F.Cu"),
                       Site(Point.from_mm((8, 5)), "F.Cu"))
            candidates, _ = protected_anchor_requests(
                board, request, policy, RecoveryPolicy(rip_up_radius_mm=2), anchors, ("PROTECTED",)
            )
            self.assertTrue(candidates)
            candidate = candidates[0]
            self.assertNotIn("PROTECTED", candidate["rip_up"])
            self.assertNotIn("PROTECTED", {item["name"] for item in candidate["nets"]})
            removed = {item["uuid"] for values in candidate["rip_up"].values() for item in values}
            self.assertIn(signal_track.m_Uuid.AsString(), removed)
            self.assertNotIn(protected_track.m_Uuid.AsString(), removed)
            omitted, _ = protected_anchor_requests(
                board, request, {**policy, "exclude_nets": ["PROTECTED"]},
                RecoveryPolicy(rip_up_radius_mm=2), anchors, ("PROTECTED",)
            )
            self.assertFalse(omitted)
            from core.kicad_campaign import CampaignPlanner
            from pcb.footprint_pose import native_pose_change
            from pcb.net_routing.protection import protected_footprint_changes

            before = source.read_bytes()
            move = native_pose_change(source, pose.reference, pose.x_mm, pose.y_mm, pose.angle_deg)
            candidate_bytes = CampaignPlanner(Path(folder)).plan("pcb", [move]).after
            candidate_path = Path(folder) / "candidate.kicad_pcb"
            candidate_path.write_bytes(candidate_bytes)
            self.assertEqual(protected_footprint_changes(
                board, pcbnew.LoadBoard(str(candidate_path)), before, candidate_bytes, ("PROTECTED",)
            ), ("R50",))
            self.assertEqual(source.read_bytes(), before)
            self.assertIsNone(protected_anchor_pose(part, {"PROTECTED", "SIGNAL"}))
            anchor.SetChamferPositions(1)
            self.assertIsNone(protected_anchor_pose(part, {"PROTECTED"}))
            anchor.SetChamferPositions(0)
            anchor.SetShape(pcbnew.PAD_SHAPE_TRAPEZOID)
            self.assertIsNone(protected_anchor_pose(part, {"PROTECTED"}))

    def test_ground_anchor_pose_keeps_exact_ground_pad_geometry(self):
        from pcb.net_routing.relocation import ground_anchor_pose

        with tempfile.TemporaryDirectory() as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            board.Add(pcbnew.NETINFO_ITEM(board, "GND"))
            part = fixture.NativeNetRoutingTests.add_pad(board, "R50", (3, 3), bottom=True)
            ground = pcbnew.PAD(part)
            ground.SetNumber("2")
            ground.SetSize(pcbnew.VECTOR2I(mm(.5), mm(.75)))
            ground.SetShape(pcbnew.PAD_SHAPE_ROUNDRECT)
            ground.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            ground.SetLayerSet(pcbnew.LSET.BackMask())
            ground.SetPosition(pcbnew.VECTOR2I(mm(4), mm(3)))
            ground.SetNetCode(board.FindNet("GND").GetNetCode())
            part.Add(ground)
            pose = ground_anchor_pose(part, {"GND"})
            self.assertIsNotNone(pose)
            self.assertEqual(pose.reference, "R50")
            self.assertEqual(pose.signal_net, "SIGNAL")
            self.assertEqual(pose.anchor_id, ground.m_Uuid.AsString())
            clone = pcbnew.FOOTPRINT(part)
            clone.SetOrientationDegrees(pose.angle_deg)
            clone.SetPosition(pcbnew.VECTOR2I(mm(pose.x_mm), mm(pose.y_mm)))
            anchored = next(pad for pad in clone.Pads() if pad.GetNumber() == "2")
            self.assertEqual(anchored.GetPosition(), ground.GetPosition())
            old_box, new_box = ground.GetBoundingBox(), anchored.GetBoundingBox()
            self.assertEqual((old_box.GetX(), old_box.GetY(), old_box.GetWidth(), old_box.GetHeight()),
                             (new_box.GetX(), new_box.GetY(), new_box.GetWidth(), new_box.GetHeight()))
            self.assertIsNone(ground_anchor_pose(part, {"GND", "SIGNAL"}))
            ground.SetShape(pcbnew.PAD_SHAPE_TRAPEZOID)
            self.assertIsNone(ground_anchor_pose(part, {"GND"}))

    def test_ground_anchor_pose_rejects_power_other_pads_and_top_parts(self):
        from pcb.net_routing.relocation import ground_anchor_pose

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
            self.assertIsNotNone(ground_anchor_pose(part, {"GND"}))
            self.assertIsNone(ground_anchor_pose(part, {"GND", "SIGNAL"}))
            part.SetLayer(pcbnew.F_Cu)
            self.assertIsNone(ground_anchor_pose(part, {"GND"}))

    def test_ground_anchor_candidates_preserve_protected_copper(self):
        from pcb.net_routing.relocation import ground_anchor_requests

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
            signal_track = track(board, "SIGNAL", (3, 3), (3, 4), bottom=True)
            ground_track = track(board, "GND", (4, 3), (4, 2.5), bottom=True)
            pcbnew.SaveBoard(str(source), board)
            request = fixture.NativeNetRoutingTests.config()
            policy = {"route": request["shared"], "max_pins": 2}
            anchors = (Site(Point.from_mm((2, 2)), "F.Cu"),
                       Site(Point.from_mm((8, 5)), "F.Cu"))
            candidates, descriptions = ground_anchor_requests(
                board, request, policy, RecoveryPolicy(rip_up_radius_mm=2), anchors, ("GND",)
            )
            self.assertTrue(candidates)
            self.assertEqual(descriptions[0]["selection"], "ground_anchor_hypothesis")
            candidate = candidates[0]
            self.assertEqual(candidate["movable"][0]["reference"], "R50")
            self.assertNotIn("GND", candidate["rip_up"])
            self.assertNotIn("GND", {item["name"] for item in candidate["nets"]})
            removed = {item["uuid"] for entries in candidate["rip_up"].values() for item in entries}
            self.assertIn(signal_track.m_Uuid.AsString(), removed)
            self.assertNotIn(ground_track.m_Uuid.AsString(), removed)
            far_anchors = (Site(Point.from_mm((9, 7)), "F.Cu"),
                           Site(Point.from_mm((9.5, 7.5)), "F.Cu"))
            farther, _ = ground_anchor_requests(
                board, request, policy, RecoveryPolicy(rip_up_radius_mm=.5), far_anchors, ("GND",)
            )
            self.assertTrue(farther, "A same-net part must not disappear outside sampled endpoints")
            rejected, _ = ground_anchor_requests(
                board, request, policy, RecoveryPolicy(rip_up_radius_mm=2), anchors, ("GND", "SIGNAL")
            )
            self.assertEqual(rejected, [])
            group = pcbnew.PCB_GROUP(board)
            group.SetName("single resistor")
            board.Add(group)
            group.AddItem(part)
            pcbnew.SaveBoard(str(source), board)
            singleton, _ = ground_anchor_requests(
                board, request, policy, RecoveryPolicy(rip_up_radius_mm=2), anchors, ("GND",)
            )
            self.assertTrue(singleton)
            group.AddItem(board.FindFootprintByReference("J1"))
            pcbnew.SaveBoard(str(source), board)
            multiple, _ = ground_anchor_requests(
                board, request, policy, RecoveryPolicy(rip_up_radius_mm=2), anchors, ("GND",)
            )
            self.assertFalse(multiple)

    def test_ground_anchor_preview_does_not_bypass_working_save_guard(self):
        from core.kicad_campaign import CampaignPlanner
        from pcb.footprint_pose import native_pose_change
        from pcb.net_routing.protection import protected_footprint_changes
        from pcb.net_routing.relocation import ground_anchor_pose

        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = boards.RouteOptimizerTests.make_board(project)
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
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            pose = ground_anchor_pose(part, {"GND"})
            change = native_pose_change(source, pose.reference, pose.x_mm, pose.y_mm, pose.angle_deg)
            candidate = CampaignPlanner(project).plan("pcb", [change]).after
            trial = project / "candidate.kicad_pcb"
            trial.write_bytes(candidate)
            after_board = pcbnew.LoadBoard(str(trial))
            self.assertEqual(protected_footprint_changes(board, after_board, before, candidate, ()), ("R50",))
            self.assertEqual(source.read_bytes(), before)

    def test_singleton_group_guard_rejects_nested_locked_or_shared_items(self):
        from pcb.net_routing.relocation import singleton_group_members

        source = """(kicad_pcb
          (group "safe" (uuid "safe-group") (members "safe-part"))
          (group "nested" (uuid "nested-group") (members "nested-part"))
          (group "outer" (uuid "outer-group") (members "nested-group" "other"))
          (group "locked" (locked yes) (uuid "locked-group") (members "locked-part"))
          (group "shared-a" (uuid "shared-a") (members "shared-part"))
          (group "shared-b" (uuid "shared-b") (members "shared-part")))"""
        self.assertEqual(singleton_group_members(source), frozenset({"safe-part"}))

    def test_local_escape_finds_only_copper_trapping_the_pad(self):
        def box(x1, y1, x2, y2, net):
            return Obstacle(Point.from_mm((x1, y1)), Point.from_mm((x2, y2)),
                            ("F.Cu",), net, "copper")

        pad = Obstacle(Point.from_mm((1.5, 1.5)), Point.from_mm((2.5, 2.5)),
                       ("F.Cu",), "SIGNAL", "pad")
        ring = (box(1.1, 1.1, 2.9, 1.2, "OTHER"),
                box(1.1, 2.8, 2.9, 2.9, "OTHER"),
                box(1.1, 1.2, 1.2, 2.8, "OTHER"),
                box(2.8, 1.2, 2.9, 2.8, "OTHER"))
        far = box(4.1, 4.1, 4.3, 4.3, "ELSE")
        settings = dict(lower=Point.from_mm((0, 0)), upper=Point.from_mm((5, 5)),
                        step=mm(.25), layers=("F.Cu",), net="SIGNAL", width=mm(.2),
                        clearance=mm(.2), via_size=mm(.6), via_drill=mm(.3))
        anchor = Site(Point.from_mm((2, 2)), "F.Cu")
        witnesses = local_escape_witnesses(GridRouter(**settings, obstacles=(pad,)),
                                            anchor, {"ring": ring, "far": (far,)},
                                            {"ring": 2.0, "far": 1.0}, lambda: None)
        self.assertTrue(witnesses)
        self.assertEqual(witnesses[0].identifiers, {"ring"})
        self.assertGreater(witnesses[0].change_cost, 0)
        # Copper that is not eligible for removal remains a hard obstacle.
        blocked = GridRouter(**settings, obstacles=(pad, *ring))
        self.assertFalse(local_escape_witnesses(blocked, anchor, {"far": (far,)},
                                                {"far": 1.0}, lambda: None))

    def test_local_escape_fallback_identifies_native_copper_after_global_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            records = []
            for first, last in (((1.1, 1.1), (2.9, 1.1)), ((2.9, 1.1), (2.9, 2.9)),
                                ((2.9, 2.9), (1.1, 2.9)), ((1.1, 2.9), (1.1, 1.1))):
                item = track(board, "OTHER", first, last, width=.1)
                box = item.GetBoundingBox()
                records.append(Obstruction(item.m_Uuid.AsString(), "OTHER", "segment",
                                           Obstacle(Point(box.GetX(), box.GetY()),
                                                    Point(box.GetRight(), box.GetBottom()),
                                                    ("F.Cu",)), mm(.1), 0, 0))
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            settings = {**fixture.NativeNetRoutingTests.config()["shared"],
                        "name": "SIGNAL", "step_mm": .25}
            recovery = RecoveryPolicy(obstruction_trials=1, obstruction_seconds=3)
            with patch.object(GridRouter, "route", side_effect=boards.SearchLimitReached("limit")):
                witnesses, _ = diagnose(
                    board, (Site(Point.from_mm((2, 2)), "F.Cu"),
                            Site(Point.from_mm((8, 5)), "F.Cu")),
                    records, settings, recovery, (), ReworkWeights(),
                )
            self.assertTrue(witnesses)
            self.assertTrue(witnesses[0].identifiers <= {item.uuid for item in records})
            self.assertEqual(source.read_bytes(), original)

    def test_local_escape_identifies_via_obstructions_on_other_layers(self):
        pad = Obstacle(Point.from_mm((1.5, 1.5)), Point.from_mm((2.5, 2.5)),
                       ("F.Cu",), "SIGNAL", "pad")
        inner = Obstacle(Point.from_mm((0, 0)), Point.from_mm((5, 5)),
                         ("In1.Cu",), "OTHER", "copper")
        own = Obstacle(Point.from_mm((2, 1.9)), Point.from_mm((4, 2.1)),
                       ("F.Cu",), "SIGNAL", "copper")
        model = GridRouter(Point.from_mm((0, 0)), Point.from_mm((5, 5)), mm(.25),
                           ("F.Cu",), "SIGNAL", mm(.2), mm(.2), mm(.6), mm(.3),
                           obstacles=(pad,))
        witnesses = local_escape_witnesses(model, Site(Point.from_mm((2, 2)), "F.Cu"),
                                            {"inner": (inner,), "own": (own,)},
                                            {"inner": 1.0, "own": 1.0}, lambda: None)
        self.assertTrue(witnesses)
        self.assertEqual(witnesses[0].identifiers, {"inner"})

    def test_local_escape_checks_budget_while_scanning_many_obstacles(self):
        pad = Obstacle(Point.from_mm((1.9, 1.9)), Point.from_mm((2.1, 2.1)),
                       ("F.Cu",), "SIGNAL", "pad")
        inner = Obstacle(Point.from_mm((0, 0)), Point.from_mm((5, 5)),
                         ("In1.Cu",), "OTHER", "copper")
        model = GridRouter(Point.from_mm((0, 0)), Point.from_mm((5, 5)), mm(.25),
                           ("F.Cu",), "SIGNAL", mm(.1), mm(.1), mm(.4), mm(.2),
                           obstacles=(pad,))
        soft = {str(index): (inner,) for index in range(500)}
        def stop():
            raise SearchBudgetExceeded("diagnostic expired")
        with self.assertRaises(SearchBudgetExceeded):
            local_escape_witnesses(model, Site(Point.from_mm((2, 2)), "F.Cu"), soft,
                                   {name: 1.0 for name in soft}, stop, max_expanded=40)

    def test_relaxed_path_identifies_both_obstructions_without_removing_them(self):
        with tempfile.TemporaryDirectory() as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            records = []
            for x in (3.5, 6.5):
                item = track(board, "OTHER", (x, -1), (x, 9))
                box = item.GetBoundingBox()
                records.append(
                    Obstruction(
                        item.m_Uuid.AsString(),
                        "OTHER",
                        "segment",
                        Obstacle(
                            Point(box.GetX(), box.GetY()),
                            Point(box.GetRight(), box.GetBottom()),
                            ("F.Cu",),
                        ),
                        mm(0.25),
                        0,
                        0,
                    )
                )
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            settings = {
                **fixture.NativeNetRoutingTests.config()["shared"],
                "name": "SIGNAL",
                "step_mm": 0.25,
            }
            recovery = RecoveryPolicy(
                obstruction_trials=1, obstruction_seconds=3, max_expansions=100000
            )
            witnesses, notes = diagnose(
                board,
                (
                    Site(Point.from_mm((2, 2)), "F.Cu"),
                    Site(Point.from_mm((8, 5)), "F.Cu"),
                ),
                records,
                settings,
                recovery,
                (),
                ReworkWeights(),
            )
            self.assertTrue(witnesses, notes)
            self.assertEqual(witnesses[0].identifiers, {item.uuid for item in records})
            self.assertGreater(witnesses[0].change_cost, 0)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(len(tuple(board.GetTracks())), 2)

    def test_obstruction_diagnosis_includes_stricter_via_hole_clearance(self):
        with tempfile.TemporaryDirectory() as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            board.GetDesignSettings().m_HoleClearance = mm(.5)
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(pcbnew.VECTOR2I(mm(5), mm(2.7)))
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetWidth(mm(.6))
            via.SetDrill(mm(.3))
            via.SetNetCode(board.FindNet("OTHER").GetNetCode())
            board.Add(via)
            pcbnew.SaveBoard(str(source), board)
            box = via.GetBoundingBox()
            record = Obstruction(
                via.m_Uuid.AsString(), "OTHER", "via",
                Obstacle(Point(box.GetX(), box.GetY()), Point(box.GetRight(), box.GetBottom()),
                         ("F.Cu", "B.Cu")),
                0, mm(.6), mm(.3),
            )
            settings = {**fixture.NativeNetRoutingTests.config()["shared"],
                        "name": "SIGNAL", "step_mm": .25}
            recovery = RecoveryPolicy(
                obstruction_trials=1, obstruction_seconds=3, max_expansions=100000
            )
            witnesses, notes = diagnose(
                board,
                (Site(Point.from_mm((2, 2)), "F.Cu"), Site(Point.from_mm((8, 2)), "F.Cu")),
                [record], settings, recovery, (), ReworkWeights(),
            )
            self.assertTrue(witnesses, notes)
            self.assertEqual(witnesses[0].identifiers, {record.uuid})

    def test_generator_preserves_top_and_excluded_parts_and_prices_moves(self):
        with tempfile.TemporaryDirectory() as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            fixture.NativeNetRoutingTests.add_pad(
                board, "R50", (3, 3), bottom=True, net_name="OTHER"
            )
            fixture.NativeNetRoutingTests.add_pad(
                board, "R51", (5, 3), bottom=True, net_name="OTHER"
            )
            old = track(board, "OTHER", (3, 3), (5, 3), bottom=True, width=0.5)
            pcbnew.SaveBoard(str(source), board)
            request = fixture.NativeNetRoutingTests.config()
            request.update(max_islands=2, recovery_stage=4)
            policy = {"route": request["shared"], "max_pins": 2}
            anchors = (
                Site(Point.from_mm((2, 2)), "F.Cu"),
                Site(Point.from_mm((8, 5)), "F.Cu"),
            )
            recovery = RecoveryPolicy(rip_up_radius_mm=2)
            candidates, _ = placement_requests(
                board, request, policy, recovery, anchors, ()
            )
            self.assertTrue(candidates)
            self.assertTrue(any(len(c["movable"]) == 2 for c in candidates))
            for candidate in candidates:
                self.assertTrue(
                    {m["reference"] for m in candidate["movable"]} <= {"R50", "R51"}
                )
                self.assertIn(
                    old.m_Uuid.AsString(),
                    {r["uuid"] for r in candidate["rip_up"]["OTHER"]},
                )
                self.assertGreaterEqual(
                    next(
                        n["width_mm"] for n in candidate["nets"] if n["name"] == "OTHER"
                    ),
                    0.5,
                )
            blocked, _ = placement_requests(
                board, request, policy, recovery, anchors, ("OTHER",)
            )
            self.assertEqual(blocked, [])
            self.assertEqual(
                placement_cost(board, {"R50": (4, 3, 90)}, ReworkWeights()), 2
            )

    def test_routing_diagnosis_moves_the_part_that_blocks_the_only_corridor(self):
        # Locked walls close the top layer and leave one bottom-layer gap,
        # which R50 fills. Two nearer bottom parts block nothing.
        def part(board, reference, at, size, net, *, bottom, locked=False):
            footprint = fixture.NativeNetRoutingTests.add_pad(
                board, reference, at, bottom=bottom, net_name=net
            )
            pad = next(iter(footprint.Pads()))
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(pcbnew.VECTOR2I(mm(size[0]), mm(size[1])))
            footprint.Reference().SetVisible(False)
            footprint.Value().SetVisible(False)
            footprint.SetLocked(locked)
            return footprint

        with tempfile.TemporaryDirectory(prefix="autopcb-guided-move-") as folder:
            project = Path(folder)
            source = boards.RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            for name in ("WALL", "BACK", "OTHER", "NEAR1", "NEAR2"):
                board.Add(pcbnew.NETINFO_ITEM(board, name))
            part(board, "W1", (6, 4), (0.6, 7.0), "WALL", bottom=False, locked=True)
            part(board, "W2", (6, 3.05), (0.6, 5.1), "BACK", bottom=True, locked=True)
            part(board, "R50", (6, 6.7), (0.6, 1.6), "OTHER", bottom=True)
            part(board, "R51", (1, 3.5), (0.5, 0.5), "NEAR1", bottom=True)
            part(board, "R52", (9, 3), (0.5, 0.5), "NEAR2", bottom=True)
            pcbnew.SaveBoard(str(source), board)
            (project / "v4.1.kicad_sch").write_text("(kicad_sch (lib_symbols))")
            request = fixture.NativeNetRoutingTests.config()
            request["shared"].update(layers=["F.Cu", "B.Cu"], step_mm=0.25)
            request.update(max_islands=2, recovery_stage=4)
            policy = {"route": request["shared"], "max_pins": 2}
            anchors = (Site(Point.from_mm((2, 2)), "F.Cu"), Site(Point.from_mm((8, 5)), "F.Cu"))
            candidates, descriptions = placement_requests(
                pcbnew.LoadBoard(str(source)), request, policy, RecoveryPolicy(), anchors, ()
            )
            self.assertEqual(descriptions[0]["selection"], "path_obstruction_move", descriptions)
            self.assertEqual(descriptions[0]["references"], ["R50"])
            self.assertTrue(descriptions[0]["clears_corridor"])
            before = source.read_bytes()
            fixed = dict(request, search={**request["search"], "seconds": 20})
            self.assertNotEqual(preview_round(project, [fixed], workers=1)["status"], "validated_preview")
            for candidate in candidates:
                candidate["search"].update(seconds=40, path_calls=100, net_path_calls=80)
                result = preview_round(project, [candidate], workers=1)
                if result["status"] == "validated_preview":
                    break
            self.assertEqual(result["status"], "validated_preview",
                             {key: result.get(key) for key in ("status", "reason")})
            self.assertEqual(result["validation"]["bottom_footprint_moves"], ["R50"])
            self.assertEqual(result["validation"]["candidate_unconnected"], 0)
            self.assertEqual(source.read_bytes(), before)

    def test_placement_generator_skips_new_courtyard_overlap(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-courtyard-pose-") as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            first = fixture.NativeNetRoutingTests.add_pad(board, "R50", (3, 3), bottom=True)
            second = fixture.NativeNetRoutingTests.add_pad(board, "R51", (4.6, 3), bottom=True)
            for footprint, x in ((first, 3), (second, 4.6)):
                shape = pcbnew.PCB_SHAPE(footprint)
                shape.SetShape(pcbnew.SHAPE_T_RECT)
                shape.SetLayer(pcbnew.B_CrtYd)
                shape.SetStart(pcbnew.VECTOR2I(mm(round(x - .6, 3)), mm(2.4)))
                shape.SetEnd(pcbnew.VECTOR2I(mm(round(x + .6, 3)), mm(3.6)))
                shape.SetWidth(mm(.05))
                footprint.Add(shape)
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            request = fixture.NativeNetRoutingTests.config()
            request.update(max_islands=2, recovery_stage=4)
            policy = {"route": request["shared"], "max_pins": 2}
            anchors = (Site(Point.from_mm((2, 2)), "F.Cu"), Site(Point.from_mm((8, 5)), "F.Cu"))
            candidates, _ = placement_requests(
                board, request, policy, RecoveryPolicy(placement_candidates=16), anchors, ()
            )
            self.assertTrue(candidates)
            self.assertFalse(any(
                len(c["movable"]) == 1 and c["movable"][0]["reference"] == "R50"
                and c["movable"][0]["positions_mm"] == [[3.5, 3.0]]
                for c in candidates
            ), "A moved courtyard must not overlap R51")
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(first.GetPosition(), pcbnew.VECTOR2I(mm(3), mm(3)))
            self.assertEqual(second.GetPosition(), pcbnew.VECTOR2I(mm(4.6), mm(3)))

    def test_placement_repairs_foreign_copper_that_its_new_pose_would_hit(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-pose-copper-") as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            fixture.NativeNetRoutingTests.add_pad(board, "R50", (3, 3), bottom=True)
            fixture.NativeNetRoutingTests.add_pad(board, "J50", (8, 7), net_name="OTHER")
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(pcbnew.VECTOR2I(mm(4.2), mm(3)))
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetWidth(mm(.6))
            via.SetDrill(mm(.3))
            via.SetNetCode(board.FindNet("OTHER").GetNetCode())
            board.Add(via)
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            request = fixture.NativeNetRoutingTests.config()
            request.update(max_islands=2, recovery_stage=4)
            policy = {"route": {**request["shared"], "clearance_mm": .2}, "max_pins": 2}
            anchors = (Site(Point.from_mm((2, 2)), "F.Cu"), Site(Point.from_mm((8, 5)), "F.Cu"))
            recovery = RecoveryPolicy(placement_candidates=16)
            candidates, _ = placement_requests(board, request, policy, recovery, anchors, ())
            pose = next(c for c in candidates if len(c["movable"]) == 1
                        and c["movable"][0]["reference"] == "R50"
                        and c["movable"][0]["positions_mm"] == [[3.5, 3.0]])
            self.assertIn("OTHER", {n["name"] for n in pose["nets"]})
            self.assertIn(via.m_Uuid.AsString(), {r["uuid"] for r in pose["rip_up"]["OTHER"]})
            protected, _ = placement_requests(board, request, policy, recovery, anchors, ("OTHER",))
            self.assertFalse(any(len(c["movable"]) == 1 and c["movable"][0]["reference"] == "R50"
                                 and c["movable"][0]["positions_mm"] == [[3.5, 3.0]]
                                 for c in protected))
            via.SetLocked(True)
            locked, _ = placement_requests(board, request, policy, recovery, anchors, ())
            self.assertFalse(any(len(c["movable"]) == 1 and c["movable"][0]["reference"] == "R50"
                                 and c["movable"][0]["positions_mm"] == [[3.5, 3.0]]
                                 for c in locked))
            self.assertEqual(source.read_bytes(), before)

    def test_pose_check_preserves_unchanged_internal_pad_geometry(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-fixed-pad-geometry-") as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            part = fixture.NativeNetRoutingTests.add_pad(board, "U50", (3, 3), bottom=True)
            second = pcbnew.PAD(part)
            second.SetNumber("2")
            second.SetSize(pcbnew.VECTOR2I(mm(.5), mm(.5)))
            second.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            second.SetLayerSet(pcbnew.LSET.BackMask())
            second.SetPosition(pcbnew.VECTOR2I(mm(3.6), mm(3)))
            second.SetNetCode(board.FindNet("OTHER").GetNetCode())
            part.Add(second)
            pcbnew.SaveBoard(str(source), board)
            request = fixture.NativeNetRoutingTests.config()
            request.update(max_islands=2, recovery_stage=4)
            policy = {"route": {**request["shared"], "clearance_mm": .2}, "max_pins": 2}
            anchors = (Site(Point.from_mm((2, 2)), "F.Cu"), Site(Point.from_mm((8, 5)), "F.Cu"))
            candidates, _ = placement_requests(
                board, request, policy, RecoveryPolicy(placement_candidates=16), anchors, ()
            )
            self.assertTrue(any(len(c["movable"]) == 1 and c["movable"][0]["reference"] == "U50"
                                and c["movable"][0]["positions_mm"] == [[3.5, 3.0]]
                                for c in candidates))

    def test_pose_check_does_not_reject_an_unchanged_existing_clearance(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-existing-gap-") as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            fixture.NativeNetRoutingTests.add_pad(board, "R50", (3, 3), bottom=True)
            fixture.NativeNetRoutingTests.add_pad(board, "R51", (3.65, 3), bottom=True, net_name="OTHER")
            pcbnew.SaveBoard(str(source), board)
            request = fixture.NativeNetRoutingTests.config()
            request.update(max_islands=2, recovery_stage=4)
            policy = {"route": {**request["shared"], "clearance_mm": .2}, "max_pins": 2}
            anchors = (Site(Point.from_mm((2, 2)), "F.Cu"), Site(Point.from_mm((8, 5)), "F.Cu"))
            candidates, _ = placement_requests(
                board, request, policy, RecoveryPolicy(placement_candidates=16), anchors, ()
            )
            self.assertTrue(any(len(c["movable"]) == 1 and c["movable"][0]["reference"] == "R50"
                                and c["movable"][0]["positions_mm"] == [[3.0, 3.0]]
                                for c in candidates))

    def test_placement_detects_foreign_via_hole_clearance(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-pose-hole-") as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            board.GetDesignSettings().m_HoleClearance = mm(.5)
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            fixture.NativeNetRoutingTests.add_pad(board, "R50", (3, 3), bottom=True)
            fixture.NativeNetRoutingTests.add_pad(board, "J50", (8, 7), net_name="OTHER")
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(pcbnew.VECTOR2I(mm(4.25), mm(3)))
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetWidth(mm(.6))
            via.SetDrill(mm(.3))
            via.SetNetCode(board.FindNet("OTHER").GetNetCode())
            board.Add(via)
            pcbnew.SaveBoard(str(source), board)
            request = fixture.NativeNetRoutingTests.config()
            request.update(max_islands=2, recovery_stage=4)
            policy = {"route": {**request["shared"], "clearance_mm": .1}, "max_pins": 2}
            anchors = (Site(Point.from_mm((2, 2)), "F.Cu"), Site(Point.from_mm((8, 5)), "F.Cu"))
            candidates, _ = placement_requests(
                board, request, policy, RecoveryPolicy(placement_candidates=16), anchors, ()
            )
            pose = next(c for c in candidates if len(c["movable"]) == 1
                        and c["movable"][0]["reference"] == "R50"
                        and c["movable"][0]["positions_mm"] == [[3.5, 3.0]])
            self.assertIn(via.m_Uuid.AsString(), {r["uuid"] for r in pose["rip_up"]["OTHER"]})

    def test_placement_uses_per_net_clearance_overrides(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-pose-override-") as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            fixture.NativeNetRoutingTests.add_pad(board, "R50", (3, 3), bottom=True)
            fixture.NativeNetRoutingTests.add_pad(board, "J50", (8, 7), net_name="OTHER")
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(pcbnew.VECTOR2I(mm(4.35), mm(3)))
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetWidth(mm(.6))
            via.SetDrill(mm(.3))
            via.SetNetCode(board.FindNet("OTHER").GetNetCode())
            board.Add(via)
            pcbnew.SaveBoard(str(source), board)
            request = fixture.NativeNetRoutingTests.config()
            request.update(max_islands=2, recovery_stage=4)
            policy = {"route": {**request["shared"], "clearance_mm": .1}, "max_pins": 2,
                      "net_overrides": {"OTHER": {"clearance_mm": .4}}}
            anchors = (Site(Point.from_mm((2, 2)), "F.Cu"), Site(Point.from_mm((8, 5)), "F.Cu"))
            candidates, _ = placement_requests(
                board, request, policy, RecoveryPolicy(placement_candidates=16), anchors, ()
            )
            pose = next(c for c in candidates if len(c["movable"]) == 1
                        and c["movable"][0]["reference"] == "R50"
                        and c["movable"][0]["positions_mm"] == [[3.5, 3.0]])
            self.assertIn(via.m_Uuid.AsString(), {r["uuid"] for r in pose["rip_up"]["OTHER"]})

    def test_generator_considers_other_nearby_bottom_component_pairs(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-pair-poses-") as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            for index, at in enumerate(((3, 3), (3, 4), (4.6, 3)), start=50):
                fixture.NativeNetRoutingTests.add_pad(board, f"R{index}", at, bottom=True)
            pcbnew.SaveBoard(str(source), board)
            request = fixture.NativeNetRoutingTests.config()
            request.update(max_islands=2, recovery_stage=4)
            policy = {"route": request["shared"], "max_pins": 2}
            anchors = (Site(Point.from_mm((2, 2)), "F.Cu"), Site(Point.from_mm((8, 5)), "F.Cu"))
            candidates, _ = placement_requests(
                board, request, policy, RecoveryPolicy(placement_candidates=32), anchors, ()
            )
            pairs = [{m["reference"] for m in c["movable"]} for c in candidates]
            self.assertIn({"R50", "R52"}, pairs)

    def test_removing_obstruction_also_repairs_newly_exposed_track_ends(self):
        from pcb.net_routing.relocation import _extend_exposed_copper

        with tempfile.TemporaryDirectory(prefix="autopcb-exposed-ends-") as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            fixture.NativeNetRoutingTests.add_pad(board, "R50", (2, 2), bottom=True)
            fixture.NativeNetRoutingTests.add_pad(board, "R51", (8, 2), bottom=True)
            first = track(board, "SIGNAL", (2, 2), (4, 2), bottom=True)
            middle = track(board, "SIGNAL", (4, 2), (6, 2), bottom=True)
            last = track(board, "SIGNAL", (6, 2), (8, 2), bottom=True)
            removals = {"SIGNAL": [{"kind": "segment", "uuid": middle.m_Uuid.AsString()}]}
            extra = _extend_exposed_copper(board, removals, (), set(), max_items=8)
            self.assertEqual({item.m_Uuid.AsString() for item in extra},
                             {first.m_Uuid.AsString(), last.m_Uuid.AsString()})
            # Native DRC must accept a replacement of the complete exposed chain.
            import hashlib
            from pcb.route_cli import campaign_changes
            from pcb.route_optimizer import Route, Segment
            from pcb.route_validation import RouteContract, validate_routes

            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            selected = [middle, *extra]
            targets = tuple({"kind": "segment", "uuid": item.m_Uuid.AsString()} for item in selected)
            changes = [{"document": "pcb", "target": target, "op": "remove", "path": []}
                       for target in targets]
            start, end = Point.from_mm((2, 2)), Point.from_mm((8, 2))
            route = Route((Segment(start, end, "B.Cu"),), (), (), 6.0, 0, 6.0, 0)
            changes.extend(campaign_changes(route, "SIGNAL", mm(.25), mm(.6), mm(.3)))
            evidence = validate_routes(
                source, hashlib.sha256(original).hexdigest(), changes,
                [RouteContract("SIGNAL", start, "B.Cu", end, "B.Cu", targets)],
            )
            self.assertEqual(evidence["status"], "no_new_drc_violations", evidence)
            self.assertFalse(evidence["new_violations"])
            self.assertFalse(evidence["broken_existing_connections"])
            self.assertEqual(source.read_bytes(), original)

    def test_exposed_end_repair_stops_at_a_surviving_branch(self):
        from pcb.net_routing.relocation import _extend_exposed_copper

        with tempfile.TemporaryDirectory(prefix="autopcb-exposed-branch-") as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            removed = track(board, "SIGNAL", (2, 3), (4, 3), bottom=True)
            track(board, "SIGNAL", (4, 3), (6, 3), bottom=True)
            track(board, "SIGNAL", (4, 3), (4, 5), bottom=True)
            extra = _extend_exposed_copper(
                board, {"SIGNAL": [{"kind": "segment", "uuid": removed.m_Uuid.AsString()}]},
                (), set(), max_items=8,
            )
            self.assertEqual(extra, ())

    def test_exposed_end_repair_does_not_skip_locks_or_item_limits(self):
        from pcb.net_routing.relocation import UnsupportedPlacement, _extend_exposed_copper

        with tempfile.TemporaryDirectory(prefix="autopcb-exposed-limits-") as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            fixture.NativeNetRoutingTests.add_pad(board, "R50", (2, 2), bottom=True)
            fixture.NativeNetRoutingTests.add_pad(board, "R51", (8, 2), bottom=True)
            first = track(board, "SIGNAL", (2, 2), (4, 2), bottom=True)
            middle = track(board, "SIGNAL", (4, 2), (6, 2), bottom=True)
            track(board, "SIGNAL", (6, 2), (8, 2), bottom=True)
            removals = {"SIGNAL": [{"kind": "segment", "uuid": middle.m_Uuid.AsString()}]}
            with self.assertRaisesRegex(UnsupportedPlacement, "item limit"):
                _extend_exposed_copper(board, removals, (), set(), max_items=1)
            first.SetLocked(True)
            with self.assertRaisesRegex(UnsupportedPlacement, "cannot be safely rerouted"):
                _extend_exposed_copper(board, removals, (), set(), max_items=8)
            self.assertEqual(len(tuple(board.GetTracks())), 3)

    def test_placement_copper_limit_is_independent_of_basic_ripup(self):
        policy = RecoveryPolicy.from_config({})
        self.assertEqual(policy.rip_up_items, 12)
        self.assertGreaterEqual(policy.placement_items, 13)
        self.assertEqual(RecoveryPolicy.from_config({"placement_items": 16}).placement_items, 16)
        for value in (0, 65, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RecoveryPolicy.from_config({"placement_items": value})

    def test_generator_keeps_every_net_of_a_multinet_bottom_component(self):
        with tempfile.TemporaryDirectory() as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            names = add_multinet_component(board)
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            request = fixture.NativeNetRoutingTests.config()
            request.update(max_islands=2, recovery_stage=4)
            policy = {"route": request["shared"], "max_pins": 2}
            anchors = (
                Site(Point.from_mm((2, 2)), "F.Cu"),
                Site(Point.from_mm((8, 5)), "F.Cu"),
            )
            recovery = RecoveryPolicy(rip_up_radius_mm=2)
            candidates, _ = placement_requests(
                board, request, policy, recovery, anchors, ()
            )
            self.assertTrue(candidates, "An eight-net bottom IC must be eligible")
            for candidate in candidates:
                self.assertEqual(
                    {m["reference"] for m in candidate["movable"]}, {"U50"}
                )
                self.assertEqual({n["name"] for n in candidate["nets"]}, names)
            blocked, _ = placement_requests(
                board, request, policy, recovery, anchors, ("INCIDENT_3",)
            )
            self.assertEqual(blocked, [])
            limited, _ = placement_requests(
                board, request, policy, RecoveryPolicy(placement_nets=7), anchors, ()
            )
            self.assertEqual(limited, [])
            self.assertEqual(source.read_bytes(), before)

    def test_multinet_placement_is_a_native_validated_transaction(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = boards.RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            names = add_multinet_component(board)
            pcbnew.SaveBoard(str(source), board)
            schematic = project / "v4.1.kicad_sch"
            schematic.write_text("(kicad_sch (lib_symbols))")
            before, sch_before = source.read_bytes(), schematic.read_bytes()
            request = fixture.NativeNetRoutingTests.config()
            request["shared"]["layers"] = ["F.Cu", "B.Cu"]
            request.update(max_islands=2, recovery_stage=4)
            policy = {"route": request["shared"], "max_pins": 2}
            anchors = (
                Site(Point.from_mm((2, 2)), "F.Cu"),
                Site(Point.from_mm((8, 5)), "F.Cu"),
            )
            candidates, _ = placement_requests(
                board, request, policy, RecoveryPolicy(rip_up_radius_mm=2), anchors, ()
            )
            self.assertTrue(candidates)
            self.assertEqual({item["name"] for item in candidates[0]["nets"]}, names)
            result = preview_round(project, [candidates[0]], workers=1)
            self.assertEqual(
                result["status"], "validated_preview",
                {key: result.get(key) for key in ("status", "reason", "attempts")},
            )
            self.assertEqual(result["validation"]["bottom_footprint_moves"], ["U50"])
            self.assertFalse(result["validation"]["broken_existing_connections"])
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(schematic.read_bytes(), sch_before)

    def test_placement_net_limit_is_independent_and_validated(self):
        policy = RecoveryPolicy.from_config({"placement_nets": 8, "rip_up_nets": 2})
        self.assertEqual(policy.placement_nets, 8)
        self.assertEqual(policy.rip_up_nets, 2)
        for invalid in (0, -1, 33, True, 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                RecoveryPolicy.from_config({"placement_nets": invalid})

    def test_placement_skips_protected_copper_but_surfaces_unexpected_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            fixture.NativeNetRoutingTests.add_pad(
                board, "R50", (3, 3), bottom=True, net_name="OTHER"
            )
            old = track(board, "OTHER", (3, 3), (5, 3), bottom=True)
            pcbnew.SaveBoard(str(source), board)
            request = fixture.NativeNetRoutingTests.config()
            request.update(max_islands=2, recovery_stage=4)
            policy = {"route": request["shared"], "max_pins": 2}
            anchors = (
                Site(Point.from_mm((2, 2)), "F.Cu"),
                Site(Point.from_mm((8, 5)), "F.Cu"),
            )
            args = (board, request, policy, RecoveryPolicy(rip_up_radius_mm=2), anchors, ())
            with patch(
                "pcb.net_routing.relocation.validate_removals",
                side_effect=ValueError("inconsistent copper records"),
            ), self.assertRaisesRegex(ValueError, "inconsistent copper records"):
                placement_requests(*args)
            old.SetLocked(True)
            blocked, _ = placement_requests(*args)
            self.assertEqual(blocked, [])
            old.SetLocked(False)
            group = pcbnew.PCB_GROUP(board)
            group.AddItem(old)
            board.Add(group)
            blocked, _ = placement_requests(*args)
            self.assertEqual(blocked, [])

    def test_partial_batch_moves_two_bottom_parts_and_repairs_previous_connectivity(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = boards.RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            fixture.NativeNetRoutingTests.add_pad(
                board, "R50", (4, 3), bottom=True, net_name="OTHER"
            )
            fixture.NativeNetRoutingTests.add_pad(
                board, "R51", (6, 3), bottom=True, net_name="OTHER"
            )
            old = track(board, "OTHER", (4, 3), (6, 3), bottom=True)
            group = pcbnew.PCB_GROUP(board)
            group.SetName("Mixed group: only the bottom members may move")
            for reference in ("R50", "R51", "J1"):
                group.AddItem(board.FindFootprintByReference(reference))
            board.Add(group)
            pcbnew.SaveBoard(str(source), board)
            schematic = project / "v4.1.kicad_sch"
            schematic.write_text("(kicad_sch (lib_symbols))")
            before, sch_before = source.read_bytes(), schematic.read_bytes()
            request = fixture.NativeNetRoutingTests.config()
            request["shared"]["layers"] = ["F.Cu", "B.Cu"]
            request["nets"].append({"name": "OTHER"})
            request.update(
                max_islands=2,
                repair_max_islands=6,
                movable=[
                    {"reference": "R50", "positions_mm": [[4, 4]]},
                    {"reference": "R51", "positions_mm": [[6, 4]]},
                ],
            )
            request["search"].update(
                seconds=40,
                path_calls=100,
                net_path_calls=80,
                tree_beam=2,
                connections_per_step=8,
            )
            rejected = preview_round(project, [request], workers=1)
            self.assertEqual(rejected["status"], "rejected")
            self.assertTrue(
                any(
                    v["type"] == "track_dangling"
                    for v in rejected["validation"]["new_violations"]
                )
            )
            request["rip_up"] = {
                "OTHER": [{"kind": "segment", "uuid": old.m_Uuid.AsString()}]
            }
            from pcb.net_routing import workflow
            with patch.object(workflow, "junction_changes", wraps=workflow.junction_changes) as junctions:
                result = preview_round(project, [request], workers=1)
            self.assertTrue(junctions.call_args_list)
            self.assertTrue(all(call.kwargs["prune_dangling_tails"] is False
                                for call in junctions.call_args_list))
            self.assertEqual(
                result["status"],
                "validated_preview",
                {key: result.get(key) for key in ("status", "reason")},
            )
            self.assertEqual(
                result["validation"]["bottom_footprint_moves"], ["R50", "R51"]
            )
            self.assertFalse(result["validation"]["broken_existing_connections"])
            self.assertEqual(result["validation"]["candidate_unconnected"], 0)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(schematic.read_bytes(), sch_before)
            from core.kicad_campaign import CampaignPlanner
            from core.kicad_sexpr import parse
            from pcb.route_validation import _canonical
            candidate = CampaignPlanner(project).plan("pcb", result["changes"]).after
            self.assertEqual(
                [_canonical(g) for g in parse(before.decode()).children("group")],
                [_canonical(g) for g in parse(candidate.decode()).children("group")],
            )


if __name__ == "__main__":
    unittest.main()
