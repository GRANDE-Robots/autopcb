"""Drive the desktop and real native jobs against a private reference project."""

import json
from pathlib import Path
import tempfile
from time import monotonic
import unittest
from unittest.mock import patch

from autopcb.native import Native
from autopcb.qualification import REFERENCE_PROJECTS, ExportArtifacts
from core.project import SourceSnapshot


class DesktopWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PySide6 import QtCore, QtTest, QtWidgets
        except ImportError as error:
            raise unittest.SkipTest(
                "Install AutoPCB's desktop dependencies to run this test"
            ) from error
        try:
            cls.native = Native.discover()
            cls.native.footprints()
        except (OSError, RuntimeError, ValueError) as error:
            raise unittest.SkipTest(
                f"Matched native toolchain unavailable: {error}"
            ) from error
        cls.qt, cls.qtest, cls.widgets = QtCore, QtTest.QTest, QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
            ["autopcb-qa", "-platform", "offscreen"]
        )

    def _wait_for_job(self, window):
        deadline = monotonic() + 450
        while window.process.state() != self.qt.QProcess.ProcessState.NotRunning:
            if monotonic() > deadline:
                self.fail(f"{window.action} did not finish: {window.status.text()}")
            self.qtest.qWait(20)

    def _click(self, window, label):
        buttons = window.tabs.currentWidget().findChildren(self.widgets.QPushButton)
        matches = [button for button in buttons if button.text() == label]
        self.assertEqual(len(matches), 1, label)
        self.assertTrue(matches[0].isEnabled(), window.status.text())
        self.qtest.mouseClick(matches[0], self.qt.Qt.MouseButton.LeftButton)
        self._wait_for_job(window)

    def test_cutout_arc_and_rotated_plated_slot(self):
        from autopcb.desktop import BoardView

        source = """(kicad_pcb (version 20240108) (generator pcbnew)
        (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user)
                (37 "F.SilkS" user) (44 "Edge.Cuts" user))
        (net 0 "") (net 1 "S")
        (footprint "Local:Test" (layer "F.Cu") (at 10 10)
          (property "Reference" "J1" (at 0 -3 0) (layer "F.SilkS"))
          (pad "1" thru_hole oval (at 0 0 90) (size 4 2) (drill oval 2.4 1)
               (layers "*.Cu" "*.Mask") (net 1 "S")))
        (gr_rect (start 2 2) (end 24 20) (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts"))
        (gr_rect (start 4 4) (end 6 6) (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts"))
        (arc (start 12 10) (mid 14 8) (end 16 10) (width 0.4) (layer "F.Cu") (net 1))
        (via (at 16 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1)))"""
        with tempfile.TemporaryDirectory(prefix="autopcb-layout-") as folder:
            board, report = (
                Path(folder) / "geometry.kicad_pcb",
                Path(folder) / "geometry.json",
            )
            board.write_text(source, encoding="utf-8")
            self.native.worker("inspect", board, report, output=report)
            geometry = json.loads(report.read_text(encoding="utf-8"))
        view = BoardView()
        view.show_board(geometry)
        point = self.qt.QPointF
        items = view.scene().items()
        outline = next(item for item in items if item.zValue() == 0).path()
        self.assertTrue(outline.contains(point(3, 3)))
        self.assertFalse(outline.contains(point(5, 5)))
        arc = next(
            item
            for item in items
            if item.toolTip() == "S · F.Cu" and item.zValue() == 3
        ).path()
        self.assertTrue(arc.contains(point(14, 8)))
        self.assertFalse(arc.contains(point(14, 10)))
        slot = next(
            item
            for item in items
            if item.toolTip() == "J1.1 · S" and item.zValue() == 6
        )
        self.assertEqual(slot.line().x1(), slot.line().x2())
        self.assertAlmostEqual(abs(slot.line().y2() - slot.line().y1()), 1.4)
        self.assertAlmostEqual(slot.pen().widthF(), 1.0)
        via_drill = next(
            item
            for item in items
            if item.toolTip() == "S · F.Cu" and item.zValue() == 6
        )
        self.assertAlmostEqual(via_drill.rect().width(), 0.30)

    def test_requirements_to_verified_draft_package(self):
        from autopcb.desktop import Desktop

        _, requirements, layers = next(
            case for case in REFERENCE_PROJECTS if case[0] == "USB Breakout"
        )
        name = "USB Desktop QA"
        with tempfile.TemporaryDirectory(prefix="autopcb-desktop-qa-") as folder:
            workspace = Path(folder)
            window = Desktop(workspace, self.native.cli, self.native.python)
            window.show()
            try:
                window.requirements.setPlainText(requirements)
                self._click(window, "Save requirements")
                self.assertEqual(
                    window.workspace.requirements.read_text(encoding="utf-8").strip(),
                    requirements,
                )
                self._click(window, "Propose design")
                self.assertTrue(window.create_button.isEnabled(), window.status.text())
                with patch.object(
                    self.widgets.QInputDialog, "getText", return_value=(name, True)
                ):
                    self._click(window, "Approve and create KiCad project")
                project = window.workspace.project(window.location)
                storage = window.workspace.storage(project.entry)
                self.assertEqual(
                    project.entry,
                    workspace / "results" / "projects" / name / (name + ".kicad_pro"),
                )
                self.assertTrue((storage.config / "autopcb.json").is_file())
                original = SourceSnapshot.capture(project)
                self.assertGreater(
                    len(window.before.scene().items()), 0, window.status.text()
                )

                self._click(window, "Preview routing")
                self.assertTrue(window.apply_button.isEnabled(), window.status.text())
                self.assertFalse(window.resume.isEnabled())
                state_file = storage.results / "autoroute" / (window.run_id + ".json")
                state = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertEqual(state["best_preview_opens"], 0)
                self.assertEqual(state["status"], "preview_zero_opens")
                self.assertFalse(state["publication_enabled"])
                self.assertGreater(
                    len(window.after.scene().items()),
                    len(window.before.scene().items()),
                )
                original.verify_unchanged()

                answers = self.widgets.QMessageBox.StandardButton
                with patch.object(
                    self.widgets.QMessageBox,
                    "question",
                    side_effect=(answers.No, answers.Yes),
                ):
                    self._click(window, "Apply preview")
                    original.verify_unchanged()
                    self._click(window, "Apply preview")
                saved = SourceSnapshot.capture(project)
                self.assertEqual(original.hashes.keys(), saved.hashes.keys())
                self.assertEqual(
                    {
                        name
                        for name in saved.hashes
                        if saved.hashes[name] != original.hashes[name]
                    },
                    {project.board.name},
                )

                window.tabs.setCurrentIndex(3)
                self._click(window, "Run independent verification")
                report = json.loads(
                    (storage.results / "verification.json").read_text(encoding="utf-8")
                )
                self.assertTrue(
                    report["native"]["passed"], report["native"]["findings"]
                )
                self.assertEqual(report["native"]["open_connections"], 0)
                self.assertEqual(
                    report["blockers"],
                    ["A source-bound electrical review report is required."],
                )

                for finding in report["reference_screen"]["findings"]:
                    (span,) = finding["gap_spans"]
                    (pin,) = span["endpoint_pads"]
                    name = f"{pin['reference']}.{pin['pad']}"
                    rows = window.findings.findItems(
                        name, self.qt.Qt.MatchFlag.MatchContains, 1
                    )
                    self.assertEqual(len(rows), 1, name)
                    window.findings.itemActivated.emit(rows[0], 1)
                    self.assertEqual(window.tabs.currentIndex(), 2)
                    marker = window.before.highlight.rect().center()
                    self.assertAlmostEqual(marker.x(), span["position_mm"]["x"])
                    self.assertAlmostEqual(marker.y(), span["position_mm"]["y"])

                window.tabs.setCurrentIndex(4)
                self._click(window, "Export draft package")
                manifest = json.loads(window.exports.toPlainText())
                draft = window.workspace.export_output(project, draft=True)
                ExportArtifacts.check(draft, manifest)
                self.assertEqual(manifest["inputs"], saved.hashes)
                self.assertEqual(manifest["status"], "draft")
                self.assertFalse(manifest["production_approved"])
                gerbers = [
                    path
                    for path in manifest["artifacts"]
                    if path.startswith("gerbers/") and not path.endswith(".gbrjob")
                ]
                self.assertEqual(len(gerbers), layers + 7)
                saved.verify_unchanged()
            finally:
                if window.process.state() != self.qt.QProcess.ProcessState.NotRunning:
                    window.cancel()
                    window.process.waitForFinished(10_000)
                window.close()
