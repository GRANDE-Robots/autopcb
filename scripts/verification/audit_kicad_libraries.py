"""audit_kicad_libraries.py: check saved KiCad library paths without changing anything.

Project-local aliases are resolved through the library tables, so the check
works when a library moves without requiring schematic or PCB identifier edits.
Unregistered aliases may be provided by a user's global KiCad configuration.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import collections
import fnmatch
import json
from pathlib import Path

from core.compat import file_sha256
from core.paths import library_directories
from typing import Any

from core.kicad_sexpr import Form, parse

CAD_SUFFIXES = {".kicad_mod", ".kicad_sym", ".step", ".stp", ".wrl"}


class FormReader:
    """Read single values out of parsed KiCad expressions."""

    @staticmethod
    def value(form: Form, name: str) -> str:
        """Return the first value after the name of the child ``name``, or an empty text."""
        matches = form.children(name)
        if not matches or len(matches[0].atoms()) < 2:
            return ""
        return matches[0].atoms()[1].value

    @staticmethod
    def properties(form: Form) -> dict[str, str]:
        """Map each property name of an expression to its value."""
        result = {}
        for item in form.children("property"):
            atoms = item.atoms()
            if len(atoms) >= 3:
                result[atoms[1].value] = atoms[2].value
        return result

    @staticmethod
    def model_values(root: Form) -> list[str]:
        """List the paths of every ``model`` expression below a root expression."""
        return [
            item.atoms()[1].value
            for item in root.descendants()
            if item.name == "model" and len(item.atoms()) >= 2
        ]


class InputFingerprints:
    """Hash every file that the library audit reads."""

    def __call__(self, project: Path) -> dict[str, str]:
        """Hash the boards, schematics, library files, tables and 3D models of a project.

        Inputs:
            project: Resolved project folder.
        Outputs:
            SHA-256 per project-relative POSIX path, sorted by path. Raises ValueError
            when a library input resolves outside the project.
        """
        files = {
            p
            for extension in ("*.kicad_pcb", "*.kicad_sch")
            for p in project.glob(extension)
        }
        for library in library_directories(project):
            files.update(
                p
                for p in library.rglob("*")
                if p.is_file() and p.suffix.lower() in CAD_SUFFIXES
            )
        for name in ("sym-lib-table", "fp-lib-table"):
            table = project / name
            if not table.is_file():
                continue
            files.add(table)
            for entry in parse(table.read_text(encoding="utf-8-sig")).children("lib"):
                try:
                    path = LibraryPaths.local_path(
                        project, FormReader.value(entry, "uri")
                    )
                except ValueError:
                    continue  # The main audit reports invalid project-relative paths.
                if path is not None and path.is_file():
                    files.add(path)
                elif path is not None and path.is_dir():
                    files.update(
                        p
                        for p in path.rglob("*")
                        if p.is_file() and p.suffix.lower() in CAD_SUFFIXES
                    )
        # Include models referenced outside a folder literally named 'libraries'.
        for owner in list(files):
            if owner.suffix.lower() not in {".kicad_pcb", ".kicad_mod"}:
                continue
            for value in FormReader.model_values(
                parse(owner.read_text(encoding="utf-8-sig"))
            ):
                try:
                    path = LibraryPaths.local_path(project, value)
                except ValueError:
                    continue
                if path is not None and path.is_file():
                    files.add(path)
        result = {}
        for path in files:
            resolved = path.resolve()
            if not resolved.is_relative_to(project):
                raise ValueError(f"Library input resolves outside the project: {path}")
            result[path.relative_to(project).as_posix()] = file_sha256(path)
        return dict(sorted(result.items()))


_fingerprints = InputFingerprints()


class LibraryPaths:
    """Resolve library and source paths inside a project."""

    @staticmethod
    def local_path(project: Path, uri: str) -> Path | None:
        """Resolve a ``${KIPRJMOD}/`` URI inside the project.

        Outputs:
            The resolved path, or None for a URI that is not project-relative. Raises
            ValueError when the path escapes the project.
        """
        value = uri.replace("\\", "/")
        prefix = "${KIPRJMOD}/"
        if not value.startswith(prefix):
            return None
        path = (project / value[len(prefix) :]).resolve()
        if not path.is_relative_to(project):
            raise ValueError(
                f"Project-relative library path escapes the project: {uri}"
            )
        return path

    @staticmethod
    def select_source(project: Path, selected: str | Path | None, suffix: str) -> Path:
        """Choose the one board or schematic file of a project root.

        Inputs:
            project: Resolved project folder.
            selected: File to use, absolute or project-relative; None picks the only file.
            suffix: ``.kicad_pcb`` or ``.kicad_sch``.
        Outputs:
            The resolved file. Raises ValueError when no file, several files or a file
            outside the project root is found.
        """
        if selected is None:
            matches = sorted(project.glob(f"*{suffix}"))
            if not matches:
                raise ValueError(f"No {suffix} file found in the project root")
            if len(matches) > 1:
                raise ValueError(f"Multiple {suffix} files; select one explicitly")
            path = matches[0].resolve()
        else:
            path = Path(selected)
            path = (path if path.is_absolute() else project / path).resolve()
        if (
            path.parent != project
            or path.suffix.lower() != suffix
            or not path.is_file()
        ):
            raise ValueError(f"Expected an existing {suffix} file in the project root")
        return path


class LibraryTables:
    """Read symbol and footprint library tables and the symbols they hold."""

    @staticmethod
    def table(
        project: Path, filename: str, errors: list[str]
    ) -> dict[str, Path | None]:
        """Read a library table into a nickname-to-path map.

        Inputs:
            project: Resolved project folder.
            filename: ``sym-lib-table`` or ``fp-lib-table``.
            errors: List that receives a message for each invalid or missing entry.
        Outputs:
            The local path of each nickname, or None for a library outside the project.
        """
        path = project / filename
        if not path.is_file():
            return {}
        table = parse(path.read_text(encoding="utf-8-sig"))
        result: dict[str, Path | None] = {}
        for entry in table.children("lib"):
            name, uri = FormReader.value(entry, "name"), FormReader.value(entry, "uri")
            if not name or not uri:
                errors.append(f"{filename}: library entry lacks a nickname or path")
                continue
            if name in result:
                errors.append(f"{filename}: duplicate nickname {name}")
                continue
            try:
                path = LibraryPaths.local_path(project, uri)
            except ValueError as error:
                errors.append(str(error))
                continue
            result[name] = path
            if path is not None:
                exists = path.is_dir() if filename == "fp-lib-table" else path.is_file()
                if not exists:
                    errors.append(
                        f"{filename}: {name} points to a missing local library: {uri}"
                    )
        return result

    @staticmethod
    def symbol_definitions(path: Path) -> tuple[set[str], set[str], set[str]]:
        """Read a symbol library into its symbol names, default footprints and footprint filters."""
        root = parse(path.read_text(encoding="utf-8-sig"))
        names: set[str] = set()
        defaults: set[str] = set()
        filters: set[str] = set()
        for symbol in root.children("symbol"):
            atoms = symbol.atoms()
            if len(atoms) >= 2:
                names.add(atoms[1].value)
            for item in symbol.descendants():
                if item.name != "property":
                    continue
                prop = item.atoms()
                if len(prop) < 3:
                    continue
                if prop[1].value == "Footprint" and prop[2].value:
                    defaults.add(prop[2].value)
                elif prop[1].value == "ki_fp_filters":
                    filters.update(prop[2].value.split())
        return names, defaults, filters


class LibraryAudit:
    """Audit the library resources that a board and a schematic use."""

    def __call__(
        self,
        project: Path,
        *,
        board: str | Path | None = None,
        schematic: str | Path | None = None,
    ) -> dict[str, Any]:
        """Return a source-bound audit; external/global libraries remain unverified."""
        project = project.resolve()
        board_path = LibraryPaths.select_source(project, board, ".kicad_pcb")
        schematic_path = LibraryPaths.select_source(project, schematic, ".kicad_sch")
        before = _fingerprints(project)
        errors: list[str] = []
        symbols = LibraryTables.table(project, "sym-lib-table", errors)
        footprints = LibraryTables.table(project, "fp-lib-table", errors)
        definitions: dict[str, set[str]] = {}
        symbol_defaults: set[str] = set()
        symbol_filters: set[str] = set()
        for nickname, path in symbols.items():
            if path is not None and path.is_file():
                names, defaults, filters = LibraryTables.symbol_definitions(path)
                definitions[nickname] = names
                symbol_defaults.update(defaults)
                symbol_filters.update(filters)

        global_symbols: collections.Counter[str] = collections.Counter()
        global_footprints: collections.Counter[str] = collections.Counter()
        board_footprints: set[str] = set()
        schematic_footprints: set[str] = set()
        schematic_source = parse(schematic_path.read_text(encoding="utf-8-sig"))
        for symbol in schematic_source.children("symbol"):
            library_id = FormReader.value(symbol, "lib_id")
            nickname, separator, name = library_id.partition(":")
            if separator:
                if nickname not in symbols or symbols[nickname] is None:
                    global_symbols[nickname] += 1
                elif nickname in definitions and name not in definitions[nickname]:
                    errors.append(f"Missing local symbol: {library_id}")
            footprint = FormReader.properties(symbol).get("Footprint", "")
            if footprint:
                schematic_footprints.add(footprint)
        del schematic_source
        board_source = parse(board_path.read_text(encoding="utf-8-sig"))
        for footprint in board_source.children("footprint"):
            atoms = footprint.atoms()
            if len(atoms) < 2:
                continue
            identifier = atoms[1].value
            board_footprints.add(identifier)
            nickname, separator, _ = identifier.partition(":")
            if separator and (
                nickname not in footprints or footprints[nickname] is None
            ):
                global_footprints[nickname] += 1

        board_model_values = FormReader.model_values(board_source)
        del board_source

        for identifier in sorted(board_footprints | schematic_footprints):
            nickname, separator, name = identifier.partition(":")
            if (
                not separator
                or nickname not in footprints
                or footprints[nickname] is None
            ):
                continue
            directory = footprints[nickname]
            if directory.is_dir() and not (directory / (name + ".kicad_mod")).is_file():
                errors.append(f"Missing local footprint: {identifier}")

        missing_models: dict[str, set[str]] = collections.defaultdict(set)
        external_models: set[str] = set()
        model_values = [(board_path.name, value) for value in board_model_values]
        local_footprints = {
            path
            for directory in library_directories(project)
            for path in directory.rglob("*.kicad_mod")
        }
        for directory in footprints.values():
            if directory is not None and directory.is_dir():
                local_footprints.update(directory.glob("*.kicad_mod"))
        for path in sorted(local_footprints):
            owner = path.relative_to(project).as_posix()
            model_values.extend(
                (owner, value)
                for value in FormReader.model_values(
                    parse(path.read_text(encoding="utf-8-sig"))
                )
            )
        local_models: set[str] = set()
        for owner, value in model_values:
            try:
                path = LibraryPaths.local_path(project, value)
            except ValueError as error:
                errors.append(str(error))
                continue
            if path is None:
                external_models.add(value)
            else:
                relative = path.relative_to(project).as_posix()
                local_models.add(relative)
                if not path.is_file():
                    missing_models[relative].add(owner)
        errors.extend(
            f"Missing local 3D model: {path}" for path in sorted(missing_models)
        )

        candidates = []
        for nickname, directory in sorted(footprints.items()):
            if directory is None or not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.kicad_mod")):
                identifier = f"{nickname}:{path.stem}"
                if (
                    identifier not in board_footprints
                    and identifier not in schematic_footprints
                ):
                    candidates.append(
                        {
                            "identifier": identifier,
                            "symbol_default": identifier in symbol_defaults,
                            "symbol_filter_match": any(
                                fnmatch.fnmatchcase(
                                    identifier if ":" in pattern else path.stem, pattern
                                )
                                for pattern in symbol_filters
                            ),
                        }
                    )

        after = _fingerprints(project)
        if before != after:
            raise RuntimeError(
                "Project inputs changed during the library audit; retry on a stable snapshot"
            )
        return {
            "status": "ok" if not errors else "missing_or_invalid_local_resources",
            "source_sha256": {
                name: before[name] for name in (board_path.name, schematic_path.name)
            },
            "registered_symbol_libraries": len(symbols),
            "registered_footprint_libraries": len(footprints),
            "external_symbol_nicknames": dict(sorted(global_symbols.items())),
            "external_footprint_nicknames": dict(sorted(global_footprints.items())),
            "local_model_paths_checked": len(local_models),
            "external_model_paths_unverified": len(external_models),
            "missing_local_3d_models": [
                {"path": path, "referenced_by": sorted(owners)}
                for path, owners in sorted(missing_models.items())
            ],
            "footprints_not_used_by_live_design": candidates,
            "errors": sorted(set(errors)),
            "limitations": "Only the selected schematic sheet is inspected. External libraries, historical boards, component fit, electrical behavior and fabrication are not verified; unused is not proof that deletion is safe.",
        }


audit = LibraryAudit()


class LibraryAuditCommand:
    """Command-line entry: print the library audit of a project."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Run the audit.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 when the audit is ok, 1 when it finds problems and 2 when it
            cannot run.
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--project", required=True, type=Path)
        parser.add_argument(
            "--board",
            type=Path,
            help="board in project root; required when multiple exist",
        )
        parser.add_argument(
            "--schematic",
            type=Path,
            help="schematic in project root; required when multiple exist",
        )
        parser.add_argument(
            "--details",
            action="store_true",
            help="include individual unused candidates and external aliases",
        )
        args = parser.parse_args(argv)
        try:
            result = audit(args.project, board=args.board, schematic=args.schematic)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"Library audit failed: {error}")
            return 2
        if args.details:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            summary = {
                key: value
                for key, value in result.items()
                if key
                not in {
                    "external_symbol_nicknames",
                    "external_footprint_nicknames",
                    "footprints_not_used_by_live_design",
                }
            }
            summary["external_symbol_nickname_count"] = len(
                result["external_symbol_nicknames"]
            )
            summary["external_footprint_nickname_count"] = len(
                result["external_footprint_nicknames"]
            )
            summary["footprints_not_used_by_live_design_count"] = len(
                result["footprints_not_used_by_live_design"]
            )
            print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
        return 0 if result["status"] == "ok" else 1


main = LibraryAuditCommand()


if __name__ == "__main__":
    raise SystemExit(main())
