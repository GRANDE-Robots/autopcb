"""Validate/normalize the installed exporter's IPC-D-356 output against native CAD.

The installed exporter leaves invalid via soldermask integers and truncates names.
Only masks and collision-free signal aliases are corrected; native test geometry
is retained and independently checked. This does not modify the board.
"""

import collections
import csv
import hashlib
import json
import re
from pathlib import Path

import pcbnew

from core.export_helpers import require, stable_zip, writecsv, writejson
from core.pcb_io import copper_width


def normalize(project, output):
    require(__debug__, "IPC validator must run with assertions enabled")
    PROJECT = Path(project)
    OUT = Path(output)
    M = OUT / "jlcpcb"

    def sha(p):
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()

    identity = json.loads((M / "SOURCE_IDENTITY.json").read_text())
    source = identity["source_sha256"]
    stem = identity["revision"]
    path = M / "gerbers" / f"{stem}.ipc"
    legacy_path = M / "gerbers" / f"{stem}.d356"
    rawpath = OUT / "IPC_NATIVE_UNNORMALIZED_NOT_FOR_FACTORY.txt"
    if not rawpath.exists():
        rawpath.write_bytes(legacy_path.read_bytes())
    raw = rawpath.read_text().splitlines()
    b = pcbnew.LoadBoard(str(PROJECT / (stem + ".kicad_pcb")))
    fs = {f.GetReference(): f for f in b.GetFootprints()}
    o = b.GetDesignSettings().GetAuxOrigin()
    ox, oy = o.x / 1e6, o.y / 1e6
    vias = [v for v in b.GetTracks() if isinstance(v, pcbnew.PCB_VIA)]
    records = []
    matched_pads = set()
    matched_vias = set()
    bad_masks = 0
    long_pins = []
    for i, line in enumerate(raw):
        if line[:3] not in ["317", "327", "367"]:
            continue
        assert len(line) >= 73
        ref = line[20:26].strip()
        pin = line[27:31].strip()
        m = re.search(
            r"A(\d{2})X([+-]\d{6})Y([+-]\d{6})X(\d{4})Y(\d{4})R(\d{3})S(-?\d+)$", line
        )
        assert m, (i, line)
        access, x, y, xsize, ysize, rot, oldmask = map(int, m.groups())
        px, py = x * 0.00254, y * 0.00254
        if ref == "VIA":
            candidates = [
                v
                for v in vias
                if abs(v.GetPosition().x / 1e6 - ox - px) <= 0.001271
                and abs(oy - v.GetPosition().y / 1e6 - py) <= 0.001271
            ]
            assert len(candidates) == 1, (i, px, py, candidates)
            item = candidates[0]
            mask = int(item.IsTented(pcbnew.F_Mask)) + 2 * int(
                item.IsTented(pcbnew.B_Mask)
            )
            assert item.m_Uuid.AsString() not in matched_vias
            matched_vias.add(item.m_Uuid.AsString())
            assert (
                access == 0
                and abs(xsize * 0.00254 - copper_width(item, layer=pcbnew.F_Cu) / 1e6)
                <= 0.001271
                and ysize == 0
            )
        else:
            assert ref in fs, (ref, line)
            candidates = [
                p
                for p in fs[ref].Pads()
                if p.GetNumber()[:4] == pin
                and abs(p.GetPosition().x / 1e6 - ox - px) <= 0.001271
                and abs(oy - p.GetPosition().y / 1e6 - py) <= 0.001271
            ]
            assert len(candidates) == 1, (ref, pin, px, py, len(candidates))
            item = candidates[0]
            mask = 3
            if item.GetLayerSet().Contains(pcbnew.F_Mask):
                mask &= ~1
            if item.GetLayerSet().Contains(pcbnew.B_Mask):
                mask &= ~2
            key = item.m_Uuid.AsString()
            assert key not in matched_pads
            matched_pads.add(key)
            assert oldmask == mask, (ref, pin, oldmask, mask)
            assert access in [0, 1, b.GetCopperLayerCount()]
            if len(item.GetNumber()) > 4:
                long_pins.append(
                    {
                        "Designator": ref,
                        "Native pad name": item.GetNumber(),
                        "IPC four-character pin": pin,
                        "X mm": px,
                        "Y mm": py,
                        "Native net": item.GetNetname(),
                        "Meaning": (
                            "IPC pin field is four characters; native full name "
                            "retained here. Location/net are unchanged."
                        ),
                    }
                )
        if oldmask not in range(4):
            bad_masks += 1
        net = item.GetNetname()
        assert net.isascii() and len(net) <= 58, net
        records.append(
            {
                "raw": line,
                "net": net,
                "ref": ref,
                "pin": pin,
                "mask": mask,
                "x": x,
                "y": y,
                "access": access,
            }
        )
    assert len(matched_vias) == len(vias)
    cu = [i for i in b.GetEnabledLayers().Seq() if pcbnew.IsCopperLayer(i)]
    conductivepads = {
        p.m_Uuid.AsString()
        for f in b.GetFootprints()
        for p in f.Pads()
        if any(p.GetLayerSet().Contains(layer) for layer in cu)
    }
    omitted = [
        {
            "ref": f.GetReference(),
            "pin": p.GetNumber(),
            "net": p.GetNetname(),
            "code": p.GetNetCode(),
            "attribute": int(p.GetAttribute()),
            "on_copper": any(p.GetLayerSet().Contains(layer) for layer in cu),
        }
        for f in b.GetFootprints()
        for p in f.Pads()
        if p.m_Uuid.AsString() not in matched_pads
    ]
    (OUT / "IPC_NATIVE_OMITTED_PADS.json").write_text(json.dumps(omitted, indent=2))
    assert matched_pads == conductivepads, (
        len(matched_pads),
        len(conductivepads),
        len(conductivepads - matched_pads),
    )
    assert all(not p["on_copper"] for p in omitted), (
        "Native IPC export omitted copper contacts"
    )
    assert 0 <= bad_masks <= len(vias)
    nets = sorted({r["net"] for r in records if r["net"]})
    aliases = {s: f"NNAME{i + 1:04d}" for i, s in enumerate(nets)}
    assert len(set(aliases.values())) == len(nets)
    lines = [
        (
            "C  AutoPCB - validated native geometry; explicit net aliases"
        ).ljust(80),
        ("C  PCB SHA256 " + source[stem + ".kicad_pcb"]).ljust(80),
        "P  CODE 00".ljust(80),
        "P  UNITS CUST 0".ljust(80),
        "P  arrayDim   N".ljust(80),
    ]
    for net, alias in aliases.items():
        # Conventional P NNAME<n> parameters; the full alias is used in test records.
        lines.append(f"P  {alias} {net}".ljust(80))
    for r in sorted(
        records,
        key=lambda r: (
            aliases.get(r["net"], "N/C"),
            r["ref"],
            r["pin"],
            r["x"],
            r["y"],
        ),
    ):
        line = r["raw"]
        label = aliases.get(r["net"], "N/C")
        core = line[:3] + f"{label:<14}" + line[17 : line.rfind("S")]
        # Column 72 is blank; S and its one-digit mask occupy columns 73-74.
        assert len(core) == 71, (len(core), line)
        normalized = core + f" S{r['mask']}" + 6 * " "
        assert len(normalized) == 80
        lines.append(normalized)
    lines.append("999".ljust(80))
    assert all(len(s) == 80 and s.isascii() for s in lines)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")

    # Read back the actual bytes using a separate fixed-column parser.
    final = path.read_text().splitlines()
    defs = {
        s.split(maxsplit=2)[1]: s.split(maxsplit=2)[2].strip()
        for s in final
        if s.startswith("P  NNAME")
    }
    assert defs == {v: k for k, v in aliases.items()}
    check = [s for s in final if s[:3] in ["317", "327", "367"]]
    assert len(check) == len(records)
    assert all(
        s[71] == " " and s[72] == "S" and s[73] in "0123" and s[74:] == " " * 6
        for s in check
    )
    oldtuples = collections.Counter(
        (r["ref"], r["pin"], r["x"], r["y"], r["net"], r["access"]) for r in records
    )
    newtuples = collections.Counter(
        (
            s[20:26].strip(),
            s[27:31].strip(),
            int(s[42:49]),
            int(s[50:57]),
            defs.get(s[3:17].strip(), ""),
            int(s[39:41]),
        )
        for s in check
    )
    assert oldtuples == newtuples
    (OUT / "IPC_EXPECTED_TESTPOINTS.json").write_text(
        json.dumps(
            [
                {
                    "ref": r["ref"],
                    "pin": r["pin"],
                    "x_decimil": r["x"],
                    "y_decimil": r["y"],
                    "net": r["net"],
                    "access": r["access"],
                    "mask": r["mask"],
                }
                for r in records
            ],
            indent=2,
        )
    )
    drills = []
    for f in b.GetFootprints():
        for p in f.Pads():
            size = p.GetDrillSize()
            if not size.x or not size.y:
                continue
            q = p.GetPosition()
            drills.append(
                {
                    "kind": "NPTH"
                    if p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH
                    else "PTH",
                    "reference": f.GetReference(),
                    "pad": p.GetNumber(),
                    "uuid": p.m_Uuid.AsString(),
                    "net": p.GetNetname(),
                    "x_mm": q.x / 1e6 - ox,
                    "y_mm": oy - q.y / 1e6,
                    "drill_local_x_mm": size.x / 1e6,
                    "drill_local_y_mm": size.y / 1e6,
                    "diameter_mm": min(size.x, size.y) / 1e6,
                    "slot_travel_mm": abs(size.x - size.y) / 1e6,
                    "native_orientation_deg": p.GetOrientationDegrees(),
                    "filled_capped": False,
                    "purpose": "Insertion/mounting hole; do not fill",
                }
            )
    for v in vias:
        q = v.GetPosition()
        filled = v.GetFillingMode() == pcbnew.FILLING_MODE_FILLED
        drills.append(
            {
                "kind": "PTH",
                "reference": "VIA",
                "pad": "",
                "uuid": v.m_Uuid.AsString(),
                "net": v.GetNetname(),
                "x_mm": q.x / 1e6 - ox,
                "y_mm": oy - q.y / 1e6,
                "drill_local_x_mm": v.GetDrill() / 1e6,
                "drill_local_y_mm": v.GetDrill() / 1e6,
                "diameter_mm": v.GetDrill() / 1e6,
                "slot_travel_mm": 0,
                "native_orientation_deg": 0,
                "filled_capped": filled,
                "purpose": "Epoxy fill and copper cap"
                if filled
                else "Ordinary via; native mask tenting retained",
            }
        )
    drills.sort(
        key=lambda r: (r["kind"], r["reference"], r["pad"], r["x_mm"], r["y_mm"])
    )
    writecsv(
        M / "assembly/NATIVE_DRILL_AND_SLOT_SCHEDULE.csv",
        drills,
        [
            "kind",
            "reference",
            "pad",
            "uuid",
            "net",
            "x_mm",
            "y_mm",
            "drill_local_x_mm",
            "drill_local_y_mm",
            "diameter_mm",
            "slot_travel_mm",
            "native_orientation_deg",
            "filled_capped",
            "purpose",
        ],
    )
    drillmap = {
        "source_sha256": source,
        "datum_mm": [ox, oy],
        "coordinates": (
            "X right, Y up from native plot/drill datum; local drill "
            "sizes and orientations are native KiCad pad attributes"
        ),
        "slot_travel_definition": (
            "Distance between end-circle centres; full oval dimensions "
            "are drill_local_x_mm and drill_local_y_mm"
        ),
        "counts": {
            "holes": len(drills),
            "NPTH": sum(r["kind"] == "NPTH" for r in drills),
            "PTH": sum(r["kind"] == "PTH" for r in drills),
            "slots": sum(r["slot_travel_mm"] > 0 for r in drills),
            "filled_capped_vias": sum(r["filled_capped"] for r in drills),
        },
        "records": drills,
        "evidence_limit": (
            "Native drill inventory and explicit fill flags; "
            "independent Excellon/CAM and factory process review "
            "remain separate"
        ),
    }
    writejson(M / "evidence/NATIVE_DRILL_MAP.json", drillmap)
    writejson(OUT / "NATIVE_DRILL_MAP.json", drillmap)
    if legacy_path.exists():
        assert legacy_path.resolve().is_relative_to(OUT.resolve())
        legacy_path.unlink()
    if long_pins:
        with (M / "assembly/IPC_LONG_PAD_NAME_MAP.csv").open(
            "w", newline="", encoding="utf8"
        ) as h:
            w = csv.DictWriter(h, fieldnames=list(long_pins[0]))
            w.writeheader()
            w.writerows(long_pins)
    gerber_entries = [
        (p, p.name)
        for p in sorted((M / "gerbers").iterdir())
        if p.is_file() and p.suffix.lower() != ".pdf"
    ]
    assert all(
        p.suffix.lower() in [".gbr", ".drl", ".gbrjob", ".ipc"]
        for p, _ in gerber_entries
    )
    assert len([p for p, _ in gerber_entries if p.suffix == ".gbr"]) == len(
        identity["exported_gerber_layers"]
    )
    assert any(p.suffix == ".drl" for p, _ in gerber_entries) and any(
        p.suffix == ".ipc" for p, _ in gerber_entries
    )
    stable_zip(M / "01_PCB_GERBERS_DRILLS.zip", gerber_entries)
    receipt = {
        "source_sha256": source,
        "native_design_unchanged": True,
        "native_raw_export_sha256": sha(rawpath),
        "normalized_netlist_sha256": sha(path),
        "native_via_records_checked": len(matched_vias),
        "native_pad_records_checked": len(matched_pads),
        "total_test_records": len(records),
        "unique_connected_signal_aliases": len(nets),
        "invalid_via_mask_fields_corrected": bad_masks,
        "corrected_mask_basis": (
            "Native IsTented(F_Mask/B_Mask), matching current official KiCad exporter semantics"
        ),
        "signal_name_basis": (
            "Collision-free P NNAME aliases retain every full native name; no truncation"
        ),
        "record_width_columns": 80,
        "native_geometry_reference_pin_net_access_preserved": True,
        "IPC_four_character_pin_field_long_names_in_separate_map": long_pins,
        "coordinate_units": (
            f"0.1 mil (0.00254 mm) per IPC UNITS CUST 0; native plot/drill origin ({ox},{oy}) mm"
        ),
        "coordinate_quantization_error_mm_per_axis_max": 0.00127,
        "physical_or_factory_test_performed": False,
        "primary_format_reference": "https://docs.kicad.org/doxygen/export__d356_8cpp_source.html",
    }
    receipt["non_copper_pad_records_excluded"] = len(omitted)
    receipt["gerber_archive_members"] = [n for _, n in gerber_entries]
    (M / "evidence/IPC_D356_VALIDATION.json").write_text(
        json.dumps(receipt, indent=2) + "\n"
    )
    (OUT / "IPC_D356_VALIDATION.json").write_text(json.dumps(receipt, indent=2) + "\n")
    assert source == {n: sha(PROJECT / n) for n in source}
    print(
        json.dumps(
            {
                k: receipt[k]
                for k in [
                    "native_via_records_checked",
                    "native_pad_records_checked",
                    "total_test_records",
                    "unique_connected_signal_aliases",
                    "invalid_via_mask_fields_corrected",
                    "native_geometry_reference_pin_net_access_preserved",
                    "record_width_columns",
                ]
            },
            indent=2,
        )
    )
