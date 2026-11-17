"""Focused tests for the single Jessie tools entry point."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import launcher as run

SCRIPTS = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def test_interruption_does_not_print_a_python_traceback(self):
        messages = io.StringIO()
        with mock.patch.object(run.subprocess, "run", side_effect=KeyboardInterrupt):
            with contextlib.redirect_stderr(messages):
                try:
                    status = run.main(
                        ["--kicad-python", sys.executable, "pcb", "checkpoints", "plan"]
                    )
                except KeyboardInterrupt:
                    self.fail("Launcher leaked KeyboardInterrupt")
        self.assertEqual(status, 130)
        self.assertIn("stopped", messages.getvalue().lower())
        self.assertNotIn("Traceback", messages.getvalue())

    def args(self, group, action=None):
        return argparse.Namespace(
            project=Path("example-project"),
            kicad_python=Path("kicad-python.exe"),
            group=group,
            action=action,
        )

    def test_action_specs_are_immutable_and_staging_is_not_exposed(self):
        action = run.ACTIONS["check"]["power"]
        self.assertEqual(action.module, "verification.audit_pcb_power_terminals")
        with self.assertRaises(AttributeError):
            action.module = "other"
        self.assertNotIn("qa-stage", run.ACTIONS["pcb"])

    def test_registered_actions_resolve_to_modules(self):
        for group, actions in run.ACTIONS.items():
            for name, action in actions.items():
                with self.subTest(group=group, action=name):
                    self.assertIsNotNone(importlib.util.find_spec(action.module))

    def test_invalid_action_spec_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported action runtime"):
            run.ActionSpec("example", "bad", "none")
        with self.assertRaisesRegex(ValueError, "Unsupported action mode"):
            run.ActionSpec("example", "python", "bad")

    def test_check_net_action_is_read_only(self):
        command = run.command_for(self.args("check", "nets"), [])
        self.assertIn("--dry-run", command)
        self.assertNotIn("--apply", command)
        with self.assertRaisesRegex(ValueError, "read-only"):
            run.command_for(self.args("check", "nets"), ["--apply"])

    def test_code_health_action_uses_the_project_without_opening_a_design(self):
        command = run.command_for(self.args("check", "code"), [])
        self.assertIn("verification.audit_code_health", command)
        self.assertIn("--project", command)
        self.assertNotIn("--apply", command)

    def test_schematic_actions_default_to_check(self):
        command = run.command_for(self.args("schematic", "style"), [])
        self.assertIn("--check", command)
        self.assertNotIn("--apply", command)
        command = run.command_for(self.args("schematic", "style"), ["--apply"])
        self.assertIn("--apply", command)
        self.assertNotIn("--check", command)

    def test_review_targets_existing_project_gallery(self):
        project = Path("example-project").resolve()
        command = run.command_for(self.args("review", "refresh"), [])
        self.assertIn(str(project / "deliverables" / "review"), command)
        self.assertEqual(command[3], "review.refresh_gallery")

    def test_project_after_action_resolves_from_caller_for_router_and_review(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-cwd-") as tmp:
            caller = Path(tmp)
            with mock.patch.object(Path, "cwd", return_value=caller):
                for group, action in (("pcb", "freeroute-fast"), ("review", "refresh")):
                    for option in (["--project", "."], ["--project=."]):
                        with self.subTest(group=group, option=option):
                            command = run.command_for(self.args(group, action), option)
                            self.assertEqual(command.count("--project"), 1)
                            self.assertEqual(command[command.index("--project") + 1], str(caller))
                            if group == "review":
                                self.assertEqual(command[command.index("--review") + 1],
                                                 str(caller / "deliverables" / "review"))

    def test_route_configs_resolve_from_the_callers_current_directory(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-cwd-") as tmp:
            caller = Path(tmp)
            config = caller / "config" / "route.json"
            with mock.patch.object(Path, "cwd", return_value=caller):
                for action in ("plan-route", "plan-batch", "plan-nets", "optimize"):
                    with self.subTest(action=action, style="separate"):
                        command = run.command_for(
                            self.args("pcb", action),
                            ["--config", "config/route.json", "--validate"],
                        )
                        self.assertEqual(
                            command[command.index("--config") + 1], str(config)
                        )
                    with self.subTest(action=action, style="equals"):
                        command = run.command_for(
                            self.args("pcb", action), ["--config=config/route.json"]
                        )
                        self.assertIn(f"--config={config}", command)

    def test_absolute_or_incomplete_route_config_arguments_are_preserved(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-config-") as tmp:
            config = str(Path(tmp) / "route.json")
            for forwarded in (
                ["--config", config],
                [f"--config={config}"],
                ["--config"],
                ["--config="],
            ):
                with self.subTest(forwarded=forwarded):
                    command = run.command_for(self.args("pcb", "plan-route"), forwarded)
                    self.assertEqual(command[-len(forwarded) :], forwarded)

    def test_help_and_invalid_action_do_not_execute_modules(self):
        help_result = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "run.py"), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("review", help_result.stdout)
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "run.py"), "remove-old-versions"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_test_output_keeps_failures_and_summarizes_only_duplicate_handlers(self):
        output = io.StringIO()
        run._relay_test_output(
            [
                "Adding duplicate image handler for 'PNG file'\n",
                "other warning\n",
                "Adding duplicate image handler for 'JPEG file'\n",
                "FAIL: a test failed\n",
            ],
            output,
        )
        result = output.getvalue()
        self.assertIn("Adding duplicate image handler for 'PNG file'", result)
        self.assertNotIn("JPEG file", result)
        self.assertIn("other warning", result)
        self.assertIn("FAIL: a test failed", result)
        self.assertIn("1 further duplicate image-handler messages", result)

    def test_test_command_defaults_to_quiet_and_accepts_verbose(self):
        with mock.patch.object(run, "run_test_suite", return_value=17) as suite:
            self.assertEqual(run.main(["--kicad-python", sys.executable, "test"]), 17)
            self.assertIn("-q", suite.call_args.args[0])
            self.assertEqual(
                run.main(["--kicad-python", sys.executable, "test", "-v"]), 17
            )
            self.assertIn("-v", suite.call_args.args[0])

    def test_test_command_can_show_all_warnings(self):
        process = mock.Mock(returncode=19)
        with mock.patch.object(run, "run_test_suite") as summarized:
            with mock.patch.object(run, "resolve_document_python", return_value=Path(sys.executable)):
                with mock.patch.object(run.subprocess, "run", return_value=process) as raw:
                    result = run.main(
                        ["--kicad-python", sys.executable, "test", "--all-warnings"]
                    )
        self.assertEqual(result, 19)
        summarized.assert_not_called()
        self.assertIn("-q", raw.call_args.args[0])

    def test_test_command_also_runs_pdf_tests_with_document_python(self):
        with mock.patch.object(run, "run_test_suite", side_effect=[0, 17]) as suite:
            self.assertEqual(run.main(["--kicad-python", sys.executable, "test"]), 17)
        self.assertEqual(suite.call_count, 2)
        self.assertIn(
            "verification.test_pdf_builder", " ".join(suite.call_args_list[1].args[0])
        )

    def test_missing_pdf_test_runtime_fails_before_kicad_tests(self):
        with mock.patch.object(run, "run_test_suite", return_value=0) as suite:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    run.main(
                        [
                            "--kicad-python",
                            sys.executable,
                            "test",
                            "--document-python",
                            "no-python.exe",
                        ]
                    ),
                    2,
                )
        self.assertEqual(suite.call_count, 0)

    def test_document_python_falls_back_to_a_usable_path_interpreter(self):
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory) / "kicad-python.exe"
            fallback = Path(directory) / "python.exe"
            current.touch()
            fallback.touch()
            with mock.patch.object(run.sys, "executable", str(current)):
                with mock.patch("shutil.which", return_value=str(fallback)):
                    with mock.patch.object(
                        run, "_has_document_dependency",
                        side_effect=lambda candidate: candidate.resolve() == fallback.resolve(),
                    ):
                        self.assertEqual(run.resolve_document_python(None), fallback.resolve())

    def test_explicit_document_python_does_not_silently_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            requested = Path(directory) / "python.exe"
            requested.touch()
            with mock.patch.object(run, "_has_document_dependency", return_value=False):
                with self.assertRaisesRegex(ValueError, "markdown-it-py"):
                    run.resolve_document_python(requested)

    def test_pdf_test_command_reports_a_missing_markdown_dependency(self):
        with mock.patch.object(run, "run_test_suite", side_effect=[0, 0]) as suite:
            self.assertEqual(run.main(["--kicad-python", sys.executable, "test"]), 0)
        document_command = suite.call_args_list[1].args[0]
        result = subprocess.run(
            [document_command[0], "-I", "-S", *document_command[1:]],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("markdown-it-py", result.stderr)

    def test_missing_test_runtime_fails_cleanly(self):
        with mock.patch.object(run, "run_test_suite") as suite:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    run.main(["--kicad-python", "no-such-python.exe", "test"]), 2
                )
            suite.assert_not_called()

    def test_held_release_manifest_blocks_even_matching_hashes(self):
        from core.export_helpers import factory_input_dir, sha

        with tempfile.TemporaryDirectory(prefix="jessie-release-test-") as tmp:
            project = Path(tmp)
            inputs = project / "design" / "release_inputs"
            inputs.mkdir(parents=True)
            item = inputs / "part.csv"
            item.write_text("valid")
            (inputs / "INPUT_MANIFEST.json").write_text(
                json.dumps(
                    {
                        "status": "STALE - RELEASE BLOCKED PENDING FULL REVIEW",
                        "files": {item.name: sha(item)},
                    }
                )
            )
            with self.assertRaisesRegex(RuntimeError, "on hold"):
                factory_input_dir(project)

    def test_review_status_requires_every_named_view(self):
        with tempfile.TemporaryDirectory(prefix="jessie-view-test-") as tmp:
            project = Path(tmp)
            for suffix in (".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_dru"):
                (project / ("v4.1" + suffix)).write_text("source")
            review = project / "deliverables" / "review"
            review.mkdir(parents=True)
            for name in run.REVIEW_FILES:
                (review / name).write_text("view")
            sources = {
                name: run.sha(project / name)
                for name in ("v4.1.kicad_sch", "v4.1.kicad_pcb")
            }
            (review / "source.json").write_text(
                json.dumps({"sha256": sources, "dependency_sha256": {}})
            )
            (review / "PCB_In2.png").unlink()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(run.show_status(project, json_mode=True), 0)
            self.assertEqual(
                json.loads(output.getvalue())["review_views"], "missing or stale"
            )

    def test_review_status_requires_both_native_sources(self):
        with tempfile.TemporaryDirectory(prefix="jessie-view-test-") as tmp:
            project = Path(tmp)
            for suffix in (".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_dru"):
                (project / ("v4.1" + suffix)).write_text("source")
            review = project / "deliverables" / "review"
            review.mkdir(parents=True)
            for name in run.REVIEW_FILES:
                (review / name).write_text("view")
            renderer = project / "scripts" / "review" / "render_views.py"
            renderer.parent.mkdir(parents=True)
            renderer.write_text("renderer")
            dependencies = {renderer.relative_to(project).as_posix(): run.sha(renderer)}
            source = {"v4.1.kicad_pcb": run.sha(project / "v4.1.kicad_pcb")}
            (review / "source.json").write_text(
                json.dumps({"sha256": source, "dependency_sha256": dependencies})
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(run.show_status(project, json_mode=True), 0)
            self.assertEqual(
                json.loads(output.getvalue())["review_views"], "missing or stale"
            )

    def test_external_dependency_path_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="jessie-view-test-") as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            outside = Path(tmp) / "outside"
            outside.write_text("outside")
            self.assertFalse(
                run.matching_hashes(root, {"../outside": run.sha(outside)})
            )

    def test_status_reports_stale_release_without_writing(self):
        with tempfile.TemporaryDirectory(prefix="jessie-status-test-") as tmp:
            project = Path(tmp)
            for suffix in (".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_dru"):
                (project / ("v4.1" + suffix)).write_text("test")
            inputs = project / "design" / "release_inputs"
            inputs.mkdir(parents=True)
            (inputs / "held.csv").write_text("changed")
            (inputs / "INPUT_MANIFEST.json").write_text(
                json.dumps({"status": "STALE", "files": {"held.csv": "old"}})
            )
            before = {
                p.relative_to(project): p.read_bytes()
                for p in project.rglob("*")
                if p.is_file()
            }
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(run.show_status(project, json_mode=True), 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data["release_input_mismatches"], ["held.csv"])
            after = {
                p.relative_to(project): p.read_bytes()
                for p in project.rglob("*")
                if p.is_file()
            }
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
