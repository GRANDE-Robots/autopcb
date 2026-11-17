#!/usr/bin/env python3
"""Canonicalize only leading indentation in a KiCad S-expression file.

This tool changes no atoms, strings, coordinates, properties, wiring, library
content, page setup, or electrical data.  It derives indentation directly from
quoted-string-safe parenthesis depth, writes atomically, and is intended to
repair formatting drift that can confuse structural validators after a series
of normal automated edits.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path


def structural_delta(text: str) -> int:
    """Return the quote-safe parenthesis delta for one physical line."""
    depth = 0
    quoted = False
    escaped = False
    for char in text:
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
    if quoted:
        raise ValueError("unterminated quoted string")
    return depth


def canonicalize(source: str) -> tuple[str, int]:
    """Return canonically indented source and its changed-line count."""
    newline = "\r\n" if "\r\n" in source else "\n"
    ends_newline = source.endswith(("\n", "\r"))
    depth = 0
    changed = 0
    result: list[str] = []
    for number, raw in enumerate(source.splitlines(), start=1):
        content = raw.strip()
        if not content:
            result.append("")
            continue
        leading_close = len(content) - len(content.lstrip(")"))
        indent_depth = depth - leading_close
        if indent_depth < 0:
            raise ValueError(f"line {number}: closing parenthesis before root")
        rendered = "\t" * indent_depth + content
        if rendered != raw:
            changed += 1
        result.append(rendered)
        depth += structural_delta(content)
        if depth < 0:
            raise ValueError(f"line {number}: parenthesis depth became negative")
    if depth:
        raise ValueError(f"unterminated expression depth {depth}")
    output = newline.join(result)
    if ends_newline:
        output += newline
    return output, changed


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_atomic(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".indent.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with args.path.open("r", encoding="utf-8", newline="") as handle:
        source = handle.read()
    before = sha256(args.path)
    normalized, changed = canonicalize(source)
    print(f"changed indentation lines: {changed}")
    print(f"input SHA256: {before}")
    if args.apply:
        write_atomic(args.path, normalized)
        print(f"output SHA256: {sha256(args.path)}")
        print("applied atomically")
    else:
        print("check only; no KiCad file changed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"indent normalization aborted: {error}")
        raise SystemExit(2) from None
