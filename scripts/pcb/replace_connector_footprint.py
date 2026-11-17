#!/usr/bin/env python3
"""Safely replace one connector footprint without moving live copper.

This is intentionally narrower than KiCad's full *Update PCB from Schematic*
operation.  That operation can reconcile every net in a legacy board, which is
the wrong blast radius when a connector's physical geometry is the only thing
being changed.  This helper accepts an explicit old-pad -> new-pad mapping and
will write a board only when every previously live pad remains at the exact
same board coordinate.  Existing tracks therefore remain attached; any
connector change that needs a reroute is rejected for a deliberate routing
batch instead of silently dragging copper.

Use a temporary output first.  The source board is never overwritten unless
both --apply and --allow-in-place are supplied explicitly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pcbnew

from core.pcb_io import BoardSaveGuard


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def point_mm(point: pcbnew.VECTOR2I) -> list[float]:
    return [round(pcbnew.ToMM(point.x), 6), round(pcbnew.ToMM(point.y), 6)]


def parse_pad_map(value: str) -> tuple[str, str]:
    try:
        old_pad, new_pad = (part.strip() for part in value.split("=", 1))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid pad map {value!r}; expected OLD_PAD=NEW_PAD"
        ) from exc
    if not old_pad or not new_pad:
        raise argparse.ArgumentTypeError(
            f"invalid pad map {value!r}; expected OLD_PAD=NEW_PAD"
        )
    return old_pad, new_pad


def unconnected_count(board: pcbnew.BOARD) -> int:
    connectivity = board.GetConnectivity()
    connectivity.Build(board)
    return int(connectivity.GetUnconnectedCount(False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board", type=Path, help="source .kicad_pcb")
    parser.add_argument("--reference", required=True, help="footprint reference")
    parser.add_argument(
        "--footprint-library",
        type=Path,
        required=True,
        help="path to the .pretty footprint-library directory",
    )
    parser.add_argument(
        "--footprint-name",
        required=True,
        help="footprint filename stem in the supplied .pretty library",
    )
    parser.add_argument(
        "--pad-map",
        action="append",
        type=parse_pad_map,
        required=True,
        metavar="OLD_PAD=NEW_PAD",
        help="map every old live pad to the coincident new pad; repeat per pad",
    )
    parser.add_argument(
        "--duplicate-net",
        action="append",
        type=parse_pad_map,
        default=[],
        metavar="OLD_PAD=NEW_PAD",
        help=(
            "copy one mapped old pad's net to an additional new pad; use only "
            "for a deliberately parallel contact and verify it with DRC"
        ),
    )
    parser.add_argument(
        "--refill-zones",
        action="store_true",
        help="refill copper zones in the output board after the connector batch",
    )
    parser.add_argument("--apply", action="store_true", help="write the output board")
    parser.add_argument("--output", type=Path, help="destination .kicad_pcb")
    parser.add_argument(
        "--allow-in-place",
        action="store_true",
        help="required with --apply when --output is the source board",
    )
    parser.add_argument("--expect-sha256", help="refuse if the source hash differs")
    args = parser.parse_args()

    board_path = args.board.resolve()
    library_path = args.footprint_library.resolve()
    if not board_path.is_file():
        parser.error(f"board does not exist: {board_path}")
    if not library_path.is_dir():
        parser.error(f"footprint library does not exist: {library_path}")
    if args.apply != (args.output is not None):
        parser.error("--apply and --output must be supplied together")
    output_path = args.output.resolve() if args.output is not None else None
    if output_path == board_path and not args.allow_in_place:
        parser.error("overwriting the source requires --allow-in-place")
    if output_path == board_path and not args.expect_sha256:
        parser.error("in-place edits require --expect-sha256")
    if output_path is not None and output_path != board_path and output_path.exists():
        parser.error(f"refusing to overwrite another file: {output_path}")
    if output_path is not None and not output_path.parent.is_dir():
        parser.error(f"output directory does not exist: {output_path.parent}")

    source_sha = sha256(board_path)
    if args.expect_sha256 and source_sha.lower() != args.expect_sha256.lower():
        parser.error(
            "source SHA-256 changed; reload and re-plan the connector migration"
        )

    guard = None
    if args.apply:
        footprint_file = (library_path / f"{args.footprint_name}.kicad_mod").resolve()
        if (
            not footprint_file.is_relative_to(library_path)
            or not footprint_file.is_file()
        ):
            parser.error(
                "the selected footprint file must exist inside the supplied library"
            )
        guard = BoardSaveGuard.capture(board_path, source_sha, [footprint_file])

    pad_map = dict(args.pad_map)
    if len(pad_map) != len(args.pad_map):
        parser.error("each old pad may be mapped only once")
    if len(set(pad_map.values())) != len(pad_map):
        parser.error("each new pad may receive only one old pad")
    duplicate_map = list(args.duplicate_net)
    duplicate_targets = [new_pad for _, new_pad in duplicate_map]
    if len(set(duplicate_targets)) != len(duplicate_targets):
        parser.error("each duplicated new pad may receive only one copied net")
    if set(duplicate_targets) & set(pad_map.values()):
        parser.error("a duplicated new pad may not also be a primary mapped pad")

    board = pcbnew.LoadBoard(str(board_path))
    before_unconnected = unconnected_count(board)
    old = board.FindFootprintByReference(args.reference)
    if old is None:
        parser.error(f"footprint not found: {args.reference}")
    if old.IsLocked():
        parser.error(f"footprint is locked: {args.reference}")

    new = pcbnew.FootprintLoad(str(library_path), args.footprint_name)
    if new is None:
        parser.error(
            f"could not load footprint {args.footprint_name!r} from {library_path}"
        )

    new.SetPosition(old.GetPosition())
    new.SetOrientationDegrees(float(old.GetOrientationDegrees()))
    new.SetLayer(old.GetLayer())
    new.SetReference(old.GetReference())
    new.SetValue(old.GetValue())
    new.SetPath(old.GetPath())
    new.SetAttributes(old.GetAttributes())
    new.SetLocked(old.IsLocked())

    old_pads = {pad.GetNumber(): pad for pad in old.Pads()}
    new_pads = {pad.GetNumber(): pad for pad in new.Pads()}
    unknown_old = sorted(
        (set(pad_map) | {old for old, _ in duplicate_map}) - set(old_pads)
    )
    unknown_new = sorted(
        (set(pad_map.values()) | set(duplicate_targets)) - set(new_pads)
    )
    if unknown_old or unknown_new:
        parser.error(
            f"pad map references unknown pads: old={unknown_old or 'none'} "
            f"new={unknown_new or 'none'}"
        )

    live_unmapped = sorted(
        pad.GetNumber()
        for pad in old_pads.values()
        if pad.GetNetCode() and pad.GetNumber() not in pad_map
    )
    if live_unmapped:
        parser.error(
            "every old live pad must be explicitly mapped; unmapped="
            + ", ".join(live_unmapped)
        )

    duplicate_without_net = sorted(
        old_number
        for old_number, _ in duplicate_map
        if old_pads[old_number].GetNetCode() == 0
    )
    if duplicate_without_net:
        parser.error(
            "a duplicated contact must copy an existing live net; source pads="
            + ", ".join(duplicate_without_net)
        )

    position_mismatches: list[dict[str, object]] = []
    payload_map: list[dict[str, object]] = []
    for old_number, new_number in sorted(pad_map.items()):
        old_pad = old_pads[old_number]
        new_pad = new_pads[new_number]
        old_position = old_pad.GetPosition()
        new_position = new_pad.GetPosition()
        row = {
            "old_pad": old_number,
            "new_pad": new_number,
            "net": old_pad.GetNetname(),
            "old_position_mm": point_mm(old_position),
            "new_position_mm": point_mm(new_position),
        }
        payload_map.append(row)
        if old_position.x != new_position.x or old_position.y != new_position.y:
            position_mismatches.append(row)
    if position_mismatches:
        raise RuntimeError(
            "replacement changes live pad coordinates; plan a routing batch instead: "
            + json.dumps(position_mismatches, indent=2)
        )

    payload: dict[str, object] = {
        "source": str(board_path),
        "source_sha256": source_sha,
        "mode": "apply" if args.apply else "preview",
        "reference": args.reference,
        "old_footprint": old.GetFPIDAsString(),
        "new_footprint": new.GetFPIDAsString(),
        "pad_map": payload_map,
        "duplicated_nets": [
            {
                "source_old_pad": old_number,
                "new_pad": new_number,
                "net": old_pads[old_number].GetNetname(),
                "new_position_mm": point_mm(new_pads[new_number].GetPosition()),
            }
            for old_number, new_number in duplicate_map
        ],
        "unconnected_before": before_unconnected,
        "output": str(output_path) if output_path is not None else None,
    }
    if not args.apply:
        print(json.dumps(payload, indent=2))
        return 0

    # Add the replacement while the old pads still own their nets, then copy
    # each explicitly mapped net.  The new footprint begins with no nets, so
    # the intentionally unmapped pads remain NC by construction.
    board.Add(new)
    for old_number, new_number in pad_map.items():
        new_pads[new_number].SetNet(old_pads[old_number].GetNet())
    for old_number, new_number in duplicate_map:
        new_pads[new_number].SetNet(old_pads[old_number].GetNet())
    board.Remove(old)

    if args.refill_zones:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())

    after_unconnected = unconnected_count(board)
    payload["unconnected_after"] = after_unconnected
    if after_unconnected != before_unconnected:
        raise RuntimeError(
            f"connectivity changed from {before_unconnected} to "
            f"{after_unconnected}; refusing to save"
        )

    assert guard is not None and output_path is not None
    payload["output_sha256"] = guard.save(
        board, output_path, pcbnew.SaveBoard, pcbnew.LoadBoard
    )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
