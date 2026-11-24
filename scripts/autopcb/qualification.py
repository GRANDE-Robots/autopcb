"""qualification.py: exercise the reference projects on the installed native toolchain.

Each reference project is proposed, generated, verified, routed and exported on
this installation, and the outcome is written to a qualification report. The
report records software evidence only: hardware validation stays pending.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import math
from pathlib import Path
import platform

from autopcb import design, jobs
from autopcb.layout import Layout
from autopcb.native import Native
from autopcb.workspace import Workspace
from core.export_helpers import sha
from core.paths import ProjectStorage
from core.project import ProjectPaths, SourceSnapshot


REFERENCE_PROJECTS = (
    ("Temperature Sensor", "Two-layer I2C temperature sensor at 3.3 V", 2),
    ("USB Breakout", "Four-layer full-speed Type-B USB breakout at 5 V", 4),
)


class ApplyExercise:
    """Checks that applying a routing preview refuses unsafe states."""

    @staticmethod
    def refuse(native, project, storage, run_id, reason: str) -> None:
        """Require that applying a preview is refused for a stated reason.

        Inputs:
            native: KiCad toolchain.
            project: Project the preview was made for.
            storage: Project storage.
            run_id: Routing preview to apply.
            reason: Text, compared without case, the refusal must contain.
        Outputs:
            None. Raises the refusal again when its text differs, and RuntimeError when the
            apply saved a board.
        """
        try:
            Layout(native, project, storage).apply(run_id)
        except RuntimeError as error:
            if reason.casefold() in str(error).casefold():
                return
            raise
        raise RuntimeError("Apply saved a PCB when it should have refused.")

    @classmethod
    def run(cls, native, project, storage, run_id, original: SourceSnapshot) -> dict:
        """Check that apply refuses unsafe states, then apply the preview.

        The apply is first tried while a KiCad editor lock file exists and while the
        rules file differs from the one the preview used. Both must be refused and leave
        the saved sources unchanged.

        Inputs:
            native: KiCad toolchain.
            project: Project the preview was made for.
            storage: Project storage.
            run_id: Routing preview to apply.
            original: Hashes of the saved sources before the apply.
        Outputs:
            Flags for the refused lock, the refused stale rules and a board-only change,
            and the open count the apply reported. Raises RuntimeError unless exactly the
            board file changed.
        """
        lock = project.board.with_name(f"~{project.board.name}.lck")
        with lock.open("x", encoding="utf-8") as handle:
            handle.write("AutoPCB qualification editor lock")
        try:
            cls.refuse(native, project, storage, run_id, "KiCad board lock present")
            original.verify_unchanged()
        finally:
            lock.unlink()

        original_rules = project.rules.read_bytes()
        try:
            project.rules.write_bytes(
                original_rules + b"\n# Qualification stale input probe\n"
            )
            cls.refuse(
                native, project, storage, run_id, "Routing inputs or policy changed"
            )
        finally:
            project.rules.write_bytes(original_rules)
        original.verify_unchanged()
        applied = Layout(native, project, storage).apply(run_id)
        current = SourceSnapshot.capture(project).hashes
        expected = dict(original.hashes)
        expected[project.board.name] = current[project.board.name]
        if current != expected or current == original.hashes:
            raise RuntimeError("Apply did not write exactly one native PCB change.")
        return {
            "locked_editor_rejected": True,
            "stale_rules_rejected": True,
            "only_pcb_changed": True,
            "reported_opens": applied["preview_opens"],
        }


class ReferenceRouting:
    """Routes a reference board in budgeted passes, applying each saved improvement."""

    @staticmethod
    def run(
        native: Native,
        project: ProjectPaths,
        storage: ProjectStorage,
        case: dict,
        baseline: SourceSnapshot,
        verified: dict,
        *,
        rounds: int,
        seconds: int,
        memory_gib: float | None,
    ) -> tuple[dict, SourceSnapshot]:
        """Route and apply previews until the saved board has no opens or the search stalls.

        Inputs:
            native: KiCad toolchain.
            project: Project to route.
            storage: Project storage.
            case: Report record that receives the stage, previews and applied checks.
            baseline: Hashes of the saved sources before routing.
            verified: Verification report of the current saved board.
            rounds: Total routing rounds to spend.
            seconds: Search time of one batch, in seconds.
            memory_gib: Memory limit in GiB, or None.
        Outputs:
            The latest verification report and the sources' hashes after the last apply.
            A preview that does not reduce opens, or spends no rounds, ends the search.
            Raises RuntimeError when a preview reports invalid counts or has no visible
            change, or when verification disagrees with the apply or finds anything but
            open connections.
        """
        case["stage"] = "route"
        case["applied"] = None
        case["routing_passes"] = []
        routing = Layout(native, project, storage)
        remaining = rounds
        while remaining > 0 and (opens := verified["native"]["open_connections"]):
            preview = routing.preview(
                rounds=remaining, seconds=seconds, memory_gib=memory_gib
            )
            baseline.verify_unchanged()
            case["preview"] = {
                key: preview.get(key)
                for key in (
                    "status",
                    "run_id",
                    "best_preview_opens",
                    "blocked_nets",
                    "attempted_rounds",
                )
            }
            attempted = preview["attempted_rounds"]
            if type(attempted) is not int or not 0 <= attempted <= remaining:
                raise RuntimeError(
                    "The routing preview reported an invalid round count."
                )
            predicted = preview["best_preview_opens"]
            if type(predicted) is not int or predicted < 0:
                raise RuntimeError(
                    "The routing preview reported an invalid open count."
                )
            attempt = {
                **case["preview"],
                "round_budget": remaining,
                "starting_opens": opens,
                "saved_opens": opens,
            }
            case["routing_passes"].append(attempt)
            remaining -= attempted
            if predicted >= opens:
                break
            compared = routing.compare(preview["run_id"])
            if compared["before"] == compared["after"]:
                raise RuntimeError(
                    "The improved preview has no visible board geometry changes."
                )
            case["applied"] = ApplyExercise.run(
                native, project, storage, preview["run_id"], baseline
            )
            baseline = SourceSnapshot.capture(project)
            verified = jobs.verify(native, project, storage=storage)
            attempt["saved_opens"] = verified["native"]["open_connections"]
            if (
                attempt["saved_opens"] != case["applied"]["reported_opens"]
                or attempt["saved_opens"] != predicted
            ):
                raise RuntimeError(
                    "Independent verification disagrees with the applied open count."
                )
            if any(
                item["check"] != "unconnected_items"
                for item in verified["native"]["findings"]
            ):
                raise RuntimeError(
                    "The applied board added a native finding other than opens."
                )
        return verified, baseline


class ExportArtifacts:
    """Checks the files of an exported package."""

    @staticmethod
    def check(package: Path, manifest: dict) -> None:
        """Check an exported package against its manifest.

        Inputs:
            package: Folder of the exported package.
            manifest: Manifest the export returned.
        Outputs:
            None. Raises RuntimeError unless the BOM, placements, PDFs, ERC, DRC and
            reference evidence, a Gerber job and a drill file are listed, and every listed
            file is not empty and has its recorded hash.
        """
        artifacts = manifest["artifacts"]
        required = {
            "bom.csv",
            "placements.csv",
            "schematic.pdf",
            "assembly.pdf",
            "evidence/erc.json",
            "evidence/drc.json",
            "evidence/reference.json",
        }
        if (
            not required <= artifacts.keys()
            or not any(
                name.startswith("gerbers/") and name.endswith(".gbrjob")
                for name in artifacts
            )
            or not any(
                name.startswith("drills/") and name.endswith(".drl")
                for name in artifacts
            )
            or any(
                not (package / name).stat().st_size or sha(package / name) != digest
                for name, digest in artifacts.items()
            )
        ):
            raise RuntimeError(
                "Manufacturing outputs are missing or differ from their manifest."
            )


class ReferenceDesign:
    """Runs one reference project through generation, routing, verification and export."""

    @staticmethod
    def run(
        native: Native,
        workspace: Workspace,
        case: dict,
        requirements: str,
        layers: int,
        rounds: int,
        seconds: int,
        memory_gib: float | None,
    ) -> None:
        """Take one reference project from requirements to a checked export.

        Inputs:
            native: KiCad toolchain.
            workspace: Workspace that receives the project.
            case: Report record of this project; it is filled in stage by stage.
            requirements: Requirements text of the reference project.
            layers: Copper layers the generated board must have.
            rounds: Routing rounds to spend in total.
            seconds: Search time of one batch, in seconds.
            memory_gib: Memory limit in GiB, or None.
        Outputs:
            None. The case records the outline, open counts, native result, blockers and
            export outcome, and its status is ``passed`` only when the checked export
            passed. Raises RuntimeError when generation, routing or export disagrees with
            independent verification.
        """
        case["stage"] = "generate"
        output = workspace.project_output(case["name"])
        proposal = design.propose(requirements)
        storage = workspace.storage(output / (output.name + ".kicad_pro"))
        build = jobs.build(
            native, proposal, proposal["proposal_id"], output, storage=storage
        )
        case["outline"] = build["outline"]
        if build["pair_layout"]:
            case["pair_layout"] = build["pair_layout"]
        project = workspace.project(Path(build["project"]))
        baseline = SourceSnapshot.capture(project)
        initial = jobs.verify(native, project, storage=storage)
        case["native_layers"] = jobs.inspect(native, project)["geometry"][
            "copper_layers"
        ]
        if len(case["native_layers"]) != layers:
            raise RuntimeError("Generated board has a different copper layer count.")
        if any(
            item["check"] != "unconnected_items"
            for item in initial["native"]["findings"]
        ):
            raise RuntimeError(
                "The initial reference project has a native finding other than opens."
            )
        case["initial_opens"] = initial["native"]["open_connections"]
        jobs.publish_report(storage.results / "initial-verification.json", initial)
        verified, baseline = ReferenceRouting.run(
            native,
            project,
            storage,
            case,
            baseline,
            initial,
            rounds=rounds,
            seconds=seconds,
            memory_gib=memory_gib,
        )

        case["stage"] = "verify"
        baseline.verify_unchanged()
        case["saved_opens"] = verified["native"]["open_connections"]
        case["native_passed"] = verified["native"]["passed"]
        case["blockers"] = verified["blockers"]
        jobs.publish_report(storage.results / "verification.json", verified)

        case["stage"] = "export"
        draft = workspace.export_output(project, draft=True)
        manifest = jobs.export(native, project, draft, draft=True, storage=storage)
        ExportArtifacts.check(draft, manifest)
        case["draft_artifacts"] = len(manifest["artifacts"])
        checked = workspace.export_output(project, draft=False)
        try:
            manifest = jobs.export(
                native, project, checked, draft=False, storage=storage
            )
        except RuntimeError as error:
            if (
                "Manufacturing export blocked." not in str(error)
                or not verified["blockers"]
                or checked.exists()
            ):
                raise
            case["checked_export"] = "blocked"
        else:
            if not verified["design_checks_passed"]:
                raise RuntimeError(
                    "Checked export disagrees with independent verification."
                )
            ExportArtifacts.check(checked, manifest)
            case["checked_export"] = "passed"
        baseline.verify_unchanged()
        case.update(
            stage="done",
            status="passed" if case["checked_export"] == "passed" else "incomplete",
        )


class Qualification:
    """Qualifies an installation on the reference projects."""

    @staticmethod
    def run(
        output: Path,
        *,
        cli: Path | None,
        python: Path | None,
        rounds: int,
        seconds: int,
        memory_gib: float | None = None,
        fixed_outlines: bool = False,
    ) -> dict:
        """Exercise both reference projects and write a qualification report.

        Inputs:
            output: New folder for the workspace, cases and ``qualification.json``.
            cli: KiCad CLI to use; the discovered one when None.
            python: KiCad Python to use; the discovered one when None.
            rounds: Routing rounds per project, 1 to 100.
            seconds: Search time of one batch, 1 to 300 seconds.
            memory_gib: Memory limit in GiB, or None.
            fixed_outlines: Ask each reference project for its nominal catalog
                outline, as a control for compact placement.
        Outputs:
            The report. Its status is ``reference_checks_passed``,
            ``qualification_incomplete``, ``blocked`` or ``cancelled``; hardware validation
            stays pending and production is never approved. Raises ValueError for rounds,
            seconds or memory out of range. The report file is republished after every
            stage.
        """
        if not 1 <= rounds <= 100 or not 1 <= seconds <= 300:
            raise ValueError(
                "Qualification expects 1 to 100 rounds and 1 to 300 seconds per batch."
            )
        if memory_gib is not None and (
            not math.isfinite(memory_gib) or memory_gib <= 0
        ):
            raise ValueError(
                "Qualification memory must be a positive finite value in GiB."
            )
        output = output.resolve()
        output.mkdir(parents=True)
        workspace = Workspace(output)
        report_path = output / "qualification.json"
        report = {
            "schema_version": 1,
            "kind": "qualification",
            "status": "running",
            "report": str(report_path),
            "host": {
                "os": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "architecture": platform.machine(),
            },
            "search_budget": {
                "rounds_per_project": rounds,
                "seconds_per_batch": seconds,
                "memory_gib": memory_gib,
            },
            "cases": [],
            "hardware_validation": "pending",
            "production_approved": False,
        }
        jobs.publish_report(report_path, report)
        try:
            native = Native.discover(cli=cli, python=python)
            report["toolchain"] = native.status()
            native.footprints()
            workspace.data.mkdir()
            for name, requirements, layers in REFERENCE_PROJECTS:
                if fixed_outlines:
                    requirements += " with a fixed catalog outline"
                (workspace.data / (name + ".txt")).write_text(
                    requirements + "\n", encoding="utf-8"
                )
                case = {"name": name, "status": "running"}
                report["cases"].append(case)
                jobs.publish_report(report_path, report)
                try:
                    ReferenceDesign.run(
                        native,
                        workspace,
                        case,
                        requirements,
                        layers,
                        rounds,
                        seconds,
                        memory_gib,
                    )
                except (
                    OSError,
                    RuntimeError,
                    ValueError,
                    KeyError,
                    TypeError,
                ) as error:
                    case.update(status="failed", error=str(error))
                jobs.publish_report(report_path, report)
            report["status"] = (
                "reference_checks_passed"
                if all(case["status"] == "passed" for case in report["cases"])
                else "qualification_incomplete"
            )
        except (OSError, RuntimeError, ValueError) as error:
            report.update(status="blocked", error=str(error))
        except KeyboardInterrupt:
            report["status"] = "cancelled"
            if report["cases"] and report["cases"][-1]["status"] == "running":
                report["cases"][-1]["status"] = "cancelled"
        jobs.publish_report(report_path, report)
        return report
