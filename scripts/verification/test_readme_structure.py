"""test_readme_structure.py: check the audit's README table and root section rules.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from verification.audit_code_health import RuleFindings

TABLE = """# folder

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`one.py`](one.py) | First. | None | None |
| `two.txt` | Second. | None | None |
| [`sub/`](sub/README.md) | Folder. | None | None |
"""


class ReadmeStructureTests(unittest.TestCase):
    """The File Structure table must match what Git tracks in a folder."""

    def setUp(self):
        """Create an empty support folder."""
        self.temp = tempfile.TemporaryDirectory(prefix="autopcb-readme-")
        self.addCleanup(self.temp.cleanup)
        self.support = Path(self.temp.name)
        self.findings = RuleFindings(self.support)

    def write(self, text: str) -> Path:
        """Write the README of the support folder and return its path."""
        path = self.support / "README.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_listed_entries_reads_linked_and_plain_names_without_the_slash(self):
        listed = RuleFindings.listed_entries(self.write(TABLE))
        self.assertEqual(listed, {"one.py", "two.txt", "sub"})

    def test_a_readme_without_the_section_lists_nothing(self):
        self.assertIsNone(RuleFindings.listed_entries(self.write("# folder\n")))

    def test_a_table_that_matches_the_tracked_entries_passes(self):
        self.write(TABLE)
        tracked = {"one.py", "two.txt", "sub"}
        with mock.patch.object(RuleFindings, "tracked_entries", return_value=tracked):
            self.assertFalse(self.findings.readme_findings(self.support))

    def test_a_missing_row_is_reported(self):
        self.write(TABLE)
        tracked = {"one.py", "two.txt", "sub", "three.py"}
        with mock.patch.object(RuleFindings, "tracked_entries", return_value=tracked):
            self.assertTrue(self.findings.readme_findings(self.support))

    def test_a_row_for_a_file_that_is_not_tracked_is_reported(self):
        self.write(TABLE)
        with mock.patch.object(
            RuleFindings, "tracked_entries", return_value={"one.py", "sub"}
        ):
            self.assertTrue(self.findings.readme_findings(self.support))

    def test_a_readme_without_a_table_is_reported(self):
        self.write("# folder\n")
        with mock.patch.object(RuleFindings, "tracked_entries", return_value={"a"}):
            self.assertTrue(self.findings.readme_findings(self.support))

    def test_a_tree_that_is_not_a_git_checkout_is_not_checked(self):
        self.write("# folder\n")
        self.assertIsNone(self.findings.tracked_entries(self.support))
        self.assertFalse(self.findings.readme_findings(self.support))

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_tracked_entries_name_what_git_tracks_directly_in_a_folder(self):
        (self.support / "pack").mkdir()
        (self.support / "pack" / "inner.py").write_text("x = 1\n", encoding="utf-8")
        (self.support / "top.txt").write_text("top\n", encoding="utf-8")
        (self.support / "loose.txt").write_text("not added\n", encoding="utf-8")
        self.write("# root\n")
        for command in (["init", "-q"], ["add", "pack", "top.txt", "README.md"]):
            subprocess.run(["git", "-C", str(self.support), *command], check=True)
        self.assertEqual(
            self.findings.tracked_entries(self.support), {"pack", "top.txt"}
        )
        self.assertEqual(
            self.findings.tracked_entries(self.support / "pack"), {"inner.py"}
        )

    def test_the_root_readme_needs_the_sections_in_order(self):
        sections = RuleFindings.ROOT_SECTIONS
        self.write(
            "# Name\n\n" + "".join(f"## {name}\n\ntext\n\n" for name in sections)
        )
        self.assertFalse(self.findings.root_sections_wrong())
        self.write(
            "# Name\n\n" + "".join(f"## {name}\n\n" for name in reversed(sections))
        )
        self.assertTrue(self.findings.root_sections_wrong())
        self.write("# Name\n\n## Overview\n\ntext\n")
        self.assertTrue(self.findings.root_sections_wrong())

    def test_a_missing_root_readme_is_reported(self):
        self.assertTrue(self.findings.root_sections_wrong())


class RepositoryReadmeTests(unittest.TestCase):
    """The READMEs of this repository match the files it tracks."""

    def test_folder_tables_and_root_sections_have_no_finding(self):
        found = RuleFindings(Path(__file__).resolve().parents[2]).collect()
        self.assertEqual(found["readme_structure"], [])
        self.assertEqual(found["root_readme_sections"], [])


if __name__ == "__main__":
    unittest.main()
