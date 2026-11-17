"""Zero opens must never conceal incomplete electrical or CAD verification."""

from __future__ import annotations

import importlib.util
import unittest
from dataclasses import replace


class CompletionGateTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec("pcb.net_routing.completion"))
        from pcb.net_routing import completion

        return completion

    def evidence(self):
        api = self.api()
        self.assertIn("unassigned_copper", api.VerificationEvidence.__dataclass_fields__)
        return api.VerificationEvidence(
            open_connections=0,
            errors=0,
            warnings=0,
            parity_errors=0,
            ignored_checks=(),
            unverified_pads=0,
            unassigned_copper=0,
            coverage_complete=True,
            geometry_ok=True,
            electrical_ok=True,
            reference_copper_ok=True,
            protected_unchanged=True,
            views_current=True,
        )

    def test_every_required_check_must_pass_for_completion(self):
        api = self.api()
        evidence = self.evidence()
        self.assertEqual(api.completion_issues(evidence), ())
        for name in ("coverage_complete", "geometry_ok", "electrical_ok", "reference_copper_ok", "protected_unchanged", "views_current"):
            for missing in (False, None):
                with self.subTest(name=name, missing=missing):
                    self.assertTrue(api.completion_issues(replace(evidence, **{name: missing})))

    def test_zero_opens_do_not_hide_warnings_exclusions_or_unverified_pads(self):
        api = self.api()
        evidence = replace(self.evidence(), warnings=1, ignored_checks=("clearance",), unverified_pads=2)
        issues = api.completion_issues(evidence)
        self.assertEqual(len(issues), 3)
        self.assertTrue(any("warning" in issue for issue in issues))
        self.assertTrue(any("ignored" in issue for issue in issues))
        self.assertTrue(any("pad" in issue for issue in issues))

    def test_unassigned_copper_blocks_completion(self):
        api = self.api()
        self.assertTrue(any("unassigned copper" in issue for issue in api.completion_issues(replace(self.evidence(), unassigned_copper=1))))

    def test_invalid_or_unknown_counts_cannot_pass(self):
        api = self.api()
        for value in (None, -1, True):
            with self.subTest(value=value):
                issues = api.completion_issues(replace(self.evidence(), open_connections=value))
                self.assertTrue(issues)


if __name__ == "__main__":
    unittest.main()
