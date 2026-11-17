"""Verify compact, read-only lookup of KiCad S-expression objects."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verification import inspect_kicad_object as inspect


class ObjectInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="jessie-object-test-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.assertTrue(
            self.project.is_relative_to(Path(tempfile.gettempdir()).resolve())
        )
        value = json.dumps(
            'quotation " (segment) \\ and a closing )', ensure_ascii=False
        )
        self.board = self.project / "v4.1.kicad_pcb"
        self.board.write_text(
            '(kicad_pcb (footprint "L:P" (property "Reference" "J9") '
            f'(property "Value" {value}) (at 1 2 90) (uuid "fp-1") '
            '(pad "1" smd circle (at 0 0) (uuid "pad-1"))) '
            '(footprint "L:P" (property "Reference" "J10") (at 3 4) (uuid "fp-2")) '
            '(segment (start 1 2) (end 2 3) (layer "B.Cu") (net "GND") (uuid "seg-1")))',
            encoding="utf-8",
        )
        self.schematic = self.project / "v4.1.kicad_sch"
        self.schematic.write_text(
            '(kicad_sch (lib_symbols (symbol "Embedded" (uuid "embedded"))) '
            '(symbol (lib_id "L:Part") (property "Reference" "Uµ1") (uuid "sch-1")))',
            encoding="utf-8",
        )

    def test_finds_exact_reference_without_exposing_full_expression(self) -> None:
        before = self.board.read_bytes()
        result = inspect.inspect(self.project, "pcb", reference="J9")
        self.assertEqual(result["matches"], 1)
        self.assertEqual(result["objects"][0]["reference"], "J9")
        self.assertEqual(result["objects"][0]["at"], ["1", "2", "90"])
        self.assertNotIn("expression", result["objects"][0])
        self.assertEqual(before, self.board.read_bytes())

    def test_batch_references_and_paginate_with_missing_items(self) -> None:
        result = inspect.inspect(
            self.project, "pcb", references=["J9", "J10", "J99"], limit=1, offset=1
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            (result["matches"], result["returned"], result["offset"]), (2, 1, 1)
        )
        self.assertEqual(result["objects"][0]["reference"], "J10")
        self.assertEqual(result["missing_references"], ["J99"])

    def test_cli_batch_reports_partial_failure(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = inspect.main(
                [
                    "--project",
                    str(self.project),
                    "--reference",
                    "J9",
                    "--reference",
                    "J99",
                ]
            )
        result = json.loads(out.getvalue())
        self.assertEqual((code, result["status"]), (1, "partial"))
        self.assertEqual(result["missing_references"], ["J99"])

    def test_rejects_invalid_batch_or_raw_offset(self) -> None:
        with self.assertRaisesRegex(ValueError, "not both"):
            inspect.inspect(self.project, "pcb", reference="J9", references=["J10"])
        with self.assertRaisesRegex(ValueError, "Offset"):
            inspect.inspect(self.project, "pcb", offset=-1)
        with self.assertRaisesRegex(ValueError, "cannot use an offset"):
            inspect.inspect(self.project, "pcb", reference="J9", raw=True, offset=1)

    def test_raw_returns_exact_single_expression(self) -> None:
        item = inspect.inspect(self.project, "pcb", reference="J9", raw=True)[
            "objects"
        ][0]
        self.assertTrue(item["expression"].startswith('(footprint "L:P"'))
        self.assertIn('(uuid "pad-1")', item["expression"])
        self.assertEqual(inspect.parse(item["expression"]).name, "footprint")

    def test_uuid_can_select_nested_pad(self) -> None:
        item = inspect.inspect(self.project, "pcb", "pad", uuid="pad-1", raw=True)[
            "objects"
        ][0]
        self.assertEqual(item["identifier"], "1")
        self.assertEqual(item["uuid"], "pad-1")

    def test_expression_names_inside_escaped_strings_do_not_count(self) -> None:
        self.assertEqual(inspect.inspect(self.project, "pcb", "segment")["matches"], 1)

    def test_library_symbol_by_identifier_and_drawing_sheet(self) -> None:
        libs = self.project / "libraries"
        libs.mkdir()
        (libs / "parts.kicad_sym").write_text(
            '(kicad_symbol_lib (symbol "PartA" (property "Reference" "U")) '
            '(symbol "PartB" (property "Reference" "U")))',
            encoding="utf-8",
        )
        sheet = self.project / "drawing.kicad_wks"
        sheet.write_text("(kicad_wks (line (start 1 2) (end 3 4)))", encoding="utf-8")
        result = inspect.inspect(
            self.project, "libraries/parts.kicad_sym", identifier="PartB", raw=True
        )
        self.assertEqual(result["objects"][0]["identifier"], "PartB")
        self.assertEqual(
            inspect.inspect(self.project, "drawing.kicad_wks")["matches"], 1
        )

    def test_top_level_excludes_cached_schematic_symbols(self) -> None:
        result = inspect.inspect(self.project, "schematic", reference="Uµ1")
        self.assertEqual(result["matches"], 1)
        self.assertEqual(result["objects"][0]["lib_id"], "L:Part")
        self.assertEqual(inspect.inspect(self.project, "schematic")["matches"], 1)
        self.assertEqual(
            inspect.inspect(self.project, "schematic", nested=True)["matches"], 2
        )

    def test_compact_limit_does_not_parse_every_segment(self) -> None:
        segments = " ".join(
            f'(segment (start {n} 0) (end {n} 1) (uuid "id-{n}"))' for n in range(600)
        )
        self.board.write_text(f"(kicad_pcb {segments})", encoding="utf-8")
        with patch.object(inspect, "parse", wraps=inspect.parse) as parser:
            result = inspect.inspect(self.project, "pcb", "segment", limit=2)
        self.assertEqual((result["matches"], result["returned"]), (600, 2))
        self.assertEqual(parser.call_count, 2)

    def test_library_and_table_files_are_supported(self) -> None:
        libs = self.project / "libraries"
        libs.mkdir()
        (libs / "example.kicad_mod").write_text(
            '(footprint "P" (pad "3" smd circle (uuid "pad-x")))', encoding="utf-8"
        )
        (self.project / "fp-lib-table").write_text(
            '(fp_lib_table (lib (name "X") (uri "${KIPRJMOD}/libraries/X.pretty")))',
            encoding="utf-8",
        )
        self.assertEqual(
            inspect.inspect(self.project, "libraries/example.kicad_mod", uuid="pad-x")[
                "matches"
            ],
            1,
        )
        self.assertEqual(inspect.inspect(self.project, "fp-lib-table")["matches"], 1)
        self.assertEqual(
            inspect.inspect(self.project, "fp-lib-table", "root", raw=True)["matches"],
            1,
        )

    def test_rejects_ambiguous_raw_and_invalid_arguments(self) -> None:
        self.board.write_text(
            '(kicad_pcb (footprint "P" (property "Reference" "J1")) '
            '(footprint "P" (property "Reference" "J1")))',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            inspect.inspect(self.project, "pcb", reference="J1", raw=True)
        with self.assertRaisesRegex(ValueError, "Raw output"):
            inspect.inspect(self.project, "pcb", raw=True)
        with self.assertRaisesRegex(ValueError, "outside the project"):
            inspect.inspect(self.project, "../other.kicad_pcb")
        with self.assertRaisesRegex(ValueError, "Limit"):
            inspect.inspect(self.project, "pcb", limit=0)

    def test_rejects_invalid_root_and_unbalanced_sources(self) -> None:
        self.board.write_text('(wrong (footprint "P"))', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "root"):
            inspect.inspect(self.project, "pcb")
        self.board.write_text('(kicad_pcb (footprint "P")', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unclosed"):
            inspect.inspect(self.project, "pcb")

    def test_changed_source_is_not_reported_as_current(self) -> None:
        with patch.object(inspect, "_hash_file", return_value="changed"):
            with self.assertRaisesRegex(RuntimeError, "changed during"):
                inspect.inspect(self.project, "pcb")

    def test_cli_emits_compact_json_and_not_found_status(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = inspect.main(
                ["--project", str(self.project), "--reference", "missing"]
            )
        result = json.loads(out.getvalue())
        self.assertEqual((code, result["status"]), (1, "not_found"))
        self.assertNotIn("\n ", out.getvalue())


if __name__ == "__main__":
    unittest.main()
