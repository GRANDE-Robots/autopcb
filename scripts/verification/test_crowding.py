"""test_crowding.py: check crowded packings.

Crowded packings move only parts the policy lets move and never overlap courtyards.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

import pcbnew

from autopcb.crowding import CrowdedVariant
from pcb.net_routing.relocation import _courtyard_conflicts, _courtyard_geometry
from pcb.route_optimizer import mm
from verification import test_net_routing as fixture
from verification import test_route_optimizer as boards


class CourtyardBuilder:
    """Draws square courtyards on footprints of native boards for the tests."""

    @staticmethod
    def add_to(footprint, half=0.6):
        """Add a square courtyard rectangle to a footprint.

        The rectangle is drawn on F.CrtYd with a line width of 0.05 mm and is centered
        on the footprint position.

        Inputs:
            footprint: native footprint that receives the rectangle.
            half: half the side length of the rectangle in mm.
        """
        at = footprint.GetPosition()
        shape = pcbnew.PCB_SHAPE(footprint)
        shape.SetShape(pcbnew.SHAPE_T_RECT)
        shape.SetLayer(pcbnew.F_CrtYd)
        shape.SetStart(pcbnew.VECTOR2I(at.x - mm(half), at.y - mm(half)))
        shape.SetEnd(pcbnew.VECTOR2I(at.x + mm(half), at.y + mm(half)))
        shape.SetWidth(mm(0.05))
        footprint.Add(shape)


class CrowdingTests(unittest.TestCase):
    """Tests for CrowdedVariant packings."""

    def test_packing_is_seeded_legal_and_keeps_fixed_parts(self):
        with tempfile.TemporaryDirectory() as folder:
            source = boards.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            for footprint in board.GetFootprints():
                CourtyardBuilder.add_to(footprint)
            for reference, at in (("R1", (5, 2)), ("R2", (3, 6)), ("R3", (7, 3))):
                CourtyardBuilder.add_to(
                    fixture.NativeNetRoutingTests.add_pad(board, reference, at)
                )
            # The policy keeps locked parts fixed, so the packing must too.
            board.FindFootprintByReference("J2").SetLocked(True)
            pcbnew.SaveBoard(str(source), board)
            unpacked = source.read_bytes()
            policy = {"placement": {"locked": False}}
            result = CrowdedVariant(source, 3, policy).write()
            moved = {item["reference"] for item in result["moved"]}
            self.assertTrue(moved)
            self.assertNotIn("J2", moved)
            packed = pcbnew.LoadBoard(str(source))
            geometry = {
                fp.m_Uuid.AsString(): _courtyard_geometry(fp)
                for fp in packed.GetFootprints()
            }
            self.assertEqual(_courtyard_conflicts(geometry, geometry), set())
            edges, gap = (
                packed.GetBoardEdgesBoundingBox(),
                packed.GetDesignSettings().m_CopperEdgeClearance,
            )
            for pad in packed.GetPads():
                box = pad.GetBoundingBox()
                self.assertTrue(
                    edges.GetX() + gap <= box.GetX()
                    and box.GetRight() <= edges.GetRight() - gap
                    and edges.GetY() + gap <= box.GetY()
                    and box.GetBottom() <= edges.GetBottom() - gap,
                    pad.GetParentFootprint().GetReference(),
                )
            point = result["point_mm"]
            for item in result["moved"]:
                self.assertLess(
                    math.dist(item["to_mm"], point) - 1e-6,
                    math.dist(item["from_mm"], point),
                )
            self.assertEqual(result["routability"]["unroutable"], [])
            source.write_bytes(unpacked)
            self.assertEqual(
                CrowdedVariant(source, 3, policy).write()["moved"], result["moved"]
            )


if __name__ == "__main__":
    unittest.main()
