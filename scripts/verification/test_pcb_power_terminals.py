"""Verify the power audit never reports missing DRC evidence as a clean result."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from verification import audit_pcb_power_terminals as power


class PowerAuditTests(unittest.TestCase):
    def pads(self):
        result = {("J1", pin): net for pin, net in power.EXPECTED_J1.items()}
        for net, terminals in power.REQUIRED_TERMINALS.items():
            for terminal in terminals:
                result[terminal] = net
        return result

    def run_audit(self, pads, arguments=()):
        output = io.StringIO()
        with (
            tempfile.TemporaryDirectory(prefix="power-board-test-") as directory,
            mock.patch.object(power.pcbnew, "LoadBoard", return_value=object()),
            mock.patch.object(power, "pad_map", return_value=pads),
            contextlib.redirect_stdout(output),
        ):
            board = Path(directory) / "board.kicad_pcb"
            board.write_text("saved board", encoding="utf-8")
            with mock.patch.object(
                sys, "argv", ["audit", str(board), "--json", *arguments]
            ):
                code = power.main()
        return code, json.loads(output.getvalue())

    def test_no_drc_report_is_unknown(self):
        code, data = self.run_audit(self.pads())
        self.assertEqual(code, 0)
        self.assertIsNone(data["native_drc_open_edges_by_power_domain"])
        self.assertEqual(data["drc_report_status"], "not supplied")

    def test_missing_lamp_pad_cannot_pass_separation(self):
        pads = self.pads()
        del pads[("J12", "2")]
        code, data = self.run_audit(pads)
        self.assertEqual(code, 2)
        self.assertFalse(data["domain_separation"]["top_minus_is_not_ground"])
        self.assertFalse(data["domain_separation"]["lamp_minus_domains_are_distinct"])

    def test_supplied_report_is_counted_with_unverified_source(self):
        with tempfile.TemporaryDirectory(prefix="jessie-power-test-") as td:
            report = Path(td) / "drc.json"
            report.write_text(
                json.dumps(
                    {
                        "unconnected_items": [
                            {"items": [{"description": "Pad [TOP_LAMP_MINUS]"}]}
                        ]
                    }
                )
            )
            code, data = self.run_audit(self.pads(), ["--drc-json", str(report)])
            self.assertEqual(code, 0)
            self.assertEqual(
                data["native_drc_open_edges_by_power_domain"]["TOP_LAMP_MINUS"], 1
            )
            self.assertEqual(
                data["drc_report_status"], "supplied; source-board match not verified"
            )

    def test_missing_drc_open_list_is_not_zero(self):
        with tempfile.TemporaryDirectory(prefix="jessie-power-test-") as td:
            report = Path(td) / "drc.json"
            report.write_text("{}")
            with self.assertRaisesRegex(ValueError, "open connections are unknown"):
                power.drc_open_counts(report, {"GND"})

    def test_unclassifiable_drc_edge_is_not_reported_as_zero(self):
        with tempfile.TemporaryDirectory(prefix="power-drc-test-") as directory:
            report = Path(directory) / "drc.json"
            report.write_text(
                json.dumps(
                    {"unconnected_items": [{"items": [{"description": "Pad 1"}]}]}
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unclassified"):
                power.drc_open_counts(report, {"GND"})


if __name__ == "__main__":
    unittest.main()
