"""Standard locations for AutoPCB inputs, policy and generated projects."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.paths import ProjectStorage
from core.project import ProjectPaths


@dataclass(frozen=True)
class Workspace:
    root: Path

    def __post_init__(self) -> None:
        root = self.root.resolve()
        if not root.is_dir():
            raise ValueError(f"Select an existing AutoPCB workspace directory: {root}")
        object.__setattr__(self, "root", root)

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def results(self) -> Path:
        return self.root / "results"

    @property
    def projects(self) -> Path:
        return self.results / "projects"

    @property
    def requirements(self) -> Path:
        return self.data / "requirements.txt"

    def entries(self) -> tuple[Path, ...]:
        recorded = ()
        if self.projects.is_dir():
            recorded = tuple(
                project.resolve()
                for directory in self.projects.iterdir()
                if directory.is_dir()
                and not directory.is_symlink()
                and not directory.name.startswith(".")
                for project in ProjectPaths.entries(directory)
            )
        return tuple(sorted({*ProjectPaths.entries(self.root), *recorded}))

    def project(self, selected: Path | None) -> ProjectPaths:
        if selected is not None:
            return ProjectPaths(selected)
        entries = self.entries()
        if len(entries) != 1:
            detail = ", ".join(
                path.relative_to(self.root).as_posix() for path in entries
            )
            message = (
                f"Several KiCad projects are available. Use --project to select one: {detail}"
                if entries
                else (
                    "Use --project to open an existing design or create a project from requirements."
                )
            )
            raise ValueError(message)
        return ProjectPaths(entries[0])

    def project_output(self, name: str) -> Path:
        if (
            not isinstance(name, str)
            or "/" in name
            or "\\" in name
            or Path(name).name != name
            or name in ("", ".", "..")
        ):
            raise ValueError("Use a project name without directory separators.")
        self.projects.mkdir(parents=True, exist_ok=True)
        return self.projects / name

    def storage(self, entry: Path) -> ProjectStorage:
        return ProjectStorage.managed(entry, self.root)

    def export_output(self, project: ProjectPaths, draft: bool) -> Path:
        root = self.results / "exports"
        root.mkdir(parents=True, exist_ok=True)
        return root / (
            self.storage(project.entry).key + ("-draft" if draft else "-checked")
        )
