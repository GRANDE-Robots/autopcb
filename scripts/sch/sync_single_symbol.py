#!/usr/bin/env python3
"""sync_single_symbol.py: copy one normalized embedded symbol back to its library.

KiCad embeds a copy of each placed symbol in a schematic.  A presentation
normalizer may deliberately update that embedded definition (for example to
apply the project-wide block width and 30 mil pin-text rules).  This helper
copies exactly one such definition into the project symbol library so ERC does
not leave a stale-library warning behind.  It only touches the named symbol,
writes atomically, and defaults to a no-write comparison.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from sch.schematic_io import FileDigest, SiblingTempWriter


class SymbolForms:
    """Locates one named ``(symbol ...)`` form in KiCad S-expression text."""

    @staticmethod
    def balanced_form(text: str, start: int) -> tuple[int, int]:
        """Return [start, end) for one S-expression, respecting quoted strings.

        Raises ValueError when ``start`` is not an opening parenthesis or the
        expression is not closed.
        """
        if start >= len(text) or text[start] != "(":
            raise ValueError("S-expression must start at an opening parenthesis")
        depth = 0
        quoted = False
        escaped = False
        for index in range(start, len(text)):
            character = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
                continue
            if character == '"':
                quoted = True
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    return start, index + 1
        raise ValueError("unterminated S-expression")

    @classmethod
    def find_symbol_form(cls, text: str, symbol_name: str) -> tuple[int, int]:
        """Return [start, end) of the first ``(symbol "<symbol_name>" ...)`` form.

        Raises ValueError when no symbol has that exact name.
        """
        pattern = re.compile(r'\(symbol\s+"((?:\\.|[^"\\])*)"')
        for match in pattern.finditer(text):
            if match.group(1) == symbol_name:
                return cls.balanced_form(text, match.start())
        raise ValueError(f"symbol {symbol_name!r} was not found")


class SingleSymbolSyncCommand:
    """Command-line entry: copy one embedded symbol definition into a library."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Compare, and with ``--apply`` copy, one embedded symbol into a library.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 after printing a record of the paths, names, whether
            the library differs and its digest before (and after, when written).
            Exits through the parser when either file is missing. Raises
            ValueError when a symbol is not found and RuntimeError when the
            embedded form does not start with the requested name.
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("schematic", type=Path)
        parser.add_argument("library", type=Path)
        parser.add_argument("--embedded-name", required=True)
        parser.add_argument("--library-name", required=True)
        parser.add_argument("--apply", action="store_true")
        args = parser.parse_args(argv)

        schematic = args.schematic.resolve()
        library = args.library.resolve()
        if not schematic.is_file() or not library.is_file():
            parser.error("schematic and library must both exist")

        source_text = schematic.read_text(encoding="utf-8")
        library_text = library.read_text(encoding="utf-8")
        source_start, source_end = SymbolForms.find_symbol_form(
            source_text, args.embedded_name
        )
        library_start, library_end = SymbolForms.find_symbol_form(
            library_text, args.library_name
        )
        normalized = source_text[source_start:source_end]
        expected_prefix = f'(symbol "{args.embedded_name}"'
        replacement_prefix = f'(symbol "{args.library_name}"'
        if not normalized.startswith(expected_prefix):
            raise RuntimeError("embedded symbol name did not match the requested name")
        replacement = replacement_prefix + normalized[len(expected_prefix) :]
        changed = library_text[library_start:library_end] != replacement
        payload = {
            "schematic": str(schematic),
            "library": str(library),
            "embedded_name": args.embedded_name,
            "library_name": args.library_name,
            "library_changed": changed,
            "mode": "apply" if args.apply else "check",
            "library_sha256_before": FileDigest.sha256(library),
        }
        if args.apply and changed:
            SiblingTempWriter.write(
                library,
                library_text[:library_start] + replacement + library_text[library_end:],
            )
            payload["library_sha256_after"] = FileDigest.sha256(library)
        print(payload)
        return 0


main = SingleSymbolSyncCommand()


if __name__ == "__main__":
    raise SystemExit(main())
