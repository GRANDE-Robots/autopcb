"""Optimistic lower bound on required new vias for a grid search."""

from __future__ import annotations

from array import array
from collections import deque
import heapq
import math

from pcb.route_optimizer import DIRECTIONS, GridRouter


UNREACHABLE = 65535


def grid_bounds(router: GridRouter, margin: int) -> tuple[int, int, int, int]:
    """Return exact inclusive grid-index bounds for a physical margin."""
    lower = -(-margin // router.step)
    return (
        lower, lower,
        (router.upper.x - router.lower.x - margin) // router.step,
        (router.upper.y - router.lower.y - margin) // router.step,
    )


def minimum_vias(router: GridRouter, goal: tuple[int, int, int]) -> array:
    """Return per-cell minimum via count, ignoring direction and bend constraints.

    Zero-cost existing vias and blocked copper use the same grid as the router.
    Endpoint-specific via prohibitions are deliberately relaxed, making this a
    lower bound for every route searched from a fixed goal.
    """
    nx, ny = router.nx, router.ny
    area = nx * ny
    nstates = area * len(router.layers)
    distance = array("H", [UNREACHABLE]) * nstates
    occupied = []
    for layer in router.layers:
        cells = bytearray(area)
        for x, y in router.blocked[layer]:
            cells[y * nx + x] = 1
        occupied.append(cells)
    via_blocked = bytearray(area)
    for x, y in router.via_blocked:
        via_blocked[y * nx + x] = 1
    gx, gy, glayer = goal
    goal_id = glayer * area + gy * nx + gx
    distance[goal_id] = 0
    queue = deque([goal_id])
    edge = router.clearance + math.ceil(router.width / 2)
    via_edge = router.clearance + math.ceil(router.via_size / 2)
    xmin, ymin, xmax, ymax = grid_bounds(router, edge)
    vxmin, vymin, vxmax, vymax = grid_bounds(router, via_edge)
    expanded = 0
    while queue:
        expanded += 1
        if expanded % 128 == 0:
            router.check_budget()
        state = queue.popleft()
        layer_idx, cell = divmod(state, area)
        y, x = divmod(cell, nx)
        cost = distance[state]
        layer_cells = occupied[layer_idx]
        for dx, dy in DIRECTIONS:
            xx, yy = x + dx, y + dy
            if not (0 <= xx < nx and 0 <= yy < ny):
                continue
            next_cell = yy * nx + xx
            if layer_cells[next_cell] or not (xmin <= xx <= xmax and ymin <= yy <= ymax):
                continue
            # The detailed router checks the swept diagonal itself. Adjacent
            # blocked vertices do not necessarily obstruct that diagonal, so
            # excluding it here would make this bound pessimistic.
            next_state = layer_idx * area + next_cell
            if cost < distance[next_state]:
                distance[next_state] = cost
                queue.appendleft(next_state)
        if not (vxmin <= x <= vxmax and vymin <= y <= vymax):
            continue
        existing = router.existing_vias.get((x, y), set())
        for other_idx, other_layer in enumerate(router.layers):
            if other_idx == layer_idx or occupied[other_idx][cell]:
                continue
            reuse = {router.layers[layer_idx], other_layer} <= existing
            if not reuse and (not router.allow_new_vias or via_blocked[cell]):
                continue
            next_state = other_idx * area + cell
            updated = cost + (0 if reuse else 1)
            if updated < distance[next_state]:
                distance[next_state] = updated
                (queue.appendleft if reuse else queue.append)(next_state)
    return distance


def relaxed_costs(router: GridRouter, goal: tuple[int, int, int], *,
                  stop_at: tuple[int, int, int] | None = None,
                  max_expansions: int | None = None) -> array:
    """Compute optimistic costs; unfinished cells have the safe lower bound zero."""
    nx, ny = router.nx, router.ny
    area = nx * ny
    nstates = area * len(router.layers)
    if max_expansions is not None and (
        isinstance(max_expansions, bool) or not isinstance(max_expansions, int)
        or max_expansions < 1
    ):
        raise ValueError("Topology expansion limit must be a positive integer")
    stop_id = None
    if stop_at is not None:
        if (not isinstance(stop_at, tuple) or len(stop_at) != 3
                or any(isinstance(v, bool) or not isinstance(v, int) for v in stop_at)
                or not 0 <= stop_at[0] < nx or not 0 <= stop_at[1] < ny
                or not 0 <= stop_at[2] < len(router.layers)):
            raise ValueError("Topology start must identify a valid grid cell and layer")
        stop_id = stop_at[2] * area + stop_at[1] * nx + stop_at[0]
    distance = array("d", [math.inf]) * nstates
    settled = bytearray(nstates) if stop_id is not None or max_expansions is not None else None
    partial = False
    occupied = []
    for layer in router.layers:
        cells = bytearray(area)
        for x, y in router.blocked[layer]:
            cells[y * nx + x] = 1
        occupied.append(cells)
    via_blocked = bytearray(area)
    for x, y in router.via_blocked:
        via_blocked[y * nx + x] = 1
    gx, gy, glayer = goal
    goal_id = glayer * area + gy * nx + gx
    distance[goal_id] = 0
    queue = [(0.0, goal_id)]
    edge = router.clearance + math.ceil(router.width / 2)
    via_edge = router.clearance + math.ceil(router.via_size / 2)
    xmin, ymin, xmax, ymax = grid_bounds(router, edge)
    vxmin, vymin, vxmax, vymax = grid_bounds(router, via_edge)
    step_mm = router.step / 1_000_000
    expanded = 0
    finalized = 0
    while queue:
        expanded += 1
        if expanded % 128 == 0:
            router.check_budget()
        cost, state = heapq.heappop(queue)
        if cost != distance[state]:
            continue
        if settled is not None:
            settled[state] = 1
            finalized += 1
            if state == stop_id or (max_expansions is not None and finalized >= max_expansions):
                partial = True
                break
        layer_idx, cell = divmod(state, area)
        y, x = divmod(cell, nx)
        layer = router.layers[layer_idx]
        layer_cells = occupied[layer_idx]
        axial = router.weights.length + router.layer_penalties.get(layer, 0)
        for dx, dy in DIRECTIONS:
            xx, yy = x + dx, y + dy
            if not (0 <= xx < nx and 0 <= yy < ny):
                continue
            next_cell = yy * nx + xx
            if layer_cells[next_cell] or not (xmin <= xx <= xmax and ymin <= yy <= ymax):
                continue
            diagonal = bool(dx and dy)
            # Keep the relaxed graph at least as permissive as the detailed
            # swept-edge search; corner vertices may be blocked independently.
            length = step_mm * (math.sqrt(2) if diagonal else 1)
            updated = cost + length * (
                axial + (router.weights.diagonal if diagonal else 0)
            )
            next_state = layer_idx * area + next_cell
            if updated < distance[next_state]:
                distance[next_state] = updated
                heapq.heappush(queue, (updated, next_state))
        if not (vxmin <= x <= vxmax and vymin <= y <= vymax):
            continue
        existing = router.existing_vias.get((x, y), set())
        for other_idx, other_layer in enumerate(router.layers):
            if other_idx == layer_idx or occupied[other_idx][cell]:
                continue
            reuse = {layer, other_layer} <= existing
            if not reuse and (not router.allow_new_vias or via_blocked[cell]):
                continue
            next_state = other_idx * area + cell
            updated = cost + (0 if reuse else router.weights.via)
            if updated < distance[next_state]:
                distance[next_state] = updated
                heapq.heappush(queue, (updated, next_state))
    if partial:
        # Tentative Dijkstra distances are upper bounds, not lower bounds.
        # Unknown cells must never be mistaken for unreachable cells either.
        for index, visited in enumerate(settled):
            if not visited:
                distance[index] = 0.0
    return distance
