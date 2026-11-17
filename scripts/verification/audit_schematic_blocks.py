#!/usr/bin/env python3
"""Read-only presentation audit for J/U/block-D symbols in a KiCad schematic.

The report checks pin directions and ordering and flags multi-column symbols
for review. It does not verify body geometry or alter the schematic, libraries,
electrical connectivity, page setup, or labels.
"""

from __future__ import annotations

import argparse
import math
import re
from collections import Counter
from pathlib import Path


def balanced_end(source: str, start: int) -> int:
    """Return the exclusive end of an S-expression, respecting quoted text."""
    depth = 0
    quote = False
    escape = False
    for pos in range(start, len(source)):
        char = source[pos]
        if quote:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                quote = False
            continue
        if char == '"':
            quote = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return pos + 1
    raise ValueError(f"unclosed expression at {start}")


def child_blocks(source: str, token: str) -> list[tuple[int, int]]:
    """Find top-level occurrences of ``(token`` in one complete expression."""
    spans: list[tuple[int, int]] = []
    depth = 0
    quote = False
    escape = False
    pos = 0
    marker = f"({token}"
    while pos < len(source):
        char = source[pos]
        if quote:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                quote = False
            pos += 1
            continue
        if char == '"':
            quote = True
            pos += 1
            continue
        if source.startswith(marker, pos) and depth == 1:
            end = balanced_end(source, pos)
            spans.append((pos, end))
            pos = end
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        pos += 1
    return spans


def lib_symbols(source: str) -> dict[str, str]:
    start = source.find("(lib_symbols")
    if start < 0:
        raise ValueError("missing lib_symbols")
    root = source[start : balanced_end(source, start)]
    result: dict[str, str] = {}
    for child_start, child_end in child_blocks(root, "symbol"):
        block = root[child_start:child_end]
        match = re.match(r'\(symbol\s+"([^"]+)"', block)
        if match:
            result[match.group(1)] = block
    return result


def instance_symbols(source: str, library_end: int) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    cursor = library_end
    marker = re.compile(r"(?m)^[\t ]*\(symbol\s*$")
    while match := marker.search(source, cursor):
        start = source.rfind("(", 0, match.end())
        end = balanced_end(source, start)
        cursor = end
        block = source[start:end]
        lib = re.search(r'\(lib_id\s+"([^"]+)"\)', block)
        ref = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', block)
        at = re.search(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\)", block)
        unit = re.search(r"\(unit\s+(\d+)\)", block)
        if lib and ref and at and unit:
            result.append(
                {
                    "reference": ref.group(1),
                    "lib_id": lib.group(1),
                    "x": float(at.group(1)),
                    "y": float(at.group(2)),
                    "rotation": int(float(at.group(3))) % 360,
                    "unit": int(unit.group(1)),
                    "mirror": re.search(r"\(mirror\s+([xy])\)", block).group(1)
                    if re.search(r"\(mirror\s+([xy])\)", block)
                    else None,
                }
            )
    return result


def unit_pins(lib_block: str, unit: int) -> list[tuple[str, float, float, int]]:
    """Read unit-specific and common (unit zero) pins from an embedded symbol."""
    candidates: list[str] = []
    for start, end in child_blocks(lib_block, "symbol"):
        child = lib_block[start:end]
        name = re.match(r'\(symbol\s+"([^"]+)"', child)
        if name and re.search(rf"_(?:0|{unit})_[0-9]+$", name.group(1)):
            candidates.append(child)
    pins: list[tuple[str, float, float, int]] = []
    for child in candidates:
        for start, end in child_blocks(child, "pin"):
            block = child[start:end]
            first = re.match(
                r"\(pin\s+\S+\s+\S+\s+\(at\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\)",
                block,
            )
            number = re.search(r'\(number\s+"([^"]+)"', block)
            if first and number:
                pins.append(
                    (
                        number.group(1),
                        float(first.group(1)),
                        float(first.group(2)),
                        int(float(first.group(3))) % 360,
                    )
                )
    return pins


def screen_point(
    x: float, y: float, rotation: int, mirror: str | None, lx: float, ly: float
) -> tuple[float, float]:
    """Convert KiCad symbol-local Y-up coordinates to sheet Y-down coordinates."""
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
    raise ValueError(f"unsupported rotation {rotation}")


def pin_points_right(angle: int, rotation: int, mirror: str | None) -> bool:
    """A left-side pin points from its endpoint rightwards into the block."""
    x, y = screen_point(
        0,
        0,
        rotation,
        mirror,
        math.cos(math.radians(angle)),
        math.sin(math.radians(angle)),
    )
    return x > 0.999999 and abs(y) < 1e-6


def numeric_key(value: str) -> tuple[int, str]:
    match = re.fullmatch(r"(\d+)(.*)", value)
    return (int(match.group(1)), match.group(2)) if match else (10**9, value)


def pin_order_key(value: str) -> tuple[int, str, int, str] | None:
    """Return the natural physical order for a visible pin designator.

    Most blocks have ``1, 2, ...`` pins, while USB-C legitimately has the
    physical ``A1, A4, ... B12, SH`` sequence.  Treating the latter as
    unorderable merely because its numbers carry a row prefix makes the audit
    report a false presentation failure.  Keep non-pin names unorderable so
    the checker never invents an ordering for arbitrary terminal text.
    """
    numeric = re.fullmatch(r"(\d+)", value)
    if numeric:
        return (0, "", int(numeric.group(1)), "")
    alpha_numeric = re.fullmatch(r"([A-Za-z]+)(\d+)", value)
    if alpha_numeric:
        return (1, alpha_numeric.group(1).upper(), int(alpha_numeric.group(2)), "")
    if value.upper() in {"SH", "SHIELD", "SHELL"}:
        return (2, "", 0, value.upper())
    return None


def is_target(reference: str, lib_id: str) -> bool:
    return reference.startswith(("J", "U")) or (
        reference.startswith("D") and not lib_id.startswith("Device:")
    )


def audit(source: str) -> list[dict[str, object]]:
    lib_start = source.find("(lib_symbols")
    lib_end = balanced_end(source, lib_start)
    definitions = lib_symbols(source)
    findings: list[dict[str, object]] = []
    for instance in instance_symbols(source, lib_end):
        reference = str(instance["reference"])
        lib_id = str(instance["lib_id"])
        if not is_target(reference, lib_id):
            continue
        pins = unit_pins(definitions[lib_id], int(instance["unit"]))
        points = [
            (
                number,
                *screen_point(
                    float(instance["x"]),
                    float(instance["y"]),
                    int(instance["rotation"]),
                    instance["mirror"],
                    px,
                    py,
                ),
            )
            for number, px, py, _angle in pins
        ]
        x_columns = {round(x, 4) for _number, x, _y in points}
        ordered_keys = [(pin_order_key(point[0]), point) for point in points]
        orderable = all(key is not None for key, _point in ordered_keys)
        ordered = [
            point
            for _key, point in sorted(
                ordered_keys,
                key=lambda item: item[0] if item[0] is not None else (99, "", 0, ""),
            )
        ]
        ascending = (
            orderable
            and bool(ordered)
            and all(
                before[2] <= after[2] + 1e-6
                for before, after in zip(ordered, ordered[1:], strict=False)
            )
        )
        one_side = len(x_columns) == 1
        left_facing = bool(pins) and all(
            pin_points_right(angle, int(instance["rotation"]), instance["mirror"])
            for _number, _px, _py, angle in pins
        )
        if not pins:
            status = "no parsed pins; review needed"
        elif one_side and left_facing and ascending:
            status = "one-sided ascending"
        elif one_side and not left_facing:
            status = "one column; pin directions require review"
        elif one_side:
            status = "one-sided; nonnumeric/mixed order"
        elif len(x_columns) > 1 and left_facing:
            status = "multiple left-facing columns; order requires review"
        else:
            status = "multi-sided functional layout"
        findings.append(
            {
                "reference": reference,
                "lib_id": lib_id,
                "pins": len(points),
                "x_columns": len(x_columns),
                "numeric_ascending": ascending,
                "pins_left_facing": left_facing,
                "status": status,
            }
        )
    return sorted(
        findings,
        key=lambda item: (
            item["reference"].rstrip("0123456789"),
            numeric_key(item["reference"].lstrip("JUDP")),
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schematic", type=Path)
    parser.add_argument(
        "--all", action="store_true", help="include already-compliant blocks"
    )
    args = parser.parse_args()
    source = args.schematic.read_text(encoding="utf-8")
    findings = audit(source)
    status_counts = Counter(item["status"] for item in findings)
    print(f"audited block symbols: {len(findings)}")
    for status, count in sorted(status_counts.items()):
        print(f"{status}: {count}")
    print()
    for item in findings:
        if args.all or item["status"] != "one-sided ascending":
            print(
                f"{item['reference']:>4} | {item['pins']:>2} pins | "
                f"{item['x_columns']} columns | {item['status']} | {item['lib_id']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
