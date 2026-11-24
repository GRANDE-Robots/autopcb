"""Generate bounded placement transactions from blocked endpoints."""

from __future__ import annotations

from core.pcb_io import enabled_copper_layers

import itertools
import math
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from core.kicad_sexpr import parse
from core.pcb_io import copper_width, net_classes
from pcb.move_scope import MoveScope
from pcb.net_routing.pose_sampler import PoseSampler
from pcb.route_native import _pad_obstacle, validate_removals
from pcb.route_optimizer import mm
from core.compat import strict_zip


@dataclass(frozen=True)
class GroundAnchorPose:
    """A half-turn of a two-pad part around one unchanged protected pad."""

    reference: str
    signal_net: str
    anchor_id: str
    x_mm: float
    y_mm: float
    angle_deg: float
    anchor_net: str


def _anchor_pose(
    footprint, excluded: set[str], anchor_nets: set[str]
) -> GroundAnchorPose | None:
    """Propose a half-turn preserving one symmetric protected pad.

    This is a geometric hypothesis, not electrical qualification. All
    disturbed signal copper must still be rerouted and independently checked.
    """
    import pcbnew

    # Whether the part may move at all is the placement policy's decision.
    reference, side = str(footprint.GetReference()), footprint.GetLayer()
    if side not in (pcbnew.F_Cu, pcbnew.B_Cu):
        return None
    other_side = pcbnew.F_Cu if side == pcbnew.B_Cu else pcbnew.B_Cu
    pads = tuple(footprint.Pads())
    if (
        len(pads) != 2
        or len({str(pad.GetNumber()) for pad in pads}) != 2
        or any(
            not str(pad.GetNumber())
            or pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD
            or not pad.IsOnLayer(side)
            or pad.IsOnLayer(other_side)
            for pad in pads
        )
    ):
        return None
    anchored = [pad for pad in pads if str(pad.GetNetname()) in anchor_nets & excluded]
    signal = [
        pad
        for pad in pads
        if str(pad.GetNetname()) not in excluded | {""}
        and not str(pad.GetNetname()).startswith("unconnected-")
    ]
    if len(anchored) != 1 or len(signal) != 1:
        return None
    pad = anchored[0]
    mask, paste = (
        (pcbnew.F_Mask, pcbnew.F_Paste)
        if side == pcbnew.F_Cu
        else (pcbnew.B_Mask, pcbnew.B_Paste)
    )
    layers = tuple(layer for layer in (side, mask, paste) if pad.IsOnLayer(layer))
    for layer in layers:
        offset = pad.GetOffset(layer)
        if (
            pad.GetShape(layer)
            not in {
                pcbnew.PAD_SHAPE_CIRCLE,
                pcbnew.PAD_SHAPE_RECT,
                pcbnew.PAD_SHAPE_OVAL,
                pcbnew.PAD_SHAPE_ROUNDRECT,
            }
            or pad.GetChamferPositions(layer)
            or offset.x != 0
            or offset.y != 0
        ):
            return None
    point, origin = pad.GetPosition(), footprint.GetPosition()
    x, y = 2 * point.x - origin.x, 2 * point.y - origin.y
    angle = (float(footprint.GetOrientationDegrees()) + 180.0) % 360.0
    clone = pcbnew.FOOTPRINT(footprint)
    clone.SetOrientationDegrees(angle)
    clone.SetPosition(pcbnew.VECTOR2I(x, y))
    copied = [
        item for item in clone.Pads() if item.m_Uuid.AsString() == pad.m_Uuid.AsString()
    ]
    if len(copied) != 1:
        return None
    rotated = copied[0]

    def box(item):
        return item.GetX(), item.GetY(), item.GetWidth(), item.GetHeight()

    if (
        rotated.GetPosition() != point
        or any(
            box(pad.GetBoundingBox(layer)) != box(rotated.GetBoundingBox(layer))
            for layer in layers
        )
        or (rotated.GetOrientationDegrees() - pad.GetOrientationDegrees()) % 180.0 != 0
    ):
        return None
    return GroundAnchorPose(
        reference,
        str(signal[0].GetNetname()),
        pad.m_Uuid.AsString(),
        x / 1e6,
        y / 1e6,
        angle,
        str(pad.GetNetname()),
    )


def ground_anchor_pose(footprint, excluded: set[str]) -> GroundAnchorPose | None:
    """Propose a private half-turn around an unchanged ground pad."""
    return _anchor_pose(footprint, excluded, {"GND"})


def protected_anchor_pose(footprint, excluded: set[str]) -> GroundAnchorPose | None:
    """Propose a private half-turn around another unchanged protected pad."""
    return _anchor_pose(footprint, excluded, excluded - {"GND", ""})


class UnsupportedPlacement(ValueError):
    """A candidate requires moving copper that this recovery cannot replace."""


class MoveGuide:
    """Rank placement moves by the relaxed route a blocked net wants to take.

    The pads of eligible parts and the copper that may be replaced are relaxed
    in a diagnostic search. Parts that route crosses are tried first, poses
    that clear it before others, and the copper it crosses joins a move's
    repair, so one candidate moves the part and reroutes what it crowds.
    Every candidate is still rerouted and checked natively.
    """

    def __init__(self, board, witnesses=(), movable=frozenset()):
        self.board, self.witnesses = board, tuple(witnesses)
        self.movable = frozenset(movable)

    @classmethod
    def diagnose(
        cls,
        board,
        request,
        policy,
        recovery,
        anchors,
        critical_nets,
        options,
        nearby,
        records=(),
    ):
        """Relax the parts near the anchors, or every movable part when the net is cut off.

        A net that pads and saved copper leave no way through may need a
        distant part moved. One that still has a way through does not.
        ``records`` are the copper items a repair may remove; the relaxed
        route may cross them as well.
        """
        import pcbnew
        from pcb.net_routing.obstructions import diagnose
        from pcb.net_routing.rework import ReworkWeights
        from pcb.routability import GridModel

        weights = ReworkWeights.from_config(request.get("rework"))
        target = request["nets"][0]["name"]
        cut_off = len(options) > len(nearby) and GridModel(
            board, policy=policy, saved_copper=True
        ).cut_off(target)
        # The parts a route starts and ends on are its endpoints, not obstructions.
        movable = {
            item[2].m_Uuid.AsString(): weights.placement_mm
            for item in (options if cut_off else nearby)
            if not any(
                pad.HitTest(pcbnew.VECTOR2I(anchor.point.x, anchor.point.y))
                for pad in item[2].Pads()
                for anchor in anchors
            )
        }
        if not movable or not recovery.obstruction_trials:
            return cls(board)
        settings = {**request["shared"], **request["nets"][0]}
        witnesses, _ = diagnose(
            board,
            anchors,
            records,
            settings,
            recovery,
            critical_nets,
            weights,
            movable=movable,
            first_feasible=True,
        )
        return cls(
            board,
            (witness for witness in witnesses if witness.identifiers & movable.keys()),
            movable.keys(),
        )

    def parts(self, witness) -> frozenset[str]:
        """The movable parts a witness names, without the copper it crosses."""
        return witness.identifiers & self.movable

    def names(self, selection) -> bool:
        identifiers = {item[2].m_Uuid.AsString() for item in selection}
        return any(self.parts(witness) <= identifiers for witness in self.witnesses)

    def crossings(self, identifiers) -> tuple[str, ...]:
        """The copper that diagnosed routes through these parts cross."""
        return tuple(
            sorted(
                {
                    uid
                    for witness in self.witnesses
                    if self.parts(witness) <= identifiers
                    for uid in witness.identifiers - self.movable
                }
            )
        )

    @property
    def corridors(self):
        return [
            witness.corridor
            for witness in self.witnesses
            if witness.corridor is not None
        ]

    def selections(self, options, nearby):
        """Diagnosed parts first, then the nearest singles and pairs."""
        by_id = {item[2].m_Uuid.AsString(): item for item in options}
        diagnosed = [
            tuple(
                sorted(
                    (by_id[uid] for uid in self.parts(witness)),
                    key=lambda item: item[:2],
                )
            )
            for witness in self.witnesses
            if len(self.parts(witness)) <= 2
        ]
        nearest = nearby[:3]
        result, seen = [], set()
        for selection in (
            *diagnosed,
            *((item,) for item in nearest),
            *itertools.combinations(nearest, 2),
        ):
            key = tuple(item[1] for item in selection)
            if key not in seen:
                seen.add(key)
                result.append(selection)
        return result

    def clears(self, copies) -> bool:
        """Whether moved pads leave every diagnosed corridor open."""
        corridors = self.corridors
        if not corridors:
            return False
        layers = enabled_copper_layers(self.board)
        shapes = []
        for copy in copies:
            for pad in copy.Pads():
                active = tuple(
                    layer
                    for layer in layers
                    if pad.IsOnLayer(self.board.GetLayerID(layer))
                )
                if active:
                    shapes.append(_pad_obstacle(self.board, pad, active))
        return not any(corridor.blocked_by(shapes) for corridor in corridors)


def _eligible(board, anchors, excluded, scope):
    """Every part the scope lets move, nearest the anchors first.

    Cheap selection only; native placement validation checks every proposal.
    """
    movable = scope.movable(Path(board.GetFileName()).read_text(encoding="utf-8-sig"))
    classes = net_classes(board)
    result = []
    for footprint in board.GetFootprints():
        ref, pads = footprint.GetReference(), tuple(footprint.Pads())
        if (
            ref not in movable
            or not pads
            or any(p.GetNetClassName() not in classes for p in pads)
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
            layer
            for layer in enabled_copper_layers(board)
            if item.IsOnLayer(board.GetLayerID(layer))
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


def _beyond_edge_clearance(board, pads) -> bool:
    """Whether any pad sits closer to the outline box than the copper edge rule."""
    box, gap = (
        board.GetBoardEdgesBoundingBox(),
        board.GetDesignSettings().m_CopperEdgeClearance,
    )
    return any(
        pad.GetBoundingBox().GetLeft() < box.GetLeft() + gap
        or pad.GetBoundingBox().GetRight() > box.GetRight() - gap
        or pad.GetBoundingBox().GetTop() < box.GetTop() + gap
        or pad.GetBoundingBox().GetBottom() > box.GetBottom() - gap
        for pad in pads
    )


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


def _placement_obstructions(
    board, copies, selected_ids, removed_ids, policy, excluded, request
):
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
            global_gap,
            overrides.get(a.GetNetname(), 0),
            overrides.get(b.GetNetname(), 0),
            *(
                classes[name].GetClearance()
                for name in (a.GetNetClassName(), b.GetNetClassName())
                if name in classes
            ),
        )

    original_pads = {pad.m_Uuid.AsString(): pad for pad in board.GetPads()}
    moved_pads = tuple(pad for part in copies for pad in part.Pads())
    static_pads = tuple(
        pad
        for part in board.GetFootprints()
        if part.m_Uuid.AsString() not in selected_ids
        for pad in part.Pads()
    )
    pads = (*moved_pads, *static_pads)
    tracks = tuple(
        item for item in board.GetTracks() if item.m_Uuid.AsString() not in removed_ids
    )
    layers = tuple(board.GetLayerID(name) for name in enabled_copper_layers(board))

    def conflicts(shape, other, gap, layer):
        other_shape = other.GetEffectiveShape(layer)
        if isinstance(other, pcbnew.PAD):
            drill = other.GetDrillSize()
            hole = other.GetEffectiveHoleShape() if drill.x or drill.y else None
        else:
            hole = (
                other.GetEffectiveHoleShape()
                if isinstance(other, pcbnew.PCB_VIA)
                else None
            )
        return (
            other_shape is not None and shape.Collide(other_shape, max(0, gap - 1))
        ) or (hole is not None and shape.Collide(hole, max(0, hole_gap - 1)))

    collateral = {}
    for pad, layer in (
        (pad, layer) for pad in moved_pads for layer in layers if pad.IsOnLayer(layer)
    ):
        shape = pad.GetEffectiveShape(layer)
        original_shape = original_pads[pad.m_Uuid.AsString()].GetEffectiveShape(layer)
        box = pad.GetBoundingBox()
        for other in pads:
            if (
                other.GetParentFootprint().m_Uuid.AsString()
                == pad.GetParentFootprint().m_Uuid.AsString()
            ):
                # A footprint move leaves its own pad-to-pad geometry intact.
                continue
            if not other.IsOnLayer(layer) or (
                other.GetNetname() and other.GetNetname() == pad.GetNetname()
            ):
                continue
            gap = clearance(pad, other)
            bounds = other.GetBoundingBox()
            bounds.Inflate(max(gap, hole_gap))
            if not box.Intersects(bounds):
                continue
            original_other = original_pads[other.m_Uuid.AsString()]
            if conflicts(shape, other, gap, layer) and not conflicts(
                original_shape, original_other, gap, layer
            ):
                raise UnsupportedPlacement(
                    "Moved pad would violate another pad's clearance"
                )
        for item in tracks:
            if not item.IsOnLayer(layer) or (
                item.GetNetname() and item.GetNetname() == pad.GetNetname()
            ):
                continue
            gap = clearance(pad, item)
            bounds = item.GetBoundingBox()
            bounds.Inflate(max(gap, hole_gap))
            if not box.Intersects(bounds):
                continue
            if not conflicts(shape, item, gap, layer) or conflicts(
                original_shape, item, gap, layer
            ):
                continue
            name = item.GetNetname()
            if (
                not name
                or name.startswith("unconnected-")
                or name in excluded
                or item.IsLocked()
                or item.GetParentGroup() is not None
                or isinstance(item, pcbnew.PCB_ARC)
                or (
                    isinstance(item, pcbnew.PCB_VIA)
                    and item.GetViaType() != pcbnew.VIATYPE_THROUGH
                )
                or not any(p.GetNetname() == name for p in board.GetPads())
            ):
                raise UnsupportedPlacement(
                    "Moved pad would hit copper that cannot be rerouted"
                )
            collateral[item.m_Uuid.AsString()] = item
    return tuple(collateral.values())


def _include_obstructions(board, removals, dimensions, items):
    """Add colliding ordinary copper and its dimensions to a repair transaction."""
    import pcbnew

    for item in items:
        name = item.GetNetname()
        via = isinstance(item, pcbnew.PCB_VIA)
        removals.setdefault(name, []).append(
            {"kind": "via" if via else "segment", "uuid": item.m_Uuid.AsString()}
        )
        width, size, drill = dimensions.get(name, (0, 0, 0))
        if via:
            sizes = [
                copper_width(item, layer=board.GetLayerID(layer))
                for layer in enabled_copper_layers(board)
                if item.IsOnLayer(board.GetLayerID(layer))
            ]
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
        pad
        for part in board.GetFootprints()
        if part.m_Uuid.AsString() not in selected_ids
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
            if (
                identifier in chosen
                or name not in affected
                or isinstance(item, pcbnew.PCB_VIA)
            ):
                continue
            layer = item.GetLayer()
            for point in (item.GetStart(), item.GetEnd()):
                if not any(
                    other.GetNetname() == name and touches(other, point, layer)
                    for other in removed
                ):
                    continue
                if any(
                    pad.GetNetname() == name and touches(pad, point, layer)
                    for pad in pads
                ):
                    continue
                if any(
                    other.m_Uuid.AsString() not in chosen
                    and other.m_Uuid.AsString() != identifier
                    and other.GetNetname() == name
                    and touches(other, point, layer)
                    for other in tracks
                ):
                    continue
                # A zone envelope is a conservative possible attachment; native
                # refill and DRC will resolve whether copper is actually present.
                if any(
                    zone.GetNetname() == name
                    and zone.IsOnLayer(layer)
                    and zone.GetBoundingBox().Contains(point)
                    for zone in zones
                ):
                    continue
                if (
                    item.IsLocked()
                    or item.GetParentGroup() is not None
                    or isinstance(item, pcbnew.PCB_ARC)
                ):
                    raise UnsupportedPlacement(
                        "An exposed trace cannot be safely rerouted"
                    )
                additional.add(identifier)
                break
        if not additional:
            break
        if len(chosen | additional) > max_items:
            raise UnsupportedPlacement(
                "Exposed trace repair exceeds the copper item limit"
            )
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
        locked = bool(group.children("locked")) or any(
            item.value == "locked" for item in group.atoms()
        )
        groups.append((identifier, members, locked))
    counts = Counter(member for _, members, _ in groups for member in members)
    return frozenset(
        members[0]
        for identifier, members, locked in groups
        if not locked
        and len(members) == 1
        and counts[members[0]] == 1
        and counts[identifier] == 0
    )


def singleton_footprint_groups(board) -> frozenset[str]:
    """Read current native group membership; missing source fails closed."""
    source = Path(board.GetFileName())
    if not source.is_file():
        return frozenset()
    return singleton_group_members(source.read_text(encoding="utf-8-sig"))


def _anchor_requests(
    board,
    request,
    policy,
    recovery,
    anchors,
    protected_nets,
    anchor_nets: set[str],
    selection: str,
):
    """Generate private two-pad half-turns while leaving anchor copper intact."""
    import pcbnew

    if not recovery.placement_candidates or not board.GetFileName():
        return [], []
    target = request["nets"][0]["name"]
    excluded = set(protected_nets) | set(policy.get("exclude_nets", [])) | {""}
    if target in excluded:
        return [], []
    text = Path(board.GetFileName()).read_text(encoding="utf-8-sig")
    movable = MoveScope.from_config(policy.get("placement")).movable(text)
    singleton_members = singleton_group_members(text)
    footprints = tuple(board.GetFootprints())
    all_geometry = {
        part.m_Uuid.AsString(): _courtyard_geometry(part) for part in footprints
    }
    box = board.GetBoardEdgesBoundingBox()
    options = []
    for part in footprints:
        pose = _anchor_pose(part, excluded, anchor_nets)
        if (
            pose is None
            or pose.reference not in movable
            or (
                part.GetParentGroup() is not None
                and part.m_Uuid.AsString() not in singleton_members
            )
        ):
            continue
        distance = min(
            math.hypot(
                pad.GetPosition().x - anchor.point.x,
                pad.GetPosition().y - anchor.point.y,
            )
            for pad in part.Pads()
            for anchor in anchors
        )
        if pose.signal_net == target or distance <= mm(recovery.rip_up_radius_mm * 2):
            options.append(
                (pose.signal_net != target, distance, pose.reference, part, pose)
            )

    choices = []
    for _, _, reference, part, pose in sorted(options, key=lambda item: item[:3]):
        if not (
            box.GetX() / 1e6 < pose.x_mm < box.GetRight() / 1e6
            and box.GetY() / 1e6 < pose.y_mm < box.GetBottom() / 1e6
        ):
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
            if (
                _courtyard_conflicts({uid: _courtyard_geometry(clone)}, all_geometry)
                - before
            ):
                continue
            removed_ids = {
                item["uuid"] for items in removals.values() for item in items
            }
            obstacles = _placement_obstructions(
                board, [clone], {uid}, removed_ids, policy, excluded, request
            )
            _include_obstructions(board, removals, dimensions, obstacles)
            exposed = _extend_exposed_copper(
                board, removals, [clone], {uid}, max_items=recovery.placement_items
            )
            _include_obstructions(board, removals, dimensions, exposed)
        except UnsupportedPlacement:
            continue
        affected = names | set(removals)
        if (
            affected & excluded
            or len(affected) > recovery.placement_nets
            or sum(map(len, removals.values())) > recovery.placement_items
        ):
            continue
        candidate = deepcopy(request)
        candidate.pop("orders", None)
        candidate.update(
            recovery_stage=4,
            rip_up=removals,
            movable=[
                {
                    "reference": reference,
                    "positions_mm": [[pose.x_mm, pose.y_mm]],
                    "rotations_deg": [pose.angle_deg],
                }
            ],
            repair_max_islands=recovery.repair_islands,
            checkpoint_offset=0,
            checkpoint_margin_mm=recovery.wide_margin_mm,
        )
        candidate["nets"] = [
            _net_settings(
                board,
                name,
                policy,
                recovery,
                dimensions,
                {**request["shared"], **request["nets"][0]} if name == target else None,
            )
            for name in (target, *sorted(affected - {target}))
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
                    "selection": selection,
                    "references": [reference],
                    "nets": sorted(affected),
                    "items": sum(map(len, removals.values())),
                },
            )
        )
        if len(choices) >= recovery.placement_candidates:
            break
    return [item[0] for item in choices], [item[1] for item in choices]


def ground_anchor_requests(board, request, policy, recovery, anchors, protected_nets):
    """Generate private two-pad half-turns while leaving ground copper intact."""
    return _anchor_requests(
        board,
        request,
        policy,
        recovery,
        anchors,
        protected_nets,
        {"GND"},
        "ground_anchor_hypothesis",
    )


def protected_anchor_requests(
    board, request, policy, recovery, anchors, protected_nets
):
    """Generate private hypotheses; working-save protection remains unchanged."""
    anchor_nets = (
        set(protected_nets) - set(policy.get("exclude_nets", ())) - {"GND", ""}
    )
    return _anchor_requests(
        board,
        request,
        policy,
        recovery,
        anchors,
        protected_nets,
        anchor_nets,
        "protected_anchor_hypothesis",
    )


class PoseLegality:
    """Whether detached copies of parts keep courtyards and the copper edge rule legal."""

    def __init__(self, board):
        """Cache every footprint's courtyard on ``board``."""
        self.board = board
        self.geometry = {
            part.m_Uuid.AsString(): _courtyard_geometry(part)
            for part in board.GetFootprints()
        }

    def __call__(self, selected, copies) -> bool:
        """Say whether moving ``selected`` parts to ``copies`` adds no overlap or edge breach.

        Existing overlaps stay baseline findings for native checks; only new
        ones make a pose illegal.
        """
        moved = {
            item[2].m_Uuid.AsString(): _courtyard_geometry(copy)
            for item, copy in strict_zip(selected, copies)
        }
        before = _courtyard_conflicts(
            {uid: self.geometry[uid] for uid in moved}, self.geometry
        )
        if _courtyard_conflicts(moved, self.geometry) - before:
            return False
        # A pad pushed past the copper edge rule cannot pass native DRC.
        return not (
            _beyond_edge_clearance(
                self.board, (pad for copy in copies for pad in copy.Pads())
            )
            and not _beyond_edge_clearance(
                self.board, (pad for item in selected for pad in item[2].Pads())
            )
        )


def placement_requests(
    board, request, policy, recovery, anchors, critical_nets, records=()
):
    """Sample independent single/package-pair poses; repair all affected nets."""
    if not recovery.placement_candidates or not board.GetFileName():
        return [], []
    target = request["nets"][0]["name"]
    options = _eligible(
        board,
        anchors,
        set(critical_nets) | set(policy.get("exclude_nets", [])),
        MoveScope.from_config(policy.get("placement")),
    )
    nearby = [item for item in options if item[0] <= mm(recovery.rip_up_radius_mm * 2)]
    guide = MoveGuide.diagnose(
        board,
        request,
        policy,
        recovery,
        anchors,
        critical_nets,
        options,
        nearby,
        records,
    )
    selections = guide.selections(options, nearby)
    box = board.GetBoardEdgesBoundingBox()
    legal = PoseLegality(board)
    tracks = {item.m_Uuid.AsString(): item for item in board.GetTracks()}
    streams = []
    for selected in selections:
        selected_ids = {item[2].m_Uuid.AsString() for item in selected}
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
        crossed = guide.crossings(frozenset(selected_ids))

        for pose in PoseSampler(board, guide, legal).poses(selected):
            candidate = deepcopy(request)
            candidate.pop("orders", None)
            candidate.update(
                recovery_stage=4,
                rip_up=removals,
                movable=[
                    {
                        "reference": ref,
                        "positions_mm": [[x, y]],
                        "rotations_deg": [angle],
                    }
                    for ref, x, y, angle in pose.moves
                ],
                repair_max_islands=recovery.repair_islands,
                checkpoint_offset=0,
                checkpoint_margin_mm=recovery.wide_margin_mm,
            )
            # Test actual KiCad courtyard polygons on detached footprint copies.
            # Existing overlaps remain baseline findings for native DRC; do not
            # spend routing time on a pose that introduces a new overlap.
            copies = PoseSampler.copies(selected, pose.moves)
            if not legal(selected, copies):
                continue
            candidate_removals = deepcopy(removals)
            candidate_dimensions = dict(dimensions)
            removed_ids = {
                item["uuid"] for items in candidate_removals.values() for item in items
            }
            try:
                # Copper the diagnosed route crosses is rerouted with the move.
                _include_obstructions(
                    board,
                    candidate_removals,
                    candidate_dimensions,
                    [
                        tracks[uid]
                        for uid in crossed
                        if uid in tracks and uid not in removed_ids
                    ],
                )
                removed_ids = {
                    item["uuid"]
                    for items in candidate_removals.values()
                    for item in items
                }
                obstructions = _placement_obstructions(
                    board,
                    copies,
                    selected_ids,
                    removed_ids,
                    policy,
                    set(critical_nets) | set(policy.get("exclude_nets", [])),
                    request,
                )
                _include_obstructions(
                    board, candidate_removals, candidate_dimensions, obstructions
                )
                exposed = _extend_exposed_copper(
                    board,
                    candidate_removals,
                    copies,
                    selected_ids,
                    max_items=recovery.placement_items,
                )
                _include_obstructions(
                    board, candidate_removals, candidate_dimensions, exposed
                )
            except UnsupportedPlacement:
                continue
            candidate_names = names | set(candidate_removals)
            if (
                len(candidate_names) > recovery.placement_nets
                or sum(map(len, candidate_removals.values())) > recovery.placement_items
            ):
                continue
            candidate["rip_up"] = candidate_removals
            # Several nets share the transaction: try more than one order.
            candidate["search"]["order_trials"] = max(
                2, candidate["search"].get("order_trials", 4)
            )
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
                        "selection": (
                            "path_obstruction_move"
                            if guide.names(selected)
                            else "placement_hypothesis"
                        ),
                        "pose": pose.kind,
                        "references": [item[1] for item in selected],
                        "nets": sorted(candidate_names),
                        "displacement_mm": pose.displacement_mm,
                        "rotation_deg": pose.rotation_deg,
                        "clears_corridor": guide.clears(copies),
                        "items": sum(map(len, candidate_removals.values())),
                    },
                )
            )
        choices.sort(key=lambda pair: not pair[1]["clears_corridor"])
        streams.append(choices)
    # Moves that open the diagnosed corridor come first; the rest share the
    # remaining slots so a small cap still samples every selection.
    guided = [
        pair
        for choices in streams
        for pair in choices
        if pair[1]["selection"] == "path_obstruction_move"
        and pair[1]["clears_corridor"]
    ]
    first = {id(pair) for pair in guided}
    interleaved = (
        pair
        for row in itertools.zip_longest(*streams)
        for pair in row
        if pair is not None and id(pair) not in first
    )
    chosen = list(
        itertools.islice(
            itertools.chain(guided, interleaved), recovery.placement_candidates
        )
    )
    return [pair[0] for pair in chosen], [pair[1] for pair in chosen]
