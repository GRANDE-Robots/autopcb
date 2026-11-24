"""test_rounding.py: tests of bounded joint rounding and its proof.

Native regressions for bounded joint rounding and its independent proof.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from pathlib import Path
import tempfile
import unittest

import pcbnew

from core.kicad_campaign import CampaignPlanner
from pcb.net_routing.rounding import find_candidate, verify_candidate
from pcb.net_routing.style import audit_style
from pcb.route_optimizer import mm


class JointRoundingTests(unittest.TestCase):
    """Tests that joint rounding repairs only eligible joints and proves the result
    independently.
    """

    def setUp(self):
        """Create a temporary project path and a board that has the net SIGNAL."""
        self.tmp = tempfile.TemporaryDirectory(prefix="autopcb-rounding-test-")
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.path = self.project / "v4.1.kicad_pcb"
        self.board = pcbnew.BOARD()
        self.board.Add(pcbnew.NETINFO_ITEM(self.board, "SIGNAL"))

    def trace(self, start, end):
        """Add a 0.2 mm B.Cu track on net SIGNAL.

        Inputs:
            start: (x, y) of the start, in nm.
            end: (x, y) of the end, in nm.
        Outputs:
            The added track.
        """
        item = pcbnew.PCB_TRACK(self.board)
        item.SetStart(pcbnew.VECTOR2I(*start))
        item.SetEnd(pcbnew.VECTOR2I(*end))
        item.SetWidth(mm(0.2))
        item.SetLayer(pcbnew.B_Cu)
        item.SetNetCode(self.board.FindNet("SIGNAL").GetNetCode())
        self.board.Add(item)
        return item

    def pair(self):
        """Add two tracks that meet at (3, 2) mm; the second ends 1 nm beyond (4, 3) mm.

        Outputs:
            The second track.
        """
        self.trace((mm(2), mm(2)), (mm(3), mm(2)))
        return self.trace((mm(3), mm(2)), (mm(4) + 1, mm(3)))

    def snapshots(self, changes):
        """Save the board, plan the campaign changes and load both results.

        Inputs:
            changes: Campaign changes for the board.
        Outputs:
            The saved bytes, the planned bytes, the saved board and the board loaded
            from the planned bytes.
        """
        pcbnew.SaveBoard(str(self.path), self.board)
        before = self.path.read_bytes()
        after = CampaignPlanner(self.project).plan("pcb", changes).after
        other = self.project / "candidate.kicad_pcb"
        other.write_bytes(after)
        return (
            before,
            after,
            pcbnew.LoadBoard(str(self.path)),
            pcbnew.LoadBoard(str(other)),
        )

    def test_eligible_joint_is_repaired_without_mutating_the_input(self):
        bad = self.pair()
        original = (bad.GetStart(), bad.GetEnd())
        proposal = find_candidate(self.board, {"SIGNAL"})
        self.assertIsNotNone(proposal)
        self.assertEqual((bad.GetStart(), bad.GetEnd()), original)
        before, after, old, new = self.snapshots(proposal["changes"])
        proof = verify_candidate(before, after, old, new, {"SIGNAL"})
        self.assertEqual(proof["corrected"], 1)
        self.assertEqual(proof["moved_joints"], 1)
        self.assertEqual(len(audit_style(new).non_octilinear), 0)

    def test_nearby_joint_is_exactly_aligned_within_the_bounded_search(self):
        first = self.trace((mm(2), mm(2)), (mm(3), mm(2)))
        second = self.trace((mm(3), mm(2)), (mm(4.025), mm(3)))
        original = [(track.GetStart(), track.GetEnd()) for track in (first, second)]
        proposal = find_candidate(self.board, {"SIGNAL"})
        self.assertIsNotNone(proposal)
        self.assertEqual(
            [(track.GetStart(), track.GetEnd()) for track in (first, second)], original
        )
        before, after, old, new = self.snapshots(proposal["changes"])
        self.assertEqual(
            verify_candidate(before, after, old, new, {"SIGNAL"})["corrected"], 1
        )
        self.assertEqual(len(audit_style(new).non_octilinear), 0)

    def test_joint_beyond_the_distance_limit_is_not_moved(self):
        self.trace((mm(2), mm(2)), (mm(3), mm(2)))
        self.trace((mm(3), mm(2)), (mm(4.050001), mm(3)))
        self.assertIsNone(find_candidate(self.board, {"SIGNAL"}))

    def test_diagonal_shift_uses_true_distance_limit(self):
        first = self.trace((mm(2.04), mm(2.04)), (mm(3), mm(2)))
        second = self.trace((mm(3), mm(2)), (mm(4.04), mm(3.04)))
        self.assertIsNone(find_candidate(self.board, {"SIGNAL"}))
        pcbnew.SaveBoard(str(self.path), self.board)
        before = self.path.read_bytes()
        first.SetEnd(pcbnew.VECTOR2I(mm(3.04), mm(2.04)))
        second.SetStart(pcbnew.VECTOR2I(mm(3.04), mm(2.04)))
        changed = self.project / "over-distance.kicad_pcb"
        pcbnew.SaveBoard(str(changed), self.board)
        with self.assertRaisesRegex(ValueError, "exceeded"):
            verify_candidate(
                before,
                changed.read_bytes(),
                pcbnew.LoadBoard(str(self.path)),
                pcbnew.LoadBoard(str(changed)),
                {"SIGNAL"},
            )

    def test_a_protected_net_and_a_branched_joint_are_not_modified(self):
        self.pair()
        self.assertIsNone(find_candidate(self.board, set()))
        self.trace((mm(3), mm(2)), (mm(3), mm(1)))
        self.assertIsNone(find_candidate(self.board, {"SIGNAL"}))

    def test_locked_track_is_not_modified(self):
        self.pair().SetLocked(True)
        self.assertIsNone(find_candidate(self.board, {"SIGNAL"}))

    def test_grouped_track_is_not_modified(self):
        track = self.pair()
        group = pcbnew.PCB_GROUP(self.board)
        self.board.Add(group)
        group.AddItem(track)
        self.assertIsNone(find_candidate(self.board, {"SIGNAL"}))

    def test_fix_that_creates_a_staircase_is_rejected(self):
        self.trace((mm(1), mm(1)), (mm(2), mm(2)))
        self.trace((mm(2), mm(2)), (mm(3), mm(2)))
        self.trace((mm(3), mm(2)), (mm(4) + 1, mm(3)))
        self.assertIsNone(find_candidate(self.board, {"SIGNAL"}))

    def test_pad_anchored_joint_is_not_moved(self):
        self.pair()
        footprint = pcbnew.FOOTPRINT(self.board)
        footprint.SetReference("J1")
        footprint.SetLayer(pcbnew.B_Cu)
        pad = pcbnew.PAD(footprint)
        pad.SetNumber("1")
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetSize(pcbnew.VECTOR2I(mm(0.5), mm(0.5)))
        pad.SetPosition(pcbnew.VECTOR2I(mm(3), mm(2)))
        layers = pcbnew.LSET()
        layers.AddLayer(pcbnew.B_Cu)
        pad.SetLayerSet(layers)
        pad.SetNetCode(self.board.FindNet("SIGNAL").GetNetCode())
        footprint.Add(pad)
        self.board.Add(footprint)
        self.assertIsNone(find_candidate(self.board, {"SIGNAL"}))

    def test_independent_proof_rejects_a_changed_width_and_protected_net(self):
        self.pair()
        proposal = find_candidate(self.board, {"SIGNAL"})
        before, after, old, new = self.snapshots(proposal["changes"])
        with self.assertRaisesRegex(ValueError, "protected"):
            verify_candidate(before, after, old, new, set())
        track = next(
            item for item in new.GetTracks() if isinstance(item, pcbnew.PCB_TRACK)
        )
        track.SetWidth(mm(0.3))
        changed = self.project / "changed.kicad_pcb"
        pcbnew.SaveBoard(str(changed), new)
        with self.assertRaisesRegex(ValueError, "attribute|another board item"):
            verify_candidate(
                before,
                changed.read_bytes(),
                old,
                pcbnew.LoadBoard(str(changed)),
                {"SIGNAL"},
            )

    def test_independent_proof_rejects_moving_a_joint_beyond_the_distance_limit(self):
        self.pair()
        proposal = find_candidate(self.board, {"SIGNAL"})
        before, _, old, new = self.snapshots(proposal["changes"])
        for item in new.GetTracks():
            a, b = item.GetStart(), item.GetEnd()
            if a.x == mm(3) + 1 and a.y == mm(2):
                item.SetStart(pcbnew.VECTOR2I(a.x + 100_000, a.y))
            if b.x == mm(3) + 1 and b.y == mm(2):
                item.SetEnd(pcbnew.VECTOR2I(b.x + 100_000, b.y))
        changed = self.project / "changed.kicad_pcb"
        pcbnew.SaveBoard(str(changed), new)
        with self.assertRaises(ValueError):
            verify_candidate(
                before,
                changed.read_bytes(),
                old,
                pcbnew.LoadBoard(str(changed)),
                {"SIGNAL"},
            )
