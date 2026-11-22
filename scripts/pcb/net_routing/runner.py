"""Execute resumable board-wide searches on a private native-board snapshot.

This preview runner never publishes a PCB. Electrical, paired-signal and full
reference-path verification remain required before a publication path exists.
"""

from __future__ import annotations

from core.project import native_path

import collections
import concurrent.futures
import hashlib
import json
import math
import multiprocessing
import os
import tempfile
import time
import uuid
from copy import deepcopy
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pcbnew

from core.kicad_campaign import CampaignPlanner
from core.paths import asset_root, config_path, data_path, results_path, stored_result
from pcb.checkpoints import read_config
from pcb.net_routing.batch import _search, preview_round
from pcb.net_routing.controller import decode_checkpoint, encode_checkpoint, make_work_plan, new_style_issues
from pcb.net_routing.inventory import inventory_board
from pcb.net_routing.isolation import run_native
from pcb.net_routing.native import Snapshot, sha
from pcb.net_routing.prepare import prepare_project
from pcb.move_scope import MoveScope
from pcb.net_routing.protection import ProtectedState, unqualified_working_nets
from pcb.net_routing.reference import reference_report
from pcb.net_routing.reference_repair import compile_reference_repairs
from pcb.net_routing.recovery import (
    RecoveryPolicy, next_stage, prioritize_recovery_requests, recovery_preference, renew_queue, search_key,
)
from pcb.net_routing.reroute import compile_reroutes
from pcb.net_routing.scheduler import overlaps, work_from_board
from pcb.net_routing.style import audit_style, normalize_segment_identities
from pcb.route_validation import (
    _broken_existing_connections, _net_identities, _new_dangling_indices, _segment_identities,
    added_violation_keys,
)
from core.compat import strict_zip


class PreviewStatus:
    @staticmethod
    def stopped(state: dict) -> str:
        if state["queue"]:
            return "paused"
        opens = state.get("best_preview_opens")
        return "preview_zero_opens" if type(opens) is int and opens == 0 else "stalled"


def _source_hashes(snapshot: Snapshot, project: Path, assets: Path) -> dict[str, str]:
    result = {snapshot.input_name(path).as_posix(): digest for path, digest in snapshot.fingerprints.items()}
    for name in ("POWER_LOAD_BUDGET.csv", "STACKUP_CURRENT.json"):
        path = data_path(project, name)
        if path.is_file():
            result[snapshot.input_name(path).as_posix()] = sha(path)
        else:
            # Keep the source key used by existing runs when an input is absent.
            result[(assets / "design" / name).relative_to(project).as_posix()] = "absent"
    policy_path = config_path(project, "autopcb.json")
    if policy_path.is_file():
        policy, _ = read_config(policy_path)
        root = project.resolve()
        pending = [policy.get("zone_stitch", {})]
        while pending:
            item = pending.pop()
            if isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, dict):
                evidence = item.get("evidence")
                source = evidence.get("source") if isinstance(evidence, dict) else None
                if isinstance(source, str) and source:
                    path = (root / source).resolve()
                    if path.is_relative_to(root):
                        result[path.relative_to(root).as_posix()] = sha(path) if path.is_file() else "absent"
                pending.extend(item.values())
    return result


def _check_sources(snapshot: Snapshot, project: Path, assets: Path, expected: dict, policy: Path, policy_hash: str) -> None:
    snapshot.check()
    if _source_hashes(snapshot, project, assets) != expected or not policy.is_file() or sha(policy) != policy_hash:
        raise RuntimeError("Routing inputs or policy changed; discard this result and retry")


def _acquire_lock(acquire, *, wait_seconds: float, progress=None) -> None:
    """Retry a busy OS lock briefly; never proceed without ownership."""
    if (not callable(acquire) or isinstance(wait_seconds, bool)
            or not isinstance(wait_seconds, (int, float)) or not math.isfinite(wait_seconds)
            or wait_seconds < 0 or (progress is not None and not callable(progress))):
        raise ValueError("Invalid lock acquisition or wait time")
    deadline = time.monotonic() + wait_seconds
    announced = False
    while True:
        try:
            acquire()
            return
        except OSError as error:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Another AutoPCB run is active") from error
            if progress and not announced:
                progress("Waiting for another AutoPCB transaction to finish")
                announced = True
            time.sleep(min(0.25, remaining))


@contextmanager
def _exclusive(path: Path, *, wait_seconds: float = 0, progress=None):
    """Use an OS-owned lock that is released even when the process is killed."""
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            _acquire_lock(lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1),
                          wait_seconds=wait_seconds, progress=progress)
        else:
            import fcntl
            _acquire_lock(lambda: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB),
                          wait_seconds=wait_seconds, progress=progress)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _save(path: Path, state: dict) -> None:
    encoded = json.dumps(state, separators=(",", ":"), allow_nan=False).encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".autoroute-", suffix=".tmp", delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _progress_sample(state: dict, board_hash: str, counts: dict, geometry: dict, elapsed: float) -> None:
    """Record native measurements, not search estimates or presumed improvements."""
    from review.routing_progress import ProgressPoint

    point = ProgressPoint(
        datetime.now(timezone.utc).isoformat(), counts["open_connections"], counts["errors"],
        counts["warnings"], counts.get("parity_findings", 0), board_hash, "",
        "Validated private preview", "Native candidate validation",
        geometry["track_length_mm"], geometry["via_count"], geometry["degree_two_joints"],
        "Native candidate geometry", routing_run_seconds=elapsed,
        optimization_evidence="Measured run wall time; private preview",
    )
    state.setdefault("progress_samples", []).append(asdict(point))


def _publish_progress(project: Path, inputs: dict, state: dict, progress=None) -> None:
    from review.routing_progress import publish_preview

    try:
        publish_preview(project, inputs, state["run_id"], state.get("progress_samples", []))
        state.pop("progress_export_error", None)
    except (OSError, ValueError, RuntimeError) as error:
        # Preserve the last validated checkpoint and stop before more search.
        state["progress_export_error"] = str(error)
        if progress:
            progress({"event": "stage", "message": f"Routing checkpoint retained; progress export needs retry: {error}"})
        raise


def _progress_elapsed(state: dict, board_hash: str) -> None:
    """Advance runtime for the identical validated board without inventing routes."""
    previous = state["progress_samples"][-1]
    if previous["pcb_sha256"] != board_hash:
        raise RuntimeError("Progress measurements do not belong to the retained preview")
    if previous["routing_run_seconds"] == state["elapsed_seconds"]:
        return
    state["progress_samples"].append({
        **previous, "checked_at": datetime.now(timezone.utc).isoformat(),
        "routing_run_seconds": state["elapsed_seconds"],
        "change": "Retained validated preview; search time updated",
    })


@contextmanager
def _record_failures(path: Path, state: dict):
    try:
        yield
    except BaseException as error:
        # Retain the last atomically committed queue and candidate. In-memory
        # work from an interrupted round was never accepted.
        committed = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else state
        committed["status"] = ("interrupted" if isinstance(error, KeyboardInterrupt)
                               else "resource_blocked" if isinstance(error, MemoryError) else "failed")
        committed["failure"] = str(error) or type(error).__name__
        if state.get("resource_limits") is not None:
            committed["resource_limits"] = state["resource_limits"]
        if state.get("progress_export_error") is not None:
            committed["progress_export_error"] = state["progress_export_error"]
        _save(path, committed)
        raise


def _run_id(value: str) -> str:
    if not isinstance(value, str) or len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("Run ID must be 32 lowercase hexadecimal characters")
    return value


def insert_reference_repairs(plan: dict, repairs: dict, policy: dict) -> None:
    """Close unfinished connections before revisiting existing reference gaps.

    New reference damage remains an independent rejection at every stage.
    Existing gaps stay in the queue and remain completion blockers.
    """
    plan["rounds"].extend(repairs["rounds"])
    plan["island_counts"].update(repairs["island_counts"])


def is_preview_improvement(previous_opens: int, candidate_opens: int, reference: dict) -> bool:
    if reference.get("status") == "degraded":
        return False
    if candidate_opens < previous_opens:
        return True
    if candidate_opens != previous_opens:
        return False
    counts = reference.get("comparable_missing_samples")
    return (isinstance(counts, list) and len(counts) == 2
            and all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in counts)
            and counts[1] < counts[0])


def _parallel_repair_alternatives(candidates: list[dict], workers: int) -> tuple[list[dict], list[dict]]:
    """Try independent repair kinds concurrently without merging their copper."""
    if not isinstance(candidates, list) or workers not in {1, 2}:
        raise ValueError("Repair alternatives need a list and one or two workers")
    if not candidates:
        return [], []
    indices = [0]
    if workers > 1 and len(candidates) > 1:
        first_is_move = bool(candidates[0].get("movable"))
        other = next((i for i in range(1, len(candidates))
                      if bool(candidates[i].get("movable")) != first_is_move), 1)
        indices.append(other)
    return ([candidates[i] for i in indices],
            [item for i, item in enumerate(candidates) if i not in indices])


def _select_alternative_result(results: list[dict], previous_opens: int) -> dict:
    """Select one checked preview; alternative board changes are never combined."""
    if not results:
        raise ValueError("A repair selection needs at least one result")
    valid = [
        item for item in results
        if item.get("status") == "validated_preview"
        and not item.get("new_style_issues")
        and is_preview_improvement(
            previous_opens, item["validation"]["candidate_unconnected"],
            item["validation"].get("reference_comparison", {}),
        )
    ]
    if valid:
        return min(valid, key=lambda item: item["validation"]["candidate_unconnected"])
    neutral = next((item for item in results if _neutral_candidate_eligible(item, previous_opens)), None)
    if neutral is not None:
        return neutral
    return next((item for item in results if item.get("status") == "validated_preview"),
                next((item for item in results if item.get("status") == "rejected"), results[0]))


def summarize_attempts(result: dict) -> list[dict]:
    summaries = []
    for attempt in result.get("attempts", []):
        search = attempt.get("search", {})
        orders = [
            {"order": trial.get("order", [])[:20],
             "nets_reached": trial.get("nets_reached", 0),
             "complete_candidates": trial.get("complete_candidates", 0)}
            for placement in attempt.get("trials", [])[:4]
            for trial in placement.get("orders", [])[:4]
        ]
        ignored_zones = max(
            (placement.get("ignored_zone_count", 0)
             for placement in attempt.get("trials", [])), default=0
        )
        zone_count = max(
            (placement.get("zone_count", placement.get("ignored_zone_count", 0))
             for placement in attempt.get("trials", [])), default=0
        )
        zone_modes = {placement.get("zone_connectivity", "unfilled")
                      for placement in attempt.get("trials", [])}
        summaries.append({"status": attempt.get("status"), "reason": attempt.get("reason"),
                          "limits_hit": search.get("limits_hit", []),
                          "failures": search.get("failures", {}),
                          "failure_details": search.get("failure_details", {}),
                          "checkpoints": attempt.get("checkpoints", []),
                          "orders": orders,
                          "ignored_zone_count": ignored_zones,
                          "zone_count": zone_count,
                          "zone_connectivity": "refilled" if zone_modes == {"refilled"} else "unfilled",
                          "rejected_candidates": attempt.get("rejected_candidates", [])[:3],
                          "routing_cost": attempt.get("routing_cost"),
                          "rework_cost": attempt.get("rework_cost"),
                          "path_calls": search.get("path_calls"),
                          "elapsed_seconds": search.get("elapsed_seconds")})
    validation = result.get("validation", {})
    if result.get("status") == "rejected" and validation:
        counts = collections.Counter(
            f"{item.get('severity', 'finding')} {item.get('type', 'unknown')}"
            for item in validation.get("new_violations", []))
        reasons = [f"{count} new {kind}" for kind, count in sorted(counts.items())]
        representative_findings = {}
        for item in validation.get("new_violations", []):
            representative_findings.setdefault(
                (item.get("severity"), item.get("type")), item
            )
        parity = validation.get("new_schematic_parity", [])
        if parity:
            reasons.append(f"{len(parity)} new schematic parity findings")
        if validation.get("new_style_issues"):
            reasons.append("new routing style issues: " + ", ".join(validation["new_style_issues"]))
        for field, label in (("net_identity_changes", "changed net assignments"),
                             ("broken_existing_connections", "broken existing connections")):
            if validation.get(field):
                reasons.append(f"{len(validation[field])} {label}")
        before, after = validation.get("baseline_unconnected"), validation.get("candidate_unconnected")
        if isinstance(before, int) and isinstance(after, int) and after > before:
            reasons.append(f"open connections increased from {before} to {after}")
        if validation.get("reference_comparison", {}).get("status") == "degraded":
            reasons.append("reference copper degraded")
        summaries.append({
            "status": "native_validation_rejected",
            "reason": "Native validation: " + ("; ".join(reasons) or "candidate failed native checks"),
            "rejected_candidates": [
                {"reason": f"{item.get('type', 'finding')}: {item.get('description', '')}"[:600],
                 "items": item.get("items", [])[:4]}
                for item in list(representative_findings.values())[:8]
            ] + [
                {"reason": f"Schematic parity: {item.get('description', item.get('type', 'finding'))}"[:600]}
                for item in parity[:3]
            ] + [
                {
                    "reason": f"Existing connection on {item.get('net', 'unknown net')} would be broken",
                    "net": item.get("net"),
                    "representative": item.get("representative"),
                    "disconnected_items": item.get("disconnected_items", []),
                }
                for item in validation.get("broken_existing_connections", [])[:3]
            ],
            "path_calls": 0, "elapsed_seconds": 0,
        })
    return summaries


def resume_action(checkpoint: dict, inputs: dict, policy: str, tools: dict) -> str:
    """Choose a safe continuation without presenting internal fingerprints."""
    if not isinstance(checkpoint, dict) or not all(
        key in checkpoint for key in ("inputs", "policy", "tools")
    ):
        return "unknown"
    if checkpoint["inputs"] != inputs or checkpoint["policy"] != policy:
        return "restart"
    return "renew" if checkpoint["tools"] != tools else "resume"


def _resume_action_for_project(project: Path, state: dict) -> str:
    checkpoint = state.get("checkpoint")
    if not isinstance(checkpoint, dict) or not all(
        key in checkpoint for key in ("inputs", "policy", "tools")
    ):
        return "unknown"
    project = project.resolve()
    assets = asset_root(project)
    try:
        snapshot = Snapshot(native_path(project, ".kicad_pcb"))
        snapshot.fingerprints = {path: sha(path) for path in snapshot.files()}
        return resume_action(
            checkpoint, _source_hashes(snapshot, project, assets),
            sha(config_path(project, "autopcb.json")), Snapshot.tools(),
        )
    except (OSError, RuntimeError, ValueError):
        return "unknown"


def read_status(project: Path, run_id: str) -> dict:
    path = stored_result(project, "autoroute", f"{_run_id(run_id)}.json")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema") != 1 or state.get("run_id") != run_id:
        raise ValueError("Invalid AutoPCB run state")
    if state.get("status") == "running":
        lock = path.parent / "active.lock"
        if lock.is_file():
            try:
                with _exclusive(lock):
                    # The previous writer may have finished since our read.
                    state = json.loads(path.read_text(encoding="utf-8"))
                    if state.get("schema") != 1 or state.get("run_id") != run_id:
                        raise ValueError("Invalid AutoPCB run state")
                    if state.get("status") == "running":
                        state["status"] = "interrupted"
                        state["failure"] = "No active routing lock; the last saved checkpoint was retained."
            except RuntimeError as error:
                if str(error) != "Another AutoPCB run is active":
                    raise
        else:
            state["status"] = "interrupted"
            state["failure"] = "No active routing lock; the last saved checkpoint was retained."
    if state.get("status") == "stalled" and state.get("queue") == [] and not state.get("failure"):
        # Older runs used "stalled" for every empty queue. Interpret the saved
        # measurement for display without rewriting the historical checkpoint.
        state["status"] = PreviewStatus.stopped(state)
    result = {key: value for key, value in state.items() if key not in {"checkpoint", "queue", "reference", "island_counts"}}
    result["resume_action"] = _resume_action_for_project(project, state)
    reference = state.get("reference", {})
    result["reference"] = {
        "status": reference.get("status"),
        "high_speed_net_count": len(reference.get("high_speed_nets", [])),
        "reservation_count": reference.get("reservation_count", 0),
        "conservative_conflict_count": len(reference.get("conservative_conflicts", [])),
        "tracks_with_missing_samples": sum(item.get("missing_samples", 0) > 0 for item in reference.get("sampled_tracks", [])),
        "electrically_verified": reference.get("electrically_verified", False),
    }
    return result


def _board(path: Path):
    board = pcbnew.LoadBoard(str(path))
    if board is None:
        raise RuntimeError("KiCad cannot load the private candidate")
    return board


def _replan(snapshot: Snapshot, policy: dict, reference: dict, *, workers: int, single: bool, offset: int = 0) -> dict:
    return run_native(_replan_native, snapshot.directory, policy, reference,
                      workers=workers, single=single, offset=offset)


def _replan_native(directory: Path, policy: dict, reference: dict, *, workers: int,
                   single: bool, offset: int) -> dict:
    board = _board(native_path(directory, ".kicad_pcb"))
    schematic = (native_path(directory, ".kicad_sch")).read_text(encoding="utf-8-sig")
    inventory = inventory_board(board, schematic=schematic)
    work = work_from_board(board, inventory, policy.get("priorities", {}),
                           margin_mm=policy.get("search_margin_mm") or 8.0)
    box = board.GetBoardEdgesBoundingBox()
    bounds = [box.GetX() / 1e6, box.GetY() / 1e6, box.GetRight() / 1e6, box.GetBottom() / 1e6]
    return make_work_plan({"work": asdict(work), "reference_copper": reference,
                           "unqualified_nets": sorted(unqualified_working_nets(
                               board, tuple(policy.get("exclude_nets", ()))
                           )),
                           "board_bounds_mm": bounds}, policy,
                          workers=workers, max_batch_nets=1 if single else 3, checkpoint_offset=offset)


def _round_targets(batch: list) -> tuple[str, ...]:
    """Count repair neighbors as dependencies, not as independent targets."""
    if not isinstance(batch, list) or not batch:
        raise ValueError("A queued routing round must contain requests")
    names = []
    for request in batch:
        if not isinstance(request, dict):
            raise ValueError("A queued routing request must be an object")
        nets = request.get("nets")
        if not isinstance(nets, list) or not nets:
            raise ValueError("A queued routing request needs target nets")
        targets = nets[:1] if request.get("rip_up") or request.get("movable") else nets
        for item in targets:
            name = item.get("name") if isinstance(item, dict) else None
            if not isinstance(name, str) or not name:
                raise ValueError("A queued target needs a net name")
            if name not in names:
                names.append(name)
    return tuple(names)


def _pop_fair_round(queue: list, attempts: dict, priorities: dict) -> list:
    """Try less-explored, higher-priority, simpler searches before repairs."""
    if not isinstance(queue, list) or not queue or not isinstance(attempts, dict) or not isinstance(priorities, dict):
        raise ValueError("Fair scheduling needs a queue, attempt counts and priorities")
    if any(not isinstance(name, str) or isinstance(value, bool) or not isinstance(value, int) or value < 0
           for name, value in attempts.items()):
        raise ValueError("Target attempt counts must be nonnegative integers")
    if any(not isinstance(name, str) or isinstance(value, bool) or not isinstance(value, int)
           for name, value in priorities.items()):
        raise ValueError("Target priorities must be integers")

    def rank(index: int) -> tuple[int, int, int, int]:
        names = _round_targets(queue[index])
        early = set()
        for request in queue[index]:
            flag = request.get("early_rework", False)
            if not isinstance(flag, bool) or (flag and request.get("recovery_stage") != 4):
                raise ValueError("Invalid early rework marker")
            if flag:
                early.update(_round_targets([request]))
        effective = {name: max(0, attempts.get(name, 0) - int(name in early)) for name in names}
        minimum = min(effective.values())
        priority = min(priorities.get(name, 100) for name in names
                       if effective[name] == minimum)
        stages = []
        for request in queue[index]:
            stage = request.get("recovery_stage", 0)
            if isinstance(stage, bool) or not isinstance(stage, int) or stage < 0:
                raise ValueError("Recovery stages must be nonnegative integers")
            if any(effective[name] == minimum and priorities.get(name, 100) == priority
                   for name in _round_targets([request])):
                stages.append(-1 if request.get("early_rework", False) else stage)
        return minimum, priority, min(stages), index

    return queue.pop(min(range(len(queue)), key=rank))


def _parallel_ordinary_requests(queue: list, primary: list, attempts: dict,
                                priorities: dict, workers: int) -> list:
    """Pair different ordinary retries as independent, competing previews."""
    if workers not in {1, 2}:
        raise ValueError("Use one or two workers")

    def ordinary(batch):
        return (len(batch) == 1 and not batch[0].get("rip_up")
                and not batch[0].get("movable")
                and batch[0].get("recovery_stage", 0) < 4)

    if workers == 1 or not ordinary(primary):
        return primary
    primary_nets = set(_round_targets(primary))
    ranked = list(queue)
    while ranked:
        candidate = _pop_fair_round(ranked, attempts, priorities)
        if ordinary(candidate) and primary_nets.isdisjoint(_round_targets(candidate)):
            index = next(i for i, item in enumerate(queue) if item is candidate)
            return [*primary, *queue.pop(index)]
    return primary


def _deferred_position(queue: list, target: str, required: int) -> int:
    """Find a place after a number of distinct other pending targets."""
    seen = set()
    position = 0
    while position < len(queue) and len(seen) < required:
        for request_index, request in enumerate(queue[position]):
            nets = request.get("nets")
            if not nets:
                seen.add(("pending", position, request_index))
                continue
            targets = nets[:1] if request.get("rip_up") or request.get("movable") else nets
            seen.update(item["name"] for item in targets if item["name"] != target)
        position += 1
    return position


def _defer_repair_alternatives(queue: list, candidates: list[dict], *, defer_rounds: int) -> None:
    """Give distinct other targets a turn between costly repair alternatives."""
    for index, candidate in enumerate(candidates):
        target = candidate["nets"][0]["name"]
        required = (index + 1) * defer_rounds
        position = (_deferred_position(queue, target, required) if required
                    else min(index, len(queue)))
        queue.insert(position, [candidate])


def _invalidate_compiled_repairs(queue: list) -> None:
    """Replace old-board repair hypotheses with one fresh marker per target."""
    result, seen = [], set()
    for batch in queue:
        ordinary = []
        for request in batch:
            if request.get("recovery_stage") != 4:
                ordinary.append(request)
                continue
            if ordinary:
                result.append(ordinary)
                ordinary = []
            target = request["nets"][0]["name"]
            if target in seen:
                continue
            seen.add(target)
            marker = deepcopy(request)
            marker["nets"] = marker["nets"][:1]
            marker["checkpoint_offset"] = 0
            for field in ("rip_up", "movable", "orders", "recovery_preference"):
                marker.pop(field, None)
            result.append([marker])
        if ordinary:
            result.append(ordinary)
    queue[:] = result


def _unfinished_combined_batches(requests: list[dict], result: dict) -> list[dict]:
    """Retain failed work when another independent batch was accepted."""
    attempts = result.get("attempts")
    if (not isinstance(attempts, list) or len(attempts) != len(requests)
            or any(not isinstance(attempt, dict) for attempt in attempts)):
        raise RuntimeError("Combined preview has incomplete batch attempts")
    completed = {"geometric_candidate_only", "already_connected"}
    retryable = {"no_complete_candidate_found", "checkpoint_exhausted"}
    unfinished = []
    for request, attempt in strict_zip(requests, attempts):
        status = attempt.get("status")
        if status in retryable:
            unfinished.append(request)
        elif status not in completed:
            raise RuntimeError(f"Unsupported combined batch attempt status: {status!r}")
    return unfinished


def _rejected_checkpoint_retries(fresh: list, previous: object,
                                 recovery: RecoveryPolicy) -> list:
    """Rebuild rejected searches from the current plan, never old geometry."""
    if not isinstance(previous, dict) or previous.get("outcome") != "rejected":
        return []
    targets = {}
    for attempt in previous.get("attempts", []):
        if not isinstance(attempt, dict) or attempt.get("status") != "geometric_candidate_only":
            continue
        for checkpoint in attempt.get("checkpoints", []):
            if not isinstance(checkpoint, dict):
                continue
            name, stage, index = checkpoint.get("net"), checkpoint.get("stage"), checkpoint.get("index", 0)
            if (isinstance(name, str) and name and isinstance(stage, int) and not isinstance(stage, bool)
                    and 0 <= stage <= 4 and isinstance(index, int) and not isinstance(index, bool) and index >= 0):
                targets.setdefault(name, (stage, index))
    retries = []
    for group in fresh:
        for request in group:
            for net in request["nets"]:
                if net["name"] not in targets:
                    continue
                stage, index = targets.pop(net["name"])
                rebuilt = deepcopy(request)
                rebuilt["nets"] = [deepcopy(net)]
                while rebuilt.get("recovery_stage", 0) < stage:
                    rebuilt = next_stage(rebuilt, recovery)
                    if rebuilt is None:
                        break
                if rebuilt is not None:
                    rebuilt["checkpoint_offset"] = index
                    retries.append([rebuilt])
    return retries


def _split_failed(queue: list, requests: list[dict], island_counts: dict[str, int],
                  result: dict | None = None, recovery: RecoveryPolicy | None = None) -> None:
    if any(request.get("recovery_stage", 0) == 4 for request in requests):
        # A target and its obstructing neighbor form one transaction. Splitting
        # them would discard the coordinated repair and repeat known failures.
        return
    if len(requests) > 1:
        queue[:0] = [[request] for request in requests]
    elif len(requests[0]["nets"]) > 1:
        request = requests[0]
        independent_failures = {
            search["net"] for attempt in (result or {}).get("attempts", [])
            for trial in attempt.get("trials", []) for order in trial.get("orders", [])
            for search in order.get("searches", [])
            if (not search.get("prior_nets") and search.get("candidates") == 0
                and search.get("budget_limited") is False)
        }
        for net in reversed(request["nets"]):
            portion = {**request, "nets": [net]}
            portion.pop("orders", None)
            if "rip_up" in request:
                portion["rip_up"] = {name: items for name, items in request["rip_up"].items() if name == net["name"]}
            if net["name"] in independent_failures:
                _split_failed(queue, [portion], island_counts, result, recovery)
            else:
                queue.insert(0, [portion])
    else:
        recovery = recovery or RecoveryPolicy()
        request = requests[0]
        name = request["nets"][0]["name"]
        count = island_counts.get(name, 0)
        offset = request.get("checkpoint_offset", 0)
        attempts = (result or {}).get("attempts", [])
        if attempts and all(item.get("status") == "unsupported_request" for item in attempts):
            return
        available = [item["count"] for attempt in attempts
                     for item in attempt.get("checkpoints", []) if item.get("net") == name]
        choices = min(available) if available else count * (count - 1) // 2
        exhausted = any(item.get("status") == "checkpoint_exhausted" for item in attempts)
        if not exhausted and offset + 1 < min(choices, recovery.alternatives_per_stage):
            queue.insert(0, [{**request, "checkpoint_offset": offset + 1}])
        else:
            escalated = next_stage(request, recovery)
            if escalated is not None:
                # Other pending nets get a turn before an expanded search. The
                # new corridor is run alone, so it cannot overlap a parallel job.
                queue.insert(_deferred_position(queue, name, recovery.defer_rounds), [escalated])
        if (recovery.early_rework and request.get("recovery_stage", 0) == 0
                and (recovery.rip_up_candidates or recovery.placement_candidates)):
            marker = deepcopy(request)
            marker.update(recovery_stage=4, checkpoint_offset=0, early_rework=True)
            preference = recovery_preference(request)
            for key in ("orders", "rip_up", "movable", "recovery_preference"):
                marker.pop(key, None)
            if preference is not None:
                marker["recovery_preference"] = preference
            if not any(len(group) == 1 and group[0].get("early_rework")
                       and _round_targets(group) == (name,) for group in queue):
                queue.insert(_deferred_position(queue, name, recovery.defer_rounds), [marker])


def verify_unchanged_native_errors(baseline: dict, candidate: dict) -> None:
    """Allow resolved errors but refuse changed, new or unidentified errors."""
    def error_evidence(summary: dict) -> collections.Counter:
        try:
            error_count = sum(value[1] == "error" for value in summary["violation_keys"])
        except (KeyError, IndexError, TypeError) as error:
            raise RuntimeError("Incomplete native error evidence") from error
        if summary.get("errors", error_count) != error_count:
            raise RuntimeError("Incomplete native error evidence")
        details = summary.get("error_details")
        if details is None and not error_count:
            return collections.Counter()
        if not isinstance(details, list) or len(details) != error_count:
            raise RuntimeError("Incomplete native error evidence")
        for detail in details:
            try:
                finding = json.loads(detail)
                items = finding.get("items")
                if (finding.get("severity") != "error" or not finding.get("type")
                        or not finding.get("description") or not isinstance(items, list) or not items
                        or any(not isinstance(item, dict) or not isinstance(item.get("uuid"), str)
                               or not item["uuid"] for item in items)):
                    raise ValueError("unidentified finding")
            except (TypeError, ValueError, AttributeError) as error:
                raise RuntimeError("Incomplete native error evidence") from error
        return collections.Counter(details)

    if error_evidence(candidate) - error_evidence(baseline):
        raise RuntimeError("Resumed candidate changes or worsens an existing native error")


def verify_resume_reports(baseline: dict, candidate: dict, *, expected_opens: int,
                          identical_inputs: bool = False,
                          stable_connection_items: dict[str, str] | None = None,
                          allow_open_increase: bool = False) -> None:
    """Compare independent native DRC findings, including identities, on resume."""
    if not isinstance(allow_open_increase, bool):
        raise ValueError("Open-increase permission must be boolean")
    if identical_inputs:
        fields = ("open_connections", "errors", "warnings", "exclusions", "parity_findings")
        if any(field not in baseline or baseline.get(field) != candidate.get(field)
               for field in fields):
            raise RuntimeError("Identical-source DRC counts disagree")
    baseline_opens = baseline.get("open_connections")
    if (isinstance(baseline_opens, bool) or not isinstance(baseline_opens, int) or baseline_opens < 0
            or isinstance(expected_opens, bool) or not isinstance(expected_opens, int) or expected_opens < 0
            or candidate.get("open_connections") != expected_opens
            or (not allow_open_increase and expected_opens > baseline_opens)):
        raise RuntimeError("Resumed candidate open-connection count does not match native DRC")
    if candidate.get("ignored_checks") != baseline.get("ignored_checks"):
        raise RuntimeError("Resumed candidate changed ignored DRC checks")
    old_ends, new_ends = baseline.get("dangling_warnings"), candidate.get("dangling_warnings")
    compare_ends = isinstance(old_ends, list) and isinstance(new_ends, list)
    if compare_ends:
        try:
            added_ends = _new_dangling_indices(old_ends, new_ends)
            if added_ends:
                raise RuntimeError(
                    f"Resumed candidate introduces native violations: {len(added_ends)} dangling-track endpoints")
        except (TypeError, ValueError) as error:
            raise RuntimeError("Incomplete native dangling-track evidence") from error
    for field, label in (("violation_keys", "violations"), ("parity_keys", "schematic parity")):
        try:
            def key(value):
                # KiCad can attach a connection-width warning to a different
                # adjacent segment when rerunning the *identical* board.
                if (identical_inputs and field == "violation_keys"
                        and value[0] == "connection_width" and value[1] == "warning"):
                    return value[0], value[1], ()
                return value[0], value[1], tuple(value[2])

            old = collections.Counter(key(value) for value in baseline[field]
                                      if not (compare_ends and field == "violation_keys" and value[0] == "track_dangling"))
            new = collections.Counter(key(value) for value in candidate[field]
                                      if not (compare_ends and field == "violation_keys" and value[0] == "track_dangling"))
        except (TypeError, KeyError, IndexError) as error:
            raise RuntimeError(f"Incomplete native {label} evidence") from error
        added_keys = added_violation_keys(old, new,
                                         stable_connection_items if field == "violation_keys" else None)
        if added_keys:
            additions = collections.Counter()
            for finding, count in added_keys.items():
                additions[f"{finding[1]} {finding[0]}"] += count
            detail = ", ".join(f"{count} {kind}" for kind, count in sorted(additions.items()))
            raise RuntimeError(f"Resumed candidate introduces native {label}: {detail}")
    verify_unchanged_native_errors(baseline, candidate)


def _same_snapshot_inputs(snapshot: Snapshot, original: bytes) -> bool:
    """Prove the copied board and all dependencies match the captured source."""
    try:
        if snapshot.board_path.read_bytes() != original:
            return False
        for source, expected in snapshot.fingerprints.items():
            target = (snapshot.board_path if source == snapshot.source else
                      snapshot.directory / snapshot.input_name(source))
            if not target.is_file() or sha(target) != expected:
                return False
        return True
    except (OSError, ValueError, AttributeError):
        return False


def verify_resume_board(snapshot: Snapshot, project: Path, expected_opens: int,
                        *, progress=None) -> dict:
    """Recheck a private candidate against the saved board, not stored scores."""
    if progress:
        progress({"event": "stage", "message": "Checking the saved board with current native rules"})
    baseline = run_native(prepare_project, project)
    if progress:
        progress({"event": "stage", "message": "Checking the retained preview with current native rules"})
    candidate = run_native(prepare_project, snapshot.directory)
    identical = _same_snapshot_inputs(snapshot, snapshot.original)
    stable = None
    if not identical:
        from pcb.route_validation import stable_connection_items

        stable = stable_connection_items(snapshot.original, snapshot.board_path.read_bytes())
    verify_resume_reports(
        baseline["drc"], candidate["drc"], expected_opens=expected_opens,
        identical_inputs=identical, stable_connection_items=stable,
    )
    run_native(_verify_resume_connectivity, snapshot.directory, snapshot.original)
    snapshot.check()
    return candidate


def _verify_resume_connectivity(directory: Path, original_bytes: bytes) -> None:
    original_path = directory / "resume-original.kicad_pcb"
    original_path.write_bytes(original_bytes)
    original, routed = _board(original_path), _board(native_path(directory, ".kicad_pcb"))
    from pcb.net_routing.via_guard import verify_no_new_via_pad_overlaps

    verify_no_new_via_pad_overlaps(original, routed)
    original.GetConnectivity().Build(original)
    routed.GetConnectivity().Build(routed)
    previous = _net_identities(original)
    current = _net_identities(routed)
    removed = {item.m_Uuid.AsString() for item in original.GetTracks()} - {item.m_Uuid.AsString() for item in routed.GetTracks()}
    if any(current.get(identifier) != net for identifier, net in previous.items() if identifier not in removed):
        raise RuntimeError("Resumed candidate changed an existing copper net")
    broken = _broken_existing_connections(original, routed, removed)
    if broken:
        nets = ", ".join(sorted({item["net"] for item in broken}))
        raise RuntimeError("Resumed candidate broke an existing copper connection: " + nets)
    old_style = normalize_segment_identities(audit_style(original), _segment_identities(original_bytes))
    new_style = normalize_segment_identities(
        audit_style(routed), _segment_identities((native_path(directory, ".kicad_pcb")).read_bytes())
    )
    if new_style_issues(old_style, new_style):
        raise RuntimeError("Resumed candidate introduced routing style issues")


def _reference_repairs_native(directory: Path, reference: dict, policy: dict,
                              workers: int) -> dict:
    return compile_reference_repairs(_board(native_path(directory, ".kicad_pcb")),
                                     reference, policy, workers=workers)


def _compile_reroutes_native(directory: Path, request: dict, policy: dict,
                             critical_nets: tuple[str, ...]) -> dict:
    board = _board(native_path(directory, ".kicad_pcb"))
    protected = set(critical_nets) | unqualified_working_nets(
        board, tuple(policy.get("exclude_nets", ()))
    )
    return compile_reroutes(board, request, policy, tuple(sorted(protected)),
                            reference_nets=critical_nets)


def _preview_round_native(directory: Path, requests: list[dict], workers: int,
                          critical_nets: tuple[str, ...], *,
                          precomputed: list[dict] | None = None) -> dict:
    """Bound native board lifetimes to one search/validation transaction."""
    source = native_path(directory, ".kicad_pcb")
    result = preview_round(directory, requests, workers=workers, precomputed=precomputed)
    if result["status"] == "validated_preview":
        style = result["validation"].get("new_style_issues")
        if not isinstance(style, list):
            raise RuntimeError("Native preview did not report routing style checks")
        plan = CampaignPlanner(directory).plan("pcb", result["changes"])
        if plan.result_sha256 != result["candidate_sha256"] or sha(source) != result["source_sha256"]:
            raise RuntimeError("Preview no longer matches the validated board")
        candidate = directory / "candidate.kicad_pcb"
        candidate.write_bytes(plan.after)
        board = _board(candidate)
        result["new_style_issues"] = tuple(style)
        result["candidate_reference"] = reference_report(board, set(critical_nets))
        from review.geometry_metrics import measure_board
        result["candidate_geometry"] = asdict(measure_board(board))
    return result


def _search_alternatives_native(directory: Path, requests: list[dict], workers: int) -> list[dict]:
    """Search concurrently; workers exit before any native DRC begins."""
    if not requests or workers not in {1, 2} or len(requests) > workers:
        raise ValueError("Alternative previews need one request per available worker")
    arguments = [(str(directory), request) for request in requests]
    if len(arguments) == 1:
        return [_search(arguments[0])]
    # One fresh process per search: KiCad's SWIG board wrappers can retain
    # native allocations between loads.
    context = multiprocessing.get_context("spawn")
    with ExitStack() as stack:
        futures = [
            stack.enter_context(concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=context))
            .submit(_search, argument)
            for argument in arguments
        ]
        return [future.result() for future in futures]


def _validate_alternative_native(directory: Path, request: dict, result: dict,
                                 critical_nets: tuple[str, ...]) -> dict:
    with Snapshot(native_path(directory, ".kicad_pcb")) as snapshot:
        return _preview_round_native(snapshot.directory, [request], 1, critical_nets,
                                     precomputed=[result])


def _validate_search_alternatives(directory: Path, requests: list[dict], searches: list[dict],
                                  critical_nets: tuple[str, ...]) -> list[dict]:
    """Validate competing proposals independently in fresh native processes."""
    results = []
    for request, search in strict_zip(requests, searches):
        if search.get("status") == "geometric_candidate_only":
            results.append(run_native(_validate_alternative_native, directory, request, search, critical_nets))
        else:
            results.append({"status": "no_candidate_found", "attempts": [search],
                            "reason": "No sampled candidate; feasibility remains unknown."})
    return results


def _preview_alternatives_native(directory: Path, requests: list[dict], workers: int,
                                 critical_nets: tuple[str, ...]) -> list[dict]:
    """Run parallel searches, then validate each proposal in a fresh process."""
    searches = _search_alternatives_native(directory, requests, workers)
    return _validate_search_alternatives(directory, requests, searches, critical_nets)


def _preview_ordinary_native(directory: Path, requests: list[dict], workers: int,
                             critical_nets: tuple[str, ...], previous_opens: int) -> dict:
    """Combine independent successes when one native validation accepts them."""
    searches = _search_alternatives_native(directory, requests, workers)
    windows = [tuple(request["shared"]["bounds_mm"]) for request in requests]
    can_combine = (
        len(requests) == 2
        and all(item.get("status") == "geometric_candidate_only" for item in searches)
        and not overlaps(windows[0], windows[1])
    )
    if can_combine:
        combined = _preview_round_native(
            directory, requests, workers, critical_nets, precomputed=searches
        )
        validation = combined.get("validation", {})
        opens = validation.get("candidate_unconnected")
        if (
            combined.get("status") == "validated_preview"
            and combined.get("new_style_issues") in ((), [])
            and validation.get("status") == "no_new_drc_violations"
            and validation.get("schematic_parity_checked") is True
            and isinstance(opens, int) and not isinstance(opens, bool) and opens >= 0
            and is_preview_improvement(
                previous_opens, opens,
                validation.get("reference_comparison", {}),
            )
        ):
            return {"result": combined, "combined": True, "alternatives": [], "selected_index": None}
    alternatives = _validate_search_alternatives(directory, requests, searches, critical_nets)
    selected = _select_alternative_result(alternatives, previous_opens)
    index = next(i for i, item in enumerate(alternatives) if item is selected)
    return {
        "result": {**selected, "attempts": [
            attempt for item in alternatives for attempt in item.get("attempts", [])
        ]},
        "combined": False, "alternatives": alternatives, "selected_index": index,
    }


def _neutral_candidate_eligible(result: dict, best_opens: int) -> bool:
    """Use an equal-open preview only as a temporary, validated search branch."""
    validation = result.get("validation", {})
    reference = validation.get("reference_comparison", {})
    return (
        result.get("status") == "validated_preview"
        and result.get("new_style_issues") in ((), [])
        and validation.get("status") == "no_new_drc_violations"
        and validation.get("schematic_parity_checked") is True
        and validation.get("candidate_unconnected") == best_opens
        and reference.get("status") in {"unchanged_sampled", "unverified", "no_high_speed_classification"}
        and not is_preview_improvement(best_opens, best_opens, reference)
    )


def _verify_cumulative_reference_native(original: Path, candidate: Path,
                                        critical_nets: tuple[str, ...]) -> dict:
    """Compare the entire two-step branch, including independently refilled zones."""
    import tempfile
    from pcb.net_routing.native import load_board
    from pcb.net_routing.reference import compare_reference, sample_reference

    # A prior/checkpoint PCB often has a different stem. Refilling it in place
    # would silently use defaults instead of the candidate's .kicad_pro/.dru,
    # inventing a return-copper regression even for unchanged physical copper.
    project = next((path for path in (candidate.with_suffix(".kicad_pro"),
                    native_path(candidate.parent, ".kicad_pro"), original.with_suffix(".kicad_pro"),
                    native_path(original.parent, ".kicad_pro")) if path.is_file()), None)
    rules = project.with_suffix(".kicad_dru") if project else None
    context = {}
    if project is not None:
        context[".kicad_pro"] = project.read_bytes()
    if rules is not None and rules.is_file():
        context[".kicad_dru"] = rules.read_bytes()
    with tempfile.TemporaryDirectory(prefix="autopcb-reference-context-") as folder:
        boards = []
        for name, source in (("before", original), ("after", candidate)):
            directory = Path(folder) / name
            directory.mkdir()
            target = native_path(directory, ".kicad_pcb")
            target.write_bytes(source.read_bytes())
            for suffix, data in context.items():
                target.with_suffix(suffix).write_bytes(data)
            boards.append(load_board(target, refill_zones=True))
        return asdict(compare_reference(sample_reference(boards[0], set(critical_nets)),
                                        sample_reference(boards[1], set(critical_nets))))


def _neutral_lookahead(snapshot: Snapshot, neutral_board: bytes, initial: dict,
                       policy: dict, *, best_opens: int, preferred_nets: tuple[str, ...],
                       workers: int, seconds_per_batch: float | None, progress=None) -> dict | None:
    """Try two fresh follow-ups without replacing the best retained checkpoint.

    Only a strictly lower open count can leave this disposable branch, after an
    independent comparison against the pre-branch board. Equal-open changes do
    not claim a symmetry or cost improvement.
    """
    if not _neutral_candidate_eligible(initial, best_opens):
        raise ValueError("Lookahead requires a checked neutral preview")
    if hashlib.sha256(neutral_board).hexdigest() != initial.get("candidate_sha256"):
        raise RuntimeError("Neutral preview differs from its native validation")
    if sha(snapshot.board_path) != initial.get("source_sha256"):
        raise RuntimeError("Neutral preview source changed")
    with Snapshot(snapshot.board_path) as branch:
        schematic = (native_path(branch.directory, ".kicad_sch")).read_bytes()
        protected = ProtectedState.capture(branch.original, schematic,
                                           movable=MoveScope.from_config(policy.get("placement")))
        if protected.differences(neutral_board, schematic):
            raise RuntimeError("Neutral preview changed a protected design item")
        branch.board_path.write_bytes(neutral_board)
        if progress:
            progress({"event": "stage", "message": "Trying a temporary rearrangement and replanning nearby work"})
        reference = initial["candidate_reference"]
        critical = tuple(reference.get("high_speed_nets", []))
        plan = _replan(branch, policy, reference, workers=workers, single=True)
        choices = [request for group in plan["rounds"] for request in group]
        preferred = set(preferred_nets) | set(initial.get("nets", []))
        choices = [request for request in choices if preferred.intersection(_round_targets([request]))]
        primary = set(preferred_nets)
        choices.sort(key=lambda request: not bool(primary.intersection(_round_targets([request]))))
        for request in choices[:2]:
            request = deepcopy(request)
            if seconds_per_batch is not None:
                request["search"]["seconds"] = seconds_per_batch
            if progress:
                progress({"event": "stage", "message":
                          "Checking temporary follow-up: " + ", ".join(_round_targets([request]))})
            result = run_native(_preview_round_native, branch.directory, [request], 1, critical)
            validation = result.get("validation", {})
            comparison = validation.get("reference_comparison", {})
            opens = validation.get("candidate_unconnected")
            if (result.get("status") != "validated_preview"
                    or result.get("new_style_issues") not in ((), [])
                    or validation.get("status") != "no_new_drc_violations"
                    or validation.get("schematic_parity_checked") is not True
                    or not isinstance(opens, int) or isinstance(opens, bool) or opens >= best_opens
                    or comparison.get("status") not in {"unchanged_sampled", "unverified", "no_high_speed_classification"}):
                continue
            candidate = CampaignPlanner(branch.directory).plan("pcb", result["changes"])
            if (candidate.result_sha256 != result["candidate_sha256"]
                    or sha(branch.board_path) != result["source_sha256"]):
                raise RuntimeError("Lookahead candidate no longer matches its validation")
            if protected.differences(candidate.after, schematic):
                raise RuntimeError("Lookahead changed a protected design item")
            branch.board_path.write_bytes(candidate.after)
            cumulative = run_native(_verify_cumulative_reference_native, branch.source,
                                    branch.board_path, critical)
            if cumulative.get("status") not in {"unchanged_sampled", "unverified"}:
                return None
            verified = verify_resume_board(branch, snapshot.directory, opens, progress=progress)
            branch.check()
            native = verified["drc"]
            return {
                "board": candidate.after,
                "nets": result.get("nets", []),
                "attempts": result.get("attempts", []),
                "candidate_reference": verified["reference_copper"],
                "candidate_geometry": verified["geometry"],
                "validation": {**validation, "candidate_unconnected": native["open_connections"],
                               "candidate_errors": native["errors"], "candidate_warnings": native["warnings"],
                               "candidate_schematic_parity": native["parity_findings"],
                               "reference_comparison": cumulative},
            }
    return None


def resume_stalled(state: dict, plan: dict) -> bool:
    if not plan["rounds"]:
        return False
    state.update(status="running", queue=plan["rounds"], blocked_nets=plan["blocked_nets"],
                 island_counts=plan["island_counts"], failed_sweeps=0,
                 sweep_start_opens=state["best_preview_opens"],
                 resume_count=state.get("resume_count", 0) + 1)
    return True


def apply_search_budget(requests: list[dict], limits: dict) -> None:
    """Override bounded search controls without changing physical rules or costs."""
    ceilings = {'max_cells': 8_000_000, 'max_expansions': 2_000_000}
    ranges = {'step_mm': (.025, .5), 'heuristic_weight': (1.0, 20.0)}
    if not isinstance(limits, dict) or set(limits) - (set(ceilings) | set(ranges)):
        raise ValueError('Invalid runtime search budget')
    for key, value in limits.items():
        if key in ceilings:
            valid = not isinstance(value, bool) and isinstance(value, int) and 1 <= value <= ceilings[key]
        else:
            valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                     and math.isfinite(value) and ranges[key][0] <= value <= ranges[key][1])
        if not valid:
            raise ValueError('Invalid runtime search budget')
    for request in requests:
        for options in (request.setdefault('shared', {}), *request.get('nets', [])):
            updated = dict(limits)
            if 'step_mm' in limits and 'step_mm' in options:
                current = options['step_mm']
                if (isinstance(current, bool) or not isinstance(current, (int, float))
                        or not math.isfinite(current) or current <= 0):
                    raise ValueError('Invalid existing search grid')
                # A runtime grid ceiling must not undo stage-two refinement or
                # a deliberately finer maintained per-net escape grid.
                updated['step_mm'] = min(current, limits['step_mm'])
            options.update(updated)


def run_project(project: Path, *, resume: str | None = None, renew: str | None = None,
                max_rounds: int | None = None,
                workers: int = 2, seconds_per_batch: float | None = None, progress=None,
                resource_limits: dict | None = None,
                previous_board: Path | None = None, previous_policy: Path | None = None,
                single_net_search: bool = False, keep_placement: bool = False,
                search_cells: int | None = None, search_expansions: int | None = None,
                search_step_mm: float | None = None, search_heuristic_weight: float | None = None) -> dict:
    """Search with bounded sweeps; retain only improved, validated private candidates."""
    if resume and renew:
        raise ValueError("Choose either --resume or --renew")
    if not isinstance(single_net_search, bool):
        raise ValueError("single_net_search must be true or false")
    if not isinstance(keep_placement, bool):
        raise ValueError("keep_placement must be true or false")
    if ((previous_board is not None or previous_policy is not None)
            and (not renew or previous_board is None or previous_policy is None)):
        raise ValueError("Layer rebase requires --renew and both previous board and previous policy")
    if workers not in {1, 2} or (max_rounds is not None and (isinstance(max_rounds, bool) or max_rounds < 0)):
        raise ValueError("Use one or two workers and a nonnegative round limit")
    if seconds_per_batch is not None and (isinstance(seconds_per_batch, bool) or seconds_per_batch <= 0):
        raise ValueError("Seconds per batch must be positive")
    requested_search = {key: value for key, value in
                        (('max_cells', search_cells), ('max_expansions', search_expansions),
                         ('step_mm', search_step_mm), ('heuristic_weight', search_heuristic_weight))
                        if value is not None}
    apply_search_budget([], requested_search)
    project = project.resolve()
    assets = asset_root(project)
    policy_path = config_path(project, "autopcb.json")
    policy, policy_hash = read_config(policy_path)
    if keep_placement:
        policy = {**policy, "recovery": {**policy.get("recovery", {}), "placement_candidates": 0}}
    recovery = RecoveryPolicy.from_config(policy.get("recovery"))
    output = results_path(project, "autoroute")
    output.mkdir(parents=True, exist_ok=True)
    run_id = _run_id(resume) if resume else uuid.uuid4().hex
    state_path = output / f"{run_id}.json"
    with _exclusive(output / "active.lock"):
        if progress:
            progress({"event": "stage", "message": "Preparing a private routing board; saved PCB stays unchanged"})
        with Snapshot(native_path(project, ".kicad_pcb")) as snapshot:
            inputs = _source_hashes(snapshot, project, assets)
            schematic = (native_path(snapshot.directory, ".kicad_sch")).read_bytes()
            protected = ProtectedState.capture(
                snapshot.original, schematic,
                movable=None if keep_placement else MoveScope.from_config(policy.get("placement")))
            if resume or renew:
                if progress:
                    progress({"event": "stage", "message": "Rechecking the saved preview, protected items and source compatibility"})
                previous_id = _run_id(resume or renew)
                previous_path = stored_result(project, "autoroute", f"{previous_id}.json")
                state = json.loads(previous_path.read_text(encoding="utf-8"))
                if state.get("schema") != 1 or state.get("run_id") != previous_id:
                    raise ValueError("Invalid AutoPCB run state")
                if previous_path.parent != output and (previous_path.parent / "active.lock").exists():
                    raise RuntimeError("A legacy routing job is active for this board")
                mode_changed = ((single_net_search and not state.get("single_net_search", False))
                                or (keep_placement and not state.get("keep_placement", False)))
                state["single_net_search"] = single_net_search or state.get("single_net_search", False)
                keep_placement = keep_placement or state.get("keep_placement", False)
                state["keep_placement"] = keep_placement
                if keep_placement:
                    policy = {**policy, "recovery": {**policy.get("recovery", {}), "placement_candidates": 0}}
                    recovery = RecoveryPolicy.from_config(policy["recovery"])
                    protected = ProtectedState.capture(snapshot.original, schematic)
                if previous_board is not None:
                    from pcb.net_routing.layer_rebase import rebase_checkpoint

                    if sha(policy_path) != policy_hash:
                        raise RuntimeError("Routing policy changed during layer rebase")
                    candidate = rebase_checkpoint(
                        state["checkpoint"], inputs, snapshot.tool_hashes,
                        previous_board.read_bytes(), snapshot.original,
                        previous_policy.read_bytes(), policy_path.read_bytes(),
                    )
                    state["layer_metadata_rebased_from"] = previous_id
                else:
                    candidate = decode_checkpoint(state["checkpoint"], inputs, snapshot.tool_hashes,
                                                  policy_hash, allow_tool_changes=bool(renew))
                if protected.differences(candidate, schematic):
                    raise RuntimeError("Routing candidate changed a protected design item")
                snapshot.board_path.write_bytes(candidate)
                state["resource_limits"] = resource_limits
                if renew or mode_changed:
                    # Keep the old run intact even if verification or replanning
                    # fails. No old campaign/request is executed by renewal.
                    verified = verify_resume_board(snapshot, project, state["best_preview_opens"], progress=progress)
                    plan = _replan(snapshot, policy, verified["reference_copper"],
                                   workers=workers, single=state.get("single_net_search", False))
                    repairs = run_native(_reference_repairs_native, snapshot.directory,
                                         verified["reference_copper"], policy, workers)
                    insert_reference_repairs(plan, repairs, policy)
                    fresh = plan["rounds"]
                    retries = ([] if mode_changed else
                               _rejected_checkpoint_retries(fresh, state.get("last_result"), recovery))
                    pending = fresh if mode_changed else renew_queue(fresh, state.get("queue", []), recovery)
                    pending_keys = {search_key(request) for group in pending for request in group}
                    retries = [group for group in retries if search_key(group[0]) not in pending_keys]
                    plan["rounds"] = retries + pending
                    for group in retries:
                        for name in _round_targets(group):
                            state.get("target_attempts", {}).pop(name, None)
                    state.update(run_id=run_id, renewed_from=previous_id,
                                 status="running", publication_enabled=False,
                                 queue=plan["rounds"], blocked_nets=plan["blocked_nets"],
                                 island_counts=plan["island_counts"], failed_sweeps=0,
                                 sweep_start_opens=state["best_preview_opens"],
                                 reference=verified["reference_copper"],
                                 reference_repair_blockers=repairs["blockers"])
                    if run_id == previous_id:
                        state.pop("renewed_from", None)
                    state.pop("attempted_searches", None)
                    # Code changes make exact search failures worth retrying, but
                    # they do not change how often each target has been served
                    # on this same preview. The board comparison below resets
                    # fairness if the candidate actually changed.
                    state["checkpoint"] = encode_checkpoint(candidate, inputs, snapshot.tool_hashes, policy_hash)
                else:
                    with _record_failures(state_path, state):
                        verified = verify_resume_board(snapshot, project, state["best_preview_opens"], progress=progress)
                _check_sources(snapshot, project, assets, inputs, policy_path, policy_hash)
                state.pop("failure", None)
                state["reference"] = verified["reference_copper"]
                if renew or mode_changed:
                    _save(state_path, state)
                    if progress:
                        progress({"event": "stage", "message":
                                  ("Search batches rebuilt in the current run; saved placement is fixed"
                                   if mode_changed else
                                   "Preview renewed; pending recovery stages retained and requests rebuilt with current code")})
                if state["status"] == "stalled":
                    plan = _replan(snapshot, policy, state["reference"], workers=workers,
                                   single=True, offset=state.get("resume_count", 0) + 1)
                    if not resume_stalled(state, plan):
                        return read_status(project, run_id)
            else:
                preparation = run_native(prepare_project, project, progress=progress, forward_progress=True)
                if progress:
                    progress({"event": "inventory",
                              "open_connections": preparation["drc"]["open_connections"],
                              "errors": preparation["drc"]["errors"],
                              "warnings": preparation["drc"]["warnings"],
                              "work_nets": len(preparation["work"]["items"]),
                              "copper_layers": preparation["copper_layers"]})
                _check_sources(snapshot, project, assets, inputs, policy_path, policy_hash)
                if preparation["tool_sha256"] != snapshot.tool_hashes:
                    raise RuntimeError("Routing tools changed during preparation")
                if progress:
                    progress({"event": "stage", "message": "Planning checkpoint order and reference-copper repairs"})
                plan = make_work_plan(preparation, policy, workers=workers,
                                      max_batch_nets=1 if single_net_search else 3)
                repairs = run_native(_reference_repairs_native, snapshot.directory,
                                     preparation["reference_copper"], policy, workers)
                insert_reference_repairs(plan, repairs, policy)
                state = {"schema": 1, "run_id": run_id, "status": "running", "publication_enabled": False,
                         "single_net_search": single_net_search,
                         "keep_placement": keep_placement,
                         "reference_repair_blockers": repairs["blockers"],
                         "saved_board_opens": preparation["drc"]["open_connections"],
                         "best_preview_opens": preparation["drc"]["open_connections"],
                         "accepted_preview_batches": 0, "attempted_rounds": 0, "failed_sweeps": 0,
                         "sweep_start_opens": preparation["drc"]["open_connections"],
                         "reference": preparation["reference_copper"], "blocked_nets": plan["blocked_nets"],
                         "island_counts": plan["island_counts"],
                         "baseline_blockers": preparation["completion_issues"], "queue": plan["rounds"],
                         "last_result": None, "elapsed_seconds": 0.0}
                state["checkpoint"] = encode_checkpoint(snapshot.original, inputs, snapshot.tool_hashes, policy_hash)
                _save(state_path, state)
            state["resource_limits"] = resource_limits
            search_limits = {**state.get('search_limits', {}), **requested_search}
            apply_search_budget([], search_limits)
            state['search_limits'] = search_limits
            route_options = deepcopy(policy.get('route', {}))
            net_options = deepcopy(policy.get('net_overrides', {}))
            apply_search_budget([{'shared': route_options, 'nets': list(net_options.values())}], search_limits)
            policy = {**policy, 'route': route_options, 'net_overrides': net_options}
            if search_limits and progress:
                progress({'event': 'stage', 'message': 'Runtime search limits: ' +
                          ', '.join(f'{key}={value:,}' for key, value in search_limits.items())})
            measured = verified if (resume or renew) else preparation
            _progress_sample(state, sha(snapshot.board_path), measured["drc"], measured["geometry"], state["elapsed_seconds"])
            with _record_failures(state_path, state):
                _publish_progress(project, inputs, state, progress)
            last_progress_update = time.monotonic()
            current_board = sha(snapshot.board_path)
            if state.get("attempted_board_sha256") != current_board:
                state.update(attempted_board_sha256=current_board, attempted_searches=[],
                             target_attempts={})
            attempted = set(state.get("attempted_searches", []))
            target_attempts = state.setdefault("target_attempts", {})
            start = time.monotonic()
            rounds = 0
            with _record_failures(state_path, state):
                while state["queue"] and (max_rounds is None or rounds < max_rounds):
                    round_started = time.monotonic()
                    _check_sources(snapshot, project, assets, inputs, policy_path, policy_hash)
                    requests = _pop_fair_round(
                        state["queue"], target_attempts, policy.get("priorities", {})
                    )
                    if seconds_per_batch is not None:
                        for request in requests:
                            request["search"]["seconds"] = seconds_per_batch
                    apply_search_budget(requests, search_limits)
                    fresh = [request for request in requests if search_key(request) not in attempted]
                    state["skipped_duplicate_batches"] = state.get("skipped_duplicate_batches", 0) + len(requests) - len(fresh)
                    requests = fresh
                    if not requests:
                        continue
                    paired = _parallel_ordinary_requests(
                        state["queue"], requests, target_attempts,
                        policy.get("priorities", {}), workers,
                    )
                    if len(paired) > len(requests):
                        other = paired[-1]
                        if seconds_per_batch is not None:
                            other["search"]["seconds"] = seconds_per_batch
                        apply_search_budget([other], search_limits)
                        if search_key(other) in attempted:
                            state["skipped_duplicate_batches"] += 1
                        else:
                            requests = paired
                    parallel_ordinary = len(requests) > len(fresh)
                    targets = _round_targets(requests)
                    request_keys = {search_key(request) for request in requests}
                    blocked_recovery = None
                    parallel_repair = False
                    combined_ordinary = False
                    alternatives = []
                    selected_index = None
                    if (len(requests) == 1 and requests[0].get("recovery_stage") == 4
                            and not requests[0].get("rip_up") and not requests[0].get("movable")):
                        preference = requests[0].pop("recovery_preference", None)
                        requests[0].pop("early_rework", None)
                        compiled = run_native(_compile_reroutes_native, snapshot.directory, requests[0],
                                              policy, tuple(state["reference"].get("high_speed_nets", [])))
                        state["reroute_candidates"] = compiled["candidates"]
                        state["reroute_blockers"] = compiled["blockers"]
                        if compiled["requests"]:
                            candidates = prioritize_recovery_requests(compiled["requests"], preference)
                            requests, pending = _parallel_repair_alternatives(candidates, workers)
                            parallel_repair = len(requests) > 1
                            _defer_repair_alternatives(
                                state["queue"], pending,
                                defer_rounds=recovery.defer_rounds,
                            )
                            request_keys.update(search_key(request) for request in requests)
                        else:
                            blocked_recovery = {
                                "status": "no_candidate_found", "attempts": [{
                                    "status": "copper_recovery_blocked",
                                    "reason": "; ".join(compiled["blockers"][:3]) or "No eligible copper in the sampled corridor",
                                }],
                            }
                    names = list(dict.fromkeys(
                        net["name"] for request in requests for net in request["nets"]
                    ))
                    if progress:
                        progress({"event": "search", "run_id": run_id, "nets": names,
                                  "recovery_stage": max(request.get("recovery_stage", 0) for request in requests),
                                  "round": state["attempted_rounds"] + 1,
                                  "alternative_count": len(requests) if parallel_repair or parallel_ordinary else 0,
                                  "preview_opens": state["best_preview_opens"], "saved_opens": state["saved_board_opens"]})
                    if blocked_recovery:
                        result = blocked_recovery
                    elif parallel_ordinary:
                        decision = run_native(
                            _preview_ordinary_native, snapshot.directory, requests, workers,
                            tuple(state["reference"].get("high_speed_nets", [])),
                            state["best_preview_opens"],
                        )
                        result = decision["result"]
                        combined_ordinary = decision["combined"]
                        alternatives = decision["alternatives"]
                        selected_index = decision["selected_index"]
                    elif parallel_repair:
                        alternatives = run_native(
                            _preview_alternatives_native, snapshot.directory, requests, workers,
                            tuple(state["reference"].get("high_speed_nets", [])),
                        )
                        selected = _select_alternative_result(alternatives, state["best_preview_opens"])
                        selected_index = next(i for i, item in enumerate(alternatives) if item is selected)
                        result = {
                            **selected,
                            "attempts": [attempt for item in alternatives
                                         for attempt in item.get("attempts", [])],
                        }
                    else:
                        result = run_native(
                            _preview_round_native, snapshot.directory, requests, workers,
                            tuple(state["reference"].get("high_speed_nets", [])))
                    _check_sources(snapshot, project, assets, inputs, policy_path, policy_hash)
                    accepted = False
                    unfinished_combined = []
                    reason = result["status"]
                    if result["status"] == "validated_preview":
                        if (combined_ordinary or not (parallel_repair or parallel_ordinary)) and len(requests) > 1:
                            unfinished_combined = _unfinished_combined_batches(requests, result)
                        plan = CampaignPlanner(snapshot.directory).plan("pcb", result["changes"])
                        if plan.result_sha256 != result["candidate_sha256"] or sha(snapshot.board_path) != result["source_sha256"]:
                            raise RuntimeError("Preview no longer matches the validated board")
                        candidate_path = snapshot.directory / "candidate.kicad_pcb"
                        candidate_path.write_bytes(plan.after)
                        candidate_bytes = plan.after
                        style = result["new_style_issues"]
                        opens = result["validation"]["candidate_unconnected"]
                        if protected.differences(candidate_bytes, schematic):
                            raise RuntimeError("Preview changed a protected design item")
                        if style:
                            reason = "new routing style issues: " + ", ".join(style)
                        elif not is_preview_improvement(state["best_preview_opens"], opens,
                                                        result["validation"].get("reference_comparison", {})):
                            reason = "no improvement in connectivity or sampled reference coverage"
                            if _neutral_candidate_eligible(result, state["best_preview_opens"]):
                                followup = _neutral_lookahead(
                                    snapshot, candidate_bytes, result, policy,
                                    best_opens=state["best_preview_opens"], preferred_nets=targets,
                                    workers=workers, seconds_per_batch=seconds_per_batch, progress=progress,
                                )
                                _check_sources(snapshot, project, assets, inputs, policy_path, policy_hash)
                                if followup is not None:
                                    candidate_bytes = followup.pop("board")
                                    if protected.differences(candidate_bytes, schematic):
                                        raise RuntimeError("Lookahead changed a protected design item")
                                    result = {**result, **followup}
                                    opens = result["validation"]["candidate_unconnected"]
                        if not style and is_preview_improvement(
                            state["best_preview_opens"], opens,
                            result["validation"].get("reference_comparison", {}),
                        ):
                            snapshot.board_path.write_bytes(candidate_bytes)
                            state["best_preview_opens"] = opens
                            state["accepted_preview_batches"] += 1
                            state["reference"] = result["candidate_reference"]
                            validation = result["validation"]
                            _progress_sample(state, sha(snapshot.board_path), {
                                "open_connections": opens,
                                "errors": validation["candidate_errors"],
                                "warnings": validation["candidate_warnings"],
                                "parity_findings": validation["candidate_schematic_parity"],
                            }, result["candidate_geometry"], state["elapsed_seconds"] + time.monotonic() - start)
                            accepted = True
                    if not accepted:
                        if parallel_ordinary and not combined_ordinary:
                            for request, alternative in strict_zip(requests, alternatives):
                                _split_failed(state["queue"], [request], state.get("island_counts", {}), alternative, recovery)
                        else:
                            _split_failed(state["queue"], requests, state.get("island_counts", {}), result, recovery)
                        attempted.update(request_keys)
                        for name in targets:
                            target_attempts[name] = target_attempts.get(name, 0) + 1
                    else:
                        # Previous failures refer to different copper now; they
                        # may be worth retrying after a validated improvement.
                        # Preserve the fairness counters: they control order,
                        # not whether those searches may be tried again.
                        attempted.clear()
                        if parallel_ordinary and not combined_ordinary:
                            for index, request in enumerate(requests):
                                if index != selected_index:
                                    target = request["nets"][0]["name"]
                                    state["queue"].insert(
                                        _deferred_position(state["queue"], target, recovery.defer_rounds),
                                        [request],
                                    )
                        for request in unfinished_combined:
                            target = request["nets"][0]["name"]
                            state["queue"].insert(
                                _deferred_position(state["queue"], target, recovery.defer_rounds),
                                [request],
                            )
                        _invalidate_compiled_repairs(state["queue"])
                        state["attempted_board_sha256"] = sha(snapshot.board_path)
                    state["attempted_searches"] = sorted(attempted)
                    state["attempted_rounds"] += 1
                    rounds += 1
                    state["last_result"] = {"nets": result.get("nets", names) if accepted else names, "outcome": "accepted_preview" if accepted else reason,
                                            "attempts": summarize_attempts(result)}
                    if not state["queue"]:
                        if state["best_preview_opens"] < state["sweep_start_opens"]:
                            state["failed_sweeps"] = 0
                        else:
                            state["failed_sweeps"] += 1
                        if state["failed_sweeps"] < 3:
                            plan = _replan(snapshot, policy, state["reference"], workers=workers,
                                           single=state.get("single_net_search", False) or state["failed_sweeps"] > 0,
                                           offset=state["failed_sweeps"])
                            state["queue"] = plan["rounds"]
                            state["blocked_nets"] = plan["blocked_nets"]
                            state["island_counts"] = plan["island_counts"]
                            state["sweep_start_opens"] = state["best_preview_opens"]
                            target_attempts.clear()
                    state["checkpoint"] = encode_checkpoint(snapshot.board_path.read_bytes(), inputs, snapshot.tool_hashes, policy_hash)
                    state["elapsed_seconds"] += round(time.monotonic() - start, 3)
                    start = time.monotonic()
                    state["status"] = "running"
                    _save(state_path, state)
                    if accepted or time.monotonic() - last_progress_update >= 60:
                        _progress_elapsed(state, sha(snapshot.board_path))
                        _publish_progress(project, inputs, state, progress)
                        last_progress_update = time.monotonic()
                    if progress:
                        progress({"event": "round", "run_id": run_id, "result": state["last_result"],
                                  "remaining_rounds": len(state["queue"]),
                                  "round_seconds": time.monotonic() - round_started,
                                  "preview_opens": state["best_preview_opens"], "saved_opens": state["saved_board_opens"]})
            state["status"] = PreviewStatus.stopped(state)
            state["blockers"] = ["Checked manufacturing export requires independent design checks and a source-bound electrical review."]
            _check_sources(snapshot, project, assets, inputs, policy_path, policy_hash)
            _progress_elapsed(state, sha(snapshot.board_path))
            _publish_progress(project, inputs, state, progress)
            _save(state_path, state)
    return read_status(project, run_id)
