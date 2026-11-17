"""Automatic small-island checkpoints advance multi-terminal nets safely."""
from __future__ import annotations

import tempfile
import random
import unittest
from pathlib import Path

import pcbnew

from pcb.net_routing.batch import preview_round
from pcb.net_routing.model import Island, Net, Site
from pcb.net_routing.workflow import plan_nets
from pcb.route_optimizer import Point
from verification import test_net_routing as fixtures
from verification import test_route_optimizer as boards


def point(x, y):
    return Site(Point.from_mm([x, y]), "F.Cu")


class CheckpointRoutingTests(unittest.TestCase):
    def test_checkpoint_uses_nearest_of_all_retained_access_sites(self):
        from pcb.net_routing.checkpoint import choose_checkpoint

        sites = tuple(point(20 + i, 0) for i in range(12)) + (point(1, 0),)
        net = Net("N", (Island("many", sites), Island("target", (point(0, 0),))))
        selection = choose_checkpoint(net, maximum=2)
        self.assertEqual({site.point for site in selection.anchors},
                         {point(0, 0).point, point(1, 0).point})

    def test_nearest_access_pair_matches_exhaustive_search(self):
        from pcb.net_routing.checkpoint import _distance, _nearest

        rng = random.Random(20261029)
        layers = ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu")
        for _ in range(30):
            def make_site():
                return Site(Point(rng.randrange(100_000_000),
                                  rng.randrange(100_000_000)), rng.choice(layers))
            a = Island("a", tuple(make_site() for _ in range(48)))
            b = Island("b", tuple(make_site() for _ in range(48)))
            expected = min(((_distance(x, y), x.key, y.key) for x in a.sites for y in b.sites))
            actual = _nearest(a, b)
            self.assertEqual(actual[:3], expected)

    def test_checkpoint_selects_small_different_subsets(self):
        from pcb.net_routing.checkpoint import select_checkpoint

        net = Net("N", tuple(Island(str(i), (point(i * 2, 0),)) for i in range(5)))
        first, anchors = select_checkpoint(net, maximum=2, offset=0)
        second, _ = select_checkpoint(net, maximum=2, offset=1)
        self.assertEqual(len(first.islands), 2)
        self.assertEqual(len(anchors), 2)
        self.assertNotEqual(first.islands, second.islands)
        self.assertEqual(len(net.islands), 5)

    def test_checkpoint_window_is_local_and_keeps_original_grid_when_possible(self):
        from pcb.net_routing.checkpoint import search_window

        window, step = search_window((point(10, 10), point(12, 10)),
                                     (0., 0., 100., 100.), 4., .1, 4, 250000)
        self.assertEqual(window, (6., 6., 16., 14.))
        self.assertEqual(step, .1)
        self.assertLess((window[2] - window[0]) * (window[3] - window[1]), 100 * 100)

    def test_checkpoint_window_reserves_room_for_physical_access(self):
        from pcb.net_routing.checkpoint import route_access_inset, search_window

        inset = route_access_inset({"width_mm": .25, "clearance_mm": .2,
                                    "via": {"size_mm": .6, "drill_mm": .3}})
        self.assertEqual(inset, .5)
        window, step = search_window((point(2, 2), point(8, 5)),
                                     (0., 0., 10., 8.), .2, .5, 4, 250000,
                                     access_inset=inset)
        self.assertEqual(window, (1., 1., 9., 6.))
        self.assertEqual(step, .5)
        coarse, coarse_step = search_window((point(30, 30), point(70, 70)),
                                            (0., 0., 100., 100.), .2, .01, 4, 10000,
                                            access_inset=inset)
        self.assertGreater(coarse_step, .01)
        self.assertLessEqual(coarse[0], 30 - inset - coarse_step + 1e-6)
        self.assertGreaterEqual(coarse[2], 70 + inset + coarse_step - 1e-6)
        for invalid in (-.1, float("nan"), True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                search_window((point(2, 2), point(8, 5)), (0., 0., 10., 8.),
                              .2, .5, 4, 250000, access_inset=invalid)

    def test_three_island_choices_do_not_repeat_the_same_set_or_wrap(self):
        from pcb.net_routing.checkpoint import CheckpointExhausted, choose_checkpoint

        net = Net("N", tuple(Island(str(i), (point(i * 2, 0),)) for i in range(6)))
        first = choose_checkpoint(net, maximum=3)
        selections = [choose_checkpoint(net, maximum=3, offset=i) for i in range(first.count)]
        signatures = [frozenset(island.identifier for island in choice.net.islands) for choice in selections]
        self.assertEqual(len(signatures), len(set(signatures)))
        with self.assertRaises(CheckpointExhausted):
            choose_checkpoint(net, maximum=3, offset=first.count)
        small = Net("N", net.islands[:3])
        self.assertEqual(choose_checkpoint(small, maximum=3).count, 1)

    def test_partial_tree_is_native_checked_without_claiming_the_whole_net(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = boards.RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            fixtures.NativeNetRoutingTests.add_pad(board, "J3", (2, 6), net_name="SIGNAL")
            fixtures.NativeNetRoutingTests.add_pad(board, "J4", (8, 6), net_name="SIGNAL")
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            config = fixtures.NativeNetRoutingTests.config()
            config["max_islands"] = 2
            result = plan_nets(config, project, validate=True, include_changes=True)
            self.assertEqual(result["status"], "validated_sampled_candidate", result)
            self.assertEqual(result["scope"], "partial_net_checkpoint")
            self.assertNotIn("whole_net_copper_connectivity", result["validation"])
            self.assertLess(result["validation"]["candidate_unconnected"], result["validation"]["baseline_unconnected"])
            self.assertGreater(result["validation"]["candidate_unconnected"], 0)
            self.assertEqual(result["validation"]["broken_existing_connections"], [])
            self.assertEqual(source.read_bytes(), original)

    def test_batch_validates_partial_endpoints_and_retains_original_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = boards.RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            fixtures.NativeNetRoutingTests.add_pad(board, "J3", (2, 6), net_name="SIGNAL")
            pcbnew.SaveBoard(str(source), board)
            (project / "v4.1.kicad_sch").write_text('(kicad_sch (lib_symbols))', encoding="utf-8")
            original = source.read_bytes()
            config = fixtures.NativeNetRoutingTests.config()
            config["max_islands"] = 2
            result = preview_round(project, [config], workers=1)
            self.assertEqual(result["status"], "validated_preview", result)
            self.assertEqual(result["scope"], "partial_net_checkpoint")
            self.assertNotIn("whole_net_copper_connectivity", result["validation"])
            self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
