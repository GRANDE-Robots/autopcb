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

    def as_dict(self) -> dict:
        board, point = self.board, self.point
        edges = board.GetBoardEdgesBoundingBox()
        return {
            "copper_layers": enabled_copper_layers(board),
            "thickness_mm": pcb.ToMM(board.GetDesignSettings().GetBoardThickness()),
            "bounds": [point(edges.GetOrigin()), point(edges.GetEnd())],
            "footprints": [{"ref": fp.GetReference(), "at": point(fp.GetPosition()), "locked": fp.IsLocked(),
                            "layer": board.GetLayerName(fp.GetLayer()),
                            "pads": [{"number": pad.GetNumber(), "net": pad.GetNetname(),
                                      "at": point(pad.GetPosition()), "size": point(pad.GetSize())}
                                     for pad in fp.Pads()]}
                           for fp in sorted(board.GetFootprints(), key=lambda fp: fp.GetReference())],
            "tracks": [{"net": tr.GetNetname(), "layer": board.GetLayerName(tr.GetLayer()),
                        "start": point(tr.GetStart()), "end": point(tr.GetEnd()),
                        "via": isinstance(tr, pcb.PCB_VIA), "arc": isinstance(tr, pcb.PCB_ARC),
                        "width_mm": pcb.ToMM(copper_width(tr, layer=tr.GetLayer())),
                        "length_mm": pcb.ToMM(tr.GetLength())} for tr in board.GetTracks()],
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

    def _outline(self, board) -> None:
        width, height, low = *self.spec["size_mm"], self.OFFSET_MM
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

    def _gap(self, track, net: str) -> dict:
        """Measure the unreferenced length and the pad the trace launches from."""
        x0, y0, x1, y1, _width = track.geometry
        stations = [track.samples[index:index + 3] for index in range(0, len(track.samples), 3)]
        spacing = math.hypot(x1 - x0, y1 - y0) / max(1, len(stations) - 1) / 1e6
        missing = [index for index, station in enumerate(stations) if not all(point[2] for point in station)]
        end = (x0, y0) if missing and sum(missing) / len(missing) < (len(stations) - 1) / 2 else (x1, y1)
        launch = next((f"{pad.GetParentFootprint().GetReference()}.{pad.GetNumber()}"
                       for pad in self.board.GetPads()
                       if pad.GetNetname() == net and pad.HitTest(pcb.VECTOR2I(*end))), None)
        return {"unreferenced_mm": round(len(missing) * spacing, 3), "launch": launch}

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
                **(self._gap(covered, name) if kind == "sampled_reference_gap" else {}),
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
