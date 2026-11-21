"""Compare fixed placement with permitted moves under one search budget."""

from __future__ import annotations

import math
import shutil
import time
from dataclasses import dataclass, field

from autopcb import jobs
from autopcb.layout import Layout
from autopcb.native import Native
from core.paths import ProjectStorage
from core.project import ProjectPaths, SourceSnapshot

MOVE_TOLERANCE_MM = 1e-3


@dataclass(frozen=True)
class Budget:
    rounds: int
    seconds: int
    memory_gib: float

    def as_dict(self) -> dict:
        return {"rounds": self.rounds, "seconds_per_batch": self.seconds, "memory_gib": self.memory_gib}


@dataclass(frozen=True)
class BoardMetrics:
    area_mm2: float
    track_length_mm: float
    via_count: int
    poses: dict = field(default_factory=dict)

    @classmethod
    def from_geometry(cls, geometry: dict) -> BoardMetrics:
        (x0, y0), (x1, y1) = geometry["bounds"]
        tracks = geometry["tracks"]
        return cls(round(abs(x1 - x0) * abs(y1 - y0), 6),
                   round(sum(t["length_mm"] for t in tracks if not t["via"]), 6),
                   sum(1 for t in tracks if t["via"]),
                   {f["ref"]: (f["at"][0], f["at"][1], f["layer"]) for f in geometry["footprints"]})

    def moved_from(self, before: BoardMetrics) -> list[dict]:
        moved = []
        for ref, (x, y, layer) in sorted(self.poses.items()):
            old = before.poses.get(ref)
            if old is None:
                continue
            shift = math.hypot(x - old[0], y - old[1])
            if shift > MOVE_TOLERANCE_MM or layer != old[2]:
                moved.append({"ref": ref, "shift_mm": round(shift, 6), "side_changed": layer != old[2]})
        return moved

    def as_dict(self) -> dict:
        return {"board_area_mm2": self.area_mm2, "track_length_mm": self.track_length_mm,
                "via_count": self.via_count}


@dataclass(frozen=True)
class ArmResult:
    name: str
    preview: dict
    metrics: BoardMetrics
    moved_parts: list
    applied: bool
    verified_opens: int | None
    native_passed: bool
    wall_seconds: float

    @property
    def passes(self) -> bool:
        return self.applied and self.verified_opens == 0 and self.native_passed

    @property
    def rank(self) -> tuple:
        return (self.metrics.area_mm2, len(self.moved_parts), self.metrics.track_length_mm,
                self.metrics.via_count)

    def added_to(self, base: BoardMetrics) -> dict:
        return {"track_length_mm": round(self.metrics.track_length_mm - base.track_length_mm, 6),
                "via_count": self.metrics.via_count - base.via_count, "moved_parts": len(self.moved_parts)}

    def as_dict(self) -> dict:
        return {"arm": self.name, "status": self.preview.get("status"), "run_id": self.preview.get("run_id"),
                "preview_opens": self.preview.get("best_preview_opens"), "applied": self.applied,
                "apply_error": self.preview.get("apply_error"), "verified_opens": self.verified_opens,
                "native_passed": self.native_passed, "wall_seconds": round(self.wall_seconds, 3),
                "moved_parts": self.moved_parts, **self.metrics.as_dict()}


class Verdict:
    """Keep the smallest verified board and measure what moving cost."""

    def __init__(self, fixed: ArmResult, moves: ArmResult, original: BoardMetrics):
        self.fixed, self.moves, self.original = fixed, moves, original

    @property
    def fixed_stalled(self) -> bool:
        opens = self.fixed.verified_opens or self.fixed.preview.get("best_preview_opens") or 0
        return not self.fixed.passes and opens > 0

    @property
    def kept(self) -> ArmResult | None:
        return min((arm for arm in (self.fixed, self.moves) if arm.passes), key=lambda a: a.rank, default=None)

    def as_dict(self) -> dict:
        kept = self.kept
        both = self.fixed.passes and self.moves.passes
        return {"kept": kept.name if kept else None, "fixed_stalled": self.fixed_stalled,
                "moves_reached_zero_opens": self.moves.passes,
                "criterion_met": self.fixed_stalled and self.moves.passes,
                "moves_added_vs_original": self.moves.added_to(self.original) if self.moves.passes else None,
                "moves_vs_fixed": self.moves.added_to(self.fixed.metrics) if both else None}


class StudyArm:
    """Route one private copy, apply the result there, then read it back through KiCad."""

    def __init__(self, native: Native, project: ProjectPaths, snapshot: SourceSnapshot,
                 storage: ProjectStorage, budget: Budget, *, allow_placement: bool):
        self.native, self.project, self.snapshot = native, project, snapshot
        self.storage, self.budget, self.allow_placement = storage, budget, allow_placement
        self.name = "moves" if allow_placement else "fixed"

    def measure(self, working: ProjectPaths) -> BoardMetrics:
        return BoardMetrics.from_geometry(jobs.inspect(self.native, working)["geometry"])

    def copy_policy(self, working: ProjectPaths) -> None:
        caller = self.storage.legacy.relative_to(self.project.root)
        for target, source in {caller / "config/autopcb.json": self.storage.config_file("autopcb.json"),
                               **{caller / "design" / n: self.storage.data_file(n)
                                  for n in ("POWER_LOAD_BUDGET.csv", "STACKUP_CURRENT.json")}}.items():
            if source.is_file():
                (working.root / target).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, working.root / target)

    def __call__(self, original: BoardMetrics) -> ArmResult:
        with jobs.isolated(self.project, self.snapshot) as working:
            self.copy_policy(working)
            inner = ProjectStorage(working.root)
            start = time.perf_counter()
            routing = Layout(self.native, working, inner)
            preview = routing.preview(rounds=self.budget.rounds, seconds=self.budget.seconds,
                                      memory_gib=self.budget.memory_gib,
                                      allow_placement=self.allow_placement)
            applied = isinstance(preview.get("run_id"), str) and preview.get("best_preview_opens") is not None
            if applied:
                try:
                    routing.apply(preview["run_id"])
                except RuntimeError as error:
                    applied, preview = False, {**preview, "apply_error": str(error)}
            wall = time.perf_counter() - start
            metrics, opens, passed = original, None, False
            if applied:
                metrics = self.measure(working)
                native = jobs.verify(self.native, working, storage=inner)["native"]
                opens, passed = native["open_connections"], bool(native["passed"])
        return ArmResult(self.name, preview, metrics, metrics.moved_from(original) if applied else [],
                         applied, opens, passed, wall)


class PlacementStudy:
    def __init__(self, native: Native, project: ProjectPaths, budget: Budget,
                 storage: ProjectStorage | None = None):
        self.native, self.project, self.budget = native, project, budget
        self.storage = storage or ProjectStorage(project.root)

    def run(self) -> dict:
        snapshot = SourceSnapshot.capture(self.project)
        original = BoardMetrics.from_geometry(jobs.inspect(self.native, self.project)["geometry"])
        fixed, moves = (StudyArm(self.native, self.project, snapshot, self.storage, self.budget,
                                 allow_placement=allow)(original) for allow in (False, True))
        return {"schema_version": 1, "kind": "placement_study", "status": "measured", "same_input_sha256": snapshot.hashes,
                "budget": self.budget.as_dict(), "original": original.as_dict(),
                "arms": {"fixed": fixed.as_dict(), "moves": moves.as_dict()},
                "verdict": Verdict(fixed, moves, original).as_dict(), "toolchain": self.native.status()}
