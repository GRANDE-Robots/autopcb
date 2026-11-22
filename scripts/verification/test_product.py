"""Regression coverage for portable project selection and the native product flow."""

from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from autopcb import cli, design, jobs
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
            root = Path(folder)
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
            root = Path(folder)
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
            root = Path(directory)
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
            project = ProjectPaths(Path(build["project"]))
            manifest = jobs.verify(self.native, project)
            self.assertGreater(manifest["native"]["open_connections"], 1)
            self.assertEqual(len(manifest["reference_screen"]["blockers"]), 2)
            self.assertEqual(jobs.inspect(self.native, project)["geometry"]["tracks"], [])


if __name__ == "__main__":
    unittest.main()
