#!/usr/bin/env python3
"""Synchronize accepted embedded J/U/block-D artwork into project libraries.

KiCad embeds a snapshot of every library symbol in ``.kicad_sch``.  A visual
normalization changes that snapshot deliberately, which otherwise produces an
ERC warning that the embedded symbol differs from its project-local source.
This utility copies only the accepted embedded definitions for J, U, and
non-generic D references back to their mapped local ``.kicad_sym`` files.  It
never changes references, pin numbers/types, fields, wires, or nets.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path


def balanced_end(source: str, start: int) -> int:
    """Return one complete quoted-string-safe S-expression's exclusive end."""
    if source[start] != "(":
        raise ValueError(f"expected '(' at {start}")
    depth = 0
    quoted = False
    escaped = False
    for position in range(start, len(source)):
        char = source[position]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return position + 1
    raise ValueError(f"unclosed expression at {start}")


def direct_blocks(source: str, token: str) -> list[tuple[int, int]]:
    """Find direct child S-expressions named *token*."""
    marker = f"({token}"
    result: list[tuple[int, int]] = []
    depth = 0
    quoted = False
    escaped = False
    position = 0
    while position < len(source):
        char = source[position]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            position += 1
            continue
        if char == '"':
            quoted = True
            position += 1
            continue
        if depth == 1 and source.startswith(marker, position):
            end = balanced_end(source, position)
            result.append((position, end))
            position = end
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        position += 1
    return result


def read_exact(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def write_atomic(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".symbolsync.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def embedded_definitions(source: str) -> tuple[str, dict[str, str]]:
    root_start = source.find("(kicad_sch")
    if root_start < 0:
        raise ValueError("missing kicad_sch root")
    root = source[root_start : balanced_end(source, root_start)]
    lib_start = root.find("(lib_symbols")
    if lib_start < 0:
        raise ValueError("missing lib_symbols")
    library = root[lib_start : balanced_end(root, lib_start)]
    definitions: dict[str, str] = {}
    for start, end in direct_blocks(library, "symbol"):
        block = library[start:end]
        match = re.match(r'\(symbol\s+"([^"]+)"', block)
        if match:
            definitions[match.group(1)] = block
    return root, definitions


def target_library_ids(root: str) -> set[str]:
    """Return library IDs used by the presentation-normalized symbol classes."""
    result: set[str] = set()
    for start, end in direct_blocks(root, "symbol"):
        block = root[start:end]
        lib = re.search(r'\(lib_id\s+"([^"]+)"\)', block)
        reference = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', block)
        if not lib or not reference:
            continue
        ref = reference.group(1)
        lib_id = lib.group(1)
        if ref.startswith(("J", "U")) or (
            ref.startswith("D") and not lib_id.startswith("Device:")
        ):
            result.add(lib_id)
    return result


def library_paths(sym_lib_table: Path, project_root: Path) -> dict[str, Path]:
    source = read_exact(sym_lib_table)
    start = source.find("(sym_lib_table")
    if start < 0:
        raise ValueError("missing sym_lib_table root")
    root = source[start : balanced_end(source, start)]
    paths: dict[str, Path] = {}
    for child_start, child_end in direct_blocks(root, "lib"):
        block = root[child_start:child_end]
        name = re.search(r'\(name\s+"([^"]+)"\)', block)
        uri = re.search(r'\(uri\s+"([^"]+)"\)', block)
        if not name or not uri:
            continue
        value = uri.group(1)
        prefix = "${KIPRJMOD}/"
        if not value.startswith(prefix):
            continue
        paths[name.group(1)] = project_root / value[len(prefix) :]
    return paths


def root_symbol_blocks(source: str) -> dict[str, tuple[int, int, str]]:
    start = source.find("(kicad_symbol_lib")
    if start < 0:
        raise ValueError("missing kicad_symbol_lib root")
    root = source[start : balanced_end(source, start)]
    result: dict[str, tuple[int, int, str]] = {}
    for child_start, child_end in direct_blocks(root, "symbol"):
        block = root[child_start:child_end]
        name = re.match(r'\(symbol\s+"([^"]+)"', block)
        if name:
            result[name.group(1)] = (start + child_start, start + child_end, block)
    return result


def external_definition(embedded: str, local_name: str) -> str:
    match = re.match(r'(\(symbol\s+")([^"]+)(")', embedded)
    if not match:
        raise ValueError("embedded definition lacks a root symbol name")
    return embedded[: match.start(2)] + local_name + embedded[match.end(2) :]


def normalized_newlines(value: str) -> str:
    """Compare symbol content without treating CRLF/LF provenance as a diff."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def build_updates(
    schematic: Path, sym_lib_table: Path
) -> tuple[dict[Path, str], dict[Path, list[str]]]:
    schematic_source = read_exact(schematic)
    root, embedded = embedded_definitions(schematic_source)
    # The library table defines the project root.  A hierarchical sheet lives
    # below it, so resolving ${KIPRJMOD} from schematic.parent would point at
    # a nonexistent schematic/libraries tree.
    paths = library_paths(sym_lib_table, sym_lib_table.parent)
    targets = sorted(target_library_ids(root))
    grouped: dict[Path, list[tuple[int, int, str, str]]] = defaultdict(list)
    changed: dict[Path, list[str]] = defaultdict(list)
    for lib_id in targets:
        nickname, separator, local_name = lib_id.partition(":")
        if not separator:
            raise ValueError(f"unsupported library ID: {lib_id}")
        path = paths.get(nickname)
        if not path:
            raise ValueError(f"{lib_id} is not mapped to a project-local KiCad library")
        if not path.exists():
            raise FileNotFoundError(path)
        if lib_id not in embedded:
            raise ValueError(f"missing embedded definition for {lib_id}")
        local_blocks = root_symbol_blocks(read_exact(path))
        if local_name not in local_blocks:
            raise ValueError(f"{path.name} lacks symbol {local_name} for {lib_id}")
        start, end, old = local_blocks[local_name]
        replacement = external_definition(embedded[lib_id], local_name)
        if normalized_newlines(old) != normalized_newlines(replacement):
            grouped[path].append((start, end, replacement, local_name))
            changed[path].append(local_name)
    outputs: dict[Path, str] = {}
    for path, replacements in grouped.items():
        source = read_exact(path)
        for start, end, replacement, _name in sorted(replacements, reverse=True):
            source = source[:start] + replacement + source[end:]
        # Confirm every written library still has one complete top-level root.
        start = source.find("(kicad_symbol_lib")
        if start < 0 or balanced_end(source, start) != len(source.rstrip()):
            raise ValueError(f"generated malformed library: {path}")
        outputs[path] = source
    return outputs, changed


def find_sym_lib_table(schematic: Path) -> Path:
    """Locate the project table for a root or hierarchical-sheet invocation."""
    for directory in (schematic.parent, *schematic.parents):
        candidate = directory / "sym-lib-table"
        if candidate.exists():
            return candidate
    return schematic.with_name("sym-lib-table")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schematic", type=Path)
    parser.add_argument(
        "--sym-lib-table",
        type=Path,
        help="project sym-lib-table; defaults beside the schematic",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    table = args.sym_lib_table or find_sym_lib_table(args.schematic)
    outputs, changed = build_updates(args.schematic, table)
    print(f"project libraries requiring synchronization: {len(outputs)}")
    print(
        f"embedded definitions requiring synchronization: "
        f"{sum(len(names) for names in changed.values())}"
    )
    for path in sorted(changed):
        print(f"  {path.name}: {', '.join(changed[path])}")
    if args.apply:
        for path, source in outputs.items():
            write_atomic(path, source)
        print("applied atomically per project-local library")
    else:
        print("check only; no library changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
