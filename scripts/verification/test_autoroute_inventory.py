"""Board-wide coverage must not silently discard nets or uncertain pads."""

from __future__ import annotations

import importlib.util
import inspect
import hashlib
import tempfile
import unittest
from pathlib import Path

import pcbnew

from pcb.route_optimizer import mm
from verification import test_net_routing as net_fixtures
from verification import test_route_optimizer as route_fixtures


class BoardInventoryTests(unittest.TestCase):
    def inventory(self, board, schematic=None):
        self.assertIsNotNone(importlib.util.find_spec("pcb.net_routing.inventory"))
        from pcb.net_routing.inventory import inventory_board

        self.assertIn("schematic", inspect.signature(inventory_board).parameters)
        return inventory_board(board, schematic=schematic)

    def test_discovers_disconnected_pads_and_copper_only_nets(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = route_fixtures.RouteOptimizerTests.make_board(
                Path(tmp), crossing=True
            )
            board = pcbnew.LoadBoard(str(source))
            inventory = self.inventory(board)
            nets = {net.name: net for net in inventory.nets}
            self.assertEqual(set(nets), {"SIGNAL", "OTHER"})
            self.assertEqual(len(nets["SIGNAL"].pad_ids), 2)
            self.assertEqual(len(nets["SIGNAL"].pad_islands), 2)
            self.assertEqual(len(nets["OTHER"].track_ids), 1)
            self.assertEqual(nets["OTHER"].pad_ids, ())

    def test_connected_pad_islands_are_counted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = route_fixtures.RouteOptimizerTests.make_board(Path(tmp))
            board = pcbnew.LoadBoard(str(source))
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(mm(2), mm(2)))
            track.SetEnd(pcbnew.VECTOR2I(mm(8), mm(5)))
            track.SetLayer(pcbnew.F_Cu)
            track.SetWidth(mm(0.2))
            track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            board.Add(track)
            net = self.inventory(board).nets[0]
            self.assertEqual(len(net.pad_islands), 1)
            self.assertEqual(set(net.pad_islands[0]), set(net.pad_ids))

    def test_isolated_track_island_is_inventoried_even_when_pads_are_connected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = route_fixtures.RouteOptimizerTests.make_board(Path(tmp))
            board = pcbnew.LoadBoard(str(source))
            for a, b in (((2, 2), (8, 5)), ((2, 6), (4, 6))):
                track = pcbnew.PCB_TRACK(board)
                track.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1])))
                track.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
                track.SetLayer(pcbnew.F_Cu)
                track.SetWidth(mm(0.2))
                track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
                board.Add(track)
            net = self.inventory(board).nets[0]
            self.assertTrue(hasattr(net, "copper_islands"))
            self.assertEqual(len(net.pad_islands), 1)
            self.assertEqual(len(net.copper_islands), 2)

    def test_native_route_search_sees_isolated_tracks_without_pads(self):
        from pcb.net_routing.model import Budget, Limits
        from pcb.net_routing.native import discover

        with tempfile.TemporaryDirectory() as tmp:
            source = route_fixtures.RouteOptimizerTests.make_board(Path(tmp))
            board = pcbnew.LoadBoard(str(source))
            for a, b in (((2, 2), (8, 5)), ((2, 6), (4, 6))):
                track = pcbnew.PCB_TRACK(board)
                track.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1])))
                track.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
                track.SetLayer(pcbnew.F_Cu)
                track.SetWidth(mm(0.2))
                track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
                board.Add(track)
            board.GetConnectivity().Build(board)
            result = discover(board, "SIGNAL", ("F.Cu",), Budget(Limits()))
            self.assertEqual(len(result.net.islands), 2)
            self.assertTrue(any(not island.pad_ids for island in result.net.islands))

    def test_whole_net_validation_includes_isolated_track_island(self):
        from pcb.net_routing.workflow import plan_nets

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = route_fixtures.RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            for a, b in (((2, 2), (8, 5)), ((2, 6), (4, 6))):
                track = pcbnew.PCB_TRACK(board)
                track.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1])))
                track.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
                track.SetLayer(pcbnew.F_Cu)
                track.SetWidth(mm(0.2))
                track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
                board.Add(track)
            pcbnew.SaveBoard(str(source), board)
            result = plan_nets(
                net_fixtures.NativeNetRoutingTests.config(), project, validate=True
            )
            self.assertEqual(result["status"], "validated_sampled_candidate", result)
            self.assertIn("whole_net_copper_connectivity", result["validation"])
            self.assertEqual(
                result["validation"]["whole_net_copper_connectivity"],
                "all_requested_copper_connected",
            )
            self.assertEqual(result["validation"]["candidate_unconnected"], 0)

    def test_whole_net_validation_rejects_a_remaining_orphan_island(self):
        from pcb.route_cli import campaign_changes
        from pcb.route_optimizer import Point, Route, Segment
        from pcb.route_validation import RouteContract, validate_routes

        self.assertIn(
            "require_full_nets", inspect.signature(validate_routes).parameters
        )
        with tempfile.TemporaryDirectory() as tmp:
            source = route_fixtures.RouteOptimizerTests.make_board(Path(tmp))
            board = pcbnew.LoadBoard(str(source))
            orphan = pcbnew.PCB_TRACK(board)
            orphan.SetStart(pcbnew.VECTOR2I(mm(2), mm(6)))
            orphan.SetEnd(pcbnew.VECTOR2I(mm(4), mm(6)))
            orphan.SetLayer(pcbnew.F_Cu)
            orphan.SetWidth(mm(0.2))
            orphan.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            board.Add(orphan)
            pcbnew.SaveBoard(str(source), board)
            a, bend, b = (Point.from_mm(value) for value in ([2, 2], [5, 2], [8, 5]))
            segments = (Segment(a, bend, "F.Cu"), Segment(bend, b, "F.Cu"))
            candidate = Route(segments, (), (), 0, 0, 0, 0)
            changes = campaign_changes(candidate, "SIGNAL", mm(0.2), mm(0.6), mm(0.3))
            contract = RouteContract("SIGNAL", a, "F.Cu", b, "F.Cu")
            with self.assertRaisesRegex(RuntimeError, "copper.*island"):
                validate_routes(
                    source,
                    hashlib.sha256(source.read_bytes()).hexdigest(),
                    changes,
                    [contract],
                    require_full_nets=True,
                )

    def test_single_pad_net_still_gets_a_whole_net_contract(self):
        from pcb.net_routing.native import contracts

        with tempfile.TemporaryDirectory() as tmp:
            source = route_fixtures.RouteOptimizerTests.make_board(Path(tmp))
            board = pcbnew.LoadBoard(str(source))
            board.Remove(board.FindFootprintByReference("J2"))
            result = contracts(board, ("SIGNAL",))
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].start, result[0].end)

    def test_unassigned_copper_is_never_discarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = route_fixtures.RouteOptimizerTests.make_board(Path(tmp))
            board = pcbnew.LoadBoard(str(source))
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(mm(1), mm(6)))
            track.SetEnd(pcbnew.VECTOR2I(mm(3), mm(6)))
            track.SetWidth(mm(0.2))
            track.SetLayer(pcbnew.F_Cu)
            board.Add(track)
            self.assertIn(
                track.m_Uuid.AsString(), self.inventory(board).unassigned_copper_ids
            )

    def test_unconnected_name_is_not_evidence_of_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = route_fixtures.RouteOptimizerTests.make_board(Path(tmp))
            board = pcbnew.LoadBoard(str(source))
            name = "unconnected-(J3-Pad1)"
            board.Add(pcbnew.NETINFO_ITEM(board, name))
            footprint = net_fixtures.NativeNetRoutingTests.add_pad(
                board, "J3", (4, 5), net_name=name
            )
            uid = next(iter(footprint.Pads())).m_Uuid.AsString()
            inventory = self.inventory(board)
            self.assertIn(uid, inventory.unverified_no_connect_pad_ids)
            self.assertIn(name, {net.name for net in inventory.nets})

    def test_empty_electrical_pad_is_not_treated_as_a_mechanical_hole(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = route_fixtures.RouteOptimizerTests.make_board(Path(tmp))
            board = pcbnew.LoadBoard(str(source))
            footprint = net_fixtures.NativeNetRoutingTests.add_pad(board, "J3", (4, 5))
            pad = next(iter(footprint.Pads()))
            pad.SetNetCode(0)
            inventory = self.inventory(board)
            self.assertIn(pad.m_Uuid.AsString(), inventory.unassigned_pad_ids)

    def test_paste_only_pad_is_not_an_unassigned_electrical_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = route_fixtures.RouteOptimizerTests.make_board(Path(tmp))
            board = pcbnew.LoadBoard(str(source))
            footprint = net_fixtures.NativeNetRoutingTests.add_pad(board, "J3", (4, 5))
            pad = next(iter(footprint.Pads()))
            pad.SetNetCode(0)
            layer_set = pcbnew.LSET()
            layer_set.AddLayer(pcbnew.B_Paste)
            pad.SetLayerSet(layer_set)
            self.assertNotIn(
                pad.m_Uuid.AsString(), self.inventory(board).unassigned_pad_ids
            )

    def test_only_unambiguous_schematic_marker_proves_a_no_connect(self):
        schematic = """(kicad_sch
          (lib_symbols (symbol "Test:Pin" (symbol "Pin_1_1"
            (pin passive line (at 0 0 0) (name "A") (number "1")))))
          (symbol (lib_id "Test:Pin") (at 4 5 0) (unit 1) (property "Reference" "J3"))
          (no_connect (at 4 5)))"""
        with tempfile.TemporaryDirectory() as tmp:
            source = route_fixtures.RouteOptimizerTests.make_board(Path(tmp))
            board = pcbnew.LoadBoard(str(source))
            name = "unconnected-(J3-Pad1)"
            board.Add(pcbnew.NETINFO_ITEM(board, name))
            footprint = net_fixtures.NativeNetRoutingTests.add_pad(
                board, "J3", (4, 5), net_name=name
            )
            uid = next(iter(footprint.Pads())).m_Uuid.AsString()
            inventory = self.inventory(board, schematic)
            self.assertIn(uid, inventory.verified_no_connect_pad_ids)
            self.assertNotIn(uid, inventory.unverified_no_connect_pad_ids)
            conflicting = schematic.replace(
                "(no_connect (at 4 5))",
                "(wire (pts (xy 4 5) (xy 6 5))) (no_connect (at 4 5))",
            )
            self.assertIn(
                uid, self.inventory(board, conflicting).unverified_no_connect_pad_ids
            )


if __name__ == "__main__":
    unittest.main()
