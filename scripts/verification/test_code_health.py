"""test_code_health.py: exercise the code-health audit on disposable and real trees.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from verification import audit_code_health

HEADER = (
    '"""{name}: hold one value.\n\nCopyright: GRANDE Robots and Aaron John Sabu\n"""\n'
)


class CodeHealthTests(unittest.TestCase):
    """Syntax, Ruff and rule outcomes on a throwaway script tree."""

    def setUp(self):
        """Create an empty project with a documented script folder."""
        self.temp = tempfile.TemporaryDirectory(prefix="autopcb-code-health-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.scripts = self.project / "autopcb" / "scripts"
        self.scripts.mkdir(parents=True)
        (self.scripts / "README.md").write_text("# Scripts\n", encoding="utf-8")

    def source(self, name: str, body: str = "value = 1\n") -> Path:
        """Write a script that carries the required header."""
        path = self.scripts / name
        path.write_text(HEADER.format(name=name) + "\n" + body, encoding="utf-8")
        return path

    def report(self, *, ruff=True, result=None, error=None) -> dict:
        """Run the audit with Ruff present or absent and its outcome mocked."""
        result = result or mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(
            audit_code_health.importlib.util, "find_spec", return_value=ruff or None
        ):
            with mock.patch.object(
                audit_code_health.subprocess,
                "run",
                return_value=result,
                side_effect=error,
            ) as run:
                self.run_calls = run
                return audit_code_health.CodeHealth(self.project).report()

    def test_clean_sources_pass_both_ruff_checks(self):
        self.source("sample.py")
        report = self.report()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["python_files"], 1)
        self.assertEqual(self.run_calls.call_count, 2)

    def test_syntax_and_format_failures_are_reported(self):
        (self.scripts / "bad.py").write_text("def broken(:\n", encoding="utf-8")
        failure = mock.Mock(returncode=1, stdout="format failure", stderr="")
        report = self.report(result=failure)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["syntax_errors"][0]["file"], "bad.py")
        self.assertEqual(report["ruff_format"]["status"], "failed")

    def test_missing_ruff_is_visible_and_not_reported_as_a_pass(self):
        self.source("sample.py")
        report = self.report(ruff=False)
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["ruff_lint"]["status"], "unavailable")

    def test_empty_script_tree_is_not_reported_as_a_pass(self):
        with self.assertRaisesRegex(ValueError, "No Python files"):
            audit_code_health.CodeHealth(self.project).report()

    def test_formatter_timeout_is_reported(self):
        self.source("sample.py")
        timeout = audit_code_health.subprocess.TimeoutExpired("ruff", 120)
        report = self.report(error=timeout)
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("timed out", report["ruff_format"]["reason"])

    def test_rule_breaches_fail_until_the_baseline_lists_them(self):
        self.source("plain.py", "def helper():\n    return 1\n")
        (self.scripts / "bare.py").write_text("value = 1\n", encoding="utf-8")
        package = self.scripts / "pack"
        package.mkdir()
        (package / "kind.py").write_text(
            HEADER.format(name="kind.py") + "\nclass Kind:\n    size = 1\n", "utf-8"
        )
        report = self.report()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(
            report["rules"]["new"],
            {
                "module_functions": ["scripts/plain.py"],
                "missing_header": ["scripts/bare.py"],
                "missing_docstrings": ["scripts/pack/kind.py", "scripts/plain.py"],
                "missing_readme": ["scripts/pack"],
            },
        )
        baseline = self.scripts / "verification" / "code_rules_baseline.json"
        baseline.parent.mkdir()
        (baseline.parent / "README.md").write_text("# Checks\n", encoding="utf-8")
        known = dict(
            report["rules"]["new"], missing_header=["scripts/bare.py", "gone.py"]
        )
        baseline.write_text(json.dumps(known), encoding="utf-8")
        listed = self.report()
        self.assertEqual(listed["rules"]["new"], {})
        self.assertEqual(listed["rules"]["fixed"], {"missing_header": ["gone.py"]})
        self.assertEqual(listed["status"], "failed")
        audit_code_health.CodeHealth(self.project).report(shrink_baseline=True)
        self.assertEqual(self.report()["status"], "ok")
        self.assertNotIn("gone.py", baseline.read_text(encoding="utf-8"))


class WorkingRulesTests(unittest.TestCase):
    """The repository itself against its rules baseline."""

    def test_the_tree_adds_no_rule_debt_and_lists_no_fixed_entry(self):
        support = Path(audit_code_health.__file__).resolve().parents[2]
        found = audit_code_health.RuleFindings(support).collect()
        rules = audit_code_health.RulesBaseline.beside(support).compare(found)
        self.assertEqual(rules["new"], {})
        self.assertEqual(rules["fixed"], {})


if __name__ == "__main__":
    unittest.main()
