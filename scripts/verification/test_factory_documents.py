"""test_factory_documents.py: check generated document consistency, source binding and
locator guards.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import json
import tempfile
import unittest
from pathlib import Path

from core.export_helpers import sha, writecsv, writejson
from release.generate_factory_documents import FactoryDocuments


class FactoryDocumentTests(unittest.TestCase):
    """Checks the factory documents against their source identity and evidence."""

    def fixture(self, root, count=301, fill_count=0):
        """Create a project and a manufacturing folder that agree with each other.

        The project holds the stackup record with its evidence, a build document and the
        reviewed input manifest. The manufacturing folder holds the source identity, the
        assembly scope, test record, rule and drill evidence, the filled via list, the
        register, the required modules and the placement list.

        Inputs:
            root: Folder that receives the project and output folders.
            count: Placement count recorded in the source identity; two of the
                placements are required modules.
            fill_count: Number of filled and capped vias recorded and listed.
        Outputs:
            The project folder, the output folder and the manufacturing folder.
        """
        p = root / "project"
        out = root / "output"
        m = out / "jlcpcb"
        (p / "deliverables/jlcpcb").mkdir(parents=True)
        (m / "evidence").mkdir(parents=True)
        (m / "assembly").mkdir()
        evidence = p / "design/stackup_evidence/test.txt"
        evidence.parent.mkdir(parents=True)
        evidence.write_text("stack evidence")
        writejson(
            p / "design/STACKUP_CURRENT.json",
            dict(
                schema=1,
                stackup_id="test-4-layer",
                copper_layers=4,
                maximum_copper_layers=4,
                nominal_order_thickness_mm=1.6,
                displayed_finished_thickness_mm=1.43,
                displayed_finished_thickness_tolerance_percent=10,
                outer_copper_oz=1,
                inner_copper_oz=2,
                minimum_inner_copper_oz=2,
                copper_thicknesses_mm=[0.035, 0.061, 0.061, 0.035],
                layer_function_status="Test only",
                dielectric_thicknesses_mm=[0.1375, 0.96, 0.1375],
                calculator=dict(
                    geometry="Differential Pair",
                    signal_layer="L1",
                    reference_layer="L2",
                    trace_gap_mm=0.24,
                    solver_tolerance_percent=0.01,
                    inverse_results=[
                        dict(
                            target_ohm=90, native_width_mm=0.22, returned_width_mm=0.22
                        )
                    ],
                ),
                evidence_files={"design/stackup_evidence/test.txt": sha(evidence)},
                factory_acceptance="PENDING",
                production_released=False,
            ),
        )
        (p / "deliverables/jlcpcb/05_BUILD_AND_ASSEMBLY.md").write_text(
            "JLC061612-1080 0.2187 0.1717 0.1375 0.3500 0.3250"
        )
        control = p / "design/release_inputs"
        control.mkdir(parents=True)
        history = control / "STACKUP_REFERENCE.md"
        history.write_text(
            (p / "deliverables/jlcpcb/05_BUILD_AND_ASSEMBLY.md").read_text()
        )
        writejson(
            control / "INPUT_MANIFEST.json",
            {"status": "REVIEWED", "files": {history.name: sha(history)}},
        )
        source = {"v4.1.kicad_sch": "test-sch", "v4.1.kicad_pcb": "test-pcb"}
        identity = dict(
            source_sha256=source,
            copper_layers=4,
            selectively_filled_capped_vias=fill_count,
            files={"register": "register.csv", "bom": "bom.csv", "cpl": "cpl.csv"},
            cpl_rows=count,
            board_components=count - 2,
            required_plugin_modules=2,
            bom_group_rows=147,
            fabricated_feature_types={"Fabricated test pad": 20},
            assembly_categories={"Top SMT": count - 2, "Top Required module": 2},
            datum_mm=[13, 113],
            edge_cuts_centerline_bbox_mm=[100.05, 100.05],
            edge_cuts_bbox_mm=[100.05, 100.05],
            minimum_via_drill_mm=0.15,
            maximum_filled_via_drill_mm=0.3 if fill_count else None,
        )
        identity.update(
            factory_cpl_rows=count - 2, factory_bom_group_rows=100, owner_install_rows=2
        )
        writejson(m / "SOURCE_IDENTITY.json", identity)
        writejson(
            m / "evidence/ASSEMBLY_SCOPE.json",
            dict(
                source_sha256=source,
                factory_parts=count - 2,
                factory_bom_groups=100,
                owner_install_parts=2,
            ),
        )
        writejson(
            m / "evidence/IPC_D356_VALIDATION.json",
            dict(
                source_sha256=source,
                native_pad_records_checked=912,
                native_via_records_checked=333,
                total_test_records=1245,
                unique_connected_signal_aliases=222,
                non_copper_pad_records_excluded=84,
            ),
        )
        writejson(
            m / "evidence/ERC_RULE_CONFIGURATION.json",
            dict(source_sha256=source, ignored_categories=["simulation_model_issue"]),
        )
        holes = [
            dict(reference=ref, kind="NPTH", drill_local_x_mm=x, drill_local_y_mm=y)
            for ref in ["J4", "J28", "J30"]
            for x, y in [(0.65, 0.65), (1.0, 0.65)]
        ]
        writejson(
            m / "evidence/NATIVE_DRILL_MAP.json",
            dict(
                source_sha256=source,
                counts=dict(
                    holes=500, PTH=480, NPTH=20, slots=3, filled_capped_vias=fill_count
                ),
                records=holes,
            ),
        )
        writecsv(
            m / "assembly/EXPLICIT_FILLED_CAPPED_VIAS.csv",
            [{"UUID": str(n)} for n in range(fill_count)],
            ["UUID"],
        )
        writecsv(
            m / "register.csv",
            [{"Designator": "R1", "LCSC Part #": ""}],
            ["Designator", "LCSC Part #"],
        )
        writecsv(
            m / "07_REQUIRED_MODULES_NOT_DNP.csv",
            [{"Designator": "U3"}, {"Designator": "U9"}],
            ["Designator"],
        )
        writecsv(
            m / "cpl.csv",
            [
                {"Designator": "U3", "Mid X": "1", "Mid Y": "2", "Layer": "Top"},
                {"Designator": "R1", "Mid X": "1", "Mid Y": "2", "Layer": "Bottom"},
            ],
            ["Designator", "Mid X", "Mid Y", "Layer"],
        )
        return p, out, m

    def generate(self, p, out):
        """Generate the factory documents of a project into an output folder.

        Inputs:
            p: The project folder.
            out: The output folder.
        """
        FactoryDocuments.generate(p, out)

    def test_dynamic_counts_zero_fills_and_owner_stock(self):
        with tempfile.TemporaryDirectory() as d:
            p, out, m = self.fixture(Path(d), count=301)
            self.generate(p, out)
            s = (m / "05_BUILD_AND_ASSEMBLY.md").read_text()
            self.assertIn("301 required placements", s)
            self.assertIn("299 board parts", s)
            self.assertIn("0 scheduled vias", s)
            self.assertIn("do not buy duplicates", s)
            self.assertIn("one 0.65 mm round", s)
            self.assertIn("1245 test records", s)
            self.assertIn("299 factory-assembled references", s)
            self.assertIn("02_BOM_JLCPCB.csv", s)
            self.assertIn("07_OWNER_FINISHING.csv", s)
            self.assertIn("already fitted", s)
            self.assertNotIn("factory verifies both 24-pin rows", s)
            self.assertNotIn("279 board", s)
            self.assertTrue((m / "08_RELEASE_GATES.csv").exists())
            self.assertTrue((out / "PROJECT_README.md").exists())
        self.assertNotIn("J29", s)
        self.assertIn("CA1.1", s)

    def test_owner_module_count_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p, out, m = self.fixture(Path(d))
            path = m / "evidence/ASSEMBLY_SCOPE.json"
            doc = json.loads(path.read_text())
            doc["owner_install_parts"] = 3
            writejson(path, doc)
            with self.assertRaisesRegex(
                RuntimeError, "Owner-install module count differs"
            ):
                self.generate(p, out)

    def test_stale_usb_round_holes_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p, out, m = self.fixture(Path(d))
            f = m / "evidence/NATIVE_DRILL_MAP.json"
            doc = json.loads(f.read_text())
            doc["records"][1]["drill_local_x_mm"] = 0.7
            doc["records"][1]["drill_local_y_mm"] = 0.7
            writejson(f, doc)
            with self.assertRaisesRegex(RuntimeError, "locating holes not corrected"):
                self.generate(p, out)

    def test_mixed_source_reports_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p, out, m = self.fixture(Path(d))
            f = m / "evidence/IPC_D356_VALIDATION.json"
            doc = json.loads(f.read_text())
            doc["source_sha256"] = {"old": "bad"}
            writejson(f, doc)
            with self.assertRaisesRegex(RuntimeError, "different source"):
                self.generate(p, out)


if __name__ == "__main__":
    unittest.main()
