"""Choose small, automatically selected subsets of native copper islands."""
from __future__ import annotations

import heapq
import itertools
import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from functools import lru_cache

from pcb.net_routing.model import Net, Site


def _distance(a: Site, b: Site) -> float:
    dx = abs(a.point.x - b.point.x)
    dy = abs(a.point.y - b.point.y)
    return max(dx, dy) + (math.sqrt(2) - 1) * min(dx, dy) + (10_000_000 if a.layer != b.layer else 0)


def _nearest(a, b):
    if not a.sites or not b.sites:
        raise ValueError("Every checkpoint island needs an accessible copper site")
    # The first dozen sites provide an upper bound, not a coverage limit.
    # Use it to prune by coordinate while considering every retained site.
    best = min(
        ((_distance(x, y), x.key, y.key, x, y) for x in a.sites[:12] for y in b.sites[:12]),
        key=lambda item: item[:3],
    )
    ordered = sorted(b.sites, key=lambda site: site.point.x)
    xs = [site.point.x for site in ordered]
    for x in a.sites:
        lo = bisect_left(xs, x.point.x - best[0])
        hi = bisect_right(xs, x.point.x + best[0])
        for y in ordered[lo:hi]:
            if abs(x.point.y - y.point.y) > best[0]:
                continue
            if x.layer != y.layer and best[0] < 10_000_000:
                continue
            distance = _distance(x, y)
            if (distance, x.key, y.key) < best[:3]:
                best = (distance, x.key, y.key, x, y)
    return best


@dataclass(frozen=True, slots=True)
class CheckpointSelection:
    net: Net
    anchors: tuple[Site, ...]
    index: int
    count: int


class CheckpointExhausted(ValueError):
    """Every distinct sampled island set at this search stage was considered."""


class UnsupportedRoutingRequest(ValueError):
    """A known search capability limit prevents this particular proposal."""


class CheckpointWindowUnavailable(ValueError):
    """The optional local window cannot contain the selected anchors."""


def choose_checkpoint(net: Net, *, maximum: int = 3, offset: int = 0) -> CheckpointSelection:
    """Enumerate distinct island sets; never wrap an offset back to earlier work."""
    if (not isinstance(net, Net) or isinstance(maximum, bool) or not isinstance(maximum, int)
            or not 2 <= maximum <= 20 or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
        raise ValueError("Invalid checkpoint settings")
    if len(net.islands) < 2:
        raise ValueError("A checkpoint needs at least two disconnected copper islands")
    @lru_cache(maxsize=4096)
    def nearest(i, j):
        return _nearest(net.islands[i], net.islands[j])

    pairs = heapq.nsmallest(64, ((nearest(i, j)[0], i, j)
                               for i, j in itertools.combinations(range(len(net.islands)), 2)))
    seen = set()
    options = []
    for _, first, second in pairs:
        _, _, _, first_site, second_site = nearest(first, second)
        indices = [first, second]
        anchors = [first_site, second_site]
        while len(indices) < min(maximum, len(net.islands)):
            _, index, parent = min(
                (nearest(i, j)[0], i, j)
                for i in range(len(net.islands)) if i not in indices for j in indices
            )
            _, _, _, new_site, _ = nearest(index, parent)
            indices.append(index)
            anchors.append(new_site)
        key = frozenset(net.islands[i].identifier for i in indices)
        if key not in seen:
            seen.add(key)
            options.append((indices, tuple(anchors)))
    if offset >= len(options):
        raise CheckpointExhausted(f"{net.name}: all {len(options)} distinct sampled checkpoints were tried")
    indices, anchors = options[offset]
    return CheckpointSelection(replace(net, islands=tuple(net.islands[i] for i in indices)),
                               anchors, offset, len(options))


def select_checkpoint(net: Net, *, maximum: int = 3, offset: int = 0) -> tuple[Net, tuple[Site, ...]]:
    selected = choose_checkpoint(net, maximum=maximum, offset=offset)
    return selected.net, selected.anchors


def repair_fragments(original_board, net: Net) -> tuple:
    """Identify every surviving fragment split by removal or component movement."""
    items = {item.m_Uuid.AsString(): item for item in (*original_board.GetPads(), *original_board.GetTracks())
             if item.GetNetname() == net.name}
    connectivity = original_board.GetConnectivity()
    groups: dict[str, list] = {}
    for island in net.islands:
        original = items.get(island.identifier)
        if original is None:
            raise ValueError("Repair island does not belong to the original board")
        linked = {item.m_Uuid.AsString() for item in connectivity.GetConnectedItems(original)
                  if item.GetNetname() == net.name}
        linked.add(island.identifier)
        groups.setdefault(min(linked), []).append(island)
    split_groups = [members for members in groups.values() if len(members) > 1]
    return tuple(island for members in split_groups for island in members)


def choose_repair_checkpoint(original_board, net: Net, *, maximum: int = 3, offset: int = 0) -> CheckpointSelection:
    """Select every surviving fragment of an original island split by rip-up."""
    fragments = repair_fragments(original_board, net)
    if not fragments:
        raise ValueError("Rip-up did not split an original surviving copper island")
    if len(fragments) > maximum:
        raise UnsupportedRoutingRequest(
            "Rip-up needs more coordinated island fragments than the checkpoint limit"
        )
    return choose_checkpoint(replace(net, islands=fragments), maximum=maximum, offset=offset)


def choose_transaction_checkpoint(original_board, net: Net, *, target: bool,
                                  maximum: int, repair_maximum: int, offset: int = 0) -> CheckpointSelection:
    """Repair damaged islands and, for the target, join a new checkpoint."""
    selected = {island.identifier: island for island in repair_fragments(original_board, net)}
    if target and len(net.islands) > 1:
        initial = choose_checkpoint(net, maximum=maximum, offset=offset)
        proposed = {**selected, **{island.identifier: island for island in initial.net.islands}}
        if not selected or len(proposed) <= repair_maximum:
            selected = proposed
    if not selected:
        # An unaffected net may already contain many unrelated open islands.
        # A self-anchor binds it without forcing an unrelated whole-net repair.
        if not net.islands:
            raise ValueError("A placement repair needs surviving copper")
        island = net.islands[0]
        return CheckpointSelection(replace(net, islands=(island,)), (island.sites[0],), 0, 1)
    if len(selected) > repair_maximum:
        raise UnsupportedRoutingRequest(
            "Bottom movement needs more repair fragments than the checkpoint limit"
        )
    return choose_checkpoint(replace(net, islands=tuple(selected.values())),
                             maximum=repair_maximum, offset=0)


# Preserve the earlier internal name for tools that imported it.
choose_move_checkpoint = choose_transaction_checkpoint


def original_anchor(original_board, moved_board, name: str, site: Site) -> Site:
    """Bind a moved pad site to the original pad's UUID through its old position."""
    old_pads = {pad.m_Uuid.AsString(): pad for pad in original_board.GetPads()}
    for pad in moved_board.GetPads():
        at = pad.GetPosition()
        if (pad.GetNetname() == name and at.x == site.point.x and at.y == site.point.y
                and pad.IsOnLayer(moved_board.GetLayerID(site.layer))):
            old = old_pads[pad.m_Uuid.AsString()].GetPosition()
            return Site(type(site.point)(old.x, old.y), site.layer)
    return site


def select_repair_checkpoint(original_board, net: Net, *, maximum: int = 3, offset: int = 0) -> tuple[Net, tuple[Site, ...]]:
    selected = choose_repair_checkpoint(original_board, net, maximum=maximum, offset=offset)
    return selected.net, selected.anchors


def route_access_inset(settings: dict) -> float:
    """Reserve room for the configured track and via at a local search edge."""
    from pcb.route_optimizer import mm

    if not isinstance(settings, dict) or not isinstance(settings.get("via", {}), dict):
        raise ValueError("Routing settings and via dimensions must be objects")
    width = mm(settings.get("width_mm", 0.2))
    clearance = mm(settings.get("clearance_mm", 0.2))
    via_size = mm(settings.get("via", {}).get("size_mm", 0.6))
    if width <= 0 or via_size <= 0 or clearance < 0:
        raise ValueError("Routing dimensions must be positive with nonnegative clearance")
    return (((max(width, via_size) + 1) // 2) + clearance) / 1_000_000


def search_window(anchors: tuple[Site, ...], bounds: tuple[float, float, float, float],
                  margin: float, step: float, layers: int, max_cells: int,
                  *, access_inset: float = 0.0) -> tuple[tuple[float, ...], float]:
    """Keep local bounds large enough for exact endpoint and via access."""
    from pcb.net_routing.controller import bounded_step

    if (not anchors or isinstance(margin, bool) or not isinstance(margin, (int, float))
            or not math.isfinite(margin) or margin <= 0
            or isinstance(access_inset, bool) or not isinstance(access_inset, (int, float))
            or not math.isfinite(access_inset) or access_inset < 0):
        raise ValueError("A checkpoint needs finite local bounds and a positive margin")
    xs = [site.point.x / 1e6 for site in anchors]
    ys = [site.point.y / 1e6 for site in anchors]
    for _ in range(16):
        window = (max(bounds[0], min(xs) - margin), max(bounds[1], min(ys) - margin),
                  min(bounds[2], max(xs) + margin), min(bounds[3], max(ys) + margin))
        window = tuple(round(value, 6) for value in window)
        if any(not (window[0] <= x <= window[2] and window[1] <= y <= window[3])
               for x, y in zip(xs, ys, strict=True)):
            raise CheckpointWindowUnavailable("Checkpoint anchors are outside the scheduled bounds")
        effective_step = bounded_step(window, step, layers, max_cells)
        if not access_inset:
            return window, effective_step
        # One grid step leaves room for an off-grid endpoint escape after the
        # router's copper/clearance inset, even if the grid was coarsened.
        needed = math.ceil((access_inset + effective_step) * 1_000_000 - 1e-9) / 1_000_000
        if needed <= margin:
            return window, effective_step
        margin = needed
    raise CheckpointWindowUnavailable("Checkpoint access bounds did not converge")
