"""audit_code_health.py: check Python syntax, lint, formatting and the working rules.

The audit reads the project tree and reports. It writes one file, the rules
baseline, and only when asked to remove entries that are already fixed.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

from core.paths import asset_root, default_project


class RuleFindings:
    """Files and folders that break a documentation or class rule in AGENTS.md."""

    COPYRIGHT = "Copyright: GRANDE Robots and Aaron John Sabu"
    FOLDERS = ("packaging", "config", "data", "docs", ".github/workflows")
    ROOT_SECTIONS = (
        "Overview",
        "Responsibilities",
        "Requirements",
        "Installation",
        "Usage",
        "Validation",
        "File Structure",
        "License",
    )

    def __init__(self, support: Path):
        """Read the tree under one folder.

        Inputs:
            support: Folder that holds ``scripts/`` and the project documents.
        Outputs: None
        """
        self.support = support

    def sources(self) -> list[Path]:
        """List the maintained Python files under ``scripts/`` and ``packaging/``."""
        return sorted(
            path
            for folder in ("scripts", "packaging")
            for path in (self.support / folder).rglob("*.py")
            if "__pycache__" not in path.parts
        )

    def header(self, path: Path, tree: ast.Module) -> bool:
        """Say whether the module docstring names its file and carries the copyright."""
        text = ast.get_docstring(tree) or ""
        return path.name in text.partition("\n")[0] and self.COPYRIGHT in text

    @staticmethod
    def undocumented(path: Path, tree: ast.Module) -> bool:
        """Say whether any class, function or method lacks a docstring.

        Test methods are named for the behavior they check and are exempt.
        """
        tests = path.name.startswith("test_")
        kinds = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        return any(
            isinstance(node, kinds)
            and not ast.get_docstring(node)
            and not (tests and node.name.startswith("test_"))
            for node in ast.walk(tree)
        )

    def tracked_entries(self, folder: Path) -> set[str] | None:
        """Name what Git tracks directly inside a folder, apart from its README.

        Outputs:
            The entry names, or None when the support folder is not a Git
            checkout or Git cannot list the folder.
        """
        if not (self.support / ".git").exists():
            return None
        relative = PurePosixPath(folder.relative_to(self.support).as_posix())
        try:
            listing = subprocess.run(
                ["git", "-C", str(self.support), "ls-files", "--", relative.as_posix()],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            return None
        names = {
            PurePosixPath(line).relative_to(relative).parts[0]
            for line in listing.splitlines()
            if line
        }
        names.discard("README.md")
        return names

    @staticmethod
    def listed_entries(readme: Path) -> set[str] | None:
        """Read the entry names in the first column of the File Structure table.

        Outputs:
            The names without a trailing slash, or None when the README has no
            File Structure section.
        """
        _, found, section = readme.read_text(encoding="utf-8").partition(
            "\n## File Structure"
        )
        if not found:
            return None
        return {
            match.group(1).rstrip("/")
            for line in section.split("\n## ")[0].splitlines()
            if (match := re.match(r"\|\s*\[?`([^`]+)`", line))
        }

    def readme_findings(self, folder: Path) -> bool:
        """Say whether a folder README's table misses a tracked entry or names a stale one."""
        readme = folder / "README.md"
        listed = self.listed_entries(readme)
        tracked = self.tracked_entries(folder)
        if tracked is None:
            return False
        return listed is None or listed != tracked

    def root_sections_wrong(self) -> bool:
        """Say whether the root README lacks the required sections in order."""
        readme = self.support / "README.md"
        if not readme.is_file():
            return True
        sections = re.findall(
            r"^## (.+?)\s*$", readme.read_text(encoding="utf-8"), re.MULTILINE
        )
        return tuple(sections) != self.ROOT_SECTIONS

    def collect(self) -> dict[str, list[str]]:
        """Find every rule breach.

        Outputs:
            Paths relative to the support folder, sorted, under six rules:
            ``module_functions``, ``missing_header``, ``missing_docstrings``,
            ``missing_readme``, ``readme_structure`` (a folder README whose File
            Structure table does not match the entries Git tracks in the folder)
            and ``root_readme_sections``. Files that do not parse are left to the
            syntax check.
        """
        found = {
            "module_functions": [],
            "missing_header": [],
            "missing_docstrings": [],
            "missing_readme": [],
            "readme_structure": [],
            "root_readme_sections": [],
        }
        sources = self.sources()
        for path in sources:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            except (OSError, SyntaxError, UnicodeError):
                continue
            name = path.relative_to(self.support).as_posix()
            functions = (ast.FunctionDef, ast.AsyncFunctionDef)
            if any(isinstance(node, functions) for node in tree.body):
                found["module_functions"].append(name)
            if not self.header(path, tree):
                found["missing_header"].append(name)
            if self.undocumented(path, tree):
                found["missing_docstrings"].append(name)
        folders = {path.parent for path in sources}
        folders |= {self.support / folder for folder in ("scripts", *self.FOLDERS)}
        found["missing_readme"] = sorted(
            folder.relative_to(self.support).as_posix()
            for folder in folders
            if folder.is_dir() and not (folder / "README.md").is_file()
        )
        found["readme_structure"] = sorted(
            folder.relative_to(self.support).as_posix()
            for folder in folders | {self.support}
            if (folder / "README.md").is_file() and self.readme_findings(folder)
        )
        if self.root_sections_wrong():
            found["root_readme_sections"] = ["README.md"]
        return found


class RulesBaseline:
    """Rule breaches that predate the rules, kept so the list can only shrink."""

    def __init__(self, path: Path):
        """Read the baseline file; a missing file is an empty baseline."""
        self.path = path
        self.entries = (
            json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        )

    @classmethod
    def beside(cls, support: Path) -> RulesBaseline:
        """Open the baseline kept beside this audit in a project tree."""
        return cls(support / "scripts" / "verification" / "code_rules_baseline.json")

    def compare(self, found: dict[str, list[str]]) -> dict:
        """Split findings into new breaches, fixed entries and remaining debt.

        Outputs:
            ``new`` and ``fixed`` map a rule to paths and omit empty rules.
            ``remaining`` maps every rule to its count of known breaches.
        """
        new, fixed, remaining = {}, {}, {}
        for rule, paths in found.items():
            known = set(self.entries.get(rule, ()))
            if added := sorted(set(paths) - known):
                new[rule] = added
            if cleared := sorted(known - set(paths)):
                fixed[rule] = cleared
            remaining[rule] = len(known & set(paths))
        return {"new": new, "fixed": fixed, "remaining": remaining}

    def shrink(self, found: dict[str, list[str]]) -> None:
        """Drop entries that are fixed; never add one.

        Outputs: None. Rewrites the baseline file.
        """
        self.entries = {
            rule: sorted(set(self.entries.get(rule, ())) & set(paths))
            for rule, paths in found.items()
        }
        self.path.write_text(json.dumps(self.entries, indent=1) + "\n", "utf-8")


class CodeHealth:
    """Syntax, Ruff and working-rule checks for one project tree."""

    def __init__(self, project: Path):
        """Locate the script folder.

        Inputs:
            project: Project root, or a folder that holds ``autopcb/``.
        Outputs: None. Raises ValueError when there is no script folder.
        """
        self.support = asset_root(project)
        self.scripts = self.support / "scripts"
        if not self.scripts.is_dir():
            raise ValueError(f"Script directory not found: {self.scripts}")

    def syntax_errors(self, files: list[Path]) -> list[dict]:
        """Parse each file and return the ones that fail, with the error text."""
        errors = []
        for path in files:
            try:
                ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            except (OSError, SyntaxError, UnicodeError) as error:
                errors.append(
                    {
                        "file": path.relative_to(self.scripts).as_posix(),
                        "error": str(error),
                    }
                )
        return errors

    def ruff(self, arguments: tuple[str, ...]) -> dict:
        """Run one Ruff command on the script folder and report its outcome."""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "ruff", *arguments, str(self.scripts)],
                cwd=self.support,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=120,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"status": "unavailable", "reason": str(error)}
        return {
            "status": "ok" if result.returncode == 0 else "failed",
            "diagnostics": (result.stdout + result.stderr).strip()[:16000],
        }

    def report(self, *, shrink_baseline: bool = False) -> dict:
        """Return the observed checks.

        Inputs:
            shrink_baseline: Remove fixed entries from the rules baseline first.
        Outputs:
            A report whose ``status`` is ``ok``, ``failed`` or ``unavailable``.
            A missing formatter is never counted as a pass. Raises ValueError
            when the script folder holds no Python file.
        """
        files = sorted(
            p for p in self.scripts.rglob("*.py") if "__pycache__" not in p.parts
        )
        if not files:
            raise ValueError(f"No Python files found in {self.scripts}")
        errors = self.syntax_errors(files)
        found = RuleFindings(self.support).collect()
        baseline = RulesBaseline.beside(self.support)
        if shrink_baseline:
            baseline.shrink(found)
        rules = baseline.compare(found)
        report = {"python_files": len(files), "syntax_errors": errors, "rules": rules}
        if importlib.util.find_spec("ruff") is None:
            missing = {
                "status": "unavailable",
                "reason": "Install Ruff in this Python.",
            }
            report.update(status="unavailable", ruff_lint=missing, ruff_format=missing)
            return report
        report["ruff_lint"] = self.ruff(("check", "--no-cache"))
        report["ruff_format"] = self.ruff(("format", "--check", "--no-cache"))
        statuses = {report["ruff_lint"]["status"], report["ruff_format"]["status"]}
        failed = errors or rules["new"] or rules["fixed"] or "failed" in statuses
        report["status"] = (
            "failed"
            if failed
            else ("unavailable" if "unavailable" in statuses else "ok")
        )
        return report


class CodeHealthCommand:
    """Command-line entry: print the report and exit 0 only when every check passes."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Run the audit.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 for ``ok``, otherwise 2.
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--project", type=Path, default=default_project(__file__))
        parser.add_argument(
            "--shrink-baseline",
            action="store_true",
            help="remove fixed entries from the rules baseline",
        )
        args = parser.parse_args(argv)
        try:
            report = CodeHealth(args.project.resolve()).report(
                shrink_baseline=args.shrink_baseline
            )
        except (OSError, ValueError) as error:
            print(f"Code health: {error}", file=sys.stderr)
            return 2
        print(json.dumps(report, indent=2))
        return 0 if report["status"] == "ok" else 2


main = CodeHealthCommand()

if __name__ == "__main__":
    raise SystemExit(main())
