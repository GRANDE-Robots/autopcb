"""Independently verify and retain an external router's private candidate."""

from __future__ import annotations

from core.project import native_path

import argparse
import json
import math
import sys
import time
from collections import Counter
from copy import deepcopy
from itertools import permutations
from pathlib import Path

from core.toolchain import kicad_cli

from core.paths import asset_root, config_path, results_path
from core.pcb_io import quiet_duplicate_image_handlers
from pcb.checkpoints import read_config
from pcb.freeroute_delta import Trace, read_copper
from pcb.net_routing.controller import decode_checkpoint, encode_checkpoint
from pcb.net_routing.isolation import run_native
from pcb.net_routing.native import Snapshot, sha
from pcb.net_routing.prepare import prepare_project, verify_parity_copper_unchanged
from pcb.net_routing.protection import ProtectedState
from pcb.net_routing.runner import (
    _check_sources,
    _exclusive,
    _progress_sample,
    _publish_progress,
    _reference_repairs_native,
    _replan,
    _run_id,
    _save,
    _same_snapshot_inputs,
    _source_hashes,
    _verify_cumulative_reference_native,
    _verify_resume_connectivity,
    insert_reference_repairs,
    verify_resume_reports,
)
from pcb.route_validation import _drc, stable_connection_items
from core.toolchain import RouterCache


def layer_cost_evidence(engine: str) -> str:
    """Describe how a retained proposal's layer-cost setting was established."""
    evidence = {
        "Freerouting": "router_log", "fastroute": "dsn_and_pinned_source_only",
        "TraceMaker": "session_layer_costs_not_independently_verified",
        "krt": "native_router_equal_costs_configured",
        "native-zone-stitch": "not_applicable_native_stitch",
        "native-via-prune": "not_applicable_native_prune",
        "native-dangling-prune": "not_applicable_native_prune",
        "native-dangling-chamfer": "not_applicable_native_repair",
        "native-staircase-repair": "not_applicable_native_repair",
        "native-rounding-repair": "not_applicable_native_repair",
    }
    if engine not in evidence:
        raise ValueError("Unsupported external router provenance")
    return evidence[engine]


def router_stage_evidence(stages: tuple[tuple[str, str | None], ...], engine: str,
                          fingerprint: str | None) -> list[dict]:
    """Record every external engine used by one independently checked proposal."""
    if not isinstance(stages, tuple) or not 2 <= len(stages) <= 8:
        raise ValueError("Mixed-router provenance requires two to eight stages")
    records = []
    for stage in stages:
        if not isinstance(stage, tuple) or len(stage) != 2:
            raise ValueError("Invalid mixed-router stage")
        name, tool = stage
        if name not in {"Freerouting", "fastroute", "krt", "TraceMaker"}:
            raise ValueError("Unsupported mixed-router engine")
        if tool is not None and (not isinstance(tool, str) or len(tool) != 64
                                 or any(char not in "0123456789abcdef" for char in tool)):
            raise ValueError("Invalid mixed-router fingerprint")
        if name in {"krt", "TraceMaker"} and tool is None:
            raise ValueError(f"{name} stage requires its fingerprint")
        record = {"engine": name, "layer_cost_evidence": layer_cost_evidence(name)}
        if tool is not None:
            record["router_fingerprint"] = tool
        records.append(record)
    if stages[-1] != (engine, fingerprint):
        raise ValueError("Mixed-router provenance must end with the final router")
    return records


def accepted_state(old: dict, run_id: str, opens: int, plan: dict, reference: dict,
                   repair_blockers: list, elapsed: float, *, allow_equal: bool = False) -> dict:
    """Create a new resumable state without changing the preceding run."""
    if not isinstance(allow_equal, bool):
        raise ValueError("The equal-open optimization flag must be boolean")
    if (isinstance(opens, bool) or not isinstance(opens, int) or opens < 0
            or opens > old["best_preview_opens"] or (opens == old["best_preview_opens"] and not allow_equal)):
        raise ValueError("A retained external preview must show a strict improvement")
    if not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("Elapsed time must be nonnegative")
    state = deepcopy(old)
    state.update(
        run_id=_run_id(run_id), renewed_from=old["run_id"], status="paused",
        publication_enabled=False, best_preview_opens=opens,
        accepted_preview_batches=old["accepted_preview_batches"] + 1,
        elapsed_seconds=old["elapsed_seconds"] + elapsed,
        queue=plan["rounds"], blocked_nets=plan["blocked_nets"],
        island_counts=plan["island_counts"], failed_sweeps=0,
        sweep_start_opens=opens, reference=reference,
        reference_repair_blockers=repair_blockers, attempted_searches=[],
        target_attempts={},
        last_result={"nets": [], "outcome": "accepted_external_preview", "attempts": []},
    )
    state.pop("failure", None)
    return state


def qualifies_via_reduction(old_opens: int, new_opens: int, before: dict, after: dict, *,
                            before_copper: list | None = None, after_copper: list | None = None) -> bool:
    """Require fewer vias without new copper, opens, length or actual joints.

    A removed via can expose an already-existing two-track junction, increasing
    the display's degree-two count without creating a new copper joint. Permit
    that reclassification only when an exact copper-subset comparison proves
    nothing was added; native style and connectivity checks are still required.
    """
    try:
        old_vias, new_vias = before["via_count"], after["via_count"]
        old_joints, new_joints = before["degree_two_joints"], after["degree_two_joints"]
        old_length, new_length = before["track_length_mm"], after["track_length_mm"]
        counts = (old_opens, new_opens, old_vias, new_vias, old_joints, new_joints)
        lengths = (old_length, new_length)
        if (any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts)
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) or value < 0 for value in lengths)):
            return False
        joints_ok = (new_joints <= old_joints or
                     (before_copper is not None and after_copper is not None
                      and removes_only_copper(before_copper, after_copper)))
        return (new_opens <= old_opens and new_vias < old_vias and joints_ok
                and new_length <= old_length + 1e-9)
    except (KeyError, TypeError):
        return False


def removes_only_copper(before: list, after: list) -> bool:
    """Pruning may delete existing copper, but cannot introduce new geometry."""
    return not bool(Counter(after) - Counter(before))


def qualifies_warning_reduction(old_opens: int, new_opens: int, before_drc: dict, after_drc: dict,
                                before_geometry: dict, after_geometry: dict,
                                before_copper: list, after_copper: list) -> bool:
    """Accept only a shorter, trace-removal-only cleanup with fewer warnings.

    Independent native DRC, pad connectivity, protected-item and reference
    checks still determine whether a candidate can actually be retained.
    """
    try:
        counts = (old_opens, new_opens, before_drc["warnings"], after_drc["warnings"],
                  before_drc["errors"], after_drc["errors"], before_geometry["via_count"],
                  after_geometry["via_count"])
        lengths = (before_geometry["track_length_mm"], after_geometry["track_length_mm"])
        if (any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts)
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) or value < 0 for value in lengths)):
            return False
        removed = Counter(before_copper) - Counter(after_copper)
        return (new_opens <= old_opens and after_drc["warnings"] < before_drc["warnings"]
                and after_drc["errors"] <= before_drc["errors"]
                and after_geometry["via_count"] == before_geometry["via_count"]
                and after_geometry["track_length_mm"] <= before_geometry["track_length_mm"] + 1e-9
                and removes_only_copper(before_copper, after_copper) and bool(removed)
                and all(isinstance(item, Trace) for item in removed))
    except (KeyError, TypeError):
        return False


def qualifies_corner_cleanup(old_opens: int, new_opens: int, before_drc: dict, after_drc: dict,
                             before_geometry: dict, after_geometry: dict,
                             before_copper: list, after_copper: list) -> bool:
    """Independently prove a single dead-end removal with a local 45-degree corner.

    This is deliberately narrower than general rerouting. Native DRC, original
    connectivity, protected-item, style and reference checks remain mandatory.
    """
    from pcb.net_routing.geometry import intersection, on_segment
    from pcb.route_optimizer import Point, Segment

    try:
        counts = (old_opens, new_opens, before_drc["warnings"], after_drc["warnings"],
                  before_drc["errors"], after_drc["errors"], before_geometry["via_count"],
                  after_geometry["via_count"])
        lengths = (before_geometry["track_length_mm"], after_geometry["track_length_mm"])
        if (any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in counts)
                or any(isinstance(n, bool) or not isinstance(n, (int, float))
                       or not math.isfinite(n) or n < 0 for n in lengths)
                or new_opens > old_opens or after_drc["warnings"] >= before_drc["warnings"]
                or after_drc["errors"] > before_drc["errors"]
                or after_geometry["via_count"] != before_geometry["via_count"]
                or after_geometry["track_length_mm"] >= before_geometry["track_length_mm"]):
            return False
        removed = list((Counter(before_copper) - Counter(after_copper)).elements())
        added = list((Counter(after_copper) - Counter(before_copper)).elements())
        if (len(removed) != 3 or len(added) != 3
                or any(not isinstance(item, Trace) for item in (*removed, *added))
                or len({(item.net, item.layer, item.width) for item in (*removed, *added)}) != 1):
            return False
        def total(items):
            return sum(math.dist(item.start, item.end) for item in items)

        if total(added) >= total(removed):
            return False

        def shape(trace):
            return Segment(Point(*trace.start), Point(*trace.end), trace.layer)

        for diagonal, short_trunk, short_branch in permutations(added):
            dx = abs(diagonal.end[0] - diagonal.start[0])
            dy = abs(diagonal.end[1] - diagonal.start[1])
            if not 0 < dx == dy <= 250_000:
                continue
            for trunk, branch, spur in permutations(removed):
                a, b = shape(trunk), shape(branch)
                joint = intersection(a, b)
                if joint is None or joint in (a.start, a.end) or joint not in (b.start, b.end):
                    continue
                trunk_cut = set((short_trunk.start, short_trunk.end)) & set((diagonal.start, diagonal.end))
                branch_cut = set((short_branch.start, short_branch.end)) & set((diagonal.start, diagonal.end))
                if len(trunk_cut) != 1 or len(branch_cut) != 1 or trunk_cut == branch_cut:
                    continue
                cut_a, cut_b = Point(*next(iter(trunk_cut))), Point(*next(iter(branch_cut)))
                if not on_segment(cut_a, a) or not on_segment(cut_b, b):
                    continue
                remaining_a = set((short_trunk.start, short_trunk.end)) - trunk_cut
                remaining_b = set((short_branch.start, short_branch.end)) - branch_cut
                if (len(remaining_a) != 1 or len(remaining_b) != 1
                        or not remaining_a <= {trunk.start, trunk.end}
                        or not remaining_b <= {branch.start, branch.end}
                        or next(iter(remaining_b)) == (joint.x, joint.y)):
                    continue
                free = {trunk.start, trunk.end} - remaining_a
                if len(free) != 1 or not free & {spur.start, spur.end}:
                    continue
                va = (cut_a.x - joint.x, cut_a.y - joint.y)
                vb = (cut_b.x - joint.x, cut_b.y - joint.y)
                if (not ((va[0] == 0) != (va[1] == 0) and (vb[0] == 0) != (vb[1] == 0))
                        or va[0] * vb[0] + va[1] * vb[1] != 0
                        or max(abs(va[0]), abs(va[1])) != dx
                        or max(abs(vb[0]), abs(vb[1])) != dx):
                    continue
                return True
        return False
    except (KeyError, TypeError, ValueError):
        return False


def _compare_checked_board(snapshot: Snapshot, baseline: dict, candidate: dict,
                           expected_opens: int, original: bytes) -> None:
    """Compare independently measured checks and recompute connectivity/style."""
    identical = _same_snapshot_inputs(snapshot, original)
    stable = None if identical else stable_connection_items(original, snapshot.board_path.read_bytes())
    verify_resume_reports(
        baseline["drc"], candidate["drc"], expected_opens=expected_opens,
        identical_inputs=identical, stable_connection_items=stable,
    )
    run_native(_verify_resume_connectivity, snapshot.directory, original)
    snapshot.check()


def _saved_checks(project: Path, inputs: dict, tools: dict, policy_hash: str,
                  cache: dict | None) -> dict:
    """Reuse only a saved-board DRC measured with identical inputs and tools."""
    key = (str(project), tuple(sorted(inputs.items())), tuple(sorted(tools.items())), policy_hash)
    if cache is not None and cache.get("key") == key:
        return {"drc": deepcopy(cache["drc"])}
    result = run_native(prepare_project, project)
    if cache is not None:
        cache.clear()
        cache.update(key=key, drc=deepcopy(result["drc"]))
    return result


def retain_candidate(project: Path, previous_run: str, candidate_path: Path, *,
                     routing_seconds: float = 0.0, progress=None,
                     expected_previous: bytes | None = None,
                     engine: str = "Freerouting", saved_checks_cache: dict | None = None,
                     objective: str = "reduce-opens", router_fingerprint: str | None = None,
                     router_stages: tuple[tuple[str, str | None], ...] | None = None) -> dict:
    """Retain an independently checked improvement, never the live PCB."""
    started = time.monotonic()

    def stage(message: str) -> None:
        if progress:
            progress(message)

    if (isinstance(routing_seconds, bool) or not isinstance(routing_seconds, (int, float))
            or not math.isfinite(routing_seconds) or routing_seconds < 0):
        raise ValueError("Routing time must be a nonnegative number")
    layer_evidence = layer_cost_evidence(engine)
    if engine in {"krt", "TraceMaker"} and (
        not isinstance(router_fingerprint, str) or len(router_fingerprint) != 64
        or any(char not in "0123456789abcdef" for char in router_fingerprint)
    ):
        raise ValueError(f"{engine} provenance requires its tool fingerprint")
    stages = (router_stage_evidence(router_stages, engine, router_fingerprint)
              if router_stages is not None else None)
    if objective not in {"reduce-opens", "minimize-vias", "reduce-warnings", "repair-rounding",
                         "repair-staircase"}:
        raise ValueError("Unsupported preview objective")
    if (engine == "native-via-prune") != (objective == "minimize-vias"):
        raise ValueError("Via pruning needs its own optimization objective and provenance")
    if (engine in {"native-dangling-prune", "native-dangling-chamfer"}) != (objective == "reduce-warnings"):
        raise ValueError("Dangling-track pruning needs its own objective and provenance")
    if (engine == "native-rounding-repair") != (objective == "repair-rounding"):
        raise ValueError("Rounding repair needs its own objective and provenance")
    if (engine == "native-staircase-repair") != (objective == "repair-staircase"):
        raise ValueError("Staircase repair needs its own objective and provenance")
    project, candidate_path = project.resolve(), candidate_path.resolve()
    assets = asset_root(project)
    policy_path = config_path(project, "autopcb.json")
    policy, policy_hash = read_config(policy_path)
    output = results_path(project, "autoroute")
    output.mkdir(parents=True, exist_ok=True)
    previous_id = _run_id(previous_run)
    previous_path = output / f"{previous_id}.json"
    candidate = candidate_path.read_bytes()
    with _exclusive(output / "active.lock", wait_seconds=55, progress=stage), \
            Snapshot(native_path(project, ".kicad_pcb")) as snapshot:
        previous_bytes = previous_path.read_bytes()
        if expected_previous is not None and previous_bytes != expected_previous:
            raise RuntimeError("The source preview changed during external routing")
        old = json.loads(previous_bytes)
        if old.get("schema") != 1 or old.get("run_id") != previous_id:
            raise ValueError("Invalid source routing run")
        inputs = _source_hashes(snapshot, project, assets)
        retained = decode_checkpoint(old["checkpoint"], inputs, snapshot.tool_hashes,
                                     policy_hash, allow_tool_changes=True)
        schematic = (native_path(snapshot.directory, ".kicad_sch")).read_bytes()
        protected = ProtectedState.capture(snapshot.original, schematic)
        if protected.differences(retained, schematic) or protected.differences(candidate, schematic):
            raise RuntimeError("The external candidate changed protected design items")
        snapshot.board_path.write_bytes(retained)
        stage("Rechecking the previous private preview")
        saved_checks = _saved_checks(project, inputs, snapshot.tool_hashes, policy_hash, saved_checks_cache)
        baseline = run_native(prepare_project, snapshot.directory)
        _compare_checked_board(snapshot, saved_checks, baseline,
                               old["best_preview_opens"], snapshot.original)
        prior = snapshot.directory / "external-prior.kicad_pcb"
        prior.write_bytes(retained)
        critical = tuple(baseline["reference_copper"]["high_speed_nets"])
        if not critical:
            raise RuntimeError("High-speed net classification is unavailable")
        original_board = None
        if objective in {"repair-rounding", "repair-staircase"}:
            import pcbnew

            original_board = pcbnew.LoadBoard(str(snapshot.board_path))
            if original_board is None:
                raise RuntimeError("KiCad could not read the rounding baseline")
        snapshot.board_path.write_bytes(candidate)
        cli = kicad_cli()
        stage("Refilling and checking the new private candidate")
        _drc(cli, snapshot.board_path, snapshot.directory / "external-refill.json",
             save_refill=True, schematic_parity=True)
        if protected.differences(snapshot.board_path.read_bytes(), schematic):
            raise RuntimeError("Native refill changed protected design items")
        before_copper = read_copper(prior)
        after_copper = read_copper(snapshot.board_path)
        verify_parity_copper_unchanged(baseline["drc"], before_copper, after_copper)
        from pcb.net_routing.via_guard import verify_files as verify_via_pad_files

        run_native(verify_via_pad_files, prior, snapshot.board_path)
        before_critical = Counter(item for item in before_copper if item.net in critical)
        after_critical = Counter(item for item in after_copper if item.net in critical)
        if before_critical != after_critical:
            raise RuntimeError("External routing changed an unverified USB/Ethernet pair")
        verified = run_native(prepare_project, snapshot.directory)
        opens = verified["drc"]["open_connections"]
        verify_resume_reports(
            baseline["drc"], verified["drc"], expected_opens=opens,
            stable_connection_items=stable_connection_items(prior.read_bytes(), snapshot.board_path.read_bytes()),
        )
        rounding_proof = staircase_proof = None
        if objective in {"repair-rounding", "repair-staircase"}:
            from pcb.net_routing.protection import unqualified_working_nets

            revised_board = pcbnew.LoadBoard(str(snapshot.board_path))
            if revised_board is None:
                raise RuntimeError("KiCad could not inspect the style candidate")
            names = {str(name) for name in original_board.GetNetsByName() if str(name)}
            eligible = names - set(critical) - unqualified_working_nets(
                original_board, tuple(policy.get("exclude_nets", ()))
            ) - unqualified_working_nets(revised_board, tuple(policy.get("exclude_nets", ())))
            if objective == "repair-rounding":
                from pcb.net_routing.rounding import verify_candidate

                rounding_proof = verify_candidate(
                    retained, snapshot.board_path.read_bytes(), original_board, revised_board, eligible
                )
            else:
                from pcb.net_routing.staircase_repair import verify_candidate

                staircase_proof = verify_candidate(
                    retained, snapshot.board_path.read_bytes(), original_board, revised_board, eligible
                )
        if objective == "minimize-vias" and not removes_only_copper(before_copper, after_copper):
            raise RuntimeError("Via pruning introduced new copper")
        if objective == "minimize-vias" and not qualifies_via_reduction(
            old["best_preview_opens"], opens, baseline["geometry"], verified["geometry"],
            before_copper=before_copper, after_copper=after_copper,
        ):
            raise RuntimeError("Via optimization did not preserve opens and reduce vias without longer or new copper")
        warning_check = (qualifies_corner_cleanup if engine == "native-dangling-chamfer"
                         else qualifies_warning_reduction)
        if objective == "reduce-warnings" and not warning_check(
            old["best_preview_opens"], opens, baseline["drc"], verified["drc"],
            baseline["geometry"], verified["geometry"], before_copper, after_copper,
        ):
            raise RuntimeError("Dangling cleanup did not meet its geometry and warning checks")
        run_native(_verify_resume_connectivity, snapshot.directory, retained)
        reference = run_native(_verify_cumulative_reference_native, prior, snapshot.board_path, critical)
        if reference["status"] == "degraded":
            raise RuntimeError("External routing degraded sampled reference copper")
        stage("Comparing the candidate with the saved board")
        _compare_checked_board(snapshot, saved_checks, verified, opens, snapshot.original)
        single = old.get("single_net_search", False)
        if not isinstance(single, bool):
            raise ValueError("Invalid net-search batching mode in the source preview")
        plan = _replan(snapshot, policy, verified["reference_copper"], workers=2, single=single)
        repairs = run_native(_reference_repairs_native, snapshot.directory,
                             verified["reference_copper"], policy, 2)
        insert_reference_repairs(plan, repairs, policy)
        run_id = previous_id
        state = accepted_state(old, run_id, opens, plan, verified["reference_copper"],
                               repairs["blockers"], routing_seconds + time.monotonic() - started,
                               allow_equal=objective in {"minimize-vias", "reduce-warnings", "repair-rounding",
                                                         "repair-staircase"})
        state.pop("renewed_from", None)
        state["attempted_board_sha256"] = sha(snapshot.board_path)
        state["checkpoint"] = encode_checkpoint(snapshot.board_path.read_bytes(), inputs,
                                                snapshot.tool_hashes, policy_hash)
        state["external_provenance"] = {
            "engine": engine,
            "layer_cost_evidence": layer_evidence,
            "reference_comparison": reference["status"],
            "electrical_verification": "not complete",
            "objective": objective,
        }
        if router_fingerprint is not None:
            state["external_provenance"]["router_fingerprint"] = router_fingerprint
        if stages is not None:
            state["external_provenance"]["router_stages"] = stages
        if rounding_proof is not None:
            state["external_provenance"]["rounding_proof"] = rounding_proof
        if staircase_proof is not None:
            state["external_provenance"]["staircase_proof"] = staircase_proof
        _progress_sample(state, sha(snapshot.board_path), verified["drc"], verified["geometry"],
                         state["elapsed_seconds"])
        _check_sources(snapshot, project, assets, inputs, policy_path, policy_hash)
        if previous_path.read_bytes() != previous_bytes or candidate_path.read_bytes() != candidate:
            raise RuntimeError("The preview or candidate changed during verification")
        state_path = output / f"{run_id}.json"
        if state_path.read_bytes() != previous_bytes:
            raise RuntimeError("The current preview changed before checkpoint replacement")
        _save(state_path, state)
        try:
            _publish_progress(project, inputs, state)
        except (OSError, RuntimeError, ValueError):
            _save(state_path, state)
            raise
        return {"status": "private_preview_retained", "run_id": run_id,
                "saved_board_opens": state["saved_board_opens"], "preview_opens": opens,
                "errors": verified["drc"]["errors"], "warnings": verified["drc"]["warnings"],
                "reference_comparison": reference["status"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--run", required=True, help="source private preview run")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--routing-seconds", type=float, default=0)
    parser.add_argument("--engine", choices=("Freerouting", "fastroute", "krt", "TraceMaker", "native-zone-stitch", "native-via-prune",
                                             "native-dangling-prune", "native-dangling-chamfer",
                                             "native-rounding-repair", "native-staircase-repair"),
                        default="Freerouting")
    parser.add_argument("--router", type=Path, help="pinned krt directory or TraceMaker binary")
    parser.add_argument("--objective", choices=("reduce-opens", "minimize-vias", "reduce-warnings", "repair-rounding",
                                               "repair-staircase"),
                        default="reduce-opens")
    args = parser.parse_args(argv)
    try:
        if args.router is not None and args.engine not in {"krt", "TraceMaker"}:
            raise ValueError("--router is only used with --engine krt or TraceMaker")
        fingerprint = None
        if args.engine == "krt":
            from pcb.net_routing.krt_adapter import validate_installation

            root = args.router or RouterCache.krt()
            fingerprint = validate_installation(root)
        elif args.engine == "TraceMaker":
            if args.router is None or not args.router.is_file():
                raise ValueError("TraceMaker retention requires its router binary")
            fingerprint = sha(args.router)
        with quiet_duplicate_image_handlers():
            result = retain_candidate(args.project, args.run, args.candidate,
                                      routing_seconds=args.routing_seconds,
                                      engine=args.engine, objective=args.objective,
                                      router_fingerprint=fingerprint,
                                      progress=lambda message: print(message, flush=True))
        print(f"Private preview: {result['saved_board_opens']} saved / {result['preview_opens']} preview opens")
        print(f"Native errors: {result['errors']}; warnings: {result['warnings']}")
        print(f"Run: {result['run_id']}")
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"External preview rejected: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
