"""zone_stitch.py: propose guarded, editable connections from ground islands to filled zones.

This is a narrow routing operation, not electrical signoff. It operates on a
private source-bound preview; native DRC and independent retention are required.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from core.project import native_path

import argparse
import hashlib
import json
import math
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from core.toolchain import kicad_cli

import pcbnew

from core.paths import asset_root, config_path, results_path
from core.pcb_io import net_classes, quiet_duplicate_image_handlers
from pcb.checkpoints import read_config
from pcb.freeroute_delta import (
    Trace,
    Via,
    native_clearance_checker,
    native_shortcut_obstacles,
    shortcut_clearance_checker,
)
from pcb.net_routing.controller import decode_checkpoint
from pcb.net_routing.electrical import Branch, Evidence, Quantity, Scenario, assess_tree
from pcb.net_routing.external import retain_candidate
from pcb.net_routing.fast import record_unimproved_batch
from pcb.net_routing.inventory import inventory_board
from pcb.net_routing.native import Snapshot, load_board
from pcb.net_routing.protection import ProtectedState, working_connectivity_classes
from pcb.net_routing.reference import high_speed_nets, reference_corridors
from pcb.net_routing.rules import RoutingRules
from pcb.net_routing.runner import _run_id, _source_hashes
from pcb.route_optimizer import COPPER_LAYERS, GridRouter, Point, Segment
from pcb.route_validation import _drc


@dataclass(frozen=True)
class StitchDimensions:
    """Dimensions in KiCad internal units, taken from the board rules."""

    track: int
    clearance: int
    via: int
    drill: int
    hole_clearance: int

    def __post_init__(self) -> None:
        """Reject non-integer or physically invalid dimensions.

        Outputs:
            None. Raises ValueError for non-integer values, a non-positive track or drill,
            a via that is not larger than its drill, or a negative clearance.
        """
        values = (self.track, self.clearance, self.via, self.drill, self.hole_clearance)
        if any(isinstance(x, bool) or not isinstance(x, int) for x in values):
            raise ValueError("Stitch dimensions must be integer board units")
        if self.track <= 0 or self.via <= self.drill or self.drill <= 0:
            raise ValueError(
                "Track and via dimensions must be positive and physically valid"
            )
        if self.clearance < 0 or self.hole_clearance < 0:
            raise ValueError("Clearances must be nonnegative")


@dataclass(frozen=True)
class StitchProof:
    """A source-bound calculation for an entire disconnected ground island."""

    pad_id: str
    dimensions: StitchDimensions
    max_length: int
    branches: tuple[Branch, ...]
    scenarios: tuple[Scenario, ...]
    island_items: tuple[str, ...] = ()
    board_sha256: str = ""


@dataclass(frozen=True)
class StitchAllowance:
    """Maximum stitch length and the exact island covered by its load model."""

    max_length: int
    island_items: frozenset[str]


class StitchAllowances:
    """Binds ground-stitch length allowances to exact copper islands and their evidence."""

    @staticmethod
    def working_stitch_allowances(
        board, classes: frozenset[str] | None
    ) -> dict[str, StitchAllowance]:
        """Bind short WIP ground proposals to exact islands without claiming ratings.

        Inputs:
            board: The loaded board.
            classes: Upper-case net classes the working scope allows, or None.
        Outputs:
            For each ground pad (by UUID) that is not an unplated hole, an allowance of
            2400000 nm (2.4 mm) for its whole copper island. Empty when ground is outside
            the scope or absent.
        """
        net = board.FindNet("GND")
        if (
            classes is None
            or net is None
            or str(net.GetNetClassName()).upper() not in classes
        ):
            return {}
        inventory = inventory_board(board)
        ground = next((item for item in inventory.nets if item.name == "GND"), None)
        if ground is None:
            return {}
        islands = {
            uid: frozenset(group) for group in ground.copper_islands for uid in group
        }
        return {
            pad.m_Uuid.AsString(): StitchAllowance(
                2_400_000, islands[pad.m_Uuid.AsString()]
            )
            for pad in board.GetPads()
            if pad.GetNetname() == "GND"
            and pad.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH
            and pad.m_Uuid.AsString() in islands
        }

    @staticmethod
    def island_matches(allowance: StitchAllowance, actual: tuple[str, ...]) -> bool:
        """Reject an allowance if any pad, trace or via joined or left its island.

        Inputs:
            allowance: The allowance to check.
            actual: UUIDs of the island's current items.
        Outputs:
            True when the allowance is a StitchAllowance with exactly those items.
        """
        return (
            isinstance(allowance, StitchAllowance)
            and frozenset(actual) == allowance.island_items
        )

    @staticmethod
    def verified_pad_limits(
        project: Path,
        proofs: tuple[StitchProof, ...],
        dimensions: StitchDimensions,
        *,
        board_sha256: str,
    ) -> dict[str, StitchAllowance]:
        """Require a matching board, complete island identity and assessed scenarios.

        Inputs:
            project: Project directory used for the electrical assessment.
            proofs: Candidate branch evidence, one per ground pad.
            dimensions: The board's stitch dimensions the proofs must match.
            board_sha256: SHA-256 of the board the proofs were made for.
        Outputs:
            For each pad whose proof is unique, matches the board and dimensions, has the
            pad-trace-via-plane topology with normal, transient and fault scenarios and
            passes the assessment, an allowance with its maximum length and island items.
            Other proofs are skipped.
        """
        result = {}
        counts = Counter(
            proof.pad_id
            for proof in proofs
            if isinstance(proof, StitchProof)
            and isinstance(proof.pad_id, str)
            and proof.pad_id
        )
        for proof in proofs:
            if (
                not isinstance(proof, StitchProof)
                or not isinstance(proof.pad_id, str)
                or not proof.pad_id
                or counts[proof.pad_id] != 1
                or proof.dimensions != dimensions
                or isinstance(proof.max_length, bool)
                or not isinstance(proof.max_length, int)
                or proof.max_length <= 0
                or not isinstance(board_sha256, str)
                or len(board_sha256) != 64
                or any(char not in "0123456789abcdef" for char in board_sha256)
                or proof.board_sha256 != board_sha256
                or not isinstance(proof.island_items, tuple)
                or not proof.island_items
                or any(
                    not isinstance(uid, str) or not uid for uid in proof.island_items
                )
                or len(set(proof.island_items)) != len(proof.island_items)
                or proof.pad_id not in proof.island_items
            ):
                continue
            topology = {
                (item.identifier, item.parent, item.child, item.kind)
                for item in proof.branches
            }
            if topology != {
                ("track", "pad", "via", "track"),
                ("via", "via", "plane", "via"),
            }:
                continue
            if not {"normal", "transient", "fault"} <= {
                item.name for item in proof.scenarios
            }:
                continue
            # Loads placed at the root would bypass the proposed trace and via and
            # falsely report zero current through both branches.
            if any(
                len(item.loads) != 1 or item.loads[0][0] != "plane"
                for item in proof.scenarios
            ):
                continue
            try:
                assessment = assess_tree(
                    project, "pad", proof.branches, proof.scenarios
                )
            except ValueError:
                continue
            # Input arithmetic is not a verification of the cited ratings or of
            # their applicability to these specific track and via dimensions.
            # The assessor does not yet provide such verification, so fail closed.
            if (
                assessment.status == "within_input_limits"
                and assessment.electrically_verified
            ):
                result[proof.pad_id] = StitchAllowance(
                    proof.max_length, frozenset(proof.island_items)
                )
        return result

    @staticmethod
    def proofs_from_policy(policy: dict) -> tuple[StitchProof, ...]:
        """Decode optional assessed branches from the source-bound routing policy.

        Inputs:
            policy: The routing policy dictionary; its zone_stitch proofs are decoded.
        Outputs:
            The decoded proofs. Raises ValueError for an invalid evidence policy or
            malformed branch evidence.
        """
        settings = policy.get("zone_stitch", {})
        if not isinstance(settings, dict) or not isinstance(
            settings.get("proofs", []), list
        ):
            raise ValueError("Invalid zone-stitch evidence policy")

        def quantity(value):
            """Decode a value, unit and evidence record into a Quantity."""
            return Quantity(
                value["value"], value["unit"], Evidence(**value["evidence"])
            )

        try:
            result = []
            for item in settings.get("proofs", []):
                branches = tuple(
                    Branch(
                        branch["identifier"],
                        branch["parent"],
                        branch["child"],
                        branch["kind"],
                        quantity(branch["capacity"]),
                        quantity(branch["resistance"]),
                    )
                    for branch in item["branches"]
                )
                scenarios = tuple(
                    Scenario(
                        scenario["name"],
                        tuple(
                            (load["node"], quantity(load["quantity"]))
                            for load in scenario["loads"]
                        ),
                        quantity(scenario["max_drop"]),
                    )
                    for scenario in item["scenarios"]
                )
                result.append(
                    StitchProof(
                        item["pad_id"],
                        StitchDimensions(**item["dimensions"]),
                        item["max_length"],
                        branches,
                        scenarios,
                        tuple(item.get("island_items", ())),
                        item.get("board_sha256", ""),
                    )
                )
            return tuple(result)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Invalid zone-stitch branch evidence: {error}") from error


class StitchGeometry:
    """Pure integer geometry for pad exits, search offsets and via clearance.

    All coordinates and lengths are in KiCad internal units (nm).
    """

    @staticmethod
    def candidate_sites(
        position: tuple[int, int],
        bounds: tuple[int, int, int, int],
        via: int,
        *,
        grid: int = 50_000,
        margin: int = 100_000,
    ) -> tuple[tuple[int, int], ...]:
        """Generate eight short, exactly octilinear pad exits outside its bounds.

        Inputs:
            position: Pad centre (x, y) in nm.
            bounds: Pad bounding box (left, top, right, bottom) in nm.
            via: Via diameter in nm.
            grid: Distance rounding grid in nm.
            margin: Extra clearance beyond the bounds in nm.
        Outputs:
            Eight (x, y) points, one in each octilinear direction. Raises ValueError for
            invalid dimensions or a position outside its bounds.
        """
        if via <= 0 or grid <= 0 or margin < 0:
            raise ValueError("Invalid via or sampling dimensions")
        x, y = position
        left, top, right, bottom = bounds
        if not left <= x <= right or not top <= y <= bottom:
            raise ValueError("Pad position lies outside its bounds")
        result = []
        for dx, dy in (
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),
            (1, 1),
            (1, -1),
            (-1, 1),
            (-1, -1),
        ):
            extent = max(
                (right - x if dx > 0 else x - left) if dx else 0,
                (bottom - y if dy > 0 else y - top) if dy else 0,
            )
            raw = extent + (via + 1) // 2 + margin
            distance = ((raw + grid - 1) // grid) * grid
            result.append((x + dx * distance, y + dy * distance))
        return tuple(result)

    @staticmethod
    @lru_cache(maxsize=8)
    def offsets(step: int, max_distance: int) -> tuple[tuple[int, int], ...]:
        """List search offsets from nearest to farthest, octilinear length first.

        Inputs:
            step: Grid step in nm.
            max_distance: Largest offset per axis in nm.
        Outputs:
            Every nonzero (dx, dy) multiple of the step within the distance, ordered by
            octilinear length. Raises ValueError for a non-positive step or a grid of
            fewer than one or more than 50 steps per side.
        """
        if step <= 0:
            raise ValueError("Invalid or excessive zone-access search grid")
        count = max_distance // step
        if count < 1 or count > 50:
            raise ValueError("Invalid or excessive zone-access search grid")
        offsets = [
            (x * step, y * step)
            for x in range(-count, count + 1)
            for y in range(-count, count + 1)
            if x or y
        ]
        offsets.sort(
            key=lambda point: (
                max(abs(point[0]), abs(point[1]))
                + (math.sqrt(2) - 1) * min(abs(point[0]), abs(point[1])),
                point[0] != 0 and point[1] != 0 and abs(point[0]) != abs(point[1]),
                point,
            )
        )
        return tuple(offsets)

    @staticmethod
    def candidate_paths(
        position: tuple[int, int],
        bounds: tuple[int, int, int, int],
        via: int,
        *,
        step: int = 200_000,
        max_distance: int = 2_400_000,
        margin: int = 100_000,
    ) -> tuple[tuple[tuple[int, int], ...], ...]:
        """Search short direct or one-chamfer paths, ordered by octilinear length.

        Inputs:
            position: Pad centre (x, y) in nm.
            bounds: Pad bounding box (left, top, right, bottom) in nm.
            via: Via diameter in nm.
            step: Search grid in nm.
            max_distance: Largest end offset per axis in nm.
            margin: Extra clearance beyond the bounds in nm.
        Outputs:
            Paths as point tuples: direct pad exits first, then paths to grid points with
            zero or one 45-degree corner. Raises ValueError for invalid dimensions or a
            position outside its bounds.
        """
        if via <= 0 or margin < 0:
            raise ValueError("Invalid via or margin")
        x, y = position
        left, top, right, bottom = bounds
        if not left <= x <= right or not top <= y <= bottom:
            raise ValueError("Pad position lies outside its bounds")
        padding = (via + 1) // 2 + margin
        paths = [
            (position, point)
            for point in StitchGeometry.candidate_sites(
                position, bounds, via, margin=margin
            )
            if max(abs(point[0] - x), abs(point[1] - y)) <= max_distance
        ]
        for dx, dy in StitchGeometry.offsets(step, max_distance):
            end = (x + dx, y + dy)
            if (
                left - padding < end[0] < right + padding
                and top - padding < end[1] < bottom + padding
            ):
                continue
            ax, ay = abs(dx), abs(dy)
            if not dx or not dy or ax == ay:
                paths.append((position, end))
                continue
            sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
            if ax > ay:
                corners = ((x + sx * (ax - ay), y), (x + sx * ay, y + sy * ay))
            else:
                corners = ((x, y + sy * (ay - ax)), (x + sx * ax, y + sy * ax))
            paths.extend((position, corner, end) for corner in corners)
        return tuple(paths)

    @staticmethod
    def via_clear_of_planned(
        point: tuple[int, int],
        others: list[tuple[int, int]],
        dimensions: StitchDimensions,
    ) -> bool:
        """Enforce both annular copper clearance and drilled-hole separation.

        Inputs:
            point: Proposed via centre (x, y) in nm.
            others: Centres of vias already planned.
            dimensions: Via, drill and clearance dimensions.
        Outputs:
            True when the point is far enough from every planned via.
        """
        required = max(
            dimensions.via + dimensions.clearance,
            dimensions.drill + dimensions.hole_clearance,
        )
        return all(
            (point[0] - x) ** 2 + (point[1] - y) ** 2 >= required**2 for x, y in others
        )

    @staticmethod
    def via_clear(
        point: Point, obstacles, dimensions: StitchDimensions, net: str
    ) -> bool:
        """Check a via position against obstacles on copper layers.

        Same-net obstacles that are not vias and need no via clearance are skipped.

        Inputs:
            point: Via centre in nm.
            obstacles: Obstacle records with bounds, layers and clearances.
            dimensions: Via and clearance dimensions.
            net: Net the via belongs to.
        Outputs:
            True when no considered obstacle intersects the via radius plus its clearance.
        """
        for obstacle in obstacles:
            if (
                obstacle.net == net
                and obstacle.kind != "via"
                and not obstacle.min_via_clearance
            ):
                continue
            if not any(layer in obstacle.layers for layer in COPPER_LAYERS):
                continue
            radius = (dimensions.via + 1) // 2 + max(
                dimensions.clearance, obstacle.min_via_clearance
            )
            if (
                point.x < obstacle.lower.x - radius
                or point.x > obstacle.upper.x + radius
                or point.y < obstacle.lower.y - radius
                or point.y > obstacle.upper.y + radius
            ):
                continue
            if GridRouter._intersects_box(point, point, obstacle, radius):
                return False
        return True

    @staticmethod
    def near_pad(
        obstacles,
        position: tuple[int, int],
        dimensions: StitchDimensions,
        maximum_clearance: int,
        search_radius: int,
    ):
        """Build a conservative local obstacle set once per pad.

        Inputs:
            obstacles: All obstacle records.
            position: Pad centre (x, y) in nm.
            dimensions: Via and clearance dimensions.
            maximum_clearance: Largest clearance of any rule in nm.
            search_radius: Search distance around the pad in nm.
        Outputs:
            A generator over the obstacles whose reach includes the pad position.
        """
        x, y = position
        for obstacle in obstacles:
            clearance = max(
                maximum_clearance,
                dimensions.clearance,
                obstacle.min_via_clearance,
                obstacle.min_track_clearance,
            )
            reach = search_radius + dimensions.via + clearance
            if not (
                x < obstacle.lower.x - reach
                or x > obstacle.upper.x + reach
                or y < obstacle.lower.y - reach
                or y > obstacle.upper.y + reach
            ):
                yield obstacle

    @staticmethod
    def profile_traces(path, layer, net_name, profile):
        """Materialize scoped package necks and wider trunks from the saved rules.

        Inputs:
            path: Points of the route in nm.
            layer: Copper layer name.
            net_name: Net of the traces.
            profile: Width profile that splits a segment into pieces with widths.
        Outputs:
            A tuple of traces, one per piece of each path segment.
        """
        return tuple(
            Trace(
                net_name,
                layer,
                width,
                (piece.start.x, piece.start.y),
                (piece.end.x, piece.end.y),
            )
            for a, b in zip(path, path[1:])
            for piece, width in profile.pieces(Segment(Point(*a), Point(*b), layer))
        )


class StitchBoard:
    """Reads dimensions from, and adds or removes stitch copper on, a loaded KiCad board."""

    @staticmethod
    def dimensions(board, net) -> StitchDimensions:
        """Read the stitch dimensions of a net's class from the board rules.

        Inputs:
            board: The loaded board.
            net: The net whose class supplies track, via and clearance values.
        Outputs:
            The dimensions in nm. Raises ValueError when the net class is unavailable.
        """
        classes = net_classes(board)
        name = str(net.GetNetClassName())
        if name not in classes:
            raise ValueError(f"No board net class is available for {name}")
        limits = classes[name]
        return StitchDimensions(
            int(limits.GetTrackWidth()),
            int(limits.GetClearance()),
            int(limits.GetViaDiameter()),
            int(limits.GetViaDrill()),
            int(board.GetDesignSettings().m_HoleToHoleMin),
        )

    @staticmethod
    def join_if_improved(board, additions) -> bool:
        """Keep an in-memory proposal only when native filled connectivity improves.

        Inputs:
            board: The loaded board.
            additions: Native items to add.
        Outputs:
            True when the unconnected count fell and the items stay on the board;
            otherwise False and the items are removed again.
        """
        connectivity = board.GetConnectivity()
        connectivity.Build(board)
        connectivity.RecalculateRatsnest()
        before = connectivity.GetUnconnectedCount(False)
        for item in additions:
            board.Add(item)
        connectivity.Build(board)
        connectivity.RecalculateRatsnest()
        if connectivity.GetUnconnectedCount(False) < before:
            return True
        for item in additions:
            board.Remove(item)
        connectivity.Build(board)
        connectivity.RecalculateRatsnest()
        return False

    @staticmethod
    def new_via(board, net, point, dimensions):
        """Create a through via on a net.

        Inputs:
            board: The loaded board.
            net: The via's net.
            point: Via centre (x, y) in nm.
            dimensions: Via diameter and drill in nm.
        Outputs:
            A native via from the front to the back layer, not yet added to the board.
        """
        via = pcbnew.PCB_VIA(board)
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetPosition(pcbnew.VECTOR2I(*point))
        via.SetWidth(dimensions.via)
        via.SetDrill(dimensions.drill)
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetNetCode(net.GetNetCode())
        return via

    @staticmethod
    def native_tracks(board, net, traces):
        """Create native tracks for traces on a net.

        Inputs:
            board: The loaded board.
            net: The tracks' net.
            traces: Traces with layer, width and endpoints in nm.
        Outputs:
            A list of native tracks, not yet added to the board.
        """
        result = []
        for trace in traces:
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(*trace.start))
            track.SetEnd(pcbnew.VECTOR2I(*trace.end))
            track.SetWidth(trace.width)
            track.SetLayer(board.GetLayerID(trace.layer))
            track.SetNetCode(net.GetNetCode())
            result.append(track)
        return result

    @staticmethod
    def via_touches_pad(board, point, drill):
        """Report whether a via drill would touch any plated pad.

        Inputs:
            board: The loaded board.
            point: Via centre (x, y) in nm.
            drill: Via drill diameter in nm.
        Outputs:
            True when a pad that is not an unplated hole is hit on an enabled copper layer.
        """
        position = pcbnew.VECTOR2I(*point)
        radius = (drill + 1) // 2
        return any(
            pad.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH
            and any(
                pad.IsOnLayer(layer) and pad.HitTest(position, radius, layer)
                for layer in board.GetEnabledLayers().CuStack()
            )
            for pad in board.GetPads()
        )

    @staticmethod
    def dimension_choices(board, dimensions, compact):
        """Use the same saved via floors for barrel-only and trace-plus-via access.

        Inputs:
            board: The loaded board.
            dimensions: The net class's dimensions.
            compact: Also offer the smallest via the board design rules allow.
        Outputs:
            The class dimensions, followed by the compact via dimensions when requested
            and smaller than the class via.
        """
        choices = [dimensions]
        if compact:
            settings = board.GetDesignSettings()
            drill = int(settings.m_MinThroughDrill)
            diameter = max(
                int(settings.m_ViasMinSize),
                drill + 2 * int(settings.m_ViasMinAnnularWidth),
            )
            if 0 < drill < diameter < dimensions.via:
                choices.append(replace(dimensions, via=diameter, drill=drill))
        return tuple(choices)


class ZoneStitchSearch:
    """Searches for guarded vias and short traces that join ground islands to filled zones."""

    @staticmethod
    def _plane_via(
        board,
        net,
        start,
        obstacles,
        dimensions,
        planned,
        *,
        compact=False,
        exact_clear=None,
    ):
        """Join actual filled islands with a via, without adding a ground detour.

        Inputs:
            board: The loaded board.
            net: The ground net.
            start: Pad centre (x, y) in nm.
            obstacles: Obstacle records near the board's copper.
            dimensions: The class via and clearance dimensions.
            planned: Via centres already planned; the new via is appended.
            compact: Also try the smallest allowed via.
            exact_clear: Optional exact clearance checker for the via.
        Outputs:
            True when a via placed on a filled zone improved native connectivity.
            The search stops after 5 s or 128 native trials per via size.
        """
        zones = [
            z
            for z in board.Zones()
            if z.GetNetname() == net.GetNetname() and not z.GetIsRuleArea()
        ]
        layers = tuple(board.GetEnabledLayers().CuStack())
        for choice in StitchBoard.dimension_choices(board, dimensions, compact):
            # A failed bounded search stays unresolved; independent batch checks
            # remain authoritative. Avoid thousands of expensive native rebuilds.
            deadline = time.monotonic() + 5.0
            trials = 0
            nearby = tuple(
                StitchGeometry.near_pad(
                    obstacles, start, choice, choice.clearance, 6_000_000
                )
            )
            for dx, dy in StitchGeometry.offsets(200_000, 6_000_000):
                if time.monotonic() >= deadline or trials >= 128:
                    break
                point = start[0] + dx, start[1] + dy
                if not StitchGeometry.via_clear_of_planned(point, planned, dimensions):
                    continue
                if not StitchGeometry.via_clear(
                    Point(*point), nearby, choice, str(net.GetNetname())
                ) and (
                    exact_clear is None
                    or not exact_clear(
                        (Via(str(net.GetNetname()), point, choice.via, choice.drill),)
                    )
                ):
                    continue
                position = pcbnew.VECTOR2I(*point)
                if not any(
                    zone.IsOnLayer(layer) and zone.HitTestFilledArea(layer, position)
                    for zone in zones
                    for layer in layers
                ):
                    continue
                if StitchBoard.via_touches_pad(board, point, choice.drill):
                    continue
                trials += 1
                if StitchBoard.join_if_improved(
                    board, [StitchBoard.new_via(board, net, point, choice)]
                ):
                    planned.append(point)
                    return True
        return False

    @staticmethod
    def propose(
        board,
        report: dict,
        project_bytes: bytes,
        rules: RoutingRules,
        *,
        eligible: dict[str, StitchAllowance],
        net_name: str = "GND",
        compact_vias: bool = False,
        progress=None,
    ) -> dict:
        """Add a conservative batch to an in-memory board; the caller must validate it.

        Inputs:
            board: The loaded board, changed in memory.
            report: Native DRC report whose unconnected items select the islands.
            project_bytes: Project file bytes that define the high-speed nets.
            rules: Routing rules used for widths and clearances.
            eligible: Allowances for ground pads, keyed by UUID.
            net_name: Net to stitch; only GND is supported.
            compact_vias: Also try the smallest allowed via.
            progress: Optional callable that receives progress messages.
        Outputs:
            A dictionary with the open pad count, proposed stitches, new vias and pads
            without an assessed allowance. Raises ValueError for another net or a missing
            net and RuntimeError when the ground islands or high-speed classification
            cannot be established.
        """
        # Other power/signal nets need branch-current and voltage-specific approval.
        if net_name != "GND":
            raise ValueError(
                "Zone stitching is currently restricted to the ground return"
            )
        net = board.FindNet(net_name)
        if net is None:
            raise ValueError(f"Board has no net named {net_name}")
        dimensions = StitchBoard.dimensions(board, net)
        ids = {
            entry.get("uuid")
            for item in report.get("unconnected_items", ())
            for entry in item.get("items", ())
        }
        inventory = inventory_board(board)
        net_inventory = next(
            (item for item in inventory.nets if item.name == net_name), None
        )
        if net_inventory is None:
            raise RuntimeError("Cannot establish the ground copper islands")
        islands = {
            uid: index
            for index, island in enumerate(net_inventory.copper_islands)
            for uid in island
        }
        open_islands = {islands[uid] for uid in ids if uid in islands}
        # A multi-layer zone has one UUID for several disconnected polygons.
        # Native DRC's zone-to-zone endpoints cannot identify their pad islands.
        if (
            getattr(net_inventory, "zone_ids", ())
            and len(net_inventory.copper_islands) > 1
        ):
            barrels = {
                pad.m_Uuid.AsString()
                for pad in board.GetPads()
                if pad.GetDrillSize().x or pad.GetDrillSize().y
            }
            barrels.update(
                item.m_Uuid.AsString()
                for item in board.GetTracks()
                if isinstance(item, pcbnew.PCB_VIA)
            )
            main = max(
                range(len(net_inventory.copper_islands)),
                key=lambda index: (
                    len(net_inventory.copper_islands[index]),
                    sum(uid in barrels for uid in net_inventory.copper_islands[index]),
                ),
            )
            open_islands.update(
                index
                for index in range(len(net_inventory.copper_islands))
                if index != main
            )
        pads = sorted(
            (
                pad
                for pad in board.GetPads()
                if pad.GetNetname() == net_name
                and pad.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH
                and (
                    pad.m_Uuid.AsString() in ids
                    or (
                        pad.m_Uuid.AsString() in eligible
                        and islands.get(pad.m_Uuid.AsString()) in open_islands
                    )
                )
            ),
            key=lambda pad: pad.m_Uuid.AsString(),
        )
        critical = high_speed_nets(
            project_bytes, {str(name) for name in board.GetNetsByName().keys()}
        )
        if not critical:
            raise RuntimeError(
                "USB/Ethernet reference-copper classifications are unavailable"
            )
        reservations = reference_corridors(board, critical)
        if reservations.unsupported_items:
            raise RuntimeError(
                "Unsupported high-speed geometry prevents reference comparison"
            )
        # Ground is the reserved reference material. Its new drill/plane effects
        # still undergo the independent sampled-reference comparison on retention.
        obstacles = native_shortcut_obstacles(board)
        profile = rules.profile(net_name, dimensions.track, dimensions.clearance)
        exact_clear = native_clearance_checker(
            board, clearance=dimensions.clearance, profile_for=lambda _: profile
        )
        zones = [
            zone
            for zone in board.Zones()
            if zone.GetNetname() == net_name and not zone.GetIsRuleArea()
        ]
        planned: list[tuple[int, int]] = []
        connected_islands = set()
        blocked = 0
        for ordinal, pad in enumerate(pads, 1):
            uid = pad.m_Uuid.AsString()
            allowance = eligible.get(uid)
            if not isinstance(allowance, StitchAllowance):
                blocked += 1
                continue
            island = islands.get(uid)
            if island is None:
                blocked += 1
                continue
            if island in connected_islands:
                continue
            if not StitchAllowances.island_matches(
                allowance, net_inventory.copper_islands[island]
            ):
                blocked += 1
                continue
            if progress:
                progress(
                    f"Searching ground island access {ordinal}/{len(pads)}; "
                    f"{len(connected_islands)} joins found"
                )
            max_length = allowance.max_length
            layers = [
                board.GetLayerName(identifier)
                for identifier in board.GetEnabledLayers().CuStack()
                if pad.IsOnLayer(identifier)
            ]
            if not layers:
                continue
            position = pad.GetPosition()
            start = (int(position.x), int(position.y))
            box = pad.GetBoundingBox()
            bounds = (
                int(box.GetX()),
                int(box.GetY()),
                int(box.GetRight()),
                int(box.GetBottom()),
            )
            nearby = tuple(
                StitchGeometry.near_pad(
                    obstacles, start, dimensions, profile.maximum_clearance, 2_400_000
                )
            )
            conservative = shortcut_clearance_checker(
                nearby, profile_for=lambda _: profile
            )

            def clear_track(traces):
                """Accept traces that pass either the conservative or the exact clearance check."""
                return conservative(traces) or exact_clear(traces)

            if zones and ZoneStitchSearch._plane_via(
                board,
                net,
                start,
                obstacles,
                dimensions,
                planned,
                compact=compact_vias,
                exact_clear=exact_clear,
            ):
                connected_islands.add(island)
                continue
            # First join a filled plane directly. A through-hole ground pad already
            # provides a barrel, and an SMD pad may have a same-layer plane nearby.
            direct = None
            radius = (dimensions.track + 1) // 2
            for distance in (400_000, 600_000, 1_000_000, 1_500_000, 2_000_000):
                if distance > max_length:
                    break
                for dx, dy in (
                    (1, 0),
                    (0, 1),
                    (-1, 0),
                    (0, -1),
                    (1, 1),
                    (-1, 1),
                    (-1, -1),
                    (1, -1),
                ):
                    end = (start[0] + dx * distance, start[1] + dy * distance)
                    if math.dist(start, end) > max_length:
                        continue
                    for candidate_layer in layers:
                        layer_id = board.GetLayerID(candidate_layer)
                        contacts = [
                            pcbnew.VECTOR2I(end[0] + ox, end[1] + oy)
                            for ox, oy in (
                                (0, 0),
                                (radius, 0),
                                (-radius, 0),
                                (0, radius),
                                (0, -radius),
                            )
                        ]
                        if not any(
                            all(
                                zone.HitTestFilledArea(layer_id, point)
                                for point in contacts
                            )
                            for zone in zones
                            if zone.IsOnLayer(layer_id)
                        ):
                            continue
                        traces = StitchGeometry.profile_traces(
                            (start, end), candidate_layer, net_name, profile
                        )
                        if clear_track(traces) and StitchBoard.join_if_improved(
                            board, StitchBoard.native_tracks(board, net, traces)
                        ):
                            direct = traces
                            break
                    if direct is not None:
                        break
                if direct is not None:
                    break
            if direct is not None:
                connected_islands.add(island)
                continue
            if len(layers) != 1:
                continue
            layer = layers[0]
            for choice in StitchBoard.dimension_choices(
                board, dimensions, compact_vias
            ):
                for path in StitchGeometry.candidate_paths(start, bounds, choice.via):
                    if (
                        sum(
                            math.hypot(b[0] - a[0], b[1] - a[1])
                            for a, b in zip(path, path[1:])
                        )
                        > max_length
                    ):
                        continue
                    x, y = path[-1]
                    end = pcbnew.VECTOR2I(x, y)
                    if not any(
                        zone.HitTestFilledArea(layer_id, end)
                        for zone in zones
                        for layer_id in board.GetEnabledLayers().CuStack()
                        if zone.IsOnLayer(layer_id)
                    ):
                        continue
                    traces = StitchGeometry.profile_traces(
                        path, layer, net_name, profile
                    )
                    if (
                        not StitchGeometry.via_clear_of_planned(
                            (x, y), planned, dimensions
                        )
                        or (
                            not StitchGeometry.via_clear(
                                Point(x, y), nearby, choice, net_name
                            )
                            and not exact_clear(
                                (Via(net_name, (x, y), choice.via, choice.drill),)
                            )
                        )
                        or StitchBoard.via_touches_pad(board, (x, y), choice.drill)
                        or not clear_track(traces)
                    ):
                        continue
                    if not StitchBoard.join_if_improved(
                        board,
                        [
                            StitchBoard.new_via(board, net, (x, y), choice),
                            *StitchBoard.native_tracks(board, net, traces),
                        ],
                    ):
                        continue
                    planned.append((x, y))
                    connected_islands.add(island)
                    break
                if island in connected_islands:
                    break
        return {
            "open_pads": len(pads),
            "proposed_stitches": len(connected_islands),
            "new_vias": len(planned),
            "unassessed_pads": blocked,
        }


class ZoneStitchRun:
    """Generates and retains one zone-stitch batch from a private routing preview."""

    @staticmethod
    def run_one(project: Path, run_id: str, *, progress=print) -> dict:
        """Generate one batch and retain it only after independent native checks.

        Inputs:
            project: Project directory.
            run_id: Source private preview run.
            progress: Callable that receives status messages.
        Outputs:
            The retained preview record with the stitch counts, or a record with status
            blocked_missing_electrical_evidence or no_candidate when nothing was proposed.
            Raises ValueError for an invalid source preview or board and RuntimeError when
            protected items or the source preview change.
        """
        started = time.monotonic()
        project = project.resolve()
        run_id = _run_id(run_id)
        assets = asset_root(project)
        policy, policy_hash = read_config(config_path(project, "autopcb.json"))
        state_path = results_path(project, "autoroute") / f"{run_id}.json"
        state_bytes = state_path.read_bytes()
        state = json.loads(state_bytes)
        if state.get("schema") != 1 or state.get("run_id") != run_id:
            raise ValueError("Invalid source routing preview")
        with tempfile.TemporaryDirectory(prefix="autopcb-zone-candidate-") as folder:
            candidate_path = Path(folder) / "candidate.kicad_pcb"
            with Snapshot(native_path(project, ".kicad_pcb")) as snapshot:
                inputs = _source_hashes(snapshot, project, assets)
                preview = decode_checkpoint(
                    state["checkpoint"],
                    inputs,
                    snapshot.tool_hashes,
                    policy_hash,
                    allow_tool_changes=True,
                )
                schematic = (native_path(snapshot.directory, ".kicad_sch")).read_bytes()
                if ProtectedState.capture(snapshot.original, schematic).differences(
                    preview, schematic
                ):
                    raise RuntimeError("Preview changed protected design items")
                snapshot.board_path.write_bytes(preview)
                progress("Refilling and inspecting the private preview")
                cli = kicad_cli()
                report = _drc(
                    cli,
                    snapshot.board_path,
                    snapshot.directory / "zone-baseline.json",
                    save_refill=True,
                    schematic_parity=True,
                )
                board = load_board(snapshot.board_path, refill_zones=True)
                rules = RoutingRules.from_board(
                    board, snapshot.board_path.with_suffix(".kicad_dru")
                )
                net = board.FindNet("GND")
                if net is None:
                    raise ValueError("Board has no ground net")
                eligible = StitchAllowances.verified_pad_limits(
                    project,
                    StitchAllowances.proofs_from_policy(policy),
                    StitchBoard.dimensions(board, net),
                    board_sha256=hashlib.sha256(preview).hexdigest(),
                )
                working_classes = working_connectivity_classes(board)
                for uid, allowance in StitchAllowances.working_stitch_allowances(
                    board, working_classes
                ).items():
                    eligible.setdefault(uid, allowance)
                result = ZoneStitchSearch.propose(
                    board,
                    report,
                    (native_path(snapshot.directory, ".kicad_pro")).read_bytes(),
                    rules,
                    eligible=eligible,
                    compact_vias=working_classes is not None,
                    progress=progress,
                )
                result["electrically_verified"] = False
                result["working_connectivity"] = working_classes is not None
                progress(
                    f"Screened {result['open_pads']} ground pads associated with open connections; "
                    f"proposed {result['proposed_stitches']} stitches; "
                    + (
                        f"{result['unassessed_pads']} without a working allowance; electrical qualification remains open"
                        if working_classes is not None
                        else f"{result['unassessed_pads']} lack current-board island electrical evidence"
                    )
                )
                if result["proposed_stitches"]:
                    if not pcbnew.SaveBoard(str(candidate_path), board):
                        raise RuntimeError(
                            "Could not save the temporary stitching proposal"
                        )
                snapshot.check()
                if state_path.read_bytes() != state_bytes:
                    raise RuntimeError(
                        "Source preview changed while creating zone stitches"
                    )
            if not result["proposed_stitches"]:
                record_unimproved_batch(project, run_id, time.monotonic() - started)
                status = (
                    "blocked_missing_electrical_evidence"
                    if result["unassessed_pads"]
                    else "no_candidate"
                )
                return {"status": status, "run_id": run_id, **result}
            progress("Independently checking the proposed batch")
            retained = retain_candidate(
                project,
                run_id,
                candidate_path,
                routing_seconds=time.monotonic() - started,
                expected_previous=state_bytes,
                engine="native-zone-stitch",
                progress=progress,
            )
            return {**retained, **result, "elapsed_seconds": time.monotonic() - started}


class ZoneStitchCommand:
    """Command line that proposes and retains one zone-stitch batch."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Run one zone-stitch batch and print the outcome.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 after a result is printed, and 1 when an OSError, RuntimeError or
            ValueError stops it (the message goes to standard error).
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--project", type=Path, required=True)
        parser.add_argument(
            "--run", required=True, help="source private routing preview"
        )
        args = parser.parse_args(argv)
        try:
            with quiet_duplicate_image_handlers():
                result = ZoneStitchRun.run_one(
                    args.project,
                    args.run,
                    progress=lambda message: print(message, flush=True),
                )
            print(f"Result: {result['status']}; run: {result['run_id']}")
            if "preview_opens" in result:
                print(
                    f"Private preview: {result['preview_opens']} opens; "
                    f"{result['errors']} errors and {result['warnings']} warnings"
                )
            return 0
        except (OSError, RuntimeError, ValueError) as error:
            print(f"Zone stitching stopped: {error}", file=sys.stderr)
            return 1


main = ZoneStitchCommand()


if __name__ == "__main__":
    raise SystemExit(main())
