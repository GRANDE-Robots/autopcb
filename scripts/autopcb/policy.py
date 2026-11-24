"""policy.py: compile the routing defaults and native board limits into a project policy.

All distances in the policy are millimeters.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import json
import math
from pathlib import Path


class RoutingPolicy:
    """The conservative routing policy written to a project's configuration."""

    @staticmethod
    def _dimensions(defaults: Path | None) -> dict:
        """The routing dimensions, with the values a defaults file overrides.

        Inputs:
            defaults: Version 1 JSON file of routing defaults; none is read when it
                is None or missing.
        Outputs:
            Search margin, step, track width, clearance, via size and via drill, in
            mm. Raises ValueError when the file is not a version 1 object that
            holds only these names as positive, finite numbers.
        """
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
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value <= 0
                for value in source.values()
            ):
                raise ValueError(
                    "Routing defaults must be positive, finite dimensions in millimeters."
                )
            dimensions.update(source)
        return dimensions

    @classmethod
    def compile(
        cls,
        layers,
        *,
        defaults: Path | None = None,
        minima: dict | None = None,
        excluded=(),
        refill: bool = True,
    ) -> dict:
        """Compile a routing policy that respects the board's own limits.

        Inputs:
            layers: Copper layer names the router may use.
            defaults: Routing defaults file; see ``_dimensions``.
            minima: Board design-rule minima in mm: ``min_clearance``,
                ``min_track_width``, ``min_through_hole_diameter``,
                ``min_via_annular_width`` and ``min_via_diameter``. A default
                dimension below its minimum is raised to it.
            excluded: Net names the router must leave alone.
            refill: Whether routing assumes zones are refilled afterwards.
        Outputs:
            The policy: version 1 route settings, a connectivity working scope
            that is not manufacturing-approved, and the sorted excluded nets.
            Raises ValueError for invalid defaults.
        """
        dimensions = cls._dimensions(defaults)
        native = minima or {}
        clearance = max(dimensions["clearance_mm"], native.get("min_clearance", 0))
        width = max(dimensions["width_mm"], native.get("min_track_width", 0))
        hole = max(
            dimensions["via_drill_mm"], native.get("min_through_hole_diameter", 0)
        )
        annulus = max(0.15, native.get("min_via_annular_width", 0))
        size = max(
            dimensions["via_size_mm"],
            hole + 2 * annulus,
            native.get("min_via_diameter", 0),
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
