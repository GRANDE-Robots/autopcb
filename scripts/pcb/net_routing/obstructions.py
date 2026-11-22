"""Find copper-removal hypotheses with the existing router, then price repairs.

A relaxed path is diagnostic evidence only. It is never materialized: the normal
router must rebuild the target and every damaged net with all rules enabled.
"""

from __future__ import annotations

from core.pcb_io import enabled_copper_layers

import heapq
import itertools
import math
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from pcb.net_routing.checkpoint import route_access_inset, search_window
from pcb.net_routing.reference import reference_corridors
from pcb.net_routing.rules import RoutingRules
from pcb.route_native import _box, _pad_obstacle, _track_boxes, _via_obstacle
from pcb.route_optimizer import (
    DIRECTIONS,
    GridRouter,
    NoGridRoute,
    Point,
    Route,
    SearchBudgetExceeded,
    SearchLimitReached,
    Segment,
    Weights,
    mm,
)


@dataclass(frozen=True, slots=True)
class Corridor:
    """A relaxed diagnostic route: where the blocked net wants to pass."""

    route: Route
    profile: object
    target: str
    via_size: int
    clearance: int

    def blocked_by(self, shapes) -> bool:
        return bool(obstruction_hits(self.route, {"shapes": tuple(shapes)}, self.profile,
                                     self.target, self.via_size, self.clearance))


@dataclass(frozen=True, slots=True)
class RepairWitness:
    identifiers: frozenset[str]
    route_cost: float
    change_cost: float
    corridor: Corridor | None = field(default=None, compare=False)

    @property
    def score(self) -> float:
        return self.route_cost + self.change_cost


def obstruction_hits(route, soft, profile, target, via_size, clearance):
    """Identify removable copper hit by a diagnostic path, not by name alone."""
    hit = set()
    for uid, shapes in soft.items():
        if any(
            shape.net != target and segment.layer in shape.layers
            and GridRouter._intersects_box(
                piece.start, piece.end, shape,
                (width + 1) // 2 + max(
                    shape.min_track_clearance, profile.clearance_at(piece, shape, width=width)
                ),
            )
            for segment in route.segments
            for piece, width in profile.pieces(segment)
            for shape in shapes
        ):
            hit.add(uid)
        if any(
            (shape.net != target or shape.kind in {"pad", "via"})
            and GridRouter._intersects_box(
                via.at, via.at, shape, (via_size + 1) // 2 + max(
                    clearance, shape.min_track_clearance, shape.min_via_clearance
                ),
            )
            for via in route.vias
            for shape in shapes
        ):
            hit.add(uid)
    return hit


def local_escape_witnesses(router, anchor, soft, costs, check, *, max_expanded=5000):
    """Find removable copper along relaxed local pad-to-via hypotheses.

    The search ignores bend rules and never creates copper. Every returned
    witness must be rerouted and independently checked by the normal pipeline.
    The router contains all non-removable obstacles; ``soft`` contains only
    items that the caller is permitted to propose for coordinated replacement.
    """
    layer, endpoint = anchor.layer, anchor.point
    if layer not in router.layers:
        return []
    groups = router._endpoint_option_groups(endpoint, layer, reverse=False)
    options = []
    for group in groups:
        options.extend(group)
        if len(options) >= 16:
            break
    queue, paths, previous = deque(), {}, {}
    for option in options[:32]:
        cell = router._index(option[-1])
        if cell not in paths:
            paths[cell] = option
            queue.append(cell)
    found, expanded = {}, 0
    via_margin = (router.via_size + 1) // 2 + router.clearance
    while queue and expanded < max_expanded:
        cell = queue.popleft()
        expanded += 1
        if expanded % 64 == 0:
            check()
        x, y = cell
        at = router._point(x, y)
        if (at != endpoint and cell not in router.via_blocked
                and router._inside(x, y, via_margin)):
            trail = [cell]
            while trail[-1] in previous:
                trail.append(previous[trail[-1]])
            trail.reverse()
            points = (*paths[trail[0]], *(router._point(*item) for item in trail[1:]))
            segments = tuple(Segment(a, b, layer) for a, b in zip(points, points[1:]) if a != b)
            hit = set()
            for index, (uid, shapes) in enumerate(soft.items()):
                if index % 16 == 0:
                    check()
                for shape in shapes:
                    if shape.net != router.net and layer in shape.layers:
                        for segment in segments:
                            pieces = (router.profile.pieces(segment) if router.profile
                                      else ((segment, router.width),))
                            if any(GridRouter._intersects_box(
                                part.start, part.end, shape,
                                (width + 1) // 2 + max(
                                    shape.min_track_clearance,
                                    router.profile.clearance_at(part, shape, width=width)
                                    if router.profile else router.clearance,
                                ),
                            ) for part, width in pieces):
                                hit.add(uid)
                                break
                    if (shape.net != router.net or shape.kind == "via") and GridRouter._intersects_box(
                        at, at, shape, (router.via_size + 1) // 2 + max(
                            router.clearance, shape.min_track_clearance, shape.min_via_clearance
                        ),
                    ):
                        hit.add(uid)
                    if uid in hit:
                        break
            if not hit:
                # A clear relaxed local escape exists; local copper removal
                # is not justified by this hypothesis.
                return []
            identity = frozenset(hit)
            length = sum(math.hypot(s.end.x - s.start.x, s.end.y - s.start.y)
                         for s in segments) / 1e6
            witness = RepairWitness(identity, router.weights.length * length + router.weights.via,
                                    sum(costs[uid] for uid in identity))
            found.setdefault(identity, witness)
            if len(found) >= 4:
                break
        for dx, dy in DIRECTIONS:
            nx, ny = x + dx, y + dy
            next_cell = (nx, ny)
            if (not 0 <= nx < router.nx or not 0 <= ny < router.ny
                    or next_cell in paths or next_cell in previous
                    or next_cell in router.blocked[layer]
                    or not router._inside(nx, ny, router.clearance + (router.width + 1) // 2)):
                continue
            if router.edge_clear(at, router._point(nx, ny), layer):
                previous[next_cell] = cell
                queue.append(next_cell)
    return sorted(found.values(), key=lambda item: (item.score, sorted(item.identifiers)))


def diagnose(board, anchors, records, settings, recovery, critical_nets, weights, *, movable=None):
    """Sample alternate corridors, retaining exact obstructing copper UUIDs.

    ``movable`` maps footprint UUIDs to a move cost. Their pads are relaxed
    like eligible copper, so a witness can name parts as well as copper.
    """
    import pcbnew

    started = time.monotonic()
    deadline = started + recovery.obstruction_seconds

    def check():
        if time.monotonic() >= deadline:
            raise SearchBudgetExceeded("Obstruction diagnosis time limit")

    target = settings["name"]
    physical = enabled_copper_layers(board)
    layers = tuple(settings.get("layers", physical))
    ids = {record.uuid for record in records}
    movable = movable or {}
    hard, soft, costs = [], {}, {}
    for pad in board.GetPads():
        active = tuple(
            layer for layer in physical if pad.IsOnLayer(board.GetLayerID(layer))
        )
        if pad.GetDrillSize().x or pad.GetDrillSize().y:
            active = physical
        if not active:
            continue
        shape = _pad_obstacle(board, pad, active)
        owner = pad.GetParentFootprint().m_Uuid.AsString()
        if owner in movable:
            soft[owner] = (*soft.get(owner, ()), shape)
            costs[owner] = movable[owner]
        else:
            hard.append(shape)
    for item in board.GetTracks():
        active = tuple(
            layer for layer in physical if item.IsOnLayer(board.GetLayerID(layer))
        )
        if not active:
            continue
        is_via = isinstance(item, pcbnew.PCB_VIA)
        shapes = [(_via_obstacle(board, item, active) if is_via
                   else _box(item, active, item.GetNetname(), "copper"))]
        if not is_via and not isinstance(item, pcbnew.PCB_ARC):
            shapes = _track_boxes(item, active, item.GetNetname())
        uid = item.m_Uuid.AsString()
        if uid in ids:
            soft[uid] = tuple(shapes)
            costs[uid] = (
                weights.via
                if is_via
                else weights.track + weights.length_mm * item.GetLength() / 1e6
            )
        else:
            hard.extend(shapes)
    for zone in board.Zones():
        active = tuple(
            layer for layer in physical if zone.IsOnLayer(board.GetLayerID(layer))
        )
        if active and (
            zone.GetIsRuleArea() or not settings.get("assume_zone_refill", False)
        ):
            hard.append(
                _box(
                    zone,
                    active,
                    "" if zone.GetIsRuleArea() else zone.GetNetname(),
                    "keepout" if zone.GetIsRuleArea() else "copper",
                )
            )
    hard.extend(reference_corridors(board, set(critical_nets)).obstacles)
    box = board.GetBoardEdgesBoundingBox()
    bounds, step = search_window(
        anchors,
        (
            box.GetX() / 1e6,
            box.GetY() / 1e6,
            box.GetRight() / 1e6,
            box.GetBottom() / 1e6,
        ),
        recovery.wide_margin_mm,
        settings.get("step_mm", 0.1),
        len(layers),
        recovery.max_cells,
        access_inset=route_access_inset(settings),
    )
    width, clearance = (
        mm(settings.get("width_mm", 0.25)),
        mm(settings.get("clearance_mm", 0.2)),
    )
    via = settings.get("via", {})
    size, drill = mm(via.get("size_mm", 0.6)), mm(via.get("drill_mm", 0.3))
    rules = RoutingRules.from_board(
        board, Path(board.GetFileName()).with_suffix(".kicad_dru")
    )
    profile = rules.profile(
        target,
        width,
        clearance,
        width_floor=width if settings.get("current_model") else 0,
    )
    queue, seen, found, notes = [], set(), {}, []
    serial = itertools.count()
    heapq.heappush(queue, (0.0, next(serial), frozenset()))
    for _ in range(recovery.obstruction_trials):
        if not queue:
            break
        _, _, retained = heapq.heappop(queue)
        if retained in seen:
            continue
        seen.add(retained)
        try:
            check()
            router = GridRouter(
                Point.from_mm(bounds[:2]),
                Point.from_mm(bounds[2:]),
                mm(step),
                layers,
                target,
                width,
                clearance,
                size,
                drill,
                (*hard, *(shape for uid in retained for shape in soft[uid])),
                weights=Weights(**settings.get("weights", {})),
                max_cells=recovery.max_cells,
                max_expansions=recovery.max_expansions,
                layer_penalties=settings.get("layer_penalties", {}),
                check_budget=check,
                profile=profile,
                gentle_turns=True,
                forbid_staircase=True,
                heuristic_weight=recovery.heuristic_weight,
            )
            route = router.route(
                anchors[0].point, anchors[0].layer, anchors[-1].point, anchors[-1].layer
            )
            hit = obstruction_hits(route, soft, profile, target, size, clearance)
            identity = frozenset(hit)
            if identity:
                witness = RepairWitness(
                    identity, route.cost, sum(costs[uid] for uid in identity),
                    Corridor(route, profile, target, size, clearance),
                )
                found.setdefault(identity, witness)
                # Keep each expensive obstruction fixed in alternate searches.
                # This compares alternative removal sets instead of just the nearest item.
                for uid in sorted(hit, key=lambda value: (-costs[value], value))[
                    : recovery.obstruction_trials
                ]:
                    alternative = retained | {uid}
                    heapq.heappush(queue, (witness.score, next(serial), alternative))
            else:
                notes.append(
                    "A sampled corridor needs no copper removal; the detailed search remains responsible for connectivity"
                )
        except (NoGridRoute, SearchLimitReached, SearchBudgetExceeded) as error:
            notes.append(str(error))
        except ValueError as error:
            notes.append(f"Obstruction model unavailable: {error}")
    if not found and soft:
        # A full-board relaxed route may spend its entire budget far from a
        # trapped terminal. Probe each endpoint in a small window with only
        # eligible copper relaxed; hard pads, keepouts and reference corridors
        # remain obstacles. This is a removal hypothesis, not a routed result.
        local_deadline = time.monotonic() + min(3.0, recovery.obstruction_seconds)

        def local_check():
            if time.monotonic() >= local_deadline:
                raise SearchBudgetExceeded("Local obstruction diagnosis time limit")

        board_bounds = (
            box.GetX() / 1e6, box.GetY() / 1e6,
            box.GetRight() / 1e6, box.GetBottom() / 1e6,
        )
        try:
            for anchor in anchors:
                local_check()
                window, local_step = search_window(
                    (anchor,), board_bounds, 4.0, settings.get("step_mm", 0.1), 1,
                    min(recovery.max_cells, 100000), access_inset=route_access_inset(settings),
                )
                lower, upper = Point.from_mm(window[:2]), Point.from_mm(window[2:])

                def in_window(shape):
                    margin = max(
                        (size + 1) // 2 + max(clearance, shape.min_track_clearance,
                                               shape.min_via_clearance),
                        (profile.maximum_width + 1) // 2 + max(
                            profile.maximum_clearance, shape.min_track_clearance
                        ),
                    )
                    return not (shape.upper.x + margin < lower.x or shape.lower.x - margin > upper.x
                                or shape.upper.y + margin < lower.y or shape.lower.y - margin > upper.y)

                local_soft = {uid: tuple(shape for shape in shapes if in_window(shape))
                              for uid, shapes in soft.items()}
                local_soft = {uid: shapes for uid, shapes in local_soft.items() if shapes}
                if not local_soft:
                    continue
                router = GridRouter(
                    lower, upper, mm(local_step), (anchor.layer,), target, width, clearance,
                    size, drill, tuple(shape for shape in hard if in_window(shape)),
                    weights=Weights(**settings.get("weights", {})),
                    max_cells=min(recovery.max_cells, 100000), check_budget=local_check,
                    profile=profile,
                )
                for witness in local_escape_witnesses(router, anchor, local_soft, costs, local_check):
                    found.setdefault(witness.identifiers, witness)
        except (NoGridRoute, SearchBudgetExceeded) as error:
            notes.append(str(error))
    return sorted(
        found.values(), key=lambda item: (item.score, sorted(item.identifiers))
    ), sorted(set(notes))[:4]
