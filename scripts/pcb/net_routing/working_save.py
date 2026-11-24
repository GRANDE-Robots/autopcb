"""Safely save an independently checked routing work-in-progress preview."""

from __future__ import annotations

from core.project import native_path

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import sys
from pathlib import Path

from core.paths import asset_root, config_path, data_path, results_path, stored_result
from core.pcb_io import BoardSaveGuard, quiet_duplicate_image_handlers
from pcb.checkpoints import read_config
from pcb.freeroute_delta import read_copper
from pcb.net_routing.controller import (
    decode_checkpoint,
    encode_checkpoint,
    rebind_exact_saved_checkpoint,
)
from pcb.net_routing.external import (
    qualifies_corner_cleanup,
    qualifies_via_reduction,
    qualifies_warning_reduction,
    removes_only_copper,
)
from pcb.net_routing.isolation import run_native
from pcb.net_routing.native import Snapshot, sha
from pcb.net_routing.prepare import prepare_project, verify_parity_copper_unchanged
from pcb.net_routing.protection import (
    ProtectedState,
    protected_footprint_changes,
    unqualified_working_nets,
)
from pcb.route_validation import stable_connection_items
from pcb.net_routing.runner import (
    _check_sources,
    _exclusive,
    _run_id,
    _source_hashes,
    _save,
    _verify_cumulative_reference_native,
    _verify_resume_connectivity,
    verify_resume_reports,
)


def working_improvement(
    before: dict,
    after: dict,
    before_copper: list,
    after_copper: list,
    provenance: dict,
    *,
    rounding_proof: dict | None = None,
    staircase_proof: dict | None = None,
) -> str:
    """Validate the narrow improvement types allowed for an unfinished board."""
    old_opens, new_opens = (
        before["drc"]["open_connections"],
        after["drc"]["open_connections"],
    )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (old_opens, new_opens)
    ):
        raise ValueError("Invalid native open-connection counts")
    if new_opens < old_opens:
        return "fewer opens"
    if new_opens > old_opens:
        raise RuntimeError("The preview worsens saved-board connectivity")
    if not isinstance(provenance, dict):
        raise RuntimeError("An equal-open preview needs recorded cleanup provenance")
    engine, objective = provenance.get("engine"), provenance.get("objective")
    if (
        engine == "native-rounding-repair"
        and objective == "repair-rounding"
        and isinstance(rounding_proof, dict)
        and isinstance(rounding_proof.get("corrected"), int)
        and not isinstance(rounding_proof.get("corrected"), bool)
        and rounding_proof["corrected"] > 0
    ):
        return "fewer off-angle tracks"
    if (
        engine == "native-staircase-repair"
        and objective == "repair-staircase"
        and isinstance(staircase_proof, dict)
        and isinstance(staircase_proof.get("corrected"), int)
        and not isinstance(staircase_proof.get("corrected"), bool)
        and staircase_proof["corrected"] > 0
    ):
        return "fewer staircases"
    if (
        engine == "native-via-prune"
        and objective == "minimize-vias"
        and removes_only_copper(before_copper, after_copper)
        and qualifies_via_reduction(
            old_opens,
            new_opens,
            before["geometry"],
            after["geometry"],
            before_copper=before_copper,
            after_copper=after_copper,
        )
    ):
        return "fewer vias"
    if (
        engine == "native-dangling-prune"
        and objective == "reduce-warnings"
        and qualifies_warning_reduction(
            old_opens,
            new_opens,
            before["drc"],
            after["drc"],
            before["geometry"],
            after["geometry"],
            before_copper,
            after_copper,
        )
    ):
        return "fewer dangling warnings"
    if (
        engine == "native-dangling-chamfer"
        and objective == "reduce-warnings"
        and qualifies_corner_cleanup(
            old_opens,
            new_opens,
            before["drc"],
            after["drc"],
            before["geometry"],
            after["geometry"],
            before_copper,
            after_copper,
        )
    ):
        return "fewer dangling warnings"
    raise RuntimeError("An equal-open preview is not a verified cleanup")


def _read_copper_pair(before: Path, after: Path) -> tuple[list, list]:
    return read_copper(before), read_copper(after)


def verify_working_net_copper(
    board,
    before: list,
    after: list,
    excluded: tuple[str, ...],
    after_board=None,
    *,
    allow_isolated_removals: bool = False,
    critical_nets: frozenset[str] = frozenset(),
) -> None:
    """Reject unqualified changes except independently proven isolated debris."""
    if not isinstance(allow_isolated_removals, bool) or not isinstance(
        critical_nets, frozenset
    ):
        raise ValueError("Invalid isolated-removal permission")
    protected = unqualified_working_nets(board, excluded)
    if after_board is not None:
        protected |= unqualified_working_nets(after_board, excluded)
    old, new = Counter(before), Counter(after)
    removed, added = old - new, new - old
    changed = {item.net for item in removed + added} & protected
    if changed:
        if allow_isolated_removals:
            from pcb.net_routing.isolated_copper import (
                verify_isolated_protected_removals,
            )

            if not critical_nets:
                raise RuntimeError("High-speed net classification is unavailable")
            verify_isolated_protected_removals(
                board, removed, added, protected, set(excluded) | set(critical_nets)
            )
            return
        raise RuntimeError(
            "Working save requires electrical qualification for changed nets: "
            + ", ".join(sorted(changed))
        )


def _write_board(path: str, content: bytes) -> bool:
    Path(path).write_bytes(content)
    return True


def _saved_state(
    state: dict,
    board: bytes,
    inputs: dict,
    tools: dict,
    policy_hash: str,
    checked: dict,
    policy: dict | None = None,
    *,
    allow_rule_changes: bool = False,
) -> dict:
    result = dict(state)
    result["checkpoint"] = rebind_exact_saved_checkpoint(
        state["checkpoint"],
        board,
        inputs,
        tools,
        policy_hash,
        allow_rule_changes=allow_rule_changes,
    )
    opens = checked["drc"]["open_connections"]
    if opens != state["best_preview_opens"]:
        raise RuntimeError("Saved-preview adoption has a different native open count")
    result.update(
        saved_board_opens=opens,
        best_preview_opens=opens,
        failed_sweeps=0,
        sweep_start_opens=opens,
        attempted_board_sha256=result["checkpoint"]["candidate_sha256"],
        last_saved_utc=datetime.now(timezone.utc).isoformat(),
    )
    result.pop("fast_retry", None)
    if policy is not None:
        from pcb.net_routing.controller import make_work_plan

        plan = make_work_plan(
            checked,
            policy,
            workers=2,
            max_batch_nets=1 if state.get("single_net_search") else 3,
        )
        result.update(
            queue=plan["rounds"],
            island_counts=plan["island_counts"],
            blocked_nets=plan["blocked_nets"],
            reference=checked["reference_copper"],
        )
        result.pop("attempted_searches", None)
    return result


def adopt_saved(
    project: Path, run_id: str, *, progress=print, allow_rule_changes: bool = False
) -> dict:
    """Recheck and rebind a current checkpoint without writing a design file."""
    project = project.resolve()
    assets = asset_root(project)
    run_id = _run_id(run_id)
    path = results_path(project, "autoroute") / f"{run_id}.json"
    source_state = stored_result(project, "autoroute", f"{run_id}.json")
    policy_path = config_path(project, "autopcb.json")
    policy, policy_hash = read_config(policy_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with (
        _exclusive(path.parent / "active.lock"),
        Snapshot(native_path(project, ".kicad_pcb")) as snapshot,
    ):
        original = source_state.read_bytes()
        state = json.loads(original)
        inputs = _source_hashes(snapshot, project, assets)
        rebind_exact_saved_checkpoint(
            state["checkpoint"],
            snapshot.original,
            inputs,
            snapshot.tool_hashes,
            policy_hash,
            allow_rule_changes=allow_rule_changes,
        )
        if allow_rule_changes:
            progress(
                "Revalidating the exact saved PCB under the intentionally updated custom rules"
            )
        progress("Rechecking the exact saved preview before continuing the current run")
        checked = run_native(prepare_project, project)
        revised = _saved_state(
            state,
            snapshot.original,
            inputs,
            snapshot.tool_hashes,
            policy_hash,
            checked,
            policy,
            allow_rule_changes=allow_rule_changes,
        )
        if allow_rule_changes:
            revised["rule_update"] = {
                "native_rechecked": True,
                "manufacturing_approved": False,
                "rules": inputs[native_path(project, ".kicad_dru").name],
            }
        snapshot.check()
        if source_state.read_bytes() != original or (
            source_state != path and path.exists()
        ):
            raise RuntimeError(
                "The current preview changed during saved-source adoption"
            )
        _save(path, revised)
        count = checked["drc"]
        return {
            "status": "saved_preview_adopted",
            "saved_opens": count["open_connections"],
            "preview_opens": count["open_connections"],
            "saved_warnings": count["warnings"],
            "preview_warnings": count["warnings"],
        }


def save_working(
    project: Path, run_id: str, *, apply: bool = False, progress=print
) -> dict:
    """Recheck and atomically save a WIP only while all source/editor guards pass."""
    import pcbnew

    if not isinstance(apply, bool):
        raise ValueError("Apply must be true or false")
    project, run_id = project.resolve(), _run_id(run_id)
    assets = asset_root(project)
    board_path = native_path(project, ".kicad_pcb")
    policy_path = config_path(project, "autopcb.json")
    policy, policy_hash = read_config(policy_path)
    state_path = results_path(project, "autoroute") / f"{run_id}.json"
    source_state = stored_result(project, "autoroute", state_path.name)
    state_bytes = source_state.read_bytes()
    state = json.loads(state_bytes)
    if state.get("run_id") != run_id or state.get("schema") != 1:
        raise ValueError("Invalid preview run")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive(state_path.parent / "active.lock"):
        if (
            source_state != state_path
            and (source_state.parent / "active.lock").exists()
        ):
            raise RuntimeError("A legacy routing job is active for this board")
        with Snapshot(board_path) as snapshot:
            inputs = _source_hashes(snapshot, project, assets)
            candidate = decode_checkpoint(
                state["checkpoint"],
                inputs,
                snapshot.tool_hashes,
                policy_hash,
                allow_tool_changes=True,
            )
            schematic = (native_path(snapshot.directory, ".kicad_sch")).read_bytes()
            protected = ProtectedState.capture(snapshot.original, schematic)
            if protected.differences(candidate, schematic):
                raise RuntimeError("The preview changes protected design items")
            BoardSaveGuard(
                board_path, tuple(snapshot.fingerprints.items())
            ).verify_editor_unlocked()
            progress(
                "Checking the saved board and the selected work-in-progress preview"
            )
            baseline = run_native(prepare_project, project)
            prior = snapshot.directory / "working-save-prior.kicad_pcb"
            prior.write_bytes(snapshot.original)
            # Load under the project filename so KiCad also loads its net classes.
            original_board = pcbnew.LoadBoard(str(snapshot.board_path))
            if original_board is None:
                raise RuntimeError(
                    "KiCad could not inspect the saved board's net roles"
                )
            snapshot.board_path.write_bytes(candidate)
            checked = run_native(prepare_project, snapshot.directory)
            before, after = baseline["drc"], checked["drc"]
            verify_resume_reports(
                before,
                after,
                expected_opens=state["best_preview_opens"],
                stable_connection_items=stable_connection_items(
                    snapshot.original, candidate
                ),
            )
            before_copper, after_copper = run_native(
                _read_copper_pair, prior, snapshot.board_path
            )
            verify_parity_copper_unchanged(before, before_copper, after_copper)
            candidate_board = pcbnew.LoadBoard(str(snapshot.board_path))
            if candidate_board is None:
                raise RuntimeError(
                    "KiCad could not inspect the preview board's net roles"
                )
            from pcb.net_routing.via_guard import verify_no_new_via_pad_overlaps

            verify_no_new_via_pad_overlaps(original_board, candidate_board)
            provenance = state.get("external_provenance", {})
            allow_isolated = isinstance(provenance, dict) and (
                provenance.get("engine"),
                provenance.get("objective"),
            ) == ("native-dangling-prune", "reduce-warnings")
            verify_working_net_copper(
                original_board,
                before_copper,
                after_copper,
                tuple(policy.get("exclude_nets", ())),
                candidate_board,
                allow_isolated_removals=allow_isolated,
                critical_nets=frozenset(
                    baseline["reference_copper"]["high_speed_nets"]
                ),
            )
            changed_parts = protected_footprint_changes(
                original_board,
                candidate_board,
                snapshot.original,
                candidate,
                tuple(policy.get("exclude_nets", ())),
            )
            if changed_parts:
                raise RuntimeError(
                    "Working save requires electrical qualification for changed footprints: "
                    + ", ".join(changed_parts)
                )
            rounding_proof = staircase_proof = None
            if isinstance(provenance, dict) and (
                provenance.get("engine"),
                provenance.get("objective"),
            ) in {
                ("native-rounding-repair", "repair-rounding"),
                ("native-staircase-repair", "repair-staircase"),
            }:
                critical_names = set(baseline["reference_copper"]["high_speed_nets"])
                names = {
                    str(name) for name in original_board.GetNetsByName() if str(name)
                }
                eligible = (
                    names
                    - critical_names
                    - unqualified_working_nets(
                        original_board, tuple(policy.get("exclude_nets", ()))
                    )
                    - unqualified_working_nets(
                        candidate_board, tuple(policy.get("exclude_nets", ()))
                    )
                )
                if provenance["engine"] == "native-rounding-repair":
                    from pcb.net_routing.rounding import verify_candidate

                    rounding_proof = verify_candidate(
                        snapshot.original,
                        candidate,
                        original_board,
                        candidate_board,
                        eligible,
                    )
                else:
                    from pcb.net_routing.staircase_repair import verify_candidate

                    staircase_proof = verify_candidate(
                        snapshot.original,
                        candidate,
                        original_board,
                        candidate_board,
                        eligible,
                    )
            improvement = working_improvement(
                baseline,
                checked,
                before_copper,
                after_copper,
                provenance,
                rounding_proof=rounding_proof,
                staircase_proof=staircase_proof,
            )
            if (
                before["exclusions"] != after["exclusions"]
                or after["errors"] > before["errors"]
            ):
                raise RuntimeError(
                    "The preview changed exclusions or introduced DRC errors"
                )
            run_native(
                _verify_resume_connectivity, snapshot.directory, snapshot.original
            )
            critical = tuple(baseline["reference_copper"]["high_speed_nets"])
            if (
                not critical
                and baseline["reference_copper"].get("classification_complete")
                is not True
            ):
                raise RuntimeError("High-speed net classification is unavailable")
            if Counter(
                item for item in before_copper if item.net in critical
            ) != Counter(item for item in after_copper if item.net in critical):
                raise RuntimeError("The preview changed a deferred USB or Ethernet net")
            progress("Comparing sampled high-speed reference copper")
            reference = run_native(
                _verify_cumulative_reference_native,
                prior,
                snapshot.board_path,
                critical,
            )
            if reference["status"] == "degraded":
                raise RuntimeError("The preview degraded sampled reference copper")
            _check_sources(snapshot, project, assets, inputs, policy_path, policy_hash)
            if source_state.read_bytes() != state_bytes or (
                source_state != state_path and state_path.exists()
            ):
                raise RuntimeError("The preview changed while checking")
            dependencies = dict(snapshot.fingerprints)
            for name in ("POWER_LOAD_BUDGET.csv", "STACKUP_CURRENT.json"):
                path = data_path(project, name)
                if path.is_file():
                    dependencies[path.resolve()] = sha(path)
            dependencies[policy_path.resolve()] = policy_hash
            guard = BoardSaveGuard(board_path, tuple(dependencies.items()))
            guard.verify_unchanged()
            guard.verify_editor_unlocked()
            tools = dict(snapshot.tool_hashes)
            expected_source = snapshot.original
            if not apply:
                return {
                    "status": "dry_run_passed",
                    "improvement": improvement,
                    "saved_opens": before["open_connections"],
                    "preview_opens": after["open_connections"],
                    "saved_warnings": before["warnings"],
                    "preview_warnings": after["warnings"],
                }
            backup_dir = results_path(project, "autoroute") / "preimages"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / "working-save.json"
            journal = {
                "kind": "pre_working_pcb_save",
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "preview_run": run_id,
                "manufacturing_approved": False,
                "board_before": encode_checkpoint(
                    expected_source, inputs, tools, policy_hash
                ),
            }
            _save(backup, journal)
            copied = json.loads(backup.read_bytes())["board_before"]
            if decode_checkpoint(copied, inputs, tools, policy_hash) != expected_source:
                raise RuntimeError("The recovery checkpoint did not verify")
            _check_sources(snapshot, project, assets, inputs, policy_path, policy_hash)
            if source_state.read_bytes() != state_bytes or (
                source_state != state_path and state_path.exists()
            ):
                raise RuntimeError("The preview changed before saving")
        if Snapshot.tools() != tools:
            raise RuntimeError("Routing tools changed before saving")
        guard.save(candidate, board_path, _write_board, pcbnew.LoadBoard)
        if board_path.read_bytes() != candidate:
            raise RuntimeError("The saved PCB did not match the checked preview")
        for path, expected in guard.fingerprints:
            if path != board_path and sha(path) != expected:
                raise RuntimeError("A protected input changed during saving")
        rebound_inputs = dict(inputs)
        rebound_inputs[board_path.name] = sha(board_path)
        rebound = _saved_state(
            state, candidate, rebound_inputs, tools, policy_hash, checked, policy
        )
        if state_path.read_bytes() != state_bytes:
            raise RuntimeError("The preview changed after the checked board was saved")
        _save(state_path, rebound)
        return {
            "status": "saved_work_in_progress",
            "improvement": improvement,
            "saved_opens": before["open_connections"],
            "preview_opens": after["open_connections"],
            "saved_warnings": before["warnings"],
            "preview_warnings": after["warnings"],
            "preimage": str(backup),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--run", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply", action="store_true", help="save the checked work-in-progress PCB"
    )
    mode.add_argument(
        "--adopt-saved",
        action="store_true",
        help="continue only from the exact preview already saved to disk",
    )
    mode.add_argument(
        "--adopt-rules",
        action="store_true",
        help="recheck that exact saved preview after intentional custom-rule changes",
    )
    args = parser.parse_args(argv)
    try:
        with quiet_duplicate_image_handlers():

            def report(message):
                print(message, flush=True)

            result = (
                adopt_saved(
                    args.project,
                    args.run,
                    progress=report,
                    allow_rule_changes=args.adopt_rules,
                )
                if args.adopt_saved or args.adopt_rules
                else save_working(
                    args.project, args.run, apply=args.apply, progress=report
                )
            )
        print(
            f"Result: {result['status']}; {result['saved_opens']} -> {result['preview_opens']} opens; "
            f"{result['saved_warnings']} -> {result['preview_warnings']} warnings",
            flush=True,
        )
        if "preimage" in result:
            print("Recoverable pre-save checkpoint:", result["preimage"], flush=True)
        print(
            "Electrical, high-speed and manufacturing qualification remain incomplete.",
            flush=True,
        )
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Working save stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
