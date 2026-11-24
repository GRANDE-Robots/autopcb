"""test_staircase_repair.py: tests of the guarded staircase cleanup.

Guarded cleanup of existing staircase-shaped signal copper.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import tempfile
import unittest
from pathlib import Path

import pcbnew

from core.kicad_campaign import CampaignPlanner
from core.kicad_sexpr import parse
from pcb.freeroute_delta import read_copper
from pcb.net_routing.style import audit_style


class StaircaseRepairTests(unittest.TestCase):
    """Tests that staircase shortcuts keep the chain ends and are proved first."""

    def add_chain(self, board, name, offset):
        """Add a net with two pads joined by a staircase of three F.Cu tracks.

        Inputs:
            board: Board that receives the net, a footprint, the pads and the tracks.
            name: Net name.
            offset: Y offset of the whole chain, in nm.
        """
        net = pcbnew.NETINFO_ITEM(board, name)
        board.Add(net)
        footprint = pcbnew.FOOTPRINT(board)
        board.Add(footprint)
        for x, y in ((0, 0), (3_000_000, 2_000_000)):
            pad = pcbnew.PAD(footprint)
            pad.SetPosition(pcbnew.VECTOR2I(x, y + offset))
            pad.SetSize(pcbnew.VECTOR2I(300_000, 300_000))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pcbnew.LSET.FrontMask())
            pad.SetNetCode(net.GetNetCode())
            footprint.Add(pad)
        points = (
            (0, 0),
            (1_000_000, 1_000_000),
            (2_000_000, 1_000_000),
            (3_000_000, 2_000_000),
        )
        for a, b in zip(points, points[1:]):
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(a[0], a[1] + offset))
            track.SetEnd(pcbnew.VECTOR2I(b[0], b[1] + offset))
            track.SetWidth(200_000)
            track.SetLayer(pcbnew.F_Cu)
            track.SetNetCode(net.GetNetCode())
            board.Add(track)

    def board(self):
        """Build a four-copper-layer board with one staircase chain on the net SIG."""
        board = pcbnew.BOARD()
        board.SetCopperLayerCount(4)
        self.add_chain(board, "SIG", 0)
        return board

    def test_shortcut_preserves_the_chain_ends_and_reduces_staircases(self):
        from pcb.net_routing.staircase_repair import find_candidates, verify_candidate

        board = self.board()
        self.assertEqual(len(audit_style(board).staircases), 1)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "v4.1.kicad_pcb"
            self.assertTrue(pcbnew.SaveBoard(str(source), board))
            before = source.read_bytes()
            candidates = find_candidates(
                board, read_copper(source), {"SIG"}, lambda _: True
            )
            self.assertTrue(candidates)
            after = (
                CampaignPlanner(Path(folder))
                .plan("pcb", candidates[0]["changes"])
                .after
            )
            self.assertEqual(source.read_bytes(), before)
            source.write_bytes(after)
            revised = pcbnew.LoadBoard(str(source))
            proof = verify_candidate(before, after, board, revised, {"SIG"})
            self.assertGreater(proof["corrected"], 0)
            self.assertEqual(proof["net"], "SIG")
            self.assertEqual(len(audit_style(revised).staircases), 0)
            with self.assertRaisesRegex(ValueError, "eligible"):
                verify_candidate(before, after, board, revised, set())

    def test_shortcuts_on_separate_nets_are_proved_together(self):
        from pcb.net_routing.staircase_repair import find_candidates, verify_candidate

        board = self.board()
        self.add_chain(board, "SIG2", 5_000_000)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "v4.1.kicad_pcb"
            self.assertTrue(pcbnew.SaveBoard(str(source), board))
            before = source.read_bytes()
            candidates = find_candidates(
                board, read_copper(source), {"SIG", "SIG2"}, lambda _: True
            )
            self.assertEqual({item["net"] for item in candidates}, {"SIG", "SIG2"})
            after = (
                CampaignPlanner(Path(folder))
                .plan(
                    "pcb", [change for item in candidates for change in item["changes"]]
                )
                .after
            )
            source.write_bytes(after)
            revised = pcbnew.LoadBoard(str(source))
            proof = verify_candidate(before, after, board, revised, {"SIG", "SIG2"})
            self.assertEqual(proof["corrected"], 2)
            self.assertEqual(proof["nets"], ["SIG", "SIG2"])
            with self.assertRaisesRegex(ValueError, "eligible"):
                verify_candidate(before, after, board, revised, {"SIG"})

    def test_reordered_unchanged_vias_are_allowed_but_moved_vias_are_rejected(self):
        from pcb.net_routing.staircase_repair import find_candidates, verify_candidate

        board = self.board()
        for x in (10_000_000, 12_000_000):
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(pcbnew.VECTOR2I(x, 0))
            via.SetWidth(600_000)
            via.SetDrill(300_000)
            via.SetViaType(pcbnew.VIATYPE_THROUGH)
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetNetCode(board.FindNet("SIG").GetNetCode())
            board.Add(via)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "v4.1.kicad_pcb"
            self.assertTrue(pcbnew.SaveBoard(str(source), board))
            before = source.read_bytes()
            proposals = find_candidates(
                board, read_copper(source), {"SIG"}, lambda _: True
            )
            text = (
                CampaignPlanner(Path(folder))
                .plan("pcb", proposals[0]["changes"])
                .after.decode()
            )
            first, second = parse(text).children("via")
            swapped = (
                text[: first.start]
                + text[second.start : second.end]
                + text[first.end : second.start]
                + text[first.start : first.end]
                + text[second.end :]
            )
            after = swapped.encode()
            source.write_bytes(after)
            revised = pcbnew.LoadBoard(str(source))
            self.assertGreater(
                verify_candidate(before, after, board, revised, {"SIG"})["corrected"], 0
            )
            via = next(
                item for item in revised.GetTracks() if isinstance(item, pcbnew.PCB_VIA)
            )
            via.SetPosition(pcbnew.VECTOR2I(15_000_000, 0))
            self.assertTrue(pcbnew.SaveBoard(str(source), revised))
            with self.assertRaisesRegex(ValueError, "non-track board item"):
                verify_candidate(
                    before,
                    source.read_bytes(),
                    board,
                    pcbnew.LoadBoard(str(source)),
                    {"SIG"},
                )

    def test_reordered_unchanged_footprints_are_allowed_but_moved_ones_are_rejected(
        self,
    ):
        from pcb.net_routing.staircase_repair import find_candidates, verify_candidate

        board = self.board()
        spare = pcbnew.FOOTPRINT(board)
        spare.SetReference("J99")
        spare.SetPosition(pcbnew.VECTOR2I(12_000_000, 0))
        board.Add(spare)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "v4.1.kicad_pcb"
            self.assertTrue(pcbnew.SaveBoard(str(source), board))
            before = source.read_bytes()
            proposals = find_candidates(
                board, read_copper(source), {"SIG"}, lambda _: True
            )
            text = (
                CampaignPlanner(Path(folder))
                .plan("pcb", proposals[0]["changes"])
                .after.decode()
            )
            first, second = parse(text).children("footprint")
            swapped = (
                text[: first.start]
                + text[second.start : second.end]
                + text[first.end : second.start]
                + text[first.start : first.end]
                + text[second.end :]
            )
            after = swapped.encode()
            source.write_bytes(after)
            revised = pcbnew.LoadBoard(str(source))
            self.assertGreater(
                verify_candidate(before, after, board, revised, {"SIG"})["corrected"], 0
            )
            revised.FindFootprintByReference("J99").SetPosition(
                pcbnew.VECTOR2I(15_000_000, 0)
            )
            self.assertTrue(pcbnew.SaveBoard(str(source), revised))
            with self.assertRaisesRegex(ValueError, "non-track board item"):
                verify_candidate(
                    before,
                    source.read_bytes(),
                    board,
                    pcbnew.LoadBoard(str(source)),
                    {"SIG"},
                )

    def test_shortcut_never_selects_locked_or_ineligible_copper(self):
        from pcb.net_routing.staircase_repair import find_candidates

        board = self.board()
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "v4.1.kicad_pcb"
            self.assertTrue(pcbnew.SaveBoard(str(source), board))
            copper = read_copper(source)
            self.assertEqual(find_candidates(board, copper, set(), lambda _: True), ())
            next(iter(board.GetTracks())).SetLocked(True)
            self.assertEqual(
                find_candidates(board, copper, {"SIG"}, lambda _: True), ()
            )


if __name__ == "__main__":
    unittest.main()
