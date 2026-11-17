"""Canonical shared copper, branch sites and geometric cost accounting."""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from functools import lru_cache
from fractions import Fraction
from itertools import combinations

from pcb.route_optimizer import (
    COPPER_LAYERS,
    GridRouter,
    Point,
    Route,
    Segment,
    Via,
    Weights,
    _guide_distance,
    is_staircase_triplet, is_standard_dogleg,
)
from pcb.net_routing.model import Site


class RouteGeometryError(ValueError):
    """A proposed route violates a represented geometry constraint."""


class StaircaseGeometryError(RouteGeometryError):
    """A checked path contains a staircase involving its added copper."""

    def __init__(self, message: str, witnesses: tuple[tuple[str, Point], ...] = ()):
        super().__init__(message)
        self.witnesses = witnesses


class OffAngleContactGeometryError(RouteGeometryError):
    """A proposed route touches existing copper outside the octilinear model."""

    def __init__(self, site: Site):
        super().__init__(
            "New copper touches off-angle existing copper away from "
            "a pad or via; reroute the existing segment"
        )
        self.site = site


def on_segment(point: Point, segment: Segment) -> bool:
    a, b = segment.start, segment.end
    return (
        (point.x - a.x) * (b.y - a.y) == (point.y - a.y) * (b.x - a.x)
        and min(a.x, b.x) <= point.x <= max(a.x, b.x)
        and min(a.y, b.y) <= point.y <= max(a.y, b.y)
    )


def _line(segment: Segment) -> tuple[tuple, int, int]:
    a, b = segment.start, segment.end
    GridRouter._direction(a, b)
    if a.x == b.x:
        key, first, last = (segment.layer, "v", a.x), a.y, b.y
    else:
        slope = (b.y - a.y) // (b.x - a.x)
        key, first, last = (segment.layer, slope, a.y - slope * a.x), a.x, b.x
    return key, min(first, last), max(first, last)


def _supported_line(segment: Segment) -> bool:
    """Whether existing copper can be indexed as an axis or 45-degree line."""
    dx = segment.end.x - segment.start.x
    dy = segment.end.y - segment.start.y
    return bool(dx or dy) and (not dx or not dy or abs(dx) == abs(dy))


def _point(key: tuple, coordinate: int) -> Point:
    _, slope, intercept = key
    return (
        Point(intercept, coordinate)
        if slope == "v"
        else Point(coordinate, slope * coordinate + intercept)
    )


def unique_segments(
    segments: tuple[Segment, ...], existing: tuple[Segment, ...] = ()
) -> tuple[Segment, ...]:
    """Union collinear proposed copper and subtract already-present copper."""
    groups: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    occupied: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    for segment in segments:
        key, lo, hi = _line(segment)
        groups[key].append((lo, hi))
    for segment in existing:
        if not _supported_line(segment):
            continue
        key, lo, hi = _line(segment)
        occupied[key].append((lo, hi))
    result = []
    for key, intervals in groups.items():
        merged: list[tuple[int, int]] = []
        for lo, hi in sorted(intervals):
            if merged and lo <= merged[-1][1]:
                merged[-1] = merged[-1][0], max(hi, merged[-1][1])
            else:
                merged.append((lo, hi))
        for start, end in merged:
            remaining = [(start, end)]
            for lo, hi in occupied[key]:
                remaining = [
                    interval
                    for a, b in remaining
                    for interval in (
                        [(a, b)]
                        if hi <= a or lo >= b
                        else ([(a, lo)] if lo > a else [])
                        + ([(hi, b)] if hi < b else [])
                    )
                ]
            result.extend(
                Segment(_point(key, a), _point(key, b), key[0]) for a, b in remaining
            )
    return tuple(
        sorted(result, key=lambda s: (s.layer, s.start.x, s.start.y, s.end.x, s.end.y))
    )


def intersection(first: Segment, second: Segment) -> Point | None:
    if first.layer != second.layer:
        return None
    a, b, c, d = first.start, first.end, second.start, second.end
    ux, uy, vx, vy = b.x - a.x, b.y - a.y, d.x - c.x, d.y - c.y
    denominator = ux * vy - uy * vx
    if not denominator:
        return None
    t = Fraction((c.x - a.x) * vy - (c.y - a.y) * vx, denominator)
    s = Fraction((c.x - a.x) * uy - (c.y - a.y) * ux, denominator)
    if not (0 <= t <= 1 and 0 <= s <= 1):
        return None
    x, y = a.x + t * ux, a.y + t * uy
    if x.denominator != 1 or y.denominator != 1:
        raise RouteGeometryError("A copper junction falls between native integer coordinates")
    return Point(int(x), int(y))


def vertices(
    segments: tuple[Segment, ...], anchors: tuple[Site, ...] = ()
) -> dict[Site, set[int]]:
    cuts = [{s.start, s.end} for s in segments]
    for i, j in combinations(range(len(segments)), 2):
        point = intersection(segments[i], segments[j])
        if point is not None:
            cuts[i].add(point)
            cuts[j].add(point)
    for index, segment in enumerate(segments):
        cuts[index].update(
            a.point
            for a in anchors
            if a.layer == segment.layer and on_segment(a.point, segment)
        )
    result: dict[Site, set[int]] = defaultdict(set)
    for segment, points in zip(segments, cuts, strict=True):
        ordered = sorted(points, key=lambda p: (p.x, p.y))
        for a, b in zip(ordered, ordered[1:]):
            direction = GridRouter._direction(a, b)
            result[Site(a, segment.layer)].add(direction)
            result[Site(b, segment.layer)].add((direction + 4) % 8)
    return result


@lru_cache(maxsize=16)
def _existing_lines(existing: tuple[Segment, ...]):
    """Merge old collinear pieces once; index four possible lines at a point."""
    supported = []
    for segment in existing:
        if _supported_line(segment):
            supported.append(segment)
    merged = unique_segments(tuple(supported))
    groups = defaultdict(list)
    for segment in merged:
        key, first, last = _line(segment)
        groups[key].append((first, last, segment))
    indexed = {}
    for key, intervals in groups.items():
        intervals.sort(key=lambda item: item[0])
        indexed[key] = tuple(item[0] for item in intervals), tuple(intervals)
    return merged, indexed


def _staircase_context(added: tuple[Segment, ...], existing: tuple[Segment, ...]) -> tuple[Segment, ...]:
    """Include the two old bends on either side of each new copper contact."""
    merged, indexed = _existing_lines(existing)
    relevant = {
        old for old in merged for new in added if old.layer == new.layer
        and (any(on_segment(p, old) for p in (new.start, new.end))
             or any(on_segment(p, new) for p in (old.start, old.end))
             or intersection(new, old) is not None)
    }
    frontier = relevant.copy()
    for _ in range(2):
        neighbors = set()
        for segment in frontier:
            for point in (segment.start, segment.end):
                for key, coordinate in (((segment.layer, "v", point.x), point.y),
                                        ((segment.layer, 0, point.y), point.x),
                                        ((segment.layer, 1, point.y - point.x), point.x),
                                        ((segment.layer, -1, point.y + point.x), point.x)):
                    starts, intervals = indexed.get(key, ((), ()))
                    index = bisect_right(starts, coordinate) - 1
                    if index >= 0 and intervals[index][0] <= coordinate <= intervals[index][1]:
                        neighbors.add(intervals[index][2])
        frontier = neighbors - relevant
        relevant.update(neighbors)
    return unique_segments((*added, *relevant))


def aggregate(
    routes: tuple[Route, ...],
    *,
    existing: tuple[Segment, ...],
    weights: Weights,
    layer_penalties: dict[str, float],
    guide: tuple[Point, ...],
    pad_sites: frozenset[Site] = frozenset(),
    forbid_staircase: bool = True,
    minimum_segment: int = 0,
) -> Route:
    """Charge each added copper length and physical through via exactly once."""
    segments = unique_segments(tuple(s for r in routes for s in r.segments), existing)
    via_by_point = {v.at: Via(v.at, "F.Cu", "B.Cu") for r in routes for v in r.vias}
    reused = {
        v.at: v for r in routes for v in r.reused_vias if v.at not in via_by_point
    }
    via_sites = {
        Site(p, layer) for p in (*via_by_point, *reused) for layer in COPPER_LAYERS
    }
    anchors = set(via_sites)
    # Inspect only contacts involving new copper; avoid quadratic rescanning of
    # a large pre-existing net and do not judge its unrelated legacy bends.
    for new in segments:
        for old in existing:
            crossing = intersection(new, old)
            if crossing is not None:
                anchors.add(Site(crossing, new.layer))
    graph = vertices(segments, tuple(anchors))
    bends = 0
    for site, directions in graph.items():
        if site in pad_sites or site in via_sites:
            continue
        relevant = set(directions)
        for old in existing:
            if site.layer == old.layer and on_segment(site.point, old):
                if not _supported_line(old):
                    raise OffAngleContactGeometryError(site)
                for endpoint in (old.start, old.end):
                    if endpoint != site.point:
                        relevant.add(GridRouter._direction(site.point, endpoint))
        # A degree-three or higher vertex is a branching junction, not a
        # two-segment bend. Match the independent saved-board style verifier.
        if len(relevant) != 2:
            continue
        a, b = relevant
        if (a - b) % 8 in {2, 6}:
            raise RouteGeometryError(
                f"A two-segment bend violates the 45-degree rule on {site.layer} "
                f"at ({site.point.x / 1e6:g}, {site.point.y / 1e6:g}) mm"
            )
        bends += (a - b) % 8 not in {0, 4}
    if forbid_staircase:
        checked = _staircase_context(segments, existing)
        stair_graph = vertices(checked, tuple(pad_sites | via_sites))
        added_intervals = defaultdict(list)
        for segment in segments:
            key, first, last = _line(segment)
            added_intervals[key].append((first, last))

        def includes_added(first, last):
            key, lo, hi = _line(Segment(first.point, last.point, first.layer))
            return any(max(lo, a) < min(hi, b) for a, b in added_intervals[key])

        adjacency: dict[Site, list[Site]] = defaultdict(list)
        for s in checked:
            points = sorted(
                (p for p in stair_graph if p.layer == s.layer and on_segment(p.point, s)),
                key=lambda p: p.key,
            )
            for a, b in zip(points, points[1:]):
                adjacency[a].append(b)
                adjacency[b].append(a)
        for start in adjacency:
            for second in adjacency[start]:
                chain = [start, second]
                while (
                    len(chain) < 4
                    and len(adjacency[chain[-1]]) == 2
                    and chain[-1] not in pad_sites
                    and chain[-1] not in via_sites
                ):
                    next_site = next(p for p in adjacency[chain[-1]] if p != chain[-2])
                    if next_site in chain:
                        break
                    chain.append(next_site)
                if len(chain) == 4:
                    a, b, c = [
                        GridRouter._direction(x.point, y.point)
                        for x, y in zip(chain, chain[1:])
                    ]
                    before = [p for p in adjacency[chain[0]] if p != chain[1]]
                    after = [p for p in adjacency[chain[-1]] if p != chain[-2]]
                    dogleg = (
                        len(before) == len(after) == 1
                        and chain[0] not in pad_sites | via_sites
                        and chain[-1] not in pad_sites | via_sites
                        and is_standard_dogleg((
                            GridRouter._direction(before[0].point, chain[0].point), a, b, c,
                            GridRouter._direction(chain[-1].point, after[0].point),
                        ))
                    )
                    if (is_staircase_triplet(a, b, c) and not dogleg
                            and any(includes_added(x, y) for x, y in zip(chain, chain[1:]))):
                        witnesses = []
                        for first, last in zip(chain, chain[1:]):
                            key, lo, hi = _line(Segment(first.point, last.point, first.layer))
                            for added_lo, added_hi in added_intervals[key]:
                                overlap_lo = max(lo, added_lo)
                                overlap_hi = min(hi, added_hi)
                                if overlap_hi - overlap_lo >= 2:
                                    witness = (first.layer, _point(key, (overlap_lo + overlap_hi) // 2))
                                    if witness not in witnesses:
                                        witnesses.append(witness)
                        raise StaircaseGeometryError(
                            f"A combined route forms a staircase on {start.layer} "
                            f"near ({start.point.x / 1e6:g}, {start.point.y / 1e6:g}) mm",
                            tuple(witnesses),
                        )
    if minimum_segment and any(
        math.hypot(s.end.x - s.start.x, s.end.y - s.start.y) < minimum_segment
        and Site(s.start, s.layer) not in pad_sites
        and Site(s.end, s.layer) not in pad_sites
        for s in segments
    ):
        raise RouteGeometryError("A combined segment is shorter than the configured minimum")
    length = sum(
        math.hypot(s.end.x - s.start.x, s.end.y - s.start.y) / 1e6 for s in segments
    )
    cost = (
        weights.length * length + weights.bend * bends + weights.via * len(via_by_point)
    )
    for s in segments:
        distance = math.hypot(s.end.x - s.start.x, s.end.y - s.start.y) / 1e6
        cost += layer_penalties.get(s.layer, 0) * distance
        if s.start.x != s.end.x and s.start.y != s.end.y:
            cost += weights.diagonal * distance
        if weights.symmetry:
            midpoint = Point((s.start.x + s.end.x) // 2, (s.start.y + s.end.y) // 2)
            cost += weights.symmetry * distance * _guide_distance(midpoint, guide)
    return Route(
        segments,
        tuple(via_by_point[p] for p in sorted(via_by_point, key=lambda p: (p.x, p.y))),
        tuple(reused[p] for p in sorted(reused, key=lambda p: (p.x, p.y))),
        length,
        bends,
        cost,
        sum(r.expanded for r in routes),
        any(r.search_truncated for r in routes),
    )


def branch_sites(
    segments: tuple[Segment, ...], targets: tuple[Site, ...], limit: int
) -> tuple[Site, ...]:
    """Sample exact endpoints and 0/45/90-degree projections onto tree copper."""
    sites = {Site(p, s.layer) for s in segments for p in (s.start, s.end)}
    for segment in segments:
        key, lo, hi = _line(segment)
        for target in targets:
            p = target.point
            coordinates = {lo, hi, (lo + hi) // 2}
            for direction in ((1, 0), (0, 1), (1, 1), (1, -1)):
                far = Point(p.x + direction[0] * 10**12, p.y + direction[1] * 10**12)
                near = Point(p.x - direction[0] * 10**12, p.y - direction[1] * 10**12)
                try:
                    crossing = intersection(segment, Segment(near, far, segment.layer))
                except RouteGeometryError:
                    continue
                if crossing:
                    sites.add(Site(crossing, segment.layer))
            sites.update(
                Site(_point(key, value), segment.layer) for value in coordinates
            )
    ordered = sorted(
        sites,
        key=lambda s: (
            min(
                (
                    math.hypot(s.point.x - t.point.x, s.point.y - t.point.y)
                    for t in targets
                ),
                default=0,
            ),
            s.key,
        ),
    )
    return tuple(ordered[:limit])


def touches(site: Site, route: Route) -> bool:
    return any(
        s.layer == site.layer and on_segment(site.point, s) for s in route.segments
    ) or any(v.at == site.point for v in (*route.vias, *route.reused_vias))
