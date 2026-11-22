"""Preview and explicitly apply source-bound local KiCad library campaigns."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.kicad_campaign import CampaignFile, Plan, digest
from core.library_edits import LibraryInputs, file_hash, library_path, plan_library
from core.library_validation import validate_library
from core.toolchain import kicad_cli

SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PreparedLibraryEdit:
    """A validated candidate bound to exact saved inputs and a campaign."""

    project: Path
    campaign_path: Path
    campaign_hash: str
    inputs: LibraryInputs
    plan: Plan
    cli_path: Path
    cli_hash: str
    selected_changes: int
    other_changes: int

    def verify(self) -> None:
        self.inputs.verify_unchanged()
        if digest(self.campaign_path.read_bytes()) != self.campaign_hash:
            raise RuntimeError("Campaign changed; preview it again")
        if not self.cli_path.is_file() or file_hash(self.cli_path) != self.cli_hash:
            raise RuntimeError("KiCad CLI changed; preview the library campaign again")

    @property
    def preview_sha256(self) -> str:
        identity = {
            "project": str(self.project),
            "document": self.plan.source.relative_to(self.project).as_posix(),
            "source_sha256": self.plan.source_sha256,
            "result_sha256": self.plan.result_sha256,
            "inputs_sha256": self.inputs.signature,
            "campaign_sha256": self.campaign_hash,
            "kicad_cli": str(self.cli_path),
            "kicad_cli_sha256": self.cli_hash,
        }
        return digest(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )

    def summary(self) -> dict[str, Any]:
        return {
            "document": self.plan.source.relative_to(self.project).as_posix(),
            "source_sha256": self.plan.source_sha256,
            "result_sha256": self.plan.result_sha256,
            "inputs_sha256": self.inputs.signature,
            "campaign_sha256": self.campaign_hash,
            "kicad_cli": str(self.cli_path),
            "kicad_cli_sha256": self.cli_hash,
            "preview_sha256": self.preview_sha256,
            "selected_changes": self.selected_changes,
            "other_campaign_changes": self.other_changes,
            "effective_edits": len(self.plan.edits),
            "validation": "KiCad SVG export on a temporary candidate",
            "limitations": (
                "External libraries are not fingerprinted. Native export does not verify "
                "electrical behavior, physical fit, or PCB/schematic synchronization."
            ),
        }


def _selected_changes(
    project: Path, target: Path, changes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    selected = []
    for change in changes:
        document = change.get("document")
        if not isinstance(document, str) or not document:
            raise ValueError("Each campaign change needs a project-relative document")
        relative = Path(document)
        if relative.is_absolute():
            raise ValueError("Campaign documents must be project-relative")
        resolved = (project / relative).resolve()
        if not resolved.is_relative_to(project):
            raise ValueError("Campaign document resolves outside the project")
        if resolved == target:
            selected.append(change)
    if not selected:
        raise ValueError("The campaign contains no changes for this library document")
    return selected


def prepare(
    project: Path, campaign: str, document: str, cli: Path | None = None
) -> PreparedLibraryEdit:
    """Plan and validate a library candidate without changing the project."""
    project = project.resolve()
    target = library_path(project, document)
    cli_path = kicad_cli(cli)
    cli_hash = file_hash(cli_path)
    inputs = LibraryInputs.capture(project)
    campaign_path, data, original_campaign = CampaignFile(project).read(campaign)
    selected = _selected_changes(project, target, data["changes"])
    plan = plan_library(project, document, selected)
    source_name = target.relative_to(project).as_posix()
    if dict(inputs.hashes).get(source_name) != plan.source_sha256:
        raise RuntimeError("Library source changed during inspection")
    result = PreparedLibraryEdit(
        project,
        campaign_path,
        digest(original_campaign),
        inputs,
        plan,
        cli_path,
        cli_hash,
        len(selected),
        len(data["changes"]) - len(selected),
    )
    result.verify()
    validate_library(plan, cli_path)
    result.verify()
    return result


def _expected(name: str, supplied: str, observed: str) -> None:
    value = supplied.lower()
    if not SHA256.fullmatch(value) or value != observed:
        raise RuntimeError(f"{name} hash differs from the preview; preview again")


def _verify_editor_unlocked(edit: PreparedLibraryEdit) -> None:
    locks = list(edit.project.glob("~*.lck"))
    target_lock = edit.plan.source.with_name(f"~{edit.plan.source.name}.lck")
    if target_lock.exists():
        locks.append(target_lock)
    if locks:
        raise RuntimeError(
            f"KiCad lock present; refusing to modify a library: {locks[0]}"
        )


def apply(edit: PreparedLibraryEdit, *, preview_sha256: str) -> str:
    """Apply exactly one previously reviewed candidate, or refuse on changes."""
    _expected("Preview", preview_sha256, edit.preview_sha256)
    edit.verify()
    _verify_editor_unlocked(edit)
    if edit.plan.before == edit.plan.after:
        return "no_changes"
    target = edit.plan.source
    descriptor, staged_name = tempfile.mkstemp(
        prefix=".library-write-", suffix=".tmp", dir=target.parent
    )
    staged = Path(staged_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(edit.plan.after)
            handle.flush()
            os.fsync(handle.fileno())
        if hashlib.sha256(staged.read_bytes()).hexdigest() != edit.plan.result_sha256:
            raise RuntimeError("Staged library failed readback")
        edit.verify()
        _verify_editor_unlocked(edit)
        os.replace(staged, target)
        if digest(target.read_bytes()) != edit.plan.result_sha256:
            raise RuntimeError("Saved library failed readback")
    finally:
        if staged.exists():
            staged.unlink()
    return "applied"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("preview", "apply"):
        action = actions.add_parser(name)
        action.add_argument("campaign", help="timestamped campaign file")
        action.add_argument(
            "--document", required=True, help="project-relative library file"
        )
        action.add_argument("--cli", type=Path, help="KiCad CLI path")
        if name == "preview":
            action.add_argument(
                "--diff", action="store_true", help="show the planned text diff"
            )
        else:
            action.add_argument("--expected-preview-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        edit = prepare(args.project, args.campaign, args.document, args.cli)
        result = edit.summary()
        if args.action == "preview":
            result["status"] = "preview"
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            if args.diff:
                print(
                    "".join(
                        difflib.unified_diff(
                            edit.plan.before.decode("utf-8").splitlines(keepends=True),
                            edit.plan.after.decode("utf-8").splitlines(keepends=True),
                            fromfile=result["document"],
                            tofile="library candidate",
                        )
                    )
                )
        else:
            result["status"] = apply(edit, preview_sha256=args.expected_preview_sha256)
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (
        OSError,
        ValueError,
        RuntimeError,
        UnicodeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Library campaign failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
