"""Check the optimistic connectivity and via-count bound on small grids."""

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


def p(x, y):
    return Point.from_mm((x, y))


def model(obstacles=()):
    return GridRouter(
        p(0, 0),
        p(10, 10),
        mm(0.25),
        ("F.Cu", "B.Cu"),
        "SIGNAL",
        mm(0.2),
        mm(0.2),
        mm(0.6),
        mm(0.3),
        obstacles=obstacles,
    )


def lookup(router, distances, point, layer):
    x, y = router._index(point)
    return distances[
        router.layers.index(layer) * router.nx * router.ny + y * router.nx + x
    ]


class TopologyBoundTests(unittest.TestCase):
    def test_grid_bounds_are_exact_without_constructing_points(self):
        router = GridRouter(
            p(-1.23, 4.56), p(2.07, 7.89), mm(.37), ("F.Cu", "B.Cu"),
            "SIGNAL", mm(.2), mm(.1), mm(.6), mm(.3),
        )
        for margin in (0, 1, mm(.15), mm(.4), mm(2)):
            expected = {
                (x, y): router._inside_point(router._point(x, y), margin)
                for x in range(-2, router.nx + 3)
                for y in range(-2, router.ny + 3)
            }
            with mock.patch.object(router, "_point", side_effect=AssertionError("point allocation")):
                actual = {
                    (x, y): router._inside(x, y, margin)
                    for x in range(-2, router.nx + 3)
                    for y in range(-2, router.ny + 3)
                }
            self.assertEqual(actual, expected)

    def test_topology_cell_bounds_match_exact_router_bounds(self):
        from pcb.topology_bound import grid_bounds

        router = GridRouter(
            p(-1.23, 4.56), p(2.07, 7.89), mm(.37), ("F.Cu", "B.Cu"),
            "SIGNAL", mm(.2), mm(.1), mm(.6), mm(.3),
        )
        for margin in (0, 1, mm(.15), mm(.4), mm(2)):
            xmin, ymin, xmax, ymax = grid_bounds(router, margin)
            for x in range(-2, router.nx + 3):
                for y in range(-2, router.ny + 3):
                    self.assertEqual(xmin <= x <= xmax and ymin <= y <= ymax,
                                     router._inside(x, y, margin))

    def test_partial_guidance_matches_unguided_search_on_seeded_obstacle_fields(self):
        rng = random.Random(20261029)
        compared = 0
        for _ in range(20):
            obstacles = tuple(
                Obstacle(p(x - 0.04, y - 0.04), p(x + 0.04, y + 0.04),
                         ("F.Cu", "B.Cu"), "OTHER")
                for x in range(1, 8) for y in range(1, 8)
                if (x, y) not in {(1, 1), (7, 7)} and rng.random() < 0.12
            )
            settings = dict(lower=p(0, 0), upper=p(8, 8), step=mm(1),
                            layers=("F.Cu", "B.Cu"), net="SIGNAL",
                            width=mm(0.2), clearance=mm(0.1),
                            via_size=mm(0.6), via_drill=mm(0.3),
                            obstacles=obstacles, max_expansions=30_000,
                            weights=Weights(length=1, bend=1, via=10, diagonal=1),
                            forbid_staircase=True, gentle_turns=True)
            baseline = GridRouter(**settings)
            guided = GridRouter(**settings, topology_heuristic=True)
            try:
                reference = baseline.route(p(1, 1), "B.Cu", p(7, 7), "B.Cu")
            except NoGridRoute:
                continue
            result = guided.route(p(1, 1), "B.Cu", p(7, 7), "B.Cu")
            self.assertAlmostEqual(result.cost, reference.cost, places=7)
            self.assertTrue(all(guided.edge_clear(s.start, s.end, s.layer)
                                for s in result.segments))
            compared += 1
        self.assertGreaterEqual(compared, 5)

    def test_partial_bound_stops_after_reaching_start_and_stays_optimistic(self):
        self.assertIn("stop_at", inspect.signature(relaxed_costs).parameters)
        router = model()
        gx, gy = router._index(p(8, 5))
        sx, sy = router._index(p(8.25, 5))
        layer = router.layers.index("B.Cu")
        goal, start = (gx, gy, layer), (sx, sy, layer)
        full = relaxed_costs(router, goal)
        router.check_budget = mock.Mock()
        partial = relaxed_costs(router, goal, stop_at=start, max_expansions=5000)
        self.assertAlmostEqual(lookup(router, partial, p(8.25, 5), "B.Cu"),
                               lookup(router, full, p(8.25, 5), "B.Cu"))
        self.assertLess(router.check_budget.call_count, 5)
        self.assertTrue(all(pcost <= fcost + 1e-8
                            for pcost, fcost in strict_zip(partial, full)
                            if math.isfinite(fcost)))

    def test_partial_bound_never_calls_unexplored_cells_unreachable(self):
        self.assertIn("stop_at", inspect.signature(relaxed_costs).parameters)
        wall = Obstacle(p(4.5, 0), p(5.5, 10), ("B.Cu", "F.Cu"), "OTHER")
        router = model((wall,))
        gx, gy = router._index(p(8, 5))
        sx, sy = router._index(p(2, 5))
        layer = router.layers.index("B.Cu")
        costs = relaxed_costs(router, (gx, gy, layer), stop_at=(sx, sy, layer),
                              max_expansions=2)
        self.assertEqual(lookup(router, costs, p(2, 5), "B.Cu"), 0)

    def test_relaxed_topology_does_not_reject_a_clear_diagonal(self):
        obstacles = tuple(
            Obstacle(p(x - 0.05, y - 0.05), p(x + 0.05, y + 0.05),
                     ("F.Cu", "B.Cu"), "OTHER")
            for x, y in ((2, 1), (1, 2))
        )
        router = GridRouter(
            p(0, 0), p(3, 3), mm(1), ("F.Cu", "B.Cu"), "SIGNAL",
            mm(0.2), mm(0.1), mm(0.6), mm(0.3), obstacles=obstacles,
            topology_heuristic=True,
        )
        try:
            route = router.route(p(1, 1), "B.Cu", p(2, 2), "B.Cu")
        except NoGridRoute as exc:
            self.fail(f"The clearance-checked diagonal is legal: {exc}")
        self.assertTrue(all(router.edge_clear(s.start, s.end, s.layer) for s in route.segments))
        self.assertEqual(route.segments[0].start, p(1, 1))
        self.assertEqual(route.segments[-1].end, p(2, 2))
        x, y = router._index(p(2, 2))
        costs = relaxed_costs(router, (x, y, router.layers.index("B.Cu")))
        self.assertLessEqual(lookup(router, costs, p(1, 1), "B.Cu"), route.cost)
        vias = minimum_vias(router, (x, y, router.layers.index("B.Cu")))
        self.assertEqual(lookup(router, vias, p(1, 1), "B.Cu"), 0)

    def test_relaxed_grid_analysis_stops_when_the_search_budget_expires(self):
        router = model()
        budget = Budget(Limits(seconds=1), started=0)
        router.check_budget = budget.check
        x, y = router._index(p(8, 5))
        with mock.patch("pcb.net_routing.model.time.monotonic", return_value=2):
            with self.assertRaises(BudgetExhausted):
                relaxed_costs(router, (x, y, router.layers.index("B.Cu")))
        self.assertIn("seconds", budget.limits_hit)

    def test_two_vias_required_to_go_around_a_single_layer_wall(self):
        wall = Obstacle(p(4.5, 0), p(5.5, 10), ("B.Cu",), "OTHER")
        router = model((wall,))
        x, y = router._index(p(8, 5))
        distances = minimum_vias(router, (x, y, router.layers.index("B.Cu")))
        self.assertEqual(lookup(router, distances, p(2, 5), "B.Cu"), 2)
        self.assertEqual(lookup(router, distances, p(2, 5), "F.Cu"), 1)

    def test_wall_on_both_layers_is_disconnected_in_this_model(self):
        wall = Obstacle(p(4.5, 0), p(5.5, 10), ("B.Cu", "F.Cu"), "OTHER")
        router = model((wall,))
        x, y = router._index(p(8, 5))
        distances = minimum_vias(router, (x, y, router.layers.index("B.Cu")))
        self.assertEqual(lookup(router, distances, p(2, 5), "B.Cu"), UNREACHABLE)

    def test_existing_same_net_via_is_free(self):
        via = Obstacle(p(2.2, 2.2), p(2.8, 2.8), ("F.Cu", "B.Cu"), "SIGNAL", "via")
        router = model((via,))
        x, y = router._index(p(8, 5))
        distances = minimum_vias(router, (x, y, router.layers.index("B.Cu")))
        self.assertEqual(lookup(router, distances, p(2, 2), "F.Cu"), 0)

    def test_relaxed_costs_include_distance_and_via_cost(self):
        router = model()
        router.weights = Weights(length=1, bend=1, via=10, diagonal=1)
        x, y = router._index(p(8, 5))
        costs = relaxed_costs(router, (x, y, router.layers.index("B.Cu")))
        self.assertAlmostEqual(lookup(router, costs, p(2, 5), "B.Cu"), 6)
        self.assertAlmostEqual(lookup(router, costs, p(2, 5), "F.Cu"), 16)

    def test_using_via_bound_preserves_cost_and_reduces_search(self):
        wall = Obstacle(p(4.5, 0), p(5.5, 10), ("B.Cu",), "OTHER")
        router = model((wall,))
        router.weights = Weights(length=1, bend=1, via=10, diagonal=1)
        router.topology_heuristic = True
        start, goal = p(2, 5), p(8, 5)
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
                    p(0, 0),
                    p(10, 10),
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
