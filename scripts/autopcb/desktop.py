"""Local desktop surface for the same jobs used by the AutoPCB CLI."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

from PySide6.QtCore import QProcess, QProcessEnvironment, QPointF, QRectF, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QGraphicsScene, QGraphicsView,
    QHBoxLayout, QInputDialog, QLabel, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)


from autopcb.workspace import Workspace


class BoardView(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setBackgroundBrush(QColor("#131c21"))
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.highlight = None

    def show_board(self, board: dict) -> None:
        scene = self.scene()
        scene.clear()
        self.highlight = None
        if not board:
            return
        (x0, y0), (x1, y1) = board["bounds"]
        if not (x1 > x0 and y1 > y0):
            scene.setSceneRect(QRectF())
            return
        scene.addRect(QRectF(x0, y0, x1-x0, y1-y0), QPen(QColor("#7c9a8c"), .10), QColor("#26352f"))
        for track in board["tracks"]:
            (x, y), (end_x, end_y) = track["start"], track["end"]
            width = max(track.get("width_mm", .2), .1)
            if track["via"]:
                item = scene.addEllipse(x-width/2, y-width/2, width, width, QPen(Qt.PenStyle.NoPen),
                                        QColor("#ffc782"))
            else:
                color = {"F.Cu": "#ef9c72", "B.Cu": "#65a2c3"}.get(track["layer"], "#bb9daa")
                pen = QPen(QColor(color), width)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                item = scene.addLine(x, y, end_x, end_y, pen)
            item.setToolTip(f"{track['net']} · {track['layer']}")
        for fp in board["footprints"]:
            for pad in fp["pads"]:
                x, y = pad["at"]
                w, h = pad["size"]
                item = scene.addRect(x-w/2, y-h/2, w, h, QPen(Qt.PenStyle.NoPen), QColor("#ddd392"))
                item.setToolTip(f"{fp['ref']}.{pad['number']} · {pad['net']}")
            text = scene.addText(fp["ref"], QFont("Inter", 9))
            text.setDefaultTextColor(QColor("#ecf1e9"))
            text.setScale(.12)
            text.setPos(*fp["at"])
        region = QRectF(x0, y0, x1-x0, y1-y0).adjusted(-3, -3, 3, 3)
        scene.setSceneRect(region)
        self.fitInView(region, Qt.AspectRatioMode.KeepAspectRatio)

    def locate(self, x: float, y: float) -> None:
        if self.highlight:
            self.scene().removeItem(self.highlight)
        self.highlight = self.scene().addEllipse(x-.9, y-.9, 1.8, 1.8, QPen(QColor("#fff4bc"), .2))
        self.highlight.setZValue(100)
        self.centerOn(QPointF(x, y))

    def wheelEvent(self, event):
        zoom = 1.15 if event.angleDelta().y() > 0 else 1/1.15
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.scale(zoom, zoom)
        event.accept()


def _panel(title: str, parent: QLayoutOwner) -> tuple[QWidget, QVBoxLayout]:
    widget = QWidget(parent)
    layout = QVBoxLayout(widget)
    label = QLabel(title)
    label.setProperty("section", True)
    layout.addWidget(label)
    return widget, layout


QLayoutOwner = QWidget | None


class Desktop(QMainWindow):
    def __init__(self, project: Path, cli: Path | None, python: Path | None, *, workspace: Path | None = None):
        super().__init__()
        self.setWindowTitle("AutoPCB")
        self.resize(1280, 820)
        self.temp = tempfile.TemporaryDirectory(prefix="autopcb-desktop-")
        self.location = project.resolve()
        self.workspace = Workspace(workspace or (self.location if self.location.is_dir() else self.location.parent))
        self.cli, self.python = cli, python
        self.run_id = None
        self.proposal_file = None
        self.proposal_id = None
        self.schematic_draft = None
        self.action = ""
        self.callbacks = {}
        self.process = QProcess(self)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.process_failed)
        self.process.readyReadStandardError.connect(self.engine_message)
        self.cancelled = False
        root = QWidget()
        self.setCentralWidget(root)
        structure = QVBoxLayout(root)
        structure.setContentsMargins(24, 20, 24, 15)
        structure.setSpacing(14)
        header = QHBoxLayout()
        logo = QLabel("AutoPCB")
        logo.setObjectName("title")
        header.addWidget(logo)
        self.project_label = QLabel("Select a KiCad project or start from requirements")
        self.project_label.setObjectName("projectLabel")
        header.addWidget(self.project_label, 1)
        self.open_button = self.button("Open KiCad project", self.open_project, header)
        self.cancel_button = self.button("Cancel job", self.cancel, header)
        self.cancel_button.setEnabled(False)
        structure.addLayout(header)
        self.tabs = QTabWidget()
        structure.addWidget(self.tabs, 1)
        self.status = QLabel("Ready")
        self.status.setWordWrap(True)
        structure.addWidget(self.status)
        self._requirements()
        self._design()
        self._layout()
        self._verify()
        self._export()
        self.menuBar().addMenu("Tools").addAction("Export toolchain diagnostics", self.diagnostics)
        if self.location.is_file():
            self.select_project(self.location)

    def button(self, text, handler, layout):
        button = QPushButton(text)
        button.clicked.connect(handler)
        layout.addWidget(button)
        return button

    def _requirements(self):
        page, box = _panel("What should the board do?", self)
        self.requirements = QPlainTextEdit()
        self.requirements.setPlaceholderText("Describe the circuit, supply voltage, layer count, and board size.")
        if self.workspace.requirements.is_file():
            try:
                self.requirements.setPlainText(self.workspace.requirements.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError) as error:
                self.status.setText(str(error))
        box.addWidget(self.requirements, 1)
        buttons = QHBoxLayout()
        self.button("Save requirements", self.save_requirements, buttons)
        self.button("Propose design", self.propose, buttons)
        box.addLayout(buttons)
        self.tabs.addTab(page, "Requirements")

    def _design(self):
        page, box = _panel("Proposed parts and connections", self)
        self.proposal = QPlainTextEdit(readOnly=True)
        box.addWidget(self.proposal, 1)
        self.draft_button = self.button("Open schematic draft in KiCad", self.open_draft, box)
        self.draft_button.setEnabled(False)
        self.create_button = self.button("Approve and create KiCad project", self.create, box)
        self.create_button.setEnabled(False)
        self.tabs.addTab(page, "Design")

    def _layout(self):
        page, box = _panel("Board comparison", self)
        bar = QHBoxLayout()
        self.button("Load saved board", lambda: self.start("inspect", self.show_saved), bar)
        self.button("Create routing policy", lambda: self.start("configure-layout"), bar)
        self.move_parts = QCheckBox("Allow eligible placement edits")
        bar.addWidget(self.move_parts)
        self.button("Preview routing", self.preview_route, bar)
        self.resume = self.button("Resume preview", self.resume_preview, bar)
        self.apply_button = self.button("Apply preview", self.apply_preview, bar)
        self.resume.setEnabled(False)
        self.apply_button.setEnabled(False)
        box.addLayout(bar)
        split = QSplitter()
        original, a = _panel("Saved", self)
        candidate, b = _panel("Preview", self)
        self.before, self.after = BoardView(), BoardView()
        a.addWidget(self.before, 1)
        b.addWidget(self.after, 1)
        split.addWidget(original)
        split.addWidget(candidate)
        box.addWidget(split, 1)
        self.tabs.addTab(page, "Layout")

    def _verify(self):
        page, box = _panel("Native checks", self)
        self.button("Run independent verification", lambda: self.start("verify", self.findings_ready), box)
        self.findings = QTreeWidget()
        self.findings.setHeaderLabels(("Check", "Finding", "Location"))
        self.findings.itemActivated.connect(self.finding_selected)
        box.addWidget(self.findings, 1)
        self.tabs.addTab(page, "Verify")

    def _export(self):
        page, box = _panel("Fabrication and assembly package", self)
        self.button("Export draft package", lambda: self.export(True), box)
        self.button("Export checked package", lambda: self.export(False), box)
        self.exports = QPlainTextEdit(readOnly=True)
        box.addWidget(self.exports, 1)
        self.tabs.addTab(page, "Export")

    def start(self, action, callback=None, *options):
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.status.setText("The current job is still running.")
            return False
        self.action = action
        self.cancelled = False
        self.callbacks[action] = callback
        self.status.setText("Running " + action.replace("-", " ") + "…")
        self.cancel_button.setEnabled(action in ("doctor", "propose", "inspect", "route", "compare", "verify"))
        self.open_button.setEnabled(False)
        arguments = ["--workspace", str(self.workspace.root), "--project", str(self.location)]
        if self.cli:
            arguments.extend(("--kicad-cli", str(self.cli)))
        if self.python:
            arguments.extend(("--kicad-python", str(self.python)))
        runtime = QProcessEnvironment.systemEnvironment()
        runtime.insert("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
        runtime.insert("PYTHONDONTWRITEBYTECODE", "1")
        self.process.setProcessEnvironment(runtime)
        invocation = [] if getattr(sys, "frozen", False) else ["-B", "-m", "autopcb"]
        self.process.start(sys.executable, [*invocation, *arguments, action, *map(str, options)])
        return True

    def process_failed(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.status.setText(self.process.errorString())
            self.cancel_button.setEnabled(False)
            self.open_button.setEnabled(True)

    def engine_message(self):
        detail = bytes(self.process.readAllStandardError()).decode("utf-8", "replace").strip()
        if detail:
            self.status.setText(detail.splitlines()[-1][:280])

    def cancel(self):
        pid = self.process.processId()
        self.cancelled = True
        if pid and sys.platform == "win32":
            subprocess.Popen(["taskkill", "/PID", str(pid), "/T", "/F"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            self.process.terminate()

    def finished(self, code, _exit_status):
        raw = bytes(self.process.readAllStandardOutput()).decode("utf-8", "replace")
        error = bytes(self.process.readAllStandardError()).decode("utf-8", "replace").strip()
        self.cancel_button.setEnabled(False)
        self.open_button.setEnabled(True)
        if self.cancelled:
            self.status.setText("Job cancelled. Saved design left intact.")
            return
        try:
            result = json.loads(raw)
        except ValueError:
            self.status.setText(error or f"Job stopped with exit code {code}.")
            return
        summary = result.get("error") or result.get("status", "Result available").replace("_", " ").capitalize()
        self.status.setText(summary)
        callback = self.callbacks.get(self.action)
        if callback and (not code or "native" in result or "unresolved" in result or "run_id" in result):
            callback(result)

    def open_project(self):
        target, _ = QFileDialog.getOpenFileName(self, "Open a KiCad project, board, or schematic",
                                               str(self.location), "KiCad files (*.kicad_pro *.kicad_pcb *.kicad_sch)")
        if target:
            self.select_project(Path(target))

    def select_project(self, target: Path):
        self.location = target.resolve()
        self.project_label.setText(self.location.name)
        self.run_id = None
        self.apply_button.setEnabled(False)
        self.resume.setEnabled(False)
        self.findings.clear()
        self.after.show_board({})
        self.tabs.setCurrentIndex(2)
        self.start("inspect", self.show_saved)

    def show_saved(self, result):
        self.before.show_board(result["geometry"])

    def save_requirements(self):
        target = self.workspace.requirements
        if target.exists():
            choice = QMessageBox.question(self, "Replace saved requirements?", str(target))
            if choice != QMessageBox.StandardButton.Yes:
                return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.requirements.toPlainText().strip() + "\n", encoding="utf-8")
        except OSError as error:
            self.status.setText(str(error))
            return
        self.status.setText("Saved to data/requirements.txt.")

    def propose(self):
        base = Path(self.temp.name)
        identifier = uuid4().hex
        text = base / f"{identifier}.txt"
        proposal = base / f"{identifier}.json"
        text.write_text(self.requirements.toPlainText(), encoding="utf-8")
        draft = base / f"{identifier}.kicad_sch"
        if self.start("propose", self.proposal_ready, text, "--output", proposal, "--schematic-draft", draft):
            self.proposal_file = proposal
            self.schematic_draft = draft
            self.create_button.setEnabled(False)
            self.draft_button.setEnabled(False)

    def open_draft(self):
        if self.schematic_draft and self.schematic_draft.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.schematic_draft)))

    def proposal_ready(self, result):
        parts = [f"{ref}  {item['manufacturer']} {item['mpn']}  ({item['value']})\n"
                 f"    {', '.join(str(key) + ' → ' + (value or 'NC') for key, value in item['nets'].items())}\n"
                 f"    {item['source']}"
                 for ref, item in result.get("components", {}).items()]
        self.proposal.setPlainText("\n\n".join([result.get("description", "Requirements review"),
                                    *parts, *result.get("assumptions", []), *result.get("unresolved", [])]))
        self.proposal_id = result.get("proposal_id")
        self.create_button.setEnabled(result["status"] == "proposed")
        self.draft_button.setEnabled(result["status"] == "proposed" and self.schematic_draft.is_file())
        self.tabs.setCurrentIndex(1)

    def create(self):
        name, accepted = QInputDialog.getText(self, "Name the new KiCad project", "Project name")
        if accepted and name and self.proposal_file is not None and self.proposal_id:
            self.start("create", lambda result: self.select_project(Path(result["project"])),
                       self.proposal_file, "--approve", self.proposal_id, "--name", name)

    def preview_route(self):
        options = ("--allow-placement",) if self.move_parts.isChecked() else ()
        self.start("route", self.route_ready, *options)

    def resume_preview(self):
        if self.run_id:
            options = ("--allow-placement",) if self.move_parts.isChecked() else ()
            self.start("route", self.route_ready, "--resume", self.run_id, *options)

    def route_ready(self, result):
        self.run_id = result["run_id"]
        before, after = result.get("saved_board_opens"), result.get("best_preview_opens")
        self.resume.setEnabled(True)
        self.apply_button.setEnabled(False)
        if isinstance(before, int) and isinstance(after, int) and after < before:
            self.start("compare", self.comparison_ready, self.run_id)
        else:
            details = result.get("blocked_nets") or result.get("failure") or result["status"]
            self.status.setText(f"{after} preview opens. {details}")

    def comparison_ready(self, result):
        self.before.show_board(result["before"])
        self.after.show_board(result["after"])
        self.apply_button.setEnabled(True)
        self.status.setText("Preview ready for review. Apply saves the checked changes to KiCad.")

    def apply_preview(self):
        if not self.run_id:
            return
        choice = QMessageBox.question(self, "Apply routing preview", "Save the independently checked routing improvement?")
        if choice == QMessageBox.StandardButton.Yes:
            self.start("apply", lambda _: self.select_project(self.location), self.run_id)

    def findings_ready(self, result):
        self.findings.clear()
        rows = list(result["native"]["findings"])
        if result.get("electrical_review_blocker"):
            rows.append({"check": "electrical", "description": result["electrical_review_blocker"],
                         "positions_mm": []})
        for row in rows:
            locations = row["positions_mm"]
            first = locations[0] if locations else None
            text = f"{first['x']:.3f}, {first['y']:.3f} mm" if first else ""
            item = QTreeWidgetItem((row["check"], row["description"], text))
            if first and row["check"] != "erc":
                item.setData(0, Qt.ItemDataRole.UserRole, (first["x"], first["y"]))
            self.findings.addTopLevelItem(item)
        self.findings.resizeColumnToContents(0)

    def finding_selected(self, item, _column):
        point = item.data(0, Qt.ItemDataRole.UserRole)
        if point is not None:
            self.tabs.setCurrentIndex(2)
            self.before.locate(*point)

    def export(self, draft):
        options = ("--draft",) if draft else ()
        self.start("export", lambda value: self.exports.setPlainText(json.dumps(value, indent=2)), *options)

    def diagnostics(self):
        target, _ = QFileDialog.getSaveFileName(self, "Export toolchain diagnostics", "autopcb-toolchain.json",
                                                "JSON (*.json)")
        if target:
            self.start("doctor", lambda _: self.status.setText("Toolchain diagnostics saved."), "--output", target)

    def closeEvent(self, event):
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.status.setText("Wait for the active job or cancel it before closing.")
            event.ignore()
            return
        self.temp.cleanup()
        super().closeEvent(event)


def main(project: Path, cli: Path | None, python: Path | None, *, workspace: Path | None = None) -> int:
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Inter", 10))
    app.setStyleSheet("""
        QMainWindow, QWidget { background: #eef1eb; color: #152621; }
        QLabel#title { font-size: 28px; font-weight: 750; padding-right: 16px; }
        QLabel#projectLabel { color: #46635a; }
        QLabel[section="true"] { font-size: 15px; font-weight: 660; padding: 6px 0; }
        QPushButton { background: #f8faf6; border: 1px solid #aac0b4; padding: 7px 12px; border-radius: 5px; }
        QPushButton:hover { background: #dcece2; }
        QPushButton:disabled { color: #74867d; background: #e9eeea; }
        QTabBar::tab { background: #e1eae4; padding: 10px 18px; color: #204639; }
        QTabBar::tab:selected { background: #285b50; color: #f9fffb; }
        QPlainTextEdit, QTreeWidget { background: #fbfdf9; padding: 8px; border: 1px solid #c3d2c6; }
        QTabWidget::pane { border: 1px solid #c3d2c6; }
        QGraphicsView { border: 1px solid #32483d; }
    """)
    window = Desktop(project, cli, python, workspace=workspace)
    window.show()
    return app.exec()
