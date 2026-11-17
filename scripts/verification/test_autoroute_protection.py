"""Protection checks must catch edits even when a board remains valid KiCad."""

from __future__ import annotations

import importlib.util
import unittest


BOARD = '''(kicad_pcb
 (layers (0 "F.Cu" signal) (2 "In1.Cu" power) (4 "In2.Cu" power) (31 "B.Cu" signal))
 (setup (stackup (layer "F.Cu" (type "copper") (thickness 0.035))))
 (footprint "TOP" (layer "F.Cu") (uuid "top") (at 1 2)
   (property "Reference" "J1" (at 0 0) (layer "F.SilkS") (uuid "j1")))
 (footprint "BOTTOM" (layer "B.Cu") (uuid "bottom") (at 5 6)
   (property "Reference" "R1" (at 0 0) (layer "B.SilkS") (uuid "r1")))
 (gr_text "IMU" (at 8 9) (layer "F.SilkS") (uuid "label"))
 (gr_line (start 0 0) (end 10 0) (layer "Edge.Cuts") (uuid "edge"))
)'''


class ProtectedBoardTests(unittest.TestCase):
    def capture(self, board=BOARD, schematic=b"schematic"):
        self.assertIsNotNone(importlib.util.find_spec("pcb.net_routing.protection"))
        from pcb.net_routing.protection import ProtectedState

        return ProtectedState.capture(board.encode(), schematic)

    def test_top_placement_and_schematic_are_protected(self):
        baseline = self.capture()
        self.assertEqual(baseline.differences(BOARD.encode(), b"schematic"), ())
        moved = BOARD.replace('(uuid "top") (at 1 2)', '(uuid "top") (at 1 3)')
        self.assertIn("top footprints", baseline.differences(moved.encode(), b"schematic"))
        self.assertIn("schematic", baseline.differences(BOARD.encode(), b"changed"))

    def test_bottom_pose_is_allowed_but_front_silkscreen_is_protected(self):
        baseline = self.capture()
        moved = BOARD.replace('(uuid "bottom") (at 5 6)', '(uuid "bottom") (at 7 6)')
        self.assertEqual(baseline.differences(moved.encode(), b"schematic"), ())
        changed = moved.replace('(at 8 9) (layer "F.SilkS")', '(at 8 10) (layer "F.SilkS")')
        self.assertIn("front silkscreen", baseline.differences(changed.encode(), b"schematic"))

    def test_moving_bottom_footprint_with_front_silk_is_rejected(self):
        board = BOARD.replace('(layer "B.SilkS") (uuid "r1")', '(layer "F.SilkS") (uuid "r1")')
        moved = board.replace('(uuid "bottom") (at 5 6)', '(uuid "bottom") (at 7 6)')
        self.assertIn("front silkscreen", self.capture(board).differences(moved.encode(), b"schematic"))

    def test_outline_and_stackup_are_protected(self):
        baseline = self.capture()
        changed_edge = BOARD.replace('(end 10 0) (layer "Edge.Cuts")', '(end 11 0) (layer "Edge.Cuts")')
        self.assertIn("outline", baseline.differences(changed_edge.encode(), b"schematic"))
        changed_stack = BOARD.replace('(thickness 0.035)', '(thickness 0.07)')
        self.assertIn("stackup", baseline.differences(changed_stack.encode(), b"schematic"))

    def test_formatting_changes_do_not_count_as_placement_changes(self):
        rewritten = BOARD.replace('(at 1 2)', '(at 1.000 2.0)')
        self.assertEqual(self.capture().differences(rewritten.encode(), b"schematic"), ())


if __name__ == "__main__":
    unittest.main()
