"""route_native.py: read conservative copper envelopes from a stable saved-board snapshot.

Nothing here writes to or opens an editor for the original PCB. Envelopes can
overestimate obstacles, while board outlines, custom rules and actual zone
refill still require native KiCad validation of any proposed route.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import hashlib
import math
import tempfile
import uuid
from dataclasses import dataclass
from itertools import chain
from pathlib import Path

from core.kicad_sexpr import parse, select_direct_children
from core.pcb_io import copper_width
from pcb.route_constraints import ExistingVia
from pcb.route_optimizer import COPPER_LAYERS, Obstacle, Point, mm
from core.compat import strict_zip


@dataclass(frozen=True)
class NativeSnapshot:
    """The obstacle model read from one saved board for one target net.

    Inputs:
        source_sha256: SHA-256 digest of the saved board bytes.
        obstacles: Pad, track, via, zone and keepout envelopes in nanometres.
        ignored_zones: Copper zones left out because they are assumed refilled.
        vias: Existing vias of the target net.
        copper_thicknesses: Nominal (layer, thickness in nanometres) pairs from the stackup.
        reference_status: How far reference-plane corridors are modeled:
            project_rules_missing, local_reservations_only or
            unmodeled_inner_high_speed.
        reference_corridor_count: Reference corridor obstacles added to obstacles.
        reference_unmodeled_ids: Items whose reference plane could not be modeled.
        enabled_layers: Copper layer names enabled on the saved board.
    """

    source_sha256: str
    obstacles: tuple[Obstacle, ...]
    ignored_zones: int
    vias: tuple[ExistingVia, ...] = ()
    copper_thicknesses: tuple[tuple[str, int], ...] = ()
    reference_status: str = "project_rules_missing"
    reference_corridor_count: int = 0
    reference_unmodeled_ids: tuple[str, ...] = ()
    enabled_layers: tuple[str, ...] = ()


class NativeObstacleCache:
    """Reuse one immutable obstacle model only while its exact inputs match."""

    def __init__(self) -> None:
        """Start with no cached model."""
        self._entry: tuple[tuple, NativeSnapshot] | None = None

    @staticmethod
    def _fingerprint(source: Path) -> tuple[str, str | None]:
        """Digest the saved board and its project rules.

        Outputs:
            The SHA-256 hex digests of the board file and of the .kicad_pro file beside
            it (None when there is none). Raises ValueError unless source is an
            existing .kicad_pcb file.
        """
        if source.suffix.lower() != ".kicad_pcb" or not source.is_file():
            raise ValueError("Expected an existing saved KiCad PCB snapshot")
        rules = source.with_suffix(".kicad_pro")
        board_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        rules_hash = (
            hashlib.sha256(rules.read_bytes()).hexdigest() if rules.is_file() else None
        )
        return board_hash, rules_hash

    def load(
        self,
        path: Path,
        net: str,
        *,
        assume_refill: bool = False,
        removals: list[dict[str, str]] | None = None,
    ) -> NativeSnapshot:
        """Read the obstacle model of a saved board, reusing the last one while its inputs match.

        Inputs:
            path: Saved .kicad_pcb file.
            net: Target net name.
            assume_refill: Leave out copper zones, as if they were refilled.
            removals: Existing copper (kind and UUID) to leave out. These reads are
                never cached.
        Outputs:
            The snapshot. Raises ValueError for an empty net, a non-boolean option or a
            missing board file, and RuntimeError when the board or its rules change
            during the read.
        """
        if not isinstance(net, str) or not net or not isinstance(assume_refill, bool):
            raise ValueError("A target net and boolean refill option are required")
        source = Path(path).resolve()
        # Removals are infrequent and require native UUID validation each time.
        if removals:
            return read_native_obstacles(
                source, net, assume_refill=assume_refill, removals=removals
            )
        fingerprint = self._fingerprint(source)
        key = (source, net, assume_refill, fingerprint)
        if self._entry is not None and self._entry[0] == key:
            result = self._entry[1]
        else:
            result = read_native_obstacles(source, net, assume_refill=assume_refill)
        if (
            self._fingerprint(source) != fingerprint
            or result.source_sha256 != fingerprint[0]
        ):
            self._entry = None
            raise RuntimeError(
                "PCB or project rules changed during obstacle inspection"
            )
        self._entry = (key, result)
        return result


class ConfiguredCopperThicknesses:
    """Reads the nominal copper thickness of each layer from a board's stackup."""

    def __call__(self, original: bytes) -> tuple[tuple[str, int], ...]:
        """Read nominal stackup from the captured bytes, not a live editor."""
        setups = tuple(
            parse(form)
            for form in select_direct_children(original.decode("utf-8-sig"), "setup")
        )
        if len(setups) != 1:
            raise ValueError("PCB must have exactly one setup section")
        stackups = setups[0].children("stackup")
        if not stackups:
            return ()
        if len(stackups) != 1:
            raise ValueError("PCB contains ambiguous stackup sections")
        values = {}
        for layer in stackups[0].children("layer"):
            atoms = layer.atoms()
            if len(atoms) < 2 or atoms[1].value not in COPPER_LAYERS:
                continue
            name = atoms[1].value
            thickness = layer.children("thickness")
            if name in values or len(thickness) != 1 or len(thickness[0].atoms()) != 2:
                raise ValueError(f"PCB has ambiguous copper thickness for {name}")
            value = mm(thickness[0].atoms()[1].value)
            if value <= 0:
                raise ValueError(f"PCB copper thickness for {name} must be positive")
            values[name] = value
        return tuple(values.items())


_configured_copper_thicknesses = ConfiguredCopperThicknesses()


class BoxObstacle:
    """Builds an obstacle from the bounding box of a native board item."""

    def __call__(self, item, layers: tuple[str, ...], net: str, kind: str) -> Obstacle:
        """Make an obstacle that covers an item's bounding box.

        Inputs:
            item: Native KiCad board item.
            layers: Copper layers the obstacle occupies.
            net: Net name of the item.
            kind: Obstacle kind; a keepout uses its boundary only.
        Outputs:
            The obstacle in nanometres.
        """
        bounds = item.GetBoundingBox()
        return Obstacle(
            Point(int(bounds.GetX()), int(bounds.GetY())),
            Point(int(bounds.GetRight()), int(bounds.GetBottom())),
            layers,
            net,
            kind,
            boundary_only=kind == "keepout",
        )


_box = BoxObstacle()


class ViaObstacle:
    """Builds the obstacle around an existing via."""

    def __call__(self, board, via, layers: tuple[str, ...]) -> Obstacle:
        """Conservatively account for drill rules around an existing via."""
        bounds = via.GetBoundingBox()
        position = via.GetPosition()
        widths = [copper_width(via, layer=board.GetLayerID(layer)) for layer in layers]
        drill = max(0, int(via.GetDrillValue()))
        # KiCad's inclusive BOX2I upper edge is commonly one nanometre beyond
        # the physical radius. Centering on that box would hide an existing via
        # from exact-grid reuse; keep a symmetric conservative envelope instead.
        radius = max(
            (max([drill, *widths]) + 1) // 2,
            int(position.x - bounds.GetX()),
            int(bounds.GetRight() - position.x),
            int(position.y - bounds.GetY()),
            int(bounds.GetBottom() - position.y),
        )
        annular = max(0, (min(widths) - drill) // 2) if widths else 0
        settings = board.GetDesignSettings()
        return Obstacle(
            Point(int(position.x) - radius, int(position.y) - radius),
            Point(int(position.x) + radius, int(position.y) + radius),
            layers,
            via.GetNetname(),
            "via",
            min_track_clearance=max(0, int(settings.m_HoleClearance) - annular),
            min_via_clearance=max(0, int(settings.m_HoleToHoleMin) - annular),
        )


_via_obstacle = ViaObstacle()


class PadObstacle:
    """Builds the obstacle around a pad, as tight as its copper layers and drill allow."""

    def __call__(self, board, pad, layers: tuple[str, ...]) -> Obstacle:
        """Use a tighter envelope only when every copper layer and drill fit it."""
        import pcbnew

        bounds = pad.GetBoundingBox()
        at = pad.GetPosition()
        drill = pad.GetDrillSize()
        drill_radius = (max(int(drill.x), int(drill.y)) + 1) // 2
        settings = board.GetDesignSettings()
        track_clearance = 0
        via_clearance = 0
        if drill.x or drill.y:
            track_clearance = max(0, int(settings.m_HoleClearance))
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                track_clearance = max(
                    track_clearance, int(settings.m_CopperEdgeClearance)
                )
            via_clearance = max(track_clearance, int(settings.m_HoleToHoleMin))
        active = [
            board.GetLayerID(layer)
            for layer in layers
            if pad.IsOnLayer(board.GetLayerID(layer))
        ]
        if (
            active
            and pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH
            and drill.x > 0
            and drill.x == drill.y
        ):
            inscribed = []
            for layer in active:
                size, offset = pad.GetSize(layer), pad.GetOffset(layer)
                shape = pad.GetShape(layer)
                if (
                    shape
                    not in {
                        pcbnew.PAD_SHAPE_CIRCLE,
                        pcbnew.PAD_SHAPE_RECT,
                        pcbnew.PAD_SHAPE_OVAL,
                        pcbnew.PAD_SHAPE_ROUNDRECT,
                    }
                    or min(size.x, size.y) <= 0
                    or offset.x
                    or offset.y
                    or (
                        shape == pcbnew.PAD_SHAPE_ROUNDRECT
                        and not 0
                        <= pad.GetRoundRectCornerRadius(layer)
                        <= min(size.x, size.y) / 2
                    )
                ):
                    break
                inscribed.append(min(int(size.x), int(size.y)) // 2)
            else:
                # Each supported centered shape contains this circle. The
                # obstacle already encloses that copper, so subtract its guaranteed
                # annulus when converting drill-edge clearance to copper-edge
                # clearance. This is conservative even for a bounding-box envelope.
                # Offset, chamfered/custom and slotted pads retain the old envelope;
                # NPTH/locator holes and all native rules are unchanged.
                annular = max(0, min(inscribed) - (int(drill.x) + 1) // 2)
                track_clearance = max(0, int(settings.m_HoleClearance) - annular)
                via_clearance = max(0, int(settings.m_HoleToHoleMin) - annular)
        clearance = {
            "min_track_clearance": track_clearance,
            "min_via_clearance": via_clearance,
        }
        fallback = Obstacle(
            Point(
                min(int(bounds.GetX()), int(at.x) - drill_radius),
                min(int(bounds.GetY()), int(at.y) - drill_radius),
            ),
            Point(
                max(int(bounds.GetRight()), int(at.x) + drill_radius),
                max(int(bounds.GetBottom()), int(at.y) + drill_radius),
            ),
            layers,
            pad.GetNetname(),
            "pad",
            **clearance,
        )
        if not active or drill.x != drill.y:
            return fallback
        # Axis-aligned ovals and rounded rectangles are Minkowski sums of an
        # inset rectangle and a circle. The smallest layer radius and the union
        # bounding box conservatively cover all active copper layers.
        shapes = [pad.GetShape(layer) for layer in active]
        if (
            math.isfinite(pad.GetOrientationDegrees())
            and pad.GetOrientationDegrees() % 90 == 0
            and all(
                shape in {pcbnew.PAD_SHAPE_OVAL, pcbnew.PAD_SHAPE_ROUNDRECT}
                for shape in shapes
            )
        ):
            radii = []
            for layer, shape in strict_zip(active, shapes):
                size, offset = pad.GetSize(layer), pad.GetOffset(layer)
                if (
                    size.x <= 0
                    or size.y <= 0
                    or offset.x
                    or offset.y
                    or max(int(drill.x), int(drill.y)) > min(int(size.x), int(size.y))
                ):
                    return fallback
                radius = (
                    min(int(size.x), int(size.y)) // 2
                    if shape == pcbnew.PAD_SHAPE_OVAL
                    else int(pad.GetRoundRectCornerRadius(layer))
                )
                radii.append(radius)
            # Shrinking the corner radius slightly makes the representation a
            # superset despite KiCad's integer-coordinate rounding.
            corner = max(0, min(radii) - 2)
            if corner * 2 <= min(
                fallback.upper.x - fallback.lower.x, fallback.upper.y - fallback.lower.y
            ):
                return Obstacle(
                    fallback.lower,
                    fallback.upper,
                    layers,
                    pad.GetNetname(),
                    "pad",
                    corner_radius=corner,
                    **clearance,
                )
            return fallback
        diameters = []
        for layer in active:
            size, offset = pad.GetSize(layer), pad.GetOffset(layer)
            if (
                pad.GetShape(layer) != pcbnew.PAD_SHAPE_CIRCLE
                or size.x != size.y
                or size.x <= 0
                or offset.x
                or offset.y
            ):
                return fallback
            diameters.append(int(size.x))
        radius = (max(*diameters, int(drill.x), int(drill.y)) + 1) // 2 + 2
        center = Point(int(at.x), int(at.y))
        if (
            fallback.lower.x < center.x - radius
            or fallback.lower.y < center.y - radius
            or fallback.upper.x > center.x + radius
            or fallback.upper.y > center.y + radius
        ):
            return fallback
        return Obstacle(
            Point(center.x - radius, center.y - radius),
            Point(center.x + radius, center.y + radius),
            layers,
            pad.GetNetname(),
            "pad",
            centerline=(center, center),
            copper_radius=radius,
            **clearance,
        )


_pad_obstacle = PadObstacle()


class TrackObstacles:
    """Builds the obstacles that cover a straight native track."""

    def __call__(self, item, layers: tuple[str, ...], net: str) -> list[Obstacle]:
        """Cover a native straight trace with bounded, inflated capsules."""
        start, end = item.GetStart(), item.GetEnd()
        dx, dy = int(end.x - start.x), int(end.y - start.y)
        # Exact scanline rasterization makes 2 mm capsules economical while
        # keeping the spatial index local around long diagonal copper.
        pieces = max(1, math.ceil(max(abs(dx), abs(dy)) / 2_000_000))
        # Integer interpolation can shift each piece endpoint by up to sqrt(2) nm.
        radius = math.ceil(copper_width(item) / 2) + 2
        result = []
        for index in range(pieces):
            x1 = int(start.x) + dx * index // pieces
            y1 = int(start.y) + dy * index // pieces
            x2 = int(start.x) + dx * (index + 1) // pieces
            y2 = int(start.y) + dy * (index + 1) // pieces
            result.append(
                Obstacle(
                    Point(min(x1, x2) - radius, min(y1, y2) - radius),
                    Point(max(x1, x2) + radius, max(y1, y2) + radius),
                    layers,
                    net,
                    "copper",
                    centerline=(Point(x1, y1), Point(x2, y2)),
                    copper_radius=radius,
                )
            )
        return result


_track_boxes = TrackObstacles()


class RemovalValidation:
    """Checks that requested copper removals name only removable items of one net."""

    def __call__(self, board, removals: list[dict[str, str]], net: str) -> set[str]:
        """Select only exact, ungrouped and unlocked native copper for one net."""
        import pcbnew

        tracks = {item.m_Uuid.AsString(): item for item in board.GetTracks()}
        selected: set[str] = set()
        for removal in removals:
            if not isinstance(removal, dict) or set(removal) != {"kind", "uuid"}:
                raise ValueError("Each removal requires only kind and UUID")
            kind, identifier = removal["kind"], removal["uuid"]
            if kind not in {"segment", "via", "arc"} or not isinstance(identifier, str):
                raise ValueError(
                    "Removal kind must be segment, via, or arc with a UUID"
                )
            try:
                if str(uuid.UUID(identifier)) != identifier.lower():
                    raise ValueError("UUID must use standard hyphenated form")
            except ValueError as exc:
                raise ValueError(f"Invalid copper UUID: {identifier}") from exc
            if identifier in selected or identifier not in tracks:
                raise ValueError(f"Copper UUID is missing or repeated: {identifier}")
            item = tracks[identifier]
            actual = (
                "via"
                if isinstance(item, pcbnew.PCB_VIA)
                else "arc"
                if isinstance(item, pcbnew.PCB_ARC)
                else "segment"
            )
            if actual != kind or item.GetNetname() != net:
                raise ValueError(
                    f"Copper removal kind or net does not match: {identifier}"
                )
            if item.IsLocked() or item.GetParentGroup() is not None:
                raise ValueError(
                    f"Refusing to remove locked or grouped copper: {identifier}"
                )
            selected.add(identifier)
        return selected


validate_removals = RemovalValidation()


class NativeObstacleReader:
    """Reads the obstacle model of a saved board into a snapshot."""

    def __call__(
        self,
        path: Path,
        net: str,
        *,
        assume_refill: bool = False,
        removals: list[dict[str, str]] | None = None,
    ) -> NativeSnapshot:
        """Capture saved bytes, inspect a temporary copy, and reject a changing source."""
        import pcbnew

        source = path.resolve()
        if source.suffix.lower() != ".kicad_pcb" or not source.is_file():
            raise ValueError("Expected an existing saved KiCad PCB snapshot")
        if not net:
            raise ValueError("A target net is required")
        original = source.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        rules_path = source.with_suffix(".kicad_pro")
        rules_bytes = rules_path.read_bytes() if rules_path.is_file() else None
        copper_thicknesses = _configured_copper_thicknesses(original)
        with tempfile.TemporaryDirectory(prefix="jessie-route-read-") as directory:
            snapshot = Path(directory) / source.name
            snapshot.write_bytes(original)
            board = pcbnew.LoadBoard(str(snapshot))
            if board is None or board.FindNet(net) is None:
                raise ValueError(f"Net is not present on the saved PCB: {net}")
            removed_ids = validate_removals(board, removals or [], net)
            if board.GetCopperLayerCount() > len(COPPER_LAYERS):
                raise ValueError(
                    "The saved board exceeds KiCad's supported copper-layer count"
                )
            enabled_layers = tuple(
                board.GetLayerName(layer)
                for layer in board.GetEnabledLayers().CuStack()
            )
            layer_ids = {layer: board.GetLayerID(layer) for layer in enabled_layers}
            if not enabled_layers:
                raise ValueError(
                    "The saved board has no supported enabled copper layers"
                )
            obstacles: list[Obstacle] = []
            vias: list[ExistingVia] = []
            for pad in board.GetPads():
                layers = tuple(
                    layer
                    for layer, identifier in layer_ids.items()
                    if pad.IsOnLayer(identifier)
                )
                drill = pad.GetDrillSize()
                if drill.x or drill.y:
                    # All plated and non-plated holes constrain through-via placement
                    # and routing clearance even when no copper pad is on that layer.
                    layers = enabled_layers
                if layers:
                    obstacles.append(_pad_obstacle(board, pad, layers))
            for track in board.GetTracks():
                if track.m_Uuid.AsString() in removed_ids:
                    continue
                if isinstance(track, pcbnew.PCB_VIA):
                    layers = tuple(
                        layer
                        for layer, identifier in layer_ids.items()
                        if track.IsOnLayer(identifier)
                    )
                    kind = "via"
                    if track.GetNetname() == net:
                        position = track.GetPosition()
                        vias.append(
                            ExistingVia(
                                Point(int(position.x), int(position.y)),
                                net,
                                tuple(
                                    (layer, copper_width(track, layer=layer_ids[layer]))
                                    for layer in layers
                                ),
                                int(track.GetDrillValue()),
                                track.GetViaType() == pcbnew.VIATYPE_THROUGH,
                            )
                        )
                else:
                    name = board.GetLayerName(track.GetLayer())
                    layers = (name,) if name in layer_ids else ()
                    kind = "copper"
                if layers:
                    if kind == "copper" and not isinstance(track, pcbnew.PCB_ARC):
                        obstacles.extend(
                            _track_boxes(track, layers, track.GetNetname())
                        )
                    elif kind == "via":
                        obstacles.append(_via_obstacle(board, track, layers))
                    else:
                        obstacles.append(_box(track, layers, track.GetNetname(), kind))
            ignored_zones = 0
            for zone in native_zones(board):
                layers = tuple(
                    layer
                    for layer, identifier in layer_ids.items()
                    if zone.IsOnLayer(identifier)
                )
                if not layers:
                    continue
                if zone.GetIsRuleArea():
                    # A refill never removes a rule-area/keepout constraint.
                    obstacles.append(_box(zone, layers, "", "keepout"))
                elif assume_refill:
                    ignored_zones += 1
                else:
                    # The outline box is conservative: this may rule out feasible
                    # paths, but never pretends a foreign zone is empty.
                    obstacles.append(_box(zone, layers, zone.GetNetname(), "copper"))
            reference_status = "project_rules_missing"
            reference_count = 0
            reference_unmodeled = ()
            if rules_bytes is not None:
                from pcb.net_routing.reference import (
                    high_speed_nets,
                    reference_corridors,
                )

                board_nets = {
                    item.GetNetname()
                    for item in (*board.GetPads(), *board.GetTracks())
                    if item.GetNetname()
                }
                critical = high_speed_nets(rules_bytes, board_nets)
                corridors = reference_corridors(board, critical)
                obstacles.extend(corridors.obstacles)
                reference_count = len(corridors.obstacles)
                reference_unmodeled = corridors.unsupported_items
                reference_status = (
                    "unmodeled_inner_high_speed"
                    if reference_unmodeled
                    else "local_reservations_only"
                )
        if (
            not source.is_file()
            or hashlib.sha256(source.read_bytes()).hexdigest() != digest
        ):
            raise RuntimeError(
                "Saved PCB changed during inspection; discard this proposal"
            )
        if (rules_path.read_bytes() if rules_path.is_file() else None) != rules_bytes:
            raise RuntimeError("Project net-class rules changed during inspection")
        return NativeSnapshot(
            digest,
            tuple(obstacles),
            ignored_zones,
            tuple(vias),
            copper_thicknesses,
            reference_status,
            reference_count,
            reference_unmodeled,
            enabled_layers,
        )


read_native_obstacles = NativeObstacleReader()


class NativeZones:
    """Lists the copper zones and rule areas of a board and its footprints."""

    def __call__(self, board):
        """Include rule areas owned by footprints as well as by the board."""
        seen = set()
        for zone in chain(
            board.Zones(), *(part.Zones() for part in board.GetFootprints())
        ):
            uid = zone.m_Uuid.AsString()
            if uid not in seen:
                seen.add(uid)
                yield zone


native_zones = NativeZones()
