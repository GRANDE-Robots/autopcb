"""Propose a grid route from JSON without modifying any KiCad file.

Example configuration (all coordinates and dimensions in mm)::

  {
    "net": "SIGNAL", "bounds_mm": [0, 0, 30, 20], "step_mm": 0.25,
    "start": {"at_mm": [2, 2], "layer": "F.Cu"},
    "end": {"at_mm": [20, 10], "layer": "B.Cu"},
    "layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
    "width_mm": 0.2, "clearance_mm": 0.2,
    "via": {"size_mm": 0.6, "drill_mm": 0.3},
    "board_snapshot": "pcb",
    "weights": {"length": 1, "bend": 1, "via": 10}
  }

For electrical screening add a branch-specific object such as::

  "electrical": {
    "current_a": 0.1, "current_basis": "replace with a documented branch load",
    "voltage_difference_v": 12, "voltage_basis": "replace with a transient review",
    "clearance": {"min_mm": 0.2, "max_voltage_difference_v": 12,
                  "basis": "replace with a reviewed clearance assessment"},
    "voltage_drop": {"max_v": 0.1, "copper_resistivity_ohm_mm": 0.00002,
                     "basis": "replace with a reviewed voltage and temperature budget"},
    "tracks": {"F.Cu": {"min_width_mm": 0.2, "max_current_a": 0.1,
                        "copper_thickness_mm": 0.035,
                        "basis": "replace with a reviewed thermal assessment"}},
    "via": {"min_size_mm": 0.6, "min_drill_mm": 0.3,
            "plating_thickness_mm": 0.025, "max_current_a": 0.1,
            "max_resistance_ohm": 0.1,
            "basis": "replace with a reviewed via assessment"}
  }

The example values are placeholders, not Jessie design ratings. Current must
cover the worst credible branch current including applicable transients;
voltage difference must cover neighboring nets and applicable transients.

Optional rectangular or centerline-capsule obstacles, a symmetry guide and a saved board snapshot
make the geometric model more useful. An ``electrical`` object can supply a
worst-case branch current and voltage difference, layer-specific track limits,
a clearance limit, and a per-via limit. The planner excludes layers that cannot
meet the supplied track limits and rejects vias that cannot meet their limit.
An unscreened proposal may be inspected but cannot be saved through this CLI.
Optional ``layer_penalties`` set a nonnegative cost per millimetre. The default
charges no layer-specific penalty; saved-board electrical and local reference
requirements still apply. Supplied ratings and stackup require independent review.
Results also require native DRC, connectivity and reference-plane checks.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from core.kicad_campaign import CampaignFile, digest
from core.kicad_sexpr import parse, quote
from core.pcb_io import quiet_duplicate_image_handlers
from pcb.route_constraints import ElectricalConstraints
from pcb.route_native import NativeObstacleCache, read_native_obstacles
from pcb.route_validation import validate_route, verify_validation_evidence
from pcb.route_optimizer import (
    NM_PER_MM,
    COPPER_LAYERS,
    GridRouter,
    NoGridRoute,
    Obstacle,
    Point,
    Route,
    SearchLimitReached,
    Weights,
    mirror_points,
    mm,
    to_mm,
)


def _fields(value: object, allowed: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown {name} fields: {', '.join(sorted(unknown))}")
    return value


def _bounds(value: object) -> tuple[Point, Point]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("Bounds must be [x_min, y_min, x_max, y_max]")
    return Point.from_mm(value[:2]), Point.from_mm(value[2:])


def _obstacle(value: object) -> Obstacle:
    data = _fields(
        value, {"bounds_mm", "layers", "net", "kind", "centerline_mm", "radius_mm",
                "min_track_clearance_mm", "min_via_clearance_mm"},
        "obstacle",
    )
    lower, upper = _bounds(data.get("bounds_mm"))
    layers = data.get("layers")
    if not isinstance(layers, list) or not all(
        isinstance(item, str) for item in layers
    ):
        raise ValueError("Obstacle layers must be an array of copper layer names")
    if ("centerline_mm" in data) != ("radius_mm" in data):
        raise ValueError("A capsule obstacle needs both centerline_mm and radius_mm")
    centerline, radius = None, 0
    if "centerline_mm" in data:
        points = data["centerline_mm"]
        if not isinstance(points, list) or len(points) != 2:
            raise ValueError("A capsule centerline needs two points")
        centerline = tuple(Point.from_mm(point) for point in points)
        radius = mm(data["radius_mm"])
        if radius <= 0:
            raise ValueError("A capsule radius must be positive")
    return Obstacle(
        lower, upper, tuple(layers), data.get("net", ""), data.get("kind", "copper"),
        centerline=centerline, copper_radius=radius,
        min_track_clearance=mm(data.get("min_track_clearance_mm", 0)),
        min_via_clearance=mm(data.get("min_via_clearance_mm", 0)),
    )


def _endpoint(value: object, name: str) -> tuple[Point, str]:
    data = _fields(value, {"at_mm", "layer"}, name)
    if not isinstance(data.get("layer"), str):
        raise ValueError(f"{name} requires a layer")
    return Point.from_mm(data.get("at_mm")), data["layer"]


def _weights(value: object) -> Weights:
    data = _fields(value, {"length", "bend", "via", "diagonal", "symmetry"}, "weights")
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float)) for v in data.values()
    ):
        raise ValueError("Weights must be numbers")
    return Weights(**data)


def _guide(value: object) -> tuple[Point, ...]:
    if value is None:
        return ()
    data = _fields(
        value, {"points_mm", "mirror_axis", "mirror_at_mm"}, "symmetry guide"
    )
    points = data.get("points_mm")
    if not isinstance(points, list) or not points:
        raise ValueError("Symmetry guide requires points_mm")
    guide = tuple(Point.from_mm(item) for item in points)
    axis = data.get("mirror_axis")
    if axis is not None:
        if "mirror_at_mm" not in data:
            raise ValueError("A mirrored guide requires mirror_at_mm")
        guide = mirror_points(guide, axis, mm(data["mirror_at_mm"]))
    elif "mirror_at_mm" in data:
        raise ValueError("mirror_at_mm requires a mirror_axis")
    return guide


def _number(value: int) -> str:
    text = format(Decimal(value) / NM_PER_MM, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def campaign_changes(
    route: Route,
    net: str,
    width: int,
    via_size: int,
    via_drill: int,
    removals: list[dict[str, str]] | None = None,
    *,
    segment_dimensions=None,
) -> list[dict[str, Any]]:
    """Return ordinary, ungrouped KiCad primitives; do not write a campaign."""
    changes = [
        {"document": "pcb", "target": removal, "op": "remove", "path": []}
        for removal in (removals or [])
    ]
    materialized = (pair for original in route.segments for pair in
                    (segment_dimensions(original) if segment_dimensions else ((original, width),)))
    for segment, segment_width in materialized:
        expression = (
            f"(segment (start {_number(segment.start.x)} {_number(segment.start.y)}) "
            f"(end {_number(segment.end.x)} {_number(segment.end.y)}) "
            f"(width {_number(segment_width)}) (layer {quote(segment.layer)}) "
            f"(net {quote(net)}) (uuid {quote(str(uuid.uuid4()))}))"
        )
        changes.append(
            {
                "document": "pcb",
                "target": {"kind": "root"},
                "op": "insert",
                "path": [],
                "expression": expression,
            }
        )
    for via in route.vias:
        expression = (
            f"(via (at {_number(via.at.x)} {_number(via.at.y)}) "
            f"(size {_number(via_size)}) (drill {_number(via_drill)}) "
            f'(layers "F.Cu" "B.Cu") (net {quote(net)}) '
            f"(uuid {quote(str(uuid.uuid4()))}))"
        )
        changes.append(
            {
                "document": "pcb",
                "target": {"kind": "root"},
                "op": "insert",
                "path": [],
                "expression": expression,
            }
        )
    return changes


def plan(
    data: dict[str, Any],
    project: Path,
    *,
    include_changes: bool = False,
    validate: bool = False,
    check_budget: Callable[[], None] | None = None,
    native_cache: NativeObstacleCache | None = None,
    candidate_check: Callable[[Route], bool] | None = None,
) -> dict[str, Any]:
    if native_cache is not None and not isinstance(native_cache, NativeObstacleCache):
        raise ValueError("The native obstacle cache must be a NativeObstacleCache")
    if check_budget is not None:
        if not callable(check_budget):
            raise ValueError("The shared search budget must be callable")
        check_budget()
    if candidate_check is not None and not callable(candidate_check):
        raise ValueError("The candidate check must be callable")
    _fields(
        data,
        {
            "net",
            "bounds_mm",
            "step_mm",
            "start",
            "end",
            "layers",
            "width_mm",
            "clearance_mm",
            "via",
            "obstacles",
            "weights",
            "symmetry_guide",
            "board_snapshot",
            "assume_zone_refill",
            "max_expansions",
            "max_cells",
            "remove",
            "electrical",
            "layer_penalties",
            "forbid_staircase",
            "min_segment_length_mm",
            "gentle_turns",
            "initial_directions",
            "final_directions",
            "enforce_endpoint_spacing",
            "first_feasible",
            "topology_heuristic",
            "heuristic_weight",
        },
        "configuration",
    )
    net = data.get("net")
    if not isinstance(net, str) or not net:
        raise ValueError("A nonempty net name is required")
    lower, upper = _bounds(data.get("bounds_mm"))
    start, start_layer = _endpoint(data.get("start"), "start")
    end, end_layer = _endpoint(data.get("end"), "end")
    layers = data.get("layers", ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    if not isinstance(layers, list) or not all(
        isinstance(item, str) for item in layers
    ):
        raise ValueError("layers must be an array of copper layer names")
    if not layers or any(layer not in COPPER_LAYERS for layer in layers):
        raise ValueError("layers must contain only known copper layers")
    requested_layers = tuple(layers)
    via = _fields(data.get("via", {}), {"size_mm", "drill_mm"}, "via")
    width = mm(data.get("width_mm", 0.2))
    clearance = mm(data.get("clearance_mm", 0.2))
    size, drill = mm(via.get("size_mm", 0.6)), mm(via.get("drill_mm", 0.3))
    constraints = (
        ElectricalConstraints.from_config(data["electrical"])
        if "electrical" in data
        else None
    )
    excluded_layers: dict[str, str] = {}
    obstacle_data = data.get("obstacles", [])
    if not isinstance(obstacle_data, list):
        raise ValueError("obstacles must be an array")
    obstacles = [_obstacle(item) for item in obstacle_data]
    ignored_zones = 0
    source_hash = None
    existing_vias = ()
    snapshot_path: Path | None = None
    snapshot = data.get("board_snapshot")
    removals = data.get("remove", [])
    if not isinstance(removals, list):
        raise ValueError("remove must be an array of copper kind/UUID objects")
    if removals and snapshot is None:
        raise ValueError("Removing existing copper requires a saved board_snapshot")
    assume_refill = data.get("assume_zone_refill", False)
    if not isinstance(assume_refill, bool):
        raise ValueError("assume_zone_refill must be true or false")
    if snapshot is not None:
        if not isinstance(snapshot, str) or not snapshot:
            raise ValueError("board_snapshot must be a saved PCB path or 'pcb'")
        path = project / "v4.1.kicad_pcb" if snapshot == "pcb" else Path(snapshot)
        if not path.is_absolute():
            path = project / path
        snapshot_path = path.resolve()
        reader = native_cache.load if native_cache is not None else read_native_obstacles
        native = reader(path, net, assume_refill=assume_refill, removals=removals)
        disabled = [layer for layer in requested_layers if layer not in native.enabled_layers]
        if "layers" in data and disabled:
            raise ValueError("Requested copper layers are not enabled on the saved board: " + ", ".join(disabled))
        layers = ([layer for layer in layers if layer in native.enabled_layers]
                  if "layers" in data else list(native.enabled_layers))
        if start_layer not in layers or end_layer not in layers:
            raise ValueError("Endpoint layers must be enabled on the saved board")
        obstacles.extend(native.obstacles)
        ignored_zones = native.ignored_zones
        existing_vias = native.vias
        source_hash = native.source_sha256
    if constraints is not None:
        eligible, excluded_layers = constraints.eligible_layers(layers, width, clearance)
        layers = list(eligible)
        if start_layer not in layers or end_layer not in layers:
            raise ValueError("An endpoint layer does not satisfy the supplied electrical limits")
        if snapshot is not None:
            constraints.check_configured_stackup(layers, dict(native.copper_thicknesses))
    for key in ("max_expansions", "max_cells"):
        if key in data and (
            isinstance(data[key], bool) or not isinstance(data[key], int)
        ):
            raise ValueError(f"{key} must be an integer")
    allow_new_vias = constraints is None or constraints.allows_via_geometry(size, drill)
    eligible_vias = (
        constraints.eligible_existing_vias(existing_vias, net)
        if constraints is not None
        else None
    )
    layer_penalties = _fields(
        data.get("layer_penalties", {}),
        set(COPPER_LAYERS),
        "layer penalties",
    )
    forbid_staircase = data.get("forbid_staircase", True)
    if not isinstance(forbid_staircase, bool):
        raise ValueError("forbid_staircase must be true or false")
    gentle_turns = data.get("gentle_turns", True)
    if not isinstance(gentle_turns, bool):
        raise ValueError("gentle_turns must be true or false")
    headings = {
        name: index
        for index, name in enumerate(("E", "SE", "S", "SW", "W", "NW", "N", "NE"))
    }

    def endpoint_directions(field: str) -> frozenset[int] | None:
        names = data.get(field)
        if names is None:
            return None
        if (
            not isinstance(names, list)
            or not names
            or any(not isinstance(name, str) or name not in headings for name in names)
        ):
            raise ValueError(f"{field} must list compass headings")
        return frozenset(headings[name] for name in names)

    initial_directions = endpoint_directions("initial_directions")
    final_directions = endpoint_directions("final_directions")
    enforce_endpoint_spacing = data.get("enforce_endpoint_spacing", False)
    if not isinstance(enforce_endpoint_spacing, bool):
        raise ValueError("enforce_endpoint_spacing must be true or false")
    optimizer = GridRouter(
        lower,
        upper,
        mm(data.get("step_mm", 0.25)),
        tuple(layers),
        net,
        width,
        clearance,
        size,
        drill,
        obstacles,
        _weights(data.get("weights", {})),
        _guide(data.get("symmetry_guide")),
        data.get("max_expansions", 200_000),
        data.get("max_cells", 1_000_000),
        allow_new_vias,
        eligible_vias,
        layer_penalties,
        forbid_staircase,
        mm(data.get("min_segment_length_mm", 0)),
        gentle_turns,
        initial_directions,
        final_directions,
        enforce_endpoint_spacing,
        data.get("topology_heuristic", False),
        data.get("heuristic_weight", 1.0),
        check_budget=check_budget,
        first_feasible=data.get("first_feasible", False),
    )
    route = optimizer.route(start, start_layer, end, end_layer, candidate_check=candidate_check)
    electrical = {
        "status": "not_assessed",
        "reason": "No electrical constraints supplied",
    }
    if constraints is not None:
        via_checks = constraints.check_vias(route, net, size, drill, existing_vias)
        voltage_drop = constraints.check_voltage_drop(route, width)
        electrical = constraints.assessment(
            tuple(layers), excluded_layers, via_checks, voltage_drop
        )
        electrical["configured_stackup"] = (
            "checked_against_nominal_saved_board"
            if snapshot_path is not None
            else "not_checked"
        )
    result: dict[str, Any] = {
        "status": "geometric_candidate_only",
        "net": net,
        "source_sha256": source_hash,
        "obstacle_count": len(obstacles),
        "zones_assumed_refilled": ignored_zones,
        "length_mm": round(route.length_mm, 6),
        "bends": route.bends,
        "via_count": len(route.vias),
        "reused_via_count": len(route.reused_vias),
        "cost": round(route.cost, 6),
        "expanded_states": route.expanded,
        "search_truncated": route.search_truncated,
        "topology_heuristic": optimizer.topology_heuristic,
        "heuristic_weight": optimizer.heuristic_weight,
        "optimality": "not_proven",
        "electrical": electrical,
        "layer_penalties": layer_penalties,
        "forbid_staircase": forbid_staircase,
        "min_segment_length_mm": data.get("min_segment_length_mm", 0),
        "gentle_turns": gentle_turns,
        "initial_directions": data.get("initial_directions"),
        "final_directions": data.get("final_directions"),
        "enforce_endpoint_spacing": enforce_endpoint_spacing,
        "first_feasible": optimizer.first_feasible,
        "segments": [
            {
                "from_mm": s.start.as_mm(),
                "to_mm": s.end.as_mm(),
                "layer": s.layer,
                "width_mm": to_mm(width),
            }
            for s in route.segments
        ],
        "vias": [
            {
                "at_mm": v.at.as_mm(),
                "from_layer": v.from_layer,
                "to_layer": v.to_layer,
                "size_mm": to_mm(size),
                "drill_mm": to_mm(drill),
            }
            for v in route.vias
        ],
        "reused_vias": [
            {"at_mm": v.at.as_mm(), "from_layer": v.from_layer, "to_layer": v.to_layer}
            for v in route.reused_vias
        ],
        "required_checks": [
            "Verify endpoints and net connectivity",
            "Run KiCad DRC with zone refill against the same saved board and rules",
            "Inspect reference copper and electrical suitability",
        ],
    }
    changes = (
        campaign_changes(route, net, width, size, drill, removals)
        if include_changes or validate
        else []
    )
    if validate:
        if snapshot_path is None or source_hash is None:
            raise ValueError("Native validation requires a saved board_snapshot")
        validation = validate_route(
            snapshot_path, source_hash, changes, net, start, start_layer, end, end_layer
        )
        changes.extend(validation.pop("_zone_refill_changes"))
        validation["electrical_assessments"] = [{"net": net, **electrical}]
        result["validation"] = validation
    if include_changes:
        result["unvalidated_campaign_changes"] = changes
    return result


def save_validated_campaign(
    project: Path,
    snapshot: str,
    name: str,
    evidence: dict[str, Any],
    source_sha256: str,
    changes: list[dict[str, Any]],
) -> dict[str, str]:
    """Save validated changes as a timestamped campaign without editing the PCB."""
    source = project / "v4.1.kicad_pcb" if snapshot == "pcb" else Path(snapshot)
    if not source.is_absolute():
        source = project / source
    if source.resolve() != (project / "v4.1.kicad_pcb").resolve():
        raise ValueError("Saved route campaigns must target this project's current PCB")
    if (
        not isinstance(source_sha256, str)
        or digest(source.read_bytes()) != source_sha256
    ):
        raise ValueError("The requested source hash must match the current PCB")
    verify_validation_evidence(source, evidence, evidence["candidate_sha256"])
    assessments = evidence.get("electrical_assessments")
    if (
        not isinstance(assessments, list)
        or not assessments
        or any(
            not isinstance(item, dict)
            or item.get("status") != "within_supplied_limits"
            or item.get("configured_stackup") != "checked_against_nominal_saved_board"
            for item in assessments
        )
    ):
        raise ValueError(
            "Saving a route campaign requires electrical limits and a checked stackup"
        )
    copper_nets = set()
    for change in changes:
        if change.get("op") == "insert" and change.get("target") == {"kind": "root"}:
            form = parse(change["expression"])
            if form.name in {"segment", "via"}:
                copper_nets.add(form.children("net")[0].atoms()[1].value)
    if not copper_nets <= {item.get("net") for item in assessments}:
        raise ValueError("Electrical assessments must cover every proposed net")
    campaign = CampaignFile(project).create(
        name, routing_validation=evidence, changes=changes
    )
    return {
        "file": str(campaign),
        "campaign_sha256": digest(campaign.read_bytes()),
        "source_sha256": source_sha256,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument(
        "--config", type=Path, required=True, help="JSON routing scenario"
    )
    parser.add_argument(
        "--include-campaign-changes",
        action="store_true",
        help="include unvalidated ordinary track/via insertions in the output; never write them",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="run KiCad DRC with refill and endpoint connectivity on temporary files",
    )
    parser.add_argument(
        "--save-campaign",
        metavar="NAME",
        help="create a timestamped campaign only after native validation passes",
    )
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("Routing configuration must be a JSON object")
        if args.save_campaign and not args.validate:
            raise ValueError("Saving a route campaign requires --validate")
        project = args.project.resolve()
        with quiet_duplicate_image_handlers():
            result = plan(
                data,
                project,
                include_changes=args.include_campaign_changes
                or bool(args.save_campaign),
                validate=args.validate,
            )
        if (
            args.save_campaign
            and result["electrical"]["status"] != "within_supplied_limits"
        ):
            raise ValueError(
                "Saving a route campaign requires supplied electrical limits"
            )
        if (
            args.save_campaign
            and result.get("validation", {}).get("status") == "no_new_drc_violations"
        ):
            result["campaign"] = save_validated_campaign(
                project,
                data["board_snapshot"],
                args.save_campaign,
                result["validation"],
                result["source_sha256"],
                result["unvalidated_campaign_changes"],
            )
            if not args.include_campaign_changes:
                del result["unvalidated_campaign_changes"]
        print(json.dumps(result, indent=2, allow_nan=False))
        return 2 if result.get("validation", {}).get("status") == "rejected" else 0
    except (
        OSError,
        ValueError,
        RuntimeError,
        NoGridRoute,
        SearchLimitReached,
    ) as error:
        print(f"Route planning error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
