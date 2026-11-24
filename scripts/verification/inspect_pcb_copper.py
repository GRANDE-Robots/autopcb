#!/usr/bin/env python3
"""inspect_pcb_copper.py: read-only copper-object inspector for DRC-coordinate triage.

Given board-space points in millimetres, identify nearby pads, tracks and vias.
Pad and via hit tests include drill openings, and copper zones are not
inspected. It never writes the board; use it to identify DRC review candidates.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import pcbnew
from core.pcb_io import copper_width


class BoardUnits:
    """Convert pcbnew internal units (nm) to millimetres."""

    @staticmethod
    def mm(value: int) -> float:
        """Convert one pcbnew length in nm to mm, rounded to six decimals."""
        return round(pcbnew.ToMM(value), 6)

    @staticmethod
    def mm_point(point: pcbnew.VECTOR2I) -> tuple[float, float]:
        """Convert a pcbnew point in nm to an (x, y) pair in mm."""
        return BoardUnits.mm(point.x), BoardUnits.mm(point.y)


class PadContainment:
    """Hit test of one pad against a board point."""

    def __call__(self, pad: pcbnew.PAD, x: float, y: float) -> bool:
        """Say whether the pad covers the point.

        Inputs:
            pad: pcbnew pad.
            x: Point X in mm.
            y: Point Y in mm.
        Outputs:
            True when the pad hit test passes at zero tolerance, drill openings included.
        """
        return bool(pad.HitTest(pcbnew.VECTOR2I(round(x * 1e6), round(y * 1e6)), 0))


contains_pad = PadContainment()


class NearbyTracks:
    """Find vias, tracks and arcs near a board point."""

    def __call__(
        self,
        board: pcbnew.BOARD,
        x: float,
        y: float,
        tolerance: float,
        layer: int | None = None,
    ) -> list[dict[str, object]]:
        """List the copper items within a tolerance of a point.

        Inputs:
            board: Loaded pcbnew board.
            x: Point X in mm.
            y: Point Y in mm.
            tolerance: Search distance in mm.
            layer: pcbnew layer id that restricts the search; None searches every layer.
        Outputs:
            One dict per via, track or arc with its net, geometry in mm and UUID.
        """
        found: list[dict[str, object]] = []
        point = pcbnew.VECTOR2I(round(x * 1e6), round(y * 1e6))
        for item in board.GetTracks():
            if layer is not None and not item.IsOnLayer(layer):
                continue
            if isinstance(item, pcbnew.PCB_VIA):
                px, py = BoardUnits.mm_point(item.GetPosition())
                layers = [int(i) for i in item.GetLayerSet().CuStack()]
                widths = [
                    copper_width(item, layer=i)
                    for i in ([layer] if layer is not None else layers)
                ]
                diameter = BoardUnits.mm(max(widths))
                radius = diameter / 2 + tolerance
                if math.hypot(x - px, y - py) <= radius:
                    found.append(
                        {
                            "kind": "via",
                            "net": item.GetNetname(),
                            "at_mm": [px, py],
                            "diameter_mm": diameter,
                            "drill_mm": BoardUnits.mm(item.GetDrillValue()),
                            "layers": [board.GetLayerName(i) for i in layers],
                            "uuid": item.m_Uuid.AsString(),
                        }
                    )
                continue
            ax, ay = BoardUnits.mm_point(item.GetStart())
            bx, by = BoardUnits.mm_point(item.GetEnd())
            if item.HitTest(point, round(tolerance * 1e6)):
                payload = {
                    "kind": "arc" if isinstance(item, pcbnew.PCB_ARC) else "track",
                    "net": item.GetNetname(),
                    "layer": board.GetLayerName(item.GetLayer()),
                    "start_mm": [ax, ay],
                    "end_mm": [bx, by],
                    "width_mm": BoardUnits.mm(copper_width(item)),
                    "uuid": item.m_Uuid.AsString(),
                }
                if isinstance(item, pcbnew.PCB_ARC):
                    payload["mid_mm"] = list(BoardUnits.mm_point(item.GetMid()))
                found.append(payload)
        return found


nearby_tracks = NearbyTracks()


class PointInspection:
    """Pad and copper report for one board point."""

    def __call__(
        self,
        board: pcbnew.BOARD,
        point: str,
        tolerance: float,
        layer: str | None = None,
    ) -> dict[str, object]:
        """Report the pads and copper items at a point.

        Inputs:
            board: Loaded pcbnew board.
            point: ``X,Y`` text in mm.
            tolerance: Search distance in mm; it must be finite and not negative.
            layer: Enabled copper layer name that restricts the search, or None.
        Outputs:
            A dict with the point, the limitations text, the pads and the copper items.
            Raises ValueError for a non-finite value, a negative tolerance or a layer
            that is not an enabled copper layer.
        """
        x_text, y_text = point.split(",", 1)
        x, y = float(x_text), float(y_text)
        if (
            not all(math.isfinite(value) for value in (x, y, tolerance))
            or tolerance < 0
        ):
            raise ValueError(
                "Coordinates and tolerance must be finite; tolerance cannot be negative"
            )
        enabled = {
            board.GetLayerName(i): int(i) for i in board.GetEnabledLayers().CuStack()
        }
        if layer is not None and layer not in enabled:
            raise ValueError(f"Not an enabled copper layer: {layer}")
        layer_id = enabled.get(layer) if layer else None
        pads: list[dict[str, object]] = []
        for footprint in board.GetFootprints():
            for pad in footprint.Pads():
                if pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH or not list(
                    pad.GetLayerSet().CuStack()
                ):
                    continue
                if (layer_id is None or pad.IsOnLayer(layer_id)) and pad.HitTest(
                    pcbnew.VECTOR2I(round(x * 1e6), round(y * 1e6)),
                    round(tolerance * 1e6),
                ):
                    pads.append(
                        {
                            "reference": footprint.GetReference(),
                            "pad": pad.GetNumber(),
                            "net": pad.GetNetname(),
                            "at_mm": list(BoardUnits.mm_point(pad.GetPosition())),
                            "drill_mm": list(BoardUnits.mm_point(pad.GetDrillSize())),
                            "layers": [
                                board.GetLayerName(layer)
                                for layer in pad.GetLayerSet().CuStack()
                            ],
                        }
                    )
        return {
            "point_mm": [x, y],
            "limitations": "Pad and via hits include drilled openings; copper zones are not inspected.",
            "pads": pads,
            "copper": nearby_tracks(board, x, y, tolerance, layer_id),
        }


inspect = PointInspection()


class CopperInspectionCommand:
    """Command-line entry: inspect copper at one or more board points."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Run the inspection.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 after printing the JSON report. Raises RuntimeError when the
            saved board changes during the inspection.
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("board", type=Path)
        parser.add_argument(
            "--point",
            action="append",
            required=True,
            help="X,Y in board millimetres; repeatable",
        )
        parser.add_argument("--tolerance-mm", type=float, default=0.03)
        parser.add_argument(
            "--layer", help="restrict results to one enabled copper layer, such as B.Cu"
        )
        args = parser.parse_args(argv)
        before = hashlib.sha256(args.board.read_bytes()).hexdigest()
        board = pcbnew.LoadBoard(str(args.board))
        result = [
            inspect(board, point, args.tolerance_mm, args.layer) for point in args.point
        ]
        if hashlib.sha256(args.board.read_bytes()).hexdigest() != before:
            raise RuntimeError(
                "Saved board changed during inspection; discard the result"
            )
        print(json.dumps(result, indent=2))
        return 0


main = CopperInspectionCommand()


if __name__ == "__main__":
    raise SystemExit(main())
