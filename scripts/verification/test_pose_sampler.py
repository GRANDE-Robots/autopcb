"""Move candidates come from board geometry, not only from fixed nudges."""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

import pcbnew

from pcb.net_routing.pose_sampler import PoseSampler
from pcb.route_optimizer import mm
from verification import test_net_routing as fixture
from verification import test_route_optimizer as boards


class Guide:
    """A diagnosed corridor that any pad left of x = 4 mm blocks."""

    corridors = (object(),)

    @staticmethod
    def clears(copies) -> bool:
        return all(copy.GetPosition().x >= mm(4) for copy in copies)


class PoseSamplerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.board = pcbnew.LoadBoard(str(boards.RouteOptimizerTests.make_board(Path(self.folder.name))))
        self.board.Add(pcbnew.NETINFO_ITEM(self.board, "OTHER"))
        self.part = fixture.NativeNetRoutingTests.add_pad(self.board, "R50", (3, 3), net_name="OTHER")
        fixture.NativeNetRoutingTests.add_pad(self.board, "J50", (8, 7), net_name="OTHER")
        self.selected = ((0, "R50", self.part, {"OTHER"}),)

    def tearDown(self):
        self.folder.cleanup()

    def test_the_shortest_clearing_move_comes_first(self):
        poses = list(PoseSampler(self.board, Guide(), lambda *_: True).poses(self.selected))
        self.assertEqual(poses[0].kind, "clearing")
        self.assertEqual(poses[0].moves, (("R50", 4.0, 3.0, 0.0),))
        self.assertEqual([pose.kind for pose in poses].count("clearing"), 1)
        self.assertIn(("R50", 3.5, 3.0, 0.0), [pose.moves[0] for pose in poses if pose.kind == "nudge"])

    def test_relocations_go_where_the_pads_meet_their_net_and_stay_legal(self):
        def legal(_selected, copies):
            at = copies[0].GetPosition()
            return math.hypot(at.x / 1e6 - 8, at.y / 1e6 - 7) >= 1

        class Unguided:
            corridors = ()

        poses = [pose for pose in PoseSampler(self.board, Unguided(), legal).poses(self.selected)
                 if pose.kind == "relocation"]
        self.assertEqual(len(poses), PoseSampler.RELOCATIONS)
        _, x, y, _ = poses[0].moves[0]
        # The nearest legal spot on the 1 mm grid anchored at the board outline.
        self.assertTrue(1 <= math.hypot(x - 8, y - 7) < 1.5)
        spots = [pose.moves[0][1:3] for pose in poses]
        self.assertTrue(all(math.dist(a, b) >= 2 for i, a in enumerate(spots) for b in spots[i + 1:]))


if __name__ == "__main__":
    unittest.main()
