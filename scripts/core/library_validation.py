"""library_validation.py: validate a planned library file using the installed KiCad CLI.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from core.kicad_campaign import Plan
from core.kicad_sexpr import parse
from core.toolchain import kicad_cli


class LibraryValidator:
    """Has KiCad export each planned symbol or footprint in an isolated sandbox."""

    @staticmethod
    def run_export(
        command: list[str],
        output: Path,
        *,
        root: Path,
        environment: dict[str, str],
        timeout: int,
        required_output: Path | None = None,
    ) -> None:
        """Run one KiCad export and require a nonempty result.

        Inputs:
            command: The ``kicad-cli`` command line.
            output: Folder that should receive SVG files.
            root: Working directory.
            environment: Process environment.
            timeout: Seconds before the run is abandoned.
            required_output: The one file that must be produced; any nonempty
                SVG in ``output`` when None.
        Outputs:
            None. Raises RuntimeError, with KiCad's message, when it fails or
            produces nothing.
        """
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        produced = (
            required_output.is_file() and required_output.stat().st_size > 0
            if required_output is not None
            else any(
                path.is_file() and path.stat().st_size > 0
                for path in output.glob("*.svg")
            )
        )
        if result.returncode != 0 or not produced:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"KiCad could not export the candidate library: {detail[:1000]}"
            )

    def __call__(self, plan: Plan, cli: Path, *, timeout: int = 60) -> None:
        """Require KiCad to export each candidate symbol or footprint in isolation.

        Inputs:
            plan: The planned library edit.
            cli: The ``kicad-cli`` path.
            timeout: Seconds allowed per export; at least 1.
        Outputs:
            None. Raises ValueError for a bad timeout, an unsupported document,
            or a symbol library without distinct named symbols, and
            RuntimeError when KiCad cannot export a candidate.
        """
        if timeout < 1:
            raise ValueError("Validation timeout must be positive")
        cli = kicad_cli(cli)
        temp_root = Path(tempfile.gettempdir()).resolve()
        with tempfile.TemporaryDirectory(
            prefix="jessie-library-validate-", dir=temp_root
        ) as work:
            root = Path(work).resolve()
            if root.parent != temp_root or not root.name.startswith(
                "jessie-library-validate-"
            ):
                raise RuntimeError("Unexpected library validation directory")
            environment = os.environ.copy()
            for name in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
                directory = root / name.lower()
                directory.mkdir()
                environment[name] = str(directory)
            if plan.source.suffix.lower() == ".kicad_mod":
                library = root / "candidate.pretty"
                library.mkdir()
                source = library / plan.source.name
                source.write_bytes(plan.after)
                output = root / "output"
                output.mkdir()
                command = [
                    str(cli),
                    "fp",
                    "export",
                    "svg",
                    "--layers",
                    "F.Cu",
                    "--footprint",
                    source.stem,
                    "--output",
                    str(output),
                    str(library),
                ]
                self.run_export(
                    command,
                    output,
                    root=root,
                    environment=environment,
                    timeout=timeout,
                    required_output=output / f"{source.stem}.svg",
                )
            elif plan.source.suffix.lower() == ".kicad_sym":
                source = root / plan.source.name
                source.write_bytes(plan.after)
                forms = parse(plan.after.decode("utf-8")).children("symbol")
                if not forms or any(len(form.atoms()) < 2 for form in forms):
                    raise ValueError("A symbol library must contain named symbols")
                names = [form.atoms()[1].value for form in forms]
                if len(names) != len(set(names)):
                    raise ValueError("A symbol library cannot contain duplicate names")
                for index, name in enumerate(names):
                    output = root / f"output-{index}"
                    output.mkdir()
                    command = [
                        str(cli),
                        "sym",
                        "export",
                        "svg",
                        "--symbol",
                        name,
                        "--output",
                        str(output),
                        str(source),
                    ]
                    self.run_export(
                        command,
                        output,
                        root=root,
                        environment=environment,
                        timeout=timeout,
                    )
            else:
                raise ValueError("Unsupported library document")


validate_library = LibraryValidator()
