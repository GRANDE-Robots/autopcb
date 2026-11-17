#!/usr/bin/env python3
"""Dispatch Jessie checks, review tools, and guarded editing utilities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from core.paths import asset_root
from core.console import Console, add_output_arguments
from typing import Iterable, Literal, TextIO

from core.project import ProjectPaths
from review.refresh_gallery import ACTIVE as REVIEW_FILES

SCRIPTS = Path(__file__).resolve().parent.parent
DEFAULT_PROJECT = (
    SCRIPTS.parent.parent
    if SCRIPTS.parent.name.lower() == "autopcb"
    else SCRIPTS.parent
)
DEFAULT_KICAD_PYTHON = Path(r"C:\Program Files\KiCad\10.0\bin\python.exe")


def _has_document_dependency(python: Path) -> bool:
    """Check the actual interpreter used for the document tests."""
    try:
        result = subprocess.run(
            [str(python), "-B", "-c",
             "import importlib.util, sys; sys.exit(importlib.util.find_spec('markdown_it') is None)"],
            cwd=SCRIPTS, env=subprocess_env(), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10, check=False,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def resolve_document_python(requested: Path | None) -> Path:
    """Prefer an explicit runtime, otherwise find a usable local interpreter."""
    if requested is not None:
        if not requested.is_file():
            raise ValueError(f"PDF-test Python runtime not found: {requested}")
        candidates = [requested]
    else:
        candidates = [Path(sys.executable)]
        for name in ("python", "python3"):
            located = shutil.which(name)
            if located:
                candidates.append(Path(located))
    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        if _has_document_dependency(resolved):
            return resolved
    raise ValueError(
        "PDF tests require a Python interpreter with markdown-it-py; "
        "install it or select one with --document-python."
    )


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """Describe one user-selected action without running it."""

    module: str
    runtime: Literal["python", "kicad"]
    mode: Literal[
        "board",
        "schematic",
        "schematic_check",
        "project",
        "review",
        "check_nets",
        "pcb_nets",
        "campaign",
        "none",
    ]

    def __post_init__(self) -> None:
        if self.runtime not in {"python", "kicad"}:
            raise ValueError(f"Unsupported action runtime: {self.runtime}")
        if self.mode not in {
            "board",
            "schematic",
            "schematic_check",
            "project",
            "review",
            "check_nets",
            "pcb_nets",
            "campaign",
            "none",
        }:
            raise ValueError(f"Unsupported action mode: {self.mode}")


# Each operation is one small module. Arguments after the action go directly
# to that module, so its own help and safety checks remain available.
ACTIONS = {
    "campaign": {
        name: ActionSpec("core.campaign_cli", "kicad", "campaign")
        for name in (
            "new",
            "show",
            "add-move",
            "add-grid",
            "add-property",
            "add-expression",
            "preview",
            "apply",
        )
    },
    "library": {
        name: ActionSpec("core.library_campaign_cli", "kicad", "campaign")
        for name in ("preview", "apply")
    },
    "check": {
        "code": ActionSpec("verification.audit_code_health", "python", "project"),
        "libraries": ActionSpec(
            "verification.audit_kicad_libraries", "python", "project"
        ),
        "object": ActionSpec("verification.inspect_kicad_object", "python", "project"),
        "tools": ActionSpec("verification.inspect_kicad_toolchain", "kicad", "project"),
        "power": ActionSpec("verification.audit_pcb_power_terminals", "kicad", "board"),
        "placement": ActionSpec("verification.audit_pcb_courtyards", "kicad", "board"),
        "islands": ActionSpec("verification.audit_pcb_net_islands", "kicad", "board"),
        "blocks": ActionSpec(
            "verification.audit_schematic_blocks", "python", "schematic"
        ),
        "nets": ActionSpec("pcb.reconcile_named_nets", "kicad", "check_nets"),
        "routing": ActionSpec("verification.audit_pcb_routing_style", "kicad", "board"),
        "conflicts": ActionSpec("verification.inspect_pcb_copper", "kicad", "board"),
        "erc": ActionSpec("verification.audit_schematic_erc", "python", "project"),
        "board": ActionSpec("verification.audit_pcb_manufacturing", "kicad", "project"),
        "board-paths": ActionSpec("verification.audit_pcb_paths", "kicad", "project"),
        "release": ActionSpec(
            "verification.audit_release_consistency", "python", "project"
        ),
    },
    "review": {
        "refresh": ActionSpec("review.refresh_gallery", "python", "review"),
        "progress": ActionSpec("review.routing_progress", "kicad", "review"),
        "render": ActionSpec("review.render_views", "kicad", "project"),
        "raster": ActionSpec("review.rasterize_gallery", "python", "none"),
        "standalone": ActionSpec("review.build_standalone_gallery", "python", "none"),
        "package": ActionSpec("review.package_review", "python", "project"),
    },
    "schematic": {
        "style": ActionSpec(
            "sch.normalize_presentation", "python", "schematic_check"
        ),
        "blocks": ActionSpec(
            "sch.standardize_block_symbols", "python", "schematic_check"
        ),
        "names": ActionSpec(
            "sch.normalize_pin_names", "python", "schematic_check"
        ),
        "sections": ActionSpec(
            "sch.layout_sections", "python", "schematic_check"
        ),
        "labels-deduplicate": ActionSpec(
            "sch.deduplicate_labels", "python", "schematic_check"
        ),
        "labels-orient": ActionSpec(
            "sch.orient_passive_labels", "python", "schematic_check"
        ),
        "labels-restore": ActionSpec(
            "sch.restore_endpoint_labels", "python", "schematic_check"
        ),
        "stubs": ActionSpec("sch.prune_dead_stubs", "python", "schematic_check"),
        "no-connects": ActionSpec(
            "sch.repair_no_connects", "python", "schematic_check"
        ),
        "collisions": ActionSpec(
            "sch.resolve_label_collisions", "python", "schematic_check"
        ),
        "format": ActionSpec(
            "sch.normalize_indentation", "python", "schematic_check"
        ),
        "sync-libraries": ActionSpec(
            "sch.sync_symbol_libraries", "python", "schematic_check"
        ),
        "sync-one": ActionSpec("sch.sync_single_symbol", "python", "schematic"),
    },
    "pcb": {
        "branch-widths": ActionSpec("pcb.branch_widths", "kicad", "project"),
        "repair-widths": ActionSpec("pcb.width_repair", "kicad", "project"),
        "clean-silkscreen": ActionSpec("pcb.silkscreen", "kicad", "project"),
        "repair-drc": ActionSpec("pcb.drc_cleanup", "kicad", "project"),
        "nets": ActionSpec("pcb.reconcile_named_nets", "kicad", "pcb_nets"),
        "move": ActionSpec("pcb.drag_routed_footprints", "kicad", "board"),
        "replace-connector": ActionSpec(
            "pcb.replace_connector_footprint", "kicad", "board"
        ),
        "plan-route": ActionSpec("pcb.route_cli", "kicad", "project"),
        "plan-batch": ActionSpec("pcb.route_batch", "kicad", "project"),
        "plan-nets": ActionSpec("pcb.net_routing.cli", "kicad", "project"),
        "autoroute": ActionSpec("pcb.net_routing.autoroute_cli", "kicad", "project"),
        "checkpoints": ActionSpec("pcb.checkpoints", "kicad", "project"),
        "move-text": ActionSpec("pcb.footprint_text", "kicad", "project"),
        "optimize": ActionSpec("pcb.joint_optimizer", "kicad", "project"),
        "freeroute-preview": ActionSpec("pcb.freeroute_preview", "kicad", "project"),
        "freeroute-retain": ActionSpec("pcb.net_routing.external", "kicad", "project"),
        "freeroute-fast": ActionSpec("pcb.net_routing.fast", "kicad", "project"),
        "freeroute-rework": ActionSpec("pcb.net_routing.fast_rework", "kicad", "project"),
        "sync-net-aliases": ActionSpec("pcb.net_routing.alias_reconcile", "kicad", "project"),
        "save-working-preview": ActionSpec("pcb.net_routing.working_save", "kicad", "project"),
        "zone-stitch": ActionSpec("pcb.net_routing.zone_stitch", "kicad", "project"),
        "via-prune": ActionSpec("pcb.net_routing.via_prune", "kicad", "project"),
        "copper-prune": ActionSpec("pcb.net_routing.via_prune", "kicad", "project"),
    },
    "docs": {
        "build-pdf": ActionSpec("utils.build_pdf", "python", "project"),
    },
    "release": {
        "build": ActionSpec("release.generate_factory_review", "kicad", "project"),
        "stage-inputs": ActionSpec("release.stage_release_inputs", "python", "project"),
        "technical": ActionSpec(
            "release.build_technical_attachment", "python", "project"
        ),
    },
}


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def matching_hashes(root: Path, values: object) -> bool:
    """Accept only source-bound relative paths with matching file hashes."""
    if not isinstance(values, dict):
        return False
    for name, expected in values.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            return False
        candidate = (root / name).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return False
        if sha(candidate).lower() != expected.lower():
            return False
    return True


def show_status(project: Path, *, json_mode: bool = False, verbose: bool = False) -> int:
    paths = ProjectPaths(project)
    project = paths.root
    sources = paths.sources
    if not all(path.is_file() for path in sources):
        print(
            f"Project is missing one or more native v4.1 files: {project}",
            file=sys.stderr,
        )
        return 2
    hashes = {path.name: sha(path) for path in sources}
    manifest = asset_root(project) / "design" / "release_inputs" / "INPUT_MANIFEST.json"
    mismatches = []
    manifest_status = "missing"
    if manifest.is_file():
        try:
            record = json.loads(manifest.read_text(encoding="utf-8-sig"))
            manifest_status = record.get("status", "not specified")
            for name, expected in record["files"].items():
                if not matching_hashes(manifest.parent.resolve(), {name: expected}):
                    mismatches.append(name)
        except (OSError, ValueError, KeyError, TypeError) as error:
            manifest_status = f"unreadable: {error}"
    review = paths.review
    view_status = "missing"
    identity = review / "source.json"
    if identity.is_file():
        try:
            record = json.loads(identity.read_text(encoding="utf-8-sig"))
            expected = record.get("sha256", {})
            dependencies = record.get("dependency_sha256", {})
            files_present = all((review / name).is_file() for name in REVIEW_FILES)
            matching_sources = (
                isinstance(expected, dict)
                and set(expected) == {paths.schematic.name, paths.board.name}
                and matching_hashes(project, expected)
            )
            matching_dependencies = (
                isinstance(dependencies, dict)
                and {
                    (asset_root(project) / "scripts/core/export_helpers.py")
                    .relative_to(project)
                    .as_posix(),
                    (asset_root(project) / "scripts/review/render_views.py")
                    .relative_to(project)
                    .as_posix(),
                    (asset_root(project) / "scripts/review/refresh_gallery.py")
                    .relative_to(project)
                    .as_posix(),
                    (asset_root(project) / "scripts/review/rasterize_gallery.py")
                    .relative_to(project)
                    .as_posix(),
                    (asset_root(project) / "scripts/review/build_standalone_gallery.py")
                    .relative_to(project)
                    .as_posix(),
                }
                <= dependencies.keys()
                and matching_hashes(project, dependencies)
            )
            view_status = (
                "current source hashes"
                if files_present and matching_sources and matching_dependencies
                else "missing or stale"
            )
        except (OSError, ValueError, TypeError):
            view_status = "unreadable"
    def display(console: Console, record: dict) -> None:
        console.heading("AutoPCB | Project status")
        console.rows([
            ("Saved KiCad files", "Present"),
            ("Review files", "Current" if record["review_views"] == "current source hashes" else record["review_views"]),
            ("Manufacturing manifest", record["release_manifest_status"]),
            ("Release input mismatches", len(record["release_input_mismatches"])),
        ])
        if console.verbose:
            console.line("Project: " + record["project"])
            for name in record["release_input_mismatches"]:
                console.line(name, prefix="  - ")
        console.line(record["note"])

    Console(json_mode=json_mode, verbose=verbose).result(
            {
                "project": str(project),
                "source_sha256": hashes,
                "review_views": view_status,
                "release_manifest_status": manifest_status,
                "release_input_mismatches": mismatches,
                "note": (
                    "Source and file status only; electrical, physical, and "
                    "factory readiness are not established."
                ),
            },
        display,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Jessie v4.1 project tools. Use an action followed by --help for its arguments."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples (from scripts):
  python run.py status
  python run.py check power
  python run.py check nets
  python run.py review refresh
  python run.py campaign new --name "Test point grid"
  python run.py campaign preview 20261027_120000.json --diff
  python run.py pcb plan-route --config path/to/route.json
  python run.py pcb plan-batch --config path/to/routes.json --validate
  python run.py pcb plan-nets --config path/to/nets.json --validate
  python run.py pcb freeroute-preview --passes 1 --timeout 600
  python run.py test

Use an action followed by --help for its arguments, for example:
  python run.py pcb move --help

The current v4.1 source paths are supplied automatically. PCB operations use
KiCad's bundled Python; set KICAD_PYTHON or pass --kicad-python if needed.
Review rasterization needs Pillow and either pypdfium2 or Poppler.
Campaign JSON files live in config/campaigns. Applying one requires the source
and campaign hashes from its preview and refuses to overwrite an open KiCad document.
Library campaigns use library preview FILE --document PATH --diff; applying one
requires the preview hash. Validation uses temporary KiCad exports.
Freerouting previews use a temporary saved-file copy and are never applied automatically.

Assistant-run schematic changes are restricted to footprint or 3D assignments.
Presentation helpers default to --check. Review refresh updates the current gallery and leaves the
separately reviewed DELIVERY_IDENTITY.json untouched. Factory inputs remain
on hold until reviewed; a status check or image is not a fabrication approval.
""",
    )
    parser.add_argument(
        "--project",
        type=Path,
        default=DEFAULT_PROJECT,
        help="native KiCad project directory (defaults to the parent of autopcb)",
    )
    parser.add_argument(
        "--kicad-python",
        type=Path,
        default=Path(os.environ.get("KICAD_PYTHON", str(DEFAULT_KICAD_PYTHON))),
        help="KiCad bundled Python, used for PCB-dependent operations",
    )
    groups = parser.add_subparsers(dest="group", required=True)
    status = groups.add_parser("status", help="show saved-source and release-input status")
    add_output_arguments(status)
    test = groups.add_parser("test", help="run the isolated script regression tests")
    test.add_argument("-v", "--verbose", action="store_true", help="list each test")
    test.add_argument(
        "--all-warnings", action="store_true", help="show every repeated KiCad warning"
    )
    test.add_argument(
        "--document-python",
        type=Path,
        help="Python with markdown-it-py for PDF tests (auto-detected by default)",
    )
    for group, entries in ACTIONS.items():
        if group == "campaign":
            group_parser = groups.add_parser(
                group,
                help="configure and preview source-bound KiCad edits",
                formatter_class=argparse.RawDescriptionHelpFormatter,
                description="Timestamped JSON campaigns live in config/campaigns.",
                epilog=(
                    'Example: create one with campaign new --name "Test points";\n'
                    "use campaign add-move or add-grid, then campaign preview FILE --diff.\n"
                    "To apply, provide the document and both hashes from the preview:\n"
                    "  campaign apply FILE --document pcb --expected-sha256 SOURCE_HASH "
                    "--expected-campaign-sha256 CAMPAIGN_HASH\n"
                    "Each apply edits one document; position edits do not drag copper.\n"
                    "Use an action followed by --help for all its options."
                ),
            )
        else:
            aliases = ["sch"] if group == "schematic" else []
            group_parser = groups.add_parser(group, aliases=aliases, help=f"{group} actions")
        actions = group_parser.add_subparsers(dest="action", required=True)
        for name in entries:
            actions.add_parser(name, add_help=False)
    return parser


def _caller_relative_configs(arguments: list[str], caller: Path) -> list[str]:
    """Preserve the terminal's relative paths when a module runs elsewhere."""
    result = list(arguments)
    for index, argument in enumerate(result):
        if argument == "--config" and index + 1 < len(result):
            value = result[index + 1]
            if value and not value.startswith("-") and not Path(value).is_absolute():
                result[index + 1] = str(caller / value)
        elif argument.startswith("--config="):
            value = argument.partition("=")[2]
            if value and not Path(value).is_absolute():
                result[index] = f"--config={caller / value}"
    return result


def command_for(args: argparse.Namespace, forwarded: list[str]) -> list[str]:
    group = "schematic" if args.group == "sch" else args.group
    action = ACTIONS[group][args.action]
    module, runtime, mode = action.module, action.runtime, action.mode
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    selected_project = args.project
    if mode in ("project", "review", "campaign"):
        remaining = []
        index = 0
        while index < len(forwarded):
            item = forwarded[index]
            if (item == "--project" and index + 1 < len(forwarded)
                    and not forwarded[index + 1].startswith("-")):
                selected_project = Path(forwarded[index + 1])
                index += 2
                continue
            if item.startswith("--project=") and item.partition("=")[2]:
                selected_project = Path(item.partition("=")[2])
                index += 1
                continue
            remaining.append(item)
            index += 1
        forwarded = remaining
    if not selected_project.is_absolute():
        selected_project = Path.cwd() / selected_project
    paths = ProjectPaths(selected_project)
    project = paths.root
    board = str(paths.board)
    schematic = str(paths.schematic)
    if args.group == "pcb" and args.action in {
        "plan-route",
        "plan-batch",
        "plan-nets",
        "optimize",
    }:
        forwarded = _caller_relative_configs(forwarded, Path.cwd())
    if args.group == "check" and "--apply" in forwarded:
        raise ValueError(
            "Check actions are read-only; use the corresponding pcb or "
            "schematic action for an explicit edit."
        )
    if mode == "board":
        defaults = [board]
    elif mode in ("schematic", "schematic_check"):
        defaults = [schematic]
        if mode == "schematic_check" and not any(
            x in forwarded
            for x in ("--check", "--apply", "--remove", "--output", "--help", "-h")
        ):
            defaults.append("--check")
    elif mode == "project":
        defaults = ["--project", str(project)]
    elif mode == "review":
        defaults = [
            "--project",
            str(project),
            "--review",
            str(paths.review),
            "--kicad-python",
            str(args.kicad_python),
        ]
    elif mode == "campaign":
        defaults = ["--project", str(project), args.action]
    elif mode in ("check_nets", "pcb_nets"):
        defaults = ["--board", board, "--schematic", schematic]
        if mode == "check_nets" or not any(
            x in forwarded for x in ("--dry-run", "--apply", "--help", "-h")
        ):
            defaults.append("--dry-run")
    elif mode == "none":
        defaults = []
    else:
        raise ValueError(f"Unsupported action mode: {mode}")
    executable = str(args.kicad_python if runtime == "kicad" else Path(sys.executable))
    return [executable, "-B", "-m", module, *defaults, *forwarded]


def subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    old_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(SCRIPTS) + (os.pathsep + old_path if old_path else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _relay_test_output(lines: Iterable[str], output: TextIO) -> None:
    """Retain test results and warnings while summarizing one repeated KiCad message."""
    duplicate_count = 0
    for line in lines:
        repeated_warning = line.startswith(
            "Adding duplicate image handler for '"
        ) and line.rstrip("\r\n").endswith("'")
        if repeated_warning:
            duplicate_count += 1
            if duplicate_count > 1:
                continue
        output.write(line)
        output.flush()
    if duplicate_count > 1:
        output.write(
            f"KiCad emitted {duplicate_count - 1} further duplicate image-handler messages.\n"
        )
        output.flush()


def run_test_suite(command: list[str]) -> int:
    """Stream regression results without retaining or repeating KiCad's handler noise."""
    with subprocess.Popen(
        command,
        cwd=SCRIPTS,
        env=subprocess_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    ) as process:
        assert process.stdout is not None
        _relay_test_output(process.stdout, sys.stdout)
        return process.wait()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, rest = parser.parse_known_args(argv)
    if args.group == "status":
        if rest:
            parser.error("status takes no additional arguments")
        return show_status(args.project.resolve(), json_mode=args.json, verbose=args.verbose)
    if args.group == "test":
        if rest:
            parser.error("test takes no additional arguments")
        command = [
            str(args.kicad_python),
            "-B",
            "-m",
            "unittest",
            "discover",
            "-s",
            str(SCRIPTS / "verification"),
            "-p",
            "test_*.py",
            "-v" if args.verbose else "-q",
        ]
    else:
        try:
            command = command_for(args, rest)
        except ValueError as error:
            parser.error(str(error))
    if not Path(command[0]).is_file():
        print(f"Python runtime not found: {command[0]}", file=sys.stderr)
        return 2
    if args.group == "test":

        def run_tests(value: list[str]) -> int:
            if args.all_warnings:
                return subprocess.run(
                    value, cwd=SCRIPTS, env=subprocess_env()
                ).returncode
            return run_test_suite(value)

        try:
            document_python = resolve_document_python(args.document_python)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
        status = run_tests(command)
        if status:
            return status
        document_check = (
            "import importlib.util, sys, unittest\n"
            "if importlib.util.find_spec('markdown_it') is None:\n"
            "    print('PDF tests require markdown-it-py in the document Python.', file=sys.stderr)\n"
            "    raise SystemExit(2)\n"
            "unittest.main(module=None, argv=[sys.argv[0], 'verification.test_pdf_builder', sys.argv[1]])\n"
        )
        print("Running PDF tests with the document Python.", flush=True)
        return run_tests(
            [
                str(document_python),
                "-B",
                "-c",
                document_check,
                "-v" if args.verbose else "-q",
            ]
        )
    try:
        return subprocess.run(command, cwd=SCRIPTS, env=subprocess_env()).returncode
    except KeyboardInterrupt:
        print("AutoPCB stopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
