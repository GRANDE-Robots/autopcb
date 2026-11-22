"""Compile saved routing dimensions for search and copper materialization.

This is deliberately a supported subset of KiCad's rule language, not a DRC
replacement. Unsupported expressions stop planning. New, ungrouped copper
does not inherit coordinate-specific or group-specific legacy exceptions.
Native DRC remains the independent acceptance gate for every candidate.
"""

from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from core.kicad_sexpr import parse
from core.pcb_io import net_classes
from pcb.route_optimizer import (
    COPPER_LAYERS, Obstacle, Point, Segment, _point_near_segment, _segments_intersect, mm,
)


_TOKEN = re.compile(
    r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|(?:\d+(?:\.\d*)?|\.\d+)(?:mm|mil)|&&|\|\||!=|!"
)
_FIELDS = {
    "Type",
    "Layer",
    "NetName",
    "NetClass",
}
_COORDINATE_FIELDS = {"Origin_X", "Origin_Y", "Start_X", "Start_Y", "End_X", "End_Y"}
_METHODS = {"intersectsCourtyard", "memberOfGroup"}


def _expression(source: str):
    def substitute(match):
        value = match.group()
        if value[0] in "'\"":
            return value
        if value in {"&&", "||", "!", "!="}:
            return {"&&": " and ", "||": " or ", "!": " not ", "!=": "!="}[value]
        return str(
            mm(value[:-2]) if value.endswith("mm") else round(float(value[:-3]) * 25400)
        )

    tree = ast.parse(_TOKEN.sub(substitute, source).strip(), mode="eval")
    allowed = (
        ast.Expression,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.UnaryOp,
        ast.Not,
        ast.USub,
        ast.UAdd,
        ast.Compare,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.Constant,
        ast.Name,
        ast.Attribute,
        ast.Load,
        ast.Call,
    )
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError(f"Unsupported rule expression: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in {"A", "B"}:
            raise ValueError(f"Unsupported rule name: {node.id}")
        if isinstance(node, ast.Attribute) and (
            not isinstance(node.value, ast.Name)
            or node.value.id not in {"A", "B"}
            or node.attr not in _FIELDS | _METHODS | _COORDINATE_FIELDS
        ):
            raise ValueError(f"Unsupported rule attribute: {node.attr}")
        if isinstance(node, ast.Call) and (
            not isinstance(node.func, ast.Attribute)
            or node.func.attr not in _METHODS
            or len(node.args) != 1
            or node.keywords
            or not isinstance(node.args[0], ast.Constant)
            or not isinstance(node.args[0].value, str)
        ):
            raise ValueError("Unsupported rule function")
    return tree, compile(tree, "<saved KiCad condition>", "eval")


@dataclass(frozen=True)
class RuleItem:
    Type: str
    Layer: str
    NetName: str
    NetClass: str
    courtyards: frozenset[str] = frozenset()

    def intersectsCourtyard(self, reference: str) -> bool:
        return reference in self.courtyards

    def memberOfGroup(self, name: str) -> bool:
        return False


@dataclass(frozen=True)
class DimensionRule:
    name: str
    kind: str
    minimum: int | None
    maximum: int | None
    expression: object
    layers: frozenset[str]
    local: bool = False
    courtyard_refs: frozenset[str] = frozenset()
    nonmonotonic_local: bool = False
    uncertain_group: bool = False
    net_specific: bool = False

    def matches(self, first: RuleItem, second: RuleItem) -> bool:
        return first.Layer in self.layers and bool(
            eval(self.expression, {"__builtins__": {}}, {"A": first, "B": second})
        )


def read_rules(path: Path) -> tuple[DimensionRule, ...]:
    if not path.is_file():
        return ()
    # A comment marker inside a quoted condition/name is ordinary text.
    source = re.sub(
        r'"(?:\\.|[^"\\])*"|#[^\r\n]*',
        lambda m: "" if m.group().startswith("#") else m.group(),
        path.read_text(encoding="utf-8-sig"),
    )
    document = parse("(rules\n" + source + "\n)")
    versions = document.children("version")
    if len(versions) != 1 or versions[0].atoms()[1].value != "1":
        raise ValueError("Unsupported saved KiCad design-rule version")
    result = []
    for form in document.children("rule"):
        constraints = [
            c
            for c in form.children("constraint")
            if c.atoms()[1].value in {"track_width", "clearance"}
        ]
        if not constraints:
            continue
        name = form.atoms()[1].value
        conditions = form.children("condition")
        condition = conditions[0].atoms()[1].value if conditions else "True"
        try:
            tree, expression = _expression(condition)
        except (SyntaxError, ValueError) as error:
            raise ValueError(f"Saved rule {name!r}: {error}") from error
        # Existing-item exceptions are never widened to a new route or group.
        attributes = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        def requires_named_net(node):
            if isinstance(node, ast.Expression):
                return requires_named_net(node.body)
            if isinstance(node, ast.BoolOp):
                values = [requires_named_net(part) for part in node.values]
                return any(values) if isinstance(node.op, ast.And) else all(values)
            if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
                sides = (node.left, node.comparators[0])
                return any(isinstance(a, ast.Attribute) and a.attr == 'NetName'
                           and isinstance(b, ast.Constant) and isinstance(b.value, str)
                           for a, b in (sides, sides[::-1]))
            return False
        def courtyard_calls(node):
            return [part for part in ast.walk(node) if isinstance(part, ast.Call)
                    and isinstance(part.func, ast.Attribute)
                    and part.func.attr == "intersectsCourtyard"]

        references = frozenset(call.args[0].value for call in courtyard_calls(tree))
        if any(re.match(r"^\$\{class:", value, flags=re.IGNORECASE) for value in references):
            raise ValueError(
                f"Saved rule {name!r}: component-class courtyard selectors are not supported"
            )
        nonmonotonic = any(
            ((isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not))
             or isinstance(node, ast.Compare)) and bool(courtyard_calls(node))
            for node in ast.walk(tree)
        )
        if attributes & _COORDINATE_FIELDS:
            continue
        layer = form.children("layer")
        selected = layer[0].atoms()[1].value if layer else "*.Cu"
        layers = {
            "outer": {"F.Cu", "B.Cu"},
            "inner": set(COPPER_LAYERS) - {"F.Cu", "B.Cu"},
            "*.Cu": set(COPPER_LAYERS),
        }.get(selected, {selected})
        if not layers <= set(COPPER_LAYERS):
            raise ValueError(f"Unsupported layer selector in saved rule {name!r}")
        for constraint in constraints:
            bounds = {}
            for bound in constraint.children():
                if bound.name not in {"min", "max", "opt"}:
                    raise ValueError(f"Unsupported dimension in saved rule {name!r}")
                value = bound.atoms()[1].value
                if value.endswith("mm"):
                    dimension = mm(value[:-2])
                elif value.endswith("mil"):
                    dimension = round(float(value[:-3]) * 25400)
                else:
                    dimension = mm(value)
                if dimension < 0:
                    raise ValueError(f"Negative dimension in saved rule {name!r}")
                bounds[bound.name] = dimension
            result.append(
                DimensionRule(
                    name,
                    constraint.atoms()[1].value,
                    bounds.get("min"),
                    bounds.get("max"),
                    expression,
                    frozenset(layers),
                    "intersectsCourtyard" in attributes,
                    references,
                    nonmonotonic,
                    "memberOfGroup" in attributes,
                    requires_named_net(tree),
                )
            )
    return tuple(reversed(result))  # KiCad: the last matching custom rule wins.


@dataclass(frozen=True)
class Courtyard:
    reference: str
    layer: str
    polygon: tuple[Point, ...]
    library_id: str = ""
    bounds: tuple[int, int, int, int] = field(init=False)

    def __post_init__(self):
        object.__setattr__(
            self,
            "bounds",
            (
                min(p.x for p in self.polygon),
                min(p.y for p in self.polygon),
                max(p.x for p in self.polygon),
                max(p.y for p in self.polygon),
            ),
        )

    def contains(self, point: Point) -> bool:
        x0, y0, x1, y1 = self.bounds
        if not x0 <= point.x <= x1 or not y0 <= point.y <= y1:
            return False
        inside = False
        for a, b in zip(self.polygon, (*self.polygon[1:], self.polygon[0])):
            cross = (b.x - a.x) * (point.y - a.y) - (b.y - a.y) * (point.x - a.x)
            if (
                cross == 0
                and min(a.x, b.x) <= point.x <= max(a.x, b.x)
                and min(a.y, b.y) <= point.y <= max(a.y, b.y)
            ):
                return True
            if (a.y > point.y) != (b.y > point.y):
                if (cross > 0) == (b.y > a.y):
                    inside = not inside
        return inside

    def cuts(self, segment: Segment) -> set[int]:
        """Integer lattice positions immediately bracketing polygon crossings."""
        x0, y0, x1, y1 = self.bounds
        if (
            max(segment.start.x, segment.end.x) < x0
            or min(segment.start.x, segment.end.x) > x1
            or max(segment.start.y, segment.end.y) < y0
            or min(segment.start.y, segment.end.y) > y1
        ):
            return set()
        dx, dy = segment.end.x - segment.start.x, segment.end.y - segment.start.y
        length = max(abs(dx), abs(dy))
        result = set()
        for a, b in zip(self.polygon, (*self.polygon[1:], self.polygon[0])):
            ex, ey = b.x - a.x, b.y - a.y
            denominator = dx * ey - dy * ex
            if denominator == 0:
                continue
            px, py = a.x - segment.start.x, a.y - segment.start.y
            t, u = (
                Fraction(px * ey - py * ex, denominator),
                Fraction(px * dy - py * dx, denominator),
            )
            if 0 <= t <= 1 and 0 <= u <= 1:
                result.update((math.floor(t * length), math.ceil(t * length)))
        return result

    def intersects_track(self, segment: Segment, radius: int) -> bool:
        """Check the complete straight track capsule against this courtyard."""
        x0, y0, x1, y1 = self.bounds
        a, b = segment.start, segment.end
        if (max(a.x, b.x) + radius < x0 or min(a.x, b.x) - radius > x1
                or max(a.y, b.y) + radius < y0 or min(a.y, b.y) - radius > y1):
            return False
        if self.contains(a) or self.contains(b):
            return True
        for c, d in zip(self.polygon, (*self.polygon[1:], self.polygon[0])):
            if (_segments_intersect(a, b, c, d)
                    or _point_near_segment(a, c, d, radius)
                    or _point_near_segment(b, c, d, radius)
                    or _point_near_segment(c, a, b, radius)
                    or _point_near_segment(d, a, b, radius)):
                return True
        return False


class RoutingRules:
    """Native net-class defaults plus supported saved custom dimensions."""

    def __init__(
        self, rules, courtyards, classes, assignments, minimum_width, minimum_clearance,
        uncertain_courtyards=None,
    ):
        self.rules, self.courtyards = tuple(rules), tuple(courtyards)
        self.rule_courtyards = frozenset(ref for rule in self.rules for ref in rule.courtyard_refs)
        self._selector_patterns = {
            selector: re.compile(
                "".join(".*" if char == "*" else "." if char == "?" else re.escape(char)
                        for char in selector)
            )
            for selector in self.rule_courtyards
        }
        self._selector_cache = {}
        self.classes, self.assignments = classes, assignments
        self.minimum_width, self.minimum_clearance = minimum_width, minimum_clearance
        self.uncertain_courtyards = {
            layer: frozenset(references)
            for layer, references in (uncertain_courtyards or {}).items()
        }

    def courtyard_selectors(self, reference: str, library_id: str) -> frozenset[str]:
        """Resolve the selectors in saved rules against a footprint identity."""
        key = reference, library_id
        if key not in self._selector_cache:
            self._selector_cache[key] = frozenset(
                selector
                for selector, pattern in self._selector_patterns.items()
                if pattern.fullmatch(library_id if ":" in selector else reference)
            )
        return self._selector_cache[key]

    @classmethod
    def from_board(cls, board, rules_path: Path):
        import pcbnew

        classes = {
            name: (value.GetTrackWidth(), value.GetClearance())
            for name, value in net_classes(board).items()
        }
        assignments = {}
        for pad in board.GetPads():
            if pad.GetNetname():
                assignments[pad.GetNetname()] = pad.GetNetClassName()
        rules = read_rules(rules_path)
        courtyards = []
        uncertain_shapes = []
        for footprint in board.GetFootprints():
            for layer, identifier in (
                ("F.Cu", pcbnew.F_CrtYd),
                ("B.Cu", pcbnew.B_CrtYd),
            ):
                shape = footprint.GetCourtyard(identifier)
                reference = footprint.GetReference()
                library_id = footprint.GetFPIDAsString()
                for index in range(shape.OutlineCount()):
                    if shape.HoleCount(index):
                        uncertain_shapes.append((reference, library_id))
                        continue
                    outline = shape.COutline(index)
                    polygon = tuple(
                        Point(outline.CPoint(i).x, outline.CPoint(i).y)
                        for i in range(outline.PointCount())
                    )
                    if len(polygon) >= 3:
                        courtyards.append(
                            Courtyard(reference, layer, polygon, library_id)
                        )
                    else:
                        uncertain_shapes.append((reference, library_id))
        design = board.GetDesignSettings()
        result = cls(
            rules,
            courtyards,
            classes,
            assignments,
            design.m_TrackMinWidth,
            design.m_MinClearance,
        )
        uncertain_selectors = frozenset(
            selector for reference, library_id in uncertain_shapes
            for selector in result.courtyard_selectors(reference, library_id)
        )
        # intersectsCourtyard() considers both sides for copper on every layer.
        result.uncertain_courtyards = {
            layer: uncertain_selectors for layer in COPPER_LAYERS
        }
        return result

    def profile(
        self,
        net: str,
        width: int,
        clearance: int,
        *,
        width_floor: int = 0,
        clearance_floor: int = 0,
    ):
        name = self.assignments.get(net, "Default")
        if name not in self.classes:
            raise ValueError(
                f"{net}: native effective net class {name!r} is not available"
            )
        return RoutingProfile(
            self, net, name, width, clearance, width_floor, clearance_floor
        )


class RoutingProfile:
    def __init__(
        self, source, net, net_class, width, clearance, width_floor, clearance_floor
    ):
        self.source, self.net, self.net_class = source, net, net_class
        self.width, self.clearance = width, clearance
        # KiCad uses Board Setup dimensions as defaults; a matching custom
        # rule can override them. Explicit caller floors (for example, a
        # qualified electrical limit) remain hard constraints everywhere.
        # width_at/clearance_at retain board defaults outside proven overrides.
        self.width_floor = max(1, width_floor)
        self.clearance_floor = max(0, clearance_floor)
        self._courtyard_selectors = {
            c: selectors
            for c in source.courtyards
            if (selectors := source.courtyard_selectors(c.reference, c.library_id))
        }
        self.courtyards = tuple(self._courtyard_selectors)
        self._region_step = mm(4)
        self._regions = {layer: {} for layer in COPPER_LAYERS}
        for courtyard in self.courtyards:
            x0, y0, x1, y1 = courtyard.bounds
            for x in range(x0 // self._region_step, x1 // self._region_step + 1):
                for y in range(y0 // self._region_step, y1 // self._region_step + 1):
                    for layer in COPPER_LAYERS:
                        self._regions[layer].setdefault((x, y), []).append(courtyard)
        self._widths, self._clearances = {}, {}
        self._minimum_widths, self._minimum_clearances = {}, {}
        self._last_clearance_geometry = None
        self.minimum_width = (
            max(
                self.width_floor,
                min(
                    width,
                    *(
                        r.minimum
                        for r in source.rules
                        if r.kind == "track_width" and r.minimum is not None
                    ),
                ),
            )
            if any(
                r.kind == "track_width" and r.minimum is not None for r in source.rules
            )
            else max(width, self.width_floor)
        )
        self.minimum_clearance = (
            max(
                self.clearance_floor,
                min(
                    clearance,
                    *(
                        r.minimum
                        for r in source.rules
                        if r.kind == "clearance" and r.minimum is not None
                    ),
                ),
            )
            if any(
                r.kind == "clearance" and r.minimum is not None for r in source.rules
            )
            else max(clearance, self.clearance_floor)
        )
        self.maximum_width = self._maximum_width_bound()
        self.maximum_clearance = max(
            clearance,
            self.clearance_floor,
            source.minimum_clearance,
            *(v[1] for v in source.classes.values()),
            *(r.minimum or 0 for r in source.rules if r.kind == "clearance"),
        )

    def references(self, segment: Segment) -> frozenset[str]:
        if not self._regions[segment.layer]:
            return frozenset()
        midpoint = Point(
            (segment.start.x + segment.end.x) // 2,
            (segment.start.y + segment.end.y) // 2,
        )
        cell = (
            segment.start.x // self._region_step,
            segment.start.y // self._region_step,
        )
        return frozenset(
            selector
            for c in self._regions[segment.layer].get(cell, ())
            if c.contains(segment.start)
            and c.contains(segment.end)
            and c.contains(midpoint)
            for selector in self._courtyard_selectors[c]
        )

    def width_at(self, segment: Segment) -> int:
        definite_refs = self.references(segment)
        possible_refs = (
            definite_refs
            | self.intersecting_references(segment, self.maximum_width, radius_adjustment_nm=2)
            | self.source.uncertain_courtyards.get(segment.layer, frozenset())
        )
        key = segment.layer, definite_refs, possible_refs
        if key not in self._widths:
            low = RuleItem("Track", segment.layer, self.net, self.net_class, definite_refs)
            high = RuleItem("Track", segment.layer, self.net, self.net_class, possible_refs)
            default = max(
                self.width,
                self.source.classes[self.net_class][0],
                self.width_floor,
                self.source.minimum_width,
            )
            possible, determined = [], False
            for rule in self.source.rules:
                if rule.kind != "track_width" or segment.layer not in rule.layers:
                    continue
                maybe, definite = self._match_bounds(
                    rule, low, high, low, high, unknown_group=False
                )
                if maybe:
                    # A matching explicit custom minimum overrides a net-class
                    # routing preset, whether local or whole-branch. Only an
                    # independently supplied electrical floor stays absolute.
                    # Uncertain stricter rules must still be respected.
                    candidate = max(
                        self.width_floor, rule.minimum or default,
                        0 if rule.net_specific or (possible_refs and rule.local) else default,
                    )
                    if rule.maximum is not None and candidate > rule.maximum:
                        raise ValueError(
                            f"{self.net}: width conflicts with saved rule {rule.name!r}"
                        )
                    possible.append(candidate)
                if definite:
                    determined = True
                    break
            if not determined:
                possible.append(default)
            self._widths[key] = max(possible)
        return self._widths[key]

    @staticmethod
    def _match_bounds(rule, first_low, first_high, second_low, second_high, *, unknown_group):
        if rule.nonmonotonic_local or (unknown_group and rule.uncertain_group):
            return True, False
        low = rule.matches(first_low, second_low) or rule.matches(second_low, first_low)
        high = rule.matches(first_high, second_high) or rule.matches(second_high, first_high)
        return (True, False) if low and not high else (high, low)

    def _maximum_width_bound(self) -> int:
        """Bound this net's width across all layers and possible courtyards."""
        default = max(self.width, self.width_floor, self.source.minimum_width,
                      self.source.classes[self.net_class][0])
        maximum = self.width_floor
        for layer in COPPER_LAYERS:
            low = RuleItem("Track", layer, self.net, self.net_class)
            high = RuleItem("Track", layer, self.net, self.net_class,
                            self.source.rule_courtyards)
            determined = False
            for rule in self.source.rules:
                if rule.kind != "track_width" or rule.minimum is None or layer not in rule.layers:
                    continue
                possible, definite = self._match_bounds(rule, low, high, low, high,
                                                  unknown_group=False)
                if possible:
                    maximum = max(maximum, self.width_floor, rule.minimum,
                                  0 if rule.net_specific else default)
                if definite:
                    determined = True
                    break
            if not determined:
                maximum = max(maximum, default)
        return maximum

    def minimum_width_for(self, layer: str) -> int:
        """Lower-bound any permitted width for this net and copper layer."""
        if layer not in self._minimum_widths:
            default = max(self.width, self.source.classes[self.net_class][0],
                          self.width_floor, self.source.minimum_width)
            low = RuleItem("Track", layer, self.net, self.net_class)
            high = RuleItem("Track", layer, self.net, self.net_class, self.source.rule_courtyards)
            possible, determined = [], False
            for rule in self.source.rules:
                if rule.kind != "track_width" or layer not in rule.layers:
                    continue
                maybe, definite = self._match_bounds(rule, low, high, low, high, unknown_group=False)
                if maybe:
                    possible.append(max(self.width_floor, rule.minimum or default,
                                        0 if rule.local or rule.net_specific else default))
                if definite:
                    determined = True
                    break
            if not determined:
                possible.append(default)
            self._minimum_widths[layer] = min(possible)
        return self._minimum_widths[layer]

    def minimum_clearance_for(self, layer: str, obstacle: Obstacle) -> int:
        """Lower-bound clearance over every possible courtyard assignment."""
        other_class = self.source.assignments.get(obstacle.net, "Default")
        key = layer, obstacle.net, obstacle.kind
        if key not in self._minimum_clearances:
            default = max(self.clearance, self.source.classes[self.net_class][1],
                          self.source.classes.get(other_class, (0, 0))[1],
                          self.clearance_floor, self.source.minimum_clearance)
            track_low = RuleItem("Track", layer, self.net, self.net_class)
            track_high = RuleItem("Track", layer, self.net, self.net_class, self.source.rule_courtyards)
            fields = ({"pad": "Pad", "via": "Via", "copper": "Track"}.get(obstacle.kind, "Keepout"),
                      layer, obstacle.net, other_class)
            other_low = RuleItem(*fields)
            other_high = RuleItem(*fields, self.source.rule_courtyards)
            possible, determined = [], False
            for rule in self.source.rules:
                if rule.kind != "clearance" or layer not in rule.layers:
                    continue
                maybe, definite = self._match_bounds(rule, track_low, track_high, other_low, other_high,
                                                      unknown_group=True)
                if maybe:
                    possible.append(max(self.clearance_floor, rule.minimum or default))
                if definite:
                    determined = True
                    break
            if not determined:
                possible.append(default)
            self._minimum_clearances[key] = min(possible)
        return self._minimum_clearances[key]

    def intersecting_references(
        self, segment: Segment, width: int, *, radius_adjustment_nm: int = 0
    ) -> frozenset[str]:
        """Resolve courtyard intersections for the entire emitted track."""
        if not self._regions[segment.layer]:
            return frozenset()
        radius = max(0, (width + 1) // 2 + radius_adjustment_nm)
        step = self._region_step
        nearby = {
            c
            for x in range((min(segment.start.x, segment.end.x) - radius) // step,
                           (max(segment.start.x, segment.end.x) + radius) // step + 1)
            for y in range((min(segment.start.y, segment.end.y) - radius) // step,
                           (max(segment.start.y, segment.end.y) + radius) // step + 1)
            for c in self._regions[segment.layer].get((x, y), ())
        }
        return frozenset(
            selector for c in nearby if c.intersects_track(segment, radius)
            for selector in self._courtyard_selectors[c]
        )

    def clearance_at(self, segment: Segment, obstacle: Obstacle, *, width: int | None = None) -> int:
        actual_width = self.width_at(segment) if width is None else width
        geometry_key = segment, actual_width
        cached = self._last_clearance_geometry
        if cached is None or cached[0] != geometry_key:
            definite_refs = self.intersecting_references(
                segment, actual_width, radius_adjustment_nm=-2
            )
            possible_geometry = self.intersecting_references(
                segment, actual_width, radius_adjustment_nm=2
            )
            self._last_clearance_geometry = geometry_key, definite_refs, possible_geometry
        else:
            _, definite_refs, possible_geometry = cached
        # Uncertain courtyards may change independently of track geometry.
        possible_refs = possible_geometry | self.source.uncertain_courtyards.get(
            segment.layer, frozenset()
        )
        other_class = self.source.assignments.get(obstacle.net, "Default")
        key = segment.layer, definite_refs, possible_refs, obstacle.net, obstacle.kind
        if key not in self._clearances:
            item_low = RuleItem("Track", segment.layer, self.net, self.net_class, definite_refs)
            item_high = RuleItem("Track", segment.layer, self.net, self.net_class, possible_refs)
            fields = (
                {"pad": "Pad", "via": "Via", "copper": "Track"}.get(
                    obstacle.kind, "Keepout"
                ),
                segment.layer,
                obstacle.net,
                other_class,
            )
            other_empty = RuleItem(*fields)
            other_all = RuleItem(*fields, self.source.rule_courtyards)
            default = max(
                self.clearance,
                self.source.classes[self.net_class][1],
                self.source.classes.get(other_class, (0, 0))[1],
                self.clearance_floor,
                self.source.minimum_clearance,
            )
            possible = []
            determined = False
            for rule in self.source.rules:
                if rule.kind != "clearance" or segment.layer not in rule.layers:
                    continue
                maybe, definite = self._match_bounds(
                    rule, item_low, item_high, other_empty, other_all, unknown_group=True
                )
                if maybe:
                    possible.append(max(self.clearance_floor, rule.minimum or default))
                if definite:
                    determined = True
                    break
            if not determined:
                possible.append(default)
            self._clearances[key] = max(possible)
        return self._clearances[key]

    def clearance_for_track(self, segment: Segment, obstacle: Obstacle, width: int) -> int:
        return self.clearance_at(segment, obstacle, width=width)

    def pieces(self, segment: Segment) -> tuple[tuple[Segment, int], ...]:
        """Return physical track pieces, merging equal widths where possible."""
        dx, dy = segment.end.x - segment.start.x, segment.end.y - segment.start.y
        length = max(abs(dx), abs(dy))
        if not length or (dx and dy and abs(dx) != abs(dy)):
            raise ValueError("Rule-aware tracks must be nonzero and exactly octilinear")
        cuts = {0, length}
        step = self._region_step
        nearby = {
            c
            for x in range(
                min(segment.start.x, segment.end.x) // step,
                max(segment.start.x, segment.end.x) // step + 1,
            )
            for y in range(
                min(segment.start.y, segment.end.y) // step,
                max(segment.start.y, segment.end.y) // step + 1,
            )
            for c in self._regions[segment.layer].get((x, y), ())
        }
        for courtyard in nearby:
            cuts.update(courtyard.cuts(segment))
        ordered = sorted(cuts)
        result = []
        for first, last in zip(ordered, ordered[1:]):
            a = Point(
                segment.start.x + first * dx // length,
                segment.start.y + first * dy // length,
            )
            b = Point(
                segment.start.x + last * dx // length,
                segment.start.y + last * dy // length,
            )
            piece = Segment(a, b, segment.layer)
            width = self.width_at(piece)
            if result and result[-1][1] == width:
                result[-1] = (Segment(result[-1][0].start, b, segment.layer), width)
            else:
                result.append((piece, width))
        return tuple(result)
