"""test_autoroute_symmetry.py: tests of matching-channel comparison and ranking.

Compare matching channels and rank only electrically eligible candidates.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import importlib.util
import unittest

from pcb.route_optimizer import Point, Route, Segment, Via


class SymmetryRoutes:
    """Builds routes on B.Cu from points given in mm."""

    @staticmethod
    def point(x, y):
        """Return the route point at (x, y) given in mm."""
        return Point.from_mm([x, y])

    @classmethod
    def route(cls, points, via=()):
        """Build a route through consecutive points.

        Inputs:
            points: (x, y) pairs in mm; each consecutive pair becomes a B.Cu segment.
            via: (x, y) pairs in mm of vias between F.Cu and B.Cu.
        Outputs:
            The route with those segments and vias and no other fields set.
        """
        segments = tuple(
            Segment(cls.point(*a), cls.point(*b), "B.Cu")
            for a, b in zip(points, points[1:])
        )
        vias = tuple(Via(cls.point(*item), "F.Cu", "B.Cu") for item in via)
        return Route(segments, vias, (), 0, 0, 0, 0)


class SymmetryTests(unittest.TestCase):
    """Tests of the channel symmetry comparison and the layout score ranking."""

    def api(self):
        """Return the ``pcb.net_routing.symmetry`` module, which must exist."""
        self.assertIsNotNone(importlib.util.find_spec("pcb.net_routing.symmetry"))
        from pcb.net_routing import symmetry

        return symmetry

    def test_translated_mirrored_channels_match(self):
        api = self.api()
        left = SymmetryRoutes.route([(1, 2), (3, 2), (4, 3)], [(4, 3)])
        right = SymmetryRoutes.route([(9, 2), (7, 2), (6, 3)], [(6, 3)])
        result = api.compare_routes(
            left,
            api.Frame(SymmetryRoutes.point(0, 0)),
            right,
            api.Frame(SymmetryRoutes.point(10, 0), mirror=True),
        )
        self.assertEqual(result.score, 0)

    def test_extra_via_or_misalignment_increases_error(self):
        api = self.api()
        left = SymmetryRoutes.route([(1, 2), (3, 2), (4, 3)], [(4, 3)])
        right = SymmetryRoutes.route([(9, 2), (7, 2), (6, 3)], [(6, 3), (7, 2)])
        result = api.compare_routes(
            left,
            api.Frame(SymmetryRoutes.point(0, 0)),
            right,
            api.Frame(SymmetryRoutes.point(10, 0), mirror=True),
        )
        self.assertGreater(result.score, 0)
        self.assertEqual(result.via_count_difference, 1)

    def test_subtolerance_via_deviation_has_no_penalty(self):
        api = self.api()
        left = SymmetryRoutes.route([(1, 2), (4, 2)], [(3, 2)])
        right = SymmetryRoutes.route([(1, 2), (4, 2)], [(3.04, 2)])
        result = api.compare_routes(
            left,
            api.Frame(SymmetryRoutes.point(0, 0)),
            right,
            api.Frame(SymmetryRoutes.point(0, 0)),
        )
        self.assertEqual(result.score, 0)

    def test_segment_splitting_does_not_change_geometry_score(self):
        api = self.api()
        left = SymmetryRoutes.route([(1, 2), (5, 2)])
        right = SymmetryRoutes.route([(1, 2), (3, 2), (5, 2)])
        result = api.compare_routes(
            left,
            api.Frame(SymmetryRoutes.point(0, 0)),
            right,
            api.Frame(SymmetryRoutes.point(0, 0)),
        )
        self.assertEqual(result.score, 0)

    def test_rank_respects_feasibility_then_opens_then_symmetry(self):
        api = self.api()
        best_symmetry = api.LayoutScore(0, 0.1, 100, 3, 2, 0, 0, True)
        shorter = api.LayoutScore(0, 0.2, 10, 0, 0, 0, 0, True)
        more_opens = api.LayoutScore(1, 0, 1, 0, 0, 0, 0, True)
        self.assertLess(best_symmetry.key(), shorter.key())
        self.assertLess(shorter.key(), more_opens.key())
        with self.assertRaisesRegex(ValueError, "ineligible"):
            api.LayoutScore(0, 0, 1, 0, 0, 0, 0, False).key()

    def test_missing_copper_between_sampling_points_is_not_symmetric(self):
        api = self.api()
        whole = SymmetryRoutes.route([(0, 0), (4, 0)])
        fragments = Route(
            tuple(
                Segment(SymmetryRoutes.point(a, 0), SymmetryRoutes.point(b, 0), "B.Cu")
                for a, b in ((0, 0.1), (1.95, 2.05), (3.9, 4))
            ),
            (),
            (),
            0,
            0,
            0,
            0,
        )
        result = api.compare_routes(
            whole,
            api.Frame(SymmetryRoutes.point(0, 0)),
            fragments,
            api.Frame(SymmetryRoutes.point(0, 0)),
        )
        self.assertGreater(result.score, 0)

    def test_routing_cost_weights_are_configurable(self):
        api = self.api()
        score = api.LayoutScore(0, 0, 10, 2, 3, 4, 5, True)
        self.assertTrue(hasattr(api, "CostWeights"))
        weights = api.CostWeights(length=2, via=20, bend=3, diagonal=1, front=0.5)
        self.assertEqual(score.key(weights)[2], 75.5)


if __name__ == "__main__":
    unittest.main()
