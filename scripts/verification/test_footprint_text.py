"""Check coordinate conversion for bottom-side KiCad footprint fields."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pcbnew

from core.kicad_campaign import CampaignPlanner
from pcb.footprint_text import SilkTextMove
import verification.test_route_optimizer as fixtures


class FootprintTextTests(unittest.TestCase):
    def test_rotated_bottom_field_uses_native_coordinates_without_touching_source(self):
        with tempfile.TemporaryDirectory(prefix="jessie-text-test-") as directory:
            project = Path(directory)
            source = fixtures.RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            footprint = pcbnew.FOOTPRINT(board)
            footprint.SetReference("R50")
            footprint.SetLayer(pcbnew.B_Cu)
            footprint.SetPosition(pcbnew.VECTOR2I(5_000_000, 4_000_000))
            footprint.SetOrientationDegrees(90)
            footprint.Reference().SetLayer(pcbnew.B_SilkS)
            footprint.Reference().SetVisible(True)
            footprint.Reference().SetTextSize(pcbnew.VECTOR2I(400_000, 400_000))
            board.Add(footprint)
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()

            result = SilkTextMove.position_change(project, "R50", "Reference", 4.0, 4.5)
            change = result["change"]
            self.assertEqual(change["coordinate_space"], "footprint_local")
            unsafe = dict(change)
            unsafe.pop("coordinate_space")
            with self.assertRaisesRegex(ValueError, "footprint-local"):
                CampaignPlanner(project).plan("pcb", [unsafe])
            candidate = CampaignPlanner(project).plan("pcb", [change])
            trial = project / "trial.kicad_pcb"
            trial.write_bytes(candidate.after)
            moved = pcbnew.LoadBoard(str(trial)).FindFootprintByReference("R50")
            self.assertEqual(
                (moved.Reference().GetPosition().x, moved.Reference().GetPosition().y),
                (4_000_000, 4_500_000),
            )
            self.assertEqual(
                (moved.GetPosition().x, moved.GetPosition().y), (5_000_000, 4_000_000)
            )
            self.assertEqual(source.read_bytes(), original)

            with self.assertRaisesRegex(ValueError, "outside"):
                SilkTextMove.position_change(project, "R50", "Reference", 50, 50)
            with self.assertRaisesRegex(ValueError, "bottom-side"):
                SilkTextMove.position_change(project, "J1", "Reference", 4, 4)
            self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
