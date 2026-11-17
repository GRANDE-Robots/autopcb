"""External search guards are local, physical-layer aware and temporary."""

import json
import tempfile
import unittest
from pathlib import Path

import pcbnew

from pcb.net_routing.export_reservations import (temporary_router_reservations,
                                                reserved_native_input, strip_native_reservations)
from pcb.route_optimizer import mm


class ExportReservationTests(unittest.TestCase):
    def test_ground_margin_is_search_only_and_keeps_high_speed_reservations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            board = pcbnew.BOARD()
            board.SetCopperLayerCount(6)
            board.SetFileName(str(root / "board.kicad_pcb"))
            (root / "board.kicad_pro").write_text(json.dumps({"net_settings": {
                "netclass_patterns": [{"netclass": "USB_DATA", "pattern": "DATA"}]}}))
            for name in ("GND", "DATA"):
                board.Add(pcbnew.NETINFO_ITEM(board, name))
            corners = ((0, 0), (30, 0), (30, 30), (0, 30))
            for a, b in zip(corners, (*corners[1:], corners[0])):
                edge = pcbnew.PCB_SHAPE(board)
                edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
                edge.SetLayer(pcbnew.Edge_Cuts)
                edge.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1])))
                edge.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
                board.Add(edge)
            part = pcbnew.FOOTPRINT(board)
            part.SetReference("C1")
            pad = pcbnew.PAD(part)
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pcbnew.LSET.FrontMask())
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(pcbnew.VECTOR2I(mm(1), mm(1)))
            pad.SetPosition(pcbnew.VECTOR2I(mm(15), mm(15)))
            pad.SetNetCode(board.FindNet("GND").GetNetCode())
            part.Add(pad)
            board.Add(part)
            zone = pcbnew.ZONE(board)
            zone.SetLayer(pcbnew.F_Cu)
            zone.SetNetCode(board.FindNet("GND").GetNetCode())
            zone.Outline().NewOutline()
            for x, y in corners:
                zone.Outline().Append(mm(x), mm(y))
            board.Add(zone)
            track = pcbnew.PCB_TRACK(board)
            track.SetLayer(pcbnew.B_Cu)
            track.SetWidth(mm(.2))
            track.SetNetCode(board.FindNet("DATA").GetNetCode())
            track.SetStart(pcbnew.VECTOR2I(mm(5), mm(5)))
            track.SetEnd(pcbnew.VECTOR2I(mm(10), mm(5)))
            board.Add(track)
            board.GetConnectivity().Build(board)
            self.assertTrue(pcbnew.ZONE_FILLER(board).Fill(board.Zones()))
            areas = []
            for margin in (.5, 0):
                with temporary_router_reservations(board, ground_contact_margin_mm=margin) as counts:
                    self.assertEqual(counts["reference_areas"], 1)
                    areas.append(sum(z.GetBoundingBox().GetWidth() * z.GetBoundingBox().GetHeight()
                                     for z in board.Zones() if z.GetIsRuleArea() and z.IsOnLayer(pcbnew.F_Cu)))
                self.assertEqual({z.m_Uuid.AsString() for z in board.Zones()}, {zone.m_Uuid.AsString()})
            self.assertGreater(areas[0], areas[1])
            self.assertEqual(len(list(board.GetPads())), 1)
            self.assertEqual(len(list(board.GetTracks())), 1)

    def test_ground_margin_rejects_invalid_search_settings(self):
        for value in (-.1, 3, float("inf"), float("nan"), True, "0.5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                with temporary_router_reservations(object(), ground_contact_margin_mm=value):
                    self.fail("Invalid margin entered the search context")

    def test_native_router_input_guards_are_removed_without_touching_user_zones(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "board.kicad_pcb"
            board = pcbnew.BOARD()
            board.SetCopperLayerCount(6)
            board.SetFileName(str(path))
            board.Add(pcbnew.NETINFO_ITEM(board, "DATA"))
            track = pcbnew.PCB_TRACK(board)
            track.SetLayer(pcbnew.B_Cu)
            track.SetNetCode(board.FindNet("DATA").GetNetCode())
            track.SetStart(pcbnew.VECTOR2I(mm(10), mm(10)))
            track.SetEnd(pcbnew.VECTOR2I(mm(20), mm(20)))
            track.SetWidth(mm(.2))
            board.Add(track)
            user = pcbnew.ZONE(board)
            user.SetLayer(pcbnew.F_Cu)
            user.SetIsRuleArea(True)
            user.SetZoneName("User keepout")
            user.Outline().NewOutline()
            for x, y in ((30, 30), (32, 30), (32, 32), (30, 32)):
                user.Outline().Append(mm(x), mm(y))
            board.Add(user)
            pcbnew.SaveBoard(str(path), board)
            path.with_suffix(".kicad_pro").write_text(json.dumps({
                "net_settings": {"netclass_patterns": [{"netclass": "USB_DATA", "pattern": "DATA"}]}}))
            original, temporary, evidence = reserved_native_input(path)
            self.assertEqual(evidence["reference_areas"], 1)
            self.assertEqual(len(temporary), 1)
            guarded = path.read_bytes()
            strip_native_reservations(path, original, temporary)
            cleaned = pcbnew.LoadBoard(str(path))
            self.assertEqual({zone.m_Uuid.AsString() for zone in cleaned.Zones()}, {user.m_Uuid.AsString()})
            self.assertEqual(len(list(cleaned.GetTracks())), 1)
            # Corrupt original-zone preservation must fail before rewriting.
            path.write_bytes(guarded)
            damaged = pcbnew.LoadBoard(str(path))
            damaged.Remove(next(zone for zone in damaged.Zones()
                                if zone.m_Uuid.AsString() == user.m_Uuid.AsString()))
            pcbnew.SaveBoard(str(path), damaged)
            before_failure = path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "original zone definitions"):
                strip_native_reservations(path, original, temporary)
            self.assertEqual(path.read_bytes(), before_failure)

    def test_return_corridor_uses_in4_and_is_removed_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            board = pcbnew.BOARD()
            board.SetCopperLayerCount(6)
            board.SetFileName(str(root / "board.kicad_pcb"))
            (root / "board.kicad_pro").write_text(json.dumps({
                "net_settings": {"netclass_patterns": [{"netclass": "USB_DATA", "pattern": "DATA"}]}}))
            board.Add(pcbnew.NETINFO_ITEM(board, "DATA"))
            track = pcbnew.PCB_TRACK(board)
            track.SetLayer(pcbnew.B_Cu)
            track.SetNetCode(board.FindNet("DATA").GetNetCode())
            track.SetStart(pcbnew.VECTOR2I(mm(10), mm(10)))
            track.SetEnd(pcbnew.VECTOR2I(mm(20), mm(20)))
            track.SetWidth(mm(0.2))
            board.Add(track)
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                with temporary_router_reservations(board) as evidence:
                    self.assertEqual(evidence["reference_areas"], 1)
                    areas = list(board.Zones())
                    self.assertEqual(len(areas), 1)
                    self.assertEqual(areas[0].GetLayer(), pcbnew.In4_Cu)
                    self.assertTrue(areas[0].GetDoNotAllowTracks())
                    self.assertLess(areas[0].GetBoundingBox().GetWidth(), mm(12))
                    raise RuntimeError("interrupted")
            self.assertEqual(len(list(board.Zones())), 0)
            self.assertEqual(len(list(board.GetTracks())), 1)


if __name__ == "__main__":
    unittest.main()
