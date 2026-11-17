"""Generate bounded bottom-placement transactions from blocked endpoints."""

from __future__ import annotations

from core.pcb_io import enabled_copper_layers

import itertools
import math
import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from core.kicad_sexpr import parse
from core.pcb_io import copper_width, net_classes
from pcb.route_native import validate_removals
from pcb.route_optimizer import mm


@dataclass(frozen=True, slots=True)
class GroundAnchorPose:
    """A half-turn of a bottom resistor around one unchanged protected pad."""

    reference: str
    signal_net: str
    anchor_id: str
    x_mm: float
    y_mm: float
    angle_deg: float
    anchor_net: str


def _anchor_pose(footprint, excluded: set[str], anchor_nets: set[str]) -> GroundAnchorPose | None:
    """Propose a half-turn preserving one symmetric bottom protected pad.

    This is a geometric hypothesis, not electrical qualification. All
    disturbed signal copper must still be rerouted and independently checked.
    """
    import pcbnew

    reference = str(footprint.GetReference())
    if (not re.fullmatch(r"R[0-9]+", reference)
            or footprint.GetLayer() != pcbnew.B_Cu or footprint.IsLocked()):
        return None
    pads = tuple(footprint.Pads())
    if (len(pads) != 2 or len({str(pad.GetNumber()) for pad in pads}) != 2
            or any(not str(pad.GetNumber()) or pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD
                   or not pad.IsOnLayer(pcbnew.B_Cu) or pad.IsOnLayer(pcbnew.F_Cu) for pad in pads)):
        return None
    anchored = [pad for pad in pads if str(pad.GetNetname()) in anchor_nets & excluded]
    signal = [pad for pad in pads if str(pad.GetNetname()) not in excluded | {""}
              and not str(pad.GetNetname()).startswith("unconnected-")]
    if len(anchored) != 1 or len(signal) != 1:
        return None
    pad = anchored[0]
    layers = tuple(layer for layer in (pcbnew.B_Cu, pcbnew.B_Mask, pcbnew.B_Paste)
                   if pad.IsOnLayer(layer))
    for layer in layers:
        offset = pad.GetOffset(layer)
        if (pad.GetShape(layer) not in {pcbnew.PAD_SHAPE_CIRCLE, pcbnew.PAD_SHAPE_RECT,
                                        pcbnew.PAD_SHAPE_OVAL, pcbnew.PAD_SHAPE_ROUNDRECT}
                or pad.GetChamferPositions(layer) or offset.x != 0 or offset.y != 0):
            return None
    point, origin = pad.GetPosition(), footprint.GetPosition()
    x, y = 2 * point.x - origin.x, 2 * point.y - origin.y
    angle = (float(footprint.GetOrientationDegrees()) + 180.0) % 360.0
    clone = pcbnew.FOOTPRINT(footprint)
    clone.SetOrientationDegrees(angle)
    clone.SetPosition(pcbnew.VECTOR2I(x, y))
    copied = [item for item in clone.Pads() if item.m_Uuid.AsString() == pad.m_Uuid.AsString()]
    if len(copied) != 1:
        return None
    rotated = copied[0]
    def box(item):
        return item.GetX(), item.GetY(), item.GetWidth(), item.GetHeight()

    if (rotated.GetPosition() != point
            or any(box(pad.GetBoundingBox(layer)) != box(rotated.GetBoundingBox(layer)) for layer in layers)
            or (rotated.GetOrientationDegrees() - pad.GetOrientationDegrees()) % 180.0 != 0):
        return None
    return GroundAnchorPose(reference, str(signal[0].GetNetname()), pad.m_Uuid.AsString(),
                            x / 1e6, y / 1e6, angle, str(pad.GetNetname()))


def ground_anchor_pose(footprint, excluded: set[str]) -> GroundAnchorPose | None:
    """Propose a private half-turn around an unchanged ground pad."""
    return _anchor_pose(footprint, excluded, {"GND"})


def protected_anchor_pose(footprint, excluded: set[str]) -> GroundAnchorPose | None:
    """Propose a private half-turn around another unchanged protected pad."""
    return _anchor_pose(footprint, excluded, excluded - {"GND", ""})


class UnsupportedPlacement(ValueError):
    """A candidate requires moving copper that this recovery cannot replace."""


def _eligible(board, anchors, excluded, recovery):
    """Cheap selection only; native placement validation checks every proposal."""
    import pcbnew

    text = Path(board.GetFileName()).read_text(encoding="utf-8-sig")
    forbidden, counts = set(), {}
    for block in parse(text).children("footprint"):
        refs = [
            p.atoms()[2].value
            for p in block.children("property")
            if len(p.atoms()) >= 3 and p.atoms()[1].value == "Reference"
        ]
        for ref in refs:
            counts[ref] = counts.get(ref, 0) + 1
            if '"F.SilkS"' in text[block.start : block.end]:
                forbidden.add(ref)
    classes = net_classes(board)
    result = []
    for footprint in board.GetFootprints():
        ref, pads = footprint.GetReference(), tuple(footprint.Pads())
        if (
            counts.get(ref) != 1
            or ref in forbidden
            or not pads
            or footprint.GetLayer() != pcbnew.B_Cu
            or footprint.IsLocked()
            or any(
                p.GetAttribute() != pcbnew.PAD_ATTRIB_SMD
                or not p.IsOnLayer(pcbnew.B_Cu)
                or p.IsOnLayer(pcbnew.F_Cu)
                or p.GetNetClassName() not in classes
                for p in pads
            )
        ):
            continue
        names = {
            p.GetNetname()
            for p in pads
            if p.GetNetname() and not p.GetNetname().startswith("unconnected-")
        }
        if not names or names & excluded:
            continue
        distance = min(
            math.hypot(p.GetPosition().x - a.point.x, p.GetPosition().y - a.point.y)
            for p in pads
            for a in anchors
        )
        if distance <= mm(recovery.rip_up_radius_mm * 2):
            result.append((distance, ref, footprint, names))
    return sorted(result, key=lambda value: (value[0], value[1]))


def _incident_copper(board, selected, names):
    """Conservatively remove copper touching moved pads, never arbitrary nets."""
    import pcbnew

    removals, dimensions = {}, {}
    for item in board.GetTracks():
        name = item.GetNetname()
        if name not in names:
            continue
        layers = tuple(
            layer for layer in enabled_copper_layers(board) if item.IsOnLayer(board.GetLayerID(layer))
        )
        bounds = item.GetBoundingBox()
        # Overselect rather than missing a contact; native checks must repair
        # every connection disturbed by the removal or movement.
        touching = any(
            p.GetNetname() == name
            and any(p.IsOnLayer(board.GetLayerID(layer)) for layer in layers)
            and bounds.Intersects(p.GetBoundingBox())
            for _, _, footprint, _ in selected
            for p in footprint.Pads()
        )
        if not touching:
            continue
        via = isinstance(item, pcbnew.PCB_VIA)
        if item.IsLocked() or item.GetParentGroup() is not None:
            raise UnsupportedPlacement(
                "Automatic movement cannot replace locked or grouped copper"
            )
        if isinstance(item, pcbnew.PCB_ARC) or (
            via and item.GetViaType() != pcbnew.VIATYPE_THROUGH
        ):
            raise UnsupportedPlacement(
                "Automatic movement cannot replace arcs or non-through vias"
            )
        removals.setdefault(name, []).append(
            {
                "kind": "via" if via else "segment",
                "uuid": item.m_Uuid.AsString(),
            }
        )
        width, size, drill = dimensions.get(name, (0, 0, 0))
        if via:
            size = max(
                size,
                *(
                    copper_width(item, layer=board.GetLayerID(layer))
                    for layer in layers
                ),
            )
            drill = max(drill, item.GetDrillValue())
        else:
            width = max(width, copper_width(item, layer=item.GetLayer()))
        dimensions[name] = width, size, drill
    for name, items in removals.items():
        validate_removals(board, items, name)
    return removals, dimensions


def _net_settings(board, name, policy, recovery, dimensions, target_settings=None):
    classes = net_classes(board)
    limits = [
        classes[p.GetNetClassName()] for p in board.GetPads() if p.GetNetname() == name
    ]
    options = {
        **deepcopy(policy.get("route", {})),
        **deepcopy(policy.get("net_overrides", {}).get(name, {})),
        **deepcopy(target_settings or {}),
    }
    width, size, drill = dimensions.get(name, (0, 0, 0))
    via = options.get("via", {})
    options.update(
        name=name,
        width_mm=max(
            mm(options.get("width_mm", 0.2)),
            width,
            *(c.GetTrackWidth() for c in limits),
        )
        / 1e6,
        clearance_mm=max(
            mm(options.get("clearance_mm", 0.2)), *(c.GetClearance() for c in limits)
        )
        / 1e6,
        via={
            "size_mm": max(mm(via.get("size_mm", 0.6)), size) / 1e6,
            "drill_mm": max(mm(via.get("drill_mm", 0.3)), drill) / 1e6,
        },
        step_mm=min(options.get("step_mm", 0.1), 0.05),
        max_cells=recovery.max_cells,
        max_expansions=recovery.max_expansions,
        topology_heuristic=True,
        heuristic_weight=recovery.heuristic_weight,
    )
    options.pop("bounds_mm", None)
    return options


def _courtyard_geometry(footprint):
    """Build native courtyard polygons; the footprint owns their memory."""
    import pcbnew

    footprint.BuildCourtyardCaches()
    result = {}
    for layer in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
        polygon = footprint.GetCourtyard(layer)
        if polygon.OutlineCount():
            result[layer] = (polygon, polygon.BBox())
    return result


def _courtyard_conflicts(moved, all_geometry):
    """Return same-side polygon overlaps involving at least one moved part."""
    conflicts = set()
    for identifier, geometry in moved.items():
        for other_id, original in all_geometry.items():
            if other_id == identifier:
                continue
            other = moved.get(other_id, original)
            for layer in geometry.keys() & other.keys():
                first, first_box = geometry[layer]
                second, second_box = other[layer]
                if first_box.Intersects(second_box) and first.Collide(second):
                    conflicts.add((layer, *sorted((identifier, other_id))))
    return conflicts


def _placement_obstructions(board, copies, selected_ids, removed_ids, policy, excluded, request):
    """Identify foreign copper hit by moved pads, or reject immovable obstructions."""
    import pcbnew

    classes = net_classes(board)
    configured = mm(policy.get("route", {}).get("clearance_mm", 0.2))
    global_gap = max(configured, int(board.GetDesignSettings().m_MinClearance))
    hole_gap = max(0, int(board.GetDesignSettings().m_HoleClearance))
    overrides = {
        name: mm(settings.get("clearance_mm", 0))
        for name, settings in policy.get("net_overrides", {}).items()
    }
    target = request["nets"][0]["name"]
    overrides[target] = max(
        overrides.get(target, 0),
        mm(request.get("shared", {}).get("clearance_mm", 0)),
        mm(request["nets"][0].get("clearance_mm", 0)),
    )

    def clearance(a, b):
        return max(
            global_gap, overrides.get(a.GetNetname(), 0), overrides.get(b.GetNetname(), 0),
            *(classes[name].GetClearance() for name in (a.GetNetClassName(), b.GetNetClassName())
              if name in classes),
        )

    original_pads = {pad.m_Uuid.AsString(): pad for pad in board.GetPads()}
    moved_pads = tuple(pad for part in copies for pad in part.Pads())
    static_pads = tuple(
        pad for part in board.GetFootprints() if part.m_Uuid.AsString() not in selected_ids
        for pad in part.Pads() if pad.IsOnLayer(pcbnew.B_Cu)
    )
    pads = (*moved_pads, *static_pads)
    tracks = tuple(item for item in board.GetTracks()
                   if item.IsOnLayer(pcbnew.B_Cu) and item.m_Uuid.AsString() not in removed_ids)
    def conflicts(shape, other, gap):
        other_shape = other.GetEffectiveShape(pcbnew.B_Cu)
        if isinstance(other, pcbnew.PAD):
            drill = other.GetDrillSize()
            hole = other.GetEffectiveHoleShape() if drill.x or drill.y else None
        else:
            hole = other.GetEffectiveHoleShape() if isinstance(other, pcbnew.PCB_VIA) else None
        return (
            other_shape is not None and shape.Collide(other_shape, max(0, gap - 1))
        ) or (hole is not None and shape.Collide(hole, max(0, hole_gap - 1)))

    collateral = {}
    for pad in moved_pads:
        shape = pad.GetEffectiveShape(pcbnew.B_Cu)
        original_shape = original_pads[pad.m_Uuid.AsString()].GetEffectiveShape(pcbnew.B_Cu)
        box = pad.GetBoundingBox()
        for other in pads:
            if other.GetParentFootprint().m_Uuid.AsString() == pad.GetParentFootprint().m_Uuid.AsString():
                # A footprint move leaves its own pad-to-pad geometry intact.
                continue
            if other.GetNetname() and other.GetNetname() == pad.GetNetname():
                continue
            gap = clearance(pad, other)
            bounds = other.GetBoundingBox()
            bounds.Inflate(max(gap, hole_gap))
            if not box.Intersects(bounds):
                continue
            original_other = original_pads[other.m_Uuid.AsString()]
            if conflicts(shape, other, gap) and not conflicts(original_shape, original_other, gap):
                raise UnsupportedPlacement("Moved pad would violate another pad's clearance")
        for item in tracks:
            if item.GetNetname() and item.GetNetname() == pad.GetNetname():
                continue
            gap = clearance(pad, item)
            bounds = item.GetBoundingBox()
            bounds.Inflate(max(gap, hole_gap))
            if not box.Intersects(bounds):
                continue
            if not conflicts(shape, item, gap) or conflicts(original_shape, item, gap):
                continue
            name = item.GetNetname()
            if (not name or name.startswith("unconnected-") or name in excluded
                    or item.IsLocked() or item.GetParentGroup() is not None
                    or isinstance(item, pcbnew.PCB_ARC)
                    or (isinstance(item, pcbnew.PCB_VIA)
                        and item.GetViaType() != pcbnew.VIATYPE_THROUGH)
                    or not any(p.GetNetname() == name for p in board.GetPads())):
                raise UnsupportedPlacement("Moved pad would hit copper that cannot be rerouted")
            collateral[item.m_Uuid.AsString()] = item
    return tuple(collateral.values())


def _include_obstructions(board, removals, dimensions, items):
    """Add colliding ordinary copper and its dimensions to a repair transaction."""
    import pcbnew

    for item in items:
        name = item.GetNetname()
        via = isinstance(item, pcbnew.PCB_VIA)
        removals.setdefault(name, []).append({
            "kind": "via" if via else "segment", "uuid": item.m_Uuid.AsString()
        })
        width, size, drill = dimensions.get(name, (0, 0, 0))
        if via:
            sizes = [copper_width(item, layer=board.GetLayerID(layer))
                     for layer in enabled_copper_layers(board) if item.IsOnLayer(board.GetLayerID(layer))]
            size, drill = max(size, *sizes), max(drill, item.GetDrillValue())
        else:
            width = max(width, copper_width(item, layer=item.GetLayer()))
        dimensions[name] = width, size, drill
    for name, selected in removals.items():
        validate_removals(board, selected, name)


def _extend_exposed_copper(board, removals, copies, selected_ids, *, max_items):
    """Include newly exposed leaf traces in the same bounded repair transaction."""
    import pcbnew

    tracks = tuple(board.GetTracks())
    by_id = {item.m_Uuid.AsString(): item for item in tracks}
    chosen = {entry["uuid"] for items in removals.values() for entry in items}
    if not chosen <= by_id.keys():
        raise ValueError("A placement repair references missing copper")
    original = set(chosen)
    pads = tuple(
        pad for part in board.GetFootprints() if part.m_Uuid.AsString() not in selected_ids
        for pad in part.Pads()
    ) + tuple(pad for part in copies for pad in part.Pads())
    zones = tuple(zone for zone in board.Zones() if not zone.GetIsRuleArea())

    def touches(item, point, layer):
        return item.IsOnLayer(layer) and item.HitTest(point)

    while True:
        additional = set()
        removed = [by_id[identifier] for identifier in chosen]
        affected = {item.GetNetname() for item in removed}
        for item in tracks:
            identifier = item.m_Uuid.AsString()
            name = item.GetNetname()
            if identifier in chosen or name not in affected or isinstance(item, pcbnew.PCB_VIA):
                continue
            layer = item.GetLayer()
            for point in (item.GetStart(), item.GetEnd()):
                if not any(other.GetNetname() == name and touches(other, point, layer)
                           for other in removed):
                    continue
                if any(pad.GetNetname() == name and touches(pad, point, layer) for pad in pads):
                    continue
                if any(other.m_Uuid.AsString() not in chosen
                       and other.m_Uuid.AsString() != identifier and other.GetNetname() == name
                       and touches(other, point, layer) for other in tracks):
                    continue
                # A zone envelope is a conservative possible attachment; native
                # refill and DRC will resolve whether copper is actually present.
                if any(zone.GetNetname() == name and zone.IsOnLayer(layer)
                       and zone.GetBoundingBox().Contains(point) for zone in zones):
                    continue
                if item.IsLocked() or item.GetParentGroup() is not None or isinstance(item, pcbnew.PCB_ARC):
                    raise UnsupportedPlacement("An exposed trace cannot be safely rerouted")
                additional.add(identifier)
                break
        if not additional:
            break
        if len(chosen | additional) > max_items:
            raise UnsupportedPlacement("Exposed trace repair exceeds the copper item limit")
        chosen.update(additional)
    return tuple(by_id[identifier] for identifier in sorted(chosen - original))


def singleton_group_members(source: str) -> frozenset[str]:
    """Find unshared, unlocked groups containing exactly one board item."""
    groups = []
    for group in parse(source).children("group"):
        ids, member_forms = group.children("uuid"), group.children("members")
        if len(ids) != 1 or len(ids[0].atoms()) != 2 or len(member_forms) != 1:
            continue
        identifier = ids[0].atoms()[1].value
        members = tuple(item.value for item in member_forms[0].atoms()[1:])
        locked = bool(group.children("locked")) or any(item.value == "locked" for item in group.atoms())
        groups.append((identifier, members, locked))
    counts = Counter(member for _, members, _ in groups for member in members)
    return frozenset(members[0] for identifier, members, locked in groups
                     if not locked and len(members) == 1 and counts[members[0]] == 1
                     and counts[identifier] == 0)


def singleton_footprint_groups(board) -> frozenset[str]:
    """Read current native group membership; missing source fails closed."""
    source = Path(board.GetFileName())
    if not source.is_file():
        return frozenset()
    return singleton_group_members(source.read_text(encoding="utf-8-sig"))


def _anchor_requests(board, request, policy, recovery, anchors, protected_nets,
                     anchor_nets: set[str], selection: str):
    """Generate private resistor half-turns while leaving anchor copper intact."""
    import pcbnew

    if not recovery.placement_candidates or not board.GetFileName():
        return [], []
    target = request["nets"][0]["name"]
    excluded = set(protected_nets) | set(policy.get("exclude_nets", [])) | {""}
    if target in excluded:
        return [], []
    text = Path(board.GetFileName()).read_text(encoding="utf-8-sig")
    forbidden, counts = set(), {}
    for block in parse(text).children("footprint"):
        refs = [item.atoms()[2].value for item in block.children("property")
                if len(item.atoms()) >= 3 and item.atoms()[1].value == "Reference"]
        for reference in refs:
            counts[reference] = counts.get(reference, 0) + 1
            if '"F.SilkS"' in text[block.start:block.end]:
                forbidden.add(reference)

    singleton_members = singleton_group_members(text)
    footprints = tuple(board.GetFootprints())
    all_geometry = {part.m_Uuid.AsString(): _courtyard_geometry(part) for part in footprints}
    box = board.GetBoardEdgesBoundingBox()
    options = []
    for part in footprints:
        pose = _anchor_pose(part, excluded, anchor_nets)
        if (pose is None or counts.get(pose.reference) != 1 or pose.reference in forbidden
                or (part.GetParentGroup() is not None
                    and part.m_Uuid.AsString() not in singleton_members)):
            continue
        distance = min(math.hypot(pad.GetPosition().x - anchor.point.x,
                                  pad.GetPosition().y - anchor.point.y)
                       for pad in part.Pads() for anchor in anchors)
        if pose.signal_net == target or distance <= mm(recovery.rip_up_radius_mm * 2):
            options.append((pose.signal_net != target, distance, pose.reference, part, pose))

    choices = []
    for _, _, reference, part, pose in sorted(options, key=lambda item: item[:3]):
        if not (box.GetX() / 1e6 < pose.x_mm < box.GetRight() / 1e6
                and box.GetY() / 1e6 < pose.y_mm < box.GetBottom() / 1e6):
            continue
        selected = [(0, reference, part, {pose.signal_net})]
        names = {target, pose.signal_net}
        if len(names) > recovery.placement_nets:
            continue
        try:
            removals, dimensions = _incident_copper(board, selected, names)
            if sum(map(len, removals.values())) > recovery.placement_items:
                continue
            clone = pcbnew.FOOTPRINT(part)
            clone.SetOrientationDegrees(pose.angle_deg)
            clone.SetPosition(pcbnew.VECTOR2I(mm(pose.x_mm), mm(pose.y_mm)))
            uid = part.m_Uuid.AsString()
            before = _courtyard_conflicts({uid: all_geometry[uid]}, all_geometry)
            if _courtyard_conflicts({uid: _courtyard_geometry(clone)}, all_geometry) - before:
                continue
            removed_ids = {item["uuid"] for items in removals.values() for item in items}
            obstacles = _placement_obstructions(board, [clone], {uid}, removed_ids,
                                               policy, excluded, request)
            _include_obstructions(board, removals, dimensions, obstacles)
            exposed = _extend_exposed_copper(board, removals, [clone], {uid},
                                              max_items=recovery.placement_items)
            _include_obstructions(board, removals, dimensions, exposed)
        except UnsupportedPlacement:
            continue
        affected = names | set(removals)
        if (affected & excluded or len(affected) > recovery.placement_nets
                or sum(map(len, removals.values())) > recovery.placement_items):
            continue
        candidate = deepcopy(request)
        candidate.pop("orders", None)
        candidate.update(recovery_stage=4, rip_up=removals,
                         movable=[{"reference": reference,
                                   "positions_mm": [[pose.x_mm, pose.y_mm]],
                                   "rotations_deg": [pose.angle_deg]}],
                         repair_max_islands=recovery.repair_islands,
                         checkpoint_offset=0, checkpoint_margin_mm=recovery.wide_margin_mm)
        candidate["nets"] = [
            _net_settings(board, name, policy, recovery, dimensions,
                          {**request["shared"], **request["nets"][0]} if name == target else None)
            for name in (target, *sorted(affected - {target}))
        ]
        candidate["shared"]["bounds_mm"] = [
            box.GetX() / 1e6, box.GetY() / 1e6,
            box.GetRight() / 1e6, box.GetBottom() / 1e6,
        ]
        choices.append((candidate, {"selection": selection,
                                    "references": [reference], "nets": sorted(affected),
                                    "items": sum(map(len, removals.values()))}))
        if len(choices) >= recovery.placement_candidates:
            break
    return [item[0] for item in choices], [item[1] for item in choices]


def ground_anchor_requests(board, request, policy, recovery, anchors, protected_nets):
    """Generate private resistor half-turns while leaving ground copper intact."""
    return _anchor_requests(board, request, policy, recovery, anchors, protected_nets,
                            {"GND"}, "ground_anchor_hypothesis")


def protected_anchor_requests(board, request, policy, recovery, anchors, protected_nets):
    """Generate private hypotheses; working-save protection remains unchanged."""
    anchor_nets = set(protected_nets) - set(policy.get("exclude_nets", ())) - {"GND", ""}
    return _anchor_requests(board, request, policy, recovery, anchors, protected_nets,
                            anchor_nets, "protected_anchor_hypothesis")


def placement_requests(board, request, policy, recovery, anchors, critical_nets):
    """Sample independent single/package-pair poses; repair all affected nets."""
    if not recovery.placement_candidates or not board.GetFileName():
        return [], []
    target = request["nets"][0]["name"]
    options = _eligible(
        board,
        anchors,
        set(critical_nets) | set(policy.get("exclude_nets", [])),
        recovery,
    )
    nearest = options[:3]
    selections = [(item,) for item in nearest]
    selections.extend(itertools.combinations(nearest, 2))
    box = board.GetBoardEdgesBoundingBox()
    footprints = tuple(board.GetFootprints())
    all_geometry = {
        part.m_Uuid.AsString(): _courtyard_geometry(part) for part in footprints
    }
    streams = []
    for selected in selections:
        selected_ids = {item[2].m_Uuid.AsString() for item in selected}
        before = _courtyard_conflicts(
            {identifier: all_geometry[identifier] for identifier in selected_ids}, all_geometry
        )
        names = set().union(*(item[3] for item in selected)) | {target}
        if len(names) > recovery.placement_nets:
            continue
        try:
            removals, dimensions = _incident_copper(board, selected, names)
        except UnsupportedPlacement:
            continue
        if sum(map(len, removals.values())) > recovery.placement_items:
            continue
        choices = []
        # Interleave axes, rotations and coarse/fine translations so a small
        # candidate cap does not consume every slot on one distance/package.
        poses = (
            (0.5, 1, 0, 0),
            (1.0, 0, 1, 0),
            (0.0, 0, 0, 90),
            (2.0, -1, 0, 0),
            (0.5, 0, -1, 0),
            (1.0, -1, 0, 0),
            (2.0, 0, 1, 0),
            (0.0, 0, 0, -90),
        )
        for step, dx, dy, turn in poses:
            candidate = deepcopy(request)
            candidate.pop("orders", None)
            candidate.update(
                recovery_stage=4,
                rip_up=removals,
                movable=[],
                repair_max_islands=recovery.repair_islands,
                checkpoint_offset=0,
                checkpoint_margin_mm=recovery.wide_margin_mm,
            )
            for i, (_, ref, footprint, _) in enumerate(selected):
                origin, sign = footprint.GetPosition(), -1 if i else 1
                x, y = (
                    round(origin.x / 1e6 + sign * dx * step, 6),
                    round(origin.y / 1e6 + sign * dy * step, 6),
                )
                if (
                    not box.GetX() / 1e6 < x < box.GetRight() / 1e6
                    or not box.GetY() / 1e6 < y < box.GetBottom() / 1e6
                ):
                    break
                candidate["movable"].append(
                    {
                        "reference": ref,
                        "positions_mm": [[x, y]],
                        "rotations_deg": [
                            (float(footprint.GetOrientationDegrees()) + turn) % 360
                        ],
                    }
                )
            if len(candidate["movable"]) != len(selected):
                continue
            # Test actual KiCad courtyard polygons on detached footprint copies.
            # Existing overlaps remain baseline findings for native DRC; do not
            # spend routing time on a pose that introduces a new overlap.
            import pcbnew

            copies = []
            moved_geometry = {}
            for item, choice in zip(selected, candidate["movable"], strict=True):
                copy = pcbnew.FOOTPRINT(item[2])
                x, y = choice["positions_mm"][0]
                copy.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
                copy.SetOrientationDegrees(choice["rotations_deg"][0])
                copies.append(copy)
                moved_geometry[item[2].m_Uuid.AsString()] = _courtyard_geometry(copy)
            if _courtyard_conflicts(moved_geometry, all_geometry) - before:
                continue
            candidate_removals = deepcopy(removals)
            candidate_dimensions = dict(dimensions)
            removed_ids = {item["uuid"] for items in candidate_removals.values() for item in items}
            try:
                obstructions = _placement_obstructions(
                    board, copies, selected_ids, removed_ids, policy,
                    set(critical_nets) | set(policy.get("exclude_nets", [])), request,
                )
                _include_obstructions(board, candidate_removals, candidate_dimensions, obstructions)
                exposed = _extend_exposed_copper(
                    board, candidate_removals, copies, selected_ids,
                    max_items=recovery.placement_items,
                )
                _include_obstructions(board, candidate_removals, candidate_dimensions, exposed)
            except UnsupportedPlacement:
                continue
            candidate_names = names | set(candidate_removals)
            if (len(candidate_names) > recovery.placement_nets
                    or sum(map(len, candidate_removals.values())) > recovery.placement_items):
                continue
            candidate["rip_up"] = candidate_removals
            candidate["nets"] = [
                _net_settings(
                    board,
                    name,
                    policy,
                    recovery,
                    candidate_dimensions,
                    {**request["shared"], **request["nets"][0]}
                    if name == target
                    else None,
                )
                for name in (target, *sorted(candidate_names - {target}))
            ]
            candidate["shared"]["bounds_mm"] = [
                box.GetX() / 1e6,
                box.GetY() / 1e6,
                box.GetRight() / 1e6,
                box.GetBottom() / 1e6,
            ]
            choices.append(
                (
                    candidate,
                    {
                        "selection": "bottom_placement_hypothesis",
                        "references": [item[1] for item in selected],
                        "nets": sorted(candidate_names),
                        "displacement_mm": step,
                        "rotation_deg": turn,
                        "items": sum(map(len, candidate_removals.values())),
                    },
                )
            )
        streams.append(choices)
    interleaved = (
        pair
        for row in itertools.zip_longest(*streams)
        for pair in row
        if pair is not None
    )
    chosen = list(itertools.islice(interleaved, recovery.placement_candidates))
    return [pair[0] for pair in chosen], [pair[1] for pair in chosen]
