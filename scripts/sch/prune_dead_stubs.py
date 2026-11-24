#!/usr/bin/env python3
"""prune_dead_stubs.py: remove dead graphical wire stubs and their now-isolated labels.

After redundant same-net labels are consolidated, an old wire can remain with
one completely bare end.  It no longer connects a pin, label, junction, or
no-connect marker and causes KiCad's ``Unconnected wire endpoint`` warning.
This script removes only such two-point dead stubs.  A label is removed only
when it becomes physically isolated after that removal.  No live pin, net,
component, coordinate, page setting, or PCB data is changed.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from sch.schematic_io import AtomicTextWriter
from sch.standardize_block_symbols import (
    SheetPoints,
    balanced_end,
    point_key,
    top_level,
    top_level_points,
)

TEMP_SUFFIX = ".dead-stubs.tmp"


class DeadStubs:
    """Finds wire stubs that connect to nothing and the labels they leave behind."""

    @staticmethod
    def plan(source: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]], int]:
        """Choose the dead stubs and orphaned labels of a schematic.

        Inputs:
            source: Complete schematic text.
        Outputs:
            The (start, end) offsets of each wire to remove, those of each label
            that becomes isolated, and the number of two-point wires examined.
            Raises ValueError when the source is not a KiCad schematic.
        """
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
            SheetPoints.pin_points(root)
            | set(labels)
            | SheetPoints.anchored(root, "junction")
            | SheetPoints.anchored(root, "no_connect")
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
            SheetPoints.pin_points(root)
            | SheetPoints.anchored(root, "junction")
            | SheetPoints.anchored(root, "no_connect")
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
                for wire_start, wire_end, first, second in wires
                if (wire_start, wire_end) in removed
            ):
                continue
            remove_labels.extend(spans)
        return remove_wires, remove_labels, len(wires)


class DeadStubsCommand:
    """Command-line entry: check or apply the removal of dead wire stubs."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Remove dead wire stubs and their isolated labels from one schematic.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 after printing the wire, stub and label counts.
            ``--apply`` writes the result atomically to ``--output`` or the
            schematic when anything is removed. Raises ValueError when the
            removal spans overlap.
        """
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
        args = parser.parse_args(argv)
        if args.output and not args.apply:
            parser.error("--output requires --apply")
        source = args.schematic.read_text(encoding="utf-8")
        remove_wires, remove_labels, total_wires = DeadStubs.plan(source)
        print(f"two_point_wires_checked={total_wires}")
        print(f"dead_wire_stubs={len(remove_wires)}")
        print(f"isolated_labels={len(remove_labels)}")
        if args.apply and (remove_wires or remove_labels):
            replacements = [
                (start, end, "") for start, end in remove_wires + remove_labels
            ]
            replacements.sort()
            for previous, current in zip(replacements, replacements[1:]):
                if current[0] < previous[1]:
                    raise ValueError("dead-stub edits overlap")
            result = source
            for start, end, replacement in reversed(replacements):
                result = result[:start] + replacement + result[end:]
            AtomicTextWriter(TEMP_SUFFIX).write(args.output or args.schematic, result)
            print("applied atomically")
        elif args.apply:
            print("no dead stubs")
        return 0


main = DeadStubsCommand()


if __name__ == "__main__":
    raise SystemExit(main())
