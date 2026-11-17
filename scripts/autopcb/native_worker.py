"""Internal native worker. It handles only files selected by a product job."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from xml.etree import ElementTree as ET

import pcbnew as pcb

from core.kicad_sexpr import parse
from core.pcb_io import copper_width, enabled_copper_layers, quiet_duplicate_image_handlers


def _mm(item) -> list[float]:
    return [pcb.ToMM(item.x), pcb.ToMM(item.y)]


def inspect(source: Path) -> dict:
    board = pcb.LoadBoard(str(source))
    if board is None:
        raise ValueError("KiCad could not load the board")
    edges = board.GetBoardEdgesBoundingBox()
    return {
        "copper_layers": enabled_copper_layers(board), "thickness_mm": pcb.ToMM(board.GetDesignSettings().GetBoardThickness()),
        "bounds": [_mm(edges.GetOrigin()), _mm(edges.GetEnd())],
        "footprints": [{"ref": fp.GetReference(), "at": _mm(fp.GetPosition()), "locked": fp.IsLocked(),
                        "layer": board.GetLayerName(fp.GetLayer()),
                        "pads": [{"number": pad.GetNumber(), "net": pad.GetNetname(), "at": _mm(pad.GetPosition()),
                                  "size": _mm(pad.GetSize())} for pad in fp.Pads()]}
                       for fp in sorted(board.GetFootprints(), key=lambda fp: fp.GetReference())],
        "tracks": [{"net": tr.GetNetname(), "layer": board.GetLayerName(tr.GetLayer()),
                    "start": _mm(tr.GetStart()), "end": _mm(tr.GetEnd()),
                    "via": isinstance(tr, pcb.PCB_VIA), "arc": isinstance(tr, pcb.PCB_ARC),
                    "width_mm": pcb.ToMM(copper_width(tr, layer=tr.GetLayer())),
                    "length_mm": pcb.ToMM(tr.GetLength())} for tr in board.GetTracks()],
    }


def generate(path: Path, spec: dict) -> None:
    xml = ET.parse(spec["netlist"])
    board = pcb.BOARD()
    board.SetFileName(str(path))
    board.SetCopperLayerCount(spec["layers"])
    width, height = spec["size_mm"]
    lib_dir = Path(spec["footprints"])
    nets = {}
    assignments = {}
    for row in xml.findall("./nets/net"):
        name = row.attrib["name"]
        net = pcb.NETINFO_ITEM(board, name)
        board.Add(net)
        nets[name] = net
        for node in row.findall("node"):
            assignments[node.attrib["ref"], node.attrib["pin"]] = name
    for ref, component in spec["components"].items():
        group, footprint = component["footprint"].split(":", 1)
        source = lib_dir / f"{group}.pretty"
        item = pcb.FootprintLoad(str(source), footprint)
        if item is None:
            raise ValueError(f"Missing KiCad footprint {group}:{footprint}")
        identifier = pcb.LIB_ID()
        if identifier.Parse(pcb.UTF8(component["footprint"])) != -1:
            raise ValueError(f"Invalid KiCad footprint identifier: {component['footprint']}")
        item.SetFPID(identifier)
        item.SetReference(ref)
        item.SetValue(component["value"])
        for key, value in (("Manufacturer", component["manufacturer"]), ("MPN", component["mpn"]),
                           ("Datasheet", component["source"])):
            item.SetField(key, value)
            item.GetField(key).SetVisible(False)
        x, y, angle = component["position"]
        item.SetPosition(pcb.VECTOR2I(pcb.FromMM(x + 20), pcb.FromMM(y + 20)))
        item.SetOrientationDegrees(angle)
        native_path = pcb.KIID_PATH()
        for key in spec["links"][ref]:
            native_path.append(pcb.KIID(key))
        item.SetPath(native_path)
        board.Add(item)
        pads = set()
        for pad in item.Pads():
            number = pad.GetNumber()
            pads.add(number)
            name = assignments.get((ref, number))
            if name is not None and component["nets"].get(number) is not None:
                pad.SetNet(nets[name])
            elif component["nets"].get(number) is not None:
                raise ValueError(f"Missing schematic connection for {ref}.{number}")
            elif name is not None:
                pad.SetNet(nets[name])
        if not set(component["pins"]) <= pads:
            raise ValueError(f"Missing footprint pads for {ref}: {sorted(set(component['pins']) - pads)}")
    corners = [(20, 20), (20 + width, 20), (20 + width, 20 + height), (20, 20 + height)]
    for start, end in zip(corners, corners[1:] + corners[:1], strict=True):
        line = pcb.PCB_SHAPE()
        line.SetShape(pcb.SHAPE_T_SEGMENT)
        line.SetLayer(pcb.Edge_Cuts)
        line.SetWidth(pcb.FromMM(0.05))
        line.SetStart(pcb.VECTOR2I(*(pcb.FromMM(n) for n in start)))
        line.SetEnd(pcb.VECTOR2I(*(pcb.FromMM(n) for n in end)))
        board.Add(line)
    if pcb.SaveBoard(str(path), board) is False:
        raise RuntimeError("KiCad failed to save the new board")
    # KiCad 10's descriptor is opaque in SWIG. Populate the native setup
    # explicitly so validation and future edits retain the same layer model.
    text = path.read_text(encoding="utf-8")
    setup = parse(text).children("setup")[0]
    copper = enabled_copper_layers(board)
    dielectric = [1.53] if len(copper) == 2 else [0.18, 1.10, 0.18]
    if len(dielectric) != len(copper) - 1:
        raise ValueError("The catalog lacks a stack model for the requested layer count")
    layers = []
    for index, layer in enumerate(copper):
        layers.append(f'(layer "{layer}" (type "copper") (thickness 0.035))')
        if index < len(dielectric):
            layers.append(f'(layer "dielectric {index+1}" (type "core") (thickness {dielectric[index]})'
                          ' (material "FR4") (epsilon_r 4.3) (loss_tangent 0.02))')
    marker = setup.start + len("(setup")
    path.write_text(text[:marker] + "\n\t\t(stackup\n\t\t\t" + "\n\t\t\t".join(layers)
                    + "\n\t\t)\n" + text[marker:], encoding="utf-8", newline="\n")


def reference_screen(entry: Path) -> dict:
    from core.project import ProjectPaths, read_profile
    from pcb.net_routing.reference import reference_report

    paths = ProjectPaths(entry)
    configured = read_profile(paths.root).get("differential_pairs", [])
    names = {name for pair in configured for name in pair}
    board = pcb.LoadBoard(str(paths.board))
    candidates = board.GetNetsByName()
    mapped = {expected: [str(key) for key in candidates if str(key) == expected or str(key) == "/" + expected]
              for expected in names}
    missing = sorted(name for name, found in mapped.items() if len(found) != 1)
    if missing:
        return {"status": "blocked", "missing_or_ambiguous_nets": missing, "electrically_verified": False}
    return reference_report(board, {key[0] for key in mapped.values()})


def compare(entry: Path, run_id: str) -> dict:
    from autopcb.layout import _check_id
    from core.paths import asset_root, config_path, stored_result
    from core.project import ProjectPaths
    from pcb.checkpoints import read_config
    from pcb.net_routing.controller import decode_checkpoint
    from pcb.net_routing.native import Snapshot
    from pcb.net_routing.runner import _source_hashes

    paths = ProjectPaths(entry)
    assets = asset_root(paths.root)
    state_path = stored_result(paths.root, "autoroute", f"{_check_id(run_id)}.json")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("schema") != 1 or state.get("run_id") != run_id:
        raise ValueError("Invalid layout preview record")
    if state.get("status") == "running":
        raise ValueError("Wait for the preview to finish or cancel it before comparing the result")
    _, policy = read_config(config_path(paths.root, "autopcb.json"))
    with Snapshot(paths.board) as baseline:
        candidate = decode_checkpoint(state["checkpoint"], _source_hashes(baseline, paths.root, assets),
                                      baseline.tool_hashes, policy)
        before = inspect(baseline.board_path)
        baseline.board_path.write_bytes(candidate)
        after = inspect(baseline.board_path)
    return {"before": before, "after": after}

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("inspect", "generate", "compare", "reference", "apply"))
    parser.add_argument("board", type=Path)
    parser.add_argument("record", type=Path)
    parser.add_argument("run_id", nargs="?")
    args = parser.parse_args()
    with quiet_duplicate_image_handlers():
        if args.operation == "inspect":
            args.record.write_text(json.dumps(inspect(args.board), allow_nan=False), encoding="utf-8")
        elif args.operation == "reference":
            args.record.write_text(json.dumps(reference_screen(args.board), allow_nan=False), encoding="utf-8")
        elif args.operation == "compare":
            args.record.write_text(json.dumps(compare(args.board, args.run_id), allow_nan=False), encoding="utf-8")
        elif args.operation == "apply":
            from autopcb.layout import _check_id
            from core.project import ENTRY_ENV, ProjectPaths
            from pcb.net_routing.working_save import save_working

            paths = ProjectPaths(args.board)
            os.environ[ENTRY_ENV] = str(paths.entry)
            result = save_working(paths.root, _check_id(args.run_id), apply=True, progress=lambda _message: None)
            args.record.write_text(json.dumps(result, allow_nan=False), encoding="utf-8")
        else:
            generate(args.board, json.loads(args.record.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
