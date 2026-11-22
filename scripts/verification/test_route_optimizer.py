"""Exercise routing search and its geometry without using a live project."""

from __future__ import annotations

import contextlib
import collections
import hashlib
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.campaign_cli import main as campaign_main
from core.kicad_campaign import CampaignFile, CampaignPlanner, digest
from pcb.route_cli import main as route_main, plan, save_validated_campaign
from pcb.route_batch import main as batch_main, plan_batch
from core.pcb_io import ProjectSettings
from core.toolchain import kicad_cli
from pcb.route_native import read_native_obstacles
from pcb.route_validation import (
    RouteContract,
    _zone_refill_changes,
    validate_route,
    validate_routes,
    verify_validation_evidence,
)
from pcb.route_optimizer import (
    GridRouter,
    NoGridRoute,
    Obstacle,
    Point,
    SearchBudgetExceeded,
    SearchLimitReached,
    Segment,
    Weights,
    mirror_points,
    mm,
)


def point(x: float, y: float) -> Point:
    return Point.from_mm([x, y])


def router(**changes) -> GridRouter:
    values = {
        "lower": point(-2, -2),
        "upper": point(12, 12),
        "step": mm(1),
        "layers": ("F.Cu", "B.Cu"),
        "net": "SIGNAL",
        "width": mm(0.2),
        "clearance": mm(0.2),
        "via_size": mm(0.6),
        "via_drill": mm(0.3),
    }
    values.update(changes)
    return GridRouter(**values)


class RouteOptimizerTests(unittest.TestCase):
    def test_centered_plated_oval_and_rectangular_pads_do_not_double_count_hole_clearance(self):
        import pcbnew
        from pcb.route_native import _pad_obstacle
        from core.pcb_io import quiet_duplicate_image_handlers
        from pcb.route_validation import _drc
        with tempfile.TemporaryDirectory() as folder, quiet_duplicate_image_handlers():
            for shape in (pcbnew.PAD_SHAPE_OVAL, pcbnew.PAD_SHAPE_RECT, pcbnew.PAD_SHAPE_ROUNDRECT):
                with self.subTest(shape=shape):
                    project = Path(folder) / str(shape)
                    project.mkdir()
                    source = self.make_board(project)
                    board = pcbnew.LoadBoard(str(source))
                    board.GetDesignSettings().m_HoleClearance = mm(.25)
                    board.GetDesignSettings().m_HoleToHoleMin = mm(.25)
                    other = pcbnew.NETINFO_ITEM(board, 'PAD_NET')
                    board.Add(other)
                    pads = []
                    for index, y in enumerate((3.73, 6.27)):
                        part = pcbnew.FOOTPRINT(board)
                        part.SetReference(f'P{index}')
                        pad = pcbnew.PAD(part)
                        pad.SetNumber('1')
                        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
                        pad.SetShape(shape)
                        pad.SetSize(pcbnew.VECTOR2I(mm(2), mm(1.524)))
                        if shape == pcbnew.PAD_SHAPE_ROUNDRECT:
                            pad.SetRoundRectRadiusRatio(.2)
                        pad.SetDrillSize(pcbnew.VECTOR2I(mm(1), mm(1)))
                        pad.SetLayerSet(pcbnew.LSET.AllCuMask())
                        pad.SetPosition(pcbnew.VECTOR2I(mm(5), mm(y)))
                        pad.SetNetCode(other.GetNetCode())
                        part.Add(pad)
                        board.Add(part)
                        pads.append(pad)
                    obstacles = tuple(_pad_obstacle(board, pad, ('F.Cu',)) for pad in pads)
                    model = router(lower=point(0, 0), upper=point(10, 8), width=mm(.5), obstacles=obstacles)
                    self.assertTrue(model.edge_clear(point(2, 5.05), point(8, 5.05), 'F.Cu'))
                    self.assertFalse(model.edge_clear(point(2, 5.15), point(8, 5.15), 'F.Cu'))
                    track = pcbnew.PCB_TRACK(board)
                    track.SetStart(pcbnew.VECTOR2I(mm(2), mm(5.05)))
                    track.SetEnd(pcbnew.VECTOR2I(mm(8), mm(5.05)))
                    track.SetWidth(mm(.5))
                    track.SetLayer(pcbnew.F_Cu)
                    track.SetNetCode(board.FindNet('SIGNAL').GetNetCode())
                    board.Add(track)
                    pcbnew.SaveBoard(str(source), board)
                    source.with_suffix('.kicad_pro').write_text('{"board":{"design_settings":{"rules":'
                        '{"min_clearance":0.2,"min_hole_clearance":0.25}}}}', encoding='utf-8')
                    report_path = project / 'drc.json'
                    report_path.unlink(missing_ok=True)
                    report = _drc(kicad_cli(), source, report_path)
                    self.assertFalse([v for v in report['violations'] if v['type'] in {'clearance', 'hole_clearance'}])
                    pad = pads[0]
                    pad.SetOffset(pcbnew.VECTOR2I(mm(.1), 0))
                    self.assertEqual(_pad_obstacle(board, pad, ('F.Cu',)).min_track_clearance, mm(.25))
                    pad.SetOffset(pcbnew.VECTOR2I(0, 0))
                    pad.SetDrillSize(pcbnew.VECTOR2I(mm(1), mm(.8)))
                    self.assertEqual(_pad_obstacle(board, pad, ('F.Cu',)).min_track_clearance, mm(.25))
                    pad.SetDrillSize(pcbnew.VECTOR2I(mm(1), mm(1)))
                    pad.SetShape(pcbnew.PAD_SHAPE_CHAMFERED_RECT)
                    self.assertEqual(_pad_obstacle(board, pad, ('F.Cu',)).min_track_clearance, mm(.25))
                    pad.SetShape(shape)
                    pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
                    self.assertGreaterEqual(_pad_obstacle(board, pad, ('F.Cu',)).min_track_clearance, mm(.25))

    def test_plated_pad_hole_margin_is_measured_from_the_drill_not_the_copper_edge(self):
        import pcbnew
        from pcb.route_native import _pad_obstacle
        from core.pcb_io import quiet_duplicate_image_handlers
        with tempfile.TemporaryDirectory() as folder, quiet_duplicate_image_handlers():
            project = Path(folder)
            source = self.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            settings = board.GetDesignSettings()
            settings.m_HoleClearance = mm(.25)
            settings.m_HoleToHoleMin = mm(.25)
            other = pcbnew.NETINFO_ITEM(board, 'PAD_NET')
            board.Add(other)
            pads = []
            for index, y in enumerate((3.73, 6.27)):
                part = pcbnew.FOOTPRINT(board)
                part.SetReference(f'P{index}')
                pad = pcbnew.PAD(part)
                pad.SetNumber('1')
                pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
                pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
                pad.SetSize(pcbnew.VECTOR2I(mm(1.524), mm(1.524)))
                pad.SetDrillSize(pcbnew.VECTOR2I(mm(1), mm(1)))
                pad.SetLayerSet(pcbnew.LSET.AllCuMask())
                pad.SetPosition(pcbnew.VECTOR2I(mm(5), mm(y)))
                pad.SetNetCode(other.GetNetCode())
                part.Add(pad)
                board.Add(part)
                pads.append(pad)
            obstacles = tuple(_pad_obstacle(board, pad, ('F.Cu',)) for pad in pads)
            model = router(lower=point(0, 0), upper=point(10, 8), width=mm(.5), obstacles=obstacles)
            self.assertTrue(model.edge_clear(point(2, 5.05), point(8, 5.05), 'F.Cu'))
            self.assertFalse(model.edge_clear(point(2, 5.15), point(8, 5.15), 'F.Cu'))
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(mm(2), mm(5.05)))
            track.SetEnd(pcbnew.VECTOR2I(mm(8), mm(5.05)))
            track.SetWidth(mm(.5))
            track.SetLayer(pcbnew.F_Cu)
            track.SetNetCode(board.FindNet('SIGNAL').GetNetCode())
            board.Add(track)
            pcbnew.SaveBoard(str(source), board)
            source.with_suffix('.kicad_pro').write_text('{"board":{"design_settings":{"rules":'
                '{"min_clearance":0.2,"min_hole_clearance":0.25}}}}', encoding='utf-8')
            from pcb.route_validation import _drc
            report = _drc(kicad_cli(), source, project / 'drc.json')
            self.assertFalse([v for v in report['violations'] if v['type'] in {'clearance', 'hole_clearance'}])

    def test_fast_warm_start_can_form_a_complete_standard_dogleg(self):
        start_wall = Obstacle(point(0, 1.7), point(5, 2), ("In3.Cu",), "OTHER", "copper")
        end_wall = Obstacle(point(9, 0), point(12, 8.5), ("In3.Cu",), "OTHER", "copper")
        model = router(lower=point(0, 0), upper=point(13, 11), step=mm(.25),
                       layers=("In3.Cu",), obstacles=(start_wall, end_wall),
                       initial_directions=frozenset({0}), final_directions=frozenset({0}),
                       first_feasible=True, forbid_staircase=True, gentle_turns=True,
                       max_expansions=1)
        try:
            result = model.route(point(1, 1), "In3.Cu", point(11, 9), "In3.Cu")
        except (NoGridRoute, SearchLimitReached) as error:
            self.fail(f"A clear standard dogleg must not require a grid search: {error}")
        self.assertFalse(result.vias)
        self.assertEqual([model._direction(s.start, s.end) for s in result.segments], [0, 1, 2, 1, 0])
        self.assertTrue(all(model.edge_clear(s.start, s.end, s.layer) for s in result.segments))
        self.assertFalse(GridRouter._has_staircase(list(result.segments)))
        self.assertEqual(result.expanded, 0)

    def test_dense_search_reuses_a_checked_blocked_edge(self):
        from pcb.net_routing.rules import RoutingRules

        rules = RoutingRules((), (), {"SIGNAL": (mm(.2), mm(.2))},
                             {"SIGNAL": "SIGNAL"}, 0, 0)
        profile = rules.profile("SIGNAL", mm(.2), mm(.2))
        obstacle = Obstacle(point(1, .95), point(1.05, 1.05), ("F.Cu",), "OTHER", "copper")
        model = router(lower=point(0, 0), upper=point(90, 90), step=mm(.1),
                       layers=("F.Cu",), max_cells=1_000_000,
                       obstacles=(obstacle,), profile=profile)
        start, end = point(1, 1), point(1.005, 1)
        with mock.patch.object(profile, "pieces", wraps=profile.pieces) as pieces:
            self.assertFalse(model.edge_clear(start, end, "F.Cu"))
            for index in range(1, 9001):
                x = mm(1) + index * 8_000
                model.edge_clear(Point(x, mm(1)), Point(x + 5_000, mm(1)), "F.Cu")
            checked = pieces.call_count
            self.assertFalse(model.edge_clear(start, end, "F.Cu"))
            self.assertEqual(pieces.call_count, checked)
        self.assertLessEqual(len(model._edge_cache), 65_536)

    def test_same_layer_endpoints_can_use_a_bounded_two_via_warm_route(self):
        wall = Obstacle(point(4, -1), point(6, 6), ("B.Cu",), "OTHER", "copper")
        model = router(lower=point(0, 0), upper=point(10, 5), step=mm(.5),
                       layers=("B.Cu", "In1.Cu"), width=mm(.1), clearance=mm(.1),
                       via_size=mm(.4), via_drill=mm(.2), obstacles=(wall,),
                       first_feasible=True, forbid_staircase=True, gentle_turns=True,
                       max_expansions=1)
        result = model.route(point(1, 2), "B.Cu", point(9, 2), "B.Cu")
        self.assertEqual(len(result.vias), 2)
        self.assertEqual(result.segments[0].start, point(1, 2))
        self.assertEqual(result.segments[-1].end, point(9, 2))
        self.assertTrue(any(segment.layer == "In1.Cu" for segment in result.segments))
        self.assertTrue(all(model.edge_clear(s.start, s.end, s.layer) for s in result.segments))
        self.assertTrue(result.search_truncated)

    def test_two_via_warm_route_obeys_reference_obstacles_and_context(self):
        wall = Obstacle(point(4, -1), point(6, 6), ("B.Cu",), "OTHER", "copper")
        reference = Obstacle(point(4, -1), point(6, 6), ("In1.Cu",), "", "keepout")
        values = dict(lower=point(0, 0), upper=point(10, 5), step=mm(.5),
                      layers=("B.Cu", "In1.Cu"), width=mm(.1), clearance=mm(.1),
                      via_size=mm(.4), via_drill=mm(.2), first_feasible=True,
                      forbid_staircase=True, gentle_turns=True, max_expansions=1)
        for changes in ({"obstacles": (wall, reference)},
                        {"obstacles": (wall,), "allow_new_vias": False}):
            with self.subTest(changes=changes):
                model = router(**values, **changes)
                with self.assertRaises((NoGridRoute, SearchLimitReached)):
                    model.route(point(1, 2), "B.Cu", point(9, 2), "B.Cu")
        model = router(**values, obstacles=(wall,))
        with self.assertRaises((NoGridRoute, SearchLimitReached)):
            model.route(point(1, 2), "B.Cu", point(9, 2), "B.Cu",
                        candidate_check=lambda _: False)

    def test_two_via_warm_route_samples_clear_off_axis_escape_sites(self):
        wall = Obstacle(point(4, -1), point(6, 6), ("B.Cu",), "OTHER", "copper")
        model = router(lower=point(0, 0), upper=point(10, 5), step=mm(.1),
                       layers=("B.Cu", "In1.Cu"), width=mm(.1), clearance=mm(.1),
                       via_size=mm(.4), via_drill=mm(.2), obstacles=(wall,),
                       first_feasible=True, forbid_staircase=True, gentle_turns=True,
                       max_expansions=1)
        allowed = {point(1.3, 2.6), point(8.7, 2.6)}
        model.via_blocked = {(x, y) for x in range(model.nx) for y in range(model.ny)
                             if model._point(x, y) not in allowed}
        result = model.route(point(1, 2), "B.Cu", point(9, 2), "B.Cu")
        self.assertEqual({via.at for via in result.vias}, allowed)
        self.assertTrue(all(model.edge_clear(s.start, s.end, s.layer) for s in result.segments))

    def test_two_via_warm_route_prefers_reusing_existing_vias(self):
        wall = Obstacle(point(4, -1), point(6, 6), ("B.Cu",), "OTHER", "copper")
        vias = tuple(Obstacle(point(x - .2, 1.8), point(x + .2, 2.2),
                              ("B.Cu", "In1.Cu"), "SIGNAL", "via") for x in (2, 8))
        for allow_new in (False, True):
            with self.subTest(allow_new_vias=allow_new):
                model = router(lower=point(0, 0), upper=point(10, 5), step=mm(.5),
                               layers=("B.Cu", "In1.Cu"), width=mm(.1), clearance=mm(.1),
                               via_size=mm(.4), via_drill=mm(.2), obstacles=(wall, *vias),
                               first_feasible=True, forbid_staircase=True, gentle_turns=True,
                               max_expansions=1, allow_new_vias=allow_new)
                result = model.route(point(1, 2), "B.Cu", point(9, 2), "B.Cu")
                self.assertFalse(result.vias)
                self.assertEqual({via.at for via in result.reused_vias}, {point(2, 2), point(8, 2)})

    def test_two_via_escape_legs_must_not_cross_or_hit_the_other_via(self):
        model = router(width=mm(.2), via_size=mm(.6))
        left = (Segment(point(1, 2), point(4, 2), "B.Cu"),)
        right = (Segment(point(7, 2), point(9, 2), "B.Cu"),)
        self.assertTrue(model._two_via_escape_clear(left, right, point(4, 2), point(7, 2)))
        crossing = (Segment(point(3, 1), point(3, 3), "B.Cu"),)
        self.assertFalse(model._two_via_escape_clear(left, crossing, point(4, 2), point(3, 1)))
        near_other_via = (Segment(point(4.3, 1), point(4.3, 3), "B.Cu"),)
        self.assertFalse(model._two_via_escape_clear(left, near_other_via, point(4, 2), point(4.3, 1)))

    def test_two_via_escape_can_detour_around_copper_on_the_transit_layer(self):
        wall = Obstacle(point(4, -1), point(6, 6), ("B.Cu",), "OTHER", "copper")
        middle = Obstacle(point(4, 1), point(6, 3), ("In1.Cu",), "OTHER", "copper")
        model = router(lower=point(0, 0), upper=point(10, 5), step=mm(.5),
                       layers=("B.Cu", "In1.Cu"), width=mm(.1), clearance=mm(.1),
                       via_size=mm(.4), via_drill=mm(.2), obstacles=(wall, middle),
                       first_feasible=True, forbid_staircase=True, gentle_turns=True,
                       max_expansions=20_000)
        allowed = {point(2, 2), point(8, 2)}
        model.via_blocked = {(x, y) for x in range(model.nx) for y in range(model.ny)
                             if model._point(x, y) not in allowed}
        result = model._direct_two_via_route(point(1, 2), point(9, 2), "B.Cu", None)
        self.assertIsNotNone(result, "Legal pad escapes need a detoured transit path, not only a straight line")
        self.assertEqual({via.at for via in result.vias}, allowed)
        self.assertTrue(all(model.edge_clear(s.start, s.end, s.layer) for s in result.segments))
        self.assertFalse(GridRouter._has_staircase(list(result.segments)))
        self.assertGreater(result.expanded, 0)

    def test_transit_detour_does_not_cross_a_reference_keepout(self):
        wall = Obstacle(point(4, -1), point(6, 6), ("B.Cu",), "OTHER", "copper")
        reference = Obstacle(point(4, -1), point(6, 6), ("In1.Cu",), "", "keepout")
        model = router(lower=point(0, 0), upper=point(10, 5), step=mm(.5),
                       layers=("B.Cu", "In1.Cu"), obstacles=(wall, reference),
                       first_feasible=True, forbid_staircase=True, gentle_turns=True)
        self.assertIsNone(model._direct_two_via_route(point(1, 2), point(9, 2), "B.Cu", None))

    def test_transit_detour_keeps_via_sites_on_grid_at_odd_nanometre_steps(self):
        step = 500_001

        def at(x, y):
            return Point(round(x * 2 * step), round(y * 2 * step))

        wall = Obstacle(at(4, -1), at(6, 6), ("B.Cu",), "OTHER", "copper")
        middle = Obstacle(at(4, 1), at(6, 3), ("In1.Cu",), "OTHER", "copper")
        model = router(lower=at(0, 0), upper=at(10, 5), step=step,
                       layers=("B.Cu", "In1.Cu"), width=mm(.1), clearance=mm(.1),
                       via_size=mm(.4), via_drill=mm(.2), obstacles=(wall, middle),
                       first_feasible=True, forbid_staircase=True, gentle_turns=True,
                       max_expansions=20_000)
        allowed = {at(2, 2), at(8, 2)}
        model.via_blocked = {(x, y) for x in range(model.nx) for y in range(model.ny)
                             if model._point(x, y) not in allowed}
        with mock.patch.object(GridRouter, "_route_off_grid", side_effect=AssertionError(
                "A transit retry must retain exact via/grid alignment and its expansion slice")):
            result = model._direct_two_via_route(at(1, 2), at(9, 2), "B.Cu", None)
        self.assertIsNotNone(result)

    def test_busy_earlier_layers_cannot_exhaust_every_transit_detour_budget(self):
        wall = Obstacle(point(4, -1), point(6, 6), ("B.Cu",), "OTHER", "copper")
        middle = Obstacle(point(4, 1), point(6, 3), ("F.Cu", "In1.Cu", "In2.Cu"), "OTHER", "copper")
        model = router(lower=point(0, 0), upper=point(10, 5), step=mm(.5),
                       layers=("B.Cu", "F.Cu", "In1.Cu", "In2.Cu"),
                       width=mm(.1), clearance=mm(.1), via_size=mm(.4), via_drill=mm(.2),
                       obstacles=(wall, middle), first_feasible=True,
                       forbid_staircase=True, gentle_turns=True, heuristic_weight=2,
                       max_expansions=80_000)
        allowed = {point(2, 2), point(8, 2)}
        model.via_blocked = {(x, y) for x in range(model.nx) for y in range(model.ny)
                             if model._point(x, y) not in allowed}
        original = GridRouter.route
        searched = []
        spent = [0]

        def limited(plane, *args, **kwargs):
            searched.append(plane.layers[0])
            if plane.layers != ("In2.Cu",):
                used = kwargs["_expansion_budget"]
                spent[0] += used
                raise SearchLimitReached("fixture: congested layer", expanded=used)
            result = original(plane, *args, **kwargs)
            spent[0] += result.expanded
            return result

        with mock.patch.object(GridRouter, "route", limited):
            result = model._direct_two_via_route(point(1, 2), point(9, 2), "B.Cu", None)
        self.assertIsNotNone(result)
        self.assertEqual(searched, ["F.Cu", "In1.Cu", "In2.Cu"])
        self.assertLessEqual(spent[0], model.max_expansions // 4)

    def test_transit_detour_checks_the_whole_route_and_restores_headings(self):
        wall = Obstacle(point(4, -1), point(6, 6), ("B.Cu",), "OTHER", "copper")
        middle = Obstacle(point(4, 1), point(6, 3), ("In1.Cu",), "OTHER", "copper")
        model = router(lower=point(0, 0), upper=point(10, 5), step=mm(.5),
                       layers=("B.Cu", "In1.Cu"), width=mm(.1), clearance=mm(.1),
                       via_size=mm(.4), via_drill=mm(.2), obstacles=(wall, middle),
                       first_feasible=True, forbid_staircase=True, gentle_turns=True,
                       initial_directions=frozenset({0}), final_directions=frozenset({0}),
                       max_expansions=20_000, heuristic_weight=2)
        allowed = {point(2, 2), point(8, 2)}
        model.via_blocked = {(x, y) for x in range(model.nx) for y in range(model.ny)
                             if model._point(x, y) not in allowed}
        checked = []

        def reject(candidate):
            checked.append(candidate)
            self.assertEqual(candidate.segments[0].start, point(1, 2))
            self.assertEqual(candidate.segments[-1].end, point(9, 2))
            self.assertEqual(len(candidate.vias), 2)
            return False

        self.assertIsNone(model._direct_two_via_route(point(1, 2), point(9, 2), "B.Cu", reject))
        self.assertTrue(checked)
        self.assertEqual(model.initial_directions, frozenset({0}))
        self.assertEqual(model.final_directions, frozenset({0}))

    def test_two_via_transit_detour_passes_native_connectivity_and_drc(self):
        import pcbnew
        from pcb.route_cli import campaign_changes

        with tempfile.TemporaryDirectory(prefix="autopcb-transit-detour-") as folder:
            source = self.make_board(Path(folder), crossing=True)
            board = pcbnew.LoadBoard(str(source))
            board.FindFootprintByReference("J2").SetPosition(pcbnew.VECTOR2I(mm(8), mm(2)))
            middle = pcbnew.PCB_TRACK(board)
            middle.SetStart(pcbnew.VECTOR2I(mm(5), mm(1)))
            middle.SetEnd(pcbnew.VECTOR2I(mm(5), mm(3)))
            middle.SetWidth(mm(.2))
            middle.SetLayer(pcbnew.B_Cu)
            middle.SetNetCode(board.FindNet("OTHER").GetNetCode())
            board.Add(middle)
            connection = pcbnew.PCB_VIA(board)
            connection.SetPosition(pcbnew.VECTOR2I(mm(5), mm(2)))
            connection.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            connection.SetWidth(mm(.6))
            connection.SetDrill(mm(.3))
            connection.SetNetCode(board.FindNet("OTHER").GetNetCode())
            board.Add(connection)
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            native = read_native_obstacles(source, "SIGNAL")
            model = router(lower=point(0, 0), upper=point(10, 8), step=mm(.25),
                           layers=("F.Cu", "B.Cu"), obstacles=native.obstacles,
                           first_feasible=True, forbid_staircase=True, gentle_turns=True,
                           max_expansions=20_000, heuristic_weight=2)
            allowed = {point(3.25, 2), point(6.75, 2)}
            model.via_blocked = {(x, y) for x in range(model.nx) for y in range(model.ny)
                                 if model._point(x, y) not in allowed}
            candidate = model._direct_two_via_route(point(2, 2), point(8, 2), "F.Cu", None)
            self.assertIsNotNone(candidate)
            changes = campaign_changes(candidate, "SIGNAL", mm(.2), mm(.6), mm(.3))
            contract = RouteContract("SIGNAL", point(2, 2), "F.Cu", point(8, 2), "F.Cu")
            checked = validate_routes(source, hashlib.sha256(original).hexdigest(), changes, [contract])
            self.assertEqual(checked["status"], "no_new_drc_violations", checked)
            self.assertEqual(checked["candidate_unconnected"], 0, checked)
            self.assertEqual(source.read_bytes(), original)

    def test_two_via_search_restores_heading_constraints_after_callback_error(self):
        wall = Obstacle(point(4, -1), point(6, 6), ("B.Cu",), "OTHER", "copper")
        model = router(lower=point(0, 0), upper=point(10, 5), step=mm(.5),
                       layers=("B.Cu", "In1.Cu"), width=mm(.1), clearance=mm(.1),
                       via_size=mm(.4), via_drill=mm(.2), obstacles=(wall,),
                       initial_directions=frozenset({0}), final_directions=frozenset({0}),
                       first_feasible=True)
        def stop(_):
            raise RuntimeError("callback interrupted")
        with self.assertRaisesRegex(RuntimeError, "callback interrupted"):
            model._direct_two_via_route(point(1, 2), point(9, 2), "B.Cu", stop)
        self.assertEqual(model.initial_directions, frozenset({0}))
        self.assertEqual(model.final_directions, frozenset({0}))

    def test_two_via_warm_route_passes_native_connectivity_and_drc(self):
        import pcbnew
        from pcb.route_cli import campaign_changes

        with tempfile.TemporaryDirectory(prefix="autopcb-two-via-native-") as folder:
            source = self.make_board(Path(folder), crossing=True)
            board = pcbnew.LoadBoard(str(source))
            board.FindFootprintByReference("J2").SetPosition(pcbnew.VECTOR2I(mm(8), mm(2)))
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            native = read_native_obstacles(source, "SIGNAL")
            model = router(lower=point(0, 0), upper=point(10, 8), step=mm(.25),
                           layers=("F.Cu", "B.Cu"), obstacles=native.obstacles,
                           first_feasible=True, forbid_staircase=True, gentle_turns=True,
                           max_expansions=1)
            candidate = model.route(point(2, 2), "F.Cu", point(8, 2), "F.Cu")
            self.assertEqual(len(candidate.vias), 2)
            changes = campaign_changes(candidate, "SIGNAL", mm(.2), mm(.6), mm(.3))
            contract = RouteContract("SIGNAL", point(2, 2), "F.Cu", point(8, 2), "F.Cu")
            checked = validate_routes(source, hashlib.sha256(original).hexdigest(), changes, [contract])
            self.assertEqual(checked["status"], "no_new_drc_violations", checked)
            self.assertEqual(checked["candidate_unconnected"], 0, checked)
            self.assertEqual(source.read_bytes(), original)

    def test_obstacle_preparation_respects_the_search_budget(self):
        calls = []

        def stop():
            calls.append(None)
            if len(calls) == 3:
                raise RuntimeError("budget expired")

        obstacles = tuple(
            Obstacle(point(i / 10, 0), point(round(i / 10 + 0.01, 2), 0.01), ("F.Cu",), "", "pad")
            for i in range(80)
        )
        with self.assertRaisesRegex(RuntimeError, "budget expired"):
            router(obstacles=obstacles, check_budget=stop)
        self.assertEqual(len(calls), 3)

    def test_large_obstacle_rasterization_can_be_interrupted(self):
        calls = []

        def stop():
            calls.append(None)
            if len(calls) == 2:
                raise RuntimeError("budget expired")

        obstacle = Obstacle(point(-2, -2), point(12, 12), ("F.Cu",), "", "pad")
        with self.assertRaisesRegex(RuntimeError, "budget expired"):
            router(
                layers=("F.Cu",), step=mm(0.05), obstacles=(obstacle,),
                check_budget=stop, max_cells=1_000_000,
            )
        self.assertEqual(len(calls), 2)

    def test_large_rectangular_mask_keeps_its_full_clearance_envelope(self):
        obstacle = Obstacle(point(-1, -1), point(11, 11), ("F.Cu",), "", "pad")
        model = router(layers=("F.Cu",), step=mm(0.05), obstacles=(obstacle,))
        mask = model.blocked["F.Cu"]
        self.assertIn((14, 20), mask)
        self.assertIn((266, 260), mask)
        self.assertNotIn((14, 14), mask)
        self.assertNotIn((266, 266), mask)
        self.assertNotIn((13, 20), mask)
        self.assertNotIn((267, 260), mask)

    def test_route_cli_keeps_real_warnings_but_filters_duplicate_image_debug(self):
        import wx

        messages = []

        class Collector(wx.Log):
            def DoLogRecord(self, level, message, info):
                messages.append((level, message))

        def fake_plan(*_args, **_kwargs):
            wx.LogDebug("Adding duplicate image handler for 'PNG file'")
            wx.LogWarning("A real KiCad warning")
            wx.Log.FlushActive()
            return {"status": "geometric_candidate_only"}

        collector = Collector()
        previous = wx.Log.SetActiveTarget(collector)
        try:
            with tempfile.TemporaryDirectory(prefix="autopcb-route-cli-") as tmp:
                config = Path(tmp) / "route.json"
                config.write_text("{}", encoding="utf-8")
                with mock.patch("pcb.route_cli.plan", side_effect=fake_plan):
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = route_main(["--project", tmp, "--config", str(config)])
        finally:
            wx.Log.SetActiveTarget(previous)
        self.assertEqual(result, 0)
        self.assertEqual(messages, [(wx.LOG_Warning, "A real KiCad warning")])

    def test_off_grid_stubs_do_not_repeat_the_same_grid_search(self):
        model = router(
            layers=("B.Cu",),
            weights=Weights(length=1, bend=1, via=10, diagonal=1),
            max_expansions=1_000_000,
        )
        calls = collections.Counter()
        original = GridRouter.route

        def counted(instance, start, start_layer, end, end_layer, **kwargs):
            if "_expansion_budget" in kwargs:
                calls[(start, start_layer, end, end_layer,
                       instance.initial_directions, instance.final_directions)] += 1
            return original(instance, start, start_layer, end, end_layer, **kwargs)

        with mock.patch.object(GridRouter, "route", counted):
            result = model.route(point(0.15, 0.2), "B.Cu", point(9.15, 8.2), "B.Cu")

        self.assertEqual(result.segments[0].start, point(0.15, 0.2))
        self.assertEqual(result.segments[-1].end, point(9.15, 8.2))
        self.assertGreater(len(calls), 1)
        self.assertEqual(max(calls.values()), 1)

    def test_one_endpoint_pair_cannot_consume_all_search_attempts(self):
        model = router(
            layers=("B.Cu",),
            weights=Weights(length=1, bend=1, via=10, diagonal=1),
            max_expansions=10000,
        )
        budgets = []
        original = GridRouter.route

        def limited_first(instance, start, start_layer, end, end_layer, **kwargs):
            if "_expansion_budget" in kwargs:
                budgets.append(kwargs["_expansion_budget"])
                if len(budgets) == 1:
                    raise SearchLimitReached("first endpoint pair", expanded=budgets[0])
            return original(instance, start, start_layer, end, end_layer, **kwargs)

        with mock.patch.object(GridRouter, "route", limited_first):
            result = model.route(point(.15, .2), "B.Cu", point(9.15, 8.2), "B.Cu")
        self.assertGreater(len(budgets), 1)
        self.assertLess(budgets[0], model.max_expansions)
        self.assertTrue(result.search_truncated)

    def test_first_feasible_gives_nearby_accesses_enough_depth_before_deadline(self):
        from pcb.route_optimizer import Route

        model = router(lower=point(-2, -2), upper=point(20, 5), layers=("F.Cu",),
                       first_feasible=True, max_expansions=1600)
        start, end = point(.1, 0), point(6, 0)
        groups = {start: [[(start, point(x, 0)) for x in range(1, 17)]], end: [[(end,)]]}
        calls = 0

        def deadline():
            nonlocal calls
            calls += 1
            if calls > 4:
                raise SearchBudgetExceeded("deadline")

        def search(first, layer, last, other_layer, *, _expansion_budget, **_kwargs):
            if _expansion_budget < 80:
                raise SearchLimitReached("pilot too shallow", expanded=_expansion_budget)
            segment = Segment(first, last, layer)
            return Route((segment,), (), (), 5.0, 0, 5.0, 80)

        model.check_budget = deadline
        with (mock.patch.object(model, "_stub_paths", return_value=[]),
              mock.patch.object(model, "_endpoint_option_groups",
                                side_effect=lambda endpoint, layer, reverse: iter(groups[endpoint])),
              mock.patch.object(model, "route", side_effect=search)):
            result = model._route_off_grid(start, "F.Cu", end, "F.Cu")
        self.assertEqual(result.segments[0].start, start)
        self.assertEqual(result.segments[-1].end, end)
        self.assertTrue(result.search_truncated)

    def test_multiple_off_grid_access_pairs_skip_repeated_topology_maps(self):
        model = router(max_expansions=100, topology_heuristic=True)
        start, end = point(.1, .1), point(4.1, 3.1)
        groups = {
            start: [[(start, point(1, .1)), (start, point(2, .1))]],
            end: [[(point(4, 3.1), end)]],
        }
        checked = []

        def limited(*_args, **_kwargs):
            checked.append(model.topology_heuristic)
            raise SearchLimitReached("bounded trial", expanded=1)

        with mock.patch.object(model, "_endpoint_option_groups",
                               side_effect=lambda endpoint, layer, reverse: iter(groups[endpoint])):
            with mock.patch.object(model, "route", side_effect=limited):
                with self.assertRaises(SearchLimitReached):
                    model._route_off_grid(start, "F.Cu", end, "B.Cu")
        self.assertEqual(checked, [False, False, False, False])
        self.assertTrue(model.topology_heuristic)

    def test_single_off_grid_access_pair_keeps_the_configured_topology(self):
        model = router(max_expansions=100, topology_heuristic=True)
        start, end = point(.1, .1), point(4.1, 3.1)
        groups = {start: [[(start, point(1, .1))]], end: [[(point(4, 3.1), end)]]}
        checked = []

        def limited(*_args, **_kwargs):
            checked.append(model.topology_heuristic)
            raise SearchLimitReached("bounded trial", expanded=1)

        with mock.patch.object(model, "_endpoint_option_groups",
                               side_effect=lambda endpoint, layer, reverse: iter(groups[endpoint])):
            with mock.patch.object(model, "route", side_effect=limited):
                with self.assertRaises(SearchLimitReached):
                    model._route_off_grid(start, "F.Cu", end, "B.Cu")
        self.assertEqual(checked, [True, True])
        self.assertTrue(model.topology_heuristic)

    def test_straight_off_grid_route_stops_at_a_proven_cost_lower_bound(self):
        model = router(layers=("B.Cu",), step=mm(0.5),
                       weights=Weights(length=1, bend=1, via=10, diagonal=1))
        result = model.route(point(0.15, 0.2), "B.Cu", point(9.15, 0.2), "B.Cu")
        self.assertEqual(result.cost, 9)
        self.assertEqual(result.expanded, 0)

    def test_straight_off_grid_route_still_tries_a_cheaper_layer(self):
        model = router(step=mm(0.5), layer_penalties={"F.Cu": 3},
                       weights=Weights(length=1, bend=1, via=0.1, diagonal=1))
        result = model.route(point(0.15, 0.2), "F.Cu", point(9.15, 0.2), "F.Cu")
        self.assertLess(result.cost, 36)
        self.assertTrue(any(segment.layer == "B.Cu" for segment in result.segments))

    def test_campaign_rejects_a_mismatched_source_hash_before_saving(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            source = project / "v4.1.kicad_pcb"
            source.write_bytes(b"saved board")
            with mock.patch("pcb.route_cli.verify_validation_evidence") as verify:
                with self.assertRaisesRegex(ValueError, "source hash must match"):
                    save_validated_campaign(project, "pcb", "test", {}, "0" * 64, [])
                verify.assert_not_called()

    def test_shortest_octilinear_route_has_one_45_degree_bend(self):
        result = router(weights=Weights(diagonal=0)).route(point(0, 0), "F.Cu", point(4, 3), "F.Cu")
        self.assertAlmostEqual(result.length_mm, 1 + 3 * math.sqrt(2))
        self.assertEqual(result.bends, 1)
        self.assertEqual(len(result.segments), 2)
        self.assertEqual(result.vias, ())
        self.assertEqual(result.segments[0].start, point(0, 0))
        self.assertEqual(result.segments[-1].end, point(4, 3))

    def test_axis_preference_allows_a_short_diagonal_and_chamfers_long_runs(self):
        model = router(
            lower=point(-1, -1),
            upper=point(11, 11),
            step=mm(0.5),
            layers=("B.Cu",),
            gentle_turns=True,
            forbid_staircase=True,
            weights=Weights(length=1, bend=1, via=10, diagonal=1),
        )
        short = model.route(point(0, 0), "B.Cu", point(1, 1), "B.Cu")
        long = model.route(point(0, 0), "B.Cu", point(10, 10), "B.Cu")
        self.assertEqual(len(short.segments), 1)
        self.assertEqual(len(long.segments), 3)
        vectors = [(s.end.x - s.start.x, s.end.y - s.start.y) for s in long.segments]
        self.assertEqual(vectors[0][1], 0)
        self.assertEqual(abs(vectors[1][0]), abs(vectors[1][1]))
        self.assertEqual(vectors[2][0], 0)

    def test_obstacle_is_avoided_and_right_angle_joints_are_forbidden(self):
        obstacle = Obstacle(point(4, 3), point(6, 7), ("F.Cu",), net="OTHER")
        result = router(obstacles=[obstacle]).route(
            point(1, 5), "F.Cu", point(9, 5), "F.Cu"
        )
        self.assertGreater(result.length_mm, 8)
        for a, b in zip(result.segments, result.segments[1:]):
            u = (a.end.x - a.start.x, a.end.y - a.start.y)
            v = (b.end.x - b.start.x, b.end.y - b.start.y)
            self.assertNotEqual(u[0] * v[0] + u[1] * v[1], 0)

    def test_staircase_count_distinguishes_chamfers_and_overlapping_windows(self):
        def chain(points):
            return [
                Segment(point(*a), point(*b), "B.Cu")
                for a, b in zip(points, points[1:])
            ]

        chamfer = chain([(0, 0), (1, 0), (2, 1), (2, 2)])
        one = chain([(0, 0), (1, 1), (2, 1), (3, 2)])
        two = chain([(0, 0), (1, 1), (2, 1), (3, 2), (4, 2), (5, 3)])
        self.assertEqual(GridRouter.staircase_count(chamfer), 0)
        self.assertEqual(GridRouter.staircase_count(one), 1)
        divided = chain([(0, 0), (0.5, 0.5), (1, 1), (2, 1), (3, 2)])
        self.assertEqual(GridRouter.staircase_count(divided), 1)
        self.assertEqual(GridRouter.staircase_count(two), 2)
        reversed_chain = [Segment(s.end, s.start, s.layer) for s in reversed(two)]
        self.assertEqual(GridRouter.staircase_count(reversed_chain), 2)
        broken = [two[0], Segment(two[1].start, two[1].end, "F.Cu"), *two[2:]]
        self.assertEqual(GridRouter.staircase_count(broken), 1)

    def test_standard_two_chamfer_dogleg_is_allowed_in_search_and_audit(self):
        from pcb.net_routing.geometry import on_segment
        from pcb.route_optimizer import _advance_staircase_history

        points = [point(*p) for p in ((1, 1), (2, 1), (3, 2), (3, 4), (4, 5), (5, 5))]
        segments = [Segment(a, b, "B.Cu") for a, b in zip(points, points[1:])]
        self.assertEqual(GridRouter.staircase_count(segments), 0)
        self.assertEqual(GridRouter.staircase_count(
            [Segment(s.end, s.start, s.layer) for s in reversed(segments)]), 0)
        memory, current = -1, -1
        for direction in (0, 1, 2, 1, 0):
            memory = _advance_staircase_history(memory, current, direction)
            self.assertIsNotNone(memory)
            current = direction
        model = router(layers=("B.Cu",), forbid_staircase=True, gentle_turns=True)
        with mock.patch.object(model, "edge_clear", side_effect=lambda a, b, _:
                               any(on_segment(a, s) and on_segment(b, s) for s in segments)):
            result = model.route(points[0], "B.Cu", points[-1], "B.Cu")
        self.assertEqual(GridRouter.staircase_count(list(result.segments)), 0)
        self.assertEqual([GridRouter._direction(s.start, s.end) for s in result.segments], [0, 1, 2, 1, 0])

    def test_standard_dogleg_does_not_need_an_extra_detour(self):
        obstacles = [
            Obstacle(point(3, 7), point(5, 8), ("B.Cu",), net="OTHER"),
            Obstacle(point(6, 4.5), point(8, 5.5), ("B.Cu",), net="OTHER"),
            Obstacle(point(8, 1), point(9, 3), ("B.Cu",), net="OTHER"),
            Obstacle(point(2, 2), point(2, 3), ("B.Cu",), net="OTHER"),
        ]
        options = {
            "lower": point(0, 0),
            "upper": point(13, 11),
            "step": mm(0.5),
            "layers": ("B.Cu",),
            "obstacles": obstacles,
            "weights": Weights(bend=0.1, diagonal=0),
        }
        unconstrained = router(**options).route(
            point(1, 1), "B.Cu", point(12, 9), "B.Cu"
        )
        constrained = router(**options, forbid_staircase=True).route(
            point(1, 1), "B.Cu", point(12, 9), "B.Cu"
        )

        def repeated_diagonal(result):
            vectors = [
                (s.end.x - s.start.x, s.end.y - s.start.y) for s in result.segments
            ]
            directions = [
                (0 if x == 0 else x // abs(x), 0 if y == 0 else y // abs(y))
                for x, y in vectors
            ]
            return any(
                a == c and all(a) and (b[0] == 0) != (b[1] == 0)
                for a, b, c in zip(directions, directions[1:], directions[2:])
            )

        self.assertTrue(repeated_diagonal(unconstrained))
        self.assertTrue(repeated_diagonal(constrained))  # The middle triplet belongs to a complete dogleg.
        self.assertFalse(GridRouter._has_staircase(list(constrained.segments)))
        self.assertEqual(constrained.segments[0].start, point(1, 1))
        self.assertEqual(constrained.segments[-1].end, point(12, 9))
        self.assertEqual(constrained.cost, unconstrained.cost)
        with self.assertRaisesRegex(ValueError, "forbid_staircase"):
            router(forbid_staircase="yes")

    def test_non_dogleg_ladder_still_requires_an_alternative_route(self):
        options = dict(lower=point(-1, -1), upper=point(7, 6), step=mm(.5), layers=("B.Cu",),
                       weights=Weights(bend=.1, diagonal=0), initial_directions=(1,), final_directions=(1,))
        old = router(**options).route(point(0, 0), "B.Cu", point(5, 3), "B.Cu")
        fixed = router(**options, forbid_staircase=True).route(point(0, 0), "B.Cu", point(5, 3), "B.Cu")
        self.assertTrue(GridRouter._has_staircase(list(old.segments)))
        self.assertFalse(GridRouter._has_staircase(list(fixed.segments)))
        self.assertGreater(fixed.cost, old.cost)

    def test_heading_and_bend_spacing_constraints_keep_a_route_editable(self):
        model = router(
            lower=point(-2, -2),
            upper=point(15, 15),
            step=mm(0.5),
            layers=("B.Cu",),
            gentle_turns=True,
            forbid_staircase=True,
            min_segment_length=mm(1),
            initial_directions=frozenset({0}),
            final_directions=frozenset({2}),
        )
        result = model.route(point(0, 0), "B.Cu", point(8, 6), "B.Cu")
        directions = [
            model._direction(segment.start, segment.end) for segment in result.segments
        ]
        self.assertEqual(directions[0], 0)
        self.assertEqual(directions[-1], 2)
        spaced = router(
            lower=point(-2, -2),
            upper=point(15, 15),
            step=mm(0.5),
            layers=("B.Cu",),
            gentle_turns=True,
            forbid_staircase=True,
            min_segment_length=mm(1),
            initial_directions=frozenset({0}),
            final_directions=frozenset({2}),
            enforce_endpoint_spacing=True,
        ).route(point(0, 0), "B.Cu", point(8, 6), "B.Cu")
        last = spaced.segments[-1]
        self.assertGreaterEqual(
            math.hypot(last.end.x - last.start.x, last.end.y - last.start.y), mm(1)
        )
        self.assertTrue(
            all((b - a) % 8 in {0, 1, 7} for a, b in zip(directions, directions[1:]))
        )
        for segment in result.segments[:-1]:
            self.assertGreaterEqual(
                math.hypot(
                    segment.end.x - segment.start.x, segment.end.y - segment.start.y
                ),
                mm(1),
            )
        invalid = self.config()
        invalid["initial_directions"] = ["upward"]
        with self.assertRaisesRegex(ValueError, "compass headings"):
            plan(invalid, Path("."))

    def test_through_via_checks_unused_inner_layers(self):
        obstacle = Obstacle(point(4, 4), point(6, 6), ("In1.Cu",), net="OTHER")
        model = router(obstacles=[obstacle])
        self.assertIn(model._index(point(5, 5)), model.via_blocked)
        result = model.route(point(3, 5), "F.Cu", point(7, 5), "B.Cu")
        self.assertEqual(len(result.vias), 1)
        self.assertNotEqual(result.vias[0].at, point(5, 5))

    def test_same_net_pads_allow_tracks_but_not_vias_in_pad(self):
        pad = Obstacle(point(4, 4), point(6, 6), ("F.Cu",), net="SIGNAL", kind="pad")
        model = router(obstacles=[pad])
        cell = model._index(point(5, 5))
        self.assertNotIn(cell, model.blocked["F.Cu"])
        self.assertIn(cell, model.via_blocked)

    def test_round_through_hole_does_not_block_a_clear_via_at_its_box_corner(self):
        import pcbnew

        with tempfile.TemporaryDirectory() as folder:
            source = self.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            board.SetCopperLayerCount(4)
            other = pcbnew.NETINFO_ITEM(board, "OTHER")
            board.Add(other)
            footprint = pcbnew.FOOTPRINT(board)
            footprint.SetReference("J3")
            center = pcbnew.VECTOR2I(mm(4), mm(4))
            footprint.SetPosition(center)
            pad = pcbnew.PAD(footprint)
            pad.SetNumber("1")
            pad.SetPosition(center)
            pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
            pad.SetSize(pcbnew.VECTOR2I(mm(1.524), mm(1.524)))
            pad.SetDrillSize(pcbnew.VECTOR2I(mm(1.016), mm(1.016)))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
            pad.SetLayerSet(pcbnew.LSET.AllCuMask())
            pad.SetNetCode(other.GetNetCode())
            footprint.Add(pad)
            board.Add(footprint)
            pcbnew.SaveBoard(str(source), board)
            snapshot = read_native_obstacles(source, "SIGNAL", assume_refill=True)
            obstacle = next(o for o in snapshot.obstacles if o.net == "OTHER" and o.kind == "pad")
            clear = point(5.24, 3.39)
            too_close = point(5.2, 4)
            self.assertFalse(GridRouter._intersects_box(clear, clear, obstacle, mm(.5)))
            self.assertTrue(GridRouter._intersects_box(too_close, too_close, obstacle, mm(.5)))
            self.assertEqual(set(obstacle.layers), set(("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")))
            from pcb.route_validation import _drc

            via = pcbnew.PCB_VIA(board)
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetWidth(mm(.6))
            via.SetDrill(mm(.3))
            via.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            board.Add(via)
            cli = kicad_cli()
            for index, (position, expected) in enumerate(((clear, False), (too_close, True))):
                via.SetPosition(pcbnew.VECTOR2I(position.x, position.y))
                pcbnew.SaveBoard(str(source), board)
                report = _drc(cli, source, Path(folder) / f"drc-{index}.json")
                pair = {via.m_Uuid.AsString(), pad.m_Uuid.AsString()}
                conflicts = [item for item in report["violations"]
                             if item["type"] == "clearance"
                             and pair <= {entry.get("uuid") for entry in item.get("items", [])}]
                self.assertEqual(bool(conflicts), expected, report)

    def test_rectangular_pad_keeps_a_conservative_corner_envelope(self):
        import pcbnew
        from pcb.route_native import _pad_obstacle

        board = pcbnew.BOARD()
        footprint = pcbnew.FOOTPRINT(board)
        pad = pcbnew.PAD(footprint)
        pad.SetPosition(pcbnew.VECTOR2I(mm(4), mm(4)))
        pad.SetShape(pcbnew.PAD_SHAPE_RECT)
        pad.SetSize(pcbnew.VECTOR2I(mm(1.524), mm(1.524)))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(pcbnew.LSET.FrontMask())
        footprint.Add(pad)
        board.Add(footprint)
        obstacle = _pad_obstacle(board, pad, ("F.Cu",))
        corner = point(5.24, 3.39)
        self.assertTrue(GridRouter._intersects_box(corner, corner, obstacle, mm(.5)))

    def test_rounded_pad_corners_match_kicad_clearance_on_both_axes(self):
        import pcbnew
        from pcb.route_validation import _drc

        with tempfile.TemporaryDirectory() as folder:
            source = self.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            board.SetCopperLayerCount(4)
            board.Add(pcbnew.NETINFO_ITEM(board, "OTHER"))
            footprint = pcbnew.FOOTPRINT(board)
            footprint.SetReference("J3")
            center = pcbnew.VECTOR2I(mm(4), mm(4))
            footprint.SetPosition(center)
            pad = pcbnew.PAD(footprint)
            pad.SetNumber("1")
            pad.SetPosition(center)
            pad.SetShape(pcbnew.PAD_SHAPE_ROUNDRECT)
            pad.SetSize(pcbnew.VECTOR2I(mm(2), mm(1)))
            pad.SetRoundRectRadiusRatio(.25)
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pcbnew.LSET.FrontMask())
            pad.SetNetCode(board.FindNet("OTHER").GetNetCode())
            footprint.Add(pad)
            board.Add(footprint)
            via = pcbnew.PCB_VIA(board)
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetWidth(mm(.6))
            via.SetDrill(mm(.3))
            via.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            board.Add(via)
            cli = kicad_cli()
            cases = (
                (0, point(5.3, 3.2), point(5.25, 3.25)),
                (90, point(4.8, 2.7), point(4.75, 2.75)),
            )
            for angle, clear, close in cases:
                with self.subTest(angle=angle):
                    pad.SetOrientationDegrees(angle)
                    pcbnew.SaveBoard(str(source), board)
                    snapshot = read_native_obstacles(source, "SIGNAL", assume_refill=True)
                    obstacle = next(o for o in snapshot.obstacles if o.net == "OTHER" and o.kind == "pad")
                    self.assertFalse(GridRouter._intersects_box(clear, clear, obstacle, mm(.5)))
                    self.assertTrue(GridRouter._intersects_box(close, close, obstacle, mm(.5)))
                    model = router(lower=point(0, 0), upper=point(8, 8), step=mm(.05))
                    mask = model._covered(obstacle, mm(.5))
                    checked_board = pcbnew.LoadBoard(str(source))
                    checked_pad = next(iter(checked_board.FindFootprintByReference("J3").Pads()))
                    native_shape = checked_pad.GetEffectiveShape(pcbnew.F_Cu)
                    for x in range(50, 120):
                        for y in range(50, 120):
                            cell = (x, y)
                            at = model._point(*cell)
                            blocked = GridRouter._intersects_box(at, at, obstacle, mm(.5))
                            self.assertEqual(cell in mask, blocked)
                            if native_shape.Collide(pcbnew.VECTOR2I(at.x, at.y), mm(.5)):
                                self.assertTrue(blocked, f"KiCad collision missed at {at}")
                    for index, (position, expected) in enumerate(((clear, False), (close, True))):
                        via.SetPosition(pcbnew.VECTOR2I(position.x, position.y))
                        pcbnew.SaveBoard(str(source), board)
                        report = _drc(cli, source, Path(folder) / f"round-{angle}-{index}.json")
                        pair = {via.m_Uuid.AsString(), pad.m_Uuid.AsString()}
                        conflicts = [item for item in report["violations"]
                                     if item["type"] == "clearance"
                                     and pair <= {entry.get("uuid") for entry in item.get("items", [])}]
                        self.assertEqual(bool(conflicts), expected, report)

    def test_oval_pad_uses_rounded_envelope_only_for_supported_rotation(self):
        import pcbnew
        from pcb.route_native import _pad_obstacle

        board = pcbnew.BOARD()
        footprint = pcbnew.FOOTPRINT(board)
        pad = pcbnew.PAD(footprint)
        pad.SetPosition(pcbnew.VECTOR2I(mm(4), mm(4)))
        pad.SetShape(pcbnew.PAD_SHAPE_OVAL)
        pad.SetSize(pcbnew.VECTOR2I(mm(2), mm(1)))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(pcbnew.LSET.FrontMask())
        footprint.Add(pad)
        board.Add(footprint)
        oval = _pad_obstacle(board, pad, ("F.Cu",))
        self.assertGreater(oval.corner_radius, 0)
        self.assertFalse(GridRouter._intersects_box(point(5.3, 3.2), point(5.3, 3.2), oval, mm(.5)))
        self.assertTrue(GridRouter._intersects_box(point(5.2, 3.3), point(5.2, 3.3), oval, mm(.5)))
        pad.SetOrientationDegrees(45)
        self.assertEqual(_pad_obstacle(board, pad, ("F.Cu",)).corner_radius, 0)

    def test_rounded_pad_obstacles_conservatively_cover_native_shapes(self):
        import pcbnew
        import random
        from pcb.route_native import _pad_obstacle

        rng = random.Random(917)
        for shape in (pcbnew.PAD_SHAPE_OVAL, pcbnew.PAD_SHAPE_ROUNDRECT):
            for angle in (0, 90, 45):
                board = pcbnew.BOARD()
                footprint = pcbnew.FOOTPRINT(board)
                pad = pcbnew.PAD(footprint)
                pad.SetPosition(pcbnew.VECTOR2I(mm(4), mm(4)))
                pad.SetSize(pcbnew.VECTOR2I(mm(2), mm(1)))
                pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
                pad.SetLayerSet(pcbnew.LSET.FrontMask())
                footprint.Add(pad)
                board.Add(footprint)
                pad.SetShape(shape)
                if shape == pcbnew.PAD_SHAPE_ROUNDRECT:
                    pad.SetRoundRectRadiusRatio(.25)
                pad.SetOrientationDegrees(angle)
                obstacle = _pad_obstacle(board, pad, ("F.Cu",))
                native = pad.GetEffectiveShape(pcbnew.F_Cu)
                for _ in range(2500):
                    x, y = rng.randrange(mm(2), mm(6)), rng.randrange(mm(2), mm(6))
                    radius = rng.randrange(0, mm(.5))
                    a = Point(x, y)
                    b = Point(x + rng.choice((-1, 0, 1)) * 100_000,
                              y + rng.choice((-1, 0, 1)) * 100_000)
                    native_segment = pcbnew.SEG(x, y, b.x, b.y)
                    if native.Collide(native_segment, radius):
                        self.assertTrue(
                            GridRouter._intersects_box(a, b, obstacle, radius),
                            f"KiCad collision missed for shape {shape}, angle {angle}",
                        )

    def test_npth_slot_uses_board_edge_clearance_during_search(self):
        import pcbnew
        from pcb.route_native import _pad_obstacle

        board = pcbnew.BOARD()
        rules = board.GetDesignSettings()
        rules.m_CopperEdgeClearance = mm(.5)
        rules.m_HoleClearance = mm(.25)
        rules.m_HoleToHoleMin = mm(.25)
        footprint = pcbnew.FOOTPRINT(board)
        pad = pcbnew.PAD(footprint)
        pad.SetPosition(pcbnew.VECTOR2I(mm(4), mm(4)))
        pad.SetShape(pcbnew.PAD_SHAPE_OVAL)
        pad.SetSize(pcbnew.VECTOR2I(mm(1), mm(.65)))
        pad.SetDrillSize(pcbnew.VECTOR2I(mm(1), mm(.65)))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
        pad.SetLayerSet(pcbnew.LSET.AllCuMask())
        footprint.Add(pad)
        board.Add(footprint)
        obstacle = _pad_obstacle(board, pad, ("F.Cu", "B.Cu"))
        model = router(
            lower=point(0, 0), upper=point(8, 8), step=mm(.05),
            layers=("B.Cu",), obstacles=(obstacle,),
        )
        # At y=3.1, a 0.2 mm trace is 0.475 mm from the slot:
        # above normal 0.2 mm copper clearance, below the 0.5 mm edge rule.
        self.assertFalse(model.edge_clear(point(3, 3.1), point(5, 3.1), "B.Cu"))
        self.assertTrue(model.edge_clear(point(3, 2.8), point(5, 2.8), "B.Cu"))
        self.assertIn(model._index(point(4, 3.1)), model.blocked["B.Cu"])

    def test_vias_also_respect_the_hole_to_hole_rule(self):
        import pcbnew
        import types
        from pcb.net_routing.backend import NativeBackend
        from pcb.route_native import _pad_obstacle

        board = pcbnew.BOARD()
        rules = board.GetDesignSettings()
        rules.m_CopperEdgeClearance = mm(.2)
        rules.m_HoleClearance = mm(.1)
        rules.m_HoleToHoleMin = mm(.8)
        footprint = pcbnew.FOOTPRINT(board)
        pad = pcbnew.PAD(footprint)
        pad.SetPosition(pcbnew.VECTOR2I(mm(4), mm(4)))
        pad.SetShape(pcbnew.PAD_SHAPE_OVAL)
        pad.SetSize(pcbnew.VECTOR2I(mm(1), mm(.65)))
        pad.SetDrillSize(pcbnew.VECTOR2I(mm(1), mm(.65)))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
        pad.SetLayerSet(pcbnew.LSET.AllCuMask())
        footprint.Add(pad)
        board.Add(footprint)
        obstacle = _pad_obstacle(board, pad, ("F.Cu", "B.Cu"))
        model = router(lower=point(0, 0), upper=point(8, 8), step=mm(.1),
                       layers=("B.Cu",), obstacles=(obstacle,))
        close, clear = point(4, 2.8), point(4, 2.2)
        self.assertIn(model._index(close), model.via_blocked)
        self.assertNotIn(model._index(clear), model.via_blocked)
        self.assertTrue(model.edge_clear(point(3, 3.1), point(5, 3.1), "B.Cu"))
        settings = types.SimpleNamespace(
            size=mm(.6), clearance=mm(.2), lower=point(0, 0), upper=point(8, 8)
        )
        net = types.SimpleNamespace(name="SIGNAL")
        self.assertFalse(NativeBackend._plane_via_clear(net, close, settings, (obstacle,)))
        self.assertTrue(NativeBackend._plane_via_clear(net, clear, settings, (obstacle,)))

    def test_existing_via_drill_rules_are_used_for_track_and_via_search(self):
        import pcbnew
        from pcb import route_native

        converter = getattr(route_native, "_via_obstacle", None)
        self.assertTrue(callable(converter), "Existing via drills need native clearance")
        board = pcbnew.BOARD()
        rules = board.GetDesignSettings()
        rules.m_HoleClearance = mm(.5)
        rules.m_HoleToHoleMin = mm(.8)
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(mm(4), mm(4)))
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetWidth(mm(.6))
        via.SetDrill(mm(.3))
        board.Add(via)
        obstacle = converter(board, via, ("F.Cu", "B.Cu"))
        model = router(lower=point(0, 0), upper=point(8, 8), step=mm(.1),
                       layers=("B.Cu",), obstacles=(obstacle,))
        self.assertFalse(model.edge_clear(point(3, 3.3), point(5, 3.3), "B.Cu"))
        self.assertTrue(model.edge_clear(point(3, 3.2), point(5, 3.2), "B.Cu"))
        self.assertIn(model._index(point(4, 2.9)), model.via_blocked)
        self.assertNotIn(model._index(point(4, 2.5)), model.via_blocked)

    def test_rectangular_clearance_uses_rounded_corners_for_edges_and_grid_masks(self):
        obstacle = Obstacle(point(0, 0), point(1, 1), ("F.Cu",), "OTHER", "pad")
        clear, close = point(1.2, 1.2), point(1.17, 1.17)
        self.assertFalse(GridRouter._intersects_box(clear, clear, obstacle, mm(.25)))
        self.assertTrue(GridRouter._intersects_box(close, close, obstacle, mm(.25)))
        model = router(layers=("F.Cu",), step=mm(.05), obstacles=(obstacle,))
        self.assertNotIn(model._index(point(1.25, 1.25)), model.blocked["F.Cu"])
        self.assertIn(model._index(point(1.2, 1.2)), model.blocked["F.Cu"])
        self.assertNotIn(model._index(point(1.4, 1.4)), model.via_blocked)
        self.assertIn(model._index(point(1.35, 1.35)), model.via_blocked)

    def test_rectangular_grid_mask_agrees_with_point_clearance_checks(self):
        import random

        rng = random.Random(37)
        model = router(layers=("F.Cu",), step=mm(.25))
        for _ in range(8):
            x, y = rng.randrange(-4, 20) * 125_000, rng.randrange(-4, 20) * 125_000
            width, height = rng.randrange(1, 12) * 125_000, rng.randrange(1, 12) * 125_000
            obstacle = Obstacle(Point(x, y), Point(x + width, y + height), ("F.Cu",), "N", "pad")
            margin = rng.randrange(0, 6) * 125_000
            mask = model._covered(obstacle, margin)
            for ix in range(model.nx):
                for iy in range(model.ny):
                    p = model._point(ix, iy)
                    self.assertEqual((ix, iy) in mask, GridRouter._intersects_box(p, p, obstacle, margin))

    def test_rectangular_pad_corner_clearance_matches_native_drc(self):
        import pcbnew
        from pcb.route_validation import _drc

        with tempfile.TemporaryDirectory() as folder:
            source = self.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            board.SetCopperLayerCount(4)
            other = pcbnew.NETINFO_ITEM(board, "OTHER")
            board.Add(other)
            footprint = pcbnew.FOOTPRINT(board)
            footprint.SetReference("J3")
            center = pcbnew.VECTOR2I(mm(4), mm(4))
            footprint.SetPosition(center)
            pad = pcbnew.PAD(footprint)
            pad.SetNumber("1")
            pad.SetPosition(center)
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(pcbnew.VECTOR2I(mm(1.524), mm(1.524)))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pcbnew.LSET.FrontMask())
            pad.SetNetCode(other.GetNetCode())
            footprint.Add(pad)
            board.Add(footprint)
            pcbnew.SaveBoard(str(source), board)
            snapshot = read_native_obstacles(source, "SIGNAL", assume_refill=True)
            obstacle = next(o for o in snapshot.obstacles if o.net == "OTHER" and o.kind == "pad")
            via = pcbnew.PCB_VIA(board)
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetWidth(mm(.6))
            via.SetDrill(mm(.3))
            via.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            board.Add(via)
            cli = kicad_cli()
            for index, (position, expected) in enumerate(((point(5.12, 5.12), False),
                                                           (point(5.10, 5.10), True))):
                self.assertEqual(GridRouter._intersects_box(position, position, obstacle, mm(.5)), expected)
                via.SetPosition(pcbnew.VECTOR2I(position.x, position.y))
                pcbnew.SaveBoard(str(source), board)
                report = _drc(cli, source, Path(folder) / f"rect-drc-{index}.json")
                pair = {via.m_Uuid.AsString(), pad.m_Uuid.AsString()}
                conflicts = [item for item in report["violations"]
                             if item["type"] == "clearance"
                             and pair <= {entry.get("uuid") for entry in item.get("items", [])}]
                self.assertEqual(bool(conflicts), expected, report)

    def test_existing_same_net_via_is_reused_without_a_new_via(self):
        via = Obstacle(
            point(4.7, 4.7), point(5.3, 5.3), ("F.Cu", "B.Cu"), "SIGNAL", "via"
        )
        result = router(obstacles=[via]).route(point(3, 5), "F.Cu", point(7, 5), "B.Cu")
        self.assertEqual(result.vias, ())
        self.assertEqual(len(result.reused_vias), 1)
        self.assertEqual(result.reused_vias[0].at, point(5, 5))
        self.assertEqual(result.length_mm, 4)

    def test_a_via_does_not_create_a_planar_bend_between_layers(self):
        via = Obstacle(
            point(4.7, 4.7), point(5.3, 5.3), ("F.Cu", "B.Cu"), "SIGNAL", "via"
        )
        model = router(
            obstacles=[via], allow_new_vias=False,
            initial_directions=frozenset({0}), final_directions=frozenset({2}),
            weights=Weights(length=1, bend=1, via=10, diagonal=1),
            gentle_turns=True,
        )
        for start, end in ((point(3, 5), point(5, 7)),
                           (point(3.1, 5), point(5, 7.1))):
            with self.subTest(start=start, end=end):
                result = model.route(start, "F.Cu", end, "B.Cu")
                self.assertEqual(result.segments, (
                    Segment(start, point(5, 5), "F.Cu"),
                    Segment(point(5, 5), end, "B.Cu"),
                ))
                self.assertAlmostEqual(result.length_mm, 4.0)
                self.assertEqual(result.bends, 0)
                self.assertEqual(len(result.reused_vias), 1)
                self.assertFalse(result.vias)

    def test_a_via_cannot_be_double_crossed_to_evade_a_bend_rule(self):
        via = Obstacle(
            point(4.7, 4.7), point(5.3, 5.3), ("F.Cu", "B.Cu"), "SIGNAL", "via"
        )
        model = router(
            obstacles=[via], allow_new_vias=False,
            weights=Weights(length=1, bend=1, via=10, diagonal=1),
            gentle_turns=True,
        )
        result = model.route(point(3, 5), "F.Cu", point(5, 7), "F.Cu")
        self.assertFalse(result.reused_vias)
        self.assertTrue(all(
            (model._direction(b.start, b.end) - model._direction(a.start, a.end)) % 8 in {0, 1, 7}
            for a, b in zip(result.segments, result.segments[1:])
        ))

    def test_a_via_at_an_endpoint_does_not_impose_a_cross_layer_heading(self):
        via = Obstacle(
            point(4.7, 4.7), point(5.3, 5.3), ("F.Cu", "B.Cu"), "SIGNAL", "via"
        )
        model = router(
            obstacles=[via], allow_new_vias=False,
            initial_directions=frozenset({0}), final_directions=frozenset({2}),
            weights=Weights(length=1, bend=1, via=10, diagonal=1),
            gentle_turns=True,
        )
        for start in (point(3, 5), point(3.1, 5)):
            with self.subTest(start=start):
                result = model.route(start, "F.Cu", point(5, 5), "B.Cu")
                self.assertEqual(result.segments, (Segment(start, point(5, 5), "F.Cu"),))
                self.assertEqual(result.bends, 0)
                self.assertEqual(len(result.reused_vias), 1)
        result = model.route(point(5, 5), "F.Cu", point(5, 7), "B.Cu")
        self.assertEqual(result.segments, (Segment(point(5, 5), point(5, 7), "B.Cu"),))
        self.assertEqual(result.bends, 0)
        self.assertEqual(len(result.reused_vias), 1)

    def test_perpendicular_layers_at_an_existing_via_pass_native_connectivity(self):
        import pcbnew
        from pcb.route_cli import campaign_changes

        with tempfile.TemporaryDirectory(prefix="autopcb-via-heading-") as folder:
            source = self.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            destination = board.FindFootprintByReference("J2")
            destination.SetPosition(pcbnew.VECTOR2I(mm(5), mm(5)))
            destination.Pads()[0].SetLayerSet(pcbnew.LSET.BackMask())
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(pcbnew.VECTOR2I(mm(5), mm(2)))
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetWidth(mm(.6))
            via.SetDrill(mm(.3))
            via.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            board.Add(via)
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()

            via_obstacle = Obstacle(
                point(4.7, 1.7), point(5.3, 2.3), ("F.Cu", "B.Cu"), "SIGNAL", "via"
            )
            model = router(
                obstacles=[via_obstacle], allow_new_vias=False,
                initial_directions=frozenset({0}), final_directions=frozenset({2}),
                weights=Weights(length=1, bend=1, via=10, diagonal=1),
                gentle_turns=True,
            )
            candidate = model.route(point(2, 2), "F.Cu", point(5, 5), "B.Cu")
            self.assertEqual(candidate.bends, 0)
            changes = campaign_changes(candidate, "SIGNAL", mm(.2), mm(.6), mm(.3))
            contract = RouteContract("SIGNAL", point(2, 2), "F.Cu", point(5, 5), "B.Cu")
            checked = validate_routes(
                source, hashlib.sha256(original).hexdigest(), changes, [contract]
            )
            self.assertEqual(checked["status"], "no_new_drc_violations", checked)
            self.assertEqual(checked["candidate_unconnected"], 0, checked)
            self.assertEqual(source.read_bytes(), original)

    def test_off_grid_search_reports_when_the_budget_was_exhausted(self):
        model = router(
            max_expansions=1,
            weights=Weights(symmetry=1),
            guide=(point(0, 1), point(5, 1)),
        )
        result = model.route(point(0.1, 0.2), "F.Cu", point(4.15, 3.25), "F.Cu")
        self.assertTrue(result.search_truncated)
        self.assertEqual(result.expanded, 1)

    def test_grid_failure_is_not_reported_as_physical_impossibility(self):
        wall = Obstacle(point(4, -2), point(6, 12), ("F.Cu", "B.Cu"), net="OTHER")
        with self.assertRaises(NoGridRoute):
            router(obstacles=[wall]).route(point(1, 5), "F.Cu", point(9, 5), "F.Cu")
        with self.assertRaises(SearchLimitReached):
            router(max_expansions=1).route(point(0, 0), "F.Cu", point(9, 9), "F.Cu")

    def test_off_grid_search_does_not_hide_unexpected_value_errors(self):
        model = router()
        with mock.patch.object(model, "route", side_effect=ValueError("unexpected rule failure")):
            with self.assertRaisesRegex(ValueError, "unexpected rule failure"):
                model._route_off_grid(point(.1, .1), "F.Cu", point(4.1, 3.1), "B.Cu")

    def test_bad_grid_and_via_dimensions_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            router().route(point(-3, 0), "F.Cu", point(4, 3), "F.Cu")
        with self.assertRaisesRegex(ValueError, "Via size"):
            router(via_size=mm(0.3), via_drill=mm(0.3))
        with self.assertRaisesRegex(ValueError, "max_cells"):
            router(max_cells=10)
        with self.assertRaises(ValueError):
            mm(float("nan"))

    def test_off_grid_endpoints_are_reached_exactly_with_legal_turns(self):
        start, end = point(0.1, 0.2), point(4.15, 3.25)
        result = router().route(start, "F.Cu", end, "F.Cu")
        self.assertEqual(result.segments[0].start, start)
        self.assertEqual(result.segments[-1].end, end)
        self.assertLessEqual(len(result.segments), 2)
        directions = [router()._direction(s.start, s.end) for s in result.segments]
        self.assertTrue(
            all(
                (b - a) % 8 in {0, 1, 3, 5, 7}
                for a, b in zip(directions, directions[1:])
            )
        )

    def test_first_feasible_finds_a_direct_chamfer_without_grid_expansions(self):
        for start, end in ((point(0, 0), point(4, 4)), (point(.05, .05), point(4.05, 4.05))):
            with self.subTest(start=start):
                model = router(layers=("B.Cu",), step=mm(.5), max_expansions=1,
                               first_feasible=True, gentle_turns=True, forbid_staircase=True,
                               min_segment_length=mm(.2), enforce_endpoint_spacing=True,
                               initial_directions=frozenset({0}), final_directions=frozenset({2}))
                result = model.route(start, "B.Cu", end, "B.Cu")
                self.assertEqual(result.expanded, 0)
                self.assertTrue(result.search_truncated)
                self.assertEqual([model._direction(s.start, s.end) for s in result.segments], [0, 1, 2])
                self.assertTrue(all(model.edge_clear(s.start, s.end, s.layer) for s in result.segments))
                self.assertEqual(model.staircase_count(list(result.segments)), 0)

    def test_direct_chamfer_works_in_every_quadrant(self):
        for sx, sy, initial, final in ((1, 1, 0, 2), (-1, 1, 4, 2),
                                       (-1, -1, 4, 6), (1, -1, 0, 6)):
            with self.subTest(sx=sx, sy=sy):
                model = router(lower=point(-8, -8), upper=point(8, 8), layers=("F.Cu",),
                               step=mm(.5), max_expansions=1, first_feasible=True,
                               initial_directions=frozenset({initial}), final_directions=frozenset({final}),
                               gentle_turns=True, forbid_staircase=True)
                result = model.route(point(0, 0), "F.Cu", point(4 * sx, 4 * sy), "F.Cu")
                directions = [model._direction(seg.start, seg.end) for seg in result.segments]
                self.assertEqual((directions[0], directions[-1]), (initial, final))
                self.assertTrue(all((b - a) % 8 in {1, 7} for a, b in zip(directions, directions[1:])))
                self.assertEqual(result.expanded, 0)

    def test_first_feasible_direct_chamfer_still_checks_obstacles_and_context(self):
        obstacle = Obstacle(point(1, -1.5), point(3, 5), ("B.Cu",), "OTHER")
        options = dict(layers=("B.Cu",), step=mm(.5), max_expansions=1,
                       first_feasible=True, initial_directions=frozenset({0}),
                       final_directions=frozenset({2}))
        with self.assertRaises((NoGridRoute, SearchLimitReached)):
            router(obstacles=(obstacle,), **options).route(point(0, 0), "B.Cu", point(4, 4), "B.Cu")
        with self.assertRaises((NoGridRoute, SearchLimitReached)):
            router(**options).route(point(0, 0), "B.Cu", point(4, 4), "B.Cu",
                                    candidate_check=lambda _route: False)

    def test_direct_chamfer_respects_minimum_segment_length(self):
        model = router(layers=("B.Cu",), step=mm(.5), max_expansions=1, first_feasible=True,
                       min_segment_length=mm(2), enforce_endpoint_spacing=True,
                       initial_directions=frozenset({0}), final_directions=frozenset({2}))
        result = model.route(point(0, 0), "B.Cu", point(4, 4), "B.Cu")
        self.assertTrue(all(math.hypot(s.end.x - s.start.x, s.end.y - s.start.y) >= mm(2)
                            for s in result.segments))

    def test_first_feasible_returns_checked_direct_access_without_more_grid_search(self):
        start, end = point(0.1, 0.2), point(4.15, 3.25)
        model = router(first_feasible=True, gentle_turns=True, forbid_staircase=True)
        with mock.patch.object(model, "_endpoint_option_groups", side_effect=AssertionError("extra search")):
            result = model.route(start, "F.Cu", end, "F.Cu")
        self.assertEqual(result.segments[0].start, start)
        self.assertEqual(result.segments[-1].end, end)
        self.assertTrue(result.search_truncated)
        self.assertTrue(all(model.edge_clear(s.start, s.end, s.layer) for s in result.segments))
        self.assertEqual(model.staircase_count(list(result.segments)), 0)

    def test_first_feasible_cross_layer_access_can_use_one_checked_via(self):
        start, end = point(0.1, 0.2), point(4.15, 3.25)
        model = router(step=mm(.5), first_feasible=True, max_expansions=1,
                       gentle_turns=True, forbid_staircase=True)
        with mock.patch.object(model, "_route_off_grid", side_effect=AssertionError("extra grid search")):
            result = model.route(start, "F.Cu", end, "B.Cu")
        self.assertEqual(len(result.vias), 1)
        self.assertEqual(result.segments[0].start, start)
        self.assertEqual(result.segments[-1].end, end)
        self.assertTrue(result.search_truncated)
        self.assertTrue(all(model.edge_clear(s.start, s.end, s.layer) for s in result.segments))

    def test_direct_cross_layer_access_respects_via_permissions_and_all_layers(self):
        start, end = point(0.1, 0.2), point(4.15, 3.25)
        blocked_layer = Obstacle(point(-2, -2), point(12, 12), ("In1.Cu",), "", "keepout")
        for options in ({"allow_new_vias": False}, {"obstacles": (blocked_layer,)}):
            model = router(step=mm(.5), first_feasible=True, max_expansions=1, **options)
            with self.subTest(options=options), self.assertRaises((NoGridRoute, SearchLimitReached)):
                model.route(start, "F.Cu", end, "B.Cu")

    def test_direct_cross_layer_access_can_reuse_an_existing_via(self):
        start, end, via = point(0.1, 0.2), point(4.15, 3.25), point(2, 2)
        existing = Obstacle(point(1.7, 1.7), point(2.3, 2.3), ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
                            "SIGNAL", "via")
        model = router(step=mm(.5), first_feasible=True, max_expansions=1,
                       allow_new_vias=False, allowed_reused_vias={via}, obstacles=(existing,))
        result = model.route(start, "F.Cu", end, "B.Cu")
        self.assertEqual(len(result.vias), 0)
        self.assertEqual([item.at for item in result.reused_vias], [via])

    def test_cross_layer_warm_start_honors_context_and_restores_headings(self):
        initial, final = frozenset({0}), frozenset({2})
        model = router(step=mm(.5), first_feasible=True, max_expansions=1,
                       initial_directions=initial, final_directions=final)
        checked = []

        def reject(candidate):
            self.assertEqual((model.initial_directions, model.final_directions), (initial, final))
            checked.append(candidate)
            return False

        with mock.patch.object(model, "_route_off_grid", side_effect=SearchLimitReached("limit")):
            with self.assertRaises(SearchLimitReached):
                model.route(point(0.1, 0.2), "F.Cu", point(4.15, 3.25), "B.Cu",
                            candidate_check=reject)
        self.assertTrue(checked)
        self.assertEqual((model.initial_directions, model.final_directions), (initial, final))

    def test_cross_layer_warm_start_is_not_repeated_inside_grid_search(self):
        model = router(step=mm(.5), first_feasible=True, max_expansions=1)
        with mock.patch.object(model, "_direct_one_via_route", side_effect=AssertionError("repeat")):
            with self.assertRaises((NoGridRoute, SearchLimitReached)):
                model.route(point(0, 0), "F.Cu", point(4, 4), "B.Cu", _expansion_budget=1)

    def test_first_feasible_still_routes_around_obstacles(self):
        obstacle = Obstacle(point(1.5, 0.5), point(2.5, 2.5), ("F.Cu",), net="OTHER")
        model = router(step=mm(0.5), obstacles=[obstacle], first_feasible=True)
        start, end = point(0.1, 0.2), point(4.15, 3.25)
        result = model.route(start, "F.Cu", end, "F.Cu")
        self.assertEqual(result.segments[0].start, start)
        self.assertEqual(result.segments[-1].end, end)
        self.assertTrue(all(model.edge_clear(s.start, s.end, s.layer) for s in result.segments))

    def test_first_feasible_checks_the_existing_join_before_selecting_an_access(self):
        start, end = point(0.1, 0.2), point(4.15, 3.25)
        model = router(first_feasible=True, gentle_turns=True, forbid_staircase=True)
        checked = []

        def compatible(candidate):
            heading = model._direction(candidate.segments[0].start, candidate.segments[0].end)
            checked.append(heading)
            return heading == 1

        result = model.route(start, "F.Cu", end, "F.Cu", candidate_check=compatible)
        self.assertGreaterEqual(len(checked), 2)
        self.assertEqual(checked[0], 0)
        self.assertNotIn(1, checked[:-1])
        self.assertEqual(checked[-1], 1)
        self.assertEqual(model._direction(result.segments[0].start, result.segments[0].end), 1)

    def test_on_grid_search_tries_another_route_when_the_join_is_rejected(self):
        model = router(layers=("F.Cu",), gentle_turns=True, forbid_staircase=True)
        seen = []

        def compatible(candidate):
            seen.append(candidate)
            return len(candidate.segments) > 1

        result = model.route(point(0, 0), "F.Cu", point(4, 0), "F.Cu", candidate_check=compatible)
        self.assertGreaterEqual(len(seen), 2)
        self.assertGreater(len(result.segments), 1)
        self.assertTrue(all(model.edge_clear(s.start, s.end, s.layer) for s in result.segments))

    def test_rejected_on_grid_context_is_not_reported_as_physical_impossibility(self):
        model = router(layers=("F.Cu",), max_expansions=20)
        with self.assertRaises(SearchLimitReached):
            model.route(point(0, 0), "F.Cu", point(4, 0), "F.Cu", candidate_check=lambda _: False)

    def test_exhausted_context_goals_remain_an_unknown_result(self):
        model = router(lower=point(-1, -1), upper=point(3, 3), layers=("F.Cu",),
                       max_expansions=50_000)
        with self.assertRaisesRegex(SearchLimitReached, "unsearched alternatives"):
            model.route(point(0, 0), "F.Cu", point(2, 0), "F.Cu", candidate_check=lambda _: False)

    def test_unexpected_context_errors_propagate(self):
        def fail(_candidate):
            raise ValueError("context bug")

        with self.assertRaisesRegex(ValueError, "context bug"):
            router().route(point(0, 0), "F.Cu", point(2, 0), "F.Cu", candidate_check=fail)

    def test_on_grid_clear_straight_path_needs_no_search(self):
        model = router()
        result = model.route(point(0, 1), "F.Cu", point(8, 1), "F.Cu")
        self.assertEqual(result.segments, (Segment(point(0, 1), point(8, 1), "F.Cu"),))
        self.assertEqual(result.expanded, 0)
        self.assertFalse(result.search_truncated)

    def test_on_grid_shortcut_obeys_obstacles_and_endpoint_headings(self):
        obstacle = Obstacle(point(3, 0), point(5, 2), ("F.Cu",), net="OTHER")
        model = router(obstacles=[obstacle], initial_directions=frozenset({2}))
        result = model.route(point(0, 1), "F.Cu", point(8, 1), "F.Cu")
        self.assertEqual(model._direction(result.segments[0].start, result.segments[0].end), 2)
        self.assertTrue(all(model.edge_clear(s.start, s.end, s.layer) for s in result.segments))
        self.assertGreater(result.expanded, 0)

    def test_first_feasible_is_a_validated_route_option(self):
        data = self.config()
        data.update(first_feasible=True, start={"at_mm": [2.03, 2.0], "layer": "F.Cu"})
        result = plan(data, Path("."))
        self.assertTrue(result["first_feasible"])
        self.assertTrue(result["search_truncated"])
        data["first_feasible"] = "yes"
        with self.assertRaisesRegex(ValueError, "first_feasible"):
            plan(data, Path("."))

    def test_off_grid_search_expands_after_the_nearest_access_is_trapped(self):
        blocked = [(-20, -20), (-20, 0), (-20, 20), (0, -20), (0, 20),
                   (20, -20), (20, 0), (20, 20), (60, 4)]
        obstacles = [
            Obstacle(point(x - .2, y - .2), point(x + .2, y + .2),
                     ("F.Cu",), net="OTHER", kind="pad")
            for x, y in blocked
        ]
        model = router(
            lower=point(-40, -40), upper=point(140, 80), step=mm(20),
            layers=("F.Cu",), obstacles=obstacles, width=mm(.2), clearance=mm(.2),
            gentle_turns=True, forbid_staircase=True,
        )
        for start, end in ((point(4, 4), point(100, 0)),
                           (point(100, 0), point(4, 4))):
            with self.subTest(start=start, end=end):
                result = model.route(start, "F.Cu", end, "F.Cu")
                self.assertEqual(result.segments[0].start, start)
                self.assertEqual(result.segments[-1].end, end)
                self.assertTrue(all(model.edge_clear(s.start, s.end, s.layer) for s in result.segments))
                self.assertEqual(model.staircase_count(list(result.segments)), 0)
                headings = [model._direction(s.start, s.end) for s in result.segments]
                self.assertTrue(all((b - a) % 8 in {0, 1, 7}
                                    for a, b in zip(headings, headings[1:])))

    def test_off_grid_context_rejection_tries_other_paths_at_same_access(self):
        model = router(
            lower=point(-2, -2), upper=point(8, 5), step=mm(1), layers=("F.Cu",),
            width=mm(.1), clearance=mm(.05), first_feasible=True,
            gentle_turns=True, forbid_staircase=True, max_expansions=100_000,
        )
        start, end = point(.2, .2), point(4.2, .8)
        groups = {start: [[(start, point(1, 1))]], end: [[(point(4, 1), end)]]}

        def usable(route):
            return not any(s.start.y == s.end.y == mm(1) for s in route.segments)

        with mock.patch.object(model, "_stub_paths", return_value=[]), mock.patch.object(
            model, "_endpoint_option_groups",
            side_effect=lambda endpoint, layer, reverse: iter(groups[endpoint]),
        ):
            result = model._route_off_grid(start, "F.Cu", end, "F.Cu", candidate_check=usable)
        self.assertTrue(usable(result))
        self.assertEqual(result.segments[0].start, start)
        self.assertEqual(result.segments[-1].end, end)
        self.assertFalse(model._has_staircase(result.segments))

    def test_off_grid_exhausted_context_is_unknown_not_impossible(self):
        model = router(
            lower=point(-2, -2), upper=point(8, 5), step=mm(1), layers=("F.Cu",),
            width=mm(.1), clearance=mm(.05), gentle_turns=True,
            forbid_staircase=True, max_expansions=10_000,
        )
        start, end = point(.2, .2), point(4.2, .8)
        groups = {start: [[(start, point(1, 1))]], end: [[(point(4, 1), end)]]}
        with mock.patch.object(model, "_stub_paths", return_value=[]), mock.patch.object(
            model, "_endpoint_option_groups",
            side_effect=lambda endpoint, layer, reverse: iter(groups[endpoint]),
        ):
            with self.assertRaises(SearchLimitReached):
                model._route_off_grid(start, "F.Cu", end, "F.Cu", candidate_check=lambda _: False)

    def test_off_grid_invalid_escape_join_tries_another_grid_path_without_callback(self):
        model = router(
            lower=point(-5, -5), upper=point(10, 10), step=mm(1), layers=("F.Cu",),
            width=mm(.1), clearance=mm(.05), first_feasible=True,
            gentle_turns=True, forbid_staircase=True, max_expansions=100_000,
        )
        start, end = point(.2, .2), point(4.8, 1.8)
        groups = {start: [[(start, point(1, 1))]], end: [[(point(4, 1), end)]]}
        with mock.patch.object(model, "_stub_paths", return_value=[]), mock.patch.object(
            model, "_endpoint_option_groups",
            side_effect=lambda endpoint, layer, reverse: iter(groups[endpoint]),
        ):
            result = model._route_off_grid(start, "F.Cu", end, "F.Cu")
        self.assertEqual(result.segments[0].start, start)
        self.assertEqual(result.segments[-1].end, end)
        self.assertFalse(model._has_staircase(result.segments))

    def test_off_grid_access_pair_cap_reports_an_unknown_result(self):
        model = router(max_expansions=3)
        start, end = point(0, 0), point(9, 0)
        groups = {
            start: [[(start, point(1, 0))],
                    [(start, point(x, 0)) for x in range(2, 7)]],
            end: [[(end,)]],
        }
        with mock.patch.object(
            model, "_endpoint_option_groups",
            side_effect=lambda endpoint, layer, reverse: iter(groups[endpoint]),
        ):
            with mock.patch.object(model, "route", side_effect=NoGridRoute("blocked")):
                with self.assertRaises(SearchLimitReached):
                    model._route_off_grid(start, "F.Cu", end, "B.Cu")

    def test_off_grid_escape_avoids_a_foreign_obstacle(self):
        obstacle = Obstacle(point(1.5, 0.5), point(2.5, 2.5), ("F.Cu",), net="OTHER")
        model = router(step=mm(0.5), obstacles=[obstacle])
        start, end = point(0.1, 0.2), point(4.15, 3.25)
        result = model.route(start, "F.Cu", end, "F.Cu")
        self.assertEqual(result.segments[0].start, start)
        self.assertEqual(result.segments[-1].end, end)
        margin = model.clearance + math.ceil(model.width / 2)
        self.assertTrue(
            all(
                not model._intersects_box(s.start, s.end, obstacle, margin)
                for s in result.segments
                if s.layer == "F.Cu"
            )
        )

    def test_off_grid_escape_can_reach_beyond_the_four_nearest_grid_vertices(self):
        obstacles = [
            Obstacle(point(x - 0.04, y - 0.04), point(x + 0.04, y + 0.04),
                     ("B.Cu",), net="OTHER")
            for x, y in ((5, 5), (5, 6), (6, 5), (6, 6), (7.5, 5.5))
        ]
        model = router(lower=point(0, 0), upper=point(12, 12), layers=("B.Cu",),
                       width=mm(0.2), clearance=mm(0.05), obstacles=obstacles)
        start, end = point(5.5, 5.5), point(9, 5.5)
        try:
            result = model.route(start, "B.Cu", end, "B.Cu")
        except NoGridRoute as exc:
            self.fail(f"A legal escape exists beyond the nearest grid vertices: {exc}")
        self.assertEqual(result.segments[0].start, start)
        self.assertEqual(result.segments[-1].end, end)
        self.assertFalse(result.vias)
        self.assertTrue(all(model.edge_clear(s.start, s.end, s.layer) for s in result.segments))
        directions = [model._direction(s.start, s.end) for s in result.segments]
        self.assertTrue(all((b - a) % 8 in {0, 1, 3, 5, 7}
                            for a, b in zip(directions, directions[1:])))

    def test_endpoint_access_can_escape_beyond_the_old_distance_limit(self):
        obstacles = (
            Obstacle(point(.2, .2), point(2, 1.03), ("B.Cu",), net="OTHER"),
            Obstacle(point(.2, 1.07), point(2, 1.9), ("B.Cu",), net="OTHER"),
        )
        model = router(
            lower=point(0, 0), upper=point(4, 4), step=mm(.1), layers=("B.Cu",),
            width=mm(.01), clearance=mm(.005), via_size=mm(.1), via_drill=mm(.05),
            obstacles=obstacles,
        )
        start = point(1.05, 1.05)
        self.assertTrue(model.edge_clear(start, point(2.05, 1.05), "B.Cu"))
        self.assertTrue(model.edge_clear(point(2.05, 1.05), point(2.1, 1), "B.Cu"))
        options = next(model._endpoint_option_groups(start, "B.Cu", reverse=False), [])
        self.assertTrue(options, "A clear corridor extends beyond the old 0.6 mm limit")
        self.assertTrue(all(max(abs(path[-1].x - start.x), abs(path[-1].y - start.y))
                            > mm(.6) for path in options))
        for path in options:
            self.assertTrue(all(model.edge_clear(a, b, "B.Cu") for a, b in zip(path, path[1:])))

    def test_endpoint_inside_minimum_foreign_clearance_skips_the_grid_scan(self):
        obstacle = Obstacle(point(.95, .95), point(1.15, 1.15), ("B.Cu",), net="OTHER")
        model = router(lower=point(0, 0), upper=point(4, 4), step=mm(.1),
                       layers=("B.Cu",), obstacles=(obstacle,))
        with mock.patch.object(model, "_stub_paths", side_effect=AssertionError("unnecessary scan")):
            self.assertEqual(next(model._endpoint_option_groups(point(1.05, 1.05), "B.Cu", reverse=False), []), [])

    def test_endpoint_precheck_uses_rule_minima_not_nominal_widths(self):
        from types import SimpleNamespace

        obstacle = Obstacle(point(1, 1), point(1.1, 1.1), ("B.Cu",), net="OTHER")
        profile = SimpleNamespace(
            minimum_width=mm(.1), minimum_clearance=mm(.05), maximum_clearance=mm(.2),
            pieces=lambda segment: ((segment, mm(.2)),), clearance_at=lambda segment, other: mm(.2),
        )
        model = router(lower=point(0, 0), upper=point(4, 4), step=mm(.1),
                       layers=("B.Cu",), obstacles=(obstacle,), profile=profile)
        with mock.patch.object(model, "_stub_paths", side_effect=RuntimeError("examined")):
            with self.assertRaisesRegex(RuntimeError, "examined"):
                next(model._endpoint_option_groups(point(1.3, 1.05), "B.Cu", reverse=False), [])

    def test_minimum_stub_leg_is_a_bound_for_all_sampled_grid_escapes(self):
        model = router(lower=Point(0, 0), upper=Point(100, 100), step=10,
                       layers=("B.Cu",), width=1, clearance=0, via_size=10, via_drill=5)
        for x in range(10):
            for y in range(10):
                endpoint = Point(40 + x, 40 + y)
                if x == y == 0:
                    continue
                minimum = model._minimum_stub_leg(endpoint)
                for gx in range(1, 9):
                    for gy in range(1, 9):
                        for path in model._stub_paths(endpoint, Point(gx * 10, gy * 10)):
                            if len(path) < 2:
                                continue
                            first, second = path[:2]
                            length = max(abs(second.x - first.x), abs(second.y - first.y))
                            self.assertGreaterEqual(length, minimum, (endpoint, path, minimum))

    def test_endpoint_trapped_before_any_possible_first_leg_skips_grid_scan(self):
        obstacles = (
            Obstacle(point(1.015, 1.015), point(1.02, 1.085), ("B.Cu",), net="OTHER"),
            Obstacle(point(1.08, 1.015), point(1.085, 1.085), ("B.Cu",), net="OTHER"),
            Obstacle(point(1.015, 1.015), point(1.085, 1.02), ("B.Cu",), net="OTHER"),
            Obstacle(point(1.015, 1.08), point(1.085, 1.085), ("B.Cu",), net="OTHER"),
        )
        model = router(lower=point(0, 0), upper=point(4, 4), step=mm(.1), layers=("B.Cu",),
                       width=mm(.01), clearance=mm(.005), obstacles=obstacles)
        endpoint = point(1.05, 1.05)
        self.assertFalse(model._endpoint_inside_minimum_foreign_clearance(endpoint, "B.Cu"))
        with mock.patch.object(model, "_stub_paths", side_effect=AssertionError("unnecessary scan")):
            self.assertEqual(next(model._endpoint_option_groups(endpoint, "B.Cu", reverse=False), []), [])

    def test_extended_endpoint_access_obeys_the_search_deadline(self):
        from pcb.route_optimizer import SearchBudgetExceeded

        obstacles = (
            Obstacle(point(.2, .2), point(2, 1.03), ("B.Cu",), net="OTHER"),
            Obstacle(point(.2, 1.07), point(2, 1.9), ("B.Cu",), net="OTHER"),
        )
        model = router(
            lower=point(0, 0), upper=point(4, 4), step=mm(.1), layers=("B.Cu",),
            width=mm(.01), clearance=mm(.005), via_size=mm(.1), via_drill=mm(.05),
            obstacles=obstacles,
        )
        with mock.patch.object(
            model, "check_budget",
            side_effect=[None, None, SearchBudgetExceeded("deadline")],
        ), self.assertRaisesRegex(SearchBudgetExceeded, "deadline"):
            next(model._endpoint_option_groups(point(1.05, 1.05), "B.Cu", reverse=False), [])

    def test_fine_grid_escape_uses_a_physical_search_distance(self):
        obstacles = (
            Obstacle(point(4.5, 5.25), point(6, 5.4), ("B.Cu",), net="OTHER"),
            Obstacle(point(4.5, 5.65), point(6, 5.8), ("B.Cu",), net="OTHER"),
            Obstacle(point(6.46, 5.485), point(6.54, 5.565), ("B.Cu",), net="OTHER"),
        )
        model = router(lower=point(4, 5), upper=point(8, 6), step=mm(0.05),
                       layers=("B.Cu",), width=mm(0.1), clearance=mm(0.05),
                       obstacles=obstacles, gentle_turns=True, forbid_staircase=True)
        start, end = point(5.525, 5.525), point(7, 5.525)
        try:
            result = model.route(start, "B.Cu", end, "B.Cu")
        except NoGridRoute as exc:
            self.fail(f"A legal fine-grid corridor extends beyond two cells: {exc}")
        self.assertEqual(result.segments[0].start, start)
        self.assertEqual(result.segments[-1].end, end)
        self.assertTrue(all(model.edge_clear(s.start, s.end, s.layer) for s in result.segments))
        self.assertFalse(model._has_staircase(result.segments))

    def test_off_grid_stubs_apply_direction_rules_at_the_real_endpoints(self):
        obstacle = Obstacle(point(3.9, 1.9), point(4.1, 2.1), ("B.Cu",), net="OTHER")
        cases = (
            (point(2.2, 2.2), point(6.2, 2.2), {"initial_directions": frozenset({0})}),
            (point(6.2, 2.2), point(2.2, 2.2), {"final_directions": frozenset({4})}),
        )
        for start, end, headings in cases:
            with self.subTest(headings=headings):
                model = router(lower=point(0, 0), upper=point(8, 5), layers=("B.Cu",),
                               width=mm(0.2), clearance=mm(0.1), obstacles=(obstacle,),
                               gentle_turns=True, forbid_staircase=True, **headings)
                try:
                    result = model.route(start, "B.Cu", end, "B.Cu")
                except NoGridRoute as exc:
                    self.fail(f"A legal endpoint-stub turn exists: {exc}")
                directions = [model._direction(s.start, s.end) for s in result.segments]
                if "initial_directions" in headings:
                    self.assertIn(directions[0], headings["initial_directions"])
                if "final_directions" in headings:
                    self.assertIn(directions[-1], headings["final_directions"])
                self.assertTrue(all((b - a) % 8 in {0, 1, 7}
                                    for a, b in zip(directions, directions[1:])))
                self.assertFalse(model._has_staircase(result.segments))
                self.assertTrue(all(model.edge_clear(s.start, s.end, s.layer) for s in result.segments))
                self.assertEqual(model.initial_directions, headings.get("initial_directions"))
                self.assertEqual(model.final_directions, headings.get("final_directions"))

    def test_symmetry_guide_and_mirroring_are_available(self):
        self.assertEqual(
            mirror_points([point(1, 3), point(2, 4)], "x", mm(5)),
            (point(9, 3), point(8, 4)),
        )
        model = router(weights=Weights(symmetry=1), guide=(point(0, 1), point(5, 1)))
        result = model.route(point(0, 0), "F.Cu", point(5, 2), "F.Cu")
        self.assertGreater(result.cost, result.length_mm)

    def test_layer_penalties_prefer_bottom_when_benefit_outweighs_vias(self):
        model = router(layer_penalties={"F.Cu": 2}, weights=Weights(via=0.1))
        result = model.route(point(0, 0), "F.Cu", point(8, 0), "F.Cu")
        self.assertTrue(any(segment.layer == "B.Cu" for segment in result.segments))
        self.assertEqual(len(result.vias), 2)
        with self.assertRaisesRegex(ValueError, "Layer penalties"):
            router(layer_penalties={"F.Cu": -1})

    def test_cli_rejects_unknown_or_misleading_configuration(self):
        data = self.config()
        data["weigths"] = {}
        with self.assertRaisesRegex(ValueError, "Unknown"):
            plan(data, Path("."))
        data = self.config()
        data["weights"] = {"symmetry": 1}
        with self.assertRaisesRegex(ValueError, "requires a guide"):
            plan(data, Path("."))

    def test_refill_cannot_change_a_silkscreen_zone(self):
        before = b'(kicad_pcb (zone (layer "F.SilkS") (uuid "zone") (fill)))'
        after = b'(kicad_pcb (zone (layer "F.SilkS") (uuid "zone") (fill yes) (filled_polygon (pts (xy 1 1)))))'
        with self.assertRaisesRegex(RuntimeError, "non-copper zone"):
            _zone_refill_changes(before, after)

    @staticmethod
    def config() -> dict:
        return {
            "net": "SIGNAL",
            "bounds_mm": [0, 0, 10, 8],
            "step_mm": 0.25,
            "start": {"at_mm": [2, 2], "layer": "F.Cu"},
            "end": {"at_mm": [8, 5], "layer": "F.Cu"},
            "layers": ["F.Cu", "B.Cu"],
            "width_mm": 0.2,
            "clearance_mm": 0.2,
        }

    def test_route_plan_obeys_a_shared_search_deadline(self):
        def expired():
            raise SearchBudgetExceeded("cleanup budget reached")

        with self.assertRaisesRegex(SearchBudgetExceeded, "cleanup budget reached"):
            plan(self.config(), Path("."), check_budget=expired)

    def test_route_plan_checks_deadline_during_grid_search(self):
        calls = 0

        def expire_during_search():
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise SearchBudgetExceeded("cleanup budget reached")

        data = self.config()
        data["start"]["at_mm"] = [2.03, 2.0]
        result = plan(data, Path("."), check_budget=expire_during_search)
        self.assertTrue(result["search_truncated"])
        self.assertGreaterEqual(calls, 2)

    @staticmethod
    def electrical() -> dict:
        """Synthetic limits for software tests only; not board ratings."""
        track = {
            "min_width_mm": 0.2,
            "max_current_a": 1,
            "copper_thickness_mm": 0.035,
            "basis": "synthetic test",
        }
        return {
            "current_a": 0.1,
            "current_basis": "synthetic test",
            "voltage_difference_v": 5,
            "voltage_basis": "synthetic test",
            "clearance": {
                "min_mm": 0.2,
                "max_voltage_difference_v": 5,
                "basis": "synthetic test",
            },
            "voltage_drop": {
                "max_v": 1,
                "copper_resistivity_ohm_mm": 0.00002,
                "basis": "synthetic test",
            },
            "tracks": {"F.Cu": dict(track), "B.Cu": dict(track)},
            "via": {
                "min_size_mm": 0.6,
                "min_drill_mm": 0.3,
                "plating_thickness_mm": 0.025,
                "max_current_a": 1,
                "max_resistance_ohm": 0.1,
                "basis": "synthetic test",
            },
        }

    def test_default_cost_charges_diagonal_length(self):
        data = self.config()
        data["start"] = {"at_mm": [2, 2], "layer": "B.Cu"}
        data["end"] = {"at_mm": [3, 3], "layer": "B.Cu"}
        result = plan(data, Path("."))
        self.assertAlmostEqual(result["cost"], 2 * math.sqrt(2), places=5)

    def test_default_standalone_route_avoids_staircases(self):
        from pcb.route_optimizer import is_staircase_triplet

        data = self.config()
        data.update(weights={"diagonal": 1}, initial_directions=["SE"], final_directions=["SE"])
        result = plan(data, Path("."))
        directions = [
            GridRouter._direction(Point.from_mm(s["from_mm"]), Point.from_mm(s["to_mm"]))
            for s in result["segments"]
        ]
        self.assertFalse(any(is_staircase_triplet(*window)
                             for window in zip(directions, directions[1:], directions[2:])))

    def test_default_standalone_route_uses_gentle_turns(self):
        data = self.config()
        data.update(
            bounds_mm=[0, 0, 8, 8], step_mm=.5, layers=["B.Cu"],
            start={"at_mm": [2, 2], "layer": "B.Cu"},
            end={"at_mm": [6, 5], "layer": "B.Cu"},
            weights={"diagonal": 1}, initial_directions=["E"], final_directions=["W"],
        )
        result = plan(data, Path("."))
        directions = [
            GridRouter._direction(Point.from_mm(s["from_mm"]), Point.from_mm(s["to_mm"]))
            for s in result["segments"]
        ]
        self.assertTrue(all((b - a) % 8 in {0, 1, 7}
                            for a, b in zip(directions, directions[1:])))

    def test_default_cost_is_equal_on_all_four_copper_layers(self):
        costs = {}
        for layer in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"):
            data = self.config()
            data.pop("layers")
            data["start"] = {"at_mm": [2, 2], "layer": layer}
            data["end"] = {"at_mm": [8, 2], "layer": layer}
            costs[layer] = plan(data, Path("."))["cost"]
        self.assertEqual(len(set(costs.values())), 1, costs)

    def test_through_via_cost_is_equal_for_any_layer_pair(self):
        costs = {}
        layers = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
        for start_layer in layers:
            for end_layer in layers:
                if start_layer == end_layer:
                    continue
                data = self.config()
                data.pop("layers")
                data["start"] = {"at_mm": [2, 2], "layer": start_layer}
                data["end"] = {"at_mm": [8, 2], "layer": end_layer}
                result = plan(data, Path("."))
                self.assertEqual(result["via_count"], 1)
                costs[start_layer, end_layer] = result["cost"]
        self.assertEqual(len(set(costs.values())), 1, costs)

    def test_default_route_layers_include_in1(self):
        data = self.config()
        data.pop("layers")
        data["start"] = {"at_mm": [2, 2], "layer": "In1.Cu"}
        data["end"] = {"at_mm": [8, 2], "layer": "In1.Cu"}
        result = plan(data, Path("."))
        self.assertTrue(result["segments"])
        self.assertEqual({item["layer"] for item in result["segments"]}, {"In1.Cu"})

    def test_four_layer_snapshot_exposes_every_copper_layer(self):
        import pcbnew

        with tempfile.TemporaryDirectory(prefix="route-four-layer-test-") as directory:
            project = Path(directory)
            source = self.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            board.SetCopperLayerCount(4)
            pcbnew.SaveBoard(str(source), board)
            snapshot = read_native_obstacles(source, "SIGNAL")
            self.assertEqual(set(snapshot.enabled_layers), {"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"})

    def test_snapshot_defaults_use_only_enabled_copper_layers(self):
        with tempfile.TemporaryDirectory(prefix="route-layer-test-") as directory:
            project = Path(directory)
            source = self.make_board(project)
            snapshot = read_native_obstacles(source, "SIGNAL")
            self.assertEqual(set(snapshot.enabled_layers), {"F.Cu", "B.Cu"})
            data = self.config()
            data.pop("layers")
            data["board_snapshot"] = "pcb"
            result = plan(data, project)
            self.assertTrue(result["segments"])
            self.assertLessEqual({s["layer"] for s in result["segments"]}, {"F.Cu", "B.Cu"})
            data["layers"] = ["F.Cu", "B.Cu", "In1.Cu"]
            with self.assertRaisesRegex(ValueError, "not enabled"):
                plan(data, project)

    def test_proposal_is_read_only_and_only_emits_free_tracks(self):
        with tempfile.TemporaryDirectory(prefix="jessie-route-test-") as directory:
            project = Path(directory)
            board = project / "v4.1.kicad_pcb"
            original = (
                '(kicad_pcb (version 20260206) (footprint "X" (layer "F.Cu") (at 1 1)))'
            )
            board.write_text(original)
            result = plan(self.config(), project, include_changes=True)
            self.assertEqual(result["status"], "geometric_candidate_only")
            changes = result["unvalidated_campaign_changes"]
            self.assertTrue(changes)
            self.assertTrue(
                all(
                    c["target"] == {"kind": "root"} and c["op"] == "insert"
                    for c in changes
                )
            )
            self.assertTrue(all("(segment " in c["expression"] for c in changes))
            candidate = CampaignPlanner(project).plan("pcb", changes)
            self.assertIn(b'(footprint "X" (layer "F.Cu") (at 1 1))', candidate.after)
            self.assertEqual(board.read_text(), original)

    @staticmethod
    def make_board(project: Path, *, crossing: bool = False) -> Path:
        import pcbnew

        source = project / "v4.1.kicad_pcb"
        board = pcbnew.BOARD()
        net = pcbnew.NETINFO_ITEM(board, "SIGNAL")
        board.Add(net)
        edges = ((0, 0, 10, 0), (10, 0, 10, 8), (10, 8, 0, 8), (0, 8, 0, 0))
        for x1, y1, x2, y2 in edges:
            edge = pcbnew.PCB_SHAPE(board)
            edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
            edge.SetLayer(pcbnew.Edge_Cuts)
            edge.SetStart(pcbnew.VECTOR2I(mm(x1), mm(y1)))
            edge.SetEnd(pcbnew.VECTOR2I(mm(x2), mm(y2)))
            edge.SetWidth(mm(0.05))
            board.Add(edge)
        for ref, (x, y) in (("J1", (2, 2)), ("J2", (8, 5))):
            footprint = pcbnew.FOOTPRINT(board)
            footprint.SetReference(ref)
            position = pcbnew.VECTOR2I(mm(x), mm(y))
            footprint.SetPosition(position)
            pad = pcbnew.PAD(footprint)
            pad.SetNumber("1")
            pad.SetSize(pcbnew.VECTOR2I(mm(1), mm(1)))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pcbnew.LSET.FrontMask())
            pad.SetPosition(position)
            pad.SetNetCode(net.GetNetCode())
            footprint.Add(pad)
            board.Add(footprint)
        if crossing:
            other = pcbnew.NETINFO_ITEM(board, "OTHER")
            board.Add(other)
            trace = pcbnew.PCB_TRACK(board)
            trace.SetStart(pcbnew.VECTOR2I(mm(5), mm(0.5)))
            trace.SetEnd(pcbnew.VECTOR2I(mm(5), mm(7.5)))
            trace.SetWidth(mm(0.2))
            trace.SetLayer(pcbnew.F_Cu)
            trace.SetNetCode(other.GetNetCode())
            board.Add(trace)
        ProjectSettings(source).write()
        pcbnew.SaveBoard(str(source), board)
        text = source.read_text(encoding="utf-8")
        text = text.replace(
            "(setup\n",
            '(setup\n (stackup (layer "F.Cu" (type "copper") (thickness 0.035)) '
            '(layer "B.Cu" (type "copper") (thickness 0.035)))\n',
            1,
        )
        source.write_text(text, encoding="utf-8")
        return source

    def test_native_snapshot_can_route_a_synthetic_two_pad_board(self):
        import pcbnew

        with tempfile.TemporaryDirectory(
            prefix="jessie-route-native-test-"
        ) as directory:
            project = Path(directory)
            source = self.make_board(project)
            original = source.read_bytes()
            snapshot = read_native_obstacles(source, "SIGNAL")
            self.assertEqual(len(snapshot.obstacles), 2)
            data = self.config()
            data["board_snapshot"] = "pcb"
            result = plan(data, project, include_changes=True, validate=True)
            self.assertEqual(result["source_sha256"], snapshot.source_sha256)
            self.assertEqual(
                result["validation"]["status"],
                "no_new_drc_violations",
                str(result["validation"]),
            )
            self.assertEqual(result["validation"]["endpoint_connectivity"], "connected")
            candidate = CampaignPlanner(project).plan(
                "pcb", result["unvalidated_campaign_changes"]
            )
            trial = project / "trial.kicad_pcb"
            trial.write_bytes(candidate.after)
            routed = pcbnew.LoadBoard(str(trial))
            connectivity = routed.GetConnectivity()
            connectivity.Build(routed)
            first = list(routed.FindFootprintByReference("J1").Pads())[0]
            second = list(routed.FindFootprintByReference("J2").Pads())[0]
            connected = {
                item.m_Uuid.AsString() for item in connectivity.GetConnectedItems(first)
            }
            self.assertIn(second.m_Uuid.AsString(), connected)
            self.assertEqual(source.read_bytes(), original)
            evidence = result["validation"]
            verify_validation_evidence(source, evidence, candidate.result_sha256)
            with mock.patch("pcb.route_validation._tool_inputs", return_value={}):
                with self.assertRaisesRegex(RuntimeError, "tools changed"):
                    verify_validation_evidence(
                        source, evidence, candidate.result_sha256
                    )
            store = CampaignFile(project)
            campaign = store.create("Validated test route", routing_validation=evidence)
            store.append(campaign.name, result["unvalidated_campaign_changes"])
            campaign_hash = digest(campaign.read_bytes())
            (project / "v4.1.kicad_dru").write_text("(version 1)\n")
            with self.assertRaisesRegex(RuntimeError, "inputs changed"):
                verify_validation_evidence(source, evidence, candidate.result_sha256)
            with contextlib.redirect_stderr(io.StringIO()):
                status = campaign_main(
                    [
                        "--project",
                        str(project),
                        "apply",
                        campaign.name,
                        "--document",
                        "pcb",
                        "--expected-sha256",
                        snapshot.source_sha256,
                        "--expected-campaign-sha256",
                        campaign_hash,
                    ]
                )
            self.assertEqual(status, 2)
            self.assertEqual(source.read_bytes(), original)

    def test_routing_footprint_move_requires_explicit_bottom_permission(self):
        with tempfile.TemporaryDirectory(prefix="jessie-route-move-test-") as directory:
            project = Path(directory)
            source = self.make_board(project)
            change = CampaignFile.move("pcb", "footprint", "J1", 2, 3)
            proposal = plan(self.config(), project, include_changes=True)
            changes = [change, *proposal["unvalidated_campaign_changes"]]
            contract = RouteContract("SIGNAL", point(2, 2), "F.Cu", point(8, 5), "F.Cu")
            with self.assertRaisesRegex(ValueError, "explicitly permitted"):
                validate_routes(
                    source, digest(source.read_bytes()), changes, [contract]
                )
            with self.assertRaisesRegex(ValueError, "bottom side"):
                validate_routes(
                    source,
                    digest(source.read_bytes()),
                    changes,
                    [contract],
                    bottom_footprint_moves=("J1",),
                )

    def test_routing_can_move_an_unlocked_bottom_smd_and_reconnect_it(self):
        import pcbnew

        with tempfile.TemporaryDirectory(prefix="jessie-route-move-test-") as directory:
            project = Path(directory)
            source = self.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            for reference in ("J1", "J2"):
                footprint = board.FindFootprintByReference(reference)
                footprint.SetLayer(pcbnew.B_Cu)
                footprint.Reference().SetLayer(pcbnew.B_SilkS)
                footprint.Value().SetLayer(pcbnew.B_Fab)
                for pad in footprint.Pads():
                    pad.SetLayerSet(pcbnew.LSET.BackMask())
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            changes = [CampaignFile.move("pcb", "footprint", "J1", 2, 3)]
            for index, (start, end) in enumerate((((2, 3), (6, 3)), ((6, 3), (8, 5)))):
                expression = (
                    f"(segment (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) "
                    f'(width 0.2) (layer "B.Cu") (net "SIGNAL") '
                    f'(uuid "00000000-0000-4000-8000-{index + 1:012d}"))'
                )
                changes.append(
                    {
                        "document": "pcb",
                        "target": {"kind": "root"},
                        "op": "insert",
                        "path": [],
                        "expression": expression,
                    }
                )
            contract = RouteContract("SIGNAL", point(2, 2), "B.Cu", point(8, 5), "B.Cu")
            result = validate_routes(
                source,
                digest(original),
                changes,
                [contract],
                bottom_footprint_moves=("J1",),
            )
            self.assertEqual(result["status"], "no_new_drc_violations", result)
            self.assertEqual(result["bottom_footprint_moves"], ["J1"])
            self.assertEqual(result["endpoint_connectivity"], "connected")
            self.assertEqual(source.read_bytes(), original)

    def test_routing_rejects_locked_through_hole_or_front_silk_moves(self):
        import pcbnew

        for reason in ("locked", "through_hole", "front_silk"):
            with (
                self.subTest(reason=reason),
                tempfile.TemporaryDirectory() as directory,
            ):
                project = Path(directory)
                source = self.make_board(project)
                board = pcbnew.LoadBoard(str(source))
                footprint = board.FindFootprintByReference("J1")
                footprint.SetLayer(pcbnew.B_Cu)
                footprint.Reference().SetLayer(
                    pcbnew.F_SilkS if reason == "front_silk" else pcbnew.B_SilkS
                )
                footprint.Value().SetLayer(pcbnew.B_Fab)
                if reason == "locked":
                    footprint.SetLocked(True)
                for pad in footprint.Pads():
                    pad.SetLayerSet(pcbnew.LSET.BackMask())
                    if reason == "through_hole":
                        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
                pcbnew.SaveBoard(str(source), board)
                change = CampaignFile.move("pcb", "footprint", "J1", 2, 3)
                expression = (
                    "(segment (start 2 3) (end 3 3) (width 0.2) "
                    '(layer "B.Cu") (net "SIGNAL") '
                    '(uuid "00000000-0000-4000-8000-000000000001"))'
                )
                changes = [
                    change,
                    {
                        "document": "pcb",
                        "target": {"kind": "root"},
                        "op": "insert",
                        "path": [],
                        "expression": expression,
                    },
                ]
                contract = RouteContract(
                    "SIGNAL", point(2, 2), "B.Cu", point(8, 5), "F.Cu"
                )
                with self.assertRaises(ValueError):
                    validate_routes(
                        source,
                        digest(source.read_bytes()),
                        changes,
                        [contract],
                        bottom_footprint_moves=("J1",),
                    )

    def test_off_grid_endpoints_pass_native_connectivity_and_drc(self):
        with tempfile.TemporaryDirectory(
            prefix="jessie-route-off-grid-test-"
        ) as directory:
            project = Path(directory)
            source = self.make_board(project)
            original = source.read_bytes()
            data = self.config()
            data.update(
                {
                    "board_snapshot": "pcb",
                    "start": {"at_mm": [2.03, 2.07], "layer": "F.Cu"},
                    "end": {"at_mm": [8.04, 5.09], "layer": "F.Cu"},
                }
            )
            result = plan(data, project, validate=True)
            self.assertEqual(result["validation"]["status"], "no_new_drc_violations")
            self.assertLessEqual(len(result["segments"]), 2)
            self.assertEqual(source.read_bytes(), original)

    def test_extended_off_grid_escape_passes_native_connectivity_and_drc(self):
        import pcbnew

        with tempfile.TemporaryDirectory(prefix="autopcb-extended-escape-") as directory:
            project = Path(directory)
            source = self.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            for ref, xy in (("J1", (5.5, 5.5)), ("J2", (9, 5.5))):
                footprint = board.FindFootprintByReference(ref)
                footprint.SetPosition(pcbnew.VECTOR2I(mm(xy[0]), mm(xy[1])))
                for pad in footprint.Pads():
                    pad.SetSize(pcbnew.VECTOR2I(mm(0.12), mm(0.12)))
            other = pcbnew.NETINFO_ITEM(board, "OTHER")
            board.Add(other)
            for index, (x, y) in enumerate(((5, 5), (5, 6), (6, 5), (6, 6), (7.5, 5.5))):
                footprint = pcbnew.FOOTPRINT(board)
                footprint.SetReference(f"X{index + 1}")
                at = pcbnew.VECTOR2I(mm(x), mm(y))
                footprint.SetPosition(at)
                pad = pcbnew.PAD(footprint)
                pad.SetNumber("1")
                pad.SetPosition(at)
                pad.SetSize(pcbnew.VECTOR2I(mm(0.08), mm(0.08)))
                pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
                pad.SetLayerSet(pcbnew.LSET.FrontMask())
                pad.SetNetCode(other.GetNetCode())
                footprint.Add(pad)
                board.Add(footprint)
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            data = self.config()
            data.update(board_snapshot="pcb", bounds_mm=[0, 0, 10, 8], step_mm=1,
                        layers=["F.Cu"], width_mm=0.2, clearance_mm=0.2,
                        start={"at_mm": [5.5, 5.5], "layer": "F.Cu"},
                        end={"at_mm": [9, 5.5], "layer": "F.Cu"})
            result = plan(data, project, validate=True)
            self.assertEqual(result["validation"]["status"], "no_new_drc_violations",
                             result["validation"])
            self.assertEqual(result["validation"]["endpoint_connectivity"], "connected")
            self.assertEqual(source.read_bytes(), original)

    def test_relaxed_diagonal_passes_native_connectivity_and_drc(self):
        import pcbnew

        with tempfile.TemporaryDirectory(prefix="autopcb-relaxed-diagonal-") as directory:
            project = Path(directory)
            source = self.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            for ref, xy in (("J1", (1, 1)), ("J2", (2, 2))):
                footprint = board.FindFootprintByReference(ref)
                footprint.SetPosition(pcbnew.VECTOR2I(mm(xy[0]), mm(xy[1])))
                for pad in footprint.Pads():
                    pad.SetSize(pcbnew.VECTOR2I(mm(0.2), mm(0.2)))
            other = pcbnew.NETINFO_ITEM(board, "OTHER")
            board.Add(other)
            for index, (x, y) in enumerate(((2, 1), (1, 2))):
                footprint = pcbnew.FOOTPRINT(board)
                footprint.SetReference(f"X{index + 1}")
                at = pcbnew.VECTOR2I(mm(x), mm(y))
                footprint.SetPosition(at)
                pad = pcbnew.PAD(footprint)
                pad.SetNumber("1")
                pad.SetPosition(at)
                pad.SetSize(pcbnew.VECTOR2I(mm(0.1), mm(0.1)))
                pad.SetDrillSize(pcbnew.VECTOR2I(mm(0.05), mm(0.05)))
                pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
                pad.SetLayerSet(pcbnew.LSET.AllCuMask())
                pad.SetNetCode(other.GetNetCode())
                footprint.Add(pad)
                board.Add(footprint)
            data = self.config()
            data.update(board_snapshot="pcb", bounds_mm=[0, 0, 3, 3], step_mm=1,
                        layers=["F.Cu", "B.Cu"], width_mm=0.2, clearance_mm=0.1,
                        topology_heuristic=True,
                        start={"at_mm": [1, 1], "layer": "B.Cu"},
                        end={"at_mm": [2, 2], "layer": "B.Cu"})
            # Route only on the back: the endpoint test pads occupy that layer.
            for ref in ("J1", "J2"):
                footprint = board.FindFootprintByReference(ref)
                footprint.SetLayer(pcbnew.B_Cu)
                footprint.Reference().SetLayer(pcbnew.B_SilkS)
                footprint.Value().SetLayer(pcbnew.B_Fab)
                for pad in footprint.Pads():
                    pad.SetLayerSet(pcbnew.LSET.BackMask())
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            result = plan(data, project, validate=True)
            self.assertEqual(result["validation"]["status"], "no_new_drc_violations",
                             result["validation"])
            self.assertEqual(result["validation"]["endpoint_connectivity"], "connected")
            self.assertEqual(source.read_bytes(), original)

    def test_off_grid_endpoint_heading_turn_passes_native_drc(self):
        import pcbnew

        with tempfile.TemporaryDirectory(prefix="autopcb-stub-heading-") as directory:
            project = Path(directory)
            source = self.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            for ref, xy in (("J1", (2.2, 2.2)), ("J2", (6.2, 2.2))):
                footprint = board.FindFootprintByReference(ref)
                footprint.SetLayer(pcbnew.B_Cu)
                footprint.Reference().SetLayer(pcbnew.B_SilkS)
                footprint.Value().SetLayer(pcbnew.B_Fab)
                footprint.SetPosition(pcbnew.VECTOR2I(mm(xy[0]), mm(xy[1])))
                for pad in footprint.Pads():
                    pad.SetLayerSet(pcbnew.LSET.BackMask())
            other = pcbnew.NETINFO_ITEM(board, "OTHER")
            board.Add(other)
            footprint = pcbnew.FOOTPRINT(board)
            footprint.SetReference("X1")
            footprint.SetLayer(pcbnew.B_Cu)
            footprint.Reference().SetLayer(pcbnew.B_SilkS)
            at = pcbnew.VECTOR2I(mm(4), mm(2))
            footprint.SetPosition(at)
            pad = pcbnew.PAD(footprint)
            pad.SetNumber("1")
            pad.SetPosition(at)
            pad.SetSize(pcbnew.VECTOR2I(mm(0.2), mm(0.2)))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pcbnew.LSET.BackMask())
            pad.SetNetCode(other.GetNetCode())
            footprint.Add(pad)
            board.Add(footprint)
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            data = self.config()
            data.update(board_snapshot="pcb", bounds_mm=[0, 0, 8, 5], step_mm=1,
                        layers=["B.Cu"], width_mm=0.2, clearance_mm=0.2,
                        gentle_turns=True, forbid_staircase=True, initial_directions=["E"],
                        start={"at_mm": [2.2, 2.2], "layer": "B.Cu"},
                        end={"at_mm": [6.2, 2.2], "layer": "B.Cu"})
            result = plan(data, project, validate=True)
            self.assertEqual(result["validation"]["status"], "no_new_drc_violations",
                             result["validation"])
            self.assertEqual(result["validation"]["endpoint_connectivity"], "connected")
            self.assertEqual(source.read_bytes(), original)

    def test_native_drc_rejects_a_new_short(self):
        with tempfile.TemporaryDirectory(prefix="jessie-route-drc-test-") as directory:
            project = Path(directory)
            source = self.make_board(project, crossing=True)
            original = source.read_bytes()
            proposal = plan(self.config(), project, include_changes=True)
            result = validate_route(
                source,
                hashlib.sha256(original).hexdigest(),
                proposal["unvalidated_campaign_changes"],
                "SIGNAL",
                point(2, 2),
                "F.Cu",
                point(8, 5),
                "F.Cu",
            )
            self.assertEqual(result["status"], "rejected")
            self.assertTrue(
                any(
                    item["before"] == "OTHER" and item["after"] == "SIGNAL"
                    for item in result["net_identity_changes"]
                )
            )
            self.assertEqual(source.read_bytes(), original)

    def test_new_drc_violations_include_affected_item_locations(self):
        with tempfile.TemporaryDirectory(prefix="jessie-route-drc-test-") as directory:
            project = Path(directory)
            source = self.make_board(project)
            original = source.read_bytes()
            proposal = plan(self.config(), project, include_changes=True)
            proposal["unvalidated_campaign_changes"].append(
                {
                    "document": "pcb",
                    "target": {"kind": "root"},
                    "op": "insert",
                    "path": [],
                    "expression": "(segment (start 2 6) (end 3 6) (width 0.2) "
                    '(layer "F.Cu") (net "SIGNAL") '
                    '(uuid "00000000-0000-4000-8000-000000009999"))',
                }
            )
            result = validate_route(
                source,
                hashlib.sha256(original).hexdigest(),
                proposal["unvalidated_campaign_changes"],
                "SIGNAL",
                point(2, 2),
                "F.Cu",
                point(8, 5),
                "F.Cu",
            )
            self.assertEqual(result["status"], "rejected")
            violations = [
                v for v in result["new_violations"] if v["type"] == "track_dangling"
            ]
            self.assertTrue(violations, result)
            self.assertTrue(violations[0].get("items"), violations[0])
            self.assertIn("pos", violations[0]["items"][0])
            self.assertEqual(source.read_bytes(), original)

    def test_replacement_preserves_existing_branch_connectivity(self):
        import pcbnew

        with tempfile.TemporaryDirectory(
            prefix="jessie-route-replace-test-"
        ) as directory:
            project = Path(directory)
            source = self.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            net = board.FindNet("SIGNAL")
            footprint = pcbnew.FOOTPRINT(board)
            footprint.SetReference("J3")
            position = pcbnew.VECTOR2I(mm(5), mm(4))
            footprint.SetPosition(position)
            pad = pcbnew.PAD(footprint)
            pad.SetNumber("1")
            pad.SetSize(pcbnew.VECTOR2I(mm(1), mm(1)))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pcbnew.LSET.FrontMask())
            pad.SetPosition(position)
            pad.SetNetCode(net.GetNetCode())
            footprint.Add(pad)
            board.Add(footprint)
            removals = []
            for index, (start, end) in enumerate(
                (((2, 2), (5, 2)), ((5, 2), (8, 5)), ((5, 2), (5, 4)))
            ):
                trace = pcbnew.PCB_TRACK(board)
                trace.SetStart(pcbnew.VECTOR2I(mm(start[0]), mm(start[1])))
                trace.SetEnd(pcbnew.VECTOR2I(mm(end[0]), mm(end[1])))
                trace.SetWidth(mm(0.2))
                trace.SetLayer(pcbnew.F_Cu)
                trace.SetNetCode(net.GetNetCode())
                board.Add(trace)
                if index < 2:
                    removals.append(
                        {"kind": "segment", "uuid": trace.m_Uuid.AsString()}
                    )
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            data = self.config()
            data.update({"board_snapshot": "pcb", "remove": removals})
            valid = plan(data, project, include_changes=True, validate=True)
            self.assertEqual(
                valid["validation"]["status"],
                "no_new_drc_violations",
                str(valid["validation"]),
            )
            self.assertFalse(valid["validation"]["broken_existing_connections"])
            data["obstacles"] = [
                {
                    "bounds_mm": [4.9, 1.9, 5.1, 2.1],
                    "layers": ["F.Cu"],
                    "kind": "keepout",
                }
            ]
            broken = plan(data, project, include_changes=True, validate=True)
            self.assertEqual(broken["validation"]["status"], "rejected")
            self.assertTrue(broken["validation"]["broken_existing_connections"])
            self.assertEqual(source.read_bytes(), original)

    def test_campaign_includes_the_refilled_zone_and_can_be_applied_to_fixture(self):
        import pcbnew

        with tempfile.TemporaryDirectory(prefix="jessie-route-zone-test-") as directory:
            project = Path(directory)
            source = self.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            ground = pcbnew.NETINFO_ITEM(board, "GND")
            board.Add(ground)
            footprint = pcbnew.FOOTPRINT(board)
            footprint.SetReference("JG")
            at = pcbnew.VECTOR2I(mm(1), mm(1))
            footprint.SetPosition(at)
            pad = pcbnew.PAD(footprint)
            pad.SetNumber("1")
            pad.SetSize(pcbnew.VECTOR2I(mm(0.5), mm(0.5)))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pcbnew.LSET.FrontMask())
            pad.SetPosition(at)
            pad.SetNetCode(ground.GetNetCode())
            footprint.Add(pad)
            board.Add(footprint)
            zone = pcbnew.ZONE(board)
            zone.SetLayer(pcbnew.F_Cu)
            zone.SetNetCode(ground.GetNetCode())
            outline = zone.Outline()
            outline.NewOutline()
            for x, y in ((0.2, 0.2), (9.8, 0.2), (9.8, 7.8), (0.2, 7.8)):
                outline.Append(mm(x), mm(y))
            board.Add(zone)
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            data = self.config()
            data.update({"board_snapshot": "pcb", "assume_zone_refill": True})
            result = plan(data, project, include_changes=True, validate=True)
            self.assertEqual(result["validation"]["status"], "no_new_drc_violations")
            self.assertEqual(result["validation"]["refilled_zone_count"], 1)
            changes = result["unvalidated_campaign_changes"]
            self.assertTrue(any(c["target"].get("kind") == "zone" for c in changes))
            candidate = CampaignPlanner(project).plan("pcb", changes)
            self.assertEqual(
                candidate.result_sha256, result["validation"]["candidate_sha256"]
            )
            self.assertEqual(source.read_bytes(), original)
            store = CampaignFile(project)
            campaign = store.create(
                "Verified route", routing_validation=result["validation"]
            )
            store.append(campaign.name, changes)
            with contextlib.redirect_stdout(io.StringIO()):
                status = campaign_main(
                    [
                        "--project",
                        str(project),
                        "apply",
                        campaign.name,
                        "--document",
                        "pcb",
                        "--expected-sha256",
                        result["source_sha256"],
                        "--expected-campaign-sha256",
                        digest(campaign.read_bytes()),
                    ]
                )
            self.assertEqual(status, 0)
            self.assertEqual(source.read_bytes(), candidate.after)
            reloaded = pcbnew.LoadBoard(str(source))
            self.assertTrue(next(iter(reloaded.Zones())).IsFilled())

    def test_cli_saves_only_a_validated_source_bound_campaign(self):
        with tempfile.TemporaryDirectory(prefix="jessie-route-cli-test-") as directory:
            project = Path(directory)
            source = self.make_board(project)
            original = source.read_bytes()
            data = self.config()
            data["board_snapshot"] = "pcb"
            data["electrical"] = self.electrical()
            config = project / "route.json"
            config.write_text(json.dumps(data), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = route_main(
                    [
                        "--project",
                        str(project),
                        "--config",
                        str(config),
                        "--validate",
                        "--save-campaign",
                        "Synthetic routing",
                    ]
                )
            self.assertEqual(status, 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["validation"]["status"], "no_new_drc_violations")
            self.assertEqual(
                result["validation"]["electrical_assessments"][0]["status"],
                "within_supplied_limits",
            )
            self.assertEqual(source.read_bytes(), original)
            campaign = Path(result["campaign"]["file"])
            self.assertEqual(campaign.parent, project / "config" / "campaigns")
            with contextlib.redirect_stdout(io.StringIO()):
                status = campaign_main(
                    [
                        "--project",
                        str(project),
                        "apply",
                        campaign.name,
                        "--document",
                        "pcb",
                        "--expected-sha256",
                        result["campaign"]["source_sha256"],
                        "--expected-campaign-sha256",
                        result["campaign"]["campaign_sha256"],
                    ]
                )
            self.assertEqual(status, 0)
            self.assertEqual(
                digest(source.read_bytes()), result["validation"]["candidate_sha256"]
            )

    def test_two_route_batch_runs_one_closing_drc_and_saves_one_campaign(self):
        import pcbnew
        from pcb import route_validation

        with tempfile.TemporaryDirectory(
            prefix="jessie-route-batch-test-"
        ) as directory:
            project = Path(directory)
            source = self.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            net = pcbnew.NETINFO_ITEM(board, "OTHER")
            board.Add(net)
            for ref, (x, y) in (("J4", (2, 6.5)), ("J5", (8, 6.5))):
                footprint = pcbnew.FOOTPRINT(board)
                footprint.SetReference(ref)
                position = pcbnew.VECTOR2I(mm(x), mm(y))
                footprint.SetPosition(position)
                pad = pcbnew.PAD(footprint)
                pad.SetNumber("1")
                pad.SetSize(pcbnew.VECTOR2I(mm(1), mm(1)))
                pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
                pad.SetLayerSet(pcbnew.LSET.FrontMask())
                pad.SetPosition(position)
                pad.SetNetCode(net.GetNetCode())
                footprint.Add(pad)
                board.Add(footprint)
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            first = self.config()
            second = self.config()
            first["electrical"] = self.electrical()
            second["electrical"] = self.electrical()
            second.update(
                {
                    "net": "OTHER",
                    "start": {"at_mm": [2, 6.5], "layer": "F.Cu"},
                    "end": {"at_mm": [8, 6.5], "layer": "F.Cu"},
                }
            )
            config = project / "routes.json"
            config.write_text(
                json.dumps({"board_snapshot": "pcb", "routes": [first, second]}),
                encoding="utf-8",
            )
            output = io.StringIO()
            with mock.patch.object(
                route_validation, "_drc", wraps=route_validation._drc
            ) as drc:
                with contextlib.redirect_stdout(output):
                    status = batch_main(
                        [
                            "--project",
                            str(project),
                            "--config",
                            str(config),
                            "--validate",
                            "--save-campaign",
                            "Synthetic batch",
                        ]
                    )
            self.assertEqual(status, 0)
            self.assertEqual(drc.call_count, 2)  # One baseline and one final candidate.
            result = json.loads(output.getvalue())
            self.assertEqual(result["validation"]["routes_validated"], 2)
            self.assertEqual(result["validation"]["status"], "no_new_drc_violations")
            self.assertEqual(len(result["validation"]["electrical_assessments"]), 2)
            self.assertEqual(source.read_bytes(), original)
            campaign = Path(result["campaign"]["file"])
            with contextlib.redirect_stdout(io.StringIO()):
                status = campaign_main(
                    [
                        "--project",
                        str(project),
                        "apply",
                        campaign.name,
                        "--document",
                        "pcb",
                        "--expected-sha256",
                        result["campaign"]["source_sha256"],
                        "--expected-campaign-sha256",
                        result["campaign"]["campaign_sha256"],
                    ]
                )
            self.assertEqual(status, 0)
            self.assertEqual(
                digest(source.read_bytes()), result["validation"]["candidate_sha256"]
            )

    def test_later_batch_routes_avoid_earlier_proposed_copper(self):
        shared = {
            "bounds_mm": [-2, -2, 12, 12],
            "step_mm": 1,
            "layers": ["F.Cu"],
            "width_mm": 0.2,
            "clearance_mm": 0.2,
        }
        first = {
            "net": "A",
            "start": {"at_mm": [1, 5], "layer": "F.Cu"},
            "end": {"at_mm": [9, 5], "layer": "F.Cu"},
        }
        second = {
            "net": "B",
            "start": {"at_mm": [5, 1], "layer": "F.Cu"},
            "end": {"at_mm": [5, 9], "layer": "F.Cu"},
        }
        result = plan_batch({**shared, "routes": [first, second]}, Path("."))
        self.assertEqual(result["routes"][0]["length_mm"], 8)
        self.assertGreater(result["routes"][1]["length_mm"], 8)
        self.assertEqual(result["new_via_count"], 0)


if __name__ == "__main__":
    unittest.main()
