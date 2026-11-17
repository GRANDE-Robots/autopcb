"""Find KiCad objects in saved sources without constructing a whole-board tree.

Default output is a small JSON summary. Use --raw for one exact expression
when preparing a campaign; the command never edits a design.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterator

from core.kicad_sexpr import Form, parse

KIND = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SPACE = b" \t\r\n"
SYNTAX = re.compile(rb'[()"]')


def _spans(data: bytes, kind: str, nested: bool) -> Iterator[tuple[int, int]]:
    """Yield matching complete expression spans while respecting quoted strings."""
    needle = kind.encode("ascii")
    depth = 0
    active: list[tuple[int, int]] = []
    quoted = False
    quote_start = 0
    root_seen = False
    root_end = 0
    for match in SYNTAX.finditer(data):
        index = match.start()
        token = data[index]
        if token == 34:
            if quoted:
                preceding = index - 1
                while preceding > quote_start and data[preceding] == 92:
                    preceding -= 1
                if (index - preceding - 1) % 2 == 0:
                    quoted = False
            else:
                if depth == 0:
                    raise ValueError("Quoted text appears outside the document root")
                quoted = True
                quote_start = index
            continue
        if quoted:
            continue
        if token == 40:
            if depth == 0:
                if root_seen or data[:index].lstrip(b"\xef\xbb\xbf" + SPACE):
                    raise ValueError("Multiple roots or text outside the document root")
                root_seen = True
            start = index + 1
            while start < len(data) and data[start] in SPACE:
                start += 1
            stop = start
            while stop < len(data) and data[stop] not in SPACE + b'()"':
                stop += 1
            if start == stop:
                raise ValueError("An expression has no name")
            is_root = depth == 0 and kind == "root"
            is_match = (
                depth >= 1 and (nested or depth == 1) and data[start:stop] == needle
            )
            if is_root or is_match:
                active.append((depth, index))
            depth += 1
        else:
            if depth <= 0:
                raise ValueError("Unexpected closing parenthesis")
            depth -= 1
            if active and active[-1][0] == depth:
                _, start = active.pop()
                yield start, index + 1
            if depth == 0:
                root_end = index + 1
    if quoted:
        raise ValueError("Unterminated quoted string")
    if not root_seen or depth or data[root_end:].strip(SPACE):
        raise ValueError("Unclosed root or trailing text")


def _field(form: Form, name: str) -> list[str]:
    children = form.children(name)
    return (
        [atom.value for atom in children[0].atoms()[1:]] if len(children) == 1 else []
    )


def _properties(form: Form) -> dict[str, str]:
    result = {}
    for prop in form.children("property"):
        atoms = prop.atoms()
        if len(atoms) >= 3 and atoms[1].value in {"Reference", "Value"}:
            result[atoms[1].value] = atoms[2].value
    return result


def _summary(form: Form) -> dict[str, Any]:
    result: dict[str, Any] = {"kind": form.name}
    atoms = form.atoms()
    if len(atoms) >= 2:
        result["identifier"] = atoms[1].value
    for name in ("uuid", "at", "start", "end", "layer", "net", "lib_id", "width"):
        values = _field(form, name)
        if values:
            result[name] = values[0] if len(values) == 1 else values
    for name, value in _properties(form).items():
        result[name.lower()] = value
    return result


def _hash_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect(
    project: Path,
    document: str,
    kind: str | None = None,
    *,
    reference: str | None = None,
    references: list[str] | tuple[str, ...] | None = None,
    uuid: str | None = None,
    identifier: str | None = None,
    limit: int = 5,
    offset: int = 0,
    nested: bool = False,
    raw: bool = False,
) -> dict[str, Any]:
    """Inspect only matching expressions, and reject a changed saved source."""
    project = project.resolve()
    if not isinstance(document, str) or not document:
        raise ValueError("Document must be a project-relative KiCad file")
    aliases = {"pcb": ".kicad_pcb", "schematic": ".kicad_sch"}
    if document in aliases:
        matches = sorted(project.glob(f"*{aliases[document]}"))
        if not matches:
            raise ValueError(f"No {document} found in the project root")
        if len(matches) > 1:
            raise ValueError(
                f"Multiple {document} files; select a project-relative document"
            )
        relative = matches[0].relative_to(project)
    else:
        relative = Path(document)
    if relative.is_absolute():
        raise ValueError("Document must be project-relative")
    path = (project / relative).resolve()
    if not path.is_relative_to(project) or not path.is_file():
        raise ValueError("Saved document is missing or outside the project")
    types = {
        ".kicad_pcb": ("kicad_pcb", "footprint"),
        ".kicad_sch": ("kicad_sch", "symbol"),
        ".kicad_sym": ("kicad_symbol_lib", "symbol"),
        ".kicad_mod": ("footprint", "pad"),
        ".kicad_wks": ("kicad_wks", "line"),
    }
    tables = {
        "fp-lib-table": ("fp_lib_table", "lib"),
        "sym-lib-table": ("sym_lib_table", "lib"),
    }
    selected_type = tables.get(path.name, types.get(path.suffix.lower()))
    if selected_type is None:
        raise ValueError("Unsupported KiCad S-expression document")
    expected_root, default_kind = selected_type
    kind = kind or default_kind
    if not isinstance(kind, str) or not KIND.fullmatch(kind):
        raise ValueError("Object kind must be a KiCad expression name")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Limit must be an integer between 1 and 100")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("Offset must be a nonnegative integer")
    if reference is not None and references is not None:
        raise ValueError("Use reference or references, not both")
    if reference is not None and (not isinstance(reference, str) or not reference):
        raise ValueError("Reference must be nonempty text")
    if references is not None and (
        not isinstance(references, (list, tuple))
        or not references
        or any(not isinstance(item, str) or not item for item in references)
    ):
        raise ValueError("References must be nonempty text values")
    requested = set(references or ([reference] if reference else []))
    if raw and not (requested or uuid or identifier or kind == "root"):
        raise ValueError("Raw output requires a reference, UUID, identifier or root")
    if raw and offset:
        raise ValueError("Raw output cannot use an offset")
    if uuid is not None and (not isinstance(uuid, str) or not uuid):
        raise ValueError("UUID must be nonempty text")
    if identifier is not None and (not isinstance(identifier, str) or not identifier):
        raise ValueError("Identifier must be nonempty text")
    data = path.read_bytes()
    source_hash = hashlib.sha256(data).hexdigest()
    root = re.match(rb"^(?:\xef\xbb\xbf)?\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)", data)
    if root is None or root.group(1).decode("ascii") != expected_root:
        raise ValueError(f"Expected a {expected_root} root expression")
    ref_bytes = tuple(
        json.dumps(value, ensure_ascii=False)[1:-1].encode("utf-8")
        for value in sorted(requested)
    )
    uuid_bytes = (
        json.dumps(uuid, ensure_ascii=False)[1:-1].encode("utf-8") if uuid else None
    )
    identifier_bytes = (
        json.dumps(identifier, ensure_ascii=False)[1:-1].encode("utf-8")
        if identifier
        else None
    )
    found = 0
    found_references: set[str] = set()
    results = []
    for start, end in _spans(data, kind, nested or bool(uuid)):
        if ref_bytes and not any(
            data.find(value, start, end) >= 0 for value in ref_bytes
        ):
            continue
        if uuid_bytes and data.find(uuid_bytes, start, end) < 0:
            continue
        if identifier_bytes and data.find(identifier_bytes, start, end) < 0:
            continue
        form = None
        if (
            requested
            or uuid
            or identifier
            or (found >= offset and len(results) < limit)
        ):
            form = parse(data[start:end].decode("utf-8"))
        current_reference = _properties(form).get("Reference") if requested else None
        if requested and current_reference not in requested:
            continue
        if uuid and _field(form, "uuid") != [uuid]:
            continue
        if identifier and (
            len(form.atoms()) < 2 or form.atoms()[1].value != identifier
        ):
            continue
        found += 1
        if current_reference is not None:
            found_references.add(current_reference)
        if found > offset and len(results) < limit:
            item = _summary(form)
            if raw:
                item["expression"] = data[start:end].decode("utf-8")
            results.append(item)
    if _hash_file(path) != source_hash:
        raise RuntimeError("Saved design changed during inspection; retry")
    if raw and found != 1:
        raise ValueError(f"Raw output requires exactly one match; found {found}")
    result = {
        "status": (
            "not_found"
            if not found
            else "partial"
            if requested and requested != found_references
            else "ok"
        ),
        "document": document,
        "source_sha256": source_hash,
        "kind": kind,
        "matches": found,
        "returned": len(results),
        "objects": results,
    }
    if offset:
        result["offset"] = offset
    if len(requested) > 1:
        result["missing_references"] = sorted(requested - found_references)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument(
        "--document",
        default="pcb",
        help="pcb, schematic, or a project-relative KiCad S-expression file",
    )
    parser.add_argument("--kind")
    parser.add_argument(
        "--reference", action="append", help="reference to inspect; repeat for a batch"
    )
    parser.add_argument("--uuid")
    parser.add_argument(
        "--identifier",
        help="first expression value, such as a library symbol name or pad number",
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument(
        "--nested", action="store_true", help="include nested expressions"
    )
    parser.add_argument(
        "--raw", action="store_true", help="include the exact expression for one match"
    )
    args = parser.parse_args(argv)
    try:
        result = inspect(
            args.project,
            args.document,
            args.kind,
            references=args.reference,
            uuid=args.uuid,
            identifier=args.identifier,
            limit=args.limit,
            offset=args.offset,
            nested=args.nested,
            raw=args.raw,
        )
    except (OSError, ValueError, RuntimeError, UnicodeError) as error:
        print(
            json.dumps(
                {"status": "error", "message": str(error)}, separators=(",", ":")
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
