"""test_topology_bound.py: tests of the optimistic topology bound on small grids.

Check the optimistic connectivity and via-count bound on small grids.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import unittest
import inspect
import math
import random
from array import array
from unittest import mock

from pcb.route_optimizer import GridRouter, NoGridRoute, Obstacle, Point, Weights, mm
from pcb.topology_bound import UNREACHABLE, minimum_vias, relaxed_costs
from pcb.net_routing.model import Budget, BudgetExhausted, Limits
from core.compat import strict_zip


class GridFixtures:
    """Builds the small grid routers and lookups that the topology bound tests use."""

    @staticmethod
    def point(x, y):
        """Return the router point at (x, y) given in mm."""
        return Point.from_mm((x, y))

    @classmethod
    def model(cls, obstacles=()):
        """Build a two-layer grid router over a 10 mm by 10 mm area.

        Inputs:
            obstacles: Obstacles the router must avoid.
        Outputs:
            A router for net class SIGNAL on F.Cu and B.Cu with a 0.25 mm grid, 0.2 mm
            track width and clearance, 0.6 mm via size and 0.3 mm via drill.
        """
        return GridRouter(
            cls.point(0, 0),
            cls.point(10, 10),
            mm(0.25),
            ("F.Cu", "B.Cu"),
            "SIGNAL",
            mm(0.2),
            mm(0.2),
            mm(0.6),
            mm(0.3),
            obstacles=obstacles,
        )

    @staticmethod
    def lookup(router, distances, point, layer):
        """Read the value that a per-cell array holds for a point on a layer.

        Inputs:
            router: Grid router that defines the cell layout.
            distances: Flat array with one entry per layer and cell.
            point: Router point to look up.
            layer: Copper layer name.
        """
        x, y = router._index(point)
        return distances[
            router.layers.index(layer) * router.nx * router.ny + y * router.nx + x
        ]


class TopologyBoundTests(unittest.TestCase):
    """Tests that the topology bound stays optimistic and agrees with the exact router."""

    def test_grid_bounds_are_exact_without_constructing_points(self):
        router = GridRouter(
            GridFixtures.point(-1.23, 4.56),
            GridFixtures.point(2.07, 7.89),
            mm(0.37),
            ("F.Cu", "B.Cu"),
            "SIGNAL",
            mm(0.2),
            mm(0.1),
            mm(0.6),
            mm(0.3),
        )
        for margin in (0, 1, mm(0.15), mm(0.4), mm(2)):
            expected = {
                (x, y): router._inside_point(router._point(x, y), margin)
                for x in range(-2, router.nx + 3)
                for y in range(-2, router.ny + 3)
            }
            with mock.patch.object(
                router, "_point", side_effect=AssertionError("point allocation")
            ):
                actual = {
                    (x, y): router._inside(x, y, margin)
                    for x in range(-2, router.nx + 3)
                    for y in range(-2, router.ny + 3)
                }
            self.assertEqual(actual, expected)

    def test_topology_cell_bounds_match_exact_router_bounds(self):
        from pcb.topology_bound import grid_bounds

        router = GridRouter(
            GridFixtures.point(-1.23, 4.56),
            GridFixtures.point(2.07, 7.89),
            mm(0.37),
            ("F.Cu", "B.Cu"),
            "SIGNAL",
            mm(0.2),
            mm(0.1),
            mm(0.6),
            mm(0.3),
        )
        for margin in (0, 1, mm(0.15), mm(0.4), mm(2)):
            xmin, ymin, xmax, ymax = grid_bounds(router, margin)
            for x in range(-2, router.nx + 3):
                for y in range(-2, router.ny + 3):
                    self.assertEqual(
                        xmin <= x <= xmax and ymin <= y <= ymax,
                        router._inside(x, y, margin),
                    )

    def test_partial_guidance_matches_unguided_search_on_seeded_obstacle_fields(self):
        rng = random.Random(20261029)
        compared = 0
        for _ in range(20):
            obstacles = tuple(
                Obstacle(
                    GridFixtures.point(x - 0.04, y - 0.04),
                    GridFixtures.point(x + 0.04, y + 0.04),
                    ("F.Cu", "B.Cu"),
                    "OTHER",
                )
                for x in range(1, 8)
                for y in range(1, 8)
                if (x, y) not in {(1, 1), (7, 7)} and rng.random() < 0.12
            )
            settings = dict(
                lower=GridFixtures.point(0, 0),
                upper=GridFixtures.point(8, 8),
                step=mm(1),
                layers=("F.Cu", "B.Cu"),
                net="SIGNAL",
                width=mm(0.2),
                clearance=mm(0.1),
                via_size=mm(0.6),
                via_drill=mm(0.3),
                obstacles=obstacles,
                max_expansions=30_000,
                weights=Weights(length=1, bend=1, via=10, diagonal=1),
                forbid_staircase=True,
                gentle_turns=True,
            )
            baseline = GridRouter(**settings)
            guided = GridRouter(**settings, topology_heuristic=True)
            try:
                reference = baseline.route(
                    GridFixtures.point(1, 1), "B.Cu", GridFixtures.point(7, 7), "B.Cu"
                )
            except NoGridRoute:
                continue
            result = guided.route(
                GridFixtures.point(1, 1), "B.Cu", GridFixtures.point(7, 7), "B.Cu"
            )
            self.assertAlmostEqual(result.cost, reference.cost, places=7)
            self.assertTrue(
                all(guided.edge_clear(s.start, s.end, s.layer) for s in result.segments)
            )
            compared += 1
        self.assertGreaterEqual(compared, 5)

    def test_partial_bound_stops_after_reaching_start_and_stays_optimistic(self):
        self.assertIn("stop_at", inspect.signature(relaxed_costs).parameters)
        router = GridFixtures.model()
        gx, gy = router._index(GridFixtures.point(8, 5))
        sx, sy = router._index(GridFixtures.point(8.25, 5))
        layer = router.layers.index("B.Cu")
        goal, start = (gx, gy, layer), (sx, sy, layer)
        full = relaxed_costs(router, goal)
        router.check_budget = mock.Mock()
        partial = relaxed_costs(router, goal, stop_at=start, max_expansions=5000)
        self.assertAlmostEqual(
            GridFixtures.lookup(router, partial, GridFixtures.point(8.25, 5), "B.Cu"),
            GridFixtures.lookup(router, full, GridFixtures.point(8.25, 5), "B.Cu"),
        )
        self.assertLess(router.check_budget.call_count, 5)
        self.assertTrue(
            all(
                pcost <= fcost + 1e-8
                for pcost, fcost in strict_zip(partial, full)
                if math.isfinite(fcost)
            )
        )

    def test_partial_bound_never_calls_unexplored_cells_unreachable(self):
        self.assertIn("stop_at", inspect.signature(relaxed_costs).parameters)
        wall = Obstacle(
            GridFixtures.point(4.5, 0),
            GridFixtures.point(5.5, 10),
            ("B.Cu", "F.Cu"),
            "OTHER",
        )
        router = GridFixtures.model((wall,))
        gx, gy = router._index(GridFixtures.point(8, 5))
        sx, sy = router._index(GridFixtures.point(2, 5))
        layer = router.layers.index("B.Cu")
        costs = relaxed_costs(
            router, (gx, gy, layer), stop_at=(sx, sy, layer), max_expansions=2
        )
        full = relaxed_costs(router, (gx, gy, layer))
        bound = GridFixtures.lookup(router, costs, GridFixtures.point(2, 5), "B.Cu")
        self.assertTrue(math.isfinite(bound))
        self.assertLessEqual(
            bound, GridFixtures.lookup(router, full, GridFixtures.point(2, 5), "B.Cu")
        )

    def test_relaxed_topology_does_not_reject_a_clear_diagonal(self):
        obstacles = tuple(
            Obstacle(
                GridFixtures.point(x - 0.05, y - 0.05),
                GridFixtures.point(x + 0.05, y + 0.05),
                ("F.Cu", "B.Cu"),
                "OTHER",
            )
            for x, y in ((2, 1), (1, 2))
        )
        router = GridRouter(
            GridFixtures.point(0, 0),
            GridFixtures.point(3, 3),
            mm(1),
            ("F.Cu", "B.Cu"),
            "SIGNAL",
            mm(0.2),
            mm(0.1),
            mm(0.6),
            mm(0.3),
            obstacles=obstacles,
            topology_heuristic=True,
        )
        try:
            route = router.route(
                GridFixtures.point(1, 1), "B.Cu", GridFixtures.point(2, 2), "B.Cu"
            )
        except NoGridRoute as exc:
            self.fail(f"The clearance-checked diagonal is legal: {exc}")
        self.assertTrue(
            all(router.edge_clear(s.start, s.end, s.layer) for s in route.segments)
        )
        self.assertEqual(route.segments[0].start, GridFixtures.point(1, 1))
        self.assertEqual(route.segments[-1].end, GridFixtures.point(2, 2))
        x, y = router._index(GridFixtures.point(2, 2))
        costs = relaxed_costs(router, (x, y, router.layers.index("B.Cu")))
        self.assertLessEqual(
            GridFixtures.lookup(router, costs, GridFixtures.point(1, 1), "B.Cu"),
            route.cost,
        )
        vias = minimum_vias(router, (x, y, router.layers.index("B.Cu")))
        self.assertEqual(
            GridFixtures.lookup(router, vias, GridFixtures.point(1, 1), "B.Cu"), 0
        )

    def test_relaxed_grid_analysis_stops_when_the_search_budget_expires(self):
        router = GridFixtures.model()
        budget = Budget(Limits(seconds=1), started=0)
        router.check_budget = budget.check
        x, y = router._index(GridFixtures.point(8, 5))
        with mock.patch("pcb.net_routing.model.time.monotonic", return_value=2):
            with self.assertRaises(BudgetExhausted):
                relaxed_costs(router, (x, y, router.layers.index("B.Cu")))
        self.assertIn("seconds", budget.limits_hit)

    def test_two_vias_required_to_go_around_a_single_layer_wall(self):
        wall = Obstacle(
            GridFixtures.point(4.5, 0), GridFixtures.point(5.5, 10), ("B.Cu",), "OTHER"
        )
        router = GridFixtures.model((wall,))
        x, y = router._index(GridFixtures.point(8, 5))
        distances = minimum_vias(router, (x, y, router.layers.index("B.Cu")))
        self.assertEqual(
            GridFixtures.lookup(router, distances, GridFixtures.point(2, 5), "B.Cu"), 2
        )
        self.assertEqual(
            GridFixtures.lookup(router, distances, GridFixtures.point(2, 5), "F.Cu"), 1
        )

    def test_wall_on_both_layers_is_disconnected_in_this_model(self):
        wall = Obstacle(
            GridFixtures.point(4.5, 0),
            GridFixtures.point(5.5, 10),
            ("B.Cu", "F.Cu"),
            "OTHER",
        )
        router = GridFixtures.model((wall,))
        x, y = router._index(GridFixtures.point(8, 5))
        distances = minimum_vias(router, (x, y, router.layers.index("B.Cu")))
        self.assertEqual(
            GridFixtures.lookup(router, distances, GridFixtures.point(2, 5), "B.Cu"),
            UNREACHABLE,
        )

    def test_existing_same_net_via_is_free(self):
        via = Obstacle(
            GridFixtures.point(2.2, 2.2),
            GridFixtures.point(2.8, 2.8),
            ("F.Cu", "B.Cu"),
            "SIGNAL",
            "via",
        )
        router = GridFixtures.model((via,))
        x, y = router._index(GridFixtures.point(8, 5))
        distances = minimum_vias(router, (x, y, router.layers.index("B.Cu")))
        self.assertEqual(
            GridFixtures.lookup(router, distances, GridFixtures.point(2, 2), "F.Cu"), 0
        )

    def test_relaxed_costs_include_distance_and_via_cost(self):
        router = GridFixtures.model()
        router.weights = Weights(length=1, bend=1, via=10, diagonal=1)
        x, y = router._index(GridFixtures.point(8, 5))
        costs = relaxed_costs(router, (x, y, router.layers.index("B.Cu")))
        self.assertAlmostEqual(
            GridFixtures.lookup(router, costs, GridFixtures.point(2, 5), "B.Cu"), 6
        )
        self.assertAlmostEqual(
            GridFixtures.lookup(router, costs, GridFixtures.point(2, 5), "F.Cu"), 16
        )

    def test_using_via_bound_preserves_cost_and_reduces_search(self):
        wall = Obstacle(
            GridFixtures.point(4.5, 0), GridFixtures.point(5.5, 10), ("B.Cu",), "OTHER"
        )
        router = GridFixtures.model((wall,))
        router.weights = Weights(length=1, bend=1, via=10, diagonal=1)
        router.topology_heuristic = True
        start, goal = GridFixtures.point(2, 5), GridFixtures.point(8, 5)
        fast = router.route(start, "B.Cu", goal, "B.Cu")
        router._topology_cache.clear()
        empty = array("d", [0]) * (router.nx * router.ny * len(router.layers))
        with mock.patch("pcb.topology_bound.relaxed_costs", return_value=empty):
            baseline = router.route(start, "B.Cu", goal, "B.Cu")
        self.assertAlmostEqual(fast.cost, baseline.cost)
        self.assertLess(fast.expanded, baseline.expanded)

    def test_heuristic_settings_are_validated(self):
        for kwargs in (
            {"heuristic_weight": True},
            {"heuristic_weight": 0.5},
            {"heuristic_weight": float("inf")},
            {"topology_heuristic": "yes"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                GridRouter(
                    GridFixtures.point(0, 0),
                    GridFixtures.point(10, 10),
                    mm(1),
                    ("B.Cu",),
                    "SIGNAL",
                    mm(0.2),
                    mm(0.2),
                    mm(0.6),
                    mm(0.3),
                    **kwargs,
                )


if __name__ == "__main__":
    unittest.main()
