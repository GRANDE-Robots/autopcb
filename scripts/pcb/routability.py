"""routability.py: prove which pads no route can join while the parts stay in place.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.pcb_io import enabled_copper_layers, net_classes
from pcb.net_routing.obstructions import BoardObstacles
from pcb.route_optimizer import GridRouter, Point, mm
from pcb.topology_bound import UNREACHABLE, minimum_vias


@dataclass(frozen=True)
class Terminal:
    """The grid states a pad can start a track from.

    Inputs:
        label: Pad name as REFERENCE.NUMBER.
        states: Free grid state indices (layer index times cell count plus cell
            index) in or beside the pad.
    """

    label: str
    states: tuple[int, ...]


class GridModel:
    """The router's grid and obstacles for a board, optionally with parts left out.

    ``footprints`` maps a footprint UUID to a detached copy at another pose,
    or to None to leave its pads out. ``policy`` is the routing policy: its
    layers are the ones routed, and its widths and clearances apply on top
    of the net classes. The search ignores bends and route order, and
    clearances are eased by one grid step so no legal path can fall between
    grid points: it reaches every place the detailed router can. With
    ``saved_copper``, the board's tracks and vias count as obstacles too.
    """

    MAX_CELLS = 2_000_000

    def __init__(
        self,
        board,
        *,
        footprints=None,
        policy=None,
        critical_nets=(),
        saved_copper=False,
        step=mm(0.05),
    ):
        """Read the board's obstacles and fix the grid.

        Inputs:
            board: Loaded KiCad board.
            footprints: Footprint UUID to a detached copy at another pose, or to None
                to leave its pads out.
            policy: Routing policy; its route layers and assume_zone_refill option
                apply, and its widths and clearances apply on top of the net classes.
            critical_nets: Nets whose reference corridors count as obstacles.
            saved_copper: Whether the board's tracks and vias count as obstacles.
            step: Smallest grid pitch in nanometres; it grows until the grid fits
                MAX_CELLS.
        """
        self.board, self.footprints, self.policy = board, footprints or {}, policy or {}
        self.saved_copper = saved_copper
        self.layers = tuple(
            self.policy.get("route", {}).get("layers") or enabled_copper_layers(board)
        )
        self.obstacles = BoardObstacles.read(
            board,
            critical_nets,
            footprints=self.footprints,
            assume_zone_refill=self.policy.get("route", {}).get(
                "assume_zone_refill", False
            ),
        )
        box = board.GetBoardEdgesBoundingBox()
        self.edge = board.GetDesignSettings().m_CopperEdgeClearance
        self.outline = (box.GetX(), box.GetY(), box.GetRight(), box.GetBottom())
        area = (self.outline[2] - self.outline[0]) * (self.outline[3] - self.outline[1])
        self.step = max(
            step,
            math.ceil(math.sqrt(area * len(self.layers) / self.MAX_CELLS) / 10_000)
            * 10_000,
        )

    def parts(self):
        """Yield each footprint of the board as posed in footprints.

        Footprints set to None are left out.
        """
        for footprint in self.board.GetFootprints():
            part = self.footprints.get(footprint.m_Uuid.AsString(), footprint)
            if part is not None:
                yield part

    def pads(self, net: str) -> list:
        """The pads of the net on the posed footprints."""
        return [
            pad
            for part in self.parts()
            for pad in part.Pads()
            if pad.GetNetname() == net
        ]

    def rules(self, net: str) -> tuple[int, int, int, int]:
        """The routing rules of a net.

        Outputs:
            Track width, clearance, via diameter and via drill, all in nanometres:
            the largest of the net classes of its pads and the policy. Raises
            ValueError when no pad of the net has a net class.
        """
        classes = net_classes(self.board)
        limits = [
            classes[pad.GetNetClassName()]
            for pad in self.pads(net)
            if pad.GetNetClassName() in classes
        ]
        if not limits:
            raise ValueError(f"{net} has no net class")
        options = {
            **self.policy.get("route", {}),
            **self.policy.get("net_overrides", {}).get(net, {}),
        }
        via = options.get("via", {})
        return (
            max(mm(options.get("width_mm", 0)), *(c.GetTrackWidth() for c in limits)),
            max(
                mm(options.get("clearance_mm", 0)), *(c.GetClearance() for c in limits)
            ),
            max(mm(via.get("size_mm", 0)), *(c.GetViaDiameter() for c in limits)),
            max(mm(via.get("drill_mm", 0)), *(c.GetViaDrill() for c in limits)),
        )

    def router(self, net: str) -> GridRouter:
        """A grid router for the net.

        It is bounded by the board outline, inset by the copper-to-edge clearance, and
        blocked by the obstacles.
        """
        width, clearance, via_size, via_drill = self.rules(net)
        # The router keeps a clearance from its bounds; the board needs its copper-to-edge rule.
        inset = max(0, self.edge - clearance)
        x0, y0, x1, y1 = (
            value + sign * inset for value, sign in zip(self.outline, (1, 1, -1, -1))
        )
        lower = Point(x0, y0)
        upper = Point(
            x0 + (x1 - x0) // self.step * self.step,
            y0 + (y1 - y0) // self.step * self.step,
        )
        # Tracks and vias can be rerouted, so by default only pads, keepouts and kept zones stand in the way.
        copper = self.obstacles.copper.values() if self.saved_copper else ()
        shapes = (
            *(shape for shapes in self.obstacles.pads.values() for shape in shapes),
            *(shape for item in copper for shape in item.shapes),
            *self.obstacles.fixed,
        )
        return GridRouter(
            lower,
            upper,
            self.step,
            self.layers,
            net,
            width,
            max(0, clearance - self.step),
            via_size,
            via_drill,
            shapes,
            max_cells=self.MAX_CELLS,
        )

    def terminal(self, router: GridRouter, pad) -> Terminal:
        """The free grid states in or beside a pad's box on the layers it reaches.

        A track may leave its own pad anywhere, so the ring of cells around
        the box counts: a fine-pitch pad whose inside lies within its
        neighbours' clearance still has a way out past its tip.
        """
        drilled = pad.GetDrillSize().x or pad.GetDrillSize().y
        layers = [
            index
            for index, name in enumerate(router.layers)
            if drilled or pad.IsOnLayer(self.board.GetLayerID(name))
        ]
        box, area, step, states = (
            pad.GetBoundingBox(),
            router.nx * router.ny,
            router.step,
            [],
        )
        for x in range(
            max(0, (box.GetX() - router.lower.x) // step),
            min(router.nx - 1, -(-(box.GetRight() - router.lower.x) // step)) + 1,
        ):
            for y in range(
                max(0, (box.GetY() - router.lower.y) // step),
                min(router.ny - 1, -(-(box.GetBottom() - router.lower.y) // step)) + 1,
            ):
                states.extend(
                    index * area + y * router.nx + x
                    for index in layers
                    if (x, y) not in router.blocked[router.layers[index]]
                )
        return Terminal(
            f"{pad.GetParentFootprint().GetReference()}.{pad.GetNumber()}",
            tuple(states),
        )

    def cut_off(self, net: str) -> bool:
        """Whether the net's pads fall into more than one group.

        A net without a net class cannot be judged and counts as connected.
        """
        try:
            return len(self.groups(net)) > 1
        except ValueError:
            return False

    def groups(self, net: str) -> list[tuple[str, ...]]:
        """The net's pads, grouped by whether any path joins them."""
        router = self.router(net)
        area = router.nx * router.ny
        remaining, result = [self.terminal(router, pad) for pad in self.pads(net)], []
        while remaining:
            first = remaining[0]
            joined = [first]
            if first.states:
                seeds = [
                    (s % area % router.nx, s % area // router.nx, s // area)
                    for s in first.states
                ]
                distance = minimum_vias(router, seeds[0], tuple(seeds[1:]))
                joined = [
                    t
                    for t in remaining
                    if any(distance[s] != UNREACHABLE for s in t.states)
                ]
            result.append(tuple(sorted(t.label for t in joined)))
            remaining = [t for t in remaining if t not in joined]
        return result


class Routability:
    """Which nets no route can complete while every part stays where it is.

    For each such net, ``blockers`` names the parts whose pads alone stand in
    the way: leaving one of them out lets every pad of the net connect.
    """

    def __init__(self, board, *, policy=None, critical_nets=(), step=mm(0.05)):
        """Remember the board and the routing policy.

        Inputs:
            board: Loaded KiCad board.
            policy: Routing policy, or None.
            critical_nets: Nets whose reference corridors count as obstacles.
            step: Smallest grid pitch in nanometres.
        """
        self.board, self.policy, self.critical_nets, self.step = (
            board,
            policy,
            critical_nets,
            step,
        )

    def model(self, footprints=None) -> GridModel:
        """A grid model of the board, with the given footprints posed or left out."""
        return GridModel(
            self.board,
            footprints=footprints,
            policy=self.policy,
            critical_nets=self.critical_nets,
            step=self.step,
        )

    def nets(self) -> list[str]:
        """Sorted names of the nets that have more than one pad."""
        counts = {}
        for pad in self.board.GetPads():
            name = str(pad.GetNetname())
            if name and not name.startswith("unconnected-"):
                counts[name] = counts.get(name, 0) + 1
        return sorted(name for name, count in counts.items() if count > 1)

    def blockers(self, net: str) -> list[str]:
        """Sorted references of the footprints that block a net.

        A footprint blocks the net when it has no pad on it and leaving it out lets
        every pad of the net connect.
        """
        result = []
        for footprint in self.board.GetFootprints():
            if any(pad.GetNetname() == net for pad in footprint.Pads()):
                continue
            if len(self.model({footprint.m_Uuid.AsString(): None}).groups(net)) == 1:
                result.append(str(footprint.GetReference()))
        return sorted(result)

    def report(self) -> dict:
        """Judge every net with more than one pad.

        Outputs:
            A dict with the grid step in millimetres, the sorted unroutable nets, and
            for each net its groups of mutually reachable pads and, when it has more
            than one group, its blockers.
        """
        model, nets = self.model(), {}
        for name in self.nets():
            groups = model.groups(name)
            nets[name] = {"groups": [list(group) for group in groups]}
            if len(groups) > 1:
                nets[name]["blockers"] = self.blockers(name)
        return {
            "step_mm": model.step / 1e6,
            "unroutable": sorted(
                name for name, item in nets.items() if len(item["groups"]) > 1
            ),
            "nets": nets,
        }
