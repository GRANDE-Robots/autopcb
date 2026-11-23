"""Create local adjacent-layer reservations near routed high-speed copper.

These conservative search obstacles do not prove that the filled reference
plane is continuous or that impedance, return current and EMI are acceptable.
"""

from __future__ import annotations

import fnmatch
import json
import math
from itertools import groupby
from dataclasses import asdict, dataclass

import pcbnew

from core.pcb_io import copper_width
from pcb.route_optimizer import GridRouter, Obstacle, Point, mm
from core.compat import strict_zip


def _default_return(board) -> str | None:
    """Recognize the root net only when its conventional name is unambiguous."""
    matches = {str(name) for name in board.GetNetsByName()} & {"GND", "/GND"}
    return matches.pop() if len(matches) == 1 else None


def adjacent_reference_layers(board) -> dict[int, tuple[int, ...]]:
    """Use physical neighbors from the enabled native copper stack."""
    stack = tuple(board.GetEnabledLayers().CuStack())
    if len(stack) < 2:
        raise ValueError("A reference check requires at least two copper layers")
    return {
        layer: tuple(stack[neighbor] for neighbor in (index - 1, index + 1)
                     if 0 <= neighbor < len(stack))
        for index, layer in enumerate(stack)
    }


@dataclass(frozen=True)
class ReferenceCorridors:
    obstacles: tuple[Obstacle, ...]
    unsupported_items: tuple[str, ...]
    source_item_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReferenceConflict:
    high_speed_item_id: str
    blocking_item_id: str
    blocking_net: str
    blocking_kind: str
    reference_layer: str


@dataclass(frozen=True)
class ReferenceGap:
    first_station: int
    last_station: int
    position: tuple[int, int]
    missing_samples: int


@dataclass(frozen=True)
class TrackReference:
    item_id: str
    net: str
    signal_layer: str
    reference_layer: str
    geometry: tuple[int, int, int, int, int]
    # (x, y, covered by reference copper, inside the trace's own pad)
    samples: tuple[tuple[int, int, bool, bool], ...]
    missing_samples: int
    continuous_polygon: bool


    def missing_runs(self) -> tuple[ReferenceGap, ...]:
        """Locate contiguous exposed gaps using the two edges and center at each station."""
        def exposed(sample) -> bool:
            return not sample[2] and not sample[3]

        runs = []
        stations = range(0, len(self.samples), 3)
        for missing, offsets in groupby(stations, lambda i: any(exposed(s) for s in self.samples[i:i + 3])):
            if not missing:
                continue
            group = list(offsets)
            middle = group[len(group) // 2]
            left, center, right = self.samples[middle:middle + 3]
            x, y = next(s for s in (center, left, right) if exposed(s))[:2]
            count = sum(exposed(s) for s in self.samples[group[0]:group[-1] + 3])
            runs.append(ReferenceGap(group[0] // 3, group[-1] // 3, (x, y), count))
        return tuple(runs)


@dataclass(frozen=True)
class ReferenceSnapshot:
    tracks: tuple[TrackReference, ...]
    unsupported_items: tuple[str, ...]

    @property
    def sampled_coverage_complete(self) -> bool:
        return bool(self.tracks) and not self.unsupported_items and all(
            not track.missing_samples and track.continuous_polygon for track in self.tracks
        )


@dataclass(frozen=True)
class ReferenceComparison:
    status: str
    degraded_track_ids: tuple[str, ...]
    unverified_track_ids: tuple[str, ...]


def high_speed_nets(project_bytes: bytes, board_nets: set[str]) -> set[str]:
    """Conservatively identify USB/Ethernet data classes from saved project rules."""
    project = json.loads(project_bytes.decode("utf-8-sig"))
    if not isinstance(project, dict) or not isinstance(project.get("net_settings"), dict):
        raise ValueError("Project has no valid net-class settings")
    settings = project["net_settings"]
    patterns = settings.get("netclass_patterns") or []
    assignments = settings.get("netclass_assignments") or {}
    if not isinstance(patterns, list) or not isinstance(assignments, dict):
        raise ValueError("Invalid project net-class assignments")
    if any(not isinstance(item, dict) or not isinstance(item.get("netclass"), str)
           or not isinstance(item.get("pattern"), str) for item in patterns):
        raise ValueError("Invalid project net-class pattern")
    critical = {"ETH_DATA", "USB_DATA"}
    result = {
        name for name in board_nets
        if any(item["netclass"] in critical and
               (item["pattern"] == name or fnmatch.fnmatchcase(name, item["pattern"]))
               for item in patterns)
    }
    for name, value in assignments.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("Unsupported direct net-class assignment")
        if value in critical and name in board_nets:
            result.add(name)
    return result


def reference_corridors(board, high_speed_nets: set[str], *, margin_mm: float = 0.5) -> ReferenceCorridors:
    """Reserve local reference copper, not an entire copper layer."""
    if not isinstance(high_speed_nets, set) or any(not isinstance(name, str) or not name for name in high_speed_nets):
        raise ValueError("High-speed nets must be an explicit set of net names")
    margin = mm(margin_mm)
    if margin < 0:
        raise ValueError("Reference-corridor margin must be nonnegative")
    missing = sorted(name for name in high_speed_nets if board.FindNet(name) is None)
    if missing:
        raise ValueError(f"High-speed nets are absent from the board: {missing}")
    obstacles = []
    unsupported = []
    sources = []
    adjacent = adjacent_reference_layers(board)
    stack = tuple(adjacent)
    return_net = _default_return(board) or "GND"

    def reserve(item, obstacle):
        obstacles.append(obstacle)
        sources.append(item.m_Uuid.AsString())

    def box(item, layer: str):
        bounds = item.GetBoundingBox()
        # These are reservations for ground return copper, not physical rule
        # areas. Ground may reinforce them; real PCB keepouts remain absolute.
        return Obstacle(
            Point(int(bounds.GetX()) - margin, int(bounds.GetY()) - margin),
            Point(int(bounds.GetRight()) + margin, int(bounds.GetBottom()) + margin),
            (layer,), return_net, "copper",
        )

    def capsule(start: Point, end: Point, radius: int, layer: str):
        return Obstacle(
            Point(min(start.x, end.x) - radius, min(start.y, end.y) - radius),
            Point(max(start.x, end.x) + radius, max(start.y, end.y) + radius),
            (layer,), return_net, "copper", centerline=(start, end), copper_radius=radius,
        )

    for pad in board.GetPads():
        if pad.GetNetname() not in high_speed_nets:
            continue
        if pad.IsOnLayer(pcbnew.F_Cu):
            for layer in adjacent[pcbnew.F_Cu]:
                reserve(pad, box(pad, board.GetLayerName(layer)))
        if pad.IsOnLayer(pcbnew.B_Cu):
            for layer in adjacent[pcbnew.B_Cu]:
                reserve(pad, box(pad, board.GetLayerName(layer)))
    for item in board.GetTracks():
        if item.GetNetname() not in high_speed_nets:
            continue
        if isinstance(item, pcbnew.PCB_VIA):
            center = item.GetPosition()
            point = Point(int(center.x), int(center.y))
            for layer in stack[1:-1]:
                if item.IsOnLayer(layer):
                    radius = (copper_width(item, layer=layer) + 1) // 2 + margin
                    reserve(item, capsule(point, point, radius, board.GetLayerName(layer)))
            continue
        references = adjacent.get(item.GetLayer())
        if references is None:
            unsupported.append(item.m_Uuid.AsString())
            continue
        if isinstance(item, pcbnew.PCB_ARC):
            for reference in references:
                reserve(item, box(item, board.GetLayerName(reference)))
            continue
        start, end = item.GetStart(), item.GetEnd()
        a, b = Point(int(start.x), int(start.y)), Point(int(end.x), int(end.y))
        radius = (copper_width(item) + 1) // 2 + margin
        for reference in references:
            reserve(item, capsule(a, b, radius, board.GetLayerName(reference)))
    return ReferenceCorridors(tuple(obstacles), tuple(sorted(unsupported)), tuple(sources))


def reference_conflicts(
    board, high_speed_nets: set[str], *, margin_mm: float = 0.5, clearance_mm: float = 0.2
) -> tuple[ReferenceConflict, ...]:
    """Attribute inner-layer copper intersecting a local high-speed reservation."""
    corridors = reference_corridors(board, high_speed_nets, margin_mm=margin_mm)
    clearance = mm(clearance_mm)
    if clearance < 0:
        raise ValueError("Reference clearance must be nonnegative")
    layers = {board.GetLayerName(layer): layer for layer in board.GetEnabledLayers().CuStack()}
    return_net = _default_return(board)
    found = {}
    for item in (*board.GetTracks(), *board.GetPads()):
        if item.GetNetname() == return_net:
            continue
        uid = item.m_Uuid.AsString()
        for guard, source in strict_zip(corridors.obstacles, corridors.source_item_ids):
            layer_name = guard.layers[0]
            layer = layers[layer_name]
            if uid == source or not item.IsOnLayer(layer):
                continue
            if isinstance(item, pcbnew.PCB_VIA):
                pos = item.GetPosition()
                point = Point(int(pos.x), int(pos.y))
                radius = (copper_width(item, layer=layer) + 1) // 2 + clearance
                hit = GridRouter._intersects_box(point, point, guard, radius)
                kind = "via"
            elif isinstance(item, pcbnew.PCB_TRACK) and not isinstance(item, pcbnew.PCB_ARC):
                start, end = item.GetStart(), item.GetEnd()
                radius = (copper_width(item) + 1) // 2 + clearance
                hit = GridRouter._intersects_box(Point(int(start.x), int(start.y)), Point(int(end.x), int(end.y)), guard, radius)
                kind = "track"
            else:
                # Pad and arc bounding boxes conservatively enclose their copper.
                bounds = item.GetBoundingBox()
                hit = not (bounds.GetRight() + clearance < guard.lower.x or bounds.GetX() - clearance > guard.upper.x
                           or bounds.GetBottom() + clearance < guard.lower.y or bounds.GetY() - clearance > guard.upper.y)
                kind = "pad" if isinstance(item, pcbnew.PAD) else "arc"
            if hit:
                found[(source, uid, layer_name)] = ReferenceConflict(source, uid, item.GetNetname(), kind, layer_name)
    return tuple(found[key] for key in sorted(found))


def sample_reference(board, high_speed_nets: set[str], *, step_mm: float = 0.1,
                     reference_net: str | None = None) -> ReferenceSnapshot:
    """Sample filled adjacent-layer GND polygons along each data trace.

    One polygon on one adjacent layer must cover all samples. Inner-layer
    signals reserve both neighbors; sampling chooses the best covered neighbor.
    Trace copper inside a same-net pad on its own layer is part of that pad, not
    an exposed line, so those samples never count as missing.
    This is not an impedance or return-current simulation.
    """
    if not isinstance(high_speed_nets, set) or any(not isinstance(name, str) or not name for name in high_speed_nets):
        raise ValueError("High-speed nets must be an explicit set of net names")
    step = mm(step_mm)
    if step <= 0:
        raise ValueError("Reference sampling step must be positive")
    adjacent = adjacent_reference_layers(board)
    reference_net = _default_return(board) if reference_net is None else reference_net
    zones = {layer: [] for layer in adjacent}
    for zone in board.Zones():
        if zone.GetIsRuleArea() or zone.GetNetname() != reference_net:
            continue
        for layer in zones:
            if zone.IsOnLayer(layer):
                polygons = zone.GetFilledPolysList(layer)
                zones[layer].extend((zone.m_Uuid.AsString(), index, polygons)
                                    for index in range(polygons.OutlineCount()))
    pads = {name: [] for name in high_speed_nets}
    for pad in board.GetPads():
        if pad.GetNetname() in pads:
            pads[pad.GetNetname()].append(pad)
    reports = []
    unsupported = []
    for item in board.GetTracks():
        if item.GetNetname() not in high_speed_nets or isinstance(item, pcbnew.PCB_VIA):
            continue
        uid = item.m_Uuid.AsString()
        layer = item.GetLayer()
        references = adjacent.get(layer)
        if isinstance(item, pcbnew.PCB_ARC) or references is None:
            unsupported.append(uid)
            continue
        start, end = item.GetStart(), item.GetEnd()
        dx, dy = int(end.x - start.x), int(end.y - start.y)
        length = math.hypot(dx, dy)
        if length == 0:
            unsupported.append(uid)
            continue
        count = max(1, math.ceil(length / step))
        if count > 100_000:
            raise ValueError("Reference sampling would exceed its per-track limit")
        width = copper_width(item)
        points = []
        for index in range(count + 1):
            x = int(start.x) + dx * index / count
            y = int(start.y) + dy * index / count
            for offset in (-width / 2, 0, width / 2):
                px = round(x - dy / length * offset)
                py = round(y + dx / length * offset)
                points.append((px, py))
        own = [pad for pad in pads[item.GetNetname()] if pad.IsOnLayer(layer)]
        inside = [any(pad.HitTest(pcbnew.VECTOR2I(px, py)) for pad in own) for px, py in points]
        choices = []
        for order, reference in enumerate(references):
            samples = []
            common = None
            for (px, py), in_pad in strict_zip(points, inside):
                point = pcbnew.VECTOR2I(px, py)
                covered = {(zone_id, number) for zone_id, number, poly in zones[reference]
                           if poly.Contains(point, number)}
                samples.append((px, py, bool(covered), in_pad))
                if not in_pad:
                    common = covered if common is None else common & covered
            missing = sum(not covered and not in_pad for _, _, covered, in_pad in samples)
            continuous = common is None or bool(common)
            choices.append(((not continuous, missing, order), reference, tuple(samples), missing, continuous))
        _, reference, samples, missing, continuous = min(choices, key=lambda choice: choice[0])
        reports.append(TrackReference(
            uid, item.GetNetname(), board.GetLayerName(layer), board.GetLayerName(reference),
            (int(start.x), int(start.y), int(end.x), int(end.y), width), samples, missing, continuous,
        ))
    return ReferenceSnapshot(tuple(sorted(reports, key=lambda item: item.item_id)), tuple(sorted(unsupported)))


def compare_reference(before: ReferenceSnapshot, after: ReferenceSnapshot) -> ReferenceComparison:
    """Detect lost sampled reference on unchanged traces; flag changed paths for review."""
    if not isinstance(before, ReferenceSnapshot) or not isinstance(after, ReferenceSnapshot):
        raise ValueError("Reference comparison requires two snapshots")
    old = {track.item_id: track for track in before.tracks}
    new = {track.item_id: track for track in after.tracks}
    degraded = set()
    unverified = set(before.unsupported_items) | set(after.unsupported_items)
    for uid in old.keys() | new.keys():
        previous, current = old.get(uid), new.get(uid)
        if previous is None or current is None or previous.geometry != current.geometry or previous.signal_layer != current.signal_layer:
            unverified.add(uid)
            continue
        if previous.reference_layer != current.reference_layer:
            if not previous.missing_samples and previous.continuous_polygon:
                degraded.add(uid)
            unverified.add(uid)
            continue
        if len(previous.samples) != len(current.samples) or any(a[:2] != b[:2] for a, b in strict_zip(previous.samples, current.samples)):
            unverified.add(uid)
            continue
        if any(a[2] and not b[2] for a, b in strict_zip(previous.samples, current.samples)) or (previous.continuous_polygon and not current.continuous_polygon):
            degraded.add(uid)
        if previous.missing_samples or current.missing_samples or not previous.continuous_polygon or not current.continuous_polygon:
            unverified.add(uid)
    status = "degraded" if degraded else "unverified" if unverified or not old else "unchanged_sampled"
    return ReferenceComparison(status, tuple(sorted(degraded)), tuple(sorted(unverified)))


def launch_reference_layer(before, after, tracks: tuple, endpoint_vias: tuple) -> str | None:
    """Prove that all local trace copper overlies one adjacent ground polygon.

    The only exceptions are existing plane voids within the saved clearance
    envelope of a same-net endpoint via. A new void inside that envelope is
    still rejected. This proves local geometry, not impedance or pair timing.
    """
    if not tracks:
        return None
    layer, net = tracks[0].GetLayer(), tracks[0].GetNetname()
    if (layer not in (pcbnew.F_Cu, pcbnew.B_Cu)
            or any(t.Type() != pcbnew.PCB_TRACE_T or t.GetLayer() != layer
                   or t.GetNetname() != net for t in tracks)):
        return None
    endpoints = {(p.x, p.y) for t in tracks for p in (t.GetStart(), t.GetEnd())}
    if any(v.GetNetname() != net or (v.GetPosition().x, v.GetPosition().y) not in endpoints
           for v in endpoint_vias):
        return None
    projection = pcbnew.SHAPE_POLY_SET()
    for track in tracks:
        shape = pcbnew.SHAPE_POLY_SET()
        track.TransformShapeToPolygon(shape, layer, 0, 2000, pcbnew.ERROR_OUTSIDE)
        projection.BooleanAdd(shape)
    previous_zones = {z.m_Uuid.AsString(): z for z in before.Zones()
                      if not z.GetIsRuleArea() and z.GetNetname() == 'GND'}
    for reference in adjacent_reference_layers(after)[layer]:
        for zone in after.Zones():
            original = previous_zones.get(zone.m_Uuid.AsString())
            if (original is None or zone.GetIsRuleArea() or zone.GetNetname() != 'GND'
                    or not zone.IsOnLayer(reference) or not original.IsOnLayer(reference)):
                continue
            clearance = original.GetLocalClearance()
            if not isinstance(clearance, int) or clearance < 0:
                continue
            old_plane = original.GetFilledPolysList(reference)
            plane = zone.GetFilledPolysList(reference)
            remaining = pcbnew.SHAPE_POLY_SET(projection)
            for via in endpoint_vias:
                if not via.IsOnLayer(reference):
                    continue
                inherited = pcbnew.SHAPE_POLY_SET()
                via.TransformShapeToPolygon(inherited, reference, clearance, 2000, pcbnew.ERROR_INSIDE)
                inherited.BooleanSubtract(old_plane)
                remaining.BooleanSubtract(inherited)
            if remaining.Area() <= 0:
                continue
            for index in range(plane.OutlineCount()):
                missing = pcbnew.SHAPE_POLY_SET(remaining)
                missing.BooleanSubtract(plane.UnitSet(index))
                if missing.Area() == 0:
                    return str(after.GetLayerName(reference))
    return None


def comparable_missing_samples(before: ReferenceSnapshot, after: ReferenceSnapshot) -> tuple[int, int] | None:
    """Compare missing samples only on the exact same high-speed geometry."""
    if not isinstance(before, ReferenceSnapshot) or not isinstance(after, ReferenceSnapshot):
        raise ValueError("Reference comparison requires two snapshots")
    old = {track.item_id: track for track in before.tracks}
    new = {track.item_id: track for track in after.tracks}
    if not old or old.keys() != new.keys() or before.unsupported_items or after.unsupported_items:
        return None
    for uid, previous in old.items():
        current = new[uid]
        if (previous.geometry != current.geometry or previous.signal_layer != current.signal_layer
                or previous.reference_layer != current.reference_layer
                or len(previous.samples) != len(current.samples)
                or any(a[:2] != b[:2] for a, b in strict_zip(previous.samples, current.samples))):
            return None
    return sum(track.missing_samples for track in old.values()), sum(track.missing_samples for track in new.values())


def reference_report(board, critical: set[str]) -> dict:
    """Summarize local reservations and sampled fills without claiming verification."""
    corridors = reference_corridors(board, critical)
    sampled = sample_reference(board, critical)
    return {
        "status": "sampled_unverified",
        "electrically_verified": False,
        "high_speed_nets": sorted(critical),
        "reservation_count": len(corridors.obstacles),
        "unsupported_items": list(corridors.unsupported_items),
        "conservative_conflicts": [asdict(item) for item in reference_conflicts(board, critical)],
        "sampled_tracks": [{key: value for key, value in asdict(track).items() if key != "samples"}
                           for track in sampled.tracks],
        "sampled_coverage_complete": sampled.sampled_coverage_complete,
        "sampling_step_mm": 0.1,
    }
