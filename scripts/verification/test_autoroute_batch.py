"""Search independent batches together and validate their combined copper once."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pcbnew

from pcb.route_optimizer import mm
from verification import test_net_routing as net_fixtures
from verification import test_route_optimizer as route_fixtures
from core.toolchain import kicad_cli


class CombinedBatchTests(unittest.TestCase):
    def test_previous_diagonal_trace_does_not_block_empty_box_corners(self):
        from pcb.route_batch import _previous_obstacles
        from pcb.route_cli import _obstacle
        from pcb.route_optimizer import GridRouter, Point

        route = {
            "net": "A",
            "vias": [],
            "segments": [
                {
                    "from_mm": [1, 1],
                    "to_mm": [1.5, 1.5],
                    "width_mm": 0.2,
                    "layer": "F.Cu",
                }
            ],
        }
        obstacles = [_obstacle(item) for item in _previous_obstacles(route)]
        clear = Point.from_mm((1, 1.5))
        collision = Point.from_mm((1.25, 1.25))
        self.assertFalse(
            any(
                GridRouter._intersects_box(clear, clear, item, mm(0.2))
                for item in obstacles
            )
        )
        self.assertTrue(
            any(
                GridRouter._intersects_box(collision, collision, item, mm(0.2))
                for item in obstacles
            )
        )

    def test_capsule_configuration_rejects_incomplete_or_unsafe_bounds(self):
        from pcb.route_cli import _obstacle

        base = {
            "bounds_mm": [0, 0, 2, 2],
            "layers": ["F.Cu"],
            "net": "N",
            "kind": "copper",
        }
        self.assertIsNone(_obstacle(base).centerline)
        for fields in (
            {"centerline_mm": [[0.5, 0.5], [1.5, 1.5]]},
            {"radius_mm": 0.1},
            {"centerline_mm": [[0.5, 0.5], [1.5, 1.5]], "radius_mm": -0.1},
            {"centerline_mm": [[0, 0], [2, 2]], "radius_mm": 0.1},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                _obstacle({**base, **fields})

    def test_previous_trace_capsules_cover_the_entire_segment_without_gaps(self):
        from pcb.route_batch import _previous_obstacles
        from pcb.route_cli import _obstacle
        from pcb.route_optimizer import GridRouter, Point

        route = {
            "net": "A",
            "vias": [],
            "segments": [
                {"from_mm": [1, 1], "to_mm": [5, 5], "width_mm": 0.2, "layer": "F.Cu"}
            ],
        }
        obstacles = [_obstacle(item) for item in _previous_obstacles(route)]
        for index in range(401):
            coordinate = mm(1) + index * 10_000
            point = Point(coordinate, coordinate)
            self.assertTrue(
                any(
                    GridRouter._intersects_box(point, point, item, 0)
                    for item in obstacles
                )
            )

    def test_capsules_match_independent_point_to_segment_distances(self):
        import math
        import random

        from pcb.route_batch import _previous_obstacles
        from pcb.route_cli import _obstacle
        from pcb.route_optimizer import GridRouter, Point

        randomizer = random.Random(4519)
        paths = (((1, 1), (5, 5)), ((1, 5), (5, 1)), ((1, 3), (5, 3)), ((3, 1), (3, 5)))
        for start, end in paths:
            route = {
                "net": "A",
                "vias": [],
                "segments": [
                    {"from_mm": start, "to_mm": end, "width_mm": 0.2, "layer": "F.Cu"}
                ],
            }
            obstacles = [_obstacle(item) for item in _previous_obstacles(route)]
            first, last = Point.from_mm(start), Point.from_mm(end)
            dx, dy = last.x - first.x, last.y - first.y
            for _ in range(400):
                fraction = randomizer.random()
                point = Point(
                    round(first.x + fraction * dx)
                    + randomizer.randrange(-300_000, 300_001),
                    round(first.y + fraction * dy)
                    + randomizer.randrange(-300_000, 300_001),
                )
                projection = max(
                    0.0,
                    min(
                        1.0,
                        ((point.x - first.x) * dx + (point.y - first.y) * dy)
                        / (dx * dx + dy * dy),
                    ),
                )
                distance = math.hypot(
                    point.x - (first.x + projection * dx),
                    point.y - (first.y + projection * dy),
                )
                modeled = any(
                    GridRouter._intersects_box(point, point, item, mm(0.15))
                    for item in obstacles
                )
                if distance < mm(0.25) - 5_000:
                    self.assertTrue(modeled)
                elif distance > mm(0.25) + 5_000:
                    self.assertFalse(modeled)

    def test_native_drc_agrees_with_diagonal_capsule_clearance(self):

        from pcb.route_batch import _previous_obstacles
        from pcb.route_cli import _obstacle
        from pcb.route_optimizer import GridRouter, Point
        from pcb.route_validation import _drc

        with tempfile.TemporaryDirectory(prefix="autopcb-capsule-drc-") as directory:
            root = Path(directory)
            source = route_fixtures.RouteOptimizerTests.make_board(root)
            board = pcbnew.LoadBoard(str(source))
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            tracks = []
            for net in ("SIGNAL", "OTHER"):
                track = pcbnew.PCB_TRACK(board)
                track.SetWidth(mm(0.2))
                track.SetLayer(pcbnew.F_Cu)
                track.SetNetCode(board.FindNet(net).GetNetCode())
                board.Add(track)
                tracks.append(track)
            tracks[0].SetStart(pcbnew.VECTOR2I(mm(1), mm(1)))
            tracks[0].SetEnd(pcbnew.VECTOR2I(mm(1.5), mm(1.5)))
            source.with_suffix(".kicad_dru").write_text("""(version 1)
              (rule "track gap" (condition "A.Type == 'Track' && B.Type == 'Track'")
                (constraint clearance (min 0.10mm)))""")
            route = {
                "net": "SIGNAL",
                "vias": [],
                "segments": [
                    {
                        "from_mm": [1, 1],
                        "to_mm": [1.5, 1.5],
                        "width_mm": 0.2,
                        "layer": "F.Cu",
                    }
                ],
            }
            obstacles = [_obstacle(item) for item in _previous_obstacles(route)]
            for index, (y, expected_collision) in enumerate(
                ((1.45, False), (1.42, True))
            ):
                tracks[1].SetStart(pcbnew.VECTOR2I(mm(1), mm(y)))
                tracks[1].SetEnd(pcbnew.VECTOR2I(mm(1), mm(round(y + 0.1, 6))))
                pcbnew.SaveBoard(str(source), board)
                report = _drc(kicad_cli(), source, root / f"drc_{index}.json")
                ids = {track.m_Uuid.AsString() for track in tracks}
                native_collision = any(
                    item.get("type") == "clearance"
                    and ids <= {part.get("uuid") for part in item.get("items", [])}
                    for item in report["violations"]
                )
                first, last = (
                    Point.from_mm((1, y)),
                    Point.from_mm((1, round(y + 0.1, 6))),
                )
                modeled_collision = any(
                    GridRouter._intersects_box(first, last, item, mm(0.2))
                    for item in obstacles
                )
                self.assertEqual(
                    native_collision, expected_collision, report["violations"]
                )
                self.assertEqual(modeled_collision, native_collision)

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec("pcb.net_routing.batch"))
        from pcb.net_routing import batch

        return batch

    def project(self, path):
        source = route_fixtures.RouteOptimizerTests.make_board(path)
        board = pcbnew.LoadBoard(str(source))
        for item in list(board.GetDrawings()):
            if item.GetLayer() == pcbnew.Edge_Cuts:
                board.Remove(item)
        for x1, y1, x2, y2 in (
            (0, 0, 30, 0),
            (30, 0, 30, 8),
            (30, 8, 0, 8),
            (0, 8, 0, 0),
        ):
            edge = pcbnew.PCB_SHAPE(board)
            edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
            edge.SetLayer(pcbnew.Edge_Cuts)
            edge.SetStart(pcbnew.VECTOR2I(mm(x1), mm(y1)))
            edge.SetEnd(pcbnew.VECTOR2I(mm(x2), mm(y2)))
            edge.SetWidth(mm(0.05))
            board.Add(edge)
        board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
        net_fixtures.NativeNetRoutingTests.add_pad(
            board, "J3", (20, 2), net_name="OTHER"
        )
        net_fixtures.NativeNetRoutingTests.add_pad(
            board, "J4", (26, 5), net_name="OTHER"
        )
        pcbnew.SaveBoard(str(source), board)
        (path / "v4.1.kicad_sch").write_text(
            "(kicad_sch (lib_symbols))", encoding="utf-8"
        )
        netclass = {
            "bus_width": 12,
            "clearance": 0.2,
            "diff_pair_gap": 0.25,
            "diff_pair_via_gap": 0.25,
            "diff_pair_width": 0.2,
            "line_style": 0,
            "microvia_diameter": 0.3,
            "microvia_drill": 0.1,
            "pcb_color": "rgba(0, 0, 0, 0.000)",
            "schematic_color": "rgba(0, 0, 0, 0.000)",
            "track_width": 0.2,
            "tuning_profile": "",
            "via_diameter": 0.6,
            "via_drill": 0.3,
            "wire_width": 6,
        }
        settings = {
            "classes": [
                {**netclass, "name": "Default", "priority": 2147483647},
                {**netclass, "name": "CORE_SIGNAL", "priority": 0},
            ],
            "meta": {"version": 5},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": [
                {"pattern": name, "netclass": "CORE_SIGNAL"}
                for name in ("SIGNAL", "OTHER")
            ],
        }
        source.with_suffix(".kicad_pro").write_text(
            json.dumps({"net_settings": settings}), encoding="utf-8"
        )
        return source

    def requests(self):
        base = net_fixtures.NativeNetRoutingTests.config()
        left = {
            **base,
            "shared": {**base["shared"], "bounds_mm": [0, 0, 11, 8]},
            "nets": [{"name": "SIGNAL"}],
        }
        right = {
            **base,
            "shared": {**base["shared"], "bounds_mm": [17, 0, 30, 8]},
            "nets": [{"name": "OTHER"}],
        }
        return [left, right]

    def test_disjoint_successes_can_be_combined_in_one_native_validation(self):
        from pcb.net_routing import runner

        requests = self.requests()
        searches = [
            {"status": "geometric_candidate_only"},
            {"status": "geometric_candidate_only"},
        ]
        combined = {
            "status": "validated_preview",
            "new_style_issues": (),
            "validation": {
                "status": "no_new_drc_violations",
                "schematic_parity_checked": True,
                "candidate_unconnected": 0,
                "reference_comparison": {},
            },
            "attempts": searches,
        }
        with (
            mock.patch.object(
                runner.NativePreview, "search_alternatives", return_value=searches
            ),
            mock.patch.object(
                runner.NativePreview, "preview_round", return_value=combined
            ) as validate,
            mock.patch.object(
                runner, "run_native", side_effect=AssertionError("unexpected fallback")
            ),
        ):
            decision = runner.NativePreview.preview_ordinary(
                Path("."), requests, 2, (), 2
            )
        self.assertTrue(decision["combined"])
        self.assertIs(decision["result"], combined)
        self.assertEqual(validate.call_count, 1)
        self.assertEqual(validate.call_args.kwargs["precomputed"], searches)

    def test_combined_validation_failure_falls_back_to_individual_previews(self):
        from pcb.net_routing import runner

        searches = [
            {"status": "geometric_candidate_only"},
            {"status": "geometric_candidate_only"},
        ]
        individual = [
            {
                "status": "validated_preview",
                "new_style_issues": (),
                "attempts": [],
                "validation": {"candidate_unconnected": 1, "reference_comparison": {}},
            },
            {"status": "rejected", "attempts": [], "validation": {}},
        ]
        with (
            mock.patch.object(
                runner.NativePreview, "search_alternatives", return_value=searches
            ),
            mock.patch.object(
                runner.NativePreview,
                "preview_round",
                return_value={"status": "rejected"},
            ),
            mock.patch.object(runner, "run_native", side_effect=individual),
        ):
            decision = runner.NativePreview.preview_ordinary(
                Path("."), self.requests(), 2, (), 2
            )
        self.assertFalse(decision["combined"])
        self.assertEqual(decision["selected_index"], 0)
        self.assertEqual(decision["result"]["validation"]["candidate_unconnected"], 1)

    def test_overlapping_ordinary_searches_remain_separate_alternatives(self):
        from pcb.net_routing import runner

        requests = self.requests()
        requests[1]["shared"]["bounds_mm"] = [10, 0, 30, 8]
        searches = [
            {"status": "geometric_candidate_only"},
            {"status": "geometric_candidate_only"},
        ]
        individual = [
            {
                "status": "validated_preview",
                "new_style_issues": (),
                "attempts": [],
                "validation": {"candidate_unconnected": 1, "reference_comparison": {}},
            },
            {"status": "no_candidate_found", "attempts": []},
        ]
        with (
            mock.patch.object(
                runner.NativePreview, "search_alternatives", return_value=searches
            ),
            mock.patch.object(runner.NativePreview, "preview_round") as combined,
            mock.patch.object(runner, "run_native", side_effect=individual),
        ):
            decision = runner.NativePreview.preview_ordinary(
                Path("."), requests, 2, (), 2
            )
        combined.assert_not_called()
        self.assertFalse(decision["combined"])
        self.assertEqual(decision["selected_index"], 0)

    def test_combined_preview_must_improve_and_have_no_new_style_issues(self):
        from pcb.net_routing import runner

        searches = [
            {"status": "geometric_candidate_only"},
            {"status": "geometric_candidate_only"},
        ]
        fallback = [
            {
                "status": "validated_preview",
                "new_style_issues": (),
                "attempts": [],
                "validation": {"candidate_unconnected": 1, "reference_comparison": {}},
            },
            {"status": "no_candidate_found", "attempts": []},
        ]
        cases = (
            {
                "status": "validated_preview",
                "new_style_issues": ("staircases",),
                "validation": {
                    "status": "no_new_drc_violations",
                    "schematic_parity_checked": True,
                    "candidate_unconnected": 0,
                    "reference_comparison": {},
                },
            },
            {
                "status": "validated_preview",
                "new_style_issues": (),
                "validation": {
                    "status": "no_new_drc_violations",
                    "schematic_parity_checked": True,
                    "candidate_unconnected": 2,
                    "reference_comparison": {},
                },
            },
        )
        for candidate in cases:
            with (
                self.subTest(candidate=candidate),
                mock.patch.object(
                    runner.NativePreview, "search_alternatives", return_value=searches
                ),
                mock.patch.object(
                    runner.NativePreview, "preview_round", return_value=candidate
                ),
                mock.patch.object(runner, "run_native", side_effect=fallback),
            ):
                decision = runner.NativePreview.preview_ordinary(
                    Path("."), self.requests(), 2, (), 2
                )
            self.assertFalse(decision["combined"])
            self.assertEqual(
                decision["result"]["validation"]["candidate_unconnected"], 1
            )

    def test_combined_preview_requires_complete_native_and_style_evidence(self):
        from pcb.net_routing import runner

        searches = [
            {"status": "geometric_candidate_only"},
            {"status": "geometric_candidate_only"},
        ]
        fallback = [
            {
                "status": "validated_preview",
                "new_style_issues": (),
                "attempts": [],
                "validation": {"candidate_unconnected": 1, "reference_comparison": {}},
            },
            {"status": "no_candidate_found", "attempts": []},
        ]
        valid = {
            "status": "validated_preview",
            "new_style_issues": (),
            "validation": {
                "status": "no_new_drc_violations",
                "schematic_parity_checked": True,
                "candidate_unconnected": 0,
                "reference_comparison": {},
            },
        }
        cases = (
            {key: value for key, value in valid.items() if key != "new_style_issues"},
            {
                **valid,
                "validation": {
                    key: value
                    for key, value in valid["validation"].items()
                    if key != "schematic_parity_checked"
                },
            },
            {**valid, "validation": {**valid["validation"], "status": "rejected"}},
        )
        for candidate in cases:
            with (
                self.subTest(candidate=candidate),
                mock.patch.object(
                    runner.NativePreview, "search_alternatives", return_value=searches
                ),
                mock.patch.object(
                    runner.NativePreview, "preview_round", return_value=candidate
                ),
                mock.patch.object(runner, "run_native", side_effect=fallback),
            ):
                decision = runner.NativePreview.preview_ordinary(
                    Path("."), self.requests(), 2, (), 2
                )
            self.assertFalse(decision["combined"])

    def test_real_disjoint_ordinary_searches_can_improve_together(self):
        from pcb.net_routing import runner

        with tempfile.TemporaryDirectory(
            prefix="autopcb-combined-ordinary-"
        ) as directory:
            project = Path(directory)
            source = self.project(project)
            before = source.read_bytes()
            decision = runner.NativePreview.preview_ordinary(
                project, self.requests(), 2, (), 2
            )
            self.assertTrue(decision["combined"], decision)
            self.assertEqual(
                decision["result"]["validation"]["candidate_unconnected"], 0
            )
            self.assertEqual(source.read_bytes(), before)

    def test_independent_candidates_receive_one_combined_native_validation(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = self.project(project)
            before = source.read_bytes()
            with mock.patch.object(
                api, "validate_routes", wraps=api.validate_routes
            ) as validate:
                result = api.preview_round(project, self.requests(), workers=2)
            self.assertEqual(result["status"], "validated_preview", result)
            self.assertEqual(validate.call_count, 1)
            self.assertEqual(result["validation"]["candidate_unconnected"], 0)
            self.assertEqual(result["electrical_status"], "not_assessed")
            self.assertFalse(result["publication_enabled"])
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(len(result["attempts"]), 2)
            for attempt in result["attempts"]:
                self.assertGreater(attempt["search"]["path_calls"], 0)
                self.assertGreater(attempt["search"]["elapsed_seconds"], 0)
                self.assertNotIn("unvalidated_campaign_changes", attempt)

    def test_overlapping_parallel_windows_are_rejected_before_search(self):
        api = self.api()
        requests = self.requests()
        requests[1]["shared"]["bounds_mm"] = [10, 0, 30, 8]
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaisesRegex(ValueError, "overlap"),
        ):
            api.preview_round(Path(tmp), requests, workers=2)

    def test_overlapping_repair_alternatives_use_isolated_native_snapshots(self):
        from pcb.net_routing import runner

        parallel = getattr(runner.NativePreview, "preview_alternatives", None)
        self.assertTrue(
            callable(parallel), "Alternative previews need isolated workers"
        )
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = self.project(project)
            before = source.read_bytes()
            good = self.requests()[0]
            limited = {**good, "shared": {**good["shared"], "max_expansions": 1}}
            results = runner.run_native(parallel, project, [limited, good], 2, ())
            self.assertEqual(len(results), 2)
            self.assertEqual(results[1]["status"], "validated_preview", results[1])
            self.assertIs(
                runner.PreviewJudge.select_alternative(results, 2), results[1]
            )
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse((project / "candidate.kicad_pcb").exists())

    def test_alternative_worker_error_is_not_hidden_by_other_results(self):
        from pcb.net_routing import runner

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = self.project(project)
            before = source.read_bytes()
            good = self.requests()[0]
            bad = {**good, "nets": [{"name": "NOT_ON_BOARD"}]}
            with self.assertRaisesRegex(
                ValueError, "Unknown|not present|not on|no pads"
            ):
                runner.NativePreview.preview_alternatives(project, [good, bad], 2, ())
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse((project / "candidate.kicad_pcb").exists())

    def test_precomputed_search_is_bound_to_its_board_and_request(self):
        import inspect

        api = self.api()
        self.assertIn("precomputed", inspect.signature(api.preview_round).parameters)
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = self.project(project)
            before = source.read_bytes()
            request = self.requests()[0]
            search = api._search((str(project), request))
            self.assertEqual(search["status"], "geometric_candidate_only")
            stale = {**search, "source_sha256": "wrong"}
            with self.assertRaisesRegex(RuntimeError, "source|snapshot"):
                api.preview_round(project, [request], workers=1, precomputed=[stale])
            changed_request = {
                **request,
                "shared": {**request["shared"], "step_mm": 0.25},
            }
            with self.assertRaisesRegex(RuntimeError, "request"):
                api.preview_round(
                    project, [changed_request], workers=1, precomputed=[search]
                )
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
