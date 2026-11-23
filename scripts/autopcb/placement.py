"""Fit a new rectangular outline around catalog placement groups, in KiCad units."""

from __future__ import annotations

from dataclasses import dataclass

from core.compat import strict_zip


@dataclass(frozen=True)
class Envelope:
    low: tuple[int, int]
    high: tuple[int, int]

    def shifted(self, axis: int, distance: int) -> Envelope:
        dx, dy = (distance, 0) if axis == 0 else (0, distance)
        return Envelope((self.low[0] + dx, self.low[1] + dy),
                        (self.high[0] + dx, self.high[1] + dy))



class Compactor:
    """Slide parts toward a point until each meets another part or the point itself.

    Boxes keep ``gap`` between them and stay ``margin`` inside the outline,
    so packing a board this way never makes two courtyards overlap.
    """

    def __init__(self, parts: dict[str, Envelope], movable: set[str], outline: Envelope,
                 margin: int, gap: int):
        if not movable <= parts.keys() or margin < 0 or gap < 0:
            raise ValueError("Compaction needs movable parts on the board and nonnegative spacing.")
        self.parts, self.movable, self.outline = parts, movable, outline
        self.margin, self.gap = margin, gap

    def slide(self, boxes: dict[str, Envelope], ref: str, axis: int, target: int) -> int:
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
            if (other == ref or obstacle.high[across] + self.gap <= box.low[across]
                    or box.high[across] + self.gap <= obstacle.low[across]):
                continue
            if wanted > 0 and obstacle.low[axis] >= box.high[axis]:
                limit = min(limit, obstacle.low[axis] - box.high[axis] - self.gap)
            elif wanted < 0 and obstacle.high[axis] <= box.low[axis]:
                limit = min(limit, box.low[axis] - obstacle.high[axis] - self.gap)
        travel = max(0, min(abs(wanted), limit)) * (1 if wanted > 0 else -1)
        if travel:
            boxes[ref] = box.shifted(axis, travel)
        return travel

    def toward(self, point: tuple[int, int], order: list[str], passes: int = 6) -> dict[str, Envelope]:
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

    def __init__(self, parts: dict[str, Envelope], groups: list[list[str]],
                 minimum: tuple[int, int], maximum: tuple[int, int], margin: int):
        self.parts, self.groups = parts, groups
        self.minimum, self.maximum, self.margin = minimum, maximum, margin
        movable = [ref for group in groups for ref in group]
        if (not parts or margin < 0 or any(not group for group in groups)
                or len(set(movable)) != len(movable) or not set(movable) <= parts.keys()
                or any(low <= 0 or low > high for low, high in strict_zip(minimum, maximum))):
            raise ValueError("Invalid compact outline limits or movable footprint groups.")
        for ref, box in parts.items():
            if min(box.low) < margin:
                raise ValueError(f"{ref} crosses the reserved margin at the fixed left or top board edge.")
        fixed = {ref: box for ref, box in parts.items() if ref not in movable}
        self.target = self.dimensions(fixed)

    def dimensions(self, boxes):
        return tuple(max([self.minimum[axis], *(box.high[axis] + self.margin for box in boxes.values())])
                     for axis in (0, 1))

    def slide(self, boxes, group, axis):
        across = 1 - axis
        wanted = max(boxes[ref].high[axis] for ref in group) + self.margin - self.target[axis]
        travel = min(wanted, min(boxes[ref].low[axis] for ref in group) - self.margin)
        for ref in group:
            box = boxes[ref]
            for other, obstacle in boxes.items():
                if (other in group or obstacle.low[axis] >= box.high[axis]
                        or obstacle.high[across] + self.margin <= box.low[across]
                        or box.high[across] + self.margin <= obstacle.low[across]):
                    continue
                travel = min(travel, box.low[axis] - obstacle.high[axis] - self.margin)
        if travel > 0:
            for ref in group:
                boxes[ref] = boxes[ref].shifted(axis, -travel)

    def score(self, boxes):
        width, height = self.dimensions(boxes)
        movement = sum(abs(box.low[i] - self.parts[ref].low[i]) for ref, box in boxes.items() for i in (0, 1))
        return (width > self.maximum[0] or height > self.maximum[1], width * height, movement)

    def fit(self):
        best = self.parts
        if self.groups and self.dimensions(best) != self.minimum:
            for axes in ((0, 1), (1, 0)):
                boxes = self.parts.copy()
                for axis in axes:
                    for group in sorted(self.groups, key=lambda g: min(boxes[ref].low[axis] for ref in g)):
                        self.slide(boxes, group, axis)
                if self.score(boxes) < self.score(best):
                    best = boxes
        size = self.dimensions(best)
        if any(value > cap for value, cap in strict_zip(size, self.maximum)):
            raise ValueError("No tested placement fits the approved maximum outline and edge margin.")
        offsets = {ref: (box.low[0] - self.parts[ref].low[0], box.low[1] - self.parts[ref].low[1])
                   for ref, box in best.items() if box != self.parts[ref]}
        return size, offsets
