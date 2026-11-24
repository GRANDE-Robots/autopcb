"""protection.py: independently compare protected native schematic and board content.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from collections import Counter
from dataclasses import dataclass

from core.kicad_sexpr import Form, parse
from pcb.move_scope import MoveScope
from pcb.route_validation import _canonical


class WorkingScope:
    """Reads the working scope that limits which nets a run may change."""

    def __call__(self, board) -> frozenset[str] | None:
        """Read the explicit WIP scope; this never supplies electrical evidence."""
        from core.paths import config_path
        from pcb.checkpoints import read_config

        filename = board.GetFileName()
        if not isinstance(filename, str) or not filename:
            return None
        policy = config_path(Path(filename).resolve().parent, "autopcb.json")
        if not policy.is_file():
            return None
        settings, _ = read_config(policy)
        scope = settings.get("working_scope")
        return frozenset(value.upper() for value in scope["classes"]) if scope else None

    @staticmethod
    def exclusions(board, classes: frozenset[str]) -> set[str]:
        """Keep unnamed, NC, critical-pair and out-of-scope nets protected."""
        result = set()
        for raw in board.GetNetsByName():
            name = str(raw)
            if not name:
                continue
            net = board.FindNet(name)
            netclass = str(net.GetNetClassName()).upper() if net is not None else ""
            if (
                name.startswith("unconnected-")
                or netclass not in classes
                or netclass in {"USB_DATA", "ETH_DATA"}
            ):
                result.add(name)
        return result


working_connectivity_classes = WorkingScope()


class ProtectionNets:
    """Names the nets that must stay unchanged because they carry power, monitoring or actuator-safety functions."""

    def __call__(self, board) -> set[str]:
        """Identify power, monitoring and actuator-safety nets conservatively."""
        classes = working_connectivity_classes(board)
        if classes is not None:
            return WorkingScope.exclusions(board, classes)
        result = set()
        for part in board.GetFootprints():
            ref, value = str(part.GetReference()), str(part.GetValue())
            description = str(part.GetFieldsText().get("Description", ""))
            # Transistor terminals may carry enable, gate, supply or return paths
            # even when their net names contain none of those words.  Defer their
            # copper and connected placement until a branch model qualifies them.
            protected_ref = bool(re.fullmatch(r"[DFQ][0-9]+", ref))
            protected_value = bool(
                re.search(
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
                    f"{value} {description}",
                    flags=re.IGNORECASE,
                )
            )
            if protected_ref or protected_value:
                result.update(
                    str(pad.GetNetname()) for pad in part.Pads() if pad.GetNetname()
                )
        for native_name in board.GetNetsByName():
            name = str(native_name)
            safety_input = re.search(
                r"(?:^|_)(?:JOG|LIMIT|ESTOP|ENC|ENCODER|VBUS|FIELD_VALID)(?:_|$)",
                name,
                flags=re.IGNORECASE,
            )
            actuator = re.search(
                r"(?:^|_)(?:BTS|MOTOR|LAMP|LIGHT|LIGHTS|TOP|FRONT)(?:_|$)",
                name,
                flags=re.IGNORECASE,
            )
            control = re.search(
                r"(?:^|_)(?:PWM|EN|ENABLE|GATE|CURRENT|SENSE|IS|FAULT)(?:_|$)",
                name,
                flags=re.IGNORECASE,
            )
            if safety_input or (actuator and control):
                result.add(name)
        return result


protection_nets = ProtectionNets()


class UnqualifiedWorkingNets:
    """Names the nets that stay out of unqualified working saves."""

    def __call__(self, board, excluded: tuple[str, ...] = ()) -> set[str]:
        """Keep protected and non-signal classes out of unqualified working saves."""
        classes = working_connectivity_classes(board)
        if classes is not None:
            return WorkingScope.exclusions(board, classes) | set(excluded)
        result = protection_nets(board) | set(excluded)
        for raw in board.GetNetsByName():
            name = str(raw)
            if not name:
                continue
            net = board.FindNet(name)
            netclass = str(net.GetNetClassName()).upper() if net is not None else ""
            if not (netclass == "SIGNAL" or netclass.endswith("_SIGNAL")) or re.search(
                r"(?:^|_)(?:POWER|GROUND|GND)(?:_|$)", netclass
            ):
                result.add(name)
        return result


unqualified_working_nets = UnqualifiedWorkingNets()


class ProtectedCopperUnchanged:
    """Compares protected track and via geometry before and after a change."""

    def __call__(self, before: list, after: list, excluded: tuple[str, ...]) -> bool:
        """Compare protected track and via geometry without relying on object order."""
        names = set(excluded)
        return Counter(item for item in before if item.net in names) == Counter(
            item for item in after if item.net in names
        )


protected_copper_unchanged = ProtectedCopperUnchanged()


class ProtectedFootprintChanges:
    """Finds footprint changes on nets that lack a qualified working-save model."""

    def __call__(
        self,
        before_board,
        after_board,
        before: bytes,
        after: bytes,
        excluded: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Find footprint changes on nets that lack a qualified working-save model."""
        nets = unqualified_working_nets(
            before_board, excluded
        ) | unqualified_working_nets(after_board, excluded)
        protected = {}
        for board in (before_board, after_board):
            for part in board.GetFootprints():
                if any(
                    str(pad.GetNetname()) in nets
                    for pad in part.Pads()
                    if pad.GetNetname()
                ):
                    protected[part.m_Uuid.AsString()] = str(part.GetReference())

        def footprints(data: bytes) -> dict:
            """Return each footprint's canonical form by UUID; raise ValueError for a non-PCB file or a repeated UUID."""
            root = parse(data.decode("utf-8-sig"))
            if root.name != "kicad_pcb":
                raise ValueError("Expected a native KiCad PCB")
            result = {}
            for item in root.children("footprint"):
                uid = ProtectedState.form_field(item, "uuid")
                if uid in result:
                    raise ValueError("Duplicate footprint UUID prevents comparison")
                result[uid] = _canonical(item)
            return result

        old, new = footprints(before), footprints(after)
        return tuple(
            sorted(
                protected[uid]
                for uid in protected
                if uid not in old or uid not in new or old[uid] != new[uid]
            )
        )


protected_footprint_changes = ProtectedFootprintChanges()


@dataclass(frozen=True)
class ProtectedState:
    """Design items a run must leave unchanged.

    Footprints that ``movable`` permits may move, with their silkscreen. Every
    other footprint is fixed, and without a scope all placement is fixed.
    """

    schematic_sha256: str
    fixed_footprints: tuple[tuple[str, str], ...]
    front_silkscreen: tuple[tuple[str, str], ...]
    outline: tuple[tuple[str, str], ...]
    stackup: str
    movable: MoveScope | None = None

    @classmethod
    def capture(
        cls,
        board_bytes: bytes,
        schematic_bytes: bytes,
        *,
        movable: MoveScope | None = None,
    ) -> ProtectedState:
        """Record the protected items of a board and schematic.

        Inputs:
            board_bytes: Native KiCad PCB bytes.
            schematic_bytes: Native schematic bytes.
            movable: Footprints allowed to move, or None to fix all placement.
        Outputs:
            The captured state. Raises ValueError when the PCB does not hold exactly
            one layers section, setup and stackup, or a footprint has an unsupported
            side, no single position or a repeated UUID.
        """
        text = board_bytes.decode("utf-8-sig")
        board = parse(text)
        if board.name != "kicad_pcb":
            raise ValueError("Expected a native KiCad PCB")
        layers = board.children("layers")
        setup = board.children("setup")
        if len(layers) != 1 or len(setup) != 1:
            raise ValueError("PCB layers and setup must each occur exactly once")
        stackups = setup[0].children("stackup")
        if len(stackups) != 1:
            raise ValueError("A single explicit PCB stackup is required")

        fixed: dict[str, str] = {}
        silk: dict[str, str] = {}
        outline: dict[str, str] = {}
        for item in board.children():
            if item.name == "footprint":
                uid = ProtectedState.form_field(item, "uuid")
                layer = ProtectedState.form_layer(item)
                if layer not in {"F.Cu", "B.Cu"}:
                    raise ValueError(f"Unsupported footprint side: {layer}")
                if len(item.children("at")) != 1:
                    raise ValueError(f"Footprint {uid} must have one position")
                if movable is None or movable.refusal(text, item):
                    ProtectedState.insert_unique(fixed, uid, _canonical(item))
            elif ProtectedState.form_layer(item) == "F.SilkS":
                ProtectedState.insert_unique(
                    silk, ProtectedState.form_field(item, "uuid"), _canonical(item)
                )
            elif ProtectedState.form_layer(item) == "Edge.Cuts":
                ProtectedState.insert_unique(
                    outline, ProtectedState.form_field(item, "uuid"), _canonical(item)
                )
        return cls(
            hashlib.sha256(schematic_bytes).hexdigest(),
            tuple(sorted(fixed.items())),
            tuple(sorted(silk.items())),
            tuple(sorted(outline.items())),
            ProtectedState.digest((_canonical(layers[0]), _canonical(stackups[0]))),
            movable,
        )

    def differences(
        self, board_bytes: bytes, schematic_bytes: bytes
    ) -> tuple[str, ...]:
        """Name the protected groups that differ in a new board and schematic.

        Inputs:
            board_bytes: Native KiCad PCB bytes.
            schematic_bytes: Native schematic bytes.
        Outputs:
            Names among schematic, fixed footprints, front silkscreen, outline and
            stackup; empty when nothing protected changed.
        """
        other = self.capture(board_bytes, schematic_bytes, movable=self.movable)
        pairs = (
            ("schematic", self.schematic_sha256, other.schematic_sha256),
            ("fixed footprints", self.fixed_footprints, other.fixed_footprints),
            ("front silkscreen", self.front_silkscreen, other.front_silkscreen),
            ("outline", self.outline, other.outline),
            ("stackup", self.stackup, other.stackup),
        )
        return tuple(name for name, old, new in pairs if old != new)

    @staticmethod
    def digest(value: object) -> str:
        """Return the SHA-256 of a value's text representation."""
        return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()

    @staticmethod
    def form_field(form: Form, name: str) -> str:
        """Return the value of the form's only child with this name; raise ValueError unless there is exactly one with one value."""
        children = form.children(name)
        if len(children) != 1 or len(children[0].atoms()) != 2:
            raise ValueError(f"Expected one {name} in {form.name}")
        return children[0].atoms()[1].value

    @staticmethod
    def form_layer(form: Form) -> str | None:
        """Return the form's layer name, or None when it has no layer child."""
        values = form.children("layer")
        if not values:
            return None
        return ProtectedState.form_field(form, "layer")

    @staticmethod
    def insert_unique(target: dict, key: object, value: object) -> None:
        """Store the digest of a value under a key; raise ValueError when the key is already present."""
        if key in target:
            raise ValueError(f"Duplicate protected board object: {key}")
        target[key] = ProtectedState.digest(value)
