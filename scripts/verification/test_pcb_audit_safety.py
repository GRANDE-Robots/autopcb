"""test_pcb_audit_safety.py: tests of PCB audit safety.

PCB audits must preserve design files and reject stale geometry.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import pcbnew


class PcbAuditSafetyTests(unittest.TestCase):
    """Tests that the PCB audits never change design files and refuse stale inputs."""

    def board(self, folder: Path) -> Path:
        """Save an empty board ``v4.1.kicad_pcb`` in a folder that is created if needed.

        Outputs:
            Path of the saved board.
        """
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "v4.1.kicad_pcb"
        pcbnew.SaveBoard(str(path), pcbnew.BOARD())
        return path

    def manufacturing_fixture(self, root: Path):
        """Build a project folder and a manufacturing folder for the board audit.

        Inputs:
            root: Folder that receives ``project`` and ``manufacturing``.
        Outputs:
            The project folder, the board with one silkscreen text and the manufacturing
            folder with empty BOM, placement and via schedule tables and an empty
            archive.
        """
        project = root / "project"
        board = self.board(project)
        model = pcbnew.LoadBoard(str(board))
        text = pcbnew.PCB_TEXT(model)
        text.SetText("test")
        text.SetLayer(pcbnew.F_SilkS)
        model.Add(text)
        pcbnew.SaveBoard(str(board), model)
        (project / "v4.1.kicad_pro").write_text(
            json.dumps(
                {
                    "net_settings": {"classes": []},
                    "board": {
                        "design_settings": {"drc_exclusions": [], "rule_severities": {}}
                    },
                }
            ),
            encoding="utf-8",
        )
        manufacturing = root / "manufacturing"
        (manufacturing / "assembly").mkdir(parents=True)
        (manufacturing / "02_BOM_COMPLETE.csv").write_text(
            "Designator\n", encoding="utf-8"
        )
        (manufacturing / "03_CPL_COMPLETE.csv").write_text(
            "Designator\n", encoding="utf-8"
        )
        (manufacturing / "assembly/EXPLICIT_FILLED_CAPPED_VIAS.csv").write_text(
            "UUID\n", encoding="utf-8"
        )
        with zipfile.ZipFile(manufacturing / "01_PCB_GERBERS_DRILLS.zip", "w"):
            pass
        return project, board, manufacturing

    def run_module(self, module, *arguments):
        """Run a Python module in a subprocess with the given arguments.

        Outputs:
            The completed process with its text output captured.
        """
        return subprocess.run(
            [sys.executable, "-B", "-m", module, *map(str, arguments)],
            text=True,
            capture_output=True,
        )

    def test_routing_metrics_report_length_joints_and_vias_without_writes(self):
        with tempfile.TemporaryDirectory(prefix="jessie-audit-test-") as directory:
            board_path = self.board(Path(directory) / "project")
            board = pcbnew.LoadBoard(str(board_path))
            for start, end in (
                ((0, 0), (1_000_000, 0)),
                ((1_000_000, 0), (2_000_000, 0)),
            ):
                track = pcbnew.PCB_TRACK(board)
                track.SetStart(pcbnew.VECTOR2I(*start))
                track.SetEnd(pcbnew.VECTOR2I(*end))
                track.SetLayer(pcbnew.F_Cu)
                track.SetWidth(250_000)
                board.Add(track)
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(pcbnew.VECTOR2I(5_000_000, 5_000_000))
            via.SetWidth(600_000)
            via.SetDrill(300_000)
            board.Add(via)
            pcbnew.SaveBoard(str(board_path), board)
            original = board_path.read_bytes()
            result = self.run_module(
                "verification.audit_pcb_routing_style", board_path, "--json"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["metrics"]["track_segments"], 2)
            self.assertAlmostEqual(report["metrics"]["track_length_mm"], 2.0)
            self.assertEqual(report["metrics"]["vias"], 1)
            self.assertEqual(len(report["collinear_merge_candidates"]), 1)
            self.assertEqual(board_path.read_bytes(), original)
            self.assertEqual(list(board_path.parent.glob("*.json")), [])

    def test_routing_does_not_treat_arc_chords_as_straight_segments(self):
        with tempfile.TemporaryDirectory(prefix="jessie-audit-test-") as directory:
            board_path = self.board(Path(directory) / "project")
            board = pcbnew.LoadBoard(str(board_path))
            arc = pcbnew.PCB_ARC(board)
            arc.SetStart(pcbnew.VECTOR2I(0, 0))
            arc.SetMid(pcbnew.VECTOR2I(1_000_000, 1_000_000))
            arc.SetEnd(pcbnew.VECTOR2I(3_000_000, 2_000_000))
            arc.SetWidth(250_000)
            arc.SetLayer(pcbnew.F_Cu)
            board.Add(arc)
            pcbnew.SaveBoard(str(board_path), board)
            result = self.run_module(
                "verification.audit_pcb_routing_style", board_path, "--json"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["metrics"]["arc_segments"], 1)
            self.assertEqual(report["metrics"]["straight_segments"], 0)
            self.assertGreater(report["metrics"]["track_length_mm"], 3.6)
            self.assertEqual(report["non_45_segments"], [])

    def test_routing_report_cannot_overwrite_the_board(self):
        with tempfile.TemporaryDirectory(prefix="jessie-audit-test-") as directory:
            board = self.board(Path(directory) / "project")
            original = board.read_bytes()
            result = self.run_module(
                "verification.audit_pcb_routing_style", board, "--out", board
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(board.read_bytes(), original)

    def test_board_audit_binds_geometry_to_its_saved_source(self):
        with tempfile.TemporaryDirectory(prefix="jessie-audit-test-") as directory:
            root = Path(directory)
            project, board, manufacturing = self.manufacturing_fixture(root)
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
            geometry = (output / "pcb_geometry_detail.json").read_bytes()
            self.assertEqual(
                report["source_sha256"][board.name],
                hashlib.sha256(board.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                report.get("geometry_sha256"), hashlib.sha256(geometry).hexdigest()
            )
            self.assertTrue(report["source_unchanged"])

    def test_board_audit_geometry_survives_editor_preferences_change(self):
        from verification.audit_pcb_paths import load_current_geometry

        with tempfile.TemporaryDirectory(prefix="jessie-audit-test-") as directory:
            root = Path(directory)
            project, _board, manufacturing = self.manufacturing_fixture(root)
            preferences = project / "v4.1.kicad_prl"
            preferences.write_text("{}", encoding="utf-8")
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
            preferences.write_text('{"editor_only": true}', encoding="utf-8")
            review, geometry = load_current_geometry(project, output)
            self.assertTrue(review["source_unchanged"])
            self.assertEqual(geometry["tracks"], [])

    def test_path_audit_consumes_fresh_geometry_and_binds_its_result(self):
        from verification import audit_pcb_paths

        with tempfile.TemporaryDirectory(prefix="jessie-audit-test-") as directory:
            root = Path(directory)
            project, board_path, manufacturing = self.manufacturing_fixture(root)
            board = pcbnew.LoadBoard(str(board_path))
            net = pcbnew.NETINFO_ITEM(board, "SIGNAL")
            board.Add(net)
            for reference, x in (("J1", 1_000_000), ("U1", 3_000_000)):
                footprint = pcbnew.FOOTPRINT(board)
                footprint.SetReference(reference)
                footprint.SetPosition(pcbnew.VECTOR2I(x, 0))
                pad = pcbnew.PAD(footprint)
                pad.SetNumber("1")
                pad.SetSize(pcbnew.VECTOR2I(500_000, 500_000))
                pad.SetPosition(pcbnew.VECTOR2I(x, 0))
                pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
                pad.SetLayerSet(pcbnew.LSET.FrontMask())
                pad.SetNet(net)
                footprint.Add(pad)
                board.Add(footprint)
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(1_000_000, 0))
            track.SetEnd(pcbnew.VECTOR2I(3_000_000, 0))
            track.SetWidth(250_000)
            track.SetLayer(pcbnew.F_Cu)
            track.SetNet(net)
            board.Add(track)
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
            args = [
                "audit_pcb_paths",
                "--project",
                str(project),
                "--manufacturing",
                str(manufacturing),
                "--out",
                str(output),
            ]
            target = [("SIGNAL", ("J1", "1"), ("U1", "1"))]
            with (
                mock.patch.object(sys, "argv", args),
                mock.patch.object(audit_pcb_paths, "path_specs", return_value=target),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                audit_pcb_paths.main()
            report = json.loads((output / "pcb_paths.json").read_text(encoding="utf-8"))
            self.assertEqual(len(report["paths"]), 1)
            self.assertAlmostEqual(
                report["paths"][0]["approx_centerline_length_mm"], 2.0
            )
            self.assertEqual(
                report.get("geometry_sha256"),
                hashlib.sha256(
                    (output / "pcb_geometry_detail.json").read_bytes()
                ).hexdigest(),
            )

    def test_board_audit_cannot_write_reports_into_project(self):
        with tempfile.TemporaryDirectory(prefix="jessie-audit-test-") as directory:
            root = Path(directory)
            project, board, manufacturing = self.manufacturing_fixture(root)
            original = board.read_bytes()
            result = self.run_module(
                "verification.audit_pcb_manufacturing",
                "--project",
                project,
                "--manufacturing",
                manufacturing,
                "--out",
                project,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((project / "pcb_native_review.json").exists())
            self.assertEqual(board.read_bytes(), original)

    def test_path_audit_rejects_stale_saved_board_identity(self):
        with tempfile.TemporaryDirectory(prefix="jessie-audit-test-") as directory:
            root = Path(directory)
            board = self.board(root / "project")
            output = root / "audit"
            output.mkdir()
            geometry = json.dumps({"tracks": [], "vias": [], "pads": []}).encode()
            (output / "pcb_geometry_detail.json").write_bytes(geometry)
            (output / "pcb_native_review.json").write_text(
                json.dumps(
                    {
                        "source_sha256": {board.name: "stale"},
                        "source_unchanged": True,
                        "geometry_sha256": hashlib.sha256(geometry).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            original = board.read_bytes()
            result = self.run_module(
                "verification.audit_pcb_paths",
                "--project",
                board.parent,
                "--manufacturing",
                root / "manufacturing",
                "--out",
                output,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("stale", (result.stdout + result.stderr).lower())
            self.assertFalse((output / "pcb_paths.json").exists())
            self.assertEqual(board.read_bytes(), original)

    def test_path_audit_rejects_altered_geometry(self):
        with tempfile.TemporaryDirectory(prefix="jessie-audit-test-") as directory:
            root = Path(directory)
            board = self.board(root / "project")
            output = root / "audit"
            output.mkdir()
            (output / "pcb_geometry_detail.json").write_text(
                json.dumps({"tracks": [], "vias": [], "pads": []}), encoding="utf-8"
            )
            (output / "pcb_native_review.json").write_text(
                json.dumps(
                    {
                        "source_sha256": {
                            board.name: hashlib.sha256(board.read_bytes()).hexdigest()
                        },
                        "source_unchanged": True,
                        "geometry_sha256": "other",
                    }
                ),
                encoding="utf-8",
            )
            result = self.run_module(
                "verification.audit_pcb_paths",
                "--project",
                board.parent,
                "--manufacturing",
                root / "manufacturing",
                "--out",
                output,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("geometry", (result.stdout + result.stderr).lower())
            self.assertFalse((output / "pcb_paths.json").exists())


if __name__ == "__main__":
    unittest.main()
