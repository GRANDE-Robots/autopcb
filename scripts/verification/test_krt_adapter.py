"""test_krt_adapter.py: check the command and tool identity of the optional native-file
router.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pcbnew

from pcb.net_routing import krt_adapter


class KrtAdapterTests(unittest.TestCase):
    """Checks the command, tool identity and result handling of the KRT router adapter."""

    def test_application_control_refusal_is_not_reported_as_missing_python_packages(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "python.exe"
            executable.touch()
            (root / "VERSION").write_text("0.23.7")
            blocked = subprocess.CompletedProcess(
                [],
                1,
                "",
                'File "shapely/__init__.py", line 1\n'
                "ImportError: An Application Control policy has blocked this file.",
            )
            with (
                patch.object(
                    krt_adapter.subprocess, "run", return_value=blocked
                ) as probe,
                self.assertRaisesRegex(
                    RuntimeError, "blocked a native Python dependency"
                ) as raised,
            ):
                krt_adapter.KrtRouterProcess._python(root, str(executable))
            self.assertNotIn("KiCadRoutingTools native module", str(raised.exception))
            self.assertEqual(probe.call_count, 1)

    def test_narrowed_nets_require_a_complete_unambiguous_ledger(self):
        rows = [
            {
                "net": 7,
                "net_name": None,
                "kind": "track_width",
                "requested": 0.25,
                "delivered": 0.2,
                "count": 1,
            },
            {
                "net": 7,
                "net_name": "A",
                "kind": "track_width",
                "requested": 0.25,
                "delivered": 0.2,
                "count": 1,
            },
            {
                "net": 9,
                "net_name": "B",
                "kind": "via_diameter",
                "requested": 0.8,
                "delivered": 0.6,
                "count": 1,
            },
        ]

        def check(
            entries, *, targets=("A", "B"), codes=None, headline=None, **overrides
        ):
            """Resolve the narrowed nets from a log built around the given ledger.

            Inputs:
                entries: Narrowed rule entries, each with a net and a count.
                targets: Net names the router was asked to route.
                codes: The router's net codes mapped to net names, or None for codes 7
                    and 9 mapped to A and B.
                headline: Strict-size headline line, or None to build one from the
                    entries.
                overrides: Design rule fields that replace the generated ones.
            Outputs:
                The result of ``StrictSizeLog.strict_size_nets``.
            """
            rules = {
                "escalation_policy": "off",
                "fab_tier_escalations": 0,
                "fab_tier_contexts": [],
                "count": sum(item["count"] for item in entries),
                "nets": sorted({item["net"] for item in entries}),
                "narrowed": entries,
                **overrides,
            }
            log = (
                'JSON_SUMMARY: {"design_rules": {"narrowed": []}}\n'
                'JSON_SUMMARY_MIN: {"failed_single": ["A"]}\n'
                + (
                    headline
                    or f"--strict-sizes: {rules['count']} feature(s) on "
                    f"{len(rules['nets'])} net(s) delivered below\n"
                )
            )
            return krt_adapter.StrictSizeLog.strict_size_nets(
                log,
                targets,
                {"design_rules": rules},
                {7: "A", 9: "B"} if codes is None else codes,
            )

        self.assertEqual(check(rows), ("A", "B"))
        self.assertEqual(
            check(rows[:1]), ("A",)
        )  # The router's input map resolves an unnamed row.
        self.assertIsNone(check(rows, codes={7: "A", 11: "B"}))
        self.assertIsNone(check(rows, codes={7: "A", 9: "A"}))
        self.assertIsNone(check([*rows, {**rows[1], "net_name": "B"}]))
        self.assertIsNone(check([{**rows[1], "delivered": 0.3}]))
        self.assertIsNone(check(rows, count=4))
        self.assertIsNone(check(rows, nets=[7]))
        self.assertIsNone(check(rows, fab_tier_escalations=1))
        self.assertIsNone(check(rows, ceiling_lowered_classes=["FIELD"]))
        self.assertIsNone(check([{**rows[1], "kind": "clearance"}]))
        self.assertIsNone(check([{**rows[1], "delivered": float("nan")}]))
        self.assertIsNone(
            check(
                rows,
                headline="--strict-sizes: 4 feature(s) on 2 net(s) delivered below\n",
            )
        )
        self.assertIsNone(
            krt_adapter.StrictSizeLog.strict_size_nets(
                "JSON_SUMMARY: {}", ("A",), {}, {7: "A"}
            )
        )

    def test_allowlisted_release_binds_source_binary_and_embedded_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rust_router").mkdir()
            (root / "VERSION").write_text("0.23.8", encoding="utf8")
            (root / "rust_router" / "grid_router.pyd").write_bytes(
                b"trusted test binary"
            )
            import hashlib

            binary_hash = hashlib.sha256(b"trusted test binary").hexdigest()
            release = ("test-commit", binary_hash, "0.23.7")
            outputs = [MagicMock(stdout="test-commit\n"), MagicMock(stdout="")]
            with (
                patch.dict(krt_adapter.RELEASES, {"0.23.8": release}),
                patch.object(
                    krt_adapter.KrtInstallation,
                    "tool_fingerprint",
                    return_value="fingerprint",
                ),
                patch.object(krt_adapter.subprocess, "run", side_effect=outputs),
            ):
                self.assertEqual(
                    krt_adapter.KrtInstallation.validate_installation(root),
                    "fingerprint",
                )
            (root / "rust_router" / "grid_router.pyd").write_bytes(b"modified")
            with (
                patch.dict(krt_adapter.RELEASES, {"0.23.8": release}),
                patch.object(
                    krt_adapter.KrtInstallation,
                    "tool_fingerprint",
                    return_value="fingerprint",
                ),
                self.assertRaisesRegex(ValueError, "binary does not match"),
            ):
                krt_adapter.KrtInstallation.validate_installation(root)
            executable = root / "python.exe"
            executable.touch()
            with (
                patch.dict(krt_adapter.RELEASES, {"0.23.8": release}),
                patch.object(
                    krt_adapter.subprocess, "run", return_value=MagicMock(returncode=0)
                ) as run,
            ):
                self.assertEqual(
                    krt_adapter.KrtRouterProcess._python(root, str(executable)),
                    executable.resolve(),
                )
                self.assertEqual(run.call_args.args[0][-1], "0.23.7")
            (root / "VERSION").write_text("unlisted", encoding="utf8")
            with self.assertRaisesRegex(ValueError, "allowlist"):
                krt_adapter.KrtInstallation._release(root)

    def test_router_net_codes_uses_and_validates_the_routers_own_parser(self):
        executable, root, board = (
            Path("python"),
            Path("router"),
            Path("board.kicad_pcb"),
        )
        with patch.object(
            krt_adapter.subprocess,
            "run",
            return_value=MagicMock(returncode=0, stdout='{"37": "A", "42": "B"}'),
        ) as run:
            self.assertEqual(
                krt_adapter.KrtRouterProcess.router_net_codes(
                    executable, root, board, {}
                ),
                {37: "A", 42: "B"},
            )
        self.assertIn("extract_nets", run.call_args.args[0][3])
        for output in ('{"037": "A"}', '{"0": "A"}', '{"37": null}', "not json"):
            with (
                patch.object(
                    krt_adapter.subprocess,
                    "run",
                    return_value=MagicMock(returncode=0, stdout=output),
                ),
                self.assertRaises(ValueError),
            ):
                krt_adapter.KrtRouterProcess.router_net_codes(
                    executable, root, board, {}
                )

    def test_strict_size_rejection_is_distinguished_from_a_crash(self):
        log = (
            'JSON_SUMMARY_MIN: {"routed": 0, "failed_single": ["A", "B"]}\n'
            "  --strict-sizes: feature below requested size\n"
        )
        result = krt_adapter.StrictSizeLog.strict_size_rejection(log, 3, ("A", "B"))
        self.assertEqual(result["failed_nets"], ("A", "B"))
        self.assertEqual(result["status"], "strict_size_rejected")
        self.assertIsNone(
            krt_adapter.StrictSizeLog.strict_size_rejection(log, 1, ("A", "B"))
        )
        self.assertIsNone(
            krt_adapter.StrictSizeLog.strict_size_rejection(
                "--strict-sizes: crashed", 3, ("A",)
            )
        )
        self.assertIsNone(
            krt_adapter.StrictSizeLog.strict_size_rejection(
                "JSON_SUMMARY_MIN: {}", 3, ("A",)
            )
        )

    def test_command_preserves_rules_and_routes_on_all_four_equal_layers(self):
        command = krt_adapter.KrtRouterProcess.build_command(
            Path("python"),
            Path("router"),
            Path("input.kicad_pcb"),
            Path("candidate.kicad_pcb"),
            ("CAM_A", "CAM_B"),
        )
        self.assertEqual(
            command[command.index("--nets") + 1 : command.index("--layers")],
            ["CAM_A", "CAM_B"],
        )
        self.assertEqual(
            command[command.index("--layers") + 1 : command.index("--layer-costs")],
            ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
        )
        start = command.index("--layer-costs") + 1
        self.assertEqual(command[start : start + 4], ["1", "1", "1", "1"])
        self.assertEqual(command[command.index("--direction-preference-cost") + 1], "0")
        self.assertTrue(command[3].endswith("krt_entry.py"))
        self.assertEqual(command[command.index("--via-cost") + 1], "100")
        self.assertEqual(command[command.index("--turn-cost") + 1], "10000")
        self.assertEqual(command[command.index("--same-net-pad-clearance") + 1], "0.01")
        for flag in (
            "--no-fix-drc-settings",
            "--strict-sizes",
            "--keep-input-copper",
            "--no-stub-layer-swap",
        ):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--escalation") + 1], "off")
        for forbidden in (
            "--overwrite",
            "--relax-drc-severities",
            "--clearance-ceiling",
            "--swappable-nets",
            "--force-reroute",
        ):
            self.assertNotIn(forbidden, command)

    def test_differential_command_keeps_the_same_guards(self):
        command = krt_adapter.KrtRouterProcess.build_command(
            Path("python"),
            Path("router"),
            Path("input.kicad_pcb"),
            Path("candidate.kicad_pcb"),
            ("USB_DP", "USB_DM"),
            differential=True,
        )
        for flag in (
            "--no-gnd-vias",
            "--no-fix-drc-settings",
            "--strict-sizes",
            "--keep-input-copper",
        ):
            self.assertIn(flag, command)
        self.assertNotIn("--polarity-swap-nets", command)
        self.assertNotIn("--schematic-dir", command)
        start = command.index("--layer-costs") + 1
        self.assertEqual(command[start : start + 4], ["1", "1", "1", "1"])

    def test_strict_differential_rejection_is_still_discarded(self):
        log = (
            'JSON_SUMMARY: {"failed_diff_pairs": ["USB_D"]}\n'
            "--strict-sizes: feature below requested size\n"
        )
        result = krt_adapter.StrictSizeLog.strict_size_rejection(
            log, 3, ("USB_DP", "USB_DM")
        )
        self.assertEqual(result["status"], "strict_size_rejected")
        self.assertEqual(result["failed_nets"], ())
        malformed = (
            'JSON_SUMMARY: {"failed_diff_pairs": [1]}\n--strict-sizes: rejected\n'
        )
        self.assertIsNone(
            krt_adapter.StrictSizeLog.strict_size_rejection(
                malformed, 3, ("USB_DP", "USB_DM")
            )
        )

    def test_command_rejects_empty_duplicate_and_option_like_targets(self):
        for nets in ((), ("A", "A"), ("--overwrite",), ("",)):
            with self.subTest(nets=nets), self.assertRaises(ValueError):
                krt_adapter.KrtRouterProcess.build_command(
                    Path("python"), Path("router"), Path("in"), Path("out"), nets
                )

    def test_layer_costs_follow_the_saved_board_stack(self):
        command = krt_adapter.KrtRouterProcess.build_command(
            Path("python"),
            Path("router"),
            Path("in"),
            Path("out"),
            ("A",),
            layers=("F.Cu", "B.Cu"),
        )
        self.assertEqual(
            command[command.index("--layers") + 1 : command.index("--layer-costs")],
            ["F.Cu", "B.Cu"],
        )
        self.assertEqual(
            command[
                command.index("--layer-costs") + 1 : command.index(
                    "--direction-preference-cost"
                )
            ],
            ["1", "1"],
        )
        with self.assertRaisesRegex(ValueError, "copper layers"):
            krt_adapter.KrtRouterProcess.build_command(
                Path("python"),
                Path("router"),
                Path("in"),
                Path("out"),
                ("A",),
                layers=("F.Cu", "F.Cu"),
            )

    def test_optional_grid_changes_search_resolution_not_design_rules(self):
        command = krt_adapter.KrtRouterProcess.build_command(
            Path("python"), Path("router"), Path("in"), Path("out"), ("A",), grid_mm=0.2
        )
        self.assertEqual(command[command.index("--grid-step") + 1], "0.2")
        self.assertEqual(command[command.index("--escalation") + 1], "off")
        for invalid in (0, -0.1, float("nan"), True):
            with self.subTest(grid=invalid), self.assertRaises(ValueError):
                krt_adapter.KrtRouterProcess.build_command(
                    Path("python"),
                    Path("router"),
                    Path("in"),
                    Path("out"),
                    ("A",),
                    grid_mm=invalid,
                )

    def test_full_search_keeps_guards_and_enables_router_recovery(self):
        command = krt_adapter.KrtRouterProcess.build_command(
            Path("python"), Path("router"), Path("in"), Path("out"), ("A",), quick=False
        )
        self.assertNotIn("--quick", command)
        self.assertIn("--no-fix-drc-settings", command)
        self.assertIn("--keep-input-copper", command)
        self.assertEqual(command[command.index("--max-ripup") + 1], "0")
        self.assertEqual(command[command.index("--direction-preference-cost") + 1], "0")
        with self.assertRaisesRegex(ValueError, "quick"):
            krt_adapter.KrtRouterProcess.build_command(
                Path("python"),
                Path("router"),
                Path("in"),
                Path("out"),
                ("A",),
                quick="yes",
            )

    def test_tool_fingerprint_changes_with_source_or_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "py_router").mkdir()
            (root / "rust_router").mkdir()
            (root / "py_router" / "route.py").write_text("print(1)", encoding="utf8")
            (root / "rust_router" / "grid_router.pyd").write_bytes(b"binary")
            (root / "VERSION").write_text("0.23.7", encoding="utf8")
            before = krt_adapter.KrtInstallation.tool_fingerprint(root)
            (root / "py_router" / "route.py").write_text("print(2)", encoding="utf8")
            changed = krt_adapter.KrtInstallation.tool_fingerprint(root)
            self.assertNotEqual(before, changed)
            (root / "rust_router" / "grid_router.pyd").write_bytes(b"different")
            self.assertNotEqual(
                changed, krt_adapter.KrtInstallation.tool_fingerprint(root)
            )

    def test_private_proposal_preserves_input_and_is_not_preapproved(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            for suffix in (".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_dru"):
                (project / f"v4.1{suffix}").write_text(suffix, encoding="utf8")
            native = MagicMock()
            native.GetEnabledLayers.return_value.CuStack.return_value = (0, 2)
            native.GetLayerName.side_effect = {0: "F.Cu", 2: "B.Cu"}.get
            process = MagicMock()
            process.wait.return_value = 0

            def spawn(command, **_kwargs):
                """Stand in for Popen: write an unapproved candidate board."""
                Path(command[5]).write_bytes(b"unapproved candidate")
                return process

            with (
                patch.object(
                    krt_adapter.KrtInstallation,
                    "validate_installation",
                    return_value="tool",
                ),
                patch.object(
                    krt_adapter.KrtInstallation, "tool_fingerprint", return_value="tool"
                ),
                patch.object(
                    krt_adapter.KrtRouterProcess, "_python", return_value=Path("python")
                ),
                patch.object(pcbnew, "LoadBoard", return_value=native),
                patch.object(krt_adapter.subprocess, "Popen", side_effect=spawn),
            ):
                result = krt_adapter.KrtPreview.preview(
                    project, project, "auto", ("A",), 10
                )
            self.assertEqual(result["status"], "unapproved_trial_only")
            self.assertEqual(result["board"], b"unapproved candidate")
            self.assertEqual(
                (project / "v4.1.kicad_pcb").read_text(encoding="utf8"), ".kicad_pcb"
            )

    def test_strict_size_failure_discards_candidate_and_reports_failed_nets(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            for suffix in (".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_dru"):
                (project / f"v4.1{suffix}").write_text(suffix, encoding="utf8")
            native = MagicMock()
            native.GetEnabledLayers.return_value.CuStack.return_value = (0, 2)
            native.GetLayerName.side_effect = {0: "F.Cu", 2: "B.Cu"}.get
            process = MagicMock()
            process.wait.return_value = 3

            def spawn(command, **kwargs):
                """Stand in for Popen: write a candidate and a strict-size failure."""
                Path(command[5]).write_bytes(b"must not be published")
                kwargs["stdout"].write(
                    'JSON_SUMMARY_MIN: {"failed_single": ["A"], "routed": 0}\n'
                    "--strict-sizes: feature below requested size\n"
                )
                kwargs["stdout"].flush()
                return process

            with (
                patch.object(
                    krt_adapter.KrtInstallation,
                    "validate_installation",
                    return_value="tool",
                ),
                patch.object(
                    krt_adapter.KrtInstallation, "tool_fingerprint", return_value="tool"
                ),
                patch.object(
                    krt_adapter.KrtRouterProcess, "_python", return_value=Path("python")
                ),
                patch.object(pcbnew, "LoadBoard", return_value=native),
                patch.object(krt_adapter.subprocess, "Popen", side_effect=spawn),
            ):
                result = krt_adapter.KrtPreview.preview(
                    project, project, "auto", ("A",), 10
                )
            self.assertEqual(result["status"], "strict_size_rejected")
            self.assertEqual(result["failed_nets"], ("A",))
            self.assertNotIn("board", result)
            self.assertEqual(
                (project / "v4.1.kicad_pcb").read_text(encoding="utf8"), ".kicad_pcb"
            )

    def test_strict_size_salvage_is_explicit_and_remains_unapproved(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            for suffix in (".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_dru"):
                (project / f"v4.1{suffix}").write_text(suffix, encoding="utf8")
            native = MagicMock()
            native.GetEnabledLayers.return_value.CuStack.return_value = (0, 2)
            native.GetLayerName.side_effect = {0: "F.Cu", 2: "B.Cu"}.get
            # KiCad's in-memory codes need not equal the router's synthetic IDs.
            native.FindNet.side_effect = lambda name: MagicMock(
                GetNetCode=lambda: {"A": 107, "B": 109}[name]
            )
            process = MagicMock()
            process.wait.return_value = 3
            summary = {
                "failed_single": ["A"],
                "design_rules": {
                    "narrowed": [
                        {
                            "net": 7,
                            "net_name": "A",
                            "kind": "track_width",
                            "requested": 0.25,
                            "delivered": 0.2,
                            "count": 1,
                        }
                    ],
                    "count": 1,
                    "nets": [7],
                    "escalation_policy": "off",
                    "fab_tier_escalations": 0,
                    "fab_tier_contexts": [],
                },
            }

            def spawn(command, **kwargs):
                """Stand in for Popen: write a mixed candidate, summary and size log."""
                Path(command[5]).write_bytes(b"unapproved mixed candidate")
                Path(command[command.index("--json-out") + 1]).write_text(
                    json.dumps(summary), encoding="utf8"
                )
                kwargs["stdout"].write(
                    "JSON_SUMMARY: "
                    + json.dumps(summary)
                    + "\n--strict-sizes: 1 feature(s) on 1 net(s) delivered below\n"
                )
                kwargs["stdout"].flush()
                return process

            with (
                patch.object(
                    krt_adapter.KrtInstallation,
                    "validate_installation",
                    return_value="tool",
                ),
                patch.object(
                    krt_adapter.KrtInstallation, "tool_fingerprint", return_value="tool"
                ),
                patch.object(
                    krt_adapter.KrtRouterProcess, "_python", return_value=Path("python")
                ),
                patch.object(
                    krt_adapter.KrtRouterProcess,
                    "router_net_codes",
                    return_value={7: "A", 9: "B"},
                ),
                patch.object(pcbnew, "LoadBoard", return_value=native),
                patch.object(krt_adapter.subprocess, "Popen", side_effect=spawn),
            ):
                result = krt_adapter.KrtPreview.preview(
                    project, project, "auto", ("A", "B"), 10, salvage_strict=True
                )
            self.assertEqual(result["status"], "strict_size_rejected")
            self.assertEqual(result["narrowed_nets"], ("A",))
            self.assertEqual(result["salvage_board"], b"unapproved mixed candidate")
            self.assertNotIn("board", result)
            self.assertEqual(
                (project / "v4.1.kicad_pcb").read_text(encoding="utf8"), ".kicad_pcb"
            )

    def test_differential_preview_selects_private_differential_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            for suffix in (".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_dru"):
                (project / f"v4.1{suffix}").write_text(suffix, encoding="utf8")
            (project / "py_router").mkdir()
            (project / "py_router" / "route_diff.py").touch()
            native = MagicMock()
            native.GetEnabledLayers.return_value.CuStack.return_value = (0, 2)
            native.GetLayerName.side_effect = {0: "F.Cu", 2: "B.Cu"}.get
            process = MagicMock()
            process.wait.return_value = 0
            settings = {}

            def spawn(command, **kwargs):
                """Stand in for Popen: record the environment and write a candidate."""
                settings.update(kwargs["env"])
                Path(command[5]).write_bytes(b"unapproved pair candidate")
                return process

            with (
                patch.object(
                    krt_adapter.KrtInstallation,
                    "validate_installation",
                    return_value="tool",
                ),
                patch.object(
                    krt_adapter.KrtInstallation, "tool_fingerprint", return_value="tool"
                ),
                patch.object(
                    krt_adapter.KrtRouterProcess, "_python", return_value=Path("python")
                ),
                patch.object(pcbnew, "LoadBoard", return_value=native),
                patch.object(krt_adapter.subprocess, "Popen", side_effect=spawn),
            ):
                result = krt_adapter.KrtPreview.preview(
                    project,
                    project,
                    "auto",
                    ("USB_DP", "USB_DM"),
                    10,
                    differential=True,
                )
            self.assertEqual(settings["AUTOPCB_KRT_DIFFERENTIAL"], "1")
            self.assertEqual(result["status"], "unapproved_trial_only")
            self.assertEqual(
                (project / "v4.1.kicad_pcb").read_text(encoding="utf8"), ".kicad_pcb"
            )

    def test_timeout_stops_the_launched_process_and_returns_no_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            for suffix in (".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_dru"):
                (project / f"v4.1{suffix}").write_text(suffix, encoding="utf8")
            native = MagicMock()
            native.GetEnabledLayers.return_value.CuStack.return_value = (0, 2)
            native.GetLayerName.side_effect = {0: "F.Cu", 2: "B.Cu"}.get
            process = MagicMock()
            process.wait.side_effect = subprocess.TimeoutExpired("router", 1)
            with (
                patch.object(
                    krt_adapter.KrtInstallation,
                    "validate_installation",
                    return_value="tool",
                ),
                patch.object(
                    krt_adapter.KrtInstallation, "tool_fingerprint", return_value="tool"
                ),
                patch.object(
                    krt_adapter.KrtRouterProcess, "_python", return_value=Path("python")
                ),
                patch.object(pcbnew, "LoadBoard", return_value=native),
                patch.object(krt_adapter.subprocess, "Popen", return_value=process),
                patch.object(krt_adapter.KrtRouterProcess, "_stop_tree") as stop,
            ):
                result = krt_adapter.KrtPreview.preview(
                    project, project, "auto", ("A",), 1
                )
            stop.assert_called_once_with(process)
            self.assertEqual(result["status"], "search_timeout")
            self.assertIn("1 second", result["reason"])
            self.assertNotIn("board", result)
            self.assertEqual(
                (project / "v4.1.kicad_pcb").read_text(encoding="utf8"), ".kicad_pcb"
            )


if __name__ == "__main__":
    unittest.main()
