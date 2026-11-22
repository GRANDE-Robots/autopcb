"""Validate a proposed route on temporary KiCad files without publishing it."""

from __future__ import annotations

from core.project import native_path

from core.project import SNAPSHOT_ENTRY, ProjectPaths
from core.toolchain import kicad_cli

import collections
import hashlib
import json
import math
import shutil
import tempfile
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from typing import Any

from core.kicad_campaign import CampaignPlanner
from core.kicad_sexpr import Atom, Form, parse, select_direct_children
from core.native_command import run_checked
from pcb.move_scope import MoveScope
from pcb.route_native import validate_removals
from pcb.route_optimizer import COPPER_LAYERS, Point


class CandidateRejected(RuntimeError):
    """A well-formed proposal failed a connectivity check, not the runtime."""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tool_inputs() -> dict[str, str]:
    scripts = Path(__file__).resolve().parents[1]
    names = (
        "pcb/route_validation.py",
        "pcb/drc_inventory.py",
        "pcb/width_repair.py",
        "pcb/branch_widths.py",
        "pcb/silkscreen.py",
        "pcb/drc_cleanup.py",
        "pcb/freeroute_delta.py",
        "pcb/freeroute_preview.py",
        "pcb/checkpoints.py",
        "pcb/route_constraints.py",
        "pcb/route_native.py",
        "pcb/route_optimizer.py",
        "pcb/route_junctions.py",
        "pcb/net_routing/backend.py",
        "pcb/net_routing/controller.py",
        "pcb/net_routing/geometry.py",
        "pcb/net_routing/inventory.py",
        "pcb/net_routing/model.py",
        "pcb/net_routing/reference.py",
        "pcb/net_routing/rules.py",
        "pcb/net_routing/smoothing.py",
        "pcb/net_routing/style.py",
        "pcb/topology_bound.py",
        "pcb/joint_optimizer.py",
        "pcb/footprint_pose.py",
        "pcb/move_scope.py",
        "pcb/route_cli.py",
        "pcb/route_batch.py",
        "core/kicad_campaign.py",
        "core/kicad_sexpr.py",
        "core/pcb_io.py",
        "core/paths.py",
        "core/native_command.py",
    )
    result = {name: _sha(scripts / name) for name in names}
    cli = kicad_cli()
    if not cli.is_file():
        raise RuntimeError("KiCad CLI is required for route validation")
    result["kicad-cli"] = _sha(cli)
    return result


def _inputs(source: Path) -> list[Path]:
    return list(ProjectPaths(source).audit_inputs())


def _copy_inputs(files: list[Path], source: Path, destination: Path, *, names=None) -> None:
    for original in files:
        target = destination / (names[original] if names is not None else original.relative_to(source.parent))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
    (destination / SNAPSHOT_ENTRY).write_text(source.name, encoding="utf-8")


def verify_validation_evidence(
    source: Path, evidence: dict[str, Any], candidate_sha256: str
) -> None:
    """Reject routing evidence if the board, design rules or libraries changed."""
    source = source.resolve()
    if (
        not isinstance(evidence, dict)
        or evidence.get("status") != "no_new_drc_violations"
    ):
        raise RuntimeError("Routing campaign does not have a passing validation result")
    if evidence.get("candidate_sha256") != candidate_sha256:
        raise RuntimeError(
            "Routing campaign candidate differs from the validated candidate"
        )
    expected = evidence.get("input_sha256")
    if not isinstance(expected, dict):
        raise RuntimeError("Routing validation input fingerprints are missing")
    current = {
        path.relative_to(source.parent).as_posix(): _sha(path)
        for path in _inputs(source)
    }
    if current != expected:
        raise RuntimeError("Routing validation inputs changed; plan and validate again")
    if evidence.get("tool_sha256") != _tool_inputs():
        raise RuntimeError("Routing validation tools changed; plan and validate again")


def _drc_native(
    cli: Path, board: Path, output: Path, *, save_refill: bool = False,
    schematic_parity: bool = False, refill_zones: bool = True,
) -> dict[str, Any]:
    if not isinstance(schematic_parity, bool):
        raise ValueError("schematic_parity must be true or false")
    if not isinstance(refill_zones, bool) or (save_refill and not refill_zones):
        raise ValueError("Only a requested zone refill may be saved")
    command = [str(cli), "pcb", "drc", "--all-track-errors", "--severity-all"]
    if refill_zones:
        command.append("--refill-zones")
    if schematic_parity:
        command.append("--schematic-parity")
    if save_refill:
        command.append("--save-board")
    command.extend(["--format", "json", "--output", str(output), str(board)])
    completed = run_checked(command, operation="KiCad DRC", output=output)
    # KiCad can exit 0 and write an empty parity array after a netlist
    # failure. Require its explicit completion line as well as the report.
    if schematic_parity and "schematic parity issues" not in completed.stdout.casefold():
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"KiCad did not complete schematic parity: {detail}")
    report = json.loads(output.read_text(encoding="utf-8-sig"))
    if not isinstance(report.get("violations"), list) or not isinstance(
        report.get("unconnected_items"), list
    ):
        raise RuntimeError("KiCad DRC returned an incomplete report")
    if not {"error", "warning", "exclusion"} <= set(report.get("included_severities", [])):
        raise RuntimeError("KiCad DRC did not include every severity")
    if schematic_parity and not isinstance(report.get("schematic_parity"), list):
        raise RuntimeError("KiCad DRC omitted schematic parity findings")
    return report


def _drc(cli: Path, board: Path, output: Path, *, save_refill: bool = False,
         schematic_parity: bool = False) -> dict[str, Any]:
    """Run native checks, exhausting the capped width inventory on private copies."""
    from pcb.drc_inventory import expand_width_report

    report = _drc_native(cli, board, output, save_refill=save_refill,
                         schematic_parity=schematic_parity)
    complete = expand_width_report(board, report, lambda path, result: _drc_native(
        cli, path, result, schematic_parity=schematic_parity))
    if complete is not report:
        output.write_text(json.dumps(complete, indent=2, allow_nan=False), encoding='utf-8')
    return complete


FILL_FORMS = {"filled_polygon", "filled_segments", "fill_segments"}


def _canonical(item: Atom | Form) -> object:
    if isinstance(item, Atom):
        try:
            number = Decimal(item.value)
            return (
                ("number", number.normalize())
                if number.is_finite()
                else ("text", item.value)
            )
        except InvalidOperation:
            return ("text", item.value)
    return tuple(_canonical(child) for child in item.items)


def _zones(source: str) -> dict[str, Form]:
    result = {}
    for zone in parse(source).children("zone"):
        ids = zone.children("uuid")
        if len(ids) != 1 or len(ids[0].atoms()) != 2:
            raise RuntimeError("Each copper zone must have exactly one UUID")
        identifier = ids[0].atoms()[1].value
        if identifier in result:
            raise RuntimeError("Duplicate zone UUIDs prevent a safe refill")
        result[identifier] = zone
    return result


def _zone_definition(zone: Form) -> tuple[object, ...]:
    result = []
    for item in zone.items:
        if isinstance(item, Form) and item.name in FILL_FORMS:
            continue
        if isinstance(item, Form) and item.name == "fill":
            # KiCad adds the optional `yes` marker when a zone is refilled;
            # thermal and island settings must otherwise remain unchanged.
            result.append(
                tuple(
                    _canonical(child)
                    for child in item.items
                    if not (isinstance(child, Atom) and child.value == "yes")
                )
            )
        else:
            result.append(_canonical(item))
    return tuple(result)


def _zone_refill_changes(before: bytes, after: bytes) -> list[dict[str, Any]]:
    """Retain only the native refill result, never a changed zone definition."""
    old_text, new_text = before.decode("utf-8"), after.decode("utf-8")
    old, new = _zones(old_text), _zones(new_text)
    if old.keys() != new.keys():
        raise RuntimeError("KiCad changed the zone inventory during refill")
    changes = []
    for identifier, old_zone in old.items():
        new_zone = new[identifier]
        old_definition = _zone_definition(old_zone)
        new_definition = _zone_definition(new_zone)
        if old_definition != new_definition:
            raise RuntimeError(f"KiCad changed non-fill settings in zone {identifier}")
        old_fill = tuple(
            _canonical(item)
            for item in old_zone.children()
            if item.name in FILL_FORMS | {"fill"}
        )
        new_fill = tuple(
            _canonical(item)
            for item in new_zone.children()
            if item.name in FILL_FORMS | {"fill"}
        )
        if old_fill != new_fill:
            layer_forms = old_zone.children("layer") + old_zone.children("layers")
            layers = [atom.value for form in layer_forms for atom in form.atoms()[1:]]
            if not layers or any(
                layer not in {*COPPER_LAYERS, "*.Cu"} for layer in layers
            ):
                raise RuntimeError(
                    f"Refill would change a non-copper zone {identifier}; preserving silkscreen"
                )
            changes.append(
                {
                    "document": "pcb",
                    "target": {"kind": "zone", "uuid": identifier},
                    "op": "replace",
                    "path": [],
                    "expression": new_text[new_zone.start : new_zone.end],
                }
            )
    return changes


def _segment_identities(board_bytes: bytes) -> dict[str, str]:
    """Identify uniquely occurring, otherwise identical native segments.

    Replacing a segment with the exact same serialized properties must not turn
    its existing warning into a new warning merely because its UUID changed.
    Ambiguous duplicate segments deliberately retain their UUID identities.
    """
    def canonical(form: Form, *, root: bool = False) -> tuple:
        return tuple(
            ("atom", item.value) if isinstance(item, Atom) else ("form", canonical(item))
            for item in form.items
            if not (root and isinstance(item, Form) and item.name == "uuid")
        )

    entries = []
    for expression in select_direct_children(board_bytes.decode("utf-8-sig"), "segment"):
        form = parse(expression)
        identifiers = form.children("uuid")
        if len(identifiers) != 1 or len(identifiers[0].atoms()) != 2:
            raise ValueError("A native segment has no unique UUID")
        nets = form.children("net")
        if len(nets) != 1 or len(nets[0].atoms()) != 2:
            continue
        # An older numeric net code needs a separate board-level name lookup;
        # do not equate codes across boards without that evidence.
        if nets[0].atoms()[1].value.isdecimal():
            continue
        identifier = identifiers[0].atoms()[1].value
        signature = hashlib.sha256(repr(canonical(form, root=True)).encode("utf-8")).hexdigest()
        entries.append((identifier, signature))
    counts = collections.Counter(signature for _, signature in entries)
    return {
        identifier: f"segment:{signature}"
        for identifier, signature in entries if counts[signature] == 1
    }


def stable_connection_items(before: bytes, after: bytes) -> dict[str, str]:
    """Prove items belong to a net with unchanged copper and footprint pads.

    Nets with zones or unnamed/numeric net identities are excluded. This is
    evidence only for comparing alternate KiCad connection-width anchors.
    """
    def inventory(data: bytes):
        text = data.decode("utf-8-sig")
        segment_names = _segment_identities(data)
        copper = collections.defaultdict(collections.Counter)
        pads = collections.defaultdict(collections.Counter)
        items = {}
        zones = set()

        def one(form, name):
            children = form.children(name)
            if len(children) != 1 or len(children[0].atoms()) != 2:
                return ""
            return children[0].atoms()[1].value

        def add(identifier, net):
            if not identifier or identifier in items:
                raise ValueError("Duplicate or unidentified copper item")
            items[identifier] = net

        for expression in select_direct_children(text, "zone"):
            net = one(parse(expression), "net")
            if net:
                zones.add(net)
        for kind in ("segment", "arc", "via"):
            for expression in select_direct_children(text, kind):
                form = parse(expression)
                net, uid = one(form, "net"), one(form, "uuid")
                if not net or net.isdecimal():
                    continue
                copper[net][_canonical(form)] += 1
                add(segment_names.get(uid, uid) if kind == "segment" else uid, net)
        for expression in select_direct_children(text, "footprint"):
            footprint = parse(expression)
            signature = _canonical(footprint)
            for pad in footprint.children("pad"):
                net, uid = one(pad, "net"), one(pad, "uuid")
                if not net or net.isdecimal():
                    continue
                pads[net][(uid, signature)] += 1
                add(uid, net)
        return copper, pads, items, zones

    old_copper, old_pads, old_items, old_zones = inventory(before)
    new_copper, new_pads, new_items, new_zones = inventory(after)
    nets = set(old_copper) | set(old_pads)
    stable = {net for net in nets if net not in old_zones | new_zones
              and old_copper[net] == new_copper[net]
              and old_pads[net] == new_pads[net]}
    return {key: net for key, net in old_items.items()
            if net in stable and new_items.get(key) == net}


def added_violation_keys(old, new, stable_items: dict[str, str] | None = None):
    """Compare native findings, proving relocated width markers net by net.

    Multiplicity, severity and rule type remain exact. Only disjoint anchor
    pairs on the same completely unchanged copper-and-pad net may be paired.
    """
    old, new = old.copy(), new.copy()
    if stable_items:
        for added, count in list((new - old).items()):
            if added[:2] != ("connection_width", "warning") or len(added[2]) != 2:
                continue
            nets = {stable_items.get(item) for item in added[2]}
            if len(nets) != 1 or None in nets:
                continue
            for _ in range(count):
                for removed in sorted((old - new).elements()):
                    if (removed[:2] == added[:2] and len(removed[2]) == 2
                            and {stable_items.get(item) for item in removed[2]} == nets):
                        old[removed] -= 1
                        new[added] -= 1
                        break
    return new - old


def _dangling_track_identities(board, report: dict, *, rebuild: bool = True) -> dict[str, tuple[str, ...]]:
    """Ask KiCad about both ends of each reported dangling straight track."""
    import pcbnew

    from core.pcb_io import copper_width

    connectivity = board.GetConnectivity()
    if rebuild:
        connectivity.Build(board)
    tracks = {
        item.m_Uuid.AsString(): item for item in board.GetTracks()
        if isinstance(item, pcbnew.PCB_TRACK)
        and not isinstance(item, (pcbnew.PCB_VIA, pcbnew.PCB_ARC))
    }
    result = {}
    reversed_any = False
    try:
        for finding in report["violations"]:
            parts = finding.get("items", ())
            if finding.get("type") != "track_dangling" or len(parts) != 1:
                continue
            identifier = parts[0].get("uuid", "")
            track = tracks.get(identifier)
            if track is None or not track.GetNetname() or identifier in result:
                continue
            # GetStart/GetEnd may return references into the native object.
            # Copy coordinates before swapping, and always restore the object.
            start = track.GetStart()
            end = track.GetEnd()
            first = (int(start.x), int(start.y))
            last = (int(end.x), int(end.y))
            position = pcbnew.VECTOR2I()
            if not connectivity.TestTrackEndpointDangling(track, True, position):
                continue
            positions = {(int(position.x), int(position.y))}
            try:
                reversed_any = True
                track.SetStart(pcbnew.VECTOR2I(*last))
                track.SetEnd(pcbnew.VECTOR2I(*first))
                position = pcbnew.VECTOR2I()
                if not connectivity.TestTrackEndpointDangling(track, True, position):
                    continue
                positions.add((int(position.x), int(position.y)))
            finally:
                track.SetStart(pcbnew.VECTOR2I(*first))
                track.SetEnd(pcbnew.VECTOR2I(*last))
            endpoints = []
            for point in positions:
                if point == first:
                    other = last
                elif point == last:
                    other = first
                else:
                    endpoints = []
                    break
                dx, dy = other[0] - point[0], other[1] - point[1]
                divisor = math.gcd(abs(dx), abs(dy))
                if not divisor:
                    endpoints = []
                    break
                fields = [
                    track.GetNetname(), board.GetLayerName(track.GetLayer()),
                    *point, int(copper_width(track)), dx // divisor, dy // divisor,
                ]
                endpoints.append("dangling:" + json.dumps(
                    fields, ensure_ascii=False, separators=(",", ":")
                ))
            if endpoints:
                result[identifier] = tuple(sorted(set(endpoints)))
    finally:
        if reversed_any:
            connectivity.Build(board)
    return result


def _dangling_summary(report: dict, identities: dict[str, tuple[str, ...]]) -> list | None:
    """Return complete per-warning endpoint evidence, or request strict fallback."""
    summary = []
    for finding in report["violations"]:
        if finding.get("type") != "track_dangling":
            continue
        parts = finding.get("items", ())
        if len(parts) != 1 or not isinstance(parts[0], dict):
            return None
        endpoints = identities.get(parts[0].get("uuid", ""))
        if not endpoints:
            return None
        summary.append([finding.get("severity", ""), list(endpoints)])
    return summary


def _new_dangling_indices(baseline: list, candidate: list) -> list[int]:
    """Match endpoint and warning multisets; never hide a newly dangling end."""
    def counters(entries):
        counts, endpoints = collections.Counter(), collections.Counter()
        for entry in entries:
            if (not isinstance(entry, (list, tuple)) or len(entry) != 2
                    or entry[0] not in {"error", "warning", "exclusion"}
                    or not isinstance(entry[1], (list, tuple))
                    or not entry[1] or len(set(entry[1])) != len(entry[1])
                    or any(not isinstance(value, str) or not value for value in entry[1])):
                raise ValueError("Invalid dangling-track evidence")
            counts[entry[0]] += 1
            endpoints.update((entry[0], value) for value in entry[1])
        return counts, endpoints

    counts, endpoints = counters(baseline)
    counters(candidate)  # Validate the complete candidate before comparing.
    new = []
    for index, (severity, values) in enumerate(candidate):
        changed = counts[severity] <= 0
        counts[severity] -= 1
        for value in values:
            key = severity, value
            changed |= endpoints[key] <= 0
            endpoints[key] -= 1
        if changed:
            new.append(index)
    return new


def _key(violation: dict[str, Any], identities: dict[str, str] | None = None) -> tuple[str, str, tuple[str, ...]]:
    identities = identities or {}
    return (
        violation.get("type", ""),
        violation.get("severity", ""),
        tuple(sorted(identities.get(item.get("uuid", ""), item.get("uuid", ""))
                     for item in violation.get("items", []))),
    )


def _anchors(board, point: Point, layer: str, net: str) -> set[str]:
    import pcbnew

    layer_id = board.GetLayerID(layer)
    coordinate = pcbnew.VECTOR2I(point.x, point.y)
    items = list(board.GetPads()) + list(board.GetTracks())
    return {
        item.m_Uuid.AsString()
        for item in items
        if item.GetNetname() == net
        and item.IsOnLayer(layer_id)
        and item.HitTest(coordinate)
    }


def _connected(
    board, starts: set[str], targets: set[str], *, rebuild: bool = True
) -> bool:
    items = list(board.GetPads()) + list(board.GetTracks())
    by_uuid = {item.m_Uuid.AsString(): item for item in items}
    connectivity = board.GetConnectivity()
    if rebuild:
        connectivity.Build(board)
    for uid in starts:
        if uid in by_uuid:
            linked = {
                item.m_Uuid.AsString()
                for item in connectivity.GetConnectedItems(by_uuid[uid])
            }
            linked.add(uid)
            if linked.intersection(targets):
                return True
    return False


def _net_identities(board) -> dict[str, str]:
    """Capture effective native net assignments, including dangling tracks."""
    identities: dict[str, str] = {}
    for item in list(board.GetPads()) + list(board.GetTracks()):
        identifier = item.m_Uuid.AsString()
        if identifier in identities:
            raise RuntimeError("Duplicate copper UUIDs prevent safe route validation")
        identities[identifier] = item.GetNetname()
    return identities


def _broken_existing_connections(
    original, routed, removed: set[str]
) -> list[dict[str, Any]]:
    """Check that each pre-existing connected copper island stays connected."""
    old_items = {
        item.m_Uuid.AsString(): item
        for item in list(original.GetPads()) + list(original.GetTracks())
        if item.m_Uuid.AsString() not in removed and item.GetNetname()
    }
    new_items = {
        item.m_Uuid.AsString(): item
        for item in list(routed.GetPads()) + list(routed.GetTracks())
    }
    old_connectivity = original.GetConnectivity()
    new_connectivity = routed.GetConnectivity()
    pending = set(old_items)
    failures: list[dict[str, Any]] = []
    while pending:
        representative = min(pending)
        item = old_items[representative]
        old_linked = {
            part.m_Uuid.AsString() for part in old_connectivity.GetConnectedItems(item)
        }
        old_linked.add(representative)
        # Compare only same-net surviving items; foreign-net contacts are
        # separately detected by DRC and effective net-identity checks.
        group = {
            identifier
            for identifier in old_linked
            if identifier in old_items
            and old_items[identifier].GetNetname() == item.GetNetname()
        }
        pending.difference_update(group)
        if len(group) < 2 or representative not in new_items:
            continue
        new_linked = {
            part.m_Uuid.AsString()
            for part in new_connectivity.GetConnectedItems(new_items[representative])
        }
        new_linked.add(representative)
        missing = sorted(group - new_linked)
        if missing:
            failures.append(
                {
                    "net": item.GetNetname(),
                    "representative": representative,
                    "disconnected_items": missing,
                }
            )
    return failures


@dataclass(frozen=True)
class RouteContract:
    net: str
    start: Point
    start_layer: str
    end: Point
    end_layer: str
    removals: tuple[dict[str, str], ...] = ()


def validate_route(
    source: Path,
    source_sha256: str,
    changes: list[dict[str, Any]],
    net: str,
    start: Point,
    start_layer: str,
    end: Point,
    end_layer: str,
) -> dict[str, Any]:
    """Validate one route using the same batch verification workflow."""
    removals = tuple(c["target"] for c in changes if c.get("op") == "remove")
    return validate_routes(
        source,
        source_sha256,
        changes,
        [RouteContract(net, start, start_layer, end, end_layer, removals)],
    )


def validate_routes(
    source: Path,
    source_sha256: str,
    changes: list[dict[str, Any]],
    contracts: list[RouteContract],
    *,
    footprint_moves: tuple[str, ...] = (),
    move_scope: MoveScope = MoveScope(),
    require_full_nets: bool = False,
) -> dict[str, Any]:
    """DRC a route batch against the same saved baseline and check endpoints.

    This function creates no project files and never applies the candidate.
    An unchanged DRC result is not a certification of the whole board.
    """
    import pcbnew

    source = source.resolve()
    if not isinstance(require_full_nets, bool):
        raise ValueError("require_full_nets must be a boolean")
    if not source.is_file() or _sha(source) != source_sha256:
        raise RuntimeError(
            "Saved board changed after planning; generate a fresh proposal"
        )
    if not changes or not contracts or any(not contract.net for contract in contracts):
        raise ValueError("Routing campaign needs copper changes and route contracts")
    requested_nets = {contract.net for contract in contracts}
    requested_ids: dict[str, str] = {}
    removals: list[dict[str, str]] = []
    permitted_moves = set(footprint_moves)
    if len(permitted_moves) != len(footprint_moves) or any(
        not isinstance(reference, str) or not reference
        for reference in footprint_moves
    ):
        raise ValueError("Footprint move permissions must be distinct references")
    moved: set[str] = set()
    for change in changes:
        if change.get("document") != "pcb":
            raise ValueError("Routing campaigns may only edit the PCB")
        if (
            change.get("op") == "replace"
            and isinstance(change.get("target"), dict)
            and (change["target"].get("kind") == "footprint")
        ):
            from pcb.footprint_pose import verify_native_pose_change

            reference = change["target"].get("reference")
            if reference not in permitted_moves or reference in moved:
                raise ValueError(
                    "Only explicitly permitted footprint poses are allowed"
                )
            verify_native_pose_change(source, change, move_scope)
            moved.add(reference)
            continue
        if change.get("op") == "set_at":
            target = change.get("target")
            reference = target.get("reference") if isinstance(target, dict) else None
            if (
                set(change) != {"document", "target", "op", "x", "y"}
                or target != {"kind": "footprint", "reference": reference}
                or reference not in permitted_moves
                or reference in moved
            ):
                raise ValueError(
                    "Only explicitly permitted footprint translations are allowed"
                )
            moved.add(reference)
            continue
        if change.get("path") != []:
            raise ValueError("Routing campaigns may only edit top-level PCB copper")
        if change.get("op") == "remove":
            target = change.get("target")
            if not isinstance(target, dict) or set(target) != {"kind", "uuid"}:
                raise ValueError("Copper removals need a kind and UUID")
            removals.append(target)
            continue
        if change.get("op") != "insert" or change.get("target") != {"kind": "root"}:
            raise ValueError("Only ordinary copper additions and removals are allowed")
        form = parse(change["expression"])
        if form.name not in {"segment", "via"} or form.children("locked"):
            raise ValueError("New routing items must be unlocked segments or vias")
        nets = form.children("net")
        if len(nets) != 1 or len(nets[0].atoms()) != 2:
            raise ValueError("New routing items must use a requested net")
        item_net = nets[0].atoms()[1].value
        if item_net not in requested_nets:
            raise ValueError("New routing items must use a requested net")
        ids = form.children("uuid")
        if len(ids) != 1 or len(ids[0].atoms()) != 2:
            raise ValueError("Every proposed segment and via needs one UUID")
        requested_ids[ids[0].atoms()[1].value] = item_net
    if moved != permitted_moves:
        raise ValueError(
            "Every permitted footprint must have exactly one pose change"
        )
    if len(requested_ids) != len(changes) - len(removals) - len(moved):
        raise ValueError("Proposed copper UUIDs must be unique")
    files = _inputs(source)
    fingerprints = {path: _sha(path) for path in files}
    tool_fingerprints = _tool_inputs()
    if fingerprints[source] != source_sha256:
        raise RuntimeError("Saved board changed while capturing validation inputs")
    cli = kicad_cli()
    if not cli.is_file():
        raise RuntimeError("KiCad CLI is required for route validation")
    with tempfile.TemporaryDirectory(prefix="jessie-route-validation-") as directory:
        root = Path(directory)
        before, after = root / "before", root / "after"
        before.mkdir()
        after.mkdir()
        _copy_inputs(files, source, before)
        _copy_inputs(files, source, after)
        if (
            _sha(native_path(before, ".kicad_pcb")) != source_sha256
            or _sha(native_path(after, ".kicad_pcb")) != source_sha256
        ):
            raise RuntimeError("Saved board changed while copying validation inputs")
        original_bytes = (native_path(before, ".kicad_pcb")).read_bytes()
        original = pcbnew.LoadBoard(str(native_path(before, ".kicad_pcb")))
        move_scope.check(original_bytes.decode("utf-8"), moved)
        requested_removals = [
            removal for contract in contracts for removal in contract.removals
        ]
        if sorted((r["kind"], r["uuid"]) for r in requested_removals) != sorted(
            (r["kind"], r["uuid"]) for r in removals
        ):
            raise ValueError("Removal changes must match the route contracts")
        removed_ids: set[str] = set()
        for contract in contracts:
            selected = validate_removals(
                original, list(contract.removals), contract.net
            )
            if selected.intersection(removed_ids):
                raise ValueError("A copper item cannot be removed by multiple routes")
            removed_ids.update(selected)
        candidate = CampaignPlanner(after).plan("pcb", changes)
        (native_path(after, ".kicad_pcb")).write_bytes(candidate.after)
        # Both boards are refilled and saved only inside the temporary folder.
        # The candidate's changed fill cache is included in the resulting
        # campaign, so applying it cannot leave stale copper behind.
        check_parity = (native_path(before, ".kicad_sch")).is_file()
        baseline = _drc(
            cli, native_path(before, ".kicad_pcb"), before / "drc.json",
            save_refill=True, schematic_parity=check_parity,
        )
        tested = _drc(
            cli, native_path(after, ".kicad_pcb"), after / "drc.json",
            save_refill=True, schematic_parity=check_parity,
        )
        zone_changes = _zone_refill_changes(
            candidate.after, (native_path(after, ".kicad_pcb")).read_bytes()
        )
        if zone_changes:
            (native_path(after, ".kicad_pcb")).write_bytes(original_bytes)
            candidate = CampaignPlanner(after).plan("pcb", [*changes, *zone_changes])
            (native_path(after, ".kicad_pcb")).write_bytes(candidate.after)
            tested = _drc(
                cli, native_path(after, ".kicad_pcb"), after / "final_drc.json",
                schematic_parity=check_parity,
            )
        original = pcbnew.LoadBoard(str(native_path(before, ".kicad_pcb")))
        original.GetConnectivity().Build(original)
        original_nets = _net_identities(original)
        routed = pcbnew.LoadBoard(str(native_path(after, ".kicad_pcb")))
        routed.GetConnectivity().Build(routed)
        anchors = []
        for contract in contracts:
            old_starts = _anchors(original, contract.start, contract.start_layer, contract.net)
            old_targets = _anchors(original, contract.end, contract.end_layer, contract.net)
            if not old_starts or not old_targets:
                raise CandidateRejected(
                    f"A {contract.net} endpoint does not touch existing copper on the requested net and layer"
                )
            # Junction insertion replaces a track UUID with equivalent split
            # tracks. Bind that endpoint to its replacement copper at the exact
            # original coordinate; the independent island check below still
            # requires all pre-existing surviving connectivity to be restored.
            starts = old_starts - removed_ids or _anchors(routed, contract.start, contract.start_layer, contract.net)
            targets = old_targets - removed_ids or _anchors(routed, contract.end, contract.end_layer, contract.net)
            if not starts or not targets:
                raise CandidateRejected(f"A {contract.net} endpoint lost its replacement copper")
            anchors.append((starts, targets))
        if not all(
            _connected(routed, starts, targets, rebuild=False)
            for starts, targets in anchors
        ):
            raise CandidateRejected(
                "The proposed trace and vias do not connect the requested endpoints"
            )
        if require_full_nets:
            from pcb.net_routing.inventory import inventory_board

            inventory = {net.name: net for net in inventory_board(routed).nets}
            incomplete = sorted(
                name for name in requested_nets
                if name not in inventory or len(inventory[name].copper_islands) != 1
            )
            if incomplete:
                raise CandidateRejected(
                    f"Requested net still has disconnected copper islands: {', '.join(incomplete)}"
                )
        routed_nets = _net_identities(routed)
        if (
            routed_nets.keys() - original_nets.keys() != requested_ids.keys()
            or original_nets.keys() - routed_nets.keys() != removed_ids
        ):
            raise RuntimeError(
                "KiCad did not retain exactly the proposed track and via UUIDs"
            )
        changed_nets = [
            {
                "uuid": identifier,
                "before": previous,
                "after": routed_nets.get(identifier),
            }
            for identifier, previous in original_nets.items()
            if identifier not in removed_ids and routed_nets.get(identifier) != previous
        ]
        for identifier, item_net in requested_ids.items():
            if routed_nets[identifier] != item_net:
                changed_nets.append(
                    {
                        "uuid": identifier,
                        "before": item_net,
                        "after": routed_nets[identifier],
                    }
                )
        broken_islands = _broken_existing_connections(original, routed, removed_ids)
        from pcb.net_routing.controller import new_style_issues
        from pcb.net_routing.style import audit_style, normalize_segment_identities

        old_segment_ids = _segment_identities(original_bytes)
        new_segment_ids = _segment_identities((native_path(after, ".kicad_pcb")).read_bytes())
        new_style = new_style_issues(
            normalize_segment_identities(audit_style(original), old_segment_ids),
            normalize_segment_identities(audit_style(routed), new_segment_ids),
        )
        from pcb.net_routing.via_guard import via_pad_overlaps

        new_via_pad = sorted(set(via_pad_overlaps(routed)) - set(via_pad_overlaps(original)))
        reference_comparison = {"status": "project_rules_missing", "degraded_track_ids": [], "unverified_track_ids": []}
        rules = native_path(before, ".kicad_pro")
        if rules.is_file():
            from pcb.net_routing.reference import comparable_missing_samples, compare_reference, high_speed_nets, sample_reference

            board_nets = {item.GetNetname() for item in (*original.GetPads(), *original.GetTracks(),
                          *routed.GetPads(), *routed.GetTracks()) if item.GetNetname()}
            critical = high_speed_nets(rules.read_bytes(), board_nets)
            if critical:
                previous_reference = sample_reference(original, critical)
                current_reference = sample_reference(routed, critical)
                reference_comparison = asdict(compare_reference(previous_reference, current_reference))
                missing = comparable_missing_samples(previous_reference, current_reference)
                reference_comparison["comparable_missing_samples"] = list(missing) if missing is not None else None
            else:
                reference_comparison["status"] = "no_high_speed_classification"
        old_dangling = _dangling_track_identities(original, baseline, rebuild=False)
        new_dangling = _dangling_track_identities(routed, tested, rebuild=False)
        old_ends = _dangling_summary(baseline, old_dangling)
        new_ends = _dangling_summary(tested, new_dangling)
        compare_ends = old_ends is not None and new_ends is not None
        unmatched_ends = set(_new_dangling_indices(old_ends, new_ends)) if compare_ends else set()
        old = collections.Counter(
            _key(item, old_segment_ids) for item in baseline["violations"]
            if not (compare_ends and item.get("type") == "track_dangling")
        )
        new = collections.Counter(
            _key(item, new_segment_ids) for item in tested["violations"]
            if not (compare_ends and item.get("type") == "track_dangling")
        )
        added_keys = added_violation_keys(old, new, stable_connection_items(original_bytes, candidate.after))
        added = []
        dangling_index = 0
        for item in tested["violations"]:
            if compare_ends and item.get("type") == "track_dangling":
                is_added = dangling_index in unmatched_ends
                dangling_index += 1
            else:
                key = _key(item, new_segment_ids)
                is_added = bool(added_keys[key])
                if is_added:
                    added_keys[key] -= 1
            if is_added:
                added.append(
                    {
                        "type": item.get("type"),
                        "severity": item.get("severity"),
                        "description": item.get("description"),
                        "items": [
                            {
                                key: part[key]
                                for key in ("uuid", "description", "pos")
                                if key in part
                            }
                            for part in item.get("items", [])
                            if isinstance(part, dict)
                        ],
                    }
                )
        added_parity_keys = (
            collections.Counter(_key(item) for item in tested["schematic_parity"])
            - collections.Counter(_key(item) for item in baseline["schematic_parity"])
            if check_parity else collections.Counter()
        )
        added_parity = []
        if check_parity:
            for item in tested["schematic_parity"]:
                key = _key(item)
                if added_parity_keys[key]:
                    added_parity.append(item)
                    added_parity_keys[key] -= 1
        before_count = len(baseline["unconnected_items"])
        after_count = len(tested["unconnected_items"])
        ignored_before = baseline.get("ignored_checks", [])
        ignored_after = tested.get("ignored_checks", [])
        if ignored_before != ignored_after:
            raise RuntimeError(
                "DRC ignored-check settings changed between baseline and candidate"
            )
        candidate_hash = candidate.result_sha256
    for path, expected in fingerprints.items():
        if not path.is_file() or _sha(path) != expected:
            raise RuntimeError(f"Routing input changed during validation: {path}")
    if _tool_inputs() != tool_fingerprints:
        raise RuntimeError("Routing validation tools changed during verification")
    return {
        "status": (
            "rejected"
            if added or added_parity or changed_nets or broken_islands or new_style or new_via_pad or after_count > before_count
            or reference_comparison["status"] == "degraded"
            else "no_new_drc_violations"
        ),
        "candidate_sha256": candidate_hash,
        "input_sha256": {
            path.relative_to(source.parent).as_posix(): value
            for path, value in fingerprints.items()
        },
        "tool_sha256": tool_fingerprints,
        "endpoint_connectivity": "connected",
        **({"whole_net_copper_connectivity": "all_requested_copper_connected"} if require_full_nets else {}),
        "routes_validated": len(contracts),
        "footprint_moves": sorted(moved),
        "baseline_violations": len(baseline["violations"]),
        "candidate_violations": len(tested["violations"]),
        "candidate_errors": sum(v.get("severity") == "error" for v in tested["violations"]),
        "candidate_warnings": sum(v.get("severity") == "warning" for v in tested["violations"]),
        "schematic_parity_checked": check_parity,
        "candidate_schematic_parity": len(tested["schematic_parity"]) if check_parity else None,
        "new_schematic_parity": added_parity,
        "baseline_unconnected": before_count,
        "candidate_unconnected": after_count,
        "ignored_checks": ignored_after,
        "new_violations": added,
        "net_identity_changes": changed_nets,
        "broken_existing_connections": broken_islands,
        "new_style_issues": list(new_style),
        "new_via_in_pad": [f"{item.reference}.{item.pad_number}" for item in new_via_pad],
        "reference_comparison": reference_comparison,
        "refilled_zone_count": len(zone_changes),
        "_zone_refill_changes": zone_changes,
        "note": "Snapshot DRC and connectivity are not electrical, physical, or manufacturing approval.",
    }
