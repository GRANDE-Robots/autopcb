"""width_repair.py: Repair track-width findings in checked batches of ordinary copper.

Native DRC lists the tracks narrower than the minimum width of their net. One mode per
run repairs them: widen in place, detour, shift sideways, relocate a series run, widen
with neighbours, remove copper that retained copper already covers, remove redundant
terminal tips, or widen and rejoin ground islands. A batch is kept only when, on a
private copy of the board, native DRC finds nothing new, protected design items,
branch-width proofs, routing style and reference copper are unchanged, and the saved
board is still the one that was checked. With --apply the checked board is saved.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from core.project import native_path

import argparse
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal
from itertools import product
import json
import math
from pathlib import Path
import re
import tempfile
import time

from core.kicad_campaign import CampaignPlanner
from core.compat import file_sha256
from core.kicad_sexpr import parse
from core.paths import asset_root, data_path
from core.toolchain import kicad_cli
from core.pcb_io import (
    BoardSaveGuard,
    copper_boundary,
    quiet_duplicate_image_handlers,
    typed_copper,
)


class ConnectivityRegression(RuntimeError):
    """A width increase disconnected copper after a fresh zone fill."""


class SeriesRuns:
    """Finds runs of series tracks between two fixed end points of a native board."""

    @staticmethod
    def chain(board, track, *, maximum: int = 32):
        """Extend a uniform-width series run, stopping at pads, vias and branches.

        Inputs:
            board: The loaded board.
            track: Track to start from.
            maximum: Most segments in the run.
        Outputs:
            (tracks, start, end): the run's tracks in order and its two end points in
            nm. A run that closes on itself returns the seed track alone with its own
            ends.
        """
        import pcbnew
        from core.pcb_io import copper_width
        from pcb.branch_widths import CopperContacts

        connectivity = board.GetConnectivity()
        connectivity.Build(board)
        net, layer, width = track.GetNetCode(), track.GetLayer(), track.GetWidth()
        grouped = {
            x.m_Uuid.AsString() for group in board.Groups() for x in group.GetItems()
        }
        path = deque([(track, track.GetStart(), track.GetEnd())])
        selected = {track.m_Uuid.AsString()}
        for left in (True, False):
            while len(path) < maximum:
                item, start, end = path[0 if left else -1]
                point = start if left else end
                if any(
                    p.GetNetCode() == net and p.IsOnLayer(layer) and p.HitTest(point)
                    for p in connectivity.GetConnectedPads(item)
                ):
                    break
                neighbors = []
                ambiguous = False
                for neighbor in map(
                    typed_copper, connectivity.GetConnectedTracks(item)
                ):
                    if neighbor.m_Uuid == item.m_Uuid or not neighbor.IsOnLayer(layer):
                        continue
                    if isinstance(neighbor, pcbnew.PCB_VIA):
                        pos = neighbor.GetPosition()
                        if (pos.x - point.x) ** 2 + (pos.y - point.y) ** 2 <= (
                            copper_width(neighbor, layer=layer) / 2
                        ) ** 2:
                            ambiguous = True
                        continue
                    port = CopperContacts.contact_port(item, neighbor)
                    if port is None:
                        ambiguous = True
                    elif port == (point.x, point.y):
                        neighbors.append(neighbor)
                if ambiguous or len(neighbors) != 1:
                    break
                neighbor = neighbors[0]
                uid = neighbor.m_Uuid.AsString()
                if (
                    uid in selected
                    or isinstance(neighbor, pcbnew.PCB_ARC)
                    or neighbor.GetNetCode() != net
                    or neighbor.GetWidth() != width
                    or neighbor.IsLocked()
                    or uid in grouped
                ):
                    break
                other = (
                    neighbor.GetEnd()
                    if neighbor.GetStart() == point
                    else neighbor.GetStart()
                )
                if other == point:
                    break
                selected.add(uid)
                if left:
                    path.appendleft((neighbor, other, point))
                else:
                    path.append((neighbor, point, other))
        if path[0][1] == path[-1][2]:
            return (track,), track.GetStart(), track.GetEnd()
        return tuple(item for item, _, _ in path), path[0][1], path[-1][2]

    @staticmethod
    def layers(
        board, chain, a, b, copper: dict[str, int], *, connection=None
    ) -> tuple[str, ...]:
        """Allow only series-end transitions that shorten both existing via paths.

        Inputs:
            board: The loaded board.
            chain: The tracks of the run.
            a, b: The end points of the run, each a through via, in nm.
            copper: Copper thickness by layer name.
            connection: Built connectivity; built here when None.
        Outputs:
            A tuple that starts with the run's own layer name, followed by each layer
            within the span of both vias that is neither the run's layer nor a layer
            where their tails land, has at least the run's copper thickness and gives
            both vias a pad no smaller than on the run's layer. Only the own layer when
            its copper thickness is unknown, the net has a zone, either end is not one
            through via whose other side is one track or SMD pad on one other layer, or
            the run touches anything else.
        """
        import pcbnew
        from core.pcb_io import copper_width

        source = chain[0].GetLayer()
        name = str(board.GetLayerName(source))
        original = (name,)
        net = chain[0].GetNetCode()
        if copper.get(name, 0) <= 0 or any(
            z.GetNetCode() == net and not z.GetIsRuleArea() for z in board.Zones()
        ):
            return original
        stack = tuple(board.GetEnabledLayers().CuStack())
        order = {layer: index for index, layer in enumerate(stack)}
        if connection is None:
            connection = board.GetConnectivity()
            connection.Build(board)
        removed = {t.m_Uuid.AsString() for t in chain}
        vias, outside, limits = [], set(), set(stack)
        exterior = set()
        for point in (a, b):
            candidates = [
                v
                for v in board.GetTracks()
                if isinstance(v, pcbnew.PCB_VIA)
                and v.GetNetCode() == net
                and v.IsOnLayer(source)
                and v.GetPosition() == point
            ]
            if len(candidates) != 1:
                return original
            via = candidates[0]
            if via.GetViaType() != pcbnew.VIATYPE_THROUGH or via.GetDrill() <= 0:
                return original
            tails = [
                t
                for t in map(typed_copper, connection.GetConnectedTracks(via))
                if t.m_Uuid != via.m_Uuid and t.m_Uuid.AsString() not in removed
            ]
            if any(
                isinstance(t, (pcbnew.PCB_VIA, pcbnew.PCB_ARC)) or t.GetNetCode() != net
                for t in tails
            ):
                return original
            layers = {t.GetLayer() for t in tails}
            for pad in connection.GetConnectedPads(via):
                pad_layers = [layer for layer in stack if pad.IsOnLayer(layer)]
                if (
                    pad.GetNetCode() != net
                    or pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD
                    or pad.GetDrillSize().x
                    or pad.GetDrillSize().y
                    or len(pad_layers) != 1
                ):
                    return original
                layers.add(pad_layers[0])
            if len(layers) != 1 or source in layers:
                return original
            layer = next(iter(layers))
            exterior.add(layer)
            lo, hi = sorted((order[source], order[layer]))
            limits &= set(stack[lo : hi + 1])
            outside.update(t.m_Uuid.AsString() for t in tails)
            vias.append(via)
        if vias[0].m_Uuid == vias[1].m_Uuid:
            return original
        allowed = outside | {v.m_Uuid.AsString() for v in vias}
        for item in chain:
            if connection.GetConnectedPads(item):
                return original
            if any(
                t.m_Uuid.AsString() not in removed | allowed
                for t in connection.GetConnectedTracks(item)
            ):
                return original
        choices = [
            str(board.GetLayerName(layer))
            for layer in stack
            if layer in limits - exterior - {source}
            and copper.get(str(board.GetLayerName(layer)), 0) >= copper[name]
            and all(
                v.IsOnLayer(layer)
                and copper_width(v, layer=layer) >= copper_width(v, layer=source)
                for v in vias
            )
        ]
        return original + tuple(choices)

    @staticmethod
    def endpoint_aliases(board, tracks, open_ports, connection):
        """Match unique, overlapping trace ends between two fixed through vias.

        Inputs:
            board: The loaded board.
            tracks: Mapping from uuid to the run's tracks.
            open_ports: Mapping from each free end point, in nm, to the uuid of its
                track.
            connection: Built connectivity.
        Outputs:
            A mapping from each loose end point to the lower of its matched pair. None
            when the run does not end on exactly two through vias, or a loose end has no
            unique mate within 0.1 mm or a quarter of the narrower width, whichever is
            smaller.
        """
        import pcbnew

        anchors = tuple(copper_boundary(board, tracks.values()).values())
        if len(anchors) != 2 or any(
            not isinstance(item, pcbnew.PCB_VIA)
            or item.GetViaType() != pcbnew.VIATYPE_THROUGH
            for item in anchors
        ):
            return None
        anchor_points = {
            (item.GetPosition().x, item.GetPosition().y) for item in anchors
        }
        if len(anchor_points) != 2 or not anchor_points <= open_ports.keys():
            return None
        loose_ports = {
            point: uid
            for point, uid in open_ports.items()
            if point not in anchor_points
        }
        matches = {}
        for point, uid in loose_ports.items():
            track = tracks[uid]
            adjacent = {
                item.m_Uuid.AsString() for item in connection.GetConnectedTracks(track)
            }
            mates = [
                other
                for other, other_uid in loose_ports.items()
                if other_uid != uid
                and other_uid in adjacent
                and math.dist(point, other)
                <= min(100000, track.GetWidth() // 4, tracks[other_uid].GetWidth() // 4)
            ]
            if len(mates) != 1:
                return None
            matches[point] = mates[0]
        if any(matches.get(other) != point for point, other in matches.items()):
            return None
        return {point: min(point, other) for point, other in matches.items()}

    @staticmethod
    def anchored(
        board, track, *, connection=None, maximum: int = 64, allow_pads: bool = False
    ):
        """Find a complete same-layer path using both copper contact and centerlines.

        A wide chamfer can touch the segment two steps away. Require a single centerline
        path, then let ``layers`` prove both external via contacts.

        Inputs:
            board: The loaded board.
            track: Track to start from.
            connection: Built connectivity; built here when None.
            maximum: Most tracks in the run.
            allow_pads: Accept runs that touch pads; the two contacts must then be SMD
                or through-hole pads or vias on the net and layer.
        Outputs:
            (tracks, start, end) with the end points as KiCad points in nm, or None when
            the run is locked, grouped, masked, branched, too long, or does not form one
            open path.
        """
        import pcbnew

        if connection is None:
            connection = board.GetConnectivity()
            connection.Build(board)
        layer, net = track.GetLayer(), track.GetNetCode()
        found, pending = {}, [track]
        while pending:
            item = pending.pop()
            uid = item.m_Uuid.AsString()
            if uid in found:
                continue
            if (
                len(found) >= maximum
                or item.Type() != pcbnew.PCB_TRACE_T
                or item.IsLocked()
                or item.GetParentGroup() is not None
                or item.HasSolderMask()
                or (not allow_pads and connection.GetConnectedPads(item))
            ):
                return None
            found[uid] = item
            pending.extend(
                other
                for other in map(typed_copper, connection.GetConnectedTracks(item))
                if other.GetNetCode() == net
                and other.GetLayer() == layer
                and not isinstance(other, pcbnew.PCB_VIA)
                and other.m_Uuid.AsString() not in found
            )

        neighbors = defaultdict(list)
        port_owners = {}
        edges = []
        for uid, item in found.items():
            a, b = item.GetStart(), item.GetEnd()
            first, last = (a.x, a.y), (b.x, b.y)
            if first == last:
                return None
            neighbors[first].append(last)
            neighbors[last].append(first)
            port_owners[first] = port_owners[last] = uid
            edges.append((first, last))
        ends = sorted(point for point, links in neighbors.items() if len(links) == 1)
        if any(len(links) > 2 for links in neighbors.values()):
            return None
        if len(ends) > 2 and not allow_pads:
            aliases = SeriesRuns.endpoint_aliases(
                board, found, {point: port_owners[point] for point in ends}, connection
            )
            if aliases is None:
                return None
            neighbors.clear()
            for first, last in edges:
                first, last = aliases.get(first, first), aliases.get(last, last)
                if first == last or last in neighbors[first]:
                    return None
                neighbors[first].append(last)
                neighbors[last].append(first)
            ends = sorted(
                point for point, links in neighbors.items() if len(links) == 1
            )
        if len(ends) != 2:
            return None
        visited, pending = set(), [ends[0]]
        while pending:
            point = pending.pop()
            if point not in visited:
                visited.add(point)
                pending.extend(neighbors[point])
        if len(visited) != len(neighbors):
            return None
        chain = tuple(found.values())
        if allow_pads:
            contacts = tuple(copper_boundary(board, chain).values())
            if len(contacts) != 2 or any(
                not item.IsOnLayer(layer) or item.GetNetCode() != net
                for item in contacts
            ):
                return None
            for item in contacts:
                if isinstance(item, pcbnew.PAD):
                    if item.GetAttribute() not in (
                        pcbnew.PAD_ATTRIB_SMD,
                        pcbnew.PAD_ATTRIB_PTH,
                    ):
                        return None
                elif not isinstance(item, pcbnew.PCB_VIA):
                    return None
            positions = [
                (item.GetPosition().x, item.GetPosition().y) for item in contacts
            ]
            if sorted(positions) != ends:
                return None
        return chain, *(pcbnew.VECTOR2I(*point) for point in ends)

    @staticmethod
    def trials(
        board,
        minima: dict[str, int],
        copper: dict[str, int],
        *,
        same_layer: bool = False,
    ):
        """Schedule a whole run once per allowed layer, with fixed boundary contacts.

        Inputs:
            board: The loaded board.
            minima: Mapping from uuid to its required width, in nm.
            copper: Copper thickness by layer name.
            same_layer: Rebuild each run on its own layer between pads or vias.
        Outputs:
            A list of (uuid, required width in nm, layer name). Runs with more findings
            come first, then shorter runs when rebuilt on the same layer or longer runs
            otherwise; each run's layers are listed from the last allowed to the first.
        """
        import pcbnew

        connection = board.GetConnectivity()
        connection.Build(board)
        tracks = {track.m_Uuid.AsString(): track for track in board.GetTracks()}
        seen, trials = set(), []
        for uid in sorted(minima):
            track = tracks[uid]
            if (
                uid in seen
                or track.Type() != pcbnew.PCB_TRACE_T
                or track.IsLocked()
                or track.GetParentGroup()
            ):
                continue
            series = SeriesRuns.anchored(
                board, track, connection=connection, allow_pads=same_layer
            )
            if series is None:
                continue
            chain, start, end = series
            members = {item.m_Uuid.AsString() for item in chain}
            seen.update(members)
            layers = (
                (str(board.GetLayerName(track.GetLayer())),)
                if same_layer
                else SeriesRuns.layers(
                    board, chain, start, end, copper, connection=connection
                )[1:]
            )
            if layers:
                findings = members & minima.keys()
                required = max(minima[item] for item in findings)
                length = sum(item.GetLength() for item in chain)
                trials.append(
                    (
                        -len(findings),
                        length if same_layer else -length,
                        uid,
                        required,
                        layers,
                    )
                )
        return [
            (uid, minimum, layer)
            for _, _, uid, minimum, layers in sorted(trials)
            for layer in reversed(layers)
        ]


class RouteEnds:
    """Constrains and offers the end points of a rerouted run."""

    @staticmethod
    def headings(board, track, *, removed_ids=frozenset(), endpoints=None):
        """Constrain new ends against a single fixed neighbor, without constraining branches.

        Inputs:
            board: The loaded board.
            track: The track being replaced.
            removed_ids: Uuids of all tracks being replaced.
            endpoints: The (start, end) points in nm; the track's own ends when None.
        Outputs:
            A tuple of two entries, one per end. Each is a frozenset of the allowed
            direction indices, the neighbor's direction and the two adjacent 45-degree
            steps, or None when the end touches a pad or via, has no single neighbor, or
            the neighbor is not horizontal, vertical or 45 degrees.
        """
        import pcbnew
        from core.pcb_io import copper_width
        from pcb.route_optimizer import DIRECTIONS

        net, layer = track.GetNetCode(), track.GetLayer()
        omitted = removed_ids | {track.m_Uuid.AsString()}
        copper = [
            x
            for x in board.GetTracks()
            if x.m_Uuid.AsString() not in omitted
            and x.GetNetCode() == net
            and x.IsOnLayer(layer)
        ]
        result = []
        a, b = endpoints or (track.GetStart(), track.GetEnd())
        for start, point in ((True, a), (False, b)):
            anchored = any(
                p.GetNetCode() == net and p.IsOnLayer(layer) and p.HitTest(point)
                for p in board.GetPads()
            )
            anchored |= any(
                isinstance(x, pcbnew.PCB_VIA)
                and (x.GetPosition().x - point.x) ** 2
                + (x.GetPosition().y - point.y) ** 2
                <= (copper_width(x, layer=layer) / 2) ** 2
                for x in copper
            )
            neighbors = [
                x
                for x in copper
                if not isinstance(x, (pcbnew.PCB_VIA, pcbnew.PCB_ARC))
                and point in (x.GetStart(), x.GetEnd())
            ]
            if anchored or len(neighbors) != 1:
                result.append(None)
                continue
            neighbor = neighbors[0]
            other = (
                neighbor.GetEnd()
                if neighbor.GetStart() == point
                else neighbor.GetStart()
            )
            dx, dy = (
                (point.x - other.x, point.y - other.y)
                if start
                else (other.x - point.x, other.y - point.y)
            )
            if not (dx or dy) or (dx and dy and abs(dx) != abs(dy)):
                result.append(None)
                continue
            direction = DIRECTIONS.index(((dx > 0) - (dx < 0), (dy > 0) - (dy < 0)))
            result.append(
                frozenset((direction, (direction - 1) % 8, (direction + 1) % 8))
            )
        return tuple(result)

    @staticmethod
    def pad_landings(board, chain, terminal, point):
        """Offer nearby landings on the same SMD pad when no branch uses this port.

        Inputs:
            board: The loaded board.
            chain: The tracks of the run.
            terminal: The run's track at this end.
            point: The end point, in nm.
        Outputs:
            A tuple of points in nm that starts with the original end. When no zone is
            on the net, no other track uses the port and the end sits on one SMD pad, it
            adds the pad centre and the points offset along each axis by the smaller of
            0.75 mm and a third of the pad extent, if they are on the pad and within 1.5
            mm of the original end.
        """
        import pcbnew
        from pcb.branch_widths import CopperContacts
        from pcb.route_optimizer import Point, mm

        origin = Point(point.x, point.y)
        layer, net = terminal.GetLayer(), terminal.GetNetCode()
        if any(
            zone.GetNetCode() == net and not zone.GetIsRuleArea()
            for zone in board.Zones()
        ):
            return (origin,)
        connectivity = board.GetConnectivity()
        connectivity.Build(board)
        selected = {item.m_Uuid.AsString() for item in chain}
        for item in map(typed_copper, connectivity.GetConnectedTracks(terminal)):
            if item.m_Uuid.AsString() in selected or not item.IsOnLayer(layer):
                continue
            port = CopperContacts.contact_port(terminal, item)
            if port is None or port == (point.x, point.y):
                return (origin,)
        pads = [
            pad
            for pad in connectivity.GetConnectedPads(terminal)
            if pad.GetNetCode() == net and pad.IsOnLayer(layer) and pad.HitTest(point)
        ]
        if len(pads) != 1 or pads[0].GetAttribute() != pcbnew.PAD_ATTRIB_SMD:
            return (origin,)
        pad = pads[0]
        center, bounds = pad.GetPosition(), pad.GetBoundingBox()
        sites = [Point(center.x, center.y)]
        for axis in (0, 1):
            for sign in (-1, 1):
                x, y = center.x, center.y
                if axis == 0:
                    x += sign * min(mm(0.75), bounds.GetWidth() // 3)
                else:
                    y += sign * min(mm(0.75), bounds.GetHeight() // 3)
                sites.append(Point(x, y))
        return tuple(
            dict.fromkeys(
                (
                    origin,
                    *(
                        site
                        for site in sites
                        if math.hypot(site.x - point.x, site.y - point.y) <= mm(1.5)
                        and pad.HitTest(pcbnew.VECTOR2I(site.x, site.y))
                    ),
                )
            )
        )


class WidthReroute:
    """Searches one rule-aware detour that replaces a thin series run.

    Inputs:
        project: Folder of the KiCad project.
        uid: Uuid of the thin track.
        minimum: Required width, in nm; at least 150000.
        seconds: Search budget, in seconds.
        relocate: Allow a track already at the minimum width.
        target_layer: Move the run to this layer; None keeps the layers the run allows.
        same_layer_series: Rebuild a pad or via run on its own layer; needs
            ``target_layer``.
    """

    def __init__(
        self,
        project: Path,
        uid: str,
        minimum: int,
        seconds: float,
        *,
        relocate: bool = False,
        target_layer: str | None = None,
        same_layer_series: bool = False,
    ):
        """Store the request.

        Inputs:
            project: Folder of the KiCad project.
            uid: Uuid of the thin track.
            minimum: Required width, in nm; at least 150000.
            seconds: Search budget, in seconds; finite and positive.
            relocate: Allow a track already at the minimum width.
            target_layer: Layer to move the run to, or None.
            same_layer_series: Rebuild a pad or via run on its own layer.
        Outputs:
            None. Raises ValueError for a bad budget or minimum, or
            ``same_layer_series`` without ``target_layer``.
        """
        if (
            (same_layer_series and target_layer is None)
            or not math.isfinite(seconds)
            or seconds <= 0
            or not isinstance(minimum, int)
            or minimum < 150000
        ):
            raise ValueError("Invalid width-rerouting budget or minimum")
        self.project = project
        self.uid = uid
        self.minimum = minimum
        self.seconds = seconds
        self.relocate = relocate
        self.target_layer = target_layer
        self.same_layer_series = same_layer_series
        self.old_style = None
        self.rejected_shapes = set()

    @classmethod
    def plan(
        cls,
        project: Path,
        uid: str,
        minimum: int,
        seconds: float,
        *,
        relocate: bool = False,
        target_layer: str | None = None,
        same_layer_series: bool = False,
    ) -> bytes | None:
        """Propose a rule-aware detour; independent native checks must accept it.

        Inputs:
            project: Folder of the KiCad project.
            uid: Uuid of the thin track.
            minimum: Required width, in nm; at least 150000.
            seconds: Search budget, in seconds.
            relocate: Allow a track already at the minimum width.
            target_layer: Move the run to this layer, or None.
            same_layer_series: Rebuild a pad or via run on its own layer.
        Outputs:
            The candidate board bytes, or None when the track is not eligible or no
            sampled detour passes. Raises ValueError for a bad budget or minimum and
            RuntimeError when the board changed during planning.
        """
        return cls(
            project,
            uid,
            minimum,
            seconds,
            relocate=relocate,
            target_layer=target_layer,
            same_layer_series=same_layer_series,
        ).run()

    def load(self) -> bool:
        """Read the board and find the run to replace.

        Outputs:
            True when the track is an ordinary, unlocked, ungrouped segment no wider
            than the minimum (equal only when relocating), not on a high-speed net, and
            has an eligible series run and layers; False otherwise.
        """
        import pcbnew
        from pcb.net_routing.reference import high_speed_nets
        from pcb.route_native import _configured_copper_thicknesses

        self.source = native_path(self.project, ".kicad_pcb")
        self.original = self.source.read_bytes()
        self.board = pcbnew.LoadBoard(str(self.source))
        board, uid = self.board, self.uid
        self.track = track = next(
            (x for x in board.GetTracks() if x.m_Uuid.AsString() == uid), None
        )
        grouped = {
            x.m_Uuid.AsString() for group in board.Groups() for x in group.GetItems()
        }
        if (
            track is None
            or isinstance(track, (pcbnew.PCB_ARC, pcbnew.PCB_VIA))
            or track.IsLocked()
            or uid in grouped
            or track.GetWidth() > self.minimum
            or (track.GetWidth() == self.minimum and not self.relocate)
        ):
            return False
        self.net = net = str(track.GetNetname())
        if net in high_speed_nets(
            self.source.with_suffix(".kicad_pro").read_bytes(), {net}
        ):
            return False
        if self.same_layer_series:
            series = SeriesRuns.anchored(board, track, allow_pads=True)
        elif self.target_layer is not None:
            series = SeriesRuns.anchored(board, track)
        else:
            series = SeriesRuns.chain(board, track)
        if series is None:
            return False
        self.chain, self.a, self.b = series
        self.copper = dict(_configured_copper_thicknesses(self.original))
        self.source_layer = str(board.GetLayerName(track.GetLayer()))
        eligible_layers = (
            (self.source_layer,)
            if self.same_layer_series
            else SeriesRuns.layers(board, self.chain, self.a, self.b, self.copper)
        )
        if self.target_layer is not None:
            if self.target_layer not in (
                eligible_layers if self.same_layer_series else eligible_layers[1:]
            ):
                return False
            self.layers = (self.target_layer,)
            self.minimum = max(self.minimum, *(item.GetWidth() for item in self.chain))
        else:
            self.layers = (self.source_layer,) if self.relocate else eligible_layers
        return True

    def prepare(self) -> bool:
        """Read the obstacles and rules and bound the search window.

        Outputs:
            True when the window around the run, inside the board edge clearance, is not
            empty.
        """
        from pcb.net_routing.rules import RoutingRules
        from pcb.route_native import read_native_obstacles
        from pcb.route_optimizer import Point, mm

        board, minimum, a, b = self.board, self.minimum, self.a, self.b
        self.removed_ids = frozenset(item.m_Uuid.AsString() for item in self.chain)
        self.remove = [
            {"kind": "segment", "uuid": key} for key in sorted(self.removed_ids)
        ]
        self.native = read_native_obstacles(
            self.source, self.net, assume_refill=True, removals=self.remove
        )
        rules = RoutingRules.from_board(board, self.source.with_suffix(".kicad_dru"))
        width_floor = (
            minimum if self.target_layer is not None else self.track.GetWidth()
        )
        self.profile = rules.profile(
            self.net, minimum, mm(0.2), width_floor=width_floor
        )
        margin = max(mm(5 if self.target_layer is not None else 2), minimum)
        outline = board.GetBoardEdgesBoundingBox()
        edge = max(
            0,
            int(board.GetDesignSettings().m_CopperEdgeClearance)
            - self.profile.minimum_clearance,
        )
        self.lower = Point(
            max(outline.GetX() + edge, min(a.x, b.x) - margin),
            max(outline.GetY() + edge, min(a.y, b.y) - margin),
        )
        self.upper = Point(
            min(outline.GetRight() - edge, max(a.x, b.x) + margin),
            min(outline.GetBottom() - edge, max(a.y, b.y) + margin),
        )
        return self.lower.x < self.upper.x and self.lower.y < self.upper.y

    def check_budget(self) -> None:
        """Raise SearchBudgetExceeded once the search budget or the layer's share is spent."""
        from pcb.route_optimizer import SearchBudgetExceeded

        if time.monotonic() >= min(self.deadline, self.attempt_deadline):
            raise SearchBudgetExceeded("Width-rerouting search budget exhausted")

    def materialize(self, route) -> bytes:
        """Build the board bytes that replace the run by a route.

        Inputs:
            route: A grid route from the search.
        Outputs:
            The board bytes with the run removed and the route inserted.
        """
        from pcb.route_junctions import junction_changes
        from pcb.route_optimizer import mm

        changes, _ = junction_changes(
            self.board,
            self.original,
            route,
            self.net,
            self.minimum,
            mm(0.6),
            mm(0.3),
            removed_copper_ids=self.removed_ids,
            prune_dangling_tails=False,
            segment_dimensions=self.profile.pieces,
        )
        changes[:0] = [
            {"document": "pcb", "op": "remove", "target": target, "path": []}
            for target in self.remove
        ]
        return self.planner.plan("pcb", changes).after

    def legal_style(self, route) -> bool:
        """Accept a route whose board introduces no routing-style defect.

        A shape that already failed, or any shape once eight have failed, is refused
        without a check.

        Inputs:
            route: A grid route from the search.
        Outputs:
            True when the style audit of the candidate shows nothing new.
        """
        import pcbnew
        from pcb.net_routing.style import audit_style

        if route.segments in self.rejected_shapes or len(self.rejected_shapes) >= 8:
            return False
        if self.old_style is None:
            self.old_style = audit_style(self.board)
        with tempfile.TemporaryDirectory(prefix="autopcb-width-style-") as folder:
            candidate = Path(folder) / "candidate.kicad_pcb"
            candidate.write_bytes(self.materialize(route))
            try:
                EditProofs.routing_style(
                    self.old_style, audit_style(pcbnew.LoadBoard(str(candidate)))
                )
            except ConnectivityRegression:
                self.rejected_shapes.add(route.segments)
                return False
        return True

    def acceptable(self, route) -> bool:
        # Equal or lower series resistance; layer moves also shorten the
        # two previously used through-via barrel spans.
        """Accept a route with no via, equal or lower series resistance and a legal style.

        Layer moves also shorten the two previously used through-via barrel spans.

        Inputs:
            route: A grid route from the search.
        Outputs:
            True when the route is accepted.
        """
        resistance = sum(
            math.hypot(piece.end.x - piece.start.x, piece.end.y - piece.start.y) / width
            for segment in route.segments
            for piece, width in self.profile.pieces(segment)
        )
        return (
            not (route.vias or route.reused_vias)
            and resistance / self.thickness <= self.prior + 1e-9
            and self.legal_style(route)
        )

    def contacts_retained(self, candidate: bytes) -> bool:
        """Check that a candidate keeps the run's boundary contacts and removes only the run.

        Inputs:
            candidate: Board bytes of the candidate.
        Outputs:
            True when exactly the run's tracks are gone and the added copper touches the
            same boundary items.
        """
        import pcbnew

        with tempfile.TemporaryDirectory(prefix="autopcb-width-contacts-") as folder:
            path = Path(folder) / "candidate.kicad_pcb"
            path.write_bytes(candidate)
            trial = pcbnew.LoadBoard(str(path))
            current = {t.m_Uuid.AsString(): t for t in trial.GetTracks()}
            if self.original_ids - current.keys() != self.removed_ids:
                return False
            added = tuple(current[key] for key in current.keys() - self.original_ids)
            return set(copper_boundary(trial, added)) == self.boundary_ids

    def run(self) -> bytes | None:
        """Search each allowed layer for an accepted route.

        Outputs:
            The candidate board bytes, or None when the track is not eligible or no
            sampled detour passes. Raises RuntimeError when the board changed during
            planning.
        """
        from pcb.route_optimizer import (
            GridRouter,
            NoGridRoute,
            Point,
            SearchBudgetExceeded,
            SearchLimitReached,
            mm,
        )

        if not self.load() or not self.prepare():
            return None
        board, track, net, minimum = self.board, self.track, self.net, self.minimum
        chain, a, b, copper = self.chain, self.a, self.b, self.copper
        lower, upper, layers = self.lower, self.upper, self.layers
        source_layer = self.source_layer
        self.deadline = time.monotonic() + self.seconds
        self.attempt_deadline = self.deadline
        initial, final = (
            (None, None)
            if self.target_layer is not None
            else RouteEnds.headings(
                board, track, removed_ids=self.removed_ids, endpoints=(a, b)
            )
        )
        step = mm(0.1 if self.target_layer is not None else 0.05)
        while ((upper.x - lower.x + step - 1) // step + 1) * (
            (upper.y - lower.y + step - 1) // step + 1
        ) > 1500000:
            step *= 2
        self.original_ids = {t.m_Uuid.AsString() for t in board.GetTracks()}
        self.boundary_ids = set(copper_boundary(board, chain))
        start, end = Point(a.x, a.y), Point(b.x, b.y)
        alternatives = [(start, end)]
        if self.target_layer is None:
            alternatives = sorted(
                product(
                    RouteEnds.pad_landings(board, chain, chain[0], a),
                    RouteEnds.pad_landings(board, chain, chain[-1], b),
                ),
                key=lambda pair: (pair[0] != start) + (pair[1] != end),
            )
        self.prior = sum(
            item.GetLength() / item.GetWidth() for item in chain
        ) / copper.get(source_layer, 1)
        self.planner = CampaignPlanner(self.project)
        for index, layer in enumerate(layers):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                break
            self.attempt_deadline = time.monotonic() + remaining / (len(layers) - index)
            try:
                router = GridRouter(
                    lower,
                    upper,
                    step,
                    (layer,),
                    net,
                    minimum,
                    mm(0.2),
                    mm(0.6),
                    mm(0.3),
                    self.native.obstacles,
                    max_expansions=80000,
                    max_cells=1500000,
                    allow_new_vias=False,
                    allowed_reused_vias=set(),
                    forbid_staircase=True,
                    gentle_turns=True,
                    first_feasible=True,
                    heuristic_weight=4,
                    profile=self.profile,
                    check_budget=self.check_budget,
                    initial_directions=initial if layer == source_layer else None,
                    final_directions=final if layer == source_layer else None,
                )
            except (NoGridRoute, SearchBudgetExceeded, SearchLimitReached):
                continue
            self.thickness = copper.get(layer, 1)
            choices = alternatives if layer == source_layer else [(start, end)]
            layer_deadline = self.attempt_deadline
            for index_site, (first, last) in enumerate(choices):
                remaining = layer_deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Keep a failed search at an original anchor from consuming the
                # whole budget before its alternate pad landings are tried.
                fraction = (
                    1
                    if index_site == len(choices) - 1
                    else 0.55
                    if index_site == 0
                    else 0.6
                )
                self.attempt_deadline = time.monotonic() + remaining * fraction
                try:
                    route = router.route(
                        first, layer, last, layer, candidate_check=self.acceptable
                    )
                except (NoGridRoute, SearchBudgetExceeded, SearchLimitReached):
                    continue
                candidate = self.materialize(route)
                if (
                    self.relocate
                    or self.same_layer_series
                    or layer != source_layer
                    or (first, last) != (start, end)
                ) and not self.contacts_retained(candidate):
                    continue
                if self.source.read_bytes() != self.original:
                    raise RuntimeError("Width-rerouting source changed during planning")
                return candidate
        return None


class ShiftedWidening:
    """Widens straight traces by moving them sideways while keeping their contacts."""

    @staticmethod
    def _covers_old_trace(old_width: int, new_width: int, dx: int, dy: int) -> bool:
        """A translated capsule contains the old one if its extra radius covers the offset.

        Inputs:
            old_width, new_width: Track widths, in nm.
            dx, dy: The sideways offset, in nm.
        Outputs:
            True when the wider track moved by the offset still covers the old track's
            copper.
        """
        added_width = new_width - old_width
        return added_width > 0 and 4 * (dx * dx + dy * dy) <= added_width * added_width

    @staticmethod
    def sites(
        project: Path, minima: dict[str, int]
    ) -> list[tuple[str, int, tuple[int, int]]]:
        """Screen small perpendicular shifts, preferring those that keep all existing copper.

        Only horizontal, vertical or 45-degree tracks of at least 0.1 mm that are
        unlocked, ungrouped and unmasked, touch a pad or other copper and touch no via
        are screened. A shift is kept when the shifted copper clears the native
        obstacles and either the track touches a pad or the wider track still covers the
        old copper. Shifts go in steps of 0.025 mm up to 0.65 mm on either side, and the
        search for a track stops once twelve are found.

        Inputs:
            project: Folder of the KiCad project.
            minima: Mapping from uuid to its required width, in nm.
        Outputs:
            A list of (uuid, width in nm, (dx, dy) in nm). Every track's first shift
            comes before any second choice, and shifts that keep all existing copper
            come first within a rank.
        """
        import pcbnew
        from pcb.freeroute_delta import Trace, native_clearance_checker
        from pcb.net_routing.rules import RoutingRules

        path = native_path(project, ".kicad_pcb")
        board = pcbnew.LoadBoard(str(path))
        connectivity = board.GetConnectivity()
        connectivity.Build(board)
        tracks = {item.m_Uuid.AsString(): item for item in board.GetTracks()}
        rules = RoutingRules.from_board(board, path.with_suffix(".kicad_dru"))
        profiles = {}
        clear = native_clearance_checker(
            board, profile_for=lambda item: profiles[item.net]
        )
        trials = []
        for uid, minimum in sorted(
            minima.items(), key=lambda entry: (entry[1], entry[0])
        ):
            track = tracks[uid]
            if (
                track.Type() != pcbnew.PCB_TRACE_T
                or track.IsLocked()
                or track.GetParentGroup() is not None
                or track.HasSolderMask()
                or track.GetLength() < 100000
            ):
                continue
            has_pad = bool(connectivity.GetConnectedPads(track))
            neighbors = tuple(connectivity.GetConnectedTracks(track))
            if (not has_pad and not neighbors) or any(
                item.Type() == pcbnew.PCB_VIA_T for item in neighbors
            ):
                continue
            a, b = track.GetStart(), track.GetEnd()
            dx, dy = b.x - a.x, b.y - a.y
            if (dx and dy and abs(dx) != abs(dy)) or not (dx or dy):
                continue
            nx, ny = (dy < 0) - (dy > 0), (dx > 0) - (dx < 0)
            net, layer = (
                str(track.GetNetname()),
                str(board.GetLayerName(track.GetLayer())),
            )
            profiles[net] = rules.profile(net, minimum, 0, width_floor=minimum)
            count = 0
            for step in range(25000, 650001, 25000):
                for distance in (step, -step):
                    x, y = nx * distance, ny * distance
                    first, last = (a.x + x, a.y + y), (b.x + x, b.y + y)
                    preserves_copper = ShiftedWidening._covers_old_trace(
                        track.GetWidth(), minimum, x, y
                    )
                    if (has_pad or preserves_copper) and clear(
                        (Trace(net, layer, minimum, first, last),)
                    ):
                        trials.append(
                            (count, not preserves_copper, minimum, uid, (x, y))
                        )
                        count += 1
                if count >= 12:
                    break
        return [(uid, minimum, offset) for _, _, minimum, uid, offset in sorted(trials)]

    @staticmethod
    def plan(
        project: Path, uid: str, minimum: int, offset: tuple[int, int]
    ) -> bytes | None:
        """Build one widened translation if all native boundary contacts are retained.

        Inputs:
            project: Folder of the KiCad project.
            uid: Uuid of the track.
            minimum: New width, in nm.
            offset: Sideways (dx, dy), in nm, at most 0.65 mm and perpendicular to the
                track.
        Outputs:
            The candidate board bytes, or None when the track is not eligible, the
            offset is not allowed, a via is a contact, the old copper is not covered, or
            the boundary contacts change. Raises RuntimeError when the board changed
            during planning.
        """
        import pcbnew

        planner = CampaignPlanner(project)
        path = planner.document_path("pcb")
        original = path.read_bytes()
        board = pcbnew.LoadBoard(str(path))
        track = next(
            (item for item in board.GetTracks() if item.m_Uuid.AsString() == uid), None
        )
        if (
            track is None
            or track.Type() != pcbnew.PCB_TRACE_T
            or track.IsLocked()
            or track.GetParentGroup() is not None
            or track.HasSolderMask()
            or track.GetWidth() >= minimum
        ):
            return None
        a, b = track.GetStart(), track.GetEnd()
        x, y = offset
        if (
            not (x or y)
            or max(abs(x), abs(y)) > 650000
            or (b.x - a.x) * x + (b.y - a.y) * y
        ):
            return None
        contacts = copper_boundary(board, (track,))
        if not contacts or any(
            isinstance(item, pcbnew.PCB_VIA) for item in contacts.values()
        ):
            return None
        if not any(
            isinstance(item, pcbnew.PAD) for item in contacts.values()
        ) and not ShiftedWidening._covers_old_trace(track.GetWidth(), minimum, x, y):
            return None
        target = {"kind": "segment", "uuid": uid}
        expressions = {
            "start": f"(start {(a.x + x) / 1e6:.6f} {(a.y + y) / 1e6:.6f})",
            "end": f"(end {(b.x + x) / 1e6:.6f} {(b.y + y) / 1e6:.6f})",
            "width": f"(width {minimum / 1e6:.6f})",
        }
        candidate = planner.plan(
            "pcb",
            [
                {
                    "op": "replace",
                    "target": target,
                    "path": [field],
                    "expression": value,
                }
                for field, value in expressions.items()
            ],
        ).after
        with tempfile.TemporaryDirectory(prefix="autopcb-shift-contacts-") as folder:
            candidate_path = Path(folder) / path.name
            candidate_path.write_bytes(candidate)
            trial = pcbnew.LoadBoard(str(candidate_path))
            shifted = next(
                item for item in trial.GetTracks() if item.m_Uuid.AsString() == uid
            )
            if set(copper_boundary(trial, (shifted,))) != set(contacts):
                return None
        if path.read_bytes() != original:
            raise RuntimeError("Width repair source changed during translation")
        return candidate


class CoupledWidening:
    """Widens a trace together with the obstructing neighbours that must move."""

    @staticmethod
    def neighbors(
        report: dict, targets: set[str], movable: set[str], *, limit: int = 3
    ) -> dict[str, tuple[str, ...]]:
        """Attribute each trial widening's clearance failures to editable traces.

        Inputs:
            report: Native DRC report of the board with the trial widenings.
            targets: Uuids of the widened tracks.
            movable: Uuids of the tracks that may be rerouted.
            limit: Most neighbours per target.
        Outputs:
            Mapping from each target to its sorted neighbour uuids. A target is left out
            when a failure involves anything but it and one movable neighbour, or when
            it has more than ``limit`` neighbours.
        """
        dependencies = {uid: set() for uid in targets}
        blocked = set()
        for finding in report["violations"]:
            if finding["type"] not in {
                "clearance",
                "shorting_items",
                "hole_clearance",
                "edge_clearance",
                "items_not_allowed",
            }:
                continue
            items = {item["uuid"] for item in finding["items"] if item.get("uuid")}
            for uid in items & targets:
                others = items - {uid}
                if len(others) != 1 or not others <= movable:
                    blocked.add(uid)
                else:
                    dependencies[uid].update(others)
        return {
            uid: tuple(sorted(values))
            for uid, values in dependencies.items()
            if uid not in blocked and len(values) <= limit
        }

    @staticmethod
    def plan(
        project: Path, uid: str, minimum: int, neighbors: dict[str, int], seconds: float
    ) -> bytes | None:
        """Widen a fixed trace and negotiate the obstructing copper together.

        Inputs:
            project: Folder of the KiCad project.
            uid: Uuid of the track to widen.
            minimum: New width, in nm.
            neighbors: Mapping from neighbour uuid to the width it must keep, in nm.
            seconds: Search budget per neighbour, in seconds.
        Outputs:
            The board bytes with the track widened and each neighbour rerouted, trying
            the given order and its reverse; the widened board alone when there are no
            neighbours; None when no order succeeds. Raises RuntimeError when the board
            changed during planning.
        """
        import pcbnew

        before = native_path(project, ".kicad_pcb")
        original = before.read_bytes()
        widened = WidthEdit.candidate(project, {uid: minimum})
        if not neighbors:
            return widened
        with tempfile.TemporaryDirectory(prefix="autopcb-width-coordinated-") as folder:
            private = Path(folder)
            candidate = private / before.name
            for suffix in (".kicad_pro", ".kicad_dru"):
                support = before.with_suffix(suffix)
                candidate.with_suffix(suffix).write_bytes(support.read_bytes())
            sequence = tuple(neighbors)
            orders = (sequence,) if len(sequence) == 1 else (sequence, sequence[::-1])
            for order in orders:
                candidate.write_bytes(widened)
                for neighbor in order:
                    board = pcbnew.LoadBoard(str(candidate))
                    tracked = next(
                        (
                            item
                            for item in board.GetTracks()
                            if item.m_Uuid.AsString() == neighbor
                        ),
                        None,
                    )
                    if tracked is None:
                        # A preceding repair may have replaced the same series run.
                        continue
                    proposal = WidthReroute.plan(
                        private, neighbor, neighbors[neighbor], seconds, relocate=True
                    )
                    if proposal is None:
                        break
                    candidate.write_bytes(proposal)
                else:
                    if before.read_bytes() != original:
                        raise RuntimeError(
                            "Board changed while planning a coordinated width repair"
                        )
                    return candidate.read_bytes()
        return None


class WidthEdit:
    """Edits the width fields of ordinary segments in a board source."""

    @staticmethod
    def candidate(project: Path, widths: dict[str, int]) -> bytes:
        """Edit only width fields on ordinary existing segments; never narrow copper.

        Inputs:
            project: Folder of the KiCad project.
            widths: Mapping from segment uuid to its new width, in nm.
        Outputs:
            The board bytes; unchanged when no width differs. Raises ValueError for a
            width that is not an integer from 150000 to 20000000, a uuid that is not a
            segment, a narrower width or a locked segment.
        """
        planner = CampaignPlanner(project)
        source = planner.document_path("pcb").read_bytes()
        root = parse(source.decode("utf-8"))
        tracks = {
            form.children("uuid")[0].atoms()[1].value: form
            for form in root.children("segment")
        }
        changes = []
        for uid, width in sorted(widths.items()):
            if (
                isinstance(width, bool)
                or not isinstance(width, int)
                or not 150000 <= width <= 20000000
            ):
                raise ValueError("Invalid width target")
            if uid not in tracks:
                raise ValueError(
                    "Width repair requires an ordinary segment, not an arc or via"
                )
            item = tracks[uid]
            old = Decimal(item.children("width")[0].atoms()[1].value) * 1000000
            if width < old:
                raise ValueError("Width repair cannot narrow existing copper")
            if width == old:
                continue
            if item.children("locked"):
                raise ValueError("Width repair cannot modify locked copper")
            changes.append(
                {
                    "op": "replace",
                    "target": {"kind": "segment", "uuid": uid},
                    "path": ["width"],
                    "expression": f"(width {width / 1e6:g})",
                }
            )
        return planner.plan("pcb", changes).after if changes else source


class CoveredCopper:
    """Finds copper that is already covered by retained copper."""

    @staticmethod
    def contains_track(outer, inner) -> bool:
        """Prove that a straight track's exact round-ended copper covers another.

        Inputs:
            outer, inner: Straight tracks.
        Outputs:
            True when the outer track is at least as wide and both ends of the inner
            track lie within the outer track's round-ended copper.
        """
        width_difference = outer.GetWidth() - inner.GetWidth()
        if width_difference < 0:
            return False
        start, end = outer.GetStart(), outer.GetEnd()
        dx, dy = end.x - start.x, end.y - start.y
        length_squared = dx * dx + dy * dy
        for point in (inner.GetStart(), inner.GetEnd()):
            x, y = point.x - start.x, point.y - start.y
            projection = x * dx + y * dy
            if projection <= 0:
                covered = 4 * (x * x + y * y) <= width_difference**2
            elif projection >= length_squared:
                x, y = point.x - end.x, point.y - end.y
                covered = 4 * (x * x + y * y) <= width_difference**2
            else:
                covered = (
                    4 * (x * dy - y * dx) ** 2 <= width_difference**2 * length_squared
                )
            if not covered:
                return False
        return True

    @staticmethod
    def plated_lands(board):
        """Yield plated objects, whose unchanged drills remove copper on either board.

        Inputs:
            board: The loaded board.
        Outputs:
            A generator of every via, then every plated through-hole pad.
        """
        import pcbnew

        yield from (
            item for item in board.GetTracks() if isinstance(item, pcbnew.PCB_VIA)
        )
        yield from (
            pad
            for pad in board.GetPads()
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH
        )

    @staticmethod
    def plated_flashes(board) -> frozenset[tuple[str, int]]:
        """Record actual flashed copper so conditional lands cannot become witnesses by accident.

        Inputs:
            board: The loaded board.
        Outputs:
            A frozenset of (uuid, layer) for each plated land flashed on a copper layer.
        """
        layers = tuple(board.GetEnabledLayers().CuStack())
        return frozenset(
            (item.m_Uuid.AsString(), layer)
            for item in CoveredCopper.plated_lands(board)
            for layer in layers
            if item.IsOnLayer(layer) and item.FlashLayer(layer)
        )

    @staticmethod
    def shape(item, layer, rounding):
        """Polygon of an item's copper on one layer.

        Inputs:
            item: A track, pad or via.
            layer: KiCad layer id.
            rounding: Whether the approximation errs outside or inside the true copper.
        Outputs:
            A KiCad polygon set.
        """
        import pcbnew

        polygon = pcbnew.SHAPE_POLY_SET()
        item.TransformShapeToPolygon(polygon, layer, 0, 2000, rounding)
        return polygon

    @staticmethod
    def tracks(board, requested: set[str]) -> frozenset[str]:
        """Find segments fully covered by retained copper on the same net and layer.

        Use an outer polygon for each candidate and inner polygons for curved witnesses.
        A flagged track can support a later deletion only after this pass has committed
        to retaining it. Callers using filled zones must supply a freshly refilled board
        and verify both the zone fills and plated lands on the final native round trip.

        Inputs:
            board: The loaded board.
            requested: Uuids of the tracks to test.
        Outputs:
            A frozenset of the requested uuids whose copper lies entirely in retained
            tracks, pads, plated lands or filled zones of the same net and layer.
            Locked, grouped and masked tracks are never selected.
        """
        import pcbnew

        shape = CoveredCopper.shape

        grouped = {
            item.m_Uuid.AsString()
            for group in board.Groups()
            for item in group.GetItems()
        }
        tracks = [
            item
            for item in board.GetTracks()
            if item.Type() == pcbnew.PCB_TRACE_T and item.GetNetCode() > 0
        ]
        candidates = [
            track
            for track in tracks
            if track.m_Uuid.AsString() in requested
            and track.m_Uuid.AsString() not in grouped
            and not track.IsLocked()
            and not track.HasSolderMask()
        ]
        candidate_ids = {track.m_Uuid.AsString() for track in candidates}
        retained = defaultdict(list)
        for track in tracks:
            if track.m_Uuid.AsString() not in candidate_ids:
                retained[track.GetNetCode(), track.GetLayer()].append(track)
        pads = [
            pad
            for pad in board.GetPads()
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_SMD
            and not (pad.GetDrillSize().x or pad.GetDrillSize().y)
        ]
        plated = list(CoveredCopper.plated_lands(board))
        flashed = CoveredCopper.plated_flashes(board)
        convex = {
            pcbnew.PAD_SHAPE_CIRCLE,
            pcbnew.PAD_SHAPE_RECT,
            pcbnew.PAD_SHAPE_OVAL,
            pcbnew.PAD_SHAPE_ROUNDRECT,
        }
        witnesses, selected = {}, set()

        for track in sorted(
            candidates,
            key=lambda item: (
                -item.GetWidth(),
                -item.GetLength(),
                item.m_Uuid.AsString(),
            ),
        ):
            net, layer = track.GetNetCode(), track.GetLayer()
            key, uid = (net, layer), track.m_Uuid.AsString()
            if any(
                CoveredCopper.contains_track(other, track) for other in retained[key]
            ):
                selected.add(uid)
                continue
            if key not in witnesses:
                items = list(retained[key])
                items.extend(
                    pad
                    for pad in pads
                    if pad.GetNetCode() == net
                    and pad.IsOnLayer(layer)
                    and pad.GetShape(layer) in convex
                )
                items.extend(
                    item
                    for item in plated
                    if item.GetNetCode() == net
                    and (item.m_Uuid.AsString(), layer) in flashed
                    and not item.ConditionallyFlashed(layer)
                    and (
                        not isinstance(item, pcbnew.PAD)
                        or item.GetShape(layer) in convex
                    )
                )
                copper = [shape(other, layer, pcbnew.ERROR_INSIDE) for other in items]
                copper.extend(
                    zone.GetFilledPolysList(layer)
                    for zone in board.Zones()
                    if zone.GetNetCode() == net
                    and zone.IsOnLayer(layer)
                    and not zone.GetIsRuleArea()
                )
                witnesses[key] = [
                    (polygon.BBox(), polygon)
                    for polygon in copper
                    if polygon is not None and polygon.OutlineCount() > 0
                ]

            remaining = shape(track, layer, pcbnew.ERROR_OUTSIDE)
            if remaining.OutlineCount() > 0:
                bounds = remaining.BBox()
                for box, polygon in witnesses[key]:
                    if bounds.Intersects(box):
                        remaining.BooleanSubtract(polygon)
                        if remaining.OutlineCount() == 0:
                            selected.add(uid)
                            break
            if uid not in selected:
                retained[key].append(track)
                polygon = shape(track, layer, pcbnew.ERROR_INSIDE)
                if polygon.OutlineCount() > 0:
                    witnesses[key].append((polygon.BBox(), polygon))
        return frozenset(selected)

    @staticmethod
    def terminal_ends(board, requested: set[str], bound_rules: str) -> frozenset[str]:
        """Find short duplicate landings at already proven low-current pads.

        Inputs:
            board: The loaded board.
            requested: Uuids of the flagged tracks.
            bound_rules: The compiled branch-width rule block with its proven taps.
        Outputs:
            A frozenset of the uuids of tracks up to 0.05 mm long that join one SMD pad
            and one retained, proven parent that itself enters the same pad, on a net
            without zones.
        """
        import pcbnew
        from pcb.branch_widths import CopperContacts

        proven = set(
            re.findall(
                r'^\(rule "AutoPCB \S+ tap ([0-9a-f-]+)"', bound_rules, re.MULTILINE
            )
        )
        zoned = {
            zone.GetNetCode() for zone in board.Zones() if not zone.GetIsRuleArea()
        }
        connectivity = board.GetConnectivity()
        connectivity.Build(board)
        found = set()
        for stub in board.GetTracks():
            uid = stub.m_Uuid.AsString()
            if (
                uid not in requested
                or uid in proven
                or stub.Type() != pcbnew.PCB_TRACE_T
                or stub.IsLocked()
                or stub.GetParentGroup() is not None
                or stub.HasSolderMask()
                or not 0 < stub.GetLength() <= 50000
                or stub.GetNetCode() in zoned
            ):
                continue
            pads = tuple(connectivity.GetConnectedPads(stub))
            neighbors = tuple(
                typed_copper(item)
                for item in connectivity.GetConnectedTracks(stub)
                if item.m_Uuid != stub.m_Uuid
            )
            if len(pads) != 1 or len(neighbors) != 1:
                continue
            pad, parent = pads[0], neighbors[0]
            layer, net = stub.GetLayer(), stub.GetNetCode()
            if (
                pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD
                or not pad.IsOnLayer(layer)
                or pad.ConditionallyFlashed(layer)
                or not pad.FlashLayer(layer)
                or pad.GetNetCode() != net
                or parent.GetNetCode() != net
                or parent.Type() != pcbnew.PCB_TRACE_T
                or parent.GetLayer() != layer
                or parent.m_Uuid.AsString() not in proven
                or parent.m_Uuid.AsString() in requested
            ):
                continue
            # The retained trace independently enters this exact pad. The nib
            # joins no other conductor, so it is not a series bottleneck.
            if (
                CopperContacts.contact_port(stub, parent) is not None
                and CopperContacts.contact_port(parent, pad) is not None
                and any(
                    item.m_Uuid == pad.m_Uuid
                    for item in connectivity.GetConnectedPads(parent)
                )
            ):
                found.add(uid)
        return frozenset(found)


class DrcFindings:
    """Reads and compares native DRC reports."""

    @staticmethod
    def required_widths(report: dict) -> dict[str, int]:
        """Read independently measured native minima, refusing unknown report forms.

        Inputs:
            report: Native DRC report with its ``violations`` list.
        Outputs:
            Mapping from uuid to the largest minimum width reported for it, in nm.
            Raises ValueError when a width finding does not name exactly one copper
            item, has no minimum in mm, or the minimum is outside 0.15 to 20 mm.
        """
        result = {}
        for finding in report["violations"]:
            if finding["type"] != "track_width":
                continue
            items = finding.get("items", [])
            if len(items) != 1 or not items[0].get("uuid"):
                raise ValueError(
                    "A width finding must identify exactly one copper item"
                )
            match = re.search(
                r"\bmin width ([0-9]+(?:\.[0-9]+)?) mm\b", finding["description"]
            )
            if not match:
                raise ValueError("Native width finding has no supported minimum in mm")
            width = int(Decimal(match[1]) * 1000000)
            if width < 150000 or width > 20000000:
                raise ValueError(
                    "Native width minimum is outside supported fabrication bounds"
                )
            uid = items[0]["uuid"]
            result[uid] = max(width, result.get(uid, 0))
        return result

    @staticmethod
    def finding_key(value: dict, stable_neck_nets: frozenset, neck_regions) -> str:
        # Native neck warnings may choose either adjacent segment on reruns.
        # Accept another marker only on nets where the caller has proven the
        # connection did not narrow. Require the same measured widths and count.
        """Identify a finding so that two reports can be compared.

        Native neck warnings may choose either adjacent segment on reruns. Accept
        another marker only on nets where the caller has proven the connection did not
        narrow, and require the same measured widths and count.

        Inputs:
            value: One finding.
            stable_neck_nets: Nets whose connection did not narrow.
            neck_regions: Mapping from uuid to the region of unchanged copper, or None.
        Outputs:
            A JSON text equal for findings that count as the same.
        """
        if value["type"] == "connection_width" and value.get("severity") == "warning":
            ids = [item.get("uuid") for item in value.get("items", [])]
            measurement = re.search(
                r"\bmin(?:imum)?(?: connection)? width\s+"
                r"([0-9]+(?:\.[0-9]+)?|\.[0-9]+)\s+mm\b",
                value["description"],
            )
            if (
                ids
                and neck_regions
                and measurement
                and Decimal(measurement[1]) <= Decimal(".5")
                and all(uid in neck_regions for uid in ids)
            ):
                regions = {neck_regions[uid] for uid in ids}
                if len(regions) == 1:
                    return json.dumps(
                        (
                            value["type"],
                            value["severity"],
                            "unchanged_region",
                            sorted(regions),
                            value["description"],
                        )
                    )
            nets = set()
            for item in value.get("items", []):
                match = re.search(r"\[([^\]]+)\]", item.get("description", ""))
                if not match:
                    break
                nets.add(match[1])
            else:
                if len(nets) == 1 and nets <= stable_neck_nets:
                    return json.dumps(
                        (
                            value["type"],
                            value["severity"],
                            sorted(nets),
                            value["description"],
                        )
                    )
        return json.dumps(value, sort_keys=True, allow_nan=False)

    @staticmethod
    def introduced(
        before: dict,
        after: dict,
        *,
        stable_neck_nets: frozenset[str] = frozenset(),
        neck_regions: dict[str, str] | None = None,
        include_widths: bool = False,
    ) -> list[dict]:
        """Compare full measurements, not counts that could hide a new short.

        Inputs:
            before, after: Native DRC reports.
            stable_neck_nets: Nets whose neck warnings may match regardless of the
                adjacent item.
            neck_regions: Mapping from uuid to unchanged region, for neck warnings of at
                most 0.5 mm.
            include_widths: Also compare track-width findings.
        Outputs:
            The findings of ``after`` that ``before`` does not account for. Raises
            RuntimeError when the schematic parity or ignored checks changed, and
            ConnectivityRegression when the open connections changed.
        """
        if after["schematic_parity"] != before["schematic_parity"] or after.get(
            "ignored_checks"
        ) != before.get("ignored_checks"):
            raise RuntimeError("Width repair changed parity or ignored checks")

        previous = Counter(
            DrcFindings.finding_key(v, stable_neck_nets, neck_regions)
            for v in before["violations"]
            if v["type"] != "track_width" or include_widths
        )
        result = []
        for finding in after["violations"]:
            if finding["type"] == "track_width" and not include_widths:
                continue
            identifier = DrcFindings.finding_key(
                finding, stable_neck_nets, neck_regions
            )
            if previous[identifier]:
                previous[identifier] -= 1
            else:
                result.append(finding)
        if result:
            return result
        if after["unconnected_items"] != before["unconnected_items"]:
            raise ConnectivityRegression("Width repair changed connectivity")
        return []


class NeckRegions:
    """Groups unchanged copper that an edit cannot have affected."""

    @staticmethod
    def inventory(board, layers: tuple) -> dict:
        """Describe every track and pad of a board.

        Inputs:
            board: The loaded board.
            layers: Enabled copper layers.
        Outputs:
            Mapping from uuid to (item, (net, active layers, geometry), active layers,
            bounding box (left, top, right, bottom) in nm).
        """
        import pcbnew
        from core.pcb_io import copper_width

        result = {}
        for item in (*board.GetTracks(), *board.GetPads()):
            net = str(item.GetNetname())
            active = frozenset(layer for layer in layers if item.IsOnLayer(layer))
            if isinstance(item, pcbnew.PCB_VIA):
                p = item.GetPosition()
                geometry = (
                    "via",
                    p.x,
                    p.y,
                    item.GetDrill(),
                    item.GetViaType(),
                    tuple(
                        (layer, copper_width(item, layer=layer))
                        for layer in sorted(active)
                    ),
                )
            elif isinstance(item, pcbnew.PAD):
                p, size = item.GetPosition(), item.GetSize()
                geometry = (
                    "protected_pad",
                    p.x,
                    p.y,
                    size.x,
                    size.y,
                    item.GetOrientationDegrees(),
                )
            elif isinstance(item, pcbnew.PCB_ARC):
                a, b, mid = item.GetStart(), item.GetEnd(), item.GetMid()
                geometry = (
                    "arc",
                    a.x,
                    a.y,
                    b.x,
                    b.y,
                    mid.x,
                    mid.y,
                    item.GetWidth(),
                )
            else:
                a, b = item.GetStart(), item.GetEnd()
                geometry = ("track", a.x, a.y, b.x, b.y, item.GetWidth())
            box = item.GetBoundingBox()
            result[item.m_Uuid.AsString()] = (
                item,
                (net, active, geometry),
                active,
                (box.GetX(), box.GetY(), box.GetRight(), box.GetBottom()),
            )
        return result

    @staticmethod
    def remote(value: tuple, touched: list) -> bool:
        """Say whether an item is clear of every touched item.

        Inputs:
            value: An inventory entry.
            touched: Inventory entries of the changed items.
        Outputs:
            True when no touched item on a shared layer lies within 1 mm of the item's
            bounding box.
        """
        _, _, active, (x0, y0, x1, y1) = value
        for _, _, other_layers, (a0, b0, a1, b1) in touched:
            if (
                active & other_layers
                and x0 <= a1 + 1000000
                and a0 <= x1 + 1000000
                and y0 <= b1 + 1000000
                and b0 <= y1 + 1000000
            ):
                return False
        return True

    @staticmethod
    def group(stable: dict, after) -> dict[str, str]:
        """Group stable items into connected regions of one net.

        Inputs:
            stable: Mapping from uuid to item.
            after: The loaded board, whose connectivity joins the items.
        Outputs:
            Mapping from uuid to the smallest uuid of its region.
        """
        connectivity = after.GetConnectivity()
        connectivity.Build(after)
        regions, pending = {}, set(stable)
        while pending:
            first = min(pending)
            pending.remove(first)
            queue = [first]
            while queue:
                uid = queue.pop()
                regions[uid] = first
                item = stable[uid]
                for other in (
                    *connectivity.GetConnectedTracks(item),
                    *connectivity.GetConnectedPads(item),
                ):
                    key = other.m_Uuid.AsString()
                    if key in pending and other.GetNetCode() == item.GetNetCode():
                        pending.remove(key)
                        queue.append(key)
        return regions

    @staticmethod
    def unchanged(before, after) -> dict[str, str]:
        """Group unchanged copper remote from edits on nets without zones.

        Callers must first verify protected footprint geometry and layer settings. A
        one-millimetre halo conservatively excludes every item near changed copper; this
        is only for identical native neck measurements, never new measurements.

        Inputs:
            before, after: The loaded boards before and after the edit.
        Outputs:
            Mapping from uuid to its region, for items on nets without zones or arcs.
        """
        import pcbnew

        layers = tuple(after.GetEnabledLayers().CuStack())
        zoned = {
            str(z.GetNetname())
            for board in (before, after)
            for z in board.Zones()
            if not z.GetIsRuleArea()
        }
        unsupported = {
            str(t.GetNetname())
            for board in (before, after)
            for t in board.GetTracks()
            if isinstance(t, pcbnew.PCB_ARC)
        }

        original = NeckRegions.inventory(before, layers)
        candidate = NeckRegions.inventory(after, layers)
        changed = {
            uid
            for uid in original.keys() | candidate.keys()
            if uid not in original
            or uid not in candidate
            or original[uid][1] != candidate[uid][1]
        }
        touched = [
            value
            for inventory_ in (original, candidate)
            for uid, value in inventory_.items()
            if uid in changed
        ]

        stable = {
            uid: value[0]
            for uid, value in candidate.items()
            if uid not in changed
            and value[1][0]
            and value[1][0] not in zoned | unsupported
            and NeckRegions.remote(value, touched)
        }
        return NeckRegions.group(stable, after)


class EditProofs:
    """Proofs that an edit changed only what it was meant to change."""

    @staticmethod
    def routing_style(before, after) -> None:
        """Use the shared semantic identities; existing defects still block completion.

        Inputs:
            before, after: Routing-style audits of the boards.
        Outputs:
            None. Raises ConnectivityRegression when the edit introduces a routing-style
            defect.
        """
        from pcb.net_routing.controller import new_style_issues

        issues = new_style_issues(before, after)
        if issues:
            raise ConnectivityRegression(
                "Detour introduced a routing-style defect: " + ", ".join(issues)
            )

    @staticmethod
    def tap_lines(block: str) -> set[str]:
        """The proven branch tap rules of a rule block.

        Inputs:
            block: Compiled rule text.
        Outputs:
            The stripped rule lines that name a tap.
        """
        return {
            line.strip()
            for line in block.splitlines()
            if line.lstrip().startswith("(rule ") and " tap " in line
        }

    @staticmethod
    def tap_proofs(before: str, after: str) -> None:
        """Added same-net contacts must not invalidate a published branch allowance.

        Inputs:
            before, after: Compiled rule text before and after the edit.
        Outputs:
            None. Raises ConnectivityRegression when a tap rule disappeared.
        """

        proofs = EditProofs.tap_lines
        if proofs(before) - proofs(after):
            raise ConnectivityRegression(
                "Width change invalidated an existing branch current proof"
            )

    @staticmethod
    def covered_edit(before: bytes, after: bytes, removed: set[str]) -> None:
        """Covered deletion must leave all retained copper and native fills exact.

        Inputs:
            before, after: Board bytes before and after the edit.
            removed: Uuids of the removed segments.
        Outputs:
            None. Raises ConnectivityRegression when retained copper or the filled zones
            changed.
        """
        from pcb.route_validation import _zone_refill_changes

        if EditProofs.copper_forms(after) != {
            uid: value
            for uid, value in EditProofs.copper_forms(before).items()
            if uid not in removed
        }:
            raise ConnectivityRegression("Covered cleanup changed retained copper")
        if _zone_refill_changes(before, after):
            raise ConnectivityRegression("Covered cleanup changed filled copper")

    @staticmethod
    def copper_forms(data: bytes) -> dict:
        """Canonical form of every track, via and arc of a board.

        Inputs:
            data: Board bytes.
        Outputs:
            Mapping from uuid to the canonical form.
        """
        from pcb.route_validation import _canonical

        root = parse(data.decode("utf-8-sig"))
        return {
            form.children("uuid")[0].atoms()[1].value: _canonical(form)
            for kind in ("segment", "via", "arc")
            for form in root.children(kind)
        }


class WidthBatches:
    """Chooses which widenings to keep when trial checks conflict."""

    @staticmethod
    def reduce_conflicts(
        targets: dict[str, int], minima: dict[str, int], conflicts: list[dict]
    ) -> tuple[dict[str, int], set[str]]:
        """Try the required minimum before deferring a conflicting widening.

        Inputs:
            targets: Mapping from uuid to the trial width, in nm.
            minima: Mapping from uuid to the required width, in nm.
            conflicts: New findings of the trial.
        Outputs:
            (revised targets, deferred uuids). A conflicting target above its minimum
            falls back to the minimum; one already at its minimum is deferred. Raises
            RuntimeError when no conflict names a target.
        """
        implicated = set()
        for finding in conflicts:
            hits = {item["uuid"] for item in finding.get("items", [])} & set(targets)
            implicated.update(hits)
        if not implicated:
            raise RuntimeError("A new finding is unattributed to the width edits")
        result, deferred = dict(targets), set()
        for uid in implicated:
            if result[uid] > minima[uid]:
                result[uid] = minima[uid]
            else:
                del result[uid]
                deferred.add(uid)
        return result, deferred

    @staticmethod
    def select(
        targets: dict[str, int],
        minima: dict[str, int],
        check,
        *,
        max_checks: int,
        progress=print,
    ) -> tuple[dict[str, int], set[str]]:
        """Keep accepted copper while partitioning interacting or obstructed edits.

        Inputs:
            targets: Mapping from uuid to the trial width, in nm.
            minima: Mapping from uuid to the required width, in nm.
            check: Callable that takes a proposal and returns its new findings, an empty
                list when it passes.
            max_checks: Most checks to run.
            progress: Callable that receives progress text.
        Outputs:
            (accepted widths, deferred uuids).
        """
        accepted, pending = {}, deque([dict(targets)])
        for iteration in range(max_checks):
            if not pending:
                break
            batch = pending.popleft()
            progress(
                f"Width batch {iteration + 1}: checking {len(batch)} edits, {len(accepted)} retained"
            )
            try:
                conflicts = check(accepted | batch)
            except ConnectivityRegression as error:
                progress(f"  Rejected trial: {error}")
                conflicts = None
            if conflicts == []:
                accepted.update(batch)
                progress(f"  Retained {len(accepted)} checked repairs")
                continue
            if conflicts:
                try:
                    revised, _ = WidthBatches.reduce_conflicts(batch, minima, conflicts)
                except RuntimeError:
                    revised = None
                if revised is not None:
                    if revised:
                        pending.appendleft(revised)
                    continue
            if any(width > minima[uid] for uid, width in batch.items()):
                pending.appendleft({uid: minima[uid] for uid in batch})
            elif len(batch) > 1:
                keys = sorted(batch)
                middle = len(keys) // 2
                pending.appendleft({uid: batch[uid] for uid in keys[middle:]})
                pending.appendleft({uid: batch[uid] for uid in keys[:middle]})
            progress(
                "  Separating interacting widenings; saved copper remains unchanged"
            )
        return accepted, set(targets) - set(accepted)


class WidthRepair:
    """One checked width repair of a project.

    Inputs:
        project: Folder of the KiCad project.
        apply: Save the checked board when True; only preview it when False.
        max_checks: Most native trial checks, 1 to 512.
        reroute, coordinated, prune_covered, trim_terminal_ends, series, ground_stitch,
            shift: The repair mode; at most one may be set.
        same_layer_series: With ``series``, rebuild runs on their own layer.
        seconds_per_route: Search budget per route, in seconds, up to 60.
    """

    def __init__(
        self,
        project: Path,
        *,
        apply: bool = False,
        max_checks: int = 16,
        reroute: bool = False,
        seconds_per_route: float = 5,
        prune_covered: bool = False,
        coordinated: bool = False,
        series: bool = False,
        ground_stitch: bool = False,
        same_layer_series: bool = False,
        shift: bool = False,
        trim_terminal_ends: bool = False,
    ):
        """Validate the budget and mode.

        Inputs:
            project: Folder of the KiCad project.
            apply: Save the checked board when True.
            max_checks: Most native trial checks, 1 to 512.
            reroute, coordinated, prune_covered, trim_terminal_ends, series,
                ground_stitch, shift: The repair mode; at most one may be set.
            same_layer_series: With ``series``, rebuild runs on their own layer.
            seconds_per_route: Search budget per route, in seconds; above 0 and up to
                60.
        Outputs:
            None. Raises ValueError for a flag that is not a bool, more than one mode,
            ``same_layer_series`` without ``series``, or a budget out of range.
        """
        modes = (
            reroute,
            prune_covered,
            coordinated,
            series,
            ground_stitch,
            shift,
            trim_terminal_ends,
        )
        if (
            any(
                not isinstance(flag, bool)
                for flag in (apply, same_layer_series, *modes)
            )
            or (same_layer_series and not series)
            or sum(modes) > 1
            or not 1 <= max_checks <= 512
            or not math.isfinite(seconds_per_route)
            or not 0 < seconds_per_route <= 60
        ):
            raise ValueError("Invalid width-repair budget")
        self.project = project.resolve()
        self.apply = apply
        self.max_checks = max_checks
        self.reroute = reroute
        self.seconds_per_route = seconds_per_route
        self.prune_covered = prune_covered
        self.coordinated = coordinated
        self.series = series
        self.ground_stitch = ground_stitch
        self.same_layer_series = same_layer_series
        self.shift = shift
        self.trim_terminal_ends = trim_terminal_ends
        self.removes_copper = prune_covered or trim_terminal_ends
        self.detours = reroute or coordinated or series or shift
        self.spatial_change = self.detours or self.removes_copper or ground_stitch
        self.ground_stitches = 0
        self.reference = None
        self.moving_nets = set()
        self.routed_candidate = None
        self.rejection_counts = Counter()

    @staticmethod
    def announce(text: str) -> None:
        """Print progress text immediately."""
        print(text, flush=True)

    @staticmethod
    def write(path, content) -> bool:
        """Write bytes to a file and report success to the save guard.

        Inputs:
            path: Target file.
            content: Bytes to write.
        Outputs:
            True.
        """
        Path(path).write_bytes(content)
        return True

    def inventory(self) -> None:
        """Fingerprint the inputs and measure the width findings of the saved board.

        Runs native DRC with refilled zones on the private board, then restores the
        original board bytes. Raises RuntimeError when the native save changes protected
        content.
        """
        import pcbnew
        from pcb.net_routing.protection import ProtectedState
        from pcb.route_validation import _drc

        snapshot = self.snapshot
        self.dependencies = dict(snapshot.fingerprints)
        self.dependencies[self.policy_path] = file_sha256(self.policy_path)
        for name in ("POWER_LOAD_BUDGET.csv", "STACKUP_CURRENT.json"):
            path = data_path(self.project, name)
            self.dependencies[path] = file_sha256(path)
        self.guard = BoardSaveGuard(snapshot.source, tuple(self.dependencies.items()))
        self.guard.verify_editor_unlocked()
        self.schematic = snapshot.source.with_suffix(".kicad_sch").read_bytes()
        self.protected = ProtectedState.capture(snapshot.original, self.schematic)
        print(
            "Inventorying all native width findings; saved board stays unchanged",
            flush=True,
        )
        self.baseline = _drc(
            self.cli,
            snapshot.board_path,
            snapshot.directory / "before.json",
            save_refill=True,
            schematic_parity=True,
        )
        self.filled_baseline = snapshot.board_path.read_bytes()
        self.filled_board = (
            pcbnew.LoadBoard(str(snapshot.board_path)) if self.prune_covered else None
        )
        self.flash_baseline = (
            CoveredCopper.plated_flashes(self.filled_board)
            if self.prune_covered
            else None
        )
        changes = self.protected.differences(
            snapshot.board_path.read_bytes(), self.schematic
        )
        if changes:
            raise RuntimeError(
                "Native save preflight changed protected content: " + ", ".join(changes)
            )
        snapshot.board_path.write_bytes(snapshot.original)

    def read_board(self) -> None:
        """Load the private board and derive the targets.

        Reads the required widths, the compiled branch rules, the high-speed nets and
        the recommended widths. Raises RuntimeError when the branch proofs are stale, no
        high-speed classification exists or a high-speed track needs a width change.
        """
        from pcb.branch_widths import RuleText, WidthRules
        from pcb.net_routing.native import load_board
        from pcb.net_routing.reference import high_speed_nets

        snapshot = self.snapshot
        self.minima = DrcFindings.required_widths(self.baseline)
        self.board = board = load_board(snapshot.board_path)
        self.bound_rules, _ = WidthRules.compile(board, self.policy)
        saved_rules = snapshot.board_path.with_suffix(".kicad_dru").read_text(
            encoding="utf-8"
        )
        if RuleText.replace_managed(saved_rules, self.bound_rules) != saved_rules:
            raise RuntimeError(
                "Branch proofs are stale; run pcb branch-widths --apply first"
            )
        self.tracks = {track.m_Uuid.AsString(): track for track in board.GetTracks()}
        self.critical = high_speed_nets(
            snapshot.board_path.with_suffix(".kicad_pro").read_bytes(),
            {str(net) for net in board.GetNetsByName() if str(net)},
        )
        if not self.critical:
            raise RuntimeError("USB/Ethernet classification is unavailable")
        if any(self.tracks[uid].GetNetname() in self.critical for uid in self.minima):
            raise RuntimeError(
                "High-speed width changes require impedance-qualified pair routing"
            )
        recommendations = {
            rail["net"]: rail.get("recommended_width_mm", rail["width_mm"])
            for rail in self.policy.get("rails", [])
        }
        zoned = {
            str(zone.GetNetname()) for zone in board.Zones() if not zone.GetIsRuleArea()
        }
        self.stable_necks = frozenset(
            str(net)
            for net in board.GetNetsByName()
            if str(net) and str(net) not in zoned
        )
        self.unchanged_necks = frozenset(
            str(net) for net in board.GetNetsByName() if str(net)
        )
        self.targets = {
            uid: max(
                width,
                round(recommendations.get(self.tracks[uid].GetNetname(), 0) * 1e6),
            )
            for uid, width in self.minima.items()
        }

    def select_targets(self) -> None:
        """Narrow the targets for the removal and ground-stitch modes.

        Covered copper, redundant terminal tips or ground-stitch candidates replace the
        default targets. Raises ValueError when a ground-stitch run starts with open
        connections.
        """
        import pcbnew
        from pcb.route_validation import _drc, _drc_native

        snapshot, minima, tracks = self.snapshot, self.minima, self.tracks
        if self.prune_covered:
            eligible = {
                uid for uid in minima if tracks[uid].GetNetname() not in self.critical
            }
            self.targets = {
                uid: minima[uid]
                for uid in CoveredCopper.tracks(self.filled_board, eligible)
            }
            print(
                f"{len(self.targets)} flagged segments lie entirely in retained copper",
                flush=True,
            )
        if self.trim_terminal_ends:
            self.targets = {
                uid: minima[uid]
                for uid in CoveredCopper.terminal_ends(
                    self.board, set(minima), self.bound_rules
                )
            }
            print(
                f"{len(self.targets)} flagged terminal tips have an independent retained pad contact",
                flush=True,
            )
            if self.targets:
                self.stored_baseline = _drc_native(
                    self.cli,
                    snapshot.board_path,
                    snapshot.directory / "stored-before.json",
                    schematic_parity=True,
                    refill_zones=False,
                )
                self.stored_output = snapshot.directory / "stored-after.json"
        if self.ground_stitch:
            if self.baseline["unconnected_items"]:
                raise ValueError(
                    "Ground-stitch width repair starts from a connected board"
                )
            self.route_policy = json.loads(
                (self.assets / "config/autopcb.json").read_bytes()
            )
            self.ground_ids = {
                item.m_Uuid.AsString()
                for item in (
                    *self.board.GetPads(),
                    *self.board.GetTracks(),
                    *self.board.Zones(),
                )
                if str(item.GetNetname()) == "GND"
            }
            eligible = {
                uid: value
                for uid, value in minima.items()
                if value <= 1500000
                and tracks[uid].Type() == pcbnew.PCB_TRACE_T
                and not tracks[uid].IsLocked()
                and tracks[uid].GetParentGroup() is None
            }
            snapshot.board_path.write_bytes(
                WidthEdit.candidate(snapshot.directory, eligible)
            )
            atlas = _drc(
                self.cli,
                snapshot.board_path,
                snapshot.directory / "width-stitch-atlas.json",
                schematic_parity=True,
            )
            clear = CoupledWidening.neighbors(atlas, set(eligible), set())
            self.targets = {uid: eligible[uid] for uid in clear}
            snapshot.board_path.write_bytes(snapshot.original)
            print(
                f"{len(self.targets)} candidate widenings clear copper and drill spacing",
                flush=True,
            )

    def prepare_checks(self) -> None:
        """Create the files and the style baseline that the trial checks use."""
        from pcb.net_routing.style import audit_style

        snapshot = self.snapshot
        self.output = snapshot.directory / "after.json"
        self.prior = snapshot.directory / "width-prior.kicad_pcb"
        self.prior.write_bytes(snapshot.original)
        self.original_style = (
            audit_style(self.board)
            if self.detours or self.ground_stitch or self.trim_terminal_ends
            else None
        )

    def check_covered(self, removed) -> None:
        """Check that a covered-copper deletion kept copper, fills and plated lands exact.

        Inputs:
            removed: Uuids of the removed segments.
        Outputs:
            None. Raises ConnectivityRegression when anything retained changed.
        """
        import pcbnew

        EditProofs.covered_edit(
            self.filled_baseline,
            self.snapshot.board_path.read_bytes(),
            set(removed),
        )
        if (
            CoveredCopper.plated_flashes(
                pcbnew.LoadBoard(str(self.snapshot.board_path))
            )
            != self.flash_baseline
        ):
            raise ConnectivityRegression("Covered cleanup changed a plated land")

    def rejoin_ground(self, candidate: bytes, after: dict) -> tuple:
        """Rejoin ground islands that a widening disconnected.

        Inputs:
            candidate: Board bytes of the widened trial.
            after: Native DRC report of the trial, with its open connections.
        Outputs:
            (new DRC report, board with the stitches). Raises ConnectivityRegression
            when a disconnection lies outside the ground net or no stitch fits, and
            RuntimeError when stitching changes protected content, retained routing or
            adds copper that is not ground.
        """
        from pcb.branch_widths import WidthRules
        from pcb.net_routing.fast_rework import prepare_ground_access
        from pcb.net_routing.isolation import run_native
        from pcb.net_routing.native import load_board
        from pcb.net_routing.style import audit_style
        from pcb.route_validation import _drc

        snapshot = self.snapshot
        missing = after["unconnected_items"]
        if not all(
            entry.get("items")
            and all(item.get("uuid") in self.ground_ids for item in entry["items"])
            for entry in missing
        ):
            raise ConnectivityRegression(
                "Widening disconnected copper outside the ground net"
            )
        stitch = run_native(
            prepare_ground_access, snapshot.directory, self.route_policy
        )
        if not stitch["proposed_stitches"]:
            raise ConnectivityRegression("No ground stitch fit the affected island")
        revised = snapshot.board_path.read_bytes()
        if self.protected.differences(revised, self.schematic):
            raise RuntimeError("Ground stitching changed protected content")
        before_stitch, after_stitch = (
            EditProofs.copper_forms(candidate),
            EditProofs.copper_forms(revised),
        )
        if any(
            after_stitch.get(uid) != record for uid, record in before_stitch.items()
        ):
            raise RuntimeError("Ground stitching changed retained routing")
        trial_board = load_board(snapshot.board_path)
        added = [
            item
            for item in trial_board.GetTracks()
            if item.m_Uuid.AsString() not in before_stitch
        ]
        if not added or any(str(item.GetNetname()) != "GND" for item in added):
            raise RuntimeError("Ground stitching added unexpected copper")
        trial_rules, _ = WidthRules.compile(trial_board, self.policy)
        EditProofs.tap_proofs(self.bound_rules, trial_rules)
        EditProofs.routing_style(self.original_style, audit_style(trial_board))
        self.output.unlink()
        after = _drc(self.cli, snapshot.board_path, self.output, schematic_parity=True)
        self.ground_stitches = stitch["proposed_stitches"]
        print(
            f"  Rejoined {stitch['proposed_stitches']} ground island(s) on the private board",
            flush=True,
        )
        return after, trial_board

    def check_trimmed_tips(self, proposal, candidate: bytes) -> set:
        """Check a terminal-tip removal against the stored fills.

        Inputs:
            proposal: Uuids of the removed tips.
            candidate: Board bytes of the trial.
        Outputs:
            The nets that the removal changed. Raises ConnectivityRegression when
            retained copper changed or native DRC with the stored fills finds anything
            new.
        """
        from pcb.route_validation import _drc_native

        snapshot = self.snapshot
        EditProofs.covered_edit(snapshot.original, candidate, set(proposal))
        changed_nets = {str(self.tracks[uid].GetNetname()) for uid in proposal}
        self.stored_output.unlink(missing_ok=True)
        stored_after = _drc_native(
            self.cli,
            snapshot.board_path,
            self.stored_output,
            schematic_parity=True,
            refill_zones=False,
        )
        if DrcFindings.introduced(
            self.stored_baseline,
            stored_after,
            stable_neck_nets=self.unchanged_necks - changed_nets,
        ):
            raise ConnectivityRegression(
                "Terminal cleanup introduced a finding with the stored fills"
            )
        return changed_nets

    def check_reference(self, proposal) -> list:
        """Check that the edit did not degrade the sampled reference copper.

        Inputs:
            proposal: The edited uuids, or the candidate board bytes.
        Outputs:
            An empty list when the reference holds; otherwise findings that name the
            edited tracks that block it. Raises ConnectivityRegression when the
            degradation is not attributable to an edited track.
        """
        from pcb.net_routing.native import load_board
        from pcb.net_routing.reference import reference_conflicts
        from pcb.net_routing.runner import _verify_cumulative_reference_native

        snapshot = self.snapshot
        reference = _verify_cumulative_reference_native(
            self.prior, snapshot.board_path, tuple(sorted(self.critical))
        )
        if reference["status"] == "degraded":
            widened = load_board(snapshot.board_path)
            sources = set(reference["degraded_track_ids"])
            edited = (
                set(proposal)
                if not isinstance(proposal, bytes)
                else {
                    t.m_Uuid.AsString()
                    for t in widened.GetTracks()
                    if t.m_Uuid.AsString() not in self.tracks
                }
            )
            hits = {
                c.blocking_item_id
                for c in reference_conflicts(widened, self.critical)
                if c.high_speed_item_id in sources
            } & edited
            if not hits:
                raise ConnectivityRegression(
                    "Width edit degraded unattributed reference copper"
                )
            return [{"items": [{"uuid": uid}]} for uid in hits]
        self.reference = reference
        return []

    def check(self, proposal):
        """Run every trial check on one proposal.

        Writes the proposal to the private board, checks protected items, branch proofs,
        routing style, native DRC against the baseline, the mode-specific proofs and the
        reference copper.

        Inputs:
            proposal: Mapping of uuid to width, a collection of uuids to remove, or
                candidate board bytes.
        Outputs:
            An empty list when the proposal passes, otherwise the new findings. Raises
            ConnectivityRegression for a rejected trial and RuntimeError when a
            protected item changes.
        """
        from pcb.branch_widths import WidthRules
        from pcb.net_routing.native import load_board
        from pcb.net_routing.style import audit_style
        from pcb.route_validation import _drc

        snapshot = self.snapshot
        snapshot.board_path.write_bytes(snapshot.original)
        self.ground_stitches = 0
        if isinstance(proposal, bytes):
            candidate = proposal
        elif self.removes_copper:
            candidate = (
                CampaignPlanner(snapshot.directory)
                .plan(
                    "pcb",
                    [
                        {
                            "op": "remove",
                            "target": {"kind": "segment", "uuid": uid},
                            "path": [],
                        }
                        for uid in sorted(proposal)
                    ],
                )
                .after
            )
        else:
            candidate = WidthEdit.candidate(snapshot.directory, proposal)
        snapshot.board_path.write_bytes(candidate)
        if self.protected.differences(candidate, self.schematic):
            raise RuntimeError("Width repair altered a protected item")
        trial_board = None
        if self.spatial_change:
            trial_board = load_board(snapshot.board_path)
            trial_rules, _ = WidthRules.compile(trial_board, self.policy)
            EditProofs.tap_proofs(self.bound_rules, trial_rules)
            # Covered cleanup proves the same physical conductor below.
            # Its deleted labels in the centerline graph can change bend attribution.
            if self.detours or self.trim_terminal_ends:
                EditProofs.routing_style(self.original_style, audit_style(trial_board))
        self.output.unlink(missing_ok=True)
        after = _drc(
            self.cli,
            snapshot.board_path,
            self.output,
            schematic_parity=True,
            save_refill=self.prune_covered,
        )
        if self.ground_stitch and after["unconnected_items"]:
            after, trial_board = self.rejoin_ground(candidate, after)
        if self.prune_covered:
            self.check_covered(proposal)
        if self.trim_terminal_ends:
            changed_nets = self.check_trimmed_tips(proposal, candidate)
        else:
            changed_nets = self.moving_nets
        necks = (
            self.unchanged_necks
            if self.prune_covered
            else self.stable_necks - changed_nets
        )
        conflicts = DrcFindings.introduced(
            self.baseline,
            after,
            stable_neck_nets=necks,
            neck_regions=NeckRegions.unchanged(self.board, trial_board)
            if self.detours
            else None,
            include_widths=self.spatial_change,
        )
        if conflicts:
            kinds = Counter(v.get("type", "reference_copper") for v in conflicts)
            print("  New conflicts:", dict(kinds), flush=True)
            return conflicts
        if trial_board is None:
            trial_board = load_board(snapshot.board_path)
            trial_rules, _ = WidthRules.compile(trial_board, self.policy)
            EditProofs.tap_proofs(self.bound_rules, trial_rules)
        return self.check_reference(proposal)

    def coordinated_groups(self) -> dict:
        """Probe which movable neighbours each widening obstructs.

        This only schedules trials; every edit is checked afterwards from a clean
        private copy.

        Outputs:
            Mapping from the uuid of each widened track to its neighbour uuids.
        """
        import pcbnew
        from pcb.route_validation import _drc

        snapshot, minima = self.snapshot, self.minima
        groups = {}
        # Probe moderate rises together; this only schedules trials.
        # Every selected edit is then checked from a clean private copy.
        probe = {uid: width for uid, width in minima.items() if width <= 1500000}
        movable = {
            uid
            for uid, item in self.tracks.items()
            if item.Type() == pcbnew.PCB_TRACE_T
            and not item.IsLocked()
            and item.GetParentGroup() is None
            and item.GetNetname() not in self.critical
            and (
                uid in minima
                or item.GetNetClassName() in {"CORE_SIGNAL", "FIELD_SIGNAL"}
            )
        }
        if probe:
            snapshot.board_path.write_bytes(
                WidthEdit.candidate(snapshot.directory, probe)
            )
            atlas = _drc(
                self.cli,
                snapshot.board_path,
                snapshot.directory / "width-obstacles.json",
                schematic_parity=True,
            )
            groups = CoupledWidening.neighbors(atlas, set(probe) & movable, movable)
        snapshot.board_path.write_bytes(snapshot.original)
        return groups

    def detour_trials(self, groups: dict) -> list:
        """List the trials of the detour modes in the order they are tried.

        Inputs:
            groups: Neighbours per track from ``coordinated_groups``.
        Outputs:
            A list of (uuid, width in nm, site), where the site is a layer name, a (dx,
            dy) shift in nm, or None.
        """
        from pcb.net_routing.isolation import run_native

        snapshot, minima = self.snapshot, self.minima
        if self.series:
            from pcb.route_native import _configured_copper_thicknesses

            trials = SeriesRuns.trials(
                self.board,
                minima,
                dict(_configured_copper_thicknesses(snapshot.original)),
                same_layer=self.same_layer_series,
            )
            print(
                f"{len(trials)} eligible trials for complete terminal runs",
                flush=True,
            )
        elif self.shift:
            trials = run_native(ShiftedWidening.sites, snapshot.directory, minima)
            print(
                f"{len({uid for uid, _, _ in trials})} traces have candidate lateral shifts",
                flush=True,
            )
        else:
            ordered = sorted(
                groups if self.coordinated else minima,
                key=lambda uid: (
                    len(groups.get(uid, ())),
                    minima[uid],
                    -self.tracks[uid].GetLength(),
                    uid,
                ),
            )
            trials = [(uid, minima[uid], None) for uid in ordered]
        return trials

    def detour_trial(
        self, index: int, shown: int, uid: str, minimum: int, site, groups: dict
    ) -> None:
        """Plan, check and record one detour, shift or coordinated trial.

        Inputs:
            index: Position of the trial in the schedule.
            shown: Number of scheduled trials shown in the progress text.
            uid: Uuid of the track to repair.
            minimum: Width the track needs, in nm.
            site: The layer or shift of the trial, or None.
            groups: Neighbours per track from ``coordinated_groups``.
        Outputs:
            None. An accepted trial updates the accepted widths, the retained nets and
            the routed candidate; a rejected one is counted in the blockers.
        """
        from pcb.net_routing.isolation import run_native

        snapshot, minima, tracks = self.snapshot, self.minima, self.tracks
        if uid in self.accepted:
            return
        snapshot.board_path.write_bytes(self.routed_candidate or snapshot.original)
        net = str(tracks[uid].GetNetname())
        method = (
            "Coordinated width"
            if self.coordinated
            else "Shifted width"
            if self.shift
            else "Detour"
        )
        location = f" to {site}" if site and not self.shift else ""
        print(
            f"{method} {index + 1}/{shown}: {net}{location}; "
            f"{len(self.accepted)} retained",
            flush=True,
        )
        self.moving_nets = self.retained_nets | {net}
        if self.coordinated:
            neighbors = {
                key: max(tracks[key].GetWidth(), minima.get(key, 0))
                for key in groups[uid]
            }
            self.moving_nets.update(str(tracks[key].GetNetname()) for key in neighbors)
            candidate = run_native(
                CoupledWidening.plan,
                snapshot.directory,
                uid,
                minima[uid],
                neighbors,
                self.seconds_per_route,
            )
        elif self.shift:
            candidate = run_native(
                ShiftedWidening.plan,
                snapshot.directory,
                uid,
                minimum,
                site,
            )
        else:
            candidate = run_native(
                WidthReroute.plan,
                snapshot.directory,
                uid,
                minimum,
                self.seconds_per_route,
                target_layer=site,
                same_layer_series=self.same_layer_series,
            )
        if candidate is None:
            self.moving_nets = self.retained_nets.copy()
            reason = (
                "shift_did_not_preserve_contacts" if self.shift else "no_sampled_detour"
            )
            self.rejection_counts[reason] += 1
            print(
                "  Shift could not keep all original contacts"
                if self.shift
                else "  No sampled rule-aware detour; feasibility remains unknown",
                flush=True,
            )
            return
        try:
            conflicts = self.check(candidate)
        except ConnectivityRegression as error:
            conflicts = [str(error)]
        if conflicts:
            self.moving_nets = self.retained_nets.copy()
            reasons = Counter(
                value.get("type", "native_reference")
                if isinstance(value, dict)
                else value
                for value in conflicts
            )
            self.rejection_counts.update(reasons)
            print("  Rejected detour: " + "; ".join(reasons), flush=True)
            return
        remaining = DrcFindings.required_widths(json.loads(self.output.read_bytes()))
        if uid in remaining:
            self.moving_nets = self.retained_nets.copy()
            self.rejection_counts["required_width_unchanged"] += 1
            return
        self.accepted.update(
            {key: minima[key] for key in minima.keys() - remaining.keys()}
        )
        self.routed_candidate = candidate
        self.retained_nets = self.moving_nets.copy()
        print(f"  Retained {len(self.accepted)} checked detours", flush=True)

    def search_detours(self) -> tuple:
        """Try the scheduled trials and keep the accepted ones.

        Outputs:
            (accepted widths, deferred uuids).
        """
        minima = self.minima
        self.accepted, self.moving_nets, self.retained_nets = {}, set(), set()
        groups = self.coordinated_groups() if self.coordinated else {}
        trials = self.detour_trials(groups)
        shown = min(self.max_checks, len(trials))
        for index, (uid, minimum, site) in enumerate(trials[: self.max_checks]):
            self.detour_trial(index, shown, uid, minimum, site, groups)
        return self.accepted, set(minima) - set(self.accepted)

    def verify_final(self, targets: dict) -> tuple:
        """Revalidate the retained batch and its native round trip.

        Inputs:
            targets: The retained widths or uuids.
        Outputs:
            (final DRC report, board bytes). Raises RuntimeError when the batch does not
            revalidate, native width violations are not reduced, protected content
            changes, the round trip introduces a finding, or the branch targets changed.
        """
        from pcb.net_routing.native import load_board
        from pcb.route_validation import _drc

        snapshot, minima = self.snapshot, self.minima
        # A budget exit may follow a rejected trial. Rebuild the accepted state.
        if self.check(self.routed_candidate or targets):
            raise RuntimeError("The retained width batch did not revalidate")
        after = json.loads(self.output.read_bytes())
        candidate = snapshot.board_path.read_bytes()
        remaining = DrcFindings.required_widths(after)
        if set(remaining) & set(targets) or len(remaining) >= len(minima):
            raise RuntimeError(
                "Width repair did not independently reduce native width violations"
            )
        if self.protected.differences(candidate, self.schematic):
            raise RuntimeError("Width repair altered a protected item")
        # Native DRC refills privately. A terminal tip on a net without zones
        # does not require replacing the existing saved fills.
        self.output.unlink(missing_ok=True)
        final = _drc(
            self.cli,
            snapshot.board_path,
            self.output,
            save_refill=not self.trim_terminal_ends,
            schematic_parity=True,
        )
        if self.prune_covered:
            self.check_covered(targets)
        if self.trim_terminal_ends:
            EditProofs.covered_edit(
                snapshot.original, snapshot.board_path.read_bytes(), set(targets)
            )
        if self.protected.differences(snapshot.board_path.read_bytes(), self.schematic):
            raise RuntimeError("Native round trip altered protected content")
        final_changed_nets = (
            {str(self.tracks[uid].GetNetname()) for uid in targets}
            if self.trim_terminal_ends
            else self.moving_nets
        )
        if DrcFindings.introduced(
            self.baseline,
            final,
            stable_neck_nets=self.unchanged_necks
            if self.prune_covered
            else self.stable_necks - final_changed_nets,
            neck_regions=NeckRegions.unchanged(
                self.board, load_board(snapshot.board_path)
            )
            if self.detours
            else None,
            include_widths=self.spatial_change,
        ):
            raise RuntimeError("Final width-repair round trip introduced a finding")
        candidate = snapshot.board_path.read_bytes()
        changes = self.protected.differences(candidate, self.schematic)
        if changes:
            raise RuntimeError(
                "Native round trip altered protected content: " + ", ".join(changes)
            )
        snapshot.check()
        if self.policy_path.read_bytes() != self.policy_bytes:
            raise RuntimeError("Branch targets changed during width repair")
        return final, candidate

    def mode(self) -> str:
        """Name the repair mode for the report."""
        return (
            "coordinated"
            if self.coordinated
            else "covered_copper_removed"
            if self.prune_covered
            else "redundant_terminal_ends_removed"
            if self.trim_terminal_ends
            else "width_with_ground_stitch"
            if self.ground_stitch
            else "terminal_series"
            if self.same_layer_series
            else "series_relocated"
            if self.series
            else "shifted"
            if self.shift
            else "rerouted"
            if self.reroute
            else "width_only"
        )

    def report(self, targets: dict, deferred: set, final: dict) -> dict:
        """Build the report of a checked repair.

        Inputs:
            targets: The retained widths or uuids.
            deferred: Uuids not repaired.
            final: Final native DRC report.
        Outputs:
            The report with status ``preview_checked``, counts, blockers, the reference
            check and the check time in UTC.
        """
        return {
            "status": "preview_checked",
            "changed_tracks": len(targets),
            "mode": self.mode(),
            "ground_stitches": self.ground_stitches,
            "blockers": dict(self.rejection_counts),
            "width_errors_before": len(self.minima),
            "width_errors_after": len(DrcFindings.required_widths(final)),
            "deferred_tracks": len(deferred),
            "open_connections": len(final["unconnected_items"]),
            "deferred_uuids": sorted(deferred),
            "reference": self.reference,
            "manufacturing_approved": False,
            "checked_utc": datetime.now(timezone.utc).isoformat(),
        }

    def save_journal(self, report: dict, tools: dict) -> None:
        """Record the pre-save checkpoint before the board is replaced.

        Inputs:
            report: The checked report.
            tools: Fingerprints of the routing tools.
        Outputs:
            None. Writes ``working-save.json`` under the project's autoroute preimages.
        """
        from pcb.net_routing.controller import encode_checkpoint
        from pcb.net_routing.runner import _save

        inputs = {
            path.relative_to(self.project).as_posix(): digest
            for path, digest in self.dependencies.items()
        }
        checkpoint = encode_checkpoint(
            self.snapshot.original,
            inputs,
            tools,
            self.dependencies[self.policy_path],
        )
        journal = {
            "kind": "pre_width_repair_save",
            "created_utc": report["checked_utc"],
            "manufacturing_approved": False,
            "board_before": checkpoint,
        }
        _save(
            self.assets / "results" / "autoroute" / "preimages" / "working-save.json",
            journal,
        )

    def run(self) -> dict:
        """Retain a source-bound batch only after independent native closing checks.

        Outputs:
            The report. ``status`` is ``obstructed`` when nothing could be repaired,
            ``preview_checked`` for a preview and ``saved_width_repairs`` after --apply,
            with the track counts, the native width errors before and after and the open
            connections. Raises ValueError when the branch policy claims electrical
            verification, and RuntimeError when a check fails, a protected item changes
            or the tools change before saving.
        """
        import pcbnew
        from pcb.net_routing.native import Snapshot
        from pcb.net_routing.runner import _exclusive, _save

        project = self.project
        self.assets = assets = asset_root(project)
        self.policy_path = assets / "config" / "branch_widths.json"
        self.policy_bytes = self.policy_path.read_bytes()
        self.policy = json.loads(self.policy_bytes)
        if self.policy.get("electrically_verified") is not False:
            raise ValueError(
                "Width targets must retain their unqualified electrical status"
            )
        self.cli = kicad_cli()
        with _exclusive(assets / "results" / "autoroute" / "active.lock"):
            with Snapshot(native_path(project, ".kicad_pcb")) as snapshot:
                self.snapshot = snapshot
                self.inventory()
                self.read_board()
                self.select_targets()
                self.prepare_checks()
                if self.detours:
                    targets, deferred = self.search_detours()
                else:
                    targets, deferred = WidthBatches.select(
                        self.targets,
                        self.minima,
                        self.check,
                        max_checks=self.max_checks,
                        progress=self.announce,
                    )
                    if self.removes_copper or self.ground_stitch:
                        deferred = set(self.minima) - set(targets)
                if not targets:
                    return {
                        "status": "obstructed",
                        "changed_tracks": 0,
                        "deferred_tracks": len(deferred),
                        "blockers": dict(self.rejection_counts),
                    }
                final, candidate = self.verify_final(targets)
                report = self.report(targets, deferred, final)
                tools = dict(snapshot.tool_hashes)
                if self.apply:
                    self.save_journal(report, tools)
            if self.apply:
                if Snapshot.tools() != tools:
                    raise RuntimeError("Routing tools changed before saving")
                self.guard.save(
                    candidate, self.guard.source, self.write, pcbnew.LoadBoard
                )
                report["status"] = "saved_width_repairs"
                report["pcb_sha256"] = file_sha256(self.guard.source)
                _save(assets / "results" / "width_repair.json", report)
            return report


class WidthRepairCommand:
    """Command-line entry that runs one checked width repair."""

    def __call__(self, argv=None) -> int:
        """Parse the command line, run the repair and print its report.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0.
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--project", required=True, type=Path)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--max-checks", type=int, default=16)
        parser.add_argument(
            "--reroute",
            action="store_true",
            help="Try rule-aware detours instead of centerline widening",
        )
        parser.add_argument(
            "--coordinated",
            action="store_true",
            help="Widen traces while rerouting eligible obstructing neighbors on their existing layers",
        )
        parser.add_argument(
            "--prune-covered",
            action="store_true",
            help="Remove flagged segments fully covered by retained pads, tracks or freshly filled copper",
        )
        parser.add_argument(
            "--trim-terminal-ends",
            action="store_true",
            help="Remove tiny redundant tips where a proven sense branch already contacts its pad",
        )
        parser.add_argument(
            "--series",
            action="store_true",
            help="Widen complete via-terminated runs on eligible alternate layers",
        )
        parser.add_argument(
            "--same-layer",
            action="store_true",
            help="With --series, rebuild complete pad/via runs on their original layer",
        )
        parser.add_argument(
            "--ground-stitch",
            action="store_true",
            help="Widen unobstructed traces and rejoin any affected ground islands",
        )
        parser.add_argument(
            "--shift",
            action="store_true",
            help="Shift straight traces laterally while retaining all original copper contacts",
        )
        parser.add_argument("--seconds-per-route", type=float, default=5)
        args = parser.parse_args(argv)
        with quiet_duplicate_image_handlers():
            result = WidthRepair(
                args.project,
                apply=args.apply,
                max_checks=args.max_checks,
                reroute=args.reroute,
                seconds_per_route=args.seconds_per_route,
                prune_covered=args.prune_covered,
                coordinated=args.coordinated,
                series=args.series,
                ground_stitch=args.ground_stitch,
                same_layer_series=args.same_layer,
                shift=args.shift,
                trim_terminal_ends=args.trim_terminal_ends,
            ).run()
        print("Width repair:", result["status"])
        for name in (
            "changed_tracks",
            "deferred_tracks",
            "width_errors_before",
            "width_errors_after",
            "open_connections",
            "ground_stitches",
        ):
            if name in result:
                print(f"  {name.replace('_', ' ')}: {result[name]}")
        if result.get("blockers"):
            print(
                "  Blockers:",
                ", ".join(
                    f"{name}={count}"
                    for name, count in sorted(result["blockers"].items())
                ),
            )
        print(
            "Normal-current targets; electrical, fault-energy and manufacturing qualification remain open."
        )
        return 0


main = WidthRepairCommand()

if __name__ == "__main__":
    raise SystemExit(main())
