"""Stable native inputs, physical copper islands and guarded placement trials."""

from __future__ import annotations

from core.project import native_path

import hashlib
import itertools
import math
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from core.kicad_campaign import CampaignPlanner
from core.paths import config_path, data_path, support_input_name
from core.pcb_io import copper_width
from pcb.footprint_pose import native_pose_change
from pcb.route_constraints import ExistingVia
from pcb.route_native import read_native_obstacles
from pcb.route_optimizer import Point, Segment, mm
from pcb.route_validation import (
    RouteContract,
    _check_bottom_footprint_moves,
    _copy_inputs,
    _inputs,
    _tool_inputs,
)
from pcb.net_routing.geometry import branch_sites
from pcb.net_routing.model import Budget, Island, Net, Site
from core.compat import strict_zip


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Snapshot:
    """Copy once; detect changed sources, dependency inventory and planner code."""

    def __init__(self, source: Path):
        self.source = source.resolve()
        self._temporary = None

    def files(self) -> tuple[Path, ...]:
        files = set(_inputs(self.source))
        schematic = self.source.with_suffix(".kicad_sch")
        if schematic.is_file():
            files.add(schematic)
        project = self.source.parent
        support = (config_path(project, "autopcb.json"), data_path(project, "POWER_LOAD_BUDGET.csv"),
                   data_path(project, "STACKUP_CURRENT.json"))
        files.update(path for path in support if path.is_file())
        return tuple(sorted(files))

    def input_name(self, path: Path) -> Path:
        return support_input_name(self.source.parent, path)

    @staticmethod
    def tools() -> dict[str, str]:
        result = _tool_inputs()
        scripts = Path(__file__).resolve().parents[2]
        for path in sorted(Path(__file__).parent.glob("*.py")):
            result[path.relative_to(scripts).as_posix()] = sha(path)
        return result

    def __enter__(self) -> Snapshot:
        if not self.source.is_file() or self.source.suffix.lower() != ".kicad_pcb":
            raise ValueError("board_snapshot must name a saved KiCad PCB")
        self.fingerprints = {p: sha(p) for p in self.files()}
        self.tool_hashes = self.tools()
        self._temporary = tempfile.TemporaryDirectory(prefix="autopcb-net-plan-")
        self.directory = Path(self._temporary.name)
        self.board_path = self.directory / self.source.name
        try:
            names = {path: self.input_name(path) for path in self.fingerprints}
            if len(set(names.values())) != len(names):
                raise ValueError("Native snapshot has two sources for the same file")
            _copy_inputs(list(self.fingerprints), self.source, self.directory, names=names)
            for path, digest in self.fingerprints.items():
                copied = self.directory / names[path]
                if sha(copied) != digest:
                    raise RuntimeError(
                        "A source changed while copying the routing snapshot"
                    )
            self.original = self.board_path.read_bytes()
            self.check()
        except BaseException:
            self._temporary.cleanup()
            raise
        return self

    def check(self) -> None:
        if {
            p: sha(p) for p in self.files()
        } != self.fingerprints or self.tools() != self.tool_hashes:
            raise RuntimeError(
                "Routing sources or tools changed; discard this result and retry"
            )

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                self.check()
        finally:
            self._temporary.cleanup()


@dataclass(frozen=True)
class NativeNet:
    net: Net
    tracks: tuple[tuple[Segment, int], ...]
    vias: tuple[ExistingVia, ...]
    unsupported_copper: bool


def load_board(path: Path, *, refill_zones: bool = False):
    import pcbnew

    board = pcbnew.LoadBoard(str(path))
    if board is None:
        raise ValueError("KiCad could not load the disposable PCB")
    if refill_zones and any(not zone.GetIsRuleArea() for zone in board.Zones()):
        # Recompute rather than trusting the saved fill cache. This board is
        # in memory only; the independent closing check refills again.
        board.GetConnectivity().Build(board)
        if not pcbnew.ZONE_FILLER(board).Fill(board.Zones()):
            raise RuntimeError("KiCad could not refill temporary copper zones")
    elif not refill_zones:
        # Removing populated zones can invalidate SWIG wrappers in KiCad 10.
        # Clear only the in-memory fill; retain zone object ownership.
        for zone in list(board.Zones()):
            if not zone.GetIsRuleArea():
                zone.UnFill()
    board.GetConnectivity().Build(board)
    return board


def _zone_access_sites(board, name: str, layers: tuple[str, ...], islands, pad_sites,
                       budget, via_margin: int, *, contacts=(), sample: bool = True):
    """Sample plane sites bound to existing copper in the same filled polygon."""
    import pcbnew

    pads_at = {}
    for pad in board.GetPads():
        if pad.GetNetname() == name:
            position = pad.GetPosition()
            pads_at.setdefault((int(position.x), int(position.y)), []).append(pad)
    additions = [dict(island.zone_anchors) for island in islands]
    offsets = tuple((mm(dx), mm(dy)) for dx, dy in (
        (.6, 0), (-.6, 0), (0, .6), (0, -.6),
        (1.2, 0), (-1.2, 0), (0, 1.2), (0, -1.2),
        (.6, .6), (.6, -.6), (-.6, .6), (-.6, -.6),
    ))
    for zone in board.Zones():
        if zone.GetIsRuleArea() or zone.GetNetname() != name:
            continue
        for layer in layers:
            layer_id = board.GetLayerID(layer)
            if not zone.IsOnLayer(layer_id):
                continue
            polygon = zone.GetFilledPolysList(layer_id)
            for outline in range(polygon.OutlineCount()):
                if budget is not None:
                    budget.check()
                owners = []
                for index, island in enumerate(islands):
                    virtual = {site for site, _ in island.zone_anchors}
                    physical = tuple(
                        site for site in island.sites
                        if site not in virtual and site.layer == layer and polygon.Contains(
                            pcbnew.VECTOR2I(site.point.x, site.point.y), outline
                        )
                    )
                    if physical:
                        owners.append((index, physical))
                if len(owners) != 1:
                    continue
                owner, physical = owners[0]
                for contact in contacts:
                    if contact.layer != layer or not polygon.Contains(
                        pcbnew.VECTOR2I(contact.point.x, contact.point.y), outline
                    ):
                        continue
                    if contact in islands[owner].sites or contact in additions[owner]:
                        continue
                    additions[owner][contact] = min(physical, key=lambda site: (
                        (site.point.x - contact.point.x) ** 2
                        + (site.point.y - contact.point.y) ** 2,
                        site.key,
                    ))
                if not sample:
                    continue
                for index, island in enumerate(islands):
                    if index == owner:
                        continue
                    centers = sorted(island.sites, key=lambda site: (
                        site not in pad_sites, site.key,
                    ))[:2]
                    added = 0
                    for center in centers:
                        for dx, dy in offsets:
                            point = Point(center.point.x + dx, center.point.y + dy)
                            position = pcbnew.VECTOR2I(point.x, point.y)
                            if via_margin and any(
                                pad.HitTest(position, via_margin)
                                for pad in pads_at.get((center.point.x, center.point.y), ())
                            ):
                                continue
                            if not polygon.Contains(position, outline):
                                continue
                            virtual = Site(point, layer)
                            if virtual in islands[owner].sites or virtual in additions[owner]:
                                continue
                            anchor = min(physical, key=lambda site: (
                                (site.point.x - point.x) ** 2 + (site.point.y - point.y) ** 2,
                                site.key,
                            ))
                            additions[owner][virtual] = anchor
                            added += 1
                            if added >= 12:
                                break
                        if added >= 12:
                            break
    return tuple(
        replace(island,
                sites=tuple(sorted(set(island.sites) | set(additions[index]), key=lambda site: site.key)),
                zone_anchors=tuple(sorted(additions[index].items(), key=lambda pair: pair[0].key)))
        for index, island in enumerate(islands)
    )


def discover(board, name: str, layers: tuple[str, ...], budget: Budget,
             *, include_zone_access: bool = False, zone_via_margin: int = 0) -> NativeNet:
    import pcbnew

    if not name or name.startswith("unconnected-") or board.FindNet(name) is None:
        raise ValueError(f"Unknown or intentionally unconnected net: {name}")
    if isinstance(zone_via_margin, bool) or not isinstance(zone_via_margin, int) or zone_via_margin < 0:
        raise ValueError("zone_via_margin must be a nonnegative integer")
    layer_ids = {board.GetLayerName(layer): layer for layer in board.GetEnabledLayers().CuStack()}
    if not set(layers) <= layer_ids.keys():
        raise ValueError("Requested routing layers are not enabled on the saved board")
    pads = {p.m_Uuid.AsString(): p for p in board.GetPads() if p.GetNetname() == name}
    if not pads:
        raise ValueError(f"Net {name} has no pads")

    def point(item) -> Point:
        p = item.GetPosition()
        return Point(int(p.x), int(p.y))

    pad_sites = frozenset(
        Site(point(p), layer)
        for p in pads.values()
        for layer in layers
        if p.IsOnLayer(layer_ids[layer])
    )
    connectivity = board.GetConnectivity()
    copper = dict(pads)
    copper.update(
        (item.m_Uuid.AsString(), item)
        for item in board.GetTracks()
        if item.GetNetname() == name
    )
    pending = set(copper)
    islands = []
    while pending:
        budget.check()
        uid = min(pending)
        linked = {
            p.m_Uuid.AsString(): p
            for p in connectivity.GetConnectedItems(copper[uid])
            if p.GetNetname() == name
        }
        linked[uid] = copper[uid]
        members = tuple(sorted(set(linked) & pads.keys()))
        pending.difference_update(set(linked) & copper.keys())
        sites: set[Site] = set()
        segments = []
        for item in linked.values():
            active = [layer for layer in layers if item.IsOnLayer(layer_ids[layer])]
            if isinstance(item, (pcbnew.PAD, pcbnew.PCB_VIA)):
                sites.update(Site(point(item), layer) for layer in active)
            elif isinstance(item, pcbnew.PCB_TRACK):
                a, b = item.GetStart(), item.GetEnd()
                segment = Segment(
                    Point(int(a.x), int(a.y)),
                    Point(int(b.x), int(b.y)),
                    board.GetLayerName(item.GetLayer()),
                )
                sites.update(
                    Site(p, layer)
                    for layer in active
                    for p in (segment.start, segment.end)
                )
                if not isinstance(item, pcbnew.PCB_ARC) and active:
                    dx, dy = b.x - a.x, b.y - a.y
                    if (dx or dy) and (not dx or not dy or abs(dx) == abs(dy)):
                        segments.append(segment)
        if segments:
            sites.update(
                branch_sites(
                    tuple(segments), tuple(pad_sites), budget.limits.sites_per_island
                )
            )
        ordered = sorted(sites, key=lambda s: (s not in pad_sites, s.key))
        budget.pruned += max(0, len(ordered) - budget.limits.sites_per_island)
        if not ordered:
            raise ValueError(
                f"No access to an existing {name} island on the permitted layers"
            )
        islands.append(
            Island(uid, tuple(ordered[: budget.limits.sites_per_island]), members)
        )
    tracks, vias, unsupported = [], [], False
    for item in board.GetTracks():
        if item.GetNetname() != name:
            continue
        if isinstance(item, pcbnew.PCB_VIA):
            sizes = tuple(
                (layer, copper_width(item, layer=identifier))
                for layer, identifier in layer_ids.items()
                if item.IsOnLayer(identifier)
            )
            vias.append(
                ExistingVia(
                    point(item),
                    name,
                    sizes,
                    int(item.GetDrillValue()),
                    item.GetViaType() == pcbnew.VIATYPE_THROUGH,
                )
            )
        elif isinstance(item, pcbnew.PCB_ARC):
            unsupported = True
        else:
            a, b = item.GetStart(), item.GetEnd()
            tracks.append(
                (
                    Segment(
                        Point(int(a.x), int(a.y)),
                        Point(int(b.x), int(b.y)),
                        board.GetLayerName(item.GetLayer()),
                    ),
                    copper_width(item),
                )
            )
    if include_zone_access:
        islands = list(_zone_access_sites(
            board, name, layers, islands, pad_sites, budget, zone_via_margin
        ))
    net = Net(name, tuple(islands), tuple(s for s, _ in tracks), pad_sites)
    return NativeNet(net, tuple(tracks), tuple(vias), unsupported)


def contracts(board, names: tuple[str, ...]) -> list[RouteContract]:
    """Bind every pad UUID via its original copper anchor, even for moved pads."""
    result = []
    for name in names:
        pads = sorted(
            (p for p in board.GetPads() if p.GetNetname() == name),
            key=lambda p: p.m_Uuid.AsString(),
        )
        if not pads:
            raise ValueError(f"Net {name} has no pads")
        sites = []
        for pad in pads:
            layer = next(
                layer
                for layer in (board.GetLayerName(identifier) for identifier in board.GetEnabledLayers().CuStack())
                if pad.IsOnLayer(board.GetLayerID(layer))
            )
            at = pad.GetPosition()
            sites.append(Site(Point(int(at.x), int(at.y)), layer))
        first = sites[0]
        if len(sites) == 1:
            # A single pad can still have detached tracks or vias on its net.
            # The self-anchor binds the net; full-net validation checks every
            # remaining copper island rather than treating this as completion.
            result.append(RouteContract(name, first.point, first.layer, first.point, first.layer))
            continue
        result.extend(
            RouteContract(name, first.point, first.layer, s.point, s.layer)
            for s in sites[1:]
        )
    return result


def placements(snapshot: Snapshot, raw: object, names: set[str], budget: Budget):
    """Yield finite explicit bottom-side poses; never invent permission to move."""
    if not isinstance(raw, list):
        raise ValueError(
            "movable must be an array of explicit bottom-side pose choices"
        )
    if not raw:
        yield {}, ()
        return
    board = load_board(snapshot.board_path)
    refs, choices = [], []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {
            "reference",
            "positions_mm",
            "rotations_deg",
        }:
            raise ValueError("Unknown bottom placement field")
        reference = item.get("reference")
        if not isinstance(reference, str) or not reference or reference in refs:
            raise ValueError("Movable references must be nonempty and distinct")
        _check_bottom_footprint_moves(
            board, snapshot.original.decode("utf-8-sig"), {reference}
        )
        footprint = board.FindFootprintByReference(reference)
        affected = {
            p.GetNetname()
            for p in footprint.Pads()
            if p.GetNetname() and not p.GetNetname().startswith("unconnected-")
        }
        if not affected <= names:
            raise ValueError(
                f"Include every net on movable {reference}: {sorted(affected - names)}"
            )
        positions = item.get("positions_mm")
        rotations = item.get(
            "rotations_deg", [float(footprint.GetOrientationDegrees())]
        )
        if (
            not isinstance(positions, list)
            or not positions
            or not isinstance(rotations, list)
            or not rotations
        ):
            raise ValueError(
                "Provide nonempty positions_mm and optional rotations_deg arrays"
            )
        for position in positions:
            Point.from_mm(position)
        if any(
            isinstance(a, bool)
            or not isinstance(a, (int, float))
            or not math.isfinite(a)
            for a in rotations
        ):
            raise ValueError("Rotations must be finite numbers")
        refs.append(reference)
        choices.append(
            tuple((p[0], p[1], angle) for p in positions for angle in rotations)
        )
    if math.prod(len(c) for c in choices) > budget.limits.placement_trials:
        raise ValueError("Explicit placement combinations exceed placement_trials")
    # Every replacement is derived independently from the pristine snapshot.
    pristine = snapshot.directory / "poses"
    pristine.mkdir()
    base = native_path(pristine, ".kicad_pcb")
    base.write_bytes(snapshot.original)
    for values in itertools.product(*choices):
        budget.check()
        snapshot.board_path.write_bytes(snapshot.original)
        changes = [
            native_pose_change(base, ref, *pose)
            for ref, pose in strict_zip(refs, values)
        ]
        if changes:
            snapshot.board_path.write_bytes(
                CampaignPlanner(snapshot.directory).plan("pcb", changes).after
            )
        yield dict(strict_zip(refs, values)), tuple(changes)


def obstacles(path: Path, first_net: str, assume_refill: bool):
    return read_native_obstacles(path, first_net, assume_refill=assume_refill)
