"""Try bounded, coordinated copper replacement on private routing snapshots.

The search is experimental. It can retain only an independently checked
improvement, and it does not certify electrical or manufacturing readiness.
"""

from __future__ import annotations

from core.project import native_path

import argparse
from collections import Counter
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
import sys
import tempfile
import time
from pathlib import Path

from core.kicad_campaign import CampaignPlanner
from core.kicad_sexpr import parse
from core.paths import asset_root, config_path, results_path
from core.pcb_io import copper_width, enabled_copper_layers, quiet_duplicate_image_handlers
from pcb.checkpoints import read_config
from pcb.freeroute_delta import Trace, Via, filter_proposal, read_copper, router_class
from pcb.freeroute_preview import preview, routing_pass_budget
from pcb.footprint_pose import native_pose_change
from pcb.net_routing.controller import decode_checkpoint, make_work_plan
from pcb.net_routing.external import retain_candidate
from pcb.net_routing.fast import (
    MAX_REWORK_ATTEMPTS, _class_names, _focus_deferred, _net_names, _roundtrip,
    fast_retry_signature, record_unimproved_batch, rework_attempts,
)
from pcb.net_routing.isolation import run_native
from pcb.net_routing.native import Snapshot
from pcb.net_routing.prepare import prepare_project, verify_parity_copper_unchanged
from pcb.net_routing.protection import (
    ProtectedState, protected_copper_unchanged, protection_nets, working_connectivity_classes,
)
from pcb.net_routing.reroute import compile_reroutes
from pcb.net_routing.rework import ReworkWeights, placement_cost
from pcb.net_routing.runner import (
    _run_id, _source_hashes, _verify_resume_connectivity, verify_resume_reports,
    verify_unchanged_native_errors,
)
from pcb.net_routing.via_prune import copper_removal_changes
from pcb.route_validation import stable_connection_items
from core.toolchain import RouterCache


@dataclass(frozen=True, slots=True)
class ReworkPlan:
    target: str
    affected: tuple[str, ...]
    items: int
    cost: float
    board: bytes
    floors: dict[str, tuple[int, int, int]]
    moves: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    attempt_key: str = ""


def repair_identity(target: str | tuple[str, ...], identifiers: frozenset[str], poses: tuple = ()) -> str:
    """Identify an exact removal/pose proposal independently of its queue position."""
    data = (target, sorted(identifiers), sorted(poses))
    return hashlib.sha256(json.dumps(data, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def copper_neighbor_distances(board, by_net: dict, targets: tuple[str, ...]) -> dict[str, float]:
    """Rank physical copper gaps on shared layers, rather than bounding-box overlap.

    This is a search-order heuristic, not a clearance approval. Native capsule
    shapes and widths are cached once per item/layer. Bounding boxes only prune
    comparisons whose lower distance bound already exceeds the best actual gap.
    """
    import pcbnew

    layers = tuple(board.GetEnabledLayers().CuStack())
    cached = {}

    def records(item):
        uid = item.m_Uuid.AsString()
        if uid not in cached:
            via = isinstance(item, pcbnew.PCB_VIA)
            points = (item.GetPosition(),) if via else (item.GetStart(), item.GetEnd())
            rows = {}
            for layer in layers:
                if item.IsOnLayer(layer):
                    shape = item.GetEffectiveShape(layer)
                    rows[layer] = (shape.BBox(), shape, points, copper_width(item, layer=layer) / 2)
            cached[uid] = rows
        return cached[uid]

    target_shapes = {}
    for name in targets:
        for item in by_net.get(name, ()):
            for layer, record in records(item).items():
                target_shapes.setdefault(layer, []).append(record)
    distances = {}
    for name, items in by_net.items():
        if name in targets:
            distances[name] = 0.0
            continue
        best = math.inf
        for item in items:
            for layer, (box, shape, points, radius) in records(item).items():
                for other_box, other_shape, other_points, other_radius in target_shapes.get(layer, ()):
                    lower = math.hypot(
                        max(0, box.GetX() - other_box.GetRight(), other_box.GetX() - box.GetRight()),
                        max(0, box.GetY() - other_box.GetBottom(), other_box.GetY() - box.GetBottom()))
                    if lower >= best:
                        continue
                    if shape.Collide(other_shape, 0):
                        best = 0.0
                    else:
                        best = min(best, max(0.0, min(
                            *(other_shape.Distance(point) - radius for point in points),
                            *(shape.Distance(point) - other_radius for point in other_points))))
                    if not best:
                        break
                if not best:
                    break
            if not best:
                break
        distances[name] = best
    return distances


def compile_tree_renewal(directory: Path, targets: tuple[str, ...], allowed: tuple[str, ...],
                         critical: tuple[str, ...], ordinal: int, limit: int,
                         avoid: tuple[str, ...] = (), parity_nets: tuple[str, ...] = (),
                         tried_plans: tuple[str, ...] = (), include_connected: bool = False) -> ReworkPlan | None:
    """Rebuild complete unfinished net trees in batches under saved native rules.

    This removes obsolete partial topology only on the private board. All prior
    required connections must be restored before the result can be retained.
    """
    import pcbnew

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64:
        raise ValueError("A tree-renewal batch must contain 1 to 64 nets")
    if not isinstance(include_connected, bool):
        raise ValueError("Connected-neighbor selection must be boolean")
    board = pcbnew.LoadBoard(str(native_path(directory, ".kicad_pcb")))
    if board is None:
        raise RuntimeError("Cannot load the private tree-renewal board")
    requested = set(_class_names(allowed))
    excluded = set(critical) | set(parity_nets) | protection_nets(board) | {"", "GND"}
    for raw in board.GetNetsByName():
        name = str(raw)
        if name and router_class(str(board.FindNet(name).GetNetClassName())) not in requested:
            excluded.add(name)
    tracks = list(board.GetTracks())
    by_net = {}
    for item in tracks:
        by_net.setdefault(str(item.GetNetname()), []).append(item)
    grouped = {part.m_Uuid.AsString() for group in board.Groups() for part in group.GetItems()}
    choices = []
    candidates = set(targets)
    if include_connected:
        candidates.update(name for name in by_net
                          if name and (net := board.FindNet(name)) is not None
                          and router_class(str(net.GetNetClassName())) in requested)
    for name in sorted(candidates - excluded - set(avoid)):
        copper = by_net.get(name, ())
        if not copper or any(item.IsLocked() or item.m_Uuid.AsString() in grouped
                             or isinstance(item, pcbnew.PCB_ARC) for item in copper):
            continue
        choices.append(name)
    if not choices:
        return None
    if include_connected:
        # Rebuild unfinished trees first, then physically nearby permitted
        # trees in the explicitly requested scope. Ground, high-speed pairs
        # and other protected nets stay.
        distances = copper_neighbor_distances(board, {name: by_net[name] for name in choices}, targets)
        def proximity(name):
            if name in targets:
                return 0, 0, name
            return 1, distances[name], name
        choices.sort(key=proximity)
    primary = [name for name in choices if name in targets]
    neighbors = [name for name in choices if name not in targets]
    for step in range(len(choices)):
        if include_connected and len(primary) < limit:
            window = limit - len(primary)
            offset = (ordinal * window + step) % len(neighbors) if neighbors else 0
            chosen = tuple(primary + (neighbors[offset:] + neighbors[:offset])[:window])
        else:
            offset = (ordinal * limit + step) % len(choices)
            chosen = tuple((choices[offset:] + choices[:offset])[:limit])
        selected = {item.m_Uuid.AsString() for name in chosen for item in by_net[name]}
        attempt_key = repair_identity(tuple(sorted(chosen)), frozenset(selected))
        if attempt_key not in tried_plans:
            break
    else:
        return None
    changes = copper_removal_changes(board, selected)
    modified = CampaignPlanner(directory).plan("pcb", changes).after
    cost = sum(10.0 if isinstance(item, pcbnew.PCB_VIA) else 1.0 + item.GetLength() / 1e6
               for name in chosen for item in by_net[name])
    # Old, mixed widths are not promoted to a blanket floor for a new tree.
    # Saved net-class and scoped package rules are compiled and checked afresh.
    return ReworkPlan(chosen[0], chosen, len(selected), cost, modified, {},
                      excluded=tuple(sorted(excluded - {""})), attempt_key=attempt_key)


def restore_unimproved_trees(directory: Path, prior: Path, names: tuple[str, ...], *,
                             force: bool = False, keep_neutral: bool = False) -> tuple[str, ...]:
    """Roll back failed trees together; the combined board still needs validation."""
    import pcbnew
    from pcb.net_routing.inventory import inventory_board
    from pcb.route_validation import _broken_existing_connections

    if not isinstance(force, bool) or not isinstance(keep_neutral, bool):
        raise ValueError("Tree restoration options must be boolean")

    source = native_path(directory, ".kicad_pcb")
    # Both inputs were independently refilled by their native batch checks.
    # Keep those fills: the prior file has a different stem and no project file.
    original = pcbnew.LoadBoard(str(prior))
    candidate = pcbnew.LoadBoard(str(source))
    if original is None or candidate is None:
        raise RuntimeError("Cannot inspect the checked tree-renewal boards")
    old_inventory = {net.name: len(net.copper_islands) for net in inventory_board(original).nets}
    new_inventory = {net.name: len(net.copper_islands) for net in inventory_board(candidate).nets}
    old_tracks = {item.m_Uuid.AsString(): item for item in original.GetTracks()}
    new_tracks = {item.m_Uuid.AsString(): item for item in candidate.GetTracks()}
    removed = set(old_tracks) - set(new_tracks)
    broken = {item["net"] for item in _broken_existing_connections(original, candidate, removed)}
    rollback = {name for name in names if force or name in broken or
                new_inventory.get(name, 0) > old_inventory.get(name, 0) or
                (not keep_neutral and new_inventory.get(name, 0) == old_inventory.get(name, 0))}
    if not rollback:
        return ()
    changes = copper_removal_changes(candidate, {uid for uid, item in new_tracks.items()
                                                if str(item.GetNetname()) in rollback})
    old_text = prior.read_text(encoding="utf-8-sig")
    for form in parse(old_text).children():
        if form.name not in {"segment", "via"}:
            continue
        uid = form.children("uuid")[0].atoms()[1].value
        if uid in old_tracks and str(old_tracks[uid].GetNetname()) in rollback:
            changes.append({"document": "pcb", "target": {"kind": "root"},
                            "op": "insert", "path": [], "expression": old_text[form.start:form.end]})
    if changes:
        source.write_bytes(CampaignPlanner(directory).plan("pcb", changes).after)
    return tuple(sorted(rollback))


def prepare_ground_access(directory: Path, policy: dict) -> dict:
    """Reconnect permitted ground islands inside a private repair transaction.

    This writes only the disposable transaction board. All original required
    connections, native rules and protected items are checked after rerouting.
    """
    import pcbnew
    from pcb.net_routing.native import load_board
    from pcb.net_routing.rules import RoutingRules
    from pcb.net_routing.zone_stitch import propose, working_stitch_allowances

    scope = policy.get("working_scope")
    if not isinstance(scope, dict) or scope.get("kind") != "connectivity":
        raise ValueError("Ground-first repair requires an explicit working-connectivity scope")
    classes = frozenset(name.upper() for name in scope.get("classes", ()))
    path = native_path(directory, ".kicad_pcb")
    board = load_board(path, refill_zones=True)
    net = board.FindNet("GND")
    if net is None or str(net.GetNetClassName()).upper() not in classes:
        raise ValueError("Ground-first repair is outside the permitted working scope")
    before = board.GetConnectivity().GetUnconnectedCount(False)
    rules = RoutingRules.from_board(board, path.with_suffix(".kicad_dru"))
    result = propose(board, {"unconnected_items": []}, path.with_suffix(".kicad_pro").read_bytes(), rules,
                     eligible=working_stitch_allowances(board, classes), compact_vias=True)
    if not pcbnew.SaveBoard(str(path), board):
        raise RuntimeError("Cannot save the private ground-first proposal")
    return {**result, "before": before,
            "after": board.GetConnectivity().GetUnconnectedCount(False),
            "electrically_verified": False}


def conflicting_replacement_nets(source: Path, baseline: dict, current: dict,
                                 eligible: tuple[str, ...]) -> tuple[str, ...]:
    """Attribute new native errors to still-active replacement copper."""
    import pcbnew

    def errors(summary):
        return Counter((row[0], row[1], tuple(row[2]))
                       for row in summary["violation_keys"] if row[1] == "error")
    additions = errors(current) - errors(baseline)
    board = pcbnew.LoadBoard(str(source))
    if board is None:
        raise RuntimeError("Cannot inspect conflicting replacement copper")
    names = {item.m_Uuid.AsString(): str(item.GetNetname())
             for item in (*board.GetPads(), *board.GetTracks())}
    allowed = set(eligible)
    return tuple(sorted({names[uid] for row in additions for uid in row[2]
                         if uid in names and names[uid] in allowed}))


def checked_tree_fallback(directory: Path, prior: Path, names: tuple[str, ...],
                           baseline: dict, *, keep_neutral: bool = False,
                           ground_policy: dict | None = None, progress=print) -> dict:
    """Restore failed trees, then resolve conflicts with those restorations."""
    restored = set(run_native(restore_unimproved_trees, directory, prior, names, keep_neutral=keep_neutral))
    if restored:
        progress(f"Restored {len(restored)} unfinished trees; checking successful replacements together")
    measured = run_native(prepare_project, directory)
    # Each sweep restores at least one complete tree. Never waive a finding,
    # and never repeat identical conflict geometry indefinitely.
    for _ in range(min(4, len(names))):
        if interim_drc_acceptable(baseline, measured["drc"]):
            break
        conflicts = run_native(conflicting_replacement_nets, native_path(directory, ".kicad_pcb"),
                               baseline, measured["drc"], tuple(sorted(set(names) - restored)))
        if not conflicts:
            break
        progress("Restoring replacements conflicting with retained copper: " + ", ".join(conflicts))
        run_native(restore_unimproved_trees, directory, prior, conflicts, force=True)
        restored.update(conflicts)
        measured = run_native(prepare_project, directory)
    if ground_policy is not None:
        ground = run_native(prepare_ground_access, directory, ground_policy)
        if ground["proposed_stitches"]:
            progress(f"Post-route ground repair: {ground['proposed_stitches']} joins; "
                     f"{ground['before']} -> {ground['after']} temporary opens")
            measured = run_native(prepare_project, directory)
    return measured


def parity_protected_nets(summary: dict) -> tuple[str, ...]:
    """Require identified parity findings before attempting unrelated rework."""
    count = summary.get("parity_findings")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise RuntimeError("Incomplete schematic parity evidence")
    if not count:
        return ()
    nets, keys = summary.get("parity_blocked_nets"), summary.get("parity_keys")
    if (not isinstance(nets, list) or not nets
            or any(not isinstance(name, str) or not name for name in nets)
            or not isinstance(keys, list) or len(keys) != count
            or any(not isinstance(key, (list, tuple)) or len(key) != 3
                   or not isinstance(key[0], str) or not isinstance(key[1], str)
                   or not isinstance(key[2], (list, tuple)) or not key[2]
                   or any(not isinstance(uid, str) or not uid for uid in key[2]) for key in keys)):
        raise RuntimeError("Unidentified schematic parity nets prevent rework")
    return tuple(sorted(set(nets)))


def checked_copper_repair(directory: Path, *, ground_policy: dict | None = None,
                          progress=print) -> dict:
    """Repair permitted ground islands after local rework, then check the board."""
    if ground_policy is not None:
        ground = run_native(prepare_ground_access, directory, ground_policy)
        if ground["proposed_stitches"]:
            progress(f"Post-route ground repair: {ground['proposed_stitches']} joins; "
                     f"{ground['before']} -> {ground['after']} temporary opens")
    # Always recheck, including a ground search that found no useful addition.
    return run_native(prepare_project, directory)


def interim_drc_acceptable(baseline: dict, candidate: dict) -> bool:
    """Permit unchanged old errors in temporary rework, never new errors."""
    if (baseline.get("exclusions") != 0 or candidate.get("exclusions") != 0
            or candidate.get("parity_findings") != baseline.get("parity_findings")
            or candidate.get("ignored_checks") != baseline.get("ignored_checks")):
        return False
    try:
        if parity_protected_nets(candidate) != parity_protected_nets(baseline):
            return False
        if baseline["parity_findings"]:
            def key(item):
                return item[0], item[1], tuple(item[2])

            if Counter(map(key, baseline["parity_keys"])) != Counter(map(key, candidate["parity_keys"])):
                return False
        verify_unchanged_native_errors(baseline, candidate)
    except (KeyError, TypeError, RuntimeError):
        return False
    return True


def native_rejection_detail(baseline: dict, candidate: dict) -> str:
    """Explain an independent rejection without hiding rule changes or errors."""
    def keys(summary):
        return Counter((row[0], row[1], tuple(row[2]))
                       for row in summary.get("violation_keys", ()) if row[1] == "error")
    kinds = Counter()
    for row, count in (keys(candidate) - keys(baseline)).items():
        kinds[row[0]] += count
    if kinds:
        return "New native errors: " + ", ".join(f"{name}={count}" for name, count in sorted(kinds.items()))
    try:
        verify_unchanged_native_errors(baseline, candidate)
    except RuntimeError as error:
        return str(error)
    return (f"Native comparison rejected the repair (parity {baseline.get('parity_findings')}"
            f" -> {candidate.get('parity_findings')}; exclusions {baseline.get('exclusions')}"
            f" -> {candidate.get('exclusions')})")


@dataclass(frozen=True, slots=True)
class RepairPreparation:
    targets: tuple[str, ...]
    deferred: tuple[str, ...]
    baseline: dict
    board: bytes


@dataclass(slots=True)
class RepairPreparationCache:
    key: tuple | None = None
    value: RepairPreparation | None = None


def ordered_requests(queue: list, targets: tuple[str, ...], preferred: tuple[str, ...]) -> list[dict]:
    """Split scheduled requests by target, prioritizing reported failures."""
    target_set, preference = set(targets), set(preferred)
    requests = []
    seen = set()
    for group in queue:
        for request in group:
            if request.get("rip_up") or request.get("movable"):
                continue
            for net in request["nets"]:
                name = net["name"]
                if name not in target_set or name in seen:
                    continue
                seen.add(name)
                single = deepcopy(request)
                single["nets"] = [deepcopy(net)]
                single.pop("orders", None)
                single.pop("early_rework", None)
                requests.append(single)
    return sorted(requests, key=lambda request: request["nets"][0]["name"] not in preference)


def repair_work_queue(queue: list, preparation: dict, policy: dict) -> list:
    """Retain pending work and add all currently eligible, unfinished nets.

    A routing run's queue is consumed as attempts finish. It is not an
    inventory of everything that remains open, so it must not limit a later
    board-wide repair pass. Recompile the fallback from the checked preview;
    this also removes newly blocked nets from older queued requests.
    """
    if not isinstance(queue, list):
        raise ValueError("The retained routing queue must be a list")
    fresh = make_work_plan(preparation, policy, workers=1, max_batch_nets=1)["rounds"]
    eligible = {net["name"] for group in fresh for request in group for net in request["nets"]}
    pending = []
    for group in queue:
        retained = []
        for request in group:
            nets = [net for net in request["nets"] if net["name"] in eligible]
            if nets:
                retained.append({**deepcopy(request), "nets": deepcopy(nets)})
        if retained:
            pending.append(retained)
    return [*pending, *fresh]


def dimensions_preserved(before: list, after: list, floors: dict[str, tuple[int, int, int]]) -> bool:
    """Do not replace selected copper with narrower tracks or smaller vias."""
    for item, count in (Counter(after) - Counter(before)).items():
        if not count or item.net not in floors:
            continue
        width, size, drill = floors[item.net]
        if isinstance(item, Trace) and item.width < width:
            return False
        if isinstance(item, Via) and (item.size < size or item.drill < drill):
            return False
    return True


def same_copper(before: list, after: list) -> bool:
    """Compare geometric copper rather than serialization or item order."""
    return Counter(before) == Counter(after)


def _same_copper_files(before: Path, after: Path) -> bool:
    return same_copper(read_copper(before), read_copper(after))


def _dimensions_preserved_files(before: Path, after: Path,
                                floors: dict[str, tuple[int, int, int]]) -> bool:
    return dimensions_preserved(read_copper(before), read_copper(after), floors)


def rework_copper_preserved(before: list, after: list, excluded: tuple[str, ...], *,
                           additive_ground: bool = False) -> bool:
    """Permit checked ground additions, never removal or alteration of old ground."""
    if not isinstance(additive_ground, bool):
        raise ValueError("The ground-addition permission must be boolean")
    if not additive_ground:
        return protected_copper_unchanged(before, after, excluded)
    old_ground = Counter(item for item in before if item.net == "GND")
    new_ground = Counter(item for item in after if item.net == "GND")
    return not (old_ground - new_ground) and protected_copper_unchanged(
        before, after, tuple(name for name in excluded if name != "GND"))


def _protected_copper_unchanged_files(before: Path, after: Path, excluded: tuple[str, ...], *,
                                     additive_ground: bool = False) -> bool:
    return rework_copper_preserved(read_copper(before), read_copper(after), excluded,
                                  additive_ground=additive_ground)


def followup_exclusions(avoid: tuple[str, ...], affected: tuple[str, ...],
                        protected: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Temporarily defer failed nets, except the target and damaged neighbors."""
    affected_set, protected_set = set(_net_names(affected)), set(_net_names(protected))
    if affected_set & protected_set:
        raise ValueError("A repair cannot alter a protected net")
    return tuple(sorted((set(_net_names(avoid)) - affected_set) | protected_set))


def _failed(run_id: str, stage: str, result: dict | None = None, *,
            unchanged_scope: tuple[str, ...] | None = None, reason: str | None = None) -> dict:
    blockers = result.get("repair_blockers", {}) if result else {}
    if not isinstance(blockers, dict):
        raise ValueError("Invalid local-repair blocker inventory")
    outcome = {"status": "no_safe_improvement", "run_id": run_id, "stage": stage,
               "repair_blockers": tuple(sorted(blockers)),
               "repair_reasons": dict(sorted(blockers.items()))}
    detail = reason or (result and (result.get("reason") or result.get("status")))
    if detail:
        outcome["reason"] = str(detail)
    if unchanged_scope is not None:
        outcome["unchanged_followup_scope"] = unchanged_scope
    return outcome


def placement_signal_classes(classes: tuple[str, ...]) -> set[str]:
    """Only explicitly named signal classes can authorize a placement trial."""
    result = set()
    for name in _class_names(classes):
        upper = name.upper()
        if ((upper == "SIGNAL" or upper.endswith("_SIGNAL"))
                and not re.search(r"(?:^|_)(?:POWER|GROUND|GND)(?:_|$)", upper)):
            result.add(name)
    return result


def placement_outside_classes(board, allowed: set[str]) -> set[str]:
    """Keep unclassified and non-signal nets out of every placement stage."""
    result = set()
    for raw in board.GetNetsByName():
        name = str(raw)
        if not name:
            continue
        net = board.FindNet(name)
        if net is None or router_class(str(net.GetNetClassName())) not in allowed:
            result.add(name)
    return result


def eligible_signal_resistors(board, allowed: set[str], excluded: set[str], *,
                              allow_ground_anchor: bool = False,
                              allow_protected_anchor: bool = False) -> tuple[str, ...]:
    """Find signal resistors and optional private anchored-pose hypotheses."""
    import pcbnew
    from pcb.net_routing.relocation import (ground_anchor_pose, protected_anchor_pose,
                                            singleton_footprint_groups)

    result = []
    singleton_members = None
    for part in board.GetFootprints():
        ref = str(part.GetReference())
        if not re.fullmatch(r"R[0-9]+", ref) or part.GetLayer() != pcbnew.B_Cu or part.IsLocked():
            continue
        pads = list(part.Pads())
        if not pads or any(pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD
                           or not pad.IsOnLayer(pcbnew.B_Cu) or pad.IsOnLayer(pcbnew.F_Cu)
                           for pad in pads):
            continue
        names = {str(pad.GetNetname()) for pad in pads if pad.GetNetname()
                 and not str(pad.GetNetname()).startswith("unconnected-")}
        if not names:
            continue
        if names & excluded:
            pose = ground_anchor_pose(part, excluded) if allow_ground_anchor else None
            if pose is None and allow_protected_anchor:
                pose = protected_anchor_pose(part, excluded)
            if pose is None or names != {pose.signal_net, pose.anchor_net}:
                continue
            if part.GetParentGroup() is not None:
                if singleton_members is None:
                    singleton_members = singleton_footprint_groups(board)
                if part.m_Uuid.AsString() not in singleton_members:
                    continue
            names = {pose.signal_net}
        if all((net := board.FindNet(name)) is not None
               and router_class(str(net.GetNetClassName())) in allowed for name in names):
            result.append(ref)
    return tuple(sorted(result))


def placement_poses(board, candidate: dict, allowed: set[str], excluded: set[str], *,
                    allow_ground_anchor: bool = False,
                    allow_protected_anchor: bool = False) -> tuple | None:
    """Accept only explicit poses whose protected pads remain unchanged."""
    import pcbnew
    from pcb.net_routing.relocation import (ground_anchor_pose, protected_anchor_pose,
                                            singleton_footprint_groups)
    from pcb.route_optimizer import mm

    moves = candidate.get("movable")
    nets = candidate.get("nets")
    if not isinstance(moves, list) or not moves or not isinstance(nets, list):
        return None
    if any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in nets):
        return None
    declared = {item["name"] for item in nets}
    if declared & excluded:
        return None
    poses, seen = [], set()
    for move in moves:
        if not isinstance(move, dict):
            return None
        ref = move.get("reference")
        positions, angles = move.get("positions_mm"), move.get("rotations_deg")
        if (not isinstance(ref, str) or not re.fullmatch(r"R[0-9]+", ref) or ref in seen
                or not isinstance(positions, list) or len(positions) != 1
                or not isinstance(positions[0], list) or len(positions[0]) != 2
                or not isinstance(angles, list) or len(angles) != 1):
            return None
        values = (*positions[0], angles[0])
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) for value in values):
            return None
        part = board.FindFootprintByReference(ref)
        if part is None or part.GetLayer() != pcbnew.B_Cu or part.IsLocked():
            return None
        pads = list(part.Pads())
        if not pads or any(pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD
                           or not pad.IsOnLayer(pcbnew.B_Cu) or pad.IsOnLayer(pcbnew.F_Cu)
                           for pad in pads):
            return None
        pad_nets = {str(pad.GetNetname()) for pad in pads if pad.GetNetname()
                    and not str(pad.GetNetname()).startswith("unconnected-")}
        if not pad_nets:
            return None
        if pad_nets & excluded:
            pose = ground_anchor_pose(part, excluded) if allow_ground_anchor else None
            if pose is None and allow_protected_anchor:
                pose = protected_anchor_pose(part, excluded)
            if (pose is None or pad_nets != {pose.signal_net, pose.anchor_net}
                    or mm(values[0]) != mm(pose.x_mm) or mm(values[1]) != mm(pose.y_mm)
                    or (float(values[2]) - pose.angle_deg) % 360.0 != 0):
                return None
            if (part.GetParentGroup() is not None
                    and part.m_Uuid.AsString() not in singleton_footprint_groups(board)):
                return None
            pad_nets = {pose.signal_net}
        if not pad_nets <= declared or pad_nets & excluded:
            return None
        for name in pad_nets | declared:
            net = board.FindNet(name)
            if net is None or router_class(str(net.GetNetClassName())) not in allowed:
                return None
        poses.append((ref, *values))
        seen.add(ref)
    return tuple(poses)


def rework_policy(policy: dict, *, bottom_placement: bool, diagnose_obstructions: bool) -> dict:
    """Select bounded repair stages without mutating the durable configuration."""
    result = deepcopy(policy)
    recovery = {**result.get("recovery", {}), "placement_candidates": 6 if bottom_placement else 0}
    if not diagnose_obstructions:
        recovery["obstruction_trials"] = 0
    if bottom_placement:
        recovery["rip_up_candidates"] = 0
        result["_ground_anchor_candidates"] = True
        result["_protected_anchor_candidates"] = True
    result["recovery"] = recovery
    return result


def compile_candidate(directory: Path, queue: list, policy: dict, targets: tuple[str, ...],
                      preferred: tuple[str, ...], allowed: tuple[str, ...],
                      critical: tuple[str, ...], ordinal: int,
                      bottom_placement: bool = False, target_offset: int = 0,
                      target_limit: int = 8, defer_affected: tuple[str, ...] = (),
                      diagnose_obstructions: bool = False,
                      parity_nets: tuple[str, ...] = (),
                      tried_plans: tuple[str, ...] = ()) -> ReworkPlan | dict | None:
    """Compile one bounded copper hypothesis without touching the saved board."""
    import pcbnew

    if (isinstance(target_offset, bool) or not isinstance(target_offset, int) or target_offset < 0
            or isinstance(target_limit, bool) or not isinstance(target_limit, int) or target_limit < 1):
        raise ValueError("Placement target window must have a nonnegative offset and positive size")
    board = pcbnew.LoadBoard(str(native_path(directory, ".kicad_pcb")))
    if board is None:
        raise RuntimeError("KiCad could not load the private repair board")
    policy = rework_policy(policy, bottom_placement=bottom_placement,
                           diagnose_obstructions=diagnose_obstructions)
    allowed_set = placement_signal_classes(allowed) if bottom_placement else set(_class_names(allowed))
    if not allowed_set:
        raise ValueError("Placement needs signal classes" if bottom_placement else
                         "Copper rework needs at least one explicitly requested class")
    working_classes = working_connectivity_classes(board)
    excluded = set(critical) | set(_net_names(parity_nets)) | set(policy.get("exclude_nets", ())) | {"GND", ""}
    excluded |= protection_nets(board)
    excluded |= placement_outside_classes(board, allowed_set)
    deferred = set(_net_names(defer_affected))
    if bottom_placement:
        if not eligible_signal_resistors(board, allowed_set, excluded, allow_ground_anchor=True,
                                         allow_protected_anchor=True):
            return {"status": "no_eligible_bottom_resistors"}
    tracks = {item.m_Uuid.AsString(): item for item in board.GetTracks()}
    seen = set()
    found = 0
    requests = ordered_requests(queue, targets, preferred)
    if bottom_placement:
        requests = requests[target_offset:target_offset + target_limit]
    else:
        # Visit different eligible targets before spending additional trials on
        # alternatives for the same neighborhood. Protected and out-of-scope
        # targets do not consume a turn in this ordering.
        requests = [
            request for request in requests
            if request["nets"][0]["name"] not in excluded
            and board.FindNet(request["nets"][0]["name"]) is not None
            and router_class(str(board.FindNet(request["nets"][0]["name"]).GetNetClassName())) in allowed_set
        ]
        if requests:
            ordinal, offset = divmod(ordinal, len(requests))
            requests = requests[offset:] + requests[:offset]
    if tried_plans:
        # Failed-plan history already consumes the earlier alternatives.
        # Applying the batch ordinal again would skip still-untried repairs.
        ordinal = 0
    for request in requests:
        target = request["nets"][0]["name"]
        if target in excluded:
            continue
        target_net = board.FindNet(target)
        if target_net is None or router_class(str(target_net.GetNetClassName())) not in allowed_set:
            continue
        protected = tuple(sorted(excluded - {""}))
        compiled = compile_reroutes(board, request, policy, protected, reference_nets=critical)
        for candidate, description in zip(compiled["requests"], compiled["candidates"], strict=True):
            repair = candidate.get("rip_up", {})
            poses = (placement_poses(board, candidate, allowed_set, excluded, allow_ground_anchor=True,
                                     allow_protected_anchor=True)
                     if bottom_placement else ())
            if bottom_placement and poses is None:
                continue
            affected = set(repair) | ({item["name"] for item in candidate["nets"]} if poses else set())
            if (not affected or affected & excluded or (affected | {target}) & deferred or any(
                board.FindNet(name) is None
                or router_class(str(board.FindNet(name).GetNetClassName())) not in allowed_set
                for name in affected
            )):
                continue
            ids = frozenset(item["uuid"] for items in repair.values() for item in items)
            identity = target, ids, poses
            if (not ids and not poses) or identity in seen:
                continue
            seen.add(identity)
            attempt_key = repair_identity(target, ids, poses)
            if attempt_key in tried_plans:
                continue
            if not ids <= tracks.keys():
                raise RuntimeError("A repair references missing copper")
            if found < ordinal:
                found += 1
                continue
            floors: dict[str, tuple[int, int, int]] = {}
            for identifier in ids:
                item = tracks[identifier]
                name = str(item.GetNetname())
                if name not in affected:
                    raise RuntimeError("A repair selected copper on an unexpected net")
                width, size, drill = floors.get(name, (0, 0, 0))
                if isinstance(item, pcbnew.PCB_VIA):
                    if item.GetViaType() != pcbnew.VIATYPE_THROUGH:
                        raise ValueError("Only through-via repair is supported")
                    size = max(size, *(int(copper_width(item, layer=board.GetLayerID(layer)))
                                       for layer in enabled_copper_layers(board)))
                    drill = max(drill, int(item.GetDrillValue()))
                else:
                    width = max(width, int(copper_width(item, layer=item.GetLayer())))
                floors[name] = (width, size, drill)
            for name, (width, size, drill) in list(floors.items()):
                if (working_classes is None
                        or str(board.FindNet(name).GetNetClassName()).upper() not in working_classes):
                    continue
                configured = {**policy.get('route', {}), **policy.get('net_overrides', {}).get(name, {})}
                if name == target:
                    configured.update(request.get('shared', {}))
                    configured.update(request['nets'][0])
                if 'electrical' in configured:
                    # Independent explicit limits remain absolute; the WIP
                    # scope is permission to search, not electrical evidence.
                    from pcb.route_optimizer import mm
                    width = max(width, mm(configured.get('width_mm', .2)))
                    via = configured.get('via', {})
                    size = max(size, mm(via.get('size_mm', .6)))
                    drill = max(drill, mm(via.get('drill_mm', .3)))
                else:
                    # Mixed trunk/escape geometry is not a whole-net current
                    # model. The saved-rule materializer and native DRC enforce
                    # each new segment's actual scope; keep old via floors.
                    width = 0
                floors[name] = (width, size, drill)
            changes = copper_removal_changes(board, set(ids))
            if poses:
                changes.extend(native_pose_change(native_path(directory, ".kicad_pcb"), ref, x, y, angle)
                               for ref, x, y, angle in poses)
            modified = CampaignPlanner(directory).plan("pcb", changes).after
            if poses:
                weights = ReworkWeights.from_config(request.get("rework"))
                cost = placement_cost(board, {ref: (x, y, angle) for ref, x, y, angle in poses}, weights)
                cost += sum(weights.via if isinstance(tracks[uid], pcbnew.PCB_VIA)
                            else weights.track + weights.length_mm * tracks[uid].GetLength() / 1e6 for uid in ids)
            else:
                cost = float(description["estimated_rework_cost"])
            if not math.isfinite(cost) or cost < 0:
                raise ValueError("Invalid rework cost")
            protected_present = tuple(sorted(name for name in excluded if name and board.FindNet(name) is not None))
            return ReworkPlan(target, tuple(sorted({target} | affected)), len(ids), cost, modified, floors,
                              tuple(ref for ref, *_ in poses), protected_present, attempt_key)
    return None


def candidate_progress(event: dict) -> str:
    """Summarize a native candidate check without dumping item identifiers."""
    message = (f"Checking candidate: {event['opens']} opens; "
               f"{event['rejected_items']} proposed items need repair")
    details = []
    if event.get("affected_nets"):
        details.append("nets: " + ", ".join(sorted(event["affected_nets"])))
    if event.get("drc_reasons"):
        details.append("DRC: " + ", ".join(f"{name}={count}" for name, count in sorted(event["drc_reasons"].items())))
    if event.get("style_reasons"):
        details.append("geometry: " + ", ".join(
            f"{name}={count}" for name, count in sorted(event["style_reasons"].items())))
    elif event.get("style_items"):
        details.append(f"geometry: {event['style_items']}")
    return message + (" (" + "; ".join(details) + ")" if details else "")


def _candidate_batch(directory: Path, router: Path, timeout: int, cleanup: float,
                     ignored: tuple[str, ...], progress, *, engine: str = "fastroute",
                     targets: tuple[str, ...] = (), router_python: str = "auto",
                     grid_mm: float | None = None, expected_tool: str | None = None,
                     minimum_widths: dict[str, int] | None = None,
                     ground_contact_margin_mm: float = .5,
                     search_pad_neckdown: bool = False, optimize: bool = False) -> dict:
    with tempfile.TemporaryDirectory(prefix="autopcb-external-router-") as folder:
        return _candidate_batch_owned(directory, router, timeout, cleanup, ignored, progress,
                                      engine=engine, targets=targets, router_python=router_python,
                                      grid_mm=grid_mm, expected_tool=expected_tool,
                                      minimum_widths=minimum_widths, trial_root=Path(folder),
                                      ground_contact_margin_mm=ground_contact_margin_mm,
                                      search_pad_neckdown=search_pad_neckdown, optimize=optimize)


def _candidate_batch_owned(directory: Path, router: Path, timeout: int, cleanup: float,
                           ignored: tuple[str, ...], progress, *, engine: str,
                           targets: tuple[str, ...], router_python: str,
                           grid_mm: float | None, expected_tool: str | None,
                           minimum_widths: dict[str, int] | None, trial_root: Path,
                           ground_contact_margin_mm: float = .5,
                           search_pad_neckdown: bool = False, optimize: bool = False) -> dict:
    def report(event):
        if event["event"] == "pass":
            progress(candidate_progress(event))
        elif event["event"] == "repair":
            progress(f"Local repair for {event['net']}: {event['status']}")
        elif event["event"] == "off_angle_repair":
            progress(f"Replaced {event['segments']} off-angle traces with 45-degree candidates")
        elif event["event"] == "rule_width_materialization":
            progress(f"Restored saved widths on {event['changed_traces']} proposed traces")
        elif event["event"] == "native_violation_retry":
            progress("Retrying without nets implicated in native violations: " + ", ".join(event["nets"]))
        elif event["event"] == "style_blocker_retry":
            progress("Retrying without nets with ambiguous geometry: " + ", ".join(event["nets"]))
        elif event["event"] == "timeout":
            progress("Cleanup reached its budget; checking the safe remainder")

    def filtered(comparison: Path, candidate: Path) -> dict:
        return run_native(filter_proposal, directory, comparison, candidate, deferred_nets=ignored,
                          max_seconds=cleanup, progress=report, forward_progress=True,
                          allow_temporary_disconnect=True,
                          heartbeat=lambda: progress("Still checking the proposed copper"))

    try:
        if engine == "krt":
            from pcb.net_routing import krt_adapter

            if not targets or set(targets) & set(ignored):
                raise ValueError("Rework targets must be nonempty and exclude protected nets")
            raw = run_native(krt_adapter.preview, directory, router, router_python, targets, timeout,
                             grid_mm=grid_mm, reserve_references=True, salvage_strict=True,
                             ground_contact_margin_mm=ground_contact_margin_mm,
                             heartbeat=lambda: progress("Still searching the private repair"))
            if raw.get("tool") != expected_tool or not expected_tool:
                raise RuntimeError("The router installation changed during the coordinated repair")
            if raw.get("status") == "search_timeout":
                return {"status": "no_safe_proposal", "reason": raw["reason"]}
            if raw.get("status") == "strict_size_rejected":
                narrowed, salvage = raw.get("narrowed_nets"), raw.get("salvage_board")
                if not narrowed or not isinstance(salvage, bytes):
                    return {"status": "no_safe_proposal", "reason": raw["reason"],
                            "repair_blockers": {name: raw["reason"] for name in raw["failed_nets"]}}
                narrowed = _net_names(narrowed)
                if not set(narrowed) <= set(targets):
                    raise RuntimeError("The router's strict-size ledger contains an unexpected net")
                ignored = tuple(sorted(set(ignored) | set(narrowed)))
                raw["board"] = salvage
                progress(f"Discarding {len(narrowed)} narrowed replacement trees; "
                         "checking the others before original-tree restoration")
            with tempfile.TemporaryDirectory(prefix="autopcb-krt-rework-") as scratch:
                candidate = Path(scratch) / "candidate.kicad_pcb"
                candidate.write_bytes(raw["board"])
                result = filtered(native_path(directory, ".kicad_pcb"), candidate)
            return {**result, "router_fingerprint": expected_tool}
        if engine not in {"fastroute", "Freerouting"}:
            raise ValueError("Unsupported coordinated-repair router")
        raw = run_native(
            preview, directory, router, "auto", routing_pass_budget(timeout), timeout, skip_optimizer=not optimize,
            max_items=80 if engine == "Freerouting" else None,
            ignore_nets=ignored, ignore_high_speed=True, starts=1 if engine == "Freerouting" else 2,
            defer_native_checks=True, engine=engine, minimum_widths=minimum_widths,
            ground_contact_margin_mm=ground_contact_margin_mm,
            search_pad_neckdown=search_pad_neckdown,
            heartbeat=lambda: progress("Still searching the private repair"), trial_root=trial_root,
        )
        if engine == "Freerouting" and (not expected_tool or raw.get("freerouting_jar_sha256") != expected_tool):
            raise RuntimeError("The router installation changed during the coordinated repair")
        trial = Path(raw["trial_board"]).parent
        comparison = run_native(_roundtrip, native_path(directory, ".kicad_pcb"), trial,
                                router, "auto", min(timeout, 120), engine)
        return filtered(comparison, Path(raw["trial_board"]))
    except RuntimeError as error:
        # These are candidate-specific verification failures. Discard the
        # private candidate and try a different neighborhood; source changes,
        # worker crashes and other unexpected failures must still stop the run.
        rejected = {
            "Resumed candidate changed an existing copper net",
            "Resumed candidate broke an existing copper connection",
            "Resumed candidate introduced routing style issues",
            "The filtered candidate has new routing-style issues",
        }
        if (engine == "Freerouting" and str(error).startswith("Freerouting timed out after ")
                and "without a complete checkpoint" in str(error)):
            return {"status": "no_safe_proposal", "reason": str(error)}
        if str(error).partition(":")[0] not in rejected:
            raise
        return {"status": "candidate_rejected", "reason": str(error)}


def try_repair_backends(board_path: Path, engines: tuple[str, ...],
                        attempt: Callable[[str], dict], progress=print) -> tuple[dict, tuple[str, ...]]:
    """Try an explicit alternate only after a known, safely rejected proposal.

    Each engine receives the identical private input. Unexpected execution or
    validation failures propagate, and rejected copper never crosses this gate.
    """
    choices = tuple(dict.fromkeys(engines))
    if not 1 <= len(choices) <= 2:
        raise ValueError("Choose one repair backend and at most one explicit alternate")
    original = board_path.read_bytes()
    tried = []
    for index, engine in enumerate(choices):
        tried.append(engine)
        result = attempt(engine)
        if board_path.read_bytes() != original:
            raise RuntimeError("The private repair board changed during candidate evaluation")
        if result["status"] in {"filtered_preview", "deferred_connectivity_preview"}:
            return result, tuple(tried)
        if "board" in result:
            raise ValueError("A rejected repair returned candidate copper")
        if result["status"] not in {"candidate_rejected", "filter_stalled", "no_safe_proposal"}:
            return result, tuple(tried)
        if index + 1 < len(choices):
            reason = str(result.get("reason", result["status"]))
            progress(f"{engine} proposal rejected: {reason[:240]}")
            progress(f"Trying {choices[index + 1]} on the unchanged private repair board")
    return result, tuple(tried)


def preparation_key(inputs: dict, tools: dict, policy: str, board: bytes,
                    focus: tuple[str, ...], state: dict) -> tuple:
    """Bind reused preparation to the complete checked inputs and search scope."""
    blocked = state.get("blocked_nets", {})
    if not isinstance(blocked, dict):
        raise ValueError("The retained preview has an invalid blocked-net inventory")
    return (tuple(sorted(inputs.items())), tuple(sorted(tools.items())), policy,
            hashlib.sha256(board).hexdigest(), focus,
            json.dumps(blocked, sort_keys=True, separators=(",", ":")))


def prepare_repair(directory: Path, state: dict, focus: tuple[str, ...], key: tuple,
                   cache: RepairPreparationCache | None, progress) -> RepairPreparation:
    """Reuse a refilled private baseline only for identical sources and scope."""
    board = native_path(directory, ".kicad_pcb")
    if cache is not None and cache.key == key and cache.value is not None:
        board.write_bytes(cache.value.board)
        progress("Reusing the source-matched checked baseline")
        return cache.value
    progress("Inventorying unfinished nets")
    targets, deferred = run_native(_focus_deferred, directory, state, focus, ())
    baseline = run_native(prepare_project, directory)
    result = RepairPreparation(targets, deferred, baseline, board.read_bytes())
    if cache is not None:
        cache.key, cache.value = key, result
    return result


def run_one(project: Path, run_id: str, *, ordinal: int, timeout: int, cleanup: float,
            focus: tuple[str, ...], avoid: tuple[str, ...] = (),
            tried_unchanged: tuple[tuple[str, ...], ...] = (), bottom_placement: bool = False,
            target_offset: int = 0, target_limit: int = 8,
            baseline_cache: RepairPreparationCache | None = None,
            engine: str = "fastroute", router_python: str = "auto", grid_mm: float | None = None,
            krt_router: Path | None = None, freerouting_jar: Path | None = None,
            diagnose_obstructions: bool = False, followup_engine: str | None = None,
            followup_rounds: int = 1, renew_net_trees: int = 0, ground_first: bool = False,
            retry_exhausted: bool = False,
            include_connected: bool = False,
            ground_contact_margin_mm: float = .5,
            search_pad_neckdown: bool = False,
            optimize: bool = False,
            progress=print) -> dict:
    """Try one repair and retain only a strict, independently checked improvement."""
    if (isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0
            or isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 15
            or isinstance(cleanup, bool) or not isinstance(cleanup, (int, float))
            or not math.isfinite(cleanup) or cleanup <= 0):
        raise ValueError("Invalid rework search budget")
    if not isinstance(bottom_placement, bool) or not isinstance(diagnose_obstructions, bool):
        raise ValueError("Repair selection flags must be boolean")
    if not isinstance(retry_exhausted, bool):
        raise ValueError("Retry exhausted must be boolean")
    if (isinstance(renew_net_trees, bool) or not isinstance(renew_net_trees, int)
            or not 0 <= renew_net_trees <= 64 or (renew_net_trees and bottom_placement)):
        raise ValueError("Tree renewal needs 1 to 64 nets and fixed component placement")
    if not isinstance(ground_first, bool) or (ground_first and bottom_placement):
        raise ValueError("Ground-first repair requires fixed component placement")
    if not isinstance(optimize, bool):
        raise ValueError("Router optimization must be boolean")
    if not isinstance(include_connected, bool) or (include_connected and not renew_net_trees):
        raise ValueError("Connected-neighbor repair requires fixed-placement tree renewal")
    if isinstance(followup_rounds, bool) or not isinstance(followup_rounds, int) or not 1 <= followup_rounds <= 6:
        raise ValueError("Follow-up rounds must be an integer from 1 to 6")
    if engine not in {"fastroute", "krt", "Freerouting"} or followup_engine not in {None, "fastroute", "krt", "Freerouting"}:
        raise ValueError("Unsupported coordinated-repair router")
    followup_engine = followup_engine or engine
    if not isinstance(search_pad_neckdown, bool):
        raise ValueError("search_pad_neckdown must be boolean")
    if search_pad_neckdown and "krt" in {engine, followup_engine}:
        raise ValueError("Pad-neckdown proposals require fastroute or Freerouting")
    focus = _class_names(focus)
    avoid = _net_names(avoid)
    if not isinstance(tried_unchanged, tuple):
        raise ValueError("Tried follow-up scopes must be a tuple")
    tried_unchanged = tuple(_net_names(scope) for scope in tried_unchanged)
    if not focus:
        raise ValueError("Choose at least one focus class")
    project, run_id = project.resolve(), _run_id(run_id)
    assets = asset_root(project)
    policy, policy_hash = read_config(config_path(project, "autopcb.json"))
    state_path = results_path(project, "autoroute") / f"{run_id}.json"
    state_bytes = state_path.read_bytes()
    state = json.loads(state_bytes)
    if state.get("schema") != 1 or state.get("run_id") != run_id:
        raise ValueError("Invalid source routing preview")
    started = time.monotonic()
    unchanged_scope = None
    routers = {"krt": Path(krt_router).resolve() if krt_router else RouterCache.krt(),
               "fastroute": RouterCache.fastroute(),
               "Freerouting": Path(freerouting_jar).resolve() if freerouting_jar else RouterCache.freerouting()}
    fingerprints: dict[str, str] = {}
    if "krt" in {engine, followup_engine}:
        from pcb.net_routing import krt_adapter

        fingerprints["krt"] = run_native(krt_adapter.validate_installation, routers["krt"])
    if "Freerouting" in {engine, followup_engine}:
        fingerprints["Freerouting"] = hashlib.sha256(routers["Freerouting"].read_bytes()).hexdigest()
    stages = []
    with Snapshot(native_path(project, ".kicad_pcb")) as snapshot:
        inputs = _source_hashes(snapshot, project, assets)
        retained = decode_checkpoint(state["checkpoint"], inputs, snapshot.tool_hashes,
                                     policy_hash, allow_tool_changes=True)
        settings = {
            "engine": engine, "followup_engine": followup_engine,
            "router_identity": {
                name: fingerprints.get(name) or (
                    hashlib.sha256(routers[name].read_bytes()).hexdigest()
                    if routers[name].is_file() else "missing")
                for name in {engine, followup_engine}
            },
            "router_python": router_python, "grid_mm": grid_mm,
            "focus": focus, "avoid": avoid, "timeout": timeout, "cleanup": cleanup,
            "bottom_placement": bottom_placement, "renew_net_trees": renew_net_trees,
            "ground_first": ground_first, "followup_rounds": followup_rounds,
            "include_connected": include_connected,
            "ground_contact_margin_mm": ground_contact_margin_mm,
            "search_pad_neckdown": search_pad_neckdown,
            "optimize": optimize,
        }
        retry_key = fast_retry_signature(state, inputs, snapshot.tool_hashes, policy_hash, settings)
        tried_plans = () if retry_exhausted else rework_attempts(state, retry_key)
        if len(tried_plans) == MAX_REWORK_ATTEMPTS:
            return {"status": "no_rework_candidate", "run_id": run_id,
                    "reason": "The bounded failed-plan history is full; search remains unresolved"}
        if tried_plans:
            progress(f"Skipping {len(tried_plans)} prior failed repair plans on unchanged inputs")
        schematic = (native_path(snapshot.directory, ".kicad_sch")).read_bytes()
        protected = ProtectedState.capture(snapshot.original, schematic, preserve_all_placement=not bottom_placement)
        if protected.differences(retained, schematic):
            raise RuntimeError("The retained preview changed protected design items")
        snapshot.board_path.write_bytes(retained)
        key = preparation_key(inputs, snapshot.tool_hashes, policy_hash, retained, focus, state)
        prepared = prepare_repair(snapshot.directory, state, focus, key, baseline_cache, progress)
        targets, deferred = prepared.targets, prepared.deferred
        baseline, retained = prepared.baseline, prepared.board
        prior = snapshot.directory / "rework-prior.kicad_pcb"
        prior.write_bytes(retained)
        if baseline["drc"]["open_connections"] != state["best_preview_opens"]:
            raise RuntimeError("The retained preview no longer matches native DRC")
        if not targets:
            return {"status": "no_focused_nets", "run_id": run_id}
        parity_nets = parity_protected_nets(baseline["drc"])
        critical = tuple(baseline["reference_copper"]["high_speed_nets"])
        progress(f"Selecting a bottom signal-resistor placement in target window {target_offset + 1}"
                 f"–{target_offset + target_limit}" if bottom_placement
                 else "Diagnosing obstructing copper" if diagnose_obstructions
                 else "Selecting nearby copper")
        repair_queue = repair_work_queue(state["queue"], baseline, policy)
        if renew_net_trees:
            plan = run_native(compile_tree_renewal, snapshot.directory, targets, focus, critical,
                              ordinal, renew_net_trees, avoid, parity_nets, tried_plans, include_connected)
        else:
            plan = run_native(compile_candidate, snapshot.directory, repair_queue, policy,
                              targets, (), focus, critical, ordinal, bottom_placement, target_offset, target_limit, avoid,
                              diagnose_obstructions, parity_nets, tried_plans,
                              heartbeat=lambda: progress("Still selecting a repair"))
        if isinstance(plan, dict) and plan.get("status") == "no_eligible_bottom_resistors":
            return {"status": "no_eligible_bottom_resistors", "run_id": run_id}
        if plan is None:
            return {"status": "no_rework_candidate", "run_id": run_id}
        if not isinstance(plan, ReworkPlan):
            raise ValueError("Unexpected placement-search result")
        def failed(stage: str, result: dict | None = None, **details) -> dict:
            outcome = _failed(run_id, stage, result, **details)
            outcome["rework_retry"] = {
                "key": retry_key, "settings": settings,
                "attempt": plan.attempt_key or hashlib.sha256(plan.board).hexdigest(),
            }
            return outcome

        if protected.differences(plan.board, schematic):
            raise RuntimeError("The repair changed protected design items")
        snapshot.board_path.write_bytes(plan.board)
        if ground_first:
            ground = run_native(prepare_ground_access, snapshot.directory, policy)
            progress(f"Ground-first private repair: {ground['proposed_stitches']} joins; "
                     f"{ground['before']} -> {ground['after']} temporary opens before rebuilding neighbors")
        if parity_nets:
            verify_parity_copper_unchanged(baseline["drc"], read_copper(prior), read_copper(snapshot.board_path))
        exported_nets = set(targets) | set(deferred)
        routing_excluded = tuple(sorted(set(plan.excluded) & exported_nets))
        ignored = tuple(sorted((exported_nets - set(plan.affected)) | set(routing_excluded)))
        progress(f"Reworking {plan.target} and {len(plan.affected) - 1} neighboring nets; "
                 f"{plan.items} copper items "
                 f"(estimated rework cost {plan.cost:.1f})")
        if plan.moves:
            progress("Private bottom placement: " + ", ".join(plan.moves))
        repair_targets = tuple(sorted((set(plan.affected) & exported_nets) - set(ignored)))
        minimum_widths = {name: dimensions[0] for name, dimensions in plan.floors.items() if dimensions[0] > 0}
        repair_input = snapshot.board_path.read_bytes()

        def attempt(repair_engine: str) -> dict:
            proposal = _candidate_batch(
                snapshot.directory, routers[repair_engine], timeout, cleanup, ignored, progress,
                engine=repair_engine, targets=repair_targets, router_python=router_python,
                grid_mm=grid_mm, expected_tool=fingerprints.get(repair_engine), minimum_widths=minimum_widths,
                ground_contact_margin_mm=ground_contact_margin_mm, search_pad_neckdown=search_pad_neckdown,
                optimize=optimize)
            if snapshot.board_path.read_bytes() != repair_input:
                raise RuntimeError("The private repair board changed during candidate evaluation")
            if proposal["status"] not in {"filtered_preview", "deferred_connectivity_preview"}:
                return proposal
            try:
                snapshot.board_path.write_bytes(proposal["board"])
                if renew_net_trees:
                    checked = checked_tree_fallback(
                        snapshot.directory, prior, plan.affected, baseline["drc"],
                        keep_neutral=include_connected, ground_policy=policy if ground_first else None,
                        progress=progress)
                else:
                    checked = checked_copper_repair(snapshot.directory,
                                                   ground_policy=policy if ground_first else None,
                                                   progress=progress)
                if not interim_drc_acceptable(baseline["drc"], checked["drc"]):
                    return {**{key: value for key, value in proposal.items() if key != "board"},
                            "status": "candidate_rejected",
                            "reason": native_rejection_detail(baseline["drc"], checked["drc"])}
                return {**proposal, "board": snapshot.board_path.read_bytes(), "checked_intermediate": checked}
            finally:
                # Restoring failed trees can introduce conflicts absent from
                # the stripped-board check. Include that check in the backend
                # attempt, then give any alternate the identical clean input.
                snapshot.board_path.write_bytes(repair_input)

        result, attempted = try_repair_backends(
            snapshot.board_path, (engine, followup_engine), attempt, progress)
        stages.extend((name, fingerprints.get(name)) for name in attempted)
        if result["status"] not in {"filtered_preview", "deferred_connectivity_preview"}:
            return failed("repair", result)
        if result["status"] == "deferred_connectivity_preview":
            progress("Temporary connections changed; the complete repair must restore the original board")
        snapshot.board_path.write_bytes(result["board"])
        intermediate = result["checked_intermediate"]
        findings = intermediate["drc"]
        for followup_index in range(followup_rounds):
            if findings["open_connections"] < baseline["drc"]["open_connections"]:
                break
            excluded = followup_exclusions(avoid, plan.affected, routing_excluded)
            if followup_index == 0 and not plan.moves and run_native(_same_copper_files, prior, snapshot.board_path):
                if excluded in tried_unchanged:
                    progress("Repair restored the same copper; this search scope was already tried")
                    return failed("unchanged_repair", result)
                unchanged_scope = excluded
                progress("Repair restored the same copper; checking this search scope once")
            progress("Rebuilding unfinished nets, including any interrupted neighbors")
            fresh, deferred = run_native(_focus_deferred, snapshot.directory, state, focus, excluded)
            if not fresh:
                return failed("followup", result, unchanged_scope=unchanged_scope)
            if followup_rounds > 1:
                progress(f"Follow-up {followup_index + 1}/{followup_rounds}")
            if followup_engine != stages[-1][0]:
                progress(f"Trying {followup_engine} on the intermediate repair")
            stages.append((followup_engine, fingerprints.get(followup_engine)))
            before_followup = snapshot.directory / f"rework-followup-{followup_index}.kicad_pcb"
            if followup_index + 1 < followup_rounds:
                before_followup.write_bytes(snapshot.board_path.read_bytes())
            result = _candidate_batch(snapshot.directory, routers[followup_engine], timeout, cleanup,
                                      tuple(sorted(set(deferred) | set(routing_excluded))), progress,
                                      engine=followup_engine, targets=fresh, router_python=router_python,
                                      grid_mm=grid_mm,
                                      expected_tool=fingerprints.get(followup_engine), minimum_widths=minimum_widths,
                                      ground_contact_margin_mm=ground_contact_margin_mm,
                                      search_pad_neckdown=search_pad_neckdown, optimize=optimize)
            if result["status"] not in {"filtered_preview", "deferred_connectivity_preview"}:
                return failed("followup", result, unchanged_scope=unchanged_scope)
            if result["status"] == "deferred_connectivity_preview":
                progress("Temporary connections changed; the complete repair must restore the original board")
            snapshot.board_path.write_bytes(result["board"])
            if renew_net_trees:
                intermediate = checked_tree_fallback(snapshot.directory, prior, plan.affected,
                                                       baseline["drc"], keep_neutral=include_connected,
                                                       ground_policy=policy if ground_first else None, progress=progress)
            else:
                intermediate = checked_copper_repair(snapshot.directory,
                                                    ground_policy=policy if ground_first else None,
                                                    progress=progress)
            findings = intermediate["drc"]
            if not interim_drc_acceptable(baseline["drc"], findings):
                return failed("native_checks", result, unchanged_scope=unchanged_scope,
                               reason=native_rejection_detail(baseline["drc"], findings))
            if (followup_index + 1 < followup_rounds
                    and findings["open_connections"] >= baseline["drc"]["open_connections"]
                    and run_native(_same_copper_files, before_followup, snapshot.board_path)):
                progress("The follow-up restored the same copper; stopping repeated search")
                break
        opens = findings["open_connections"]
        if opens >= baseline["drc"]["open_connections"]:
            return failed("connectivity", result, unchanged_scope=unchanged_scope)
        candidate = snapshot.board_path.read_bytes()
        if protected.differences(candidate, schematic):
            raise RuntimeError("The repair changed protected design items")
        if not run_native(_dimensions_preserved_files, prior, snapshot.board_path, plan.floors):
            return failed("copper_dimensions", result, unchanged_scope=unchanged_scope)
        if plan.excluded and not run_native(_protected_copper_unchanged_files, prior,
                                            snapshot.board_path, plan.excluded, additive_ground=ground_first):
            return failed("protected_copper", result, unchanged_scope=unchanged_scope)
        try:
            verify_resume_reports(baseline["drc"], findings, expected_opens=opens,
                                  stable_connection_items=stable_connection_items(retained, candidate))
            run_native(_verify_resume_connectivity, snapshot.directory, retained)
        except RuntimeError as error:
            if not str(error).startswith("Resumed candidate"):
                raise
            return failed("preservation", result, unchanged_scope=unchanged_scope,
                           reason=str(error))
        snapshot.check()
        if state_path.read_bytes() != state_bytes:
            raise RuntimeError("The source preview changed during repair")
        if any(name == "krt" for name, _ in stages) and run_native(
            krt_adapter.validate_installation, routers["krt"]
        ) != fingerprints["krt"]:
            raise RuntimeError("The router installation changed during the coordinated repair")
        if (any(name == "Freerouting" for name, _ in stages)
                and hashlib.sha256(routers["Freerouting"].read_bytes()).hexdigest() != fingerprints["Freerouting"]):
            raise RuntimeError("The router installation changed during the coordinated repair")
        candidate_path = snapshot.directory / "rework-candidate.kicad_pcb"
        candidate_path.write_bytes(candidate)
        progress("Independently validating the combined repair")
        final_engine, final_fingerprint = stages[-1]
        mixed_provenance = {"router_stages": tuple(stages)} if len(stages) > 1 else {}
        return run_native(retain_candidate, project, run_id, candidate_path,
                          routing_seconds=time.monotonic() - started,
                          expected_previous=state_bytes, engine=final_engine, router_fingerprint=final_fingerprint,
                          **mixed_provenance,
                          progress=progress, forward_progress=True,
                          heartbeat=lambda: progress("Still validating the combined repair"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--batches", type=int, default=2)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--cleanup-seconds", type=float, default=75)
    parser.add_argument("--engine", choices=("fastroute", "krt", "Freerouting"), default="fastroute")
    parser.add_argument("--router-python", default="auto")
    parser.add_argument("--router-grid-mm", type=float)
    parser.add_argument("--krt-router", type=Path,
                        help="allowlisted KiCadRoutingTools checkout for either routing stage")
    parser.add_argument("--freerouting-jar", type=Path,
                        help="Freerouting JAR for either routing stage")
    parser.add_argument("--focus-open-class", action="append", required=True)
    parser.add_argument("--diagnose-obstructions", action="store_true",
                        help="identify candidate blocking copper with bounded relaxed-path searches")
    parser.add_argument("--followup-engine", choices=("fastroute", "krt", "Freerouting"),
                        help="explicit alternate after a rejected initial proposal or intermediate repair")
    parser.add_argument("--followup-rounds", type=int, default=1,
                        help="maximum consecutive searches from changed intermediate layouts (1 to 6)")
    parser.add_argument("--bottom-placement", action="store_true",
                        help="try guarded bottom signal-resistor movements with coordinated rerouting")
    parser.add_argument("--renew-net-trees", type=int, default=0, metavar="NETS",
                        help="rebuild complete unfinished net trees in batches, keeping all placement fixed")
    parser.add_argument("--ground-first", action="store_true",
                        help="reconnect WIP ground access before and after fixed-placement copper repair")
    parser.add_argument("--optimize", action="store_true",
                        help="run the backend's bounded post-route optimizer before native cleanup")
    parser.add_argument("--retry-exhausted", action="store_true",
                        help="explicitly retry failed plans against identical inputs and search settings")
    parser.add_argument("--include-connected", action="store_true",
                        help="rebuild nearby permitted connected signal trees with --renew-net-trees")
    parser.add_argument("--ground-contact-guard-mm", type=float, default=.5,
                        help="extra search-only ground-pad margin; native physical rules remain unchanged")
    parser.add_argument("--search-pad-neckdown", action="store_true",
                        help="propose smaller pad exits; saved width rules and native checks still apply")
    parser.add_argument("--start-target", type=int, default=0,
                        help="zero-based first target for bottom-placement windows")
    parser.add_argument("--target-window", type=int, default=8,
                        help="maximum number of targets inspected in each bottom-placement batch")
    args = parser.parse_args(argv)
    if args.batches < 1 or args.start_index < 0 or args.start_target < 0 or args.target_window < 1:
        parser.error("batches and target-window must be positive; start indices must be nonnegative")
    run_id = args.run
    deferred: set[str] = set()
    tried_unchanged: set[tuple[str, ...]] = set()
    baseline_cache = RepairPreparationCache()
    try:
        with quiet_duplicate_image_handlers():
            for index in range(args.start_index, args.start_index + args.batches):
                started = time.monotonic()
                print(f"Coordinated repair {index + 1}", flush=True)
                try:
                    target_offset = args.start_target + (index - args.start_index) * args.target_window
                    result = run_one(args.project, run_id, ordinal=0 if args.bottom_placement else index,
                                     timeout=args.timeout,
                                     cleanup=args.cleanup_seconds, focus=tuple(args.focus_open_class),
                                     avoid=tuple(sorted(deferred)),
                                     tried_unchanged=tuple(sorted(tried_unchanged)),
                                     bottom_placement=args.bottom_placement,
                                     target_offset=target_offset, target_limit=args.target_window,
                                     baseline_cache=baseline_cache,
                                     engine=args.engine, router_python=args.router_python, grid_mm=args.router_grid_mm,
                                     krt_router=args.krt_router, freerouting_jar=args.freerouting_jar,
                                     diagnose_obstructions=args.diagnose_obstructions,
                                     followup_engine=args.followup_engine, followup_rounds=args.followup_rounds,
                                     renew_net_trees=args.renew_net_trees, ground_first=args.ground_first,
                                     retry_exhausted=args.retry_exhausted,
                                     include_connected=args.include_connected,
                                     ground_contact_margin_mm=args.ground_contact_guard_mm,
                                     search_pad_neckdown=args.search_pad_neckdown,
                                     optimize=args.optimize,
                                     progress=lambda message: print(message, flush=True))
                except Exception:
                    try:
                        record_unimproved_batch(args.project, run_id, time.monotonic() - started)
                    except (OSError, RuntimeError, ValueError) as error:
                        print(f"Could not record failed repair time: {error}", file=sys.stderr)
                    raise
                if result["status"] == "private_preview_retained":
                    run_id = result["run_id"]
                    print(f"Checked private preview: {result['preview_opens']} opens; run {run_id}", flush=True)
                    break
                unchanged_scope = result.get("unchanged_followup_scope")
                if unchanged_scope is not None:
                    tried_unchanged.add(_net_names(unchanged_scope))
                newly_deferred = set(result.get("repair_blockers", ())) - deferred
                if newly_deferred:
                    deferred.update(newly_deferred)
                    print("Temporarily deferring unrelated failed nets: "
                          + ", ".join(sorted(newly_deferred)), flush=True)
                record_unimproved_batch(args.project, run_id, time.monotonic() - started,
                                        rework_retry=result.get("rework_retry"))
                detail = f"; stage: {result['stage']}" if result.get("stage") else ""
                if result.get("reason"):
                    detail += f"; {result['reason']}"
                print(f"Result: {result['status']}{detail}", flush=True)
                for name, reason in result.get("repair_reasons", {}).items():
                    print(f"  {name}: {reason}", flush=True)
                if result["status"] in {"no_focused_nets", "no_eligible_bottom_resistors"} or (
                    result["status"] == "no_rework_candidate" and not args.bottom_placement
                ):
                    break
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Coordinated repair stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

