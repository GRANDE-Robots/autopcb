"""Compare fixed placement with permitted placement moves under one search budget.

Each arm routes a private copy of the same saved design with identical rounds,
seconds per batch and memory. A winning preview is applied only to that copy,
then read back through KiCad verification and geometry inspection, so the
verdict never rests on the router's own opens count.
"""

from __future__ import annotations

import math
import shutil

from autopcb import jobs, layout
from autopcb.native import Native
from core.export_helpers import sha
from core.paths import ProjectStorage
from core.project import ProjectPaths, SourceSnapshot

ARMS = ("fixed", "moves")
MOVE_TOLERANCE_MM = 1e-3


def board_metrics(geometry: dict) -> dict:
    """Reduce an inspect geometry record to the numbers the study compares."""
    (x0, y0), (x1, y1) = geometry["bounds"]
    tracks = geometry["tracks"]
    return {
        "board_area_mm2": round(abs(x1 - x0) * abs(y1 - y0), 6),
        "track_length_mm": round(sum(item["length_mm"] for item in tracks if not item["via"]), 6),
        "via_count": sum(1 for item in tracks if item["via"]),
        "poses": {fp["ref"]: (fp["at"][0], fp["at"][1], fp["layer"]) for fp in geometry["footprints"]},
    }


def moved_parts(before: dict, after: dict) -> list[dict]:
    """List parts whose position or side changed between two metric records."""
    moved = []
    for ref, (x, y, layer) in sorted(after["poses"].items()):
        old = before["poses"].get(ref)
        if old is None:
            continue
        shift = math.hypot(x - old[0], y - old[1])
        if shift > MOVE_TOLERANCE_MM or layer != old[2]:
            moved.append({"ref": ref, "shift_mm": round(shift, 6), "side_changed": layer != old[2]})
    return moved


def arm_record(name: str, original: dict, final: dict | None, *, preview: dict, verification: dict | None,
               applied: bool, wall_seconds: float) -> dict:
    """Describe one arm. `final` is None when nothing was applied."""
    record = {"arm": name, "status": preview.get("status"), "run_id": preview.get("run_id"),
              "preview_opens": preview.get("best_preview_opens"), "applied": applied,
              "wall_seconds": round(wall_seconds, 3)}
    measured = final or original
    record.update({key: measured[key] for key in ("board_area_mm2", "track_length_mm", "via_count")})
    record["moved_parts"] = moved_parts(original, final) if final else []
    if verification is None:
        record.update(verified_opens=None, native_passed=False)
    else:
        native = verification["native"]
        record.update(verified_opens=native["open_connections"], native_passed=bool(native["passed"]))
    return record


def _passes(arm: dict) -> bool:
    return bool(arm["applied"] and arm["verified_opens"] == 0 and arm["native_passed"])


def decide(fixed: dict, moves: dict, original: dict) -> dict:
    """Choose the smallest fully routed verified board and measure what moving cost."""
    passing = [arm for arm in (fixed, moves) if _passes(arm)]
    keep = min(passing, key=lambda arm: (arm["board_area_mm2"], len(arm["moved_parts"]),
                                         arm["track_length_mm"], arm["via_count"]),
               default=None)
    stalled = not _passes(fixed) and (fixed["verified_opens"] or fixed["preview_opens"] or 0) > 0

    def delta(arm: dict, base: dict) -> dict:
        return {"track_length_mm": round(arm["track_length_mm"] - base["track_length_mm"], 6),
                "via_count": arm["via_count"] - base["via_count"], "moved_parts": len(arm["moved_parts"])}

    return {
        "kept": keep["arm"] if keep else None,
        "fixed_stalled": stalled,
        "moves_reached_zero_opens": _passes(moves),
        "criterion_met": stalled and _passes(moves),
        # Copper added relative to the saved board, whichever arm completed.
        "moves_added_vs_original": delta(moves, original) if _passes(moves) else None,
        # A direct comparison needs both arms to have finished routing.
        "moves_vs_fixed": delta(moves, fixed) if _passes(fixed) and _passes(moves) else None,
    }


def _copy_evidence(project: ProjectPaths, storage: ProjectStorage, working: ProjectPaths) -> dict:
    caller = storage.legacy.relative_to(project.root)
    sources = {caller / "config/autopcb.json": storage.config_file("autopcb.json"),
               **{caller / "design" / name: storage.data_file(name)
                  for name in ("POWER_LOAD_BUDGET.csv", "STACKUP_CURRENT.json")}}
    if not sources[caller / "config/autopcb.json"].is_file():
        raise ValueError("Create and review the project routing policy before the placement study.")
    copied = {}
    for name, source in sources.items():
        if source.is_file():
            target = working.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied[name] = (source, sha(source))
    return copied


def run_arm(native: Native, project: ProjectPaths, snapshot: SourceSnapshot, storage: ProjectStorage,
            name: str, *, rounds: int, seconds: int, memory_gib: float, original: dict | None = None) -> dict:
    import time

    with jobs.isolated(project, snapshot) as working:
        _copy_evidence(project, storage, working)
        inner = ProjectStorage(working.root)
        before = original or board_metrics(jobs.inspect(native, working)["geometry"])
        start = time.perf_counter()
        preview = layout.preview(native, working, rounds=rounds, seconds=seconds, memory_gib=memory_gib,
                                 allow_placement=name == "moves", storage=inner)
        applied = False
        if isinstance(preview.get("run_id"), str) and preview.get("best_preview_opens") is not None:
            try:
                layout.apply(native, working, preview["run_id"], storage=inner)
                applied = True
            except RuntimeError as error:
                preview = {**preview, "apply_error": str(error)}
        wall = time.perf_counter() - start
        final = verification = None
        if applied:
            final = board_metrics(jobs.inspect(native, working)["geometry"])
            verification = jobs.verify(native, working, storage=inner)
    record = arm_record(name, before, final, preview=preview, verification=verification,
                        applied=applied, wall_seconds=wall)
    if "apply_error" in preview:
        record["apply_error"] = preview["apply_error"]
    return record


def placement_study(native: Native, project: ProjectPaths, *, rounds: int, seconds: int, memory_gib: float,
                    storage: ProjectStorage | None = None) -> dict:
    if not 1 <= rounds <= 500 or not 1 <= seconds <= 1800:
        raise ValueError("Study rounds or seconds are outside the supported range")
    storage = storage or ProjectStorage(project.root)
    snapshot = SourceSnapshot.capture(project)
    original = board_metrics(jobs.inspect(native, project)["geometry"])
    arms = {name: run_arm(native, project, snapshot, storage, name, rounds=rounds, seconds=seconds,
                          memory_gib=memory_gib, original=original) for name in ARMS}
    snapshot.verify_unchanged()
    return {"schema_version": 1, "kind": "placement_study",
            "same_input_sha256": snapshot.hashes,
            "budget": {"rounds": rounds, "seconds_per_batch": seconds, "memory_gib": memory_gib},
            "original": {key: original[key] for key in ("board_area_mm2", "track_length_mm", "via_count")},
            "arms": arms, "verdict": decide(arms["fixed"], arms["moves"], original),
            "toolchain": native.status()}
