"""Repair reported copper contacts and local keepout crossings."""

from __future__ import annotations

from core.project import native_path

import argparse
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
from itertools import combinations
import hashlib
import json
from pathlib import Path
import sys
import time

from core.kicad_campaign import CampaignPlanner
from core.paths import asset_root
from core.pcb_io import BoardSaveGuard, copper_boundary, quiet_duplicate_image_handlers


MAX_LOCAL_LAUNCH_MM = 3.0
MAX_ADDED_LENGTH_MM = 0.25


def _launch_vias(board, tracks: tuple, endpoints: tuple) -> tuple:
    import pcbnew

    return tuple(via for via in copper_boundary(board, tracks).values()
                 if isinstance(via, pcbnew.PCB_VIA) and via.GetPosition() in endpoints)


def keepout_proposal(directory: Path, report: dict) -> tuple[bytes, dict]:
    """Find a bounded, constant-width detour for a native high-speed finding."""
    import pcbnew
    from pcb.net_routing.pair_probe import _pairs
    from pcb.net_routing.reference import launch_reference_layer, reference_corridors
    from pcb.net_routing.rules import RoutingRules
    from pcb.route_cli import campaign_changes
    from pcb.route_native import read_native_obstacles
    from pcb.route_optimizer import (GridRouter, NoGridRoute, Point, SearchBudgetExceeded,
                                     SearchLimitReached, mm)
    from pcb.width_repair import route_headings, width_chain

    path = native_path(directory, ".kicad_pcb")
    original = path.read_bytes()
    board = pcbnew.LoadBoard(str(path))
    targets = {item['uuid'] for finding in report['violations']
               if finding['type'] == 'items_not_allowed' for item in finding['items']}
    if not targets:
        return original, {'keepouts_repaired': 0}
    pairs, critical = _pairs(board, path.with_suffix('.kicad_pro').read_bytes())
    by_net = {net: pair for pair in pairs for net in (pair.positive, pair.negative)}
    rules = RoutingRules.from_board(board, path.with_suffix('.kicad_dru'))
    for track in board.GetTracks():
        key, net = track.m_Uuid.AsString(), str(track.GetNetname())
        layer_id = track.GetLayer()
        if (key not in targets or net not in by_net or track.Type() != pcbnew.PCB_TRACE_T
                or layer_id not in (pcbnew.F_Cu, pcbnew.B_Cu)
                or track.IsLocked() or track.GetParentGroup() is not None):
            continue
        chain, a, b = width_chain(board, track)
        length = sum(item.GetLength() for item in chain) / 1e6
        if not 0 < length <= MAX_LOCAL_LAUNCH_MM:
            continue
        vias = _launch_vias(board, chain, (a, b))
        contacts = copper_boundary(board, chain)
        if not all(any((isinstance(item, pcbnew.PCB_VIA) and item.GetPosition() == point)
                       or (isinstance(item, pcbnew.PAD) and item.IsOnLayer(layer_id) and item.HitTest(point))
                       for item in contacts.values()) for point in (a, b)):
            continue
        removed = frozenset(item.m_Uuid.AsString() for item in chain)
        removals = [{'kind': 'segment', 'uuid': uid} for uid in sorted(removed)]
        native = read_native_obstacles(path, net, assume_refill=True, removals=removals)
        pair = by_net[net]
        mate = pair.negative if net == pair.positive else pair.positive
        # Suppress only the moving net's synthetic guards, which otherwise
        # block its own fixed launch. Native pads, tracks, vias and rule areas
        # remain. The actual adjacent ground polygon is checked below.
        physical = native.obstacles[:len(native.obstacles)-native.reference_corridor_count]
        obstacles = tuple(replace(item, min_track_clearance=max(item.min_track_clearance, mm(pair.gap_mm)))
                          if item.net == mate else item for item in physical)
        obstacles += reference_corridors(board, critical-{net}).obstacles
        layer, width = str(board.GetLayerName(layer_id)), track.GetWidth()
        profile = rules.profile(net, width, mm(.2), width_floor=width)
        initial, final = route_headings(board, track, removed_ids=removed, endpoints=(a, b))
        margin = mm(1.5)
        lower = Point(min(a.x, b.x)-margin, min(a.y, b.y)-margin)
        upper = Point(max(a.x, b.x)+margin, max(a.y, b.y)+margin)
        deadline = time.monotonic()+6.0

        def budget():
            if time.monotonic() >= deadline:
                raise SearchBudgetExceeded('Local keepout repair reached its time limit')

        def covered(route):
            if route.vias or route.reused_vias or abs(route.length_mm-length) > MAX_ADDED_LENGTH_MM:
                return False
            geometry = []
            for segment in route.segments:
                if any(w != width for _, w in profile.pieces(segment)):
                    return False
                item = pcbnew.PCB_TRACK(board)
                item.SetLayer(layer_id)
                item.SetNetCode(track.GetNetCode())
                item.SetWidth(width)
                item.SetStart(pcbnew.VECTOR2I(segment.start.x, segment.start.y))
                item.SetEnd(pcbnew.VECTOR2I(segment.end.x, segment.end.y))
                geometry.append(item)
            return launch_reference_layer(board, board, tuple(geometry), vias) is not None

        try:
            router = GridRouter(lower, upper, mm(.05), (layer,), net, width, mm(.2), mm(.6), mm(.3),
                                obstacles, max_expansions=25000, max_cells=300000, allow_new_vias=False,
                                allowed_reused_vias=set(), gentle_turns=True, forbid_staircase=True,
                                first_feasible=True, profile=profile, check_budget=budget,
                                initial_directions=initial, final_directions=final)
            route = router.route(Point(a.x, a.y), layer, Point(b.x, b.y), layer, candidate_check=covered)
        except (NoGridRoute, SearchBudgetExceeded, SearchLimitReached):
            continue
        candidate = CampaignPlanner(directory).plan('pcb', campaign_changes(
            route, net, width, mm(.6), mm(.3), removals)).after
        return candidate, {'net': net, 'pair_mate': mate, 'removed_segments': sorted(removed),
                           'endpoints': [[a.x, a.y], [b.x, b.y]], 'before_length_mm': length,
                           'after_length_mm': route.length_mm, 'keepouts_repaired': 1}
    return original, {'keepouts_repaired': 0}


def verify_keepout_edit(before, after, proposal: dict) -> str:
    """Recompute route contacts and reference from the native refilled board."""
    import pcbnew
    from pcb.net_routing.reference import launch_reference_layer
    from pcb.net_routing.style import audit_style
    from pcb.width_repair import verify_routing_style

    old = {x.m_Uuid.AsString(): x for x in before.GetTracks()}
    new = {x.m_Uuid.AsString(): x for x in after.GetTracks()}
    removed = set(proposal['removed_segments'])
    if old.keys()-new.keys() != removed:
        raise RuntimeError('Keepout repair did not preserve the retained copper identities')
    chain = tuple(old[key] for key in sorted(removed))
    inserted = tuple(new[key] for key in sorted(new.keys()-old.keys()))
    if not inserted or any(x.Type() != pcbnew.PCB_TRACE_T or x.GetNetname() != proposal['net']
                           or x.GetLayer() != chain[0].GetLayer() or x.GetWidth() != chain[0].GetWidth()
                           for x in inserted):
        raise RuntimeError('Keepout repair changed the signal width, layer, or via count')
    if copper_boundary(before, chain).keys() != copper_boundary(after, inserted).keys():
        raise RuntimeError('Keepout repair changed the local native copper contacts')
    old_length, new_length = (sum(x.GetLength() for x in group)/1e6 for group in (chain, inserted))
    if abs(new_length-old_length) > MAX_ADDED_LENGTH_MM:
        raise RuntimeError('Keepout repair changed more than the bounded launch length')
    verify_routing_style(audit_style(before), audit_style(after))
    ends = tuple(pcbnew.VECTOR2I(*p) for p in proposal['endpoints'])
    reference = launch_reference_layer(before, after, inserted, _launch_vias(before, chain, ends))
    if reference is None:
        raise RuntimeError('Refilled board does not preserve continuous local reference copper')
    return reference


def center_solution(center: tuple[int, int], tracks: list, radius: int):
    """Intersect existing H/V/45-degree support lines in exact integer units."""
    lines, endpoints, candidates = [], {}, {center}
    for uid, start, end in tracks:
        dx, dy = end[0] - start[0], end[1] - start[1]
        if not (dx or dy) or (dx and dy and abs(dx) != abs(dy)):
            return None
        a, b = (1, 0) if not dx else (0, 1) if not dy else (1, -1 if dx * dy > 0 else 1)
        lines.append((a, b, a * start[0] + b * start[1]))
        distances = [(sum((p[i] - center[i]) ** 2 for i in (0, 1)), name, p)
                     for name, p in (('start', start), ('end', end))]
        distance, name, point = min(distances)
        if distance > radius ** 2:
            return None
        endpoints[uid] = name
        candidates.add(point)
    for (a, b, c), (d, e, f) in combinations(lines, 2):
        determinant = a * e - b * d
        if determinant and (c * e - b * f) % determinant == 0 and (a * f - c * d) % determinant == 0:
            candidates.add(((c * e - b * f) // determinant, (a * f - c * d) // determinant))
    feasible = [p for p in candidates if sum((p[i] - center[i]) ** 2 for i in (0, 1)) <= radius ** 2
                and all(a * p[0] + b * p[1] == c for a, b, c in lines)
                and all(p != (end if endpoints[uid] == 'start' else start) for uid, start, end in tracks)]
    if not feasible:
        return None
    return min(feasible, key=lambda p: (sum((p[i] - center[i]) ** 2 for i in (0, 1)), p)), endpoints


def proposal(directory: Path, report: dict) -> tuple[bytes, dict]:
    import pcbnew
    from core.pcb_io import copper_width
    from pcb.net_routing.style import audit_style

    source = native_path(directory, ".kicad_pcb")
    original = source.read_bytes()
    board = pcbnew.LoadBoard(str(source))
    items = {item.m_Uuid.AsString(): item for item in board.GetTracks()}
    connectivity = board.GetConnectivity()
    connectivity.Build(board)
    grouped = {x.m_Uuid.AsString() for group in board.Groups() for x in group.GetItems()}
    changes, edited, centered, removed = [], set(), 0, 0
    reported_vias = {i['uuid'] for finding in report['violations'] if finding['type'] == 'track_not_centered_on_via'
                     for i in finding.get('items', []) if isinstance(items.get(i['uuid']), pcbnew.PCB_VIA)}
    dangling = {i['uuid'] for finding in report['violations'] if finding['type'] == 'via_dangling'
                for i in finding.get('items', [])}
    def replace(uid, field, point):
        key = uid, field
        if key not in edited:
            edited.add(key)
            changes.append({'op': 'replace', 'target': {'kind': 'segment', 'uuid': uid},
                            'path': [field], 'expression': f'({field} {point[0] / 1e6:.6f} {point[1] / 1e6:.6f})'})
    for uid in sorted(dangling | reported_vias):
        via = items.get(uid)
        if not isinstance(via, pcbnew.PCB_VIA) or via.IsLocked() or uid in grouped:
            continue
        tracks = tuple(connectivity.GetConnectedTracks(via))
        if (not tracks or connectivity.GetConnectedPads(via)
                or any(not isinstance(x, pcbnew.PCB_TRACK) or isinstance(x, (pcbnew.PCB_VIA, pcbnew.PCB_ARC))
                       or x.IsLocked() or x.m_Uuid.AsString() in grouped for x in tracks)):
            continue
        if uid in dangling and len({x.GetLayer() for x in tracks}) == 1 and via.GetNetname() != 'GND':
            changes.append({'op': 'remove', 'target': {'kind': 'via', 'uuid': uid}})
            removed += 1
            continue
        if uid not in reported_vias:
            continue
        center = via.GetPosition()
        entries = [(x.m_Uuid.AsString(), (x.GetStart().x, x.GetStart().y), (x.GetEnd().x, x.GetEnd().y)) for x in tracks]
        solution = center_solution((center.x, center.y), entries,
                                   min(300000, copper_width(via, layer=via.GetLayer()) // 2))
        if solution is None:
            continue
        point, endpoints = solution
        if point != (center.x, center.y):
            changes.append({'op': 'set_at', 'target': {'kind': 'via', 'uuid': uid},
                            'x': point[0] / 1e6, 'y': point[1] / 1e6})
        for track in tracks:
            name = endpoints[track.m_Uuid.AsString()]
            end = track.GetStart() if name == 'start' else track.GetEnd()
            if point != (end.x, end.y):
                replace(track.m_Uuid.AsString(), name, point)
        centered += 1
    if not changes:
        return original, {'centered_vias': 0, 'removed_dangling_vias': 0}
    candidate = CampaignPlanner(directory).plan('pcb', changes).after
    temporary = directory / 'contact-check.kicad_pcb'
    try:
        temporary.write_bytes(candidate)
        checked = pcbnew.LoadBoard(str(temporary))
        before_style, after_style = asdict(audit_style(board)), asdict(audit_style(checked))
        for field in before_style:
            old = Counter(json.dumps(x, sort_keys=True) for x in before_style[field])
            new = Counter(json.dumps(x, sort_keys=True) for x in after_style[field])
            if new - old:
                raise RuntimeError('Contact cleanup introduced routing-style defects: ' + field)
    finally:
        temporary.unlink(missing_ok=True)
    return candidate, {'centered_vias': centered, 'removed_dangling_vias': removed}


def repair(project: Path, *, apply: bool = False, keepouts: bool = False) -> dict:
    import pcbnew
    from pcb.branch_widths import compile_widths
    from pcb.net_routing.controller import encode_checkpoint
    from pcb.net_routing.isolation import run_native
    from pcb.net_routing.native import Snapshot
    from pcb.net_routing.protection import ProtectedState
    from pcb.net_routing.reference import high_speed_nets
    from pcb.net_routing.runner import _exclusive, _save, _verify_cumulative_reference_native
    from pcb.route_validation import _drc
    from pcb.width_repair import introduced_findings, verify_tap_proofs

    assets = asset_root(project)
    policy = assets / 'config' / 'branch_widths.json'
    policy_bytes = policy.read_bytes()
    policy_hash = hashlib.sha256(policy_bytes).hexdigest()
    config = json.loads(policy_bytes)
    cli = Path(sys.executable).with_name('kicad-cli.exe')
    with _exclusive(assets / 'results' / 'autoroute' / 'active.lock'):
        with Snapshot(native_path(project, ".kicad_pcb")) as snapshot:
            guard = BoardSaveGuard(snapshot.source, tuple(snapshot.fingerprints.items()) + ((policy, policy_hash),))
            guard.verify_editor_unlocked()
            label = 'local keepout crossings' if keepouts else 'reported contacts and redundant vias'
            print(f'Checking {label} on a private PCB', flush=True)
            before = _drc(cli, snapshot.board_path, snapshot.directory / 'before.json',
                          schematic_parity=True, save_refill=keepouts)
            filled_source = snapshot.board_path.read_bytes()
            candidate, counts = run_native(keepout_proposal if keepouts else proposal, snapshot.directory, before)
            if candidate == filled_source:
                return {'status': 'no_eligible_contact_repairs', **counts}
            original_board = pcbnew.LoadBoard(str(snapshot.board_path))
            before_rules, _ = compile_widths(original_board, config)
            prior = snapshot.directory / 'prior.kicad_pcb'
            prior.write_bytes(snapshot.original)
            snapshot.board_path.write_bytes(candidate)
            after = _drc(cli, snapshot.board_path, snapshot.directory / 'after.json',
                         schematic_parity=True, save_refill=keepouts)
            candidate_board = pcbnew.LoadBoard(str(snapshot.board_path))
            after_rules, _ = compile_widths(candidate_board, config)
            verify_tap_proofs(before_rules, after_rules)
            zones = {str(z.GetNetname()) for z in original_board.Zones() if not z.GetIsRuleArea()}
            nets = frozenset(str(n) for n in original_board.GetNetsByName() if str(n) and str(n) not in zones)
            conflicts = introduced_findings(before, after, stable_neck_nets=nets)
            def widths(report):
                return Counter((v['severity'], v['description'], tuple(sorted(i['uuid'] for i in v['items'])))
                               for v in report['violations'] if v['type'] == 'track_width')
            if conflicts or widths(after) - widths(before):
                raise RuntimeError('Contact cleanup introduced native violations')
            if keepouts:
                from core.kicad_sexpr import parse
                from pcb.route_validation import _canonical, _zone_definition

                candidate = snapshot.board_path.read_bytes()
                def forms(data, names):
                    root = parse(data.decode('utf-8-sig'))
                    return {form.children('uuid')[0].atoms()[1].value: (
                        _zone_definition(form) if form.name == 'zone' else _canonical(form))
                        for name in names for form in root.children(name)}
                original_copper = forms(filled_source, ('segment', 'arc', 'via', 'zone'))
                current_copper = forms(candidate, ('segment', 'arc', 'via', 'zone'))
                if any(current_copper.get(uid) != geometry for uid, geometry in original_copper.items()
                       if uid not in counts['removed_segments']):
                    raise RuntimeError('Keepout repair changed fixed copper or zone definitions')
                counts['reference_layer'] = verify_keepout_edit(original_board, candidate_board, counts)
                old_keepouts = sum(v['type'] == 'items_not_allowed' for v in before['violations'])
                new_keepouts = sum(v['type'] == 'items_not_allowed' for v in after['violations'])
                if old_keepouts - new_keepouts != counts['keepouts_repaired']:
                    raise RuntimeError('Keepout repair did not remove its native finding')
                counts['keepout_errors_after'] = new_keepouts
                counts['electrically_verified'] = False
            critical = high_speed_nets(snapshot.board_path.with_suffix('.kicad_pro').read_bytes(),
                                       {str(n) for n in original_board.GetNetsByName() if str(n)})
            reference = run_native(_verify_cumulative_reference_native, prior, snapshot.board_path, tuple(sorted(critical)))
            if reference['status'] == 'degraded':
                raise RuntimeError('Contact cleanup degraded sampled reference copper')
            schematic = snapshot.source.with_suffix('.kicad_sch').read_bytes()
            protected = ProtectedState.capture(snapshot.original, schematic, preserve_all_placement=True)
            if protected.differences(candidate, schematic):
                raise RuntimeError('Contact cleanup changed protected design items')
            result = {'status': 'checked', **counts, 'opens': len(after['unconnected_items']),
                      'warnings_before': sum(v['severity'] == 'warning' for v in before['violations']),
                      'warnings_after': sum(v['severity'] == 'warning' for v in after['violations'])}
            if not keepouts and result['warnings_after'] >= result['warnings_before']:
                raise RuntimeError('Contact cleanup did not reduce native warnings')
            snapshot.check()
            tools = snapshot.tool_hashes
            if apply:
                inputs = {p.relative_to(project).as_posix(): digest for p, digest in guard.fingerprints}
                _save(assets / 'results' / 'autoroute' / 'preimages' / 'working-save.json', {
                    'kind': 'pre_drc_keepout_save' if keepouts else 'pre_drc_contact_save',
                    'created_utc': datetime.now(timezone.utc).isoformat(),
                    'manufacturing_approved': False, 'board_before': encode_checkpoint(
                        snapshot.original, inputs, tools, policy_hash)})
        if apply:
            if Snapshot.tools() != tools:
                raise RuntimeError('Tools changed before contact publication')
            def write(path, content):
                Path(path).write_bytes(content)
                return True
            guard.save(candidate, guard.source, write, pcbnew.LoadBoard)
            result['status'] = 'saved'
        return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--keepouts', action='store_true', help='Repair one local high-speed keepout crossing')
    args = parser.parse_args(argv)
    with quiet_duplicate_image_handlers():
        result = repair(args.project.resolve(), apply=args.apply, keepouts=args.keepouts)
    print('DRC repair:', result['status'])
    for key, value in result.items():
        if key not in {'status', 'removed_segments', 'endpoints'}:
            print(f'  {key.replace("_", " ")}: {value}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
