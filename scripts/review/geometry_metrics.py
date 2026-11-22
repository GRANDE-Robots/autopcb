"""Measure routing geometry from one immutable KiCad board snapshot.

Length includes straight tracks and arcs, but excludes zones and via barrels.
The joint count follows the routing-style audit: endpoints with exactly two
straight segments on one net and layer, excluding same-net pads and vias.
Interior branches and arc junctions are not counted. These are progress
metrics, not electrical or manufacturing checks.
"""

from __future__ import annotations

import collections
import math
from dataclasses import dataclass

import pcbnew

from core.pcb_io import copper_width


@dataclass(frozen=True)
class GeometryMetrics:
    track_length_mm: float
    via_count: int
    degree_two_joints: int


def measure_board(board) -> GeometryMetrics:
    """Return the same geometry measures used by the routing-style audit."""
    tracks = list(board.GetTracks())
    vias = [track for track in tracks if isinstance(track, pcbnew.PCB_VIA)]
    copper = [track for track in tracks if not isinstance(track, pcbnew.PCB_VIA)]
    straight = [track for track in copper if not isinstance(track, pcbnew.PCB_ARC)]
    endpoints = collections.defaultdict(list)
    for track in straight:
        for point in (track.GetStart(), track.GetEnd()):
            endpoints[(track.GetNetCode(), track.GetLayer(), point.x, point.y)].append(
                track
            )

    pads_by_net = collections.defaultdict(list)
    for pad in board.GetPads():
        pads_by_net[pad.GetNetCode()].append(pad)
    vias_by_net = collections.defaultdict(list)
    for via in vias:
        vias_by_net[via.GetNetCode()].append(
            (via.GetPosition(), copper_width(via, layer=pcbnew.F_Cu) / 2)
        )

    joints = 0
    for (net, layer, x, y), incident in endpoints.items():
        if len(incident) != 2:
            continue
        at = pcbnew.VECTOR2I(x, y)
        if any(pad.IsOnLayer(layer) and pad.HitTest(at) for pad in pads_by_net[net]):
            continue
        if any(
            math.hypot(point.x - x, point.y - y) <= radius
            for point, radius in vias_by_net[net]
        ):
            continue
        if all(track.GetLength() > 0 for track in incident):
            joints += 1

    return GeometryMetrics(
        track_length_mm=round(sum(track.GetLength() for track in copper) / 1e6, 6),
        via_count=len(vias),
        degree_two_joints=joints,
    )
