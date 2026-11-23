"""Board-wide planning includes every net and preserves reference constraints."""

import unittest


class AutoControllerTests(unittest.TestCase):
    def test_runtime_search_budgets_change_only_numerical_limits(self):
        from pcb.net_routing.runner import apply_search_budget
        request = {'shared': {'width_mm': .5, 'clearance_mm': .2, 'layers': ['F.Cu', 'B.Cu'],
                              'max_cells': 250000},
                   'nets': [{'name': 'POWER', 'max_cells': 100000, 'width_mm': .6}]}
        apply_search_budget([request], {'max_cells': 8000000, 'max_expansions': 1000000})
        self.assertEqual(request['shared']['max_cells'], 8000000)
        self.assertEqual(request['nets'][0]['max_cells'], 8000000)
        self.assertEqual(request['shared']['width_mm'], .5)
        self.assertEqual(request['shared']['clearance_mm'], .2)
        self.assertEqual(request['nets'][0]['width_mm'], .6)
        for limits in ({'width_mm': .1}, {'max_cells': -1}, {'max_cells': True}, {'max_cells': 9000000}):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                apply_search_budget([], limits)

    def test_runtime_grid_and_greediness_do_not_change_physical_dimensions_or_costs(self):
        from pcb.net_routing.runner import apply_search_budget
        request = {'shared': {'width_mm': .5, 'weights': {'via': 10}},
                   'nets': [{'name': 'A', 'step_mm': .1, 'width_mm': .3}]}
        apply_search_budget([request], {'step_mm': .05, 'heuristic_weight': 8.0})
        self.assertEqual(request['shared']['step_mm'], .05)
        self.assertEqual(request['nets'][0]['heuristic_weight'], 8.0)
        self.assertEqual(request['nets'][0]['step_mm'], .05)
        self.assertEqual(request['shared']['width_mm'], .5)
        self.assertEqual(request['nets'][0]['width_mm'], .3)
        self.assertEqual(request['shared']['weights'], {'via': 10})
        for settings in ({'step_mm': 0}, {'step_mm': float('nan')}, {'step_mm': True},
                         {'heuristic_weight': float('inf')}, {'heuristic_weight': .5},
                         {'heuristic_weight': 21}, {'max_cells': 1.5}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                apply_search_budget([], settings)

    def test_runtime_grid_ceiling_preserves_the_finer_recovery_grid(self):
        from pcb.net_routing.runner import apply_search_budget
        request = {'recovery_stage': 2, 'shared': {'step_mm': .1},
                   'nets': [{'name': 'A', 'step_mm': .05, 'width_mm': .5}]}
        apply_search_budget([request], {'step_mm': .1, 'max_expansions': 2_000_000})
        self.assertEqual(request['nets'][0]['step_mm'], .05)
        self.assertEqual(request['nets'][0]['width_mm'], .5)
        self.assertEqual(request['nets'][0]['max_expansions'], 2_000_000)

    def test_native_recovery_never_rips_up_unqualified_neighbors(self):
        from pathlib import Path
        from unittest.mock import patch
        from pcb.net_routing import runner

        board = object()
        with (patch.object(runner, "_board", return_value=board),
              patch.object(runner, "unqualified_working_nets", return_value={"POWER", "ENCODER"}),
              patch.object(runner, "compile_reroutes", return_value={}) as compile_reroutes):
            runner._compile_reroutes_native(Path("."), {"nets": []}, {}, ("USB",))
        self.assertEqual(set(compile_reroutes.call_args.args[3]), {"POWER", "ENCODER", "USB"})

    def test_partial_combined_preview_keeps_unfinished_batches(self):
        from pcb.net_routing import runner

        requests = [
            {"nets": [{"name": "A"}]},
            {"nets": [{"name": "B"}]},
            {"nets": [{"name": "C"}]},
        ]
        result = {"status": "validated_preview", "attempts": [
            {"status": "geometric_candidate_only"},
            {"status": "no_complete_candidate_found"},
            {"status": "already_connected"},
        ]}
        self.assertEqual(runner._unfinished_combined_batches(requests, result), [requests[1]])
        with self.assertRaisesRegex(RuntimeError, "attempt"):
            runner._unfinished_combined_batches(requests, {**result, "attempts": []})

    def test_parallel_repair_alternatives_include_distinct_kinds(self):
        from pcb.net_routing import runner

        choose = getattr(runner, "_parallel_repair_alternatives", None)
        self.assertTrue(callable(choose), "Repair alternatives should use both workers")
        copper1 = {"nets": [{"name": "A"}], "rip_up": {"A": [{"uuid": "one"}]}}
        copper2 = {"nets": [{"name": "A"}], "rip_up": {"A": [{"uuid": "two"}]}}
        placement = {"nets": [{"name": "A"}], "movable": [{"reference": "R1"}]}
        selected, pending = choose([copper1, copper2, placement], 2)
        self.assertEqual(selected, [copper1, placement])
        self.assertEqual(pending, [copper2])
        self.assertEqual(choose([copper1, placement], 1), ([copper1], [placement]))

    def test_parallel_repair_selection_prefers_the_best_validated_improvement(self):
        from pcb.net_routing import runner

        select = getattr(runner, "_select_alternative_result", None)
        self.assertTrue(callable(select), "Independent repair previews need a coordinator")
        def candidate(opens, style=()):
            return {"status": "validated_preview", "validation": {
                "candidate_unconnected": opens, "reference_comparison": {"status": "unchanged"},
            }, "new_style_issues": style, "attempts": []}
        rejected = {"status": "no_candidate_found", "attempts": []}
        worse = candidate(278)
        best = candidate(277)
        bad_style = candidate(276, ("non_octilinear",))
        self.assertIs(select([rejected, worse, bad_style, best], 279), best)

    def test_parallel_selection_does_not_hide_a_usable_neutral_alternative(self):
        from pcb.net_routing.runner import _select_alternative_result

        checked = {"status": "validated_preview", "new_style_issues": (), "validation": {
            "status": "no_new_drc_violations", "schematic_parity_checked": True,
            "candidate_unconnected": 10, "reference_comparison": {"status": "unverified"},
        }}
        bad = {**checked, "new_style_issues": ("staircases",)}
        self.assertIs(_select_alternative_result([bad, checked], 10), checked)

    def test_parallel_searches_finish_before_sequential_native_validation(self):
        from unittest.mock import patch
        from pathlib import Path
        from pcb.net_routing import runner

        search = getattr(runner, "_search_alternatives_native", None)
        self.assertTrue(callable(search), "Search and native validation need separate phases")
        calls = []
        requests = [{"nets": [{"name": "A"}]}, {"nets": [{"name": "B"}]}]
        searches = [{"status": "geometric_candidate_only", "marker": name} for name in ("A", "B")]

        def fake_search(*args):
            calls.append("search")
            return searches

        def fake_validate(function, directory, request, result, critical):
            calls.append("validate " + result["marker"])
            return {"status": "validated_preview", "attempts": []}

        with patch.object(runner, "_search_alternatives_native", side_effect=fake_search), \
             patch.object(runner, "run_native", side_effect=fake_validate):
            result = runner._preview_alternatives_native(Path("."), requests, 2, ())
        self.assertEqual(calls, ["search", "validate A", "validate B"])
        self.assertEqual(len(result), 2)

    def test_single_net_retries_can_share_workers_on_isolated_boards(self):
        from pcb.net_routing import runner

        take = getattr(runner, "_parallel_ordinary_requests", None)
        self.assertTrue(callable(take), "Independent retries should use both workers")
        primary = [{"nets": [{"name": "A"}], "recovery_stage": 1}]
        duplicate = [{"nets": [{"name": "A"}], "recovery_stage": 2}]
        repair = [{"nets": [{"name": "B"}], "recovery_stage": 4}]
        other = [{"nets": [{"name": "C"}], "recovery_stage": 0}]
        queue = [duplicate, repair, other]
        selected = take(queue, primary, {}, {}, 2)
        self.assertEqual(selected, [primary[0], other[0]])
        self.assertEqual(queue, [duplicate, repair])
        queue = [other]
        self.assertEqual(take(queue, primary, {}, {}, 1), primary)
        self.assertEqual(queue, [other])

    def test_untried_targets_get_a_turn_before_repeated_repairs(self):
        from pcb.net_routing import runner

        selector = getattr(runner, "_pop_fair_round", None)
        self.assertTrue(callable(selector), "The running queue needs a fair target selector")
        camera = [{"nets": [{"name": "CAMERA"}, {"name": "GND"}],
                   "rip_up": {"GND": [{"kind": "segment", "uuid": "old"}]}}]
        timing = [{"nets": [{"name": "TIMING"}]}]
        other = [{"nets": [{"name": "OTHER"}]}]
        retry = [{"nets": [{"name": "CAMERA"}]}]
        queue = [camera, timing, other, retry]
        attempts = {"CAMERA": 3, "TIMING": 1, "GND": 0}
        priorities = {"TIMING": 0}
        self.assertEqual(selector(queue, attempts, priorities), other)
        self.assertEqual(selector(queue, attempts, priorities), timing)
        self.assertEqual(selector(queue, attempts, priorities), camera)
        self.assertEqual(selector(queue, attempts, priorities), retry)

    def test_fair_scheduler_prefers_simpler_searches_at_equal_priority(self):
        from pcb.net_routing.runner import _pop_fair_round

        repair = [{"nets": [{"name": "REPAIR"}], "recovery_stage": 4}]
        wide = [{"nets": [{"name": "WIDE"}], "recovery_stage": 2}]
        local = [{"nets": [{"name": "LOCAL"}], "recovery_stage": 0}]
        queue = [repair, wide, local]
        self.assertEqual(_pop_fair_round(queue, {}, {}), local)
        self.assertEqual(_pop_fair_round(queue, {}, {}), wide)
        self.assertEqual(_pop_fair_round(queue, {}, {}), repair)
        priority_repair = [{"nets": [{"name": "TIMING"}], "recovery_stage": 4}]
        queue = [local, priority_repair]
        self.assertEqual(_pop_fair_round(queue, {}, {"TIMING": 0}), priority_repair)

    def test_fair_scheduler_keeps_parallel_work_and_ignores_repair_neighbors(self):
        from pcb.net_routing.runner import _pop_fair_round, _round_targets

        repair = [{"nets": [{"name": "CAMERA"}, {"name": "GND"}],
                   "rip_up": {"GND": [{"kind": "segment", "uuid": "old"}]}}]
        parallel = [{"nets": [{"name": "DRIVER"}]}, {"nets": [{"name": "OTHER"}]}]
        timing = [{"nets": [{"name": "TIMING"}]}]
        queue = [repair, parallel, timing]
        self.assertEqual(_round_targets(repair), ("CAMERA",))
        self.assertEqual(_round_targets(parallel), ("DRIVER", "OTHER"))
        self.assertEqual(_pop_fair_round(queue, {}, {"TIMING": 0, "DRIVER": 2}), timing)
        self.assertEqual(_pop_fair_round(queue, {}, {"TIMING": 0, "DRIVER": 2}), parallel)
        self.assertEqual(queue, [repair])

    def test_all_ordinary_work_is_scheduled_despite_legacy_include_list(self):
        from pcb.net_routing.controller import make_work_plan

        preparation = {
            "unqualified_nets": [],
            "work": {"items": [
                {"name": "TIMING", "priority": 0, "window": (0., 0., 10., 10.), "island_count": 2},
                {"name": "DRIVER", "priority": 2, "window": (20., 0., 30., 10.), "island_count": 2},
                {"name": "USB_DP", "priority": 3, "window": (40., 0., 50., 10.), "island_count": 2},
                {"name": "OTHER", "priority": 100, "window": (60., 0., 70., 10.), "island_count": 2},
            ]},
            "reference_copper": {"high_speed_nets": ["USB_DP"]},
        }
        policy = {
            "include_nets": ["TIMING"],
            "exclude_nets": [],
            "route": {"layers": ["B.Cu", "In2.Cu", "In1.Cu", "F.Cu"], "step_mm": .1,
                      "max_cells": 250000},
            "net_overrides": {}, "search": {"seconds": 2},
        }
        plan = make_work_plan(preparation, policy)
        names = [entry["name"] for round_ in plan["rounds"] for req in round_ for entry in req["nets"]]
        self.assertEqual(set(names), {"TIMING", "DRIVER", "OTHER"})
        self.assertEqual(names[0], "TIMING")
        self.assertEqual(plan["blocked_nets"], {
            "USB_DP": "needs a coupled route; the ordinary router preserves differential copper",
        })
        for round_ in plan["rounds"]:
            for request in round_:
                self.assertEqual(request["shared"]["layers"], ["B.Cu", "In2.Cu", "In1.Cu", "F.Cu"])
                self.assertEqual(request["max_islands"], 3)

    def test_adaptive_step_respects_each_override_cell_limit(self):
        from pcb.net_routing.controller import bounded_step

        step = bounded_step((0., 0., 100., 100.), .05, 4, 250000)
        self.assertGreater(step, .05)
        self.assertLessEqual((int(100 / step) + 1) ** 2 * 4, 250000)
        self.assertEqual(bounded_step((0., 0., 1., 1.), .1, 4, 250000), .1)

    def test_scheduled_bounds_use_exact_nanometre_coordinates(self):
        from pcb.net_routing.controller import make_work_plan
        from pcb.route_optimizer import Point

        preparation = {"unqualified_nets": [], "work": {"items": [{"name": "X", "priority": 0,
                         "window": (0.1 + 0.2, 1.0000000000000002, 10.000000000000002, 5.1),
                         "island_count": 2}]}, "reference_copper": {"high_speed_nets": []}}
        policy = {"route": {"layers": ["B.Cu"], "step_mm": .1}, "search": {}}
        request = make_work_plan(preparation, policy)["rounds"][0][0]
        left, top, right, bottom = request["shared"]["bounds_mm"]
        Point.from_mm([left, top])
        Point.from_mm([right, bottom])

    def test_new_style_violations_are_not_accepted_as_progress(self):
        from pcb.net_routing.controller import new_style_issues
        from pcb.net_routing.style import Joint, StyleReport

        old = StyleReport(("old",), (), (), (), (), (), (), ())
        changed = StyleReport(("old", "new"), (),
                              (Joint("N", "B.Cu", (1, 2), ("new",)),), (), (), (), (), ())
        self.assertEqual(set(new_style_issues(old, changed)), {"non_octilinear", "invalid_bends"})
        self.assertEqual(new_style_issues(old, old), ())

    def test_resumption_rejects_changed_inputs_or_tampered_candidate(self):
        import hashlib
        from pcb.net_routing.controller import decode_checkpoint, encode_checkpoint

        board = b"synthetic native board"
        checkpoint = encode_checkpoint(board, {"source": "first"}, {"router": "original"}, "policy")
        self.assertEqual(decode_checkpoint(checkpoint, {"source": "first"}, {"router": "original"}, "policy"), board)
        for source, tools, policy in (({"source": "edited"}, {"router": "original"}, "policy"),
                                      ({"source": "first"}, {"router": "edited"}, "policy"),
                                      ({"source": "first"}, {"router": "original"}, "changed")):
            with self.assertRaisesRegex(RuntimeError, "changed"):
                decode_checkpoint(checkpoint, source, tools, policy)
        checkpoint["candidate_sha256"] = hashlib.sha256(b"tampered").hexdigest()
        with self.assertRaisesRegex(RuntimeError, "candidate"):
            decode_checkpoint(checkpoint, {"source": "first"}, {"router": "original"}, "policy")

    def test_native_preview_run_is_resumable_and_never_edits_the_saved_board(self):
        import json
        import tempfile
        from pathlib import Path

        from pcb.net_routing.runner import run_project
        from verification.test_autoroute_batch import CombinedBatchTests
        from verification.test_net_routing import NativeNetRoutingTests

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = CombinedBatchTests().project(project)
            (project / "v4.1.kicad_sch").write_text(
                '(kicad_sch (version 20250114) (generator "eeschema") '
                '(uuid "6b6b7707-0425-4195-9c79-1824e2f1a24e") (paper "A4") '
                '(lib_symbols) (sheet_instances (path "/" (page "1"))) (embedded_fonts no))',
                encoding="utf-8")
            config = NativeNetRoutingTests.config()
            (project / "config").mkdir()
            policy = {"schema": 1, "max_pins": 3, "route": config["shared"],
                      "search": config["search"], "include_nets": ["SIGNAL"]}
            (project / "config" / "autopcb.json").write_text(json.dumps(policy), encoding="utf-8")
            before = source.read_bytes()
            initialized = run_project(project, max_rounds=0, workers=1)
            self.assertEqual(initialized["attempted_rounds"], 0)
            self.assertFalse(initialized["publication_enabled"])
            self.assertEqual(source.read_bytes(), before)
            result = run_project(project, max_rounds=1, workers=1)
            self.assertIn(result["status"], {"paused", "preview_zero_opens" if result["best_preview_opens"] == 0 else "stalled"})
            self.assertFalse(result["publication_enabled"])
            self.assertEqual(source.read_bytes(), before)
            self.assertTrue((project / "results" / "autoroute" / (result["run_id"] + ".json")).is_file())
            resumed = run_project(project, resume=result["run_id"], max_rounds=1, workers=1)
            self.assertIn(resumed["status"], {"paused", "preview_zero_opens" if resumed["best_preview_opens"] == 0 else "stalled"})
            self.assertEqual(source.read_bytes(), before)
            # A changed native source must invalidate resumption before any search.
            (project / "v4.1.kicad_sch").write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "changed"):
                run_project(project, resume=result["run_id"], max_rounds=1, workers=1)
            self.assertEqual(source.read_bytes(), before)

    def test_resume_uses_saved_target_attempts_to_avoid_retry_starvation(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from pcb.net_routing import runner
        from verification.test_autoroute_batch import CombinedBatchTests
        from verification.test_net_routing import NativeNetRoutingTests

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = CombinedBatchTests().project(project)
            (project / "v4.1.kicad_sch").write_text(
                '(kicad_sch (version 20250114) (generator "eeschema") '
                '(uuid "6b6b7707-0425-4195-9c79-1824e2f1a24e") (paper "A4") '
                '(lib_symbols) (sheet_instances (path "/" (page "1"))) (embedded_fonts no))',
                encoding="utf-8",
            )
            config = NativeNetRoutingTests.config()
            (project / "config").mkdir()
            policy = {"schema": 1, "max_pins": 3, "route": config["shared"],
                      "search": config["search"], "search_margin_mm": .1,
                      "priorities": {"SIGNAL": 0}}
            (project / "config" / "autopcb.json").write_text(json.dumps(policy), encoding="utf-8")
            before = source.read_bytes()
            original_native = runner.run_native
            searched = []

            def native(function, *args, **kwargs):
                if function is runner._preview_round_native:
                    searched.append(args[1][0]["nets"][0]["name"])
                    return {"status": "no_candidate_found", "attempts": []}
                return original_native(function, *args, **kwargs)

            with patch.object(runner, "run_native", side_effect=native):
                first = runner.run_project(project, max_rounds=1, workers=1)
                state_path = project / "results" / "autoroute" / (first["run_id"] + ".json")
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["target_attempts"], {"SIGNAL": 1})
                signal = next(batch for batch in state["queue"] if batch[0]["nets"][0]["name"] == "SIGNAL")
                other = next(batch for batch in state["queue"] if batch[0]["nets"][0]["name"] == "OTHER")
                state["queue"] = [signal, other]
                state_path.write_text(json.dumps(state), encoding="utf-8")
                runner.run_project(project, resume=first["run_id"], max_rounds=1, workers=1)
            self.assertEqual(searched, ["SIGNAL", "OTHER"])
            self.assertEqual(source.read_bytes(), before)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["target_attempts"], {"SIGNAL": 1, "OTHER": 1})

    def test_renew_preserves_attempt_fairness_on_the_same_preview(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from pcb.net_routing import runner
        from verification.test_autoroute_batch import CombinedBatchTests
        from verification.test_net_routing import NativeNetRoutingTests

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = CombinedBatchTests().project(project)
            (project / "v4.1.kicad_sch").write_text(
                '(kicad_sch (version 20250114) (generator "eeschema") '
                '(uuid "6b6b7707-0425-4195-9c79-1824e2f1a24e") (paper "A4") '
                '(lib_symbols) (sheet_instances (path "/" (page "1"))) (embedded_fonts no))',
                encoding="utf-8",
            )
            config = NativeNetRoutingTests.config()
            (project / "config").mkdir()
            policy = {"schema": 1, "max_pins": 3, "route": config["shared"],
                      "search": config["search"], "search_margin_mm": .1,
                      "priorities": {"SIGNAL": 0}}
            (project / "config" / "autopcb.json").write_text(json.dumps(policy), encoding="utf-8")
            before = source.read_bytes()
            original_native = runner.run_native
            searched = []

            def native(function, *args, **kwargs):
                if function is runner._preview_round_native:
                    searched.append(args[1][0]["nets"][0]["name"])
                    return {"status": "no_candidate_found", "attempts": []}
                return original_native(function, *args, **kwargs)

            with patch.object(runner, "run_native", side_effect=native):
                first = runner.run_project(project, max_rounds=1, workers=1)
                old_path = project / "results" / "autoroute" / (first["run_id"] + ".json")
                old_bytes = old_path.read_bytes()
                renewed = runner.run_project(project, renew=first["run_id"], max_rounds=1, workers=1)
            self.assertEqual(searched, ["SIGNAL", "OTHER"])
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(old_path.read_bytes(), old_bytes)
            state = json.loads((old_path.parent / (renewed["run_id"] + ".json")).read_text(encoding="utf-8"))
            self.assertEqual(state["target_attempts"], {"SIGNAL": 1, "OTHER": 1})

    def test_renew_retries_rejected_checkpoint_without_old_geometry(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from pcb.net_routing import runner
        from verification.test_autoroute_batch import CombinedBatchTests
        from verification.test_net_routing import NativeNetRoutingTests

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = CombinedBatchTests().project(project)
            (project / "v4.1.kicad_sch").write_text(
                '(kicad_sch (version 20250114) (generator "eeschema") '
                '(uuid "6b6b7707-0425-4195-9c79-1824e2f1a24e") (paper "A4") '
                '(lib_symbols) (sheet_instances (path "/" (page "1"))) (embedded_fonts no))',
                encoding="utf-8",
            )
            config = NativeNetRoutingTests.config()
            (project / "config").mkdir()
            policy = {"schema": 1, "max_pins": 3, "route": config["shared"],
                      "search": config["search"], "search_margin_mm": .1,
                      "priorities": {"SIGNAL": 0}}
            (project / "config" / "autopcb.json").write_text(json.dumps(policy), encoding="utf-8")
            before = source.read_bytes()
            original_native = runner.run_native
            searched = []

            def native(function, *args, **kwargs):
                if function is runner._preview_round_native:
                    searched.append(args[1])
                    return {"status": "no_candidate_found", "attempts": []}
                return original_native(function, *args, **kwargs)

            with patch.object(runner, "run_native", side_effect=native):
                first = runner.run_project(project, max_rounds=1, workers=1)
                old_path = project / "results" / "autoroute" / (first["run_id"] + ".json")
                state = json.loads(old_path.read_text(encoding="utf-8"))
                signal = next(req for group in state["queue"] for req in group
                              if req["nets"][0]["name"] == "SIGNAL")
                signal.update(recovery_stage=4, rip_up={"SIGNAL": [{"uuid": "obsolete"}]})
                state["queue"] = [[signal]]
                state["last_result"] = {"outcome": "rejected", "attempts": [
                    {"status": "geometric_candidate_only", "checkpoints": [
                        {"net": "SIGNAL", "stage": 3, "index": 0,
                         "bounds_mm": [99, 99, 100, 100]},
                    ]},
                    {"status": "native_validation_rejected"},
                ]}
                state["target_attempts"] = {"SIGNAL": 8}
                old_path.write_text(json.dumps(state), encoding="utf-8")
                old_bytes = old_path.read_bytes()
                runner.run_project(project, renew=first["run_id"], max_rounds=1, workers=1)
            self.assertEqual(searched[-1][0]["nets"][0]["name"], "SIGNAL")
            self.assertEqual(searched[-1][0]["recovery_stage"], 3)
            self.assertNotIn("obsolete", str(searched[-1]))
            self.assertNotEqual(searched[-1][0]["shared"]["bounds_mm"], [99, 99, 100, 100])
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(old_path.read_bytes(), old_bytes)

    def test_controller_dispatches_repair_alternatives_to_two_workers(self):
        import copy
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from pcb.net_routing import runner
        from verification.test_autoroute_batch import CombinedBatchTests
        from verification.test_net_routing import NativeNetRoutingTests

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = CombinedBatchTests().project(project)
            (project / "v4.1.kicad_sch").write_text(
                '(kicad_sch (version 20250114) (generator "eeschema") '
                '(uuid "6b6b7707-0425-4195-9c79-1824e2f1a24e") (paper "A4") '
                '(lib_symbols) (sheet_instances (path "/" (page "1"))) (embedded_fonts no))',
                encoding="utf-8",
            )
            config = NativeNetRoutingTests.config()
            (project / "config").mkdir()
            policy = {"schema": 1, "max_pins": 3, "route": config["shared"],
                      "search": config["search"], "search_margin_mm": .1}
            (project / "config" / "autopcb.json").write_text(json.dumps(policy), encoding="utf-8")
            before = source.read_bytes()
            original_native = runner.run_native
            dispatched = []
            progress = []

            def native(function, *args, **kwargs):
                if function is runner._preview_round_native:
                    return {"status": "no_candidate_found", "attempts": []}
                if function is runner._compile_reroutes_native:
                    marker = args[1]
                    self.assertNotIn("early_rework", marker)
                    copper = {**copy.deepcopy(marker),
                              "rip_up": {"SIGNAL": [{"kind": "segment", "uuid": "old"}]}}
                    move = {**copy.deepcopy(marker),
                            "movable": [{"reference": "R1", "positions_mm": [[2, 2]]}]}
                    return {"requests": [copper, move], "candidates": [], "blockers": []}
                if function is getattr(runner, "_preview_alternatives_native", None):
                    dispatched.extend(args[1])
                    return [{"status": "no_candidate_found", "attempts": []} for _ in args[1]]
                return original_native(function, *args, **kwargs)

            with patch.object(runner, "run_native", side_effect=native):
                first = runner.run_project(project, max_rounds=1, workers=1)
                path = project / "results" / "autoroute" / (first["run_id"] + ".json")
                state = json.loads(path.read_text(encoding="utf-8"))
                marker = next(request for batch in state["queue"] for request in batch
                              if request["nets"][0]["name"] == "SIGNAL")
                marker.update(recovery_stage=4, checkpoint_offset=0, early_rework=True)
                state["queue"] = [[marker]]
                state["attempted_searches"] = []
                state["target_attempts"] = {}
                path.write_text(json.dumps(state), encoding="utf-8")
                runner.run_project(project, resume=first["run_id"], max_rounds=1,
                                   workers=2, progress=progress.append)
            self.assertEqual(len(dispatched), 2)
            self.assertEqual({bool(req.get("movable")) for req in dispatched}, {False, True})
            self.assertTrue(any(event.get("alternative_count") == 2 for event in progress))
            self.assertEqual(source.read_bytes(), before)

    def test_controller_retains_both_failed_independent_retries(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from pcb.net_routing import runner
        from verification.test_autoroute_batch import CombinedBatchTests
        from verification.test_net_routing import NativeNetRoutingTests

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = CombinedBatchTests().project(project)
            (project / "v4.1.kicad_sch").write_text(
                '(kicad_sch (version 20250114) (generator "eeschema") '
                '(uuid "6b6b7707-0425-4195-9c79-1824e2f1a24e") (paper "A4") '
                '(lib_symbols) (sheet_instances (path "/" (page "1"))) (embedded_fonts no))',
                encoding="utf-8",
            )
            config = NativeNetRoutingTests.config()
            (project / "config").mkdir()
            policy = {"schema": 1, "max_pins": 3, "route": config["shared"],
                      "search": config["search"], "search_margin_mm": .1}
            (project / "config" / "autopcb.json").write_text(json.dumps(policy), encoding="utf-8")
            before = source.read_bytes()
            original_native = runner.run_native
            dispatched = []

            def native(function, *args, **kwargs):
                if function is runner._preview_round_native:
                    return {"status": "no_candidate_found", "attempts": []}
                if function is runner._preview_ordinary_native:
                    dispatched.extend(args[1])
                    alternatives = [{"status": "no_candidate_found", "attempts": []} for _ in args[1]]
                    return {"result": alternatives[0], "combined": False,
                            "alternatives": alternatives, "selected_index": 0}
                return original_native(function, *args, **kwargs)

            with patch.object(runner, "run_native", side_effect=native):
                first = runner.run_project(project, max_rounds=1, workers=1)
                path = project / "results" / "autoroute" / (first["run_id"] + ".json")
                state = json.loads(path.read_text(encoding="utf-8"))
                signal = next(req for batch in state["queue"] for req in batch
                              if req["nets"][0]["name"] == "SIGNAL")
                other = next(req for batch in state["queue"] for req in batch
                             if req["nets"][0]["name"] == "OTHER")
                signal.update(recovery_stage=0, checkpoint_offset=0)
                other.update(recovery_stage=0, checkpoint_offset=0)
                state.update(queue=[[signal], [other]], attempted_searches=[], target_attempts={})
                path.write_text(json.dumps(state), encoding="utf-8")
                runner.run_project(project, resume=first["run_id"], max_rounds=1, workers=2)
            self.assertEqual({req["nets"][0]["name"] for req in dispatched}, {"SIGNAL", "OTHER"})
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(state["target_attempts"]), {"SIGNAL", "OTHER"})
            self.assertEqual(
                {req["nets"][0]["name"] for batch in state["queue"] for req in batch},
                {"SIGNAL", "OTHER"},
            )
            self.assertEqual(source.read_bytes(), before)

    def test_controller_requeues_unselected_retry_after_accepting_one(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from pcb.net_routing import runner
        from verification.test_autoroute_batch import CombinedBatchTests
        from verification.test_net_routing import NativeNetRoutingTests

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = CombinedBatchTests().project(project)
            schematic = project / "v4.1.kicad_sch"
            schematic.write_text(
                '(kicad_sch (version 20250114) (generator "eeschema") '
                '(uuid "6b6b7707-0425-4195-9c79-1824e2f1a24e") (paper "A4") '
                '(lib_symbols) (sheet_instances (path "/" (page "1"))) (embedded_fonts no))',
                encoding="utf-8",
            )
            config = NativeNetRoutingTests.config()
            (project / "config").mkdir()
            policy = {"schema": 1, "max_pins": 3, "route": config["shared"],
                      "search": config["search"], "search_margin_mm": .1}
            (project / "config" / "autopcb.json").write_text(json.dumps(policy), encoding="utf-8")
            before, sch_before = source.read_bytes(), schematic.read_bytes()
            original_native = runner.run_native

            def native(function, *args, **kwargs):
                if function is runner._preview_round_native:
                    return {"status": "no_candidate_found", "attempts": []}
                return original_native(function, *args, **kwargs)

            with patch.object(runner, "run_native", side_effect=native):
                first = runner.run_project(project, max_rounds=1, workers=1)
            path = project / "results" / "autoroute" / (first["run_id"] + ".json")
            state = json.loads(path.read_text(encoding="utf-8"))
            signal = next(req for batch in state["queue"] for req in batch
                          if req["nets"][0]["name"] == "SIGNAL")
            other = next(req for batch in state["queue"] for req in batch
                         if req["nets"][0]["name"] == "OTHER")
            signal.update(recovery_stage=0, checkpoint_offset=0)
            other.update(recovery_stage=0, checkpoint_offset=0)
            # Overlapping windows exercise the individual-preview fallback.
            signal["shared"]["bounds_mm"] = [0, 0, 30, 8]
            other["shared"]["bounds_mm"] = [0, 0, 30, 8]
            attempts = {"SIGNAL": 2, "OTHER": 3, "TIMING": 8}
            state.update(queue=[[signal], [other]], attempted_searches=[], target_attempts=attempts)
            path.write_text(json.dumps(state), encoding="utf-8")
            result = runner.run_project(project, resume=first["run_id"], max_rounds=1, workers=2)
            self.assertEqual(result["best_preview_opens"], 1, result)
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(state["accepted_preview_batches"], 1)
            remaining = {req["nets"][0]["name"] for batch in state["queue"] for req in batch}
            self.assertEqual(len(remaining), 1)
            self.assertTrue(remaining <= {"SIGNAL", "OTHER"})
            self.assertEqual(state["target_attempts"], attempts)
            self.assertEqual(state["attempted_searches"], [])
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(schematic.read_bytes(), sch_before)

    def test_accepted_preview_drops_queued_work_for_the_nets_it_connected(self):
        import copy
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from pcb.net_routing import runner
        from verification.test_autoroute_batch import CombinedBatchTests
        from verification.test_net_routing import NativeNetRoutingTests

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            CombinedBatchTests().project(project)
            (project / "v4.1.kicad_sch").write_text(
                '(kicad_sch (version 20250114) (generator "eeschema") '
                '(uuid "6b6b7707-0425-4195-9c79-1824e2f1a24e") (paper "A4") '
                '(lib_symbols) (sheet_instances (path "/" (page "1"))) (embedded_fonts no))',
                encoding="utf-8",
            )
            config = NativeNetRoutingTests.config()
            (project / "config").mkdir()
            policy = {"schema": 1, "max_pins": 3, "route": config["shared"],
                      "search": config["search"], "search_margin_mm": .1}
            (project / "config" / "autopcb.json").write_text(json.dumps(policy), encoding="utf-8")
            original_native = runner.run_native

            def native(function, *args, **kwargs):
                if function is runner._preview_round_native:
                    return {"status": "no_candidate_found", "attempts": []}
                return original_native(function, *args, **kwargs)

            with patch.object(runner, "run_native", side_effect=native):
                first = runner.run_project(project, max_rounds=1, workers=1)
            path = project / "results" / "autoroute" / (first["run_id"] + ".json")
            state = json.loads(path.read_text(encoding="utf-8"))
            signal, other = (next(req for batch in state["queue"] for req in batch
                                  if req["nets"][0]["name"] == name) for name in ("SIGNAL", "OTHER"))
            signal.update(recovery_stage=0, checkpoint_offset=0)
            other.update(recovery_stage=0, checkpoint_offset=0)
            later = {**copy.deepcopy(signal), "recovery_stage": 1}
            state.update(queue=[[signal], [later], [other]], attempted_searches=[],
                         target_attempts={"OTHER": 5})
            path.write_text(json.dumps(state), encoding="utf-8")
            runner.run_project(project, resume=first["run_id"], max_rounds=1, workers=1)
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(state["accepted_preview_batches"], 1)
            self.assertEqual([batch[0]["nets"][0]["name"] for batch in state["queue"]], ["OTHER"])
            self.assertEqual(set(state["island_counts"]), {"OTHER"})

    def test_controller_accepts_two_disjoint_ordinary_retries_together(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from pcb.net_routing import runner
        from verification.test_autoroute_batch import CombinedBatchTests
        from verification.test_net_routing import NativeNetRoutingTests

        with tempfile.TemporaryDirectory(prefix="autopcb-combined-controller-") as tmp:
            project = Path(tmp)
            source = CombinedBatchTests().project(project)
            schematic = project / "v4.1.kicad_sch"
            schematic.write_text(
                '(kicad_sch (version 20250114) (generator "eeschema") '
                '(uuid "6b6b7707-0425-4195-9c79-1824e2f1a24e") (paper "A4") '
                '(lib_symbols) (sheet_instances (path "/" (page "1"))) (embedded_fonts no))',
                encoding="utf-8",
            )
            config = NativeNetRoutingTests.config()
            (project / "config").mkdir()
            policy = {"schema": 1, "max_pins": 3, "route": config["shared"],
                      "search": config["search"], "search_margin_mm": .1}
            (project / "config" / "autopcb.json").write_text(json.dumps(policy), encoding="utf-8")
            before, sch_before = source.read_bytes(), schematic.read_bytes()
            original_native = runner.run_native

            def native(function, *args, **kwargs):
                if function is runner._preview_round_native:
                    return {"status": "no_candidate_found", "attempts": []}
                return original_native(function, *args, **kwargs)

            with patch.object(runner, "run_native", side_effect=native):
                first = runner.run_project(project, max_rounds=1, workers=1)
            path = project / "results" / "autoroute" / (first["run_id"] + ".json")
            state = json.loads(path.read_text(encoding="utf-8"))
            signal = next(req for group in state["queue"] for req in group
                          if req["nets"][0]["name"] == "SIGNAL")
            other = next(req for group in state["queue"] for req in group
                         if req["nets"][0]["name"] == "OTHER")
            signal.update(recovery_stage=0, checkpoint_offset=0)
            other.update(recovery_stage=0, checkpoint_offset=0)
            signal["shared"]["bounds_mm"] = [0, 0, 11, 8]
            other["shared"]["bounds_mm"] = [17, 0, 30, 8]
            state.update(queue=[[signal], [other]], attempted_searches=[], target_attempts={})
            path.write_text(json.dumps(state), encoding="utf-8")
            result = runner.run_project(project, resume=first["run_id"], max_rounds=1, workers=2)
            self.assertEqual(result["best_preview_opens"], 0, result)
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(state["accepted_preview_batches"], 1)
            remaining = {req["nets"][0]["name"] for group in state["queue"] for req in group}
            self.assertTrue({"SIGNAL", "OTHER"}.isdisjoint(remaining),
                            (remaining, state["last_result"], state["status"]))
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(schematic.read_bytes(), sch_before)

    def test_partial_combined_preview_requeues_the_failed_batch(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from pcb.net_routing import batch, runner
        from verification.test_autoroute_batch import CombinedBatchTests
        from verification.test_net_routing import NativeNetRoutingTests

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = CombinedBatchTests().project(project)
            schematic = project / "v4.1.kicad_sch"
            schematic.write_text(
                '(kicad_sch (version 20250114) (generator "eeschema") '
                '(uuid "6b6b7707-0425-4195-9c79-1824e2f1a24e") (paper "A4") '
                '(lib_symbols) (sheet_instances (path "/" (page "1"))) (embedded_fonts no))',
                encoding="utf-8",
            )
            config = NativeNetRoutingTests.config()
            (project / "config").mkdir()
            policy = {"schema": 1, "max_pins": 3, "route": config["shared"],
                      "search": config["search"], "search_margin_mm": .1}
            (project / "config" / "autopcb.json").write_text(json.dumps(policy), encoding="utf-8")
            before, sch_before = source.read_bytes(), schematic.read_bytes()
            original_native, original_search = runner.run_native, batch._search

            def seed_native(function, *args, **kwargs):
                if function is runner._preview_round_native:
                    return {"status": "no_candidate_found", "attempts": []}
                return original_native(function, *args, **kwargs)

            with patch.object(runner, "run_native", side_effect=seed_native):
                first = runner.run_project(project, max_rounds=1, workers=1)
            path = project / "results" / "autoroute" / (first["run_id"] + ".json")
            state = json.loads(path.read_text(encoding="utf-8"))
            signal = next(req for group in state["queue"] for req in group
                          if req["nets"][0]["name"] == "SIGNAL")
            other = next(req for group in state["queue"] for req in group
                         if req["nets"][0]["name"] == "OTHER")
            signal.update(recovery_stage=0, checkpoint_offset=0)
            other.update(recovery_stage=0, checkpoint_offset=0)
            state.update(queue=[[signal, other]], attempted_searches=[], target_attempts={})
            path.write_text(json.dumps(state), encoding="utf-8")

            def partial_search(args):
                if args[1]["nets"][0]["name"] == "OTHER":
                    return {"status": "no_complete_candidate_found", "search": {}}
                return original_search(args)

            def local_preview(function, *args, **kwargs):
                if function is runner._preview_round_native:
                    return function(*args, **kwargs)
                return original_native(function, *args, **kwargs)

            with patch.object(runner, "run_native", side_effect=local_preview), \
                 patch.object(batch, "_search", side_effect=partial_search):
                result = runner.run_project(project, resume=first["run_id"], max_rounds=1, workers=1)
            self.assertEqual(result["best_preview_opens"], 1, result)
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(state["accepted_preview_batches"], 1)
            self.assertEqual(
                {req["nets"][0]["name"] for group in state["queue"] for req in group},
                {"OTHER"},
            )
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(schematic.read_bytes(), sch_before)

    def test_memory_ceiling_uses_half_available_with_six_gib_cap(self):
        from pcb.net_routing.resources import memory_ceiling
        gib = 1024 ** 3
        self.assertEqual(memory_ceiling(20 * gib), 6 * gib)
        self.assertEqual(memory_ceiling(4 * gib), 2 * gib)
        for value in (0, -1, None, True):
            with self.assertRaises(ValueError):
                memory_ceiling(value)

    def test_job_limit_rounds_down_to_an_operating_system_page(self):
        from pcb.net_routing.resources import job_memory_ceiling
        self.assertEqual(job_memory_ceiling(2 * 4097, 4096), 4096)
        self.assertEqual(job_memory_ceiling(2 * 8192, 4096), 8192)
        with self.assertRaises(ValueError):
            job_memory_ceiling(1000, 4096)

    def test_reference_repairs_follow_unfinished_connectivity_sections(self):
        from pcb.net_routing.runner import insert_reference_repairs
        def round_for(name):
            return [{"nets": [{"name": name}]}]
        plan = {"rounds": [round_for("TIMING"), round_for("DRIVER"), round_for("OTHER")], "island_counts": {}}
        repairs = {"rounds": [round_for("REPAIR")], "island_counts": {"REPAIR": 2}, "blockers": []}
        insert_reference_repairs(plan, repairs, {"priorities": {"TIMING": 0, "DRIVER": 2}})
        self.assertEqual([r[0]["nets"][0]["name"] for r in plan["rounds"]], ["TIMING", "DRIVER", "OTHER", "REPAIR"])
        self.assertEqual(plan["island_counts"]["REPAIR"], 2)

    def test_splitting_a_repair_batch_keeps_only_the_matching_removals(self):
        from pcb.net_routing.runner import _split_failed
        request = {"nets": [{"name": "A"}, {"name": "B"}],
                   "rip_up": {"A": [{"uuid": "a", "kind": "segment"}],
                              "B": [{"uuid": "b", "kind": "via"}]}}
        queue = []
        _split_failed(queue, [request], {"A": 2, "B": 2})
        self.assertEqual(set(queue[0][0]["rip_up"]), {"A"})
        self.assertEqual(set(queue[1][0]["rip_up"]), {"B"})

    def test_failed_checkpoint_tries_distinct_island_combinations_then_stops(self):
        from pcb.net_routing.runner import _split_failed
        from pcb.net_routing.recovery import RecoveryPolicy

        queue = []
        request = {"nets": [{"name": "BUS"}], "checkpoint_offset": 0}
        _split_failed(queue, [request], {"BUS": 5}, recovery=RecoveryPolicy(stages=0))
        self.assertEqual(queue[0][0]["checkpoint_offset"], 1)
        next_request = queue.pop(0)[0]
        _split_failed(queue, [next_request], {"BUS": 5}, recovery=RecoveryPolicy(stages=0))
        self.assertEqual(queue[0][0]["checkpoint_offset"], 2)
        last_request = queue.pop(0)[0]
        _split_failed(queue, [last_request], {"BUS": 5}, recovery=RecoveryPolicy(stages=0))
        self.assertEqual(queue, [])
        _split_failed(queue, [request], {"BUS": 2}, recovery=RecoveryPolicy(stages=0))
        self.assertEqual(queue, [])

    def test_unchanged_net_evidence_requires_all_copper_and_pads_to_match(self):
        from pcb import route_validation

        helper = getattr(route_validation, "stable_connection_items", None)
        self.assertTrue(callable(helper), "stable copper identity is missing")
        board = (
            '(kicad_pcb (footprint "x" (layer "B.Cu") (uuid "fp") (at 1 2) '
            '(pad "1" smd rect (net "P") (uuid "pad"))) '
            '(segment (start 0 0) (end 1 0) (width .2) (layer "B.Cu") '
            '(net "P") (uuid "s1")))'
        ).encode()
        stable = helper(board, board)
        self.assertEqual(stable.get("pad"), "P")
        self.assertIn("P", stable.values())
        changed_copper = board.replace(b'(width .2)', b'(width .3)')
        self.assertEqual(helper(board, changed_copper), {})
        changed_pad = board.replace(b'(at 1 2)', b'(at 1 3)')
        self.assertEqual(helper(board, changed_pad), {})
        zone_added = board[:-1] + b'(zone (net "P") (uuid "zone")))'
        self.assertEqual(helper(board, zone_added), {})

    def test_warning_anchor_can_change_only_when_the_net_is_proven_unchanged(self):
        import inspect
        from pcb.net_routing.runner import verify_resume_reports

        self.assertIn("stable_connection_items", inspect.signature(verify_resume_reports).parameters)
        baseline = {"open_connections": 2, "ignored_checks": [],
                    "violation_keys": [["connection_width", "warning", ["a", "b"]]],
                    "parity_keys": []}
        candidate = {**baseline, "open_connections": 1, "violation_keys":
                     [["connection_width", "warning", ["a", "pad"]]]}
        stable = {"a": "POWER", "b": "POWER", "pad": "POWER"}
        verify_resume_reports(baseline, candidate, expected_opens=1,
                              stable_connection_items=stable)
        disjoint = {**candidate, "violation_keys": [["connection_width", "warning", ["c", "pad"]]]}
        verify_resume_reports(baseline, disjoint, expected_opens=1,
                              stable_connection_items={**stable, "c": "POWER"})
        for unsafe in ({"a": "POWER", "b": "POWER"},
                       {"a": "POWER", "b": "POWER", "pad": "OTHER"}):
            with self.assertRaisesRegex(RuntimeError, "violations"):
                verify_resume_reports(baseline, candidate, expected_opens=1,
                                      stable_connection_items=unsafe)
        extra = {**candidate, "violation_keys": candidate["violation_keys"] * 2}
        with self.assertRaisesRegex(RuntimeError, "violations"):
            verify_resume_reports(baseline, extra, expected_opens=1,
                                  stable_connection_items=stable)

    def test_net_reconciliation_may_expose_opens_but_never_new_violations(self):
        from pcb.net_routing.runner import verify_resume_reports

        baseline = {"open_connections": 2, "errors": 0, "warnings": 0,
                    "ignored_checks": [], "violation_keys": [], "error_details": [],
                    "parity_keys": [["net_conflict", "warning", ["pad"]]]}
        candidate = {**baseline, "open_connections": 3, "parity_keys": []}
        with self.assertRaisesRegex(RuntimeError, "open-connection"):
            verify_resume_reports(baseline, candidate, expected_opens=3)
        verify_resume_reports(baseline, candidate, expected_opens=3, allow_open_increase=True)
        with self.assertRaisesRegex(RuntimeError, "violations"):
            verify_resume_reports(baseline, {**candidate, "violation_keys": [["shorting_items", "error", ["a"]]],
                                             "errors": 1, "error_details": []},
                                  expected_opens=3, allow_open_increase=True)
        with self.assertRaises(ValueError):
            verify_resume_reports(baseline, candidate, expected_opens=3, allow_open_increase=1)
        with self.assertRaisesRegex(RuntimeError, "open-connection"):
            verify_resume_reports({key: value for key, value in baseline.items() if key != "open_connections"},
                                  candidate, expected_opens=3, allow_open_increase=True)

    def test_identical_source_reports_allow_only_equivalent_warning_anchors(self):
        import inspect
        from pcb.net_routing.runner import verify_resume_reports

        self.assertIn("identical_inputs", inspect.signature(verify_resume_reports).parameters)
        baseline = {"open_connections": 2, "errors": 0, "warnings": 1,
                    "exclusions": 0, "parity_findings": 0, "ignored_checks": [],
                    "violation_keys": [["connection_width", "warning", ["pad", "a"]]],
                    "parity_keys": []}
        candidate = {**baseline, "violation_keys":
                     [["connection_width", "warning", ["pad", "b"]]]}
        with self.assertRaisesRegex(RuntimeError, "violations"):
            verify_resume_reports(baseline, candidate, expected_opens=2)
        verify_resume_reports(baseline, candidate, expected_opens=2, identical_inputs=True)
        changed = {**candidate, "violation_keys": [["clearance", "error", ["pad", "b"]]]}
        with self.assertRaises(RuntimeError):
            verify_resume_reports(baseline, changed, expected_opens=2, identical_inputs=True)
        with self.assertRaises(RuntimeError):
            verify_resume_reports(baseline, {**candidate, "warnings": 2},
                                  expected_opens=2, identical_inputs=True)

    def test_identical_source_detection_includes_settings_and_libraries(self):
        import hashlib
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from pcb.net_routing import runner

        helper = getattr(runner, "_same_snapshot_inputs", None)
        self.assertTrue(callable(helper), "snapshot identity check is missing")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            copied = root / "copy"
            source.mkdir()
            copied.mkdir()
            files = {"Board.kicad_pcb": b"board", "Board.kicad_dru": b"rules",
                     "Board.kicad_pro": b"settings", "libraries/custom.kicad_sym": b"symbols"}
            for name, data in files.items():
                (source / name).parent.mkdir(parents=True, exist_ok=True)
                (copied / name).parent.mkdir(parents=True, exist_ok=True)
                (source / name).write_bytes(data)
                (copied / name).write_bytes(data)
            fingerprints = {source / name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                            for name in files}
            snapshot = SimpleNamespace(source=source / "Board.kicad_pcb", directory=copied,
                                       board_path=copied / "Board.kicad_pcb", fingerprints=fingerprints,
                                       input_name=lambda path: path.relative_to(source))
            self.assertTrue(helper(snapshot, b"board"))
            for name, data in files.items():
                (copied / name).write_bytes(b"changed")
                self.assertFalse(helper(snapshot, b"board"), name)
                (copied / name).write_bytes(data)

    def test_resume_rechecks_native_drc_and_does_not_trust_saved_counts(self):
        from pcb.net_routing.runner import verify_resume_reports

        baseline = {"open_connections": 10, "ignored_checks": [],
                    "violation_keys": [["warning_a", "warning", ["a"]]], "parity_keys": []}
        candidate = {"open_connections": 9, "ignored_checks": [],
                     "violation_keys": [["warning_a", "warning", ["a"]]], "parity_keys": []}
        verify_resume_reports(baseline, candidate, expected_opens=9)
        with self.assertRaisesRegex(RuntimeError, "open"):
            verify_resume_reports(baseline, candidate, expected_opens=8)
        altered = {**candidate, "violation_keys": [["warning_b", "warning", ["b"]]]}
        with self.assertRaisesRegex(RuntimeError, "violations"):
            verify_resume_reports(baseline, altered, expected_opens=9)
        with self.assertRaisesRegex(RuntimeError, "ignored"):
            verify_resume_reports(baseline, {**candidate, "ignored_checks": ["shorting_items"]}, expected_opens=9)

    def test_resume_preserves_an_exact_replacement_warning_but_not_changed_copper(self):
        from pcb.net_routing.prepare import summarize_drc
        from pcb.net_routing.runner import verify_resume_reports
        from pcb.route_validation import _segment_identities

        def summary(identifier, width, opens):
            source = (
                f'(kicad_pcb (segment (start 1 2) (end 3 2) (width {width}) '
                f'(layer "F.Cu") (net "N") (uuid "{identifier}")))'
            ).encode()
            report = {
                "violations": [{"type": "track_dangling", "severity": "warning",
                                "items": [{"uuid": identifier}]}],
                "unconnected_items": [{}] * opens, "schematic_parity": [],
                "included_severities": ["error", "warning", "exclusion"], "ignored_checks": [],
            }
            return summarize_drc(report, segment_identities=_segment_identities(source))

        baseline = summary("old", ".2", 10)
        candidate = summary("new", ".2", 9)
        verify_resume_reports(baseline, candidate, expected_opens=9)
        with self.assertRaisesRegex(RuntimeError, "violations"):
            verify_resume_reports(baseline, summary("new", ".21", 9), expected_opens=9)

    def test_resume_rejects_worsened_existing_error_even_with_same_item_ids(self):
        from pcb.net_routing.prepare import summarize_drc
        from pcb.net_routing.runner import verify_resume_reports

        def summary(actual, opens):
            finding = {
                "type": "copper_edge_clearance", "severity": "error",
                "description": f"required 0.5 mm, actual {actual} mm",
                "items": [{"uuid": "pad"}, {"uuid": "hole"}],
            }
            return summarize_drc({
                "violations": [finding], "unconnected_items": [{}] * opens,
                "schematic_parity": [], "included_severities": ["error", "warning", "exclusion"],
                "ignored_checks": [],
            })

        baseline = summary("0.4214", 10)
        verify_resume_reports(baseline, summary("0.4214", 9), expected_opens=9)
        with self.assertRaisesRegex(RuntimeError, "error"):
            verify_resume_reports(baseline, summary("0.3000", 9), expected_opens=9)

    def test_resume_with_existing_errors_requires_detailed_evidence(self):
        from pcb.net_routing.runner import verify_resume_reports

        baseline = {"open_connections": 10, "ignored_checks": [],
                    "violation_keys": [["clearance", "error", ["pad", "hole"]]], "parity_keys": []}
        candidate = {**baseline, "open_connections": 9}
        with self.assertRaisesRegex(RuntimeError, "error"):
            verify_resume_reports(baseline, candidate, expected_opens=9)

    def test_resume_never_hides_a_second_new_dangling_endpoint(self):
        from pcb.net_routing.runner import verify_resume_reports

        def report(opens, warnings):
            return {
                "open_connections": opens, "ignored_checks": [],
                "violation_keys": [["track_dangling", level, [f"track-{index}"]]
                                   for index, (level, _) in enumerate(warnings)],
                "dangling_warnings": warnings, "parity_keys": [],
            }

        baseline = report(10, [["warning", ["a", "b"]]])
        verify_resume_reports(baseline, report(9, [["warning", ["a"]]]), expected_opens=9)
        with self.assertRaisesRegex(RuntimeError, "violations"):
            verify_resume_reports(baseline, report(9, [["warning", ["a", "new"]]]), expected_opens=9)
        with self.assertRaisesRegex(RuntimeError, "violations"):
            verify_resume_reports(
                baseline, report(9, [["warning", ["a"]], ["warning", ["b"]]]), expected_opens=9
            )
        with self.assertRaisesRegex(RuntimeError, "violations"):
            verify_resume_reports(baseline, report(9, [["error", ["a"]]]), expected_opens=9)
        previous = report(10, [["warning", ["a"]], ["warning", ["b"]]])
        verify_resume_reports(previous, report(9, [["warning", ["a", "b"]]]), expected_opens=9)

    def test_empty_work_queue_reports_measured_opens_and_retains_historical_checkpoint(self):
        import json
        from pathlib import Path
        import tempfile
        from pcb.net_routing.runner import PreviewStatus, read_status

        self.assertEqual(PreviewStatus.stopped({"queue": ["reference-repair"], "best_preview_opens": 0}), "paused")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = root / "results" / "autoroute" / ("a" * 32 + ".json")
            record.parent.mkdir(parents=True)
            for opens, expected in ((0, "preview_zero_opens"), (2, "stalled"), (None, "stalled"), (False, "stalled")):
                with self.subTest(opens=opens):
                    state = {"schema": 1, "run_id": record.stem, "status": "stalled", "queue": [],
                             "publication_enabled": False, "best_preview_opens": opens}
                    original = json.dumps(state).encode()
                    record.write_bytes(original)
                    report = read_status(root, record.stem)
                    self.assertEqual(PreviewStatus.stopped(state), expected)
                    self.assertEqual(report["status"], expected)
                    self.assertFalse(report["publication_enabled"])
                    self.assertEqual(record.read_bytes(), original)

    def test_stalled_checkpoint_can_be_resumed_with_a_new_plan(self):
        from pcb.net_routing.runner import resume_stalled

        state = {"status": "stalled", "best_preview_opens": 7, "failed_sweeps": 3}
        plan = {"rounds": [[{"nets": [{"name": "N"}]}]], "blocked_nets": {}, "island_counts": {"N": 2}}
        self.assertTrue(resume_stalled(state, plan))
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["sweep_start_opens"], 7)
        self.assertEqual(state["resume_count"], 1)
        self.assertEqual(state["queue"], plan["rounds"])

    def test_reference_repair_is_progress_only_when_coverage_improves_without_new_opens(self):
        from pcb.net_routing.runner import is_preview_improvement

        self.assertTrue(is_preview_improvement(10, 9, {}))
        self.assertTrue(is_preview_improvement(10, 10, {"status": "unverified", "comparable_missing_samples": [9, 3]}))
        self.assertFalse(is_preview_improvement(10, 11, {"status": "unverified", "comparable_missing_samples": [9, 0]}))
        self.assertFalse(is_preview_improvement(10, 10, {"status": "degraded", "comparable_missing_samples": [9, 3]}))
        self.assertFalse(is_preview_improvement(10, 9, {"status": "degraded", "comparable_missing_samples": [9, 3]}))
        self.assertFalse(is_preview_improvement(10, 10, {"status": "unverified", "comparable_missing_samples": None}))

    def test_unsupported_search_stops_instead_of_moving_on(self):
        from unittest import mock
        from pcb.net_routing import batch
        from pcb.net_routing.checkpoint import UnsupportedRoutingRequest

        with mock.patch.object(batch, "plan_nets", side_effect=UnsupportedRoutingRequest(
            "repair needs more fragments"
        )):
            with self.assertRaisesRegex(UnsupportedRoutingRequest, "fragments"):
                batch._search((".", {}))
        with mock.patch.object(batch, "plan_nets", side_effect=RuntimeError("sources changed")):
            with self.assertRaisesRegex(RuntimeError, "sources changed"):
                batch._search((".", {}))

    def test_unexpected_value_error_is_not_misreported_as_an_unsupported_request(self):
        from unittest import mock
        from pcb.net_routing import batch

        with mock.patch.object(batch, "plan_nets", side_effect=ValueError("unexpected rule failure")):
            with self.assertRaisesRegex(ValueError, "unexpected rule failure"):
                batch._search((".", {}))

    def test_missing_search_statistics_are_not_reported_as_zero(self):
        from io import StringIO
        from core.console import Console
        from pcb.net_routing.presentation import RoutingDisplay
        from pcb.net_routing.runner import summarize_attempts

        attempts = summarize_attempts({"attempts": [{"status": "already_connected"}]})
        self.assertIsNone(attempts[0]["path_calls"])
        self.assertIsNone(attempts[0]["elapsed_seconds"])
        output = StringIO()
        display = RoutingDisplay(Console(verbose=True, stream=output))
        display({"event": "round", "saved_opens": 5, "preview_opens": 4,
                 "result": {"outcome": "accepted_preview", "attempts": attempts}})
        self.assertEqual(output.getvalue().count("Not recorded"), 2)

    def test_failed_search_reports_why_without_emitting_large_candidates(self):
        from pcb.net_routing.runner import summarize_attempts

        result = {"attempts": [{"status": "no_complete_candidate_found",
                  "search": {"limits_hit": ["seconds"], "failures": {"blocked": 5},
                             "path_calls": 4, "elapsed_seconds": 2},
                  "unvalidated_campaign_changes": ["large private data"]}]}
        summary = summarize_attempts(result)
        self.assertEqual(summary[0]["limits_hit"], ["seconds"])
        self.assertEqual(summary[0]["failures"], {"blocked": 5})
        self.assertNotIn("unvalidated_campaign_changes", summary[0])

    def test_rejection_summary_preserves_broken_net_and_parity_details(self):
        from pcb.net_routing.runner import summarize_attempts

        result = {
            "status": "rejected",
            "attempts": [],
            "validation": {
                "new_violations": [],
                "new_schematic_parity": [{
                    "type": "pin_to_pin", "description": "Connection differs"
                }],
                "broken_existing_connections": [{
                    "net": "GND", "representative": "pad-a",
                    "disconnected_items": ["via-b", "pad-c"],
                }],
            },
        }
        diagnostic = summarize_attempts(result)[-1]
        self.assertIn("new schematic parity", diagnostic["reason"])
        self.assertIn("broken existing connections", diagnostic["reason"])
        self.assertTrue(any("GND" in item["reason"] for item in diagnostic["rejected_candidates"]))
        broken = next(item for item in diagnostic["rejected_candidates"] if item.get("net") == "GND")
        self.assertEqual(broken["representative"], "pad-a")
        self.assertEqual(broken["disconnected_items"], ["via-b", "pad-c"])
        self.assertTrue(any("Connection differs" in item["reason"] for item in diagnostic["rejected_candidates"]))

    def test_rejection_summary_includes_distinct_violation_types(self):
        from pcb.net_routing.runner import summarize_attempts

        violations = [{"type": "clearance", "severity": "error", "description": "copper"}] * 4
        violations += [
            {"type": "hole_clearance", "severity": "error", "description": "hole"},
            {"type": "courtyards_overlap", "severity": "error", "description": "courtyard"},
            {"type": "track_dangling", "severity": "warning", "description": "tail"},
        ]
        summary = summarize_attempts({"status": "rejected", "attempts": [],
                                      "validation": {"new_violations": violations}})[-1]
        reasons = [item["reason"] for item in summary["rejected_candidates"]]
        for kind in ("clearance", "hole_clearance", "courtyards_overlap", "track_dangling"):
            self.assertTrue(any(reason.startswith(kind + ":") for reason in reasons), kind)

    def test_failed_run_is_not_reported_as_running(self):
        import json
        import tempfile
        from pathlib import Path
        from pcb.net_routing.runner import _record_failures

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.json"
            state = {"status": "running", "queue": ["pending"]}
            target.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "example"):
                with _record_failures(target, state):
                    state["queue"].clear()
                    raise RuntimeError("example")
            saved = json.loads(target.read_text())
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["queue"], ["pending"])

    def test_status_detects_a_run_terminated_without_a_final_save(self):
        import json
        import tempfile
        from pathlib import Path
        from pcb.net_routing.runner import read_status

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "results" / "autoroute"
            output.mkdir(parents=True)
            run_id = "a" * 32
            target = output / f"{run_id}.json"
            state = {"schema": 1, "run_id": run_id, "status": "running",
                     "checkpoint": {"unchanged": True}, "queue": ["pending"]}
            target.write_text(json.dumps(state), encoding="utf-8")
            (output / "active.lock").write_bytes(b"0")
            before = target.read_bytes()
            result = read_status(root, run_id)
            self.assertEqual(result["status"], "interrupted")
            self.assertIn("checkpoint", result["failure"].lower())
            self.assertEqual(target.read_bytes(), before)

    def test_status_keeps_running_when_the_active_lock_is_held(self):
        import json
        import tempfile
        from pathlib import Path
        from pcb.net_routing.runner import _exclusive, read_status

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "results" / "autoroute"
            output.mkdir(parents=True)
            run_id = "a" * 32
            (output / f"{run_id}.json").write_text(
                json.dumps({"schema": 1, "run_id": run_id, "status": "running"}),
                encoding="utf-8",
            )
            with _exclusive(output / "active.lock"):
                self.assertEqual(read_status(root, run_id)["status"], "running")

    def test_resume_recommendation_requires_renewal_when_tools_change(self):
        from pcb.net_routing.runner import resume_action

        checkpoint = {"inputs": {"board": "original"}, "policy": "p",
                      "tools": {"planner": "old"}}
        self.assertEqual(
            resume_action(checkpoint, {"board": "original"}, "p", {"planner": "new"}),
            "renew",
        )
        self.assertEqual(
            resume_action(checkpoint, {"board": "changed"}, "p", {"planner": "new"}),
            "restart",
        )
        self.assertEqual(
            resume_action(checkpoint, {"board": "original"}, "p", {"planner": "old"}),
            "resume",
        )

    def test_status_displays_the_required_resume_action(self):
        from io import StringIO
        from core.console import Console
        from pcb.net_routing.presentation import show_run

        output = StringIO()
        show_run(Console(stream=output), {
            "status": "interrupted", "run_id": "a" * 32, "resume_action": "renew",
        })
        self.assertIn("--renew " + "a" * 32, output.getvalue())
        self.assertNotIn("--resume ", output.getvalue())

    def test_verbose_status_aggregates_repeated_reference_blockers_without_changing_evidence(self):
        from io import StringIO
        from core.console import Console
        from pcb.net_routing.presentation import show_run
        reasons = ["Reference conflict a: no editable track or via",
                   "Reference conflict b: no editable track or via",
                   "Reference conflict c: net requires separate review"]
        output = StringIO()
        report = {"status": "paused", "reference_repair_blockers": list(reasons)}
        show_run(Console(verbose=True, stream=output), report)
        self.assertIn("no editable track or via (2)", output.getvalue())
        self.assertIn("net requires separate review (1)", output.getvalue())
        self.assertNotIn("Reference conflict a", output.getvalue())
        self.assertEqual(report["reference_repair_blockers"], reasons)

    def test_heartbeat_reports_during_a_long_search_and_stops(self):
        from queue import Queue
        from threading import current_thread
        from pcb.net_routing.autoroute_cli import progress_heartbeat

        reports = Queue()

        def record(event):
            reports.put((current_thread(), event))

        with progress_heartbeat(record, interval=0.01):
            worker, event = reports.get(timeout=5)
            self.assertEqual(event["event"], "heartbeat")
        self.assertFalse(worker.is_alive())

    def test_cli_enforces_and_records_the_aggregate_memory_limit(self):
        from contextlib import contextmanager, redirect_stdout
        from io import StringIO
        from unittest import mock
        from pcb.net_routing import autoroute_cli

        @contextmanager
        def bounded(**kwargs):
            yield {"job_commit_limit_bytes": 1234}

        with mock.patch.object(autoroute_cli, "limit_resources", bounded), \
             mock.patch.object(autoroute_cli, "run_project", return_value={"status": "paused"}) as run, \
             redirect_stdout(StringIO()):
            self.assertEqual(autoroute_cli.main(["--project", ".", "run", "--max-rounds", "1"]), 0)
        self.assertEqual(run.call_args.kwargs["resource_limits"]["job_commit_limit_bytes"], 1234)

    def test_publication_stays_disabled_until_electrical_checks_exist(self):
        import contextlib
        import io
        from pcb.net_routing.autoroute_cli import main

        error = io.StringIO()
        with contextlib.redirect_stderr(error), self.assertRaisesRegex(SystemExit, "2"):
            main(["--project", ".", "run", "--apply"])
        self.assertIn("publication is disabled", error.getvalue())

    def test_an_explicit_exclusion_remains_visible_as_a_blocker(self):
        from pcb.net_routing.controller import make_work_plan

        preparation = {"unqualified_nets": [], "work": {"items": [{"name": "X", "priority": 1, "window": (0., 0., 1., 1.), "island_count": 2}]},
                       "reference_copper": {"high_speed_nets": []}}
        policy = {"exclude_nets": ["X"], "route": {"layers": ["B.Cu"], "step_mm": .1}, "search": {}}
        plan = make_work_plan(preparation, policy)
        self.assertEqual(plan["rounds"], [])
        self.assertIn("X", plan["blocked_nets"])

    def test_unqualified_nets_remain_visible_but_are_not_scheduled(self):
        from pcb.net_routing.controller import make_work_plan

        preparation = {"unqualified_nets": ["POWER"], "work": {"items": [
            {"name": "POWER", "priority": 0, "window": (0., 0., 1., 1.), "island_count": 2},
            {"name": "SIGNAL", "priority": 1, "window": (2., 0., 3., 1.), "island_count": 2},
        ]}, "reference_copper": {"high_speed_nets": []}}
        plan = make_work_plan(preparation, {"route": {"layers": ["B.Cu"], "step_mm": .1}, "search": {}})
        self.assertEqual([net["name"] for group in plan["rounds"] for request in group for net in request["nets"]],
                         ["SIGNAL"])
        self.assertIn("POWER", plan["blocked_nets"])
        with self.assertRaisesRegex(ValueError, "qualification"):
            make_work_plan({key: value for key, value in preparation.items() if key != "unqualified_nets"}, {})


if __name__ == "__main__":
    unittest.main()
