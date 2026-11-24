"""Which footprints the placement policy lets routing move."""

import unittest

from core.kicad_sexpr import parse
from pcb.move_scope import MoveScope


def footprint(reference, layer="F.Cu", pads=("smd",), extra=""):
    pad_forms = " ".join(
        f'(pad "{index}" {kind} circle)' for index, kind in enumerate(pads, 1)
    )
    return (
        f'(footprint "Lib:Part" {extra}(layer "{layer}") (uuid "u-{reference}") (at 1 2) '
        f'(property "Reference" "{reference}" (layer "{"F" if layer == "F.Cu" else "B"}.SilkS")) {pad_forms})'
    )


def board(*footprints):
    return "(kicad_pcb (version 20241229) " + " ".join(footprints) + ")"


class MoveScopeTests(unittest.TestCase):
    def refusal(self, scope, text):
        return scope.refusal(text, parse(text).children("footprint")[0])

    def test_by_default_any_part_may_move_even_if_locked(self):
        text = board(
            footprint("U1"),
            footprint("R1", "B.Cu"),
            footprint("J1", pads=("thru_hole", "np_thru_hole")),
            footprint("J2", extra="(locked yes) "),
            footprint("J3", extra="locked "),
        )
        self.assertEqual(MoveScope().movable(text), {"U1", "R1", "J1", "J2", "J3"})
        self.assertEqual(MoveScope.from_config(None), MoveScope())
        self.assertEqual(MoveScope.from_config({}), MoveScope())

    def test_locked_parts_can_be_kept_in_place(self):
        scope = MoveScope.from_config({"locked": False})
        text = board(
            footprint("U1"),
            footprint("J2", extra="(locked yes) "),
            footprint("J3", extra="locked "),
            footprint("J4", extra="(locked no) "),
        )
        self.assertEqual(scope.movable(text), {"U1", "J4"})
        self.assertIn(
            "locked", self.refusal(scope, board(footprint("J2", extra="(locked yes) ")))
        )

    def test_a_fixed_side_keeps_its_parts_and_its_silkscreen(self):
        scope = MoveScope.from_config({"sides": ["bottom"]})
        self.assertIn("top side", self.refusal(scope, board(footprint("U1"))))
        self.assertIsNone(self.refusal(scope, board(footprint("R1", "B.Cu"))))
        printed = footprint("R2", "B.Cu").replace(
            "(at 1 2)", '(at 1 2) (fp_text user "x" (layer "F.SilkS"))'
        )
        self.assertIn("top silkscreen", self.refusal(scope, board(printed)))

    def test_through_hole_parts_can_be_kept_in_place(self):
        scope = MoveScope.from_config({"through_hole": False})
        self.assertIn(
            "through-hole",
            self.refusal(scope, board(footprint("J1", pads=("smd", "thru_hole")))),
        )
        self.assertIn(
            "through-hole",
            self.refusal(scope, board(footprint("H1", pads=("np_thru_hole",)))),
        )
        self.assertIsNone(self.refusal(scope, board(footprint("R1"))))

    def test_check_names_one_movable_part_per_reference(self):
        text = board(
            footprint("R1"),
            footprint("R2"),
            footprint("R2"),
            footprint("J1", extra="(locked yes) "),
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
