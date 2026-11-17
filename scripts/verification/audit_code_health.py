"""Check Python syntax, lint and formatting without modifying project files."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from core.paths import asset_root, default_project


def inspect_code(project: Path) -> dict:
    """Return observed checks; a missing formatter is never counted as a pass."""
    support = asset_root(project)
    scripts = support / "scripts"
    if not scripts.is_dir():
        raise ValueError(f"Script directory not found: {scripts}")
    files = sorted(p for p in scripts.rglob("*.py") if "__pycache__" not in p.parts)
    if not files:
        raise ValueError(f"No Python files found in {scripts}")
    errors = []
    for path in files:
        try:
            ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as error:
            errors.append(
                {"file": path.relative_to(scripts).as_posix(), "error": str(error)}
            )

    report = {"python_files": len(files), "syntax_errors": errors}
    if importlib.util.find_spec("ruff") is None:
        report.update(
            status="unavailable",
            ruff_lint={
                "status": "unavailable",
                "reason": "Install Ruff in this Python.",
            },
            ruff_format={
                "status": "unavailable",
                "reason": "Install Ruff in this Python.",
            },
        )
        return report
    for name, arguments in (
        ("ruff_lint", ("check", "--no-cache")),
        ("ruff_format", ("format", "--check", "--no-cache")),
    ):
        try:
            result = subprocess.run(
                [sys.executable, "-m", "ruff", *arguments, str(scripts)],
                cwd=support,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=120,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
            report[name] = {
                "status": "ok" if result.returncode == 0 else "failed",
                "diagnostics": (result.stdout + result.stderr).strip()[:16000],
            }
        except (OSError, subprocess.TimeoutExpired) as error:
            report[name] = {"status": "unavailable", "reason": str(error)}
    statuses = {report["ruff_lint"]["status"], report["ruff_format"]["status"]}
    report["status"] = (
        "failed"
        if errors or "failed" in statuses
        else ("unavailable" if "unavailable" in statuses else "ok")
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=default_project(__file__))
    args = parser.parse_args(argv)
    try:
        report = inspect_code(args.project.resolve())
    except (OSError, ValueError) as error:
        print(f"Code health: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
