"""Exercise electrical screening and its integration with route planning."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from pcb.route_cli import main, plan
from pcb.route_constraints import ElectricalConstraints, ExistingVia
from pcb.route_native import read_native_obstacles
from pcb.route_optimizer import (
    COPPER_LAYERS,
    NoGridRoute,
    Point,
    Route,
    Segment,
    Via,
    mm,
)
from verification import test_route_optimizer as fixtures


class RouteConstraintTests(unittest.TestCase):
    @staticmethod
    def config() -> dict:
        data = fixtures.RouteOptimizerTests.config()
        data["electrical"] = fixtures.RouteOptimizerTests.electrical()
        return data

    def test_missing_limits_are_not_assessed(self):
        result = plan(fixtures.RouteOptimizerTests.config(), Path("."))
        self.assertEqual(result["electrical"]["status"], "not_assessed")

    def test_insufficient_current_or_voltage_and_clearance_are_rejected(self):
        for field, value, message in (
            ("current_a", 1.01, "No requested layer"),
            ("voltage_difference_v", 5.01, "voltage difference"),
        ):
            data = self.config()
            data["electrical"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                plan(data, Path("."))
        data = self.config()
        data["clearance_mm"] = 0.19
        with self.assertRaisesRegex(ValueError, "clearance"):
            plan(data, Path("."))

    def test_ineligible_layer_is_excluded_and_endpoint_must_be_eligible(self):
        data = self.config()
        data["electrical"]["tracks"]["B.Cu"]["max_current_a"] = 0.05
        result = plan(data, Path("."))
        self.assertEqual(result["electrical"]["eligible_layers"], ["F.Cu"])
        self.assertIn("B.Cu", result["electrical"]["excluded_layers"])
        data["end"]["layer"] = "B.Cu"
        with self.assertRaisesRegex(ValueError, "endpoint layer"):
            plan(data, Path("."))

    def test_no_via_rating_disables_layer_transition_before_search(self):
        data = self.config()
        data["electrical"].pop("via")
        data["end"]["layer"] = "B.Cu"
        with self.assertRaises(NoGridRoute):
            plan(data, Path("."))
        data["end"]["layer"] = "F.Cu"
        self.assertEqual(
            plan(data, Path("."))["electrical"]["status"], "within_supplied_limits"
        )

    def test_via_current_and_geometry_are_enforced(self):
        data = self.config()
        data["end"]["layer"] = "B.Cu"
        result = plan(data, Path("."))
        self.assertEqual(result["via_count"], 1)
        self.assertEqual(len(result["electrical"]["via_checks"]), 1)
        data["electrical"]["via"]["max_current_a"] = 0.05
        with self.assertRaises(NoGridRoute):
            plan(data, Path("."))
        data["electrical"]["via"]["max_current_a"] = 1
        data["electrical"]["via"]["min_drill_mm"] = 0.4
        with self.assertRaises(NoGridRoute):
            plan(data, Path("."))

    def test_route_voltage_drop_is_checked_with_track_and_via_resistance(self):
        data = self.config()
        result = plan(data, Path("."))
        electrical = result["electrical"]["voltage_drop"]
        self.assertGreater(electrical["estimated_v"], 0)
        self.assertGreater(electrical["estimated_power_w"], 0)
        data["electrical"]["voltage_drop"]["max_v"] = electrical["estimated_v"] / 2
        with self.assertRaisesRegex(ValueError, "voltage-drop"):
            plan(data, Path("."))
        data = self.config()
        data["end"]["layer"] = "B.Cu"
        data["electrical"]["voltage_drop"]["max_v"] = 0.005
        with self.assertRaisesRegex(ValueError, "voltage-drop"):
            plan(data, Path("."))

    def test_voltage_drop_units_for_a_known_straight_copper_trace(self):
        data = self.config()["electrical"]
        data["current_a"] = 5
        data["tracks"]["F.Cu"]["max_current_a"] = 5
        data["voltage_drop"]["copper_resistivity_ohm_mm"] = 0.000017
        limit = ElectricalConstraints.from_config(data)
        route = Route(
            (Segment(Point(mm(0), mm(0)), Point(mm(100), mm(0)), "F.Cu"),),
            (),
            (),
            100,
            0,
            0,
            0,
        )
        result = limit.check_voltage_drop(route, mm(0.25))
        resistance = 0.000017 * 100 / (0.25 * 0.035)
        self.assertAlmostEqual(result["estimated_resistance_ohm"], resistance)
        self.assertAlmostEqual(result["estimated_v"], 5 * resistance)
        self.assertAlmostEqual(result["estimated_power_w"], 25 * resistance)

    def test_existing_via_requires_matching_native_geometry(self):
        limits = ElectricalConstraints.from_config(self.config()["electrical"])
        at = Point(mm(1), mm(2))
        via = Via(at, "F.Cu", "B.Cu")
        route = Route((), (), (via,), 0, 0, 0, 0)
        valid = ExistingVia(
            at,
            "SIGNAL",
            tuple((layer, mm(0.6)) for layer in COPPER_LAYERS),
            mm(0.3),
            True,
        )
        self.assertEqual(limits.eligible_existing_vias((valid,), "SIGNAL"), {at})
        self.assertEqual(
            limits.check_vias(route, "SIGNAL", mm(0.6), mm(0.3), (valid,))[0]["kind"],
            "existing",
        )
        bad = ExistingVia(at, "SIGNAL", valid.sizes, mm(0.2), True)
        self.assertEqual(limits.eligible_existing_vias((bad,), "SIGNAL"), set())
        with self.assertRaisesRegex(ValueError, "geometry"):
            limits.check_vias(route, "SIGNAL", mm(0.6), mm(0.3), (bad,))
        with self.assertRaisesRegex(ValueError, "not established"):
            limits.check_vias(route, "SIGNAL", mm(0.6), mm(0.3), ())

    def test_via_reuse_checks_every_real_layer_without_requiring_nonexistent_layers(self):
        limits = ElectricalConstraints.from_config(self.config()["electrical"])
        at = Point(mm(1), mm(2))
        layers = ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu")
        valid = ExistingVia(at, "SIGNAL", tuple((layer, mm(.6)) for layer in layers), mm(.3), True)
        self.assertEqual(limits.eligible_existing_vias((valid,), "SIGNAL"), {at})
        route = Route((), (), (Via(at, "F.Cu", "B.Cu"),), 0, 0, 0, 0)
        self.assertEqual(limits.check_vias(route, "SIGNAL", mm(.6), mm(.3), (valid,))[0]["kind"],
                         "existing")
        narrow = ExistingVia(at, "SIGNAL", tuple((layer, limits.via.minimum_size - 1
                                if layer == "In3.Cu" else size) for layer, size in valid.sizes), mm(.3), True)
        self.assertEqual(limits.eligible_existing_vias((narrow,), "SIGNAL"), set())
        with self.assertRaisesRegex(ValueError, "below"):
            limits.check_vias(route, "SIGNAL", mm(.6), mm(.3), (narrow,))
        missing = ExistingVia(at, "SIGNAL", valid.sizes[:-1], mm(.3), True)
        self.assertEqual(limits.eligible_existing_vias((missing,), "SIGNAL"), set())
        duplicate = ExistingVia(at, "SIGNAL", (*valid.sizes, valid.sizes[0]), mm(.3), True)
        self.assertEqual(limits.eligible_existing_vias((duplicate,), "SIGNAL"), set())

    def test_invalid_or_unknown_electrical_inputs_fail_closed(self):
        for field, value in (
            ("current_a", float("nan")),
            ("current_a", True),
            ("voltage_difference_v", None),
        ):
            data = self.config()["electrical"]
            data[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                ElectricalConstraints.from_config(data)
        data = self.config()["electrical"]
        data["tracks"]["F.Cu"]["basis"] = " "
        with self.assertRaisesRegex(ValueError, "basis"):
            ElectricalConstraints.from_config(data)
        data = self.config()["electrical"]
        del data["current_basis"]
        with self.assertRaisesRegex(ValueError, "basis"):
            ElectricalConstraints.from_config(data)

    def test_native_via_sizes_are_read_per_layer_without_default_width(self):
        import pcbnew

        with tempfile.TemporaryDirectory(prefix="jessie-via-snapshot-") as directory:
            project = Path(directory)
            source = fixtures.RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            board.SetCopperLayerCount(4)
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(pcbnew.VECTOR2I(mm(5), mm(3)))
            via.SetWidth(mm(0.6))
            via.SetDrill(mm(0.3))
            via.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            board.Add(via)
            pcbnew.SaveBoard(str(source), board)
            snapshot = read_native_obstacles(source, "SIGNAL")
            self.assertEqual(len(snapshot.vias), 1)
            self.assertEqual(
                dict(snapshot.vias[0].sizes),
                {layer: mm(0.6) for layer in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")},
            )
            self.assertEqual(snapshot.vias[0].drill, mm(0.3))
            self.assertTrue(snapshot.vias[0].through)
            board.SetCopperLayerCount(6)
            pcbnew.SaveBoard(str(source), board)
            six = read_native_obstacles(source, "SIGNAL")
            self.assertEqual(dict(six.vias[0].sizes),
                             {layer: mm(.6) for layer in
                              ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu")})

    def test_native_diagonal_envelopes_cover_copper_without_blocking_full_box(self):
        import pcbnew

        with tempfile.TemporaryDirectory(
            prefix="jessie-diagonal-obstacles-"
        ) as directory:
            project = Path(directory)
            source = fixtures.RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            trace = pcbnew.PCB_TRACK(board)
            trace.SetStart(pcbnew.VECTOR2I(mm(1), mm(1)))
            trace.SetEnd(pcbnew.VECTOR2I(mm(9), mm(7)))
            trace.SetWidth(mm(0.2))
            trace.SetLayer(pcbnew.F_Cu)
            trace.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            board.Add(trace)
            pcbnew.SaveBoard(str(source), board)
            boxes = [
                o
                for o in read_native_obstacles(source, "SIGNAL").obstacles
                if o.net == "SIGNAL" and o.kind == "copper"
            ]
            self.assertGreater(len(boxes), 1)
            for index in range(101):
                x = mm(1) + mm(8) * index // 100
                y = mm(1) + mm(6) * index // 100
                self.assertTrue(
                    any(
                        o.lower.x <= x <= o.upper.x and o.lower.y <= y <= o.upper.y
                        for o in boxes
                    )
                )
            self.assertFalse(
                any(
                    o.lower.x <= mm(1) <= o.upper.x and o.lower.y <= mm(7) <= o.upper.y
                    for o in boxes
                )
            )

    def test_stackup_reader_rejects_ambiguous_or_malformed_input(self):
        from pcb.route_native import _configured_copper_thicknesses

        valid = b'(kicad_pcb (setup (stackup (layer "F.Cu" (type "copper") (thickness 0.035)))))'
        self.assertEqual(_configured_copper_thicknesses(valid), (("F.Cu", 35000),))
        bad = (
            b'(kicad_pcb (setup) (setup))',
            b'(kicad_pcb (setup (stackup) (stackup)))',
            b'(kicad_pcb (setup (stackup (layer "F.Cu" (thickness 0.035)) (layer "F.Cu" (thickness 0.035)))))',
            b'(kicad_pcb (setup (stackup (layer "F.Cu"))))',
            b'(kicad_pcb (setup (stackup (layer "F.Cu" (thickness 0)))))',
            b'(kicad_pcb (setup) (broken "unterminated))',
        )
        for source in bad:
            with self.subTest(source=source), self.assertRaises(ValueError):
                _configured_copper_thicknesses(source)

    def test_saved_stackup_cannot_be_thinner_than_assessed_copper(self):
        with tempfile.TemporaryDirectory(prefix="jessie-stackup-check-") as directory:
            project = Path(directory)
            source = fixtures.RouteOptimizerTests.make_board(project)
            data = self.config()
            data["board_snapshot"] = "pcb"
            result = plan(data, project)
            self.assertEqual(result["electrical"]["status"], "within_supplied_limits")
            data["electrical"]["tracks"]["F.Cu"]["copper_thickness_mm"] = 0.070
            with self.assertRaisesRegex(ValueError, "more copper"):
                plan(data, project)
            text = source.read_text(encoding="utf-8")
            text = text.replace('(layer "F.Cu" (type "copper") (thickness 0.035))', "")
            source.write_text(text, encoding="utf-8")
            data["electrical"]["tracks"]["F.Cu"]["copper_thickness_mm"] = 0.035
            with self.assertRaisesRegex(ValueError, "no configured copper thickness"):
                plan(data, project)

    def test_cli_refuses_saving_without_electrical_limits(self):
        with tempfile.TemporaryDirectory(
            prefix="jessie-route-constraints-"
        ) as directory:
            project = Path(directory)
            fixtures.RouteOptimizerTests.make_board(project)
            data = fixtures.RouteOptimizerTests.config()
            data["board_snapshot"] = "pcb"
            config = project / "route.json"
            config.write_text(json.dumps(data), encoding="utf-8")
            stderr = io.StringIO()
            with (
                contextlib.redirect_stderr(stderr),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                status = main(
                    [
                        "--project",
                        str(project),
                        "--config",
                        str(config),
                        "--validate",
                        "--save-campaign",
                        "Must not save",
                    ]
                )
            self.assertEqual(status, 2)
            self.assertIn("electrical limits", stderr.getvalue())
            self.assertFalse((project / "config" / "campaigns").exists())


if __name__ == "__main__":
    unittest.main()
