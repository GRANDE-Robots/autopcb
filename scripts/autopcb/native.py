"""native.py: capability-checked, process-isolated adapter to KiCad.

``Native`` pairs a KiCad CLI with a Python whose ``pcbnew`` has the same
version, runs CLI commands and runs the native worker in that Python.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys

from core.native_command import run_checked
from core.paths import ProjectStorage
from core.toolchain import kicad_cli, native_python_candidates


class EnginePath:
    """Locates the folder that holds the AutoPCB packages."""

    def __call__(self) -> Path:
        """The ``engine`` folder of a frozen bundle, else the ``scripts`` folder."""
        bundle = getattr(sys, "_MEIPASS", None)
        return (
            Path(bundle) / "engine" if bundle else Path(__file__).resolve().parents[1]
        )


engine_path = EnginePath()


@dataclass(frozen=True)
class Native:
    """A KiCad CLI and a Python with the same version of ``pcbnew``."""

    cli: Path
    python: Path
    version: str

    @staticmethod
    def _version(text: str) -> str:
        """The first ``major.minor.patch`` version in a tool's output.

        Outputs:
            The version text. Raises ValueError when the output has none.
        """
        match = re.search(r"\b(\d+\.\d+\.\d+)\b", text)
        if match is None:
            raise ValueError(f"Could not identify KiCad version: {text.strip()}")
        return match[1]

    @staticmethod
    def prepare_windows_cli() -> None:
        """Keep the bundled Qt runtime out of the external Windows toolchain."""
        bundle = getattr(sys, "_MEIPASS", None)
        if sys.platform != "win32" or bundle is None:
            return
        import ctypes

        # The bootloader adds a DLL directory that child processes also inherit.
        # This process only runs CLI jobs; the desktop keeps its bundled Qt paths.
        if not ctypes.windll.kernel32.SetDllDirectoryW(None):
            raise ctypes.WinError()
        root = Path(bundle)
        for name in ("PATH", "PYTHONPATH", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH"):
            if name not in os.environ:
                continue
            entries = os.environ[name].split(os.pathsep)
            external = [
                entry
                for entry in entries
                if not Path(os.path.normpath(entry.strip('"'))).is_relative_to(root)
            ]
            if external:
                os.environ[name] = os.pathsep.join(external)
            else:
                del os.environ[name]

    @classmethod
    def discover(cls, *, cli: Path | None = None, python: Path | None = None) -> Native:
        """Find a KiCad CLI and a Python whose ``pcbnew`` matches its version.

        Inputs:
            cli: KiCad CLI to use; the discovered one when None.
            python: Python to use; the candidates beside the CLI when None.
        Outputs:
            The matched toolchain. Raises RuntimeError when no candidate Python
            reports the CLI's ``pcbnew`` version, and ValueError when the CLI
            reports no version.
        """
        executable = kicad_cli(cli)
        version = cls._version(
            run_checked(
                [str(executable), "version"], operation="KiCad version", timeout=10
            ).stdout
        )
        candidates = (
            (python.resolve(),)
            if python is not None
            else native_python_candidates(executable)
        )
        for candidate in candidates:
            try:
                output = run_checked(
                    [
                        str(candidate),
                        "-B",
                        "-c",
                        "import pcbnew; print(pcbnew.GetBuildVersion())",
                    ],
                    operation="KiCad Python probe",
                    timeout=10,
                )
                if cls._version(output.stdout) == version:
                    return cls(executable, candidate, version)
            except (RuntimeError, ValueError):
                continue
        raise RuntimeError(
            f"KiCad CLI {version} needs a matching Python with pcbnew. Set KICAD_PYTHON."
        )

    def call(
        self, *args: str | Path, output: Path | None = None, timeout: float = 180
    ) -> None:
        """Run one KiCad CLI command.

        Inputs:
            args: Arguments after the executable.
            output: File the command must create, which must not exist beforehand.
            timeout: Seconds before the command is abandoned.
        Outputs:
            None. Raises RuntimeError when the command fails or leaves no output.
        """
        run_checked(
            [str(self.cli), *map(str, args)],
            operation="KiCad " + " ".join(map(str, args[:3])),
            output=output,
            timeout=timeout,
        )

    def worker(
        self,
        operation: str,
        *args: str | Path,
        output: Path | None = None,
        timeout: float = 180,
        storage: ProjectStorage | None = None,
    ):
        """Run one native worker operation in KiCad's Python.

        Inputs:
            operation: Worker operation, such as ``inspect`` or ``generate``.
            args: Board, record and optional argument files the operation reads.
            output: File the operation must create, which must not exist beforehand.
            timeout: Seconds before the worker is abandoned.
            storage: Project storage whose environment variables the worker gets.
        Outputs:
            The completed process. Raises RuntimeError when the worker fails or
            leaves no output.
        """
        root = engine_path()
        command = (
            "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
            "runpy.run_module('autopcb.native_worker',run_name='__main__')"
        )
        env = {
            **os.environ,
            "KICAD_CLI": str(self.cli),
            "KICAD_PYTHON": str(self.python),
        }
        if storage is not None:
            env.update(storage.environment(Path(args[0])))
        return run_checked(
            [
                str(self.python),
                "-B",
                "-c",
                command,
                str(root),
                operation,
                *map(str, args),
            ],
            operation=f"KiCad native {operation}",
            output=output,
            timeout=timeout,
            env=env,
        )

    def footprints(self) -> Path:
        """The folder of KiCad's standard footprint libraries.

        Outputs:
            ``KICAD10_FOOTPRINT_DIR`` when set, else the first standard install
            location that exists. Raises RuntimeError when none is found.
        """
        configured = os.environ.get("KICAD10_FOOTPRINT_DIR")
        candidates = (
            [Path(configured)]
            if configured
            else [
                self.cli.parent.parent / "share/kicad/footprints",
                self.cli.parent.parent / "Resources/footprints",
                self.cli.parent.parent.parent / "SharedSupport/footprints",
                self.cli.parent.parent / "SharedSupport/footprints",
                Path("/usr/share/kicad/footprints"),
                Path("/usr/local/share/kicad/footprints"),
                Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints"),
            ]
        )
        for path in candidates:
            if path.is_dir():
                return path.resolve()
        raise RuntimeError(
            "KiCad footprint libraries were not found. Set KICAD10_FOOTPRINT_DIR."
        )

    def report(self, args: tuple[str, ...], source: Path, output: Path) -> dict:
        """Run a KiCad CLI check that writes a JSON report and read the report.

        Inputs:
            args: Command words, such as ``("pcb", "drc")``.
            source: Schematic or board to check.
            output: New report file.
        Outputs:
            The report. Raises RuntimeError when the command fails or the report
            is not a JSON object.
        """
        self.call(
            *args,
            "--format",
            "json",
            "--severity-all",
            "--output",
            output,
            source,
            output=output,
        )
        data = json.loads(output.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise RuntimeError(f"Invalid KiCad report: {output.name}")
        return data

    def status(self) -> dict:
        """The toolchain as report fields: KiCad version, CLI, Python and platform."""
        return {
            "kicad_version": self.version,
            "kicad_cli": str(self.cli),
            "pcbnew_runtime": str(self.python),
            "platform": sys.platform,
        }
