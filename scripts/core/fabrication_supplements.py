"""Add reviewed, native-bound fabrication instructions to the generated CAM ZIP."""

import csv
import json
import zipfile
from pathlib import Path

from core.paths import asset_root

from core.export_helpers import require, sha, stable_zip, writejson


def apply_fabrication_supplements(project, manufacturing):
    P = Path(project).resolve()
    M = Path(manufacturing).resolve()
    identity = json.loads((M / "SOURCE_IDENTITY.json").read_text(encoding="utf-8-sig"))
    root = asset_root(P) / "design/fabrication"
    spec_path = root / "FABRICATION_SUPPLEMENTS.json"
    require(spec_path.is_file(), "Reviewed fabrication supplement manifest is required")
    spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    require(
        spec.get("schema") == 1
        and spec.get("source_sha256") == identity["source_sha256"],
        "Fabrication instructions belong to another native source",
    )
    require(
        spec.get("quotation_only") is True,
        "Fabrication supplement must preserve quotation-only scope",
    )
    for n, h in identity["source_sha256"].items():
        require(sha(P / n) == h, "Native source changed: " + n)
    expected = {
        "REQUIRED_IMPEDANCE_CONTROL.xlsx",
        "IMPEDANCE_POSITIONS.jpg",
        "FABRICATION_NOTES.txt",
        "FACTORY_MODEL_HOLDS.csv",
    }
    require(
        set(spec.get("files", {})) == expected, "Reviewed supplement inventory changed"
    )
    for rel, h in spec.get("support_evidence", {}).items():
        p = root / rel
        require(
            p.resolve().is_relative_to(root) and p.is_file() and sha(p) == h,
            "Reviewed support evidence changed: " + rel,
        )
    files = []
    records = []
    for n, h in spec["files"].items():
        require(Path(n).name == n, "Supplement names must be simple filenames")
        p = root / n
        require(p.is_file() and sha(p) == h, "Reviewed supplement changed: " + n)
        # The model-hold table belongs to the compact review, not to CAM instructions.
        if n != "FACTORY_MODEL_HOLDS.csv":
            files.append((p, n))
        records.append(
            {
                "path": (root / n).relative_to(P).as_posix(),
                "sha256": h,
                "CAM_ZIP": n != "FACTORY_MODEL_HOLDS.csv",
            }
        )
    via = M / "assembly/EXPLICIT_FILLED_CAPPED_VIAS.csv"
    with via.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    require(
        len(rows)
        == identity["selectively_filled_capped_vias"]
        == spec["selective_filled_capped_vias"],
        "Selective fill/cap count differs from current native identity or reviewed schedule",
    )
    require(len({r["UUID"] for r in rows}) == len(rows), "Duplicate selective-via UUID")
    require(
        all(float(r["Drill mm"]) <= 0.4 for r in rows),
        "Scheduled filled-via drill exceeds reviewed process",
    )
    require(
        sha(via) == spec["generated_via_schedule_sha256"],
        "Regenerated selective-via schedule differs from reviewed instructions",
    )
    files.append((via, "EXPLICIT_FILLED_CAPPED_VIAS.csv"))
    archive = M / "01_PCB_GERBERS_DRILLS.zip"
    cam = [
        (p, p.name)
        for p in sorted((M / "gerbers").iterdir())
        if p.is_file() and p.suffix.lower() != ".pdf"
    ]
    require(
        all(p.suffix.lower() in [".gbr", ".drl", ".gbrjob", ".ipc"] for p, _ in cam),
        "Unexpected loose CAM entry",
    )
    with zipfile.ZipFile(archive) as z:
        require(
            set(z.namelist()) == {n for _, n in cam},
            "Apply supplements once to a fresh native CAM archive",
        )
        require(
            all(z.read(n) == p.read_bytes() for p, n in cam),
            "Existing native CAM ZIP differs from loose exports",
        )
    entries = cam + files
    require(
        len({n for _, n in entries}) == len(entries),
        "Duplicate CAM/supplement member name",
    )
    stable_zip(archive, entries)
    ipc_path = M / "evidence/IPC_D356_VALIDATION.json"
    ipc = json.loads(ipc_path.read_text(encoding="utf-8-sig"))
    ipc["gerber_archive_members"] = [n for _, n in sorted(entries, key=lambda x: x[1])]
    ipc["fabrication_supplements_manifest_sha256"] = sha(spec_path)
    writejson(ipc_path, ipc)
    receipt = {
        "source_sha256": identity["source_sha256"],
        "manifest_path": spec_path.relative_to(P).as_posix(),
        "manifest_sha256": sha(spec_path),
        "controlled_files": records,
        "generated_via_schedule_sha256": sha(via),
        "selective_filled_capped_vias": len(rows),
        "archive_sha256": sha(archive),
        "archive_members": ipc["gerber_archive_members"],
        "original_CAM_members_unchanged": True,
        "native_design_unchanged": True,
        "quotation_only": True,
        "production_released": False,
        "limits": (
            "Actual route geometry and discontinuities are submitted "
            "for factory review. A worksheet is not impedance "
            "qualification."
        ),
    }
    writejson(M / "evidence/FABRICATION_SUPPLEMENTS.json", receipt)
    return receipt
