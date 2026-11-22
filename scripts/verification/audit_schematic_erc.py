"""Run configured ERC on the saved schematic without making a project copy."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.project import ProjectPaths, SourceSnapshot
from core.toolchain import kicad_cli


@dataclass(frozen=True)
class ErcAudit:
    """Results from the configured rules, with their limits made explicit."""

    source_sha256: dict[str, str]
    kicad_version: str
    ignored_categories: list[str]
    individual_exclusions: int
    findings: list[dict[str, Any]]

    @property
    def severity_counts(self) -> dict[str, int]:
        return dict(
            sorted(
                Counter(
                    str(item.get("severity", "unknown")) for item in self.findings
                ).items()
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "severity_counts": self.severity_counts,
            "scope": (
                "Saved files and configured ERC rules only. Ignored categories and "
                "individual exclusions were not checked; electrical and physical "
                "qualification are separate."
            ),
        }


def audit(project: ProjectPaths, cli: Path) -> ErcAudit:
    """Read saved sources, run KiCad once, and reject mixed-source results."""
    before = SourceSnapshot.capture(project)
    settings = json.loads(project.settings.read_text(encoding="utf-8-sig"))
    erc = settings.get("erc", {})
    severities = erc.get("rule_severities", {})
    exclusions = erc.get("erc_exclusions", [])
    if not isinstance(severities, dict) or not isinstance(exclusions, list):
        raise ValueError("Project ERC settings are malformed.")
    ignored = sorted(
        name for name, severity in severities.items() if severity == "ignore"
    )

    with tempfile.TemporaryDirectory(prefix="jessie-erc-") as work:
        report_path = Path(work) / "erc.json"
        command = [
            str(cli),
            "sch",
            "erc",
            "--format",
            "json",
            "-o",
            str(report_path),
            str(project.schematic),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        if result.returncode:
            raise RuntimeError(
                f"KiCad ERC failed ({result.returncode}): {result.stderr or result.stdout}"
            )
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))

    sheets = report.get("sheets")
    if not isinstance(sheets, list):
        raise ValueError("KiCad ERC report has no valid sheet list.")
    findings = []
    for sheet in sheets:
        if not isinstance(sheet, dict) or not isinstance(sheet.get("violations"), list):
            raise ValueError("KiCad ERC report has an invalid violation list.")
        findings.extend(sheet["violations"])
    if any(not isinstance(item, dict) for item in findings):
        raise ValueError("KiCad ERC report contains an invalid finding.")

    before.verify_unchanged()
    return ErcAudit(
        source_sha256=before.hashes,
        kicad_version=str(report.get("kicad_version", "unknown")),
        ignored_categories=ignored,
        individual_exclusions=len(exclusions),
        findings=findings,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument(
        "--cli", type=Path, help="KiCad CLI; defaults to KICAD_CLI or KiCad 10"
    )
    parser.add_argument(
        "--json", action="store_true", help="print the complete result as JSON"
    )
    parser.add_argument(
        "--out", type=Path, help="optionally write a new JSON file outside the project"
    )
    args = parser.parse_args()
    project = ProjectPaths(args.project)

    target = args.out.resolve() if args.out else None
    if target is not None:
        if target.is_relative_to(project.root) or target.suffix.lower() != ".json":
            parser.error("--out must be a JSON file outside the project")
        if target.exists() or not target.parent.is_dir():
            parser.error("--out must be a new file in an existing directory")

    try:
        result = audit(project, kicad_cli(args.cli))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"ERC audit failed: {error}", file=sys.stderr)
        return 2
    data = result.to_dict()
    if target is not None:
        with target.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        counts = result.severity_counts
        errors, warnings = counts.get("error", 0), counts.get("warning", 0)
        print(
            f"Configured ERC: {errors} error{'s' if errors != 1 else ''}, "
            f"{warnings} warning{'s' if warnings != 1 else ''}"
        )
        for finding in result.findings:
            print(
                f"  {finding.get('severity', 'unknown')}: "
                f"{finding.get('description', 'unnamed finding')}"
            )
        other = sum(
            count
            for severity, count in counts.items()
            if severity not in {"error", "warning"}
        )
        if other:
            print(f"Findings with other or unknown severity: {other}")
        print("Ignored categories:", ", ".join(result.ignored_categories) or "none")
        print(f"Individual exclusions: {result.individual_exclusions}")
        print(data["scope"])
    counts = result.severity_counts
    return (
        1
        if counts.get("error", 0)
        or any(severity not in {"error", "warning"} for severity in counts)
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
