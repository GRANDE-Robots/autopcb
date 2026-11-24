"""Build one compact, source-bound JLC review attachment; no CAD or order writes."""

import argparse
import json
import tempfile
from pathlib import Path

from core.paths import data_path, data_reference

from core.export_helpers import (
    assert_factory_sources,
    factory_input_dir,
    require,
    sha,
    stable_zip,
    writejson,
)

MAX_BYTES = 9_500_000


def validate_generated_files(manufacturing, source, entries):
    manifest = json.loads(
        (manufacturing / "MANIFEST_SHA256.json").read_text(encoding="utf-8-sig")
    )
    require(manifest["source_sha256"] == source, "Manifest/source mismatch")
    for path, name in entries:
        require(path.is_file(), "Missing compact review input: " + name)
        if path.is_relative_to(manufacturing):
            relative = path.relative_to(manufacturing).as_posix()
            require(
                manifest["files"].get(relative) == sha(path),
                "Generated attachment input changed: " + relative,
            )


def build(project, manufacturing, gallery, out):
    P = Path(project).resolve()
    M = Path(manufacturing).resolve()
    V = Path(gallery).resolve()
    output_dir = Path(out).resolve()
    require(
        not output_dir.is_relative_to(P),
        "Keep generated attachments outside source project",
    )
    require(not output_dir.exists(), "Use a new attachment filename")
    receipt_path = output_dir.with_suffix(".receipt.json")
    require(not receipt_path.exists(), "Use a new receipt filename")
    factory_input_dir(P)
    identity = json.loads((M / "SOURCE_IDENTITY.json").read_text(encoding="utf-8-sig"))
    source = identity["source_sha256"]
    assert_factory_sources(P, source, identity["dependency_sha256"])
    view = json.loads((V / "source.json").read_text(encoding="utf-8-sig"))
    require(
        view["sha256"]
        == {
            n: h
            for n, h in identity["source_sha256"].items()
            if n.endswith((".kicad_pcb", ".kicad_sch"))
        },
        "Gallery/source mismatch",
    )
    require(
        len(view["copper_layers"]) == identity["copper_layers"],
        "Gallery copper layers differ from current native identity",
    )
    view_count = len(view["copper_layers"]) + 7
    factory_count = identity["factory_cpl_rows"]
    fill_count = identity["selectively_filled_capped_vias"]
    stack = json.loads(
        (data_path(P, "STACKUP_CURRENT.json")).read_text(encoding="utf-8-sig")
    )
    require(
        stack.get("stackup_id")
        and stack.get("calculator")
        and stack.get("dielectric_thicknesses_mm"),
        "Current stack observations are incomplete; no compact factory attachment generated",
    )
    require(
        stack["inner_copper_oz"] >= stack["minimum_inner_copper_oz"],
        "Inner copper is below the reviewed power-path requirement",
    )
    require(
        stack["copper_layers"]
        == identity["copper_layers"]
        <= stack["maximum_copper_layers"]
        <= 4,
        "Compact attachment stack/native mismatch",
    )
    paths = [
        "01_PCB_GERBERS_DRILLS.zip",
        "SOURCE_IDENTITY.json",
        "02_BOM_JLCPCB.csv",
        "03_CPL_JLCPCB.csv",
        "05_BUILD_AND_ASSEMBLY.md",
        "07_OWNER_FINISHING.csv",
        "08_RELEASE_GATES.csv",
        "09_CONNECTOR_PINOUTS.csv",
        "12_ORDER_CONFIGURATION.md",
        "assembly/SCHEMATIC.pdf",
        "assembly/EXPLICIT_FILLED_CAPPED_VIAS.csv",
        "assembly/ASSEMBLY_ROTATION_OFFSETS.csv",
    ]
    entries = [(M / n, n) for n in paths]
    entries += [
        (V / n, "views/" + n)
        for n in [
            "top_silkscreen.pdf",
            "bottom_silkscreen.pdf",
            "3D_top.png",
            "3D_bottom.png",
            "source.json",
        ]
    ]
    entries += [
        (data_path(P, n), "data/" + n)
        for n in ["USB_HOST_POWER_GATE.md", "STACKUP_CURRENT.json"]
    ]
    entries += [
        (data_path(P, "fabrication", n), "fabrication/" + n)
        for n in [
            "FABRICATION_SUPPLEMENTS.json",
            "REQUIRED_IMPEDANCE_CONTROL.xlsx",
            "IMPEDANCE_POSITIONS.jpg",
            "FABRICATION_NOTES.txt",
            "FACTORY_MODEL_HOLDS.csv",
        ]
    ]
    entries += [
        (data_path(P, "fabrication", n), "fabrication/" + n)
        for n in [
            "evidence/NATIVE_IMPEDANCE_GEOMETRY.json",
            "evidence/ALL_DIFFERENTIAL_TRACKS.csv",
            "evidence/IMPEDANCE_MANIFEST.json",
        ]
    ]
    for n, h in stack["evidence_files"].items():
        evidence = data_reference(P, n)
        require(
            evidence.is_file() and sha(evidence) == h, "Stack evidence changed: " + n
        )
        member = (
            (Path("data") / Path(n.removeprefix("design/"))).as_posix()
            if n.startswith("design/")
            else n
        )
        entries.append((evidence, member))
    validate_generated_files(M, source, entries)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="jessie-attachment-") as work:
        scratch = Path(work)
        readme = scratch / "README.txt"
        readme.write_text(
            (
                f"GRANDE Jessie v4.1 — unpaid engineering review only.\n\n5 "
                f"PCBs total; 2 carrier assemblies. Fit {factory_count} "
                f"required factory parts per carrier; owner plugs in the "
                f"already-headered Teensy/RTC. No duplicate "
                f"modules.\n\nThis compact attachment retains "
                f"fabrication/CAM, exact factory BOM/CPL, schematic and "
                f"both silkscreen PDFs, top/bottom 3D, via schedule, "
                f"USB/impedance notes and model holds. The full "
                f"{view_count}-view gallery and complete review/native "
                f"packages remain separate.\n\nImpedance worksheet records "
                f"actual varying route widths/layers and unresolved "
                f"discontinuities. Factory must assess the route/stack and "
                f"{fill_count} selective fill/caps with insertion holes "
                f"open. No paid work, procurement or production without "
                f"separate owner approval.\n"
            ),
            encoding="utf-8",
        )
        entries.append((readme, "README.txt"))
        manifest = scratch / "COMPACT_MANIFEST_SHA256.json"
        writejson(
            manifest,
            {
                "source_sha256": identity["source_sha256"],
                "files": {n: sha(p) for p, n in entries},
                "quotation_only": True,
                "production_released": False,
            },
        )
        entries.append((manifest, manifest.name))
        archive = scratch / output_dir.name
        stable_zip(archive, entries)
        require(
            archive.stat().st_size < MAX_BYTES,
            (
                "Compact review exceeds the 9.5 MB conservative attachment "
                "limit; do not silently omit required files"
            ),
        )
        assert_factory_sources(P, source, identity["dependency_sha256"])
        factory_input_dir(P)
        data = archive.read_bytes()
        with output_dir.open("xb") as handle:
            handle.write(data)
        require(output_dir.read_bytes() == data, "Attachment failed readback")
        receipt = {
            "source_sha256": identity["source_sha256"],
            "archive": str(output_dir),
            "archive_sha256": sha(output_dir),
            "bytes": len(data),
            "maximum_bytes": MAX_BYTES,
            "members": len(entries),
            "all_selected_files_preserved": True,
            "full_review_and_all_views_remain_separate": True,
            "full_gallery_view_count": view_count,
            "selective_filled_capped_vias": fill_count,
            "quotation_only": True,
            "production_released": False,
        }
        with receipt_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True)
            handle.write("\n")
        return receipt


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project", type=Path, required=True)
    p.add_argument("--manufacturing", type=Path, required=True)
    p.add_argument("--gallery", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(build(a.project, a.manufacturing, a.gallery, a.out), indent=2))


if __name__ == "__main__":
    main()
