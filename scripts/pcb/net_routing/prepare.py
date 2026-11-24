"""Prepare a whole-board inventory from a stable, disposable KiCad snapshot."""

from __future__ import annotations

from core.project import ProjectProfile, native_path

import json
import re
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from core.toolchain import kicad_cli

import pcbnew

from core.paths import config_path, data_path
from pcb.net_routing.completion import VerificationEvidence, completion_issues
from pcb.net_routing.inventory import inventory_board
from pcb.net_routing.native import Snapshot, sha
from pcb.move_scope import MoveScope
from pcb.net_routing.protection import ProtectedState, unqualified_working_nets
from pcb.net_routing.reference import high_speed_nets, reference_report
from pcb.net_routing.scheduler import schedule, work_from_board
from pcb.net_routing.style import audit_style
from review.geometry_metrics import measure_board


def parity_blocked_nets(report: dict, pad_nets: dict[str, str]) -> tuple[str, ...]:
    """Identify both PCB and schematic nets for known pad-net conflicts."""
    findings = report.get("schematic_parity") if isinstance(report, dict) else None
    if not isinstance(findings, list) or not isinstance(pad_nets, dict):
        raise ValueError("Incomplete schematic parity evidence")
    blocked = set()
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("type") != "net_conflict":
            raise ValueError("Unsupported schematic parity finding")
        match = re.fullmatch(
            r"Pad net \((.*)\) doesn't match net given by schematic \((.*)\)",
            finding.get("description", ""),
        )
        items = finding.get("items")
        if (
            match is None
            or not isinstance(items, list)
            or len(items) != 1
            or not isinstance(items[0], dict)
            or not items[0].get("description", "").startswith("Pad ")
        ):
            raise ValueError("Unidentified schematic parity conflict")
        old, expected = match.groups()
        identifier = items[0].get("uuid")
        if not old or not expected or pad_nets.get(identifier) != old:
            raise ValueError("Cannot identify both nets in schematic parity conflict")
        blocked.update((old, expected))
    return tuple(sorted(blocked))


def verify_parity_copper_unchanged(baseline: dict, before, after) -> None:
    """Do not let an unrelated preview change any unresolved parity net."""
    if not baseline.get("parity_findings", 0):
        return
    blocked = baseline.get("parity_blocked_nets")
    if (
        not isinstance(blocked, list)
        or not blocked
        or any(not isinstance(name, str) or not name for name in blocked)
    ):
        raise RuntimeError("Unidentified schematic parity nets prevent routing")
    names = set(blocked)
    if Counter(item for item in before if item.net in names) != Counter(
        item for item in after if item.net in names
    ):
        raise RuntimeError("The candidate changed a schematic parity conflict net")


def summarize_drc(
    report: object,
    *,
    segment_identities: dict[str, str] | None = None,
    dangling_identities: dict[str, tuple[str, ...]] | None = None,
) -> dict:
    if not isinstance(report, dict):
        raise ValueError("KiCad DRC report is not an object")
    for name in (
        "violations",
        "unconnected_items",
        "schematic_parity",
        "included_severities",
        "ignored_checks",
    ):
        if not isinstance(report.get(name), list):
            raise ValueError(f"KiCad DRC did not report {name}")
    severities = set(report["included_severities"])
    if not {"error", "warning", "exclusion"} <= severities:
        raise ValueError("KiCad DRC did not include errors, warnings and exclusions")
    issues = report["violations"]
    if any(
        not isinstance(item, dict) or item.get("severity") not in severities
        for item in issues
    ):
        raise ValueError("KiCad DRC returned an invalid violation severity")
    ignored = []
    for item in report["ignored_checks"]:
        key = item.get("key") if isinstance(item, dict) else item
        if not isinstance(key, str) or not key:
            raise ValueError("KiCad DRC returned invalid ignored checks")
        ignored.append(key)
    errors = sum(item["severity"] == "error" for item in issues)
    warnings = sum(item["severity"] == "warning" for item in issues)
    exclusions = sum(item["severity"] == "exclusion" for item in issues)
    from pcb.route_validation import _dangling_summary, _key

    if any(not isinstance(item, dict) for item in report["schematic_parity"]):
        raise ValueError("KiCad DRC returned invalid schematic-parity records")
    return {
        "violation_keys": sorted(
            [
                list(_key(item, segment_identities)[:2])
                + [list(_key(item, segment_identities)[2])]
                for item in issues
            ]
        ),
        # Keep native descriptions and measurements as well as UUIDs. An error
        # with the same objects may still have become more severe.
        "error_details": sorted(
            json.dumps(item, sort_keys=True, allow_nan=False)
            for item in issues
            if item["severity"] == "error"
        ),
        "dangling_warnings": (
            _dangling_summary(report, dangling_identities)
            if dangling_identities is not None
            else None
        ),
        "parity_keys": sorted(
            [
                list(_key(item)[:2]) + [list(_key(item)[2])]
                for item in report["schematic_parity"]
            ]
        ),
        "open_connections": len(report["unconnected_items"]),
        "errors": errors,
        "warnings": warnings,
        "exclusions": exclusions,
        "parity_findings": len(report["schematic_parity"]),
        "ignored_checks": ignored,
        "complete": not (
            report["unconnected_items"]
            or errors
            or warnings
            or exclusions
            or report["schematic_parity"]
            or ignored
        ),
    }


def _review_current(project: Path, source_hashes: dict[str, str]) -> bool:
    from review.refresh_gallery import verify_gallery

    review = project / "deliverables" / "review"
    try:
        verify_gallery(review, source_hashes)
        standalone = review / "index_standalone.html"
        progress = review / "routing_progress.png"
        data = json.loads((review / "source.json").read_text(encoding="utf-8"))
        dependencies = data["dependency_sha256"]
        if (
            not isinstance(dependencies, dict)
            or not standalone.is_file()
            or not progress.is_file()
        ):
            return False
        for name, expected in dependencies.items():
            path = (project / name).resolve()
            if (
                not path.is_relative_to(project)
                or not path.is_file()
                or sha(path) != expected
            ):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False


def prepare_project(project: Path, *, progress=None) -> dict:
    """Read all board nets and native DRC from a stable saved-file copy."""

    def stage(message: str) -> None:
        if progress:
            progress({"event": "stage", "message": message})

    stage("1/6 | Reading saved files and recording protected items")
    project = project.resolve()
    source = native_path(project, ".kicad_pcb")
    schematic = native_path(project, ".kicad_sch")
    if not schematic.is_file():
        raise ValueError("A saved schematic is required for coverage and parity")
    extra_paths = tuple(
        path
        for path in (
            config_path(project, "autopcb.json"),
            data_path(project, "POWER_LOAD_BUDGET.csv"),
            data_path(project, "STACKUP_CURRENT.json"),
        )
        if path.is_file()
    )
    extra_hashes = {path: sha(path) for path in extra_paths}
    with Snapshot(source) as snapshot:
        schematic_bytes = (snapshot.directory / schematic.name).read_bytes()
        protected = ProtectedState.capture(snapshot.original, schematic_bytes)
        cli = kicad_cli()
        if not cli.is_file():
            raise RuntimeError("KiCad CLI is required beside the selected Python")
        output = snapshot.directory / "autoroute-drc.json"
        stage("2/6 | Running KiCad checks and refilling a temporary board")
        from pcb.route_validation import (
            _dangling_track_identities,
            _drc,
            _segment_identities,
        )

        report = _drc(
            cli, snapshot.board_path, output, save_refill=True, schematic_parity=True
        )
        differences = protected.differences(
            snapshot.board_path.read_bytes(), schematic_bytes
        )
        if differences:
            raise RuntimeError(
                f"Native refill changed protected items: {', '.join(differences)}"
            )
        board = pcbnew.LoadBoard(str(snapshot.board_path))
        if board is None:
            raise RuntimeError("KiCad could not reopen the refilled snapshot")
        drc = summarize_drc(
            report,
            segment_identities=_segment_identities(snapshot.board_path.read_bytes()),
            dangling_identities=_dangling_track_identities(board, report),
        )
        try:
            pads = {
                pad.m_Uuid.AsString(): str(pad.GetNetname()) for pad in board.GetPads()
            }
            drc["parity_blocked_nets"] = list(parity_blocked_nets(report, pads))
        except ValueError as error:
            drc["parity_blocked_nets"] = None
            drc["parity_blocker_error"] = str(error)
        stage("3/6 | Counting nets, pads and connected copper islands")
        inventory = inventory_board(
            board, schematic=schematic_bytes.decode("utf-8-sig")
        )
        geometry = measure_board(board)
        stage("4/6 | Checking routing geometry and USB / Ethernet reference copper")
        style = audit_style(board)
        from pcb.net_routing.via_guard import via_pad_overlaps

        via_pad = via_pad_overlaps(board)
        rules = native_path(snapshot.directory, ".kicad_pro")
        reference = {"status": "missing_project_rules", "electrically_verified": False}
        if rules.is_file():
            names = {net.name for net in inventory.nets}
            declared = ProjectProfile.declared_high_speed(project, names)
            critical = high_speed_nets(rules.read_bytes(), names) | (declared or set())
            reference = reference_report(board, critical)
            reference["classification_complete"] = declared is not None
        config = config_path(project, "autopcb.json")
        priorities = {}
        margin = 8.0
        routing_policy = {}
        if config.is_file():
            from pcb.checkpoints import read_config

            routing_policy, _ = read_config(config)
            priorities = routing_policy.get("priorities", {})
            margin = routing_policy.get("search_margin_mm") or margin
        fixed = ProtectedState.capture(
            snapshot.original,
            schematic_bytes,
            movable=MoveScope.from_config(routing_policy.get("placement")),
        )
        stage("5/6 | Scheduling unfinished nets and checking review freshness")
        work = work_from_board(board, inventory, priorities, margin_mm=margin)
        rounds = schedule(work.items)
        source_hashes = {
            path.name: snapshot.fingerprints[path.resolve()]
            for path in (source, schematic)
        }
        views_current = _review_current(project, source_hashes)
        evidence = VerificationEvidence(
            open_connections=drc["open_connections"],
            errors=drc["errors"],
            warnings=drc["warnings"],
            parity_errors=drc["parity_findings"],
            ignored_checks=tuple(drc["ignored_checks"])
            + (("DRC exclusions",) if drc["exclusions"] else ()),
            unverified_pads=len(inventory.unassigned_pad_ids)
            + len(inventory.unverified_no_connect_pad_ids),
            unassigned_copper=len(inventory.unassigned_copper_ids),
            coverage_complete=True,
            geometry_ok=style.passed,
            electrical_ok=None,
            reference_copper_ok=None,
            protected_unchanged=True,
            views_current=views_current,
        )
        stage("6/6 | Confirming saved inputs stayed unchanged")
        snapshot.check()
        if extra_hashes != {path: sha(path) for path in extra_paths}:
            raise RuntimeError(
                "Routing policy or design evidence changed during preparation"
            )
        return {
            "schema": 1,
            "kind": "autopcb_board_preparation",
            "status": "prepared_incomplete",
            "publication_enabled": False,
            "source_sha256": source_hashes,
            "input_sha256": {
                snapshot.input_name(path).as_posix(): digest
                for path, digest in snapshot.fingerprints.items()
            },
            "policy_sha256": {
                snapshot.input_name(path).as_posix(): digest
                for path, digest in extra_hashes.items()
            },
            "tool_sha256": snapshot.tool_hashes,
            "copper_layers": board.GetCopperLayerCount(),
            "board_bounds_mm": [
                board.GetBoardEdgesBoundingBox().GetX() / 1e6,
                board.GetBoardEdgesBoundingBox().GetY() / 1e6,
                board.GetBoardEdgesBoundingBox().GetRight() / 1e6,
                board.GetBoardEdgesBoundingBox().GetBottom() / 1e6,
            ],
            "protected_counts": {
                "fixed_footprints": len(fixed.fixed_footprints),
                "front_silkscreen": len(fixed.front_silkscreen),
                "outline": len(fixed.outline),
            },
            "nets": [asdict(net) for net in inventory.nets],
            "unqualified_nets": sorted(
                unqualified_working_nets(
                    board, tuple(routing_policy.get("exclude_nets", ()))
                )
            ),
            "work": asdict(work),
            "scheduled_rounds": [asdict(round_) for round_ in rounds],
            "unassigned_pad_ids": list(inventory.unassigned_pad_ids),
            "unverified_no_connect_pad_ids": list(
                inventory.unverified_no_connect_pad_ids
            ),
            "verified_no_connect_pad_ids": list(inventory.verified_no_connect_pad_ids),
            "unassigned_copper_ids": list(inventory.unassigned_copper_ids),
            "drc": drc,
            "geometry": asdict(geometry),
            "style": {**asdict(style), "passed": style.passed},
            "reference_copper": reference,
            "views_current": views_current,
            "completion_issues": [
                *completion_issues(evidence),
                *([f"unqualified via-in-pad: {len(via_pad)}"] if via_pad else []),
            ],
            "unqualified_via_in_pad": [asdict(item) for item in via_pad],
            "limitations": [
                "The style audit uses exact native track geometry but does not verify electrical limits, reference-copper continuity or physical fit.",
                "The complete board-wide routing and independent verification pipeline is not yet available.",
            ],
        }
