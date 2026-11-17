"""Whole-net search can rearrange existing copper only on a disposable copy."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pcbnew

from pcb.net_routing.workflow import plan_nets
from pcb.route_optimizer import mm
from verification import test_net_routing as net_fixtures
from verification import test_route_optimizer as route_fixtures


class RipupTests(unittest.TestCase):
    def make_project(self, project):
        source = route_fixtures.RouteOptimizerTests.make_board(project)
        board = pcbnew.LoadBoard(str(source))
        removed = []
        for a, b in (((2, 2), (4, 2)), ((4, 2), (7, 5)), ((7, 5), (8, 5))):
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1])))
            track.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
            track.SetLayer(pcbnew.F_Cu)
            track.SetWidth(mm(0.2))
            track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            board.Add(track)
            removed.append({"kind": "segment", "uuid": track.m_Uuid.AsString()})
        pcbnew.SaveBoard(str(source), board)
        return source, removed

    def test_rearranges_a_whole_existing_net_without_changing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source, removed = self.make_project(project)
            before = source.read_bytes()
            config = net_fixtures.NativeNetRoutingTests.config()
            config["rip_up"] = {"SIGNAL": removed}
            try:
                result = plan_nets(config, project, validate=True, include_changes=True)
            except ValueError as error:
                self.fail(f"Whole-net rip-up was rejected: {error}")
            self.assertEqual(result["status"], "validated_sampled_candidate", result)
            self.assertEqual(result["validation"]["whole_net_copper_connectivity"], "all_requested_copper_connected")
            self.assertEqual(result["validation"]["broken_existing_connections"], [])
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(sum(change["op"] == "remove" for change in result["unvalidated_campaign_changes"]), 3)

    def test_partial_repair_restores_a_cut_island_without_claiming_other_islands(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source, removed = self.make_project(project)
            board = pcbnew.LoadBoard(str(source))
            net_fixtures.NativeNetRoutingTests.add_pad(board, "J3", (2, 6), net_name="SIGNAL")
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            config = net_fixtures.NativeNetRoutingTests.config()
            config["max_islands"] = 2
            config["rip_up"] = {"SIGNAL": [removed[1]]}
            result = plan_nets(config, project, validate=True, include_changes=True)
            self.assertEqual(result["status"], "validated_sampled_candidate", result)
            self.assertEqual(result["scope"], "partial_net_checkpoint")
            self.assertEqual(result["validation"]["broken_existing_connections"], [])
            self.assertGreater(result["validation"]["candidate_unconnected"], 0)
            self.assertEqual(source.read_bytes(), before)

    def test_rejects_ripup_of_copper_on_an_unrequested_net(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source, removed = self.make_project(project)
            before = source.read_bytes()
            config = net_fixtures.NativeNetRoutingTests.config()
            config["rip_up"] = {"OTHER": removed}
            with self.assertRaisesRegex(ValueError, "requested"):
                plan_nets(config, project)
            self.assertEqual(source.read_bytes(), before)

    def test_track_and_via_ripup_keeps_native_board_wrappers_valid(self):
        """KiCad 10 can invalidate SWIG types after native via removal."""
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source, removed = self.make_project(project)
            board = pcbnew.LoadBoard(str(source))
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(pcbnew.VECTOR2I(mm(4), mm(2)))
            via.SetWidth(mm(0.6))
            via.SetDrill(mm(0.3))
            via.SetViaType(pcbnew.VIATYPE_THROUGH)
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            board.Add(via)
            removed.append({"kind": "via", "uuid": via.m_Uuid.AsString()})
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            config = net_fixtures.NativeNetRoutingTests.config()
            config["rip_up"] = {"SIGNAL": removed}
            with mock.patch.object(pcbnew.BOARD, "Remove", side_effect=AssertionError(
                    "Do not mutate native copper ownership during proposal construction")):
                result = plan_nets(config, project, validate=True, include_changes=True)
            self.assertEqual(result["status"], "validated_sampled_candidate", result)
            self.assertEqual(result["validation"]["broken_existing_connections"], [])
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(sum(change["op"] == "remove"
                                 for change in result["unvalidated_campaign_changes"]), 4)
            # A stale type registry also affects subsequently loaded boards.
            reloaded = pcbnew.LoadBoard(str(source))
            self.assertGreater(len(reloaded.GetTracks()), 0)
            self.assertGreater(reloaded.GetBoardEdgesBoundingBox().GetWidth(), 0)


if __name__ == "__main__":
    unittest.main()
