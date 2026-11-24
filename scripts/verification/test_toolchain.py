"""test_toolchain.py: check tool discovery and version-dependent behavior without a live
editor.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import toolchain
from verification.inspect_kicad_toolchain import main

CLI = toolchain.kicad_cli.locate(None)
CLI_AVAILABLE = CLI is not None and CLI.is_file()


class ToolchainTests(unittest.TestCase):
    """Checks KiCad tool discovery, capability reports and the inspect command."""

    def setUp(self) -> None:
        """Create a temporary folder under the system temporary directory.

        Sets ``root`` to it and removes it after the test.
        """
        self.temporary = tempfile.TemporaryDirectory(prefix="jessie-toolchain-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(Path(tempfile.gettempdir()).resolve()))

    def test_integration_tests_follow_the_selected_kicad_cli(self) -> None:
        cli = self.root / "kicad-cli.exe"
        cli.write_text("placeholder", encoding="utf-8")
        environment = {**os.environ, "KICAD_CLI": str(cli)}
        code = (
            "import verification.test_library_campaign as a; "
            "import verification.test_toolchain as b; "
            "print(a.CLI); print(b.CLI)"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [str(cli), str(cli)])

    def test_discovery_reports_capabilities_without_promoting_unchecked_tools(
        self,
    ) -> None:
        cli = self.root / "kicad-cli.exe"
        cli.write_text("placeholder", encoding="utf-8")
        jar = self.root / "freerouting.jar"
        jar.write_text("placeholder", encoding="utf-8")

        def run(executable, args):
            """Stand in for running a KiCad command line query.

            Inputs:
                executable: The command line tool, which must be the one under test.
                args: The query arguments; only the version, help, DRC help and ERC help
                    queries are known.
            Outputs:
                The canned success flag and output text for the query.
            """
            self.assertEqual(executable, cli)
            return {
                ("version",): (True, "10.0.6"),
                ("--help",): (True, "pcb sch sym fp"),
                ("pcb", "drc", "--help"): (True, "--schematic-parity --refill-zones"),
                ("sch", "erc", "--help"): (True, "--format"),
            }[tuple(args)]

        with mock.patch("core.toolchain.ToolchainInspector.run", side_effect=run):
            with mock.patch(
                "core.toolchain.ToolchainInspector.installed",
                side_effect=lambda name: name == "pcbnew",
            ):
                with mock.patch("core.toolchain.shutil.which", return_value="java.exe"):
                    result = toolchain.inspect_toolchain(cli=cli, freerouting_jar=jar)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["kicad_cli"]["pcb_schematic_parity_option"])
        self.assertTrue(result["kicad_cli"]["pcb_refill_zones_option"])
        self.assertFalse(result["kicad_cli"]["cli_headless_api_server"])
        self.assertEqual(
            result["python_modules_in_this_runtime"],
            {
                "pcbnew": True,
                "kipy": False,
                "skidl": False,
            },
        )
        self.assertTrue(result["freerouting"]["jar_exists"])
        self.assertFalse(result["freerouting"]["execution_checked"])

    def test_unavailable_cli_returns_clear_status_and_no_subprocess(self) -> None:
        with mock.patch("core.toolchain.subprocess.run") as runner:
            result = toolchain.inspect_toolchain(cli=self.root / "missing.exe")
        self.assertEqual(result["status"], "kicad_cli_unavailable")
        self.assertIsNone(result["kicad_cli"]["version"])
        runner.assert_not_called()

    def test_failed_and_timed_out_processes_are_reported(self) -> None:
        cli = self.root / "kicad-cli.exe"
        cli.write_text("placeholder", encoding="utf-8")
        with mock.patch(
            "core.toolchain.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="kicad-cli", timeout=10),
        ):
            result = toolchain.inspect_toolchain(cli=cli)
        self.assertEqual(result["status"], "kicad_cli_unavailable")
        self.assertIn("timed out", result["kicad_cli"]["version_error"])

    def test_cli_prints_compact_json_and_failure_status(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                ["--project", str(self.root), "--cli", str(self.root / "missing.exe")]
            )
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(output.getvalue())["status"], "kicad_cli_unavailable"
        )

    @unittest.skipUnless(CLI_AVAILABLE, "KiCad CLI is unavailable")
    def test_installed_cli_is_read_without_touching_project(self) -> None:
        marker = self.root / "v4.1.kicad_pcb"
        marker.write_text("untouched", encoding="utf-8")
        before = {
            p.relative_to(self.root): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }
        result = toolchain.inspect_toolchain(
            cli=CLI, freerouting_jar=self.root / "not-installed.jar"
        )
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["kicad_cli"]["pcb_drc_help"])
        self.assertTrue(result["kicad_cli"]["schematic_erc_help"])
        self.assertFalse(result["freerouting"]["jar_exists"])
        after = {
            p.relative_to(self.root): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after)


class RouterCacheTests(unittest.TestCase):
    """Checks the cache locations of the external router binaries."""

    def test_external_router_binaries_are_named_for_the_host(self):
        name = "fastroute.exe" if os.name == "nt" else "fastroute"
        with (
            tempfile.TemporaryDirectory() as home,
            mock.patch.object(toolchain.Path, "home", return_value=Path(home)),
        ):
            self.assertEqual(
                toolchain.RouterCache.fastroute(),
                Path(home) / ".kicad-mcp" / "fastroute-0.1.14" / name,
            )
            self.assertEqual(
                toolchain.RouterCache.freerouting().name, "freerouting-2.5.0.jar"
            )


if __name__ == "__main__":
    unittest.main()
