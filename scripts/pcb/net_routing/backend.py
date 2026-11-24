"""Reuse the bounded path router with cached geometry and whole-net screens."""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass, replace
from itertools import islice, product

from pcb.route_batch import SHARED_FIELDS, _previous_obstacles
from pcb.route_cli import _bounds, _fields, _guide, _obstacle, _weights
from pcb.route_constraints import (
    ElectricalConstraints,
    ElectricalConstraintViolation,
    via_sizes_meet,
)
from pcb.route_optimizer import (
    COPPER_LAYERS,
    GridRouter,
    NoGridRoute,
    Obstacle,
    Point,
    Route,
    SearchLimitReached,
    Segment,
    Via,
    mm,
    to_mm,
)
from pcb.net_routing.geometry import (
    OffAngleContactGeometryError,
    RouteGeometryError,
    StaircaseGeometryError,
    _supported_line,
    aggregate,
    on_segment,
    unique_segments,
)
from pcb.net_routing.model import Budget, BudgetExhausted, Net, NetPlan, Site
from pcb.net_routing.native import NativeNet
from pcb.net_routing.rework import (
    RemovedCopper,
    octilinear_projections,
    restored_connects_islands,
)
from pcb.net_routing.smoothing import (
    chain_endpoints,
    simplify_route,
    simplify_terminal_branches,
)
from core.compat import strict_zip


class _ContextDiversionRequested(Exception):
    """Give a bounded detour a chance before rejected paths consume the slice."""

    def __init__(self, expanded: int):
        self.expanded = expanded


class _ContextAlternativesRequested(Exception):
    """Give alternative endpoint headings a turn before the full retry."""


def route_payload(
    route: Route,
    net: str,
    width: int,
    size: int,
    drill: int,
    profile=None,
    segment_dimensions=None,
) -> dict:
    dimensions = segment_dimensions or (profile.pieces if profile else None)
    return {
        "net": net,
        "length_mm": route.length_mm,
        "bends": route.bends,
        "via_count": len(route.vias),
        "cost": route.cost,
        "segments": [
            {
                "from_mm": s.start.as_mm(),
                "to_mm": s.end.as_mm(),
                "layer": s.layer,
                "width_mm": to_mm(segment_width),
            }
            for segment in route.segments
            for s, segment_width in (
                dimensions(segment) if dimensions else ((segment, width),)
            )
        ],
        "vias": [
            {"at_mm": v.at.as_mm(), "size_mm": to_mm(size), "drill_mm": to_mm(drill)}
            for v in route.vias
        ],
    }


def plan_dimensions(plan: NetPlan, settings):
    """Preserve the exact width of restored source copper after junction splits."""
    if not plan.restored_widths:
        return settings.profile.pieces if settings.profile else None

    def dimensions(segment):
        widths = {
            width
            for original, width in plan.restored_widths
            if original.layer == segment.layer
            and on_segment(segment.start, original)
            and on_segment(segment.end, original)
        }
        if len(widths) != 1:
            raise ValueError("Restored copper has ambiguous source widths")
        return ((segment, widths.pop()),)

    return dimensions


@dataclass
class Settings:
    raw: dict
    native: NativeNet
    copper: dict[str, int]
    board_bounds: tuple[Point, Point] | None = None
    edge_clearance: int = 0
    saved_rules: object = None

    def __post_init__(self):
        _fields(
            self.raw, SHARED_FIELDS | {"obstacles", "current_model"}, "net settings"
        )
        self.lower, self.upper = _bounds(self.raw.get("bounds_mm"))
        self.step = mm(self.raw.get("step_mm", 0.25))
        self.width = mm(self.raw.get("width_mm", 0.2))
        self.clearance = mm(self.raw.get("clearance_mm", 0.2))
        via = _fields(self.raw.get("via", {}), {"size_mm", "drill_mm"}, "via")
        self.size, self.drill = (
            mm(via.get("size_mm", 0.6)),
            mm(via.get("drill_mm", 0.3)),
        )
        layers = self.raw.get(
            "layers",
            [layer for layer in COPPER_LAYERS if layer in self.copper]
            or ["F.Cu", "B.Cu"],
        )
        if (
            not isinstance(layers, list)
            or not layers
            or any(x not in COPPER_LAYERS for x in layers)
        ):
            raise ValueError("layers must be a nonempty array of known copper layers")
        if len(set(layers)) != len(layers):
            raise ValueError("layers must be distinct")
        self.constraints = (
            ElectricalConstraints.from_config(self.raw["electrical"])
            if "electrical" in self.raw
            else None
        )
        self.excluded = {}
        if self.constraints:
            if self.raw.get("current_model") != "whole_net_upper_bound":
                raise ValueError(
                    "electrical requires current_model=whole_net_upper_bound; include all branch loads"
                )
            self.layers, self.excluded = self.constraints.eligible_layers(
                layers, self.width, self.clearance
            )
            self.constraints.check_configured_stackup(list(self.layers), self.copper)
        else:
            self.layers = tuple(layers)
            if "current_model" in self.raw:
                raise ValueError("current_model requires supplied electrical limits")
        self.weights = _weights(self.raw.get("weights", {}))
        self.guide = _guide(self.raw.get("symmetry_guide"))
        self.penalties = _fields(
            self.raw.get("layer_penalties", {}),
            set(COPPER_LAYERS),
            "layer penalties",
        )
        self.forbid_staircase = self.raw.get("forbid_staircase", True)
        self.minimum_segment = mm(self.raw.get("min_segment_length_mm", 0))
        self.extra = tuple(_obstacle(x) for x in self.raw.get("obstacles", []))
        self.profile = (
            self.saved_rules.profile(
                self.native.net.name,
                self.width,
                self.clearance,
                width_floor=self.width if self.constraints else 0,
                clearance_floor=self.clearance if self.constraints else 0,
            )
            if self.saved_rules
            else None
        )
        if self.board_bounds is not None and self.step > 0:
            # Local pad escapes may use less clearance than the net default.
            # Account for that smaller value when clipping the grid to the
            # board edge; exact nonrectangular edges remain a native DRC gate.
            minimum = (
                min(self.clearance, self.profile.minimum_clearance)
                if self.profile
                else self.clearance
            )
            first, last = self.board_bounds
            inset = max(0, self.edge_clearance - minimum)
            origin = self.lower
            self.lower = Point(
                origin.x
                + max(0, math.ceil((first.x + inset - origin.x) / self.step))
                * self.step,
                origin.y
                + max(0, math.ceil((first.y + inset - origin.y) / self.step))
                * self.step,
            )
            self.upper = Point(
                origin.x
                + math.floor((min(self.upper.x, last.x - inset) - origin.x) / self.step)
                * self.step,
                origin.y
                + math.floor((min(self.upper.y, last.y - inset) - origin.y) / self.step)
                * self.step,
            )
        for key in ("max_expansions", "max_cells"):
            if key in self.raw and (
                isinstance(self.raw[key], bool) or not isinstance(self.raw[key], int)
            ):
                raise ValueError(f"{key} must be an integer")

    def router(
        self,
        obstacles: tuple[Obstacle, ...],
        proposed_vias: frozenset[Point] = frozenset(),
        budget: Budget | None = None,
    ) -> GridRouter:
        c = self.constraints
        headings = {
            name: i
            for i, name in enumerate(("E", "SE", "S", "SW", "W", "NW", "N", "NE"))
        }

        def directions(field):
            names = self.raw.get(field)
            if names is None:
                return None
            if (
                not isinstance(names, list)
                or not names
                or any(n not in headings for n in names)
            ):
                raise ValueError(f"{field} must contain compass headings")
            return frozenset(headings[n] for n in names)

        return GridRouter(
            self.lower,
            self.upper,
            self.step,
            self.layers,
            self.native.net.name,
            self.width,
            self.clearance,
            self.size,
            self.drill,
            (*obstacles, *self.extra),
            self.weights,
            self.guide,
            self.raw.get("max_expansions", 30_000),
            self.raw.get("max_cells", 250_000),
            c is None or c.allows_via_geometry(self.size, self.drill),
            None
            if c is None
            else c.eligible_existing_vias(self.native.vias, self.native.net.name)
            | (
                set(proposed_vias)
                if c.allows_via_geometry(self.size, self.drill)
                else set()
            ),
            self.penalties,
            self.forbid_staircase,
            self.minimum_segment,
            self.raw.get("gentle_turns", True),
            directions("initial_directions"),
            directions("final_directions"),
            self.raw.get("enforce_endpoint_spacing", False),
            self.raw.get("topology_heuristic", False),
            self.raw.get("heuristic_weight", 1.0),
            None if budget is None else budget.check,
            self.profile,
            first_feasible=self.raw.get("first_feasible", False),
            pad_copper=self.native.net.pad_copper,
        )

    def assessment(self, route: Route) -> dict:
        c = self.constraints
        if c is None:
            return {"status": "not_assessed", "reason": "No electrical limits supplied"}
        if self.native.unsupported_copper:
            raise ValueError(
                "Whole-net electrical screening cannot yet assess existing arcs"
            )
        via_route = Route(
            route.segments,
            route.vias,
            tuple(Via(v.at, "F.Cu", "B.Cu") for v in self.native.vias),
            route.length_mm,
            route.bends,
            route.cost,
            route.expanded,
        )
        checks = c.check_vias(
            via_route, self.native.net.name, self.size, self.drill, self.native.vias
        )
        resistance = c.check_voltage_drop(via_route, self.width)[
            "estimated_resistance_ohm"
        ]
        for segment, width in self.native.tracks:
            c.eligible_layers([segment.layer], width, self.clearance)
            c.check_configured_stackup([segment.layer], self.copper)
            thickness = to_mm(c.tracks[segment.layer].copper_thickness)
            length = (
                math.hypot(
                    segment.end.x - segment.start.x, segment.end.y - segment.start.y
                )
                / 1e6
            )
            resistance += c.resistivity_ohm_mm * length / (to_mm(width) * thickness)
        drop = c.current_a * resistance
        if drop > c.maximum_voltage_drop_v:
            raise ElectricalConstraintViolation(
                "Whole-net conservative copper voltage-drop bound exceeds the supplied limit"
            )
        result = c.assessment(
            self.layers,
            self.excluded,
            checks,
            {
                "estimated_v": drop,
                "maximum_v": c.maximum_voltage_drop_v,
                "estimated_resistance_ohm": resistance,
                "basis": c.voltage_drop_basis,
            },
        )
        result.update(
            {
                "current_model": "whole_net_upper_bound",
                "configured_stackup": "checked_against_nominal_saved_board",
                "scope": "All saved and proposed tracks/vias use the declared worst-case total net current; "
                "this conservative sum excludes pad/plane/contact resistance and component ratings.",
            }
        )
        return result


class NativeBackend:
    """Bounded LRU caches; no native board reload per path search."""

    def __init__(
        self,
        settings: dict[str, Settings],
        obstacles: tuple[Obstacle, ...],
        budget: Budget,
    ):
        self.settings, self.obstacles, self.budget = settings, obstacles, budget
        self.cache: OrderedDict[tuple, tuple[Route, ...]] = OrderedDict()
        self.routers: OrderedDict[tuple, GridRouter] = OrderedDict()

    def combine(self, net: Net, routes: tuple[Route, ...]) -> Route:
        return self._combine(
            net, routes, forbid_staircase=self.settings[net.name].forbid_staircase
        )

    def _combine(
        self,
        net: Net,
        routes: tuple[Route, ...],
        *,
        forbid_staircase: bool,
        legacy_contacts: frozenset[Site] = frozenset(),
    ) -> Route:
        s = self.settings[net.name]
        return aggregate(
            routes,
            existing=net.existing,
            weights=s.weights,
            layer_penalties=s.penalties,
            guide=s.guide,
            pad_sites=net.pad_sites
            | legacy_contacts
            | frozenset(
                Site(v.at, layer) for v in s.native.vias for layer, _ in v.sizes
            ),
            pad_copper=net.pad_copper,
            forbid_staircase=forbid_staircase,
            minimum_segment=s.minimum_segment,
        )

    def local_repair(
        self, net: Net, prior: tuple[NetPlan, ...], route: Route
    ) -> Route | None:
        """Search a bounded window around an unbranched removed copper chain."""
        ends = chain_endpoints(route)
        if ends is None or not route.segments:
            return None
        setting = self.settings[net.name]
        points = [
            point
            for segment in route.segments
            for point in (segment.start, segment.end)
        ]
        points.extend(via.at for via in route.vias)
        margin = max(mm(1.5), 2 * (setting.width + setting.clearance), setting.size)
        lower = Point(
            max(setting.lower.x, min(p.x for p in points) - margin),
            max(setting.lower.y, min(p.y for p in points) - margin),
        )
        upper = Point(
            min(setting.upper.x, max(p.x for p in points) + margin),
            min(setting.upper.y, max(p.y for p in points) + margin),
        )
        if lower.x >= upper.x or lower.y >= upper.y:
            return None
        raw = {
            **setting.raw,
            "bounds_mm": [
                to_mm(lower.x),
                to_mm(lower.y),
                to_mm(upper.x),
                to_mm(upper.y),
            ],
        }
        obstacles = (
            *(
                self._clearance_envelope(obstacle, setting)
                for obstacle in self.obstacles
            ),
            *self.envelopes(prior, setting),
        )
        try:
            local = Settings(
                raw,
                setting.native,
                setting.copper,
                setting.board_bounds,
                setting.edge_clearance,
                setting.saved_rules,
            )
            router = local.router(obstacles, frozenset(), self.budget)
            empty = Route((), (), (), 0.0, 0, 0.0, 0)
            initial, final = router.initial_directions, router.final_directions
            alternatives = [(initial, final)]
            for start_directions, end_directions in alternatives:
                router.initial_directions, router.final_directions = (
                    start_directions,
                    end_directions,
                )
                candidate = None
                try:
                    self.budget.call()
                    candidate, _ = self._path(router, net, empty, *ends)
                    if restored_connects_islands(net, candidate):
                        return self.combine(net, (candidate,))
                except SearchLimitReached:
                    self.budget.hit_limit("path_expansions")
                    continue
                except NoGridRoute:
                    continue
                except RouteGeometryError:
                    pass
                if candidate is None:
                    continue
                # A valid grid path can form a staircase with older copper at
                # its endpoint. Try a different arrival or departure heading.
                for alternative in self.alternative_headings(
                    candidate, ends, initial, final
                ):
                    if alternative not in alternatives:
                        alternatives.append(alternative)
        except (BudgetExhausted, RouteGeometryError):
            pass
        return None

    @staticmethod
    def alternative_headings(route: Route, ends: tuple[Site, Site], initial, final):
        """Suggest different endpoint directions after a context rejection."""
        alternatives = []
        for index, site in enumerate(ends):
            for segment in route.segments:
                if segment.layer != site.layer or site.point not in (
                    segment.start,
                    segment.end,
                ):
                    continue
                other = segment.end if site.point == segment.start else segment.start
                heading = GridRouter._direction(site.point, other)
                if index == 1:
                    heading = (heading + 4) % 8
                allowed = set(
                    initial
                    if index == 0 and initial is not None
                    else final
                    if index == 1 and final is not None
                    else range(8)
                ) - {heading}
                if allowed:
                    alternatives.append(
                        (frozenset(allowed), final)
                        if index == 0
                        else (initial, frozenset(allowed))
                    )
                break
        return tuple(alternatives)

    @staticmethod
    def context_detour_points(
        routes: tuple[Route, ...],
    ) -> tuple[tuple[str, Point], ...]:
        """Sample a few interior points to diversify context-rejected paths."""
        result = []
        for route in routes:
            segments = sorted(
                route.segments,
                key=lambda item: max(
                    abs(item.end.x - item.start.x), abs(item.end.y - item.start.y)
                ),
                reverse=True,
            )
            for item in segments[:2]:
                dx, dy = item.end.x - item.start.x, item.end.y - item.start.y
                distance = max(abs(dx), abs(dy))
                if distance < 2 or (dx and dy and abs(dx) != abs(dy)):
                    continue
                half = distance // 2
                point = Point(
                    item.start.x + (half if dx > 0 else -half if dx < 0 else 0),
                    item.start.y + (half if dy > 0 else -half if dy < 0 else 0),
                )
                witness = item.layer, point
                if witness not in result:
                    result.append(witness)
                if len(result) == 3:
                    return tuple(result)
        return tuple(result)

    def restored_plan(
        self,
        net: Net,
        prior: tuple[NetPlan, ...],
        removed,
        *,
        source_unchanged: bool = False,
        _quiet: bool = False,
    ) -> NetPlan | None:
        """Offer unchanged removed copper as a conservative, checked warm start."""
        if removed is None or not (
            removed.tracks
            or removed.vias
            or removed.arc_lengths_mm
            or removed.off_angle_tracks
        ):
            return None

        def reject(reason):
            if not _quiet:
                self.budget.failure("restore_seed", f"{net.name}: {reason}")
            return None

        if removed.arc_lengths_mm:
            return reject("curved removed copper is not supported")
        if removed.off_angle_tracks:
            choices = [
                tuple(
                    (projected, width) for projected in octilinear_projections(segment)
                )
                for segment, width in removed.off_angle_tracks
            ]
            if any(not options for options in choices):
                return reject("removed copper has no nearby exact-angle projection")
            for combination in islice(product(*choices), 32):
                self.budget.check()
                proposed = RemovedCopper(
                    tracks=(*removed.tracks, *combination),
                    vias=removed.vias,
                )
                plan = self.restored_plan(
                    net, prior, proposed, source_unchanged=False, _quiet=True
                )
                if plan is not None:
                    return replace(plan, source_modified=True)
            return reject(
                "nearby exact-angle projections do not pass contact or clearance checks"
            )
        setting = self.settings[net.name]
        if any(
            not via.through
            or via.drill != setting.drill
            or not via_sizes_meet(
                via.sizes, setting.copper or setting.layers, exact_size=setting.size
            )
            for via in removed.vias
        ):
            return reject(
                "a removed via does not match the permitted through-via dimensions"
            )
        # The whole-net electrical screen uses the configured width. It must
        # never evaluate a restored segment narrower than that assumption.
        if setting.constraints and any(
            width < setting.width for _, width in removed.tracks
        ):
            return reject(
                "an original track is narrower than the configured electrical floor"
            )
        segments = tuple(segment for segment, _ in removed.tracks)
        vias = tuple(Via(via.point, "F.Cu", "B.Cu") for via in removed.vias)
        reused_vias = ()
        raw = Route(segments, vias, (), 0.0, 0, 0.0, 0)
        if not restored_connects_islands(net, raw):
            return reject("removed pieces do not connect every selected island")
        try:
            repaired = False
            try:
                measured = self.combine(net, (raw,))
            except RouteGeometryError as original_error:
                # A removed legacy path can violate the current style rules.
                # Try a fully checked local cleanup, but only at an unchanged,
                # unambiguous track width; mixed widths need a wider solver.
                candidate = None
                if all(width == setting.width for _, width in removed.tracks):
                    candidate = self.refine_complete(
                        net, prior, raw, repair_invalid=True
                    )
                    if (
                        candidate.segments == raw.segments
                        or not restored_connects_islands(net, candidate)
                    ):
                        candidate = self.local_repair(net, prior, raw)
                if candidate is not None and restored_connects_islands(net, candidate):
                    measured = self.combine(net, (candidate,))
                    segments = candidate.segments
                    vias = candidate.vias
                    reused_vias = candidate.reused_vias
                    repaired = True
                elif source_unchanged and isinstance(
                    original_error,
                    (StaircaseGeometryError, OffAngleContactGeometryError),
                ):
                    # This is only the identical removed copper, with its
                    # original widths and no footprint moves. The independent
                    # native style comparison still rejects new style defects.
                    legacy_contacts = set()
                    while True:
                        self.budget.check()
                        try:
                            measured = self._combine(
                                net,
                                (raw,),
                                forbid_staircase=False,
                                legacy_contacts=frozenset(legacy_contacts),
                            )
                            break
                        except OffAngleContactGeometryError as contact_error:
                            if (
                                contact_error.site in legacy_contacts
                                or len(legacy_contacts) >= 128
                            ):
                                raise
                            legacy_contacts.add(contact_error.site)
                    # The regular combiner exempts these original contacts
                    # from bend classification; recover the exact count from
                    # all incident physical rays, distinguishing branches.
                    legacy_bends = 0
                    for site in legacy_contacts:
                        rays = set()
                        for old in (*segments, *net.existing):
                            if old.layer != site.layer or not on_segment(
                                site.point, old
                            ):
                                continue
                            for endpoint in (old.start, old.end):
                                dx, dy = (
                                    endpoint.x - site.point.x,
                                    endpoint.y - site.point.y,
                                )
                                if dx or dy:
                                    divisor = math.gcd(abs(dx), abs(dy))
                                    rays.add((dx // divisor, dy // divisor))
                        if len(rays) == 2:
                            first, second = tuple(rays)
                            legacy_bends += first != (-second[0], -second[1])
                    measured = replace(
                        measured,
                        bends=measured.bends + legacy_bends,
                        cost=measured.cost + setting.weights.bend * legacy_bends,
                    )
                else:
                    raise
            length = sum(
                math.hypot(s.end.x - s.start.x, s.end.y - s.start.y) / 1e6
                for s in segments
            )
            # Do not silently duplicate or subtract source copper.
            if abs(measured.length_mm - length) > 1e-9:
                return reject("removed copper overlaps retained copper")
            if not self.accept(net, measured):
                return reject("the original copper failed electrical screening")
            obstacles = (
                *(
                    (self._clearance_envelope(obstacle, setting), True)
                    for obstacle in self.obstacles
                    if not source_unchanged or repaired
                ),
                *((obstacle, False) for obstacle in self.envelopes(prior, setting)),
            )
            if repaired:
                pieces = tuple(
                    item
                    for segment in segments
                    for item in (
                        setting.profile.pieces(segment)
                        if setting.profile
                        else ((segment, setting.width),)
                    )
                )
            else:
                pieces = tuple(removed.tracks)

            def nearby(a, b, obstacle, margin):
                return not (
                    max(a.x, b.x) + margin + 1 < obstacle.lower.x
                    or min(a.x, b.x) - margin - 1 > obstacle.upper.x
                    or max(a.y, b.y) + margin + 1 < obstacle.lower.y
                    or min(a.y, b.y) - margin - 1 > obstacle.upper.y
                )

            for obstacle, original_obstacle in obstacles:
                clearance = setting.clearance
                same_net = (
                    bool(obstacle.net)
                    and obstacle.net == net.name
                    and obstacle.kind != "keepout"
                )
                for piece, width in pieces:
                    if same_net or piece.layer not in obstacle.layers:
                        continue
                    retained = tuple(
                        segment
                        for segment, old_width in removed.tracks
                        if old_width == width
                    )
                    checked = (
                        unique_segments((piece,), retained)
                        if (original_obstacle and source_unchanged and repaired)
                        else (piece,)
                    )
                    for part in checked:
                        maximum = max(
                            obstacle.min_track_clearance,
                            setting.profile.maximum_clearance
                            if setting.profile
                            else clearance,
                        )
                        if not nearby(
                            part.start,
                            part.end,
                            obstacle,
                            math.ceil(width / 2) + maximum,
                        ):
                            continue
                        gap = obstacle.gap(
                            setting.profile.clearance_at(piece, obstacle, width=width)
                            if setting.profile
                            else clearance,
                        )
                        if GridRouter._intersects_box(
                            part.start, part.end, obstacle, math.ceil(width / 2) + gap
                        ):
                            return reject(
                                f"original track clearance conflicts with {obstacle.net or 'keepout'} "
                                f"{obstacle.kind} on {', '.join(obstacle.layers)}"
                            )
                for via in vias:
                    if same_net and obstacle.kind not in {"pad", "via"}:
                        continue
                    if (
                        original_obstacle
                        and source_unchanged
                        and repaired
                        and any(old.point == via.at for old in removed.vias)
                    ):
                        continue
                    via_clearance = obstacle.gap(clearance, via=True)
                    if not nearby(
                        via.at,
                        via.at,
                        obstacle,
                        math.ceil(setting.size / 2) + via_clearance,
                    ):
                        continue
                    if GridRouter._intersects_box(
                        via.at,
                        via.at,
                        obstacle,
                        math.ceil(setting.size / 2) + via_clearance,
                    ):
                        return reject(
                            f"original via clearance conflicts with {obstacle.net or 'keepout'} "
                            f"{obstacle.kind} on {', '.join(obstacle.layers)}"
                        )
            restored = Route(
                segments,
                vias,
                reused_vias,
                measured.length_mm,
                measured.bends,
                measured.cost,
                0,
            )
            return NetPlan(
                net.name,
                restored,
                0.0,
                () if repaired else removed.tracks,
                source_modified=repaired,
            )
        except RouteGeometryError as error:
            return reject(f"original geometry is not eligible: {error}")

    def endpoint_headings(
        self, net: Net, tree: Route, site: Site, *, arriving: bool
    ) -> frozenset[int] | None:
        """Constrain a true end-to-end bend while permitting pads and branches."""
        if site in net.pad_sites:
            return None
        setting = self.settings[net.name]
        if any(
            v.at == site.point and site.layer in dict(v.sizes)
            for v in setting.native.vias
        ):
            return None
        if any(v.at == site.point for v in (*tree.vias, *tree.reused_vias)):
            return None
        directions = set()
        for legacy, segments in ((True, net.existing), (False, tree.segments)):
            for segment in segments:
                if segment.layer != site.layer or not on_segment(site.point, segment):
                    continue
                if legacy and not _supported_line(segment):
                    raise NoGridRoute(
                        "Endpoint touches off-angle existing copper; "
                        "try another access point or reroute that copper"
                    )
                directions.update(
                    GridRouter._direction(site.point, p)
                    for p in (segment.start, segment.end)
                    if p != site.point
                )
        if len(directions) != 1:
            return None
        existing = next(iter(directions))
        return frozenset(
            heading
            for heading in range(8)
            if ((heading + (4 if arriving else 0)) - existing) % 8 not in {2, 6}
        )

    def _path(
        self,
        router: GridRouter,
        net: Net,
        tree: Route,
        start: Site,
        end: Site,
        *,
        context_check=None,
    ) -> tuple[Route, bool]:
        """Apply endpoint headings for this call without contaminating cached routers."""
        original = router.initial_directions, router.final_directions
        dynamic = (
            self.endpoint_headings(net, tree, start, arriving=False),
            self.endpoint_headings(net, tree, end, arriving=True),
        )
        allowed = tuple(
            fixed if extra is None else extra if fixed is None else fixed & extra
            for fixed, extra in strict_zip(original, dynamic)
        )
        if any(value is not None and not value for value in allowed):
            raise NoGridRoute(
                "Configured endpoint directions conflict with an existing copper end"
            )
        try:
            router.initial_directions, router.final_directions = allowed
            if context_check is not None:
                route = router.route(
                    start.point,
                    start.layer,
                    end.point,
                    end.layer,
                    candidate_check=context_check,
                )
            else:
                route = router.route(start.point, start.layer, end.point, end.layer)
            limited = False
            if (
                self.budget.limits.smoothing_trials
                and "seconds" not in self.budget.limits_hit
            ):
                try:
                    initial_route = route
                    candidate = simplify_route(
                        route,
                        router,
                        net,
                        start,
                        end,
                        self.budget,
                        max_trials=self.budget.limits.smoothing_trials,
                    )
                    # Refinement must not spoil a previously valid join.
                    route = (
                        initial_route
                        if context_check is not None
                        and not context_check(candidate)
                        and context_check(initial_route)
                        else candidate
                    )
                except BudgetExhausted:
                    limited = True
            return route, limited
        finally:
            router.initial_directions, router.final_directions = original

    def _path_with_context(
        self, router: GridRouter, net: Net, tree: Route, start: Site, end: Site
    ) -> tuple[Route, bool]:
        """Retry headings when an otherwise valid path conflicts with its tree."""
        initial, final = router.initial_directions, router.final_directions
        alternatives = [(initial, final)]
        expanded, limited = 0, False
        rejected_context = False
        rejected_routes = []
        detour_routes = []
        conflict_witnesses = []
        last_no_route = None
        detours_tried = False
        accepted_context = False

        def compatible(candidate):
            nonlocal accepted_context, rejected_context
            try:
                self.combine(net, (tree, candidate))
            except RouteGeometryError as error:
                rejected_context = True
                self.budget.failure("combined_geometry", f"{net.name}: {error}")
                if len(rejected_routes) < 3 and candidate not in rejected_routes:
                    rejected_routes.append(candidate)
                if len(detour_routes) < 3 and all(
                    (candidate.segments, candidate.vias, candidate.reused_vias)
                    != (old.segments, old.vias, old.reused_vias)
                    for old in detour_routes
                ):
                    detour_routes.append(candidate)
                if isinstance(error, StaircaseGeometryError):
                    for layer, point in error.witnesses:
                        witness = (layer, point)
                        if (
                            len(conflict_witnesses) < 3
                            and witness not in conflict_witnesses
                            and point not in (start.point, end.point)
                            and any(
                                part.layer == layer and on_segment(point, part)
                                for part in candidate.segments
                            )
                        ):
                            conflict_witnesses.append(witness)
                if (
                    len(detour_routes) >= 3
                    and not detours_tried
                    and not accepted_context
                ):
                    raise _ContextDiversionRequested(candidate.expanded)
                return False
            accepted_context = True
            return True

        def retry_rejected_headings():
            # A context-sensitive search can exhaust or hit its limit after
            # rejecting every path it materialized. Try a few different
            # endpoint headings; the same geometry checks still apply.
            while rejected_routes and len(alternatives) < 7:
                candidate = rejected_routes.pop(0)
                for alternative in self.alternative_headings(
                    candidate, (start, end), initial, final
                ):
                    if alternative not in alternatives and len(alternatives) < 7:
                        alternatives.append(alternative)

        def try_detours():
            nonlocal detours_tried, expanded, limited
            detours_tried = True
            original_clearance = router.edge_clear
            had_instance_clearance = "edge_clear" in vars(router)
            try:
                points = tuple(
                    dict.fromkeys(
                        (
                            *conflict_witnesses,
                            *self.context_detour_points(tuple(detour_routes)),
                        )
                    )
                )[:3]
                for layer, point in points:
                    self.budget.call()

                    def clear_without_point(
                        first, last, check_layer, *, blocked_layer=layer, blocked=point
                    ):
                        if check_layer == blocked_layer and on_segment(
                            blocked, Segment(first, last, check_layer)
                        ):
                            return False
                        return original_clearance(first, last, check_layer)

                    router.edge_clear = clear_without_point
                    try:
                        candidate, path_limited = self._path(
                            router,
                            net,
                            tree,
                            start,
                            end,
                            context_check=compatible,
                        )
                    except NoGridRoute as error:
                        expanded += error.expanded
                        continue
                    except SearchLimitReached as error:
                        expanded += error.expanded
                        limited = True
                        continue
                    expanded += candidate.expanded
                    limited |= path_limited or candidate.search_truncated
                    if compatible(candidate):
                        # The temporary exclusion deliberately leaves other
                        # paths unsearched; it is a feasible warm result.
                        return replace(candidate, expanded=expanded), True
            finally:
                if had_instance_clearance:
                    router.edge_clear = original_clearance
                elif "edge_clear" in vars(router):
                    del router.edge_clear
            return None

        try:
            for index, (start_directions, end_directions) in enumerate(alternatives):
                if index:
                    self.budget.call()
                router.initial_directions, router.final_directions = (
                    start_directions,
                    end_directions,
                )
                try:
                    while True:
                        try:
                            route, path_limited = self._path(
                                router, net, tree, start, end, context_check=compatible
                            )
                            break
                        except _ContextDiversionRequested as signal:
                            expanded += signal.expanded
                            limited = True
                            detour = try_detours()
                            if detour is not None:
                                return detour
                            retry_rejected_headings()
                            if len(alternatives) > index + 1:
                                # Keep the unrestricted attempt as a final
                                # fallback, but do not let repeated context
                                # failures consume the whole slice before
                                # testing already-known alternative headings.
                                current = (start_directions, end_directions)
                                if current not in alternatives[index + 1 :]:
                                    alternatives.append(current)
                                raise _ContextAlternativesRequested()
                            # Preserve the original search as a fallback if the
                            # bounded diversions do not find a valid path.
                            self.budget.call()
                except _ContextAlternativesRequested:
                    continue
                except NoGridRoute as error:
                    expanded += error.expanded
                    last_no_route = str(error)
                    retry_rejected_headings()
                    continue
                except SearchLimitReached as error:
                    expanded += error.expanded
                    limited = True
                    retry_rejected_headings()
                    continue
                expanded += route.expanded
                limited |= path_limited or route.search_truncated
                if not compatible(route):
                    retry_rejected_headings()
                    continue
                return replace(route, expanded=expanded), limited
        finally:
            router.initial_directions, router.final_directions = initial, final

        # A cheapest path may merge with a different path before the endpoint.
        # A bounded retry around an interior point gives that other history a
        # chance without relaxing clearance, layer, bend, or context checks.
        if not detours_tried:
            detour = try_detours()
            if detour is not None:
                return detour
        if limited:
            raise SearchLimitReached(
                "Context-aware path search reached a limit", expanded=expanded
            )
        if not rejected_context and last_no_route:
            raise NoGridRoute(last_no_route, expanded=expanded)
        raise NoGridRoute(
            "No sampled endpoint heading joins the existing copper", expanded=expanded
        )

    def accept(self, net: Net, route: Route) -> bool:
        try:
            self.settings[net.name].assessment(route)
            return True
        except ElectricalConstraintViolation as error:
            self.budget.failure("electrical_screen", f"{net.name}: {error}")
            return False

    def refine_complete(
        self,
        net: Net,
        prior: tuple[NetPlan, ...],
        route: Route,
        *,
        repair_invalid: bool = False,
    ) -> Route:
        """Improve a complete warm route while preserving its terminals."""
        trials = self.budget.limits.smoothing_trials
        if not route.segments:
            return route
        ends = chain_endpoints(route) if trials else None
        if not trials or "seconds" in self.budget.limits_hit:
            return route
        settings = self.settings[net.name]
        obstacles = (
            *tuple(self._clearance_envelope(o, settings) for o in self.obstacles),
            *self.envelopes(prior, settings),
        )
        try:
            router = settings.router(
                obstacles, frozenset(v.at for v in route.vias), self.budget
            )
            if (
                router.initial_directions is not None
                or router.final_directions is not None
            ):
                return route
            if ends is None:
                if repair_invalid:
                    return route
                return simplify_terminal_branches(
                    route, router, net, self.budget, max_trials=trials
                )
            return simplify_route(
                route,
                router,
                net,
                *ends,
                self.budget,
                max_trials=trials,
                repair_invalid=repair_invalid,
            )
        except BudgetExhausted:
            return route

    def _clearance_envelope(self, obstacle: Obstacle, current: Settings) -> Obstacle:
        """Respect both nets' supplied clearances, independent of routing order."""
        other = self.settings.get(obstacle.net)
        extra = (
            0
            if other is None or other is current
            else max(0, other.clearance - current.clearance)
        )
        if not extra:
            return obstacle
        return Obstacle(
            Point(obstacle.lower.x - extra, obstacle.lower.y - extra),
            Point(obstacle.upper.x + extra, obstacle.upper.y + extra),
            obstacle.layers,
            obstacle.net,
            obstacle.kind,
            centerline=obstacle.centerline,
            copper_radius=obstacle.copper_radius + extra if obstacle.centerline else 0,
            corner_radius=obstacle.corner_radius + extra
            if obstacle.corner_radius
            else 0,
            min_track_clearance=obstacle.min_track_clearance,
            min_via_clearance=obstacle.min_via_clearance,
            boundary_only=obstacle.boundary_only,
        )

    def envelopes(
        self, plans: tuple[NetPlan, ...], current: Settings
    ) -> tuple[Obstacle, ...]:
        result = []
        for plan in plans:
            s = self.settings[plan.net]
            result.extend(
                self._clearance_envelope(_obstacle(o), current)
                for o in _previous_obstacles(
                    route_payload(
                        plan.route,
                        plan.net,
                        s.width,
                        s.size,
                        s.drill,
                        s.profile,
                        plan_dimensions(plan, s),
                    )
                )
            )
        return tuple(result)

    @staticmethod
    def _plane_via_clear(
        net: Net, point: Point, setting: Settings, obstacles: tuple[Obstacle, ...]
    ) -> bool:
        margin = math.ceil(setting.size / 2) + setting.clearance
        if not (
            setting.lower.x + margin <= point.x <= setting.upper.x - margin
            and setting.lower.y + margin <= point.y <= setting.upper.y - margin
        ):
            return False
        for obstacle in obstacles:
            same_net = obstacle.net == net.name and obstacle.kind != "keepout"
            if same_net and obstacle.kind not in {"pad", "via"}:
                continue
            margin = math.ceil(setting.size / 2) + obstacle.gap(
                setting.clearance, via=True
            )
            # Avoid the more expensive exact check when even the expanded
            # bounding box cannot contain the candidate via.
            if (
                point.x < obstacle.lower.x - margin - 1
                or point.x > obstacle.upper.x + margin + 1
                or point.y < obstacle.lower.y - margin - 1
                or point.y > obstacle.upper.y + margin + 1
            ):
                continue
            if GridRouter._intersects_box(point, point, obstacle, margin):
                return False
        return True

    def clear_plane_sites(
        self, net: Net, prior: tuple[NetPlan, ...], tree: Route
    ) -> frozenset[Site]:
        """Rank sampled plane sites without discarding difficult alternatives."""
        sites = {site for island in net.islands for site, _ in island.zone_anchors}
        if not sites:
            return frozenset()
        setting = self.settings[net.name]
        if setting.constraints and not setting.constraints.allows_via_geometry(
            setting.size, setting.drill
        ):
            return frozenset()
        obstacles = (
            *tuple(self._clearance_envelope(o, setting) for o in self.obstacles),
            *self.envelopes((*prior, NetPlan(net.name, tree)), setting),
            *setting.extra,
        )
        clear = set()
        for site in sorted(sites, key=lambda item: item.key):
            self.budget.check()
            if self._plane_via_clear(net, site.point, setting, obstacles):
                clear.add(site)
        return frozenset(clear)

    def endpoint_in_bounds(self, net: Net, site: Site) -> bool:
        """Match the grid's own endpoint envelope when ranking access points."""
        setting = self.settings[net.name]
        margin = math.ceil(setting.width / 2) + setting.clearance
        point = site.point
        return (
            setting.lower.x + margin <= point.x <= setting.upper.x - margin
            and setting.lower.y + margin <= point.y <= setting.upper.y - margin
        )

    def direct_plane_path(
        self,
        net: Net,
        tree: Route,
        start: Site,
        end: Site,
        router: GridRouter,
        obstacles: tuple[Obstacle, ...],
    ) -> Route | None:
        """Try a short through-via at a sampled, existing-plane access site."""
        virtual = {site for island in net.islands for site, _ in island.zone_anchors}
        if (start in virtual) == (end in virtual) or start.point == end.point:
            return None
        plane, terminal = (start, end) if start in virtual else (end, start)
        if plane.layer == terminal.layer or not router.allow_new_vias:
            return None
        setting = self.settings[net.name]
        if not self._plane_via_clear(
            net, plane.point, setting, (*obstacles, *setting.extra)
        ):
            return None
        first_allowed = self.endpoint_headings(net, tree, start, arriving=False)
        last_allowed = self.endpoint_headings(net, tree, end, arriving=True)
        candidates = []
        for points in router._stub_paths(start.point, end.point):
            segments = tuple(
                Segment(a, b, terminal.layer) for a, b in zip(points, points[1:])
            )
            if not segments or any(
                not router.edge_clear(s.start, s.end, s.layer) for s in segments
            ):
                continue
            first = GridRouter._direction(segments[0].start, segments[0].end)
            last = GridRouter._direction(segments[-1].start, segments[-1].end)
            if (
                first_allowed is not None
                and first not in first_allowed
                or last_allowed is not None
                and last not in last_allowed
                or router.initial_directions is not None
                and first not in router.initial_directions
                or router.final_directions is not None
                and last not in router.final_directions
            ):
                continue
            raw = Route(
                segments, (Via(plane.point, "F.Cu", "B.Cu"),), (), 0.0, 0, 0.0, 0
            )
            try:
                candidate = self.combine(net, (raw,))
                self.combine(net, (tree, candidate))
            except RouteGeometryError:
                continue
            candidates.append(candidate)
        return min(candidates, key=lambda route: route.cost, default=None)

    def paths(
        self, net: Net, prior: tuple[NetPlan, ...], tree: Route, start: Site, end: Site
    ) -> tuple[Route, ...]:
        self.budget.check()
        settings = self.settings[net.name]
        if start.layer not in settings.layers or end.layer not in settings.layers:
            return ()
        context = (
            net.name,
            tuple(sorted(p.signature for p in prior)),
            NetPlan(net.name, tree).signature,
        )
        key = context, start.key, end.key
        if key in self.cache:
            self.budget.cache_hits += 1
            self.cache.move_to_end(key)
            return self.cache[key]
        extra = self.envelopes((*prior, NetPlan(net.name, tree)), settings)
        obstacles = (
            *tuple(self._clearance_envelope(o, settings) for o in self.obstacles),
            *extra,
        )
        if context not in self.routers:
            self.routers[context] = settings.router(
                obstacles, frozenset(v.at for v in tree.vias), self.budget
            )
            if len(self.routers) > 2:
                self.routers.popitem(last=False)
        self.routers.move_to_end(context)
        base = self.routers[context]
        results: dict[tuple, Route] = {}
        direct = self.direct_plane_path(net, tree, start, end, base, obstacles)
        if direct is not None:
            self.budget.call()
            results[NetPlan(net.name, direct).signature] = direct
        exclusions: list[Obstacle | None] = [None]
        limited = False
        index = 0
        while (
            index < len(exclusions)
            and len(results) < self.budget.limits.path_candidates
        ):
            exclusion = exclusions[index]
            index += 1
            try:
                self.budget.call()
                router = (
                    base
                    if exclusion is None
                    else settings.router(
                        (*obstacles, exclusion),
                        frozenset(v.at for v in tree.vias),
                        self.budget,
                    )
                )
                route, refinement_limited = self._path_with_context(
                    router, net, tree, start, end
                )
                limited |= refinement_limited
                self.budget.expanded += route.expanded
                limited |= route.search_truncated
                if route.search_truncated:
                    self.budget.hit_limit("off_grid_search")
                results[NetPlan(net.name, route).signature] = route
                if exclusion is None:
                    for segment in sorted(
                        route.segments,
                        key=lambda s: (
                            -math.hypot(s.end.x - s.start.x, s.end.y - s.start.y)
                        ),
                    ):
                        middle = Point(
                            (segment.start.x + segment.end.x) // 2,
                            (segment.start.y + segment.end.y) // 2,
                        )
                        radius = max(settings.step // 2, settings.width)
                        if (
                            min(
                                math.hypot(middle.x - p.point.x, middle.y - p.point.y)
                                for p in (start, end)
                            )
                            <= radius * 3
                        ):
                            continue
                        exclusions.append(
                            Obstacle(
                                Point(middle.x - radius, middle.y - radius),
                                Point(middle.x + radius, middle.y + radius),
                                (segment.layer,),
                                "",
                                "keepout",
                            )
                        )
                        if len(exclusions) >= self.budget.limits.path_candidates * 2:
                            break
            except NoGridRoute as error:
                self.budget.expanded += error.expanded
                self.budget.failure("no_path_in_grid", f"{net.name}: {error}")
            except SearchLimitReached as error:
                self.budget.expanded += error.expanded
                limited = True
                self.budget.hit_limit("path_expansions")
                self.budget.failure("path_search_unknown", f"{net.name}: {error}")
            except BudgetExhausted:
                limited = True
                break
        answer = tuple(sorted(results.values(), key=lambda r: r.cost))
        if not limited:
            self.cache[key] = answer
            while len(self.cache) > self.budget.limits.cache_entries:
                self.cache.popitem(last=False)
        return answer
