#!/usr/bin/env python3
"""Safely preview or apply a small KiCad footprint-drag batch.

The generic PCB footprint move operation leaves copper at the old pad
locations.  That is fine for a deliberate unrouted placement pass, but not for
a dense board that already has routed branches.  This helper uses KiCad's
native pcbnew API to perform the bounded analogue of a UI drag:

* move/rotate only the explicitly named footprints;
* translate every directly connected track endpoint carried by a moved pad,
  including a deliberate endpoint at the pad edge rather than its centre;
* refuse a batch with a via directly anchored at a moved pad, because blindly
  translating a via can silently create a clearance or topology problem;
* rebuild connectivity and require the global unconnected-edge count to stay
  unchanged;
* optionally refill copper zones after the complete drag batch, because a
  moved pad or routed endpoint otherwise leaves stale zone clearances.

It is intentionally conservative.  It never autoroutes, and only refills
zones when explicitly requested for a write.  It defaults to a no-write
preview.  In particular, it is *not* KiCad's
interactive shove router: translating an endpoint can still leave a route
with bad geometry or clearance.  Use a temporary output first, inspect the
batch, then run the normal project-context KiCad DRC once after the complete
placement/routing batch.  Source boards are only overwritten with the
explicit ``--allow-in-place`` acknowledgement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pcbnew

from core.pcb_io import BoardSaveGuard, copper_width
from core.compat import strict_zip


@dataclass(frozen=True)
class Move:
    reference: str
    x_mm: float
    y_mm: float
    rotation_deg: float | None


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def point_key(point: pcbnew.VECTOR2I) -> tuple[int, int]:
    return (point.x, point.y)


def point_mm(point: pcbnew.VECTOR2I) -> list[float]:
    return [round(pcbnew.ToMM(point.x), 6), round(pcbnew.ToMM(point.y), 6)]


def parse_move(value: str) -> Move:
    """Parse REF=X,Y[,ROT] in millimetres/degrees."""
    try:
        reference, coordinates = value.split("=", 1)
        fields = [field.strip() for field in coordinates.split(",")]
        if len(fields) not in (2, 3) or not reference.strip():
            raise ValueError
        return Move(
            reference=reference.strip(),
            x_mm=float(fields[0]),
            y_mm=float(fields[1]),
            rotation_deg=float(fields[2]) if len(fields) == 3 else None,
        )
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid move {value!r}; expected REF=X_mm,Y_mm[,rotation_deg]"
        ) from exc


def unconnected_count(board: pcbnew.BOARD) -> int:
    connectivity = board.GetConnectivity()
    connectivity.Build(board)
    return int(connectivity.GetUnconnectedCount(False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board", type=Path, help="source .kicad_pcb")
    parser.add_argument(
        "--move",
        action="append",
        type=parse_move,
        required=True,
        metavar="REF=X,Y[,ROT]",
        help="new footprint position in mm and optional rotation in degrees; repeat per footprint",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the changed board; otherwise print a no-write preview",
    )
    parser.add_argument(
        "--refill-zones",
        action="store_true",
        help="refill copper zones after the complete drag batch before saving",
    )
    parser.add_argument(
        "--allow-zone-connectivity-change",
        action="store_true",
        help=(
            "allow a temporary output when refilling zones changes native "
            "connectivity; diagnostic-only and never permitted in place"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="destination .kicad_pcb required with --apply",
    )
    parser.add_argument(
        "--allow-in-place",
        action="store_true",
        help="required in addition to --apply when --output is the source board",
    )
    parser.add_argument(
        "--expect-sha256",
        help="refuse if the source board no longer has this SHA-256",
    )
    args = parser.parse_args()

    board_path = args.board.resolve()
    if not board_path.is_file():
        parser.error(f"board does not exist: {board_path}")
    if args.apply and args.output is None:
        parser.error("--apply requires --output")
    if not args.apply and args.output is not None:
        parser.error("--output is only valid with --apply")
    if args.refill_zones and not args.apply:
        parser.error("--refill-zones requires --apply")
    output_path = args.output.resolve() if args.output is not None else None
    if output_path == board_path and not args.allow_in_place:
        parser.error("overwriting the source requires --allow-in-place")
    if output_path == board_path and not args.expect_sha256:
        parser.error("in-place edits require --expect-sha256")
    if output_path is not None and output_path != board_path and output_path.exists():
        parser.error(f"refusing to overwrite another file: {output_path}")
    if args.allow_zone_connectivity_change:
        if not (args.apply and args.refill_zones):
            parser.error(
                "--allow-zone-connectivity-change requires --apply --refill-zones"
            )
        if output_path == board_path:
            parser.error(
                "--allow-zone-connectivity-change is diagnostic-only and cannot write in place"
            )
    if output_path is not None and not output_path.parent.is_dir():
        parser.error(f"output directory does not exist: {output_path.parent}")

    source_sha = sha256(board_path)
    if args.expect_sha256 and source_sha.lower() != args.expect_sha256.lower():
        parser.error("source SHA-256 changed; reload and re-plan the placement batch")
    if len({move.reference for move in args.move}) != len(args.move):
        parser.error("each reference may appear only once")

    guard = BoardSaveGuard.capture(board_path, source_sha) if args.apply else None
    board = pcbnew.LoadBoard(str(board_path))
    before_unconnected = unconnected_count(board)
    connectivity = board.GetConnectivity()
    connectivity.Build(board)
    pad_targets: dict[tuple[int, int], pcbnew.VECTOR2I] = {}
    move_payload: list[dict[str, object]] = []
    planned_moves: list[dict[str, object]] = []
    anchored_vias: list[dict[str, object]] = []

    # Collect direct pad-to-track attachments before moving anything.  A dense
    # board often terminates a 0.2 mm track at a pad edge, rather than at its
    # centre.  ``GetConnectedTracks`` limits this to the immediate electrical
    # attachment; the expanded hit test accounts for half the track width.
    for move in args.move:
        footprint = board.FindFootprintByReference(move.reference)
        if footprint is None:
            parser.error(f"footprint not found: {move.reference}")
        if footprint.IsLocked():
            parser.error(f"footprint is locked: {move.reference}")

        old_position = footprint.GetPosition()
        old_rotation = float(footprint.GetOrientationDegrees())
        old_pads = list(footprint.Pads())
        old_pad_positions = [pad.GetPosition() for pad in old_pads]
        old_pad_nets = [pad.GetNetCode() for pad in old_pads]
        endpoint_attachments: list[tuple[tuple[int, int], int, int, int]] = []

        for pad_index, (pad, pad_position) in enumerate(
            strict_zip(old_pads, old_pad_positions)
        ):
            for item in connectivity.GetConnectedTracks(pad):
                if isinstance(item, pcbnew.PCB_VIA):
                    via_position = item.GetPosition()
                    if pad.HitTest(via_position, pcbnew.FromMM(0.01)):
                        anchored_vias.append(
                            {
                                "reference": move.reference,
                                "at_mm": point_mm(via_position),
                                "net": item.GetNetname(),
                            }
                        )
                    continue
                margin = pcbnew.FromMM(pcbnew.ToMM(copper_width(item)) / 2 + 0.01)
                for endpoint in (item.GetStart(), item.GetEnd()):
                    if pad.HitTest(endpoint, margin):
                        endpoint_attachments.append(
                            (
                                point_key(endpoint),
                                pad_index,
                                endpoint.x - pad_position.x,
                                endpoint.y - pad_position.y,
                            )
                        )

        planned_moves.append(
            {
                "move": move,
                "footprint": footprint,
                "old_position": old_position,
                "old_rotation": old_rotation,
                "old_pads": old_pads,
                "old_pad_positions": old_pad_positions,
                "old_pad_nets": old_pad_nets,
                "endpoint_attachments": endpoint_attachments,
            }
        )

    if anchored_vias:
        raise RuntimeError(
            (
                "refusing to move pads with directly anchored vias; plan "
                "those vias/routing explicitly: "
            )
            + json.dumps(anchored_vias)
        )

    def register_target(
        old_key: tuple[int, int], target: pcbnew.VECTOR2I, context: str
    ) -> None:
        existing = pad_targets.get(old_key)
        if existing is not None and point_key(existing) != point_key(target):
            raise RuntimeError(
                f"multiple moved pads claim endpoint {old_key} with different targets ({context})"
            )
        pad_targets[old_key] = target

    for plan in planned_moves:
        move = plan["move"]
        footprint = plan["footprint"]
        old_position = plan["old_position"]
        old_rotation = float(plan["old_rotation"])
        old_pads = plan["old_pads"]
        old_pad_positions = plan["old_pad_positions"]
        old_pad_nets = plan["old_pad_nets"]
        endpoint_attachments = plan["endpoint_attachments"]

        footprint.SetPosition(
            pcbnew.VECTOR2I(pcbnew.FromMM(move.x_mm), pcbnew.FromMM(move.y_mm))
        )
        if move.rotation_deg is not None:
            footprint.SetOrientationDegrees(move.rotation_deg)

        new_pads = list(footprint.Pads())
        new_rotation = float(footprint.GetOrientationDegrees())
        if len(old_pads) != len(new_pads):
            raise RuntimeError(f"pad count changed while moving {move.reference}")
        for _old_pad, new_pad, old_position_key, old_net in strict_zip(
            old_pads, new_pads, old_pad_positions, old_pad_nets
        ):
            old_key = point_key(old_position_key)
            if old_net != new_pad.GetNetCode():
                raise RuntimeError(f"pad net changed while moving {move.reference}")
            register_target(
                old_key, new_pad.GetPosition(), f"{move.reference} pad centre"
            )

        for old_key, pad_index, dx, dy in endpoint_attachments:
            if (dx or dy) and abs(new_rotation - old_rotation) > 1e-6:
                raise RuntimeError(
                    "refusing to rotate a footprint with off-centre track endpoints; "
                    f"plan that routing explicitly: {move.reference}"
                )
            new_pad_position = new_pads[pad_index].GetPosition()
            register_target(
                old_key,
                pcbnew.VECTOR2I(new_pad_position.x + dx, new_pad_position.y + dy),
                f"{move.reference} pad-edge track",
            )

        move_payload.append(
            {
                "reference": move.reference,
                "from_mm": point_mm(old_position),
                "to_mm": [move.x_mm, move.y_mm],
                "from_rotation_deg": round(old_rotation, 6),
                "to_rotation_deg": round(new_rotation, 6),
                "pads": len(old_pads),
            }
        )

    endpoint_updates: list[dict[str, object]] = []
    for item in board.GetTracks():
        if isinstance(item, pcbnew.PCB_VIA):
            continue
        changed = []
        start = item.GetStart()
        end = item.GetEnd()
        if point_key(start) in pad_targets:
            item.SetStart(pad_targets[point_key(start)])
            changed.append("start")
        if point_key(end) in pad_targets:
            item.SetEnd(pad_targets[point_key(end)])
            changed.append("end")
        if changed:
            endpoint_updates.append(
                {
                    "uuid": item.m_Uuid.AsString(),
                    "net": item.GetNetname(),
                    "ends": changed,
                }
            )

    after_drag_unconnected = unconnected_count(board)
    if after_drag_unconnected != before_unconnected:
        raise RuntimeError(
            f"connectivity changed from {before_unconnected} to "
            f"{after_drag_unconnected}; refusing to save"
        )

    if args.refill_zones:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    after_zone_unconnected = unconnected_count(board)
    if (
        after_zone_unconnected != before_unconnected
        and not args.allow_zone_connectivity_change
    ):
        raise RuntimeError(
            "zone refill changed native connectivity from "
            f"{before_unconnected} to {after_zone_unconnected}; refusing to save"
        )

    payload = {
        "source": str(board_path),
        "source_sha256": source_sha,
        "mode": "apply" if args.apply else "preview",
        "moves": move_payload,
        "moved_pad_count": sum(len(plan["old_pads"]) for plan in planned_moves),
        "tracked_endpoint_count": len(pad_targets),
        "track_endpoint_updates": len(endpoint_updates),
        "endpoint_updates": endpoint_updates,
        "unconnected_before": before_unconnected,
        "unconnected_after_drag": after_drag_unconnected,
        "unconnected_after_zone_refill": after_zone_unconnected,
        "zones_refilled": bool(args.refill_zones),
        "zone_connectivity_change_allowed": bool(args.allow_zone_connectivity_change),
        "output": str(output_path) if output_path is not None else None,
    }
    if args.apply:
        assert guard is not None and output_path is not None
        payload["output_sha256"] = guard.save(
            board, output_path, pcbnew.SaveBoard, pcbnew.LoadBoard
        )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
