"""test_verification_generalized.py: check the reusable PCB verification tools.

Regression checks for reusable, accurate PCB verification tools.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import inspect
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pcbnew

from verification import (
    audit_kicad_libraries,
    audit_pcb_paths,
    audit_schematic_blocks,
    inspect_kicad_object,
    inspect_pcb_copper,
    test_audit_kicad_libraries,
    test_pcb_audit_safety,
)


class GeneralizedVerificationTests(unittest.TestCase):
    """Tests for the reusable PCB audit and inspection tools."""

    def run_module(self, module, *arguments):
        """Run a module as a command with this interpreter and capture its output.

        Inputs:
            module: dotted module name.
            arguments: command line arguments, each converted to text.
        Outputs:
            The completed process with text stdout and stderr.
        """
        return subprocess.run(
            [sys.executable, "-B", "-m", module, *map(str, arguments)],
            capture_output=True,
            text=True,
        )

    def test_default_ethernet_paths_match_current_connector_nets(self):
        paths = audit_pcb_paths.path_specs()
        ethernet = [net for net, first, _ in paths if first[0] in {"J22", "J24"}]
        self.assertCountEqual(
            ethernet,
            [
                "L1_ETH_TX_P",
                "L1_ETH_TX_N",
                "L1_ETH_RX_P",
                "L1_ETH_RX_N",
                "L2_ETH_TX_P",
                "L2_ETH_TX_N",
                "L2_ETH_RX_P",
                "L2_ETH_RX_N",
            ],
        )

    def test_object_aliases_discover_other_unique_board_names(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "example.kicad_pcb").write_text(
                '(kicad_pcb (footprint "Part" (property "Reference" "J7")))',
                encoding="utf-8",
            )
            result = inspect_kicad_object.inspect(project, "pcb", reference="J7")
            self.assertEqual(result["objects"][0]["reference"], "J7")
            (project / "extra.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "(?i)more than one|multiple"):
                inspect_kicad_object.inspect(project, "pcb")
            self.assertEqual(
                inspect_kicad_object.inspect(
                    project, "example.kicad_pcb", reference="J7"
                )["matches"],
                1,
            )

    def test_library_audit_uses_selected_project_files_and_tracks_local_dependencies(
        self,
    ):
        fixture = test_audit_kicad_libraries.LibraryAuditTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        project = fixture.project
        (project / "v4.1.kicad_pcb").rename(project / "example.kicad_pcb")
        (project / "v4.1.kicad_sch").rename(project / "example.kicad_sch")
        custom = project / "custom"
        custom.mkdir()
        (project / "libraries/Local.kicad_sym").rename(custom / "Local.kicad_sym")
        (project / "sym-lib-table").write_text(
            '(sym_lib_table (lib (name "LocalSym") (uri "${KIPRJMOD}/custom/Local.kicad_sym")))',
            encoding="utf-8",
        )
        result = audit_kicad_libraries.audit(project)
        self.assertEqual(result["status"], "ok")
        self.assertIn("example.kicad_pcb", result["source_sha256"])
        self.assertIn(
            "custom/Local.kicad_sym", audit_kicad_libraries._fingerprints(project)
        )
        (project / "other.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "(?i)more than one|multiple"):
            audit_kicad_libraries.audit(project)
        selected = audit_kicad_libraries.audit(project, board="example.kicad_pcb")
        self.assertEqual(selected["status"], "ok")

    def test_schematic_block_audit_checks_pin_direction_including_mirroring(self):
        source = """(kicad_sch
          (lib_symbols (symbol "Custom:Part" (symbol "Part_1_1"
            (pin passive line (at -5 1 0) (number "1"))
            (pin passive line (at -5 -1 180) (number "2")))))
          (symbol
            (lib_id "Custom:Part") (at 0 0 0) (unit 1) (property "Reference" "U1")))"""
        finding = audit_schematic_blocks.audit(source)[0]
        self.assertNotEqual(finding["status"], "one-sided ascending")
        self.assertFalse(finding["pins_left_facing"])
        mirrored = source.replace("-5 1 0", "-5 1 180").replace(
            '(lib_id "Custom:Part") (at 0 0 0)',
            '(lib_id "Custom:Part") (at 0 0 0) (mirror y)',
        )
        finding = audit_schematic_blocks.audit(mirrored)[0]
        self.assertTrue(finding["pins_left_facing"])
        self.assertEqual(finding["status"], "one-sided ascending")

    def test_schematic_block_audit_includes_pins_common_to_all_units(self):
        source = """(kicad_sch
          (lib_symbols (symbol "Custom:Part"
            (symbol "Part_0_1" (pin passive line (at -5 1 0) (number "1")))
            (symbol "Part_1_1" (pin passive line (at -5 -1 0) (number "2")))))
          (symbol
            (lib_id "Custom:Part") (at 0 0 0) (unit 1) (property "Reference" "U1")))"""
        finding = audit_schematic_blocks.audit(source)[0]
        self.assertEqual(finding["pins"], 2)
        self.assertEqual(finding["status"], "one-sided ascending")

    def test_custom_path_specs_are_loaded_and_validated(self):
        self.assertTrue(hasattr(audit_pcb_paths, "load_path_specs"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paths.json"
            path.write_text(
                json.dumps(
                    {
                        "paths": [
                            {"net": "SENSE", "from": ["J7", "1"], "to": ["U3", "2"]}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            specs = audit_pcb_paths.load_path_specs(path)
            self.assertEqual(specs, [("SENSE", ("J7", "1"), ("U3", "2"))])
            audit_pcb_paths.validate_path_specs(
                specs, {("J7", "1"): "SENSE", ("U3", "2"): "SENSE"}
            )

    def test_pad_inspection_uses_real_copper_shape(self):
        board = pcbnew.BOARD()
        footprint = pcbnew.FOOTPRINT(board)
        board.Add(footprint)
        pad = pcbnew.PAD(footprint)
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.VECTOR2I(1_000_000, 1_000_000))
        pad.SetPosition(pcbnew.VECTOR2I(0, 0))
        footprint.Add(pad)
        self.assertTrue(inspect_pcb_copper.contains_pad(pad, 0, 0))
        self.assertFalse(inspect_pcb_copper.contains_pad(pad, 0.49, 0.49))

    def test_arc_inspection_follows_the_arc_not_the_chord(self):
        board = pcbnew.BOARD()
        arc = pcbnew.PCB_ARC(board)
        arc.SetStart(pcbnew.VECTOR2I(0, 0))
        arc.SetMid(pcbnew.VECTOR2I(1_000_000, 1_000_000))
        arc.SetEnd(pcbnew.VECTOR2I(2_000_000, 0))
        arc.SetWidth(200_000)
        arc.SetLayer(pcbnew.F_Cu)
        board.Add(arc)
        self.assertEqual(inspect_pcb_copper.nearby_tracks(board, 1, 0, 0), [])
        self.assertEqual(
            inspect_pcb_copper.nearby_tracks(board, 1, 1, 0)[0]["kind"], "arc"
        )

    def test_copper_inspection_can_filter_by_layer(self):
        self.assertIn("layer", inspect.signature(inspect_pcb_copper.inspect).parameters)
        board = pcbnew.BOARD()
        for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(0, 0))
            track.SetEnd(pcbnew.VECTOR2I(1_000_000, 0))
            track.SetWidth(200_000)
            track.SetLayer(layer)
            board.Add(track)
        data = inspect_pcb_copper.inspect(board, "0.5,0", 0, layer="F.Cu")
        self.assertEqual([item["layer"] for item in data["copper"]], ["F.Cu"])

    def test_copper_inspection_excludes_non_copper_holes(self):
        board = pcbnew.BOARD()
        footprint = pcbnew.FOOTPRINT(board)
        board.Add(footprint)
        hole = pcbnew.PAD(footprint)
        hole.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
        hole.SetSize(pcbnew.VECTOR2I(1_000_000, 1_000_000))
        hole.SetDrillSize(pcbnew.VECTOR2I(1_000_000, 1_000_000))
        hole.SetLayerSet(pcbnew.LSET())
        footprint.Add(hole)
        self.assertEqual(inspect_pcb_copper.inspect(board, "0,0", 0)["pads"], [])

    def test_path_specs_reject_reverse_duplicates(self):
        first = ("SIGNAL", ("J1", "1"), ("U1", "2"))
        reverse = ("SIGNAL", ("U1", "2"), ("J1", "1"))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            audit_pcb_paths.validate_path_specs(
                [first, reverse], {("J1", "1"): "SIGNAL", ("U1", "2"): "SIGNAL"}
            )

    def test_single_pad_nets_are_reported_only_when_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.kicad_pcb"
            board = pcbnew.BOARD()
            net = pcbnew.NETINFO_ITEM(board, "SINGLE")
            board.Add(net)
            footprint = pcbnew.FOOTPRINT(board)
            footprint.SetReference("J1")
            board.Add(footprint)
            pad = pcbnew.PAD(footprint)
            pad.SetNumber("1")
            pad.SetSize(pcbnew.VECTOR2I(500_000, 500_000))
            pad.SetLayerSet(pcbnew.LSET.FrontMask())
            pad.SetNet(net)
            footprint.Add(pad)
            pcbnew.SaveBoard(str(path), board)
            result = self.run_module(
                "verification.audit_pcb_net_islands", path, "--include-single-pad"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["split_net_count"], 0)
            self.assertEqual(
                [item["net"] for item in report["single_pad_nets"]], ["SINGLE"]
            )

    def test_path_audit_does_not_measure_an_arc_as_a_chord(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = test_pcb_audit_safety.PcbAuditSafetyTests()
            project, board_path, manufacturing = fixture.manufacturing_fixture(root)
            board = pcbnew.LoadBoard(str(board_path))
            net = pcbnew.NETINFO_ITEM(board, "CURVED")
            board.Add(net)
            for reference, x in (("J1", 0), ("U1", 2_000_000)):
                footprint = pcbnew.FOOTPRINT(board)
                footprint.SetReference(reference)
                board.Add(footprint)
                pad = pcbnew.PAD(footprint)
                pad.SetNumber("1")
                pad.SetSize(pcbnew.VECTOR2I(500_000, 500_000))
                pad.SetPosition(pcbnew.VECTOR2I(x, 0))
                pad.SetLayerSet(pcbnew.LSET.FrontMask())
                pad.SetNet(net)
                footprint.Add(pad)
            arc = pcbnew.PCB_ARC(board)
            arc.SetStart(pcbnew.VECTOR2I(0, 0))
            arc.SetMid(pcbnew.VECTOR2I(1_000_000, 1_000_000))
            arc.SetEnd(pcbnew.VECTOR2I(2_000_000, 0))
            arc.SetWidth(200_000)
            arc.SetLayer(pcbnew.F_Cu)
            arc.SetNet(net)
            board.Add(arc)
            pcbnew.SaveBoard(str(board_path), board)
            output = root / "audit"
            result = self.run_module(
                "verification.audit_pcb_manufacturing",
                "--project",
                project,
                "--manufacturing",
                manufacturing,
                "--out",
                output,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            config = root / "paths.json"
            config.write_text(
                json.dumps(
                    {
                        "paths": [
                            {"net": "CURVED", "from": ["J1", "1"], "to": ["U1", "1"]}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = self.run_module(
                "verification.audit_pcb_paths",
                "--project",
                project,
                "--manufacturing",
                manufacturing,
                "--out",
                output,
                "--paths-file",
                config,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((output / "pcb_paths.json").read_text(encoding="utf-8"))
            path = report["paths"][0]
            self.assertIsNone(path["not_found"])
            self.assertEqual(path["status"], "unsupported")
            self.assertIn("arc", path["reason"].lower())
            self.assertNotIn("approx_centerline_length_mm", path)

    def test_courtyard_exceptions_apply_only_to_explicit_pairs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.kicad_pcb"
            board = pcbnew.BOARD()
            for reference in ("U3", "U9", "J16"):
                footprint = pcbnew.FOOTPRINT(board)
                footprint.SetReference(reference)
                pad = pcbnew.PAD(footprint)
                pad.SetNumber("1")
                pad.SetSize(pcbnew.VECTOR2I(1_000_000, 1_000_000))
                pad.SetPosition(pcbnew.VECTOR2I(0, 0))
                pad.SetLayerSet(pcbnew.LSET.FrontMask())
                footprint.Add(pad)
                board.Add(footprint)
            pcbnew.SaveBoard(str(path), board)
            result = self.run_module("verification.audit_pcb_courtyards", path)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["summary"]["same_side_conflicts"], 3)
            result = self.run_module(
                "verification.audit_pcb_courtyards", path, "--allow-pair", "U3:J16"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["summary"]["same_side_conflicts"], 2)
            self.assertEqual(report["summary"]["intentional_same_side_colocations"], 1)

    def test_power_audit_accepts_an_explicit_contract_for_another_board(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            board = pcbnew.BOARD()
            footprint = pcbnew.FOOTPRINT(board)
            footprint.SetReference("J99")
            board.Add(footprint)
            for index, name in enumerate(("SUPPLY", "RETURN"), start=1):
                net = pcbnew.NETINFO_ITEM(board, name)
                board.Add(net)
                pad = pcbnew.PAD(footprint)
                pad.SetNumber(str(index))
                pad.SetSize(pcbnew.VECTOR2I(500_000, 500_000))
                pad.SetPosition(pcbnew.VECTOR2I(index * 1_000_000, 0))
                pad.SetLayerSet(pcbnew.LSET.FrontMask())
                pad.SetNet(net)
                footprint.Add(pad)
            board_path = root / "different.kicad_pcb"
            pcbnew.SaveBoard(str(board_path), board)
            original = board_path.read_bytes()
            contract = root / "contract.json"
            contract.write_text(
                json.dumps(
                    {
                        "terminals": {
                            "SUPPLY": [["J99", "1"]],
                            "RETURN": [["J99", "2"]],
                        },
                        "separations": [
                            {
                                "name": "supply_return",
                                "first": ["J99", "1"],
                                "second": ["J99", "2"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = self.run_module(
                "verification.audit_pcb_power_terminals",
                board_path,
                "--contract",
                contract,
                "--json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["static_contract_passed"])
            self.assertTrue(report["domain_separation"]["supply_return"])
            self.assertIsNone(report["native_drc_open_edges_by_power_domain"])
            self.assertEqual(
                report["source_sha256"], hashlib.sha256(original).hexdigest()
            )
            self.assertEqual(board_path.read_bytes(), original)

    def test_manufacturing_audit_accepts_other_board_names_and_reports_all_nets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = test_pcb_audit_safety.PcbAuditSafetyTests()
            project, old_board, manufacturing = fixture.manufacturing_fixture(root)
            board_path = project / "alternate.kicad_pcb"
            old_board.rename(board_path)
            (project / "v4.1.kicad_pro").rename(project / "alternate.kicad_pro")
            board = pcbnew.LoadBoard(str(board_path))
            net = pcbnew.NETINFO_ITEM(board, "CUSTOM_SUPPLY")
            board.Add(net)
            footprint = pcbnew.FOOTPRINT(board)
            footprint.SetReference("J99")
            pad = pcbnew.PAD(footprint)
            pad.SetNumber("1")
            pad.SetSize(pcbnew.VECTOR2I(500_000, 500_000))
            pad.SetLayerSet(pcbnew.LSET.FrontMask())
            pad.SetNet(net)
            footprint.Add(pad)
            board.Add(footprint)
            pcbnew.SaveBoard(str(board_path), board)
            original = board_path.read_bytes()
            output = root / "audit"
            result = self.run_module(
                "verification.audit_pcb_manufacturing",
                "--project",
                project,
                "--manufacturing",
                manufacturing,
                "--out",
                output,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((output / "pcb_native_review.json").read_text())
            self.assertIn("alternate.kicad_pcb", report["source_sha256"])
            self.assertIn("CUSTOM_SUPPLY", report["key_nets"])
            self.assertEqual(board_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
