"""Source-bound jobs shared by the desktop app and the terminal."""

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
from autopcb.policy import routing_policy
from core.export_helpers import sha
from core.kicad_sexpr import quote
from core.paths import ProjectStorage
from core.project import PROFILE_NAME, ProjectPaths, SourceSnapshot, read_profile


class DraftPlacement:
    @staticmethod
    def run(native, paths, scratch, spec, compact, pair_layout, drc_checks, erc_checks):
        trials = [("fixed", None)] if compact is None else [
            ("compact", compact),
            ("catalog_fallback", {**compact, "minimum_mm": spec["size_mm"], "movable_groups": []}),
        ]
        fallback_reason = None
        for mode, outline in trials:
            trial = scratch / mode
            trial.mkdir()
            model = trial / "board.json"
            write_json(model, {**spec, "compact_outline": outline})
            native.worker("generate", paths.board, model, output=paths.board)
            settings = json.loads(paths.settings.read_text(encoding="utf-8-sig"))
            settings.setdefault("board", {}).setdefault("design_settings", {}).setdefault(
                "rule_severities", {}).update(dict.fromkeys(drc_checks, "warning"))
            settings.setdefault("erc", {}).setdefault("rule_severities", {}).update(dict.fromkeys(erc_checks, "warning"))
            paths.settings.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
            normalized = trial / "normalize.json"
            drc = native.report(("pcb", "drc", "--refill-zones", "--save-board", "--schematic-parity"),
                                paths.board, normalized)
            findings = _issues(drc, "violations") + _issues(drc, "schematic_parity")
            if compact is not None and (findings or drc.get("ignored_checks")):
                reason = ", ".join(str(item.get("description", item.get("type"))) for item in findings[:3])
                fallback_reason = "Placement DRC: " + (reason or "ignored checks")
                if mode != "compact":
                    raise ValueError(fallback_reason)
                paths.board.unlink()  # Only the rejected, private generation trial.
                continue
            pair_result = None
            if pair_layout is not None:
                request = trial / "pair-request.json"
                result = trial / "pair-result.json"
                write_json(request, {"pair_layout": pair_layout, "baseline_drc": str(normalized)})
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
                raise ValueError("KiCad could not measure the generated rectangular outline.")
            axes = list(strict_zip(*geometry["outline"][0]))
            lower = tuple(map(min, axes))
            upper = tuple(map(max, axes))
            size = [round(b - a, 6) for a, b in strict_zip(lower, upper)]
            moved = sorted(fp["ref"] for fp in geometry["footprints"] if any(
                abs(fp["at"][axis] - lower[axis] - spec["components"][fp["ref"]]["position"][axis]) > 0.000001
                for axis in (0, 1)))
            receipt = {"mode": mode, "size_mm": size, "area_mm2": round(size[0] * size[1], 6),
                       "maximum_size_mm": spec["size_mm"], "moved_footprints": moved}
            if fallback_reason:
                receipt["fallback_reason"] = fallback_reason
            return receipt, pair_result


def write_json(path: Path, record: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(record, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")


def publish_report(target: Path, record: dict) -> None:
    """Publish a fresh machine result without partially replacing the previous report."""
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=target.parent, prefix=".autopcb-report-", delete=False) as handle:
            staged = Path(handle.name)
            json.dump(record, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        os.replace(staged, target)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


@contextmanager
def _new_directory(target: Path):
    target = target.resolve()
    if target.exists() or not target.parent.is_dir():
        raise ValueError(f"Select a new destination under an existing directory: {target}")
    with tempfile.TemporaryDirectory(prefix=".autopcb-stage-", dir=target.parent) as staging:
        stage = Path(staging) / target.name
        stage.mkdir()
        yield stage
        if target.exists():
            raise FileExistsError(f"Destination appeared during preparation: {target}")
        os.rename(stage, target)


@contextmanager
def isolated(paths: ProjectPaths, snapshot: SourceSnapshot | None = None):
    snapshot = snapshot or SourceSnapshot.capture(paths)
    with tempfile.TemporaryDirectory(prefix="autopcb-native-") as scratch:
        root = Path(scratch)
        for path, expected in snapshot.hashes.items():
            copied = root / path
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(paths.root / path, copied)
            if sha(copied) != expected:
                raise RuntimeError(f"Source changed while preparing the job: {path}")
        snapshot.verify_unchanged()
        yield ProjectPaths(root / paths.entry.name)
        snapshot.verify_unchanged()


def _issues(report: dict, name: str) -> list[dict]:
    values = report.get(name)
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        raise RuntimeError(f"Missing or invalid KiCad report field: {name}")
    return values


def _findings(items: list[dict], family: str) -> list[dict]:
    findings = []
    for item in items:
        points = []
        for child in item.get("items", ()):
            position = child.get("pos", {})
            if all(isinstance(position.get(n), (int, float)) for n in ("x", "y")):
                points.append({"x": position["x"], "y": position["y"],
                               "description": child.get("description", "")})
        findings.append({"check": family, "type": item.get("type", "unknown"),
                         "description": item.get("description", ""), "severity": item.get("severity", "unknown"),
                         "positions_mm": points})
    return findings


def native_checks(native: Native, paths: ProjectPaths, output: Path, *, save_zones: bool = False) -> dict:
    if not paths.board.is_file() or not paths.schematic.is_file():
        raise ValueError("Native verification needs a saved schematic and PCB")
    output.mkdir()
    erc = native.report(("sch", "erc"), paths.schematic, output / "erc.json")
    flags = ["pcb", "drc", "--schematic-parity", "--refill-zones"]
    if save_zones:
        flags.append("--save-board")
    drc = native.report(tuple(flags), paths.board, output / "drc.json")
    findings = []
    for sheet in _issues(erc, "sheets"):
        findings.extend(_findings(_issues(sheet, "violations"), "erc"))
    for field in ("violations", "unconnected_items", "schematic_parity"):
        findings.extend(_findings(_issues(drc, field), field))
    for label, report in (("DRC", drc), ("ERC", erc)):
        ignored = report.get("ignored_checks", [])
        if not isinstance(ignored, list):
            raise RuntimeError(f"Invalid ignored checks in the {label} report")
        findings.extend({"check": label, "type": "ignored_check", "description": str(check),
                         "severity": "blocked", "positions_mm": []} for check in ignored)
    return {"passed": not findings, "findings": findings,
            "open_connections": len(_issues(drc, "unconnected_items"))}


def inspect(native: Native, paths: ProjectPaths) -> dict:
    snapshot = SourceSnapshot.capture(paths)
    with isolated(paths, snapshot) as working:
        output = working.root / "autopcb-geometry.json"
        native.worker("inspect", working.board, output, output=output)
        geometry = json.loads(output.read_text(encoding="utf-8"))
    return {"schema_version": 1, "kind": "inspect", "status": "inspected", "entry": str(paths.entry),
            "inputs": snapshot.hashes, "geometry": geometry, "toolchain": native.status()}


def review_evidence(paths: ProjectPaths, snapshot: SourceSnapshot, reference: dict,
                    storage: ProjectStorage | None = None) -> tuple[bytes | None, str | None]:
    profile = read_profile(paths.root)
    review = profile.pop("electrical_review", None)
    if not isinstance(review, dict) or not isinstance(review.get("file"), str):
        return None, "A source-bound electrical review report is required."
    relative = Path(review["file"])
    if relative.is_absolute() or ".." in relative.parts:
        return None, "Use a relative electrical review file in project data."
    data = (storage or ProjectStorage(paths.root)).data_file(*relative.parts)
    fallback = paths.root / relative
    path = data.resolve() if data.is_file() else fallback.resolve()
    scope = (storage.workspace if storage and storage.workspace and path.is_relative_to(storage.data.resolve())
             else paths.root)
    if not path.is_relative_to(scope) or not path.is_file() or path.stat().st_size > 2_000_000:
        return None, "The electrical review must be a report file in project or workspace data."
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != review.get("sha256"):
        return None, "The electrical review file differs from the approved report."
    try:
        report = json.loads(content)
        required = ("electrical", "reference_copper") if reference["pairs"] else ("electrical",)
        measured = report.get("checks", {})
        excluded = {PROFILE_NAME}
        if path.is_relative_to(paths.root):
            excluded.add(path.relative_to(paths.root).as_posix())
        source_hashes = {key: value for key, value in snapshot.hashes.items() if key not in excluded}
        if (report.get("schema_version") != 1 or report.get("sources") != source_hashes
                or report.get("policy_sha256") != design.digest(profile)
                or not isinstance(report.get("reviewer"), str) or not report["reviewer"].strip()
                or not all(isinstance(measured.get(check), dict)
                           and measured[check].get("status") == "pass"
                           and measured[check].get("measurements")
                           and isinstance(measured[check].get("method"), str)
                           and measured[check]["method"].strip() for check in required)):
            return None, "The electrical report lacks passing measurements for these exact sources and policy."
        if reference["findings"]:
            expected = {finding["id"] for finding in reference["findings"]}
            dispositions = measured["reference_copper"].get("findings")
            if (not isinstance(dispositions, dict) or dispositions.keys() != expected
                    or any(not isinstance(reason, str) or not reason.strip() for reason in dispositions.values())):
                return None, ("Document the disposition of every current reference finding in the source-bound "
                              "electrical review.")
    except (ValueError, AttributeError, TypeError):
        return None, "The electrical review report is malformed."
    if sha(path) != review.get("sha256"):
        return None, "The electrical review changed while checking it."
    return content, None


def _design_checks(native: Native, paths: ProjectPaths, working: ProjectPaths, output: Path,
                   snapshot: SourceSnapshot, storage: ProjectStorage | None, *, save_zones: bool):
    checks = native_checks(native, working, output, save_zones=save_zones)
    report_path = output / "reference.json"
    native.worker("reference", working.entry, report_path, output=report_path)
    reference = json.loads(report_path.read_text(encoding="utf-8"))
    proof, review_blocker = review_evidence(paths, snapshot, reference, storage)
    blockers = [] if checks["passed"] else ["Native DRC, ERC or schematic parity has findings."]
    blockers.extend(reference["blockers"])
    if review_blocker:
        blockers.append(review_blocker)
    return checks, reference, proof, review_blocker, blockers


def verify(native: Native, paths: ProjectPaths, *, storage: ProjectStorage | None = None) -> dict:
    snapshot = SourceSnapshot.capture(paths)
    with isolated(paths, snapshot) as working:
        checks, reference, _, review_blocker, blockers = _design_checks(
            native, paths, working, working.root / "autopcb-native-reports", snapshot, storage,
            save_zones=bool(read_profile(working.root).get("differential_pairs")),
        )
    snapshot.verify_unchanged()
    return {"schema_version": 1, "kind": "verify", "status": "native_passed" if checks["passed"] else "blocked",
            "entry": str(paths.entry), "inputs": snapshot.hashes, "native": checks, "reference_screen": reference,
            "design_checks_passed": not blockers, "blockers": blockers,
            "electrical_review_blocker": review_blocker, "toolchain": native.status(),
            "hardware_validation": "pending", "production_approved": False}


def _remove_new_policy(path: Path, initial: bytes) -> None:
    if path.is_file() and path.read_bytes() == initial:
        path.unlink()


def build(native: Native, proposal: dict, approval: str, output: Path, *, defaults: Path | None = None,
          storage: ProjectStorage | None = None) -> dict:
    body = design.validated(proposal, approval)
    title = output.name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}", title) or title.endswith((" ", ".")):
        raise ValueError("Use a short project folder name with letters, numbers, spaces, dots, hyphens or underscores")
    source, symbols, links = design.schematic(proposal, title)
    layers = ["F.Cu", *[f"In{idx}.Cu" for idx in range(1, body["layers"] - 1)], "B.Cu"]
    critical = ["/" + net for pair in body["differential_pairs"] for net in pair]
    pair_layout = body.get("pair_layout")
    policy = routing_policy(layers, defaults=defaults, excluded=critical, refill=bool(pair_layout))
    final_policy = (storage.config if storage else output / "config") / "autopcb.json"
    if final_policy.exists():
        raise FileExistsError(f"A routing policy already exists: {final_policy}")
    with ExitStack() as rollback:
        with _new_directory(output) as stage:
            paths = ProjectPaths(stage / f"{title}.kicad_pro")
            paths.schematic.write_bytes(source.encode("utf-8"))
            drc_checks = ("missing_courtyard", "track_not_centered_on_via", "tuning_profile_track_geometries",
                          "footprint_filters_mismatch", "footprint_type_mismatch")
            erc_checks = ("single_global_label", "four_way_junction", "simulation_model_issue", "footprint_filter")
            write_json(paths.settings, {
                "meta": {"filename": paths.settings.name, "version": 1},
                "board": {"design_settings": {"rule_severities": dict.fromkeys(drc_checks, "warning")}},
                "erc": {"rule_severities": dict.fromkeys(erc_checks, "warning")},
            })
            lib = stage / "libraries"
            lib.mkdir()
            (lib / "AutoPCB.kicad_sym").write_bytes(symbols.encode("utf-8"))
            (stage / "sym-lib-table").write_text(
                '(sym_lib_table (version 7) (lib (name "AutoPCB")(type "KiCad")'
                '(uri "${KIPRJMOD}/libraries/AutoPCB.kicad_sym")(options "")(descr "AutoPCB generated symbols")))\n',
                encoding="utf-8",
            )
            groups = {component["footprint"].split(":", 1)[0] for component in body["components"].values()}
            table = "\n".join(
                f'(lib (name {quote(group)})(type "KiCad")(uri "${{KICAD10_FOOTPRINT_DIR}}/{group}.pretty")'
                '(options "")(descr "KiCad standard footprint library"))' for group in sorted(groups)
            )
            (stage / "fp-lib-table").write_text(f"(fp_lib_table (version 7)\n{table}\n)\n", encoding="utf-8")
            paths.rules.write_text("(version 1)\n", encoding="utf-8")
            profile = {"schema_version": 1, "entry": paths.settings.name,
                       "proposal": approval, "requirements": body["requirements"], "design": body["design"],
                       "supply_v": body["supply_v"], "copper_layers": body["layers"],
                       "differential_pairs": body["differential_pairs"], "high_speed_reviewed": True,
                       "assumptions": body["assumptions"]}
            if pair_layout is not None:
                profile["reference_net"] = pair_layout["reference_net"]
            write_json(stage / PROFILE_NAME, profile)
            write_json(stage / "design-proposal.json", proposal)
            with tempfile.TemporaryDirectory(prefix="autopcb-netlist-") as scratch:
                scratch = Path(scratch)
                xml = scratch / "schematic.xml"
                native.call("sch", "export", "netlist", "--format", "kicadxml", "--output", xml,
                            paths.schematic, output=xml)
                erc = native.report(("sch", "erc"), paths.schematic, scratch / "erc.json")
                findings = [item for sheet in _issues(erc, "sheets") for item in _issues(sheet, "violations")
                            if item.get("severity") != "warning"]
                if findings:
                    raise RuntimeError("Generated schematic did not pass native ERC: "
                                       + "; ".join(str(item.get("description")) for item in findings[:6]))
                spec = {"netlist": str(xml), "links": links, "footprints": str(native.footprints()),
                        **{key: body[key] for key in ("layers", "size_mm", "components")}}
                placed, pair_result = DraftPlacement.run(native, paths, scratch, spec, body.get("compact_outline"),
                                                    pair_layout, drc_checks, erc_checks)
                if "compact_outline" in body:
                    publish_report(stage / PROFILE_NAME, {**profile, "initial_outline": placed})
            target = final_policy if storage else stage / "config" / final_policy.name
            target.parent.mkdir(parents=True, exist_ok=True)
            write_json(target, policy)
            if storage:
                rollback.callback(_remove_new_policy, target, target.read_bytes())
            # The output context renames the prepared native files on successful exit.
        rollback.pop_all()
    return {"schema_version": 1, "kind": "design_build", "status": "created",
            "project": str(output.resolve() / f"{title}.kicad_pro"), "proposal_id": approval,
            "policy": str(final_policy.resolve()), "pair_layout": pair_result, "outline": placed}


def _check_gerbers(directory: Path, layer_count: int) -> None:
    job_files = list(directory.glob("*.gbrjob"))
    if len(job_files) != 1:
        raise RuntimeError("Expected one fresh KiCad Gerber job file.")
    job = json.loads(job_files[0].read_text(encoding="utf-8-sig"))
    rows = job.get("FilesAttributes", [])
    for row in rows:
        name = row["Path"]
        file = directory / name
        if file.resolve().parent != directory or not file.is_file() or not file.stat().st_size:
            raise RuntimeError(f"Gerber job references a missing or invalid plot: {name}")
    functions = [row["FileFunction"].split(",") for row in rows]
    required = {("SolderMask", "Top"), ("SolderMask", "Bot"), ("SolderPaste", "Top"),
                ("SolderPaste", "Bot"), ("Legend", "Top"), ("Legend", "Bot")}
    if (sorted((item[1] for item in functions if len(item) >= 2 and item[0] == "Copper"))
            != sorted(f"L{index}" for index in range(1, layer_count + 1))
            or sum(item[0] == "Profile" for item in functions) != 1
            or not required <= {tuple(item[:2]) for item in functions}):
        raise RuntimeError("Gerber job does not cover the required board layers.")


def export(native: Native, paths: ProjectPaths, output: Path, *, draft: bool,
           storage: ProjectStorage | None = None) -> dict:
    snapshot = SourceSnapshot.capture(paths)
    with _new_directory(output) as stage, isolated(paths, snapshot) as working:
        evidence = stage / "evidence"
        _, _, proof, _, blockers = _design_checks(native, paths, working, evidence, snapshot, storage, save_zones=True)
        if proof is not None:
            (evidence / "electrical.json").write_bytes(proof)
        if blockers and not draft:
            raise RuntimeError("Manufacturing export blocked. " + " ".join(blockers))
        geometry = working.root / "autopcb-geometry.json"
        native.worker("inspect", working.board, geometry, output=geometry)
        layers = json.loads(geometry.read_text(encoding="utf-8"))["copper_layers"]
        gerbers, drills = stage / "gerbers", stage / "drills"
        gerbers.mkdir()
        drills.mkdir()
        native.call("pcb", "export", "gerbers", "--layers",
                    ",".join([*layers, "F.Mask", "B.Mask", "F.Paste", "B.Paste", "F.Silkscreen", "B.Silkscreen", "Edge.Cuts"]),
                    "--output", gerbers, working.board)
        native.call("pcb", "export", "drill", "--output", drills, working.board)
        _check_gerbers(gerbers, len(layers))
        if not any(path.is_file() and path.stat().st_size for path in drills.glob("*.drl")):
            raise RuntimeError("KiCad did not produce an Excellon drill file.")
        def file_export(action, source, name, flags=()):
            target = stage / name
            native.call(*action, *flags, "--output", target, source, output=target)
            if not target.stat().st_size:
                raise RuntimeError(f"Empty manufacturing file: {name}")
        file_export(("sch", "export", "bom"), working.schematic, "bom.csv",
                    ("--fields", "Reference,Value,Footprint,Manufacturer,MPN,DNP"))
        file_export(("pcb", "export", "pos"), working.board, "placements.csv",
                    ("--format", "csv", "--units", "mm", "--exclude-dnp"))
        file_export(("sch", "export", "pdf"), working.schematic, "schematic.pdf")
        file_export(("pcb", "export", "pdf"), working.board, "assembly.pdf",
                    ("--layers", "F.Fab,B.Fab,F.Silkscreen,B.Silkscreen,Edge.Cuts"))
        snapshot.verify_unchanged()
        manifest = {"schema_version": 1, "kind": "manufacturing_package",
                    "status": "draft" if draft else "design_checked", "blockers": blockers,
                    "inputs": snapshot.hashes, "manufacturing_board_sha256": sha(working.board),
                    "toolchain": native.status(), "hardware_validation": "pending", "production_approved": False,
                    "artifacts": {p.relative_to(stage).as_posix(): sha(p) for p in sorted(stage.rglob("*")) if p.is_file()}}
        write_json(stage / "manifest.json", manifest)
    return manifest
