"""build_technical_attachment.py: build one compact, source-bound JLC review attachment; no CAD or order writes.

Copyright: GRANDE Robots and Aaron John Sabu
"""

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


class GeneratedFileCheck:
    """Checks the generated files an attachment takes against the manufacturing manifest."""

    def __call__(self, manufacturing, source, entries):
        """Require the manifest to match the source and every generated entry to be unchanged.

        Inputs:
            manufacturing: Generated ``jlcpcb`` folder with ``MANIFEST_SHA256.json``.
            source: Source hashes the attachment is bound to.
            entries: Pairs of file path and archive name.
        Outputs: None. Raises RuntimeError when the manifest belongs to another
            source, a file is missing, or a file inside the folder differs from
            its manifest hash.
        """
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


validate_generated_files = GeneratedFileCheck()


class TechnicalAttachment:
    """One compact review archive and its receipt, built from a generated manufacturing set."""

    def __init__(self, project, manufacturing, gallery, out):
        """Resolve the folders and the archive path.

        Inputs:
            project: Native project folder.
            manufacturing: Generated ``jlcpcb`` folder.
            gallery: Review gallery folder with ``source.json``.
            out: New archive file, outside the project; its receipt is written
                beside it with the suffix ``.receipt.json``.
        """
        self.project = Path(project).resolve()
        self.manufacturing = Path(manufacturing).resolve()
        self.gallery = Path(gallery).resolve()
        self.out = Path(out).resolve()

    def check_gallery(self, identity):
        """Require the gallery to be built from the same source and copper stack.

        Inputs:
            identity: The parsed ``SOURCE_IDENTITY.json``.
        Outputs:
            The parsed gallery ``source.json``. Raises RuntimeError on a source
            or copper layer mismatch.
        """
        view = json.loads(
            (self.gallery / "source.json").read_text(encoding="utf-8-sig")
        )
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
        return view

    def read_stack(self, identity):
        """Read the current stack observations and require them to be complete and consistent.

        Inputs:
            identity: The parsed ``SOURCE_IDENTITY.json``.
        Outputs:
            The parsed ``STACKUP_CURRENT.json``. Raises RuntimeError when the
            observations are incomplete, the inner copper is below the reviewed
            requirement or the layer counts disagree with the native design.
        """
        stack = json.loads(
            (data_path(self.project, "STACKUP_CURRENT.json")).read_text(
                encoding="utf-8-sig"
            )
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
        return stack

    def entries(self, stack):
        """List every file the attachment keeps and the name it has in the archive.

        Inputs:
            stack: The parsed ``STACKUP_CURRENT.json``.
        Outputs:
            Pairs of file path and archive name. Raises RuntimeError when a stack
            evidence file is missing or changed.
        """
        P, M, V = self.project, self.manufacturing, self.gallery
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
                evidence.is_file() and sha(evidence) == h,
                "Stack evidence changed: " + n,
            )
            member = (
                (Path("data") / Path(n.removeprefix("design/"))).as_posix()
                if n.startswith("design/")
                else n
            )
            entries.append((evidence, member))
        return entries

    @staticmethod
    def readme_text(factory_count, view_count, fill_count):
        """Write the attachment's ``README.txt``.

        Inputs:
            factory_count: Factory-assembled references per carrier.
            view_count: Views in the full review gallery.
            fill_count: Selectively filled and capped vias.
        """
        return (
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
        )

    def build(self):
        """Verify the sources and write the archive and its receipt.

        Outputs:
            The receipt as a dict. Raises RuntimeError when the output is inside
            the project or already exists, the manufacturing set, gallery or
            stack does not match the current source, the archive reaches
            ``MAX_BYTES`` or fails readback, and FileExistsError when the archive
            or receipt appears during the build.
        """
        P, M, output_dir = self.project, self.manufacturing, self.out
        require(
            not output_dir.is_relative_to(P),
            "Keep generated attachments outside source project",
        )
        require(not output_dir.exists(), "Use a new attachment filename")
        receipt_path = output_dir.with_suffix(".receipt.json")
        require(not receipt_path.exists(), "Use a new receipt filename")
        factory_input_dir(P)
        identity = json.loads(
            (M / "SOURCE_IDENTITY.json").read_text(encoding="utf-8-sig")
        )
        source = identity["source_sha256"]
        assert_factory_sources(P, source, identity["dependency_sha256"])
        view = self.check_gallery(identity)
        view_count = len(view["copper_layers"]) + 7
        factory_count = identity["factory_cpl_rows"]
        fill_count = identity["selectively_filled_capped_vias"]
        stack = self.read_stack(identity)
        entries = self.entries(stack)
        validate_generated_files(M, source, entries)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="jessie-attachment-") as work:
            scratch = Path(work)
            readme = scratch / "README.txt"
            readme.write_text(
                self.readme_text(factory_count, view_count, fill_count),
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


class BuildTechnicalAttachmentCommand:
    """Command-line entry: build the compact review attachment and print its receipt."""

    def __call__(self, argv=None):
        """Parse the command line, build the attachment and print the receipt as JSON.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        """
        p = argparse.ArgumentParser()
        p.add_argument("--project", type=Path, required=True)
        p.add_argument("--manufacturing", type=Path, required=True)
        p.add_argument("--gallery", type=Path, required=True)
        p.add_argument("--out", type=Path, required=True)
        a = p.parse_args(argv)
        attachment = TechnicalAttachment(a.project, a.manufacturing, a.gallery, a.out)
        print(json.dumps(attachment.build(), indent=2))


main = BuildTechnicalAttachmentCommand()

if __name__ == "__main__":
    main()
