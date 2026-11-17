#!/usr/bin/env python3
"""Repair no-connect markers displaced one grid cell from their pin.

The one-sided symbol formatter deliberately rebuilds pin geometry.  If a
symbol is subsequently nudged in KiCad, a retained no-connect cross can be
left one native grid cell away from its intended pin.  This utility derives
every live pin endpoint from the embedded schematic libraries and moves only
such unambiguous one-grid stale markers onto their actual pins.  It never
deletes no-connect markers or changes a net, pin, symbol, page setting, PCB,
or library.

Run ``--check`` before ``--apply``.  Writes are atomic.
"""

from __future__ import annotations

import argparse
import math
import os
import re
from pathlib import Path

from sch.standardize_block_symbols import (
    NATIVE_GRID,
    balanced_end,
    fmt,
    instances,
    library_units,
    local_screen_point,
    point_key,
    source_lib_definitions,
    top_level,
    top_level_points,
)

EPSILON = 1e-6


def pin_endpoints(root: str) -> dict[tuple[float, float], list[tuple[str, str]]]:
    """Map each current embedded-symbol pin endpoint to reference/pin IDs."""
    _start, _end, definitions = source_lib_definitions(root)
    result: dict[tuple[float, float], list[tuple[str, str]]] = {}
    for instance in instances(root):
        definition = definitions.get(instance.lib_id)
        if definition is None:
            continue
        unit = library_units(definition[2]).get(instance.unit)
        if unit is None:
            continue
        for pin in unit[1]:
            point = point_key(
                *local_screen_point(
                    instance.x,
                    instance.y,
                    instance.rotation,
                    instance.mirror,
                    pin.x,
                    pin.y,
                )
            )
            result.setdefault(point, []).append((instance.reference, pin.number))
    return result


def stale_no_connects(
    root: str,
) -> tuple[
    list[
        tuple[
            int,
            int,
            str,
            tuple[float, float],
            tuple[float, float],
            list[tuple[str, str]],
        ]
    ],
    int,
]:
    """Return only markers exactly one native grid step from one live pin."""
    endpoints = pin_endpoints(root)
    stale = []
    valid = 0
    for start, end, block in top_level(root, "no_connect"):
        points = top_level_points(block, "no_connect")
        if len(points) != 1:
            raise ValueError("no-connect marker lacks exactly one coordinate")
        point = point_key(*points[0])
        if point in endpoints:
            valid += 1
            continue
        candidates = []
        for endpoint, pins in endpoints.items():
            distance = math.dist(point, endpoint)
            if abs(distance - NATIVE_GRID) <= EPSILON:
                candidates.append((endpoint, pins))
        if len(candidates) == 1:
            endpoint, pins = candidates[0]
            stale.append((start, end, block, point, endpoint, pins))
            continue
        if candidates:
            raise ValueError(f"ambiguous stale no-connect at {point}: {candidates}")
        raise ValueError(
            f"no-connect at {point} is not on a pin or exactly one grid cell from one; "
            "leave it for manual electrical review"
        )
    return stale, valid


def moved_block(block: str, old: tuple[float, float], new: tuple[float, float]) -> str:
    """Replace only the marker's top-level coordinate; preserve its UUID."""
    pattern = re.compile(
        rf"\(at\s+{re.escape(fmt(old[0]))}\s+{re.escape(fmt(old[1]))}(?:\s+[-\d.]+)?\)"
    )
    replacement = f"(at {fmt(new[0])} {fmt(new[1])})"
    updated, count = pattern.subn(replacement, block, count=1)
    if count != 1:
        raise ValueError(f"cannot rewrite no-connect at {old}")
    return updated


def write_atomic(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".no-connect-repair.tmp")
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
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.check == args.apply:
        parser.error("choose exactly one of --check or --apply")

    source = args.schematic.read_text(encoding="utf-8")
    root_end = balanced_end(source, source.find("(kicad_sch"))
    root = source[:root_end]
    stale, valid = stale_no_connects(root)
    print(f"valid_no_connects={valid}")
    print(f"stale_no_connects={len(stale)}")
    for _start, _end, _block, old, new, pins in stale:
        pin_text = ", ".join(f"{reference}.{number}" for reference, number in pins)
        print(
            f"{fmt(old[0])},{fmt(old[1])} -> {fmt(new[0])},{fmt(new[1])} [{pin_text}]"
        )
    if args.check:
        return 0

    replacements = [
        (start, end, moved_block(block, old, new))
        for start, end, block, old, new, _pins in stale
    ]
    result = source
    for start, end, replacement in reversed(replacements):
        result = result[:start] + replacement + result[end:]
    write_atomic(args.schematic, result)
    print(f"applied_moves={len(replacements)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
