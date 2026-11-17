#!/usr/bin/env python3
"""Read-only, two-sided courtyard bounding-box audit for a KiCad PCB.

KiCad's broad courtyard-overlap scan is valuable, but a dense two-sided board
can legitimately place an F.Cu connector over a B.Cu SMD part.  Treating all
such pairs as one failure list obscures the real same-face collisions.  This
audit reports the three physically different cases separately:

* same-side courtyard bounding-box overlap: a possible placement conflict
  unless that exact pair is explicitly declared intentional;
* opposite-side overlap: a packaging review item, not a DRC failure;
* opposite-side overlap involving a drilled hole: a physical/process review
  item, because board geometry alone does not establish Z clearance.

It never edits the board and writes no report file; JSON is emitted to stdout
for a review or a routing/placement batch plan.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pcbnew

# Optional Jessie module/socket pairs.  Reference names alone must not silently
# waive overlaps on another board, so this preset is explicitly requested.
JESSIE_SOCKET_PAIRS = frozenset(
    {frozenset(("U3", "J16")), frozenset(("U3", "J17")), frozenset(("U9", "J18"))}
)


def reference_pair(value: str) -> frozenset[str]:
    parts = value.split(":")
    if len(parts) != 2 or not all(part.strip() for part in parts):
        raise argparse.ArgumentTypeError("Expected two references as REF:REF")
    pair = frozenset(part.strip() for part in parts)
    if len(pair) != 2:
        raise argparse.ArgumentTypeError("A pair must contain two different references")
    return pair


@dataclass(frozen=True)
class FootprintRecord:
    reference: str
    value: str
    side: str
    through_hole: bool
    bbox: tuple[float, float, float, float]


def mm(value: int) -> float:
    return round(pcbnew.ToMM(value), 6)


def bbox_tuple(box: pcbnew.BOX2I) -> tuple[float, float, float, float]:
    return mm(box.GetLeft()), mm(box.GetTop()), mm(box.GetRight()), mm(box.GetBottom())


def union_box(boxes: list[pcbnew.BOX2I]) -> tuple[float, float, float, float]:
    return (
        min(mm(box.GetLeft()) for box in boxes),
        min(mm(box.GetTop()) for box in boxes),
        max(mm(box.GetRight()) for box in boxes),
        max(mm(box.GetBottom()) for box in boxes),
    )


def side_of(footprint: pcbnew.FOOTPRINT) -> str:
    return "B" if footprint.GetLayer() == pcbnew.B_Cu else "F"


def courtyard_or_body(footprint: pcbnew.FOOTPRINT) -> tuple[float, float, float, float]:
    courtyard_layer = pcbnew.B_CrtYd if side_of(footprint) == "B" else pcbnew.F_CrtYd
    courtyard = [
        item.GetBoundingBox()
        for item in footprint.GraphicalItems()
        if item.GetLayer() == courtyard_layer
    ]
    return union_box(courtyard) if courtyard else bbox_tuple(footprint.GetBoundingBox())


def has_drilled_hole(footprint: pcbnew.FOOTPRINT) -> bool:
    return any(
        pad.GetDrillSize().x > 0 or pad.GetDrillSize().y > 0 for pad in footprint.Pads()
    )


def intersection(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> dict[str, float] | None:
    x1, y1 = max(first[0], second[0]), max(first[1], second[1])
    x2, y2 = min(first[2], second[2]), min(first[3], second[3])
    if x2 <= x1 or y2 <= y1:
        return None
    return {
        "x1_mm": x1,
        "y1_mm": y1,
        "x2_mm": x2,
        "y2_mm": y2,
        "width_mm": round(x2 - x1, 6),
        "height_mm": round(y2 - y1, 6),
        "area_mm2": round((x2 - x1) * (y2 - y1), 6),
    }


def record(footprint: pcbnew.FOOTPRINT) -> FootprintRecord:
    return FootprintRecord(
        reference=footprint.GetReference(),
        value=footprint.GetValue(),
        side=side_of(footprint),
        through_hole=has_drilled_hole(footprint),
        bbox=courtyard_or_body(footprint),
    )


def pair_payload(
    first: FootprintRecord, second: FootprintRecord, overlap: dict[str, float]
) -> dict[str, object]:
    return {
        "a": first.reference,
        "b": second.reference,
        "a_side": first.side,
        "b_side": second.side,
        "a_through_hole": first.through_hole,
        "b_through_hole": second.through_hole,
        "overlap": overlap,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board", type=Path)
    parser.add_argument(
        "--allow-pair",
        action="append",
        type=reference_pair,
        default=[],
        metavar="REF:REF",
        help="report this exact same-side pair as intentional; repeatable",
    )
    parser.add_argument(
        "--jessie-sockets",
        action="store_true",
        help="add the three Jessie module/socket pairs to the explicit exceptions",
    )
    args = parser.parse_args()

    board_path = args.board.resolve()
    board_sha256 = hashlib.sha256(board_path.read_bytes()).hexdigest()
    board = pcbnew.LoadBoard(str(board_path))
    parts = sorted(
        (record(footprint) for footprint in board.GetFootprints()),
        key=lambda item: item.reference,
    )
    same_side: list[dict[str, object]] = []
    intentional: list[dict[str, object]] = []
    opposite_side: list[dict[str, object]] = []
    through_hole_cross_side: list[dict[str, object]] = []
    allowed = set(args.allow_pair)
    if args.jessie_sockets:
        allowed.update(JESSIE_SOCKET_PAIRS)
    available = {part.reference for part in parts}
    missing = sorted(set().union(*allowed) - available) if allowed else []
    if missing:
        parser.error(
            f"Exception references not present on this board: {', '.join(missing)}"
        )

    for index, first in enumerate(parts):
        for second in parts[index + 1 :]:
            overlap = intersection(first.bbox, second.bbox)
            if overlap is None:
                continue
            payload = pair_payload(first, second, overlap)
            if first.side == second.side:
                if frozenset((first.reference, second.reference)) in allowed:
                    intentional.append(payload)
                else:
                    same_side.append(payload)
            elif first.through_hole or second.through_hole:
                through_hole_cross_side.append(payload)
            else:
                opposite_side.append(payload)

    payload = {
        "source": str(board_path),
        "source_sha256": board_sha256,
        "method": (
            "Footprint courtyard bbox on its placed side; fall back to "
            "body bbox only when the footprint has no placed-side "
            "courtyard. Bounding-box overlap is a review candidate, "
            "not proof of a physical courtyard collision or fit."
        ),
        "intentional_colocation_set": sorted(set().union(*allowed)) if allowed else [],
        "intentional_colocation_pairs": sorted(sorted(pair) for pair in allowed),
        "summary": {
            "footprints": len(parts),
            "same_side_conflicts": len(same_side),
            "intentional_same_side_colocations": len(intentional),
            "opposite_side_reviews": len(opposite_side),
            "through_hole_cross_side_reviews": len(through_hole_cross_side),
        },
        "same_side_conflicts": same_side,
        "intentional_same_side_colocations": intentional,
        "opposite_side_reviews": opposite_side,
        "through_hole_cross_side_reviews": through_hole_cross_side,
    }
    if hashlib.sha256(board_path.read_bytes()).hexdigest() != board_sha256:
        raise RuntimeError(
            "Saved board changed during courtyard audit; discard the result"
        )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
