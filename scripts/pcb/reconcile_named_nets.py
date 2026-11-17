#!/usr/bin/env python3
"""Conservatively reconcile stale PCB pad-net aliases with the schematic.

Run this script with KiCad's bundled Python, for example:

    python scripts\\run.py check nets

The KiCad MCP update operation can leave legacy auto-generated board net names
on pads after a schematic pin is given a meaningful net label.  This utility
does not try to be a general replacement for Update PCB from Schematic.  It
only fixes a narrow, reviewable case:

* The current board net is an auto-generated ``Net-(...)`` name.
* The schematic expects one existing, explicitly named net.
* Every pad on the old board net maps unambiguously to that same target net.
* The old board net has no tracks, vias, or zones to relabel.

Anything else is reported and left untouched.  This makes it safe to use as a
pre-routing repair for legacy named-net aliases without silently changing
placement, geometry, or copper.

``--include-auto-aliases`` is deliberately opt-in.  It permits the similarly
strict case where both names are generated ``Net-(...)`` aliases: the target
must be absent, every source-net pad must map to it, and all source copper is
retagged without moving any geometry.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import pcbnew

from core.pcb_io import BoardSaveGuard


def find_kicad_cli() -> str:
    """Prefer the CLI installed beside KiCad's bundled Python."""

    adjacent = Path(sys.executable).with_name("kicad-cli.exe")
    if adjacent.is_file():
        return str(adjacent)
    cli = shutil.which("kicad-cli")
    if cli:
        return cli
    raise RuntimeError("Could not locate kicad-cli next to KiCad Python or on PATH.")


def export_schematic_data(
    schematic: Path,
) -> tuple[dict[tuple[str, str], set[str]], dict[str, str]]:
    """Return schematic pin-net expectations and component values."""

    with tempfile.TemporaryDirectory(prefix="kicad-net-reconcile-") as temp_dir:
        netlist = Path(temp_dir) / "schematic.net.xml"
        subprocess.run(
            [
                find_kicad_cli(),
                "sch",
                "export",
                "netlist",
                "--format",
                "kicadxml",
                "--output",
                str(netlist),
                str(schematic),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        root = ET.parse(netlist).getroot()

    expected: dict[tuple[str, str], set[str]] = defaultdict(set)
    for net in root.findall("./nets/net"):
        net_name = net.attrib["name"]
        for node in net.findall("node"):
            reference = node.attrib["ref"]
            if not reference.startswith("#"):
                expected[(reference, node.attrib["pin"])].add(net_name)
    values = {
        component.attrib["ref"]: component.findtext("value", default="")
        for component in root.findall("./components/comp")
    }
    return expected, values


def pad_pin_candidates(pad: pcbnew.PAD) -> set[str]:
    """Return schematic pin spellings plausibly represented by one PCB pad."""

    number = pad.GetNumber()
    candidates = {number}
    pin_function = pad.GetPinFunction()
    if pin_function:
        candidates.add(pin_function)
        suffix = f"_{number}"
        if pin_function.endswith(suffix):
            candidates.add(pin_function[: -len(suffix)])
    return {candidate for candidate in candidates if candidate}


def explicitly_named(net_name: str) -> bool:
    """Whether a net label is safe to use as a stable, intentional name."""

    return bool(net_name) and not (
        net_name.startswith("Net-(") or net_name.startswith("unconnected-(")
    )


def board_pads(board: pcbnew.BOARD) -> list[pcbnew.PAD]:
    return [pad for footprint in board.GetFootprints() for pad in footprint.Pads()]


def old_net_has_copper(board: pcbnew.BOARD, old_name: str) -> bool:
    if any(item.GetNetname() == old_name for item in board.GetTracks()):
        return True
    return any(zone.GetNetname() == old_name for zone in board.Zones())


def collect_candidates(
    board: pcbnew.BOARD,
    expected: dict[tuple[str, str], set[str]],
    include_auto_aliases: bool,
) -> tuple[dict[tuple[str, str], list[pcbnew.PAD]], int, int]:
    """Find conservative old-net -> named-net pad migrations.

    Returns candidates, ambiguous mapping count, and non-actionable mismatch
    count.  Candidates are grouped so each source net can be validated as a
    complete unit before any pad is modified.
    """

    candidates: dict[tuple[str, str], list[pcbnew.PAD]] = defaultdict(list)
    ambiguous = 0
    non_actionable = 0

    for pad in board_pads(board):
        reference = pad.GetParentFootprint().GetReference()
        expected_names: set[str] = set()
        for pin in pad_pin_candidates(pad):
            expected_names.update(expected.get((reference, pin), set()))

        current_name = pad.GetNetname()
        if not expected_names or current_name in expected_names:
            continue
        if len(expected_names) != 1:
            ambiguous += 1
            continue

        target_name = next(iter(expected_names))
        named_target = explicitly_named(target_name)
        auto_alias_target = include_auto_aliases and target_name.startswith("Net-(")
        if current_name.startswith("Net-(") and (named_target or auto_alias_target):
            candidates[(current_name, target_name)].append(pad)
        else:
            non_actionable += 1

    return candidates, ambiguous, non_actionable


def validated_groups(
    board: pcbnew.BOARD,
    candidates: dict[tuple[str, str], list[pcbnew.PAD]],
    include_auto_aliases: bool,
) -> tuple[list[tuple[str, str, list[pcbnew.PAD]]], list[str]]:
    """Accept only full pad groups whose source net has no copper objects."""

    pads_by_net: dict[str, list[pcbnew.PAD]] = defaultdict(list)
    for pad in board_pads(board):
        pads_by_net[pad.GetNetname()].append(pad)

    approved: list[tuple[str, str, list[pcbnew.PAD]]] = []
    blocked: list[str] = []
    for (old_name, target_name), pads in sorted(candidates.items()):
        target = board.FindNet(target_name)
        all_old_pads = pads_by_net[old_name]
        can_create_auto_target = (
            include_auto_aliases and target is None and target_name.startswith("Net-(")
        )
        if target is None and not can_create_auto_target:
            blocked.append(
                f"{old_name} -> {target_name}: target net is absent from the board"
            )
        elif {pad.m_Uuid.AsString() for pad in pads} != {
            pad.m_Uuid.AsString() for pad in all_old_pads
        }:
            blocked.append(
                f"{old_name} -> {target_name}: not every source-net pad maps to target"
            )
        elif old_net_has_copper(board, old_name) and not can_create_auto_target:
            blocked.append(
                f"{old_name} -> {target_name}: source net has tracks, vias, or zones"
            )
        else:
            approved.append((old_name, target_name, pads))
    return approved, blocked


def connector_value_changes(
    board: pcbnew.BOARD, expected_values: dict[str, str], align_connector_values: bool
) -> list[tuple[pcbnew.FOOTPRINT, str]]:
    """Return schematic-authoritative value updates for connectors only."""

    if not align_connector_values:
        return []
    changes: list[tuple[pcbnew.FOOTPRINT, str]] = []
    for footprint in board.GetFootprints():
        reference = footprint.GetReference()
        expected_value = expected_values.get(reference, "")
        if (
            reference.startswith("J")
            and expected_value
            and footprint.GetValue() != expected_value
        ):
            changes.append((footprint, expected_value))
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True, type=Path)
    parser.add_argument("--schematic", required=True, type=Path)
    parser.add_argument(
        "--expect-sha256", help="required source board hash when applying changes"
    )
    parser.add_argument(
        "--include-auto-aliases",
        action="store_true",
        help="retag complete legacy Net-(...) aliases only after the guarded checks pass",
    )
    parser.add_argument(
        "--align-connector-values",
        action="store_true",
        help="copy only J-reference footprint values from the schematic",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not args.board.is_file():
        parser.error(f"board does not exist: {args.board}")
    if not args.schematic.is_file():
        parser.error(f"schematic does not exist: {args.schematic}")
    if args.apply and not args.expect_sha256:
        parser.error("--apply requires --expect-sha256")
    guard = (
        BoardSaveGuard.capture(args.board, args.expect_sha256, [args.schematic])
        if args.apply
        else None
    )
    board = pcbnew.LoadBoard(str(args.board))
    expected, expected_values = export_schematic_data(args.schematic)
    candidates, ambiguous, non_actionable = collect_candidates(
        board, expected, args.include_auto_aliases
    )
    approved, blocked = validated_groups(board, candidates, args.include_auto_aliases)
    value_changes = connector_value_changes(
        board, expected_values, args.align_connector_values
    )

    for old_name, target_name, pads in approved:
        pad_names = ", ".join(
            f"{pad.GetParentFootprint().GetReference()}/{pad.GetNumber()}"
            for pad in pads
        )
        print(f"CANDIDATE {old_name} -> {target_name}: {pad_names}")
    for reason in blocked:
        print(f"BLOCKED  {reason}")
    for footprint, expected_value in value_changes:
        print(
            f"VALUE    {footprint.GetReference()}: {footprint.GetValue()} -> {expected_value}"
        )
    candidate_pad_count = sum(len(pads) for _, _, pads in approved)
    print(
        "SUMMARY "
        f"candidate_groups={len(approved)} candidate_pads={candidate_pad_count} "
        f"blocked_groups={len(blocked)} ambiguous_pads={ambiguous} "
        f"non_actionable_mismatches={non_actionable} connector_value_changes={len(value_changes)}"
    )

    if args.apply and (approved or value_changes):
        for old_name, target_name, pads in approved:
            target = board.FindNet(target_name)
            if target is None:
                target = pcbnew.NETINFO_ITEM(board, target_name)
                board.Add(target)
            for item in board.GetTracks():
                if item.GetNetname() == old_name:
                    item.SetNet(target)
            for zone in board.Zones():
                if zone.GetNetname() == old_name:
                    zone.SetNet(target)
            for pad in pads:
                pad.SetNet(target)
        for footprint, expected_value in value_changes:
            footprint.SetValue(expected_value)
        assert guard is not None
        output_sha = guard.save(board, args.board, pcbnew.SaveBoard, pcbnew.LoadBoard)
        print(f"SAVED {args.board} SHA256={output_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
