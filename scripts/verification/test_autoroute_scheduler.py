"""Schedule every requested net in coordinated, nonconflicting batches."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import pcbnew

from pcb.route_optimizer import mm
from verification import test_route_optimizer as route_fixtures


class SchedulerTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec("pcb.net_routing.scheduler"))
        from pcb.net_routing import scheduler

        return scheduler

    def test_overlapping_nets_share_a_batch_and_separate_regions_run_together(self):
        api = self.api()
        work = (
            api.WorkItem("timing", 0, (0, 0, 4, 4), 3),
            api.WorkItem("driver", 1, (3, 3, 5, 5), 2),
            api.WorkItem("remote", 10, (20, 20, 25, 25), 2),
        )
        rounds = api.schedule(work, workers=2, max_batch_nets=4)
        self.assertEqual(len(rounds), 1)
        self.assertEqual({tuple(item.name for item in batch.items) for batch in rounds[0].batches}, {("timing", "driver"), ("remote",)})

    def test_conflicting_batches_do_not_run_at_the_same_time(self):
        api = self.api()
        work = tuple(api.WorkItem(f"n{i}", i, (0, 0, 5, 5), 2) for i in range(5))
        rounds = api.schedule(work, workers=2, max_batch_nets=2)
        self.assertEqual(len(rounds), 3)
        self.assertTrue(all(len(round_.batches) == 1 for round_ in rounds))
        self.assertEqual([item.name for round_ in rounds for batch in round_.batches for item in batch.items], [f"n{i}" for i in range(5)])

    def test_touching_windows_conflict_and_no_net_disappears(self):
        api = self.api()
        work = (
            api.WorkItem("a", 0, (0, 0, 2, 2), 2),
            api.WorkItem("b", 0, (2, 2, 4, 4), 2),
            api.WorkItem("c", 0, (9, 9, 10, 10), 2),
        )
        rounds = api.schedule(work, workers=2, max_batch_nets=1)
        self.assertEqual(len(rounds), 2)
        self.assertEqual({item.name for round_ in rounds for batch in round_.batches for item in batch.items}, {"a", "b", "c"})

    def test_duplicate_names_and_invalid_windows_are_rejected(self):
        api = self.api()
        item = api.WorkItem("a", 0, (0, 0, 1, 1), 2)
        with self.assertRaisesRegex(ValueError, "distinct"):
            api.schedule((item, item))
        with self.assertRaisesRegex(ValueError, "window"):
            api.WorkItem("bad", 0, (3, 1, 2, 5), 2)

    def test_whole_board_work_includes_an_orphan_track_island(self):
        api = self.api()
        self.assertTrue(hasattr(api, "work_from_board"))
        from pcb.net_routing.inventory import inventory_board

        with tempfile.TemporaryDirectory() as tmp:
            source = route_fixtures.RouteOptimizerTests.make_board(Path(tmp))
            board = pcbnew.LoadBoard(str(source))
            for a, b in (((2, 2), (8, 5)), ((2, 6), (4, 6))):
                track = pcbnew.PCB_TRACK(board)
                track.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1])))
                track.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
                track.SetLayer(pcbnew.F_Cu)
                track.SetWidth(mm(0.2))
                track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
                board.Add(track)
            result = api.work_from_board(board, inventory_board(board), {"SIGNAL": 0})
            self.assertEqual([(item.name, item.island_count) for item in result.items], [("SIGNAL", 2)])
            self.assertEqual(result.blockers, ())


if __name__ == "__main__":
    unittest.main()
