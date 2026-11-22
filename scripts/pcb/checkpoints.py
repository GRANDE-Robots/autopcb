"""Plan and validate source-bound, bounded-pin PCB routing checkpoints.

Planning reads a disposable, zone-refilled copy. Attempting a checkpoint never
applies PCB changes. A new campaign can be saved only after native validation
and explicitly supplied whole-net electrical limits pass. Search is bounded and
does not prove global optimality or physical manufacturing readiness.
"""

from __future__ import annotations

from core.project import native_path

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from core.paths import asset_root
from core.pcb_io import quiet_duplicate_image_handlers
from typing import Any, Callable

import pcbnew

from pcb.net_routing.backend import NativeBackend, Settings, route_payload
from pcb.net_routing.model import Budget, BudgetExhausted, Limits, Net, Site
from pcb.net_routing.native import Snapshot, discover, load_board, obstacles, sha
from pcb.net_routing.rework import ReworkWeights, junction_tail_cost
from pcb.net_routing.topology import whole_net
from pcb.route_batch import SHARED_FIELDS
from pcb.route_cli import _bounds as route_bounds, save_validated_campaign
from pcb.route_junctions import junction_changes
from pcb.route_optimizer import COPPER_LAYERS, Point, mm
from pcb.route_validation import RouteContract, _drc, validate_routes
from core.toolchain import kicad_cli
from core.compat import strict_zip

SCHEMA = 1


def _object(value: object, allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(f"Invalid {label} or unexpected fields")
    return value


def read_config(path: Path) -> tuple[dict[str, Any], str]:
    """Read an explicit policy without inventing electrical ratings."""
    raw = path.read_bytes()
    config = _object(
        json.loads(
            raw.decode("utf-8-sig"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"Non-finite JSON number: {value}")
            ),
        ),
        {
            "schema",
            "max_pins",
            "search_margin_mm",
            "include_nets",
            "exclude_nets",
            "priorities",
            "route",
            "net_overrides",
            "search",
            "max_nets",
            "max_checkpoints",
            "recovery",
            "rework",
            "zone_stitch",
            "working_scope",
        },
        "AutoPCB configuration",
    )
    if config.get("schema") != SCHEMA:
        raise ValueError("Unsupported AutoPCB configuration schema")
    scope = _object(config.get("working_scope", {}), {"kind", "classes", "manufacturing_approved"},
                    "working routing scope")
    if scope:
        if (scope.get("kind") != "connectivity" or scope.get("manufacturing_approved") is not False
                or not isinstance(scope.get("classes"), list) or not scope["classes"]
                or any(not isinstance(value, str) or not value for value in scope["classes"])
                or len(scope["classes"]) != len(set(scope["classes"]))):
            raise ValueError("Connectivity scope requires named classes and an explicit manufacturing hold")
    zone_stitch = _object(config.get("zone_stitch", {}), {"proofs"}, "zone-stitch settings")
    if not isinstance(zone_stitch.get("proofs", []), list):
        raise ValueError("zone-stitch proofs must be a list")
    maximum = config.get("max_pins")
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or not 1 <= maximum <= 20
    ):
        raise ValueError("max_pins must be an integer from 1 to 20")
    for name in ("include_nets", "exclude_nets"):
        values = config.get(name, [])
        if not isinstance(values, list) or any(
            not isinstance(n, str) or not n for n in values
        ):
            raise ValueError(f"{name} must be a list of net names")
        if len(values) != len(set(values)):
            raise ValueError(f"{name} cannot contain duplicates")
    priorities = config.get("priorities", {})
    if not isinstance(priorities, dict) or any(
        not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, int)
        for k, v in priorities.items()
    ):
        raise ValueError("priorities must map net names to integer ranks")
    route = _object(
        config.get("route", {}), SHARED_FIELDS | {"current_model"}, "route settings"
    )
    overrides = config.get("net_overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("net_overrides must be an object")
    for name, settings in overrides.items():
        if not isinstance(name, str) or not name:
            raise ValueError("Net override names must be nonempty strings")
        _object(settings, SHARED_FIELDS | {"current_model"}, "net override")
    layers = route.get("layers", list(COPPER_LAYERS))
    if (
        not isinstance(layers, list)
        or not layers
        or any(x not in COPPER_LAYERS for x in layers)
    ):
        raise ValueError("Route layers must list known copper layers")
    margin = config.get("search_margin_mm")
    if margin is not None and (
        isinstance(margin, bool)
        or not isinstance(margin, (int, float))
        or not math.isfinite(margin)
        or margin <= 0
    ):
        raise ValueError("search_margin_mm must be a positive finite number")
    Limits.from_config(config.get("search", {}))
    from pcb.net_routing.recovery import RecoveryPolicy
    RecoveryPolicy.from_config(config.get("recovery"))
    from pcb.net_routing.rework import ReworkWeights
    ReworkWeights.from_config(config.get("rework"))
    for name, default in (("max_nets", 1000), ("max_checkpoints", 5000)):
        value = config.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if sha(path) != hashlib.sha256(raw).hexdigest():
        raise RuntimeError("AutoPCB configuration changed while being read")
    return config, hashlib.sha256(raw).hexdigest()


def octile(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Obstacle-free octilinear distance; it is a heuristic, not a route."""
    dx = abs(a["position_mm"][0] - b["position_mm"][0])
    dy = abs(a["position_mm"][1] - b["position_mm"][1])
    return max(dx, dy) + (math.sqrt(2) - 1) * min(dx, dy)


def group_islands(
    islands: list[list[dict[str, Any]]], maximum: int
) -> list[dict[str, Any]]:
    """Greedily form small estimated connection trees, with no board mutation."""
    if maximum < 2 or any(not island for island in islands):
        raise ValueError(
            "Each island needs a terminal and the pin limit must be at least two"
        )
    groups = [list(island) for island in islands]
    output = []
    while len(groups) > 1:
        _, i, j, a, b = min(
            (octile(a, b), i, j, a["uuid"], b["uuid"])
            for i in range(len(groups))
            for j in range(i + 1, len(groups))
            for a in groups[i]
            for b in groups[j]
        )
        by_id = {terminal["uuid"]: terminal for group in groups for terminal in group}
        selected = [by_id[a], by_id[b]]
        chosen = [i, j]
        estimate = octile(selected[0], selected[1])
        while len(selected) < maximum and len(chosen) < len(groups):
            distance, index, _, terminal_id = min(
                (octile(existing, terminal), index, existing["uuid"], terminal["uuid"])
                for index, group in enumerate(groups)
                if index not in chosen
                for existing in selected
                for terminal in group
            )
            chosen.append(index)
            selected.append(by_id[terminal_id])
            estimate += distance
        output.append(
            {"terminals": selected, "estimated_length_mm": round(estimate, 6)}
        )
        merged = [terminal for index in chosen for terminal in groups[index]]
        groups = [group for index, group in enumerate(groups) if index not in chosen]
        groups.append(merged)
    return output


def single_pin_groups(
    native, terminals: list[list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Propose one pin to an already existing non-pad copper access site."""
    if len(terminals) != len(native.net.islands):
        raise ValueError("Each island must have a routable terminal")
    copper = [
        (index, site)
        for index, island in enumerate(native.net.islands)
        for site in island.sites
        if site not in native.net.pad_sites
    ]
    output = []
    for index, group in enumerate(terminals):
        options = []
        for other, site in copper:
            if other == index:
                continue
            position = [site.point.x / 1e6, site.point.y / 1e6]
            for terminal in group:
                options.append(
                    (
                        octile(terminal, {"position_mm": position}),
                        other,
                        site.key,
                        terminal["uuid"],
                        site,
                        terminal,
                    )
                )
        if not options:
            continue
        distance, other, _, _, site, terminal = min(options, key=lambda item: item[:4])
        output.append(
            {
                "terminals": [terminal],
                "estimated_length_mm": round(distance, 6),
                "anchor": {
                    "kind": "existing_copper",
                    "island_pad_uuid": native.net.islands[other].pad_ids[0],
                    "position_mm": [site.point.x / 1e6, site.point.y / 1e6],
                    "layer": site.layer,
                },
            }
        )
    return output


def _tools(snapshot: Snapshot) -> dict[str, str]:
    tools = dict(snapshot.tool_hashes)
    tools["pcb/checkpoints.py"] = sha(Path(__file__))
    return tools


def _inputs(snapshot: Snapshot) -> dict[str, str]:
    return {
        path.relative_to(snapshot.source.parent).as_posix(): digest
        for path, digest in snapshot.fingerprints.items()
    }


def _bounds(board) -> list[float]:
    box = board.GetBoardEdgesBoundingBox()
    if box.GetWidth() <= 0 or box.GetHeight() <= 0:
        raise ValueError("A valid board outline is required")
    return [
        box.GetX() / 1e6,
        box.GetY() / 1e6,
        box.GetRight() / 1e6,
        box.GetBottom() / 1e6,
    ]


def local_bounds(
    board_bounds: list[float],
    terminals: list[dict[str, Any]],
    anchor: dict[str, Any] | None,
    margin: float | None,
) -> list[float]:
    """Limit a search to the terminals plus margin, clipped to the board box."""
    if margin is None:
        return board_bounds
    points = [Point.from_mm(item.get("position_mm")) for item in terminals]
    if anchor is not None:
        points.append(Point.from_mm(anchor.get("position_mm")))
    if not points:
        raise ValueError("A local search requires at least one terminal")
    low, high = Point.from_mm(board_bounds[:2]), Point.from_mm(board_bounds[2:])
    if any(p.x < low.x or p.x > high.x or p.y < low.y or p.y > high.y for p in points):
        raise ValueError("A checkpoint terminal lies outside the board bounds")
    margin_nm = mm(margin)
    if margin_nm <= 0:
        raise ValueError("Local search margin must be at least one nanometre")
    return [
        max(low.x, min(p.x for p in points) - margin_nm) / 1e6,
        max(low.y, min(p.y for p in points) - margin_nm) / 1e6,
        min(high.x, max(p.x for p in points) + margin_nm) / 1e6,
        min(high.y, max(p.y for p in points) + margin_nm) / 1e6,
    ]


def fit_local_bounds(
    board_bounds: list[float],
    terminals: list[dict[str, Any]],
    anchor: dict[str, Any] | None,
    margin: float | None,
    route: dict[str, Any],
) -> tuple[list[float], dict[str, Any]]:
    """Fit an automatic local window to the cell limit without coarsening it."""
    step = mm(route.get("step_mm", 0.25))
    width = mm(route.get("width_mm", 0.2))
    clearance = mm(route.get("clearance_mm", 0.2))
    via = route.get("via", {})
    if not isinstance(via, dict):
        raise ValueError("via must be an object")
    via_size = mm(via.get("size_mm", 0.6))
    layers = route.get("layers", list(COPPER_LAYERS))
    maximum = route.get("max_cells", 250_000)
    if (
        step <= 0
        or width <= 0
        or clearance < 0
        or via_size <= 0
        or not isinstance(layers, list)
        or not layers
        or any(layer not in COPPER_LAYERS for layer in layers)
        or len(set(layers)) != len(layers)
        or isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or maximum < 1
    ):
        raise ValueError("Invalid route settings for the search window")

    def cell_count(bounds: list[float]) -> int:
        low, high = Point.from_mm(bounds[:2]), Point.from_mm(bounds[2:])
        return (
            ((high.x - low.x) // step + 1)
            * ((high.y - low.y) // step + 1)
            * len(layers)
        )

    bounds = local_bounds(board_bounds, terminals, anchor, margin)
    count = cell_count(bounds)
    info = {
        "requested_margin_mm": margin,
        "margin_mm": margin,
        "reduced": False,
        "cells": count,
    }
    if count <= maximum:
        return bounds, info
    if margin is None:
        raise ValueError("Search window exceeds max_cells; set a local search margin")
    requested = mm(margin)
    # Leave space for a via or track plus one grid step around each endpoint.
    minimum = clearance + (max(width, via_size) + 1) // 2 + step
    if (
        minimum >= requested
        or cell_count(local_bounds(board_bounds, terminals, anchor, minimum / 1e6))
        > maximum
    ):
        raise ValueError(
            "Endpoints cannot fit within max_cells at the configured grid step"
        )
    low, high = minimum, requested
    while low < high:
        middle = (low + high + 1) // 2
        if (
            cell_count(local_bounds(board_bounds, terminals, anchor, middle / 1e6))
            <= maximum
        ):
            low = middle
        else:
            high = middle - 1
    bounds = local_bounds(board_bounds, terminals, anchor, low / 1e6)
    return bounds, {
        "requested_margin_mm": margin,
        "margin_mm": low / 1e6,
        "reduced": True,
        "cells": cell_count(bounds),
    }


def _terminals(board, native, layers: tuple[str, ...]) -> list[list[dict[str, Any]]]:
    pads = {pad.m_Uuid.AsString(): pad for pad in board.GetPads()}
    output = []
    for island in native.net.islands:
        group = []
        for uid in island.pad_ids:
            pad = pads[uid]
            layer = next(
                (layer for layer in layers if pad.IsOnLayer(board.GetLayerID(layer))),
                None,
            )
            if layer is None:
                raise ValueError(f"Pad {uid} has no access on configured layers")
            pos = pad.GetPosition()
            group.append(
                {
                    "uuid": uid,
                    "reference": pad.GetParentFootprint().GetReference(),
                    "pin": pad.GetNumber(),
                    "position_mm": [pos.x / 1e6, pos.y / 1e6],
                    "layer": layer,
                }
            )
        if group:
            output.append(group)
    return output


def create_plan(
    project: Path, config_path: Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build an adaptive-order starting plan; no routes are searched or applied."""
    project, config_path = project.resolve(), config_path.resolve()
    if not config_path.is_relative_to(project) or not config_path.is_file():
        raise ValueError("Configuration must be a file inside the project")
    config, config_hash = read_config(config_path)
    source = native_path(project, ".kicad_pcb")
    limits = Limits.from_config(config.get("search", {}))
    budget = Budget(limits)
    plan_items, skipped = [], {}
    with Snapshot(source) as snapshot:
        # Refill only the disposable copy so ground and plane connectivity are real.
        cli = kicad_cli()
        baseline_drc = _drc(
            cli,
            snapshot.board_path,
            snapshot.directory / "planning-drc.json",
            save_refill=True,
        )
        board = pcbnew.LoadBoard(str(snapshot.board_path))
        if board is None:
            raise RuntimeError("KiCad could not load the temporary planning board")
        board.GetConnectivity().Build(board)
        bounds = _bounds(board)
        nets = sorted(
            {
                pad.GetNetname()
                for pad in board.GetPads()
                if pad.GetNetname() and not pad.GetNetname().startswith("unconnected-")
            }
        )
        included, excluded = (
            set(config.get("include_nets", [])),
            set(config.get("exclude_nets", [])),
        )
        missing = sorted(included - set(nets))
        if missing:
            raise ValueError(f"Configured nets are absent from the board: {missing}")
        names = [
            name
            for name in nets
            if (not included or name in included) and name not in excluded
        ]
        names.sort(key=lambda name: (config.get("priorities", {}).get(name, 100), name))
        if len(names) > config.get("max_nets", 1000):
            skipped["net_limit"] = names[config.get("max_nets", 1000) :]
            names = names[: config.get("max_nets", 1000)]
        for name in names:
            try:
                budget.check()
                route = {
                    **config.get("route", {}),
                    **config.get("net_overrides", {}).get(name, {}),
                }
                layers = tuple(route.get("layers", list(COPPER_LAYERS)))
                native = discover(board, name, layers, budget)
                groups = _terminals(board, native, layers)
                if len(groups) <= 1:
                    items = ()
                elif config["max_pins"] == 1:
                    items = single_pin_groups(native, groups)
                    if not items:
                        skipped[name] = (
                            "No existing non-pad copper anchor for a one-pin checkpoint"
                        )
                else:
                    items = group_islands(groups, config["max_pins"])
                for item in items:
                    item_route = dict(route)
                    window = None
                    if "bounds_mm" not in item_route:
                        item_route["bounds_mm"], window = fit_local_bounds(
                            bounds,
                            item["terminals"],
                            item.get("anchor"),
                            config.get("search_margin_mm"),
                            item_route,
                        )
                    plan_items.append(
                        {
                            "net": name,
                            "priority": config.get("priorities", {}).get(name, 100),
                            "route": item_route,
                            **({"search_window": window} if window is not None else {}),
                            **item,
                        }
                    )
            except BudgetExhausted:
                skipped["time_limit"] = names[names.index(name) :]
                break
            except ValueError as exc:
                skipped[name] = str(exc)
        plan_items.sort(
            key=lambda item: (
                item["priority"],
                item["estimated_length_mm"],
                item["net"],
            )
        )
        maximum = config.get("max_checkpoints", 5000)
        if len(plan_items) > maximum:
            skipped["checkpoint_limit"] = len(plan_items) - maximum
            plan_items = plan_items[:maximum]
        fingerprints = _inputs(snapshot)
        tools = _tools(snapshot)
        source_hash = snapshot.fingerprints[source.resolve()]
        if sha(config_path) != config_hash:
            raise RuntimeError("AutoPCB configuration changed during planning")
        snapshot.check()
        if tools != _tools(snapshot):
            raise RuntimeError("Checkpoint tooling changed during planning")
    checkpoints = []
    for index, item in enumerate(plan_items, 1):
        checkpoints.append(
            {
                "schema": SCHEMA,
                "kind": "autopcb_checkpoint",
                "number": index,
                "status": "planned",
                "source_sha256": source_hash,
                "input_sha256": fingerprints,
                "tool_sha256": tools,
                "config_path": config_path.relative_to(project).as_posix(),
                "config_sha256": config_hash,
                "max_pins": config["max_pins"],
                "search": config.get("search", {}),
                **item,
            }
        )
    plan = {
        "schema": SCHEMA,
        "source_sha256": source_hash,
        "input_sha256": fingerprints,
        "tool_sha256": tools,
        "config_path": config_path.relative_to(project).as_posix(),
        "config_sha256": config_hash,
        "max_pins": config["max_pins"],
        "checkpoints": len(checkpoints),
        "baseline_unconnected": len(baseline_drc["unconnected_items"]),
        "baseline_violations": len(baseline_drc["violations"]),
        "excluded_nets": sorted(excluded),
        "skipped": skipped,
        "order_method": "priority_then_octilinear_heuristic",
        "optimality": "not_proven",
        "estimated_seconds": None,
        "limitations": [
            "Later checkpoints are tentative and must be replanned after any board change.",
            "Estimated distance ignores obstacles and is not actual route length or time.",
            "A bounded local search can miss routes that detour outside its window.",
            "Electrical limits and manufacturing readiness are not established by planning.",
        ],
    }
    return plan, checkpoints


def save_plan(
    project: Path, plan: dict[str, Any], checkpoints: list[dict[str, Any]]
) -> Path:
    """Write a new plan directory; never overwrite a previous run."""
    root = asset_root(project) / "results" / "autopcb"
    root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    destination = root / run_id
    with Snapshot(native_path(project, ".kicad_pcb")) as snapshot:
        if (
            _inputs(snapshot) != plan["input_sha256"]
            or _tools(snapshot) != plan["tool_sha256"]
        ):
            raise RuntimeError("Sources or tools changed before saving the plan")
        config = (project / plan["config_path"]).resolve()
        if (
            not config.is_relative_to(project.resolve())
            or sha(config) != plan["config_sha256"]
        ):
            raise RuntimeError("Configuration changed before saving the plan")
        with tempfile.TemporaryDirectory(prefix=".plan-", dir=root) as temp:
            staging = Path(temp)
            (staging / "checkpoints").mkdir()
            names = []
            for item in checkpoints:
                name = f"checkpoints/{item['number']:04d}.json"
                (staging / name).write_text(
                    json.dumps(item, indent=2) + "\n", encoding="utf-8"
                )
                names.append(name)
            (staging / "plan.json").write_text(
                json.dumps({**plan, "checkpoint_files": names}, indent=2) + "\n",
                encoding="utf-8",
            )
            if destination.exists():
                raise FileExistsError(f"Plan already exists: {destination}")
            snapshot.check()
            if (
                _tools(snapshot) != plan["tool_sha256"]
                or sha(config) != plan["config_sha256"]
            ):
                raise RuntimeError(
                    "Tools or configuration changed before saving the plan"
                )
            os.rename(staging, destination)
    return destination / "plan.json"


def attempt(
    project: Path,
    checkpoint_path: Path,
    *,
    save_campaign: bool = False,
    include_preview_changes: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Search and validate one new checkpoint on copies, never applying the PCB."""
    project, checkpoint_path = project.resolve(), checkpoint_path.resolve()
    root = (asset_root(project) / "results" / "autopcb").resolve()
    if not checkpoint_path.is_relative_to(root) or not checkpoint_path.is_file():
        raise ValueError("Checkpoint must be a saved file under results/autopcb")
    checkpoint_bytes = checkpoint_path.read_bytes()
    checkpoint = json.loads(checkpoint_bytes.decode("utf-8-sig"))
    if (
        checkpoint.get("schema") != SCHEMA
        or checkpoint.get("kind") != "autopcb_checkpoint"
    ):
        raise ValueError("Unsupported checkpoint file")
    terminals, maximum = checkpoint.get("terminals"), checkpoint.get("max_pins")
    if (
        not isinstance(terminals, list)
        or isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or not 1 <= maximum <= 20
    ):
        raise ValueError("Checkpoint terminals and pin limit are required")
    ids = [item.get("uuid") for item in terminals if isinstance(item, dict)]
    if (
        len(ids) != len(terminals)
        or any(not isinstance(uid, str) or not uid for uid in ids)
        or len(set(ids)) != len(ids)
        or not 1 <= len(ids) <= maximum
    ):
        raise ValueError(
            "Checkpoint must select distinct pad UUIDs within its pin limit"
        )
    anchor = checkpoint.get("anchor")
    if len(ids) == 1 and (
        not isinstance(anchor, dict) or anchor.get("kind") != "existing_copper"
    ):
        raise ValueError("A one-pin checkpoint needs an existing copper anchor")
    if len(ids) > 1 and anchor is not None:
        raise ValueError("Copper anchors are only used for one-pin checkpoints")
    name = checkpoint.get("net")
    if not isinstance(name, str) or not name:
        raise ValueError("Checkpoint net name is missing")
    route = _object(
        checkpoint.get("route"), SHARED_FIELDS | {"current_model"}, "route settings"
    )
    limits = Limits.from_config(checkpoint.get("search", {}))
    budget = Budget(limits)
    if progress is not None:
        progress(
            f"Searching {name} on a temporary board (budget: {limits.seconds:g} seconds)."
        )
    source = native_path(project, ".kicad_pcb")
    config_name = checkpoint.get("config_path")
    if not isinstance(config_name, str) or not config_name:
        raise ValueError("Checkpoint configuration path is missing")
    config = (project / config_name).resolve()
    if (
        not config.is_relative_to(project)
        or not config.is_file()
        or sha(config) != checkpoint.get("config_sha256")
    ):
        raise RuntimeError("Checkpoint configuration changed; create a fresh plan")
    policy, _ = read_config(config)
    if (
        maximum != policy["max_pins"]
        or checkpoint.get("search") != policy.get("search", {})
        or name in policy.get("exclude_nets", [])
        or (policy.get("include_nets") and name not in policy["include_nets"])
    ):
        raise ValueError("Checkpoint does not match its saved configuration")
    with Snapshot(source) as snapshot:
        if (
            _inputs(snapshot) != checkpoint.get("input_sha256")
            or _tools(snapshot) != checkpoint.get("tool_sha256")
            or snapshot.fingerprints[source] != checkpoint.get("source_sha256")
        ):
            raise RuntimeError(
                "Board, design inputs or tools changed; create a fresh plan"
            )
        board = load_board(snapshot.board_path)
        layers = tuple(route.get("layers", list(COPPER_LAYERS)))
        native = discover(board, name, layers, budget)
        island_indexes = {
            uid: index
            for index, island in enumerate(native.net.islands)
            for uid in island.pad_ids
        }
        if any(uid not in island_indexes for uid in ids):
            raise ValueError("A selected pad is missing from the requested net")
        indexes = sorted({island_indexes[uid] for uid in ids})
        if len(ids) == 1:
            anchor_id = anchor.get("island_pad_uuid")
            if not isinstance(anchor_id, str) or anchor_id not in island_indexes:
                raise ValueError(
                    "Copper anchor island is missing from the requested net"
                )
            indexes = sorted(set(indexes) | {island_indexes[anchor_id]})
        if len(indexes) < 2:
            snapshot.check()
            return {"status": "already_connected", "applied": False}
        pads = {pad.m_Uuid.AsString(): pad for pad in board.GetPads()}
        anchors = []
        for terminal in terminals:
            pad = pads[terminal["uuid"]]
            if (
                pad.GetNetname() != name
                or pad.GetParentFootprint().GetReference() != terminal["reference"]
                or pad.GetNumber() != terminal["pin"]
            ):
                raise ValueError(
                    "A selected terminal no longer matches its recorded identity"
                )
            layer = terminal["layer"]
            if layer not in layers or not pad.IsOnLayer(board.GetLayerID(layer)):
                raise ValueError(
                    "A selected terminal has no access on its recorded layer"
                )
            pos = pad.GetPosition()
            anchors.append((Point(int(pos.x), int(pos.y)), layer))
        access: dict[int, list[Site]] = {}
        for uid, (point, layer) in strict_zip(ids, anchors):
            # A plated-through selected pin can be reached on every copper
            # layer it physically occupies. These are still one selected pin,
            # and the route contract remains anchored to that original pad.
            pad = pads[uid]
            allowed = [layer] + [
                candidate
                for candidate in layers
                if candidate != layer and pad.IsOnLayer(board.GetLayerID(candidate))
            ]
            access.setdefault(island_indexes[uid], []).extend(
                Site(point, candidate) for candidate in allowed
            )
        if len(ids) == 1:
            point = Point.from_mm(anchor.get("position_mm"))
            site = Site(point, anchor.get("layer"))
            anchor_index = island_indexes[anchor_id]
            if (
                site not in native.net.islands[anchor_index].sites
                or site in native.net.pad_sites
            ):
                raise ValueError(
                    "Copper anchor is no longer an existing non-pad access site"
                )
            access.setdefault(anchor_index, []).append(site)
            anchors.insert(0, (point, site.layer))
        contracts = [
            RouteContract(name, anchors[0][0], anchors[0][1], p, layer)
            for p, layer in anchors[1:]
        ]
        bounds = board.GetBoardEdgesBoundingBox()
        board_bounds = (
            Point(bounds.GetX(), bounds.GetY()),
            Point(bounds.GetRight(), bounds.GetBottom()),
        )
        configured_route = {
            **policy.get("route", {}),
            **policy.get("net_overrides", {}).get(name, {}),
        }
        window = None
        if "bounds_mm" not in configured_route:
            configured_route["bounds_mm"], window = fit_local_bounds(
                _bounds(board),
                terminals,
                anchor,
                policy.get("search_margin_mm"),
                configured_route,
            )
        if checkpoint.get("search_window") != window:
            raise ValueError(
                "Checkpoint search window differs from its saved configuration"
            )
        if route != configured_route:
            raise ValueError(
                "Checkpoint route settings differ from its saved configuration"
            )
        lower, upper = route_bounds(configured_route.get("bounds_mm"))
        selected_islands = []
        for index in indexes:
            chosen = tuple(dict.fromkeys(access[index]))
            other_points = [
                site.point
                for other, sites in access.items()
                if other != index
                for site in sites
            ]
            available = (
                site
                for site in native.net.islands[index].sites
                if site not in native.net.pad_sites
                and site not in chosen
                and lower.x <= site.point.x <= upper.x
                and lower.y <= site.point.y <= upper.y
            )
            extra = sorted(
                available,
                key=lambda site: (
                    min(
                        (site.point.x - p.x) ** 2 + (site.point.y - p.y) ** 2
                        for p in other_points
                    ),
                    site.key,
                ),
            )
            remaining = max(0, limits.sites_per_island - len(chosen))
            selected_islands.append(
                replace(
                    native.net.islands[index], sites=chosen + tuple(extra[:remaining])
                )
            )
        selected = replace(
            native,
            net=Net(
                name, tuple(selected_islands), native.net.existing, native.net.pad_sites
            ),
        )
        edge_clearance = int(board.GetDesignSettings().m_CopperEdgeClearance)
        native_obstacles = obstacles(
            snapshot.board_path, name, route.get("assume_zone_refill", False)
        )
        settings = Settings(
            route,
            selected,
            dict(native_obstacles.copper_thicknesses),
            board_bounds,
            edge_clearance,
        )
        settings.router(())
        backend = NativeBackend({name: settings}, native_obstacles.obstacles, budget)
        candidates = whole_net(selected.net, (), backend, budget)
        variants = []
        for candidate in candidates:
            refined = backend.refine_complete(selected.net, (), candidate.route)
            if (
                refined.segments != candidate.route.segments
                and refined.cost + 1e-9 < candidate.route.cost
                and backend.accept(selected.net, refined)
            ):
                variants.append(replace(candidate, route=refined))
            # Keep the original as a fallback if KiCad rejects a refined path.
            variants.append(candidate)
        budget.finish_search()
        output: dict[str, Any] = {
            "status": "no_validated_candidate_found",
            "applied": False,
            "search": budget.report(),
            "optimality": "not_proven",
        }
        if window is not None:
            output["search_window"] = window
        rework_weights = ReworkWeights.from_config(policy.get("rework"))
        materialized = []
        for candidate in variants:
            pruned_tails = []
            try:
                changes, removals = junction_changes(
                    board,
                    snapshot.original,
                    candidate.route,
                    name,
                    settings.width,
                    settings.size,
                    settings.drill,
                    pruned_tails=pruned_tails,
                    protected_sites=tuple(
                        site for contract in contracts if contract.net == name
                        for site in ((contract.start, contract.start_layer),
                                     (contract.end, contract.end_layer))
                    ),
                )
            except ValueError:
                budget.failure("unsupported_junction")
                continue
            if changes:
                rework_cost = junction_tail_cost(pruned_tails, rework_weights)
                materialized.append(
                    (candidate.route.cost + rework_cost, candidate, changes, removals, rework_cost)
                )
        materialized.sort(key=lambda entry: entry[0])
        for _, candidate, changes, removals, rework_cost in materialized[:limits.validation_candidates]:
            candidate_contracts = list(contracts)
            if removals:
                if len(ids) == 1:
                    # The selected branch anchor may itself be split. Bind the
                    # contract to its surviving island pad rather than to a
                    # track UUID intentionally removed by this candidate.
                    original_pad = pads[anchor_id]
                    pad_layer = next(
                        layer
                        for layer in COPPER_LAYERS
                        if original_pad.IsOnLayer(board.GetLayerID(layer))
                    )
                    pos = original_pad.GetPosition()
                    candidate_contracts[0] = replace(
                        candidate_contracts[0],
                        start=Point(int(pos.x), int(pos.y)),
                        start_layer=pad_layer,
                    )
                candidate_contracts[0] = replace(
                    candidate_contracts[0], removals=tuple(removals)
                )
            if progress is not None:
                progress(
                    f"Validating a {name} candidate with KiCad; this may take a while."
                )
            evidence = validate_routes(
                source, checkpoint["source_sha256"], changes, candidate_contracts
            )
            if (
                evidence["status"] != "no_new_drc_violations"
                or evidence["candidate_unconnected"] >= evidence["baseline_unconnected"]
            ):
                continue
            replay_changes = list(changes) if include_preview_changes else None
            changes.extend(evidence.pop("_zone_refill_changes", []))
            electrical = settings.assessment(candidate.route)
            evidence["electrical_assessments"] = [{"net": name, **electrical}]
            output.update(
                status="validated_candidate",
                route=route_payload(
                    candidate.route, name, settings.width, settings.size, settings.drill
                ),
                electrical=electrical,
                rework_cost=rework_cost,
                cost=candidate.route.cost + rework_cost,
                validation={
                    key: evidence[key]
                    for key in (
                        "status",
                        "baseline_unconnected",
                        "candidate_unconnected",
                        "baseline_violations",
                        "candidate_violations",
                        "new_violations",
                        "broken_existing_connections",
                        "candidate_sha256",
                    )
                },
            )
            if include_preview_changes:
                output["_preview_changes"] = changes
                output["_preview_replay"] = {
                    "changes": replay_changes,
                    "contracts": [
                        {
                            "net": contract.net,
                            "start": contract.start.as_mm(),
                            "start_layer": contract.start_layer,
                            "end": contract.end.as_mm(),
                            "end_layer": contract.end_layer,
                            "removals": list(contract.removals),
                        }
                        for contract in candidate_contracts
                    ],
                }
            if save_campaign:
                if electrical["status"] != "within_supplied_limits":
                    raise ValueError(
                        "Saving a campaign requires supplied whole-net electrical limits"
                    )
                if (
                    checkpoint_path.read_bytes() != checkpoint_bytes
                    or sha(config) != checkpoint["config_sha256"]
                ):
                    raise RuntimeError(
                        "Checkpoint or configuration changed during search"
                    )
                snapshot.check()
                if _tools(snapshot) != checkpoint["tool_sha256"]:
                    raise RuntimeError("Checkpoint tooling changed during search")
                output["campaign"] = save_validated_campaign(
                    project,
                    "pcb",
                    f"AutoPCB checkpoint {checkpoint['number']}: {name}",
                    evidence,
                    checkpoint["source_sha256"],
                    changes,
                )
            break
        output["search"] = budget.report()
        if (
            checkpoint_path.read_bytes() != checkpoint_bytes
            or sha(config) != checkpoint["config_sha256"]
        ):
            raise RuntimeError("Checkpoint or configuration changed during search")
        snapshot.check()
        if _tools(snapshot) != checkpoint["tool_sha256"]:
            raise RuntimeError("Checkpoint tooling changed during search")
    return output


def record_attempt(path: Path, original: bytes, result: dict[str, Any]) -> None:
    """Record an attempt only if the checkpoint has not been edited meanwhile."""
    data = json.loads(original.decode("utf-8-sig"))
    data["last_attempt"] = result
    payload = (json.dumps(data, indent=2) + "\n").encode("utf-8")
    fd, name = tempfile.mkstemp(prefix=".checkpoint-", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        temporary.write_bytes(payload)
        if path.read_bytes() != original:
            raise RuntimeError("Checkpoint changed before recording the attempt")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    plan = actions.add_parser("plan", help="create a new source-bound checkpoint plan")
    plan.add_argument("--config", type=Path, default=Path("config/autopcb.json"))
    batch = actions.add_parser(
        "run", help="plan and preview checkpoints on board copies"
    )
    batch.add_argument("--config", type=Path, default=Path("config/autopcb.json"))
    batch.add_argument("--limit", type=int, help="try at most this many checkpoints")
    batch.add_argument(
        "--sequential",
        action="store_true",
        help="carry accepted previews forward on one temporary board",
    )
    batch.add_argument("--resume", type=Path, help="resume a saved sequential preview")
    batch.add_argument(
        "--seed", type=Path, help="preview a validated enabling routing change first"
    )
    run = actions.add_parser("attempt", help="try one checkpoint on temporary copies")
    run.add_argument("checkpoint", type=Path)
    run.add_argument("--save-campaign", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "run" and args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer")
    if args.action == "run" and args.resume is not None and not args.sequential:
        parser.error("--resume requires --sequential")
    if args.action == "run" and args.seed is not None and not args.sequential:
        parser.error("--seed requires --sequential")
    if args.action == "run" and args.seed is not None and args.resume is not None:
        parser.error("a resumed preview already contains its seed")
    project = args.project.resolve()

    def report(message: str) -> None:
        print(f"AutoPCB: {message}", file=sys.stderr, flush=True)

    try:
        if args.action == "run" and args.sequential:
            from pcb.net_routing.sequence import preview_sequence

            config = (project / args.config).resolve()
            if not config.is_file():
                config = (asset_root(project) / args.config).resolve()
            with quiet_duplicate_image_handlers():
                result = preview_sequence(
                    project,
                    config,
                    max_attempts=args.limit,
                    resume=(project / args.resume).resolve() if args.resume else None,
                    seed=(project / args.seed).resolve() if args.seed else None,
                    progress=report,
                )
        elif args.action in {"plan", "run"}:
            config = (project / args.config).resolve()
            if not config.is_file():
                config = (asset_root(project) / args.config).resolve()
            with quiet_duplicate_image_handlers():
                plan_data, checkpoints = create_plan(project, config)
                destination = save_plan(project, plan_data, checkpoints)
            if args.action == "plan":
                result = {
                    "status": "planned",
                    "plan": str(destination),
                    "checkpoints": len(checkpoints),
                    "skipped": plan_data["skipped"],
                    "optimality": "not_proven",
                    "applied": False,
                }
            else:
                selected = checkpoints[: args.limit] if args.limit else checkpoints
                summaries = []
                for index, item in enumerate(selected, 1):
                    path = (
                        destination.parent
                        / "checkpoints"
                        / f"{item['number']:04d}.json"
                    )
                    original = path.read_bytes()
                    report(f"Checkpoint {index}/{len(selected)}: {item['net']}.")
                    with quiet_duplicate_image_handlers():
                        trial = attempt(project, path, progress=report)
                    record_attempt(path, original, trial)
                    summary = {
                        "number": item["number"],
                        "net": item["net"],
                        "file": str(path),
                        "status": trial["status"],
                        "electrical": trial.get("electrical", {}).get("status"),
                        "limits_hit": trial.get("search", {}).get("limits_hit", []),
                        "search_failures": trial.get("search", {}).get("failures", {}),
                    }
                    if trial.get("search_window", {}).get("reduced"):
                        summary["search_window"] = trial["search_window"]
                    if "validation" in trial:
                        summary["unconnected_before"] = trial["validation"][
                            "baseline_unconnected"
                        ]
                        summary["unconnected_after"] = trial["validation"][
                            "candidate_unconnected"
                        ]
                    summaries.append(summary)
                    report(f"{item['net']}: {trial['status']}.")
                result = {
                    "status": "preview_completed",
                    "plan": str(destination),
                    "attempted": len(summaries),
                    "remaining": len(checkpoints) - len(summaries),
                    "checkpoints": summaries,
                    "skipped": plan_data["skipped"],
                    "optimality": "not_proven",
                    "applied": False,
                }
        else:
            path = (project / args.checkpoint).resolve()
            if not path.is_file():
                path = (asset_root(project) / args.checkpoint).resolve()
            original = path.read_bytes()
            with quiet_duplicate_image_handlers():
                result = attempt(
                    project,
                    path,
                    save_campaign=args.save_campaign,
                    progress=report,
                )
            record_attempt(path, original, result)
        print(json.dumps(result, separators=(",", ":"), allow_nan=False))
        return (
            0
            if result["status"]
            in {
                "planned",
                "preview_completed",
                "preview_paused",
                "already_connected",
                "validated_candidate",
            }
            else 2
        )
    except KeyboardInterrupt:
        print("AutoPCB stopped; no PCB changes were applied.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"AutoPCB checkpoints: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
