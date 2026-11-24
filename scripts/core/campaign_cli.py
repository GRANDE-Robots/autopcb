"""campaign_cli.py: create, preview, and apply source-bound KiCad edit campaigns.

Examples:
  python run.py campaign new --name "Test point grid"
  python run.py campaign add-grid 20261027_120000.json --document pcb --prefix TP --count 24 --rows 4 --origin 0 0 --spacing 2.5 2.5
  python run.py campaign preview 20261027_120000.json --diff
  python run.py campaign apply 20261027_120000.json --document pcb --expected-sha256 SOURCE_HASH --expected-campaign-sha256 CAMPAIGN_HASH

Position edits change only the object's (at ...) expression; attached tracks
and vias do not move. Raw expression edits are powerful and do not substitute
for electrical, connectivity, DRC, or physical checks.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from core.kicad_campaign import CampaignFile, CampaignPlanner, Plan, digest
from core.paths import library_directories, library_root
from core.pcb_io import BoardSaveGuard
from core.project import ProjectPaths
from core.toolchain import kicad_cli


class CampaignArguments:
    """Reads the campaign command line."""

    @staticmethod
    def add_target(command: argparse.ArgumentParser) -> None:
        """Add the document, kind, reference and UUID options that name one object."""
        command.add_argument("campaign")
        command.add_argument(
            "--document",
            required=True,
            help="pcb, schematic, or a project-relative KiCad file",
        )
        command.add_argument(
            "--kind", help="KiCad object type; defaults to footprint or symbol"
        )
        command.add_argument("--reference", help="placed object reference, such as TP1")
        command.add_argument("--uuid", help="exact UUID, including for nested objects")

    @classmethod
    def parser(cls) -> argparse.ArgumentParser:
        """The argument parser with one subcommand per campaign action."""
        parser = argparse.ArgumentParser(
            description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
        )
        parser.add_argument("--project", type=Path, required=True)
        actions = parser.add_subparsers(dest="action", required=True)
        new = actions.add_parser("new", help="create a timestamped JSON campaign")
        new.add_argument("--name", required=True, help="human-readable campaign name")
        show = actions.add_parser("show", help="show a campaign")
        show.add_argument("campaign")
        move = actions.add_parser(
            "add-move", help="add an object position to a campaign"
        )
        cls.add_target(move)
        move.add_argument(
            "--xy", type=float, nargs=2, required=True, metavar=("X", "Y")
        )
        move.add_argument(
            "--rotation",
            type=float,
            help="degrees; PCB footprint rotations require pcb move",
        )
        grid = actions.add_parser("add-grid", help="add a grid of object positions")
        grid.add_argument("campaign")
        grid.add_argument("--document", required=True)
        grid.add_argument("--kind", help="object type; defaults to footprint or symbol")
        grid.add_argument(
            "--prefix", required=True, help="reference prefix, such as TP"
        )
        grid.add_argument("--first", type=int, default=1)
        grid.add_argument("--count", type=int, required=True)
        grid.add_argument("--rows", type=int, required=True, help="number of grid rows")
        grid.add_argument("--order", choices=("column", "row"), default="column")
        grid.add_argument(
            "--origin", type=float, nargs=2, required=True, metavar=("X", "Y")
        )
        grid.add_argument(
            "--spacing", type=float, nargs=2, required=True, metavar=("DX", "DY")
        )
        prop = actions.add_parser(
            "add-property", help="add a named string-property edit"
        )
        cls.add_target(prop)
        prop.add_argument("--name", required=True)
        prop.add_argument("--value", required=True)
        raw = actions.add_parser(
            "add-expression",
            help="add a raw expression replacement, insertion, or removal",
        )
        cls.add_target(raw)
        raw.add_argument(
            "--operation", choices=("replace", "insert", "remove"), required=True
        )
        raw.add_argument(
            "--path",
            default="[]",
            help='JSON array of child names/selectors, e.g. ["at"]',
        )
        raw.add_argument(
            "--expression", help="one complete S-expression for replace or insert"
        )
        preview = actions.add_parser(
            "preview", help="plan changes without writing designs"
        )
        preview.add_argument("campaign")
        preview.add_argument(
            "--diff", action="store_true", help="show a unified text diff"
        )
        apply = actions.add_parser("apply", help="apply one document from a campaign")
        apply.add_argument("campaign")
        apply.add_argument(
            "--document",
            required=True,
            help="document to apply; one file per invocation",
        )
        apply.add_argument(
            "--expected-sha256", required=True, help="source hash printed by preview"
        )
        apply.add_argument(
            "--expected-campaign-sha256",
            required=True,
            help="campaign hash printed by preview",
        )
        return parser

    @staticmethod
    def kind(document: str, kind: str | None) -> str:
        """The object kind: as given, else ``symbol`` for a schematic and ``footprint`` otherwise."""
        if kind:
            return kind
        return (
            "symbol"
            if document == "schematic" or document.endswith(".kicad_sch")
            else "footprint"
        )

    @classmethod
    def target(cls, args: argparse.Namespace) -> dict[str, str]:
        """The campaign target an ``add-*`` command names.

        Outputs:
            ``kind`` with ``reference`` and ``uuid`` when given. Raises
            ValueError when neither is given for a kind other than ``root``.
        """
        target = {"kind": cls.kind(args.document, args.kind)}
        if args.reference:
            target["reference"] = args.reference
        if args.uuid:
            target["uuid"] = args.uuid
        if not args.reference and not args.uuid and target["kind"] != "root":
            raise ValueError("Specify a reference or UUID, or select kind 'root'")
        return target


class CampaignApplier:
    """Publishes one planned file without overwriting newer edits."""

    @staticmethod
    def groups(
        planner: CampaignPlanner, data: dict[str, Any]
    ) -> dict[Path, tuple[str, list[dict[str, Any]]]]:
        """Group a campaign's changes by the design file they edit."""
        groups: dict[Path, tuple[str, list[dict[str, Any]]]] = {}
        for change in data["changes"]:
            document = change.get("document")
            path = planner.document_path(document)
            if path not in groups:
                groups[path] = (document, [])
            groups[path][1].append(change)
        return groups

    @staticmethod
    def summary(plan: Plan) -> dict[str, Any]:
        """The file, both hashes and the edit count of a plan."""
        return {
            "file": str(plan.source),
            "source_sha256": plan.source_sha256,
            "result_sha256": plan.result_sha256,
            "edits": len(plan.edits),
        }

    @staticmethod
    def dependencies(project: Path, source: Path) -> list[Path]:
        """The project files, libraries and symbols an edit to ``source`` depends on."""
        candidates = list(ProjectPaths(source).audit_inputs())
        candidates.extend(
            path
            for directory in library_directories(project)
            for pattern in ("*.kicad_sym", "*.kicad_mod")
            for path in directory.rglob(pattern)
            if path.is_file()
        )
        return sorted(
            {
                path.resolve()
                for path in candidates
                if path.is_file() and path.resolve() != source
            }
        )

    @staticmethod
    def schematic_validation(plan: Plan, project: Path) -> None:
        """Have KiCad export a netlist of the candidate schematic in a temporary copy.

        Outputs: None. Raises RuntimeError, with KiCad's message, when it cannot.
        """
        cli = kicad_cli()
        with tempfile.TemporaryDirectory(prefix="jessie-campaign-sch-") as directory:
            temporary = Path(directory)
            source = temporary / plan.source.name
            source.write_bytes(plan.after)
            for name in ("sym-lib-table", plan.source.with_suffix(".kicad_pro").name):
                existing = project / name
                if existing.is_file():
                    shutil.copy2(existing, temporary / name)
            library = library_root(project)
            if library.is_dir():
                for original in library.rglob("*.kicad_sym"):
                    if original.is_file():
                        destination = temporary / original.relative_to(project)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(original, destination)
            result = subprocess.run(
                [
                    str(cli),
                    "sch",
                    "export",
                    "netlist",
                    "--output",
                    str(temporary / "netlist.net"),
                    str(source),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if result.returncode != 0 or not (temporary / "netlist.net").is_file():
                detail = (result.stderr or result.stdout).strip()
                raise RuntimeError(
                    f"KiCad could not export the candidate schematic netlist: {detail}"
                )

    @classmethod
    def write_schematic(
        cls, plan: Plan, project: Path, dependencies: list[Path]
    ) -> str:
        """Validate and write a planned schematic through an atomic replacement.

        Outputs:
            The SHA-256 of the saved file. Raises RuntimeError when the file or
            a dependency changed after preview, KiCad holds a lock or rejects
            the candidate, or the staged or saved file fails readback.
        """
        fingerprints = {
            path: digest(path.read_bytes()) for path in [plan.source, *dependencies]
        }
        if fingerprints[plan.source] != plan.source_sha256:
            raise RuntimeError("Schematic changed after preview")

        def verify() -> None:
            """Raise RuntimeError if a lock appeared or any recorded file changed."""
            lock = plan.source.with_name(f"~{plan.source.name}.lck")
            if lock.exists():
                raise RuntimeError(f"KiCad schematic lock present: {lock}")
            for path, expected in fingerprints.items():
                if not path.is_file() or digest(path.read_bytes()) != expected:
                    raise RuntimeError(
                        f"Campaign input changed; refusing to save: {path}"
                    )

        verify()
        cls.schematic_validation(plan, project)
        verify()
        descriptor, staged_name = tempfile.mkstemp(
            prefix=".campaign-write-", suffix=".tmp", dir=plan.source.parent
        )
        staged = Path(staged_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(plan.after)
                handle.flush()
                os.fsync(handle.fileno())
            if staged.read_bytes() != plan.after:
                raise RuntimeError("Staged schematic failed readback")
            verify()
            os.replace(staged, plan.source)
        finally:
            if staged.exists():
                staged.unlink()
        if plan.source.read_bytes() != plan.after:
            raise RuntimeError("Saved schematic failed readback")
        return plan.result_sha256

    @classmethod
    def apply(cls, plan: Plan, project: Path, expected: str) -> dict[str, Any]:
        """Validate and atomically publish one file without overwriting newer edits.

        Inputs:
            plan: The previewed plan.
            project: Project folder.
            expected: The source SHA-256 the preview printed.
        Outputs:
            A summary with ``status`` ``unchanged`` or ``applied``, the saved
            hash and the path of the preimage checkpoint. Raises ValueError for
            a malformed hash and RuntimeError when the source differs from the
            preview, KiCad holds a lock, or any save guard fails.
        """
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            raise ValueError("Expected source hash must be a full SHA-256")
        if plan.source_sha256 != expected.lower():
            raise RuntimeError(
                "Source hash differs from preview; preview the current file again"
            )
        if not plan.edits:
            return {**cls.summary(plan), "status": "unchanged"}
        project = project.resolve()
        dependencies = cls.dependencies(project, plan.source)
        if plan.source.suffix.lower() == ".kicad_pcb":
            guard = BoardSaveGuard.capture(plan.source, expected, dependencies)
            guard.verify_editor_unlocked()
        else:
            if plan.source.with_name(f"~{plan.source.name}.lck").exists():
                raise RuntimeError(
                    "KiCad schematic lock present; refusing to overwrite it"
                )
        # Keep a recoverable exact preimage outside the project, not an extra PCB
        # snapshot next to the user's design files.
        checkpoint = (
            Path(tempfile.mkdtemp(prefix="jessie-campaign-before-")) / plan.source.name
        )
        checkpoint.write_bytes(plan.before)
        if plan.source.suffix.lower() == ".kicad_pcb":
            import pcbnew

            def save_text(path: str, _board: object) -> bool:
                """Write the planned text as the board file."""
                Path(path).write_bytes(plan.after)
                return True

            result = guard.save(None, plan.source, save_text, pcbnew.LoadBoard)
        else:
            result = cls.write_schematic(plan, project, dependencies)
        return {
            **cls.summary(plan),
            "status": "applied",
            "saved_sha256": result,
            "checkpoint": str(checkpoint),
        }


class CampaignCommand:
    """Command-line entry: create, show, extend, preview and apply campaigns."""

    @staticmethod
    def add(campaigns: CampaignFile, args: argparse.Namespace) -> None:
        """Append an ``add-grid``, ``add-move``, ``add-property`` or ``add-expression`` change and print it."""
        if args.action == "add-grid":
            changes = campaigns.grid(
                args.document,
                CampaignArguments.kind(args.document, args.kind),
                args.prefix,
                args.first,
                args.count,
                args.rows,
                tuple(args.origin),
                tuple(args.spacing),
                args.order,
            )
            path = campaigns.append(args.campaign, changes)
            print(json.dumps({"campaign": str(path), "changes_added": len(changes)}))
            return
        target = CampaignArguments.target(args)
        change: dict[str, Any] = {"document": args.document, "target": target}
        if args.action == "add-move":
            change.update({"op": "set_at", "x": args.xy[0], "y": args.xy[1]})
            if args.rotation is not None:
                change["rotation"] = args.rotation
        elif args.action == "add-property":
            change.update(
                {"op": "set_property", "name": args.name, "value": args.value}
            )
        else:
            path = json.loads(args.path)
            if not isinstance(path, list):
                raise ValueError("Expression path must be a JSON array")
            if args.operation != "remove" and not args.expression:
                raise ValueError("Replace and insert require --expression")
            change.update({"op": args.operation, "path": path})
            if args.expression:
                change["expression"] = args.expression
        path = campaigns.append(args.campaign, [change])
        print(json.dumps({"campaign": str(path), "changes_added": 1}))

    @staticmethod
    def preview(
        args: argparse.Namespace,
        planner: CampaignPlanner,
        groups: dict[Path, tuple[str, list[dict[str, Any]]]],
        campaign_bytes: bytes,
    ) -> None:
        """Plan every document and print the summaries, and the text diffs with ``--diff``."""
        plans = [
            planner.plan(document, changes) for document, changes in groups.values()
        ]
        print(
            json.dumps(
                {
                    "status": "preview",
                    "campaign_sha256": digest(campaign_bytes),
                    "sources": [CampaignApplier.summary(plan) for plan in plans],
                },
                indent=2,
            )
        )
        if args.diff:
            for plan in plans:
                old = plan.before.decode("utf-8").splitlines(keepends=True)
                new = plan.after.decode("utf-8").splitlines(keepends=True)
                print(
                    "".join(
                        difflib.unified_diff(
                            old,
                            new,
                            fromfile=str(plan.source),
                            tofile="campaign candidate",
                        )
                    )
                )

    @staticmethod
    def apply(
        args: argparse.Namespace,
        project: Path,
        planner: CampaignPlanner,
        groups: dict[Path, tuple[str, list[dict[str, Any]]]],
        data: dict[str, Any],
        campaign_path: Path,
        campaign_bytes: bytes,
    ) -> None:
        """Apply one document of a previewed campaign and print the result.

        Raises ValueError for a malformed campaign hash or a document with no
        changes, and RuntimeError when the campaign changed after preview or
        its routing validation evidence does not hold.
        """
        campaign_hash = args.expected_campaign_sha256.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", campaign_hash):
            raise ValueError("Expected campaign hash must be a full SHA-256")
        if (
            digest(campaign_bytes) != campaign_hash
            or digest(campaign_path.read_bytes()) != campaign_hash
        ):
            raise RuntimeError("Campaign changed after preview; preview it again")
        selected = planner.document_path(args.document)
        if selected not in groups:
            raise ValueError("The selected document has no changes in this campaign")
        document, changes = groups[selected]
        plan = planner.plan(document, changes)
        if "routing_validation" in data:
            if plan.source.suffix.lower() != ".kicad_pcb":
                raise ValueError("Routing validation applies only to PCB campaigns")
            from pcb.route_validation import verify_validation_evidence

            verify_validation_evidence(
                plan.source, data["routing_validation"], plan.result_sha256
            )
        print(
            json.dumps(
                CampaignApplier.apply(plan, project, args.expected_sha256), indent=2
            )
        )

    def __call__(self, argv: list[str] | None = None) -> int:
        """Run one campaign action.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0, or 2 after printing the failure to standard error.
        """
        parser = CampaignArguments.parser()
        args = parser.parse_args(argv)
        project = args.project.resolve()
        campaigns = CampaignFile(project)
        try:
            if args.action == "new":
                print(campaigns.create(args.name))
                return 0
            if args.action == "show":
                _, data, _ = campaigns.read(args.campaign)
                print(json.dumps(data, indent=2, ensure_ascii=False))
                return 0
            if args.action in {
                "add-grid",
                "add-move",
                "add-property",
                "add-expression",
            }:
                self.add(campaigns, args)
                return 0
            campaign_path, data, campaign_bytes = campaigns.read(args.campaign)
            planner = CampaignPlanner(project)
            groups = CampaignApplier.groups(planner, data)
            if not groups:
                raise ValueError("Campaign has no changes")
            if args.action == "preview":
                self.preview(args, planner, groups, campaign_bytes)
                return 0
            if args.action == "apply":
                self.apply(
                    args,
                    project,
                    planner,
                    groups,
                    data,
                    campaign_path,
                    campaign_bytes,
                )
                return 0
            raise ValueError(f"Unsupported campaign action: {args.action}")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            print(f"Campaign error: {error}", file=sys.stderr)
            return 2


main = CampaignCommand()

if __name__ == "__main__":
    raise SystemExit(main())
