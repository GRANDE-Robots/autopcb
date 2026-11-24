"""Poses worth trying for parts a blocked net may move, most promising first."""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import zip_longest

from pcb.route_optimizer import mm


@dataclass(frozen=True)
class Pose:
    kind: str
    moves: tuple[tuple[str, float, float, float], ...]
    displacement_mm: float
    rotation_deg: float


class PoseSampler:
    """The shortest moves that clear a diagnosed corridor, nudges, and relocations into open board.

    Where diagnosis found the corridor a blocked net wants, the smallest
    move along each axis that takes the selected parts off it comes first.
    Nudges along each axis and quarter and half turns in place cover small repairs.
    A relocation puts one part where its pads sit nearest the pads they
    connect to, for crowding no local move relieves. ``legal`` says whether
    detached copies at new poses keep courtyards and the board edge legal.
    """

    # A half turn in place swaps which net each pad of a two-pad part offers
    # its neighbours.
    NUDGES = (
        (0.5, 1, 0, 0),
        (1.0, 0, 1, 0),
        (0.0, 0, 0, 90),
        (0.0, 0, 0, 180),
        (2.0, -1, 0, 0),
        (0.5, 0, -1, 0),
        (1.0, -1, 0, 0),
        (2.0, 0, 1, 0),
        (0.0, 0, 0, -90),
    )
    CLEARING_STEP_MM = 0.25
    CLEARING_REACH_MM = 6.0
    PITCH_MM = 1.0
    PITCHES_PER_SIDE = 60
    RELOCATIONS = 3
    RELOCATION_CHECKS = 200

    def __init__(self, board, guide, legal):
        self.board, self.guide, self.legal = board, guide, legal
        box = board.GetBoardEdgesBoundingBox()
        self.bounds = (
            box.GetX() / 1e6,
            box.GetY() / 1e6,
            box.GetRight() / 1e6,
            box.GetBottom() / 1e6,
        )
        # A coarser grid on a large board keeps the number of spots, and the time, bounded.
        self.pitch = max(
            self.PITCH_MM,
            max(box.GetWidth(), box.GetHeight()) / 1e6 / self.PITCHES_PER_SIDE,
        )

    def poses(self, selected):
        """Clearing moves first, then relocations and nudges taking turns.

        A caller that keeps only the first few poses still gets both kinds.
        """
        seen = set()
        turns = zip_longest(self.relocations(selected), self.nudges(selected))
        for pose in (
            *self.clearing(selected),
            *(pose for pair in turns for pose in pair if pose is not None),
        ):
            if pose.moves not in seen:
                seen.add(pose.moves)
                yield pose

    def inside(self, x: float, y: float) -> bool:
        x0, y0, x1, y1 = self.bounds
        return x0 < x < x1 and y0 < y < y1

    @staticmethod
    def copies(selected, moves):
        import pcbnew

        result = []
        for item, (_, x, y, angle) in zip(selected, moves):
            copy = pcbnew.FOOTPRINT(item[2])
            copy.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
            copy.SetOrientationDegrees(angle)
            result.append(copy)
        return result

    def shifted(
        self, selected, dx: float, dy: float, turn: float = 0.0, *, opposed: bool
    ):
        """Every selected part moved by one offset, or the second against the first when ``opposed``."""
        moves = []
        for index, (_, ref, footprint, _) in enumerate(selected):
            origin, sign = footprint.GetPosition(), -1 if index and opposed else 1
            x, y = (
                round(origin.x / 1e6 + sign * dx, 6),
                round(origin.y / 1e6 + sign * dy, 6),
            )
            if not self.inside(x, y):
                return None
            moves.append(
                (ref, x, y, (float(footprint.GetOrientationDegrees()) + turn) % 360)
            )
        return tuple(moves)

    def clearing(self, selected):
        """The shortest legal move in each of the four directions that leaves every corridor open."""
        if not self.guide.corridors:
            return []
        found, steps = [], int(self.CLEARING_REACH_MM / self.CLEARING_STEP_MM)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            for index in range(1, steps + 1):
                distance = index * self.CLEARING_STEP_MM
                moves = self.shifted(
                    selected, dx * distance, dy * distance, opposed=False
                )
                if moves is None:
                    break
                copies = self.copies(selected, moves)
                if self.guide.clears(copies) and self.legal(selected, copies):
                    found.append(Pose("clearing", moves, distance, 0.0))
                    break
        return sorted(found, key=lambda pose: pose.displacement_mm)

    def nudges(self, selected):
        for step, dx, dy, turn in self.NUDGES:
            moves = self.shifted(selected, dx * step, dy * step, turn, opposed=True)
            if moves is not None:
                yield Pose("nudge", moves, step, turn)

    def relocations(self, selected):
        """Open spots ranked by the distance from each pad to the nearest pad it connects to."""
        import pcbnew

        if len(selected) != 1:
            return
        _, ref, footprint, _ = selected[0]
        nets = {str(pad.GetNetname()) for pad in footprint.Pads()}
        targets = {}
        for other in self.board.GetFootprints():
            if other.m_Uuid.AsString() == footprint.m_Uuid.AsString():
                continue
            for pad in other.Pads():
                if str(pad.GetNetname()) in nets:
                    at = pad.GetPosition()
                    targets.setdefault(str(pad.GetNetname()), []).append(
                        (at.x / 1e6, at.y / 1e6)
                    )
        if not targets:
            return
        origin = footprint.GetPosition()
        x0, y0 = origin.x / 1e6, origin.y / 1e6
        base = float(footprint.GetOrientationDegrees())
        ranked = []
        for angle in (base, (base + 90) % 360):
            probe = pcbnew.FOOTPRINT(footprint)
            probe.SetOrientationDegrees(angle)
            at = probe.GetPosition()
            offsets = [
                (
                    (pad.GetPosition().x - at.x) / 1e6,
                    (pad.GetPosition().y - at.y) / 1e6,
                    str(pad.GetNetname()),
                )
                for pad in probe.Pads()
                if str(pad.GetNetname()) in targets
            ]
            bx0, by0, bx1, by1 = self.bounds
            for i in range(1, int((bx1 - bx0) / self.pitch)):
                for j in range(1, int((by1 - by0) / self.pitch)):
                    x, y = (
                        round(bx0 + i * self.pitch, 6),
                        round(by0 + j * self.pitch, 6),
                    )
                    if math.hypot(x - x0, y - y0) < self.pitch:
                        continue
                    cost = sum(
                        min(
                            math.hypot(x + ox - tx, y + oy - ty)
                            for tx, ty in targets[net]
                        )
                        for ox, oy, net in offsets
                    )
                    ranked.append((cost, x, y, angle))
        ranked.sort()
        chosen = []
        for cost, x, y, angle in ranked[: self.RELOCATION_CHECKS]:
            if any(math.hypot(x - cx, y - cy) < 2 * self.pitch for cx, cy in chosen):
                continue
            moves = ((ref, x, y, angle),)
            if self.legal(selected, self.copies(selected, moves)):
                chosen.append((x, y))
                yield Pose(
                    "relocation",
                    moves,
                    round(math.hypot(x - x0, y - y0), 6),
                    (angle - base) % 360,
                )
                if len(chosen) >= self.RELOCATIONS:
                    return
