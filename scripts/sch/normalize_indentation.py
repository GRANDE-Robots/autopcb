#!/usr/bin/env python3
"""normalize_indentation.py: re-indent KiCad S-expression files by nesting depth.

This tool changes no atoms, strings, coordinates, properties, wiring, library
content, page setup, or electrical data.  It derives indentation directly from
quoted-string-safe parenthesis depth, writes atomically, and is intended to
repair formatting drift that can confuse structural validators after a series
of normal automated edits.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sch.schematic_io import AtomicTextWriter, ExactText, FileDigest

TEMP_SUFFIX = ".indent.tmp"


class SExpressionIndentation:
    """Rewrites the leading whitespace of S-expression lines from their depth."""

    @staticmethod
    def structural_delta(text: str) -> int:
        """Return the quote-safe parenthesis delta for one physical line.

        Raises ValueError when the line ends inside a quoted string.
        """
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

    @classmethod
    def canonicalize(cls, source: str) -> tuple[str, int]:
        """Return canonically indented source and its changed-line count.

        Inputs:
            source: Complete S-expression text; CRLF is kept when present.
        Outputs:
            The re-indented text and the number of lines whose indentation
            changed. Raises ValueError for a closing parenthesis before the
            root, an unterminated expression or an unterminated string.
        """
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
            depth += cls.structural_delta(content)
            if depth < 0:
                raise ValueError(f"line {number}: parenthesis depth became negative")
        if depth:
            raise ValueError(f"unterminated expression depth {depth}")
        output = newline.join(result)
        if ends_newline:
            output += newline
        return output, changed


class IndentationCommand:
    """Command-line entry: check or apply canonical indentation of one file."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Report the indentation lines that differ and optionally rewrite them.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0. Prints the changed-line count and the input digest,
            and with ``--apply`` replaces the file atomically and prints the
            output digest.
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("path", type=Path)
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--check", action="store_true")
        mode.add_argument("--apply", action="store_true")
        args = parser.parse_args(argv)
        source = ExactText.read(args.path)
        before = FileDigest.sha256(args.path)
        normalized, changed = SExpressionIndentation.canonicalize(source)
        print(f"changed indentation lines: {changed}")
        print(f"input SHA256: {before}")
        if args.apply:
            AtomicTextWriter(TEMP_SUFFIX).write(args.path, normalized)
            print(f"output SHA256: {FileDigest.sha256(args.path)}")
            print("applied atomically")
        else:
            print("check only; no KiCad file changed")
        return 0


main = IndentationCommand()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"indent normalization aborted: {error}")
        raise SystemExit(2) from None
