#!/usr/bin/env python3
"""sync_symbol_libraries.py: copy accepted embedded artwork into project libraries.

KiCad embeds a snapshot of every library symbol in ``.kicad_sch``.  A visual
normalization changes that snapshot deliberately, which otherwise produces an
ERC warning that the embedded symbol differs from its project-local source.
This utility copies only the accepted embedded definitions for J, U, and
non-generic D references back to their mapped local ``.kicad_sym`` files.  It
never changes references, pin numbers/types, fields, wires, or nets.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path

from sch.schematic_io import AtomicTextWriter, ExactText
from sch.standardize_block_symbols import SExpressionText, balanced_end

TEMP_SUFFIX = ".symbolsync.tmp"


class LibrarySync:
    """Works out which project library symbols differ from the embedded ones."""

    @staticmethod
    def embedded_definitions(source: str) -> tuple[str, dict[str, str]]:
        """Read the embedded library symbols of a schematic.

        Inputs:
            source: Complete schematic text.
        Outputs:
            The ``(kicad_sch ...)`` root text and each embedded definition by
            library ID. Raises ValueError when the root or its ``lib_symbols``
            block is missing.
        """
        root_start = source.find("(kicad_sch")
        if root_start < 0:
            raise ValueError("missing kicad_sch root")
        root = source[root_start : balanced_end(source, root_start)]
        lib_start = root.find("(lib_symbols")
        if lib_start < 0:
            raise ValueError("missing lib_symbols")
        library = root[lib_start : balanced_end(root, lib_start)]
        definitions: dict[str, str] = {}
        for start, end in SExpressionText.direct_blocks(library, "symbol"):
            block = library[start:end]
            match = re.match(r'\(symbol\s+"([^"]+)"', block)
            if match:
                definitions[match.group(1)] = block
        return root, definitions

    @staticmethod
    def target_library_ids(root: str) -> set[str]:
        """Return library IDs used by the presentation-normalized symbol classes."""
        result: set[str] = set()
        for start, end in SExpressionText.direct_blocks(root, "symbol"):
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

    @staticmethod
    def library_paths(sym_lib_table: Path, project_root: Path) -> dict[str, Path]:
        """Map each project-local library nickname to its file.

        Inputs:
            sym_lib_table: The project's ``sym-lib-table`` file.
            project_root: Folder that ``${KIPRJMOD}`` stands for.
        Outputs:
            Nickname to path for each library whose URI starts with
            ``${KIPRJMOD}/``; other libraries are skipped. Raises ValueError when
            the table has no ``sym_lib_table`` root.
        """
        source = ExactText.read(sym_lib_table)
        start = source.find("(sym_lib_table")
        if start < 0:
            raise ValueError("missing sym_lib_table root")
        root = source[start : balanced_end(source, start)]
        paths: dict[str, Path] = {}
        for child_start, child_end in SExpressionText.direct_blocks(root, "lib"):
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

    @staticmethod
    def root_symbol_blocks(source: str) -> dict[str, tuple[int, int, str]]:
        """Locate each symbol of a ``.kicad_sym`` library.

        Outputs:
            Symbol name to its start offset, end offset and text in ``source``.
            Raises ValueError when the file has no ``kicad_symbol_lib`` root.
        """
        start = source.find("(kicad_symbol_lib")
        if start < 0:
            raise ValueError("missing kicad_symbol_lib root")
        root = source[start : balanced_end(source, start)]
        result: dict[str, tuple[int, int, str]] = {}
        for child_start, child_end in SExpressionText.direct_blocks(root, "symbol"):
            block = root[child_start:child_end]
            name = re.match(r'\(symbol\s+"([^"]+)"', block)
            if name:
                result[name.group(1)] = (start + child_start, start + child_end, block)
        return result

    @staticmethod
    def external_definition(embedded: str, local_name: str) -> str:
        """Rename an embedded definition to the symbol's name inside its library.

        Raises ValueError when the definition has no root symbol name.
        """
        match = re.match(r'(\(symbol\s+")([^"]+)(")', embedded)
        if not match:
            raise ValueError("embedded definition lacks a root symbol name")
        return embedded[: match.start(2)] + local_name + embedded[match.end(2) :]

    @staticmethod
    def normalized_newlines(value: str) -> str:
        """Compare symbol content without treating CRLF/LF provenance as a diff."""
        return value.replace("\r\n", "\n").replace("\r", "\n")

    @classmethod
    def build_updates(
        cls, schematic: Path, sym_lib_table: Path
    ) -> tuple[dict[Path, str], dict[Path, list[str]]]:
        """Compute the library files that must change to match the schematic.

        Inputs:
            schematic: The schematic holding the accepted embedded symbols.
            sym_lib_table: The project's ``sym-lib-table``; its folder is the
                project root.
        Outputs:
            The new text of each library file that changes, and the names of the
            symbols that change in each. Raises ValueError when a target symbol
            has an unsupported library ID, no project-local library, no embedded
            definition or no matching library symbol, or when a generated library
            would be malformed, and FileNotFoundError for a mapped library file
            that does not exist.
        """
        schematic_source = ExactText.read(schematic)
        root, embedded = cls.embedded_definitions(schematic_source)
        # The library table defines the project root.  A hierarchical sheet lives
        # below it, so resolving ${KIPRJMOD} from schematic.parent would point at
        # a nonexistent schematic/libraries tree.
        paths = cls.library_paths(sym_lib_table, sym_lib_table.parent)
        targets = sorted(cls.target_library_ids(root))
        grouped: dict[Path, list[tuple[int, int, str, str]]] = defaultdict(list)
        changed: dict[Path, list[str]] = defaultdict(list)
        for lib_id in targets:
            nickname, separator, local_name = lib_id.partition(":")
            if not separator:
                raise ValueError(f"unsupported library ID: {lib_id}")
            path = paths.get(nickname)
            if not path:
                raise ValueError(
                    f"{lib_id} is not mapped to a project-local KiCad library"
                )
            if not path.exists():
                raise FileNotFoundError(path)
            if lib_id not in embedded:
                raise ValueError(f"missing embedded definition for {lib_id}")
            local_blocks = cls.root_symbol_blocks(ExactText.read(path))
            if local_name not in local_blocks:
                raise ValueError(f"{path.name} lacks symbol {local_name} for {lib_id}")
            start, end, old = local_blocks[local_name]
            replacement = cls.external_definition(embedded[lib_id], local_name)
            if cls.normalized_newlines(old) != cls.normalized_newlines(replacement):
                grouped[path].append((start, end, replacement, local_name))
                changed[path].append(local_name)
        outputs: dict[Path, str] = {}
        for path, replacements in grouped.items():
            source = ExactText.read(path)
            for start, end, replacement, _name in sorted(replacements, reverse=True):
                source = source[:start] + replacement + source[end:]
            # Confirm every written library still has one complete top-level root.
            start = source.find("(kicad_symbol_lib")
            if start < 0 or balanced_end(source, start) != len(source.rstrip()):
                raise ValueError(f"generated malformed library: {path}")
            outputs[path] = source
        return outputs, changed

    @staticmethod
    def find_sym_lib_table(schematic: Path) -> Path:
        """Locate the project table for a root or hierarchical-sheet invocation.

        Outputs:
            The nearest ``sym-lib-table`` in the schematic's folder or any parent,
            else the path where one would sit beside the schematic.
        """
        for directory in (schematic.parent, *schematic.parents):
            candidate = directory / "sym-lib-table"
            if candidate.exists():
                return candidate
        return schematic.with_name("sym-lib-table")


class SymbolLibrarySyncCommand:
    """Command-line entry: synchronize embedded symbols into project libraries."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Report, and with ``--apply`` write, the library symbols that differ.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 after printing the number of libraries and definitions
            to synchronize and the symbols per library. ``--apply`` replaces each
            library file atomically.
        """
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
        args = parser.parse_args(argv)
        table = args.sym_lib_table or LibrarySync.find_sym_lib_table(args.schematic)
        outputs, changed = LibrarySync.build_updates(args.schematic, table)
        print(f"project libraries requiring synchronization: {len(outputs)}")
        print(
            f"embedded definitions requiring synchronization: "
            f"{sum(len(names) for names in changed.values())}"
        )
        for path in sorted(changed):
            print(f"  {path.name}: {', '.join(changed[path])}")
        if args.apply:
            writer = AtomicTextWriter(TEMP_SUFFIX)
            for path, source in outputs.items():
                writer.write(path, source)
            print("applied atomically per project-local library")
        else:
            print("check only; no library changed")
        return 0


main = SymbolLibrarySyncCommand()


if __name__ == "__main__":
    raise SystemExit(main())
