"""One-time explicit migration of controlled inputs out of generated outputs.

Writes only data/release_inputs in the explicitly selected project.
Refuses to overwrite an existing snapshot. No CAD, manufacturing, carts or orders
are changed. Carry this directory with the native project when publishing.
"""

import argparse
import datetime
import shutil
from pathlib import Path

from core.paths import asset_root, data_path

from core.export_helpers import (
    FILES,
    controlled_input,
    require,
    sha,
    writejson,
)


def stage(project):
    p = Path(project).resolve()
    src = p / "deliverables/jlcpcb"
    dest = asset_root(p) / "data/release_inputs"
    require(
        not data_path(p, "release_inputs").exists(),
        (
            "Controlled input snapshot already exists; review changes "
            "explicitly instead of overwriting"
        ),
    )
    register = controlled_input(src, FILES["register"], "06_EXACT_PART_REGISTER_*.csv")
    require(
        register is not None,
        "Missing exact register for controlled fallback/provenance",
    )
    pairs = [(register, "FALLBACK_EXACT_PART_REGISTER.csv")]
    for name in [
        "07_REQUIRED_MODULES_NOT_DNP.csv",
        "09_CONNECTOR_PINOUTS.csv",
        "10_FINISHED_CABLE_SCHEDULE.csv",
    ]:
        pairs.append((src / name, name))
    pairs.extend(
        [
            (
                src / "assembly/PCB_FEATURES_NO_PURCHASE.csv",
                "PCB_FEATURES_NO_PURCHASE.csv",
            ),
            (src / "05_BUILD_AND_ASSEMBLY.md", "STACKUP_REFERENCE.md"),
        ]
    )
    for path, _ in pairs:
        require(path.is_file(), "Missing controlled migration input: " + str(path))
    before = {str(path): sha(path) for path, _ in pairs}
    dest.mkdir(parents=True)
    for path, name in pairs:
        shutil.copy2(path, dest / name)
    files = {name: sha(dest / name) for _, name in pairs}
    require(
        all(sha(path) == before[str(path)] == files[name] for path, name in pairs),
        "Input changed while staging snapshot",
    )
    writejson(
        dest / "INPUT_MANIFEST.json",
        {
            "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status": "STALE - RELEASE BLOCKED PENDING FULL REVIEW",
            "purpose": (
                "Immutable engineering control inputs; never regenerated "
                "from manufacturing outputs during export"
            ),
            "migration_source_project": str(p),
            "original_files": {
                name: {
                    "relative_path": path.relative_to(p).as_posix(),
                    "sha256": files[name],
                }
                for path, name in pairs
            },
            "files": files,
            "publication_rule": (
                "Preserve this folder with the native project. Replacing "
                "manufacturing outputs does not replace these controlled "
                "inputs. Update only after explicit review and refresh the "
                "manifest."
            ),
        },
    )
    return dest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args()
    print(stage(args.project))
