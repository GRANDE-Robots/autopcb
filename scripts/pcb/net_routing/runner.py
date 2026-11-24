"""runner.py: execute resumable board-wide searches on a private native-board snapshot.

This preview runner never publishes a PCB. Electrical, paired-signal and full
reference-path verification remain required before a publication path exists.

Copyright: GRANDE Robots and Aaron John Sabu
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
from pcb.net_routing.controller import (
    decode_checkpoint,
    encode_checkpoint,
    make_work_plan,
    new_style_issues,
)
from pcb.net_routing.inventory import inventory_board
from pcb.net_routing.isolation import run_native
from pcb.net_routing.native import Snapshot, sha
from pcb.net_routing.prepare import prepare_project
from pcb.move_scope import MoveScope
from pcb.net_routing.protection import ProtectedState, unqualified_working_nets
from pcb.net_routing.reference import reference_report
from pcb.net_routing.reference_repair import compile_reference_repairs
from pcb.net_routing.recovery import (
    RecoveryPolicy,
    next_stage,
    prioritize_recovery_requests,
    recovery_preference,
    renew_queue,
    search_key,
)
from pcb.net_routing.reroute import compile_reroutes
from pcb.net_routing.scheduler import SearchWindow, work_from_board
from pcb.net_routing.style import audit_style, normalize_segment_identities
from pcb.route_validation import (
    _broken_existing_connections,
    _net_identities,
    _new_dangling_indices,
    _segment_identities,
    added_violation_keys,
)
from core.compat import strict_zip


class PreviewStatus:
    """Describes a run that has stopped searching."""

    @staticmethod
    def stopped(state: dict) -> str:
        """Return the status of a run that is no longer searching.

        Inputs:
            state: Run state with the queue and best_preview_opens.
        Outputs:
            paused when work remains queued, preview_zero_opens when the best preview
            has
            no open connections, otherwise stalled.
        """
        if state["queue"]:
            return "paused"
        opens = state.get("best_preview_opens")
        return "preview_zero_opens" if type(opens) is int and opens == 0 else "stalled"


class SourceHashes:
    """Fingerprints the inputs that a run's results depend on."""

    def __call__(
        self, snapshot: Snapshot, project: Path, assets: Path
    ) -> dict[str, str]:
        """Return the SHA-256 of every input of a run by project-relative name.

        Inputs:
            snapshot: Private snapshot whose fingerprints cover the board and its
                dependencies.
            project: Project folder.
            assets: Folder that holds the project's AutoPCB files.
        Outputs:
            A map from input name to hash. The power-load budget and the current stackup
            are recorded as absent when missing. Files that the routing policy names as
            zone-stitch evidence are included, or recorded as absent.
        """
        result = {
            snapshot.input_name(path).as_posix(): digest
            for path, digest in snapshot.fingerprints.items()
        }
        for name in ("POWER_LOAD_BUDGET.csv", "STACKUP_CURRENT.json"):
            path = data_path(project, name)
            if path.is_file():
                result[snapshot.input_name(path).as_posix()] = sha(path)
            else:
                # Keep the source key used by existing runs when an input is absent.
                result[(assets / "design" / name).relative_to(project).as_posix()] = (
                    "absent"
                )
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
                    source = (
                        evidence.get("source") if isinstance(evidence, dict) else None
                    )
                    if isinstance(source, str) and source:
                        path = (root / source).resolve()
                        if path.is_relative_to(root):
                            result[path.relative_to(root).as_posix()] = (
                                sha(path) if path.is_file() else "absent"
                            )
                    pending.extend(item.values())
        return result


_source_hashes = SourceHashes()


class SourceCheck:
    """Checks that a run's inputs and routing policy are unchanged."""

    def __call__(
        self,
        snapshot: Snapshot,
        project: Path,
        assets: Path,
        expected: dict,
        policy: Path,
        policy_hash: str,
    ) -> None:
        """Raise RuntimeError when the snapshot, the input hashes or the policy changed.

        Inputs:
            snapshot: Private snapshot of the board.
            project: Project folder.
            assets: Folder that holds the project's AutoPCB files.
            expected: Input hashes recorded for the run.
            policy: Routing policy file.
            policy_hash: SHA-256 recorded for the policy file.
        """
        snapshot.check()
        if (
            _source_hashes(snapshot, project, assets) != expected
            or not policy.is_file()
            or sha(policy) != policy_hash
        ):
            raise RuntimeError(
                "Routing inputs or policy changed; discard this result and retry"
            )


_check_sources = SourceCheck()


class RunLock:
    """Takes the OS lock that lets one AutoPCB routing run own a results folder."""

    @staticmethod
    def acquire(acquire, *, wait_seconds: float, progress=None) -> None:
        """Retry a busy OS lock briefly; never proceed without ownership."""
        if (
            not callable(acquire)
            or isinstance(wait_seconds, bool)
            or not isinstance(wait_seconds, (int, float))
            or not math.isfinite(wait_seconds)
            or wait_seconds < 0
            or (progress is not None and not callable(progress))
        ):
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
    def __call__(self, path: Path, *, wait_seconds: float = 0, progress=None):
        """Use an OS-owned lock that is released even when the process is killed."""
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                RunLock.acquire(
                    lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1),
                    wait_seconds=wait_seconds,
                    progress=progress,
                )
            else:
                import fcntl

                RunLock.acquire(
                    lambda: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB),
                    wait_seconds=wait_seconds,
                    progress=progress,
                )
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


_exclusive = RunLock()


class RunState:
    """Writes a run's state file and keeps the last committed state when a run fails."""

    def __call__(self, path: Path, state: dict) -> None:
        """Atomically replace the state file with the state as compact JSON.

        Inputs:
            path: State file.
            state: JSON-serializable run state; non-finite numbers raise ValueError.
        Outputs:
            None. The new content is flushed to disk before it replaces the file, and
            the
            temporary file is removed if anything fails.
        """
        encoded = json.dumps(state, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".autoroute-", suffix=".tmp", delete=False
        ) as stream:
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

    @staticmethod
    @contextmanager
    def record_failures(path: Path, state: dict):
        """Record a failed or interrupted run on its last committed state.

        Inputs:
            path: State file of the run.
            state: In-memory state, used when no file was committed yet.
        Outputs:
            A context manager. On any exception it saves status interrupted (Ctrl-C),
            resource_blocked (MemoryError) or failed with the failure text, keeps any
            resource limits and progress-export error, and re-raises.
        """
        try:
            yield
        except BaseException as error:
            # Retain the last atomically committed queue and candidate. In-memory
            # work from an interrupted round was never accepted.
            committed = (
                json.loads(path.read_text(encoding="utf-8"))
                if path.is_file()
                else state
            )
            committed["status"] = (
                "interrupted"
                if isinstance(error, KeyboardInterrupt)
                else "resource_blocked"
                if isinstance(error, MemoryError)
                else "failed"
            )
            committed["failure"] = str(error) or type(error).__name__
            if state.get("resource_limits") is not None:
                committed["resource_limits"] = state["resource_limits"]
            if state.get("progress_export_error") is not None:
                committed["progress_export_error"] = state["progress_export_error"]
            _save(path, committed)
            raise


_save = RunState()


class ProgressSample:
    """Appends measured progress points to a run's state."""

    def __call__(
        self, state: dict, board_hash: str, counts: dict, geometry: dict, elapsed: float
    ) -> None:
        """Record native measurements, not search estimates or presumed improvements."""
        from review.routing_progress import ProgressPoint

        point = ProgressPoint(
            datetime.now(timezone.utc).isoformat(),
            counts["open_connections"],
            counts["errors"],
            counts["warnings"],
            counts.get("parity_findings", 0),
            board_hash,
            "",
            "Validated private preview",
            "Native candidate validation",
            geometry["track_length_mm"],
            geometry["via_count"],
            geometry["degree_two_joints"],
            "Native candidate geometry",
            routing_run_seconds=elapsed,
            optimization_evidence="Measured run wall time; private preview",
        )
        state.setdefault("progress_samples", []).append(asdict(point))


_progress_sample = ProgressSample()


class ProgressPublish:
    """Publishes a run's progress samples to the review views."""

    def __call__(self, project: Path, inputs: dict, state: dict, progress=None) -> None:
        """Publish the run's progress samples.

        Inputs:
            project: Project folder.
            inputs: Input hashes of the run.
            state: Run state; progress_export_error is set on failure and cleared on
                success.
            progress: Optional callback that receives a stage event on failure.
        Outputs:
            None. OSError, ValueError and RuntimeError are recorded in the state and
            re-raised.
        """
        from review.routing_progress import publish_preview

        try:
            publish_preview(
                project, inputs, state["run_id"], state.get("progress_samples", [])
            )
            state.pop("progress_export_error", None)
        except (OSError, ValueError, RuntimeError) as error:
            # Preserve the last validated checkpoint and stop before more search.
            state["progress_export_error"] = str(error)
            if progress:
                progress(
                    {
                        "event": "stage",
                        "message": f"Routing checkpoint retained; progress export needs retry: {error}",
                    }
                )
            raise


_publish_progress = ProgressPublish()


class ProgressElapsed:
    """Advances the recorded run time of an unchanged validated board."""

    def __call__(self, state: dict, board_hash: str) -> None:
        """Advance runtime for the identical validated board without inventing
        routes."""
        previous = state["progress_samples"][-1]
        if previous["pcb_sha256"] != board_hash:
            raise RuntimeError(
                "Progress measurements do not belong to the retained preview"
            )
        if previous["routing_run_seconds"] == state["elapsed_seconds"]:
            return
        state["progress_samples"].append(
            {
                **previous,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "routing_run_seconds": state["elapsed_seconds"],
                "change": "Retained validated preview; search time updated",
            }
        )


_progress_elapsed = ProgressElapsed()


class RunId:
    """Validates run IDs."""

    def __call__(self, value: str) -> str:
        """Return the run ID; raise ValueError unless it is 32 lowercase hexadecimal
        characters."""
        if (
            not isinstance(value, str)
            or len(value) != 32
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("Run ID must be 32 lowercase hexadecimal characters")
        return value


_run_id = RunId()


class ReferenceRepairInsertion:
    """Adds reference-copper repair rounds to a work plan."""

    def __call__(self, plan: dict, repairs: dict, policy: dict) -> None:
        """Close unfinished connections before revisiting existing reference gaps.

        New reference damage remains an independent rejection at every stage.
        Existing gaps stay in the queue and remain completion blockers.
        """
        plan["rounds"].extend(repairs["rounds"])
        plan["island_counts"].update(repairs["island_counts"])


insert_reference_repairs = ReferenceRepairInsertion()


class PreviewJudge:
    """Decides whether native preview results improve a board and which alternative to
    keep."""

    @staticmethod
    def is_improvement(
        previous_opens: int, candidate_opens: int, reference: dict
    ) -> bool:
        """Say whether a candidate improves the retained board.

        Inputs:
            previous_opens: Open connections of the retained board.
            candidate_opens: Open connections of the candidate.
            reference: Reference-copper comparison of the candidate.
        Outputs:
            False when the reference copper degraded or the candidate has more opens.
            True when it has fewer opens. With equal opens, True only when the
            comparable missing reference samples decreased.
        """
        if reference.get("status") == "degraded":
            return False
        if candidate_opens < previous_opens:
            return True
        if candidate_opens != previous_opens:
            return False
        counts = reference.get("comparable_missing_samples")
        return (
            isinstance(counts, list)
            and len(counts) == 2
            and all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in counts
            )
            and counts[1] < counts[0]
        )

    @staticmethod
    def repair_alternatives(
        candidates: list[dict], workers: int
    ) -> tuple[list[dict], list[dict]]:
        """Try independent repair kinds concurrently without merging their copper."""
        if not isinstance(candidates, list) or workers not in {1, 2}:
            raise ValueError("Repair alternatives need a list and one or two workers")
        if not candidates:
            return [], []
        indices = [0]
        if workers > 1 and len(candidates) > 1:
            first_is_move = bool(candidates[0].get("movable"))
            other = next(
                (
                    i
                    for i in range(1, len(candidates))
                    if bool(candidates[i].get("movable")) != first_is_move
                ),
                1,
            )
            indices.append(other)
        return (
            [candidates[i] for i in indices],
            [item for i, item in enumerate(candidates) if i not in indices],
        )

    @staticmethod
    def select_alternative(results: list[dict], previous_opens: int) -> dict:
        """Select one checked preview; alternative board changes are never combined."""
        if not results:
            raise ValueError("A repair selection needs at least one result")
        valid = [
            item
            for item in results
            if item.get("status") == "validated_preview"
            and not item.get("new_style_issues")
            and PreviewJudge.is_improvement(
                previous_opens,
                item["validation"]["candidate_unconnected"],
                item["validation"].get("reference_comparison", {}),
            )
        ]
        if valid:
            return min(
                valid, key=lambda item: item["validation"]["candidate_unconnected"]
            )
        neutral = next(
            (
                item
                for item in results
                if PreviewJudge.neutral_eligible(item, previous_opens)
            ),
            None,
        )
        if neutral is not None:
            return neutral
        return next(
            (item for item in results if item.get("status") == "validated_preview"),
            next(
                (item for item in results if item.get("status") == "rejected"),
                results[0],
            ),
        )

    @staticmethod
    def neutral_eligible(result: dict, best_opens: int) -> bool:
        """Use an equal-open preview only as a temporary, validated search branch."""
        validation = result.get("validation", {})
        reference = validation.get("reference_comparison", {})
        return (
            result.get("status") == "validated_preview"
            and result.get("new_style_issues") in ((), [])
            and validation.get("status") == "no_new_drc_violations"
            and validation.get("schematic_parity_checked") is True
            and validation.get("candidate_unconnected") == best_opens
            and reference.get("status")
            in {"unchanged_sampled", "unverified", "no_high_speed_classification"}
            and not PreviewJudge.is_improvement(best_opens, best_opens, reference)
        )


class AttemptSummary:
    """Condenses the attempts of a preview result for the run record."""

    def __call__(self, result: dict) -> list[dict]:
        """Summarize a result's search attempts.

        Inputs:
            result: Preview result with attempts and, when rejected, its validation.
        Outputs:
            One summary per attempt, limited to four trials and four orders each and
            three
            rejected candidates. A rejected native validation adds one summary that
            lists
            the new violations, parity findings and broken connections.
        """
        summaries = []
        for attempt in result.get("attempts", []):
            search = attempt.get("search", {})
            orders = [
                {
                    "order": trial.get("order", [])[:20],
                    "nets_reached": trial.get("nets_reached", 0),
                    "complete_candidates": trial.get("complete_candidates", 0),
                }
                for placement in attempt.get("trials", [])[:4]
                for trial in placement.get("orders", [])[:4]
            ]
            ignored_zones = max(
                (
                    placement.get("ignored_zone_count", 0)
                    for placement in attempt.get("trials", [])
                ),
                default=0,
            )
            zone_count = max(
                (
                    placement.get("zone_count", placement.get("ignored_zone_count", 0))
                    for placement in attempt.get("trials", [])
                ),
                default=0,
            )
            zone_modes = {
                placement.get("zone_connectivity", "unfilled")
                for placement in attempt.get("trials", [])
            }
            summaries.append(
                {
                    "status": attempt.get("status"),
                    "reason": attempt.get("reason"),
                    "limits_hit": search.get("limits_hit", []),
                    "failures": search.get("failures", {}),
                    "failure_details": search.get("failure_details", {}),
                    "checkpoints": attempt.get("checkpoints", []),
                    "orders": orders,
                    "ignored_zone_count": ignored_zones,
                    "zone_count": zone_count,
                    "zone_connectivity": "refilled"
                    if zone_modes == {"refilled"}
                    else "unfilled",
                    "rejected_candidates": attempt.get("rejected_candidates", [])[:3],
                    "routing_cost": attempt.get("routing_cost"),
                    "rework_cost": attempt.get("rework_cost"),
                    "path_calls": search.get("path_calls"),
                    "elapsed_seconds": search.get("elapsed_seconds"),
                }
            )
        validation = result.get("validation", {})
        if result.get("status") == "rejected" and validation:
            counts = collections.Counter(
                f"{item.get('severity', 'finding')} {item.get('type', 'unknown')}"
                for item in validation.get("new_violations", [])
            )
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
                reasons.append(
                    "new routing style issues: "
                    + ", ".join(validation["new_style_issues"])
                )
            for field, label in (
                ("net_identity_changes", "changed net assignments"),
                ("broken_existing_connections", "broken existing connections"),
            ):
                if validation.get(field):
                    reasons.append(f"{len(validation[field])} {label}")
            before, after = (
                validation.get("baseline_unconnected"),
                validation.get("candidate_unconnected"),
            )
            if isinstance(before, int) and isinstance(after, int) and after > before:
                reasons.append(f"open connections increased from {before} to {after}")
            if validation.get("reference_comparison", {}).get("status") == "degraded":
                reasons.append("reference copper degraded")
            summaries.append(
                {
                    "status": "native_validation_rejected",
                    "reason": "Native validation: "
                    + ("; ".join(reasons) or "candidate failed native checks"),
                    "rejected_candidates": [
                        {
                            "reason": f"{item.get('type', 'finding')}: {item.get('description', '')}"[
                                :600
                            ],
                            "items": item.get("items", [])[:4],
                        }
                        for item in list(representative_findings.values())[:8]
                    ]
                    + [
                        {
                            "reason": f"Schematic parity: {item.get('description', item.get('type', 'finding'))}"[
                                :600
                            ]
                        }
                        for item in parity[:3]
                    ]
                    + [
                        {
                            "reason": f"Existing connection on {item.get('net', 'unknown net')} would be broken",
                            "net": item.get("net"),
                            "representative": item.get("representative"),
                            "disconnected_items": item.get("disconnected_items", []),
                        }
                        for item in validation.get("broken_existing_connections", [])[
                            :3
                        ]
                    ],
                    "path_calls": 0,
                    "elapsed_seconds": 0,
                }
            )
        return summaries


summarize_attempts = AttemptSummary()


class RunStatus:
    """Reads the saved status of a run."""

    @staticmethod
    def resume_action(checkpoint: dict, inputs: dict, policy: str, tools: dict) -> str:
        """Choose a safe continuation without presenting internal fingerprints."""
        if not isinstance(checkpoint, dict) or not all(
            key in checkpoint for key in ("inputs", "policy", "tools")
        ):
            return "unknown"
        if checkpoint["inputs"] != inputs or checkpoint["policy"] != policy:
            return "restart"
        return "renew" if checkpoint["tools"] != tools else "resume"

    @staticmethod
    def project_resume_action(project: Path, state: dict) -> str:
        """Choose how a saved run may continue given the project's current sources.

        Inputs:
            project: Project folder.
            state: Saved run state.
        Outputs:
            resume, renew, restart or unknown; unknown also when the current sources
            cannot be fingerprinted.
        """
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
            return RunStatus.resume_action(
                checkpoint,
                _source_hashes(snapshot, project, assets),
                sha(config_path(project, "autopcb.json")),
                Snapshot.tools(),
            )
        except (OSError, RuntimeError, ValueError):
            return "unknown"

    def __call__(self, project: Path, run_id: str) -> dict:
        """Return a saved run's status without its checkpoint and queue.

        Inputs:
            project: Project folder.
            run_id: Run ID; ValueError when it or the saved state is invalid.
        Outputs:
            The saved state without checkpoint, queue, reference and island counts, plus
            resume_action and a reference summary. A run that says running while no
            process holds its lock is reported as interrupted.
        """
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
                            state["failure"] = (
                                "No active routing lock; the last saved checkpoint was retained."
                            )
                except RuntimeError as error:
                    if str(error) != "Another AutoPCB run is active":
                        raise
            else:
                state["status"] = "interrupted"
                state["failure"] = (
                    "No active routing lock; the last saved checkpoint was retained."
                )
        if (
            state.get("status") == "stalled"
            and state.get("queue") == []
            and not state.get("failure")
        ):
            # Older runs used "stalled" for every empty queue. Interpret the saved
            # measurement for display without rewriting the historical checkpoint.
            state["status"] = PreviewStatus.stopped(state)
        result = {
            key: value
            for key, value in state.items()
            if key not in {"checkpoint", "queue", "reference", "island_counts"}
        }
        result["resume_action"] = RunStatus.project_resume_action(project, state)
        reference = state.get("reference", {})
        result["reference"] = {
            "status": reference.get("status"),
            "high_speed_net_count": len(reference.get("high_speed_nets", [])),
            "reservation_count": reference.get("reservation_count", 0),
            "conservative_conflict_count": len(
                reference.get("conservative_conflicts", [])
            ),
            "tracks_with_missing_samples": sum(
                item.get("missing_samples", 0) > 0
                for item in reference.get("sampled_tracks", [])
            ),
            "electrically_verified": reference.get("electrically_verified", False),
        }
        return result


read_status = RunStatus()


class NativePreview:
    """Steps of the routing preview that run in a fresh native worker process.

    Each step takes plain data and a snapshot folder, loads KiCad boards itself and
    returns plain records, so a worker can be given the step by name.
    """

    @staticmethod
    def board(path: Path):
        """Load a board; raise RuntimeError when KiCad cannot load it."""
        board = pcbnew.LoadBoard(str(path))
        if board is None:
            raise RuntimeError("KiCad cannot load the private candidate")
        return board

    @staticmethod
    def replan(
        directory: Path,
        policy: dict,
        reference: dict,
        *,
        workers: int,
        single: bool,
        offset: int,
    ) -> dict:
        """Plan fresh work for the board in a snapshot folder.

        Inputs:
            directory: Snapshot folder.
            policy: Routing policy.
            reference: Reference-copper report of the board.
            workers: One or two parallel searches.
            single: Plan one net per batch.
            offset: Checkpoint offset for the new requests.
        Outputs:
            The work plan.
        """
        board = NativePreview.board(native_path(directory, ".kicad_pcb"))
        schematic = (native_path(directory, ".kicad_sch")).read_text(
            encoding="utf-8-sig"
        )
        inventory = inventory_board(board, schematic=schematic)
        work = work_from_board(
            board,
            inventory,
            policy.get("priorities", {}),
            margin_mm=policy.get("search_margin_mm") or 8.0,
        )
        box = board.GetBoardEdgesBoundingBox()
        bounds = [
            box.GetX() / 1e6,
            box.GetY() / 1e6,
            box.GetRight() / 1e6,
            box.GetBottom() / 1e6,
        ]
        return make_work_plan(
            {
                "work": asdict(work),
                "reference_copper": reference,
                "unqualified_nets": sorted(
                    unqualified_working_nets(
                        board, tuple(policy.get("exclude_nets", ()))
                    )
                ),
                "board_bounds_mm": bounds,
            },
            policy,
            workers=workers,
            max_batch_nets=1 if single else 3,
            checkpoint_offset=offset,
        )

    @staticmethod
    def reroutes(
        directory: Path, request: dict, policy: dict, critical_nets: tuple[str, ...]
    ) -> dict:
        """Compile rip-up and move requests for a recovery target.

        Inputs:
            directory: Snapshot folder.
            request: Stage-four request for the target net.
            policy: Routing policy.
            critical_nets: High-speed nets, protected together with the unqualified
                nets.
        Outputs:
            The compiled candidates, requests and blockers.
        """
        board = NativePreview.board(native_path(directory, ".kicad_pcb"))
        protected = set(critical_nets) | unqualified_working_nets(
            board, tuple(policy.get("exclude_nets", ()))
        )
        return compile_reroutes(
            board,
            request,
            policy,
            tuple(sorted(protected)),
            reference_nets=critical_nets,
        )

    @staticmethod
    def preview_round(
        directory: Path,
        requests: list[dict],
        workers: int,
        critical_nets: tuple[str, ...],
        *,
        precomputed: list[dict] | None = None,
    ) -> dict:
        """Bound native board lifetimes to one search/validation transaction."""
        source = native_path(directory, ".kicad_pcb")
        result = preview_round(
            directory, requests, workers=workers, precomputed=precomputed
        )
        if result["status"] == "validated_preview":
            style = result["validation"].get("new_style_issues")
            if not isinstance(style, list):
                raise RuntimeError("Native preview did not report routing style checks")
            plan = CampaignPlanner(directory).plan("pcb", result["changes"])
            if (
                plan.result_sha256 != result["candidate_sha256"]
                or sha(source) != result["source_sha256"]
            ):
                raise RuntimeError("Preview no longer matches the validated board")
            candidate = directory / "candidate.kicad_pcb"
            candidate.write_bytes(plan.after)
            board = NativePreview.board(candidate)
            result["new_style_issues"] = tuple(style)
            result["candidate_reference"] = reference_report(board, set(critical_nets))
            from review.geometry_metrics import measure_board

            result["candidate_geometry"] = asdict(measure_board(board))
        return result

    @staticmethod
    def search_alternatives(
        directory: Path, requests: list[dict], workers: int
    ) -> list[dict]:
        """Search concurrently; workers exit before any native DRC begins."""
        if not requests or workers not in {1, 2} or len(requests) > workers:
            raise ValueError(
                "Alternative previews need one request per available worker"
            )
        arguments = [(str(directory), request) for request in requests]
        if len(arguments) == 1:
            return [_search(arguments[0])]
        # One fresh process per search: KiCad's SWIG board wrappers can retain
        # native allocations between loads.
        context = multiprocessing.get_context("spawn")
        with ExitStack() as stack:
            futures = [
                stack.enter_context(
                    concurrent.futures.ProcessPoolExecutor(
                        max_workers=1, mp_context=context
                    )
                ).submit(_search, argument)
                for argument in arguments
            ]
            return [future.result() for future in futures]

    @staticmethod
    def validate_alternative(
        directory: Path, request: dict, result: dict, critical_nets: tuple[str, ...]
    ) -> dict:
        """Validate one searched alternative on a fresh snapshot and return its preview
        round."""
        with Snapshot(native_path(directory, ".kicad_pcb")) as snapshot:
            return NativePreview.preview_round(
                snapshot.directory, [request], 1, critical_nets, precomputed=[result]
            )

    @staticmethod
    def validate_searches(
        directory: Path,
        requests: list[dict],
        searches: list[dict],
        critical_nets: tuple[str, ...],
    ) -> list[dict]:
        """Validate competing proposals independently in fresh native processes."""
        results = []
        for request, search in strict_zip(requests, searches):
            if search.get("status") == "geometric_candidate_only":
                results.append(
                    run_native(
                        NativePreview.validate_alternative,
                        directory,
                        request,
                        search,
                        critical_nets,
                    )
                )
            else:
                results.append(
                    {
                        "status": "no_candidate_found",
                        "attempts": [search],
                        "reason": "No sampled candidate; feasibility remains unknown.",
                    }
                )
        return results

    @staticmethod
    def preview_alternatives(
        directory: Path,
        requests: list[dict],
        workers: int,
        critical_nets: tuple[str, ...],
    ) -> list[dict]:
        """Run parallel searches, then validate each proposal in a fresh process."""
        searches = NativePreview.search_alternatives(directory, requests, workers)
        return NativePreview.validate_searches(
            directory, requests, searches, critical_nets
        )

    @staticmethod
    def preview_ordinary(
        directory: Path,
        requests: list[dict],
        workers: int,
        critical_nets: tuple[str, ...],
        previous_opens: int,
    ) -> dict:
        """Combine independent successes when one native validation accepts them."""
        searches = NativePreview.search_alternatives(directory, requests, workers)
        windows = [tuple(request["shared"]["bounds_mm"]) for request in requests]
        can_combine = (
            len(requests) == 2
            and all(
                item.get("status") == "geometric_candidate_only" for item in searches
            )
            and not SearchWindow.overlaps(windows[0], windows[1])
        )
        if can_combine:
            combined = NativePreview.preview_round(
                directory, requests, workers, critical_nets, precomputed=searches
            )
            validation = combined.get("validation", {})
            opens = validation.get("candidate_unconnected")
            if (
                combined.get("status") == "validated_preview"
                and combined.get("new_style_issues") in ((), [])
                and validation.get("status") == "no_new_drc_violations"
                and validation.get("schematic_parity_checked") is True
                and isinstance(opens, int)
                and not isinstance(opens, bool)
                and opens >= 0
                and PreviewJudge.is_improvement(
                    previous_opens,
                    opens,
                    validation.get("reference_comparison", {}),
                )
            ):
                return {
                    "result": combined,
                    "combined": True,
                    "alternatives": [],
                    "selected_index": None,
                }
        alternatives = NativePreview.validate_searches(
            directory, requests, searches, critical_nets
        )
        selected = PreviewJudge.select_alternative(alternatives, previous_opens)
        index = next(i for i, item in enumerate(alternatives) if item is selected)
        return {
            "result": {
                **selected,
                "attempts": [
                    attempt
                    for item in alternatives
                    for attempt in item.get("attempts", [])
                ],
            },
            "combined": False,
            "alternatives": alternatives,
            "selected_index": index,
        }


class Replan:
    """Plans fresh work for a snapshot in a native worker process."""

    def __call__(
        self,
        snapshot: Snapshot,
        policy: dict,
        reference: dict,
        *,
        workers: int,
        single: bool,
        offset: int = 0,
    ) -> dict:
        """Plan fresh work for a snapshot's board in a native worker process.

        Inputs:
            snapshot: Private snapshot of the board.
            policy: Routing policy.
            reference: Reference-copper report of the board.
            workers: One or two parallel searches.
            single: Plan one net per batch.
            offset: Checkpoint offset for the new requests.
        Outputs:
            The work plan.
        """
        return run_native(
            NativePreview.replan,
            snapshot.directory,
            policy,
            reference,
            workers=workers,
            single=single,
            offset=offset,
        )


_replan = Replan()


class RoundQueue:
    """Operations on the queue of pending search rounds; each round is a list of
    requests."""

    @staticmethod
    def targets(batch: list) -> tuple[str, ...]:
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
            targets = (
                nets[:1] if request.get("rip_up") or request.get("movable") else nets
            )
            for item in targets:
                name = item.get("name") if isinstance(item, dict) else None
                if not isinstance(name, str) or not name:
                    raise ValueError("A queued target needs a net name")
                if name not in names:
                    names.append(name)
        return tuple(names)

    @staticmethod
    def pop_fair(queue: list, attempts: dict, priorities: dict) -> list:
        """Try less-explored, higher-priority, simpler searches before repairs."""
        if (
            not isinstance(queue, list)
            or not queue
            or not isinstance(attempts, dict)
            or not isinstance(priorities, dict)
        ):
            raise ValueError(
                "Fair scheduling needs a queue, attempt counts and priorities"
            )
        if any(
            not isinstance(name, str)
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for name, value in attempts.items()
        ):
            raise ValueError("Target attempt counts must be nonnegative integers")
        if any(
            not isinstance(name, str)
            or isinstance(value, bool)
            or not isinstance(value, int)
            for name, value in priorities.items()
        ):
            raise ValueError("Target priorities must be integers")

        def rank(index: int) -> tuple[int, int, int, int]:
            """Return the sort key of a queued round: fewest attempts, best priority,
            lowest recovery stage, queue position."""
            names = RoundQueue.targets(queue[index])
            early = set()
            for request in queue[index]:
                flag = request.get("early_rework", False)
                if not isinstance(flag, bool) or (
                    flag and request.get("recovery_stage") != 4
                ):
                    raise ValueError("Invalid early rework marker")
                if flag:
                    early.update(RoundQueue.targets([request]))
            effective = {
                name: max(0, attempts.get(name, 0) - int(name in early))
                for name in names
            }
            minimum = min(effective.values())
            priority = min(
                priorities.get(name, 100)
                for name in names
                if effective[name] == minimum
            )
            stages = []
            for request in queue[index]:
                stage = request.get("recovery_stage", 0)
                if isinstance(stage, bool) or not isinstance(stage, int) or stage < 0:
                    raise ValueError("Recovery stages must be nonnegative integers")
                if any(
                    effective[name] == minimum and priorities.get(name, 100) == priority
                    for name in RoundQueue.targets([request])
                ):
                    stages.append(-1 if request.get("early_rework", False) else stage)
            return minimum, priority, min(stages), index

        return queue.pop(min(range(len(queue)), key=rank))

    @staticmethod
    def pair_ordinary(
        queue: list, primary: list, attempts: dict, priorities: dict, workers: int
    ) -> list:
        """Pair different ordinary retries as independent, competing previews."""
        if workers not in {1, 2}:
            raise ValueError("Use one or two workers")

        def ordinary(batch):
            """Say whether a round is one ordinary request below the repair stage."""
            return (
                len(batch) == 1
                and not batch[0].get("rip_up")
                and not batch[0].get("movable")
                and batch[0].get("recovery_stage", 0) < 4
            )

        if workers == 1 or not ordinary(primary):
            return primary
        primary_nets = set(RoundQueue.targets(primary))
        ranked = list(queue)
        while ranked:
            candidate = RoundQueue.pop_fair(ranked, attempts, priorities)
            if ordinary(candidate) and primary_nets.isdisjoint(
                RoundQueue.targets(candidate)
            ):
                index = next(i for i, item in enumerate(queue) if item is candidate)
                return [*primary, *queue.pop(index)]
        return primary

    @staticmethod
    def deferred_position(queue: list, target: str, required: int) -> int:
        """Find a place after a number of distinct other pending targets."""
        seen = set()
        position = 0
        while position < len(queue) and len(seen) < required:
            for request_index, request in enumerate(queue[position]):
                nets = request.get("nets")
                if not nets:
                    seen.add(("pending", position, request_index))
                    continue
                targets = (
                    nets[:1]
                    if request.get("rip_up") or request.get("movable")
                    else nets
                )
                seen.update(item["name"] for item in targets if item["name"] != target)
            position += 1
        return position

    @staticmethod
    def defer_alternatives(
        queue: list, candidates: list[dict], *, defer_rounds: int
    ) -> None:
        """Give distinct other targets a turn between costly repair alternatives."""
        for index, candidate in enumerate(candidates):
            target = candidate["nets"][0]["name"]
            required = (index + 1) * defer_rounds
            position = (
                RoundQueue.deferred_position(queue, target, required)
                if required
                else min(index, len(queue))
            )
            queue.insert(position, [candidate])

    @staticmethod
    def invalidate_compiled(queue: list) -> None:
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

    @staticmethod
    def unfinished_combined(requests: list[dict], result: dict) -> list[dict]:
        """Retain failed work when another independent batch was accepted."""
        attempts = result.get("attempts")
        if (
            not isinstance(attempts, list)
            or len(attempts) != len(requests)
            or any(not isinstance(attempt, dict) for attempt in attempts)
        ):
            raise RuntimeError("Combined preview has incomplete batch attempts")
        completed = {"geometric_candidate_only", "already_connected"}
        retryable = {"no_complete_candidate_found", "checkpoint_exhausted"}
        unfinished = []
        for request, attempt in strict_zip(requests, attempts):
            status = attempt.get("status")
            if status in retryable:
                unfinished.append(request)
            elif status not in completed:
                raise RuntimeError(
                    f"Unsupported combined batch attempt status: {status!r}"
                )
        return unfinished

    @staticmethod
    def rejected_retries(
        fresh: list, previous: object, recovery: RecoveryPolicy
    ) -> list:
        """Rebuild rejected searches from the current plan, never old geometry."""
        if not isinstance(previous, dict) or previous.get("outcome") != "rejected":
            return []
        targets = {}
        for attempt in previous.get("attempts", []):
            if (
                not isinstance(attempt, dict)
                or attempt.get("status") != "geometric_candidate_only"
            ):
                continue
            for checkpoint in attempt.get("checkpoints", []):
                if not isinstance(checkpoint, dict):
                    continue
                name, stage, index = (
                    checkpoint.get("net"),
                    checkpoint.get("stage"),
                    checkpoint.get("index", 0),
                )
                if (
                    isinstance(name, str)
                    and name
                    and isinstance(stage, int)
                    and not isinstance(stage, bool)
                    and 0 <= stage <= 4
                    and isinstance(index, int)
                    and not isinstance(index, bool)
                    and index >= 0
                ):
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

    @staticmethod
    def split_failed(
        queue: list,
        requests: list[dict],
        island_counts: dict[str, int],
        result: dict | None = None,
        recovery: RecoveryPolicy | None = None,
    ) -> None:
        """Requeue the parts of a failed round.

        Inputs:
            queue: Pending rounds, changed in place.
            requests: Requests of the failed round.
            island_counts: Copper island count by net name.
            result: The round's result, or None.
            recovery: Recovery policy, or the default.
        Outputs:
            None. A round of several requests becomes one round each. A request for
            several nets is split by net. A single net advances its checkpoint offset
            or escalates to the next recovery stage, and may add an early-rework
            marker. Split parts go to the front of the queue, an escalated request
            waits behind other pending targets, and a repair transaction is not split.
        """
        if any(request.get("recovery_stage", 0) == 4 for request in requests):
            # A target and its obstructing neighbor form one transaction. Splitting
            # them would discard the coordinated repair and repeat known failures.
            return
        if len(requests) > 1:
            queue[:0] = [[request] for request in requests]
        elif len(requests[0]["nets"]) > 1:
            request = requests[0]
            independent_failures = {
                search["net"]
                for attempt in (result or {}).get("attempts", [])
                for trial in attempt.get("trials", [])
                for order in trial.get("orders", [])
                for search in order.get("searches", [])
                if (
                    not search.get("prior_nets")
                    and search.get("candidates") == 0
                    and search.get("budget_limited") is False
                )
            }
            for net in reversed(request["nets"]):
                portion = {**request, "nets": [net]}
                portion.pop("orders", None)
                if "rip_up" in request:
                    portion["rip_up"] = {
                        name: items
                        for name, items in request["rip_up"].items()
                        if name == net["name"]
                    }
                if net["name"] in independent_failures:
                    RoundQueue.split_failed(
                        queue, [portion], island_counts, result, recovery
                    )
                else:
                    queue.insert(0, [portion])
        else:
            recovery = recovery or RecoveryPolicy()
            request = requests[0]
            name = request["nets"][0]["name"]
            count = island_counts.get(name, 0)
            offset = request.get("checkpoint_offset", 0)
            attempts = (result or {}).get("attempts", [])
            if attempts and all(
                item.get("status") == "unsupported_request" for item in attempts
            ):
                return
            available = [
                item["count"]
                for attempt in attempts
                for item in attempt.get("checkpoints", [])
                if item.get("net") == name
            ]
            choices = min(available) if available else count * (count - 1) // 2
            exhausted = any(
                item.get("status") == "checkpoint_exhausted" for item in attempts
            )
            if not exhausted and offset + 1 < min(
                choices, recovery.alternatives_per_stage
            ):
                queue.insert(0, [{**request, "checkpoint_offset": offset + 1}])
            else:
                escalated = next_stage(request, recovery)
                if escalated is not None:
                    # Other pending nets get a turn before an expanded search. The
                    # new corridor is run alone, so it cannot overlap a parallel job.
                    queue.insert(
                        RoundQueue.deferred_position(
                            queue, name, recovery.defer_rounds
                        ),
                        [escalated],
                    )
            if (
                recovery.early_rework
                and request.get("recovery_stage", 0) == 0
                and (recovery.rip_up_candidates or recovery.placement_candidates)
            ):
                marker = deepcopy(request)
                marker.update(recovery_stage=4, checkpoint_offset=0, early_rework=True)
                preference = recovery_preference(request)
                for key in ("orders", "rip_up", "movable", "recovery_preference"):
                    marker.pop(key, None)
                if preference is not None:
                    marker["recovery_preference"] = preference
                if not any(
                    len(group) == 1
                    and group[0].get("early_rework")
                    and RoundQueue.targets(group) == (name,)
                    for group in queue
                ):
                    queue.insert(
                        RoundQueue.deferred_position(
                            queue, name, recovery.defer_rounds
                        ),
                        [marker],
                    )


class NativeErrorCheck:
    """Compares the native errors of a resumed candidate with its baseline."""

    def __call__(self, baseline: dict, candidate: dict) -> None:
        """Allow resolved errors but refuse changed, new or unidentified errors."""

        def error_evidence(summary: dict) -> collections.Counter:
            """Return the error details of a DRC summary; raise RuntimeError when the
            evidence is incomplete."""
            try:
                error_count = sum(
                    value[1] == "error" for value in summary["violation_keys"]
                )
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
                    if (
                        finding.get("severity") != "error"
                        or not finding.get("type")
                        or not finding.get("description")
                        or not isinstance(items, list)
                        or not items
                        or any(
                            not isinstance(item, dict)
                            or not isinstance(item.get("uuid"), str)
                            or not item["uuid"]
                            for item in items
                        )
                    ):
                        raise ValueError("unidentified finding")
                except (TypeError, ValueError, AttributeError) as error:
                    raise RuntimeError("Incomplete native error evidence") from error
            return collections.Counter(details)

        if error_evidence(candidate) - error_evidence(baseline):
            raise RuntimeError(
                "Resumed candidate changes or worsens an existing native error"
            )


verify_unchanged_native_errors = NativeErrorCheck()


class ResumeReportCheck:
    """Compares the native DRC findings of a resumed candidate with its baseline."""

    def __call__(
        self,
        baseline: dict,
        candidate: dict,
        *,
        expected_opens: int,
        identical_inputs: bool = False,
        stable_connection_items: dict[str, str] | None = None,
        allow_open_increase: bool = False,
    ) -> None:
        """Compare independent native DRC findings, including identities, on resume."""
        if not isinstance(allow_open_increase, bool):
            raise ValueError("Open-increase permission must be boolean")
        if identical_inputs:
            fields = (
                "open_connections",
                "errors",
                "warnings",
                "exclusions",
                "parity_findings",
            )
            if any(
                field not in baseline or baseline.get(field) != candidate.get(field)
                for field in fields
            ):
                raise RuntimeError("Identical-source DRC counts disagree")
        baseline_opens = baseline.get("open_connections")
        if (
            isinstance(baseline_opens, bool)
            or not isinstance(baseline_opens, int)
            or baseline_opens < 0
            or isinstance(expected_opens, bool)
            or not isinstance(expected_opens, int)
            or expected_opens < 0
            or candidate.get("open_connections") != expected_opens
            or (not allow_open_increase and expected_opens > baseline_opens)
        ):
            raise RuntimeError(
                "Resumed candidate open-connection count does not match native DRC"
            )
        if candidate.get("ignored_checks") != baseline.get("ignored_checks"):
            raise RuntimeError("Resumed candidate changed ignored DRC checks")
        old_ends, new_ends = (
            baseline.get("dangling_warnings"),
            candidate.get("dangling_warnings"),
        )
        compare_ends = isinstance(old_ends, list) and isinstance(new_ends, list)
        if compare_ends:
            try:
                added_ends = _new_dangling_indices(old_ends, new_ends)
                if added_ends:
                    raise RuntimeError(
                        f"Resumed candidate introduces native violations: {len(added_ends)} dangling-track endpoints"
                    )
            except (TypeError, ValueError) as error:
                raise RuntimeError(
                    "Incomplete native dangling-track evidence"
                ) from error
        for field, label in (
            ("violation_keys", "violations"),
            ("parity_keys", "schematic parity"),
        ):
            try:

                def key(value):
                    """Return a violation key; the connection-width warnings of an
                    identical board ignore their items."""
                    # KiCad can attach a connection-width warning to a different
                    # adjacent segment when rerunning the *identical* board.
                    if (
                        identical_inputs
                        and field == "violation_keys"
                        and value[0] == "connection_width"
                        and value[1] == "warning"
                    ):
                        return value[0], value[1], ()
                    return value[0], value[1], tuple(value[2])

                old = collections.Counter(
                    key(value)
                    for value in baseline[field]
                    if not (
                        compare_ends
                        and field == "violation_keys"
                        and value[0] == "track_dangling"
                    )
                )
                new = collections.Counter(
                    key(value)
                    for value in candidate[field]
                    if not (
                        compare_ends
                        and field == "violation_keys"
                        and value[0] == "track_dangling"
                    )
                )
            except (TypeError, KeyError, IndexError) as error:
                raise RuntimeError(f"Incomplete native {label} evidence") from error
            added_keys = added_violation_keys(
                old, new, stable_connection_items if field == "violation_keys" else None
            )
            if added_keys:
                additions = collections.Counter()
                for finding, count in added_keys.items():
                    additions[f"{finding[1]} {finding[0]}"] += count
                detail = ", ".join(
                    f"{count} {kind}" for kind, count in sorted(additions.items())
                )
                raise RuntimeError(
                    f"Resumed candidate introduces native {label}: {detail}"
                )
        verify_unchanged_native_errors(baseline, candidate)


verify_resume_reports = ResumeReportCheck()


class SameSnapshotInputs:
    """Compares a snapshot with the source it was captured from."""

    def __call__(self, snapshot: Snapshot, original: bytes) -> bool:
        """Prove the copied board and all dependencies match the captured source."""
        try:
            if snapshot.board_path.read_bytes() != original:
                return False
            for source, expected in snapshot.fingerprints.items():
                target = (
                    snapshot.board_path
                    if source == snapshot.source
                    else snapshot.directory / snapshot.input_name(source)
                )
                if not target.is_file() or sha(target) != expected:
                    return False
            return True
        except (OSError, ValueError, AttributeError):
            return False


_same_snapshot_inputs = SameSnapshotInputs()


class ResumeBoardCheck:
    """Rechecks a retained candidate against the saved board with current native
    rules."""

    def __call__(
        self, snapshot: Snapshot, project: Path, expected_opens: int, *, progress=None
    ) -> dict:
        """Recheck a private candidate against the saved board, not stored scores."""
        if progress:
            progress(
                {
                    "event": "stage",
                    "message": "Checking the saved board with current native rules",
                }
            )
        baseline = run_native(prepare_project, project)
        if progress:
            progress(
                {
                    "event": "stage",
                    "message": "Checking the retained preview with current native rules",
                }
            )
        candidate = run_native(prepare_project, snapshot.directory)
        identical = _same_snapshot_inputs(snapshot, snapshot.original)
        stable = None
        if not identical:
            from pcb.route_validation import stable_connection_items

            stable = stable_connection_items(
                snapshot.original, snapshot.board_path.read_bytes()
            )
        verify_resume_reports(
            baseline["drc"],
            candidate["drc"],
            expected_opens=expected_opens,
            identical_inputs=identical,
            stable_connection_items=stable,
        )
        run_native(_verify_resume_connectivity, snapshot.directory, snapshot.original)
        snapshot.check()
        return candidate


verify_resume_board = ResumeBoardCheck()


class ResumeConnectivityCheck:
    """Checks, in a worker process, that a resumed candidate kept the original
    connections."""

    def __call__(self, directory: Path, original_bytes: bytes) -> None:
        """Check that a resumed candidate kept the original nets, connections and
        routing style.

        Inputs:
            directory: Snapshot folder that holds the candidate.
            original_bytes: The saved board.
        Outputs:
            None. RuntimeError when a net assignment changed, an existing connection
            broke or a routing-style issue appeared.
        """
        original_path = directory / "resume-original.kicad_pcb"
        original_path.write_bytes(original_bytes)
        original, routed = (
            NativePreview.board(original_path),
            NativePreview.board(native_path(directory, ".kicad_pcb")),
        )
        from pcb.net_routing.via_guard import verify_no_new_via_pad_overlaps

        verify_no_new_via_pad_overlaps(original, routed)
        original.GetConnectivity().Build(original)
        routed.GetConnectivity().Build(routed)
        previous = _net_identities(original)
        current = _net_identities(routed)
        removed = {item.m_Uuid.AsString() for item in original.GetTracks()} - {
            item.m_Uuid.AsString() for item in routed.GetTracks()
        }
        if any(
            current.get(identifier) != net
            for identifier, net in previous.items()
            if identifier not in removed
        ):
            raise RuntimeError("Resumed candidate changed an existing copper net")
        broken = _broken_existing_connections(original, routed, removed)
        if broken:
            nets = ", ".join(sorted({item["net"] for item in broken}))
            raise RuntimeError(
                "Resumed candidate broke an existing copper connection: " + nets
            )
        old_style = normalize_segment_identities(
            audit_style(original), _segment_identities(original_bytes)
        )
        new_style = normalize_segment_identities(
            audit_style(routed),
            _segment_identities((native_path(directory, ".kicad_pcb")).read_bytes()),
        )
        if new_style_issues(old_style, new_style):
            raise RuntimeError("Resumed candidate introduced routing style issues")


_verify_resume_connectivity = ResumeConnectivityCheck()


class ReferenceRepairs:
    """Compiles reference-copper repairs in a native worker process."""

    def __call__(
        self, directory: Path, reference: dict, policy: dict, workers: int
    ) -> dict:
        """Compile repairs for the reference copper of the snapshot's board.

        Inputs:
            directory: Snapshot folder.
            reference: Reference-copper report.
            policy: Routing policy.
            workers: One or two parallel searches.
        Outputs:
            The repair plan with its rounds and blockers.
        """
        return compile_reference_repairs(
            NativePreview.board(native_path(directory, ".kicad_pcb")),
            reference,
            policy,
            workers=workers,
        )


_reference_repairs_native = ReferenceRepairs()


class CumulativeReferenceCheck:
    """Compares reference copper across a two-step branch of the board."""

    def __call__(
        self, original: Path, candidate: Path, critical_nets: tuple[str, ...]
    ) -> dict:
        """Compare the entire two-step branch, including independently refilled
        zones."""
        import tempfile
        from pcb.net_routing.native import load_board
        from pcb.net_routing.reference import compare_reference, sample_reference

        # A prior/checkpoint PCB often has a different stem. Refilling it in place
        # would silently use defaults instead of the candidate's .kicad_pro/.dru,
        # inventing a return-copper regression even for unchanged physical copper.
        project = next(
            (
                path
                for path in (
                    candidate.with_suffix(".kicad_pro"),
                    native_path(candidate.parent, ".kicad_pro"),
                    original.with_suffix(".kicad_pro"),
                    native_path(original.parent, ".kicad_pro"),
                )
                if path.is_file()
            ),
            None,
        )
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
            return asdict(
                compare_reference(
                    sample_reference(boards[0], set(critical_nets)),
                    sample_reference(boards[1], set(critical_nets)),
                )
            )


_verify_cumulative_reference_native = CumulativeReferenceCheck()


class NeutralLookahead:
    """Tries follow-up previews on a temporary branch of an equal-open preview."""

    def __call__(
        self,
        snapshot: Snapshot,
        neutral_board: bytes,
        initial: dict,
        policy: dict,
        *,
        best_opens: int,
        preferred_nets: tuple[str, ...],
        workers: int,
        seconds_per_batch: float | None,
        progress=None,
    ) -> dict | None:
        """Try two fresh follow-ups without replacing the best retained checkpoint.

        Only a strictly lower open count can leave this disposable branch, after an
        independent comparison against the pre-branch board. Equal-open changes do
        not claim a symmetry or cost improvement.
        """
        if not PreviewJudge.neutral_eligible(initial, best_opens):
            raise ValueError("Lookahead requires a checked neutral preview")
        if hashlib.sha256(neutral_board).hexdigest() != initial.get("candidate_sha256"):
            raise RuntimeError("Neutral preview differs from its native validation")
        if sha(snapshot.board_path) != initial.get("source_sha256"):
            raise RuntimeError("Neutral preview source changed")
        with Snapshot(snapshot.board_path) as branch:
            schematic = (native_path(branch.directory, ".kicad_sch")).read_bytes()
            protected = ProtectedState.capture(
                branch.original,
                schematic,
                movable=MoveScope.from_config(policy.get("placement")),
            )
            if protected.differences(neutral_board, schematic):
                raise RuntimeError("Neutral preview changed a protected design item")
            branch.board_path.write_bytes(neutral_board)
            if progress:
                progress(
                    {
                        "event": "stage",
                        "message": "Trying a temporary rearrangement and replanning nearby work",
                    }
                )
            reference = initial["candidate_reference"]
            critical = tuple(reference.get("high_speed_nets", []))
            plan = _replan(branch, policy, reference, workers=workers, single=True)
            choices = [request for group in plan["rounds"] for request in group]
            preferred = set(preferred_nets) | set(initial.get("nets", []))
            choices = [
                request
                for request in choices
                if preferred.intersection(RoundQueue.targets([request]))
            ]
            primary = set(preferred_nets)
            choices.sort(
                key=lambda request: (
                    not bool(primary.intersection(RoundQueue.targets([request])))
                )
            )
            for request in choices[:2]:
                request = deepcopy(request)
                if seconds_per_batch is not None:
                    request["search"]["seconds"] = seconds_per_batch
                if progress:
                    progress(
                        {
                            "event": "stage",
                            "message": "Checking temporary follow-up: "
                            + ", ".join(RoundQueue.targets([request])),
                        }
                    )
                result = run_native(
                    NativePreview.preview_round,
                    branch.directory,
                    [request],
                    1,
                    critical,
                )
                validation = result.get("validation", {})
                comparison = validation.get("reference_comparison", {})
                opens = validation.get("candidate_unconnected")
                if (
                    result.get("status") != "validated_preview"
                    or result.get("new_style_issues") not in ((), [])
                    or validation.get("status") != "no_new_drc_violations"
                    or validation.get("schematic_parity_checked") is not True
                    or not isinstance(opens, int)
                    or isinstance(opens, bool)
                    or opens >= best_opens
                    or comparison.get("status")
                    not in {
                        "unchanged_sampled",
                        "unverified",
                        "no_high_speed_classification",
                    }
                ):
                    continue
                candidate = CampaignPlanner(branch.directory).plan(
                    "pcb", result["changes"]
                )
                if (
                    candidate.result_sha256 != result["candidate_sha256"]
                    or sha(branch.board_path) != result["source_sha256"]
                ):
                    raise RuntimeError(
                        "Lookahead candidate no longer matches its validation"
                    )
                if protected.differences(candidate.after, schematic):
                    raise RuntimeError("Lookahead changed a protected design item")
                branch.board_path.write_bytes(candidate.after)
                cumulative = run_native(
                    _verify_cumulative_reference_native,
                    branch.source,
                    branch.board_path,
                    critical,
                )
                if cumulative.get("status") not in {"unchanged_sampled", "unverified"}:
                    return None
                verified = verify_resume_board(
                    branch, snapshot.directory, opens, progress=progress
                )
                branch.check()
                native = verified["drc"]
                return {
                    "board": candidate.after,
                    "nets": result.get("nets", []),
                    "attempts": result.get("attempts", []),
                    "candidate_reference": verified["reference_copper"],
                    "candidate_geometry": verified["geometry"],
                    "validation": {
                        **validation,
                        "candidate_unconnected": native["open_connections"],
                        "candidate_errors": native["errors"],
                        "candidate_warnings": native["warnings"],
                        "candidate_schematic_parity": native["parity_findings"],
                        "reference_comparison": cumulative,
                    },
                }
        return None


_neutral_lookahead = NeutralLookahead()


class StalledResume:
    """Reactivates a stalled run."""

    def __call__(self, state: dict, plan: dict) -> bool:
        """Make a stalled run active again with a new plan.

        Inputs:
            state: Run state, changed in place.
            plan: New work plan.
        Outputs:
            False when the plan has no rounds. Otherwise True, after the state is set to
            running with the new queue, blocked nets and island counts, no failed sweeps
            and one more resume.
        """
        if not plan["rounds"]:
            return False
        state.update(
            status="running",
            queue=plan["rounds"],
            blocked_nets=plan["blocked_nets"],
            island_counts=plan["island_counts"],
            failed_sweeps=0,
            sweep_start_opens=state["best_preview_opens"],
            resume_count=state.get("resume_count", 0) + 1,
        )
        return True


resume_stalled = StalledResume()


class SearchBudget:
    """Applies runtime search limits to routing requests."""

    def __call__(self, requests: list[dict], limits: dict) -> None:
        """Override bounded search controls without changing physical rules or costs."""
        ceilings = {"max_cells": 8_000_000, "max_expansions": 2_000_000}
        ranges = {"step_mm": (0.025, 0.5), "heuristic_weight": (1.0, 20.0)}
        if not isinstance(limits, dict) or set(limits) - (set(ceilings) | set(ranges)):
            raise ValueError("Invalid runtime search budget")
        for key, value in limits.items():
            if key in ceilings:
                valid = (
                    not isinstance(value, bool)
                    and isinstance(value, int)
                    and 1 <= value <= ceilings[key]
                )
            else:
                valid = (
                    not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and math.isfinite(value)
                    and ranges[key][0] <= value <= ranges[key][1]
                )
            if not valid:
                raise ValueError("Invalid runtime search budget")
        for request in requests:
            for options in (request.setdefault("shared", {}), *request.get("nets", [])):
                updated = dict(limits)
                if "step_mm" in limits and "step_mm" in options:
                    current = options["step_mm"]
                    if (
                        isinstance(current, bool)
                        or not isinstance(current, (int, float))
                        or not math.isfinite(current)
                        or current <= 0
                    ):
                        raise ValueError("Invalid existing search grid")
                    # A runtime grid ceiling must not undo stage-two refinement or
                    # a deliberately finer maintained per-net escape grid.
                    updated["step_mm"] = min(current, limits["step_mm"])
                options.update(updated)


apply_search_budget = SearchBudget()


class RunProject:
    """Starts, resumes or renews one bounded board-wide search run."""

    def __call__(
        self,
        project: Path,
        *,
        resume: str | None = None,
        renew: str | None = None,
        max_rounds: int | None = None,
        workers: int = 2,
        seconds_per_batch: float | None = None,
        progress=None,
        resource_limits: dict | None = None,
        previous_board: Path | None = None,
        previous_policy: Path | None = None,
        single_net_search: bool = False,
        keep_placement: bool = False,
        search_cells: int | None = None,
        search_expansions: int | None = None,
        search_step_mm: float | None = None,
        search_heuristic_weight: float | None = None,
    ) -> dict:
        """Search with bounded sweeps; retain only improved, validated private
        candidates.

        Inputs:
            project: Project folder; its saved PCB is never written.
            resume: Run ID of a saved run to continue, or None.
            renew: Run ID of a saved run to revalidate with current code, or None.
            max_rounds: Most search rounds to run in this call, or None for all.
            workers: One or two parallel searches.
            seconds_per_batch: Search time limit for each batch, in seconds.
            progress: Callback that receives event dicts.
            resource_limits: Memory budget recorded in the run state.
            previous_board, previous_policy: Board and policy of a run made with
                other layer metadata; rebases that run's checkpoint when renewing.
            single_net_search: Search one net per batch.
            keep_placement: Forbid placement changes.
            search_cells, search_expansions, search_step_mm,
                search_heuristic_weight: Runtime search limits, or None.
        Outputs:
            The saved run's status. Raises ValueError for contradictory or
            out-of-range options.
        """
        if resume and renew:
            raise ValueError("Choose either --resume or --renew")
        if not isinstance(single_net_search, bool):
            raise ValueError("single_net_search must be true or false")
        if not isinstance(keep_placement, bool):
            raise ValueError("keep_placement must be true or false")
        if (previous_board is not None or previous_policy is not None) and (
            not renew or previous_board is None or previous_policy is None
        ):
            raise ValueError(
                "Layer rebase requires --renew and both previous board and previous policy"
            )
        if workers not in {1, 2} or (
            max_rounds is not None and (isinstance(max_rounds, bool) or max_rounds < 0)
        ):
            raise ValueError("Use one or two workers and a nonnegative round limit")
        if seconds_per_batch is not None and (
            isinstance(seconds_per_batch, bool) or seconds_per_batch <= 0
        ):
            raise ValueError("Seconds per batch must be positive")
        requested_search = {
            key: value
            for key, value in (
                ("max_cells", search_cells),
                ("max_expansions", search_expansions),
                ("step_mm", search_step_mm),
                ("heuristic_weight", search_heuristic_weight),
            )
            if value is not None
        }
        apply_search_budget([], requested_search)
        return RunSession(
            project,
            resume=resume,
            renew=renew,
            max_rounds=max_rounds,
            workers=workers,
            seconds_per_batch=seconds_per_batch,
            progress=progress,
            resource_limits=resource_limits,
            previous_board=previous_board,
            previous_policy=previous_policy,
            single_net_search=single_net_search,
            keep_placement=keep_placement,
            requested_search=requested_search,
        ).execute()


run_project = RunProject()


class RunSession:
    """One run over a private snapshot: its options, its state and its phases.

    The session holds the results-folder lock while it runs. Its phases are
    restoring a saved run or starting a new one, preparing the search, running
    search rounds and recording the final status.
    """

    def __init__(
        self,
        project: Path,
        *,
        resume: str | None,
        renew: str | None,
        max_rounds: int | None,
        workers: int,
        seconds_per_batch: float | None,
        progress,
        resource_limits: dict | None,
        previous_board: Path | None,
        previous_policy: Path | None,
        single_net_search: bool,
        keep_placement: bool,
        requested_search: dict,
    ):
        """Resolve the project, read its routing policy and name the run.

        Inputs:
            project: Project folder.
            The other arguments are the checked options of run_project, and
            requested_search holds the runtime search limits asked for on this call.
        Outputs:
            None. Creates the autoroute results folder when it is missing.
        """
        self.resume = resume
        self.renew = renew
        self.max_rounds = max_rounds
        self.workers = workers
        self.seconds_per_batch = seconds_per_batch
        self.progress = progress
        self.resource_limits = resource_limits
        self.previous_board = previous_board
        self.previous_policy = previous_policy
        self.single_net_search = single_net_search
        self.keep_placement = keep_placement
        self.requested_search = requested_search
        self.project = project.resolve()
        self.assets = asset_root(self.project)
        self.policy_path = config_path(self.project, "autopcb.json")
        self.policy, self.policy_hash = read_config(self.policy_path)
        if keep_placement:
            self.policy = {
                **self.policy,
                "recovery": {
                    **self.policy.get("recovery", {}),
                    "placement_candidates": 0,
                },
            }
        self.recovery = RecoveryPolicy.from_config(self.policy.get("recovery"))
        self.output = results_path(self.project, "autoroute")
        self.output.mkdir(parents=True, exist_ok=True)
        self.run_id = _run_id(resume) if resume else uuid.uuid4().hex
        self.state_path = self.output / f"{self.run_id}.json"

    def stage(self, message: str) -> None:
        """Report a stage message through the progress callback, if any."""
        if self.progress:
            self.progress({"event": "stage", "message": message})

    def check_sources(self) -> None:
        """Raise RuntimeError when the snapshot, inputs or policy changed."""
        _check_sources(
            self.snapshot,
            self.project,
            self.assets,
            self.inputs,
            self.policy_path,
            self.policy_hash,
        )

    def critical_nets(self) -> tuple[str, ...]:
        """Return the high-speed nets of the retained preview's reference copper."""
        return tuple(self.state["reference"].get("high_speed_nets", []))

    def execute(self) -> dict:
        """Run the search under the results lock and return the saved status."""
        with _exclusive(self.output / "active.lock"):
            self.stage("Preparing a private routing board; saved PCB stays unchanged")
            with Snapshot(native_path(self.project, ".kicad_pcb")) as snapshot:
                self.snapshot = snapshot
                self.inputs = _source_hashes(snapshot, self.project, self.assets)
                self.schematic = (
                    native_path(snapshot.directory, ".kicad_sch")
                ).read_bytes()
                self.protected = ProtectedState.capture(
                    snapshot.original,
                    self.schematic,
                    movable=None
                    if self.keep_placement
                    else MoveScope.from_config(self.policy.get("placement")),
                )
                if self.resume or self.renew:
                    if not self.restore():
                        return read_status(self.project, self.run_id)
                else:
                    self.begin()
                self.prepare_search()
                self.search()
                self.finish()
        return read_status(self.project, self.run_id)

    def restore(self) -> bool:
        """Load a saved run, recheck its candidate and rebuild its queue when needed.

        Outputs:
            False when a stalled run has no further work to plan, so the saved
            status is the result; True when the search should continue.
        """
        snapshot = self.snapshot
        self.stage(
            "Rechecking the saved preview, protected items and source compatibility"
        )
        previous_id = _run_id(self.resume or self.renew)
        previous_path = stored_result(self.project, "autoroute", f"{previous_id}.json")
        state = json.loads(previous_path.read_text(encoding="utf-8"))
        self.state = state
        if state.get("schema") != 1 or state.get("run_id") != previous_id:
            raise ValueError("Invalid AutoPCB run state")
        if (
            previous_path.parent != self.output
            and (previous_path.parent / "active.lock").exists()
        ):
            raise RuntimeError("A legacy routing job is active for this board")
        mode_changed = (
            self.single_net_search and not state.get("single_net_search", False)
        ) or (self.keep_placement and not state.get("keep_placement", False))
        state["single_net_search"] = self.single_net_search or state.get(
            "single_net_search", False
        )
        self.keep_placement = self.keep_placement or state.get("keep_placement", False)
        state["keep_placement"] = self.keep_placement
        if self.keep_placement:
            self.policy = {
                **self.policy,
                "recovery": {
                    **self.policy.get("recovery", {}),
                    "placement_candidates": 0,
                },
            }
            self.recovery = RecoveryPolicy.from_config(self.policy["recovery"])
            self.protected = ProtectedState.capture(snapshot.original, self.schematic)
        if self.previous_board is not None:
            from pcb.net_routing.layer_rebase import rebase_checkpoint

            if sha(self.policy_path) != self.policy_hash:
                raise RuntimeError("Routing policy changed during layer rebase")
            candidate = rebase_checkpoint(
                state["checkpoint"],
                self.inputs,
                snapshot.tool_hashes,
                self.previous_board.read_bytes(),
                snapshot.original,
                self.previous_policy.read_bytes(),
                self.policy_path.read_bytes(),
            )
            state["layer_metadata_rebased_from"] = previous_id
        else:
            candidate = decode_checkpoint(
                state["checkpoint"],
                self.inputs,
                snapshot.tool_hashes,
                self.policy_hash,
                allow_tool_changes=bool(self.renew),
            )
        if self.protected.differences(candidate, self.schematic):
            raise RuntimeError("Routing candidate changed a protected design item")
        snapshot.board_path.write_bytes(candidate)
        state["resource_limits"] = self.resource_limits
        if self.renew or mode_changed:
            verified = self.rebuild_queue(previous_id, mode_changed, candidate)
        else:
            with RunState.record_failures(self.state_path, state):
                verified = verify_resume_board(
                    snapshot,
                    self.project,
                    state["best_preview_opens"],
                    progress=self.progress,
                )
        self.check_sources()
        state.pop("failure", None)
        state["reference"] = verified["reference_copper"]
        self.measured = verified
        if self.renew or mode_changed:
            _save(self.state_path, state)
            self.stage(
                "Search batches rebuilt in the current run; saved placement is fixed"
                if mode_changed
                else "Preview renewed; pending recovery stages retained and requests rebuilt with current code"
            )
        if state["status"] == "stalled":
            plan = _replan(
                snapshot,
                self.policy,
                state["reference"],
                workers=self.workers,
                single=True,
                offset=state.get("resume_count", 0) + 1,
            )
            if not resume_stalled(state, plan):
                return False
        return True

    def rebuild_queue(
        self, previous_id: str, mode_changed: bool, candidate: bytes
    ) -> dict:
        """Verify the retained candidate and replace the queue with current requests.

        Inputs:
            previous_id: Run ID of the saved run.
            mode_changed: True when single-net search or fixed placement was newly
                requested, which discards the old queue.
            candidate: The retained candidate board.
        Outputs:
            The native verification of the candidate. The saved run stays intact
            when verification or replanning fails.
        """
        # Keep the old run intact even if verification or replanning
        # fails. No old campaign/request is executed by renewal.
        state = self.state
        snapshot = self.snapshot
        verified = verify_resume_board(
            snapshot,
            self.project,
            state["best_preview_opens"],
            progress=self.progress,
        )
        plan = _replan(
            snapshot,
            self.policy,
            verified["reference_copper"],
            workers=self.workers,
            single=state.get("single_net_search", False),
        )
        repairs = run_native(
            _reference_repairs_native,
            snapshot.directory,
            verified["reference_copper"],
            self.policy,
            self.workers,
        )
        insert_reference_repairs(plan, repairs, self.policy)
        fresh = plan["rounds"]
        retries = (
            []
            if mode_changed
            else RoundQueue.rejected_retries(
                fresh, state.get("last_result"), self.recovery
            )
        )
        pending = (
            fresh
            if mode_changed
            else renew_queue(fresh, state.get("queue", []), self.recovery)
        )
        pending_keys = {search_key(request) for group in pending for request in group}
        retries = [
            group for group in retries if search_key(group[0]) not in pending_keys
        ]
        plan["rounds"] = retries + pending
        for group in retries:
            for name in RoundQueue.targets(group):
                state.get("target_attempts", {}).pop(name, None)
        state.update(
            run_id=self.run_id,
            renewed_from=previous_id,
            status="running",
            publication_enabled=False,
            queue=plan["rounds"],
            blocked_nets=plan["blocked_nets"],
            island_counts=plan["island_counts"],
            failed_sweeps=0,
            sweep_start_opens=state["best_preview_opens"],
            reference=verified["reference_copper"],
            reference_repair_blockers=repairs["blockers"],
        )
        if self.run_id == previous_id:
            state.pop("renewed_from", None)
        state.pop("attempted_searches", None)
        # Code changes make exact search failures worth retrying, but
        # they do not change how often each target has been served
        # on this same preview. The board comparison below resets
        # fairness if the candidate actually changed.
        state["checkpoint"] = encode_checkpoint(
            candidate, self.inputs, snapshot.tool_hashes, self.policy_hash
        )
        return verified

    def begin(self) -> None:
        """Prepare the board natively, plan its work and save a new run's state."""
        snapshot = self.snapshot
        preparation = run_native(
            prepare_project, self.project, progress=self.progress, forward_progress=True
        )
        if self.progress:
            self.progress(
                {
                    "event": "inventory",
                    "open_connections": preparation["drc"]["open_connections"],
                    "errors": preparation["drc"]["errors"],
                    "warnings": preparation["drc"]["warnings"],
                    "work_nets": len(preparation["work"]["items"]),
                    "copper_layers": preparation["copper_layers"],
                }
            )
        self.check_sources()
        if preparation["tool_sha256"] != snapshot.tool_hashes:
            raise RuntimeError("Routing tools changed during preparation")
        self.stage("Planning checkpoint order and reference-copper repairs")
        plan = make_work_plan(
            preparation,
            self.policy,
            workers=self.workers,
            max_batch_nets=1 if self.single_net_search else 3,
        )
        repairs = run_native(
            _reference_repairs_native,
            snapshot.directory,
            preparation["reference_copper"],
            self.policy,
            self.workers,
        )
        insert_reference_repairs(plan, repairs, self.policy)
        state = {
            "schema": 1,
            "run_id": self.run_id,
            "status": "running",
            "publication_enabled": False,
            "single_net_search": self.single_net_search,
            "keep_placement": self.keep_placement,
            "reference_repair_blockers": repairs["blockers"],
            "saved_board_opens": preparation["drc"]["open_connections"],
            "best_preview_opens": preparation["drc"]["open_connections"],
            "accepted_preview_batches": 0,
            "attempted_rounds": 0,
            "failed_sweeps": 0,
            "sweep_start_opens": preparation["drc"]["open_connections"],
            "reference": preparation["reference_copper"],
            "blocked_nets": plan["blocked_nets"],
            "island_counts": plan["island_counts"],
            "baseline_blockers": preparation["completion_issues"],
            "queue": plan["rounds"],
            "last_result": None,
            "elapsed_seconds": 0.0,
        }
        state["checkpoint"] = encode_checkpoint(
            snapshot.original, self.inputs, snapshot.tool_hashes, self.policy_hash
        )
        _save(self.state_path, state)
        self.state = state
        self.measured = preparation

    def prepare_search(self) -> None:
        """Apply runtime search limits, record the first progress sample and reset the
        round bookkeeping."""
        state = self.state
        state["resource_limits"] = self.resource_limits
        self.search_limits = {**state.get("search_limits", {}), **self.requested_search}
        apply_search_budget([], self.search_limits)
        state["search_limits"] = self.search_limits
        route_options = deepcopy(self.policy.get("route", {}))
        net_options = deepcopy(self.policy.get("net_overrides", {}))
        apply_search_budget(
            [{"shared": route_options, "nets": list(net_options.values())}],
            self.search_limits,
        )
        self.policy = {
            **self.policy,
            "route": route_options,
            "net_overrides": net_options,
        }
        if self.search_limits and self.progress:
            self.progress(
                {
                    "event": "stage",
                    "message": "Runtime search limits: "
                    + ", ".join(
                        f"{key}={value:,}" for key, value in self.search_limits.items()
                    ),
                }
            )
        _progress_sample(
            state,
            sha(self.snapshot.board_path),
            self.measured["drc"],
            self.measured["geometry"],
            state["elapsed_seconds"],
        )
        with RunState.record_failures(self.state_path, state):
            _publish_progress(self.project, self.inputs, state, self.progress)
        self.last_progress_update = time.monotonic()
        current_board = sha(self.snapshot.board_path)
        if state.get("attempted_board_sha256") != current_board:
            state.update(
                attempted_board_sha256=current_board,
                attempted_searches=[],
                target_attempts={},
            )
        self.attempted = set(state.get("attempted_searches", []))
        self.target_attempts = state.setdefault("target_attempts", {})
        self.start = time.monotonic()
        self.rounds = 0

    def search(self) -> None:
        """Run search rounds until the queue empties or the round limit is reached.

        A failure keeps the last committed state and records the failure on it.
        """
        state = self.state
        with RunState.record_failures(self.state_path, state):
            while state["queue"] and (
                self.max_rounds is None or self.rounds < self.max_rounds
            ):
                SearchRound(self).execute()

    def finish(self) -> None:
        """Record the final status and progress, then save the run state."""
        state = self.state
        state["status"] = PreviewStatus.stopped(state)
        state["blockers"] = [
            "Checked manufacturing export requires independent design checks and a source-bound electrical review."
        ]
        self.check_sources()
        _progress_elapsed(state, sha(self.snapshot.board_path))
        _publish_progress(self.project, self.inputs, state, self.progress)
        _save(self.state_path, state)


class SearchRound:
    """One pass of the search loop: pick requests, preview them, then accept or requeue.

    The round holds the requests and results of this pass; run-wide state such
    as the queue, attempt counts and best open count stays on the session.
    """

    def __init__(self, run: RunSession):
        """Start a round on a session.

        Inputs:
            run: The session that owns the queue and the private snapshot.
        """
        self.run = run

    def execute(self) -> None:
        """Run one round and update the session's state, board and saved run.

        A round whose requests were all tried before ends without counting.
        """
        run = self.run
        self.started = time.monotonic()
        run.check_sources()
        if not self.select():
            return
        self.recover()
        self.announce()
        self.preview()
        run.check_sources()
        self.judge()
        if not self.accepted:
            self.retry_failed()
        else:
            self.requeue_accepted()
        self.record()

    def select(self) -> bool:
        """Pop the next fair round, drop requests already tried and pair ordinary
        retries.

        Outputs:
            False when every request was tried before; True otherwise.
        """
        run = self.run
        state = run.state
        priorities = run.policy.get("priorities", {})
        requests = RoundQueue.pop_fair(state["queue"], run.target_attempts, priorities)
        if run.seconds_per_batch is not None:
            for request in requests:
                request["search"]["seconds"] = run.seconds_per_batch
        apply_search_budget(requests, run.search_limits)
        fresh = [
            request for request in requests if search_key(request) not in run.attempted
        ]
        state["skipped_duplicate_batches"] = (
            state.get("skipped_duplicate_batches", 0) + len(requests) - len(fresh)
        )
        requests = fresh
        if not requests:
            return False
        paired = RoundQueue.pair_ordinary(
            state["queue"],
            requests,
            run.target_attempts,
            run.policy.get("priorities", {}),
            run.workers,
        )
        if len(paired) > len(requests):
            other = paired[-1]
            if run.seconds_per_batch is not None:
                other["search"]["seconds"] = run.seconds_per_batch
            apply_search_budget([other], run.search_limits)
            if search_key(other) in run.attempted:
                state["skipped_duplicate_batches"] += 1
            else:
                requests = paired
        self.parallel_ordinary = len(requests) > len(fresh)
        self.targets = RoundQueue.targets(requests)
        self.request_keys = {search_key(request) for request in requests}
        self.requests = requests
        return True

    def recover(self) -> None:
        """Compile a copper-recovery request into repair alternatives.

        A queued stage-four marker is replaced by the repair requests that fit the
        current board; when none fit, the round's result is a blocked recovery.
        """
        run = self.run
        state = run.state
        requests = self.requests
        self.blocked_recovery = None
        self.parallel_repair = False
        self.combined_ordinary = False
        self.alternatives = []
        self.selected_index = None
        if (
            len(requests) == 1
            and requests[0].get("recovery_stage") == 4
            and not requests[0].get("rip_up")
            and not requests[0].get("movable")
        ):
            preference = requests[0].pop("recovery_preference", None)
            requests[0].pop("early_rework", None)
            compiled = run_native(
                NativePreview.reroutes,
                run.snapshot.directory,
                requests[0],
                run.policy,
                run.critical_nets(),
            )
            state["reroute_candidates"] = compiled["candidates"]
            state["reroute_blockers"] = compiled["blockers"]
            if compiled["requests"]:
                candidates = prioritize_recovery_requests(
                    compiled["requests"], preference
                )
                requests, pending = PreviewJudge.repair_alternatives(
                    candidates, run.workers
                )
                self.parallel_repair = len(requests) > 1
                RoundQueue.defer_alternatives(
                    state["queue"],
                    pending,
                    defer_rounds=run.recovery.defer_rounds,
                )
                self.request_keys.update(search_key(request) for request in requests)
            else:
                self.blocked_recovery = {
                    "status": "no_candidate_found",
                    "attempts": [
                        {
                            "status": "copper_recovery_blocked",
                            "reason": "; ".join(compiled["blockers"][:3])
                            or "No eligible copper in the sampled corridor",
                        }
                    ],
                }
        self.requests = requests
        self.names = list(
            dict.fromkeys(
                net["name"] for request in requests for net in request["nets"]
            )
        )

    def announce(self) -> None:
        """Report the nets about to be searched through the progress callback."""
        run = self.run
        state = run.state
        if run.progress:
            run.progress(
                {
                    "event": "search",
                    "run_id": run.run_id,
                    "nets": self.names,
                    "recovery_stage": max(
                        request.get("recovery_stage", 0) for request in self.requests
                    ),
                    "round": state["attempted_rounds"] + 1,
                    "alternative_count": len(self.requests)
                    if self.parallel_repair or self.parallel_ordinary
                    else 0,
                    "preview_opens": state["best_preview_opens"],
                    "saved_opens": state["saved_board_opens"],
                }
            )

    def preview(self) -> None:
        """Search and validate the requests in native worker processes.

        Sets the round's result, and its alternatives when several proposals
        competed.
        """
        run = self.run
        state = run.state
        requests = self.requests
        if self.blocked_recovery:
            self.result = self.blocked_recovery
        elif self.parallel_ordinary:
            decision = run_native(
                NativePreview.preview_ordinary,
                run.snapshot.directory,
                requests,
                run.workers,
                run.critical_nets(),
                state["best_preview_opens"],
            )
            self.result = decision["result"]
            self.combined_ordinary = decision["combined"]
            self.alternatives = decision["alternatives"]
            self.selected_index = decision["selected_index"]
        elif self.parallel_repair:
            self.alternatives = run_native(
                NativePreview.preview_alternatives,
                run.snapshot.directory,
                requests,
                run.workers,
                run.critical_nets(),
            )
            selected = PreviewJudge.select_alternative(
                self.alternatives, state["best_preview_opens"]
            )
            self.selected_index = next(
                i for i, item in enumerate(self.alternatives) if item is selected
            )
            self.result = {
                **selected,
                "attempts": [
                    attempt
                    for item in self.alternatives
                    for attempt in item.get("attempts", [])
                ],
            }
        else:
            self.result = run_native(
                NativePreview.preview_round,
                run.snapshot.directory,
                requests,
                run.workers,
                run.critical_nets(),
            )

    def judge(self) -> None:
        """Decide whether the round's preview improves the retained board.

        Sets accepted, the reason it was not accepted and the batches a combined
        preview left unfinished.
        """
        self.accepted = False
        self.unfinished_combined = []
        self.reason = self.result["status"]
        if self.result["status"] == "validated_preview":
            self.validated()

    def validated(self) -> None:
        """Check a validated preview against the board and accept an improvement.

        An equal-open preview may be tried further by a temporary lookahead.
        Raises RuntimeError when the preview no longer matches the board or changes a
        protected design item.
        """
        run = self.run
        state = run.state
        requests = self.requests
        if (
            self.combined_ordinary
            or not (self.parallel_repair or self.parallel_ordinary)
        ) and len(requests) > 1:
            self.unfinished_combined = RoundQueue.unfinished_combined(
                requests, self.result
            )
        plan = CampaignPlanner(run.snapshot.directory).plan(
            "pcb", self.result["changes"]
        )
        if (
            plan.result_sha256 != self.result["candidate_sha256"]
            or sha(run.snapshot.board_path) != self.result["source_sha256"]
        ):
            raise RuntimeError("Preview no longer matches the validated board")
        candidate_path = run.snapshot.directory / "candidate.kicad_pcb"
        candidate_path.write_bytes(plan.after)
        candidate_bytes = plan.after
        style = self.result["new_style_issues"]
        opens = self.result["validation"]["candidate_unconnected"]
        if run.protected.differences(candidate_bytes, run.schematic):
            raise RuntimeError("Preview changed a protected design item")
        if style:
            self.reason = "new routing style issues: " + ", ".join(style)
        elif not PreviewJudge.is_improvement(
            state["best_preview_opens"],
            opens,
            self.result["validation"].get("reference_comparison", {}),
        ):
            self.reason = "no improvement in connectivity or sampled reference coverage"
            if PreviewJudge.neutral_eligible(self.result, state["best_preview_opens"]):
                followup = _neutral_lookahead(
                    run.snapshot,
                    candidate_bytes,
                    self.result,
                    run.policy,
                    best_opens=state["best_preview_opens"],
                    preferred_nets=self.targets,
                    workers=run.workers,
                    seconds_per_batch=run.seconds_per_batch,
                    progress=run.progress,
                )
                run.check_sources()
                if followup is not None:
                    candidate_bytes = followup.pop("board")
                    if run.protected.differences(candidate_bytes, run.schematic):
                        raise RuntimeError("Lookahead changed a protected design item")
                    self.result = {**self.result, **followup}
                    opens = self.result["validation"]["candidate_unconnected"]
        if not style and PreviewJudge.is_improvement(
            state["best_preview_opens"],
            opens,
            self.result["validation"].get("reference_comparison", {}),
        ):
            self.accept(candidate_bytes, opens)

    def accept(self, candidate_bytes: bytes, opens: int) -> None:
        """Make an improved candidate the retained board and record its progress sample.

        Inputs:
            candidate_bytes: The improved board.
            opens: Open connections of that board.
        """
        run = self.run
        state = run.state
        run.snapshot.board_path.write_bytes(candidate_bytes)
        state["best_preview_opens"] = opens
        state["accepted_preview_batches"] += 1
        state["reference"] = self.result["candidate_reference"]
        validation = self.result["validation"]
        _progress_sample(
            state,
            sha(run.snapshot.board_path),
            {
                "open_connections": opens,
                "errors": validation["candidate_errors"],
                "warnings": validation["candidate_warnings"],
                "parity_findings": validation["candidate_schematic_parity"],
            },
            self.result["candidate_geometry"],
            state["elapsed_seconds"] + time.monotonic() - run.start,
        )
        self.accepted = True

    def retry_failed(self) -> None:
        """Split the failed requests back into the queue and count the attempt."""
        run = self.run
        state = run.state
        if self.parallel_ordinary and not self.combined_ordinary:
            for request, alternative in strict_zip(self.requests, self.alternatives):
                RoundQueue.split_failed(
                    state["queue"],
                    [request],
                    state.get("island_counts", {}),
                    alternative,
                    run.recovery,
                )
        else:
            RoundQueue.split_failed(
                state["queue"],
                self.requests,
                state.get("island_counts", {}),
                self.result,
                run.recovery,
            )
        run.attempted.update(self.request_keys)
        for name in self.targets:
            run.target_attempts[name] = run.target_attempts.get(name, 0) + 1

    def requeue_accepted(self) -> None:
        """After an accepted preview, requeue unused work and drop work for connected
        nets."""
        run = self.run
        state = run.state
        # Previous failures refer to different copper now; they
        # may be worth retrying after a validated improvement.
        # Preserve the fairness counters: they control order,
        # not whether those searches may be tried again.
        run.attempted.clear()
        if self.parallel_ordinary and not self.combined_ordinary:
            for index, request in enumerate(self.requests):
                if index != self.selected_index:
                    target = request["nets"][0]["name"]
                    state["queue"].insert(
                        RoundQueue.deferred_position(
                            state["queue"],
                            target,
                            run.recovery.defer_rounds,
                        ),
                        [request],
                    )
        for request in self.unfinished_combined:
            target = request["nets"][0]["name"]
            state["queue"].insert(
                RoundQueue.deferred_position(
                    state["queue"], target, run.recovery.defer_rounds
                ),
                [request],
            )
        RoundQueue.invalidate_compiled(state["queue"])
        # Queued work for nets this improvement connected is done.
        plan = _replan(
            run.snapshot,
            run.policy,
            state["reference"],
            workers=run.workers,
            single=state.get("single_net_search", False),
        )
        state["queue"] = [
            batch
            for batch in state["queue"]
            if set(plan["island_counts"]).intersection(RoundQueue.targets(batch))
        ]
        state["island_counts"] = plan["island_counts"]
        state["attempted_board_sha256"] = sha(run.snapshot.board_path)

    def record(self) -> None:
        """Save the round: counters, last result, sweep replanning, checkpoint and
        progress."""
        run = self.run
        state = run.state
        state["attempted_searches"] = sorted(run.attempted)
        state["attempted_rounds"] += 1
        run.rounds += 1
        state["last_result"] = {
            "nets": self.result.get("nets", self.names)
            if self.accepted
            else self.names,
            "outcome": "accepted_preview" if self.accepted else self.reason,
            "attempts": summarize_attempts(self.result),
        }
        if not state["queue"]:
            if state["best_preview_opens"] < state["sweep_start_opens"]:
                state["failed_sweeps"] = 0
            else:
                state["failed_sweeps"] += 1
            if state["failed_sweeps"] < 3:
                plan = _replan(
                    run.snapshot,
                    run.policy,
                    state["reference"],
                    workers=run.workers,
                    single=state.get("single_net_search", False)
                    or state["failed_sweeps"] > 0,
                    offset=state["failed_sweeps"],
                )
                state["queue"] = plan["rounds"]
                state["blocked_nets"] = plan["blocked_nets"]
                state["island_counts"] = plan["island_counts"]
                state["sweep_start_opens"] = state["best_preview_opens"]
                run.target_attempts.clear()
        state["checkpoint"] = encode_checkpoint(
            run.snapshot.board_path.read_bytes(),
            run.inputs,
            run.snapshot.tool_hashes,
            run.policy_hash,
        )
        state["elapsed_seconds"] += round(time.monotonic() - run.start, 3)
        run.start = time.monotonic()
        state["status"] = "running"
        _save(run.state_path, state)
        if self.accepted or time.monotonic() - run.last_progress_update >= 60:
            _progress_elapsed(state, sha(run.snapshot.board_path))
            _publish_progress(run.project, run.inputs, state, run.progress)
            run.last_progress_update = time.monotonic()
        if run.progress:
            run.progress(
                {
                    "event": "round",
                    "run_id": run.run_id,
                    "result": state["last_result"],
                    "remaining_rounds": len(state["queue"]),
                    "round_seconds": time.monotonic() - self.started,
                    "preview_opens": state["best_preview_opens"],
                    "saved_opens": state["saved_board_opens"],
                }
            )
