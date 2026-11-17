"""Search bottom-side placements and octilinear routes on saved-board copies.

The result is the lowest-cost *validated candidate among those sampled*, not
proof of a global optimum. The live board is never modified by this module.
"""

from __future__ import annotations

from core.project import native_path

import argparse
import hashlib
import itertools
import json
import math
import tempfile
from pathlib import Path
from typing import Any

import pcbnew

from core.kicad_campaign import CampaignFile, CampaignPlanner
from pcb.footprint_pose import native_pose_change
from pcb.route_cli import _endpoint, plan, save_validated_campaign
from pcb.route_validation import (
    RouteContract,
    _check_bottom_footprint_moves,
    _copy_inputs,
    _inputs,
    validate_routes,
)


def _fingerprints(source: Path) -> dict[Path, str]:
    return {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in _inputs(source)
    }


def _endpoint_for_board(endpoint: dict[str, Any], board) -> dict[str, Any]:
    """Resolve a pad endpoint against a particular placement, or keep coordinates."""
    if not isinstance(endpoint, dict):
        raise ValueError("An endpoint must be an object")
    if set(endpoint) == {"at_mm", "layer"}:
        _endpoint(endpoint, "endpoint")
        return endpoint
    if set(endpoint) != {"reference", "pad", "layer"}:
        raise ValueError("An endpoint needs at_mm/layer or reference/pad/layer")
    footprint = board.FindFootprintByReference(str(endpoint["reference"]))
    if footprint is None:
        raise ValueError(f"Endpoint footprint not found: {endpoint['reference']}")
    pads = [pad for pad in footprint.Pads() if pad.GetNumber() == str(endpoint["pad"])]
    if len(pads) != 1:
        raise ValueError("Endpoint pad is missing or ambiguous")
    position = pads[0].GetPosition()
    resolved = {
        "at_mm": [position.x / 1e6, position.y / 1e6],
        "layer": endpoint["layer"],
    }
    _endpoint(resolved, "endpoint")
    return resolved


def _staircase(segments: list[dict[str, Any]]) -> bool:
    """Detect repeated same-direction diagonal steps separated by a straight step."""
    for first, middle, last in zip(segments, segments[1:], segments[2:]):
        if len({first["layer"], middle["layer"], last["layer"]}) != 1:
            continue
        if first["to_mm"] != middle["from_mm"] or middle["to_mm"] != last["from_mm"]:
            continue
        directions = []
        for segment in (first, middle, last):
            dx = segment["to_mm"][0] - segment["from_mm"][0]
            dy = segment["to_mm"][1] - segment["from_mm"][1]
            directions.append(
                (
                    0 if abs(dx) < 1e-8 else (1 if dx > 0 else -1),
                    0 if abs(dy) < 1e-8 else (1 if dy > 0 else -1),
                )
            )
        if (
            directions[0] == directions[2]
            and all(directions[0])
            and bool(directions[1][0]) != bool(directions[1][1])
        ):
            return True
    return False


def _adjacent_pad_estimate(board, references: list[str]) -> float:
    """Estimate future wiring from movable pads to their nearest same-net pads."""
    pads = list(board.GetPads())
    distance = 0.0
    for reference in references:
        footprint = board.FindFootprintByReference(reference)
        for pad in footprint.Pads():
            net = pad.GetNetname()
            if not net or net.startswith("unconnected-"):
                continue
            origin = pad.GetPosition()
            options = []
            for other in pads:
                if (
                    other.GetNetname() != net
                    or other.GetParentFootprint().GetReference() == reference
                ):
                    continue
                target = other.GetPosition()
                dx, dy = abs(target.x - origin.x) / 1e6, abs(target.y - origin.y) / 1e6
                options.append(max(dx, dy) + (math.sqrt(2) - 1) * min(dx, dy))
            if options:
                distance += min(options)
    return distance


def optimize(project: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Sample a finite placement set, rank routes, and validate in cost order."""
    project = project.resolve()
    if not isinstance(config, dict) or set(config) - {
        "movable",
        "route",
        "max_candidates",
        "future_net_weight",
    }:
        raise ValueError("Unknown optimization configuration field")
    movable = config.get("movable", [])
    route = config.get("route")
    limit = config.get("max_candidates", 100)
    future_weight = config.get("future_net_weight", 1.0)
    if not isinstance(movable, list) or not isinstance(route, dict):
        raise ValueError("Provide a movable-footprint list and one route configuration")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("max_candidates must be a positive integer")
    if (
        isinstance(future_weight, bool)
        or not isinstance(future_weight, (int, float))
        or not math.isfinite(future_weight)
        or future_weight < 0
    ):
        raise ValueError("future_net_weight must be a finite nonnegative number")
    if "electrical" not in route or "board_snapshot" in route:
        raise ValueError(
            "Provide electrical constraints; the board snapshot is selected automatically"
        )
    names: list[str] = []
    choices: list[list[tuple[float, float, float | None]]] = []
    for item in movable:
        if (
            not isinstance(item, dict)
            or set(item) - {"reference", "positions_mm", "rotations_deg"}
            or not {"reference", "positions_mm"} <= set(item)
        ):
            raise ValueError("Each movable needs a reference and positions_mm")
        name, positions = item["reference"], item["positions_mm"]
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("Movable references must be unique nonempty strings")
        if not isinstance(positions, list) or not positions:
            raise ValueError("Each movable needs at least one candidate position")
        parsed = []
        for position in positions:
            if (
                not isinstance(position, list)
                or len(position) != 2
                or any(
                    isinstance(v, bool)
                    or not isinstance(v, (int, float))
                    or not math.isfinite(v)
                    for v in position
                )
            ):
                raise ValueError("Candidate positions must contain two finite numbers")
            parsed.append((float(position[0]), float(position[1])))
        if len(set(parsed)) != len(parsed):
            raise ValueError("Candidate positions must be distinct")
        rotations = item.get("rotations_deg")
        if rotations is None:
            angles: list[float | None] = [None]
        else:
            if (
                not isinstance(rotations, list)
                or not rotations
                or any(
                    isinstance(a, bool)
                    or not isinstance(a, (int, float))
                    or not math.isfinite(a)
                    for a in rotations
                )
            ):
                raise ValueError("rotations_deg must contain finite angles")
            angles = [float(a) % 360 for a in rotations]
            if len(set(angles)) != len(angles):
                raise ValueError(
                    "Candidate rotations must be distinct modulo 360 degrees"
                )
        names.append(name)
        choices.append(
            [(x, y, angle) for (x, y), angle in itertools.product(parsed, angles)]
        )
    total = math.prod(len(values) for values in choices)
    if total > limit:
        raise ValueError(
            f"The {total} placement combinations exceed max_candidates={limit}"
        )
    source = native_path(project, ".kicad_pcb")
    inputs = _fingerprints(source)
    original_bytes = source.read_bytes()
    source_hash = hashlib.sha256(original_bytes).hexdigest()
    original = pcbnew.LoadBoard(str(source))
    _check_bottom_footprint_moves(original, original_bytes.decode("utf-8"), set(names))
    original_start = _endpoint_for_board(route["start"], original)
    original_end = _endpoint_for_board(route["end"], original)
    start, start_layer = _endpoint(original_start, "start")
    end, end_layer = _endpoint(original_end, "end")
    contract = RouteContract(
        route["net"], start, start_layer, end, end_layer, tuple(route.get("remove", []))
    )
    candidates = []
    failures: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="jessie-joint-opt-") as directory:
        trial = Path(directory)
        _copy_inputs(list(inputs), source, trial)
        trial_board = native_path(trial, ".kicad_pcb")
        for placement in itertools.product(*choices):
            trial_board.write_bytes(original_bytes)
            moves = [
                (
                    CampaignFile.move("pcb", "footprint", name, x, y)
                    if angle is None
                    else native_pose_change(source, name, x, y, angle)
                )
                for name, (x, y, angle) in zip(names, placement, strict=True)
            ]
            if moves:
                trial_board.write_bytes(CampaignPlanner(trial).plan("pcb", moves).after)
            candidate_board = pcbnew.LoadBoard(str(trial_board))
            adjacent_estimate = _adjacent_pad_estimate(candidate_board, names)
            settings = dict(route)
            settings["board_snapshot"] = "pcb"
            settings.setdefault("forbid_staircase", True)
            settings["start"] = _endpoint_for_board(route["start"], candidate_board)
            settings["end"] = _endpoint_for_board(route["end"], candidate_board)
            try:
                result = plan(settings, trial, include_changes=True)
                if result["electrical"]["status"] != "within_supplied_limits":
                    raise ValueError("Route failed its supplied electrical screen")
                if _staircase(result["segments"]):
                    raise ValueError("Candidate contains repeated staircase steps")
                candidates.append(
                    {
                        "cost": result["cost"] + future_weight * adjacent_estimate,
                        "adjacent_pad_estimate_mm": adjacent_estimate,
                        "placement": placement,
                        "moves": moves,
                        "route": result,
                    }
                )
            except (ValueError, RuntimeError) as error:
                failures.append({"placement": placement, "reason": str(error)})
        candidates.sort(
            key=lambda item: (
                item["cost"],
                item["route"]["via_count"],
                item["route"]["bends"],
                item["route"]["length_mm"],
            )
        )
        for candidate in candidates:
            result = candidate["route"]
            changes = [*candidate["moves"], *result["unvalidated_campaign_changes"]]
            evidence = validate_routes(
                source,
                source_hash,
                changes,
                [contract],
                bottom_footprint_moves=tuple(names),
            )
            changes.extend(evidence.pop("_zone_refill_changes"))
            if evidence["status"] != "no_new_drc_violations" or (
                evidence["candidate_unconnected"] >= evidence["baseline_unconnected"]
            ):
                failures.append(
                    {
                        "placement": candidate["placement"],
                        "reason": "Native DRC/connectivity did not improve safely",
                    }
                )
                continue
            evidence["electrical_assessments"] = [
                {"net": route["net"], **result["electrical"]}
            ]
            if _fingerprints(source) != inputs:
                raise RuntimeError("Project inputs changed during optimization")
            result.pop("unvalidated_campaign_changes")
            return {
                "status": "validated_sampled_candidate",
                "source_sha256": source_hash,
                "sampled": total,
                "geometric_candidates": len(candidates),
                "placement": {
                    name: (
                        [x, y]
                        if angle is None
                        else {"at_mm": [x, y], "rotation_deg": angle}
                    )
                    for name, (x, y, angle) in zip(
                        names, candidate["placement"], strict=True
                    )
                },
                "cost": candidate["cost"],
                "adjacent_pad_estimate_mm": candidate["adjacent_pad_estimate_mm"],
                "route": result,
                "validation": evidence,
                "changes": changes,
                "limitations": "Finite position, rotation and grid search; electrical inputs are screens, not manufacturing approval.",
            }
    if _fingerprints(source) != inputs:
        raise RuntimeError("Project inputs changed during optimization")
    return {
        "status": "no_validated_candidate",
        "source_sha256": source_hash,
        "sampled": total,
        "geometric_candidates": len(candidates),
        "failures": failures,
        "limitations": "No candidate was found in this sampled search; feasibility remains unknown.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--save-campaign",
        help="save the validated best candidate for guarded application",
    )
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        result = optimize(args.project, config)
        changes = result.pop("changes", None)
        if args.save_campaign and changes:
            result["campaign"] = save_validated_campaign(
                args.project,
                "pcb",
                args.save_campaign,
                result["validation"],
                result["source_sha256"],
                changes,
            )
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0 if result["status"] == "validated_sampled_candidate" else 2
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f"Joint optimization failed: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
