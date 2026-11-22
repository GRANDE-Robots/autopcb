"""Prepare a complete, source-bound board inventory without editing the source."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from verification import test_route_optimizer as route_fixtures
from pcb.route_validation import _drc
from core.toolchain import kicad_cli


class PrepareTests(unittest.TestCase):
    def test_parity_conflicts_identify_both_sides_or_fail_closed(self):
        api = self.api()
        helper = getattr(api, "parity_blocked_nets", None)
        self.assertTrue(callable(helper), "parity net isolation is missing")
        finding = {
            "type": "net_conflict",
            "description": "Pad net (OLD) doesn't match net given by schematic (NEW)",
            "items": [{"uuid": "pad-1", "description": "Pad 7 [OLD] of D21"}],
        }
        self.assertEqual(helper({"schematic_parity": [finding]}, {"pad-1": "OLD"}),
                         ("NEW", "OLD"))
        self.assertEqual(helper({"schematic_parity": []}, {}), ())
        for report, pads in (
            ({"schematic_parity": [finding]}, {}),
            ({"schematic_parity": [finding]}, {"pad-1": "OTHER"}),
            ({"schematic_parity": [{**finding, "type": "unknown"}]}, {"pad-1": "OLD"}),
            ({"schematic_parity": [{**finding, "description": "unreadable"}]}, {"pad-1": "OLD"}),
        ):
            with self.subTest(report=report, pads=pads), self.assertRaises(ValueError):
                helper(report, pads)

    def test_existing_parity_conflict_nets_cannot_be_rerouted(self):
        from collections import namedtuple

        api = self.api()
        helper = getattr(api, "verify_parity_copper_unchanged", None)
        self.assertTrue(callable(helper), "parity copper guard is missing")
        item = namedtuple("Item", "net location")
        baseline = {"parity_findings": 1, "parity_blocked_nets": ["NEW", "OLD"]}
        prior = [item("OLD", 1), item("NEW", 2), item("SAFE", 3)]
        helper(baseline, prior, [item("OLD", 1), item("NEW", 2), item("SAFE", 4)])
        with self.assertRaisesRegex(RuntimeError, "parity"):
            helper(baseline, prior, [item("OLD", 5), item("NEW", 2), item("SAFE", 3)])
        with self.assertRaisesRegex(RuntimeError, "parity"):
            helper({"parity_findings": 1, "parity_blocked_nets": None}, prior, prior)

    def test_native_drc_includes_every_severity(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-all-severities-") as tmp:
            source = route_fixtures.RouteOptimizerTests.make_board(Path(tmp))
            cli = kicad_cli()
            report = _drc(cli, source, Path(tmp) / "drc.json")
            self.assertTrue({"error", "warning", "exclusion"} <= set(report["included_severities"]))

    def test_native_parity_failure_cannot_be_reported_as_zero_findings(self):
        with tempfile.TemporaryDirectory(prefix="autopcb-missing-schematic-") as tmp:
            source = route_fixtures.RouteOptimizerTests.make_board(Path(tmp))
            cli = kicad_cli()
            # KiCad 10 can exit successfully and emit an empty parity array
            # even though it failed to fetch the schematic netlist.
            with self.assertRaisesRegex(RuntimeError, "(?i)parity|schematic"):
                _drc(cli, source, Path(tmp) / "drc.json", schematic_parity=True)

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec("pcb.net_routing.prepare"))
        from pcb.net_routing import prepare

        return prepare

    def test_incomplete_or_suppressed_drc_is_not_a_pass(self):
        api = self.api()
        report = {"violations": [], "unconnected_items": [], "schematic_parity": [], "included_severities": ["error", "warning", "exclusion"], "ignored_checks": ["clearance"]}
        result = api.summarize_drc(report)
        self.assertEqual(result["ignored_checks"], ["clearance"])
        self.assertFalse(result["complete"])
        del report["schematic_parity"]
        with self.assertRaisesRegex(ValueError, "parity"):
            api.summarize_drc(report)

    def test_native_ignored_check_records_are_reported_by_key(self):
        api = self.api()
        report = {"violations": [], "unconnected_items": [], "schematic_parity": [], "included_severities": ["error", "warning", "exclusion"], "ignored_checks": [{"key": "missing_courtyard", "description": "no courtyard"}]}
        try:
            result = api.summarize_drc(report)
        except ValueError as error:
            self.fail(f"Native KiCad ignored-check record was rejected: {error}")
        self.assertEqual(result["ignored_checks"], ["missing_courtyard"])

    def test_native_preparation_keeps_saved_sources_and_includes_all_nets(self):
        api = self.api()
        schematic = '''(kicad_sch (version 20250114) (generator "eeschema")
          (uuid "6b6b7707-0425-4195-9c79-1824e2f1a24e") (paper "A4")
          (lib_symbols) (sheet_instances (path "/" (page "1"))) (embedded_fonts no))'''
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = route_fixtures.RouteOptimizerTests.make_board(project, crossing=True)
            (project / "v4.1.kicad_sch").write_text(schematic, encoding="utf-8")
            # The route fixture intentionally writes a partial synthetic stackup;
            # let KiCad normalize it before recording this test's baseline.
            cli = kicad_cli()
            _drc(cli, source, project / "fixture-drc.json", save_refill=True)
            before = {p.name: p.read_bytes() for p in project.iterdir() if p.is_file()}
            result = api.prepare_project(project)
            after = {p.name: p.read_bytes() for p in project.iterdir() if p.is_file()}
            self.assertEqual(before, after)
            self.assertEqual({item["name"] for item in result["nets"]}, {"SIGNAL", "OTHER"})
            self.assertIn("work", result)
            self.assertEqual([item["name"] for item in result["work"]["items"]], ["SIGNAL"])
            self.assertIn("OTHER", result["work"]["blocked_nets"])
            self.assertIn("style", result)
            self.assertIn("passed", result["style"])
            self.assertIn("reference_copper", result)
            self.assertIn(result["reference_copper"]["status"], {"sampled_unverified", "missing_project_rules"})
            self.assertFalse(result["reference_copper"]["electrically_verified"])
            self.assertGreater(result["drc"]["open_connections"], 0)
            self.assertEqual(result["status"], "prepared_incomplete")
            self.assertFalse(result["publication_enabled"])
            self.assertTrue(result["completion_issues"])


if __name__ == "__main__":
    unittest.main()
