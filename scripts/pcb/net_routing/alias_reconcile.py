"""Reconcile complete schematic-proven board-net aliases with guarded checks.

The operation changes net names only, never copper or footprint geometry. A
previously hidden schematic connection may become an open PCB connection.
"""

from __future__ import annotations

from core.project import native_path

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.toolchain import kicad_cli
from uuid import uuid4

import pcbnew

from core.kicad_sexpr import quote
from core.paths import results_path
from core.pcb_io import BoardSaveGuard, quiet_duplicate_image_handlers
from pcb.net_routing.controller import decode_checkpoint, encode_checkpoint
from pcb.net_routing.native import Snapshot
from pcb.net_routing.prepare import parity_blocked_nets, summarize_drc
from pcb.net_routing.protection import ProtectedState
from pcb.net_routing.reference import high_speed_nets
from pcb.net_routing.runner import (
    _verify_cumulative_reference_native,
    _verify_resume_connectivity,
    verify_resume_reports,
)
from pcb.reconcile_named_nets import board_pads, collect_candidates, export_schematic_data, explicitly_named
from pcb.route_validation import _dangling_track_identities, _drc, _segment_identities, stable_connection_items


@dataclass(frozen=True)
class AliasGroup:
    old: str
    target: str
    pad_ids: tuple[str, ...]
    track_ids: tuple[str, ...]


def replace_aliases(data: bytes, groups: tuple[AliasGroup, ...]) -> bytes:
    """Retag only an entire, uniquely identified old pad-and-copper group."""
    if not isinstance(data, bytes) or not isinstance(groups, tuple):
        raise ValueError("A board and complete alias groups are required")
    originals, targets, identifiers = set(), set(), set()
    for group in groups:
        if (not isinstance(group, AliasGroup) or not group.old.startswith("Net-(")
                or not explicitly_named(group.target) or not group.pad_ids or not group.track_ids
                or any(not isinstance(uid, str) or not uid for uid in group.pad_ids + group.track_ids)
                or len(set(group.pad_ids + group.track_ids)) != len(group.pad_ids + group.track_ids)
                or set(group.pad_ids + group.track_ids) & identifiers or group.old in originals):
            raise ValueError("Invalid or ambiguous copper alias group")
        originals.add(group.old)
        targets.add(group.target)
        identifiers.update(group.pad_ids + group.track_ids)
    if originals & targets:
        raise ValueError("Alias targets cannot also be old aliases")
    result = data
    for group in groups:
        old = f"(net {quote(group.old)})".encode()
        new = f"(net {quote(group.target)})".encode()
        if data.count(old) != len(group.pad_ids) + len(group.track_ids):
            raise ValueError("The board does not contain the complete known alias group")
        result = result.replace(old, new)
    return result


def find_groups(board, expected: dict, report: dict) -> tuple[AliasGroup, ...]:
    """Identify only whole, unambiguous mismatched pad groups without zones."""
    pads = board_pads(board)
    candidates, _, _ = collect_candidates(board, expected, False)
    pad_nets = {pad.m_Uuid.AsString(): str(pad.GetNetname()) for pad in pads}
    parity_blocked_nets(report, pad_nets)  # Unknown findings fail closed.
    parity_ids = {str(item.get("uuid")) for finding in report["schematic_parity"]
                  for item in finding.get("items", []) if isinstance(item, dict) and item.get("uuid")}
    result = []
    for (old, target), chosen in sorted(candidates.items()):
        all_old = [pad for pad in pads if str(pad.GetNetname()) == old]
        chosen_ids = {pad.m_Uuid.AsString() for pad in chosen}
        tracks = [item for item in board.GetTracks() if str(item.GetNetname()) == old]
        if (not explicitly_named(target) or board.FindNet(target) is None or not tracks
                or chosen_ids != {pad.m_Uuid.AsString() for pad in all_old}
                or not chosen_ids <= parity_ids or any(item.IsLocked() for item in tracks)
                or any(str(zone.GetNetname()) == old for zone in board.Zones())):
            continue
        result.append(AliasGroup(old, target, tuple(sorted(chosen_ids)),
                                 tuple(sorted(item.m_Uuid.AsString() for item in tracks))))
    return tuple(result)


def _write_bytes(path: str, data: bytes) -> bool:
    Path(path).write_bytes(data)
    return True


def reconcile(project: Path, *, apply: bool = False, progress=print) -> dict:
    """Validate aliases on a snapshot and optionally publish the checked board."""
    if not isinstance(apply, bool):
        raise ValueError("Apply selection must be boolean")
    project = project.resolve()
    source = native_path(project, ".kicad_pcb")
    cli = kicad_cli()
    if not cli.is_file():
        raise RuntimeError("KiCad CLI is required for net reconciliation")
    with Snapshot(source) as snapshot:
        schematic = (native_path(snapshot.directory, ".kicad_sch")).read_bytes()
        protected = ProtectedState.capture(snapshot.original, schematic)
        progress("Checking saved schematic and PCB connectivity")
        before_report = _drc(cli, snapshot.board_path, snapshot.directory / "alias-before.json",
                             save_refill=True, schematic_parity=True)
        before_bytes = snapshot.board_path.read_bytes()
        before_board = pcbnew.LoadBoard(str(snapshot.board_path))
        if before_board is None or protected.differences(before_bytes, schematic):
            raise RuntimeError("Could not preserve protected items in the baseline")
        before = summarize_drc(before_report, segment_identities=_segment_identities(before_bytes),
                               dangling_identities=_dangling_track_identities(before_board, before_report))
        if before["exclusions"] or before["ignored_checks"]:
            raise RuntimeError("The source has excluded or ignored checks")
        expected, _ = export_schematic_data(native_path(snapshot.directory, ".kicad_sch"))
        groups = find_groups(before_board, expected, before_report)
        if not groups:
            return {"status": "no_complete_aliases", "groups": 0}
        renamed = replace_aliases(before_bytes, groups)
        prior = snapshot.directory / "alias-prior.kicad_pcb"
        prior.write_bytes(before_bytes)
        snapshot.board_path.write_bytes(renamed)
        progress(f"Checking {len(groups)} complete net-name correction(s) on a private board")
        after_report = _drc(cli, snapshot.board_path, snapshot.directory / "alias-after.json",
                            save_refill=True, schematic_parity=True)
        candidate = snapshot.board_path.read_bytes()
        candidate_board = pcbnew.LoadBoard(str(snapshot.board_path))
        if candidate_board is None or protected.differences(candidate, schematic):
            raise RuntimeError("Net reconciliation changed protected design items")
        after = summarize_drc(after_report, segment_identities=_segment_identities(candidate),
                              dangling_identities=_dangling_track_identities(candidate_board, after_report))
        if after["exclusions"] or after["parity_findings"] != before["parity_findings"] - sum(
                len(group.pad_ids) for group in groups):
            raise RuntimeError("The expected schematic mismatches did not all resolve")
        verify_resume_reports(before, after, expected_opens=after["open_connections"], allow_open_increase=True,
                              stable_connection_items=stable_connection_items(renamed, candidate))
        _verify_resume_connectivity(snapshot.directory, renamed)
        critical = tuple(high_speed_nets((native_path(snapshot.directory, ".kicad_pro")).read_bytes(),
                                         {str(name) for name in before_board.GetNetsByName()}))
        if not critical:
            raise RuntimeError("High-speed reference nets could not be identified")
        reference = _verify_cumulative_reference_native(prior, snapshot.board_path, critical)
        if reference.get("status") == "degraded":
            raise RuntimeError("The candidate degraded sampled reference copper")
        snapshot.check()
        inputs = {str(path): digest for path, digest in snapshot.fingerprints.items()}
        tools = dict(snapshot.tool_hashes)
        original = snapshot.original
        guard = BoardSaveGuard.capture(source, snapshot.fingerprints[source.resolve()],
                                       [p for p in snapshot.fingerprints if p != source.resolve()])
        result = {"status": "checked_preview", "groups": len(groups),
                  "aliases": [(group.old, group.target) for group in groups],
                  "before_opens": before["open_connections"], "after_opens": after["open_connections"],
                  "before_parity": before["parity_findings"], "after_parity": after["parity_findings"],
                  "errors": after["errors"], "warnings": after["warnings"],
                  "reference_status": reference.get("status")}
    if not apply:
        return result
    if Snapshot.tools() != tools:
        raise RuntimeError("Routing tools changed before saving")
    guard.verify_unchanged()
    guard.verify_editor_unlocked()
    backup_dir = results_path(project, "autoroute") / "preimages"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"net-alias-{uuid4().hex}.json"
    checkpoint = encode_checkpoint(original, inputs, tools, "net-alias-v1")
    journal = {"kind": "pre_net_alias_reconciliation", "created_utc": datetime.now(timezone.utc).isoformat(),
               "manufacturing_approved": False, "board_before": checkpoint}
    with backup.open("xb") as stream:
        stream.write(json.dumps(journal, separators=(",", ":")).encode())
        stream.flush()
        os.fsync(stream.fileno())
    saved = json.loads(backup.read_bytes())["board_before"]
    if decode_checkpoint(saved, inputs, tools, "net-alias-v1") != original:
        raise RuntimeError("The recovery checkpoint did not verify")
    if Snapshot.tools() != tools:
        raise RuntimeError("Routing tools changed before saving")
    guard.save(candidate, source, _write_bytes, pcbnew.LoadBoard)
    return {**result, "status": "saved_work_in_progress", "preimage": str(backup)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="save the independently checked net-name correction")
    args = parser.parse_args(argv)
    try:
        with quiet_duplicate_image_handlers():
            result = reconcile(args.project, apply=args.apply, progress=lambda message: print(message, flush=True))
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Net reconciliation stopped: {error}", file=sys.stderr)
        return 1
    print(f"Result: {result['status']}")
    if result.get("groups"):
        print(f"Open connections: {result['before_opens']} -> {result['after_opens']}")
        print(f"Schematic mismatches: {result['before_parity']} -> {result['after_parity']}")
        print(f"Other DRC errors: {result['errors']}; warnings: {result['warnings']}")
        print("Electrical and manufacturing qualification remain incomplete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
