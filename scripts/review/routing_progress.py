"""Record and plot KiCad checks and routing geometry from saved snapshots.

The recorder runs full KiCad DRC on a temporary copy and only publishes when
all checked sources remain unchanged. It never saves or reloads an editor.
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
FIELDS = PREVIOUS_FIELDS + ("symmetry_score", "routing_run_seconds", "optimization_evidence")


@dataclass(frozen=True, slots=True)
class ProgressPoint:
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
        return {
            field: "" if getattr(self, field) is None else str(getattr(self, field))
            for field in FIELDS
        }


@dataclass(frozen=True, slots=True)
class RunMetrics:
    pcb_sha256: str
    source_signature: str
    symmetry_score: float | None
    routing_run_seconds: float | None
    evidence: str


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _fingerprint(project: Path) -> tuple[list[Path], dict[str, str], str]:
    project = long_path(project)
    board = native_path(project, ".kicad_pcb")
    schematic = native_path(project, ".kicad_sch")
    if not board.is_file() or not schematic.is_file():
        raise ValueError("The saved v4.1 PCB and schematic are required")
    files = sorted({*_inputs(board), schematic})
    hashes = {path.relative_to(project).as_posix(): _sha(path) for path in files}
    signature = hashlib.sha256(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return files, hashes, signature


def read_history(path: Path) -> list[ProgressPoint]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames not in (list(LEGACY_FIELDS), list(PREVIOUS_FIELDS), list(FIELDS)):
            raise ValueError("Progress table has unexpected columns")
        points = []
        for row in reader:
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
                len(signature) != 64
                or any(c not in "0123456789abcdef" for c in signature)
            ):
                raise ValueError("Progress source signature is invalid")
            geometry = [row.get(field, "") for field in ("track_length_mm", "via_count", "degree_two_joints")]
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
            optimization = [row.get(field, "") for field in ("symmetry_score", "routing_run_seconds")]
            symmetry, runtime = (float(value) if value else None for value in optimization)
            if any(value is not None and (not math.isfinite(value) or value < 0) for value in (symmetry, runtime)):
                raise ValueError("Optimization measurements must be finite and nonnegative")
            optimization_evidence = row.get("optimization_evidence", "")
            if any(value is not None for value in (symmetry, runtime)) and not optimization_evidence.strip():
                raise ValueError("Optimization measurements require source-bound evidence")
            points.append(
                ProgressPoint(
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
            )
    if any(
        datetime.fromisoformat(a.checked_at) > datetime.fromisoformat(b.checked_at)
        for a, b in zip(points, points[1:])
    ):
        raise ValueError("Progress timestamps must be chronological")
    return points


def encode_history(points: list[ProgressPoint]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(point.as_row() for point in points)
    return output.getvalue().encode("utf-8")


def summarize_report(data: dict) -> tuple[int, int, int, int]:
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
    if not isinstance(severities, list) or not {"error", "warning"} <= set(severities):
        raise ValueError("KiCad did not include both errors and warnings")
    other_errors = sum(item.get("severity") == "error" for item in data["violations"])
    other_warnings = sum(
        item.get("severity") == "warning" for item in data["violations"]
    )
    return (
        len(data["unconnected_items"]),
        other_errors,
        other_warnings,
        len(data["schematic_parity"]),
    )


def check_saved_board(
    project: Path, kicad_python: Path, label: str
) -> tuple[ProgressPoint, dict[str, str]]:
    project = long_path(project)
    files, before, signature = _fingerprint(project)
    board = native_path(project, ".kicad_pcb")
    cli = kicad_python.parent / (
        "kicad-cli.exe" if sys.platform == "win32" else "kicad-cli"
    )
    if not cli.is_file():
        raise ValueError("KiCad CLI was not found beside KiCad Python")
    with tempfile.TemporaryDirectory(prefix="jessie-progress-drc-") as directory:
        scratch = Path(directory)
        _copy_inputs(
            [path for path in files if path.suffix != ".kicad_sch"], board, scratch
        )
        shutil.copy2(native_path(project, ".kicad_sch"), native_path(scratch, ".kicad_sch"))
        copied = {name: _sha(scratch / name) for name in before}
        if copied != before:
            raise RuntimeError("Inputs changed while copying the saved board")
        output = scratch / "drc.json"
        report = _drc(cli, scratch / board.name, output, schematic_parity=True)
        counts = summarize_report(report)
        copied_board = scratch / board.name
        if _sha(copied_board) != before[board.name]:
            raise RuntimeError("The copied PCB changed during DRC")
        geometry = measure_board(pcbnew.LoadBoard(str(copied_board)))
        if _sha(copied_board) != before[board.name]:
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


def _font(
    size: int, bold: bool = False
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
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


def chart_series(points: list[ProgressPoint]) -> tuple:
    """Return the available metrics, with distinct styles and named units."""
    definitions = (
        ("Open connections", "open_connections", "#087f8c", None, "circle", "count"),
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
        ("Symmetry error", "symmetry_score", "#1267ac", (7, 4), "triangle", "score"),
        ("Run time", "routing_run_seconds", "#a84839", (4, 3), "circle", "seconds"),
    )
    result = []
    for label, field, color, pattern, marker, axis in definitions:
        values = [getattr(point, field) for point in points]
        if any(value is not None for value in values):
            result.append((label, field, color, pattern, marker, axis, values))
    return tuple(result)


def latest_scope_change(points: list[ProgressPoint], preview_start: int | None):
    """Find the latest saved design change that alters the routing scope."""
    end = len(points) if preview_start is None else min(preview_start, len(points))
    for index in range(end - 1, -1, -1):
        description = points[index].change.strip()
        marker = description.casefold().find("; scope change")
        if marker >= 0:
            return index, description[:marker].strip() or "Design scope changed"
    return None


def latest_preview_pace(points: list[ProgressPoint], preview_start: int | None):
    """Measure the latest open reduction against elapsed wall time."""
    if preview_start is None or preview_start < 0 or preview_start >= len(points) - 1:
        return None
    latest = points[-1]
    for previous in reversed(points[preview_start:-1]):
        gain = previous.open_connections - latest.open_connections
        if gain > 0:
            seconds = (datetime.fromisoformat(latest.checked_at)
                       - datetime.fromisoformat(previous.checked_at)).total_seconds()
            return (gain, seconds, gain * 60 / seconds) if seconds > 0 else None
    first = points[preview_start]
    if all(point.open_connections == latest.open_connections for point in points[preview_start:]):
        seconds = (datetime.fromisoformat(latest.checked_at)
                   - datetime.fromisoformat(first.checked_at)).total_seconds()
        return (0, seconds, 0.0) if seconds > 0 else None
    return None


def latest_recorded_run_pace(points: list[ProgressPoint], preview_start: int | None):
    """Measure gain against recorded routing/checking time, excluding idle time."""
    if preview_start is None or preview_start < 0 or preview_start >= len(points) - 1:
        return None
    latest = points[-1]
    if latest.routing_run_seconds is None or not math.isfinite(latest.routing_run_seconds):
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
    if (all(point.open_connections == latest.open_connections for point in points[preview_start:])
            and first.routing_run_seconds is not None and math.isfinite(first.routing_run_seconds)):
        seconds = latest.routing_run_seconds - first.routing_run_seconds
        return (0, seconds, 0.0) if seconds > 0 else None
    return None


def _series_runs(vertices, break_indices):
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


def draw_chart(points: list[ProgressPoint], output: Path, *, preview_start: int | None = None) -> None:
    """Plot each recorded count on one time axis without redundant annotations."""
    if not points:
        raise ValueError("At least one progress point is required")
    scale = 2
    width, height = 1600, 820
    image = Image.new("RGB", (width * scale, height * scale), "white")
    draw = ImageDraw.Draw(image)

    def xy(value: float) -> int:
        return round(value * scale)

    def text(
        x: float,
        y: float,
        value: str,
        size: int,
        color: str,
        *,
        bold: bool = False,
        anchor: str | None = None,
    ) -> None:
        draw.text(
            (xy(x), xy(y)), value, fill=color, font=_font(xy(size), bold), anchor=anchor
        )

    def line(
        vertices: list[tuple[float, float]],
        color: str,
        pattern: tuple[int, ...] | None,
        thickness: int = 3,
    ) -> None:
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

    def marker(x: float, y: float, color: str, shape: str, radius: float = 4) -> None:
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

    series = chart_series(points)
    text(75, 50, "PCB progress", 34, "#15233b", bold=True)
    if preview_start is not None:
        text(1525, 55, f"Saved: {points[preview_start - 1].open_connections} opens  |  Private preview: {points[-1].open_connections}",
             20, "#34445a", anchor="ra")
        pace = latest_preview_pace(points, preview_start)
        if pace is not None:
            gain, seconds, per_minute = pace
            unit = "open" if gain == 1 else "opens"
            text(1525, 85, f"Elapsed: {gain} {unit} in {seconds / 60:.1f} min ({per_minute:.3g}/min)",
                 16, "#34445a", anchor="ra")
        recorded = latest_recorded_run_pace(points, preview_start)
        if recorded is not None:
            gain, seconds, per_minute = recorded
            unit = "open" if gain == 1 else "opens"
            text(1525, 106, f"Recorded run: {gain} {unit} in {seconds / 60:.1f} min ({per_minute:.3g}/min)",
                 16, "#34445a", anchor="ra")
    extra_axes = [axis for axis in ("mm", "score", "seconds") if any(item[5] == axis for item in series)]
    left, right, top, bottom = (
        135,
        1530 - 75 * len(extra_axes),
        220 if len(series) > 4 else 190,
        680,
    )
    for index, (label, _, color, pattern, shape, _, _) in enumerate(series):
        x = left + (index % 4) * 345
        y = 145 + (index // 4) * 36
        line([(x, y), (x + 42, y)], color, pattern)
        marker(x + 21, y, color, shape)
        text(x + 55, y, label, 18, "#34445a", anchor="lm")

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
        value * magnitude for value in (1, 2, 5, 10) if value * magnitude >= raw_step
    )
    high = max(step, math.ceil(max_value / step) * step)
    for value in range(0, int(high) + 1, int(step)):
        y = bottom - value * (bottom - top) / high
        draw.line((xy(left), xy(y), xy(right), xy(y)), fill="#e4e9ef", width=xy(1))
        text(left - 16, y, str(value), 17, "#64748b", anchor="rm")
    text(left, top - 25, "Count", 16, "#64748b")
    axis_high = {"count": high}
    for axis_index, axis_name in enumerate(extra_axes):
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
            text(x, y, format(value, ".4g"), 17, color, anchor="lm")
        text(x, top - 25, {"mm": "mm", "score": "score", "seconds": "s"}[axis_name], 16, color, anchor="lm")

    times = [datetime.fromisoformat(point.checked_at) for point in points]
    seconds = [value.timestamp() for value in times]
    span = seconds[-1] - seconds[0]
    xs = (
        [left + (right - left) / 2] * len(points)
        if span == 0
        else [left + (stamp - seconds[0]) * (right - left) / span for stamp in seconds]
    )
    if preview_start is not None:
        boundary = max(left, xs[preview_start])
        draw.rectangle((xy(boundary), xy(top), xy(right), xy(bottom)), fill="#eef7fa")
        text(right, bottom + 83, "Triangles: KiCad-checked preview; electrical checks pending; not saved to PCB", 16, "#176780", anchor="ra")
    scope_change = latest_scope_change(points, preview_start)
    if scope_change is not None:
        index, description = scope_change
        x = xs[index]
        line([(x, top), (x, bottom)], "#9baabd", (4, 5), thickness=1)
        anchor = "ra" if x > (left + right) / 2 else "la"
        text(x, bottom + 58, f"Scope change: {description}", 15, "#53657a", anchor=anchor)
    for _, _, color, pattern, shape, axis, values in series:
        limit = axis_high[axis]
        vertices = [
            (x, None if value is None else bottom - value * (bottom - top) / limit)
            for x, value in zip(xs, values)
        ]
        saved_end = len(points) if preview_start is None else preview_start
        boundaries = {
            index for index, point in enumerate(points[:saved_end])
            if "; scope change" in point.change.casefold()
        }
        if preview_start is not None:
            boundaries.add(preview_start)
        for run in _series_runs(vertices, boundaries):
            line(run, color, pattern)
        valid = [index for index, (_, y) in enumerate(vertices) if y is not None]
        stride = max(1, len(valid) // 12)
        selected = set(valid[::stride]) | ({valid[-1]} if valid else set())
        for index in sorted(selected):
            x, y = vertices[index]
            marker(x, y, color, "triangle" if preview_start is not None and index >= preview_start else shape)

    tick_count = 5 if span > 0 else 1
    for index in range(tick_count):
        fraction = index / (tick_count - 1) if tick_count > 1 else 0.5
        stamp = seconds[0] + fraction * span
        display_zone = times[preview_start - 1].tzinfo if preview_start is not None else times[-1].tzinfo
        label = datetime.fromtimestamp(stamp, display_zone).strftime(
            "%b %d  %I:%M %p"
        )
        x = left + fraction * (right - left)
        draw.line(
            (xy(x), xy(bottom), xy(x), xy(bottom + 7)), fill="#a5b1c1", width=xy(1)
        )
        text(x, bottom + 20, label, 16, "#64748b", anchor="ma")
    image.resize((width, height), Image.Resampling.LANCZOS).save(
        output, format="PNG", optimize=True
    )


def _valid_chart(path: Path) -> bool:
    try:
        with Image.open(path) as picture:
            if picture.format != "PNG":
                return False
            picture.verify()
        return True
    except (OSError, ValueError):
        return False


def review_points(history: list[ProgressPoint], review: Path) -> tuple[list[ProgressPoint], int | None]:
    """Overlay only source-matching private measurements; never rewrite saved history."""
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
        actual = _sha(source) if source.is_file() else "absent"
        if actual != expected:
            return history, None  # An older preview is not current-board progress.
    if data["inputs"].get(native_path(project, ".kicad_pcb").name) != history[-1].pcb_sha256:
        return history, None
    points = [ProgressPoint(**raw) for raw in data.get("points", [])]
    for point in points:
        if (datetime.fromisoformat(point.checked_at).tzinfo is None
                or any(isinstance(v, bool) or not isinstance(v, int) or v < 0
                       for v in (point.open_connections, point.other_errors, point.other_warnings))
                or len(point.pcb_sha256) != 64):
            raise ValueError("Invalid private progress measurement")
    cutoff = datetime.fromisoformat(history[-1].checked_at)
    points = [p for p in points if datetime.fromisoformat(p.checked_at) >= cutoff]
    if any(datetime.fromisoformat(a.checked_at) > datetime.fromisoformat(b.checked_at)
           for a, b in zip(points, points[1:])):
        raise ValueError("Private progress timestamps are out of order")
    return (history + points, len(history)) if points else (history, None)


def draw_review_chart(history: list[ProgressPoint], review: Path, output: Path) -> None:
    points, first = review_points(history, review)
    draw_chart(points, output, preview_start=first)


def publish_preview(project: Path, inputs: dict, run_id: str, samples: list[dict]) -> None:
    """Publish already validated candidate measurements as a distinct preview series."""
    review = project / "deliverables" / "review"
    if not samples or not (review / CSV_NAME).is_file():
        return
    publication = ReviewSnapshot.capture(review, {PNG_NAME, PREVIEW_NAME})
    saved_history = (review / CSV_NAME).read_bytes()
    history = read_history(review / CSV_NAME)
    payload = {"schema": 1, "run_id": run_id, "inputs": inputs, "points": samples}
    with tempfile.TemporaryDirectory(prefix="autopcb-preview-progress-") as directory:
        staged = Path(directory)
        (staged / PREVIEW_NAME).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        # Validate the native source identity immediately before publication.
        for name, expected in inputs.items():
            source = (project / name).resolve()
            if not source.is_relative_to(project) or (_sha(source) if source.is_file() else "absent") != expected:
                raise RuntimeError("Routing inputs changed before progress publication")
        points = [ProgressPoint(**raw) for raw in samples]
        draw_chart(history + points, staged / PNG_NAME, preview_start=len(history))
        with Image.open(staged / PNG_NAME) as picture:
            picture.verify()
        for name, expected in inputs.items():
            source = project / name
            if (_sha(source) if source.is_file() else "absent") != expected:
                raise RuntimeError("Routing inputs changed during progress rendering")
        if (review / CSV_NAME).read_bytes() != saved_history:
            raise RuntimeError("Saved progress changed during preview rendering")
        publication.publish(staged)


def refresh_preview(project: Path, run_id: str) -> dict:
    """Revalidate a retained private checkpoint before showing its measurements."""
    from core.paths import asset_root
    from pcb.net_routing.controller import decode_checkpoint
    from pcb.net_routing.native import Snapshot, sha
    from pcb.net_routing.protection import ProtectedState
    from pcb.net_routing.runner import _run_id, _source_hashes, _progress_sample, verify_resume_board

    assets = asset_root(project)
    path = assets / "results" / "autoroute" / f"{_run_id(run_id)}.json"
    original = path.read_bytes()
    state = json.loads(original)
    with Snapshot(native_path(project, ".kicad_pcb")) as snapshot:
        inputs = _source_hashes(snapshot, project, assets)
        candidate = decode_checkpoint(state["checkpoint"], inputs, snapshot.tool_hashes,
                                      sha(assets / "config" / "autopcb.json"), allow_tool_changes=True)
        schematic = (native_path(snapshot.directory, ".kicad_sch")).read_bytes()
        if ProtectedState.capture(snapshot.original, schematic).differences(candidate, schematic):
            raise ValueError("Preview changes protected design items")
        snapshot.board_path.write_bytes(candidate)
        verified = verify_resume_board(snapshot, project, state["best_preview_opens"])
        _progress_sample(state, sha(snapshot.board_path), verified["drc"], verified["geometry"], state["elapsed_seconds"])
        snapshot.check()
        if path.read_bytes() != original:
            raise RuntimeError("Preview changed during progress verification; retry")
        publish_preview(project, inputs, run_id, state["progress_samples"])
    return {"status": "preview_refreshed", "open_connections": state["saved_board_opens"],
            "preview_opens": state["best_preview_opens"], "points": len(state["progress_samples"]),
            "plot": str(project / "deliverables" / "review" / PNG_NAME)}


def redraw(review: Path) -> dict:
    """Redraw existing recorded data without opening or checking the board."""
    review = review.resolve()
    publication = ReviewSnapshot.capture(review, {CSV_NAME, PNG_NAME})
    if publication.originals[CSV_NAME] is None:
        raise ValueError("No routing progress history is available")
    points = read_history(review / CSV_NAME)
    with tempfile.TemporaryDirectory(prefix="jessie-progress-redraw-") as directory:
        staged = Path(directory)
        (staged / CSV_NAME).write_bytes(publication.originals[CSV_NAME])
        draw_review_chart(points, review, staged / PNG_NAME)
        with Image.open(staged / PNG_NAME) as image:
            image.verify()
        publication.publish(staged)
    return {"status": "redrawn", "points": len(points), "plot": str(review / PNG_NAME)}


def attach_geometry(
    point: ProgressPoint, metrics: GeometryMetrics, evidence: str
) -> ProgressPoint:
    """Fill missing geometry only; never silently rewrite measured history."""
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


def attach_run_metrics(
    point: ProgressPoint,
    pcb_sha256: str,
    source_signature: str,
    symmetry_score: float | None,
    routing_run_seconds: float | None,
    evidence: str,
) -> ProgressPoint:
    """Add measured optimization metrics only to their exact saved-source row."""
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
        value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ) for value in values
    ):
        raise ValueError("Run metrics must be finite and nonnegative")
    old = (point.symmetry_score, point.routing_run_seconds)
    if any(previous is not None and new is not None and previous != new for previous, new in zip(old, values, strict=True)):
        raise ValueError("Run metrics disagree with the recorded history")
    return replace(
        point,
        symmetry_score=old[0] if old[0] is not None else symmetry_score,
        routing_run_seconds=old[1] if old[1] is not None else routing_run_seconds,
        optimization_evidence=point.optimization_evidence or evidence.strip(),
    )


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


def backfill(project: Path, review: Path, snapshots: Path) -> dict:
    """Fill historical geometry from exact-hash PCB snapshots, without routing."""
    project, review, snapshots = (
        project.resolve(),
        review.resolve(),
        snapshots.resolve(),
    )
    if not review.is_dir() or review != (project / "deliverables" / "review").resolve():
        raise ValueError("Progress destination must be this project's review folder")
    if not snapshots.is_dir():
        raise ValueError("Snapshot root must be an existing directory")
    publication = ReviewSnapshot.capture(review, {CSV_NAME, PNG_NAME})
    history = read_history(review / CSV_NAME)
    wanted = {point.pcb_sha256 for point in history if point.track_length_mm is None}
    measured: dict[str, tuple[GeometryMetrics, str]] = {}
    candidates = [native_path(project, ".kicad_pcb"), *snapshots.glob("*/*.kicad_pcb")]
    with summarize_kicad_warnings():
        for path in candidates:
            if not wanted:
                break
            if not path.is_file():
                continue
            digest = _sha(path)
            if digest not in wanted:
                continue
            metrics = measure_board(pcbnew.LoadBoard(str(path)))
            if _sha(path) != digest:
                raise RuntimeError(f"PCB snapshot changed during measurement: {path}")
            measured[digest] = (metrics, f"Exact SHA-256-matched snapshot: {path}")
            wanted.remove(digest)
    updated = 0
    for index, point in enumerate(history):
        if point.track_length_mm is None and point.pcb_sha256 in measured:
            metrics, evidence = measured[point.pcb_sha256]
            history[index] = attach_geometry(point, metrics, evidence)
            updated += 1
    if not updated:
        return {"status": "unchanged", "points": len(history), "snapshots_matched": 0}
    with tempfile.TemporaryDirectory(prefix="jessie-progress-backfill-") as directory:
        staged = Path(directory)
        (staged / CSV_NAME).write_bytes(encode_history(history))
        draw_review_chart(history, review, staged / PNG_NAME)
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


def record(
    project: Path,
    review: Path,
    kicad_python: Path,
    label: str,
    *,
    run_metrics: RunMetrics | None = None,
) -> dict:
    project, review = project.resolve(), review.resolve()
    if not review.is_dir() or review != (project / "deliverables" / "review").resolve():
        raise ValueError("Progress destination must be this project's review folder")
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
        point = attach_run_metrics(point, run_metrics.pcb_sha256, run_metrics.source_signature,
                                   run_metrics.symmetry_score, run_metrics.routing_run_seconds,
                                   run_metrics.evidence)
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
            enriched = attach_geometry(
                enriched,
                GeometryMetrics(point.track_length_mm, point.via_count, point.degree_two_joints),
                point.geometry_evidence,
            )
        if run_metrics is not None:
            enriched = attach_run_metrics(enriched, run_metrics.pcb_sha256, run_metrics.source_signature,
                                          run_metrics.symmetry_score, run_metrics.routing_run_seconds,
                                          run_metrics.evidence)
        metrics_added = enriched != history[-1]
        history[-1] = enriched
    if same and not metrics_added and _valid_chart(review / PNG_NAME):
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
        draw_review_chart(history, review, staged / PNG_NAME)
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


def main(argv: list[str] | None = None) -> int:
    from core.console import Console, add_output_arguments
    from review.presentation import show_progress

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--kicad-python", type=Path, required=True)
    parser.add_argument("--label", default="Saved board check")
    parser.add_argument("--preview-run", help="revalidate and plot a retained private autorouter run")
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
        if sum(bool(x) for x in (args.redraw, args.backfill_root, args.preview_run)) > 1:
            raise ValueError("Choose only one of --redraw, --backfill-root and --preview-run")
        label = ("Redrawing recorded progress" if args.redraw else
                 "Checking historical progress sources" if args.backfill_root else
                 "Checking saved-board connectivity, DRC and routing measurements")
        with console.activity(label):
            result = (
                refresh_preview(args.project, args.preview_run)
                if args.preview_run else redraw(args.review)
                if args.redraw
                else backfill(args.project, args.review, args.backfill_root)
                if args.backfill_root
                else record(args.project, args.review, args.kicad_python, args.label)
            )
        console.result(result, show_progress)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        console.error(error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
