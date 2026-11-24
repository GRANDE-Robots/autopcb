"""model.py: small immutable search records and explicit resource budgets.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, fields
from contextlib import contextmanager

from pcb.route_optimizer import PadCopper, Point, Route, SearchBudgetExceeded, Segment


@dataclass(frozen=True)
class Site:
    """A routing access point: a position on one copper layer.

    Inputs:
        point: board position in nm.
        layer: copper layer name.
    """

    point: Point
    layer: str

    @property
    def key(self) -> tuple[int, int, str]:
        """Sort key: (x, y, layer), with x and y in nm."""
        return self.point.x, self.point.y, self.layer


@dataclass(frozen=True)
class Island:
    """A physically connected group of existing same-net copper.

    Inputs:
        identifier: UUID of the item the island was discovered from.
        sites: access points on the island that routes may start or end at.
        pad_ids: UUIDs of the pads on the island.
        zone_anchors: (plane site, physical site) pairs; the plane site stands for
            a point on a filled zone connected to the physical site.
    """

    identifier: str
    sites: tuple[Site, ...]
    pad_ids: tuple[str, ...] = ()
    zone_anchors: tuple[tuple[Site, Site], ...] = ()


@dataclass(frozen=True)
class Net:
    """One net's routing problem: its islands and the copper already present.

    Inputs:
        name: net name.
        islands: the copper islands to join.
        existing: straight existing track segments of the net.
        pad_sites: sites that lie on pads.
        pad_copper: pad copper rectangles that anchor track ends.
    """

    name: str
    islands: tuple[Island, ...]
    existing: tuple[Segment, ...] = ()
    pad_sites: frozenset[Site] = frozenset()
    pad_copper: PadCopper = PadCopper()


@dataclass(frozen=True)
class NetPlan:
    """A chosen route for one net with the cost of changing existing copper.

    Inputs:
        net: net name.
        route: the proposed new copper.
        rework_cost: cost of the existing copper the plan changes.
        restored_widths: (segment, width in nm) pairs of restored source copper.
        source_modified: True when the restored copper was changed, not kept as it was.
    """

    net: str
    route: Route
    rework_cost: float = 0.0
    restored_widths: tuple[tuple[Segment, int], ...] = ()
    source_modified: bool = False

    @property
    def cost(self) -> float:
        """Route cost plus rework cost."""
        return self.route.cost + self.rework_cost

    @property
    def signature(self) -> tuple:
        """A hashable description of the plan's net, segments, via positions and
        restored widths."""
        return (
            self.net,
            tuple(
                (s.layer, s.start.x, s.start.y, s.end.x, s.end.y)
                for s in self.route.segments
            ),
            tuple(sorted((v.at.x, v.at.y) for v in self.route.vias)),
            tuple(
                sorted(
                    (s.layer, s.start.x, s.start.y, s.end.x, s.end.y, width)
                    for s, width in self.restored_widths
                )
            ),
        )


@dataclass(frozen=True)
class Limits:
    """Resource limits and sampling widths of a search.

    Inputs:
        tree_beam: partial trees kept per step.
        board_beam: partial board plans kept per step.
        net_candidates: complete candidates kept per net.
        path_candidates: alternative paths kept per endpoint pair.
        connections_per_step: endpoint pairs tried per tree.
        fallback_connections_per_step: wider endpoint sample used when the first yields
            no useful tree.
        sites_per_island: access sites kept per island.
        branch_sites: branch sites sampled on tree copper.
        order_trials: net orders tried.
        placement_trials: explicit placement combinations allowed.
        path_calls: path searches allowed per batch.
        net_path_calls: path searches allowed per net.
        endpoint_seconds: time share for one access point, in seconds.
        cache_entries: path results cached.
        validation_candidates: candidates carried to validation.
        smoothing_trials: shortcut trials per route; 0 disables smoothing.
        seconds: search time limit, in seconds.
        seed: seed for generated net orders.
    """

    tree_beam: int = 4
    board_beam: int = 4
    net_candidates: int = 3
    path_candidates: int = 2
    connections_per_step: int = 12
    fallback_connections_per_step: int = 96
    sites_per_island: int = 48
    branch_sites: int = 32
    order_trials: int = 4
    placement_trials: int = 8
    path_calls: int = 400
    net_path_calls: int = 120
    endpoint_seconds: int = 5
    cache_entries: int = 128
    validation_candidates: int = 3
    smoothing_trials: int = 256
    seconds: float = 120.0
    seed: int = 0

    @classmethod
    def from_config(cls, raw: object) -> Limits:
        """Build limits from a configuration object.

        Inputs:
            raw: mapping of field names to values; omitted fields keep their defaults.
                A supplied connections_per_step without fallback_connections_per_step
                also caps the fallback sample.
        Outputs:
            The limits. Raises ValueError for a non-mapping, an unknown field, a
            seconds value that is not a positive finite number, or a count that is not
            an integer of at least 1 (0 for seed and smoothing_trials).
        """
        if not isinstance(raw, dict) or set(raw) - {f.name for f in fields(cls)}:
            raise ValueError("Unknown search limit or search must be an object")
        values = dict(raw)
        # Older configurations used this as a hard cap. Preserve that meaning
        # unless they explicitly opt into a wider fallback sample.
        if (
            "connections_per_step" in values
            and "fallback_connections_per_step" not in values
        ):
            values["fallback_connections_per_step"] = values["connections_per_step"]
        result = cls(**values)
        for item in fields(cls):
            value = getattr(result, item.name)
            if item.name == "seconds":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError("seconds must be a positive finite number")
                if not math.isfinite(value) or value <= 0:
                    raise ValueError("seconds must be a positive finite number")
            elif (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < (0 if item.name in {"seed", "smoothing_trials"} else 1)
            ):
                raise ValueError(f"{item.name} must be a valid integer")
        return result


class BudgetExhausted(SearchBudgetExceeded):
    """A configured search limit was reached; feasibility is still unknown."""


@dataclass
class Budget:
    """Counters and deadlines that bound one search, with a report of what it used.

    Inputs:
        limits: the limits to enforce.
        started: monotonic start time, in seconds.
        calls, expanded, cache_hits, pruned: running counts.
        failures: counts by failure reason.
        failure_details: up to three example messages per reason.
        limits_hit: names of the limits reached.
        limit_events: number of limit events recorded.
        finished_at: monotonic time the search stopped, or None while running.
        slice_deadline: monotonic deadline of the current time slice, or None.
        slice_limit: name recorded when the slice deadline is reached.
    """

    limits: Limits
    started: float = field(default_factory=time.monotonic)
    calls: int = 0
    expanded: int = 0
    cache_hits: int = 0
    pruned: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    failure_details: dict[str, list[str]] = field(default_factory=dict)
    limits_hit: set[str] = field(default_factory=set)
    limit_events: int = 0
    finished_at: float | None = None
    slice_deadline: float | None = None
    slice_limit: str = "net_seconds"

    def hit_limit(self, name: str) -> None:
        """Record each limit event, even when the same limit was hit before."""
        self.limits_hit.add(name)
        self.limit_events += 1

    def check(self) -> None:
        """Raise BudgetExhausted when the search time or the current time slice is used
        up."""
        if time.monotonic() - self.started >= self.limits.seconds:
            self.hit_limit("seconds")
            raise BudgetExhausted("Search time budget exhausted")
        if self.slice_deadline is not None and time.monotonic() >= self.slice_deadline:
            self.hit_limit(self.slice_limit)
            raise BudgetExhausted("This search's share of the batch time is exhausted")

    @contextmanager
    def order_slice(self, *, reserve_alternative: bool):
        """Leave part of the remaining budget for another distinct net order."""
        previous, previous_limit = self.slice_deadline, self.slice_limit
        if reserve_alternative:
            now = time.monotonic()
            deadline = self.started + self.limits.seconds
            if previous is not None:
                deadline = min(deadline, previous)
            self.slice_deadline = now + max(0.0, deadline - now) * (2 / 3)
            self.slice_limit = "order_seconds"
        try:
            yield
        finally:
            self.slice_deadline, self.slice_limit = previous, previous_limit

    @contextmanager
    def net_slice(self, count: int):
        """Share the remaining batch time among nets still to be searched."""
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("A net time slice requires a positive net count")
        previous, previous_limit = self.slice_deadline, self.slice_limit
        if count > 1:
            now = time.monotonic()
            deadline = self.started + self.limits.seconds
            if previous is not None:
                deadline = min(deadline, previous)
            self.slice_deadline = now + max(0.0, deadline - now) / count
            self.slice_limit = "net_seconds"
        try:
            yield
        finally:
            self.slice_deadline, self.slice_limit = previous, previous_limit

    @contextmanager
    def tree_slice(self, count: int):
        """Share a net's remaining time among distinct starting islands."""
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("A starting-island time slice requires a positive count")
        previous, previous_limit = self.slice_deadline, self.slice_limit
        if count > 1:
            now = time.monotonic()
            deadline = self.started + self.limits.seconds
            if previous is not None:
                deadline = min(deadline, previous)
            self.slice_deadline = now + max(0.0, deadline - now) / count
            self.slice_limit = "tree_seconds"
        try:
            yield
        finally:
            self.slice_deadline, self.slice_limit = previous, previous_limit

    @contextmanager
    def endpoint_slice(self, count: int):
        """Leave time for other access points on the same copper island."""
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("An endpoint time slice requires a positive choice count")
        previous, previous_limit = self.slice_deadline, self.slice_limit
        if count > 1:
            now = time.monotonic()
            deadline = min(
                self.started + self.limits.seconds,
                previous if previous is not None else math.inf,
            )
            share = min(
                self.limits.endpoint_seconds, max(0.0, deadline - now) / min(count, 4)
            )
            self.slice_deadline = now + share
            self.slice_limit = "endpoint_seconds"
        try:
            yield
        finally:
            self.slice_deadline, self.slice_limit = previous, previous_limit

    def call(self) -> None:
        """Count one path search.

        Outputs:
            None. Raises BudgetExhausted when time is used up or the path-call limit
            is already reached.
        """
        self.check()
        if self.calls >= self.limits.path_calls:
            self.hit_limit("path_calls")
            raise BudgetExhausted("Path-call budget exhausted")
        self.calls += 1

    def failure(self, reason: str, detail: object | None = None) -> None:
        """Count a failure and keep up to three distinct example messages per reason.

        Inputs:
            reason: category of the failure.
            detail: optional description; stored as text trimmed to 400 characters.
        """
        self.failures[reason] = self.failures.get(reason, 0) + 1
        if detail is not None:
            message = str(detail).strip()[:400]
            examples = self.failure_details.setdefault(reason, [])
            if message and message not in examples and len(examples) < 3:
                examples.append(message)

    def finish_search(self) -> None:
        """Stop search timing before potentially slow native validation."""
        if self.finished_at is None:
            self.finished_at = time.monotonic()

    def report(self) -> dict:
        """Summarize the search.

        Outputs:
            A mapping of path calls, expanded states, cache hits, pruned candidates,
            limits hit, failures with details, elapsed seconds (rounded to ms), and
            optimality, which is always not_proven.
        """
        ended = self.finished_at if self.finished_at is not None else time.monotonic()
        elapsed = ended - self.started
        observed_limits = self.limits_hit | (
            {"seconds"} if elapsed >= self.limits.seconds else set()
        )
        return {
            "path_calls": self.calls,
            "expanded_states": self.expanded,
            "cache_hits": self.cache_hits,
            "pruned_candidates": self.pruned,
            "limits_hit": sorted(observed_limits),
            "failures": dict(self.failures),
            "failure_details": {
                key: list(values) for key, values in self.failure_details.items()
            },
            "elapsed_seconds": round(elapsed, 3),
            "optimality": "not_proven",
        }
