"""Bounded cleanup of candidate routes while preserving copper contacts."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import replace
from itertools import combinations

from pcb.net_routing.geometry import (
    RouteGeometryError,
    aggregate,
    intersection,
    on_segment,
)
from pcb.net_routing.model import Budget, BudgetExhausted, Net, Site
from pcb.route_optimizer import GridRouter, Route, Segment
from core.compat import strict_zip


class RefinementNotApplicable(ValueError):
    """The optional refinement cannot safely treat this route as a chain."""


def _octilinear(points) -> bool:
    for a, b in zip(points, points[1:]):
        dx, dy = b.x - a.x, b.y - a.y
        if (not dx and not dy) or (dx and dy and abs(dx) != abs(dy)):
            return False
    return True


def _terminal_chains(route: Route, net: Net) -> list[tuple[Site, ...]]:
    """Partition proposed copper at terminals, vias and existing contacts.

    Each returned chain is on one layer. Ambiguous overlaps or closed loops are
    rejected so a cleanup cannot silently disconnect an existing connection.
    """
    segments = route.segments
    if not segments or len(segments) > 64:
        raise RefinementNotApplicable(
            "Too many segments for bounded terminal refinement"
        )
    cuts = [{s.start, s.end} for s in segments]
    anchors = set(net.pad_sites)
    anchors.update(site for island in net.islands for site in island.sites)
    anchors.update(
        Site(v.at, layer)
        for v in (*route.vias, *route.reused_vias)
        for layer in {s.layer for s in segments}
    )
    for i, segment in enumerate(segments):
        for j in range(i):
            other = segments[j]
            if other.layer != segment.layer:
                continue
            for point in (segment.start, segment.end, other.start, other.end):
                if on_segment(point, segment) and on_segment(point, other):
                    cuts[i].add(point)
                    cuts[j].add(point)
            point = intersection(segment, other)
            if point is not None:
                cuts[i].add(point)
                cuts[j].add(point)
        for old in net.existing:
            if old.layer != segment.layer:
                continue
            contacts = {
                point
                for point in (segment.start, segment.end, old.start, old.end)
                if on_segment(point, segment) and on_segment(point, old)
            }
            point = intersection(segment, old)
            if point is not None:
                contacts.add(point)
            cuts[i].update(contacts)
            anchors.update(Site(point, segment.layer) for point in contacts)
        cuts[i].update(
            site.point
            for site in anchors
            if site.layer == segment.layer and on_segment(site.point, segment)
        )

    adjacency: dict[Site, set[Site]] = defaultdict(set)
    edges = set()
    for segment, points in strict_zip(segments, cuts):
        ordered = sorted(points, key=lambda point: (point.x, point.y))
        for first, last in zip(ordered, ordered[1:]):
            a, b = Site(first, segment.layer), Site(last, segment.layer)
            edge = frozenset((a, b))
            if edge in edges:
                raise RefinementNotApplicable("Overlapping proposed copper")
            edges.add(edge)
            adjacency[a].add(b)
            adjacency[b].add(a)
    anchors.update(site for site, adjacent in adjacency.items() if len(adjacent) != 2)
    visited = set()
    chains = []
    for start in sorted(anchors & adjacency.keys(), key=lambda site: site.key):
        for neighbor in sorted(adjacency[start], key=lambda site: site.key):
            edge = frozenset((start, neighbor))
            if edge in visited:
                continue
            visited.add(edge)
            chain = [start, neighbor]
            while chain[-1] not in anchors:
                following = adjacency[chain[-1]] - {chain[-2]}
                if len(following) != 1:
                    raise RefinementNotApplicable("Ambiguous route branch")
                site = next(iter(following))
                edge = frozenset((chain[-1], site))
                if edge in visited:
                    raise RefinementNotApplicable("Closed route loop")
                visited.add(edge)
                chain.append(site)
            chains.append(tuple(chain))
    if visited != edges:
        raise RefinementNotApplicable("Unanchored route loop")
    return chains


def simplify_terminal_branches(
    route: Route, router: GridRouter, net: Net, budget: Budget, *, max_trials: int = 256
) -> Route:
    """Try local improvements from a completed tree without moving its anchors.

    The full tree is scored and its geometry checked after every shortcut. This
    is a bounded warm-start refinement; native DRC and electrical checks remain
    the caller's responsibility, and global optimality is not claimed.
    """
    if max_trials < 1:
        return route
    if any(not _octilinear((s.start, s.end)) for s in route.segments):
        return route
    try:
        paths = _terminal_chains(route, net)
        parts = [
            tuple(Segment(a.point, b.point, a.layer) for a, b in zip(path, path[1:]))
            for path in paths
        ]
        best = aggregate(
            (route,),
            existing=net.existing,
            weights=router.weights,
            layer_penalties=router.layer_penalties,
            guide=router.guide,
            pad_sites=net.pad_sites,
            pad_copper=net.pad_copper,
            forbid_staircase=router.forbid_staircase,
            minimum_segment=router.min_segment_length,
        )
    except (RefinementNotApplicable, RouteGeometryError):
        return route
    choices = sorted(
        (i for i, path in enumerate(paths) if len(path) > 2),
        key=lambda i: (
            not (paths[i][0] in net.pad_sites or paths[i][-1] in net.pad_sites),
            paths[i][0].key,
            paths[i][-1].key,
        ),
    )[:max_trials]
    if not choices:
        return route
    per_chain = max(1, max_trials // len(choices))
    changed = False
    for index in choices:
        try:
            budget.check()
            other = tuple(s for i, part in enumerate(parts) if i != index for s in part)
            local_net = replace(net, existing=(*net.existing, *other))
            local = Route(parts[index], (), (), 0, 0, 0, 0)
            improved = simplify_route(
                local,
                router,
                local_net,
                paths[index][0],
                paths[index][-1],
                budget,
                max_trials=per_chain,
            )
            if improved.segments == local.segments:
                continue
            candidate = replace(best, segments=(*other, *improved.segments))
            measured = aggregate(
                (candidate,),
                existing=net.existing,
                weights=router.weights,
                layer_penalties=router.layer_penalties,
                guide=router.guide,
                pad_sites=net.pad_sites,
                pad_copper=net.pad_copper,
                forbid_staircase=router.forbid_staircase,
                minimum_segment=router.min_segment_length,
            )
            if measured.cost + 1e-9 < best.cost:
                parts[index] = improved.segments
                best = measured
                changed = True
        except BudgetExhausted:
            return replace(best if changed else route, search_truncated=True)
        except RouteGeometryError:
            continue
    return best if changed else route


def chain_endpoints(route: Route) -> tuple[Site, Site] | None:
    """Find the two ends of a simple copper chain, or decline ambiguous trees."""
    degree = defaultdict(int)
    for segment in route.segments:
        degree[Site(segment.start, segment.layer)] += 1
        degree[Site(segment.end, segment.layer)] += 1
    for via in (*route.vias, *route.reused_vias):
        degree[Site(via.at, via.from_layer)] += 1
        degree[Site(via.at, via.to_layer)] += 1
    ends = sorted((s for s, count in degree.items() if count == 1), key=lambda s: s.key)
    if len(ends) != 2:
        return None
    try:
        _ordered_runs(route, *ends)
    except RefinementNotApplicable:
        return None
    return ends[0], ends[1]


def _ordered_runs(route: Route, start: Site, end: Site):
    """Return an unbranched path's points, preserving all layer transitions."""
    edges = defaultdict(list)
    seen_edges = set()

    def add(a, b):
        key = frozenset((a, b))
        if len(key) != 2 or key in seen_edges:
            raise RefinementNotApplicable("Route is not a simple chain")
        seen_edges.add(key)
        edges[a].append(b)
        edges[b].append(a)

    for segment in route.segments:
        add(Site(segment.start, segment.layer), Site(segment.end, segment.layer))
    for via in (*route.vias, *route.reused_vias):
        add(Site(via.at, via.from_layer), Site(via.at, via.to_layer))
    if start == end or start not in edges or end not in edges:
        raise RefinementNotApplicable("Route endpoints do not form a simple chain")
    if any(len(adjacent) > 2 for adjacent in edges.values()):
        raise RefinementNotApplicable("Branched routes are not simplified")
    if {site for site, adjacent in edges.items() if len(adjacent) == 1} != {start, end}:
        raise RefinementNotApplicable("Route has ambiguous endpoints")
    ordered = [start]
    previous = None
    while ordered[-1] != end:
        adjacent = [site for site in edges[ordered[-1]] if site != previous]
        if len(adjacent) != 1 or adjacent[0] in ordered:
            raise RefinementNotApplicable("Route has a loop or disconnected component")
        previous = ordered[-1]
        ordered.append(adjacent[0])
    if len(ordered) != len(edges):
        raise RefinementNotApplicable("Route has disconnected components")
    runs = [[start.point]]
    layers = [start.layer]
    for a, b in zip(ordered, ordered[1:]):
        if a.layer == b.layer:
            runs[-1].append(b.point)
        else:
            runs.append([b.point])
            layers.append(b.layer)
    if any(len(points) < 2 for points in runs):
        raise RefinementNotApplicable("A via-only run is not simplified")
    return runs, layers


def _self_intersects(segments: tuple[Segment, ...]) -> bool:
    for i, j in combinations(range(len(segments)), 2):
        if j == i + 1 or segments[i].layer != segments[j].layer:
            continue
        first, second = segments[i], segments[j]
        if (
            any(
                on_segment(p, first) and on_segment(p, second)
                for p in (first.start, first.end, second.start, second.end)
            )
            or intersection(first, second) is not None
        ):
            return True
    return False


def simplify_route(
    route: Route,
    router: GridRouter,
    net: Net,
    start: Site,
    end: Site,
    budget: Budget,
    *,
    max_trials: int = 256,
    repair_invalid: bool = False,
) -> Route:
    """Try shorter axis/45 shortcuts; return the original when unsure.

    Endpoints, layers and via objects never move. Exact KiCad checks and the
    caller's electrical assessment remain required for every candidate.
    """
    if len(route.segments) > 64 or max_trials < 1:
        return route
    try:
        runs, layers = _ordered_runs(route, start, end)
    except RefinementNotApplicable:
        return route
    if any(not _octilinear(points) for points in runs):
        return route
    required = set()
    try:
        for segment in route.segments:
            required.update(
                site
                for site in net.pad_sites
                if site.layer == segment.layer and on_segment(site.point, segment)
            )
            for old in net.existing:
                if old.layer != segment.layer:
                    continue
                for point in (segment.start, segment.end, old.start, old.end):
                    if on_segment(point, segment) and on_segment(point, old):
                        required.add(Site(point, segment.layer))
                crossing = intersection(segment, old)
                if crossing is not None:
                    required.add(Site(crossing, segment.layer))
    except RouteGeometryError:
        return route

    def collapse(points):
        merged = [points[0]]
        for point in points[1:]:
            if len(merged) > 1 and router._direction(
                merged[-2], merged[-1]
            ) == router._direction(merged[-1], point):
                merged[-1] = point
            else:
                merged.append(point)
        return merged

    def measure(points_by_run):
        segments = tuple(
            Segment(a, b, layer)
            for points, layer in strict_zip(points_by_run, layers)
            for a, b in zip(points, points[1:])
        )
        if not segments:
            return None
        if any(not _octilinear(points) for points in points_by_run):
            return None
        try:
            if any(
                not any(
                    s.layer == site.layer and on_segment(site.point, s)
                    for s in segments
                )
                for site in required
            ):
                return None
            directions_by_run = [
                [router._direction(a, b) for a, b in zip(points, points[1:])]
                for points in points_by_run
            ]
            directions = [heading for run in directions_by_run for heading in run]
            turns = (
                (b - a) % 8 for run in directions_by_run for a, b in zip(run, run[1:])
            )
            if any(
                t in {2, 4, 6} or (router.gentle_turns and t in {3, 5}) for t in turns
            ):
                return None
            if (
                router.initial_directions is not None
                and directions[0] not in router.initial_directions
            ):
                return None
            if (
                router.final_directions is not None
                and directions[-1] not in router.final_directions
            ):
                return None
            if router.enforce_endpoint_spacing and any(
                math.hypot(s.end.x - s.start.x, s.end.y - s.start.y)
                < router.min_segment_length
                for s in (segments[0], segments[-1])
            ):
                return None
            if _self_intersects(segments):
                return None
            raw = replace(route, segments=segments)
            measured = aggregate(
                (raw,),
                existing=net.existing,
                weights=router.weights,
                layer_penalties=router.layer_penalties,
                guide=router.guide,
                pad_sites=net.pad_sites,
                pad_copper=net.pad_copper,
                forbid_staircase=router.forbid_staircase,
                minimum_segment=router.min_segment_length,
            )
            return replace(
                raw,
                length_mm=measured.length_mm,
                bends=measured.bends,
                cost=measured.cost,
            )
        except RouteGeometryError:
            return None

    def clear(points, layer):
        return all(router.edge_clear(a, b, layer) for a, b in zip(points, points[1:]))

    compressed = [collapse(points) for points in runs]
    changed = compressed != runs
    runs = compressed
    best = measure(runs)
    if best is None and not repair_invalid:
        return route
    trials = 0
    while trials < max_trials:
        selected = None
        for index, points in enumerate(runs):
            for i in range(len(points) - 2):
                for j in range(i + 2, len(points)):
                    for shortcut in router._stub_paths(points[i], points[j]):
                        if len(shortcut) > j - i + 1 or (
                            best is not None and len(shortcut) == j - i + 1
                        ):
                            continue
                        trials += 1
                        if trials % 8 == 1:
                            try:
                                budget.check()
                            except BudgetExhausted:
                                return replace(
                                    best if changed and best is not None else route,
                                    search_truncated=True,
                                )
                        if clear(shortcut, layers[index]):
                            trial = [list(run) for run in runs]
                            trial[index] = collapse(
                                points[:i] + list(shortcut) + points[j + 1 :]
                            )
                            candidate = measure(trial)
                            if candidate is not None and (
                                best is None or candidate.cost + 1e-9 < best.cost
                            ):
                                if (
                                    selected is None
                                    or candidate.cost < selected[0].cost
                                ):
                                    selected = candidate, trial
                        if trials >= max_trials:
                            break
                    if trials >= max_trials:
                        break
                if trials >= max_trials:
                    break
            if trials >= max_trials:
                break
        if selected is None:
            break
        best, runs = selected
        changed = True
    return best if changed and best is not None else route
