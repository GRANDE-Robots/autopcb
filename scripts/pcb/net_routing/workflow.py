"""Compose native snapshots, placement sampling, net search and final checks."""

from __future__ import annotations

from core.project import native_path

from dataclasses import dataclass, replace
from contextlib import nullcontext
import math
from pathlib import Path

from pcb.route_batch import SHARED_FIELDS
from pcb.route_cli import _fields
from core.kicad_campaign import CampaignPlanner
from pcb.route_junctions import junction_changes
from pcb.route_native import validate_removals
from pcb.route_optimizer import COPPER_LAYERS, Point, mm
from pcb.move_scope import MoveScope
from pcb.route_validation import CandidateRejected, RouteContract, validate_routes
from pcb.net_routing.checkpoint import (
    CheckpointWindowUnavailable,
    route_access_inset,
    search_window,
    choose_checkpoint,
    choose_transaction_checkpoint,
    original_anchor,
)
from pcb.net_routing.backend import (
    NativeBackend,
    Settings,
    plan_dimensions,
    route_payload,
)
from pcb.net_routing.model import Budget, BudgetExhausted, Limits
from pcb.net_routing.native import (
    Snapshot,
    contracts,
    discover,
    load_board,
    obstacles,
    placements,
)
from pcb.net_routing.schedule import BoardPlan, search_orders
from pcb.net_routing.topology import whole_net
from pcb.net_routing.rework import (
    RemovedCopper,
    ReworkWeights,
    capture_removals,
    include_repair_contacts,
    junction_tail_cost,
    placement_cost,
)
from pcb.net_routing.rules import RoutingRules


@dataclass
class Candidate:
    board: BoardPlan
    poses: dict
    moves: tuple[dict, ...]
    settings: dict[str, Settings]
    contracts: tuple[RouteContract, ...]
    placement_cost: float = 0.0
    junction_rework_cost: float = 0.0

    @property
    def cost(self) -> float:
        return self.board.cost + self.placement_cost + self.junction_rework_cost


def _solve_with_terminal_refinement(
    net, prior, backend, budget, removed=None, weights=None, *, source_unchanged=False
):
    """Keep a greedy tree when local cleanup cannot improve or validate it."""
    variants = []
    setting = backend.settings[net.name]
    copper = removed or RemovedCopper()
    weights = weights or ReworkWeights()

    def rework_cost(route):
        return copper.score(
            route,
            width=setting.width,
            size=setting.size,
            drill=setting.drill,
            weights=weights,
            segment_dimensions=setting.profile.pieces if setting.profile else None,
        ).cost

    restored = backend.restored_plan(
        net, prior, removed, source_unchanged=source_unchanged
    )
    if restored is not None:
        if restored.source_modified:
            score = copper.score(
                restored.route,
                width=setting.width,
                size=setting.size,
                drill=setting.drill,
                weights=weights,
                segment_dimensions=plan_dimensions(restored, setting),
            )
            restored = replace(restored, rework_cost=score.cost)
        variants.append(restored)

    def collect(candidate_net, engine):
        for candidate in whole_net(
            candidate_net, prior, engine, budget, rework_cost=rework_cost
        ):
            refined = engine.refine_complete(candidate_net, prior, candidate.route)
            if (
                refined.segments != candidate.route.segments
                and refined.cost + rework_cost(refined) + 1e-9 < candidate.cost
                and engine.accept(candidate_net, refined)
            ):
                variants.append(
                    replace(candidate, route=refined, rework_cost=rework_cost(refined))
                )
            variants.append(candidate)

    # After earlier nets have been placed, try a tighter window around this
    # net's islands. Use a separate cache and retain the wider search as a
    # fallback; neither bounded search establishes infeasibility.
    if restored is None and prior and len(net.islands) > 1:
        try:
            anchors = choose_checkpoint(net, maximum=len(net.islands)).anchors
            bounds = (
                setting.lower.x / 1e6,
                setting.lower.y / 1e6,
                setting.upper.x / 1e6,
                setting.upper.y / 1e6,
            )
            window, step = search_window(
                anchors,
                bounds,
                1.5,
                setting.step / 1e6,
                len(setting.layers),
                setting.raw.get("max_cells", 250000),
                access_inset=route_access_inset(setting.raw),
            )
        except CheckpointWindowUnavailable as error:
            budget.failure("local_window", f"{net.name}: {error}")
        else:
            if tuple(window) != tuple(bounds):
                islands = tuple(
                    replace(
                        island,
                        sites=tuple(
                            site
                            for site in island.sites
                            if window[0] <= site.point.x / 1e6 <= window[2]
                            and window[1] <= site.point.y / 1e6 <= window[3]
                        ),
                    )
                    for island in net.islands
                )
                if all(island.sites for island in islands):
                    local_net = replace(net, islands=islands)
                    local = Settings(
                        {**setting.raw, "bounds_mm": list(window), "step_mm": step},
                        setting.native,
                        setting.copper,
                        setting.board_bounds,
                        setting.edge_clearance,
                        setting.saved_rules,
                    )
                    engine = NativeBackend(
                        {**backend.settings, net.name: local}, backend.obstacles, budget
                    )
                    try:
                        with budget.net_slice(2):
                            collect(local_net, engine)
                    except BudgetExhausted:
                        pass
    # Once an exact restoration is available, reserve most of this net's
    # slice for the other nets in the transaction. If the restored path is
    # blocked by an earlier proposal, normal full-slice search still applies.
    limit = (
        budget.net_slice(4)
        if restored is not None and budget.slice_deadline is not None
        else nullcontext()
    )
    try:
        with limit:
            collect(net, backend)
    except BudgetExhausted:
        if not variants:
            raise
    return tuple(variants)


def configurations(data: object) -> tuple[dict, dict[str, dict], Limits]:
    data = _fields(
        data,
        {
            "board_snapshot",
            "shared",
            "nets",
            "orders",
            "movable",
            "placement",
            "search",
            "rip_up",
            "rework",
            "max_islands",
            "repair_max_islands",
            "checkpoint_offset",
            "checkpoint_margin_mm",
            "recovery_stage",
            "recovery_bounds_mm",
        },
        "net planner",
    )
    shared = _fields(
        data.get("shared", {}),
        SHARED_FIELDS | {"obstacles", "current_model"},
        "shared settings",
    )
    raw = data.get("nets")
    if not isinstance(raw, list) or not raw:
        raise ValueError("nets must list the complete nets to route")
    nets = {}
    for request in raw:
        request = _fields(
            request,
            SHARED_FIELDS | {"name", "obstacles", "current_model"},
            "net request",
        )
        name = request.get("name")
        if not isinstance(name, str) or not name or name in nets:
            raise ValueError("Every net needs a distinct nonempty name")
        nets[name] = {**shared, **{k: v for k, v in request.items() if k != "name"}}
        refill = nets[name].get("assume_zone_refill", False)
        if not isinstance(refill, bool):
            raise ValueError("assume_zone_refill must be boolean")
    if len({n.get("assume_zone_refill", False) for n in nets.values()}) != 1:
        raise ValueError("All nets must use the same zone-refill assumption")
    rip_up = data.get("rip_up", {})
    if not isinstance(rip_up, dict) or any(name not in nets for name in rip_up):
        raise ValueError("Rip-up is allowed only for requested nets")
    if any(not isinstance(items, list) for items in rip_up.values()):
        raise ValueError("Each rip-up entry must be a list of copper items")
    maximum = data.get("max_islands")
    offset = data.get("checkpoint_offset", 0)
    if maximum is not None and (
        isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or not 2 <= maximum <= 20
    ):
        raise ValueError("max_islands must be an integer between 2 and 20")
    repair_maximum = data.get("repair_max_islands", maximum)
    if repair_maximum is not None and (
        isinstance(repair_maximum, bool)
        or not isinstance(repair_maximum, int)
        or not 2 <= repair_maximum <= 20
    ):
        raise ValueError("repair_max_islands must be an integer between 2 and 20")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("checkpoint_offset must be a nonnegative integer")
    stage = data.get("recovery_stage", 0)
    if isinstance(stage, bool) or not isinstance(stage, int) or not 0 <= stage <= 4:
        raise ValueError("recovery_stage must be an integer between 0 and 4")
    ReworkWeights.from_config(data.get("rework"))
    MoveScope.from_config(data.get("placement"))
    margin = data.get("checkpoint_margin_mm", 4.0)
    if (
        isinstance(margin, bool)
        or not isinstance(margin, (int, float))
        or not math.isfinite(margin)
        or margin <= 0
    ):
        raise ValueError("checkpoint_margin_mm must be finite and positive")
    return data, nets, Limits.from_config(data.get("search", {}))


def plan_nets(
    data: dict,
    project: Path,
    *,
    validate: bool = False,
    details: bool = False,
    include_changes: bool = False,
) -> dict:
    data, requests, limits = configurations(data)
    move_scope = MoveScope.from_config(data.get("placement"))
    source_name = data.get("board_snapshot", "pcb")
    if not isinstance(source_name, str) or not source_name:
        raise ValueError("board_snapshot must be a saved PCB path or 'pcb'")
    source = (
        native_path(project, ".kicad_pcb")
        if source_name == "pcb"
        else project / source_name
    )
    budget = Budget(limits)
    candidates: list[Candidate] = []
    trials = []
    checkpoint_records = []
    seen = set()
    refill_zones = next(iter(requests.values())).get("assume_zone_refill", False)
    with Snapshot(source) as snapshot:
        original_board = load_board(snapshot.board_path, refill_zones=refill_zones)
        original_contracts = contracts(original_board, tuple(requests))
        selected_ids: set[str] = set()
        removal_changes = []
        for name, removals in data.get("rip_up", {}).items():
            identifiers = validate_removals(original_board, removals, name)
            if identifiers.intersection(selected_ids):
                raise ValueError("Copper may not be removed more than once")
            selected_ids.update(identifiers)
            index = next(
                i
                for i, contract in enumerate(original_contracts)
                if contract.net == name
            )
            original_contracts[index] = replace(
                original_contracts[index], removals=tuple(removals)
            )
            removal_changes.extend(
                {"document": "pcb", "target": item, "op": "remove", "path": []}
                for item in removals
            )
        original_pad_count = {
            name: sum(p.GetNetname() == name for p in original_board.GetPads())
            for name in requests
        }
        removed_copper = capture_removals(original_board, selected_ids)
        rework_weights = ReworkWeights.from_config(data.get("rework"))
        try:
            for poses, moves in placements(
                snapshot, data.get("movable", []), set(requests), budget, move_scope
            ):
                if removal_changes:
                    snapshot.board_path.write_bytes(
                        CampaignPlanner(snapshot.directory)
                        .plan("pcb", removal_changes)
                        .after
                    )
                board = load_board(snapshot.board_path, refill_zones=refill_zones)
                edge_box = board.GetBoardEdgesBoundingBox()
                board_bounds = (
                    Point(int(edge_box.GetX()), int(edge_box.GetY())),
                    Point(int(edge_box.GetRight()), int(edge_box.GetBottom())),
                )
                edge_clearance = int(board.GetDesignSettings().m_CopperEdgeClearance)
                zone_count = sum(not zone.GetIsRuleArea() for zone in board.Zones())
                native = {}
                effective_requests = {
                    name: dict(settings) for name, settings in requests.items()
                }
                checkpoint_contracts = []
                for name, settings in effective_requests.items():
                    layers = settings.get(
                        "layers",
                        [
                            board.GetLayerName(layer)
                            for layer in board.GetEnabledLayers().CuStack()
                        ],
                    )
                    if (
                        not isinstance(layers, list)
                        or not layers
                        or any(layer not in COPPER_LAYERS for layer in layers)
                    ):
                        raise ValueError("layers must list known copper layers")
                    zone_via_margin = 0
                    if refill_zones:
                        via = _fields(
                            settings.get("via", {}), {"size_mm", "drill_mm"}, "via"
                        )
                        via_size = mm(via.get("size_mm", 0.6))
                        clearance = mm(settings.get("clearance_mm", 0.2))
                        if via_size <= 0 or clearance < 0:
                            raise ValueError(
                                "Via size must be positive and clearance nonnegative"
                            )
                        zone_via_margin = (via_size + 1) // 2 + clearance
                    native[name] = discover(
                        board,
                        name,
                        tuple(layers),
                        budget,
                        include_zone_access=refill_zones,
                        zone_via_margin=zone_via_margin,
                    )
                    if name in removed_copper:
                        native[name] = replace(
                            native[name],
                            net=include_repair_contacts(
                                board,
                                native[name].net,
                                removed_copper[name],
                                budget=budget,
                            ),
                        )
                    if data.get("max_islands") is not None:
                        if (
                            len(native[name].net.islands) >= 2
                            or poses
                            or data.get("rip_up", {}).get(name)
                        ):
                            if poses or data.get("rip_up", {}).get(name):
                                selection = choose_transaction_checkpoint(
                                    original_board,
                                    native[name].net,
                                    target=name == next(iter(requests)),
                                    maximum=data["max_islands"],
                                    repair_maximum=data.get(
                                        "repair_max_islands", data["max_islands"]
                                    ),
                                    offset=data.get("checkpoint_offset", 0),
                                )
                            else:
                                selection = choose_checkpoint(
                                    native[name].net,
                                    maximum=data["max_islands"],
                                    offset=data.get("checkpoint_offset", 0),
                                )
                            partial, anchors = selection.net, selection.anchors
                            window, step = search_window(
                                anchors,
                                tuple(settings["bounds_mm"]),
                                data.get("checkpoint_margin_mm", 4.0),
                                settings.get("step_mm", 0.25),
                                len(layers),
                                settings.get("max_cells", 250000),
                                access_inset=route_access_inset(settings),
                            )
                            settings["bounds_mm"] = list(window)
                            settings["step_mm"] = step
                            checkpoint_records.append(
                                {
                                    "net": name,
                                    "index": selection.index,
                                    "count": selection.count,
                                    "islands": sorted(
                                        island.identifier for island in partial.islands
                                    ),
                                    "bounds_mm": list(window),
                                    "step_mm": step,
                                    "stage": data.get("recovery_stage", 0),
                                }
                            )
                            local_islands = tuple(
                                replace(
                                    island,
                                    sites=tuple(
                                        site
                                        for site in island.sites
                                        if window[0] <= site.point.x / 1e6 <= window[2]
                                        and window[1] <= site.point.y / 1e6 <= window[3]
                                    ),
                                )
                                for island in partial.islands
                            )
                            native[name] = replace(
                                native[name],
                                net=replace(partial, islands=local_islands),
                            )
                            physical_sites = {
                                virtual: physical
                                for island in partial.islands
                                for virtual, physical in island.zone_anchors
                            }
                            bound_anchors = tuple(
                                physical_sites.get(site, site) for site in anchors
                            )
                            if poses:
                                bound_anchors = tuple(
                                    original_anchor(original_board, board, name, site)
                                    for site in bound_anchors
                                )
                            first = bound_anchors[0]
                            checkpoint_contracts.extend(
                                RouteContract(
                                    name,
                                    first.point,
                                    first.layer,
                                    site.point,
                                    site.layer,
                                )
                                for site in (bound_anchors[1:] or bound_anchors)
                            )
                        else:
                            checkpoint_contracts.extend(
                                c for c in original_contracts if c.net == name
                            )
                for name, removals in data.get("rip_up", {}).items():
                    if data.get("max_islands") is not None and removals:
                        index = next(
                            i
                            for i, contract in enumerate(checkpoint_contracts)
                            if contract.net == name
                        )
                        checkpoint_contracts[index] = replace(
                            checkpoint_contracts[index], removals=tuple(removals)
                        )
                saved_rules = RoutingRules.from_board(
                    board, snapshot.board_path.with_suffix(".kicad_dru")
                )
                del board
                baseline = obstacles(
                    snapshot.board_path,
                    next(iter(requests)),
                    next(iter(requests.values())).get("assume_zone_refill", False),
                )
                settings = {
                    name: Settings(
                        raw,
                        native[name],
                        dict(baseline.copper_thicknesses),
                        board_bounds,
                        edge_clearance,
                        saved_rules,
                    )
                    for name, raw in effective_requests.items()
                }
                # Validate settings even for nets that are already connected.
                for setting in settings.values():
                    setting.router(())
                backend = NativeBackend(settings, baseline.obstacles, budget)
                solved, order_trials = search_orders(
                    tuple(item.net for item in native.values()),
                    budget,
                    lambda net, prior: _solve_with_terminal_refinement(
                        net,
                        prior,
                        backend,
                        budget,
                        removed_copper.get(net.name),
                        rework_weights,
                        source_unchanged=not poses,
                    ),
                    data.get("orders"),
                    preferred_first=frozenset(removed_copper) - {next(iter(requests))},
                )
                trials.append(
                    {
                        "placement": poses,
                        "orders": list(order_trials),
                        "island_counts": {
                            n: len(v.net.islands) for n, v in native.items()
                        },
                        "ignored_zone_count": baseline.ignored_zones,
                        "zone_count": zone_count,
                        "zone_connectivity": "refilled" if refill_zones else "unfilled",
                    }
                )
                for result in solved:
                    key = tuple(sorted(poses.items())), result.signature
                    if key not in seen:
                        seen.add(key)
                        candidates.append(
                            Candidate(
                                result,
                                poses,
                                (*moves, *removal_changes),
                                settings,
                                tuple(
                                    checkpoint_contracts
                                    if data.get("max_islands") is not None
                                    else original_contracts
                                ),
                                placement_cost(original_board, poses, rework_weights),
                            )
                        )
                candidates.sort(key=lambda c: (c.cost, c.board.signature))
                budget.pruned += max(0, len(candidates) - limits.board_beam)
                candidates = candidates[: limits.board_beam]
                snapshot.check()
                if budget.limits_hit.intersection({"seconds", "path_calls"}):
                    break
        except BudgetExhausted:
            pass
        budget.finish_search()
        snapshot.check()
        result = {
            "status": "no_complete_candidate_found",
            "source_sha256": snapshot.fingerprints[snapshot.source],
            "search": budget.report(),
            "trials": trials,
            "checkpoints": checkpoint_records,
            "complete_candidates": len(candidates),
            "optimality": "not_proven",
            "source_mode": "saved_disk_snapshot",
            "scope": "partial_net_checkpoint"
            if data.get("max_islands") is not None
            else "whole_net",
            "applied": False,
            "planner_sha256": snapshot.tool_hashes,
            "limitations": [
                "Finite grids, access sites, detour alternatives, placements and beams are sampled.",
                "Failure or a search limit leaves feasibility unknown; no global optimum is claimed.",
                (
                    "Search uses freshly refilled temporary zones; native closing checks refill again."
                    if refill_zones
                    else "Zones are not credited for connectivity during search; native closing checks refill them."
                ),
                "Supplied electrical ratings are not independently established by this planner.",
                "Signal integrity, reference copper, component ratings and manufacturing readiness require review.",
                "Search clips to the board extent and saved edge allowance; exact outlines and custom rules are checked by native DRC.",
            ],
        }
        rejected = []
        selected = None
        junction_path = snapshot.directory / "junction-source.kicad_pcb"
        junction_path.write_bytes(snapshot.original)
        materialized = []
        for candidate in candidates:
            junction_board = load_board(junction_path)
            changes = list(candidate.moves)
            candidate_contracts = list(candidate.contracts)
            pruned_tails = []
            try:
                for plan in candidate.board.plans:
                    s = candidate.settings[plan.net]
                    additions, removals = junction_changes(
                        junction_board,
                        snapshot.original,
                        plan.route,
                        plan.net,
                        s.width,
                        s.size,
                        s.drill,
                        removed_copper_ids=frozenset(selected_ids),
                        segment_dimensions=plan_dimensions(plan, s),
                        prune_dangling_tails=not bool(candidate.poses),
                        merge_collinear=not bool(candidate.poses),
                        pruned_tails=pruned_tails,
                        protected_sites=tuple(
                            site
                            for contract in candidate_contracts
                            if contract.net == plan.net
                            for site in (
                                (contract.start, contract.start_layer),
                                (contract.end, contract.end_layer),
                            )
                        ),
                    )
                    changes.extend(additions)
                    if removals:
                        index = next(
                            (
                                i
                                for i, contract in enumerate(candidate_contracts)
                                if contract.net == plan.net
                            ),
                            None,
                        )
                        if index is None:
                            raise ValueError(
                                "A split track needs a matching net contract"
                            )
                        candidate_contracts[index] = replace(
                            candidate_contracts[index],
                            removals=(*candidate_contracts[index].removals, *removals),
                        )
            except ValueError as error:
                rejected.append({"cost": candidate.cost, "reason": str(error)})
                continue
            junction_rework_cost = junction_tail_cost(pruned_tails, rework_weights)
            candidate = replace(candidate, junction_rework_cost=junction_rework_cost)
            materialized.append((candidate, changes, candidate_contracts))
        materialized.sort(key=lambda entry: (entry[0].cost, entry[0].board.signature))
        for candidate, changes, candidate_contracts in materialized[
            : limits.validation_candidates if validate else 1
        ]:
            evidence = None
            if validate and changes:
                snapshot.check()
                try:
                    evidence = validate_routes(
                        snapshot.source,
                        result["source_sha256"],
                        changes,
                        candidate_contracts,
                        footprint_moves=tuple(candidate.poses),
                        move_scope=move_scope,
                        require_full_nets=data.get("max_islands") is None,
                    )
                except CandidateRejected as error:
                    snapshot.check()
                    rejected.append({"cost": candidate.cost, "reason": str(error)})
                    continue
                if evidence["status"] != "no_new_drc_violations":
                    rejected.append(
                        {
                            "cost": candidate.cost,
                            "validation": {
                                k: v
                                for k, v in evidence.items()
                                if not k.startswith("_")
                            },
                        }
                    )
                    continue
                changes.extend(evidence.pop("_zone_refill_changes", []))
                if data.get("max_islands") is None:
                    evidence["whole_net_pad_counts"] = original_pad_count
                    evidence["whole_net_connectivity"] = "all_requested_pads_connected"
                else:
                    evidence["checkpoint_connectivity"] = (
                        "selected_copper_islands_connected"
                    )
            selected = candidate
            result.update(
                {
                    "status": "validated_sampled_candidate"
                    if evidence
                    else "already_connected"
                    if not changes
                    else "geometric_candidate_only",
                    "cost": candidate.cost,
                    "routing_cost": sum(p.route.cost for p in candidate.board.plans),
                    "rework_cost": sum(p.rework_cost for p in candidate.board.plans)
                    + candidate.junction_rework_cost,
                    "placement_cost": candidate.placement_cost,
                    "placement": candidate.poses,
                    "order": [p.net for p in candidate.board.plans],
                    "order_selection": {
                        "method": "score_complete_trial_routings",
                        "selected": [p.net for p in candidate.board.plans],
                        "reuses_trial_routing": True,
                        "optimality": "best_validated_sampled_candidate"
                        if evidence
                        else "best_sampled_candidate",
                    },
                    "total_length_mm": sum(
                        p.route.length_mm for p in candidate.board.plans
                    ),
                    "new_via_count": sum(
                        len(p.route.vias) for p in candidate.board.plans
                    ),
                    "bend_count": sum(p.route.bends for p in candidate.board.plans),
                    "nets": [],
                }
            )
            for plan in candidate.board.plans:
                s = candidate.settings[plan.net]
                payload = route_payload(
                    plan.route,
                    plan.net,
                    s.width,
                    s.size,
                    s.drill,
                    s.profile,
                    plan_dimensions(plan, s),
                )
                if not details:
                    payload.pop("segments")
                    payload.pop("vias")
                payload.update(
                    pad_count=original_pad_count[plan.net],
                    electrical=s.assessment(plan.route),
                )
                result["nets"].append(payload)
            if evidence:
                evidence["electrical_assessments"] = [
                    {"net": item["net"], **item["electrical"]}
                    for item in result["nets"]
                ]
                result["validation"] = evidence
            if include_changes:
                result["unvalidated_campaign_changes"] = changes
                result["route_contracts"] = [
                    {
                        "net": c.net,
                        "start": [c.start.x, c.start.y],
                        "start_layer": c.start_layer,
                        "end": [c.end.x, c.end.y],
                        "end_layer": c.end_layer,
                    }
                    for c in candidate_contracts
                ]
            break
        if selected is None and candidates and validate:
            result["status"] = "no_validated_candidate_found"
        if rejected:
            result["rejected_candidates"] = rejected
        result["search"] = budget.report()
        snapshot.check()
    return result
