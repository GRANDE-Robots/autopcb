#!/usr/bin/env python3
"""orient_passive_labels.py: point labels outward on the right of horizontal R/C parts.

Two distinct net labels on opposite sides of a horizontal resistor or capacitor
must both remain; deleting either would hide a real filter/drop/net boundary.
When both point left, their text overlaps the component.  This script derives
physical wire components and changes only a global label attached to the right
side of a horizontal two-pin R/C from left-facing to right-facing.  No anchor,
wire, pin, net, component, or electrical property changes.  The write is
atomic and idempotent.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from sch.deduplicate_labels import PhysicalWires
from sch.schematic_io import AtomicTextWriter
from sch.standardize_block_symbols import (
    balanced_end,
    instances,
    library_units,
    point_key,
    source_lib_definitions,
    top_level,
)

EPSILON = 1e-6
TEMP_SUFFIX = ".passive-labels.tmp"


class PassiveLabelOrientation:
    """Plans which labels on horizontal R/C networks should face right."""

    @staticmethod
    def horizontal_passive_right_pins(root: str) -> set[tuple[float, float]]:
        """Return right-side endpoints of current horizontal two-terminal R/Cs."""
        _start, _end, definitions = source_lib_definitions(root)
        result = set()
        for instance in instances(root):
            if not instance.reference.startswith(("R", "C")):
                continue
            definition = definitions.get(instance.lib_id)
            if definition is None:
                continue
            unit = library_units(definition[2]).get(instance.unit)
            if unit is None or len(unit[1]) != 2:
                continue
            points = [instance.pin_point(pin) for pin in unit[1]]
            if abs(points[0][1] - points[1][1]) <= EPSILON:
                result.add(max(points, key=lambda point: point[0]))
        return result

    @staticmethod
    def label_at_angle(block: str) -> tuple[tuple[float, float], int]:
        """Return a label's anchor point and its angle in degrees, 0 to 359.

        Raises ValueError when the label has no ``(at x y angle)``.
        """
        match = re.search(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\)", block)
        if not match:
            raise ValueError("label has no complete at record")
        return point_key(float(match.group(1)), float(match.group(2))), int(
            float(match.group(3))
        ) % 360

    @staticmethod
    def face_right(block: str) -> str:
        """Keep position/text size/UUID and alter just presentation direction.

        Raises ValueError when the label's angle cannot be rewritten or its
        effects block is malformed.
        """
        updated, count = re.subn(
            r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)\s+-?[\d.]+\)",
            r"(at \1 \2 0)",
            block,
            count=1,
        )
        if count != 1:
            raise ValueError("cannot rewrite label angle")
        if "(justify right)" in updated:
            return updated.replace("(justify right)", "(justify left)", 1)
        effects_end = updated.find("\n\t\t)\n", updated.find("(effects"))
        if effects_end < 0:
            raise ValueError("label effects are malformed")
        return updated[:effects_end] + "\n\t\t\t(justify left)" + updated[effects_end:]

    @classmethod
    def plan(cls, source: str) -> list[tuple[int, int, str, str]]:
        """Find the global labels to turn right-facing.

        Inputs:
            source: Complete schematic text.
        Outputs:
            For each label on the conductor of a horizontal passive's right pin
            that now faces left, its start, end, rewritten text and net name.
            Raises ValueError when the source is not a KiCad schematic or a
            label lacks a position or name.
        """
        root_start = source.find("(kicad_sch")
        if root_start < 0:
            raise ValueError("not a KiCad schematic")
        root_end = balanced_end(source, root_start)
        root = source[root_start:root_end]
        labels = []
        for start, end, block in top_level(root, "global_label"):
            point, angle = cls.label_at_angle(block)
            labels.append((start, end, block, point, angle))
        dsu = PhysicalWires.physical_components(
            root, {item[3] for item in labels} | cls.horizontal_passive_right_pins(root)
        )
        right_components = {
            dsu.find(point) for point in cls.horizontal_passive_right_pins(root)
        }
        updates = []
        for start, end, block, point, angle in labels:
            if angle != 180 or dsu.find(point) not in right_components:
                continue
            name = re.match(r'\(global_label\s+"((?:[^"\\]|\\.)*)"', block)
            if not name:
                raise ValueError("global label has no name")
            updates.append((start, end, cls.face_right(block), name.group(1)))
        return updates


class PassiveLabelCommand:
    """Command-line entry: check or apply right-facing labels on passives."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Turn the right-hand labels of horizontal R/Cs to face right.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 after printing the number of labels and their net
            names. ``--apply`` writes the result atomically to ``--output`` or the
            schematic when any label changes.
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
        updates = PassiveLabelOrientation.plan(source)
        print(f"right_facing_labels={len(updates)}")
        for _start, _end, _replacement, name in updates:
            print(name)
        if args.apply and updates:
            result = source
            for start, end, replacement, _name in reversed(updates):
                result = result[:start] + replacement + result[end:]
            AtomicTextWriter(TEMP_SUFFIX).write(args.output or args.schematic, result)
            print("applied atomically")
        elif args.apply:
            print("already oriented")
        return 0


main = PassiveLabelCommand()


if __name__ == "__main__":
    raise SystemExit(main())
