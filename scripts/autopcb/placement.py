"""placement.py: slide parts together and fit a rectangular outline around them.

This is pure geometry. Boxes and distances are integers in KiCad internal units
(nm), and no KiCad object is touched.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from dataclasses import dataclass

from core.compat import strict_zip


@dataclass(frozen=True)
class Envelope:
    """An axis-aligned box given by its low and high corners, each an (x, y) pair in nm."""

    low: tuple[int, int]
    high: tuple[int, int]

    def shifted(self, axis: int, distance: int) -> Envelope:
        """A copy of the box moved by ``distance`` nm along an axis (0 for x, 1 for y)."""
        dx, dy = (distance, 0) if axis == 0 else (0, distance)
        return Envelope(
            (self.low[0] + dx, self.low[1] + dy), (self.high[0] + dx, self.high[1] + dy)
        )


class Compactor:
    """Slide parts toward a point until each meets another part or the point itself.

    Boxes keep ``gap`` between them and stay ``margin`` inside the outline,
    so packing a board this way never makes two courtyards overlap.
    """

    def __init__(
        self,
        parts: dict[str, Envelope],
        movable: set[str],
        outline: Envelope,
        margin: int,
        gap: int,
    ):
        """Set up packing for one board.

        Inputs:
            parts: Box of every part, by reference.
            movable: References of the parts that may move.
            outline: Board outline box.
            margin: Closest a box may come to the outline, in nm.
            gap: Closest two boxes may come to each other, in nm.
        Outputs:
            None. Raises ValueError when a movable reference is not a part or the margin or
            gap is negative.
        """
        if not movable <= parts.keys() or margin < 0 or gap < 0:
            raise ValueError(
                "Compaction needs movable parts on the board and nonnegative spacing."
            )
        self.parts, self.movable, self.outline = parts, movable, outline
        self.margin, self.gap = margin, gap

    def slide(
        self, boxes: dict[str, Envelope], ref: str, axis: int, target: int
    ) -> int:
        """Move one part along an axis toward a target until it meets the outline or a part.

        Inputs:
            boxes: Current boxes by reference; the moved part's box is replaced.
            ref: Part to move.
            axis: 0 for x, 1 for y.
            target: Coordinate in nm that the part's centre should reach.
        Outputs:
            The signed distance moved in nm, 0 when the part cannot move.
        """
        across, box = 1 - axis, boxes[ref]
        wanted = target - (box.low[axis] + box.high[axis]) // 2
        if not wanted:
            return 0
        if wanted > 0:
            limit = self.outline.high[axis] - self.margin - box.high[axis]
        else:
            limit = box.low[axis] - self.outline.low[axis] - self.margin
        limit = max(0, limit)
        for other, obstacle in boxes.items():
            if (
                other == ref
                or obstacle.high[across] + self.gap <= box.low[across]
                or box.high[across] + self.gap <= obstacle.low[across]
            ):
                continue
            if wanted > 0 and obstacle.low[axis] >= box.high[axis]:
                limit = min(limit, obstacle.low[axis] - box.high[axis] - self.gap)
            elif wanted < 0 and obstacle.high[axis] <= box.low[axis]:
                limit = min(limit, box.low[axis] - obstacle.high[axis] - self.gap)
        travel = max(0, min(abs(wanted), limit)) * (1 if wanted > 0 else -1)
        if travel:
            boxes[ref] = box.shifted(axis, travel)
        return travel

    def toward(
        self, point: tuple[int, int], order: list[str], passes: int = 6
    ) -> dict[str, Envelope]:
        """Slide the movable parts toward a point until no part moves.

        Inputs:
            point: Target (x, y) in nm.
            order: References in the order they are slid on each pass.
            passes: Most passes to make.
        Outputs:
            The box of every part after packing.
        """
        boxes = dict(self.parts)
        for _ in range(passes):
            moved = False
            for ref in order:
                if ref in self.movable:
                    for axis in (0, 1):
                        moved |= bool(self.slide(boxes, ref, axis, point[axis]))
            if not moved:
                break
        return boxes


class OutlineFitter:
    """Try the approved group translations and the unmodified catalog placement."""

    def __init__(
        self,
        parts: dict[str, Envelope],
        groups: list[list[str]],
        minimum: tuple[int, int],
        maximum: tuple[int, int],
        margin: int,
    ):
        """Check the limits and the fixed parts, and measure the fixed target size.

        Inputs:
            parts: Box of every part, by reference.
            groups: References that move together, one list per movable group.
            minimum: Smallest outline (width, height), in nm.
            maximum: Largest outline (width, height), in nm.
            margin: Closest a box may come to an outline edge, in nm.
        Outputs:
            None. Raises ValueError for invalid limits, empty, repeated or unknown group
            members, or a part that crosses the margin at the fixed left or top edge.
        """
        self.parts, self.groups = parts, groups
        self.minimum, self.maximum, self.margin = minimum, maximum, margin
        movable = [ref for group in groups for ref in group]
        if (
            not parts
            or margin < 0
            or any(not group for group in groups)
            or len(set(movable)) != len(movable)
            or not set(movable) <= parts.keys()
            or any(low <= 0 or low > high for low, high in strict_zip(minimum, maximum))
        ):
            raise ValueError(
                "Invalid compact outline limits or movable footprint groups."
            )
        for ref, box in parts.items():
            if min(box.low) < margin:
                raise ValueError(
                    f"{ref} crosses the reserved margin at the fixed left or top board edge."
                )
        fixed = {ref: box for ref, box in parts.items() if ref not in movable}
        self.target = self.dimensions(fixed)

    def dimensions(self, boxes):
        """The outline size (width, height) in nm that contains the boxes.

        Outputs:
            On each axis the larger of the minimum and the farthest box edge plus margin.
        """
        return tuple(
            max(
                [
                    self.minimum[axis],
                    *(box.high[axis] + self.margin for box in boxes.values()),
                ]
            )
            for axis in (0, 1)
        )

    def slide(self, boxes, group, axis):
        """Move a group toward the low side of an axis as far as the target size allows.

        Inputs:
            boxes: Current boxes by reference; the group's boxes are replaced.
            group: References that move together.
            axis: 0 for x, 1 for y.
        """
        across = 1 - axis
        wanted = (
            max(boxes[ref].high[axis] for ref in group)
            + self.margin
            - self.target[axis]
        )
        travel = min(wanted, min(boxes[ref].low[axis] for ref in group) - self.margin)
        for ref in group:
            box = boxes[ref]
            for other, obstacle in boxes.items():
                if (
                    other in group
                    or obstacle.low[axis] >= box.high[axis]
                    or obstacle.high[across] + self.margin <= box.low[across]
                    or box.high[across] + self.margin <= obstacle.low[across]
                ):
                    continue
                travel = min(travel, box.low[axis] - obstacle.high[axis] - self.margin)
        if travel > 0:
            for ref in group:
                boxes[ref] = boxes[ref].shifted(axis, -travel)

    def score(self, boxes):
        """Rank a layout; smaller tuples are better.

        Outputs:
            Whether the outline exceeds the maximum, its area in nm squared, and the
            summed movement of the box corners from the original layout in nm.
        """
        width, height = self.dimensions(boxes)
        movement = sum(
            abs(box.low[i] - self.parts[ref].low[i])
            for ref, box in boxes.items()
            for i in (0, 1)
        )
        return (
            width > self.maximum[0] or height > self.maximum[1],
            width * height,
            movement,
        )

    def fit(self):
        """Pick the best of the unmodified placement and the tested group translations.

        Outputs:
            The outline size (width, height) in nm, and the (dx, dy) offset in nm of each
            part that moved. Raises ValueError when no tested placement fits the maximum.
        """
        best = self.parts
        if self.groups and self.dimensions(best) != self.minimum:
            for axes in ((0, 1), (1, 0)):
                boxes = self.parts.copy()
                for axis in axes:
                    for group in sorted(
                        self.groups,
                        key=lambda g: min(boxes[ref].low[axis] for ref in g),
                    ):
                        self.slide(boxes, group, axis)
                if self.score(boxes) < self.score(best):
                    best = boxes
        size = self.dimensions(best)
        if any(value > cap for value, cap in strict_zip(size, self.maximum)):
            raise ValueError(
                "No tested placement fits the approved maximum outline and edge margin."
            )
        offsets = {
            ref: (
                box.low[0] - self.parts[ref].low[0],
                box.low[1] - self.parts[ref].low[1],
            )
            for ref, box in best.items()
            if box != self.parts[ref]
        }
        return size, offsets
