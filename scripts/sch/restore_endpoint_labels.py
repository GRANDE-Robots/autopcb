#!/usr/bin/env python3
"""Repair bare wire ends without restoring visually duplicated net labels.

This is the safe complement to same-conductor label consolidation.  It finds
every wire end that KiCad still regards as bare, derives the one existing
global-net name on that physical conductor, and adds an auxiliary *hidden*
electrical tag at that endpoint.  The visible 30-mil label remains singular
and clean; the tiny tag exists only so KiCad keeps the established short-stub
topology ERC-clean.  Ambiguous or unnamed conductors stop the operation.  No
component, wire, pin, or PCB data changes; writes are atomic.
"""

from __future__ import annotations

import argparse
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

from sch.deduplicate_labels import DisjointSet, on_segment
from sch.standardize_block_symbols import (
    TEXT_SIZE,
    balanced_end,
    fmt,
    instances,
    label_block,
    library_units,
    local_screen_point,
    point_key,
    source_lib_definitions,
    top_level,
    top_level_points,
)

AUXILIARY_TEXT_SIZE = 0.01


def all_pin_points(root: str) -> set[tuple[float, float]]:
    _start, _end, definitions = source_lib_definitions(root)
    result = set()
    for instance in instances(root):
        definition = definitions.get(instance.lib_id)
        if definition is None:
            continue
        unit = library_units(definition[2]).get(instance.unit)
        if unit is None:
            continue
        for pin in unit[1]:
            result.add(
                point_key(
                    *local_screen_point(
                        instance.x,
                        instance.y,
                        instance.rotation,
                        instance.mirror,
                        pin.x,
                        pin.y,
                    )
                )
            )
    return result


def global_label_records(
    root: str,
) -> list[tuple[int, int, str, tuple[float, float], str]]:
    result = []
    for _start, _end, block in top_level(root, "global_label"):
        name = re.match(r'\(global_label\s+"((?:[^"\\]|\\.)*)"', block)
        points = top_level_points(block, "global_label")
        if not name or len(points) != 1:
            raise ValueError("malformed global label")
        result.append(
            (
                _start,
                _end,
                name.group(1).replace(r'\\"', '"'),
                point_key(*points[0]),
                block,
            )
        )
    return result


def anchored(root: str, token: str) -> set[tuple[float, float]]:
    result = set()
    for _start, _end, block in top_level(root, token):
        points = top_level_points(block, token)
        if points:
            result.add(point_key(*points[0]))
    return result


def wire_graph(
    root: str,
    attachments: set[tuple[float, float]],
) -> tuple[
    DisjointSet,
    list[tuple[tuple[float, float], tuple[float, float], int, int, bool]],
    Counter[tuple[float, float]],
]:
    segments = []
    degree: Counter[tuple[float, float]] = Counter()
    for wire_start, wire_end, block in top_level(root, "wire"):
        points = [point_key(*point) for point in top_level_points(block, "wire")]
        if len(points) < 2:
            raise ValueError("wire lacks endpoints")
        simple = len(points) == 2
        for first, second in zip(points, points[1:]):
            segments.append((first, second, wire_start, wire_end, simple))
            degree[first] += 1
            degree[second] += 1
    dsu = DisjointSet()
    for first, second, _wire_start, _wire_end, _simple in segments:
        dsu.union(first, second)
    for point in attachments:
        dsu.add(point)
        for first, second, _wire_start, _wire_end, _simple in segments:
            if on_segment(point, first, second):
                dsu.union(point, first)
                dsu.union(point, second)
    return dsu, segments, degree


def outward_angle(point: tuple[float, float], neighbour: tuple[float, float]) -> int:
    if abs(point[0] - neighbour[0]) > 1e-6:
        return 180 if point[0] < neighbour[0] else 0
    # A vertical stub has no natural text side; retain the project's standard
    # right-justified outward label rather than generating rotated text.
    return 180


def hidden_label_block(net: str, x: float, y: float, angle: int) -> str:
    """Emit an ERC-only global tag that is invisible at normal schematic zoom."""
    visible = f"(size {fmt(TEXT_SIZE)} {fmt(TEXT_SIZE)})"
    hidden = f"(size {fmt(AUXILIARY_TEXT_SIZE)} {fmt(AUXILIARY_TEXT_SIZE)})"
    original = label_block(net, x, y, angle=angle)
    result = original.replace(visible, hidden, 1)
    if result == original:
        raise ValueError("generated label lacked its visible font size")
    return result


def plan(
    source: str,
) -> tuple[
    list[tuple[tuple[float, float], str, int]],
    list[tuple[float, float]],
]:
    root_start = source.find("(kicad_sch")
    if root_start < 0:
        raise ValueError("not a KiCad schematic")
    root_end = balanced_end(source, root_start)
    root = source[root_start:root_end]
    pins = all_pin_points(root)
    global_labels = global_label_records(root)
    globals_by_point: dict[tuple[float, float], list[str]] = defaultdict(list)
    for _start, _end, name, point, _block in global_labels:
        globals_by_point[point].append(name)
    any_labels = set(globals_by_point)
    for token in ("label", "hierarchical_label"):
        any_labels |= anchored(root, token)
    junctions = anchored(root, "junction")
    no_connects = anchored(root, "no_connect")
    attachments = pins | any_labels | junctions | no_connects
    dsu, segments, degree = wire_graph(root, attachments)
    names_by_component: dict[tuple[float, float], set[str]] = defaultdict(set)
    for _start, _end, name, point, _block in global_labels:
        names_by_component[dsu.find(point)].add(name)

    additions = []
    unresolved = []
    for first, second, _wire_start, _wire_end, _simple in segments:
        for point, neighbour in ((first, second), (second, first)):
            if degree[point] != 1 or point in attachments:
                continue
            names = names_by_component.get(dsu.find(point), set())
            if len(names) != 1:
                unresolved.append(point)
                continue
            additions.append(
                (point, next(iter(names)), outward_angle(point, neighbour))
            )

    # Defensive deduplication makes reruns idempotent if a malformed source
    # repeats an identical bare end.
    dedup_additions = {}
    for point, name, angle in additions:
        dedup_additions[point] = (name, angle)
    additions = [
        (point, name, angle) for point, (name, angle) in sorted(dedup_additions.items())
    ]
    return additions, sorted(set(unresolved))


def write_atomic(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".endpoint-labels.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schematic", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        help="write an --apply candidate here instead of the source",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.output and not args.apply:
        parser.error("--output requires --apply")
    source = args.schematic.read_text(encoding="utf-8")
    additions, unresolved = plan(source)
    print(f"hidden_endpoint_tags_to_add={len(additions)}")
    print(f"unresolved_bare_endpoints={len(unresolved)}")
    for point, name, angle in additions:
        print(f"{fmt(point[0])},{fmt(point[1])}: {name} angle={angle}")
    for point in unresolved:
        print(f"unresolved {fmt(point[0])},{fmt(point[1])}")
    if unresolved:
        return 2
    if args.apply and additions:
        root_end = balanced_end(source, source.find("(kicad_sch"))
        root = source[:root_end]
        insert = root.rfind("(sheet_instances")
        if insert < 0:
            raise ValueError("missing sheet_instances")
        blocks = "".join(
            hidden_label_block(name, point[0], point[1], angle)
            for point, name, angle in additions
        )
        result = root[:insert] + blocks + root[insert:] + source[root_end:]
        write_atomic(args.output or args.schematic, result)
        print("applied atomically")
    elif args.apply:
        print("no labels needed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
