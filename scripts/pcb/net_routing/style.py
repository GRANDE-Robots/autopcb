"""style.py: independently audit saved native track geometry and copper editability.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import collections
import math
from dataclasses import dataclass, field, replace

import pcbnew

from core.pcb_io import copper_width
from pcb.net_routing.geometry import _line, on_segment, vertices
from pcb.net_routing.model import Site
from pcb.route_optimizer import GridRouter, Point, Segment


@dataclass(frozen=True)
class Joint:
    """A track joint that breaks a style rule.

    Inputs:
        net: net name.
        layer: copper layer name.
        at_nm: joint position as (x, y) in nm.
        item_ids: UUIDs of the tracks that meet there.
    """

    net: str
    layer: str
    at_nm: tuple[int, int]
    item_ids: tuple[str, ...]


@dataclass(frozen=True)
class Staircase:
    """Local defect identity, independent of track splits and chain extension."""

    net: str
    layer: str
    turns_nm: tuple[tuple[int, int], tuple[int, int]]
    directions: tuple[int, int, int]
    item_ids: tuple[str, ...] = field(compare=False)


@dataclass(frozen=True)
class StyleReport:
    """Routing-style findings for a board.

    Inputs:
        non_octilinear: UUIDs of tracks that are not horizontal, vertical or at 45 degrees.
        arcs: UUIDs of arc tracks.
        invalid_bends: two-track joints that bend by 90 degrees.
        redundant_joints: joints of two collinear tracks of equal width.
        staircases: staircase defects.
        locked_items: UUIDs of locked board items.
        grouped_items: UUIDs of copper items that belong to a group.
        ambiguous: messages for duplicate or overlapping copper the audit cannot classify.
    """

    non_octilinear: tuple[str, ...]
    arcs: tuple[str, ...]
    invalid_bends: tuple[Joint, ...]
    redundant_joints: tuple[Joint, ...]
    staircases: tuple[Staircase, ...]
    locked_items: tuple[str, ...]
    grouped_items: tuple[str, ...]
    ambiguous: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """True when every finding list is empty."""
        return not any(
            (
                self.non_octilinear,
                self.arcs,
                self.invalid_bends,
                self.redundant_joints,
                self.staircases,
                self.locked_items,
                self.grouped_items,
                self.ambiguous,
            )
        )


class SegmentIdentityNormalizer:
    """Maps the track UUIDs in a style report to stable identities."""

    def __call__(self, report: StyleReport, identities: dict[str, str]) -> StyleReport:
        """Keep unchanged segment defects stable when exact copper gets a new UUID."""

        def normalize_ids(values):
            """Replace each UUID by its stable identity and sort the result."""
            return tuple(sorted(identities.get(value, value) for value in values))

        def normalize_joints(joints):
            """Apply normalize_ids to the item UUIDs of each joint."""
            return tuple(
                replace(joint, item_ids=normalize_ids(joint.item_ids))
                for joint in joints
            )

        return replace(
            report,
            non_octilinear=normalize_ids(report.non_octilinear),
            invalid_bends=normalize_joints(report.invalid_bends),
            redundant_joints=normalize_joints(report.redundant_joints),
        )


normalize_segment_identities = SegmentIdentityNormalizer()


class StyleAuditor:
    """Audits the saved copper of a native board for routing-style findings."""

    def __call__(self, board) -> StyleReport:
        """Use integer nanometres; a native DRC and electrical checks are separate."""
        tracks = list(board.GetTracks())
        vias = [item for item in tracks if isinstance(item, pcbnew.PCB_VIA)]
        pads_by_net = collections.defaultdict(list)
        vias_by_net = collections.defaultdict(list)
        for pad in board.GetPads():
            pads_by_net[pad.GetNetCode()].append(pad)
        zone_nets = {
            zone.GetNetCode() for zone in board.Zones() if not zone.GetIsRuleArea()
        }
        connectivity = board.GetConnectivity()
        connectivity.Build(board)
        for via in vias:
            contacts = tuple(connectivity.GetConnectedTracks(via))
            single_layer = len({item.GetLayer() for item in contacts}) <= 1
            if (
                single_layer
                and via.GetNetCode() not in zone_nets
                and not connectivity.GetConnectedPads(via)
                and not any(isinstance(item, pcbnew.PCB_VIA) for item in contacts)
            ):
                continue
            vias_by_net[via.GetNetCode()].append(via)
        non_octilinear, arcs = [], []
        groups = collections.defaultdict(list)
        for track in tracks:
            uid = track.m_Uuid.AsString()
            if isinstance(track, pcbnew.PCB_VIA):
                continue
            if isinstance(track, pcbnew.PCB_ARC):
                arcs.append(uid)
                continue
            start, end = track.GetStart(), track.GetEnd()
            dx, dy = end.x - start.x, end.y - start.y
            if not (dx or dy) or (dx and dy and abs(dx) != abs(dy)):
                non_octilinear.append(uid)
                continue
            layer = board.GetLayerName(track.GetLayer())
            segment = Segment(
                Point(int(start.x), int(start.y)), Point(int(end.x), int(end.y)), layer
            )
            groups[
                (track.GetNetCode(), track.GetLayer(), track.GetNetname(), layer)
            ].append((track, segment))

        all_items = [
            *tracks,
            *board.GetFootprints(),
            *board.Zones(),
            *board.GetDrawings(),
            *board.Groups(),
        ]
        locked = tuple(
            sorted(item.m_Uuid.AsString() for item in all_items if item.IsLocked())
        )
        copper_ids = {item.m_Uuid.AsString() for item in tracks}
        grouped = tuple(
            sorted(
                {
                    item.m_Uuid.AsString()
                    for group in board.Groups()
                    for item in group.GetItems()
                    if item.m_Uuid.AsString() in copper_ids
                }
            )
        )
        invalid, redundant, stairs, ambiguous = [], [], set(), []
        for (net_code, layer_id, net_name, layer), entries in groups.items():
            segments = tuple(segment for _, segment in entries)
            signatures = set()
            line_intervals = collections.defaultdict(list)
            for _, segment in entries:
                signature = tuple(
                    sorted(
                        (
                            (segment.start.x, segment.start.y),
                            (segment.end.x, segment.end.y),
                        )
                    )
                )
                if signature in signatures:
                    ambiguous.append(
                        f"{net_name} on {layer}: duplicate overlapping tracks"
                    )
                signatures.add(signature)
                line, first, last = _line(segment)
                line_intervals[line].append((first, last))
            for intervals in line_intervals.values():
                end = None
                for first, last in sorted(intervals):
                    if end is not None and first < end:
                        ambiguous.append(
                            f"{net_name} on {layer}: overlapping collinear tracks"
                        )
                    end = max(end, last) if end is not None else last
            try:
                graph = vertices(segments)
            except ValueError as error:
                ambiguous.append(f"{net_name} on {layer}: {error}")
                continue
            endpoints = collections.defaultdict(list)
            for track, segment in entries:
                endpoints[segment.start].append((track, segment))
                endpoints[segment.end].append((track, segment))

            def anchored(point: Point) -> bool:
                """Whether a point lies on same-net pad copper or via copper on the current layer.

                Inputs:
                    point: board point in nm.
                """
                location = pcbnew.VECTOR2I(point.x, point.y)
                if any(
                    pad.IsOnLayer(layer_id) and pad.HitTest(location)
                    for pad in pads_by_net[net_code]
                ):
                    return True
                for via in vias_by_net[net_code]:
                    if via.IsOnLayer(layer_id):
                        center = via.GetPosition()
                        radius = copper_width(via, layer=layer_id) / 2
                        if math.hypot(center.x - point.x, center.y - point.y) <= radius:
                            return True
                return False

            for site, directions in graph.items():
                if len(directions) != 2 or anchored(site.point):
                    continue
                a, b = directions
                delta = (a - b) % 8
                touching = tuple(
                    sorted(
                        track.m_Uuid.AsString()
                        for track, seg in entries
                        if on_segment(site.point, seg)
                    )
                )
                joint = Joint(net_name, layer, (site.point.x, site.point.y), touching)
                if delta in {2, 6}:
                    invalid.append(joint)
                elif delta == 4 and len(endpoints[site.point]) == 2:
                    first, second = endpoints[site.point]
                    if copper_width(first[0]) == copper_width(second[0]):
                        redundant.append(joint)

            visited = set()
            for track, middle in entries:
                uid = track.m_Uuid.AsString()
                if uid in visited:
                    continue
                visited.add(uid)
                chain_ids = {uid}

                def extend(current, point: Point, *, backwards: bool):
                    """Follow a chain of tracks from a joint.

                    Inputs:
                        current: the track the walk leaves.
                        point: the joint where the walk continues.
                        backwards: orient the returned segments toward the start of the chain.
                    Outputs:
                        The further segments in walking order. The walk ends at an anchored
                        joint, a branch, or a track already visited; revisiting a track of the
                        same chain records an ambiguity.
                    """
                    chain = []
                    while (
                        len(graph.get(Site(point, layer), ())) == 2
                        and not anchored(point)
                        and len(endpoints[point]) == 2
                    ):
                        following = next(
                            (pair for pair in endpoints[point] if pair[0] != current),
                            None,
                        )
                        if following is None:
                            break
                        neighbor, segment = following
                        neighbor_id = neighbor.m_Uuid.AsString()
                        if neighbor_id in visited:
                            if neighbor_id in chain_ids:
                                ambiguous.append(
                                    f"{net_name} on {layer}: closed or overlapping track chain"
                                )
                            break
                        visited.add(neighbor_id)
                        chain_ids.add(neighbor_id)
                        other = segment.end if segment.start == point else segment.start
                        chain.append(
                            Segment(other, point, layer)
                            if backwards
                            else Segment(point, other, layer)
                        )
                        current, point = neighbor, other
                    return chain

                before = extend(track, middle.start, backwards=True)
                after = extend(track, middle.end, backwards=False)
                chain = [*reversed(before), middle, *after]
                for first, middle, last in GridRouter.staircase_windows(chain):
                    # The defect belongs to these three directional runs, not to
                    # every other track in the connected chain. A run may contain
                    # multiple collinear tracks after geometric normalization.
                    runs = (first, middle, last)
                    window_ids = {
                        item.m_Uuid.AsString()
                        for item, segment in entries
                        if item.m_Uuid.AsString() in chain_ids
                        and any(
                            on_segment(segment.start, run)
                            and on_segment(segment.end, run)
                            for run in runs
                        )
                    }
                    if not window_ids:
                        ambiguous.append(
                            f"{net_name} on {layer}: staircase has no identifiable tracks"
                        )
                        continue
                    turns = (
                        (middle.start.x, middle.start.y),
                        (middle.end.x, middle.end.y),
                    )
                    directions = tuple(
                        GridRouter._direction(s.start, s.end)
                        for s in (first, middle, last)
                    )
                    reverse = tuple((d + 4) % 8 for d in reversed(directions))
                    turns, directions = min(
                        (turns, directions), (tuple(reversed(turns)), reverse)
                    )
                    stairs.add(
                        Staircase(
                            net_name,
                            layer,
                            turns,
                            directions,
                            tuple(sorted(window_ids)),
                        )
                    )
        return StyleReport(
            tuple(sorted(non_octilinear)),
            tuple(sorted(arcs)),
            tuple(invalid),
            tuple(redundant),
            tuple(
                sorted(stairs, key=lambda s: (s.net, s.layer, s.turns_nm, s.directions))
            ),
            locked,
            grouped,
            tuple(sorted(ambiguous)),
        )


audit_style = StyleAuditor()
