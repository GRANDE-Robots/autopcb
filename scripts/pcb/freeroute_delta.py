"""Extract additive copper proposals without replaying a lossy SES round trip.

The result is only a proposal. It still needs native DRC, protected-item,
connectivity, routing-style, reference-copper and electrical validation.
"""

from __future__ import annotations

from core.project import native_path

from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal
from itertools import chain, combinations
import json
import math
from pathlib import Path

from core.toolchain import kicad_cli
import tempfile
import time
from uuid import uuid4

from core.kicad_sexpr import quote
from core.pcb_io import copper_width
from pcb.net_routing.geometry import (
    RouteGeometryError,
    intersection,
    on_segment,
    unique_segments,
)
from pcb.route_optimizer import (
    COPPER_LAYERS,
    GridRouter,
    NoGridRoute,
    Obstacle,
    Point,
    SearchBudgetExceeded,
    SearchLimitReached,
    Segment,
    mm,
)
from core.compat import strict_zip


@dataclass(frozen=True, order=True)
class Trace:
    net: str
    layer: str
    width: int
    start: tuple[int, int]
    end: tuple[int, int]

    def __post_init__(self):
        if self.start > self.end:
            first, last = self.end, self.start
            object.__setattr__(self, "start", first)
            object.__setattr__(self, "end", last)


@dataclass(frozen=True, order=True)
class Via:
    net: str
    at: tuple[int, int]
    size: int
    drill: int


@dataclass(frozen=True)
class Delta:
    traces: tuple[Trace, ...]
    vias: tuple[Via, ...]
    excluded_nets: tuple[str, ...]
    unsupported: int


class FailedPruneOutcomes:
    """Avoid checking the same disconnected copper proposal more than once."""

    def __init__(self) -> None:
        self._outcomes: set[tuple[tuple[Trace, ...], tuple[Via, ...]]] = set()

    @staticmethod
    def _key(delta: Delta) -> tuple[tuple[Trace, ...], tuple[Via, ...]]:
        return tuple(sorted(delta.traces)), tuple(sorted(delta.vias))

    def add(self, delta: Delta) -> None:
        self._outcomes.add(self._key(delta))

    def contains(self, delta: Delta) -> bool:
        return self._key(delta) in self._outcomes


class RepairAttemptMemory:
    """Deduplicate searches only while the surrounding proposal is unchanged."""

    def __init__(self) -> None:
        self._context: Delta | None = None
        self._attempts: set[tuple] = set()

    def claim(
        self,
        delta: Delta,
        net: str,
        layer: str,
        ends: tuple[tuple[int, int], tuple[int, int]],
    ) -> bool:
        if not isinstance(delta, Delta) or not net or not layer or len(ends) != 2:
            raise ValueError(
                "A proposal, net, layer and two repair endpoints are required"
            )
        if self._context != delta:
            self._context = delta
            self._attempts.clear()
        key = (net, layer, ends)
        if key in self._attempts:
            return False
        self._attempts.add(key)
        return True


class RepairOrdering:
    """Try a second bounded repair order after a partial greedy repair stalls."""

    def __init__(self, initial: Delta) -> None:
        if not isinstance(initial, Delta):
            raise ValueError("A starting copper proposal is required")
        self._initial = initial
        self._repaired = False
        self._alternate = False

    def record_repair(self) -> None:
        self._repaired = True

    def seeds(self, items: set[Trace | Via]) -> list[Trace]:
        traces = [item for item in items if isinstance(item, Trace)]
        if self._alternate:
            return sorted(
                traces,
                key=lambda item: (
                    math.hypot(
                        item.end[0] - item.start[0], item.end[1] - item.start[1]
                    ),
                    item,
                ),
            )
        return sorted(traces)

    def restart(self, *, may_continue: bool) -> Delta | None:
        if not isinstance(may_continue, bool):
            raise ValueError("Repair budget state must be boolean")
        if not self._repaired or self._alternate or not may_continue:
            return None
        self._alternate = True
        return self._initial


def without_nets(delta: Delta, nets: set[str]) -> Delta:
    """Drop proposed copper on selected nets; never modify existing copper."""
    return Delta(
        tuple(item for item in delta.traces if item.net not in nets),
        tuple(item for item in delta.vias if item.net not in nets),
        delta.excluded_nets,
        delta.unsupported,
    )


def materialize_saved_widths(
    delta: Delta, profile_for, *, max_traces: int = 50_000
) -> tuple[Delta, int, dict[str, str]]:
    """Widen proposed copper to saved dimensions; never change existing copper.

    Reuse the search engine's package/trunk splitter. A router's narrow escape
    is not permission to keep a skinny trunk. Geometry, clearance and native
    validation still decide whether a widened proposal can survive.
    """
    if (
        not isinstance(delta, Delta)
        or not callable(profile_for)
        or isinstance(max_traces, bool)
        or not isinstance(max_traces, int)
        or max_traces < 1
    ):
        raise ValueError(
            "Width materialization requires a proposal, profiles and a positive segment limit"
        )
    traces, changed, blocked = {}, 0, {}
    for item in delta.traces:
        try:
            pieces = profile_for(item).pieces(_segment(item))
            replacements = tuple(
                Trace(
                    item.net,
                    segment.layer,
                    max(item.width, width),
                    (segment.start.x, segment.start.y),
                    (segment.end.x, segment.end.y),
                )
                for segment, width in pieces
            )
            if not replacements:
                raise ValueError("Saved rules produced no track pieces")
        except ValueError as error:
            # Keep the unqualified proposal for the ordinary width gate to
            # reject. Do not reinterpret an unsupported rule as permission.
            blocked[item.net] = str(error)
            replacements = (item,)
        changed += replacements != (item,)
        for replacement in replacements:
            key = replacement.net, replacement.layer, replacement.start, replacement.end
            if key not in traces or replacement.width > traces[key].width:
                traces[key] = replacement
            if len(traces) > max_traces:
                raise ValueError(
                    f"Width materialization exceeds its {max_traces:,}-segment limit"
                )
    return (
        Delta(
            tuple(sorted(traces.values())),
            delta.vias,
            delta.excluded_nets,
            delta.unsupported,
        ),
        changed,
        blocked,
    )


class ConnectivityRetries:
    """Try bounded subsets after a batch disturbs previously connected copper.

    These are proposals only: each still goes through the normal complete
    native checks. No existing copper is removed by this fallback.
    """

    def __init__(self, delta: Delta, *, max_trials: int = 8) -> None:
        if not isinstance(delta, Delta):
            raise ValueError("A copper proposal is required")
        if (
            isinstance(max_trials, bool)
            or not isinstance(max_trials, int)
            or max_trials < 0
        ):
            raise ValueError("The trial limit must be a nonnegative integer")
        names = sorted({item.net for item in (*delta.traces, *delta.vias)})
        self._delta = delta
        self._omissions = (
            self._balanced_omissions(tuple(names))
            if len(names) > 4
            else chain(combinations(names, 1), combinations(names, 2))
        )
        self._remaining = max_trials

    @staticmethod
    def _balanced_omissions(names: tuple[str, ...]):
        """Find a clean sub-batch even when several unrelated routes conflict.

        Breadth-first splitting tests both halves before smaller subsets.
        Removing one net at a time cannot isolate multiple simultaneous
        conflicts within a small validation budget on a board-wide proposal.
        """
        pending = deque([names])
        all_names = frozenset(names)
        while pending:
            group = pending.popleft()
            if len(group) < 2:
                continue
            middle = len(group) // 2
            for subset in (group[:middle], group[middle:]):
                yield tuple(sorted(all_names.difference(subset)))
                pending.append(subset)

    def next(self) -> tuple[Delta, tuple[str, ...]] | None:
        while self._remaining:
            omitted = next(self._omissions, None)
            if omitted is None:
                return None
            candidate = without_nets(self._delta, set(omitted))
            if not (candidate.traces or candidate.vias):
                continue
            self._remaining -= 1
            return candidate, omitted
        return None


def cleanup_remainder(delta: Delta, rejected_nets: set[str]) -> Delta | None:
    """Try retaining clean proposals from other nets when cleanup runs out of time."""
    remainder = without_nets(delta, rejected_nets)
    if remainder == delta or not (remainder.traces or remainder.vias):
        return None
    return remainder


def timeout_remainder(
    delta: Delta, rejected_nets: set[str], *, pending: bool
) -> Delta | None:
    """Allow one native check of a prune already prepared before the deadline."""
    if not isinstance(pending, bool):
        raise ValueError("A pending-prune flag must be boolean")
    return delta if pending else cleanup_remainder(delta, rejected_nets)


def salvage_remainder(
    delta: Delta, rejected_nets: set[str], *, attempts: int, max_attempts: int = 3
) -> Delta | None:
    """Bound final native rechecks while retaining other proposed nets."""
    if (
        isinstance(attempts, bool)
        or not isinstance(attempts, int)
        or attempts < 0
        or isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts < 0
    ):
        raise ValueError("Salvage limits must be nonnegative integers")
    if attempts >= max_attempts:
        return None
    return cleanup_remainder(delta, rejected_nets)


def without_items(delta: Delta, items: set[Trace | Via]) -> Delta:
    """Remove a selected set of new copper from a disposable proposal."""
    return Delta(
        tuple(item for item in delta.traces if item not in items),
        tuple(item for item in delta.vias if item not in items),
        delta.excluded_nets,
        delta.unsupported,
    )


def without_in_pad_segments(delta: Delta, board) -> Delta:
    """Drop new segments whose entire copper capsule is already inside one pad.

    A bounding box or endpoint-only test is insufficient. Both end discs are
    enclosed by a circumscribed polygon, and every polygon vertex must lie in
    the same convex, same-net SMD pad on the actual copper layer. Convexity then
    proves containment of the whole capsule. Existing board copper is untouched.
    """
    import pcbnew

    convex_shapes = {
        pcbnew.PAD_SHAPE_RECT,
        pcbnew.PAD_SHAPE_CIRCLE,
        pcbnew.PAD_SHAPE_OVAL,
        pcbnew.PAD_SHAPE_ROUNDRECT,
    }
    pads = defaultdict(list)
    for pad in board.GetPads():
        if pad.GetAttribute() == pcbnew.PAD_ATTRIB_SMD:
            pads[pad.GetNetname()].append(pad)
    shapes, offsets, removed = {}, {}, set()
    for item in delta.traces:
        if item.width <= 0:
            continue
        layer = board.GetLayerID(item.layer)
        if item.width not in offsets:
            radius = math.ceil(item.width / (2 * math.cos(math.pi / 16))) + 2
            offsets[item.width] = tuple(
                (
                    round(radius * math.cos(index * math.pi / 8)),
                    round(radius * math.sin(index * math.pi / 8)),
                )
                for index in range(16)
            )
        for pad in pads[item.net]:
            if not pad.IsOnLayer(layer) or pad.GetShape(layer) not in convex_shapes:
                continue
            key = pad.m_Uuid.AsString(), layer
            if key not in shapes:
                shapes[key] = pad.GetEffectiveShape(layer)
            if all(
                shapes[key].Collide(pcbnew.VECTOR2I(point[0] + dx, point[1] + dy))
                for point in (item.start, item.end)
                for dx, dy in offsets[item.width]
            ):
                removed.add(item)
                break
    return without_items(delta, removed)


def replace_repaired_chain(
    delta: Delta,
    chain: tuple[Trace, ...],
    replacement: tuple[Trace, ...],
    new_vias: tuple[Via, ...],
) -> Delta:
    """Replace one proposed chain without discarding other proposed copper."""
    if not chain or not replacement or not set(chain) <= set(delta.traces):
        raise ValueError("A repair must replace an existing proposed chain")
    net = chain[0].net
    if (
        any(not isinstance(item, Trace) for item in (*chain, *replacement))
        or any(not isinstance(item, Via) for item in new_vias)
        or any(item.net != net for item in (*chain, *replacement, *new_vias))
    ):
        raise ValueError("A repaired chain must keep one net and valid copper types")
    tracks = (set(delta.traces) - set(chain)) | set(replacement)
    vias = set(delta.vias) | set(new_vias)
    return Delta(
        tuple(sorted(tracks)),
        tuple(sorted(vias)),
        delta.excluded_nets,
        delta.unsupported,
    )


def replacement_style_clear(
    bad_ids: set[str],
    proposed: dict[str, Trace | Via],
    replacement: tuple[Trace, ...],
    new_vias: tuple[Via, ...],
    *,
    blockers: tuple[str, ...] = (),
) -> bool:
    """Reject unsafe local alternatives without rejecting the whole batch."""
    if blockers:
        return False
    replacement_items = set(replacement) | set(new_vias)
    replacement_ids = {
        identifier for identifier, item in proposed.items() if item in replacement_items
    }
    if not replacement_ids:
        raise ValueError("The replacement must be present in the checked proposal")
    return not bool(set(bad_ids) & replacement_ids)


def proposed_component(
    board,
    proposed: dict[str, Trace | Via],
    seed: str,
    *,
    tracks: dict | None = None,
    connectivity=None,
) -> set[Trace | Via]:
    """Find directly connected proposed copper without traversing old copper."""
    if tracks is None:
        tracks = {item.m_Uuid.AsString(): item for item in board.GetTracks()}
    if seed not in proposed or seed not in tracks:
        raise ValueError(
            "A dangling component needs an identifiable proposed copper item"
        )
    if connectivity is None:
        connectivity = board.GetConnectivity()
        connectivity.Build(board)
    found = set()
    pending = [seed]
    net = proposed[seed].net
    while pending:
        identifier = pending.pop()
        if identifier in found:
            continue
        found.add(identifier)
        for neighbor in connectivity.GetConnectedTracks(tracks[identifier]):
            other = neighbor.m_Uuid.AsString()
            if other in proposed and other not in found and proposed[other].net == net:
                pending.append(other)
    return {proposed[identifier] for identifier in found}


def dangling_proposed_branches(
    board,
    proposed: dict[str, Trace | Via],
    seeds: set[str],
    *,
    tracks: dict | None = None,
    connectivity=None,
) -> set[Trace | Via]:
    """Peel proven dangling leaves, stopping at pads, old copper, zones, and junctions.

    This is only a cleanup of a disposable candidate.  A native check must still
    verify the remaining candidate; no connection or geometry is accepted here.
    Nets with zones are conservatively left to the native per-item cleanup.
    """
    if not seeds <= proposed.keys():
        raise ValueError("Every dangling seed must identify proposed copper")
    if tracks is None:
        tracks = {item.m_Uuid.AsString(): item for item in board.GetTracks()}
    if not proposed.keys() <= tracks.keys():
        raise ValueError("Proposed copper must be present in the candidate board")
    if connectivity is None:
        connectivity = board.GetConnectivity()
        connectivity.Build(board)
    zoned = {
        zone.GetNetname()
        for zone in board.Zones()
        if not zone.GetIsRuleArea() and zone.GetNetname()
    }
    adjacent: dict[str, set[str]] = {}
    anchors = set()
    for identifier, record in proposed.items():
        item = tracks[identifier]
        neighbors = {
            neighbor.m_Uuid.AsString()
            for neighbor in connectivity.GetConnectedTracks(item)
        }
        adjacent[identifier] = neighbors & proposed.keys()
        if (
            record.net in zoned
            or neighbors - proposed.keys()
            or connectivity.GetConnectedPads(item)
        ):
            anchors.add(identifier)

    removed: set[str] = set()
    pending = list(sorted(seeds))
    while pending:
        identifier = pending.pop()
        if identifier in removed or identifier in anchors:
            continue
        neighbors = adjacent[identifier] - removed
        if len(neighbors) > 1:
            continue
        removed.add(identifier)
        pending.extend(neighbors)
    return {proposed[identifier] for identifier in removed}


def restore_raw_nets(delta: Delta, raw: tuple[Trace, ...], nets: set[str]) -> Delta:
    """Replace shortcut traces on selected nets with their original proposal."""
    traces = {item for item in delta.traces if item.net not in nets}
    traces.update(item for item in raw if item.net in nets)
    return Delta(
        tuple(sorted(traces)), delta.vias, delta.excluded_nets, delta.unsupported
    )


def _octilinear(trace: Trace) -> bool:
    dx = abs(trace.end[0] - trace.start[0])
    dy = abs(trace.end[1] - trace.start[1])
    return bool(dx or dy) and (not dx or not dy or dx == dy)


def octilinear_alternatives(trace: Trace) -> tuple[tuple[Trace, Trace], ...]:
    """Return both shortest two-segment, 45-degree alternatives for a trace."""
    if not isinstance(trace, Trace):
        raise ValueError("An external trace is required")
    if _octilinear(trace):
        return ()
    x0, y0 = trace.start
    x1, y1 = trace.end
    dx, dy = x1 - x0, y1 - y0
    ax, ay = abs(dx), abs(dy)
    if not ax or not ay or ax == ay:
        return ()
    sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
    if ax > ay:
        bends = ((x0 + sx * (ax - ay), y0), (x0 + sx * ay, y0 + sy * ay))
    else:
        bends = ((x0, y0 + sy * (ay - ax)), (x0 + sx * ax, y0 + sy * ax))
    return tuple(
        (
            Trace(trace.net, trace.layer, trace.width, trace.start, point),
            Trace(trace.net, trace.layer, trace.width, point, trace.end),
        )
        for point in bends
    )


def recover_off_angle_additions(
    comparison: list[Trace | Via],
    routed: list[Trace | Via],
    excluded: set[str] | frozenset[str],
    allowed,
    *,
    may_continue=lambda: True,
) -> tuple[list[Trace | Via], int]:
    """Replace only new external traces when a checked 45-degree path is clear.

    This is a proposal repair, not DRC, electrical or connectivity approval.
    The caller must independently validate the complete resulting transaction.
    """
    if (
        not isinstance(excluded, (set, frozenset))
        or not callable(allowed)
        or not callable(may_continue)
        or any(not isinstance(item, (Trace, Via)) for item in (*comparison, *routed))
    ):
        raise ValueError(
            "Off-angle recovery needs copper, exclusions and callable checks"
        )
    before, after = Counter(comparison), Counter(routed)
    candidates = sorted(
        item
        for item in (after - before)
        if isinstance(item, Trace)
        and not _octilinear(item)
        and item.net not in excluded
        and not before[item]
        and after[item] == 1
    )
    amended, repaired = list(routed), 0
    for item in candidates:
        if not may_continue():
            break
        for path in octilinear_alternatives(item):
            if allowed(path):
                amended.remove(item)
                amended.extend(path)
                repaired += 1
                break
    return amended, repaired


def _segment(trace: Trace) -> Segment:
    return Segment(Point(*trace.start), Point(*trace.end), trace.layer)


def router_class(native_name: str) -> str:
    """Match KiCad's default-class name to its Specctra export."""
    return "kicad_default" if native_name == "Default" else native_name


def retryable_native_rejection(error: Exception) -> bool:
    """Only candidate-specific native violations may trigger a subset retry."""
    return isinstance(error, RuntimeError) and str(error).partition(":")[0] in {
        "Resumed candidate introduces native violations",
        "Resumed candidate changes or worsens an existing native error",
    }


def newly_implicated_nets(
    baseline: dict,
    candidate: dict,
    baseline_ids: dict[str, str],
    candidate_ids: dict[str, str],
    item_nets: dict[str, str],
    proposed_nets: set[str],
) -> tuple[str, ...]:
    """Attribute new native findings to proposed nets without approving them."""
    from pcb.route_validation import _key

    before, after = baseline.get("violations"), candidate.get("violations")
    if not isinstance(before, list) or not isinstance(after, list):
        raise ValueError("Native violation records are required for a subset retry")
    old = Counter(_key(item, baseline_ids) for item in before)
    implicated = set()
    for finding in after:
        key = _key(finding, candidate_ids)
        if old[key]:
            old[key] -= 1
            continue
        for item in finding.get("items", []):
            net = item_nets.get(item.get("uuid")) if isinstance(item, dict) else None
            if net in proposed_nets:
                implicated.add(net)
    return tuple(sorted(implicated))


def deferred_class(proposals: Counter, rejected: Counter) -> str | None:
    """Try other classes first when a class caused rejected copper."""
    counts = rejected or proposals
    return min(counts, key=lambda name: (-counts[name], name)) if counts else None


def checked_improvement(
    baseline: dict,
    candidate: dict,
    *,
    stable_connection_items: dict[str, str] | None = None,
) -> bool:
    """Discard non-improvements; require native safety checks for improvements."""
    from pcb.net_routing.runner import verify_resume_reports

    old, new = baseline.get("open_connections"), candidate.get("open_connections")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (old, new)
    ):
        raise ValueError("Native open-connection counts must be nonnegative integers")
    if new >= old:
        return False
    verify_resume_reports(
        baseline,
        candidate,
        expected_opens=new,
        stable_connection_items=stable_connection_items,
    )
    return True


def cleanup_timed_out(passes: int, elapsed: float, limit: float | None) -> bool:
    """Permit one check, then bound time spent cleaning an unresolved proposal."""
    return bool(passes and limit is not None and elapsed >= limit)


def repair_budget_available(elapsed: float, limit: float | None) -> bool:
    """Do not start another optional repair after the cleanup budget expires."""
    return limit is None or elapsed < limit


def remember_blocked_nets(
    blockers: dict[str, str], nets: set[str], reason: str
) -> None:
    """Carry the last rejected nets into a bounded retry without hiding earlier evidence."""
    for name in nets:
        blockers.setdefault(name, reason)


def unconnected_by_net(report: dict, item_nets: dict[str, str]) -> dict[str, int]:
    """Summarize DRC missing-connection pairs using native item net identities."""
    entries = report.get("unconnected_items")
    if not isinstance(entries, list) or not isinstance(item_nets, dict):
        raise ValueError("Complete DRC items and native net identities are required")
    counts = Counter()
    for entry in entries:
        items = entry.get("items", ()) if isinstance(entry, dict) else ()
        names = {
            item_nets.get(item.get("uuid")) for item in items if isinstance(item, dict)
        } - {None, ""}
        counts[next(iter(names)) if len(names) == 1 else "<unknown>"] += 1
    return dict(counts)


def regressed_open_nets(
    baseline: dict[str, int], candidate: dict[str, int], proposed_nets: set[str]
) -> tuple[str, ...]:
    """Identify proposals that add isolated islands, without losing other nets."""
    for counts in (baseline, candidate):
        if any(
            isinstance(count, bool) or not isinstance(count, int) or count < 0
            for count in counts.values()
        ):
            raise ValueError("Open-connection counts must be nonnegative integers")
        if counts.get("<unknown>", 0):
            raise ValueError("Cannot compare unidentified open connections by net")
    return tuple(
        sorted(
            name
            for name in proposed_nets
            if candidate.get(name, 0) > baseline.get(name, 0)
        )
    )


def unproductive_pruned_nets(
    baseline: dict[str, int], candidate: dict[str, int], pruned_nets: set[str]
) -> set[str]:
    """Temporarily defer pruned nets that remain as open as before the batch."""
    if baseline.get("<unknown>", 0) or candidate.get("<unknown>", 0):
        return set()
    return {
        name
        for name in pruned_nets
        if baseline.get(name, 0) > 0 and candidate.get(name, 0) >= baseline[name]
    }


def native_item_nets(board) -> dict[str, str]:
    """Map KiCad item UUIDs to nets for attributing native DRC open pairs."""
    items = (*board.GetPads(), *board.GetTracks(), *board.Zones())
    return {
        item.m_Uuid.AsString(): str(item.GetNetname())
        for item in items
        if item.GetNetname()
    }


def align_proposal_to_existing_vias(
    delta: Delta, original: list[Trace | Via], *, tolerance_nm: int = 2
) -> tuple[Delta, dict[str, tuple[int, int]]]:
    """Translate new net copper only when all near-via anchors agree on a tiny offset.

    Existing copper is untouched. The translated proposal still requires the
    normal native connectivity, clearance, style and reference checks.
    """
    if (
        isinstance(tolerance_nm, bool)
        or not isinstance(tolerance_nm, int)
        or tolerance_nm < 1
    ):
        raise ValueError(
            "Via alignment tolerance must be a positive integer in nanometres"
        )
    via_points = defaultdict(list)
    for item in original:
        if isinstance(item, Via):
            via_points[item.net].append(item.at)
    candidates = defaultdict(set)
    for trace in delta.traces:
        for x, y in (trace.start, trace.end):
            for vx, vy in via_points[trace.net]:
                offset = vx - x, vy - y
                if max(abs(offset[0]), abs(offset[1])) <= tolerance_nm:
                    candidates[trace.net].add(offset)
    offsets = {
        net: next(iter(values))
        for net, values in candidates.items()
        if len(values) == 1 and (0, 0) not in values
    }
    if not offsets:
        return delta, {}

    def shifted(net, point):
        dx, dy = offsets.get(net, (0, 0))
        return point[0] + dx, point[1] + dy

    traces = tuple(
        Trace(
            item.net,
            item.layer,
            item.width,
            shifted(item.net, item.start),
            shifted(item.net, item.end),
        )
        for item in delta.traces
    )
    vias = tuple(
        Via(item.net, shifted(item.net, item.at), item.size, item.drill)
        for item in delta.vias
    )
    return Delta(traces, vias, delta.excluded_nets, delta.unsupported), offsets


def snap_proposal_via_escapes(
    delta: Delta,
    original: list[Trace | Via],
    is_anchor,
    *,
    tolerance_nm: int = 1_000,
    capture_via_copper: bool = False,
    shortcut_allowed=None,
    protected_endpoint=None,
) -> tuple[Delta, set[str]]:
    """Center a near-via endpoint while preserving existing copper contacts.

    Two-track cases preserve both headings; an isolated pad or via escape can
    instead use a checked shortest octilinear path. Ambiguous vias, branches
    and existing contacts are unchanged. Native and electrical checks remain
    necessary.
    """
    if (
        not callable(is_anchor)
        or isinstance(tolerance_nm, bool)
        or not isinstance(tolerance_nm, int)
        or tolerance_nm < 1
    ):
        raise ValueError("Via snapping needs an anchor check and positive tolerance")
    if not isinstance(capture_via_copper, bool):
        raise ValueError("Via copper capture must be true or false")
    if shortcut_allowed is not None and not callable(shortcut_allowed):
        raise ValueError("Via snapping needs a callable clearance check")
    if protected_endpoint is not None and not callable(protected_endpoint):
        raise ValueError("Via snapping needs a callable pad-contact check")
    traces = list(delta.traces)
    vias = defaultdict(dict)
    for via in (*original, *delta.vias):
        if isinstance(via, Via):
            vias[via.net][via.at] = max(vias[via.net].get(via.at, 0), via.size)
    aligned = set()

    def direction(first, last):
        return (
            (last[0] > first[0]) - (last[0] < first[0]),
            (last[1] > first[1]) - (last[1] < first[1]),
        )

    for _ in range(len(traces)):
        adjacency = defaultdict(list)
        for trace in traces:
            adjacency[(trace.net, trace.layer, trace.start)].append(trace)
            adjacency[(trace.net, trace.layer, trace.end)].append(trace)
        changed = False
        for trace in sorted(traces):
            if not _octilinear(trace):
                continue
            for endpoint, joint in ((trace.start, trace.end), (trace.end, trace.start)):
                near = []
                for at, size in vias[trace.net].items():
                    dx, dy = at[0] - endpoint[0], at[1] - endpoint[1]
                    # KiCad tests copper contact, including the trace end cap.
                    # Its center can lie outside the via radius and still
                    # produce a real off-center-via connection warning.
                    limit = (size + trace.width + 1) // 2 if capture_via_copper else 0
                    if max(abs(dx), abs(dy)) <= tolerance_nm or (
                        limit and dx * dx + dy * dy <= limit * limit
                    ):
                        near.append((at, max(tolerance_nm, limit)))
                if len(near) != 1 or near[0][0] == endpoint:
                    continue
                pair = adjacency[(trace.net, trace.layer, joint)]
                if (
                    len(pair) == 1
                    and capture_via_copper
                    and shortcut_allowed is not None
                    and protected_endpoint is not None
                ):
                    others = [item for item in traces if item != trace]
                    via_at, _ = near[0]
                    replacement = _single_via_escape(
                        trace,
                        endpoint,
                        joint,
                        via_at,
                        vias[trace.net][via_at],
                        [*original, *delta.vias, *others],
                        is_anchor,
                        protected_endpoint,
                        shortcut_allowed,
                        set(vias[trace.net]),
                    )
                    if replacement is not None:
                        traces = [*others, *replacement]
                        aligned.add(trace.net)
                        changed = True
                        break
                if (
                    len(pair) != 2
                    or pair[0].width != pair[1].width
                    or is_anchor(trace.net, trace.layer, joint)
                ):
                    continue
                neighbor = pair[1] if pair[0] == trace else pair[0]
                if not _octilinear(neighbor):
                    continue
                others = [item for item in traces if item not in pair]
                safe = dangling_trace_remainder(
                    trace, tuple(pair), [*original, *others], is_anchor
                )
                if set(safe) != set(pair):
                    continue
                other = neighbor.end if neighbor.start == joint else neighbor.start
                via, movement_limit = near[0]
                u, v = direction(endpoint, joint), direction(joint, other)
                denominator = u[0] * v[1] - u[1] * v[0]
                if not denominator:
                    continue
                numerator = (other[0] - via[0]) * v[1] - (other[1] - via[1]) * v[0]
                if numerator % denominator:
                    continue
                scale = numerator // denominator
                moved = via[0] + scale * u[0], via[1] + scale * u[1]
                if (
                    max(abs(moved[0] - joint[0]), abs(moved[1] - joint[1]))
                    > 2 * movement_limit
                    or moved in {via, other}
                    or direction(via, moved) != u
                    or direction(moved, other) != v
                ):
                    continue
                replacement = (
                    Trace(trace.net, trace.layer, trace.width, via, moved),
                    Trace(trace.net, trace.layer, trace.width, moved, other),
                )
                if any(
                    not _octilinear(item) or item in others for item in replacement
                ) or (
                    shortcut_allowed is not None and not shortcut_allowed(replacement)
                ):
                    continue
                traces = [*others, *replacement]
                aligned.add(trace.net)
                changed = True
                break
            if changed:
                break
        if not changed:
            break
    return Delta(
        tuple(sorted(traces)), delta.vias, delta.excluded_nets, delta.unsupported
    ), aligned


def _single_via_escape(
    trace: Trace,
    endpoint: tuple[int, int],
    anchor: tuple[int, int],
    via: tuple[int, int],
    via_size: int,
    contacts: list[Trace | Via],
    is_anchor,
    protected_endpoint,
    shortcut_allowed,
    via_centers: set[tuple[int, int]],
) -> tuple[Trace, ...] | None:
    """Center one proposed terminal trace without moving its other contact."""
    if (
        not is_anchor(trace.net, trace.layer, anchor)
        or not (
            protected_endpoint(trace.net, trace.layer, anchor) or anchor in via_centers
        )
        or protected_endpoint(trace.net, trace.layer, endpoint)
    ):
        return None

    def contacts_only_at(item: Trace, allowed: set[tuple[int, int]]) -> bool:
        for other in contacts:
            if (
                isinstance(other, Via)
                and other.net == item.net
                and other.at not in allowed
            ):
                ax, ay = item.start
                dx, dy = item.end[0] - ax, item.end[1] - ay
                px, py = other.at[0] - ax, other.at[1] - ay
                length_sq = dx * dx + dy * dy
                projection = px * dx + py * dy
                radius = (other.size + item.width + 1) // 2
                if projection <= 0:
                    touching = px * px + py * py <= radius * radius
                elif projection >= length_sq:
                    ex, ey = other.at[0] - item.end[0], other.at[1] - item.end[1]
                    touching = ex * ex + ey * ey <= radius * radius
                else:
                    touching = (px * dy - py * dx) ** 2 <= radius * radius * length_sq
                if touching:
                    return False
                continue
            if not isinstance(other, Trace) or (other.net, other.layer) != (
                item.net,
                item.layer,
            ):
                continue
            if _same_net_collinear_overlap(item, other):
                return False
            first, second = _segment(item), _segment(other)
            try:
                crossing = intersection(first, second)
            except RouteGeometryError:
                return False
            touching = {
                point
                for point in (item.start, item.end, other.start, other.end)
                if on_segment(Point(*point), first)
                and on_segment(Point(*point), second)
            }
            if crossing is not None:
                touching.add((crossing.x, crossing.y))
            if touching - allowed:
                return False
        return True

    if not contacts_only_at(trace, {anchor, via}):
        return None
    dx, dy = via[0] - anchor[0], via[1] - anchor[1]
    if not (dx or dy):
        return None
    if not dx or not dy or abs(dx) == abs(dy):
        paths = ((anchor, via),)
    else:
        step = min(abs(dx), abs(dy))
        sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
        paths = (
            (anchor, (anchor[0] + sx * step, anchor[1] + sy * step), via),
            (anchor, (via[0] - sx * step, via[1] - sy * step), via),
        )
    radius = (via_size + trace.width + 1) // 2
    for path in paths:
        # A bend inside the via copper would still produce KiCad's warning.
        if any(
            (point[0] - via[0]) ** 2 + (point[1] - via[1]) ** 2 <= radius**2
            for point in path[:-1]
        ):
            continue
        replacement = tuple(
            Trace(trace.net, trace.layer, trace.width, a, b)
            for a, b in zip(path, path[1:])
        )
        if all(
            _octilinear(item) and contacts_only_at(item, {anchor, via})
            for item in replacement
        ) and shortcut_allowed(replacement):
            return replacement
    return None


def _trace_touches_via(trace: Trace, via: Via) -> bool:
    """Test the trace capsule against a via's circular copper envelope."""
    ax, ay = trace.start
    dx, dy = trace.end[0] - ax, trace.end[1] - ay
    px, py = via.at[0] - ax, via.at[1] - ay
    length_sq = dx * dx + dy * dy
    radius = (trace.width + via.size + 1) // 2
    projection = px * dx + py * dy
    if not length_sq or projection <= 0:
        return px * px + py * py <= radius * radius
    if projection >= length_sq:
        ex, ey = via.at[0] - trace.end[0], via.at[1] - trace.end[1]
        return ex * ex + ey * ey <= radius * radius
    return (px * dy - py * dx) ** 2 <= radius * radius * length_sq


def snap_new_vias_to_existing_endpoints(
    delta: Delta,
    original: list[Trace | Via],
    anchor_factory,
    via_move_allowed,
    shortcut_allowed,
    protected_endpoint,
) -> tuple[Delta, set[str]]:
    """Center a proposed via on one existing endpoint while preserving contacts.

    Only unambiguous, pad-free movements are attempted. Existing copper never
    moves; each proposed escape must be re-centered and native checks remain
    authoritative for the combined candidate.
    """
    if not isinstance(delta, Delta) or any(
        not callable(value)
        for value in (
            anchor_factory,
            via_move_allowed,
            shortcut_allowed,
            protected_endpoint,
        )
    ):
        raise ValueError(
            "Via relocation requires a proposal and contact and clearance checks"
        )
    result = delta
    changed: set[str] = set()
    for via in delta.vias:
        old_traces = [
            item for item in original if isinstance(item, Trace) and item.net == via.net
        ]
        endpoints = {
            point
            for item in old_traces
            for point in (item.start, item.end)
            if sum((point[i] - via.at[i]) ** 2 for i in (0, 1)) <= (via.size // 2) ** 2
        }
        if via.at in endpoints or len(endpoints) != 1:
            continue
        target = next(iter(endpoints))
        moved = Via(via.net, target, via.size, via.drill)
        if not via_move_allowed(via, moved):
            continue
        if any(
            _trace_touches_via(item, via) and not _trace_touches_via(item, moved)
            for item in old_traces
        ):
            continue
        neighbors = [
            item
            for item in (*original, *result.vias)
            if isinstance(item, Via) and item != via
        ]
        if any(
            sum((other.at[i] - position[i]) ** 2 for i in (0, 1))
            <= ((via.size + other.size + 1) // 2) ** 2
            for other in neighbors
            for position in (via.at, target)
        ):
            continue
        incident = [
            item
            for item in result.traces
            if item.net == via.net and _trace_touches_via(item, via)
        ]
        if not incident or any(
            via.at not in (item.start, item.end) for item in incident
        ):
            continue
        if len(incident) == 1:
            chain = connected_trace_component(incident[0], result.traces)
            ends = chain_endpoints(chain)
            other_traces = [item for item in result.traces if item not in chain]
            anchor = anchor_factory(result.vias)
            if (
                ends is not None
                and via.at in ends
                and len(chain) <= 8
                and set(
                    dangling_trace_remainder(
                        incident[0], chain, [*original, *other_traces], anchor
                    )
                )
                == set(chain)
            ):
                far = ends[1] if ends[0] == via.at else ends[0]
                synthetic = Trace(
                    via.net, incident[0].layer, incident[0].width, far, via.at
                )
                via_centers = {
                    item.at
                    for item in (*original, *result.vias)
                    if isinstance(item, Via) and item.net == via.net
                }
                replacement = _single_via_escape(
                    synthetic,
                    via.at,
                    far,
                    target,
                    via.size,
                    [
                        *original,
                        *other_traces,
                        *(item for item in result.vias if item != via),
                    ],
                    anchor,
                    protected_endpoint,
                    shortcut_allowed,
                    via_centers,
                )
                if replacement is not None:
                    result = Delta(
                        tuple(sorted((*other_traces, *replacement))),
                        tuple(moved if item == via else item for item in result.vias),
                        result.excluded_nets,
                        result.unsupported,
                    )
                    changed.add(via.net)
                    continue
        candidate = Delta(
            result.traces,
            tuple(moved if item == via else item for item in result.vias),
            result.excluded_nets,
            result.unsupported,
        )
        candidate, _ = snap_proposal_via_escapes(
            candidate,
            original,
            anchor_factory(candidate.vias),
            capture_via_copper=True,
            shortcut_allowed=shortcut_allowed,
            protected_endpoint=protected_endpoint,
        )
        old_layers = Counter(item.layer for item in incident)
        new_layers = Counter(
            item.layer
            for item in candidate.traces
            if item.net == via.net and target in (item.start, item.end)
        )
        if any(item in candidate.traces for item in incident) or any(
            new_layers[layer] < count for layer, count in old_layers.items()
        ):
            continue
        result = candidate
        changed.add(via.net)
    return result, changed


def additive_delta(
    original: list[Trace | Via],
    roundtrip: list[Trace | Via],
    routed: list[Trace | Via],
    *,
    excluded: set[str] | frozenset[str] = frozenset(),
) -> Delta:
    """Subtract DSN/SES conversion artifacts and already-present copper."""
    for collection in (original, roundtrip, routed):
        if any(not isinstance(item, (Trace, Via)) for item in collection):
            raise ValueError("Copper records must be traces or through vias")
    new = list((Counter(routed) - Counter(roundtrip)).elements())
    skipped = {item.net for item in new if item.net in excluded}
    traces = [
        item for item in new if isinstance(item, Trace) and item.net not in excluded
    ]
    vias = [item for item in new if isinstance(item, Via) and item.net not in excluded]
    unsupported = sum(not _octilinear(item) for item in traces)
    traces = [item for item in traces if _octilinear(item)]
    occupied = defaultdict(list)
    for item in original:
        if isinstance(item, Trace) and _octilinear(item):
            occupied[(item.net, item.layer)].append(_segment(item))
    proposed = defaultdict(list)
    for item in traces:
        proposed[(item.net, item.layer, item.width)].append(_segment(item))
    additions = []
    for (net, layer, width), segments in proposed.items():
        for segment in unique_segments(tuple(segments), tuple(occupied[(net, layer)])):
            additions.append(
                Trace(
                    net,
                    layer,
                    width,
                    (segment.start.x, segment.start.y),
                    (segment.end.x, segment.end.y),
                )
            )
    old_vias = defaultdict(list)
    for item in original:
        if isinstance(item, Via):
            old_vias[item.net].append(item.at)
    # SES import can round a via centre by fractions of a micrometre. Two
    # drilled holes this close cannot be distinct physical vias, so keep the
    # native one instead of adding a near-coincident duplicate.
    tolerance_nm = 1_000
    vias = [
        item
        for item in vias
        if not any(
            (item.at[0] - at[0]) ** 2 + (item.at[1] - at[1]) ** 2 <= tolerance_nm**2
            for at in old_vias[item.net]
        )
    ]
    return Delta(
        tuple(sorted(set(additions))),
        tuple(sorted(set(vias))),
        tuple(sorted(skipped)),
        unsupported,
    )


def chamfer_right_angles(
    traces: tuple[Trace, ...],
    original: list[Trace | Via],
    is_anchor,
    *,
    chamfer_nm: int = 250_000,
) -> tuple[Trace, ...]:
    """Replace unanchored right angles in new copper with 45-degree corners."""
    if not callable(is_anchor) or isinstance(chamfer_nm, bool) or chamfer_nm < 1:
        raise ValueError(
            "A native anchor check and positive chamfer length are required"
        )
    result = list(traces)
    old = defaultdict(list)
    for item in original:
        if isinstance(item, Trace) and _octilinear(item):
            old[(item.net, item.layer)].append(_segment(item))
    while True:
        endpoints = defaultdict(list)
        for item in result:
            endpoints[(item.net, item.layer, item.start)].append(item)
            endpoints[(item.net, item.layer, item.end)].append(item)
        replacement = None
        for (net, layer, point), pair in sorted(endpoints.items()):
            if len(pair) != 2 or pair[0].width != pair[1].width:
                continue
            if is_anchor(net, layer, point) or any(
                on_segment(Point(*point), segment) for segment in old[(net, layer)]
            ):
                continue
            other = [item.end if item.start == point else item.start for item in pair]
            vectors = [(end[0] - point[0], end[1] - point[1]) for end in other]
            if vectors[0][0] * vectors[1][0] + vectors[0][1] * vectors[1][1] != 0:
                continue
            lengths = [max(abs(dx), abs(dy)) for dx, dy in vectors]
            distance = min(chamfer_nm, *(length // 2 for length in lengths))
            if not distance:
                continue
            units = [((dx > 0) - (dx < 0), (dy > 0) - (dy < 0)) for dx, dy in vectors]
            cut = [
                (point[0] + dx * distance, point[1] + dy * distance) for dx, dy in units
            ]
            width = pair[0].width
            new = (
                Trace(net, layer, width, other[0], cut[0]),
                Trace(net, layer, width, cut[0], cut[1]),
                Trace(net, layer, width, cut[1], other[1]),
            )
            if all(_octilinear(item) for item in new):
                replacement = pair, new
                break
        if replacement is None:
            return tuple(sorted(result))
        old_pair, new_traces = replacement
        for item in old_pair:
            result.remove(item)
        result.extend(new_traces)


def native_shortcut_obstacles(board) -> tuple[Obstacle, ...]:
    """Build conservative per-layer obstacles for shortcut screening."""
    return tuple(obstacle for obstacle, _ in _native_shortcut_sources(board))


def _native_shortcut_sources(board):
    """Keep native shapes available for precision checks on an immutable board."""
    import pcbnew

    from pcb.route_native import (
        _box,
        _pad_obstacle,
        _track_boxes,
        _via_obstacle,
        native_zones,
    )

    physical = tuple(
        board.GetLayerName(layer) for layer in board.GetEnabledLayers().CuStack()
    )
    for pad in board.GetPads():
        drill = pad.GetDrillSize()
        layers = (
            physical
            if drill.x or drill.y
            else tuple(
                layer for layer in physical if pad.IsOnLayer(board.GetLayerID(layer))
            )
        )
        if layers:
            yield _pad_obstacle(board, pad, layers), pad
    for item in board.GetTracks():
        layers = tuple(
            layer for layer in physical if item.IsOnLayer(board.GetLayerID(layer))
        )
        if not layers:
            continue
        if isinstance(item, pcbnew.PCB_VIA):
            yield _via_obstacle(board, item, layers), item
        elif isinstance(item, pcbnew.PCB_ARC):
            yield _box(item, layers, item.GetNetname(), "copper"), item
        else:
            for obstacle in _track_boxes(item, layers, item.GetNetname()):
                yield obstacle, item
    for zone in native_zones(board):
        if zone.GetIsRuleArea():
            layers = tuple(
                layer for layer in physical if zone.IsOnLayer(board.GetLayerID(layer))
            )
            if layers:
                yield _box(zone, layers, "", "keepout"), zone


def native_clearance_checker(board, *, clearance: int = 0, profile_for=None):
    """Use native copper shapes when padded envelopes reject an exact gap.

    This local proposal screen does not relax a saved rule. KiCad DRC, hole,
    via-pad and connectivity checks remain required on the combined candidate.
    The source board's foreign copper must stay fixed while using the cache.
    Drilled objects and keepouts retain their conservative hole envelopes.
    """
    import pcbnew

    if isinstance(clearance, bool) or not isinstance(clearance, int) or clearance < 0:
        raise ValueError("Native clearance must be a nonnegative integer distance")
    if profile_for is not None and not callable(profile_for):
        raise ValueError("Native rule lookup must be callable")
    sources = tuple(_native_shortcut_sources(board))
    layers = tuple(
        board.GetLayerName(layer) for layer in board.GetEnabledLayers().CuStack()
    )
    by_layer = {
        layer: [(obs, part) for obs, part in sources if layer in obs.layers]
        for layer in layers
    }
    shapes = {}

    def clear(pieces):
        for piece in pieces:
            via = isinstance(piece, Via)
            if not via and not isinstance(piece, Trace):
                raise ValueError(
                    "A native clearance proposal must contain tracks or vias"
                )
            active = layers if via else (piece.layer,)
            for layer in active:
                if layer not in by_layer:
                    return False
                layer_id = board.GetLayerID(layer)
                start, end = (piece.at, piece.at) if via else (piece.start, piece.end)
                width = piece.size if via else piece.width
                segment = Segment(Point(*start), Point(*end), layer)
                proposed = pcbnew.PCB_TRACK(board)
                proposed.SetStart(pcbnew.VECTOR2I(*start))
                proposed.SetEnd(pcbnew.VECTOR2I(*end))
                proposed.SetWidth(width)
                proposed.SetLayer(layer_id)
                profile = profile_for(piece) if profile_for and not via else None
                shape = None
                for obstacle, part in by_layer[layer]:
                    minimum = (
                        obstacle.min_via_clearance
                        if via
                        else obstacle.min_track_clearance
                    )
                    if (
                        obstacle.net == piece.net
                        and obstacle.kind != "keepout"
                        and (not via or (obstacle.kind != "via" and not minimum))
                    ):
                        continue
                    required = obstacle.gap(
                        max(
                            clearance,
                            profile.clearance_at(segment, obstacle, width=width)
                            if profile
                            else 0,
                        ),
                        via=via,
                    )
                    if not GridRouter._intersects_box(
                        segment.start,
                        segment.end,
                        obstacle,
                        (width + 1) // 2 + required,
                    ):
                        continue
                    if obstacle.kind in {"keepout", "via"}:
                        return False
                    if isinstance(part, pcbnew.PAD):
                        drill = part.GetDrillSize()
                        if drill.x or drill.y:
                            return False
                    key = part.m_Uuid.AsString(), layer
                    if key not in shapes:
                        shapes[key] = part.GetEffectiveShape(layer_id)
                    if shape is None:
                        shape = proposed.GetEffectiveShape(layer_id)
                    if shapes[key].Collide(shape, required):
                        return False
        return True

    return clear


def shortcut_clearance_checker(obstacles: tuple[Obstacle, ...], *, profile_for=None):
    """Reject obvious shortcut collisions; native DRC remains authoritative."""
    by_layer = {layer: [] for layer in COPPER_LAYERS}
    for obstacle in obstacles:
        if not isinstance(obstacle, Obstacle):
            raise ValueError("Shortcut obstacles must be conservative copper envelopes")
        for layer in obstacle.layers:
            by_layer[layer].append(obstacle)
    if profile_for is not None and not callable(profile_for):
        raise ValueError("Shortcut rule lookup must be callable")

    def clear(traces: tuple[Trace, ...]) -> bool:
        for item in traces:
            segment = _segment(item)
            profile = profile_for(item) if profile_for else None
            maximum = profile.maximum_clearance if profile else 0
            radius = (item.width + 1) // 2
            for obstacle in by_layer[item.layer]:
                if (
                    obstacle.net
                    and obstacle.net == item.net
                    and obstacle.kind != "keepout"
                ):
                    continue
                reach = radius + max(maximum, obstacle.min_track_clearance)
                if (
                    max(item.start[0], item.end[0]) < obstacle.lower.x - reach
                    or min(item.start[0], item.end[0]) > obstacle.upper.x + reach
                    or max(item.start[1], item.end[1]) < obstacle.lower.y - reach
                    or min(item.start[1], item.end[1]) > obstacle.upper.y + reach
                ):
                    continue
                clearance = (
                    profile.clearance_at(segment, obstacle, width=item.width)
                    if profile
                    else 0
                )
                margin = radius + obstacle.gap(clearance)
                if GridRouter._intersects_box(
                    segment.start, segment.end, obstacle, margin
                ):
                    return False
        return True

    return clear


def screen_reference_regions(
    delta: Delta,
    regions: tuple[Obstacle, ...],
    *,
    profile_for=None,
    reference_net: str = "GND",
) -> tuple[Delta, tuple[str, ...]]:
    """Exclude whole proposed nets that would cut local high-speed reference regions."""
    if not isinstance(delta, Delta):
        raise ValueError("A reference screen needs a copper proposal")
    if not isinstance(reference_net, str) or not reference_net:
        raise ValueError("A reference screen requires its return net")
    clear_track = shortcut_clearance_checker(regions, profile_for=profile_for)
    blocked = {
        trace.net
        for trace in delta.traces
        if trace.net != reference_net and not clear_track((trace,))
    }
    for via in delta.vias:
        if via.net == reference_net:
            continue
        position = Point(*via.at)
        if any(
            GridRouter._intersects_box(
                position,
                position,
                region,
                (via.size + 1) // 2 + region.min_via_clearance,
            )
            for region in regions
        ):
            blocked.add(via.net)
    return without_nets(delta, blocked), tuple(sorted(blocked))


def _same_net_collinear_overlap(first: Trace, second: Trace) -> bool:
    """Identify positive-length overlap, while allowing a shared endpoint."""
    if first.net != second.net or first.layer != second.layer:
        return False
    dx = first.end[0] - first.start[0]
    dy = first.end[1] - first.start[1]
    if not (dx or dy):
        return False
    for point in (second.start, second.end):
        if dx * (point[1] - first.start[1]) != dy * (point[0] - first.start[0]):
            return False
    axis = 0 if abs(dx) >= abs(dy) else 1
    low = max(
        min(first.start[axis], first.end[axis]),
        min(second.start[axis], second.end[axis]),
    )
    high = min(
        max(first.start[axis], first.end[axis]),
        max(second.start[axis], second.end[axis]),
    )
    return low < high


def merge_proposed_collinear(
    traces: tuple[Trace, ...], original: list[Trace | Via], is_anchor
) -> tuple[Trace, ...]:
    """Merge proposal-only joints without changing their physical copper."""
    if not callable(is_anchor):
        raise ValueError("Collinear cleanup needs an anchor check")
    pending = set(traces)
    if len(pending) != len(traces):
        return tuple(sorted(traces))  # Leave duplicate-copper findings to validation.
    old = defaultdict(list)
    for item in original:
        if isinstance(item, Trace):
            old[(item.net, item.layer)].append(item)
    while True:
        endpoints = defaultdict(list)
        for item in pending:
            endpoints[(item.net, item.layer, item.start)].append(item)
            endpoints[(item.net, item.layer, item.end)].append(item)
        merged = False
        for (net, layer, point), items in sorted(endpoints.items()):
            if (
                len(items) != 2
                or items[0].width != items[1].width
                or is_anchor(net, layer, point)
            ):
                continue
            first, second = items
            if not (_octilinear(first) and _octilinear(second)):
                continue
            a = first.end if first.start == point else first.start
            b = second.end if second.start == point else second.start
            ax, ay = a[0] - point[0], a[1] - point[1]
            bx, by = b[0] - point[0], b[1] - point[1]
            if ax * by != ay * bx or ax * bx + ay * by >= 0:
                continue
            contacts = [
                *old[(net, layer)],
                *(
                    item
                    for item in pending
                    if item not in items and item.net == net and item.layer == layer
                ),
            ]
            if any(on_segment(Point(*point), _segment(item)) for item in contacts):
                continue
            replacement = Trace(net, layer, first.width, a, b)
            if replacement in pending:
                continue
            pending.difference_update(items)
            pending.add(replacement)
            merged = True
            break
        if not merged:
            return tuple(sorted(pending))


def normalize_repaired_trial(
    trial: Delta,
    previous: Delta,
    replacement: tuple[Trace, ...],
    original: list[Trace | Via],
    is_anchor,
) -> tuple[Delta, tuple[Trace, ...]]:
    """Audit merged replacement copper, including pieces spanning old proposal joins."""
    merged = merge_proposed_collinear(trial.traces, original, is_anchor)
    changed = (set(merged) - set(previous.traces)) | (set(merged) & set(replacement))
    return Delta(merged, trial.vias, trial.excluded_nets, trial.unsupported), tuple(
        sorted(changed)
    )


def simplify_terminal_chains(
    traces: tuple[Trace, ...],
    original: list[Trace | Via],
    is_anchor,
    *,
    shortcut_allowed=None,
    may_continue=None,
    materialize=None,
) -> tuple[Trace, ...]:
    """Replace an unbranched new chain with a shorter octilinear connection."""
    if (
        not callable(is_anchor)
        or (shortcut_allowed is not None and not callable(shortcut_allowed))
        or (may_continue is not None and not callable(may_continue))
        or (materialize is not None and not callable(materialize))
    ):
        raise ValueError("Valid anchor, clearance and budget checks are required")
    if may_continue is not None and not may_continue():
        return tuple(sorted(traces))
    groups = defaultdict(list)
    for item in traces:
        groups[(item.net, item.layer, None if materialize else item.width)].append(item)
    old = defaultdict(list)
    for item in original:
        if isinstance(item, Trace):
            old[(item.net, item.layer)].append(item)
    output = []
    for (net, layer, width), items in sorted(groups.items()):
        adjacency = defaultdict(list)
        for item in items:
            adjacency[item.start].append(item)
            adjacency[item.end].append(item)
        pending = set(items)
        while pending:
            seed = min(pending)
            component = set()
            stack = [seed]
            while stack:
                item = stack.pop()
                if item in component:
                    continue
                component.add(item)
                for point in (item.start, item.end):
                    stack.extend(
                        other for other in adjacency[point] if other not in component
                    )
            pending.difference_update(component)
            degree = Counter(
                point for item in component for point in (item.start, item.end)
            )
            ends = sorted(point for point, count in degree.items() if count == 1)
            if (
                len(component) < 3
                or len(ends) != 2
                or any(count > 2 for count in degree.values())
            ):
                output.extend(component)
                continue
            ordered = [ends[0]]
            used = set()
            while ordered[-1] != ends[1]:
                options = [
                    item
                    for item in adjacency[ordered[-1]]
                    if item in component and item not in used
                ]
                if len(options) != 1:
                    break
                item = options[0]
                used.add(item)
                ordered.append(item.end if item.start == ordered[-1] else item.start)
            if len(used) != len(component):
                output.extend(component)
                continue
            internal = set(ordered[1:-1])
            if any(
                is_anchor(net, layer, point)
                or any(
                    on_segment(Point(*point), _segment(existing))
                    for existing in old[(net, layer)]
                )
                for point in internal
            ):
                output.extend(component)
                continue
            # Existing copper or another proposed width touching the interior
            # is an additional connection, not a movable intermediate bend.
            contacts = [
                *old[(net, layer)],
                *(
                    item
                    for item in traces
                    if item.net == net and item.layer == layer and item not in component
                ),
            ]
            unsafe = False
            for item in component:
                for other in contacts:
                    first, second = _segment(item), _segment(other)
                    try:
                        crossing = intersection(first, second)
                    except RouteGeometryError:
                        unsafe = True
                        break
                    points = {
                        point
                        for point in (item.start, item.end, other.start, other.end)
                        if on_segment(Point(*point), first)
                        and on_segment(Point(*point), second)
                    }
                    if crossing is not None:
                        points.add((crossing.x, crossing.y))
                    if points - set(ends):
                        unsafe = True
                        break
                if unsafe:
                    break
            if unsafe:
                output.extend(component)
                continue
            start, end = ends
            dx, dy = end[0] - start[0], end[1] - start[1]
            sx, sy = (dx > 0) - (dx < 0), (dy > 0) - (dy < 0)
            distance = min(abs(dx), abs(dy))
            paths = [(start, end)]
            if dx and dy and abs(dx) != abs(dy):
                paths = [
                    (start, (start[0] + sx * distance, start[1] + sy * distance), end),
                    (start, (end[0] - sx * distance, end[1] - sy * distance), end),
                ]

            def endpoint_ok(point, other):
                if is_anchor(net, layer, point):
                    return True
                touching = [
                    item
                    for item in old[(net, layer)]
                    if on_segment(Point(*point), _segment(item))
                ]
                # A contact in the middle of existing copper is a branch.
                if len(touching) != 1 or point not in (
                    touching[0].start,
                    touching[0].end,
                ):
                    return True
                existing = (
                    touching[0].end if touching[0].start == point else touching[0].start
                )
                a = existing[0] - point[0], existing[1] - point[1]
                b = other[0] - point[0], other[1] - point[1]
                dot = a[0] * b[0] + a[1] * b[1]
                cross = a[0] * b[1] - a[1] * b[0]
                return dot != 0 and cross != 0

            choices = []
            old_length = sum(
                math.hypot(item.end[0] - item.start[0], item.end[1] - item.start[1])
                for item in component
            )
            seed_width = (
                width if width is not None else min(item.width for item in component)
            )

            def consider(alternatives):
                for path in alternatives:
                    if may_continue is not None and not may_continue():
                        break
                    candidate = tuple(
                        Trace(net, layer, seed_width, a, b)
                        for a, b in zip(path, path[1:])
                    )
                    if materialize is not None:
                        try:
                            candidate = tuple(materialize(candidate))
                        except ValueError:
                            continue
                        if any(
                            (item.net, item.layer) != (net, layer)
                            or item.width < seed_width
                            for item in candidate
                        ) or chain_endpoints(candidate, variable_width=True) != tuple(
                            ends
                        ):
                            continue
                    length = sum(
                        math.hypot(
                            item.end[0] - item.start[0], item.end[1] - item.start[1]
                        )
                        for item in candidate
                    )
                    if (
                        len(candidate) < len(component)
                        and length <= old_length + 1e-6
                        and all(_octilinear(item) for item in candidate)
                        and endpoint_ok(path[0], path[1])
                        and endpoint_ok(path[-1], path[-2])
                        and not any(
                            _same_net_collinear_overlap(item, other)
                            for item in candidate
                            for other in contacts
                        )
                        and (shortcut_allowed is None or shortcut_allowed(candidate))
                    ):
                        choices.append((len(candidate), length, candidate))

            consider(paths)
            if not choices and dx and dy and abs(dx) != abs(dy) and len(component) > 3:
                # Move the diagonal transition between its two shortest-path
                # extremes, refining between coarse samples when needed.
                # Length remains unchanged; every sample still has to pass
                # the supplied clearance check and later native validation.
                free = max(abs(dx), abs(dy)) - distance
                seen_offsets = {0, free}
                for divisions in (16, 32, 64, 128):
                    if choices or (may_continue is not None and not may_continue()):
                        break
                    offsets = sorted(
                        {free * index // divisions for index in range(1, divisions)}
                        - seen_offsets
                    )
                    seen_offsets.update(offsets)
                    for offset in offsets:
                        if choices or (may_continue is not None and not may_continue()):
                            break
                        first = (
                            (start[0] + sx * offset, start[1])
                            if abs(dx) > abs(dy)
                            else (start[0], start[1] + sy * offset)
                        )
                        second = (first[0] + sx * distance, first[1] + sy * distance)
                        consider(((start, first, second, end),))
            output.extend(min(choices)[2] if choices else component)
    return tuple(sorted(output))


def chain_endpoints(
    traces: tuple[Trace, ...], *, variable_width: bool = False
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Find endpoints of an unbranched chain; width transitions require opt-in."""
    if not traces or len(set(traces)) != len(traces):
        return None
    if (
        len(
            {
                (item.net, item.layer, None if variable_width else item.width)
                for item in traces
            }
        )
        != 1
    ):
        return None
    adjacency = defaultdict(set)
    for item in traces:
        if not _octilinear(item):
            return None
        adjacency[item.start].add(item.end)
        adjacency[item.end].add(item.start)
    ends = sorted(
        point for point, neighbors in adjacency.items() if len(neighbors) == 1
    )
    if len(ends) != 2 or any(len(neighbors) > 2 for neighbors in adjacency.values()):
        return None
    seen = {ends[0]}
    stack = [ends[0]]
    while stack:
        for neighbor in adjacency[stack.pop()] - seen:
            seen.add(neighbor)
            stack.append(neighbor)
    return tuple(ends) if len(seen) == len(adjacency) else None


def simplify_local_windows(
    traces: tuple[Trace, ...],
    original: list[Trace | Via],
    is_anchor,
    *,
    shortcut_allowed=None,
    max_windows: int = 128,
    may_continue=None,
    materialize=None,
) -> tuple[Trace, ...]:
    """Remove local proposed staircases without moving any saved contacts.

    A full chain may need to go around an obstacle even when a small section
    can be shortened. Mixed widths require an explicit saved-rule materializer;
    the entire revised chain must have fewer defects and no new right angles or
    redundant joints. Native clearance and connectivity checks remain required.
    """
    if (
        not callable(is_anchor)
        or (shortcut_allowed is not None and not callable(shortcut_allowed))
        or (materialize is not None and not callable(materialize))
    ):
        raise ValueError("Local cleanup needs valid anchor and clearance checks")
    if (
        isinstance(max_windows, bool)
        or not isinstance(max_windows, int)
        or max_windows < 0
        or (may_continue is not None and not callable(may_continue))
    ):
        raise ValueError("Local cleanup needs a bounded window count and budget check")

    def ordered(component):
        ends = chain_endpoints(tuple(component), variable_width=materialize is not None)
        if ends is None:
            return None
        adjacency = defaultdict(list)
        for item in component:
            adjacency[item.start].append(item)
            adjacency[item.end].append(item)
        point, used, result = ends[0], set(), []
        while point != ends[1]:
            choices = [item for item in adjacency[point] if item not in used]
            if len(choices) != 1:
                return None
            item = choices[0]
            following = item.end if item.start == point else item.start
            result.append((item, point, following))
            used.add(item)
            point = following
        return result if len(used) == len(component) else None

    def defects(chain):
        segments = [
            Segment(Point(*start), Point(*end), item.layer)
            for item, start, end in chain
        ]
        headings = [
            GridRouter._direction(segment.start, segment.end) for segment in segments
        ]
        turns = [(second - first) % 8 for first, second in zip(headings, headings[1:])]
        return (
            sum(turn in {2, 4, 6} for turn in turns),
            GridRouter.staircase_count(segments),
            sum(
                turn == 0 and chain[i][0].width == chain[i + 1][0].width
                for i, turn in enumerate(turns)
            ),
        )

    result = tuple(sorted(traces))
    attempts = 0
    while attempts < max_windows and (may_continue is None or may_continue()):
        pending = set(result)
        improved = False
        while pending and attempts < max_windows:
            component = connected_trace_component(
                min(pending), result, variable_width=materialize is not None
            )
            pending.difference_update(component)
            chain = ordered(component)
            if chain is None or len(chain) < 3:
                continue
            before = defects(chain)
            if not (before[0] or before[1]):
                continue
            for length in range(min(10, len(chain)), 2, -1):
                for start in range(len(chain) - length + 1):
                    if attempts >= max_windows or (
                        may_continue is not None and not may_continue()
                    ):
                        return result
                    attempts += 1
                    window = tuple(item for item, _, _ in chain[start : start + length])
                    window_set = set(window)
                    outside = [item for item in result if item not in window_set]
                    net, layer = window[0].net, window[0].layer
                    old_contacts = [
                        item
                        for item in original
                        if isinstance(item, Trace)
                        and item.net == net
                        and item.layer == layer
                    ]
                    contacts = old_contacts + [
                        item
                        for item in outside
                        if item.net == net and item.layer == layer
                    ]
                    ends = {chain[start][1], chain[start + length - 1][2]}
                    candidate = simplify_terminal_chains(
                        window,
                        contacts,
                        lambda name, layer_name, point: (
                            point in ends or is_anchor(name, layer_name, point)
                        ),
                        shortcut_allowed=shortcut_allowed,
                        may_continue=may_continue,
                        materialize=materialize,
                    )
                    if len(candidate) >= len(window):
                        continue
                    revised = (
                        tuple(item for item in component if item not in window_set)
                        + candidate
                    )
                    revised_chain = ordered(revised)
                    if revised_chain is None:
                        continue
                    after = defects(revised_chain)
                    if (
                        after[0] > before[0]
                        or after[1] > before[1]
                        or after[2] > before[2]
                        or (after[0], after[1]) == (before[0], before[1])
                    ):
                        continue
                    result = tuple(sorted((*outside, *candidate)))
                    improved = True
                    break
                if improved:
                    break
            if improved:
                break
        if not improved:
            break
    return result


def connected_trace_component(
    seed: Trace, traces: tuple[Trace, ...], *, variable_width: bool = False
) -> tuple[Trace, ...]:
    """Collect same-net, same-layer traces joined at exact endpoints."""
    group = [
        item
        for item in traces
        if (item.net, item.layer) == (seed.net, seed.layer)
        and (variable_width or item.width == seed.width)
    ]
    if seed not in group:
        raise ValueError("The repair seed is not a proposed trace")
    adjacency = defaultdict(list)
    for item in group:
        adjacency[item.start].append(item)
        adjacency[item.end].append(item)
    found = set()
    pending = [seed]
    while pending:
        item = pending.pop()
        if item in found:
            continue
        found.add(item)
        for point in (item.start, item.end):
            pending.extend(other for other in adjacency[point] if other not in found)
    return tuple(sorted(found))


def dangling_trace_remainder(
    seed: Trace, traces: tuple[Trace, ...], original: list[Trace | Via], is_anchor
) -> tuple[Trace, ...]:
    """Prune an unbranched rejected remainder without altering old copper contacts."""
    chain = connected_trace_component(seed, traces)
    ends = chain_endpoints(chain)
    if ends is None:
        return (seed,)
    internal = {point for item in chain for point in (item.start, item.end)} - set(ends)
    if any(is_anchor(seed.net, seed.layer, point) for point in internal):
        return (seed,)
    for item in chain:
        for old in original:
            if (
                not isinstance(old, Trace)
                or old.net != seed.net
                or old.layer != seed.layer
            ):
                continue
            try:
                crossing = intersection(_segment(item), _segment(old))
            except RouteGeometryError:
                return (seed,)
            contacts = {
                point
                for point in (item.start, item.end, old.start, old.end)
                if on_segment(Point(*point), _segment(item))
                and on_segment(Point(*point), _segment(old))
            }
            if crossing is not None:
                contacts.add((crossing.x, crossing.y))
            if contacts - set(ends):
                return (seed,)
    return chain


def repairable_subchain(
    seed: Trace,
    traces: tuple[Trace, ...],
    rejected: set[Trace | Via],
    original: list[Trace | Via],
    is_anchor,
) -> tuple[Trace, ...]:
    """Choose connected rejected copper while retaining every neighboring contact."""
    if seed not in rejected or seed not in traces:
        raise ValueError("A repair seed must be rejected proposed copper")
    selected = tuple(item for item in traces if item in rejected)
    untouched = [item for item in traces if item not in rejected]
    return dangling_trace_remainder(seed, selected, [*original, *untouched], is_anchor)


def expand_repairable_subchain(
    seed: Trace,
    traces: tuple[Trace, ...],
    rejected: set[Trace | Via],
    original: list[Trace | Via],
    is_anchor,
    *,
    max_extra: int = 4,
) -> tuple[Trace, ...]:
    """Widen a rejected chain through safe proposal-only neighbors.

    Pads, vias, old copper, crossings and branches remain fixed. The complete
    replacement still requires native DRC, style and connectivity checks.
    """
    if isinstance(max_extra, bool) or not isinstance(max_extra, int) or max_extra < 0:
        raise ValueError("The repair expansion limit must be a nonnegative integer")
    selected = set(repairable_subchain(seed, traces, rejected, original, is_anchor))
    for _ in range(max_extra):
        ends = chain_endpoints(tuple(selected))
        if ends is None:
            break
        expanded = False
        for end in ends:
            if is_anchor(seed.net, seed.layer, end):
                continue
            adjacent = [
                item
                for item in traces
                if item not in selected
                and (item.net, item.layer, item.width)
                == (seed.net, seed.layer, seed.width)
                and end in (item.start, item.end)
            ]
            if len(adjacent) != 1:
                continue
            proposed = selected | {adjacent[0]}
            rest = [item for item in traces if item not in proposed]
            safe = dangling_trace_remainder(
                seed, tuple(proposed), [*original, *rest], is_anchor
            )
            if set(safe) == proposed:
                selected = proposed
                expanded = True
                break
        if not expanded:
            break
    return tuple(sorted(selected))


def prioritize_expanded_repair(
    queue: deque, seed: Trace, narrow: tuple[Trace, ...], expanded: tuple[Trace, ...]
) -> None:
    """Try a distinct wider window before unrelated repairs exhaust the budget."""
    if set(expanded) != set(narrow):
        queue.appendleft((seed, True))


def endpoint_directions(
    net: str,
    layer: str,
    point: tuple[int, int],
    original: list[Trace | Via],
    reverse: bool,
) -> tuple[str, ...] | None:
    """Keep new bends compatible with a single old track at an endpoint."""
    touching = [
        item
        for item in original
        if isinstance(item, Trace)
        and item.net == net
        and item.layer == layer
        and on_segment(Point(*point), _segment(item))
    ]
    if len(touching) != 1 or point not in (touching[0].start, touching[0].end):
        return None
    old = touching[0]
    if not _octilinear(old):
        return ()
    other = old.end if old.start == point else old.start
    ox, oy = other[0] - point[0], other[1] - point[1]
    names = ("E", "SE", "S", "SW", "W", "NW", "N", "NE")
    vectors = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))

    def permissible(dx, dy):
        dot, cross = ox * dx + oy * dy, ox * dy - oy * dx
        # A straight continuation points opposite the existing outgoing
        # segment. Reject right angles and retracing along the existing ray.
        return dot != 0 and (cross != 0 or dot < 0)

    return tuple(
        names[(index + 4) % 8 if reverse else index]
        for index, (dx, dy) in enumerate(vectors)
        if permissible(dx, dy)
    )


def initial_heading(
    traces: tuple[Trace, ...], endpoint: tuple[int, int], layer: str
) -> str:
    """Return the unique outgoing heading at an unbranched repair endpoint."""
    from pcb.route_optimizer import GridRouter

    touching = [
        item
        for item in traces
        if item.layer == layer and endpoint in (item.start, item.end)
    ]
    if len(touching) != 1:
        raise ValueError("A repaired endpoint has no unique outgoing trace")
    trace = touching[0]
    other = trace.end if trace.start == endpoint else trace.start
    direction = GridRouter._direction(Point(*endpoint), Point(*other))
    return ("E", "SE", "S", "SW", "W", "NW", "N", "NE")[direction]


def retry_limited_search(
    search,
    *,
    first_limit: int = 15_000,
    retry_limit: int = 150_000,
    may_retry=None,
    on_retry=None,
):
    """Retry a search-limit result once; do not reinterpret it as no path."""
    if not callable(search) or (may_retry is not None and not callable(may_retry)):
        raise ValueError("Search and retry-budget checks must be callable")
    if on_retry is not None and not callable(on_retry):
        raise ValueError("Retry progress callback must be callable")
    if (
        isinstance(first_limit, bool)
        or not isinstance(first_limit, int)
        or first_limit < 1
        or isinstance(retry_limit, bool)
        or not isinstance(retry_limit, int)
        or retry_limit < first_limit
    ):
        raise ValueError("Search limits must be positive ordered integers")
    try:
        return search(first_limit)
    except SearchLimitReached:
        if retry_limit == first_limit or (may_retry is not None and not may_retry()):
            raise
        if on_retry is not None:
            on_retry(retry_limit)
        return search(retry_limit)


def retry_local_grids(
    search,
    *,
    may_retry=None,
    on_retry=None,
    expansion_limits: tuple[int, ...] = (15_000, 150_000, 400_000),
):
    """Try coarse and fine access before spending a larger per-grid budget."""
    if not callable(search) or (may_retry is not None and not callable(may_retry)):
        raise ValueError("Local grid search and budget checks must be callable")
    if on_retry is not None and not callable(on_retry):
        raise ValueError("Local grid progress must be callable")
    if (
        not isinstance(expansion_limits, tuple)
        or not expansion_limits
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in expansion_limits
        )
        or any(a >= b for a, b in zip(expansion_limits, expansion_limits[1:]))
    ):
        raise ValueError(
            "Local grid expansion limits must be positive and strictly increasing"
        )
    steps = (0.2, 0.1, 0.05)
    blocked, unavailable, limited = set(), set(), set()
    expanded, attempted = 0, False
    for limit in expansion_limits:
        for step in steps:
            if step in blocked or step in unavailable:
                continue
            if attempted and may_retry is not None and not may_retry():
                raise SearchBudgetExceeded("Cleanup time budget reached")
            if attempted and on_retry is not None:
                on_retry(step, limit)
            attempted = True
            try:
                return search(step, limit)
            except NoGridRoute as error:
                expanded += error.expanded
                blocked.add(step)
                limited.discard(step)
            except SearchLimitReached as error:
                expanded += error.expanded
                limited.add(step)
            except ValueError as error:
                if (
                    str(error)
                    != "Grid exceeds max_cells; increase the step or reduce the bounds"
                ):
                    raise
                unavailable.add(step)
    if limited or unavailable:
        raise SearchLimitReached(
            "Local grid searches reached their limits; the route remains unknown",
            expanded=expanded,
        )
    raise NoGridRoute("No route found in the tested local grids", expanded=expanded)


def prefer_no_via_repair(search, *, may_retry=None):
    """Try the chain's existing layer, then allow every copper layer equally."""
    if not callable(search) or (may_retry is not None and not callable(may_retry)):
        raise ValueError("Search and budget checks must be callable")
    try:
        return search(True)
    except (NoGridRoute, SearchLimitReached):
        if may_retry is not None and not may_retry():
            raise SearchBudgetExceeded("Cleanup time budget reached")
        return search(False)


def proposed_via_obstacles(vias: tuple[Via, ...], board) -> list[dict]:
    """Include private proposal vias in local search, using native hole rules."""
    if not isinstance(vias, tuple):
        raise ValueError("Proposed vias must be a tuple")
    if not vias:
        return []
    settings = board.GetDesignSettings()
    hole_clearance = int(settings.m_HoleClearance)
    hole_spacing = int(settings.m_HoleToHoleMin)
    if hole_clearance < 0 or hole_spacing < 0:
        raise ValueError("Native hole clearances must be nonnegative")
    result = []
    for via in vias:
        if (
            not isinstance(via, Via)
            or not isinstance(via.net, str)
            or not via.net
            or len(via.at) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (*via.at, via.size, via.drill)
            )
            or via.drill <= 0
            or via.size <= via.drill
        ):
            raise ValueError(
                "A proposed via needs valid net, coordinates and drill dimensions"
            )
        x, y = via.at
        # Cover KiCad's inclusive upper bounding-box edge symmetrically so
        # the obstacle retains the actual via center for safe reuse.
        radius = (via.size + 1) // 2 + 1
        annular = (via.size - via.drill) // 2
        result.append(
            {
                "bounds_mm": [
                    (x - radius) / 1e6,
                    (y - radius) / 1e6,
                    (x + radius) / 1e6,
                    (y + radius) / 1e6,
                ],
                "layers": [
                    board.GetLayerName(layer)
                    for layer in board.GetEnabledLayers().CuStack()
                ],
                "net": via.net,
                "kind": "via",
                "min_track_clearance_mm": max(0, hole_clearance - annular) / 1e6,
                "min_via_clearance_mm": max(0, hole_spacing - annular) / 1e6,
            }
        )
    return result


def proposed_trace_obstacles(traces: tuple[Trace, ...]) -> list[dict]:
    """Model other proposed copper as conservative per-layer capsules."""
    if not isinstance(traces, tuple):
        raise ValueError("Proposed traces must be a tuple")
    result = []
    for trace in traces:
        if (
            not isinstance(trace, Trace)
            or trace.width <= 0
            or trace.layer not in COPPER_LAYERS
        ):
            raise ValueError("Invalid proposed trace")
        x1, y1 = trace.start
        dx, dy = trace.end[0] - x1, trace.end[1] - y1
        count = max(1, math.ceil(max(abs(dx), abs(dy)) / 2_000_000))
        radius = (trace.width + 1) // 2 + 2
        for index in range(count):
            a = (x1 + dx * index // count, y1 + dy * index // count)
            b = (x1 + dx * (index + 1) // count, y1 + dy * (index + 1) // count)
            result.append(
                {
                    "bounds_mm": [
                        (min(a[0], b[0]) - radius) / 1e6,
                        (min(a[1], b[1]) - radius) / 1e6,
                        (max(a[0], b[0]) + radius) / 1e6,
                        (max(a[1], b[1]) + radius) / 1e6,
                    ],
                    "centerline_mm": [
                        [a[0] / 1e6, a[1] / 1e6],
                        [b[0] / 1e6, b[1] / 1e6],
                    ],
                    "radius_mm": radius / 1e6,
                    "layers": [trace.layer],
                    "net": trace.net,
                    "kind": "copper",
                }
            )
    return result


def local_context_checker(net: str, original: tuple[Trace | Via, ...], is_anchor):
    """Reject local paths that add forbidden bends or staircases to nearby copper."""
    from pcb.net_routing.geometry import aggregate
    from pcb.net_routing.model import Site
    from pcb.route_optimizer import Weights

    existing = tuple(
        _segment(item)
        for item in original
        if isinstance(item, Trace) and item.net == net and _octilinear(item)
    )

    def check(route):
        segments = (*existing, *route.segments)
        anchors = frozenset(
            Site(point, segment.layer)
            for segment in segments
            for point in (segment.start, segment.end)
            if is_anchor(net, segment.layer, (point.x, point.y))
        )
        try:
            aggregate(
                (route,),
                existing=existing,
                weights=Weights(),
                layer_penalties={},
                guide=(),
                pad_sites=anchors,
                forbid_staircase=True,
            )
        except ValueError:
            return False
        return True

    return check


def local_chain_repair(
    project: Path,
    chain: tuple[Trace, ...],
    original: list[Trace | Via],
    board,
    is_anchor,
    *,
    max_expansions: int = 15_000,
    excluded_initial: frozenset[str] = frozenset(),
    heuristic_weight: float = 1.0,
    step_mm: float = 0.2,
    same_layer_only: bool = False,
    check_budget=None,
    native_cache=None,
    proposed_vias: tuple[Via, ...] = (),
    proposed_traces: tuple[Trace, ...] = (),
) -> tuple[tuple[Trace, ...], tuple[Via, ...]]:
    """Ask the existing obstacle-aware grid router for a bounded replacement."""
    from core.pcb_io import net_classes
    from pcb.route_cli import plan

    ends = chain_endpoints(chain)
    if ends is None:
        raise ValueError("A local repair needs one unbranched trace chain")
    net, layer, width = chain[0].net, chain[0].layer, chain[0].width
    internal = {point for item in chain for point in (item.start, item.end)} - set(ends)
    if any(
        is_anchor(net, layer, point)
        or any(
            isinstance(item, Trace)
            and item.net == net
            and item.layer == layer
            and on_segment(Point(*point), _segment(item))
            for item in original
        )
        for point in internal
    ):
        raise ValueError("The local repair would move an existing copper contact")
    for item in chain:
        for old in original:
            if not isinstance(old, Trace) or old.net != net or old.layer != layer:
                continue
            try:
                crossing = intersection(_segment(item), _segment(old))
            except RouteGeometryError as error:
                raise ValueError(
                    "A local repair has an ambiguous old-copper contact"
                ) from error
            contacts = {
                point
                for point in (item.start, item.end, old.start, old.end)
                if on_segment(Point(*point), _segment(item))
                and on_segment(Point(*point), _segment(old))
            }
            if crossing is not None:
                contacts.add((crossing.x, crossing.y))
            if contacts - set(ends):
                raise ValueError(
                    "The local repair would alter an existing copper contact"
                )
    info = board.FindNet(net)
    classes = net_classes(board)
    if info is None or str(info.GetNetClassName()) not in classes:
        raise ValueError(f"The net class for {net} is unavailable")
    netclass = classes[str(info.GetNetClassName())]
    a, b = ends
    margin = 3_000_000
    config = {
        "net": net,
        "board_snapshot": "pcb",
        "start": {"at_mm": [a[0] / 1e6, a[1] / 1e6], "layer": layer},
        "end": {"at_mm": [b[0] / 1e6, b[1] / 1e6], "layer": layer},
        "bounds_mm": [
            (min(a[0], b[0]) - margin) / 1e6,
            (min(a[1], b[1]) - margin) / 1e6,
            (max(a[0], b[0]) + margin) / 1e6,
            (max(a[1], b[1]) + margin) / 1e6,
        ],
        "step_mm": step_mm,
        "layers": [layer]
        if same_layer_only
        else [
            board.GetLayerName(identifier)
            for identifier in board.GetEnabledLayers().CuStack()
        ],
        "width_mm": width / 1e6,
        "clearance_mm": netclass.GetClearance() / 1e6,
        "via": {
            "size_mm": netclass.GetViaDiameter() / 1e6,
            "drill_mm": netclass.GetViaDrill() / 1e6,
        },
        "assume_zone_refill": True,
        "max_cells": 250_000,
        "max_expansions": max_expansions,
        "gentle_turns": False,
        "forbid_staircase": True,
        "heuristic_weight": heuristic_weight,
        "first_feasible": True,
        "obstacles": [
            *proposed_via_obstacles(proposed_vias, board),
            *proposed_trace_obstacles(proposed_traces),
        ],
    }
    for field, point, reverse in (
        ("initial_directions", a, False),
        ("final_directions", b, True),
    ):
        allowed = (
            None
            if is_anchor(net, layer, point)
            else endpoint_directions(net, layer, point, original, reverse)
        )
        if allowed == ():
            raise ValueError("A local endpoint touches unsupported off-angle copper")
        if field == "initial_directions" and excluded_initial:
            allowed = tuple(
                name
                for name in (allowed or ("E", "SE", "S", "SW", "W", "NW", "N", "NE"))
                if name not in excluded_initial
            )
            if not allowed:
                raise ValueError("All eligible initial repair directions were tried")
        if allowed:
            config[field] = list(allowed)
    result = plan(
        config,
        project,
        check_budget=check_budget,
        native_cache=native_cache,
        candidate_check=local_context_checker(net, tuple(original), is_anchor),
    )
    traces = tuple(
        Trace(
            net,
            item["layer"],
            mm(item["width_mm"]),
            tuple(mm(value) for value in item["from_mm"]),
            tuple(mm(value) for value in item["to_mm"]),
        )
        for item in result["segments"]
    )
    vias = tuple(
        Via(
            net,
            tuple(mm(value) for value in item["at_mm"]),
            mm(item["size_mm"]),
            mm(item["drill_mm"]),
        )
        for item in result["vias"]
    )
    if not traces:
        raise ValueError("Local repair produced no trace")
    return traces, vias


def native_pad_checker(board):
    """Identify existing pad contacts without confusing them with via copper."""
    import pcbnew

    pads = defaultdict(list)
    for pad in board.GetPads():
        pads[pad.GetNetname()].append(pad)

    def is_pad(net: str, layer: str, point: tuple[int, int]) -> bool:
        layer_id = board.GetLayerID(layer)
        location = pcbnew.VECTOR2I(*point)
        return any(
            pad.IsOnLayer(layer_id) and pad.HitTest(location) for pad in pads[net]
        )

    return is_pad


def native_via_move_checker(board):
    """Refuse moving a proposed via whose old or new copper touches a pad."""
    import pcbnew

    pads = []
    for pad in board.GetPads():
        for layer in board.GetEnabledLayers().CuStack():
            if pad.IsOnLayer(layer):
                bounds = pad.GetBoundingBox(layer)
                pads.append(
                    (
                        pad,
                        layer,
                        bounds.GetX(),
                        bounds.GetY(),
                        bounds.GetRight(),
                        bounds.GetBottom(),
                    )
                )

    def allowed(old: Via, new: Via) -> bool:
        if (old.net, old.size, old.drill) != (new.net, new.size, new.drill):
            return False
        radius = (old.size + 1) // 2
        for point in (old.at, new.at):
            for pad, layer, left, top, right, bottom in pads:
                if (
                    left - radius <= point[0] <= right + radius
                    and top - radius <= point[1] <= bottom + radius
                    and pad.HitTest(pcbnew.VECTOR2I(*point), radius, layer)
                ):
                    return False
        return True

    return allowed


def native_anchor_checker(board, added_vias: tuple[Via, ...]):
    """Preserve contacts with existing pads and both existing and proposed vias."""
    import pcbnew

    is_pad = native_pad_checker(board)
    vias = defaultdict(list)
    for item in board.GetTracks():
        if isinstance(item, pcbnew.PCB_VIA):
            vias[item.GetNetname()].append(item)
    added = defaultdict(list)
    for item in added_vias:
        added[item.net].append(item)

    def is_anchor(net: str, layer: str, point: tuple[int, int]) -> bool:
        layer_id = board.GetLayerID(layer)
        if is_pad(net, layer, point):
            return True
        for item in vias[net]:
            if item.IsOnLayer(layer_id):
                at = item.GetPosition()
                radius = copper_width(item, layer=layer_id) / 2
                if (point[0] - at.x) ** 2 + (point[1] - at.y) ** 2 <= radius**2:
                    return True
        return any(
            (point[0] - item.at[0]) ** 2 + (point[1] - item.at[1]) ** 2
            <= (item.size / 2) ** 2
            for item in added[net]
        )

    return is_anchor


def drc_rejections(
    report: dict,
    proposed_ids: set[str],
    *,
    baseline_report: dict | None = None,
    stable_items: dict[str, str] | None = None,
    baseline_ids: dict[str, str] | None = None,
    candidate_ids: dict[str, str] | None = None,
) -> tuple[set[str], tuple[str, ...]]:
    """Reject proposed copper and block new errors or unattributed narrow necks.

    Existing errors remain errors: this comparison only permits an interim
    candidate to be evaluated. Final completion still requires a clean DRC.
    """
    violations = report.get("violations")
    if not isinstance(violations, list):
        raise ValueError("A complete KiCad DRC report is required")
    existing_errors = Counter()
    baseline_violations = []
    if baseline_report is not None:
        baseline_violations = baseline_report.get("violations")
        if not isinstance(baseline_violations, list):
            raise ValueError("A complete baseline KiCad DRC report is required")
        for finding in baseline_violations:
            if not isinstance(finding, dict) or finding.get("severity") not in {
                "error",
                "warning",
                "exclusion",
            }:
                raise ValueError("KiCad returned an invalid baseline DRC finding")
            if not (
                finding["severity"] == "error"
                or (
                    finding["severity"] == "warning"
                    and finding.get("type") == "connection_width"
                )
            ):
                continue
            items = finding.get("items")
            if (
                not isinstance(finding.get("type"), str)
                or not finding["type"]
                or not isinstance(finding.get("description"), str)
                or not finding["description"]
                or not isinstance(items, list)
                or not items
                or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("uuid"), str)
                    or not item["uuid"]
                    for item in items
                )
            ):
                raise ValueError("Cannot compare an unidentified baseline DRC error")
            existing_errors[json.dumps(finding, sort_keys=True, allow_nan=False)] += 1
    from pcb.route_validation import _key, added_violation_keys

    def neck_key(finding, identities):
        return _key(finding, identities)

    added_necks = added_violation_keys(
        Counter(
            neck_key(f, baseline_ids)
            for f in baseline_violations
            if f.get("type") == "connection_width"
        ),
        Counter(
            neck_key(f, candidate_ids)
            for f in violations
            if f.get("type") == "connection_width"
        ),
        stable_items,
    )
    rejected = set()
    blockers = []
    for finding in violations:
        if not isinstance(finding, dict) or finding.get("severity") not in {
            "error",
            "warning",
            "exclusion",
        }:
            raise ValueError("KiCad returned an invalid DRC finding")
        mentioned = {
            item.get("uuid")
            for item in finding.get("items", ())
            if isinstance(item, dict) and item.get("uuid") in proposed_ids
        }
        rejected.update(mentioned)
        if (
            finding["severity"] == "error"
            or (
                finding["severity"] == "warning"
                and finding.get("type") == "connection_width"
            )
        ) and not mentioned:
            identity = json.dumps(finding, sort_keys=True, allow_nan=False)
            if existing_errors[identity]:
                existing_errors[identity] -= 1
            else:
                key = (
                    neck_key(finding, candidate_ids)
                    if finding.get("type") == "connection_width"
                    else None
                )
                stable_nets = (
                    {stable_items.get(item) for item in key[2]}
                    if key and stable_items
                    else set()
                )
                if (
                    key
                    and not added_necks[key]
                    and len(key[2]) == 2
                    and len(stable_nets) == 1
                    and None not in stable_nets
                ):
                    continue  # Same complete copper-and-pad net; only the marker moved.
                if key and added_necks[key]:
                    added_necks[key] -= 1
                detail = str(
                    finding.get(
                        "description", "Native DRC finding without a proposed item"
                    )
                )
                if finding.get("type") == "connection_width":
                    objects = [
                        str(item["description"])
                        for item in finding.get("items", ())
                        if isinstance(item, dict) and item.get("description")
                    ]
                    if objects:
                        detail += "; objects: " + ", ".join(objects)
                blockers.append(detail)
    return rejected, tuple(blockers)


def dangling_only_rejections(
    report: dict, proposed_ids: set[str], style_ids: set[str]
) -> set[str]:
    """Do not spend path-search time repairing copper with only a dangling warning."""
    dangling, other = set(), set(style_ids)
    for finding in report.get("violations", ()):
        target = (
            dangling
            if finding.get("type") in {"track_dangling", "via_dangling"}
            else other
        )
        target.update(
            item.get("uuid")
            for item in finding.get("items", ())
            if isinstance(item, dict) and item.get("uuid") in proposed_ids
        )
    return dangling - other


def style_rejections(
    before,
    after,
    before_identities: dict[str, str],
    after_identities: dict[str, str],
    proposed_ids: set[str],
    *,
    reasons=None,
) -> tuple[set[str], tuple[str, ...]]:
    """Identify proposed items responsible for newly introduced style defects."""
    from pcb.net_routing.style import normalize_segment_identities

    old = normalize_segment_identities(before, before_identities)
    current = normalize_segment_identities(after, after_identities)
    rejected = set()
    blockers = []

    def mark(ids, name):
        affected = set(ids) & proposed_ids
        if affected:
            rejected.update(affected)
            if reasons is not None:
                reasons[name] += 1
        else:
            blockers.append(f"New {name} has no identifiable proposed item")

    for field in ("non_octilinear", "arcs", "locked_items", "grouped_items"):
        previous = set(getattr(old, field))
        for uid in getattr(after, field):
            identity = (
                after_identities.get(uid, uid) if field == "non_octilinear" else uid
            )
            if identity not in previous:
                mark((uid,), field)
    for field in ("invalid_bends", "redundant_joints", "staircases"):
        previous = set(getattr(old, field))
        for raw, normalized in strict_zip(
            getattr(after, field), getattr(current, field)
        ):
            if normalized not in previous:
                mark(raw.item_ids, field)
    blockers.extend(
        f"New ambiguous geometry: {item}"
        for item in set(current.ambiguous) - set(old.ambiguous)
    )
    return rejected, tuple(sorted(blockers))


def attributable_style_blocker_nets(
    blockers: tuple[str, ...], proposed_nets: set[str]
) -> tuple[str, ...] | None:
    """Attribute only exact native ambiguity messages to one proposed net each."""
    if not blockers:
        return None
    affected = set()
    for blocker in blockers:
        matches = {
            name
            for name in proposed_nets
            for layer in COPPER_LAYERS
            if blocker.startswith(f"New ambiguous geometry: {name} on {layer}: ")
        }
        if len(matches) != 1:
            return None
        affected.update(matches)
    return tuple(sorted(affected))


def read_copper(board_path: Path) -> list[Trace | Via]:
    """Read exact nanometre geometry; refuse unsupported via padstacks."""
    import pcbnew

    board = pcbnew.LoadBoard(str(board_path))
    if board is None:
        raise ValueError(f"KiCad could not read {board_path}")
    layer_ids = {name: board.GetLayerID(name) for name in COPPER_LAYERS}
    result = []
    for item in board.GetTracks():
        net = item.GetNetname()
        if not net:
            continue
        if isinstance(item, pcbnew.PCB_VIA):
            layers = tuple(
                name for name, number in layer_ids.items() if item.IsOnLayer(number)
            )
            widths = {int(copper_width(item, layer=layer_ids[name])) for name in layers}
            if (
                item.GetViaType() != pcbnew.VIATYPE_THROUGH
                or layers != COPPER_LAYERS
                or len(widths) != 1
            ):
                raise ValueError(
                    "External comparison contains an unsupported via padstack"
                )
            point = item.GetPosition()
            result.append(
                Via(
                    net,
                    (int(point.x), int(point.y)),
                    widths.pop(),
                    int(item.GetDrillValue()),
                )
            )
        elif isinstance(item, pcbnew.PCB_ARC):
            raise ValueError("External comparison contains arc copper")
        else:
            a, b = item.GetStart(), item.GetEnd()
            result.append(
                Trace(
                    net,
                    board.GetLayerName(item.GetLayer()),
                    int(copper_width(item)),
                    (int(a.x), int(a.y)),
                    (int(b.x), int(b.y)),
                )
            )
    return result


def _millimetres(value: int) -> str:
    return format(Decimal(value) / Decimal(1_000_000), "f")


def changes(delta: Delta) -> list[dict]:
    """Create ordinary, unlocked PCB copper changes for campaign planning."""
    result = []
    for item in delta.traces:
        x1, y1 = map(_millimetres, item.start)
        x2, y2 = map(_millimetres, item.end)
        expression = (
            f"(segment (start {x1} {y1}) (end {x2} {y2}) "
            f"(width {_millimetres(item.width)}) (layer {quote(item.layer)}) "
            f'(net {quote(item.net)}) (uuid "{uuid4()}"))'
        )
        result.append(
            {
                "document": "pcb",
                "target": {"kind": "root"},
                "op": "insert",
                "path": [],
                "expression": expression,
            }
        )
    for item in delta.vias:
        x, y = map(_millimetres, item.at)
        expression = (
            f"(via (at {x} {y}) (size {_millimetres(item.size)}) "
            f'(drill {_millimetres(item.drill)}) (layers "F.Cu" "B.Cu") '
            f'(net {quote(item.net)}) (uuid "{uuid4()}"))'
        )
        result.append(
            {
                "document": "pcb",
                "target": {"kind": "root"},
                "op": "insert",
                "path": [],
                "expression": expression,
            }
        )
    return result


def check_intermediate_connectivity(
    directory: Path, retained: bytes, *, allow_temporary_disconnect: bool = False
) -> bool:
    """Flag a temporary disconnection without certifying or publishing the candidate."""
    from pcb.net_routing.runner import _verify_resume_connectivity

    if not isinstance(allow_temporary_disconnect, bool):
        raise ValueError("Temporary disconnect permission must be boolean")
    try:
        _verify_resume_connectivity(directory, retained)
    except RuntimeError as error:
        if (
            not allow_temporary_disconnect
            or str(error).partition(":")[0]
            != "Resumed candidate broke an existing copper connection"
        ):
            raise
        return False
    return True


def filter_proposal(
    project: Path,
    roundtrip: Path,
    routed: Path,
    *,
    max_passes: int = 32,
    deferred_classes: tuple[str, ...] = (),
    deferred_nets: tuple[str, ...] = (),
    max_seconds: float | None = None,
    progress=None,
    allow_temporary_disconnect: bool = False,
) -> dict:
    """Build and check an additive proposal on a disposable project copy."""
    import pcbnew

    from core.kicad_campaign import CampaignPlanner
    from core.kicad_sexpr import parse
    from pcb.freeroute_preview import _fingerprints, _snapshot, _source_files
    from pcb.net_routing.controller import new_style_issues
    from pcb.net_routing.prepare import parity_blocked_nets, summarize_drc
    from pcb.net_routing.protection import ProtectedState
    from pcb.net_routing.reference import high_speed_nets, reference_corridors
    from pcb.net_routing.rules import RoutingRules
    from pcb.net_routing.style import audit_style, normalize_segment_identities
    from pcb.route_native import NativeObstacleCache
    from pcb.route_optimizer import NoGridRoute, SearchLimitReached
    from pcb.route_validation import (
        _dangling_track_identities,
        _drc,
        _segment_identities,
        stable_connection_items,
    )

    if not isinstance(allow_temporary_disconnect, bool):
        raise ValueError("Temporary disconnect permission must be boolean")
    if (
        isinstance(max_passes, bool)
        or not isinstance(max_passes, int)
        or max_passes < 1
    ):
        raise ValueError("The filter needs a positive pass limit")
    if max_seconds is not None and (
        isinstance(max_seconds, bool)
        or not isinstance(max_seconds, (int, float))
        or not math.isfinite(max_seconds)
        or max_seconds <= 0
    ):
        raise ValueError("The filter time limit must be positive and finite")
    started = time.monotonic()

    def check_cleanup_budget() -> None:
        if not repair_budget_available(time.monotonic() - started, max_seconds):
            raise SearchBudgetExceeded("Cleanup time budget reached")

    project = project.resolve()
    source = native_path(project, ".kicad_pcb")
    cli = kicad_cli()
    if not cli.is_file():
        raise RuntimeError("KiCad CLI is required for proposal filtering")
    with tempfile.TemporaryDirectory(prefix="autopcb-external-filter-") as directory:
        stage = Path(directory)
        fingerprints = _snapshot(source, stage)
        boardfile = stage / source.name
        schematic = (native_path(stage, ".kicad_sch")).read_bytes()
        source_bytes = boardfile.read_bytes()
        protected = ProtectedState.capture(source_bytes, schematic)
        baseline_report = _drc(
            cli,
            boardfile,
            stage / "baseline-drc.json",
            save_refill=True,
            schematic_parity=True,
        )
        retained = boardfile.read_bytes()
        if protected.differences(retained, schematic):
            raise RuntimeError("KiCad refill changed protected design items")
        baseline_board = pcbnew.LoadBoard(str(boardfile))
        baseline_open_counts = unconnected_by_net(
            baseline_report, native_item_nets(baseline_board)
        )
        baseline_style = audit_style(baseline_board)
        baseline_ids = _segment_identities(retained)
        baseline = summarize_drc(
            baseline_report,
            segment_identities=baseline_ids,
            dangling_identities=_dangling_track_identities(
                baseline_board, baseline_report
            ),
        )
        if baseline["exclusions"] or baseline["ignored_checks"]:
            raise RuntimeError("The source preview has excluded or ignored checks")
        pads = {
            pad.m_Uuid.AsString(): str(pad.GetNetname())
            for pad in baseline_board.GetPads()
        }
        parity_nets = set(parity_blocked_nets(baseline_report, pads))
        original = read_copper(boardfile)
        comparison, external = read_copper(roundtrip), read_copper(routed)
        net_names = {item.net for item in original + comparison + external}
        critical = set(
            high_speed_nets((native_path(stage, ".kicad_pro")).read_bytes(), net_names)
        )
        netclasses = {}
        for name in net_names:
            net = baseline_board.FindNet(name)
            if net is None:
                raise RuntimeError(f"External comparison contains unknown net {name}")
            netclasses[name] = router_class(str(net.GetNetClassName()))
        excluded_nets = (
            set(deferred_nets)
            | parity_nets
            | {
                name
                for name, netclass in netclasses.items()
                if netclass in deferred_classes
            }
        )
        delta = additive_delta(
            original, comparison, external, excluded=critical | excluded_nets
        )
        delta, alignment = align_proposal_to_existing_vias(delta, original)
        if alignment and progress:
            progress({"event": "via_alignment", "nets": sorted(alignment)})
        anchor = native_anchor_checker(baseline_board, delta.vias)
        delta, snapped = snap_proposal_via_escapes(delta, original, anchor)
        if snapped and progress:
            progress({"event": "via_alignment", "nets": sorted(snapped)})
        rules = RoutingRules.from_board(
            baseline_board, native_path(stage, ".kicad_dru")
        )
        reference = reference_corridors(baseline_board, critical)
        if reference.unsupported_items:
            raise RuntimeError(
                "Cannot screen unsupported high-speed reference geometry"
            )
        class_counts = Counter(
            netclasses[item.net] for item in (*delta.traces, *delta.vias)
        )
        if progress:
            progress(
                {
                    "event": "proposal",
                    "classes": dict(class_counts),
                    "deferred": list(deferred_classes),
                }
            )
        rejected_classes = Counter()
        profiles = {}
        width_blockers = {}

        def profile_for(item):
            key = item.net, item.width
            if key not in profiles:
                profiles[key] = rules.profile(item.net, item.width, 0)
            return profiles[key]

        def legal_width(item):
            try:
                required = profile_for(item).width_at(_segment(item))
                if item.width >= required:
                    return True
                width_blockers[item.net] = (
                    "Proposed track is narrower than the compiled rule"
                )
            except ValueError as error:
                width_blockers[item.net] = str(error)
            return False

        clear_shortcut = shortcut_clearance_checker(
            (*native_shortcut_obstacles(baseline_board), *reference.obstacles),
            profile_for=profile_for,
        )
        pad_contact = native_pad_checker(baseline_board)
        via_move_allowed = native_via_move_checker(baseline_board)

        def anchor_factory(vias):
            return native_anchor_checker(baseline_board, vias)

        def shortcut_allowed(traces):
            try:
                if any(
                    item.width < profile_for(item).width_at(_segment(item))
                    for item in traces
                ):
                    return False
            except ValueError:
                return False
            return clear_shortcut(traces)

        def saved_width_materializer(traces):
            candidate, _, blocked = materialize_saved_widths(
                Delta(tuple(traces), (), (), 0), profile_for
            )
            if blocked:
                raise ValueError("A shortcut has unsupported saved width requirements")
            return candidate.traces

        repaired_external, repaired_count = recover_off_angle_additions(
            comparison,
            external,
            critical | excluded_nets,
            shortcut_allowed,
            may_continue=lambda: repair_budget_available(
                time.monotonic() - started, max_seconds
            ),
        )
        if repaired_count:
            # Rebuild from the external comparison, so ordinary subtraction,
            # via alignment and the complete native checks still apply.
            delta = additive_delta(
                original,
                comparison,
                repaired_external,
                excluded=critical | excluded_nets,
            )
            delta, _ = align_proposal_to_existing_vias(delta, original)
            anchor = native_anchor_checker(baseline_board, delta.vias)
            delta, _ = snap_proposal_via_escapes(delta, original, anchor)
            class_counts = Counter(
                netclasses[item.net] for item in (*delta.traces, *delta.vias)
            )
            if progress:
                progress({"event": "off_angle_repair", "segments": repaired_count})

        delta, width_changes, width_failures = materialize_saved_widths(
            delta, profile_for
        )
        width_blockers.update(width_failures)
        if width_changes and progress:
            progress(
                {"event": "rule_width_materialization", "changed_traces": width_changes}
            )
        delta, moved = snap_new_vias_to_existing_endpoints(
            delta,
            original,
            anchor_factory,
            via_move_allowed,
            shortcut_allowed,
            pad_contact,
        )
        if moved and progress:
            progress({"event": "via_alignment", "nets": sorted(moved)})
        anchor = anchor_factory(delta.vias)
        delta, snapped = snap_proposal_via_escapes(
            delta,
            original,
            anchor,
            capture_via_copper=True,
            shortcut_allowed=shortcut_allowed,
            protected_endpoint=pad_contact,
        )
        if snapped and progress:
            progress({"event": "via_alignment", "nets": sorted(snapped)})
        before_pad_cleanup = len(delta.traces)
        delta = without_in_pad_segments(delta, baseline_board)
        if len(delta.traces) < before_pad_cleanup and progress:
            progress(
                {
                    "event": "local_simplification",
                    "removed_segments": before_pad_cleanup - len(delta.traces),
                }
            )
        kept = [item for item in delta.traces if legal_width(item)]
        chamfered = tuple(
            item
            for item in chamfer_right_angles(tuple(kept), original, anchor)
            if legal_width(item)
        )
        simplified = simplify_terminal_chains(
            chamfered,
            original,
            anchor,
            shortcut_allowed=shortcut_allowed,
            materialize=saved_width_materializer,
            may_continue=lambda: repair_budget_available(
                time.monotonic() - started, max_seconds
            ),
        )
        locally_simplified = simplify_local_windows(
            simplified,
            original,
            anchor,
            shortcut_allowed=shortcut_allowed,
            materialize=saved_width_materializer,
            may_continue=lambda: repair_budget_available(
                time.monotonic() - started, max_seconds
            ),
        )
        if locally_simplified != simplified and progress:
            progress(
                {
                    "event": "local_simplification",
                    "removed_segments": len(simplified) - len(locally_simplified),
                }
            )
        simplified = merge_proposed_collinear(locally_simplified, original, anchor)
        delta = Delta(
            tuple(item for item in simplified if legal_width(item)),
            delta.vias,
            delta.excluded_nets,
            delta.unsupported,
        )
        raw_by_net = defaultdict(set)
        simplified_by_net = defaultdict(set)
        for item in chamfered:
            raw_by_net[item.net].add(item)
        for item in delta.traces:
            simplified_by_net[item.net].add(item)
        fallback_nets = {
            net
            for net in set(raw_by_net) | set(simplified_by_net)
            if raw_by_net[net] != simplified_by_net[net]
        }
        initial_fallback_nets = frozenset(fallback_nets)
        repair_ordering = RepairOrdering(delta)
        rejected_total = 0
        attempted_repairs = RepairAttemptMemory()
        native_cache = NativeObstacleCache()
        repair_blockers = {}
        attempted_net_prunes = set()
        attempted_components = set()
        attempted_component_groups = set()
        failed_prune_outcomes = FailedPruneOutcomes()
        pending_net_prune = None
        last_rejected_nets: set[str] = set()
        salvaging = False
        salvage_attempts = 0
        native_retry_count = 0
        style_blocker_retries = 0
        connectivity_retries: ConnectivityRetries | None = None
        connectivity_omitted: tuple[str, ...] = ()
        for index in range(max_passes):
            delta, moved = snap_new_vias_to_existing_endpoints(
                delta,
                original,
                anchor_factory,
                via_move_allowed,
                shortcut_allowed,
                pad_contact,
            )
            if moved and progress:
                progress({"event": "via_alignment", "nets": sorted(moved)})
            anchor = anchor_factory(delta.vias)
            delta, snapped = snap_proposal_via_escapes(
                delta,
                original,
                anchor,
                capture_via_copper=True,
                shortcut_allowed=shortcut_allowed,
                protected_endpoint=pad_contact,
            )
            if snapped and progress:
                progress({"event": "via_alignment", "nets": sorted(snapped)})
            merged = merge_proposed_collinear(delta.traces, original, anchor)
            delta = Delta(merged, delta.vias, delta.excluded_nets, delta.unsupported)
            delta, reference_blocked = screen_reference_regions(
                delta, reference.obstacles, profile_for=profile_for
            )
            if reference_blocked:
                remember_blocked_nets(
                    repair_blockers,
                    set(reference_blocked),
                    "Local high-speed reference reservation",
                )
                if progress:
                    progress(
                        {"event": "reference_blocked", "nets": list(reference_blocked)}
                    )
            if not salvaging and cleanup_timed_out(
                index, time.monotonic() - started, max_seconds
            ):
                remember_blocked_nets(
                    repair_blockers,
                    last_rejected_nets,
                    "Cleanup reached its time budget",
                )
                if progress:
                    progress({"event": "timeout", "nets": sorted(last_rejected_nets)})
                remainder = timeout_remainder(
                    delta, last_rejected_nets, pending=pending_net_prune is not None
                )
                if remainder is None:
                    return {
                        "status": "filter_stalled",
                        "baseline_opens": baseline["open_connections"],
                        "dominant_class": deferred_class(
                            class_counts, rejected_classes
                        ),
                        "width_blockers": width_blockers,
                        "repair_blockers": repair_blockers,
                    }
                delta = remainder
                salvaging = True
            boardfile.write_bytes(retained)
            modifications = changes(delta)
            if not modifications:
                remember_blocked_nets(
                    repair_blockers,
                    last_rejected_nets,
                    "No safe proposed copper remained",
                )
                return {
                    "status": "no_safe_proposal",
                    "baseline_opens": baseline["open_connections"],
                    "dominant_class": deferred_class(class_counts, rejected_classes),
                    "width_blockers": width_blockers,
                    "repair_blockers": repair_blockers,
                }
            records = [*delta.traces, *delta.vias]
            proposed = {
                parse(change["expression"]).children("uuid")[0].atoms()[1].value: item
                for item, change in strict_zip(records, modifications)
            }
            boardfile.write_bytes(
                CampaignPlanner(stage).plan("pcb", modifications).after
            )
            report = _drc(
                cli,
                boardfile,
                stage / f"pass-{index}.json",
                save_refill=True,
                schematic_parity=True,
            )
            candidate_bytes = boardfile.read_bytes()
            if protected.differences(candidate_bytes, schematic):
                raise RuntimeError("Filtering changed protected design items")
            candidate_board = pcbnew.LoadBoard(str(boardfile))
            candidate_open_counts = unconnected_by_net(
                report, native_item_nets(candidate_board)
            )
            candidate_ids = _segment_identities(candidate_bytes)
            candidate_style = audit_style(candidate_board)
            if pending_net_prune is not None:
                before_delta, before_opens, pruned_nets, prune_kind = pending_net_prune
                pending_net_prune = None
                if len(report["unconnected_items"]) > before_opens:
                    failed_prune_outcomes.add(delta)
                    delta = before_delta
                    if progress:
                        progress(
                            {
                                "event": "prune_net",
                                "nets": sorted(pruned_nets),
                                "status": "rolled_back",
                                "kind": prune_kind,
                            }
                        )
                    continue
                unresolved = unproductive_pruned_nets(
                    baseline_open_counts, candidate_open_counts, pruned_nets
                )
                remember_blocked_nets(
                    repair_blockers,
                    unresolved,
                    "Pruned incomplete copper without improving this net",
                )
            stable = stable_connection_items(retained, candidate_bytes)
            bad_drc, drc_blockers = drc_rejections(
                report,
                set(proposed),
                baseline_report=baseline_report,
                stable_items=stable,
                baseline_ids=baseline_ids,
                candidate_ids=candidate_ids,
            )
            style_reasons = Counter()
            bad_style, style_blockers = style_rejections(
                baseline_style,
                candidate_style,
                baseline_ids,
                candidate_ids,
                set(proposed),
                reasons=style_reasons,
            )
            rejected = bad_drc | bad_style
            if rejected:
                last_rejected_nets = {
                    proposed[identifier].net for identifier in rejected
                }
            rejected_total += len(rejected)
            rejected_classes.update(
                netclasses[proposed[identifier].net] for identifier in rejected
            )
            if progress:
                affected = Counter(proposed[identifier].net for identifier in rejected)
                reasons = Counter(
                    item.get("type", "unknown")
                    for item in report["violations"]
                    if any(
                        part.get("uuid") in bad_drc
                        for part in item.get("items", [])
                        if isinstance(part, dict)
                    )
                )
                candidate_nets = {item.net for item in (*delta.traces, *delta.vias)}
                progress(
                    {
                        "event": "pass",
                        "pass": index + 1,
                        "opens": len(report["unconnected_items"]),
                        "rejected_items": len(rejected),
                        "affected_nets": dict(affected),
                        "drc_reasons": dict(reasons),
                        "style_items": len(bad_style),
                        "style_reasons": dict(style_reasons),
                        "remaining_by_net": {
                            name: candidate_open_counts.get(name, 0)
                            for name in sorted(candidate_nets)
                        },
                        "unidentified_opens": candidate_open_counts.get("<unknown>", 0),
                    }
                )
            if drc_blockers or style_blockers:
                names = (
                    attributable_style_blocker_nets(
                        style_blockers,
                        {item.net for item in (*delta.traces, *delta.vias)},
                    )
                    if not drc_blockers
                    else None
                )
                remainder = (
                    cleanup_remainder(delta, set(names))
                    if names is not None and style_blocker_retries < 3
                    else None
                )
                if remainder is not None:
                    delta = remainder
                    style_blocker_retries += 1
                    salvaging = True
                    pending_net_prune = None
                    last_rejected_nets = set()
                    remember_blocked_nets(
                        repair_blockers,
                        set(names),
                        "Deferred after ambiguous proposed geometry",
                    )
                    if progress:
                        progress({"event": "style_blocker_retry", "nets": list(names)})
                    continue
                if names is not None:
                    remember_blocked_nets(
                        repair_blockers,
                        set(names),
                        "No remaining subset avoids ambiguous geometry",
                    )
                    return {
                        "status": "filter_stalled",
                        "reason": "; ".join(style_blockers),
                        "baseline_opens": baseline["open_connections"],
                        "dominant_class": deferred_class(
                            class_counts, rejected_classes
                        ),
                        "width_blockers": width_blockers,
                        "repair_blockers": repair_blockers,
                    }
                if drc_blockers:
                    # A zone or existing pad can be the only object named by a
                    # native violation.  Reject the whole proposal when it cannot
                    # be safely pruned; do not turn a known unsafe candidate into
                    # an execution failure or return its copper for publication.
                    return {
                        "status": "candidate_rejected",
                        "reason": "; ".join((*drc_blockers, *style_blockers)),
                        "native_blockers": drc_blockers,
                        "style_blockers": style_blockers,
                        "baseline_opens": baseline["open_connections"],
                        "dominant_class": deferred_class(
                            class_counts, rejected_classes
                        ),
                        "width_blockers": width_blockers,
                        "repair_blockers": repair_blockers,
                    }
                raise RuntimeError("; ".join((*drc_blockers, *style_blockers)))
            if rejected and salvaging:
                remember_blocked_nets(
                    repair_blockers,
                    last_rejected_nets,
                    "Clean subset still needs repair",
                )
                remainder = salvage_remainder(
                    delta, last_rejected_nets, attempts=salvage_attempts
                )
                if remainder is None:
                    return {
                        "status": "filter_stalled",
                        "baseline_opens": baseline["open_connections"],
                        "dominant_class": deferred_class(
                            class_counts, rejected_classes
                        ),
                        "width_blockers": width_blockers,
                        "repair_blockers": repair_blockers,
                    }
                if progress:
                    progress({"event": "salvage", "nets": sorted(last_rejected_nets)})
                delta = remainder
                salvage_attempts += 1
                continue
            if rejected:
                fallback = {
                    proposed[identifier].net for identifier in rejected
                } & fallback_nets
                if fallback:
                    delta = restore_raw_nets(delta, chamfered, fallback)
                    fallback_nets.difference_update(fallback)
                    if progress:
                        progress({"event": "fallback", "nets": sorted(fallback)})
                    continue
                bad = {proposed[identifier] for identifier in rejected}
                dangling = dangling_only_rejections(report, set(proposed), bad_style)
                if rejected == dangling:
                    tracks = {
                        item.m_Uuid.AsString(): item
                        for item in candidate_board.GetTracks()
                    }
                    connectivity = candidate_board.GetConnectivity()
                    connectivity.Build(candidate_board)
                    branches = dangling_proposed_branches(
                        candidate_board,
                        proposed,
                        dangling,
                        tracks=tracks,
                        connectivity=connectivity,
                    )
                    if branches:
                        trial = without_items(delta, branches)
                        if (
                            trial.traces or trial.vias
                        ) and not failed_prune_outcomes.contains(trial):
                            nets = {item.net for item in branches}
                            pending_net_prune = (
                                delta,
                                len(report["unconnected_items"]),
                                nets,
                                "branches",
                            )
                            delta = trial
                            if progress:
                                progress(
                                    {
                                        "event": "prune_net",
                                        "nets": sorted(nets),
                                        "status": "trial",
                                        "kind": "branches",
                                    }
                                )
                            continue
                    components = []
                    for identifier in sorted(dangling):
                        component = proposed_component(
                            candidate_board,
                            proposed,
                            identifier,
                            tracks=tracks,
                            connectivity=connectivity,
                        )
                        key = frozenset(component)
                        if len(component) >= 2 and key not in components:
                            components.append(key)
                    group = frozenset().union(*components)
                    if len(components) > 1 and group not in attempted_component_groups:
                        attempted_component_groups.add(group)
                        trial = without_items(delta, set(group))
                        if (
                            trial.traces or trial.vias
                        ) and not failed_prune_outcomes.contains(trial):
                            nets = {item.net for item in group}
                            pending_net_prune = (
                                delta,
                                len(report["unconnected_items"]),
                                nets,
                                "components",
                            )
                            delta = trial
                            if progress:
                                progress(
                                    {
                                        "event": "prune_net",
                                        "nets": sorted(nets),
                                        "status": "trial",
                                        "kind": "components",
                                    }
                                )
                    if pending_net_prune is not None:
                        continue
                    for key in components:
                        if key in attempted_components:
                            continue
                        attempted_components.add(key)
                        trial = without_items(delta, set(key))
                        if (
                            trial.traces or trial.vias
                        ) and not failed_prune_outcomes.contains(trial):
                            nets = {item.net for item in key}
                            pending_net_prune = (
                                delta,
                                len(report["unconnected_items"]),
                                nets,
                                "component",
                            )
                            delta = trial
                            if progress:
                                progress(
                                    {
                                        "event": "prune_net",
                                        "nets": sorted(nets),
                                        "status": "trial",
                                        "kind": "component",
                                    }
                                )
                            break
                    if pending_net_prune is not None:
                        continue
                    counts = Counter(
                        proposed[identifier].net for identifier in dangling
                    )
                    for net in sorted(counts, key=lambda name: (-counts[name], name)):
                        if net in attempted_net_prunes:
                            continue
                        attempted_net_prunes.add(net)
                        nets = {net}
                        trial = without_nets(delta, nets)
                        if (
                            trial.traces or trial.vias
                        ) and not failed_prune_outcomes.contains(trial):
                            pending_net_prune = (
                                delta,
                                len(report["unconnected_items"]),
                                nets,
                                "net",
                            )
                            delta = trial
                            if progress:
                                progress(
                                    {
                                        "event": "prune_net",
                                        "nets": sorted(nets),
                                        "status": "trial",
                                        "kind": "net",
                                    }
                                )
                            break
                    if pending_net_prune is not None:
                        continue
                expanded = set()
                for identifier in dangling:
                    item = proposed[identifier]
                    if isinstance(item, Trace):
                        expanded.update(
                            dangling_trace_remainder(
                                item, delta.traces, original, anchor
                            )
                        )
                if expanded - bad and progress:
                    progress({"event": "prune", "items": len(expanded - bad)})
                bad.update(expanded)
                repairable = {
                    proposed[identifier] for identifier in rejected - dangling
                }
                repaired_any = False
                repair_queue = deque(
                    (item, False) for item in repair_ordering.seeds(repairable)
                )
                while repair_queue:
                    seed, wider = repair_queue.popleft()
                    if (
                        max_seconds is not None
                        and time.monotonic() - started >= max_seconds
                    ):
                        break
                    if seed not in delta.traces:
                        continue
                    chain = (
                        expand_repairable_subchain(
                            seed, delta.traces, repairable, original, anchor
                        )
                        if wider
                        else repairable_subchain(
                            seed, delta.traces, repairable, original, anchor
                        )
                    )
                    if not wider:
                        expanded = expand_repairable_subchain(
                            seed, delta.traces, repairable, original, anchor
                        )
                        prioritize_expanded_repair(repair_queue, seed, chain, expanded)
                    ends = chain_endpoints(chain)
                    if ends is None or not attempted_repairs.claim(
                        delta, seed.net, seed.layer, ends
                    ):
                        continue
                    boardfile.write_bytes(retained)
                    repair_contacts = [
                        *original,
                        *(item for item in delta.traces if item not in chain),
                    ]
                    excluded = set()
                    replacement_delta = None
                    for _ in range(4):
                        if not repair_budget_available(
                            time.monotonic() - started, max_seconds
                        ):
                            repair_blockers[seed.net] = "Cleanup time budget reached"
                            break
                        boardfile.write_bytes(retained)
                        try:
                            replacement, new_vias = retry_local_grids(
                                lambda step, expansions: prefer_no_via_repair(
                                    lambda same: local_chain_repair(
                                        stage,
                                        chain,
                                        repair_contacts,
                                        baseline_board,
                                        anchor,
                                        max_expansions=expansions,
                                        excluded_initial=frozenset(excluded),
                                        heuristic_weight=2.0
                                        if expansions > 15_000
                                        else 1.0,
                                        step_mm=step,
                                        same_layer_only=same,
                                        check_budget=check_cleanup_budget
                                        if max_seconds is not None
                                        else None,
                                        native_cache=native_cache,
                                        proposed_vias=delta.vias,
                                        proposed_traces=tuple(
                                            item
                                            for item in delta.traces
                                            if item not in chain
                                        ),
                                    ),
                                    may_retry=lambda: (
                                        max_seconds is None
                                        or time.monotonic() - started < max_seconds
                                    ),
                                ),
                                may_retry=lambda: (
                                    max_seconds is None
                                    or time.monotonic() - started < max_seconds
                                ),
                                on_retry=(
                                    lambda step, limit: progress(
                                        {
                                            "event": "repair_retry",
                                            "net": seed.net,
                                            "step_mm": step,
                                            "max_expansions": limit,
                                        }
                                    )
                                )
                                if progress
                                else None,
                            )
                            if not all(legal_width(item) for item in replacement):
                                raise ValueError(
                                    "A repaired trace does not meet the compiled width rule"
                                )
                            if set(replacement) == set(chain) and not new_vias:
                                raise ValueError(
                                    "Local repair repeated the rejected geometry"
                                )
                            heading = initial_heading(replacement, ends[0], seed.layer)
                        except (
                            ValueError,
                            NoGridRoute,
                            SearchLimitReached,
                            SearchBudgetExceeded,
                        ) as error:
                            repair_blockers[seed.net] = str(error)
                            break
                        trial = replace_repaired_chain(
                            delta, chain, replacement, new_vias
                        )
                        trial, checked_replacement = normalize_repaired_trial(
                            trial,
                            delta,
                            replacement,
                            original,
                            native_anchor_checker(baseline_board, trial.vias),
                        )
                        trial_changes = changes(trial)
                        trial_records = {
                            parse(change["expression"])
                            .children("uuid")[0]
                            .atoms()[1]
                            .value: item
                            for item, change in strict_zip(
                                (*trial.traces, *trial.vias), trial_changes
                            )
                        }
                        trial_ids = set(trial_records)
                        boardfile.write_bytes(
                            CampaignPlanner(stage).plan("pcb", trial_changes).after
                        )
                        trial_bytes = boardfile.read_bytes()
                        trial_board = pcbnew.LoadBoard(str(boardfile))
                        if trial_board is None:
                            raise RuntimeError("KiCad could not read a local repair")
                        quick_bad, quick_blockers = style_rejections(
                            baseline_style,
                            audit_style(trial_board),
                            baseline_ids,
                            _segment_identities(trial_bytes),
                            trial_ids,
                        )
                        if replacement_style_clear(
                            quick_bad,
                            trial_records,
                            checked_replacement,
                            new_vias,
                            blockers=quick_blockers,
                        ):
                            replacement_delta = trial
                            break
                        excluded.add(heading)
                        repair_blockers[seed.net] = (
                            "A candidate produced ambiguous track geometry"
                            if quick_blockers
                            else "A candidate conflicted with neighboring track geometry"
                        )
                    if replacement_delta is None:
                        if progress:
                            progress(
                                {
                                    "event": "repair",
                                    "net": seed.net,
                                    "status": "not_found",
                                }
                            )
                        continue
                    repair_blockers.pop(seed.net, None)
                    if progress:
                        progress(
                            {"event": "repair", "net": seed.net, "status": "candidate"}
                        )
                    delta = replacement_delta
                    anchor = native_anchor_checker(baseline_board, delta.vias)
                    repaired_any = True
                    repair_ordering.record_repair()
                if repaired_any:
                    remaining_bad = bad & set((*delta.traces, *delta.vias))
                    last_rejected_nets = {item.net for item in remaining_bad}
                    if (
                        not remaining_bad
                        and max_seconds is not None
                        and time.monotonic() - started >= max_seconds
                    ):
                        # Give the combined repaired batch one final native check.
                        salvaging = True
                else:
                    restart = repair_ordering.restart(
                        may_continue=max_seconds is None
                        or time.monotonic() - started < max_seconds
                    )
                    if restart is not None:
                        delta = restart
                        fallback_nets = set(initial_fallback_nets)
                        attempted_repairs = RepairAttemptMemory()
                        last_rejected_nets = set()
                        if progress:
                            progress(
                                {"event": "repair_order", "strategy": "shortest_first"}
                            )
                        continue
                    delta = Delta(
                        tuple(item for item in delta.traces if item not in bad),
                        tuple(item for item in delta.vias if item not in bad),
                        delta.excluded_nets,
                        delta.unsupported,
                    )
                continue
            proposed_nets = {item.net for item in (*delta.traces, *delta.vias)}
            regressions = regressed_open_nets(
                baseline_open_counts, candidate_open_counts, proposed_nets
            )
            if regressions:
                regressed = set(regressions)
                delta = without_nets(delta, regressed)
                remember_blocked_nets(
                    repair_blockers,
                    regressed,
                    "Proposal creates an additional isolated copper island",
                )
                if progress:
                    progress({"event": "island_regression", "nets": list(regressions)})
                continue
            summary = summarize_drc(
                report,
                segment_identities=candidate_ids,
                dangling_identities=_dangling_track_identities(candidate_board, report),
            )
            try:
                improved = checked_improvement(
                    baseline, summary, stable_connection_items=stable
                )
            except RuntimeError as error:
                if not retryable_native_rejection(error):
                    raise
                implicated = newly_implicated_nets(
                    baseline_report,
                    report,
                    baseline_ids,
                    candidate_ids,
                    native_item_nets(candidate_board),
                    proposed_nets,
                )
                remainder = (
                    cleanup_remainder(delta, set(implicated))
                    if implicated and native_retry_count < 3
                    else None
                )
                targeted = remainder is not None
                if (
                    remainder is None
                    and native_retry_count < 3
                    and repair_budget_available(time.monotonic() - started, max_seconds)
                ):
                    if connectivity_retries is None:
                        connectivity_retries = ConnectivityRetries(delta)
                    trial = connectivity_retries.next()
                    if trial is not None:
                        remainder, implicated = trial
                if remainder is not None:
                    delta = remainder
                    if targeted:
                        connectivity_retries = None
                        connectivity_omitted = ()
                    else:
                        connectivity_omitted = implicated
                    native_retry_count += 1
                    pending_net_prune = None
                    last_rejected_nets = set()
                    salvaging = True
                    remember_blocked_nets(
                        repair_blockers,
                        set(implicated),
                        "Deferred after a new native violation",
                    )
                    if progress:
                        progress(
                            {
                                "event": "native_violation_retry",
                                "nets": list(implicated),
                            }
                        )
                    continue
                remember_blocked_nets(
                    repair_blockers,
                    proposed_nets,
                    "No checked subset avoided new native violations",
                )
                return {
                    "status": "filter_stalled",
                    "reason": str(error),
                    "baseline_opens": baseline["open_connections"],
                    "dominant_class": deferred_class(class_counts, rejected_classes),
                    "width_blockers": width_blockers,
                    "repair_blockers": repair_blockers,
                }
            if not improved:
                trial = (
                    connectivity_retries.next()
                    if connectivity_retries is not None
                    else None
                )
                if trial is not None:
                    delta, connectivity_omitted = trial
                    if progress:
                        progress(
                            {
                                "event": "connectivity_retry",
                                "nets": list(connectivity_omitted),
                            }
                        )
                    continue
                remember_blocked_nets(
                    repair_blockers,
                    last_rejected_nets,
                    "Proposal did not improve connectivity",
                )
                return {
                    "status": "no_improvement",
                    "baseline_opens": baseline["open_connections"],
                    "candidate_opens": summary["open_connections"],
                    "dominant_class": deferred_class(class_counts, rejected_classes),
                    "width_blockers": width_blockers,
                    "repair_blockers": repair_blockers,
                }
            if new_style_issues(
                normalize_segment_identities(baseline_style, baseline_ids),
                normalize_segment_identities(candidate_style, candidate_ids),
            ):
                raise RuntimeError(
                    "The filtered candidate has new routing-style issues"
                )
            try:
                connected = check_intermediate_connectivity(
                    stage,
                    retained,
                    allow_temporary_disconnect=allow_temporary_disconnect,
                )
            except RuntimeError as error:
                if (
                    str(error).partition(":")[0]
                    != "Resumed candidate broke an existing copper connection"
                ):
                    raise
                if connectivity_retries is None:
                    connectivity_retries = ConnectivityRetries(delta)
                trial = connectivity_retries.next()
                if trial is None:
                    remember_blocked_nets(
                        repair_blockers,
                        proposed_nets,
                        "No checked subset preserved existing connectivity",
                    )
                    return {
                        "status": "filter_stalled",
                        "baseline_opens": baseline["open_connections"],
                        "dominant_class": deferred_class(
                            class_counts, rejected_classes
                        ),
                        "width_blockers": width_blockers,
                        "repair_blockers": repair_blockers,
                    }
                delta, connectivity_omitted = trial
                last_rejected_nets = set()
                pending_net_prune = None
                salvaging = True
                if progress:
                    progress(
                        {
                            "event": "connectivity_retry",
                            "nets": list(connectivity_omitted),
                        }
                    )
                continue
            if _fingerprints(_source_files(source), project) != fingerprints:
                raise RuntimeError(
                    "The source changed during external proposal filtering"
                )
            if connected and connectivity_omitted:
                remember_blocked_nets(
                    repair_blockers,
                    set(connectivity_omitted),
                    "Deferred while preserving existing copper connectivity",
                )
            for name in tuple(repair_blockers):
                if candidate_open_counts.get(name, 0) < baseline_open_counts.get(
                    name, 0
                ):
                    repair_blockers.pop(name)
            return {
                "status": "filtered_preview"
                if connected
                else "deferred_connectivity_preview",
                "intermediate_connectivity_verified": connected,
                "board": candidate_bytes,
                "baseline_opens": baseline["open_connections"],
                "candidate_opens": summary["open_connections"],
                "errors": summary["errors"],
                "warnings": summary["warnings"],
                "passes": index + 1,
                "rejected_items": rejected_total,
                "excluded_nets": list(delta.excluded_nets),
                "dominant_class": deferred_class(class_counts, rejected_classes),
                "width_blockers": width_blockers,
                "repair_blockers": repair_blockers,
            }
        remember_blocked_nets(
            repair_blockers, last_rejected_nets, "Cleanup reached its pass limit"
        )
        return {
            "status": "filter_stalled",
            "baseline_opens": baseline["open_connections"],
            "dominant_class": deferred_class(class_counts, rejected_classes),
            "width_blockers": width_blockers,
            "repair_blockers": repair_blockers,
        }
