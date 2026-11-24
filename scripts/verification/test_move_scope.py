"""test_move_scope.py: check which footprints routing may move.

The placement policy decides which footprints routing may move.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import unittest

from core.kicad_sexpr import parse
from pcb.move_scope import MoveScope


class BoardText:
    """Builds the footprint and board S-expression texts the tests parse."""

    @staticmethod
    def footprint(reference, layer="F.Cu", pads=("smd",), extra=""):
        """Return the S-expression text of a footprint.

        Inputs:
            reference: reference designator.
            layer: layer name, F.Cu or B.Cu.
            pads: pad kinds, one pad per entry.
            extra: text inserted before the layer form, such as a locked flag.
        """
        pad_forms = " ".join(
            f'(pad "{index}" {kind} circle)' for index, kind in enumerate(pads, 1)
        )
        return (
            f'(footprint "Lib:Part" {extra}(layer "{layer}") (uuid "u-{reference}") (at 1 2) '
            f'(property "Reference" "{reference}" (layer "{"F" if layer == "F.Cu" else "B"}.SilkS")) {pad_forms})'
        )

    @staticmethod
    def board(*footprints):
        """Return the S-expression text of a board holding the given footprints.

        Inputs:
            footprints: footprint texts from footprint.
        """
        return "(kicad_pcb (version 20241229) " + " ".join(footprints) + ")"


class MoveScopeTests(unittest.TestCase):
    """Tests for which footprints MoveScope lets move and why it refuses the rest."""

    def refusal(self, scope, text):
        """Return the refusal reason for the first footprint of a board text.

        Inputs:
            scope: the MoveScope under test.
            text: board text.
        Outputs:
            The refusal text, or None when the footprint may move.
        """
        return scope.refusal(text, parse(text).children("footprint")[0])

    def test_by_default_any_part_may_move_even_if_locked(self):
        text = BoardText.board(
            BoardText.footprint("U1"),
            BoardText.footprint("R1", "B.Cu"),
            BoardText.footprint("J1", pads=("thru_hole", "np_thru_hole")),
            BoardText.footprint("J2", extra="(locked yes) "),
            BoardText.footprint("J3", extra="locked "),
        )
        self.assertEqual(MoveScope().movable(text), {"U1", "R1", "J1", "J2", "J3"})
        self.assertEqual(MoveScope.from_config(None), MoveScope())
        self.assertEqual(MoveScope.from_config({}), MoveScope())

    def test_locked_parts_can_be_kept_in_place(self):
        scope = MoveScope.from_config({"locked": False})
        text = BoardText.board(
            BoardText.footprint("U1"),
            BoardText.footprint("J2", extra="(locked yes) "),
            BoardText.footprint("J3", extra="locked "),
            BoardText.footprint("J4", extra="(locked no) "),
        )
        self.assertEqual(scope.movable(text), {"U1", "J4"})
        self.assertIn(
            "locked",
            self.refusal(
                scope, BoardText.board(BoardText.footprint("J2", extra="(locked yes) "))
            ),
        )

    def test_a_fixed_side_keeps_its_parts_and_its_silkscreen(self):
        scope = MoveScope.from_config({"sides": ["bottom"]})
        self.assertIn(
            "top side", self.refusal(scope, BoardText.board(BoardText.footprint("U1")))
        )
        self.assertIsNone(
            self.refusal(scope, BoardText.board(BoardText.footprint("R1", "B.Cu")))
        )
        printed = BoardText.footprint("R2", "B.Cu").replace(
            "(at 1 2)", '(at 1 2) (fp_text user "x" (layer "F.SilkS"))'
        )
        self.assertIn("top silkscreen", self.refusal(scope, BoardText.board(printed)))

    def test_through_hole_parts_can_be_kept_in_place(self):
        scope = MoveScope.from_config({"through_hole": False})
        self.assertIn(
            "through-hole",
            self.refusal(
                scope,
                BoardText.board(BoardText.footprint("J1", pads=("smd", "thru_hole"))),
            ),
        )
        self.assertIn(
            "through-hole",
            self.refusal(
                scope,
                BoardText.board(BoardText.footprint("H1", pads=("np_thru_hole",))),
            ),
        )
        self.assertIsNone(
            self.refusal(scope, BoardText.board(BoardText.footprint("R1")))
        )

    def test_check_names_one_movable_part_per_reference(self):
        text = BoardText.board(
            BoardText.footprint("R1"),
            BoardText.footprint("R2"),
            BoardText.footprint("R2"),
            BoardText.footprint("J1", extra="(locked yes) "),
        )
        scope = MoveScope.from_config({"locked": False})
        scope.check(text, ["R1"])
        for reference, message in (
            ("R2", "ambiguous"),
            ("R9", "missing"),
            ("J1", "locked"),
        ):
            with (
                self.subTest(reference=reference),
                self.assertRaisesRegex(ValueError, message),
            ):
                scope.check(text, [reference])

    def test_policy_values_are_checked(self):
        for raw in (
            {"sides": ["left"]},
            {"sides": "top"},
            {"sides": ["top", "top"]},
            {"through_hole": "no"},
            {"locked": "no"},
            {"pinned": True},
            ["top"],
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                MoveScope.from_config(raw)


if __name__ == "__main__":
    unittest.main()
