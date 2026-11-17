"""Read-only ERC checks do not copy or modify the native project."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.project import ProjectPaths, SourceSnapshot
from verification.audit_schematic_erc import ErcAudit, audit, main


class ReadOnlyErcTests(unittest.TestCase):
    def project(self, root: Path) -> ProjectPaths:
        project = ProjectPaths(root)
        project.schematic.write_text("schematic")
        project.board.write_text("board")
        project.rules.write_text("rules")
        project.settings.write_text(
            json.dumps(
                {
                    "erc": {
                        "rule_severities": {"single_global_label": "ignore"},
                        "erc_exclusions": [],
                    }
                }
            )
        )
        return project

    @staticmethod
    def result(command, **_kwargs):
        report = Path(command[command.index("-o") + 1])
        report.write_text(
            json.dumps(
                {
                    "kicad_version": "test",
                    "sheets": [
                        {
                            "violations": [
                                {
                                    "severity": "warning",
                                    "description": "example warning",
                                }
                            ]
                        }
                    ],
                }
            )
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def test_audit_uses_saved_schematic_and_preserves_all_project_files(self):
        with tempfile.TemporaryDirectory(prefix="jessie-erc-test-") as work:
            project = self.project(Path(work))
            before = SourceSnapshot.capture(project)
            with mock.patch(
                "verification.audit_schematic_erc.subprocess.run",
                side_effect=self.result,
            ) as run:
                result = audit(project, Path("kicad-cli"))
            before.verify_unchanged()
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][-1], str(project.schematic))
            self.assertEqual(result.severity_counts, {"warning": 1})
            self.assertEqual(result.ignored_categories, ["single_global_label"])
            self.assertEqual(result.individual_exclusions, 0)
            self.assertEqual(
                {path.name for path in project.root.iterdir()},
                {
                    "v4.1.kicad_sch",
                    "v4.1.kicad_pcb",
                    "v4.1.kicad_pro",
                    "v4.1.kicad_dru",
                },
            )

    def test_changed_source_rejects_result(self):
        with tempfile.TemporaryDirectory(prefix="jessie-erc-test-") as work:
            project = self.project(Path(work))

            def change_source(command, **kwargs):
                result = self.result(command, **kwargs)
                project.schematic.write_text("changed externally")
                return result

            with mock.patch(
                "verification.audit_schematic_erc.subprocess.run",
                side_effect=change_source,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed during ERC"):
                    audit(project, Path("kicad-cli"))

    def test_missing_violation_list_is_not_reported_as_clean(self):
        with tempfile.TemporaryDirectory(prefix="jessie-erc-test-") as work:
            project = self.project(Path(work))

            def malformed(command, **_kwargs):
                Path(command[command.index("-o") + 1]).write_text('{"sheets": [{}]}')
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch(
                "verification.audit_schematic_erc.subprocess.run", side_effect=malformed
            ):
                with self.assertRaisesRegex(ValueError, "invalid violation list"):
                    audit(project, Path("kicad-cli"))

    def test_project_internal_report_path_is_rejected_before_audit(self):
        with tempfile.TemporaryDirectory(prefix="jessie-erc-test-") as work:
            project = self.project(Path(work))
            args = [
                "erc",
                "--project",
                str(project.root),
                "--out",
                str(project.root / "extra.json"),
            ]
            with (
                mock.patch.object(sys, "argv", args),
                mock.patch("verification.audit_schematic_erc.audit") as run,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    main()
            run.assert_not_called()
            self.assertFalse((project.root / "extra.json").exists())

    def test_unknown_severity_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory(prefix="jessie-erc-test-") as work:
            project = self.project(Path(work))
            result = ErcAudit(
                {}, "test", [], 0, [{"description": "unspecified severity"}]
            )
            args = ["erc", "--project", str(project.root)]
            with (
                mock.patch.object(sys, "argv", args),
                mock.patch(
                    "verification.audit_schematic_erc.find_cli",
                    return_value=Path("cli"),
                ),
                mock.patch(
                    "verification.audit_schematic_erc.audit", return_value=result
                ),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(main(), 1)
            self.assertIn("unknown severity", output.getvalue())


if __name__ == "__main__":
    unittest.main()
