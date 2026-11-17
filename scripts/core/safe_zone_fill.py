"""Guard KiCad's assertion-prone filled-polygon accessor.

Declared layer membership is NOT evidence that the per-layer fill cache exists.
Do not replace this guard with GetLayer(), GetLayerSet() or exception handling.
"""

from __future__ import annotations

import re
from collections import Counter


def safe_filled_polys(zone, layer):
    """Return an explicit availability record and, only when safe, polygon data.

    UNKNOWN means unavailable to this readback, never empty copper or a pass.
    Native assertions are not catchable Python exceptions: query the stored-map
    predicate FIRST. If the installed binding lacks it, use native source records.
    """
    predicate = getattr(zone, "HasFilledPolysForLayer", None)
    if not callable(predicate):
        return {
            "status": "UNKNOWN",
            "reason": "No safe filled-map predicate is exposed",
            "polygon_count": None,
        }, None
    try:
        present = bool(predicate(layer))
    except Exception as exc:
        return {
            "status": "UNKNOWN",
            "reason": "Filled-map predicate failed: " + str(exc),
            "polygon_count": None,
        }, None
    if not present:
        return {
            "status": "UNKNOWN",
            "reason": "No stored filled-polygon entry for this layer",
            "polygon_count": None,
        }, None
    accessor = getattr(zone, "GetFilledPolysList", None)
    if not callable(accessor):
        return {
            "status": "UNKNOWN",
            "reason": "Filled-polygon accessor unavailable",
            "polygon_count": None,
        }, None
    try:
        polys = accessor(layer)
        if polys is None:
            return {
                "status": "UNKNOWN",
                "reason": "Accessor returned no polygon object",
                "polygon_count": None,
            }, None
        count = int(polys.OutlineCount())
    except Exception as exc:
        return {
            "status": "UNKNOWN",
            "reason": "Guarded accessor failed: " + str(exc),
            "polygon_count": None,
        }, None
    return {
        "status": "AVAILABLE_NONEMPTY" if count else "AVAILABLE_EMPTY",
        "reason": "Stored-map predicate verified before accessor",
        "polygon_count": count,
    }, polys


def balanced_expression(text, start):
    """Extract a native source expression without invoking pcbnew geometry APIs."""
    assert text[start] == "("
    depth = 0
    quoted = escaped = False
    for offset in range(start, len(text)):
        ch = text[offset]
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start : offset + 1]
    raise ValueError("Unbalanced native expression")


def native_zone_fill_records(text):
    """Inventory explicitly saved fill records, independently of SWIG cache state.

    Absence is UNKNOWN/unavailable, not proof of an intentionally empty plane.
    This inventory alone does not claim continuity or physical copper coverage.
    """
    out = []
    for match in re.finditer(r"(?m)^\t\(zone\s*$", text):
        start = text.index("(", match.start())
        block = balanced_expression(text, start)
        uuid = re.search(r'\(uuid "([^"]+)"\)', block)
        net = re.search(r'\(net "([^"]*)"\)', block)
        fills = Counter(re.findall(r'\(filled_polygon\s*\(layer "([^"]+)"\)', block))
        out.append(
            {
                "uuid": uuid[1] if uuid else None,
                "net": net[1] if net else None,
                "stored_polygon_counts": dict(fills),
                "source_status": "SAVED_FILL_RECORDS_PRESENT"
                if fills
                else "UNKNOWN_NO_SAVED_FILL_RECORDS",
            }
        )
    return out
