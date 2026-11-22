"""Price physical changes to existing copper separately from new routing.

UUID replacement and collinear splitting alone are not physical rework.
This is an optimization preference, never an electrical qualification.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields, replace

from pcb.net_routing.geometry import intersection, on_segment, unique_segments
from pcb.route_optimizer import COPPER_LAYERS, GridRouter, Point, Route, Segment, mm
from pcb.route_constraints import via_sizes_meet


@dataclass(frozen=True)
class ReworkWeights:
    track: float = 1.0
    length_mm: float = 1.0
    via: float = 10.0
    placement_mm: float = 1.0
    quarter_turn: float = 1.0

    @classmethod
    def from_config(cls, raw: object = None) -> ReworkWeights:
        if raw is None:
            return cls()
        if not isinstance(raw, dict) or set(raw) - {field.name for field in fields(cls)}:
            raise ValueError("Unknown rework weights")
        result = cls(**raw)
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or value < 0
               for value in (result.track, result.length_mm, result.via, result.placement_mm, result.quarter_turn)):
            raise ValueError("Rework weights must be finite and nonnegative")
        return result


def junction_tail_cost(pruned_tails: list[tuple[str, Segment]], weights: ReworkWeights) -> float:
    """Price removed dangling copper; a pure split without removal is free."""
    tracks = len({identifier for identifier, _ in pruned_tails})
    length = sum(
        math.hypot(piece.end.x - piece.start.x, piece.end.y - piece.start.y) / 1e6
        for _, piece in pruned_tails
    )
    return tracks * weights.track + length * weights.length_mm


def placement_cost(board, poses: dict, weights: ReworkWeights) -> float:
    """Price displacement and the shortest angular change of permitted parts."""
    cost = 0.0
    for reference, (x, y, angle) in poses.items():
        footprint = board.FindFootprintByReference(reference)
        origin = footprint.GetPosition()
        distance = math.hypot(x - origin.x / 1e6, y - origin.y / 1e6)
        turn = abs((angle - float(footprint.GetOrientationDegrees()) + 180) % 360 - 180)
        cost += weights.placement_mm * distance + weights.quarter_turn * turn / 90
    return cost


@dataclass(frozen=True)
class ReworkScore:
    tracks: int = 0
    length_mm: float = 0.0
    vias: int = 0
    cost: float = 0.0


@dataclass(frozen=True)
class RemovedVia:
    point: Point
    drill: int
    sizes: tuple[tuple[str, int], ...]
    through: bool


@dataclass(frozen=True)
class RemovedCopper:
    tracks: tuple[tuple[Segment, int], ...] = ()
    vias: tuple[RemovedVia, ...] = ()
    arc_lengths_mm: tuple[float, ...] = ()
    off_angle_tracks: tuple[tuple[Segment, int], ...] = ()

    def score(self, route: Route, *, width: int, size: int, drill: int,
              weights: ReworkWeights, segment_dimensions=None) -> ReworkScore:
        changed = len(self.arc_lengths_mm) + len(self.off_angle_tracks)
        length = sum(self.arc_lengths_mm) + sum(
            math.hypot(segment.end.x - segment.start.x, segment.end.y - segment.start.y) / 1e6
            for segment, _ in self.off_angle_tracks
        )
        proposed = tuple(pair for segment in route.segments for pair in
                         (segment_dimensions(segment) if segment_dimensions else ((segment, width),)))
        for segment, old_width in self.tracks:
            # A changed width/layer counts as rework even on the same centreline.
            remaining = unique_segments((segment,), tuple(s for s, w in proposed if w == old_width))
            missing = sum(math.hypot(s.end.x - s.start.x, s.end.y - s.start.y) / 1e6
                          for s in remaining)
            if missing > 0:
                changed += 1
                length += missing
        restored = {via.at for via in (*route.vias, *route.reused_vias)}
        vias = sum(not (via.point in restored and via.through and via.drill == drill
                        and via_sizes_meet(via.sizes, (), exact_size=size))
                   for via in self.vias)
        return ReworkScore(changed, length, vias,
                           changed * weights.track + length * weights.length_mm + vias * weights.via)


def restored_connects_islands(net, route: Route) -> bool:
    """Conservatively require restored pieces and sampled islands to form one tree."""
    segments, vias, islands = route.segments, route.vias, net.islands
    if not islands or not (segments or vias):
        return False
    count = len(segments) + len(vias) + len(islands)
    parent = list(range(count))

    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def join(a, b):
        parent[root(a)] = root(b)

    try:
        for i, first in enumerate(segments):
            for j, second in enumerate(segments[:i]):
                if first.layer == second.layer and (
                    any(on_segment(point, second) for point in (first.start, first.end))
                    or any(on_segment(point, first) for point in (second.start, second.end))
                    or intersection(first, second) is not None
                ):
                    join(i, j)
            for j, via in enumerate(vias):
                if on_segment(via.at, first):
                    join(i, len(segments) + j)
        for i, island in enumerate(islands):
            index = len(segments) + len(vias) + i
            for site in island.sites:
                for j, segment in enumerate(segments):
                    if site.layer == segment.layer and on_segment(site.point, segment):
                        join(index, j)
                for j, via in enumerate(vias):
                    if site.point == via.at:
                        join(index, len(segments) + j)
    except ValueError:
        return False
    first = root(count - 1)
    return all(root(index) == first for index in range(count))


def octilinear_projections(segment: Segment, *, tolerance: int = mm(.05)) -> tuple[Segment, ...]:
    """Try exact-angle versions by moving one endpoint a bounded distance."""
    a, b = segment.start, segment.end
    dx, dy = b.x - a.x, b.y - a.y
    choices = set()
    if abs(dy) <= tolerance and dx:
        choices.update((Segment(a, Point(b.x, a.y), segment.layer),
                        Segment(Point(a.x, b.y), b, segment.layer)))
    if abs(dx) <= tolerance and dy:
        choices.update((Segment(a, Point(a.x, b.y), segment.layer),
                        Segment(Point(b.x, a.y), b, segment.layer)))
    if dx and dy and abs(abs(dx) - abs(dy)) <= tolerance:
        sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
        choices.update((
            Segment(a, Point(b.x, a.y + sy * abs(dx)), segment.layer),
            Segment(a, Point(a.x + sx * abs(dy), b.y), segment.layer),
            Segment(Point(b.x - sx * abs(dy), a.y), b, segment.layer),
            Segment(Point(a.x, b.y - sy * abs(dx)), b, segment.layer),
        ))
    valid = []
    for choice in choices:
        try:
            GridRouter._direction(choice.start, choice.end)
        except ValueError:
            continue
        valid.append(choice)
    return tuple(sorted(valid, key=lambda item: (
        item.start.x, item.start.y, item.end.x, item.end.y,
    )))


def closest_route_point(segment: Segment, point: Point) -> Point | None:
    """Project onto an exact-angle segment in KiCad's integer coordinate grid."""
    a, b = segment.start, segment.end
    dx, dy = b.x - a.x, b.y - a.y
    if dx == 0 and dy:
        return Point(a.x, min(max(point.y, min(a.y, b.y)), max(a.y, b.y)))
    if dy == 0 and dx:
        return Point(min(max(point.x, min(a.x, b.x)), max(a.x, b.x)), a.y)
    if dx and abs(dx) == abs(dy):
        sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
        along = (sx * (point.x - a.x) + sy * (point.y - a.y)) // 2
        along = min(max(along, 0), abs(dx))
        return Point(a.x + sx * along, a.y + sy * along)
    return None


def include_repair_contacts(board, net, removed: RemovedCopper, *, budget=None):
    """Retain removed endpoints touching surviving same-net copper islands."""
    import pcbnew
    from core.pcb_io import copper_width
    from pcb.net_routing.model import Site

    segments = [segment for segment, _ in removed.tracks]
    segments.extend(
        projected for segment, _ in removed.off_angle_tracks
        for projected in octilinear_projections(segment)
    )
    candidates = {
        Site(point, segment.layer)
        for segment, _ in (*removed.tracks, *removed.off_angle_tracks)
        for point in (segment.start, segment.end)
    }
    candidates.update(
        Site(point, segment.layer)
        for segment, _ in removed.off_angle_tracks
        for projected in octilinear_projections(segment)
        for point in (projected.start, projected.end)
    )
    candidates.update(
        Site(via.point, layer) for via in removed.vias for layer, _ in via.sizes
    )
    if not candidates:
        return net
    items = {item.m_Uuid.AsString(): item for item in (*board.GetPads(), *board.GetTracks())
             if item.GetNetname() == net.name}
    connectivity = board.GetConnectivity()
    pad_sites = set(net.pad_sites)
    islands = []
    for island in net.islands:
        original = items.get(island.identifier)
        if original is None:
            islands.append(island)
            continue
        linked = {item.m_Uuid.AsString(): item
                  for item in connectivity.GetConnectedItems(original)
                  if item.GetNetname() == net.name}
        linked[island.identifier] = original
        local_candidates = set(candidates)
        for item in linked.values():
            if isinstance(item, (pcbnew.PAD, pcbnew.PCB_VIA)):
                anchors = (item.GetPosition(),)
            elif isinstance(item, pcbnew.PCB_TRACK):
                anchors = (item.GetStart(), item.GetEnd())
            else:
                continue
            for segment in segments:
                if not item.IsOnLayer(board.GetLayerID(segment.layer)):
                    continue
                for anchor in anchors:
                    point = closest_route_point(segment, Point(anchor.x, anchor.y))
                    if point is not None:
                        local_candidates.add(Site(point, segment.layer))
                if isinstance(item, pcbnew.PCB_TRACK) and not isinstance(
                    item, (pcbnew.PCB_VIA, pcbnew.PCB_ARC)
                ):
                    start, end = item.GetStart(), item.GetEnd()
                    other = Segment(Point(start.x, start.y), Point(end.x, end.y),
                                    board.GetLayerName(item.GetLayer()))
                    try:
                        crossing = intersection(segment, other)
                    except ValueError:
                        crossing = None
                    if crossing is not None:
                        local_candidates.add(Site(crossing, segment.layer))
        hits, pad_hits = set(), set()
        for site in local_candidates:
            layer = board.GetLayerID(site.layer)
            point = pcbnew.VECTOR2I(site.point.x, site.point.y)
            for item in linked.values():
                if not item.IsOnLayer(layer):
                    continue
                if isinstance(item, pcbnew.PCB_VIA):
                    position = item.GetPosition()
                    radius = copper_width(item, layer=layer) / 2
                    touches = ((position.x - point.x) ** 2 + (position.y - point.y) ** 2
                               <= radius ** 2)
                else:
                    touches = item.HitTest(point)
                if touches:
                    hits.add(site)
                    if isinstance(item, pcbnew.PAD):
                        pad_hits.add(site)
        pad_sites.update(pad_hits)
        sites = tuple(sorted(set(island.sites) | hits, key=lambda site: site.key))
        islands.append(replace(island, sites=sites))
    result = replace(net, islands=tuple(islands), pad_sites=frozenset(pad_sites))
    from pcb.net_routing.native import _zone_access_sites
    layers = tuple(layer for layer in COPPER_LAYERS if any(site.layer == layer for site in candidates))
    filled = _zone_access_sites(
        board, net.name, layers, result.islands, result.pad_sites, budget, 0,
        contacts=tuple(sorted(candidates, key=lambda site: site.key)), sample=False,
    )
    return replace(result, islands=filled)


def capture_removals(board, selected_ids: set[str]) -> dict[str, RemovedCopper]:
    """Capture immutable dimensions before a disposable text-edit transaction."""
    import pcbnew
    from core.pcb_io import copper_width

    grouped: dict[str, dict[str, list]] = {}
    for item in board.GetTracks():
        if item.m_Uuid.AsString() not in selected_ids:
            continue
        values = grouped.setdefault(item.GetNetname(), {
            "tracks": [], "vias": [], "arc_lengths_mm": [], "off_angle_tracks": [],
        })
        if isinstance(item, pcbnew.PCB_VIA):
            sizes = tuple((layer, copper_width(item, layer=board.GetLayerID(layer)))
                          for layer in (board.GetLayerName(identifier)
                                        for identifier in board.GetEnabledLayers().CuStack())
                          if item.IsOnLayer(board.GetLayerID(layer)))
            values["vias"].append(RemovedVia(
                Point(item.GetPosition().x, item.GetPosition().y), item.GetDrillValue(),
                sizes, item.GetViaType() == pcbnew.VIATYPE_THROUGH))
        elif isinstance(item, pcbnew.PCB_ARC):
            values["arc_lengths_mm"].append(item.GetLength() / 1e6)
        else:
            segment = Segment(Point(item.GetStart().x, item.GetStart().y),
                              Point(item.GetEnd().x, item.GetEnd().y), board.GetLayerName(item.GetLayer()))
            # Legacy non-octilinear tracks cannot be reproduced by this router.
            try:
                unique_segments((segment,))
            except ValueError:
                values["off_angle_tracks"].append(
                    (segment, copper_width(item, layer=item.GetLayer()))
                )
            else:
                values["tracks"].append((segment, copper_width(item, layer=item.GetLayer())))
    return {name: RemovedCopper(**{key: tuple(items) for key, items in values.items()})
            for name, values in grouped.items()}
