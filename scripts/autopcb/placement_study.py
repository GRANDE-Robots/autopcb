"""placement_study.py: compare fixed placement with permitted moves under one search budget.

Each arm routes a private copy of the board, applies the result there and reads
it back through KiCad. The verdict keeps the smallest verified board and counts
what moving parts cost. Lengths are mm and areas are mm squared.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field

from autopcb import jobs
from autopcb.layout import Budget, Layout
from autopcb.native import Native
from autopcb.private_copy import PrivateCopy
from core.paths import ProjectStorage
from core.project import ProjectPaths

MOVE_TOLERANCE_MM = 1e-3


@dataclass(frozen=True)
class BoardMetrics:
    """Board area, track length, via count and part poses read from inspected geometry."""

    area_mm2: float
    track_length_mm: float
    via_count: int
    poses: dict = field(default_factory=dict)

    @classmethod
    def from_geometry(cls, geometry: dict) -> BoardMetrics:
        """Measure the geometry that ``inspect`` reports.

        Inputs:
            geometry: Inspect geometry with bounds, tracks and footprints.
        Outputs:
            The area of the bounds, the length of the tracks that are not vias, the via
            count, and the (x, y, layer) of every footprint by reference.
        """
        (x0, y0), (x1, y1) = geometry["bounds"]
        tracks = geometry["tracks"]
        return cls(
            round(abs(x1 - x0) * abs(y1 - y0), 6),
            round(sum(t["length_mm"] for t in tracks if not t["via"]), 6),
            sum(1 for t in tracks if t["via"]),
            {
                f["ref"]: (f["at"][0], f["at"][1], f["layer"])
                for f in geometry["footprints"]
            },
        )

    def moved_from(self, before: BoardMetrics) -> list[dict]:
        """Find the parts that moved or changed side since an earlier measurement.

        Inputs:
            before: Metrics of the earlier board.
        Outputs:
            A record of reference, shift in mm and side change for every part whose shift
            exceeds MOVE_TOLERANCE_MM or whose layer changed, sorted by reference. Parts
            missing from the earlier board are skipped.
        """
        moved = []
        for ref, (x, y, layer) in sorted(self.poses.items()):
            old = before.poses.get(ref)
            if old is None:
                continue
            shift = math.hypot(x - old[0], y - old[1])
            if shift > MOVE_TOLERANCE_MM or layer != old[2]:
                moved.append(
                    {
                        "ref": ref,
                        "shift_mm": round(shift, 6),
                        "side_changed": layer != old[2],
                    }
                )
        return moved

    def as_dict(self) -> dict:
        """The area, track length and via count as report fields, without poses."""
        return {
            "board_area_mm2": self.area_mm2,
            "track_length_mm": self.track_length_mm,
            "via_count": self.via_count,
        }


@dataclass(frozen=True)
class ArmResult:
    """What one arm of the study produced, and whether its saved board passed verification."""

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
        """Whether the preview was applied and verification found zero opens and no findings."""
        return self.applied and self.verified_opens == 0 and self.native_passed

    @property
    def rank(self) -> tuple:
        """Sort key where smaller is better: area, moved part count, track length, vias."""
        return (
            self.metrics.area_mm2,
            len(self.moved_parts),
            self.metrics.track_length_mm,
            self.metrics.via_count,
        )

    def added_to(self, base: BoardMetrics) -> dict:
        """What this arm added to a base board.

        Inputs:
            base: Metrics of the board to compare with.
        Outputs:
            The extra track length in mm, the extra vias and the moved part count.
        """
        return {
            "track_length_mm": round(
                self.metrics.track_length_mm - base.track_length_mm, 6
            ),
            "via_count": self.metrics.via_count - base.via_count,
            "moved_parts": len(self.moved_parts),
        }

    def as_dict(self) -> dict:
        """The arm as a report record, with its metrics but not its part poses."""
        return {
            "arm": self.name,
            "status": self.preview.get("status"),
            "run_id": self.preview.get("run_id"),
            "preview_opens": self.preview.get("best_preview_opens"),
            "applied": self.applied,
            "apply_error": self.preview.get("apply_error"),
            "verified_opens": self.verified_opens,
            "native_passed": self.native_passed,
            "wall_seconds": round(self.wall_seconds, 3),
            "moved_parts": self.moved_parts,
            **self.metrics.as_dict(),
        }


class Verdict:
    """Keep the smallest verified board and measure what moving cost."""

    def __init__(self, fixed: ArmResult, moves: ArmResult, original: BoardMetrics):
        """Inputs:
        fixed: Result of the arm that kept placement.
        moves: Result of the arm that could move parts.
        original: Metrics of the board before routing.
        """
        self.fixed, self.moves, self.original = fixed, moves, original

    @property
    def fixed_stalled(self) -> bool:
        """Use the verified open count when available, falling back to the preview."""
        opens = self.fixed.verified_opens
        if opens is None:
            opens = self.fixed.preview.get("best_preview_opens")
        return opens is not None and opens > 0

    @property
    def kept(self) -> ArmResult | None:
        """The passing arm with the smallest rank, or None when neither arm passes."""
        return min(
            (arm for arm in (self.fixed, self.moves) if arm.passes),
            key=lambda a: a.rank,
            default=None,
        )

    def as_dict(self) -> dict:
        """The verdict as a report record.

        Outputs:
            The kept arm, whether the fixed arm stalled, whether the moving arm reached
            zero opens, whether both held (``criterion_met``), and what moving cost against
            the original and the fixed result when those arms passed.
        """
        kept = self.kept
        both = self.fixed.passes and self.moves.passes
        return {
            "kept": kept.name if kept else None,
            "fixed_stalled": self.fixed_stalled,
            "moves_reached_zero_opens": self.moves.passes,
            "criterion_met": self.fixed_stalled and self.moves.passes,
            "moves_added_vs_original": self.moves.added_to(self.original)
            if self.moves.passes
            else None,
            "moves_vs_fixed": self.moves.added_to(self.fixed.metrics) if both else None,
        }


class StudyArm:
    """Route one private copy, apply the result there, then read it back through KiCad."""

    def __init__(
        self,
        native: Native,
        copy: PrivateCopy,
        budget: Budget,
        *,
        allow_placement: bool,
    ):
        """Inputs:
        native: KiCad toolchain.
        copy: Private copy of the project to route.
        budget: Search budget.
        allow_placement: Let the router move parts. The arm is named ``moves`` then,
            and ``fixed`` otherwise.
        """
        self.native, self.copy, self.budget, self.allow_placement = (
            native,
            copy,
            budget,
            allow_placement,
        )
        self.name = "moves" if allow_placement else "fixed"

    def measure(self, working: ProjectPaths) -> BoardMetrics:
        """Inspect a working copy and measure its board."""
        return BoardMetrics.from_geometry(
            jobs.inspect(self.native, working)["geometry"]
        )

    def __call__(self, original: BoardMetrics) -> ArmResult:
        """Route a private copy, apply the best preview there and verify it.

        Inputs:
            original: Metrics of the board before routing.
        Outputs:
            The arm's result. A preview without a run ID or open count is not applied, and
            a refused apply is recorded as ``apply_error``. Neither is verified.
        """
        with self.copy.open() as (working, inner):
            start = time.perf_counter()
            routing = Layout(self.native, working, inner)
            preview = routing.preview(
                rounds=self.budget.rounds,
                seconds=self.budget.seconds,
                memory_gib=self.budget.memory_gib,
                allow_placement=self.allow_placement,
            )
            applied = (
                isinstance(preview.get("run_id"), str)
                and preview.get("best_preview_opens") is not None
            )
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
        return ArmResult(
            self.name,
            preview,
            metrics,
            metrics.moved_from(original) if applied else [],
            applied,
            opens,
            passed,
            wall,
        )


class PlacementStudy:
    """Run the fixed and the moving arm on private copies of one project."""

    def __init__(
        self,
        native: Native,
        project: ProjectPaths,
        budget: Budget,
        storage: ProjectStorage | None = None,
    ):
        """Inputs:
        native: KiCad toolchain.
        project: Project to study.
        budget: Search budget shared by both arms.
        storage: Storage holding the reviewed inputs; the project's own when None.
        """
        self.native, self.project, self.budget = native, project, budget
        self.storage = storage or ProjectStorage(project.root)

    def run(self) -> dict:
        """Run both arms from the same inputs.

        Outputs:
            A placement_study report with the input hashes, the budget, the original
            metrics, both arms, the verdict and the toolchain.
        """
        copy = PrivateCopy(self.project, self.storage)
        original = BoardMetrics.from_geometry(
            jobs.inspect(self.native, self.project)["geometry"]
        )
        fixed, moves = (
            StudyArm(self.native, copy, self.budget, allow_placement=allow)(original)
            for allow in (False, True)
        )
        return {
            "schema_version": 1,
            "kind": "placement_study",
            "status": "measured",
            "same_input_sha256": copy.fingerprint,
            "budget": self.budget.as_dict(),
            "original": original.as_dict(),
            "arms": {"fixed": fixed.as_dict(), "moves": moves.as_dict()},
            "verdict": Verdict(fixed, moves, original).as_dict(),
            "toolchain": self.native.status(),
        }


class VariantStudy:
    """Run the study on seeded crowded packings of one board, then count the outcomes.

    Each seed packs the parts the policy lets move toward a random point
    until their courtyards touch. The routability proof says whether a net
    is physically cut off as placed or only crowded.
    """

    def __init__(
        self,
        native: Native,
        project: ProjectPaths,
        budget: Budget,
        seeds: list[int],
        storage: ProjectStorage | None = None,
    ):
        """Inputs:
        native: KiCad toolchain.
        project: Project to study.
        budget: Search budget used for every study.
        seeds: Seed of each crowded packing.
        storage: Storage holding the reviewed inputs; the project's own when None.
        """
        self.native, self.project, self.budget, self.seeds = (
            native,
            project,
            budget,
            seeds,
        )
        self.storage = storage or ProjectStorage(project.root)

    def variant(self, seed: int) -> dict:
        """Crowd a private copy with one seed and study it.

        Inputs:
            seed: Packing seed.
        Outputs:
            The seed, the packing record and the placement study of the packed copy.
        """
        with PrivateCopy(self.project, self.storage).open() as (working, inner):
            policy = inner.config_file("autopcb.json")
            request, record = (
                working.root / "crowd-request.json",
                working.root / "crowd.json",
            )
            jobs.write_json(
                request,
                {
                    "seed": seed,
                    "policy": json.loads(policy.read_text(encoding="utf-8-sig"))
                    if policy.is_file()
                    else {},
                },
            )
            self.native.worker("crowd", working.board, record, request, output=record)
            packing = json.loads(record.read_text(encoding="utf-8"))
            request.unlink()
            record.unlink()
            study = PlacementStudy(self.native, working, self.budget, inner).run()
        return {"seed": seed, "packing": packing, "study": study}

    def run(self) -> dict:
        """Study every seed and count the outcomes.

        Outputs:
            A placement_variant_study report with the budget, the seeds, counts of
            variants, boards unroutable as placed, stalled fixed arms, moving arms that
            reached zero opens and met criteria, each variant and the toolchain.
        """
        variants = [self.variant(seed) for seed in self.seeds]
        verdicts = [item["study"]["verdict"] for item in variants]
        return {
            "schema_version": 1,
            "kind": "placement_variant_study",
            "status": "measured",
            "budget": self.budget.as_dict(),
            "seeds": self.seeds,
            "summary": {
                "variants": len(variants),
                "unroutable_as_placed": sum(
                    bool(item["packing"]["routability"]["unroutable"])
                    for item in variants
                ),
                "fixed_stalled": sum(v["fixed_stalled"] for v in verdicts),
                "moves_reached_zero_opens": sum(
                    v["moves_reached_zero_opens"] for v in verdicts
                ),
                "criterion_met": sum(v["criterion_met"] for v in verdicts),
            },
            "variants": variants,
            "toolchain": self.native.status(),
        }
