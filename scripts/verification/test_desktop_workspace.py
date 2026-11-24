"""test_desktop_workspace.py: check which workspace the desktop application opens.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autopcb.cli import DesktopWorkspace


class DesktopWorkspaceTests(unittest.TestCase):
    """The desktop opens a per-user workspace only in an installed build."""

    def setUp(self):
        """Point the home folder and the data variables at a disposable folder."""
        self.temp = tempfile.TemporaryDirectory(prefix="autopcb-workspace-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def select(self, platform: str, *, frozen: bool = True, **environment) -> Path:
        """Select the workspace as an installed or source run on one platform."""
        with contextlib.ExitStack() as patches:
            patches.enter_context(mock.patch("autopcb.cli.sys.platform", platform))
            patches.enter_context(
                mock.patch("autopcb.cli.sys.frozen", frozen, create=True)
            )
            patches.enter_context(
                mock.patch("autopcb.cli.Path.home", return_value=self.home)
            )
            patches.enter_context(
                mock.patch.dict("autopcb.cli.os.environ", environment, clear=True)
            )
            return DesktopWorkspace.select(None)

    def test_a_requested_workspace_is_used_as_given(self):
        requested = self.home / "mine"
        self.assertEqual(DesktopWorkspace.select(requested), requested)
        self.assertFalse(requested.exists())

    def test_a_source_run_opens_the_current_folder(self):
        for platform in ("win32", "darwin", "linux"):
            self.assertEqual(self.select(platform, frozen=False), Path("."))

    def test_installed_windows_uses_local_app_data(self):
        local = self.home / "Local"
        workspace = self.select("win32", LOCALAPPDATA=str(local))
        self.assertEqual(workspace, local / "AutoPCB" / "Workspace")
        self.assertTrue(workspace.is_dir())

    def test_installed_windows_falls_back_to_the_home_folder(self):
        workspace = self.select("win32")
        self.assertEqual(workspace, self.home / "AppData/Local/AutoPCB/Workspace")

    def test_installed_macos_uses_application_support(self):
        workspace = self.select("darwin")
        expected = self.home / "Library/Application Support/AutoPCB/Workspace"
        self.assertEqual(workspace, expected)
        self.assertTrue(workspace.is_dir())

    def test_installed_linux_uses_the_data_home_when_set(self):
        data = self.home / "data"
        workspace = self.select("linux", XDG_DATA_HOME=str(data))
        self.assertEqual(workspace, data / "AutoPCB" / "Workspace")
        self.assertTrue(workspace.is_dir())

    def test_installed_linux_defaults_to_the_local_share_folder(self):
        workspace = self.select("linux")
        self.assertEqual(workspace, self.home / ".local/share/AutoPCB/Workspace")

    def test_an_uncreatable_workspace_is_reported(self):
        (self.home / ".local").write_text("a file, not a folder", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            self.select("linux")
        self.assertIn("Could not create the AutoPCB workspace", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
