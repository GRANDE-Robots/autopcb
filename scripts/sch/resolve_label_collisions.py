#!/usr/bin/env python3
"""resolve_label_collisions.py: clear left-facing labels off neighboring block bodies.

The standardized schematic uses short, left-facing labeled stubs.  When two
independent blocks are placed too closely, a left label from one can print
through the neighboring body.  This utility finds those cases generically,
then shifts the *label-owning* block bundle right by the smallest native-grid
amount that clears every visible object.  If a rightward position is genuinely
unavailable, it finds the smallest clear two-dimensional native-grid location
instead of shrinking text, breaking the left-facing label convention, or
forcing a collision.  A bundle comprises the symbol, Reference/Value fields,
its short stubs, labels, no-connect markers and a nearby 30-mil caption. Pins,
names, numbers, nets, values, and wiring topology are preserved exactly.

It intentionally fails closed when a collision is caused by an unowned label:
that situation needs a human layout decision rather than a guessed move.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from sch.schematic_io import AtomicTextWriter
from sch.standardize_block_symbols import (
    AT_RE,
    NATIVE_GRID,
    PAGE_BOTTOM,
    PAGE_RIGHT,
    TITLE_BLOCK_LEFT,
    TITLE_BLOCK_TOP,
    XY_RE,
    PlanGeometry,
    SymbolInstance,
    VisibleText,
    balanced_end,
    instances,
    is_target,
    library_units,
    local_screen_point,
    point_key,
    source_lib_definitions,
    top_level,
    top_level_points,
)

LABEL_TOKENS = ("global_label", "hierarchical_label", "label")
MARK_TOKENS = ("no_connect", "junction")
AT_SHIFT_RE = re.compile(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)(\s+-?[\d.]+)?\)")
RECT_RE = re.compile(
    r"\(rectangle\s+\(start\s+(-?[\d.]+)\s+(-?[\d.]+)\)\s+"
    r"\(end\s+(-?[\d.]+)\s+(-?[\d.]+)\)",
    re.S,
)
HIDDEN_LABEL_RE = re.compile(r"\(font\s+\(size\s+0\.01\s+0\.01\)\s*\)")
TEMP_SUFFIX = ".labelspacing.tmp"


@dataclass(frozen=True)
class Record:
    """One top-level schematic object with its position and visible area.

    Inputs:
        start, end: Offsets of the object in the schematic root.
        token: The object's kind, for example ``wire`` or ``global_label``.
        block: The object's text.
        point: Anchor in mm, or None when it has none.
        box: Visible (left, top, right, bottom) in mm, or None when it has no
            visible text.
    """

    start: int
    end: int
    token: str
    block: str
    point: tuple[float, float] | None
    box: tuple[float, float, float, float] | None


class BundleShifter:
    """Finds a clear shift for each label-owning bundle that collides.

    Inputs:
        bundle_boxes: Visible boxes of each target's bundle by reference.
        static_boxes: Visible boxes of everything that does not move.
        body_by_ref: Body rectangles of each target by reference.
        collisions: For each owner, the references whose bodies its labels hit.
    The chosen shifts, in mm, accumulate in ``shifts``.
    """

    def __init__(
        self,
        bundle_boxes: dict[str, list[tuple[float, float, float, float]]],
        static_boxes: list[tuple[float, float, float, float]],
        body_by_ref: dict[str, list[tuple[float, float, float, float]]],
        collisions: dict[str, set[str]],
    ) -> None:
        """Store the obstacles; no bundle has moved yet."""
        self.bundle_boxes = bundle_boxes
        self.static_boxes = static_boxes
        self.body_by_ref = body_by_ref
        self.collisions = collisions
        self.protected_victims = {
            victim for victims in collisions.values() for victim in victims
        }
        self.shifts: dict[str, tuple[float, float]] = {}

    @staticmethod
    def shift_box(
        box: tuple[float, float, float, float], dx: float, dy: float
    ) -> tuple[float, float, float, float]:
        """Return ``box`` moved by (dx, dy) in mm."""
        return (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)

    @staticmethod
    def inside_sheet(box: tuple[float, float, float, float]) -> bool:
        """Whether a box is within the page and clear of the title block."""
        if not PlanGeometry.inside_page(box):
            return False
        return not PlanGeometry.boxes_overlap(
            box, (TITLE_BLOCK_LEFT, TITLE_BLOCK_TOP, PAGE_RIGHT, PAGE_BOTTOM)
        )

    def clear_group(self, group: set[str], dx: float, dy: float) -> bool:
        """Whether moving a group of bundles by (dx, dy) mm collides with nothing."""
        candidate = [
            self.shift_box(box, dx, dy)
            for reference in group
            for box in self.bundle_boxes[reference]
        ]
        if not all(self.inside_sheet(box) for box in candidate):
            return False
        if any(
            PlanGeometry.boxes_overlap(box, obstacle)
            for box in candidate
            for obstacle in self.static_boxes
        ):
            return False
        for other, boxes in self.bundle_boxes.items():
            if other in group:
                continue
            other_dx, other_dy = self.shifts.get(other, (0.0, 0.0))
            if any(
                PlanGeometry.boxes_overlap(
                    candidate_box, self.shift_box(obstacle, other_dx, other_dy)
                )
                for candidate_box in candidate
                for obstacle in boxes
            ):
                return False
        return True

    def minimum_right_clearance(self, owner: str) -> float | None:
        """Smallest rightward shift in mm that frees ``owner`` from its victims.

        Outputs:
            The shift, trying 1 to 96 grid cells, or None when none fits.
        """
        for step in range(1, 97):
            dx = step * NATIVE_GRID
            candidate = [
                self.shift_box(box, dx, 0.0) for box in self.bundle_boxes[owner]
            ]
            if not all(self.inside_sheet(box) for box in candidate):
                continue
            if not any(
                PlanGeometry.boxes_overlap(candidate_box, victim_box)
                for victim in self.collisions[owner]
                for candidate_box in candidate
                for victim_box in self.body_by_ref[victim]
            ):
                return dx
        return None

    def cluster_blockers(self, group: set[str], dx: float) -> set[str]:
        """References outside ``group`` whose bundles a rightward shift would hit."""
        candidate = [
            self.shift_box(box, dx, 0.0)
            for reference in group
            for box in self.bundle_boxes[reference]
        ]
        return {
            reference
            for reference, boxes in self.bundle_boxes.items()
            if reference not in group
            and any(
                PlanGeometry.boxes_overlap(
                    candidate_box,
                    self.shift_box(obstacle, *self.shifts.get(reference, (0.0, 0.0))),
                )
                for candidate_box in candidate
                for obstacle in boxes
            )
        }

    def shift_owner(self, owner: str) -> None:
        """Choose and record the shift of one owner, and of any cluster it needs.

        Left-facing labels belong left of their body, so the owner shifts right
        first.  When that cannot fit in a dense connector bank, the smallest
        automatically discovered local cluster moves by the nearest
        right/up/down native-grid offset.  That preserves the block treatment
        without creating a large empty gap or a reference/value collision.
        Raises ValueError when no rightward clearance or no clear placement
        exists.
        """
        if owner in self.shifts:
            return
        plan_group = {owner}
        chosen: tuple[float, float] | None = None
        for step in range(1, 97):
            dx = step * NATIVE_GRID
            if self.clear_group({owner}, dx, 0.0):
                chosen = (dx, 0.0)
                break
        if chosen is None:
            minimum_dx = self.minimum_right_clearance(owner)
            if minimum_dx is None:
                raise ValueError(
                    f"no rightward clearance from collision victim(s) for {owner}"
                )
            base_step = round(minimum_dx / NATIVE_GRID)
            offsets = [
                (x_step, y_step)
                for x_step in range(base_step, base_step + 49)
                for y_step in range(-24, 25)
            ]
            offsets.sort(
                key=lambda offset: (
                    abs(offset[0] - base_step) + abs(offset[1]),
                    0 if offset[1] == 0 else 1,
                    abs(offset[1]),
                    offset[0],
                    offset[1],
                )
            )
            while chosen is None:
                chosen = next(
                    (
                        (x_step * NATIVE_GRID, y_step * NATIVE_GRID)
                        for x_step, y_step in offsets
                        if self.clear_group(
                            plan_group, x_step * NATIVE_GRID, y_step * NATIVE_GRID
                        )
                    ),
                    None,
                )
                if chosen is not None:
                    break
                movable = (
                    self.cluster_blockers(plan_group, minimum_dx)
                    - self.protected_victims
                    - set(self.shifts)
                )
                if not movable:
                    break
                plan_group.update(movable)
        if chosen is None:
            raise ValueError(
                f"no clear native-grid placement for {owner} group "
                f"({', '.join(sorted(plan_group))})"
            )
        for reference in plan_group:
            self.shifts[reference] = chosen


class LabelCollisions:
    """Plans the shifts that clear left-facing labels off neighboring bodies."""

    @staticmethod
    def at_point(block: str) -> tuple[float, float] | None:
        """Return the first ``(at x y)`` of a block as a point key, or None."""
        match = AT_RE.search(block)
        if not match:
            return None
        return point_key(float(match.group(1)), float(match.group(2)))

    @classmethod
    def record_blocks(cls, root: str, token: str) -> list[Record]:
        """Read every top-level ``token`` object with its anchor and visible area.

        Hidden 0.01 mm endpoint tags get no visible area.
        """
        records: list[Record] = []
        for start, end, block in top_level(root, token):
            point = cls.at_point(block)
            box = (
                None
                if HIDDEN_LABEL_RE.search(block)
                else VisibleText.visible_block_box(block, token)
            )
            records.append(Record(start, end, token, block, point, box))
        return records

    @staticmethod
    def body_boxes(
        item: SymbolInstance, definition: str
    ) -> list[tuple[float, float, float, float]]:
        """Sheet-coordinate boxes, in mm, of a placed symbol's body rectangles.

        Raises ValueError when the definition has no body rectangle.
        """
        boxes: list[tuple[float, float, float, float]] = []
        for match in RECT_RE.finditer(definition):
            x1, y1, x2, y2 = map(float, match.groups())
            corners = [
                local_screen_point(item.x, item.y, item.rotation, item.mirror, x, y)
                for x, y in ((x1, y1), (x1, y2), (x2, y1), (x2, y2))
            ]
            boxes.append(
                (
                    min(point[0] for point in corners),
                    min(point[1] for point in corners),
                    max(point[0] for point in corners),
                    max(point[1] for point in corners),
                )
            )
        if not boxes:
            raise ValueError(f"{item.reference} has no visible body rectangle")
        return boxes

    @staticmethod
    def pin_points(item: SymbolInstance, definition: str) -> set[tuple[float, float]]:
        """Sheet coordinates, in mm, of every pin of a placed symbol's unit.

        Raises ValueError when the definition has no such unit.
        """
        units = library_units(definition)
        if item.unit not in units:
            raise ValueError(f"{item.reference} has no unit {item.unit}")
        return {item.pin_point(pin) for pin in units[item.unit][1]}

    @staticmethod
    def shift_at_block(block: str, dx: float, dy: float) -> str:
        """Move every ``(at x y [angle])`` of a block by (dx, dy) mm.

        Raises ValueError when the block has no position.
        """

        def replacement(match: re.Match[str]) -> str:
            """Render one matched position moved by (dx, dy), keeping its angle."""
            angle = match.group(3) or ""
            return f"(at {float(match.group(1)) + dx:.6g} {float(match.group(2)) + dy:.6g}{angle})"

        shifted, count = AT_SHIFT_RE.subn(replacement, block)
        if count == 0:
            raise ValueError("bundle block lacks an at coordinate")
        return shifted

    @staticmethod
    def shift_wire_block(block: str, dx: float, dy: float) -> str:
        """Move every ``(xy x y)`` of a wire by (dx, dy) mm.

        Raises ValueError when the wire has no coordinates.
        """

        def replacement(match: re.Match[str]) -> str:
            """Render one matched point moved by (dx, dy)."""
            return f"(xy {float(match.group(1)) + dx:.6g} {float(match.group(2)) + dy:.6g})"

        shifted, count = XY_RE.subn(replacement, block)
        if count == 0:
            raise ValueError("wire lacks coordinates")
        return shifted

    @staticmethod
    def caption_records(
        text_records: list[Record], body: list[tuple[float, float, float, float]]
    ) -> list[Record]:
        """Return the small texts that sit just below a body, as its caption."""
        left = min(box[0] for box in body)
        right = max(box[2] for box in body)
        bottom = max(box[3] for box in body)
        result: list[Record] = []
        for record in text_records:
            if record.box is None:
                continue
            if VisibleText.visible_text_size(record.block) > 1.0:
                continue
            center_x = (record.box[0] + record.box[2]) / 2
            if (
                left - 5.08 <= center_x <= right + 5.08
                and bottom - 0.01 <= record.box[1] <= bottom + 5.08
            ):
                result.append(record)
        return result

    @classmethod
    def build_plan(cls, source: str) -> tuple[str, list[tuple[str, float, float, int]]]:
        """Shift the label-owning bundles whose labels print through a neighbor.

        Inputs:
            source: Complete schematic text.
        Outputs:
            The new schematic text and, per shifted owner, its reference, the
            shift right and down in mm, and the number of foreign bodies it
            clears. Raises ValueError when the source is not one KiCad
            schematic, a wire is not a two-point stub, a collision is caused by
            an unowned label, or no clear placement exists.
        """
        root_start = source.find("(kicad_sch")
        if root_start != 0:
            raise ValueError("not a KiCad schematic")
        root_end = balanced_end(source, root_start)
        if root_end != len(source.rstrip()):
            raise ValueError("unexpected content outside KiCad schematic root")
        root = source[root_start:root_end]
        _lib_start, _lib_end, definitions = source_lib_definitions(root)
        all_items = instances(root)
        # U3 is intentionally drawn as two folded visual units and is not moved by
        # this compact single-block reflow.  It remains an ordinary static obstacle.
        candidate_targets = [
            item for item in all_items if is_target(item.reference, item.lib_id)
        ]
        reference_counts: dict[str, int] = defaultdict(int)
        for item in candidate_targets:
            reference_counts[item.reference] += 1
        # Like folded U3, a multi-unit IC needs a dedicated whole-device reflow;
        # retain it as a static obstacle rather than moving one unit independently.
        targets = [
            item for item in candidate_targets if reference_counts[item.reference] == 1
        ]
        target_by_reference = {item.reference: item for item in targets}

        labels = [
            record
            for token in LABEL_TOKENS
            for record in cls.record_blocks(root, token)
        ]
        texts = cls.record_blocks(root, "text")
        marks = [
            record for token in MARK_TOKENS for record in cls.record_blocks(root, token)
        ]
        wires: list[Record] = []
        for start, end, block in top_level(root, "wire"):
            points = top_level_points(block, "wire")
            if len(points) != 2:
                raise ValueError("only straight two-point stubs can be reflowed")
            wires.append(Record(start, end, "wire", block, None, None))

        labels_at: dict[tuple[float, float], list[Record]] = defaultdict(list)
        for record in labels:
            if record.point is not None:
                labels_at[record.point].append(record)
        marks_at: dict[tuple[float, float], list[Record]] = defaultdict(list)
        for record in marks:
            if record.point is not None:
                marks_at[record.point].append(record)
        wires_at: dict[
            tuple[float, float], list[tuple[Record, tuple[float, float]]]
        ] = defaultdict(list)
        for record in wires:
            points = [
                point_key(*point) for point in top_level_points(record.block, "wire")
            ]
            wires_at[points[0]].append((record, points[1]))
            wires_at[points[1]].append((record, points[0]))

        body_by_ref = {
            item.reference: cls.body_boxes(item, definitions[item.lib_id][2])
            for item in targets
        }
        owned_records: dict[str, dict[tuple[int, int], Record]] = defaultdict(dict)
        owned_visible_labels: dict[tuple[int, int], str] = {}
        for item in targets:
            owner = owned_records[item.reference]
            owner[(item.start, item.end)] = Record(
                item.start, item.end, "symbol", item.block, None, None
            )
            for point in cls.pin_points(item, definitions[item.lib_id][2]):
                for mark in marks_at.get(point, []):
                    owner[(mark.start, mark.end)] = mark
                for label in labels_at.get(point, []):
                    owner[(label.start, label.end)] = label
                    owned_visible_labels[(label.start, label.end)] = item.reference
                for wire, remote in wires_at.get(point, []):
                    owner[(wire.start, wire.end)] = wire
                    for label in labels_at.get(remote, []):
                        owner[(label.start, label.end)] = label
                        owned_visible_labels[(label.start, label.end)] = item.reference
                    for mark in marks_at.get(remote, []):
                        owner[(mark.start, mark.end)] = mark
            for text in cls.caption_records(texts, body_by_ref[item.reference]):
                owner[(text.start, text.end)] = text

        collisions: dict[str, set[str]] = defaultdict(set)
        unowned: list[tuple[str, Record]] = []
        for victim in targets:
            for label in labels:
                if label.box is None:
                    continue
                if any(
                    PlanGeometry.boxes_overlap(box, label.box)
                    for box in body_by_ref[victim.reference]
                ):
                    owner = owned_visible_labels.get((label.start, label.end))
                    if owner is None:
                        unowned.append((victim.reference, label))
                    elif owner != victim.reference:
                        collisions[owner].add(victim.reference)
        if unowned:
            names = ", ".join(
                sorted({f"{victim}:{record.token}" for victim, record in unowned})
            )
            raise ValueError(f"foreign body collision has unowned label(s): {names}")
        if not collisions:
            return source, []

        # Keep the geometry of every target bundle together.  Its body, fields,
        # short stubs, labels and caption must all travel as one visual unit.
        all_owned_spans = {span for bundle in owned_records.values() for span in bundle}
        bundle_boxes: dict[str, list[tuple[float, float, float, float]]] = {}
        for reference, item in target_by_reference.items():
            decorations = VisibleText.visible_symbol_property_boxes(item.block)
            decorations.extend(
                record.box
                for record in owned_records[reference].values()
                if record.box is not None
            )
            bundle_boxes[reference] = list(body_by_ref[reference]) + decorations

        static_boxes: list[tuple[float, float, float, float]] = []
        for item in all_items:
            if item.reference in target_by_reference:
                continue
            envelope = VisibleText.instance_pin_envelope(item, definitions)
            if envelope:
                static_boxes.append(envelope)
            static_boxes.extend(VisibleText.visible_symbol_property_boxes(item.block))
        for record in labels + texts:
            if (
                record.box is not None
                and (record.start, record.end) not in all_owned_spans
            ):
                static_boxes.append(record.box)

        shifter = BundleShifter(bundle_boxes, static_boxes, body_by_ref, collisions)
        for owner in sorted(
            collisions,
            key=lambda reference: (
                target_by_reference[reference].y,
                target_by_reference[reference].x,
                reference,
            ),
        ):
            shifter.shift_owner(owner)
        shifts = shifter.shifts

        replacements: list[tuple[int, int, str]] = []
        for owner, (dx, dy) in shifts.items():
            for record in owned_records[owner].values():
                replacement = (
                    cls.shift_wire_block(record.block, dx, dy)
                    if record.token == "wire"
                    else cls.shift_at_block(record.block, dx, dy)
                )
                replacements.append((record.start, record.end, replacement))
        for start, end, replacement in sorted(replacements, reverse=True):
            root = root[:start] + replacement + root[end:]
        result = source[:root_start] + root + source[root_end:]
        planned = [
            (owner, shifts[owner][0], shifts[owner][1], len(collisions[owner]))
            for owner in sorted(shifts)
        ]
        return result, planned


class LabelCollisionCommand:
    """Command-line entry: check or apply the label-collision reflow."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Shift colliding label-owner bundles of one schematic.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 after printing each shifted bundle. ``--apply`` writes
            the result atomically to ``--output`` or the schematic when it
            differs from the source.
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("schematic", type=Path)
        parser.add_argument(
            "--output", type=Path, help="destination for an atomic --apply result"
        )
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--check", action="store_true")
        mode.add_argument("--apply", action="store_true")
        args = parser.parse_args(argv)
        if args.output and not args.apply:
            parser.error("--output requires --apply")
        source = args.schematic.read_text(encoding="utf-8")
        result, shifts = LabelCollisions.build_plan(source)
        print(f"colliding label-owner bundles: {len(shifts)}")
        for reference, dx, dy, blockers in shifts:
            direction = f"{dx:+.2f} mm right, {dy:+.2f} mm down"
            print(f"  {reference}: {direction}; clears {blockers} foreign body/bodies")
        if args.apply and result != source:
            destination = args.output or args.schematic
            AtomicTextWriter(TEMP_SUFFIX).write(destination, result)
            print("applied atomically")
        elif args.apply:
            print("already clear")
        else:
            print("check only; no schematic changed")
        return 0


main = LabelCollisionCommand()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"label-body reflow aborted: {error}", file=sys.stderr)
        raise SystemExit(2) from None
