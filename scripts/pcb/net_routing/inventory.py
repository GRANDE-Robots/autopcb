"""inventory.py: inventory all native board nets without inferring intentional no-connects.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from dataclasses import dataclass

import pcbnew


@dataclass(frozen=True)
class NetInventory:
    """The copper of one net, by KiCad UUID.

    Inputs:
        name: Net name.
        pad_ids: Pads on the net.
        pad_islands: Pads grouped by connected copper island.
        copper_islands: All items grouped by connected copper island.
        track_ids, via_ids, zone_ids: Tracks, vias and zones on the net.
    """

    name: str
    pad_ids: tuple[str, ...]
    pad_islands: tuple[tuple[str, ...], ...]
    copper_islands: tuple[tuple[str, ...], ...]
    track_ids: tuple[str, ...]
    via_ids: tuple[str, ...]
    zone_ids: tuple[str, ...]


@dataclass(frozen=True)
class BoardInventory:
    """Every net of a board plus copper that lacks a net or a verified no-connect.

    Inputs:
        nets: One inventory per net.
        unassigned_pad_ids: Copper pads without a net.
        unverified_no_connect_pad_ids: Pads on unconnected-* nets that the
            schematic does not mark no-connect.
        verified_no_connect_pad_ids: Pads the schematic marks no-connect.
        unassigned_copper_ids: Tracks and vias without a net.
    """

    nets: tuple[NetInventory, ...]
    unassigned_pad_ids: tuple[str, ...]
    unverified_no_connect_pad_ids: tuple[str, ...]
    verified_no_connect_pad_ids: tuple[str, ...]
    unassigned_copper_ids: tuple[str, ...]


class InventoryReader:
    """Reads a board's nets, copper islands and no-connect pads."""

    @staticmethod
    def schematic_no_connects(source: str) -> set[tuple[str, str]]:
        """Accept only one-pin markers without a wire or label at the same point."""
        from sch.repair_no_connects import pin_endpoints
        from sch.standardize_block_symbols import point_key, top_level, top_level_points

        endpoints = pin_endpoints(source)
        occupied = set()
        for kind in ("label", "global_label", "hierarchical_label", "junction"):
            for _, _, block in top_level(source, kind):
                occupied.update(
                    point_key(*point) for point in top_level_points(block, kind)
                )
        wires = []
        for _, _, block in top_level(source, "wire"):
            points = [point_key(*point) for point in top_level_points(block, "wire")]
            wires.extend(zip(points, points[1:]))

        def on_wire(point, start, end) -> bool:
            # Schematic points are rounded to KiCad's coordinate precision.
            """Say whether a point lies on the wire from start to end, with all coordinates rounded to 1 nm."""
            px, py = (round(value * 1_000_000) for value in point)
            ax, ay = (round(value * 1_000_000) for value in start)
            bx, by = (round(value * 1_000_000) for value in end)
            return (
                (px - ax) * (by - ay) == (py - ay) * (bx - ax)
                and min(ax, bx) <= px <= max(ax, bx)
                and min(ay, by) <= py <= max(ay, by)
            )

        result = set()
        for _, _, block in top_level(source, "no_connect"):
            points = top_level_points(block, "no_connect")
            if len(points) != 1:
                raise ValueError("A schematic no-connect marker needs one coordinate")
            point = point_key(*points[0])
            pins = set(endpoints.get(point, ()))
            if (
                len(pins) == 1
                and point not in occupied
                and not any(on_wire(point, start, end) for start, end in wires)
            ):
                result.update(pins)
        return result

    def __call__(self, board, *, schematic: str | None = None) -> BoardInventory:
        """Read a board after any desired zone refill; never mutate native objects.

        KiCad's unconnected-* name is a hint, not evidence that the schematic
        intentionally leaves a pin open. Blank electrical pads remain visible too.
        """
        pads_by_net: dict[str, dict[str, object]] = {}
        tracks_by_net: dict[str, list[str]] = {}
        vias_by_net: dict[str, list[str]] = {}
        zones_by_net: dict[str, list[str]] = {}
        items_by_net: dict[str, dict[str, object]] = {}
        unassigned: list[str] = []
        unassigned_copper: list[str] = []
        no_connect: list[str] = []
        verified_no_connect: list[str] = []
        marked = (
            InventoryReader.schematic_no_connects(schematic)
            if schematic is not None
            else set()
        )
        seen: set[str] = set()

        def identifier(item) -> str:
            """Return the item's UUID; raise ValueError when it is missing or already seen."""
            uid = item.m_Uuid.AsString()
            if not uid or uid in seen:
                raise ValueError("Duplicate or missing copper UUID prevents inventory")
            seen.add(uid)
            return uid

        for pad in board.GetPads():
            uid = identifier(pad)
            name = pad.GetNetname()
            copper = any(
                pad.IsOnLayer(layer) for layer in board.GetEnabledLayers().CuStack()
            )
            if not name:
                if copper and pad.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH:
                    key = (pad.GetParentFootprint().GetReference(), pad.GetNumber())
                    (verified_no_connect if key in marked else unassigned).append(uid)
                continue
            pads_by_net.setdefault(name, {})[uid] = pad
            items_by_net.setdefault(name, {})[uid] = pad
            if name.startswith("unconnected-"):
                key = (pad.GetParentFootprint().GetReference(), pad.GetNumber())
                (verified_no_connect if key in marked else no_connect).append(uid)

        for item in board.GetTracks():
            uid = identifier(item)
            name = item.GetNetname()
            if not name:
                unassigned_copper.append(uid)
                continue
            items_by_net.setdefault(name, {})[uid] = item
            target = vias_by_net if isinstance(item, pcbnew.PCB_VIA) else tracks_by_net
            target.setdefault(name, []).append(uid)

        for zone in board.Zones():
            uid = identifier(zone)
            name = zone.GetNetname()
            if name and not zone.GetIsRuleArea():
                zones_by_net.setdefault(name, []).append(uid)

        connectivity = board.GetConnectivity()
        connectivity.Build(board)
        nets = []
        for name in sorted(
            pads_by_net.keys()
            | tracks_by_net.keys()
            | vias_by_net.keys()
            | zones_by_net.keys()
        ):
            pads = pads_by_net.get(name, {})
            items = items_by_net.get(name, {})
            remaining = set(items)
            copper_islands = []
            while remaining:
                first = min(remaining)
                connected = {
                    item.m_Uuid.AsString()
                    for item in connectivity.GetConnectedItems(items[first])
                    if item.GetNetname() == name
                }
                members = tuple(sorted((connected | {first}) & remaining))
                if not members:
                    raise RuntimeError(f"Failed to inventory a copper island on {name}")
                remaining.difference_update(members)
                copper_islands.append(members)
            pad_islands = tuple(
                members
                for island in copper_islands
                if (members := tuple(uid for uid in island if uid in pads))
            )
            nets.append(
                NetInventory(
                    name=name,
                    pad_ids=tuple(sorted(pads)),
                    pad_islands=pad_islands,
                    copper_islands=tuple(copper_islands),
                    track_ids=tuple(sorted(tracks_by_net.get(name, ()))),
                    via_ids=tuple(sorted(vias_by_net.get(name, ()))),
                    zone_ids=tuple(sorted(zones_by_net.get(name, ()))),
                )
            )
        return BoardInventory(
            tuple(nets),
            tuple(sorted(unassigned)),
            tuple(sorted(no_connect)),
            tuple(sorted(verified_no_connect)),
            tuple(sorted(unassigned_copper)),
        )


inventory_board = InventoryReader()
