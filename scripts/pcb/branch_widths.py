"""Compile terminal-aware width allowances without editing schematic or copper.

Allowances describe normal-current design targets, not fault qualification.
Unknown terminals, meshed current paths and zones retain the existing rules.
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

from core.paths import asset_root
from core.toolchain import kicad_cli
from core.pcb_io import BoardSaveGuard, file_sha256, quiet_duplicate_image_handlers, typed_copper
from pcb.net_routing.native import Snapshot
from pcb.route_validation import _drc


BEGIN = '# BEGIN AUTOPCB TERMINAL WIDTHS'
END = '# END AUTOPCB TERMINAL WIDTHS'
TAP_MATCH_NM = 2


def component(graph: dict[str, set[str]], seed: str, omitted: str | None = None) -> set[str]:
    seen, pending = set(), [seed]
    while pending:
        node = pending.pop()
        if node == omitted or node in seen:
            continue
        seen.add(node)
        pending.extend(graph[node] - seen - {omitted})
    return seen


def bounded_leaves(graph: dict[str, set[str]], pads: set[str], loads: dict[str, float],
                   tracks: set[str], *,
                   contact_ports: dict[tuple[str, str], tuple[int, int]] | None = None,
                   common_landing: Callable[[str, set[str]], bool] | None = None,
                   declared_loads: set[str] | None = None,
                   include_downstream: bool = False) -> dict[str, float]:
    """Find cut tracks whose downstream terminals all have explicit load bounds.

    A unique unknown-terminal component must remain upstream. This deliberately
    rejects meshes with a second trunk attachment and floating load islands.
    With a proven trunk cut, copper entirely inside a local branch can
    share its current bound even when that branch contains a mesh.
    """
    declared_loads = set(loads) if declared_loads is None else declared_loads
    if len(graph) > 2048 or not set(loads) <= declared_loads <= pads <= set(graph):
        raise ValueError('Invalid or oversized branch connectivity graph')
    if any(not math.isfinite(v) or v < 0 for v in loads.values()):
        raise ValueError('Loads need finite nonnegative design currents')
    result = {}
    for track in sorted(tracks):
        connected = component(graph, track)
        if not (connected & pads) - declared_loads:
            continue
        pending = connected - {track}
        groups = []
        while pending:
            group = component(graph, min(pending), track)
            pending.difference_update(group)
            groups.append(group)
        roots = [group for group in groups if (group & pads) - set(loads)]
        if len(groups) < 2 or len(roots) != 1 or not (roots[0] & pads) - declared_loads:
            continue
        branches = [group for group in groups if group is not roots[0] and group & pads]
        leaves = set().union(*(group & pads for group in branches))
        if not leaves or not leaves <= loads.keys():
            continue
        # A track that bridges two upstream ports can carry trunk current.
        # Accept an ambiguous junction only when its contacts share one landing.
        upstream = graph[track] & roots[0]
        if contact_ports is not None:
            ports = {contact_ports.get((track, other)) for other in upstream}
            if (None in ports or len(ports) != 1) and not (
                    common_landing and common_landing(track, upstream)):
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


def contact_port(track, other) -> tuple[int, int] | None:
    """Prove an exact endpoint junction, rejecting overlap and interior contacts."""
    import pcbnew

    track, other = typed_copper(track), typed_copper(other)
    if not isinstance(track, pcbnew.PCB_TRACK) or isinstance(track, (pcbnew.PCB_VIA, pcbnew.PCB_ARC)):
        return None
    def point(value):
        return value.x, value.y
    a, b = point(track.GetStart()), point(track.GetEnd())
    if a == b or not other.IsOnLayer(track.GetLayer()):
        return None
    if isinstance(other, pcbnew.PCB_VIA):
        p = point(other.GetPosition())
        return p if p in (a, b) else None
    if isinstance(other, pcbnew.PAD):
        if other.GetShape(track.GetLayer()) not in {pcbnew.PAD_SHAPE_CIRCLE, pcbnew.PAD_SHAPE_RECT,
                                                  pcbnew.PAD_SHAPE_OVAL, pcbnew.PAD_SHAPE_ROUNDRECT}:
            return None
        hits = [p for p in (a, b) if other.HitTest(pcbnew.VECTOR2I(*p))]
        return hits[0] if len(hits) == 1 else None
    if not isinstance(other, pcbnew.PCB_TRACK) or isinstance(other, pcbnew.PCB_ARC):
        return None
    c, d = point(other.GetStart()), point(other.GetEnd())
    if c == d:
        return None
    def on(p, first, last):
        return ((p[0] - first[0]) * (last[1] - first[1]) == (p[1] - first[1]) * (last[0] - first[0])
                and all(min(first[i], last[i]) <= p[i] <= max(first[i], last[i]) for i in (0, 1)))
    hits = [p for p in (a, b) if on(p, c, d)]
    if len(hits) != 1:
        return None
    # Collinear copper with positive overlap can carry upstream current along
    # the candidate. A common endpoint alone does not prove a load-only spur.
    if ((b[0] - a[0]) * (d[1] - c[1]) == (b[1] - a[1]) * (d[0] - c[0])
            and any(on(p, a, b) and p not in (a, b) for p in (c, d))):
        return None
    return hits[0]


def common_copper_landing(track, upstream) -> bool:
    return retained_copper_landing((track, item) for item in upstream)


def retained_copper_landing(pairs) -> bool:
    """Confirm that the branch contacts upstream through one retained conductor.

    A secondary contact must fit completely inside the landing's copper.
    A solid graphic or a pad may underlie several touching branch segments.
    """
    import pcbnew

    def polygon(item, layer, error_mode):
        result = pcbnew.SHAPE_POLY_SET()
        item.TransformShapeToPolygon(result, layer, 0, 2000, error_mode)
        return result

    convex_pads = {pcbnew.PAD_SHAPE_CIRCLE, pcbnew.PAD_SHAPE_RECT,
                   pcbnew.PAD_SHAPE_OVAL, pcbnew.PAD_SHAPE_ROUNDRECT}
    contacts = []
    for trace, parent in pairs:
        trace, parent = typed_copper(trace), typed_copper(parent)
        layer = trace.GetLayer()
        if (trace.Type() != pcbnew.PCB_TRACE_T or trace.GetNetCode() <= 0
                or trace.GetStart() == trace.GetEnd()
                or parent.GetNetCode() != trace.GetNetCode() or not parent.IsOnLayer(layer)):
            return False
        if isinstance(parent, (pcbnew.PAD, pcbnew.PCB_VIA)):
            if parent.ConditionallyFlashed(layer) or not parent.FlashLayer(layer):
                return False
            if isinstance(parent, pcbnew.PAD) and parent.GetShape(layer) not in convex_pads:
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
        if not ((inside.Contains(start) and not outside.Contains(end))
                or (inside.Contains(end) and not outside.Contains(start))):
            continue
        if isinstance(witness, pcbnew.PCB_SHAPE):
            if witness.GetShape() != pcbnew.SHAPE_T_RECT:
                continue
        elif contact_port(trace, witness) is None:
            if witness.Type() != pcbnew.PCB_TRACE_T or witness.GetWidth() <= trace.GetWidth():
                continue
            a, b = witness.GetStart(), witness.GetEnd()
            if (end.x-start.x)*(b.y-a.y) == (end.y-start.y)*(b.x-a.x):
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


def direct_copper_graph(connectivity, items, graphics) -> dict[str, set[str]]:
    """Use KiCad's direct contacts and include contacts between copper graphics."""
    graph = {uid: set() for uid in items}
    for uid, item in items.items():
        # The whole-island GetConnectedItems query cannot establish a direct contact.
        for neighbor in (*connectivity.GetConnectedTracks(item), *connectivity.GetConnectedPads(item)):
            other = neighbor.m_Uuid.AsString()
            if other in graph and other != uid:
                graph[uid].add(other)
                graph[other].add(uid)
    for a, b in combinations(graphics, 2):
        layer = a.GetLayer()
        if layer == b.GetLayer() and a.GetEffectiveShape(layer).Collide(b.GetEffectiveShape(layer), 0):
            i, j = a.m_Uuid.AsString(), b.m_Uuid.AsString()
            graph[i].add(j)
            graph[j].add(i)
    return graph


def bounded_landing_cut(graph, items, pads, loads, tracks, declared_loads) -> float | None:
    """Prove a connected branch made of several segments sharing one landing."""
    if not tracks or not tracks <= graph.keys():
        return None
    seed = min(tracks)
    connected = component(graph, seed)
    source_pads = (connected & pads) - declared_loads
    if not source_pads or not tracks <= connected:
        return None
    retained = {uid: adjacent - tracks for uid, adjacent in graph.items() if uid not in tracks}
    upstream = component(retained, min(source_pads))
    if not source_pads <= upstream:
        return None
    isolated = connected - upstream
    leaves = isolated & pads
    if not leaves or not leaves <= loads.keys():
        return None
    if component({uid: graph[uid] & isolated for uid in isolated}, seed) != isolated:
        return None
    contacts = ((items[uid], items[neighbor]) for uid in tracks for neighbor in graph[uid] & upstream)
    if not retained_copper_landing(contacts):
        return None
    return sum(loads[pad] for pad in leaves)


def selected_pad(board, net: str, item: dict):
    part = board.FindFootprintByReference(item['reference'])
    if part is None or not str(part.GetValue()).startswith(item['value']):
        raise ValueError(f"{net}: component identity changed: {item['reference']}")
    pads = [p for p in part.Pads() if p.GetNetname() == net
            and ('pin' not in item or p.GetNumber() == item['pin'])]
    if len(pads) != 1:
        raise ValueError(f"{net}: ambiguous/missing terminal on {item['reference']}")
    return pads[0]


def quoted(value: str) -> str:
    if any(ch in value for ch in "'\"\\\r\n"):
        raise ValueError('Unsafe rule selector')
    return value


def rule(name: str, condition: str, width: float, *, precision: int = 3, layer: str = "") -> str:
    if isinstance(width, bool) or not isinstance(width, (int, float)) or not math.isfinite(width) or width < .15:
        raise ValueError('Invalid branch width')
    scope = f" (layer {layer})" if layer else ""
    return f'(rule "AutoPCB {quoted(name)}"{scope} (condition "{condition}") (constraint track_width (min {width:.{precision}f}mm)))'


def tap_selector(board, track, copper) -> str | None:
    """Bind a proven leaf to one native track despite mm conversion rounding."""
    import pcbnew

    def coordinates(item):
        a, b = item.GetStart(), item.GetEnd()
        return a.x, a.y, b.x, b.y

    start = coordinates(track)
    reverse = start[2:] + start[:2]
    # Reject even a nearby competitor outside the nominal matching box: the
    # KiCad parser can round either endpoint of that box by one native unit.
    for item in copper:
        if (item.m_Uuid == track.m_Uuid or item.GetNetCode() != track.GetNetCode()
                or item.Type() not in {pcbnew.PCB_TRACE_T, pcbnew.PCB_ARC_T}
                or item.GetLayer() != track.GetLayer()):
            continue
        actual = coordinates(item)
        if any(all(abs(a-b) <= TAP_MATCH_NM+1 for a, b in zip(actual, expected, strict=True))
               for expected in (start, reverse)):
            return None

    def limits(values):
        return ' && '.join(f'(A.{field} > {(value-TAP_MATCH_NM)/1e6:.6f}mm && '
                           f'A.{field} < {(value+TAP_MATCH_NM)/1e6:.6f}mm)'
                           for field, value in zip(('Origin_X', 'Origin_Y', 'End_X', 'End_Y'), values, strict=True))

    return (f"A.Type == 'Track' && A.NetName == '{quoted(str(track.GetNetname()))}' && "
            f"A.Layer == '{quoted(str(board.GetLayerName(track.GetLayer())))}' && "
            f'(({limits(start)}) || ({limits(reverse)}))')


def rail_rules(board, rails: list[dict]) -> list[str]:
    lines = []
    for rail in rails:
        name = quoted(rail['net'])
        expected = {selected_pad(board, name, p).m_Uuid.AsString() for p in rail['terminals']}
        actual = {p.m_Uuid.AsString() for p in board.GetPads() if p.GetNetname() == name}
        if actual != expected:
            raise ValueError(f'{name}: whole-branch terminal inventory changed')
        terminal_refs = {pad['reference'] for pad in rail['terminals']}
        rule_scope = f"A.Type == 'Track' && A.NetName == '{name}'"
        lines.append(rule(name, rule_scope, rail['width_mm']))
        if 'inner_width_mm' in rail:
            lines.append(rule(name + ' inner', rule_scope, rail['inner_width_mm'], layer='inner'))
        for width, escapes in groupby(rail.get('escapes', []), key=lambda escape: escape['width_mm']):
            references = [escape['reference'] for escape in escapes]
            if any(ref not in terminal_refs for ref in references):
                raise ValueError(f'{name}: escape is not on the documented branch')
            scopes = ' || '.join(f"A.intersectsCourtyard('{quoted(ref)}')" for ref in references)
            if len(references) == 1:
                lines.append(rule(name + ' ' + references[0] + ' escape', f"{rule_scope} && {scopes}", width))
            else:
                title = f'{name} local {width:.3f}mm escapes'
                lines.append(rule(title, f"{rule_scope} && ({scopes})", width))
    return lines


def compile_widths(board, config: dict) -> tuple[str, dict]:
    import pcbnew

    if config.get('schema') != 1 or config.get('electrically_verified') is not False:
        raise ValueError('Branch targets must retain their unqualified electrical status')
    for item in config.get('required_components', []):
        part = board.FindFootprintByReference(item['reference'])
        if part is None or not str(part.GetValue()).startswith(item['value']):
            raise ValueError(f"Current-setting component changed: {item['reference']}")
    lines, counts = [BEGIN, '# Normal-current targets; fault/thermal/physical qualification remains open.'], {}
    connectivity = board.GetConnectivity()
    connectivity.Build(board)
    lines.extend(rail_rules(board, config.get('rails', [])))
    tap_groups, declared_loads = [], defaultdict(set)
    for tap in config.get('taps', []):
        name = quoted(tap['net'])
        loads = {selected_pad(board, name, item).m_Uuid.AsString(): item['max_current_a']
                 for item in tap['terminals']}
        tap_groups.append((name, tap, loads))
        declared_loads[name].update(loads)
    graphics_by_net = defaultdict(list)
    footprint_graphics = chain.from_iterable(part.GraphicalItems() for part in board.GetFootprints())
    for shape in chain(board.GetDrawings(), footprint_graphics):
        if isinstance(shape, pcbnew.PCB_SHAPE) and shape.GetNetCode() > 0 and pcbnew.IsCopperLayer(shape.GetLayer()):
            graphics_by_net[shape.GetNetname()].append(shape)
    for name, tap, loads in tap_groups:
        if any(z.GetNetname() == name and not z.GetIsRuleArea() for z in board.Zones()):
            raise ValueError(f'{name}: zone-mediated branch flow needs a different model')
        parts = [p for p in board.GetPads() if p.GetNetname() == name]
        copper = [t for t in board.GetTracks() if t.GetNetname() == name]
        parts_by_id = {p.m_Uuid.AsString(): p for p in parts}
        graphics = graphics_by_net[name]
        items = parts_by_id | {t.m_Uuid.AsString(): t for t in chain(copper, graphics)}
        graph = direct_copper_graph(connectivity, items, graphics)
        tracks = {t.m_Uuid.AsString() for t in copper if not isinstance(t, (pcbnew.PCB_VIA, pcbnew.PCB_ARC))}
        ports = {(uid, neighbor): port for uid in tracks for neighbor in graph[uid]
                 if (port := contact_port(items[uid], items[neighbor])) is not None}
        landing_proofs = set()

        def verify_landing(uid, parents):
            if not common_copper_landing(items[uid], (items[parent] for parent in parents)):
                return False
            landing_proofs.add(uid)
            return True

        floors = tap.get('track_width_floors_mm')
        if floors is not None and (not isinstance(floors, dict) or not floors or any(
                not isinstance(width, (int, float)) or isinstance(width, bool)
                or not math.isfinite(width) or width < tap['width_mm'] for width in floors.values())):
            raise ValueError(f'{name}: invalid local track width floors')
        selected = bounded_leaves(graph, set(parts_by_id), loads, tracks,
                                  contact_ports=ports, common_landing=verify_landing,
                                  declared_loads=declared_loads[name], include_downstream=floors is not None)
        eligible = {uid for uid, current in selected.items() if current <= tap['max_branch_a']}
        if floors is not None and (unproved := set(floors) - eligible):
            cut = set(floors)
            current = bounded_landing_cut(graph, items, set(parts_by_id), loads, cut, declared_loads[name]) if cut <= tracks else None
            if current is None or current > tap['max_branch_a']:
                refs = ', '.join(str(p['reference']) for p in tap['terminals'])
                raise ValueError(f'{name} ({refs}): local load path unproven for {", ".join(sorted(unproved))}')
            eligible.update(cut)
        counts.setdefault(name, 0)
        for uid in sorted(eligible if floors is None else floors):
            condition = tap_selector(board, items[uid], copper)
            width = items[uid].GetWidth()/1e6
            if floors is not None and (condition is None or width < floors[uid]):
                raise ValueError(f'{name}: local width floor or unique geometry changed for {uid}')
            if condition is None:
                continue
            counts[name] += 1
            if floors is not None:
                lines.append(rule(name + ' tap ' + uid, condition, floors[uid], precision=6))
            elif uid in landing_proofs or tap.get('preserve_installed_width', False):
                floor = max(tap['width_mm'], width)
                lines.append(rule(name + ' tap ' + uid, condition, floor, precision=6))
            else:
                lines.append(rule(name + ' tap ' + uid, condition, tap['width_mm']))
    return '\n'.join((*lines, END, '')), counts


def replace_managed(source: str, block: str) -> str:
    if source.count(BEGIN) != source.count(END) or source.count(BEGIN) > 1:
        raise ValueError('Malformed managed width rules')
    if BEGIN in source:
        start, end = source.index(BEGIN), source.index(END) + len(END)
        return source[:start] + block.rstrip() + source[end:]
    return source.rstrip() + '\n\n' + block


def check_unchanged_other_rules(before: dict, after: dict) -> None:
    def findings(report):
        return Counter((v['type'], v['severity'], tuple(sorted(i['uuid'] for i in v.get('items', []))))
                       for v in report['violations'] if v['type'] not in {'track_width', 'connection_width'})
    new = findings(after) - findings(before)
    if new or after['schematic_parity'] != before['schematic_parity']:
        kinds = Counter()
        for (kind, _, _), count in new.items():
            kinds[kind] += count
        raise RuntimeError(f'Branch-rule edit changed unrelated findings: {dict(kinds)}')
    if len(after['unconnected_items']) != len(before['unconnected_items']):
        raise RuntimeError('Width-only rule edit changed connectivity')


def main(argv=None) -> int:
    import pcbnew

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    project = args.project.resolve()
    policy = asset_root(project) / 'config' / 'branch_widths.json'
    policy_hash = file_sha256(policy)
    config = json.loads(policy.read_text(encoding='utf-8'))
    target = native_path(project, ".kicad_dru")
    with quiet_duplicate_image_handlers(), Snapshot(native_path(project, ".kicad_pcb")) as snapshot:
        guard = BoardSaveGuard(snapshot.source, tuple(snapshot.fingerprints.items()) + ((policy, policy_hash),))
        board = pcbnew.LoadBoard(str(snapshot.board_path))
        block, counts = compile_widths(board, config)
        original = target.read_text(encoding='utf-8')
        candidate = replace_managed(original, block)
        if candidate == original:
            print('Branch-width rules already match the current terminal topology')
            return 0
        cli = kicad_cli()
        print('Checking original and terminal-aware rules on the same private PCB', flush=True)
        before = _drc(cli, snapshot.board_path, snapshot.directory / 'before.json', schematic_parity=True)
        snapshot.board_path.with_suffix('.kicad_dru').write_text(candidate, encoding='utf-8')
        after = _drc(cli, snapshot.board_path, snapshot.directory / 'after.json', schematic_parity=True)
        check_unchanged_other_rules(before, after)
        print('Proven leaf tracks:', sum(counts.values()), counts)
        print('Track-width findings:', sum(v['type'] == 'track_width' for v in before['violations']),
              '->', sum(v['type'] == 'track_width' for v in after['violations']))
        snapshot.check()
    if args.apply:
        guard.verify_unchanged()
        guard.verify_editor_unlocked()
        fd, path = tempfile.mkstemp(prefix='.width-rules-', suffix='.tmp', dir=target.parent)
        staged = Path(path)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(candidate.encode('utf-8'))
                stream.flush()
                os.fsync(stream.fileno())
            guard.verify_unchanged()
            guard.verify_editor_unlocked()
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)
        print('Applied branch rules; schematic, placement and copper were not edited')
    else:
        print('Preview only; use --apply to publish the checked rules')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
