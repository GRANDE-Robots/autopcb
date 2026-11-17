"""Identify electrically isolated, tiny track fragments without altering a board.

This screen is deliberately narrow: a selected item is a single, unlocked
straight track, at most 0.05 mm and one tenth of its width, with no connected
copper or pad, and no zone of its net. Native checks and protected-item checks still apply.
"""

from __future__ import annotations

from collections import Counter

import pcbnew

from core.pcb_io import copper_width
from pcb.freeroute_delta import Trace

MAX_FRAGMENT_NM = 50_000


def same_net_bounding_overlap(item, others) -> bool:
    """Conservatively reject nearby same-net copper even if connectivity misses it."""
    bounds = item.GetBoundingBox()
    uid, net, layer = item.m_Uuid.AsString(), str(item.GetNetname()), item.GetLayer()
    for other in others:
        if (other.m_Uuid.AsString() == uid or str(other.GetNetname()) != net
                or not other.IsOnLayer(layer)):
            continue
        box = other.GetBoundingBox()
        if not (box.GetRight() < bounds.GetX() or box.GetX() > bounds.GetRight()
                or box.GetBottom() < bounds.GetY() or box.GetY() > bounds.GetBottom()):
            return True
    return False


def isolated_fragments(board, excluded: set[str]) -> dict[str, Trace]:
    """Return exact records for individually isolated copper fragments."""
    if not isinstance(excluded, set) or any(not isinstance(name, str) for name in excluded):
        raise ValueError("Excluded nets must be a set of names")
    zones = {str(zone.GetNetname()) for zone in board.Zones()
             if not zone.GetIsRuleArea() and zone.GetNetname()}
    grouped = {item.m_Uuid.AsString() for group in board.Groups() for item in group.GetItems()}
    connectivity = board.GetConnectivity()
    connectivity.Build(board)
    tracks, pads = tuple(board.GetTracks()), tuple(board.GetPads())
    neighbors_by_net = {}
    for other in (*tracks, *pads):
        neighbors_by_net.setdefault(str(other.GetNetname()), []).append(other)
    result = {}
    for item in tracks:
        if (not isinstance(item, pcbnew.PCB_TRACK)
                or isinstance(item, (pcbnew.PCB_VIA, pcbnew.PCB_ARC))):
            continue
        uid, net = item.m_Uuid.AsString(), str(item.GetNetname())
        maximum_length = min(MAX_FRAGMENT_NM, int(copper_width(item)) // 10)
        if (not net or net == "GND" or net in excluded or net in zones or uid in grouped
                or item.IsLocked() or item.GetLength() > maximum_length):
            continue
        connected = {other.m_Uuid.AsString() for other in connectivity.GetConnectedItems(item)}
        if (connected - {uid} or connectivity.GetConnectedPads(item)
                or same_net_bounding_overlap(item, neighbors_by_net.get(net, ()))):
            continue
        start, end = item.GetStart(), item.GetEnd()
        result[uid] = Trace(net, board.GetLayerName(item.GetLayer()), int(copper_width(item)),
                            (int(start.x), int(start.y)), (int(end.x), int(end.y)))
    return result


def isolated_dangling_fragments(board, report: dict, excluded: set[str], *,
                                require_error: bool = False) -> tuple[str, ...]:
    """Select isolated dangling fragments, optionally requiring a native error."""
    if not isinstance(require_error, bool):
        raise ValueError("The error-only setting must be boolean")
    if not isinstance(report, dict) or not isinstance(report.get("violations"), list):
        raise ValueError("A native DRC report is required")
    errors, dangling = set(), set()
    for finding in report["violations"]:
        if not isinstance(finding, dict) or not isinstance(finding.get("items"), list):
            raise ValueError("Invalid native DRC finding")
        ids = {item.get("uuid") for item in finding["items"]
               if isinstance(item, dict) and isinstance(item.get("uuid"), str)}
        if finding.get("severity") == "error":
            errors.update(ids)
        if finding.get("severity") == "warning" and finding.get("type") == "track_dangling":
            dangling.update(ids)
    selected = set(isolated_fragments(board, excluded)) & dangling
    return tuple(sorted(selected & errors if require_error else selected))


def verify_isolated_protected_removals(board, removed: Counter, added: Counter,
                                       protected: set[str], excluded: set[str]) -> None:
    """Allow only proven disconnected fragments among protected-net changes."""
    protected_removed = Counter({item: count for item, count in removed.items() if item.net in protected})
    protected_added = Counter({item: count for item, count in added.items() if item.net in protected})
    if protected_added:
        raise RuntimeError("Protected-net cleanup cannot add or modify copper")
    available = Counter(item for item in isolated_fragments(board, excluded).values()
                        if item.net in protected)
    if protected_removed - available:
        raise RuntimeError("Protected-net cleanup is not limited to isolated track fragments")
