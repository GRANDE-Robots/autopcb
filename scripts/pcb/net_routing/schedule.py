"""Try net orders while retaining alternate earlier board states."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable

from pcb.net_routing.model import Budget, BudgetExhausted, Net, NetPlan


@dataclass(frozen=True)
class BoardPlan:
    plans: tuple[NetPlan, ...] = ()

    @property
    def cost(self) -> float:
        return sum(p.cost for p in self.plans)

    @property
    def signature(self) -> tuple:
        return tuple(sorted(p.signature for p in self.plans))


def net_orders(
    nets: tuple[Net, ...], budget: Budget, requested: object = None
) -> tuple[tuple[str, ...], ...]:
    names = tuple(net.name for net in nets)
    if not names or len(set(names)) != len(names):
        raise ValueError("Net names must be nonempty and distinct")
    if requested is not None:
        if not isinstance(requested, list) or not requested:
            raise ValueError("orders must be a nonempty array of complete net orders")
        result = []
        for order in requested:
            if not isinstance(order, list) or any(
                not isinstance(n, str) for n in order
            ):
                raise ValueError("Each order must list net names")
            if len(order) != len(names) or set(order) != set(names):
                raise ValueError(
                    "Each order must contain every requested net exactly once"
                )
            if tuple(order) not in result:
                result.append(tuple(order))
        if len(result) > budget.limits.order_trials:
            raise ValueError("Explicit orders exceed order_trials")
        return tuple(result)
    choices = [
        names,
        tuple(reversed(names)),
        tuple(n.name for n in sorted(nets, key=lambda n: (-len(n.islands), n.name))),
    ]
    rng = random.Random(budget.limits.seed)
    unique: list[tuple[str, ...]] = []
    for index in range(budget.limits.order_trials * 20):
        order = (
            choices[index]
            if index < len(choices)
            else tuple(rng.sample(names, len(names)))
        )
        if order not in unique:
            unique.append(order)
        if len(unique) == budget.limits.order_trials:
            break
    return tuple(unique)


def search_orders(
    nets: tuple[Net, ...],
    budget: Budget,
    solve: Callable[[Net, tuple[NetPlan, ...]], tuple[NetPlan, ...]],
    requested: object = None,
    *,
    preferred_first: frozenset[str] = frozenset(),
) -> tuple[tuple[BoardPlan, ...], tuple[dict, ...]]:
    """A beam retains earlier choices so a failed cheap branch can be abandoned."""
    by_name = {net.name: net for net in nets}
    complete: dict[tuple, BoardPlan] = {}
    trials = []
    # Alternate orders must not repeat an identical failed empty-board search.
    # The cache belongs to this snapshot, placement, checkpoint and budget only.
    independent: dict[str, tuple[NetPlan, ...]] = {}
    if requested is None:
        ordered_nets = tuple(n for n in nets if n.name in preferred_first) + tuple(
            n for n in nets if n.name not in preferred_first
        )
    else:
        ordered_nets = nets
    orders = []
    for proposed_order in net_orders(ordered_nets, budget, requested):
        # Nets with one surviving island need only an electrical/rework check.
        # Check them first so they do not reserve a full routing time slice.
        order = tuple(
            name for name in proposed_order if len(by_name[name].islands) <= 1
        ) + tuple(name for name in proposed_order if len(by_name[name].islands) > 1)
        if order in orders:
            continue
        orders.append(order)
    for order_index, order in enumerate(orders):
        frontier = [BoardPlan()]
        reached = 0
        trial_costs = []
        searches = []
        with budget.order_slice(reserve_alternative=order_index + 1 < len(orders)):
            try:
                for name in order:
                    proposed: dict[tuple, BoardPlan] = {}
                    for board in frontier:
                        budget.check()
                        prior_limits = budget.limit_events
                        cached = not board.plans and name in independent
                        if cached:
                            candidates = independent[name]
                        else:
                            with budget.net_slice(len(nets) - reached):
                                candidates = solve(by_name[name], board.plans)
                            if not board.plans and budget.limit_events == prior_limits:
                                independent[name] = candidates
                        searches.append(
                            {
                                "net": name,
                                "prior_nets": [p.net for p in board.plans],
                                "candidates": len(candidates),
                                "cached": cached,
                                "budget_limited": budget.limit_events > prior_limits,
                            }
                        )
                        for candidate in candidates:
                            if candidate.net != name:
                                raise ValueError("Net backend returned the wrong net")
                            result = BoardPlan((*board.plans, candidate))
                            proposed[result.signature] = result
                            if len(result.plans) == len(nets):
                                complete[result.signature] = result
                                trial_costs.append(result.cost)
                    budget.pruned += max(0, len(proposed) - budget.limits.board_beam)
                    frontier = sorted(
                        proposed.values(), key=lambda b: (b.cost, b.signature)
                    )[: budget.limits.board_beam]
                    if not frontier:
                        break
                    reached += 1
            except BudgetExhausted:
                pass
        trials.append(
            {
                "order": list(order),
                "nets_reached": reached,
                "complete_candidates": sum(len(b.plans) == len(nets) for b in frontier),
                "best_complete_cost": min(trial_costs) if trial_costs else None,
                "searches": searches,
            }
        )
        if budget.limits_hit.intersection({"seconds", "path_calls"}):
            break
        # Bound retained complete boards across order trials as well.
        complete = dict(
            sorted(complete.items(), key=lambda item: item[1].cost)[
                : budget.limits.board_beam
            ]
        )
    return tuple(
        sorted(complete.values(), key=lambda b: (b.cost, b.signature))[
            : budget.limits.board_beam
        ]
    ), tuple(trials)
