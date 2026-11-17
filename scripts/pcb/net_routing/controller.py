"""Turn a full-board inventory into bounded automated search requests.

This controller prepares search work only. It does not certify an electrical
model or authorize publication of a candidate PCB.
"""

from __future__ import annotations

import base64
import hashlib
import math
import zlib
from copy import deepcopy

from pcb.net_routing.scheduler import WorkItem, schedule


def bounded_step(window: tuple[float, float, float, float], step: float, layers: int, max_cells: int) -> float:
    """Coarsen a bounded search just enough to respect the configured cell cap."""
    if (len(window) != 4 or any(not math.isfinite(value) for value in window)
            or window[0] >= window[2] or window[1] >= window[3]
            or not math.isfinite(step) or step <= 0 or layers < 1 or max_cells < layers * 4):
        raise ValueError("Invalid grid bounds or cell budget")
    dx, dy = window[2] - window[0], window[3] - window[1]
    result = step
    if (math.floor(dx / result) + 1) * (math.floor(dy / result) + 1) * layers > max_cells:
        # Round upward in micrometres; the finite grid remains a search heuristic.
        result = max(step, math.ceil(math.sqrt(dx * dy * layers / max_cells) * 1000) / 1000)
        while (math.floor(dx / result) + 1) * (math.floor(dy / result) + 1) * layers > max_cells:
            result = round(result + 0.001, 6)
    return result


def make_work_plan(preparation: dict, policy: dict, *, workers: int = 2, max_batch_nets: int = 3, checkpoint_offset: int = 0) -> dict:
    """Schedule all ordinary unfinished nets; expose every deferred high-speed net."""
    if not isinstance(preparation, dict) or not isinstance(policy, dict):
        raise ValueError("A board preparation and routing policy are required")
    if isinstance(checkpoint_offset, bool) or not isinstance(checkpoint_offset, int) or checkpoint_offset < 0:
        raise ValueError("Checkpoint offset must be a nonnegative integer")
    work = preparation.get("work", {}).get("items", [])
    critical = set(preparation.get("reference_copper", {}).get("high_speed_nets", []))
    unqualified = preparation.get("unqualified_nets")
    if (not isinstance(unqualified, list)
            or any(not isinstance(name, str) or not name for name in unqualified)):
        raise ValueError("Net qualification classification is required before scheduling")
    unqualified = set(unqualified)
    excluded = set(policy.get("exclude_nets", []))
    base = deepcopy(policy.get("route", {}))
    overrides = policy.get("net_overrides", {})
    search = deepcopy(policy.get("search", {}))
    maximum = policy.get("max_pins", 3)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 2 <= maximum <= 20:
        raise ValueError("Routing checkpoints require a maximum of at least two islands")
    items = []
    blocked = {}
    for raw in work:
        item = WorkItem(raw["name"], raw["priority"], tuple(raw["window"]), raw["island_count"])
        if item.name in critical:
            blocked[item.name] = "differential-pair routing is not implemented"
        elif item.name in excluded:
            blocked[item.name] = "excluded by routing policy"
        elif item.name in unqualified:
            blocked[item.name] = "electrical qualification or protected-net review required"
        else:
            items.append(item)
    if len({item.name for item in items}) + len(blocked) != len(work):
        raise ValueError("Board work contains duplicate net names")
    rounds = []
    # Preserve subsystem priority even when unrelated windows happen to overlap.
    for priority in sorted({item.priority for item in items}):
        for round_ in schedule(tuple(item for item in items if item.priority == priority),
                               workers=workers, max_batch_nets=max_batch_nets):
            requests = []
            for batch in round_.batches:
                shared = deepcopy(base)
                # KiCad stores coordinates as integer nanometres; remove only
                # binary floating-point noise introduced by window arithmetic.
                window = tuple(round(value, 6) for value in batch.window)
                shared["bounds_mm"] = list(window)
                nets = []
                for item in batch.items:
                    fields = deepcopy(overrides.get(item.name, {}))
                    if "bounds_mm" in fields:
                        raise ValueError(f"Per-net bounds override the scheduled window: {item.name}")
                    layers = fields.get("layers", shared.get("layers", ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]))
                    cells = fields.get("max_cells", shared.get("max_cells", 250000))
                    step = fields.get("step_mm", shared.get("step_mm", 0.25))
                    # The checkpoint engine adapts grid size after shrinking to
                    # selected islands, retaining fine resolution where possible.
                    if not isinstance(layers, list) or not layers or not isinstance(cells, int) or cells < 1:
                        raise ValueError(f"Invalid layer or grid settings for {item.name}")
                    fields["step_mm"] = step
                    nets.append({"name": item.name, **fields})
                requests.append({"shared": shared, "nets": nets, "search": deepcopy(search),
                                 "rework": deepcopy(policy.get("rework", {})),
                                 "max_islands": maximum, "checkpoint_offset": checkpoint_offset,
                                 "recovery_stage": 0,
                                 "recovery_bounds_mm": list(preparation.get("board_bounds_mm", window))})
            rounds.append(requests)
    return {"rounds": rounds, "blocked_nets": blocked,
            "island_counts": {item.name: item.island_count for item in items},
            "legacy_include_list_ignored": bool(policy.get("include_nets"))}


def new_style_issues(before, after) -> tuple[str, ...]:
    """Do not introduce new geometry/editability defects into a candidate."""
    names = ("non_octilinear", "arcs", "invalid_bends", "redundant_joints",
             "staircases", "locked_items", "grouped_items", "ambiguous")
    return tuple(name for name in names if set(getattr(after, name)) - set(getattr(before, name)))


def encode_checkpoint(board: bytes, inputs: dict[str, str], tools: dict[str, str], policy: str) -> dict:
    """Keep a resumable candidate and its immutable input identity together."""
    if not isinstance(board, bytes) or len(board) > 128 * 1024 * 1024:
        raise ValueError("Invalid checkpoint board")
    return {"candidate_sha256": hashlib.sha256(board).hexdigest(),
            "candidate_zlib_base64": base64.b64encode(zlib.compress(board)).decode("ascii"),
            "inputs": dict(inputs), "tools": dict(tools), "policy": policy}


def decode_checkpoint(checkpoint: dict, inputs: dict[str, str], tools: dict[str, str], policy: str,
                      *, allow_tool_changes: bool = False) -> bytes:
    """Decode an untrusted candidate; renewal still requires native verification.

    Source and policy changes always reject reuse. Only explicit renewal may
    cross a code revision; callers must revalidate and build a new work queue.
    """
    if checkpoint.get("inputs") != inputs or checkpoint.get("policy") != policy:
        raise RuntimeError("Routing inputs or policy changed; start a new run from the saved board")
    if checkpoint.get("tools") != tools and not allow_tool_changes:
        raise RuntimeError("Routing tools changed; use run --renew RUN_ID to revalidate the preview with current code")
    try:
        encoded = checkpoint["candidate_zlib_base64"]
        if not isinstance(encoded, str) or len(encoded) > 180 * 1024 * 1024:
            raise ValueError("Invalid compressed board")
        compressed = base64.b64decode(encoded, validate=True)
        decoder = zlib.decompressobj()
        board = decoder.decompress(compressed, 128 * 1024 * 1024 + 1)
        if len(board) > 128 * 1024 * 1024 or not decoder.eof or decoder.unused_data:
            raise ValueError("Invalid compressed board")
    except (KeyError, TypeError, ValueError, zlib.error) as error:
        raise RuntimeError("Routing candidate checkpoint is invalid") from error
    if hashlib.sha256(board).hexdigest() != checkpoint.get("candidate_sha256"):
        raise RuntimeError("Routing candidate checkpoint hash does not match")
    return board


def rebind_exact_saved_checkpoint(checkpoint: dict, board: bytes, inputs: dict[str, str],
                                  tools: dict[str, str], policy: str,
                                  board_key: str | None = None, *, allow_rule_changes: bool = False) -> dict:
    """Continue only when the saved board is exactly the retained candidate."""
    if not isinstance(allow_rule_changes, bool):
        raise ValueError('Rule adoption must be explicitly boolean')
    if board_key is None:
        boards = [key for key in inputs if key.endswith(".kicad_pcb") and "/" not in key]
        if len(boards) != 1:
            raise ValueError("Checkpoint must identify one PCB source")
        board_key = boards[0]
    allowed = {board_key}
    if allow_rule_changes:
        allowed.add(board_key.removesuffix(".kicad_pcb") + ".kicad_dru")
    old = checkpoint.get("inputs")
    if (not isinstance(old, dict) or old.keys() != inputs.keys()
            or any(old[key] != inputs[key] for key in inputs if key not in allowed)
            or inputs.get(board_key) != hashlib.sha256(board).hexdigest()):
        raise RuntimeError("Saved-preview adoption changed another input")
    candidate = decode_checkpoint(checkpoint, old, tools, policy, allow_tool_changes=True)
    if board != candidate:
        raise RuntimeError("Saved board is not the exact retained preview")
    return {**checkpoint, "inputs": dict(inputs), "tools": dict(tools)}
