"""Independently compare protected native schematic and board content."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from collections import Counter
from dataclasses import dataclass

from core.kicad_sexpr import Form, parse
from pcb.route_validation import _canonical


def working_connectivity_classes(board) -> frozenset[str] | None:
    """Read the explicit WIP scope; this never supplies electrical evidence."""
    from core.paths import asset_root
    from pcb.checkpoints import read_config

    filename = board.GetFileName()
    if not isinstance(filename, str) or not filename:
        return None
    policy = asset_root(Path(filename).resolve().parent) / "config" / "autopcb.json"
    if not policy.is_file():
        return None
    settings, _ = read_config(policy)
    scope = settings.get("working_scope")
    return frozenset(value.upper() for value in scope["classes"]) if scope else None


def _working_scope_exclusions(board, classes: frozenset[str]) -> set[str]:
    """Keep unnamed, NC, critical-pair and out-of-scope nets protected."""
    result = set()
    for raw in board.GetNetsByName():
        name = str(raw)
        if not name:
            continue
        net = board.FindNet(name)
        netclass = str(net.GetNetClassName()).upper() if net is not None else ""
        if (name.startswith("unconnected-") or netclass not in classes
                or netclass in {"USB_DATA", "ETH_DATA"}):
            result.add(name)
    return result


def protection_nets(board) -> set[str]:
    """Identify power, monitoring and actuator-safety nets conservatively."""
    classes = working_connectivity_classes(board)
    if classes is not None:
        return _working_scope_exclusions(board, classes)
    result = set()
    for part in board.GetFootprints():
        ref, value = str(part.GetReference()), str(part.GetValue())
        description = str(part.GetFieldsText().get("Description", ""))
        # Transistor terminals may carry enable, gate, supply or return paths
        # even when their net names contain none of those words.  Defer their
        # copper and connected placement until a branch model qualifies them.
        protected_ref = bool(re.fullmatch(r"[DFQ][0-9]+", ref))
        protected_value = bool(re.search(
            r"\b(?:E[ -]?FUSE|TVS|ESD|SURGE|OVERVOLTAGE|UNDERVOLTAGE|SUPERVISOR|"
            r"(?:VBUS|VOLTAGE)[ -]+MONITOR|REVERSE.{0,60}DISCONNECT|"
            r"POWER[ -]+(?:MUX|MULTIPLEXER|SWITCH|PATH)|HOT[ -]?SWAP|"
            r"IDEAL[ -]+DIODE|TPS2121[A-Z0-9]*|LDO|LOW[ -]+DROPOUT|"
            r"VOLTAGE[ -]+REGULATOR|CONTROL[ -]+SUPPLY|WINDOW[ -]+MONITOR|"
            r"CURRENT[ -]+SENSE|MOSFET[ -]+DRIVER|"
            r"[0-9]+(?:\.[0-9]+)?[ ]?V[ -]+DRIVER|"
            r"CC[ -]+SINK|TUSB320[A-Z0-9]*|USB[0-9A-Z -]*DATA[ -]+DISCONNECT|"
            r"SPST[ -]+SWITCH(?:ES)?|"
            r"(?:MOTOR|LAMP|LIGHT|LIGHTING).{0,80}(?:DRIVER|GATE|BUFFER|ENABLE))\b",
            f"{value} {description}", flags=re.IGNORECASE,
        ))
        if protected_ref or protected_value:
            result.update(str(pad.GetNetname()) for pad in part.Pads() if pad.GetNetname())
    for native_name in board.GetNetsByName():
        name = str(native_name)
        safety_input = re.search(r"(?:^|_)(?:JOG|LIMIT|ESTOP|ENC|ENCODER|VBUS|FIELD_VALID)(?:_|$)",
                                 name, flags=re.IGNORECASE)
        actuator = re.search(r"(?:^|_)(?:BTS|MOTOR|LAMP|LIGHT|LIGHTS|TOP|FRONT)(?:_|$)",
                             name, flags=re.IGNORECASE)
        control = re.search(r"(?:^|_)(?:PWM|EN|ENABLE|GATE|CURRENT|SENSE|IS|FAULT)(?:_|$)",
                            name, flags=re.IGNORECASE)
        if safety_input or (actuator and control):
            result.add(name)
    return result


def unqualified_working_nets(board, excluded: tuple[str, ...] = ()) -> set[str]:
    """Keep protected and non-signal classes out of unqualified working saves."""
    classes = working_connectivity_classes(board)
    if classes is not None:
        return _working_scope_exclusions(board, classes) | set(excluded)
    result = protection_nets(board) | set(excluded)
    for raw in board.GetNetsByName():
        name = str(raw)
        if not name:
            continue
        net = board.FindNet(name)
        netclass = str(net.GetNetClassName()).upper() if net is not None else ""
        if (not (netclass == "SIGNAL" or netclass.endswith("_SIGNAL"))
                or re.search(r"(?:^|_)(?:POWER|GROUND|GND)(?:_|$)", netclass)):
            result.add(name)
    return result


def protected_copper_unchanged(before: list, after: list, excluded: tuple[str, ...]) -> bool:
    """Compare protected track and via geometry without relying on object order."""
    names = set(excluded)
    return Counter(item for item in before if item.net in names) == Counter(
        item for item in after if item.net in names
    )


def protected_footprint_changes(before_board, after_board, before: bytes, after: bytes,
                                excluded: tuple[str, ...]) -> tuple[str, ...]:
    """Find footprint changes on nets that lack a qualified working-save model."""
    nets = unqualified_working_nets(before_board, excluded) | unqualified_working_nets(after_board, excluded)
    protected = {}
    for board in (before_board, after_board):
        for part in board.GetFootprints():
            if any(str(pad.GetNetname()) in nets for pad in part.Pads() if pad.GetNetname()):
                protected[part.m_Uuid.AsString()] = str(part.GetReference())

    def footprints(data: bytes) -> dict:
        root = parse(data.decode("utf-8-sig"))
        if root.name != "kicad_pcb":
            raise ValueError("Expected a native KiCad PCB")
        result = {}
        for item in root.children("footprint"):
            uid = _field(item, "uuid")
            if uid in result:
                raise ValueError("Duplicate footprint UUID prevents comparison")
            result[uid] = _canonical(item)
        return result

    old, new = footprints(before), footprints(after)
    return tuple(sorted(protected[uid] for uid in protected
                        if uid not in old or uid not in new or old[uid] != new[uid]))


def _digest(value: object) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def _field(form: Form, name: str) -> str:
    children = form.children(name)
    if len(children) != 1 or len(children[0].atoms()) != 2:
        raise ValueError(f"Expected one {name} in {form.name}")
    return children[0].atoms()[1].value


def _layer(form: Form) -> str | None:
    values = form.children("layer")
    if not values:
        return None
    return _field(form, "layer")


def _insert(target: dict, key: object, value: object) -> None:
    if key in target:
        raise ValueError(f"Duplicate protected board object: {key}")
    target[key] = _digest(value)


@dataclass(frozen=True, slots=True)
class ProtectedState:
    schematic_sha256: str
    top_footprints: tuple[tuple[str, str], ...]
    front_silkscreen: tuple[tuple[tuple[str, ...], str], ...]
    outline: tuple[tuple[str, str], ...]
    stackup: str
    preserve_all_placement: bool = False

    @classmethod
    def capture(cls, board_bytes: bytes, schematic_bytes: bytes, *,
                preserve_all_placement: bool = False) -> ProtectedState:
        if not isinstance(preserve_all_placement, bool):
            raise ValueError("Placement protection must be a boolean")
        board = parse(board_bytes.decode("utf-8-sig"))
        if board.name != "kicad_pcb":
            raise ValueError("Expected a native KiCad PCB")
        layers = board.children("layers")
        setup = board.children("setup")
        if len(layers) != 1 or len(setup) != 1:
            raise ValueError("PCB layers and setup must each occur exactly once")
        stackups = setup[0].children("stackup")
        if len(stackups) != 1:
            raise ValueError("A single explicit PCB stackup is required")

        top: dict[str, str] = {}
        silk: dict[tuple[str, ...], str] = {}
        outline: dict[str, str] = {}
        for item in board.children():
            if item.name == "footprint":
                uid = _field(item, "uuid")
                layer = _layer(item)
                if layer not in {"F.Cu", "B.Cu"}:
                    raise ValueError(f"Unsupported footprint side: {layer}")
                if layer == "F.Cu" or preserve_all_placement:
                    _insert(top, uid, _canonical(item))
                pose = item.children("at")
                if len(pose) != 1:
                    raise ValueError(f"Footprint {uid} must have one position")
                for child in item.children():
                    if _layer(child) == "F.SilkS":
                        child_id = _field(child, "uuid")
                        _insert(silk, ("footprint", uid, child_id), (_canonical(pose[0]), _canonical(child)))
            elif _layer(item) == "F.SilkS":
                _insert(silk, ("board", _field(item, "uuid")), _canonical(item))
            elif _layer(item) == "Edge.Cuts":
                _insert(outline, _field(item, "uuid"), _canonical(item))
        return cls(
            hashlib.sha256(schematic_bytes).hexdigest(),
            tuple(sorted(top.items())),
            tuple(sorted(silk.items())),
            tuple(sorted(outline.items())),
            _digest((_canonical(layers[0]), _canonical(stackups[0]))),
            preserve_all_placement,
        )

    def differences(self, board_bytes: bytes, schematic_bytes: bytes) -> tuple[str, ...]:
        other = self.capture(board_bytes, schematic_bytes,
                             preserve_all_placement=self.preserve_all_placement)
        pairs = (
            ("schematic", self.schematic_sha256, other.schematic_sha256),
            ("component placement" if self.preserve_all_placement else "top footprints",
             self.top_footprints, other.top_footprints),
            ("front silkscreen", self.front_silkscreen, other.front_silkscreen),
            ("outline", self.outline, other.outline),
            ("stackup", self.stackup, other.stackup),
        )
        return tuple(name for name, old, new in pairs if old != new)
