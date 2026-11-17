"""Discover installed KiCad-related capabilities without opening a design."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _executable(requested: Path | None) -> Path | None:
    if requested is not None:
        return requested.resolve()
    if os.environ.get("KICAD_CLI"):
        return Path(os.environ["KICAD_CLI"]).resolve()
    adjacent = Path(sys.executable).parent / (
        "kicad-cli.exe" if os.name == "nt" else "kicad-cli"
    )
    if adjacent.is_file():
        return adjacent.resolve()
    located = shutil.which("kicad-cli")
    return Path(located).resolve() if located else None


def _run(cli: Path, args: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [str(cli), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)
    return result.returncode == 0, (result.stdout or result.stderr).strip()


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def inspect_toolchain(
    *, cli: Path | None = None, freerouting_jar: Path | None = None
) -> dict[str, Any]:
    """Report observed tools, not design readiness or untested capabilities."""
    candidate = _executable(cli)
    usable = candidate is not None and candidate.is_file()
    version_ok, version = _run(candidate, ["version"]) if usable else (False, "")
    help_ok, cli_help = _run(candidate, ["--help"]) if version_ok else (False, "")
    pcb_ok, pcb_help = (
        _run(candidate, ["pcb", "drc", "--help"]) if version_ok else (False, "")
    )
    sch_ok, sch_help = (
        _run(candidate, ["sch", "erc", "--help"]) if version_ok else (False, "")
    )
    jar = (
        freerouting_jar.resolve()
        if freerouting_jar is not None
        else Path.home() / ".kicad-mcp" / "freerouting.jar"
    )
    java = shutil.which("java")
    return {
        "status": "ok" if version_ok else "kicad_cli_unavailable",
        "python_runtime": sys.executable,
        "kicad_cli": {
            "path": str(candidate) if candidate else None,
            "version": version if version_ok else None,
            "version_error": None if version_ok else version or "executable not found",
            "pcb_drc_help": pcb_ok,
            "schematic_erc_help": sch_ok,
            "pcb_schematic_parity_option": pcb_ok and "--schematic-parity" in pcb_help,
            "pcb_refill_zones_option": pcb_ok and "--refill-zones" in pcb_help,
            "cli_headless_api_server": help_ok and "api-server" in cli_help,
        },
        "python_modules_in_this_runtime": {
            "pcbnew": _installed("pcbnew"),
            "kipy": _installed("kipy"),
            "skidl": _installed("skidl"),
        },
        "freerouting": {
            "jar": str(jar),
            "jar_exists": jar.is_file(),
            "java_path": java,
            "execution_checked": False,
        },
        "scope": (
            "Installed paths and CLI help only; editor IPC, routing quality, "
            "design safety, and physical readiness were not tested."
        ),
    }
