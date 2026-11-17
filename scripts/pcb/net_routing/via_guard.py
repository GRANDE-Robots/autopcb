"""Detect unqualified via drills intersecting copper pads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pcbnew


@dataclass(frozen=True, order=True, slots=True)
class ViaPadOverlap:
    via_id: str
    pad_id: str
    position: tuple[int, int]
    drill: int
    reference: str
    pad_number: str
    net: str


def via_pad_overlaps(board) -> tuple[ViaPadOverlap, ...]:
    """Find via drill disks touching copper pads, including rotated pad shapes."""
    if board is None:
        raise ValueError("A native PCB is required to inspect via-in-pad")
    pads = []
    copper_layers = tuple(board.GetEnabledLayers().CuStack())
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                continue
            for layer in copper_layers:
                if pad.IsOnLayer(layer):
                    bounds = pad.GetBoundingBox(layer)
                    pads.append((pad, layer, bounds.GetX(), bounds.GetY(),
                                 bounds.GetRight(), bounds.GetBottom()))
    overlaps = set()
    for via in board.GetTracks():
        if not isinstance(via, pcbnew.PCB_VIA):
            continue
        point, drill = via.GetPosition(), int(via.GetDrillValue())
        if drill <= 0:
            raise ValueError("A via has no valid drill size")
        radius = (drill + 1) // 2
        for pad, layer, left, top, right, bottom in pads:
            if not via.IsOnLayer(layer) or not (left - radius <= point.x <= right + radius
                    and top - radius <= point.y <= bottom + radius):
                continue
            if pad.HitTest(point, radius, layer):
                overlaps.add(ViaPadOverlap(via.m_Uuid.AsString(), pad.m_Uuid.AsString(),
                                           (int(point.x), int(point.y)), drill,
                                           str(pad.GetParentFootprint().GetReference()),
                                           str(pad.GetNumber()), str(via.GetNetname())))
    return tuple(sorted(overlaps))


def verify_no_new_via_pad_overlaps(before, after) -> None:
    """Preserve legacy evidence, but reject newly introduced drill/pad overlap."""
    added = set(via_pad_overlaps(after)) - set(via_pad_overlaps(before))
    if added:
        labels = sorted({f"{item.reference}.{item.pad_number}" for item in added})
        raise RuntimeError("Candidate adds unqualified via-in-pad: " + ", ".join(labels))


def verify_files(before: Path, after: Path) -> None:
    """Use independent native boards when called from an isolated worker."""
    verify_no_new_via_pad_overlaps(pcbnew.LoadBoard(str(before)), pcbnew.LoadBoard(str(after)))
