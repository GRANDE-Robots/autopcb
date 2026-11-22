"""Regression coverage for portable project selection and the native product flow."""

from hashlib import sha256
import json
import math
import os
from pathlib import Path

from core.compat import strict_zip
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from autopcb import cli, design, jobs, qualification
from autopcb.workspace import Workspace
from core.paths import config_path, data_path, project_storage, stored_result, support_input_name
from autopcb.native import Native
from core.project import PROFILE_NAME, ProjectPaths, SourceSnapshot, declared_high_speed
from pcb.route_validation import _copy_inputs, _inputs


class ProjectIntakeTests(unittest.TestCase):
    def test_explicit_selection_and_hierarchy_survive_a_private_copy(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as copied:
            root, stage = Path(folder), Path(copied)
            (root / "Names with spaces.kicad_pro").write_text("{}")
            (root / "Names with spaces.kicad_pcb").write_text("(kicad_pcb)")
            (root / "other.kicad_pcb").write_text("(kicad_pcb)")
            (root / "sheets").mkdir()
            child = root / "sheets" / "input.kicad_sch"
            child.write_text("(kicad_sch)")
            (root / "Names with spaces.kicad_sch").write_text(
                '(kicad_sch (sheet (property "Sheetfile" "sheets/input.kicad_sch")))'
            )
            with self.assertRaisesRegex(ValueError, "Multiple KiCad"):
                ProjectPaths(root)
            project = ProjectPaths(root / "Names with spaces.kicad_pro")
            source = SourceSnapshot.capture(project)
            self.assertIn("sheets/input.kicad_sch", source.hashes)
            _copy_inputs(_inputs(project.board), project.board, stage)
            (stage / "candidate.kicad_pcb").write_text("(kicad_pcb)")
            self.assertEqual(ProjectPaths(stage).board.name, project.board.name)
            self.assertEqual((stage / "sheets/input.kicad_sch").read_bytes(), child.read_bytes())
            child.write_text("(kicad_sch (paper A4))")
            with self.assertRaisesRegex(RuntimeError, "inputs changed"):
                source.verify_unchanged()

    def test_family_empty_pair_declaration_differs_from_unknown_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(declared_high_speed(root, {"GND"}))
            profile = root / PROFILE_NAME
            profile.write_text('{"schema_version":1,"high_speed_reviewed":true,"differential_pairs":[]}')
            self.assertEqual(declared_high_speed(root, {"GND"}), set())
            profile.write_text('{"schema_version":1,"high_speed_reviewed":true,"differential_pairs":[["P","N"]]}')
            self.assertEqual(declared_high_speed(root, {"/P", "/N", "GND"}), {"/P", "/N"})
            with self.assertRaisesRegex(ValueError, "Missing"):
                declared_high_speed(root, {"P", "/P", "N"})


class WorkspaceTests(unittest.TestCase):

    def test_route_exit_status_distinguishes_zero_opens_from_exhaustion(self):
        with mock.patch("signal.signal"), mock.patch("builtins.print"):
            for status, code in (("preview_zero_opens", 0), ("paused", 0), ("stalled", 2), ("interrupted", 2)):
                with self.subTest(status=status), mock.patch.object(cli, "_run", return_value={"status": status}):
                    self.assertEqual(cli.main(["route"]), code)
    @unittest.skipUnless(os.name == "nt", "Windows DLL search path")
    def test_frozen_cli_does_not_expose_bundle_libraries_to_kicad(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder) / "_internal"
            bundle.mkdir()
            system = str(Path(os.environ["SystemRoot"]) / "System32")
            env = {**os.environ, "PATH": os.pathsep.join((f'"{bundle}"', system)),
                   "PYTHONPATH": str(bundle / "engine"),
                   "QT_PLUGIN_PATH": str(bundle / "PySide6" / "plugins"), "QML2_IMPORT_PATH": system}
            probe = """
import ctypes, os, sys
sys.path.insert(0, sys.argv[3])
from autopcb.native import Native
sys._MEIPASS = sys.argv[1]
kernel = ctypes.windll.kernel32
assert kernel.SetDllDirectoryW(sys._MEIPASS)
directory = ctypes.create_unicode_buffer(32768)
assert kernel.GetDllDirectoryW(len(directory), directory)
Native.prepare_windows_cli()
assert not kernel.GetDllDirectoryW(len(directory), directory)
assert os.environ["PATH"] == sys.argv[2]
assert "PYTHONPATH" not in os.environ
assert "QT_PLUGIN_PATH" not in os.environ
assert os.environ["QML2_IMPORT_PATH"] == sys.argv[2]
"""
            result = subprocess.run([sys.executable, "-B", "-c", probe, str(bundle), system,
                                     str(Path(__file__).resolve().parents[1])],
                                    env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_windows_shortcut_uses_the_user_workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            user_data = Path(folder)
            selected = user_data / "Explicit workspace"
            launch = mock.Mock(return_value=0)
            desktop = types.SimpleNamespace(main=launch)
            with (mock.patch.object(cli.sys, "platform", "win32"),
                  mock.patch.object(cli.sys, "frozen", True, create=True),
                  mock.patch.dict(os.environ, {"LOCALAPPDATA": folder}),
                  mock.patch.dict(cli.sys.modules, {"autopcb.desktop": desktop}),
                  mock.patch("signal.signal")):
                self.assertEqual(cli.main(["desktop"]), 0)
                default = user_data / "AutoPCB" / "Workspace"
                self.assertTrue(default.is_dir())
                launch.assert_called_once_with(default, None, None, workspace=default)
                launch.reset_mock()
                self.assertEqual(cli.main(["--workspace", str(selected), "desktop"]), 0)
                launch.assert_called_once_with(selected, None, None, workspace=selected)
                self.assertFalse(selected.exists())
                launch.reset_mock()
                with mock.patch.object(cli.sys, "frozen", False):
                    self.assertEqual(cli.main(["desktop"]), 0)
                launch.assert_called_once_with(Path("."), None, None, workspace=Path("."))

    def test_proposal_defaults_and_native_project_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Workspace(Path(folder))
            workspace.data.mkdir()
            workspace.requirements.write_text('Two-layer I2C temperature sensor at 3.3 V')
            parse = cli.parser().parse_args
            arguments = ['--workspace', folder]
            proposal = cli._run(parse([*arguments, 'propose']))
            saved = workspace.results / 'proposals' / (proposal['proposal_id'] + '.json')
            self.assertEqual(json.loads(saved.read_text(encoding='utf-8')), proposal)
            self.assertEqual(cli._run(parse([*arguments, 'propose'])), proposal)
            self.assertEqual(cli._run(parse([*arguments, 'discover']))['entries'], [])

            first = workspace.project_output('Board A v1.0')
            first.mkdir()
            target = first / 'Board A v1.0.kicad_pro'
            target.write_text('{}')
            self.assertEqual(workspace.project(None).entry, target)
            self.assertEqual(cli._run(parse([*arguments, 'discover']))['entries'], [str(target)])
            second = workspace.project_output('Board B')
            second.mkdir()
            (second / 'Board B.kicad_pcb').write_text('(kicad_pcb)')
            with self.assertRaisesRegex(ValueError, 'Use --project'):
                workspace.project(None)
            self.assertEqual(workspace.project(target).entry, target)
            with self.assertRaisesRegex(ValueError, 'directory separators'):
                workspace.project_output('../other')

    def test_managed_families_isolate_roles_and_keep_old_runs_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            workspace = Workspace(root)
            title = "Control Board"
            entry = workspace.project_output(title) / (title + ".kicad_pro")
            peer = root / "imports" / (title + ".kicad_pcb")
            entry.parent.mkdir()
            storage = workspace.storage(entry)
            self.assertEqual(workspace.storage(entry.with_suffix(".kicad_sch")), storage)
            self.assertNotEqual(workspace.storage(peer).key, storage.key)
            self.assertEqual(storage.config, root / "config" / "projects" / title)
            self.assertEqual(storage.campaigns, root / "config" / "campaigns" / title)
            historical = entry.parent / "results" / "autoroute" / "run.json"
            historical.parent.mkdir(parents=True)
            historical.write_text("{}")
            with mock.patch.dict(os.environ, storage.environment(entry)):
                self.assertEqual(project_storage(entry.parent), storage)
                self.assertEqual(config_path(entry.parent, "autopcb.json"), storage.config / "autopcb.json")
                self.assertEqual(support_input_name(entry.parent, storage.data / "STACKUP_CURRENT.json"),
                                 Path("data/STACKUP_CURRENT.json"))
                self.assertEqual(stored_result(entry.parent, "autoroute", "run.json"), historical)
                self.assertIsNone(project_storage(peer.parent).workspace)

    def test_project_data_precedes_matching_historical_input(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            support = root / 'autopcb'
            legacy = support / 'design'
            current = support / 'data'
            legacy.mkdir(parents=True)
            current.mkdir()
            name = 'STACKUP_CURRENT.json'
            (legacy / name).write_text('{}')
            self.assertEqual(data_path(root, name), legacy / name)
            (current / name).write_text('{}')
            self.assertEqual(data_path(root, name), current / name)
            self.assertEqual(data_path(root, 'OTHER.json'), current / 'OTHER.json')

    def test_current_release_input_folder_owns_its_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            historical = root / "design" / "release_inputs"
            historical.mkdir(parents=True)
            manifest = historical / "INPUT_MANIFEST.json"
            manifest.write_text("{}", encoding="utf-8")
            self.assertEqual(data_path(root, "release_inputs", manifest.name), manifest)
            current = root / "data" / "release_inputs"
            current.mkdir(parents=True)
            self.assertEqual(data_path(root, "release_inputs", manifest.name), current / manifest.name)


class RequirementsTests(unittest.TestCase):
    def test_unknown_supply_unsupported_signal_and_unapproved_edits_block_generation(self):
        incomplete = design.propose("Two-layer temperature sensor")
        self.assertEqual(incomplete["status"], "blocked")
        self.assertTrue(incomplete["unresolved"])
        self.assertEqual(design.propose("Four-layer high-speed USB at 5 V")["status"], "blocked")
        proposal = design.propose("Two-layer I2C temperature sensor at 3.3 V")
        approval = proposal["proposal_id"]
        self.assertEqual(design.validated(proposal, approval)["layers"], 2)
        proposal["layers"] = 16
        with self.assertRaisesRegex(ValueError, "approval"):
            design.validated(proposal, approval)


class ReferenceRoutingBudgetTests(unittest.TestCase):
    def start_patch(self, patch):
        self.addCleanup(patch.stop)
        return patch.start()

    def setUp(self):
        self.case = {}
        self.baseline = mock.Mock()
        self.layout = self.start_patch(mock.patch.object(qualification, "Layout")).return_value
        self.apply = self.start_patch(mock.patch.object(qualification, "_exercise_apply"))
        self.verify = self.start_patch(mock.patch.object(jobs, "verify"))
        self.start_patch(mock.patch.object(SourceSnapshot, "capture", return_value=self.baseline))
        self.layout.compare.return_value = {
            "before": {"tracks": [], "footprints": [{"at": [10, 12]}]},
            "after": {"tracks": [], "footprints": [{"at": [10, 14]}]},
        }

    def route(self, rounds=5):
        initial = {"native": {"open_connections": 3}}
        return qualification.ReferenceRouting.run(mock.sentinel.native, mock.sentinel.project, mock.sentinel.storage,
                                              self.case, self.baseline, initial, rounds=rounds,
                                              seconds=10, memory_gib=2.0)[0]

    def test_fresh_pass_uses_remaining_rounds_and_reads_back_each_saved_result(self):
        self.layout.preview.side_effect = [
            {"run_id": "first", "attempted_rounds": 2, "best_preview_opens": 1},
            {"run_id": "second", "attempted_rounds": 1, "best_preview_opens": 0},
        ]
        self.apply.side_effect = [{"reported_opens": 1}, {"reported_opens": 0}]
        self.verify.side_effect = [
            {"native": {"open_connections": 1, "findings": [{"check": "unconnected_items"}]}},
            {"native": {"open_connections": 0, "findings": []}},
        ]
        self.assertEqual(self.route()["native"]["open_connections"], 0)
        self.assertEqual([c.kwargs["rounds"] for c in self.layout.preview.call_args_list], [5, 3])
        self.assertEqual([(p["starting_opens"], p["saved_opens"]) for p in self.case["routing_passes"]],
                         [(3, 1), (1, 0)])

    def test_stall_stops_even_when_it_spends_no_rounds(self):
        self.layout.preview.return_value = {"run_id": "blocked", "attempted_rounds": 0, "best_preview_opens": 3}
        self.assertEqual(self.route()["native"]["open_connections"], 3)
        self.layout.preview.assert_called_once()
        self.apply.assert_not_called()

    def test_invalid_budget_or_open_count_stops_before_apply(self):
        for attempted, opens in ((6, 0), (-1, 0), (True, 0), (1, -1)):
            with self.subTest(attempted=attempted, opens=opens):
                self.layout.preview.return_value = {"attempted_rounds": attempted, "best_preview_opens": opens}
                with self.assertRaisesRegex(RuntimeError, "invalid"):
                    self.route()
        self.apply.assert_not_called()

    def test_saved_board_disagreement_and_new_findings_stop_the_search(self):
        self.layout.preview.return_value = {"run_id": "trial", "attempted_rounds": 1, "best_preview_opens": 1}
        self.apply.return_value = {"reported_opens": 1}
        for opens, findings, reason in ((2, [], "disagrees"), (1, [{"check": "violations"}], "native finding")):
            with self.subTest(opens=opens, findings=findings):
                self.verify.return_value = {"native": {"open_connections": opens, "findings": findings}}
                with self.assertRaisesRegex(RuntimeError, reason):
                    self.route()


class ElectricalEvidenceTests(unittest.TestCase):
    def test_current_reference_findings_cannot_be_omitted_or_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = ProjectPaths(root / "Board.kicad_pcb")
            project.board.write_text("(kicad_pcb)", encoding="utf-8")
            scope = {"schema_version": 1, "high_speed_reviewed": True, "differential_pairs": [["P", "N"]]}
            profile = root / PROFILE_NAME
            review = root / "review.json"
            source = SourceSnapshot.capture(project)
            pass_check = {"status": "pass", "method": "Fixture measurements", "measurements": {"count": 1}}
            record = {"schema_version": 1, "sources": source.hashes, "policy_sha256": design.digest(scope),
                      "reviewer": "Fixture", "checks": {"electrical": pass_check,
                                                       "reference_copper": dict(pass_check)}}
            reference = {"pairs": [["P", "N"]], "findings": [{"id": "via_transition/current"}]}

            def evidence():
                review.write_text(json.dumps(record), encoding="utf-8")
                profile.write_text(json.dumps({**scope, "electrical_review": {
                    "file": review.name, "sha256": sha256(review.read_bytes()).hexdigest()}}), encoding="utf-8")
                return jobs.review_evidence(project, SourceSnapshot.capture(project), reference)

            self.assertIn("every current reference finding", evidence()[1])
            record["checks"]["reference_copper"]["findings"] = {"via_transition/old": "Reviewed launch"}
            self.assertIn("every current reference finding", evidence()[1])
            record["checks"]["reference_copper"]["findings"] = {"via_transition/current": "Reviewed launch"}
            self.assertIsNone(evidence()[1])
            project.board.write_text("(kicad_pcb (version 1))", encoding="utf-8")
            self.assertIn("exact sources", evidence()[1])


class NativeProductTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.native = Native.discover()
            cls.native.footprints()
        except (ValueError, RuntimeError, OSError) as error:
            raise unittest.SkipTest(f"Matched native toolchain unavailable: {error}") from error

    def test_native_compaction_moves_a_catalog_group_together_and_keeps_the_header_fixed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = design.catalog()
            recipe = catalog["designs"]["temperature"]
            group = recipe["compact_outline"]["movable_groups"][0]
            for ref in group:
                recipe["parts"][ref]["position"][0] += 11
            custom = root / "catalog.json"
            custom.write_text(json.dumps(catalog), encoding="utf-8")
            request = design.propose("Two-layer I2C temperature sensor at 3.3 V", catalog_file=custom)
            built = jobs.build(self.native, request, request["proposal_id"], root / "Sensor")
            self.assertEqual(built["outline"]["size_mm"], recipe["minimum_mm"])
            self.assertEqual(built["outline"]["moved_footprints"], sorted(group))
            project = ProjectPaths(Path(built["project"]))
            geometry = jobs.inspect(self.native, project)["geometry"]
            left, top = map(min, strict_zip(*geometry["outline"][0]))
            shifts = {}
            for fp in geometry["footprints"]:
                x, y = fp["at"]
                start_x, start_y, _ = recipe["parts"][fp["ref"]]["position"]
                shifts[fp["ref"]] = (round(x - left - start_x, 6), round(y - top - start_y, 6))
            self.assertEqual(shifts["J1"], (0, 0))
            self.assertEqual(len({shifts[ref] for ref in group}), 1)
            self.assertLess(shifts[group[0]][0], 0)
            findings = jobs.verify(self.native, project)["native"]["findings"]
            self.assertEqual({finding["check"] for finding in findings}, {"unconnected_items"})

    def test_reference_projects_build_and_export_as_drafts_with_live_opens(self):
        requirements = (
            ("Temperature Board", "Two-layer I2C temperature sensor at 3.3 V", 2),
            ("USB Board v1.0", "Four-layer full-speed Type-B USB breakout at 5 V", 4),
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, text, layers in requirements:
                with self.subTest(name=name):
                    proposal = design.propose(text)
                    build = jobs.build(self.native, proposal, proposal["proposal_id"], root / name)
                    project = ProjectPaths(Path(build["project"]))
                    original = SourceSnapshot.capture(project)
                    layout = jobs.inspect(self.native, project)["geometry"]
                    self.assertEqual(len(layout["copper_layers"]), layers)
                    self.assertEqual(build["outline"]["size_mm"], [28, 20] if layers == 2 else [30, 25])
                    self.assertEqual(build["outline"]["moved_footprints"], [])
                    findings = jobs.verify(self.native, project)
                    self.assertEqual({item["check"] for item in findings["native"]["findings"]},
                                     {"unconnected_items"})
                    self.assertGreater(findings["native"]["open_connections"], 0)
                    self.assertFalse(findings["design_checks_passed"])
                    screen = findings["reference_screen"]
                    if layers == 4:
                        self.assertEqual(build["pair_layout"]["status"], "seeded")
                        self.assertEqual(screen["reference_net"], "/GND")
                        self.assertEqual(len(screen["pairs"]), 1)
                        self.assertEqual(screen["blockers"], [])
                        self.assertEqual(screen["status"], "needs_review")
                        self.assertEqual(findings["native"]["open_connections"], 1)
                        self.assertTrue(all(item["type"] == "sampled_reference_gap" for item in screen["findings"]))
                        # Each gap is the In1.Cu clearance ring around a through-hole connector pin.
                        self.assertEqual(sorted(item["launch"] for item in screen["findings"]),
                                         ["J1.2", "J1.3", "J2.2", "J2.3"])
                        self.assertTrue(all(0 < item["unreferenced_mm"] < 1.5 for item in screen["findings"]))

                        pads = {(fp["ref"], pad["number"]): pad for fp in layout["footprints"] for pad in fp["pads"]}
                        flagged = []
                        for item in screen["findings"]:
                            span, = item["gap_spans"]
                            pin, = span["endpoint_pads"]
                            key = (pin["reference"], pin["pad"])
                            flagged.append(key)
                            self.assertEqual(item["positions_mm"], [span["position_mm"]])
                            self.assertEqual(item["missing_samples"], span["missing_samples"])
                            physical_pad = pads[key]
                            self.assertEqual(physical_pad["net"], item["net"])
                            self.assertTrue(physical_pad["contours"])
                            self.assertGreater(physical_pad["hole"]["width_mm"], 0)
                            probe = (span["position_mm"]["x"], span["position_mm"]["y"])
                            self.assertLess(math.dist(physical_pad["at"], probe),
                                            max(physical_pad["size"]) / 2 + proposal["pair_layout"]["clearance_mm"])
                        self.assertCountEqual(flagged, [("J1", "2"), ("J1", "3"), ("J2", "2"), ("J2", "3")])
                    else:
                        self.assertEqual(screen["status"], "not_applicable")
                    fabrication = root / (name + " fab")
                    with self.assertRaisesRegex(RuntimeError, "Manufacturing export blocked"):
                        jobs.export(self.native, project, fabrication, draft=False)
                    self.assertFalse(fabrication.exists())
                    report = jobs.export(self.native, project, fabrication, draft=True)
                    self.assertEqual(report["status"], "draft")
                    self.assertEqual(report["blockers"], findings["blockers"])
                    self.assertIn("evidence/reference.json", report["artifacts"])
                    self.assertEqual(report, json.loads((fabrication / "manifest.json").read_text(encoding="utf-8")))
                    for item, digest in report["artifacts"].items():
                        self.assertEqual(sha256((fabrication / item).read_bytes()).hexdigest(), digest)
                    original.verify_unchanged()

    def test_bottom_side_catalog_part_is_built_on_the_bottom_layer(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            spec = design.catalog()
            spec["designs"]["temperature"]["parts"]["R1"].update(position=[12, 5, 270], side="bottom")
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps(spec), encoding="utf-8")
            proposal = design.propose("Two-layer I2C temperature sensor at 3.3 V", catalog_file=catalog)
            build = jobs.build(self.native, proposal, proposal["proposal_id"], root / "Bottom Part")
            project = ProjectPaths(Path(build["project"]))
            sides = {fp["ref"]: fp["layer"] for fp in jobs.inspect(self.native, project)["geometry"]["footprints"]}
            self.assertEqual((sides["R1"], sides["R2"]), ("B.Cu", "F.Cu"))
            findings = jobs.verify(self.native, project)["native"]["findings"]
            self.assertEqual({item["check"] for item in findings}, {"unconnected_items"})

    def test_exhausted_pair_seed_preserves_the_unrouted_project(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            spec = design.catalog()
            spec["designs"]["usb"]["pair_layout"]["max_launch_mm"] = 0.01
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps(spec), encoding="utf-8")
            proposal = design.propose("Four-layer full-speed Type-B USB breakout at 5 V", catalog_file=catalog)
            build = jobs.build(self.native, proposal, proposal["proposal_id"], root / "USB Short Escapes")
            self.assertEqual(build["pair_layout"]["status"], "deferred")
            self.assertEqual(build["outline"]["mode"], "catalog_fallback")
            self.assertEqual(build["outline"]["size_mm"], proposal["size_mm"])
            project = ProjectPaths(Path(build["project"]))
            manifest = jobs.verify(self.native, project)
            self.assertGreater(manifest["native"]["open_connections"], 1)
            self.assertEqual(len(manifest["reference_screen"]["blockers"]), 2)
            self.assertEqual(jobs.inspect(self.native, project)["geometry"]["tracks"], [])

    def test_explicit_and_historical_outlines_preserve_the_requested_size(self):
        with tempfile.TemporaryDirectory() as folder:
            for source, label in (("35 mm by 25 mm", "Fixed"), ("", "Historical")):
                proposal = design.propose("Two-layer temperature sensor at 3.3 V " + source)
                if not source:
                    proposal.pop("compact_outline")
                    proposal.pop("proposal_id")
                    proposal["proposal_id"] = design.digest(proposal)
                build = jobs.build(self.native, proposal, proposal["proposal_id"], Path(folder) / label)
                self.assertEqual(build["outline"]["mode"], "fixed")
                self.assertEqual(build["outline"]["size_mm"], [35, 25])

    def test_native_compaction_moves_only_the_authorized_group(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            catalog = design.catalog()
            recipe = catalog["designs"]["temperature"]
            group, = recipe["compact_outline"]["movable_groups"]
            for ref in group:
                recipe["parts"][ref]["position"][0] += 10
            file = root / "catalog.json"
            file.write_text(json.dumps(catalog), encoding="utf-8")
            proposal = design.propose("Temperature sensor at 3.3 V", catalog_file=file)
            built = jobs.build(self.native, proposal, proposal["proposal_id"], root / "Moved group")
            self.assertEqual(built["outline"]["size_mm"], [28, 20])
            self.assertEqual(built["outline"]["moved_footprints"], sorted(group))
            geometry = jobs.inspect(self.native, ProjectPaths(Path(built["project"])))["geometry"]
            positions = {fp["ref"]: fp["at"] for fp in geometry["footprints"]}
            self.assertEqual(positions["J1"], [24, 26])
            for ref in group:
                for axis in (0, 1):
                    offset = positions[ref][axis] - positions["U1"][axis]
                    self.assertAlmostEqual(offset, recipe["parts"][ref]["position"][axis]
                                           - recipe["parts"]["U1"]["position"][axis])


if __name__ == "__main__":
    unittest.main()
