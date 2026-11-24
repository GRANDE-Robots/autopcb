#!/usr/bin/env python3
"""standardize_block_symbols.py: convert J/U/block-D symbols to a one-sided style.

The script edits only a KiCad ``.kicad_sch`` file.  It preserves every target
reference, pin number, pin electrical type, component field value, footprint,
and net name supplied by a pre-change KiCad XML netlist.  Every connected pin
receives a one-grid leftward wire stub and matching global label; intentionally
unconnected pins retain a no-connect marker.  The former pin endpoint also
receives its net label before the symbol moves, so untouched existing wiring
keeps its original electrical identity.  This deliberately avoids deleting a
whole wire segment that happens to meet a pin: those segments can contain
unrelated crossings or junctions.

Use ``--check`` first, export a native pre-change netlist, then use ``--apply``.
The write is atomic.  This is a schematic-presentation operation only: it does
not edit the PCB, drawing sheet, page settings, footprint, BOM, or libraries.

The module also holds the S-expression and embedded-symbol readers that the
other schematic tools in this package share.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import uuid
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from sch.schematic_io import AtomicTextWriter, FileDigest

POINT_PRECISION = 6
TEXT_SIZE = 0.762
# KiCad's connection grid for this project is 50 mil (1.27 mm).  Generated
# symbol origins, pins, stubs, labels, and fields must all land on it.
NATIVE_GRID = 1.27
PIN_X = -5.08
BODY_LEFT = -2.54
# Compact, grid-aligned body rules.  Every terminal name is visible, so width
# is calculated per library from its longest rendered terminal name.  Pin names
# are overwhelmingly uppercase identifiers with underscores; using an average
# glyph advance made a few dense blocks visually too narrow.  Treat every
# character as a near-full-height glyph instead and leave 200 mil of internal
# clearance.  The result is a uniform, safe per-family width rule: a body is
# exactly as narrow as its longest displayed terminal permits, never narrower.
MIN_BODY_WIDTH = 8.89
HIDDEN_NAME_BODY_WIDTH = 6.35
PIN_NAME_GLYPH_WIDTH = 0.90
BODY_TEXT_MARGIN = 5.08
# One hundred mil is two native cells: it leaves a real clear gap around
# 30-mil labels while remaining valid for every local pin, endpoint, label,
# and one-grid wire stub.
PIN_PITCH = 2.54
PIN_NAME_TEXT_SIZE = TEXT_SIZE
STUB_LENGTH = 1.27
FIELD_GAP = 1.27
SPATIAL_CELL = 10.16
# A1 landscape, preserving the existing drawing sheet.  These are visual
# placement bounds only; no page setting or title-block geometry is changed.
PAGE_LEFT = 10.0
PAGE_TOP = 10.0
PAGE_RIGHT = 831.0
PAGE_BOTTOM = 570.0
TITLE_BLOCK_LEFT = 690.0
TITLE_BLOCK_TOP = 520.0
# A normalizer may reorganize symbol geometry, but must not silently relocate a
# block across the drawing into a different subsystem.  Larger intentional page
# reflows need their own explicit section-layout rule set.
DEFAULT_MAX_TRAVEL_MM = 127.0
# U3 is intentionally handled by the companion folded-Teensy renderer.  A
# 46-pin one-column block would consume the vertical space of the adjacent
# LiDAR section; the companion preserves the same left-facing pin convention
# in two visually linked columns without changing the physical U3/BOM item.
EXCLUDED_REFERENCES = {"U3"}
# The physical Teensy remains one U3/one BOM item.  Its visual symbol folds
# into two equally tall left-facing columns, which keeps 30-mil net labels at
# the required 100-mil pitch while preserving the Section 2 vertical envelope.
FOLDED_LIB_IDS = {"V6_Generated:U2"}
FOLDED_COLUMN_OFFSET = 60.96

AT_RE = re.compile(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)(?:\s+(-?[\d.]+))?\)")
XY_RE = re.compile(r"\(xy\s+(-?[\d.]+)\s+(-?[\d.]+)\)")


class BalancedEnd:
    """Finds where one quote-safe S-expression ends."""

    def __call__(self, source: str, start: int) -> int:
        """Return the exclusive end index of the S-expression that opens at ``start``.

        Raises ValueError when ``source[start]`` is not ``(`` or the expression is
        not closed.
        """
        if source[start] != "(":
            raise ValueError(f"expected '(' at {start}")
        depth = 0
        in_quote = False
        escaped = False
        for pos in range(start, len(source)):
            char = source[pos]
            if in_quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_quote = False
                continue
            if char == '"':
                in_quote = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return pos + 1
        raise ValueError(f"unclosed expression at {start}")


balanced_end = BalancedEnd()


class SExpressionText:
    """Slices and re-renders KiCad S-expression text without a full parse."""

    @staticmethod
    def direct_blocks(source: str, token: str) -> list[tuple[int, int]]:
        """Find direct children whose text starts with ``(token``.

        Inputs:
            source: One complete S-expression.
            token: Child name, without the opening parenthesis.
        Outputs:
            The (start, end) offsets of each such child at depth one, in source
            order.
        """
        marker = f"({token}"
        result: list[tuple[int, int]] = []
        depth = 0
        in_quote = False
        escaped = False
        pos = 0
        while pos < len(source):
            char = source[pos]
            if in_quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_quote = False
                pos += 1
                continue
            if char == '"':
                in_quote = True
                pos += 1
                continue
            if depth == 1 and source.startswith(marker, pos):
                end = balanced_end(source, pos)
                result.append((pos, end))
                pos = end
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            pos += 1
        return result

    @staticmethod
    def paren_delta(text: str) -> int:
        """Count structural parentheses in one S-expression line, quote-safe."""
        delta = 0
        quoted = False
        escaped = False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == '"':
                quoted = True
            elif char == "(":
                delta += 1
            elif char == ")":
                delta -= 1
        return delta

    @classmethod
    def reindent(cls, block: str, base_indent: str) -> str:
        """Render one extracted S-expression with canonical local indentation.

        Inputs:
            block: One complete S-expression.
            base_indent: Text placed before every rendered line.
        Outputs:
            The block with one tab per nesting level after ``base_indent``.
            Raises ValueError when the block is unbalanced.
        """
        depth = 0
        rendered: list[str] = []
        for source_line in block.splitlines():
            line = source_line.strip()
            if not line:
                continue
            leading_close = len(line) - len(line.lstrip(")"))
            line_depth = max(0, depth - leading_close)
            rendered.append(base_indent + "\t" * line_depth + line)
            depth += cls.paren_delta(line)
            if depth < 0:
                raise ValueError("unbalanced pin S-expression while formatting")
        if depth:
            raise ValueError("unterminated pin S-expression while formatting")
        return "\n".join(rendered)

    @staticmethod
    def quote(value: str) -> str:
        """Escape backslashes and double quotes for use inside a quoted string."""
        return value.replace("\\", "\\\\").replace('"', '\\"')


class PointKey:
    """Rounds a coordinate pair so equal schematic points compare equal."""

    def __call__(self, x: float, y: float) -> tuple[float, float]:
        """Return (x, y) rounded to ``POINT_PRECISION`` decimal places."""
        return (round(x, POINT_PRECISION), round(y, POINT_PRECISION))


point_key = PointKey()


class NumberFormat:
    """Formats numbers the way KiCad writes coordinates and sizes."""

    def __call__(self, value: float) -> str:
        """Return ``value`` with at most six decimals and no trailing zeros.

        Magnitudes below 1e-5 print as 0.
        """
        if abs(value) < 10 ** (-(POINT_PRECISION - 1)):
            value = 0.0
        return f"{value:.6f}".rstrip("0").rstrip(".")


fmt = NumberFormat()


class TopLevelBlocks:
    """Lists the direct children of a schematic root by token."""

    def __call__(self, root: str, token: str) -> list[tuple[int, int, str]]:
        """Return (start, end, text) for each direct child opened by ``(token``."""
        return [
            (start, end, root[start:end])
            for start, end in SExpressionText.direct_blocks(root, token)
        ]


top_level = TopLevelBlocks()


class TopLevelPoints:
    """Reads the coordinates a top-level schematic object is anchored at."""

    def __call__(self, block: str, kind: str) -> list[tuple[float, float]]:
        """Return the points of one object.

        Inputs:
            block: Text of one top-level object.
            kind: The object's token; ``wire`` yields every ``(xy ...)`` point,
                any other kind yields its first ``(at ...)`` point.
        Outputs:
            Unrounded (x, y) pairs in mm; empty when the object has no position.
        """
        if kind == "wire":
            return [
                (float(match.group(1)), float(match.group(2)))
                for match in XY_RE.finditer(block)
            ]
        match = AT_RE.search(block)
        return [(float(match.group(1)), float(match.group(2)))] if match else []


top_level_points = TopLevelPoints()


class PinNumbers:
    """Orders pin numbers the way a reader counts them."""

    @staticmethod
    def natural_key(value: str) -> tuple[object, ...]:
        """Sort key that compares digit runs as numbers and text case-insensitively."""
        result: list[object] = []
        for part in re.split(r"(\d+)", value):
            result.append(int(part) if part.isdigit() else part.casefold())
        return tuple(result)


@dataclass(frozen=True)
class PinTemplate:
    """One pin read from an embedded symbol definition.

    Inputs:
        number: Pin number.
        name: Visible terminal name, or ``PIN_<number>`` when the library has none.
        x, y: Local pin endpoint in mm (symbol coordinates, Y up).
        angle: Pin direction in degrees, 0 to 359.
        block: The complete ``(pin ...)`` text.
    """

    number: str
    name: str
    x: float
    y: float
    angle: int
    block: str


@dataclass(frozen=True)
class SymbolInstance:
    """One placed symbol of the sheet.

    Inputs:
        start, end: Offsets of the ``(symbol ...)`` block in the schematic root.
        block: The complete block text.
        reference: Reference designator.
        lib_id: Library identifier, ``nickname:name``.
        x, y: Placement in mm.
        rotation: Placement angle in degrees, 0 to 359.
        mirror: ``x``, ``y`` or None.
        unit: Unit number.
    """

    start: int
    end: int
    block: str
    reference: str
    lib_id: str
    x: float
    y: float
    rotation: int
    mirror: str | None
    unit: int

    def pin_point(self, pin: PinTemplate) -> tuple[float, float]:
        """Sheet coordinate in mm of ``pin`` for this placement, as a point key."""
        return point_key(
            *local_screen_point(
                self.x, self.y, self.rotation, self.mirror, pin.x, pin.y
            )
        )


@dataclass(frozen=True)
class PinMove:
    """One pin's former and new endpoint on the sheet.

    Inputs:
        instance: The symbol the pin belongs to.
        number: Pin number.
        old, new: Endpoint in mm before and after the conversion.
        net: Net name from the netlist, or None when the netlist omits the pin.
    """

    instance: SymbolInstance
    number: str
    old: tuple[float, float]
    new: tuple[float, float]
    net: str | None


@dataclass(frozen=True)
class SymbolGeometry:
    """Local body envelope for one normalized library definition."""

    left: float
    top: float
    right: float
    bottom: float
    folded: bool = False


class ScreenPoint:
    """Maps a symbol-local point onto the sheet."""

    def __call__(
        self,
        x: float,
        y: float,
        rotation: int,
        mirror: str | None,
        lx: float,
        ly: float,
    ) -> tuple[float, float]:
        """Map a symbol-local (Y-up) pin endpoint into KiCad sheet coordinates.

        Inputs:
            x, y: Symbol placement in mm.
            rotation: Placement angle in degrees; 0, 90, 180 or 270.
            mirror: ``x``, ``y`` or None.
            lx, ly: Local endpoint in mm.
        Outputs:
            The sheet coordinate in mm. Raises ValueError for any other rotation.
        """
        if mirror == "x":
            ly = -ly
        elif mirror == "y":
            lx = -lx
        if rotation == 0:
            return x + lx, y - ly
        if rotation == 90:
            return x - ly, y - lx
        if rotation == 180:
            return x - lx, y + ly
        if rotation == 270:
            return x + ly, y + lx
        raise ValueError(f"unsupported symbol rotation {rotation}")


local_screen_point = ScreenPoint()


class LibraryUnits:
    """Reads the pin-bearing units of an embedded library symbol."""

    @staticmethod
    def terminal_name(block: str, number: str) -> str:
        """Return a visible, stable terminal name for every normalized pin."""
        match = re.search(r'\(name\s+"([^"]*)"', block)
        if match and match.group(1).strip() not in {"", "~"}:
            return match.group(1)
        return f"PIN_{number}"

    @classmethod
    def direct_pin_blocks(cls, unit_block: str) -> list[PinTemplate]:
        """Read every ``(pin ...)`` child of one unit that has a position and number."""
        result: list[PinTemplate] = []
        for start, end in SExpressionText.direct_blocks(unit_block, "pin"):
            block = unit_block[start:end]
            at = re.match(
                r"\(pin\s+\S+\s+\S+\s+\(at\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\)",
                block,
            )
            number = re.search(r'\(number\s+"([^"]+)"', block)
            if not (at and number):
                continue
            result.append(
                PinTemplate(
                    number=number.group(1),
                    name=cls.terminal_name(block, number.group(1)),
                    x=float(at.group(1)),
                    y=float(at.group(2)),
                    angle=int(float(at.group(3))) % 360,
                    block=block,
                )
            )
        return result

    def __call__(self, lib_block: str) -> dict[int, tuple[str, list[PinTemplate]]]:
        """Read the pins of each unit of one library symbol.

        Inputs:
            lib_block: Text of one embedded ``(symbol "<lib_id>" ...)`` definition.
        Outputs:
            Unit number mapped to the unit's child text and its pins. Pins that
            KiCad keeps in unit 0 are merged into every unit. Units without pins
            are omitted.
        """
        raw: dict[int, tuple[str, list[PinTemplate]]] = {}
        for start, end in SExpressionText.direct_blocks(lib_block, "symbol"):
            child = lib_block[start:end]
            name = re.match(r'\(symbol\s+"([^"]+)"', child)
            if not name:
                continue
            unit = re.search(r"_(\d+)_[0-9]+$", name.group(1))
            if not unit:
                continue
            pins = self.direct_pin_blocks(child)
            if pins:
                raw[int(unit.group(1))] = (child, pins)
        # KiCad keeps shared pins (for example a TVS-array ground pin) in unit 0.
        # They are electrically present on every physical unit instance, so make
        # them explicit before rebuilding a one-sided unit.
        common = raw.get(0, ("", []))[1]
        result: dict[int, tuple[str, list[PinTemplate]]] = {}
        for unit, (child, pins) in raw.items():
            if unit == 0:
                continue
            merged = {pin.number: pin for pin in common}
            merged.update({pin.number: pin for pin in pins})
            result[unit] = (child, list(merged.values()))
        return result


library_units = LibraryUnits()


class EmbeddedDefinitions:
    """Locates the embedded library symbols of a schematic root."""

    def __call__(self, root: str) -> tuple[int, int, dict[str, tuple[int, int, str]]]:
        """Find the ``(lib_symbols ...)`` block and each symbol definition in it.

        Inputs:
            root: Text of the ``(kicad_sch ...)`` root.
        Outputs:
            The block's start and end offsets in ``root`` and, for each library
            ID, the definition's start offset, end offset and text. Raises
            ValueError when the root has no ``lib_symbols`` block.
        """
        start = root.find("(lib_symbols")
        if start < 0:
            raise ValueError("missing lib_symbols block")
        end = balanced_end(root, start)
        lib_source = root[start:end]
        definitions: dict[str, tuple[int, int, str]] = {}
        for child_start, child_end in SExpressionText.direct_blocks(
            lib_source, "symbol"
        ):
            block = lib_source[child_start:child_end]
            match = re.match(r'\(symbol\s+"([^"]+)"', block)
            if match:
                definitions[match.group(1)] = (
                    start + child_start,
                    start + child_end,
                    block,
                )
        return start, end, definitions


source_lib_definitions = EmbeddedDefinitions()


class PlacedSymbols:
    """Reads the symbols placed on a schematic sheet."""

    def __call__(self, root: str) -> list[SymbolInstance]:
        """Return each placed symbol that has a library ID, reference, position, unit."""
        result: list[SymbolInstance] = []
        for start, end in SExpressionText.direct_blocks(root, "symbol"):
            block = root[start:end]
            lib = re.search(r'\(lib_id\s+"([^"]+)"\)', block)
            ref = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', block)
            at = re.search(
                r'\(lib_id\s+"[^"]+"\)\s*\(at\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\)',
                block,
            )
            unit = re.search(r"\(unit\s+(\d+)\)", block)
            mirror = re.search(r"\(mirror\s+([xy])\)", block)
            if not (lib and ref and at and unit):
                continue
            result.append(
                SymbolInstance(
                    start=start,
                    end=end,
                    block=block,
                    reference=ref.group(1),
                    lib_id=lib.group(1),
                    x=float(at.group(1)),
                    y=float(at.group(2)),
                    rotation=int(float(at.group(3))) % 360,
                    mirror=mirror.group(1) if mirror else None,
                    unit=int(unit.group(1)),
                )
            )
        return result


instances = PlacedSymbols()


class SheetPoints:
    """Finds the points of a sheet where pins and other objects attach."""

    @staticmethod
    def placed_pins(
        root: str,
    ) -> list[tuple[SymbolInstance, PinTemplate, tuple[float, float]]]:
        """Return (symbol, pin, endpoint) for each pin of each placed symbol.

        Symbols whose definition or unit is not embedded are skipped.
        """
        _start, _end, definitions = source_lib_definitions(root)
        result = []
        for instance in instances(root):
            definition = definitions.get(instance.lib_id)
            if definition is None:
                continue
            unit = library_units(definition[2]).get(instance.unit)
            if unit is None:
                continue
            for pin in unit[1]:
                result.append((instance, pin, instance.pin_point(pin)))
        return result

    @classmethod
    def pin_points(cls, root: str) -> set[tuple[float, float]]:
        """Return every pin endpoint of every placed symbol, in mm."""
        return {point for _instance, _pin, point in cls.placed_pins(root)}

    @staticmethod
    def anchored(root: str, token: str) -> set[tuple[float, float]]:
        """Return the anchor point of each top-level ``token`` object that has one."""
        result = set()
        for _start, _end, block in top_level(root, token):
            points = top_level_points(block, token)
            if points:
                result.add(point_key(*points[0]))
        return result


class NetlistPins:
    """Reads pin-to-net assignments from a KiCad XML netlist."""

    @staticmethod
    def read(netlist: Path) -> dict[tuple[str, str], str]:
        """Map (reference, pin number) to the net the netlist places it on.

        Raises ValueError when a pin appears in two different nets.
        """
        document = ET.parse(netlist)
        mapping: dict[tuple[str, str], str] = {}
        for net in document.findall("./nets/net"):
            name = net.attrib.get("name", "")
            for node in net.findall("node"):
                reference = node.attrib.get("ref")
                number = node.attrib.get("pin")
                if reference and number:
                    key = (reference, number)
                    prior = mapping.get(key)
                    if prior and prior != name:
                        raise ValueError(f"pin {reference}/{number} occurs in two nets")
                    mapping[key] = name
        return mapping


class BlockTargets:
    """Decides which placed symbols are rebuilt as one-sided blocks.

    Inputs:
        only_references: When given, exactly these references are targets;
            otherwise every J, U and non-``Device:`` D symbol except the excluded
            controller.
    """

    def __init__(self, only_references: set[str] | None = None) -> None:
        """Remember the optional reference filter."""
        self.only_references = only_references

    def __call__(self, reference: str, lib_id: str) -> bool:
        """Whether the symbol with this reference and library ID is a target."""
        if self.only_references is not None:
            return reference in self.only_references
        return reference not in EXCLUDED_REFERENCES and (
            reference.startswith(("J", "U"))
            or (reference.startswith("D") and not lib_id.startswith("Device:"))
        )


is_target = BlockTargets()


class PinLayout:
    """Places the pins and body of a normalized one-sided symbol on the grid.

    Inputs:
        pin_pitch_mm: Row pitch of the pin column in mm. The command line
            accepts only positive multiples of the native grid.
    """

    def __init__(self, pin_pitch_mm: float = PIN_PITCH) -> None:
        """Remember the pin pitch."""
        self.pin_pitch_mm = pin_pitch_mm

    def pin_top(self, count: int) -> float:
        """Grid-aligned local Y coordinate for the first pin in a one-side list."""
        nominal = (count - 1) * self.pin_pitch_mm / 2
        return math.ceil(nominal / NATIVE_GRID - 1e-9) * NATIVE_GRID

    def pin_body_half(self, count: int) -> float:
        """Symmetric body extent that encloses a grid-aligned pin list."""
        top = self.pin_top(count)
        bottom = top - (count - 1) * self.pin_pitch_mm
        return max(abs(top), abs(bottom)) + 1.27

    @staticmethod
    def body_right_for(max_name_length: int, pin_names_visible: bool) -> float:
        """Fit only the text that is actually rendered inside a 30-mil body."""
        required = (
            max(
                MIN_BODY_WIDTH,
                max_name_length * TEXT_SIZE * PIN_NAME_GLYPH_WIDTH + BODY_TEXT_MARGIN,
            )
            if pin_names_visible
            else HIDDEN_NAME_BODY_WIDTH
        )
        width = math.ceil(required / NATIVE_GRID - 1e-9) * NATIVE_GRID
        return BODY_LEFT + width

    def geometry_for(
        self,
        lib_id: str,
        pin_count: int,
        max_name_length: int,
        pin_names_visible: bool,
    ) -> SymbolGeometry:
        """Body envelope of one normalized symbol, folded for the controller."""
        body_right = self.body_right_for(max_name_length, pin_names_visible)
        if lib_id in FOLDED_LIB_IDS:
            rows = math.ceil(pin_count / 2)
            half = self.pin_body_half(rows)
            return SymbolGeometry(
                BODY_LEFT, half, body_right + FOLDED_COLUMN_OFFSET, -half, True
            )
        half = self.pin_body_half(pin_count)
        return SymbolGeometry(BODY_LEFT, half, body_right, -half)

    def normalized_pin_position(
        self, lib_id: str, pin_count: int, index: int
    ) -> tuple[float, float]:
        """Return a grid-aligned local endpoint for the naturally sorted pin."""
        if lib_id in FOLDED_LIB_IDS:
            rows = math.ceil(pin_count / 2)
            column, row = divmod(index, rows)
            return (
                PIN_X + column * FOLDED_COLUMN_OFFSET,
                self.pin_top(rows) - row * self.pin_pitch_mm,
            )
        return PIN_X, self.pin_top(pin_count) - index * self.pin_pitch_mm


class NewSchematicItems:
    """Renders the wires, labels and markers this tool adds to the sheet."""

    @staticmethod
    def label_block(net: str, x: float, y: float, angle: int = 180) -> str:
        """Emit a named global label at an exact electrical endpoint.

        Inputs:
            net: Label text.
            x, y: Anchor in mm.
            angle: 180 for a left-facing, right-justified label; any other value
                gives a left-justified one.
        """
        justify = "right" if angle == 180 else "left"
        return (
            f'\t(global_label "{SExpressionText.quote(net)}"\n'
            "\t\t(shape bidirectional)\n"
            f"\t\t(at {fmt(x)} {fmt(y)} {angle})\n"
            "\t\t(effects\n"
            "\t\t\t(font\n"
            f"\t\t\t\t(size {fmt(TEXT_SIZE)} {fmt(TEXT_SIZE)})\n"
            "\t\t\t)\n"
            f"\t\t\t(justify {justify})\n"
            "\t\t)\n"
            f'\t\t(uuid "{uuid.uuid4()}")\n'
            "\t)\n"
        )

    @staticmethod
    def wire_block(start: tuple[float, float], end: tuple[float, float]) -> str:
        """Emit a straight wire between two points."""
        return (
            "\t(wire\n"
            "\t\t(pts\n"
            f"\t\t\t(xy {fmt(start[0])} {fmt(start[1])}) (xy {fmt(end[0])} {fmt(end[1])})\n"
            "\t\t)\n"
            "\t\t(stroke\n"
            "\t\t\t(width 0)\n"
            "\t\t\t(type default)\n"
            "\t\t)\n"
            f'\t\t(uuid "{uuid.uuid4()}")\n'
            "\t)\n"
        )

    @staticmethod
    def no_connect_block(point: tuple[float, float]) -> str:
        """Emit a no-connect marker at a pin endpoint."""
        return (
            "\t(no_connect\n"
            f"\t\t(at {fmt(point[0])} {fmt(point[1])})\n"
            f'\t\t(uuid "{uuid.uuid4()}")\n'
            "\t)\n"
        )


class BlockSymbolWriter:
    """Rebuilds embedded symbol definitions and placed symbols in the one-sided style.

    Inputs:
        pin_pitch_mm: Row pitch of the generated pin column in mm.
    """

    def __init__(self, pin_pitch_mm: float = PIN_PITCH) -> None:
        """Create the pin layout used for generated symbols."""
        self.layout = PinLayout(pin_pitch_mm)

    @staticmethod
    def named_pin_block(name: str) -> str:
        """Render a ``(name ...)`` field in the standard terminal text size."""
        return (
            f'(name "{SExpressionText.quote(name)}"\n'
            "\t(effects\n"
            "\t\t(font\n"
            f"\t\t\t(size {fmt(PIN_NAME_TEXT_SIZE)} {fmt(PIN_NAME_TEXT_SIZE)})\n"
            "\t\t)\n"
            "\t)\n"
            ")"
        )

    @classmethod
    def replace_pin_at(cls, block: str, x: float, y: float, display_name: str) -> str:
        """Move one pin to a left-facing endpoint and restyle its name and number.

        Inputs:
            block: Text of one ``(pin ...)`` block.
            x, y: New local endpoint in mm.
            display_name: Terminal name to show.
        Outputs:
            The pin text with angle 0, the name and number text at their
            standard sizes, and a name field added when the pin had none.
            Raises ValueError when the pin has no ``(at ...)``.
        """
        replacement = f"(at {fmt(x)} {fmt(y)} 0)"
        updated, count = AT_RE.subn(replacement, block, count=1)
        if count != 1:
            raise ValueError("pin block lacks at coordinate")
        replacements: list[tuple[int, int, str]] = []
        name_spans = SExpressionText.direct_blocks(updated, "name")
        for kind, size in (("name", PIN_NAME_TEXT_SIZE), ("number", TEXT_SIZE)):
            for start, end in SExpressionText.direct_blocks(updated, kind):
                field = updated[start:end]
                if kind == "name":
                    field = re.sub(
                        r'^\(name\s+"[^"]*"',
                        f'(name "{SExpressionText.quote(display_name)}"',
                        field,
                        count=1,
                    )
                normalized = re.sub(
                    r"\(size\s+-?[\d.]+\s+-?[\d.]+\)",
                    f"(size {fmt(size)} {fmt(size)})",
                    field,
                )
                replacements.append((start, end, normalized))
        for start, end, normalized in reversed(replacements):
            updated = updated[:start] + normalized + updated[end:]
        if not name_spans:
            insert = updated.rfind(")")
            updated = (
                updated[:insert]
                + "\n\t"
                + cls.named_pin_block(display_name)
                + updated[insert:]
            )
        return updated

    @staticmethod
    def body_rectangle(left: float, top: float, right: float, bottom: float) -> str:
        """Render one filled body rectangle from its corners."""
        return (
            "\t\t\t\t(rectangle\n"
            f"\t\t\t\t\t(start {fmt(left)} {fmt(top)})\n"
            f"\t\t\t\t\t(end {fmt(right)} {fmt(bottom)})\n"
            "\t\t\t\t\t(stroke\n"
            "\t\t\t\t\t\t(width 0.254)\n"
            "\t\t\t\t\t\t(type default)\n"
            "\t\t\t\t\t)\n"
            "\t\t\t\t\t(fill\n"
            "\t\t\t\t\t\t(type background)\n"
            "\t\t\t\t\t)\n"
            "\t\t\t\t)"
        )

    @classmethod
    def generic_body(cls, name: str, geometry: SymbolGeometry) -> str:
        """Render the body-only graphic unit, with a second column when folded."""
        column_right = (
            geometry.right - FOLDED_COLUMN_OFFSET if geometry.folded else geometry.right
        )
        rectangles = [
            cls.body_rectangle(
                geometry.left, geometry.top, column_right, geometry.bottom
            )
        ]
        if geometry.folded:
            rectangles.append(
                cls.body_rectangle(
                    geometry.left + FOLDED_COLUMN_OFFSET,
                    geometry.top,
                    geometry.right,
                    geometry.bottom,
                )
            )
        return f'(symbol "{name}_0_1"\n' + "\n".join(rectangles) + "\n\t\t\t)"

    @staticmethod
    def set_symbol_text_visibility(block: str, token: str, visible: bool) -> str:
        """Show or hide a root ``pin_names``/``pin_numbers`` field uniformly.

        Raises ValueError when hiding a field that has no closing indentation line.
        """
        spans = SExpressionText.direct_blocks(block, token)
        if not spans:
            return block
        start, end = spans[0]
        field = re.sub(r"\s*\(hide\s+yes\)", "", block[start:end])
        field = re.sub(r"\n[ \t]*\n", "\n", field)
        if not visible:
            final_newline = field.rfind("\n")
            if final_newline < 0:
                raise ValueError(f"{token} lacks a closing indentation line")
            closing_indent = re.match(r"[ \t]*", field[final_newline + 1 :]).group(0)
            field = (
                field[:final_newline]
                + f"\n{closing_indent}\t(hide yes)"
                + field[final_newline:]
            )
        return block[:start] + field + block[end:]

    def generic_unit(self, child: str, pins: list[PinTemplate], lib_id: str) -> str:
        """Return a pin-only unit; no inherited text or graphics survive."""
        name = re.match(r'\(symbol\s+"([^"]+)"', child)
        if not name:
            raise ValueError("unit lacks a symbol name")
        ordered = sorted(pins, key=lambda pin: PinNumbers.natural_key(pin.number))
        updated_pins = [
            self.replace_pin_at(
                pin.block,
                *self.layout.normalized_pin_position(lib_id, len(ordered), index),
                pin.name,
            )
            for index, pin in enumerate(ordered)
        ]
        # Pin templates originate at different indentation depths in their source
        # libraries.  Re-indent from their S-expression structure so generated
        # symbols obey KiCad's normal nested layout as well as its electrical grid.
        indent = "\t\t\t\t"
        pin_text = "\n".join(
            SExpressionText.reindent(block, indent) for block in updated_pins
        )
        return f'(symbol "{name.group(1)}"\n{pin_text}\n\t\t\t)'

    def generic_definition(
        self, lib_id: str, block: str, pin_names_visible: bool
    ) -> tuple[str, dict[int, list[PinTemplate]], SymbolGeometry]:
        """Rebuild one embedded definition as a body plus one-sided pin units.

        Inputs:
            lib_id: Library identifier of the definition.
            block: Text of the embedded definition.
            pin_names_visible: Whether pin names are rendered.
        Outputs:
            The new definition text, the naturally sorted pins of each unit, and
            the body envelope. Raises ValueError when the definition has no
            pin-bearing units or no child symbols.
        """
        # Pin numbers remain visible and ordered for every normalized block.  Pin
        # names remain semantically present, but are rendered only when they add
        # information beyond the external net label.
        block = self.set_symbol_text_visibility(block, "pin_names", pin_names_visible)
        block = self.set_symbol_text_visibility(block, "pin_numbers", True)
        units = library_units(block)
        if not units:
            raise ValueError(f"{lib_id} has no pin-bearing units")
        maximum = max(len(pins) for _child, pins in units.values())
        maximum_name_length = max(
            len(pin.name) for _child, pins in units.values() for pin in pins
        )
        geometry = self.layout.geometry_for(
            lib_id, maximum, maximum_name_length, pin_names_visible
        )
        children = SExpressionText.direct_blocks(block, "symbol")
        if not children:
            raise ValueError(f"{lib_id} has no child symbols")
        base = lib_id.split(":", 1)[-1]
        replacement = [self.generic_body(base, geometry)]
        pin_templates: dict[int, list[PinTemplate]] = {}
        for unit in sorted(units):
            original_child, pins = units[unit]
            pin_templates[unit] = sorted(
                pins, key=lambda pin: PinNumbers.natural_key(pin.number)
            )
            replacement.append(self.generic_unit(original_child, pins, lib_id))
        start = children[0][0]
        end = children[-1][1]
        joined = "\n\t\t\t".join(replacement)
        return block[:start] + joined + block[end:], pin_templates, geometry

    @staticmethod
    def property_effects() -> str:
        """Render the ``(effects ...)`` of a right-justified Reference or Value."""
        return (
            "(effects\n"
            "\t\t\t\t(font\n"
            f"\t\t\t\t\t(size {fmt(TEXT_SIZE)} {fmt(TEXT_SIZE)})\n"
            "\t\t\t\t)\n"
            "\t\t\t\t(justify right)\n"
            "\t\t\t)"
        )

    @classmethod
    def restyle_property(cls, block: str, x: float, y: float) -> str:
        """Move a property to (x, y) at angle 0 and give it the standard effects.

        Raises ValueError when the property has no ``(at ...)`` or no effects.
        """
        updated, count = AT_RE.subn(f"(at {fmt(x)} {fmt(y)} 0)", block, count=1)
        if count != 1:
            raise ValueError("property lacks at coordinate")
        effects = SExpressionText.direct_blocks(updated, "effects")
        if not effects:
            raise ValueError("property lacks effects")
        start, end = effects[0]
        return updated[:start] + cls.property_effects() + updated[end:]

    @classmethod
    def generic_instance(
        cls, instance: SymbolInstance, geometry: SymbolGeometry, x: float, y: float
    ) -> str:
        """Re-place a symbol at (x, y) unmirrored and dock its visible fields.

        Inputs:
            instance: The placed symbol.
            geometry: Body envelope of its rebuilt definition.
            x, y: New placement in mm.
        Outputs:
            The symbol text at angle 0 with Reference above and Value below the
            body, right-aligned to it. Hidden fields are untouched. Raises
            ValueError when the placement cannot be located.
        """
        block = instance.block
        header = re.compile(
            r'(\(lib_id\s+"[^"]+"\)\s*)\(at\s+-?[\d.]+\s+-?[\d.]+\s+-?[\d.]+\)'
        )
        block, count = header.subn(
            lambda match: match.group(1) + f"(at {fmt(x)} {fmt(y)} 0)",
            block,
            count=1,
        )
        if count != 1:
            raise ValueError(f"cannot locate symbol placement for {instance.reference}")
        block = re.sub(r"\s*\(mirror\s+[xy]\)", "", block, count=1)
        replacements: list[tuple[int, int, str]] = []
        for start, end in SExpressionText.direct_blocks(block, "property"):
            field = block[start:end]
            name = re.match(r'\(property\s+"([^"]+)"', field)
            if not name or name.group(1) not in {"Reference", "Value"}:
                continue
            if re.search(r"\(hide\s+yes\)", field):
                continue
            field_y = (
                y - geometry.top - FIELD_GAP
                if name.group(1) == "Reference"
                else y - geometry.bottom + FIELD_GAP
            )
            replacements.append(
                (start, end, cls.restyle_property(field, x + geometry.right, field_y))
            )
        for start, end, replacement in reversed(replacements):
            block = block[:start] + replacement + block[end:]
        return block

    @classmethod
    def compact_body_fields(
        cls, instance: SymbolInstance, geometry: SymbolGeometry
    ) -> str:
        """Retain an instance and nets while docking visible fields to a new body."""
        replacements: list[tuple[int, int, str]] = []
        for start, end in SExpressionText.direct_blocks(instance.block, "property"):
            field = instance.block[start:end]
            name = re.match(r'\(property\s+"(Reference|Value)"', field)
            if not name or re.search(r"\(hide\s+yes\)", field):
                continue
            field_y = (
                instance.y - geometry.top - FIELD_GAP
                if name.group(1) == "Reference"
                else instance.y - geometry.bottom + FIELD_GAP
            )
            replacements.append(
                (
                    start,
                    end,
                    cls.restyle_property(field, instance.x + geometry.right, field_y),
                )
            )
        block = instance.block
        for start, end, replacement in reversed(replacements):
            block = block[:start] + replacement + block[end:]
        return block


class VisibleText:
    """Estimates the area visible text and symbol artwork cover on the sheet."""

    @staticmethod
    def visible_text(block: str, token: str) -> str | None:
        """Return the primary quoted text from a top-level KiCad text object."""
        match = re.match(rf'\({re.escape(token)}\s+"((?:[^"\\]|\\.)*)"', block)
        return (
            match.group(1).replace(r'\\"', '"').replace(r"\\\\", "\\")
            if match
            else None
        )

    @staticmethod
    def property_text(block: str, name: str) -> str | None:
        """Return the quoted value of the property called ``name``, or None."""
        match = re.search(
            rf'\(property\s+"{re.escape(name)}"\s+"((?:[^"\\]|\\.)*)"', block
        )
        return (
            match.group(1).replace(r'\\"', '"').replace(r"\\\\", "\\")
            if match
            else None
        )

    @staticmethod
    def visible_text_size(block: str) -> float:
        """Read a text object's font size, defaulting to the schematic style."""
        match = re.search(r"\(size\s+(-?[\d.]+)\s+(-?[\d.]+)\)", block)
        return max(float(match.group(1)), float(match.group(2))) if match else TEXT_SIZE

    @staticmethod
    def text_box(
        text: str,
        x: float,
        y: float,
        size: float,
        justify: str = "center",
        shaped: bool = False,
    ) -> tuple[float, float, float, float]:
        """Conservative visual envelope for KiCad text, used only for placement.

        Inputs:
            text: The displayed text.
            x, y: Anchor in mm.
            size: Font size in mm.
            justify: ``left``, ``right`` or ``center``.
            shaped: Whether the text sits in a label arrow body.
        Outputs:
            The (left, top, right, bottom) box in mm.
        """
        # Global/hierarchical labels also have an arrow body that reaches beyond
        # their glyphs.  Include it so an old relay label cannot sit under a new
        # label just because their text anchors differ by one grid cell.
        width = max(size, (len(text) + 1) * size * 0.68) + (
            STUB_LENGTH if shaped else 0.0
        )
        half_height = size * 0.85
        if justify == "right":
            left, right = x - width, x + size * 0.25
        elif justify == "left":
            left, right = x - size * 0.25, x + width
        else:
            left, right = x - width / 2, x + width / 2
        return (left, y - half_height, right, y + half_height)

    @classmethod
    def visible_block_box(
        cls, block: str, token: str, default_justify: str = "center"
    ) -> tuple[float, float, float, float] | None:
        """Envelope of one top-level text, label or heading object, or None.

        None is returned when the block has no text or no position.
        """
        text = cls.visible_text(block, token)
        at = AT_RE.search(block)
        if not text or not at:
            return None
        angle = int(round(float(at.group(3) or "0"))) % 360
        justify = default_justify
        if "(justify right)" in block or angle == 180:
            justify = "right"
        elif "(justify left)" in block or angle == 0:
            justify = "left"
        return cls.text_box(
            text,
            float(at.group(1)),
            float(at.group(2)),
            cls.visible_text_size(block),
            justify,
            token in {"label", "global_label", "hierarchical_label"},
        )

    @classmethod
    def visible_symbol_property_boxes(
        cls,
        block: str,
    ) -> list[tuple[float, float, float, float]]:
        """Reserve the visible reference/value artwork of an untouched symbol."""
        boxes: list[tuple[float, float, float, float]] = []
        for start, end in SExpressionText.direct_blocks(block, "property"):
            field = block[start:end]
            match = re.match(
                r'\(property\s+"(?:Reference|Value)"\s+"((?:[^"\\]|\\.)*)"', field
            )
            at = AT_RE.search(field)
            if not match or not at or re.search(r"\(hide\s+yes\)", field):
                continue
            text = match.group(1).replace(r'\\"', '"').replace(r"\\\\", "\\")
            angle = int(round(float(at.group(3) or "0"))) % 360
            justify = (
                "right"
                if "(justify right)" in field or angle == 180
                else "left"
                if "(justify left)" in field or angle == 0
                else "center"
            )
            boxes.append(
                cls.text_box(
                    text,
                    float(at.group(1)),
                    float(at.group(2)),
                    cls.visible_text_size(field),
                    justify,
                )
            )
        return boxes

    @staticmethod
    def instance_pin_envelope(
        item: SymbolInstance, definitions: dict[str, tuple[int, int, str]]
    ) -> tuple[float, float, float, float] | None:
        """A conservative keep-out for an untouched passive or discrete body.

        Outputs:
            The pin bounding box grown by 2.54 mm, or None when the definition
            or unit is not embedded or has no pins.
        """
        definition = definitions.get(item.lib_id)
        if not definition:
            return None
        units = library_units(definition[2])
        if item.unit not in units:
            return None
        points = [item.pin_point(pin) for pin in units[item.unit][1]]
        if not points:
            return None
        margin = 2.54
        return (
            min(point[0] for point in points) - margin,
            min(point[1] for point in points) - margin,
            max(point[0] for point in points) + margin,
            max(point[1] for point in points) + margin,
        )


class PlanGeometry:
    """Plane geometry on points and boxes, in mm, used to test placements."""

    @staticmethod
    def point_in_box(
        point: tuple[float, float], left: float, top: float, right: float, bottom: float
    ) -> bool:
        """Whether a point lies inside a box, edges included within 1e-6 mm."""
        return (
            left - 1e-6 <= point[0] <= right + 1e-6
            and top - 1e-6 <= point[1] <= bottom + 1e-6
        )

    @staticmethod
    def boxes_overlap(
        first: tuple[float, float, float, float],
        second: tuple[float, float, float, float],
    ) -> bool:
        """Whether two (left, top, right, bottom) boxes overlap, within 1e-6 mm."""
        return not (
            first[2] < second[0] - 1e-6
            or second[2] < first[0] - 1e-6
            or first[3] < second[1] - 1e-6
            or second[3] < first[1] - 1e-6
        )

    @staticmethod
    def inside_page(box: tuple[float, float, float, float]) -> bool:
        """Whether a box lies within the page placement bounds."""
        return (
            PAGE_LEFT <= box[0]
            and PAGE_TOP <= box[1]
            and box[2] <= PAGE_RIGHT
            and box[3] <= PAGE_BOTTOM
        )

    @staticmethod
    def point_index(
        points: set[tuple[float, float]],
    ) -> dict[tuple[int, int], list[tuple[float, float]]]:
        """Bucket points into square cells of ``SPATIAL_CELL`` mm for box queries."""
        indexed: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
        for point in points:
            indexed[
                (int(point[0] // SPATIAL_CELL), int(point[1] // SPATIAL_CELL))
            ].append(point)
        return indexed

    @classmethod
    def indexed_point_in_box(
        cls,
        indexed: dict[tuple[int, int], list[tuple[float, float]]],
        left: float,
        top: float,
        right: float,
        bottom: float,
    ) -> bool:
        """Whether any point of a ``point_index`` lies inside the box."""
        for cell_x in range(int(left // SPATIAL_CELL), int(right // SPATIAL_CELL) + 1):
            for cell_y in range(
                int(top // SPATIAL_CELL), int(bottom // SPATIAL_CELL) + 1
            ):
                if any(
                    cls.point_in_box(point, left, top, right, bottom)
                    for point in indexed.get((cell_x, cell_y), [])
                ):
                    return True
        return False

    @staticmethod
    def orient(
        a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
    ) -> float:
        """Twice the signed area of triangle a, b, c; zero when they are collinear."""
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    @classmethod
    def on_segment(
        cls, a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
    ) -> bool:
        """Whether point ``c`` lies on the segment from ``a`` to ``b``."""
        return (
            abs(cls.orient(a, b, c)) < 1e-6
            and min(a[0], b[0]) - 1e-6 <= c[0] <= max(a[0], b[0]) + 1e-6
            and min(a[1], b[1]) - 1e-6 <= c[1] <= max(a[1], b[1]) + 1e-6
        )

    @classmethod
    def segments_intersect(
        cls,
        a: tuple[float, float],
        b: tuple[float, float],
        c: tuple[float, float],
        d: tuple[float, float],
    ) -> bool:
        """Whether segment a-b crosses or touches segment c-d."""
        values = (
            cls.orient(a, b, c),
            cls.orient(a, b, d),
            cls.orient(c, d, a),
            cls.orient(c, d, b),
        )
        if values[0] * values[1] < -1e-9 and values[2] * values[3] < -1e-9:
            return True
        return any(
            (
                cls.on_segment(a, b, c),
                cls.on_segment(a, b, d),
                cls.on_segment(c, d, a),
                cls.on_segment(c, d, b),
            )
        )

    @classmethod
    def segment_hits_box(
        cls,
        a: tuple[float, float],
        b: tuple[float, float],
        left: float,
        top: float,
        right: float,
        bottom: float,
    ) -> bool:
        """Whether segment a-b has an end inside the box or crosses its outline."""
        if cls.point_in_box(a, left, top, right, bottom) or cls.point_in_box(
            b, left, top, right, bottom
        ):
            return True
        corners = ((left, top), (right, top), (right, bottom), (left, bottom))
        return any(
            cls.segments_intersect(a, b, corners[index], corners[(index + 1) % 4])
            for index in range(4)
        )

    @staticmethod
    def spiral_offsets(max_radius: int = 250) -> list[tuple[float, float]]:
        """Candidate (dx, dy) shifts in mm in rings of 2.54 mm steps around zero.

        Inputs:
            max_radius: Number of rings, 1 to ``max_radius``.
        Outputs:
            (0, 0) followed by each ring in turn, so earlier offsets are closer.
        """
        offsets = [(0.0, 0.0)]
        # A large normalized controller may need to move farther than one
        # crowded subsystem cell; the deterministic grid still keeps the first
        # clear location closest to its original coordinates.
        for radius in range(1, max_radius + 1):
            for delta in range(-radius, radius + 1):
                offsets.append((radius * 2.54, delta * 2.54))
                offsets.append((-radius * 2.54, delta * 2.54))
            for delta in range(-radius + 1, radius):
                offsets.append((delta * 2.54, radius * 2.54))
                offsets.append((delta * 2.54, -radius * 2.54))
        return offsets


class PlacementSearch:
    """Chooses a clear grid position for each normalized block, one after another.

    Inputs:
        layout: Pin layout of the generated symbols.
        static_points: Pin and label points of everything that stays put.
        static_segments: Retained wire segments.
        static_text_boxes: Visible text and artwork of everything that stays put.
        max_travel_mm: Largest displacement of a block from its first position.
        preserve_location: Skip the body collision checks and try only a small
            neighbourhood, for in-place trials.
    The search records each accepted block as an obstacle for the next.
    """

    def __init__(
        self,
        layout: PinLayout,
        static_points: set[tuple[float, float]],
        static_segments: list[tuple[tuple[float, float], tuple[float, float]]],
        static_text_boxes: list[tuple[float, float, float, float]],
        max_travel_mm: float,
        preserve_location: bool,
    ) -> None:
        """Store the fixed obstacles and start with no placed blocks."""
        self.layout = layout
        self.static_points = static_points
        self.static_segments = static_segments
        self.static_text_boxes = static_text_boxes
        self.max_travel_mm = max_travel_mm
        self.preserve_location = preserve_location
        # A controlled in-place pass may nudge a block by at most 25.4 mm only to
        # keep a newly normalized body inside the sheet.  It never uses the global
        # empty space that would visually detach a part from its subsystem.
        self.placement_offsets = (
            PlanGeometry.spiral_offsets(10)
            if preserve_location
            else PlanGeometry.spiral_offsets()
        )
        self.occupied_boxes: list[tuple[float, float, float, float]] = []
        self.occupied_points: set[tuple[float, float]] = set()
        self.occupied_segments: list[
            tuple[tuple[float, float], tuple[float, float]]
        ] = []
        self.occupied_visual_boxes: list[tuple[float, float, float, float]] = []
        self.occupied_label_boxes: list[tuple[float, float, float, float]] = []
        self.static_point_index = PlanGeometry.point_index(static_points)

    def place(
        self,
        item: SymbolInstance,
        current: tuple[float, float],
        geometry: SymbolGeometry,
        pins: list[PinTemplate],
        pin_moves: list[PinMove],
    ) -> tuple[float, float]:
        """Choose the nearest clear placement for one block and reserve it.

        Inputs:
            item: The symbol being placed.
            current: Preferred anchor in mm before snapping to the grid.
            geometry: Body envelope of its rebuilt definition.
            pins: Its pins in natural order.
            pin_moves: The pin moves of this symbol, which carry the net names.
        Outputs:
            The chosen anchor in mm. Raises ValueError, with the count of
            rejections by cause, when no offset within the travel limit is clear.
        """
        # The original artwork is already connected on the native 50-mil
        # grid, but a source symbol's library anchor need not be.  Our
        # normalized body uses a new anchor, so snap that anchor before
        # trying positions; every generated connection remains on-grid.
        current_x, current_y = current
        current_x = round(current_x / NATIVE_GRID) * NATIVE_GRID
        current_y = round(current_y / NATIVE_GRID) * NATIVE_GRID
        rejection_counts: Counter[str] = Counter()
        for dx, dy in self.placement_offsets:
            if math.hypot(dx, dy) > self.max_travel_mm:
                rejection_counts["subsystem affinity"] += 1
                continue
            x, y = point_key(current_x + dx, current_y + dy)
            left, top, right, bottom = (
                x + geometry.left - 2.54,
                y - geometry.top - 2.54,
                x + geometry.right + 2.54,
                y - geometry.bottom + 2.54,
            )
            candidate_visual = [
                (left, top, right, bottom),
                VisibleText.text_box(
                    item.reference,
                    x + geometry.right,
                    y - geometry.top - FIELD_GAP,
                    TEXT_SIZE,
                    "right",
                ),
            ]
            value = VisibleText.property_text(item.block, "Value")
            if value:
                candidate_visual.append(
                    VisibleText.text_box(
                        value,
                        x + geometry.right,
                        y - geometry.bottom + FIELD_GAP,
                        TEXT_SIZE,
                        "right",
                    )
                )
            if not all(PlanGeometry.inside_page(visual) for visual in candidate_visual):
                rejection_counts["page bounds"] += 1
                continue
            if any(
                PlanGeometry.boxes_overlap(
                    visual, (TITLE_BLOCK_LEFT, TITLE_BLOCK_TOP, PAGE_RIGHT, PAGE_BOTTOM)
                )
                for visual in candidate_visual
            ):
                rejection_counts["title block"] += 1
                continue
            if not self.preserve_location:
                if PlanGeometry.indexed_point_in_box(
                    self.static_point_index, left, top, right, bottom
                ) or any(
                    PlanGeometry.point_in_box(point, left, top, right, bottom)
                    for point in self.occupied_points
                ):
                    rejection_counts["static point"] += 1
                    continue
                if any(
                    PlanGeometry.segment_hits_box(a, b, left, top, right, bottom)
                    for a, b in self.static_segments + self.occupied_segments
                ):
                    rejection_counts["static segment"] += 1
                    continue
                if any(
                    not (
                        right < other_left
                        or other_right < left
                        or bottom < other_top
                        or other_bottom < top
                    )
                    for other_left, other_top, other_right, other_bottom in self.occupied_boxes
                ):
                    rejection_counts["normalized block"] += 1
                    continue
                if any(
                    PlanGeometry.boxes_overlap(visual, obstacle)
                    for visual in candidate_visual
                    for obstacle in self.static_text_boxes + self.occupied_visual_boxes
                ):
                    rejection_counts["visible text"] += 1
                    continue
            stubs = []
            candidate_label_boxes: list[tuple[float, float, float, float]] = []
            valid = True
            for index, pin in enumerate(pins):
                local_x, local_y = self.layout.normalized_pin_position(
                    item.lib_id, len(pins), index
                )
                endpoint = point_key(x + local_x, y - local_y)
                label_end = point_key(endpoint[0] - STUB_LENGTH, endpoint[1])
                # A local placement may relax *body* collision scoring while
                # finding room inside its original subsystem, but its actual
                # electrical endpoints and one-grid stubs must never land on
                # retained circuitry.  Skipping this check caused an unsafe
                # visual U3 preview to short unrelated nets.
                if (
                    endpoint in self.static_points
                    or label_end in self.static_points
                    or endpoint in self.occupied_points
                    or label_end in self.occupied_points
                ):
                    rejection_counts["stub point"] += 1
                    valid = False
                    break
                if any(
                    PlanGeometry.segments_intersect(endpoint, label_end, a, b)
                    for a, b in self.static_segments + self.occupied_segments
                ):
                    rejection_counts["stub segment"] += 1
                    valid = False
                    break
                net = next(move.net for move in pin_moves if move.number == pin.number)
                if not BlockPlanner.unconnected_net(net):
                    visual = VisibleText.text_box(
                        str(net),
                        label_end[0],
                        label_end[1],
                        TEXT_SIZE,
                        "right",
                        shaped=True,
                    )
                    if not self.preserve_location and any(
                        PlanGeometry.boxes_overlap(visual, obstacle)
                        for obstacle in self.static_text_boxes
                        + self.occupied_visual_boxes
                        + self.occupied_label_boxes
                        + candidate_visual
                    ):
                        rejection_counts["label text"] += 1
                        valid = False
                        break
                    candidate_label_boxes.append(visual)
                stubs.append((endpoint, label_end))
            if not valid:
                continue
            self.occupied_boxes.append((left, top, right, bottom))
            self.occupied_visual_boxes.extend(candidate_visual)
            self.occupied_label_boxes.extend(candidate_label_boxes)
            for endpoint, label_end in stubs:
                self.occupied_points.update((endpoint, label_end))
                self.occupied_segments.append((endpoint, label_end))
            return (x, y)
        raise ValueError(
            f"no clear placement found for {item.reference} unit "
            f"{item.unit}: {dict(rejection_counts)}"
        )


class BlockPlanner:
    """Plans the one-sided conversion of a schematic's J/U/block-D symbols.

    Inputs:
        pin_pitch_mm: Row pitch of the generated pin columns in mm.
        only_references: Restrict the conversion to these references, or None
            for the standard target set.
    """

    def __init__(
        self, pin_pitch_mm: float = PIN_PITCH, only_references: set[str] | None = None
    ) -> None:
        """Create the target filter and symbol writer for one run."""
        self.targets = BlockTargets(only_references)
        self.writer = BlockSymbolWriter(pin_pitch_mm)
        self.layout = self.writer.layout

    @staticmethod
    def unconnected_net(name: str | None) -> bool:
        """Whether a net name marks a pin that connects to nothing."""
        return not name or name.startswith("unconnected-(")

    @staticmethod
    def pin_names_add_information(
        items: list[SymbolInstance],
        templates: dict[int, tuple[str, list[PinTemplate]]],
        nets: dict[tuple[str, str], str],
    ) -> bool:
        """Always render terminal names for the standardized circuit drawing.

        The visible net tag explains the circuit role; the pin name explains the
        physical terminal.  Keeping both prevents a reader from having to infer a
        connector's cavity or protocol contact from the net name alone.  Geometry
        is sized from the longest such terminal name by
        :meth:`PinLayout.body_right_for`.
        """
        return True

    @staticmethod
    def existing_labels(
        root: str,
    ) -> dict[tuple[float, float], list[tuple[int, int, str]]]:
        """Group the root's labels of every kind by anchor point."""
        result: dict[tuple[float, float], list[tuple[int, int, str]]] = defaultdict(
            list
        )
        for token in ("label", "global_label", "hierarchical_label"):
            for start, end, block in top_level(root, token):
                points = top_level_points(block, token)
                if points:
                    result[point_key(*points[0])].append((start, end, block))
        return result

    def build_plan(
        self,
        source: str,
        nets: dict[tuple[str, str], str],
        max_travel_mm: float = DEFAULT_MAX_TRAVEL_MM,
        preserve_location: bool = False,
    ) -> tuple[str, dict[str, int]]:
        """Convert every target symbol to the one-sided style.

        Inputs:
            source: Complete schematic text.
            nets: Net name of each (reference, pin number) from the netlist.
            max_travel_mm: Largest displacement of a block in mm.
            preserve_location: Keep each block at its source pin envelope, moving
                it by at most 25.4 mm to stay inside the sheet.
        Outputs:
            The converted schematic text and a count summary. Raises ValueError
            when the source is not one KiCad schematic, a target lacks its
            embedded definition or pins, nets would merge, or no clear placement
            exists.
        """
        root_start = source.find("(kicad_sch")
        if root_start < 0:
            raise ValueError("not a KiCad schematic")
        root_end = balanced_end(source, root_start)
        if root_start != 0 or root_end != len(source.rstrip()):
            raise ValueError("unexpected content outside KiCad schematic root")
        root = source[root_start:root_end]
        _lib_start, _lib_end, definitions = source_lib_definitions(root)
        all_instances = instances(root)
        targets = [
            item for item in all_instances if self.targets(item.reference, item.lib_id)
        ]
        if not targets:
            raise ValueError("no J/U/block-D symbols found")
        by_lib: dict[str, list[SymbolInstance]] = defaultdict(list)
        for item in targets:
            by_lib[item.lib_id].append(item)
        all_by_lib: dict[str, list[SymbolInstance]] = defaultdict(list)
        for item in all_instances:
            all_by_lib[item.lib_id].append(item)
        for lib_id, items in by_lib.items():
            if lib_id not in definitions:
                raise ValueError(f"missing embedded definition for {lib_id}")
            if len(all_by_lib[lib_id]) != len(items):
                others = ", ".join(
                    item.reference
                    for item in all_by_lib[lib_id]
                    if not self.targets(item.reference, item.lib_id)
                )
                raise ValueError(
                    f"{lib_id} is also used by non-target symbol(s): {others}"
                )

        definition_updates: dict[
            str, tuple[str, dict[int, list[PinTemplate]], SymbolGeometry]
        ] = {}
        hidden_pin_name_definitions = 0
        for lib_id, items in by_lib.items():
            original_units = library_units(definitions[lib_id][2])
            pin_names_visible = self.pin_names_add_information(
                items, original_units, nets
            )
            if not pin_names_visible:
                hidden_pin_name_definitions += 1
            definition_updates[lib_id] = self.writer.generic_definition(
                lib_id, definitions[lib_id][2], pin_names_visible
            )

        non_target_pin_points: set[tuple[float, float]] = set()
        for item in all_instances:
            definition = definitions.get(item.lib_id)
            if not definition:
                continue
            units = library_units(definition[2])
            if item.unit not in units:
                continue
            for pin in units[item.unit][1]:
                point = item.pin_point(pin)
                if not self.targets(item.reference, item.lib_id):
                    non_target_pin_points.add(point)

        moves: list[PinMove] = []
        old_point_moves: dict[tuple[float, float], list[PinMove]] = defaultdict(list)
        placements: dict[SymbolInstance, tuple[float, float]] = {}
        for item in targets:
            _definition, templates, _geometry = definition_updates[item.lib_id]
            original_units = library_units(definitions[item.lib_id][2])
            if item.unit not in templates or item.unit not in original_units:
                raise ValueError(
                    f"{item.reference} unit {item.unit} cannot be converted"
                )
            old_pins = {pin.number: pin for pin in original_units[item.unit][1]}
            new_pins = sorted(
                templates[item.unit], key=lambda pin: PinNumbers.natural_key(pin.number)
            )
            local_positions = {
                pin.number: self.layout.normalized_pin_position(
                    item.lib_id, len(new_pins), index
                )
                for index, pin in enumerate(new_pins)
            }
            old_locations = {
                number: item.pin_point(pin) for number, pin in old_pins.items()
            }
            # Normalize the new block against the former pin envelope rather than
            # its library anchor.  Connector libraries often anchor at pin 1;
            # centering against the real envelope prevents a global vertical jump.
            new_x = min(point[0] for point in old_locations.values()) - min(
                point[0] for point in local_positions.values()
            )
            new_y = min(point[1] for point in old_locations.values()) + max(
                point[1] for point in local_positions.values()
            )
            placements[item] = (new_x, new_y)
            for new_pin in new_pins:
                old_pin = old_pins.get(new_pin.number)
                if old_pin is None:
                    raise ValueError(
                        f"missing original pin {item.reference}/{new_pin.number}"
                    )
                old = old_locations[new_pin.number]
                local_x, local_y = local_positions[new_pin.number]
                new = point_key(new_x + local_x, new_y - local_y)
                move = PinMove(
                    item,
                    new_pin.number,
                    old,
                    new,
                    nets.get((item.reference, new_pin.number)),
                )
                moves.append(move)
                old_point_moves[old].append(move)

        for point, point_moves in old_point_moves.items():
            used_nets = {
                move.net for move in point_moves if not self.unconnected_net(move.net)
            }
            if len(used_nets) > 1:
                labels = ", ".join(sorted(used_nets))
                raise ValueError(
                    f"multiple electrical nets share old pin coordinate {point}: {labels}"
                )

        target_old_points = set(old_point_moves)
        label_map = self.existing_labels(root)
        all_wires = [
            (
                start,
                end,
                block,
                [point_key(*point) for point in top_level_points(block, "wire")],
            )
            for start, end, block in top_level(root, "wire")
        ]
        removed_wires: list[
            tuple[int, int, str, list[tuple[float, float]], list[tuple[float, float]]]
        ] = []
        retained_wire_endpoints: Counter[tuple[float, float]] = Counter()
        for start, end, block, points in all_wires:
            if len(points) < 2:
                raise ValueError("wire without two endpoints")
            hits = [
                point for point in (points[0], points[-1]) if point in target_old_points
            ]
            interior_hits = [
                point for point in points[1:-1] if point in target_old_points
            ]
            if interior_hits:
                raise ValueError(
                    f"target pin lies within a multi-segment wire: {interior_hits}"
                )
            if hits:
                removed_wires.append((start, end, block, points, hits))
            else:
                retained_wire_endpoints.update((points[0], points[-1]))

        removal_spans: list[tuple[int, int, str]] = [
            (start, end, "wire") for start, end, _block, _points, _hits in removed_wires
        ]
        remove_label_spans: set[tuple[int, int]] = set()
        source_labels: set[tuple[tuple[float, float], str]] = set()

        # Labels attached to the former target pin would become floating text.
        for point in target_old_points:
            for start, end, _block in label_map.get(point, []):
                remove_label_spans.add((start, end))

        # A removed wire is replaced by a named connection at its remote endpoint
        # only when a non-target object or retained wire remains there.  This keeps
        # the old net alive without leaving old wires across the new block layout.
        remote_names: dict[tuple[float, float], set[str]] = defaultdict(set)
        for _start, _end, _block, points, hits in removed_wires:
            if len(hits) > 2:
                raise ValueError(f"wire has too many target endpoints: {hits}")
            if len(hits) == 2:
                # A target-to-target wire can still meet a passive pin at either
                # endpoint (for example a pull resistor on an IC's paired pins).
                # The physical wire is removed, so preserve that passive endpoint
                # explicitly with the same named net.
                for target_point in hits:
                    if (
                        target_point not in non_target_pin_points
                        and retained_wire_endpoints[target_point] == 0
                    ):
                        continue
                    names = {
                        move.net
                        for move in old_point_moves[target_point]
                        if not self.unconnected_net(move.net)
                    }
                    if len(names) != 1:
                        raise ValueError(
                            f"ambiguous target-to-target wire at {target_point}: {names}"
                        )
                    remote_names[target_point].add(next(iter(names)))
                continue
            target_point = hits[0]
            names = {
                move.net
                for move in old_point_moves[target_point]
                if not self.unconnected_net(move.net)
            }
            if len(names) > 1:
                raise ValueError(
                    f"multiple electrical nets share old pin coordinate {target_point}: {names}"
                )
            if not names:
                continue
            remote = points[-1] if target_point == points[0] else points[0]
            remote_names[remote].add(next(iter(names)))

        for point, names in remote_names.items():
            if len(names) > 1:
                raise ValueError(
                    f"removed wires would merge nets at {point}: {sorted(names)}"
                )
            net = next(iter(names))
            has_remaining_connection = (
                retained_wire_endpoints[point] > 0 or point in non_target_pin_points
            )
            existing = label_map.get(point, [])
            if not has_remaining_connection:
                for start, end, _block in existing:
                    remove_label_spans.add((start, end))
                continue
            if existing:
                for _start, _end, block in existing:
                    name = re.match(
                        r'\((?:label|global_label|hierarchical_label)\s+"([^"]+)"',
                        block,
                    )
                    if name and name.group(1) != net:
                        raise ValueError(
                            f"label at {point} is {name.group(1)}, expected {net}"
                        )
            else:
                source_labels.add((point, net))

        for start, end in remove_label_spans:
            removal_spans.append((start, end, "label"))
        for token in ("no_connect", "junction"):
            for start, end, block in top_level(root, token):
                points = top_level_points(block, token)
                if points and point_key(*points[0]) in target_old_points:
                    removal_spans.append((start, end, token))

        # Choose each normalized block position from a common grid.  The former
        # wires are gone, but all passive parts, labels, and retained wire segments
        # remain hard obstacles: a new pin or one-grid stub may not land on any of
        # them.  Large blocks are placed first, as near their original subsystem as
        # possible, so this is deterministic rather than a manual tidy-up.
        removed_wire_spans = {
            (start, end) for start, end, _block, _points, _hits in removed_wires
        }
        removed_label_spans_set = set(remove_label_spans)
        static_points = set(non_target_pin_points)
        for point, blocks in label_map.items():
            if any(
                (start, end) not in removed_label_spans_set
                for start, end, _block in blocks
            ):
                static_points.add(point)
        # Text has a real visual envelope, not only an anchor point.  Keeping those
        # envelopes hard prevents long net tags and subsystem headings from being
        # overprinted by a relocated one-sided block.
        static_text_boxes: list[tuple[float, float, float, float]] = []
        for _point, blocks in label_map.items():
            for start, end, block in blocks:
                if (start, end) not in removed_label_spans_set:
                    visual = VisibleText.visible_block_box(
                        block, block[1 : block.find(" ")]
                    )
                    if visual:
                        static_text_boxes.append(visual)
        # Subsystem headings are fixed visual landmarks, not places for a newly
        # normalized block to grow into.
        for _start, _end, block in top_level(root, "text"):
            points = top_level_points(block, "text")
            if points:
                static_points.add(point_key(*points[0]))
            visual = VisibleText.visible_block_box(block, "text")
            if visual:
                static_text_boxes.append(visual)
        # Passive/discrete artwork is part of the subsystem layout.  Keep both its
        # body envelope and its reference/value text clear so a normalized block
        # never appears to sit on top of an unchanged RC network or diode.
        target_set = set(targets)
        for item in all_instances:
            if item in target_set:
                continue
            static_text_boxes.extend(
                VisibleText.visible_symbol_property_boxes(item.block)
            )
            envelope = VisibleText.instance_pin_envelope(item, definitions)
            if envelope:
                static_text_boxes.append(envelope)
        for point, net in source_labels:
            static_text_boxes.append(
                VisibleText.text_box(
                    net, point[0], point[1], TEXT_SIZE, "right", shaped=True
                )
            )
        static_points.update(point for point, _net in source_labels)
        static_segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for start, end, _block, points in all_wires:
            if (start, end) in removed_wire_spans:
                continue
            static_segments.extend(zip(points, points[1:]))

        initial_placements = dict(placements)
        moves_by_item: dict[SymbolInstance, list[PinMove]] = defaultdict(list)
        for move in moves:
            moves_by_item[move.instance].append(move)
        search = PlacementSearch(
            self.layout,
            static_points,
            static_segments,
            static_text_boxes,
            max_travel_mm,
            preserve_location,
        )
        chosen: dict[SymbolInstance, tuple[float, float]] = {}
        for item in sorted(
            targets,
            key=lambda candidate: (
                -len(moves_by_item[candidate]),
                candidate.reference,
                candidate.unit,
            ),
        ):
            _definition, templates, geometry = definition_updates[item.lib_id]
            pins = sorted(
                templates[item.unit], key=lambda pin: PinNumbers.natural_key(pin.number)
            )
            chosen[item] = search.place(
                item, placements[item], geometry, pins, moves_by_item[item]
            )

        moves = []
        for item in targets:
            x, y = chosen[item]
            _definition, templates, _geometry = definition_updates[item.lib_id]
            pins = sorted(
                templates[item.unit], key=lambda pin: PinNumbers.natural_key(pin.number)
            )
            for index, pin in enumerate(pins):
                old = next(
                    move.old
                    for move in moves_by_item[item]
                    if move.number == pin.number
                )
                local_x, local_y = self.layout.normalized_pin_position(
                    item.lib_id, len(pins), index
                )
                moves.append(
                    PinMove(
                        item,
                        pin.number,
                        old,
                        point_key(x + local_x, y - local_y),
                        nets.get((item.reference, pin.number)),
                    )
                )
        placements = chosen

        additions: list[str] = []
        for point, net in sorted(
            source_labels, key=lambda item: (item[0][1], item[0][0], item[1])
        ):
            additions.append(NewSchematicItems.label_block(net, *point, angle=180))

        for move in sorted(
            moves,
            key=lambda item: (
                item.instance.reference,
                item.instance.unit,
                PinNumbers.natural_key(item.number),
            ),
        ):
            if self.unconnected_net(move.net):
                additions.append(NewSchematicItems.no_connect_block(move.new))
            else:
                stub_end = point_key(move.new[0] - STUB_LENGTH, move.new[1])
                additions.append(NewSchematicItems.wire_block(move.new, stub_end))
                additions.append(
                    NewSchematicItems.label_block(str(move.net), *stub_end, angle=180)
                )

        replacements: list[tuple[int, int, str, str]] = []
        for lib_id, (updated, _templates, _half) in definition_updates.items():
            start, end, _block = definitions[lib_id]
            replacements.append((start, end, updated, f"definition:{lib_id}"))
        for item in targets:
            _updated_def, _templates, geometry = definition_updates[item.lib_id]
            x, y = placements[item]
            replacements.append(
                (
                    item.start,
                    item.end,
                    self.writer.generic_instance(item, geometry, x, y),
                    f"instance:{item.reference}",
                )
            )
        for start, end, kind in removal_spans:
            replacements.append((start, end, "", f"remove:{kind}"))
        replacements.sort(key=lambda item: (item[0], item[1]))
        for previous, current in zip(replacements, replacements[1:]):
            if current[0] < previous[1]:
                raise ValueError(f"overlapping edits: {previous[3]} and {current[3]}")
        for start, end, replacement, _kind in reversed(replacements):
            root = root[:start] + replacement + root[end:]
        insert = root.rfind("(sheet_instances")
        if insert < 0:
            raise ValueError("missing sheet_instances block")
        root = root[:insert] + "\n" + "".join(additions) + root[insert:]
        result = source[:root_start] + root + source[root_end:]
        summary = {
            "target_instances": len(targets),
            "target_definitions": len(definition_updates),
            "moved_pins": len(moves),
            "removed_wires": len(removed_wires),
            "source_net_labels": len(source_labels),
            "relocated_blocks": sum(
                initial_placements[item] != placements[item] for item in targets
            ),
            "new_blocks": len(additions),
            "hidden_pin_name_definitions": hidden_pin_name_definitions,
        }
        return result, summary

    def build_compact_body_plan(
        self, source: str, nets: dict[tuple[str, str], str]
    ) -> tuple[str, dict[str, int]]:
        """Compact only library artwork and fields; never move a pin or wire.

        This lets the visual-width formula be refined after the one-sided layout is
        accepted.  The instance anchors, pin endpoints, labels, junctions and
        wires remain byte-for-byte untouched, so it is safe to validate as a pure
        presentation pass.

        Inputs:
            source: Complete schematic text.
            nets: Net name of each (reference, pin number) from the netlist.
        Outputs:
            The compacted schematic text and a count summary. Raises ValueError
            when the source is not one KiCad schematic, no target is found or a
            target lacks its embedded definition.
        """
        root_start = source.find("(kicad_sch")
        if root_start != 0:
            raise ValueError("not a KiCad schematic")
        root_end = balanced_end(source, root_start)
        if root_end != len(source.rstrip()):
            raise ValueError("unexpected content outside KiCad schematic root")
        root = source[root_start:root_end]
        _lib_start, _lib_end, definitions = source_lib_definitions(root)
        all_instances = instances(root)
        only_references = self.targets.only_references
        targets = [
            item
            for item in all_instances
            if self.targets(item.reference, item.lib_id)
            or (
                item.reference == "U3"
                and (only_references is None or "U3" in only_references)
            )
        ]
        if not targets:
            raise ValueError("no compactable J/U/block-D symbols found")
        by_lib: dict[str, list[SymbolInstance]] = defaultdict(list)
        all_by_lib: dict[str, list[SymbolInstance]] = defaultdict(list)
        for item in all_instances:
            all_by_lib[item.lib_id].append(item)
        for item in targets:
            by_lib[item.lib_id].append(item)
        for lib_id, items in by_lib.items():
            if lib_id not in definitions:
                raise ValueError(f"missing embedded definition for {lib_id}")
            if len(all_by_lib[lib_id]) != len(items):
                others = ", ".join(
                    item.reference for item in all_by_lib[lib_id] if item not in items
                )
                raise ValueError(
                    f"{lib_id} is also used by non-target symbol(s): {others}"
                )

        geometry_by_lib: dict[str, SymbolGeometry] = {}
        replacements: list[tuple[int, int, str]] = []
        for lib_id, items in by_lib.items():
            definition_start, definition_end, definition = definitions[lib_id]
            units = library_units(definition)
            pin_names_visible = self.pin_names_add_information(items, units, nets)
            compact_definition, _templates, geometry = self.writer.generic_definition(
                lib_id, definition, pin_names_visible
            )
            geometry_by_lib[lib_id] = geometry
            if compact_definition != definition:
                replacements.append(
                    (definition_start, definition_end, compact_definition)
                )

        field_updates = 0
        for item in targets:
            compact_instance = self.writer.compact_body_fields(
                item, geometry_by_lib[item.lib_id]
            )
            if compact_instance != item.block:
                replacements.append((item.start, item.end, compact_instance))
                field_updates += 1
        result = source
        for start, end, replacement in sorted(replacements, reverse=True):
            result = result[:start] + replacement + result[end:]
        return result, {
            "compact_definitions": sum(
                1
                for lib_id, (_start, _end, definition) in definitions.items()
                if lib_id in geometry_by_lib
                and self.writer.generic_definition(
                    lib_id,
                    definition,
                    self.pin_names_add_information(
                        by_lib[lib_id], library_units(definition), nets
                    ),
                )[0]
                != definition
            ),
            "field_pairs_repositioned": field_updates,
            "pins_or_wires_moved": 0,
        }


class StandardizeCommand:
    """Command-line entry: convert target symbols to the one-sided presentation."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Check or apply the one-sided conversion of a schematic.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 after printing the count summary and the input digest,
            and with ``--apply`` the atomically written output's digest and path.
            Raises ValueError for a pin pitch that is not a positive multiple of
            the 1.27 mm grid, a non-positive travel limit, or any planning error.
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("schematic", type=Path)
        parser.add_argument(
            "--netlist",
            required=True,
            type=Path,
            help="native XML netlist exported before conversion",
        )
        parser.add_argument(
            "--output",
            type=Path,
            help="destination for an atomic --apply result; defaults to the input schematic",
        )
        parser.add_argument(
            "--max-travel-mm",
            type=float,
            default=DEFAULT_MAX_TRAVEL_MM,
            help=(
                "maximum automatic block displacement; keeps a normalizer inside its source subsystem"
            ),
        )
        parser.add_argument(
            "--only-reference",
            action="append",
            default=[],
            help="normalize only this reference; repeatable, for a declared visual exception",
        )
        parser.add_argument(
            "--pin-pitch-mm",
            type=float,
            default=PIN_PITCH,
            help="native-grid row pitch for an --only-reference run",
        )
        parser.add_argument(
            "--preserve-location",
            action="store_true",
            help=(
                "keep each normalized block at its source pin envelope; "
                "for controlled section reflow trials only"
            ),
        )
        parser.add_argument(
            "--compact-bodies",
            action="store_true",
            help=(
                "refit accepted one-sided body artwork and Reference/Value "
                "fields only; never move pins or wires"
            ),
        )
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--check", action="store_true")
        mode.add_argument("--apply", action="store_true")
        args = parser.parse_args(argv)
        if args.output and not args.apply:
            parser.error("--output requires --apply")
        only_references = set(args.only_reference) if args.only_reference else None
        if (
            args.pin_pitch_mm <= 0
            or abs(
                args.pin_pitch_mm / NATIVE_GRID - round(args.pin_pitch_mm / NATIVE_GRID)
            )
            > 1e-9
        ):
            raise ValueError(
                "--pin-pitch-mm must be a positive multiple of the native 1.27 mm grid"
            )
        planner = BlockPlanner(args.pin_pitch_mm, only_references)
        source = args.schematic.read_text(encoding="utf-8")
        before = FileDigest.sha256(args.schematic)
        if args.max_travel_mm <= 0:
            raise ValueError("--max-travel-mm must be positive")
        nets = NetlistPins.read(args.netlist)
        if args.compact_bodies:
            result, summary = planner.build_compact_body_plan(source, nets)
        else:
            result, summary = planner.build_plan(
                source, nets, args.max_travel_mm, args.preserve_location
            )
        for name, count in summary.items():
            print(f"{name.replace('_', ' ')}: {count}")
        print(f"input schematic SHA256: {before}")
        if args.apply:
            destination = args.output or args.schematic
            AtomicTextWriter(".oneside.tmp").write(destination, result)
            print(f"output schematic SHA256: {FileDigest.sha256(destination)}")
            print(f"output schematic: {destination}")
            print("applied atomically")
        else:
            print("check only; no schematic changed")
        return 0


main = StandardizeCommand()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # fail closed: no partial write has occurred
        print(f"one-side conversion aborted: {error}", file=sys.stderr)
        raise SystemExit(2) from None
