"""Plan library edits and fingerprint their design dependencies."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from core.compat import file_sha256
from core.paths import library_directories, library_root
from typing import Any

from core.kicad_campaign import CampaignPlanner, Edit, Plan
from core.kicad_sexpr import parse

LIBRARY_ROOTS = {".kicad_mod": "footprint", ".kicad_sym": "kicad_symbol_lib"}
PROJECT_SUFFIXES = {
    ".kicad_pcb",
    ".kicad_sch",
    ".kicad_pro",
    ".kicad_dru",
    ".kicad_wks",
}
LIBRARY_SUFFIXES = {*LIBRARY_ROOTS, ".step", ".stp", ".wrl"}


file_hash = file_sha256


def library_path(project: Path, document: str) -> Path:
    """Resolve only existing, project-local KiCad symbol or footprint files."""
    project = project.resolve()
    if not isinstance(document, str) or not document:
        raise ValueError("A project-relative library document is required")
    relative = Path(document)
    if relative.is_absolute():
        raise ValueError("Library paths must be relative to the project")
    path = (
        (library_root(project) / relative.relative_to("libraries"))
        if relative.parts[0] == "libraries"
        else (project / relative)
    ).resolve()
    roots = [directory.resolve() for directory in library_directories(project)]
    if not any(root.is_relative_to(project) and path.is_relative_to(root) for root in roots):
        raise ValueError("Library document must be inside a project library directory")
    if path.suffix.lower() not in LIBRARY_ROOTS or not path.is_file():
        raise ValueError("Expected an existing .kicad_sym or .kicad_mod file")
    return path


def _input_paths(project: Path) -> list[Path]:
    project = project.resolve()
    paths = {
        path
        for path in project.iterdir()
        if path.is_file()
        and (
            path.suffix.lower() in PROJECT_SUFFIXES
            or path.name in {"fp-lib-table", "sym-lib-table"}
        )
    }
    for directory in library_directories(project):
        if not directory.resolve().is_relative_to(project):
            raise ValueError("Library directory resolves outside the project")
        paths.update(
            path
            for path in directory.rglob("*")
            if path.is_file() and path.suffix.lower() in LIBRARY_SUFFIXES
        )
    for path in paths:
        if not path.resolve().is_relative_to(project):
            raise ValueError(f"Project input resolves outside the project: {path}")
    return sorted(paths)


@dataclass(frozen=True)
class LibraryInputs:
    """A reproducible inventory and hashes of saved design and library inputs."""

    project: Path
    hashes: tuple[tuple[str, str], ...]

    @classmethod
    def capture(cls, project: Path) -> LibraryInputs:
        project = project.resolve()
        paths = _input_paths(project)
        hashes = tuple(
            (path.relative_to(project).as_posix(), file_hash(path)) for path in paths
        )
        if paths != _input_paths(project):
            raise RuntimeError("Project input inventory changed during inspection")
        return cls(project, hashes)

    @property
    def signature(self) -> str:
        encoded = json.dumps(
            self.hashes, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def verify_unchanged(self) -> None:
        if self != self.capture(self.project):
            raise RuntimeError(
                "Project inputs changed; preview the library campaign again"
            )


def plan_library(project: Path, document: str, changes: list[dict[str, Any]]) -> Plan:
    """Reuse campaign expression edits for one local library without writing it."""
    path = library_path(project, document)
    before = path.read_bytes()
    source = before.decode("utf-8")
    root = parse(source)
    expected = LIBRARY_ROOTS[path.suffix.lower()]
    if root.name != expected:
        raise ValueError(f"Expected {expected!r} root, found {root.name!r}")
    edits: list[Edit] = []
    for change in changes:
        if not isinstance(change, dict) or change.get("target") != {"kind": "root"}:
            raise ValueError(
                "Library edits must target the root and select children by path"
            )
        edit = CampaignPlanner._edit(source, root, change)
        if edit.before != edit.after:
            edits.append(edit)

    ordered = sorted(enumerate(edits), key=lambda pair: (pair[1].start, pair[1].end))
    for (_, left), (_, right) in zip(ordered, ordered[1:]):
        if left.end > right.start or (
            left.start == left.end == right.start and right.end > right.start
        ):
            raise ValueError(
                f"Overlapping library changes: {left.description}; {right.description}"
            )

    pieces: list[str] = []
    cursor = 0
    for _, edit in ordered:
        pieces.append(source[cursor : edit.start])
        pieces.append(edit.after)
        cursor = edit.end
    pieces.append(source[cursor:])
    updated = "".join(pieces)
    del root
    if parse(updated).name != expected:
        raise ValueError("Library edit changed the root expression")
    return Plan(path, before, updated.encode("utf-8"), tuple(edits))
