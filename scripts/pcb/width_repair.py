"""Repair track-width findings in checked batches of ordinary copper."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal
from itertools import product
import json
import math
from pathlib import Path
import re
import sys
import tempfile
import time

from core.kicad_campaign import CampaignPlanner
from core.kicad_sexpr import parse
from core.paths import asset_root
from core.pcb_io import BoardSaveGuard, copper_boundary, file_sha256, quiet_duplicate_image_handlers, typed_copper


class ConnectivityRegression(RuntimeError):
    """A width increase disconnected copper after a fresh zone fill."""


def verify_routing_style(before, after) -> None:
    """Use the shared semantic identities; existing defects still block completion."""
    from pcb.net_routing.controller import new_style_issues

    issues = new_style_issues(before, after)
    if issues:
        raise ConnectivityRegression('Detour introduced a routing-style defect: ' + ', '.join(issues))


def width_chain(board, track, *, maximum: int = 32):
    """Extend a uniform-width series run, stopping at pads, vias and branches."""
    import pcbnew
    from core.pcb_io import copper_width
    from pcb.branch_widths import contact_port

    connectivity = board.GetConnectivity()
    connectivity.Build(board)
    net, layer, width = track.GetNetCode(), track.GetLayer(), track.GetWidth()
    grouped = {x.m_Uuid.AsString() for group in board.Groups() for x in group.GetItems()}
    path = deque([(track, track.GetStart(), track.GetEnd())])
    selected = {track.m_Uuid.AsString()}
    for left in (True, False):
        while len(path) < maximum:
            item, start, end = path[0 if left else -1]
            point = start if left else end
            if any(p.GetNetCode() == net and p.IsOnLayer(layer) and p.HitTest(point)
                   for p in connectivity.GetConnectedPads(item)):
                break
            neighbors = []
            ambiguous = False
            for neighbor in map(typed_copper, connectivity.GetConnectedTracks(item)):
                if neighbor.m_Uuid == item.m_Uuid or not neighbor.IsOnLayer(layer):
                    continue
                if isinstance(neighbor, pcbnew.PCB_VIA):
                    pos = neighbor.GetPosition()
                    if ((pos.x-point.x)**2 + (pos.y-point.y)**2
                            <= (copper_width(neighbor, layer=layer)/2)**2):
                        ambiguous = True
                    continue
                port = contact_port(item, neighbor)
                if port is None:
                    ambiguous = True
                elif port == (point.x, point.y):
                    neighbors.append(neighbor)
            if ambiguous or len(neighbors) != 1:
                break
            neighbor = neighbors[0]
            uid = neighbor.m_Uuid.AsString()
            if (uid in selected or isinstance(neighbor, pcbnew.PCB_ARC)
                    or neighbor.GetNetCode() != net or neighbor.GetWidth() != width
                    or neighbor.IsLocked() or uid in grouped):
                break
            other = neighbor.GetEnd() if neighbor.GetStart() == point else neighbor.GetStart()
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


def route_headings(board, track, *, removed_ids=frozenset(), endpoints=None):
    """Constrain new ends against a single fixed neighbor, without constraining branches."""
    import pcbnew
    from core.pcb_io import copper_width
    from pcb.route_optimizer import DIRECTIONS

    net, layer = track.GetNetCode(), track.GetLayer()
    omitted = removed_ids | {track.m_Uuid.AsString()}
    copper = [x for x in board.GetTracks() if x.m_Uuid.AsString() not in omitted
              and x.GetNetCode() == net and x.IsOnLayer(layer)]
    result = []
    a, b = endpoints or (track.GetStart(), track.GetEnd())
    for start, point in ((True, a), (False, b)):
        anchored = any(p.GetNetCode() == net and p.IsOnLayer(layer) and p.HitTest(point) for p in board.GetPads())
        anchored |= any(isinstance(x, pcbnew.PCB_VIA) and
                        (x.GetPosition().x - point.x) ** 2 + (x.GetPosition().y - point.y) ** 2
                        <= (copper_width(x, layer=layer) / 2) ** 2 for x in copper)
        neighbors = [x for x in copper if not isinstance(x, (pcbnew.PCB_VIA, pcbnew.PCB_ARC))
                     and point in (x.GetStart(), x.GetEnd())]
        if anchored or len(neighbors) != 1:
            result.append(None)
            continue
        neighbor = neighbors[0]
        other = neighbor.GetEnd() if neighbor.GetStart() == point else neighbor.GetStart()
        dx, dy = ((point.x - other.x, point.y - other.y) if start
                  else (other.x - point.x, other.y - point.y))
        if not (dx or dy) or (dx and dy and abs(dx) != abs(dy)):
            result.append(None)
            continue
        direction = DIRECTIONS.index(((dx > 0) - (dx < 0), (dy > 0) - (dy < 0)))
        result.append(frozenset((direction, (direction - 1) % 8, (direction + 1) % 8)))
    return tuple(result)


def repair_layers(board, chain, a, b, copper: dict[str, int], *, connection=None) -> tuple[str, ...]:
    """Allow only series-end transitions that shorten both existing via paths."""
    import pcbnew
    from core.pcb_io import copper_width

    source = chain[0].GetLayer()
    name = str(board.GetLayerName(source))
    original = (name,)
    net = chain[0].GetNetCode()
    if copper.get(name, 0) <= 0 or any(z.GetNetCode() == net and not z.GetIsRuleArea() for z in board.Zones()):
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
        candidates = [v for v in board.GetTracks() if isinstance(v, pcbnew.PCB_VIA)
                      and v.GetNetCode() == net and v.IsOnLayer(source) and v.GetPosition() == point]
        if len(candidates) != 1:
            return original
        via = candidates[0]
        if via.GetViaType() != pcbnew.VIATYPE_THROUGH or via.GetDrill() <= 0:
            return original
        tails = [t for t in map(typed_copper, connection.GetConnectedTracks(via))
                 if t.m_Uuid != via.m_Uuid and t.m_Uuid.AsString() not in removed]
        if any(isinstance(t, (pcbnew.PCB_VIA, pcbnew.PCB_ARC))
               or t.GetNetCode() != net for t in tails):
            return original
        layers = {t.GetLayer() for t in tails}
        for pad in connection.GetConnectedPads(via):
            pad_layers = [layer for layer in stack if pad.IsOnLayer(layer)]
            if (pad.GetNetCode() != net or pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD
                    or pad.GetDrillSize().x or pad.GetDrillSize().y or len(pad_layers) != 1):
                return original
            layers.add(pad_layers[0])
        if len(layers) != 1 or source in layers:
            return original
        layer = next(iter(layers))
        exterior.add(layer)
        lo, hi = sorted((order[source], order[layer]))
        limits &= set(stack[lo:hi+1])
        outside.update(t.m_Uuid.AsString() for t in tails)
        vias.append(via)
    if vias[0].m_Uuid == vias[1].m_Uuid:
        return original
    allowed = outside | {v.m_Uuid.AsString() for v in vias}
    for item in chain:
        if connection.GetConnectedPads(item):
            return original
        if any(t.m_Uuid.AsString() not in removed | allowed for t in connection.GetConnectedTracks(item)):
            return original
    choices = [str(board.GetLayerName(layer)) for layer in stack if layer in limits-exterior-{source}
               and copper.get(str(board.GetLayerName(layer)), 0) >= copper[name]
               and all(v.IsOnLayer(layer) and copper_width(v, layer=layer) >= copper_width(v, layer=source)
                       for v in vias)]
    return original + tuple(choices)


def series_endpoint_aliases(board, tracks, open_ports, connection):
    """Match unique, overlapping trace ends between two fixed through vias."""
    import pcbnew

    anchors = tuple(copper_boundary(board, tracks.values()).values())
    if len(anchors) != 2 or any(not isinstance(item, pcbnew.PCB_VIA)
                               or item.GetViaType() != pcbnew.VIATYPE_THROUGH for item in anchors):
        return None
    anchor_points = {(item.GetPosition().x, item.GetPosition().y) for item in anchors}
    if len(anchor_points) != 2 or not anchor_points <= open_ports.keys():
        return None
    loose_ports = {point: uid for point, uid in open_ports.items() if point not in anchor_points}
    matches = {}
    for point, uid in loose_ports.items():
        track = tracks[uid]
        adjacent = {item.m_Uuid.AsString() for item in connection.GetConnectedTracks(track)}
        mates = [other for other, other_uid in loose_ports.items()
                 if other_uid != uid and other_uid in adjacent
                 and math.dist(point, other) <= min(100000, track.GetWidth() // 4,
                                                      tracks[other_uid].GetWidth() // 4)]
        if len(mates) != 1:
            return None
        matches[point] = mates[0]
    if any(matches.get(other) != point for point, other in matches.items()):
        return None
    return {point: min(point, other) for point, other in matches.items()}


def anchored_series(board, track, *, connection=None, maximum: int = 64, allow_pads: bool = False):
    """Find a complete same-layer path using both copper contact and centerlines.

    A wide chamfer can touch the segment two steps away. Require a single
    centerline path, then let repair_layers prove both external via contacts.
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
        if (len(found) >= maximum or item.Type() != pcbnew.PCB_TRACE_T
                or item.IsLocked() or item.GetParentGroup() is not None or item.HasSolderMask()
                or (not allow_pads and connection.GetConnectedPads(item))):
            return None
        found[uid] = item
        pending.extend(other for other in map(typed_copper, connection.GetConnectedTracks(item))
                       if other.GetNetCode() == net and other.GetLayer() == layer
                       and not isinstance(other, pcbnew.PCB_VIA) and other.m_Uuid.AsString() not in found)

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
        aliases = series_endpoint_aliases(board, found, {point: port_owners[point] for point in ends},
                                          connection)
        if aliases is None:
            return None
        neighbors.clear()
        for first, last in edges:
            first, last = aliases.get(first, first), aliases.get(last, last)
            if first == last or last in neighbors[first]:
                return None
            neighbors[first].append(last)
            neighbors[last].append(first)
        ends = sorted(point for point, links in neighbors.items() if len(links) == 1)
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
        if len(contacts) != 2 or any(not item.IsOnLayer(layer) or item.GetNetCode() != net
                                      for item in contacts):
            return None
        for item in contacts:
            if isinstance(item, pcbnew.PAD):
                if item.GetAttribute() not in (pcbnew.PAD_ATTRIB_SMD, pcbnew.PAD_ATTRIB_PTH):
                    return None
            elif not isinstance(item, pcbnew.PCB_VIA):
                return None
        positions = [(item.GetPosition().x, item.GetPosition().y) for item in contacts]
        if sorted(positions) != ends:
            return None
    return chain, *(pcbnew.VECTOR2I(*point) for point in ends)


def series_trials(board, minima: dict[str, int], copper: dict[str, int], *, same_layer: bool = False):
    """Schedule a whole run once per allowed layer, with fixed boundary contacts."""
    import pcbnew

    connection = board.GetConnectivity()
    connection.Build(board)
    tracks = {track.m_Uuid.AsString(): track for track in board.GetTracks()}
    seen, trials = set(), []
    for uid in sorted(minima):
        track = tracks[uid]
        if uid in seen or track.Type() != pcbnew.PCB_TRACE_T or track.IsLocked() or track.GetParentGroup():
            continue
        series = anchored_series(board, track, connection=connection, allow_pads=same_layer)
        if series is None:
            continue
        chain, start, end = series
        members = {item.m_Uuid.AsString() for item in chain}
        seen.update(members)
        layers = ((str(board.GetLayerName(track.GetLayer())),) if same_layer
                  else repair_layers(board, chain, start, end, copper, connection=connection)[1:])
        if layers:
            findings = members & minima.keys()
            required = max(minima[item] for item in findings)
            length = sum(item.GetLength() for item in chain)
            trials.append((-len(findings), length if same_layer else -length, uid, required, layers))
    return [(uid, minimum, layer) for _, _, uid, minimum, layers in sorted(trials) for layer in reversed(layers)]


def pad_endpoints(board, chain, terminal, point):
    """Offer nearby landings on the same SMD pad when no branch uses this port."""
    import pcbnew
    from pcb.branch_widths import contact_port
    from pcb.route_optimizer import Point, mm

    origin = Point(point.x, point.y)
    layer, net = terminal.GetLayer(), terminal.GetNetCode()
    if any(zone.GetNetCode() == net and not zone.GetIsRuleArea() for zone in board.Zones()):
        return (origin,)
    connectivity = board.GetConnectivity()
    connectivity.Build(board)
    selected = {item.m_Uuid.AsString() for item in chain}
    for item in map(typed_copper, connectivity.GetConnectedTracks(terminal)):
        if item.m_Uuid.AsString() in selected or not item.IsOnLayer(layer):
            continue
        port = contact_port(terminal, item)
        if port is None or port == (point.x, point.y):
            return (origin,)
    pads = [pad for pad in connectivity.GetConnectedPads(terminal)
            if pad.GetNetCode() == net and pad.IsOnLayer(layer) and pad.HitTest(point)]
    if len(pads) != 1 or pads[0].GetAttribute() != pcbnew.PAD_ATTRIB_SMD:
        return (origin,)
    pad = pads[0]
    center, bounds = pad.GetPosition(), pad.GetBoundingBox()
    sites = [Point(center.x, center.y)]
    for axis in (0, 1):
        for sign in (-1, 1):
            x, y = center.x, center.y
            if axis == 0:
                x += sign * min(mm(.75), bounds.GetWidth()//3)
            else:
                y += sign * min(mm(.75), bounds.GetHeight()//3)
            sites.append(Point(x, y))
    return tuple(dict.fromkeys((origin, *(site for site in sites
        if math.hypot(site.x-point.x, site.y-point.y) <= mm(1.5)
        and pad.HitTest(pcbnew.VECTOR2I(site.x, site.y))))))


def plan_width_reroute(project: Path, uid: str, minimum: int, seconds: float, *,
                       relocate: bool = False, target_layer: str | None = None,
                       same_layer_series: bool = False) -> bytes | None:
    """Propose a rule-aware detour; independent native checks must accept it."""
    import pcbnew
    from pcb.net_routing.reference import high_speed_nets
    from pcb.net_routing.rules import RoutingRules
    from pcb.net_routing.style import audit_style
    from pcb.route_junctions import junction_changes
    from pcb.route_native import _configured_copper_thicknesses, read_native_obstacles
    from pcb.route_optimizer import (GridRouter, NoGridRoute, Point, SearchBudgetExceeded,
                                     SearchLimitReached, mm)

    if ((same_layer_series and target_layer is None) or not math.isfinite(seconds) or seconds <= 0
            or not isinstance(minimum, int) or minimum < 150000):
        raise ValueError('Invalid width-rerouting budget or minimum')
    source = project / 'v4.1.kicad_pcb'
    original = source.read_bytes()
    board = pcbnew.LoadBoard(str(source))
    track = next((x for x in board.GetTracks() if x.m_Uuid.AsString() == uid), None)
    grouped = {x.m_Uuid.AsString() for group in board.Groups() for x in group.GetItems()}
    if (track is None or isinstance(track, (pcbnew.PCB_ARC, pcbnew.PCB_VIA))
            or track.IsLocked() or uid in grouped or track.GetWidth() > minimum
            or (track.GetWidth() == minimum and not relocate)):
        return None
    net = str(track.GetNetname())
    if net in high_speed_nets(source.with_suffix('.kicad_pro').read_bytes(), {net}):
        return None
    if same_layer_series:
        series = anchored_series(board, track, allow_pads=True)
    elif target_layer is not None:
        series = anchored_series(board, track)
    else:
        series = width_chain(board, track)
    if series is None:
        return None
    chain, a, b = series
    copper = dict(_configured_copper_thicknesses(original))
    source_layer = str(board.GetLayerName(track.GetLayer()))
    eligible_layers = ((source_layer,) if same_layer_series
                       else repair_layers(board, chain, a, b, copper))
    if target_layer is not None:
        if target_layer not in (eligible_layers if same_layer_series else eligible_layers[1:]):
            return None
        layers = (target_layer,)
        minimum = max(minimum, *(item.GetWidth() for item in chain))
    else:
        layers = (source_layer,) if relocate else eligible_layers
    removed_ids = frozenset(item.m_Uuid.AsString() for item in chain)
    remove = [{'kind': 'segment', 'uuid': key} for key in sorted(removed_ids)]
    native = read_native_obstacles(source, net, assume_refill=True, removals=remove)
    rules = RoutingRules.from_board(board, source.with_suffix('.kicad_dru'))
    width_floor = minimum if target_layer is not None else track.GetWidth()
    profile = rules.profile(net, minimum, mm(.2), width_floor=width_floor)
    margin = max(mm(5 if target_layer is not None else 2), minimum)
    outline = board.GetBoardEdgesBoundingBox()
    edge = max(0, int(board.GetDesignSettings().m_CopperEdgeClearance) - profile.minimum_clearance)
    lower = Point(max(outline.GetX() + edge, min(a.x, b.x) - margin),
                  max(outline.GetY() + edge, min(a.y, b.y) - margin))
    upper = Point(min(outline.GetRight() - edge, max(a.x, b.x) + margin),
                  min(outline.GetBottom() - edge, max(a.y, b.y) + margin))
    if lower.x >= upper.x or lower.y >= upper.y:
        return None
    deadline = time.monotonic() + seconds
    attempt_deadline = deadline
    def budget():
        if time.monotonic() >= min(deadline, attempt_deadline):
            raise SearchBudgetExceeded('Width-rerouting search budget exhausted')
    initial, final = ((None, None) if target_layer is not None
                      else route_headings(board, track, removed_ids=removed_ids, endpoints=(a, b)))
    step = mm(.1 if target_layer is not None else .05)
    while (((upper.x - lower.x + step - 1) // step + 1)
           * ((upper.y - lower.y + step - 1) // step + 1) > 1500000):
        step *= 2
    original_ids = {t.m_Uuid.AsString() for t in board.GetTracks()}
    boundary_ids = set(copper_boundary(board, chain))
    start, end = Point(a.x, a.y), Point(b.x, b.y)
    alternatives = [(start, end)]
    if target_layer is None:
        alternatives = sorted(product(pad_endpoints(board, chain, chain[0], a),
                                      pad_endpoints(board, chain, chain[-1], b)),
                              key=lambda pair: (pair[0] != start) + (pair[1] != end))
    prior = sum(item.GetLength()/item.GetWidth() for item in chain)/copper.get(source_layer, 1)
    planner = CampaignPlanner(project)
    old_style = None
    rejected_shapes = set()

    def materialize(route):
        changes, _ = junction_changes(board, original, route, net, minimum, mm(.6), mm(.3),
                                      removed_copper_ids=removed_ids, prune_dangling_tails=False,
                                      segment_dimensions=profile.pieces)
        changes[:0] = [{'document': 'pcb', 'op': 'remove', 'target': target, 'path': []} for target in remove]
        return planner.plan('pcb', changes).after

    def legal_style(route):
        nonlocal old_style
        if route.segments in rejected_shapes or len(rejected_shapes) >= 8:
            return False
        if old_style is None:
            old_style = audit_style(board)
        with tempfile.TemporaryDirectory(prefix='autopcb-width-style-') as folder:
            candidate = Path(folder)/'candidate.kicad_pcb'
            candidate.write_bytes(materialize(route))
            try:
                verify_routing_style(old_style, audit_style(pcbnew.LoadBoard(str(candidate))))
            except ConnectivityRegression:
                rejected_shapes.add(route.segments)
                return False
        return True
    for index, layer in enumerate(layers):
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            break
        attempt_deadline = time.monotonic() + remaining/(len(layers)-index)
        try:
            router = GridRouter(lower, upper, step, (layer,), net, minimum,
                            mm(.2), mm(.6), mm(.3), native.obstacles,
                            max_expansions=80000, max_cells=1500000, allow_new_vias=False,
                            allowed_reused_vias=set(), forbid_staircase=True, gentle_turns=True,
                            first_feasible=True, heuristic_weight=4, profile=profile, check_budget=budget,
                            initial_directions=initial if layer == source_layer else None,
                            final_directions=final if layer == source_layer else None)
        except (NoGridRoute, SearchBudgetExceeded, SearchLimitReached):
            continue
        thickness = copper.get(layer, 1)

        def acceptable(route):
            # Equal or lower series resistance; layer moves also shorten the
            # two previously used through-via barrel spans.
            resistance = sum(math.hypot(piece.end.x-piece.start.x, piece.end.y-piece.start.y)/width
                             for segment in route.segments for piece, width in profile.pieces(segment))
            return (not (route.vias or route.reused_vias) and resistance/thickness <= prior + 1e-9
                    and legal_style(route))

        choices = alternatives if layer == source_layer else [(start, end)]
        layer_deadline = attempt_deadline
        for index_site, (first, last) in enumerate(choices):
            remaining = layer_deadline-time.monotonic()
            if remaining <= 0:
                break
            # Keep a failed search at an original anchor from consuming the
            # whole budget before its alternate pad landings are tried.
            fraction = 1 if index_site == len(choices)-1 else 0.55 if index_site == 0 else 0.6
            attempt_deadline = time.monotonic() + remaining*fraction
            try:
                route = router.route(first, layer, last, layer, candidate_check=acceptable)
            except (NoGridRoute, SearchBudgetExceeded, SearchLimitReached):
                continue
            candidate = materialize(route)
            if relocate or same_layer_series or layer != source_layer or (first, last) != (start, end):
                with tempfile.TemporaryDirectory(prefix='autopcb-width-contacts-') as folder:
                    path = Path(folder)/'candidate.kicad_pcb'
                    path.write_bytes(candidate)
                    trial = pcbnew.LoadBoard(str(path))
                    current = {t.m_Uuid.AsString():t for t in trial.GetTracks()}
                    if original_ids-current.keys() != removed_ids:
                        continue
                    added = tuple(current[key] for key in current.keys()-original_ids)
                    if set(copper_boundary(trial, added)) != boundary_ids:
                        continue
            if source.read_bytes() != original:
                raise RuntimeError('Width-rerouting source changed during planning')
            return candidate
    return None


def _covers_old_trace(old_width: int, new_width: int, dx: int, dy: int) -> bool:
    """A translated capsule contains the old one if its extra radius covers the offset."""
    added_width = new_width - old_width
    return added_width > 0 and 4 * (dx * dx + dy * dy) <= added_width * added_width


def shifted_trial_sites(
    project: Path, minima: dict[str, int]
) -> list[tuple[str, int, tuple[int, int]]]:
    """Screen small perpendicular shifts, preferring those that keep all existing copper."""
    import pcbnew
    from pcb.freeroute_delta import Trace, native_clearance_checker
    from pcb.net_routing.rules import RoutingRules

    path = project/'v4.1.kicad_pcb'
    board = pcbnew.LoadBoard(str(path))
    connectivity = board.GetConnectivity()
    connectivity.Build(board)
    tracks = {item.m_Uuid.AsString():item for item in board.GetTracks()}
    rules = RoutingRules.from_board(board,path.with_suffix('.kicad_dru'))
    profiles = {}
    clear = native_clearance_checker(board,profile_for=lambda item: profiles[item.net])
    trials = []
    for uid, minimum in sorted(minima.items(), key=lambda entry: (entry[1],entry[0])):
        track = tracks[uid]
        if (track.Type()!=pcbnew.PCB_TRACE_T or track.IsLocked()
                or track.GetParentGroup() is not None or track.HasSolderMask()
                or track.GetLength()<100000):
            continue
        has_pad = bool(connectivity.GetConnectedPads(track))
        neighbors = tuple(connectivity.GetConnectedTracks(track))
        if (not has_pad and not neighbors) or any(item.Type() == pcbnew.PCB_VIA_T for item in neighbors):
            continue
        a,b = track.GetStart(),track.GetEnd()
        dx,dy = b.x-a.x,b.y-a.y
        if (dx and dy and abs(dx)!=abs(dy)) or not (dx or dy):
            continue
        nx,ny = (dy<0)-(dy>0),(dx>0)-(dx<0)
        net,layer = str(track.GetNetname()),str(board.GetLayerName(track.GetLayer()))
        profiles[net] = rules.profile(net,minimum,0,width_floor=minimum)
        count = 0
        for step in range(25000,650001,25000):
            for distance in (step,-step):
                x,y = nx*distance,ny*distance
                first,last = (a.x+x,a.y+y),(b.x+x,b.y+y)
                preserves_copper = _covers_old_trace(track.GetWidth(), minimum, x, y)
                if (has_pad or preserves_copper) and clear((Trace(net,layer,minimum,first,last),)):
                    trials.append((count, not preserves_copper, minimum, uid, (x, y)))
                    count += 1
            if count>=12:
                break
    return [(uid, minimum, offset) for _, _, minimum, uid, offset in sorted(trials)]


def plan_shifted_widening(project: Path, uid: str, minimum: int, offset: tuple[int, int]) -> bytes | None:
    """Build one widened translation if all native boundary contacts are retained."""
    import pcbnew

    planner = CampaignPlanner(project)
    path = planner.document_path('pcb')
    original = path.read_bytes()
    board = pcbnew.LoadBoard(str(path))
    track = next((item for item in board.GetTracks() if item.m_Uuid.AsString()==uid),None)
    if (track is None or track.Type()!=pcbnew.PCB_TRACE_T or track.IsLocked()
            or track.GetParentGroup() is not None or track.HasSolderMask() or track.GetWidth()>=minimum):
        return None
    a,b = track.GetStart(),track.GetEnd()
    x,y = offset
    if not (x or y) or max(abs(x),abs(y))>650000 or (b.x-a.x)*x+(b.y-a.y)*y:
        return None
    contacts = copper_boundary(board,(track,))
    if not contacts or any(isinstance(item, pcbnew.PCB_VIA) for item in contacts.values()):
        return None
    if (not any(isinstance(item, pcbnew.PAD) for item in contacts.values())
            and not _covers_old_trace(track.GetWidth(), minimum, x, y)):
        return None
    target = {'kind':'segment','uuid':uid}
    expressions = {'start':f'(start {(a.x+x)/1e6:.6f} {(a.y+y)/1e6:.6f})',
                   'end':f'(end {(b.x+x)/1e6:.6f} {(b.y+y)/1e6:.6f})',
                   'width':f'(width {minimum/1e6:.6f})'}
    candidate = planner.plan('pcb',[{'op':'replace','target':target,'path':[field],'expression':value}
                                    for field,value in expressions.items()]).after
    with tempfile.TemporaryDirectory(prefix='autopcb-shift-contacts-') as folder:
        candidate_path = Path(folder)/path.name
        candidate_path.write_bytes(candidate)
        trial = pcbnew.LoadBoard(str(candidate_path))
        shifted = next(item for item in trial.GetTracks() if item.m_Uuid.AsString()==uid)
        if set(copper_boundary(trial,(shifted,))) != set(contacts):
            return None
    if path.read_bytes()!=original:
        raise RuntimeError('Width repair source changed during translation')
    return candidate


def coupled_neighbors(report: dict, targets: set[str], movable: set[str], *,
                      limit: int = 3) -> dict[str, tuple[str, ...]]:
    """Attribute each trial widening's clearance failures to editable traces."""
    dependencies = {uid: set() for uid in targets}
    blocked = set()
    for finding in report['violations']:
        if finding['type'] not in {'clearance', 'shorting_items', 'hole_clearance',
                                  'edge_clearance', 'items_not_allowed'}:
            continue
        items = {item['uuid'] for item in finding['items'] if item.get('uuid')}
        for uid in items & targets:
            others = items - {uid}
            if len(others) != 1 or not others <= movable:
                blocked.add(uid)
            else:
                dependencies[uid].update(others)
    return {uid: tuple(sorted(values)) for uid, values in dependencies.items()
            if uid not in blocked and len(values) <= limit}


def plan_coupled_widening(project: Path, uid: str, minimum: int,
                          neighbors: dict[str, int], seconds: float) -> bytes | None:
    """Widen a fixed trace and negotiate the obstructing copper together."""
    import pcbnew

    before = project / 'v4.1.kicad_pcb'
    original = before.read_bytes()
    widened = width_candidate(project, {uid: minimum})
    if not neighbors:
        return widened
    with tempfile.TemporaryDirectory(prefix='autopcb-width-coordinated-') as folder:
        private = Path(folder)
        candidate = private / before.name
        for suffix in ('.kicad_pro', '.kicad_dru'):
            support = before.with_suffix(suffix)
            candidate.with_suffix(suffix).write_bytes(support.read_bytes())
        sequence = tuple(neighbors)
        orders = (sequence,) if len(sequence) == 1 else (sequence, sequence[::-1])
        for order in orders:
            candidate.write_bytes(widened)
            for neighbor in order:
                board = pcbnew.LoadBoard(str(candidate))
                tracked = next((item for item in board.GetTracks()
                                if item.m_Uuid.AsString() == neighbor), None)
                if tracked is None:
                    # A preceding repair may have replaced the same series run.
                    continue
                proposal = plan_width_reroute(private, neighbor, neighbors[neighbor], seconds,
                                               relocate=True)
                if proposal is None:
                    break
                candidate.write_bytes(proposal)
            else:
                if before.read_bytes() != original:
                    raise RuntimeError('Board changed while planning a coordinated width repair')
                return candidate.read_bytes()
    return None


def verify_tap_proofs(before: str, after: str) -> None:
    """Added same-net contacts must not invalidate a published branch allowance."""
    def proofs(block):
        return {line.strip() for line in block.splitlines()
                if line.lstrip().startswith('(rule ') and ' tap ' in line}
    if proofs(before) - proofs(after):
        raise ConnectivityRegression('Width change invalidated an existing branch current proof')


def required_widths(report: dict) -> dict[str, int]:
    """Read independently measured native minima, refusing unknown report forms."""
    result = {}
    for finding in report['violations']:
        if finding['type'] != 'track_width':
            continue
        items = finding.get('items', [])
        if len(items) != 1 or not items[0].get('uuid'):
            raise ValueError('A width finding must identify exactly one copper item')
        match = re.search(r'\bmin width ([0-9]+(?:\.[0-9]+)?) mm\b', finding['description'])
        if not match:
            raise ValueError('Native width finding has no supported minimum in mm')
        width = int(Decimal(match[1]) * 1000000)
        if width < 150000 or width > 20000000:
            raise ValueError('Native width minimum is outside supported fabrication bounds')
        uid = items[0]['uuid']
        result[uid] = max(width, result.get(uid, 0))
    return result


def width_candidate(project: Path, widths: dict[str, int]) -> bytes:
    """Edit only width fields on ordinary existing segments; never narrow copper."""
    planner = CampaignPlanner(project)
    source = planner.document_path('pcb').read_bytes()
    root = parse(source.decode('utf-8'))
    tracks = {form.children('uuid')[0].atoms()[1].value: form for form in root.children('segment')}
    changes = []
    for uid, width in sorted(widths.items()):
        if isinstance(width, bool) or not isinstance(width, int) or not 150000 <= width <= 20000000:
            raise ValueError('Invalid width target')
        if uid not in tracks:
            raise ValueError('Width repair requires an ordinary segment, not an arc or via')
        item = tracks[uid]
        old = Decimal(item.children('width')[0].atoms()[1].value) * 1000000
        if width < old:
            raise ValueError('Width repair cannot narrow existing copper')
        if width == old:
            continue
        if item.children('locked'):
            raise ValueError('Width repair cannot modify locked copper')
        changes.append({'op': 'replace', 'target': {'kind': 'segment', 'uuid': uid},
                        'path': ['width'], 'expression': f'(width {width / 1e6:g})'})
    return planner.plan('pcb', changes).after if changes else source


def contains_track(outer, inner) -> bool:
    """Prove that a straight track's exact round-ended copper covers another."""
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
            covered = 4 * (x * dy - y * dx)**2 <= width_difference**2 * length_squared
        if not covered:
            return False
    return True


def plated_lands(board):
    """Yield plated objects, whose unchanged drills remove copper on either board."""
    import pcbnew

    yield from (item for item in board.GetTracks() if isinstance(item, pcbnew.PCB_VIA))
    yield from (pad for pad in board.GetPads() if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH)


def plated_flashes(board) -> frozenset[tuple[str, int]]:
    """Record actual flashed copper so conditional lands cannot become witnesses by accident."""
    layers = tuple(board.GetEnabledLayers().CuStack())
    return frozenset((item.m_Uuid.AsString(), layer) for item in plated_lands(board)
                     for layer in layers if item.IsOnLayer(layer) and item.FlashLayer(layer))


def redundant_terminal_ends(board, requested: set[str], bound_rules: str) -> frozenset[str]:
    """Find short duplicate landings at already proven low-current pads."""
    import pcbnew
    from pcb.branch_widths import contact_port

    proven = set(re.findall(r'^\(rule "AutoPCB \S+ tap ([0-9a-f-]+)"', bound_rules, re.MULTILINE))
    zoned = {zone.GetNetCode() for zone in board.Zones() if not zone.GetIsRuleArea()}
    connectivity = board.GetConnectivity()
    connectivity.Build(board)
    found = set()
    for stub in board.GetTracks():
        uid = stub.m_Uuid.AsString()
        if (uid not in requested or uid in proven or stub.Type() != pcbnew.PCB_TRACE_T
                or stub.IsLocked() or stub.GetParentGroup() is not None or stub.HasSolderMask()
                or not 0 < stub.GetLength() <= 50000 or stub.GetNetCode() in zoned):
            continue
        pads = tuple(connectivity.GetConnectedPads(stub))
        neighbors = tuple(typed_copper(item) for item in connectivity.GetConnectedTracks(stub)
                          if item.m_Uuid != stub.m_Uuid)
        if len(pads) != 1 or len(neighbors) != 1:
            continue
        pad, parent = pads[0], neighbors[0]
        layer, net = stub.GetLayer(), stub.GetNetCode()
        if (pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD or not pad.IsOnLayer(layer)
                or pad.ConditionallyFlashed(layer) or not pad.FlashLayer(layer)
                or pad.GetNetCode() != net or parent.GetNetCode() != net
                or parent.Type() != pcbnew.PCB_TRACE_T or parent.GetLayer() != layer
                or parent.m_Uuid.AsString() not in proven
                or parent.m_Uuid.AsString() in requested):
            continue
        # The retained trace independently enters this exact pad. The nib
        # joins no other conductor, so it is not a series bottleneck.
        if (contact_port(stub, parent) is not None and contact_port(parent, pad) is not None
                and any(item.m_Uuid == pad.m_Uuid for item in connectivity.GetConnectedPads(parent))):
            found.add(uid)
    return frozenset(found)


def covered_width_tracks(board, requested: set[str]) -> frozenset[str]:
    """Find segments fully covered by retained copper on the same net and layer.

    Use an outer polygon for each candidate and inner polygons for curved
    witnesses. A flagged track can support a later deletion only after this
    pass has committed to retaining it. Callers using filled zones must
    supply a freshly refilled board and verify both the zone fills and
    plated lands on the final native round trip.
    """
    import pcbnew

    def shape(item, layer, rounding):
        polygon = pcbnew.SHAPE_POLY_SET()
        item.TransformShapeToPolygon(polygon, layer, 0, 2000, rounding)
        return polygon

    grouped = {item.m_Uuid.AsString() for group in board.Groups() for item in group.GetItems()}
    tracks = [item for item in board.GetTracks()
              if item.Type() == pcbnew.PCB_TRACE_T and item.GetNetCode() > 0]
    candidates = [track for track in tracks if track.m_Uuid.AsString() in requested
                  and track.m_Uuid.AsString() not in grouped and not track.IsLocked() and not track.HasSolderMask()]
    candidate_ids = {track.m_Uuid.AsString() for track in candidates}
    retained = defaultdict(list)
    for track in tracks:
        if track.m_Uuid.AsString() not in candidate_ids:
            retained[track.GetNetCode(), track.GetLayer()].append(track)
    pads = [pad for pad in board.GetPads() if pad.GetAttribute() == pcbnew.PAD_ATTRIB_SMD
            and not (pad.GetDrillSize().x or pad.GetDrillSize().y)]
    plated = list(plated_lands(board))
    flashed = plated_flashes(board)
    convex = {pcbnew.PAD_SHAPE_CIRCLE, pcbnew.PAD_SHAPE_RECT,
              pcbnew.PAD_SHAPE_OVAL, pcbnew.PAD_SHAPE_ROUNDRECT}
    witnesses, selected = {}, set()

    for track in sorted(candidates, key=lambda item: (-item.GetWidth(), -item.GetLength(),
                                                      item.m_Uuid.AsString())):
        net, layer = track.GetNetCode(), track.GetLayer()
        key, uid = (net, layer), track.m_Uuid.AsString()
        if any(contains_track(other, track) for other in retained[key]):
            selected.add(uid)
            continue
        if key not in witnesses:
            items = list(retained[key])
            items.extend(pad for pad in pads if pad.GetNetCode() == net and pad.IsOnLayer(layer)
                         and pad.GetShape(layer) in convex)
            items.extend(item for item in plated if item.GetNetCode() == net
                         and (item.m_Uuid.AsString(), layer) in flashed
                         and not item.ConditionallyFlashed(layer)
                         and (not isinstance(item, pcbnew.PAD) or item.GetShape(layer) in convex))
            copper = [shape(other, layer, pcbnew.ERROR_INSIDE) for other in items]
            copper.extend(zone.GetFilledPolysList(layer) for zone in board.Zones()
                          if zone.GetNetCode() == net and zone.IsOnLayer(layer) and not zone.GetIsRuleArea())
            witnesses[key] = [(polygon.BBox(), polygon) for polygon in copper
                              if polygon is not None and polygon.OutlineCount() > 0]

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


def copper_forms(data: bytes) -> dict:
    from pcb.route_validation import _canonical

    root = parse(data.decode('utf-8-sig'))
    return {form.children('uuid')[0].atoms()[1].value: _canonical(form)
            for kind in ('segment', 'via', 'arc') for form in root.children(kind)}


def verify_covered_edit(before: bytes, after: bytes, removed: set[str]) -> None:
    """Covered deletion must leave all retained copper and native fills exact."""
    from pcb.route_validation import _zone_refill_changes

    if copper_forms(after) != {uid: value for uid, value in copper_forms(before).items() if uid not in removed}:
        raise ConnectivityRegression('Covered cleanup changed retained copper')
    if _zone_refill_changes(before, after):
        raise ConnectivityRegression('Covered cleanup changed filled copper')


def unchanged_neck_regions(before, after) -> dict[str, str]:
    """Group unchanged copper remote from edits on nets without zones.

    Callers must first verify protected footprint geometry and layer settings.
    A one-millimetre halo conservatively excludes every item near changed copper;
    this is only for identical native neck measurements, never new measurements.
    """
    import pcbnew
    from core.pcb_io import copper_width

    layers = tuple(after.GetEnabledLayers().CuStack())
    zoned = {str(z.GetNetname()) for board in (before, after) for z in board.Zones()
             if not z.GetIsRuleArea()}
    unsupported = {str(t.GetNetname()) for board in (before, after) for t in board.GetTracks()
                   if isinstance(t, pcbnew.PCB_ARC)}
    def inventory(board):
        result = {}
        for item in (*board.GetTracks(), *board.GetPads()):
            net = str(item.GetNetname())
            active = frozenset(layer for layer in layers if item.IsOnLayer(layer))
            if isinstance(item, pcbnew.PCB_VIA):
                p = item.GetPosition()
                geometry = ('via', p.x, p.y, item.GetDrill(), item.GetViaType(),
                            tuple((layer, copper_width(item, layer=layer)) for layer in sorted(active)))
            elif isinstance(item, pcbnew.PAD):
                p, size = item.GetPosition(), item.GetSize()
                geometry = ('protected_pad', p.x, p.y, size.x, size.y, item.GetOrientationDegrees())
            elif isinstance(item, pcbnew.PCB_ARC):
                a, b, mid = item.GetStart(), item.GetEnd(), item.GetMid()
                geometry = ('arc', a.x, a.y, b.x, b.y, mid.x, mid.y, item.GetWidth())
            else:
                a, b = item.GetStart(), item.GetEnd()
                geometry = ('track', a.x, a.y, b.x, b.y, item.GetWidth())
            box = item.GetBoundingBox()
            result[item.m_Uuid.AsString()] = (item, (net, active, geometry), active,
                                              (box.GetX(), box.GetY(), box.GetRight(), box.GetBottom()))
        return result
    original, candidate = inventory(before), inventory(after)
    changed = {uid for uid in original.keys() | candidate.keys()
               if uid not in original or uid not in candidate or original[uid][1] != candidate[uid][1]}
    touched = [value for inventory_ in (original, candidate)
               for uid, value in inventory_.items() if uid in changed]
    def remote(value):
        _, _, active, (x0, y0, x1, y1) = value
        for _, _, other_layers, (a0, b0, a1, b1) in touched:
            if (active & other_layers and x0 <= a1 + 1000000 and a0 <= x1 + 1000000
                    and y0 <= b1 + 1000000 and b0 <= y1 + 1000000):
                return False
        return True
    stable = {uid: value[0] for uid, value in candidate.items()
              if uid not in changed and value[1][0] and value[1][0] not in zoned | unsupported and remote(value)}
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
            for other in (*connectivity.GetConnectedTracks(item), *connectivity.GetConnectedPads(item)):
                key = other.m_Uuid.AsString()
                if key in pending and other.GetNetCode() == item.GetNetCode():
                    pending.remove(key)
                    queue.append(key)
    return regions


def introduced_findings(before: dict, after: dict, *,
                        stable_neck_nets: frozenset[str] = frozenset(),
                        neck_regions: dict[str, str] | None = None,
                        include_widths: bool = False) -> list[dict]:
    """Compare full measurements, not counts that could hide a new short."""
    if (after['schematic_parity'] != before['schematic_parity']
            or after.get('ignored_checks') != before.get('ignored_checks')):
        raise RuntimeError('Width repair changed parity or ignored checks')
    def key(value):
        # Native neck warnings may choose either adjacent segment on reruns.
        # Accept another marker only on nets where the caller has proven the
        # connection did not narrow. Require the same measured widths and count.
        if value['type'] == 'connection_width' and value.get('severity') == 'warning':
            ids = [item.get('uuid') for item in value.get('items', [])]
            measurement = re.search(r'\bmin(?:imum)?(?: connection)? width\s+'
                                    r'([0-9]+(?:\.[0-9]+)?|\.[0-9]+)\s+mm\b', value['description'])
            if (ids and neck_regions and measurement and Decimal(measurement[1]) <= Decimal('.5')
                    and all(uid in neck_regions for uid in ids)):
                regions = {neck_regions[uid] for uid in ids}
                if len(regions) == 1:
                    return json.dumps((value['type'], value['severity'], 'unchanged_region',
                                       sorted(regions), value['description']))
            nets = set()
            for item in value.get('items', []):
                match = re.search(r'\[([^\]]+)\]', item.get('description', ''))
                if not match:
                    break
                nets.add(match[1])
            else:
                if len(nets) == 1 and nets <= stable_neck_nets:
                    return json.dumps((value['type'], value['severity'], sorted(nets), value['description']))
        return json.dumps(value, sort_keys=True, allow_nan=False)
    previous = Counter(key(v) for v in before['violations'] if v['type'] != 'track_width' or include_widths)
    result = []
    for finding in after['violations']:
        if finding['type'] == 'track_width' and not include_widths:
            continue
        identifier = key(finding)
        if previous[identifier]:
            previous[identifier] -= 1
        else:
            result.append(finding)
    if result:
        return result
    if after['unconnected_items'] != before['unconnected_items']:
        raise ConnectivityRegression('Width repair changed connectivity')
    return []


def reduce_conflicts(targets: dict[str, int], minima: dict[str, int],
                     conflicts: list[dict]) -> tuple[dict[str, int], set[str]]:
    """Try the required minimum before deferring a conflicting widening."""
    implicated = set()
    for finding in conflicts:
        hits = {item['uuid'] for item in finding.get('items', [])} & set(targets)
        implicated.update(hits)
    if not implicated:
        raise RuntimeError('A new finding is unattributed to the width edits')
    result, deferred = dict(targets), set()
    for uid in implicated:
        if result[uid] > minima[uid]:
            result[uid] = minima[uid]
        else:
            del result[uid]
            deferred.add(uid)
    return result, deferred


def select_widenings(targets: dict[str, int], minima: dict[str, int], check, *,
                     max_checks: int, progress=print) -> tuple[dict[str, int], set[str]]:
    """Keep accepted copper while partitioning interacting or obstructed edits."""
    accepted, pending = {}, deque([dict(targets)])
    for iteration in range(max_checks):
        if not pending:
            break
        batch = pending.popleft()
        progress(f'Width batch {iteration + 1}: checking {len(batch)} edits, {len(accepted)} retained')
        try:
            conflicts = check(accepted | batch)
        except ConnectivityRegression as error:
            progress(f'  Rejected trial: {error}')
            conflicts = None
        if conflicts == []:
            accepted.update(batch)
            progress(f'  Retained {len(accepted)} checked repairs')
            continue
        if conflicts:
            try:
                revised, _ = reduce_conflicts(batch, minima, conflicts)
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
        progress('  Separating interacting widenings; saved copper remains unchanged')
    return accepted, set(targets) - set(accepted)


def repair(project: Path, *, apply: bool = False, max_checks: int = 16,
           reroute: bool = False, seconds_per_route: float = 5, prune_covered: bool = False,
           coordinated: bool = False, series: bool = False, ground_stitch: bool = False,
           same_layer_series: bool = False, shift: bool = False, trim_terminal_ends: bool = False) -> dict:
    """Retain a source-bound batch only after independent native closing checks."""
    import pcbnew
    from pcb.net_routing.isolation import run_native
    from pcb.net_routing.controller import encode_checkpoint
    from pcb.net_routing.native import Snapshot, load_board
    from pcb.net_routing.protection import ProtectedState
    from pcb.net_routing.reference import high_speed_nets, reference_conflicts
    from pcb.net_routing.runner import _exclusive, _save, _verify_cumulative_reference_native
    from pcb.net_routing.style import audit_style
    from pcb.branch_widths import compile_widths, replace_managed
    from pcb.route_validation import _drc, _drc_native

    modes = (reroute, prune_covered, coordinated, series, ground_stitch, shift, trim_terminal_ends)
    removes_copper = prune_covered or trim_terminal_ends
    if (any(not isinstance(flag, bool) for flag in (apply, same_layer_series, *modes))
            or (same_layer_series and not series) or sum(modes) > 1 or not 1 <= max_checks <= 512
            or not math.isfinite(seconds_per_route) or not 0 < seconds_per_route <= 60):
        raise ValueError('Invalid width-repair budget')
    project = project.resolve()
    assets = asset_root(project)
    policy_path = assets / 'config' / 'branch_widths.json'
    policy_bytes = policy_path.read_bytes()
    policy = json.loads(policy_bytes)
    if policy.get('electrically_verified') is not False:
        raise ValueError('Width targets must retain their unqualified electrical status')
    cli = Path(sys.executable).with_name('kicad-cli.exe')
    if not cli.is_file():
        raise ValueError('Native KiCad validation tool not found')
    with _exclusive(assets / 'results' / 'autoroute' / 'active.lock'):
        with Snapshot(project / 'v4.1.kicad_pcb') as snapshot:
            dependencies = dict(snapshot.fingerprints)
            dependencies[policy_path] = file_sha256(policy_path)
            for name in ('POWER_LOAD_BUDGET.csv', 'STACKUP_CURRENT.json'):
                path = assets / 'design' / name
                dependencies[path] = file_sha256(path)
            guard = BoardSaveGuard(snapshot.source, tuple(dependencies.items()))
            guard.verify_editor_unlocked()
            schematic = snapshot.source.with_suffix('.kicad_sch').read_bytes()
            protected = ProtectedState.capture(snapshot.original, schematic, preserve_all_placement=True)
            print('Inventorying all native width findings; saved board stays unchanged', flush=True)
            baseline = _drc(cli, snapshot.board_path, snapshot.directory / 'before.json',
                            save_refill=True, schematic_parity=True)
            filled_baseline = snapshot.board_path.read_bytes()
            filled_board = pcbnew.LoadBoard(str(snapshot.board_path)) if prune_covered else None
            flash_baseline = plated_flashes(filled_board) if prune_covered else None
            changes = protected.differences(snapshot.board_path.read_bytes(), schematic)
            if changes:
                raise RuntimeError('Native save preflight changed protected content: ' + ', '.join(changes))
            snapshot.board_path.write_bytes(snapshot.original)
            minima = required_widths(baseline)
            board = load_board(snapshot.board_path)
            bound_rules, _ = compile_widths(board, policy)
            saved_rules = snapshot.board_path.with_suffix('.kicad_dru').read_text(encoding='utf-8')
            if replace_managed(saved_rules, bound_rules) != saved_rules:
                raise RuntimeError('Branch proofs are stale; run pcb branch-widths --apply first')
            tracks = {track.m_Uuid.AsString(): track for track in board.GetTracks()}
            critical = high_speed_nets(snapshot.board_path.with_suffix('.kicad_pro').read_bytes(),
                                       {str(net) for net in board.GetNetsByName() if str(net)})
            if not critical:
                raise RuntimeError('USB/Ethernet classification is unavailable')
            if any(tracks[uid].GetNetname() in critical for uid in minima):
                raise RuntimeError('High-speed width changes require impedance-qualified pair routing')
            recommendations = {rail['net']: rail.get('recommended_width_mm', rail['width_mm'])
                               for rail in policy.get('rails', [])}
            zoned = {str(zone.GetNetname()) for zone in board.Zones() if not zone.GetIsRuleArea()}
            stable_necks = frozenset(str(net) for net in board.GetNetsByName()
                                     if str(net) and str(net) not in zoned)
            unchanged_necks = frozenset(str(net) for net in board.GetNetsByName() if str(net))
            targets = {uid: max(width, round(recommendations.get(tracks[uid].GetNetname(), 0) * 1e6))
                       for uid, width in minima.items()}
            if prune_covered:
                eligible = {uid for uid in minima if tracks[uid].GetNetname() not in critical}
                targets = {uid: minima[uid] for uid in covered_width_tracks(filled_board, eligible)}
                print(f'{len(targets)} flagged segments lie entirely in retained copper', flush=True)
            if trim_terminal_ends:
                targets = {uid: minima[uid] for uid in redundant_terminal_ends(board, set(minima), bound_rules)}
                print(f'{len(targets)} flagged terminal tips have an independent retained pad contact', flush=True)
                if targets:
                    stored_baseline = _drc_native(cli, snapshot.board_path, snapshot.directory/'stored-before.json',
                                                  schematic_parity=True, refill_zones=False)
                    stored_output = snapshot.directory/'stored-after.json'
            if ground_stitch:
                if baseline['unconnected_items']:
                    raise ValueError('Ground-stitch width repair starts from a connected board')
                route_policy = json.loads((assets/'config/autopcb.json').read_bytes())
                ground_ids = {item.m_Uuid.AsString() for item in (*board.GetPads(), *board.GetTracks(), *board.Zones())
                              if str(item.GetNetname()) == 'GND'}
                eligible = {uid: value for uid, value in minima.items()
                            if value <= 1500000 and tracks[uid].Type() == pcbnew.PCB_TRACE_T
                            and not tracks[uid].IsLocked() and tracks[uid].GetParentGroup() is None}
                snapshot.board_path.write_bytes(width_candidate(snapshot.directory, eligible))
                atlas = _drc(cli, snapshot.board_path, snapshot.directory/'width-stitch-atlas.json',
                             schematic_parity=True)
                clear = coupled_neighbors(atlas, set(eligible), set())
                targets = {uid: eligible[uid] for uid in clear}
                snapshot.board_path.write_bytes(snapshot.original)
                print(f'{len(targets)} candidate widenings clear copper and drill spacing', flush=True)
            output = snapshot.directory / 'after.json'
            prior = snapshot.directory / 'width-prior.kicad_pcb'
            prior.write_bytes(snapshot.original)
            latest = {'reference': None, 'ground_stitches': 0}
            moving_nets = set()
            detours = reroute or coordinated or series or shift
            spatial_change = detours or removes_copper or ground_stitch
            original_style = audit_style(board) if detours or ground_stitch or trim_terminal_ends else None
            def check_covered(removed):
                verify_covered_edit(filled_baseline, snapshot.board_path.read_bytes(), set(removed))
                if plated_flashes(pcbnew.LoadBoard(str(snapshot.board_path))) != flash_baseline:
                    raise ConnectivityRegression('Covered cleanup changed a plated land')

            def check(proposal):
                snapshot.board_path.write_bytes(snapshot.original)
                latest['ground_stitches'] = 0
                if isinstance(proposal, bytes):
                    candidate = proposal
                elif removes_copper:
                    candidate = CampaignPlanner(snapshot.directory).plan('pcb', [
                        {'op': 'remove', 'target': {'kind': 'segment', 'uuid': uid}, 'path': []}
                        for uid in sorted(proposal)]).after
                else:
                    candidate = width_candidate(snapshot.directory, proposal)
                snapshot.board_path.write_bytes(candidate)
                if protected.differences(candidate, schematic):
                    raise RuntimeError('Width repair altered a protected item')
                trial_board = None
                if spatial_change:
                    trial_board = load_board(snapshot.board_path)
                    trial_rules, _ = compile_widths(trial_board, policy)
                    verify_tap_proofs(bound_rules, trial_rules)
                    # Covered cleanup proves the same physical conductor below.
                    # Its deleted labels in the centerline graph can change bend attribution.
                    if detours or trim_terminal_ends:
                        verify_routing_style(original_style, audit_style(trial_board))
                output.unlink(missing_ok=True)
                after = _drc(cli, snapshot.board_path, output, schematic_parity=True, save_refill=prune_covered)
                if ground_stitch and after['unconnected_items']:
                    from pcb.net_routing.fast_rework import prepare_ground_access

                    missing = after['unconnected_items']
                    if not all(entry.get('items') and all(item.get('uuid') in ground_ids for item in entry['items'])
                               for entry in missing):
                        raise ConnectivityRegression('Widening disconnected copper outside the ground net')
                    stitch = run_native(prepare_ground_access, snapshot.directory, route_policy)
                    if not stitch['proposed_stitches']:
                        raise ConnectivityRegression('No ground stitch fit the affected island')
                    revised = snapshot.board_path.read_bytes()
                    if protected.differences(revised, schematic):
                        raise RuntimeError('Ground stitching changed protected content')
                    before_stitch, after_stitch = copper_forms(candidate), copper_forms(revised)
                    if any(after_stitch.get(uid) != record for uid, record in before_stitch.items()):
                        raise RuntimeError('Ground stitching changed retained routing')
                    trial_board = load_board(snapshot.board_path)
                    added = [item for item in trial_board.GetTracks()
                             if item.m_Uuid.AsString() not in before_stitch]
                    if not added or any(str(item.GetNetname()) != 'GND' for item in added):
                        raise RuntimeError('Ground stitching added unexpected copper')
                    trial_rules, _ = compile_widths(trial_board, policy)
                    verify_tap_proofs(bound_rules, trial_rules)
                    verify_routing_style(original_style, audit_style(trial_board))
                    output.unlink()
                    after = _drc(cli, snapshot.board_path, output, schematic_parity=True)
                    latest['ground_stitches'] = stitch['proposed_stitches']
                    print(f"  Rejoined {stitch['proposed_stitches']} ground island(s) on the private board", flush=True)
                if prune_covered:
                    check_covered(proposal)
                if trim_terminal_ends:
                    verify_covered_edit(snapshot.original, candidate, set(proposal))
                    changed_nets = {str(tracks[uid].GetNetname()) for uid in proposal}
                    stored_output.unlink(missing_ok=True)
                    stored_after = _drc_native(cli, snapshot.board_path, stored_output,
                                               schematic_parity=True, refill_zones=False)
                    if introduced_findings(stored_baseline, stored_after,
                                           stable_neck_nets=unchanged_necks-changed_nets):
                        raise ConnectivityRegression('Terminal cleanup introduced a finding with the stored fills')
                else:
                    changed_nets = moving_nets
                necks = unchanged_necks if prune_covered else stable_necks-changed_nets
                conflicts = introduced_findings(baseline, after, stable_neck_nets=necks,
                                                neck_regions=unchanged_neck_regions(board, trial_board)
                                                if detours else None,
                                                include_widths=spatial_change)
                if conflicts:
                    kinds = Counter(v.get('type', 'reference_copper') for v in conflicts)
                    print('  New conflicts:', dict(kinds), flush=True)
                    return conflicts
                if trial_board is None:
                    trial_board = load_board(snapshot.board_path)
                    trial_rules, _ = compile_widths(trial_board, policy)
                    verify_tap_proofs(bound_rules, trial_rules)
                reference = _verify_cumulative_reference_native(prior, snapshot.board_path, tuple(sorted(critical)))
                if reference['status'] == 'degraded':
                    widened = load_board(snapshot.board_path)
                    sources = set(reference['degraded_track_ids'])
                    edited = (set(proposal) if not isinstance(proposal, bytes) else
                              {t.m_Uuid.AsString() for t in widened.GetTracks()
                               if t.m_Uuid.AsString() not in tracks})
                    hits = {c.blocking_item_id for c in reference_conflicts(widened, critical)
                            if c.high_speed_item_id in sources} & edited
                    if not hits:
                        raise ConnectivityRegression('Width edit degraded unattributed reference copper')
                    return [{'items': [{'uuid': uid}]} for uid in hits]
                latest['reference'] = reference
                return []
            routed_candidate = None
            rejection_counts = Counter()
            if detours:
                accepted, moving_nets, retained_nets = {}, set(), set()
                groups = {}
                if coordinated:
                    # Probe moderate rises together; this only schedules trials.
                    # Every selected edit is then checked from a clean private copy.
                    probe = {uid: width for uid, width in minima.items() if width <= 1500000}
                    movable = {uid for uid, item in tracks.items()
                               if item.Type() == pcbnew.PCB_TRACE_T and not item.IsLocked()
                               and item.GetParentGroup() is None and item.GetNetname() not in critical
                               and (uid in minima or item.GetNetClassName() in {'CORE_SIGNAL', 'FIELD_SIGNAL'})}
                    if probe:
                        snapshot.board_path.write_bytes(width_candidate(snapshot.directory, probe))
                        atlas = _drc(cli, snapshot.board_path, snapshot.directory/'width-obstacles.json',
                                     schematic_parity=True)
                        groups = coupled_neighbors(atlas, set(probe) & movable, movable)
                    snapshot.board_path.write_bytes(snapshot.original)
                if series:
                    from pcb.route_native import _configured_copper_thicknesses
                    trials = series_trials(board, minima, dict(_configured_copper_thicknesses(snapshot.original)),
                                           same_layer=same_layer_series)
                    print(f'{len(trials)} eligible trials for complete terminal runs', flush=True)
                elif shift:
                    trials = run_native(shifted_trial_sites, snapshot.directory, minima)
                    print(f'{len({uid for uid, _, _ in trials})} traces have candidate lateral shifts', flush=True)
                else:
                    ordered = sorted(groups if coordinated else minima,
                                     key=lambda uid: (len(groups.get(uid, ())), minima[uid],
                                                      -tracks[uid].GetLength(), uid))
                    trials = [(uid, minima[uid], None) for uid in ordered]
                for index, (uid, minimum, site) in enumerate(trials[:max_checks]):
                    if uid in accepted:
                        continue
                    snapshot.board_path.write_bytes(routed_candidate or snapshot.original)
                    net = str(tracks[uid].GetNetname())
                    method = 'Coordinated width' if coordinated else 'Shifted width' if shift else 'Detour'
                    location = f' to {site}' if site and not shift else ''
                    print(f'{method} {index + 1}/{min(max_checks, len(trials))}: {net}{location}; '
                          f'{len(accepted)} retained', flush=True)
                    moving_nets = retained_nets | {net}
                    if coordinated:
                        neighbors = {key: max(tracks[key].GetWidth(), minima.get(key, 0))
                                     for key in groups[uid]}
                        moving_nets.update(str(tracks[key].GetNetname()) for key in neighbors)
                        candidate = run_native(plan_coupled_widening, snapshot.directory, uid,
                                               minima[uid], neighbors, seconds_per_route)
                    elif shift:
                        candidate = run_native(plan_shifted_widening, snapshot.directory, uid, minimum, site)
                    else:
                        candidate = run_native(plan_width_reroute, snapshot.directory, uid,
                                               minimum, seconds_per_route, target_layer=site,
                                               same_layer_series=same_layer_series)
                    if candidate is None:
                        moving_nets = retained_nets.copy()
                        reason = 'shift_did_not_preserve_contacts' if shift else 'no_sampled_detour'
                        rejection_counts[reason] += 1
                        print('  Shift could not keep all original contacts' if shift
                              else '  No sampled rule-aware detour; feasibility remains unknown', flush=True)
                        continue
                    try:
                        conflicts = check(candidate)
                    except ConnectivityRegression as error:
                        conflicts = [str(error)]
                    if conflicts:
                        moving_nets = retained_nets.copy()
                        reasons = Counter(value.get('type', 'native_reference') if isinstance(value, dict)
                                          else value for value in conflicts)
                        rejection_counts.update(reasons)
                        print('  Rejected detour: ' + '; '.join(reasons), flush=True)
                        continue
                    remaining = required_widths(json.loads(output.read_bytes()))
                    if uid in remaining:
                        moving_nets = retained_nets.copy()
                        rejection_counts['required_width_unchanged'] += 1
                        continue
                    accepted.update({key: minima[key] for key in minima.keys() - remaining.keys()})
                    routed_candidate = candidate
                    retained_nets = moving_nets.copy()
                    print(f'  Retained {len(accepted)} checked detours', flush=True)
                targets, deferred = accepted, set(minima) - set(accepted)
            else:
                targets, deferred = select_widenings(targets, minima, check, max_checks=max_checks,
                                                     progress=lambda text: print(text, flush=True))
                if removes_copper or ground_stitch:
                    deferred = set(minima)-set(targets)
            if not targets:
                return {'status': 'obstructed', 'changed_tracks': 0, 'deferred_tracks': len(deferred),
                        'blockers': dict(rejection_counts)}
            # A budget exit may follow a rejected trial. Rebuild the accepted state.
            if check(routed_candidate or targets):
                raise RuntimeError('The retained width batch did not revalidate')
            after = json.loads(output.read_bytes())
            candidate = snapshot.board_path.read_bytes()
            remaining = required_widths(after)
            if set(remaining) & set(targets) or len(remaining) >= len(minima):
                raise RuntimeError('Width repair did not independently reduce native width violations')
            if protected.differences(candidate, schematic):
                raise RuntimeError('Width repair altered a protected item')
            # Native DRC refills privately. A terminal tip on a net without zones
            # does not require replacing the existing saved fills.
            output.unlink(missing_ok=True)
            final = _drc(cli, snapshot.board_path, output, save_refill=not trim_terminal_ends,
                         schematic_parity=True)
            if prune_covered:
                check_covered(targets)
            if trim_terminal_ends:
                verify_covered_edit(snapshot.original, snapshot.board_path.read_bytes(), set(targets))
            if protected.differences(snapshot.board_path.read_bytes(), schematic):
                raise RuntimeError('Native round trip altered protected content')
            final_changed_nets = {str(tracks[uid].GetNetname()) for uid in targets} if trim_terminal_ends else moving_nets
            if introduced_findings(baseline, final,
                                   stable_neck_nets=unchanged_necks if prune_covered else stable_necks-final_changed_nets,
                                   neck_regions=unchanged_neck_regions(board, load_board(snapshot.board_path))
                                   if detours else None,
                                   include_widths=spatial_change):
                raise RuntimeError('Final width-repair round trip introduced a finding')
            candidate = snapshot.board_path.read_bytes()
            changes = protected.differences(candidate, schematic)
            if changes:
                raise RuntimeError('Native round trip altered protected content: ' + ', '.join(changes))
            snapshot.check()
            if policy_path.read_bytes() != policy_bytes:
                raise RuntimeError('Branch targets changed during width repair')
            report = {'status': 'preview_checked', 'changed_tracks': len(targets),
                      'mode': 'coordinated' if coordinated else 'covered_copper_removed' if prune_covered
                      else 'redundant_terminal_ends_removed' if trim_terminal_ends
                      else 'width_with_ground_stitch' if ground_stitch else 'terminal_series' if same_layer_series
                      else 'series_relocated' if series else 'shifted' if shift
                      else 'rerouted' if reroute else 'width_only',
                      'ground_stitches': latest['ground_stitches'], 'blockers': dict(rejection_counts),
                      'width_errors_before': len(minima), 'width_errors_after': len(required_widths(final)),
                      'deferred_tracks': len(deferred), 'open_connections': len(final['unconnected_items']),
                      'deferred_uuids': sorted(deferred), 'reference': latest['reference'],
                      'manufacturing_approved': False, 'checked_utc': datetime.now(timezone.utc).isoformat()}
            tools = dict(snapshot.tool_hashes)
            if apply:
                inputs = {path.relative_to(project).as_posix(): digest for path, digest in dependencies.items()}
                checkpoint = encode_checkpoint(snapshot.original, inputs, tools, dependencies[policy_path])
                journal = {'kind': 'pre_width_repair_save', 'created_utc': report['checked_utc'],
                           'manufacturing_approved': False, 'board_before': checkpoint}
                _save(assets / 'results' / 'autoroute' / 'preimages' / 'working-save.json', journal)
        if apply:
            if Snapshot.tools() != tools:
                raise RuntimeError('Routing tools changed before saving')
            def write(path, content):
                Path(path).write_bytes(content)
                return True
            guard.save(candidate, guard.source, write, pcbnew.LoadBoard)
            report['status'] = 'saved_width_repairs'
            report['pcb_sha256'] = file_sha256(guard.source)
            _save(assets / 'results' / 'width_repair.json', report)
        return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--max-checks', type=int, default=16)
    parser.add_argument('--reroute', action='store_true', help='Try rule-aware detours instead of centerline widening')
    parser.add_argument('--coordinated', action='store_true',
                        help='Widen traces while rerouting eligible obstructing neighbors on their existing layers')
    parser.add_argument('--prune-covered', action='store_true', help='Remove flagged segments fully covered by retained pads, tracks or freshly filled copper')
    parser.add_argument('--trim-terminal-ends', action='store_true',
                        help='Remove tiny redundant tips where a proven sense branch already contacts its pad')
    parser.add_argument('--series', action='store_true',
                        help='Widen complete via-terminated runs on eligible alternate layers')
    parser.add_argument('--same-layer', action='store_true',
                        help='With --series, rebuild complete pad/via runs on their original layer')
    parser.add_argument('--ground-stitch', action='store_true',
                        help='Widen unobstructed traces and rejoin any affected ground islands')
    parser.add_argument('--shift', action='store_true',
                        help='Shift straight traces laterally while retaining all original copper contacts')
    parser.add_argument('--seconds-per-route', type=float, default=5)
    args = parser.parse_args(argv)
    with quiet_duplicate_image_handlers():
        result = repair(args.project, apply=args.apply, max_checks=args.max_checks,
                        reroute=args.reroute, seconds_per_route=args.seconds_per_route,
                        prune_covered=args.prune_covered, coordinated=args.coordinated, series=args.series,
                        ground_stitch=args.ground_stitch, same_layer_series=args.same_layer, shift=args.shift,
                        trim_terminal_ends=args.trim_terminal_ends)
    print('Width repair:', result['status'])
    for name in ('changed_tracks', 'deferred_tracks', 'width_errors_before', 'width_errors_after',
                 'open_connections', 'ground_stitches'):
        if name in result:
            print(f'  {name.replace("_", " ")}: {result[name]}')
    if result.get('blockers'):
        print('  Blockers:', ', '.join(f'{name}={count}' for name, count in sorted(result['blockers'].items())))
    print('Normal-current targets; electrical, fault-energy and manufacturing qualification remain open.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
