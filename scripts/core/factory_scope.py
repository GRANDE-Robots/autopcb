"""Derive a complete system scope and an explicit JLCPCB assembly scope.

No CAD writes, purchases, substitutions or DNP changes. Exact purchasing identity
and a normalized native land-pattern fingerprint are both required to combine
BOM rows; descriptive values and historical footprint aliases are not identities.
"""

import argparse
import collections
import hashlib
import json
from pathlib import Path

from core.paths import data_path, support_input_name

from core.assembly_rotation import POLICY_FILE as ROTATION_POLICY_FILE
from core.assembly_rotation import SCHEDULE_FIELDS, AssemblyRotation
from core.export_helpers import (
    BOM_FIELDS,
    CPL_FIELDS,
    BomRows,
    readcsv,
    require,
    sha,
    writecsv,
    writejson,
)

FACTORY_BOM = "02_BOM_JLCPCB.csv"
FACTORY_CPL = "03_CPL_JLCPCB.csv"
OWNER_SCHEDULE = "07_OWNER_FINISHING.csv"
OWNER_FIELDS = [
    "Designator",
    "Manufacturer",
    "MPN",
    "Quantity",
    "Required",
    "Supply owner",
    "Assembly stage",
    "Required finishing",
    "Acceptance status",
]


def exact_identity(part):
    """Only harmless manufacturer whitespace/case and LCSC case are normalized."""
    manufacturer = " ".join(str(part["Manufacturer"]).split()).casefold()
    mpn = str(part["MPN"]).strip()
    lcsc = str(part.get("LCSC Part #", "")).strip().upper()
    require(
        manufacturer and mpn, "Missing exact purchasing identity: " + part["Designator"]
    )
    return manufacturer, mpn, lcsc


def partition(parts, policy, module_refs=(), native_fields=None):
    """A required owner-installed part is retained in the system, never DNP."""
    require(policy.get("schema") == 1, "Unsupported assembly policy schema")
    require(
        policy.get("all_system_parts_required") is True,
        "Policy must retain every system part",
    )
    refs = [p["Designator"] for p in parts]
    require(len(refs) == len(set(refs)), "Duplicate system reference")
    overrides = policy.get("overrides", {})
    require(
        set(overrides).issubset(refs),
        "Assembly-policy reference disappeared from native system",
    )
    require(
        set(module_refs).issubset(overrides),
        "Every required plug-in module needs an explicit assembly policy",
    )
    factory, owner, audit = [], [], []
    for part in parts:
        ref = part["Designator"]
        require(
            not part.get("DNP", False),
            ref + ": DNP cannot be used to partition a required system",
        )
        require(
            int(part.get("Quantity", 1)) == 1,
            ref + ": native part must represent one placement",
        )
        rule = {**policy.get("default", {}), **overrides.get(ref, {})}
        require(
            rule.get("required") is True,
            ref + ": explicit required disposition missing",
        )
        stage, supply = rule.get("assembly_stage"), rule.get("supply_owner")
        require(
            (stage, supply)
            in {("FACTORY_ASSEMBLY", "JLCPCB"), ("OWNER_INSTALL", "OWNER")},
            ref + ": unsupported assembly/supply disposition",
        )
        if ref in overrides:
            for key, field in [
                ("expected_mpn", "MPN"),
                ("expected_manufacturer", "Manufacturer"),
            ]:
                if key in rule:
                    require(
                        rule[key] == part[field],
                        ref + ": assembly policy belongs to a different " + field,
                    )
        native = (native_fields or {}).get(ref, {})
        for field, selected in [("Assembly_Stage", stage), ("Supply_Owner", supply)]:
            if field in native:
                require(
                    native[field] == selected,
                    ref + ": native " + field + " conflicts with assembly policy",
                )
        if stage == "OWNER_INSTALL":
            require(
                rule.get("required_finishing") and rule.get("acceptance_status"),
                ref + ": owner finishing and acceptance status must be explicit",
            )
            owner.append(
                {
                    "Designator": ref,
                    "Manufacturer": part["Manufacturer"],
                    "MPN": part["MPN"],
                    "Quantity": 1,
                    "Required": True,
                    "Supply owner": supply,
                    "Assembly stage": stage,
                    "Required finishing": rule["required_finishing"],
                    "Acceptance status": rule["acceptance_status"],
                }
            )
        else:
            factory.append(part)
        audit.append(
            {
                "Designator": ref,
                "Required": True,
                "Supply_owner": supply,
                "Assembly_stage": stage,
                "Native_DNP": False,
                "Policy_source": "override" if ref in overrides else "default",
            }
        )
    frefs, orefs = {p["Designator"] for p in factory}, {p["Designator"] for p in owner}
    require(
        not frefs & orefs and frefs | orefs == set(refs),
        "Required scope partition lost or duplicated a reference",
    )
    return factory, owner, audit


def consolidate(parts, geometry_by_ref):
    """Fail closed: neither alias names nor identical MPNs prove pad equivalence."""
    groups, identity_geometries = {}, collections.defaultdict(set)
    refs = [p["Designator"] for p in parts]
    require(len(refs) == len(set(refs)), "Duplicate factory reference")
    for part in parts:
        ref = part["Designator"]
        require(
            ref in geometry_by_ref and geometry_by_ref[ref],
            "Missing native footprint geometry: " + ref,
        )
        identity, geometry = exact_identity(part), geometry_by_ref[ref]
        groups.setdefault((*identity, geometry), []).append(part)
        identity_geometries[identity].add(geometry)
    rows, evidence = [], []
    for key, members in groups.items():
        members.sort(key=lambda p: BomRows.natural(p["Designator"]))
        first = members[0]
        row = {
            k: first[k]
            for k in BOM_FIELDS
            if k not in ("Comment", "Designator", "Quantity")
        }
        # A functional label such as LIDAR1/2_AUX is not an assembly identity.
        row.update(
            Comment=first["MPN"],
            Designator=",".join(p["Designator"] for p in members),
            Quantity=len(members),
        )
        rows.append(row)
        evidence.append(
            {
                "Designator": row["Designator"],
                "Quantity": len(members),
                "Manufacturer": first["Manufacturer"],
                "MPN": first["MPN"],
                "LCSC Part #": first.get("LCSC Part #", ""),
                "Native_geometry_sha256": key[-1],
                "Representative_footprint": first["Footprint"],
                "Equivalent_footprint_aliases": sorted(
                    {p["Footprint"] for p in members}
                ),
                "Original_comments": sorted({p["Comment"] for p in members}),
            }
        )
    rows.sort(key=lambda r: BomRows.natural(r["Designator"].split(",")[0]))
    evidence.sort(key=lambda r: BomRows.natural(r["Designator"].split(",")[0]))
    covered = [r for row in rows for r in row["Designator"].split(",")]
    require(
        len(covered) == len(set(covered)) == len(parts) and set(covered) == set(refs),
        "Consolidation changed factory reference coverage",
    )
    splits = [
        {
            "Manufacturer_normalized": ident[0],
            "MPN": ident[1],
            "LCSC Part #": ident[2],
            "Geometry_groups": len(geometries),
            "Disposition": "Kept separate; no geometric equivalence established",
        }
        for ident, geometries in identity_geometries.items()
        if len(geometries) > 1
    ]
    return rows, evidence, splits


def native_pad_geometry(footprint):
    """Fingerprint a detached, top-side, zero-rotation footprint's full pad stack.

    Pin numbers, coordinates, pad/drill dimensions, pad types/shapes, copper,
    mask/paste layers and local margins must agree. Custom polygons include every
    outline and hole, normalized only for contour start point and winding.
    """
    import pcbnew

    clone = pcbnew.FOOTPRINT(footprint)
    if clone.IsFlipped():
        clone.Flip(clone.GetPosition(), False)
    clone.SetOrientationDegrees(0)
    clone.SetPosition(pcbnew.VECTOR2I(0, 0))

    def contour(chain):
        pts = [
            (int(chain.CPoint(i).x), int(chain.CPoint(i).y))
            for i in range(chain.PointCount())
        ]
        require(bool(pts), "Empty custom-pad polygon contour")
        # Polygon contours are cyclic; vertex start/winding do not change shape.
        candidates = [pts[i:] + pts[:i] for i in range(len(pts))]
        rev = list(reversed(pts))
        candidates += [rev[i:] + rev[:i] for i in range(len(rev))]
        return min(candidates)

    pads = []
    for pad in clone.Pads():
        custom = None
        if pad.GetShape() == pcbnew.PAD_SHAPE_CUSTOM:
            poly = pad.GetCustomShapeAsPolygon()
            require(poly.OutlineCount() > 0, "Empty custom-pad polygon")
            custom = sorted(
                [
                    {
                        "outline": contour(poly.COutline(i)),
                        "holes": sorted(
                            contour(poly.CHole(i, j)) for j in range(poly.HoleCount(i))
                        ),
                    }
                    for i in range(poly.OutlineCount())
                ],
                key=lambda x: json.dumps(x, sort_keys=True),
            )

        def vec(v):
            return [int(v.x), int(v.y)]

        pads.append(
            {
                "number": pad.GetNumber(),
                "attribute": int(pad.GetAttribute()),
                "shape": int(pad.GetShape()),
                "at_nm": vec(pad.GetFPRelativePosition()),
                "size_nm": vec(pad.GetSize()),
                "offset_nm": vec(pad.GetOffset()),
                "angle_deg": round(pad.GetOrientationDegrees() % 360, 7),
                "drill_shape": int(pad.GetDrillShape()),
                "drill_nm": vec(pad.GetDrillSize()),
                "layers": sorted(int(x) for x in pad.GetLayerSet().Seq()),
                "roundrect_ratio": round(pad.GetRoundRectRadiusRatio(), 10),
                "chamfer_ratio": round(pad.GetChamferRectRatio(), 10),
                "chamfer_positions": int(pad.GetChamferPositions()),
                "trapezoid_delta_nm": vec(pad.GetDelta()),
                "mask_margin_nm": pad.GetLocalSolderMaskMargin(),
                "paste_margin_nm": pad.GetLocalSolderPasteMargin(),
                "paste_margin_ratio": pad.GetLocalSolderPasteMarginRatio(),
                "custom_polygon": custom,
            }
        )
    require(pads, "Footprint has no pads: " + footprint.GetReference())
    pads.sort(key=lambda row: json.dumps(row, sort_keys=True))
    return pads


def native_geometry(footprint):
    return hashlib.sha256(
        json.dumps(
            native_pad_geometry(footprint), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def validate_native_connections(policy, footprints):
    evidence = []
    for rule in policy.get("native_connection_requirements", []):
        ref, cavity = rule["connector"], str(rule["cavity"])
        require(ref in footprints, "Assembly policy connector absent: " + ref)
        pads = [p for p in footprints[ref].Pads() if p.GetNumber() == cavity]
        require(bool(pads), "Assembly policy cavity absent: " + ref + "." + cavity)
        nets = sorted({p.GetNetname() for p in pads})
        if rule["condition"] == "UNCONNECTED":
            valid = all(not name or name.startswith("unconnected-") for name in nets)
        elif rule["condition"] == "EXACT_NET":
            valid = nets == [rule["net"]]
        else:
            raise RuntimeError("Unknown assembly policy connection condition")
        require(
            valid,
            "Assembly policy connection failed: "
            + ref
            + "."
            + cavity
            + " "
            + str(nets),
        )
        evidence.append({**rule, "actual_native_nets": nets, "passed": True})
    return evidence


def export_factory_scope(
    project, manufacturing, parts, cpl, footprints, module_refs=(), native_fields=None
):
    project, manufacturing = Path(project), Path(manufacturing)
    policy_path = data_path(project, "ASSEMBLY_POLICY.json")
    require(
        policy_path.is_file(),
        "The source-controlled ASSEMBLY_POLICY.json is required",
    )
    policy = json.loads(policy_path.read_text(encoding="utf-8-sig"))
    connection_evidence = validate_native_connections(policy, footprints)
    factory, owner, scope = partition(parts, policy, module_refs, native_fields)
    allrefs, cplrefs = {p["Designator"] for p in parts}, [p["Designator"] for p in cpl]
    require(
        len(cplrefs) == len(set(cplrefs)) and set(cplrefs) == allrefs,
        "Complete system CPL does not match required system parts",
    )
    geometry = {
        p["Designator"]: native_geometry(footprints[p["Designator"]]) for p in factory
    }
    bom, merge_evidence, split_evidence = consolidate(factory, geometry)
    factoryrefs = {p["Designator"] for p in factory}
    rotation_policy = data_path(project, ROTATION_POLICY_FILE)
    factorycpl, rotation_schedule = AssemblyRotation.apply(
        [p for p in cpl if p["Designator"] in factoryrefs],
        factory,
        geometry,
        AssemblyRotation.load(project),
    )
    require(
        sum(p["Quantity"] for p in bom) == len(factorycpl) == len(factory),
        "Factory BOM/CPL partition quantity mismatch",
    )
    writecsv(manufacturing / FACTORY_BOM, bom, BOM_FIELDS)
    writecsv(manufacturing / FACTORY_CPL, factorycpl, CPL_FIELDS)
    writecsv(manufacturing / OWNER_SCHEDULE, owner, OWNER_FIELDS)
    writecsv(
        manufacturing / "assembly/ASSEMBLY_ROTATION_OFFSETS.csv",
        rotation_schedule,
        SCHEDULE_FIELDS,
    )
    for side in ("Top", "Bottom"):
        writecsv(
            manufacturing / f"assembly/04_CPL_JLCPCB_{side.upper()}.csv",
            [p for p in factorycpl if p["Layer"] == side],
            CPL_FIELDS,
        )
    receipt = {
        "schema": 1,
        "policy_path": support_input_name(project, policy_path).as_posix(),
        "policy_sha256": sha(policy_path),
        "source_sha256": {
            p.name: sha(p)
            for p in project.glob("*.kicad_*")
            if p.suffix in (".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_dru")
        },
        "system_required_refs": sorted(allrefs, key=BomRows.natural),
        "factory_assembly_refs": sorted(factoryrefs, key=BomRows.natural),
        "required_owner_install_refs": sorted(
            {p["Designator"] for p in owner}, key=BomRows.natural
        ),
        "system_parts": len(parts),
        "factory_parts": len(factory),
        "owner_install_parts": len(owner),
        "factory_bom_groups": len(bom),
        "scope_partition": scope,
        "native_geometry_by_ref": geometry,
        "rotation_policy_path": support_input_name(project, rotation_policy).as_posix(),
        "rotation_policy_sha256": sha(rotation_policy),
        "qualified_catalog_rotation_offsets": rotation_schedule,
        "native_connection_requirements": connection_evidence,
        "consolidation_groups": merge_evidence,
        "same_identity_kept_separate": split_evidence,
        "all_system_parts_required": True,
        "native_DNP_changed": False,
        "factory_scope_excludes_only_explicit_required_owner_install": True,
        "qualification_limit": (
            "File scope and native land-pattern identity only; owner "
            "preparation, physical fit, sourcing and factory "
            "acceptance are separate."
        ),
    }
    (manufacturing / "evidence").mkdir(parents=True, exist_ok=True)
    writejson(manufacturing / "evidence/ASSEMBLY_SCOPE.json", receipt)
    return receipt


def main():
    import pcbnew

    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--manufacturing", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    project, m, out = (
        args.project.resolve(),
        args.manufacturing.resolve(),
        args.out.resolve(),
    )
    require(not out.is_relative_to(project), "Output must be outside native source")
    require(not out.exists() or not any(out.iterdir()), "Output must be new/empty")
    out.mkdir(parents=True, exist_ok=True)
    input_identity = json.loads(
        (m / "SOURCE_IDENTITY.json").read_text(encoding="utf-8-sig")
    )
    for rel, expected in input_identity["source_sha256"].items():
        require(
            (project / rel).is_file() and sha(project / rel) == expected,
            "Complete system input belongs to a different native source: " + rel,
        )
    bom = readcsv(m / "02_BOM_COMPLETE.csv")
    parts = [
        {**row, "Designator": ref, "Quantity": 1}
        for row in bom
        for ref in row["Designator"].split(",")
    ]
    modules = readcsv(m / "07_REQUIRED_MODULES_NOT_DNP.csv")
    b = pcbnew.LoadBoard(str(next(project.glob("*.kicad_pcb"))))
    fs = {f.GetReference(): f for f in b.GetFootprints()}
    for part in parts:
        require(part["Designator"] in fs, "System BOM part missing from board")
        require(
            part["Footprint"] == fs[part["Designator"]].GetFPID().GetLibItemName(),
            "System BOM footprint differs from native board",
        )
        part["DNP"] = fs[part["Designator"]].IsDNP()
    fields = {
        f.GetReference(): {p.GetName(): p.GetText() for p in f.GetFields()}
        for f in fs.values()
    }
    for part in parts:
        native = fields[part["Designator"]]
        for column, aliases in [
            ("Manufacturer", ["Manufacturer"]),
            ("MPN", ["MPN"]),
            ("LCSC Part #", ["LCSC", "LCSC Part #"]),
        ]:
            for alias in aliases:
                if alias in native:
                    require(
                        native[alias] == part[column],
                        part["Designator"] + ": stale system " + column,
                    )
                    break
    receipt = export_factory_scope(
        project,
        out,
        parts,
        readcsv(m / "03_CPL_COMPLETE.csv"),
        fs,
        [p["Designator"] for p in modules],
        fields,
    )
    require(
        receipt["source_sha256"] == input_identity["source_sha256"],
        "Native source changed while deriving assembly scope",
    )
    print(
        json.dumps(
            {
                k: receipt[k]
                for k in [
                    "system_parts",
                    "factory_parts",
                    "owner_install_parts",
                    "factory_bom_groups",
                ]
            }
        )
    )


if __name__ == "__main__":
    main()
