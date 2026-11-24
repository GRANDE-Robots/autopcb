"""test_route_smoothing.py: check route simplification without touching the saved PCB.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import hashlib
import tempfile
import unittest
from pathlib import Path

from pcb.net_routing.model import Budget, Island, Limits, Net, Site
from pcb.net_routing.geometry import on_segment
from pcb.net_routing.smoothing import simplify_route
from pcb.route_cli import campaign_changes
from pcb.route_optimizer import (
    GridRouter,
    Obstacle,
    Point,
    Route,
    Segment,
    Via,
    Weights,
    mm,
)
from pcb.route_validation import RouteContract, validate_routes
from verification import test_route_optimizer as route_tests


class PointBuilder:
    """Builds a point from x and y in millimetres."""

    def __call__(self, x, y):
        """Build a point from x and y in mm.

        Inputs:
            x: Horizontal position in mm.
            y: Vertical position in mm.
        Outputs:
            The point in nm.
        """
        return Point(mm(x), mm(y))


point = PointBuilder()


class SimpleRouteBuilder:
    """Builds a B.Cu route with a detour between two pad positions."""

    def __call__(self):
        """Build a route with a detour that a clear path makes unnecessary.

        Outputs:
            A route of five B.Cu segments through (1, 1), (3, 1), (4, 2), (5, 2), (6, 1)
            and (8, 1) mm, with no vias.
        """
        pts = [
            point(1, 1),
            point(3, 1),
            point(4, 2),
            point(5, 2),
            point(6, 1),
            point(8, 1),
        ]
        return Route(
            tuple(Segment(a, b, "B.Cu") for a, b in zip(pts, pts[1:])),
            (),
            (),
            0,
            0,
            0,
            3,
        )


simple_route = SimpleRouteBuilder()


class GridRouterBuilder:
    """Builds a grid router over a 10 mm square for the net N."""

    def __call__(self, obstacles=(), **kwargs):
        """Build the grid router that the simplification tests search with.

        Inputs:
            obstacles: Obstacles on the grid.
            kwargs: Further arguments of the router.
        Outputs:
            A router for the net N on B.Cu and F.Cu over (0, 0) mm to (10, 10) mm with a
            0.25 mm step, 0.2 mm tracks and clearance, 0.6 mm vias with 0.3 mm drills,
            gentle turns and no staircases.
        """
        return GridRouter(
            point(0, 0),
            point(10, 10),
            mm(0.25),
            ("B.Cu", "F.Cu"),
            "N",
            mm(0.2),
            mm(0.2),
            mm(0.6),
            mm(0.3),
            obstacles,
            weights=Weights(length=1, bend=1, via=10, diagonal=0),
            gentle_turns=True,
            forbid_staircase=True,
            **kwargs,
        )


router = GridRouterBuilder()


class RouteSmoothingTests(unittest.TestCase):
    """Checks that simplification removes bends only where the copper stays valid."""

    def setUp(self):
        """Create the net N with a pad at (1, 1) mm and one at (8, 1) mm on B.Cu.

        Sets the two pad sites, the net and a 30 s search budget.
        """
        self.start, self.end = Site(point(1, 1), "B.Cu"), Site(point(8, 1), "B.Cu")
        self.net = Net(
            "N",
            (Island("a", (self.start,)), Island("b", (self.end,))),
            pad_sites=frozenset((self.start, self.end)),
        )
        self.budget = Budget(Limits(seconds=30))

    def test_clear_path_loses_unnecessary_bends(self):
        route = simple_route()
        result = simplify_route(
            route, router(), self.net, self.start, self.end, self.budget
        )
        self.assertEqual(
            result.segments, (Segment(self.start.point, self.end.point, "B.Cu"),)
        )
        self.assertEqual(result.bends, 0)
        self.assertEqual(result.vias, ())

    def test_shortcut_does_not_cross_a_foreign_copper_obstacle(self):
        obstacle = Obstacle(point(4.9, 0.5), point(5.1, 0.8), ("B.Cu",), "OTHER")
        model = router((obstacle,))
        result = simplify_route(
            simple_route(), model, self.net, self.start, self.end, self.budget
        )
        margin = model.clearance + model.width // 2
        self.assertTrue(
            all(
                not model._intersects_box(s.start, s.end, obstacle, margin)
                for s in result.segments
            )
        )
        self.assertGreater(len(result.segments), 1)

    def test_vias_stay_in_place_and_keep_their_layers(self):
        p = [
            point(1, 1),
            point(3, 1),
            point(4, 2),
            point(5, 3),
            point(6, 3),
            point(7, 2),
            point(8, 2),
        ]
        via = Via(p[2], "B.Cu", "F.Cu")
        original = Route(
            (
                Segment(p[0], p[1], "B.Cu"),
                Segment(p[1], p[2], "B.Cu"),
                *(Segment(a, b, "F.Cu") for a, b in zip(p[2:], p[3:])),
            ),
            (via,),
            (),
            0,
            0,
            0,
            3,
        )
        end = Site(p[-1], "F.Cu")
        net = Net("N", self.net.islands, pad_sites=frozenset((self.start, end)))
        result = simplify_route(original, router(), net, self.start, end, self.budget)
        self.assertEqual(result.vias, (via,))
        self.assertEqual(result.segments[0].start, self.start.point)
        self.assertEqual(result.segments[-1].end, end.point)
        self.assertEqual({s.layer for s in result.segments}, {"F.Cu", "B.Cu"})

    def test_via_does_not_create_a_false_same_layer_turn(self):
        pts = [
            point(1, 1),
            point(3, 1),
            point(3, 2),
            point(4, 3),
            point(5, 3),
            point(6, 2),
            point(7, 2),
        ]
        via = Via(pts[1], "B.Cu", "F.Cu")
        original = Route(
            (
                Segment(pts[0], pts[1], "B.Cu"),
                *(Segment(a, b, "F.Cu") for a, b in zip(pts[1:], pts[2:])),
            ),
            (via,),
            (),
            0,
            0,
            0,
            1,
        )
        end = Site(pts[-1], "F.Cu")
        net = Net("N", self.net.islands, pad_sites=frozenset((self.start, end)))
        result = simplify_route(original, router(), net, self.start, end, self.budget)
        self.assertLess(len(result.segments), len(original.segments))
        self.assertEqual(result.vias, (via,))
        self.assertEqual(result.segments[0].end, via.at)
        self.assertEqual(result.segments[-1].end, end.point)

    def test_endpoint_heading_constraint_is_preserved(self):
        result = simplify_route(
            simple_route(),
            router(initial_directions=frozenset({0})),
            self.net,
            self.start,
            self.end,
            self.budget,
        )
        self.assertEqual(
            GridRouter._direction(result.segments[0].start, result.segments[0].end), 0
        )

    def test_existing_copper_contact_is_not_shortcut_away(self):
        old = Segment(point(4, 2), point(5, 3), "B.Cu")
        net = Net("N", self.net.islands, (old,), self.net.pad_sites)
        result = simplify_route(
            simple_route(), router(), net, self.start, self.end, self.budget
        )
        self.assertTrue(
            any(
                s.layer == "B.Cu" and on_segment(point(4, 2), s)
                for s in result.segments
            )
        )

    def test_an_intermediate_pad_stays_connected(self):
        pad = Site(point(4, 2), "B.Cu")
        net = Net("N", self.net.islands, pad_sites=self.net.pad_sites | {pad})
        result = simplify_route(
            simple_route(), router(), net, self.start, self.end, self.budget
        )
        self.assertTrue(
            any(
                s.layer == pad.layer and on_segment(pad.point, s)
                for s in result.segments
            )
        )

    def test_expired_budget_returns_an_unchanged_truncated_route(self):
        original = simple_route()
        budget = Budget(Limits(seconds=1))
        budget.started -= 2
        result = simplify_route(
            original, router(), self.net, self.start, self.end, budget
        )
        self.assertEqual(result.segments, original.segments)
        self.assertTrue(result.search_truncated)

    def test_branching_graph_is_left_unchanged(self):
        original = simple_route()
        branch = Segment(point(3, 1), point(4, 0), "B.Cu")
        original = Route((*original.segments, branch), (), (), 0, 0, 0, 3)
        result = simplify_route(
            original, router(), self.net, self.start, self.end, self.budget
        )
        self.assertIs(result, original)

    def test_unexpected_segment_angles_do_not_crash_the_optimizer(self):
        original = Route(
            (
                Segment(point(1, 1), point(3, 2), "B.Cu"),
                Segment(point(3, 2), point(8, 1), "B.Cu"),
            ),
            (),
            (),
            0,
            0,
            0,
            1,
        )
        result = simplify_route(
            original, router(), self.net, self.start, self.end, self.budget
        )
        self.assertIs(result, original)

    def test_simplified_route_passes_native_kicad_on_a_copy(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-smoothing-") as folder:
            source = route_tests.RouteOptimizerTests.make_board(Path(folder))
            original = source.read_bytes()
            points = [
                point(2, 2),
                point(3, 2),
                point(4, 3),
                point(5, 3),
                point(6, 2),
                point(7, 2),
                point(8, 3),
                point(8, 5),
            ]
            path = Route(
                tuple(Segment(a, b, "F.Cu") for a, b in zip(points, points[1:])),
                (),
                (),
                0,
                0,
                0,
                3,
            )
            start, end = Site(points[0], "F.Cu"), Site(points[-1], "F.Cu")
            net = Net(
                "SIGNAL",
                (Island("a", (start,)), Island("b", (end,))),
                pad_sites=frozenset((start, end)),
            )
            model = GridRouter(
                point(0, 0),
                point(10, 8),
                mm(0.25),
                ("F.Cu",),
                "SIGNAL",
                mm(0.2),
                mm(0.2),
                mm(0.6),
                mm(0.3),
                weights=Weights(length=1, bend=1),
                gentle_turns=True,
                forbid_staircase=True,
            )
            result = simplify_route(path, model, net, start, end, self.budget)
            self.assertLess(len(result.segments), len(path.segments))
            changes = campaign_changes(result, "SIGNAL", mm(0.2), mm(0.6), mm(0.3))
            evidence = validate_routes(
                source,
                hashlib.sha256(original).hexdigest(),
                changes,
                [
                    RouteContract(
                        "SIGNAL", start.point, start.layer, end.point, end.layer
                    )
                ],
            )
            self.assertEqual(evidence["status"], "no_new_drc_violations", evidence)
            self.assertLess(
                evidence["candidate_unconnected"], evidence["baseline_unconnected"]
            )
            self.assertEqual(source.read_bytes(), original)
