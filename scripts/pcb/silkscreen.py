"""Apply configured silkscreen placement and edge-clearance repairs only."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fnmatch
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

from core.kicad_campaign import CampaignPlanner
from core.kicad_sexpr import Form, parse, quote
from core.paths import asset_root
from core.pcb_io import BoardSaveGuard, file_sha256, quiet_duplicate_image_handlers
from pcb.route_validation import _canonical, _drc


def nonsilkscreen(content: bytes) -> tuple:
    """Canonicalize every protected item, omitting only the two silk layers."""
    def walk(item):
        if not isinstance(item, Form):
            return _canonical(item)
        layers = item.children('layer')
        if layers and layers[0].atoms()[1].value in {'F.SilkS', 'B.SilkS'}:
            return None
        return tuple(value for child in item.items if (value := walk(child)) is not None)
    return walk(parse(content.decode('utf-8-sig')))


def verify_silkscreen_only(before: bytes, after: bytes) -> None:
    if nonsilkscreen(before) != nonsilkscreen(after):
        raise RuntimeError('Silkscreen edit changed non-silkscreen design items')


def verify_top_text_alignment(before: bytes, after: bytes) -> None:
    """Only the pose and justification of board-level front text may change."""
    def signature(data):
        result = []
        for item in parse(data.decode('utf-8-sig')).items:
            if (not isinstance(item, Form) or item.name != 'gr_text'
                    or item.children('layer')[0].atoms()[1].value != 'F.SilkS'):
                result.append(_canonical(item))
                continue
            parts = []
            for field in item.items:
                if isinstance(field, Form) and field.name == 'at':
                    continue
                if isinstance(field, Form) and field.name == 'effects':
                    parts.append(tuple(_canonical(value) for value in field.items
                                       if not isinstance(value, Form) or value.name != 'justify'))
                else:
                    parts.append(_canonical(field))
            result.append(tuple(parts))
        return tuple(result)
    if signature(before) != signature(after):
        raise RuntimeError('Top label alignment changed content, geometry or another layer')


def plan_top_text(project: Path, policy: dict) -> tuple[bytes, dict[str, int]]:
    """Set connector label banks from the saved footprints, with fixed line spacing."""
    import pcbnew

    original = (project / 'v4.1.kicad_pcb').read_bytes()
    source = original.decode('utf-8-sig')
    board = pcbnew.LoadBoard(str(project / 'v4.1.kicad_pcb'))
    pitch = round(policy['line_pitch_mm'] * 1e6)
    gap = round(policy['gap_mm'] * 1e6)
    if not 1250000 <= pitch <= 2400000 or not 250000 <= gap <= 1200000:
        raise ValueError('Top label spacing is outside the supported range')
    text_items = [item for item in board.GetDrawings()
                  if isinstance(item, pcbnew.PCB_TEXT) and item.GetLayer() == pcbnew.F_SilkS]
    forms = {form.children('uuid')[0].atoms()[1].value: form
             for form in parse(source).children('gr_text')}
    used, changes, moved = set(), [], 0

    def label(name, nearby=None):
        matches = [item for item in text_items if str(item.GetText()) == name
                   and item.m_Uuid.AsString() not in used]
        if nearby is not None:
            pos = nearby.GetPosition()
            matches.sort(key=lambda item: (item.GetPosition().x-pos.x)**2 + (item.GetPosition().y-pos.y)**2)
            matches = [item for item in matches
                       if item.GetTextAngle().AsDegrees() == nearby.GetTextAngle().AsDegrees()] or matches
            matches = matches[:1]
        if len(matches) != 1:
            raise ValueError(f'Missing or ambiguous front label: {name}')
        used.add(matches[0].m_Uuid.AsString())
        return matches[0]

    def set_text(item, x, y, left):
        nonlocal moved
        # Snap the anchor, not the footprint, to the text layout grid.
        x, y = (round(v/50000)*50000 for v in (x, y))
        uid = item.m_Uuid.AsString()
        target = {'kind': 'gr_text', 'uuid': uid}
        at = item.GetPosition()
        if (at.x, at.y, item.GetTextAngle().AsDegrees()) != (x, y, 0):
            changes.append({'op': 'replace', 'target': target, 'path': ['at'],
                            'expression': f'(at {x/1e6:.6f} {y/1e6:.6f} 0)'})
            moved += 1
        effects = forms[uid].children('effects')[0]
        justification = effects.children('justify')
        desired = ['left'] if left else []
        existing = [atom.value for atom in justification[0].atoms()[1:]] if justification else []
        if existing != desired:
            if set(existing) - {'left', 'right', 'top', 'bottom'}:
                raise ValueError('Unexpected mirrored or unsupported label justification')
            raw = source[effects.start:effects.end]
            if justification:
                field = justification[0]
                raw = raw[:field.start-effects.start] + raw[field.end-effects.start:]
            if left:
                raw = raw[:-1] + ' (justify left))'
            changes.append({'op': 'replace', 'target': target, 'path': ['effects'], 'expression': raw})

    for bank in policy['banks']:
        mode = bank['side']
        parts = [board.FindFootprintByReference(ref) for ref, _ in bank['items']]
        if not parts or any(part is None for part in parts) or mode not in {'right', 'above', 'below', 'fixed'}:
            raise ValueError('Invalid top label bank or missing connector')
        boxes = [part.GetBoundingBox(False, False) for part in parts]
        common_x = max(box.GetRight() for box in boxes) + gap
        first_above = min(box.GetY() for box in boxes) - gap - 500000 - pitch
        first_below = max(box.GetBottom() for box in boxes) + gap + 500000
        for (ref, caption), box in zip(bank['items'], boxes, strict=True):
            title = label(ref)
            role = label(caption, title)
            if mode == 'right':
                x = common_x
                y = box.GetY() + 250000 if bank.get('start_near_top') else (box.GetY()+box.GetBottom()-pitch)/2
            elif mode == 'fixed':
                x, y = (round(v*1e6) for v in bank['first_mm'])
            else:
                x = (box.GetX()+box.GetRight())/2
                y = first_above if mode == 'above' else first_below
            y += round(bank.get('vertical_bias_mm', 0)*1e6)
            if bank.get('inline'):
                if mode not in {'above', 'below'}:
                    raise ValueError('Inline labels need a centered connector bank')
                def ink_width(item):
                    angle = item.GetTextAngle()
                    item.SetTextAngle(pcbnew.EDA_ANGLE(0, pcbnew.DEGREES_T))
                    width = item.GetBoundingBox().GetWidth()
                    item.SetTextAngle(angle)
                    return width
                first, second = ink_width(title), ink_width(role)
                spacer = 400000
                left = x-(first+second+spacer)/2
                set_text(title, left, y, True)
                set_text(role, left+first+spacer, y, True)
            else:
                for index, item in enumerate((title, role)):
                    set_text(item, x, y + index*pitch, mode in {'right', 'fixed'})
    candidate = CampaignPlanner(project).plan('pcb', changes).after if changes else original
    verify_top_text_alignment(original, candidate)
    return candidate, {'aligned_labels': len(used), 'moved_texts': moved}


def restore_stock_silk(content: bytes, bindings: list[dict]) -> bytes:
    """Restore only top-side footprint strokes, leaving pads and models untouched."""
    source = content.decode('utf-8')
    parts = {p.atoms()[2].value: f for f in parse(source).children('footprint')
             for p in f.children('property') if p.atoms()[1].value == 'Reference'}
    edits = []
    for binding in bindings:
        footprint = parts[binding['reference']]
        if footprint.children('layer')[0].atoms()[1].value != 'F.Cu':
            raise ValueError('Stock silk restore currently supports top footprints')
        text = Path(binding['path']).read_text(encoding='utf-8')
        def lines(root):
            return [f for f in root.children('fp_line')
                    if f.children('layer')[0].atoms()[1].value == 'F.SilkS']
        old, new = lines(footprint), lines(parse(text))
        if len(old) != len(new):
            raise ValueError('Stock silk topology differs; review the footprint first')
        for prior, stock in zip(old, new, strict=True):
            uid = prior.children('uuid')[0].atoms()[1].value
            ids = stock.children('uuid')
            if ids:
                replacement = text[stock.start:ids[0].start] + '(uuid ' + quote(uid) + ')' + text[ids[0].end:stock.end]
            else:
                replacement = text[stock.start:stock.end - 1] + '\n(uuid ' + quote(uid) + '))'
            edits.append((prior.start, prior.end, replacement))
    for start, end, replacement in sorted(edits, reverse=True):
        source = source[:start] + replacement + source[end:]
    result = source.encode('utf-8')
    verify_silkscreen_only(content, result)
    return result


def sync_silk_libraries(project: Path, before: bytes, after: bytes, policy: dict) -> dict[Path, bytes]:
    """Keep existing local library artwork synchronized after symmetric clipping."""
    def parts(content):
        return {p.atoms()[2].value: f for f in parse(content.decode()).children('footprint')
                for p in f.children('property') if p.atoms()[1].value == 'Reference'}
    old, new, result = parts(before), parts(after), {}
    def bounds(footprint):
        points = [[float(a.value) for a in point.atoms()[1:]]
                  for line in footprint.children('fp_line')
                  if 'SilkS' in line.children('layer')[0].atoms()[1].value
                  for name in ('start', 'end') for point in line.children(name)]
        return tuple(max(abs(p[axis]) for p in points) for axis in (0, 1))
    for binding in policy.get('library_silk_sync', []):
        reference = binding['reference']
        if bounds(old[reference]) == bounds(new[reference]):
            continue
        path = (project / binding['path']).resolve()
        if not path.is_relative_to(project.resolve()) or not path.is_file():
            raise ValueError('Silkscreen synchronization requires an existing project library')
        source = path.read_text(encoding='utf-8')
        radius = bounds(new[reference])
        edits = []
        for line in parse(source).children('fp_line'):
            if 'SilkS' not in line.children('layer')[0].atoms()[1].value:
                continue
            for name in ('start', 'end'):
                point = line.children(name)[0]
                coords = [float(a.value) for a in point.atoms()[1:]]
                desired = [max(-limit, min(value, limit)) for value, limit in zip(coords, radius, strict=True)]
                if desired != coords:
                    edits.append((point.start, point.end, f'({name} {desired[0]:g} {desired[1]:g})'))
        for start, end, replacement in sorted(edits, reverse=True):
            source = source[:start] + replacement + source[end:]
        content = source.encode('utf-8')
        verify_silkscreen_only(path.read_bytes(), content)
        result[path] = content
    return result


def replace_saved_file(path: Path, expected: bytes, content: bytes) -> None:
    """Replace one owned source after readback, with same-volume atomic staging."""
    fd, temporary = tempfile.mkstemp(prefix='.silkscreen-', suffix='.tmp', dir=path.parent)
    staged = Path(temporary)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.read_bytes() != expected:
            raise RuntimeError('Source changed before silkscreen publication')
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def order_board_label_fields(content: bytes) -> bytes:
    """Keep owned display properties in KiCad's native property order."""
    source = content.decode('utf-8')
    edits = []
    for footprint in parse(source).children('footprint'):
        properties = footprint.children('property')
        labels = [p for p in properties if len(p.atoms()) >= 3 and p.atoms()[1].value == 'Board label'
                  and p.children('layer') and p.children('layer')[0].atoms()[1].value in ('F.SilkS', 'B.SilkS')]
        if not labels:
            continue
        if len(labels) != 1:
            raise ValueError('A footprint has duplicate owned display properties')
        field = labels[0]
        others = [p for p in properties if p is not field and len(p.atoms()) >= 3
                  and p.atoms()[1].value != 'ki_fp_filters']
        if not others:
            raise ValueError('An owned display property needs the native reference/value properties')
        position = others[-1].end
        if position <= field.start and not source[position:field.start].strip():
            continue
        edits.extend([(field.start, field.end, ''),
                      (position, position, '\n\t\t' + source[field.start:field.end])])
    for start, end, replacement in sorted(edits, reverse=True):
        source = source[:start] + replacement + source[end:]
    return source.encode('utf-8')


def attach_reference_labels(board, policy: dict) -> tuple[dict[str, str], set[str]]:
    """Replace stale free text by footprint-owned display fields, without renaming parts."""
    import pcbnew

    patterns = policy.get('attach_references', [])
    if not patterns:
        return {}, set()
    parts = {part.GetReference(): part for part in board.GetFootprints()}
    labels, removed = {}, set()
    for text in list(board.GetDrawings()):
        if not isinstance(text, pcbnew.PCB_TEXT) or text.GetLayer() not in (pcbnew.F_SilkS, pcbnew.B_SilkS):
            continue
        match = re.fullmatch(r'(TP|F)(0*[1-9][0-9]*)', text.GetText())
        if not match:
            continue
        reference = match[1] + str(int(match[2]))
        if not any(fnmatch.fnmatchcase(reference, pattern) for pattern in patterns):
            continue
        part = parts.get(reference)
        if part is None:
            if policy.get('remove_orphan_references'):
                removed.add(text.m_Uuid.AsString())
            continue
        if part.GetField('Board label') is not None:
            raise ValueError('Board-label field already exists beside an unbound duplicate')
        part.SetField('Board label', text.GetText())
        field = part.GetField('Board label')
        field.SetLayer(text.GetLayer())
        field.SetVisible(True)
        field.SetTextSize(text.GetTextSize())
        # PCB_FIELD reloads an omitted zero stroke as KiCad's 0.15 mm default.
        # Write it explicitly so subsequent native saves preserve the field.
        field.SetTextThickness(text.GetTextThickness() or 150000)
        field.SetTextAngle(pcbnew.EDA_ANGLE(0, pcbnew.DEGREES_T))
        field.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
        field.SetVertJustify(pcbnew.GR_TEXT_V_ALIGN_CENTER)
        labels[field.m_Uuid.AsString()] = (part.m_Uuid.AsString(), text.m_Uuid.AsString())
        # Keep native source objects alive. Delete free text only in the
        # source-preserving campaign, avoiding SWIG ownership invalidation.
    points = [p for shape in board.GetDrawings() if isinstance(shape, pcbnew.PCB_SHAPE)
              and shape.GetLayer() == pcbnew.Edge_Cuts for p in (shape.GetStart(), shape.GetEnd())]
    bounds = min(p.x for p in points), min(p.y for p in points), max(p.x for p in points), max(p.y for p in points)
    def rectangle(box):
        return box.GetX(), box.GetY(), box.GetRight(), box.GetBottom()
    def overlaps(a, b, gap=200000):
        return not (a[2] + gap <= b[0] or b[2] + gap <= a[0]
                    or a[3] + gap <= b[1] or b[3] + gap <= a[1])
    masks = [rectangle(pad.GetBoundingBox()) for pad in board.GetPads()
             if pad.IsOnLayer(pcbnew.F_Mask)]
    superseded = removed | {old_uid for _, old_uid in labels.values()}
    obstacles = [rectangle(text.GetBoundingBox()) for text in board.GetDrawings()
                 if isinstance(text, pcbnew.PCB_TEXT) and text.GetLayer() == pcbnew.F_SilkS
                 and text.m_Uuid.AsString() not in superseded]
    field_by_id = {field.m_Uuid.AsString(): (part, field) for part in board.GetFootprints()
                   for field in part.GetFields() if field.m_Uuid.AsString() in labels}
    fuse_parts = [parts[ref] for ref in ('F5', 'F6', 'F7', 'F8') if ref in parts]
    fuse_y = (min(p.GetBoundingBox(False, False).GetY() for p in fuse_parts) - 1200000
              if fuse_parts else 0)
    for uid in sorted(labels, key=lambda key: field_by_id[key][0].GetReference()):
        part, field = field_by_id[uid]
        center = part.GetPosition()
        if part.GetReference().startswith('F'):
            field.SetPosition(pcbnew.VECTOR2I(center.x, fuse_y))
            obstacles.append(rectangle(field.GetBoundingBox()))
            continue
        candidates = [(dx * radius, dy * radius) for radius in (1350000, 1600000, 1850000, 2100000, 2600000, 3100000, 3600000, 4100000)
                      for dx, dy in ((0, -1), (0, 1), (1, 0), (-1, 0), (1, -1), (-1, -1), (1, 1), (-1, 1))]
        for dx, dy in candidates:
            field.SetPosition(pcbnew.VECTOR2I(center.x + dx, center.y + dy))
            box = rectangle(field.GetBoundingBox())
            if (box[0] < bounds[0] + 250000 or box[1] < bounds[1] + 250000
                    or box[2] > bounds[2] - 250000 or box[3] > bounds[3] - 250000):
                continue
            if not any(overlaps(box, obstacle) for obstacle in (*masks, *obstacles)):
                obstacles.append(box)
                break
        else:
            raise ValueError('No nearby mask-clear label position for ' + part.GetReference())
    return labels, removed


def plan_silkscreen(project: Path, policy: dict) -> tuple[bytes, dict[str, int]]:
    """Use native board coordinates, then transfer only selected field edits."""
    import pcbnew

    if policy.get('schema') != 1:
        raise ValueError('Unsupported silkscreen policy')
    original = (project / 'v4.1.kicad_pcb').read_bytes()
    board = pcbnew.LoadBoard(str(project / 'v4.1.kicad_pcb'))
    silks = {pcbnew.F_SilkS, pcbnew.B_SilkS}
    graphics = [*board.GetDrawings(), *(x for f in board.GetFootprints() for x in f.GraphicalItems())]
    indexed = {item.m_Uuid.AsString(): item for item in graphics}
    edited, counts = {}, {'moved_texts': 0, 'clipped_lines': 0}
    positions = list(policy.get('text_positions', []))
    for group in policy.get('text_groups', []):
        positions.extend({'uuid': uid, 'x_mm': group['x_mm']} for uid in group['items'])
    for move in positions:
        item = indexed.get(move['uuid'])
        if not isinstance(item, pcbnew.PCB_TEXT) or item.GetLayer() not in silks:
            raise ValueError('Configured label is missing or is not silkscreen text')
        old = item.GetPosition()
        if 'right_of' in move:
            parts = [board.FindFootprintByReference(ref) for ref in move['right_of']]
            if not parts or any(part is None for part in parts):
                raise ValueError('Caption component bank is missing')
            right = max(part.GetBoundingBox(False, False).GetRight() for part in parts)
            desired_left = right + round(move.get('gap_mm', 1.0) * 1e6)
            offset = old.x - item.GetBoundingBox().GetX()
            move = {**move, 'x_mm': math.ceil((desired_left + offset) / 250000) / 4}
        if 'below_bank' in move:
            parts = [board.FindFootprintByReference(ref) for ref in move['below_bank']]
            boxes = [part.GetBoundingBox(False, False) for part in parts]
            item.SetTextAngle(pcbnew.EDA_ANGLE(0, pcbnew.DEGREES_T))
            item.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
            item.SetVertJustify(pcbnew.GR_TEXT_V_ALIGN_CENTER)
            center_x = (min(box.GetX() for box in boxes) + max(box.GetRight() for box in boxes)) / 2
            height = item.GetBoundingBox().GetHeight()
            center_y = max(box.GetBottom() for box in boxes) + move.get('gap_mm', 1.0) * 1e6 + height / 2
            move = {**move, 'x_mm': round(center_x / 250000) / 4,
                    'y_mm': math.ceil(center_y / 250000) / 4}
            edited[item.m_Uuid.AsString()] = ('gr_text', ('at', 'effects'))
        desired = pcbnew.VECTOR2I(round(move.get('x_mm', old.x / 1e6) * 1e6),
                                 round(move.get('y_mm', old.y / 1e6) * 1e6))
        if old != desired:
            item.SetPosition(desired)
            edited.setdefault(item.m_Uuid.AsString(), ('gr_text', ('at',)))
            counts['moved_texts'] += 1
    owned, removed = attach_reference_labels(board, policy)
    counts.update(attached_labels=len(owned), removed_orphan_labels=len(removed))
    outline = [x for x in board.GetDrawings() if isinstance(x, pcbnew.PCB_SHAPE)
               and x.GetLayer() == pcbnew.Edge_Cuts]
    if not outline or any(x.GetShape() not in {pcbnew.SHAPE_T_RECT, pcbnew.SHAPE_T_SEGMENT} for x in outline):
        raise ValueError('Automatic silk-edge cleanup currently requires a rectilinear outline')
    points = [p for shape in outline for p in (shape.GetStart(), shape.GetEnd())]
    xmin, xmax = min(p.x for p in points), max(p.x for p in points)
    ymin, ymax = min(p.y for p in points), max(p.y for p in points)
    margin = policy.get('edge_margin_mm', .25)
    if not isinstance(margin, (int, float)) or not .15 <= margin <= 2:
        raise ValueError('Silkscreen edge margin must be between 0.15 and 2 mm')
    for item in graphics:
        if (not isinstance(item, pcbnew.PCB_SHAPE) or item.GetLayer() not in silks
                or item.GetShape() != pcbnew.SHAPE_T_SEGMENT):
            continue
        gap = round(margin * 1e6) + (item.GetWidth() + 1) // 2
        def inside(point):
            return pcbnew.VECTOR2I(max(xmin + gap, min(point.x, xmax - gap)),
                                  max(ymin + gap, min(point.y, ymax - gap)))
        start, end = item.GetStart(), item.GetEnd()
        parent = item.GetParentFootprint()
        balanced = {binding['reference'] for binding in policy.get('library_silk_sync', [])}
        if parent is not None and parent.GetReference() in balanced:
            center = parent.GetPosition()
            rx = min(center.x - xmin - gap, xmax - gap - center.x)
            ry = min(center.y - ymin - gap, ymax - gap - center.y)
            if rx <= 0 or ry <= 0:
                raise ValueError('Footprint origin is too close to the board edge')
            def inside(point):
                return pcbnew.VECTOR2I(max(center.x - rx, min(point.x, center.x + rx)),
                                      max(center.y - ry, min(point.y, center.y + ry)))
        a, b = inside(start), inside(end)
        if a != start or b != end:
            if a == b:
                raise ValueError('Edge cleanup would collapse a silk line')
            item.SetStart(a)
            item.SetEnd(b)
            kind = 'gr_line' if item.GetParentFootprint() is None else 'fp_line'
            edited[item.m_Uuid.AsString()] = (kind, ('start', 'end'))
            counts['clipped_lines'] += 1
    if not edited and not owned and not removed:
        candidate = restore_stock_silk(original, policy.get('stock_silkscreen', []))
        candidate = order_board_label_fields(candidate)
        verify_silkscreen_only(original, candidate)
        return candidate, counts
    with tempfile.TemporaryDirectory(prefix='autopcb-silkscreen-') as directory:
        serialized = Path(directory) / 'serialized.kicad_pcb'
        if not pcbnew.SaveBoard(str(serialized), board):
            raise RuntimeError('Native silkscreen serialization failed')
        text = serialized.read_text(encoding='utf-8')
        forms = {form.children('uuid')[0].atoms()[1].value: form
                 for form in parse(text).descendants() if form.children('uuid')}
        changes = []
        for uid in removed:
            changes.append({'op': 'remove', 'target': {'kind': 'gr_text', 'uuid': uid}})
        for uid, (parent, old_uid) in owned.items():
            field = forms[uid]
            changes.extend([{'op': 'remove', 'target': {'kind': 'gr_text', 'uuid': old_uid}},
                            {'op': 'insert', 'target': {'kind': 'footprint', 'uuid': parent},
                             'expression': text[field.start:field.end]}])
        for uid, (kind, fields) in edited.items():
            for name in fields:
                field = forms[uid].children(name)[0]
                changes.append({'op': 'replace', 'target': {'kind': kind, 'uuid': uid},
                                'path': [name], 'expression': text[field.start:field.end]})
        candidate = CampaignPlanner(project).plan('pcb', changes).after
        candidate = restore_stock_silk(candidate, policy.get('stock_silkscreen', []))
        candidate = order_board_label_fields(candidate)
    verify_silkscreen_only(original, candidate)
    return candidate, counts


def cleanup(project: Path, *, apply: bool = False, top_alignment: bool = False) -> dict:
    import pcbnew
    from pcb.net_routing.controller import encode_checkpoint
    from pcb.net_routing.isolation import run_native
    from pcb.net_routing.native import Snapshot
    from pcb.net_routing.runner import _exclusive, _save
    from pcb.width_repair import introduced_findings

    assets = asset_root(project)
    policy_path = assets / 'config' / 'silkscreen.json'
    config = json.loads(policy_path.read_bytes())
    cli = Path(sys.executable).with_name('kicad-cli.exe')
    with _exclusive(assets / 'results' / 'autoroute' / 'active.lock'):
        with Snapshot(project / 'v4.1.kicad_pcb') as snapshot:
            dependencies = dict(snapshot.fingerprints)
            dependencies[policy_path] = file_sha256(policy_path)
            for binding in (() if top_alignment else config.get('stock_silkscreen', [])):
                path = Path(binding['path']).resolve()
                dependencies[path] = file_sha256(path)
            guard = BoardSaveGuard(snapshot.source, tuple(dependencies.items()))
            guard.verify_editor_unlocked()
            if top_alignment:
                candidate, counts = run_native(plan_top_text, snapshot.directory, config['top_alignment'])
                library_updates = {}
            else:
                candidate, counts = run_native(plan_silkscreen, snapshot.directory, config)
                library_updates = sync_silk_libraries(snapshot.directory, snapshot.original, candidate, config)
            saved_libraries = {project / path.relative_to(snapshot.directory): (path.read_bytes(), content)
                               for path, content in library_updates.items()}
            if candidate == snapshot.original:
                return {'status': 'unchanged', **counts}
            print('Checking silkscreen repairs against the unchanged saved copper', flush=True)
            before = _drc(cli, snapshot.board_path, snapshot.directory / 'before.json', schematic_parity=True)
            snapshot.board_path.write_bytes(candidate)
            for path, content in library_updates.items():
                path.write_bytes(content)
            after = _drc(cli, snapshot.board_path, snapshot.directory / 'after.json', schematic_parity=True)
            board = pcbnew.LoadBoard(str(snapshot.board_path))
            # Copper and filled-zone definitions are identical, so existing neck
            # markers may be matched independent of native adjacent-item choice.
            nets = frozenset(str(net) for net in board.GetNetsByName() if str(net))
            conflicts = introduced_findings(before, after, stable_neck_nets=nets)
            if conflicts:
                raise RuntimeError('Silkscreen candidate introduced native findings: '
                                   + '; '.join(v['description'] + ' ' + str([
                                       (i.get('description'), i.get('pos')) for i in v.get('items', [])])
                                       for v in conflicts[:4]))
            def silk_count(report):
                return sum(v['type'].startswith('silk_') for v in report['violations'])
            result = {'status': 'checked', **counts, 'silk_before': silk_count(before),
                      'silk_after': silk_count(after), 'opens': len(after['unconnected_items'])}
            if result['silk_after'] > result['silk_before']:
                raise RuntimeError('Silkscreen repair worsened native silk findings')
            verify_silkscreen_only(snapshot.original, candidate)
            if top_alignment:
                verify_top_text_alignment(snapshot.original, candidate)
            snapshot.check()
            tools = snapshot.tool_hashes
            if apply:
                inputs = {p.relative_to(project).as_posix(): value for p, value in dependencies.items()
                          if p.is_relative_to(project)}
                journal = {'kind': 'pre_silkscreen_save', 'created_utc': datetime.now(timezone.utc).isoformat(),
                           'manufacturing_approved': False,
                           'library_before': {p.relative_to(project).as_posix(): prior.decode('utf-8')
                                              for p, (prior, _) in saved_libraries.items()},
                           'board_before': encode_checkpoint(snapshot.original, inputs, tools, dependencies[policy_path])}
                _save(assets / 'results' / 'autoroute' / 'preimages' / 'working-save.json', journal)
        if apply:
            if Snapshot.tools() != tools:
                raise RuntimeError('Tools changed before silkscreen publication')
            def write(path, content):
                Path(path).write_bytes(content)
                return True
            guard.verify_unchanged()
            written = []
            try:
                for path, (prior, content) in saved_libraries.items():
                    replace_saved_file(path, prior, content)
                    written.append(path)
                updated = tuple((path, file_sha256(path) if path in saved_libraries else digest)
                                for path, digest in guard.fingerprints)
                BoardSaveGuard(guard.source, updated).save(candidate, guard.source, write, pcbnew.LoadBoard)
            except BaseException:
                for path in reversed(written):
                    prior, content = saved_libraries[path]
                    replace_saved_file(path, content, prior)
                raise
            result['status'] = 'saved'
        return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--top-alignment', action='store_true', help='Align only front connector label text')
    args = parser.parse_args(argv)
    with quiet_duplicate_image_handlers():
        result = cleanup(args.project.resolve(), apply=args.apply, top_alignment=args.top_alignment)
    print('Silkscreen:', result['status'])
    for name, value in result.items():
        if name != 'status':
            print(f'  {name.replace("_", " ")}: {value}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
