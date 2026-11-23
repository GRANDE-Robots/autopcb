"""Protect local high-speed return corridors while leaving inner layers usable."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pcbnew

from pcb.route_optimizer import GridRouter, Point, mm
from verification import test_route_optimizer as route_fixtures


class ReferenceCorridorTests(unittest.TestCase):
    def test_local_launch_proof_rejects_a_new_slot_and_allows_only_existing_antipad(self):
        api = self.api()
        before, after = self.board(), self.board()
        bounds = [[(-1, -1), (5, -1), (5, 1), (-1, 1)]]
        old_zone = self.fill(before, pcbnew.In1_Cu, bounds)
        zone = self.fill(after, pcbnew.In1_Cu, bounds)
        zone.SetUuid(old_zone.m_Uuid)
        for item in (old_zone, zone):
            item.SetLocalClearance(mm(.25))
        via = pcbnew.PCB_VIA(before)
        via.SetNetCode(before.FindNet('HS').GetNetCode())
        via.SetPosition(pcbnew.VECTOR2I(0, 0))
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetWidth(mm(.6))
        via.SetDrill(mm(.3))
        before.Add(via)

        def cut(item, polygon):
            void = pcbnew.SHAPE_POLY_SET()
            void.NewOutline()
            for x, y in polygon:
                void.Append(mm(x), mm(y))
            copper = pcbnew.SHAPE_POLY_SET(item.GetFilledPolysList(pcbnew.In1_Cu))
            copper.BooleanSubtract(void)
            item.SetFilledPolysList(pcbnew.In1_Cu, copper)

        track = self.track(after, 'HS', pcbnew.F_Cu, (0, 0), (4, 0))
        for item in (old_zone, zone):
            cut(item, ((-.35, -.35), (.35, -.35), (.35, .35), (-.35, .35)))
        self.assertEqual(api.launch_reference_layer(before, after, (track,), (via,)), 'In1.Cu')
        self.assertIsNone(api.launch_reference_layer(before, after, (track,), ()))
        cut(zone, ((1.5, -.03), (1.7, -.03), (1.7, .03), (1.5, .03)))
        self.assertIsNone(api.launch_reference_layer(before, after, (track,), (via,)))

    def test_ground_routes_may_fill_reference_reservations_but_not_physical_keepouts(self):
        from pcb.route_optimizer import Obstacle
        api = self.api()
        board = self.board()
        self.track(board, 'HS', pcbnew.F_Cu, (0, 0), (2, 0))
        guards = api.reference_corridors(board, {'HS'}).obstacles
        values = dict(lower=Point.from_mm([-3, -3]), upper=Point.from_mm([5, 3]), step=mm(.1),
                      layers=('In1.Cu',), net='GND', width=mm(.2), clearance=mm(.2),
                      via_size=mm(.6), via_drill=mm(.3), gentle_turns=True)
        a, b = Point.from_mm([-2, 0]), Point.from_mm([4, 0])
        route = GridRouter(**values, obstacles=guards).route(a, 'In1.Cu', b, 'In1.Cu')
        self.assertAlmostEqual(route.length_mm, 6)
        physical = Obstacle(Point.from_mm([.9, -.3]), Point.from_mm([1.1, .3]),
                            ('In1.Cu',), 'GND', 'keepout')
        route = GridRouter(**values, obstacles=(*guards, physical)).route(a, 'In1.Cu', b, 'In1.Cu')
        self.assertGreater(route.length_mm, 6)

    def test_refill_comparison_uses_identical_project_rules_for_a_renamed_prior(self):
        from pcb.net_routing import native, reference, runner

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prior, candidate = root / "alias-prior.kicad_pcb", root / "v4.1.kicad_pcb"
            prior.write_bytes(b"before copper")
            candidate.write_bytes(b"after copper")
            project = b'{"board":{"design_settings":{"rules":{"min_clearance":0.37}}}}'
            rules = b'(version 1)\n(rule "test" (constraint clearance (min 0.37mm)))'
            candidate.with_suffix(".kicad_pro").write_bytes(project)
            candidate.with_suffix(".kicad_dru").write_bytes(rules)
            used = []

            def load(path, *, refill_zones):
                self.assertTrue(refill_zones)
                self.assertEqual(path.with_suffix(".kicad_pro").read_bytes(), project)
                self.assertEqual(path.with_suffix(".kicad_dru").read_bytes(), rules)
                used.append(path)
                return path.read_bytes()

            result = reference.ReferenceComparison("unchanged_sampled", (), ())
            with (patch.object(native, "load_board", side_effect=load),
                  patch.object(reference, "sample_reference", side_effect=lambda board, _: board),
                  patch.object(reference, "compare_reference", return_value=result) as compare):
                checked = runner._verify_cumulative_reference_native(prior, candidate, ("USB_DP",))
            self.assertEqual(checked["status"], "unchanged_sampled")
            compare.assert_called_once_with(b"before copper", b"after copper")
            self.assertEqual(len(used), 2)
            self.assertTrue(all(not path.parent.exists() for path in used))
            self.assertEqual(prior.read_bytes(), b"before copper")
            self.assertEqual(candidate.read_bytes(), b"after copper")
            used.clear()

            def fail(path, *, refill_zones):
                load(path, refill_zones=refill_zones)
                raise RuntimeError("refill failed")

            with (patch.object(native, "load_board", side_effect=fail),
                  self.assertRaisesRegex(RuntimeError, "refill failed")):
                runner._verify_cumulative_reference_native(prior, candidate, ("USB_DP",))
            self.assertTrue(all(not path.parent.exists() for path in used))

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec("pcb.net_routing.reference"))
        from pcb.net_routing import reference

        return reference

    def board(self):
        board = pcbnew.BOARD()
        board.SetCopperLayerCount(4)
        board.Add(pcbnew.NETINFO_ITEM(board, "HS"))
        board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
        return board

    def track(self, board, net, layer, start, end):
        track = pcbnew.PCB_TRACK(board)
        track.SetNetCode(board.FindNet(net).GetNetCode())
        track.SetLayer(layer)
        track.SetWidth(mm(0.2))
        track.SetStart(pcbnew.VECTOR2I(mm(start[0]), mm(start[1])))
        track.SetEnd(pcbnew.VECTOR2I(mm(end[0]), mm(end[1])))
        board.Add(track)
        return track

    def test_front_and_back_tracks_protect_only_their_adjacent_plane(self):
        api = self.api()
        board = self.board()
        self.track(board, "HS", pcbnew.F_Cu, (0, 0), (2, 0))
        self.track(board, "HS", pcbnew.B_Cu, (0, 4), (2, 4))
        self.track(board, "OTHER", pcbnew.F_Cu, (0, 8), (2, 8))
        corridors = api.reference_corridors(board, {"HS"}, margin_mm=0.5)
        self.assertEqual(len(corridors.obstacles), 2)
        self.assertEqual({item.layers for item in corridors.obstacles}, {("In1.Cu",), ("In2.Cu",)})
        self.assertTrue(all(item.centerline for item in corridors.obstacles))
        self.assertEqual(corridors.unsupported_items, ())

    def test_in1_route_detours_around_the_corridor_but_far_space_is_usable(self):
        api = self.api()
        board = self.board()
        self.track(board, "HS", pcbnew.F_Cu, (0, 0), (2, 0))
        corridors = api.reference_corridors(board, {"HS"}, margin_mm=0.5)
        model = GridRouter(Point.from_mm([-3, -3]), Point.from_mm([5, 3]), mm(0.25), ("In1.Cu",), "OTHER", mm(0.2), mm(0.2), mm(0.6), mm(0.3), obstacles=corridors.obstacles, gentle_turns=True)
        result = model.route(Point.from_mm([-2, 0]), "In1.Cu", Point.from_mm([4, 0]), "In1.Cu")
        self.assertGreater(result.length_mm, 6)
        direct = model.route(Point.from_mm([-2, 2]), "In1.Cu", Point.from_mm([4, 2]), "In1.Cu")
        self.assertAlmostEqual(direct.length_mm, 6)

    def test_inner_layer_high_speed_tracks_reserve_both_adjacent_layers_locally(self):
        api = self.api()
        board = self.board()
        self.track(board, "HS", pcbnew.In1_Cu, (0, 0), (2, 0))
        result = api.reference_corridors(board, {"HS"})
        self.assertEqual({item.layers for item in result.obstacles}, {("F.Cu",), ("In2.Cu",)})
        self.assertEqual(result.unsupported_items, ())
        sampled = api.sample_reference(board, {"HS"})
        self.assertFalse(sampled.sampled_coverage_complete)
        self.assertGreater(sampled.tracks[0].missing_samples, 0)

        other = self.board()
        self.track(other, "HS", pcbnew.In2_Cu, (0, 0), (2, 0))
        guards = api.reference_corridors(other, {"HS"}).obstacles
        self.assertEqual({item.layers for item in guards}, {("In1.Cu",), ("B.Cu",)})

    def test_native_obstacles_use_netclass_to_reserve_the_local_reference(self):
        from pcb.route_native import read_native_obstacles

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = route_fixtures.RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            board.SetCopperLayerCount(4)
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            self.track(board, "SIGNAL", pcbnew.F_Cu, (2, 3), (4, 3))
            self.track(board, "OTHER", pcbnew.F_Cu, (2, 6), (4, 6))
            pcbnew.SaveBoard(str(source), board)
            source.with_suffix(".kicad_pro").write_text(json.dumps({"net_settings": {"netclass_patterns": [{"pattern": "SIGNAL", "netclass": "ETH_DATA"}]}}), encoding="utf-8")
            result = read_native_obstacles(source, "OTHER", assume_refill=True)
            guards = [item for item in result.obstacles if item.layers == ("In1.Cu",) and item.net == "GND" and item.centerline]
            self.assertTrue(guards)

    def test_missing_project_rules_are_not_claimed_as_verified(self):
        from pcb.route_native import read_native_obstacles

        with tempfile.TemporaryDirectory() as tmp:
            source = route_fixtures.RouteOptimizerTests.make_board(Path(tmp))
            source.with_suffix(".kicad_pro").unlink(missing_ok=True)
            result = read_native_obstacles(source, "SIGNAL", assume_refill=True)
            self.assertTrue(hasattr(result, "reference_status"))
            self.assertEqual(result.reference_status, "project_rules_missing")

    def fill(self, board, layer, polygons):
        ground = board.FindNet("GND")
        if ground is None:
            ground = pcbnew.NETINFO_ITEM(board, "GND")
            board.Add(ground)
        zone = pcbnew.ZONE(board)
        zone.SetLayer(layer)
        zone.SetNetCode(ground.GetNetCode())
        fill = pcbnew.SHAPE_POLY_SET()
        for polygon in polygons:
            fill.NewOutline()
            for x, y in polygon:
                fill.Append(mm(x), mm(y))
        zone.SetFilledPolysList(layer, fill)
        zone.SetIsFilled(True)
        board.Add(zone)
        return zone

    def test_reference_check_detects_new_gaps_on_the_adjacent_plane(self):
        api = self.api()
        self.assertTrue(hasattr(api, "sample_reference"))
        board = self.board()
        self.track(board, "HS", pcbnew.F_Cu, (0, 0), (4, 0))
        zone = self.fill(board, pcbnew.In1_Cu, [[(-1, -1), (5, -1), (5, 1), (-1, 1)]])
        before = api.sample_reference(board, {"HS"})
        self.assertEqual(before.tracks[0].missing_samples, 0)
        board.Remove(zone)
        self.fill(board, pcbnew.In1_Cu, [[(-1, -1), (1.5, -1), (1.5, 1), (-1, 1)], [(2.5, -1), (5, -1), (5, 1), (2.5, 1)]])
        after = api.sample_reference(board, {"HS"})
        self.assertGreater(after.tracks[0].missing_samples, 0)
        self.assertEqual(api.compare_reference(before, after).status, "degraded")

    def test_trace_over_its_own_pad_is_not_counted_as_unreferenced(self):
        api = self.api()
        for plane_start, missing in ((1.2, 6), (0.9, 0)):
            with self.subTest(plane_start=plane_start):
                board = self.board()
                self.track(board, "HS", pcbnew.F_Cu, (0, 0), (4, 0))
                self.fill(board, pcbnew.In1_Cu, [[(plane_start, -1), (5, -1), (5, 1), (plane_start, 1)]])
                exposed = api.sample_reference(board, {"HS"}).tracks[0].missing_samples
                footprint = pcbnew.FOOTPRINT(board)
                pad = pcbnew.PAD(footprint)
                pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
                pad.SetSize(pcbnew.VECTOR2I(mm(2), mm(2)))
                pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
                pad.SetLayerSet(pcbnew.LSET.FrontMask())
                pad.SetNetCode(board.FindNet("HS").GetNetCode())
                footprint.Add(pad)
                board.Add(footprint)
                launch = api.sample_reference(board, {"HS"}).tracks[0]
                # Only trace between the pad edge and the plane is exposed.
                self.assertEqual(launch.missing_samples, missing)
                self.assertGreater(exposed, missing)
                self.assertEqual(launch.continuous_polygon, not missing)
                self.assertTrue(all(sample[3] for sample in launch.samples if sample[0] < mm(0.89)))

    def test_inner_reference_switch_is_detected_even_if_other_plane_covers_it(self):
        api = self.api()
        board = self.board()
        self.track(board, "HS", pcbnew.In1_Cu, (0, 0), (4, 0))
        front = self.fill(board, pcbnew.F_Cu, [[(-1, -1), (5, -1), (5, 1), (-1, 1)]])
        self.fill(board, pcbnew.In2_Cu, [[(-1, -1), (5, -1), (5, 1), (-1, 1)]])
        before = api.sample_reference(board, {"HS"})
        self.assertEqual(before.tracks[0].reference_layer, "F.Cu")
        self.assertTrue(before.sampled_coverage_complete)
        board.Remove(front)
        after = api.sample_reference(board, {"HS"})
        self.assertEqual(after.tracks[0].reference_layer, "In2.Cu")
        self.assertTrue(after.sampled_coverage_complete)
        self.assertEqual(api.compare_reference(before, after).status, "degraded")
        self.assertIsNone(api.comparable_missing_samples(before, after))
        self.assertFalse(api.reference_report(board, {"HS"})["electrically_verified"])

    def test_reference_report_is_compact_and_keeps_unverified_status(self):
        api = self.api()
        board = self.board()
        self.track(board, "HS", pcbnew.F_Cu, (0, 0), (4, 0))
        report = api.reference_report(board, {"HS"})
        self.assertEqual(report["status"], "sampled_unverified")
        self.assertFalse(report["electrically_verified"])
        self.assertGreater(report["sampled_tracks"][0]["missing_samples"], 0)
        self.assertNotIn("samples", report["sampled_tracks"][0])

    def test_reference_improvement_requires_comparable_unchanged_signal_samples(self):
        api = self.api()
        board = self.board()
        track = self.track(board, "HS", pcbnew.F_Cu, (0, 0), (4, 0))
        before = api.sample_reference(board, {"HS"})
        self.fill(board, pcbnew.In1_Cu, [[(-1, -1), (5, -1), (5, 1), (-1, 1)]])
        after = api.sample_reference(board, {"HS"})
        counts = api.comparable_missing_samples(before, after)
        self.assertGreater(counts[0], 0)
        self.assertEqual(counts[1], 0)
        track.SetEnd(pcbnew.VECTOR2I(mm(5), 0))
        moved = api.sample_reference(board, {"HS"})
        self.assertIsNone(api.comparable_missing_samples(before, moved))

    def test_missing_reference_zone_is_unverified_not_a_pass(self):
        api = self.api()
        self.assertTrue(hasattr(api, "sample_reference"))
        board = self.board()
        self.track(board, "HS", pcbnew.B_Cu, (0, 0), (4, 0))
        self.fill(board, pcbnew.In1_Cu, [[(-1, -1), (5, -1), (5, 1), (-1, 1)]])
        result = api.sample_reference(board, {"HS"})
        self.assertEqual(result.tracks[0].reference_layer, "In2.Cu")
        self.assertGreater(result.tracks[0].missing_samples, 0)
        self.assertFalse(result.sampled_coverage_complete)

    def test_existing_inner_track_that_cuts_reference_is_attributed(self):
        api = self.api()
        self.assertTrue(hasattr(api, "reference_conflicts"))
        board = self.board()
        high_speed = self.track(board, "HS", pcbnew.B_Cu, (0, 0), (4, 0))
        blocker = self.track(board, "OTHER", pcbnew.In2_Cu, (2, -1), (2, 1))
        result = api.reference_conflicts(board, {"HS"}, margin_mm=0.5, clearance_mm=0.2)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].high_speed_item_id, high_speed.m_Uuid.AsString())
        self.assertEqual(result[0].blocking_item_id, blocker.m_Uuid.AsString())
        self.assertEqual(result[0].reference_layer, "In2.Cu")

    def test_automatic_repair_selects_only_editable_copper_at_sampled_gaps(self):
        from pcb.net_routing.reference_repair import compile_reference_repairs
        from verification import test_net_routing as net_fixtures

        with tempfile.TemporaryDirectory() as tmp:
            source = route_fixtures.RouteOptimizerTests.make_board(Path(tmp))
            board = pcbnew.LoadBoard(str(source))
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            net_fixtures.NativeNetRoutingTests.add_pad(board, "J3", (3, 4), net_name="OTHER")
            hs = self.track(board, "SIGNAL", pcbnew.F_Cu, (2, 3), (6, 3))
            blocker = self.track(board, "OTHER", pcbnew.In1_Cu, (4, 2), (4, 4))
            reference = {"high_speed_nets": ["SIGNAL"],
                         "sampled_tracks": [{"item_id": hs.m_Uuid.AsString(), "missing_samples": 2, "continuous_polygon": False}],
                         "conservative_conflicts": [{"high_speed_item_id": hs.m_Uuid.AsString(),
                             "blocking_item_id": blocker.m_Uuid.AsString(), "blocking_net": "OTHER", "blocking_kind": "track"}]}
            policy = {"route": {"layers": ["B.Cu", "In2.Cu", "In1.Cu", "F.Cu"], "step_mm": .1}, "search": {}}
            with patch("pcb.net_routing.reference_repair.unqualified_working_nets", return_value=set()):
                plan = compile_reference_repairs(board, reference, policy, workers=1)
                request = plan["rounds"][0][0]
                self.assertEqual(request["rip_up"], {"OTHER": [{"kind": "segment", "uuid": blocker.m_Uuid.AsString()}]})
                self.assertEqual(request["max_islands"], 3)
                blocker.SetLocked(True)
                locked = compile_reference_repairs(board, reference, policy, workers=1)
            self.assertEqual(locked["rounds"], [])
            self.assertTrue(locked["blockers"])
            blocker.SetLocked(False)
            with patch("pcb.net_routing.reference_repair.unqualified_working_nets", return_value={"OTHER"}):
                protected = compile_reference_repairs(board, reference, policy, workers=1)
            self.assertEqual(protected["rounds"], [])
            self.assertTrue(protected["blockers"])

    def test_reference_plane_ground_copper_is_not_a_blocker(self):
        api = self.api()
        self.assertTrue(hasattr(api, "reference_conflicts"))
        board = self.board()
        board.Add(pcbnew.NETINFO_ITEM(board, "GND"))
        self.track(board, "HS", pcbnew.F_Cu, (0, 0), (4, 0))
        self.track(board, "GND", pcbnew.In1_Cu, (2, -1), (2, 1))
        self.assertEqual(api.reference_conflicts(board, {"HS"}), ())

    def test_native_validation_rejects_a_new_cut_under_high_speed(self):
        from pcb.route_cli import campaign_changes
        from pcb.route_optimizer import Route, Segment
        from pcb.route_validation import RouteContract, validate_routes

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = project / "v4.1.kicad_pcb"
            board = self.board()
            board.Add(pcbnew.NETINFO_ITEM(board, "GND"))
            for a, b in (((0, 0), (20, 0)), ((20, 0), (20, 20)), ((20, 20), (0, 20)), ((0, 20), (0, 0))):
                edge = pcbnew.PCB_SHAPE(board)
                edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
                edge.SetLayer(pcbnew.Edge_Cuts)
                edge.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1])))
                edge.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
                edge.SetWidth(mm(0.05))
                board.Add(edge)
            self.track(board, "HS", pcbnew.F_Cu, (2, 10), (8, 10))
            for reference, at in (("J1", (5, 8)), ("J2", (5, 12))):
                footprint = pcbnew.FOOTPRINT(board)
                footprint.SetReference(reference)
                pos = pcbnew.VECTOR2I(mm(at[0]), mm(at[1]))
                footprint.SetPosition(pos)
                pad = pcbnew.PAD(footprint)
                pad.SetNumber("1")
                pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
                pad.SetLayerSet(pcbnew.LSET.AllCuMask())
                pad.SetSize(pcbnew.VECTOR2I(mm(1), mm(1)))
                pad.SetDrillSize(pcbnew.VECTOR2I(mm(0.4), mm(0.4)))
                pad.SetPosition(pos)
                pad.SetNetCode(board.FindNet("OTHER").GetNetCode())
                footprint.Add(pad)
                board.Add(footprint)
            zone = pcbnew.ZONE(board)
            zone.SetLayer(pcbnew.In1_Cu)
            zone.SetNetCode(board.FindNet("GND").GetNetCode())
            zone.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_NEVER)
            outline = zone.Outline()
            outline.NewOutline()
            for x, y in ((0.5, 0.5), (19.5, 0.5), (19.5, 19.5), (0.5, 19.5)):
                outline.Append(mm(x), mm(y))
            board.Add(zone)
            pcbnew.SaveBoard(str(source), board)
            source.with_suffix(".kicad_pro").write_text(json.dumps({"net_settings": {"netclass_patterns": [{"pattern": "HS", "netclass": "ETH_DATA"}]}}), encoding="utf-8")
            a, b = Point.from_mm([5, 8]), Point.from_mm([5, 12])
            route = Route((Segment(a, b, "In1.Cu"),), (), (), 0, 0, 0, 0)
            changes = campaign_changes(route, "OTHER", mm(0.25), mm(0.6), mm(0.3))
            contract = RouteContract("OTHER", a, "In1.Cu", b, "In1.Cu")
            result = validate_routes(source, hashlib.sha256(source.read_bytes()).hexdigest(), changes, [contract], require_full_nets=True)
            self.assertEqual(result["status"], "rejected", result)
            self.assertEqual(result["reference_comparison"]["status"], "degraded")


if __name__ == "__main__":
    unittest.main()
