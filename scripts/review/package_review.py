"""Package a generated review folder, verifying current source and every file.

Does not publish into canonical folders or release an order. Optional galleries
must carry the same board/schematic hashes. External audits are not invented.
"""

import argparse
import json
from pathlib import Path

from core.paths import data_path

from core.export_helpers import (
    FILES,
    assert_factory_sources,
    factory_input_dir,
    readcsv,
    require,
    sha,
    stable_zip,
    writejson,
)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project", type=Path, required=True)
    p.add_argument("--generated", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--gallery", type=Path)
    a = p.parse_args()
    P = a.project.resolve()
    M = a.generated.resolve() / "jlcpcb"
    output_dir = a.out.resolve()
    require(not output_dir.is_relative_to(P), "Package output must be outside project")
    require(
        not output_dir.exists() or not any(output_dir.iterdir()),
        "Package output must be new/empty",
    )
    factory_input_dir(P)
    identity = json.loads((M / "SOURCE_IDENTITY.json").read_text())
    source = identity["source_sha256"]
    assert_factory_sources(P, source, identity["dependency_sha256"])
    manifest = json.loads((M / "MANIFEST_SHA256.json").read_text())
    require(manifest["source_sha256"] == source, "Manifest/source mismatch")
    actual = {
        p.relative_to(M).as_posix(): sha(p)
        for p in M.rglob("*")
        if p.is_file()
        and p.name.lower() not in ("desktop.ini", "thumbs.db", ".ds_store")
        and p.name != "MANIFEST_SHA256.json"
    }
    require(
        actual == manifest["files"],
        (
            "Generated folder changed since manifest; rebuild or "
            "explicitly regenerate manifest after validation"
        ),
    )
    binding = json.loads((M / "evidence/CURRENT_EVIDENCE_BINDING.json").read_text())
    require(binding["source_sha256"] == source, "Evidence/source mismatch")
    for rel, digest in binding["files"].items():
        require(sha(M / rel) == digest, "Evidence changed: " + rel)
    erc = json.loads((M / "evidence/ERC.json").read_text())
    drc = json.loads((M / "evidence/DRC.json").read_text())
    require(not [v for s in erc["sheets"] for v in s["violations"]], "ERC failure")
    for key in [
        "violations",
        "unconnected_items",
        "schematic_parity",
        "ignored_checks",
    ]:
        require(key in drc and not drc[key], "DRC " + key + " failure")
    bom = readcsv(M / FILES["bom"])
    cpl = readcsv(M / FILES["cpl"])
    reg = readcsv(M / FILES["register"])
    mods = readcsv(M / "07_REQUIRED_MODULES_NOT_DNP.csv")
    refs = {r for x in bom for r in x["Designator"].split(",")}
    require(
        sum(int(x["Quantity"]) for x in bom)
        == len(cpl)
        == len(refs)
        == identity["cpl_rows"],
        "Quantity/placement mismatch",
    )
    require(
        refs == {r["Designator"] for r in cpl} == {r["Designator"] for r in reg + mods},
        "Reference mismatch",
    )
    scope = json.loads((M / "evidence/ASSEMBLY_SCOPE.json").read_text())
    fbom = readcsv(M / "02_BOM_JLCPCB.csv")
    fcpl = readcsv(M / "03_CPL_JLCPCB.csv")
    owner = readcsv(M / "07_OWNER_FINISHING.csv")
    frefs = [r for row in fbom for r in row["Designator"].split(",")]
    orefs = {r["Designator"] for r in owner}
    require(
        scope["source_sha256"] == source
        and scope["policy_sha256"] == sha(data_path(P, "ASSEMBLY_POLICY.json")),
        "Assembly scope/source policy mismatch",
    )
    require(
        len(frefs)
        == len(set(frefs))
        == len(fcpl)
        == sum(int(r["Quantity"]) for r in fbom),
        "Factory pair quantities differ",
    )
    require(
        set(frefs) == {r["Designator"] for r in fcpl}
        and not set(frefs) & orefs
        and set(frefs) | orefs == refs,
        "Factory/required owner/system partition differs",
    )
    entries = [
        (p, p.relative_to(M).as_posix())
        for p in M.rglob("*")
        if p.is_file()
        and p.name.lower() not in ("desktop.ini", "thumbs.db", ".ds_store")
    ]
    if a.gallery:
        G = a.gallery.resolve()
        viewsource = json.loads((G / "source.json").read_text())
        require(
            viewsource["sha256"]
            == {
                n: h
                for n, h in source.items()
                if n.endswith((".kicad_pcb", ".kicad_sch"))
            },
            "Gallery/source mismatch",
        )
        require(
            not any((M / "views").iterdir()),
            "Generated folder already has views; avoid duplicate entries",
        )
        entries += [
            (p, "views/" + p.relative_to(G).as_posix())
            for p in G.rglob("*")
            if p.is_file()
            and p.name.lower() not in ("desktop.ini", "thumbs.db", ".ds_store")
        ]
    output_dir.mkdir(parents=True, exist_ok=True)
    z = output_dir / f"{identity['revision']}_factory_engineering_review.zip"
    stable_zip(z, entries)
    writejson(
        output_dir / "PACKAGE_RECEIPT.json",
        {
            "source_sha256": source,
            "archive": z.name,
            "archive_sha256": sha(z),
            "entries": len(entries),
            "bom_groups": len(bom),
            "placements": len(cpl),
            "board_components": len(reg),
            "modules": len(mods),
            "factory_placements": len(fcpl),
            "factory_bom_groups": len(fbom),
            "required_owner_install": len(owner),
            "factory_upload_pair": ["02_BOM_JLCPCB.csv", "03_CPL_JLCPCB.csv"],
            "factory_order_released": False,
            "scope": (
                "Generated review files only; full electrical, independent "
                "CAM, visual and factory acceptance claims require "
                "separately refreshed evidence"
            ),
            "verified_archive_bytes": True,
        },
    )
    assert_factory_sources(P, source, identity["dependency_sha256"])
    print(z)


if __name__ == "__main__":
    main()
