"""desktop.py: local desktop surface for the jobs that the AutoPCB command runs.

Every job runs as a child process of ``python -m autopcb`` (or the frozen
executable) and the window shows its JSON report. The window never edits a design
itself. Board geometry is drawn in mm.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

from PySide6.QtCore import QProcess, QProcessEnvironment, QPointF, QRectF, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QGraphicsScene,
    QGraphicsView,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


from autopcb.workspace import Workspace


class BoardView(QGraphicsView):
    """A zoomable drawing of one board's outline, tracks, vias, pads and front silkscreen."""

    @staticmethod
    def contour_path(rings: list) -> QPainterPath:
        """A painter path from polygon rings.

        Inputs:
            rings: Lists of [x, y] points in mm. Every non-empty ring is closed.
        """
        path = QPainterPath()
        for ring in rings:
            if ring:
                path.moveTo(*ring[0])
                for point in ring[1:]:
                    path.lineTo(*point)
                path.closeSubpath()
        return path

    @staticmethod
    def round_stroke(scene, start, end, width: float, color: QColor):
        """Draw a round-capped line, or a dot when its ends coincide.

        Inputs:
            scene: Scene to draw into.
            start: Start (x, y) in mm.
            end: End (x, y) in mm.
            width: Line width in mm.
            color: Line colour.
        Outputs:
            The scene item.
        """
        x, y = start
        if start == end:
            return scene.addEllipse(
                x - width / 2,
                y - width / 2,
                width,
                width,
                QPen(Qt.PenStyle.NoPen),
                color,
            )
        pen = QPen(color, width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        return scene.addLine(x, y, *end, pen)

    def __init__(self):
        """Create an empty, antialiased view that scrolls by dragging."""
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setBackgroundBrush(QColor("#131c21"))
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.highlight = None

    def show_board(self, board: dict) -> None:
        """Draw a board and fit it in the view.

        Inputs:
            board: Geometry from the inspect job, in mm. An empty record, or one without
                positive bounds, clears the view.
        """
        scene = self.scene()
        scene.clear()
        self.highlight = None
        if not board:
            scene.setSceneRect(QRectF())
            return
        (x0, y0), (x1, y1) = board["bounds"]
        if not (x1 > x0 and y1 > y0):
            scene.setSceneRect(QRectF())
            return
        edge, surface = QPen(QColor("#7c9a8c"), 0.10), QColor("#15372f")
        no_pen = QPen(Qt.PenStyle.NoPen)
        hole_color = QColor("#131c21")
        if "outline" in board:
            scene.addPath(BoardView.contour_path(board["outline"]), edge, surface)
        else:
            scene.addRect(QRectF(x0, y0, x1 - x0, y1 - y0), edge, surface)
        for track in board["tracks"]:
            layer = track["layer"]
            copper = {"F.Cu": "#f08a68", "B.Cu": "#67c5df"}.get(layer, "#b7a1d8")
            color = QColor("#e9c674" if track["via"] else copper)
            if "contours" in track:
                item = scene.addPath(
                    BoardView.contour_path(track["contours"]), no_pen, color
                )
            else:
                item = BoardView.round_stroke(
                    scene,
                    track["start"],
                    track["end"],
                    track.get("width_mm", 0.2),
                    color,
                )
            item.setZValue(4 if track["via"] else {"F.Cu": 3, "B.Cu": 1}.get(layer, 2))
            item.setToolTip(f"{track['net']} · {layer}")
            if track.get("drill_mm", 0) > 0:
                hole = BoardView.round_stroke(
                    scene, track["start"], track["start"], track["drill_mm"], hole_color
                )
                hole.setZValue(6)
                hole.setToolTip(item.toolTip())
        for fp in board["footprints"]:
            for rings in fp.get("front_silk", ()):
                scene.addPath(
                    BoardView.contour_path(rings), no_pen, QColor("#d9e9d7")
                ).setZValue(4)
            for pad in fp["pads"]:
                if "contours" in pad:
                    item = scene.addPath(
                        BoardView.contour_path(pad["contours"]),
                        no_pen,
                        QColor("#e9c674"),
                    )
                else:
                    x, y = pad["at"]
                    w, h = pad["size"]
                    item = scene.addRect(
                        x - w / 2, y - h / 2, w, h, no_pen, QColor("#e9c674")
                    )
                item.setZValue(5)
                item.setToolTip(f"{fp['ref']}.{pad['number']} · {pad['net']}")
                if pad.get("hole"):
                    drill = pad["hole"]
                    hole = BoardView.round_stroke(
                        scene,
                        drill["start"],
                        drill["end"],
                        drill["width_mm"],
                        hole_color,
                    )
                    hole.setZValue(6)
                    hole.setToolTip(item.toolTip())
            text = scene.addText(fp["ref"], QFont("Inter", 9))
            text.setDefaultTextColor(QColor("#ecf1e9"))
            text.setScale(0.12)
            label_at = fp.get("label_at", fp["at"])
            text.setPos(
                QPointF(*label_at) - text.boundingRect().center() * text.scale()
            )
            text.setZValue(7)
        region = QRectF(x0, y0, x1 - x0, y1 - y0).adjusted(-3, -3, 3, 3)
        scene.setSceneRect(region)
        self.fitInView(region, Qt.AspectRatioMode.KeepAspectRatio)

    def locate(self, x: float, y: float) -> None:
        """Mark a point and centre it, first zooming to a 14 mm square when the view is larger.

        Inputs:
            x: Horizontal position in mm.
            y: Vertical position in mm.
        """
        if self.highlight:
            self.scene().removeItem(self.highlight)
        self.highlight = self.scene().addEllipse(
            x - 0.9, y - 0.9, 1.8, 1.8, QPen(QColor("#fff4bc"), 0.2)
        )
        self.highlight.setZValue(100)
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        if min(visible.width(), visible.height()) > 14:
            self.fitInView(
                QRectF(x - 7, y - 7, 14, 14), Qt.AspectRatioMode.KeepAspectRatio
            )
        self.centerOn(QPointF(x, y))

    def wheelEvent(self, event):
        """Zoom by 15 percent per wheel step about the cursor."""
        zoom = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.scale(zoom, zoom)
        event.accept()


class Desktop(QMainWindow):
    """The main window: tabs that run AutoPCB jobs and show their reports."""

    def __init__(
        self,
        project: Path,
        cli: Path | None,
        python: Path | None,
        *,
        workspace: Path | None = None,
    ):
        """Build the window and select the project when a file was given.

        Inputs:
            project: KiCad file or folder to open.
            cli: KiCad CLI to pass to jobs, or None.
            python: KiCad Python to pass to jobs, or None.
            workspace: Workspace folder; the project's folder when None.
        """
        super().__init__()
        self.setWindowTitle("AutoPCB")
        self.resize(1280, 820)
        self.temp = tempfile.TemporaryDirectory(prefix="autopcb-desktop-")
        self.location = project.resolve()
        self.workspace = Workspace(
            workspace
            or (self.location if self.location.is_dir() else self.location.parent)
        )
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
        self.menuBar().addMenu("Tools").addAction(
            "Export toolchain diagnostics", self.diagnostics
        )
        if self.location.is_file():
            self.select_project(self.location)

    @staticmethod
    def _panel(title: str, parent: QWidget | None) -> tuple[QWidget, QVBoxLayout]:
        """A titled panel and its vertical layout.

        Inputs:
            title: Section title.
            parent: Parent widget.
        """
        widget = QWidget(parent)
        layout = QVBoxLayout(widget)
        label = QLabel(title)
        label.setProperty("section", True)
        layout.addWidget(label)
        return widget, layout

    def button(self, text, handler, layout):
        """Add a push button to a layout.

        Inputs:
            text: Button label.
            handler: Called when the button is clicked.
            layout: Layout that receives the button.
        Outputs:
            The button.
        """
        button = QPushButton(text)
        button.clicked.connect(handler)
        layout.addWidget(button)
        return button

    def _requirements(self):
        """Build the Requirements tab: a text editor, Save and Propose buttons."""
        page, box = self._panel("What should the board do?", self)
        self.requirements = QPlainTextEdit()
        self.requirements.setPlaceholderText(
            "Describe the circuit, supply voltage and layer count. Give exact board dimensions if required."
        )
        if self.workspace.requirements.is_file():
            try:
                self.requirements.setPlainText(
                    self.workspace.requirements.read_text(encoding="utf-8-sig")
                )
            except (OSError, UnicodeError) as error:
                self.status.setText(str(error))
        box.addWidget(self.requirements, 1)
        buttons = QHBoxLayout()
        self.button("Save requirements", self.save_requirements, buttons)
        self.button("Propose design", self.propose, buttons)
        box.addLayout(buttons)
        self.tabs.addTab(page, "Requirements")

    def _design(self):
        """Build the Design tab: the proposal text and the draft and create buttons."""
        page, box = self._panel("Proposed parts and connections", self)
        self.proposal = QPlainTextEdit(readOnly=True)
        box.addWidget(self.proposal, 1)
        self.draft_button = self.button(
            "Open schematic draft in KiCad", self.open_draft, box
        )
        self.draft_button.setEnabled(False)
        self.create_button = self.button(
            "Approve and create KiCad project", self.create, box
        )
        self.create_button.setEnabled(False)
        self.tabs.addTab(page, "Design")

    def _layout(self):
        """Build the Layout tab: preview controls above the saved and preview boards."""
        page, box = self._panel("Board comparison", self)
        bar = QHBoxLayout()
        self.button(
            "Load saved board", lambda: self.start("inspect", self.show_saved), bar
        )
        self.button(
            "Create routing policy", lambda: self.start("configure-layout"), bar
        )
        self.move_parts = QCheckBox("Allow eligible placement edits")
        bar.addWidget(self.move_parts)
        self.button("Preview routing", self.preview_route, bar)
        self.resume = self.button("Resume preview", self.resume_preview, bar)
        self.apply_button = self.button("Apply preview", self.apply_preview, bar)
        self.resume.setEnabled(False)
        self.apply_button.setEnabled(False)
        box.addLayout(bar)
        split = QSplitter()
        original, a = self._panel("Saved", self)
        candidate, b = self._panel("Preview", self)
        self.before, self.after = BoardView(), BoardView()
        a.addWidget(self.before, 1)
        b.addWidget(self.after, 1)
        split.addWidget(original)
        split.addWidget(candidate)
        box.addWidget(split, 1)
        box.addWidget(
            QLabel(
                '<span style="color:#f08a68">●</span> Front tracks &nbsp; '
                '<span style="color:#67c5df">●</span> Back tracks &nbsp; '
                '<span style="color:#b7a1d8">●</span> Inner tracks &nbsp; '
                '<span style="color:#c3a05b">●</span> Pads and vias &nbsp; · &nbsp; Copper fills hidden'
            )
        )
        self.tabs.addTab(page, "Layout")

    def _verify(self):
        """Build the Verify tab: the verification button and a tree of findings."""
        page, box = self._panel("Native checks", self)
        self.button(
            "Run independent verification",
            lambda: self.start("verify", self.findings_ready),
            box,
        )
        self.findings = QTreeWidget()
        self.findings.setHeaderLabels(("Check", "Finding", "Location"))
        header = self.findings.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.findings.itemActivated.connect(self.finding_selected)
        box.addWidget(self.findings, 1)
        self.tabs.addTab(page, "Verify")

    def _export(self):
        """Build the Export tab: the package buttons and the manifest text."""
        page, box = self._panel("Fabrication and assembly package", self)
        self.button("Export draft package", lambda: self.export(True), box)
        self.button("Export checked package", lambda: self.export(False), box)
        self.exports = QPlainTextEdit(readOnly=True)
        box.addWidget(self.exports, 1)
        self.tabs.addTab(page, "Export")

    def start(self, action, callback=None, *options):
        """Run one CLI action as a child process.

        Inputs:
            action: ``autopcb`` subcommand to run.
            callback: Called with the JSON report when the job ends with a result.
            options: Arguments placed after the subcommand.
        Outputs:
            True when the job started. False when another job is still running.
        """
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.status.setText("The current job is still running.")
            return False
        self.action = action
        self.cancelled = False
        self.callbacks[action] = callback
        self.status.setText("Running " + action.replace("-", " ") + "…")
        self.cancel_button.setEnabled(
            action in ("doctor", "propose", "inspect", "route", "compare", "verify")
        )
        self.open_button.setEnabled(False)
        arguments = [
            "--workspace",
            str(self.workspace.root),
            "--project",
            str(self.location),
        ]
        if self.cli:
            arguments.extend(("--kicad-cli", str(self.cli)))
        if self.python:
            arguments.extend(("--kicad-python", str(self.python)))
        runtime = QProcessEnvironment.systemEnvironment()
        runtime.insert("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
        runtime.insert("PYTHONDONTWRITEBYTECODE", "1")
        self.process.setProcessEnvironment(runtime)
        invocation = [] if getattr(sys, "frozen", False) else ["-B", "-m", "autopcb"]
        self.process.start(
            sys.executable, [*invocation, *arguments, action, *map(str, options)]
        )
        return True

    def process_failed(self, error):
        """Report a job that could not start and allow opening a project again."""
        if error == QProcess.ProcessError.FailedToStart:
            self.status.setText(self.process.errorString())
            self.cancel_button.setEnabled(False)
            self.open_button.setEnabled(True)

    def engine_message(self):
        """Show the last line of the job's error output, up to 280 characters, as the status."""
        detail = (
            bytes(self.process.readAllStandardError())
            .decode("utf-8", "replace")
            .strip()
        )
        if detail:
            self.status.setText(detail.splitlines()[-1][:280])

    def cancel(self):
        """Stop the running job, with its child processes on Windows; the saved design stays intact."""
        pid = self.process.processId()
        self.cancelled = True
        if pid and sys.platform == "win32":
            subprocess.Popen(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            self.process.terminate()

    def finished(self, code, _exit_status):
        """Read a finished job's report, show its summary and pass it to the callback.

        Inputs:
            code: Exit code of the job.
            _exit_status: Qt exit status; unused.
        """
        raw = bytes(self.process.readAllStandardOutput()).decode("utf-8", "replace")
        error = (
            bytes(self.process.readAllStandardError())
            .decode("utf-8", "replace")
            .strip()
        )
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
        summary = (
            result.get("error")
            or result.get("status", "Result available").replace("_", " ").capitalize()
        )
        self.status.setText(summary)
        callback = self.callbacks.get(self.action)
        if callback and (
            not code
            or "native" in result
            or "unresolved" in result
            or "run_id" in result
        ):
            callback(result)

    def open_project(self):
        """Ask for a KiCad project, board or schematic and select it."""
        target, _ = QFileDialog.getOpenFileName(
            self,
            "Open a KiCad project, board, or schematic",
            str(self.location),
            "KiCad files (*.kicad_pro *.kicad_pcb *.kicad_sch)",
        )
        if target:
            self.select_project(Path(target))

    def select_project(self, target: Path):
        """Select a project, clear the views and load its saved board.

        Inputs:
            target: KiCad file of the project.
        """
        self.location = target.resolve()
        self.project_label.setText(self.location.name)
        self.run_id = None
        self.apply_button.setEnabled(False)
        self.resume.setEnabled(False)
        self.findings.clear()
        self.before.show_board({})
        self.after.show_board({})
        self.tabs.setCurrentIndex(2)
        self.start("inspect", self.show_saved)

    def show_saved(self, result):
        """Show the saved board in the left view.

        Inputs:
            result: Inspect report.
        """
        self.before.show_board(result["geometry"])

    def save_requirements(self):
        """Save the requirements text to the workspace, asking before it replaces a file."""
        target = self.workspace.requirements
        if target.exists():
            choice = QMessageBox.question(
                self, "Replace saved requirements?", str(target)
            )
            if choice != QMessageBox.StandardButton.Yes:
                return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                self.requirements.toPlainText().strip() + "\n", encoding="utf-8"
            )
        except OSError as error:
            self.status.setText(str(error))
            return
        self.status.setText("Saved to data/requirements.txt.")

    def propose(self):
        """Run the propose job on the requirements text.

        The proposal and a schematic draft are written to temporary files.
        """
        base = Path(self.temp.name)
        identifier = uuid4().hex
        text = base / f"{identifier}.txt"
        proposal = base / f"{identifier}.json"
        text.write_text(self.requirements.toPlainText(), encoding="utf-8")
        draft = base / f"{identifier}.kicad_sch"
        if self.start(
            "propose",
            self.proposal_ready,
            text,
            "--output",
            proposal,
            "--schematic-draft",
            draft,
        ):
            self.proposal_file = proposal
            self.schematic_draft = draft
            self.create_button.setEnabled(False)
            self.draft_button.setEnabled(False)

    def open_draft(self):
        """Open the schematic draft in the system's handler for KiCad files."""
        if self.schematic_draft and self.schematic_draft.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.schematic_draft)))

    def proposal_ready(self, result):
        """Show a proposal in the Design tab.

        Inputs:
            result: Proposal report. Creating a project is enabled for a proposed design
                and opening the draft when the draft file exists.
        """
        parts = [
            f"{ref}  {item['manufacturer']} {item['mpn']}  ({item['value']})\n"
            f"    {', '.join(str(key) + ' → ' + (value or 'NC') for key, value in item['nets'].items())}\n"
            f"    {item['source']}"
            for ref, item in result.get("components", {}).items()
        ]
        size = result.get("size_mm")
        outline = []
        if size:
            width, height = size
            if compact := result.get("compact_outline"):
                min_width, min_height = compact["minimum_mm"]
                outline.append(
                    f"Flexible outline. Placement minimizes area between {min_width:g} × {min_height:g} mm "
                    f"and {width:g} × {height:g} mm."
                )
            else:
                outline.append(f"Fixed outline. {width:g} × {height:g} mm.")
        self.proposal.setPlainText(
            "\n\n".join(
                [
                    result.get("description", "Requirements review"),
                    *outline,
                    *parts,
                    *result.get("assumptions", []),
                    *result.get("unresolved", []),
                ]
            )
        )
        self.proposal_id = result.get("proposal_id")
        self.create_button.setEnabled(result["status"] == "proposed")
        self.draft_button.setEnabled(
            result["status"] == "proposed" and self.schematic_draft.is_file()
        )
        self.tabs.setCurrentIndex(1)

    def create(self):
        """Ask for a project name and create the approved proposal as a KiCad project."""
        name, accepted = QInputDialog.getText(
            self, "Name the new KiCad project", "Project name"
        )
        if accepted and name and self.proposal_file is not None and self.proposal_id:
            self.start(
                "create",
                self.created,
                self.proposal_file,
                "--approve",
                self.proposal_id,
                "--name",
                name,
            )

    def created(self, result):
        """Open the new project and show its outline size in mm.

        Inputs:
            result: design_build report.
        """
        self.select_project(Path(result["project"]))
        width, height = result["outline"]["size_mm"]
        self.project_label.setText(
            f"{self.location.name}   ·   {width:g} × {height:g} mm"
        )

    def preview_route(self):
        """Run a routing preview, letting it move parts when that box is ticked."""
        options = ("--allow-placement",) if self.move_parts.isChecked() else ()
        self.start("route", self.route_ready, *options)

    def resume_preview(self):
        """Resume the last routing preview."""
        if self.run_id:
            options = ("--allow-placement",) if self.move_parts.isChecked() else ()
            self.start("route", self.route_ready, "--resume", self.run_id, *options)

    def route_ready(self, result):
        """Handle a routing preview report.

        Inputs:
            result: Report of the preview. A preview with fewer opens than the saved board
                is compared with it. Otherwise the status explains why not.
        """
        self.run_id = result["run_id"]
        before, after = (
            result.get("saved_board_opens"),
            result.get("best_preview_opens"),
        )
        zero_open_preview = result["status"] == "preview_zero_opens"
        self.resume.setEnabled(not zero_open_preview)
        self.apply_button.setEnabled(False)
        if isinstance(before, int) and isinstance(after, int) and after < before:
            self.start("compare", self.comparison_ready, self.run_id)
        elif zero_open_preview:
            self.status.setText(
                "The preview has zero open connections. Verify checks the saved board."
            )
        else:
            details = (
                result.get("blocked_nets") or result.get("failure") or result["status"]
            )
            self.status.setText(f"{after} preview opens. {details}")

    def comparison_ready(self, result):
        """Show the saved and previewed boards and allow applying the preview.

        Inputs:
            result: Comparison report with the geometry before and after.
        """
        self.before.show_board(result["before"])
        self.after.show_board(result["after"])
        self.apply_button.setEnabled(True)
        self.status.setText(
            "Preview ready for review. Apply saves the checked changes to KiCad."
        )

    def apply_preview(self):
        """After confirmation, save the previewed routing and reload the saved board."""
        if not self.run_id:
            return
        choice = QMessageBox.question(
            self,
            "Apply routing preview",
            "Save the independently checked routing improvement?",
        )
        if choice == QMessageBox.StandardButton.Yes:
            self.start(
                "apply", lambda _: self.select_project(self.location), self.run_id
            )

    def findings_ready(self, result):
        """List verification findings in the Verify tab.

        Inputs:
            result: Verify report. Native findings, reference-copper issues and the
                electrical review blocker become rows with a location in mm when known.
        """
        self.findings.clear()
        rows = list(result["native"]["findings"])
        reference = result.get("reference_screen") or {}
        for message in reference.get("blockers", []):
            rows.append(
                {
                    "check": "reference copper",
                    "description": message,
                    "positions_mm": [],
                }
            )
        labels = {
            "sampled_reference_gap": "Gap in sampled reference copper",
            "via_transition": "Layer transition needs return-path review",
            "unsupported_trace": "Trace geometry needs reference review",
        }
        for finding in reference.get("findings", []):
            route = f"{finding['net']} on {finding['signal_layer']}"
            spans = finding.get("gap_spans")
            if spans:
                for span in spans:
                    pads = ", ".join(
                        f"{pad['reference']}.{pad['pad']}"
                        for pad in span["endpoint_pads"]
                    )
                    detail = f"Gap on {finding['reference_layer']}"
                    if pads:
                        detail += f" at {pads}"
                    rows.append(
                        {
                            "check": "reference copper",
                            "description": f"{route} · {detail}",
                            "positions_mm": [span["position_mm"]],
                        }
                    )
            else:
                detail = (
                    f"No single reference polygon spans the trace on {finding['reference_layer']}"
                    if spans == []
                    else labels[finding["type"]]
                )
                rows.append(
                    {
                        "check": "reference copper",
                        "description": f"{route} · {detail}",
                        "positions_mm": finding["positions_mm"],
                    }
                )
        if result.get("electrical_review_blocker"):
            rows.append(
                {
                    "check": "electrical",
                    "description": result["electrical_review_blocker"],
                    "positions_mm": [],
                }
            )
        for row in rows:
            locations = row["positions_mm"]
            first = locations[0] if locations else None
            text = f"{first['x']:.3f}, {first['y']:.3f} mm" if first else ""
            item = QTreeWidgetItem((row["check"], row["description"], text))
            item.setToolTip(1, row["description"])
            if first and row["check"] != "erc":
                item.setData(0, Qt.ItemDataRole.UserRole, (first["x"], first["y"]))
            self.findings.addTopLevelItem(item)

    def finding_selected(self, item, _column):
        """Show the Layout tab and mark the location of the selected finding."""
        point = item.data(0, Qt.ItemDataRole.UserRole)
        if point is not None:
            self.tabs.setCurrentIndex(2)
            self.before.locate(*point)

    def export(self, draft):
        """Run the export job.

        Inputs:
            draft: True for the draft package, False for the checked package.
        """
        options = ("--draft",) if draft else ()
        self.start(
            "export",
            lambda value: self.exports.setPlainText(json.dumps(value, indent=2)),
            *options,
        )

    def diagnostics(self):
        """Ask for a file and save the toolchain diagnostics to it."""
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Export toolchain diagnostics",
            "autopcb-toolchain.json",
            "JSON (*.json)",
        )
        if target:
            self.start(
                "doctor",
                lambda _: self.status.setText("Toolchain diagnostics saved."),
                "--output",
                target,
            )

    def closeEvent(self, event):
        """Refuse to close while a job runs, otherwise remove the temporary files."""
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.status.setText("Wait for the active job or cancel it before closing.")
            event.ignore()
            return
        self.temp.cleanup()
        super().closeEvent(event)


class DesktopLauncher:
    """Starts the Qt application and shows the desktop window."""

    def __call__(
        self,
        project: Path,
        cli: Path | None,
        python: Path | None,
        *,
        workspace: Path | None = None,
    ) -> int:
        """Show the desktop window and run the Qt event loop.

        Inputs:
            project: KiCad file or folder to open.
            cli: KiCad CLI to pass to jobs, or None.
            python: KiCad Python to pass to jobs, or None.
            workspace: Workspace folder; the project's folder when None.
        Outputs:
            The application's exit status.
        """
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


main = DesktopLauncher()
