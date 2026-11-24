#!/usr/bin/env python3
"""repair_no_connects.py: repair no-connect markers displaced one grid cell from a pin.

The one-sided symbol formatter deliberately rebuilds pin geometry.  If a
symbol is subsequently nudged in KiCad, a retained no-connect cross can be
left one native grid cell away from its intended pin.  This utility derives
every live pin endpoint from the embedded schematic libraries and moves only
such unambiguous one-grid stale markers onto their actual pins.  It never
deletes no-connect markers or changes a net, pin, symbol, page setting, PCB,
or library.

Run ``--check`` before ``--apply``.  Writes are atomic.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import math
import re
from pathlib import Path

from sch.schematic_io import AtomicTextWriter
from sch.standardize_block_symbols import (
    NATIVE_GRID,
    SheetPoints,
    balanced_end,
    fmt,
    point_key,
    top_level,
    top_level_points,
)

EPSILON = 1e-6
TEMP_SUFFIX = ".no-connect-repair.tmp"


class PinEndpoints:
    """Maps pin endpoints to the pins that end there."""

    def __call__(self, root: str) -> dict[tuple[float, float], list[tuple[str, str]]]:
        """Map each current embedded-symbol pin endpoint to reference/pin IDs."""
        result: dict[tuple[float, float], list[tuple[str, str]]] = {}
        for instance, pin, point in SheetPoints.placed_pins(root):
            result.setdefault(point, []).append((instance.reference, pin.number))
        return result


pin_endpoints = PinEndpoints()


class NoConnectRepair:
    """Finds no-connect markers one grid cell from their pin and moves them."""

    @staticmethod
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
        """Return only markers exactly one native grid step from one live pin.

        Inputs:
            root: Text of the ``(kicad_sch ...)`` root.
        Outputs:
            For each stale marker its start, end, text, current point, pin
            endpoint and the pins there; and the number of markers already on a
            pin. Raises ValueError for a marker without exactly one coordinate,
            one near several pins, or neither on nor one grid cell from a pin.
        """
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

    @staticmethod
    def moved_block(
        block: str, old: tuple[float, float], new: tuple[float, float]
    ) -> str:
        """Replace only the marker's top-level coordinate; preserve its UUID.

        Raises ValueError when the marker's coordinate cannot be rewritten.
        """
        pattern = re.compile(
            rf"\(at\s+{re.escape(fmt(old[0]))}\s+{re.escape(fmt(old[1]))}(?:\s+[-\d.]+)?\)"
        )
        replacement = f"(at {fmt(new[0])} {fmt(new[1])})"
        updated, count = pattern.subn(replacement, block, count=1)
        if count != 1:
            raise ValueError(f"cannot rewrite no-connect at {old}")
        return updated


class NoConnectRepairCommand:
    """Command-line entry: check or apply the repair of stale no-connect markers."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Move stale no-connect markers of one schematic onto their pins.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 after printing the marker counts and each move.
            ``--apply`` replaces the schematic atomically and prints the move
            count. Exactly one of ``--check`` and ``--apply`` is required.
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("schematic", type=Path)
        parser.add_argument("--check", action="store_true")
        parser.add_argument("--apply", action="store_true")
        args = parser.parse_args(argv)
        if args.check == args.apply:
            parser.error("choose exactly one of --check or --apply")

        source = args.schematic.read_text(encoding="utf-8")
        root_end = balanced_end(source, source.find("(kicad_sch"))
        root = source[:root_end]
        stale, valid = NoConnectRepair.stale_no_connects(root)
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
            (start, end, NoConnectRepair.moved_block(block, old, new))
            for start, end, block, old, new, _pins in stale
        ]
        result = source
        for start, end, replacement in reversed(replacements):
            result = result[:start] + replacement + result[end:]
        AtomicTextWriter(TEMP_SUFFIX).write(args.schematic, result)
        print(f"applied_moves={len(replacements)}")
        return 0


main = NoConnectRepairCommand()


if __name__ == "__main__":
    raise SystemExit(main())
