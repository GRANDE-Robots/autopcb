"""test_project_migration.py: check migrated and legacy project paths.

The tests do not modify live CAD.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import tempfile
import unittest
from pathlib import Path

from core.kicad_campaign import CampaignFile
from core.library_edits import library_path
from core.paths import asset_root, default_project
from core.project import ProjectPaths
from pcb.route_validation import _copy_inputs, _inputs


class ProjectMigrationTests(unittest.TestCase):
    """Tests for resolving legacy and migrated project paths and libraries."""

    def test_legacy_fixtures_and_migrated_support_resolve_separately(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            self.assertEqual(asset_root(project), project)
            self.assertEqual(
                CampaignFile(project).directory, project / "config/campaigns"
            )
            (project / "autopcb").mkdir()
            self.assertEqual(asset_root(project), project / "autopcb")
            self.assertEqual(
                CampaignFile(project).directory, project / "autopcb/config/campaigns"
            )
            (project / "New project.kicad_pro").write_text("{}")
            self.assertEqual(
                ProjectPaths(project).board, project / "New project.kicad_pcb"
            )
            self.assertEqual(
                ProjectPaths(project).review, project / "deliverables/review"
            )
            self.assertEqual(
                default_project(
                    str(project / "autopcb/scripts/review/render_views.py")
                ),
                project,
            )

    def test_migrated_libraries_are_guarded_and_copied_with_their_relative_path(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            tempfile.TemporaryDirectory() as copied,
        ):
            project, copy = Path(folder).resolve(), Path(copied).resolve()
            board = project / "v4.1.kicad_pcb"
            board.write_text("(kicad_pcb)", encoding="utf-8")
            library = project / "autopcb/libraries/test.pretty"
            library.mkdir(parents=True)
            footprint = library / "test.kicad_mod"
            footprint.write_text("(footprint test)", encoding="utf-8")
            self.assertEqual(
                library_path(project, "libraries/test.pretty/test.kicad_mod"), footprint
            )
            self.assertEqual(
                library_path(project, "autopcb/libraries/test.pretty/test.kicad_mod"),
                footprint,
            )
            self.assertIn(footprint, _inputs(board))
            _copy_inputs(_inputs(board), board, copy)
            self.assertEqual(
                (copy / "autopcb/libraries/test.pretty/test.kicad_mod").read_bytes(),
                footprint.read_bytes(),
            )
            with self.assertRaises(ValueError):
                library_path(project, "../outside.kicad_mod")

    def test_migrated_audit_inputs_include_local_symbols(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            library = project / "autopcb/libraries"
            library.mkdir(parents=True)
            symbol = library / "test.kicad_sym"
            symbol.write_text("(kicad_symbol_lib)", encoding="utf-8")
            self.assertIn(symbol, ProjectPaths(project).audit_inputs())


if __name__ == "__main__":
    unittest.main()
