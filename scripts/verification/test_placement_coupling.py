"""test_placement_coupling.py: moves and rerouting are proposed and checked together.

Two small one-layer boards, each closed by locked walls or copper: a barrier
that a relocation opens, and a doorway sealed by a movable part and a fence
track at once.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pcbnew

from pcb.net_routing.batch import preview_round
from pcb.net_routing.model import Site
from pcb.net_routing.recovery import RecoveryPolicy
from pcb.net_routing.relocation import placement_requests
from pcb.net_routing.reroute import compile_reroutes
from pcb.route_optimizer import Point, mm
from verification import test_net_routing as fixture
from verification import test_route_optimizer as boards


class SmallBoards:
    """Builds the one-layer boards on a disposable project folder."""

    def __init__(self, project: Path):
        """Start from the stock 10 x 8 mm board with pads J1 and J2 on SIGNAL."""
        self.project = project
        self.source = boards.RouteOptimizerTests.make_board(project)
        self.board = pcbnew.LoadBoard(str(self.source))

    def net(self, *names: str) -> None:
        """Add nets to the board."""
        for name in names:
            self.board.Add(pcbnew.NETINFO_ITEM(self.board, name))

    def part(self, reference, at, size, net, *, locked=False):
        """Add a one-pad SMD part with a 0.25 mm courtyard margin on F.Cu."""
        footprint = fixture.NativeNetRoutingTests.add_pad(
            self.board, reference, at, net_name=net
        )
        pad = next(iter(footprint.Pads()))
        pad.SetShape(pcbnew.PAD_SHAPE_RECT)
        pad.SetSize(pcbnew.VECTOR2I(mm(size[0]), mm(size[1])))
        footprint.Reference().SetVisible(False)
        footprint.Value().SetVisible(False)
        footprint.SetLocked(locked)
        self.courtyard(footprint, size)
        return footprint

    @staticmethod
    def courtyard(footprint, size, margin=0.25) -> None:
        """Draw the courtyard rectangle around a footprint's pad."""
        at = footprint.GetPosition()
        half = (mm(size[0] / 2 + margin), mm(size[1] / 2 + margin))
        shape = pcbnew.PCB_SHAPE(footprint)
        shape.SetShape(pcbnew.SHAPE_T_RECT)
        shape.SetLayer(pcbnew.F_CrtYd)
        shape.SetStart(pcbnew.VECTOR2I(at.x - half[0], at.y - half[1]))
        shape.SetEnd(pcbnew.VECTOR2I(at.x + half[0], at.y + half[1]))
        shape.SetWidth(mm(0.05))
        footprint.Add(shape)

    def place(self, reference, at, *, locked=True) -> None:
        """Move one of the stock pads and give it a courtyard."""
        footprint = self.board.FindFootprintByReference(reference)
        footprint.SetPosition(pcbnew.VECTOR2I(mm(at[0]), mm(at[1])))
        footprint.SetLocked(locked)
        footprint.Reference().SetVisible(False)
        footprint.Value().SetVisible(False)
        self.courtyard(footprint, (1.0, 1.0))

    def track(self, net, start, end):
        """Add an F.Cu segment of 0.25 mm and return it."""
        item = pcbnew.PCB_TRACK(self.board)
        item.SetStart(pcbnew.VECTOR2I(mm(start[0]), mm(start[1])))
        item.SetEnd(pcbnew.VECTOR2I(mm(end[0]), mm(end[1])))
        item.SetLayer(pcbnew.F_Cu)
        item.SetWidth(mm(0.25))
        item.SetNetCode(self.board.FindNet(net).GetNetCode())
        self.board.Add(item)
        return item

    def save(self) -> Path:
        """Write the board and its schematic stub; return the board path."""
        pcbnew.SaveBoard(str(self.source), self.board)
        (self.project / "v4.1.kicad_sch").write_text("(kicad_sch (lib_symbols))")
        return self.source

    def barrier(self) -> Path:
        """Pad, track and pad span the board on F.Cu; R50 sits behind it.

        J1 and J2 touch the copper edge limit, so the SIGNAL track closes the
        board. OTHER joins B1 below the track to R50 above it, and only R50
        may move.
        """
        self.net("OTHER")
        self.place("J1", (1.0, 2.0))
        self.place("J2", (9.0, 5.0))
        self.part("B1", (2, 6.5), (0.5, 0.5), "OTHER", locked=True)
        self.part("R50", (8, 1.5), (0.5, 0.5), "OTHER")
        self.track("SIGNAL", (1, 2), (9, 5))
        return self.save()

    def doorway(self) -> tuple[Path, str]:
        """A wall with one doorway, sealed by part P1 and by a fence track.

        SIGNAL runs from J1 on the left to J2 on the right. P1 sits at the
        doorway's entrance. Right of the wall, the YNET track from Y1 down to
        Y2 and the locked wall W3 close the board. Moving P1 alone leaves the
        fence, and replacing the fence alone leaves P1.

        Returns the board path and the fence track's UUID.
        """
        self.net("WALL1", "WALL2", "WALL3", "ZNET", "YNET")
        self.place("J1", (2.0, 4.0))
        self.place("J2", (8.0, 4.0))
        self.part("W1", (5.0, 1.85), (0.6, 2.7), "WALL1", locked=True)
        self.part("W2", (5.0, 6.15), (0.6, 2.7), "WALL2", locked=True)
        self.part("P1", (4.3, 4.0), (0.5, 1.0), "ZNET")
        self.part("Z2", (2.0, 6.5), (0.5, 0.5), "ZNET", locked=True)
        self.part("Y1", (6.2, 1.3), (0.5, 0.5), "YNET", locked=True)
        self.part("Y2", (6.2, 4.7), (0.5, 0.5), "YNET", locked=True)
        self.part("W3", (6.2, 6.475), (0.6, 2.05), "WALL3", locked=True)
        fence = self.track("YNET", (6.2, 1.3), (6.2, 4.7))
        return self.save(), fence.m_Uuid.AsString()


class PlacementCouplingTests(unittest.TestCase):
    """Fixed placement stalls where a move, or a move with rerouting, is needed."""

    @staticmethod
    def request(layers=("F.Cu",)) -> dict:
        """A stage-four request on one layer at a 0.25 mm step."""
        request = fixture.NativeNetRoutingTests.config()
        request["shared"].update(layers=list(layers), step_mm=0.25)
        request.update(max_islands=2, recovery_stage=4)
        return request

    def test_a_relocation_opens_a_barrier_that_fixed_placement_cannot_cross(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-barrier-") as folder:
            project = Path(folder)
            source = SmallBoards(project).barrier()
            before = source.read_bytes()
            request = self.request()
            request["nets"] = [{"name": "OTHER"}]
            policy = {
                "route": request["shared"],
                "max_pins": 2,
                "placement": {"locked": False},
            }
            fixed = dict(request, search={**request["search"], "seconds": 10})
            self.assertNotEqual(
                preview_round(project, [fixed], workers=1)["status"],
                "validated_preview",
            )
            anchors = (
                Site(Point.from_mm((2, 6.5)), "F.Cu"),
                Site(Point.from_mm((8, 1.5)), "F.Cu"),
            )
            candidates, descriptions = placement_requests(
                pcbnew.LoadBoard(str(source)),
                request,
                policy,
                RecoveryPolicy(),
                anchors,
                (),
            )
            self.assertEqual(descriptions[0]["pose"], "relocation")
            result = {}
            for candidate in candidates:
                candidate["search"].update(seconds=10)
                result = preview_round(project, [candidate], workers=1)
                if result["status"] == "validated_preview":
                    break
            self.assertEqual(result["status"], "validated_preview")
            self.assertEqual(result["validation"]["footprint_moves"], ["R50"])
            self.assertEqual(result["validation"]["candidate_unconnected"], 0)
            self.assertEqual(source.read_bytes(), before)

    def test_a_move_carries_the_copper_its_diagnosed_route_crosses(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-doorway-") as folder:
            project = Path(folder)
            source, fence = SmallBoards(project).doorway()
            request = self.request()
            policy = {
                "route": request["shared"],
                "max_pins": 2,
                "placement": {"locked": False},
            }
            fixed = dict(request, search={**request["search"], "seconds": 5})
            self.assertNotEqual(
                preview_round(project, [fixed], workers=1)["status"],
                "validated_preview",
            )
            compiled = compile_reroutes(
                pcbnew.LoadBoard(str(source)), request, policy, ()
            )
            moves = [
                (candidate, description)
                for candidate, description in zip(
                    compiled["requests"], compiled["candidates"]
                )
                if candidate.get("movable")
            ]
            self.assertTrue(
                any(
                    "only moving a part can connect it" in note
                    for note in compiled["blockers"]
                )
            )
            self.assertTrue(moves)
            for candidate, description in moves:
                self.assertEqual(description["selection"], "path_obstruction_move")
                self.assertEqual(description["references"], ["P1"])
                self.assertEqual(
                    [item["uuid"] for item in candidate["rip_up"]["YNET"]], [fence]
                )
                self.assertEqual(
                    {item["name"] for item in candidate["nets"]},
                    {"SIGNAL", "YNET", "ZNET"},
                )
                self.assertGreaterEqual(candidate["search"]["order_trials"], 2)
            copper = [
                candidate
                for candidate in compiled["requests"]
                if not candidate.get("movable")
            ]
            self.assertTrue(copper)
            self.assertTrue(all(not candidate.get("movable") for candidate in copper))


if __name__ == "__main__":
    unittest.main()
