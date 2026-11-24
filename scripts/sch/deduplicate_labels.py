#!/usr/bin/env python3
"""deduplicate_labels.py: remove redundant same-net labels, keeping connectivity.

A label is removed only when another label of the same kind and net name is
on the *same physical wire/pin component*.  Thus the retained label still
names that entire conductor; labels on separate, global-net-connected islands
are never merged.  The formatter's one-grid pin-stub label wins when present,
so the compact one-sided blocks stay self-explanatory.  ERC-only endpoint tags
use a 0.01 mm font and are never deduplicated: removing one would recreate a
bare legacy wire end.  The write is atomic.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from pathlib import Path

from sch.schematic_io import AtomicTextWriter
from sch.standardize_block_symbols import (
    STUB_LENGTH,
    balanced_end,
    instances,
    is_target,
    library_units,
    point_key,
    source_lib_definitions,
    top_level,
    top_level_points,
)

EPSILON = 1e-6
LABEL_TOKENS = ("label", "global_label", "hierarchical_label")
AUXILIARY_TEXT_SIZE = 0.01
TEMP_SUFFIX = ".labels.tmp"


class DisjointSet:
    """Groups points into connected components by union and find."""

    def __init__(self) -> None:
        """Start with no points."""
        self.parent: dict[tuple[float, float], tuple[float, float]] = {}

    def add(self, item: tuple[float, float]) -> None:
        """Register ``item`` as its own component if it is new."""
        self.parent.setdefault(item, item)

    def find(self, item: tuple[float, float]) -> tuple[float, float]:
        """Return the representative of ``item``'s component, registering ``item``."""
        self.add(item)
        root = self.parent[item]
        if root != item:
            root = self.find(root)
            self.parent[item] = root
        return root

    def union(self, first: tuple[float, float], second: tuple[float, float]) -> None:
        """Merge the components of two points."""
        first_root, second_root = self.find(first), self.find(second)
        if first_root != second_root:
            self.parent[second_root] = first_root


class PhysicalWires:
    """Derives which points of a sheet share one physical conductor."""

    @staticmethod
    def on_segment(
        point: tuple[float, float],
        first: tuple[float, float],
        second: tuple[float, float],
    ) -> bool:
        """Exact-grid point lies on a horizontal/vertical/diagonal wire segment."""
        cross = (point[0] - first[0]) * (second[1] - first[1]) - (
            point[1] - first[1]
        ) * (second[0] - first[0])
        return (
            abs(cross) <= EPSILON
            and min(first[0], second[0]) - EPSILON
            <= point[0]
            <= max(first[0], second[0]) + EPSILON
            and min(first[1], second[1]) - EPSILON
            <= point[1]
            <= max(first[1], second[1]) + EPSILON
        )

    @classmethod
    def physical_components(
        cls, root: str, attachment_points: set[tuple[float, float]]
    ) -> DisjointSet:
        """Build only physical wire connectivity; global labels are not graph edges.

        Inputs:
            root: Text of the ``(kicad_sch ...)`` root.
            attachment_points: Points, in mm, that join a wire they lie on.
        Outputs:
            The components of wire points, junctions and attachment points.
        """
        wires: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for _start, _end, block in top_level(root, "wire"):
            points = [point_key(*point) for point in top_level_points(block, "wire")]
            for first, second in zip(points, points[1:]):
                wires.append((first, second))
        dsu = DisjointSet()
        for first, second in wires:
            dsu.union(first, second)

        # A wire crossing becomes an electrical junction only when the source
        # explicitly contains a junction marker; do not infer connections from
        # graphical crossings.
        junctions = set()
        for _start, _end, block in top_level(root, "junction"):
            points = top_level_points(block, "junction")
            if points:
                junctions.add(point_key(*points[0]))
        for point in junctions | attachment_points:
            dsu.add(point)
            for first, second in wires:
                if cls.on_segment(point, first, second):
                    dsu.union(point, first)
                    dsu.union(point, second)
        return dsu


class LabelDeduplicator:
    """Plans the removal of labels that repeat a name on one conductor."""

    @staticmethod
    def formatter_stub_points(root: str) -> set[tuple[float, float]]:
        """Return the preferred left-side label anchors of normalized block pins."""
        _start, _end, definitions = source_lib_definitions(root)
        result = set()
        for instance in instances(root):
            if not is_target(instance.reference, instance.lib_id):
                continue
            definition = definitions.get(instance.lib_id)
            if definition is None:
                continue
            unit = library_units(definition[2]).get(instance.unit)
            if unit is None:
                continue
            for pin in unit[1]:
                endpoint = instance.pin_point(pin)
                result.add(point_key(endpoint[0] - STUB_LENGTH, endpoint[1]))
        return result

    @staticmethod
    def label_name(block: str, token: str) -> str:
        """Return the unescaped quoted name of a label.

        Raises ValueError when the label has no quoted name.
        """
        match = re.match(rf'\({re.escape(token)}\s+"((?:[^"\\]|\\.)*)"', block)
        if not match:
            raise ValueError(f"{token} lacks a quoted name")
        return match.group(1).replace(r'\\"', '"').replace(r"\\\\", "\\")

    @classmethod
    def plan(cls, source: str) -> tuple[list[tuple[int, int]], Counter[str]]:
        """Choose the redundant labels of a schematic.

        Inputs:
            source: Complete schematic text.
        Outputs:
            The (start, end) offsets of each label to remove and the number
            removed per net name. Raises ValueError when the source is not a
            KiCad schematic or a label lacks one anchor or a quoted name.
        """
        root_start = source.find("(kicad_sch")
        if root_start < 0:
            raise ValueError("not a KiCad schematic")
        root_end = balanced_end(source, root_start)
        root = source[root_start:root_end]
        labels: list[tuple[int, int, str, str, tuple[float, float]]] = []
        auxiliary_keys: set[tuple[str, str, tuple[float, float]]] = set()
        for token in LABEL_TOKENS:
            for start, end, block in top_level(root, token):
                points = top_level_points(block, token)
                if len(points) != 1:
                    raise ValueError(f"{token} lacks one anchor")
                name = cls.label_name(block, token)
                point = point_key(*points[0])
                labels.append((start, end, token, name, point))
                if token == "global_label" and re.search(
                    rf"\(size\s+{AUXILIARY_TEXT_SIZE:g}\s+{AUXILIARY_TEXT_SIZE:g}\)",
                    block,
                ):
                    auxiliary_keys.add((token, name, point))
        dsu = PhysicalWires.physical_components(root, {item[4] for item in labels})
        stubs = cls.formatter_stub_points(root)
        groups: dict[
            tuple[str, str, tuple[float, float]],
            list[tuple[int, int, str, str, tuple[float, float]]],
        ] = defaultdict(list)
        for item in labels:
            groups[(item[2], item[3], dsu.find(item[4]))].append(item)

        removals: list[tuple[int, int]] = []
        removed_by_name: Counter[str] = Counter()
        for (_token, name, _component), members in groups.items():
            if len(members) < 2:
                continue
            if any((item[2], item[3], item[4]) in auxiliary_keys for item in members):
                continue
            # Prefer a generated left pin-stub label.  Otherwise retain the
            # deterministic upper-left member, which keeps unconnected islands
            # separate and makes repeated runs idempotent.
            keep = min(
                members,
                key=lambda item: (
                    0 if item[4] in stubs else 1,
                    item[4][0],
                    item[4][1],
                    item[0],
                ),
            )
            for item in members:
                if item is keep:
                    continue
                removals.append((item[0], item[1]))
                removed_by_name[name] += 1
        return removals, removed_by_name


class DeduplicateCommand:
    """Command-line entry: check or apply the removal of redundant labels."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Remove redundant same-conductor labels from one schematic.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 after printing the redundant-label count and the count
            per net name. ``--apply`` writes the result atomically to
            ``--output`` or the schematic when any label was redundant.
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
        removals, by_name = LabelDeduplicator.plan(source)
        print(f"redundant_labels={len(removals)}")
        for name, count in sorted(by_name.items()):
            print(f"{name}: {count}")
        if args.apply and removals:
            result = source
            for start, end in reversed(sorted(removals)):
                result = result[:start] + result[end:]
            AtomicTextWriter(TEMP_SUFFIX).write(args.output or args.schematic, result)
            print("applied atomically")
        elif args.apply:
            print("already deduplicated")
        return 0


main = DeduplicateCommand()


if __name__ == "__main__":
    raise SystemExit(main())
