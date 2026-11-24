"""Source-bound checkpoint behavior on disposable synthetic KiCad boards."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pcbnew

from pcb import checkpoints
from verification import test_net_routing as net_tests
from verification import test_route_optimizer as route_tests


class GroupingTests(unittest.TestCase):
    def test_run_command_forwards_a_neutral_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            config = project / "config" / "autopcb.json"
            config.parent.mkdir()
            config.write_text("{}")
            seed = project / "config" / "seed.json"
            seed.write_text("{}")
            with mock.patch(
                "pcb.net_routing.sequence.preview_sequence",
                return_value={"status": "preview_paused", "applied": False},
            ) as preview:
                with contextlib.redirect_stdout(io.StringIO()):
                    status = checkpoints.main(
                        [
                            "--project",
                            str(project),
                            "run",
                            "--sequential",
                            "--seed",
                            str(seed),
                        ]
                    )
            self.assertEqual(status, 0)
            self.assertEqual(preview.call_args.kwargs["seed"], seed.resolve())

    def test_progress_record_does_not_overwrite_external_edits(self):
        from pcb.net_routing.sequence import _write_report

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sequence_preview.json"
            previous = _write_report(path, {"status": "running"}, None)
            path.write_text("owner edit")
            with self.assertRaisesRegex(RuntimeError, "changed externally"):
                _write_report(path, {"status": "complete"}, previous)
            self.assertEqual(path.read_text(), "owner edit")
            self.assertEqual(list(path.parent.glob(".sequence-*.tmp")), [])

    def test_run_command_can_select_sequential_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            config = project / "config" / "autopcb.json"
            config.parent.mkdir()
            config.write_text("{}")
            output = io.StringIO()
            with mock.patch(
                "pcb.net_routing.sequence.preview_sequence",
                return_value={"status": "preview_paused", "applied": False},
            ) as preview:
                with (
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    try:
                        status = checkpoints.main(
                            [
                                "--project",
                                str(project),
                                "run",
                                "--sequential",
                                "--limit",
                                "2",
                            ]
                        )
                    except SystemExit as exc:
                        self.fail(f"Sequential command is unavailable: {exc}")
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "preview_paused")
            self.assertEqual(preview.call_args.kwargs["max_attempts"], 2)
            self.assertEqual(
                preview.call_args.args, (project.resolve(), config.resolve())
            )

    def test_run_command_forwards_a_saved_preview_for_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            config = project / "config" / "autopcb.json"
            config.parent.mkdir()
            config.write_text("{}")
            report = project / "results" / "sequence_preview.json"
            report.parent.mkdir()
            report.write_text("{}")
            with mock.patch(
                "pcb.net_routing.sequence.preview_sequence",
                return_value={"status": "preview_paused", "applied": False},
            ) as preview:
                with (
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    try:
                        status = checkpoints.main(
                            [
                                "--project",
                                str(project),
                                "run",
                                "--sequential",
                                "--resume",
                                str(report),
                            ]
                        )
                    except SystemExit as exc:
                        self.fail(f"Sequential resume is unavailable: {exc}")
            self.assertEqual(status, 0)
            self.assertEqual(preview.call_args.kwargs["resume"], report.resolve())

    def test_local_search_window_fits_cell_budget_without_changing_grid_or_endpoints(
        self,
    ):
        fit = getattr(checkpoints, "fit_local_bounds", None)
        self.assertIsNotNone(fit, "Search windows must respect the cell budget")
        terminals = [
            {"position_mm": [24.26, 99.88]},
            {"position_mm": [26.675, 45.2]},
        ]
        route = {
            "step_mm": 0.1,
            "width_mm": 0.25,
            "clearance_mm": 0.2,
            "via": {"size_mm": 0.6, "drill_mm": 0.3},
            "layers": ["B.Cu", "In2.Cu", "F.Cu"],
            "max_cells": 250_000,
        }
        bounds, info = fit([0, 0, 100, 120], terminals, None, 8.0, route)
        nx = int((bounds[2] - bounds[0]) / route["step_mm"]) + 1
        ny = int((bounds[3] - bounds[1]) / route["step_mm"]) + 1
        self.assertLessEqual(nx * ny * len(route["layers"]), route["max_cells"])
        self.assertTrue(info["reduced"])
        self.assertLess(info["margin_mm"], 8.0)
        self.assertGreater(info["margin_mm"], 0.5)
        for terminal in terminals:
            x, y = terminal["position_mm"]
            self.assertLess(bounds[0], x)
            self.assertLess(x, bounds[2])
            self.assertLess(bounds[1], y)
            self.assertLess(y, bounds[3])
        with self.assertRaisesRegex(ValueError, "max_cells"):
            fit([0, 0, 100, 120], terminals, None, 8.0, {**route, "max_cells": 100})

    def test_preview_batch_continues_after_an_unresolved_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            config = project / "config" / "autopcb.json"
            config.parent.mkdir()
            config.write_text("{}")
            destination = project / "results" / "autopcb" / "new" / "plan.json"
            folder = destination.parent / "checkpoints"
            folder.mkdir(parents=True)
            for number in (1, 2):
                (folder / f"{number:04d}.json").write_text("{}")
            items = [{"number": 1, "net": "A"}, {"number": 2, "net": "B"}]
            trials = [
                {
                    "status": "no_validated_candidate_found",
                    "search": {"limits_hit": ["seconds"]},
                },
                {"status": "already_connected"},
            ]
            output = io.StringIO()
            with mock.patch.object(
                checkpoints, "create_plan", return_value=({"skipped": {}}, items)
            ):
                with mock.patch.object(
                    checkpoints, "save_plan", return_value=destination
                ):
                    with mock.patch.object(checkpoints, "attempt", side_effect=trials):
                        with (
                            contextlib.redirect_stdout(output),
                            contextlib.redirect_stderr(io.StringIO()),
                        ):
                            status = checkpoints.main(
                                ["--project", str(project), "run"]
                            )
            report = json.loads(output.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(report["attempted"], 2)
            self.assertEqual(
                [item["status"] for item in report["checkpoints"]],
                ["no_validated_candidate_found", "already_connected"],
            )
            self.assertFalse(report["applied"])

    def test_cli_reports_interruption_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            checkpoint = project / "checkpoint.json"
            checkpoint.write_text("{}", encoding="utf-8")
            messages = io.StringIO()
            with mock.patch.object(
                checkpoints, "attempt", side_effect=KeyboardInterrupt
            ):
                with contextlib.redirect_stderr(messages):
                    try:
                        status = checkpoints.main(
                            ["--project", str(project), "attempt", str(checkpoint)]
                        )
                    except KeyboardInterrupt:
                        self.fail("Checkpoint command leaked KeyboardInterrupt")
            self.assertEqual(status, 130)
            self.assertIn("stopped", messages.getvalue().lower())
            self.assertNotIn("Traceback", messages.getvalue())

    def test_cli_resolves_relative_checkpoints_from_project(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            checkpoint = project / "results" / "checkpoints" / "0001.json"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_text("{}", encoding="utf-8")
            with mock.patch.object(
                checkpoints, "attempt", return_value={"status": "already_connected"}
            ) as run:
                with mock.patch.object(checkpoints, "record_attempt"):
                    with contextlib.redirect_stdout(io.StringIO()):
                        status = checkpoints.main(
                            [
                                "--project",
                                str(project),
                                "attempt",
                                "results/checkpoints/0001.json",
                            ]
                        )
            self.assertEqual(status, 0)
            args, kwargs = run.call_args
            self.assertEqual(args, (project.resolve(), checkpoint.resolve()))
            self.assertFalse(kwargs["save_campaign"])
            self.assertTrue(callable(kwargs["progress"]))

    def test_local_bounds_include_pins_and_copper_anchor_and_clip_to_board(self):
        pins = [{"position_mm": [2, 3]}, {"position_mm": [7, 4]}]
        anchor = {"position_mm": [9, 8]}
        self.assertEqual(
            checkpoints.local_bounds([0, 0, 10, 10], pins, anchor, 2), [0, 1, 10, 10]
        )
        self.assertEqual(
            checkpoints.local_bounds([0, 0, 10, 10], pins, None, None), [0, 0, 10, 10]
        )
        with self.assertRaisesRegex(ValueError, "outside"):
            checkpoints.local_bounds(
                [0, 0, 10, 10], [{"position_mm": [11, 0]}], None, 2
            )

    def test_groups_are_bounded_and_deterministic(self):
        islands = [[{"uuid": str(i), "position_mm": [i * 2, i % 2]}] for i in range(8)]
        first = checkpoints.group_islands(islands, 3)
        self.assertEqual(first, checkpoints.group_islands(islands, 3))
        self.assertEqual(len(first), 4)
        for item in first:
            ids = [terminal["uuid"] for terminal in item["terminals"]]
            self.assertGreaterEqual(len(ids), 2)
            self.assertLessEqual(len(ids), 3)
            self.assertEqual(len(ids), len(set(ids)))

    def test_bad_limit_and_nonfinite_config_are_rejected(self):
        with self.assertRaises(ValueError):
            checkpoints.group_islands([[{"uuid": "a", "position_mm": [0, 0]}]], 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"schema": 1, "max_pins": 3, "route": {"width_mm": NaN}}')
            with self.assertRaisesRegex(ValueError, "Non-finite"):
                checkpoints.read_config(path)


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autopcb-checkpoint-test-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.board = route_tests.RouteOptimizerTests.make_board(self.project)
        self.before = self.board.read_bytes()
        data = net_tests.NativeNetRoutingTests.config()
        self.config = self.project / "config" / "autopcb.json"
        self.config.parent.mkdir()
        self.config.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "max_pins": 3,
                    "include_nets": ["SIGNAL"],
                    "route": data["shared"],
                    "search": data["search"],
                }
            )
        )

    def make_checkpoint(self):
        plan, items = checkpoints.create_plan(self.project, self.config)
        self.assertEqual(len(items), 1, plan)
        saved = checkpoints.save_plan(self.project, plan, items)
        self.assertEqual(self.board.read_bytes(), self.before)
        self.assertTrue(saved.is_file())
        return saved.parent / "checkpoints" / "0001.json"

    def test_plan_records_baseline_open_connections_from_kicad(self):
        plan, _ = checkpoints.create_plan(self.project, self.config)
        self.assertEqual(plan.get("baseline_unconnected"), 1)
        self.assertIsInstance(plan.get("baseline_violations"), int)
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_plan_and_attempt_validate_without_mutating_board(self):
        path = self.make_checkpoint()
        result = checkpoints.attempt(self.project, path)
        self.assertEqual(result["status"], "validated_candidate", result)
        self.assertFalse(result["applied"])
        self.assertEqual(result["electrical"]["status"], "not_assessed")
        self.assertLess(
            result["validation"]["candidate_unconnected"],
            result["validation"]["baseline_unconnected"],
        )
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_checkpoint_can_escape_from_an_existing_trace_on_the_selected_island(self):
        from pcb.route_optimizer import Obstacle, Point, mm

        board = pcbnew.LoadBoard(str(self.board))
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I(mm(2), mm(2)))
        track.SetEnd(pcbnew.VECTOR2I(mm(4), mm(2)))
        track.SetWidth(mm(0.2))
        track.SetLayer(pcbnew.F_Cu)
        track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
        board.Add(track)
        pcbnew.SaveBoard(str(self.board), board)
        self.before = self.board.read_bytes()
        path = self.make_checkpoint()
        original_obstacles = checkpoints.obstacles

        def restrict_pad(*args, **kwargs):
            native = original_obstacles(*args, **kwargs)
            keepout = Obstacle(
                Point.from_mm([1, 1]), Point.from_mm([2.8, 3]), ("F.Cu",), "", "keepout"
            )
            return replace(native, obstacles=(*native.obstacles, keepout))

        with mock.patch.object(checkpoints, "obstacles", side_effect=restrict_pad):
            result = checkpoints.attempt(self.project, path)
        self.assertEqual(result["status"], "validated_candidate", result)
        self.assertEqual(result["validation"]["status"], "no_new_drc_violations")
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_selected_through_hole_pin_can_use_every_configured_copper_layer(self):
        from pcb.net_routing.model import Site
        from pcb.route_optimizer import Point, mm

        board = pcbnew.LoadBoard(str(self.board))
        pad = next(iter(board.FindFootprintByReference("J1").Pads()))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetDrillSize(pcbnew.VECTOR2I(mm(0.3), mm(0.3)))
        pad.SetLayerSet(pcbnew.LSET.AllCuMask())
        pcbnew.SaveBoard(str(self.board), board)
        self.before = self.board.read_bytes()
        config = json.loads(self.config.read_text())
        config["route"]["layers"] = ["B.Cu", "F.Cu"]
        self.config.write_text(json.dumps(config))
        path = self.make_checkpoint()
        selected = json.loads(path.read_text())["terminals"]
        terminal = next(item for item in selected if item["reference"] == "J1")
        self.assertEqual(terminal["layer"], "B.Cu")
        captured = []

        def capture(net, _prior, _backend, _budget):
            captured.append(net)
            return ()

        with mock.patch.object(checkpoints, "whole_net", side_effect=capture):
            checkpoints.attempt(self.project, path)
        self.assertEqual(len(captured), 1)
        island = next(i for i in captured[0].islands if terminal["uuid"] in i.pad_ids)
        point = Point.from_mm(terminal["position_mm"])
        self.assertIn(Site(point, "B.Cu"), island.sites)
        self.assertIn(Site(point, "F.Cu"), island.sites)
        result = checkpoints.attempt(self.project, path)
        self.assertEqual(result["status"], "validated_candidate", result)
        self.assertEqual(result["route"]["via_count"], 0)
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_existing_island_access_does_not_add_unselected_pins(self):
        from pcb.net_routing.model import Site
        from pcb.route_optimizer import Point, mm

        board = pcbnew.LoadBoard(str(self.board))
        net_tests.NativeNetRoutingTests.add_pad(board, "J3", (4, 2))
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I(mm(2), mm(2)))
        track.SetEnd(pcbnew.VECTOR2I(mm(4), mm(2)))
        track.SetWidth(mm(0.2))
        track.SetLayer(pcbnew.F_Cu)
        track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
        board.Add(track)
        pcbnew.SaveBoard(str(self.board), board)
        self.before = self.board.read_bytes()
        path = self.make_checkpoint()
        selected = json.loads(path.read_text())["terminals"]
        selected_sites = {
            Site(Point.from_mm(item["position_mm"]), item["layer"]) for item in selected
        }
        captured = []

        def capture(net, _prior, _backend, _budget):
            captured.append(net)
            return ()

        with mock.patch.object(checkpoints, "whole_net", side_effect=capture):
            checkpoints.attempt(self.project, path)
        self.assertEqual(len(captured), 1)
        for island in captured[0].islands:
            self.assertLessEqual(
                set(island.sites) & captured[0].pad_sites, selected_sites
            )
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_attempt_can_return_source_bound_preview_changes(self):
        from core.kicad_campaign import CampaignPlanner

        path = self.make_checkpoint()
        try:
            result = checkpoints.attempt(
                self.project, path, include_preview_changes=True
            )
        except TypeError as exc:
            self.fail(f"Sequential preview changes are unavailable: {exc}")
        self.assertEqual(result["status"], "validated_candidate", result)
        self.assertFalse(result["applied"])
        self.assertEqual(result["electrical"]["status"], "not_assessed")
        candidate = CampaignPlanner(self.project).plan(
            "pcb", result["_preview_changes"]
        )
        self.assertEqual(
            candidate.result_sha256, result["validation"]["candidate_sha256"]
        )
        self.assertTrue(result.get("_preview_replay", {}).get("contracts"))
        self.assertTrue(result["_preview_replay"]["changes"])
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_attempt_falls_back_when_refined_path_fails_native_validation(self):
        path = self.make_checkpoint()
        validate = checkpoints.validate_routes
        calls = []

        def reject_first(*args, **kwargs):
            evidence = validate(*args, **kwargs)
            calls.append(evidence)
            if len(calls) == 1:
                evidence["status"] = "new_drc_violations"
            return evidence

        def refine(_backend, _net, _prior, original):
            self.assertGreater(len(original.segments), 1)
            return replace(
                original,
                segments=tuple(reversed(original.segments)),
                cost=original.cost - 1,
            )

        with (
            mock.patch.object(checkpoints.NativeBackend, "refine_complete", refine),
            mock.patch.object(checkpoints, "validate_routes", side_effect=reject_first),
        ):
            result = checkpoints.attempt(self.project, path)
        self.assertEqual(result["status"], "validated_candidate", result)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(result["validation"]["status"], "no_new_drc_violations")
        self.assertEqual(self.board.read_bytes(), self.before)

    def add_second_signal(self):
        board = pcbnew.LoadBoard(str(self.board))
        board.Add(pcbnew.NETINFO_ITEM(board, "SIGNAL2"))
        for ref, position in (("J3", (2, 6.5)), ("J4", (8, 6.5))):
            net_tests.NativeNetRoutingTests.add_pad(
                board, ref, position, net_name="SIGNAL2"
            )
        pcbnew.SaveBoard(str(self.board), board)
        self.before = self.board.read_bytes()
        data = json.loads(self.config.read_text())
        data["include_nets"] = ["SIGNAL", "SIGNAL2"]
        data["priorities"] = {"SIGNAL": 0, "SIGNAL2": 1}
        self.config.write_text(json.dumps(data))

    def make_neutral_seed(self):
        from pcb.route_cli import campaign_changes
        from pcb.route_optimizer import Point, Route, Segment, mm

        board = pcbnew.LoadBoard(str(self.board))
        board.Add(pcbnew.NETINFO_ITEM(board, "EXISTING"))
        for ref, pos in (("J3", (2, 7)), ("J4", (8, 7))):
            net_tests.NativeNetRoutingTests.add_pad(
                board, ref, pos, net_name="EXISTING"
            )
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I(mm(2), mm(7)))
        track.SetEnd(pcbnew.VECTOR2I(mm(8), mm(7)))
        track.SetWidth(mm(0.2))
        track.SetLayer(pcbnew.F_Cu)
        track.SetNetCode(board.FindNet("EXISTING").GetNetCode())
        board.Add(track)
        removed = {"kind": "segment", "uuid": track.m_Uuid.AsString()}
        pcbnew.SaveBoard(str(self.board), board)
        self.before = self.board.read_bytes()
        # Same geometry under new copper: open count unchanged, style clean.
        candidate = Route(
            (Segment(Point.from_mm([2, 7]), Point.from_mm([8, 7]), "F.Cu"),),
            (),
            (),
            6,
            0,
            0,
            0,
        )
        seed = {
            "schema": 1,
            "source_sha256": hashlib.sha256(self.before).hexdigest(),
            "changes": campaign_changes(
                candidate, "EXISTING", mm(0.2), mm(0.6), mm(0.3), [removed]
            ),
            "contracts": [
                {
                    "net": "EXISTING",
                    "start": [2, 7],
                    "start_layer": "F.Cu",
                    "end": [8, 7],
                    "end_layer": "F.Cu",
                    "removals": [removed],
                }
            ],
        }
        path = self.project / "config" / "seed.json"
        path.write_text(json.dumps(seed))
        return path

    def test_neutral_seed_is_validated_and_replayed_on_resume(self):
        from pcb.net_routing.sequence import preview_sequence

        seed = self.make_neutral_seed()
        first = preview_sequence(self.project, self.config, max_attempts=1, seed=seed)
        self.assertFalse(first["applied"])
        self.assertEqual(first["seed"]["before"], 1)
        self.assertEqual(first["seed"]["after"], 1)
        self.assertEqual(first["seed"]["electrical"], "not_assessed")
        self.assertEqual(first["final_unconnected"], 0)
        resumed = preview_sequence(
            self.project, self.config, resume=Path(first["report"])
        )
        self.assertEqual(resumed["final_unconnected"], 0)
        self.assertEqual(resumed["seed"]["after"], 1)
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_neutral_seed_rejects_a_footprint_move(self):
        from pcb.net_routing.sequence import preview_sequence

        seed_path = self.make_neutral_seed()
        seed = json.loads(seed_path.read_text())
        seed["changes"][0] = {
            "document": "pcb",
            "target": {"kind": "footprint", "reference": "J1"},
            "op": "set_at",
            "x": 7,
            "y": 7,
        }
        seed_path.write_text(json.dumps(seed))
        with self.assertRaisesRegex(ValueError, "explicitly permitted"):
            preview_sequence(self.project, self.config, seed=seed_path)
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_neutral_seed_rejects_non_octilinear_tracks(self):
        from pcb.net_routing.sequence import preview_sequence
        from pcb.route_cli import campaign_changes
        from pcb.route_optimizer import Point, Route, Segment, mm

        seed_path = self.make_neutral_seed()
        seed = json.loads(seed_path.read_text())
        removed = seed["contracts"][0]["removals"]
        a, middle, b = (Point.from_mm(value) for value in ([2, 7], [5, 6.5], [8, 7]))
        candidate = Route(
            (Segment(a, middle, "F.Cu"), Segment(middle, b, "F.Cu")), (), (), 0, 0, 0, 0
        )
        seed["changes"] = campaign_changes(
            candidate, "EXISTING", mm(0.2), mm(0.6), mm(0.3), removed
        )
        seed_path.write_text(json.dumps(seed))
        with self.assertRaisesRegex(ValueError, "45-degree"):
            preview_sequence(self.project, self.config, seed=seed_path)
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_neutral_seed_rejects_a_different_source_board(self):
        from pcb.net_routing.sequence import preview_sequence

        seed_path = self.make_neutral_seed()
        data = json.loads(seed_path.read_text())
        data["source_sha256"] = "0" * 64
        seed_path.write_text(json.dumps(data))
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            preview_sequence(self.project, self.config, seed=seed_path)
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_neutral_seed_rejects_a_right_angle_joint(self):
        from pcb.net_routing.sequence import preview_sequence
        from pcb.route_cli import campaign_changes
        from pcb.route_optimizer import Point, Route, Segment, mm

        seed_path = self.make_neutral_seed()
        seed = json.loads(seed_path.read_text())
        removed = seed["contracts"][0]["removals"]
        points = [Point.from_mm(v) for v in ([2, 7], [4, 7], [4, 6], [7, 6], [8, 7])]
        candidate = Route(
            tuple(Segment(a, b, "F.Cu") for a, b in zip(points, points[1:])),
            (),
            (),
            0,
            0,
            0,
            0,
        )
        seed["changes"] = campaign_changes(
            candidate, "EXISTING", mm(0.2), mm(0.6), mm(0.3), removed
        )
        seed_path.write_text(json.dumps(seed))
        with self.assertRaisesRegex(ValueError, "45-degree rule"):
            preview_sequence(self.project, self.config, seed=seed_path)
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_sequential_preview_carries_routes_forward_without_editing_sources(self):
        spec = importlib.util.find_spec("pcb.net_routing.sequence")
        self.assertIsNotNone(spec, "Sequential preview routing is unavailable")
        from pcb.net_routing.sequence import preview_sequence

        self.add_second_signal()
        result = preview_sequence(self.project, self.config, max_attempts=4)
        self.assertEqual(result["status"], "preview_completed", result)
        self.assertFalse(result["applied"])
        self.assertEqual(result["initial_unconnected"], 2)
        self.assertEqual(result["final_unconnected"], 0)
        self.assertEqual(result["accepted"], 2)
        self.assertEqual(result["termination_reason"], "no_configured_checkpoints")
        self.assertEqual([s["after"] for s in result["steps"] if s["accepted"]], [1, 0])
        self.assertTrue(all(s.get("completed_at") for s in result["steps"]))
        self.assertTrue(all("limits_hit" in s["search"] for s in result["steps"]))
        self.assertTrue(
            all(s["validation"]["new_violations"] == [] for s in result["steps"])
        )
        report = Path(result["report"])
        stored = json.loads(report.read_text())
        self.assertEqual(stored["final_unconnected"], result["final_unconnected"])
        self.assertTrue(all(s.get("replay") for s in stored["steps"] if s["accepted"]))
        self.assertTrue(all("replay" not in s for s in result["steps"]))
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_paused_sequential_preview_resumes_from_verified_steps(self):
        from pcb.net_routing.sequence import preview_sequence

        self.add_second_signal()
        first = preview_sequence(self.project, self.config, max_attempts=1)
        self.assertEqual(first["status"], "preview_paused")
        self.assertEqual(first["termination_reason"], "attempt_limit")
        self.assertEqual(first["final_unconnected"], 1)
        try:
            resumed = preview_sequence(
                self.project, self.config, max_attempts=1, resume=Path(first["report"])
            )
        except TypeError as exc:
            self.fail(f"Resuming a sequential preview is unavailable: {exc}")
        self.assertEqual(resumed["status"], "preview_completed", resumed)
        self.assertEqual(resumed["initial_unconnected"], 2)
        self.assertEqual(resumed["final_unconnected"], 0)
        self.assertEqual(resumed["accepted"], 2)
        self.assertEqual(resumed["resumed_from"], first["report"])
        self.assertNotEqual(resumed["report"], first["report"])
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_resume_rejects_a_changed_configuration(self):
        from pcb.net_routing.sequence import preview_sequence

        first = preview_sequence(self.project, self.config, max_attempts=1)
        self.config.write_text(self.config.read_text() + "\n")
        with self.assertRaisesRegex(RuntimeError, "inputs or tools changed"):
            preview_sequence(self.project, self.config, resume=Path(first["report"]))
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_resume_rejects_a_tampered_footprint_move(self):
        from pcb.net_routing.sequence import preview_sequence

        first = preview_sequence(self.project, self.config, max_attempts=1)
        path = Path(first["report"])
        saved = json.loads(path.read_text())
        accepted = next(step for step in saved["steps"] if step["accepted"])
        accepted["replay"]["changes"][0] = {
            "document": "pcb",
            "target": {"kind": "footprint", "reference": "J1"},
            "op": "set_at",
            "x": 7,
            "y": 7,
        }
        path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(ValueError, "explicitly permitted"):
            preview_sequence(self.project, self.config, resume=path)
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_interrupted_sequential_preview_keeps_a_honest_progress_record(self):
        from pcb.net_routing.sequence import preview_sequence

        def stop_after_candidate(message):
            if "temporary board has" in message:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            preview_sequence(self.project, self.config, progress=stop_after_candidate)
        reports = list(
            (self.project / "results" / "autopcb").glob("*/sequence_preview.json")
        )
        self.assertEqual(len(reports), 1)
        result = json.loads(reports[0].read_text())
        self.assertEqual(result["status"], "preview_interrupted")
        self.assertEqual(result["accepted"], 1)
        self.assertFalse(result["applied"])
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_sequential_preview_rejects_a_config_change_during_capture(self):
        from pcb.net_routing.sequence import preview_sequence

        original_read = Path.read_bytes
        changed = False

        def race(path):
            nonlocal changed
            content = original_read(path)
            if path == self.config.resolve() and not changed:
                changed = True
                path.write_bytes(content + b"\n")
            return content

        with mock.patch.object(Path, "read_bytes", race):
            with self.assertRaisesRegex(RuntimeError, "configuration changed"):
                preview_sequence(self.project, self.config, max_attempts=1)
        self.assertTrue(changed)
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_attempt_reports_search_and_validation_stages(self):
        path = self.make_checkpoint()
        events = []
        try:
            result = checkpoints.attempt(self.project, path, progress=events.append)
        except TypeError as exc:
            self.fail(f"Checkpoint progress is unavailable: {exc}")
        self.assertEqual(result["status"], "validated_candidate", result)
        self.assertTrue(any("search" in event.lower() for event in events), events)
        self.assertTrue(any("validat" in event.lower() for event in events), events)
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_one_command_plans_and_previews_a_checkpoint_without_board_edits(self):
        output, messages = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(messages):
            try:
                status = checkpoints.main(
                    ["--project", str(self.project), "run", "--limit", "1"]
                )
            except SystemExit as exc:
                self.fail(f"The checkpoint workflow command is unavailable: {exc}")
        self.assertEqual(status, 0, messages.getvalue())
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "preview_completed")
        self.assertFalse(report["applied"])
        self.assertEqual(report["attempted"], 1)
        self.assertEqual(report["checkpoints"][0]["status"], "validated_candidate")
        result_path = Path(report["checkpoints"][0]["file"])
        recorded = json.loads(result_path.read_text())
        self.assertEqual(recorded["last_attempt"]["status"], "validated_candidate")
        self.assertIn("search", messages.getvalue().lower())
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_campaign_requires_electrical_limits(self):
        path = self.make_checkpoint()
        with self.assertRaisesRegex(ValueError, "electrical limits"):
            checkpoints.attempt(self.project, path, save_campaign=True)
        self.assertEqual(self.board.read_bytes(), self.before)
        self.assertFalse((self.project / "config" / "campaigns").exists())

    def test_changed_board_or_config_forces_replan(self):
        path = self.make_checkpoint()
        self.board.write_bytes(self.before + b"\n")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            checkpoints.attempt(self.project, path)
        self.board.write_bytes(self.before)
        self.config.write_text(self.config.read_text() + "\n")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            checkpoints.attempt(self.project, path)

    def test_one_pin_uses_existing_copper_as_anchor(self):
        board = pcbnew.LoadBoard(str(self.board))
        trace = pcbnew.PCB_TRACK(board)
        trace.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(2), pcbnew.FromMM(2)))
        trace.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(3), pcbnew.FromMM(2)))
        trace.SetWidth(pcbnew.FromMM(0.2))
        trace.SetLayer(pcbnew.F_Cu)
        trace.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
        board.Add(trace)
        pcbnew.SaveBoard(str(self.board), board)
        self.before = self.board.read_bytes()
        data = json.loads(self.config.read_text())
        data["max_pins"] = 1
        self.config.write_text(json.dumps(data))
        path = self.make_checkpoint()
        item = json.loads(path.read_text())
        self.assertEqual(len(item["terminals"]), 1)
        self.assertEqual(item["anchor"]["kind"], "existing_copper")
        result = checkpoints.attempt(self.project, path)
        self.assertEqual(result["status"], "validated_candidate", result)
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_checkpoint_cannot_override_the_saved_policy(self):
        path = self.make_checkpoint()
        data = json.loads(path.read_text())
        data["route"]["width_mm"] = 0.3
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "saved configuration"):
            checkpoints.attempt(self.project, path)

    def test_checked_campaign_is_saved_without_applying(self):
        data = json.loads(self.config.read_text())
        data["route"]["electrical"] = route_tests.RouteOptimizerTests.electrical()
        data["route"]["current_model"] = "whole_net_upper_bound"
        self.config.write_text(json.dumps(data))
        path = self.make_checkpoint()
        result = checkpoints.attempt(self.project, path, save_campaign=True)
        self.assertEqual(result["status"], "validated_candidate", result)
        self.assertEqual(result["electrical"]["status"], "within_supplied_limits")
        self.assertTrue(Path(result["campaign"]["file"]).is_file())
        self.assertEqual(self.board.read_bytes(), self.before)

    def test_checkpoint_edits_are_not_overwritten(self):
        path = self.make_checkpoint()
        original = path.read_bytes()
        path.write_bytes(original + b"\n")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            checkpoints.record_attempt(path, original, {"status": "example"})
        self.assertEqual(path.read_bytes(), original + b"\n")


if __name__ == "__main__":
    unittest.main()
