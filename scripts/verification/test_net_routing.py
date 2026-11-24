"""Regression cases for whole-net geometry, search order and real KiCad checks."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pcbnew

from pcb.net_routing.backend import NativeBackend, Settings
from pcb.net_routing.cli import main
from pcb.net_routing.geometry import (
    RouteGeometryError,
    aggregate,
    branch_sites,
    on_segment,
    unique_segments,
    vertices,
)
from pcb.net_routing.model import (
    Budget,
    BudgetExhausted,
    Island,
    Limits,
    Net,
    NetPlan,
    Site,
)
from pcb.net_routing.native import NativeNet, Snapshot, discover, load_board
from pcb.net_routing.schedule import net_orders, search_orders
from pcb.net_routing.topology import EMPTY_ROUTE, whole_net
from pcb.net_routing.workflow import (
    _solve_with_terminal_refinement,
    configurations,
    plan_nets,
)
from pcb.route_optimizer import COPPER_LAYERS, Point, Route, Segment, Via, Weights, mm
from pcb.route_cli import campaign_changes
from pcb.route_validation import RouteContract, _drc, validate_routes
from verification import test_route_optimizer as route_tests
from core.toolchain import kicad_cli


def site(x, y, layer="F.Cu"):
    return Site(Point.from_mm([x, y]), layer)


def segment(a, b, layer="F.Cu"):
    return Segment(site(*a).point, site(*b).point, layer)


def route(*segments, cost=0, vias=()):
    return Route(tuple(segments), tuple(vias), (), 0, 0, cost, 0)


def combine(*routes, **overrides):
    values = dict(existing=(), weights=Weights(), layer_penalties={}, guide=())
    values.update(overrides)
    return aggregate(tuple(routes), **values)


def make_net(name, positions):
    sites = tuple(site(*p) for p in positions)
    return Net(
        name,
        tuple(Island(str(i), (s,)) for i, s in enumerate(sites)),
        pad_sites=frozenset(sites),
    )


def synthetic_backend(net, budget, **settings):
    raw = dict(
        bounds_mm=[-2, -2, 12, 12],
        step_mm=1,
        layers=["F.Cu"],
        width_mm=0.2,
        clearance_mm=0.2,
        layer_penalties={},
        max_expansions=5000,
    )
    raw.update(settings)
    native = NativeNet(net, (), (), False)
    return NativeBackend(
        {
            net.name: Settings(
                raw, native, {layer: mm(0.035) for layer in COPPER_LAYERS}
            )
        },
        (),
        budget,
    )


class NetGeometryTests(unittest.TestCase):
    def test_candidate_combination_does_not_hide_unexpected_value_errors(self):
        net = make_net("SIGNAL", [(0, 0), (1, 0)])
        budget = Budget(Limits(seconds=30, tree_beam=1))

        class BrokenBackend:
            def paths(self, net, prior, tree, start, end):
                return (route(segment((0, 0), (1, 0))),)

            def combine(self, net, routes):
                raise ValueError("unexpected combination failure")

            def accept(self, net, candidate):
                return True

        with self.assertRaisesRegex(ValueError, "unexpected combination failure"):
            whole_net(net, (), BrokenBackend(), budget)

    def test_direct_plane_join_does_not_hide_unexpected_value_errors(self):
        pad = site(0, 0, "B.Cu")
        virtual = site(1.2, 0, "In1.Cu")
        physical = site(5, 0, "In1.Cu")
        net = Net(
            "SIGNAL",
            (
                Island("pad", (pad,)),
                Island(
                    "plane", (physical, virtual), zone_anchors=((virtual, physical),)
                ),
            ),
            pad_sites=frozenset((pad,)),
        )
        budget = Budget(Limits(seconds=30, path_candidates=1))
        backend = synthetic_backend(
            net, budget, layers=["B.Cu", "In1.Cu"], step_mm=0.25
        )
        with patch.object(
            backend, "combine", side_effect=ValueError("unexpected join failure")
        ):
            with self.assertRaisesRegex(ValueError, "unexpected join failure"):
                backend.paths(net, (), EMPTY_ROUTE, pad, virtual)

    def test_context_join_does_not_hide_unexpected_value_errors(self):
        net = make_net("SIGNAL", [(0, 0), (1, 0)])
        budget = Budget(Limits(seconds=30))
        backend = synthetic_backend(net, budget)
        router = backend.settings[net.name].router((), frozenset(), budget)
        candidate = route(segment((0, 0), (1, 0)))
        with patch.object(backend, "_path", return_value=(candidate, False)):
            with patch.object(
                backend, "combine", side_effect=ValueError("unexpected context failure")
            ):
                with self.assertRaisesRegex(ValueError, "unexpected context failure"):
                    backend._path_with_context(
                        router, net, EMPTY_ROUTE, site(0, 0), site(1, 0)
                    )

    def test_electrical_screen_does_not_hide_unexpected_value_errors(self):
        net = make_net("SIGNAL", [(0, 0), (1, 0)])
        backend = synthetic_backend(net, Budget(Limits(seconds=30)))
        with patch.object(
            backend.settings[net.name],
            "assessment",
            side_effect=ValueError("unexpected electrical failure"),
        ):
            with self.assertRaisesRegex(ValueError, "unexpected electrical failure"):
                backend.accept(net, route(segment((0, 0), (1, 0))))

    def test_electrical_limit_violation_rejects_only_the_candidate(self):
        from pcb.route_constraints import ElectricalConstraintViolation

        net = make_net("SIGNAL", [(0, 0), (1, 0)])
        budget = Budget(Limits(seconds=30))
        backend = synthetic_backend(net, budget)
        with patch.object(
            backend.settings[net.name],
            "assessment",
            side_effect=ElectricalConstraintViolation("candidate exceeds limit"),
        ):
            self.assertFalse(backend.accept(net, route(segment((0, 0), (1, 0)))))
        self.assertIn("electrical_screen", budget.failures)

    def test_refinement_does_not_hide_unexpected_geometry_errors(self):
        from pcb.net_routing import smoothing

        net = make_net("SIGNAL", [(0, 0), (2, 0)])
        budget = Budget(Limits(seconds=30))
        backend = synthetic_backend(net, budget)
        router = backend.settings[net.name].router((), frozenset(), budget)
        candidate = route(segment((0, 0), (1, 0)), segment((1, 0), (2, 0)))
        with patch.object(
            smoothing,
            "aggregate",
            side_effect=ValueError("unexpected refinement failure"),
        ):
            with self.assertRaisesRegex(ValueError, "unexpected refinement failure"):
                smoothing.simplify_terminal_branches(candidate, router, net, budget)
            with self.assertRaisesRegex(ValueError, "unexpected refinement failure"):
                smoothing.simplify_route(
                    candidate, router, net, site(0, 0), site(2, 0), budget
                )

    def test_refinement_declines_non_octilinear_legacy_copper(self):
        from pcb.net_routing import smoothing

        net = make_net("SIGNAL", [(0, 0), (2, 1)])
        budget = Budget(Limits(seconds=30))
        backend = synthetic_backend(net, budget)
        router = backend.settings[net.name].router((), frozenset(), budget)
        candidate = route(segment((0, 0), (2, 1)))
        self.assertIs(
            smoothing.simplify_terminal_branches(candidate, router, net, budget),
            candidate,
        )
        self.assertIs(
            smoothing.simplify_route(
                candidate, router, net, site(0, 0), site(2, 1), budget
            ),
            candidate,
        )

    def test_existing_copper_index_does_not_hide_unexpected_errors(self):
        from pcb.net_routing import geometry

        old = (segment((0, 0), (2, 0)),)
        geometry._existing_lines.cache_clear()
        with patch.object(
            geometry, "_line", side_effect=ValueError("unexpected index failure")
        ):
            with self.assertRaisesRegex(ValueError, "unexpected index failure"):
                geometry.unique_segments((), old)
            with self.assertRaisesRegex(ValueError, "unexpected index failure"):
                geometry._existing_lines(old)
        geometry._existing_lines.cache_clear()

    def test_branch_projection_does_not_hide_unexpected_errors(self):
        from pcb.net_routing import geometry

        with patch.object(
            geometry,
            "intersection",
            side_effect=ValueError("unexpected projection failure"),
        ):
            with self.assertRaisesRegex(ValueError, "unexpected projection failure"):
                geometry.branch_sites((segment((0, 0), (2, 0)),), (site(1, 1),), 8)

    def test_repair_nets_can_be_searched_before_the_new_target(self):
        target = make_net("TARGET", [(0, 0), (4, 0)])
        repair = make_net("REPAIR", [(0, 1), (4, 1)])
        budget = Budget(Limits(seconds=30, order_trials=2))
        visited = []

        def solve(net, prior):
            visited.append((net.name, tuple(plan.net for plan in prior)))
            if net.name == "REPAIR" and prior:
                return ()
            return (NetPlan(net.name, route()),)

        plans, trials = search_orders(
            (target, repair), budget, solve, preferred_first=frozenset({"REPAIR"})
        )
        self.assertTrue(plans)
        self.assertEqual(trials[0]["order"], ["REPAIR", "TARGET"])
        self.assertEqual(visited[0], ("REPAIR", ()))

    def test_explicit_net_order_is_not_overridden_by_repair_preference(self):
        target = make_net("TARGET", [(0, 0), (4, 0)])
        repair = make_net("REPAIR", [(0, 1), (4, 1)])
        budget = Budget(Limits(seconds=30, order_trials=1))
        plans, trials = search_orders(
            (target, repair),
            budget,
            lambda net, prior: (NetPlan(net.name, route()),),
            requested=[["TARGET", "REPAIR"]],
            preferred_first=frozenset({"REPAIR"}),
        )
        self.assertTrue(plans)
        self.assertEqual(trials[0]["order"], ["TARGET", "REPAIR"])

    def test_slow_repair_order_leaves_time_to_try_target_first(self):
        target = make_net("TARGET", [(0, 0), (4, 0)])
        repair = make_net("REPAIR", [(0, 1), (4, 1)])
        clock = [0.0]
        budget = Budget(Limits(seconds=100, order_trials=2), started=0.0)
        visited = []

        def solve(net, prior):
            visited.append((net.name, tuple(plan.net for plan in prior)))
            if net.name == "TARGET" and prior:
                while True:
                    clock[0] += 1
                    budget.check()
            return (NetPlan(net.name, route()),)

        with patch(
            "pcb.net_routing.model.time.monotonic", side_effect=lambda: clock[0]
        ):
            plans, trials = search_orders(
                (target, repair), budget, solve, preferred_first=frozenset({"REPAIR"})
            )
        self.assertTrue(plans)
        self.assertEqual(trials[0]["order"], ["REPAIR", "TARGET"])
        self.assertEqual(trials[1]["order"], ["TARGET", "REPAIR"])
        self.assertIn(("TARGET", ()), visited)
        self.assertIn("order_seconds", budget.limits_hit)

    def test_starting_islands_share_the_per_net_path_budget(self):
        first, second, alternate = site(0, 0), site(1, 0), site(2, 0)
        net = Net(
            "SIGNAL",
            (
                Island("first", (first,)),
                Island("second", (second, alternate)),
            ),
        )
        budget = Budget(
            Limits(
                seconds=30,
                tree_beam=2,
                connections_per_step=2,
                fallback_connections_per_step=2,
                path_calls=2,
                net_path_calls=2,
            )
        )

        class DirectionalBackend:
            def paths(self, net, prior, tree, start, end):
                budget.call()
                if start == second and end == first:
                    return (route(segment((1, 0), (0, 0)), cost=1),)
                return ()

            def combine(self, net, routes):
                return combine(*routes)

            def accept(self, net, candidate):
                return True

        plans = whole_net(net, (), DirectionalBackend(), budget)
        self.assertTrue(plans)
        self.assertEqual(budget.calls, 2)

    def test_slow_starting_island_leaves_time_for_another_start(self):
        first, second = site(0, 0), site(1, 0)
        net = Net("SIGNAL", (Island("first", (first,)), Island("second", (second,))))
        clock = [0.0]
        budget = Budget(Limits(seconds=10, tree_beam=2, path_calls=2), started=0.0)

        class DirectionalBackend:
            def paths(self, net, prior, tree, start, end):
                budget.call()
                if start == first:
                    while True:
                        clock[0] += 1
                        budget.check()
                return (route(segment((1, 0), (0, 0)), cost=1),)

            def combine(self, net, routes):
                return combine(*routes)

            def accept(self, net, candidate):
                return True

        with patch(
            "pcb.net_routing.model.time.monotonic", side_effect=lambda: clock[0]
        ):
            plans = whole_net(net, (), DirectionalBackend(), budget)
        self.assertTrue(plans)
        self.assertIn("tree_seconds", budget.limits_hit)

    def test_slow_nearest_access_point_leaves_time_for_another_on_the_same_island(self):
        origin, near, alternate = site(0, 0), site(1, 0), site(2, 0)
        net = Net(
            "SIGNAL", (Island("start", (origin,)), Island("end", (near, alternate)))
        )
        clock = [0.0]
        budget = Budget(
            Limits(
                seconds=12,
                tree_beam=1,
                connections_per_step=2,
                fallback_connections_per_step=2,
                path_calls=4,
            ),
            started=0.0,
        )
        seen = []

        class Backend:
            def paths(self, net, prior, tree, start, end):
                budget.call()
                seen.append(end)
                if end == near:
                    while True:
                        clock[0] += 1
                        budget.check()
                return (route(Segment(start.point, end.point, start.layer)),)

            def combine(self, net, routes):
                return combine(*routes)

            def accept(self, net, candidate):
                return True

        with patch(
            "pcb.net_routing.model.time.monotonic", side_effect=lambda: clock[0]
        ):
            plans = whole_net(net, (), Backend(), budget)
        self.assertTrue(plans)
        self.assertIn(alternate, seen)
        self.assertIn("endpoint_seconds", budget.limits_hit)
        self.assertLess(clock[0], 12)
        self.assertIsNone(budget.slice_deadline)

    def test_path_search_retries_a_staircase_at_existing_copper(self):
        contact, endpoint, old_pad = site(7, 2), site(6, 3.5), site(8, 1)
        net = Net(
            "SIGNAL",
            (Island("a", (contact,)), Island("b", (endpoint,))),
            (segment((8, 1), (7, 2)),),
            frozenset((old_pad, endpoint)),
        )
        budget = Budget(Limits(seconds=30, path_candidates=1, smoothing_trials=0))
        backend = synthetic_backend(net, budget, step_mm=0.25, forbid_staircase=True)
        found = backend.paths(net, (), EMPTY_ROUTE, endpoint, contact)
        self.assertTrue(found)
        for candidate in found:
            backend.combine(net, (candidate,))

    def test_staircase_error_identifies_added_copper_in_the_conflict(self):
        from pcb.net_routing.geometry import StaircaseGeometryError

        old = segment((0, 0), (1, 1))
        added = route(segment((1, 1), (2, 1)), segment((2, 1), (3, 2)))
        with self.assertRaises(StaircaseGeometryError) as caught:
            combine(
                added, existing=(old,), pad_sites=frozenset((site(0, 0), site(3, 2)))
            )
        witnesses = caught.exception.witnesses
        self.assertTrue(witnesses)
        for layer, point in witnesses:
            self.assertEqual(layer, "F.Cu")
            self.assertTrue(any(on_segment(point, part) for part in added.segments))
            self.assertNotIn(point, (site(0, 0).point, site(3, 2).point))

    def test_standard_dogleg_combines_with_existing_outer_runs(self):
        existing = (segment((0, 0), (1, 0)), segment((3, 4), (4, 4)))
        added = route(
            segment((1, 0), (2, 1)), segment((2, 1), (2, 3)), segment((2, 3), (3, 4))
        )
        result = combine(
            added, existing=existing, pad_sites=frozenset((site(0, 0), site(4, 4)))
        )
        self.assertTrue(result.segments)

    def test_a_dogleg_crossing_existing_copper_of_its_net_is_not_a_staircase(self):
        existing = (segment((0, 1), (6, 1)),)
        added = route(
            segment((1, 2), (2, 2)),
            segment((2, 2), (4, 0)),
            segment((4, 0), (4, -2)),
            segment((4, -2), (5, -3)),
            segment((5, -3), (6, -3)),
        )
        result = combine(
            added, existing=existing, pad_sites=frozenset((site(1, 2), site(6, -3)))
        )
        self.assertTrue(result.segments)

    def test_a_dogleg_whose_exit_lies_inside_pad_copper_is_a_staircase(self):
        from pcb.net_routing.geometry import StaircaseGeometryError
        from pcb.route_optimizer import PadCopper

        added = route(
            segment((0, 6), (1, 6)),
            segment((1, 6), (2, 5)),
            segment((2, 5), (2, 3)),
            segment((2, 3), (3, 2)),
            segment((3, 2), (4, 2)),
        )
        pads = frozenset((site(0, 6), site(4, 2)))
        self.assertTrue(combine(added, pad_sites=pads).segments)
        copper = PadCopper((("F.Cu", mm(2.9), mm(1.5), mm(4.5), mm(2.5)),))
        with self.assertRaises(StaircaseGeometryError):
            combine(added, pad_sites=pads, pad_copper=copper)

    def test_context_detour_prioritizes_the_conflicting_added_copper(self):
        from pcb.net_routing.geometry import StaircaseGeometryError

        net = make_net("SIGNAL", [(0, 0), (5, 1)])
        budget = Budget(Limits(seconds=20, path_calls=20, smoothing_trials=0))
        backend = synthetic_backend(net, budget)
        model = backend.settings[net.name].router((), frozenset(), budget)
        rejected = (
            route(segment((0, 0), (4, 0)), segment((4, 0), (5, 1))),
            route(
                segment((0, 0), (3, 0)),
                segment((3, 0), (4, 1)),
                segment((4, 1), (5, 1)),
            ),
            route(segment((0, 0), (1, 1)), segment((1, 1), (5, 1))),
        )
        witnesses = (site(4.5, 0.5), site(3.5, 0.5), site(0.5, 0.5))
        accepted = route(
            segment((0, 0), (2, 0)),
            segment((2, 0), (3, 1)),
            segment((3, 1), (5, 1)),
            cost=1,
        )

        def combined(_net, routes):
            candidate = routes[-1]
            if candidate is accepted:
                return accepted
            index = next(i for i, item in enumerate(rejected) if item is candidate)
            witness = witnesses[index]
            raise StaircaseGeometryError("context", ((witness.layer, witness.point),))

        def search(router, _net, _tree, _start, _end, *, context_check):
            if router.edge_clear is not original_clearance:
                self.assertFalse(
                    router.edge_clear(site(4, 0).point, site(5, 1).point, "F.Cu")
                )
                self.assertTrue(
                    router.edge_clear(site(0, 0).point, site(4, 0).point, "F.Cu")
                )
                self.assertTrue(context_check(accepted))
                return accepted, False
            for candidate in rejected:
                self.assertFalse(context_check(candidate))
            self.fail("The router did not try its conflict-directed detour")

        with (
            patch.object(model, "edge_clear", return_value=True) as original_clearance,
            patch.object(backend, "combine", side_effect=combined),
            patch.object(backend, "_path", side_effect=search),
        ):
            result, limited = backend._path_with_context(
                model, net, EMPTY_ROUTE, site(0, 0), site(5, 1)
            )
        self.assertEqual(result, accepted)
        self.assertTrue(limited)

    def test_off_grid_warm_search_checks_staircase_context_before_selecting_a_path(
        self,
    ):
        contact, endpoint, old_pad = (
            site(7.03, 2.02),
            site(6.03, 3.52),
            site(8.03, 1.02),
        )
        net = Net(
            "SIGNAL",
            (Island("a", (contact,)), Island("b", (endpoint,))),
            (segment((8.03, 1.02), (7.03, 2.02)),),
            frozenset((old_pad, endpoint)),
        )
        budget = Budget(Limits(seconds=30, path_candidates=1, smoothing_trials=0))
        backend = synthetic_backend(
            net, budget, step_mm=0.25, forbid_staircase=True, first_feasible=True
        )
        model = backend.settings[net.name].router((), frozenset(), budget)
        with patch.object(
            backend, "alternative_headings", wraps=backend.alternative_headings
        ) as retry:
            result, _ = backend._path_with_context(
                model, net, EMPTY_ROUTE, contact, endpoint
            )
        backend.combine(net, (result,))
        retry.assert_not_called()

    def test_on_grid_warm_search_checks_staircase_context_during_search(self):
        contact, endpoint, old_pad = site(7, 2), site(6, 3.5), site(8, 1)
        net = Net(
            "SIGNAL",
            (Island("a", (contact,)), Island("b", (endpoint,))),
            (segment((8, 1), (7, 2)),),
            frozenset((old_pad, endpoint)),
        )
        budget = Budget(Limits(seconds=30, path_candidates=1, smoothing_trials=0))
        backend = synthetic_backend(
            net, budget, step_mm=0.25, forbid_staircase=True, first_feasible=True
        )
        model = backend.settings[net.name].router((), frozenset(), budget)
        with patch.object(
            backend, "alternative_headings", wraps=backend.alternative_headings
        ) as retry:
            result, _ = backend._path_with_context(
                model, net, EMPTY_ROUTE, contact, endpoint
            )
        backend.combine(net, (result,))
        retry.assert_not_called()

    def test_context_rejection_retries_a_different_endpoint_direction(self):
        from pcb.net_routing.geometry import StaircaseGeometryError
        from pcb.route_optimizer import NoGridRoute, SearchLimitReached

        for error in (
            NoGridRoute("exhausted"),
            SearchLimitReached("limited", expanded=3),
        ):
            with self.subTest(error=type(error).__name__):
                net = make_net("SIGNAL", [(0, 0), (2, 1)])
                budget = Budget(Limits(seconds=30, smoothing_trials=0))
                backend = synthetic_backend(net, budget, step_mm=0.25)
                model = backend.settings[net.name].router((), frozenset(), budget)
                rejected = route(segment((0, 0), (1, 0)), segment((1, 0), (2, 1)))
                accepted = route(segment((0, 0), (1, 1)), segment((1, 1), (2, 1)))

                def combined(_net, routes):
                    if routes[-1] is rejected:
                        raise StaircaseGeometryError("context rejected")
                    return accepted

                def search(router, _net, _tree, _start, _end, *, context_check):
                    if (
                        router.initial_directions is None
                        and router.final_directions is None
                    ):
                        self.assertFalse(context_check(rejected))
                        raise error
                    self.assertIsNotNone(router.initial_directions)
                    self.assertNotIn(0, router.initial_directions)
                    return accepted, False

                with (
                    patch.object(backend, "combine", side_effect=combined),
                    patch.object(backend, "_path", side_effect=search) as searched,
                ):
                    result, limited = backend._path_with_context(
                        model, net, EMPTY_ROUTE, site(0, 0), site(2, 1)
                    )
                self.assertEqual(result.segments, accepted.segments)
                self.assertEqual(
                    result.expanded, 3 if isinstance(error, SearchLimitReached) else 0
                )
                self.assertEqual(searched.call_count, 2)
                self.assertEqual(limited, isinstance(error, SearchLimitReached))
                self.assertIsNone(model.initial_directions)
                self.assertIsNone(model.final_directions)

    def test_context_retry_can_diverge_inside_a_shared_endpoint_corridor(self):
        from pcb.net_routing.geometry import StaircaseGeometryError

        prefix = [(0, 0), (1, 0), (2, 0)]
        lower = [(2, 0), (3, 0), (4, 0), (5, 0), (6, 0)]
        upper = [(2, 0), (3, 1), (4, 1), (5, 1), (6, 0)]
        tail = [(6, 0), (7, 0), (8, 0), (9, 1), (10, 1), (11, 1), (12, 1)]
        edges = {
            frozenset((site(*a).point, site(*b).point))
            for path in (prefix, lower, upper, tail)
            for a, b in zip(path, path[1:])
        }
        net = make_net("SIGNAL", [(0, 0), (12, 1)])
        budget = Budget(Limits(seconds=20, path_calls=20, smoothing_trials=0))
        backend = synthetic_backend(
            net,
            budget,
            bounds_mm=[-2, -2, 15, 5],
            gentle_turns=True,
            forbid_staircase=True,
            max_expansions=5000,
        )
        model = backend.settings[net.name].router((), frozenset(), budget)

        def compatible(_net, routes):
            candidate = routes[-1]
            if any(on_segment(site(3, 1).point, item) for item in candidate.segments):
                return candidate
            raise StaircaseGeometryError(
                "The lower corridor conflicts with existing copper"
            )

        with (
            patch.object(
                model,
                "edge_clear",
                side_effect=lambda a, b, _layer: frozenset((a, b)) in edges,
            ) as original_clearance,
            patch.object(backend, "combine", side_effect=compatible),
        ):
            result, _ = backend._path_with_context(
                model, net, EMPTY_ROUTE, site(0, 0), site(12, 1)
            )
            self.assertTrue(
                any(on_segment(site(3, 1).point, item) for item in result.segments)
            )
            self.assertFalse(model._has_staircase(list(result.segments)))
            self.assertIs(model.edge_clear, original_clearance)

    def test_context_detour_restores_clearance_after_unexpected_error(self):
        from pcb.net_routing.geometry import StaircaseGeometryError
        from pcb.route_optimizer import NoGridRoute, SearchLimitReached

        net = make_net("SIGNAL", [(0, 0), (4, 1)])
        budget = Budget(Limits(seconds=20, path_calls=20, smoothing_trials=0))
        backend = synthetic_backend(net, budget)
        model = backend.settings[net.name].router((), frozenset(), budget)
        rejected = route(segment((0, 0), (3, 0)), segment((3, 0), (4, 1)))

        def search(router, _net, _tree, _start, _end, *, context_check):
            if "edge_clear" in vars(router):
                raise ValueError("unexpected detour failure")
            if router.initial_directions is None and router.final_directions is None:
                self.assertFalse(context_check(rejected))
                raise SearchLimitReached("context rejected", expanded=3)
            raise NoGridRoute("heading unavailable")

        with (
            patch.object(
                backend, "combine", side_effect=StaircaseGeometryError("context")
            ),
            patch.object(backend, "_path", side_effect=search),
        ):
            with self.assertRaisesRegex(ValueError, "unexpected detour failure"):
                backend._path_with_context(
                    model, net, EMPTY_ROUTE, site(0, 0), site(4, 1)
                )
        self.assertNotIn("edge_clear", vars(model))
        self.assertIsNone(model.initial_directions)
        self.assertIsNone(model.final_directions)

    def test_context_detour_starts_before_rejections_exhaust_search_time(self):
        from pcb.net_routing.geometry import StaircaseGeometryError

        net = make_net("SIGNAL", [(0, 0), (4, 1)])
        budget = Budget(Limits(seconds=20, path_calls=20, smoothing_trials=0))
        backend = synthetic_backend(net, budget)
        model = backend.settings[net.name].router((), frozenset(), budget)
        rejected = (
            route(
                segment((0, 0), (1, 0)),
                segment((1, 0), (2, 1)),
                segment((2, 1), (4, 1)),
            ),
            route(
                segment((0, 0), (2, 0)),
                segment((2, 0), (3, 1)),
                segment((3, 1), (4, 1)),
            ),
            route(segment((0, 0), (1, 1)), segment((1, 1), (4, 1))),
        )
        accepted = route(segment((0, 0), (3, 0)), segment((3, 0), (4, 1)))

        def combine_candidate(_net, routes):
            if routes[-1] is accepted:
                return accepted
            raise StaircaseGeometryError("context")

        def search(router, _net, _tree, _start, _end, *, context_check):
            if "edge_clear" in vars(router):
                self.assertTrue(context_check(accepted))
                return accepted, False
            for candidate in rejected:
                self.assertFalse(context_check(candidate))
            self.fail("The router spent the entire search without trying a detour")

        with (
            patch.object(backend, "combine", side_effect=combine_candidate),
            patch.object(backend, "_path", side_effect=search),
        ):
            result, limited = backend._path_with_context(
                model, net, EMPTY_ROUTE, site(0, 0), site(4, 1)
            )
        self.assertEqual(result.segments, accepted.segments)
        self.assertTrue(limited)
        self.assertNotIn("edge_clear", vars(model))

    def test_unsuccessful_context_detours_resume_the_unrestricted_search(self):
        from pcb.net_routing.geometry import StaircaseGeometryError
        from pcb.route_optimizer import NoGridRoute

        net = make_net("SIGNAL", [(0, 0), (4, 1)])
        budget = Budget(Limits(seconds=20, path_calls=20, smoothing_trials=0))
        backend = synthetic_backend(net, budget)
        model = backend.settings[net.name].router((), frozenset(), budget)
        rejected = (
            route(
                segment((0, 0), (1, 0)),
                segment((1, 0), (2, 1)),
                segment((2, 1), (4, 1)),
            ),
            route(
                segment((0, 0), (2, 0)),
                segment((2, 0), (3, 1)),
                segment((3, 1), (4, 1)),
            ),
            route(segment((0, 0), (1, 1)), segment((1, 1), (4, 1))),
        )
        accepted = route(segment((0, 0), (3, 0)), segment((3, 0), (4, 1)))
        unrestricted = []

        def combine_candidate(_net, routes):
            if routes[-1] is accepted:
                return accepted
            raise StaircaseGeometryError("context")

        def search(router, _net, _tree, _start, _end, *, context_check):
            if "edge_clear" in vars(router):
                raise NoGridRoute("this detour is blocked")
            unrestricted.append(None)
            if len(unrestricted) == 1:
                for candidate in rejected:
                    context_check(candidate)
                self.fail("The router did not try its detours")
            self.assertTrue(context_check(accepted))
            return accepted, False

        with (
            patch.object(backend, "combine", side_effect=combine_candidate),
            patch.object(backend, "_path", side_effect=search),
        ):
            result, _ = backend._path_with_context(
                model, net, EMPTY_ROUTE, site(0, 0), site(4, 1)
            )
        self.assertEqual(result.segments, accepted.segments)
        self.assertEqual(len(unrestricted), 2)
        self.assertNotIn("edge_clear", vars(model))

    def test_failed_context_detours_try_other_headings_before_full_retry(self):
        from pcb.net_routing.geometry import StaircaseGeometryError
        from pcb.route_optimizer import NoGridRoute

        net = make_net("SIGNAL", [(0, 0), (4, 1)])
        budget = Budget(Limits(seconds=20, path_calls=20, smoothing_trials=0))
        backend = synthetic_backend(net, budget)
        model = backend.settings[net.name].router((), frozenset(), budget)
        rejected = (
            route(
                segment((0, 0), (1, 0)),
                segment((1, 0), (2, 1)),
                segment((2, 1), (4, 1)),
            ),
            route(
                segment((0, 0), (2, 0)),
                segment((2, 0), (3, 1)),
                segment((3, 1), (4, 1)),
            ),
            route(segment((0, 0), (1, 1)), segment((1, 1), (4, 1))),
        )
        alternate = route(segment((0, 0), (1, 1)), segment((1, 1), (4, 1)))
        fallback = route(segment((0, 0), (3, 0)), segment((3, 0), (4, 1)))
        calls = []

        def combine_candidate(_net, routes):
            candidate = routes[-1]
            if candidate is alternate or candidate is fallback:
                return candidate
            raise StaircaseGeometryError("context")

        def search(router, _net, _tree, _start, _end, *, context_check):
            if "edge_clear" in vars(router):
                raise NoGridRoute("detour blocked")
            if router.initial_directions is None and router.final_directions is None:
                calls.append("unrestricted")
                if calls.count("unrestricted") == 1:
                    for candidate in rejected:
                        context_check(candidate)
                    self.fail("The router did not try its detours")
                return fallback, False
            calls.append("alternate")
            return alternate, False

        with (
            patch.object(backend, "combine", side_effect=combine_candidate),
            patch.object(backend, "_path", side_effect=search),
        ):
            result, limited = backend._path_with_context(
                model, net, EMPTY_ROUTE, site(0, 0), site(4, 1)
            )
        self.assertEqual(result.segments, alternate.segments)
        self.assertEqual(calls, ["unrestricted", "alternate"])
        self.assertTrue(limited)
        self.assertIsNone(model.initial_directions)
        self.assertIsNone(model.final_directions)

    def test_context_heading_alternatives_preserve_unrestricted_fallback(self):
        from pcb.net_routing.geometry import StaircaseGeometryError
        from pcb.route_optimizer import NoGridRoute

        net = make_net("SIGNAL", [(0, 0), (4, 1)])
        budget = Budget(Limits(seconds=20, path_calls=30, smoothing_trials=0))
        backend = synthetic_backend(net, budget)
        model = backend.settings[net.name].router((), frozenset(), budget)
        rejected = (
            route(
                segment((0, 0), (1, 0)),
                segment((1, 0), (2, 1)),
                segment((2, 1), (4, 1)),
            ),
            route(
                segment((0, 0), (2, 0)),
                segment((2, 0), (3, 1)),
                segment((3, 1), (4, 1)),
            ),
            route(segment((0, 0), (1, 1)), segment((1, 1), (4, 1))),
        )
        accepted = route(segment((0, 0), (3, 0)), segment((3, 0), (4, 1)))
        calls = []

        def combine_candidate(_net, routes):
            if routes[-1] is accepted:
                return accepted
            raise StaircaseGeometryError("context")

        def search(router, _net, _tree, _start, _end, *, context_check):
            if "edge_clear" in vars(router):
                raise NoGridRoute("detour blocked")
            if (
                router.initial_directions is not None
                or router.final_directions is not None
            ):
                calls.append("alternate")
                raise NoGridRoute("heading blocked")
            calls.append("unrestricted")
            if calls.count("unrestricted") == 1:
                for candidate in rejected:
                    context_check(candidate)
                self.fail("The router did not try its detours")
            return accepted, False

        with (
            patch.object(backend, "combine", side_effect=combine_candidate),
            patch.object(backend, "_path", side_effect=search),
        ):
            result, limited = backend._path_with_context(
                model, net, EMPTY_ROUTE, site(0, 0), site(4, 1)
            )
        self.assertEqual(result.segments, accepted.segments)
        self.assertEqual(calls.count("unrestricted"), 2)
        self.assertIn("alternate", calls)
        self.assertEqual(calls[-1], "unrestricted")
        self.assertTrue(limited)

    def test_refinement_rejection_does_not_discard_an_accepted_context_path(self):
        from pcb.net_routing.geometry import StaircaseGeometryError

        net = make_net("SIGNAL", [(0, 0), (4, 1)])
        budget = Budget(Limits(seconds=20, path_calls=20, smoothing_trials=0))
        backend = synthetic_backend(net, budget)
        model = backend.settings[net.name].router((), frozenset(), budget)
        rejected = (
            route(
                segment((0, 0), (1, 0)),
                segment((1, 0), (2, 1)),
                segment((2, 1), (4, 1)),
            ),
            route(
                segment((0, 0), (2, 0)),
                segment((2, 0), (3, 1)),
                segment((3, 1), (4, 1)),
            ),
            route(segment((0, 0), (1, 1)), segment((1, 1), (4, 1))),
        )
        accepted = route(segment((0, 0), (3, 0)), segment((3, 0), (4, 1)))

        def combine_candidate(_net, routes):
            if routes[-1] is accepted:
                return accepted
            raise StaircaseGeometryError("context")

        def search(router, _net, _tree, _start, _end, *, context_check):
            self.assertFalse(context_check(rejected[0]))
            self.assertFalse(context_check(rejected[1]))
            self.assertTrue(context_check(accepted))
            self.assertFalse(context_check(rejected[2]))
            return accepted, False

        with (
            patch.object(backend, "combine", side_effect=combine_candidate),
            patch.object(backend, "_path", side_effect=search) as searched,
        ):
            result, _ = backend._path_with_context(
                model, net, EMPTY_ROUTE, site(0, 0), site(4, 1)
            )
        self.assertEqual(result.segments, accepted.segments)
        self.assertEqual(searched.call_count, 1)

    def test_context_detour_does_not_sample_non_octilinear_segments(self):
        invalid = route(segment((0, 0), (3, 1)))
        self.assertEqual(NativeBackend.context_detour_points((invalid,)), ())

    def test_context_refinement_keeps_valid_original_and_restores_headings(self):
        net = make_net("SIGNAL", [(0, 0), (1, 0)])
        budget = Budget(Limits(seconds=30, smoothing_trials=2))
        backend = synthetic_backend(net, budget, step_mm=0.25)
        model = backend.settings[net.name].router((), frozenset(), budget)
        model.initial_directions, model.final_directions = (
            frozenset({0}),
            frozenset({0}),
        )
        original = route(segment((0, 0), (1, 0)))
        invalid_refinement = route(segment((0, 0), (1, 0)))
        with (
            patch.object(model, "route", return_value=original),
            patch(
                "pcb.net_routing.backend.simplify_route",
                return_value=invalid_refinement,
            ),
        ):
            result, limited = backend._path(
                model,
                net,
                EMPTY_ROUTE,
                site(0, 0),
                site(1, 0),
                context_check=lambda candidate: candidate is original,
            )
        self.assertIs(result, original)
        self.assertFalse(limited)
        self.assertEqual(
            (model.initial_directions, model.final_directions),
            (frozenset({0}), frozenset({0})),
        )

    def test_context_search_preserves_an_endpoint_access_failure(self):
        from pcb.route_optimizer import NoGridRoute

        net = make_net("SIGNAL", [(0, 0), (1, 0)])
        budget = Budget(Limits(seconds=30))
        backend = synthetic_backend(net, budget)
        router = backend.settings["SIGNAL"].router((), frozenset(), budget)
        with patch.object(
            backend, "_path", side_effect=NoGridRoute("endpoint access blocked")
        ):
            with self.assertRaisesRegex(NoGridRoute, "endpoint access blocked"):
                backend._path_with_context(
                    router, net, EMPTY_ROUTE, site(0, 0), site(1, 0)
                )

    def test_direct_via_to_a_plane_site_is_a_checked_route_candidate(self):
        pad = site(0, 0, "B.Cu")
        virtual = site(1.2, 0, "In1.Cu")
        physical = site(5, 0, "In1.Cu")
        net = Net(
            "SIGNAL",
            (
                Island("pad", (pad,)),
                Island(
                    "plane", (physical, virtual), zone_anchors=((virtual, physical),)
                ),
            ),
            pad_sites=frozenset((pad,)),
        )
        budget = Budget(Limits(seconds=30, path_candidates=1, smoothing_trials=0))
        backend = synthetic_backend(
            net, budget, layers=["B.Cu", "In1.Cu"], step_mm=0.25
        )
        with patch.object(
            backend,
            "_path_with_context",
            side_effect=AssertionError("grid fallback was unnecessary"),
        ):
            answers = backend.paths(net, (), EMPTY_ROUTE, pad, virtual)
        self.assertTrue(answers)
        self.assertEqual(answers[0].vias[0].at, virtual.point)
        self.assertTrue(all(s.layer == "B.Cu" for s in answers[0].segments))
        backend.combine(net, (answers[0],))

    def test_direct_plane_via_checks_obstacles_on_every_copper_layer(self):
        from pcb.route_optimizer import NoGridRoute, Obstacle

        pad = site(0, 0, "B.Cu")
        virtual = site(1.2, 0, "In1.Cu")
        physical = site(5, 0, "In1.Cu")
        net = Net(
            "SIGNAL",
            (
                Island("pad", (pad,)),
                Island(
                    "plane", (physical, virtual), zone_anchors=((virtual, physical),)
                ),
            ),
            pad_sites=frozenset((pad,)),
        )
        budget = Budget(Limits(seconds=30, path_candidates=1, smoothing_trials=0))
        backend = synthetic_backend(
            net, budget, layers=["B.Cu", "In1.Cu"], step_mm=0.25
        )
        backend.obstacles = (
            Obstacle(
                Point.from_mm([1.1, -0.1]),
                Point.from_mm([1.3, 0.1]),
                ("F.Cu",),
                "OTHER",
                "pad",
            ),
        )
        with patch.object(
            backend, "_path_with_context", side_effect=NoGridRoute("blocked")
        ):
            self.assertFalse(backend.paths(net, (), EMPTY_ROUTE, pad, virtual))

    def test_plane_search_prefers_a_clear_via_over_a_nearer_blocked_one(self):
        from pcb.route_optimizer import NoGridRoute, Obstacle

        pad = site(0, 0, "B.Cu")
        blocked = site(0.2, 0, "In1.Cu")
        clear = site(1.2, 0, "In1.Cu")
        physical = site(5, 0, "In1.Cu")
        net = Net(
            "SIGNAL",
            (
                Island("pad", (pad,)),
                Island(
                    "plane",
                    (blocked, clear, physical),
                    zone_anchors=((blocked, physical), (clear, physical)),
                ),
            ),
            pad_sites=frozenset((pad,)),
        )
        budget = Budget(
            Limits(
                seconds=30,
                tree_beam=1,
                connections_per_step=1,
                fallback_connections_per_step=1,
                path_candidates=1,
                smoothing_trials=0,
            )
        )
        backend = synthetic_backend(
            net, budget, layers=["B.Cu", "In1.Cu"], step_mm=0.25
        )
        backend.obstacles = (
            Obstacle(
                Point.from_mm([0.15, -0.05]),
                Point.from_mm([0.25, 0.05]),
                ("F.Cu",),
                "OTHER",
                "pad",
            ),
        )
        with patch.object(
            backend, "_path_with_context", side_effect=NoGridRoute("blocked")
        ):
            plans = whole_net(net, (), backend, budget)
        self.assertTrue(plans)
        self.assertEqual(plans[0].route.vias[0].at, clear.point)

    def test_search_prefers_an_in_bounds_contact_over_a_nearer_unreachable_one(self):
        start = site(1.6, 0)
        outside = site(1.9, 0)
        inside = site(0, 0)
        net = Net(
            "SIGNAL",
            (Island("start", (start,)), Island("end", (outside, inside))),
            pad_sites=frozenset((start, inside)),
        )
        budget = Budget(
            Limits(
                seconds=30,
                tree_beam=1,
                connections_per_step=1,
                fallback_connections_per_step=1,
                path_candidates=1,
                smoothing_trials=0,
            )
        )
        backend = synthetic_backend(net, budget, step_mm=0.25, bounds_mm=[-2, -2, 2, 2])
        plans = whole_net(net, (), backend, budget)
        self.assertTrue(plans)
        self.assertTrue(
            any(
                segment.end == inside.point or segment.start == inside.point
                for segment in plans[0].route.segments
            )
        )

    def test_direct_plane_via_respects_configured_extra_keepout_on_other_layer(self):
        from pcb.route_optimizer import NoGridRoute, Obstacle

        pad = site(0, 0, "B.Cu")
        virtual = site(1.2, 0, "In1.Cu")
        physical = site(5, 0, "In1.Cu")
        net = Net(
            "SIGNAL",
            (
                Island("pad", (pad,)),
                Island(
                    "plane", (physical, virtual), zone_anchors=((virtual, physical),)
                ),
            ),
            pad_sites=frozenset((pad,)),
        )
        budget = Budget(Limits(seconds=30, path_candidates=1, smoothing_trials=0))
        backend = synthetic_backend(
            net, budget, layers=["B.Cu", "In1.Cu"], step_mm=0.25
        )
        backend.settings[net.name].extra = (
            Obstacle(
                Point.from_mm([1.1, -0.1]),
                Point.from_mm([1.3, 0.1]),
                ("F.Cu",),
                "",
                "keepout",
            ),
        )
        with patch.object(
            backend, "_path_with_context", side_effect=NoGridRoute("blocked")
        ):
            self.assertFalse(backend.paths(net, (), EMPTY_ROUTE, pad, virtual))

    def test_shared_copper_is_charged_once_and_collinear_joints_removed(self):
        answer = combine(route(segment((0, 0), (6, 0))), route(segment((2, 0), (8, 0))))
        self.assertEqual(answer.length_mm, 8)
        self.assertEqual(len(answer.segments), 1)
        self.assertEqual(answer.bends, 0)

    def test_already_existing_copper_is_subtracted(self):
        result = unique_segments(
            (segment((0, 0), (10, 0)),),
            (segment((2, 0), (4, 0)), segment((6, 0), (8, 0))),
        )
        self.assertEqual(
            result,
            (
                segment((0, 0), (2, 0)),
                segment((4, 0), (6, 0)),
                segment((8, 0), (10, 0)),
            ),
        )

    def test_through_via_is_one_physical_item_across_multiple_layer_hops(self):
        point = site(2, 2).point
        answer = combine(
            route(vias=(Via(point, "F.Cu", "In1.Cu"), Via(point, "In1.Cu", "B.Cu")))
        )
        self.assertEqual(len(answer.vias), 1)
        self.assertEqual(answer.cost, 10)

    def test_right_angle_bend_rejected_but_branch_junctions_are_allowed(self):
        with self.assertRaisesRegex(RouteGeometryError, "45-degree"):
            combine(route(segment((0, 0), (4, 0)), segment((4, 0), (4, 4))))
        tee = combine(route(segment((0, 0), (8, 0)), segment((4, 0), (4, 4))))
        self.assertEqual(tee.bends, 0)
        self.assertTrue(any(len(v) == 3 for v in vertices(tee.segments).values()))
        answer = combine(route(segment((0, 0), (8, 0)), segment((4, 0), (7, 3))))
        self.assertTrue(any(len(v) == 3 for v in vertices(answer.segments).values()))

    def test_native_contact_angle_is_checked(self):
        with self.assertRaisesRegex(ValueError, "45-degree"):
            combine(route(segment((4, 0), (4, 4))), existing=(segment((0, 0), (4, 0)),))
        tee = combine(
            route(segment((4, 0), (4, 4))), existing=(segment((0, 0), (8, 0)),)
        )
        self.assertEqual(tee.bends, 0)

    def test_pad_connections_are_not_trace_bends(self):
        answer = combine(
            route(segment((0, 0), (4, 0)), segment((4, 0), (4, 4))),
            pad_sites=frozenset({site(4, 0)}),
        )
        self.assertEqual(answer.length_mm, 8)
        self.assertEqual(answer.bends, 0)

    def test_staircases_formed_by_separate_routes_rejected(self):
        with self.assertRaisesRegex(ValueError, "staircase"):
            combine(
                route(
                    segment((0, 0), (1, 1)),
                    segment((1, 1), (2, 1)),
                    segment((2, 1), (3, 2)),
                )
            )

    def test_branch_sampler_includes_interior_45_projection(self):
        points = branch_sites((segment((0, 0), (8, 0)),), (site(6, 4),), 20)
        self.assertIn(site(2, 0), points)
        self.assertIn(site(6, 0), points)

    def test_native_subnanometre_projection_is_skipped(self):
        points = branch_sites(
            (Segment(Point(0, 0), Point(4, 4), "F.Cu"),),
            (Site(Point(1, 0), "F.Cu"),),
            20,
        )
        self.assertTrue(points)

    def test_non45_trace_rejected(self):
        with self.assertRaisesRegex(ValueError, "45-degree"):
            combine(route(segment((0, 0), (3, 2))))


class NetSearchTests(unittest.TestCase):
    def test_search_report_excludes_later_validation_time(self):
        budget = Budget(Limits(seconds=10), started=0)
        with patch("pcb.net_routing.model.time.monotonic", return_value=4):
            budget.finish_search()
        budget.failure("validation_issue")
        with patch("pcb.net_routing.model.time.monotonic", return_value=30):
            report = budget.report()
        self.assertEqual(report["elapsed_seconds"], 4)
        self.assertNotIn("seconds", report["limits_hit"])
        self.assertEqual(report["failures"], {"validation_issue": 1})

    def test_search_report_marks_elapsed_deadline_even_without_a_later_check(self):
        budget = Budget(Limits(seconds=10), started=0)
        with patch("pcb.net_routing.model.time.monotonic", return_value=10.25):
            report = budget.report()
        self.assertEqual(report["elapsed_seconds"], 10.25)
        self.assertIn("seconds", report["limits_hit"])

    def test_refines_complete_unbranched_route_to_existing_copper(self):
        start, end, pad = site(1, 1), site(8, 1), site(9, 1)
        net = Net(
            "A",
            (Island("a", (start,)), Island("b", (pad,))),
            (segment((8, 1), (9, 1)),),
            frozenset((start, pad)),
        )
        backend = synthetic_backend(net, Budget(Limits(seconds=30)))
        candidate = backend.combine(
            net,
            (
                route(
                    segment((1, 1), (3, 1)),
                    segment((3, 1), (4, 2)),
                    segment((4, 2), (5, 2)),
                    segment((5, 2), (6, 1)),
                    segment((6, 1), (8, 1)),
                ),
            ),
        )
        result = backend.refine_complete(net, (), candidate)
        self.assertLess(result.length_mm, candidate.length_mm)
        self.assertLess(result.bends, candidate.bends)
        self.assertEqual(result.segments, (segment((1, 1), (8, 1)),))
        self.assertTrue(any(s.end == end.point for s in result.segments))

    def test_refinement_keeps_branched_routes_unchanged(self):
        net = make_net("A", [(1, 1), (5, 1), (3, 3)])
        backend = synthetic_backend(net, Budget(Limits(seconds=30)))
        candidate = route(
            segment((1, 1), (3, 1)),
            segment((3, 1), (5, 1)),
            segment((3, 1), (3, 3)),
        )
        self.assertIs(backend.refine_complete(net, (), candidate), candidate)

    def test_refines_detour_in_one_branch_without_losing_other_terminals(self):
        net = make_net("A", [(1, 4), (10, 4), (9, 1)])
        backend = synthetic_backend(net, Budget(Limits(seconds=30)))
        candidate = backend.combine(
            net,
            (
                route(
                    segment((1, 4), (3, 4)),
                    segment((3, 4), (4, 5)),
                    segment((4, 5), (5, 5)),
                    segment((5, 5), (6, 4)),
                    segment((6, 4), (10, 4)),
                    segment((6, 4), (9, 1)),
                ),
            ),
        )
        result = backend.refine_complete(net, (), candidate)
        self.assertLess(result.cost, candidate.cost)
        self.assertLess(result.length_mm, candidate.length_mm)
        for pad in net.pad_sites:
            self.assertTrue(
                any(
                    s.layer == pad.layer and on_segment(pad.point, s)
                    for s in result.segments
                ),
                pad,
            )
        self.assertTrue(any(on_segment(site(6, 4).point, s) for s in result.segments))
        self.assertEqual(result.vias, candidate.vias)

    def test_branched_refinement_preserves_intermediate_pad_and_existing_contact(self):
        start, intermediate, old_contact = site(1, 4), site(4, 5), site(5, 5)
        net = Net(
            "A",
            tuple(
                Island(str(i), (s,))
                for i, s in enumerate((start, site(10, 4), site(9, 1)))
            ),
            (segment((5, 5), (4, 6)),),
            frozenset((start, intermediate, site(10, 4), site(9, 1))),
        )
        backend = synthetic_backend(net, Budget(Limits(seconds=30)))
        candidate = backend.combine(
            net,
            (
                route(
                    segment((1, 4), (3, 4)),
                    segment((3, 4), (4, 5)),
                    segment((4, 5), (5, 5)),
                    segment((5, 5), (6, 4)),
                    segment((6, 4), (10, 4)),
                    segment((6, 4), (9, 1)),
                ),
            ),
        )
        result = backend.refine_complete(net, (), candidate)
        for anchor in (intermediate, old_contact, *net.pad_sites):
            self.assertTrue(
                any(
                    s.layer == anchor.layer and on_segment(anchor.point, s)
                    for s in result.segments
                ),
                anchor,
            )

    def test_workflow_keeps_greedy_candidate_as_fallback_after_terminal_refinement(
        self,
    ):
        net = make_net("A", [(1, 4), (10, 4), (9, 1)])
        budget = Budget(Limits(seconds=30))
        backend = synthetic_backend(net, budget)
        candidate = backend.combine(
            net,
            (
                route(
                    segment((1, 4), (3, 4)),
                    segment((3, 4), (4, 5)),
                    segment((4, 5), (5, 5)),
                    segment((5, 5), (6, 4)),
                    segment((6, 4), (10, 4)),
                    segment((6, 4), (9, 1)),
                ),
            ),
        )
        original = NetPlan("A", candidate)
        with patch("pcb.net_routing.workflow.whole_net", return_value=(original,)):
            answers = _solve_with_terminal_refinement(net, (), backend, budget)
        self.assertEqual(len(answers), 2)
        self.assertLess(answers[0].route.cost, candidate.cost)
        self.assertEqual(answers[1], original)

    def test_backend_returns_candidate_when_the_time_budget_expires_after_it_is_found(
        self,
    ):
        net = make_net("A", [(0.15, 0.2), (9.15, 8.2)])
        budget = Budget(
            Limits(seconds=1, path_candidates=1, smoothing_trials=1), started=0
        )
        backend = synthetic_backend(
            net,
            budget,
            weights={"length": 1, "bend": 1, "via": 10, "diagonal": 1},
        )
        clock = iter((0, 0, 2))
        with patch(
            "pcb.net_routing.model.time.monotonic", side_effect=lambda: next(clock, 2)
        ):
            result = backend.paths(
                net, (), EMPTY_ROUTE, net.islands[0].sites[0], net.islands[1].sites[0]
            )
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].search_truncated)
        self.assertIn("seconds", budget.limits_hit)

    def test_smoothing_timeout_keeps_the_route_found_before_it(self):
        net = make_net("A", [(0, 0), (8, 4)])
        budget = Budget(Limits(seconds=30, path_candidates=1, smoothing_trials=1))
        backend = synthetic_backend(net, budget)
        with patch(
            "pcb.net_routing.backend.simplify_route", side_effect=BudgetExhausted
        ):
            result = backend.paths(
                net, (), EMPTY_ROUTE, net.islands[0].sites[0], net.islands[1].sites[0]
            )
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].segments)

    def test_time_limit_preserves_an_existing_off_grid_candidate(self):
        def stop():
            raise BudgetExhausted("Search time budget exhausted")

        model = route_tests.router(
            layers=("B.Cu",),
            weights=Weights(length=1, bend=1, via=10, diagonal=1),
            check_budget=stop,
        )
        try:
            result = model.route(
                site(0.15, 0.2, "B.Cu").point,
                "B.Cu",
                site(9.15, 8.2, "B.Cu").point,
                "B.Cu",
            )
        except BudgetExhausted:
            self.fail("A candidate found before the deadline was discarded")
        self.assertTrue(result.search_truncated)
        self.assertEqual(result.segments[0].start, site(0.15, 0.2, "B.Cu").point)
        self.assertEqual(result.segments[-1].end, site(9.15, 8.2, "B.Cu").point)

    def test_path_search_checks_the_time_budget_while_expanding(self):
        net = make_net("A", [(0, 0), (10, 8)])
        budget = Budget(Limits(seconds=1, smoothing_trials=0), started=0)
        backend = synthetic_backend(net, budget, step_mm=0.1)
        clock = iter((0, 0, 0, 2))

        with patch(
            "pcb.net_routing.model.time.monotonic", side_effect=lambda: next(clock, 2)
        ):
            result = backend.paths(
                net, (), EMPTY_ROUTE, net.islands[0].sites[0], net.islands[1].sites[0]
            )

        self.assertEqual(result, ())
        self.assertIn("seconds", budget.limits_hit)

    def test_native_edge_allowance_is_enforced_before_path_search(self):
        net = make_net("A", [(2, 2), (8, 2)])
        backend = synthetic_backend(
            net, Budget(Limits()), bounds_mm=[0, 0, 10, 8], step_mm=0.5
        )
        s = backend.settings["A"]
        s = Settings(
            s.raw, s.native, s.copper, (site(0, 0).point, site(10, 8).point), mm(0.5)
        )
        router = s.router(())
        self.assertFalse(
            router._inside_point(site(5, 0.5).point, s.clearance + s.width // 2)
        )
        self.assertTrue(
            router._inside_point(site(5, 1).point, s.clearance + s.width // 2)
        )

    def test_later_routing_respects_the_other_nets_larger_clearance(self):
        first = make_net("A", [(0, 0), (8, 0)])
        second = make_net("B", [(0, 1), (8, 1)])
        budget = Budget(Limits(path_candidates=1))
        backend = synthetic_backend(second, budget, step_mm=0.25)
        backend.settings["A"] = synthetic_backend(
            first, budget, clearance_mm=1
        ).settings["A"]
        prior = (NetPlan("A", route(segment((0, 0), (8, 0)))),)
        answers = backend.paths(second, prior, EMPTY_ROUTE, site(0, 1), site(8, 1))
        self.assertFalse(answers)
        self.assertIn("no_path_in_grid", budget.failures)

    def test_five_pads_share_one_short_connection(self):
        net = make_net("PPS", [(0, 0), (2, 0), (4, 0), (6, 0), (8, 0)])
        budget = Budget(Limits(path_candidates=1, path_calls=250, seconds=30))
        answers = whole_net(net, (), synthetic_backend(net, budget), budget)
        self.assertTrue(answers, budget.report())
        self.assertEqual(answers[0].route.length_mm, 8)
        self.assertEqual(len(answers[0].route.segments), 1)

    def test_paths_include_a_distinct_detour_alternative(self):
        net = make_net("A", [(0, 0), (8, 0)])
        budget = Budget(Limits(path_candidates=2, seconds=30))
        backend = synthetic_backend(net, budget)
        paths = backend.paths(net, (), EMPTY_ROUTE, site(0, 0), site(8, 0))
        self.assertEqual(len(paths), 2)
        self.assertGreater(paths[1].length_mm, paths[0].length_mm)
        calls = budget.calls
        self.assertEqual(
            paths, backend.paths(net, (), EMPTY_ROUTE, site(0, 0), site(8, 0))
        )
        self.assertEqual(budget.calls, calls)
        self.assertEqual(budget.cache_hits, 1)

    def test_fixed_order_backtracks_from_cheapest_earlier_choice(self):
        a, b = make_net("A", [(0, 0), (2, 0)]), make_net("B", [(0, 2), (2, 2)])

        def solver(net, prior):
            if net.name == "A":
                return (
                    NetPlan("A", route(segment((0, 0), (1, 0)), cost=1)),
                    NetPlan("A", route(segment((0, 1), (2, 1)), cost=2)),
                )
            return (
                ()
                if prior[0].route.cost == 1
                else (NetPlan("B", route(segment((0, 2), (1, 2)), cost=1)),)
            )

        greedy, _ = search_orders(
            (a, b), Budget(Limits(board_beam=1)), solver, [["A", "B"]]
        )
        lookahead, _ = search_orders(
            (a, b), Budget(Limits(board_beam=2)), solver, [["A", "B"]]
        )
        self.assertFalse(greedy)
        self.assertEqual(lookahead[0].cost, 3)

    def test_smoothing_budget_can_disable_the_cleanup(self):
        net = make_net("A", [(0, 0), (8, 0)])
        budget = Budget(Limits.from_config({"smoothing_trials": 0}))
        backend = synthetic_backend(net, budget)
        with patch(
            "pcb.net_routing.backend.simplify_route",
            side_effect=AssertionError("disabled"),
        ):
            paths = backend.paths(net, (), EMPTY_ROUTE, site(0, 0), site(8, 0))
        self.assertTrue(paths)
        with self.assertRaises(ValueError):
            Limits.from_config({"smoothing_trials": -1})
        with self.assertRaises(ValueError):
            Limits.from_config({"smoothing_trials": True})

    def test_reordering_can_improve_a_completed_board(self):
        nets = (make_net("A", [(0, 0), (1, 0)]), make_net("B", [(0, 2), (1, 2)]))

        def solver(net, prior):
            cost = 50 if net.name == "B" and prior else 1
            y = 0 if net.name == "A" else 2
            return (NetPlan(net.name, route(segment((0, y), (1, y)), cost=cost)),)

        answer, trials = search_orders(nets, Budget(Limits(order_trials=2)), solver)
        self.assertEqual(answer[0].cost, 2)
        self.assertEqual([p.net for p in answer[0].plans], ["B", "A"])
        self.assertEqual(len(trials), 2)
        self.assertEqual([t["best_complete_cost"] for t in trials], [51, 2])

    def test_seeded_orders_repeat_and_explicit_order_is_preserved(self):
        nets = tuple(make_net(n, [(0, 0), (1, 0)]) for n in "ABCD")
        self.assertEqual(
            net_orders(nets, Budget(Limits(seed=9))),
            net_orders(nets, Budget(Limits(seed=9))),
        )
        self.assertEqual(
            net_orders(nets, Budget(Limits()), [["D", "A", "C", "B"]]),
            (("D", "A", "C", "B"),),
        )
        with self.assertRaisesRegex(ValueError, "every requested"):
            net_orders(nets, Budget(Limits()), [["A", "A", "C", "D"]])

    def test_exhausted_search_is_unknown_and_keeps_a_completed_candidate(self):
        net = make_net("A", [(0, 0), (8, 0)])
        budget = Budget(Limits(path_calls=1, path_candidates=1, tree_beam=2))
        answers = whole_net(net, (), synthetic_backend(net, budget), budget)
        self.assertTrue(answers)
        self.assertIn("path_calls", budget.limits_hit)
        self.assertEqual(budget.report()["optimality"], "not_proven")

    def test_existing_single_island_requires_no_new_copper(self):
        net = Net("A", (Island("already", (site(0, 0), site(8, 0))),))
        budget = Budget(Limits())
        answer = whole_net(net, (), synthetic_backend(net, budget), budget)
        self.assertEqual(answer[0].route, EMPTY_ROUTE)
        self.assertEqual(budget.calls, 0)

    def test_connected_net_does_not_build_a_grid_to_refine_an_empty_route(self):
        net = Net("A", (Island("already", (site(0, 0), site(8, 0))),))
        budget = Budget(Limits())
        backend = synthetic_backend(net, budget)
        with patch.object(
            backend.settings["A"], "router", side_effect=AssertionError("unneeded grid")
        ):
            answer = _solve_with_terminal_refinement(net, (), backend, budget)
        self.assertEqual(answer[0].route, EMPTY_ROUTE)
        self.assertEqual(budget.calls, 0)

    def test_refinement_keeps_a_found_route_if_grid_preparation_times_out(self):
        net = make_net("A", [(0, 0), (8, 0)])
        budget = Budget(Limits())
        backend = synthetic_backend(net, budget)
        found = route(segment((0, 0), (8, 0)))
        with patch.object(backend.settings["A"], "router", side_effect=BudgetExhausted):
            refined = backend.refine_complete(net, (), found)
        self.assertEqual(refined, found)

    def test_no_progress_tries_farther_retained_access_sites(self):
        origin = site(0, 0)
        ends = tuple(site(x, 0) for x in range(1, 15))
        net = Net("A", (Island("start", (origin,)), Island("end", ends)))
        budget = Budget(
            Limits(
                tree_beam=1,
                connections_per_step=3,
                fallback_connections_per_step=14,
                path_candidates=1,
            )
        )
        seen = []

        class Backend:
            def paths(self, net, prior, tree, start, end):
                budget.call()
                seen.append(end)
                if end != ends[12]:
                    return ()
                return (route(Segment(start.point, end.point, start.layer)),)

            def combine(self, net, routes):
                return routes[-1]

            def accept(self, net, result):
                return True

        answers = whole_net(net, (), Backend(), budget)
        self.assertTrue(answers)
        self.assertIn(ends[12], seen)
        self.assertLessEqual(len(seen), 14)

    def test_successful_primary_access_search_does_not_expand_the_fallback(self):
        origin = site(0, 0)
        ends = tuple(site(x, 0) for x in range(1, 15))
        net = Net("A", (Island("start", (origin,)), Island("end", ends)))
        budget = Budget(
            Limits(
                tree_beam=1,
                connections_per_step=3,
                fallback_connections_per_step=14,
                path_candidates=1,
            )
        )
        seen = []

        class Backend:
            def paths(self, net, prior, tree, start, end):
                budget.call()
                seen.append(end)
                return (route(Segment(start.point, end.point, start.layer)),)

            def combine(self, net, routes):
                return routes[-1]

            def accept(self, net, result):
                return True

        self.assertTrue(whole_net(net, (), Backend(), budget))
        self.assertEqual(len(seen), 3)

    def test_fallback_access_search_respects_its_configured_limit(self):
        origin = site(0, 0)
        ends = tuple(site(x, 0) for x in range(1, 15))
        net = Net("A", (Island("start", (origin,)), Island("end", ends)))
        budget = Budget(
            Limits(
                tree_beam=1,
                connections_per_step=3,
                fallback_connections_per_step=5,
                path_candidates=1,
            )
        )
        seen = []

        class Backend:
            def paths(self, net, prior, tree, start, end):
                budget.call()
                seen.append(end)
                return ()

        self.assertFalse(whole_net(net, (), Backend(), budget))
        self.assertEqual(len(seen), 5)

    def test_bad_limits_rejected(self):
        for raw in (
            {"board_beam": True},
            {"seconds": float("inf")},
            {"path_calls": 0},
            {"unknown": 1},
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                Limits.from_config(raw)

    def test_existing_explicit_access_limit_stays_a_hard_limit(self):
        old = Limits.from_config({"connections_per_step": 3})
        self.assertEqual(old.fallback_connections_per_step, 3)
        extended = Limits.from_config(
            {
                "connections_per_step": 3,
                "fallback_connections_per_step": 14,
            }
        )
        self.assertEqual(extended.fallback_connections_per_step, 14)

    def test_unknown_configuration_keys_rejected(self):
        with self.assertRaises(ValueError):
            configurations({"nets": [{"name": "A"}], "move_top": True})

    def test_electrical_screen_requires_total_net_current(self):
        net = make_net("A", [(0, 0), (8, 0)])
        with self.assertRaisesRegex(ValueError, "whole_net_upper_bound"):
            synthetic_backend(
                net,
                Budget(Limits()),
                electrical=route_tests.RouteOptimizerTests.electrical(),
            )

    def test_voltage_drop_includes_the_shared_network_and_existing_tracks(self):
        net = make_net("A", [(0, 0), (8, 0)])
        electrical = route_tests.RouteOptimizerTests.electrical()
        backend = synthetic_backend(
            net,
            Budget(Limits()),
            electrical=electrical,
            current_model="whole_net_upper_bound",
        )
        s = backend.settings["A"]
        new = combine(route(segment((0, 0), (8, 0))))
        before = s.assessment(new)["voltage_drop"]["estimated_v"]
        s.native = replace(s.native, tracks=((segment((0, 2), (8, 2)), s.width),))
        after = s.assessment(new)["voltage_drop"]["estimated_v"]
        self.assertAlmostEqual(after, before * 2)


class NativeNetRoutingTests(unittest.TestCase):
    def test_farther_access_candidate_passes_native_connectivity_and_drc(self):
        with tempfile.TemporaryDirectory() as folder:
            source = route_tests.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            trunk = pcbnew.PCB_TRACK(board)
            trunk.SetStart(pcbnew.VECTOR2I(mm(8), mm(5)))
            trunk.SetEnd(pcbnew.VECTOR2I(mm(8), mm(7)))
            trunk.SetWidth(mm(0.2))
            trunk.SetLayer(pcbnew.F_Cu)
            trunk.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            board.Add(trunk)
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()

            start = site(2, 2)
            ends = tuple(site(8, 5 + i * 0.5) for i in range(5))
            net = Net(
                "SIGNAL",
                (Island("start", (start,)), Island("end", ends)),
                existing=(segment((8, 5), (8, 7)),),
                pad_sites=frozenset((start, ends[0])),
            )
            budget = Budget(
                Limits(
                    tree_beam=1,
                    connections_per_step=2,
                    fallback_connections_per_step=5,
                    path_candidates=1,
                    smoothing_trials=0,
                    seconds=30,
                )
            )
            native = synthetic_backend(net, budget, step_mm=0.5)

            class PartlyBlocked:
                def paths(self, net, prior, tree, a, b):
                    if b != ends[-1]:
                        budget.call()
                        return ()
                    return native.paths(net, prior, tree, a, b)

                def combine(self, *args):
                    return native.combine(*args)

                def accept(self, *args):
                    return native.accept(*args)

            candidates = whole_net(net, (), PartlyBlocked(), budget)
            self.assertTrue(candidates)
            changes = campaign_changes(
                candidates[0].route, "SIGNAL", mm(0.2), mm(0.6), mm(0.3)
            )
            contract = RouteContract(
                "SIGNAL", start.point, "F.Cu", ends[0].point, "F.Cu"
            )
            evidence = validate_routes(
                source, hashlib.sha256(before).hexdigest(), changes, [contract]
            )
            self.assertEqual(evidence["status"], "no_new_drc_violations", evidence)
            self.assertEqual(evidence["candidate_unconnected"], 0)
            self.assertEqual(source.read_bytes(), before)

    def test_refined_three_terminal_tree_passes_kicad_on_a_copy(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-branches-") as folder:
            source = route_tests.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            self.add_pad(board, "J3", (8, 2))
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            net = make_net("SIGNAL", [(2, 2), (8, 2), (8, 5)])
            backend = synthetic_backend(net, Budget(Limits(seconds=30)))
            candidate = backend.combine(
                net,
                (
                    route(
                        segment((2, 2), (3, 2)),
                        segment((3, 2), (4, 1)),
                        segment((4, 1), (5, 1)),
                        segment((5, 1), (6, 2)),
                        segment((6, 2), (8, 2)),
                        segment((6, 2), (8, 4)),
                        segment((8, 4), (8, 5)),
                    ),
                ),
            )
            refined = backend.refine_complete(net, (), candidate)
            self.assertLess(refined.cost, candidate.cost)
            changes = campaign_changes(refined, "SIGNAL", mm(0.2), mm(0.6), mm(0.3))
            contracts = [
                RouteContract(
                    "SIGNAL", site(2, 2).point, "F.Cu", site(*target).point, "F.Cu"
                )
                for target in ((8, 2), (8, 5))
            ]
            result = validate_routes(
                source, hashlib.sha256(before).hexdigest(), changes, contracts
            )
            self.assertEqual(result["status"], "no_new_drc_violations", result)
            self.assertEqual(result["candidate_unconnected"], 0)
            self.assertEqual(source.read_bytes(), before)

    def test_existing_zero_length_track_is_retained_as_an_access_point(self):
        with tempfile.TemporaryDirectory() as folder:
            source = route_tests.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            trace = pcbnew.PCB_TRACK(board)
            point = pcbnew.VECTOR2I(mm(2), mm(2))
            trace.SetStart(point)
            trace.SetEnd(point)
            trace.SetWidth(mm(0.2))
            trace.SetLayer(pcbnew.F_Cu)
            trace.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            board.Add(trace)
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            result = plan_nets(self.config(), Path(folder))
            self.assertEqual(result["status"], "geometric_candidate_only", result)
            self.assertEqual(source.read_bytes(), before)

    def test_filled_zones_are_not_credited_and_native_wrappers_remain_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            source = route_tests.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            zone = pcbnew.ZONE(board)
            zone.SetLayer(pcbnew.F_Cu)
            zone.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            outline = zone.Outline()
            outline.NewOutline()
            for x, y in ((0.2, 0.2), (9.8, 0.2), (9.8, 7.8), (0.2, 7.8)):
                outline.Append(mm(x), mm(y))
            board.Add(zone)
            pcbnew.SaveBoard(str(source), board)
            cli = kicad_cli()
            _drc(cli, source, source.parent / "filled.json", save_refill=True)
            before = source.read_bytes()
            raw = pcbnew.LoadBoard(str(source))
            raw.GetConnectivity().Build(raw)
            connected = discover(raw, "SIGNAL", ("F.Cu",), Budget(Limits()))
            self.assertEqual(len(connected.net.islands), 1)
            for _ in range(3):
                loaded = load_board(source)
                found = discover(loaded, "SIGNAL", ("F.Cu",), Budget(Limits()))
                self.assertEqual(len(found.net.islands), 2)
            self.assertEqual(source.read_bytes(), before)

    def test_temporary_native_refill_credits_zone_connectivity_without_saving(self):
        with tempfile.TemporaryDirectory() as folder:
            source = route_tests.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            zone = pcbnew.ZONE(board)
            zone.SetLayer(pcbnew.F_Cu)
            zone.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            outline = zone.Outline()
            outline.NewOutline()
            for x, y in ((0.2, 0.2), (9.8, 0.2), (9.8, 7.8), (0.2, 7.8)):
                outline.Append(mm(x), mm(y))
            board.Add(zone)
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            conservative = load_board(source)
            self.assertEqual(
                len(
                    discover(
                        conservative, "SIGNAL", ("F.Cu",), Budget(Limits())
                    ).net.islands
                ),
                2,
            )
            refilled = load_board(source, refill_zones=True)
            self.assertEqual(
                len(
                    discover(
                        refilled, "SIGNAL", ("F.Cu",), Budget(Limits())
                    ).net.islands
                ),
                1,
            )
            self.assertEqual(source.read_bytes(), before)

    def test_planner_uses_refilled_zone_connectivity_when_requested(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = route_tests.RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            zone = pcbnew.ZONE(board)
            zone.SetLayer(pcbnew.F_Cu)
            zone.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            outline = zone.Outline()
            outline.NewOutline()
            for x, y in ((0.2, 0.2), (9.8, 0.2), (9.8, 7.8), (0.2, 7.8)):
                outline.Append(mm(x), mm(y))
            board.Add(zone)
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            config = self.config()
            config["shared"]["assume_zone_refill"] = True
            result = plan_nets(config, project, validate=False)
            self.assertEqual(result["status"], "already_connected", result)
            self.assertEqual(source.read_bytes(), before)

    @staticmethod
    def make_plane_fixture(project):
        source = route_tests.RouteOptimizerTests.make_board(project)
        board = pcbnew.LoadBoard(str(source))
        board.SetCopperLayerCount(4)
        target = board.FindFootprintByReference("J2")
        target.SetLayer(pcbnew.B_Cu)
        for pad in target.Pads():
            pad.SetLayerSet(pcbnew.LSET.BackMask())
        signal = board.FindNet("SIGNAL")
        trace = pcbnew.PCB_TRACK(board)
        trace.SetStart(pcbnew.VECTOR2I(mm(2), mm(2)))
        trace.SetEnd(pcbnew.VECTOR2I(mm(3), mm(2)))
        trace.SetWidth(mm(0.2))
        trace.SetLayer(pcbnew.F_Cu)
        trace.SetNetCode(signal.GetNetCode())
        board.Add(trace)
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(mm(3), mm(2)))
        via.SetWidth(mm(0.6))
        via.SetDrill(mm(0.3))
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetNetCode(signal.GetNetCode())
        board.Add(via)
        zone = pcbnew.ZONE(board)
        zone.SetLayer(pcbnew.In1_Cu)
        zone.SetNetCode(signal.GetNetCode())
        outline = zone.Outline()
        outline.NewOutline()
        for x, y in ((0.2, 0.2), (9.8, 0.2), (9.8, 7.8), (0.2, 7.8)):
            outline.Append(mm(x), mm(y))
        board.Add(zone)
        pcbnew.SaveBoard(str(source), board)
        return source

    def test_refilled_plane_exposes_local_sites_bound_to_existing_copper(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self.make_plane_fixture(Path(folder))
            before = source.read_bytes()
            filled = load_board(source, refill_zones=True)
            found = discover(
                filled,
                "SIGNAL",
                ("B.Cu", "In1.Cu", "F.Cu"),
                Budget(Limits()),
                include_zone_access=True,
            )
            self.assertEqual(len(found.net.islands), 2)
            plane = next(island for island in found.net.islands if island.zone_anchors)
            self.assertTrue(
                any(
                    virtual.layer == "In1.Cu"
                    and abs(virtual.point.x - mm(8)) <= mm(1.5)
                    and abs(virtual.point.y - mm(5)) <= mm(1.5)
                    and physical.point == site(3, 2).point
                    for virtual, physical in plane.zone_anchors
                )
            )
            self.assertEqual(source.read_bytes(), before)

    def test_plane_samples_skip_via_positions_too_close_to_the_target_pad(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self.make_plane_fixture(Path(folder))
            filled = load_board(source, refill_zones=True)
            found = discover(
                filled,
                "SIGNAL",
                ("B.Cu", "In1.Cu", "F.Cu"),
                Budget(Limits()),
                include_zone_access=True,
                zone_via_margin=mm(0.5),
            )
            pad = list(filled.FindFootprintByReference("J2").Pads())[0]
            plane = next(island for island in found.net.islands if island.zone_anchors)
            self.assertTrue(plane.zone_anchors)
            for virtual, _ in plane.zone_anchors:
                self.assertFalse(
                    pad.HitTest(
                        pcbnew.VECTOR2I(virtual.point.x, virtual.point.y), mm(0.5)
                    )
                )

    def test_plane_sites_never_claim_a_different_disconnected_filled_zone(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self.make_plane_fixture(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            for zone in list(board.Zones()):
                board.Remove(zone)
            signal = board.FindNet("SIGNAL")
            extension = pcbnew.PCB_TRACK(board)
            extension.SetStart(pcbnew.VECTOR2I(mm(3), mm(2)))
            extension.SetEnd(pcbnew.VECTOR2I(mm(4.2), mm(2)))
            extension.SetWidth(mm(0.2))
            extension.SetLayer(pcbnew.In1_Cu)
            extension.SetNetCode(signal.GetNetCode())
            board.Add(extension)
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(pcbnew.VECTOR2I(mm(5.8), mm(2)))
            via.SetWidth(mm(0.6))
            via.SetDrill(mm(0.3))
            via.SetViaType(pcbnew.VIATYPE_THROUGH)
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetNetCode(signal.GetNetCode())
            board.Add(via)
            for left, right in ((0.2, 4.8), (5.2, 9.8)):
                zone = pcbnew.ZONE(board)
                zone.SetLayer(pcbnew.In1_Cu)
                zone.SetNetCode(signal.GetNetCode())
                outline = zone.Outline()
                outline.NewOutline()
                for x, y in ((left, 0.2), (right, 0.2), (right, 7.8), (left, 7.8)):
                    outline.Append(mm(x), mm(y))
                board.Add(zone)
            pcbnew.SaveBoard(str(source), board)
            filled = load_board(source, refill_zones=True)
            found = discover(
                filled,
                "SIGNAL",
                ("B.Cu", "In1.Cu", "F.Cu"),
                Budget(Limits()),
                include_zone_access=True,
                zone_via_margin=mm(0.5),
            )
            self.assertEqual(len(found.net.islands), 3)
            zones = [zone.GetFilledPolysList(pcbnew.In1_Cu) for zone in filled.Zones()]
            used = set()
            for island in found.net.islands:
                for virtual, physical in island.zone_anchors:
                    p = pcbnew.VECTOR2I(virtual.point.x, virtual.point.y)
                    q = pcbnew.VECTOR2I(physical.point.x, physical.point.y)
                    matches = [
                        index
                        for index, polygon in enumerate(zones)
                        if any(
                            polygon.Contains(p, i) and polygon.Contains(q, i)
                            for i in range(polygon.OutlineCount())
                        )
                    ]
                    self.assertEqual(len(matches), 1)
                    used.add(matches[0])
            self.assertEqual(used, {0, 1})

    def test_planner_connects_a_pad_to_an_existing_filled_plane(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = self.make_plane_fixture(project)
            board = pcbnew.LoadBoard(str(source))
            blocker = pcbnew.NETINFO_ITEM(board, "OTHER")
            board.Add(blocker)
            wall = pcbnew.PCB_TRACK(board)
            wall.SetStart(pcbnew.VECTOR2I(mm(5), mm(0.3)))
            wall.SetEnd(pcbnew.VECTOR2I(mm(5), mm(7.7)))
            wall.SetWidth(mm(0.2))
            wall.SetLayer(pcbnew.B_Cu)
            wall.SetNetCode(blocker.GetNetCode())
            board.Add(wall)
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            config = self.config()
            config["shared"].update(
                layers=["B.Cu", "In1.Cu", "F.Cu"],
                assume_zone_refill=True,
                step_mm=0.25,
                max_expansions=100000,
                topology_heuristic=True,
            )
            config.update(max_islands=2)
            result = plan_nets(config, project, validate=True, include_changes=True)
            self.assertEqual(
                result["status"],
                "validated_sampled_candidate",
                {
                    key: result.get(key)
                    for key in (
                        "status",
                        "search",
                        "checkpoints",
                        "rejected_candidates",
                        "trials",
                    )
                },
            )
            self.assertEqual(result["validation"]["candidate_unconnected"], 0)
            self.assertFalse(result["validation"]["broken_existing_connections"])
            self.assertGreater(result["new_via_count"], 0)
            self.assertEqual(source.read_bytes(), before)

    def test_native_validation_rejects_a_route_that_breaks_a_zone_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = route_tests.RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            board.FindFootprintByReference("J2").SetPosition(
                pcbnew.VECTOR2I(mm(8), mm(2))
            )
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            self.add_pad(board, "J3", (5, 0.5), net_name="OTHER")
            self.add_pad(board, "J4", (5, 7.5), net_name="OTHER")
            zone = pcbnew.ZONE(board)
            zone.SetLayer(pcbnew.F_Cu)
            zone.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            outline = zone.Outline()
            outline.NewOutline()
            for x, y in ((0.5, 1), (9.5, 1), (9.5, 3), (0.5, 3)):
                outline.Append(mm(x), mm(y))
            board.Add(zone)
            pcbnew.SaveBoard(str(source), board)
            (project / "v4.1.kicad_sch").write_text("(kicad_sch (lib_symbols))")
            before = source.read_bytes()
            crossing = route(segment((5, 0.5), (5, 7.5)))
            changes = campaign_changes(crossing, "OTHER", mm(0.2), mm(0.6), mm(0.3))
            contract = RouteContract(
                "OTHER", site(5, 0.5).point, "F.Cu", site(5, 7.5).point, "F.Cu"
            )
            evidence = validate_routes(
                source, hashlib.sha256(before).hexdigest(), changes, [contract]
            )
            self.assertNotEqual(evidence["status"], "no_new_drc_violations")
            self.assertTrue(evidence["broken_existing_connections"])
            self.assertEqual(source.read_bytes(), before)

    @staticmethod
    def add_pad(board, reference, at, *, bottom=False, net_name="SIGNAL"):
        footprint = pcbnew.FOOTPRINT(board)
        footprint.SetReference(reference)
        if bottom:
            footprint.SetLayer(pcbnew.B_Cu)
            footprint.Reference().SetLayer(pcbnew.B_SilkS)
            footprint.Value().SetLayer(pcbnew.B_Fab)
        point = pcbnew.VECTOR2I(mm(at[0]), mm(at[1]))
        footprint.SetPosition(point)
        pad = pcbnew.PAD(footprint)
        pad.SetNumber("1")
        pad.SetSize(pcbnew.VECTOR2I(mm(0.5), mm(0.5)))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(pcbnew.LSET.BackMask() if bottom else pcbnew.LSET.FrontMask())
        pad.SetPosition(point)
        pad.SetNetCode(board.FindNet(net_name).GetNetCode())
        footprint.Add(pad)
        board.Add(footprint)
        return footprint

    @staticmethod
    def config():
        original = route_tests.RouteOptimizerTests.config()
        for name in ("net", "start", "end", "board_snapshot"):
            original.pop(name, None)
        original.update(step_mm=0.5, layers=["F.Cu"], max_expansions=4000)
        return {
            "shared": original,
            "nets": [{"name": "SIGNAL"}],
            "search": {
                "path_candidates": 1,
                "tree_beam": 2,
                "path_calls": 40,
                "order_trials": 1,
                "seconds": 30,
            },
        }

    def test_native_plan_and_closing_validation_preserve_all_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = route_tests.RouteOptimizerTests.make_board(project)
            before = source.read_bytes()
            result = plan_nets(
                self.config(), project, validate=True, include_changes=True
            )
            self.assertEqual(result["status"], "validated_sampled_candidate", result)
            self.assertEqual(
                result["validation"]["whole_net_connectivity"],
                "all_requested_pads_connected",
            )
            self.assertEqual(result["nets"][0]["pad_count"], 2)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(result["optimality"], "not_proven")
            self.assertFalse(result["applied"])
            self.assertTrue(
                all(
                    c["target"]["kind"] == "root"
                    for c in result["unvalidated_campaign_changes"]
                )
            )

    def test_native_connectivity_groups_pads_through_a_track_chain(self):
        with tempfile.TemporaryDirectory() as folder:
            source = route_tests.RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            net = board.FindNet("SIGNAL")
            for a, b in (((2, 2), (5, 2)), ((5, 2), (8, 5))):
                track = pcbnew.PCB_TRACK(board)
                track.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1])))
                track.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
                track.SetWidth(mm(0.2))
                track.SetLayer(pcbnew.F_Cu)
                track.SetNetCode(net.GetNetCode())
                board.Add(track)
            pcbnew.SaveBoard(str(source), board)
            loaded = load_board(source)
            found = discover(loaded, "SIGNAL", ("F.Cu",), Budget(Limits()))
            self.assertEqual(len(found.net.islands), 1)
            result = plan_nets(self.config(), Path(folder))
            self.assertEqual(result["status"], "already_connected")
            self.assertEqual(result["search"]["path_calls"], 0)

    def test_five_native_pads_all_connect_in_one_closing_check(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = route_tests.RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            for ref, at in (("J3", (4, 2)), ("J4", (6, 2)), ("J5", (8, 2))):
                self.add_pad(board, ref, at)
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            config = self.config()
            config["search"].update(path_calls=200, net_path_calls=200, tree_beam=4)
            result = plan_nets(config, project, validate=True, details=True)
            self.assertEqual(result["status"], "validated_sampled_candidate", result)
            self.assertEqual(
                result["validation"]["whole_net_pad_counts"], {"SIGNAL": 5}
            )
            self.assertEqual(result["validation"]["routes_validated"], 4)
            self.assertEqual(source.read_bytes(), before)

    def test_crossing_native_nets_are_planned_together_and_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = route_tests.RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            other = pcbnew.NETINFO_ITEM(board, "OTHER")
            board.Add(other)
            self.add_pad(board, "J3", (2, 5), net_name="OTHER")
            self.add_pad(board, "J4", (8, 2), net_name="OTHER")
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            config = self.config()
            config["shared"]["layers"] = ["F.Cu", "B.Cu"]
            config["nets"].append({"name": "OTHER"})
            config["search"].update(
                path_calls=50,
                tree_beam=1,
                connections_per_step=3,
                order_trials=2,
                seconds=40,
            )
            result = plan_nets(config, project, validate=True)
            self.assertEqual(result["status"], "validated_sampled_candidate", result)
            self.assertEqual(
                result["validation"]["whole_net_pad_counts"], {"SIGNAL": 2, "OTHER": 2}
            )
            self.assertEqual(result["validation"]["new_violations"], [])
            self.assertEqual(source.read_bytes(), before)

    def test_sampled_bottom_moves_preserve_top_placement_and_source_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = route_tests.RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            self.add_pad(board, "R50", (5, 4), bottom=True)
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            config = self.config()
            config["shared"]["layers"] = ["F.Cu", "B.Cu"]
            config["search"].update(
                path_calls=50,
                net_path_calls=20,
                seconds=40,
                tree_beam=1,
                connections_per_step=4,
            )
            config["movable"] = [
                {
                    "reference": "R50",
                    "positions_mm": [[5, 4], [5, 6]],
                    "rotations_deg": [0],
                }
            ]
            result = plan_nets(config, project, validate=True, include_changes=True)
            self.assertEqual(result["status"], "validated_sampled_candidate", result)
            self.assertEqual(result["validation"]["footprint_moves"], ["R50"])
            moves = [
                c
                for c in result["unvalidated_campaign_changes"]
                if c["target"]["kind"] == "footprint"
            ]
            self.assertEqual([m["target"]["reference"] for m in moves], ["R50"])
            self.assertEqual(source.read_bytes(), before)

    def test_moving_a_part_requires_all_its_nets_in_the_search(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = route_tests.RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            other = pcbnew.NETINFO_ITEM(board, "OTHER")
            board.Add(other)
            self.add_pad(board, "R50", (5, 4), bottom=True, net_name="OTHER")
            pcbnew.SaveBoard(str(source), board)
            config = self.config()
            config["movable"] = [{"reference": "R50", "positions_mm": [[5, 6]]}]
            with self.assertRaisesRegex(ValueError, "every net"):
                plan_nets(config, project)

    def test_snapshot_detects_dependency_inventory_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = route_tests.RouteOptimizerTests.make_board(project)
            with Snapshot(source) as snapshot:
                rules = source.with_suffix(".kicad_dru")
                rules.write_text("(version 1)", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "changed"):
                    snapshot.check()
                rules.unlink()

    def test_current_limits_gate_campaign_save(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = route_tests.RouteOptimizerTests.make_board(project)
            original = source.read_bytes()
            config_file = project / "input.json"
            config_file.write_text(json.dumps(self.config()), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                code = main(
                    [
                        "--project",
                        folder,
                        "--config",
                        str(config_file),
                        "--validate",
                        "--save-campaign",
                        "fixture",
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn("electrical", errors.getvalue())
            self.assertFalse((project / "config" / "campaigns").exists())
            self.assertEqual(source.read_bytes(), original)

    def test_native_and_electrical_checked_campaign_is_saved_without_applying(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = route_tests.RouteOptimizerTests.make_board(project)
            original = source.read_bytes()
            config = self.config()
            config["shared"].update(
                electrical=route_tests.RouteOptimizerTests.electrical(),
                current_model="whole_net_upper_bound",
            )
            config_file = project / "input.json"
            config_file.write_text(json.dumps(config), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "--project",
                        folder,
                        "--config",
                        str(config_file),
                        "--validate",
                        "--save-campaign",
                        "fixture",
                    ]
                )
            self.assertEqual(code, 0, output.getvalue())
            self.assertIn("campaign", json.loads(output.getvalue()))
            self.assertEqual(
                len(list((project / "config" / "campaigns").glob("*.json"))), 1
            )
            self.assertEqual(source.read_bytes(), original)

    def test_configuration_changes_during_search_discard_result(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            config = project / "input.json"
            config.write_text("{}", encoding="utf-8")

            def changed(*args, **kwargs):
                config.write_text('{"changed":true}', encoding="utf-8")
                return {"status": "geometric_candidate_only"}

            with patch("pcb.net_routing.cli.plan_nets", side_effect=changed):
                with contextlib.redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(
                        main(["--project", folder, "--config", str(config)]), 2
                    )
                self.assertIn("configuration changed", output.getvalue())

    def test_topside_placement_follows_the_placement_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = route_tests.RouteOptimizerTests.make_board(project)
            before = source.read_bytes()
            config = self.config()
            config["movable"] = [{"reference": "J1", "positions_mm": [[2, 3]]}]
            config["placement"] = {"sides": ["bottom"]}
            with self.assertRaisesRegex(ValueError, "top side"):
                plan_nets(config, project)
            del config["placement"]
            self.assertIn("status", plan_nets(config, project))
            self.assertEqual(source.read_bytes(), before)

    def test_source_change_during_planning_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = route_tests.RouteOptimizerTests.make_board(project)
            with Snapshot(source) as snapshot:
                original = source.read_bytes()
                source.write_bytes(original + b"\n")
                with self.assertRaisesRegex(RuntimeError, "changed"):
                    snapshot.check()
                source.write_bytes(original)

    def test_missing_net_and_duplicate_nets_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            route_tests.RouteOptimizerTests.make_board(project)
            config = self.config()
            config["nets"] = [{"name": "DOES_NOT_EXIST"}]
            with self.assertRaisesRegex(ValueError, "no pads|Unknown"):
                plan_nets(config, project)
            config["nets"] = [{"name": "SIGNAL"}, {"name": "SIGNAL"}]
            with self.assertRaisesRegex(ValueError, "distinct"):
                plan_nets(config, project)

    def test_cli_example_and_default_compact_output(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--project", ".", "--example"])
        self.assertEqual(code, 0)
        self.assertIn("nets", json.loads(output.getvalue()))
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = route_tests.RouteOptimizerTests.make_board(project)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            config = project / "config.json"
            config.write_text(json.dumps(self.config()), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["--project", folder, "--config", str(config)])
            self.assertEqual(code, 0, output.getvalue())
            result = json.loads(output.getvalue())
            self.assertNotIn("segments", result["nets"][0])
            self.assertNotIn("unvalidated_campaign_changes", result)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)

    def test_public_launcher_returns_machine_readable_json(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = route_tests.RouteOptimizerTests.make_board(project)
            original = source.read_bytes()
            config = project / "input.json"
            config.write_text(json.dumps(self.config()), encoding="utf-8")
            launcher = Path(__file__).resolve().parents[1] / "run.py"
            process = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(launcher),
                    "--project",
                    folder,
                    "--kicad-python",
                    sys.executable,
                    "pcb",
                    "plan-nets",
                    "--config",
                    str(config),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(result["status"], "geometric_candidate_only")
            self.assertNotIn("segments", result["nets"][0])
            self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
