#!/usr/bin/env python3
"""Remove dead graphical wire stubs and their now-isolated labels.

After redundant same-net labels are consolidated, an old wire can remain with
one completely bare end.  It no longer connects a pin, label, junction, or
no-connect marker and causes KiCad's ``Unconnected wire endpoint`` warning.
This script removes only such two-point dead stubs.  A label is removed only
when it becomes physically isolated after that removal.  No live pin, net,
component, coordinate, page setting, or PCB data is changed.
"""

from __future__ import annotations

import argparse
import os
from collections import Counter
from pathlib import Path

from sch.standardize_block_symbols import (
    balanced_end,
    instances,
    library_units,
    local_screen_point,
    point_key,
    source_lib_definitions,
    top_level,
    top_level_points,
)


def pin_points(root: str) -> set[tuple[float, float]]:
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


def anchored_points(root: str, token: str) -> set[tuple[float, float]]:
    result = set()
    for _start, _end, block in top_level(root, token):
        points = top_level_points(block, token)
        if points:
            result.add(point_key(*points[0]))
    return result


def plan(source: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]], int]:
    root_start = source.find("(kicad_sch")
    if root_start < 0:
        raise ValueError("not a KiCad schematic")
    root_end = balanced_end(source, root_start)
    root = source[root_start:root_end]
    labels: dict[tuple[float, float], list[tuple[int, int]]] = {}
    for token in ("label", "global_label", "hierarchical_label"):
        for start, end, block in top_level(root, token):
            points = top_level_points(block, token)
            if points:
                labels.setdefault(point_key(*points[0]), []).append((start, end))
    protected = (
        pin_points(root)
        | set(labels)
        | anchored_points(root, "junction")
        | anchored_points(root, "no_connect")
    )
    wires = []
    degree: Counter[tuple[float, float]] = Counter()
    for start, end, block in top_level(root, "wire"):
        points = [point_key(*point) for point in top_level_points(block, "wire")]
        if len(points) != 2:
            continue
        first, second = points
        wires.append((start, end, first, second))
        degree[first] += 1
        degree[second] += 1

    remove_wires: list[tuple[int, int]] = []
    for start, end, first, second in wires:
        first_bare = first not in protected and degree[first] == 1
        second_bare = second not in protected and degree[second] == 1
        # One bare end means the segment cannot convey a connection to it.
        # Two bare ends would be a fully isolated wire and are removed too.
        if first_bare or second_bare:
            remove_wires.append((start, end))
    removed = set(remove_wires)

    retained_degree: Counter[tuple[float, float]] = Counter()
    for start, end, first, second in wires:
        if (start, end) in removed:
            continue
        retained_degree[first] += 1
        retained_degree[second] += 1
    immutable = (
        pin_points(root)
        | anchored_points(root, "junction")
        | anchored_points(root, "no_connect")
    )
    remove_labels: list[tuple[int, int]] = []
    for point, spans in labels.items():
        if point in immutable or retained_degree[point] > 0:
            continue
        # A label is cleanup collateral only when it was attached to a wire
        # that this pass is actually removing.  A label can legitimately sit
        # on a symbol-side endpoint that the lightweight custom-pin parser
        # cannot classify; removing it merely because no *wire* remains there
        # would erase a live visible net name.
        if not any(
            point == first or point == second
            for _start, _end, first, second in wires
            if (_start, _end) in removed
        ):
            continue
        remove_labels.extend(spans)
    return remove_wires, remove_labels, len(wires)


def write_atomic(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".dead-stubs.tmp")
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
    remove_wires, remove_labels, total_wires = plan(source)
    print(f"two_point_wires_checked={total_wires}")
    print(f"dead_wire_stubs={len(remove_wires)}")
    print(f"isolated_labels={len(remove_labels)}")
    if args.apply and (remove_wires or remove_labels):
        replacements = [(start, end, "") for start, end in remove_wires + remove_labels]
        replacements.sort()
        for previous, current in zip(replacements, replacements[1:]):
            if current[0] < previous[1]:
                raise ValueError("dead-stub edits overlap")
        result = source
        for start, end, replacement in reversed(replacements):
            result = result[:start] + replacement + result[end:]
        write_atomic(args.output or args.schematic, result)
        print("applied atomically")
    elif args.apply:
        print("no dead stubs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
