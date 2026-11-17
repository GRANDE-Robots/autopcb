"""Plan precise, source-preserving edits described by JSON campaigns."""

from __future__ import annotations

from core.project import native_path

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from core.paths import project_storage
from typing import Any

from core.kicad_sexpr import Form, parse, quote

SCHEMA = 1
CAMPAIGN_NAME = re.compile(r"^\d{8}_\d{6}\.json$")
DESIGN_SUFFIXES = {".kicad_pcb", ".kicad_sch"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _number(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("Coordinates and angles must be finite numbers")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"Invalid number: {value!r}") from error
    if not number.is_finite():
        raise ValueError(f"Invalid number: {value!r}")
    result = format(number, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if Decimal(result) == 0 else result


def _one(forms: list[Form] | tuple[Form, ...], description: str) -> Form:
    if len(forms) != 1:
        raise ValueError(f"Expected exactly one {description}; found {len(forms)}")
    return forms[0]


def _property(form: Form, name: str) -> Form:
    return _one(
        [
            child
            for child in form.children("property")
            if len(child.atoms()) >= 2 and child.atoms()[1].value == name
        ],
        f"property {name!r}",
    )


def _field_value(form: Form, name: str) -> str | None:
    matches = form.children(name)
    if len(matches) != 1 or len(matches[0].atoms()) < 2:
        return None
    return matches[0].atoms()[1].value


@dataclass(frozen=True, slots=True)
class _TargetIndex:
    top: dict[str, tuple[Form, ...]]
    uuid: dict[tuple[str, str], tuple[Form, ...]]


def _build_target_index(root: Form, changes: list[dict[str, Any]]) -> _TargetIndex:
    """Index only targets requested by this campaign, with one tree traversal."""
    top_kinds: set[str] = set()
    requested: set[tuple[str, str]] = set()
    for change in changes:
        target = change.get("target")
        if not isinstance(target, dict):
            continue
        kind, identifier = target.get("kind"), target.get("uuid")
        if not isinstance(kind, str) or not kind or kind == "root":
            continue
        if identifier is None:
            top_kinds.add(kind)
        elif isinstance(identifier, str):
            requested.add((kind, identifier))
    top = {kind: root.children(kind) for kind in top_kinds}
    by_uuid: dict[tuple[str, str], list[Form]] = {key: [] for key in requested}
    if requested:
        kinds = {kind for kind, _ in requested}
        for form in root.descendants():
            if form.name in kinds:
                identifier = _field_value(form, "uuid")
                key = (form.name, identifier)
                if key in by_uuid:
                    by_uuid[key].append(form)
    return _TargetIndex(top, {key: tuple(forms) for key, forms in by_uuid.items()})


def _select(
    root: Form, target: dict[str, Any], index: _TargetIndex | None = None
) -> Form:
    kind = target.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ValueError("Every target needs a kind")
    reference, uuid = target.get("reference"), target.get("uuid")
    if reference is not None and not isinstance(reference, str):
        raise ValueError("Target reference must be text")
    if uuid is not None and not isinstance(uuid, str):
        raise ValueError("Target UUID must be text")
    if kind == "root":
        if reference is not None or uuid is not None:
            raise ValueError("The root selector does not take reference or UUID")
        return root
    # References identify placed, top-level objects. UUIDs can also identify
    # nested objects such as an individual pad, field, or graphic.
    if index is None:
        pool = root.descendants() if uuid is not None else iter(root.children())
    else:
        pool = (
            index.uuid.get((kind, uuid), ())
            if uuid is not None
            else index.top.get(kind, ())
        )
    matches = []
    for form in pool:
        if form.name != kind:
            continue
        if uuid is not None and _field_value(form, "uuid") != uuid:
            continue
        if reference is not None:
            try:
                prop = _property(form, "Reference")
            except ValueError:
                continue
            atoms = prop.atoms()
            if len(atoms) < 3 or atoms[2].value != reference:
                continue
        matches.append(form)
    return _one(matches, f"{kind} matching {target}")


def _follow(form: Form, path: object) -> Form:
    if not isinstance(path, list):
        raise ValueError("Expression path must be a list")
    for step in path:
        if isinstance(step, str):
            name, first, index = step, None, None
        elif isinstance(step, dict):
            if set(step) - {"name", "first", "index"}:
                raise ValueError(f"Unknown path selector: {step}")
            name, first, index = step.get("name"), step.get("first"), step.get("index")
        else:
            raise ValueError(f"Invalid path selector: {step!r}")
        if not isinstance(name, str) or not name:
            raise ValueError("Each path selector needs a name")
        candidates = list(form.children(name))
        if first is not None:
            if not isinstance(first, str):
                raise ValueError("Path selector 'first' must be text")
            candidates = [
                child
                for child in candidates
                if len(child.atoms()) >= 2 and child.atoms()[1].value == first
            ]
        if index is not None:
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ValueError("Path selector index must be a nonnegative integer")
            if index >= len(candidates):
                raise ValueError(f"No {name!r} child at index {index}")
            form = candidates[index]
        else:
            form = _one(candidates, f"{name!r} child")
    return form


@dataclass(frozen=True, slots=True)
class Edit:
    start: int
    end: int
    before: str
    after: str
    description: str


@dataclass(frozen=True, slots=True)
class Plan:
    """A complete, reviewable edit against one exact saved design file."""

    source: Path
    before: bytes
    after: bytes
    edits: tuple[Edit, ...]

    @property
    def source_sha256(self) -> str:
        return digest(self.before)

    @property
    def result_sha256(self) -> str:
        return digest(self.after)


class CampaignPlanner:
    """Find objects and edit their position, properties, or expressions."""

    def __init__(self, project: Path):
        self.project = project.resolve()

    def document_path(self, document: str) -> Path:
        if not isinstance(document, str) or not document:
            raise ValueError("Each change needs a document")
        roles = {"pcb": ".kicad_pcb", "schematic": ".kicad_sch"}
        relative = Path(native_path(self.project, roles[document]).name if document in roles else document)
        if relative.is_absolute():
            raise ValueError("Document paths must be relative to the project")
        path = (self.project / relative).resolve()
        if (
            not path.is_relative_to(self.project)
            or path.suffix.lower() not in DESIGN_SUFFIXES
        ):
            raise ValueError(
                f"Document is outside the project or unsupported: {document}"
            )
        if not path.is_file():
            raise ValueError(f"Design file does not exist: {path}")
        return path

    @staticmethod
    def _edit(
        source: str,
        root: Form,
        change: dict[str, Any],
        index: _TargetIndex | None = None,
    ) -> Edit:
        target = change.get("target")
        if not isinstance(target, dict):
            raise ValueError("Every change needs a target object")
        item = _select(root, target, index)
        operation = change.get("op")
        label = f"{operation} {target}"
        if operation == "set_at":
            nested_in_footprint = root.name == "kicad_pcb" and any(
                footprint.start < item.start and item.end < footprint.end
                for footprint in root.children("footprint")
            )
            if (
                nested_in_footprint
                and change.get("coordinate_space") != "footprint_local"
            ):
                raise ValueError(
                    "Positions inside a PCB footprint use footprint-local coordinates; "
                    "use 'pcb move-text' for a bottom-silkscreen field, or explicitly "
                    "specify coordinate_space='footprint_local'."
                )
            at = _one(item.children("at"), "position")
            atoms = at.atoms()
            if len(atoms) not in (3, 4):
                raise ValueError("Position must contain X, Y, and optionally rotation")
            x, y = _number(change.get("x")), _number(change.get("y"))
            angle = change.get("rotation")
            if (
                root.name == "kicad_pcb"
                and item.name == "footprint"
                and angle is not None
            ):
                old_angle = Decimal(atoms[3].value) if len(atoms) == 4 else Decimal(0)
                new_angle = Decimal(_number(angle))
                if (new_angle - old_angle) % Decimal(360):
                    raise ValueError(
                        "Changing a PCB footprint's rotation requires native KiCad geometry; "
                        "use 'pcb move' and validate its attached copper."
                    )
            if angle is None:
                angle_text = (
                    " " + source[atoms[3].start : atoms[3].end]
                    if len(atoms) == 4
                    else ""
                )
            else:
                angle_text = " " + _number(angle)
            before = source[at.start : at.end]
            if (
                Decimal(atoms[1].value) == Decimal(x)
                and Decimal(atoms[2].value) == Decimal(y)
                and (
                    angle is None
                    or (
                        len(atoms) == 4
                        and Decimal(atoms[3].value) == Decimal(_number(angle))
                    )
                )
            ):
                after = before
            else:
                after = f"(at {x} {y}{angle_text})"
            return Edit(at.start, at.end, before, after, label)
        if operation == "set_property":
            name, value = change.get("name"), change.get("value")
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError("Property name and value must be text")
            atoms = _property(item, name).atoms()
            if len(atoms) < 3:
                raise ValueError(f"Property {name!r} has no value")
            atom = atoms[2]
            before = source[atom.start : atom.end]
            after = before if atom.value == value else quote(value)
            return Edit(
                atom.start, atom.end, before, after, label + f" property {name!r}"
            )
        if operation not in {"replace", "insert", "remove"}:
            raise ValueError(f"Unknown campaign operation: {operation!r}")
        path = change.get("path", [])
        form = _follow(item, path)
        if operation == "remove":
            if form is root:
                raise ValueError("Cannot remove the design's root expression")
            return Edit(form.start, form.end, source[form.start : form.end], "", label)
        expression = change.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError(f"{operation} requires one S-expression")
        expression = expression.strip()
        parse(expression)
        if operation == "replace":
            return Edit(
                form.start, form.end, source[form.start : form.end], expression, label
            )
        if form.end <= form.start + 1:
            raise ValueError("Invalid insertion target")
        line = source.rfind("\n", form.start, form.end - 1)
        closing_indent = source[line + 1 : form.end - 1] if line >= 0 else ""
        if line >= 0 and (not closing_indent or closing_indent.isspace()):
            newline = "\r\n" if "\r\n" in source else "\n"
            expression = expression.replace("\r\n", "\n").replace("\r", "\n")
            insertion = (
                closing_indent + "\t" + expression.replace("\n", newline) + newline
            )
            return Edit(line + 1, line + 1, "", insertion, label)
        return Edit(form.end - 1, form.end - 1, "", " " + expression, label)

    def plan(self, document: str, changes: list[dict[str, Any]]) -> Plan:
        path = self.document_path(document)
        before = path.read_bytes()
        source = before.decode("utf-8")
        root = parse(source)
        expected = "kicad_pcb" if path.suffix.lower() == ".kicad_pcb" else "kicad_sch"
        if root.name != expected:
            raise ValueError(f"Expected {expected!r} root, found {root.name!r}")
        index = _build_target_index(root, changes) if len(changes) > 1 else None
        edits = [self._edit(source, root, change, index) for change in changes]
        del index, root  # Do not keep source forms while validating the result.
        edits = [edit for edit in edits if edit.before != edit.after]
        ordered = sorted(
            enumerate(edits), key=lambda pair: (pair[1].start, pair[1].end)
        )
        for (_, left), (_, right) in zip(ordered, ordered[1:]):
            if left.end > right.start or (
                left.start == left.end == right.start and right.end > right.start
            ):
                raise ValueError(
                    f"Overlapping campaign changes: {left.description}; {right.description}"
                )
        pieces: list[str] = []
        cursor = 0
        for _, edit in ordered:
            pieces.append(source[cursor : edit.start])
            pieces.append(edit.after)
            cursor = edit.end
        pieces.append(source[cursor:])
        updated = "".join(pieces)
        if parse(updated).name != expected:
            raise ValueError("Campaign changed the design's root expression")
        return Plan(path, before, updated.encode("utf-8"), tuple(edits))


class CampaignFile:
    """Create and extend timestamped campaign JSON files."""

    def __init__(self, project: Path):
        root = project.resolve()
        storage = project_storage(root)
        self.directory = storage.campaigns.resolve()
        self.relative_root = storage.workspace or storage.legacy
        if not self.directory.is_relative_to(storage.workspace or root):
            raise ValueError("Campaign directory must remain inside its workspace or project")

    def path(self, name: str) -> Path:
        path = Path(name)
        if not path.is_absolute():
            # Accept either the generated filename or its project-relative path.
            path = (
                self.directory / path.name
                if len(path.parts) == 1
                else self.relative_root / path
            )
        path = path.resolve()
        if path.parent != self.directory or not CAMPAIGN_NAME.fullmatch(path.name):
            raise ValueError(
                "Campaign must be a YYYYMMDD_HHMMSS.json file in config/campaigns"
            )
        return path

    def create(
        self,
        name: str,
        *,
        routing_validation: dict[str, Any] | None = None,
        changes: list[dict[str, Any]] | None = None,
    ) -> Path:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Campaign name cannot be blank")
        self.directory.mkdir(parents=True, exist_ok=True)
        now = datetime.now().astimezone()
        path = self.directory / (now.strftime("%Y%m%d_%H%M%S") + ".json")
        if changes is not None and (
            not isinstance(changes, list)
            or any(not isinstance(change, dict) for change in changes)
        ):
            raise ValueError("Initial campaign changes must be a list of objects")
        content = {
            "schema": SCHEMA,
            "name": name.strip(),
            "created_at": now.isoformat(timespec="seconds"),
            "changes": list(changes or []),
        }
        if routing_validation is not None:
            if not isinstance(routing_validation, dict):
                raise ValueError("Routing validation metadata must be an object")
            content["routing_validation"] = routing_validation
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(content, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        return path

    def read(self, name: str) -> tuple[Path, dict[str, Any], bytes]:
        path = self.path(name)
        original = path.read_bytes()
        data = json.loads(original.decode("utf-8-sig"))
        if (
            not isinstance(data, dict)
            or data.get("schema") != SCHEMA
            or not isinstance(data.get("changes"), list)
        ):
            raise ValueError("Invalid campaign schema or changes list")
        if any(not isinstance(change, dict) for change in data["changes"]):
            raise ValueError("Campaign changes must be objects")
        return path, data, original

    def append(self, name: str, changes: list[dict[str, Any]]) -> Path:
        path, data, original = self.read(name)
        if not changes:
            raise ValueError("No campaign changes supplied")
        data["changes"].extend(changes)
        content = (
            json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        ).encode("utf-8")
        descriptor, staged_name = tempfile.mkstemp(
            prefix=".campaign-", suffix=".tmp", dir=path.parent
        )
        staged = Path(staged_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if path.read_bytes() != original:
                raise RuntimeError(
                    "Campaign changed while editing; refusing to overwrite it"
                )
            os.replace(staged, path)
        finally:
            if staged.exists():
                staged.unlink()
        return path

    @staticmethod
    def move(
        document: str,
        kind: str,
        reference: str,
        x: object,
        y: object,
        rotation: object | None = None,
    ) -> dict[str, Any]:
        change: dict[str, Any] = {
            "document": document,
            "target": {"kind": kind, "reference": reference},
            "op": "set_at",
            "x": _number(x),
            "y": _number(y),
        }
        if rotation is not None:
            change["rotation"] = _number(rotation)
        return change

    @classmethod
    def grid(
        cls,
        document: str,
        kind: str,
        prefix: str,
        first: int,
        count: int,
        rows: int,
        origin: tuple[float, float],
        spacing: tuple[float, float],
        order: str = "column",
    ) -> list[dict[str, Any]]:
        if not prefix or first < 1 or count < 1 or rows < 1:
            raise ValueError(
                "Grid prefix, first number, count, and rows must be positive"
            )
        if order not in {"column", "row"}:
            raise ValueError("Grid order must be 'column' or 'row'")
        if not all(math.isfinite(value) for value in (*origin, *spacing)):
            raise ValueError("Grid coordinates must be finite")
        if (count > rows and spacing[0] == 0) or (
            min(count, rows) > 1 and spacing[1] == 0
        ):
            raise ValueError("Grid spacing would overlap objects")
        x0, y0 = (Decimal(str(value)) for value in origin)
        dx, dy = (Decimal(str(value)) for value in spacing)
        columns = (count + rows - 1) // rows
        return [
            cls.move(
                document,
                kind,
                f"{prefix}{first + i}",
                str(x0 + (i // rows if order == "column" else i % columns) * dx),
                str(y0 + (i % rows if order == "column" else i // columns) * dy),
            )
            for i in range(count)
        ]
