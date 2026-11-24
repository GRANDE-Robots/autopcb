"""Propose and independently verify bounded staircase simplifications."""

from __future__ import annotations

import math
from collections import Counter, defaultdict

from core.kicad_sexpr import parse
from core.pcb_io import copper_width
from pcb.freeroute_delta import (
    Delta,
    Trace,
    changes,
    native_anchor_checker,
    simplify_terminal_chains,
)
from pcb.net_routing.controller import new_style_issues
from pcb.net_routing.rounding import _forms, _straight_tracks, _track_points
from pcb.net_routing.style import audit_style, normalize_segment_identities
from pcb.route_junctions import _contacts
from pcb.route_optimizer import Point, Segment
from pcb.route_validation import _segment_identities


def _trace(board, item) -> Trace:
    start, end = item.GetStart(), item.GetEnd()
    return Trace(
        str(item.GetNetname()),
        board.GetLayerName(item.GetLayer()),
        int(copper_width(item)),
        (int(start.x), int(start.y)),
        (int(end.x), int(end.y)),
    )


def find_candidates(
    board, copper: list, eligible: set[str], shortcut_allowed, *, limit: int = 12
) -> tuple[dict, ...]:
    """Find local eligible shortcuts; a candidate is never a validated route."""
    from pcb.net_routing.via_prune import copper_removal_changes

    if (
        not isinstance(eligible, set)
        or not callable(shortcut_allowed)
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
    ):
        raise ValueError(
            "Eligible nets, a clearance checker and a positive limit are required"
        )
    tracks = _straight_tracks(board)
    counts = Counter(copper)
    anchor = native_anchor_checker(board, ())
    result, seen = [], set()
    for issue in audit_style(board).staircases:
        identifiers = frozenset(issue.item_ids)
        if (
            issue.net not in eligible
            or not identifiers
            or identifiers in seen
            or not identifiers <= tracks.keys()
        ):
            continue
        seen.add(identifiers)
        selected = [tracks[uid] for uid in sorted(identifiers)]
        if any(
            item.IsLocked() or item.GetParentGroup() is not None for item in selected
        ):
            continue
        old = tuple(_trace(board, item) for item in selected)
        if any(counts[item] != 1 for item in old):
            continue
        remaining = list(copper)
        try:
            for item in old:
                remaining.remove(item)
            new = simplify_terminal_chains(
                old, remaining, anchor, shortcut_allowed=shortcut_allowed
            )
        except ValueError:
            continue
        if Counter(old) == Counter(new):
            continue
        proposal = copper_removal_changes(board, set(identifiers)) + changes(
            Delta(new, (), (), 0)
        )
        result.append(
            {
                "net": issue.net,
                "changes": proposal,
                "removed_tracks": len(old),
                "replacement_tracks": len(new),
            }
        )
        if len(result) >= limit:
            break
    return tuple(result)


def _chain(traces: tuple[Trace, ...]) -> frozenset[tuple[int, int]]:
    """Require one simple octilinear chain and return its exact endpoints."""
    if not traces or len(set(traces)) != len(traces):
        raise ValueError("A shortcut must be a nonempty, unique chain")
    adjacency = defaultdict(set)
    for item in traces:
        dx, dy = abs(item.end[0] - item.start[0]), abs(item.end[1] - item.start[1])
        if not (dx or dy) or (dx and dy and dx != dy):
            raise ValueError("A shortcut contains off-angle copper")
        adjacency[item.start].add(item.end)
        adjacency[item.end].add(item.start)
    ends = frozenset(
        point for point, neighbors in adjacency.items() if len(neighbors) == 1
    )
    if len(ends) != 2 or any(len(neighbors) > 2 for neighbors in adjacency.values()):
        raise ValueError("A shortcut must not branch or loop")
    visited, pending = set(), [next(iter(ends))]
    while pending:
        point = pending.pop()
        if point in visited:
            continue
        visited.add(point)
        pending.extend(adjacency[point] - visited)
    if visited != set(adjacency):
        raise ValueError("A shortcut must remain connected")
    for index, first in enumerate(traces):
        for second in traces[index + 1 :]:
            a = Segment(Point(*first.start), Point(*first.end), first.layer)
            b = Segment(Point(*second.start), Point(*second.end), second.layer)
            common = {
                Point(*point)
                for point in (
                    set((first.start, first.end)) & set((second.start, second.end))
                )
            }
            if _contacts(a, b) != common:
                raise ValueError("A shortcut cannot overlap or cross itself")
    return ends


def _plain_segments(data: bytes, identifiers: set[str]) -> bool:
    required = {"start", "end", "width", "layer", "net", "uuid"}
    found = set()
    for item in parse(data.decode("utf-8-sig")).children("segment"):
        ids = item.children("uuid")
        if len(ids) != 1 or len(ids[0].atoms()) != 2:
            return False
        uid = ids[0].atoms()[1].value
        if uid not in identifiers:
            continue
        names = [child.name for child in item.children()]
        if (
            len(item.atoms()) != 1
            or set(names) != required
            or len(names) != len(required)
        ):
            return False
        found.add(uid)
    return found == identifiers


def verify_candidate(
    before: bytes, after: bytes, old_board, new_board, eligible: set[str]
) -> dict:
    """Recompute an endpoint-preserving style improvement from native boards."""
    if not isinstance(eligible, set):
        raise ValueError("Eligible nets must be provided")
    old_forms, old_other = _forms(before)
    new_forms, new_other = _forms(after)
    if old_other != new_other:
        raise ValueError("Staircase repair changed a non-track board item")
    old_tracks, new_tracks = _straight_tracks(old_board), _straight_tracks(new_board)
    if set(old_tracks) != set(old_forms) or set(new_tracks) != set(new_forms):
        raise ValueError("Staircase repair has an inconsistent track inventory")
    shared = old_tracks.keys() & new_tracks.keys()
    if any(
        old_forms[uid] != new_forms[uid]
        or _track_points(old_tracks[uid]) != _track_points(new_tracks[uid])
        for uid in shared
    ):
        raise ValueError("Staircase repair changed a surviving track")
    removed_ids, added_ids = (
        old_tracks.keys() - new_tracks.keys(),
        new_tracks.keys() - old_tracks.keys(),
    )
    if not 2 <= len(removed_ids) <= 64 or not 0 < len(added_ids) < len(removed_ids):
        raise ValueError("Staircase repair must replace a shorter local chain")
    if not _plain_segments(before, removed_ids) or not _plain_segments(
        after, added_ids
    ):
        raise ValueError("Staircase repair touched nonstandard track attributes")
    removed = [old_tracks[uid] for uid in removed_ids]
    added = [new_tracks[uid] for uid in added_ids]
    if any(
        item.IsLocked() or item.GetParentGroup() is not None
        for item in (*removed, *added)
    ):
        raise ValueError("Staircase repair touched locked or grouped copper")
    old = tuple(_trace(old_board, item) for item in removed)
    new = tuple(_trace(new_board, item) for item in added)
    old_groups, new_groups = defaultdict(list), defaultdict(list)
    for item in old:
        old_groups[(item.net, item.layer, item.width)].append(item)
    for item in new:
        new_groups[(item.net, item.layer, item.width)].append(item)
    if set(old_groups) != set(new_groups) or any(
        key[0] not in eligible for key in old_groups
    ):
        raise ValueError("Staircase repair changed ineligible nets or dimensions")

    def length(items):
        return sum(math.dist(item.start, item.end) for item in items)

    for key, old_chain in old_groups.items():
        new_chain = new_groups[key]
        if len(new_chain) >= len(old_chain) or _chain(tuple(old_chain)) != _chain(
            tuple(new_chain)
        ):
            raise ValueError(
                "Staircase repair moved a chain endpoint or failed to simplify it"
            )
        if length(new_chain) > length(old_chain) + 1e-6:
            raise ValueError("Staircase repair increased track length")
    old_style, new_style = audit_style(old_board), audit_style(new_board)
    old_norm = normalize_segment_identities(old_style, _segment_identities(before))
    new_norm = normalize_segment_identities(new_style, _segment_identities(after))
    if new_style_issues(old_norm, new_norm):
        raise ValueError("Staircase repair introduced a routing-style issue")
    corrected = len(old_style.staircases) - len(new_style.staircases)
    if corrected <= 0:
        raise ValueError("Staircase repair did not reduce staircases")
    nets = sorted({key[0] for key in old_groups})
    return {
        "corrected": corrected,
        "net": nets[0] if len(nets) == 1 else None,
        "nets": nets,
        "removed_tracks": len(old),
        "replacement_tracks": len(new),
    }
