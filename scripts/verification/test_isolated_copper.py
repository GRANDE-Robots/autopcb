"""Only detached, tiny native copper may bypass protected-net cleanup gates."""

import unittest
from collections import Counter
from unittest.mock import patch

import pcbnew

from pcb.net_routing.isolated_copper import (
    IsolatedFragments,
    isolated_dangling_fragments,
    isolated_fragments,
    verify_isolated_protected_removals,
)
from pcb.net_routing.working_save import WorkingQualification


class IsolatedCopperTests(unittest.TestCase):
    def setUp(self):
        self.board = pcbnew.BOARD()
        self.board.SetCopperLayerCount(4)
        self.net = pcbnew.NETINFO_ITEM(self.board, "SIG")
        self.board.Add(self.net)

    def track(self, x=0, length=5_000):
        track = pcbnew.PCB_TRACK(self.board)
        track.SetStart(pcbnew.VECTOR2I(x, 0))
        track.SetEnd(pcbnew.VECTOR2I(x + length, 0))
        track.SetWidth(200_000)
        track.SetLayer(pcbnew.B_Cu)
        track.SetNetCode(self.net.GetNetCode())
        self.board.Add(track)
        return track

    @staticmethod
    def report(track):
        uid = track.m_Uuid.AsString()
        return {
            "violations": [
                {"severity": "error", "type": "clearance", "items": [{"uuid": uid}]},
                {
                    "severity": "warning",
                    "type": "track_dangling",
                    "items": [{"uuid": uid}],
                },
            ]
        }

    def test_requires_tiny_unlocked_ungrouped_and_native_error_evidence(self):
        tiny = self.track()
        long_track = self.track(x=2_000_000, length=20_001)
        locked = self.track(x=4_000_000)
        locked.SetLocked(True)
        grouped = self.track(x=6_000_000)
        group = pcbnew.PCB_GROUP(self.board)
        self.board.Add(group)
        group.AddItem(grouped)
        self.assertEqual(
            set(isolated_fragments(self.board, set())), {tiny.m_Uuid.AsString()}
        )
        self.assertEqual(
            isolated_dangling_fragments(
                self.board, self.report(tiny), set(), require_error=True
            ),
            (tiny.m_Uuid.AsString(),),
        )
        self.assertEqual(
            isolated_dangling_fragments(self.board, {"violations": []}, set()), ()
        )
        warning_only = {"violations": self.report(tiny)["violations"][1:]}
        self.assertEqual(
            isolated_dangling_fragments(self.board, warning_only, set()),
            (tiny.m_Uuid.AsString(),),
        )
        self.assertEqual(
            isolated_dangling_fragments(
                self.board, warning_only, set(), require_error=True
            ),
            (),
        )
        self.assertEqual(isolated_fragments(self.board, {"SIG"}), {})
        self.assertNotIn(
            long_track.m_Uuid.AsString(), isolated_fragments(self.board, set())
        )

    def test_touching_a_track_pad_via_or_same_net_zone_is_not_isolated(self):
        tiny = self.track()
        touching = self.track(x=5_000, length=1_000_000)
        self.assertNotIn(tiny.m_Uuid.AsString(), isolated_fragments(self.board, set()))
        self.board.Remove(touching)
        via = pcbnew.PCB_VIA(self.board)
        via.SetPosition(pcbnew.VECTOR2I(0, 0))
        via.SetWidth(600_000)
        via.SetDrill(300_000)
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetNetCode(self.net.GetNetCode())
        self.board.Add(via)
        self.assertNotIn(tiny.m_Uuid.AsString(), isolated_fragments(self.board, set()))
        self.board.Remove(via)
        footprint = pcbnew.FOOTPRINT(self.board)
        self.board.Add(footprint)
        pad = pcbnew.PAD(footprint)
        pad.SetPosition(pcbnew.VECTOR2I(0, 0))
        pad.SetSize(pcbnew.VECTOR2I(400_000, 400_000))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(pcbnew.LSET.BackMask())
        pad.SetNetCode(self.net.GetNetCode())
        footprint.Add(pad)
        self.assertNotIn(tiny.m_Uuid.AsString(), isolated_fragments(self.board, set()))
        footprint.Remove(pad)
        zone = pcbnew.ZONE(self.board)
        self.board.Add(zone)
        zone.SetNet(self.net)
        self.assertNotIn(tiny.m_Uuid.AsString(), isolated_fragments(self.board, set()))

    def test_length_bound_scales_with_width_and_has_an_absolute_limit(self):
        track = self.track(length=20_000)
        uid = track.m_Uuid.AsString()
        self.assertIn(uid, isolated_fragments(self.board, set()))
        track.SetEnd(pcbnew.VECTOR2I(20_001, 0))
        self.assertNotIn(uid, isolated_fragments(self.board, set()))
        track.SetWidth(1_000_000)
        track.SetEnd(pcbnew.VECTOR2I(50_000, 0))
        self.assertIn(uid, isolated_fragments(self.board, set()))
        track.SetEnd(pcbnew.VECTOR2I(50_001, 0))
        self.assertNotIn(uid, isolated_fragments(self.board, set()))

    def test_same_net_copper_overlap_is_conservatively_detected(self):
        tiny = self.track()
        nearby = self.track(x=190_000, length=1_000_000)
        self.assertTrue(IsolatedFragments.same_net_bounding_overlap(tiny, [nearby]))
        self.assertNotIn(tiny.m_Uuid.AsString(), isolated_fragments(self.board, set()))
        nearby.SetLayer(pcbnew.F_Cu)
        self.assertFalse(IsolatedFragments.same_net_bounding_overlap(tiny, [nearby]))

    def test_protected_gate_allows_only_the_proven_removal(self):
        tiny = self.track()
        record = isolated_fragments(self.board, set())[tiny.m_Uuid.AsString()]
        verify_isolated_protected_removals(
            self.board, Counter([record]), Counter(), {"SIG"}, set()
        )
        with self.assertRaisesRegex(RuntimeError, "cannot add"):
            verify_isolated_protected_removals(
                self.board, Counter(), Counter([record]), {"SIG"}, set()
            )
        with self.assertRaisesRegex(RuntimeError, "not limited"):
            verify_isolated_protected_removals(
                self.board, Counter([record]), Counter(), {"SIG"}, {"SIG"}
            )
        with patch(
            "pcb.net_routing.working_save.unqualified_working_nets",
            return_value={"SIG"},
        ):
            with self.assertRaisesRegex(RuntimeError, "electrical qualification"):
                WorkingQualification.verify_working_net_copper(
                    self.board, [record], [], ()
                )
            WorkingQualification.verify_working_net_copper(
                self.board,
                [record],
                [],
                (),
                allow_isolated_removals=True,
                critical_nets=frozenset({"USB"}),
            )
            with self.assertRaisesRegex(RuntimeError, "not limited"):
                WorkingQualification.verify_working_net_copper(
                    self.board,
                    [record],
                    [],
                    (),
                    allow_isolated_removals=True,
                    critical_nets=frozenset({"SIG"}),
                )


if __name__ == "__main__":
    unittest.main()
