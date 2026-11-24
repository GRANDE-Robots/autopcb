"""jobs.py: source-bound jobs shared by the desktop app and the terminal.

Each job reads the saved project through a private copy, checks it with KiCad's own
tools and reports a JSON record. Nothing here approves a design for production.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
from pathlib import Path

from core.compat import strict_zip
import json
import os
import re
import shutil
import tempfile

from autopcb import design
from autopcb.native import Native
from autopcb.policy import RoutingPolicy
from core.export_helpers import sha
from core.kicad_sexpr import quote
from core.paths import ProjectStorage
from core.project import PROFILE_NAME, ProjectPaths, ProjectProfile, SourceSnapshot


class JsonRecordWriter:
    """Writes a record as a new UTF-8 JSON file."""

    def __call__(self, path: Path, record: dict) -> None:
        """Write a record as a new JSON file.

        Inputs:
            path: File to create; it must not exist.
            record: JSON-compatible record; NaN is refused.
        Outputs:
            None. Raises FileExistsError when the file exists.
        """
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")


write_json = JsonRecordWriter()


class ReportPublisher:
    """Replaces a report file in one step."""

    def __call__(self, target: Path, record: dict) -> None:
        """Publish a fresh machine result without partially replacing the previous report.

        Inputs:
            target: Report file; its folder is created when missing.
            record: JSON-compatible record; NaN is refused.
        Outputs:
            None. The record is staged in a temporary file beside the target and moved over
            it, and the temporary file is removed on failure.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=target.parent,
                prefix=".autopcb-report-",
                delete=False,
            ) as handle:
                staged = Path(handle.name)
                json.dump(record, handle, indent=2, ensure_ascii=False, allow_nan=False)
                handle.write("\n")
            os.replace(staged, target)
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)


publish_report = ReportPublisher()


class NewDirectory:
    """Builds a folder in a staging area and moves it into place."""

    @staticmethod
    @contextmanager
    def stage(target: Path):
        """Stage a new folder and move it into place when the job succeeds.

        Inputs:
            target: Destination folder; it must not exist and its parent must.
        Outputs:
            Yields the staging folder, named like the target. A normal exit renames it to
            the target and an error leaves nothing behind. Raises ValueError when the
            destination exists or its parent does not, and FileExistsError when the
            destination appears during the job.
        """
        target = target.resolve()
        if target.exists() or not target.parent.is_dir():
            raise ValueError(
                f"Select a new destination under an existing directory: {target}"
            )
        with tempfile.TemporaryDirectory(
            prefix=".autopcb-stage-", dir=target.parent
        ) as staging:
            stage = Path(staging) / target.name
            stage.mkdir()
            yield stage
            if target.exists():
                raise FileExistsError(
                    f"Destination appeared during preparation: {target}"
                )
            os.rename(stage, target)


class IsolatedCopy:
    """Runs a job on a temporary copy of a project's saved files."""

    @staticmethod
    @contextmanager
    def open(paths: ProjectPaths, snapshot: SourceSnapshot | None = None):
        """Copy a project's saved source files into a temporary folder.

        Inputs:
            paths: Project to copy.
            snapshot: Hashes of the saved sources; captured now when None.
        Outputs:
            Yields the copy's project paths. Raises RuntimeError when a source changes
            while it is copied or before or after the job body.
        """
        snapshot = snapshot or SourceSnapshot.capture(paths)
        with tempfile.TemporaryDirectory(prefix="autopcb-native-") as scratch:
            root = Path(scratch)
            for path, expected in snapshot.hashes.items():
                copied = root / path
                copied.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(paths.root / path, copied)
                if sha(copied) != expected:
                    raise RuntimeError(
                        f"Source changed while preparing the job: {path}"
                    )
            snapshot.verify_unchanged()
            yield ProjectPaths(root / paths.entry.name)
            snapshot.verify_unchanged()


class NativeReport:
    """Reads the records inside KiCad's JSON reports."""

    @staticmethod
    def issues(report: dict, name: str) -> list[dict]:
        """The list of records under a name in a KiCad JSON report.

        Inputs:
            report: Parsed KiCad report.
            name: Field that holds the list.
        Outputs:
            The list. Raises RuntimeError when the field is missing, is not a list or
            holds anything but objects.
        """
        values = report.get(name)
        if not isinstance(values, list) or any(
            not isinstance(item, dict) for item in values
        ):
            raise RuntimeError(f"Missing or invalid KiCad report field: {name}")
        return values

    @staticmethod
    def findings(items: list[dict], family: str) -> list[dict]:
        """Turn KiCad report records into findings.

        Inputs:
            items: Violation records of one check family.
            family: Check family recorded in each finding.
        Outputs:
            One finding per record with its check, type, description and severity, and
            the positions in mm of its items that have a numeric position.
        """
        findings = []
        for item in items:
            points = []
            for child in item.get("items", ()):
                position = child.get("pos", {})
                if all(isinstance(position.get(n), (int, float)) for n in ("x", "y")):
                    points.append(
                        {
                            "x": position["x"],
                            "y": position["y"],
                            "description": child.get("description", ""),
                        }
                    )
            findings.append(
                {
                    "check": family,
                    "type": item.get("type", "unknown"),
                    "description": item.get("description", ""),
                    "severity": item.get("severity", "unknown"),
                    "positions_mm": points,
                }
            )
        return findings


class NativeChecks:
    """Runs KiCad's schematic and board checks on a saved project."""

    @staticmethod
    def run(
        native: Native, paths: ProjectPaths, output: Path, *, save_zones: bool = False
    ) -> dict:
        """Run ERC and DRC on a saved project and list their findings.

        Inputs:
            native: KiCad toolchain.
            paths: Project with a saved schematic and board.
            output: New folder for the two reports.
            save_zones: Also save the refilled zones to the board.
        Outputs:
            Whether nothing was found, every finding (an ignored check counts as a blocked
            finding) and the number of open connections. Raises ValueError when the
            schematic or board is not saved and RuntimeError for an invalid report.
        """
        if not paths.board.is_file() or not paths.schematic.is_file():
            raise ValueError("Native verification needs a saved schematic and PCB")
        output.mkdir()
        erc = native.report(("sch", "erc"), paths.schematic, output / "erc.json")
        flags = ["pcb", "drc", "--schematic-parity", "--refill-zones"]
        if save_zones:
            flags.append("--save-board")
        drc = native.report(tuple(flags), paths.board, output / "drc.json")
        findings = []
        for sheet in NativeReport.issues(erc, "sheets"):
            findings.extend(
                NativeReport.findings(NativeReport.issues(sheet, "violations"), "erc")
            )
        for field in ("violations", "unconnected_items", "schematic_parity"):
            findings.extend(
                NativeReport.findings(NativeReport.issues(drc, field), field)
            )
        for label, report in (("DRC", drc), ("ERC", erc)):
            ignored = report.get("ignored_checks", [])
            if not isinstance(ignored, list):
                raise RuntimeError(f"Invalid ignored checks in the {label} report")
            findings.extend(
                {
                    "check": label,
                    "type": "ignored_check",
                    "description": str(check),
                    "severity": "blocked",
                    "positions_mm": [],
                }
                for check in ignored
            )
        return {
            "passed": not findings,
            "findings": findings,
            "open_connections": len(NativeReport.issues(drc, "unconnected_items")),
        }


class DraftPlacement:
    """Generates the first native board of a new project from the catalog."""

    @staticmethod
    def run(native, paths, scratch, spec, compact, pair_layout, drc_checks, erc_checks):
        """Generate the board, trying the compact outline first when one is approved.

        Inputs:
            native: KiCad toolchain.
            paths: Paths of the new project; its board and settings are written.
            scratch: Folder for the trial files.
            spec: Board spec with netlist, links, footprint libraries, layers, size_mm
                and components.
            compact: Compact outline limits in mm, or None for the catalog outline.
            pair_layout: Limits for seeding a differential pair, or None.
            drc_checks: DRC rules lowered to warnings in the project settings.
            erc_checks: ERC rules lowered to warnings in the project settings.
        Outputs:
            The outline receipt (mode, size_mm, area_mm2, maximum_size_mm, moved
            footprints and the fallback reason when the compact outline was refused) and
            the pair seeding result or None. A compact outline that fails placement checks
            or defers pair seeding falls back to the catalog outline. Raises ValueError
            when the fallback outline fails the placement checks or an outline cannot be
            measured, and RuntimeError for an invalid KiCad report.
        """
        trials = (
            [("fixed", None)]
            if compact is None
            else [
                ("compact", compact),
                (
                    "catalog_fallback",
                    {**compact, "minimum_mm": spec["size_mm"], "movable_groups": []},
                ),
            ]
        )
        fallback_reason = None
        for mode, outline in trials:
            trial = scratch / mode
            trial.mkdir()
            model = trial / "board.json"
            write_json(model, {**spec, "compact_outline": outline})
            native.worker("generate", paths.board, model, output=paths.board)
            settings = json.loads(paths.settings.read_text(encoding="utf-8-sig"))
            settings.setdefault("board", {}).setdefault(
                "design_settings", {}
            ).setdefault("rule_severities", {}).update(
                dict.fromkeys(drc_checks, "warning")
            )
            settings.setdefault("erc", {}).setdefault("rule_severities", {}).update(
                dict.fromkeys(erc_checks, "warning")
            )
            paths.settings.write_text(
                json.dumps(settings, indent=2) + "\n", encoding="utf-8"
            )
            normalized = trial / "normalize.json"
            drc = native.report(
                ("pcb", "drc", "--refill-zones", "--save-board", "--schematic-parity"),
                paths.board,
                normalized,
            )
            findings = NativeReport.issues(drc, "violations") + NativeReport.issues(
                drc, "schematic_parity"
            )
            if compact is not None and (findings or drc.get("ignored_checks")):
                reason = ", ".join(
                    str(item.get("description", item.get("type")))
                    for item in findings[:3]
                )
                fallback_reason = "Placement DRC: " + (reason or "ignored checks")
                if mode != "compact":
                    raise ValueError(fallback_reason)
                paths.board.unlink()  # Only the rejected, private generation trial.
                continue
            pair_result = None
            if pair_layout is not None:
                request = trial / "pair-request.json"
                result = trial / "pair-result.json"
                write_json(
                    request,
                    {"pair_layout": pair_layout, "baseline_drc": str(normalized)},
                )
                native.worker("pair-seed", paths.entry, result, request, output=result)
                pair_result = json.loads(result.read_text(encoding="utf-8"))
                if mode == "compact" and pair_result["status"] == "deferred":
                    fallback_reason = pair_result["reason"]
                    paths.board.unlink()
                    continue
            report = trial / "inspect.json"
            native.worker("inspect", paths.board, report, output=report)
            geometry = json.loads(report.read_text(encoding="utf-8"))
            # Edge.Cuts centerlines define the finished size. The display bounds also include the line stroke.
            if len(geometry["outline"]) != 1 or len(geometry["outline"][0]) < 4:
                raise ValueError(
                    "KiCad could not measure the generated rectangular outline."
                )
            axes = list(strict_zip(*geometry["outline"][0]))
            lower = tuple(map(min, axes))
            upper = tuple(map(max, axes))
            size = [round(b - a, 6) for a, b in strict_zip(lower, upper)]
            moved = sorted(
                fp["ref"]
                for fp in geometry["footprints"]
                if any(
                    abs(
                        fp["at"][axis]
                        - lower[axis]
                        - spec["components"][fp["ref"]]["position"][axis]
                    )
                    > 0.000001
                    for axis in (0, 1)
                )
            )
            receipt = {
                "mode": mode,
                "size_mm": size,
                "area_mm2": round(size[0] * size[1], 6),
                "maximum_size_mm": spec["size_mm"],
                "moved_footprints": moved,
            }
            if fallback_reason:
                receipt["fallback_reason"] = fallback_reason
            return receipt, pair_result


class GeometryInspection:
    """Reads a project's board geometry through the native worker."""

    def __call__(self, native: Native, paths: ProjectPaths) -> dict:
        """Read a project's board geometry in an isolated copy.

        Inputs:
            native: KiCad toolchain.
            paths: Project to inspect.
        Outputs:
            An inspect report with the source hashes, the geometry in mm and the
            toolchain. Raises RuntimeError when the saved sources change during the job.
        """
        snapshot = SourceSnapshot.capture(paths)
        with IsolatedCopy.open(paths, snapshot) as working:
            output = working.root / "autopcb-geometry.json"
            native.worker("inspect", working.board, output, output=output)
            geometry = json.loads(output.read_text(encoding="utf-8"))
        return {
            "schema_version": 1,
            "kind": "inspect",
            "status": "inspected",
            "entry": str(paths.entry),
            "inputs": snapshot.hashes,
            "geometry": geometry,
            "toolchain": native.status(),
        }


inspect = GeometryInspection()


class ElectricalReview:
    """Checks the electrical review report that a project names."""

    @staticmethod
    def evidence(
        paths: ProjectPaths,
        snapshot: SourceSnapshot,
        reference: dict,
        storage: ProjectStorage | None = None,
    ) -> tuple[bytes | None, str | None]:
        """Check the source-bound electrical review report that a project names.

        Inputs:
            paths: Project whose profile names the review in ``electrical_review``.
            snapshot: Hashes of the saved sources the review must match.
            reference: Reference-copper screen of the board, with its pairs and findings.
            storage: Where project data lives; the project's own storage when None.
        Outputs:
            The report bytes and None when the report is a current, approved review of
            these exact sources, policy and findings. Otherwise None and the reason.
        """
        profile = ProjectProfile.read(paths.root)
        review = profile.pop("electrical_review", None)
        if not isinstance(review, dict) or not isinstance(review.get("file"), str):
            return None, "A source-bound electrical review report is required."
        relative = Path(review["file"])
        if relative.is_absolute() or ".." in relative.parts:
            return None, "Use a relative electrical review file in project data."
        data = (storage or ProjectStorage(paths.root)).data_file(*relative.parts)
        fallback = paths.root / relative
        path = data.resolve() if data.is_file() else fallback.resolve()
        scope = (
            storage.workspace
            if storage
            and storage.workspace
            and path.is_relative_to(storage.data.resolve())
            else paths.root
        )
        if (
            not path.is_relative_to(scope)
            or not path.is_file()
            or path.stat().st_size > 2_000_000
        ):
            return (
                None,
                "The electrical review must be a report file in project or workspace data.",
            )
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != review.get("sha256"):
            return None, "The electrical review file differs from the approved report."
        try:
            report = json.loads(content)
            required = (
                ("electrical", "reference_copper")
                if reference["pairs"]
                else ("electrical",)
            )
            measured = report.get("checks", {})
            excluded = {PROFILE_NAME}
            if path.is_relative_to(paths.root):
                excluded.add(path.relative_to(paths.root).as_posix())
            source_hashes = {
                key: value
                for key, value in snapshot.hashes.items()
                if key not in excluded
            }
            if (
                report.get("schema_version") != 1
                or report.get("sources") != source_hashes
                or report.get("policy_sha256") != design.digest(profile)
                or not isinstance(report.get("reviewer"), str)
                or not report["reviewer"].strip()
                or not all(
                    isinstance(measured.get(check), dict)
                    and measured[check].get("status") == "pass"
                    and measured[check].get("measurements")
                    and isinstance(measured[check].get("method"), str)
                    and measured[check]["method"].strip()
                    for check in required
                )
            ):
                return (
                    None,
                    "The electrical report lacks passing measurements for these exact sources and policy.",
                )
            if reference["findings"]:
                expected = {finding["id"] for finding in reference["findings"]}
                dispositions = measured["reference_copper"].get("findings")
                if (
                    not isinstance(dispositions, dict)
                    or dispositions.keys() != expected
                    or any(
                        not isinstance(reason, str) or not reason.strip()
                        for reason in dispositions.values()
                    )
                ):
                    return None, (
                        "Document the disposition of every current reference finding in the source-bound "
                        "electrical review."
                    )
        except (ValueError, AttributeError, TypeError):
            return None, "The electrical review report is malformed."
        if sha(path) != review.get("sha256"):
            return None, "The electrical review changed while checking it."
        return content, None


class DesignChecks:
    """Gathers native findings, the reference screen and review blockers."""

    @staticmethod
    def run(
        native: Native,
        paths: ProjectPaths,
        working: ProjectPaths,
        output: Path,
        snapshot: SourceSnapshot,
        storage: ProjectStorage | None,
        *,
        save_zones: bool,
    ):
        """Run the native checks and the reference screen and collect the blockers.

        Inputs:
            native: KiCad toolchain.
            paths: The saved project.
            working: Isolated copy the checks run on.
            output: New folder for the reports.
            snapshot: Hashes of the saved sources.
            storage: Where project data lives; the project's own storage when None.
            save_zones: Also save the refilled zones in the copy.
        Outputs:
            The native checks, the reference screen, the review report bytes (or None),
            the review blocker (or None) and every blocker as text.
        """
        checks = NativeChecks.run(native, working, output, save_zones=save_zones)
        report_path = output / "reference.json"
        native.worker("reference", working.entry, report_path, output=report_path)
        reference = json.loads(report_path.read_text(encoding="utf-8"))
        proof, review_blocker = ElectricalReview.evidence(
            paths, snapshot, reference, storage
        )
        blockers = (
            []
            if checks["passed"]
            else ["Native DRC, ERC or schematic parity has findings."]
        )
        blockers.extend(reference["blockers"])
        if review_blocker:
            blockers.append(review_blocker)
        return checks, reference, proof, review_blocker, blockers


class Verification:
    """Verifies a project independently of the saved design."""

    def __call__(
        self,
        native: Native,
        paths: ProjectPaths,
        *,
        storage: ProjectStorage | None = None,
    ) -> dict:
        """Verify a project independently without changing the saved design.

        Inputs:
            native: KiCad toolchain.
            paths: Project to verify.
            storage: Where project data lives; the project's own storage when None.
        Outputs:
            A verify report with the source hashes, native findings, reference screen,
            blockers and toolchain. Its status is ``native_passed`` or ``blocked``;
            hardware validation stays pending and production is never approved. Raises
            RuntimeError when the saved sources change during the job.
        """
        snapshot = SourceSnapshot.capture(paths)
        with IsolatedCopy.open(paths, snapshot) as working:
            checks, reference, _, review_blocker, blockers = DesignChecks.run(
                native,
                paths,
                working,
                working.root / "autopcb-native-reports",
                snapshot,
                storage,
                save_zones=bool(
                    ProjectProfile.read(working.root).get("differential_pairs")
                ),
            )
        snapshot.verify_unchanged()
        return {
            "schema_version": 1,
            "kind": "verify",
            "status": "native_passed" if checks["passed"] else "blocked",
            "entry": str(paths.entry),
            "inputs": snapshot.hashes,
            "native": checks,
            "reference_screen": reference,
            "design_checks_passed": not blockers,
            "blockers": blockers,
            "electrical_review_blocker": review_blocker,
            "toolchain": native.status(),
            "hardware_validation": "pending",
            "production_approved": False,
        }


verify = Verification()


class DesignBuild:
    """Creates editable native files from an approved proposal."""

    @staticmethod
    def _remove_new_policy(path: Path, initial: bytes) -> None:
        """Delete a policy file the build wrote, unless it was changed since.

        Inputs:
            path: Policy file.
            initial: The bytes the build wrote.
        """
        if path.is_file() and path.read_bytes() == initial:
            path.unlink()

    def __call__(
        self,
        native: Native,
        proposal: dict,
        approval: str,
        output: Path,
        *,
        defaults: Path | None = None,
        storage: ProjectStorage | None = None,
    ) -> dict:
        """Create editable native files from an approved proposal.

        Inputs:
            native: KiCad toolchain.
            proposal: Design proposal.
            approval: The ``proposal_id`` the reviewer approved.
            output: New project folder; its name is the project name.
            defaults: Routing defaults file for the starter policy.
            storage: Where the routing policy is written; beside the project when None.
        Outputs:
            A design_build report with the project entry, proposal ID, policy path, pair
            seeding result and outline receipt. Raises ValueError for an unapproved
            proposal or a project name that is not 1 to 64 plain characters, FileExistsError
            when the policy or folder exists, and RuntimeError when the generated schematic
            fails ERC. A failed build leaves no folder and removes the policy it wrote.
        """
        body = design.validated(proposal, approval)
        title = output.name
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}", title
        ) or title.endswith((" ", ".")):
            raise ValueError(
                "Use a short project folder name with letters, numbers, spaces, dots, hyphens or underscores"
            )
        source, symbols, links = design.schematic(proposal, title)
        layers = [
            "F.Cu",
            *[f"In{idx}.Cu" for idx in range(1, body["layers"] - 1)],
            "B.Cu",
        ]
        critical = ["/" + net for pair in body["differential_pairs"] for net in pair]
        pair_layout = body.get("pair_layout")
        policy = RoutingPolicy.compile(
            layers, defaults=defaults, excluded=critical, refill=bool(pair_layout)
        )
        final_policy = (
            storage.config if storage else output / "config"
        ) / "autopcb.json"
        if final_policy.exists():
            raise FileExistsError(f"A routing policy already exists: {final_policy}")
        with ExitStack() as rollback:
            with NewDirectory.stage(output) as stage:
                paths = ProjectPaths(stage / f"{title}.kicad_pro")
                paths.schematic.write_bytes(source.encode("utf-8"))
                drc_checks = (
                    "missing_courtyard",
                    "track_not_centered_on_via",
                    "tuning_profile_track_geometries",
                    "footprint_filters_mismatch",
                    "footprint_type_mismatch",
                )
                erc_checks = (
                    "single_global_label",
                    "four_way_junction",
                    "simulation_model_issue",
                    "footprint_filter",
                )
                write_json(
                    paths.settings,
                    {
                        "meta": {"filename": paths.settings.name, "version": 1},
                        "board": {
                            "design_settings": {
                                "rule_severities": dict.fromkeys(drc_checks, "warning")
                            }
                        },
                        "erc": {
                            "rule_severities": dict.fromkeys(erc_checks, "warning")
                        },
                    },
                )
                lib = stage / "libraries"
                lib.mkdir()
                (lib / "AutoPCB.kicad_sym").write_bytes(symbols.encode("utf-8"))
                (stage / "sym-lib-table").write_text(
                    '(sym_lib_table (version 7) (lib (name "AutoPCB")(type "KiCad")'
                    '(uri "${KIPRJMOD}/libraries/AutoPCB.kicad_sym")(options "")(descr "AutoPCB generated symbols")))\n',
                    encoding="utf-8",
                )
                groups = {
                    component["footprint"].split(":", 1)[0]
                    for component in body["components"].values()
                }
                table = "\n".join(
                    f'(lib (name {quote(group)})(type "KiCad")(uri "${{KICAD10_FOOTPRINT_DIR}}/{group}.pretty")'
                    '(options "")(descr "KiCad standard footprint library"))'
                    for group in sorted(groups)
                )
                (stage / "fp-lib-table").write_text(
                    f"(fp_lib_table (version 7)\n{table}\n)\n", encoding="utf-8"
                )
                paths.rules.write_text("(version 1)\n", encoding="utf-8")
                profile = {
                    "schema_version": 1,
                    "entry": paths.settings.name,
                    "proposal": approval,
                    "requirements": body["requirements"],
                    "design": body["design"],
                    "supply_v": body["supply_v"],
                    "copper_layers": body["layers"],
                    "differential_pairs": body["differential_pairs"],
                    "high_speed_reviewed": True,
                    "assumptions": body["assumptions"],
                }
                if pair_layout is not None:
                    profile["reference_net"] = pair_layout["reference_net"]
                write_json(stage / PROFILE_NAME, profile)
                write_json(stage / "design-proposal.json", proposal)
                with tempfile.TemporaryDirectory(prefix="autopcb-netlist-") as scratch:
                    scratch = Path(scratch)
                    xml = scratch / "schematic.xml"
                    native.call(
                        "sch",
                        "export",
                        "netlist",
                        "--format",
                        "kicadxml",
                        "--output",
                        xml,
                        paths.schematic,
                        output=xml,
                    )
                    erc = native.report(
                        ("sch", "erc"), paths.schematic, scratch / "erc.json"
                    )
                    findings = [
                        item
                        for sheet in NativeReport.issues(erc, "sheets")
                        for item in NativeReport.issues(sheet, "violations")
                        if item.get("severity") != "warning"
                    ]
                    if findings:
                        raise RuntimeError(
                            "Generated schematic did not pass native ERC: "
                            + "; ".join(
                                str(item.get("description")) for item in findings[:6]
                            )
                        )
                    spec = {
                        "netlist": str(xml),
                        "links": links,
                        "footprints": str(native.footprints()),
                        **{
                            key: body[key]
                            for key in ("layers", "size_mm", "components")
                        },
                    }
                    placed, pair_result = DraftPlacement.run(
                        native,
                        paths,
                        scratch,
                        spec,
                        body.get("compact_outline"),
                        pair_layout,
                        drc_checks,
                        erc_checks,
                    )
                    if "compact_outline" in body:
                        publish_report(
                            stage / PROFILE_NAME, {**profile, "initial_outline": placed}
                        )
                target = (
                    final_policy if storage else stage / "config" / final_policy.name
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                write_json(target, policy)
                if storage:
                    rollback.callback(
                        self._remove_new_policy, target, target.read_bytes()
                    )
                # The output context renames the prepared native files on successful exit.
            rollback.pop_all()
        return {
            "schema_version": 1,
            "kind": "design_build",
            "status": "created",
            "project": str(output.resolve() / f"{title}.kicad_pro"),
            "proposal_id": approval,
            "policy": str(final_policy.resolve()),
            "pair_layout": pair_result,
            "outline": placed,
        }


build = DesignBuild()


class ManufacturingExport:
    """Exports a manufacturing package from an isolated copy of a project."""

    @staticmethod
    def _check_gerbers(directory: Path, layer_count: int) -> None:
        """Check a fresh Gerber folder against its job file.

        Inputs:
            directory: Folder KiCad plotted into.
            layer_count: Copper layers the board has.
        Outputs:
            None. Raises RuntimeError unless there is one job file whose plots exist and are
            not empty, and which covers every copper layer, both solder mask, paste and
            legend sides and exactly one board profile.
        """
        job_files = list(directory.glob("*.gbrjob"))
        if len(job_files) != 1:
            raise RuntimeError("Expected one fresh KiCad Gerber job file.")
        job = json.loads(job_files[0].read_text(encoding="utf-8-sig"))
        rows = job.get("FilesAttributes", [])
        for row in rows:
            name = row["Path"]
            file = directory / name
            if (
                file.resolve().parent != directory
                or not file.is_file()
                or not file.stat().st_size
            ):
                raise RuntimeError(
                    f"Gerber job references a missing or invalid plot: {name}"
                )
        functions = [row["FileFunction"].split(",") for row in rows]
        required = {
            ("SolderMask", "Top"),
            ("SolderMask", "Bot"),
            ("SolderPaste", "Top"),
            ("SolderPaste", "Bot"),
            ("Legend", "Top"),
            ("Legend", "Bot"),
        }
        if (
            sorted(
                (
                    item[1]
                    for item in functions
                    if len(item) >= 2 and item[0] == "Copper"
                )
            )
            != sorted(f"L{index}" for index in range(1, layer_count + 1))
            or sum(item[0] == "Profile" for item in functions) != 1
            or not required <= {tuple(item[:2]) for item in functions}
        ):
            raise RuntimeError("Gerber job does not cover the required board layers.")

    def __call__(
        self,
        native: Native,
        paths: ProjectPaths,
        output: Path,
        *,
        draft: bool,
        storage: ProjectStorage | None = None,
    ) -> dict:
        """Export a manufacturing package into a new folder.

        Inputs:
            native: KiCad toolchain.
            paths: Project to export.
            output: New package folder.
            draft: Label the package a draft and allow it despite blockers.
            storage: Where project data lives; the project's own storage when None.
        Outputs:
            The manifest with its status (``draft`` or ``design_checked``), blockers,
            source hashes, board hash, toolchain and the hash of every file. The package
            holds Gerbers, drills, BOM, placements, schematic and assembly PDFs and the
            check evidence. Raises RuntimeError when blockers remain without ``draft`` or an
            export is missing or empty. The folder is not created on failure.
        """
        snapshot = SourceSnapshot.capture(paths)
        with (
            NewDirectory.stage(output) as stage,
            IsolatedCopy.open(paths, snapshot) as working,
        ):
            evidence = stage / "evidence"
            _, _, proof, _, blockers = DesignChecks.run(
                native, paths, working, evidence, snapshot, storage, save_zones=True
            )
            if proof is not None:
                (evidence / "electrical.json").write_bytes(proof)
            if blockers and not draft:
                raise RuntimeError(
                    "Manufacturing export blocked. " + " ".join(blockers)
                )
            geometry = working.root / "autopcb-geometry.json"
            native.worker("inspect", working.board, geometry, output=geometry)
            layers = json.loads(geometry.read_text(encoding="utf-8"))["copper_layers"]
            gerbers, drills = stage / "gerbers", stage / "drills"
            gerbers.mkdir()
            drills.mkdir()
            native.call(
                "pcb",
                "export",
                "gerbers",
                "--layers",
                ",".join(
                    [
                        *layers,
                        "F.Mask",
                        "B.Mask",
                        "F.Paste",
                        "B.Paste",
                        "F.Silkscreen",
                        "B.Silkscreen",
                        "Edge.Cuts",
                    ]
                ),
                "--output",
                gerbers,
                working.board,
            )
            native.call("pcb", "export", "drill", "--output", drills, working.board)
            self._check_gerbers(gerbers, len(layers))
            if not any(
                path.is_file() and path.stat().st_size for path in drills.glob("*.drl")
            ):
                raise RuntimeError("KiCad did not produce an Excellon drill file.")

            def file_export(action, source, name, flags=()):
                """Run one KiCad export into the package and require a nonempty file."""
                target = stage / name
                native.call(*action, *flags, "--output", target, source, output=target)
                if not target.stat().st_size:
                    raise RuntimeError(f"Empty manufacturing file: {name}")

            file_export(
                ("sch", "export", "bom"),
                working.schematic,
                "bom.csv",
                ("--fields", "Reference,Value,Footprint,Manufacturer,MPN,DNP"),
            )
            file_export(
                ("pcb", "export", "pos"),
                working.board,
                "placements.csv",
                ("--format", "csv", "--units", "mm", "--exclude-dnp"),
            )
            file_export(("sch", "export", "pdf"), working.schematic, "schematic.pdf")
            file_export(
                ("pcb", "export", "pdf"),
                working.board,
                "assembly.pdf",
                ("--layers", "F.Fab,B.Fab,F.Silkscreen,B.Silkscreen,Edge.Cuts"),
            )
            snapshot.verify_unchanged()
            manifest = {
                "schema_version": 1,
                "kind": "manufacturing_package",
                "status": "draft" if draft else "design_checked",
                "blockers": blockers,
                "inputs": snapshot.hashes,
                "manufacturing_board_sha256": sha(working.board),
                "toolchain": native.status(),
                "hardware_validation": "pending",
                "production_approved": False,
                "artifacts": {
                    p.relative_to(stage).as_posix(): sha(p)
                    for p in sorted(stage.rglob("*"))
                    if p.is_file()
                },
            }
            write_json(stage / "manifest.json", manifest)
        return manifest


export = ManufacturingExport()
