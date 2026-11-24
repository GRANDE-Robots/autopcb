"""routing_progress.py: record and plot KiCad checks and routing geometry from saved snapshots.

The recorder runs full KiCad DRC on a temporary copy and only publishes when
all checked sources remain unchanged. It never saves or reloads an editor.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from core.project import native_path

import argparse
import contextlib
import csv
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import pcbnew

from core.paths import long_path
from pcb.route_validation import _copy_inputs, _drc, _inputs
from review.geometry_metrics import GeometryMetrics, measure_board
from review.refresh_gallery import ReviewSnapshot
from core.toolchain import kicad_cli
from core.compat import file_sha256, strict_zip

CSV_NAME = "routing_progress.csv"
PNG_NAME = "routing_progress.png"
PREVIEW_NAME = "routing_preview_progress.json"
LEGACY_FIELDS = (
    "checked_at",
    "open_connections",
    "other_errors",
    "other_warnings",
    "schematic_parity",
    "pcb_sha256",
    "source_signature",
    "change",
    "evidence",
)
PREVIOUS_FIELDS = LEGACY_FIELDS + (
    "track_length_mm",
    "via_count",
    "degree_two_joints",
    "geometry_evidence",
)
FIELDS = PREVIOUS_FIELDS + (
    "symmetry_score",
    "routing_run_seconds",
    "optimization_evidence",
)


@dataclass(frozen=True)
class ProgressPoint:
    """One recorded check of the saved board.

    Inputs:
        checked_at: ISO 8601 time with an explicit time zone.
        open_connections: Unconnected items reported by DRC.
        other_errors: DRC violations of severity error.
        other_warnings: DRC violations of severity warning.
        schematic_parity: Schematic parity findings.
        pcb_sha256: SHA-256 of the saved board file.
        source_signature: SHA-256 over the hashes of every checked input.
        change: Short label of what changed.
        evidence: How the counts were obtained.
        track_length_mm: Total copper track length in mm, or None when unmeasured.
        via_count: Number of vias, or None when unmeasured.
        degree_two_joints: Two-segment joints, or None when unmeasured.
        geometry_evidence: How the geometry was measured.
        symmetry_score: Symmetry error of the routing, or None when unmeasured.
        routing_run_seconds: Recorded routing time in seconds, or None.
        optimization_evidence: Source-bound evidence for the two measures above.
    """

    checked_at: str
    open_connections: int
    other_errors: int
    other_warnings: int
    schematic_parity: int
    pcb_sha256: str
    source_signature: str
    change: str
    evidence: str
    track_length_mm: float | None = None
    via_count: int | None = None
    degree_two_joints: int | None = None
    geometry_evidence: str = ""
    symmetry_score: float | None = None
    routing_run_seconds: float | None = None
    optimization_evidence: str = ""

    def as_row(self) -> dict[str, str]:
        """Return the point as text for the progress table, with "" for an unmeasured value."""
        return {
            field: "" if getattr(self, field) is None else str(getattr(self, field))
            for field in FIELDS
        }


@dataclass(frozen=True)
class RunMetrics:
    """Measured optimization results of one routing run, bound to a saved board.

    Inputs:
        pcb_sha256: SHA-256 of the saved board the run produced.
        source_signature: Source signature of that board's checked inputs.
        symmetry_score: Symmetry error of the routing, or None.
        routing_run_seconds: Routing time in seconds, or None.
        evidence: Reference to the record that holds the measurement.
    """

    pcb_sha256: str
    source_signature: str
    symmetry_score: float | None
    routing_run_seconds: float | None
    evidence: str


class Fingerprint:
    """Identifies the saved inputs that a progress check depends on."""

    def __call__(self, project: Path) -> tuple[list[Path], dict[str, str], str]:
        """Hash the saved board, its routing inputs and the schematic.

        Inputs:
            project: Native project folder.
        Outputs:
            The input files, their SHA-256 keyed by project-relative path, and a
            SHA-256 over those hashes. Raises ValueError when the saved board or
            schematic is missing.
        """
        project = long_path(project)
        board = native_path(project, ".kicad_pcb")
        schematic = native_path(project, ".kicad_sch")
        if not board.is_file() or not schematic.is_file():
            raise ValueError("The saved v4.1 PCB and schematic are required")
        files = sorted({*_inputs(board), schematic})
        hashes = {
            path.relative_to(project).as_posix(): file_sha256(path) for path in files
        }
        signature = hashlib.sha256(
            json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return files, hashes, signature


_fingerprint = Fingerprint()


class ReadHistory:
    """Reads the recorded progress table."""

    @staticmethod
    def _point(row: dict) -> ProgressPoint:
        """Validate one table row and return it as a point.

        Inputs:
            row: A row of the progress table keyed by column name.
        Outputs:
            The point. Raises ValueError for a timestamp without a time zone, a
            negative count, a malformed hash, incomplete geometry, or a
            non-finite or negative measurement.
        """
        checked = datetime.fromisoformat(row["checked_at"])
        if checked.tzinfo is None:
            raise ValueError("Progress timestamps need an explicit time zone")
        counts = [int(row[field]) for field in FIELDS[1:5]]
        board = row["pcb_sha256"]
        signature = row["source_signature"]
        if any(value < 0 for value in counts):
            raise ValueError("Progress counts cannot be negative")
        if len(board) != 64 or any(c not in "0123456789abcdef" for c in board):
            raise ValueError("Progress board hash is invalid")
        if signature and (
            len(signature) != 64 or any(c not in "0123456789abcdef" for c in signature)
        ):
            raise ValueError("Progress source signature is invalid")
        geometry = [
            row.get(field, "")
            for field in ("track_length_mm", "via_count", "degree_two_joints")
        ]
        if any(geometry) and not all(geometry):
            raise ValueError("Geometry measurements must be complete or empty")
        length, vias, joints = (
            (float(geometry[0]), int(geometry[1]), int(geometry[2]))
            if all(geometry)
            else (None, None, None)
        )
        if length is not None and (
            not math.isfinite(length) or length < 0 or vias < 0 or joints < 0
        ):
            raise ValueError("Geometry measurements must be finite and nonnegative")
        optimization = [
            row.get(field, "") for field in ("symmetry_score", "routing_run_seconds")
        ]
        symmetry, runtime = (float(value) if value else None for value in optimization)
        if any(
            value is not None and (not math.isfinite(value) or value < 0)
            for value in (symmetry, runtime)
        ):
            raise ValueError("Optimization measurements must be finite and nonnegative")
        optimization_evidence = row.get("optimization_evidence", "")
        if (
            any(value is not None for value in (symmetry, runtime))
            and not optimization_evidence.strip()
        ):
            raise ValueError("Optimization measurements require source-bound evidence")
        return ProgressPoint(
            row["checked_at"],
            *counts,
            board,
            signature,
            row["change"],
            row["evidence"],
            length,
            vias,
            joints,
            row.get("geometry_evidence", ""),
            symmetry,
            runtime,
            optimization_evidence,
        )

    def __call__(self, path: Path) -> list[ProgressPoint]:
        """Read and validate the progress table.

        Inputs:
            path: CSV file with the legacy, previous or current columns.
        Outputs:
            The recorded points, or an empty list when the file does not exist.
            Raises ValueError when the columns are unexpected, a row is invalid
            or the timestamps are not in chronological order.
        """
        if not path.is_file():
            return []
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames not in (
                list(LEGACY_FIELDS),
                list(PREVIOUS_FIELDS),
                list(FIELDS),
            ):
                raise ValueError("Progress table has unexpected columns")
            points = [self._point(row) for row in reader]
        if any(
            datetime.fromisoformat(a.checked_at) > datetime.fromisoformat(b.checked_at)
            for a, b in zip(points, points[1:])
        ):
            raise ValueError("Progress timestamps must be chronological")
        return points


class EncodeHistory:
    """Serializes the progress table."""

    def __call__(self, points: list[ProgressPoint]) -> bytes:
        """Return the points as UTF-8 CSV with a header row and Unix line endings."""
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(point.as_row() for point in points)
        return output.getvalue().encode("utf-8")


read_history = ReadHistory()
encode_history = EncodeHistory()


class SavedBoardCheck:
    """Runs full KiCad DRC and the geometry measures on a stable copy of the saved board."""

    @staticmethod
    def summarize_report(data: dict) -> tuple[int, int, int, int]:
        """Count the findings of a KiCad DRC report.

        Inputs:
            data: The parsed DRC report with schematic parity.
        Outputs:
            Unconnected items, error violations, warning violations and parity
            findings. Raises ValueError when the report lacks a list, ignores a
            check, or excludes errors or warnings.
        """
        if not isinstance(data, dict) or not isinstance(
            data.get("unconnected_items"), list
        ):
            raise ValueError("KiCad did not provide the unconnected-item list")
        if not isinstance(data.get("violations"), list) or not isinstance(
            data.get("schematic_parity"), list
        ):
            raise ValueError("KiCad did not provide the complete DRC and parity lists")
        if data.get("ignored_checks"):
            raise ValueError(
                "KiCad reports ignored checks; progress count was not recorded"
            )
        severities = data.get("included_severities")
        if not isinstance(severities, list) or not {"error", "warning"} <= set(
            severities
        ):
            raise ValueError("KiCad did not include both errors and warnings")
        other_errors = sum(
            item.get("severity") == "error" for item in data["violations"]
        )
        other_warnings = sum(
            item.get("severity") == "warning" for item in data["violations"]
        )
        return (
            len(data["unconnected_items"]),
            other_errors,
            other_warnings,
            len(data["schematic_parity"]),
        )

    def __call__(
        self, project: Path, kicad_python: Path, label: str
    ) -> tuple[ProgressPoint, dict[str, str]]:
        """Check the saved board on a temporary copy and measure its geometry.

        Inputs:
            project: Native project folder.
            kicad_python: KiCad's Python; ``kicad-cli`` beside it is preferred.
            label: Short description stored with the point.
        Outputs:
            The new point and the input hashes it was measured from. Raises
            RuntimeError when an input or the copied board changes during the
            check, and ValueError for an incomplete DRC report.
        """
        project = long_path(project)
        files, before, signature = _fingerprint(project)
        board = native_path(project, ".kicad_pcb")
        adjacent = kicad_python.parent / (
            "kicad-cli.exe" if os.name == "nt" else "kicad-cli"
        )
        cli = kicad_cli(adjacent if adjacent.is_file() else None)
        with tempfile.TemporaryDirectory(prefix="jessie-progress-drc-") as directory:
            scratch = Path(directory)
            _copy_inputs(
                [path for path in files if path.suffix != ".kicad_sch"], board, scratch
            )
            shutil.copy2(
                native_path(project, ".kicad_sch"), native_path(scratch, ".kicad_sch")
            )
            copied = {name: file_sha256(scratch / name) for name in before}
            if copied != before:
                raise RuntimeError("Inputs changed while copying the saved board")
            output = scratch / "drc.json"
            report = _drc(cli, scratch / board.name, output, schematic_parity=True)
            counts = self.summarize_report(report)
            copied_board = scratch / board.name
            if file_sha256(copied_board) != before[board.name]:
                raise RuntimeError("The copied PCB changed during DRC")
            geometry = measure_board(pcbnew.LoadBoard(str(copied_board)))
            if file_sha256(copied_board) != before[board.name]:
                raise RuntimeError("The copied PCB changed during measurement")
        _, after, after_signature = _fingerprint(project)
        if after != before or after_signature != signature:
            raise RuntimeError(
                "Saved design inputs changed during DRC; no progress was recorded"
            )
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        point = ProgressPoint(
            timestamp,
            *counts,
            before[board.name],
            signature,
            label,
            "Full KiCad DRC of stable saved-file copy",
            geometry.track_length_mm,
            geometry.via_count,
            geometry.degree_two_joints,
            "Exact saved PCB hash; measured on temporary copy",
        )
        return point, before


check_saved_board = SavedBoardCheck()


class ProgressPace:
    """Measures how fast the open connections fell, and where the design scope changed."""

    @staticmethod
    def latest_scope_change(points: list[ProgressPoint], preview_start: int | None):
        """Find the latest saved design change that alters the routing scope.

        Inputs:
            points: Saved points followed by any private preview points.
            preview_start: Index of the first preview point, or None.
        Outputs:
            The index of the latest saved point whose change text marks a scope
            change and the text before the marker, or None when there is none.
        """
        end = len(points) if preview_start is None else min(preview_start, len(points))
        for index in range(end - 1, -1, -1):
            description = points[index].change.strip()
            marker = description.casefold().find("; scope change")
            if marker >= 0:
                return index, description[:marker].strip() or "Design scope changed"
        return None

    @staticmethod
    def latest_preview_pace(points: list[ProgressPoint], preview_start: int | None):
        """Measure the latest open reduction against elapsed wall time.

        Inputs:
            points: Saved points followed by the preview points.
            preview_start: Index of the first preview point, or None.
        Outputs:
            The open connections gained, the elapsed seconds and the gain per
            minute, or None when there is no preview or no positive elapsed time.
        """
        if (
            preview_start is None
            or preview_start < 0
            or preview_start >= len(points) - 1
        ):
            return None
        latest = points[-1]
        for previous in reversed(points[preview_start:-1]):
            gain = previous.open_connections - latest.open_connections
            if gain > 0:
                seconds = (
                    datetime.fromisoformat(latest.checked_at)
                    - datetime.fromisoformat(previous.checked_at)
                ).total_seconds()
                return (gain, seconds, gain * 60 / seconds) if seconds > 0 else None
        first = points[preview_start]
        if all(
            point.open_connections == latest.open_connections
            for point in points[preview_start:]
        ):
            seconds = (
                datetime.fromisoformat(latest.checked_at)
                - datetime.fromisoformat(first.checked_at)
            ).total_seconds()
            return (0, seconds, 0.0) if seconds > 0 else None
        return None

    @staticmethod
    def latest_recorded_run_pace(
        points: list[ProgressPoint], preview_start: int | None
    ):
        """Measure gain against recorded routing/checking time, excluding idle time.

        Inputs:
            points: Saved points followed by the preview points.
            preview_start: Index of the first preview point, or None.
        Outputs:
            The open connections gained, the recorded run seconds between the
            two points and the gain per minute, or None when the run times are
            missing, not finite or not increasing.
        """
        if (
            preview_start is None
            or preview_start < 0
            or preview_start >= len(points) - 1
        ):
            return None
        latest = points[-1]
        if latest.routing_run_seconds is None or not math.isfinite(
            latest.routing_run_seconds
        ):
            return None
        for previous in reversed(points[preview_start:-1]):
            gain = previous.open_connections - latest.open_connections
            if gain > 0:
                old_seconds = previous.routing_run_seconds
                if old_seconds is None or not math.isfinite(old_seconds):
                    return None
                seconds = latest.routing_run_seconds - old_seconds
                return (gain, seconds, gain * 60 / seconds) if seconds > 0 else None
        first = points[preview_start]
        if (
            all(
                point.open_connections == latest.open_connections
                for point in points[preview_start:]
            )
            and first.routing_run_seconds is not None
            and math.isfinite(first.routing_run_seconds)
        ):
            seconds = latest.routing_run_seconds - first.routing_run_seconds
            return (0, seconds, 0.0) if seconds > 0 else None
        return None


class ChartCanvas:
    """A supersampled drawing surface for the progress chart."""

    def __init__(self, width: int, height: int, scale: int = 2):
        """Create a white canvas.

        Inputs:
            width: Output width in pixels.
            height: Output height in pixels.
            scale: Supersampling factor; every coordinate is multiplied by it.
        """
        self.scale = scale
        self.width, self.height = width, height
        self.image = Image.new("RGB", (width * scale, height * scale), "white")
        self.draw = ImageDraw.Draw(self.image)

    def xy(self, value: float) -> int:
        """Convert a chart coordinate to a supersampled pixel."""
        return round(value * self.scale)

    @staticmethod
    def font(
        size: int, bold: bool = False
    ) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        """Load a Windows UI font of ``size`` pixels, or the default font when none is installed."""
        names = (
            [r"C:\Windows\Fonts\seguisb.ttf", r"C:\Windows\Fonts\arialbd.ttf"]
            if bold
            else [r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf"]
        )
        for name in names:
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                pass
        return ImageFont.load_default(size=size)

    def text(
        self,
        x: float,
        y: float,
        value: str,
        size: int,
        color: str,
        *,
        bold: bool = False,
        anchor: str | None = None,
    ) -> None:
        """Draw text with its anchor at (``x``, ``y``) in chart coordinates."""
        self.draw.text(
            (self.xy(x), self.xy(y)),
            value,
            fill=color,
            font=self.font(self.xy(size), bold),
            anchor=anchor,
        )

    def line(
        self,
        vertices: list[tuple[float, float]],
        color: str,
        pattern: tuple[int, ...] | None,
        thickness: int = 3,
    ) -> None:
        """Draw a polyline, solid or dashed.

        Inputs:
            vertices: Chart coordinates; fewer than two draw nothing.
            color: Line color.
            pattern: Alternating dash and gap lengths, or None for a solid line.
            thickness: Line width in chart units.
        """
        xy, draw = self.xy, self.draw
        if len(vertices) < 2:
            return
        if pattern is None:
            draw.line(
                [(xy(x), xy(y)) for x, y in vertices],
                fill=color,
                width=xy(thickness),
                joint="curve",
            )
            return
        index, used = 0, 0.0
        for (x1, y1), (x2, y2) in zip(vertices, vertices[1:]):
            dx, dy = x2 - x1, y2 - y1
            length = math.hypot(dx, dy)
            position = 0.0
            while position < length:
                distance = min(pattern[index] - used, length - position)
                if index % 2 == 0 and distance > 0:
                    a, b = position / length, (position + distance) / length
                    draw.line(
                        (
                            xy(x1 + dx * a),
                            xy(y1 + dy * a),
                            xy(x1 + dx * b),
                            xy(y1 + dy * b),
                        ),
                        fill=color,
                        width=xy(thickness),
                    )
                position += distance
                used += distance
                if used >= pattern[index] - 1e-8:
                    index = (index + 1) % len(pattern)
                    used = 0.0

    def marker(
        self, x: float, y: float, color: str, shape: str, radius: float = 4
    ) -> None:
        """Draw a circle, square, diamond or (for any other shape) triangle marker."""
        xy, draw = self.xy, self.draw
        box = (xy(x - radius), xy(y - radius), xy(x + radius), xy(y + radius))
        if shape == "circle":
            draw.ellipse(box, fill="white", outline=color, width=xy(2))
        elif shape == "square":
            draw.rectangle(box, fill="white", outline=color, width=xy(2))
        elif shape == "diamond":
            draw.polygon(
                [
                    (xy(x), xy(y - radius)),
                    (xy(x + radius), xy(y)),
                    (xy(x), xy(y + radius)),
                    (xy(x - radius), xy(y)),
                ],
                fill=color,
            )
        else:
            draw.polygon(
                [
                    (xy(x), xy(y - radius)),
                    (xy(x + radius), xy(y + radius)),
                    (xy(x - radius), xy(y + radius)),
                ],
                fill=color,
            )

    def save(self, output: Path) -> None:
        """Downsample to the output size and write an optimized PNG."""
        self.image.resize((self.width, self.height), Image.Resampling.LANCZOS).save(
            output, format="PNG", optimize=True
        )


class ProgressChart:
    """Plots recorded progress points on one time axis."""

    def __init__(self, points: list[ProgressPoint], preview_start: int | None):
        """Prepare one chart.

        Inputs:
            points: Saved points followed by any private preview points.
            preview_start: Index of the first preview point, or None.
        """
        self.points = points
        self.preview_start = preview_start
        self.canvas = ChartCanvas(1600, 820)
        self.series = self.chart_series(points)

    @staticmethod
    def chart_series(points: list[ProgressPoint]) -> tuple:
        """Return the available metrics, with distinct styles and named units."""
        definitions = (
            (
                "Open connections",
                "open_connections",
                "#087f8c",
                None,
                "circle",
                "count",
            ),
            ("Other errors", "other_errors", "#c45b2b", (11, 6), "square", "count"),
            ("Warnings", "other_warnings", "#7452a3", (2, 5), "diamond", "count"),
            ("Vias", "via_count", "#a16b00", (12, 4), "square", "count"),
            (
                "Two-segment joints",
                "degree_two_joints",
                "#4c793c",
                (3, 4),
                "diamond",
                "count",
            ),
            ("Track length", "track_length_mm", "#b54d74", None, "circle", "mm"),
            (
                "Symmetry error",
                "symmetry_score",
                "#1267ac",
                (7, 4),
                "triangle",
                "score",
            ),
            ("Run time", "routing_run_seconds", "#a84839", (4, 3), "circle", "seconds"),
        )
        result = []
        for label, field, color, pattern, marker, axis in definitions:
            values = [getattr(point, field) for point in points]
            if any(value is not None for value in values):
                result.append((label, field, color, pattern, marker, axis, values))
        return tuple(result)

    @staticmethod
    def series_runs(vertices, break_indices):
        """Keep plotted lines separate across design changes and preview boundaries."""
        runs = []
        current = []
        for index, (x, y) in enumerate(vertices):
            if (index in break_indices or y is None) and current:
                runs.append(current)
                current = []
            if y is not None:
                current.append((x, y))
        if current:
            runs.append(current)
        return runs

    @staticmethod
    def is_valid(path: Path) -> bool:
        """Say whether ``path`` is a readable, intact PNG."""
        try:
            with Image.open(path) as picture:
                if picture.format != "PNG":
                    return False
                picture.verify()
            return True
        except (OSError, ValueError):
            return False

    @classmethod
    def draw(
        cls,
        points: list[ProgressPoint],
        output: Path,
        *,
        preview_start: int | None = None,
    ) -> None:
        """Plot each recorded count on one time axis without redundant annotations.

        Inputs:
            points: Saved points followed by any private preview points.
            output: PNG file to write.
            preview_start: Index of the first preview point, shaded and drawn
                with triangle markers, or None.
        Outputs: None. Raises ValueError when ``points`` is empty.
        """
        if not points:
            raise ValueError("At least one progress point is required")
        chart = cls(points, preview_start)
        chart.write_title()
        chart.set_layout()
        chart.write_legend()
        chart.draw_count_axis()
        chart.draw_extra_axes()
        chart.place_times()
        chart.shade_preview()
        chart.mark_scope_change()
        chart.draw_series()
        chart.draw_time_ticks()
        chart.canvas.save(output)

    def write_title(self) -> None:
        """Write the title and, with a preview, the saved and preview open counts and paces."""
        text = self.canvas.text
        points, preview_start = self.points, self.preview_start
        text(75, 50, "PCB progress", 34, "#15233b", bold=True)
        if preview_start is not None:
            text(
                1525,
                55,
                f"Saved: {points[preview_start - 1].open_connections} opens  |  Private preview: {points[-1].open_connections}",
                20,
                "#34445a",
                anchor="ra",
            )
            pace = ProgressPace.latest_preview_pace(points, preview_start)
            if pace is not None:
                gain, seconds, per_minute = pace
                unit = "open" if gain == 1 else "opens"
                text(
                    1525,
                    85,
                    f"Elapsed: {gain} {unit} in {seconds / 60:.1f} min ({per_minute:.3g}/min)",
                    16,
                    "#34445a",
                    anchor="ra",
                )
            recorded = ProgressPace.latest_recorded_run_pace(points, preview_start)
            if recorded is not None:
                gain, seconds, per_minute = recorded
                unit = "open" if gain == 1 else "opens"
                text(
                    1525,
                    106,
                    f"Recorded run: {gain} {unit} in {seconds / 60:.1f} min ({per_minute:.3g}/min)",
                    16,
                    "#34445a",
                    anchor="ra",
                )

    def set_layout(self) -> None:
        """Choose the secondary axes and the plot area."""
        series = self.series
        self.extra_axes = [
            axis
            for axis in ("mm", "score", "seconds")
            if any(item[5] == axis for item in series)
        ]
        self.left, self.right, self.top, self.bottom = (
            135,
            1530 - 75 * len(self.extra_axes),
            220 if len(series) > 4 else 190,
            680,
        )

    def write_legend(self) -> None:
        """Write one legend entry per series, four to a row."""
        canvas, left = self.canvas, self.left
        for index, (label, _, color, pattern, shape, _, _) in enumerate(self.series):
            x = left + (index % 4) * 345
            y = 145 + (index // 4) * 36
            canvas.line([(x, y), (x + 42, y)], color, pattern)
            canvas.marker(x + 21, y, color, shape)
            canvas.text(x + 55, y, label, 18, "#34445a", anchor="lm")

    def draw_count_axis(self) -> None:
        """Draw the grid and left-hand scale for the count series."""
        canvas, series = self.canvas, self.series
        left, right, top, bottom = self.left, self.right, self.top, self.bottom
        max_value = max(
            value
            for _, _, _, _, _, axis, values in series
            if axis == "count"
            for value in values
            if value is not None
        )
        raw_step = max(max_value / 5, 1.0)
        magnitude = 10 ** math.floor(math.log10(raw_step))
        step = next(
            value * magnitude
            for value in (1, 2, 5, 10)
            if value * magnitude >= raw_step
        )
        high = max(step, math.ceil(max_value / step) * step)
        for value in range(0, int(high) + 1, int(step)):
            y = bottom - value * (bottom - top) / high
            canvas.draw.line(
                (canvas.xy(left), canvas.xy(y), canvas.xy(right), canvas.xy(y)),
                fill="#e4e9ef",
                width=canvas.xy(1),
            )
            canvas.text(left - 16, y, str(value), 17, "#64748b", anchor="rm")
        canvas.text(left, top - 25, "Count", 16, "#64748b")
        self.axis_high = {"count": high}

    def draw_extra_axes(self) -> None:
        """Draw a right-hand scale for each of the track length, score and run time axes."""
        canvas, series, axis_high = self.canvas, self.series, self.axis_high
        right, top, bottom = self.right, self.top, self.bottom
        for axis_index, axis_name in enumerate(self.extra_axes):
            maximum = max(
                value
                for _, _, _, _, _, axis, values in series
                if axis == axis_name
                for value in values
                if value is not None
            )
            raw = maximum / 5 if maximum else 1.0
            magnitude = 10 ** math.floor(math.log10(raw))
            axis_step = next(
                value * magnitude for value in (1, 2, 5, 10) if value * magnitude >= raw
            )
            limit = max(axis_step, math.ceil(maximum / axis_step) * axis_step)
            axis_high[axis_name] = limit
            color = next(item[2] for item in series if item[5] == axis_name)
            x = right + 15 + axis_index * 75
            for index in range(round(limit / axis_step) + 1):
                value = index * axis_step
                y = bottom - value * (bottom - top) / limit
                canvas.text(x, y, format(value, ".4g"), 17, color, anchor="lm")
            canvas.text(
                x,
                top - 25,
                {"mm": "mm", "score": "score", "seconds": "s"}[axis_name],
                16,
                color,
                anchor="lm",
            )

    def place_times(self) -> None:
        """Convert the check times to horizontal positions across the plot area."""
        left, right = self.left, self.right
        self.times = [datetime.fromisoformat(point.checked_at) for point in self.points]
        self.seconds = [value.timestamp() for value in self.times]
        self.span = self.seconds[-1] - self.seconds[0]
        self.xs = (
            [left + (right - left) / 2] * len(self.points)
            if self.span == 0
            else [
                left + (stamp - self.seconds[0]) * (right - left) / self.span
                for stamp in self.seconds
            ]
        )

    def shade_preview(self) -> None:
        """Shade the private-preview region and explain its markers."""
        preview_start = self.preview_start
        if preview_start is not None:
            canvas = self.canvas
            boundary = max(self.left, self.xs[preview_start])
            canvas.draw.rectangle(
                (
                    canvas.xy(boundary),
                    canvas.xy(self.top),
                    canvas.xy(self.right),
                    canvas.xy(self.bottom),
                ),
                fill="#eef7fa",
            )
            canvas.text(
                self.right,
                self.bottom + 83,
                "Triangles: KiCad-checked preview; electrical checks pending; not saved to PCB",
                16,
                "#176780",
                anchor="ra",
            )

    def mark_scope_change(self) -> None:
        """Draw a dashed line and caption at the latest scope change."""
        scope_change = ProgressPace.latest_scope_change(self.points, self.preview_start)
        if scope_change is not None:
            canvas = self.canvas
            index, description = scope_change
            x = self.xs[index]
            canvas.line(
                [(x, self.top), (x, self.bottom)], "#9baabd", (4, 5), thickness=1
            )
            anchor = "ra" if x > (self.left + self.right) / 2 else "la"
            canvas.text(
                x,
                self.bottom + 58,
                f"Scope change: {description}",
                15,
                "#53657a",
                anchor=anchor,
            )

    def draw_series(self) -> None:
        """Draw every series as separate runs with markers on about twelve points."""
        canvas, points, preview_start = self.canvas, self.points, self.preview_start
        top, bottom = self.top, self.bottom
        for _, _, color, pattern, shape, axis, values in self.series:
            limit = self.axis_high[axis]
            vertices = [
                (x, None if value is None else bottom - value * (bottom - top) / limit)
                for x, value in zip(self.xs, values)
            ]
            saved_end = len(points) if preview_start is None else preview_start
            boundaries = {
                index
                for index, point in enumerate(points[:saved_end])
                if "; scope change" in point.change.casefold()
            }
            if preview_start is not None:
                boundaries.add(preview_start)
            for run in self.series_runs(vertices, boundaries):
                canvas.line(run, color, pattern)
            valid = [index for index, (_, y) in enumerate(vertices) if y is not None]
            stride = max(1, len(valid) // 12)
            selected = set(valid[::stride]) | ({valid[-1]} if valid else set())
            for index in sorted(selected):
                x, y = vertices[index]
                canvas.marker(
                    x,
                    y,
                    color,
                    "triangle"
                    if preview_start is not None and index >= preview_start
                    else shape,
                )

    def draw_time_ticks(self) -> None:
        """Draw five time ticks, or one when every point has the same time."""
        canvas, span, seconds = self.canvas, self.span, self.seconds
        left, right, bottom = self.left, self.right, self.bottom
        tick_count = 5 if span > 0 else 1
        for index in range(tick_count):
            fraction = index / (tick_count - 1) if tick_count > 1 else 0.5
            stamp = seconds[0] + fraction * span
            display_zone = (
                self.times[self.preview_start - 1].tzinfo
                if self.preview_start is not None
                else self.times[-1].tzinfo
            )
            label = datetime.fromtimestamp(stamp, display_zone).strftime(
                "%b %d  %I:%M %p"
            )
            x = left + fraction * (right - left)
            canvas.draw.line(
                (
                    canvas.xy(x),
                    canvas.xy(bottom),
                    canvas.xy(x),
                    canvas.xy(bottom + 7),
                ),
                fill="#a5b1c1",
                width=canvas.xy(1),
            )
            canvas.text(x, bottom + 20, label, 16, "#64748b", anchor="ma")


class PreviewSeries:
    """Overlays private routing-preview measurements on the saved progress history."""

    @staticmethod
    def review_points(
        history: list[ProgressPoint], review: Path
    ) -> tuple[list[ProgressPoint], int | None]:
        """Overlay only source-matching private measurements; never rewrite saved history.

        Inputs:
            history: The saved points.
            review: Review folder that may hold the preview record.
        Outputs:
            The saved points followed by the preview points and the index of the
            first preview point, or the saved points and None when there is no
            current preview. Raises ValueError for an invalid preview record,
            measurement or ordering.
        """
        path = review / PREVIEW_NAME
        if not history or not path.is_file():
            return history, None
        data = json.loads(path.read_text(encoding="utf-8"))
        project = review.parent.parent.resolve()
        if data.get("schema") != 1 or not isinstance(data.get("inputs"), dict):
            raise ValueError("Invalid preview progress record")
        for name, expected in data["inputs"].items():
            source = (project / name).resolve()
            if not source.is_relative_to(project):
                raise ValueError("Preview input escapes the project")
            actual = file_sha256(source) if source.is_file() else "absent"
            if actual != expected:
                return history, None  # An older preview is not current-board progress.
        if (
            data["inputs"].get(native_path(project, ".kicad_pcb").name)
            != history[-1].pcb_sha256
        ):
            return history, None
        points = [ProgressPoint(**raw) for raw in data.get("points", [])]
        for point in points:
            if (
                datetime.fromisoformat(point.checked_at).tzinfo is None
                or any(
                    isinstance(v, bool) or not isinstance(v, int) or v < 0
                    for v in (
                        point.open_connections,
                        point.other_errors,
                        point.other_warnings,
                    )
                )
                or len(point.pcb_sha256) != 64
            ):
                raise ValueError("Invalid private progress measurement")
        cutoff = datetime.fromisoformat(history[-1].checked_at)
        points = [p for p in points if datetime.fromisoformat(p.checked_at) >= cutoff]
        if any(
            datetime.fromisoformat(a.checked_at) > datetime.fromisoformat(b.checked_at)
            for a, b in zip(points, points[1:])
        ):
            raise ValueError("Private progress timestamps are out of order")
        return (history + points, len(history)) if points else (history, None)

    @classmethod
    def draw_review_chart(
        cls, history: list[ProgressPoint], review: Path, output: Path
    ) -> None:
        """Plot the saved history with any current private preview.

        Inputs:
            history: The saved points.
            review: Review folder that may hold the preview record.
            output: PNG file to write.
        """
        points, first = cls.review_points(history, review)
        ProgressChart.draw(points, output, preview_start=first)

    @staticmethod
    def refresh_preview(project: Path, run_id: str) -> dict:
        """Revalidate a retained private checkpoint before showing its measurements.

        Inputs:
            project: Native project folder.
            run_id: Identifier of a retained autorouter run.
        Outputs:
            The result record with the saved and preview open counts, the point
            count and the chart path. Raises ValueError when the checkpoint
            changes protected design items, and RuntimeError when the run file
            changes during verification.
        """
        from core.paths import asset_root
        from pcb.net_routing.controller import decode_checkpoint
        from pcb.net_routing.native import Snapshot, sha
        from pcb.net_routing.protection import ProtectedState
        from pcb.net_routing.runner import (
            _run_id,
            _source_hashes,
            _progress_sample,
            verify_resume_board,
        )

        assets = asset_root(project)
        path = assets / "results" / "autoroute" / f"{_run_id(run_id)}.json"
        original = path.read_bytes()
        state = json.loads(original)
        with Snapshot(native_path(project, ".kicad_pcb")) as snapshot:
            inputs = _source_hashes(snapshot, project, assets)
            candidate = decode_checkpoint(
                state["checkpoint"],
                inputs,
                snapshot.tool_hashes,
                sha(assets / "config" / "autopcb.json"),
                allow_tool_changes=True,
            )
            schematic = (native_path(snapshot.directory, ".kicad_sch")).read_bytes()
            if ProtectedState.capture(snapshot.original, schematic).differences(
                candidate, schematic
            ):
                raise ValueError("Preview changes protected design items")
            snapshot.board_path.write_bytes(candidate)
            verified = verify_resume_board(
                snapshot, project, state["best_preview_opens"]
            )
            _progress_sample(
                state,
                sha(snapshot.board_path),
                verified["drc"],
                verified["geometry"],
                state["elapsed_seconds"],
            )
            snapshot.check()
            if path.read_bytes() != original:
                raise RuntimeError(
                    "Preview changed during progress verification; retry"
                )
            publish_preview(project, inputs, run_id, state["progress_samples"])
        return {
            "status": "preview_refreshed",
            "open_connections": state["saved_board_opens"],
            "preview_opens": state["best_preview_opens"],
            "points": len(state["progress_samples"]),
            "plot": str(project / "deliverables" / "review" / PNG_NAME),
        }


class PublishPreview:
    """Publishes private preview measurements as a distinct series on the progress chart."""

    def __call__(
        self, project: Path, inputs: dict, run_id: str, samples: list[dict]
    ) -> None:
        """Publish already validated candidate measurements as a distinct preview series.

        Inputs:
            project: Native project folder.
            inputs: SHA-256, or "absent", of each routing input by project path.
            run_id: Identifier of the routing run.
            samples: Preview points as dicts of ``ProgressPoint`` fields.
        Outputs: None. Nothing is published without samples or saved history.
            Raises RuntimeError when an input or the saved history changes
            before or during publication.
        """
        project = project.resolve()
        review = project / "deliverables" / "review"
        if not samples or not (review / CSV_NAME).is_file():
            return
        publication = ReviewSnapshot.capture(review, {PNG_NAME, PREVIEW_NAME})
        saved_history = (review / CSV_NAME).read_bytes()
        history = read_history(review / CSV_NAME)
        payload = {"schema": 1, "run_id": run_id, "inputs": inputs, "points": samples}
        with tempfile.TemporaryDirectory(
            prefix="autopcb-preview-progress-"
        ) as directory:
            staged = Path(directory)
            (staged / PREVIEW_NAME).write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
            # Validate the native source identity immediately before publication.
            for name, expected in inputs.items():
                source = (project / name).resolve()
                if (
                    not source.is_relative_to(project)
                    or (file_sha256(source) if source.is_file() else "absent")
                    != expected
                ):
                    raise RuntimeError(
                        "Routing inputs changed before progress publication"
                    )
            points = [ProgressPoint(**raw) for raw in samples]
            ProgressChart.draw(
                history + points, staged / PNG_NAME, preview_start=len(history)
            )
            with Image.open(staged / PNG_NAME) as picture:
                picture.verify()
            for name, expected in inputs.items():
                source = project / name
                if (file_sha256(source) if source.is_file() else "absent") != expected:
                    raise RuntimeError(
                        "Routing inputs changed during progress rendering"
                    )
            if (review / CSV_NAME).read_bytes() != saved_history:
                raise RuntimeError("Saved progress changed during preview rendering")
            publication.publish(staged)


publish_preview = PublishPreview()


class ProgressRecorder:
    """Records, redraws and backfills the saved-board progress table and chart."""

    @staticmethod
    def redraw(review: Path) -> dict:
        """Redraw existing recorded data without opening or checking the board.

        Inputs:
            review: Review folder that holds the progress table.
        Outputs:
            The result record. Raises ValueError when there is no history.
        """
        review = review.resolve()
        publication = ReviewSnapshot.capture(review, {CSV_NAME, PNG_NAME})
        if publication.originals[CSV_NAME] is None:
            raise ValueError("No routing progress history is available")
        points = read_history(review / CSV_NAME)
        with tempfile.TemporaryDirectory(prefix="jessie-progress-redraw-") as directory:
            staged = Path(directory)
            (staged / CSV_NAME).write_bytes(publication.originals[CSV_NAME])
            PreviewSeries.draw_review_chart(points, review, staged / PNG_NAME)
            with Image.open(staged / PNG_NAME) as image:
                image.verify()
            publication.publish(staged)
        return {
            "status": "redrawn",
            "points": len(points),
            "plot": str(review / PNG_NAME),
        }

    @staticmethod
    def attach_geometry(
        point: ProgressPoint, metrics: GeometryMetrics, evidence: str
    ) -> ProgressPoint:
        """Fill missing geometry only; never silently rewrite measured history.

        Inputs:
            point: A recorded point.
            metrics: Geometry measured from the point's exact board.
            evidence: How the geometry was measured.
        Outputs:
            The point with the geometry filled in, or unchanged when it already
            matches. Raises ValueError when the point has different geometry.
        """
        old = (point.track_length_mm, point.via_count, point.degree_two_joints)
        new = (metrics.track_length_mm, metrics.via_count, metrics.degree_two_joints)
        if all(value is None for value in old):
            return replace(
                point,
                track_length_mm=new[0],
                via_count=new[1],
                degree_two_joints=new[2],
                geometry_evidence=evidence,
            )
        if old != new:
            raise ValueError(f"Geometry disagrees for PCB hash {point.pcb_sha256}")
        return point

    @staticmethod
    def attach_run_metrics(
        point: ProgressPoint,
        pcb_sha256: str,
        source_signature: str,
        symmetry_score: float | None,
        routing_run_seconds: float | None,
        evidence: str,
    ) -> ProgressPoint:
        """Add measured optimization metrics only to their exact saved-source row.

        Inputs:
            point: A recorded point.
            pcb_sha256: SHA-256 of the board the metrics were measured on.
            source_signature: Source signature of that board's checked inputs.
            symmetry_score: Symmetry error, or None.
            routing_run_seconds: Routing time in seconds, or None.
            evidence: Reference to the record that holds the measurement.
        Outputs:
            The point with the missing metrics filled in. Raises ValueError when
            the board or source differs, the evidence is empty, a metric is not
            a finite nonnegative number, or it disagrees with the recorded one.
        """
        if (
            pcb_sha256 != point.pcb_sha256
            or not source_signature
            or source_signature != point.source_signature
        ):
            raise ValueError("Run metrics do not match the saved board source")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("Run metrics require an evidence reference")
        values = (symmetry_score, routing_run_seconds)
        if all(value is None for value in values) or any(
            value is not None
            and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            )
            for value in values
        ):
            raise ValueError("Run metrics must be finite and nonnegative")
        old = (point.symmetry_score, point.routing_run_seconds)
        if any(
            previous is not None and new is not None and previous != new
            for previous, new in strict_zip(old, values)
        ):
            raise ValueError("Run metrics disagree with the recorded history")
        return replace(
            point,
            symmetry_score=old[0] if old[0] is not None else symmetry_score,
            routing_run_seconds=old[1] if old[1] is not None else routing_run_seconds,
            optimization_evidence=point.optimization_evidence or evidence.strip(),
        )

    @staticmethod
    @contextlib.contextmanager
    def summarize_kicad_warnings():
        """Summarize only KiCad's repeated image-handler message; keep other stderr."""
        sys.stderr.flush()
        previous = os.dup(2)
        with tempfile.TemporaryFile(mode="w+b") as captured:
            try:
                os.dup2(captured.fileno(), 2)
                yield
            finally:
                sys.stderr.flush()
                os.dup2(previous, 2)
                os.close(previous)
                captured.seek(0)
                duplicates = 0
                for line in (
                    captured.read()
                    .decode("utf-8", errors="replace")
                    .splitlines(keepends=True)
                ):
                    if line.startswith(
                        "Adding duplicate image handler for '"
                    ) and line.rstrip().endswith("'"):
                        duplicates += 1
                    else:
                        sys.stderr.write(line)
                if duplicates:
                    print(
                        f"KiCad emitted {duplicates} duplicate image-handler messages.",
                        file=sys.stderr,
                    )

    @classmethod
    def backfill(cls, project: Path, review: Path, snapshots: Path) -> dict:
        """Fill historical geometry from exact-hash PCB snapshots, without routing.

        Inputs:
            project: Native project folder.
            review: The project's review folder.
            snapshots: Folder whose subfolders hold saved ``.kicad_pcb`` copies.
        Outputs:
            The result record. Raises ValueError for a review folder that is not
            this project's or a missing snapshot folder, and RuntimeError when a
            snapshot changes during measurement.
        """
        project, review, snapshots = (
            project.resolve(),
            review.resolve(),
            snapshots.resolve(),
        )
        if (
            not review.is_dir()
            or review != (project / "deliverables" / "review").resolve()
        ):
            raise ValueError(
                "Progress destination must be this project's review folder"
            )
        if not snapshots.is_dir():
            raise ValueError("Snapshot root must be an existing directory")
        publication = ReviewSnapshot.capture(review, {CSV_NAME, PNG_NAME})
        history = read_history(review / CSV_NAME)
        wanted = {
            point.pcb_sha256 for point in history if point.track_length_mm is None
        }
        measured: dict[str, tuple[GeometryMetrics, str]] = {}
        candidates = [
            native_path(project, ".kicad_pcb"),
            *snapshots.glob("*/*.kicad_pcb"),
        ]
        with cls.summarize_kicad_warnings():
            for path in candidates:
                if not wanted:
                    break
                if not path.is_file():
                    continue
                digest = file_sha256(path)
                if digest not in wanted:
                    continue
                metrics = measure_board(pcbnew.LoadBoard(str(path)))
                if file_sha256(path) != digest:
                    raise RuntimeError(
                        f"PCB snapshot changed during measurement: {path}"
                    )
                measured[digest] = (metrics, f"Exact SHA-256-matched snapshot: {path}")
                wanted.remove(digest)
        updated = 0
        for index, point in enumerate(history):
            if point.track_length_mm is None and point.pcb_sha256 in measured:
                metrics, evidence = measured[point.pcb_sha256]
                history[index] = cls.attach_geometry(point, metrics, evidence)
                updated += 1
        if not updated:
            return {
                "status": "unchanged",
                "points": len(history),
                "snapshots_matched": 0,
            }
        with tempfile.TemporaryDirectory(
            prefix="jessie-progress-backfill-"
        ) as directory:
            staged = Path(directory)
            (staged / CSV_NAME).write_bytes(encode_history(history))
            PreviewSeries.draw_review_chart(history, review, staged / PNG_NAME)
            with Image.open(staged / PNG_NAME) as image:
                image.verify()
            publication.publish(staged)
        return {
            "status": "backfilled",
            "points": len(history),
            "rows_updated": updated,
            "snapshots_matched": len(measured),
            "unmatched_hashes": len(wanted),
            "table": str(review / CSV_NAME),
            "plot": str(review / PNG_NAME),
        }

    @classmethod
    def record(
        cls,
        project: Path,
        review: Path,
        kicad_python: Path,
        label: str,
        *,
        run_metrics: RunMetrics | None = None,
    ) -> dict:
        """Check the saved board and publish the progress table and chart.

        Nothing is rewritten when the board, its inputs and the counts match
        the last recorded point and the chart is intact.

        Inputs:
            project: Native project folder.
            review: The project's review folder.
            kicad_python: KiCad's Python, used to find ``kicad-cli``.
            label: Short description of the saved board; must not be blank.
            run_metrics: Optimization measurements bound to this saved board.
        Outputs:
            The result record, whose ``status`` is ``unchanged``, ``recorded``,
            ``metrics_added`` or ``chart_restored``. Raises ValueError for a
            review folder that is not this project's, a blank label or
            mismatched run metrics, and RuntimeError when the saved inputs
            change before publication.
        """
        project, review = project.resolve(), review.resolve()
        if (
            not review.is_dir()
            or review != (project / "deliverables" / "review").resolve()
        ):
            raise ValueError(
                "Progress destination must be this project's review folder"
            )
        if not label.strip():
            raise ValueError("A short progress label is required")
        publication = ReviewSnapshot.capture(review, {CSV_NAME, PNG_NAME})
        history = read_history(review / CSV_NAME)
        point, fingerprints = check_saved_board(
            project, kicad_python.resolve(), label.strip()
        )
        if run_metrics is not None:
            if not isinstance(run_metrics, RunMetrics):
                raise ValueError("Run metrics must be a RunMetrics record")
            point = cls.attach_run_metrics(
                point,
                run_metrics.pcb_sha256,
                run_metrics.source_signature,
                run_metrics.symmetry_score,
                run_metrics.routing_run_seconds,
                run_metrics.evidence,
            )
        same = bool(
            history
            and history[-1].pcb_sha256 == point.pcb_sha256
            and (history[-1].source_signature == point.source_signature)
            and (
                history[-1].open_connections,
                history[-1].other_errors,
                history[-1].other_warnings,
                history[-1].schematic_parity,
            )
            == (
                point.open_connections,
                point.other_errors,
                point.other_warnings,
                point.schematic_parity,
            )
        )
        metrics_added = False
        if same:
            enriched = history[-1]
            if point.track_length_mm is not None:
                enriched = cls.attach_geometry(
                    enriched,
                    GeometryMetrics(
                        point.track_length_mm, point.via_count, point.degree_two_joints
                    ),
                    point.geometry_evidence,
                )
            if run_metrics is not None:
                enriched = cls.attach_run_metrics(
                    enriched,
                    run_metrics.pcb_sha256,
                    run_metrics.source_signature,
                    run_metrics.symmetry_score,
                    run_metrics.routing_run_seconds,
                    run_metrics.evidence,
                )
            metrics_added = enriched != history[-1]
            history[-1] = enriched
        if same and not metrics_added and ProgressChart.is_valid(review / PNG_NAME):
            return {
                "status": "unchanged",
                "open_connections": point.open_connections,
                "other_errors": point.other_errors,
                "other_warnings": point.other_warnings,
                "track_length_mm": point.track_length_mm,
                "via_count": point.via_count,
                "degree_two_joints": point.degree_two_joints,
                "symmetry_score": point.symmetry_score,
                "routing_run_seconds": point.routing_run_seconds,
                "pcb_sha256": point.pcb_sha256,
                "points": len(history),
                "plot": str(review / PNG_NAME),
                "table": str(review / CSV_NAME),
            }
        if not same:
            history.append(point)
        with tempfile.TemporaryDirectory(prefix="jessie-progress-output-") as directory:
            staged = Path(directory)
            (staged / CSV_NAME).write_bytes(encode_history(history))
            PreviewSeries.draw_review_chart(history, review, staged / PNG_NAME)
            with Image.open(staged / PNG_NAME) as generated:
                generated.verify()
            _, after, _ = _fingerprint(project)
            if after != fingerprints:
                raise RuntimeError("Saved design inputs changed before publication")
            publication.publish(staged)
        return {
            "status": "recorded"
            if not same
            else "metrics_added"
            if metrics_added
            else "chart_restored",
            "open_connections": point.open_connections,
            "other_errors": point.other_errors,
            "other_warnings": point.other_warnings,
            "schematic_parity": point.schematic_parity,
            "track_length_mm": point.track_length_mm,
            "via_count": point.via_count,
            "degree_two_joints": point.degree_two_joints,
            "symmetry_score": point.symmetry_score,
            "routing_run_seconds": point.routing_run_seconds,
            "pcb_sha256": point.pcb_sha256,
            "points": len(history),
            "table": str(review / CSV_NAME),
            "plot": str(review / PNG_NAME),
        }


class RoutingProgressCommand:
    """Command-line entry: record, redraw, backfill or preview the routing progress."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Parse the command line and run one progress action.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 on success, 2 after reporting an operating-system,
            value, runtime or subprocess error.
        """
        from core.console import Console, add_output_arguments
        from review.presentation import ReviewDisplay

        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--project", type=Path, required=True)
        parser.add_argument("--review", type=Path, required=True)
        parser.add_argument("--kicad-python", type=Path, required=True)
        parser.add_argument("--label", default="Saved board check")
        parser.add_argument(
            "--preview-run",
            help="revalidate and plot a retained private autorouter run",
        )
        parser.add_argument(
            "--redraw",
            action="store_true",
            help="redraw existing history without checking the board",
        )
        parser.add_argument(
            "--backfill-root",
            type=Path,
            help="read exact-hash PCB snapshots one directory below this root",
        )
        add_output_arguments(parser)
        args = parser.parse_args(argv)
        console = Console(json_mode=args.json, verbose=args.verbose)
        try:
            if (
                sum(
                    bool(x) for x in (args.redraw, args.backfill_root, args.preview_run)
                )
                > 1
            ):
                raise ValueError(
                    "Choose only one of --redraw, --backfill-root and --preview-run"
                )
            label = (
                "Redrawing recorded progress"
                if args.redraw
                else "Checking historical progress sources"
                if args.backfill_root
                else "Checking saved-board connectivity, DRC and routing measurements"
            )
            with console.activity(label):
                result = (
                    PreviewSeries.refresh_preview(args.project, args.preview_run)
                    if args.preview_run
                    else ProgressRecorder.redraw(args.review)
                    if args.redraw
                    else ProgressRecorder.backfill(
                        args.project, args.review, args.backfill_root
                    )
                    if args.backfill_root
                    else ProgressRecorder.record(
                        args.project, args.review, args.kicad_python, args.label
                    )
                )
            console.result(result, ReviewDisplay.progress)
            return 0
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            console.error(error)
            return 2


main = RoutingProgressCommand()

if __name__ == "__main__":
    raise SystemExit(main())
