"""Search disjoint batches on one snapshot and validate their combined proposal."""

from __future__ import annotations

from core.project import native_path

import concurrent.futures
import multiprocessing
from dataclasses import replace
from pathlib import Path

from core.kicad_campaign import CampaignPlanner
from core.pcb_io import quiet_duplicate_image_handlers
from pcb.net_routing.native import Snapshot, load_board, sha
from pcb.net_routing.checkpoint import CheckpointExhausted
from pcb.net_routing.protection import ProtectedState
from pcb.net_routing.recovery import search_key
from pcb.net_routing.scheduler import overlaps
from pcb.net_routing.workflow import configurations, plan_nets
from pcb.route_validation import CandidateRejected, RouteContract, validate_routes
from pcb.route_optimizer import Point
from core.compat import strict_zip


@quiet_duplicate_image_handlers()
def _search(args: tuple[str, dict]) -> dict:
    project, request = args
    try:
        result = plan_nets(request, Path(project), validate=False, include_changes=True)
    except CheckpointExhausted as error:
        result = {"status": "checkpoint_exhausted", "reason": str(error), "search": {}}
    return {**result, "request_fingerprint": search_key(request)}


def _check_requests(requests: list[dict]) -> None:
    if not isinstance(requests, list) or not requests:
        raise ValueError("A routing round needs at least one batch")
    windows = []
    names = set()
    for request in requests:
        _, nets, _ = configurations(request)
        if names.intersection(nets):
            raise ValueError("Parallel batches must contain distinct nets")
        names.update(nets)
        shared = request.get("shared", {})
        window = shared.get("bounds_mm")
        if not isinstance(window, list):
            raise ValueError("Each parallel batch needs a bounded search window")
        window = tuple(window)
        # overlaps validates the complete window, including finite coordinates.
        if not overlaps(window, window):
            raise ValueError("Invalid batch window")
        for settings in request["nets"]:
            if "bounds_mm" in settings and settings["bounds_mm"] != list(window):
                raise ValueError("A net cannot escape its parallel batch window")
        if any(overlaps(window, existing) for existing in windows):
            raise ValueError("Parallel batch windows overlap")
        windows.append(window)


def preview_round(project: Path, requests: list[dict], *, workers: int = 2, executor=None,
                  precomputed: list[dict] | None = None) -> dict:
    """Return a native-validated preview; never write the saved design."""
    _check_requests(requests)
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    project = project.resolve()
    source = native_path(project, ".kicad_pcb")
    schematic = native_path(project, ".kicad_sch")
    if not schematic.is_file():
        raise ValueError("A saved schematic is required to protect the design")
    with Snapshot(source) as snapshot:
        schematic_bytes = (snapshot.directory / schematic.name).read_bytes()
        protected = ProtectedState.capture(snapshot.original, schematic_bytes)
        arguments = [(str(snapshot.directory), request) for request in requests]
        if precomputed is not None:
            if not isinstance(precomputed, list) or len(precomputed) != len(requests):
                raise ValueError("Precomputed searches must match the requested batches")
            for request, item in strict_zip(requests, precomputed):
                if not isinstance(item, dict):
                    raise ValueError("A precomputed search must be an object")
                if item.get("source_sha256") != snapshot.fingerprints[source]:
                    raise RuntimeError("Precomputed search source differs from the current snapshot")
                if item.get("planner_sha256") != snapshot.tool_hashes:
                    raise RuntimeError("Precomputed search tools changed")
                if item.get("request_fingerprint") != search_key(request):
                    raise RuntimeError("Precomputed search request differs from the current request")
            results = list(precomputed)
        elif workers == 1 or len(arguments) == 1:
            results = [_search(argument) for argument in arguments]
        elif executor is not None:
            results = list(executor.map(_search, arguments))
        else:
            context = multiprocessing.get_context("spawn")
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=min(workers, len(arguments)), mp_context=context
            ) as temporary_executor:
                results = list(temporary_executor.map(_search, arguments))
        snapshot.check()
        successful = [item for item in results if item.get("status") == "geometric_candidate_only"]
        failed = [item for item in results if item.get("status") not in {"geometric_candidate_only", "already_connected"}]
        if not successful:
            return {"status": "no_candidate_found", "source_sha256": sha(source),
                    "publication_enabled": False, "attempts": results,
                    "reason": "No sampled candidate; feasibility remains unknown."}
        names = tuple(name for item in successful for name in item["order"])
        changes = [change for item in successful for change in item["unvalidated_campaign_changes"]]
        original_board = load_board(snapshot.board_path)
        scopes = {item["scope"] for item in successful}
        if len(scopes) != 1:
            raise ValueError("A combined batch cannot mix checkpoint and whole-net validation")
        scope = next(iter(scopes))
        route_contracts = [
            RouteContract(c["net"], Point(*c["start"]), c["start_layer"],
                          Point(*c["end"]), c["end_layer"])
            for item in successful for c in item["route_contracts"]
        ]
        if {c.net for c in route_contracts} != set(names):
            raise RuntimeError("A candidate is missing its endpoint contracts")
        track_nets = {item.m_Uuid.AsString(): item.GetNetname() for item in original_board.GetTracks()}
        for change in changes:
            if change.get("op") != "remove":
                continue
            target = change.get("target", {})
            net = track_nets.get(target.get("uuid"))
            index = next((i for i, contract in enumerate(route_contracts) if contract.net == net), None)
            if index is None:
                raise RuntimeError("A removed track has no matching net contract")
            contract = route_contracts[index]
            route_contracts[index] = replace(contract, removals=(*contract.removals, target))
        try:
            evidence = validate_routes(snapshot.board_path, sha(snapshot.board_path), changes,
                                       route_contracts, require_full_nets=scope == "whole_net",
                                       bottom_footprint_moves=tuple(sorted({
                                           reference for item in successful for reference in item.get("placement", {})
                                       })))
        except CandidateRejected as error:
            snapshot.check()
            return {"status": "rejected", "source_sha256": sha(source),
                    "reason": str(error), "publication_enabled": False,
                    "attempts": [{**item, "rejected_candidates": [
                        *item.get("rejected_candidates", []), {"reason": str(error)}
                    ]} for item in results]}
        if evidence["status"] != "no_new_drc_violations":
            return {"status": "rejected", "source_sha256": sha(source), "validation": evidence,
                    "publication_enabled": False, "attempts": results}
        changes.extend(evidence.pop("_zone_refill_changes", []))
        candidate = CampaignPlanner(snapshot.directory).plan("pcb", changes)
        if candidate.result_sha256 != evidence["candidate_sha256"]:
            raise RuntimeError("Combined candidate differs from the native validation")
        differences = protected.differences(candidate.after, schematic_bytes)
        if differences:
            raise RuntimeError(f"Candidate changes protected items: {', '.join(differences)}")
        snapshot.check()
        return {
            "status": "validated_preview",
            "scope": scope,
            "source_sha256": snapshot.fingerprints[source],
            "candidate_sha256": candidate.result_sha256,
            "nets": list(names),
            "changes": changes,
            "validation": evidence,
            "electrical_status": "not_assessed",
            "reference_copper_status": "not_assessed",
            "publication_enabled": False,
            "attempted_batches": len(results),
            "failed_batches": len(failed),
            "attempts": [
                {key: item[key] for key in ("status", "reason", "search", "checkpoints", "rejected_candidates",
                                          "routing_cost", "rework_cost", "cost") if key in item}
                for item in results
            ],
        }
