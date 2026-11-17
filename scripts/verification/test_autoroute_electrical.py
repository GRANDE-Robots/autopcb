"""Branch current and voltage-drop checks must fail closed on missing evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path


class ElectricalNetworkTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec("pcb.net_routing.electrical"))
        from pcb.net_routing import electrical

        return electrical

    def fixture(self, project):
        api = self.api()
        document = project / "ratings.txt"
        document.write_text("Reviewed design envelope", encoding="utf-8")
        evidence = api.Evidence("ratings.txt", hashlib.sha256(document.read_bytes()).hexdigest(), "load table", "verified")
        def quantity(value, unit):
            return api.Quantity(value, unit, evidence)
        branches = (
            api.Branch("trunk", "supply", "split", "track", quantity(1.4, "A"), quantity(0.1, "ohm")),
            api.Branch("left", "split", "left-load", "track", quantity(1.0, "A"), quantity(0.2, "ohm")),
            api.Branch("right", "split", "right-load", "via", quantity(0.6, "A"), quantity(0.05, "ohm")),
        )
        scenarios = (api.Scenario("both", (("left-load", quantity(1, "A")), ("right-load", quantity(0.5, "A"))), quantity(0.4, "V")),)
        return api, quantity, branches, scenarios

    def test_shared_trunk_carries_the_sum_of_simultaneous_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            api, _, branches, scenarios = self.fixture(project)
            result = api.assess_tree(project, "supply", branches, scenarios)
            self.assertEqual(result.status, "rejected")
            self.assertAlmostEqual(result.scenarios[0].branch_currents_a["trunk"], 1.5)
            self.assertAlmostEqual(result.scenarios[0].branch_currents_a["right"], 0.5)
            self.assertTrue(any("trunk" in issue and "current" in issue for issue in result.issues))

    def test_path_drop_accounts_for_trunk_and_branch_currents(self):
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            api, quantity, branches, scenarios = self.fixture(project)
            branches = (replace(branches[0], capacity=quantity(2, "A")), *branches[1:])
            result = api.assess_tree(project, "supply", branches, scenarios)
            self.assertEqual(result.status, "within_input_limits")
            self.assertFalse(result.electrically_verified)
            self.assertAlmostEqual(result.scenarios[0].load_drops_v["left-load"], 0.35)
            self.assertAlmostEqual(result.scenarios[0].load_drops_v["right-load"], 0.175)

    def test_unverified_or_changed_evidence_blocks_assessment(self):
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            api, quantity, branches, scenarios = self.fixture(project)
            unknown = api.Evidence("ratings.txt", "", "load table", "unknown")
            branches = (replace(branches[0], capacity=api.Quantity(10, "A", unknown)), *branches[1:])
            self.assertEqual(api.assess_tree(project, "supply", branches, scenarios).status, "blocked")
            branches = (replace(branches[0], capacity=quantity(10, "A")), *branches[1:])
            (project / "ratings.txt").write_text("changed", encoding="utf-8")
            self.assertEqual(api.assess_tree(project, "supply", branches, scenarios).status, "blocked")

    def test_a_design_target_cannot_establish_a_physical_branch_rating(self):
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            api, _, branches, scenarios = self.fixture(project)
            target = replace(branches[0].capacity.evidence, status="design_target")
            for field, unit in (("capacity", "A"), ("resistance", "ohm")):
                modified = replace(
                    branches[0],
                    **{field: api.Quantity(10, unit, target)},
                )
                result = api.assess_tree(project, "supply", (modified, *branches[1:]), scenarios)
                self.assertEqual(result.status, "blocked")
                self.assertTrue(any(field in issue for issue in result.issues))

    def test_a_design_target_can_specify_a_scenario_limit(self):
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            api, quantity, branches, scenarios = self.fixture(project)
            branches = (replace(branches[0], capacity=quantity(2, "A")), *branches[1:])
            target = replace(scenarios[0].max_drop.evidence, status="design_target")
            scenario = replace(scenarios[0], max_drop=api.Quantity(0.4, "V", target))
            result = api.assess_tree(project, "supply", branches, (scenario,))
            self.assertEqual(result.status, "within_input_limits")
            self.assertFalse(result.electrically_verified)

    def test_cycles_or_multiple_feed_paths_cannot_be_assessed_as_a_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            api, quantity, branches, scenarios = self.fixture(project)
            loop = api.Branch("loop", "right-load", "split", "track", quantity(2, "A"), quantity(0.1, "ohm"))
            with self.assertRaisesRegex(ValueError, "tree"):
                api.assess_tree(project, "supply", (*branches, loop), scenarios)


if __name__ == "__main__":
    unittest.main()
