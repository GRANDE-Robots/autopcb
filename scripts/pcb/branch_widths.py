"""branch_widths.py: Compile terminal-aware width allowances without editing schematic or copper.

Allowances describe normal-current design targets, not fault qualification. Unknown
terminals, meshed current paths and zones retain the existing rules. The managed block
of the project's rules file is rewritten only after native DRC on a private copy shows
no unrelated finding change; --apply then publishes it.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from core.project import native_path

import argparse
from collections import Counter, defaultdict
from collections.abc import Callable
from itertools import chain, combinations, groupby
import json
import math
import os
from pathlib import Path
import tempfile

from core.compat import file_sha256
from core.paths import asset_root
from core.toolchain import kicad_cli
from core.pcb_io import (
    BoardSaveGuard,
    quiet_duplicate_image_handlers,
    typed_copper,
)
from pcb.net_routing.native import Snapshot
from pcb.route_validation import _drc
from core.compat import strict_zip


BEGIN = "# BEGIN AUTOPCB TERMINAL WIDTHS"
END = "# END AUTOPCB TERMINAL WIDTHS"
TAP_MATCH_NM = 2


class BranchGraph:
    """Current-path reasoning on the connectivity graph of one net."""

    @staticmethod
    def component(
        graph: dict[str, set[str]], seed: str, omitted: str | None = None
    ) -> set[str]:
        """Collect the nodes reachable from a seed, optionally without one node.

        Inputs:
            graph: Mapping from node to its neighbor nodes.
            seed: Node to start from.
            omitted: Node to treat as removed, so it is neither entered nor crossed.
        Outputs:
            The set of reachable nodes; empty when the seed is the omitted node.
        """
        seen, pending = set(), [seed]
        while pending:
            node = pending.pop()
            if node == omitted or node in seen:
                continue
            seen.add(node)
            pending.extend(graph[node] - seen - {omitted})
        return seen

    @staticmethod
    def bounded_leaves(
        graph: dict[str, set[str]],
        pads: set[str],
        loads: dict[str, float],
        tracks: set[str],
        *,
        contact_ports: dict[tuple[str, str], tuple[int, int]] | None = None,
        common_landing: Callable[[str, set[str]], bool] | None = None,
        declared_loads: set[str] | None = None,
        include_downstream: bool = False,
    ) -> dict[str, float]:
        """Find cut tracks whose downstream terminals all have explicit load bounds.

        A unique unknown-terminal component must remain upstream. This deliberately
        rejects meshes with a second trunk attachment and floating load islands. With a
        proven trunk cut, copper entirely inside a local branch can share its current
        bound even when that branch contains a mesh.

        Inputs:
            graph: Mapping from node to its neighbor nodes.
            pads: Pad nodes.
            loads: Mapping from pad node to its design current bound, in A.
            tracks: Track nodes that may be cut.
            contact_ports: Mapping from (track, neighbor) to the exact contact point, in
                nm.
            common_landing: Callable that proves the upstream contacts of a track share
                one landing.
            declared_loads: Pad nodes that have a declared load; defaults to those in
                loads.
            include_downstream: Also give every track inside the branch the bound.
        Outputs:
            Mapping from proven track node to the current bound downstream of it, in A.
            Raises ValueError for a graph over 2048 nodes, loads that are not declared
            pads of the graph, or a load that is negative or not finite.
        """
        declared_loads = set(loads) if declared_loads is None else declared_loads
        if len(graph) > 2048 or not set(loads) <= declared_loads <= pads <= set(graph):
            raise ValueError("Invalid or oversized branch connectivity graph")
        if any(not math.isfinite(v) or v < 0 for v in loads.values()):
            raise ValueError("Loads need finite nonnegative design currents")
        result = {}
        for track in sorted(tracks):
            connected = BranchGraph.component(graph, track)
            if not (connected & pads) - declared_loads:
                continue
            pending = connected - {track}
            groups = []
            while pending:
                group = BranchGraph.component(graph, min(pending), track)
                pending.difference_update(group)
                groups.append(group)
            roots = [group for group in groups if (group & pads) - set(loads)]
            if (
                len(groups) < 2
                or len(roots) != 1
                or not (roots[0] & pads) - declared_loads
            ):
                continue
            branches = [
                group for group in groups if group is not roots[0] and group & pads
            ]
            leaves = set().union(*(group & pads for group in branches))
            if not leaves or not leaves <= loads.keys():
                continue
            # A track that bridges two upstream ports can carry trunk current.
            # Accept an ambiguous junction only when its contacts share one landing.
            upstream = graph[track] & roots[0]
            if contact_ports is not None:
                ports = {contact_ports.get((track, other)) for other in upstream}
                if (None in ports or len(ports) != 1) and not (
                    common_landing and common_landing(track, upstream)
                ):
                    continue
            elif len(upstream) != 1:
                continue
            current = sum(loads[pad] for pad in leaves)
            proved = {track}
            if include_downstream:
                for branch in branches:
                    proved.update(branch & tracks)
            for uid in proved:
                result[uid] = min(current, result.get(uid, current))
        return result


class CopperContacts:
    """Native-geometry contacts between the copper items of one net."""

    @staticmethod
    def xy(value) -> tuple[int, int]:
        """Coordinates of a KiCad point.

        Inputs:
            value: A KiCad point.
        Outputs:
            The (x, y) pair, in nm.
        """
        return value.x, value.y

    @staticmethod
    def on_segment(p: tuple, first: tuple, last: tuple) -> bool:
        """Say whether a point lies on a closed straight segment.

        Inputs:
            p: The (x, y) point, in nm.
            first, last: The (x, y) segment ends, in nm.
        Outputs:
            True when the point is collinear with the ends and between them.
        """
        return (p[0] - first[0]) * (last[1] - first[1]) == (p[1] - first[1]) * (
            last[0] - first[0]
        ) and all(
            min(first[i], last[i]) <= p[i] <= max(first[i], last[i]) for i in (0, 1)
        )

    @staticmethod
    def contact_port(track, other) -> tuple[int, int] | None:
        """Prove an exact endpoint junction, rejecting overlap and interior contacts.

        Inputs:
            track: A straight track.
            other: A via, a pad of convex shape, or another straight track on the same
                layer.
        Outputs:
            The (x, y) point, in nm, where exactly one end of the track meets the other
            item; None when there is no such unique endpoint contact, when the other
            item is zero-length or an arc, or when collinear copper overlaps the track.
        """
        import pcbnew

        track, other = typed_copper(track), typed_copper(other)
        if not isinstance(track, pcbnew.PCB_TRACK) or isinstance(
            track, (pcbnew.PCB_VIA, pcbnew.PCB_ARC)
        ):
            return None

        point = CopperContacts.xy
        a, b = point(track.GetStart()), point(track.GetEnd())
        if a == b or not other.IsOnLayer(track.GetLayer()):
            return None
        if isinstance(other, pcbnew.PCB_VIA):
            p = point(other.GetPosition())
            return p if p in (a, b) else None
        if isinstance(other, pcbnew.PAD):
            if other.GetShape(track.GetLayer()) not in {
                pcbnew.PAD_SHAPE_CIRCLE,
                pcbnew.PAD_SHAPE_RECT,
                pcbnew.PAD_SHAPE_OVAL,
                pcbnew.PAD_SHAPE_ROUNDRECT,
            }:
                return None
            hits = [p for p in (a, b) if other.HitTest(pcbnew.VECTOR2I(*p))]
            return hits[0] if len(hits) == 1 else None
        if not isinstance(other, pcbnew.PCB_TRACK) or isinstance(other, pcbnew.PCB_ARC):
            return None
        c, d = point(other.GetStart()), point(other.GetEnd())
        if c == d:
            return None

        on = CopperContacts.on_segment
        hits = [p for p in (a, b) if on(p, c, d)]
        if len(hits) != 1:
            return None
        # Collinear copper with positive overlap can carry upstream current along
        # the candidate. A common endpoint alone does not prove a load-only spur.
        if (b[0] - a[0]) * (d[1] - c[1]) == (b[1] - a[1]) * (d[0] - c[0]) and any(
            on(p, a, b) and p not in (a, b) for p in (c, d)
        ):
            return None
        return hits[0]

    @staticmethod
    def common_landing(track, upstream) -> bool:
        """Say whether a track meets its upstream items through one retained conductor.

        Inputs:
            track: The branch track.
            upstream: The upstream items that touch the track.
        Outputs:
            True when ``retained_landing`` accepts every (track, item) pair.
        """
        return CopperContacts.retained_landing((track, item) for item in upstream)

    @staticmethod
    def polygon(item, layer, error_mode):
        """Copper outline of an item on one layer.

        Inputs:
            item: A track, pad, via or shape.
            layer: KiCad layer id.
            error_mode: Whether the polygon approximation errs outside or inside the
                true copper.
        Outputs:
            A KiCad polygon set.
        """
        import pcbnew

        result = pcbnew.SHAPE_POLY_SET()
        item.TransformShapeToPolygon(result, layer, 0, 2000, error_mode)
        return result

    @staticmethod
    def retained_landing(pairs) -> bool:
        """Confirm that the branch contacts upstream through one retained conductor.

        A secondary contact must fit completely inside the landing's copper. A solid
        graphic or a pad may underlie several touching branch segments.

        Inputs:
            pairs: (branch track, upstream item) pairs.
        Outputs:
            True when one retained conductor, a flashed convex pad or via, a solid
            filled graphic or a wider track, covers every contact. False otherwise.
        """
        import pcbnew

        polygon = CopperContacts.polygon

        convex_pads = {
            pcbnew.PAD_SHAPE_CIRCLE,
            pcbnew.PAD_SHAPE_RECT,
            pcbnew.PAD_SHAPE_OVAL,
            pcbnew.PAD_SHAPE_ROUNDRECT,
        }
        contacts = []
        for trace, parent in pairs:
            trace, parent = typed_copper(trace), typed_copper(parent)
            layer = trace.GetLayer()
            if (
                trace.Type() != pcbnew.PCB_TRACE_T
                or trace.GetNetCode() <= 0
                or trace.GetStart() == trace.GetEnd()
                or parent.GetNetCode() != trace.GetNetCode()
                or not parent.IsOnLayer(layer)
            ):
                return False
            if isinstance(parent, (pcbnew.PAD, pcbnew.PCB_VIA)):
                if parent.ConditionallyFlashed(layer) or not parent.FlashLayer(layer):
                    return False
                if (
                    isinstance(parent, pcbnew.PAD)
                    and parent.GetShape(layer) not in convex_pads
                ):
                    return False
            elif isinstance(parent, pcbnew.PCB_SHAPE):
                if not parent.IsSolidFill():
                    return False
            elif parent.Type() != pcbnew.PCB_TRACE_T:
                return False
            outside = polygon(parent, layer, pcbnew.ERROR_OUTSIDE)
            contact = polygon(trace, layer, pcbnew.ERROR_OUTSIDE)
            contact.BooleanIntersection(outside)
            if not contact.OutlineCount():
                return False
            contacts.append((trace, parent, outside, contact))

        for trace, witness, outside, _ in contacts:
            layer = trace.GetLayer()
            inside = polygon(witness, layer, pcbnew.ERROR_INSIDE)
            start, end = trace.GetStart(), trace.GetEnd()
            if not (
                (inside.Contains(start) and not outside.Contains(end))
                or (inside.Contains(end) and not outside.Contains(start))
            ):
                continue
            if isinstance(witness, pcbnew.PCB_SHAPE):
                if witness.GetShape() != pcbnew.SHAPE_T_RECT:
                    continue
            elif CopperContacts.contact_port(trace, witness) is None:
                if (
                    witness.Type() != pcbnew.PCB_TRACE_T
                    or witness.GetWidth() <= trace.GetWidth()
                ):
                    continue
                a, b = witness.GetStart(), witness.GetEnd()
                if (end.x - start.x) * (b.y - a.y) == (end.y - start.y) * (b.x - a.x):
                    continue
            for piece, parent, _, overlap in contacts:
                if piece.GetLayer() != layer:
                    break
                if parent.m_Uuid == witness.m_Uuid:
                    continue
                uncovered = pcbnew.SHAPE_POLY_SET(overlap)
                uncovered.BooleanSubtract(inside)
                if uncovered.OutlineCount():
                    break
            else:
                return True
        return False

    @staticmethod
    def direct_graph(connectivity, items, graphics) -> dict[str, set[str]]:
        """Use KiCad's direct contacts and include contacts between copper graphics.

        Inputs:
            connectivity: Built KiCad connectivity of the board.
            items: Mapping from uuid to the pads, tracks and graphics of one net.
            graphics: The copper graphic shapes of the net.
        Outputs:
            Mapping from uuid to the set of uuids it touches.
        """
        graph = {uid: set() for uid in items}
        for uid, item in items.items():
            # The whole-island GetConnectedItems query cannot establish a direct contact.
            for neighbor in (
                *connectivity.GetConnectedTracks(item),
                *connectivity.GetConnectedPads(item),
            ):
                other = neighbor.m_Uuid.AsString()
                if other in graph and other != uid:
                    graph[uid].add(other)
                    graph[other].add(uid)
        for a, b in combinations(graphics, 2):
            layer = a.GetLayer()
            if layer == b.GetLayer() and a.GetEffectiveShape(layer).Collide(
                b.GetEffectiveShape(layer), 0
            ):
                i, j = a.m_Uuid.AsString(), b.m_Uuid.AsString()
                graph[i].add(j)
                graph[j].add(i)
        return graph

    @staticmethod
    def bounded_landing_cut(
        graph, items, pads, loads, tracks, declared_loads
    ) -> float | None:
        """Prove a connected branch made of several segments sharing one landing.

        Inputs:
            graph: Contact graph of the net, from ``direct_graph``.
            items: Mapping from uuid to the net's items.
            pads: Uuids of the pads of the net.
            loads: Mapping from pad uuid to its design current bound, in A.
            tracks: Uuids of the segments that form the branch.
            declared_loads: Pad uuids that have a declared load.
        Outputs:
            The current bound downstream of the cut, in A; None when the segments are
            not one connected branch, an isolated pad has no load, or the branch does
            not land on one retained conductor.
        """
        if not tracks or not tracks <= graph.keys():
            return None
        seed = min(tracks)
        connected = BranchGraph.component(graph, seed)
        source_pads = (connected & pads) - declared_loads
        if not source_pads or not tracks <= connected:
            return None
        retained = {
            uid: adjacent - tracks
            for uid, adjacent in graph.items()
            if uid not in tracks
        }
        upstream = BranchGraph.component(retained, min(source_pads))
        if not source_pads <= upstream:
            return None
        isolated = connected - upstream
        leaves = isolated & pads
        if not leaves or not leaves <= loads.keys():
            return None
        if (
            BranchGraph.component(
                {uid: graph[uid] & isolated for uid in isolated}, seed
            )
            != isolated
        ):
            return None
        contacts = (
            (items[uid], items[neighbor])
            for uid in tracks
            for neighbor in graph[uid] & upstream
        )
        if not CopperContacts.retained_landing(contacts):
            return None
        return sum(loads[pad] for pad in leaves)


class LandingProofs:
    """Proves that branch tracks land on one retained conductor and remembers which did.

    Inputs:
        items: Mapping from uuid to the pads, tracks and graphics of one net.
    """

    def __init__(self, items: dict):
        """Store the items and start with no proven track.

        Inputs:
            items: Mapping from uuid to the net's items.
        """
        self.items = items
        self.proven = set()

    def __call__(self, uid: str, parents) -> bool:
        """Prove one track's upstream contacts share a landing.

        Inputs:
            uid: Uuid of the branch track.
            parents: Uuids of the upstream items that touch it.
        Outputs:
            True, and the track is recorded in ``proven``, when
            ``CopperContacts.common_landing`` accepts them; False otherwise.
        """
        if not CopperContacts.common_landing(
            self.items[uid], (self.items[parent] for parent in parents)
        ):
            return False
        self.proven.add(uid)
        return True


class RuleText:
    """The text of width rules and of their managed block in a KiCad rules file."""

    @staticmethod
    def quoted(value: str) -> str:
        """Return a value that is safe inside a rule string.

        Inputs:
            value: A net name, layer or reference.
        Outputs:
            The value. Raises ValueError when it contains a quote, backslash or line
            break.
        """
        if any(ch in value for ch in "'\"\\\r\n"):
            raise ValueError("Unsafe rule selector")
        return value

    @staticmethod
    def rule(
        name: str, condition: str, width: float, *, precision: int = 3, layer: str = ""
    ) -> str:
        """Format one minimum track-width rule.

        Inputs:
            name: Rule name after the ``AutoPCB`` prefix.
            condition: KiCad rule condition.
            width: Minimum track width, in mm; at least 0.15 mm.
            precision: Decimal places written for the width.
            layer: Layer the rule is limited to; all layers when empty.
        Outputs:
            The rule text. Raises ValueError when the width is not a finite number of at
            least 0.15 mm.
        """
        if (
            isinstance(width, bool)
            or not isinstance(width, (int, float))
            or not math.isfinite(width)
            or width < 0.15
        ):
            raise ValueError("Invalid branch width")
        scope = f" (layer {layer})" if layer else ""
        return f'(rule "AutoPCB {RuleText.quoted(name)}"{scope} (condition "{condition}") (constraint track_width (min {width:.{precision}f}mm)))'

    @staticmethod
    def replace_managed(source: str, block: str) -> str:
        """Insert or replace the managed width-rule block of a rules file.

        Inputs:
            source: Rules file text.
            block: New managed block, from the BEGIN to the END marker.
        Outputs:
            The updated text; a missing block is appended. Raises ValueError when the
            markers are unbalanced or repeated.
        """
        if source.count(BEGIN) != source.count(END) or source.count(BEGIN) > 1:
            raise ValueError("Malformed managed width rules")
        if BEGIN in source:
            start, end = source.index(BEGIN), source.index(END) + len(END)
            return source[:start] + block.rstrip() + source[end:]
        return source.rstrip() + "\n\n" + block


class WidthRules:
    """Compiles terminal-aware width rules from a board and the branch-width policy."""

    @staticmethod
    def selected_pad(board, net: str, item: dict):
        """Find the one pad named by a terminal description.

        Inputs:
            board: The loaded board.
            net: Net name of the terminal.
            item: Terminal description with the footprint ``reference``, a ``value``
                prefix and an optional ``pin``.
        Outputs:
            The pad. Raises ValueError when the footprint or its value changed, or the
            terminal is missing or ambiguous.
        """
        part = board.FindFootprintByReference(item["reference"])
        if part is None or not str(part.GetValue()).startswith(item["value"]):
            raise ValueError(f"{net}: component identity changed: {item['reference']}")
        pads = [
            p
            for p in part.Pads()
            if p.GetNetname() == net
            and ("pin" not in item or p.GetNumber() == item["pin"])
        ]
        if len(pads) != 1:
            raise ValueError(
                f"{net}: ambiguous/missing terminal on {item['reference']}"
            )
        return pads[0]

    @staticmethod
    def coordinates(item) -> tuple[int, int, int, int]:
        """Ends of a track.

        Inputs:
            item: A track.
        Outputs:
            (x1, y1, x2, y2), in nm.
        """
        a, b = item.GetStart(), item.GetEnd()
        return a.x, a.y, b.x, b.y

    @staticmethod
    def limits(values) -> str:
        """Rule condition that matches track end coordinates within the native tolerance.

        Inputs:
            values: (x1, y1, x2, y2) in nm; each is matched within TAP_MATCH_NM.
        Outputs:
            The condition text, in mm.
        """
        return " && ".join(
            f"(A.{field} > {(value - TAP_MATCH_NM) / 1e6:.6f}mm && "
            f"A.{field} < {(value + TAP_MATCH_NM) / 1e6:.6f}mm)"
            for field, value in strict_zip(
                ("Origin_X", "Origin_Y", "End_X", "End_Y"), values
            )
        )

    @staticmethod
    def tap_selector(board, track, copper) -> str | None:
        """Bind a proven leaf to one native track despite mm conversion rounding.

        Inputs:
            board: The loaded board.
            track: The track to select.
            copper: The tracks and arcs of the same net.
        Outputs:
            A rule condition that matches only this track by net, layer and both end
            coordinates in either direction; None when another copper item of the net on
            the layer lies within one native unit of the matching box.
        """
        import pcbnew

        coordinates = WidthRules.coordinates
        start = coordinates(track)
        reverse = start[2:] + start[:2]
        # Reject even a nearby competitor outside the nominal matching box: the
        # KiCad parser can round either endpoint of that box by one native unit.
        for item in copper:
            if (
                item.m_Uuid == track.m_Uuid
                or item.GetNetCode() != track.GetNetCode()
                or item.Type() not in {pcbnew.PCB_TRACE_T, pcbnew.PCB_ARC_T}
                or item.GetLayer() != track.GetLayer()
            ):
                continue
            actual = coordinates(item)
            if any(
                all(
                    abs(a - b) <= TAP_MATCH_NM + 1
                    for a, b in strict_zip(actual, expected)
                )
                for expected in (start, reverse)
            ):
                return None

        limits = WidthRules.limits
        return (
            f"A.Type == 'Track' && A.NetName == '{RuleText.quoted(str(track.GetNetname()))}' && "
            f"A.Layer == '{RuleText.quoted(str(board.GetLayerName(track.GetLayer())))}' && "
            f"(({limits(start)}) || ({limits(reverse)}))"
        )

    @staticmethod
    def rail_rules(board, rails: list[dict]) -> list[str]:
        """Write the whole-branch rail rules.

        Inputs:
            board: The loaded board.
            rails: Configured rails, each with a ``net``, ``width_mm``, optional
                ``inner_width_mm``, its ``terminals`` and optional ``escapes``.
        Outputs:
            The rule lines. Raises ValueError when the net's pads differ from the
            documented terminals or an escape is not on the documented branch.
        """
        lines = []
        for rail in rails:
            name = RuleText.quoted(rail["net"])
            expected = {
                WidthRules.selected_pad(board, name, p).m_Uuid.AsString()
                for p in rail["terminals"]
            }
            actual = {
                p.m_Uuid.AsString() for p in board.GetPads() if p.GetNetname() == name
            }
            if actual != expected:
                raise ValueError(f"{name}: whole-branch terminal inventory changed")
            terminal_refs = {pad["reference"] for pad in rail["terminals"]}
            rule_scope = f"A.Type == 'Track' && A.NetName == '{name}'"
            lines.append(RuleText.rule(name, rule_scope, rail["width_mm"]))
            if "inner_width_mm" in rail:
                lines.append(
                    RuleText.rule(
                        name + " inner",
                        rule_scope,
                        rail["inner_width_mm"],
                        layer="inner",
                    )
                )
            for width, escapes in groupby(
                rail.get("escapes", []), key=lambda escape: escape["width_mm"]
            ):
                references = [escape["reference"] for escape in escapes]
                if any(ref not in terminal_refs for ref in references):
                    raise ValueError(f"{name}: escape is not on the documented branch")
                scopes = " || ".join(
                    f"A.intersectsCourtyard('{RuleText.quoted(ref)}')"
                    for ref in references
                )
                if len(references) == 1:
                    lines.append(
                        RuleText.rule(
                            name + " " + references[0] + " escape",
                            f"{rule_scope} && {scopes}",
                            width,
                        )
                    )
                else:
                    title = f"{name} local {width:.3f}mm escapes"
                    lines.append(
                        RuleText.rule(title, f"{rule_scope} && ({scopes})", width)
                    )
        return lines

    @staticmethod
    def graphics_by_net(board) -> dict:
        """Group the copper graphic shapes of a board by net.

        Inputs:
            board: The loaded board.
        Outputs:
            Mapping from net name to the board and footprint copper shapes that carry a
            net.
        """
        import pcbnew

        result = defaultdict(list)
        footprint_graphics = chain.from_iterable(
            part.GraphicalItems() for part in board.GetFootprints()
        )
        for shape in chain(board.GetDrawings(), footprint_graphics):
            if (
                isinstance(shape, pcbnew.PCB_SHAPE)
                and shape.GetNetCode() > 0
                and pcbnew.IsCopperLayer(shape.GetLayer())
            ):
                result[shape.GetNetname()].append(shape)
        return result

    @staticmethod
    def tap_rules(
        board, connectivity, name: str, tap: dict, loads: dict, declared: set, graphics
    ) -> tuple[list[str], int]:
        """Write the width rules of one tap group.

        Inputs:
            board: The loaded board.
            connectivity: Built connectivity of the board.
            name: Net name of the tap.
            tap: Configured tap with its widths, current limit and optional width
                floors.
            loads: Mapping from terminal pad uuid to its design current bound, in A.
            declared: Pad uuids of the net with a declared load.
            graphics: Copper graphic shapes of the net.
        Outputs:
            (rule lines, number of rules). Raises ValueError for a zone on the net,
            invalid width floors, an unproven local load path, or a changed floor or
            non-unique geometry.
        """
        import pcbnew

        if any(z.GetNetname() == name and not z.GetIsRuleArea() for z in board.Zones()):
            raise ValueError(
                f"{name}: zone-mediated branch flow needs a different model"
            )
        parts = [p for p in board.GetPads() if p.GetNetname() == name]
        copper = [t for t in board.GetTracks() if t.GetNetname() == name]
        parts_by_id = {p.m_Uuid.AsString(): p for p in parts}
        items = parts_by_id | {t.m_Uuid.AsString(): t for t in chain(copper, graphics)}
        graph = CopperContacts.direct_graph(connectivity, items, graphics)
        tracks = {
            t.m_Uuid.AsString()
            for t in copper
            if not isinstance(t, (pcbnew.PCB_VIA, pcbnew.PCB_ARC))
        }
        ports = {
            (uid, neighbor): port
            for uid in tracks
            for neighbor in graph[uid]
            if (port := CopperContacts.contact_port(items[uid], items[neighbor]))
            is not None
        }
        landing_proofs = LandingProofs(items)
        floors = tap.get("track_width_floors_mm")
        if floors is not None and (
            not isinstance(floors, dict)
            or not floors
            or any(
                not isinstance(width, (int, float))
                or isinstance(width, bool)
                or not math.isfinite(width)
                or width < tap["width_mm"]
                for width in floors.values()
            )
        ):
            raise ValueError(f"{name}: invalid local track width floors")
        selected = BranchGraph.bounded_leaves(
            graph,
            set(parts_by_id),
            loads,
            tracks,
            contact_ports=ports,
            common_landing=landing_proofs,
            declared_loads=declared,
            include_downstream=floors is not None,
        )
        eligible = {
            uid for uid, current in selected.items() if current <= tap["max_branch_a"]
        }
        if floors is not None and (unproved := set(floors) - eligible):
            cut = set(floors)
            current = (
                CopperContacts.bounded_landing_cut(
                    graph, items, set(parts_by_id), loads, cut, declared
                )
                if cut <= tracks
                else None
            )
            if current is None or current > tap["max_branch_a"]:
                refs = ", ".join(str(p["reference"]) for p in tap["terminals"])
                raise ValueError(
                    f"{name} ({refs}): local load path unproven for {', '.join(sorted(unproved))}"
                )
            eligible.update(cut)
        lines, count = [], 0
        for uid in sorted(eligible if floors is None else floors):
            condition = WidthRules.tap_selector(board, items[uid], copper)
            width = items[uid].GetWidth() / 1e6
            if floors is not None and (condition is None or width < floors[uid]):
                raise ValueError(
                    f"{name}: local width floor or unique geometry changed for {uid}"
                )
            if condition is None:
                continue
            count += 1
            if floors is not None:
                lines.append(
                    RuleText.rule(
                        name + " tap " + uid, condition, floors[uid], precision=6
                    )
                )
            elif uid in landing_proofs.proven or tap.get(
                "preserve_installed_width", False
            ):
                floor = max(tap["width_mm"], width)
                lines.append(
                    RuleText.rule(name + " tap " + uid, condition, floor, precision=6)
                )
            else:
                lines.append(
                    RuleText.rule(name + " tap " + uid, condition, tap["width_mm"])
                )
        return lines, count

    @staticmethod
    def compile(board, config: dict) -> tuple[str, dict]:
        """Compile the managed width-rule block of a board.

        Inputs:
            board: The loaded board.
            config: Branch-width policy with ``schema`` 1, ``electrically_verified``
                False, the required components, rails and taps.
        Outputs:
            (block text, mapping from net name to the number of proven tap tracks).
            Raises ValueError when the policy status is wrong, a required component or
            terminal changed, a tap net has a zone, or a branch cannot be proven.
        """
        if (
            config.get("schema") != 1
            or config.get("electrically_verified") is not False
        ):
            raise ValueError(
                "Branch targets must retain their unqualified electrical status"
            )
        for item in config.get("required_components", []):
            part = board.FindFootprintByReference(item["reference"])
            if part is None or not str(part.GetValue()).startswith(item["value"]):
                raise ValueError(
                    f"Current-setting component changed: {item['reference']}"
                )
        lines, counts = (
            [
                BEGIN,
                "# Normal-current targets; fault/thermal/physical qualification remains open.",
            ],
            {},
        )
        connectivity = board.GetConnectivity()
        connectivity.Build(board)
        lines.extend(WidthRules.rail_rules(board, config.get("rails", [])))
        tap_groups, declared_loads = [], defaultdict(set)
        for tap in config.get("taps", []):
            name = RuleText.quoted(tap["net"])
            loads = {
                WidthRules.selected_pad(board, name, item).m_Uuid.AsString(): item[
                    "max_current_a"
                ]
                for item in tap["terminals"]
            }
            tap_groups.append((name, tap, loads))
            declared_loads[name].update(loads)
        graphics_by_net = WidthRules.graphics_by_net(board)
        for name, tap, loads in tap_groups:
            rules, count = WidthRules.tap_rules(
                board,
                connectivity,
                name,
                tap,
                loads,
                declared_loads[name],
                graphics_by_net[name],
            )
            counts.setdefault(name, 0)
            counts[name] += count
            lines.extend(rules)
        return "\n".join((*lines, END, "")), counts


class WidthRuleCheck:
    """Checks that a width-rule edit changed nothing but width findings."""

    @staticmethod
    def findings(report: dict) -> Counter:
        """Count the findings that a width-rule edit may not change.

        Inputs:
            report: Native DRC report with its ``violations`` list.
        Outputs:
            A count of (type, severity, sorted item uuids) per finding, leaving out
            track-width and connection-width findings.
        """
        return Counter(
            (
                v["type"],
                v["severity"],
                tuple(sorted(i["uuid"] for i in v.get("items", []))),
            )
            for v in report["violations"]
            if v["type"] not in {"track_width", "connection_width"}
        )

    @staticmethod
    def unchanged_other_rules(before: dict, after: dict) -> None:
        """Require that only width findings differ between two reports.

        Inputs:
            before, after: Native DRC reports before and after the rule edit.
        Outputs:
            None. Raises RuntimeError when a new unrelated finding appears, the
            schematic parity changes, or the number of open connections changes.
        """
        findings = WidthRuleCheck.findings
        new = findings(after) - findings(before)
        if new or after["schematic_parity"] != before["schematic_parity"]:
            kinds = Counter()
            for (kind, _, _), count in new.items():
                kinds[kind] += count
            raise RuntimeError(
                f"Branch-rule edit changed unrelated findings: {dict(kinds)}"
            )
        if len(after["unconnected_items"]) != len(before["unconnected_items"]):
            raise RuntimeError("Width-only rule edit changed connectivity")


class BranchWidthsCommand:
    """Command-line entry that compiles, checks and optionally publishes the width rules."""

    @staticmethod
    def publish(guard, candidate: str, target: Path) -> None:
        """Replace the rules file atomically after the guard confirms nothing changed.

        Inputs:
            guard: The board save guard with the checked dependencies.
            candidate: The new rules text.
            target: The rules file to replace.
        Outputs:
            None. Raises when a dependency changed or the editor holds the project.
        """
        guard.verify_unchanged()
        guard.verify_editor_unlocked()
        fd, path = tempfile.mkstemp(
            prefix=".width-rules-", suffix=".tmp", dir=target.parent
        )
        staged = Path(path)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(candidate.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            guard.verify_unchanged()
            guard.verify_editor_unlocked()
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)

    def __call__(self, argv=None) -> int:
        """Parse the command line, check the compiled rules and optionally publish them.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0.
        """
        import pcbnew

        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--project", type=Path, required=True)
        parser.add_argument("--apply", action="store_true")
        args = parser.parse_args(argv)
        project = args.project.resolve()
        policy = asset_root(project) / "config" / "branch_widths.json"
        policy_hash = file_sha256(policy)
        config = json.loads(policy.read_text(encoding="utf-8"))
        target = native_path(project, ".kicad_dru")
        with (
            quiet_duplicate_image_handlers(),
            Snapshot(native_path(project, ".kicad_pcb")) as snapshot,
        ):
            guard = BoardSaveGuard(
                snapshot.source,
                tuple(snapshot.fingerprints.items()) + ((policy, policy_hash),),
            )
            board = pcbnew.LoadBoard(str(snapshot.board_path))
            block, counts = WidthRules.compile(board, config)
            original = target.read_text(encoding="utf-8")
            candidate = RuleText.replace_managed(original, block)
            if candidate == original:
                print("Branch-width rules already match the current terminal topology")
                return 0
            cli = kicad_cli()
            print(
                "Checking original and terminal-aware rules on the same private PCB",
                flush=True,
            )
            before = _drc(
                cli,
                snapshot.board_path,
                snapshot.directory / "before.json",
                schematic_parity=True,
            )
            snapshot.board_path.with_suffix(".kicad_dru").write_text(
                candidate, encoding="utf-8"
            )
            after = _drc(
                cli,
                snapshot.board_path,
                snapshot.directory / "after.json",
                schematic_parity=True,
            )
            WidthRuleCheck.unchanged_other_rules(before, after)
            print("Proven leaf tracks:", sum(counts.values()), counts)
            print(
                "Track-width findings:",
                sum(v["type"] == "track_width" for v in before["violations"]),
                "->",
                sum(v["type"] == "track_width" for v in after["violations"]),
            )
            snapshot.check()
        if args.apply:
            self.publish(guard, candidate, target)
            print(
                "Applied branch rules; schematic, placement and copper were not edited"
            )
        else:
            print("Preview only; use --apply to publish the checked rules")
        return 0


main = BranchWidthsCommand()

if __name__ == "__main__":
    raise SystemExit(main())
