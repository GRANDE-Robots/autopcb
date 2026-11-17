#!/usr/bin/env python3
"""Normalize free-text typography in a KiCad schematic without touching nets.

This deliberately changes only standalone ``(text ...)`` annotations.  It
leaves symbols, pins, wires, fields, the drawing sheet, and page settings
untouched.  Run ``--check`` before ``--apply``; the latter writes atomically.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

MASTER_TITLE = "GRANDE Jessie | Integrated MCU Circuit v4.1"
WORDING = {
    "23. USB HOST-POWER GATE": "23. USB HOST-PRESENCE DATA GATE",
    "USB host-power gate (U29)": "USB host-presence monitor (U29)",
    "USB host-power gate (U28)": "USB host-presence data gate (U28)",
}
SECTION_RE = re.compile(r"^\d+\.\s+")
TEXT_START_RE = re.compile(r"(?m)^[\t ]*\(text\s+")
TEXT_VALUE_RE = re.compile(r'^\s*\(text\s+"((?:\\.|[^"\\])*)"')
NET_LABEL_START_RE = re.compile(
    r"(?m)^[\t ]*\((?:global_label|hierarchical_label|label)\s+"
)
SYMBOL_START_RE = re.compile(r"(?m)^[\t ]*\(symbol\s*$")
PROPERTY_START_RE = re.compile(r'(?m)^[\t ]*\(property "(Reference|Value)"\s+')
AT_RE = re.compile(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\)")


def balanced_end(source: str, start: int) -> int:
    """Return the exclusive end of the S-expression beginning at *start*."""
    if source[start] != "(":
        raise ValueError(f"expected '(' at {start}")
    depth = 0
    quote = False
    escaped = False
    for pos in range(start, len(source)):
        char = source[pos]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
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
    raise ValueError(f"unclosed S-expression at {start}")


def direct_child_block(source: str, opener: str) -> tuple[int, int]:
    """Locate the first named child expression inside a complete block."""
    marker = f"({opener}"
    index = source.find(marker)
    if index < 0:
        raise ValueError(f"missing {marker}")
    return index, balanced_end(source, index)


def text_effects(style: str) -> str:
    if style == "section":
        return (
            "(effects\n\t\t\t(font\n\t\t\t\t(size 1.778 "
            "1.778)\n\t\t\t\t(bold yes)\n\t\t\t)\n\t\t\t(justify "
            "left)\n\t\t)"
        )
    return "(effects\n\t\t\t(font\n\t\t\t\t(size 0.762 0.762)\n\t\t\t)\n\t\t\t(justify left)\n\t\t)"


def transform_text(block: str) -> tuple[str, str, str] | None:
    """Return replacement, old text, new text; master title is intentionally skipped."""
    match = TEXT_VALUE_RE.match(block)
    if not match:
        raise ValueError("unreadable text annotation")
    old_text = bytes(match.group(1), "utf-8").decode("unicode_escape")
    if old_text == MASTER_TITLE:
        return None
    new_text = WORDING.get(old_text, old_text)
    if new_text != old_text:
        encoded = new_text.replace("\\", "\\\\").replace('"', '\\"')
        block = block[: match.start(1)] + encoded + block[match.end(1) :]
    style = "section" if SECTION_RE.match(new_text) else "normal"
    effects_start, effects_end = direct_child_block(block, "effects")
    replacement = block[:effects_start] + text_effects(style) + block[effects_end:]
    return replacement, old_text, new_text


def normalize_free_text(source: str) -> tuple[str, list[tuple[str, str]]]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    while True:
        match = TEXT_START_RE.search(source, cursor)
        if not match:
            break
        start = source.rfind("(", 0, match.end())
        end = balanced_end(source, start)
        spans.append((start, end))
        cursor = end
    replacements: list[tuple[int, int, str]] = []
    changes: list[tuple[str, str]] = []
    for start, end in spans:
        outcome = transform_text(source[start:end])
        if outcome is None:
            continue
        replacement, old_text, new_text = outcome
        if replacement != source[start:end]:
            replacements.append((start, end, replacement))
            changes.append((old_text, new_text))
    for start, end, replacement in reversed(replacements):
        source = source[:start] + replacement + source[end:]
    return source, changes


def normalize_net_labels(source: str) -> tuple[str, int]:
    """Normalize only label typography; orientation/shape/nets are preserved."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    while True:
        match = NET_LABEL_START_RE.search(source, cursor)
        if not match:
            break
        start = source.rfind("(", 0, match.end())
        end = balanced_end(source, start)
        spans.append((start, end))
        cursor = end
    replacements: list[tuple[int, int, str]] = []
    for start, end in spans:
        block = source[start:end]
        # The endpoint-repair utility deliberately adds 0.01 mm labels only
        # so legacy one-grid wire ends remain electrically connected without
        # duplicating visible text.  They are implementation markers, not
        # presentation labels, and must remain invisible.
        if re.search(r"\(font\s+\(size\s+0\.01\s+0\.01\)\s*\)", block):
            continue
        updated = set_effects_font_and_justify(block, right_justify=False)
        if updated != block:
            replacements.append((start, end, updated))
    for start, end, replacement in reversed(replacements):
        source = source[:start] + replacement + source[end:]
    return source, len(replacements)


def is_hidden(property_block: str) -> bool:
    return bool(re.search(r"\(hide\s+yes\)", property_block))


def property_at(property_block: str) -> tuple[float, float, float]:
    match = AT_RE.search(property_block)
    if not match:
        raise ValueError("property has no position")
    return tuple(float(value) for value in match.groups())


def set_effects_font_and_justify(property_block: str, *, right_justify: bool) -> str:
    effects_start, effects_end = direct_child_block(property_block, "effects")
    effects = property_block[effects_start:effects_end]
    font_start, font_end = direct_child_block(effects, "font")
    font = effects[font_start:font_end]
    if not re.search(r"\(size\s+[-\d.]+\s+[-\d.]+\)", font):
        raise ValueError("property font has no size")
    # Rebuild the small effects tree instead of splicing a child before a
    # possibly same-line close parenthesis.  That was the source of malformed
    # `(justify ...)` nesting in the earlier generic pass.
    font_line_start = effects.rfind("\n", 0, font_start) + 1
    child_indent = re.match(r"[ \t]*", effects[font_line_start:font_start]).group(0)
    outer_indent = child_indent[:-1] if child_indent.endswith("\t") else child_indent
    existing_justify = re.search(r"\(justify(?:\s+[^)]*)?\)", effects)
    justify = (
        "(justify right)"
        if right_justify
        else (existing_justify.group(0) if existing_justify else "")
    )
    rebuilt = (
        "(effects\n"
        f"{child_indent}(font\n"
        f"{child_indent}\t(size 0.762 0.762)\n"
        f"{child_indent})\n"
        + (f"{child_indent}{justify}\n" if justify else "")
        + f"{outer_indent})"
    )
    return property_block[:effects_start] + rebuilt + property_block[effects_end:]


def update_property(
    property_block: str, *, target_x: float | None, right_justify: bool
) -> str:
    changed = set_effects_font_and_justify(property_block, right_justify=right_justify)
    if target_x is not None:
        match = AT_RE.search(changed)
        if not match:
            raise ValueError("property has no position")
        _, old_y, old_angle = property_at(changed)
        new_at = f"(at {target_x:.6g} {old_y:.6g} {old_angle:.6g})"
        changed = changed[: match.start()] + new_at + changed[match.end() :]
    return changed


def instance_symbol_blocks(source: str) -> list[tuple[int, int, str]]:
    lib_start = source.find("(lib_symbols")
    lib_end = balanced_end(source, lib_start) if lib_start >= 0 else -1
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    while True:
        match = SYMBOL_START_RE.search(source, cursor)
        if not match:
            break
        start = source.rfind("(", 0, match.end())
        end = balanced_end(source, start)
        cursor = end
        if lib_start <= start < lib_end:
            continue
        block = source[start:end]
        lib_id = re.search(r'\(lib_id\s+"([^"]+)"\)', block)
        if lib_id:
            spans.append((start, end, lib_id.group(1)))
    return spans


def visible_ref_value_blocks(
    symbol_block: str,
) -> list[tuple[int, int, str, float, float, float]]:
    items: list[tuple[int, int, str, float, float, float]] = []
    cursor = 0
    while True:
        match = PROPERTY_START_RE.search(symbol_block, cursor)
        if not match:
            break
        start = symbol_block.rfind("(", 0, match.end())
        end = balanced_end(symbol_block, start)
        cursor = end
        block = symbol_block[start:end]
        if not is_hidden(block):
            x, y, angle = property_at(block)
            items.append((start, end, match.group(1), x, y, angle))
    return items


def is_block_symbol(reference: str, lib_id: str) -> bool:
    if reference.startswith(("J", "U")):
        return True
    return reference.startswith("D") and not lib_id.startswith("Device:")


def normalize_visible_fields(source: str) -> tuple[str, int, int]:
    """Set visible Reference/Value text to 30 mil; align block-symbol pairs."""
    updates: list[tuple[int, int, str]] = []
    field_count = 0
    pair_count = 0
    for symbol_start, symbol_end, lib_id in instance_symbol_blocks(source):
        symbol = source[symbol_start:symbol_end]
        ref_match = re.search(r'\(property "Reference" "([^"]+)"', symbol)
        if not ref_match:
            continue
        reference = ref_match.group(1)
        items = visible_ref_value_blocks(symbol)
        by_name = {
            name: (start, end, x, y, angle) for start, end, name, x, y, angle in items
        }
        align_pair = (
            is_block_symbol(reference, lib_id)
            and "Reference" in by_name
            and "Value" in by_name
            and by_name["Reference"][4] == 0
            and by_name["Value"][4] == 0
        )
        target_x = None
        if align_pair:
            target_x = max(by_name["Reference"][2], by_name["Value"][2])
        pair_changed = False
        for start, end, _name, _x, _y, angle in items:
            right = bool(align_pair and angle == 0)
            updated = update_property(
                symbol[start:end],
                target_x=target_x if right else None,
                right_justify=right,
            )
            if updated != symbol[start:end]:
                updates.append((symbol_start + start, symbol_start + end, updated))
                field_count += 1
                pair_changed = pair_changed or align_pair
        if pair_changed:
            pair_count += 1
    for start, end, replacement in reversed(updates):
        source = source[:start] + replacement + source[end:]
    return source, field_count, pair_count


def normalize(
    source: str, *, fields: bool, labels: bool
) -> tuple[str, list[tuple[str, str]], int, int, int]:
    source, text_changes = normalize_free_text(source)
    label_count = 0
    if labels:
        source, label_count = normalize_net_labels(source)
    if fields:
        source, field_count, pair_count = normalize_visible_fields(source)
    else:
        field_count, pair_count = 0, 0
    return source, text_changes, label_count, field_count, pair_count


def read_exact(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def write_atomic(path: Path, content: str) -> None:
    temp = path.with_suffix(path.suffix + ".presentation.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schematic", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        help="destination for an atomic --apply result; defaults to the input schematic",
    )
    parser.add_argument(
        "--fields",
        action="store_true",
        help="also set visible Reference/Value fields to 30 mil and align J/U/block-D pairs",
    )
    parser.add_argument(
        "--labels",
        action="store_true",
        help="also set visible net-label text to 30 mil without moving labels or nets",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.output and not args.apply:
        parser.error("--output requires --apply")
    source = read_exact(args.schematic)
    normalized, changes, label_count, field_count, pair_count = normalize(
        source, fields=args.fields, labels=args.labels
    )
    heading_count = sum(1 for old, new in changes if SECTION_RE.match(new))
    wording_count = sum(1 for old, new in changes if old != new)
    print(f"text annotations changed: {len(changes)}")
    print(f"numbered section headings normalized: {heading_count}")
    print(f"section-23 wording changes: {wording_count}")
    if args.labels:
        print(f"net-label text normalized: {label_count}")
    if args.fields:
        print(f"visible Reference/Value fields normalized: {field_count}")
        print(f"J/U/block-D field pairs right-aligned: {pair_count}")
    if args.apply and normalized != source:
        write_atomic(args.output or args.schematic, normalized)
        print("applied atomically")
    elif args.apply:
        print("already normalized")
    return 0


if __name__ == "__main__":
    sys.exit(main())
