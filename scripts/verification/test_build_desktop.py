"""test_build_desktop.py: check the Debian package that the desktop builder writes.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

BUILDER = Path(__file__).resolve().parents[2] / "packaging" / "build_desktop.py"


class DebianPackageTests(unittest.TestCase):
    """The package depends on KiCad 10 and installs a launcher, an entry and an icon."""

    @classmethod
    def setUpClass(cls):
        """Load the builder and build a package from a stand-in application bundle."""
        if not shutil.which("dpkg-deb"):
            raise unittest.SkipTest("dpkg-deb is not installed")
        spec = importlib.util.spec_from_file_location("build_desktop", BUILDER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.temp = tempfile.TemporaryDirectory(prefix="autopcb-deb-")
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        bundle = root / "dist" / "AutoPCB"
        bundle.mkdir(parents=True)
        (bundle / "AutoPCB").write_text("#!/bin/sh\n", encoding="utf-8")
        output = root / "out"
        output.mkdir()
        cls.package = module.InstallerBuilder.linux(bundle, output, "1.2.3")

    def dpkg(self, option: str) -> str:
        """Return the text of one dpkg-deb listing of the built package."""
        return subprocess.run(
            ["dpkg-deb", option, str(self.package)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def test_the_package_is_named_for_its_version_and_architecture(self):
        self.assertRegex(self.package.name, r"^autopcb_1\.2\.3_[a-z0-9]+\.deb$")

    def test_the_package_depends_on_kicad_10(self):
        self.assertIn("Depends: kicad (>= 10.0)", self.dpkg("--info"))

    def test_the_package_installs_the_launcher_entry_and_icon(self):
        listing = self.dpkg("--contents")
        for installed in (
            "./opt/autopcb/AutoPCB",
            "./usr/bin/autopcb -> /opt/autopcb/AutoPCB",
            "./usr/share/applications/autopcb.desktop",
            "./usr/share/icons/hicolor/scalable/apps/autopcb.svg",
        ):
            self.assertIn(installed, listing)

    def test_the_desktop_entry_launches_the_desktop_with_the_icon(self):
        entry = subprocess.run(
            ["dpkg-deb", "--fsys-tarfile", str(self.package)],
            capture_output=True,
            check=True,
        ).stdout
        temp = Path(self.temp.name) / "unpacked"
        temp.mkdir()
        subprocess.run(["tar", "-x", "-C", str(temp)], input=entry, check=True)
        text = (temp / "usr/share/applications/autopcb.desktop").read_text()
        self.assertIn("Exec=/usr/bin/autopcb desktop", text)
        self.assertIn("Icon=autopcb", text)


if __name__ == "__main__":
    unittest.main()
