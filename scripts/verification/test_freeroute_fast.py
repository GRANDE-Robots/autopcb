"""Check that batch routing continues only from retained private previews."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pcb.net_routing import fast
from core.toolchain import RouterCache


class ExternalBatchTests(unittest.TestCase):
    def test_rework_receipts_survive_restart_and_require_the_same_context(self):
        state = {"checkpoint": {"candidate_sha256": "board"}}
        inputs, tools, policy, settings = {"source": "a"}, {"script": "a"}, "policy", {"seconds": 30}
        key = fast.fast_retry_signature(state, inputs, tools, policy, settings)
        receipt = {"key": key, "settings": settings, "attempt": "a" * 64}
        fast.remember_rework_attempt(state, inputs, tools, policy, receipt)
        fast.remember_rework_attempt(state, inputs, tools, policy, receipt)
        import json

        restored = json.loads(json.dumps(state))
        self.assertEqual(fast.rework_attempts(restored, key), ("a" * 64,))
        for changed in (
            fast.fast_retry_signature(state, inputs, {"script": "b"}, policy, settings),
            fast.fast_retry_signature(state, {"source": "b"}, tools, policy, settings),
            fast.fast_retry_signature(state, inputs, tools, policy, {"seconds": 60}),
            fast.fast_retry_signature({"checkpoint": {"candidate_sha256": "other"}}, inputs, tools, policy, settings),
        ):
            self.assertEqual(fast.rework_attempts(restored, changed), ())
        with self.assertRaisesRegex(RuntimeError, "context changed"):
            fast.remember_rework_attempt(state, inputs, {"script": "changed"}, policy, receipt)
        full = {"checkpoint": state["checkpoint"], "rework_retry": {
            "key": key, "attempts": [format(i, "064x") for i in range(fast.MAX_REWORK_ATTEMPTS)]}}
        with self.assertRaisesRegex(RuntimeError, "budget is exhausted"):
            fast.remember_rework_attempt(full, inputs, tools, policy, receipt)
        for invalid in (["bad"], ["a" * 64, "a" * 64], [True]):
            with self.assertRaises(ValueError):
                fast.rework_attempts({"rework_retry": {"key": key, "attempts": invalid}}, key)

    def test_elapsed_evidence_distinguishes_completed_and_interrupted_trials(self):
        self.assertIsNone(fast._elapsed_evidence(False, None))
        self.assertIn("interrupted", fast._elapsed_evidence(True, None))
        self.assertEqual(fast._elapsed_evidence(True, " completed private trial "),
                         "completed private trial")
        with self.assertRaises(ValueError):
            fast._elapsed_evidence(True, " ")

    def setUp(self):
        recorder = patch.object(fast, "record_unimproved_batch")
        self.record = recorder.start()
        self.addCleanup(recorder.stop)
        loader = patch.object(fast, "load_fast_retry", return_value={})
        loader.start()
        self.addCleanup(loader.stop)

    def test_reuses_new_checkpoint_and_stops_when_no_improvement(self):
        first = "a" * 32
        second = "b" * 32
        third = "c" * 32
        results = [
            {"status": "private_preview_retained", "run_id": second, "preview_opens": 250},
            {"status": "private_preview_retained", "run_id": third, "preview_opens": 240},
            {"status": "no_improvement", "preview_opens": 240},
        ]
        with patch.object(fast, "run_one", side_effect=results) as run:
            outcome = fast.run_batches(Path("."), first, batches=10, items=80, timeout=180)
        self.assertEqual(outcome["run_id"], third)
        self.assertEqual(outcome["accepted_batches"], 2)
        self.assertEqual(outcome["status"], "no_improvement")
        self.assertEqual([call.args[1] for call in run.call_args_list], [first, second, third])
        self.record.assert_called_once()

    def test_rejects_invalid_batch_budget(self):
        with self.assertRaises(ValueError):
            fast.run_batches(Path("."), "a" * 32, batches=0, items=80, timeout=180)
        with self.assertRaises(ValueError):
            fast.run_batches(Path("."), "a" * 32, batches=1, items=-1, timeout=180)

    def test_failed_plot_update_keeps_the_already_saved_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "run.json"
            state_path.write_text('{"elapsed_seconds": 12}', encoding="utf-8")
            state = {"run_id": "a" * 32, "elapsed_seconds": 12}
            with (patch.object(fast, "_publish_progress", side_effect=OSError("drive unavailable")),
                  patch.object(fast, "_save", side_effect=OSError("drive unavailable"))):
                with self.assertRaisesRegex(RuntimeError, "measurement was saved.*plot"):
                    fast._publish_recorded_progress(Path(directory), {}, state_path, state)
            self.assertEqual(state_path.read_text(encoding="utf-8"), '{"elapsed_seconds": 12}')

    def test_focus_selects_only_unfinished_nets_and_preserves_blocks(self):
        members = {"OPEN", "CONNECTED", "POWER", "PAIR"}
        classes = {"OPEN": "SIGNAL", "CONNECTED": "SIGNAL", "POWER": "POWER", "PAIR": "SIGNAL"}
        opens = {"OPEN": 1, "POWER": 2, "PAIR": 1}
        targets, deferred = fast.focus_open_nets(members, classes, opens, ("SIGNAL",), {"PAIR"})
        self.assertEqual(targets, ("OPEN",))
        self.assertEqual(set(deferred), {"CONNECTED", "POWER", "PAIR"})
        with self.assertRaisesRegex(ValueError, "unidentified"):
            fast.focus_open_nets(members, classes, {"<unknown>": 1}, ("SIGNAL",), set())
        with self.assertRaisesRegex(ValueError, "export"):
            fast.focus_open_nets(members - {"OPEN"}, classes, opens, ("SIGNAL",), {"PAIR"})

    def test_focus_can_reinspect_the_same_private_board_after_rework(self):
        """Each fresh inventory needs its own native DRC and DSN output."""
        import pcbnew

        board = MagicMock()
        board.GetNetsByName.return_value = {"OPEN": 1, "CONNECTED": 2}
        board.FindNet.return_value.GetNetClassName.return_value = "SIGNAL"
        member_class = MagicMock()
        member_class.atoms.return_value = [MagicMock(), MagicMock(),
                                           MagicMock(value="OPEN"), MagicMock(value="CONNECTED")]
        network = MagicMock()
        network.children.return_value = [member_class]
        outputs = []

        def drc(_cli, _board, output, **_kwargs):
            self.assertFalse(output.exists(), "native DRC cannot overwrite its own output")
            outputs.append(output)
            output.write_text("{}", encoding="utf-8")
            return {}

        def export(_board, path):
            Path(path).write_text("dsn", encoding="utf-8")
            return True

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "v4.1.kicad_pcb").write_text("pcb", encoding="utf-8")
            with (patch("pcb.route_validation._drc", side_effect=drc),
                  patch.object(pcbnew, "LoadBoard", return_value=board),
                  patch.object(pcbnew, "ExportSpecctraDSN", side_effect=export),
                  patch.object(fast, "native_item_nets", return_value={}),
                  patch.object(fast, "unconnected_by_net", return_value={"OPEN": 1}),
                  patch.object(fast, "_dsn_scope", return_value=(None, network))):
                first = fast._focus_deferred(project, {}, ("SIGNAL",), ())
                second = fast._focus_deferred(project, {}, ("SIGNAL",), ())
                with patch.object(fast, "unqualified_working_nets", return_value={"OPEN"}):
                    protected = fast._focus_deferred(project, {}, ("SIGNAL",), ())
        self.assertEqual(first, (("OPEN",), ("CONNECTED",)))
        self.assertEqual(first, second)
        self.assertEqual(protected, ((), ("CONNECTED", "OPEN")))
        self.assertNotEqual(outputs[0], outputs[1])

    def test_unrouted_comparison_imports_no_placement(self):
        import pcbnew

        board = MagicMock()
        board.GetFootprints.return_value = []
        with tempfile.TemporaryDirectory() as directory:
            trial = Path(directory)

            def run(command, **_kwargs):
                Path(command[command.index("-do") + 1]).write_text(
                    "(session x (base_design x) (placement (component FOO)) "
                    "(routes (network_out)))", encoding="utf-8")
                return MagicMock(returncode=0)

            def import_session(_board, path):
                data = Path(path).read_text(encoding="utf-8")
                self.assertNotIn("(placement", data)
                self.assertIn("(network_out)", data)
                return True

            with (patch.object(fast.subprocess, "run", side_effect=run),
                  patch.object(pcbnew, "LoadBoard", return_value=board),
                  patch.object(pcbnew, "ImportSpecctraSES", side_effect=import_session),
                  patch.object(pcbnew, "SaveBoard", return_value=True)):
                result = fast._roundtrip(trial / "input.kicad_pcb", trial,
                                         trial / "router.exe", "auto", 30, "fastroute")
            self.assertEqual(result, trial / "roundtrip.kicad_pcb")

    def test_only_exported_protected_nets_are_sent_to_the_router(self):
        board = MagicMock()
        with patch.object(fast, "unqualified_working_nets", return_value={"POWER", "NOT_EXPORTED"}):
            self.assertEqual(fast.protected_export_members(board, {"SIGNAL", "POWER"}, ()), ("POWER",))

    def test_focus_option_is_passed_to_the_search(self):
        result = {"status": "no_improvement", "preview_opens": 191}
        with (patch.object(fast, "run_one", return_value=result) as run,
              patch.object(fast, "load_fast_retry", return_value={})):
            fast.run_batches(Path("."), "a" * 32, batches=1, items=80, timeout=70,
                             focus_classes=("SIGNAL",))
        self.assertEqual(run.call_args.kwargs["focus_classes"], ("SIGNAL",))

    def test_krt_uses_existing_batch_pipeline_with_a_pinned_tool_identity(self):
        result = {"status": "no_improvement", "preview_opens": 191}
        with (patch("pcb.net_routing.krt_adapter.validate_installation", return_value="tool-id") as verify,
              patch.object(fast, "run_one", return_value=result) as run):
            fast.run_batches(Path("."), "a" * 32, batches=1, items=0, timeout=70,
                             engine="krt", router_python="python.exe")
        verify.assert_called_once()
        self.assertEqual(run.call_args.kwargs["engine"], "krt")
        self.assertEqual(run.call_args.kwargs["router_python"], "python.exe")
        self.assertEqual(run.call_args.kwargs["items"], 0)

    def test_krt_rejects_unsupported_optimizer_and_multiple_starts(self):
        with self.assertRaisesRegex(ValueError, "optimizer"):
            fast.run_batches(Path("."), "a" * 32, batches=1, items=80, timeout=70,
                             engine="krt", optimizer=True)
        with self.assertRaisesRegex(ValueError, "Multiple starts"):
            fast.run_batches(Path("."), "a" * 32, batches=1, items=80, timeout=70,
                             engine="krt", starts=2)

    def test_krt_grid_resolution_is_forwarded_and_bound_to_retry_settings(self):
        result = {"status": "no_improvement", "preview_opens": 191}
        with (patch("pcb.net_routing.krt_adapter.validate_installation", return_value="tool-id"),
              patch.object(fast, "load_fast_retry", return_value={}) as load,
              patch.object(fast, "run_one", return_value=result) as run):
            fast.run_batches(Path("."), "a" * 32, batches=1, items=0, timeout=70,
                             engine="krt", focus_classes=("SIGNAL",), router_grid_mm=0.2)
        self.assertEqual(run.call_args.kwargs["router_grid_mm"], 0.2)
        self.assertEqual(load.call_args.args[2]["router_grid_mm"], 0.2)

    def test_krt_full_mode_is_forwarded_and_kept_distinct_from_quick_retries(self):
        result = {"status": "no_improvement", "preview_opens": 191}
        with (patch("pcb.net_routing.krt_adapter.validate_installation", return_value="tool-id"),
              patch.object(fast, "load_fast_retry", return_value={}) as load,
              patch.object(fast, "run_one", return_value=result) as run):
            fast.run_batches(Path("."), "a" * 32, batches=1, items=0, timeout=70,
                             engine="krt", focus_classes=("SIGNAL",), router_full=True)
        self.assertTrue(run.call_args.kwargs["router_full"])
        self.assertTrue(load.call_args.args[2]["router_full"])
        with self.assertRaisesRegex(ValueError, "only.*krt"):
            fast.run_batches(Path("."), "a" * 32, batches=1, items=0, timeout=70,
                             engine="fastroute", router_full=True)

    def test_optimizer_mode_is_forwarded_and_has_distinct_retry_settings(self):
        result = {"status": "no_improvement", "preview_opens": 191}
        with (patch.object(fast, "run_one", return_value=result) as run,
              patch.object(fast, "load_fast_retry", return_value={}) as load):
            fast.run_batches(Path("."), "a" * 32, batches=1, items=80, timeout=70,
                             focus_classes=("SIGNAL",), optimizer=True)
        self.assertTrue(run.call_args.kwargs["optimizer"])
        self.assertTrue(load.call_args.args[2]["optimizer"])
        with self.assertRaisesRegex(ValueError, "optimizer"):
            fast.run_batches(Path("."), "a" * 32, batches=1, items=80, timeout=70,
                             optimizer="yes")

    def test_pad_neckdown_is_forwarded_and_bound_to_failed_attempt_memory(self):
        result = {"status": "no_improvement", "preview_opens": 3}
        with (patch.object(fast, "run_one", return_value=result) as run,
              patch.object(fast, "load_fast_retry", return_value={}) as load):
            fast.run_batches(Path("."), "a" * 32, batches=1, items=80, timeout=70,
                             engine="fastroute", search_pad_neckdown=True)
        self.assertTrue(run.call_args.kwargs["search_pad_neckdown"])
        self.assertTrue(load.call_args.args[2]["search_pad_neckdown"])
        for invalid in (None, 1, "yes"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "search_pad_neckdown"):
                fast.run_batches(Path("."), "a" * 32, batches=1, items=80, timeout=70,
                                 search_pad_neckdown=invalid)
        with self.assertRaisesRegex(ValueError, "fastroute or Freerouting"):
            fast.run_batches(Path("."), "a" * 32, batches=1, items=80, timeout=70,
                             engine="krt", search_pad_neckdown=True)

    def test_retry_identity_changes_with_inputs_tools_or_search_settings(self):
        state = {"checkpoint": {"candidate_sha256": "board-a"}}
        signature = fast.fast_retry_signature(state, {"pcb": "a"}, {"router": "one"}, "policy", {"timeout": 30})
        self.assertNotEqual(signature, fast.fast_retry_signature(state, {"pcb": "b"}, {"router": "one"},
                                                                 "policy", {"timeout": 30}))
        self.assertNotEqual(signature, fast.fast_retry_signature(state, {"pcb": "a"}, {"router": "two"},
                                                                 "policy", {"timeout": 30}))
        self.assertNotEqual(signature, fast.fast_retry_signature(state, {"pcb": "a"}, {"router": "one"},
                                                                 "policy", {"timeout": 60}))

    def test_exhausted_identical_focused_search_does_not_run_again(self):
        run_id = "a" * 32
        cached = {"deferred_nets": ["A"], "deferred_classes": [], "exhausted": True,
                  "preview_opens": 191}
        with (patch.object(fast, "load_fast_retry", return_value=cached),
              patch.object(fast, "run_one") as run):
            result = fast.run_batches(Path("."), run_id, batches=2, items=0, timeout=70,
                                      engine="fastroute", focus_classes=("SIGNAL",))
        self.assertEqual(result["status"], "deferred_repeat")
        self.assertEqual(result["preview_opens"], 191)
        run.assert_not_called()

    def test_automatic_focus_reuses_and_records_failed_net_deferrals(self):
        run_id = "a" * 32
        cached = {"deferred_nets": ["A"], "deferred_classes": [], "exhausted": False,
                  "preview_opens": 191}
        result = {"status": "no_improvement", "run_id": run_id, "preview_opens": 191,
                  "repair_blockers": {"B": "no safe route"}}
        with (patch.object(fast, "load_fast_retry", return_value=cached) as load,
              patch.object(fast, "run_one", return_value=result) as run):
            fast.run_batches(Path("."), run_id, batches=1, items=0, timeout=70,
                             engine="fastroute")
        load.assert_called_once()
        self.assertEqual(run.call_args.kwargs["ignore_nets"], ("A",))
        self.assertEqual(self.record.call_args.kwargs["retry"]["deferred_nets"], ("A", "B"))

    def test_explicit_retry_can_run_an_exhausted_focused_search(self):
        result = {"status": "no_improvement", "preview_opens": 191}
        with (patch.object(fast, "load_fast_retry") as load,
              patch.object(fast, "run_one", return_value=result) as run):
            fast.run_batches(Path("."), "a" * 32, batches=1, items=0, timeout=70,
                             engine="fastroute", focus_classes=("SIGNAL",), retry_exhausted=True)
        load.assert_not_called()
        run.assert_called_once()

    def test_does_not_defer_the_only_focused_class_and_repeat_it(self):
        result = {"status": "no_improvement", "preview_opens": 191, "dominant_class": "SIGNAL"}
        with (patch.object(fast, "load_fast_retry", return_value={}),
              patch.object(fast, "run_one", return_value=result) as run):
            fast.run_batches(Path("."), "a" * 32, batches=2, items=0, timeout=70,
                             engine="fastroute", focus_classes=("SIGNAL",))
        self.assertEqual(run.call_count, 1)
        self.assertTrue(self.record.call_args.kwargs["retry"]["exhausted"])

    def test_full_pass_requires_a_bounded_fastroute_run(self):
        with self.assertRaisesRegex(ValueError, "time-bounded"):
            fast.run_batches(Path("."), "a" * 32, batches=1, items=0, timeout=180, engine="Freerouting")
        with self.assertRaisesRegex(ValueError, "time-bounded"):
            fast.run_batches(Path("."), "a" * 32, batches=1, items=0, timeout=10, engine="fastroute")
        with patch.object(fast, "run_one", return_value={"status": "no_improvement", "preview_opens": 231}) as run:
            fast.run_batches(Path("."), "a" * 32, batches=1, items=0, timeout=70, engine="fastroute")
        self.assertEqual(run.call_args.kwargs["items"], 0)

    def test_defers_a_dominant_class_after_a_non_improving_batch(self):
        first, second = "a" * 32, "b" * 32
        results = [
            {"status": "no_improvement", "run_id": first, "preview_opens": 250,
             "dominant_class": "GROUND"},
            {"status": "private_preview_retained", "run_id": second, "preview_opens": 245},
        ]
        with patch.object(fast, "run_one", side_effect=results) as run:
            result = fast.run_batches(Path("."), first, batches=2, items=80, timeout=180)
        self.assertEqual(result["run_id"], second)
        self.assertEqual(run.call_args_list[0].kwargs["ignore_net_classes"], ())
        self.assertEqual(run.call_args_list[1].kwargs["ignore_net_classes"], ("GROUND",))

    def test_default_class_deferral_uses_the_exported_class_name(self):
        result = {"status": "no_improvement", "preview_opens": 203}
        with patch.object(fast, "run_one", return_value=result) as run:
            fast.run_batches(Path("."), "a" * 32, batches=1, items=80, timeout=90,
                             initial_deferred=("Default",))
        self.assertEqual(run.call_args.kwargs["ignore_net_classes"], ("kicad_default",))
        with self.assertRaisesRegex(ValueError, "distinct"):
            fast.run_batches(Path("."), "a" * 32, batches=1, items=80, timeout=90,
                             initial_deferred=("Default", "kicad_default"))

    def test_temporarily_defers_failed_nets_without_discarding_their_entire_class(self):
        first, second = "a" * 32, "b" * 32
        results = [
            {"status": "no_improvement", "run_id": first, "preview_opens": 250,
             "dominant_class": "CORE_POWER", "repair_blockers": {"SYS5_CORE": "no local path"}},
            {"status": "private_preview_retained", "run_id": second, "preview_opens": 249,
             "repair_blockers": {}},
        ]
        with patch.object(fast, "run_one", side_effect=results) as run:
            result = fast.run_batches(Path("."), first, batches=2, items=80, timeout=180)
        self.assertEqual(result["run_id"], second)
        self.assertEqual(run.call_args_list[1].kwargs["ignore_net_classes"], ())
        self.assertEqual(run.call_args_list[1].kwargs["ignore_nets"], ("SYS5_CORE",))

    def test_failed_nets_are_revisited_after_three_improving_batches(self):
        ids = [str(i) * 32 for i in range(1, 5)]
        results = [
            {"status": "private_preview_retained", "run_id": ids[i], "preview_opens": 250 - i,
             "repair_blockers": {"SYS5_CORE": "no local path"} if i == 0 else {}}
            for i in range(3)
        ] + [{"status": "no_improvement", "run_id": ids[3], "preview_opens": 248}]
        with patch.object(fast, "run_one", side_effect=results) as run:
            fast.run_batches(Path("."), "a" * 32, batches=4, items=80, timeout=180)
        self.assertEqual(run.call_args_list[1].kwargs["ignore_nets"], ("SYS5_CORE",))
        self.assertEqual(run.call_args_list[2].kwargs["ignore_nets"], ("SYS5_CORE",))
        self.assertEqual(run.call_args_list[3].kwargs["ignore_nets"], ())

    def test_explicitly_excluded_classes_remain_excluded_after_improvements(self):
        ids = [str(i) * 32 for i in range(1, 5)]
        results = [{"status": "private_preview_retained", "run_id": ids[i], "preview_opens": 250 - i}
                   for i in range(3)] + [{"status": "no_improvement", "run_id": ids[3], "preview_opens": 248}]
        with patch.object(fast, "run_one", side_effect=results) as run:
            fast.run_batches(Path("."), "a" * 32, batches=4, items=80, timeout=180,
                             excluded_classes=("GROUND",), excluded_nets=("CRITICAL",))
        for call in run.call_args_list:
            self.assertIn("GROUND", call.kwargs["ignore_net_classes"])
            self.assertIn("CRITICAL", call.kwargs["ignore_nets"])

    def test_no_short_batch_proposal_triggers_a_bounded_full_pass(self):
        first, second = "a" * 32, "b" * 32
        results = [
            {"status": "no_safe_proposal", "run_id": first, "preview_opens": 250},
            {"status": "private_preview_retained", "run_id": second, "preview_opens": 249},
        ]
        with patch.object(fast, "run_one", side_effect=results) as run:
            result = fast.run_batches(Path("."), first, batches=2, items=80, timeout=70, engine="fastroute")
        self.assertEqual(result["run_id"], second)
        self.assertEqual([call.kwargs["items"] for call in run.call_args_list], [80, 0])

    def test_last_short_batch_does_not_claim_a_full_pass_will_run(self):
        messages = []
        with patch.object(fast, "run_one", return_value={"status": "no_safe_proposal", "preview_opens": 214}):
            fast.run_batches(Path("."), "a" * 32, batches=1, items=80, timeout=70,
                             engine="fastroute", progress=messages.append)
        self.assertFalse(any("trying a time-bounded full pass" in message for message in messages))

    def test_passes_the_selected_engine_to_each_batch(self):
        run_id = "a" * 32
        with patch.object(fast, "run_one", return_value={"status": "no_improvement", "preview_opens": 245}) as run:
            fast.run_batches(Path("."), run_id, batches=1, items=80, timeout=180, engine="fastroute")
        self.assertEqual(run.call_args.kwargs["engine"], "fastroute")
        self.assertEqual(run.call_args.kwargs["jar"], RouterCache.fastroute())

    def test_can_compare_two_router_orders_without_changing_validation(self):
        with patch.object(fast, "run_one", return_value={"status": "no_improvement", "preview_opens": 211}) as run:
            fast.run_batches(Path("."), "a" * 32, batches=1, items=80, timeout=70,
                             engine="fastroute", starts=2)
        self.assertEqual(run.call_args.kwargs["starts"], 2)
        with self.assertRaisesRegex(ValueError, "starts"):
            fast.run_batches(Path("."), "a" * 32, batches=1, items=80, timeout=70, starts=0)

    def test_auto_engine_preserves_explicit_jar_compatibility(self):
        run_id = "a" * 32
        with patch.object(fast, "run_one", return_value={"status": "no_improvement", "preview_opens": 245}) as run:
            fast.run_batches(Path("."), run_id, batches=1, items=80, timeout=180, jar=Path("older.jar"))
        self.assertEqual(run.call_args.kwargs["engine"], "Freerouting")

    def test_validation_worker_returns_its_source_bound_cache(self):
        cache = {}

        def retain(*_args, saved_checks_cache, **_kwargs):
            saved_checks_cache.update(key="source", drc={"errors": 0})
            return {"status": "private_preview_retained"}

        with patch.object(fast, "retain_candidate", side_effect=retain):
            result, returned = fast._retain_in_worker(Path("project"), "a" * 32, Path("candidate"),
                                                      1.0, b"state", "fastroute", cache)
        self.assertEqual(result["status"], "private_preview_retained")
        self.assertEqual(returned, {"key": "source", "drc": {"errors": 0}})

    def test_overall_rate_includes_unsuccessful_batch_time(self):
        first, second = "a" * 32, "b" * 32
        results = [
            {"status": "private_preview_retained", "run_id": second, "preview_opens": 248,
             "opens_reduced": 2, "batch_seconds": 30, "opens_per_minute": 4.0},
            {"status": "no_improvement", "preview_opens": 248},
        ]
        messages = []
        with (patch.object(fast, "run_one", side_effect=results),
              patch.object(fast.time, "monotonic", side_effect=[100.0, 101.0, 160.0, 161.0, 189.0, 190.0])):
            result = fast.run_batches(Path("."), first, batches=2, items=80, timeout=180,
                                      progress=messages.append)
        self.assertEqual(result["total_opens_reduced"], 2)
        self.assertAlmostEqual(result["overall_opens_per_minute"], 4 / 3)
        self.assertTrue(any("including unsuccessful batches" in message for message in messages))
        self.assertEqual(self.record.call_args.args[1:], (second, 28.0))

    def test_failed_batch_records_its_elapsed_time_before_reporting_error(self):
        run_id = "a" * 32
        with (patch.object(fast, "run_one", side_effect=RuntimeError("router failed")),
              patch.object(fast.time, "monotonic", side_effect=[100.0, 101.0, 131.0])):
            with self.assertRaisesRegex(RuntimeError, "router failed"):
                fast.run_batches(Path("."), run_id, batches=1, items=80, timeout=180)
        self.assertEqual(self.record.call_args.args[1:], (run_id, 30.0))


if __name__ == "__main__":
    unittest.main()
