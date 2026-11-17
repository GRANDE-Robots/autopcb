"""Compare native PCB geometry with a generated manufacturing review folder.

Writes new source-bound JSON reports only outside the source and manufacturing
folders. This is a consistency check, not a fabrication approval.
"""

import argparse
import collections
import csv
import hashlib
import json
import zipfile
from pathlib import Path

import pcbnew
from core.pcb_io import copper_width


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument(
        "--board",
        type=Path,
        help="board in the project root; auto-detected when unique",
    )
    ap.add_argument(
        "--manufacturing", type=Path, required=True, help="generated review folder"
    )
    ap.add_argument(
        "--out", type=Path, required=True, help="new report folder outside project"
    )
    a = ap.parse_args()
    P = a.project.resolve()
    M = a.manufacturing.resolve()
    if a.board:
        board_path = (a.board if a.board.is_absolute() else P / a.board).resolve()
    else:
        boards = list(P.glob("*.kicad_pcb"))
        if len(boards) != 1:
            raise RuntimeError(
                "Expected one project board; select it explicitly with --board"
            )
        board_path = boards[0].resolve()
    if (
        board_path.parent != P
        or board_path.suffix.lower() != ".kicad_pcb"
        or not board_path.is_file()
    ):
        raise ValueError("Board must be an existing KiCad PCB in the project root")
    settings_path = board_path.with_suffix(".kicad_pro")
    if not settings_path.is_file():
        raise ValueError("Board has no matching KiCad project settings file")
    output_dir = a.out.resolve()
    if output_dir.is_relative_to(P) or output_dir.is_relative_to(M):
        raise RuntimeError(
            "Keep board audit output outside the project and manufacturing folders"
        )
    if output_dir.exists() and not output_dir.is_dir():
        raise RuntimeError("Board audit output must be a folder")
    report_path = output_dir / "pcb_native_review.json"
    geometry_path = output_dir / "pcb_geometry_detail.json"
    if report_path.exists() or geometry_path.exists():
        raise FileExistsError(
            "Board audit reports already exist; use a new output folder"
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    def sha(p):
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()

    def native_hashes():
        return {
            path.name: sha(path)
            for path in P.glob(f"{board_path.stem}.kicad_*")
            if path.suffix in {".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_dru"}
        }

    before = native_hashes()
    b = pcbnew.LoadBoard(str(board_path))
    if b is None:
        raise RuntimeError("KiCad could not load the saved board")
    origin = b.GetDesignSettings().GetAuxOrigin()
    ox = origin.x / 1e6
    oy = origin.y / 1e6

    def mm(v):
        return round(v / 1e6, 6)

    def xy(p):
        return [mm(p.x), mm(p.y)]

    fs = {f.GetReference(): f for f in b.GetFootprints()}
    tr = []
    vs = []
    pd = []
    for t in b.GetTracks():
        d = {
            "uuid": t.m_Uuid.AsString(),
            "net": t.GetNetname(),
            "layer": b.GetLayerName(t.GetLayer()),
            "at": xy(t.GetPosition()),
            "width": mm(copper_width(t, layer=pcbnew.F_Cu)),
        }
        if isinstance(t, pcbnew.PCB_VIA):
            d.update(
                drill=mm(t.GetDrillValue()),
                layers=[b.GetLayerName(i) for i in t.GetLayerSet().CuStack()],
            )
            vs.append(d)
        else:
            d.update(
                start=xy(t.GetStart()), end=xy(t.GetEnd()), length=mm(t.GetLength())
            )
            tr.append(d)
    for r, f in fs.items():
        for p in f.Pads():
            pd.append(
                {
                    "ref": r,
                    "pin": p.GetNumber(),
                    "net": p.GetNetname(),
                    "at": xy(p.GetPosition()),
                    "size": xy(p.GetSize()),
                    "drill": xy(p.GetDrillSize()),
                    "attribute": int(p.GetAttribute()),
                    "layers": [b.GetLayerName(i) for i in p.GetLayerSet().CuStack()],
                }
            )
    zones = []
    for z in b.Zones():
        zones.append(
            {
                "net": z.GetNetname(),
                "layers": [b.GetLayerName(i) for i in z.GetLayerSet().CuStack()],
                "filled": z.IsFilled(),
                "pad_connection": int(z.GetPadConnection()),
                "thermal_spoke_width": mm(z.GetThermalReliefSpokeWidth()),
                "thermal_gap": mm(z.GetThermalReliefGap()),
                "outline_count": z.Outline().OutlineCount(),
            }
        )
    g = [
        {"name": x.GetName(), "members": [q.m_Uuid.AsString() for q in x.GetItems()]}
        for x in b.Groups()
    ]
    pro = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    classes = {q["name"]: q for q in pro["net_settings"]["classes"]}
    interesting = sorted(
        name
        for name in {item["net"] for item in [*tr, *vs, *pd]}
        if name and not name.startswith("unconnected-")
    )
    nets = {}
    for n in interesting:
        tt = [t for t in tr if t["net"] == n]
        vv = [v for v in vs if v["net"] == n]
        nets[n] = {
            "width_lengths_mm": {
                str(w): round(sum(t["length"] for t in tt if t["width"] == w), 3)
                for w in sorted({t["width"] for t in tt})
            },
            "layers": dict(collections.Counter(t["layer"] for t in tt)),
            "total_track_length": round(sum(t["length"] for t in tt), 3),
            "vias": vv,
            "pads": [p for p in pd if p["net"] == n],
        }

    def read(p):
        return list(csv.DictReader(Path(p).open(encoding="utf-8-sig", newline="")))

    bom = read(M / "02_BOM_COMPLETE.csv")
    cpl = read(M / "03_CPL_COMPLETE.csv")
    br = [r.strip() for row in bom for r in row["Designator"].split(",")]
    cr = [row["Designator"] for row in cpl]
    missing = set(fs) - set(cr)
    cpl_errors = []
    for row in cpl:
        r = row["Designator"]
        if r not in fs:
            cpl_errors.append(r)
            continue
        f = fs[r]
        layer = pcbnew.F_Fab if f.GetLayer() == pcbnew.F_Cu else pcbnew.B_Fab
        gg = [
            x.GetBoundingBox()
            for x in f.GraphicalItems()
            if isinstance(x, pcbnew.PCB_SHAPE) and x.GetLayer() == layer
        ]
        cx, cy = (
            (
                (min(x.GetX() for x in gg) + max(x.GetRight() for x in gg)) / 2e6,
                (min(x.GetY() for x in gg) + max(x.GetBottom() for x in gg)) / 2e6,
            )
            if gg
            else tuple(xy(f.GetPosition()))
        )
        if (
            abs(float(row["Mid X"]) - (cx - ox)) > 1e-5
            or abs(float(row["Mid Y"]) - (oy - cy)) > 1e-5
            or row["Layer"] != ("Top" if f.GetLayer() == pcbnew.F_Cu else "Bottom")
            or abs(
                (float(row["Rotation"]) - f.GetOrientationDegrees() + 180) % 360 - 180
            )
            > 1e-5
        ):
            cpl_errors.append(r)
    fill = read(M / "assembly/EXPLICIT_FILLED_CAPPED_VIAS.csv")
    vby = {v["uuid"]: v for v in vs}
    fill_errors = []
    for row in fill:
        v = vby.get(row["UUID"])
        if (
            not v
            or max(
                abs(float(row["X mm from datum"]) - (v["at"][0] - ox)),
                abs(float(row["Y mm from datum"]) - (oy - v["at"][1])),
                abs(float(row["Drill mm"]) - v["drill"]),
                abs(float(row["Land mm"]) - v["width"]),
            )
            > 1e-5
        ):
            fill_errors.append(row)
    zcheck = []
    with zipfile.ZipFile(M / "01_PCB_GERBERS_DRILLS.zip") as z:
        for name in z.namelist():
            q = M / "gerbers" / Path(name).name
            if q.exists() and hashlib.sha256(z.read(name)).hexdigest() != sha(q):
                zcheck.append(name)
    legend = []
    for f in fs.values():
        for t in [f.Reference(), f.Value(), *f.GraphicalItems()]:
            if (
                isinstance(t, pcbnew.PCB_TEXT)
                and t.GetLayer() in [pcbnew.F_SilkS, pcbnew.B_SilkS]
                and t.IsVisible()
            ):
                legend.append(
                    {
                        "ref": f.GetReference(),
                        "text": t.GetText(),
                        "layer": b.GetLayerName(t.GetLayer()),
                        "height": mm(t.GetTextHeight()),
                        "thickness": mm(t.GetTextThickness()),
                        "at": xy(t.GetPosition()),
                    }
                )
    for t in b.GetDrawings():
        if isinstance(t, pcbnew.PCB_TEXT) and t.GetLayer() in [
            pcbnew.F_SilkS,
            pcbnew.B_SilkS,
        ]:
            legend.append(
                {
                    "ref": "board",
                    "text": t.GetText(),
                    "layer": b.GetLayerName(t.GetLayer()),
                    "height": mm(t.GetTextHeight()),
                    "thickness": mm(t.GetTextThickness()),
                    "at": xy(t.GetPosition()),
                }
            )
    report = {
        "source_sha256": before,
        "source_unchanged": before == native_hashes(),
        "footprints": len(fs),
        "tracks": len(tr),
        "vias": len(vs),
        "nets": len([n for n in b.GetNetsByName() if str(n)]),
        "layer_track_counts": dict(collections.Counter(t["layer"] for t in tr)),
        "layer_track_lengths": {
            layer: round(sum(t["length"] for t in tr if t["layer"] == layer), 3)
            for layer in sorted({t["layer"] for t in tr})
        },
        "via_sizes": {
            str(k): v
            for k, v in collections.Counter(
                (v["width"], v["drill"]) for v in vs
            ).items()
        },
        "zones": zones,
        "groups": g,
        "key_nets": nets,
        "net_summary_scope": "All named copper and pad nets, excluding generated unconnected-pad nets.",
        "drc_exclusions": pro["board"]["design_settings"]["drc_exclusions"],
        "ignored_checks": [
            k
            for k, v in pro["board"]["design_settings"]["rule_severities"].items()
            if v == "ignore"
        ],
        "netclasses": classes,
        "BOM_CPL": {
            "bomrows": len(bom),
            "bomrefs": len(br),
            "cplrows": len(cr),
            "BOM_minus_CPL": sorted(set(br) - set(cr)),
            "CPL_minus_BOM": sorted(set(cr) - set(br)),
            "duplicates": [r for r, n in collections.Counter(br).items() if n > 1],
            "excluded_native_features": sorted(missing),
            "cpl_position_side_rotation_errors": cpl_errors,
        },
        "fill_schedule_count": len(fill),
        "fill_schedule_errors": fill_errors,
        "zip_vs_loose_CAM_mismatches": zcheck,
        "silk_text_count": len(legend),
        "silk_min_height": min((t["height"] for t in legend), default=None),
        "silk_min_thickness": min((t["thickness"] for t in legend), default=None),
    }
    if not report["source_unchanged"]:
        raise RuntimeError(
            "Saved project changed during board audit; discard the result"
        )
    geometry = {
        "tracks": tr,
        "vias": vs,
        "pads": pd,
        "legend": legend,
        "footprints": {
            r: {
                "value": f.GetValue(),
                "at": xy(f.GetPosition()),
                "side": b.GetLayerName(f.GetLayer()),
                "rotation": f.GetOrientationDegrees(),
                "id": str(f.GetFPID()),
            }
            for r, f in fs.items()
        },
    }
    geometry_bytes = json.dumps(geometry, indent=2).encode("utf-8")
    report["geometry_sha256"] = hashlib.sha256(geometry_bytes).hexdigest()
    report_bytes = json.dumps(report, indent=2).encode("utf-8")
    created = []
    try:
        for target, data in (
            (geometry_path, geometry_bytes),
            (report_path, report_bytes),
        ):
            with target.open("xb") as handle:
                created.append((target, data))
                handle.write(data)
        if before != native_hashes():
            raise RuntimeError(
                "Saved project changed during board audit; discard the result"
            )
    except BaseException:
        for target, data in created:
            if target.is_file() and target.read_bytes() == data:
                target.unlink()
        raise
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in ["key_nets", "groups", "netclasses", "source_sha256"]
            },
            indent=2,
        )
    )
    print("KEY NETS:")
    for n, d in nets.items():
        print(n, d["width_lengths_mm"], d["layers"], "vias", len(d["vias"]))


if __name__ == "__main__":
    main()
