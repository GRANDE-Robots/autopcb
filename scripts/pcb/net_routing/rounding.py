"""rounding.py: find and independently verify bounded, connected track-rounding repairs.

Coordinates are integer nanometres.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from itertools import product

import pcbnew

from core.kicad_sexpr import Form, parse
from pcb.net_routing.controller import new_style_issues
from pcb.net_routing.style import audit_style, normalize_segment_identities
from pcb.route_validation import _canonical, _segment_identities
from core.compat import strict_zip


MAX_JOINT_SHIFT_NM = 50_000


class TrackGeometry:
    """Integer-nanometre geometry of straight tracks and their joints."""

    @staticmethod
    def point(value) -> tuple[int, int]:
        """Return a KiCad vector as an (x, y) pair of integers in nanometres."""
        return int(value.x), int(value.y)

    @staticmethod
    def octilinear(start: tuple[int, int], end: tuple[int, int]) -> bool:
        """Say whether a segment has length and is horizontal, vertical or at 45 degrees."""
        dx, dy = abs(end[0] - start[0]), abs(end[1] - start[1])
        return bool(dx or dy) and (not dx or not dy or dx == dy)

    @staticmethod
    def joint_tracks(board, net: str, layer: int, point: tuple[int, int]) -> set[str]:
        """Reject a point that also touches a pad, via, branch or crossing track."""
        at = pcbnew.VECTOR2I(*point)
        if any(
            pad.GetNetname() == net and pad.IsOnLayer(layer) and pad.HitTest(at)
            for pad in board.GetPads()
        ):
            return set()
        if any(
            isinstance(item, (pcbnew.PCB_VIA, pcbnew.PCB_ARC))
            and item.GetNetname() == net
            and item.IsOnLayer(layer)
            and item.HitTest(at)
            for item in board.GetTracks()
        ):
            return set()
        touching = {
            uid
            for uid, track in _straight_tracks(board).items()
            if track.GetNetname() == net
            and track.GetLayer() == layer
            and track.HitTest(at)
        }
        endpoints = {
            uid
            for uid, track in _straight_tracks(board).items()
            if track.GetNetname() == net
            and track.GetLayer() == layer
            and point
            in {
                TrackGeometry.point(track.GetStart()),
                TrackGeometry.point(track.GetEnd()),
            }
        }
        return endpoints if len(endpoints) == 2 and endpoints == touching else set()

    @staticmethod
    def joint_targets(
        point: tuple[int, int], originals: dict
    ) -> tuple[tuple[int, int], ...]:
        """Intersect octilinear directions from fixed endpoints in integer nanometres."""
        fixed = [end if start == point else start for start, end in originals.values()]
        if len(fixed) != 2:
            return ()
        targets = {
            (point[0] + x, point[1] + y)
            for x, y in product((-1, 0, 1), repeat=2)
            if x or y
        }
        lines = ((0, 1), (1, 0), (1, -1), (1, 1))
        for (a, b), (c, d) in product(lines, repeat=2):
            det = a * d - c * b
            if not det:
                continue
            first = a * fixed[0][0] + b * fixed[0][1]
            second = c * fixed[1][0] + d * fixed[1][1]
            nx, ny = first * d - second * b, a * second - c * first
            if nx % det == 0 and ny % det == 0:
                targets.add((nx // det, ny // det))
        targets.discard(point)

        def distance_squared(target: tuple[int, int]) -> int:
            """Return the squared distance from the joint to a target, in nm squared."""
            return (target[0] - point[0]) ** 2 + (target[1] - point[1]) ** 2

        return tuple(
            sorted(
                (
                    target
                    for target in targets
                    if distance_squared(target) <= MAX_JOINT_SHIFT_NM**2
                ),
                key=lambda target: (distance_squared(target), target),
            )
        )


class StraightTracks:
    """Selects the straight tracks of a board."""

    def __call__(self, board) -> dict[str, object]:
        """Return the board's tracks that are neither vias nor arcs, keyed by UUID."""
        return {
            item.m_Uuid.AsString(): item
            for item in board.GetTracks()
            if not isinstance(item, (pcbnew.PCB_VIA, pcbnew.PCB_ARC))
        }


_straight_tracks = StraightTracks()


class TrackPoints:
    """Reads the endpoints of a track."""

    def __call__(self, track) -> tuple[tuple[int, int], tuple[int, int]]:
        """Return a track's start and end as integer nanometre points."""
        return TrackGeometry.point(track.GetStart()), TrackGeometry.point(
            track.GetEnd()
        )


_track_points = TrackPoints()


class RoundingCandidate:
    """Searches for one joint repair that makes an off-angle track octilinear."""

    def __call__(self, board, eligible: set[str]) -> dict | None:
        """Find a bounded octilinear joint repair without changing the input board."""
        if not isinstance(eligible, set) or not eligible:
            return None
        original_style = audit_style(board)
        tracks = _straight_tracks(board)
        for uid in sorted(original_style.non_octilinear):
            track = tracks[uid]
            net, layer = str(track.GetNetname()), track.GetLayer()
            if net not in eligible:
                continue
            a, b = _track_points(track)
            for point in (a, b):
                ids = TrackGeometry.joint_tracks(board, net, layer, point)
                if uid not in ids:
                    continue
                items = [tracks[name] for name in sorted(ids)]
                if any(
                    item.IsLocked() or item.GetParentGroup() is not None
                    for item in items
                ):
                    continue
                originals = {
                    item.m_Uuid.AsString(): _track_points(item) for item in items
                }
                old_bad = sum(
                    not TrackGeometry.octilinear(*points)
                    for points in originals.values()
                )
                for target in TrackGeometry.joint_targets(point, originals):
                    # A moved joint may not acquire or lose an existing anchor.
                    new_ids = TrackGeometry.joint_tracks(board, net, layer, target)
                    if new_ids and new_ids != ids:
                        continue
                    at = pcbnew.VECTOR2I(*target)
                    if any(
                        p.GetNetname() == net and p.IsOnLayer(layer) and p.HitTest(at)
                        for p in board.GetPads()
                    ):
                        continue
                    if any(
                        isinstance(v, (pcbnew.PCB_VIA, pcbnew.PCB_ARC))
                        and v.GetNetname() == net
                        and v.IsOnLayer(layer)
                        and v.HitTest(at)
                        for v in board.GetTracks()
                    ):
                        continue
                    if any(
                        name not in ids
                        and other.GetNetname() == net
                        and other.GetLayer() == layer
                        and other.HitTest(at)
                        for name, other in tracks.items()
                    ):
                        continue
                    revised = {
                        name: (
                            target if start == point else start,
                            target if end == point else end,
                        )
                        for name, (start, end) in originals.items()
                    }
                    if any(
                        TrackGeometry.octilinear(*originals[name])
                        and not TrackGeometry.octilinear(*points)
                        for name, points in revised.items()
                    ):
                        continue
                    if (
                        sum(
                            not TrackGeometry.octilinear(*points)
                            for points in revised.values()
                        )
                        >= old_bad
                    ):
                        continue
                    try:
                        for item in items:
                            start, end = revised[item.m_Uuid.AsString()]
                            item.SetStart(pcbnew.VECTOR2I(*start))
                            item.SetEnd(pcbnew.VECTOR2I(*end))
                        after = audit_style(board)
                        if new_style_issues(original_style, after):
                            continue
                    finally:
                        for item in items:
                            start, end = originals[item.m_Uuid.AsString()]
                            item.SetStart(pcbnew.VECTOR2I(*start))
                            item.SetEnd(pcbnew.VECTOR2I(*end))
                    changes = []
                    for name in sorted(ids):
                        for field, old, new in strict_zip(
                            ("start", "end"), originals[name], revised[name]
                        ):
                            if old == new:
                                continue
                            x, y = (
                                format(Decimal(value) / 1_000_000, "f") for value in new
                            )
                            changes.append(
                                {
                                    "document": "pcb",
                                    "target": {"kind": "segment", "uuid": name},
                                    "op": "replace",
                                    "path": [field],
                                    "expression": f"({field} {x} {y})",
                                }
                            )
                    return {
                        "net": net,
                        "changes": changes,
                        "corrected": old_bad
                        - sum(
                            not TrackGeometry.octilinear(*points)
                            for points in revised.values()
                        ),
                    }
        return None


find_candidate = RoundingCandidate()


class BoardForms:
    """Splits a native PCB into its track forms and everything else."""

    def __call__(self, data: bytes):
        """Parse a native PCB.

        Outputs:
            A map of track UUID to its canonical form without endpoints, and a counter of
            every other canonical board item; zone fills are ignored. Raises ValueError
            when the data is not a PCB, a track lacks a UUID or a UUID repeats.
        """
        root = parse(data.decode("utf-8-sig"))
        if root.name != "kicad_pcb":
            raise ValueError("Expected a native KiCad PCB")
        segments = {}
        other = Counter()
        for item in root.items:
            if isinstance(item, Form) and item.name == "segment":
                ids = item.children("uuid")
                if len(ids) != 1 or len(ids[0].atoms()) != 2:
                    raise ValueError("Every track needs one UUID")
                uid = ids[0].atoms()[1].value
                if uid in segments:
                    raise ValueError("Duplicate track UUID")
                segments[uid] = tuple(
                    _canonical(x)
                    for x in item.items
                    if not isinstance(x, Form) or x.name not in {"start", "end"}
                )
            elif isinstance(item, Form) and item.name == "zone":
                # Zone refill is independently checked; its rules and outline must match.
                other[
                    tuple(
                        _canonical(x)
                        for x in item.items
                        if not isinstance(x, Form)
                        or x.name not in {"filled_polygon", "fill_segments"}
                    )
                ] += 1
            else:
                # KiCad may reorder top-level records during refill. Compare the
                # full contents and multiplicity of each unchanged board item.
                other[_canonical(item)] += 1
        return segments, other


_forms = BoardForms()


class RoundingVerification:
    """Re-proves a rounding change from native board snapshots."""

    def __call__(
        self, before: bytes, after: bytes, old_board, new_board, eligible: set[str]
    ) -> dict:
        """Recompute the bounded endpoint-only proof from native board snapshots."""
        old_forms, old_other = _forms(before)
        new_forms, new_other = _forms(after)
        if old_forms != new_forms or old_other != new_other:
            raise ValueError("Rounding changed a track attribute or another board item")
        old_tracks, new_tracks = (
            _straight_tracks(old_board),
            _straight_tracks(new_board),
        )
        if set(old_tracks) != set(new_tracks) or set(old_tracks) != set(old_forms):
            raise ValueError("Rounding changed the track inventory")
        moved = defaultdict(set)
        changed = set()
        for uid, old in old_tracks.items():
            new = new_tracks[uid]
            a, b = _track_points(old), _track_points(new)
            if a == b:
                continue
            net, layer = str(old.GetNetname()), old.GetLayer()
            if (
                net not in eligible
                or old.IsLocked()
                or old.GetParentGroup() is not None
                or str(new.GetNetname()) != net
                or new.GetLayer() != layer
            ):
                raise ValueError("Rounding touched protected or unsupported copper")
            if TrackGeometry.octilinear(*a) and not TrackGeometry.octilinear(*b):
                raise ValueError("Rounding introduced an off-angle track")
            for first, second in strict_zip(a, b):
                if first == second:
                    continue
                if (first[0] - second[0]) ** 2 + (
                    first[1] - second[1]
                ) ** 2 > MAX_JOINT_SHIFT_NM**2:
                    raise ValueError("Joint alignment exceeded 0.05 mm")
                moved[(net, layer, first, second)].add(uid)
            changed.add(uid)
        if not changed:
            raise ValueError("Rounding did not change any track")
        for (net, layer, old_point, new_point), ids in moved.items():
            if (
                TrackGeometry.joint_tracks(old_board, net, layer, old_point) != ids
                or TrackGeometry.joint_tracks(new_board, net, layer, new_point) != ids
            ):
                raise ValueError("Rounding moved an anchored, branched or split joint")
        old_style, new_style = audit_style(old_board), audit_style(new_board)
        old_norm = normalize_segment_identities(old_style, _segment_identities(before))
        new_norm = normalize_segment_identities(new_style, _segment_identities(after))
        if new_style_issues(old_norm, new_norm):
            raise ValueError("Rounding introduced another routing-style issue")
        corrected = len(old_style.non_octilinear) - len(new_style.non_octilinear)
        if corrected <= 0:
            raise ValueError("Rounding did not reduce off-angle tracks")
        return {
            "corrected": corrected,
            "changed_tracks": len(changed),
            "moved_joints": len(moved),
            "nets": sorted({key[0] for key in moved}),
        }


verify_candidate = RoundingVerification()
