#!/usr/bin/env python3
"""audit_pcb_net_islands.py: report native KiCad pad islands of split nets.

This is deliberately read-only.  KiCad's aggregate ratsnest count cannot tell
which routed subsystems remain open, while this audit groups every connected
pad island on a multi-pad net.  It excludes generated one-pad ``unconnected-*``
nets because those represent intentional NC pads rather than missing copper.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import pcbnew


class PadReport:
    """Identify a pad and describe it for the JSON report."""

    @staticmethod
    def pad_id(pad: pcbnew.PAD) -> tuple[str, str]:
        """Return the (footprint reference, pad number) pair that names a pad."""
        return (pad.GetParentFootprint().GetReference(), pad.GetNumber())

    @staticmethod
    def pad_payload(pad: pcbnew.PAD) -> dict[str, object]:
        """Describe a pad by its reference, number and position in mm."""
        position = pad.GetPosition()
        reference, number = PadReport.pad_id(pad)
        return {
            "reference": reference,
            "pad": number,
            "x_mm": round(pcbnew.ToMM(position.x), 6),
            "y_mm": round(pcbnew.ToMM(position.y), 6),
        }


class NetIslandAudit:
    """Group the connected pad islands of every multi-pad net on a saved board."""

    @staticmethod
    def pads_by_net(board: pcbnew.BOARD) -> dict[int, list[pcbnew.PAD]]:
        """Group the pads of each real net by net code.

        Outputs:
            Pads keyed by net code; pads without a net and pads on generated
            ``unconnected-*`` nets are left out.
        """
        pads_by_net: dict[int, list[pcbnew.PAD]] = defaultdict(list)
        for pad in board.GetPads():
            net_code = pad.GetNetCode()
            net_name = pad.GetNetname()
            if not net_code or net_name.startswith("unconnected-"):
                continue
            pads_by_net[net_code].append(pad)
        return pads_by_net

    @staticmethod
    def islands(
        connectivity: pcbnew.CONNECTIVITY_DATA, pads: list[pcbnew.PAD]
    ) -> dict[tuple[tuple[str, str], ...], list[pcbnew.PAD]]:
        """Group the pads of one net by the pads that the copper connects them to.

        Outputs:
            The pads of each island keyed by the sorted ids of every pad it reaches.
        """
        islands: dict[tuple[tuple[str, str], ...], list[pcbnew.PAD]] = {}
        for pad in pads:
            connected = connectivity.GetConnectedItems(pad)
            island_pads = sorted(
                (
                    PadReport.pad_id(item)
                    for item in connected
                    if isinstance(item, pcbnew.PAD)
                ),
                key=lambda item: (item[0], item[1]),
            )
            key = tuple(island_pads or [PadReport.pad_id(pad)])
            islands.setdefault(key, []).append(pad)
        return islands

    def __call__(self, board_path: Path, include_single_pad: bool) -> dict[str, object]:
        """Audit one saved board.

        Inputs:
            board_path: Saved ``.kicad_pcb`` file.
            include_single_pad: Also list the nets that have only one pad.
        Outputs:
            The report with the board hash, the native unconnected-edge count and
            every net whose pads form more than one island. Raises RuntimeError
            when the board file changes during the audit.
        """
        source_sha256 = hashlib.sha256(board_path.read_bytes()).hexdigest()
        board = pcbnew.LoadBoard(str(board_path))
        connectivity = board.GetConnectivity()
        connectivity.Build(board)

        pads_by_net = self.pads_by_net(board)

        split_nets: list[dict[str, object]] = []
        single_pad_nets: list[dict[str, object]] = []
        for net_code, pads in sorted(
            pads_by_net.items(), key=lambda item: item[1][0].GetNetname()
        ):
            if len(pads) == 1:
                if include_single_pad:
                    single_pad_nets.append(
                        {
                            "net": pads[0].GetNetname(),
                            "net_code": net_code,
                            "pad": PadReport.pad_payload(pads[0]),
                        }
                    )
                continue

            islands = self.islands(connectivity, pads)

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
                            "pads": [
                                PadReport.pad_payload(pad)
                                for pad in sorted(group, key=PadReport.pad_id)
                            ],
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
            "native_unconnected_edge_count": int(
                connectivity.GetUnconnectedCount(False)
            ),
            "split_net_count": len(split_nets),
            "split_nets": split_nets,
        }
        if include_single_pad:
            payload["single_pad_nets"] = single_pad_nets
        if hashlib.sha256(board_path.read_bytes()).hexdigest() != source_sha256:
            raise RuntimeError(
                "Saved board changed during island audit; discard the result"
            )
        return payload


class NetIslandCommand:
    """Command-line entry: print the split-net report of a saved board."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Run the audit.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 after printing the JSON report.
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("board", type=Path, help="native .kicad_pcb source")
        parser.add_argument("--include-single-pad", action="store_true")
        args = parser.parse_args(argv)

        payload = NetIslandAudit()(args.board.resolve(), args.include_single_pad)
        print(json.dumps(payload, indent=2))
        return 0


main = NetIslandCommand()

if __name__ == "__main__":
    raise SystemExit(main())
