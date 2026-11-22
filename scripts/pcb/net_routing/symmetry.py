"""Measured symmetry of explicitly paired, electrically equivalent routes."""

from __future__ import annotations

import math
from dataclasses import dataclass

from pcb.net_routing.geometry import vertices
from pcb.route_optimizer import NM_PER_MM, Point, Route


@dataclass(frozen=True)
class Frame:
    origin: Point
    quarter_turns: int = 0
    mirror: bool = False

    def transform(self, point: Point) -> tuple[float, float]:
        if isinstance(self.quarter_turns, bool) or not isinstance(self.quarter_turns, int):
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
    path_distance_mm: float
    via_distance_mm: float
    via_count_difference: int
    branch_count_difference: int
    bend_count_difference: int

    @property
    def score(self) -> float:
        return (self.path_distance_mm + self.via_distance_mm + self.via_count_difference
                + self.branch_count_difference + self.bend_count_difference)


def _distance(point, start, end) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length2 = dx * dx + dy * dy
    if not length2:
        return math.dist(point, start)
    t = max(0.0, min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length2))
    return math.hypot(point[0] - start[0] - t * dx, point[1] - start[1] - t * dy)


def _segments(route: Route, frame: Frame):
    return tuple((frame.transform(s.start), frame.transform(s.end), s.layer) for s in route.segments)


def _path_distance(first, second, tolerance: float) -> float:
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
            (start[0] + (end[0] - start[0]) * i / intervals,
             start[1] + (end[1] - start[1]) * i / intervals)
            for i in range(intervals + 1)
        )
        distances = (
            [min(_distance(sample, a, b) for a, b in options) for sample in samples]
            if options else [max(1.0, length)] * len(samples)
        )
        weighted += length * sum(max(0.0, value - tolerance) for value in distances) / len(samples)
        total += length
    return weighted / total if total else 0.0


def _via_distance(first, second, tolerance: float) -> float:
    if not first or not second:
        return 0.0
    return sum(max(0.0, min(math.dist(p, q) for q in second) - tolerance) for p in first) / len(first)


def _topology(route: Route) -> tuple[int, int]:
    branches = bends = 0
    for directions in vertices(route.segments).values():
        if len(directions) > 2:
            branches += 1
        elif len(directions) == 2:
            a, b = directions
            if (a - b) % 8 != 4:
                bends += 1
    return branches, bends


def compare_routes(
    first: Route, first_frame: Frame, second: Route, second_frame: Frame, *, tolerance_mm: float = 0.05
) -> SymmetryComparison:
    """Compare supplied matching channels; this function never infers pairs."""
    if isinstance(tolerance_mm, bool) or not isinstance(tolerance_mm, (int, float)) or not math.isfinite(tolerance_mm) or tolerance_mm < 0:
        raise ValueError("Symmetry tolerance must be finite and nonnegative")
    left, right = _segments(first, first_frame), _segments(second, second_frame)
    left_vias = tuple(first_frame.transform(v.at) for v in first.vias)
    right_vias = tuple(second_frame.transform(v.at) for v in second.vias)
    left_branch, left_bend = _topology(first)
    right_branch, right_bend = _topology(second)
    return SymmetryComparison(
        (_path_distance(left, right, tolerance_mm) + _path_distance(right, left, tolerance_mm)) / 2,
        (_via_distance(left_vias, right_vias, tolerance_mm) + _via_distance(right_vias, left_vias, tolerance_mm)) / 2,
        abs(len(left_vias) - len(right_vias)),
        abs(left_branch - right_branch),
        abs(left_bend - right_bend),
    )


@dataclass(frozen=True)
class CostWeights:
    length: float = 1.0
    via: float = 10.0
    bend: float = 1.0
    diagonal: float = 1.0
    front: float = 0.25

    def __post_init__(self) -> None:
        values = (self.length, self.via, self.bend, self.diagonal, self.front)
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0 for x in values) or not self.length:
            raise ValueError("Routing weights must be finite and nonnegative, with positive length weight")


@dataclass(frozen=True)
class LayoutScore:
    open_connections: int
    symmetry: float
    length_mm: float
    vias: int
    bends: int
    diagonal_mm: float
    front_mm: float
    eligible: bool

    def key(self, weights: CostWeights = CostWeights()) -> tuple[int, float, float]:
        if self.eligible is not True:
            raise ValueError("An ineligible layout cannot be ranked for acceptance")
        counts = (self.open_connections, self.vias, self.bends)
        values = (self.symmetry, self.length_mm, self.diagonal_mm, self.front_mm)
        if any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in counts):
            raise ValueError("Layout counts must be nonnegative integers")
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0 for x in values):
            raise ValueError("Layout costs must be finite and nonnegative")
        if not isinstance(weights, CostWeights):
            raise ValueError("Routing weights must be a CostWeights record")
        cost = (weights.length * self.length_mm + weights.via * self.vias
                + weights.bend * self.bends + weights.diagonal * self.diagonal_mm
                + weights.front * self.front_mm)
        return self.open_connections, self.symmetry, cost
