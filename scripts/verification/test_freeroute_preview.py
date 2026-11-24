"""test_freeroute_preview.py: tests of autorouting trials on private snapshots.

Check that autorouting trials cannot overwrite the live design.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import tempfile
import unittest
import zipfile
import hashlib
import subprocess
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pcbnew

from pcb import freeroute_preview


class FreerouteSnapshotTests(unittest.TestCase):
    """Tests that router trials run on private snapshots of the design."""

    def test_compiled_named_width_splits_only_its_net_and_inherits_pair_clearances(
        self,
    ):
        from core.kicad_sexpr import parse

        design = self.root / "widths.dsn"
        design.write_text(
            "(pcb x\n(unit um)\n(network "
            "(class POWER CONTROL LOAD (rule (width 500) (clearance 230)) "
            "(circuit (use_via V600))) "
            "(class SIGNAL S (rule (width 200) (clearance 200))) "
            "(class_class (classes POWER SIGNAL) (rule (clearance 350)))))"
        )
        freeroute_preview.DsnNetClasses.set_net_widths(
            design, {"CONTROL": 200_000, "LOAD": 500_000}
        )
        network = parse(design.read_text()).children("network")[0]
        classes = {item.atoms()[1].value: item for item in network.children("class")}
        load = next(
            item
            for item in classes.values()
            if "LOAD" in [a.value for a in item.atoms()[2:]]
        )
        control = next(
            item
            for item in classes.values()
            if "CONTROL" in [a.value for a in item.atoms()[2:]]
        )
        self.assertIsNot(load, control)
        self.assertEqual(
            load.children("rule")[0].children("width")[0].atoms()[1].value, "500"
        )
        self.assertEqual(
            control.children("rule")[0].children("width")[0].atoms()[1].value,
            "200.000000",
        )
        self.assertEqual(
            control.children("rule")[0].children("clearance")[0].atoms()[1].value, "230"
        )
        self.assertEqual(
            control.children("circuit")[0].children("use_via")[0].atoms()[1].value,
            "V600",
        )
        relationships = {
            tuple(a.value for a in item.children("classes")[0].atoms()[1:])
            for item in network.children("class_class")
        }
        self.assertEqual(
            relationships, {("POWER", "SIGNAL"), (control.atoms()[1].value, "SIGNAL")}
        )

    def test_compiled_width_uses_trunk_bound_not_local_escape_and_preserves_unknown_nets(
        self,
    ):
        from pcb.net_routing.rules import RoutingRules
        from verification.test_route_optimizer import RouteOptimizerTests

        source = RouteOptimizerTests.make_board(self.project)
        board = pcbnew.LoadBoard(str(source))
        rules_path = source.with_suffix(".kicad_dru")
        rules_path.write_text("""(version 1)
          (rule "trunk" (condition "A.NetName == 'SIGNAL'") (constraint track_width (min 0.25mm)))
          (rule "escape" (condition "A.NetName == 'SIGNAL' && A.intersectsCourtyard('J1')")
            (constraint track_width (min 0.15mm)))""")
        rules = RoutingRules.from_board(board, rules_path)
        self.assertEqual(rules.profile("SIGNAL", 0, 0).maximum_width, 250_000)
        self.assertEqual(
            freeroute_preview.DsnNetClasses.compiled_router_widths(board, rules_path),
            {"SIGNAL": 250_000},
        )
        self.assertEqual(
            freeroute_preview.DsnNetClasses.compiled_router_widths(
                board, rules_path, ignore_nets=("SIGNAL",)
            ),
            {},
        )

    def test_pass_budget_allows_negotiation_without_an_unbounded_outer_loop(self):
        for seconds, expected in (
            (1, 1),
            (14, 1),
            (30, 2),
            (60, 4),
            (120, 8),
            (600, 8),
        ):
            self.assertEqual(freeroute_preview.routing_pass_budget(seconds), expected)
        for invalid in (True, 0, -1, 1.5, "60"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                freeroute_preview.routing_pass_budget(invalid)

    @staticmethod
    def _export_test_dsn(_board, destination):
        """Write a minimal two-layer DSN file and report success.

        Inputs:
            _board: Ignored.
            destination: File that receives the DSN text.
        """
        Path(destination).write_text(
            "(pcb example (structure (layer F.Cu (type signal)) "
            "(layer B.Cu (type signal))))",
            encoding="utf-8",
        )
        return True

    def setUp(self):
        """Create a temporary project with board files, a library and a trial folder."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.board = self.project / "v4.1.kicad_pcb"
        for suffix in (".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_dru"):
            self.board.with_suffix(suffix).write_text(suffix, encoding="utf-8")
        library = self.project / "libraries" / "test.pretty"
        library.mkdir(parents=True)
        (library / "part.kicad_mod").write_text("footprint", encoding="utf-8")
        self.trial = self.root / "trial"
        self.trial.mkdir()

    def test_snapshot_is_exact_and_preserves_the_source(self):
        before = self.board.read_bytes()
        hashes = freeroute_preview._snapshot(self.board, self.trial)
        self.assertEqual(self.board.read_bytes(), before)
        self.assertEqual((self.trial / self.board.name).read_bytes(), before)
        self.assertIn("libraries/test.pretty/part.kicad_mod", hashes)
        self.assertEqual(
            hashes["v4.1.kicad_sch"],
            freeroute_preview._fingerprints(
                [self.project / "v4.1.kicad_sch"], self.project
            )["v4.1.kicad_sch"],
        )

    def test_rejects_trial_inside_the_live_project(self):
        with self.assertRaisesRegex(ValueError, "outside the project"):
            freeroute_preview._snapshot(self.board, self.project)

    def test_rejects_sources_that_change_during_copy(self):
        copy_inputs = freeroute_preview._copy_inputs

        def change_after_copy(files, source, destination):
            """Copy the inputs as usual, then rewrite the source file."""
            copy_inputs(files, source, destination)
            source.write_text("changed", encoding="utf-8")

        with patch.object(freeroute_preview, "_copy_inputs", change_after_copy):
            with self.assertRaisesRegex(RuntimeError, "changed while"):
                freeroute_preview._snapshot(self.board, self.trial)

    def test_rejects_missing_rules(self):
        self.board.with_suffix(".kicad_dru").unlink()
        with self.assertRaisesRegex(ValueError, "custom rules"):
            freeroute_preview._snapshot(self.board, self.trial)

    def test_resolves_simple_windows_java_shim_without_running_it(self):
        executable = self.root / "java.exe"
        executable.write_bytes(b"")
        wrapper = self.root / "java.cmd"
        wrapper.write_text(f'@echo off\n"{executable}" %*\n', encoding="utf-8")
        with patch.object(freeroute_preview.shutil, "which", return_value=str(wrapper)):
            self.assertEqual(
                freeroute_preview.JavaSelection.executable("java"), str(executable)
            )
        wrapper.write_text("echo unexpected command\n", encoding="utf-8")
        with patch.object(freeroute_preview.shutil, "which", return_value=str(wrapper)):
            with self.assertRaisesRegex(ValueError, "batch wrapper"):
                freeroute_preview.JavaSelection.executable("java")

    def test_uses_java_compatible_with_the_router(self):
        jar = self.root / "router.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr(
                "app/freerouting/Freerouting.class",
                bytes.fromhex("cafebabe00000045"),
            )
        old_java = self.root / "old" / "bin" / "java.exe"
        new_java = self.root / "new" / "bin" / "java.exe"
        for executable in (old_java, new_java):
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"")
            executable.chmod(0o755)
        with (
            patch.object(
                freeroute_preview.JavaSelection,
                "candidates",
                return_value=[str(old_java), str(new_java)],
            ),
            patch.object(
                freeroute_preview.JavaSelection, "version", side_effect=[21, 25]
            ),
        ):
            self.assertEqual(freeroute_preview._select_java("auto", jar), str(new_java))

    def test_incompatible_explicit_java_explains_the_requirement(self):
        jar = self.root / "router.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr(
                "app/freerouting/Freerouting.class",
                bytes.fromhex("cafebabe00000045"),
            )
        with (
            patch.object(
                freeroute_preview.JavaSelection, "executable", return_value="java.exe"
            ),
            patch.object(freeroute_preview.JavaSelection, "version", return_value=21),
        ):
            with self.assertRaisesRegex(ValueError, "Java 25.*Java 21"):
                freeroute_preview._select_java("java.exe", jar)

    def test_trial_drc_requires_full_native_parity_and_all_severities(self):
        report = {
            "unconnected_items": [{}],
            "violations": [],
            "schematic_parity": [],
            "included_severities": ["error", "warning", "exclusion"],
            "ignored_checks": [],
        }
        with patch.object(
            freeroute_preview, "_native_drc", return_value=report
        ) as check:
            counts = freeroute_preview.TrialChecks.drc(
                Path("kicad-cli"), self.board, self.root / "drc.json"
            )
        self.assertTrue(check.call_args.kwargs["schematic_parity"])
        self.assertEqual(counts["unconnected_items"], 1)
        self.assertEqual(counts["other_errors"], 0)
        self.assertEqual(counts["other_warnings"], 0)

    def test_dsn_router_settings_use_equal_costs_on_every_layer(self):
        design = self.root / "candidate.dsn"
        design.write_text(
            "(pcb example (structure (layer F.Cu (type signal)) "
            "(layer In1.Cu (type signal)) (layer In2.Cu (type signal)) "
            "(layer B.Cu (type signal)) (boundary (rect pcb 0 0 1 1))))",
            encoding="utf-8",
        )
        freeroute_preview.DsnStructure.set_equal_layer_costs(design)
        from core.kicad_sexpr import parse

        structure = parse(design.read_text(encoding="utf-8")).children("structure")[0]
        settings = structure.children("autoroute_settings")[0]
        self.assertLess(settings.start, structure.children("boundary")[0].start)
        rules = settings.children("layer_rule")
        self.assertEqual(
            [x.atoms()[1].value for x in rules], ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        )
        for rule in rules:
            self.assertEqual(rule.children("active")[0].atoms()[1].value, "on")
            for name in (
                "preferred_direction_trace_costs",
                "against_preferred_direction_trace_costs",
            ):
                self.assertEqual(float(rule.children(name)[0].atoms()[1].value), 1.0)
        self.assertEqual(settings.children("via_costs")[0].atoms()[1].value, "10")
        self.assertEqual(settings.children("plane_via_costs")[0].atoms()[1].value, "10")

    def test_dsn_removes_only_signal_layer_planes_backed_by_copper_zones(self):
        design = self.root / "candidate.dsn"
        design.write_text(
            '(pcb example (parser (string_quote ")) '
            "(structure (layer F.Cu (type signal)) (layer In1.Cu (type signal)) "
            "(layer In2.Cu (type power)) (layer B.Cu (type signal)) "
            "(plane GND (polygon In1.Cu 0 0 0 10 0 10 10)) "
            "(plane GND (polygon In2.Cu 0 0 0 10 0 10 10)) "
            "(plane OTHER (polygon B.Cu 0 0 0 10 0 10 10)) "
            "(keepout (polygon In1.Cu 0 1 1 2 1 2 2))))",
            encoding="utf-8",
        )
        removed = freeroute_preview.DsnStructure.strip_zone_planes(
            design, {("GND", "In1.Cu"), ("GND", "In2.Cu")}
        )
        text = design.read_text(encoding="utf-8")
        self.assertEqual(removed, (("GND", "In1.Cu"),))
        self.assertNotIn("(plane GND (polygon In1.Cu", text)
        self.assertIn("(plane GND (polygon In2.Cu", text)
        self.assertIn("(plane OTHER (polygon B.Cu", text)
        self.assertIn("(keepout (polygon In1.Cu", text)
        self.assertIn('(parser (string_quote "))', text)

    def test_dsn_plane_with_unsupported_geometry_fails_without_editing(self):
        design = self.root / "candidate.dsn"
        source = (
            "(pcb example (structure (layer F.Cu (type signal)) "
            "(plane GND (unsupported F.Cu 0 0))))"
        )
        design.write_text(source, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "plane"):
            freeroute_preview.DsnStructure.strip_zone_planes(design, {("GND", "F.Cu")})
        self.assertEqual(design.read_text(encoding="utf-8"), source)

    def test_dsn_zone_planes_are_collected_from_native_copper_zones(self):
        board = pcbnew.BOARD()
        board.SetCopperLayerCount(4)
        zone = pcbnew.ZONE(board)
        zone.SetLayer(pcbnew.In1_Cu)
        net = pcbnew.NETINFO_ITEM(board, "GND")
        board.Add(net)
        zone.SetNetCode(net.GetNetCode())
        board.Add(zone)
        rule_area = pcbnew.ZONE(board)
        rule_area.SetLayer(pcbnew.In2_Cu)
        rule_area.SetIsRuleArea(True)
        board.Add(rule_area)
        self.assertEqual(
            freeroute_preview.DsnStructure.native_zone_keys(board), {("GND", "In1.Cu")}
        )

    def test_prepared_router_dsn_removes_zone_planes_and_enables_equal_layer_costs(
        self,
    ):
        design = self.root / "candidate.dsn"
        board = pcbnew.BOARD()

        def export(_board, destination):
            """Write a three-layer DSN file with a GND plane on In1.Cu.

            Inputs:
                _board: Ignored.
                destination: File that receives the DSN text.
            Outputs:
                True.
            """
            Path(destination).write_text(
                "(pcb example (structure (layer F.Cu (type signal)) "
                "(layer In1.Cu (type signal)) (layer B.Cu (type signal)) "
                "(plane GND (polygon In1.Cu 0 0 0 10 0 10 10))))",
                encoding="utf-8",
            )
            return True

        with (
            patch.object(pcbnew, "ExportSpecctraDSN", side_effect=export),
            patch.object(
                freeroute_preview.DsnStructure,
                "native_zone_keys",
                return_value={("GND", "In1.Cu")},
            ),
        ):
            layers, removed = freeroute_preview._prepare_router_dsn(
                board, design, optimizer=False
            )
        self.assertEqual(layers, ("F.Cu", "In1.Cu", "B.Cu"))
        self.assertEqual(removed, (("GND", "In1.Cu"),))
        self.assertNotIn("(plane GND", design.read_text(encoding="utf-8"))
        self.assertIn(
            "(snap_angle fortyfive_degree)", design.read_text(encoding="utf-8")
        )

    def test_timed_out_checkpoint_must_be_complete_and_have_routing_data(self):
        session = self.root / "candidate.ses"
        self.assertFalse(
            freeroute_preview.RouterSessions.complete_fastroute_session(session)
        )
        for malformed in (
            "(session candidate (routes",
            "(session candidate (routes))",
            "(other candidate (base_design candidate) (routes (network_out)))",
        ):
            session.write_text(malformed, encoding="utf-8")
            self.assertFalse(
                freeroute_preview.RouterSessions.complete_fastroute_session(session)
            )
        session.write_text(
            "(session candidate (base_design candidate) (routes (network_out)))",
            encoding="utf-8",
        )
        self.assertTrue(
            freeroute_preview.RouterSessions.complete_fastroute_session(session)
        )

    def test_dsn_optimizer_setting_matches_the_requested_mode(self):
        import inspect
        from core.kicad_sexpr import parse

        self.assertIn(
            "optimizer",
            inspect.signature(
                freeroute_preview.DsnStructure.set_equal_layer_costs
            ).parameters,
        )
        design = self.root / "candidate.dsn"
        source = (
            "(pcb example (structure (layer F.Cu (type signal)) "
            "(layer B.Cu (type signal)) (boundary (rect pcb 0 0 1 1))))"
        )
        for enabled in (False, True):
            with self.subTest(optimizer=enabled):
                design.write_text(source, encoding="utf-8")
                freeroute_preview.DsnStructure.set_equal_layer_costs(
                    design, optimizer=enabled
                )
                settings = parse(design.read_text(encoding="utf-8")).children(
                    "structure"
                )[0]
                settings = settings.children("autoroute_settings")[0]
                self.assertEqual(
                    settings.children("postroute")[0].atoms()[1].value,
                    "on" if enabled else "off",
                )

    def test_dsn_rejects_existing_router_settings_instead_of_hiding_them(self):
        design = self.root / "candidate.dsn"
        design.write_text(
            "(pcb example (structure (layer F.Cu (type signal)) "
            "(layer B.Cu (type signal)) (autoroute_settings (vias off)) "
            "(boundary (rect pcb 0 0 1 1))))",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "router settings"):
            freeroute_preview.DsnStructure.set_equal_layer_costs(design)

    def test_dsn_accepts_specctra_string_quote_directive(self):
        design = self.root / "candidate.dsn"
        design.write_text(
            '(pcb example\n  (parser (string_quote "))\n'
            "  (structure (layer F.Cu (type signal)) (layer B.Cu (type signal)) "
            "(boundary (rect pcb 0 0 1 1)))\n)",
            encoding="utf-8",
        )
        freeroute_preview.DsnStructure.set_equal_layer_costs(design)
        result = design.read_text(encoding="utf-8")
        self.assertIn('(parser (string_quote "))', result)
        self.assertIn("(snap_angle fortyfive_degree)", result)

    def test_checks_the_router_used_the_requested_layer_costs(self):
        log = self.root / "router.log"
        log.write_text(
            "DEBUG Via Costs: 10, Plane Via Costs: 10\n"
            "DEBUG Layer Costs (Preferred / Against):\n"
            "DEBUG   Layer 1: 1.0 / 1.0\n"
            "DEBUG   Layer 2: 1.0 / 1.0\n",
            encoding="utf-8",
        )
        freeroute_preview.TrialChecks.verify_reported_costs(log, 2)
        log.write_text(
            log.read_text().replace("Layer 2: 1.0 / 1.0", "Layer 2: 1.8 / 1.0")
        )
        with self.assertRaisesRegex(RuntimeError, "layer costs"):
            freeroute_preview.TrialChecks.verify_reported_costs(log, 2)
        log.write_text("No routing cost summary\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "layer costs"):
            freeroute_preview.TrialChecks.verify_reported_costs(log, 2)

    def test_deferred_default_class_gets_a_temporary_router_alias(self):
        design = self.root / "candidate.dsn"
        design.write_text(
            '(pcb example (parser (string_quote ")) '
            "(network (class kicad_default A B (rule (width 200))) "
            "(class GROUND GND (rule (width 500)))))",
            encoding="utf-8",
        )
        names = freeroute_preview._alias_deferred_default(
            design, ("GROUND", "kicad_default")
        )
        result = design.read_text(encoding="utf-8")
        self.assertEqual(names, ("GROUND", "AUTOPCB_DEFER_DEFAULT"))
        self.assertIn("(class AUTOPCB_DEFER_DEFAULT A B (rule (width 200)))", result)
        self.assertIn("(class GROUND GND (rule (width 500)))", result)

    def test_deferred_nets_get_temporary_classes_with_their_original_rules(self):
        design = self.root / "candidate.dsn"
        design.write_text(
            '(pcb example (parser (string_quote ")) (network '
            '(class POWER A "B-net" C (circuit (use_via V1)) (rule (width 500))) '
            "(class SIGNAL D (rule (width 200)))))",
            encoding="utf-8",
        )
        names = freeroute_preview._defer_selected_nets(design, ("B-net", "D"), ())
        start, network = freeroute_preview._dsn_scope(
            design.read_text(encoding="utf-8"), "network"
        )
        del start
        classes = {form.atoms()[1].value: form for form in network.children("class")}
        self.assertEqual(names, ("AUTOPCB_DEFER_NET_1", "SIGNAL"))
        self.assertEqual([a.value for a in classes["POWER"].atoms()[2:]], ["A", "C"])
        deferred = classes["AUTOPCB_DEFER_NET_1"]
        self.assertEqual([a.value for a in deferred.atoms()[2:]], ["B-net"])
        self.assertEqual(
            deferred.children("rule")[0].children("width")[0].atoms()[1].value, "500"
        )
        self.assertEqual(
            deferred.children("circuit")[0].children("use_via")[0].atoms()[1].value,
            "V1",
        )

    def test_rework_width_floors_raise_only_affected_temporary_classes(self):
        design = self.root / "candidate.dsn"
        design.write_text(
            "(pcb example\n (unit um)\n (network "
            "(class SIGNAL A B (circuit (use_via V1)) (rule (width 250) (clearance 200))) "
            "(class OTHER C (rule (width 500) (clearance 250))) "
            "(class_class (classes SIGNAL OTHER) (rule (clearance 300)))))",
            encoding="utf-8",
        )
        freeroute_preview.DsnNetClasses.enforce_width_floors(
            design, {"A": 400_000, "B": 350_000, "C": 100_000}
        )
        result = design.read_text(encoding="utf-8")
        self.assertIn("(rule (width 400.000000) (clearance 200))", result)
        self.assertIn("(class OTHER C (rule (width 500) (clearance 250)))", result)
        self.assertIn(
            "(class_class (classes SIGNAL OTHER) (rule (clearance 300)))", result
        )

    def test_rework_width_floors_fail_closed_on_missing_or_ambiguous_data(self):
        design = self.root / "candidate.dsn"
        design.write_text(
            "(pcb x\n (unit um)\n (network (class A net (rule (width 250))) "
            "(class B net (rule (width 200)))))",
            encoding="utf-8",
        )
        original = design.read_bytes()
        for floors in (
            {"missing": 200_000},
            {"net": 200_000},
            {"net": -1},
            {"net": True},
        ):
            with self.subTest(floors=floors), self.assertRaises(ValueError):
                freeroute_preview.DsnNetClasses.enforce_width_floors(design, floors)
            self.assertEqual(design.read_bytes(), original)

    def test_deferred_aliases_preserve_cross_class_clearance_rules(self):
        design = self.root / "candidate.dsn"
        design.write_text(
            "(pcb x (network "
            "(class CORE A B (rule (width 200))) "
            "(class FIELD C D (rule (width 250))) "
            "(class OTHER E (rule (width 300))) "
            "(class_class (classes CORE FIELD) (rule (clearance 255))) "
            "(class_class (classes CORE OTHER) (rule (clearance 300)))))",
            encoding="utf-8",
        )
        names = freeroute_preview._defer_selected_nets(design, ("B", "D"), ())
        self.assertEqual(names, ("AUTOPCB_DEFER_NET_1", "AUTOPCB_DEFER_NET_2"))
        _, network = freeroute_preview._dsn_scope(
            design.read_text(encoding="utf-8"), "network"
        )
        rules = {
            (
                tuple(atom.value for atom in item.children("classes")[0].atoms()[1:]),
                item.children("rule")[0].children("clearance")[0].atoms()[1].value,
            )
            for item in network.children("class_class")
        }
        self.assertEqual(
            rules,
            {
                (("CORE", "FIELD"), "255"),
                (("AUTOPCB_DEFER_NET_1", "FIELD"), "255"),
                (("CORE", "AUTOPCB_DEFER_NET_2"), "255"),
                (("AUTOPCB_DEFER_NET_1", "AUTOPCB_DEFER_NET_2"), "255"),
                (("CORE", "OTHER"), "300"),
                (("AUTOPCB_DEFER_NET_1", "OTHER"), "300"),
            },
        )

    def test_default_alias_updates_cross_class_references(self):
        design = self.root / "candidate.dsn"
        design.write_text(
            "(pcb x (network (class kicad_default A (rule (width 200))) "
            "(class FIELD B (rule (width 250))) "
            "(class_class (classes kicad_default FIELD) (rule (clearance 255)))))",
            encoding="utf-8",
        )
        freeroute_preview._alias_deferred_default(design, ("kicad_default",))
        _, network = freeroute_preview._dsn_scope(
            design.read_text(encoding="utf-8"), "network"
        )
        members = network.children("class_class")[0].children("classes")[0].atoms()[1:]
        self.assertEqual(
            [atom.value for atom in members], ["AUTOPCB_DEFER_DEFAULT", "FIELD"]
        )

    def test_alias_refuses_an_unreadable_cross_class_rule_without_writing(self):
        design = self.root / "candidate.dsn"
        design.write_text(
            "(pcb x (network (class CORE A B (rule (width 200))) "
            "(class_class (unexpected CORE) (rule (clearance 255)))))",
            encoding="utf-8",
        )
        original = design.read_bytes()
        with self.assertRaisesRegex(ValueError, "cross-class"):
            freeroute_preview._defer_selected_nets(design, ("B",), ())
        self.assertEqual(design.read_bytes(), original)

    def test_deferred_nets_reject_unknown_or_ambiguous_membership(self):
        design = self.root / "candidate.dsn"
        design.write_text(
            "(pcb x (network (class A net (rule (width 1))) "
            "(class B net (rule (width 2)))))",
            encoding="utf-8",
        )
        original = design.read_bytes()
        for names in (("missing",), ("net",)):
            with self.assertRaisesRegex(ValueError, "class membership"):
                freeroute_preview._defer_selected_nets(design, names, ())
            self.assertEqual(design.read_bytes(), original)

    def test_high_speed_reservations_come_from_board_net_classes(self):
        rules = (
            b'{"net_settings":{"netclass_patterns":'
            b'[{"netclass":"USB_DATA","pattern":"USB_*"}],'
            b'"netclass_assignments":{"ETH_P":"ETH_DATA"}}}'
        )
        native = MagicMock()
        native.GetNetsByName.return_value = {
            "USB_DP": 1,
            "USB_DM": 2,
            "ETH_P": 3,
            "OTHER": 4,
        }
        self.assertEqual(
            freeroute_preview.DsnNetClasses.high_speed_reservations(rules, native),
            ("ETH_P", "USB_DM", "USB_DP"),
        )

    def test_routing_session_omits_only_top_level_placement(self):
        helper = getattr(freeroute_preview.RouterSessions, "routing_only_session", None)
        self.assertTrue(callable(helper), "routing-only session filter is missing")
        source = (
            '(session "board" (base_design board) '
            '(placement (component "part (A)" (place R1 10 20 back 90))) '
            '(routes (network_out (net "placement" (wire (path F.Cu 10 0 0 5 5))))))'
        )
        result = helper(source)
        self.assertNotIn("(placement (component", result)
        self.assertIn("(base_design board)", result)
        self.assertIn('(net "placement" (wire (path F.Cu 10 0 0 5 5)))', result)
        self.assertEqual(helper(result), result)

    def test_routing_session_rejects_ambiguous_or_invalid_structure(self):
        helper = getattr(freeroute_preview.RouterSessions, "routing_only_session", None)
        self.assertTrue(callable(helper), "routing-only session filter is missing")
        for source in (
            "(session x (base_design x) (placement) (placement) (routes))",
            "(session x (base_design x) (placement)",
            "(board x (base_design x) (routes))",
            "(session x (base_design x) (routes) (routes))",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                helper(source)

    def test_only_one_nanometre_of_bottom_import_rounding_is_restored(self):
        board = pcbnew.BOARD()
        bottom = pcbnew.FOOTPRINT(board)
        bottom.SetReference("R1")
        bottom.SetLayer(pcbnew.B_Cu)
        bottom.SetPosition(pcbnew.VECTOR2I(4_000_000, 5_000_000))
        board.Add(bottom)
        original = freeroute_preview._positions(board)
        bottom.SetPosition(pcbnew.VECTOR2I(4_000_000, 5_000_001))
        self.assertEqual(
            len(freeroute_preview._restore_bottom_rounding(board, original)), 1
        )
        self.assertEqual(freeroute_preview._positions(board), original)

        bottom.SetPosition(pcbnew.VECTOR2I(4_000_000, 5_000_002))
        with self.assertRaisesRegex(RuntimeError, "footprint positions"):
            freeroute_preview._restore_bottom_rounding(board, original)

        bottom.SetPosition(pcbnew.VECTOR2I(4_000_000, 5_000_000))
        bottom.SetLayer(pcbnew.F_Cu)
        original = freeroute_preview._positions(board)
        bottom.SetPosition(pcbnew.VECTOR2I(4_000_000, 5_000_001))
        with self.assertRaisesRegex(RuntimeError, "footprint positions"):
            freeroute_preview._restore_bottom_rounding(board, original)

    def test_trial_routes_the_loaded_snapshot_and_never_saves_the_source(self):
        jar = self.root / "router.jar"
        jar.write_bytes(b"placeholder")
        native = MagicMock()
        native.GetFootprints.return_value = []
        native.GetTracks.return_value = []
        counts = {
            "unconnected_items": 1,
            "other_violations": 0,
            "schematic_parity_items": 0,
        }

        def route(command, **kwargs):
            """Stand in for the router process and write a session.

            Check the router environment and command-line options first.

            Inputs:
                command: Router command line.
                kwargs: Process options; the router log goes to ``stdout``.
            Outputs:
                A result with return code 0 after the session file named by ``-do`` is
                written.
            """
            self.assertEqual(
                kwargs["env"]["APPDATA"], str(self.trial / "router-home" / "appdata")
            )
            self.assertIn(f"-Duser.home={self.trial / 'router-home'}", command)
            self.assertIn("-da", command)
            self.assertIn("--router.fanout.enabled=false", command)
            self.assertIn("--router.automatic_neckdown=false", command)
            self.assertIn("--router.strict_drc=true", command)
            self.assertIn("--router.optimizer.max_threads=1", command)
            self.assertIn("--router.optimizer.enabled=false", command)
            self.assertIn("--logging.console.level=DEBUG", command)
            self.assertEqual(command[command.index("-inc") + 1], "GROUND")
            kwargs["stdout"].write(
                "DEBUG Via Costs: 10, Plane Via Costs: 10\n"
                "DEBUG Layer Costs (Preferred / Against):\n"
                "DEBUG   Layer 1: 1.0 / 1.0\n"
                "DEBUG   Layer 2: 1.0 / 1.0\n"
            )
            Path(command[command.index("-do") + 1]).write_text(
                "(session candidate (base_design candidate) (placement (component FOO)) (routes (network_out)))",
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0)

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    freeroute_preview.tempfile, "mkdtemp", return_value=str(self.trial)
                )
            )
            stack.enter_context(
                patch.object(freeroute_preview, "_select_java", return_value="java.exe")
            )
            check = stack.enter_context(
                patch.object(freeroute_preview.TrialChecks, "drc", return_value=counts)
            )
            stack.enter_context(
                patch.object(freeroute_preview.subprocess, "run", side_effect=route)
            )
            load = stack.enter_context(
                patch.object(pcbnew, "LoadBoard", return_value=native)
            )
            export = stack.enter_context(
                patch.object(
                    pcbnew, "ExportSpecctraDSN", side_effect=self._export_test_dsn
                )
            )
            settings = stack.enter_context(
                patch.object(
                    freeroute_preview.DsnStructure,
                    "set_equal_layer_costs",
                    return_value=("F.Cu", "B.Cu"),
                )
            )
            imported = stack.enter_context(
                patch.object(pcbnew, "ImportSpecctraSES", return_value=True)
            )
            save = stack.enter_context(
                patch.object(pcbnew, "SaveBoard", return_value=True)
            )
            before = self.board.read_bytes()
            result = freeroute_preview.preview(
                self.project,
                jar,
                "java",
                1,
                10,
                skip_optimizer=True,
                ignore_net_classes=("GROUND",),
            )

        trial_board = self.trial / self.board.name
        load.assert_called_once_with(str(trial_board))
        self.assertIs(export.call_args.args[0], native)
        settings.assert_called_once_with(self.trial / "candidate.dsn", optimizer=False)
        self.assertIs(imported.call_args.args[0], native)
        imported_text = Path(imported.call_args.args[1]).read_text(encoding="utf-8")
        self.assertNotIn("(placement", imported_text)
        self.assertIn("(network_out)", imported_text)
        save.assert_called_once_with(str(trial_board), native)
        self.assertEqual(self.board.read_bytes(), before)
        self.assertEqual(result["status"], "unapproved_trial_only")
        self.assertEqual(result["native_drc"], "checked")
        self.assertEqual(check.call_count, 2)

    def test_raw_batch_can_defer_checks_to_the_required_proposal_filter(self):
        jar = self.root / "router.jar"
        jar.write_bytes(b"placeholder")
        native = MagicMock()
        native.GetFootprints.return_value = []
        native.GetTracks.return_value = []

        def route(command, **_kwargs):
            """Stand in for the router process: write a session file and return code 0."""
            Path(command[command.index("-do") + 1]).write_text(
                "(session candidate (base_design candidate) (placement (component FOO)) (routes (network_out)))",
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0)

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    freeroute_preview.tempfile, "mkdtemp", return_value=str(self.trial)
                )
            )
            stack.enter_context(
                patch.object(freeroute_preview, "_select_java", return_value="java.exe")
            )
            check = stack.enter_context(
                patch.object(freeroute_preview.TrialChecks, "drc")
            )
            stack.enter_context(
                patch.object(freeroute_preview.subprocess, "run", side_effect=route)
            )
            stack.enter_context(
                patch.object(freeroute_preview.TrialChecks, "verify_reported_costs")
            )
            stack.enter_context(patch.object(pcbnew, "LoadBoard", return_value=native))
            stack.enter_context(
                patch.object(
                    pcbnew, "ExportSpecctraDSN", side_effect=self._export_test_dsn
                )
            )
            stack.enter_context(
                patch.object(
                    freeroute_preview.DsnStructure,
                    "set_equal_layer_costs",
                    return_value=("F.Cu", "B.Cu"),
                )
            )
            stack.enter_context(
                patch.object(pcbnew, "ImportSpecctraSES", return_value=True)
            )
            stack.enter_context(patch.object(pcbnew, "SaveBoard", return_value=True))
            result = freeroute_preview.preview(
                self.project, jar, "java", 1, 10, defer_native_checks=True
            )

        check.assert_not_called()
        self.assertEqual(result["status"], "unapproved_trial_only")
        self.assertEqual(result["native_drc"], "deferred_to_proposal_filter")

    def test_pad_neckdown_is_an_explicit_search_option_for_both_engines(self):
        router = self.root / "router.jar"
        router.write_bytes(b"test router")
        digest = hashlib.sha256(router.read_bytes()).hexdigest()
        native = MagicMock()
        native.GetFootprints.return_value = []
        native.GetTracks.return_value = []

        def route(command, **_kwargs):
            """Stand in for the router process and write a session.

            Check the neckdown, strictness and fanout options and log a version line.
            """
            self.assertIn("--router.automatic_neckdown=true", command)
            self.assertIn("--router.strict_drc=true", command)
            self.assertIn("--router.fanout.enabled=false", command)
            _kwargs["stdout"].write(
                "fastroute 0.1.14\nparsed input: 2 layers, 1 components\n"
            )
            Path(command[command.index("-do") + 1]).write_text(
                "(session candidate (base_design candidate) (routes (network_out)))",
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0)

        for engine in ("Freerouting", "fastroute"):
            trial = self.root / engine
            trial.mkdir()
            with (
                self.subTest(engine=engine),
                patch.object(freeroute_preview, "FASTROUTE_014_SHA256", digest),
                patch.object(
                    freeroute_preview.tempfile, "mkdtemp", return_value=str(trial)
                ),
                patch.object(
                    freeroute_preview, "_select_java", return_value="java.exe"
                ),
                patch.object(freeroute_preview.subprocess, "run", side_effect=route),
                patch.object(freeroute_preview.TrialChecks, "verify_reported_costs"),
                patch.object(pcbnew, "LoadBoard", return_value=native),
                patch.object(
                    pcbnew, "ExportSpecctraDSN", side_effect=self._export_test_dsn
                ),
                patch.object(
                    freeroute_preview.DsnStructure,
                    "set_equal_layer_costs",
                    return_value=("F.Cu", "B.Cu"),
                ),
                patch.object(pcbnew, "ImportSpecctraSES", return_value=True),
                patch.object(pcbnew, "SaveBoard", return_value=True),
            ):
                result = freeroute_preview.preview(
                    self.project,
                    router,
                    "java",
                    1,
                    10,
                    engine=engine,
                    defer_native_checks=True,
                    search_pad_neckdown=True,
                )
                self.assertTrue(result["search_pad_neckdown"])
                self.assertEqual(result["status"], "unapproved_trial_only")

        with self.assertRaisesRegex(ValueError, "search_pad_neckdown"):
            freeroute_preview.preview(
                self.project, router, "java", 1, 10, search_pad_neckdown="yes"
            )

    def test_fastroute_trial_is_pinned_and_runs_on_a_private_snapshot(self):
        binary = self.root / "fastroute.exe"
        binary.write_bytes(b"verified test executable")
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        native = MagicMock()
        native.GetFootprints.return_value = []
        native.GetTracks.return_value = []

        def route(command, **kwargs):
            """Stand in for the fast router and write a session.

            Check its item limit, start count and options and log a version line.
            """
            self.assertIn("--router.autorouter.max_items=80", command)
            self.assertIn("--multi-start=2", command)
            self.assertIn(
                "--router.autorouter.ignore_net_classes=GROUND,USB_DATA", command
            )
            self.assertIn("--router.strict_drc=true", command)
            self.assertIn("--router.automatic_neckdown=false", command)
            kwargs["stdout"].write(
                "fastroute 0.1.14\nparsed input: 4 layers, 1 components\n"
            )
            Path(command[command.index("-do") + 1]).write_text(
                "(session candidate (base_design candidate) (placement (component FOO)) (routes (network_out)))",
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0)

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(freeroute_preview, "FASTROUTE_014_SHA256", digest)
            )
            stack.enter_context(
                patch.object(
                    freeroute_preview.tempfile, "mkdtemp", return_value=str(self.trial)
                )
            )
            stack.enter_context(
                patch.object(freeroute_preview.subprocess, "run", side_effect=route)
            )
            stack.enter_context(patch.object(pcbnew, "LoadBoard", return_value=native))
            stack.enter_context(
                patch.object(
                    pcbnew, "ExportSpecctraDSN", side_effect=self._export_test_dsn
                )
            )
            stack.enter_context(
                patch.object(
                    freeroute_preview.DsnStructure,
                    "set_equal_layer_costs",
                    return_value=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
                )
            )
            stack.enter_context(
                patch.object(
                    freeroute_preview.DsnNetClasses,
                    "high_speed_reservations",
                    return_value=("USB_DM", "USB_DP"),
                )
            )
            reserved = stack.enter_context(
                patch.object(
                    freeroute_preview,
                    "_defer_selected_nets",
                    return_value=("USB_DATA",),
                )
            )
            stack.enter_context(
                patch.object(pcbnew, "ImportSpecctraSES", return_value=True)
            )
            stack.enter_context(patch.object(pcbnew, "SaveBoard", return_value=True))
            result = freeroute_preview.preview(
                self.project,
                binary,
                "auto",
                1,
                30,
                max_items=80,
                ignore_net_classes=("GROUND",),
                defer_native_checks=True,
                engine="fastroute",
                ignore_high_speed=True,
                starts=2,
            )
        self.assertEqual(result["engine"], "fastroute")
        self.assertIn("optimizer_status", result)
        self.assertEqual(result["optimizer_status"], "requested_not_observed")
        self.assertEqual(result["status"], "unapproved_trial_only")
        self.assertEqual(reserved.call_args.args[1], ("USB_DM", "USB_DP"))
        with self.assertRaisesRegex(ValueError, "verified 0.1.14"):
            freeroute_preview.preview(
                self.project, binary, "auto", 1, 30, engine="fastroute"
            )

    def test_timed_out_fastroute_uses_complete_checkpoint_only_as_a_trial(self):
        binary = self.root / "fastroute.exe"
        binary.write_bytes(b"verified test executable")
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        native = MagicMock()
        native.GetFootprints.return_value = []
        native.GetTracks.return_value = []

        def timeout(command, **kwargs):
            """Stand in for a fast router that times out after 30 s.

            It logs a version line and writes a complete session first.
            """
            kwargs["stdout"].write(
                "fastroute 0.1.14\nparsed input: 4 layers, 1 components\n"
            )
            Path(command[command.index("-do") + 1]).write_text(
                "(session candidate (base_design candidate) (routes (network_out)))",
                encoding="utf-8",
            )
            raise subprocess.TimeoutExpired(command, 30)

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(freeroute_preview, "FASTROUTE_014_SHA256", digest)
            )
            stack.enter_context(
                patch.object(
                    freeroute_preview.tempfile, "mkdtemp", return_value=str(self.trial)
                )
            )
            stack.enter_context(
                patch.object(freeroute_preview.subprocess, "run", side_effect=timeout)
            )
            stack.enter_context(patch.object(pcbnew, "LoadBoard", return_value=native))
            stack.enter_context(
                patch.object(
                    pcbnew, "ExportSpecctraDSN", side_effect=self._export_test_dsn
                )
            )
            stack.enter_context(
                patch.object(
                    freeroute_preview.DsnStructure,
                    "set_equal_layer_costs",
                    return_value=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
                )
            )
            imported = stack.enter_context(
                patch.object(pcbnew, "ImportSpecctraSES", return_value=True)
            )
            stack.enter_context(patch.object(pcbnew, "SaveBoard", return_value=True))
            before = self.board.read_bytes()
            result = freeroute_preview.preview(
                self.project,
                binary,
                "auto",
                1,
                30,
                defer_native_checks=True,
                engine="fastroute",
            )
        imported.assert_called_once()
        self.assertTrue(result["router_timed_out"])
        self.assertEqual(result["status"], "unapproved_trial_only")
        self.assertEqual(self.board.read_bytes(), before)

    def test_timed_out_fastroute_does_not_import_a_partial_checkpoint(self):
        binary = self.root / "fastroute.exe"
        binary.write_bytes(b"verified test executable")
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        native = MagicMock()
        native.GetFootprints.return_value = []
        native.GetTracks.return_value = []

        def timeout(command, **_kwargs):
            """Stand in for a router that times out after 30 s.

            It writes a truncated session first.
            """
            Path(command[command.index("-do") + 1]).write_text(
                "(session broken", encoding="utf-8"
            )
            raise subprocess.TimeoutExpired(command, 30)

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(freeroute_preview, "FASTROUTE_014_SHA256", digest)
            )
            stack.enter_context(
                patch.object(
                    freeroute_preview.tempfile, "mkdtemp", return_value=str(self.trial)
                )
            )
            stack.enter_context(
                patch.object(freeroute_preview.subprocess, "run", side_effect=timeout)
            )
            stack.enter_context(patch.object(pcbnew, "LoadBoard", return_value=native))
            stack.enter_context(
                patch.object(
                    pcbnew, "ExportSpecctraDSN", side_effect=self._export_test_dsn
                )
            )
            stack.enter_context(
                patch.object(
                    freeroute_preview.DsnStructure,
                    "set_equal_layer_costs",
                    return_value=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
                )
            )
            imported = stack.enter_context(
                patch.object(pcbnew, "ImportSpecctraSES", return_value=True)
            )
            with self.assertRaisesRegex(RuntimeError, "without a complete checkpoint"):
                freeroute_preview.preview(
                    self.project,
                    binary,
                    "auto",
                    1,
                    30,
                    defer_native_checks=True,
                    engine="fastroute",
                )
        imported.assert_not_called()

    def test_source_change_during_routing_rejects_session_before_import(self):
        jar = self.root / "router.jar"
        jar.write_bytes(b"placeholder")
        native = MagicMock()
        native.GetFootprints.return_value = []
        native.GetTracks.return_value = []

        def route(command, **_kwargs):
            """Stand in for the router process: write a session, then edit the board."""
            Path(command[command.index("-do") + 1]).write_text(
                "(session candidate (base_design candidate) (placement (component FOO)) (routes (network_out)))",
                encoding="utf-8",
            )
            self.board.write_text("newer edit", encoding="utf-8")
            return SimpleNamespace(returncode=0)

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    freeroute_preview.tempfile, "mkdtemp", return_value=str(self.trial)
                )
            )
            stack.enter_context(
                patch.object(freeroute_preview, "_select_java", return_value="java.exe")
            )
            stack.enter_context(
                patch.object(freeroute_preview.TrialChecks, "drc", return_value={})
            )
            stack.enter_context(
                patch.object(freeroute_preview.subprocess, "run", side_effect=route)
            )
            stack.enter_context(patch.object(pcbnew, "LoadBoard", return_value=native))
            stack.enter_context(
                patch.object(
                    pcbnew, "ExportSpecctraDSN", side_effect=self._export_test_dsn
                )
            )
            stack.enter_context(
                patch.object(
                    freeroute_preview.DsnStructure,
                    "set_equal_layer_costs",
                    return_value=("F.Cu", "B.Cu"),
                )
            )
            imported = stack.enter_context(
                patch.object(pcbnew, "ImportSpecctraSES", return_value=True)
            )
            with self.assertRaisesRegex(RuntimeError, "changed during routing"):
                freeroute_preview.preview(self.project, jar, "java", 1, 10)
        imported.assert_not_called()


if __name__ == "__main__":
    unittest.main()
