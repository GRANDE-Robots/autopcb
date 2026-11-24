"""reroute.py: compile bounded, coordinated replacement of obstructing native copper.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from core.pcb_io import enabled_copper_layers

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
import math

from core.pcb_io import copper_width, net_classes
from pcb.net_routing.checkpoint import choose_checkpoint
from pcb.net_routing.model import Budget, Limits
from pcb.net_routing.native import discover
from pcb.net_routing.recovery import RecoveryPolicy
from pcb.net_routing.rework import ReworkWeights, capture_removals
from pcb.route_optimizer import (
    GridRouter,
    Obstacle,
    Point,
    Route,
    Segment,
    is_staircase_triplet,
    is_standard_dogleg,
    mm,
)


@dataclass(frozen=True)
class Obstruction:
    """A track or via near a blocked net that a repair may remove.

    Inputs:
        uuid: UUID of the item.
        net: net name.
        kind: segment or via.
        bounds: the item's bounding box as an obstacle.
        width: track width in nm; 0 for a via.
        size: via diameter in nm; 0 for a track.
        drill: via drill in nm; 0 for a track.
    """

    uuid: str
    net: str
    kind: str
    bounds: Obstacle
    width: int
    size: int
    drill: int

    def distance(self, point: Point) -> float:
        """Distance in nm from a point to the item's bounding box; 0 inside it."""
        low, high = self.bounds.lower, self.bounds.upper
        return math.hypot(
            max(low.x - point.x, 0, point.x - high.x),
            max(low.y - point.y, 0, point.y - high.y),
        )


class StaircaseSupport:
    """Extends a repair selection across contiguous staircase copper."""

    def __call__(
        self,
        segments: dict[str, tuple[str, Segment]],
        selected: frozenset[str],
        *,
        max_items: int,
        selectable: frozenset[str] | None = None,
        anchors: frozenset[tuple[str, str, Point]] = frozenset(),
    ) -> frozenset[str]:
        """Extend a repair across contiguous, eligible staircase copper.

        Only degree-two, non-anchor joints are followed within one net and layer.
        Only selectable, unlocked tracks are added; all candidates still undergo
        full connectivity, geometry and native DRC checks before acceptance.
        """
        adjacency = defaultdict(list)
        valid = {}
        for uid, (net, segment) in segments.items():
            try:
                GridRouter._direction(segment.start, segment.end)
            except ValueError:
                continue
            valid[uid] = (net, segment)
            for point in (segment.start, segment.end):
                adjacency[(net, segment.layer, point)].append(uid)
        windows = set()
        for middle_uid, (net, middle) in valid.items():
            if (net, middle.layer, middle.start) in anchors or (
                net,
                middle.layer,
                middle.end,
            ) in anchors:
                continue
            left = adjacency[(net, middle.layer, middle.start)]
            right = adjacency[(net, middle.layer, middle.end)]
            if len(left) != 2 or len(right) != 2:
                continue
            left_uid = next(uid for uid in left if uid != middle_uid)
            right_uid = next(uid for uid in right if uid != middle_uid)
            if left_uid == right_uid:
                continue
            first, last = valid[left_uid][1], valid[right_uid][1]
            first_point = first.end if first.start == middle.start else first.start
            last_point = last.end if last.start == middle.end else last.start
            directions = (
                GridRouter._direction(first_point, middle.start),
                GridRouter._direction(middle.start, middle.end),
                GridRouter._direction(middle.end, last_point),
            )
            before = [
                uid
                for uid in adjacency[(net, middle.layer, first_point)]
                if uid != left_uid
            ]
            after = [
                uid
                for uid in adjacency[(net, middle.layer, last_point)]
                if uid != right_uid
            ]
            dogleg = False
            if (
                len(before) == len(after) == 1
                and (net, middle.layer, first_point) not in anchors
                and (net, middle.layer, last_point) not in anchors
            ):
                before_segment, after_segment = valid[before[0]][1], valid[after[0]][1]
                before_point = (
                    before_segment.end
                    if before_segment.start == first_point
                    else before_segment.start
                )
                after_point = (
                    after_segment.end
                    if after_segment.start == last_point
                    else after_segment.start
                )
                dogleg = is_standard_dogleg(
                    (
                        GridRouter._direction(before_point, first_point),
                        *directions,
                        GridRouter._direction(last_point, after_point),
                    )
                )
            if is_staircase_triplet(*directions) and not dogleg:
                windows.add(frozenset((left_uid, middle_uid, right_uid)))
        result = set(selected)
        allowed = set(segments) if selectable is None else set(selectable)
        allowed.update(selected)
        changed = True
        while changed:
            changed = False
            for window in sorted(windows, key=lambda item: tuple(sorted(item))):
                if (
                    window & result
                    and not window <= result
                    and window <= allowed
                    and len(result | window) <= max_items
                ):
                    result.update(window)
                    changed = True
        return frozenset(result)


staircase_support = StaircaseSupport()


class RerouteCompiler:
    """Compiles coordinated copper-replacement and part-move requests for a blocked
    net."""

    def __call__(
        self,
        board,
        request: dict,
        policy: dict,
        critical_nets: tuple[str, ...],
        *,
        reference_nets: tuple[str, ...] | None = None,
    ) -> dict:
        """Keep the target and all repairs in one independently checked transaction.

        Only eligible copper can be removed. Protected nets and USB/Ethernet
        reference corridors are separate, independent constraints. A candidate
        remains a geometric preview until the complete checks accept it.
        """
        import pcbnew

        # Existing callers that supply only one scope retain the conservative
        # behavior. New callers distinguish all non-removable nets from the
        # USB/Ethernet nets that actually need adjacent reference reservations.
        reference_nets = critical_nets if reference_nets is None else reference_nets
        protected_nets = set(critical_nets) | set(reference_nets)
        recovery = RecoveryPolicy.from_config(policy.get("recovery"))
        if len(request["nets"]) != 1 or request.get("rip_up") or request.get("movable"):
            raise ValueError("Automatic copper replacement needs one unresolved target")
        if not recovery.rip_up_candidates and not recovery.placement_candidates:
            return {
                "requests": [],
                "candidates": [],
                "blockers": ["Copper replacement is disabled"],
            }
        target = request["nets"][0]["name"]
        settings = {**request["shared"], **request["nets"][0]}
        physical = enabled_copper_layers(board)
        layers = tuple(settings.get("layers", physical))
        net = discover(board, target, layers, Budget(Limits(seconds=15))).net
        if len(net.islands) < 2:
            return {
                "requests": [],
                "candidates": [],
                "blockers": ["Target is already connected"],
            }
        choice = choose_checkpoint(net, maximum=2, offset=0)
        first, last = (site.point for site in choice.anchors)
        width, clearance = (
            mm(settings.get("width_mm", 0.2)),
            mm(settings.get("clearance_mm", 0.2)),
        )
        pads = defaultdict(list)
        for pad in board.GetPads():
            pads[pad.GetNetname()].append(pad)
        classes = net_classes(board)
        excluded = set(policy.get("exclude_nets", [])) | protected_nets | {""}
        eligible, blockers, topology_segments, via_anchors = [], [], {}, set()
        # Keep a separate whole-target inventory before distance filtering. A local
        # obstruction window can omit old target copper whose staircases prevent a
        # coherent replacement; only a complete, eligible set may be ripped up.
        target_records = []
        target_supported = True
        radius = mm(recovery.rip_up_radius_mm)
        for item in board.GetTracks():
            name = item.GetNetname()
            uid = item.m_Uuid.AsString()
            is_via = isinstance(item, pcbnew.PCB_VIA)
            if is_via:
                position = item.GetPosition()
                via_anchors.update(
                    (name, layer, Point(position.x, position.y))
                    for layer in layers
                    if item.IsOnLayer(board.GetLayerID(layer))
                )
            elif not isinstance(item, pcbnew.PCB_ARC):
                start, end = item.GetStart(), item.GetEnd()
                topology_segments[uid] = (
                    name,
                    Segment(
                        Point(start.x, start.y),
                        Point(end.x, end.y),
                        board.GetLayerName(item.GetLayer()),
                    ),
                )
            if name in excluded or name.startswith("unconnected-"):
                continue
            active = tuple(
                layer for layer in layers if item.IsOnLayer(board.GetLayerID(layer))
            )
            if not active:
                if name == target:
                    target_supported = False
                continue
            box = item.GetBoundingBox()
            obstacle = Obstacle(
                Point(box.GetX(), box.GetY()),
                Point(box.GetRight(), box.GetBottom()),
                active,
            )
            record = Obstruction(
                uid,
                name,
                "via" if is_via else "segment",
                obstacle,
                0 if is_via else copper_width(item, layer=item.GetLayer()),
                max(
                    copper_width(item, layer=board.GetLayerID(layer))
                    for layer in physical
                )
                if is_via
                else 0,
                item.GetDrillValue() if is_via else 0,
            )
            nearby = not (
                min(record.distance(first), record.distance(last))
                > mm(recovery.wide_margin_mm)
                and not GridRouter._intersects_box(
                    first, last, obstacle, width // 2 + clearance
                )
            )
            related = pads[name]
            reason = None
            if item.IsLocked() or item.GetParentGroup() is not None:
                reason = "locked or grouped"
            elif not related:
                reason = "no schematic pad"
            elif any(p.GetNetClassName() not in classes for p in related):
                reason = "effective net class is not available to the native API"
            elif isinstance(item, pcbnew.PCB_ARC):
                reason = "arc replacement is outside automatic local recovery"
            elif is_via and item.GetViaType() != pcbnew.VIATYPE_THROUGH:
                reason = "automatic recovery requires through vias"
            if name == target:
                if reason:
                    target_supported = False
                else:
                    target_records.append(record)
            if not nearby:
                continue
            if reason:
                blockers.append(f"{name} {uid}: {reason}")
            else:
                eligible.append(record)

        target_group = (
            tuple(sorted(target_records, key=lambda item: item.uuid))
            if (
                target_supported
                and 0 < len(target_records) <= recovery.whole_net_rip_up_items
            )
            else ()
        )
        eligible.sort(
            key=lambda item: (min(item.distance(first), item.distance(last)), item.uuid)
        )
        # Reserve a coordinated hotspot before singleton alternatives. Otherwise a
        # small candidate budget would starve the useful multi-item repair.
        clusters = []
        for endpoint in choice.anchors:
            point = endpoint.point
            nearby = sorted(
                (
                    item
                    for item in eligible
                    if endpoint.layer in item.bounds.layers
                    and item.distance(point) <= radius
                ),
                key=lambda item: (item.distance(point), item.uuid),
            )
            cluster, names = [], set()
            for item in nearby:
                if len(cluster) >= recovery.rip_up_items:
                    break
                if item.net not in names and len(names) >= recovery.rip_up_nets:
                    continue
                cluster.append(item)
                names.add(item.net)
            if cluster:
                clusters.append(tuple(cluster))
        weights = ReworkWeights.from_config(request.get("rework"))
        witnesses = []
        if (
            recovery.rip_up_candidates
            and recovery.obstruction_trials
            and eligible
            and board.GetFileName()
        ):
            from pcb.net_routing.obstructions import diagnose
            from pcb.routability import GridModel

            # Removing tracks cannot help a net that pads and keepouts cut off.
            if GridModel(board, policy=policy, critical_nets=reference_nets).cut_off(
                target
            ):
                blockers.append(
                    "Parts and keepouts leave the net no way through; "
                    "only moving a part can connect it"
                )
            else:
                witnesses, diagnosis_notes = diagnose(
                    board,
                    choice.anchors,
                    eligible,
                    {**settings, "name": target},
                    recovery,
                    reference_nets,
                    weights,
                )
                blockers.extend(diagnosis_notes)
        by_id = {item.uuid: item for item in (*eligible, *target_group)}
        eligible_names = {item.net for item in by_id.values()}
        topology_segments = {
            uid: value
            for uid, value in topology_segments.items()
            if value[0] in eligible_names
        }
        anchors = set(via_anchors)
        for name, segment in topology_segments.values():
            layer_id = board.GetLayerID(segment.layer)
            for point in (segment.start, segment.end):
                key = (name, segment.layer, point)
                if key not in anchors and any(
                    pad.IsOnLayer(layer_id)
                    and pad.HitTest(pcbnew.VECTOR2I(point.x, point.y))
                    for pad in pads[name]
                ):
                    anchors.add(key)
        diagnosed = [
            tuple(by_id[uid] for uid in sorted(witness.identifiers))
            for witness in witnesses
            if len(witness.identifiers) <= recovery.rip_up_items
            and len({by_id[uid].net for uid in witness.identifiers})
            <= recovery.rip_up_nets
        ]
        # Reserve a distinct whole-target alternative alongside the first
        # diagnosed obstruction; neither alternative bypasses downstream checks.
        base_patterns = [
            *diagnosed[:1],
            *((target_group,) if target_group else ()),
            *diagnosed[1:],
            *sorted(clusters, key=lambda group: -len(group)),
            *((item,) for item in eligible),
        ]
        target_ids = frozenset(item.uuid for item in target_group)
        witnesses_by_id = {w.identifiers for w in witnesses}
        patterns = []
        for selected in base_patterns:
            identifiers = frozenset(item.uuid for item in selected)
            if settings.get("forbid_staircase", True):
                expanded = staircase_support(
                    topology_segments,
                    identifiers,
                    max_items=recovery.rip_up_items,
                    selectable=frozenset(by_id),
                    anchors=frozenset(anchors),
                )
                if expanded != identifiers:
                    patterns.append(
                        (
                            tuple(by_id[uid] for uid in sorted(expanded)),
                            "staircase_context",
                        )
                    )
            selection = (
                "whole_target"
                if target_ids and identifiers == target_ids
                else "path_obstructions"
                if identifiers in witnesses_by_id
                else "local_hypothesis"
            )
            patterns.append((selected, selection))
        requests, descriptions, seen = [], [], set()
        empty = Route((), (), (), 0.0, 0, 0.0, 0)
        for selected, selection in patterns if recovery.rip_up_candidates else []:
            identifiers = frozenset(item.uuid for item in selected)
            if identifiers in seen:
                continue
            seen.add(identifiers)
            grouped = defaultdict(list)
            for item in selected:
                grouped[item.net].append(item)
            candidate = deepcopy(request)
            candidate.pop("orders", None)
            candidate.update(
                recovery_stage=4,
                max_islands=policy.get("max_pins", 3),
                repair_max_islands=recovery.repair_islands,
                checkpoint_offset=0,
                rip_up={},
            )
            removed = capture_removals(board, set(identifiers))
            cost = 0.0
            for name, items in sorted(grouped.items()):
                if name == target:
                    # Rework may include copper already on the target itself.
                    # Keep the effective request and dimensions; do not add a
                    # duplicate net to the transaction or weaken shared settings.
                    neighbor = deepcopy(settings)
                else:
                    neighbor = {
                        **deepcopy(policy.get("route", {})),
                        **deepcopy(policy.get("net_overrides", {}).get(name, {})),
                    }
                net_limits = [classes[p.GetNetClassName()] for p in pads[name]]
                own_width = max(
                    mm(neighbor.get("width_mm", 0.2)),
                    *(c.GetTrackWidth() for c in net_limits),
                    *(i.width for i in items),
                )
                own_clearance = max(
                    mm(neighbor.get("clearance_mm", 0.2)),
                    *(c.GetClearance() for c in net_limits),
                )
                via = neighbor.get("via", {})
                size = max(mm(via.get("size_mm", 0.6)), *(i.size for i in items))
                drill = max(mm(via.get("drill_mm", 0.3)), *(i.drill for i in items))
                neighbor.update(
                    name=name,
                    width_mm=own_width / 1e6,
                    clearance_mm=own_clearance / 1e6,
                    via={"size_mm": size / 1e6, "drill_mm": drill / 1e6},
                )
                # Restore a local repair search instead of inheriting the coarse
                # full-board window from the preceding detour stage. Neighbors
                # share recovery search resources, never the target's dimensions.
                neighbor.update(
                    step_mm=min(
                        neighbor.get("step_mm", 0.1), settings.get("step_mm", 0.1)
                    ),
                    max_cells=max(
                        neighbor.get("max_cells", 250000), recovery.max_cells
                    ),
                    max_expansions=max(
                        neighbor.get("max_expansions", 200000), recovery.max_expansions
                    ),
                    topology_heuristic=True,
                    heuristic_weight=max(
                        neighbor.get("heuristic_weight", 1.0), recovery.heuristic_weight
                    ),
                )
                neighbor.pop("bounds_mm", None)
                if name == target:
                    candidate["nets"][0] = neighbor
                else:
                    candidate["nets"].append(neighbor)
                candidate["rip_up"][name] = [
                    {"kind": item.kind, "uuid": item.uuid} for item in items
                ]
                cost += (
                    removed[name]
                    .score(
                        empty, width=own_width, size=size, drill=drill, weights=weights
                    )
                    .cost
                )
            bounds = board.GetBoardEdgesBoundingBox()
            candidate["shared"]["bounds_mm"] = [
                bounds.GetX() / 1e6,
                bounds.GetY() / 1e6,
                bounds.GetRight() / 1e6,
                bounds.GetBottom() / 1e6,
            ]
            candidate["checkpoint_margin_mm"] = recovery.wide_margin_mm
            candidate["search"]["order_trials"] = max(
                2, candidate["search"].get("order_trials", 4)
            )
            requests.append(candidate)
            descriptions.append(
                {
                    "nets": sorted(grouped),
                    "uuids": sorted(identifiers),
                    "items": len(identifiers),
                    "estimated_rework_cost": cost,
                    "selection": selection,
                }
            )
            if len(requests) >= recovery.rip_up_candidates:
                break
        from pcb.net_routing.relocation import AnchorRequests, placement_requests

        if policy.get("_ground_anchor_candidates"):
            anchored, anchored_descriptions = AnchorRequests.ground(
                board,
                request,
                policy,
                recovery,
                choice.anchors,
                tuple(sorted(protected_nets)),
            )
            requests.extend(anchored)
            descriptions.extend(anchored_descriptions)
        if policy.get("_protected_anchor_candidates"):
            anchored, anchored_descriptions = AnchorRequests.protected(
                board,
                request,
                policy,
                recovery,
                choice.anchors,
                tuple(sorted(protected_nets)),
            )
            requests.extend(anchored)
            descriptions.extend(anchored_descriptions)
        moved, move_descriptions = placement_requests(
            board,
            request,
            policy,
            recovery,
            choice.anchors,
            tuple(sorted(protected_nets)),
            eligible,
        )
        requests.extend(moved)
        descriptions.extend(move_descriptions)
        return {
            "requests": requests,
            "candidates": descriptions,
            "blockers": sorted(set(blockers))[:16],
        }


compile_reroutes = RerouteCompiler()
