"""Locate AutoPCB support files beside a native KiCad project."""

import os
from pathlib import Path


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
    return support.parent if support.name.lower() == "autopcb" else support
