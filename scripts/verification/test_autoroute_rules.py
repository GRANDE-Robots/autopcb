"""Saved-rule, search/materialization and renewal consistency regressions."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.kicad_sexpr import parse
from core.pcb_io import net_classes
from pcb.net_routing.backend import route_payload
from pcb.net_routing.model import Budget, BudgetExhausted, Limits
from pcb.net_routing.recovery import RecoveryPolicy, next_stage, renew_queue
from pcb.net_routing.rules import Courtyard, RoutingRules, read_rules
from pcb.net_routing.runner import _split_failed
from pcb.net_routing.schedule import search_orders
from pcb.route_cli import campaign_changes
from pcb.route_optimizer import GridRouter, Obstacle, Point, Segment, mm
from verification import test_net_routing as fixtures
from verification import test_autoroute_recovery as recovery_fixture
from core.toolchain import kicad_cli


RULES = """(version 1)
(rule "general width" (condition "A.NetClass == 'SIGNAL'") (constraint track_width (min 0.20mm)))
(rule "power width" (condition "A.NetClass == 'POWER'") (constraint track_width (min 0.50mm)))
(rule "general clearance" (condition "A.Type == 'Track' || B.Type == 'Track'") (constraint clearance (min 0.20mm)))
(rule "local signal neck" (condition "A.Type == 'Track' && A.NetClass == 'SIGNAL' && (A.Layer == 'F.Cu' || A.Layer == 'B.Cu') && A.intersectsCourtyard('U1')") (constraint track_width (min 0.15mm)))
(rule "local clearance" (condition "A.Type == 'Track' && A.intersectsCourtyard('U1')") (constraint clearance (min 0.15mm)))
(rule "legacy group" (condition "A.memberOfGroup('OLD')") (constraint track_width (min 0.10mm)))
(rule "legacy coordinate" (condition "A.Origin_X == 2mm && A.End_X == 4mm") (constraint track_width (min 0.10mm)))
(rule "exact saved segment" (condition "A.Start_X == 2mm && A.Start_Y == 1mm && A.End_X == 4mm") (constraint track_width (min 0.10mm)))
"""


def compiler(source=RULES):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "sample.kicad_dru"
        path.write_text(source)
        rules = read_rules(path)
    courtyard = Courtyard(
        "U1", "F.Cu", tuple(Point.from_mm(p) for p in [(1, 1), (3, 1), (3, 3), (1, 3)])
    )
    return RoutingRules(
        rules,
        (courtyard,),
        {
            "Default": (mm(0.25), mm(0.2)),
            "SIGNAL": (mm(0.25), mm(0.2)),
            "POWER": (mm(0.5), mm(0.2)),
        },
        {"S": "SIGNAL", "P": "POWER"},
        mm(0.1),
        0,
    )


class RuleConsistencyTests(unittest.TestCase):
    def test_maximum_width_excludes_an_unrelated_power_rule(self):
        source = """(version 1)
        (rule "signal" (condition "A.NetClass == 'SIGNAL'")
          (constraint track_width (min 0.25mm)))
        (rule "power" (condition "A.NetClass == 'POWER'")
          (constraint track_width (min 6.00mm)))"""
        rules = compiler(source)
        self.assertEqual(rules.profile("S", mm(0.25), mm(0.2)).maximum_width, mm(0.25))
        self.assertEqual(rules.profile("P", mm(0.25), mm(0.2)).maximum_width, mm(6))

    def test_maximum_width_keeps_applicable_local_and_uncertain_rules(self):
        source = """(version 1)
        (rule "wide inside" (condition "A.NetClass == 'SIGNAL' && A.intersectsCourtyard('U1')")
          (constraint track_width (min 0.80mm)))
        (rule "wide outside" (condition "A.NetClass == 'SIGNAL' && !A.intersectsCourtyard('U1')")
          (constraint track_width (min 1.00mm)))"""
        profile = compiler(source).profile("S", mm(0.25), mm(0.2))
        self.assertGreaterEqual(profile.maximum_width, mm(1))
        for layer in ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu"):
            for x in (0.5, 1.1, 2.0, 3.1, 4.0):
                segment = Segment(
                    Point.from_mm((x, 1.5)), Point.from_mm((x, 2.5)), layer
                )
                self.assertLessEqual(profile.width_at(segment), profile.maximum_width)

    def test_clearance_reuses_track_courtyard_checks_across_obstacles(self):
        source = """(version 1)
        (rule "general" (condition "A.Type == 'Track'") (constraint clearance (min 0.20mm)))
        (rule "power" (condition "A.NetName == 'S' && B.NetName == 'P'")
          (constraint clearance (min 0.50mm)))
        (rule "local" (condition "A.NetName == 'S' && A.Type == 'Track' && A.intersectsCourtyard('U1')")
          (constraint clearance (min 0.30mm)))"""
        profile = compiler(source).profile("S", mm(0.25), mm(0.2))
        segment = fixtures.segment((4, 2), (5, 2))
        power = Obstacle(
            Point.from_mm((4, 4)), Point.from_mm((4.1, 4.1)), ("F.Cu",), "P"
        )
        ordinary = Obstacle(
            Point.from_mm((6, 4)), Point.from_mm((6.1, 4.1)), ("F.Cu",), "X"
        )
        with patch.object(
            profile, "intersecting_references", wraps=profile.intersecting_references
        ) as resolve:
            self.assertEqual(
                profile.clearance_at(segment, power, width=mm(0.25)), mm(0.5)
            )
            self.assertEqual(
                profile.clearance_at(segment, ordinary, width=mm(0.25)), mm(0.2)
            )
            self.assertEqual(resolve.call_count, 2)
            inside = fixtures.segment((2, 2), (2.5, 2))
            self.assertEqual(
                profile.clearance_at(inside, ordinary, width=mm(0.25)), mm(0.3)
            )
            self.assertEqual(resolve.call_count, 4)
            self.assertEqual(
                profile.clearance_at(segment, ordinary, width=mm(0.25)), mm(0.2)
            )
            self.assertEqual(resolve.call_count, 6)
            profile.clearance_at(segment, power, width=mm(0.30))
            self.assertEqual(resolve.call_count, 8)

    def test_cached_clearance_geometry_still_respects_changed_uncertainty(self):
        source = """(version 1)
        (rule "general" (condition "A.Type == 'Track'") (constraint clearance (min 0.20mm)))
        (rule "local" (condition "A.NetName == 'S' && A.Type == 'Track' && A.intersectsCourtyard('U1')")
          (constraint clearance (min 0.50mm)))"""
        rules = compiler(source)
        profile = rules.profile("S", mm(0.25), mm(0.2))
        segment = fixtures.segment((4, 2), (5, 2))
        obstacle = Obstacle(
            Point.from_mm((6, 4)), Point.from_mm((6.1, 4.1)), ("F.Cu",), "P"
        )
        self.assertEqual(
            profile.clearance_at(segment, obstacle, width=mm(0.25)), mm(0.2)
        )
        rules.uncertain_courtyards["F.Cu"] = frozenset({"U1"})
        self.assertEqual(
            profile.clearance_at(segment, obstacle, width=mm(0.25)), mm(0.5)
        )

    def test_board_edge_inset_respects_smaller_local_clearance(self):
        from pcb.net_routing.backend import Settings
        from pcb.net_routing.native import NativeNet
        from pcb.route_optimizer import COPPER_LAYERS

        net = fixtures.make_net("S", [(1, 1), (2, 2)])
        settings = Settings(
            {
                "bounds_mm": [0, 0, 10, 10],
                "step_mm": 0.05,
                "layers": ["F.Cu"],
                "width_mm": 0.25,
                "clearance_mm": 0.2,
            },
            NativeNet(net, (), (), False),
            {layer: mm(0.035) for layer in COPPER_LAYERS},
            (Point.from_mm((0, 0)), Point.from_mm((10, 10))),
            mm(0.5),
            compiler(),
        )
        self.assertEqual(settings.profile.minimum_clearance, mm(0.15))
        self.assertGreaterEqual(
            settings.lower.x + settings.profile.minimum_clearance, mm(0.5)
        )

    def test_staircase_crossing_existing_copper_is_rejected_during_search(self):
        old = (
            fixtures.segment((0, 0), (1, 1)),
            fixtures.segment((1, 1), (2, 1)),
            fixtures.segment((2, 1), (3, 1)),
        )
        new = fixtures.route(fixtures.segment((3, 1), (5, 3)))
        with self.assertRaisesRegex(ValueError, "staircase"):
            fixtures.combine(new, existing=old)

    def test_old_unrelated_staircase_is_not_rejected_and_real_branch_stops_chain(self):
        old = (
            fixtures.segment((0, 0), (1, 1)),
            fixtures.segment((1, 1), (3, 1)),
            fixtures.segment((3, 1), (5, 3)),
        )
        self.assertTrue(
            fixtures.combine(
                fixtures.route(fixtures.segment((20, 20), (22, 20))), existing=old
            ).segments
        )
        branch = (*old[:2], fixtures.segment((3, 1), (3, -1)))
        self.assertTrue(
            fixtures.combine(fixtures.route(old[2]), existing=branch).segments
        )

    def test_wrapped_native_keys_are_normalized(self):
        class NativeName:
            def __str__(self):
                return "POWER"

        value = object()
        board = Mock()
        board.GetAllNetClasses.return_value = {NativeName(): value}
        self.assertIs(net_classes(board)["POWER"], value)

    def test_local_neck_restores_trunk_width_at_actual_boundary(self):
        profile = compiler().profile("S", mm(0.25), mm(0.2))
        segment = fixtures.segment((2, 2), (5, 2))
        pieces = profile.pieces(segment)
        self.assertEqual([width for _, width in pieces], [mm(0.15), mm(0.25)])
        self.assertEqual(pieces[0][0].end, Point.from_mm([3, 2]))
        self.assertEqual(pieces[1][0].start, pieces[0][0].end)
        outside = fixtures.segment((4, 4), (5, 4))
        self.assertEqual(profile.width_at(outside), mm(0.25))
        self.assertEqual(
            compiler().profile("P", mm(0.25), mm(0.2)).width_at(outside), mm(0.5)
        )

    def test_board_minimum_clearance_is_in_search_envelope(self):
        source = RoutingRules(
            (), (), {"Default": (mm(0.2), mm(0.2))}, {}, mm(0.2), mm(0.5)
        )
        profile = source.profile("S", mm(0.2), mm(0.2))
        obstacle = Obstacle(
            Point.from_mm((1.45, 2.05)),
            Point.from_mm((1.55, 2.10)),
            ("F.Cu",),
            "T",
        )
        model = GridRouter(
            Point.from_mm((0, 0)),
            Point.from_mm((8, 8)),
            mm(0.1),
            ("F.Cu",),
            "S",
            mm(0.2),
            mm(0.2),
            mm(0.6),
            mm(0.3),
            (obstacle,),
            profile=profile,
        )
        first, last = Point.from_mm((1, 1.6)), Point.from_mm((2, 1.6))
        self.assertEqual(
            profile.clearance_at(Segment(first, last, "F.Cu"), obstacle), mm(0.5)
        )
        self.assertGreaterEqual(profile.maximum_clearance, mm(0.5))
        self.assertFalse(model.edge_clear(first, last, "F.Cu"))

    def test_saved_local_exceptions_override_defaults_but_not_explicit_floors(self):
        rules = compiler()
        rules.minimum_width = mm(0.20)
        rules.minimum_clearance = mm(0.18)
        profile = rules.profile("S", mm(0.25), mm(0.20))
        segment = fixtures.segment((1.5, 2), (2.5, 2))
        obstacle = Obstacle(
            Point.from_mm((4, 4)), Point.from_mm((4.1, 4.1)), ("F.Cu",), "P", "copper"
        )
        self.assertEqual(profile.width_at(segment), mm(0.15))
        self.assertLessEqual(profile.minimum_width, mm(0.15))
        self.assertEqual(profile.minimum_width_for("F.Cu"), mm(0.15))
        self.assertEqual(profile.clearance_at(segment, obstacle), mm(0.15))
        self.assertLessEqual(profile.minimum_clearance, mm(0.15))
        self.assertEqual(profile.minimum_clearance_for("F.Cu", obstacle), mm(0.15))
        outside = fixtures.segment((4, 4), (5, 4))
        self.assertGreaterEqual(profile.width_at(outside), mm(0.20))
        self.assertGreaterEqual(profile.clearance_at(outside, obstacle), mm(0.18))
        self.assertGreaterEqual(
            profile.width_at(Segment(segment.start, segment.end, "In1.Cu")), mm(0.20)
        )

        bounded = rules.profile(
            "S", mm(0.25), mm(0.20), width_floor=mm(0.22), clearance_floor=mm(0.19)
        )
        self.assertGreaterEqual(bounded.width_at(segment), mm(0.22))
        self.assertGreaterEqual(bounded.minimum_width_for("F.Cu"), mm(0.22))
        self.assertGreaterEqual(bounded.clearance_at(segment, obstacle), mm(0.19))
        self.assertGreaterEqual(
            bounded.minimum_clearance_for("F.Cu", obstacle), mm(0.19)
        )

    def test_native_drc_confirms_scoped_exceptions_to_board_defaults(self):
        import json
        import pcbnew
        from pcb.route_validation import _drc

        with tempfile.TemporaryDirectory(prefix="autopcb-local-rule-") as folder:
            root = Path(folder)
            path = root / "probe.kicad_pcb"
            board = pcbnew.BOARD()
            board.SetCopperLayerCount(6)
            for name in ("S", "OTHER"):
                board.Add(pcbnew.NETINFO_ITEM(board, name))
            footprint = pcbnew.FOOTPRINT(board)
            footprint.SetReference("U16")
            board.Add(footprint)
            corners = ((2, 2), (8, 2), (8, 8), (2, 8))
            for a, b in zip(corners, (*corners[1:], corners[0])):
                line = pcbnew.PCB_SHAPE(footprint)
                line.SetShape(pcbnew.SHAPE_T_SEGMENT)
                line.SetLayer(pcbnew.F_CrtYd)
                line.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1])))
                line.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
                line.SetWidth(mm(0.05))
                footprint.Add(line)
            tracks = {}
            for name, y, layer in (
                ("S", 5, pcbnew.F_Cu),
                ("OTHER", 5.3, pcbnew.F_Cu),
                ("S", 15, pcbnew.F_Cu),
                ("OTHER", 15.3, pcbnew.F_Cu),
                ("S", 6, pcbnew.In1_Cu),
            ):
                track = pcbnew.PCB_TRACK(board)
                track.SetNet(board.FindNet(name))
                track.SetStart(pcbnew.VECTOR2I(mm(3), mm(y)))
                track.SetEnd(pcbnew.VECTOR2I(mm(7), mm(y)))
                track.SetWidth(mm(0.15))
                track.SetLayer(layer)
                board.Add(track)
                tracks[y] = track.m_Uuid.AsString()
            pcbnew.SaveBoard(str(path), board)
            path.with_suffix(".kicad_pro").write_text(
                json.dumps(
                    {
                        "board": {
                            "design_settings": {
                                "rules": {"min_track_width": 0.2, "min_clearance": 0.2}
                            }
                        }
                    }
                )
            )
            path.with_suffix(".kicad_dru").write_text("""(version 1)
              (rule "Outer package neck"
                (condition "A.Type == 'Track' && A.Layer == 'F.Cu' && A.intersectsCourtyard('U16')")
                (constraint track_width (min 0.15mm)))
              (rule "Outer package spacing"
                (condition "A.Layer == 'F.Cu' && B.Layer == 'F.Cu' && (A.intersectsCourtyard('U16') || B.intersectsCourtyard('U16'))")
                (constraint clearance (min 0.15mm)))""")
            report = _drc(kicad_cli(), path, root / "drc.json")
            width_ids = {
                part.get("uuid")
                for item in report["violations"]
                if item.get("type") == "track_width"
                for part in item.get("items", ())
            }
            self.assertFalse({tracks[5], tracks[5.3]} & width_ids, report)
            self.assertTrue({tracks[15], tracks[15.3], tracks[6]} <= width_ids, report)
            clearance_pairs = [
                {part.get("uuid") for part in item.get("items", ())}
                for item in report["violations"]
                if item.get("type") == "clearance"
            ]
            self.assertFalse(
                any({tracks[5], tracks[5.3]} <= pair for pair in clearance_pairs),
                report,
            )
            self.assertTrue(
                any({tracks[15], tracks[15.3]} <= pair for pair in clearance_pairs),
                report,
            )
            native = pcbnew.LoadBoard(str(path))
            rules = RoutingRules.from_board(native, path.with_suffix(".kicad_dru"))
            profile = rules.profile("S", mm(0.15), 0)
            inside = fixtures.segment((3, 5), (7, 5))
            self.assertEqual(profile.width_at(inside), mm(0.15))
            self.assertGreaterEqual(
                profile.width_at(fixtures.segment((3, 15), (7, 15))), mm(0.2)
            )
            self.assertGreaterEqual(
                profile.width_at(Segment(inside.start, inside.end, "In1.Cu")), mm(0.2)
            )

    def test_same_width_regions_keep_their_distinct_clearance_checks(self):
        source = """(version 1)
        (rule "general" (condition "A.Type == 'Track'") (constraint clearance (min 0.20mm)))
        (rule "local" (condition "A.Type == 'Track' && A.intersectsCourtyard('U1')")
          (constraint clearance (min 0.50mm)))"""
        profile = compiler(source).profile("S", mm(0.25), mm(0.2))
        segment = fixtures.segment((2, 2), (5, 2))
        obstacle = Obstacle(
            Point.from_mm([2.49, 2.39]), Point.from_mm([2.51, 2.41]), ("F.Cu",), "P"
        )
        # The physical copper has uniform width and should remain one segment.
        self.assertEqual(len(profile.pieces(segment)), 1)
        self.assertEqual(
            profile.clearance_at(segment, obstacle, width=mm(0.25)), mm(0.5)
        )
        model = GridRouter(
            Point.from_mm([0, 0]),
            Point.from_mm([8, 8]),
            mm(0.1),
            ("F.Cu",),
            "S",
            mm(0.25),
            mm(0.2),
            mm(0.6),
            mm(0.3),
            (obstacle,),
            profile=profile,
        )
        self.assertFalse(model.edge_clear(segment.start, segment.end, "F.Cu"))
        outside = Obstacle(
            Point.from_mm([4.29, 2.39]), Point.from_mm([4.31, 2.41]), ("F.Cu",), "P"
        )
        model = GridRouter(
            Point.from_mm([0, 0]),
            Point.from_mm([8, 8]),
            mm(0.1),
            ("F.Cu",),
            "S",
            mm(0.25),
            mm(0.2),
            mm(0.6),
            mm(0.3),
            (outside,),
            profile=profile,
        )
        self.assertFalse(model.edge_clear(segment.start, segment.end, "F.Cu"))

    def test_restored_copper_respects_local_clearance_with_uniform_width(self):
        from pcb.net_routing.rework import RemovedCopper

        source = """(version 1)
        (rule "general" (condition "A.Type == 'Track'") (constraint clearance (min 0.20mm)))
        (rule "local" (condition "A.Type == 'Track' && A.intersectsCourtyard('U1')")
          (constraint clearance (min 0.50mm)))"""
        net = fixtures.make_net("S", [(2, 2), (5, 2)])
        backend = fixtures.synthetic_backend(
            net, Budget(Limits(seconds=10)), width_mm=0.25
        )
        backend.settings["S"].profile = compiler(source).profile("S", mm(0.25), mm(0.2))
        backend.obstacles = (
            Obstacle(
                Point.from_mm([2.49, 2.39]), Point.from_mm([2.51, 2.41]), ("F.Cu",), "P"
            ),
        )
        removed = RemovedCopper(tracks=((fixtures.segment((2, 2), (5, 2)), mm(0.25)),))
        self.assertIsNone(
            backend.restored_plan(net, (), removed, source_unchanged=False)
        )
        self.assertTrue(backend.budget.failure_details.get("restore_seed"))

    def test_unknown_obstacle_courtyard_cannot_weaken_clearance(self):
        source = """(version 1)
        (rule "general" (condition "A.Type == 'Track'") (constraint clearance (min 0.20mm)))
        (rule "obstacle local" (condition "B.Type == 'Track' && B.intersectsCourtyard('U1')")
          (constraint clearance (min 0.50mm)))"""
        profile = compiler(source).profile("S", mm(0.25), mm(0.2))
        segment = fixtures.segment((4, 2), (5, 2))
        obstacle = Obstacle(
            Point.from_mm([4.3, 2.4]),
            Point.from_mm([4.4, 2.5]),
            ("F.Cu",),
            "P",
            "copper",
        )
        self.assertGreaterEqual(
            profile.clearance_at(segment, obstacle, width=mm(0.25)), mm(0.5)
        )

    def test_courtyard_intersection_accounts_for_the_track_width(self):
        profile = compiler().profile("S", mm(0.25), mm(0.2))
        touching = fixtures.segment((3.1, 1.5), (3.1, 2.5))
        clear = fixtures.segment((3.14, 1.5), (3.14, 2.5))
        self.assertIn("U1", profile.intersecting_references(touching, mm(0.25)))
        self.assertNotIn("U1", profile.intersecting_references(clear, mm(0.25)))

    def test_width_model_cannot_ignore_a_stricter_intersected_courtyard(self):
        source = """(version 1)
        (rule "general" (condition "A.Type == 'Track'") (constraint track_width (min 0.25mm)))
        (rule "local" (condition "A.Type == 'Track' && A.intersectsCourtyard('U1')")
          (constraint track_width (min 0.50mm)))"""
        profile = compiler(source).profile("S", mm(0.25), mm(0.2))
        crossing = fixtures.segment((0.5, 2), (3.5, 2))
        self.assertGreaterEqual(profile.width_at(crossing), mm(0.5))

    def test_unrelated_courtyard_does_not_enable_a_local_neck(self):
        source = """(version 1)
        (rule "conditional" (condition "A.Type == 'Track' && (A.NetName == 'S' || A.intersectsCourtyard('U1'))")
          (constraint track_width (min 0.15mm)))"""
        rules = compiler(source)
        rules.courtyards += (
            Courtyard(
                "R100",
                "F.Cu",
                tuple(
                    Point.from_mm(point) for point in ((4, 1), (6, 1), (6, 3), (4, 3))
                ),
            ),
        )
        segment = fixtures.segment((4.5, 2), (5.5, 2))
        self.assertEqual(
            rules.profile("S", mm(0.25), mm(0.2)).width_at(segment), mm(0.25)
        )

    def test_general_courtyard_rules_apply_to_all_copper_layers(self):
        source = """(version 1)
        (rule "width" (condition "A.Type == 'Track' && A.intersectsCourtyard('U1')")
          (constraint track_width (min 0.50mm)))
        (rule "gap" (condition "A.Type == 'Track' && A.intersectsCourtyard('U1')")
          (constraint clearance (min 0.50mm)))"""
        profile = compiler(source).profile("S", mm(0.25), mm(0.2))
        obstacle = Obstacle(
            Point.from_mm((4, 2)), Point.from_mm((4.1, 2.1)), ("B.Cu",), "P"
        )
        for layer in ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu"):
            segment = Segment(Point.from_mm((1.5, 2)), Point.from_mm((2.5, 2)), layer)
            with self.subTest(layer=layer):
                self.assertGreaterEqual(profile.width_at(segment), mm(0.5))
                self.assertGreaterEqual(
                    profile.clearance_at(segment, obstacle, width=mm(0.25)), mm(0.5)
                )

    def test_courtyard_reference_wildcards_use_actual_footprint_references(self):
        source = """(version 1)
        (rule "local" (condition "A.Type == 'Track' && A.intersectsCourtyard('U?')")
          (constraint track_width (min 0.50mm)))"""
        profile = compiler(source).profile("S", mm(0.25), mm(0.2))
        self.assertGreaterEqual(
            profile.width_at(fixtures.segment((1.5, 2), (2.5, 2))), mm(0.5)
        )

    def test_courtyard_library_wildcards_match_the_footprint_identifier(self):
        source = """(version 1)
        (rule "local" (condition "A.Type == 'Track' && A.intersectsCourtyard('Demo:Device*')")
          (constraint track_width (min 0.50mm)))"""
        rules = compiler(source)
        rules.courtyards += (
            Courtyard(
                "U42",
                "B.Cu",
                tuple(Point.from_mm(p) for p in ((4, 1), (6, 1), (6, 3), (4, 3))),
                "Demo:Device_ABC",
            ),
        )
        segment = Segment(Point.from_mm((4.5, 2)), Point.from_mm((5.5, 2)), "In1.Cu")
        self.assertGreaterEqual(
            rules.profile("S", mm(0.25), mm(0.2)).width_at(segment), mm(0.5)
        )

    def test_unimplemented_component_class_selector_fails_closed(self):
        source = """(version 1)
        (rule "class" (condition "A.intersectsCourtyard('${class:critical}')")
          (constraint track_width (min 0.50mm)))"""
        with self.assertRaisesRegex(ValueError, "component-class courtyard selectors"):
            compiler(source)

    def test_unmodeled_courtyard_cannot_hide_stricter_local_rules(self):
        source = """(version 1)
        (rule "general width" (condition "A.Type == 'Track'") (constraint track_width (min 0.25mm)))
        (rule "local width" (condition "A.Type == 'Track' && A.intersectsCourtyard('U1')")
          (constraint track_width (min 0.50mm)))
        (rule "general clearance" (condition "A.Type == 'Track'") (constraint clearance (min 0.20mm)))
        (rule "local clearance" (condition "A.Type == 'Track' && A.intersectsCourtyard('U1')")
          (constraint clearance (min 0.50mm)))"""
        rules = compiler(source)
        rules.courtyards = ()
        rules.uncertain_courtyards = {"F.Cu": frozenset({"U1"})}
        profile = rules.profile("S", mm(0.25), mm(0.2))
        segment = fixtures.segment((4, 2), (5, 2))
        obstacle = Obstacle(
            Point.from_mm([4.3, 2.4]), Point.from_mm([4.4, 2.5]), ("F.Cu",), "P"
        )
        self.assertGreaterEqual(profile.width_at(segment), mm(0.5))
        self.assertGreaterEqual(
            profile.clearance_at(segment, obstacle, width=mm(0.25)), mm(0.5)
        )

    def test_unmodeled_courtyard_from_native_board_remains_uncertain_on_all_layers(
        self,
    ):
        import pcbnew

        board, footprint, shape, empty, netclass, design = (Mock() for _ in range(6))
        board.GetAllNetClasses.return_value = {"Default": netclass}
        netclass.GetTrackWidth.return_value = mm(0.25)
        netclass.GetClearance.return_value = mm(0.2)
        board.GetPads.return_value = []
        board.GetFootprints.return_value = [footprint]
        board.GetDesignSettings.return_value = design
        design.m_TrackMinWidth, design.m_MinClearance = mm(0.2), 0
        footprint.GetReference.return_value = "U42"
        footprint.GetFPIDAsString.return_value = "Demo:Device_ABC"
        footprint.GetCourtyard.side_effect = lambda layer: (
            shape if layer == pcbnew.F_CrtYd else empty
        )
        shape.OutlineCount.return_value = 1
        shape.HoleCount.return_value = 1
        empty.OutlineCount.return_value = 0
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.kicad_dru"
            path.write_text("""(version 1)
            (rule "local" (condition "A.Type == 'Track' && A.intersectsCourtyard('Demo:Device*')")
              (constraint track_width (min 0.50mm)))""")
            rules = RoutingRules.from_board(board, path)
        profile = rules.profile("S", mm(0.25), mm(0.2))
        for layer in ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu"):
            segment = Segment(Point.from_mm((4, 2)), Point.from_mm((5, 2)), layer)
            self.assertGreaterEqual(profile.width_at(segment), mm(0.5))

    def test_uncertain_boundary_does_not_assume_a_permissive_local_clearance(self):
        source = """(version 1)
        (rule "general" (condition "A.Type == 'Track'") (constraint clearance (min 0.50mm)))
        (rule "local" (condition "A.Type == 'Track' && A.intersectsCourtyard('U1')")
          (constraint clearance (min 0.15mm)))"""
        profile = compiler(source).profile("S", mm(0.25), mm(0.2))
        # The edge of the physical track is precisely on the courtyard edge.
        segment = fixtures.segment((3.125, 1.5), (3.125, 2.5))
        obstacle = Obstacle(
            Point.from_mm([4, 2]), Point.from_mm([4.1, 2.1]), ("F.Cu",), "P"
        )
        self.assertEqual(
            profile.clearance_at(segment, obstacle, width=mm(0.25)), mm(0.5)
        )

    def test_layer_width_lower_bound_never_exceeds_modeled_segment_width(self):
        sources = (
            RULES,
            """(version 1)
            (rule "general" (condition "A.Type == 'Track'") (constraint track_width (min 0.25mm)))
            (rule "wide" (condition "A.Type == 'Track' && A.intersectsCourtyard('U1')")
              (constraint track_width (min 0.50mm)))""",
        )
        for source in sources:
            profile = compiler(source).profile("S", mm(0.25), mm(0.2))
            for x in (0.5, 1, 2, 3, 3.125, 3.3, 4):
                for layer in ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu"):
                    segment = Segment(
                        Point.from_mm((x, 1.5)), Point.from_mm((x, 2.5)), layer
                    )
                    self.assertLessEqual(
                        profile.minimum_width_for(layer), profile.width_at(segment)
                    )

    def test_minimum_width_bound_respects_net_class_and_rule_priority(self):
        source = """(version 1)
        (rule "signal neck" (condition "A.NetClass == 'SIGNAL' && A.intersectsCourtyard('U1')")
          (constraint track_width (min 0.15mm)))
        (rule "power width" (condition "A.NetClass == 'POWER'")
          (constraint track_width (min 1.50mm)))"""
        profile = compiler(source).profile("P", mm(0.25), mm(0.2))
        self.assertEqual(profile.minimum_width, mm(0.15))
        self.assertEqual(profile.minimum_width_for("F.Cu"), mm(1.5))
        local_power = (
            source
            + """
        (rule "power neck" (condition "A.NetClass == 'POWER' && A.intersectsCourtyard('U1')")
          (constraint track_width (min 0.30mm)))"""
        )
        profile = compiler(local_power).profile("P", mm(0.25), mm(0.2))
        self.assertEqual(profile.minimum_width_for("F.Cu"), mm(0.3))

    def test_small_global_width_keeps_larger_net_class_and_electrical_floors(self):
        rules = RoutingRules(
            (),
            (),
            {"CORE": (mm(0.2), mm(0.2)), "FIELD": (mm(0.25), mm(0.2))},
            {"LOGIC": "CORE", "FIELD_IO": "FIELD"},
            mm(0.16),
            0,
        )
        profiles = (
            (rules.profile("LOGIC", mm(0.2), mm(0.2)), mm(0.2)),
            (rules.profile("FIELD_IO", mm(0.2), mm(0.2)), mm(0.25)),
            (rules.profile("FIELD_IO", mm(0.2), mm(0.2), width_floor=mm(0.3)), mm(0.3)),
        )
        for profile, expected in profiles:
            for layer in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"):
                with self.subTest(net=profile.net, floor=expected, layer=layer):
                    segment = Segment(
                        Point.from_mm((1, 1)), Point.from_mm((2, 1)), layer
                    )
                    self.assertEqual(profile.width_at(segment), expected)
                    self.assertEqual(profile.minimum_width_for(layer), expected)
                    self.assertEqual(profile.pieces(segment), ((segment, expected),))

    def test_minimum_clearance_bound_ignores_unrelated_nets_but_not_uncertain_rules(
        self,
    ):
        source = """(version 1)
        (rule "general" (condition "A.Type == 'Track'") (constraint clearance (min 0.20mm)))
        (rule "unrelated" (condition "A.NetName == 'OTHER' && A.intersectsCourtyard('U1')")
          (constraint clearance (min 0.10mm)))"""
        profile = compiler(source).profile("S", mm(0.25), mm(0.2))
        obstacle = Obstacle(
            Point.from_mm([4.3, 2.4]), Point.from_mm([4.4, 2.5]), ("F.Cu",), "P", "pad"
        )
        self.assertEqual(profile.minimum_clearance_for("F.Cu", obstacle), mm(0.2))
        uncertain = (
            source
            + """
        (rule "outside" (condition "A.Type == 'Track' && !A.intersectsCourtyard('U1')")
          (constraint clearance (min 0.10mm)))"""
        )
        profile = compiler(uncertain).profile("S", mm(0.25), mm(0.2))
        self.assertEqual(profile.minimum_clearance_for("F.Cu", obstacle), mm(0.1))

    def test_negated_courtyard_condition_cannot_create_an_unsafe_allowance(self):
        source = """(version 1)
        (rule "general" (condition "A.Type == 'Track'") (constraint clearance (min 0.20mm)))
        (rule "outside" (condition "A.Type == 'Track' && !A.intersectsCourtyard('U1')")
          (constraint clearance (min 0.10mm)))"""
        profile = compiler(source).profile("S", mm(0.25), mm(0.2))
        segment = fixtures.segment((4, 2), (5, 2))
        obstacle = Obstacle(
            Point.from_mm([4.3, 2.4]), Point.from_mm([4.4, 2.5]), ("F.Cu",), "P", "pad"
        )
        self.assertGreaterEqual(
            profile.clearance_at(segment, obstacle, width=mm(0.25)), mm(0.2)
        )

    def test_unknown_obstacle_group_cannot_weaken_clearance(self):
        source = """(version 1)
        (rule "general" (condition "A.Type == 'Track'") (constraint clearance (min 0.20mm)))
        (rule "group" (condition "B.Type == 'Track' && B.memberOfGroup('OLD')")
          (constraint clearance (min 0.50mm)))"""
        profile = compiler(source).profile("S", mm(0.25), mm(0.2))
        segment = fixtures.segment((4, 2), (5, 2))
        obstacle = Obstacle(
            Point.from_mm([4.3, 2.4]),
            Point.from_mm([4.4, 2.5]),
            ("F.Cu",),
            "P",
            "copper",
        )
        self.assertGreaterEqual(
            profile.clearance_at(segment, obstacle, width=mm(0.25)), mm(0.5)
        )

    def test_endpoint_precheck_matches_exhaustive_rule_aware_access(self):
        import random

        profile = compiler().profile("S", mm(0.25), mm(0.2))
        ring = (
            Obstacle(
                Point.from_mm([1.80, 1.80]), Point.from_mm([1.81, 2.30]), ("F.Cu",), "P"
            ),
            Obstacle(
                Point.from_mm([2.29, 1.80]), Point.from_mm([2.30, 2.30]), ("F.Cu",), "P"
            ),
            Obstacle(
                Point.from_mm([1.80, 1.80]), Point.from_mm([2.30, 1.81]), ("F.Cu",), "P"
            ),
            Obstacle(
                Point.from_mm([1.80, 2.29]), Point.from_mm([2.30, 2.30]), ("F.Cu",), "P"
            ),
        )
        samples = [(Point.from_mm([2.05, 2.05]), ring)]
        rng = random.Random(9182)
        for _ in range(50):
            x = rng.randrange(230, 350) / 100
            y = rng.randrange(130, 270) / 100
            ox = round(x + rng.randrange(-40, 41) / 100, 2)
            oy = round(y + rng.randrange(-40, 41) / 100, 2)
            obstacle = Obstacle(
                Point.from_mm([ox, oy]),
                Point.from_mm([round(ox + 0.08, 2), round(oy + 0.08, 2)]),
                ("F.Cu",),
                "P",
            )
            samples.append((Point.from_mm([x, y]), (obstacle,)))
        trapped = 0
        for endpoint, obstacles in samples:
            model = GridRouter(
                Point.from_mm([0, 0]),
                Point.from_mm([4, 4]),
                mm(0.2),
                ("F.Cu",),
                "S",
                mm(0.25),
                mm(0.2),
                mm(0.6),
                mm(0.3),
                obstacles,
                profile=profile,
            )
            if (endpoint.x - model.lower.x) % model.step or (
                endpoint.y - model.lower.y
            ) % model.step:
                trapped += not model._endpoint_inside_minimum_foreign_clearance(
                    endpoint, "F.Cu"
                ) and model._endpoint_cannot_begin_stub(endpoint, "F.Cu")
            for reverse in (False, True):
                with (
                    patch.object(
                        model,
                        "_endpoint_inside_minimum_foreign_clearance",
                        return_value=False,
                    ),
                    patch.object(
                        model, "_endpoint_cannot_begin_stub", return_value=False
                    ),
                ):
                    expected = next(
                        model._endpoint_option_groups(
                            endpoint, "F.Cu", reverse=reverse
                        ),
                        [],
                    )
                actual = next(
                    model._endpoint_option_groups(endpoint, "F.Cu", reverse=reverse), []
                )
                self.assertEqual(actual, expected, (endpoint, obstacles, reverse))
        self.assertGreater(
            trapped, 0, "Exercise the prefix-based rejection, not only overlap checks"
        )

    def test_inner_layers_do_not_inherit_outer_necks(self):
        profile = compiler().profile("S", mm(0.25), mm(0.2))
        self.assertEqual(
            profile.width_at(
                Segment(Point.from_mm([2, 2]), Point.from_mm([2.5, 2]), "In1.Cu")
            ),
            mm(0.25),
        )

    def test_electrical_floor_overrides_package_allowance(self):
        profile = compiler().profile(
            "S", mm(0.25), mm(0.2), width_floor=mm(0.3), clearance_floor=mm(0.25)
        )
        segment = fixtures.segment((2, 2), (2.5, 2))
        obstacle = Obstacle(
            Point.from_mm([4, 4]), Point.from_mm([5, 5]), ("F.Cu",), "P"
        )
        self.assertEqual(profile.width_at(segment), mm(0.3))
        self.assertEqual(profile.clearance_at(segment, obstacle), mm(0.25))

    def test_emitted_copper_and_obstacle_payload_use_identical_dimensions(self):
        profile = compiler().profile("S", mm(0.25), mm(0.2))
        route = fixtures.route(fixtures.segment((2, 2), (5, 2)))
        changes = campaign_changes(
            route, "S", mm(0.25), mm(0.6), mm(0.3), segment_dimensions=profile.pieces
        )
        emitted = [
            float(parse(c["expression"]).children("width")[0].atoms()[1].value)
            for c in changes
        ]
        payload = route_payload(route, "S", mm(0.25), mm(0.6), mm(0.3), profile)
        self.assertEqual(emitted, [0.15, 0.25])
        self.assertEqual(emitted, [s["width_mm"] for s in payload["segments"]])

    def test_unsupported_rule_expressions_fail_explicitly(self):
        for expression in (
            "A.Unknown == 'x'",
            "A.getSecret()",
            "__import__('os')",
            "A.Type[0] == 'x'",
        ):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                compiler(
                    '(version 1) (rule "unknown" (condition "'
                    + expression
                    + '") (constraint track_width (min .1mm)))'
                )

    def test_last_rule_wins_and_comments_inside_strings_are_not_removed(self):
        profile = compiler(
            RULES
            + '\n# comment\n(rule "last # width" (condition "A.NetName == \'S\'") (constraint track_width (min .4mm)))'
        ).profile("S", mm(0.25), mm(0.2))
        self.assertEqual(profile.width_at(fixtures.segment((2, 2), (2.5, 2))), mm(0.4))

    def test_concave_courtyard_does_not_grant_neck_across_outside_gap(self):
        source = compiler()
        source.courtyards = (
            Courtyard(
                "U1",
                "F.Cu",
                tuple(
                    Point.from_mm(p)
                    for p in [
                        (1, 1),
                        (5, 1),
                        (5, 5),
                        (4, 5),
                        (4, 2),
                        (2, 2),
                        (2, 5),
                        (1, 5),
                    ]
                ),
            ),
        )
        widths = [
            w
            for _, w in source.profile("S", mm(0.25), mm(0.2)).pieces(
                fixtures.segment((1.5, 3), (4.5, 3))
            )
        ]
        self.assertEqual(widths, [mm(0.15), mm(0.25), mm(0.15)])

    def test_exact_edge_check_opens_real_corridor_without_sampling_padding(self):
        obstacles = (
            Obstacle(Point.from_mm([1, 1]), Point.from_mm([9, 4.79]), ("F.Cu",), "X"),
            Obstacle(Point.from_mm([1, 5.21]), Point.from_mm([9, 9]), ("F.Cu",), "X"),
        )
        router = GridRouter(
            Point.from_mm([0, 0]),
            Point.from_mm([10, 10]),
            mm(0.1),
            ("F.Cu",),
            "S",
            mm(0.2),
            mm(0.1),
            mm(0.6),
            mm(0.3),
            obstacles,
        )
        route = router.route(
            Point.from_mm([2, 5]), "F.Cu", Point.from_mm([8, 5]), "F.Cu"
        )
        self.assertTrue(route.segments)
        self.assertTrue(
            all(router.edge_clear(s.start, s.end, s.layer) for s in route.segments)
        )

    def test_exact_edge_check_rejects_obstacle_between_clear_vertices(self):
        obstacle = Obstacle(
            Point.from_mm([2.45, 1.95]), Point.from_mm([2.55, 2.05]), ("F.Cu",), "X"
        )
        router = GridRouter(
            Point.from_mm([0, 0]),
            Point.from_mm([5, 5]),
            mm(1),
            ("F.Cu",),
            "S",
            mm(0.1),
            mm(0.1),
            mm(0.6),
            mm(0.3),
            (obstacle,),
        )
        self.assertNotIn((2, 2), router.blocked["F.Cu"])
        self.assertNotIn((3, 2), router.blocked["F.Cu"])
        self.assertFalse(
            router.edge_clear(Point.from_mm([2, 2]), Point.from_mm([3, 2]), "F.Cu")
        )

    def test_search_cannot_use_local_neck_as_a_whole_net_width(self):
        profile = compiler().profile("S", mm(0.25), mm(0.2))
        obstacle = Obstacle(
            Point.from_mm([1, 2.23]), Point.from_mm([6, 2.8]), ("F.Cu",), "P"
        )
        router = GridRouter(
            Point.from_mm([0, 0]),
            Point.from_mm([8, 8]),
            mm(0.1),
            ("F.Cu",),
            "S",
            mm(0.25),
            mm(0.2),
            mm(0.6),
            mm(0.3),
            (obstacle,),
            profile=profile,
        )
        self.assertTrue(
            router.edge_clear(Point.from_mm([1.5, 2]), Point.from_mm([2.5, 2]), "F.Cu")
        )
        self.assertFalse(
            router.edge_clear(Point.from_mm([3.5, 2]), Point.from_mm([5, 2]), "F.Cu")
        )


class RetryConsistencyTests(unittest.TestCase):
    def test_multi_net_search_reuses_unused_time_for_remaining_nets(self):
        from pcb.net_routing.model import NetPlan

        clock = [0.0]
        with patch(
            "pcb.net_routing.model.time.monotonic", side_effect=lambda: clock[0]
        ):
            budget = Budget(Limits(seconds=20, order_trials=1), started=0.0)
            nets = tuple(fixtures.make_net(name, [(0, 0), (1, 0)]) for name in "ABC")

            def solve(net, prior):
                clock[0] += 8 if net.name == "C" else 1
                budget.check()
                return (NetPlan(net.name, fixtures.route()),)

            result, _ = search_orders(nets, budget, solve, [["A", "B", "C"]])
            self.assertEqual(len(result), 1)
            self.assertNotIn("net_seconds", budget.limits_hit)

    def test_net_slice_shares_remaining_not_original_time(self):
        clock = [9.0]
        with patch(
            "pcb.net_routing.model.time.monotonic", side_effect=lambda: clock[0]
        ):
            budget = Budget(Limits(seconds=20), started=0.0)
            with budget.net_slice(2):
                clock[0] = 15.0
                with self.assertRaises(BudgetExhausted):
                    budget.check()
            self.assertEqual(budget.limits_hit, {"net_seconds"})

    def test_renewal_preserves_independent_parallel_batches(self):
        first = recovery_fixture.request()
        second = copy.deepcopy(first)
        second["nets"] = [{"name": "OTHER"}]
        second["shared"]["bounds_mm"] = [30, 30, 40, 40]
        fresh = [[first, second]]
        self.assertEqual(renew_queue(fresh, fresh, RecoveryPolicy()), fresh)

    def test_net_time_slice_leaves_the_rest_of_the_batch_available(self):
        clock = [0.0]
        with patch(
            "pcb.net_routing.model.time.monotonic", side_effect=lambda: clock[0]
        ):
            budget = Budget(Limits(seconds=30), started=0.0)
            with budget.net_slice(3):
                clock[0] = 11.0
                with self.assertRaises(BudgetExhausted):
                    budget.check()
            budget.check()
            self.assertEqual(budget.limits_hit, {"net_seconds"})
            budget.hit_limit("net_seconds")
            self.assertEqual(budget.limit_events, 2)

    def test_renewal_keeps_stage_but_rebuilds_dimensions_and_never_replays_removals(
        self,
    ):
        initial = recovery_fixture.request()
        previous = copy.deepcopy(initial)
        for _ in range(4):
            previous = next_stage(previous, RecoveryPolicy())
        previous["rip_up"] = {"OLD_NEIGHBOR": [{"kind": "via", "uuid": "stale"}]}
        previous["nets"].append({"name": "OLD_NEIGHBOR"})
        initial["shared"]["width_mm"] = 0.4
        rebuilt = renew_queue([[initial]], [[previous]], RecoveryPolicy())
        self.assertEqual(rebuilt[0][0]["recovery_stage"], 4)
        self.assertEqual(rebuilt[0][0]["shared"]["width_mm"], 0.4)
        self.assertEqual([n["name"] for n in rebuilt[0][0]["nets"]], ["SIGNAL"])
        self.assertNotIn("rip_up", rebuilt[0][0])

    def test_failed_independent_search_is_not_repeated_when_splitting_batch(self):
        request = recovery_fixture.request()
        request["nets"].append({"name": "NEXT"})
        attempt = {
            "checkpoints": [{"net": "SIGNAL", "count": 1}],
            "trials": [
                {
                    "orders": [
                        {
                            "searches": [
                                {
                                    "net": "SIGNAL",
                                    "prior_nets": [],
                                    "candidates": 0,
                                    "budget_limited": False,
                                }
                            ]
                        }
                    ]
                }
            ],
        }
        queue = []
        _split_failed(
            queue, [request], {"SIGNAL": 2, "NEXT": 2}, {"attempts": [attempt]}
        )
        self.assertEqual(queue[0][0]["nets"][0]["name"], "NEXT")
        self.assertEqual(queue[1][0]["recovery_stage"], 1)

    def test_limited_batch_failure_gets_full_single_net_retry(self):
        request = recovery_fixture.request()
        request["nets"].append({"name": "NEXT"})
        attempt = {
            "checkpoints": [{"net": "SIGNAL", "count": 1}],
            "trials": [
                {
                    "orders": [
                        {
                            "searches": [
                                {
                                    "net": "SIGNAL",
                                    "prior_nets": [],
                                    "candidates": 0,
                                    "budget_limited": True,
                                }
                            ]
                        }
                    ]
                }
            ],
        }
        queue = []
        _split_failed(
            queue, [request], {"SIGNAL": 2, "NEXT": 2}, {"attempts": [attempt]}
        )
        retries = {batch[0]["nets"][0]["name"]: batch[0] for batch in queue}
        self.assertEqual(retries["SIGNAL"]["recovery_stage"], 0)
        self.assertEqual(retries["SIGNAL"].get("checkpoint_offset", 0), 0)

    def test_search_order_records_whether_a_net_hit_its_slice(self):
        nets = tuple(fixtures.make_net(name, [(0, 0), (1, 0)]) for name in ("A", "B"))
        budget = Budget(Limits(seconds=10, order_trials=1))

        def limited_failure(net, prior):
            budget.hit_limit("tree_seconds")
            return ()

        _, trials = search_orders(nets, budget, limited_failure, [["A", "B"]])
        self.assertIs(trials[0]["searches"][0]["budget_limited"], True)

    def test_alternate_orders_reuse_independent_failure(self):
        nets = tuple(
            fixtures.make_net(name, [(0, 0), (2, 2)]) for name in ("A", "B", "C")
        )
        calls = []

        def solve(net, prior):
            calls.append((net.name, len(prior)))
            return ()

        search_orders(nets, Budget(Limits(seconds=3, order_trials=6)), solve)
        self.assertEqual(len(calls), len(set(calls)))

    def test_connected_net_does_not_consume_half_a_hard_net_budget(self):
        hard = fixtures.make_net("HARD", [(0, 0), (5, 5)])
        connected = fixtures.Net(
            "GND", (fixtures.Island("connected", (fixtures.site(1, 1),)),)
        )
        budget = Budget(Limits(seconds=30, order_trials=1), started=0.0)
        clock = [0.0]

        def solve(net, prior):
            if net.name == "HARD":
                clock[0] = 20.0
                budget.check()
            return (fixtures.NetPlan(net.name, fixtures.route()),)

        with patch(
            "pcb.net_routing.model.time.monotonic", side_effect=lambda: clock[0]
        ):
            plans, _ = search_orders(
                (hard, connected), budget, solve, requested=[["HARD", "GND"]]
            )
        self.assertTrue(plans)
        self.assertEqual([plan.net for plan in plans[0].plans], ["GND", "HARD"])
        self.assertNotIn("net_seconds", budget.limits_hit)

    def test_noop_ordering_preserves_active_order_and_avoids_duplicate_trials(self):
        first = fixtures.make_net("FIRST", [(0, 0), (2, 2)])
        second = fixtures.make_net("SECOND", [(0, 3), (2, 5)])
        connected = fixtures.Net(
            "GND", (fixtures.Island("connected", (fixtures.site(1, 1),)),)
        )
        calls = []

        def solve(net, prior):
            calls.append(net.name)
            return (fixtures.NetPlan(net.name, fixtures.route()),)

        plans, trials = search_orders(
            (first, second, connected),
            Budget(Limits(order_trials=2)),
            solve,
            requested=[["FIRST", "GND", "SECOND"], ["GND", "FIRST", "SECOND"]],
        )
        self.assertTrue(plans)
        self.assertEqual(calls, ["GND", "FIRST", "SECOND"])
        self.assertEqual(len(trials), 1)

    def test_time_limited_empty_search_is_retried_in_a_later_order(self):
        nets = tuple(
            fixtures.make_net(name, [(0, 0), (2, 2)]) for name in ("A", "B", "C")
        )
        calls = []
        clock = [0.0]
        budget = Budget(Limits(seconds=30, order_trials=3), started=0.0)

        def solve(net, prior):
            calls.append((net.name, len(prior)))
            if net.name == "A" and calls.count(("A", 0)) == 1:
                clock[0] = 11.0
                try:
                    budget.check()
                except BudgetExhausted:
                    return ()
            if net.name == "B" and not prior:
                return ()
            return (fixtures.NetPlan(net.name, fixtures.route()),)

        with patch(
            "pcb.net_routing.model.time.monotonic", side_effect=lambda: clock[0]
        ):
            plans, _ = search_orders(
                nets,
                budget,
                solve,
                requested=[["A", "B", "C"], ["B", "A", "C"], ["A", "C", "B"]],
            )
        self.assertEqual(calls.count(("A", 0)), 2)
        self.assertTrue(plans)

    def test_a_previous_limit_does_not_disable_caching_an_unlimited_failure(self):
        nets = tuple(
            fixtures.make_net(name, [(0, 0), (2, 2)]) for name in ("A", "B", "C")
        )
        calls = []
        clock = [0.0]
        budget = Budget(Limits(seconds=30, order_trials=3), started=0.0)

        def solve(net, prior):
            calls.append((net.name, len(prior)))
            if net.name == "A":
                clock[0] = 11.0
                try:
                    budget.check()
                except BudgetExhausted:
                    return ()
            return ()

        with patch(
            "pcb.net_routing.model.time.monotonic", side_effect=lambda: clock[0]
        ):
            search_orders(
                nets,
                budget,
                solve,
                requested=[["A", "B", "C"], ["B", "A", "C"], ["B", "C", "A"]],
            )
        self.assertEqual(calls.count(("B", 0)), 1)


class NativeRuleConsistencyTests(unittest.TestCase):
    def test_native_drc_and_search_both_apply_stricter_crossing_width(self):
        import pcbnew

        from pcb.net_routing.native import load_board
        from pcb.route_validation import _drc
        from verification import test_route_optimizer as boards

        with tempfile.TemporaryDirectory(prefix="autopcb-width-region-") as directory:
            root = Path(directory)
            path = boards.RouteOptimizerTests.make_board(root)
            board = pcbnew.LoadBoard(str(path))
            board.SetCopperLayerCount(4)
            footprint = board.FindFootprintByReference("J1")
            footprint.SetFPIDAsString("Demo:Device_ABC")
            for start, end in (
                ((1, 1), (3, 1)),
                ((3, 1), (3, 3)),
                ((3, 3), (1, 3)),
                ((1, 3), (1, 1)),
            ):
                shape = pcbnew.PCB_SHAPE(footprint)
                shape.SetShape(pcbnew.SHAPE_T_SEGMENT)
                shape.SetLayer(pcbnew.F_CrtYd)
                shape.SetStart(pcbnew.VECTOR2I(mm(start[0]), mm(start[1])))
                shape.SetEnd(pcbnew.VECTOR2I(mm(end[0]), mm(end[1])))
                shape.SetWidth(mm(0.05))
                footprint.Add(shape)
            tracks = []
            for index, layer in enumerate(
                (pcbnew.F_Cu, pcbnew.B_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu)
            ):
                y = 1.5 + 0.3 * index
                track = pcbnew.PCB_TRACK(board)
                track.SetStart(pcbnew.VECTOR2I(mm(0.5), mm(y)))
                track.SetEnd(pcbnew.VECTOR2I(mm(3.5), mm(y)))
                track.SetWidth(mm(0.25))
                track.SetLayer(layer)
                track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
                board.Add(track)
                tracks.append((track, y))
            pcbnew.SaveBoard(str(path), board)
            for selector_index, selector in enumerate(("J?", "Demo:Device*")):
                with self.subTest(selector=selector):
                    path.with_suffix(".kicad_dru").write_text(f"""(version 1)
                      (rule "general" (condition "A.Type == 'Track'") (constraint track_width (min 0.25mm)))
                      (rule "local" (condition "A.Type == 'Track' && A.intersectsCourtyard('{selector}')")
                        (constraint track_width (min 0.50mm)))""")
                    rules = RoutingRules.from_board(
                        load_board(path), path.with_suffix(".kicad_dru")
                    )
                    profile = rules.profile("SIGNAL", mm(0.25), mm(0.2))
                    for index, layer in enumerate(("F.Cu", "B.Cu", "In1.Cu", "In2.Cu")):
                        y = 1.5 + 0.3 * index
                        segment = Segment(
                            Point.from_mm((0.5, y)), Point.from_mm((3.5, y)), layer
                        )
                        self.assertGreaterEqual(profile.width_at(segment), mm(0.5))
                    report = _drc(
                        kicad_cli(),
                        path,
                        root / f"drc_{selector_index}.json",
                    )
                    violations = {
                        part.get("uuid")
                        for item in report["violations"]
                        if item.get("type") == "track_width"
                        for part in item.get("items", [])
                    }
                    for track, _ in tracks:
                        self.assertIn(track.m_Uuid.AsString(), violations, report)

    def test_native_drc_and_search_both_reject_stricter_local_clearance(self):
        import pcbnew

        from pcb.net_routing.native import load_board
        from pcb.route_native import _pad_obstacle
        from pcb.route_validation import _drc
        from verification import test_route_optimizer as boards

        with tempfile.TemporaryDirectory(
            prefix="autopcb-clearance-region-"
        ) as directory:
            root = Path(directory)
            path = boards.RouteOptimizerTests.make_board(root)
            board = pcbnew.LoadBoard(str(path))
            footprint = board.FindFootprintByReference("J1")
            for start, end in (
                ((1, 1), (3, 1)),
                ((3, 1), (3, 3)),
                ((3, 3), (1, 3)),
                ((1, 3), (1, 1)),
            ):
                shape = pcbnew.PCB_SHAPE(footprint)
                shape.SetShape(pcbnew.SHAPE_T_SEGMENT)
                shape.SetLayer(pcbnew.F_CrtYd)
                shape.SetStart(pcbnew.VECTOR2I(mm(start[0]), mm(start[1])))
                shape.SetEnd(pcbnew.VECTOR2I(mm(end[0]), mm(end[1])))
                shape.SetWidth(mm(0.05))
                footprint.Add(shape)
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            other = fixtures.NativeNetRoutingTests.add_pad(
                board, "J3", (2.8, 2.4), net_name="OTHER"
            )
            pad = list(other.Pads())[0]
            pad.SetSize(pcbnew.VECTOR2I(mm(0.1), mm(0.1)))
            outer = fixtures.NativeNetRoutingTests.add_pad(
                board, "J4", (4.3, 2.4), net_name="OTHER"
            )
            outer_pad = list(outer.Pads())[0]
            outer_pad.SetSize(pcbnew.VECTOR2I(mm(0.1), mm(0.1)))
            pcbnew.SaveBoard(str(path), board)
            path.with_suffix(".kicad_dru").write_text("""(version 1)
              (rule "general" (condition "A.Type == 'Track'") (constraint clearance (min 0.20mm)))
              (rule "local" (condition "A.Type == 'Track' && A.intersectsCourtyard('J1')")
                (constraint clearance (min 0.50mm)))""")
            native = load_board(path)
            rules = RoutingRules.from_board(native, path.with_suffix(".kicad_dru"))
            profile = rules.profile("SIGNAL", mm(0.25), mm(0.2))
            native_pad = list(native.FindFootprintByReference("J3").Pads())[0]
            obstacle = _pad_obstacle(native, native_pad, ("F.Cu",))
            model = GridRouter(
                Point.from_mm([0, 0]),
                Point.from_mm([10, 8]),
                mm(0.1),
                ("F.Cu",),
                "SIGNAL",
                mm(0.25),
                mm(0.2),
                mm(0.6),
                mm(0.3),
                (obstacle,),
                profile=profile,
            )
            self.assertFalse(
                model.edge_clear(Point.from_mm([2, 2]), Point.from_mm([5, 2]), "F.Cu")
            )
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(mm(2), mm(2)))
            track.SetEnd(pcbnew.VECTOR2I(mm(5), mm(2)))
            track.SetWidth(mm(0.25))
            track.SetLayer(pcbnew.F_Cu)
            track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            board.Add(track)
            pcbnew.SaveBoard(str(path), board)
            report = _drc(kicad_cli(), path, root / "drc.json")
            for candidate_pad in (pad, outer_pad):
                ids = {track.m_Uuid.AsString(), candidate_pad.m_Uuid.AsString()}
                self.assertTrue(
                    any(
                        item.get("type") == "clearance"
                        and ids <= {part.get("uuid") for part in item.get("items", [])}
                        for item in report["violations"]
                    ),
                    report,
                )

    def test_recovery_can_coordinate_multiple_ground_and_power_obstructions(self):
        import pcbnew
        from pcb.net_routing.reroute import compile_reroutes
        from verification import test_route_optimizer as boards
        from verification import test_autoroute_rework as copper

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = boards.RouteOptimizerTests.make_board(root)
            board = pcbnew.LoadBoard(str(path))
            board.SetCopperLayerCount(4)
            for name, y in (("GND", 2.7), ("POWER", 3.0)):
                board.Add(pcbnew.NETINFO_ITEM(board, name))
                fixtures.NativeNetRoutingTests.add_pad(
                    board, name + "_A", (1, y), net_name=name
                )
                fixtures.NativeNetRoutingTests.add_pad(
                    board, name + "_B", (3, y), net_name=name
                )
                for pad in board.GetPads():
                    if pad.GetNetname() == name:
                        pad.SetPinType("power_in")
                copper.add_track(board, (1, y), (3, y), name)
            zone = pcbnew.ZONE(board)
            zone.SetNetCode(board.FindNet("GND").GetNetCode())
            zone.SetLayer(pcbnew.In1_Cu)
            board.Add(zone)
            board.GetConnectivity().Build(board)
            request = fixtures.NativeNetRoutingTests.config()
            request.update(max_islands=3, recovery_stage=4, checkpoint_margin_mm=100)
            request["nets"][0]["step_mm"] = 0.05
            policy = {"route": request["shared"], "max_pins": 3}
            compiled = compile_reroutes(board, request, policy, ())
            candidates = [
                r for r in compiled["requests"] if set(r["rip_up"]) == {"GND", "POWER"}
            ]
            self.assertTrue(candidates, compiled)
            self.assertEqual(sum(map(len, candidates[0]["rip_up"].values())), 2)
            self.assertEqual(candidates[0]["repair_max_islands"], 12)
            self.assertEqual(candidates[0]["checkpoint_margin_mm"], 12)
            for neighbor in candidates[0]["nets"][1:]:
                self.assertEqual(neighbor["step_mm"], 0.05)
                self.assertGreaterEqual(neighbor["max_cells"], 1000000)
            protected = compile_reroutes(board, request, policy, ("POWER",))
            self.assertTrue(
                all("POWER" not in r["rip_up"] for r in protected["requests"])
            )

    def test_native_drc_accepts_saved_neck_below_board_default(self):
        import hashlib
        import json
        import shutil
        import pcbnew
        from pcb.net_routing.native import load_board
        from pcb.route_validation import RouteContract, validate_routes
        from verification import test_route_optimizer as boards

        # Each project needs a fresh path: pcbnew caches project settings by
        # filename, so changing a .kicad_pro after its first load is not a test.
        for minimum, expected_neck in ((0.1, 0.15), (0.2, 0.15)):
            with (
                self.subTest(board_minimum=minimum),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                staging = root / "staging"
                staging.mkdir()
                source = boards.RouteOptimizerTests.make_board(staging)
                project = root / "project"
                project.mkdir()
                path = project / "v4.1.kicad_pcb"
                shutil.copyfile(source, path)
                path.with_suffix(".kicad_pro").write_text(
                    json.dumps(
                        {
                            "board": {
                                "design_settings": {
                                    "rules": {"min_track_width": minimum}
                                }
                            }
                        }
                    )
                )
                board = pcbnew.LoadBoard(str(path))
                self.assertEqual(board.GetDesignSettings().m_TrackMinWidth, mm(minimum))
                footprint = board.FindFootprintByReference("J1")
                for start, end in [
                    ((1, 1), (3, 1)),
                    ((3, 1), (3, 3)),
                    ((3, 3), (1, 3)),
                    ((1, 3), (1, 1)),
                ]:
                    shape = pcbnew.PCB_SHAPE(footprint)
                    shape.SetShape(pcbnew.SHAPE_T_SEGMENT)
                    shape.SetLayer(pcbnew.F_CrtYd)
                    shape.SetStart(pcbnew.VECTOR2I(mm(start[0]), mm(start[1])))
                    shape.SetEnd(pcbnew.VECTOR2I(mm(end[0]), mm(end[1])))
                    shape.SetWidth(mm(0.05))
                    footprint.Add(shape)
                pcbnew.SaveBoard(str(path), board)
                path.with_suffix(".kicad_dru").write_text(
                    RULES.replace("'SIGNAL'", "'Default'").replace("'U1'", "'J1'")
                )
                native = load_board(path)
                rules = RoutingRules.from_board(native, path.with_suffix(".kicad_dru"))
                profile = rules.profile("SIGNAL", mm(0.25), mm(0.2))
                route = fixtures.route(
                    fixtures.segment((2, 2), (5, 2)), fixtures.segment((5, 2), (8, 5))
                )
                changes = campaign_changes(
                    route,
                    "SIGNAL",
                    mm(0.25),
                    mm(0.6),
                    mm(0.3),
                    segment_dimensions=profile.pieces,
                )
                widths = [
                    float(parse(c["expression"]).children("width")[0].atoms()[1].value)
                    for c in changes
                ]
                self.assertIn(expected_neck, widths)
                self.assertIn(0.25, widths)
                self.assertTrue(all(width >= expected_neck for width in widths))
                before = path.read_bytes()
                evidence = validate_routes(
                    path,
                    hashlib.sha256(before).hexdigest(),
                    changes,
                    [
                        RouteContract(
                            "SIGNAL",
                            Point.from_mm([2, 2]),
                            "F.Cu",
                            Point.from_mm([8, 5]),
                            "F.Cu",
                        )
                    ],
                )
                self.assertEqual(evidence["status"], "no_new_drc_violations", evidence)
                self.assertEqual(evidence["candidate_unconnected"], 0)
                self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
