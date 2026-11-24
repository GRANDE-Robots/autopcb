"""workspace.py: standard locations for AutoPCB inputs, policy and generated projects.

A workspace is one folder with ``config``, ``data`` and ``results`` beneath it.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.paths import ProjectStorage
from core.project import ProjectPaths


@dataclass(frozen=True)
class Workspace:
    """An existing folder that holds AutoPCB configuration, input data and results."""

    root: Path

    def __post_init__(self) -> None:
        """Resolve the root folder.

        Outputs:
            None. Raises ValueError when the root is not an existing directory.
        """
        root = self.root.resolve()
        if not root.is_dir():
            raise ValueError(f"Select an existing AutoPCB workspace directory: {root}")
        object.__setattr__(self, "root", root)

    @property
    def config(self) -> Path:
        """Folder for shared configuration such as routing defaults."""
        return self.root / "config"

    @property
    def data(self) -> Path:
        """Folder for requirements and the optional design catalog."""
        return self.root / "data"

    @property
    def results(self) -> Path:
        """Folder for proposals, generated projects, exports and source downloads."""
        return self.root / "results"

    @property
    def projects(self) -> Path:
        """Folder under results that holds generated projects."""
        return self.results / "projects"

    @property
    def requirements(self) -> Path:
        """Default requirements text file, under data."""
        return self.data / "requirements.txt"

    def entries(self) -> tuple[Path, ...]:
        """Find the native project entries in the workspace.

        Outputs:
            The sorted, resolved entries found in the root and in each project folder under
            results/projects, skipping hidden folders and symbolic links.
        """
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
        """Select the native project to work on.

        Inputs:
            selected: KiCad file or project folder; the workspace's only project when None.
        Outputs:
            The project. Raises ValueError that lists the choices when the workspace has
            none or several.
        """
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
        """The path of a new project folder under results/projects.

        Inputs:
            name: Folder name.
        Outputs:
            The path; the projects folder is created when missing. Raises ValueError unless
            the name is a plain name without directory separators.
        """
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
        """The managed storage of the project with this entry file."""
        return ProjectStorage.managed(entry, self.root)

    def export_output(self, project: ProjectPaths, draft: bool) -> Path:
        """The package folder for a project's export under results/exports.

        Inputs:
            project: Project to export.
            draft: True for the draft package, False for the checked package.
        Outputs:
            The path, named from the project's storage key and ``-draft`` or ``-checked``;
            the exports folder is created when missing.
        """
        root = self.results / "exports"
        root.mkdir(parents=True, exist_ok=True)
        return root / (
            self.storage(project.entry).key + ("-draft" if draft else "-checked")
        )
