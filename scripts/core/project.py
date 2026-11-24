"""Resolve related KiCad documents and fingerprint the native project tree."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path

from core.export_helpers import sha
from core.kicad_sexpr import parse, select_direct_children
from core.paths import ENTRY_ENV as ENTRY_ENV, library_directories


PROFILE_NAME = "autopcb.project.json"
SNAPSHOT_ENTRY = ".autopcb-snapshot-entry"
NATIVE_SUFFIXES = (".kicad_pro", ".kicad_pcb", ".kicad_sch")


def _contained(parent: Path, child: Path) -> Path:
    resolved = child.resolve()
    if not resolved.is_relative_to(parent):
        raise ValueError(f"KiCad source must be inside its project: {child}")
    return resolved


def _sheet_references(document: Path, root: Path) -> tuple[Path, ...]:
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
                references.append(_contained(root, document.parent / atoms[2].value))
    return tuple(references)


def project_entries(root: Path) -> tuple[Path, ...]:
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
        children = {child for path in sheets for child in _sheet_references(path, root)}
        sheets -= children
    return tuple(sorted(sheets))


def read_profile(root: Path) -> dict:
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


@dataclass(frozen=True)
class ProjectPaths:
    """Select one native project family, retaining its basename and sheet structure."""

    root: Path
    entry: Path = field(init=False)

    def __post_init__(self) -> None:
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
        if entry is None and (selected := read_profile(root).get("entry")) is not None:
            if not isinstance(selected, str) or Path(selected).name != selected:
                raise ValueError(
                    "Profile entry must be the name of a KiCad file in the project directory"
                )
            entry = root / selected
            if not entry.is_file():
                raise ValueError(f"Profile entry is missing: {entry}")
        if entry is None:
            entries = project_entries(root)
            if len(entries) > 1:
                names = ", ".join(item.name for item in entries)
                raise ValueError(
                    f"Multiple KiCad projects in {root}: {names}. Select a native file with --project."
                )
            entry = entries[0] if entries else root / f"{root.name}.kicad_pro"
        entry = _contained(root, entry)
        if entry.suffix.lower() not in NATIVE_SUFFIXES:
            raise ValueError(f"Expected a KiCad project, board, or schematic: {entry}")
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "entry", entry)

    @property
    def schematic(self) -> Path:
        return self.entry.with_suffix(".kicad_sch")

    @property
    def board(self) -> Path:
        return self.entry.with_suffix(".kicad_pcb")

    @property
    def settings(self) -> Path:
        return self.entry.with_suffix(".kicad_pro")

    @property
    def rules(self) -> Path:
        return self.entry.with_suffix(".kicad_dru")

    @property
    def review(self) -> Path:
        return self.root / "deliverables" / "review"

    @property
    def sources(self) -> tuple[Path, ...]:
        return self.schematic, self.board, self.settings, self.rules

    def sheets(self) -> tuple[Path, ...]:
        pending = [self.schematic] if self.schematic.is_file() else []
        visited = set()
        while pending:
            document = pending.pop()
            if document in visited:
                continue
            if not document.is_file():
                raise ValueError(f"Missing schematic sheet: {document}")
            visited.add(document)
            pending.extend(_sheet_references(document, self.root))
        return tuple(sorted(visited))

    def audit_inputs(self) -> tuple[Path, ...]:
        """Find documents, the selected hierarchy, and project-local libraries."""
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
                        target = _contained(self.root, self.root / value[12:])
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
        return tuple(sorted(_contained(self.root, path) for path in paths))


def declared_pairs(
    root: Path, native_nets: set[str]
) -> tuple[tuple[str, str], ...] | None:
    profile = read_profile(root)
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


def declared_high_speed(root: Path, native_nets: set[str]) -> set[str] | None:
    pairs = declared_pairs(root, native_nets)
    return None if pairs is None else {name for pair in pairs for name in pair}


def native_path(location: Path, suffix: str) -> Path:
    """Locate the design on paths already passed through an operation's selector."""
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
        entries = project_entries(root)
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


@dataclass(frozen=True)
class SourceSnapshot:
    """Bind operations to the exact saved files and dependency inventory."""

    project: ProjectPaths
    hashes: dict[str, str]

    @classmethod
    def capture(cls, project: ProjectPaths) -> SourceSnapshot:
        return cls(
            project,
            {
                path.relative_to(project.root).as_posix(): sha(path)
                for path in project.audit_inputs()
            },
        )

    def verify_unchanged(self) -> None:
        if self.hashes != self.capture(self.project).hashes:
            raise RuntimeError("Saved project inputs changed; discard this result.")
