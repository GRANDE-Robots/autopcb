"""Exercise the reference projects on the installed native toolchain."""

from __future__ import annotations

import math
from pathlib import Path
import platform

from autopcb import design, jobs, layout
from autopcb.native import Native
from autopcb.workspace import Workspace
from core.export_helpers import sha
from core.project import SourceSnapshot


REFERENCE_PROJECTS = (
    ("Temperature Sensor", "Two-layer I2C temperature sensor at 3.3 V", 2),
    ("USB Breakout", "Four-layer full-speed Type-B USB breakout at 5 V", 4),
)


def _refuse_apply(native, project, storage, run_id, reason: str) -> None:
    try:
        layout.apply(native, project, run_id, storage=storage)
    except RuntimeError as error:
        if reason.casefold() in str(error).casefold():
            return
        raise
    raise RuntimeError("Apply saved a PCB when it should have refused.")


def _exercise_apply(native, project, storage, run_id, original: SourceSnapshot) -> dict:
    lock = project.board.with_name(f"~{project.board.name}.lck")
    with lock.open("x", encoding="utf-8") as handle:
        handle.write("AutoPCB qualification editor lock")
    try:
        _refuse_apply(native, project, storage, run_id, "KiCad board lock present")
        original.verify_unchanged()
    finally:
        lock.unlink()

    original_rules = project.rules.read_bytes()
    try:
        project.rules.write_bytes(original_rules + b"\n# Qualification stale input probe\n")
        _refuse_apply(native, project, storage, run_id, "Routing inputs or policy changed")
    finally:
        project.rules.write_bytes(original_rules)
    original.verify_unchanged()
    applied = layout.apply(native, project, run_id, storage=storage)
    current = SourceSnapshot.capture(project).hashes
    expected = dict(original.hashes)
    expected[project.board.name] = current[project.board.name]
    if current != expected or current == original.hashes:
        raise RuntimeError("Apply did not write exactly one native PCB change.")
    return {"locked_editor_rejected": True, "stale_rules_rejected": True,
            "only_pcb_changed": True, "reported_opens": applied["preview_opens"]}


def _check_artifacts(package: Path, manifest: dict) -> None:
    artifacts = manifest["artifacts"]
    required = {"bom.csv", "placements.csv", "schematic.pdf", "assembly.pdf",
                "evidence/erc.json", "evidence/drc.json", "evidence/reference.json"}
    if (not required <= artifacts.keys()
            or not any(name.startswith("gerbers/") and name.endswith(".gbrjob") for name in artifacts)
            or not any(name.startswith("drills/") and name.endswith(".drl") for name in artifacts)
            or any(not (package / name).stat().st_size or sha(package / name) != digest
                   for name, digest in artifacts.items())):
        raise RuntimeError("Manufacturing outputs are missing or differ from their manifest.")


def _exercise_design(native: Native, workspace: Workspace, case: dict, requirements: str, layers: int,
                     rounds: int, seconds: int, memory_gib: float | None) -> None:
    case["stage"] = "generate"
    output = workspace.project_output(case["name"])
    proposal = design.propose(requirements)
    storage = workspace.storage(output / (output.name + ".kicad_pro"))
    build = jobs.build(native, proposal, proposal["proposal_id"], output, storage=storage)
    if build["pair_layout"]:
        case["pair_layout"] = build["pair_layout"]
    project = workspace.project(Path(build["project"]))
    baseline = SourceSnapshot.capture(project)
    initial = jobs.verify(native, project, storage=storage)
    case["native_layers"] = jobs.inspect(native, project)["geometry"]["copper_layers"]
    if len(case["native_layers"]) != layers:
        raise RuntimeError("Generated board has a different copper layer count.")
    if any(item["check"] != "unconnected_items" for item in initial["native"]["findings"]):
        raise RuntimeError("The initial reference project has a native finding other than opens.")
    case["initial_opens"] = initial["native"]["open_connections"]
    jobs.publish_report(storage.results / "initial-verification.json", initial)

    case["stage"] = "route"
    case["applied"] = None
    if case["initial_opens"]:
        preview = layout.preview(native, project, rounds=rounds, seconds=seconds,
                                 memory_gib=memory_gib, storage=storage)
        baseline.verify_unchanged()
        case["preview"] = {key: preview.get(key) for key in (
            "status", "run_id", "best_preview_opens", "blocked_nets", "attempted_rounds")}
        if preview["best_preview_opens"] < case["initial_opens"]:
            compared = layout.compare(native, project, preview["run_id"], storage=storage)
            if compared["before"]["tracks"] == compared["after"]["tracks"]:
                raise RuntimeError("The improved preview has no visible copper changes.")
            case["applied"] = _exercise_apply(native, project, storage, preview["run_id"], baseline)

    case["stage"] = "verify"
    current = SourceSnapshot.capture(project)
    verified = jobs.verify(native, project, storage=storage)
    case["saved_opens"] = verified["native"]["open_connections"]
    case["native_passed"] = verified["native"]["passed"]
    case["blockers"] = verified["blockers"]
    if case["applied"] and case["applied"]["reported_opens"] != case["saved_opens"]:
        raise RuntimeError("Independent verification disagrees with the applied open count.")
    jobs.publish_report(storage.results / "verification.json", verified)

    case["stage"] = "export"
    draft = workspace.export_output(project, draft=True)
    manifest = jobs.export(native, project, draft, draft=True, storage=storage)
    _check_artifacts(draft, manifest)
    case["draft_artifacts"] = len(manifest["artifacts"])
    checked = workspace.export_output(project, draft=False)
    try:
        manifest = jobs.export(native, project, checked, draft=False, storage=storage)
    except RuntimeError as error:
        if "Manufacturing export blocked." not in str(error) or not verified["blockers"] or checked.exists():
            raise
        case["checked_export"] = "blocked"
    else:
        if not verified["design_checks_passed"]:
            raise RuntimeError("Checked export disagrees with independent verification.")
        _check_artifacts(checked, manifest)
        case["checked_export"] = "passed"
    current.verify_unchanged()
    case.update(stage="done", status="passed" if case["checked_export"] == "passed" else "incomplete")


def qualify(output: Path, *, cli: Path | None, python: Path | None, rounds: int, seconds: int,
            memory_gib: float | None = None) -> dict:
    if not 1 <= rounds <= 100 or not 1 <= seconds <= 300:
        raise ValueError("Qualification expects 1 to 100 rounds and 1 to 300 seconds per batch.")
    if memory_gib is not None and (not math.isfinite(memory_gib) or memory_gib <= 0):
        raise ValueError("Qualification memory must be a positive finite value in GiB.")
    output = output.resolve()
    output.mkdir(parents=True)
    workspace = Workspace(output)
    report_path = output / "qualification.json"
    report = {"schema_version": 1, "kind": "qualification", "status": "running", "report": str(report_path),
              "host": {"os": platform.system(), "release": platform.release(), "version": platform.version(),
                       "architecture": platform.machine()},
              "search_budget": {"rounds_per_project": rounds, "seconds_per_batch": seconds,
                                "memory_gib": memory_gib},
              "cases": [], "hardware_validation": "pending", "production_approved": False}
    jobs.publish_report(report_path, report)
    try:
        native = Native.discover(cli=cli, python=python)
        report["toolchain"] = native.status()
        native.footprints()
        workspace.data.mkdir()
        for name, requirements, layers in REFERENCE_PROJECTS:
            (workspace.data / (name + ".txt")).write_text(requirements + "\n", encoding="utf-8")
            case = {"name": name, "status": "running"}
            report["cases"].append(case)
            jobs.publish_report(report_path, report)
            try:
                _exercise_design(native, workspace, case, requirements, layers, rounds, seconds, memory_gib)
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                case.update(status="failed", error=str(error))
            jobs.publish_report(report_path, report)
        report["status"] = ("reference_checks_passed" if all(case["status"] == "passed" for case in report["cases"])
                            else "qualification_incomplete")
    except (OSError, RuntimeError, ValueError) as error:
        report.update(status="blocked", error=str(error))
    except KeyboardInterrupt:
        report["status"] = "cancelled"
        if report["cases"] and report["cases"][-1]["status"] == "running":
            report["cases"][-1]["status"] = "cancelled"
    jobs.publish_report(report_path, report)
    return report
