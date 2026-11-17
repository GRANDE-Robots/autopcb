"""Fresh views from one unchanged native source snapshot, using the selected CLI."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from core.paths import default_project, long_path

import pcbnew

from core.export_helpers import assert_current_inputs, require, sha, writejson
from review.refresh_gallery import dependency_paths


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project", type=Path, default=default_project(__file__))
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--cli", type=Path)
    a = p.parse_args()
    C = a.project.resolve()
    OUT = a.out.resolve()
    folder = OUT / "gallery"
    require(not OUT.is_relative_to(C), "Output must be outside project")
    require(
        not folder.exists() or not any(folder.iterdir()), "Gallery must be new/empty"
    )
    folder.mkdir(parents=True, exist_ok=True)
    boards = list(C.glob("*.kicad_pcb"))
    schs = list(C.glob("*.kicad_sch"))
    require(len(boards) == len(schs) == 1, "One board and one schematic required")
    board, sch = boards[0], schs[0]
    # v4.1 now deliberately has one editable schematic source.  Validate the
    # source itself rather than probing any retired/history folder beneath the
    # synced project root.
    require(
        "\n\t(sheet\n" not in sch.read_text(encoding="utf8"),
        "One flattened v4.1 schematic source required; found hierarchy sheets",
    )
    sheets = [sch]
    source = {p.relative_to(C).as_posix(): sha(p) for p in [board, *sheets]}
    CLI = str((a.cli or Path(sys.executable).with_name("kicad-cli.exe")).resolve())
    dependencies = {name: sha(path) for name, path in dependency_paths(C).items()}
    commands = []

    def run(args):
        r = subprocess.run(
            [CLI, *map(str, args)],
            capture_output=True,
            text=True,
            encoding="utf8",
            errors="replace",
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        commands.append(
            {
                "command": [CLI, *map(str, args)],
                "returncode": r.returncode,
                "stdout": r.stdout,
                "stderr": r.stderr,
            }
        )
        writejson(folder / "VIEW_TOOL_RUNS.json", commands)
        require(r.returncode == 0, "KiCad view failure: " + r.stdout + r.stderr)
        return r.stdout.strip()

    version = run(["version"])
    b = pcbnew.LoadBoard(str(board))
    layers = [
        b.GetLayerName(i) for i in b.GetEnabledLayers().Seq() if pcbnew.IsCopperLayer(i)
    ]
    layer_views = (
        [("PCB_top", "F.Cu")]
        + [(f"PCB_{n.split('.')[0]}", n) for n in sorted(layers) if n.startswith("In")]
        + [
            ("PCB_bottom", "B.Cu"),
            ("top_silkscreen", "F.SilkS"),
            ("bottom_silkscreen", "B.SilkS"),
        ]
    )
    for name, layer in layer_views:
        argv = [
            "pcb",
            "export",
            "pdf",
            "--mode-single",
            "--layers",
            layer + ",Edge.Cuts",
        ]
        if layer == "B.SilkS":
            argv += ["--mirror"]
        if "SilkS" in layer:
            argv += ["--black-and-white"]
        run(argv + ["-o", folder / (name + ".pdf"), board])
        print(name, flush=True)
    run(["sch", "export", "pdf", "-o", folder / "schematic.pdf", sch])
    for name, side, rot in [
        ("3D_top", "top", "0,0,0"),
        ("3D_bottom", "bottom", "0,0,0"),
        ("3D_isometric_top", "top", "30,0,30"),
        ("3D_isometric_bottom", "bottom", "30,0,-30"),
    ]:
        run(
            [
                "pcb",
                "render",
                "--width",
                "1500",
                "--height",
                "1400",
                "--quality",
                "basic",
                "--side",
                side,
                "--rotate",
                rot,
                "--zoom",
                ".69",
                "--background",
                "opaque",
                "-o",
                folder / (name + ".png"),
                board,
            ]
        )
        print(name, flush=True)
    require(
        source == {p.relative_to(C).as_posix(): sha(p) for p in [board, *sheets]},
        "Source changed during rendering; discard this mixed gallery",
    )
    assert_current_inputs(long_path(C), source, dependencies)
    writejson(
        folder / "source.json",
        {
            "source": str(C),
            "sha256": source,
            "dependency_sha256": dependencies,
            "preview_only": True,
            "kicad_version": version,
            "copper_layers": layers,
        },
    )


if __name__ == "__main__":
    main()
