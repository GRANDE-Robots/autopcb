"""project.py: resolve related KiCad documents and fingerprint the native project tree.

A native project is a family of files sharing one basename: schematic, board,
settings and rules. These classes choose the family a command means, follow its
schematic sheets and libraries, read the optional AutoPCB profile, and bind an
operation to the exact saved files.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from core.export_helpers import sha
from core.kicad_sexpr import parse, select_direct_children
from core.paths import ENTRY_ENV as ENTRY_ENV, library_directories

PROFILE_NAME = "autopcb.project.json"
SNAPSHOT_ENTRY = ".autopcb-snapshot-entry"
NATIVE_SUFFIXES = (".kicad_pro", ".kicad_pcb", ".kicad_sch")


class ProjectProfile:
    """The optional ``autopcb.project.json`` that selects an entry and declares nets."""

    @staticmethod
    def read(root: Path) -> dict:
        """Read the profile in a project folder.

        Outputs:
            The profile, or an empty dict when there is none. Raises ValueError
            when the file is over 2 MB or is not a version 1 profile.
        """
        path = root / PROFILE_NAME
        if not path.is_file():
            return {}
        if path.stat().st_size > 2_000_000:
            raise ValueError("AutoPCB project profile is too large")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if (
            not isinstance(data, dict)
            or type(data.get("schema_version")) is not int
            or data["schema_version"] != 1
        ):
            raise ValueError(f"Unsupported AutoPCB project profile: {path}")
        return data

    @classmethod
    def declared_pairs(
        cls, root: Path, native_nets: set[str]
    ) -> tuple[tuple[str, str], ...] | None:
        """The differential pairs the profile declares, as native net names.

        Inputs:
            root: Project folder.
            native_nets: Net names on the board, with or without a leading ``/``.
        Outputs:
            Pairs of board net names, or None when the profile has not marked
            the high-speed review as done. Raises ValueError when the list is
            missing or a pair is malformed, missing, ambiguous or repeated.
        """
        profile = cls.read(root)
        if profile.get("high_speed_reviewed") is not True:
            return None
        pairs = profile.get("differential_pairs")
        if not isinstance(pairs, list):
            raise ValueError(
                "High-speed review requires an explicit differential-pair list"
            )
        selected = set()
        resolved = []
        for pair in pairs:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not all(isinstance(name, str) and name.strip() for name in pair)
            ):
                raise ValueError(
                    "A differential pair requires two nonempty native net names"
                )
            names = []
            for name in pair:
                matches = native_nets & {name, "/" + name}
                if len(matches) != 1 or matches & selected:
                    raise ValueError(
                        f"Missing, ambiguous or repeated differential net: {name}"
                    )
                actual = matches.pop()
                selected.add(actual)
                names.append(actual)
            resolved.append((names[0], names[1]))
        return tuple(resolved)

    @classmethod
    def declared_high_speed(cls, root: Path, native_nets: set[str]) -> set[str] | None:
        """Every net in a declared pair, or None when no review is declared."""
        pairs = cls.declared_pairs(root, native_nets)
        return None if pairs is None else {name for pair in pairs for name in pair}


@dataclass(frozen=True)
class ProjectPaths:
    """Select one native project family, retaining its basename and sheet structure."""

    root: Path
    entry: Path = field(init=False)

    def __post_init__(self) -> None:
        """Choose the project entry for ``root``, a folder or a KiCad file.

        The entry comes from, in order: an explicit KiCad file, the worker's
        forwarded entry, a snapshot marker, the profile, or the only project
        in the folder. Raises ValueError when none applies, several do, or the
        choice is outside the project or not a KiCad file.
        """
        location = Path(self.root).resolve()
        explicit = not location.is_dir() and location.suffix.lower() in NATIVE_SUFFIXES
        root = location.parent if explicit else location
        entry = location if explicit else None
        forwarded = (
            Path(os.environ[ENTRY_ENV]).resolve() if ENTRY_ENV in os.environ else None
        )
        if entry is None and forwarded is not None and forwarded.parent == root:
            entry = forwarded
        marker = root / SNAPSHOT_ENTRY
        if entry is None and marker.is_file():
            name = marker.read_text(encoding="utf-8").strip()
            if not name or Path(name).name != name or not (root / name).is_file():
                raise ValueError(f"Invalid native snapshot selection: {marker}")
            entry = root / name
        if (
            entry is None
            and (selected := ProjectProfile.read(root).get("entry")) is not None
        ):
            if not isinstance(selected, str) or Path(selected).name != selected:
                raise ValueError(
                    "Profile entry must be the name of a KiCad file in the project directory"
                )
            entry = root / selected
            if not entry.is_file():
                raise ValueError(f"Profile entry is missing: {entry}")
        if entry is None:
            entries = self.entries(root)
            if len(entries) > 1:
                names = ", ".join(item.name for item in entries)
                raise ValueError(
                    f"Multiple KiCad projects in {root}: {names}. Select a native file with --project."
                )
            entry = entries[0] if entries else root / f"{root.name}.kicad_pro"
        entry = self.contained(root, entry)
        if entry.suffix.lower() not in NATIVE_SUFFIXES:
            raise ValueError(f"Expected a KiCad project, board, or schematic: {entry}")
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "entry", entry)

    @staticmethod
    def contained(parent: Path, child: Path) -> Path:
        """Resolve a path and require it to be inside ``parent``; raises ValueError if not."""
        resolved = child.resolve()
        if not resolved.is_relative_to(parent):
            raise ValueError(f"KiCad source must be inside its project: {child}")
        return resolved

    @classmethod
    def sheet_references(cls, document: Path, root: Path) -> tuple[Path, ...]:
        """The sheet files a schematic names, each required to be inside ``root``."""
        text = document.read_text(encoding="utf-8-sig")
        if not text.lstrip().startswith("(kicad_sch"):
            return ()
        references = []
        for sheet in select_direct_children(text, "sheet"):
            for prop in parse(sheet).children("property"):
                atoms = prop.atoms()
                if (
                    len(atoms) >= 3
                    and atoms[1].value.lower().replace(" ", "") == "sheetfile"
                ):
                    references.append(
                        cls.contained(root, document.parent / atoms[2].value)
                    )
        return tuple(references)

    @classmethod
    def entries(cls, root: Path) -> tuple[Path, ...]:
        """List project families without listing the child sheets as independent designs."""
        root = root.resolve()
        anchors: dict[str, Path] = {}
        for suffix in NATIVE_SUFFIXES[:2]:
            for path in sorted(root.glob(f"*{suffix}")):
                if path.is_file() and not path.name.startswith(("_autosave-", "~")):
                    anchors.setdefault(path.stem, path)
        if anchors:
            return tuple(sorted(anchors.values()))
        sheets = {
            path
            for path in root.glob("*.kicad_sch")
            if path.is_file() and not path.name.startswith(("_autosave-", "~"))
        }
        if len(sheets) > 1:
            children = {
                child for path in sheets for child in cls.sheet_references(path, root)
            }
            sheets -= children
        return tuple(sorted(sheets))

    @property
    def schematic(self) -> Path:
        """The top schematic file."""
        return self.entry.with_suffix(".kicad_sch")

    @property
    def board(self) -> Path:
        """The board file."""
        return self.entry.with_suffix(".kicad_pcb")

    @property
    def settings(self) -> Path:
        """The project settings file."""
        return self.entry.with_suffix(".kicad_pro")

    @property
    def rules(self) -> Path:
        """The custom design rules file."""
        return self.entry.with_suffix(".kicad_dru")

    @property
    def review(self) -> Path:
        """The folder review deliverables are written to."""
        return self.root / "deliverables" / "review"

    @property
    def sources(self) -> tuple[Path, ...]:
        """The schematic, board, settings and rules paths, whether or not they exist."""
        return self.schematic, self.board, self.settings, self.rules

    def sheets(self) -> tuple[Path, ...]:
        """Every schematic sheet reachable from the top schematic, sorted.

        Outputs: Sheet paths. Raises ValueError for a sheet file that is missing.
        """
        pending = [self.schematic] if self.schematic.is_file() else []
        visited = set()
        while pending:
            document = pending.pop()
            if document in visited:
                continue
            if not document.is_file():
                raise ValueError(f"Missing schematic sheet: {document}")
            visited.add(document)
            pending.extend(self.sheet_references(document, self.root))
        return tuple(sorted(visited))

    def audit_inputs(self) -> tuple[Path, ...]:
        """Find documents, the selected hierarchy, and project-local libraries.

        Outputs:
            Sorted paths of the existing KiCad files, sheets, the profile, the
            library tables and every symbol, footprint and worksheet file in
            the project's libraries. Raises ValueError for a project library
            a table names that does not exist.
        """
        paths = {path for path in self.sources if path.is_file()}
        paths.update(self.sheets())
        directories = set(library_directories(self.root))
        profile = self.root / PROFILE_NAME
        if profile.is_file():
            paths.add(profile)
        for table_name in ("sym-lib-table", "fp-lib-table"):
            table = self.root / table_name
            if not table.is_file():
                continue
            paths.add(table)
            data = table.read_text(encoding="utf-8-sig")
            if not data.lstrip().startswith("("):
                continue
            for lib in parse(data).children("lib"):
                for uri in lib.children("uri"):
                    value = uri.atoms()[1].value
                    if value.startswith(("${KIPRJMOD}/", "${KIPRJMOD}\\")):
                        target = self.contained(self.root, self.root / value[12:])
                        if target.is_dir():
                            directories.add(target)
                        elif target.is_file():
                            paths.add(target)
                        else:
                            raise ValueError(f"Missing project library: {target}")
        paths.update(
            path
            for directory in directories
            for path in directory.rglob("*")
            if path.is_file()
            and path.suffix.lower() in {".kicad_sym", ".kicad_mod", ".kicad_wks"}
        )
        return tuple(sorted(self.contained(self.root, path) for path in paths))


class NativePath:
    """Locates a native file of the design an operation was pointed at."""

    def __call__(self, location: Path, suffix: str) -> Path:
        """Locate the design on paths already passed through an operation's selector.

        Inputs:
            location: A project folder or KiCad file.
            suffix: The wanted extension, such as ``.kicad_pcb``.
        Outputs:
            The path with that extension. When the selector rejects the folder,
            a folder holding disposable PCB-only candidates beside one complete
            source family resolves to that family; otherwise the original
            ValueError is raised.
        """
        try:
            return ProjectPaths(location).entry.with_suffix(suffix)
        except ValueError:
            root = location.resolve()
            selection = os.environ.get(ENTRY_ENV)
            if (
                not root.is_dir()
                or (root / PROFILE_NAME).exists()
                or (root / SNAPSHOT_ENTRY).exists()
                or (selection and Path(selection).resolve().parent == root)
            ):
                raise
            entries = ProjectPaths.entries(root)
            if len(entries) <= 1:
                raise
            # Legacy native workers sometimes share a directory with disposable
            # PCB-only candidates. Prefer the single complete native source family.
            complete = [
                entry for entry in entries if entry.with_suffix(".kicad_sch").is_file()
            ]
            if len(complete) == 1:
                return complete[0].with_suffix(suffix)
            configured = [entry for entry in entries if entry.suffix == ".kicad_pro"]
            if not complete and len(configured) == 1:
                return configured[0].with_suffix(suffix)
            raise


native_path = NativePath()


@dataclass(frozen=True)
class SourceSnapshot:
    """Bind operations to the exact saved files and dependency inventory."""

    project: ProjectPaths
    hashes: dict[str, str]

    @classmethod
    def capture(cls, project: ProjectPaths) -> SourceSnapshot:
        """Hash every audit input of a project, by path relative to its root."""
        return cls(
            project,
            {
                path.relative_to(project.root).as_posix(): sha(path)
                for path in project.audit_inputs()
            },
        )

    def verify_unchanged(self) -> None:
        """Raise RuntimeError when the saved inputs differ from the snapshot."""
        if self.hashes != self.capture(self.project).hashes:
            raise RuntimeError("Saved project inputs changed; discard this result.")
