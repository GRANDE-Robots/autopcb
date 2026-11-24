"""Propose conservative copper cleanup on private, source-bound previews."""

from __future__ import annotations

from core.project import native_path

import argparse
import json
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

from core.toolchain import kicad_cli

import pcbnew

from core.kicad_campaign import CampaignPlanner
from core.paths import asset_root, config_path, results_path
from core.pcb_io import copper_width, quiet_duplicate_image_handlers
from pcb.checkpoints import read_config
from pcb.freeroute_delta import router_class
from pcb.net_routing.controller import decode_checkpoint
from pcb.net_routing.external import retain_candidate
from pcb.net_routing.fast import record_unimproved_batch
from pcb.net_routing.isolation import run_native
from pcb.net_routing.native import Snapshot
from pcb.net_routing.prepare import parity_blocked_nets, summarize_drc
from pcb.net_routing.protection import ProtectedState, unqualified_working_nets
from pcb.net_routing.reference import high_speed_nets
from pcb.net_routing.rounding import find_candidate
from pcb.net_routing.runner import (
    _run_id,
    _source_hashes,
    _verify_resume_connectivity,
    verify_resume_reports,
)
from pcb.net_routing.style import audit_style, normalize_segment_identities
from pcb.route_validation import (
    _dangling_track_identities,
    _drc,
    _new_dangling_indices,
    _segment_identities,
)
from core.compat import strict_zip


def candidate_vias(
    board, eligible_nets: set[str], critical_nets: set[str]
) -> tuple[str, ...]:
    """Select through vias touching at most one track layer, with no pad or zone.

    This is only a candidate screen. It makes no claim that removing a via
    preserves connectivity, clearance, reference paths or electrical behavior.
    """
    zones = {
        str(zone.GetNetname())
        for zone in board.Zones()
        if not zone.GetIsRuleArea() and zone.GetNetname()
    }
    grouped = {
        item.m_Uuid.AsString() for group in board.Groups() for item in group.GetItems()
    }
    connectivity = board.GetConnectivity()
    connectivity.Build(board)
    result = []
    for item in board.GetTracks():
        if not isinstance(item, pcbnew.PCB_VIA):
            continue
        uid, net = item.m_Uuid.AsString(), str(item.GetNetname())
        if (
            not net
            or net == "GND"
            or net not in eligible_nets
            or net in critical_nets
            or net in zones
            or uid in grouped
            or item.IsLocked()
            or item.GetViaType() != pcbnew.VIATYPE_THROUGH
        ):
            continue
        if connectivity.GetConnectedPads(item):
            continue
        tracks = tuple(connectivity.GetConnectedTracks(item))
        if any(isinstance(track, (pcbnew.PCB_VIA, pcbnew.PCB_ARC)) for track in tracks):
            continue
        if len({int(track.GetLayer()) for track in tracks}) <= 1:
            result.append(uid)
    return tuple(sorted(result))


def dangling_spur_items(board, via_ids: set[str]) -> set[str]:
    """Extend candidate vias over unbranched dead ends, never across contacts.

    Remaining pads and tracks must all touch the same endpoint of a candidate
    track; an interior or two-ended contact stops the walk. The result still
    requires native DRC, connectivity and reference-copper validation.
    """
    tracks = {item.m_Uuid.AsString(): item for item in board.GetTracks()}
    if not via_ids <= tracks.keys() or any(
        not isinstance(tracks[uid], pcbnew.PCB_VIA) for uid in via_ids
    ):
        raise ValueError("Spur seeds must be board vias")
    nets = {str(tracks[uid].GetNetname()) for uid in via_ids}
    if not via_ids <= set(candidate_vias(board, nets, set())):
        raise ValueError("Spur seeds must be safe single-layer via candidates")
    grouped = {
        item.m_Uuid.AsString() for group in board.Groups() for item in group.GetItems()
    }
    connectivity = board.GetConnectivity()
    connectivity.Build(board)
    removed = set(via_ids)
    pending = [
        item for uid in via_ids for item in connectivity.GetConnectedTracks(tracks[uid])
    ]
    while pending:
        item = pending.pop()
        uid = item.m_Uuid.AsString()
        if (
            uid in removed
            or not isinstance(item, pcbnew.PCB_TRACK)
            or isinstance(item, (pcbnew.PCB_VIA, pcbnew.PCB_ARC))
            or item.IsLocked()
            or uid in grouped
            or str(item.GetNetname()) not in nets
        ):
            continue
        neighbors = [
            other
            for other in connectivity.GetConnectedTracks(item)
            if other.m_Uuid.AsString() not in removed
        ]
        attachments = [*neighbors, *connectivity.GetConnectedPads(item)]
        ends = item.GetStart(), item.GetEnd()
        attached = set()
        for other in attachments:
            hits = {index for index, point in enumerate(ends) if other.HitTest(point)}
            if not hits:
                attached = {0, 1}
                break
            attached.update(hits)
        if len(attached) > 1:
            continue
        removed.add(uid)
        pending.extend(neighbors)
    return removed


def candidate_dangling_tracks(
    board, report: dict, eligible_nets: set[str], critical_nets: set[str]
) -> tuple[str, ...]:
    """Select reported dangling signal tracks, not protected copper or zones."""
    if not isinstance(report, dict) or not isinstance(report.get("violations"), list):
        raise ValueError(
            "Native DRC violations are required for dangling-track selection"
        )
    tracks = {item.m_Uuid.AsString(): item for item in board.GetTracks()}
    zones = {
        str(zone.GetNetname())
        for zone in board.Zones()
        if not zone.GetIsRuleArea() and zone.GetNetname()
    }
    grouped = {
        item.m_Uuid.AsString() for group in board.Groups() for item in group.GetItems()
    }
    result = set()
    for violation in report["violations"]:
        if not isinstance(violation, dict) or violation.get("type") != "track_dangling":
            continue
        for record in violation.get("items", []):
            if not isinstance(record, dict):
                continue
            item = tracks.get(record.get("uuid"))
            if (
                item is None
                or not isinstance(item, pcbnew.PCB_TRACK)
                or isinstance(item, (pcbnew.PCB_VIA, pcbnew.PCB_ARC))
            ):
                continue
            uid, net = item.m_Uuid.AsString(), str(item.GetNetname())
            if (
                not net
                or net == "GND"
                or net not in eligible_nets
                or net in critical_nets
                or net in zones
                or uid in grouped
                or item.IsLocked()
            ):
                continue
            result.add(uid)
    return tuple(sorted(result))


def extend_dangling_tracks(
    board, seed_ids: set[str], eligible_nets: set[str], critical_nets: set[str]
) -> set[str]:
    """Walk dead-end signal tails without crossing a pad, via or branch.

    Seeds must come from native DRC; every result still requires independent
    connectivity, DRC, style and reference-copper validation.
    """
    tracks = {item.m_Uuid.AsString(): item for item in board.GetTracks()}
    zones = {
        str(zone.GetNetname())
        for zone in board.Zones()
        if not zone.GetIsRuleArea() and zone.GetNetname()
    }
    grouped = {
        item.m_Uuid.AsString() for group in board.Groups() for item in group.GetItems()
    }
    if not seed_ids <= tracks.keys():
        raise ValueError("A dangling-track seed is missing")
    for uid in seed_ids:
        item = tracks[uid]
        net = str(item.GetNetname())
        if (
            not isinstance(item, pcbnew.PCB_TRACK)
            or isinstance(item, (pcbnew.PCB_VIA, pcbnew.PCB_ARC))
            or not net
            or net == "GND"
            or net not in eligible_nets
            or net in critical_nets
            or net in zones
            or uid in grouped
            or item.IsLocked()
        ):
            raise ValueError("A dangling-track seed is protected or unsupported")
    nets = {str(tracks[uid].GetNetname()) for uid in seed_ids}
    connectivity = board.GetConnectivity()
    connectivity.Build(board)
    removed = set(seed_ids)
    pending = [
        item
        for uid in seed_ids
        for item in connectivity.GetConnectedTracks(tracks[uid])
    ]
    while pending:
        item = pending.pop()
        uid, net = item.m_Uuid.AsString(), str(item.GetNetname())
        if (
            uid in removed
            or net not in nets
            or not isinstance(item, pcbnew.PCB_TRACK)
            or isinstance(item, (pcbnew.PCB_VIA, pcbnew.PCB_ARC))
            or item.IsLocked()
            or uid in grouped
        ):
            continue
        neighbors = [
            other
            for other in connectivity.GetConnectedTracks(item)
            if other.m_Uuid.AsString() not in removed
        ]
        attached = set()
        for other in (*neighbors, *connectivity.GetConnectedPads(item)):
            hits = {
                index
                for index, point in enumerate((item.GetStart(), item.GetEnd()))
                if other.HitTest(point)
            }
            if not hits:
                attached = {0, 1}
                break
            attached.update(hits)
        if len(attached) > 1:
            continue
        removed.add(uid)
        pending.extend(neighbors)
    return removed


def merge_cleanup_groups(groups) -> tuple[frozenset[str], ...]:
    """Keep overlapping dead-end tails in the same cleanup transaction."""
    merged: list[set[str]] = []
    for group in groups:
        combined = set(group)
        if not combined:
            continue
        while any(combined & previous for previous in merged):
            touching = [previous for previous in merged if combined & previous]
            for previous in touching:
                combined.update(previous)
                merged.remove(previous)
        merged.append(combined)
    return tuple(
        sorted(
            (frozenset(group) for group in merged),
            key=lambda group: tuple(sorted(group)),
        )
    )


def split_cleanup_groups(
    groups: tuple[frozenset[str], ...], selected: frozenset[str]
) -> tuple[frozenset[str], ...]:
    """Split a failed candidate into smaller complete, non-overlapping tails."""
    included = [group for group in groups if group <= selected]
    if frozenset().union(*included) != selected:
        raise ValueError("Cleanup candidate does not contain complete branch groups")
    if len(included) < 2:
        return ()
    midpoint = len(included) // 2
    return (
        frozenset().union(*included[:midpoint]),
        frozenset().union(*included[midpoint:]),
    )


def newly_dangling_nets(
    before: dict, after: dict, item_nets: dict[str, str]
) -> set[str]:
    """Identify nets with new native dangling-track or dangling-via warnings."""

    def keys(report):
        if not isinstance(report, dict) or not isinstance(
            report.get("violations"), list
        ):
            raise ValueError(
                "Native DRC violations are required for warning comparison"
            )
        return {
            (entry.get("type"), item.get("uuid"))
            for entry in report["violations"]
            if isinstance(entry, dict)
            and entry.get("type") in {"track_dangling", "via_dangling"}
            for item in entry.get("items", [])
            if isinstance(item, dict) and item.get("uuid")
        }

    return {
        item_nets[uid]
        for _kind, uid in keys(after) - keys(before)
        if uid in item_nets and item_nets[uid]
    }


def newly_dangling_endpoint_nets(before: dict, after: dict) -> set[str]:
    """Recognize a newly dangling end even when its track UUID is unchanged."""
    old, new = before.get("dangling_warnings"), after.get("dangling_warnings")
    if old is None or new is None:
        return set()  # The caller retains strict native-report comparison.
    result = set()
    for index in _new_dangling_indices(old, new):
        for signature in new[index][1]:
            if not signature.startswith("dangling:"):
                raise ValueError("Invalid dangling endpoint identity")
            fields = json.loads(signature.removeprefix("dangling:"))
            if (
                not isinstance(fields, list)
                or not fields
                or not isinstance(fields[0], str)
                or not fields[0]
            ):
                raise ValueError("Invalid dangling endpoint net")
            result.add(fields[0])
    return result


def newly_affected_style_nets(before, after) -> set[str]:
    """Find nets whose surviving copper gained a bend, joint or staircase issue."""
    return {
        item.net
        for field in ("invalid_bends", "redundant_joints", "staircases")
        for item in set(getattr(after, field)) - set(getattr(before, field))
    }


def copper_removal_changes(board, selected: set[str]) -> list[dict]:
    """Describe removals without transferring native KiCad object ownership."""
    tracks = {item.m_Uuid.AsString(): item for item in board.GetTracks()}
    if not selected <= tracks.keys():
        raise ValueError("A selected copper item was not found on the board")
    grouped = {
        item.m_Uuid.AsString() for group in board.Groups() for item in group.GetItems()
    }
    result = []
    for uid in sorted(selected):
        item = tracks[uid]
        if item.IsLocked() or uid in grouped or isinstance(item, pcbnew.PCB_ARC):
            raise ValueError("A selected copper item is locked, grouped or unsupported")
        kind = "via" if isinstance(item, pcbnew.PCB_VIA) else "segment"
        result.append(
            {
                "document": "pcb",
                "target": {"kind": kind, "uuid": uid},
                "op": "remove",
                "path": [],
            }
        )
    return result


def coordinated_tail_changes(
    board, selected: set[str], eligible_nets: set[str], critical_nets: set[str]
) -> list[dict]:
    """Chamfer an exposed interior branch after an eligible dead-end removal.

    Only exact, unambiguous axis-aligned junctions are considered. Unrecognized
    contacts, pads on the trunk, protected nets, and locked or grouped items
    defer the cleanup. The resulting proposal still needs all native checks.
    """
    from pcb.net_routing.geometry import on_segment
    from pcb.route_cli import campaign_changes
    from pcb.route_junctions import _contacts
    from pcb.route_optimizer import Point, Route, Segment

    tracks = {item.m_Uuid.AsString(): item for item in board.GetTracks()}
    if not selected <= tracks.keys():
        raise ValueError("A selected copper item was not found on the board")
    grouped = {
        item.m_Uuid.AsString() for group in board.Groups() for item in group.GetItems()
    }
    connectivity = board.GetConnectivity()
    connectivity.Build(board)
    claimed, changes = set(), []

    def point(value):
        return Point(int(value.x), int(value.y))

    def segment(item):
        return Segment(
            point(item.GetStart()),
            point(item.GetEnd()),
            board.GetLayerName(item.GetLayer()),
        )

    def plain(item):
        return (
            isinstance(item, pcbnew.PCB_TRACK)
            and not isinstance(item, (pcbnew.PCB_VIA, pcbnew.PCB_ARC))
            and not item.IsLocked()
            and item.m_Uuid.AsString() not in grouped
        )

    def contact(first, other):
        if isinstance(other, pcbnew.PCB_VIA):
            at = point(other.GetPosition())
            return {at} if on_segment(at, first) else set()
        if not plain(other) or board.GetLayerName(other.GetLayer()) != first.layer:
            return set()
        return _contacts(first, segment(other))

    def replace_endpoint(uid, field, at):
        x, y = (format(Decimal(value) / 1_000_000, "f") for value in (at.x, at.y))
        return {
            "document": "pcb",
            "target": {"kind": "segment", "uuid": uid},
            "op": "replace",
            "path": [field],
            "expression": f"({field} {x} {y})",
        }

    candidates = {}
    for uid in sorted(selected):
        seed = tracks[uid]
        for raw in connectivity.GetConnectedTracks(seed):
            other_uid = raw.m_Uuid.AsString()
            if other_uid in tracks and other_uid not in selected:
                candidates[other_uid] = tracks[other_uid]
    for uid, trunk in sorted(candidates.items()):
        net = str(trunk.GetNetname())
        if (
            uid in claimed
            or not plain(trunk)
            or not net
            or net not in eligible_nets
            or net in critical_nets
            or connectivity.GetConnectedPads(trunk)
        ):
            continue
        trunk_segment = segment(trunk)
        ends = {"start": trunk_segment.start, "end": trunk_segment.end}
        removed_contacts, remaining, valid = set(), {}, True
        for raw in connectivity.GetConnectedTracks(trunk):
            other_uid = raw.m_Uuid.AsString()
            if other_uid == uid or other_uid not in tracks:
                continue
            other = tracks[other_uid]
            if str(other.GetNetname()) != net:
                valid = False
                break
            contacts = contact(trunk_segment, other)
            if len(contacts) != 1:
                valid = False
                break
            at = next(iter(contacts))
            if other_uid in selected:
                removed_contacts.add(at)
            else:
                remaining.setdefault(at, []).append(other)
        if not valid or len(removed_contacts) != 1 or len(remaining) < 2:
            continue
        free = next(iter(removed_contacts))
        fields = [name for name, at in ends.items() if at == free]
        if len(fields) != 1 or free in remaining:
            continue
        joint = min(
            remaining, key=lambda at: (at.x - free.x) ** 2 + (at.y - free.y) ** 2
        )
        if len(remaining[joint]) != 1:
            continue
        branch = remaining[joint][0]
        branch_uid = branch.m_Uuid.AsString()
        if (
            branch_uid in claimed
            or branch_uid in selected
            or not plain(branch)
            or branch.GetLayer() != trunk.GetLayer()
            or copper_width(branch) != copper_width(trunk)
        ):
            continue
        branch_segment = segment(branch)
        branch_ends = {"start": branch_segment.start, "end": branch_segment.end}
        branch_fields = [name for name, at in branch_ends.items() if at == joint]
        if len(branch_fields) != 1:
            continue
        far_trunk = ends["end" if fields[0] == "start" else "start"]
        far_branch = branch_ends["end" if branch_fields[0] == "start" else "start"]
        vectors = [
            (far_trunk.x - joint.x, far_trunk.y - joint.y),
            (far_branch.x - joint.x, far_branch.y - joint.y),
        ]
        if (
            any((dx == 0) == (dy == 0) for dx, dy in vectors)
            or sum(a * b for a, b in strict_zip(*vectors)) != 0
        ):
            continue
        distance = min(250_000, *(max(abs(dx), abs(dy)) // 2 for dx, dy in vectors))
        if distance < 1:
            continue
        cuts = [
            Point(
                joint.x + ((dx > 0) - (dx < 0)) * distance,
                joint.y + ((dy > 0) - (dy < 0)) * distance,
            )
            for dx, dy in vectors
        ]
        radius = (copper_width(trunk) + 1) // 2
        left, right = (
            min(joint.x, *(p.x for p in cuts)) - radius,
            max(joint.x, *(p.x for p in cuts)) + radius,
        )
        top, bottom = (
            min(joint.y, *(p.y for p in cuts)) - radius,
            max(joint.y, *(p.y for p in cuts)) + radius,
        )
        excluded = selected | {uid, branch_uid}
        blocked = False
        for other in (*board.GetPads(), *board.GetTracks(), *board.Zones()):
            if (
                other.m_Uuid.AsString() in excluded
                or str(other.GetNetname()) != net
                or not other.IsOnLayer(trunk.GetLayer())
            ):
                continue
            box = other.GetBoundingBox()
            if (
                box.GetX() <= right
                and box.GetRight() >= left
                and box.GetY() <= bottom
                and box.GetBottom() >= top
            ):
                blocked = True
                break
        if blocked:
            continue
        changes.extend(
            (
                replace_endpoint(uid, fields[0], cuts[0]),
                replace_endpoint(branch_uid, branch_fields[0], cuts[1]),
            )
        )
        diagonal = Segment(cuts[0], cuts[1], trunk_segment.layer)
        changes.extend(
            campaign_changes(
                Route((diagonal,), (), (), 0, 0, 0, 0),
                net,
                int(copper_width(trunk)),
                0,
                0,
            )
        )
        claimed.update((uid, branch_uid))
    return changes


def run_one(
    project: Path, run_id: str, classes: tuple[str, ...], *, progress=print
) -> dict:
    """Retain a via-only candidate only after independent native validation."""
    if (
        not classes
        or any(not isinstance(name, str) or not name for name in classes)
        or len(set(classes)) != len(classes)
    ):
        raise ValueError("Choose one or more distinct net classes")
    project = project.resolve()
    run_id = _run_id(run_id)
    assets = asset_root(project)
    policy, policy_hash = read_config(config_path(project, "autopcb.json"))
    state_path = results_path(project, "autoroute") / f"{run_id}.json"
    state_bytes = state_path.read_bytes()
    state = json.loads(state_bytes)
    if state.get("schema") != 1 or state.get("run_id") != run_id:
        raise ValueError("Invalid source routing preview")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="autopcb-via-candidate-") as folder:
        candidate_path = Path(folder) / "candidate.kicad_pcb"
        with Snapshot(native_path(project, ".kicad_pcb")) as snapshot:
            inputs = _source_hashes(snapshot, project, assets)
            retained = decode_checkpoint(
                state["checkpoint"],
                inputs,
                snapshot.tool_hashes,
                policy_hash,
                allow_tool_changes=True,
            )
            schematic = (native_path(snapshot.directory, ".kicad_sch")).read_bytes()
            protected = ProtectedState.capture(snapshot.original, schematic)
            if protected.differences(retained, schematic):
                raise RuntimeError(
                    "The retained preview changed protected design items"
                )
            snapshot.board_path.write_bytes(retained)
            board = pcbnew.LoadBoard(str(snapshot.board_path))
            if board is None:
                raise RuntimeError("KiCad could not open the private preview")
            requested = {router_class(name) for name in classes}
            all_names = {
                str(name) for name in board.GetNetsByName().keys() if str(name)
            }
            eligible = {
                name
                for name in all_names
                if router_class(str(board.FindNet(name).GetNetClassName())) in requested
            }
            if not eligible:
                raise ValueError("No nets have the requested classes")
            critical = high_speed_nets(
                (native_path(snapshot.directory, ".kicad_pro")).read_bytes(), all_names
            )
            critical |= unqualified_working_nets(
                board, tuple(policy.get("exclude_nets", ()))
            )
            vias = set(candidate_vias(board, eligible, critical))
            selected = dangling_spur_items(board, vias)
            progress(
                f"Found {len(vias)} possible single-layer signal vias and "
                f"{len(selected) - len(vias)} dead-end tracks"
            )
            if vias:
                progress("Validating the removal candidate on a private board")
                changes = copper_removal_changes(board, selected)
                candidate_path.write_bytes(
                    CampaignPlanner(snapshot.directory).plan("pcb", changes).after
                )
                if protected.differences(candidate_path.read_bytes(), schematic):
                    raise RuntimeError("Via pruning changed protected design items")
            snapshot.check()
            if state_path.read_bytes() != state_bytes:
                raise RuntimeError("The source preview changed during via selection")
        if not vias:
            record_unimproved_batch(project, run_id, time.monotonic() - started)
            return {"status": "no_candidate", "run_id": run_id, "candidate_vias": 0}
        result = run_native(
            retain_candidate,
            project,
            run_id,
            candidate_path,
            routing_seconds=time.monotonic() - started,
            expected_previous=state_bytes,
            engine="native-via-prune",
            objective="minimize-vias",
            progress=progress,
            forward_progress=True,
            heartbeat=lambda: progress("Still validating the private via candidate"),
        )
        return {
            **result,
            "candidate_vias": len(vias),
            "candidate_spur_tracks": len(selected) - len(vias),
        }


def verify_cleanup_baseline(baseline: dict, *, expected_opens: int) -> tuple[str, ...]:
    """Identify parity nets while requiring attributable, unchanged WIP checks."""
    from pcb.net_routing.fast_rework import parity_protected_nets

    if (
        not isinstance(baseline, dict)
        or baseline.get("exclusions") != 0
        or baseline.get("ignored_checks") != []
    ):
        raise RuntimeError("The cleanup baseline has exclusions or ignored checks")
    parity_nets = parity_protected_nets(baseline)
    # This validates the detailed native identities even when a prior error is
    # present. Candidate retention later compares against this same baseline.
    verify_resume_reports(baseline, baseline, expected_opens=expected_opens)
    return parity_nets


def run_dangling(
    project: Path, run_id: str, classes: tuple[str, ...], *, progress=print
) -> dict:
    """Retain a validated subset of reported dead-end signal tracks."""
    if (
        not classes
        or any(not isinstance(name, str) or not name for name in classes)
        or len(set(classes)) != len(classes)
    ):
        raise ValueError("Choose one or more distinct net classes")
    project, run_id = project.resolve(), _run_id(run_id)
    assets = asset_root(project)
    policy, policy_hash = read_config(config_path(project, "autopcb.json"))
    state_path = results_path(project, "autoroute") / f"{run_id}.json"
    state_bytes = state_path.read_bytes()
    state = json.loads(state_bytes)
    if state.get("schema") != 1 or state.get("run_id") != run_id:
        raise ValueError("Invalid source routing preview")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="autopcb-dangling-candidate-") as folder:
        candidate_path = Path(folder) / "candidate.kicad_pcb"
        with Snapshot(native_path(project, ".kicad_pcb")) as snapshot:
            inputs = _source_hashes(snapshot, project, assets)
            retained = decode_checkpoint(
                state["checkpoint"],
                inputs,
                snapshot.tool_hashes,
                policy_hash,
                allow_tool_changes=True,
            )
            schematic = (native_path(snapshot.directory, ".kicad_sch")).read_bytes()
            protected = ProtectedState.capture(snapshot.original, schematic)
            if protected.differences(retained, schematic):
                raise RuntimeError(
                    "The retained preview changed protected design items"
                )
            snapshot.board_path.write_bytes(retained)
            cli = kicad_cli()
            progress("Inspecting native dangling-track warnings on a private board")
            before = _drc(
                cli,
                snapshot.board_path,
                snapshot.directory / "dangling-before.json",
                save_refill=True,
                schematic_parity=True,
            )
            retained = snapshot.board_path.read_bytes()
            if protected.differences(retained, schematic):
                raise RuntimeError("Native refill changed protected design items")
            board = pcbnew.LoadBoard(str(snapshot.board_path))
            if board is None:
                raise RuntimeError("KiCad could not open the private preview")
            baseline = summarize_drc(
                before,
                segment_identities=_segment_identities(retained),
                dangling_identities=_dangling_track_identities(board, before),
            )
            pads = {
                pad.m_Uuid.AsString(): str(pad.GetNetname()) for pad in board.GetPads()
            }
            baseline["parity_blocked_nets"] = list(parity_blocked_nets(before, pads))
            parity_nets = verify_cleanup_baseline(
                baseline, expected_opens=state["best_preview_opens"]
            )
            all_names = {
                str(name) for name in board.GetNetsByName().keys() if str(name)
            }
            requested = {router_class(name) for name in classes}
            eligible = {
                name
                for name in all_names
                if router_class(str(board.FindNet(name).GetNetClassName())) in requested
            }
            if not eligible:
                raise ValueError("No nets have the requested classes")
            critical = high_speed_nets(
                (native_path(snapshot.directory, ".kicad_pro")).read_bytes(), all_names
            )
            if not critical:
                raise RuntimeError("High-speed net classification is unavailable")
            critical |= unqualified_working_nets(
                board, tuple(policy.get("exclude_nets", ()))
            )
            critical.update(parity_nets)
            seeds = set(candidate_dangling_tracks(board, before, eligible, critical))
            groups = merge_cleanup_groups(
                extend_dangling_tracks(board, {seed}, eligible, critical)
                for seed in sorted(seeds)
            )
            selected = set().union(*groups)
            progress(
                f"Found {len(seeds)} reported dangling tracks and {len(selected) - len(seeds)} tail pieces"
            )
            if not selected:
                return {"status": "no_candidate", "run_id": run_id}
            tracks = {item.m_Uuid.AsString(): item for item in board.GetTracks()}
            old_style = normalize_segment_identities(
                audit_style(board), _segment_identities(retained)
            )
            accepted = False
            pending = [frozenset(selected)]
            tried: set[frozenset[str]] = set()
            for attempt in range(8):
                if not pending:
                    break
                selected = set(pending.pop(0))
                tried.add(frozenset(selected))
                snapshot.board_path.write_bytes(retained)
                changes = copper_removal_changes(board, selected)
                changes.extend(
                    coordinated_tail_changes(board, selected, eligible, critical)
                )
                candidate = (
                    CampaignPlanner(snapshot.directory).plan("pcb", changes).after
                )
                if protected.differences(candidate, schematic):
                    raise RuntimeError(
                        "Dangling cleanup changed protected design items"
                    )
                snapshot.board_path.write_bytes(candidate)
                after = _drc(
                    cli,
                    snapshot.board_path,
                    snapshot.directory / f"dangling-after-{attempt}.json",
                    save_refill=True,
                    schematic_parity=True,
                )
                after_bytes = snapshot.board_path.read_bytes()
                after_board = pcbnew.LoadBoard(str(snapshot.board_path))
                if after_board is None:
                    raise RuntimeError("KiCad could not inspect the cleanup candidate")
                summary = summarize_drc(
                    after,
                    segment_identities=_segment_identities(after_bytes),
                    dangling_identities=_dangling_track_identities(after_board, after),
                )
                progress(
                    f"Cleanup check {attempt + 1}: {summary['open_connections']} opens, "
                    f"{summary['warnings']} warnings"
                )
                try:
                    verify_resume_reports(
                        baseline, summary, expected_opens=summary["open_connections"]
                    )
                    run_native(
                        _verify_resume_connectivity, snapshot.directory, retained
                    )
                except RuntimeError as error:
                    item_nets = {
                        item.m_Uuid.AsString(): str(item.GetNetname())
                        for item in after_board.GetTracks()
                    }
                    affected = newly_dangling_nets(before, after, item_nets)
                    affected.update(newly_dangling_endpoint_nets(baseline, summary))
                    if "routing style issues" in str(error):
                        after_style = normalize_segment_identities(
                            audit_style(after_board), _segment_identities(after_bytes)
                        )
                        affected.update(
                            newly_affected_style_nets(old_style, after_style)
                        )
                    remaining = {
                        uid
                        for uid in selected
                        if str(tracks[uid].GetNetname()) not in affected
                    }
                    alternatives = []
                    if affected and remaining and remaining != selected:
                        progress(
                            "Deferring affected nets: " + ", ".join(sorted(affected))
                        )
                        alternatives.append(frozenset(remaining))
                    alternatives.extend(
                        split_cleanup_groups(groups, frozenset(selected))
                    )
                    pending.extend(
                        option
                        for option in alternatives
                        if option and option not in tried and option not in pending
                    )
                    if pending:
                        progress("Trying smaller independent cleanup groups")
                    else:
                        progress(f"No safe cleanup subset: {error}")
                    continue
                if summary["warnings"] >= baseline["warnings"]:
                    progress("No warning reduction remained after validation")
                    break
                candidate_path.write_bytes(after_bytes)
                accepted = True
                break
            snapshot.check()
            if state_path.read_bytes() != state_bytes:
                raise RuntimeError(
                    "The source preview changed during dangling-track selection"
                )
        if not accepted:
            return {"status": "no_safe_candidate", "run_id": run_id}
        progress("Independently validating the cleanup candidate")
        engine = (
            "native-dangling-chamfer"
            if any(change["op"] == "insert" for change in changes)
            else "native-dangling-prune"
        )
        result = run_native(
            retain_candidate,
            project,
            run_id,
            candidate_path,
            routing_seconds=time.monotonic() - started,
            expected_previous=state_bytes,
            engine=engine,
            objective="reduce-warnings",
            progress=progress,
            forward_progress=True,
            heartbeat=lambda: progress("Still validating the private cleanup"),
        )
        return {**result, "removed_tracks": len(selected)}


def run_rounding(
    project: Path, run_id: str, classes: tuple[str, ...], *, progress=print
) -> dict:
    """Try one bounded joint repair and independently retain it if valid."""
    if not classes or len(set(classes)) != len(classes):
        raise ValueError("Choose one or more distinct net classes")
    project, run_id = project.resolve(), _run_id(run_id)
    assets = asset_root(project)
    policy, policy_hash = read_config(config_path(project, "autopcb.json"))
    state_path = results_path(project, "autoroute") / f"{run_id}.json"
    state_bytes = state_path.read_bytes()
    state = json.loads(state_bytes)
    if state.get("schema") != 1 or state.get("run_id") != run_id:
        raise ValueError("Invalid source routing preview")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="autopcb-rounding-candidate-") as folder:
        candidate_path = Path(folder) / "candidate.kicad_pcb"
        with Snapshot(native_path(project, ".kicad_pcb")) as snapshot:
            inputs = _source_hashes(snapshot, project, assets)
            retained = decode_checkpoint(
                state["checkpoint"],
                inputs,
                snapshot.tool_hashes,
                policy_hash,
                allow_tool_changes=True,
            )
            schematic = (native_path(snapshot.directory, ".kicad_sch")).read_bytes()
            protected = ProtectedState.capture(snapshot.original, schematic)
            if protected.differences(retained, schematic):
                raise RuntimeError(
                    "The retained preview changed protected design items"
                )
            snapshot.board_path.write_bytes(retained)
            board = pcbnew.LoadBoard(str(snapshot.board_path))
            if board is None:
                raise RuntimeError("KiCad could not open the private preview")
            names = {str(name) for name in board.GetNetsByName() if str(name)}
            requested = {router_class(name) for name in classes}
            critical = high_speed_nets(
                (native_path(snapshot.directory, ".kicad_pro")).read_bytes(), names
            )
            if not critical:
                raise RuntimeError("High-speed net classification is unavailable")
            blocked = critical | unqualified_working_nets(
                board, tuple(policy.get("exclude_nets", ()))
            )
            eligible = {
                name
                for name in names - blocked
                if router_class(str(board.FindNet(name).GetNetClassName())) in requested
            }
            progress("Searching eligible signals for a bounded 0.05 mm joint alignment")
            proposal = find_candidate(board, eligible)
            if proposal:
                candidate_path.write_bytes(
                    CampaignPlanner(snapshot.directory)
                    .plan("pcb", proposal["changes"])
                    .after
                )
                if protected.differences(candidate_path.read_bytes(), schematic):
                    raise RuntimeError("Rounding changed protected design items")
            snapshot.check()
            if state_path.read_bytes() != state_bytes:
                raise RuntimeError("The source preview changed during rounding search")
        if not proposal:
            record_unimproved_batch(project, run_id, time.monotonic() - started)
            return {"status": "no_candidate", "run_id": run_id}
        progress(
            f"Independently checking the {proposal['net']} joint-alignment proposal"
        )
        result = run_native(
            retain_candidate,
            project,
            run_id,
            candidate_path,
            routing_seconds=time.monotonic() - started,
            expected_previous=state_bytes,
            engine="native-rounding-repair",
            objective="repair-rounding",
            progress=progress,
            forward_progress=True,
            heartbeat=lambda: progress(
                "Still validating the private rounding candidate"
            ),
        )
        return {**result, "corrected": proposal["corrected"], "net": proposal["net"]}


def run_staircase(
    project: Path, run_id: str, classes: tuple[str, ...], *, progress=print
) -> dict:
    """Try automatically selected, independently checked staircase shortcuts."""
    from pcb.freeroute_delta import (
        native_shortcut_obstacles,
        read_copper,
        shortcut_clearance_checker,
    )
    from pcb.net_routing.reference import reference_corridors
    from pcb.net_routing.rules import RoutingRules
    from pcb.net_routing.staircase_repair import find_candidates, verify_candidate
    from pcb.route_optimizer import mm

    if not classes or len(set(classes)) != len(classes):
        raise ValueError("Choose one or more distinct net classes")
    project, run_id = project.resolve(), _run_id(run_id)
    assets = asset_root(project)
    policy, policy_hash = read_config(config_path(project, "autopcb.json"))
    state_path = results_path(project, "autoroute") / f"{run_id}.json"
    state_bytes = state_path.read_bytes()
    state = json.loads(state_bytes)
    if state.get("schema") != 1 or state.get("run_id") != run_id:
        raise ValueError("Invalid source routing preview")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="autopcb-staircase-candidate-") as folder:
        candidate_path = Path(folder) / "candidate.kicad_pcb"
        with Snapshot(native_path(project, ".kicad_pcb")) as snapshot:
            inputs = _source_hashes(snapshot, project, assets)
            retained = decode_checkpoint(
                state["checkpoint"],
                inputs,
                snapshot.tool_hashes,
                policy_hash,
                allow_tool_changes=True,
            )
            schematic = (native_path(snapshot.directory, ".kicad_sch")).read_bytes()
            protected = ProtectedState.capture(snapshot.original, schematic)
            if protected.differences(retained, schematic):
                raise RuntimeError(
                    "The retained preview changed protected design items"
                )
            snapshot.board_path.write_bytes(retained)
            cli = kicad_cli()
            progress("Checking the native baseline for staircase cleanup")
            before = _drc(
                cli,
                snapshot.board_path,
                snapshot.directory / "staircase-before.json",
                save_refill=True,
                schematic_parity=True,
            )
            retained = snapshot.board_path.read_bytes()
            board = pcbnew.LoadBoard(str(snapshot.board_path))
            if board is None or protected.differences(retained, schematic):
                raise RuntimeError(
                    "The staircase baseline changed protected design items"
                )
            baseline = summarize_drc(
                before,
                segment_identities=_segment_identities(retained),
                dangling_identities=_dangling_track_identities(board, before),
            )
            pads = {
                pad.m_Uuid.AsString(): str(pad.GetNetname()) for pad in board.GetPads()
            }
            baseline["parity_blocked_nets"] = list(parity_blocked_nets(before, pads))
            parity_nets = verify_cleanup_baseline(
                baseline, expected_opens=state["best_preview_opens"]
            )
            names = {str(name) for name in board.GetNetsByName() if str(name)}
            high_speed = high_speed_nets(
                (native_path(snapshot.directory, ".kicad_pro")).read_bytes(), names
            )
            if not high_speed:
                raise RuntimeError("High-speed net classification is unavailable")
            blocked = (
                high_speed
                | unqualified_working_nets(board, tuple(policy.get("exclude_nets", ())))
                | set(parity_nets)
            )
            requested = {router_class(name) for name in classes}
            eligible = {
                name
                for name in names - blocked
                if router_class(str(board.FindNet(name).GetNetClassName())) in requested
            }
            rules = RoutingRules.from_board(
                board, native_path(snapshot.directory, ".kicad_dru")
            )
            obstacles = (
                *native_shortcut_obstacles(board),
                *reference_corridors(board, high_speed).obstacles,
            )
            profiles = {}

            def profile(trace):
                key = (trace.net, trace.width)
                if key not in profiles:
                    default = policy.get("route", {}).get("clearance_mm", 0.2)
                    setting = (
                        policy.get("net_overrides", {})
                        .get(trace.net, {})
                        .get("clearance_mm", default)
                    )
                    profiles[key] = rules.profile(trace.net, trace.width, mm(setting))
                return profiles[key]

            clear = shortcut_clearance_checker(obstacles, profile_for=profile)
            proposals = find_candidates(
                board, read_copper(snapshot.board_path), eligible, clear
            )
            progress(f"Found {len(proposals)} eligible local staircase shortcuts")
            independent, seen_nets = [], set()
            for proposal in proposals:
                if proposal["net"] not in seen_nets:
                    independent.append(proposal)
                    seen_nets.add(proposal["net"])
            pending = [tuple(independent)] if independent else []
            accepted = None
            index = 0
            while pending and index < 12:
                group = pending.pop(0)
                snapshot.board_path.write_bytes(retained)
                candidate = (
                    CampaignPlanner(snapshot.directory)
                    .plan(
                        "pcb",
                        [
                            change
                            for proposal in group
                            for change in proposal["changes"]
                        ],
                    )
                    .after
                )
                if protected.differences(candidate, schematic):
                    raise RuntimeError(
                        "Staircase cleanup changed protected design items"
                    )
                snapshot.board_path.write_bytes(candidate)
                report = _drc(
                    cli,
                    snapshot.board_path,
                    snapshot.directory / f"staircase-after-{index}.json",
                    save_refill=True,
                    schematic_parity=True,
                )
                candidate = snapshot.board_path.read_bytes()
                revised = pcbnew.LoadBoard(str(snapshot.board_path))
                if revised is None:
                    raise RuntimeError(
                        "KiCad could not inspect the staircase candidate"
                    )
                summary = summarize_drc(
                    report,
                    segment_identities=_segment_identities(candidate),
                    dangling_identities=_dangling_track_identities(revised, report),
                )
                try:
                    verify_resume_reports(
                        baseline, summary, expected_opens=summary["open_connections"]
                    )
                    proof = verify_candidate(
                        retained, candidate, board, revised, eligible
                    )
                    run_native(
                        _verify_resume_connectivity, snapshot.directory, retained
                    )
                except (ValueError, RuntimeError) as error:
                    progress(f"Shortcut batch {index + 1} did not pass: {error}")
                    if len(group) > 1:
                        midpoint = len(group) // 2
                        pending.extend((group[:midpoint], group[midpoint:]))
                    index += 1
                    continue
                candidate_path.write_bytes(candidate)
                accepted = proof
                break
            snapshot.check()
            if state_path.read_bytes() != state_bytes:
                raise RuntimeError(
                    "The source preview changed during staircase selection"
                )
        if accepted is None:
            record_unimproved_batch(project, run_id, time.monotonic() - started)
            return {"status": "no_safe_candidate", "run_id": run_id}
        progress("Independently validating the staircase shortcut")
        result = run_native(
            retain_candidate,
            project,
            run_id,
            candidate_path,
            routing_seconds=time.monotonic() - started,
            expected_previous=state_bytes,
            engine="native-staircase-repair",
            objective="repair-staircase",
            progress=progress,
            forward_progress=True,
            heartbeat=lambda: progress("Still validating the staircase shortcut"),
        )
        return {**result, **accepted}


def run_isolated_fragments(
    project: Path, run_id: str, *, require_error: bool, progress=print
) -> dict:
    """Propose removal of native-reported, electrically isolated tiny fragments."""
    from pcb.net_routing.isolated_copper import isolated_dangling_fragments

    project, run_id = project.resolve(), _run_id(run_id)
    assets = asset_root(project)
    policy, policy_hash = read_config(config_path(project, "autopcb.json"))
    state_path = results_path(project, "autoroute") / f"{run_id}.json"
    state_bytes = state_path.read_bytes()
    state = json.loads(state_bytes)
    if state.get("schema") != 1 or state.get("run_id") != run_id:
        raise ValueError("Invalid source routing preview")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="autopcb-isolated-candidate-") as folder:
        candidate_path = Path(folder) / "candidate.kicad_pcb"
        with Snapshot(native_path(project, ".kicad_pcb")) as snapshot:
            inputs = _source_hashes(snapshot, project, assets)
            retained = decode_checkpoint(
                state["checkpoint"],
                inputs,
                snapshot.tool_hashes,
                policy_hash,
                allow_tool_changes=True,
            )
            schematic = (native_path(snapshot.directory, ".kicad_sch")).read_bytes()
            protected = ProtectedState.capture(snapshot.original, schematic)
            if protected.differences(retained, schematic):
                raise RuntimeError(
                    "The retained preview changed protected design items"
                )
            snapshot.board_path.write_bytes(retained)
            cli = kicad_cli()
            report = _drc(
                cli,
                snapshot.board_path,
                snapshot.directory / "isolated-before.json",
                save_refill=True,
                schematic_parity=True,
            )
            retained = snapshot.board_path.read_bytes()
            board = pcbnew.LoadBoard(str(snapshot.board_path))
            if board is None or protected.differences(retained, schematic):
                raise RuntimeError(
                    "The cleanup baseline changed protected design items"
                )
            baseline = summarize_drc(
                report,
                segment_identities=_segment_identities(retained),
                dangling_identities=_dangling_track_identities(board, report),
            )
            pads = {
                pad.m_Uuid.AsString(): str(pad.GetNetname()) for pad in board.GetPads()
            }
            baseline["parity_blocked_nets"] = list(parity_blocked_nets(report, pads))
            parity_nets = verify_cleanup_baseline(
                baseline, expected_opens=state["best_preview_opens"]
            )
            names = {str(name) for name in board.GetNetsByName() if str(name)}
            critical = high_speed_nets(
                (native_path(snapshot.directory, ".kicad_pro")).read_bytes(), names
            )
            if not critical:
                raise RuntimeError("High-speed net classification is unavailable")
            excluded = critical | set(policy.get("exclude_nets", ())) | set(parity_nets)
            selected = set(
                isolated_dangling_fragments(
                    board, report, excluded, require_error=require_error
                )
            )
            progress(f"Found {len(selected)} isolated dangling fragments")
            if selected:
                candidate = (
                    CampaignPlanner(snapshot.directory)
                    .plan("pcb", copper_removal_changes(board, selected))
                    .after
                )
                if protected.differences(candidate, schematic):
                    raise RuntimeError(
                        "Isolated-fragment cleanup changed protected design items"
                    )
                candidate_path.write_bytes(candidate)
            snapshot.check()
            if state_path.read_bytes() != state_bytes:
                raise RuntimeError(
                    "The source preview changed during cleanup selection"
                )
        if not selected:
            return {"status": "no_candidate", "run_id": run_id}
        progress("Independently verifying the isolated-fragment cleanup")
        result = run_native(
            retain_candidate,
            project,
            run_id,
            candidate_path,
            routing_seconds=time.monotonic() - started,
            expected_previous=state_bytes,
            engine="native-dangling-prune",
            objective="reduce-warnings",
            progress=progress,
            forward_progress=True,
            heartbeat=lambda: progress("Still verifying the cleanup"),
        )
        return {**result, "removed_fragments": len(selected)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument(
        "--net-class",
        action="append",
        help="consider copper on this class; may be repeated",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dangling-tracks",
        action="store_true",
        help="instead prune reported dead-end tracks from the selected classes",
    )
    mode.add_argument(
        "--repair-rounding",
        action="store_true",
        help="instead repair a safe one-nanometre off-angle signal joint",
    )
    mode.add_argument(
        "--repair-staircases",
        action="store_true",
        help="instead simplify eligible existing signal staircases",
    )
    mode.add_argument(
        "--isolated-errors",
        action="store_true",
        help="remove tiny isolated tracks with an error and dangling warning",
    )
    mode.add_argument(
        "--isolated-warnings",
        action="store_true",
        help="remove tiny isolated tracks with a dangling warning",
    )
    args = parser.parse_args(argv)
    try:
        with quiet_duplicate_image_handlers():
            started = time.monotonic()

            def progress(message):
                print(message, flush=True)

            if args.isolated_errors or args.isolated_warnings:
                if args.net_class:
                    raise ValueError(
                        "Isolated-fragment cleanup searches all permitted nets; omit --net-class"
                    )
                result = run_isolated_fragments(
                    args.project,
                    args.run,
                    require_error=args.isolated_errors,
                    progress=progress,
                )
            else:
                if not args.net_class:
                    raise ValueError("Choose at least one --net-class for this cleanup")
                operation = (
                    run_dangling
                    if args.dangling_tracks
                    else run_rounding
                    if args.repair_rounding
                    else run_staircase
                    if args.repair_staircases
                    else run_one
                )
                result = operation(
                    args.project, args.run, tuple(args.net_class), progress=progress
                )
            if (
                args.dangling_tracks or args.isolated_errors or args.isolated_warnings
            ) and result["status"] != "private_preview_retained":
                record_unimproved_batch(
                    args.project, args.run, time.monotonic() - started
                )
        print(f"Result: {result['status']}; run: {result['run_id']}")
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Copper cleanup stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
