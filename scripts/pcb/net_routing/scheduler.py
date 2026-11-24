"""scheduler.py: group nearby net work and schedule only disjoint search windows in parallel.

A search window is (x_min, y_min, x_max, y_max) in millimetres.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import math
from dataclasses import dataclass


Window = tuple[float, float, float, float]


class SearchWindow:
    """Validation and geometry of rectangular search windows in millimetres."""

    @staticmethod
    def check(window: Window) -> None:
        """Raise ValueError unless the window is four finite numbers with each minimum not above its maximum."""
        if (
            not isinstance(window, tuple)
            or len(window) != 4
            or any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                for v in window
            )
            or window[0] > window[2]
            or window[1] > window[3]
        ):
            raise ValueError("A search window needs four ordered finite coordinates")

    @staticmethod
    def overlaps(a: Window, b: Window) -> bool:
        """Say whether two windows share any area or touch.

        Inputs:
            a, b: Search windows; ValueError is raised for an invalid window.
        """
        SearchWindow.check(a)
        SearchWindow.check(b)
        return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])

    @staticmethod
    def union(a: Window, b: Window) -> Window:
        """Return the smallest window that contains both windows."""
        return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


@dataclass(frozen=True)
class WorkItem:
    """One net that needs routing.

    Inputs:
        name: Net name.
        priority: Integer rank; lower ranks are scheduled first.
        window: Search window in millimetres.
        island_count: Number of disconnected copper islands, at least two.
    """

    name: str
    priority: int
    window: Window
    island_count: int

    def __post_init__(self) -> None:
        """Raise ValueError unless the fields describe a schedulable net."""
        SearchWindow.check(self.window)
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("A work item needs a nonempty net name")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ValueError("A work item needs an integer priority")
        if (
            isinstance(self.island_count, bool)
            or not isinstance(self.island_count, int)
            or self.island_count < 2
        ):
            raise ValueError("A routing work item needs at least two copper islands")


@dataclass(frozen=True)
class RoutingBatch:
    """Work items searched together and the window that contains them."""

    items: tuple[WorkItem, ...]
    window: Window


@dataclass(frozen=True)
class RoutingRound:
    """Batches with pairwise disjoint windows, searched in parallel."""

    batches: tuple[RoutingBatch, ...]


@dataclass(frozen=True)
class BoardWork:
    """Classification of every net on a board.

    Inputs:
        items: Nets that need routing, in priority order.
        connected_nets: Nets that already have one copper island.
        intentional_no_connect_nets: Nets whose pads the schematic marks no-connect.
        blocked_nets: Nets that cannot be scheduled.
        blockers: Reasons for the blocked nets and unassigned copper.
    """

    items: tuple[WorkItem, ...]
    connected_nets: tuple[str, ...]
    intentional_no_connect_nets: tuple[str, ...]
    blocked_nets: tuple[str, ...]
    blockers: tuple[str, ...]


class WorkFromBoard:
    """Builds the scheduling work for a native board and its inventory."""

    def __call__(
        self, board, inventory, priorities: dict[str, int], *, margin_mm: float = 8.0
    ) -> BoardWork:
        """Create work for every multi-island net, including orphan track islands."""
        import pcbnew

        from core.pcb_io import copper_width
        from pcb.net_routing.inventory import BoardInventory

        if not isinstance(inventory, BoardInventory) or not isinstance(
            priorities, dict
        ):
            raise ValueError(
                "Board work requires a native inventory and priority mapping"
            )
        if (
            isinstance(margin_mm, bool)
            or not isinstance(margin_mm, (int, float))
            or not math.isfinite(margin_mm)
            or margin_mm < 0
        ):
            raise ValueError("Search margin must be finite and nonnegative")
        if any(
            not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, int)
            for k, v in priorities.items()
        ):
            raise ValueError("Priorities must map net names to integer ranks")
        box = board.GetBoardEdgesBoundingBox()
        if box.GetWidth() <= 0 or box.GetHeight() <= 0:
            raise ValueError("A valid board outline is required for scheduling")
        bounds = (
            box.GetX() / 1e6,
            box.GetY() / 1e6,
            box.GetRight() / 1e6,
            box.GetBottom() / 1e6,
        )
        lookup = {
            item.m_Uuid.AsString(): item
            for item in (*board.GetPads(), *board.GetTracks())
        }
        verified = set(inventory.verified_no_connect_pad_ids)
        work = []
        connected = []
        intentional = []
        blocked_nets = []
        blockers = []
        if inventory.unassigned_pad_ids:
            blockers.append(
                f"{len(inventory.unassigned_pad_ids)} unassigned copper pads"
            )
        if inventory.unassigned_copper_ids:
            blockers.append(
                f"{len(inventory.unassigned_copper_ids)} unassigned track or via items"
            )
        for net in inventory.nets:
            if net.name.startswith("unconnected-"):
                if (
                    net.pad_ids
                    and set(net.pad_ids) <= verified
                    and not (net.track_ids or net.via_ids or net.zone_ids)
                ):
                    intentional.append(net.name)
                else:
                    blockers.append(
                        f"No-connect intent or copper is unresolved: {net.name}"
                    )
                    blocked_nets.append(net.name)
                continue
            if not net.pad_ids:
                blockers.append(f"Net has copper but no schematic terminal: {net.name}")
                blocked_nets.append(net.name)
                continue
            if len(net.copper_islands) <= 1:
                connected.append(net.name)
                continue
            coordinates = []
            for uid in (*net.pad_ids, *net.track_ids, *net.via_ids):
                item = lookup[uid]
                if isinstance(item, pcbnew.PCB_VIA):
                    layer_ids = tuple(board.GetEnabledLayers().CuStack())
                    widths = [
                        copper_width(item, layer=layer)
                        for layer in layer_ids
                        if item.IsOnLayer(layer)
                    ]
                    if not widths:
                        raise ValueError(f"Via {uid} has no supported copper layer")
                    point = item.GetPosition()
                    radius = max(widths) / 2
                    coordinates.extend(
                        (
                            (point.x - radius, point.y - radius),
                            (point.x + radius, point.y + radius),
                        )
                    )
                else:
                    item_box = item.GetBoundingBox()
                    coordinates.extend(
                        (
                            (item_box.GetX(), item_box.GetY()),
                            (item_box.GetRight(), item_box.GetBottom()),
                        )
                    )
            if not coordinates:
                blockers.append(f"Net has no schedulable copper geometry: {net.name}")
                blocked_nets.append(net.name)
                continue
            window = (
                max(
                    bounds[0], min(point[0] for point in coordinates) / 1e6 - margin_mm
                ),
                max(
                    bounds[1], min(point[1] for point in coordinates) / 1e6 - margin_mm
                ),
                min(
                    bounds[2], max(point[0] for point in coordinates) / 1e6 + margin_mm
                ),
                min(
                    bounds[3], max(point[1] for point in coordinates) / 1e6 + margin_mm
                ),
            )
            try:
                work.append(
                    WorkItem(
                        net.name,
                        priorities.get(net.name, 100),
                        window,
                        len(net.copper_islands),
                    )
                )
            except ValueError:
                blockers.append(
                    f"Net has copper outside the schedulable board: {net.name}"
                )
                blocked_nets.append(net.name)
        classified = (
            {item.name for item in work}
            | set(connected)
            | set(intentional)
            | set(blocked_nets)
        )
        if classified != {net.name for net in inventory.nets}:
            raise RuntimeError("The board-wide work inventory does not cover every net")
        return BoardWork(
            tuple(sorted(work, key=lambda item: (item.priority, item.name))),
            tuple(sorted(connected)),
            tuple(sorted(intentional)),
            tuple(sorted(blocked_nets)),
            tuple(sorted(blockers)),
        )


work_from_board = WorkFromBoard()


class NetSchedule:
    """Groups work items into batches and rounds."""

    def __call__(
        self, items: tuple[WorkItem, ...], *, workers: int = 2, max_batch_nets: int = 6
    ) -> tuple[RoutingRound, ...]:
        """Schedule all input nets exactly once; combined native checks remain mandatory."""
        if any(
            isinstance(v, bool) or not isinstance(v, int) or v < 1
            for v in (workers, max_batch_nets)
        ):
            raise ValueError("Worker and batch limits must be positive integers")
        if any(not isinstance(item, WorkItem) for item in items):
            raise ValueError("Every scheduled item must be a WorkItem")
        if len({item.name for item in items}) != len(items):
            raise ValueError("Scheduled net names must be distinct")
        pending = sorted(items, key=lambda item: (item.priority, item.name))
        batches = []
        while pending:
            seed = pending.pop(0)
            selected = [seed]
            window = seed.window
            index = 0
            while index < len(pending) and len(selected) < max_batch_nets:
                candidate = pending[index]
                if SearchWindow.overlaps(window, candidate.window):
                    selected.append(pending.pop(index))
                    window = SearchWindow.union(window, candidate.window)
                else:
                    index += 1
            batches.append(RoutingBatch(tuple(selected), window))

        rounds = []
        while batches:
            selected = [batches.pop(0)]
            index = 0
            while index < len(batches) and len(selected) < workers:
                candidate = batches[index]
                if all(
                    not SearchWindow.overlaps(candidate.window, prior.window)
                    for prior in selected
                ):
                    selected.append(batches.pop(index))
                else:
                    index += 1
            rounds.append(RoutingRound(tuple(selected)))
        return tuple(rounds)


schedule = NetSchedule()
