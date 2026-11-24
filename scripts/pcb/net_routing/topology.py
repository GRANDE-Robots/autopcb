"""topology.py: beam search over complete multi-terminal trees, including shared branches.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Callable, Protocol

from pcb.route_optimizer import Route
from pcb.net_routing.geometry import RouteGeometryError, branch_sites, touches
from pcb.net_routing.model import Budget, BudgetExhausted, Net, NetPlan, Site


EMPTY_ROUTE = Route((), (), (), 0.0, 0, 0.0, 0)


class RoutingBackend(Protocol):
    """The path services whole-net search needs from a routing backend."""

    def paths(
        self, net: Net, prior: tuple[NetPlan, ...], tree: Route, start: Site, end: Site
    ) -> tuple[Route, ...]:
        """Candidate routes joining two access sites.

        Inputs:
            net: the net being routed.
            prior: plans already chosen for other nets.
            tree: copper of this net routed so far.
            start, end: the access sites to join.
        """

    def combine(self, net: Net, routes: tuple[Route, ...]) -> Route:
        """Merge routes of one net into a single measured route."""

    def accept(self, net: Net, route: Route) -> bool:
        """Whether a route passes the backend's additional screens."""


@dataclass(frozen=True)
class Tree:
    """A partial multi-terminal tree.

    Inputs:
        connected: indices of the net's islands the tree already joins.
        route: the copper routed so far.
    """

    connected: frozenset[int]
    route: Route


class TreeSearch:
    """Beam search over complete trees of a multi-terminal net; callable as whole_net."""

    def __call__(
        self,
        net: Net,
        prior: tuple[NetPlan, ...],
        backend: RoutingBackend,
        budget: Budget,
        *,
        rework_cost: Callable[[Route], float] | None = None,
    ) -> tuple[NetPlan, ...]:
        """Return diverse complete candidates; beam pruning does not prove optimality."""
        if any(not island.sites for island in net.islands):
            raise ValueError(
                f"Net {net.name} has an island without a routable access point"
            )
        if len(net.islands) <= 1:
            return (
                (
                    NetPlan(
                        net.name,
                        EMPTY_ROUTE,
                        rework_cost(EMPTY_ROUTE) if rework_cost else 0.0,
                    ),
                )
                if backend.accept(net, EMPTY_ROUTE)
                else ()
            )
        limits = budget.limits
        seeds = [Tree(frozenset({i}), EMPTY_ROUTE) for i in range(len(net.islands))]
        budget.pruned += max(0, len(seeds) - limits.tree_beam)
        frontier = seeds[: limits.tree_beam]
        completed: dict[tuple, NetPlan] = {}
        initial_calls = budget.calls
        try:
            for _ in range(len(net.islands) - 1):
                proposed: dict[tuple, Tree] = {}
                for tree_index, tree in enumerate(frontier):
                    budget.check()
                    with budget.tree_slice(len(frontier) - tree_index):
                        try:
                            tree_start_calls = budget.calls
                            remaining_calls = min(
                                limits.net_path_calls - (budget.calls - initial_calls),
                                limits.path_calls - budget.calls,
                            )
                            trees_left = len(frontier) - tree_index
                            tree_call_limit = max(
                                1, math.ceil(remaining_calls / trees_left)
                            )
                            sites = self._sites(net, tree, budget)
                            virtual = {
                                s
                                for island in net.islands
                                for s, _ in island.zone_anchors
                            }
                            plane_check = getattr(backend, "clear_plane_sites", None)
                            clear_plane = (
                                plane_check(net, prior, tree.route)
                                if virtual and plane_check
                                else None
                            )
                            endpoint_check = getattr(
                                backend, "endpoint_in_bounds", None
                            )
                            pairs = (
                                (
                                    int(
                                        endpoint_check is not None
                                        and (
                                            not endpoint_check(net, a)
                                            or not endpoint_check(net, b)
                                        )
                                    )
                                    + int(
                                        clear_plane is not None
                                        and a.layer != b.layer
                                        and (
                                            (a in virtual and a not in clear_plane)
                                            or (b in virtual and b not in clear_plane)
                                        )
                                    ),
                                    math.hypot(
                                        a.point.x - b.point.x, a.point.y - b.point.y
                                    ),
                                    a.key,
                                    b.key,
                                    i,
                                    a,
                                    b,
                                )
                                for i, island in enumerate(net.islands)
                                if i not in tree.connected
                                for a in sites
                                for b in island.sites
                                if a != b
                            )
                            # Keep the ordinary small sample fast. If none of those sites
                            # produces a useful tree, try farther retained sites rather
                            # than treating a sampling boundary as a routing failure.
                            sample_count = min(
                                max(
                                    limits.connections_per_step,
                                    limits.fallback_connections_per_step,
                                ),
                                limits.net_path_calls,
                                limits.path_calls,
                            )
                            tree_progress = False
                            sampled = heapq.nsmallest(sample_count, pairs)
                            for attempt, (_, _, _, _, index, start, end) in enumerate(
                                sampled
                            ):
                                if budget.calls - tree_start_calls >= tree_call_limit:
                                    if tree_index + 1 < len(frontier):
                                        budget.hit_limit("tree_path_calls")
                                    break
                                if (
                                    attempt >= limits.connections_per_step
                                    and tree_progress
                                ):
                                    break
                                if (
                                    budget.calls - initial_calls
                                    >= limits.net_path_calls
                                ):
                                    budget.hit_limit("net_path_calls")
                                    raise BudgetExhausted(
                                        "Per-net path-call budget exhausted"
                                    )
                                try:
                                    with budget.endpoint_slice(len(sampled) - attempt):
                                        paths = backend.paths(
                                            net, prior, tree.route, start, end
                                        )
                                except BudgetExhausted:
                                    # Restore the parent deadline first. A local
                                    # timeout must not skip all other access sites;
                                    # an expired tree/net/batch still stops here.
                                    budget.check()
                                    continue
                                for route in paths:
                                    try:
                                        combined = backend.combine(
                                            net, (tree.route, route)
                                        )
                                    except RouteGeometryError as error:
                                        budget.failure(
                                            "combined_geometry", f"{net.name}: {error}"
                                        )
                                        continue
                                    connected = tree.connected | {index}
                                    connected |= {
                                        i
                                        for i, island in enumerate(net.islands)
                                        if any(
                                            touches(s, combined) for s in island.sites
                                        )
                                    }
                                    plan = NetPlan(
                                        net.name,
                                        combined,
                                        rework_cost(combined) if rework_cost else 0.0,
                                    )
                                    if len(connected) == len(net.islands):
                                        if backend.accept(net, combined):
                                            completed[plan.signature] = plan
                                            tree_progress = True
                                            if (
                                                len(completed)
                                                > limits.net_candidates * 4
                                            ):
                                                completed = dict(
                                                    sorted(
                                                        completed.items(),
                                                        key=lambda item: item[1].cost,
                                                    )[: limits.net_candidates * 2]
                                                )
                                        continue
                                    state = Tree(frozenset(connected), combined)
                                    proposed[(state.connected, plan.signature)] = state
                                    tree_progress = True
                        except BudgetExhausted:
                            continue
                frontier = sorted(
                    proposed.values(),
                    key=lambda t: (
                        t.route.cost + self._remaining_estimate(net, t, budget),
                        NetPlan(net.name, t.route).signature,
                    ),
                )[: limits.tree_beam]
                budget.pruned += max(0, len(proposed) - limits.tree_beam)
                if not frontier:
                    break
        except BudgetExhausted:
            # Preserve any complete result obtained before a resource limit.
            pass
        return tuple(
            sorted(completed.values(), key=lambda p: (p.cost, p.signature))[
                : limits.net_candidates
            ]
        )

    @staticmethod
    def _sites(net: Net, tree: Tree, budget: Budget) -> tuple[Site, ...]:
        """Sites a new connection may start from.

        Outputs:
            Every site of the connected islands plus branch points sampled on the
            tree's copper toward the unconnected islands, sorted by site key.
        """
        pending = tuple(
            s
            for i, island in enumerate(net.islands)
            if i not in tree.connected
            for s in island.sites
        )
        originals = {s for i in tree.connected for s in net.islands[i].sites}
        originals.update(
            branch_sites(tree.route.segments, pending, budget.limits.branch_sites)
        )
        return tuple(sorted(originals, key=lambda s: s.key))

    @staticmethod
    def _remaining_estimate(net: Net, tree: Tree, budget: Budget) -> float:
        """Estimate the length still to route, in mm.

        Outputs:
            For each unconnected island, the shortest straight-line distance from
            its sites to the tree's start sites; the largest of these, or 0.0 when
            every island is connected.
        """
        sites = TreeSearch._sites(net, tree, budget)
        return max(
            (
                min(
                    math.hypot(a.point.x - b.point.x, a.point.y - b.point.y)
                    for a in island.sites
                    for b in sites
                )
                / 1e6
                for i, island in enumerate(net.islands)
                if i not in tree.connected
            ),
            default=0.0,
        )


whole_net = TreeSearch()
