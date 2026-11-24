"""test_release_guards.py: check held and source-bound factory review workflows on
temporary fixtures.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from core.export_helpers import assert_factory_sources, factory_input_dir, sha
from release import build_technical_attachment
from review import package_review
from verification import audit_release_consistency


class ReleaseGuardTests(unittest.TestCase):
    """Checks the hold, source binding and output rules of the release workflows."""

    def held_project(self, root: Path) -> Path:
        """Create a project whose release input manifest is stale.

        Inputs:
            root: Folder that receives the project.
        Outputs:
            The project folder.
        """
        project = root / "project"
        inputs = project / "design" / "release_inputs"
        inputs.mkdir(parents=True)
        (inputs / "INPUT_MANIFEST.json").write_text(
            json.dumps({"status": "STALE", "files": {}}), encoding="utf-8"
        )
        return project

    def attachment_fixture(self, root: Path, input_folder: str = "design"):
        """Create the folders that the technical attachment builder reads.

        The project holds a reviewed input manifest, a stackup record with calculator
        evidence, the fabrication files and the schematic and board files. The
        manufacturing folder holds a source identity, the manufacturing files and a hash
        manifest. The gallery holds a source record and the silkscreen and 3D images.

        Inputs:
            root: Folder that receives the three folders.
            input_folder: Folder inside the project that holds the release inputs.
        Outputs:
            The project, manufacturing and gallery folders.
        """
        project = root / "project"
        inputs = project / input_folder
        control = inputs / "release_inputs"
        control.mkdir(parents=True)
        (control / "INPUT_MANIFEST.json").write_text(
            json.dumps({"status": "REVIEWED", "files": {}}), encoding="utf-8"
        )
        for name in ("v4.1.kicad_sch", "v4.1.kicad_pcb"):
            (project / name).write_text(name, encoding="utf-8")
        source = {
            name: sha(project / name) for name in ("v4.1.kicad_sch", "v4.1.kicad_pcb")
        }
        stack = {
            "stackup_id": "fixture",
            "calculator": {"method": "fixture"},
            "dielectric_thicknesses_mm": [0.1, 1.0, 0.1],
            "inner_copper_oz": 2,
            "minimum_inner_copper_oz": 2,
            "copper_layers": 4,
            "maximum_copper_layers": 4,
            "evidence_files": {},
        }
        evidence = inputs / "stackup_evidence" / "calculator.txt"
        evidence.parent.mkdir()
        evidence.write_text("fixture calculator evidence", encoding="utf-8")
        stack["evidence_files"]["design/stackup_evidence/calculator.txt"] = sha(
            evidence
        )
        (inputs / "STACKUP_CURRENT.json").write_text(
            json.dumps(stack), encoding="utf-8"
        )
        design_files = [
            "USB_HOST_POWER_GATE.md",
            "fabrication/FABRICATION_SUPPLEMENTS.json",
            "fabrication/REQUIRED_IMPEDANCE_CONTROL.xlsx",
            "fabrication/IMPEDANCE_POSITIONS.jpg",
            "fabrication/FABRICATION_NOTES.txt",
            "fabrication/FACTORY_MODEL_HOLDS.csv",
            "fabrication/evidence/NATIVE_IMPEDANCE_GEOMETRY.json",
            "fabrication/evidence/ALL_DIFFERENTIAL_TRACKS.csv",
            "fabrication/evidence/IMPEDANCE_MANIFEST.json",
        ]
        for name in design_files:
            file = inputs / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("fixture", encoding="utf-8")
        manufacturing = root / "manufacturing"
        manufacturing.mkdir()
        identity = {
            "source_sha256": source,
            "dependency_sha256": {},
            "copper_layers": 4,
            "factory_cpl_rows": 1,
            "selectively_filled_capped_vias": 0,
        }
        (manufacturing / "SOURCE_IDENTITY.json").write_text(
            json.dumps(identity), encoding="utf-8"
        )
        files = [
            "01_PCB_GERBERS_DRILLS.zip",
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
        for name in files:
            file = manufacturing / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("fixture", encoding="utf-8")
        hashes = {
            p.relative_to(manufacturing).as_posix(): sha(p)
            for p in manufacturing.rglob("*")
            if p.is_file()
        }
        (manufacturing / "MANIFEST_SHA256.json").write_text(
            json.dumps({"source_sha256": source, "files": hashes}), encoding="utf-8"
        )
        gallery = root / "gallery"
        gallery.mkdir()
        (gallery / "source.json").write_text(
            json.dumps(
                {
                    "sha256": source,
                    "copper_layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
                }
            ),
            encoding="utf-8",
        )
        for name in (
            "top_silkscreen.pdf",
            "bottom_silkscreen.pdf",
            "3D_top.png",
            "3D_bottom.png",
        ):
            (gallery / name).write_text("fixture", encoding="utf-8")
        return project, manufacturing, gallery

    def test_technical_attachment_leaves_no_scratch_folder(self):
        with tempfile.TemporaryDirectory(prefix="jessie-attachment-test-") as directory:
            root = Path(directory)
            project, manufacturing, gallery = self.attachment_fixture(root)
            output = root / "attachment.zip"
            build_technical_attachment.TechnicalAttachment(
                project, manufacturing, gallery, output
            ).build()
            self.assertTrue(output.is_file())
            self.assertFalse((root / "attachment_inputs").exists())

    def test_technical_attachment_uses_data_after_input_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, manufacturing, gallery = self.attachment_fixture(
                root, input_folder="data"
            )
            output = root / "attachment.zip"
            build_technical_attachment.TechnicalAttachment(
                project, manufacturing, gallery, output
            ).build()
            with zipfile.ZipFile(output) as archive:
                self.assertIn("data/STACKUP_CURRENT.json", archive.namelist())
                self.assertIn(
                    "data/stackup_evidence/calculator.txt", archive.namelist()
                )
                self.assertFalse(
                    any(name.startswith("design/") for name in archive.namelist())
                )

    def test_technical_attachment_does_not_overwrite_existing_receipt(self):
        with tempfile.TemporaryDirectory(prefix="jessie-attachment-test-") as directory:
            root = Path(directory)
            project, manufacturing, gallery = self.attachment_fixture(root)
            output = root / "attachment.zip"
            receipt = root / "attachment.receipt.json"
            receipt.write_text("unrelated receipt", encoding="utf-8")
            with self.assertRaises((RuntimeError, FileExistsError)):
                build_technical_attachment.TechnicalAttachment(
                    project, manufacturing, gallery, output
                ).build()
            self.assertEqual(receipt.read_text(encoding="utf-8"), "unrelated receipt")
            self.assertFalse(output.exists())

    def test_technical_attachment_preserves_output_created_during_validation(self):
        with tempfile.TemporaryDirectory(prefix="jessie-attachment-test-") as directory:
            root = Path(directory)
            project, manufacturing, gallery = self.attachment_fixture(root)
            output = root / "attachment.zip"
            original_validate = build_technical_attachment.validate_generated_files

            def concurrently_create(*args):
                """Run the real validation, then create the output file as a concurrent
                writer would.
                """
                original_validate(*args)
                output.write_bytes(b"another file")

            with mock.patch.object(
                build_technical_attachment,
                "validate_generated_files",
                side_effect=concurrently_create,
            ):
                with self.assertRaises((RuntimeError, FileExistsError)):
                    build_technical_attachment.TechnicalAttachment(
                        project, manufacturing, gallery, output
                    ).build()
            self.assertEqual(output.read_bytes(), b"another file")

    def test_independent_audit_preserves_existing_report(self):
        with tempfile.TemporaryDirectory(prefix="jessie-audit-test-") as directory:
            root = Path(directory)
            project = root / "project"
            manufacturing = root / "manufacturing"
            project.mkdir()
            manufacturing.mkdir()
            output = root / "audit.json"
            output.write_text("unrelated report", encoding="utf-8")
            args = [
                "audit_release_consistency",
                "--project",
                str(project),
                "--manufacturing",
                str(manufacturing),
                "--out",
                str(output),
            ]
            result = {
                "all_consistency_checks_passed": True,
                "check_count": 0,
                "counts": {},
                "failures": [],
            }
            with mock.patch.object(sys, "argv", args):
                with mock.patch.object(
                    audit_release_consistency, "audit", return_value=result
                ):
                    with self.assertRaises((RuntimeError, FileExistsError)):
                        audit_release_consistency.main()
            self.assertEqual(output.read_text(encoding="utf-8"), "unrelated report")

    def test_missing_review_status_does_not_clear_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.held_project(Path(directory))
            manifest = project / "design/release_inputs/INPUT_MANIFEST.json"
            manifest.write_text(json.dumps({"files": {}}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "on hold"):
                factory_input_dir(project)

    def test_package_respects_hold_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self.held_project(root)
            output = root / "package"
            args = [
                "package_review",
                "--project",
                str(project),
                "--generated",
                str(root / "generated"),
                "--out",
                str(output),
            ]
            with mock.patch.object(sys, "argv", args):
                with self.assertRaisesRegex(RuntimeError, "on hold"):
                    package_review.main()
            self.assertFalse(output.exists())

    def test_technical_attachment_respects_hold_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self.held_project(root)
            output = root / "new" / "attachment.zip"
            with self.assertRaisesRegex(RuntimeError, "on hold"):
                build_technical_attachment.TechnicalAttachment(
                    project, root / "generated", root / "views", output
                ).build()
            self.assertFalse(output.parent.exists())

    @unittest.skipUnless(
        importlib.util.find_spec("pcbnew"), "KiCad Python is not available"
    )
    def test_factory_build_respects_hold_before_creating_output(self):
        from release import generate_factory_review

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self.held_project(root)
            output = root / "generated"
            args = [
                "generate_factory_review",
                "--project",
                str(project),
                "--out",
                str(output),
            ]
            with mock.patch.object(sys, "argv", args):
                with self.assertRaisesRegex(RuntimeError, "on hold"):
                    generate_factory_review.main()
            self.assertFalse(output.exists())

    def test_factory_identity_requires_both_native_sources_and_project_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            schematic = project / "one.kicad_sch"
            board = project / "one.kicad_pcb"
            schematic.write_text("schematic", encoding="utf-8")
            board.write_text("board", encoding="utf-8")
            sources = {schematic.name: sha(schematic), board.name: sha(board)}
            assert_factory_sources(project, sources, {})
            with self.assertRaisesRegex(RuntimeError, "one schematic and one PCB"):
                assert_factory_sources(project, {schematic.name: sha(schematic)}, {})
            external = root / "outside"
            external.write_text("outside", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "project file"):
                assert_factory_sources(project, sources, {"../outside": sha(external)})

    def test_technical_attachment_checks_generated_file_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            manufacturing = Path(directory)
            generated = manufacturing / "bom.csv"
            generated.write_text("original", encoding="utf-8")
            source = {"one.kicad_pcb": "test"}
            manifest = manufacturing / "MANIFEST_SHA256.json"
            manifest.write_text(
                json.dumps(
                    {"source_sha256": source, "files": {generated.name: sha(generated)}}
                ),
                encoding="utf-8",
            )
            entries = [(generated, generated.name)]
            build_technical_attachment.validate_generated_files(
                manufacturing, source, entries
            )
            generated.write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "input changed"):
                build_technical_attachment.validate_generated_files(
                    manufacturing, source, entries
                )
            with self.assertRaisesRegex(RuntimeError, "Manifest/source mismatch"):
                build_technical_attachment.validate_generated_files(
                    manufacturing, {}, entries
                )


if __name__ == "__main__":
    unittest.main()
