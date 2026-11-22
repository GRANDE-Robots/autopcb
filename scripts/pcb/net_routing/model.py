"""Small immutable search records and explicit resource budgets."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, fields
from contextlib import contextmanager

from pcb.route_optimizer import Point, Route, SearchBudgetExceeded, Segment


@dataclass(frozen=True)
class Site:
    point: Point
    layer: str

    @property
    def key(self) -> tuple[int, int, str]:
        return self.point.x, self.point.y, self.layer


@dataclass(frozen=True)
class Island:
    identifier: str
    sites: tuple[Site, ...]
    pad_ids: tuple[str, ...] = ()
    zone_anchors: tuple[tuple[Site, Site], ...] = ()


@dataclass(frozen=True)
class Net:
    name: str
    islands: tuple[Island, ...]
    existing: tuple[Segment, ...] = ()
    pad_sites: frozenset[Site] = frozenset()


@dataclass(frozen=True)
class NetPlan:
    net: str
    route: Route
    rework_cost: float = 0.0
    restored_widths: tuple[tuple[Segment, int], ...] = ()
    source_modified: bool = False

    @property
    def cost(self) -> float:
        return self.route.cost + self.rework_cost

    @property
    def signature(self) -> tuple:
        return (
            self.net,
            tuple(
                (s.layer, s.start.x, s.start.y, s.end.x, s.end.y)
                for s in self.route.segments
            ),
            tuple(sorted((v.at.x, v.at.y) for v in self.route.vias)),
            tuple(sorted((s.layer, s.start.x, s.start.y, s.end.x, s.end.y, width)
                         for s, width in self.restored_widths)),
        )


@dataclass(frozen=True)
class Limits:
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
        if not isinstance(raw, dict) or set(raw) - {f.name for f in fields(cls)}:
            raise ValueError("Unknown search limit or search must be an object")
        values = dict(raw)
        # Older configurations used this as a hard cap. Preserve that meaning
        # unless they explicitly opt into a wider fallback sample.
        if "connections_per_step" in values and "fallback_connections_per_step" not in values:
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
            raise ValueError('An endpoint time slice requires a positive choice count')
        previous, previous_limit = self.slice_deadline, self.slice_limit
        if count > 1:
            now = time.monotonic()
            deadline = min(self.started + self.limits.seconds,
                           previous if previous is not None else math.inf)
            share = min(self.limits.endpoint_seconds, max(0.0, deadline - now) / min(count, 4))
            self.slice_deadline = now + share
            self.slice_limit = 'endpoint_seconds'
        try:
            yield
        finally:
            self.slice_deadline, self.slice_limit = previous, previous_limit

    def call(self) -> None:
        self.check()
        if self.calls >= self.limits.path_calls:
            self.hit_limit("path_calls")
            raise BudgetExhausted("Path-call budget exhausted")
        self.calls += 1

    def failure(self, reason: str, detail: object | None = None) -> None:
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
            "failure_details": {key: list(values) for key, values in self.failure_details.items()},
            "elapsed_seconds": round(elapsed, 3),
            "optimality": "not_proven",
        }
