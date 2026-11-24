"""test_six_layer_routing.py: check routing on six copper layers.

The tests exercise native six-layer access, return references and fixed placement.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pcbnew

from pcb.net_routing import krt_adapter, reference
from pcb.net_routing.protection import ProtectedState
from pcb.route_native import read_native_obstacles
from pcb.route_optimizer import COPPER_LAYERS, mm
from verification.test_autoroute_protection import BOARD


class SixLayerRoutingTests(unittest.TestCase):
    """Tests for routing on six copper layers."""

    def test_single_route_defaults_to_every_enabled_layer(self):
        from pcb import route_cli
        from pcb.route_optimizer import Route
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = RouteOptimizerTests.make_board(root)
            board = pcbnew.LoadBoard(str(source))
            board.SetCopperLayerCount(6)
            pcbnew.SaveBoard(str(source), board)
            config = RouteOptimizerTests.config()
            config.pop("layers")
            config["board_snapshot"] = "pcb"
            with patch.object(route_cli, "GridRouter") as router:
                router.return_value.route.return_value = Route((), (), (), 0, 0, 0, 0)
                route_cli.plan(config, root)
            self.assertEqual(
                router.call_args.args[3],
                ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"),
            )

    def test_all_six_layers_reach_the_native_obstacle_model(self):
        board = pcbnew.BOARD()
        board.SetCopperLayerCount(6)
        board.Add(pcbnew.NETINFO_ITEM(board, "SIGNAL"))
        track = pcbnew.PCB_TRACK(board)
        track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
        track.SetLayer(pcbnew.In3_Cu)
        track.SetWidth(mm(0.2))
        track.SetStart(pcbnew.VECTOR2I(0, 0))
        track.SetEnd(pcbnew.VECTOR2I(mm(2), 0))
        board.Add(track)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "board.kicad_pcb"
            pcbnew.SaveBoard(str(source), board)
            model = read_native_obstacles(source, "SIGNAL")
        self.assertEqual(
            model.enabled_layers,
            ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"),
        )

    def test_bottom_reference_is_in4_and_middle_references_are_adjacent(self):
        board = pcbnew.BOARD()
        board.SetCopperLayerCount(6)
        board.Add(pcbnew.NETINFO_ITEM(board, "HS"))
        for layer, y in ((pcbnew.B_Cu, 0), (pcbnew.In2_Cu, 4)):
            track = pcbnew.PCB_TRACK(board)
            track.SetLayer(layer)
            track.SetNetCode(board.FindNet("HS").GetNetCode())
            track.SetWidth(mm(0.2))
            track.SetStart(pcbnew.VECTOR2I(0, mm(y)))
            track.SetEnd(pcbnew.VECTOR2I(mm(2), mm(y)))
            board.Add(track)
        guards = reference.reference_corridors(board, {"HS"})
        self.assertEqual(guards.unsupported_items, ())
        self.assertEqual(
            {guard.layers for guard in guards.obstacles},
            {("In4.Cu",), ("In1.Cu",), ("In3.Cu",)},
        )
        sampled = reference.sample_reference(board, {"HS"})
        self.assertEqual(sampled.unsupported_items, ())
        self.assertEqual(len(sampled.tracks), 2)

    def test_known_layers_and_external_adapter_accept_the_six_layer_stack(self):
        layers = ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu")
        self.assertTrue(set(layers) <= set(COPPER_LAYERS))
        command = krt_adapter.KrtRouterProcess.build_command(
            Path("python"),
            Path("router"),
            Path("in"),
            Path("out"),
            ("A",),
            layers=layers,
        )
        start = command.index("--layers") + 1
        self.assertEqual(command[start : start + 6], list(layers))
        start = command.index("--layer-costs") + 1
        self.assertEqual(command[start : start + 6], ["1"] * 6)

    def test_strict_placement_guard_rejects_bottom_moves(self):
        state = ProtectedState.capture(BOARD.encode(), b"sch")
        moved = BOARD.replace('(uuid "bottom") (at 5 6)', '(uuid "bottom") (at 7 6)')
        self.assertIn("fixed footprints", state.differences(moved.encode(), b"sch"))
        self.assertEqual(state.differences(BOARD.encode(), b"sch"), ())


if __name__ == "__main__":
    unittest.main()
