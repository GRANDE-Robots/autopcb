"""Regression tests for omission, stale metadata and archive identity failures."""

import json
import tempfile
import unittest
from pathlib import Path

from core.export_helpers import (
    FILES,
    assert_current_inputs,
    controlled_input,
    exact_metadata,
    factory_input_dir,
    feature_kind,
    group_bom,
    readcsv,
    sha,
    stable_zip,
    writecsv,
)


class ExportContractTests(unittest.TestCase):
    def test_new_native_part_needs_no_old_register_row(self):
        evidence = []
        r = exact_metadata(
            {"MPN": "NEW", "Manufacturer": "TI", "LCSC": "C42"}, {}, "U99", evidence
        )
        self.assertEqual(r["MPN"], "NEW")
        self.assertTrue(all(x["Source"].startswith("schematic:") for x in evidence))

    def test_native_mpn_change_overrides_stale_register(self):
        e = []
        r = exact_metadata(
            {"MPN": "DMG2305UX-7", "Manufacturer": "Diodes", "LCSC": "C150470"},
            {"MPN": "DMP2305U-7", "Manufacturer": "old", "LCSC Part #": "old"},
            "Q4",
            e,
        )
        self.assertEqual(r["MPN"], "DMG2305UX-7")
        self.assertEqual(r["LCSC Part #"], "C150470")

    def test_explicit_blank_mpn_does_not_fallback(self):
        with self.assertRaises(RuntimeError):
            exact_metadata(
                {"MPN": "", "Manufacturer": "TI"}, {"MPN": "STALE"}, "U1", []
            )

    def test_absent_field_may_use_controlled_fallback(self):
        e = []
        r = exact_metadata(
            {"Manufacturer": "TI"}, {"MPN": "EXACT", "LCSC Part #": ""}, "U1", e
        )
        self.assertEqual(r["MPN"], "EXACT")
        self.assertIn("fallback", e[1]["Source"])

    def test_native_empty_lcsc_retained_for_global_sourcing(self):
        r = exact_metadata(
            {"MPN": "EXACT", "Manufacturer": "TI", "LCSC": ""},
            {"LCSC Part #": "STALE"},
            "U1",
            [],
        )
        self.assertEqual(r["LCSC Part #"], "")

    def test_reference_prefix_alone_never_omits_purchase(self):
        self.assertIsNone(feature_kind("TP99", "Connector_PogoPin", {"MPN": "123"}, {}))
        self.assertIsNone(feature_kind("TP99", "Unknown", {}, {}))

    def test_only_verified_feature_family_is_classified(self):
        self.assertEqual(
            feature_kind("TP99", "TestPoint_Pad_D1.0mm", {}, {}), "Fabricated test pad"
        )

    def test_dynamic_group_counts_include_added_parts_and_modules(self):
        parts = [
            dict(
                Designator=r,
                Comment=v,
                Footprint=fp,
                Manufacturer=m,
                MPN=mpn,
                **{"LCSC Part #": lcsc},
            )
            for r, v, fp, m, mpn, lcsc in [
                ("R1", "10k", "0603", "Yageo", "EXACT", "C1"),
                ("R2", "10k", "0603", "Yageo", "EXACT", "C1"),
                ("U99", "New chip", "QFN", "TI", "NEW", "C42"),
                ("U3", "Teensy", "Module", "PJRC", "OWNER", ""),
            ]
        ]
        bom = group_bom(parts)
        self.assertEqual(len(bom), 3)
        self.assertEqual(sum(r["Quantity"] for r in bom), 4)
        self.assertIn("U3", {r for x in bom for r in x["Designator"].split(",")})

    def test_header_only_csv_when_no_filled_vias(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "empty.csv"
            writecsv(p, [], ["UUID", "Net"])
            self.assertEqual(readcsv(p), [])

    def test_archive_deterministic_and_byte_verified(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "a").write_text("abc")
            (p / "b").write_text("def")
            stable_zip(p / "one.zip", [(p / "a", "a"), (p / "b", "b")])
            stable_zip(p / "two.zip", [(p / "b", "b"), (p / "a", "a")])
            self.assertEqual(sha(p / "one.zip"), sha(p / "two.zip"))

    def test_ambiguous_legacy_register_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "06_EXACT_PART_REGISTER_2.csv").touch()
            (p / "06_EXACT_PART_REGISTER_3.csv").touch()
            with self.assertRaises(RuntimeError):
                controlled_input(p, FILES["register"], "06_EXACT_PART_REGISTER_*.csv")

    def test_changed_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            f = p / "board"
            f.write_text("before")
            s = {"board": sha(f)}
            f.write_text("after")
            with self.assertRaises(RuntimeError):
                assert_current_inputs(p, s, {})

    def test_replacing_generated_outputs_preserves_controlled_inputs(self):
        from release.stage_release_inputs import stage

        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            m = p / "deliverables/jlcpcb"
            (m / "assembly").mkdir(parents=True)
            for name in [
                FILES["register"],
                "07_REQUIRED_MODULES_NOT_DNP.csv",
                "09_CONNECTOR_PINOUTS.csv",
                "10_FINISHED_CABLE_SCHEDULE.csv",
                "assembly/PCB_FEATURES_NO_PURCHASE.csv",
                "05_BUILD_AND_ASSEMBLY.md",
            ]:
                (m / name).write_text("controlled old content")
            control = stage(p)
            deps = {f.relative_to(p).as_posix(): sha(f) for f in control.iterdir()}
            for f in m.rglob("*"):
                if f.is_file():
                    f.write_text("new generated content")
            assert_current_inputs(p, {}, deps)
            with self.assertRaisesRegex(RuntimeError, "on hold"):
                factory_input_dir(p)
            manifest = control / "INPUT_MANIFEST.json"
            document = json.loads(manifest.read_text())
            document["status"] = "REVIEWED"
            manifest.write_text(json.dumps(document))
            self.assertEqual(factory_input_dir(p), control)
            (control / "09_CONNECTOR_PINOUTS.csv").write_text("unreviewed change")
            with self.assertRaisesRegex(
                RuntimeError, "Controlled factory input changed"
            ):
                factory_input_dir(p)


if __name__ == "__main__":
    unittest.main()
