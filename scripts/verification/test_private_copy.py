"""Private copies carry the reviewed policy and refuse inputs that change mid-run."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autopcb.benchmark import Benchmark
from autopcb.private_copy import PrivateCopy
from core.paths import ProjectStorage
from core.project import ProjectPaths


class PrivateCopyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        for suffix in (".kicad_pro", ".kicad_pcb", ".kicad_sch"):
            (root / f"board{suffix}").write_text(suffix, encoding="utf-8")
        self.project = ProjectPaths(root / "board.kicad_pro")
        self.storage = ProjectStorage(root)
        self.policy = self.storage.config / "autopcb.json"
        self.policy.parent.mkdir(parents=True)
        self.policy.write_text(json.dumps({"schema": 1, "recovery": {"stages": 3}}), encoding="utf-8")

    def test_recovery_override_reaches_only_the_copy(self):
        copy = PrivateCopy(self.project, self.storage)
        with copy.open(Benchmark.GREEDY) as (working, _):
            copied = json.loads((working.root / copy.policy).read_text(encoding="utf-8"))
        self.assertEqual(copied["recovery"], Benchmark.GREEDY)
        self.assertEqual(json.loads(self.policy.read_text(encoding="utf-8"))["recovery"], {"stages": 3})

    def test_policy_change_during_a_run_is_refused(self):
        copy = PrivateCopy(self.project, self.storage)
        with self.assertRaisesRegex(RuntimeError, "changed during the run"):
            with copy.open():
                self.policy.write_text(json.dumps({"schema": 1}), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
