"""Independent, read-only consistency audit of a finished factory review folder.

Does not import the exporter helpers, mutate CAD, or establish factory acceptance.
Report is written outside the source and generated directories.
"""

import argparse
import csv
import hashlib
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from core.paths import data_path, data_reference


def digest(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def read_csv(p):
    with Path(p).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def audit(project, manufacturing, gallery=None, archive=None):
    project, manufacturing = Path(project).resolve(), Path(manufacturing).resolve()
    checks, failures = [], []

    def check(condition, message):
        checks.append({"check": message, "passed": bool(condition)})
        if not condition:
            failures.append(message)

    def obj(path):
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))

    ident = obj(manufacturing / "SOURCE_IDENTITY.json")
    source = ident["source_sha256"]
    for rel, expected in {**source, **ident["dependency_sha256"]}.items():
        check(
            (project / rel).is_file() and digest(project / rel) == expected,
            "Current source/dependency: " + rel,
        )
    check(
        not any(
            rel.replace("\\", "/").startswith("manufacturing/")
            for rel in ident["dependency_sha256"]
        ),
        "Regenerated output files are not controlled source inputs",
    )
    controls = data_path(project, "release_inputs")
    control_manifest = obj(controls / "INPUT_MANIFEST.json")
    for rel, expected in control_manifest["files"].items():
        check(digest(controls / rel) == expected, "Frozen factory input: " + rel)

    manifest = obj(manufacturing / "MANIFEST_SHA256.json")
    actual = {
        p.relative_to(manufacturing).as_posix(): digest(p)
        for p in manufacturing.rglob("*")
        if p.is_file() and p.name != "MANIFEST_SHA256.json"
    }
    check(manifest["source_sha256"] == source, "Manufacturing manifest source binding")
    check(
        actual == manifest["files"],
        "Every generated file matches the manifest, with no extras",
    )
    binding = obj(manufacturing / "evidence/CURRENT_EVIDENCE_BINDING.json")
    check(binding["source_sha256"] == source, "Current evidence source binding")
    for rel, expected in binding["files"].items():
        check(digest(manufacturing / rel) == expected, "Current evidence bytes: " + rel)

    bom = read_csv(manufacturing / "02_BOM_COMPLETE.csv")
    cpl = read_csv(manufacturing / "03_CPL_COMPLETE.csv")
    reg = read_csv(manufacturing / "06_EXACT_PART_REGISTER.csv")
    mods = read_csv(manufacturing / "07_REQUIRED_MODULES_NOT_DNP.csv")
    grouped_refs = [ref.strip() for row in bom for ref in row["Designator"].split(",")]
    refset = set(grouped_refs)
    check(
        len(grouped_refs) == len(refset),
        "Each required reference occurs in exactly one BOM group",
    )
    check(
        refset
        == {x["Designator"] for x in cpl}
        == {x["Designator"] for x in reg + mods},
        "BOM, CPL, register and module reference sets agree",
    )
    check(
        sum(int(x["Quantity"]) for x in bom)
        == len(cpl)
        == len(refset)
        == ident["cpl_rows"],
        "BOM quantities equal all required placements",
    )
    check(
        (len(reg), len(mods), len(bom))
        == (
            ident["board_components"],
            ident["required_plugin_modules"],
            ident["bom_group_rows"],
        ),
        "Identity counts agree with the exported tables",
    )
    check(
        {x["Designator"] for x in mods} == {"U3", "U9"},
        "Required owner-stock module identities retained",
    )
    check(
        all(
            "OWNER" in (x.get("Status", "") + " " + x.get("Factory work", "")).upper()
            for x in mods
        ),
        "Both modules retain owner-stock handling",
    )
    for row in bom:
        check(
            len(row["Designator"].split(",")) == int(row["Quantity"]),
            "BOM group quantity: " + row["Designator"],
        )
        check(
            bool(row["Manufacturer"].strip() and row["MPN"].strip()),
            "Exact identity: " + row["Designator"],
        )
    native_flags = read_csv(manufacturing / "evidence/NATIVE_ASSEMBLY_FLAGS.csv")
    required_flags = [x for x in native_flags if x["Designator"] in refset]
    check(
        len(required_flags) == len(refset),
        "Every placement has a native assembly flag record",
    )
    check(
        all(x["DNP"].lower() in ("false", "0", "") for x in required_flags),
        "No required reference is native DNP",
    )

    # Independently verify the explicit system/factory/owner partition and exact row mapping.
    policy = obj(data_path(project, "ASSEMBLY_POLICY.json"))
    scope = obj(manufacturing / "evidence/ASSEMBLY_SCOPE.json")
    factory_bom = read_csv(manufacturing / "02_BOM_JLCPCB.csv")
    factory_cpl = read_csv(manufacturing / "03_CPL_JLCPCB.csv")
    owner_finish = read_csv(manufacturing / "07_OWNER_FINISHING.csv")
    factory_refs = [
        r.strip() for row in factory_bom for r in row["Designator"].split(",")
    ]
    factory_refset, owner_refset = (
        set(factory_refs),
        {r["Designator"] for r in owner_finish},
    )
    policy_owner = {
        r
        for r, rule in policy["overrides"].items()
        if rule["assembly_stage"] == "OWNER_INSTALL"
    }
    check(
        policy["schema"] == 1 and policy["all_system_parts_required"] is True,
        "Assembly policy keeps every native system requirement",
    )
    check(
        not factory_refset & owner_refset and factory_refset | owner_refset == refset,
        "Factory and required owner scopes partition the complete required system",
    )
    check(
        owner_refset == policy_owner == {r["Designator"] for r in mods},
        "Only explicitly controlled required owner modules are outside factory scope",
    )
    check(
        len(factory_refs)
        == len(factory_refset)
        == len(factory_cpl)
        == sum(int(r["Quantity"]) for r in factory_bom),
        "Factory BOM references, quantities and CPL count agree without duplicates",
    )
    check(
        factory_refset == {r["Designator"] for r in factory_cpl},
        "Factory BOM/CPL reference membership agrees",
    )
    complete_positions = {r["Designator"]: r for r in cpl}
    rotation_path = data_path(project, "ASSEMBLY_ROTATION_OFFSETS.json")
    rotation_policy = obj(rotation_path)
    rules = rotation_policy.get("overrides", [])
    rotation_rules = {r["designator"]: r for r in rules}
    check(
        rotation_policy.get("schema") == 1
        and rotation_policy.get("export_scope") == "JLCPCB_FACTORY_CPL"
        and rotation_policy.get("rotation_convention")
        == "ADD_TO_NATIVE_DEGREES_MOD_360"
        and rotation_policy.get("native_geometry_unchanged") is True,
        "Factory catalog-angle contract explicitly preserves native geometry",
    )
    check(
        len(rules) == len(rotation_rules)
        and set(rotation_rules).issubset(factory_refset),
        "Catalog-angle overrides are unique required factory references",
    )
    system_rotation_parts = {
        ref: row for row in bom for ref in row["Designator"].split(",")
    }
    expected_rotation_schedule = []
    for row in factory_cpl:
        ref = row["Designator"]
        native = complete_positions.get(ref)
        expected = dict(native or {})
        if ref in rotation_rules:
            rule = rotation_rules[ref]
            guard = rule.get("expected", {})
            e = rule.get("qualification_evidence", {})
            check(
                rule.get("qualified") is True
                and bool(str(rule.get("qualification_note", "")).strip()),
                "Catalog orientation qualification: " + ref,
            )
            offset = float(rule["offset_deg"])
            check(
                offset % 90 == 0 and offset % 360 != 0,
                "Nonzero qualified catalog quarter-turn: " + ref,
            )
            relative = Path(str(e.get("path", "")))
            valid_path = (
                len(relative.parts) > 1
                and not relative.anchor
                and ".." not in relative.parts
                and relative.parts[0] in {"data", "design"}
            )
            evidence_path = (
                data_reference(project, relative.as_posix()) if valid_path else None
            )
            check(
                evidence_path is not None
                and evidence_path.is_file()
                and digest(evidence_path) == e.get("sha256"),
                "Catalog qualification evidence bytes: " + ref,
            )
            guard_actual = {
                **system_rotation_parts[ref],
                **(native or {}),
                "Native_geometry_sha256": scope["native_geometry_by_ref"].get(ref),
            }
            required = {
                "Manufacturer",
                "MPN",
                "LCSC Part #",
                "Footprint",
                "Layer",
                "Rotation",
                "Native_geometry_sha256",
            }
            check(required.issubset(guard), "Catalog rotation guard coverage: " + ref)
            for key, value in guard.items():
                check(
                    key in guard_actual
                    and (
                        (float(guard_actual[key]) % 360 == float(value) % 360)
                        if key == "Rotation"
                        else guard_actual[key] == value
                    ),
                    "Catalog rotation identity/native guard " + ref + ": " + key,
                )
            expected["Rotation"] = f"{(float(native['Rotation']) + offset) % 360:.6f}"
            expected_rotation_schedule.append(
                {
                    "Designator": ref,
                    "Native rotation deg": native["Rotation"],
                    "Factory rotation deg": expected["Rotation"],
                    "Offset deg": str(rule["offset_deg"]),
                    "Layer": native["Layer"],
                    **{
                        k: system_rotation_parts[ref][k]
                        for k in ("Manufacturer", "MPN", "LCSC Part #", "Footprint")
                    },
                    "Native geometry SHA256": scope["native_geometry_by_ref"][ref],
                    "Qualification evidence": e["path"],
                    "Qualification SHA256": e["sha256"],
                }
            )
        check(
            row == expected,
            "Factory position differs only by its explicitly qualified catalog angle: "
            + ref,
        )
    rotation_schedule = read_csv(
        manufacturing / "assembly/ASSEMBLY_ROTATION_OFFSETS.csv"
    )
    check(
        rotation_schedule == expected_rotation_schedule,
        "Every qualified rotation appears once in the explicit native/factory schedule",
    )
    check(
        scope["rotation_policy_sha256"]
        == digest(rotation_path)
        == ident["assembly_rotation_policy_sha256"]
        and ident["assembly_rotation_offset_count"] == len(rules),
        "Catalog rotation recipe identity and count are source-bound",
    )
    check(
        scope["source_sha256"] == source
        and scope["policy_sha256"]
        == digest(data_path(project, "ASSEMBLY_POLICY.json")),
        "Assembly scope binds current native source and source-controlled policy",
    )
    check(
        scope["factory_parts"] == ident["factory_cpl_rows"] == len(factory_cpl)
        and scope["factory_bom_groups"]
        == ident["factory_bom_group_rows"]
        == len(factory_bom)
        and scope["owner_install_parts"]
        == ident["owner_install_rows"]
        == len(owner_finish),
        "Identity, scope and both exported assembly stages agree in quantity",
    )
    check(
        set(scope["system_required_refs"]) == refset
        and set(scope["factory_assembly_refs"]) == factory_refset
        and set(scope["required_owner_install_refs"]) == owner_refset
        and not scope["native_DNP_changed"],
        "Scope receipt preserves all system refs without changing native DNP",
    )
    system_by_ref = {ref: row for row in bom for ref in row["Designator"].split(",")}
    merges = {r["Designator"]: r for r in scope["consolidation_groups"]}
    geom = scope["native_geometry_by_ref"]
    for row in factory_bom:
        members = row["Designator"].split(",")
        check(
            len(members) == int(row["Quantity"]),
            "Factory group quantity: " + row["Designator"],
        )
        check(
            all(
                all(
                    system_by_ref[ref][k] == row[k]
                    for k in ("Manufacturer", "MPN", "LCSC Part #")
                )
                for ref in members
            ),
            "Factory group retains exact system purchasing identities: "
            + row["Designator"],
        )
        merge = merges.get(row["Designator"], {})
        check(
            bool(merge)
            and all(
                geom.get(ref) == merge.get("Native_geometry_sha256") for ref in members
            )
            and row["Footprint"] in merge.get("Equivalent_footprint_aliases", [])
            and {system_by_ref[ref]["Footprint"] for ref in members}
            == set(merge.get("Equivalent_footprint_aliases", [])),
            "Factory consolidated aliases have equal source-bound native pad geometry: "
            + row["Designator"],
        )
    for row in owner_finish:
        ref = row["Designator"]
        rule = policy["overrides"][ref]
        check(
            row["MPN"] == system_by_ref[ref]["MPN"] == rule["expected_mpn"]
            and row["Manufacturer"]
            == system_by_ref[ref]["Manufacturer"]
            == rule["expected_manufacturer"]
            and int(row["Quantity"]) == 1
            and row["Required"].lower() == "true"
            and row["Supply owner"] == "OWNER"
            and row["Assembly stage"] == "OWNER_INSTALL"
            and row["Required finishing"] == rule["required_finishing"]
            and row["Acceptance status"] == rule["acceptance_status"],
            "Required owned module exact identity and owner plug-in acceptance retained: "
            + ref,
        )

    ports = read_csv(manufacturing / "09_CONNECTOR_PINOUTS.csv")
    cables = read_csv(manufacturing / "10_FINISHED_CABLE_SCHEDULE.csv")
    netlist = ET.parse(manufacturing / "evidence/NETLIST.xml").getroot()
    native_pins = {
        (node.get("ref"), node.get("pin")): net.get("name")
        for net in netlist.findall("./nets/net")
        for node in net.findall("node")
    }
    for rule in policy.get("native_connection_requirements", []):
        endpoint = (rule["connector"], str(rule["cavity"]))
        net = native_pins.get(endpoint)
        valid = net is not None and (
            (not net or net.startswith("unconnected-"))
            if rule["condition"] == "UNCONNECTED"
            else net == rule["net"]
        )
        check(valid, "Reviewed native pin contract: " + ".".join(endpoint))
        if re.fullmatch(r"J\d+", rule["connector"]):
            contacts = [r for r in ports if (r["Connector"], r["Cavity"]) == endpoint]
            check(
                len(contacts) == 1 and contacts[0]["Net"] == net,
                "Connector table agrees with native pin contract: "
                + ".".join(endpoint),
            )
    connector_refs = {r for r in refset if re.fullmatch(r"J\d+", r)}
    check(
        connector_refs
        == {r["Connector"] for r in ports}
        == {r["Connector"] for r in cables},
        "Every J connector has a pin contract and finished-cable schedule",
    )
    check(
        len(ports) == ident["connector_contacts"],
        "Connector contact count matches native export",
    )
    parts_by_ref = {r["Designator"]: r for r in reg}
    check(
        all(
            r["Header MPN"] == parts_by_ref[r["Connector"]]["MPN"]
            for r in ports + cables
        ),
        "Cable/header exact MPNs match the assembly register",
    )

    fills = read_csv(manufacturing / "assembly/EXPLICIT_FILLED_CAPPED_VIAS.csv")
    holes = read_csv(manufacturing / "assembly/NATIVE_DRILL_AND_SLOT_SCHEDULE.csv")
    drill = obj(manufacturing / "evidence/NATIVE_DRILL_MAP.json")
    check(
        len(fills)
        == ident["selectively_filled_capped_vias"]
        == drill["counts"]["filled_capped_vias"],
        "Native selective fill/cap quantities agree",
    )
    check(
        len(holes) == drill["counts"]["holes"],
        "Every native hole or slot has a drill-schedule row",
    )
    erc = obj(manufacturing / "evidence/ERC.json")
    drc = obj(manufacturing / "evidence/DRC.json")
    check(
        not [v for s in erc["sheets"] for v in s["violations"]],
        "Configured native ERC is clear",
    )
    for key in [
        "violations",
        "unconnected_items",
        "schematic_parity",
        "ignored_checks",
    ]:
        check(key in drc and not drc[key], "Full native DRC " + key + " is clear")

    doc = obj(manufacturing / "evidence/FACTORY_DOCUMENT_GENERATION.json")
    check(
        doc["source_sha256"] == source
        and doc["source_identity_sha256"]
        == digest(manufacturing / "SOURCE_IDENTITY.json"),
        "Generated factory instructions belong to the current identity",
    )
    build = (manufacturing / "05_BUILD_AND_ASSEMBLY.md").read_text(encoding="utf8")
    check(
        f"{ident['cpl_rows']} required placements" in build
        and f"{ident['board_components']} board parts" in build
        and f"{ident['bom_group_rows']} BOM rows" in build,
        "Factory build counts use the current native census",
    )
    check(
        "02_BOM_JLCPCB.csv" in build
        and "03_CPL_JLCPCB.csv" in build
        and "07_OWNER_FINISHING.csv" in build
        and f"{ident['factory_cpl_rows']} factory-assembled references" in build,
        "Factory instructions distinguish JLC upload scope from the complete system",
    )
    check(
        "historical until rematched" in build.lower(),
        "Earlier JLC imports/carts are not treated as current acceptance",
    )
    check(
        not ident["factory_order_released"]
        and not ident["stock_reserved"]
        and not ident["physically_tested"],
        "Factory/purchase/physical evidence boundaries remain explicit",
    )
    obsolete = [
        p.name
        for p in manufacturing.iterdir()
        if re.match(r"(02_BOM|03_CPL|06_EXACT_PART_REGISTER).*\d{3}", p.name)
    ]
    check(not obsolete, "No obsolete count-named active BOM/CPL/register files")
    for rel in [
        "evidence/IPC_D356_VALIDATION.json",
        "evidence/NATIVE_DRILL_MAP.json",
        "evidence/ERC_RULE_CONFIGURATION.json",
        "evidence/USB_C_LOCATOR_DRILL_CHECK.json",
    ]:
        check(
            obj(manufacturing / rel)["source_sha256"] == source,
            "Independent export evidence binding: " + rel,
        )

    if gallery:
        gallery = Path(gallery).resolve()
        view = obj(gallery / "source.json")
        check(
            view["sha256"]
            == {
                n: h
                for n, h in source.items()
                if n.endswith((".kicad_sch", ".kicad_pcb"))
            },
            "Gallery belongs to the final native schematic and board",
        )
        check(
            bool(view.get("dependency_sha256"))
            and all(
                ident["dependency_sha256"].get(n) == h
                for n, h in view.get("dependency_sha256", {}).items()
            ),
            "Gallery model, library and renderer dependencies match the final export",
        )
        required = [
            "schematic",
            "PCB_top",
            "PCB_bottom",
            "top_silkscreen",
            "bottom_silkscreen",
        ]
        required += [
            "PCB_" + n.split(".")[0]
            for n in view["copper_layers"]
            if n.startswith("In")
        ]
        required += ["3D_top", "3D_bottom", "3D_isometric_top", "3D_isometric_bottom"]
        check(
            all((gallery / (name + ".png")).is_file() for name in required),
            "All requested layer/schematic/3D images exist",
        )
    if archive:
        with zipfile.ZipFile(archive) as z:
            check(z.testzip() is None, "Factory archive CRC is valid")
            check(
                len(z.namelist()) == len(set(z.namelist())),
                "Factory archive has unique member names",
            )
            for rel, expected in {
                **actual,
                "MANIFEST_SHA256.json": digest(manufacturing / "MANIFEST_SHA256.json"),
            }.items():
                check(
                    rel in z.namelist()
                    and hashlib.sha256(z.read(rel)).hexdigest() == expected,
                    "Factory archive contains current bytes: " + rel,
                )
    return {
        "source_sha256": source,
        "all_consistency_checks_passed": not failures,
        "check_count": len(checks),
        "failures": failures,
        "checks": checks,
        "counts": {
            "placements": len(cpl),
            "board_parts": len(reg),
            "owner_modules": len(mods),
            "bom_groups": len(bom),
            "filled_capped_vias": len(fills),
            "holes_slots": len(holes),
        },
        "scope": (
            "Read-only document, table, source, export and optional "
            "archive consistency. No physical, SI, thermal, "
            "OEM-interface or factory-process acceptance is inferred."
        ),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project", type=Path, required=True)
    p.add_argument("--manufacturing", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--gallery", type=Path)
    p.add_argument("--archive", type=Path)
    a = p.parse_args()
    out = a.out.resolve()
    if out.is_relative_to(a.project.resolve()) or out.is_relative_to(
        a.manufacturing.resolve()
    ):
        raise RuntimeError(
            "Keep independent audit output outside source and manufacturing directories"
        )
    if out.exists():
        raise FileExistsError(f"Refusing to overwrite existing audit output: {out}")
    report = audit(a.project, a.manufacturing, a.gallery, a.archive)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(
        json.dumps(
            {
                k: report[k]
                for k in [
                    "all_consistency_checks_passed",
                    "check_count",
                    "counts",
                    "failures",
                ]
            },
            indent=2,
        )
    )
    if report["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
