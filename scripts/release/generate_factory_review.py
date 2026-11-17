"""Build a fresh, count-independent manufacturing review set from native CAD.

Run with KiCad Python. Writes only to a new external output folder. All ERC,
DRC, parity and connectivity checks are mandatory. No production release.
"""

import argparse
import collections
import datetime
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from core.paths import asset_root, data_path, default_project, library_directories

import pcbnew

from core.export_helpers import (
    BOM_FIELDS,
    CPL_FIELDS,
    FILES,
    REGISTER_FIELDS,
    assert_current_inputs,
    exact_metadata,
    factory_input_dir,
    feature_kind,
    group_bom,
    natural,
    readcsv,
    require,
    sha,
    writecsv,
    writejson,
)
from core.factory_scope import export_factory_scope
from core.pcb_io import copper_width


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=Path, default=default_project(__file__))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cli", type=Path)
    a = ap.parse_args()
    P = a.project.resolve()
    OUT = a.out.resolve()
    M = OUT / "jlcpcb"
    require(not OUT.is_relative_to(P), "Output must be outside source project")
    require(
        not OUT.exists() or not any(OUT.iterdir()),
        "Output must be new/empty to prevent stale files",
    )
    control = factory_input_dir(P)
    OUT.mkdir(parents=True, exist_ok=True)
    for n in ["gerbers", "assembly", "evidence", "sourcing", "views"]:
        (M / n).mkdir(parents=True, exist_ok=True)
    CLI = str((a.cli or Path(sys.executable).with_name("kicad-cli.exe")).resolve())
    require(
        Path(CLI).is_file(), "Pass --cli for the KiCad CLI installed with this Python"
    )
    boards = list(P.glob("*.kicad_pcb"))
    schematics = list(P.glob("*.kicad_sch"))
    require(
        len(boards) == len(schematics) == 1,
        "Expected one native board and one single-sheet schematic",
    )
    boardpath = boards[0]
    schpath = schematics[0]
    stem = boardpath.stem
    source = {
        p.name: sha(p)
        for p in P.glob("*.kicad_*")
        if p.suffix in [".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_dru"]
    }
    propath = P / (stem + ".kicad_pro")
    erc_settings = (
        json.loads(propath.read_text(encoding="utf-8-sig")).get("erc", {})
        if propath.exists()
        else {}
    )
    require(
        not erc_settings.get("erc_exclusions", []),
        "ERC exclusions must be explicitly resolved before export",
    )
    erc_ignored = sorted(
        k for k, v in erc_settings.get("rule_severities", {}).items() if v == "ignore"
    )
    writejson(
        M / "evidence/ERC_RULE_CONFIGURATION.json",
        {
            "source_sha256": source,
            "rule_severities": erc_settings.get("rule_severities", {}),
            "ignored_categories": erc_ignored,
            "individual_exclusions": erc_settings.get("erc_exclusions", []),
            "interpretation": (
                "ERC executes every configured rule; any listed ignored "
                "categories are disclosed and are not claimed checked"
            ),
        },
    )
    support = asset_root(P)
    inputs = [
        p
        for directory in (*library_directories(P), support / "scripts", data_path(P))
        for p in directory.rglob("*")
        if p.is_file()
        and p.name.lower() not in ("desktop.ini", "thumbs.db", ".ds_store")
        and "__pycache__" not in p.parts
    ]
    inputs += [p for p in [P / "sym-lib-table", P / "fp-lib-table"] if p.exists()]
    featurepath = control / "PCB_FEATURES_NO_PURCHASE.csv"
    deps = {p.relative_to(P).as_posix(): sha(p) for p in inputs}
    writejson(
        OUT / "INPUT_SNAPSHOT.json",
        {"source_sha256": source, "dependency_sha256": deps},
    )
    commands = []

    def run(argv):
        r = subprocess.run(
            [CLI, *map(str, argv)],
            capture_output=True,
            text=True,
            encoding="utf8",
            errors="replace",
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        commands.append(
            {
                "command": [CLI, *map(str, argv)],
                "returncode": r.returncode,
                "stdout": r.stdout,
                "stderr": r.stderr,
            }
        )
        writejson(M / "evidence/TOOL_RUNS.json", commands)
        require(r.returncode == 0, "KiCad failed: " + str(argv) + "\n" + r.stderr)
        return r.stdout.strip()

    version = run(["version"])
    run(["sch", "erc", "--format", "json", "-o", M / "evidence/ERC.json", schpath])
    run(
        [
            "pcb",
            "drc",
            "--format",
            "json",
            "--schematic-parity",
            "--all-track-errors",
            "-o",
            M / "evidence/DRC.json",
            boardpath,
        ]
    )
    erc = json.loads((M / "evidence/ERC.json").read_text())
    drc = json.loads((M / "evidence/DRC.json").read_text())
    require(
        not [v for s in erc.get("sheets", []) for v in s.get("violations", [])],
        "ERC findings; no factory package generated",
    )
    for key in [
        "violations",
        "unconnected_items",
        "schematic_parity",
        "ignored_checks",
    ]:
        require(key in drc, f"DRC report missing required {key} evidence")
        require(not drc[key], f"DRC {key} is not empty")
    run(
        [
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadxml",
            "-o",
            M / "evidence/NETLIST.xml",
            schpath,
        ]
    )
    b = pcbnew.LoadBoard(str(boardpath))
    layer_limit = data_path(P, "BOARD_LAYER_LIMIT.json")
    if layer_limit.exists():
        maximum = int(
            json.loads(layer_limit.read_text(encoding="utf8"))["maximum_copper_layers"]
        )
        require(
            b.GetCopperLayerCount() <= maximum,
            (
                f"Board has {b.GetCopperLayerCount()} copper layers; owner "
                f"limit is {maximum}. No manufacturing export."
            ),
        )
    fl = list(b.GetFootprints())
    fs = {f.GetReference(): f for f in fl}
    require(len(fs) == len(fl), "Duplicate PCB references")
    root = ET.parse(M / "evidence/NETLIST.xml").getroot()
    cl = root.findall("./components/comp")
    comps = {c.get("ref"): c for c in cl}
    require(len(comps) == len(cl), "Duplicate schematic references")
    regpath = control / "FALLBACK_EXACT_PART_REGISTER.csv"
    old = {r["Designator"]: r for r in readcsv(regpath)}
    modpath = control / "07_REQUIRED_MODULES_NOT_DNP.csv"
    mods = readcsv(modpath)
    module_cfg = {r["Designator"]: r for r in mods}
    known_features = (
        {r["Designator"]: r for r in readcsv(featurepath)}
        if featurepath.exists()
        else {}
    )
    parts = []
    features = []
    provenance = []
    native_flags = []
    module_rows = []
    native_fields_by_ref = {}
    for ref in sorted(set(fs) | set(comps), key=natural):
        f = fs.get(ref)
        c = comps.get(ref)
        require(f is not None, f"{ref}: schematic has no placed PCB footprint")
        fields = (
            {e.get("name"): e.text or "" for e in c.findall("./fields/field")}
            if c is not None
            else {e.GetName(): e.GetText() for e in f.GetFields()}
        )
        fp = str(f.GetFPID().GetLibItemName())
        kind = feature_kind(ref, fp, fields, known_features)
        if kind:
            require(
                ref in known_features
                or (f.IsExcludedFromBOM() and f.IsExcludedFromPosFiles()),
                (
                    f"{ref}: new fabricated feature requires explicit "
                    f"classification, not silent omission"
                ),
            )
            features.append(
                {
                    "Designator": ref,
                    "Type": kind,
                    "Procurement": "Fabricated feature; no loose part; not an assembly omission",
                }
            )
            continue
        require(
            c is not None, f"{ref}: nonfabrication PCB footprint absent from schematic"
        )
        require(
            c.findtext("value") == f.GetValue(), f"{ref}: schematic/PCB value mismatch"
        )
        require(
            c.findtext("footprint", "").split(":")[-1] == fp,
            f"{ref}: schematic/PCB footprint mismatch",
        )
        meta = exact_metadata(fields, old.get(ref, {}), ref, provenance)
        boardfields = {e.GetName(): e.GetText() for e in f.GetFields()}
        for k in ["MPN", "Manufacturer"]:
            if k in boardfields:
                require(
                    boardfields[k] == meta[k],
                    f"{ref}: PCB {k} does not match selected schematic identity",
                )
        ismodule = (
            ref in module_cfg
            or "plug-in module" in fields.get("Assembly_Service", "").lower()
        )
        native_flags.append(
            dict(
                Designator=ref,
                DNP=f.IsDNP(),
                Native_exclude_BOM=f.IsExcludedFromBOM(),
                Native_exclude_positions=f.IsExcludedFromPosFiles(),
                Included=True,
                Category="Required module" if ismodule else "Required board part",
            )
        )
        require(
            not f.IsDNP(),
            (
                f"{ref}: native DNP flag conflicts with required assembly; "
                f"resolve explicitly, never silently omit"
            ),
        )
        native_fields_by_ref[ref] = fields
        part = dict(
            Designator=ref,
            Comment=c.findtext("value"),
            Footprint=fp,
            **meta,
            Quantity="1",
            Sourcing=fields.get(
                "Procurement",
                "Exact identity; inventory and factory acceptance unverified",
            ),
            Assembly=fields.get("Assembly", "Required; never auto-DNP"),
            Side="Bottom" if f.GetLayer() == pcbnew.B_Cu else "Top",
        )
        parts.append(part)
        if ismodule:
            cfg = module_cfg.get(ref, {})
            require(
                cfg.get("Quantity", "1") == "1",
                f"{ref}: each native module reference represents one placement",
            )
            for k in ["MPN", "Manufacturer"]:
                if cfg.get(k):
                    require(
                        cfg[k] == meta[k],
                        f"{ref}: owner-stock/module preparation {k} conflicts with schematic",
                    )
            module_rows.append(
                dict(
                    Designator=ref,
                    Manufacturer=meta["Manufacturer"],
                    MPN=meta["MPN"],
                    Quantity="1",
                    Status=cfg.get(
                        "Status",
                        "REQUIRED MODULE; exact preparation/ownership must be confirmed; not DNP",
                    ),
                    **{
                        "Factory work": cfg.get(
                            "Factory work", fields.get("Assembly", "")
                        )
                    },
                )
            )
    require(
        set(module_cfg).issubset({r["Designator"] for r in parts}),
        "A required module vanished from native CAD",
    )
    require(
        set(fs) == {r["Designator"] for r in parts + features},
        "Unclassified footprint / silent omission",
    )
    require(bool(parts), "No purchased parts or modules found")
    module_refs = {r["Designator"] for r in module_rows}
    pr = [r for r in parts if r["Designator"] not in module_refs]
    bom = group_bom(parts)
    writecsv(M / FILES["register"], pr, REGISTER_FIELDS)
    writecsv(M / FILES["bom"], bom, BOM_FIELDS)
    writecsv(
        M / "07_REQUIRED_MODULES_NOT_DNP.csv",
        module_rows,
        ["Designator", "Manufacturer", "MPN", "Quantity", "Status", "Factory work"],
    )
    writecsv(
        M / "assembly/PCB_FEATURES_NO_PURCHASE.csv",
        features,
        ["Designator", "Type", "Procurement"],
    )
    writecsv(
        M / "evidence/METADATA_PROVENANCE.csv",
        provenance,
        ["Designator", "Field", "Value", "Source", "Previous"],
    )
    writecsv(
        M / "evidence/NATIVE_ASSEMBLY_FLAGS.csv",
        native_flags,
        [
            "Designator",
            "DNP",
            "Native_exclude_BOM",
            "Native_exclude_positions",
            "Included",
            "Category",
        ],
    )
    writejson(
        M / "evidence/PART_MEMBERSHIP.json",
        {
            "source_sha256": source,
            "required_refs": [r["Designator"] for r in parts],
            "required_module_refs": sorted(module_refs, key=natural),
            "fabricated_feature_refs": [r["Designator"] for r in features],
            "legacy_register_refs_not_in_current_board_parts": sorted(
                set(old) - {r["Designator"] for r in pr}, key=natural
            ),
            "membership_basis": (
                "Native schematic plus placed board, explicit fabricated "
                "features, required module declarations; no hardcoded "
                "count or new-reference allowlist"
            ),
        },
    )
    o = b.GetDesignSettings().GetAuxOrigin()
    ox = o.x / 1e6
    oy = o.y / 1e6
    run(
        [
            "pcb",
            "export",
            "pos",
            "--format",
            "csv",
            "--units",
            "mm",
            "--side",
            "both",
            "--use-drill-file-origin",
            "-o",
            M / "assembly/NATIVE_ANCHOR_POSITIONS.csv",
            boardpath,
        ]
    )
    native = {r["Ref"]: r for r in readcsv(M / "assembly/NATIVE_ANCHOR_POSITIONS.csv")}

    def center(f):
        layer = pcbnew.B_Fab if f.GetLayer() == pcbnew.B_Cu else pcbnew.F_Fab
        gg = [
            g.GetBoundingBox()
            for g in f.GraphicalItems()
            if isinstance(g, pcbnew.PCB_SHAPE) and g.GetLayer() == layer
        ]
        if gg:
            return (
                (min(g.GetX() for g in gg) + max(g.GetRight() for g in gg)) / 2e6,
                (min(g.GetY() for g in gg) + max(g.GetBottom() for g in gg)) / 2e6,
                "Native Fab geometric envelope; factory orientation/centroid review required",
            )
        return (
            f.GetPosition().x / 1e6,
            f.GetPosition().y / 1e6,
            "Native footprint anchor; no Fab envelope, factory centroid review required",
        )

    cpl = []
    offsets = []
    for r in parts:
        ref = r["Designator"]
        f = fs[ref]
        cx, cy, method = center(f)
        pos = f.GetPosition()
        angle = (
            float(native[ref]["Rot"]) if ref in native else f.GetOrientationDegrees()
        )
        cpl.append(
            dict(
                Designator=ref,
                **{"Mid X": f"{cx - ox:.6f}", "Mid Y": f"{oy - cy:.6f}"},
                Rotation=f"{angle % 360:.6f}",
                Layer=r["Side"],
            )
        )
        offsets.append(
            {
                "Designator": ref,
                "Native anchor X mm": pos.x / 1e6 - ox,
                "Native anchor Y mm": oy - pos.y / 1e6,
                "Body centre X mm": cx - ox,
                "Body centre Y mm": oy - cy,
                "Centre method": method,
                "Rotation basis": "Native KiCad CCW; common top-view datum",
            }
        )
    require(
        sum(r["Quantity"] for r in bom) == len(cpl) == len(parts),
        "BOM/CPL quantity mismatch",
    )
    require(
        {r["Designator"] for r in cpl}
        == {r for row in bom for r in row["Designator"].split(",")},
        "BOM/CPL reference mismatch",
    )
    writecsv(M / FILES["cpl"], cpl, CPL_FIELDS)
    writecsv(M / "assembly/COMPONENT_CENTRE_OFFSETS.csv", offsets, list(offsets[0]))
    for side in ["Top", "Bottom"]:
        writecsv(
            M / f"assembly/04_CPL_{side.upper()}.csv",
            [r for r in cpl if r["Layer"] == side],
            CPL_FIELDS,
        )
    scope = export_factory_scope(
        P, M, parts, cpl, fs, module_refs, native_fields_by_ref
    )
    owner_finish = {r["Designator"]: r for r in readcsv(M / "07_OWNER_FINISHING.csv")}
    for row in module_rows:
        if row["Designator"] in owner_finish:
            finish = owner_finish[row["Designator"]]
            row["Status"] = (
                "REQUIRED OWNER STOCK; OWNER_INSTALL; excluded only from "
                "JLC carrier BOM/CPL; never DNP. "
            ) + finish["Acceptance status"]
            row["Factory work"] = (
                "Outside JLC carrier assembly scope. " + finish["Required finishing"]
            )
    writecsv(
        M / "07_REQUIRED_MODULES_NOT_DNP.csv",
        module_rows,
        ["Designator", "Manufacturer", "MPN", "Quantity", "Status", "Factory work"],
    )

    # Export all enabled copper, not a fixed number of layers.
    copper = [
        b.GetLayerName(i) for i in b.GetEnabledLayers().Seq() if pcbnew.IsCopperLayer(i)
    ]
    copper = sorted(
        copper,
        key=lambda n: (
            0
            if n == "F.Cu"
            else 100
            if n == "B.Cu"
            else int(re.search(r"\d+", n).group())
        ),
    )
    layers = copper + [
        "F.Mask",
        "B.Mask",
        "F.SilkS",
        "B.SilkS",
        "F.Paste",
        "B.Paste",
        "Edge.Cuts",
    ]
    run(
        [
            "pcb",
            "export",
            "gerbers",
            "--layers",
            ",".join(layers),
            "--use-drill-file-origin",
            "--no-protel-ext",
            "--subtract-soldermask",
            "--check-zones",
            "-o",
            str(M / "gerbers") + os.sep,
            boardpath,
        ]
    )
    run(
        [
            "pcb",
            "export",
            "drill",
            "--format",
            "excellon",
            "--drill-origin",
            "plot",
            "--excellon-zeros-format",
            "decimal",
            "--excellon-units",
            "mm",
            "--excellon-separate-th",
            "--generate-map",
            "--map-format",
            "pdf",
            "--generate-report",
            "--report-path",
            M / "assembly/DRILL_REPORT.txt",
            "-o",
            str(M / "gerbers") + os.sep,
            boardpath,
        ]
    )
    for side, ls in [
        ("TOP", "F.Fab,F.SilkS,Edge.Cuts"),
        ("BOTTOM", "B.Fab,B.SilkS,Edge.Cuts"),
    ]:
        argv = [
            "pcb",
            "export",
            "pdf",
            "--mode-single",
            "--black-and-white",
            "--sketch-pads-on-fab-layers",
            "--layers",
            ls,
        ]
        if side == "BOTTOM":
            argv += ["--mirror"]
        run(argv + ["-o", M / f"assembly/ASSEMBLY_{side}.pdf", boardpath])
    run(["sch", "export", "pdf", "-o", M / "assembly/SCHEMATIC.pdf", schpath])
    run(
        [
            "pcb",
            "export",
            "ipcd356",
            "-o",
            OUT / "IPC_NATIVE_UNNORMALIZED_NOT_FOR_FACTORY.txt",
            boardpath,
        ]
    )
    vias = [t for t in b.GetTracks() if isinstance(t, pcbnew.PCB_VIA)]
    tracks = [t for t in b.GetTracks() if not isinstance(t, pcbnew.PCB_VIA)]
    filled = []
    for v in vias:
        if v.GetFillingMode() == pcbnew.FILLING_MODE_FILLED:
            q = v.GetPosition()
            filled.append(
                {
                    "UUID": v.m_Uuid.AsString(),
                    "X mm from datum": q.x / 1e6 - ox,
                    "Y mm from datum": oy - q.y / 1e6,
                    "Land mm": copper_width(v, layer=pcbnew.F_Cu) / 1e6,
                    "Drill mm": v.GetDrill() / 1e6,
                    "Net": v.GetNetname(),
                    "Process": (
                        "Epoxy fill and copper cap; retain every connector and "
                        "module insertion hole"
                    ),
                }
            )
    writecsv(
        M / "assembly/EXPLICIT_FILLED_CAPPED_VIAS.csv",
        filled,
        [
            "UUID",
            "X mm from datum",
            "Y mm from datum",
            "Land mm",
            "Drill mm",
            "Net",
            "Process",
        ],
    )
    pins = {
        (x.get("ref"), x.get("pin")): n.get("name")
        for n in root.findall("./nets/net")
        for x in n.findall("node")
    }
    ports = readcsv(control / "09_CONNECTOR_PINOUTS.csv")
    port_refs = {r["Connector"] for r in ports}
    require(
        port_refs == {r for r in fs if re.fullmatch(r"J\d+", r)},
        "Connector schedule membership changed; review new/removed connectors explicitly",
    )
    libparts = {
        (x.get("lib"), x.get("part")): x for x in root.findall("./libparts/libpart")
    }
    required_contacts = set()
    for ref in port_refs:
        require(ref in comps, "Connector absent from schematic: " + ref)
        libsrc = comps[ref].find("libsource")
        lib = libparts[(libsrc.get("lib"), libsrc.get("part"))]
        required_contacts.update((ref, x.get("num")) for x in lib.findall("./pins/pin"))
        required_contacts.update(
            (ref, x.GetNumber()) for x in fs[ref].Pads() if x.GetNumber()
        )
    require(
        required_contacts == {(r["Connector"], r["Cavity"]) for r in ports},
        "Connector cavity membership changed; review harness contract before export",
    )
    for r in ports:
        require(r["Connector"] in comps, "Connector contract reference missing")
        key = (r["Connector"], r["Cavity"])
        r["Net"] = pins.get(key, "")
        r["Use"] = (
            "Not connected"
            if not r["Net"] or r["Net"].startswith("unconnected-")
            else "Connected"
        )
        r["Header MPN"] = next(
            q["MPN"] for q in parts if q["Designator"] == r["Connector"]
        )
    writecsv(M / "09_CONNECTOR_PINOUTS.csv", ports, list(ports[0]))
    schedule = readcsv(control / "10_FINISHED_CABLE_SCHEDULE.csv")
    require(
        {r["Connector"] for r in schedule} == port_refs,
        "Cable schedule membership differs from connectors",
    )
    for row in schedule:
        ref = row["Connector"]
        part = next(q for q in parts if q["Designator"] == ref)
        require(
            row["Header MPN"] == part["MPN"],
            f"{ref}: cable mating contract belongs to another header MPN",
        )
        row["Role"] = part["Comment"]
    # Procurement, length, and termination remain controlled inputs;
    # they are not inferred from connectivity.
    writecsv(M / "10_FINISHED_CABLE_SCHEDULE.csv", schedule, list(schedule[0]))
    writecsv(
        M / "assembly/TEENSY_SOCKET_PINMAP.csv",
        [r for r in ports if r["Connector"] in ["J16", "J17"]],
        list(ports[0]),
    )
    historical = []
    for p in (P / "deliverables/jlcpcb/evidence").rglob("*"):
        if not p.is_file() or p.name.lower() in (
            "desktop.ini",
            "thumbs.db",
            ".ds_store",
        ):
            continue
        historic_source = None
        if p.suffix == ".json":
            try:
                historic_source = json.loads(p.read_text(encoding="utf-8-sig")).get(
                    "source_sha256"
                )
            except (ValueError, AttributeError):
                pass
        historical.append(
            {
                "path": p.relative_to(P).as_posix(),
                "sha256": sha(p),
                "recorded_source_sha256": historic_source,
                "status": (
                    "HISTORICAL NOT REUSED; rerun separately against this "
                    "source before current-qualification claims"
                ),
            }
        )
    writejson(M / "evidence/HISTORICAL_EVIDENCE_INDEX.json", historical)
    bbox = b.GetBoardEdgesBoundingBox()
    outline = pcbnew.SHAPE_POLY_SET()
    require(b.GetBoardPolygonOutlines(outline, False), "Open or invalid board outline")
    centreline_box = outline.BBox()
    outline_mm = [centreline_box.GetWidth() / 1e6, centreline_box.GetHeight() / 1e6]
    require(all(v > 0 for v in outline_mm), "Empty board outline")
    assembly_categories = collections.Counter()
    for part in parts:
        f = fs[part["Designator"]]
        kind = (
            "Required module"
            if part["Designator"] in module_refs
            else "THT/mixed"
            if any(p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH for p in f.Pads())
            else "SMT"
        )
        assembly_categories[part["Side"] + " " + kind] += 1
    identity = {
        "revision": stem,
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_sha256": source,
        "dependency_sha256": deps,
        "source_project": str(P),
        "kicad_cli": CLI,
        "kicad_version": version,
        "readiness_level": "FACTORY_ENGINEERING_REVIEW_ONLY",
        "files": FILES,
        "board_components": len(pr),
        "required_plugin_modules": len(module_rows),
        "bom_group_rows": len(bom),
        "cpl_rows": len(cpl),
        "placements_by_side": dict(collections.Counter(r["Layer"] for r in cpl)),
        "assembly_categories": dict(assembly_categories),
        "fabricated_feature_types": dict(
            collections.Counter(r["Type"] for r in features)
        ),
        "footprints": len(fs),
        "fabricated_features": len(features),
        "tracks": len(tracks),
        "vias": len(vias),
        "selectively_filled_capped_vias": len(filled),
        "minimum_via_drill_mm": min((v.GetDrill() / 1e6 for v in vias), default=None),
        "maximum_filled_via_drill_mm": max(
            (r["Drill mm"] for r in filled), default=None
        ),
        "connector_contacts": len(ports),
        "pin_net_entries": len(pins),
        "nets": len(root.findall("./nets/net")),
        "native_erc_violations": 0,
        "native_drc_violations": 0,
        "unconnected_items": 0,
        "schematic_parity": 0,
        "ignored_check_categories": erc_ignored,
        "ignored_DRC_categories": [],
        "copper_layers": len(copper),
        "exported_gerber_layers": layers,
        "edge_cuts_bbox_mm": [bbox.GetWidth() / 1e6, bbox.GetHeight() / 1e6],
        "factory_order_released": False,
        "stock_reserved": False,
        "physically_tested": False,
        "datum_mm": [ox, oy],
        "datum": "X right, Y up, common unmirrored top-view CPL coordinates",
        "exporter_script_sha256": {
            p.name: sha(p) for p in Path(__file__).parent.glob("*.py")
        },
        "current_evidence_limit": (
            "Native rule checks and export consistency only; prior "
            "independent/electrical/visual audits are historical until "
            "rerun"
        ),
    }
    identity.update(edge_cuts_centerline_bbox_mm=outline_mm)
    identity.update(
        factory_cpl_rows=scope["factory_parts"],
        factory_bom_group_rows=scope["factory_bom_groups"],
        owner_install_rows=scope["owner_install_parts"],
        factory_upload_files={"bom": "02_BOM_JLCPCB.csv", "cpl": "03_CPL_JLCPCB.csv"},
        assembly_policy_sha256=scope["policy_sha256"],
    )
    identity.update(
        assembly_rotation_policy_sha256=scope["rotation_policy_sha256"],
        assembly_rotation_offset_count=len(scope["qualified_catalog_rotation_offsets"]),
    )
    writejson(M / "SOURCE_IDENTITY.json", identity)
    from release.normalize_ipc_netlist import normalize as normalize_ipc

    normalize_ipc(P, OUT)
    from core.fabrication_supplements import apply_fabrication_supplements

    apply_fabrication_supplements(P, M)
    from release.generate_factory_documents import generate as generate_factory_docs

    generate_factory_docs(P, OUT)
    assert_current_inputs(P, source, deps)
    evidence_files = {
        p.relative_to(M).as_posix(): sha(p)
        for p in (M / "evidence").rglob("*")
        if p.is_file()
        and p.name.lower() not in ("desktop.ini", "thumbs.db", ".ds_store")
    }
    writejson(
        M / "evidence/CURRENT_EVIDENCE_BINDING.json",
        {
            "source_sha256": source,
            "dependency_sha256": deps,
            "files": evidence_files,
            "inputs_unchanged_through_export": True,
        },
    )
    manifest = {
        p.relative_to(M).as_posix(): sha(p)
        for p in M.rglob("*")
        if p.is_file()
        and p.name.lower() not in ("desktop.ini", "thumbs.db", ".ds_store")
    }
    writejson(M / "MANIFEST_SHA256.json", {"source_sha256": source, "files": manifest})
    writejson(OUT / "candidate_identity.json", identity)
    print(
        json.dumps(
            {
                k: identity[k]
                for k in [
                    "board_components",
                    "required_plugin_modules",
                    "bom_group_rows",
                    "cpl_rows",
                    "fabricated_features",
                    "copper_layers",
                    "kicad_version",
                ]
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
