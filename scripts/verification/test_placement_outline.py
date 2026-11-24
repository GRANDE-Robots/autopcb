"""test_placement_outline.py: tests of compact placement and the outline fit.

Compact placement respects catalog limits, fixed poses and the complete native envelope.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import unittest

from autopcb import design
from autopcb.placement import Compactor, Envelope, OutlineFitter


class CompactorTests(unittest.TestCase):
    """Tests that parts slide toward a point until something stops them."""

    def test_parts_slide_toward_the_point_until_they_meet(self):
        parts = {
            "A": Envelope((2, 2), (4, 4)),
            "B": Envelope((10, 2), (12, 4)),
            "J": Envelope((20, 20), (24, 24)),
        }
        packed = Compactor(parts, {"A", "B"}, Envelope((0, 0), (30, 30)), 1, 1).toward(
            (3, 3), ["A", "B"]
        )
        self.assertEqual(packed["A"], parts["A"])
        self.assertEqual(packed["B"], Envelope((5, 2), (7, 4)))
        self.assertEqual(packed["J"], parts["J"])

    def test_a_fixed_part_and_the_edge_margin_stop_a_slide(self):
        parts = {"A": Envelope((10, 10), (12, 12)), "J": Envelope((4, 9), (6, 13))}
        outline = Envelope((0, 0), (20, 20))
        packed = Compactor(parts, {"A"}, outline, 2, 1).toward((0, 11), ["A"])
        self.assertEqual(packed["A"], Envelope((7, 10), (9, 12)))
        packed = Compactor(parts, {"A"}, outline, 2, 1).toward((11, 30), ["A"])
        self.assertEqual(packed["A"], Envelope((10, 16), (12, 18)))


class OutlineTests(unittest.TestCase):
    """Tests of the outline fit around fixed and movable part envelopes."""

    def test_floor_prevents_pointless_component_motion(self):
        parts = {"J1": Envelope((1, 1), (4, 4)), "C1": Envelope((7, 3), (9, 7))}
        self.assertEqual(
            OutlineFitter(parts, [["C1"]], (12, 10), (30, 20), 1).fit(), ((12, 10), {})
        )

    def test_group_slides_together_and_keeps_clear_of_the_fixed_connector(self):
        parts = {
            "J": Envelope((1, 1), (4, 4)),
            "U": Envelope((12, 3), (15, 7)),
            "C": Envelope((18, 3), (20, 7)),
        }
        size, moved = OutlineFitter(parts, [["U", "C"]], (5, 10), (24, 20), 1).fit()
        self.assertEqual(size, (14, 10))
        self.assertEqual(moved, {"U": (-7, 0), "C": (-7, 0)})

    def test_fixed_envelopes_set_size_and_block_out_of_range_placement(self):
        parts = {"J": Envelope((1, 2), (16, 8))}
        self.assertEqual(
            OutlineFitter(parts, [], (8, 8), (20, 20), 1).fit(), ((17, 9), {})
        )
        with self.assertRaisesRegex(ValueError, "maximum outline"):
            OutlineFitter(parts, [], (8, 8), (16, 20), 1).fit()
        with self.assertRaisesRegex(ValueError, "left or top"):
            OutlineFitter(
                {"J": Envelope((0, 2), (16, 8))}, [], (8, 8), (20, 20), 1
            ).fit()

    def test_only_an_unsized_eligible_proposal_requests_compaction(self):
        description = "Two-layer temperature sensor at 3.3 V"
        proposal = design.propose(description)
        self.assertEqual(proposal["compact_outline"]["minimum_mm"], [28, 20])
        self.assertNotIn("J1", sum(proposal["compact_outline"]["movable_groups"], []))
        for dimensions in (
            "35 x 25 mm",
            "35 mm × 25 mm",
            "3.5 cm by 25 mm",
            "35 mm by 25",
        ):
            with self.subTest(dimensions=dimensions):
                fixed = design.propose(f"{description} on a {dimensions} board")
                self.assertEqual(fixed["size_mm"], [35, 25])
                self.assertNotIn("compact_outline", fixed)
        self.assertNotIn(
            "compact_outline", design.propose(description + " with the catalog outline")
        )
        self.assertEqual(
            design.propose(description + " at address 0x18")["status"], "proposed"
        )
        self.assertEqual(
            design.propose(description + " on a 35 x 25 board")["status"], "blocked"
        )
        limited = design.propose(description + " up to 32 x 22 mm")
        self.assertEqual(limited["size_mm"], [32, 22])
        self.assertEqual(limited["compact_outline"]["minimum_mm"], [28, 20])
        self.assertEqual(
            design.propose(description + " within 24 x 18 mm")["status"], "blocked"
        )


if __name__ == "__main__":
    unittest.main()
