"""Paths and source snapshots for the live Jessie project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.paths import library_directories

from core.export_helpers import sha


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    """Locate native sources without copying or modifying the project."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    @property
    def schematic(self) -> Path:
        return self.root / "v4.1.kicad_sch"

    @property
    def board(self) -> Path:
        return self.root / "v4.1.kicad_pcb"

    @property
    def settings(self) -> Path:
        return self.root / "v4.1.kicad_pro"

    @property
    def rules(self) -> Path:
        return self.root / "v4.1.kicad_dru"

    @property
    def review(self) -> Path:
        return self.root / "deliverables" / "review"

    @property
    def sources(self) -> tuple[Path, ...]:
        return self.schematic, self.board, self.settings, self.rules

    def audit_inputs(self) -> tuple[Path, ...]:
        """List the native files and relevant libraries used by ERC."""
        paths = list(self.sources)
        paths.extend(
            path
            for path in (self.root / "sym-lib-table", self.root / "fp-lib-table")
            if path.is_file()
        )
        paths.extend(
            path
            for directory in library_directories(self.root)
            for path in directory.rglob("*")
            if path.is_file()
            and path.suffix.lower() in {".kicad_sym", ".kicad_mod", ".kicad_wks"}
        )
        return tuple(sorted(paths))


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """Bind an audit to the exact saved files and detect concurrent changes."""

    project: ProjectPaths
    hashes: dict[str, str]

    @classmethod
    def capture(cls, project: ProjectPaths) -> SourceSnapshot:
        hashes = {}
        for path in project.audit_inputs():
            resolved = path.resolve()
            if not resolved.is_relative_to(project.root) or not resolved.is_file():
                raise ValueError(f"Missing or out-of-project audit source: {path}")
            hashes[resolved.relative_to(project.root).as_posix()] = sha(resolved)
        return cls(project=project, hashes=hashes)

    def verify_unchanged(self) -> None:
        if self.hashes != self.capture(self.project).hashes:
            raise RuntimeError(
                "Saved project inputs changed during ERC; discard the result."
            )
