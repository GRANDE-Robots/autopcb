"""test_native_command.py: check that native crashes and stale or missing reports never
pass validation.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.native_command import NativeCommandError, NativeResourceError, run_checked


class NativeCommandTests(unittest.TestCase):
    """Checks how checked native commands report failures and outputs."""

    def test_silent_failure_retains_exit_status(self):
        with mock.patch(
            "core.native_command.subprocess.run",
            return_value=subprocess.CompletedProcess(["tool"], 7, "", ""),
        ):
            with self.assertRaisesRegex(NativeCommandError, "exit 7.*no diagnostic"):
                run_checked(["tool"], operation="Native check")

    def test_both_output_streams_are_retained(self):
        with mock.patch(
            "core.native_command.subprocess.run",
            return_value=subprocess.CompletedProcess(["tool"], 1, "why", "what"),
        ):
            with self.assertRaises(NativeCommandError) as caught:
                run_checked(["tool"], operation="Native check")
        self.assertIn("what\nwhy", str(caught.exception))

    def test_native_commit_exhaustion_is_distinct_from_a_design_failure(self):
        with mock.patch(
            "core.native_command.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["tool"], 3221226505, "", "error 1455: paging file is too small"
            ),
        ):
            with self.assertRaises(NativeResourceError) as caught:
                run_checked(["tool"], operation="KiCad DRC")
        self.assertIsInstance(caught.exception, MemoryError)

    def test_timeout_keeps_partial_diagnostics(self):
        error = subprocess.TimeoutExpired(
            ["tool"], 3, output=b"loading", stderr=b"slow"
        )
        with mock.patch("core.native_command.subprocess.run", side_effect=error):
            with self.assertRaisesRegex(
                NativeCommandError, "timed out after 3"
            ) as caught:
                run_checked(["tool"], operation="Native check", timeout=3)
        self.assertIn("slow\nloading", str(caught.exception))

    def test_success_requires_a_new_report_and_cannot_reuse_a_stale_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "check.json"
            success = subprocess.CompletedProcess(["tool"], 0, "", "")
            with mock.patch("core.native_command.subprocess.run", return_value=success):
                with self.assertRaisesRegex(NativeCommandError, "did not produce"):
                    run_checked(["tool"], operation="Native check", output=output)
            output.write_text("old")
            with mock.patch("core.native_command.subprocess.run") as process:
                with self.assertRaisesRegex(NativeCommandError, "existing output"):
                    run_checked(["tool"], operation="Native check", output=output)
                process.assert_not_called()
            output.unlink()

            def produce(*args, **kwargs):
                """Stand in for the process: write the report it must produce.

                The call fails the test unless stdin is closed.
                """
                output.write_text("new")
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                return success

            with mock.patch("core.native_command.subprocess.run", side_effect=produce):
                self.assertEqual(
                    run_checked(["tool"], operation="Native check", output=output),
                    success,
                )

    def test_missing_executable_is_actionable(self):
        with mock.patch(
            "core.native_command.subprocess.run", side_effect=FileNotFoundError("tool")
        ):
            with self.assertRaisesRegex(NativeCommandError, "could not start"):
                run_checked(["tool"], operation="Native check")

    def test_process_creation_memory_failure_keeps_its_category(self):
        with mock.patch(
            "core.native_command.subprocess.run",
            side_effect=OSError(12, "memory exhausted"),
        ):
            with self.assertRaises(NativeResourceError):
                run_checked(["tool"], operation="Native check")
