"""Exercise library checking and path-only reorganization on disposable projects."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verification import audit_kicad_libraries as audit


class LibraryAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="jessie-lib-test-")
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name).resolve()
        self.assertTrue(
            self.project.is_relative_to(Path(tempfile.gettempdir()).resolve())
        )
        libs = self.project / "libraries"
        (libs / "symbols").mkdir(parents=True)
        (libs / "Local.pretty").mkdir()
        (libs / "models").mkdir()
        (libs / "Local.kicad_sym").write_text(
            '(kicad_symbol_lib (symbol "Part" (property "Footprint" "Local:Spare")))',
            encoding="utf-8",
        )
        (libs / "Local.pretty/Part.kicad_mod").write_text(
            '(footprint "Part" (model "${KIPRJMOD}/libraries/models/part.step"))',
            encoding="utf-8",
        )
        (libs / "Local.pretty/Spare.kicad_mod").write_text(
            '(footprint "Spare")', encoding="utf-8"
        )
        (libs / "models/part.step").write_text("model", encoding="utf-8")
        (self.project / "sym-lib-table").write_text(
            '(sym_lib_table (lib (name "LocalSym") (uri "${KIPRJMOD}/libraries/Local.kicad_sym")))',
            encoding="utf-8",
        )
        (self.project / "fp-lib-table").write_text(
            '(fp_lib_table (lib (name "Local") (uri "${KIPRJMOD}/libraries/Local.pretty")))',
            encoding="utf-8",
        )
        (self.project / "v4.1.kicad_sch").write_text(
            '(kicad_sch (symbol (lib_id "LocalSym:Part") (property "Footprint" "Local:Part"))'
            ' (symbol (lib_id "power:GND")))',
            encoding="utf-8",
        )
        (self.project / "v4.1.kicad_pcb").write_text(
            '(kicad_pcb (footprint "Local:Part" (model "${KIPRJMOD}/libraries/models/part.step"))'
            ' (footprint "Connector_Molex:External"))',
            encoding="utf-8",
        )

    def test_resolves_local_and_distinguishes_external_dependencies(self) -> None:
        before = {
            p.relative_to(self.project): p.read_bytes()
            for p in self.project.rglob("*")
            if p.is_file()
        }
        result = audit.audit(self.project)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["external_symbol_nicknames"], {"power": 1})
        self.assertEqual(result["external_footprint_nicknames"], {"Connector_Molex": 1})
        self.assertEqual(result["local_model_paths_checked"], 1)
        self.assertEqual(
            result["footprints_not_used_by_live_design"],
            [
                {
                    "identifier": "Local:Spare",
                    "symbol_default": True,
                    "symbol_filter_match": False,
                }
            ],
        )
        self.assertEqual(
            before,
            {
                p.relative_to(self.project): p.read_bytes()
                for p in self.project.rglob("*")
                if p.is_file()
            },
        )

    def test_cli_is_compact_by_default_and_details_are_available(self) -> None:
        compact_output = io.StringIO()
        with contextlib.redirect_stdout(compact_output):
            self.assertEqual(audit.main(["--project", str(self.project)]), 0)
        compact = json.loads(compact_output.getvalue())
        self.assertEqual(compact["footprints_not_used_by_live_design_count"], 1)
        self.assertNotIn("footprints_not_used_by_live_design", compact)
        detailed_output = io.StringIO()
        with contextlib.redirect_stdout(detailed_output):
            self.assertEqual(
                audit.main(["--project", str(self.project), "--details"]), 0
            )
        detailed = json.loads(detailed_output.getvalue())
        self.assertEqual(
            detailed["footprints_not_used_by_live_design"][0]["identifier"],
            "Local:Spare",
        )
        self.assertEqual(detailed["status"], compact["status"])

    def test_table_based_relocation_keeps_native_identifiers(self) -> None:
        board = self.project / "v4.1.kicad_pcb"
        schematic = self.project / "v4.1.kicad_sch"
        originals = (board.read_bytes(), schematic.read_bytes())
        libs = self.project / "libraries"
        (libs / "footprints").mkdir()
        moves = [
            (libs / "Local.pretty", libs / "footprints/Local.pretty"),
            (libs / "Local.kicad_sym", libs / "symbols/Local.kicad_sym"),
        ]
        for source, target in moves:
            self.assertTrue(source.resolve().is_relative_to(self.project))
            self.assertTrue(target.resolve().is_relative_to(self.project))
            source.rename(target)
        (self.project / "sym-lib-table").write_text(
            '(sym_lib_table (lib (name "LocalSym") (uri "${KIPRJMOD}/libraries/symbols/Local.kicad_sym")))',
            encoding="utf-8",
        )
        (self.project / "fp-lib-table").write_text(
            '(fp_lib_table (lib (name "Local") (uri "${KIPRJMOD}/libraries/footprints/Local.pretty")))',
            encoding="utf-8",
        )
        result = audit.audit(self.project)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(originals, (board.read_bytes(), schematic.read_bytes()))

    def test_reports_missing_local_model_and_footprint(self) -> None:
        (self.project / "libraries/models/part.step").unlink()
        (self.project / "libraries/Local.pretty/Part.kicad_mod").unlink()
        result = audit.audit(self.project)
        self.assertEqual(result["status"], "missing_or_invalid_local_resources")
        self.assertIn("Missing local footprint: Local:Part", result["errors"])
        self.assertIn(
            "Missing local 3D model: libraries/models/part.step", result["errors"]
        )

    def test_distinguishes_symbol_filters_from_defaults(self) -> None:
        (self.project / "libraries/Local.kicad_sym").write_text(
            '(kicad_symbol_lib (symbol "Part" (property "ki_fp_filters" "Spare")))',
            encoding="utf-8",
        )
        result = audit.audit(self.project)
        self.assertEqual(
            result["footprints_not_used_by_live_design"],
            [
                {
                    "identifier": "Local:Spare",
                    "symbol_default": False,
                    "symbol_filter_match": True,
                }
            ],
        )

    def test_reports_origin_of_missing_model_in_dormant_footprint(self) -> None:
        (self.project / "v4.1.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
        (self.project / "libraries/models/part.step").unlink()
        result = audit.audit(self.project)
        self.assertEqual(
            result["missing_local_3d_models"],
            [
                {
                    "path": "libraries/models/part.step",
                    "referenced_by": ["libraries/Local.pretty/Part.kicad_mod"],
                }
            ],
        )

    def test_reports_missing_symbol_without_treating_external_as_missing(self) -> None:
        (self.project / "libraries/Local.kicad_sym").write_text(
            '(kicad_symbol_lib (symbol "Other"))', encoding="utf-8"
        )
        result = audit.audit(self.project)
        self.assertIn("Missing local symbol: LocalSym:Part", result["errors"])
        self.assertNotIn("Missing local symbol: power:GND", result["errors"])

    def test_rejects_project_path_escape(self) -> None:
        (self.project / "fp-lib-table").write_text(
            '(fp_lib_table (lib (name "Local") (uri "${KIPRJMOD}/../outside.pretty")))',
            encoding="utf-8",
        )
        result = audit.audit(self.project)
        self.assertTrue(
            any("escapes the project" in error for error in result["errors"])
        )

    def test_single_entry_point_registers_read_only_library_check(self) -> None:
        import argparse
        from core import launcher

        spec = launcher.ACTIONS["check"]["libraries"]
        self.assertEqual(spec.module, "verification.audit_kicad_libraries")
        self.assertEqual(spec.mode, "project")
        args = argparse.Namespace(
            project=self.project,
            kicad_python=Path("unused"),
            group="check",
            action="libraries",
        )
        with self.assertRaisesRegex(ValueError, "read-only"):
            launcher.command_for(args, ["--apply"])

    def test_rejects_changes_during_audit(self) -> None:
        original = audit._fingerprints
        count = 0

        def unstable(project: Path) -> dict[str, str]:
            nonlocal count
            count += 1
            hashes = original(project)
            if count == 2:
                hashes["v4.1.kicad_pcb"] = "changed"
            return hashes

        with patch.object(audit, "_fingerprints", unstable):
            with self.assertRaisesRegex(RuntimeError, "changed during"):
                audit.audit(self.project)


if __name__ == "__main__":
    unittest.main()
