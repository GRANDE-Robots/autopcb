"""Apply supplied electrical limits to geometric routing proposals.

The caller supplies conservative, documented limits for the particular branch,
stackup and environment. This module checks those limits; it does not derive
ampacity or insulation ratings from current and voltage alone, and a passing
result is not an electrical, thermal, insulation or manufacturing approval.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from pcb.route_optimizer import COPPER_LAYERS, Point, Route, mm, to_mm


class ElectricalConstraintViolation(ValueError):
    """A proposed route fails a supplied electrical limit."""


def _object(value: object, allowed: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown {name} fields: {', '.join(sorted(unknown))}")
    return value


def _positive(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return result


def _dimension(value: object, name: str) -> int:
    _positive(value, name)
    result = mm(value)
    if result <= 0:
        raise ValueError(f"{name} must be at least one nanometre")
    return result


def _basis(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} needs a nonempty source or assessment basis")
    return value.strip()


@dataclass(frozen=True)
class TrackLimit:
    minimum_width: int
    maximum_current_a: float
    copper_thickness: int
    basis: str


@dataclass(frozen=True)
class ViaLimit:
    minimum_size: int
    minimum_drill: int
    plating_thickness: int
    maximum_current_a: float
    maximum_resistance_ohm: float
    basis: str


@dataclass(frozen=True)
class ExistingVia:
    """Dimensions of a same-net through via from a stable native snapshot."""

    at: Point
    net: str
    sizes: tuple[tuple[str, int], ...]
    drill: int
    through: bool


def via_sizes_meet(sizes: tuple[tuple[str, int], ...], required_layers, *,
                   minimum_size: int = 0, exact_size: int | None = None) -> bool:
    """Check every observed annulus on the real stack, not all possible layers."""
    observed = dict(sizes)
    return (len(observed) == len(sizes)
            and {"F.Cu", "B.Cu", *required_layers} <= observed.keys()
            and all(layer in COPPER_LAYERS and isinstance(size, int) and not isinstance(size, bool)
                    and size > 0 and size >= minimum_size
                    and (exact_size is None or size == exact_size)
                    for layer, size in observed.items()))


@dataclass(frozen=True)
class ElectricalConstraints:
    current_a: float
    current_basis: str
    voltage_difference_v: float
    voltage_basis: str
    minimum_clearance: int
    clearance_voltage_v: float
    clearance_basis: str
    maximum_voltage_drop_v: float
    resistivity_ohm_mm: float
    voltage_drop_basis: str
    tracks: Mapping[str, TrackLimit]
    via: ViaLimit | None

    @classmethod
    def from_config(cls, value: object) -> ElectricalConstraints:
        data = _object(
            value,
            {
                "current_a",
                "current_basis",
                "voltage_difference_v",
                "voltage_basis",
                "clearance",
                "voltage_drop",
                "tracks",
                "via",
            },
            "electrical constraints",
        )
        clearance = _object(
            data.get("clearance"),
            {"min_mm", "max_voltage_difference_v", "basis"},
            "electrical clearance",
        )
        drop = _object(
            data.get("voltage_drop"),
            {"max_v", "copper_resistivity_ohm_mm", "basis"},
            "voltage drop",
        )
        raw_tracks = data.get("tracks")
        if not isinstance(raw_tracks, dict) or not raw_tracks:
            raise ValueError("electrical tracks must specify at least one copper layer")
        tracks: dict[str, TrackLimit] = {}
        for layer, item in raw_tracks.items():
            if layer not in COPPER_LAYERS:
                raise ValueError(f"Unknown electrical track layer: {layer}")
            entry = _object(
                item,
                {"min_width_mm", "max_current_a", "copper_thickness_mm", "basis"},
                f"electrical track {layer}",
            )
            tracks[layer] = TrackLimit(
                _dimension(entry.get("min_width_mm"), "track minimum width"),
                _positive(entry.get("max_current_a"), "track maximum current"),
                _dimension(entry.get("copper_thickness_mm"), "track copper thickness"),
                _basis(entry.get("basis"), "track limit"),
            )
        via = None
        if data.get("via") is not None:
            entry = _object(
                data["via"],
                {
                    "min_size_mm",
                    "min_drill_mm",
                    "plating_thickness_mm",
                    "max_current_a",
                    "max_resistance_ohm",
                    "basis",
                },
                "electrical via",
            )
            via = ViaLimit(
                _dimension(entry.get("min_size_mm"), "via minimum size"),
                _dimension(entry.get("min_drill_mm"), "via minimum drill"),
                _dimension(entry.get("plating_thickness_mm"), "via plating thickness"),
                _positive(entry.get("max_current_a"), "via maximum current"),
                _positive(entry.get("max_resistance_ohm"), "via maximum resistance"),
                _basis(entry.get("basis"), "via limit"),
            )
            if via.minimum_size <= via.minimum_drill:
                raise ValueError(
                    "Via minimum size must be larger than its minimum drill"
                )
        return cls(
            _positive(data.get("current_a"), "route current"),
            _basis(data.get("current_basis"), "route current"),
            _positive(
                data.get("voltage_difference_v"), "worst-case voltage difference"
            ),
            _basis(data.get("voltage_basis"), "voltage difference"),
            _dimension(clearance.get("min_mm"), "minimum electrical clearance"),
            _positive(
                clearance.get("max_voltage_difference_v"), "clearance voltage limit"
            ),
            _basis(clearance.get("basis"), "clearance limit"),
            _positive(drop.get("max_v"), "maximum voltage drop"),
            _positive(drop.get("copper_resistivity_ohm_mm"), "copper resistivity"),
            _basis(drop.get("basis"), "voltage drop limit"),
            tracks,
            via,
        )

    def eligible_layers(
        self, layers: list[str], width: int, clearance: int
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        if clearance < self.minimum_clearance:
            raise ValueError("Route clearance is below the supplied electrical minimum")
        if self.voltage_difference_v > self.clearance_voltage_v:
            raise ValueError(
                "Worst-case voltage difference exceeds the supplied clearance rating"
            )
        eligible, excluded = [], {}
        for layer in layers:
            limit = self.tracks.get(layer)
            if limit is None:
                excluded[layer] = "no supplied track rating"
            elif width < limit.minimum_width:
                excluded[layer] = "track width is below the supplied current limit"
            elif self.current_a > limit.maximum_current_a:
                excluded[layer] = "route current exceeds the supplied track rating"
            else:
                eligible.append(layer)
        if not eligible:
            raise ValueError(
                "No requested layer satisfies the supplied electrical track limits"
            )
        return tuple(eligible), excluded

    def check_vias(
        self,
        route: Route,
        net: str,
        size: int,
        drill: int,
        existing: tuple[ExistingVia, ...],
    ) -> list[dict[str, Any]]:
        if not route.vias and not route.reused_vias:
            return []
        limit = self.via
        if limit is None:
            raise ElectricalConstraintViolation(
                "A route using vias needs a supplied per-via current limit"
            )
        if self.current_a > limit.maximum_current_a:
            raise ElectricalConstraintViolation("Route current exceeds the supplied per-via current limit")
        checks = []
        for via in route.vias:
            if size < limit.minimum_size or drill < limit.minimum_drill:
                raise ElectricalConstraintViolation(
                    "Proposed via geometry is below the supplied current limit"
                )
            checks.append(
                {
                    "at_mm": via.at.as_mm(),
                    "kind": "new",
                    "size_mm": to_mm(size),
                    "drill_mm": to_mm(drill),
                }
            )
        for via in route.reused_vias:
            matches = [
                item for item in existing if item.at == via.at and item.net == net
            ]
            if len(matches) != 1 or not matches[0].through:
                raise ElectricalConstraintViolation(
                    "Reused via geometry or through-via type is not established"
                )
            actual = matches[0]
            sizes = dict(actual.sizes)
            if (
                not via_sizes_meet(actual.sizes, self.tracks, minimum_size=limit.minimum_size)
                or actual.drill < limit.minimum_drill
            ):
                raise ElectricalConstraintViolation(
                    "Reused via geometry is below the supplied current limit"
                )
            checks.append(
                {
                    "at_mm": via.at.as_mm(),
                    "kind": "existing",
                    "minimum_size_mm": to_mm(min(sizes.values())),
                    "drill_mm": to_mm(actual.drill),
                }
            )
        return checks

    def check_configured_stackup(
        self, layers: list[str], copper: Mapping[str, int]
    ) -> None:
        """Reject a rating that assumes more copper than the saved stackup specifies."""
        for layer in layers:
            if layer not in copper:
                raise ValueError(
                    f"Saved PCB has no configured copper thickness for {layer}"
                )
            if self.tracks[layer].copper_thickness > copper[layer]:
                raise ValueError(
                    f"Electrical rating assumes more copper than the PCB specifies for {layer}"
                )

    def check_voltage_drop(self, route: Route, width: int) -> dict[str, Any]:
        """Bound resistive loss for the proposed route, not the whole circuit."""
        track_resistance = 0.0
        for segment in route.segments:
            length = math.hypot(
                segment.end.x - segment.start.x, segment.end.y - segment.start.y
            )
            length = to_mm(length)
            copper = to_mm(self.tracks[segment.layer].copper_thickness)
            track_resistance += (
                self.resistivity_ohm_mm * length / (to_mm(width) * copper)
            )
        via_count = len(route.vias) + len(route.reused_vias)
        via_resistance = (
            0.0 if not via_count else via_count * self.via.maximum_resistance_ohm
        )
        resistance = track_resistance + via_resistance
        drop = self.current_a * resistance
        if drop > self.maximum_voltage_drop_v:
            raise ElectricalConstraintViolation(
                "Proposed route exceeds its supplied voltage-drop limit; "
                "another route may still meet it"
            )
        return {
            "maximum_v": self.maximum_voltage_drop_v,
            "estimated_v": drop,
            "estimated_resistance_ohm": resistance,
            "estimated_power_w": self.current_a * self.current_a * resistance,
            "copper_resistivity_ohm_mm": self.resistivity_ohm_mm,
            "basis": self.voltage_drop_basis,
        }

    def allows_via_geometry(self, size: int, drill: int) -> bool:
        """Each via must carry the whole declared current without assumed sharing."""
        limit = self.via
        return bool(
            limit is not None
            and self.current_a <= limit.maximum_current_a
            and size >= limit.minimum_size
            and drill >= limit.minimum_drill
        )

    def eligible_existing_vias(
        self, existing: tuple[ExistingVia, ...], net: str
    ) -> set[Point]:
        limit = self.via
        if limit is None or self.current_a > limit.maximum_current_a:
            return set()
        points: set[Point] = set()
        for item in existing:
            if (
                item.net == net
                and item.through
                and item.drill >= limit.minimum_drill
                and via_sizes_meet(item.sizes, self.tracks, minimum_size=limit.minimum_size)
                and sum(other.at == item.at and other.net == net for other in existing)
                == 1
            ):
                points.add(item.at)
        return points

    def assessment(
        self,
        layers: tuple[str, ...],
        excluded: dict[str, str],
        via_checks: list[dict[str, Any]],
        voltage_drop: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "status": "within_supplied_limits",
            "current_a": self.current_a,
            "current_basis": self.current_basis,
            "worst_case_voltage_difference_v": self.voltage_difference_v,
            "voltage_basis": self.voltage_basis,
            "minimum_clearance_mm": to_mm(self.minimum_clearance),
            "clearance_maximum_voltage_difference_v": self.clearance_voltage_v,
            "clearance_basis": self.clearance_basis,
            "eligible_layers": list(layers),
            "excluded_layers": excluded,
            "track_limits": {
                layer: {
                    "minimum_width_mm": to_mm(self.tracks[layer].minimum_width),
                    "maximum_current_a": self.tracks[layer].maximum_current_a,
                    "copper_thickness_mm": to_mm(self.tracks[layer].copper_thickness),
                    "basis": self.tracks[layer].basis,
                }
                for layer in layers
            },
            "via_checks": via_checks,
            "voltage_drop": voltage_drop,
            "via_basis": self.via.basis if self.via is not None else None,
            "via_maximum_current_a": self.via.maximum_current_a
            if self.via is not None
            else None,
            "via_maximum_resistance_ohm": (
                self.via.maximum_resistance_ohm if self.via is not None else None
            ),
            "via_minimum_size_mm": to_mm(self.via.minimum_size)
            if self.via is not None
            else None,
            "via_minimum_drill_mm": to_mm(self.via.minimum_drill)
            if self.via is not None
            else None,
            "via_assumed_plating_thickness_mm": (
                to_mm(self.via.plating_thickness) if self.via is not None else None
            ),
            "limitations": (
                "Checks use supplied ratings and do not verify their basis, actual stackup, "
                "existing copper and components, thermal coupling, return path, protection, "
                "pairwise insulation or physical fabrication."
            ),
        }
