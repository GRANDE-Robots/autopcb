#!/usr/bin/env python3
"""restore_endpoint_labels.py: tag bare wire ends without duplicating visible labels.

This is the safe complement to same-conductor label consolidation.  It finds
every wire end that KiCad still regards as bare, derives the one existing
global-net name on that physical conductor, and adds an auxiliary *hidden*
electrical tag at that endpoint.  The visible 30-mil label remains singular
and clean; the tiny tag exists only so KiCad keeps the established short-stub
topology ERC-clean.  Ambiguous or unnamed conductors stop the operation.  No
component, wire, pin, or PCB data changes; writes are atomic.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from pathlib import Path

from sch.deduplicate_labels import AUXILIARY_TEXT_SIZE, DisjointSet, PhysicalWires
from sch.schematic_io import AtomicTextWriter
from sch.standardize_block_symbols import (
    TEXT_SIZE,
    NewSchematicItems,
    SheetPoints,
    balanced_end,
    fmt,
    point_key,
    top_level,
    top_level_points,
)

TEMP_SUFFIX = ".endpoint-labels.tmp"


class EndpointLabels:
    """Finds bare wire ends and renders the hidden tags that connect them."""

    @staticmethod
    def global_label_records(
        root: str,
    ) -> list[tuple[int, int, str, tuple[float, float], str]]:
        """Read every global label as (start, end, name, point, text).

        Raises ValueError for a label that has no name or not exactly one anchor.
        """
        result = []
        for start, end, block in top_level(root, "global_label"):
            name = re.match(r'\(global_label\s+"((?:[^"\\]|\\.)*)"', block)
            points = top_level_points(block, "global_label")
            if not name or len(points) != 1:
                raise ValueError("malformed global label")
            result.append(
                (
                    start,
                    end,
                    name.group(1).replace(r'\\"', '"'),
                    point_key(*points[0]),
                    block,
                )
            )
        return result

    @staticmethod
    def wire_graph(
        root: str,
        attachments: set[tuple[float, float]],
    ) -> tuple[
        DisjointSet,
        list[tuple[tuple[float, float], tuple[float, float], int, int, bool]],
        Counter[tuple[float, float]],
    ]:
        """Build the wire connectivity of a sheet.

        Inputs:
            root: Text of the ``(kicad_sch ...)`` root.
            attachments: Points, in mm, that join a wire they lie on.
        Outputs:
            The components, each wire segment as (first, second, wire start,
            wire end, whether the wire has exactly two points), and the number of
            segments ending at each point. Raises ValueError for a wire with
            fewer than two points.
        """
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
                if PhysicalWires.on_segment(point, first, second):
                    dsu.union(point, first)
                    dsu.union(point, second)
        return dsu, segments, degree

    @staticmethod
    def outward_angle(point: tuple[float, float], neighbor: tuple[float, float]) -> int:
        """Label angle, 180 or 0 degrees, that points away from the wire's neighbor."""
        if abs(point[0] - neighbor[0]) > 1e-6:
            return 180 if point[0] < neighbor[0] else 0
        # A vertical stub has no natural text side; retain the project's standard
        # right-justified outward label rather than generating rotated text.
        return 180

    @staticmethod
    def hidden_label_block(net: str, x: float, y: float, angle: int) -> str:
        """Emit an ERC-only global tag that is invisible at normal schematic zoom.

        Raises ValueError when the generated label lacks the visible font size.
        """
        visible = f"(size {fmt(TEXT_SIZE)} {fmt(TEXT_SIZE)})"
        hidden = f"(size {fmt(AUXILIARY_TEXT_SIZE)} {fmt(AUXILIARY_TEXT_SIZE)})"
        original = NewSchematicItems.label_block(net, x, y, angle=angle)
        result = original.replace(visible, hidden, 1)
        if result == original:
            raise ValueError("generated label lacked its visible font size")
        return result

    @classmethod
    def plan(
        cls,
        source: str,
    ) -> tuple[
        list[tuple[tuple[float, float], str, int]],
        list[tuple[float, float]],
    ]:
        """Find the bare wire ends of a schematic and the tag each needs.

        Inputs:
            source: Complete schematic text.
        Outputs:
            The (point, net name, angle) of each hidden tag to add, sorted by
            point, and the sorted bare ends whose conductor has no single global
            name. Raises ValueError when the source is not a KiCad schematic or a
            wire or global label is malformed.
        """
        root_start = source.find("(kicad_sch")
        if root_start < 0:
            raise ValueError("not a KiCad schematic")
        root_end = balanced_end(source, root_start)
        root = source[root_start:root_end]
        pins = SheetPoints.pin_points(root)
        global_labels = cls.global_label_records(root)
        globals_by_point: dict[tuple[float, float], list[str]] = defaultdict(list)
        for _start, _end, name, point, _block in global_labels:
            globals_by_point[point].append(name)
        any_labels = set(globals_by_point)
        for token in ("label", "hierarchical_label"):
            any_labels |= SheetPoints.anchored(root, token)
        junctions = SheetPoints.anchored(root, "junction")
        no_connects = SheetPoints.anchored(root, "no_connect")
        attachments = pins | any_labels | junctions | no_connects
        dsu, segments, degree = cls.wire_graph(root, attachments)
        names_by_component: dict[tuple[float, float], set[str]] = defaultdict(set)
        for _start, _end, name, point, _block in global_labels:
            names_by_component[dsu.find(point)].add(name)

        additions = []
        unresolved = []
        for first, second, _wire_start, _wire_end, _simple in segments:
            for point, neighbor in ((first, second), (second, first)):
                if degree[point] != 1 or point in attachments:
                    continue
                names = names_by_component.get(dsu.find(point), set())
                if len(names) != 1:
                    unresolved.append(point)
                    continue
                additions.append(
                    (point, next(iter(names)), cls.outward_angle(point, neighbor))
                )

        # Defensive deduplication makes reruns idempotent if a malformed source
        # repeats an identical bare end.
        dedup_additions = {}
        for point, name, angle in additions:
            dedup_additions[point] = (name, angle)
        additions = [
            (point, name, angle)
            for point, (name, angle) in sorted(dedup_additions.items())
        ]
        return additions, sorted(set(unresolved))


class EndpointLabelsCommand:
    """Command-line entry: check or apply hidden tags on bare wire ends."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Add hidden endpoint tags to one schematic.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 after printing the tags to add; 2 when a bare end has no
            single global name (nothing is written). ``--apply`` inserts the
            tags before ``sheet_instances`` and replaces the file atomically.
            Raises ValueError when ``--apply`` finds no ``sheet_instances``.
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
        additions, unresolved = EndpointLabels.plan(source)
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
                EndpointLabels.hidden_label_block(name, point[0], point[1], angle)
                for point, name, angle in additions
            )
            result = root[:insert] + blocks + root[insert:] + source[root_end:]
            AtomicTextWriter(TEMP_SUFFIX).write(args.output or args.schematic, result)
            print("applied atomically")
        elif args.apply:
            print("no labels needed")
        return 0


main = EndpointLabelsCommand()


if __name__ == "__main__":
    raise SystemExit(main())
