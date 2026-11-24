"""Compile the routing defaults and native board limits into a project policy."""

from __future__ import annotations

import json
import math
from pathlib import Path


def routing_policy(
    layers,
    *,
    defaults: Path | None = None,
    minima: dict | None = None,
    excluded=(),
    refill: bool = True,
) -> dict:
    dimensions = {
        "search_margin_mm": 6.0,
        "step_mm": 0.15,
        "width_mm": 0.25,
        "clearance_mm": 0.25,
        "via_size_mm": 0.65,
        "via_drill_mm": 0.30,
    }
    if defaults is not None and defaults.is_file():
        source = json.loads(defaults.read_text(encoding="utf-8-sig"))
        if not isinstance(source, dict):
            raise ValueError("Routing defaults must be a JSON object.")
        version = source.pop("schema_version", None)
        if (
            type(version) is not int
            or version != 1
            or source.keys() - dimensions.keys()
        ):
            raise ValueError(
                "Expected version 1 routing defaults containing only geometry and search distances."
            )
        if any(
            type(value) not in (int, float) or not math.isfinite(value) or value <= 0
            for value in source.values()
        ):
            raise ValueError(
                "Routing defaults must be positive, finite dimensions in millimeters."
            )
        dimensions.update(source)
    native = minima or {}
    clearance = max(dimensions["clearance_mm"], native.get("min_clearance", 0))
    width = max(dimensions["width_mm"], native.get("min_track_width", 0))
    hole = max(dimensions["via_drill_mm"], native.get("min_through_hole_diameter", 0))
    annulus = max(0.15, native.get("min_via_annular_width", 0))
    size = max(
        dimensions["via_size_mm"], hole + 2 * annulus, native.get("min_via_diameter", 0)
    )
    return {
        "schema": 1,
        "max_pins": 3,
        "search_margin_mm": dimensions["search_margin_mm"],
        "route": {
            "layers": list(layers),
            "width_mm": width,
            "clearance_mm": clearance,
            "step_mm": dimensions["step_mm"],
            "via": {"size_mm": size, "drill_mm": hole},
            "assume_zone_refill": refill,
        },
        "working_scope": {
            "kind": "connectivity",
            "classes": ["Default"],
            "manufacturing_approved": False,
        },
        "exclude_nets": sorted(set(excluded)),
    }
