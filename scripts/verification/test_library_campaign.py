"""Exercise guarded library campaigns on disposable KiCad projects."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.kicad_campaign import CampaignFile
from core import toolchain
from core.library_campaign_cli import apply, main, prepare
from core.library_edits import LibraryInputs, library_path, plan_library
from core.library_validation import validate_library

CLI = toolchain._executable(None)
CLI_AVAILABLE = CLI is not None and CLI.is_file()


def _kicad_python() -> Path:
    """The KiCad Python the product picks for this CLI, as in the macOS app bundle."""
    return toolchain.native_python_candidates(CLI)[0]


FOOTPRINT = (
    '(footprint "Probe" (version 20221018) (generator "pcbnew") (layer "F.Cu") '
    '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask")))'
)
SYMBOL = (
    '(kicad_symbol_lib (version 20231120) (generator "kicad_symbol_editor") '
    '(symbol "Probe" (pin_names (offset 0)) (exclude_from_sim no) '
    "(in_bom yes) (on_board yes) "
    '(property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27)))) '
    '(property "Value" "Probe" (at 0 -1 0) (effects (font (size 1.27 1.27)))) '
    '(symbol "Probe_0_1" (rectangle (start -2.54 2.54) (end 2.54 -2.54) '
    "(stroke (width 0) (type default)) (fill (type background))))))"
)


class LibraryCampaignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="jessie-library-campaign-test-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.library = self.root / "libraries" / "Probe.pretty"
        self.library.mkdir(parents=True)
        self.footprint = self.library / "Probe.kicad_mod"
        self.footprint.write_text(FOOTPRINT, encoding="utf-8")
        self.symbol = self.root / "libraries" / "Probe.kicad_sym"
        self.symbol.write_text(SYMBOL, encoding="utf-8")
        self.board = self.root / "v4.1.kicad_pcb"
        self.board.write_text("(kicad_pcb)", encoding="utf-8")
        self.schematic = self.root / "v4.1.kicad_sch"
        self.schematic.write_text("(kicad_sch)", encoding="utf-8")
        self.fake_cli = self.root / "fake-cli.exe"
        self.fake_cli.write_text("fake CLI", encoding="utf-8")
        self.document = "libraries/Probe.pretty/Probe.kicad_mod"
        self.change = {
            "document": self.document,
            "target": {"kind": "root"},
            "op": "replace",
            "path": [{"name": "pad", "first": "1"}, "size"],
            "expression": "(size 2 1)",
        }
        self.campaign = CampaignFile(self.root).create(
            "Library test", changes=[self.change]
        )

    def prepare_mocked(self):
        with mock.patch("core.library_campaign_cli.validate_library"):
            return prepare(self.root, self.campaign.name, self.document, self.fake_cli)

    def apply_arguments(self, edit):
        return {"preview_sha256": edit.preview_sha256}

    def test_plan_replaces_only_selected_expression_without_writing(self) -> None:
        before = self.footprint.read_bytes()
        result = plan_library(self.root, self.document, [self.change])
        self.assertEqual(result.before, before)
        self.assertEqual(result.after, before.replace(b"(size 1 1)", b"(size 2 1)"))
        self.assertEqual(self.footprint.read_bytes(), before)
        self.assertEqual(len(result.edits), 1)

    def test_plan_preserves_unrelated_crlf_and_unicode(self) -> None:
        source = (FOOTPRINT.replace('"Probe"', '"Prøbe"') + "\r\n").encode("utf-8")
        self.footprint.write_bytes(source)
        result = plan_library(self.root, self.document, [self.change])
        self.assertEqual(result.after, source.replace(b"(size 1 1)", b"(size 2 1)"))
        self.assertTrue(result.after.endswith(b"\r\n"))
        self.assertEqual(self.footprint.read_bytes(), source)

    def test_multiple_insertions_preserve_campaign_order(self) -> None:
        changes = [
            {
                "target": {"kind": "root"},
                "op": "insert",
                "expression": '(marker "first")',
            },
            {
                "target": {"kind": "root"},
                "op": "insert",
                "expression": '(marker "second")',
            },
        ]
        result = plan_library(self.root, self.document, changes)
        self.assertLess(
            result.after.index(b'(marker "first")'),
            result.after.index(b'(marker "second")'),
        )
        self.assertEqual(self.footprint.read_text(encoding="utf-8"), FOOTPRINT)

    def test_noop_apply_does_not_rewrite_the_library(self) -> None:
        data = json.loads(self.campaign.read_text(encoding="utf-8"))
        data["changes"][0]["expression"] = "(size 1 1)"
        self.campaign.write_text(json.dumps(data), encoding="utf-8")
        edit = self.prepare_mocked()
        self.assertFalse(edit.plan.edits)
        with mock.patch("core.library_campaign_cli.os.replace") as replace:
            self.assertEqual(apply(edit, **self.apply_arguments(edit)), "no_changes")
        replace.assert_not_called()
        self.assertEqual(self.footprint.read_text(encoding="utf-8"), FOOTPRINT)

    def test_symbol_library_uses_nested_identifier_path(self) -> None:
        change = {
            "target": {"kind": "root"},
            "op": "replace",
            "path": [
                {"name": "symbol", "first": "Probe"},
                {"name": "property", "first": "Value"},
            ],
            "expression": '(property "Value" "Changed" (at 0 -1 0) (effects (font (size 1.27 1.27))))',
        }
        plan = plan_library(self.root, "libraries/Probe.kicad_sym", [change])
        self.assertIn(b'(property "Value" "Changed"', plan.after)
        self.assertEqual(self.symbol.read_text(encoding="utf-8"), SYMBOL)

    def test_rejects_outside_unsupported_or_ambiguous_targets(self) -> None:
        with self.assertRaisesRegex(ValueError, "inside"):
            library_path(self.root, "v4.1.kicad_pcb")
        with self.assertRaisesRegex(ValueError, "relative"):
            library_path(self.root, str(self.footprint))
        with self.assertRaisesRegex(ValueError, "target the root"):
            plan_library(
                self.root, self.document, [{**self.change, "target": {"kind": "pad"}}]
            )
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            plan_library(self.root, self.document, [self.change, self.change])

    def test_input_snapshot_detects_changes_and_new_library_files(self) -> None:
        snapshot = LibraryInputs.capture(self.root)
        self.board.write_text("(kicad_pcb changed)", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            snapshot.verify_unchanged()
        snapshot = LibraryInputs.capture(self.root)
        (self.library / "New.kicad_mod").write_text(FOOTPRINT, encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            snapshot.verify_unchanged()

    def test_apply_requires_matching_hashes_and_preserves_native_designs(self) -> None:
        edit = self.prepare_mocked()
        native_before = (self.board.read_bytes(), self.schematic.read_bytes())
        args = self.apply_arguments(edit)
        with self.assertRaisesRegex(RuntimeError, "Preview hash"):
            apply(edit, **{**args, "preview_sha256": "0" * 64})
        self.assertEqual(self.footprint.read_text(encoding="utf-8"), FOOTPRINT)
        self.assertEqual(apply(edit, **args), "applied")
        self.assertIn("(size 2 1)", self.footprint.read_text(encoding="utf-8"))
        self.assertEqual(
            (self.board.read_bytes(), self.schematic.read_bytes()), native_before
        )

    def test_changed_inputs_campaign_or_editor_lock_prevent_apply(self) -> None:
        edit = self.prepare_mocked()
        self.board.write_text("(kicad_pcb changed)", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "inputs changed"):
            apply(edit, **self.apply_arguments(edit))
        edit = self.prepare_mocked()
        self.campaign.write_text(self.campaign.read_text() + " ", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Campaign changed"):
            apply(edit, **self.apply_arguments(edit))
        edit = self.prepare_mocked()
        self.fake_cli.write_text("changed CLI", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "KiCad CLI changed"):
            apply(edit, **self.apply_arguments(edit))
        edit = self.prepare_mocked()
        (self.root / "~v4.1.kicad_pcb.lck").write_text("open", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "KiCad lock"):
            apply(edit, **self.apply_arguments(edit))
        self.assertEqual(self.footprint.read_text(encoding="utf-8"), FOOTPRINT)

    def test_preview_rejects_source_changes_during_native_validation(self) -> None:
        def change_source(*args, **kwargs):
            self.board.write_text("(kicad_pcb changed)", encoding="utf-8")

        with mock.patch(
            "core.library_campaign_cli.validate_library", side_effect=change_source
        ):
            with self.assertRaisesRegex(RuntimeError, "inputs changed"):
                prepare(self.root, self.campaign.name, self.document, self.fake_cli)
        self.assertEqual(self.footprint.read_text(encoding="utf-8"), FOOTPRINT)

    def test_refuses_zero_exit_without_a_real_export(self) -> None:
        plan = plan_library(self.root, self.document, [self.change])
        fake_cli = self.root / "fake-cli.exe"
        fake_cli.write_text("placeholder", encoding="utf-8")
        result = subprocess.CompletedProcess(
            [], 0, stdout="Error creating svg file", stderr=""
        )
        with mock.patch("core.library_validation.subprocess.run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "KiCad could not export"):
                validate_library(plan, fake_cli)

    def test_symbol_validation_requires_an_export_for_every_symbol(self) -> None:
        start = SYMBOL.index('(symbol "Probe"')
        first = SYMBOL[start:-1]
        second = first.replace('"Probe"', '"Second"', 1).replace(
            '"Probe_0_1"', '"Second_0_1"'
        )
        self.symbol.write_text(SYMBOL[:-1] + " " + second + ")", encoding="utf-8")
        change = {
            "target": {"kind": "root"},
            "op": "replace",
            "path": [
                {"name": "symbol", "first": "Probe"},
                {"name": "property", "first": "Value"},
            ],
            "expression": '(property "Value" "Changed" (at 0 -1 0) (effects (font (size 1.27 1.27))))',
        }
        plan = plan_library(self.root, "libraries/Probe.kicad_sym", [change])
        fake_cli = self.root / "fake-cli.exe"
        fake_cli.write_text("placeholder", encoding="utf-8")
        calls = []

        def incomplete_export(command, **kwargs):
            name = command[command.index("--symbol") + 1]
            calls.append(name)
            if name == "Probe":
                output = Path(command[command.index("--output") + 1])
                (output / "Probe_unit1.svg").write_text("<svg/>", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch(
            "core.library_validation.subprocess.run", side_effect=incomplete_export
        ):
            with self.assertRaisesRegex(RuntimeError, "KiCad could not export"):
                validate_library(plan, fake_cli)
        self.assertEqual(calls, ["Probe", "Second"])

    @unittest.skipUnless(CLI_AVAILABLE, "KiCad CLI is unavailable")
    def test_real_kicad_exports_both_library_types(self) -> None:
        footprint = plan_library(self.root, self.document, [self.change])
        validate_library(footprint, CLI)
        symbol_change = {
            "target": {"kind": "root"},
            "op": "replace",
            "path": [
                {"name": "symbol", "first": "Probe"},
                {"name": "property", "first": "Value"},
            ],
            "expression": '(property "Value" "Changed" (at 0 -1 0) (effects (font (size 1.27 1.27))))',
        }
        symbol = plan_library(self.root, "libraries/Probe.kicad_sym", [symbol_change])
        validate_library(symbol, CLI)
        self.assertEqual(self.footprint.read_text(encoding="utf-8"), FOOTPRINT)
        self.assertEqual(self.symbol.read_text(encoding="utf-8"), SYMBOL)

    @unittest.skipUnless(CLI_AVAILABLE, "KiCad CLI is unavailable")
    def test_cli_preview_and_apply_are_bound_to_reviewed_candidate(self) -> None:
        before = self.footprint.read_bytes()
        output = io.StringIO()
        arguments = [
            "--project",
            str(self.root),
            "preview",
            self.campaign.name,
            "--document",
            self.document,
            "--cli",
            str(CLI),
        ]
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(arguments), 0)
        preview = json.loads(output.getvalue())
        self.assertEqual(preview["status"], "preview")
        self.assertEqual(self.footprint.read_bytes(), before)
        apply_args = [
            "--project",
            str(self.root),
            "apply",
            self.campaign.name,
            "--document",
            self.document,
            "--cli",
            str(CLI),
        ]
        apply_args += ["--expected-preview-sha256", preview["preview_sha256"]]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(apply_args), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "applied")
        self.assertIn("(size 2 1)", self.footprint.read_text(encoding="utf-8"))
        self.assertEqual(self.board.read_text(encoding="utf-8"), "(kicad_pcb)")
        self.assertEqual(self.schematic.read_text(encoding="utf-8"), "(kicad_sch)")

    @unittest.skipUnless(CLI_AVAILABLE, "KiCad CLI is unavailable")
    def test_standard_campaign_can_add_a_library_change(self) -> None:
        scripts = Path(__file__).resolve().parents[1]
        kicad_python = _kicad_python()
        base = [
            str(kicad_python),
            "-B",
            str(scripts / "run.py"),
            "--project",
            str(self.root),
            "--kicad-python",
            str(kicad_python),
        ]
        add = subprocess.run(
            base
            + [
                "campaign",
                "add-expression",
                self.campaign.name,
                "--document",
                self.document,
                "--kind",
                "root",
                "--operation",
                "replace",
                "--path",
                json.dumps([{"name": "pad", "first": "1"}, "at"]),
                "--expression",
                "(at 0 0)",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        self.assertEqual(add.returncode, 0, add.stderr)
        preview = subprocess.run(
            base
            + ["library", "preview", self.campaign.name, "--document", self.document],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        self.assertEqual(preview.returncode, 0, preview.stderr)
        result = json.loads(preview.stdout)
        self.assertEqual(result["selected_changes"], 2)
        self.assertEqual(result["effective_edits"], 1)
        self.assertEqual(self.footprint.read_text(encoding="utf-8"), FOOTPRINT)

    @unittest.skipUnless(CLI_AVAILABLE, "KiCad CLI is unavailable")
    def test_single_entrypoint_dispatches_library_preview(self) -> None:
        scripts = Path(__file__).resolve().parents[1]
        kicad_python = _kicad_python()
        result = subprocess.run(
            [
                str(kicad_python),
                "-B",
                str(scripts / "run.py"),
                "--project",
                str(self.root),
                "--kicad-python",
                str(kicad_python),
                "library",
                "preview",
                self.campaign.name,
                "--document",
                self.document,
                "--cli",
                str(CLI),
                "--diff",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[0])["status"], "preview")
        self.assertIn("--- libraries/Probe.pretty/Probe.kicad_mod", result.stdout)
        self.assertIn("(size 2 1)", result.stdout)
        self.assertEqual(self.footprint.read_text(encoding="utf-8"), FOOTPRINT)


if __name__ == "__main__":
    unittest.main()
