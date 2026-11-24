#!/usr/bin/env python3
"""audit_pcb_courtyards.py: read-only two-sided courtyard bounding-box audit for a PCB.

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

Copyright: GRANDE Robots and Aaron John Sabu
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


class ReferencePairType:
    """Argparse type that reads a ``REF:REF`` text into a pair of references."""

    def __call__(self, value: str) -> frozenset[str]:
        """Parse one pair of footprint references.

        Inputs:
            value: Two references separated by a colon.
        Outputs:
            The two references as a frozenset. Raises argparse.ArgumentTypeError when
            the text does not hold two nonempty, different references.
        """
        parts = value.split(":")
        if len(parts) != 2 or not all(part.strip() for part in parts):
            raise argparse.ArgumentTypeError("Expected two references as REF:REF")
        pair = frozenset(part.strip() for part in parts)
        if len(pair) != 2:
            raise argparse.ArgumentTypeError(
                "A pair must contain two different references"
            )
        return pair


reference_pair = ReferencePairType()


@dataclass(frozen=True)
class FootprintRecord:
    """Reference, side, drill flag and courtyard box (mm) of one footprint."""

    reference: str
    value: str
    side: str
    through_hole: bool
    bbox: tuple[float, float, float, float]


class FootprintGeometry:
    """Read the side, drill flag and courtyard box of a pcbnew footprint."""

    @staticmethod
    def mm(value: int) -> float:
        """Convert one pcbnew length in nm to mm, rounded to six decimals."""
        return round(pcbnew.ToMM(value), 6)

    @classmethod
    def bbox_tuple(cls, box: pcbnew.BOX2I) -> tuple[float, float, float, float]:
        """Convert a pcbnew box to (left, top, right, bottom) in mm."""
        return (
            cls.mm(box.GetLeft()),
            cls.mm(box.GetTop()),
            cls.mm(box.GetRight()),
            cls.mm(box.GetBottom()),
        )

    @classmethod
    def union_box(cls, boxes: list[pcbnew.BOX2I]) -> tuple[float, float, float, float]:
        """Return the (left, top, right, bottom) box in mm that covers every box given."""
        return (
            min(cls.mm(box.GetLeft()) for box in boxes),
            min(cls.mm(box.GetTop()) for box in boxes),
            max(cls.mm(box.GetRight()) for box in boxes),
            max(cls.mm(box.GetBottom()) for box in boxes),
        )

    @staticmethod
    def side_of(footprint: pcbnew.FOOTPRINT) -> str:
        """Return ``B`` for a footprint on the bottom copper layer, otherwise ``F``."""
        return "B" if footprint.GetLayer() == pcbnew.B_Cu else "F"

    @classmethod
    def courtyard_or_body(
        cls, footprint: pcbnew.FOOTPRINT
    ) -> tuple[float, float, float, float]:
        """Return the courtyard box in mm on the placed side.

        Outputs:
            The union of the courtyard graphics on the placed side, or the footprint
            bounding box when it has none.
        """
        courtyard_layer = (
            pcbnew.B_CrtYd if cls.side_of(footprint) == "B" else pcbnew.F_CrtYd
        )
        courtyard = [
            item.GetBoundingBox()
            for item in footprint.GraphicalItems()
            if item.GetLayer() == courtyard_layer
        ]
        return (
            cls.union_box(courtyard)
            if courtyard
            else cls.bbox_tuple(footprint.GetBoundingBox())
        )

    @staticmethod
    def has_drilled_hole(footprint: pcbnew.FOOTPRINT) -> bool:
        """Say whether any pad of the footprint has a drill."""
        return any(
            pad.GetDrillSize().x > 0 or pad.GetDrillSize().y > 0
            for pad in footprint.Pads()
        )

    @classmethod
    def record(cls, footprint: pcbnew.FOOTPRINT) -> FootprintRecord:
        """Read one footprint into a FootprintRecord."""
        return FootprintRecord(
            reference=footprint.GetReference(),
            value=footprint.GetValue(),
            side=cls.side_of(footprint),
            through_hole=cls.has_drilled_hole(footprint),
            bbox=cls.courtyard_or_body(footprint),
        )


class PairOverlap:
    """Overlap geometry and report rows for pairs of footprints."""

    @staticmethod
    def intersection(
        first: tuple[float, float, float, float],
        second: tuple[float, float, float, float],
    ) -> dict[str, float] | None:
        """Intersect two boxes.

        Inputs:
            first: (left, top, right, bottom) in mm.
            second: (left, top, right, bottom) in mm.
        Outputs:
            The overlap corners, width and height in mm and area in mm2, or None when
            the boxes only touch or do not meet.
        """
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

    @staticmethod
    def pair_payload(
        first: FootprintRecord, second: FootprintRecord, overlap: dict[str, float]
    ) -> dict[str, object]:
        """Describe one overlapping pair by reference, side, drill flag and overlap."""
        return {
            "a": first.reference,
            "b": second.reference,
            "a_side": first.side,
            "b_side": second.side,
            "a_through_hole": first.through_hole,
            "b_through_hole": second.through_hole,
            "overlap": overlap,
        }


class CourtyardAudit:
    """Sort the overlapping footprint pairs of a board into review lists."""

    def __call__(
        self,
        board_path: Path,
        board_sha256: str,
        parts: list[FootprintRecord],
        allowed: set[frozenset[str]],
    ) -> dict[str, object]:
        """Build the overlap report.

        Inputs:
            board_path: Saved board the footprints were read from.
            board_sha256: SHA-256 of that board file.
            parts: Footprint records sorted by reference.
            allowed: Same-side pairs declared intentional.
        Outputs:
            The report with the four review lists and their counts.
        """
        same_side: list[dict[str, object]] = []
        intentional: list[dict[str, object]] = []
        opposite_side: list[dict[str, object]] = []
        through_hole_cross_side: list[dict[str, object]] = []
        for index, first in enumerate(parts):
            for second in parts[index + 1 :]:
                overlap = PairOverlap.intersection(first.bbox, second.bbox)
                if overlap is None:
                    continue
                payload = PairOverlap.pair_payload(first, second, overlap)
                if first.side == second.side:
                    if frozenset((first.reference, second.reference)) in allowed:
                        intentional.append(payload)
                    else:
                        same_side.append(payload)
                elif first.through_hole or second.through_hole:
                    through_hole_cross_side.append(payload)
                else:
                    opposite_side.append(payload)

        return {
            "source": str(board_path),
            "source_sha256": board_sha256,
            "method": (
                "Footprint courtyard bbox on its placed side; fall back to "
                "body bbox only when the footprint has no placed-side "
                "courtyard. Bounding-box overlap is a review candidate, "
                "not proof of a physical courtyard collision or fit."
            ),
            "intentional_colocation_set": sorted(set().union(*allowed))
            if allowed
            else [],
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


class CourtyardCommand:
    """Command-line entry: print the courtyard overlap report of a saved board."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Run the audit.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 after printing the JSON report. Exits through the
            argument parser when an exception names a reference the board lacks.
            Raises RuntimeError when the board file changes during the audit.
        """
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
        args = parser.parse_args(argv)

        board_path = args.board.resolve()
        board_sha256 = hashlib.sha256(board_path.read_bytes()).hexdigest()
        board = pcbnew.LoadBoard(str(board_path))
        parts = sorted(
            (
                FootprintGeometry.record(footprint)
                for footprint in board.GetFootprints()
            ),
            key=lambda item: item.reference,
        )
        allowed = set(args.allow_pair)
        if args.jessie_sockets:
            allowed.update(JESSIE_SOCKET_PAIRS)
        available = {part.reference for part in parts}
        missing = sorted(set().union(*allowed) - available) if allowed else []
        if missing:
            parser.error(
                f"Exception references not present on this board: {', '.join(missing)}"
            )

        payload = CourtyardAudit()(board_path, board_sha256, parts, allowed)
        if hashlib.sha256(board_path.read_bytes()).hexdigest() != board_sha256:
            raise RuntimeError(
                "Saved board changed during courtyard audit; discard the result"
            )
        print(json.dumps(payload, indent=2))
        return 0


main = CourtyardCommand()

if __name__ == "__main__":
    raise SystemExit(main())
