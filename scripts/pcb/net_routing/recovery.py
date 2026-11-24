"""Bounded search escalation without weakening physical or electrical rules."""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import dataclass, fields


STAGE_NAMES = ("local", "wider pairs", "finer pairs", "board detour", "copper reroute")


@dataclass(frozen=True)
class RecoveryPolicy:
    early_rework: bool = False
    stages: int = 3
    alternatives_per_stage: int = 3
    defer_rounds: int = 2
    wide_margin_mm: float = 12.0
    max_cells: int = 1_000_000
    max_expansions: int = 800_000
    path_candidates: int = 3
    heuristic_weight: float = 2.0
    rip_up_candidates: int = 3
    rip_up_items: int = 12
    whole_net_rip_up_items: int = 32
    rip_up_nets: int = 3
    repair_islands: int = 12
    rip_up_radius_mm: float = 1.5
    obstruction_trials: int = 4
    obstruction_seconds: float = 10.0
    placement_candidates: int = 6
    placement_nets: int = 12
    placement_items: int = 24

    @classmethod
    def from_config(cls, raw: object = None) -> RecoveryPolicy:
        if raw is None:
            return cls()
        if not isinstance(raw, dict) or set(raw) - {
            field.name for field in fields(cls)
        }:
            raise ValueError("Unknown recovery settings")
        result = cls(**raw)
        if not isinstance(result.early_rework, bool):
            raise ValueError("recovery.early_rework must be true or false")
        for name in (
            "stages",
            "alternatives_per_stage",
            "defer_rounds",
            "max_cells",
            "max_expansions",
            "path_candidates",
            "rip_up_candidates",
            "rip_up_items",
            "whole_net_rip_up_items",
            "rip_up_nets",
            "repair_islands",
            "placement_nets",
            "placement_items",
        ):
            value = getattr(result, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value
                < (
                    0
                    if name
                    in {
                        "stages",
                        "defer_rounds",
                        "rip_up_candidates",
                        "whole_net_rip_up_items",
                    }
                    else 1
                )
            ):
                raise ValueError(f"recovery.{name} must be a valid integer")
        if result.stages > 3 or result.alternatives_per_stage > 64:
            raise ValueError(
                "Recovery supports at most 3 stages and 64 alternatives per stage"
            )
        if result.rip_up_candidates > 16:
            raise ValueError(
                "Recovery supports at most 16 copper replacement candidates"
            )
        if (
            isinstance(result.obstruction_trials, bool)
            or not isinstance(result.obstruction_trials, int)
            or not 0 <= result.obstruction_trials <= 16
        ):
            raise ValueError("Obstruction diagnosis supports zero to 16 searches")
        if (
            isinstance(result.placement_candidates, bool)
            or not isinstance(result.placement_candidates, int)
            or not 0 <= result.placement_candidates <= 32
        ):
            raise ValueError("Placement recovery supports zero to 32 candidates")
        if result.placement_nets > 32 or result.placement_items > 64:
            raise ValueError(
                "Bottom placement recovery supports up to 32 affected nets and 64 copper items"
            )
        if (
            result.rip_up_items > 64
            or result.whole_net_rip_up_items > 64
            or result.rip_up_nets > 8
            or not 2 <= result.repair_islands <= 20
        ):
            raise ValueError(
                "Copper recovery supports up to 64 items, 8 nets and 2 to 20 repair islands"
            )
        for name in (
            "wide_margin_mm",
            "heuristic_weight",
            "rip_up_radius_mm",
            "obstruction_seconds",
        ):
            value = getattr(result, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"recovery.{name} must be positive and finite")
        if result.heuristic_weight < 1:
            raise ValueError("Recovery heuristic weight must be at least one")
        return result


def search_key(request: dict) -> str:
    """Identify an exact search request; callers bind its history to board bytes."""
    return hashlib.sha256(
        json.dumps(
            request, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def recovery_preference(request: dict) -> dict | None:
    """Keep only a repair's purpose, never its old geometry or copper IDs."""
    if not request.get("rip_up") and not request.get("movable"):
        preference = request.get("recovery_preference")
        if preference is None:
            return None
        if (
            not isinstance(preference, dict)
            or set(preference) != {"neighbors", "moving"}
            or any(
                not isinstance(preference[key], list)
                or any(
                    not isinstance(value, str) or not value for value in preference[key]
                )
                for key in ("neighbors", "moving")
            )
        ):
            raise ValueError("Invalid saved recovery preference")
        return {key: sorted(set(preference[key])) for key in ("neighbors", "moving")}
    nets = request.get("nets", [])
    moves = request.get("movable", [])
    if (
        not isinstance(nets, list)
        or not nets
        or not isinstance(moves, list)
        or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str)
            for item in nets
        )
        or any(
            not isinstance(item, dict) or not isinstance(item.get("reference"), str)
            for item in moves
        )
    ):
        return None
    return {
        "neighbors": sorted({item["name"] for item in nets[1:]}),
        "moving": sorted({item["reference"] for item in moves}),
    }


def prioritize_recovery_requests(
    requests: list[dict], preference: dict | None
) -> list[dict]:
    """Match an intent to newly compiled requests; unmatched hints are harmless."""
    if preference is None:
        return list(requests)
    return sorted(
        requests, key=lambda candidate: recovery_preference(candidate) != preference
    )


def renew_queue(fresh: list, previous: list, policy: RecoveryPolicy) -> list:
    """Recompile pending recovery stages without replaying saved copper edits.

    Net names, queue order, escalation levels and a geometry-free repair
    preference survive a code renewal. Geometry, dimensions, search options
    and removal UUIDs come from the new planner.
    """
    frontier = {}
    early_frontier = {}
    ordinal = 0
    for round_ in previous:
        for request in round_:
            # Legacy records may contain obsolete request schemas. They carry
            # no usable frontier; fresh coverage still includes those nets.
            if (
                not isinstance(request, dict)
                or not isinstance(request.get("nets"), list)
                or any(
                    not isinstance(net, dict) or not isinstance(net.get("name"), str)
                    for net in request["nets"]
                )
            ):
                continue
            stage = request.get("recovery_stage", 0)
            if (
                not isinstance(stage, int)
                or isinstance(stage, bool)
                or not 0 <= stage <= 4
            ):
                raise ValueError("Invalid saved recovery stage")
            early = request.get("early_rework", False)
            if not isinstance(early, bool) or (early and stage != 4):
                raise ValueError("Invalid saved early rework marker")
            # Repair neighbors are subordinate to the first unresolved target.
            nets = (
                request["nets"][:1]
                if request.get("rip_up") or request.get("movable")
                else request["nets"]
            )
            for net in nets:
                name = net["name"]
                if early:
                    if policy.early_rework:
                        early_frontier.setdefault(
                            name, (ordinal, recovery_preference(request))
                        )
                    ordinal += 1
                    continue
                old_stage, old_order, preference = frontier.get(
                    name, (0, ordinal, None)
                )
                if name not in frontier or stage > old_stage:
                    preference = recovery_preference(request)
                frontier[name] = (max(stage, old_stage), old_order, preference)
                ordinal += 1
    pending = []
    for round_ in fresh:
        ordinary_requests, ordinary_orders = [], []
        for request in round_:
            ordinary = []
            for net in request["nets"]:
                if net["name"] in early_frontier:
                    early_order, early_preference = early_frontier[net["name"]]
                    marker = deepcopy(request)
                    marker.update(
                        nets=[deepcopy(net)],
                        checkpoint_offset=0,
                        recovery_stage=4,
                        early_rework=True,
                    )
                    for key in ("orders", "rip_up", "movable", "recovery_preference"):
                        marker.pop(key, None)
                    if early_preference is not None:
                        marker["recovery_preference"] = early_preference
                    pending.append((early_order, [marker]))
                stage, order, preference = frontier.get(net["name"], (0, ordinal, None))
                if not stage:
                    ordinary.append(net)
                    continue
                rebuilt = deepcopy(request)
                rebuilt["nets"] = [deepcopy(net)]
                rebuilt["checkpoint_offset"] = 0
                while rebuilt.get("recovery_stage", 0) < stage:
                    advanced = next_stage(rebuilt, policy)
                    if advanced is None:
                        break
                    rebuilt = advanced
                if rebuilt.get("recovery_stage") == 4 and preference is not None:
                    rebuilt["recovery_preference"] = preference
                pending.append((order, [rebuilt]))
            if ordinary:
                rebuilt = deepcopy(request)
                rebuilt["nets"] = ordinary
                ordinary_requests.append(rebuilt)
                ordinary_orders.append(
                    min(
                        frontier.get(n["name"], (0, ordinal, None))[1] for n in ordinary
                    )
                )
                ordinal += 1
        if ordinary_requests:
            # These windows were scheduled as independent by the fresh planner.
            # Keep their parallelism; escalated/widened windows remain isolated.
            pending.append((min(ordinary_orders), ordinary_requests))
    return [round_ for _, round_ in sorted(pending, key=lambda item: item[0])]


def next_stage(request: dict, policy: RecoveryPolicy) -> dict | None:
    """Prefer simpler pairs, then a finer grid, then a wider detour corridor.

    Widths, clearances, vias, layer eligibility, angle rules, and time budgets
    stay unchanged. Larger windows run alone, outside the parallel batch.
    Rip-up repairs retain their full fragment set and therefore cannot be
    reduced to pairs. The final marker is compiled against the current board.
    """
    stage = request.get("recovery_stage", 0) + 1
    if stage > policy.stages:
        if (
            policy.stages
            and (policy.rip_up_candidates or policy.placement_candidates)
            and stage <= 4
            and not request.get("rip_up")
            and not request.get("movable")
        ):
            result = deepcopy(request)
            result.update(recovery_stage=4, checkpoint_offset=0)
            return result
        return None
    result = deepcopy(request)
    result.update(recovery_stage=stage, checkpoint_offset=0)
    if result.get("max_islands") is not None and not result.get("rip_up"):
        result["max_islands"] = 2
    result["checkpoint_margin_mm"] = max(
        result.get("checkpoint_margin_mm", 4.0), policy.wide_margin_mm
    )
    result.setdefault("search", {})["path_candidates"] = max(
        result.get("search", {}).get("path_candidates", 1), policy.path_candidates
    )
    shared = result["shared"]
    for net in result["nets"]:
        # Set effective per-net search options so durable overrides are honored.
        net["topology_heuristic"] = True
        net["heuristic_weight"] = max(
            net.get("heuristic_weight", shared.get("heuristic_weight", 1.0)),
            policy.heuristic_weight,
        )
        net["max_expansions"] = max(
            net.get("max_expansions", shared.get("max_expansions", 200_000)),
            policy.max_expansions,
        )
        net["max_cells"] = max(
            net.get("max_cells", shared.get("max_cells", 250_000)), policy.max_cells
        )
        if stage == 2:
            step = net.get("step_mm", shared.get("step_mm", 0.1))
            net["step_mm"] = max(0.000001, round(step / 2, 6))
    if stage == 3:
        window = list(result.get("recovery_bounds_mm", shared["bounds_mm"]))
        if (
            len(window) != 4
            or any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                for value in window
            )
            or window[0] >= window[2]
            or window[1] >= window[3]
        ):
            raise ValueError("Recovery requires finite board bounds")
        shared["bounds_mm"] = window
        result["checkpoint_margin_mm"] = max(
            window[2] - window[0], window[3] - window[1]
        )
    return result
