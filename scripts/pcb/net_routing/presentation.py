"""Human-facing AutoPCB summaries, separate from routing and verification."""

from __future__ import annotations

from collections import Counter, defaultdict

from core.console import Console, duration
from pcb.net_routing.recovery import STAGE_NAMES

_FINDING_NAMES = {
    "track_dangling": "Dangling tracks",
    "via_dangling": "Dangling vias",
    "lib_footprint_mismatch": "Footprint/library mismatches",
    "shorting_items": "Short circuits",
    "clearance": "Clearance violations",
}


def _number(value, unit: str = "") -> str | None:
    return f"{value:,.2f}{unit}" if isinstance(value, (float, int)) else None


def _names(console: Console, title: str, names) -> None:
    names = list(names)
    if names:
        console.line(title + ": " + ", ".join(map(str, names)), prefix="  ")


def show_pair_probe(console: Console, report: dict) -> None:
    console.heading("AutoPCB | Paired-signal diagnostic")
    if report["status"] == "no_candidate":
        console.line("No extractable paired-route proposal was found within this trial.")
    console.rows([
        ("Pairs examined", len(report["pairs"])),
        ("Saved / trial opens", f"{report['saved_opens']} / {report['candidate_opens']}"),
        ("Saved / trial pair opens", f"{report['saved_critical_opens']} / {report['candidate_critical_opens']}"),
        ("Saved / trial other errors", f"{report['saved_other_errors']} / {report['other_errors']}"),
        ("Saved / trial warnings", f"{report['saved_warnings']} / {report['warnings']}"),
        ("Trial additions", f"{report['added_traces']} tracks; {report['added_vias']} vias"),
        ("Non-octilinear tracks skipped", report["skipped_non_octilinear"]),
        ("New style findings", sum(report["new_style_issues"].values())),
        ("New via-in-pad overlaps", len(report.get("new_via_pad_overlaps", ()))),
        ("Saved / trial segments missing reference", f"{report['reference_before_missing']} / {report['reference_after_missing']}"),
        ("Unsupported reference geometry", report["reference_unsupported"]),
        ("Protected changes", len(report["protected_changes"])),
    ])
    if report["new_style_issues"]:
        console.line("Style findings: " + ", ".join(
            f"{name.replace('_', ' ')}: {count}" for name, count in report["new_style_issues"].items()
        ))
    if console.verbose:
        for pair in report["pairs"]:
            detail = f"{pair['positive']} / {pair['negative']}: {pair['gap_mm']:.3f} mm target"
            if "saved_opens" in pair:
                detail += (f"; opens {pair['saved_opens']} / {pair['trial_opens']}"
                           f"; incomplete reference segments {pair['trial_missing_reference_segments']}")
            console.line(detail, prefix="  ")
        obstacles = report.get("nearby_reference_obstacles", {})
        if obstacles:
            console.line("Nearby non-ground copper (conservative screen): " + ", ".join(
                f"{net}: {count}" for net, count in obstacles.items()
            ), prefix="  ")
    for item in report["limitations"]:
        console.line(item, prefix="  - ")


def show_preparation(console: Console, report: dict) -> None:
    drc = report["drc"]
    work = report.get("work", {}).get("items", [])
    geometry = report.get("geometry", {})
    reference = report.get("reference_copper", {})
    console.heading("AutoPCB | Saved board check")
    console.rows(
        [
            ("Open connections", drc["open_connections"]),
            ("Other DRC errors", drc["errors"]),
            ("DRC warnings", drc["warnings"]),
            (
                "Nets",
                f"{len(report.get('nets', []))} total; {len(work)} need connections",
            ),
            ("Copper layers", report.get("copper_layers")),
            ("Track length", _number(geometry.get("track_length_mm"), " mm")),
            ("Vias", geometry.get("via_count")),
            ("Track joints", geometry.get("degree_two_joints")),
            (
                "Review PNGs / PDFs",
                "Current" if report.get("views_current") else "Refresh needed",
            ),
            ("Source checks", "Passed; saved files were stable during inspection"),
            (
                "Board publication",
                "Disabled; routing and verification remain incomplete",
            ),
        ]
    )
    counts = Counter((item[0], item[1]) for item in drc.get("violation_keys", []))
    if counts:
        console.line("DRC findings by type:")
        console.rows(
            (_FINDING_NAMES.get(kind, kind.replace("_", " ")), f"{count} ({severity})")
            for (kind, severity), count in sorted(counts.items())
        )
    issues = report.get("completion_issues", [])
    if issues:
        console.line("Still required for completion:")
        for issue in dict.fromkeys(issues):
            console.line(issue, prefix="  - ")
    if console.verbose:
        style = report.get("style", {})
        console.line("Routing geometry findings:")
        console.rows(
            (name.replace("_", " "), len(value))
            for name, value in style.items()
            if isinstance(value, (list, tuple))
        )
        console.rows(
            [
                ("Reference corridors", reference.get("reservation_count")),
                (
                    "Reference assessment",
                    reference.get("status", "not verified").replace("_", " "),
                ),
                ("Schematic agreement issues", drc.get("parity_findings")),
                ("DRC exclusions", drc.get("exclusions")),
                ("Ignored DRC checks", len(drc.get("ignored_checks", []))),
            ]
        )
        _names(console, "Nets needing connections", (item["name"] for item in work))
        _names(console, "USB / Ethernet nets", reference.get("high_speed_nets", []))
    console.line("Next: preview routing, or refresh review files if marked above.")


_LIMIT_LABELS = {
    "seconds": "Batch time limit reached",
    "order_seconds": "Order time slice reached",
    "net_seconds": "Net time slice reached",
    "tree_seconds": "Starting-island time slice reached",
    "endpoint_seconds": "Endpoint-search time slice reached",
    "path_calls": "Path-search count limit reached",
    "net_path_calls": "Net path-search count limit reached",
    "tree_path_calls": "Starting-island search slice reached",
    "off_grid_search": "Endpoint alternatives were not fully searched",
    "path_expansions": "Path-search expansion limit reached",
}
_FAILURE_LABELS = {
    "no_path_in_grid": "No route found in tested grid",
    "path_search_unknown": "Path search ended without an answer",
    "combined_geometry": "Candidate failed routing geometry",
    "electrical_screen": "Candidate failed electrical screening",
    "restore_seed": "Original copper did not qualify as a complete warm route",
}


def _attempts(console: Console, attempts: list[dict]) -> None:
    reasons = Counter()
    for attempt in attempts:
        if attempt.get("reason"):
            reasons[str(attempt["reason"])] += 1
        for reason in attempt.get("limits_hit", []):
            reasons[_LIMIT_LABELS.get(reason, f"Search limit: {reason}")] += 1
        for reason, count in attempt.get("failures", {}).items():
            reasons[_FAILURE_LABELS.get(reason, str(reason))] += count if isinstance(count, int) else 1
    for reason, count in reasons.most_common(None if console.verbose else 3):
        console.line(f"{reason} ({count})", prefix="  - ")
    if len(reasons) > 3 and not console.verbose:
        console.line(
            f"{len(reasons) - 3} more diagnostic categories; use --verbose.",
            prefix="  ",
        )
    if console.verbose:
        for attempt in attempts:
            zones = attempt.get("zone_count", attempt.get("ignored_zone_count", 0))
            if isinstance(zones, int) and zones > 0:
                message = (f"{zones} zones refilled for search; final checks refill again."
                           if attempt.get("zone_connectivity") == "refilled"
                           else f"{zones} zone fills are not credited for connectivity during search.")
                console.line(message, prefix="  - ")
            for order in attempt.get("orders", []):
                names = order.get("order", [])
                reached = order.get("nets_reached", 0)
                if names and isinstance(reached, int):
                    state = ("stopped at " + names[reached]
                             if 0 <= reached < len(names) else "completed")
                    console.line(f"Order: {' -> '.join(names)} ({state})", prefix="  ")
            if isinstance(attempt.get("rework_cost"), (int, float)):
                console.line(f"Route cost: {attempt['routing_cost']:.2f}; "
                             f"existing-copper change cost: {attempt['rework_cost']:.2f}", prefix="  ")
            for checkpoint in attempt.get("checkpoints", []):
                stage = checkpoint.get("stage", 0)
                console.line(
                    f"{checkpoint['net']}: {STAGE_NAMES[stage]}, choice {checkpoint['index'] + 1}"
                    f"/{checkpoint['count']}, {len(checkpoint['islands'])} copper groups, "
                    f"grid {checkpoint['step_mm']:g} mm", prefix="  ")
            for category, details in attempt.get("failure_details", {}).items():
                for detail in details:
                    console.line(f"{_FAILURE_LABELS.get(category, category)}: {detail}", prefix="    ")
            for rejection in attempt.get("rejected_candidates", []):
                if rejection.get("reason"):
                    console.line(rejection["reason"], prefix="    Candidate rejected: ")
        calls = [item.get("path_calls") for item in attempts]
        seconds = [item.get("elapsed_seconds") for item in attempts]
        calls_recorded = bool(calls) and all(isinstance(value, int) for value in calls)
        time_recorded = bool(seconds) and all(isinstance(value, (float, int)) for value in seconds)
        console.rows(
            [
                ("Search calls", sum(calls) if calls_recorded else "Not recorded"),
                (
                    "Worker search time",
                    f"{sum(seconds):,.2f} s" if time_recorded else "Not recorded",
                ),
            ]
        )


def show_run(console: Console, report: dict) -> None:
    console.heading("AutoPCB | Routing preview " + report["status"].replace("_", " "))
    console.rows(
        [
            ("Saved board opens", report.get("saved_board_opens")),
            ("Best preview opens", report.get("best_preview_opens")),
            ("Accepted preview batches", report.get("accepted_preview_batches")),
            ("Attempted rounds", report.get("attempted_rounds")),
            ("Elapsed search / checking", duration(report.get("elapsed_seconds", 0))),
            ("Publication", "None; this run keeps candidates in a private preview"),
        ]
    )
    if report.get("failure"):
        console.line(report["failure"], prefix="  Failure: ")
    last = report.get("last_result")
    if last:
        console.line("Last result: " + last["outcome"].replace("_", " "))
        _names(console, "Nets", last.get("nets", []))
        _attempts(console, last.get("attempts", []))
    grouped = defaultdict(list)
    for net, reason in report.get("blocked_nets", {}).items():
        grouped[reason].append(net)
    for reason, names in grouped.items():
        console.line(f"{len(names)} deferred nets: {reason}", prefix="  - ")
        if console.verbose:
            _names(console, "Nets", names)
    repair_blockers = report.get("reference_repair_blockers", [])
    if repair_blockers:
        console.line(
            f"{len(repair_blockers)} reference-copper repair blockers remain.",
            prefix="  - ",
        )
        if console.verbose:
            reasons = Counter(
                reason.partition(": ")[2] if reason.startswith("Reference conflict ") and ": " in reason else reason
                for reason in repair_blockers
            )
            for reason, count in reasons.most_common():
                console.line(f"{reason} ({count})", prefix="    ")
    blockers = report.get("blockers", report.get("baseline_blockers", []))
    for reason in dict.fromkeys(blockers):
        console.line(reason, prefix="  - ")
    if report.get("run_id"):
        limits = report.get("resource_limits") or {}
        budget = limits.get("job_commit_limit_bytes", 0) / 1024**3
        memory_option = f" --memory-gib {budget:g}" if limits.get("budget_mode") == "explicit" else ""
        action = report.get("resume_action", "resume")
        if action == "unknown":
            console.line("Continuation cannot be determined; check the saved inputs and tools.")
        elif action == "restart":
            console.line("Saved inputs or policy changed; start a new preview:")
            console.command(f"python run.py pcb autoroute run{memory_option}")
        else:
            console.line("Resume this preview:")
            console.command(f"python run.py pcb autoroute run --{action} {report['run_id']}{memory_option}")


class RoutingDisplay:
    """Translate structured worker events without leaking large internal records."""

    def __init__(self, console: Console) -> None:
        self.console = console

    def __call__(self, record: dict) -> None:
        console = self.console
        if console.json_mode or record.get("event") in {"stage", "heartbeat"}:
            console.event(record)
            return
        event = record.get("event")
        if event == "resources":
            console.stage("Routing resources ready")
            console.rows(
                [
                    ("Search workers", record.get("workers")),
                    (
                        "Memory ceiling",
                        _number(record["job_commit_limit_bytes"] / 1024**3, " GiB")
                        if "job_commit_limit_bytes" in record
                        else None,
                    ),
                    ("Budget selection", record.get("budget_mode")),
                ]
            )
        elif event == "inventory":
            console.stage("Saved board inventory ready")
            console.rows(
                [
                    ("Saved board opens", record["open_connections"]),
                    ("Other DRC errors", record["errors"]),
                    ("DRC warnings", record["warnings"]),
                    ("Nets needing connections", record["work_nets"]),
                    ("Copper layers", record["copper_layers"]),
                ]
            )
        elif event == "search":
            round_label = f"Round {record['round']}" if "round" in record else "Batch"
            console.stage(
                round_label + " | " + STAGE_NAMES[record.get("recovery_stage", 0)]
                + " | Searching and checking: " + ", ".join(record["nets"])
            )
            console.line(
                f"Opens: {record['saved_opens']} saved / {record['preview_opens']} preview",
                prefix="  ",
            )
            if record.get("alternative_count", 0) > 1:
                console.line(
                    f"{record['alternative_count']} independent searches on separate temporary boards",
                    prefix="  ",
                )
        elif event == "round":
            result = record["result"]
            console.stage(result["outcome"].replace("_", " ").capitalize())
            console.line(
                f"Opens: {record['saved_opens']} saved / {record['preview_opens']} preview",
                prefix="  ",
            )
            if "remaining_rounds" in record:
                console.line(
                    f"Queued rounds: {record['remaining_rounds']}; batch time: "
                    + duration(record.get("round_seconds", 0)),
                    prefix="  ",
                )
            _attempts(console, result.get("attempts", []))
        else:
            console.event(record)
