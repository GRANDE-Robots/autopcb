"""symmetry.py: measured symmetry of explicitly paired, electrically equivalent routes.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from pcb.net_routing.geometry import vertices
from pcb.route_optimizer import NM_PER_MM, Point, Route


@dataclass(frozen=True)
class Frame:
    """A reference frame for comparing two routes, optionally turned and mirrored.

    Inputs:
        origin: board point (nm) that becomes the frame's (0, 0).
        quarter_turns: integer number of turns applied as (x, y) -> (-y, x).
        mirror: when True, x is negated after the turns.
    """

    origin: Point
    quarter_turns: int = 0
    mirror: bool = False

    def transform(self, point: Point) -> tuple[float, float]:
        """Map a board point into the frame.

        Inputs:
            point: board point in nm.
        Outputs:
            (x, y) in mm relative to the origin, after the quarter turns and the
            optional mirror. Raises ValueError when quarter_turns is not an integer
            or mirror is not a boolean.
        """
        if isinstance(self.quarter_turns, bool) or not isinstance(
            self.quarter_turns, int
        ):
            raise ValueError("A symmetry frame needs integer quarter turns")
        if not isinstance(self.mirror, bool):
            raise ValueError("A symmetry frame needs a boolean mirror flag")
        x = (point.x - self.origin.x) / NM_PER_MM
        y = (point.y - self.origin.y) / NM_PER_MM
        for _ in range(self.quarter_turns % 4):
            x, y = -y, x
        return (-x, y) if self.mirror else (x, y)


@dataclass(frozen=True)
class SymmetryComparison:
    """How far two routes are from being symmetric.

    Inputs:
        path_distance_mm: mean distance between the two routes' copper beyond the
            tolerance, in mm.
        via_distance_mm: mean distance between the two routes' vias beyond the
            tolerance, in mm.
        via_count_difference: absolute difference of the via counts.
        branch_count_difference: absolute difference of the branch point counts.
        bend_count_difference: absolute difference of the bend counts.
    """

    path_distance_mm: float
    via_distance_mm: float
    via_count_difference: int
    branch_count_difference: int
    bend_count_difference: int

    @property
    def score(self) -> float:
        """The sum of all five terms; zero for symmetric routes."""
        return (
            self.path_distance_mm
            + self.via_distance_mm
            + self.via_count_difference
            + self.branch_count_difference
            + self.bend_count_difference
        )


class RouteSymmetryComparer:
    """Measures how closely two supplied routes match after each is mapped into its
    frame."""

    def __call__(
        self,
        first: Route,
        first_frame: Frame,
        second: Route,
        second_frame: Frame,
        *,
        tolerance_mm: float = 0.05,
    ) -> SymmetryComparison:
        """Compare supplied matching channels; this function never infers pairs."""
        if (
            isinstance(tolerance_mm, bool)
            or not isinstance(tolerance_mm, (int, float))
            or not math.isfinite(tolerance_mm)
            or tolerance_mm < 0
        ):
            raise ValueError("Symmetry tolerance must be finite and nonnegative")
        left, right = (
            self._segments(first, first_frame),
            self._segments(second, second_frame),
        )
        left_vias = tuple(first_frame.transform(v.at) for v in first.vias)
        right_vias = tuple(second_frame.transform(v.at) for v in second.vias)
        left_branch, left_bend = self._topology(first)
        right_branch, right_bend = self._topology(second)
        return SymmetryComparison(
            (
                self._path_distance(left, right, tolerance_mm)
                + self._path_distance(right, left, tolerance_mm)
            )
            / 2,
            (
                self._via_distance(left_vias, right_vias, tolerance_mm)
                + self._via_distance(right_vias, left_vias, tolerance_mm)
            )
            / 2,
            abs(len(left_vias) - len(right_vias)),
            abs(left_branch - right_branch),
            abs(left_bend - right_bend),
        )

    @staticmethod
    def _distance(point, start, end) -> float:
        """Distance from a point to the segment start-end; all values in mm."""
        dx, dy = end[0] - start[0], end[1] - start[1]
        length2 = dx * dx + dy * dy
        if not length2:
            return math.dist(point, start)
        t = max(
            0.0,
            min(
                1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length2
            ),
        )
        return math.hypot(point[0] - start[0] - t * dx, point[1] - start[1] - t * dy)

    @staticmethod
    def _segments(route: Route, frame: Frame):
        """The route's segments as (start, end, layer) with the ends mapped into the
        frame (mm)."""
        return tuple(
            (frame.transform(s.start), frame.transform(s.end), s.layer)
            for s in route.segments
        )

    @staticmethod
    def _path_distance(first, second, tolerance: float) -> float:
        """Length-weighted mean distance of the first route from the second, in mm.

        Inputs:
            first, second: (start, end, layer) segments in mm.
            tolerance: distance in mm below which a deviation counts as zero.
        Outputs:
            Each segment of first is sampled along its length (at most 256 intervals);
            a sample's distance is measured to the nearest segment of second on the
            same layer, or max(1 mm, segment length) when second has none on that
            layer. Returns the length-weighted mean of the distances beyond
            tolerance, or 0.0 for no segments. Raises ValueError for a zero-length
            segment.
        """
        weighted = total = 0.0
        for start, end, layer in first:
            length = math.dist(start, end)
            if length == 0:
                raise ValueError("Zero-length route segment cannot be compared")
            options = [(a, b) for a, b, other_layer in second if other_layer == layer]
            # Sample the complete run rather than only its ends and midpoint.  The
            # cap bounds comparison cost; this is a layout score, not a geometric
            # connectivity or clearance proof.
            intervals = min(256, max(2, math.ceil(length / 0.1)))
            samples = tuple(
                (
                    start[0] + (end[0] - start[0]) * i / intervals,
                    start[1] + (end[1] - start[1]) * i / intervals,
                )
                for i in range(intervals + 1)
            )
            distances = (
                [
                    min(
                        RouteSymmetryComparer._distance(sample, a, b)
                        for a, b in options
                    )
                    for sample in samples
                ]
                if options
                else [max(1.0, length)] * len(samples)
            )
            weighted += (
                length
                * sum(max(0.0, value - tolerance) for value in distances)
                / len(samples)
            )
            total += length
        return weighted / total if total else 0.0

    @staticmethod
    def _via_distance(first, second, tolerance: float) -> float:
        """Mean distance in mm from each via of first to the nearest via of second,
        beyond tolerance; 0.0 when either has no vias."""
        if not first or not second:
            return 0.0
        return sum(
            max(0.0, min(math.dist(p, q) for q in second) - tolerance) for p in first
        ) / len(first)

    @staticmethod
    def _topology(route: Route) -> tuple[int, int]:
        """Count the branch points and bends of a route.

        Outputs:
            (branches, bends): vertices with more than two segment directions, and
            vertices with exactly two directions that are not opposite.
        """
        branches = bends = 0
        for directions in vertices(route.segments).values():
            if len(directions) > 2:
                branches += 1
            elif len(directions) == 2:
                a, b = directions
                if (a - b) % 8 != 4:
                    bends += 1
        return branches, bends


compare_routes = RouteSymmetryComparer()


@dataclass(frozen=True)
class CostWeights:
    """Weights that turn a layout's measurements into one cost.

    Inputs:
        length: cost per mm of track length; must be positive.
        via: cost per via.
        bend: cost per bend.
        diagonal: cost per mm of diagonal track.
        front: cost per mm of track on the front layer.
    """

    length: float = 1.0
    via: float = 10.0
    bend: float = 1.0
    diagonal: float = 1.0
    front: float = 0.25

    def __post_init__(self) -> None:
        """Reject weights that are not finite nonnegative numbers, or a zero length
        weight."""
        values = (self.length, self.via, self.bend, self.diagonal, self.front)
        if (
            any(
                isinstance(x, bool)
                or not isinstance(x, (int, float))
                or not math.isfinite(x)
                or x < 0
                for x in values
            )
            or not self.length
        ):
            raise ValueError(
                "Routing weights must be finite and nonnegative, with positive length weight"
            )


@dataclass(frozen=True)
class LayoutScore:
    """Measurements of one candidate layout.

    Inputs:
        open_connections: number of unrouted connections.
        symmetry: symmetry error of the layout (see SymmetryComparison.score).
        length_mm: track length in mm.
        vias: number of vias.
        bends: number of bends.
        diagonal_mm: diagonal track length in mm.
        front_mm: track length on the front layer in mm.
        eligible: True when the layout passed every hard check.
    """

    open_connections: int
    symmetry: float
    length_mm: float
    vias: int
    bends: int
    diagonal_mm: float
    front_mm: float
    eligible: bool

    def key(self, weights: CostWeights = CostWeights()) -> tuple[int, float, float]:
        """Rank key for choosing between layouts; lower sorts first.

        Inputs:
            weights: cost weights for length, vias, bends, diagonal and front copper.
        Outputs:
            (open connections, symmetry, weighted cost). Raises ValueError when the
            layout is not eligible, a count is not a nonnegative integer, a
            measurement is not a finite nonnegative number, or weights is not a
            CostWeights.
        """
        if self.eligible is not True:
            raise ValueError("An ineligible layout cannot be ranked for acceptance")
        counts = (self.open_connections, self.vias, self.bends)
        values = (self.symmetry, self.length_mm, self.diagonal_mm, self.front_mm)
        if any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in counts):
            raise ValueError("Layout counts must be nonnegative integers")
        if any(
            isinstance(x, bool)
            or not isinstance(x, (int, float))
            or not math.isfinite(x)
            or x < 0
            for x in values
        ):
            raise ValueError("Layout costs must be finite and nonnegative")
        if not isinstance(weights, CostWeights):
            raise ValueError("Routing weights must be a CostWeights record")
        cost = (
            weights.length * self.length_mm
            + weights.via * self.vias
            + weights.bend * self.bends
            + weights.diagonal * self.diagonal_mm
            + weights.front * self.front_mm
        )
        return self.open_connections, self.symmetry, cost
