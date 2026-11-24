"""geometry.py: canonical shared copper, branch sites and geometric cost accounting.

Copyright: GRANDE Robots and Aaron John Sabu
"""

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
    PadCopper,
    Point,
    Route,
    Segment,
    Via,
    Weights,
    _guide_distance,
)
from pcb.net_routing.model import Site
from core.compat import strict_zip


class RouteGeometryError(ValueError):
    """A proposed route violates a represented geometry constraint."""


class StaircaseGeometryError(RouteGeometryError):
    """A checked path contains a staircase involving its added copper."""

    def __init__(self, message: str, witnesses: tuple[tuple[str, Point], ...] = ()):
        """Record the staircase and where added copper overlaps it.

        Inputs:
            message: description of the staircase.
            witnesses: (layer name, point) pairs inside the staircase where added copper
                overlaps it.
        """
        super().__init__(message)
        self.witnesses = witnesses


class OffAngleContactGeometryError(RouteGeometryError):
    """A proposed route touches existing copper outside the octilinear model."""

    def __init__(self, site: Site):
        """Record the offending contact.

        Inputs:
            site: the contact on off-angle existing copper, kept as the site attribute.
        """
        super().__init__(
            "New copper touches off-angle existing copper away from "
            "a pad or via; reroute the existing segment"
        )
        self.site = site


class OnSegment:
    """Tests whether a point lies on a segment."""

    def __call__(self, point: Point, segment: Segment) -> bool:
        """Whether a point lies on the closed segment; exact on integer nm coordinates
        and independent of layer."""
        a, b = segment.start, segment.end
        return (
            (point.x - a.x) * (b.y - a.y) == (point.y - a.y) * (b.x - a.x)
            and min(a.x, b.x) <= point.x <= max(a.x, b.x)
            and min(a.y, b.y) <= point.y <= max(a.y, b.y)
        )


on_segment = OnSegment()


class SegmentLine:
    """Describes the line through an axis-aligned or 45-degree segment."""

    def __call__(self, segment: Segment) -> tuple[tuple, int, int]:
        """Describe the line through an octilinear segment.

        Outputs:
            (key, first, last). The key is (layer, "v", x) for a vertical line and
            (layer, slope, intercept) otherwise; first and last are the lowest and
            highest y (vertical) or x coordinate of the segment, in nm. Raises
            ValueError when the segment is not octilinear.
        """
        a, b = segment.start, segment.end
        GridRouter._direction(a, b)
        if a.x == b.x:
            key, first, last = (segment.layer, "v", a.x), a.y, b.y
        else:
            slope = (b.y - a.y) // (b.x - a.x)
            key, first, last = (segment.layer, slope, a.y - slope * a.x), a.x, b.x
        return key, min(first, last), max(first, last)

    @staticmethod
    def supported(segment: Segment) -> bool:
        """Whether existing copper can be indexed as an axis or 45-degree line."""
        dx = segment.end.x - segment.start.x
        dy = segment.end.y - segment.start.y
        return bool(dx or dy) and (not dx or not dy or abs(dx) == abs(dy))

    @staticmethod
    def point(key: tuple, coordinate: int) -> Point:
        """The point at a coordinate along a line.

        Inputs:
            key: a line key from SegmentLine().
            coordinate: y for a vertical line, otherwise x, in nm.
        """
        _, slope, intercept = key
        return (
            Point(intercept, coordinate)
            if slope == "v"
            else Point(coordinate, slope * coordinate + intercept)
        )


_line = SegmentLine()


class UniqueSegments:
    """Merges collinear proposed copper and removes what existing copper already
    covers."""

    def __call__(
        self, segments: tuple[Segment, ...], existing: tuple[Segment, ...] = ()
    ) -> tuple[Segment, ...]:
        """Union collinear proposed copper and subtract already-present copper."""
        groups: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
        occupied: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
        for segment in segments:
            key, lo, hi = _line(segment)
            groups[key].append((lo, hi))
        for segment in existing:
            if not SegmentLine.supported(segment):
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
                    Segment(
                        SegmentLine.point(key, a), SegmentLine.point(key, b), key[0]
                    )
                    for a, b in remaining
                )
        return tuple(
            sorted(
                result, key=lambda s: (s.layer, s.start.x, s.start.y, s.end.x, s.end.y)
            )
        )


unique_segments = UniqueSegments()


class SegmentIntersection:
    """Finds where two segments cross."""

    def __call__(self, first: Segment, second: Segment) -> Point | None:
        """Intersection point of two segments.

        Outputs:
            The crossing as a Point, or None when the layers differ, the segments are
            parallel or they do not meet. Raises RouteGeometryError when the crossing
            falls between native integer coordinates.
        """
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
            raise RouteGeometryError(
                "A copper junction falls between native integer coordinates"
            )
        return Point(int(x), int(y))


intersection = SegmentIntersection()


class SegmentVertices:
    """Finds the vertices of a set of segments and the headings leaving each."""

    def __call__(
        self, segments: tuple[Segment, ...], anchors: tuple[Site, ...] = ()
    ) -> dict[Site, set[int]]:
        """Split segments at their ends, crossings and anchors.

        Inputs:
            segments: the copper to split.
            anchors: sites that also split a segment they lie on.
        Outputs:
            For each Site, the set of compass headings 0 to 7 of the pieces that leave
            it.
        """
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
        for segment, points in strict_zip(segments, cuts):
            ordered = sorted(points, key=lambda p: (p.x, p.y))
            for a, b in zip(ordered, ordered[1:]):
                direction = GridRouter._direction(a, b)
                result[Site(a, segment.layer)].add(direction)
                result[Site(b, segment.layer)].add((direction + 4) % 8)
        return result


vertices = SegmentVertices()


class ExistingLines:
    """Merged and indexed existing collinear copper, cached for recent copper sets."""

    def __init__(self):
        """Create an empty cache holding up to 16 copper sets."""
        self._cached = lru_cache(maxsize=16)(self._merge)

    def __call__(self, existing: tuple[Segment, ...]):
        """Merge old collinear pieces once; index four possible lines at a point.

        Inputs:
            existing: existing segments; unsupported (off-angle) ones are ignored.
        Outputs:
            (merged segments, index) where the index maps each line key to its
            sorted interval starts and (first, last, segment) intervals.
        """
        return self._cached(existing)

    def cache_clear(self) -> None:
        """Forget every cached copper set."""
        self._cached.cache_clear()

    @staticmethod
    def _merge(existing: tuple[Segment, ...]):
        """Merge and index existing copper; the uncached work of a call."""
        supported = []
        for segment in existing:
            if SegmentLine.supported(segment):
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


_existing_lines = ExistingLines()


class RouteAggregator:
    """Combines routes into one measured route and rejects copper that breaks the
    geometry rules."""

    def __call__(
        self,
        routes: tuple[Route, ...],
        *,
        existing: tuple[Segment, ...],
        weights: Weights,
        layer_penalties: dict[str, float],
        guide: tuple[Point, ...],
        pad_sites: frozenset[Site] = frozenset(),
        pad_copper: PadCopper = PadCopper(),
        forbid_staircase: bool = True,
        minimum_segment: int = 0,
    ) -> Route:
        """Charge each added copper length and physical through via exactly once.

        A point inside same-net pad copper anchors the track there, as in the
        saved-board style check: bends and staircase chains end at it.

        Inputs:
            routes: the routes to merge.
            existing: segments already on the board for this net; they are
                subtracted from the new copper and checked for contacts.
            weights: cost weights for length, bends, vias, diagonals and symmetry.
            layer_penalties: extra cost per mm of track, by layer name.
            guide: points of the symmetry guide.
            pad_sites: sites that lie on pads.
            pad_copper: pad copper rectangles that anchor track ends.
            forbid_staircase: reject staircases that involve the added copper.
            minimum_segment: shortest allowed segment away from pads, in nm; 0 disables.
        Outputs:
            One route with unique segments, one via per point, its length in mm,
            bend count and cost. Raises RouteGeometryError for a junction between
            integer coordinates, a 90 degree two-segment bend or a too-short
            segment, OffAngleContactGeometryError for new copper touching
            off-angle existing copper, and StaircaseGeometryError for a staircase.
        """
        segments = unique_segments(
            tuple(s for r in routes for s in r.segments), existing
        )
        via_by_point = {v.at: Via(v.at, "F.Cu", "B.Cu") for r in routes for v in r.vias}
        reused = {
            v.at: v for r in routes for v in r.reused_vias if v.at not in via_by_point
        }
        via_sites = {
            Site(p, layer) for p in (*via_by_point, *reused) for layer in COPPER_LAYERS
        }
        anchors = set(via_sites)

        def anchored(site: Site) -> bool:
            """Whether a site lies on a pad, a via or pad copper."""
            return (
                site in pad_sites
                or site in via_sites
                or pad_copper.holds(site.point, site.layer)
            )

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
            if anchored(site):
                continue
            relevant = set(directions)
            for old in existing:
                if site.layer == old.layer and on_segment(site.point, old):
                    if not SegmentLine.supported(old):
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
            checked = self._staircase_context(segments, existing)
            graph = vertices(checked)
            added_intervals = defaultdict(list)
            for segment in segments:
                key, first, last = _line(segment)
                added_intervals[key].append((first, last))
            ends: dict[Site, list[Segment]] = defaultdict(list)
            for segment in checked:
                ends[Site(segment.start, segment.layer)].append(segment)
                ends[Site(segment.end, segment.layer)].append(segment)
            visited: set[Segment] = set()

            def extend(
                segment: Segment, point: Point, *, backwards: bool
            ) -> list[Segment]:
                # As in the saved-board style check, a chain runs through a track's
                # interior, crossings included, and continues only at a plain joint
                # of two tracks that is not anchored on pad or via copper.
                """Follow a chain of segments from a point.

                Inputs:
                    segment: the segment the walk leaves.
                    point: its end where the walk continues.
                    backwards: orient the returned segments toward the start of the
                        chain.
                Outputs:
                    The further segments in walking order; the walk stops at an anchored
                    site, a branch or a segment already visited.
                """
                run = []
                site = Site(point, segment.layer)
                while (
                    len(graph.get(site, ())) == 2
                    and len(ends[site]) == 2
                    and not anchored(site)
                ):
                    following = next(s for s in ends[site] if s != segment)
                    if following in visited:
                        break
                    visited.add(following)
                    other = (
                        following.end if following.start == point else following.start
                    )
                    run.append(
                        Segment(other, point, site.layer)
                        if backwards
                        else Segment(point, other, site.layer)
                    )
                    segment, point = following, other
                    site = Site(point, segment.layer)
                return run

            for seed in segments:
                if seed in visited:
                    continue
                visited.add(seed)
                chain = [
                    *reversed(extend(seed, seed.start, backwards=True)),
                    seed,
                    *extend(seed, seed.end, backwards=False),
                ]
                for window in GridRouter.staircase_windows(chain):
                    witnesses, touched = [], False
                    for run in window:
                        key, lo, hi = _line(run)
                        for added_lo, added_hi in added_intervals[key]:
                            overlap_lo, overlap_hi = (
                                max(lo, added_lo),
                                min(hi, added_hi),
                            )
                            touched = touched or overlap_lo < overlap_hi
                            if overlap_hi - overlap_lo >= 2:
                                witness = (
                                    run.layer,
                                    SegmentLine.point(
                                        key, (overlap_lo + overlap_hi) // 2
                                    ),
                                )
                                if witness not in witnesses:
                                    witnesses.append(witness)
                    if touched:
                        turn = window[1].start
                        raise StaircaseGeometryError(
                            f"A combined route forms a staircase on {seed.layer} "
                            f"near ({turn.x / 1e6:g}, {turn.y / 1e6:g}) mm",
                            tuple(witnesses),
                        )
        if minimum_segment and any(
            math.hypot(s.end.x - s.start.x, s.end.y - s.start.y) < minimum_segment
            and Site(s.start, s.layer) not in pad_sites
            and Site(s.end, s.layer) not in pad_sites
            and not pad_copper.covers(s)
            for s in segments
        ):
            raise RouteGeometryError(
                "A combined segment is shorter than the configured minimum"
            )
        length = sum(
            math.hypot(s.end.x - s.start.x, s.end.y - s.start.y) / 1e6 for s in segments
        )
        cost = (
            weights.length * length
            + weights.bend * bends
            + weights.via * len(via_by_point)
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
            tuple(
                via_by_point[p] for p in sorted(via_by_point, key=lambda p: (p.x, p.y))
            ),
            tuple(reused[p] for p in sorted(reused, key=lambda p: (p.x, p.y))),
            length,
            bends,
            cost,
            sum(r.expanded for r in routes),
            any(r.search_truncated for r in routes),
        )

    @staticmethod
    def _staircase_context(
        added: tuple[Segment, ...], existing: tuple[Segment, ...]
    ) -> tuple[Segment, ...]:
        """Include the two old bends on either side of each new copper contact."""
        merged, indexed = _existing_lines(existing)
        relevant = {
            old
            for old in merged
            for new in added
            if old.layer == new.layer
            and (
                any(on_segment(p, old) for p in (new.start, new.end))
                or any(on_segment(p, new) for p in (old.start, old.end))
                or intersection(new, old) is not None
            )
        }
        frontier = relevant.copy()
        for _ in range(2):
            neighbors = set()
            for segment in frontier:
                for point in (segment.start, segment.end):
                    for key, coordinate in (
                        ((segment.layer, "v", point.x), point.y),
                        ((segment.layer, 0, point.y), point.x),
                        ((segment.layer, 1, point.y - point.x), point.x),
                        ((segment.layer, -1, point.y + point.x), point.x),
                    ):
                        starts, intervals = indexed.get(key, ((), ()))
                        index = bisect_right(starts, coordinate) - 1
                        if (
                            index >= 0
                            and intervals[index][0] <= coordinate <= intervals[index][1]
                        ):
                            neighbors.add(intervals[index][2])
            frontier = neighbors - relevant
            relevant.update(neighbors)
        return unique_segments((*added, *relevant))


aggregate = RouteAggregator()


class BranchSites:
    """Samples sites on tree copper from which a new connection may start."""

    def __call__(
        self, segments: tuple[Segment, ...], targets: tuple[Site, ...], limit: int
    ) -> tuple[Site, ...]:
        """Sample exact endpoints and 0/45/90-degree projections onto tree copper."""
        sites = {Site(p, s.layer) for s in segments for p in (s.start, s.end)}
        for segment in segments:
            key, lo, hi = _line(segment)
            for target in targets:
                p = target.point
                coordinates = {lo, hi, (lo + hi) // 2}
                for direction in ((1, 0), (0, 1), (1, 1), (1, -1)):
                    far = Point(
                        p.x + direction[0] * 10**12, p.y + direction[1] * 10**12
                    )
                    near = Point(
                        p.x - direction[0] * 10**12, p.y - direction[1] * 10**12
                    )
                    try:
                        crossing = intersection(
                            segment, Segment(near, far, segment.layer)
                        )
                    except RouteGeometryError:
                        continue
                    if crossing:
                        sites.add(Site(crossing, segment.layer))
                sites.update(
                    Site(SegmentLine.point(key, value), segment.layer)
                    for value in coordinates
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


branch_sites = BranchSites()


class RouteTouch:
    """Tests whether a route reaches a site."""

    def __call__(self, site: Site, route: Route) -> bool:
        """Whether a segment on the site's layer passes through its point, or a via sits
        there."""
        return any(
            s.layer == site.layer and on_segment(site.point, s) for s in route.segments
        ) or any(v.at == site.point for v in (*route.vias, *route.reused_vias))


touches = RouteTouch()
