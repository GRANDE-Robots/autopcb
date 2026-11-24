"""pair_seed.py: seed a short, unbranched differential pair while creating a catalog design.

The seed runs on the new board inside KiCad's Python and keeps its traces only
after the native checks accept them. Lengths are KiCad internal units (nm) unless
a name says mm.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path

import pcbnew as pcb

from autopcb.native import Native
from core.project import ProjectPaths, ProjectProfile
from pcb.freeroute_delta import native_item_nets, unconnected_by_net
from pcb.net_routing.protection import ProtectedState
from pcb.net_routing.reference import adjacent_reference_layers, sample_reference
from pcb.net_routing.style import audit_style
from pcb.route_native import read_native_obstacles
from pcb.route_optimizer import (
    GridRouter,
    Point,
    Segment,
    _point_near_segment,
    _segments_intersect,
    mm,
)
from core.compat import strict_zip


@dataclass(frozen=True)
class Limits:
    """The explicit limits of a catalog pair layout, with lengths in nm.

    width is the trace width, gap the edge-to-edge spacing, clearance the distance to
    other copper, launch_length the longest launch and skew the largest length
    difference between the pair's nets.
    """

    signal_layer: str
    reference_layer: str
    reference_net: str
    width: int
    gap: int
    clearance: int
    launch_length: int
    skew: int

    @classmethod
    def read(cls, data: dict) -> Limits:
        """Read and convert a catalog ``pair_layout``.

        Inputs:
            data: Layout with the signal_layer, reference_layer and reference_net names and
                width_mm, gap_mm, clearance_mm, max_launch_mm and max_skew_mm.
        Outputs:
            The limits in nm. Raises ValueError unless exactly these keys are present, the
            names are text and each dimension is a finite number from 0.001 to 100 mm.
        """
        labels = ("signal_layer", "reference_layer", "reference_net")
        dimensions = (
            "width_mm",
            "gap_mm",
            "clearance_mm",
            "max_launch_mm",
            "max_skew_mm",
        )
        if (
            not isinstance(data, dict)
            or set(data) != {*labels, *dimensions}
            or any(
                not isinstance(data[key], str) or not data[key].strip()
                for key in labels
            )
            or any(
                type(data[key]) not in (int, float)
                or not math.isfinite(data[key])
                or not 0.001 <= data[key] <= 100
                for key in dimensions
            )
        ):
            raise ValueError(
                "Pair seed needs explicit layers, return net, width, gap, clearance and launch and skew limits."
            )
        return cls(
            *(data[key] for key in labels), *(mm(data[key]) for key in dimensions)
        )


class PairSeed:
    """Seeds one declared differential pair with a short shared corridor."""

    @staticmethod
    def _pad_rows(board, names, layer):
        """Find the two pads of each net of a pair, ordered by footprint reference.

        Inputs:
            board: KiCad board.
            names: Names of the two nets.
            layer: KiCad ID of the signal layer.
        Outputs:
            One tuple of pad positions per net. Raises ValueError unless each net has
            exactly two pads on the layer, on separate footprints, and both nets end on
            the same two footprints.
        """
        rows = []
        for name in names:
            pads = [
                pad
                for pad in board.GetPads()
                if pad.GetNetname() == name and pad.IsOnLayer(layer)
            ]
            sites = {
                pad.GetParentFootprint().GetReference(): Point(
                    pad.GetPosition().x, pad.GetPosition().y
                )
                for pad in pads
            }
            if len(pads) != 2 or len(sites) != 2:
                raise ValueError(
                    f"{name} needs exactly two accessible pads on separate footprints."
                )
            rows.append(sites)
        if rows[0].keys() != rows[1].keys():
            raise ValueError(
                "The two signal nets must terminate on the same pair of footprints."
            )
        return [tuple(row[ref] for ref in sorted(row)) for row in rows]

    @staticmethod
    def _path(
        first: Point, last: Point, lane: int, launch_length: int, layer: str, swap: bool
    ) -> tuple[Segment, ...]:
        """Lay out one net from its first pad to its last along a lane.

        Inputs:
            first: Position of the first pad.
            last: Position of the last pad.
            lane: Coordinate of the shared corridor across the rows.
            launch_length: Longest 45-degree launch or landing, in nm.
            layer: Signal layer name.
            swap: Rows were transposed, so the corridor runs along y.
        Outputs:
            The segments of a 45-degree launch, a straight run along the lane and a
            45-degree landing. They are empty when the run does not fit, a launch is
            longer than allowed or the path forms a staircase.
        """
        a, b = abs(first.y - lane), abs(last.y - lane)
        if first.x + a >= last.x - b or max(a, b) * math.sqrt(2) > launch_length:
            return ()
        points = (first, Point(first.x + a, lane), Point(last.x - b, lane), last)
        if swap:
            points = tuple(Point(point.y, point.x) for point in points)
        result = tuple(
            Segment(s, e, layer) for s, e in zip(points, points[1:]) if s != e
        )
        return () if GridRouter.staircase_count(list(result)) else result

    @staticmethod
    def _interfere(first: Segment, last: Segment, spacing: int) -> bool:
        """Say whether two segments cross or come closer than ``spacing`` nm."""
        a, b, c, d = first.start, first.end, last.start, last.end
        return (
            _segments_intersect(a, b, c, d)
            or _point_near_segment(a, c, d, spacing)
            or _point_near_segment(b, c, d, spacing)
            or _point_near_segment(c, a, b, spacing)
            or _point_near_segment(d, a, b, spacing)
        )

    @classmethod
    def _candidates(cls, rows, limits: Limits, routers) -> list:
        """List the corridors that fit, best first.

        Inputs:
            rows: Pad positions of the two nets.
            limits: Launch, coupling and skew limits.
            routers: One grid router per net, used to test clearance to obstacles.
        Outputs:
            Pairs of a (total length, skew, shift size, shift) key and the two nets' paths,
            in ascending key order. Corridors are tried at 0.25 mm steps up to 8 mm either
            side of the pads' centre, and kept when their paths do not interfere, stay
            within the skew limit and are clear. Raises ValueError when the pad rows have
            no common routing axis or their launches cross.
        """
        p, n = rows
        if p[0].x == n[0].x and p[1].x == n[1].x and p[0].x != p[1].x:
            swap = False
        elif p[0].y == n[0].y and p[1].y == n[1].y and p[0].y != p[1].y:
            swap = True
            rows = [tuple(Point(point.y, point.x) for point in row) for row in rows]
        else:
            raise ValueError("The two pad rows need a common routing axis.")
        rows = [tuple(sorted(row, key=lambda point: point.x)) for row in rows]
        p, n = rows
        positive_above = p[0].y < n[0].y
        if any(
            first.y == last.y or (first.y < last.y) != positive_above
            for first, last in strict_zip(p, n)
        ):
            raise ValueError(
                "Crossing or coincident pair launches need a different routing topology."
            )
        center = (p[0].y + p[1].y + n[0].y + n[1].y) // 4
        pitch = limits.width + limits.gap + 2
        layer = limits.signal_layer
        candidates = []
        for shift in range(-32, 33):
            lower = center + shift * mm(0.25) - pitch // 2
            lanes = (lower, lower + pitch) if positive_above else (lower + pitch, lower)
            paths = tuple(
                cls._path(*row, lane, limits.launch_length, layer, swap)
                for row, lane in strict_zip(rows, lanes)
            )
            if not all(paths) or any(
                cls._interfere(a, b, pitch - 1) for a in paths[0] for b in paths[1]
            ):
                continue
            lengths = tuple(
                sum(math.hypot(s.end.x - s.start.x, s.end.y - s.start.y) for s in path)
                for path in paths
            )
            skew = abs(lengths[0] - lengths[1])
            if skew > limits.skew or not all(
                router.edge_clear(s.start, s.end, layer)
                for router, path in strict_zip(routers, paths)
                for s in path
            ):
                continue
            candidates.append(((sum(lengths), skew, abs(shift), shift), paths))
        return sorted(candidates, key=lambda item: item[0])

    @staticmethod
    def _add_reference_plane(
        board, reference_layer, ground, lower, upper, clearance: int
    ) -> None:
        """Add a ground zone on the reference layer, inset from the board edge.

        Inputs:
            board: KiCad board.
            reference_layer: KiCad ID of the reference layer.
            ground: Ground net.
            lower: Top-left corner of the board's bounds.
            upper: Bottom-right corner of the board's bounds.
            clearance: Zone clearance in nm; the inset is the larger of it and 0.5 mm.
        """
        zone = pcb.ZONE(board)
        zone.SetLayer(reference_layer)
        zone.SetNetCode(ground.GetNetCode())
        zone.SetLocalClearance(clearance)
        outline = zone.Outline()
        outline.NewOutline()
        margin = max(mm(0.5), clearance)
        for x, y in (
            (lower.x + margin, lower.y + margin),
            (upper.x - margin, lower.y + margin),
            (upper.x - margin, upper.y - margin),
            (lower.x + margin, upper.y - margin),
        ):
            outline.Append(x, y)
        board.Add(zone)

    @classmethod
    def run(cls, entry: Path, request: dict) -> dict:
        """Try a shared corridor and publish it into the new project only after native checks.

        Inputs:
            entry: Project entry of the new board.
            request: ``pair_layout`` limits and ``baseline_drc``, the DRC report of the
                board before seeding.
        Outputs:
            ``seeded`` with the pair, layers, gap, width, total trace length and length
            difference in mm and electrically_verified False, or ``deferred`` with the
            reason. Raises ValueError when the board already has copper or the pair,
            layers or ground are unsuitable, and RuntimeError when the baseline report
            does not show one open on each member or KiCad cannot write the candidate.
            The board file is restored unless a candidate is accepted.
        """
        paths = ProjectPaths(entry)
        source = paths.board.read_bytes()
        board = pcb.LoadBoard(str(paths.board))
        if board is None or list(board.GetTracks()) or list(board.Zones()):
            raise ValueError("Pair seeding needs a new board without routed copper.")
        limits = Limits.read(request["pair_layout"])
        layers = {
            str(board.GetLayerName(layer)): layer
            for layer in board.GetEnabledLayers().CuStack()
        }
        signal = layers.get(limits.signal_layer)
        reference = layers.get(limits.reference_layer)
        if (
            signal not in (pcb.F_Cu, pcb.B_Cu)
            or reference not in adjacent_reference_layers(board)[signal]
        ):
            raise ValueError(
                "Pair seeding needs an outer signal layer and its adjacent reference layer."
            )
        names = {str(name) for name in board.GetNetsByName() if str(name)}
        pairs = ProjectProfile.declared_pairs(paths.root, names)
        ground_matches = names & {limits.reference_net, "/" + limits.reference_net}
        if (
            not pairs
            or len(pairs) != 1
            or len(ground_matches) != 1
            or ground_matches & set(pairs[0])
        ):
            raise ValueError(
                "Pair seeding needs one declared pair and a distinct native ground net."
            )
        pair = pairs[0]
        ground = ground_matches.pop()
        rows = cls._pad_rows(board, pair, signal)
        edge = board.GetBoardEdgesBoundingBox()
        lower, upper = (
            Point(edge.GetX(), edge.GetY()),
            Point(edge.GetRight(), edge.GetBottom()),
        )
        obstacles = read_native_obstacles(paths.board, pair[0]).obstacles
        step = max(mm(0.25), (max(upper.x - lower.x, upper.y - lower.y) + 99) // 100)
        routers = tuple(
            GridRouter(
                lower,
                upper,
                step,
                (limits.signal_layer,),
                name,
                limits.width,
                limits.clearance,
                mm(0.6),
                mm(0.3),
                obstacles,
                allow_new_vias=False,
            )
            for name in pair
        )
        candidates = cls._candidates(rows, limits, routers)
        if not candidates:
            return {
                "status": "deferred",
                "reason": "No shared corridor fits the current launch, spacing and skew limits.",
            }

        schematic = paths.schematic.read_bytes()
        protection = ProtectedState.capture(source, schematic)
        baseline = json.loads(Path(request["baseline_drc"]).read_text(encoding="utf-8"))
        opens = unconnected_by_net(baseline, native_item_nets(board))
        if opens.get("<unknown>") or not all(opens.get(name) == 1 for name in pair):
            raise RuntimeError(
                "Native reports did not identify one open on each pair member."
            )
        native = Native.discover()
        reason = ""
        for index, ((total, skew, _, _), geometry) in enumerate(candidates[:4]):
            accepted = False
            try:
                board = pcb.LoadBoard(str(paths.board))
                cls._add_reference_plane(
                    board,
                    reference,
                    board.FindNet(ground),
                    lower,
                    upper,
                    limits.clearance,
                )
                trunks = set()
                for name, path in strict_zip(pair, geometry):
                    for segment in path:
                        trace = pcb.PCB_TRACK(board)
                        trace.SetLayer(signal)
                        trace.SetWidth(limits.width)
                        trace.SetNetCode(board.FindNet(name).GetNetCode())
                        trace.SetStart(pcb.VECTOR2I(segment.start.x, segment.start.y))
                        trace.SetEnd(pcb.VECTOR2I(segment.end.x, segment.end.y))
                        board.Add(trace)
                        if (
                            segment.start.x == segment.end.x
                            or segment.start.y == segment.end.y
                        ):
                            trunks.add(trace.m_Uuid.AsString())

                if pcb.SaveBoard(str(paths.board), board) is False:
                    raise RuntimeError("KiCad did not write the pair candidate.")
                report = native.report(
                    (
                        "pcb",
                        "drc",
                        "--refill-zones",
                        "--save-board",
                        "--schematic-parity",
                    ),
                    paths.board,
                    Path(request["baseline_drc"]).with_name(f"pair-{index}.json"),
                )
                verified = pcb.LoadBoard(str(paths.board))
                if verified is None:
                    raise RuntimeError("KiCad did not reopen the pair candidate.")
                remaining = unconnected_by_net(report, native_item_nets(verified))
                if (
                    remaining.get("<unknown>")
                    or any(remaining.get(name) for name in pair)
                    or any(
                        count > opens.get(name, 0) for name, count in remaining.items()
                    )
                    or any(
                        report[key]
                        for key in ("violations", "schematic_parity", "ignored_checks")
                    )
                ):
                    violations = sorted(
                        {finding["type"] for finding in report["violations"]}
                    )
                    reason = "Native connectivity or rules rejected the candidate" + (
                        " (" + ", ".join(violations) + ")." if violations else "."
                    )
                    continue
                if (
                    protection.differences(
                        paths.board.read_bytes(), paths.schematic.read_bytes()
                    )
                    or not audit_style(verified).passed
                ):
                    reason = "The candidate changed protected items or failed the geometry audit."
                    continue
                sampled = sample_reference(verified, set(pair), reference_net=ground)
                covered = {
                    item.item_id
                    for item in sampled.tracks
                    if not item.missing_samples and item.continuous_polygon
                }
                if len(trunks) != 2 or not trunks <= covered:
                    reason = "The coupled run had a gap in its sampled reference plane."
                    continue
                accepted = True
                return {
                    "status": "seeded",
                    "pair": list(pair),
                    "layer": limits.signal_layer,
                    "reference_layer": limits.reference_layer,
                    "minimum_gap_mm": pcb.ToMM(limits.gap),
                    "width_mm": pcb.ToMM(limits.width),
                    "total_trace_length_mm": round(total / 1_000_000, 6),
                    "length_difference_mm": round(skew / 1_000_000, 6),
                    "electrically_verified": False,
                }
            finally:
                if not accepted:
                    paths.board.write_bytes(source)
        return {"status": "deferred", "reason": reason}
