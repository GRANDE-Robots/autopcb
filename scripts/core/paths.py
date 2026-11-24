"""paths.py: locate AutoPCB support files beside a native KiCad project.

A project keeps its configuration, inputs and results either in an
``autopcb/`` folder beside the KiCad files or, for a managed design, in an
AutoPCB workspace. These classes resolve those locations, including the
previous layouts, and are the only place that knows the folder names.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

ENTRY_ENV = "AUTOPCB_NATIVE_ENTRY"
WORKSPACE_ENV = "AUTOPCB_WORKSPACE_ROOT"


class LongPath:
    """Makes filesystem access independent of Windows' legacy path limit."""

    def __call__(self, path: Path) -> Path:
        """Resolve a path and, on Windows, give it the extended-length prefix."""
        resolved = path.resolve()
        text = str(resolved)
        if os.name != "nt" or text.startswith(("\\\\?\\", "\\\\.\\")):
            return resolved
        if text.startswith("\\\\"):
            return Path("\\\\?\\UNC\\" + text[2:])
        return Path("\\\\?\\" + text)


class AssetRoot:
    """Finds the folder that holds a project's AutoPCB files."""

    def __call__(self, project: Path) -> Path:
        """Use the migrated ``autopcb/`` folder, or the project root for legacy/test fixtures."""
        root = Path(project).resolve()
        migrated = root / "autopcb"
        return migrated if migrated.is_dir() else root


@dataclass(frozen=True)
class ProjectStorage:
    """Paths for one native design, optionally owned by an AutoPCB workspace.

    Inputs:
        native: Folder that holds the KiCad project files.
        workspace: Workspace root that owns the design, or None for the legacy
            layout beside the project.
        key: The design's folder name inside the workspace.
    """

    native: Path
    workspace: Path | None = None
    key: str | None = None

    @classmethod
    def managed(cls, entry: Path, workspace: Path) -> ProjectStorage:
        """Storage for a design inside a workspace.

        Inputs:
            entry: The design's KiCad board file.
            workspace: Existing workspace folder.
        Outputs:
            Storage keyed by the board name when it sits at
            ``results/projects/<name>/``, otherwise by a name and a hash of its
            path. Raises ValueError when the workspace does not exist.
        """
        entry, workspace = entry.resolve(), workspace.resolve()
        if not workspace.is_dir():
            raise ValueError(f"AutoPCB workspace does not exist: {workspace}")
        family = entry.with_suffix(".kicad_pro")
        identity = (
            family.relative_to(workspace)
            if family.is_relative_to(workspace)
            else family
        )
        if identity.parts == ("results", "projects", entry.stem, family.name):
            key = entry.stem
        else:
            label = (
                re.sub(r"[^A-Za-z0-9_-]", "-", entry.stem).strip("-")[:32] or "board"
            )
            token = hashlib.sha256(
                os.path.normcase(identity.as_posix()).encode()
            ).hexdigest()[:16]
            key = f"{label}-{token}"
        return cls(entry.parent, workspace, key)

    @classmethod
    def bound(cls, project: Path) -> ProjectStorage:
        """Storage for a project folder.

        Uses the workspace named by the worker's environment only when it is
        bound to this exact project; otherwise the legacy layout beside it.
        """
        root = Path(project).resolve()
        workspace, entry = os.environ.get(WORKSPACE_ENV), os.environ.get(ENTRY_ENV)
        if workspace and entry and Path(entry).resolve().parent == root:
            return cls.managed(Path(entry), Path(workspace))
        return cls(root)

    @property
    def legacy(self) -> Path:
        """The asset folder beside the native project."""
        return asset_root(self.native)

    def _owned(self, *parts: str) -> Path:
        """A path under the workspace; raises ValueError if it would leave it."""
        target = self.workspace.joinpath(*parts)
        if not target.resolve().is_relative_to(self.workspace):
            raise ValueError(f"Workspace path points outside the workspace: {target}")
        return target

    @property
    def config(self) -> Path:
        """Folder of this design's configuration."""
        return (
            self._owned("config", "projects", self.key)
            if self.workspace
            else self.legacy / "config"
        )

    @property
    def campaigns(self) -> Path:
        """Folder of this design's campaign files."""
        return (
            self._owned("config", "campaigns", self.key)
            if self.workspace
            else self.config / "campaigns"
        )

    @property
    def data(self) -> Path:
        """Folder of this design's engineering inputs."""
        return (
            self._owned("data", "projects", self.key)
            if self.workspace
            else self.legacy / "data"
        )

    @property
    def results(self) -> Path:
        """Folder this design's results are written to."""
        return (
            self._owned("results", "jobs", self.key)
            if self.workspace
            else self.legacy / "results"
        )

    def config_file(self, name: str) -> Path:
        """A configuration file, falling back to the legacy copy when only that exists."""
        selected = self.config / name
        legacy = self.legacy / "config" / name
        return legacy if not selected.exists() and legacy.exists() else selected

    def data_file(self, *relative: str) -> Path:
        """An input path under the folder that owns its first component.

        The current data folder, then the legacy ``data`` and ``design``
        folders are tried in turn; the first one that has the first component
        supplies the whole collection.
        """
        roots = (self.data, self.legacy / "data", self.legacy / "design")
        owner = next(
            (root for root in roots if root.joinpath(*relative[:1]).exists()), roots[0]
        )
        return owner.joinpath(*relative)

    def environment(self, entry: Path) -> dict[str, str]:
        """The variables that bind a worker process to this design and workspace."""
        return (
            {ENTRY_ENV: str(entry.resolve()), WORKSPACE_ENV: str(self.workspace)}
            if self.workspace
            else {}
        )


class DataPath:
    """Resolves engineering inputs, including the previous design directory."""

    def __call__(self, project: Path, *relative: str) -> Path:
        """The input path for a project folder and path components."""
        return ProjectStorage.bound(project).data_file(*relative)


class DataReference:
    """Resolves a saved evidence reference to an input file."""

    def __call__(self, project: Path, name: str) -> Path:
        """Resolve a reference across historical and current input paths.

        Outputs:
            The file path. Raises ValueError when the name is absolute, climbs
            out with ``..``, or names ``data`` or ``design`` without a file.
        """
        reference = Path(name.replace("\\", "/"))
        if not reference.parts or reference.anchor or ".." in reference.parts:
            raise ValueError(f"Expected a relative evidence path: {name}")
        if reference.parts[0] in {"data", "design"}:
            if len(reference.parts) < 2:
                raise ValueError(f"Expected an evidence file: {name}")
            return data_path(project, *reference.parts[1:])
        return asset_root(project) / reference


class ConfigPath:
    """Resolves a configuration file for a project."""

    def __call__(self, project: Path, name: str) -> Path:
        """The configuration file, preferring the current folder over the legacy one."""
        return ProjectStorage.bound(project).config_file(name)


class ResultsPath:
    """Resolves where a project's results are written."""

    def __call__(self, project: Path, *parts: str) -> Path:
        """A path under the project's results folder."""
        return ProjectStorage.bound(project).results.joinpath(*parts)


class StoredResult:
    """Resolves an existing result across the current and historical folders."""

    def __call__(self, project: Path, *parts: str) -> Path:
        """The result path, using the historical folder only when it alone has the file."""
        store = ProjectStorage.bound(project)
        preferred = store.results.joinpath(*parts)
        historical = (store.legacy / "results").joinpath(*parts)
        return (
            historical if not preferred.exists() and historical.exists() else preferred
        )


class SupportInputName:
    """Names a support input as it appears in a private native copy."""

    def __call__(self, project: Path, source: Path) -> Path:
        """Give a support input the same relative name in a private native copy."""
        store = ProjectStorage.bound(project)
        if store.workspace:
            support = store.legacy.relative_to(store.native)
            if source.is_relative_to(store.config):
                return support / "config" / source.relative_to(store.config)
            if source.is_relative_to(store.data):
                return support / "data" / source.relative_to(store.data)
        return source.relative_to(store.native)


class LibraryRoot:
    """Locates a project's library folder."""

    def __call__(self, project: Path) -> Path:
        """Locate project-level libraries, with support for the previous AutoPCB layout."""
        root = Path(project).resolve()
        libraries = root / "libraries"
        return libraries if libraries.is_dir() else asset_root(root) / "libraries"


class LibraryDirectories:
    """Lists the library folders a project uses."""

    def __call__(self, project: Path) -> tuple[Path, ...]:
        """Find the selected library tree and any footprint libraries beside it."""
        libraries = library_root(project)
        candidates = (libraries, *sorted(libraries.parent.glob("*.pretty")))
        return tuple(directory for directory in candidates if directory.is_dir())


class DefaultProject:
    """Finds the native project for a module run directly."""

    def __call__(self, module_file: str) -> Path:
        """Find the native project from a module's own file path.

        Outputs:
            The folder above ``autopcb/`` when the module sits in an embedded
            support folder beside KiCad files, otherwise the support folder.
        """
        support = Path(module_file).resolve().parents[2]
        parent = support.parent
        embedded = support.name.lower() == "autopcb" and any(
            next(parent.glob(f"*{suffix}"), None) is not None
            for suffix in (".kicad_pro", ".kicad_pcb", ".kicad_sch")
        )
        return parent if embedded else support


long_path = LongPath()
asset_root = AssetRoot()
data_path = DataPath()
data_reference = DataReference()
config_path = ConfigPath()
results_path = ResultsPath()
stored_result = StoredResult()
support_input_name = SupportInputName()
library_root = LibraryRoot()
library_directories = LibraryDirectories()
default_project = DefaultProject()
