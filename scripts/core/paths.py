"""Locate AutoPCB support files beside a native KiCad project."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
from dataclasses import dataclass


ENTRY_ENV = "AUTOPCB_NATIVE_ENTRY"
WORKSPACE_ENV = "AUTOPCB_WORKSPACE_ROOT"


def long_path(path: Path) -> Path:
    """Make filesystem access independent of Windows' legacy path limit."""
    resolved = path.resolve()
    text = str(resolved)
    if os.name != "nt" or text.startswith(("\\\\?\\", "\\\\.\\")):
        return resolved
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + text[2:])
    return Path("\\\\?\\" + text)


def asset_root(project: Path) -> Path:
    """Use the migrated folder, or the project root for legacy/test fixtures."""
    root = Path(project).resolve()
    migrated = root / "autopcb"
    return migrated if migrated.is_dir() else root


@dataclass(frozen=True, slots=True)
class ProjectStorage:
    """Paths for one native design, optionally owned by an AutoPCB workspace."""

    native: Path
    workspace: Path | None = None
    key: str | None = None

    @classmethod
    def managed(cls, entry: Path, workspace: Path) -> ProjectStorage:
        entry, workspace = entry.resolve(), workspace.resolve()
        if not workspace.is_dir():
            raise ValueError(f"AutoPCB workspace does not exist: {workspace}")
        family = entry.with_suffix(".kicad_pro")
        identity = family.relative_to(workspace) if family.is_relative_to(workspace) else family
        if identity.parts == ("results", "projects", entry.stem, family.name):
            key = entry.stem
        else:
            label = re.sub(r"[^A-Za-z0-9_-]", "-", entry.stem).strip("-")[:32] or "board"
            token = hashlib.sha256(os.path.normcase(identity.as_posix()).encode()).hexdigest()[:16]
            key = f"{label}-{token}"
        return cls(entry.parent, workspace, key)

    @property
    def legacy(self) -> Path:
        return asset_root(self.native)

    def _owned(self, *parts: str) -> Path:
        target = self.workspace.joinpath(*parts)
        if not target.resolve().is_relative_to(self.workspace):
            raise ValueError(f"Workspace path points outside the workspace: {target}")
        return target

    @property
    def config(self) -> Path:
        return self._owned("config", "projects", self.key) if self.workspace else self.legacy / "config"

    @property
    def campaigns(self) -> Path:
        return self._owned("config", "campaigns", self.key) if self.workspace else self.config / "campaigns"

    @property
    def data(self) -> Path:
        return self._owned("data", "projects", self.key) if self.workspace else self.legacy / "data"

    @property
    def results(self) -> Path:
        return self._owned("results", "jobs", self.key) if self.workspace else self.legacy / "results"

    def config_file(self, name: str) -> Path:
        selected = self.config / name
        legacy = self.legacy / "config" / name
        return legacy if not selected.exists() and legacy.exists() else selected

    def data_file(self, *relative: str) -> Path:
        selected = self.data.joinpath(*relative)
        current = (self.legacy / "data").joinpath(*relative)
        historical = (self.legacy / "design").joinpath(*relative)
        return next((path for path in (selected, current, historical) if path.exists()), selected)

    def environment(self, entry: Path) -> dict[str, str]:
        return {ENTRY_ENV: str(entry.resolve()), WORKSPACE_ENV: str(self.workspace)} if self.workspace else {}


def project_storage(project: Path) -> ProjectStorage:
    """Use an explicit worker binding only for the selected native project."""
    root = Path(project).resolve()
    workspace, entry = os.environ.get(WORKSPACE_ENV), os.environ.get(ENTRY_ENV)
    if workspace and entry and Path(entry).resolve().parent == root:
        return ProjectStorage.managed(Path(entry), Path(workspace))
    return ProjectStorage(root)


def data_path(project: Path, *relative: str) -> Path:
    """Resolve engineering inputs, including the previous design directory."""
    return project_storage(project).data_file(*relative)



def config_path(project: Path, name: str) -> Path:
    return project_storage(project).config_file(name)


def results_path(project: Path, *parts: str) -> Path:
    return project_storage(project).results.joinpath(*parts)



def stored_result(project: Path, *parts: str) -> Path:
    store = project_storage(project)
    preferred = store.results.joinpath(*parts)
    historical = (store.legacy / "results").joinpath(*parts)
    return historical if not preferred.exists() and historical.exists() else preferred


def support_input_name(project: Path, source: Path) -> Path:
    """Give a support input the same relative name in a private native copy."""
    store = project_storage(project)
    if store.workspace:
        support = store.legacy.relative_to(store.native)
        if source.is_relative_to(store.config):
            return support / "config" / source.relative_to(store.config)
        if source.is_relative_to(store.data):
            return support / "data" / source.relative_to(store.data)
    return source.relative_to(store.native)

def library_root(project: Path) -> Path:
    """Locate project-level libraries, with support for the previous AutoPCB layout."""
    root = Path(project).resolve()
    libraries = root / "libraries"
    return libraries if libraries.is_dir() else asset_root(root) / "libraries"


def library_directories(project: Path) -> tuple[Path, ...]:
    """Find the selected library tree and any footprint libraries beside it."""
    libraries = library_root(project)
    candidates = (libraries, *sorted(libraries.parent.glob("*.pretty")))
    return tuple(directory for directory in candidates if directory.is_dir())


def default_project(module_file: str) -> Path:
    """Find the native project when invoked directly from a module."""
    support = Path(module_file).resolve().parents[2]
    parent = support.parent
    embedded = support.name.lower() == "autopcb" and any(
        next(parent.glob(f"*{suffix}"), None) is not None
        for suffix in (".kicad_pro", ".kicad_pcb", ".kicad_sch")
    )
    return parent if embedded else support
