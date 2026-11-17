"""Exercise the code-health command on disposable script trees."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from verification import audit_code_health


class CodeHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autopcb-code-health-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.scripts = self.project / "autopcb" / "scripts"
        self.scripts.mkdir(parents=True)

    def test_clean_sources_pass_both_ruff_checks(self):
        (self.scripts / "sample.py").write_text("value = 1\n", encoding="utf-8")
        result = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(
            audit_code_health.importlib.util, "find_spec", return_value=True
        ):
            with mock.patch.object(
                audit_code_health.subprocess, "run", return_value=result
            ) as run:
                report = audit_code_health.inspect_code(self.project)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["python_files"], 1)
        self.assertEqual(run.call_count, 2)

    def test_syntax_and_format_failures_are_reported(self):
        (self.scripts / "bad.py").write_text("def broken(:\n", encoding="utf-8")
        result = mock.Mock(returncode=1, stdout="format failure", stderr="")
        with mock.patch.object(
            audit_code_health.importlib.util, "find_spec", return_value=True
        ):
            with mock.patch.object(
                audit_code_health.subprocess, "run", return_value=result
            ):
                report = audit_code_health.inspect_code(self.project)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["syntax_errors"][0]["file"], "bad.py")
        self.assertEqual(report["ruff_format"]["status"], "failed")

    def test_missing_ruff_is_visible_and_not_reported_as_a_pass(self):
        (self.scripts / "sample.py").write_text("pass\n", encoding="utf-8")
        with mock.patch.object(
            audit_code_health.importlib.util, "find_spec", return_value=None
        ):
            report = audit_code_health.inspect_code(self.project)
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["ruff_lint"]["status"], "unavailable")

    def test_empty_script_tree_is_not_reported_as_a_pass(self):
        with self.assertRaisesRegex(ValueError, "No Python files"):
            audit_code_health.inspect_code(self.project)

    def test_formatter_timeout_is_reported(self):
        (self.scripts / "sample.py").write_text("pass\n", encoding="utf-8")
        timeout = audit_code_health.subprocess.TimeoutExpired("ruff", 120)
        with mock.patch.object(
            audit_code_health.importlib.util, "find_spec", return_value=True
        ):
            with mock.patch.object(
                audit_code_health.subprocess, "run", side_effect=timeout
            ):
                report = audit_code_health.inspect_code(self.project)
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("timed out", report["ruff_format"]["reason"])
