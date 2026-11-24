"""route_junctions.py: materialize explicit junctions in proposed, source-bound PCB copper.

This prepares ordinary campaign edits only. Native DRC, connectivity and
electrical checks remain mandatory before a candidate can be applied.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import math
from dataclasses import replace
from itertools import combinations

from core.kicad_sexpr import parse
from core.pcb_io import copper_width
from pcb.net_routing.geometry import intersection, on_segment
from pcb.route_cli import campaign_changes
from pcb.route_optimizer import Point, Route, Segment
from core.compat import strict_zip


class Contacts:
    """Finds where two track segments touch or cross."""

    def __call__(self, first: Segment, second: Segment) -> set[Point]:
        """Points that two segments share.

        Inputs:
            first, second: Track segments.
        Outputs:
            The endpoints of either segment that lie on both, plus the crossing point
            when the segments cross. Empty for segments on different layers or with
            disjoint bounding boxes.
        """
        if first.layer != second.layer:
            return set()
        if (
            max(first.start.x, first.end.x) < min(second.start.x, second.end.x)
            or max(second.start.x, second.end.x) < min(first.start.x, first.end.x)
            or max(first.start.y, first.end.y) < min(second.start.y, second.end.y)
            or max(second.start.y, second.end.y) < min(first.start.y, first.end.y)
        ):
            return set()
        points = {
            point
            for point in (first.start, first.end, second.start, second.end)
            if on_segment(point, first) and on_segment(point, second)
        }
        crossing = intersection(first, second)
        if crossing is not None:
            points.add(crossing)
        return points


_contacts = Contacts()


class JunctionChanges:
    """Splits a route and the existing same-net tracks it meets at every contact and
    builds the campaign changes.
    """

    def __call__(
        self,
        board,
        original: bytes,
        route: Route,
        net: str,
        width: int,
        via_size: int,
        via_drill: int,
        *,
        removed_copper_ids: frozenset[str] = frozenset(),
        segment_dimensions=None,
        prune_dangling_tails: bool = True,
        merge_collinear: bool = True,
        pruned_tails: list[tuple[str, Segment]] | None = None,
        protected_sites: tuple[tuple[Point, str], ...] = (),
    ) -> tuple[list[dict], list[dict[str, str]]]:
        """Split same-net straight tracks at their exact new contacts, on a copy.

        Existing tracks may be replaced only if their original expression is a
        plain unlocked, ungrouped segment. Their width, layer, net and endpoints
        are preserved. Unrepresentable fractional-nanometre intersections are
        rejected rather than rounded. The caller must bind removals to its route
        contracts and run native validation. Planned removals are ignored here,
        without calling BOARD.Remove: that can invalidate KiCad 10 SWIG wrappers
        when a detached via is released. Actual removal stays in the text campaign.
        """
        import pcbnew

        if not isinstance(prune_dangling_tails, bool):
            raise ValueError("prune_dangling_tails must be true or false")
        if not isinstance(merge_collinear, bool):
            raise ValueError("merge_collinear must be true or false")
        if pruned_tails is not None and not isinstance(pruned_tails, list):
            raise ValueError("pruned_tails must be a list")
        if not isinstance(protected_sites, tuple) or any(
            not isinstance(site, tuple)
            or len(site) != 2
            or not isinstance(site[0], Point)
            or not isinstance(site[1], str)
            or not site[1]
            for site in protected_sites
        ):
            raise ValueError("Protected sites must be point and layer pairs")
        segments = route.segments
        cuts = [{s.start, s.end} for s in segments]
        for i, j in combinations(range(len(segments)), 2):
            points = _contacts(segments[i], segments[j])
            cuts[i].update(points)
            cuts[j].update(points)

        old = []
        layers = {segment.layer for segment in segments}
        via_sites = {(v.at, layer) for v in route.vias for layer in layers}
        for item in board.GetTracks():
            if item.m_Uuid.AsString() in removed_copper_ids:
                continue
            if item.GetNetname() != net:
                continue
            if isinstance(item, pcbnew.PCB_VIA):
                at = item.GetPosition()
                position = Point(int(at.x), int(at.y))
                via_sites.update(
                    (position, layer)
                    for layer in layers
                    if item.IsOnLayer(board.GetLayerID(layer))
                )
            elif not isinstance(item, pcbnew.PCB_ARC):
                start, end = item.GetStart(), item.GetEnd()
                if start.x == end.x and start.y == end.y:
                    continue
                segment = Segment(
                    Point(int(start.x), int(start.y)),
                    Point(int(end.x), int(end.y)),
                    board.GetLayerName(item.GetLayer()),
                )
                old.append((item, segment))

        replacements = []
        for item, segment in old:
            points = {segment.start, segment.end}
            for index, proposed in enumerate(segments):
                shared = _contacts(segment, proposed)
                points.update(shared)
                cuts[index].update(shared)
            pieces = self._pieces(segment, points)
            if len(pieces) > 1:
                if item.IsLocked() or item.GetParentGroup() is not None:
                    raise ValueError("Cannot split locked or grouped existing copper")
                replacements.append((item, pieces))

        for index, segment in enumerate(segments):
            cuts[index].update(
                p
                for p, layer in via_sites
                if layer == segment.layer and on_segment(p, segment)
            )
        new_segments = tuple(
            piece
            for segment, points in strict_zip(segments, cuts)
            for piece in self._pieces(segment, points)
        )
        if merge_collinear:
            new_segments, merged = self._merge_collinear_extensions(
                board,
                old,
                new_segments,
                {item.m_Uuid.AsString() for item, _ in replacements},
                removed_copper_ids,
                net,
                width,
                via_size,
                (*route.vias, *route.reused_vias),
                segment_dimensions,
            )
            replacements.extend((item, ()) for item in merged)
        changes = campaign_changes(
            replace(route, segments=new_segments),
            net,
            width,
            via_size,
            via_drill,
            segment_dimensions=segment_dimensions,
        )
        removals = []
        if replacements:
            if prune_dangling_tails:
                board.GetConnectivity().Build(board)
            root = parse(original.decode("utf-8-sig"))
            forms = {
                ids[0].atoms()[1].value: form
                for form in root.children("segment")
                if len(ids := form.children("uuid")) == 1 and len(ids[0].atoms()) == 2
            }
            for item, pieces in replacements:
                identifier = item.m_Uuid.AsString()
                form = forms.get(identifier)
                if (
                    form is None
                    or len(form.atoms()) != 1
                    or sorted(c.name for c in form.children())
                    != ["end", "layer", "net", "start", "uuid", "width"]
                ):
                    raise ValueError(
                        "Cannot safely split a nonstandard existing segment"
                    )
                removal = {"kind": "segment", "uuid": identifier}
                removals.append(removal)
                if prune_dangling_tails:
                    retained = self._without_unattached_tail(
                        board, item, pieces, segments, route.vias, protected_sites
                    )
                    if pruned_tails is not None:
                        pruned_tails.extend(
                            (identifier, piece)
                            for piece in pieces
                            if piece not in retained
                        )
                    pieces = retained
                replacement = replace(route, segments=pieces, vias=(), reused_vias=())
                changes.extend(
                    campaign_changes(
                        replacement, net, copper_width(item), via_size, via_drill
                    )
                )
        changes[:0] = [
            {"document": "pcb", "target": removal, "op": "remove", "path": []}
            for removal in removals
        ]
        return changes, removals

    @staticmethod
    def _pieces(segment: Segment, points: set[Point]) -> tuple[Segment, ...]:
        """Split a segment at the given points, ordered by coordinate, without zero-
        length pieces.
        """
        ordered = sorted(
            points | {segment.start, segment.end}, key=lambda p: (p.x, p.y)
        )
        return tuple(
            Segment(a, b, segment.layer) for a, b in zip(ordered, ordered[1:]) if a != b
        )

    @staticmethod
    def _point_in_trace(point: Point, segment: Segment, width: int) -> bool:
        """Test the complete round-ended trace width using integer geometry."""
        dx, dy = segment.end.x - segment.start.x, segment.end.y - segment.start.y
        px, py = point.x - segment.start.x, point.y - segment.start.y
        length_squared = dx * dx + dy * dy
        radius_squared = ((width + 1) // 2) ** 2
        projection = px * dx + py * dy
        if projection <= 0:
            return px * px + py * py <= radius_squared
        if projection >= length_squared:
            ex, ey = point.x - segment.end.x, point.y - segment.end.y
            return ex * ex + ey * ey <= radius_squared
        return (px * dy - py * dx) ** 2 <= radius_squared * length_squared

    def _without_unattached_tail(
        self, board, item, pieces, proposed, vias, protected_sites=()
    ):
        """Drop a confirmed dangling end only when no other copper can use it."""
        import pcbnew

        position = pcbnew.VECTOR2I()
        connectivity = board.GetConnectivity()
        if not hasattr(connectivity, "TestTrackEndpointDangling"):
            return pieces
        if not connectivity.TestTrackEndpointDangling(item, True, position):
            return pieces
        marker = Point(int(position.x), int(position.y))
        matching = [piece for piece in pieces if marker in (piece.start, piece.end)]
        if len(matching) != 1:
            return pieces
        tail = matching[0]
        join = tail.end if tail.start == marker else tail.start
        if any(
            layer == tail.layer
            and self._point_in_trace(site, tail, int(copper_width(item)))
            for site, layer in protected_sites
        ):
            return pieces
        # The proposed copper must not need the part we intend to remove.
        if any(_contacts(tail, segment) - {join} for segment in proposed):
            return pieces
        if any(via.at != join and on_segment(via.at, tail) for via in vias):
            return pieces

        # Bounding boxes deliberately overselect: pads, traces, vias and zones on
        # the same net anywhere along this tail make removal ineligible.
        radius = (copper_width(item) + 1) // 2
        left, right = (
            min(tail.start.x, tail.end.x) - radius,
            max(tail.start.x, tail.end.x) + radius,
        )
        top, bottom = (
            min(tail.start.y, tail.end.y) - radius,
            max(tail.start.y, tail.end.y) + radius,
        )
        identifier = item.m_Uuid.AsString()
        layer = board.GetLayerID(tail.layer)
        for other in (*board.GetPads(), *board.GetTracks(), *board.Zones()):
            if (
                other.m_Uuid.AsString() == identifier
                or other.GetNetname() != item.GetNetname()
                or not other.IsOnLayer(layer)
            ):
                continue
            bounds = other.GetBoundingBox()
            if (
                bounds.GetX() <= right
                and bounds.GetRight() >= left
                and bounds.GetY() <= bottom
                and bounds.GetBottom() >= top
            ):
                return pieces
        return tuple(piece for piece in pieces if piece != tail)

    def _merge_collinear_extensions(
        self,
        board,
        old,
        segments,
        split_ids,
        removed_ids,
        net,
        width,
        via_size,
        vias,
        segment_dimensions,
    ):
        """Merge exact, unanchored degree-two joins without changing copper shape."""
        import pcbnew

        result = list(segments)
        consumed = set()
        merged_items = []

        def uniform(segment):
            """The one width of a segment: the route width, or the width of its single
            piece from segment_dimensions; None when it needs several widths or they are
            unavailable.
            """
            if segment_dimensions is None:
                return width
            try:
                pieces = tuple(segment_dimensions(segment))
            except ValueError:
                return None
            return (
                pieces[0][1] if len(pieces) == 1 and pieces[0][0] == segment else None
            )

        def anchored(point, layer):
            """Whether a pad, a via or a same-net arc holds the point on the layer, so
            no track may be merged across it.
            """
            location = pcbnew.VECTOR2I(point.x, point.y)
            layer_id = board.GetLayerID(layer)
            if any(
                pad.IsOnLayer(layer_id) and pad.HitTest(location)
                for pad in board.GetPads()
            ):
                return True
            if any(
                math.hypot(via.at.x - point.x, via.at.y - point.y) <= via_size / 2
                for via in vias
            ):
                return True
            for item in board.GetTracks():
                if (
                    item.m_Uuid.AsString() in removed_ids
                    or item.GetNetname() != net
                    or not item.IsOnLayer(layer_id)
                ):
                    continue
                if isinstance(item, pcbnew.PCB_VIA):
                    center = item.GetPosition()
                    if (
                        math.hypot(center.x - point.x, center.y - point.y)
                        <= copper_width(item, layer=layer_id) / 2
                    ):
                        return True
                elif isinstance(item, pcbnew.PCB_ARC) and item.HitTest(location):
                    return True
            return False

        for index, current in enumerate(result):
            while True:
                replacement = None
                current_width = uniform(current)
                if current_width is None:
                    break
                for joint in (current.start, current.end):
                    if anchored(joint, current.layer):
                        continue
                    if any(
                        i != index
                        and other.layer == current.layer
                        and on_segment(joint, other)
                        for i, other in enumerate(result)
                    ):
                        continue
                    touching = [
                        (item, segment)
                        for item, segment in old
                        if item.m_Uuid.AsString() not in consumed
                        and segment.layer == current.layer
                        and on_segment(joint, segment)
                    ]
                    if len(touching) != 1:
                        continue
                    item, previous = touching[0]
                    if (
                        item.m_Uuid.AsString() in split_ids
                        or joint not in (previous.start, previous.end)
                        or item.IsLocked()
                        or item.GetParentGroup() is not None
                        or copper_width(item) != current_width
                    ):
                        continue
                    first = previous.end if previous.start == joint else previous.start
                    last = current.end if current.start == joint else current.start
                    ax, ay = first.x - joint.x, first.y - joint.y
                    bx, by = last.x - joint.x, last.y - joint.y
                    if ax * by != ay * bx or ax * bx + ay * by >= 0:
                        continue
                    merged = Segment(first, last, current.layer)
                    if uniform(merged) != current_width:
                        continue
                    replacement = item, merged
                    break
                if replacement is None:
                    break
                item, current = replacement
                consumed.add(item.m_Uuid.AsString())
                merged_items.append(item)
                result[index] = current
        return tuple(result), merged_items


junction_changes = JunctionChanges()
