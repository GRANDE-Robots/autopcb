"""test_routability.py: check the proofs that no route joins pads while every part stays
put.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

import pcbnew

from pcb.route_optimizer import mm
from pcb.routability import GridModel, Routability
from verification import test_route_optimizer as boards


class WallRingBuilder:
    """Builds a ring of foreign-net pads that seals a point."""

    def __call__(self, board, center=(2, 2), radius=1.3, count=8, size=1.0):
        """Add a ring of through-hole pads of another net around a point.

        The pads close the ring on every copper layer.

        Inputs:
            board: The board that receives the net OTHER and the wall parts.
            center: Centre of the ring in mm.
            radius: Radius of the ring in mm.
            count: Number of wall parts.
            size: Pad diameter in mm.
        Outputs:
            The wall parts, each with one pad, named W1 and up.
        """
        board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
        walls = []
        for index in range(count):
            angle = 2 * math.pi * index / count
            at = pcbnew.VECTOR2I(
                mm(round(center[0] + radius * math.cos(angle), 4)),
                mm(round(center[1] + radius * math.sin(angle), 4)),
            )
            footprint = pcbnew.FOOTPRINT(board)
            footprint.SetReference(f"W{index + 1}")
            footprint.SetPosition(at)
            pad = pcbnew.PAD(footprint)
            pad.SetNumber("1")
            pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
            pad.SetSize(pcbnew.VECTOR2I(mm(size), mm(size)))
            pad.SetDrillSize(pcbnew.VECTOR2I(mm(0.4), mm(0.4)))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
            pad.SetLayerSet(pcbnew.PAD.PTHMask())
            pad.SetPosition(at)
            pad.SetNetCode(board.FindNet("OTHER").GetNetCode())
            footprint.Add(pad)
            board.Add(footprint)
            walls.append(footprint)
        return walls


ring = WallRingBuilder()


class RoutabilityTests(unittest.TestCase):
    """Checks that the proof reports unroutable nets with their blocking parts."""

    def board(self, folder):
        """Load the fixture board saved in a folder.

        Inputs:
            folder: Folder that receives the board file.
        Outputs:
            The loaded board.
        """
        return pcbnew.LoadBoard(
            str(boards.RouteOptimizerTests.make_board(Path(folder)))
        )

    def test_a_ringed_pad_cannot_connect_and_every_wall_part_is_a_blocker(self):
        with tempfile.TemporaryDirectory() as folder:
            board = self.board(folder)
            self.assertEqual(Routability(board).report()["unroutable"], [])
            walls = ring(board)
            sealed = Routability(board).report()
            self.assertEqual(sealed["unroutable"], ["SIGNAL"])
            self.assertEqual(
                sorted(sealed["nets"]["SIGNAL"]["groups"]), [["J1.1"], ["J2.1"]]
            )
            # W5 to W7 face the board corner, where the edge rule closes any gap they leave.
            self.assertEqual(
                sealed["nets"]["SIGNAL"]["blockers"], ["W1", "W2", "W3", "W4", "W8"]
            )
            opened = GridModel(board, footprints={walls[0].m_Uuid.AsString(): None})
            self.assertEqual(opened.groups("SIGNAL"), [("J1.1", "J2.1")])

    def test_a_track_that_could_be_rerouted_does_not_count_as_a_block(self):
        with tempfile.TemporaryDirectory() as folder:
            board = self.board(folder)
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            wall = pcbnew.PCB_TRACK(board)
            wall.SetStart(pcbnew.VECTOR2I(mm(5), mm(0.6)))
            wall.SetEnd(pcbnew.VECTOR2I(mm(5), mm(7.4)))
            wall.SetWidth(mm(0.3))
            wall.SetLayer(pcbnew.F_Cu)
            wall.SetNetCode(board.FindNet("OTHER").GetNetCode())
            board.Add(wall)
            one_layer = {"route": {"layers": ["F.Cu"]}}
            self.assertEqual(
                Routability(board, policy=one_layer).report()["unroutable"], []
            )
            self.assertEqual(
                len(
                    GridModel(board, policy=one_layer, saved_copper=True).groups(
                        "SIGNAL"
                    )
                ),
                2,
            )

    def test_the_proof_uses_the_wider_of_the_policy_and_net_class(self):
        with tempfile.TemporaryDirectory() as folder:
            board = self.board(folder)
            walls = ring(board)
            # One missing wall leaves a 0.84 mm gap: room for a 0.2 mm track, not a 0.6 mm one.
            removed = {walls[0].m_Uuid.AsString(): None}
            self.assertEqual(
                len(GridModel(board, footprints=removed).groups("SIGNAL")), 1
            )
            wide = {"route": {"width_mm": 0.6, "clearance_mm": 0.2}}
            self.assertEqual(
                len(GridModel(board, footprints=removed, policy=wide).groups("SIGNAL")),
                2,
            )


if __name__ == "__main__":
    unittest.main()
