"""Supply local return-copper obstacles to disposable external searches."""

from contextlib import contextmanager
import math
from pathlib import Path

import pcbnew

from pcb.net_routing.reference import high_speed_nets, reference_corridors


def reserved_native_input(
    path: Path, *, ground_contact_margin_mm: float = 0.5
) -> tuple[dict, frozenset[str], dict]:
    """Write search-only constraints to an owned external-router input file."""
    from pcb.net_routing.native import load_board
    from pcb.route_validation import _zone_definition, _zones

    original = {
        uid: _zone_definition(zone)
        for uid, zone in _zones(path.read_text(encoding="utf-8-sig")).items()
    }
    board = load_board(path, refill_zones=True)
    with temporary_router_reservations(
        board, ground_contact_margin_mm=ground_contact_margin_mm
    ) as counts:
        temporary = frozenset(
            zone.m_Uuid.AsString()
            for zone in board.Zones()
            if zone.m_Uuid.AsString() not in original
        )
        if not pcbnew.SaveBoard(str(path), board):
            raise RuntimeError("Cannot write the temporary router reservations")
    return original, temporary, dict(counts)


def strip_native_reservations(
    path: Path, original: dict, temporary: frozenset[str]
) -> None:
    """Remove only owned search areas and require all original zones unchanged."""
    from core.kicad_sexpr import parse
    from pcb.route_validation import _zone_definition

    source = path.read_text(encoding="utf-8-sig")
    root = parse(source)
    remaining, spans = {}, []
    for zone in root.children("zone"):
        ids = zone.children("uuid")
        if len(ids) != 1 or len(ids[0].atoms()) != 2:
            raise RuntimeError("Unidentified router zone prevents reservation cleanup")
        uid = ids[0].atoms()[1].value
        if uid in temporary:
            spans.append((zone.start, zone.end))
        else:
            if uid in remaining:
                raise RuntimeError("Duplicate router zones prevent reservation cleanup")
            remaining[uid] = _zone_definition(zone)
    if remaining != original:
        raise RuntimeError("The router changed original zone definitions")
    for start, end in reversed(spans):
        source = source[:start] + source[end:]
    path.write_bytes(source.encode("utf-8"))


def _hull(points):
    points = sorted(set(points))
    if len(points) < 3:
        return points

    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    lower, upper = [], []
    for point in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    for point in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def _outline(points):
    shape = pcbnew.SHAPE_POLY_SET()
    shape.NewOutline()
    for x, y in points:
        shape.Append(int(x), int(y))
    return shape


def _corridor_polygon(obstacle):
    if obstacle.centerline is None:
        return _outline(
            (
                (obstacle.lower.x, obstacle.lower.y),
                (obstacle.upper.x, obstacle.lower.y),
                (obstacle.upper.x, obstacle.upper.y),
                (obstacle.lower.x, obstacle.upper.y),
            )
        )
    radius = obstacle.copper_radius
    shoulder = math.ceil(radius * (math.sqrt(2) - 1))
    offsets = (
        (radius, shoulder),
        (shoulder, radius),
        (-shoulder, radius),
        (-radius, shoulder),
        (-radius, -shoulder),
        (-shoulder, -radius),
        (shoulder, -radius),
        (radius, -shoulder),
    )
    return _outline(
        _hull(
            (point.x + dx, point.y + dy)
            for point in obstacle.centerline
            for dx, dy in offsets
        )
    )


@contextmanager
def temporary_router_reservations(board, *, ground_contact_margin_mm: float = 0.5):
    """Add search-only rule areas and always remove them before returning.

    No saved design object is changed. The later native connectivity, DRC,
    via-pad, routing-style and reference checks remain independently required.
    """
    if (
        isinstance(ground_contact_margin_mm, bool)
        or not isinstance(ground_contact_margin_mm, (int, float))
        or not math.isfinite(ground_contact_margin_mm)
        or not 0 <= ground_contact_margin_mm <= 2
    ):
        raise ValueError(
            "The extra ground-contact search margin must be between 0 and 2 mm"
        )
    margin = round(ground_contact_margin_mm * 1_000_000)
    added = []
    vertices = 0
    try:

        def add(shape, layer):
            nonlocal vertices
            shape.Fracture()
            for index in range(shape.OutlineCount()):
                chain = shape.COutline(index)
                points = [
                    (chain.CPoint(i).x, chain.CPoint(i).y)
                    for i in range(chain.PointCount())
                ]
                if len(points) < 3:
                    continue
                vertices += len(points)
                if len(added) >= 1024 or vertices > 100_000:
                    raise RuntimeError(
                        "Local routing reservations exceed their geometry budget"
                    )
                area = pcbnew.ZONE(board)
                area.SetLayer(layer)
                area.SetIsRuleArea(True)
                area.SetDoNotAllowTracks(True)
                area.SetDoNotAllowVias(True)
                area.SetDoNotAllowPads(False)
                area.SetDoNotAllowFootprints(False)
                area.SetDoNotAllowZoneFills(False)
                area.SetZoneName("AutoPCB search-only return reservation")
                area.Outline().Append(_outline(points))
                board.Add(area)
                added.append(area)

        filename = board.GetFileName()
        project = Path(filename).with_suffix(".kicad_pro") if filename else None
        if project is not None and project.is_file():
            names = {str(name) for name in board.GetNetsByName() if str(name)}
            critical = high_speed_nets(project.read_bytes(), names)
            corridors = reference_corridors(board, critical)
            if corridors.unsupported_items:
                raise RuntimeError(
                    "Unsupported high-speed geometry prevents local routing reservations"
                )
            for obstacle in corridors.obstacles:
                add(_corridor_polygon(obstacle), board.GetLayerID(obstacle.layers[0]))
        reference_count = len(added)

        # Preserve only local, actually filled ground copper near pads.
        # Other pads' clearance holes remain available; no full plane is blocked.
        ground_zones = [
            zone
            for zone in board.Zones()
            if not zone.GetIsRuleArea() and zone.GetNetname() == "GND"
        ]
        for layer in board.GetEnabledLayers().CuStack():
            zones = [zone for zone in ground_zones if zone.IsOnLayer(layer)]
            if not zones:
                continue
            mask = pcbnew.SHAPE_POLY_SET()
            for pad in board.GetPads():
                if pad.GetNetname() != "GND" or not pad.IsOnLayer(layer):
                    continue
                box = pad.GetBoundingBox()
                mask.Append(
                    _outline(
                        (
                            (box.GetX() - margin, box.GetY() - margin),
                            (box.GetRight() + margin, box.GetY() - margin),
                            (box.GetRight() + margin, box.GetBottom() + margin),
                            (box.GetX() - margin, box.GetBottom() + margin),
                        )
                    )
                )
            if not mask.OutlineCount():
                continue
            mask.Simplify()
            for zone in zones:
                clipped = pcbnew.SHAPE_POLY_SET(zone.GetFilledPolysList(layer))
                clipped.BooleanIntersection(mask)
                add(clipped, layer)
        yield {
            "reference_areas": reference_count,
            "ground_contact_areas": len(added) - reference_count,
        }
    finally:
        for area in reversed(added):
            board.Remove(area)
