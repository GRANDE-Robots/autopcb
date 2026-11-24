"""test_via_guard.py: tests of the via-to-pad drill guard.

A new unqualified via must not drill into a copper pad.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from pathlib import Path
import tempfile
import unittest

import pcbnew

from pcb.net_routing.via_guard import via_pad_overlaps, verify_no_new_via_pad_overlaps


class ViaPadGuardTests(unittest.TestCase):
    """Tests that a via drilling into a pad is detected and a nearby via is allowed."""

    @staticmethod
    def board(via_x):
        """Build a board with an SMD pad at the origin and a via.

        Inputs:
            via_x: X position of the via in nm.
        Outputs:
            A board with footprint U1 holding a 1 mm square pad at (0, 0) and a via at
            (via_x, 0) with a 0.6 mm size and a 0.3 mm drill.
        """
        board = pcbnew.BOARD()
        footprint = pcbnew.FOOTPRINT(board)
        footprint.SetReference("U1")
        board.Add(footprint)
        pad = pcbnew.PAD(footprint)
        pad.SetNumber("1")
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetSize(pcbnew.VECTOR2I(1_000_000, 1_000_000))
        pad.SetLayerSet(pcbnew.LSET.FrontMask())
        footprint.Add(pad)
        pad.SetPosition(pcbnew.VECTOR2I(0, 0))
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(via_x, 0))
        via.SetWidth(600_000)
        via.SetDrill(300_000)
        board.Add(via)
        return board

    def test_drill_intersection_is_detected_but_nearby_via_is_allowed(self):
        hit = via_pad_overlaps(self.board(600_000))
        self.assertEqual(
            [(item.reference, item.pad_number) for item in hit], [("U1", "1")]
        )
        self.assertEqual(via_pad_overlaps(self.board(700_000)), ())

    def test_existing_overlap_is_not_a_new_regression(self):
        board = self.board(0)
        verify_no_new_via_pad_overlaps(board, board)

    def test_inner_layer_pad_overlap_is_detected(self):
        board = self.board(0)
        board.SetCopperLayerCount(4)
        pad = next(iter(next(iter(board.GetFootprints())).Pads()))
        layers = pcbnew.LSET()
        layers.AddLayer(pcbnew.In1_Cu)
        pad.SetLayerSet(layers)
        self.assertEqual(len(via_pad_overlaps(board)), 1)

    def test_drill_intersection_with_through_hole_pad_is_detected(self):
        board = self.board(600_000)
        board.SetCopperLayerCount(4)
        pad = next(iter(next(iter(board.GetFootprints())).Pads()))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetLayerSet(pcbnew.LSET.AllCuMask())
        self.assertEqual(len(via_pad_overlaps(board)), 1)

    def test_moving_a_pad_onto_an_existing_via_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "fixture.kicad_pcb"
            pcbnew.SaveBoard(str(source), self.board(2_000_000))
            before, after = pcbnew.LoadBoard(str(source)), pcbnew.LoadBoard(str(source))
            pad = next(iter(next(iter(after.GetFootprints())).Pads()))
            pad.SetPosition(pcbnew.VECTOR2I(2_000_000, 0))
            with self.assertRaisesRegex(RuntimeError, r"U1\.1"):
                verify_no_new_via_pad_overlaps(before, after)


if __name__ == "__main__":
    unittest.main()
