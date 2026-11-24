"""Crowd a board's movable parts toward one point, as a packed first placement would."""

from __future__ import annotations

import math
import random
from pathlib import Path

from autopcb.placement import Compactor, Envelope
from core.kicad_campaign import CampaignPlanner
from pcb.footprint_pose import native_pose_change
from pcb.move_scope import MoveScope
from pcb.net_routing.relocation import _courtyard_conflicts, _courtyard_geometry
from pcb.routability import Routability

GAP = 10_000


class CrowdedVariant:
    """One seeded packing: a random point, and every movable part slid toward it until it meets another.

    Some parts first turn by a quarter or half turn where that overlaps
    nothing. Courtyards never overlap and stay inside the copper-to-edge
    rule, so the packing is legal; whether it still routes is what a study
    measures. Parts on opposite sides are kept apart as well.
    """

    TURN_CHANCE = 0.3

    def __init__(self, board: Path, seed: int, policy: dict | None = None):
        """``policy`` is the routing policy: its placement scope picks the parts to pack."""
        self.path, self.seed, self.policy = Path(board), seed, policy or {}
        self.scope = MoveScope.from_config(self.policy.get("placement"))

    @staticmethod
    def envelope(footprint) -> Envelope:
        boxes = [box for _, box in _courtyard_geometry(footprint).values()] or [
            footprint.GetBoundingBox(False)
        ]
        return Envelope(
            (min(b.GetX() for b in boxes), min(b.GetY() for b in boxes)),
            (max(b.GetRight() for b in boxes), max(b.GetBottom() for b in boxes)),
        )

    @staticmethod
    def overlaps(box: Envelope, others) -> bool:
        return any(
            box.low[0] < o.high[0]
            and o.low[0] < box.high[0]
            and box.low[1] < o.high[1]
            and o.low[1] < box.high[1]
            for o in others
        )

    @staticmethod
    def inside(box: Envelope, outline: Envelope, margin: int) -> bool:
        return all(
            outline.low[axis] + margin <= box.low[axis]
            and box.high[axis] <= outline.high[axis] - margin
            for axis in (0, 1)
        )

    def write(self) -> dict:
        import pcbnew

        board = pcbnew.LoadBoard(str(self.path))
        rng = random.Random(self.seed)
        movable = self.scope.movable(self.path.read_text(encoding="utf-8-sig"))
        footprints = {str(fp.GetReference()): fp for fp in board.GetFootprints()}
        parts = {ref: self.envelope(fp) for ref, fp in footprints.items()}
        angles = {
            ref: float(fp.GetOrientationDegrees()) for ref, fp in footprints.items()
        }
        edges = board.GetBoardEdgesBoundingBox()
        outline = Envelope(
            (edges.GetX(), edges.GetY()), (edges.GetRight(), edges.GetBottom())
        )
        margin = board.GetDesignSettings().m_CopperEdgeClearance
        for ref in sorted(movable & parts.keys()):
            if rng.random() >= self.TURN_CHANCE:
                continue
            turned = pcbnew.FOOTPRINT(footprints[ref])
            turned.SetOrientationDegrees(
                (angles[ref] + rng.choice((90, 180, 270))) % 360
            )
            box = self.envelope(turned)
            if self.inside(box, outline, margin) and not self.overlaps(
                box, (other for name, other in parts.items() if name != ref)
            ):
                parts[ref], angles[ref] = box, float(turned.GetOrientationDegrees())
        point = (
            rng.randint(outline.low[0], outline.high[0]),
            rng.randint(outline.low[1], outline.high[1]),
        )

        def distance(ref):
            box = parts[ref]
            return math.hypot(
                (box.low[0] + box.high[0]) / 2 - point[0],
                (box.low[1] + box.high[1]) / 2 - point[1],
            )

        packed = Compactor(parts, movable & parts.keys(), outline, margin, GAP).toward(
            point, sorted(parts, key=lambda ref: (distance(ref), ref))
        )
        moved, copies = [], {}
        for ref, box in sorted(packed.items()):
            footprint = footprints[ref]
            at = footprint.GetPosition()
            x, y = (
                at.x + box.low[0] - parts[ref].low[0],
                at.y + box.low[1] - parts[ref].low[1],
            )
            if (x, y) == (at.x, at.y) and angles[ref] == float(
                footprint.GetOrientationDegrees()
            ):
                continue
            copy = pcbnew.FOOTPRINT(footprint)
            copy.SetOrientationDegrees(angles[ref])
            copy.SetPosition(pcbnew.VECTOR2I(x, y))
            copies[footprint.m_Uuid.AsString()] = copy
            moved.append(
                {
                    "reference": ref,
                    "from_mm": [at.x / 1e6, at.y / 1e6],
                    "to_mm": [x / 1e6, y / 1e6],
                    "rotation_deg": angles[ref],
                }
            )
        geometry = {
            fp.m_Uuid.AsString(): _courtyard_geometry(fp) for fp in footprints.values()
        }
        before = _courtyard_conflicts(geometry, geometry)
        if (
            _courtyard_conflicts(
                {uid: _courtyard_geometry(copy) for uid, copy in copies.items()},
                geometry,
            )
            - before
        ):
            raise RuntimeError(f"Packing seed {self.seed} made courtyards overlap")
        changes = [
            native_pose_change(
                self.path,
                item["reference"],
                *item["to_mm"],
                item["rotation_deg"],
                self.scope,
            )
            for item in moved
        ]
        if changes:
            self.path.write_bytes(
                CampaignPlanner(self.path.parent).plan("pcb", changes).after
            )
        return {
            "seed": self.seed,
            "point_mm": [point[0] / 1e6, point[1] / 1e6],
            "moved": moved,
            "routability": Routability(
                pcbnew.LoadBoard(str(self.path)),
                policy=self.policy,
                critical_nets=self.policy.get("exclude_nets", ()),
            ).report(),
        }
