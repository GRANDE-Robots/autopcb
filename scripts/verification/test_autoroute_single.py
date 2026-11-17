"""One-net searches use the parallel scheduler without losing resume state."""

import json
import tempfile
import unittest
from pathlib import Path

from pcb.net_routing.runner import run_project
from verification import test_autoroute_batch as batch_fixtures
from verification import test_net_routing as net_fixtures


class SingleNetSearchTests(unittest.TestCase):
    def project(self, project):
        source = batch_fixtures.CombinedBatchTests().project(project)
        (project / "v4.1.kicad_sch").write_text(
                '(kicad_sch (version 20250114) (generator "eeschema") '
                '(uuid "6b6b7707-0425-4195-9c79-1824e2f1a24e") (paper "A4") '
                '(lib_symbols) (sheet_instances (path "/" (page "1"))) (embedded_fonts no))',
                encoding="utf-8",
            )
        config = net_fixtures.NativeNetRoutingTests.config()
        (project / "config").mkdir()
        policy = {"schema": 1, "max_pins": 3, "route": config["shared"], "search": config["search"]}
        (project / "config" / "autopcb.json").write_text(json.dumps(policy), encoding="utf-8")
        return source

    def test_single_net_planning_is_resumable_and_keeps_saved_board(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            source = self.project(project)
            before = source.read_bytes()
            result = run_project(project, max_rounds=0, workers=2, single_net_search=True)
            path = project / "results" / "autoroute" / (result["run_id"] + ".json")
            state = json.loads(path.read_text())
            self.assertTrue(state["single_net_search"])
            self.assertTrue(state["queue"])
            self.assertTrue(all(len(request["nets"]) == 1 for group in state["queue"] for request in group))
            resumed = run_project(project, resume=result["run_id"], max_rounds=0, workers=2)
            self.assertTrue(resumed["single_net_search"])
            self.assertEqual(source.read_bytes(), before)

    def test_resume_can_rebatch_in_place_and_persist_fixed_placement(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            source = self.project(project)
            before = source.read_bytes()
            initial = run_project(project, max_rounds=0, workers=2)
            run_id = initial["run_id"]
            resumed = run_project(project, resume=run_id, max_rounds=0, workers=2,
                                  single_net_search=True, keep_placement=True)
            state_path = project / "results" / "autoroute" / f"{run_id}.json"
            state = json.loads(state_path.read_text())
            self.assertEqual(resumed["run_id"], run_id)
            self.assertTrue(state["single_net_search"])
            self.assertTrue(state["keep_placement"])
            self.assertTrue(all(len(request["nets"]) == 1 and not request.get("movable")
                                for group in state["queue"] for request in group))
            self.assertNotIn("renewed_from", state)
            self.assertEqual(len(list(state_path.parent.glob("*.json"))), 1)
            run_project(project, resume=run_id, max_rounds=0, workers=2)
            self.assertTrue(json.loads(state_path.read_text())["keep_placement"])
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
