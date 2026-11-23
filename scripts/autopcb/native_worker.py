"""Internal native worker. It handles only files selected by a product job."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
import os
from pathlib import Path
from xml.etree import ElementTree as ET

import pcbnew as pcb

from core.kicad_sexpr import parse
from core.pcb_io import ProjectSettings, copper_width, enabled_copper_layers, quiet_duplicate_image_handlers
from core.compat import strict_zip


class BoardGeometry:
    """Read a saved board's geometry for comparisons and reports."""

    def __init__(self, source: Path):
        self.board = pcb.LoadBoard(str(source))
        if self.board is None:
            raise ValueError("KiCad could not load the board")

    @staticmethod
    def point(item) -> list[float]:
        return [pcb.ToMM(item.x), pcb.ToMM(item.y)]

    def contours(self, polygons) -> list:
        rings = []
        for index in range(polygons.OutlineCount()):
            outline = polygons.Outline(index)
            rings.append([self.point(outline.CPoint(i)) for i in range(outline.PointCount())])
            for hole in range(polygons.HoleCount(index)):
                inner = polygons.CHole(index, hole)
                rings.append([self.point(inner.CPoint(i)) for i in range(inner.PointCount())])
        return rings

    def pad(self, pad, stack) -> dict:
        layer = next((layer for layer in stack if pad.IsOnLayer(layer)), None)
        result = {"number": pad.GetNumber(), "net": pad.GetNetname(), "at": self.point(pad.GetPosition()),
                  "size": self.point(pad.GetSize()),
                  "contours": self.contours(pad.GetEffectivePolygon(layer)) if layer is not None else []}
        if pad.GetDrillSize().x > 0:
            hole = pad.GetEffectiveHoleShape()
            result["hole"] = {"start": self.point(hole.GetStart()), "end": self.point(hole.GetEnd()), "width_mm": pcb.ToMM(hole.GetWidth())}
        return result

    def drawn(self, item, layer: int) -> list:
        shape = pcb.SHAPE_POLY_SET()
        item.TransformShapeToPolygon(shape, layer, 0, pcb.FromMM(0.01), pcb.ERROR_INSIDE)
        return self.contours(shape)

    def track(self, track) -> dict:
        layer = track.GetLayer()
        result = {"net": track.GetNetname(), "layer": self.board.GetLayerName(layer),
                  "start": self.point(track.GetStart()), "end": self.point(track.GetEnd()),
                  "via": isinstance(track, pcb.PCB_VIA), "arc": isinstance(track, pcb.PCB_ARC),
                  "width_mm": pcb.ToMM(copper_width(track, layer=layer)), "length_mm": pcb.ToMM(track.GetLength())}
        if result["via"]:
            result["drill_mm"] = pcb.ToMM(track.GetDrillValue())
        elif result["arc"]:
            result["contours"] = self.drawn(track, layer)
        return result

    def as_dict(self) -> dict:
        board = self.board
        edges = board.GetBoardEdgesBoundingBox()
        outline = pcb.SHAPE_POLY_SET()
        closed_outline = board.GetBoardPolygonOutlines(outline, False)
        stack = tuple(board.GetEnabledLayers().CuStack())
        return {
            "copper_layers": enabled_copper_layers(board), "thickness_mm": pcb.ToMM(board.GetDesignSettings().GetBoardThickness()),
            "bounds": [self.point(edges.GetOrigin()), self.point(edges.GetEnd())],
            "outline": self.contours(outline) if closed_outline else [],
            "footprints": [{"ref": fp.GetReference(), "at": self.point(fp.GetPosition()), "locked": fp.IsLocked(),
                            "layer": board.GetLayerName(fp.GetLayer()), "label_at": self.point(fp.Reference().GetPosition()),
                            "front_silk": [self.drawn(item, pcb.F_SilkS) for item in fp.GraphicalItems()
                                           if item.GetLayer() == pcb.F_SilkS and isinstance(item, pcb.PCB_SHAPE)],
                            "pads": [self.pad(pad, stack) for pad in fp.Pads()]}
                           for fp in sorted(board.GetFootprints(), key=lambda fp: fp.GetReference())],
            "tracks": [self.track(track) for track in board.GetTracks()],
        }


class BoardGenerator:
    """Build a native board from an approved proposal and its exported netlist."""

    OFFSET_MM = 20
    DIELECTRIC_MM = {2: [1.53], 4: [0.18, 1.10, 0.18]}

    def __init__(self, path: Path, spec: dict):
        self.path, self.spec = path, spec

    def write(self) -> None:
        # KiCad will not save project settings once the board is open.
        ProjectSettings(self.path).write()
        board = pcb.BOARD()
        board.SetFileName(str(self.path))
        board.SetCopperLayerCount(self.spec["layers"])
        nets, assignments = self._nets(board)
        for reference, component in self.spec["components"].items():
            self._place(board, reference, component, nets, assignments)
        self._outline(board)
        if pcb.SaveBoard(str(self.path), board) is False:
            raise RuntimeError("KiCad failed to save the new board")
        self._stackup(enabled_copper_layers(board))

    def _nets(self, board) -> tuple[dict, dict]:
        nets, assignments = {}, {}
        for row in ET.parse(self.spec["netlist"]).findall("./nets/net"):
            name = row.attrib["name"]
            nets[name] = pcb.NETINFO_ITEM(board, name)
            board.Add(nets[name])
            for node in row.findall("node"):
                assignments[node.attrib["ref"], node.attrib["pin"]] = name
        return nets, assignments

    def _place(self, board, reference: str, component: dict, nets: dict, assignments: dict) -> None:
        group, footprint = component["footprint"].split(":", 1)
        item = pcb.FootprintLoad(str(Path(self.spec["footprints"]) / f"{group}.pretty"), footprint)
        if item is None:
            raise ValueError(f"Missing KiCad footprint {group}:{footprint}")
        identifier = pcb.LIB_ID()
        if identifier.Parse(pcb.UTF8(component["footprint"])) != -1:
            raise ValueError(f"Invalid KiCad footprint identifier: {component['footprint']}")
        item.SetFPID(identifier)
        item.SetReference(reference)
        item.SetValue(component["value"])
        for key, value in (("Manufacturer", component["manufacturer"]), ("MPN", component["mpn"]),
                           ("Datasheet", component["source"])):
            item.SetField(key, value)
            item.GetField(key).SetVisible(False)
        x, y, angle = component["position"]
        item.SetPosition(pcb.VECTOR2I(pcb.FromMM(x + self.OFFSET_MM), pcb.FromMM(y + self.OFFSET_MM)))
        item.SetOrientationDegrees(angle)
        native_path = pcb.KIID_PATH()
        for key in self.spec["links"][reference]:
            native_path.append(pcb.KIID(key))
        item.SetPath(native_path)
        board.Add(item)
        if component.get("side", "top") == "bottom":
            item.Flip(item.GetPosition(), pcb.FLIP_DIRECTION_TOP_BOTTOM)
        pads = set()
        for pad in item.Pads():
            number = pad.GetNumber()
            pads.add(number)
            name = assignments.get((reference, number))
            if name is None and component["nets"].get(number) is not None:
                raise ValueError(f"Missing schematic connection for {reference}.{number}")
            if name is not None:
                pad.SetNet(nets[name])
        if not set(component["pins"]) <= pads:
            raise ValueError(f"Missing footprint pads for {reference}: {sorted(set(component['pins']) - pads)}")

    def _fit_outline(self, board, outline):
        from autopcb.placement import Envelope, OutlineFitter

        origin = pcb.FromMM(self.OFFSET_MM)
        envelopes = {}
        for fp in board.GetFootprints():
            # Body drawings, pads and courtyards plus the markings physically printed on the board.
            box = fp.GetBoundingBox(False, False)
            for item in (*fp.GetFields(), *fp.GraphicalItems()):
                if (item.GetLayer() in (pcb.F_SilkS, pcb.B_SilkS)
                        and (not isinstance(item, pcb.PCB_FIELD) or item.IsVisible())):
                    box.Merge(item.GetBoundingBox())
            for pad in fp.Pads():
                if pad.GetDrillSize().x > 0:
                    box.Merge(pad.GetEffectiveHoleShape().BBox())
            envelopes[fp.GetReference()] = Envelope((box.GetLeft() - origin, box.GetTop() - origin),
                                                    (box.GetRight() - origin, box.GetBottom() - origin))
        size, offsets = OutlineFitter(envelopes, outline["movable_groups"],
                                    tuple(map(pcb.FromMM, outline["minimum_mm"])),
                                    tuple(map(pcb.FromMM, self.spec["size_mm"])),
                                    max(pcb.FromMM(outline["margin_mm"]), board.GetDesignSettings().m_CopperEdgeClearance)).fit()
        width, height = map(pcb.ToMM, size)
        for fp in board.GetFootprints():
            if fp.GetReference() in offsets:
                fp.Move(pcb.VECTOR2I(*offsets[fp.GetReference()]))
        return width, height

    def _outline(self, board) -> None:
        outline = self.spec.get("compact_outline")
        width, height = self._fit_outline(board, outline) if outline else self.spec["size_mm"]
        low = self.OFFSET_MM
        corners = [(low, low), (low + width, low), (low + width, low + height), (low, low + height)]
        for start, end in strict_zip(corners, corners[1:] + corners[:1]):
            line = pcb.PCB_SHAPE()
            line.SetShape(pcb.SHAPE_T_SEGMENT)
            line.SetLayer(pcb.Edge_Cuts)
            line.SetWidth(pcb.FromMM(0.05))
            line.SetStart(pcb.VECTOR2I(*(pcb.FromMM(n) for n in start)))
            line.SetEnd(pcb.VECTOR2I(*(pcb.FromMM(n) for n in end)))
            board.Add(line)

    def _stackup(self, copper: tuple[str, ...]) -> None:
        # KiCad 10's descriptor is opaque in SWIG. Write the native setup
        # explicitly so validation and later edits keep the same layer model.
        dielectric = self.DIELECTRIC_MM.get(len(copper))
        if dielectric is None:
            raise ValueError("The catalog lacks a stack model for the requested layer count")
        layers = []
        for index, layer in enumerate(copper):
            layers.append(f'(layer "{layer}" (type "copper") (thickness 0.035))')
            if index < len(dielectric):
                layers.append(f'(layer "dielectric {index+1}" (type "core") (thickness {dielectric[index]})'
                              ' (material "FR4") (epsilon_r 4.3) (loss_tangent 0.02))')
        text = self.path.read_text(encoding="utf-8")
        marker = parse(text).children("setup")[0].start + len("(setup")
        self.path.write_bytes((text[:marker] + "\n\t\t(stackup\n\t\t\t" + "\n\t\t\t".join(layers)
                               + "\n\t\t)\n" + text[marker:]).encode("utf-8"))


class ReferenceScreen:
    """Sample return copper under each declared differential pair."""

    STEP_MM = 0.1

    def __init__(self, entry: Path):
        from core.project import ProjectPaths

        self.paths = ProjectPaths(entry)
        self.board = pcb.LoadBoard(str(self.paths.board))
        if self.board is None:
            raise ValueError("KiCad could not reopen the board for reference-copper checks")

    def _scope(self, names: set[str]) -> tuple[list, str | None]:
        """Return the declared pairs and their reference net, or raise the blocker."""
        from core.project import declared_pairs, read_profile
        from pcb.net_routing.reference import high_speed_nets

        pairs = declared_pairs(self.paths.root, names)
        if pairs is None:
            raise ValueError("Review the high-speed scope and record an explicit pair list in the project profile.")
        critical = {name for pair in pairs for name in pair}
        settings = self.paths.settings.read_bytes() if self.paths.settings.is_file() else b"{}"
        if "net_settings" in json.loads(settings.decode("utf-8-sig")):
            omitted = high_speed_nets(settings, names) - critical
            if omitted:
                raise ValueError("Reviewed scope omits nets assigned to native high-speed classes: " +
                                 ", ".join(sorted(omitted)))
        if not pairs:
            return pairs, None
        configured = read_profile(self.paths.root).get("reference_net", "GND")
        if not isinstance(configured, str) or not configured:
            raise ValueError("Choose a native reference net in the project profile.")
        matches = names & {configured, "/" + configured}
        if len(matches) != 1 or matches & critical:
            raise ValueError("Reference net is missing, ambiguous, or assigned to a signal pair.")
        return pairs, matches.pop()

    @staticmethod
    def gap_spans(track, through_pads, signal_layer: int, reference_layer: int) -> list[dict]:
        x0, y0, x1, y1, _ = track.geometry
        last_station = len(track.samples) // 3 - 1
        spacing = math.hypot(pcb.ToMM(x1 - x0), pcb.ToMM(y1 - y0)) / max(1, last_station)
        spans = []
        for run in track.missing_runs():
            terminals = set()
            for station, point in ((0, pcb.VECTOR2I(x0, y0)), (last_station, pcb.VECTOR2I(x1, y1))):
                # A launch gap starts at the pad edge: every station before it lies in the pad.
                between = range(station, run.first_station) if station < run.first_station else range(
                    run.last_station + 1, station + 1)
                if any(not track.samples[3 * index + 1][3] for index in between):
                    continue
                for pad in through_pads:
                    if (pad.IsOnLayer(signal_layer) and pad.IsOnLayer(reference_layer)
                            and pad.GetBoundingBox().Contains(point)
                            and pad.GetEffectivePolygon(signal_layer).Contains(point)):
                        terminals.add((pad.GetParentFootprint().GetReference(), pad.GetNumber()))
            x, y = run.position
            spans.append({"first_sample_mm": round(run.first_station * spacing, 6),
                          "last_sample_mm": round(run.last_station * spacing, 6),
                          "position_mm": {"x": pcb.ToMM(x), "y": pcb.ToMM(y)},
                          "missing_samples": run.missing_samples,
                          "endpoint_pads": [{"reference": ref, "pad": pin} for ref, pin in sorted(terminals)]})
        return spans

    @classmethod
    def _gap(cls, track, pads, signal_layer, reference_layer):
        spans = cls.gap_spans(track, pads, signal_layer, reference_layer)
        x0, y0, x1, y1, _ = track.geometry
        length = math.hypot(pcb.ToMM(x1 - x0), pcb.ToMM(y1 - y0))
        last = len(track.samples) // 3 - 1
        spacing = length / max(1, last)
        # End stations represent half a sample interval inside the trace.
        sampled_length = sum(min(length, span["last_sample_mm"] + spacing / 2)
                             - max(0.0, span["first_sample_mm"] - spacing / 2) for span in spans)
        launches = {pad["reference"] + "." + pad["pad"] for span in spans for pad in span["endpoint_pads"]}
        return {"unreferenced_mm": round(sampled_length, 3),
                "launch": next(iter(launches)) if len(launches) == 1 else None,
                "gap_spans": spans, "positions_mm": [span["position_mm"] for span in spans]}

    def run(self) -> dict:
        from pcb.net_routing.reference import sample_reference

        board = self.board
        result = {"schema_version": 1, "status": "blocked", "electrically_verified": False,
                  "pairs": [], "findings": [], "blockers": []}
        names = {str(name) for name in board.GetNetsByName() if str(name)}
        try:
            pairs, ground = self._scope(names)
        except ValueError as error:
            result["blockers"].append(str(error))
            return result
        if not pairs:
            return {**result, "status": "not_applicable"}
        critical = {name for pair in pairs for name in pair}
        sample = sample_reference(board, critical, reference_net=ground)
        covered_by_id = {track.item_id: track for track in sample.tracks}
        with_reference = {track.net for track in sample.tracks if any(value[2] for value in track.samples)}
        metrics = {name: {"net": name, "trace_length_mm": 0.0, "widths_mm_by_layer": defaultdict(set),
                          "segments": 0, "vias": 0} for name in critical}
        through_pads = defaultdict(list)
        for pad in board.GetPads():
            if pad.GetNetname() in critical and pad.GetAttribute() == pcb.PAD_ATTRIB_PTH:
                through_pads[pad.GetNetname()].append(pad)
        for item in board.GetTracks():
            name = str(item.GetNetname())
            if name not in critical:
                continue
            item_id = item.m_Uuid.AsString()
            position = BoardGeometry.point(item.GetPosition())
            layer = str(board.GetLayerName(item.GetLayer()))
            covered = covered_by_id.get(item_id)
            measured = metrics[name]
            if isinstance(item, pcb.PCB_VIA):
                measured["vias"] += 1
                kind = "via_transition"
            else:
                measured["segments"] += 1
                measured["trace_length_mm"] += pcb.ToMM(item.GetLength())
                measured["widths_mm_by_layer"][layer].add(pcb.ToMM(copper_width(item)))
                if covered is None:
                    kind = "unsupported_trace"
                elif covered.missing_samples or not covered.continuous_polygon:
                    kind = "sampled_reference_gap"
                else:
                    continue
            result["findings"].append({
                "id": f"{kind}/{item_id}", "type": kind, "net": name, "signal_layer": layer,
                "reference_layer": covered.reference_layer if covered else None,
                "missing_samples": covered.missing_samples if covered else None,
                "continuous_polygon": covered.continuous_polygon if covered else None,
                "positions_mm": [{"x": position[0], "y": position[1]}],
                **(self._gap(covered, through_pads[name], item.GetLayer(), board.GetLayerID(covered.reference_layer))
                   if kind == "sampled_reference_gap" else {}),
            })
        for row in metrics.values():
            row["trace_length_mm"] = round(row["trace_length_mm"], 6)
            row["widths_mm_by_layer"] = {layer: sorted(widths) for layer, widths in row["widths_mm_by_layer"].items()}
            if not row["segments"]:
                result["blockers"].append(f"{row['net']} has no routed traces to screen.")
            elif row["net"] not in with_reference:
                result["blockers"].append(f"No adjacent reference copper was sampled under {row['net']}.")
        result["blockers"].sort()
        result["pairs"] = [{"positive": metrics[p], "negative": metrics[n]} for p, n in pairs]
        result["findings"].sort(key=lambda item: item["id"])
        result.update(reference_net=ground, sampling_step_mm=self.STEP_MM)
        if not result["blockers"]:
            result["status"] = "needs_review" if result["findings"] else "sampled"
        return result


class PreviewComparison:
    """Inspect the saved board and a stored routing preview side by side."""

    def __init__(self, entry: Path, run_id: str):
        self.entry, self.run_id = entry, run_id

    def run(self) -> dict:
        from autopcb.layout import Layout
        from core.paths import asset_root, config_path, stored_result
        from core.project import ProjectPaths
        from pcb.checkpoints import read_config
        from pcb.net_routing.controller import decode_checkpoint
        from pcb.net_routing.native import Snapshot
        from pcb.net_routing.runner import _source_hashes

        paths = ProjectPaths(self.entry)
        state = json.loads(stored_result(paths.root, "autoroute", f"{Layout.check_id(self.run_id)}.json")
                           .read_text(encoding="utf-8"))
        if state.get("schema") != 1 or state.get("run_id") != self.run_id:
            raise ValueError("Invalid layout preview record")
        if state.get("status") == "running":
            raise ValueError("Wait for the preview to finish or cancel it before comparing the result")
        _, policy = read_config(config_path(paths.root, "autopcb.json"))
        with Snapshot(paths.board) as baseline:
            candidate = decode_checkpoint(state["checkpoint"],
                                          _source_hashes(baseline, paths.root, asset_root(paths.root)),
                                          baseline.tool_hashes, policy)
            before = BoardGeometry(baseline.board_path).as_dict()
            baseline.board_path.write_bytes(candidate)
            after = BoardGeometry(baseline.board_path).as_dict()
        return {"before": before, "after": after}


class NativeWorker:
    """Run one native operation on files a product job selected."""

    OPERATIONS = ("inspect", "generate", "compare", "reference", "apply", "pair-seed")

    def __init__(self, argv: list[str] | None = None):
        parser = argparse.ArgumentParser()
        parser.add_argument("operation", choices=self.OPERATIONS)
        parser.add_argument("board", type=Path)
        parser.add_argument("record", type=Path)
        parser.add_argument("argument", nargs="?")
        self.args = parser.parse_args(argv)

    def _result(self) -> dict | None:
        args = self.args
        if args.operation == "inspect":
            return BoardGeometry(args.board).as_dict()
        if args.operation == "reference":
            return ReferenceScreen(args.board).run()
        if args.operation == "pair-seed":
            from autopcb.pair_seed import seed

            return seed(args.board, json.loads(Path(args.argument).read_text(encoding="utf-8")))
        if args.operation == "compare":
            return PreviewComparison(args.board, args.argument).run()
        if args.operation == "apply":
            from autopcb.layout import Layout
            from core.project import ENTRY_ENV, ProjectPaths
            from pcb.net_routing.working_save import save_working

            paths = ProjectPaths(args.board)
            os.environ[ENTRY_ENV] = str(paths.entry)
            return save_working(paths.root, Layout.check_id(args.argument), apply=True,
                                progress=lambda _message: None)
        BoardGenerator(args.board, json.loads(args.record.read_text(encoding="utf-8"))).write()
        return None

    def run(self) -> int:
        with quiet_duplicate_image_handlers():
            result = self._result()
        if result is not None:
            self.args.record.write_text(json.dumps(result, allow_nan=False), encoding="utf-8")
        return 0


if __name__ == "__main__":
    raise SystemExit(NativeWorker().run())
