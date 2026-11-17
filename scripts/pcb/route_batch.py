"""Plan a sequential route batch and perform one closing KiCad verification.

The JSON object contains optional shared route settings, one saved
``board_snapshot`` and a ``routes`` array. Each route overrides shared settings
and supplies its own net, start and end. Earlier proposals become conservative
obstacles for later routes. The route order matters; this is not a global
multi-net optimum. The PCB and schematic are never written by this command.

For example::

  {
    "board_snapshot": "pcb", "assume_zone_refill": true,
    "bounds_mm": [0, 0, 30, 20], "step_mm": 0.25,
    "layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"], "width_mm": 0.2,
    "routes": [
      {"name": "first", "net": "NET_A",
       "start": {"at_mm": [2, 2], "layer": "F.Cu"},
       "end": {"at_mm": [20, 10], "layer": "F.Cu"}},
      {"name": "second", "net": "NET_B",
       "start": {"at_mm": [2, 5], "layer": "F.Cu"},
       "end": {"at_mm": [20, 14], "layer": "F.Cu"}}
    ]
  }

Both endpoints of every route must touch pre-existing copper on its net when
using native validation. Route widths, via sizes, references and electrical
limits still need to be selected for the actual circuit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from pcb.route_cli import _endpoint, _fields, plan, save_validated_campaign
from pcb.route_optimizer import COPPER_LAYERS, NM_PER_MM, mm
from pcb.route_validation import RouteContract, validate_routes

SHARED_FIELDS = {
    "bounds_mm",
    "step_mm",
    "layers",
    "width_mm",
    "clearance_mm",
    "via",
    "weights",
    "symmetry_guide",
    "assume_zone_refill",
    "max_expansions",
    "max_cells",
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
}


def _number(value: int) -> str:
    return format(Decimal(value) / NM_PER_MM, "f")


def _previous_obstacles(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Envelope already-proposed tracks and vias without altering the PCB."""
    obstacles = []
    net = result["net"]
    for segment in result["segments"]:
        x1, y1 = (mm(value) for value in segment["from_mm"])
        x2, y2 = (mm(value) for value in segment["to_mm"])
        radius = math.ceil(mm(segment["width_mm"]) / 2) + 2
        # Short capsules exactly follow the track centerline. Two nanometres
        # cover integer interpolation and keep every piece conservative.
        pieces = max(1, math.ceil(max(abs(x2 - x1), abs(y2 - y1)) / 2_000_000))
        for index in range(pieces):
            ax, ay = x1 + (x2 - x1) * index // pieces, y1 + (y2 - y1) * index // pieces
            bx, by = (
                x1 + (x2 - x1) * (index + 1) // pieces,
                y1 + (y2 - y1) * (index + 1) // pieces,
            )
            obstacles.append(
                {
                    "bounds_mm": [
                        _number(min(ax, bx) - radius),
                        _number(min(ay, by) - radius),
                        _number(max(ax, bx) + radius),
                        _number(max(ay, by) + radius),
                    ],
                    "layers": [segment["layer"]],
                    "net": net,
                    "kind": "copper",
                    "centerline_mm": [[_number(ax), _number(ay)], [_number(bx), _number(by)]],
                    "radius_mm": _number(radius),
                }
            )
    for via in result["vias"]:
        x, y = (mm(value) for value in via["at_mm"])
        radius = math.ceil(mm(via["size_mm"]) / 2)
        obstacles.append(
            {
                "bounds_mm": [
                    _number(x - radius),
                    _number(y - radius),
                    _number(x + radius),
                    _number(y + radius),
                ],
                "layers": list(COPPER_LAYERS),
                "net": net,
                "kind": "via",
            }
        )
    return obstacles


def plan_batch(
    data: dict[str, Any],
    project: Path,
    *,
    include_changes: bool = False,
    validate: bool = False,
) -> dict[str, Any]:
    _fields(
        data,
        SHARED_FIELDS | {"board_snapshot", "routes", "obstacles"},
        "batch configuration",
    )
    requests = data.get("routes")
    if not isinstance(requests, list) or not requests:
        raise ValueError("A batch needs a nonempty routes array")
    snapshot = data.get("board_snapshot")
    if validate and (not isinstance(snapshot, str) or not snapshot):
        raise ValueError("Native batch validation requires a saved board_snapshot")
    shared_obstacles = data.get("obstacles", [])
    if not isinstance(shared_obstacles, list):
        raise ValueError("Shared obstacles must be an array")
    shared = {key: data[key] for key in SHARED_FIELDS if key in data}
    previous: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    contracts: list[RouteContract] = []
    source_hash = None
    for index, request in enumerate(requests, 1):
        if not isinstance(request, dict):
            raise ValueError("Each batch route must be an object")
        if "board_snapshot" in request:
            raise ValueError("Set one board_snapshot at batch level, not per route")
        local = dict(request)
        name = local.pop("name", f"route_{index}")
        if not isinstance(name, str) or not name:
            raise ValueError("Route names must be nonempty text")
        route_obstacles = local.pop("obstacles", [])
        if not isinstance(route_obstacles, list):
            raise ValueError("Route obstacles must be an array")
        config = {
            **shared,
            **local,
            "obstacles": [*shared_obstacles, *route_obstacles, *previous],
        }
        if snapshot is not None:
            config["board_snapshot"] = snapshot
        result = plan(config, project, include_changes=True)
        if index == 1:
            source_hash = result["source_sha256"]
        elif result["source_sha256"] != source_hash:
            raise RuntimeError("Saved PCB changed while planning the batch")
        changes.extend(result.pop("unvalidated_campaign_changes"))
        previous.extend(_previous_obstacles(result))
        result["name"] = name
        routes.append(result)
        start, start_layer = _endpoint(config.get("start"), "start")
        end, end_layer = _endpoint(config.get("end"), "end")
        contracts.append(
            RouteContract(
                config["net"],
                start,
                start_layer,
                end,
                end_layer,
                tuple(config.get("remove", [])),
            )
        )
    if len({route["name"] for route in routes}) != len(routes):
        raise ValueError("Route names must be unique within a batch")
    result: dict[str, Any] = {
        "status": "geometric_batch_only",
        "source_sha256": source_hash,
        "routes": routes,
        "total_length_mm": round(sum(route["length_mm"] for route in routes), 6),
        "total_bends": sum(route["bends"] for route in routes),
        "new_via_count": sum(route["via_count"] for route in routes),
        "note": "Sequential proposals require batch DRC, connectivity, reference-plane and electrical review.",
    }
    if snapshot is not None:
        source = project / "v4.1.kicad_pcb" if snapshot == "pcb" else Path(snapshot)
        if not source.is_absolute():
            source = project / source
        source = source.resolve()
        if (
            not source.is_file()
            or hashlib.sha256(source.read_bytes()).hexdigest() != source_hash
        ):
            raise RuntimeError("Saved PCB changed after batch planning")
        if validate:
            evidence = validate_routes(source, source_hash, changes, contracts)
            changes.extend(evidence.pop("_zone_refill_changes"))
            evidence["electrical_assessments"] = [
                {"net": route["net"], "name": route["name"], **route["electrical"]}
                for route in routes
            ]
            result["validation"] = evidence
    if include_changes:
        result["unvalidated_campaign_changes"] = changes
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument(
        "--config", type=Path, required=True, help="JSON batch configuration"
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="check the whole batch with KiCad on temporary copies",
    )
    parser.add_argument(
        "--include-campaign-changes",
        action="store_true",
        help="include ordinary copper changes in output without applying them",
    )
    parser.add_argument(
        "--save-campaign",
        metavar="NAME",
        help="save one timestamped campaign only after validation passes",
    )
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("Batch configuration must be a JSON object")
        if args.save_campaign and not args.validate:
            raise ValueError("Saving a route campaign requires --validate")
        project = args.project.resolve()
        result = plan_batch(
            data,
            project,
            include_changes=args.include_campaign_changes or bool(args.save_campaign),
            validate=args.validate,
        )
        if args.save_campaign and any(
            route["electrical"]["status"] != "within_supplied_limits"
            for route in result["routes"]
        ):
            raise ValueError(
                "Saving a route batch requires supplied electrical limits for every route"
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
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Route batch error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
