"""Exercise source-preserving joint placement and routing search."""

import tempfile
import unittest
from pathlib import Path

import pcbnew

from pcb.footprint_pose import native_pose_change, verify_native_pose_change
from pcb.joint_optimizer import _staircase, optimize
from pcb.move_scope import MoveScope
import verification.test_route_optimizer as route_tests


class JointOptimizerTests(unittest.TestCase):
    def test_repeated_diagonal_steps_are_rejected(self):
        def segment(a, b, layer="B.Cu"):
            return {"from_mm": a, "to_mm": b, "layer": layer}

        self.assertTrue(
            _staircase(
                [
                    segment([0, 0], [1, 1]),
                    segment([1, 1], [1, 2]),
                    segment([1, 2], [2, 3]),
                ]
            )
        )
        self.assertFalse(
            _staircase(
                [
                    segment([0, 0], [1, 1]),
                    segment([1, 1], [1, 2]),
                    segment([1, 2], [2, 3], "F.Cu"),
                ]
            )
        )

    def test_search_moves_only_bottom_smd_and_never_writes_source(self):
        with tempfile.TemporaryDirectory(prefix="jessie-joint-test-") as directory:
            project = Path(directory)
            source = route_tests.RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            net = pcbnew.NETINFO_ITEM(board, "OTHER")
            board.Add(net)
            footprint = pcbnew.FOOTPRINT(board)
            footprint.SetReference("R50")
            footprint.SetLayer(pcbnew.B_Cu)
            footprint.Reference().SetLayer(pcbnew.B_SilkS)
            footprint.Value().SetLayer(pcbnew.B_Fab)
            footprint.SetPosition(pcbnew.VECTOR2I(5_000_000, 7_000_000))
            pad = pcbnew.PAD(footprint)
            pad.SetNumber("1")
            pad.SetSize(pcbnew.VECTOR2I(500_000, 500_000))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pcbnew.LSET.BackMask())
            pad.SetPosition(footprint.GetPosition())
            pad.SetNetCode(net.GetNetCode())
            footprint.Add(pad)
            board.Add(footprint)
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            route = route_tests.RouteOptimizerTests.config()
            route["layers"] = ["F.Cu"]
            route["electrical"] = route_tests.RouteOptimizerTests.electrical()
            config = {
                "movable": [{"reference": "R50", "positions_mm": [[4, 7], [5, 7]]}],
                "route": route,
                "max_candidates": 2,
            }
            result = optimize(project, config)
            self.assertEqual(result["status"], "validated_sampled_candidate", result)
            self.assertEqual(result["sampled"], 2)
            self.assertEqual(result["validation"]["endpoint_connectivity"], "connected")
            self.assertEqual(source.read_bytes(), original)

    def test_rotation_is_native_guarded_and_compared_with_translation(self):
        with tempfile.TemporaryDirectory(prefix="jessie-pose-test-") as directory:
            project = Path(directory)
            source = route_tests.RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            signal = board.FindNet("SIGNAL")
            other = pcbnew.NETINFO_ITEM(board, "OTHER")
            board.Add(other)
            footprint = pcbnew.FOOTPRINT(board)
            footprint.SetReference("R50")
            footprint.SetLayer(pcbnew.B_Cu)
            footprint.Reference().SetLayer(pcbnew.B_SilkS)
            footprint.Value().SetLayer(pcbnew.B_Fab)
            footprint.SetPosition(pcbnew.VECTOR2I(5_000_000, 5_000_000))
            for number, x, net in (("1", 4_500_000, signal), ("2", 5_500_000, other)):
                pad = pcbnew.PAD(footprint)
                pad.SetNumber(number)
                pad.SetSize(pcbnew.VECTOR2I(400_000, 400_000))
                pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
                pad.SetLayerSet(pcbnew.LSET.BackMask())
                pad.SetPosition(pcbnew.VECTOR2I(x, 5_000_000))
                pad.SetNetCode(net.GetNetCode())
                footprint.Add(pad)
            board.Add(footprint)
            anchor = pcbnew.FOOTPRINT(board)
            anchor.SetReference("R60")
            anchor.SetLayer(pcbnew.B_Cu)
            anchor.Reference().SetLayer(pcbnew.B_SilkS)
            anchor.SetPosition(pcbnew.VECTOR2I(8_000_000, 5_000_000))
            pad = pcbnew.PAD(anchor)
            pad.SetNumber("1")
            pad.SetSize(pcbnew.VECTOR2I(400_000, 400_000))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pcbnew.LSET.BackMask())
            pad.SetPosition(anchor.GetPosition())
            pad.SetNetCode(signal.GetNetCode())
            anchor.Add(pad)
            board.Add(anchor)
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            change = native_pose_change(source, "R50", 5, 5, 180)
            verify_native_pose_change(source, change)
            altered = dict(
                change, expression=change["expression"].replace("OTHER", "SIGNAL")
            )
            with self.assertRaisesRegex(ValueError, "exact native pose"):
                verify_native_pose_change(source, altered)
            self.assertEqual(native_pose_change(source, "J1", 2, 2, 90)["target"]["reference"], "J1")
            with self.assertRaisesRegex(ValueError, "top side"):
                native_pose_change(source, "J1", 2, 2, 90, MoveScope.from_config({"sides": ["bottom"]}))
            route = route_tests.RouteOptimizerTests.config()
            route.update(
                {
                    "start": {"reference": "R50", "pad": "1", "layer": "B.Cu"},
                    "end": {"reference": "R60", "pad": "1", "layer": "B.Cu"},
                    "layers": ["B.Cu"],
                    "electrical": route_tests.RouteOptimizerTests.electrical(),
                }
            )
            result = optimize(
                project,
                {
                    "movable": [
                        {
                            "reference": "R50",
                            "positions_mm": [[5, 5]],
                            "rotations_deg": [0, 180],
                        }
                    ],
                    "route": route,
                    "max_candidates": 2,
                },
            )
            self.assertEqual(result["status"], "validated_sampled_candidate", result)
            self.assertEqual(result["placement"]["R50"]["rotation_deg"], 180)
            self.assertEqual(result["validation"]["endpoint_connectivity"], "connected")
            self.assertEqual(source.read_bytes(), original)

    def test_route_only_search_needs_no_dummy_footprint_move(self):
        with tempfile.TemporaryDirectory(prefix="jessie-joint-test-") as directory:
            project = Path(directory)
            source = route_tests.RouteOptimizerTests.make_board(project)
            original = source.read_bytes()
            route = route_tests.RouteOptimizerTests.config()
            route["layers"] = ["F.Cu"]
            route["electrical"] = route_tests.RouteOptimizerTests.electrical()
            result = optimize(project, {"route": route})
            self.assertEqual(result["status"], "validated_sampled_candidate", result)
            self.assertEqual(result["placement"], {})
            self.assertEqual(result["validation"]["footprint_moves"], [])
            self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
