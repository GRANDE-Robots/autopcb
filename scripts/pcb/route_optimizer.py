"""Plan editable octilinear track/via candidates on a conservative grid.

This is a geometric optimizer, not an electrical or manufacturing verifier.
Native KiCad DRC and connectivity checks are required before using a result.
"""

from __future__ import annotations

import heapq
import itertools
import math
from collections import OrderedDict
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable
from core.compat import strict_zip

NM_PER_MM = 1_000_000
COPPER_LAYERS = ("F.Cu", *(f"In{index}.Cu" for index in range(1, 31)), "B.Cu")
DIRECTIONS = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))


def is_staircase_triplet(first: int, middle: int, last: int) -> bool:
    """Match diagonal, cardinal, then the same diagonal direction."""
    return first == last and first % 2 == 1 and middle % 2 == 0


def is_standard_dogleg(directions: Iterable[int]) -> bool:
    """Recognize two 45-degree chamfers around one perpendicular cardinal run."""
    directions = tuple(directions)
    if len(directions) != 5:
        return False
    first, diagonal, middle, other_diagonal, last = directions
    return (first == last and diagonal == other_diagonal
            and first % 2 == middle % 2 == 0 and diagonal % 2 == 1
            and (first - middle) % 8 in {2, 6}
            and (diagonal - first) % 8 in {1, 7}
            and (diagonal - middle) % 8 in {1, 7})


def _advance_staircase_history(memory: int, current: int, following: int,
                                anchored: bool = False) -> int | None:
    """Keep three prior runs; resolve a diagonal/cardinal/diagonal after its exit.

    A pending final triplet is checked on the complete candidate, where pad
    access and existing-copper context are available. No unsafe path is accepted
    merely because this bounded search history has not seen its ending yet.
    A turn inside same-net pad copper ends the chain there, so a staircase
    ending at it has no dogleg exit and the next run starts a new chain.
    """
    if current < 0 or current == following:
        return memory
    history = []
    value = max(0, memory)
    while value:
        history.append(value % 9 - 1)
        value //= 9
    history.reverse()
    if anchored:
        return None if len(history) >= 2 and is_staircase_triplet(*history[-2:], current) else -1
    context = [*history, current, following]
    if (len(context) == 5 and is_staircase_triplet(*context[1:4])
            and not is_standard_dogleg(context)):
        return None
    result = 0
    for direction in (*history, current)[-3:]:
        result = result * 9 + direction + 1
    return result


def mm(value: object) -> int:
    """Convert finite millimetres to exact integer nanometres."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("Millimetre values must be numbers")
    try:
        result = Decimal(str(value)) * NM_PER_MM
    except InvalidOperation as exc:
        raise ValueError("Invalid millimetre value") from exc
    if not result.is_finite() or result != result.to_integral_value():
        raise ValueError(
            "Coordinates must be finite and have at most nanometre precision"
        )
    return int(result)


def to_mm(value: int) -> float:
    return value / NM_PER_MM


def _octilinear_lower_bound(
    dx: int, dy: int, step: float, axial_weight: float, diagonal_weight: float
) -> float:
    """Cheapest unobstructed displacement, ignoring bends and layer changes."""
    short, long = sorted((dx, dy))
    diagonal_cost = math.sqrt(2) * (axial_weight + diagonal_weight)
    return step * (
        (long - short) * axial_weight + short * min(2 * axial_weight, diagonal_cost)
    )


@dataclass(frozen=True)
class Point:
    x: int
    y: int

    @classmethod
    def from_mm(cls, value: object) -> Point:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError(
                "A point must be a two-element [x, y] array in millimetres"
            )
        return cls(mm(value[0]), mm(value[1]))

    def as_mm(self) -> list[float]:
        return [to_mm(self.x), to_mm(self.y)]


@dataclass(frozen=True)
class Obstacle:
    """Conservative bounds, optionally with capsule or rounded-pad geometry."""

    lower: Point
    upper: Point
    layers: tuple[str, ...]
    net: str = ""
    kind: str = "copper"
    centerline: tuple[Point, Point] | None = None
    copper_radius: int = 0
    corner_radius: int = 0
    min_track_clearance: int = 0
    min_via_clearance: int = 0
    boundary_only: bool = False

    def __post_init__(self) -> None:
        if self.lower.x > self.upper.x or self.lower.y > self.upper.y:
            raise ValueError("Obstacle bounds are reversed")
        if not self.layers or any(layer not in COPPER_LAYERS for layer in self.layers):
            raise ValueError("Obstacle layers must be copper layers")
        if self.kind not in {"pad", "copper", "via", "keepout"}:
            raise ValueError("Obstacle kind must be pad, copper, via, or keepout")
        if not isinstance(self.boundary_only, bool) or (self.boundary_only and self.kind != "keepout"):
            raise ValueError("Only a native rule area can use its boundary without a copper gap")
        if (
            isinstance(self.copper_radius, bool)
            or not isinstance(self.copper_radius, int)
            or self.copper_radius < 0
        ):
            raise ValueError("Copper radius must be a nonnegative integer distance")
        if (
            isinstance(self.corner_radius, bool)
            or not isinstance(self.corner_radius, int)
            or self.corner_radius < 0
            or self.corner_radius * 2 > min(
                self.upper.x - self.lower.x, self.upper.y - self.lower.y
            )
            or (self.corner_radius and (self.kind != "pad" or self.centerline is not None))
        ):
            raise ValueError("Rounded corners require a valid pad rectangle")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in (self.min_track_clearance, self.min_via_clearance)):
            raise ValueError("Obstacle clearances must be nonnegative integer distances")
        if self.centerline is None:
            if self.copper_radius:
                raise ValueError("Copper radius requires a trace centerline")
            return
        if (
            self.kind not in {"copper", "pad"}
            or not isinstance(self.centerline, tuple)
            or len(self.centerline) != 2
            or any(not isinstance(point, Point) for point in self.centerline)
        ):
            raise ValueError("A copper capsule requires two points and a supported kind")
        if self.kind == "pad" and self.centerline[0] != self.centerline[1]:
            raise ValueError("A circular pad requires a single center point")
        if any(
            point.x - self.copper_radius < self.lower.x
            or point.x + self.copper_radius > self.upper.x
            or point.y - self.copper_radius < self.lower.y
            or point.y + self.copper_radius > self.upper.y
            for point in self.centerline
        ):
            raise ValueError("Trace bounds must include the whole copper capsule")

    def gap(self, clearance: int, *, via: bool = False) -> int:
        """Native rule areas already define where the copper edge must stop."""
        if self.boundary_only:
            return 0
        return max(clearance, self.min_track_clearance, self.min_via_clearance if via else 0)


def _point_near_segment(point: Point, first: Point, last: Point, radius: int) -> bool:
    """Compare squared distances exactly, including both round end caps."""
    dx, dy = last.x - first.x, last.y - first.y
    px, py = point.x - first.x, point.y - first.y
    length_sq = dx * dx + dy * dy
    projection = px * dx + py * dy
    if not length_sq or projection <= 0:
        return px * px + py * py <= radius * radius
    if projection >= length_sq:
        qx, qy = point.x - last.x, point.y - last.y
        return qx * qx + qy * qy <= radius * radius
    cross = px * dy - py * dx
    return cross * cross <= radius * radius * length_sq


def _segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    def cross(p: Point, q: Point, r: Point) -> int:
        return (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x)

    def on_segment(p: Point, q: Point, r: Point) -> bool:
        return min(p.x, q.x) <= r.x <= max(p.x, q.x) and min(p.y, q.y) <= r.y <= max(
            p.y, q.y
        )

    ab_c, ab_d, cd_a, cd_b = (
        cross(a, b, c),
        cross(a, b, d),
        cross(c, d, a),
        cross(c, d, b),
    )
    if (ab_c > 0 > ab_d or ab_d > 0 > ab_c) and (cd_a > 0 > cd_b or cd_b > 0 > cd_a):
        return True
    return any(
        (
            ab_c == 0 and on_segment(a, b, c),
            ab_d == 0 and on_segment(a, b, d),
            cd_a == 0 and on_segment(c, d, a),
            cd_b == 0 and on_segment(c, d, b),
        )
    )


@dataclass(frozen=True)
class Weights:
    length: float = 1.0
    bend: float = 1.0
    via: float = 10.0
    diagonal: float = 1.0
    symmetry: float = 0.0

    def __post_init__(self) -> None:
        values = (self.length, self.bend, self.via, self.diagonal, self.symmetry)
        if (
            any(not math.isfinite(value) or value < 0 for value in values)
            or self.length == 0
        ):
            raise ValueError(
                "Weights must be finite and nonnegative, with positive length weight"
            )


@dataclass(frozen=True)
class Segment:
    start: Point
    end: Point
    layer: str


@dataclass(frozen=True)
class Via:
    at: Point
    from_layer: str
    to_layer: str


@dataclass(frozen=True)
class PadCopper:
    """Boxes (layer, x0, y0, x1, y1) that lie inside one net's pad copper.

    A track vertex inside one is anchored on the pad, as in the saved-board
    style check: no bend or staircase rule applies at it.
    """

    boxes: tuple[tuple[str, int, int, int, int], ...] = ()

    def holds(self, point: Point, layer: str) -> bool:
        return any(name == layer and x0 <= point.x <= x1 and y0 <= point.y <= y1
                   for name, x0, y0, x1, y1 in self.boxes)

    def covers(self, segment: Segment) -> bool:
        return self.holds(segment.start, segment.layer) and self.holds(segment.end, segment.layer)


@dataclass(frozen=True)
class Route:
    segments: tuple[Segment, ...]
    vias: tuple[Via, ...]
    reused_vias: tuple[Via, ...]
    length_mm: float
    bends: int
    cost: float
    expanded: int
    search_truncated: bool = False


class SearchBudgetExceeded(RuntimeError):
    """A shared search budget was exhausted."""


class SearchLimitReached(RuntimeError):
    """The search was stopped at its configured expansion limit."""

    def __init__(self, message: str, *, expanded: int = 0) -> None:
        super().__init__(message)
        self.expanded = expanded


class NoGridRoute(RuntimeError):
    """No route exists in the supplied discrete grid and obstacle model."""

    def __init__(self, message: str, *, expanded: int = 0) -> None:
        super().__init__(message)
        self.expanded = expanded


def mirror_points(
    points: Iterable[Point], axis: str, coordinate: int
) -> tuple[Point, ...]:
    if axis == "x":
        return tuple(Point(2 * coordinate - p.x, p.y) for p in points)
    if axis == "y":
        return tuple(Point(p.x, 2 * coordinate - p.y) for p in points)
    raise ValueError("Mirror axis must be x or y")


def _guide_distance(point: Point, guide: tuple[Point, ...]) -> float:
    if not guide:
        return 0.0
    if len(guide) == 1:
        return math.hypot(point.x - guide[0].x, point.y - guide[0].y) / NM_PER_MM
    best = math.inf
    for first, last in zip(guide, guide[1:]):
        dx, dy = last.x - first.x, last.y - first.y
        length2 = dx * dx + dy * dy
        t = (
            0.0
            if not length2
            else max(
                0.0,
                min(
                    1.0, ((point.x - first.x) * dx + (point.y - first.y) * dy) / length2
                ),
            )
        )
        best = min(
            best, math.hypot(point.x - first.x - t * dx, point.y - first.y - t * dy)
        )
    return best / NM_PER_MM


class GridRouter:
    """Search one net over a bounded grid; every layer transition is a through via."""

    def __init__(
        self,
        lower: Point,
        upper: Point,
        step: int,
        layers: tuple[str, ...],
        net: str,
        width: int,
        clearance: int,
        via_size: int,
        via_drill: int,
        obstacles: Iterable[Obstacle] = (),
        weights: Weights = Weights(),
        guide: tuple[Point, ...] = (),
        max_expansions: int = 200_000,
        max_cells: int = 1_000_000,
        allow_new_vias: bool = True,
        allowed_reused_vias: set[Point] | None = None,
        layer_penalties: dict[str, float] | None = None,
        forbid_staircase: bool = False,
        min_segment_length: int = 0,
        gentle_turns: bool = False,
        initial_directions: frozenset[int] | None = None,
        final_directions: frozenset[int] | None = None,
        enforce_endpoint_spacing: bool = False,
        topology_heuristic: bool = False,
        heuristic_weight: float = 1.0,
        check_budget: Callable[[], None] | None = None,
        profile=None,
        first_feasible: bool = False,
        pad_copper: PadCopper = PadCopper(),
    ) -> None:
        if step <= 0 or width <= 0 or clearance < 0:
            raise ValueError(
                "Grid step and width must be positive and clearance nonnegative"
            )
        if via_drill <= 0 or via_size <= via_drill:
            raise ValueError("Via size must exceed its positive drill diameter")
        if not net or not layers or len(set(layers)) != len(layers):
            raise ValueError("A net and distinct copper layers are required")
        if any(layer not in COPPER_LAYERS for layer in layers):
            raise ValueError("Unknown copper layer")
        if (
            lower.x >= upper.x
            or lower.y >= upper.y
            or max_expansions < 1
            or max_cells < 1
        ):
            raise ValueError("Invalid routing bounds or search limits")
        if weights.symmetry and not guide:
            raise ValueError("A symmetry weight requires a guide")
        penalties = layer_penalties or {}
        if any(layer not in COPPER_LAYERS for layer in penalties) or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in penalties.values()
        ):
            raise ValueError(
                "Layer penalties must be nonnegative finite costs per millimetre"
            )
        self.lower, self.upper, self.step = lower, upper, step
        self.layers, self.net = layers, net
        self.width, self.clearance = width, clearance
        self.profile = profile
        self.via_size, self.via_drill = via_size, via_drill
        self.allow_new_vias = allow_new_vias
        if not isinstance(forbid_staircase, bool):
            raise ValueError("forbid_staircase must be true or false")
        self.forbid_staircase = forbid_staircase
        if (
            isinstance(min_segment_length, bool)
            or not isinstance(min_segment_length, int)
            or min_segment_length < 0
        ):
            raise ValueError("min_segment_length must be a nonnegative distance")
        self.min_segment_length = min_segment_length
        if not isinstance(gentle_turns, bool):
            raise ValueError("gentle_turns must be true or false")
        self.gentle_turns = gentle_turns
        for allowed in (initial_directions, final_directions):
            if allowed is not None and (
                not allowed
                or any(
                    isinstance(item, bool)
                    or not isinstance(item, int)
                    or item not in range(8)
                    for item in allowed
                )
            ):
                raise ValueError(
                    "Endpoint directions must be nonempty sets of headings 0 to 7"
                )
        self.initial_directions = initial_directions
        self.final_directions = final_directions
        if not isinstance(enforce_endpoint_spacing, bool):
            raise ValueError("enforce_endpoint_spacing must be true or false")
        self.enforce_endpoint_spacing = enforce_endpoint_spacing
        if not isinstance(first_feasible, bool):
            raise ValueError("first_feasible must be true or false")
        self.first_feasible = first_feasible
        self.pad_copper = pad_copper
        if not isinstance(topology_heuristic, bool):
            raise ValueError("topology_heuristic must be true or false")
        if (
            isinstance(heuristic_weight, bool)
            or not isinstance(heuristic_weight, (int, float))
            or not math.isfinite(heuristic_weight)
            or heuristic_weight < 1
        ):
            raise ValueError("heuristic_weight must be finite and at least 1")
        self.topology_heuristic, self.heuristic_weight = (
            topology_heuristic,
            heuristic_weight,
        )
        self.check_budget = check_budget or (lambda: None)
        self.layer_penalties = dict(penalties)
        self.weights, self.guide, self.max_expansions = weights, guide, max_expansions
        self.max_cells = max_cells
        self.nx = (upper.x - lower.x) // step + 1
        self.ny = (upper.y - lower.y) // step + 1
        if self.nx * self.ny * len(layers) > max_cells:
            raise ValueError(
                "Grid exceeds max_cells; increase the step or reduce the bounds"
            )
        self.blocked: dict[str, set[tuple[int, int]]] = {
            layer: set() for layer in layers
        }
        self._pad_cells = {
            (layers.index(name), x, y)
            for name, x0, y0, x1, y1 in pad_copper.boxes if name in layers
            for x in range(max(0, -((lower.x - x0) // step)), min(self.nx - 1, (x1 - lower.x) // step) + 1)
            for y in range(max(0, -((lower.y - y0) // step)), min(self.ny - 1, (y1 - lower.y) // step) + 1)
        }
        self.via_blocked: set[tuple[int, int]] = set()
        self.existing_vias: dict[tuple[int, int], set[str]] = {}
        self.foreign_obstacles: dict[str, list[Obstacle]] = {
            layer: [] for layer in layers
        }
        self._topology_cache = OrderedDict()
        # Grid accesses of the off-grid endpoint now being searched; one
        # relaxed map from all of them bounds the cost to each.
        self._goal_seeds: frozenset[tuple[int, int, int]] = frozenset()
        self._edge_cache = OrderedDict()
        # Dense searches revisit the same physical edge with many headings.
        # Scale this cache with the actual grid, while bounding memory per router.
        self._edge_cache_limit = max(8192, min(65536, self.nx * self.ny * len(layers) // 16))
        self._spatial = {layer: {} for layer in layers}
        self._spatial_step = mm(2)
        # Masks only eliminate impossible vertices. Every actual edge is
        # checked geometrically, so narrow corridors need no half-cell padding.
        obstacles = tuple(obstacles)
        self._obstacles = obstacles
        self.max_obstacle_track_clearance = max(
            (item.min_track_clearance for item in obstacles), default=0
        )
        for index, obstacle in enumerate(obstacles):
            if index % 32 == 0:
                self.check_budget()
            same_net = (
                bool(obstacle.net)
                and obstacle.net == net
                and obstacle.kind != "keepout"
            )
            affected = set(obstacle.layers).intersection(layers)
            if same_net and obstacle.kind == "via":
                cx2 = obstacle.lower.x + obstacle.upper.x
                cy2 = obstacle.lower.y + obstacle.upper.y
                if cx2 % 2 == 0 and cy2 % 2 == 0:
                    center = Point(cx2 // 2, cy2 // 2)
                    if allowed_reused_vias is None or center in allowed_reused_vias:
                        try:
                            cell = self._index(center)
                        except ValueError:
                            pass  # An off-grid via can be considered with a finer grid.
                        else:
                            self.existing_vias.setdefault(cell, set()).update(
                                obstacle.layers
                            )
            if affected and not same_net:
                for layer in affected:
                    self.foreign_obstacles[layer].append(obstacle)
                    for cell in self._spatial_cells(obstacle.lower, obstacle.upper):
                        self._spatial[layer].setdefault(cell, []).append(obstacle)
                track_width = profile.minimum_width if profile else width
                track_clearance = profile.minimum_clearance if profile else clearance
                track_margin = math.ceil(track_width / 2) + obstacle.gap(track_clearance)
                cells = self._covered(obstacle, track_margin)
                for layer in affected:
                    self.blocked[layer].update(cells)
            # Through vias cross every layer, including layers not selected for
            # routing. The caller must include obstacles on all copper layers.
            if not same_net or obstacle.kind in {"pad", "via"}:
                via_margin = math.ceil(via_size / 2) + obstacle.gap(clearance, via=True)
                self.via_blocked.update(self._covered(obstacle, via_margin))

    def _spatial_cells(self, first: Point, last: Point, margin: int = 0):
        step = self._spatial_step
        for x in range((min(first.x, last.x) - margin) // step, (max(first.x, last.x) + margin) // step + 1):
            for y in range((min(first.y, last.y) - margin) // step, (max(first.y, last.y) + margin) // step + 1):
                yield x, y

    def edge_clear(self, first: Point, last: Point, layer: str) -> bool:
        """Check complete swept copper, including off-grid access and necks."""
        if first == last:
            return True
        key = (first, last, layer) if (first.x, first.y) < (last.x, last.y) else (last, first, layer)
        if key in self._edge_cache:
            self._edge_cache.move_to_end(key)
            return self._edge_cache[key]
        profile = self.profile
        segment = Segment(first, last, layer)
        pieces = profile.pieces(segment) if profile else ((segment, self.width),)
        clear = True
        for piece, width in pieces:
            allowance = max(
                profile.maximum_clearance if profile else self.clearance,
                self.max_obstacle_track_clearance,
            )
            nearby = {id(obstacle): obstacle for cell in self._spatial_cells(piece.start, piece.end, math.ceil(width / 2) + allowance)
                      for obstacle in self._spatial[layer].get(cell, ())}
            edge_margin = math.ceil(width / 2) + (profile.minimum_clearance if profile else self.clearance)
            if not self._inside_point(piece.start, edge_margin) or not self._inside_point(piece.end, edge_margin):
                clear = False
                break
            def clearance(obstacle):
                if profile is None:
                    return self.clearance
                resolve = getattr(profile, "clearance_for_track", None)
                return resolve(piece, obstacle, width) if resolve else profile.clearance_at(piece, obstacle)

            if any(self._intersects_box(piece.start, piece.end, obstacle,
                                        math.ceil(width / 2) + obstacle.gap(clearance(obstacle)))
                   for obstacle in nearby.values()):
                clear = False
                break
        self._edge_cache[key] = clear
        if len(self._edge_cache) > self._edge_cache_limit:
            self._edge_cache.popitem(last=False)
        return clear

    def _covered(self, obstacle: Obstacle, margin: int) -> set[tuple[int, int]]:
        x0 = max(
            0, math.ceil((obstacle.lower.x - margin - 1 - self.lower.x) / self.step)
        )
        x1 = min(
            self.nx - 1, (obstacle.upper.x + margin + 1 - self.lower.x) // self.step
        )
        y0 = max(
            0, math.ceil((obstacle.lower.y - margin - 1 - self.lower.y) / self.step)
        )
        y1 = min(
            self.ny - 1, (obstacle.upper.y + margin + 1 - self.lower.y) // self.step
        )
        rows = max(0, y1 - y0 + 1)
        columns = max(0, x1 - x0 + 1)
        if obstacle.centerline is None:
            # Euclidean clearance around a rectangle has round corners.
            # Rasterize each column with integer distances rather than
            # blocking the entire inflated bounding square.
            result = set()
            corner = obstacle.corner_radius
            left, right = obstacle.lower.x + corner, obstacle.upper.x - corner
            top, bottom = obstacle.lower.y + corner, obstacle.upper.y - corner
            radius = margin + corner + 1
            columns_per_check = max(1, 16_384 // max(1, rows))
            for x in range(x0, x1 + 1):
                if columns * rows > 32_768 and (x - x0) % columns_per_check == 0:
                    self.check_budget()
                px = self.lower.x + x * self.step
                distance = max(left - px, 0, px - right)
                if distance > radius:
                    continue
                vertical = math.isqrt(radius * radius - distance * distance)
                lo = max(y0, math.ceil((top - vertical - self.lower.y) / self.step))
                hi = min(y1, (bottom + vertical - self.lower.y) // self.step)
                result.update((x, y) for y in range(lo, hi + 1))
            return result
        first, last = obstacle.centerline
        radius = obstacle.copper_radius + margin + 1
        if first.x > last.x:
            first, last = last, first
        dx, dy = last.x - first.x, last.y - first.y
        result = set()
        columns_per_check = max(1, 16_384 // max(1, rows))
        for x in range(x0, x1 + 1):
            if columns * rows > 32_768 and (x - x0) % columns_per_check == 0:
                self.check_budget()
            px = self.lower.x + x * self.step
            if dx == 0:
                if abs(px - first.x) > radius:
                    continue
                lower = min(first.y, last.y) - radius
                upper = max(first.y, last.y) + radius
            else:
                # Any capsule point is within radius of a segment point in
                # this x interval. Bound its y before the exact distance test.
                left = max(first.x, px - radius)
                right = min(last.x, px + radius)
                if left > right:
                    continue
                a = first.y * dx + (left - first.x) * dy
                b = first.y * dx + (right - first.x) * dy
                lower = min(a, b) // dx - radius
                upper = -(-max(a, b) // dx) + radius
            lo = max(y0, -(-(lower - self.lower.y) // self.step))
            hi = min(y1, (upper - self.lower.y) // self.step)
            # Check deadlines even for a nearly vertical, very long trace.
            for y_start in range(lo, hi + 1, 4096):
                if columns * rows > 32_768 and y_start != lo:
                    self.check_budget()
                result.update(
                    (x, y) for y in range(y_start, min(y_start + 4096, hi + 1))
                    if _point_near_segment(self._point(x, y), first, last, radius)
                )
        return result

    def _index(self, point: Point) -> tuple[int, int]:
        dx, dy = point.x - self.lower.x, point.y - self.lower.y
        if dx % self.step or dy % self.step:
            raise ValueError("Endpoint is not on the specified grid")
        x, y = dx // self.step, dy // self.step
        if not (0 <= x < self.nx and 0 <= y < self.ny):
            raise ValueError("Endpoint is outside routing bounds")
        return x, y

    def _point(self, x: int, y: int) -> Point:
        return Point(self.lower.x + x * self.step, self.lower.y + y * self.step)

    def _inside(self, x: int, y: int, margin: int) -> bool:
        # Relative integer coordinates are exactly equivalent to creating a
        # Point and checking the absolute bounds, without a per-cell allocation.
        return (
            margin <= x * self.step <= self.upper.x - self.lower.x - margin
            and margin <= y * self.step <= self.upper.y - self.lower.y - margin
        )

    def _inside_point(self, p: Point, margin: int) -> bool:
        return (
            self.lower.x + margin <= p.x <= self.upper.x - margin
            and self.lower.y + margin <= p.y <= self.upper.y - margin
        )

    @staticmethod
    def _direction(a: Point, b: Point) -> int:
        dx, dy = b.x - a.x, b.y - a.y
        if not dx and not dy:
            raise ValueError("Zero-length route segment")
        if dx and dy and abs(dx) != abs(dy):
            raise ValueError("Route segment is not an axis or 45-degree line")
        return DIRECTIONS.index(((dx > 0) - (dx < 0), (dy > 0) - (dy < 0)))

    @staticmethod
    def _stub_paths(start: Point, end: Point) -> list[tuple[Point, ...]]:
        dx, dy = end.x - start.x, end.y - start.y
        if not dx and not dy:
            return [(start,)]
        if not dx or not dy or abs(dx) == abs(dy):
            return [(start, end)]
        sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
        if abs(dx) > abs(dy):
            first = Point(end.x - sx * abs(dy), start.y)
            second = Point(start.x + sx * abs(dy), end.y)
        else:
            first = Point(start.x, end.y - sy * abs(dx))
            second = Point(end.x, start.y + sy * abs(dx))
        return [(start, first, end), (start, second, end)]

    def _direct_warm_route(
        self, start: Point, end: Point, layer: str,
        candidate_check: Callable[[Route], bool] | None,
    ) -> Route | None:
        """Try exact same-layer chamfers before a bounded grid search."""
        paths = list(self._stub_paths(start, end))
        dx, dy = end.x - start.x, end.y - start.y
        span = min(abs(dx), abs(dy))
        if dx and dy and span > 1:
            sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
            samples = {span // 4, span // 2, 3 * span // 4,
                       self.step // 2, self.step, span - self.step // 2, span - self.step}
            for distance in sorted(value for value in samples if 0 < value < span):
                paths.append((start, Point(end.x - sx * distance, start.y),
                              Point(end.x, start.y + sy * distance), end))
                paths.append((start, Point(start.x, end.y - sy * distance),
                              Point(start.x + sx * distance, end.y), end))
        result = self._checked_warm_paths(paths, layer, candidate_check)
        if result is not None or max(abs(dx), abs(dy)) < max(mm(6), 16 * self.step):
            return result

        # A full two-chamfer dogleg can use an interior cardinal corridor when
        # both endpoint corridors are obstructed. Keep short pad escapes cheap
        # and sample these longer patterns only after the simpler paths fail.
        paths = []
        for distance in sorted({self.step, 2 * self.step, span // 16, span // 8, span // 4}):
            if not 0 < 2 * distance < span:
                continue
            for horizontal in (True, False):
                room = (abs(dx) if horizontal else abs(dy)) - 2 * distance
                offsets = {room // 8, room // 4, room // 2, 3 * room // 4,
                           7 * room // 8, self.step, room - self.step}
                for offset in sorted(value for value in offsets if 0 < value < room):
                    if horizontal:
                        first = Point(start.x + sx * offset, start.y)
                        second = Point(first.x + sx * distance, start.y + sy * distance)
                        third = Point(second.x, end.y - sy * distance)
                        fourth = Point(third.x + sx * distance, end.y)
                    else:
                        first = Point(start.x, start.y + sy * offset)
                        second = Point(start.x + sx * distance, first.y + sy * distance)
                        third = Point(end.x - sx * distance, second.y)
                        fourth = Point(end.x, third.y + sy * distance)
                    paths.append((start, first, second, third, fourth, end))
        return self._checked_warm_paths(paths, layer, candidate_check)

    def _checked_warm_paths(
        self, paths: Iterable[tuple[Point, ...]], layer: str,
        candidate_check: Callable[[Route], bool] | None,
    ) -> Route | None:
        """Apply identical dimensions, geometry and context gates to warm paths."""
        choices = []
        seen = set()
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            if len(path) < 2 or any(a == b for a, b in zip(path, path[1:])):
                continue
            segments = tuple(Segment(a, b, layer) for a, b in zip(path, path[1:]))
            directions = [self._direction(segment.start, segment.end) for segment in segments]
            if ((self.initial_directions is not None and directions[0] not in self.initial_directions)
                    or (self.final_directions is not None and directions[-1] not in self.final_directions)):
                continue
            turns = [(right - left) % 8 for left, right in zip(directions, directions[1:])]
            if any(self._illegal_turn(turn, at, layer) for turn, at in zip(turns, path[1:])):
                continue
            if self.forbid_staircase and self._staircase_off_pads(segments):
                continue
            distances = [math.hypot(seg.end.x - seg.start.x, seg.end.y - seg.start.y)
                         for seg in segments]
            short = [value < self.min_segment_length and not self.pad_copper.covers(segment)
                     for segment, value in strict_zip(segments, distances)]
            if any(short[1:-1]) or self.enforce_endpoint_spacing and (short[0] or short[-1]):
                continue
            length = sum(distances) / NM_PER_MM
            diagonal = sum(distance for segment, distance in strict_zip(segments, distances)
                           if segment.start.x != segment.end.x and segment.start.y != segment.end.y) / NM_PER_MM
            symmetry = sum(distance / NM_PER_MM * _guide_distance(segment.end, self.guide)
                           for segment, distance in strict_zip(segments, distances))
            bends = sum(bool(turn) for turn in turns)
            cost = ((self.weights.length + self.layer_penalties.get(layer, 0)) * length
                    + self.weights.bend * bends + self.weights.diagonal * diagonal
                    + self.weights.symmetry * symmetry)
            choices.append((cost, len(segments), path, segments, length, bends))
        for cost, _, _, segments, length, bends in sorted(choices, key=lambda value: (value[0], value[1])):
            self.check_budget()
            if any(not self.edge_clear(segment.start, segment.end, layer) for segment in segments):
                continue
            result = Route(segments, (), (), length, bends, cost, 0, search_truncated=True)
            if candidate_check is None or candidate_check(result):
                return result
        return None

    def _direct_one_via_route(
        self, start: Point, start_layer: str, end: Point, end_layer: str,
        candidate_check: Callable[[Route], bool] | None,
    ) -> Route | None:
        """Try bounded exact cross-layer paths before expanding the grid."""
        if start_layer == end_layer:
            return None
        midpoint = Point((start.x + end.x) // 2, (start.y + end.y) // 2)
        anchors = (start, end, midpoint, Point(start.x, end.y), Point(end.x, start.y))
        cells = set(self.existing_vias)
        if self.allow_new_vias:
            for anchor in anchors:
                x = (anchor.x - self.lower.x) // self.step
                y = (anchor.y - self.lower.y) // self.step
                cells.update((x + dx, y + dy) for dx in (-1, 0, 1, 2) for dy in (-1, 0, 1, 2))

        def estimate(cell):
            at = self._point(*cell)
            reusable = {start_layer, end_layer} <= self.existing_vias.get(cell, set())
            distance = sum(max(abs(a.x - b.x), abs(a.y - b.y))
                           + (math.sqrt(2) - 1) * min(abs(a.x - b.x), abs(a.y - b.y))
                           for a, b in ((start, at), (at, end)))
            return (0 if reusable else self.weights.via, distance, cell)

        margin = math.ceil(self.via_size / 2) + self.clearance
        viable = []
        for cell in cells:
            x, y = cell
            if not (0 <= x < self.nx and 0 <= y < self.ny) or not self._inside(x, y, margin):
                continue
            at = self._point(x, y)
            if at in (start, end) or cell in self.blocked[start_layer] or cell in self.blocked[end_layer]:
                continue
            reusable = {start_layer, end_layer} <= self.existing_vias.get(cell, set())
            if not reusable and (not self.allow_new_vias or cell in self.via_blocked):
                continue
            viable.append(cell)
        original = self.initial_directions, self.final_directions
        try:
            for cell in sorted(viable, key=estimate)[:64]:
                self.check_budget()
                at = self._point(*cell)
                self.initial_directions, self.final_directions = original[0], None
                first = self._direct_warm_route(start, at, start_layer, None)
                if first is None:
                    continue
                self.initial_directions, self.final_directions = None, original[1]
                second = self._direct_warm_route(at, end, end_layer, None)
                if second is None:
                    continue
                reusable = {start_layer, end_layer} <= self.existing_vias.get(cell, set())
                via = Via(at, start_layer, end_layer)
                result = Route((*first.segments, *second.segments), () if reusable else (via,),
                               (via,) if reusable else (), first.length_mm + second.length_mm,
                               first.bends + second.bends,
                               first.cost + second.cost + (0 if reusable else self.weights.via),
                               0, search_truncated=True)
                self.initial_directions, self.final_directions = original
                if candidate_check is None or candidate_check(result):
                    return result
        finally:
            self.initial_directions, self.final_directions = original
        return None

    def _two_via_escape_clear(
        self, first: tuple[Segment, ...], last: tuple[Segment, ...],
        first_via: Point, last_via: Point,
    ) -> bool:
        """Keep independent escape legs and their opposite vias separated."""
        width = self.profile.maximum_width if self.profile else self.width
        radius = math.ceil((self.via_size + width) / 2)
        if any(_point_near_segment(last_via, part.start, part.end, radius)
               for part in first):
            return False
        if any(_point_near_segment(first_via, part.start, part.end, radius)
               for part in last):
            return False
        return not any(_segments_intersect(a.start, a.end, b.start, b.end)
                       for a in first for b in last)

    def _direct_two_via_route(
        self, start: Point, end: Point, layer: str,
        candidate_check: Callable[[Route], bool] | None,
    ) -> Route | None:
        """Try bounded pad escapes and a straight/chamfered inner-layer path."""
        if len(self.layers) < 2:
            return None

        def candidates(endpoint: Point) -> list[tuple[tuple[int, int], bool]]:
            cx = (endpoint.x - self.lower.x) // self.step
            cy = (endpoint.y - self.lower.y) // self.step
            cells = set()
            # Sample the whole local area, not only compass rays: neighboring
            # pads can make an off-axis escape the only usable via site. Limit
            # the lattice to about 61 cells per axis on very fine grids.
            radius = max(1, math.ceil(mm(3) / self.step))
            stride = max(1, math.ceil(radius / 30))
            for index, x in enumerate(range(max(0, cx - radius), min(self.nx, cx + radius + 1), stride)):
                if index % 8 == 0:
                    self.check_budget()
                for y in range(max(0, cy - radius), min(self.ny, cy + radius + 1), stride):
                    cells.add((x, y))
            for distance_mm in (.5, .8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0):
                radius = max(1, round(mm(distance_mm) / self.step))
                for dx, dy in DIRECTIONS:
                    for ox in (0, 1):
                        for oy in (0, 1):
                            cells.add((cx + radius * dx + ox, cy + radius * dy + oy))
            cells.update(cell for cell in self.existing_vias
                         if max(abs(self._point(*cell).x - endpoint.x),
                                abs(self._point(*cell).y - endpoint.y)) <= mm(3))
            margin = math.ceil(self.via_size / 2) + self.clearance
            result = []
            for x, y in cells:
                if not (0 <= x < self.nx and 0 <= y < self.ny) or not self._inside(x, y, margin):
                    continue
                at = self._point(x, y)
                if at in (start, end) or (x, y) in self.blocked[layer]:
                    continue
                reuse = layer in self.existing_vias.get((x, y), set())
                if not reuse and (not self.allow_new_vias or (x, y) in self.via_blocked):
                    continue
                result.append(((x, y), reuse))
            return sorted(result, key=lambda item: (
                not item[1],
                (self._point(*item[0]).x - endpoint.x) ** 2
                + (self._point(*item[0]).y - endpoint.y) ** 2, item[0]
            ))[:96]

        original = self.initial_directions, self.final_directions
        try:
            escapes = []
            for endpoint, departing in ((start, True), (end, False)):
                found = []
                self.initial_directions = original[0] if departing else None
                self.final_directions = None if departing else original[1]
                for cell, reuse in candidates(endpoint):
                    self.check_budget()
                    at = self._point(*cell)
                    path = (self._direct_warm_route(endpoint, at, layer, None) if departing
                            else self._direct_warm_route(at, endpoint, layer, None))
                    if path is not None:
                        found.append((cell, reuse, path))
                        if len(found) == 8:
                            break
                if not found:
                    return None
                escapes.append(found)
            self.initial_directions = self.final_directions = None
            detours = {}

            def joined(first, last, transit, middle):
                first_cell, first_reuse, first_path = first
                last_cell, last_reuse, last_path = last
                a, b = self._point(*first_cell), self._point(*last_cell)
                vias = (Via(a, layer, transit), Via(b, transit, layer))
                new = tuple(v for v, reuse in strict_zip(vias, (first_reuse, last_reuse)) if not reuse)
                reused = tuple(v for v, reuse in strict_zip(vias, (first_reuse, last_reuse)) if reuse)
                return Route((*first_path.segments, *middle.segments, *last_path.segments),
                             new, reused,
                             first_path.length_mm + middle.length_mm + last_path.length_mm,
                             first_path.bends + middle.bends + last_path.bends,
                             first_path.cost + middle.cost + last_path.cost + len(new) * self.weights.via,
                             middle.expanded, search_truncated=True)

            def accepted(result):
                if self.forbid_staircase and self._staircase_off_pads(result.segments):
                    return False
                self.initial_directions, self.final_directions = original
                try:
                    return candidate_check is None or candidate_check(result)
                finally:
                    self.initial_directions = self.final_directions = None

            for first, last in itertools.product(*escapes):
                first_cell, first_reuse, first_path = first
                last_cell, last_reuse, last_path = last
                a, b = self._point(*first_cell), self._point(*last_cell)
                # Avoid overlapping newly drilled vias, even for the same net.
                if math.hypot(a.x - b.x, a.y - b.y) < self.via_size + self.clearance:
                    continue
                if not self._two_via_escape_clear(first_path.segments, last_path.segments, a, b):
                    continue
                for transit in self.layers:
                    if transit == layer or first_cell in self.blocked[transit] or last_cell in self.blocked[transit]:
                        continue
                    if (first_reuse and transit not in self.existing_vias[first_cell]
                            or last_reuse and transit not in self.existing_vias[last_cell]):
                        continue
                    middle = self._direct_warm_route(a, b, transit, None)
                    if middle is None:
                        pairs = detours.setdefault(transit, [])
                        if len(pairs) < 3:
                            pairs.append((first, last))
                        continue
                    result = joined(first, last, transit, middle)
                    if accepted(result):
                        return result

            # A legal pad escape does not imply a straight corridor between
            # vias. Search a few single-layer detours before returning to the
            # coarser multilayer search. Reuse the same cell cap and reserve
            # at least three quarters of the expansion limit for the fallback.
            remaining = min(40_000, self.max_expansions // 4)
            expanded = 0
            # An integer subdivision must divide the original lattice exactly;
            # otherwise the off-grid retry would lose this expansion slice.
            step = self.step // 2 if self.step % 2 == 0 else self.step
            if (((self.upper.x - self.lower.x) // step + 1)
                    * ((self.upper.y - self.lower.y) // step + 1) > self.max_cells):
                step = self.step
            # Congestion changes search order, never a layer's routing cost.
            # Reserve a share for each remaining layer so early busy layers
            # cannot consume the whole warm-start budget.
            ordered_layers = sorted(detours, key=lambda name: len(self.blocked[name]))
            for layer_index, transit in enumerate(ordered_layers):
                if not remaining:
                    break
                pairs = detours[transit]
                layer_remaining = max(1, remaining // (len(ordered_layers) - layer_index))
                self.check_budget()
                plane = GridRouter(
                    self.lower, self.upper, step, (transit,), self.net,
                    self.width, self.clearance, self.via_size, self.via_drill,
                    self._obstacles, self.weights, self.guide,
                    min(10_000, layer_remaining), self.max_cells,
                    allow_new_vias=False, allowed_reused_vias=set(),
                    layer_penalties=self.layer_penalties,
                    forbid_staircase=self.forbid_staircase,
                    min_segment_length=self.min_segment_length,
                    gentle_turns=self.gentle_turns,
                    enforce_endpoint_spacing=self.enforce_endpoint_spacing,
                    heuristic_weight=self.heuristic_weight,
                    check_budget=self.check_budget, profile=self.profile,
                    first_feasible=True,
                )
                for pair_index, (first, last) in enumerate(pairs):
                    if not remaining or not layer_remaining:
                        break
                    allowance = (layer_remaining if pair_index + 1 == len(pairs)
                                 else max(1, layer_remaining * 2 // 3))
                    a, b = self._point(*first[0]), self._point(*last[0])

                    def compatible(middle):
                        return accepted(joined(first, last, transit, middle))

                    try:
                        middle = plane.route(
                            a, transit, b, transit,
                            _expansion_budget=min(10_000, allowance),
                            candidate_check=compatible,
                        )
                    except (NoGridRoute, SearchLimitReached) as error:
                        used = max(1, error.expanded)
                        remaining = max(0, remaining - used)
                        layer_remaining = max(0, layer_remaining - used)
                        expanded += error.expanded
                        continue
                    result = joined(first, last, transit, middle)
                    return Route(result.segments, result.vias, result.reused_vias,
                                 result.length_mm, result.bends, result.cost,
                                 expanded + middle.expanded, search_truncated=True)
        finally:
            self.initial_directions, self.final_directions = original
        return None

    @staticmethod
    def _intersects_box(a: Point, b: Point, obstacle: Obstacle, margin: int) -> bool:
        """Check a route segment against an inflated copper capsule or box."""
        if obstacle.centerline is not None:
            c, d = obstacle.centerline
            radius = obstacle.copper_radius + margin + 1
            return _segments_intersect(a, b, c, d) or any(
                (
                    _point_near_segment(a, c, d, radius),
                    _point_near_segment(b, c, d, radius),
                    _point_near_segment(c, a, b, radius),
                    _point_near_segment(d, a, b, radius),
                )
            )
        corner = obstacle.corner_radius
        radius = margin + corner + 1
        x0, y0 = obstacle.lower.x + corner, obstacle.lower.y + corner
        x1, y1 = obstacle.upper.x - corner, obstacle.upper.y - corner

        def intersects_rectangle(left, top, right, bottom):
            if (max(a.x, b.x) < left or min(a.x, b.x) > right
                    or max(a.y, b.y) < top or min(a.y, b.y) > bottom):
                return False
            if any(left <= p.x <= right and top <= p.y <= bottom for p in (a, b)):
                return True
            corners = (Point(left, top), Point(right, top),
                       Point(right, bottom), Point(left, bottom))
            return any(_segments_intersect(a, b, first, last)
                       for first, last in zip(corners, (*corners[1:], corners[0])))

        if (intersects_rectangle(x0 - radius, y0, x1 + radius, y1)
                or intersects_rectangle(x0, y0 - radius, x1, y1 + radius)):
            return True
        return any(_point_near_segment(corner, a, b, radius)
                   for corner in (Point(x0, y0), Point(x0, y1), Point(x1, y0), Point(x1, y1)))

    def _endpoint_options(
        self, endpoint: Point, layer: str, *, reverse: bool
    ) -> list[tuple[Point, ...]]:
        return next(self._endpoint_option_groups(endpoint, layer, reverse=reverse), [])

    def _endpoint_inside_minimum_foreign_clearance(self, endpoint: Point, layer: str) -> bool:
        """Prove an off-grid escape is blocked even at the smallest rule widths."""
        return self._minimum_envelope_blocked(endpoint, endpoint, layer)

    def _minimum_envelope_blocked(self, first: Point, last: Point, layer: str) -> bool:
        """Test a necessary, rule-independent minimum swept-copper envelope."""
        width_bound = getattr(self.profile, "minimum_width_for", None) if self.profile else None
        minimum_width = (width_bound(layer) if width_bound else
                         self.profile.minimum_width if self.profile else self.width)
        minimum_clearance = self.profile.minimum_clearance if self.profile else self.clearance
        radius = math.ceil(minimum_width / 2)
        if (not self._inside_point(first, radius + minimum_clearance)
                or not self._inside_point(last, radius + minimum_clearance)):
            return True
        maximum_clearance = self.profile.maximum_clearance if self.profile else self.clearance
        reach = radius + max(maximum_clearance, self.max_obstacle_track_clearance)
        seen = set()
        for cell in self._spatial_cells(first, last, reach):
            for obstacle in self._spatial[layer].get(cell, ()):
                identity = id(obstacle)
                if identity in seen:
                    continue
                seen.add(identity)
                clearance_bound = getattr(self.profile, "minimum_clearance_for", None) if self.profile else None
                clearance = clearance_bound(layer, obstacle) if clearance_bound else minimum_clearance
                margin = radius + obstacle.gap(clearance)
                if self._intersects_box(first, last, obstacle, margin):
                    return True
        return False

    def _minimum_stub_leg(self, endpoint: Point) -> int:
        """Lower bound on the first leg of every nonempty octilinear grid stub."""
        x = (endpoint.x - self.lower.x) % self.step
        y = (endpoint.y - self.lower.y) % self.step
        residues = (x, y, (x + y) % self.step, (x - y) % self.step)
        return min(value for remainder in residues for value in (
            remainder or self.step, self.step - remainder if remainder else self.step,
        ))

    def _endpoint_cannot_begin_stub(self, endpoint: Point, layer: str) -> bool:
        # Longer physical tracks can activate a more permissive local rule.
        # Only minimum envelopes are monotone under every such extension.
        length = self._minimum_stub_leg(endpoint)
        return all(
            self._minimum_envelope_blocked(
                endpoint, Point(endpoint.x + dx * length, endpoint.y + dy * length), layer,
            )
            for dx, dy in DIRECTIONS
        )

    def _endpoint_option_groups(self, endpoint: Point, layer: str, *, reverse: bool):
        """Yield increasingly distant, clear grid escapes only as needed."""
        margin = self.clearance + math.ceil(self.width / 2)
        if not self._inside_point(endpoint, margin):
            raise NoGridRoute("An endpoint is outside the supplied routing bounds")
        dx, dy = endpoint.x - self.lower.x, endpoint.y - self.lower.y
        # Every off-grid stub contains its exact endpoint. If even the least
        # restrictive saved-rule envelope intersects foreign copper there,
        # scanning increasingly distant grid vertices cannot find an escape.
        # Keep on-grid endpoints unchanged so zero-length access and an
        # immediate layer transition remain available to the grid router.
        if (dx % self.step or dy % self.step) and self._endpoint_inside_minimum_foreign_clearance(endpoint, layer):
            return
        if (dx % self.step or dy % self.step) and self._endpoint_cannot_begin_stub(endpoint, layer):
            return
        x0, y0 = dx // self.step, dy // self.step
        x1 = x0 if dx % self.step == 0 else x0 + 1
        y1 = y0 if dy % self.step == 0 else y0 + 1
        # Expand through all grid cells if necessary. A narrow legal corridor
        # can reach its first grid vertex beyond any fixed physical cutoff.
        # Each ring is disjoint; construct only its perimeter instead of
        # repeatedly sorting every cell in the enclosing square.
        max_radius = max(x0, y0, self.nx - 1 - x1, self.ny - 1 - y1, 0)
        for radius in range(max_radius + 1):
            if radius:
                self.check_budget()
            left, right = x0 - radius, x1 + radius
            bottom, top = y0 - radius, y1 + radius
            if radius == 0:
                candidates = itertools.product(range(left, right + 1), range(bottom, top + 1))
            else:
                candidates = itertools.chain(
                    ((x, y) for x in range(left, right + 1) for y in (bottom, top)),
                    ((x, y) for y in range(bottom + 1, top) for x in (left, right)),
                )
            candidates = sorted(
                candidates,
                key=lambda cell: (
                    (self.lower.x + cell[0] * self.step - endpoint.x) ** 2
                    + (self.lower.y + cell[1] * self.step - endpoint.y) ** 2,
                    cell,
                ),
            )
            options = []
            for index, (x, y) in enumerate(candidates):
                if index and index % 128 == 0:
                    self.check_budget()
                if not (0 <= x < self.nx and 0 <= y < self.ny):
                    continue
                if (x, y) in self.blocked[layer] or not self._inside(x, y, margin):
                    continue
                for stub in self._stub_paths(endpoint, self._point(x, y)):
                    if any(not self.edge_clear(a, b, layer) for a, b in zip(stub, stub[1:])):
                        continue
                    oriented = tuple(reversed(stub)) if reverse else stub
                    allowed = self.final_directions if reverse else self.initial_directions
                    if len(oriented) > 1 and allowed is not None:
                        pair = oriented[-2:] if reverse else oriented[:2]
                        if self._direction(*pair) not in allowed:
                            continue
                    options.append(oriented)
            if options:
                yield options

    def _join_off_grid(
        self, start, start_layer, end, end_layer, first, last, base,
        initial_directions, final_directions, enforce_endpoint_spacing,
    ) -> Route | None:
        """Join endpoint escapes to a grid path and reject illegal joins."""
        leading = [Segment(a, b, start_layer) for a, b in zip(first, first[1:])]
        trailing = [Segment(a, b, end_layer) for a, b in zip(last, last[1:])]
        segments = [*leading, *base.segments, *trailing]
        if not segments:
            return None
        directions = [self._direction(s.start, s.end) for s in segments]
        if (
            initial_directions is not None
            and segments[0].layer == start_layer
            and segments[0].start == start
            and directions[0] not in initial_directions
        ) or (
            final_directions is not None
            and segments[-1].layer == end_layer
            and segments[-1].end == end
            and directions[-1] not in final_directions
        ):
            return None
        # A via separates planar copper; its outgoing heading is not a bend.
        turns = [
            (segments[i].end, segments[i].layer, (directions[i + 1] - directions[i]) % 8)
            for i in range(len(segments) - 1)
            if segments[i].layer == segments[i + 1].layer
        ]
        if any(self._illegal_turn(turn, at, layer) for at, layer, turn in turns):
            return None
        bends = sum(bool(turn) for _, _, turn in turns)
        stub_segments = [*leading, *trailing]
        stub_length = sum(
            math.hypot(s.end.x - s.start.x, s.end.y - s.start.y) / NM_PER_MM
            for s in stub_segments
        )
        diagonal_length = sum(
            math.hypot(s.end.x - s.start.x, s.end.y - s.start.y) / NM_PER_MM
            for s in stub_segments if s.end.x != s.start.x and s.end.y != s.start.y
        )
        symmetry_cost = sum(
            math.hypot(s.end.x - s.start.x, s.end.y - s.start.y) / NM_PER_MM
            * _guide_distance(s.end, self.guide) for s in stub_segments
        )
        cost = (
            base.cost + self.weights.length * stub_length
            + sum(self.layer_penalties.get(s.layer, 0)
                  * math.hypot(s.end.x - s.start.x, s.end.y - s.start.y) / NM_PER_MM
                  for s in stub_segments)
            + self.weights.bend * (bends - base.bends)
            + self.weights.diagonal * diagonal_length
            + self.weights.symmetry * symmetry_cost
        )
        via_points = {via.at for via in (*base.vias, *base.reused_vias)}
        merged: list[Segment] = []
        for segment in segments:
            if (
                merged and merged[-1].layer == segment.layer
                and merged[-1].end == segment.start and segment.start not in via_points
                and self._direction(merged[-1].start, merged[-1].end)
                == self._direction(segment.start, segment.end)
            ):
                merged[-1] = Segment(merged[-1].start, segment.end, segment.layer)
            else:
                merged.append(segment)
        if self.forbid_staircase and self._staircase_off_pads(merged):
            return None
        if enforce_endpoint_spacing and any(
            math.hypot(s.end.x - s.start.x, s.end.y - s.start.y) < self.min_segment_length
            and not self.pad_copper.covers(s)
            for s in (merged[0], merged[-1])
        ):
            return None
        if any(
            segment.start not in via_points and segment.end not in via_points
            and math.hypot(segment.end.x - segment.start.x,
                           segment.end.y - segment.start.y) < self.min_segment_length
            and not self.pad_copper.covers(segment)
            for segment in merged[1:-1]
        ):
            return None
        return Route(tuple(merged), base.vias, base.reused_vias,
                     base.length_mm + stub_length, bends, cost, base.expanded)

    def _route_off_grid(
        self, start: Point, start_layer: str, end: Point, end_layer: str,
        *, candidate_check: Callable[[Route], bool] | None = None,
    ) -> Route:
        best = None
        context_rejected = False
        margin = self.clearance + math.ceil(self.width / 2)
        if (
            start_layer == end_layer
            and self._inside_point(start, margin)
            and self._inside_point(end, margin)
        ):
            for path in self._stub_paths(start, end):
                if any(not self.edge_clear(a, b, start_layer) for a, b in zip(path, path[1:])):
                    continue
                segments = tuple(
                    Segment(a, b, start_layer) for a, b in zip(path, path[1:])
                )
                if (
                    self.initial_directions is not None
                    and self._direction(segments[0].start, segments[0].end)
                    not in self.initial_directions
                ) or (
                    self.final_directions is not None
                    and self._direction(segments[-1].start, segments[-1].end)
                    not in self.final_directions
                ):
                    continue
                if self.enforce_endpoint_spacing and any(
                    math.hypot(s.end.x - s.start.x, s.end.y - s.start.y)
                    < self.min_segment_length
                    for s in (segments[0], segments[-1])
                ):
                    continue
                length = sum(
                    math.hypot(s.end.x - s.start.x, s.end.y - s.start.y) / NM_PER_MM
                    for s in segments
                )
                diagonal = sum(
                    math.hypot(s.end.x - s.start.x, s.end.y - s.start.y) / NM_PER_MM
                    for s in segments
                    if s.start.x != s.end.x and s.start.y != s.end.y
                )
                symmetry = sum(
                    math.hypot(s.end.x - s.start.x, s.end.y - s.start.y)
                    / NM_PER_MM
                    * _guide_distance(s.end, self.guide)
                    for s in segments
                )
                bends = max(0, len(segments) - 1)
                cost = (
                    self.weights.length * length
                    + self.layer_penalties.get(start_layer, 0) * length
                    + self.weights.bend * bends
                    + self.weights.diagonal * diagonal
                    + self.weights.symmetry * symmetry
                )
                result = Route(segments, (), (), length, bends, cost, 0)
                if candidate_check is not None and not candidate_check(result):
                    context_rejected = True
                    continue
                if best is None or result.cost < best.cost:
                    best = result
            if best is not None:
                minimum_layer_penalty = min(
                    self.layer_penalties.get(layer, 0) for layer in self.layers
                )
                lower_bound = _octilinear_lower_bound(
                    abs(end.x - start.x),
                    abs(end.y - start.y),
                    1 / NM_PER_MM,
                    self.weights.length + minimum_layer_penalty,
                    self.weights.diagonal,
                )
                if best.cost <= lower_bound + 1e-9 or (
                    not (self.weights.symmetry or self.weights.diagonal)
                    and self.layer_penalties.get(start_layer, 0) <= minimum_layer_penalty
                ):
                    # Costs of bends, vias and symmetry are nonnegative. If a
                    # valid candidate reaches this relaxed bound, more search
                    # cannot improve its objective.
                    return best
                if self.first_feasible:
                    # Board-wide connectivity takes precedence over path-cost
                    # refinement. Mark the remaining alternatives as unsearched.
                    return replace(best, search_truncated=True)
        start_groups = self._endpoint_option_groups(start, start_layer, reverse=False)
        end_groups = self._endpoint_option_groups(end, end_layer, reverse=True)
        try:
            starts = next(start_groups, [])
            ends = next(end_groups, [])
        except SearchBudgetExceeded:
            if best is None:
                raise
            return replace(best, search_truncated=True)
        if not starts or not ends:
            if best is not None:
                return best
            if context_rejected:
                raise SearchLimitReached("Context-rejected paths leave the route unknown")
            raise NoGridRoute(
                "No safe nearby grid connection exists in the supplied obstacle model"
            )
        unresolved = set()
        global_limit_hit = False
        access_truncated = False
        remaining = self.max_expansions

        def goal_seeds() -> frozenset[tuple[int, int, int]]:
            layer = self.layers.index(end_layer)
            return frozenset((*self._index(last[0]), layer) for last in ends)

        grid_cache: dict[tuple, tuple[Route, Route | None] | None] = {}
        allowed_turns = {0, 1, 7} if self.gentle_turns else {0, 1, 3, 5, 7}
        pad_turns = {0, 1, 2, 3, 5, 6, 7}
        pairs = list(itertools.product(starts, ends))
        work = [(first, last, False) for first, last in pairs]
        work.append((None, None, False))
        # In warm-start mode a large endpoint cross-product previously gave
        # every nearby access so few expansions that the wall-clock deadline
        # often expired before any useful search.  Limit the number of shares
        # for the first trials; the hard global expansion/time limits remain.
        pilot_shares = min(len(pairs), 4) if self.first_feasible else len(pairs)
        pilot = (self.max_expansions if len(pairs) == 1 else
                 max(1, self.max_expansions // (4 * pilot_shares)))
        for first, last, retry in work:
            if first is None:
                if best is not None or remaining <= 0:
                    break
                more_starts = next(start_groups, [])
                more_ends = next(end_groups, [])
                if not more_starts and not more_ends:
                    break
                alternatives = (
                    itertools.product(more_starts, ends),
                    itertools.product(starts, more_ends),
                    itertools.product(more_starts, more_ends),
                )
                # Interleave the three expansion types and cap each ring to
                # avoid materializing an unbounded endpoint cross-product.
                candidates = (
                    pair for group in itertools.zip_longest(*alternatives)
                    for pair in group if pair is not None
                )
                pair_limit = min(2048, self.max_expansions)
                pairs = list(itertools.islice(candidates, pair_limit + 1))
                if len(pairs) > pair_limit:
                    access_truncated = True
                    pairs.pop()
                starts.extend(more_starts)
                ends.extend(more_ends)
                pilot_shares = min(len(pairs), 4) if self.first_feasible else len(pairs)
                pilot = max(1, self.max_expansions // (4 * max(1, pilot_shares)))
                work.extend((a, b, False) for a, b in pairs)
                work.append((None, None, False))
                continue
            try:
                self.check_budget()
            except SearchBudgetExceeded:
                if best is None:
                    raise
                return replace(
                    best,
                    search_truncated=True,
                    expanded=self.max_expansions - remaining,
                )
            grid_start, grid_end = first[-1], last[0]
            initial = self.initial_directions
            final = self.final_directions
            # Inside same-net pad copper an escape may turn any way but back.
            if len(first) > 1:
                incoming = self._direction(first[-2], first[-1])
                turns = pad_turns if self.pad_copper.holds(first[-1], start_layer) else allowed_turns
                initial = frozenset(d for d in range(8) if (d - incoming) % 8 in turns)
            if len(last) > 1:
                outgoing = self._direction(last[0], last[1])
                turns = pad_turns if self.pad_copper.holds(last[0], end_layer) else allowed_turns
                final = frozenset(d for d in range(8) if (outgoing - d) % 8 in turns)
            spacing = self.enforce_endpoint_spacing and len(first) == len(last) == 1
            key = grid_start, grid_end, initial, final, spacing
            if candidate_check is not None:
                # The same grid route may join one escape legally and another
                # illegally; context-sensitive results cannot share that cache.
                key = (*key, tuple(first), tuple(last))
            if key not in grid_cache:
                if remaining <= 0:
                    global_limit_hit = True
                    break
                joined = None
                try:
                    if grid_start == grid_end and start_layer == end_layer:
                        base = Route((), (), (), 0.0, 0, 0.0, 0)
                    else:
                        saved = (self.initial_directions, self.final_directions,
                                 self.enforce_endpoint_spacing, self._goal_seeds)
                        accepted = []

                        def valid_join(candidate):
                            result = self._join_off_grid(
                                start, start_layer, end, end_layer, first, last, candidate,
                                saved[0], saved[1], saved[2],
                            )
                            if result is None or not candidate_check(result):
                                return False
                            accepted.append(result)
                            return True

                        try:
                            self.initial_directions, self.final_directions = initial, final
                            self.enforce_endpoint_spacing = spacing
                            # One reverse map per endpoint serves every access pair.
                            self._goal_seeds = goal_seeds()
                            base = self.route(
                                grid_start,
                                start_layer,
                                grid_end,
                                end_layer,
                                _expansion_budget=remaining if retry else min(remaining, pilot),
                                candidate_check=valid_join if candidate_check is not None else None,
                            )
                            if accepted:
                                joined = accepted[-1]
                        finally:
                            (self.initial_directions, self.final_directions,
                             self.enforce_endpoint_spacing, self._goal_seeds) = saved
                except SearchLimitReached as exc:
                    unresolved.add(key)
                    remaining = max(0, remaining - exc.expanded)
                    if not retry and remaining:
                        work.insert(len(work) - 1, (first, last, True))
                    if remaining <= 0:
                        global_limit_hit = True
                        break
                    continue
                except SearchBudgetExceeded:
                    if best is None:
                        raise
                    return replace(
                        best,
                        search_truncated=True,
                        expanded=self.max_expansions - remaining,
                    )
                except NoGridRoute as exc:
                    remaining -= exc.expanded
                    unresolved.discard(key)
                    grid_cache[key] = None
                    continue
                remaining -= base.expanded
                unresolved.discard(key)
                grid_cache[key] = (base, joined)
            else:
                cached = grid_cache[key]
                if cached is None:
                    continue
                base, joined = cached
            result = joined or self._join_off_grid(
                start, start_layer, end, end_layer, first, last, base,
                self.initial_directions, self.final_directions, self.enforce_endpoint_spacing,
            )
            if (result is None and candidate_check is None
                    and (grid_start != grid_end or start_layer != end_layer)):
                # The cached cheapest grid path may form an illegal staircase
                # with these particular endpoint escapes. Retry the same access
                # with the completed join checked while exploring grid goals.
                if remaining <= 0:
                    global_limit_hit = True
                    break
                saved = (self.initial_directions, self.final_directions,
                         self.enforce_endpoint_spacing, self._goal_seeds)
                alternatives = []

                def legal_join(candidate):
                    joined_candidate = self._join_off_grid(
                        start, start_layer, end, end_layer, first, last, candidate,
                        saved[0], saved[1], saved[2],
                    )
                    if joined_candidate is None:
                        return False
                    alternatives.append(joined_candidate)
                    return True

                try:
                    self.initial_directions, self.final_directions = initial, final
                    self.enforce_endpoint_spacing = spacing
                    self._goal_seeds = goal_seeds()
                    alternative = self.route(
                        grid_start, start_layer, grid_end, end_layer,
                        _expansion_budget=remaining, candidate_check=legal_join,
                    )
                except SearchLimitReached as exc:
                    remaining = max(0, remaining - exc.expanded)
                    unresolved.add((*key, tuple(first), tuple(last)))
                    if remaining <= 0:
                        global_limit_hit = True
                        break
                    continue
                except NoGridRoute as exc:
                    remaining = max(0, remaining - exc.expanded)
                    continue
                except SearchBudgetExceeded:
                    if best is None:
                        raise
                    return replace(best, search_truncated=True,
                                   expanded=self.max_expansions - remaining)
                finally:
                    (self.initial_directions, self.final_directions,
                     self.enforce_endpoint_spacing, self._goal_seeds) = saved
                remaining -= alternative.expanded
                result = alternatives[-1] if alternatives else None
            if result is None:
                continue
            if joined is None and candidate_check is not None and not candidate_check(result):
                context_rejected = True
                continue
            if best is None or result.cost < best.cost:
                best = result
            if self.first_feasible:
                return replace(best, search_truncated=True,
                               expanded=self.max_expansions - remaining)
        if best is None:
            if unresolved or global_limit_hit or access_truncated or context_rejected:
                raise SearchLimitReached(
                    "Endpoint-grid searches reached their limit; the route remains unknown",
                    expanded=self.max_expansions - remaining,
                )
            raise NoGridRoute(
                "No route found among nearby endpoint-grid connections",
                expanded=self.max_expansions - remaining,
            )
        return replace(
            best, search_truncated=bool(unresolved) or global_limit_hit or access_truncated,
            expanded=self.max_expansions - remaining
        )

    @classmethod
    def _runs(cls, segments: Iterable[Segment]) -> list[Segment]:
        """Merge connected collinear pieces into directional runs."""
        merged: list[Segment] = []
        for segment in segments:
            direction = cls._direction(segment.start, segment.end)
            if (
                merged
                and merged[-1].layer == segment.layer
                and merged[-1].end == segment.start
                and cls._direction(merged[-1].start, merged[-1].end) == direction
            ):
                merged[-1] = Segment(merged[-1].start, segment.end, segment.layer)
            else:
                merged.append(segment)
        return merged

    @classmethod
    def staircase_windows(cls, segments: list[Segment]) -> tuple[tuple[Segment, Segment, Segment], ...]:
        """Find local staircase windows after merging collinear chain pieces."""
        merged = cls._runs(segments)
        windows = []
        for index, (first, middle, last) in enumerate(zip(merged, merged[1:], merged[2:])):
            if not (
                first.layer == middle.layer == last.layer
                and first.end == middle.start
                and middle.end == last.start
            ):
                continue
            directions = [cls._direction(s.start, s.end) for s in (first, middle, last)]
            if is_staircase_triplet(*directions):
                context = merged[index - 1:index + 4] if index else []
                if (len(context) == 5 and len({s.layer for s in context}) == 1
                        and all(a.end == b.start for a, b in zip(context, context[1:]))
                        and is_standard_dogleg(cls._direction(s.start, s.end) for s in context)):
                    continue
                windows.append((first, middle, last))
        return tuple(windows)

    @classmethod
    def staircase_count(cls, segments: list[Segment]) -> int:
        """Count overlapping staircase windows in an oriented segment chain."""
        return len(cls.staircase_windows(segments))

    @classmethod
    def _has_staircase(cls, segments: list[Segment]) -> bool:
        """Reject candidate chains containing any staircase window."""
        return cls.staircase_count(segments) > 0

    def _staircase_off_pads(self, segments: Iterable[Segment]) -> bool:
        """Judge each track chain between same-net pad copper, as the saved-board style check does."""
        chain: list[Segment] = []
        for segment in self._runs(segments):
            if chain and self.pad_copper.holds(segment.start, segment.layer):
                if self._has_staircase(chain):
                    return True
                chain = []
            chain.append(segment)
        return self._has_staircase(chain)

    def _illegal_turn(self, turn: int, at: Point, layer: str) -> bool:
        """Reversals are never legal; inside same-net pad copper any other turn is."""
        return turn == 4 or not self.pad_copper.holds(at, layer) and (
            turn in {2, 6} or (self.gentle_turns and turn in {3, 5}))

    def route(
        self,
        start: Point,
        start_layer: str,
        end: Point,
        end_layer: str,
        *,
        _expansion_budget: int | None = None,
        candidate_check: Callable[[Route], bool] | None = None,
    ) -> Route:
        if start_layer not in self.layers or end_layer not in self.layers:
            raise ValueError("Endpoint layers must be enabled for routing")
        if start == end and start_layer == end_layer:
            raise ValueError("Start and end must be different")
        if candidate_check is not None and not callable(candidate_check):
            raise ValueError("candidate_check must be callable")
        if self.first_feasible and start_layer == end_layer:
            direct = self._direct_warm_route(start, end, start_layer, candidate_check)
            if direct is not None:
                return direct
            if _expansion_budget is None:
                direct = self._direct_two_via_route(start, end, start_layer, candidate_check)
                if direct is not None:
                    return direct
        elif self.first_feasible and _expansion_budget is None:
            direct = self._direct_one_via_route(start, start_layer, end, end_layer, candidate_check)
            if direct is not None:
                return direct
        if any(
            (coordinate - origin) % self.step
            for coordinate, origin in (
                (start.x, self.lower.x),
                (start.y, self.lower.y),
                (end.x, self.lower.x),
                (end.y, self.lower.y),
            )
        ):
            return self._route_off_grid(start, start_layer, end, end_layer,
                                        candidate_check=candidate_check)
        sx, sy = self._index(start)
        ex, ey = self._index(end)
        if (sx, sy, start_layer) == (ex, ey, end_layer):
            raise ValueError("Start and end must be different")
        edge_margin = self.clearance + math.ceil(self.width / 2)
        for x, y, layer in ((sx, sy, start_layer), (ex, ey, end_layer)):
            if (x, y) in self.blocked[layer] or not self._inside(x, y, edge_margin):
                raise NoGridRoute(
                    "An endpoint conflicts with the supplied obstacle model or bounds"
                )
        # A clear, same-layer cardinal line on a least-cost layer reaches the
        # global length lower bound when no symmetry guide is charged.
        if (start_layer == end_layer and (sx == ex or sy == ey)
                and not self.weights.symmetry
                and self.layer_penalties.get(start_layer, 0)
                == min(self.layer_penalties.get(layer, 0) for layer in self.layers)):
            direction = self._direction(start, end)
            allowed = ((self.initial_directions is None or direction in self.initial_directions)
                       and (self.final_directions is None or direction in self.final_directions))
            long_enough = (not self.enforce_endpoint_spacing
                           or math.hypot(end.x - start.x, end.y - start.y) >= self.min_segment_length)
            if allowed and long_enough:
                self.check_budget()
                if self.edge_clear(start, end, start_layer):
                    length = math.hypot(end.x - start.x, end.y - start.y) / NM_PER_MM
                    direct = Route((Segment(start, end, start_layer),), (), (), length, 0,
                                   length * (self.weights.length + self.layer_penalties.get(start_layer, 0)), 0)
                    if candidate_check is None or candidate_check(direct):
                        return direct
        start_state = (sx, sy, self.layers.index(start_layer), -1, -1, 0)
        max_run = math.ceil(self.min_segment_length / self.step)

        def long_enough(direction: int, steps: int) -> bool:
            if not steps or direction == -1:
                return True
            distance = steps * self.step * (math.sqrt(2) if direction % 2 else 1)
            return distance + 1e-6 >= self.min_segment_length

        goal_layer = self.layers.index(end_layer)
        serial = itertools.count()
        step_mm = to_mm(self.step)
        minimum_layer_penalty = min(
            self.layer_penalties.get(name, 0) for name in self.layers
        )
        topology = None
        if self.topology_heuristic and len(self.layers) > 1:
            from pcb.topology_bound import relaxed_costs

            goal = (ex, ey, goal_layer)
            seeds = self._goal_seeds if goal in self._goal_seeds else frozenset({goal})
            if seeds not in self._topology_cache:
                self._topology_cache[seeds] = relaxed_costs(
                    self, goal, stop_at=(sx, sy, start_state[2]),
                    max_expansions=min(50_000, max(1, self.max_expansions // 4)),
                    more_goals=tuple(sorted(seeds - {goal})),
                )
                if len(self._topology_cache) > 2:
                    self._topology_cache.popitem(last=False)
            self._topology_cache.move_to_end(seeds)
            topology = self._topology_cache[seeds]
            area = self.nx * self.ny
            if math.isinf(topology[start_state[2] * area + sy * self.nx + sx]):
                raise NoGridRoute("No route exists in the relaxed grid topology")

        def relaxed_cost(x: int, y: int, layer: int) -> float:
            if topology is None:
                return 0
            return topology[layer * self.nx * self.ny + y * self.nx + x]

        def heuristic(x: int, y: int, layer: int) -> float:
            dx, dy = abs(ex - x), abs(ey - y)
            straight = _octilinear_lower_bound(
                dx,
                dy,
                step_mm,
                self.weights.length + minimum_layer_penalty,
                self.weights.diagonal,
            )
            # Allow for floating-point summation order between reverse and forward search.
            return self.heuristic_weight * max(
                straight, relaxed_cost(x, y, layer) - 1e-8
            )

        queue = [(heuristic(sx, sy, start_state[2]), 0.0, next(serial), start_state)]
        distances = {start_state: 0.0}
        previous: dict[tuple[int, ...], tuple[int, ...]] = {}
        expanded = 0
        final = None
        rejected_context = False
        while queue:
            _, cost, _, state = heapq.heappop(queue)
            if cost != distances.get(state):
                continue
            x, y, layer_index, old_direction, previous_direction, run_steps = state
            if (
                (x, y, layer_index) == (ex, ey, goal_layer)
                and (
                    self.final_directions is None
                    or old_direction == -1
                    or old_direction in self.final_directions
                )
                and (
                    not self.enforce_endpoint_spacing
                    or long_enough(old_direction, run_steps)
                )
            ):
                if candidate_check is not None or self.forbid_staircase:
                    self.check_budget()
                    candidate = self._materialize_grid_route(state, start_state, previous, distances, expanded)
                    if (candidate_check(candidate) if candidate_check is not None
                            else not self._staircase_off_pads(candidate.segments)):
                        return candidate
                    rejected_context = True
                    continue
                final = state
                break
            expanded += 1
            if expanded % 128 == 0:
                self.check_budget()
            if expanded > (
                self.max_expansions if _expansion_budget is None else _expansion_budget
            ):
                raise SearchLimitReached(
                    "Search limit reached; the route remains unknown",
                    expanded=expanded - 1,
                )
            layer = self.layers[layer_index]
            for direction, (dx, dy) in enumerate(DIRECTIONS):
                if (
                    state == start_state
                    and self.initial_directions is not None
                    and direction not in self.initial_directions
                ):
                    continue
                turn = 0 if old_direction == -1 else (direction - old_direction) % 8
                if turn in {2, 4, 6} or (self.gentle_turns and turn in {3, 5}):
                    continue
                next_previous = (_advance_staircase_history(
                    previous_direction, old_direction, direction,
                    (layer_index, x, y) in self._pad_cells,
                ) if self.forbid_staircase else -1)
                if next_previous is None:
                    continue
                if turn and not long_enough(old_direction, run_steps):
                    continue
                nx, ny = x + dx, y + dy
                if not (0 <= nx < self.nx and 0 <= ny < self.ny):
                    continue
                if (nx, ny) in self.blocked[layer] or not self._inside(
                    nx, ny, edge_margin
                ):
                    continue
                if topology is not None and math.isinf(
                    relaxed_cost(nx, ny, layer_index)
                ):
                    continue
                if not self.edge_clear(self._point(x, y), self._point(nx, ny), layer):
                    continue
                length = step_mm * (math.sqrt(2) if dx and dy else 1.0)
                extra = (
                    self.weights.length + self.layer_penalties.get(layer, 0)
                ) * length
                if turn:
                    extra += self.weights.bend
                if dx and dy:
                    extra += self.weights.diagonal * length
                extra += (
                    self.weights.symmetry
                    * length
                    * _guide_distance(self._point(nx, ny), self.guide)
                )
                next_run = min(max_run, (1 if turn else run_steps + 1))
                nxt = (nx, ny, layer_index, direction, next_previous, next_run)
                self._offer(
                    queue,
                    distances,
                    previous,
                    serial,
                    state,
                    nxt,
                    cost + extra,
                    heuristic,
                )
            via_edge = self.clearance + math.ceil(self.via_size / 2)
            if (
                self._inside(x, y, via_edge)
                and long_enough(old_direction, run_steps)
                # A through via reaches every enabled layer directly. Do not
                # cross twice without copper between the transitions to reset
                # a same-layer heading and evade the bend rule.
                and (old_direction != -1 or state == start_state)
            ):
                for other, other_layer in enumerate(self.layers):
                    if other != layer_index and (x, y) not in self.blocked[other_layer]:
                        if topology is not None and math.isinf(
                            relaxed_cost(x, y, other)
                        ):
                            continue
                        reuse = {layer, other_layer} <= self.existing_vias.get(
                            (x, y), set()
                        )
                        if not reuse and (
                            not self.allow_new_vias
                            or (x, y) in self.via_blocked
                            or (x, y) in {(sx, sy), (ex, ey)}
                        ):
                            continue
                        nxt = (x, y, other, -1, -1, 0)
                        self._offer(
                            queue,
                            distances,
                            previous,
                            serial,
                            state,
                            nxt,
                            cost + (0 if reuse else self.weights.via),
                            heuristic,
                        )
        if final is None:
            if rejected_context:
                raise SearchLimitReached(
                    "Context-rejected grid paths may have unsearched alternatives",
                    expanded=expanded,
                )
            raise NoGridRoute(
                "No route found in the supplied grid and obstacle model",
                expanded=expanded,
            )
        return self._materialize_grid_route(final, start_state, previous, distances, expanded)

    def _materialize_grid_route(self, final, start_state, previous, distances, expanded) -> Route:
        """Reconstruct one goal path for cost and context checks."""
        step_mm = to_mm(self.step)
        states = [final]
        while states[-1] != start_state:
            states.append(previous[states[-1]])
        states.reverse()
        segments: list[Segment] = []
        vias: list[Via] = []
        reused_vias: list[Via] = []
        length_mm = 0.0
        bends = 0
        prior_direction = -1
        last_was_via = False
        for first, last in zip(states, states[1:]):
            x, y, layer, _, _, _ = first
            nx, ny, new_layer, direction, _, _ = last
            a, b = self._point(x, y), self._point(nx, ny)
            if layer != new_layer:
                via = Via(a, self.layers[layer], self.layers[new_layer])
                if {via.from_layer, via.to_layer} <= self.existing_vias.get(
                    (x, y), set()
                ):
                    reused_vias.append(via)
                else:
                    vias.append(via)
                prior_direction = -1
                last_was_via = True
                continue
            length_mm += math.hypot(nx - x, ny - y) * step_mm
            if prior_direction != -1 and direction != prior_direction:
                bends += 1
            if (
                segments
                and not last_was_via
                and prior_direction == direction
                and segments[-1].layer == self.layers[layer]
                and segments[-1].end == a
            ):
                segments[-1] = Segment(segments[-1].start, b, self.layers[layer])
            else:
                segments.append(Segment(a, b, self.layers[layer]))
            prior_direction = direction
            last_was_via = False
        return Route(
            tuple(segments),
            tuple(vias),
            tuple(reused_vias),
            length_mm,
            bends,
            distances[final],
            expanded,
        )

    @staticmethod
    def _offer(
        queue, distances, previous, serial, current, nxt, cost, heuristic
    ) -> None:
        if cost + 1e-12 < distances.get(nxt, math.inf):
            distances[nxt] = cost
            previous[nxt] = current
            heapq.heappush(
                queue,
                (cost + heuristic(nxt[0], nxt[1], nxt[2]), cost, next(serial), nxt),
            )
