"""render_views.py: fresh views from one unchanged native source snapshot, using the selected CLI.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import argparse
import os
import subprocess
from pathlib import Path

from core.paths import default_project, long_path

import pcbnew

from core.export_helpers import assert_current_inputs, require, sha, writejson
from review.refresh_gallery import ReviewGallery
from core.toolchain import kicad_cli


class ViewRunner:
    """Runs KiCad CLI commands for the views and keeps a log of every run."""

    def __init__(self, cli: str, folder: Path):
        """Set the CLI and the folder that receives ``VIEW_TOOL_RUNS.json``.

        Inputs:
            cli: Path of the KiCad command-line program.
            folder: Gallery folder that holds the run log.
        """
        self.cli = cli
        self.folder = folder
        self.commands = []

    def __call__(self, args) -> str:
        """Run one KiCad CLI command and rewrite the run log.

        Inputs:
            args: Command arguments after the program name.
        Outputs:
            The command's standard output without surrounding whitespace.
            Raises RuntimeError when the command exits with a nonzero status.
        """
        r = subprocess.run(
            [self.cli, *map(str, args)],
            capture_output=True,
            text=True,
            encoding="utf8",
            errors="replace",
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        self.commands.append(
            {
                "command": [self.cli, *map(str, args)],
                "returncode": r.returncode,
                "stdout": r.stdout,
                "stderr": r.stderr,
            }
        )
        writejson(self.folder / "VIEW_TOOL_RUNS.json", self.commands)
        require(r.returncode == 0, "KiCad view failure: " + r.stdout + r.stderr)
        return r.stdout.strip()


class ViewRender:
    """Renders the native PDFs and 3D views of one project from a stable source."""

    def __init__(self, project: Path, out: Path, cli: Path):
        """Resolve the folders and require a new output folder outside the project.

        Inputs:
            project: Native project folder with one board and one schematic.
            out: Output folder; the views go in its ``gallery`` subfolder.
            cli: KiCad command-line program, or None to find one.
        Outputs: None. Raises RuntimeError when the output is inside the
            project or not empty, when the project lacks exactly one board and
            schematic, or when the schematic uses hierarchy sheets.
        """
        self.project = project.resolve()
        out = out.resolve()
        self.folder = out / "gallery"
        require(not out.is_relative_to(self.project), "Output must be outside project")
        require(
            not self.folder.exists() or not any(self.folder.iterdir()),
            "Gallery must be new/empty",
        )
        self.folder.mkdir(parents=True, exist_ok=True)
        boards = list(self.project.glob("*.kicad_pcb"))
        schs = list(self.project.glob("*.kicad_sch"))
        require(len(boards) == len(schs) == 1, "One board and one schematic required")
        self.board, self.schematic = boards[0], schs[0]
        # v4.1 now deliberately has one editable schematic source.  Validate the
        # source itself rather than probing any retired/history folder beneath the
        # synced project root.
        require(
            "\n\t(sheet\n" not in self.schematic.read_text(encoding="utf8"),
            "One flattened v4.1 schematic source required; found hierarchy sheets",
        )
        self.sources = [self.board, self.schematic]
        self.cli = cli

    def source_hashes(self) -> dict:
        """Hash the board and schematic, keyed by their path inside the project."""
        return {p.relative_to(self.project).as_posix(): sha(p) for p in self.sources}

    def render(self) -> None:
        """Render every view and write ``source.json``.

        Outputs: None. Raises RuntimeError when a KiCad command fails or the
        source or a tracked dependency changed during rendering.
        """
        source = self.source_hashes()
        run = ViewRunner(str(kicad_cli(self.cli)), self.folder)
        dependencies = {
            name: sha(path)
            for name, path in ReviewGallery.dependency_paths(self.project).items()
        }
        version = run(["version"])
        b = pcbnew.LoadBoard(str(self.board))
        layers = [
            b.GetLayerName(i)
            for i in b.GetEnabledLayers().Seq()
            if pcbnew.IsCopperLayer(i)
        ]
        self.export_layers(run, layers)
        run(
            [
                "sch",
                "export",
                "pdf",
                "-o",
                self.folder / "schematic.pdf",
                self.schematic,
            ]
        )
        self.render_3d(run)
        require(
            source == self.source_hashes(),
            "Source changed during rendering; discard this mixed gallery",
        )
        assert_current_inputs(long_path(self.project), source, dependencies)
        writejson(
            self.folder / "source.json",
            {
                "source": str(self.project),
                "sha256": source,
                "dependency_sha256": dependencies,
                "preview_only": True,
                "kicad_version": version,
                "copper_layers": layers,
            },
        )

    def export_layers(self, run: ViewRunner, layers: list) -> None:
        """Export one PDF per copper layer and silkscreen.

        Inputs:
            run: Runner for KiCad CLI commands.
            layers: Enabled copper layer names of the board.
        """
        layer_views = (
            [("PCB_top", "F.Cu")]
            + [
                (f"PCB_{n.split('.')[0]}", n)
                for n in sorted(layers)
                if n.startswith("In")
            ]
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
            run(argv + ["-o", self.folder / (name + ".pdf"), self.board])
            print(name, flush=True)

    def render_3d(self, run: ViewRunner) -> None:
        """Render the top, bottom and two isometric 3D views as PNG files.

        Inputs:
            run: Runner for KiCad CLI commands.
        """
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
                    self.folder / (name + ".png"),
                    self.board,
                ]
            )
            print(name, flush=True)


class RenderViewsCommand:
    """Command-line entry: render the review views of a project."""

    def __call__(self, argv=None):
        """Parse the command line and render the views.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        """
        p = argparse.ArgumentParser()
        p.add_argument("--project", type=Path, default=default_project(__file__))
        p.add_argument("--out", type=Path, required=True)
        p.add_argument("--cli", type=Path)
        a = p.parse_args(argv)
        ViewRender(a.project, a.out, a.cli).render()


main = RenderViewsCommand()

if __name__ == "__main__":
    main()
