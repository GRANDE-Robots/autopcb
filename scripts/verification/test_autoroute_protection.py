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
    def capture(self, board=BOARD, schematic=b"schematic", movable=None):
        self.assertIsNotNone(importlib.util.find_spec("pcb.net_routing.protection"))
        from pcb.net_routing.protection import ProtectedState

        return ProtectedState.capture(board.encode(), schematic, movable=movable)

    def test_without_a_scope_all_placement_and_the_schematic_are_fixed(self):
        baseline = self.capture()
        self.assertEqual(baseline.differences(BOARD.encode(), b"schematic"), ())
        for uid, at, moved_at in (("top", "(at 1 2)", "(at 1 3)"), ("bottom", "(at 5 6)", "(at 7 6)")):
            moved = BOARD.replace(f'(uuid "{uid}") {at}', f'(uuid "{uid}") {moved_at}')
            self.assertIn("fixed footprints", baseline.differences(moved.encode(), b"schematic"))
        self.assertIn("schematic", baseline.differences(BOARD.encode(), b"changed"))

    def test_movable_parts_may_move_but_locked_parts_and_board_silkscreen_stay(self):
        from pcb.move_scope import MoveScope

        board = BOARD.replace('(footprint "BOTTOM" (layer "B.Cu")', '(footprint "BOTTOM" (locked yes) (layer "B.Cu")')
        baseline = self.capture(board, movable=MoveScope())
        moved = board.replace('(uuid "top") (at 1 2)', '(uuid "top") (at 3 2)')
        self.assertEqual(baseline.differences(moved.encode(), b"schematic"), ())
        locked = board.replace('(uuid "bottom") (at 5 6)', '(uuid "bottom") (at 7 6)')
        self.assertIn("fixed footprints", baseline.differences(locked.encode(), b"schematic"))
        label = moved.replace('(at 8 9) (layer "F.SilkS")', '(at 8 10) (layer "F.SilkS")')
        self.assertIn("front silkscreen", baseline.differences(label.encode(), b"schematic"))

    def test_a_bottom_only_scope_fixes_top_parts_and_parts_printing_on_top(self):
        from pcb.move_scope import MoveScope

        scope = MoveScope.from_config({"sides": ["bottom"]})
        baseline = self.capture(movable=scope)
        bottom = BOARD.replace('(uuid "bottom") (at 5 6)', '(uuid "bottom") (at 7 6)')
        self.assertEqual(baseline.differences(bottom.encode(), b"schematic"), ())
        top = BOARD.replace('(uuid "top") (at 1 2)', '(uuid "top") (at 1 3)')
        self.assertIn("fixed footprints", baseline.differences(top.encode(), b"schematic"))
        printed = BOARD.replace('(layer "B.SilkS") (uuid "r1")', '(layer "F.SilkS") (uuid "r1")')
        moved = printed.replace('(uuid "bottom") (at 5 6)', '(uuid "bottom") (at 7 6)')
        self.assertIn("fixed footprints", self.capture(printed, movable=scope).differences(moved.encode(), b"schematic"))

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
