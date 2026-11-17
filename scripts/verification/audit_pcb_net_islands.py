#!/usr/bin/env python3
"""Report native KiCad pad islands for nets that are still electrically split.

This is deliberately read-only.  KiCad's aggregate ratsnest count cannot tell
which routed subsystems remain open, while this audit groups every connected
pad island on a multi-pad net.  It excludes generated one-pad ``unconnected-*``
nets because those represent intentional NC pads rather than missing copper.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import pcbnew


def pad_id(pad: pcbnew.PAD) -> tuple[str, str]:
    return (pad.GetParentFootprint().GetReference(), pad.GetNumber())


def pad_payload(pad: pcbnew.PAD) -> dict[str, object]:
    position = pad.GetPosition()
    reference, number = pad_id(pad)
    return {
        "reference": reference,
        "pad": number,
        "x_mm": round(pcbnew.ToMM(position.x), 6),
        "y_mm": round(pcbnew.ToMM(position.y), 6),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board", type=Path, help="native .kicad_pcb source")
    parser.add_argument("--include-single-pad", action="store_true")
    args = parser.parse_args()

    board_path = args.board.resolve()
    source_sha256 = hashlib.sha256(board_path.read_bytes()).hexdigest()
    board = pcbnew.LoadBoard(str(board_path))
    connectivity = board.GetConnectivity()
    connectivity.Build(board)

    pads_by_net: dict[int, list[pcbnew.PAD]] = defaultdict(list)
    for pad in board.GetPads():
        net_code = pad.GetNetCode()
        net_name = pad.GetNetname()
        if not net_code or net_name.startswith("unconnected-"):
            continue
        pads_by_net[net_code].append(pad)

    split_nets: list[dict[str, object]] = []
    single_pad_nets: list[dict[str, object]] = []
    for net_code, pads in sorted(
        pads_by_net.items(), key=lambda item: item[1][0].GetNetname()
    ):
        if len(pads) == 1:
            if args.include_single_pad:
                single_pad_nets.append(
                    {
                        "net": pads[0].GetNetname(),
                        "net_code": net_code,
                        "pad": pad_payload(pads[0]),
                    }
                )
            continue

        islands: dict[tuple[tuple[str, str], ...], list[pcbnew.PAD]] = {}
        for pad in pads:
            connected = connectivity.GetConnectedItems(pad)
            island_pads = sorted(
                (pad_id(item) for item in connected if isinstance(item, pcbnew.PAD)),
                key=lambda item: (item[0], item[1]),
            )
            key = tuple(island_pads or [pad_id(pad)])
            islands.setdefault(key, []).append(pad)

        if len(islands) <= 1:
            continue

        split_nets.append(
            {
                "net": pads[0].GetNetname(),
                "net_code": net_code,
                "pad_count": len(pads),
                "island_count": len(islands),
                "islands": [
                    {
                        "pads": [pad_payload(pad) for pad in sorted(group, key=pad_id)],
                        "all_connected_pads": [
                            {"reference": reference, "pad": number}
                            for reference, number in key
                        ],
                    }
                    for key, group in sorted(islands.items())
                ],
            }
        )

    payload = {
        "source": str(board_path),
        "source_sha256": source_sha256,
        "native_unconnected_edge_count": int(connectivity.GetUnconnectedCount(False)),
        "split_net_count": len(split_nets),
        "split_nets": split_nets,
    }
    if args.include_single_pad:
        payload["single_pad_nets"] = single_pad_nets
    if hashlib.sha256(board_path.read_bytes()).hexdigest() != source_sha256:
        raise RuntimeError(
            "Saved board changed during island audit; discard the result"
        )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
