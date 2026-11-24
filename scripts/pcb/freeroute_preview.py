"""Generate a disposable Freerouting candidate from the saved PCB.

This command never applies the candidate. Freerouting may rip up existing
copper, ignore KiCad custom rules, or disturb reference planes; independently
validate any individual proposed change before considering it for the board.
"""

from __future__ import annotations

from core.project import native_path

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from itertools import product
from pathlib import Path

from core.toolchain import kicad_cli
from typing import Any

from core.kicad_sexpr import parse, quote
from pcb.net_routing.prepare import summarize_drc
from pcb.route_validation import _copy_inputs, _drc as _native_drc
from core.toolchain import RouterCache
from core.compat import strict_zip

FASTROUTE_014_SHA256 = (
    "764d6289173d5a3c66d34bc1c1d8e3e1ceea6121a78c51c7738714ddae23f1e5"
)


def _java_executable(value: str) -> str:
    resolved = shutil.which(value)
    if not resolved:
        raise ValueError("Java was not found; supply the path to the java executable")
    path = Path(resolved)
    if path.suffix.lower() in {".cmd", ".bat"}:
        # A common Windows shim is just an executable path followed by %*.
        # Do not execute or interpret a more complicated batch file.
        text = path.read_text(encoding="utf-8-sig")
        match = re.fullmatch(
            r'\s*@echo\s+off\s*\r?\n\s*"([^"\r\n]+)"\s+%\*\s*', text, re.I
        )
        if not match or not Path(match.group(1)).is_file():
            raise ValueError(
                "Java is a batch wrapper; supply the path to the java executable"
            )
        path = Path(match.group(1))
    return str(path)


def routing_pass_budget(timeout: int) -> int:
    """Permit bounded router negotiation inside the existing wall-time cap."""
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise ValueError("The router timeout must be a positive integer")
    return max(1, min(8, timeout // 15))


def _required_java(jar: Path) -> int:
    """Read the router's entry-point class version without executing the jar."""
    try:
        with zipfile.ZipFile(jar) as archive:
            header = archive.read("app/freerouting/Freerouting.class")[:8]
    except (OSError, KeyError, zipfile.BadZipFile) as error:
        raise ValueError(
            "The Freerouting JAR has no readable entry-point class"
        ) from error
    if len(header) != 8 or header[:4] != bytes.fromhex("cafebabe"):
        raise ValueError("The Freerouting entry-point class has an invalid header")
    version = int.from_bytes(header[6:8], "big") - 44
    if version < 1:
        raise ValueError("The Freerouting JAR has an invalid Java version")
    return version


def _java_candidates() -> list[str]:
    candidates = []
    if os.environ.get("JAVA_HOME"):
        candidates.append(
            str(
                Path(os.environ["JAVA_HOME"])
                / "bin"
                / ("java.exe" if os.name == "nt" else "java")
            )
        )
    if shutil.which("java"):
        candidates.append("java")
    private = RouterCache.root()
    if private.is_dir():
        name = "java.exe" if os.name == "nt" else "java"
        candidates.extend(str(path) for path in private.glob(f"java*/**/bin/{name}"))
    return list(dict.fromkeys(candidates))


def _java_version(executable: str) -> int:
    result = subprocess.run(
        [executable, "-version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    match = re.search(
        r'(?:version\s+")?(?:openjdk\s+)?(1\.)?(\d+)(?:[.\s"\-]|$)',
        result.stderr + "\n" + result.stdout,
        re.I,
    )
    if result.returncode or not match:
        raise ValueError(f"Could not determine the Java version for {executable}")
    return int(match.group(2))


def _select_java(value: str, jar: Path) -> str:
    required = _required_java(jar)
    if value != "auto":
        executable = _java_executable(value)
        available = _java_version(executable)
        if available < required:
            raise ValueError(
                f"Freerouting needs Java {required} or newer; {executable} is Java {available}"
            )
        return executable
    for candidate in _java_candidates():
        try:
            executable = _java_executable(candidate)
            if _java_version(executable) >= required:
                return executable
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    raise ValueError(
        f"Freerouting needs Java {required} or newer; install it or pass --java"
    )


def _fingerprints(files: list[Path], root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    }


def _source_files(board: Path) -> list[Path]:
    from core.project import ProjectPaths

    paths = ProjectPaths(board)
    if not all(path.is_file() for path in paths.sources):
        raise ValueError(
            "Freerouting needs a saved PCB, schematic, KiCad project and custom rules"
        )
    return list(paths.audit_inputs())


def _dsn_scope(text: str, name: str):
    """Read a Specctra scope without parsing its special string-quote directive."""
    # Specctra's '(string_quote ")' directive is not a KiCad quoted
    # string. Parse just the structure scope, where ordinary strings occur.
    matches = list(re.finditer(r"\(" + re.escape(name) + r"(?=\s|\()", text))
    if len(matches) != 1:
        raise ValueError(f"The exported DSN does not contain one {name} scope")
    start = matches[0].start()
    depth = 0
    quoted = False
    escaped = False
    end = None
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end is None:
        raise ValueError(f"The exported DSN has an unclosed {name} scope")
    form = parse(text[start:end])
    if form.name != name:
        raise ValueError(f"The exported DSN has an invalid {name} scope")
    return start, form


def _native_zone_keys(board: Any) -> set[tuple[str, str]]:
    """Identify real copper zones, excluding rule areas and unassigned zones."""
    layers = tuple(board.GetEnabledLayers().CuStack())
    return {
        (str(zone.GetNetname()), str(board.GetLayerName(layer)))
        for zone in board.Zones()
        if not zone.GetIsRuleArea() and zone.GetNetname()
        for layer in layers
        if zone.IsOnLayer(layer)
    }


def _strip_zone_planes(
    design: Path, zone_keys: set[tuple[str, str]]
) -> tuple[tuple[str, str], ...]:
    """Avoid assuming a KiCad signal-layer zone is an uninterrupted plane.

    Only the temporary DSN changes. KiCad refills the original zones and the
    native checks must still validate connectivity, clearances and references.
    """
    text = design.read_text(encoding="utf-8")
    start, structure = _dsn_scope(text, "structure")
    layers: dict[str, str] = {}
    for layer in structure.children("layer"):
        atoms, kinds = layer.atoms(), layer.children("type")
        if len(atoms) != 2 or len(kinds) != 1 or len(kinds[0].atoms()) != 2:
            raise ValueError("The exported DSN has an invalid copper layer")
        name, kind = atoms[1].value, kinds[0].atoms()[1].value
        if name in layers:
            raise ValueError("The exported DSN has duplicate layers")
        layers[name] = kind
    spans = []
    removed = []
    for plane in structure.children("plane"):
        atoms, shapes = plane.atoms(), plane.children()
        if (
            len(atoms) != 2
            or len(shapes) != 1
            or shapes[0].name not in {"polygon", "rect", "path"}
            or len(shapes[0].atoms()) < 2
        ):
            raise ValueError("Cannot safely interpret an exported DSN plane")
        key = (atoms[1].value, shapes[0].atoms()[1].value)
        if key[1] not in layers:
            raise ValueError("The exported DSN plane refers to an unknown layer")
        if key in zone_keys and layers[key[1]] == "signal":
            spans.append((start + plane.start, start + plane.end))
            removed.append(key)
    for begin, end in reversed(spans):
        text = text[:begin] + text[end:]
    if spans:
        design.write_text(text, encoding="utf-8")
    return tuple(removed)


def _set_equal_layer_costs(design: Path, *, optimizer: bool = False) -> tuple[str, ...]:
    """Set temporary DSN routing costs and the requested optimizer mode."""
    if not isinstance(optimizer, bool):
        raise ValueError("Optimizer selection must be true or false")
    text = design.read_text(encoding="utf-8")
    start, structure = _dsn_scope(text, "structure")
    if structure.children("autoroute_settings"):
        raise ValueError("The exported DSN already contains router settings")
    layers = structure.children("layer")
    if not layers:
        raise ValueError("The exported DSN contains no routing layers")
    names = []
    for layer in layers:
        atoms = layer.atoms()
        kind = layer.children("type")
        if len(atoms) != 2 or len(kind) != 1 or len(kind[0].atoms()) != 2:
            raise ValueError("The exported DSN has an invalid copper layer")
        if kind[0].atoms()[1].value != "signal":
            raise ValueError(f"The exported DSN marks {atoms[1].value} as non-routing")
        names.append(atoms[1].value)
    if len(set(names)) != len(names):
        raise ValueError("The exported DSN has duplicate layers")
    local_after = layers[-1].end
    if any(
        item.start < local_after
        for name in ("plane", "keepout", "via_keepout", "place_keepout")
        for item in structure.children(name)
    ):
        raise ValueError("The exported DSN defines obstacles before all copper layers")
    after = start + local_after
    angle = structure.children("snap_angle")
    if angle and (
        len(angle) != 1
        or len(angle[0].atoms()) != 2
        or angle[0].atoms()[1].value != "fortyfive_degree"
    ):
        raise ValueError("The exported DSN has incompatible snap-angle settings")
    rules = [
        f"    (layer_rule {quote(name)} (active on) (preferred_direction horizontal) "
        "(preferred_direction_trace_costs 1.0) (against_preferred_direction_trace_costs 1.0))"
        for name in names
    ]
    postroute = "on" if optimizer else "off"
    settings = (
        f"\n    (autoroute_settings (autoroute on) (postroute {postroute}) (vias on) "
        "(via_costs 10) (plane_via_costs 10)\n" + "\n".join(rules) + ")\n"
    )
    if not angle:
        settings += "    (snap_angle fortyfive_degree)\n"
    design.write_text(text[:after] + settings + text[after:], encoding="utf-8")
    return tuple(names)


def _prepare_router_dsn(
    board: Any,
    design: Path,
    *,
    optimizer: bool,
    ground_contact_margin_mm: float = 0.5,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Apply the same conservative temporary export to every router path."""
    import pcbnew

    from pcb.net_routing.export_reservations import temporary_router_reservations

    with temporary_router_reservations(
        board, ground_contact_margin_mm=ground_contact_margin_mm
    ) as reservations:
        if not pcbnew.ExportSpecctraDSN(board, str(design)):
            raise RuntimeError("KiCad could not export a temporary router DSN")
        if any(reservations.values()):
            print(
                "Local search reservations: "
                f"{reservations['reference_areas']} reference corridors, "
                f"{reservations['ground_contact_areas']} ground-contact regions",
                file=sys.stderr,
                flush=True,
            )
    removed = _strip_zone_planes(design, _native_zone_keys(board))
    return _set_equal_layer_costs(design, optimizer=optimizer), removed


def _complete_fastroute_session(path: Path) -> bool:
    """Reject missing or interrupted checkpoints before a native trial import."""
    try:
        session = parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    routes = session.children("routes")
    return (
        session.name == "session"
        and len(session.children("base_design")) == 1
        and len(routes) == 1
        and len(routes[0].children("network_out")) == 1
    )


def _routing_only_session(source: str) -> str:
    """Remove component placement before importing router copper into a trial."""
    session = parse(source)
    placements = session.children("placement")
    routes = session.children("routes")
    if (
        session.name != "session"
        or len(session.children("base_design")) != 1
        or len(placements) > 1
        or len(routes) != 1
        or len(routes[0].children("network_out")) != 1
    ):
        raise ValueError("Cannot safely interpret the routing session")
    if not placements:
        return source
    placement = placements[0]
    return source[: placement.start] + source[placement.end :]


def _routing_only_session_file(session: Path) -> Path:
    """Create a temporary session that cannot move any footprints."""
    result = session.with_name(session.stem + "-routing-only.ses")
    result.write_text(
        _routing_only_session(session.read_text(encoding="utf-8-sig")),
        encoding="utf-8",
    )
    return result


def _high_speed_reservations(project_bytes: bytes, board: Any) -> tuple[str, ...]:
    """Keep project-classified paired nets out of single-net routing passes."""
    from pcb.net_routing.reference import high_speed_nets

    names = {str(name) for name in board.GetNetsByName().keys()}
    return tuple(sorted(high_speed_nets(project_bytes, names)))


def _cross_class_members(network):
    """Read cross-class rule references without guessing unsupported forms."""
    for rule in network.children("class_class"):
        groups = rule.children("classes")
        if len(groups) != 1 or groups[0].children() or len(groups[0].atoms()) < 3:
            raise ValueError("Cannot safely interpret a cross-class routing rule")
        yield rule, groups[0].atoms()[1:]


def _alias_deferred_default(design: Path, deferred: tuple[str, ...]) -> tuple[str, ...]:
    """Avoid Freerouting's special treatment of KiCad's default class."""
    if "kicad_default" not in deferred:
        return deferred
    text = design.read_text(encoding="utf-8")
    start, network = _dsn_scope(text, "network")
    classes = network.children("class")
    defaults = [
        item
        for item in classes
        if len(item.atoms()) >= 2 and item.atoms()[1].value == "kicad_default"
    ]
    alias = "AUTOPCB_DEFER_DEFAULT"
    if len(defaults) != 1 or any(
        len(item.atoms()) >= 2 and item.atoms()[1].value == alias for item in classes
    ):
        raise ValueError("Cannot safely alias the exported default net class")
    name = defaults[0].atoms()[1]
    edits = [(start + name.start, start + name.end, alias)]
    for _, members in _cross_class_members(network):
        edits.extend(
            (start + atom.start, start + atom.end, alias)
            for atom in members
            if atom.value == "kicad_default"
        )
    for first, last, replacement in sorted(edits, reverse=True):
        text = text[:first] + replacement + text[last:]
    design.write_text(text, encoding="utf-8")
    return tuple(alias if item == "kicad_default" else item for item in deferred)


def _defer_selected_nets(
    design: Path, nets: tuple[str, ...], deferred_classes: tuple[str, ...]
) -> tuple[str, ...]:
    """Temporarily split net classes without changing their routing rules."""
    if not nets:
        return ()
    text = design.read_text(encoding="utf-8")
    start, network = _dsn_scope(text, "network")
    classes = network.children("class")
    existing_names = {
        item.atoms()[1].value for item in classes if len(item.atoms()) >= 2
    }
    matches = {name: [] for name in nets}
    for item in classes:
        for atom in item.atoms()[2:]:
            if atom.value in matches:
                matches[atom.value].append((item, atom))
    if any(len(found) != 1 for found in matches.values()):
        raise ValueError("Cannot safely identify deferred net class membership")
    selected = set(nets)
    edits = []
    additions = []
    ignored = []
    aliases = {}
    index = 0
    for item in classes:
        atoms = item.atoms()
        if len(atoms) < 2:
            raise ValueError("The exported DSN contains an invalid net class")
        class_name = atoms[1].value
        chosen = [atom for atom in atoms[2:] if atom.value in selected]
        if not chosen or class_name in deferred_classes:
            continue
        if len(chosen) == len(atoms) - 2:
            ignored.append(class_name)
            continue
        index += 1
        alias = f"AUTOPCB_DEFER_NET_{index}"
        if alias in existing_names:
            raise ValueError("A temporary deferred net class already exists")
        existing_names.add(alias)
        aliases[class_name] = alias
        # Preserve every class setting, including widths, clearances and vias.
        settings = " ".join(
            text[start + child.start : start + child.end] for child in item.children()
        )
        member_names = " ".join(
            text[start + atom.start : start + atom.end] for atom in chosen
        )
        additions.append(f"\n    (class {alias} {member_names} {settings})")
        ignored.append(alias)
        edits.extend((start + atom.start, start + atom.end, "") for atom in chosen)
    if additions:
        # Splitting a class also splits every pairwise clearance relationship
        # involving it. Duplicate the rule for every alias combination; the
        # original class and its remaining nets keep the original rule.
        inherited = set()
        for rule, members in _cross_class_members(network):
            changed = [atom for atom in members if atom.value in aliases]
            if len(changed) > 8:
                raise ValueError("A cross-class rule has too many alias combinations")
            source = text[start + rule.start : start + rule.end]
            for mask in range(1, 1 << len(changed)):
                duplicate = source
                replacements = [
                    (
                        atom.start - rule.start,
                        atom.end - rule.start,
                        aliases[atom.value],
                    )
                    for index, atom in enumerate(changed)
                    if mask & (1 << index)
                ]
                for first, last, replacement in sorted(replacements, reverse=True):
                    duplicate = duplicate[:first] + replacement + duplicate[last:]
                inherited.add(duplicate)
        additions.extend("\n    " + rule for rule in sorted(inherited))
        edits.append(
            (
                start + network.end - 1,
                start + network.end - 1,
                "".join(additions) + "\n  ",
            )
        )
        for first, last, replacement in sorted(edits, reverse=True):
            text = text[:first] + replacement + text[last:]
        design.write_text(text, encoding="utf-8")
    return tuple(ignored)


def _dsn_width_factor(text: str) -> int:
    units = re.findall(r"(?m)^\s*\(unit\s+(um|mm|mil|inch)\s*\)\s*$", text)
    if len(units) != 1:
        raise ValueError("Cannot safely interpret the router design's units")
    return {"um": 1_000, "mm": 1_000_000, "mil": 25_400, "inch": 25_400_000}[units[0]]


def _compiled_router_widths(
    board,
    rules_path: Path,
    *,
    ignore_nets: tuple[str, ...] = (),
    ignore_classes: tuple[str, ...] = (),
) -> dict[str, int]:
    """Compile conservative whole-net search widths from the saved custom rules.

    Local escape minima cannot become trunk widths. A definite named-net rule
    can override a KiCad routing preset; exact saved rules are checked again on
    every proposed segment after import. The native design is never modified.
    """
    from pcb.net_routing.rules import RoutingRules

    eligible = []
    for raw in board.GetNetsByName():
        name = str(raw)
        if not name or name.startswith("unconnected-") or name in ignore_nets:
            continue
        netclass = str(board.FindNet(name).GetNetClassName())
        router_class = "kicad_default" if netclass == "Default" else netclass
        if router_class not in ignore_classes:
            eligible.append(name)
    if not eligible:
        return {}
    rules = RoutingRules.from_board(board, rules_path)
    return {name: rules.profile(name, 0, 0).maximum_width for name in eligible}


def _set_net_widths(design: Path, widths: dict[str, int]) -> None:
    """Materialize per-net search widths without thinning unrelated class members.

    Split only differing members and inherit via settings and every pairwise
    clearance relationship. All edits are made to a disposable DSN, after the
    complete class inventory and bounded alias expansion have been checked.
    """
    if (
        not isinstance(widths, dict)
        or len(widths) > 4096
        or any(
            not isinstance(name, str)
            or not name
            or isinstance(width, bool)
            or not isinstance(width, int)
            or width <= 0
            for name, width in widths.items()
        )
    ):
        raise ValueError(
            "Router widths require named nets and positive integer dimensions"
        )
    if not widths:
        return
    text = design.read_text(encoding="utf-8")
    factor = _dsn_width_factor(text)
    start, network = _dsn_scope(text, "network")
    classes = network.children("class")
    matches = {name: [] for name in widths}
    names = set()
    for item in classes:
        atoms = item.atoms()
        if len(atoms) < 2 or atoms[1].value in names:
            raise ValueError("The exported DSN contains an invalid net class")
        names.add(atoms[1].value)
        for atom in atoms[2:]:
            if atom.value in matches:
                matches[atom.value].append(item)
    if any(len(found) != 1 for found in matches.values()):
        raise ValueError("Cannot safely identify compiled-width net class membership")
    edits, additions, aliases = [], [], defaultdict(list)
    for item in classes:
        atoms = item.atoms()
        selected = [atom for atom in atoms[2:] if atom.value in widths]
        if not selected:
            continue
        rules = item.children("rule")
        values = rules[0].children("width") if len(rules) == 1 else []
        tokens = values[0].atoms() if len(values) == 1 else []
        if len(tokens) != 2:
            raise ValueError("Cannot safely interpret a router class width")
        try:
            existing = Decimal(tokens[1].value)
        except InvalidOperation as error:
            raise ValueError("Cannot safely interpret a router class width") from error
        if not existing.is_finite() or existing <= 0:
            raise ValueError("Cannot safely interpret a router class width")
        groups = defaultdict(list)
        for atom in selected:
            width = (Decimal(widths[atom.value]) / factor).quantize(
                Decimal("0.000001"), rounding=ROUND_CEILING
            )
            if width != existing:
                groups[width].append(atom)
        changed = {atom.value for members in groups.values() for atom in members}
        if changed and changed == {atom.value for atom in atoms[2:]}:
            # Keep one group on the original class, avoiding empty classes.
            width = min(groups)
            groups.pop(width)
            edits.append(
                (start + tokens[1].start, start + tokens[1].end, format(width, "f"))
            )
        for width, members in sorted(groups.items()):
            alias = f"AUTOPCB_WIDTH_{len(additions) + 1}"
            if alias in names:
                raise ValueError("A temporary compiled-width class already exists")
            names.add(alias)
            aliases[atoms[1].value].append(alias)
            chosen = {atom.value for atom in members}
            snippet = text[start + item.start : start + item.end]
            local = [
                (
                    tokens[1].start - item.start,
                    tokens[1].end - item.start,
                    format(width, "f"),
                ),
                (atoms[1].start - item.start, atoms[1].end - item.start, quote(alias)),
            ]
            local.extend(
                (atom.start - item.start, atom.end - item.start, "")
                for atom in atoms[2:]
                if atom.value not in chosen
            )
            for first, last, replacement in sorted(local, reverse=True):
                snippet = snippet[:first] + replacement + snippet[last:]
            additions.append(snippet)
            edits.extend((start + atom.start, start + atom.end, "") for atom in members)
    inherited = set()
    for rule, members in _cross_class_members(network):
        choices = [(atom.value, *aliases[atom.value]) for atom in members]
        count = math.prod(len(choice) for choice in choices)
        if count > 1024 or len(inherited) + count > 4096:
            raise ValueError(
                "Compiled-width clearance inheritance exceeds its expansion budget"
            )
        for variant in product(*choices):
            if all(name == atom.value for atom, name in strict_zip(members, variant)):
                continue
            snippet = text[start + rule.start : start + rule.end]
            replacements = [
                (atom.start - rule.start, atom.end - rule.start, quote(name))
                for atom, name in strict_zip(members, variant)
                if atom.value != name
            ]
            for first, last, replacement in sorted(replacements, reverse=True):
                snippet = snippet[:first] + replacement + snippet[last:]
            inherited.add(snippet)
    additions.extend(sorted(inherited))
    if additions:
        edits.append(
            (
                start + network.end - 1,
                start + network.end - 1,
                "\n    " + "\n    ".join(additions) + "\n",
            )
        )
    if edits:
        for first, last, replacement in sorted(edits, reverse=True):
            text = text[:first] + replacement + text[last:]
        design.write_text(text, encoding="utf-8")


def _enforce_width_floors(design: Path, floors: dict[str, int]) -> None:
    """Raise temporary router class widths to cover replaced copper, in nm.

    Classes shared by several selected nets receive the largest floor. This
    never changes project rules or reduces a width; native checks still apply.
    """
    if not isinstance(floors, dict) or any(
        not isinstance(name, str)
        or not name
        or isinstance(width, bool)
        or not isinstance(width, int)
        or width <= 0
        for name, width in floors.items()
    ):
        raise ValueError(
            "Router width floors require named nets and positive integer dimensions"
        )
    if not floors:
        return
    text = design.read_text(encoding="utf-8")
    factor = _dsn_width_factor(text)
    start, network = _dsn_scope(text, "network")
    matches = {name: [] for name in floors}
    classes = network.children("class")
    for item in classes:
        atoms = item.atoms()
        if len(atoms) < 2:
            raise ValueError("The exported DSN contains an invalid net class")
        for atom in atoms[2:]:
            if atom.value in matches:
                matches[atom.value].append(item)
    if any(len(found) != 1 for found in matches.values()):
        raise ValueError("Cannot safely identify width-floor net class membership")
    edits = []
    for item in classes:
        requested = [
            floors[atom.value] for atom in item.atoms()[2:] if atom.value in floors
        ]
        if not requested:
            continue
        rules = item.children("rule")
        widths = rules[0].children("width") if len(rules) == 1 else []
        atoms = widths[0].atoms() if len(widths) == 1 else []
        if len(atoms) != 2:
            raise ValueError("Cannot safely interpret a router class width")
        try:
            existing = Decimal(atoms[1].value)
        except InvalidOperation as error:
            raise ValueError("Cannot safely interpret a router class width") from error
        if not existing.is_finite() or existing <= 0:
            raise ValueError("Cannot safely interpret a router class width")
        required = (Decimal(max(requested)) / factor).quantize(
            Decimal("0.000001"), rounding=ROUND_CEILING
        )
        if required > existing:
            edits.append(
                (start + atoms[1].start, start + atoms[1].end, format(required, "f"))
            )
    if edits:
        for first, last, replacement in sorted(edits, reverse=True):
            text = text[:first] + replacement + text[last:]
        design.write_text(text, encoding="utf-8")


def _verify_reported_costs(log: Path, layer_count: int) -> None:
    """Check the router's actual costs; some releases ignore DSN settings."""
    text = log.read_text(encoding="utf-8", errors="replace")
    number = r"([0-9]+(?:\.[0-9]+)?)"
    found = re.findall(r"Layer\s+(\d+):\s*" + number + r"\s*/\s*" + number, text)
    vias = re.findall(r"Via Costs:\s*(\d+),\s*Plane Via Costs:\s*(\d+)", text)
    last = found[-layer_count:]
    if (
        len(last) != layer_count
        or [int(item[0]) for item in last] != list(range(1, layer_count + 1))
        or any(
            float(preferred) != 1.0 or float(other) != 1.0
            for _, preferred, other in last
        )
        or not vias
        or vias[-1] != ("10", "10")
    ):
        raise RuntimeError(
            f"Freerouting did not use the requested equal layer costs; inspect {log}"
        )


def _snapshot(board: Path, destination: Path) -> dict[str, str]:
    """Copy stable inputs to a location that cannot be the live project."""
    board = board.resolve()
    destination = destination.resolve()
    if destination.is_relative_to(board.parent) or not destination.is_dir():
        raise ValueError("The trial directory must exist outside the project")
    files = _source_files(board)
    before = _fingerprints(files, board.parent)
    _copy_inputs(list(files), board, destination)
    if files != _source_files(board) or _fingerprints(files, board.parent) != before:
        raise RuntimeError("Project inputs changed while taking the snapshot")
    copied = _fingerprints(
        _source_files(native_path(destination, ".kicad_pcb")), destination
    )
    if copied != before:
        raise RuntimeError("The trial copy does not match the saved project inputs")
    return before


def _positions(board: Any) -> dict[str, tuple[int, int, float, int]]:
    positions = {}
    for footprint in board.GetFootprints():
        position = footprint.GetPosition()
        identifier = footprint.m_Uuid.AsString()
        if identifier in positions:
            raise ValueError("Duplicate footprint UUIDs prevent a reliable trial")
        positions[identifier] = (
            int(position.x),
            int(position.y),
            float(footprint.GetOrientationDegrees()),
            int(footprint.GetLayer()),
        )
    return positions


def _restore_bottom_rounding(
    board: Any, before: dict[str, tuple[int, int, float, int]]
) -> list[str]:
    """Undo at most one nanometre of bottom-side import rounding on the trial."""
    import pcbnew

    after = _positions(board)
    if before.keys() != after.keys():
        raise RuntimeError(
            "The imported trial changed the footprint inventory; discard it"
        )
    restored = []
    footprints = {item.m_Uuid.AsString(): item for item in board.GetFootprints()}
    for identifier, old in before.items():
        new = after[identifier]
        if old == new:
            continue
        if (
            old[3] != pcbnew.B_Cu
            or new[2:] != old[2:]
            or abs(new[0] - old[0]) > 1
            or abs(new[1] - old[1]) > 1
        ):
            raise RuntimeError(
                "The imported trial changed footprint positions; discard it"
            )
        footprints[identifier].SetPosition(pcbnew.VECTOR2I(old[0], old[1]))
        restored.append(identifier)
    if _positions(board) != before:
        raise RuntimeError("Could not restore the trial's original footprint positions")
    return restored


def _drc(cli: Path, board: Path, output: Path) -> dict[str, int]:
    data = _native_drc(cli, board, output, schematic_parity=True)
    summary = summarize_drc(data)
    return {
        "unconnected_items": summary["open_connections"],
        "other_violations": len(data["violations"]),
        "other_errors": summary["errors"],
        "other_warnings": summary["warnings"],
        "exclusions": summary["exclusions"],
        "ignored_checks": len(summary["ignored_checks"]),
        "schematic_parity_items": summary["parity_findings"],
    }


def preview(
    project: Path,
    jar: Path,
    java: str,
    passes: int,
    timeout: int,
    *,
    skip_optimizer: bool = False,
    max_items: int | None = None,
    ignore_net_classes: tuple[str, ...] = (),
    ignore_nets: tuple[str, ...] = (),
    ignore_high_speed: bool = False,
    starts: int = 1,
    defer_native_checks: bool = False,
    engine: str = "Freerouting",
    minimum_widths: dict[str, int] | None = None,
    trial_root: Path | None = None,
    ground_contact_margin_mm: float = 0.5,
    search_pad_neckdown: bool = False,
) -> dict[str, Any]:
    import pcbnew

    project = project.resolve()
    board = native_path(project, ".kicad_pcb")
    jar = jar.resolve()
    if engine not in {"Freerouting", "fastroute"}:
        raise ValueError("Unsupported routing engine")
    if not isinstance(search_pad_neckdown, bool):
        raise ValueError("search_pad_neckdown must be true or false")
    if isinstance(starts, bool) or not isinstance(starts, int) or not 1 <= starts <= 2:
        raise ValueError("starts must be one or two")
    if engine == "Freerouting" and starts != 1:
        raise ValueError("Multiple starts require fastroute")
    if not jar.is_file():
        raise ValueError(f"The {engine} executable is required")
    jar_sha256 = hashlib.sha256(jar.read_bytes()).hexdigest()
    if engine == "fastroute" and jar_sha256 != FASTROUTE_014_SHA256:
        raise ValueError(
            "The fastroute executable does not match the verified 0.1.14 release"
        )
    executable = _select_java(java, jar) if engine == "Freerouting" else str(jar)
    if passes < 1 or timeout < 1:
        raise ValueError("Passes and timeout must both be positive")
    if not isinstance(skip_optimizer, bool):
        raise ValueError("skip_optimizer must be true or false")
    if not isinstance(defer_native_checks, bool):
        raise ValueError("defer_native_checks must be true or false")
    if not isinstance(ignore_high_speed, bool):
        raise ValueError("ignore_high_speed must be true or false")
    if max_items is not None and (isinstance(max_items, bool) or max_items < 1):
        raise ValueError("The item limit must be a positive integer")
    if (
        not isinstance(ignore_net_classes, tuple)
        or any(
            not isinstance(name, str)
            or not name
            or "," in name
            or any(ord(char) < 32 for char in name)
            for name in ignore_net_classes
        )
        or len(set(ignore_net_classes)) != len(ignore_net_classes)
    ):
        raise ValueError("Deferred net classes must be distinct, nonempty class names")
    if (
        not isinstance(ignore_nets, tuple)
        or any(
            not isinstance(name, str)
            or not name
            or any(ord(char) < 32 for char in name)
            for name in ignore_nets
        )
        or len(set(ignore_nets)) != len(ignore_nets)
    ):
        raise ValueError("Deferred nets must be distinct, nonempty net names")
    trial = Path(tempfile.mkdtemp(prefix="jessie-freeroute-", dir=trial_root))
    inputs = _snapshot(board, trial)
    trial_board = trial / board.name
    native = pcbnew.LoadBoard(str(trial_board))
    positions = _positions(native)
    before_tracks = len(native.GetTracks())
    cli = kicad_cli()
    if not cli.is_file():
        raise RuntimeError("KiCad CLI is required")
    baseline = (
        {}
        if defer_native_checks
        else _drc(cli, trial_board, trial / "baseline-drc.json")
    )
    design, session = trial / "candidate.dsn", trial / "candidate.ses"
    layers, removed_zone_planes = _prepare_router_dsn(
        native,
        design,
        optimizer=not skip_optimizer,
        ground_contact_margin_mm=ground_contact_margin_mm,
    )
    critical = (
        _high_speed_reservations(
            (native_path(trial, ".kicad_pro")).read_bytes(), native
        )
        if ignore_high_speed
        else ()
    )
    all_deferred_nets = tuple(dict.fromkeys((*ignore_nets, *critical)))
    _set_net_widths(
        design,
        _compiled_router_widths(
            native,
            trial_board.with_suffix(".kicad_dru"),
            ignore_nets=all_deferred_nets,
            ignore_classes=ignore_net_classes,
        ),
    )
    router_deferred = ignore_net_classes + _defer_selected_nets(
        design, all_deferred_nets, ignore_net_classes
    )
    router_deferred = _alias_deferred_default(
        design, tuple(dict.fromkeys(router_deferred))
    )
    if minimum_widths is not None:
        _enforce_width_floors(design, minimum_widths)
    print(f"{engine} trial: {trial}", file=sys.stderr, flush=True)
    home = trial / "router-home"
    home.mkdir()
    env = os.environ.copy()
    for name in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        directory = home / name.lower()
        directory.mkdir()
        env[name] = str(directory)
    timed_out = False
    result = None
    with (trial / "freerouting.log").open("w", encoding="utf-8") as log:
        if engine == "Freerouting":
            command = [
                executable,
                f"-Duser.home={home}",
                "-jar",
                str(jar),
                "-de",
                str(design),
                "-do",
                str(session),
                "-mp",
                str(passes),
                f"--user_data_path={home}",
                "--gui.enabled=false",
                "--router.fanout.enabled=false",
                f"--router.automatic_neckdown={str(search_pad_neckdown).lower()}",
                "--router.strict_drc=true",
                "--router.optimizer.max_threads=1",
                "--logging.console.level=DEBUG",
                "-da",
            ]
        else:
            command = [
                executable,
                "-de",
                str(design),
                "-do",
                str(session),
                "-mp",
                str(passes),
                f"--max-time={max(1, timeout - 10)}",
                f"--multi-start={starts}",
                "--router.autorouter.max_threads=1",
                "--router.optimizer.max_threads=1",
                "--router.fanout.enabled=false",
                f"--router.automatic_neckdown={str(search_pad_neckdown).lower()}",
                "--router.strict_drc=true",
            ]
        if skip_optimizer:
            command.append("--router.optimizer.enabled=false")
        if max_items is not None:
            field = "max_items" if engine == "Freerouting" else "autorouter.max_items"
            command.append(f"--router.{field}={max_items}")
        if router_deferred:
            if engine == "Freerouting":
                command.extend(["-inc", ",".join(router_deferred)])
            else:
                command.append(
                    "--router.autorouter.ignore_net_classes="
                    + ",".join(router_deferred)
                )
        try:
            result = subprocess.run(
                command,
                cwd=trial,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            if engine != "fastroute" or not _complete_fastroute_session(session):
                raise RuntimeError(
                    f"{engine} timed out after {timeout} seconds without a complete checkpoint; "
                    f"try a smaller --max-items. Log: {trial / 'freerouting.log'}"
                ) from error
            timed_out = True
            print(
                "fastroute timed out; checking its last complete private checkpoint",
                file=sys.stderr,
                flush=True,
            )
    if (result is not None and result.returncode) or not session.is_file():
        detail = "\n".join(
            (trial / "freerouting.log").read_text(encoding="utf-8").splitlines()[-5:]
        )
        raise RuntimeError(
            f"{engine} failed: {detail}; log: {trial / 'freerouting.log'}"
        )
    current = _source_files(board)
    if _fingerprints(current, project) != inputs:
        raise RuntimeError(
            "Saved project inputs changed during routing; discard this candidate"
        )
    if hashlib.sha256(jar.read_bytes()).hexdigest() != jar_sha256:
        raise RuntimeError(f"{engine} changed during the trial; discard this candidate")
    optimizer_status = "disabled" if skip_optimizer else "requested_not_verified"
    if engine == "Freerouting":
        _verify_reported_costs(trial / "freerouting.log", len(layers))
    else:
        log_text = (trial / "freerouting.log").read_text(
            encoding="utf-8", errors="replace"
        )
        if (
            "fastroute 0.1.14" not in log_text
            or f": {len(layers)} layers," not in log_text
        ):
            raise RuntimeError(
                "fastroute did not report the expected version and layer count"
            )
        if not skip_optimizer:
            optimizer_status = (
                "observed"
                if "Optimization stage started" in log_text
                else "requested_not_observed"
            )
    if not pcbnew.ImportSpecctraSES(native, str(_routing_only_session_file(session))):
        raise RuntimeError("KiCad could not import the trial session")
    restored = _restore_bottom_rounding(native, positions)
    if not pcbnew.SaveBoard(str(trial_board), native):
        raise RuntimeError("KiCad could not save the trial board")
    candidate = (
        {}
        if defer_native_checks
        else _drc(cli, trial_board, trial / "candidate-drc.json")
    )
    if _fingerprints(_source_files(board), project) != inputs:
        raise RuntimeError(
            "Saved project inputs changed during validation; discard this candidate"
        )
    return {
        "status": "unapproved_trial_only",
        "native_drc": "deferred_to_proposal_filter"
        if defer_native_checks
        else "checked",
        "engine": engine,
        "router_passes": passes,
        "router_timed_out": timed_out,
        "optimizer_status": optimizer_status,
        "layer_cost_evidence": "router_log"
        if engine == "Freerouting"
        else "dsn_and_pinned_source_only",
        "saved_board_sha256": inputs[board.name],
        "freerouting_jar_sha256": jar_sha256,
        "trial_board": str(trial_board),
        "footprint_positions_unchanged": True,
        "deferred_net_classes": list(ignore_net_classes),
        "deferred_nets": list(ignore_nets),
        "reserved_high_speed_nets": list(critical),
        "ground_contact_search_margin_mm": ground_contact_margin_mm,
        "search_pad_neckdown": search_pad_neckdown,
        "temporary_zone_planes_removed": [list(key) for key in removed_zone_planes],
        "restored_bottom_rounding_uuids": restored,
        "baseline": {"copper_items": before_tracks, **baseline},
        "candidate": {"copper_items": len(native.GetTracks()), **candidate},
        "limitations": (
            "Do not apply the trial as-is. Check the proposed copper, connectivity, "
            "angles, current, protection, reference planes, and native KiCad rules."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    newest = RouterCache.freerouting()
    parser.add_argument(
        "--jar",
        type=Path,
        default=newest if newest.is_file() else RouterCache.root() / "freerouting.jar",
    )
    parser.add_argument(
        "--java",
        default="auto",
        help="Java executable, or auto to find a compatible runtime",
    )
    parser.add_argument("--passes", type=int, default=1)
    parser.add_argument(
        "--timeout", type=int, default=600, help="routing timeout in seconds"
    )
    parser.add_argument(
        "--skip-optimizer", action="store_true", help="export the raw routing pass"
    )
    parser.add_argument(
        "--max-items", type=int, help="limit attempted items in the routing pass"
    )
    parser.add_argument(
        "--ground-contact-guard-mm",
        type=float,
        default=0.5,
        help="extra search-only ground-pad margin; native physical rules remain unchanged",
    )
    parser.add_argument(
        "--search-pad-neckdown",
        action="store_true",
        help="propose smaller pad exits; saved width rules and native checks still apply",
    )
    args = parser.parse_args(argv)
    try:
        print(
            json.dumps(
                preview(
                    args.project,
                    args.jar,
                    args.java,
                    args.passes,
                    args.timeout,
                    skip_optimizer=args.skip_optimizer,
                    max_items=args.max_items,
                    ground_contact_margin_mm=args.ground_contact_guard_mm,
                    search_pad_neckdown=args.search_pad_neckdown,
                ),
                indent=2,
            )
        )
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Freerouting preview failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
