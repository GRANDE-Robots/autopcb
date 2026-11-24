"""Regenerate the one active review gallery directly from one stable KiCad source pair.

This intentionally keeps only the canonical named PNG/PDF views, the linked
gallery, and its byte-self-contained counterpart.  Rendering happens outside
the Drive project; verified files replace their active counterparts atomically.
"""

from __future__ import annotations

from core.project import native_path

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from core.paths import asset_root, library_directories, long_path
from core.console import Console, add_output_arguments
from review.presentation import show_gallery

PDFS = {
    "schematic.pdf",
    "PCB_top.pdf",
    "PCB_In1.pdf",
    "PCB_In2.pdf",
    "PCB_bottom.pdf",
    "top_silkscreen.pdf",
    "bottom_silkscreen.pdf",
}
PNGS = {
    "schematic.png",
    "PCB_top.png",
    "PCB_In1.png",
    "PCB_In2.png",
    "PCB_bottom.png",
    "top_silkscreen.png",
    "bottom_silkscreen.png",
    "3D_top.png",
    "3D_bottom.png",
    "3D_isometric_top.png",
    "3D_isometric_bottom.png",
}
ACTIVE = PDFS | PNGS | {"source.json", "index.html", "index_standalone.html"}


def required_outputs(layers=None):
    """Derive the named layer exports from the saved board's copper stack."""
    if layers is None:
        return set(PDFS), set(PNGS), set(ACTIVE)
    if (
        not isinstance(layers, list)
        or len(layers) != len(set(layers))
        or not {"F.Cu", "B.Cu"} <= set(layers)
        or any(
            not isinstance(name, str)
            or not re.fullmatch(r"(?:F|B|In[1-9][0-9]?)\.Cu", name)
            for name in layers
        )
    ):
        raise ValueError("Invalid review copper-layer inventory")
    inner = {f"PCB_{name.split('.')[0]}" for name in layers if name.startswith("In")}
    pdfs = {name for name in PDFS if not name.startswith("PCB_In")} | {
        name + ".pdf" for name in inner
    }
    pngs = {name for name in PNGS if not name.startswith("PCB_In")} | {
        name + ".png" for name in inner
    }
    return (
        pdfs,
        pngs,
        pdfs | pngs | {"source.json", "index.html", "index_standalone.html"},
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run(
    command: list[str],
    label: str,
    env: dict[str, str],
    *,
    console: Console | None = None,
) -> None:
    with console.activity(label) if console else nullcontext():
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    if result.returncode:
        raise RuntimeError(
            f"{label} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}"
        )


def dependency_paths(project: Path) -> dict[str, Path]:
    """Find the library and render inputs tracked by every review snapshot."""
    root = long_path(project)
    support = asset_root(root)
    excluded_names = {"desktop.ini", "thumbs.db", ".ds_store"}
    candidates = [
        path
        for directory in library_directories(root)
        for path in directory.rglob("*")
        if "__pycache__" not in path.parts
    ]
    candidates.extend((root / "fp-lib-table", root / "sym-lib-table"))
    candidates.extend(
        support / "scripts" / name
        for name in (
            "core/export_helpers.py",
            "core/paths.py",
            "review/render_views.py",
            "review/refresh_gallery.py",
            "review/rasterize_gallery.py",
            "review/build_standalone_gallery.py",
        )
    )
    return {
        path.relative_to(root).as_posix(): path
        for path in candidates
        if path.name.lower() not in excluded_names and path.is_file()
    }


def verify_gallery(folder: Path, source_hashes: dict[str, str]) -> None:
    source = json.loads((folder / "source.json").read_text(encoding="utf-8"))
    pdfs, pngs, active = required_outputs(source.get("copper_layers"))
    expected = active - {"index_standalone.html"}
    actual = {path.name for path in folder.iterdir() if path.is_file()}
    require(expected <= actual, "Fresh gallery is missing expected review views")
    for name in pdfs:
        data = (folder / name).read_bytes()
        require(
            data.startswith(b"%PDF-") and len(data) > 1024,
            f"Invalid native PDF: {name}",
        )
    for name in pngs:
        data = (folder / name).read_bytes()
        require(
            data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) > 1024,
            f"Invalid PNG: {name}",
        )
    source = json.loads((folder / "source.json").read_text(encoding="utf-8"))
    require(
        source.get("sha256") == source_hashes,
        "Fresh gallery source identity does not match live CAD",
    )
    index = (folder / "index.html").read_text(encoding="utf-8")
    require(
        index.count("<section id=") == len(pngs),
        "Gallery view count does not match the saved copper stack",
    )


def gallery_is_current(
    project: Path, folder: Path, source_hashes: dict[str, str]
) -> bool:
    """Verify the current set and its tracked dependencies before skipping rendering."""
    manifest = folder / "source.json"
    try:
        manifest_bytes = manifest.read_bytes()
        source = json.loads(manifest_bytes)
        if not isinstance(source, dict) or source.get("sha256") != source_hashes:
            return False
        dependencies = source.get("dependency_sha256")
        if not isinstance(dependencies, dict) or not dependencies:
            return False
        verify_gallery(folder, source_hashes)
        standalone = (folder / "index_standalone.html").read_text(encoding="utf-8")
        _, pngs, _ = required_outputs(source.get("copper_layers"))
        if standalone.count("data:image/png;base64,") != len(pngs):
            return False
    except (FileNotFoundError, UnicodeError, ValueError, RuntimeError):
        if (
            manifest.is_file()
            and "manifest_bytes" in locals()
            and manifest.read_bytes() != manifest_bytes
        ):
            raise RuntimeError(
                "Review manifest changed during the freshness check"
            ) from None
        return False

    root = long_path(project)
    required_dependencies = set(dependency_paths(root))
    if not required_dependencies <= dependencies.keys():
        return False
    observed = {}
    for relative, expected in dependencies.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            return False
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise RuntimeError("Review dependency is outside the project")
        if not path.is_file():
            return False
        observed[path] = digest(path)
    # Refuse to call an actively changing source set current or stale.
    if (
        manifest.read_bytes() != manifest_bytes
        or set(dependency_paths(root)) != required_dependencies
        or any(digest(path) != value for path, value in observed.items())
    ):
        raise RuntimeError("Review dependencies changed during the freshness check")
    if any(digest(root / name) != value for name, value in source_hashes.items()):
        raise RuntimeError("Design changed during the review freshness check")
    return all(
        observed[(root / relative).resolve()] == expected
        for relative, expected in dependencies.items()
    )


def atomic_write(target: Path, content: bytes) -> None:
    """Replace one file using a temporary file in the same directory."""
    fd, name = tempfile.mkstemp(
        prefix=".review-refresh-", suffix=".tmp", dir=target.parent
    )
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, target)
    finally:
        if staged.exists():
            staged.unlink()


@dataclass(frozen=True)
class ReviewSnapshot:
    """Preserve user-visible files if publication fails or they change."""

    folder: Path
    originals: dict[str, bytes | None]

    @classmethod
    def capture(cls, folder: Path, names: set[str]) -> ReviewSnapshot:
        return cls(
            folder=folder,
            originals={
                name: (folder / name).read_bytes()
                if (folder / name).is_file()
                else None
                for name in names
            },
        )

    def verify_unchanged(self) -> None:
        for name, original in self.originals.items():
            target = self.folder / name
            current = target.read_bytes() if target.is_file() else None
            require(
                current == original, "Review file changed during rendering: " + name
            )

    def publish(self, fresh: Path) -> None:
        self.verify_unchanged()
        published: list[tuple[str, bytes]] = []
        try:
            for name in sorted(self.originals):
                target = self.folder / name
                current = target.read_bytes() if target.is_file() else None
                require(
                    current == self.originals[name],
                    "Review file changed during rendering: " + name,
                )
                content = (fresh / name).read_bytes()
                atomic_write(target, content)
                published.append((name, content))
                require(
                    target.read_bytes() == content,
                    "Review file failed readback: " + name,
                )
        except BaseException as error:
            not_restored = []
            for name, content in reversed(published):
                target = self.folder / name
                current = target.read_bytes() if target.is_file() else None
                if current != content:
                    not_restored.append(name)
                    continue
                original = self.originals[name]
                try:
                    if original is None:
                        target.unlink()
                    else:
                        atomic_write(target, original)
                except OSError:
                    not_restored.append(name)
            if not_restored:
                raise RuntimeError(
                    "Review publication could not safely restore: "
                    + ", ".join(not_restored)
                ) from error
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument(
        "--kicad-python",
        type=Path,
        default=Path(r"C:\Program Files\KiCad\10.0\bin\python.exe"),
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    add_output_arguments(parser)
    args = parser.parse_args(argv)
    console = Console(json_mode=args.json, verbose=args.verbose)
    try:
        return refresh(args, console)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        console.error(error)
        return 2


def refresh(args: argparse.Namespace, console: Console) -> int:
    console.stage("1/5 | Checking saved design and review destination")

    project = args.project.resolve()
    review = args.review.resolve()
    kicad_python = args.kicad_python.resolve()
    python = args.python.resolve()
    board = native_path(project, ".kicad_pcb")
    schematic = native_path(project, ".kicad_sch")
    require(
        board.is_file() and schematic.is_file(), "Expected live v4.1 PCB and schematic"
    )
    require(
        review.is_dir() and review == (project / "deliverables" / "review").resolve(),
        "Review destination must be the canonical project review folder",
    )
    require(kicad_python.is_file(), "KiCad Python runtime was not found")
    require(python.is_file(), "Raster Python runtime was not found")

    require(
        "\n\t(sheet\n" not in schematic.read_text(encoding="utf-8"),
        "One flattened v4.1 schematic source required; found hierarchy sheets",
    )
    source_paths = [board, schematic]
    # The root is the complete editable circuit, so gallery provenance binds
    # directly to that single source rather than a hierarchy overview.
    source_hashes = {
        path.relative_to(project).as_posix(): digest(path) for path in source_paths
    }
    full_hashes = {
        path.relative_to(project).as_posix(): digest(path)
        for path in [
            *source_paths,
            native_path(project, ".kicad_pro"),
            native_path(project, ".kicad_dru"),
        ]
    }
    if gallery_is_current(project, review, source_hashes):
        require(
            full_hashes
            == {
                path.relative_to(project).as_posix(): digest(path)
                for path in [
                    *source_paths,
                    native_path(project, ".kicad_pro"),
                    native_path(project, ".kicad_dru"),
                ]
            },
            "Native project settings changed during the review freshness check",
        )
        console.result(
            {
                "status": "unchanged",
                "review": str(review),
                "pdfs_refreshed": 0,
                "pngs_refreshed": 0,
            },
            show_gallery,
        )
        return 0
    tool_dir = asset_root(project) / "scripts" / "review"
    require(
        all(
            (tool_dir / name).is_file()
            for name in (
                "render_views.py",
                "rasterize_gallery.py",
                "build_standalone_gallery.py",
            )
        ),
        "Required review-render modules are missing",
    )
    tool_env = os.environ.copy()
    existing_pythonpath = tool_env.get("PYTHONPATH", "")
    tool_env["PYTHONPATH"] = str(asset_root(project) / "scripts") + (
        os.pathsep + existing_pythonpath if existing_pythonpath else ""
    )
    tool_env["PYTHONDONTWRITEBYTECODE"] = "1"
    header = (
        (native_path(project, ".kicad_pcb"))
        .read_text(encoding="utf-8-sig")
        .split("(setup", 1)[0]
    )
    layers = re.findall(r'\(\d+\s+"((?:F|B|In[0-9]+)\.Cu)"\s', header)
    pdfs, pngs, active = required_outputs(layers)
    original_review = ReviewSnapshot.capture(review, active)

    with tempfile.TemporaryDirectory(prefix="v41-live-review-") as work:
        root = Path(work)
        run(
            [
                str(kicad_python),
                "-B",
                "-m",
                "review.render_views",
                "--project",
                str(project),
                "--out",
                str(root),
            ],
            "2/5 | Rendering native PDFs and 3D views",
            tool_env,
            console=console,
        )
        gallery = root / "gallery"
        run(
            [str(python), "-B", "-m", "review.rasterize_gallery", str(gallery)],
            "3/5 | Creating PNGs and gallery",
            tool_env,
            console=console,
        )
        run(
            [
                str(python),
                "-B",
                "-m",
                "review.build_standalone_gallery",
                "--views",
                str(gallery),
                "--out",
                str(gallery / "index_standalone.html"),
                "--status",
                (
                    "Current CAD snapshot. Routing and manufacturing remain on "
                    "hold pending validation."
                ),
            ],
            "4/5 | Building standalone gallery",
            tool_env,
            console=console,
        )
        console.stage("5/5 | Verifying exports and publishing the current review set")
        verify_gallery(gallery, source_hashes)
        require(
            (gallery / "index_standalone.html")
            .read_text(encoding="utf-8")
            .count("data:image/png;base64,")
            == len(pngs),
            "Standalone gallery view count does not match the saved copper stack",
        )
        require(
            source_hashes
            == {
                path.relative_to(project).as_posix(): digest(path)
                for path in source_paths
            },
            "Live PCB or schematic changed during rendering; refusing mixed review output",
        )

        require(
            full_hashes
            == {
                path.relative_to(project).as_posix(): digest(path)
                for path in [
                    *source_paths,
                    native_path(project, ".kicad_pro"),
                    native_path(project, ".kicad_dru"),
                ]
            },
            "Native project settings changed during rendering; refusing mixed review output",
        )
        dependencies = json.loads(
            (gallery / "source.json").read_text(encoding="utf-8")
        )["dependency_sha256"]
        current_dependencies = dependency_paths(project)
        require(
            current_dependencies.keys() == dependencies.keys(),
            "Review dependency inventory changed during rendering",
        )
        dependency_root = long_path(project)
        for relative, expected in dependencies.items():
            dependency = current_dependencies[relative].resolve()
            require(
                dependency.is_relative_to(dependency_root)
                and dependency.is_file()
                and digest(dependency) == expected,
                "Review dependency changed during rendering: " + relative,
            )
        original_review.publish(gallery)
        # The delivery identity may contain reviewed claims; do not rewrite it.

    console.result(
        {
            "review": str(review),
            "pdfs_refreshed": len(pdfs),
            "pngs_refreshed": len(pngs),
            "view_count": len(pngs),
            "source_sha256": source_hashes,
            "standalone_embedded": True,
            "redundant_snapshot_created": False,
        },
        show_gallery,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
