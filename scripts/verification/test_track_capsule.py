"""test_track_capsule.py: exercise exact trace envelopes without modifying a live KiCad board.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import tempfile
import types
import unittest
import math
import random
from pathlib import Path
from unittest import mock

from pcb.net_routing.backend import NativeBackend
from pcb.route_native import _track_boxes
from pcb.route_cli import plan
from pcb.route_optimizer import (
    GridRouter,
    NoGridRoute,
    Obstacle,
    Point,
    SearchLimitReached,
    mm,
)
from pcb.route_optimizer import Weights, _octilinear_lower_bound
from pcb.route_optimizer import _point_near_segment
from verification import test_route_optimizer as route_tests


class CapsuleGeometry:
    """Builds points and the diagonal copper capsule the track capsule tests use."""

    @staticmethod
    def point(x, y):
        """Return the Point at (x, y).

        Inputs:
            x, y: coordinates in mm.
        """
        return Point.from_mm((x, y))

    @staticmethod
    def capsule():
        """Return a B.Cu copper Obstacle of net OTHER shaped as a capsule.

        The centerline runs from (1, 1) mm to (2, 2) mm with a copper radius of
        0.1 mm inside the box (0.9, 0.9) mm to (2.1, 2.1) mm.
        """
        return Obstacle(
            CapsuleGeometry.point(0.9, 0.9),
            CapsuleGeometry.point(2.1, 2.1),
            ("B.Cu",),
            "OTHER",
            "copper",
            centerline=(CapsuleGeometry.point(1, 1), CapsuleGeometry.point(2, 2)),
            copper_radius=mm(0.1),
        )


class TrackCapsuleTests(unittest.TestCase):
    """Tests for capsule obstacles in the grid router and native track conversion."""

    def test_long_diagonal_rasterization_avoids_the_empty_bounding_rectangle(self):
        model = GridRouter(
            CapsuleGeometry.point(-1, -1),
            CapsuleGeometry.point(61, 61),
            mm(0.1),
            ("B.Cu",),
            "SIGNAL",
            mm(0.2),
            mm(0.2),
            mm(0.6),
            mm(0.3),
            max_cells=500_000,
        )
        first, last, radius = (
            CapsuleGeometry.point(0, 0),
            CapsuleGeometry.point(60, 60),
            mm(0.2),
        )
        obstacle = Obstacle(
            CapsuleGeometry.point(-0.2, -0.2),
            CapsuleGeometry.point(60.2, 60.2),
            ("B.Cu",),
            "OTHER",
            "copper",
            centerline=(first, last),
            copper_radius=radius,
        )
        calls = 0

        def counted(*args):
            """Count the call and return the result of the point-to-segment test."""
            nonlocal calls
            calls += 1
            return _point_near_segment(*args)

        with mock.patch("pcb.route_optimizer._point_near_segment", new=counted):
            actual = model._covered(obstacle, mm(0.2))
        self.assertLess(calls, 30_000)
        expected = {
            (x, y)
            for x in range(model.nx)
            for y in range(model.ny)
            if _point_near_segment(
                model._point(x, y), first, last, radius + mm(0.2) + 1
            )
        }
        self.assertEqual(actual, expected)

    def test_capsule_scanline_matches_brute_force_for_arbitrary_directions(self):
        model = GridRouter(
            CapsuleGeometry.point(-3, -3),
            CapsuleGeometry.point(3, 3),
            mm(0.25),
            ("B.Cu",),
            "SIGNAL",
            mm(0.2),
            mm(0.2),
            mm(0.6),
            mm(0.3),
        )
        rng = random.Random(894)
        cases = [
            (Point(0, 0), Point(0, 0)),
            (CapsuleGeometry.point(0, -2), CapsuleGeometry.point(0, 2)),
            (CapsuleGeometry.point(-2, 0), CapsuleGeometry.point(2, 0)),
            (CapsuleGeometry.point(2, -2), CapsuleGeometry.point(-2, 2)),
        ]
        cases.extend(
            (
                Point(
                    rng.randrange(-2_000_000, 2_000_001),
                    rng.randrange(-2_000_000, 2_000_001),
                ),
                Point(
                    rng.randrange(-2_000_000, 2_000_001),
                    rng.randrange(-2_000_000, 2_000_001),
                ),
            )
            for _ in range(80)
        )
        for first, last in cases:
            radius, margin = rng.randrange(1, 400_000), rng.randrange(0, 400_000)
            obstacle = Obstacle(
                Point(min(first.x, last.x) - radius, min(first.y, last.y) - radius),
                Point(max(first.x, last.x) + radius, max(first.y, last.y) + radius),
                ("B.Cu",),
                "OTHER",
                "copper",
                centerline=(first, last),
                copper_radius=radius,
            )
            with self.subTest(first=first, last=last, radius=radius, margin=margin):
                actual = model._covered(obstacle, margin)
                expected = {
                    (x, y)
                    for x in range(model.nx)
                    for y in range(model.ny)
                    if _point_near_segment(
                        model._point(x, y), first, last, radius + margin + 1
                    )
                }
                self.assertEqual(actual, expected)

    def test_large_capsule_rasterization_obeys_the_search_deadline(self):
        model = GridRouter(
            CapsuleGeometry.point(-2, -1),
            CapsuleGeometry.point(2, 61),
            mm(0.05),
            ("B.Cu",),
            "SIGNAL",
            mm(0.2),
            mm(0.2),
            mm(0.6),
            mm(0.3),
            max_cells=200_000,
        )
        obstacle = Obstacle(
            CapsuleGeometry.point(-1, -1),
            CapsuleGeometry.point(1, 61),
            ("B.Cu",),
            "OTHER",
            "copper",
            centerline=(CapsuleGeometry.point(0, 0), CapsuleGeometry.point(0, 60)),
            copper_radius=mm(1),
        )

        def stop():
            """Raise RuntimeError as an expired search budget."""
            raise RuntimeError("budget expired")

        model.check_budget = stop
        with self.assertRaisesRegex(RuntimeError, "budget expired"):
            model._covered(obstacle, mm(0.2))

    def test_lower_bound_accounts_for_the_diagonal_penalty(self):
        self.assertAlmostEqual(
            _octilinear_lower_bound(10, 10, 1, 1, 0), 10 * math.sqrt(2)
        )
        self.assertAlmostEqual(_octilinear_lower_bound(10, 10, 1, 1, 1), 20)
        self.assertAlmostEqual(_octilinear_lower_bound(10, 5, 1, 1, 1), 15)
        self.assertAlmostEqual(
            _octilinear_lower_bound(10, 5, 1, 2, 0), 10 + 10 * math.sqrt(2)
        )

    def test_stronger_lower_bound_preserves_optimum_and_reduces_search(self):
        model = GridRouter(
            CapsuleGeometry.point(-1, -1),
            CapsuleGeometry.point(11, 11),
            mm(0.25),
            ("B.Cu",),
            "SIGNAL",
            mm(0.2),
            mm(0.2),
            mm(0.6),
            mm(0.3),
            weights=Weights(length=1, bend=1, via=10, diagonal=1),
            gentle_turns=True,
            forbid_staircase=True,
        )
        start, end = CapsuleGeometry.point(0, 0), CapsuleGeometry.point(9, 7)
        fast = model.route(start, "B.Cu", end, "B.Cu")

        def old_lower_bound(dx, dy, step, axial, diagonal):
            """Return the earlier lower bound, which ignores the diagonal weight.

            Inputs:
                dx, dy, step, axial, diagonal: the arguments of the octilinear lower bound;
                    diagonal is ignored.
            """
            del diagonal
            return (max(dx, dy) + (math.sqrt(2) - 1) * min(dx, dy)) * step * axial

        with mock.patch("pcb.route_optimizer._octilinear_lower_bound", old_lower_bound):
            old = model.route(start, "B.Cu", end, "B.Cu")
        self.assertAlmostEqual(fast.cost, old.cost)
        self.assertLess(fast.expanded, old.expanded)

    def test_exact_endpoint_and_segments_clear_false_rectangle_corners(self):
        obstacle = CapsuleGeometry.capsule()
        collision = GridRouter._intersects_box
        margin = mm(0.2)
        self.assertFalse(
            collision(
                CapsuleGeometry.point(2.25, 1),
                CapsuleGeometry.point(2.25, 1),
                obstacle,
                margin,
            )
        )
        self.assertFalse(
            collision(
                CapsuleGeometry.point(2.25, 1),
                CapsuleGeometry.point(2.5, 1),
                obstacle,
                margin,
            )
        )
        self.assertTrue(
            collision(
                CapsuleGeometry.point(1.5, 1.5),
                CapsuleGeometry.point(1.5, 1.5),
                obstacle,
                margin,
            )
        )
        self.assertTrue(
            collision(
                CapsuleGeometry.point(1, 2),
                CapsuleGeometry.point(2, 1),
                obstacle,
                margin,
            )
        )
        self.assertTrue(
            collision(
                CapsuleGeometry.point(1, 0.7),
                CapsuleGeometry.point(2, 0.7),
                obstacle,
                margin,
            )
        )
        self.assertFalse(
            collision(
                CapsuleGeometry.point(1, 0.699),
                CapsuleGeometry.point(2, 0.699),
                obstacle,
                margin,
            )
        )

    def test_grid_tracks_and_vias_use_capsule_with_sampling_margin(self):
        model = GridRouter(
            CapsuleGeometry.point(-1, -1),
            CapsuleGeometry.point(4, 4),
            mm(0.25),
            ("B.Cu",),
            "SIGNAL",
            mm(0.2),
            mm(0.2),
            mm(0.6),
            mm(0.3),
            obstacles=(CapsuleGeometry.capsule(),),
        )
        clear = model._index(CapsuleGeometry.point(2.25, 1))
        blocked = model._index(CapsuleGeometry.point(1.5, 1.75))
        self.assertNotIn(clear, model.blocked["B.Cu"])
        self.assertIn(blocked, model.blocked["B.Cu"])
        self.assertIn(blocked, model.via_blocked)

    def test_both_nets_clearance_expands_actual_capsule(self):
        backend = object.__new__(NativeBackend)
        current = types.SimpleNamespace(clearance=mm(0.2))
        backend.settings = {"OTHER": types.SimpleNamespace(clearance=mm(0.4))}
        enlarged = backend._clearance_envelope(CapsuleGeometry.capsule(), current)
        self.assertEqual(enlarged.copper_radius, mm(0.3))
        self.assertEqual(enlarged.centerline, CapsuleGeometry.capsule().centerline)
        self.assertTrue(
            GridRouter._intersects_box(
                CapsuleGeometry.point(1, 0.5),
                CapsuleGeometry.point(1, 0.5),
                enlarged,
                mm(0.2),
            )
        )

    def test_extra_net_clearance_preserves_rounded_pad_geometry(self):
        backend = object.__new__(NativeBackend)
        current = types.SimpleNamespace(clearance=mm(0.2))
        backend.settings = {"OTHER": types.SimpleNamespace(clearance=mm(0.4))}
        pad = Obstacle(
            CapsuleGeometry.point(0, 0),
            CapsuleGeometry.point(2, 1),
            ("F.Cu",),
            "OTHER",
            "pad",
            corner_radius=mm(0.25),
        )
        enlarged = backend._clearance_envelope(pad, current)
        for x in range(-6, 28):
            for y in range(-6, 18):
                at = CapsuleGeometry.point(x / 10, y / 10)
                self.assertEqual(
                    GridRouter._intersects_box(at, at, pad, mm(0.4)),
                    GridRouter._intersects_box(at, at, enlarged, mm(0.2)),
                )

    def test_native_straight_track_is_covered_by_split_capsules(self):
        import pcbnew

        item = types.SimpleNamespace(
            Type=lambda: pcbnew.PCB_TRACE_T,
            GetStart=lambda: types.SimpleNamespace(x=mm(0), y=mm(0)),
            GetEnd=lambda: types.SimpleNamespace(x=mm(1.000001), y=mm(1)),
            GetWidth=lambda: mm(0.2),
        )
        pieces = _track_boxes(item, ("B.Cu",), "OTHER")
        self.assertEqual(pieces[0].centerline[0], CapsuleGeometry.point(0, 0))
        self.assertEqual(pieces[-1].centerline[-1], Point(mm(1.000001), mm(1)))
        self.assertTrue(all(part.copper_radius >= mm(0.1) + 2 for part in pieces))
        self.assertTrue(
            all(a.centerline[1] == b.centerline[0] for a, b in zip(pieces, pieces[1:]))
        )

    def test_long_trace_uses_fewer_capsules_without_missing_copper(self):
        import pcbnew

        first, last = Point(0, 0), Point(mm(7.123456), mm(-4.123457))
        item = types.SimpleNamespace(
            Type=lambda: pcbnew.PCB_TRACE_T,
            GetStart=lambda: types.SimpleNamespace(x=first.x, y=first.y),
            GetEnd=lambda: types.SimpleNamespace(x=last.x, y=last.y),
            GetWidth=lambda: mm(0.2),
        )
        pieces = _track_boxes(item, ("B.Cu",), "OTHER")
        self.assertEqual(len(pieces), 4)
        radius = mm(0.1)
        original = Obstacle(
            Point(min(first.x, last.x) - radius, min(first.y, last.y) - radius),
            Point(max(first.x, last.x) + radius, max(first.y, last.y) + radius),
            ("B.Cu",),
            "OTHER",
            "copper",
            centerline=(first, last),
            copper_radius=radius,
        )
        rng = random.Random(91)
        for _ in range(5000):
            a = Point(
                rng.randrange(-1_000_000, 8_000_000),
                rng.randrange(-5_000_000, 1_000_000),
            )
            b = Point(
                a.x + rng.randrange(-600_000, 600_001),
                a.y + rng.randrange(-600_000, 600_001),
            )
            if GridRouter._intersects_box(a, b, original, mm(0.2)):
                self.assertTrue(
                    any(
                        GridRouter._intersects_box(a, b, part, mm(0.2))
                        for part in pieces
                    )
                )

    def test_track_envelope_rejects_a_via_without_calling_its_width(self):
        import pcbnew

        class ViaWithForbiddenWidth:
            """Stand-in via that fails the test when its width is read."""

            def GetStart(self):
                """Return the start position (0, 0)."""
                return types.SimpleNamespace(x=0, y=0)

            def GetEnd(self):
                """Return the end position (0, 0)."""
                return types.SimpleNamespace(x=0, y=0)

            def GetWidth(self, *_args):
                """Raise AssertionError because reading a via width is unsafe."""
                raise AssertionError("Called the unsafe via width accessor")

        with mock.patch.object(pcbnew, "PCB_VIA", ViaWithForbiddenWidth):
            with self.assertRaisesRegex(ValueError, "layer is required"):
                _track_boxes(ViaWithForbiddenWidth(), ("B.Cu",), "OTHER")

    def test_capsule_requires_conservative_bounds_and_nonnegative_radius(self):
        with self.assertRaises(ValueError):
            Obstacle(
                CapsuleGeometry.point(1, 1),
                CapsuleGeometry.point(2, 2),
                ("B.Cu",),
                "X",
                "copper",
                centerline=(CapsuleGeometry.point(1, 1), CapsuleGeometry.point(2, 2)),
                copper_radius=mm(0.1),
            )
        with self.assertRaises(ValueError):
            Obstacle(
                CapsuleGeometry.point(0, 0),
                CapsuleGeometry.point(3, 3),
                ("B.Cu",),
                "X",
                "pad",
                centerline=(CapsuleGeometry.point(1, 1), CapsuleGeometry.point(2, 2)),
                copper_radius=mm(0.1),
            )
        with self.assertRaises(ValueError):
            Obstacle(
                CapsuleGeometry.point(0, 0),
                CapsuleGeometry.point(3, 3),
                ("B.Cu",),
                "X",
                "copper",
                copper_radius=-1,
            )

    def test_diagonal_native_copper_route_passes_kicad_validation(self):
        import pcbnew

        with tempfile.TemporaryDirectory(prefix="autopcb-capsule-") as folder:
            project = Path(folder)
            source = route_tests.RouteOptimizerTests.make_board(project, crossing=True)
            board = pcbnew.LoadBoard(str(source))
            trace = next(iter(board.GetTracks()))
            trace.SetStart(pcbnew.VECTOR2I(mm(4), mm(1)))
            trace.SetEnd(pcbnew.VECTOR2I(mm(5.5), mm(2.5)))
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            options = route_tests.RouteOptimizerTests.config()
            options["board_snapshot"] = "pcb"
            result = plan(options, project, include_changes=True, validate=True)
            self.assertEqual(result["validation"]["status"], "no_new_drc_violations")
            self.assertEqual(result["validation"]["endpoint_connectivity"], "connected")
            self.assertEqual(source.read_bytes(), original)

    def test_search_reports_work_done_when_limit_is_hit_or_no_path_exists(self):
        wall = Obstacle(
            CapsuleGeometry.point(4, -2),
            CapsuleGeometry.point(6, 12),
            ("B.Cu",),
            "OTHER",
        )
        options = dict(
            lower=CapsuleGeometry.point(-2, -2),
            upper=CapsuleGeometry.point(12, 12),
            step=mm(0.5),
            layers=("B.Cu",),
            net="SIGNAL",
            width=mm(0.2),
            clearance=mm(0.2),
            via_size=mm(0.6),
            via_drill=mm(0.3),
            obstacles=(wall,),
        )
        start, end = CapsuleGeometry.point(0.1, 0.1), CapsuleGeometry.point(9.1, 9.1)
        with self.assertRaises(SearchLimitReached) as limited:
            GridRouter(**options, max_expansions=1).route(start, "B.Cu", end, "B.Cu")
        self.assertEqual(limited.exception.expanded, 1)
        # Exhaust the grid itself without the additional off-grid access-pair
        # search, which may correctly stop at its own resource limit.
        with self.assertRaises(NoGridRoute) as exhausted:
            GridRouter(**options).route(
                CapsuleGeometry.point(0, 0), "B.Cu", CapsuleGeometry.point(9, 9), "B.Cu"
            )
        self.assertGreater(exhausted.exception.expanded, 0)


if __name__ == "__main__":
    unittest.main()
