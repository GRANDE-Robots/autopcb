"""test_via_prune.py: check selection of redundant vias.

Potentially redundant vias are selected safely for private validation.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import tempfile
import unittest
from pathlib import Path

import pcbnew

from pcb.net_routing.via_prune import CleanupCandidates


class ViaPruneTests(unittest.TestCase):
    """Tests for selecting redundant vias and dangling copper for cleanup."""

    def test_cleanup_can_defer_both_identified_parity_nets(self):
        from pcb.net_routing.prepare import parity_blocked_nets, summarize_drc
        from pcb.net_routing.via_prune import CleanupBaseline

        report = {
            "violations": [],
            "unconnected_items": [{}] * 3,
            "schematic_parity": [
                {
                    "type": "net_conflict",
                    "severity": "error",
                    "description": "Pad net (OLD) doesn't match net given by schematic (NEW)",
                    "items": [{"uuid": "pad-1", "description": "Pad 7 [OLD]"}],
                }
            ],
            "included_severities": ["error", "warning", "exclusion"],
            "ignored_checks": [],
        }
        baseline = summarize_drc(report)
        baseline["parity_blocked_nets"] = list(
            parity_blocked_nets(report, {"pad-1": "OLD"})
        )
        self.assertEqual(
            CleanupBaseline.verify_cleanup_baseline(baseline, expected_opens=3),
            ("NEW", "OLD"),
        )
        with self.assertRaises(RuntimeError):
            CleanupBaseline.verify_cleanup_baseline(
                {**baseline, "parity_blocked_nets": []}, expected_opens=3
            )
        with self.assertRaises(ValueError):
            parity_blocked_nets(report, {"pad-1": "UNRELATED"})

    def test_cleanup_baseline_can_attribute_existing_native_errors(self):
        from pcb.net_routing.prepare import summarize_drc
        from pcb.net_routing.via_prune import CleanupBaseline

        finding = {
            "type": "copper_edge_clearance",
            "severity": "error",
            "description": "required 0.5 mm, actual 0.4214 mm",
            "items": [{"uuid": "pad"}, {"uuid": "hole"}],
        }
        report = {
            "violations": [finding],
            "unconnected_items": [{}] * 3,
            "schematic_parity": [],
            "included_severities": ["error", "warning", "exclusion"],
            "ignored_checks": [],
        }
        baseline = summarize_drc(report)
        CleanupBaseline.verify_cleanup_baseline(baseline, expected_opens=3)
        self.assertFalse(baseline["complete"])

    def test_cleanup_baseline_rejects_incomplete_or_excluded_evidence(self):
        from pcb.net_routing.prepare import summarize_drc
        from pcb.net_routing.via_prune import CleanupBaseline

        report = {
            "violations": [],
            "unconnected_items": [{}] * 3,
            "schematic_parity": [],
            "included_severities": ["error", "warning", "exclusion"],
            "ignored_checks": [],
        }
        baseline = summarize_drc(report)
        invalid = (
            ({**baseline, "exclusions": 1}, 3),
            ({**baseline, "parity_findings": 1}, 3),
            ({**baseline, "ignored_checks": ["clearance"]}, 3),
            (baseline, 2),
            (
                {
                    **baseline,
                    "errors": 1,
                    "violation_keys": [["clearance", "error", ["pad"]]],
                    "error_details": [],
                },
                3,
            ),
        )
        for candidate, expected in invalid:
            with self.subTest(candidate=candidate, expected=expected):
                with self.assertRaises(RuntimeError):
                    CleanupBaseline.verify_cleanup_baseline(
                        candidate, expected_opens=expected
                    )

    def setUp(self):
        """Create a four-layer board with a net SIG."""
        self.board = pcbnew.BOARD()
        self.board.SetCopperLayerCount(4)
        self.net = pcbnew.NETINFO_ITEM(self.board, "SIG")
        self.board.Add(self.net)

    def via(self, x):
        """Add a through via on net SIG and return it.

        The via is 0.6 mm wide with a 0.3 mm drill and lies on y = 0.

        Inputs:
            x: x position in nm.
        """
        via = pcbnew.PCB_VIA(self.board)
        via.SetPosition(pcbnew.VECTOR2I(x, 0))
        via.SetWidth(600_000)
        via.SetDrill(300_000)
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetNetCode(self.net.GetNetCode())
        self.board.Add(via)
        return via

    def track(self, x, layer):
        """Add a 1 mm long, 0.2 mm wide track on net SIG and return it.

        Inputs:
            x: start x position in nm; y is 0.
            layer: native layer of the track.
        """
        item = pcbnew.PCB_TRACK(self.board)
        item.SetStart(pcbnew.VECTOR2I(x, 0))
        item.SetEnd(pcbnew.VECTOR2I(x + 1_000_000, 0))
        item.SetWidth(200_000)
        item.SetLayer(layer)
        item.SetNetCode(self.net.GetNetCode())
        self.board.Add(item)
        return item

    def test_only_unlocked_single_layer_vias_are_candidates(self):
        single = self.via(0)
        self.track(0, pcbnew.B_Cu)
        through = self.via(3_000_000)
        self.track(3_000_000, pcbnew.B_Cu)
        self.track(3_000_000, pcbnew.F_Cu)
        locked = self.via(6_000_000)
        locked.SetLocked(True)
        self.track(6_000_000, pcbnew.B_Cu)
        group_via = self.via(9_000_000)
        self.track(9_000_000, pcbnew.B_Cu)
        group = pcbnew.PCB_GROUP(self.board)
        self.board.Add(group)
        group.AddItem(group_via)
        self.assertEqual(
            CleanupCandidates.candidate_vias(self.board, {"SIG"}, set()),
            (single.m_Uuid.AsString(),),
        )
        self.assertEqual(
            CleanupCandidates.candidate_vias(self.board, {"SIG"}, {"SIG"}), ()
        )
        self.assertEqual(CleanupCandidates.candidate_vias(self.board, set(), set()), ())
        self.assertNotIn(
            through.m_Uuid.AsString(),
            CleanupCandidates.candidate_vias(self.board, {"SIG"}, set()),
        )

    def test_zone_or_pad_connections_are_never_assumed_redundant(self):
        via = self.via(0)
        self.track(0, pcbnew.B_Cu)
        footprint = pcbnew.FOOTPRINT(self.board)
        self.board.Add(footprint)
        pad = pcbnew.PAD(footprint)
        pad.SetPosition(pcbnew.VECTOR2I(0, 0))
        pad.SetSize(pcbnew.VECTOR2I(400_000, 400_000))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(pcbnew.LSET.BackMask())
        pad.SetNetCode(self.net.GetNetCode())
        footprint.Add(pad)
        self.assertEqual(
            CleanupCandidates.candidate_vias(self.board, {"SIG"}, set()), ()
        )
        footprint.Remove(pad)
        zone = pcbnew.ZONE(self.board)
        self.board.Add(zone)
        zone.SetNet(self.net)
        self.assertEqual(
            CleanupCandidates.candidate_vias(self.board, {"SIG"}, set()), ()
        )
        self.assertTrue(via.GetNetname() == "SIG")

    def test_dead_end_track_is_removed_with_its_via(self):
        from pcb.net_routing.via_prune import copper_removal_changes, CleanupCandidates

        via = self.via(0)
        self.track(0, pcbnew.B_Cu)
        track = next(
            item
            for item in self.board.GetTracks()
            if not isinstance(item, pcbnew.PCB_VIA)
        )
        self.assertEqual(
            CleanupCandidates.dangling_spur_items(self.board, {via.m_Uuid.AsString()}),
            {via.m_Uuid.AsString(), track.m_Uuid.AsString()},
        )
        changes = copper_removal_changes(
            self.board, {via.m_Uuid.AsString(), track.m_Uuid.AsString()}
        )
        self.assertEqual(
            {entry["target"]["kind"] for entry in changes}, {"via", "segment"}
        )
        self.assertTrue(all(entry["op"] == "remove" for entry in changes))
        with self.assertRaisesRegex(ValueError, "not found"):
            copper_removal_changes(self.board, {"missing"})

    def test_text_candidate_removes_only_selected_copper(self):
        from core.kicad_campaign import CampaignPlanner
        from pcb.net_routing.via_prune import copper_removal_changes, CleanupCandidates

        via = self.via(0)
        self.track(0, pcbnew.B_Cu)
        self.track(4_000_000, pcbnew.F_Cu)
        selected = CleanupCandidates.dangling_spur_items(
            self.board, {via.m_Uuid.AsString()}
        )
        survivor = next(
            item.m_Uuid.AsString()
            for item in self.board.GetTracks()
            if item.m_Uuid.AsString() not in selected
        )
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "v4.1.kicad_pcb"
            self.assertTrue(pcbnew.SaveBoard(str(source), self.board))
            original = source.read_bytes()
            candidate = (
                CampaignPlanner(Path(folder))
                .plan("pcb", copper_removal_changes(self.board, selected))
                .after
            )
            self.assertEqual(source.read_bytes(), original)
            for identifier in selected:
                self.assertNotIn(identifier.encode(), candidate)
            self.assertIn(survivor.encode(), candidate)

    def test_spur_does_not_remove_tracks_connecting_two_pads(self):
        from pcb.net_routing.via_prune import CleanupCandidates

        via = self.via(0)
        self.track(0, pcbnew.B_Cu)
        spur = next(
            item
            for item in self.board.GetTracks()
            if not isinstance(item, pcbnew.PCB_VIA)
        )
        self.track(1_000_000, pcbnew.B_Cu)
        third = pcbnew.PCB_TRACK(self.board)
        third.SetStart(pcbnew.VECTOR2I(1_000_000, 0))
        third.SetEnd(pcbnew.VECTOR2I(1_000_000, 1_000_000))
        third.SetWidth(200_000)
        third.SetLayer(pcbnew.B_Cu)
        third.SetNetCode(self.net.GetNetCode())
        self.board.Add(third)
        footprint = pcbnew.FOOTPRINT(self.board)
        self.board.Add(footprint)
        for x, y in ((2_000_000, 0), (1_000_000, 1_000_000)):
            pad = pcbnew.PAD(footprint)
            pad.SetPosition(pcbnew.VECTOR2I(x, y))
            pad.SetSize(pcbnew.VECTOR2I(400_000, 400_000))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pcbnew.LSET.BackMask())
            pad.SetNetCode(self.net.GetNetCode())
            footprint.Add(pad)
        self.assertEqual(
            CleanupCandidates.dangling_spur_items(self.board, {via.m_Uuid.AsString()}),
            {via.m_Uuid.AsString(), spur.m_Uuid.AsString()},
        )

    def test_spur_stops_at_an_interior_copper_contact(self):
        from pcb.net_routing.via_prune import CleanupCandidates

        via = self.via(0)
        self.track(0, pcbnew.B_Cu)
        branch = pcbnew.PCB_TRACK(self.board)
        branch.SetStart(pcbnew.VECTOR2I(500_000, 0))
        branch.SetEnd(pcbnew.VECTOR2I(500_000, 1_000_000))
        branch.SetWidth(200_000)
        branch.SetLayer(pcbnew.B_Cu)
        branch.SetNetCode(self.net.GetNetCode())
        self.board.Add(branch)
        self.assertEqual(
            CleanupCandidates.dangling_spur_items(self.board, {via.m_Uuid.AsString()}),
            {via.m_Uuid.AsString()},
        )

    def test_dangling_track_seeds_require_warning_and_eligible_unlocked_net(self):
        from pcb.net_routing.via_prune import CleanupCandidates

        eligible = self.track(0, pcbnew.B_Cu)
        locked = self.track(3_000_000, pcbnew.B_Cu)
        locked.SetLocked(True)
        unwarned = self.track(6_000_000, pcbnew.B_Cu)
        report = {
            "violations": [
                {
                    "type": "track_dangling",
                    "items": [
                        {"uuid": eligible.m_Uuid.AsString()},
                        {"uuid": locked.m_Uuid.AsString()},
                    ],
                }
            ]
        }
        self.assertEqual(
            CleanupCandidates.candidate_dangling_tracks(
                self.board, report, {"SIG"}, set()
            ),
            (eligible.m_Uuid.AsString(),),
        )
        self.assertEqual(
            CleanupCandidates.candidate_dangling_tracks(
                self.board, report, {"SIG"}, {"SIG"}
            ),
            (),
        )
        self.assertEqual(
            CleanupCandidates.candidate_dangling_tracks(
                self.board, report, set(), set()
            ),
            (),
        )
        self.assertNotIn(
            unwarned.m_Uuid.AsString(),
            CleanupCandidates.candidate_dangling_tracks(
                self.board, report, {"SIG"}, set()
            ),
        )

    def test_dangling_track_walk_stops_at_a_branch_with_two_pad_connections(self):
        from pcb.net_routing.via_prune import CleanupCandidates

        trunk = self.track(0, pcbnew.B_Cu)
        middle = self.track(1_000_000, pcbnew.B_Cu)
        seed = self.track(2_000_000, pcbnew.B_Cu)
        branch = pcbnew.PCB_TRACK(self.board)
        branch.SetStart(pcbnew.VECTOR2I(1_000_000, 0))
        branch.SetEnd(pcbnew.VECTOR2I(1_000_000, 1_000_000))
        branch.SetWidth(200_000)
        branch.SetLayer(pcbnew.B_Cu)
        branch.SetNetCode(self.net.GetNetCode())
        self.board.Add(branch)
        footprint = pcbnew.FOOTPRINT(self.board)
        self.board.Add(footprint)
        for x, y in ((0, 0), (1_000_000, 1_000_000)):
            pad = pcbnew.PAD(footprint)
            pad.SetPosition(pcbnew.VECTOR2I(x, y))
            pad.SetSize(pcbnew.VECTOR2I(400_000, 400_000))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pcbnew.LSET.BackMask())
            pad.SetNetCode(self.net.GetNetCode())
            footprint.Add(pad)
        selected = CleanupCandidates.extend_dangling_tracks(
            self.board, {seed.m_Uuid.AsString()}, {"SIG"}, set()
        )
        self.assertEqual(selected, {seed.m_Uuid.AsString(), middle.m_Uuid.AsString()})
        self.assertNotIn(trunk.m_Uuid.AsString(), selected)
        self.assertNotIn(branch.m_Uuid.AsString(), selected)

    def test_dangling_cleanup_chamfers_a_live_interior_branch(self):
        from core.kicad_campaign import CampaignPlanner
        from pcb.net_routing.via_prune import TailChamfer, copper_removal_changes

        trunk = self.track(0, pcbnew.B_Cu)
        trunk.SetEnd(pcbnew.VECTOR2I(4_000_000, 0))
        seed = self.track(4_000_000, pcbnew.B_Cu)
        branch = pcbnew.PCB_TRACK(self.board)
        branch.SetStart(pcbnew.VECTOR2I(2_000_000, 0))
        branch.SetEnd(pcbnew.VECTOR2I(2_000_000, 2_000_000))
        branch.SetWidth(200_000)
        branch.SetLayer(pcbnew.B_Cu)
        branch.SetNetCode(self.net.GetNetCode())
        self.board.Add(branch)
        self.via(0)
        selected = {seed.m_Uuid.AsString()}
        changes = TailChamfer.coordinated_tail_changes(
            self.board, selected, {"SIG"}, set()
        )
        self.assertEqual(len(changes), 3)
        self.assertEqual(sum(entry["op"] == "insert" for entry in changes), 1)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "v4.1.kicad_pcb"
            self.assertTrue(pcbnew.SaveBoard(str(source), self.board))
            original = source.read_bytes()
            candidate = (
                CampaignPlanner(Path(folder))
                .plan("pcb", copper_removal_changes(self.board, selected) + changes)
                .after
            )
            self.assertEqual(source.read_bytes(), original)
            source.write_bytes(candidate)
            after = pcbnew.LoadBoard(str(source))
            self.assertIsNotNone(after)
            self.assertNotIn(
                seed.m_Uuid.AsString(),
                {item.m_Uuid.AsString() for item in after.GetTracks()},
            )
            from pcb.net_routing.style import audit_style

            self.assertFalse(audit_style(after).invalid_bends)

    def test_dangling_cleanup_never_edits_a_protected_or_locked_branch(self):
        from pcb.net_routing.via_prune import TailChamfer

        trunk = self.track(0, pcbnew.B_Cu)
        trunk.SetEnd(pcbnew.VECTOR2I(4_000_000, 0))
        seed = self.track(4_000_000, pcbnew.B_Cu)
        branch = pcbnew.PCB_TRACK(self.board)
        branch.SetStart(pcbnew.VECTOR2I(2_000_000, 0))
        branch.SetEnd(pcbnew.VECTOR2I(2_000_000, 2_000_000))
        branch.SetWidth(200_000)
        branch.SetLayer(pcbnew.B_Cu)
        branch.SetNetCode(self.net.GetNetCode())
        self.board.Add(branch)
        self.via(0)
        selected = {seed.m_Uuid.AsString()}
        self.assertEqual(
            TailChamfer.coordinated_tail_changes(
                self.board, selected, {"SIG"}, {"SIG"}
            ),
            [],
        )
        branch.SetLocked(True)
        self.assertEqual(
            TailChamfer.coordinated_tail_changes(self.board, selected, {"SIG"}, set()),
            [],
        )

    def test_dangling_cleanup_does_not_move_an_anchored_junction(self):
        from pcb.net_routing.via_prune import TailChamfer

        trunk = self.track(0, pcbnew.B_Cu)
        trunk.SetEnd(pcbnew.VECTOR2I(4_000_000, 0))
        seed = self.track(4_000_000, pcbnew.B_Cu)
        branch = pcbnew.PCB_TRACK(self.board)
        branch.SetStart(pcbnew.VECTOR2I(2_000_000, 0))
        branch.SetEnd(pcbnew.VECTOR2I(2_000_000, 2_000_000))
        branch.SetWidth(200_000)
        branch.SetLayer(pcbnew.B_Cu)
        branch.SetNetCode(self.net.GetNetCode())
        self.board.Add(branch)
        self.via(0)
        footprint = pcbnew.FOOTPRINT(self.board)
        self.board.Add(footprint)
        pad = pcbnew.PAD(footprint)
        pad.SetPosition(pcbnew.VECTOR2I(2_000_000, 0))
        pad.SetSize(pcbnew.VECTOR2I(400_000, 400_000))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(pcbnew.LSET.BackMask())
        pad.SetNetCode(self.net.GetNetCode())
        footprint.Add(pad)
        self.assertEqual(
            TailChamfer.coordinated_tail_changes(
                self.board, {seed.m_Uuid.AsString()}, {"SIG"}, set()
            ),
            [],
        )

    def test_cleanup_defers_nets_with_new_dangling_or_geometry_findings(self):
        from pcb.net_routing.style import Joint, Staircase, StyleReport
        from pcb.net_routing.via_prune import CleanupRegressions

        before = {
            "violations": [{"type": "track_dangling", "items": [{"uuid": "old"}]}]
        }
        after = {
            "violations": [
                *before["violations"],
                {"type": "via_dangling", "items": [{"uuid": "new-via"}]},
                {"type": "track_dangling", "items": [{"uuid": "new-track"}]},
            ]
        }
        self.assertEqual(
            CleanupRegressions.newly_dangling_nets(
                before, after, {"old": "A", "new-via": "B", "new-track": "C"}
            ),
            {"B", "C"},
        )
        empty = StyleReport((), (), (), (), (), (), (), ())
        changed = StyleReport(
            (),
            (),
            (Joint("D", "F.Cu", (0, 0), ("one", "two")),),
            (),
            (Staircase("E", "B.Cu", ((0, 0), (1, 0)), (1, 0, 1), ("three",)),),
            (),
            (),
            (),
        )
        self.assertEqual(
            CleanupRegressions.newly_affected_style_nets(empty, changed), {"D", "E"}
        )

    def test_cleanup_finds_a_new_dangling_endpoint_on_the_same_track(self):
        from pcb.net_routing.via_prune import CleanupRegressions

        before = {
            "violations": [{"type": "track_dangling", "items": [{"uuid": "same"}]}]
        }
        after = {
            "violations": [{"type": "track_dangling", "items": [{"uuid": "same"}]}]
        }
        old = {
            "dangling_warnings": [
                ["warning", ['dangling:["NMEA","F.Cu",1,2,250000,1,0]']]
            ]
        }
        new = {
            "dangling_warnings": [
                ["warning", ['dangling:["NMEA","F.Cu",3,4,250000,1,0]']]
            ]
        }
        self.assertEqual(
            CleanupRegressions.newly_dangling_nets(before, after, {"same": "NMEA"}),
            set(),
        )
        self.assertEqual(
            CleanupRegressions.newly_dangling_endpoint_nets(old, new), {"NMEA"}
        )

    def test_cleanup_splits_independent_branches_but_keeps_overlapping_tails_together(
        self,
    ):
        from pcb.net_routing.via_prune import CleanupGroups

        groups = CleanupGroups.merge_cleanup_groups(
            ({"a", "b"}, {"b", "c"}, {"d"}, {"e", "f"})
        )
        self.assertEqual(
            groups,
            (frozenset({"a", "b", "c"}), frozenset({"d"}), frozenset({"e", "f"})),
        )
        pieces = CleanupGroups.split_cleanup_groups(groups, frozenset("abcdef"))
        self.assertEqual(len(pieces), 2)
        self.assertFalse(pieces[0] & pieces[1])
        self.assertEqual(pieces[0] | pieces[1], frozenset("abcdef"))
        self.assertTrue(any({"a", "b", "c"} <= piece for piece in pieces))
        self.assertEqual(
            CleanupGroups.split_cleanup_groups(
                (frozenset({"a", "b"}),), frozenset({"a", "b"})
            ),
            (),
        )


if __name__ == "__main__":
    unittest.main()
