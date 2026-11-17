"""Check conservative discovery for nonpublishing differential-pair trials."""

import contextlib
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from pcb.net_routing.pair_probe import _pair_only_classes, discover_pairs, summarize_pairs


class PairProbeTests(unittest.TestCase):
    def setUp(self):
        self.critical = {"USB_DP", "USB_DM"}
        self.endpoints = {
            "USB_DP": {"J1": {"D+"}, "U1": {"1D+"}},
            "USB_DM": {"J1": {"D-"}, "U1": {"1D-"}},
        }
        self.classes = {"USB_DP": "USB_DATA", "USB_DM": "USB_DATA"}
        self.gaps = {"USB_DATA": 0.24}

    def test_diagnostic_cli_does_not_report_pair_trials_as_verified(self):
        from pcb.net_routing import autoroute_cli

        outcome = {"status": "diagnostic_only", "pairs": [{}, {}],
                   "saved_opens": 191, "candidate_opens": 189,
                   "saved_critical_opens": 10, "candidate_critical_opens": 8,
                   "saved_other_errors": 0, "other_errors": 2,
                   "saved_warnings": 29, "warnings": 33,
                   "added_traces": 6, "added_vias": 1,
                   "new_style_issues": {"staircases": 2},
                   "reference_before_missing": 1, "reference_after_missing": 2,
                   "reference_unsupported": 0, "skipped_non_octilinear": 0,
                   "protected_changes": [], "limitations": ["Candidate not saved; pair geometry not verified."]}
        output = io.StringIO()
        with (patch("pcb.net_routing.isolation.run_native", return_value=outcome) as probe,
              patch("pcb.net_routing.fast.record_unimproved_batch") as record,
              patch.object(autoroute_cli, "limit_resources", return_value=contextlib.nullcontext()),
              patch.object(autoroute_cli, "progress_heartbeat", return_value=contextlib.nullcontext()),
              contextlib.redirect_stdout(output)):
            status = autoroute_cli.main(["--project", ".", "pair-probe", "--seconds", "30", "--run", "a" * 32])
        self.assertEqual(status, 0)
        self.assertEqual(probe.call_args.kwargs["seconds"], 30)
        record.assert_called_once()
        self.assertIn("not saved", output.getvalue().lower())
        self.assertIn("not verified", output.getvalue().lower())
        self.assertIn("staircases", output.getvalue().lower())
        self.assertIn("29 / 33", output.getvalue())

    def test_pair_cli_forwards_the_alternative_engine(self):
        from pcb.net_routing import autoroute_cli

        with (patch("pcb.net_routing.isolation.run_native", return_value={}) as run,
              patch.object(autoroute_cli, "limit_resources", return_value=contextlib.nullcontext()),
              patch.object(autoroute_cli, "progress_heartbeat", return_value=contextlib.nullcontext()),
              patch.object(autoroute_cli.Console, "result")):
            status = autoroute_cli.main(["--project", ".", "pair-probe", "--engine", "krt",
                                         "--router-grid-mm", "0.2", "--pair-index", "1"])
        self.assertEqual(status, 0)
        self.assertEqual(run.call_args.kwargs["engine"], "krt")
        self.assertEqual(run.call_args.kwargs["router_grid_mm"], .2)
        self.assertEqual(run.call_args.kwargs["pair_index"], 1)

    def test_pair_has_matching_class_endpoints_polarity_and_gap(self):
        result = discover_pairs(self.critical, self.endpoints, self.classes, self.gaps)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0].positive, result[0].negative, result[0].gap_mm),
                         ("USB_DP", "USB_DM", 0.24))

    def test_pair_selection_is_whole_pair_and_rejects_out_of_range(self):
        from pcb.net_routing.pair_probe import PairSpec, select_pair_scope

        pairs = (PairSpec("A_P", "A_N", "ETH", 0.2), PairSpec("B_DP", "B_DM", "USB", 0.24))
        self.assertEqual(select_pair_scope(pairs, None), (pairs, {"A_P", "A_N", "B_DP", "B_DM"}))
        self.assertEqual(select_pair_scope(pairs, 2), ((pairs[1],), {"B_DP", "B_DM"}))
        for invalid in (0, 3, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                select_pair_scope(pairs, invalid)

    def test_pair_summary_keeps_trial_opens_and_reference_findings_distinct(self):
        pairs = discover_pairs(self.critical, self.endpoints, self.classes, self.gaps)
        tracks = [SimpleNamespace(net="USB_DP", missing_samples=1, continuous_polygon=False),
                  SimpleNamespace(net="USB_DM", missing_samples=0, continuous_polygon=True),
                  SimpleNamespace(net="OTHER", missing_samples=1, continuous_polygon=False)]
        result = summarize_pairs(pairs, {"USB_DP": 2, "USB_DM": 1},
                                 {"USB_DP": 1, "USB_DM": 0}, tracks)
        self.assertEqual(result[0]["saved_opens"], 3)
        self.assertEqual(result[0]["trial_opens"], 1)
        self.assertEqual(result[0]["trial_missing_reference_segments"], 1)

    def test_reference_blocker_summary_counts_unique_nearby_items_on_the_selected_layer(self):
        from pcb.net_routing.pair_probe import reference_blocker_summary

        tracks = [SimpleNamespace(item_id="a", reference_layer="In1.Cu", missing_samples=1, continuous_polygon=False),
                  SimpleNamespace(item_id="b", reference_layer="In2.Cu", missing_samples=1, continuous_polygon=False),
                  SimpleNamespace(item_id="c", reference_layer="In1.Cu", missing_samples=0, continuous_polygon=True)]
        def conflict(signal, blocker, net, layer):
            return SimpleNamespace(high_speed_item_id=signal, blocking_item_id=blocker,
                                   blocking_net=net, reference_layer=layer)
        conflicts = (conflict("a", "x", "POWER", "In1.Cu"),
                     conflict("a", "x", "POWER", "In1.Cu"),
                     conflict("b", "x", "POWER", "In2.Cu"),
                     conflict("a", "y", "OTHER", "In2.Cu"),
                     conflict("c", "z", "OTHER", "In1.Cu"))
        self.assertEqual(reference_blocker_summary(tracks, conflicts), {"POWER": 1})

    def test_pair_trial_reports_a_new_via_drilling_a_pad(self):
        import pcbnew
        from pcb.net_routing.pair_probe import introduced_via_pad_overlaps
        from verification.test_via_guard import ViaPadGuardTests

        with TemporaryDirectory() as directory:
            source = Path(directory) / "fixture.kicad_pcb"
            pcbnew.SaveBoard(str(source), ViaPadGuardTests.board(2_000_000))
            before, after = pcbnew.LoadBoard(str(source)), pcbnew.LoadBoard(str(source))
            via = next(item for item in after.GetTracks() if isinstance(item, pcbnew.PCB_VIA))
            via.SetPosition(pcbnew.VECTOR2I(600_000, 0))
            overlaps = introduced_via_pad_overlaps(before, after)
            self.assertEqual([(item.reference, item.pad_number) for item in overlaps], [("U1", "1")])
            self.assertEqual(introduced_via_pad_overlaps(after, after), ())

    def test_similar_names_without_endpoint_or_polarity_evidence_are_rejected(self):
        self.endpoints["USB_DM"] = {"OTHER": {"D-"}, "U1": {"1D-"}}
        with self.assertRaisesRegex(ValueError, "endpoint"):
            discover_pairs(self.critical, self.endpoints, self.classes, self.gaps)
        self.endpoints["USB_DM"] = {"J1": {"IO1"}, "U1": {"IO2"}}
        with self.assertRaisesRegex(ValueError, "polarity"):
            discover_pairs(self.critical, self.endpoints, self.classes, self.gaps)

    def test_missing_mate_class_or_gap_cannot_be_silently_ignored(self):
        with self.assertRaisesRegex(ValueError, "mate"):
            discover_pairs({"USB_DP"}, self.endpoints, self.classes, self.gaps)
        self.classes["USB_DM"] = "OTHER"
        with self.assertRaisesRegex(ValueError, "class"):
            discover_pairs(self.critical, self.endpoints, self.classes, self.gaps)
        self.classes["USB_DM"] = "USB_DATA"
        with self.assertRaisesRegex(ValueError, "gap"):
            discover_pairs(self.critical, self.endpoints, self.classes, {"USB_DATA": float("nan")})

    def test_only_complete_pair_classes_are_eligible_in_temporary_dsn(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.dsn"
            path.write_text('(pcb example (parser (string_quote ")) (network '
                            '(class kicad_default GND (rule (width 200))) '
                            '(class USB_DATA USB_DP USB_DM (rule (width 220)))))', encoding="utf-8")
            self.assertEqual(_pair_only_classes(path, self.critical), ("AUTOPCB_DEFER_DEFAULT",))
            self.assertIn("(class AUTOPCB_DEFER_DEFAULT GND", path.read_text(encoding="utf-8"))
            path.write_text('(pcb example (network (class USB_DATA USB_DP USB_DM OTHER)))', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unrelated"):
                _pair_only_classes(path, self.critical)

    def test_single_pair_scope_defers_other_pairs_without_changing_class_rules(self):
        from pcb.net_routing.pair_probe import _pair_scope_classes

        with TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.dsn"
            path.write_text('(pcb example (parser (string_quote ")) (network '
                            '(class kicad_default GND (rule (width 200))) '
                            '(class USB_DATA USB_DP USB_DM USB2_DP USB2_DM (rule (width 220)))))',
                            encoding="utf-8")
            all_nets = {"USB_DP", "USB_DM", "USB2_DP", "USB2_DM"}
            ignored = _pair_scope_classes(path, all_nets, {"USB_DP", "USB_DM"})
            self.assertEqual(set(ignored), {"AUTOPCB_DEFER_DEFAULT", "AUTOPCB_DEFER_NET_1"})
            text = path.read_text(encoding="utf-8")
            self.assertIn("(class USB_DATA USB_DP USB_DM", text)
            self.assertIn("(class AUTOPCB_DEFER_NET_1 USB2_DP USB2_DM", text)
            self.assertEqual(text.count("(rule (width 220))"), 2)


if __name__ == "__main__":
    unittest.main()
