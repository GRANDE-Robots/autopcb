"""Work-in-progress saves require a real, independently checked improvement."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pcb.freeroute_delta import Trace, Via
from pcb.net_routing.protection import protected_footprint_changes
from pcb.net_routing.working_save import save_working, verify_working_net_copper, working_improvement


class WorkingSaveTests(unittest.TestCase):
    def test_saved_checkpoint_replans_only_current_unfinished_nets(self):
        from pcb.net_routing import working_save as api
        state = {"checkpoint": {}, "best_preview_opens": 1, "single_net_search": True,
                 "queue": [[{"nets": [{"name": "ALREADY_CLOSED"}]}]], "keep_placement": True}
        checked = {"drc": {"open_connections": 1}, "work": {"items": [
            {"name": "OPEN", "priority": 100, "window": [0, 0, 4, 4], "island_count": 2}]},
            "unqualified_nets": [], "reference_copper": {"high_speed_nets": []}}
        policy = {"max_pins": 3, "route": {"layers": ["F.Cu", "B.Cu"]}}
        with patch.object(api, "rebind_exact_saved_checkpoint", return_value={"candidate_sha256": "x"}):
            updated = api._saved_state(state, b"pcb", {}, {}, "policy", checked, policy)
        self.assertEqual([n["name"] for group in updated["queue"] for request in group for n in request["nets"]],
                         ["OPEN"])
        self.assertTrue(updated["keep_placement"])
        self.assertEqual(state["queue"][0][0]["nets"][0]["name"], "ALREADY_CLOSED")

    def setUp(self):
        self.trace = Trace("A", "B.Cu", 200_000, (0, 0), (1_000_000, 0))
        self.other = Trace("B", "F.Cu", 200_000, (0, 0), (0, 1_000_000))
        self.via = Via("B", (0, 1_000_000), 600_000, 300_000)
        self.copper = [self.trace, self.other, self.via]
        self.before = {"drc": {"open_connections": 10, "warnings": 5, "errors": 0},
                       "geometry": {"via_count": 1, "track_length_mm": 2.0, "degree_two_joints": 2}}
        self.cleaned = {"drc": {"open_connections": 10, "warnings": 4, "errors": 0},
                        "geometry": {"via_count": 1, "track_length_mm": 1.0, "degree_two_joints": 1}}

    def test_equal_open_cleanup_requires_fewer_warnings_and_trace_removal_only(self):
        provenance = {"engine": "native-dangling-prune", "objective": "reduce-warnings"}
        self.assertEqual(working_improvement(self.before, self.cleaned, self.copper,
                                             [self.other, self.via], provenance),
                         "fewer dangling warnings")
        with self.assertRaisesRegex(RuntimeError, "verified cleanup"):
            working_improvement(self.before, self.cleaned, self.copper, self.copper, provenance)
        with self.assertRaisesRegex(RuntimeError, "verified cleanup"):
            working_improvement(self.before, self.cleaned, self.copper, [self.other], provenance)
        with self.assertRaisesRegex(RuntimeError, "verified cleanup"):
            working_improvement(self.before, self.cleaned, self.copper,
                                [self.other, self.via], {"engine": "fastroute", "objective": "reduce-warnings"})

    def test_via_cleanup_requires_fewer_vias_and_no_new_copper(self):
        after = {"drc": {"open_connections": 10, "warnings": 5, "errors": 0},
                 "geometry": {"via_count": 0, "track_length_mm": 2.0, "degree_two_joints": 2}}
        provenance = {"engine": "native-via-prune", "objective": "minimize-vias"}
        self.assertEqual(working_improvement(self.before, after, self.copper,
                                             [self.trace, self.other], provenance), "fewer vias")
        added = Trace("C", "B.Cu", 200_000, (0, 0), (0, 1_000_000))
        with self.assertRaisesRegex(RuntimeError, "verified cleanup"):
            working_improvement(self.before, after, self.copper, [self.trace, self.other, added], provenance)

    def test_equal_open_junction_cleanup_requires_recomputed_geometry(self):
        def trace(start, end):
            return Trace("A", "B.Cu", 200_000, start, end)

        trunk = trace((0, 0), (4_000_000, 0))
        branch = trace((2_000_000, 0), (2_000_000, 2_000_000))
        spur = trace((4_000_000, 0), (5_000_000, 0))
        shortened = trace((0, 0), (1_750_000, 0))
        clipped = trace((2_000_000, 250_000), (2_000_000, 2_000_000))
        diagonal = trace((1_750_000, 0), (2_000_000, 250_000))
        before = [trunk, branch, spur, self.via]
        after = [shortened, clipped, diagonal, self.via]
        provenance = {"engine": "native-dangling-chamfer", "objective": "reduce-warnings"}
        self.assertEqual(working_improvement(self.before, self.cleaned, before, after, provenance),
                         "fewer dangling warnings")
        with self.assertRaisesRegex(RuntimeError, "cleanup"):
            working_improvement(self.before, self.cleaned, before, [shortened, clipped, self.via], provenance)

    def test_more_opens_never_qualifies(self):
        after = {**self.cleaned, "drc": {**self.cleaned["drc"], "open_connections": 11}}
        with self.assertRaisesRegex(RuntimeError, "worsens"):
            working_improvement(self.before, after, self.copper, [self.other, self.via], {})

    def test_equal_open_rounding_requires_recomputed_improvement_evidence(self):
        provenance = {"engine": "native-rounding-repair", "objective": "repair-rounding"}
        with self.assertRaisesRegex(RuntimeError, "verified cleanup"):
            working_improvement(self.before, self.before, self.copper, self.copper, provenance)
        with self.assertRaisesRegex(RuntimeError, "verified cleanup"):
            working_improvement(self.before, self.before, self.copper, self.copper, provenance,
                                rounding_proof={"corrected": 0})
        self.assertEqual(
            working_improvement(self.before, self.before, self.copper, self.copper, provenance,
                                rounding_proof={"corrected": 1}),
            "fewer off-angle tracks",
        )

    def test_equal_open_staircase_cleanup_requires_recomputed_evidence(self):
        provenance = {"engine": "native-staircase-repair", "objective": "repair-staircase"}
        for proof in (None, {"corrected": 0}, {"corrected": True}):
            with self.subTest(proof=proof), self.assertRaisesRegex(RuntimeError, "verified cleanup"):
                working_improvement(self.before, self.before, self.copper, self.copper,
                                    provenance, staircase_proof=proof)
        self.assertEqual(
            working_improvement(self.before, self.before, self.copper, self.copper,
                                provenance, staircase_proof={"corrected": 1}),
            "fewer staircases",
        )

    def test_save_blocks_unqualified_power_and_safety_copper_changes(self):
        board = MagicMock()
        board.GetFootprints.return_value = []
        board.GetNetsByName.return_value = {"SIGNAL_A": None, "ENC_A_MCU": None,
                                             "SUPPLY": None, "UNCLASSIFIED": None}
        classes = {"SIGNAL_A": "CORE_SIGNAL", "ENC_A_MCU": "CORE_SIGNAL",
                   "SUPPLY": "POWER", "UNCLASSIFIED": "Default"}
        board.FindNet.side_effect = lambda name: MagicMock(GetNetClassName=lambda: classes[name])
        signal = Trace("SIGNAL_A", "B.Cu", 200_000, (0, 0), (1_000_000, 0))
        verify_working_net_copper(board, [], [signal], ())
        for net in ("ENC_A_MCU", "SUPPLY", "UNCLASSIFIED"):
            with self.subTest(net=net):
                item = Trace(net, "B.Cu", 200_000, (0, 0), (1_000_000, 0))
                with self.assertRaisesRegex(RuntimeError, net):
                    verify_working_net_copper(board, [], [item], ())
                with self.assertRaisesRegex(RuntimeError, net):
                    verify_working_net_copper(board, [item], [], ())
        with self.assertRaisesRegex(RuntimeError, "SIGNAL_A"):
            verify_working_net_copper(board, [], [signal], ("SIGNAL_A",))

    def test_unqualified_footprint_geometry_cannot_change(self):
        board, part, pad = MagicMock(), MagicMock(), MagicMock()
        board.GetNetsByName.return_value = {"SUPPLY": None}
        board.FindNet.return_value.GetNetClassName.return_value = "POWER"
        board.GetFootprints.return_value = [part]
        part.GetReference.return_value = "R5"
        part.GetValue.return_value = "10k"
        part.GetFieldsText.return_value = {}
        part.m_Uuid.AsString.return_value = "part-1"
        part.Pads.return_value = [pad]
        pad.GetNetname.return_value = "SUPPLY"
        before = b'(kicad_pcb (footprint "R" (uuid "part-1") (at 0 0)))'
        moved = b'(kicad_pcb (footprint "R" (uuid "part-1") (at 1 0)))'
        self.assertEqual(protected_footprint_changes(board, board, before, before, ()), ())
        self.assertEqual(protected_footprint_changes(board, board, before, moved, ()), ("R5",))

    def test_editor_lock_stops_before_native_validation_or_save(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            runs = project / "autopcb" / "results" / "autoroute"
            runs.mkdir(parents=True)
            run_id = "a" * 32
            (runs / f"{run_id}.json").write_text(json.dumps({"schema": 1, "run_id": run_id,
                                                            "checkpoint": {}}), encoding="utf-8")
            scratch = project / "scratch"
            scratch.mkdir()
            (scratch / "v4.1.kicad_sch").write_bytes(b"schematic")
            snapshot = MagicMock()
            snapshot.__enter__.return_value = snapshot
            snapshot.directory = scratch
            snapshot.original = b"saved"
            snapshot.fingerprints = {}
            snapshot.tool_hashes = {}
            guard = MagicMock()
            guard.verify_editor_unlocked.side_effect = RuntimeError("editor lock")
            protected = MagicMock()
            protected.differences.return_value = []
            with (patch("pcb.net_routing.working_save.read_config", return_value=({}, "policy")),
                  patch("pcb.net_routing.working_save._exclusive") as exclusive,
                  patch("pcb.net_routing.working_save.Snapshot", return_value=snapshot),
                  patch("pcb.net_routing.working_save._source_hashes", return_value={}),
                  patch("pcb.net_routing.working_save.decode_checkpoint", return_value=b"candidate"),
                  patch("pcb.net_routing.working_save.ProtectedState.capture", return_value=protected),
                  patch("pcb.net_routing.working_save.BoardSaveGuard", return_value=guard),
                  patch("pcb.net_routing.working_save.run_native") as native):
                exclusive.return_value.__enter__.return_value = None
                with self.assertRaisesRegex(RuntimeError, "editor lock"):
                    save_working(project, run_id, apply=True, progress=lambda _message: None)
            native.assert_not_called()
            guard.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
