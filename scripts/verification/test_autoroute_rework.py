"""Physical edit costs, coordinated copper repair, and native branch anchors."""

import hashlib
import tempfile
import unittest
from core.pcb_io import enabled_copper_layers
from pathlib import Path
from unittest.mock import patch

import pcbnew

from pcb.net_routing import batch
from pcb.net_routing.model import Budget, Limits, NetPlan
from pcb.net_routing.reroute import compile_reroutes
from pcb.net_routing.rework import (
    RemovedCopper,
    RemovedVia,
    ReworkWeights,
    capture_removals,
)
from pcb.net_routing.runner import RoundQueue
from pcb.net_routing.schedule import BoardPlan
from pcb.route_junctions import junction_changes
from pcb.route_optimizer import COPPER_LAYERS, Via, mm
from pcb.route_validation import CandidateRejected, RouteContract, validate_routes
from verification import test_net_routing as f
from verification import test_route_optimizer as boards


def add_track(board, start, end, name="SIGNAL"):
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(pcbnew.VECTOR2I(*[mm(x) for x in start]))
    track.SetEnd(pcbnew.VECTOR2I(*[mm(x) for x in end]))
    track.SetWidth(mm(0.25))
    track.SetLayer(pcbnew.F_Cu)
    track.SetNetCode(board.FindNet(name).GetNetCode())
    board.Add(track)
    return track


class ReworkTests(unittest.TestCase):
    def test_runner_separates_protected_copper_from_high_speed_reference_nets(self):
        from pcb.net_routing import runner

        board = object()
        request, policy = {"nets": []}, {"exclude_nets": []}
        high_speed = ("USB_DP", "USB_DM")
        with (
            patch.object(runner.NativePreview, "board", return_value=board),
            patch.object(
                runner, "unqualified_working_nets", return_value={"GND", "CONTROL"}
            ),
            patch.object(runner, "compile_reroutes", return_value={}) as compile_repair,
        ):
            runner.NativePreview.reroutes(Path("unused"), request, policy, high_speed)
        compile_repair.assert_called_once_with(
            board,
            request,
            policy,
            ("CONTROL", "GND", "USB_DM", "USB_DP"),
            reference_nets=high_speed,
        )

    def test_repair_diagnosis_keeps_protection_and_reference_scopes_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            source = boards.RouteOptimizerTests.make_board(Path(directory))
            board = pcbnew.LoadBoard(str(source))
            board.SetCopperLayerCount(4)
            for name, x in (("NEIGHBOR", 4), ("ELIGIBLE", 6), ("USB_DP", 7)):
                board.Add(pcbnew.NETINFO_ITEM(board, name))
                f.NativeNetRoutingTests.add_pad(board, f"T{x}", (x, 1), net_name=name)
                f.NativeNetRoutingTests.add_pad(board, f"B{x}", (x, 7), net_name=name)
                add_track(board, (x, 1), (x, 7), name)
            board.GetConnectivity().Build(board)
            pcbnew.SaveBoard(str(source), board)
            request = f.NativeNetRoutingTests.config()
            request["shared"].update(layers=list(enabled_copper_layers(board)))
            request.update(max_islands=3, recovery_stage=4)
            policy = {
                "route": request["shared"],
                "max_pins": 3,
                "recovery": {"obstruction_trials": 1, "placement_candidates": 0},
            }
            with patch(
                "pcb.net_routing.obstructions.diagnose", return_value=([], [])
            ) as diagnose:
                result = compile_reroutes(
                    board, request, policy, ("NEIGHBOR",), reference_nets=("USB_DP",)
                )
            self.assertTrue(diagnose.called)
            self.assertEqual(diagnose.call_args.args[5], ("USB_DP",))
            self.assertTrue(result["requests"])
            self.assertTrue(
                all(
                    not ({"NEIGHBOR", "USB_DP"} & set(r["rip_up"]))
                    for r in result["requests"]
                )
            )

    def test_removed_track_can_reconnect_to_an_existing_filled_plane(self):
        from pcb.net_routing.native import discover, load_board
        from pcb.net_routing.rework import include_repair_contacts

        with tempfile.TemporaryDirectory() as directory:
            source = f.NativeNetRoutingTests.make_plane_fixture(Path(directory))
            board = pcbnew.LoadBoard(str(source))
            for old in list(board.Zones()):
                board.Remove(old)
            zone = pcbnew.ZONE(board)
            zone.SetLayer(pcbnew.In1_Cu)
            zone.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            outline = zone.Outline()
            outline.NewOutline()
            for x, y in ((0.2, 0.2), (6, 0.2), (6, 7.8), (0.2, 7.8)):
                outline.Append(mm(x), mm(y))
            board.Add(zone)
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(pcbnew.VECTOR2I(mm(7), mm(5)))
            via.SetWidth(mm(0.6))
            via.SetDrill(mm(0.3))
            via.SetViaType(pcbnew.VIATYPE_THROUGH)
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            board.Add(via)
            connection = add_track(board, (7, 5), (8, 5))
            connection.SetLayer(pcbnew.B_Cu)
            pcbnew.SaveBoard(str(source), board)
            refilled = load_board(source, refill_zones=True)
            layers = ("B.Cu", "In1.Cu", "F.Cu")
            native = discover(refilled, "SIGNAL", layers, Budget(Limits()))
            self.assertEqual(len(native.net.islands), 2)
            old_track = f.segment((5, 5), (7, 5), "In1.Cu")
            removed = RemovedCopper(tracks=((old_track, mm(0.25)),))
            augmented = include_repair_contacts(refilled, native.net, removed)
            self.assertTrue(
                RemovedCopper.connects_islands(augmented, f.route(old_track))
            )
            plane = next(
                island
                for island in augmented.islands
                if f.site(5, 5, "In1.Cu") in island.sites
            )
            self.assertTrue(
                any(
                    virtual == f.site(5, 5, "In1.Cu")
                    for virtual, _ in plane.zone_anchors
                )
            )
            changes = f.campaign_changes(
                f.route(old_track), "SIGNAL", mm(0.25), mm(0.6), mm(0.3)
            )
            contract = RouteContract(
                "SIGNAL", f.site(2, 2).point, "F.Cu", f.site(8, 5).point, "B.Cu"
            )
            evidence = validate_routes(
                source,
                hashlib.sha256(source.read_bytes()).hexdigest(),
                changes,
                [contract],
            )
            self.assertEqual(evidence["status"], "no_new_drc_violations", evidence)
            self.assertEqual(evidence["candidate_unconnected"], 0)
            self.assertFalse(evidence["broken_existing_connections"])

    def test_recovery_includes_adjacent_copper_in_a_staircase(self):
        from pcb.net_routing.reroute import staircase_support

        segments = {
            "old": ("SIGNAL", f.segment((8, 1), (7, 2))),
            "first": ("SIGNAL", f.segment((7, 2), (6.5, 2))),
            "second": ("SIGNAL", f.segment((6.5, 2), (6, 2.5))),
            "last": ("SIGNAL", f.segment((6, 2.5), (6, 3.5))),
            "unrelated": ("OTHER", f.segment((8, 1), (7, 2))),
        }
        selected = frozenset(("first", "second", "last"))
        self.assertEqual(
            staircase_support(segments, selected, max_items=6),
            selected | {"old"},
        )
        self.assertEqual(staircase_support(segments, selected, max_items=3), selected)
        self.assertEqual(
            staircase_support(
                segments,
                selected,
                max_items=6,
                selectable=selected,
            ),
            selected,
        )
        self.assertEqual(
            staircase_support(
                segments,
                selected,
                max_items=6,
                anchors=frozenset({("SIGNAL", "F.Cu", f.site(7, 2).point)}),
            ),
            selected,
        )

    def test_staircase_support_does_not_cross_a_branch(self):
        from pcb.net_routing.reroute import staircase_support

        segments = {
            "old": ("SIGNAL", f.segment((8, 1), (7, 2))),
            "first": ("SIGNAL", f.segment((7, 2), (6.5, 2))),
            "second": ("SIGNAL", f.segment((6.5, 2), (6, 2.5))),
            "branch": ("SIGNAL", f.segment((7, 2), (7, 3))),
        }
        selected = frozenset(("first", "second"))
        self.assertEqual(staircase_support(segments, selected, max_items=6), selected)

    def test_removed_copper_is_a_width_preserving_repair_candidate(self):
        from pcb.net_routing.workflow import _solve_with_terminal_refinement

        net = f.make_net("SIGNAL", [(0, 0), (8, 0)])
        budget = Budget(Limits(smoothing_trials=0, tree_beam=1))
        backend = f.synthetic_backend(net, budget)
        original = f.segment((0, 0), (8, 0))
        removed = RemovedCopper(tracks=((original, mm(0.4)),))
        with patch.object(backend, "paths", return_value=()):
            plans = _solve_with_terminal_refinement(net, (), backend, budget, removed)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].restored_widths, ((original, mm(0.4)),))
        self.assertEqual(plans[0].rework_cost, 0)

    def test_valid_original_repair_leaves_time_for_other_nets(self):
        from pcb.net_routing.workflow import _solve_with_terminal_refinement

        net = f.make_net("SIGNAL", [(0, 0), (8, 0)])
        budget = Budget(Limits(seconds=30, smoothing_trials=0), started=0.0)
        backend = f.synthetic_backend(net, budget)
        removed = RemovedCopper(tracks=((f.segment((0, 0), (8, 0)), mm(0.2)),))
        clock = [0.0]

        def slow_search(*args, **kwargs):
            for second in range(1, 30):
                clock[0] = float(second)
                budget.check()
            return ()

        with patch(
            "pcb.net_routing.model.time.monotonic", side_effect=lambda: clock[0]
        ):
            with budget.net_slice(2):
                with patch(
                    "pcb.net_routing.workflow.whole_net", side_effect=slow_search
                ):
                    plans = _solve_with_terminal_refinement(
                        net, (), backend, budget, removed
                    )
                budget.check()
        self.assertTrue(plans)
        self.assertLess(clock[0], 15)

    def test_conflicting_repair_tries_local_search_then_wider_fallback(self):
        from pcb.net_routing.workflow import _solve_with_terminal_refinement

        net = f.make_net("SIGNAL", [(0, 0), (1, 0)])
        other = f.make_net("OTHER", [(0, 2), (1, 2)])
        budget = Budget(Limits(seconds=30, smoothing_trials=0))
        backend = f.synthetic_backend(net, budget)
        backend.settings.update(f.synthetic_backend(other, budget).settings)
        prior = (NetPlan("OTHER", f.route(f.segment((0, 2), (1, 2)))),)
        removed = RemovedCopper(tracks=((f.segment((0, 0), (1, 0)), mm(0.2)),))
        windows = []

        def search(net, prior, engine, budget, **kwargs):
            setting = engine.settings[net.name]
            windows.append((setting.lower.x, setting.upper.x))
            return () if len(windows) == 1 else (NetPlan(net.name, f.route()),)

        with patch.object(backend, "restored_plan", return_value=None):
            with patch("pcb.net_routing.workflow.whole_net", side_effect=search):
                plans = _solve_with_terminal_refinement(
                    net, prior, backend, budget, removed
                )
        self.assertTrue(plans)
        self.assertEqual(len(windows), 2)
        self.assertLess(windows[0][1] - windows[0][0], windows[1][1] - windows[1][0])

    def test_unavailable_local_window_keeps_wider_repair_search(self):
        from pcb.net_routing.checkpoint import CheckpointWindowUnavailable
        from pcb.net_routing.workflow import _solve_with_terminal_refinement

        net = f.make_net("SIGNAL", [(0, 0), (1, 0)])
        other = f.make_net("OTHER", [(0, 2), (1, 2)])
        budget = Budget(Limits(seconds=30, smoothing_trials=0))
        backend = f.synthetic_backend(net, budget)
        backend.settings.update(f.synthetic_backend(other, budget).settings)
        prior = (NetPlan("OTHER", f.route(f.segment((0, 2), (1, 2)))),)
        removed = RemovedCopper(tracks=((f.segment((0, 0), (1, 0)), mm(0.2)),))
        with patch.object(backend, "restored_plan", return_value=None):
            with patch(
                "pcb.net_routing.workflow.search_window",
                side_effect=CheckpointWindowUnavailable("local bounds"),
            ):
                with patch(
                    "pcb.net_routing.workflow.whole_net",
                    return_value=(NetPlan("SIGNAL", f.route()),),
                ):
                    plans = _solve_with_terminal_refinement(
                        net, prior, backend, budget, removed
                    )
        self.assertTrue(plans)

    def test_unexpected_local_window_error_stops_the_search(self):
        from pcb.net_routing.workflow import _solve_with_terminal_refinement

        net = f.make_net("SIGNAL", [(0, 0), (1, 0)])
        other = f.make_net("OTHER", [(0, 2), (1, 2)])
        budget = Budget(Limits(seconds=30, smoothing_trials=0))
        backend = f.synthetic_backend(net, budget)
        backend.settings.update(f.synthetic_backend(other, budget).settings)
        prior = (NetPlan("OTHER", f.route(f.segment((0, 2), (1, 2)))),)
        with patch.object(backend, "restored_plan", return_value=None):
            with patch(
                "pcb.net_routing.workflow.search_window",
                side_effect=ValueError("unexpected local configuration error"),
            ):
                with self.assertRaisesRegex(
                    ValueError, "unexpected local configuration error"
                ):
                    _solve_with_terminal_refinement(net, prior, backend, budget)

    def test_target_after_repair_also_uses_local_first_search(self):
        from pcb.net_routing.workflow import _solve_with_terminal_refinement

        net = f.make_net("TARGET", [(0, 0), (1, 0)])
        other = f.make_net("REPAIR", [(0, 2), (1, 2)])
        budget = Budget(Limits(seconds=30, smoothing_trials=0))
        backend = f.synthetic_backend(net, budget)
        backend.settings.update(f.synthetic_backend(other, budget).settings)
        prior = (NetPlan("REPAIR", f.route(f.segment((0, 2), (1, 2)))),)
        windows = []

        def search(net, prior, engine, budget, **kwargs):
            setting = engine.settings[net.name]
            windows.append((setting.lower.x, setting.upper.x))
            return () if len(windows) == 1 else (NetPlan(net.name, f.route()),)

        with patch("pcb.net_routing.workflow.whole_net", side_effect=search):
            plans = _solve_with_terminal_refinement(net, prior, backend, budget)
        self.assertTrue(plans)
        self.assertEqual(len(windows), 2)
        self.assertLess(windows[0][1] - windows[0][0], windows[1][1] - windows[1][0])

    def test_local_repair_search_finds_a_checked_real_candidate(self):
        from pcb.net_routing.topology import whole_net
        from pcb.net_routing.workflow import _solve_with_terminal_refinement

        net = f.make_net("SIGNAL", [(0, 0), (1, 0)])
        other = f.make_net("OTHER", [(0, 2), (1, 2)])
        budget = Budget(Limits(seconds=30, smoothing_trials=0, path_candidates=1))
        backend = f.synthetic_backend(net, budget, step_mm=0.25)
        backend.settings.update(f.synthetic_backend(other, budget).settings)
        prior = (NetPlan("OTHER", f.route(f.segment((0, 2), (1, 2)))),)
        removed = RemovedCopper(tracks=((f.segment((0, 0), (1, 0)), mm(0.2)),))
        with patch.object(backend, "restored_plan", return_value=None):
            with patch(
                "pcb.net_routing.workflow.whole_net", side_effect=whole_net
            ) as searched:
                plans = _solve_with_terminal_refinement(
                    net, prior, backend, budget, removed
                )
        self.assertTrue(plans)
        local_engine = searched.call_args_list[0].args[2]
        self.assertLess(
            local_engine.settings["SIGNAL"].upper.x, backend.settings["SIGNAL"].upper.x
        )
        backend.combine(net, (plans[0].route,))

    def test_disconnected_removed_copper_is_not_a_repair_candidate(self):
        from pcb.net_routing.workflow import _solve_with_terminal_refinement

        net = f.make_net("SIGNAL", [(0, 0), (8, 0)])
        budget = Budget(Limits(smoothing_trials=0, tree_beam=1))
        backend = f.synthetic_backend(net, budget)
        removed = RemovedCopper(
            tracks=(
                (f.segment((0, 0), (1, 0)), mm(0.2)),
                (f.segment((7, 0), (8, 0)), mm(0.2)),
            )
        )
        with patch.object(backend, "paths", return_value=()):
            self.assertFalse(
                _solve_with_terminal_refinement(net, (), backend, budget, removed)
            )

    def test_restored_copper_cannot_cross_an_earlier_proposed_net(self):
        from pcb.net_routing.workflow import _solve_with_terminal_refinement

        net = f.make_net("SIGNAL", [(0, 0), (8, 0)])
        other = f.make_net("OTHER", [(4, -2), (4, 2)])
        budget = Budget(Limits(smoothing_trials=0, tree_beam=1))
        backend = f.synthetic_backend(net, budget)
        backend.settings.update(f.synthetic_backend(other, budget).settings)
        prior = (NetPlan("OTHER", f.route(f.segment((4, -2), (4, 2)))),)
        removed = RemovedCopper(tracks=((f.segment((0, 0), (8, 0)), mm(0.2)),))
        with patch.object(backend, "paths", return_value=()):
            self.assertFalse(
                _solve_with_terminal_refinement(
                    net, prior, backend, budget, removed, source_unchanged=True
                )
            )

    def test_restored_copper_cannot_cross_existing_foreign_copper(self):
        from pcb.net_routing.workflow import _solve_with_terminal_refinement
        from pcb.route_optimizer import Obstacle, Point

        net = f.make_net("SIGNAL", [(0, 0), (8, 0)])
        budget = Budget(Limits(smoothing_trials=0, tree_beam=1))
        backend = f.synthetic_backend(net, budget)
        backend.obstacles = (
            Obstacle(
                Point.from_mm([3.9, -0.1]),
                Point.from_mm([4.1, 0.1]),
                ("F.Cu",),
                "OTHER",
                "pad",
            ),
        )
        removed = RemovedCopper(tracks=((f.segment((0, 0), (8, 0)), mm(0.2)),))
        with patch.object(backend, "paths", return_value=()):
            self.assertFalse(
                _solve_with_terminal_refinement(net, (), backend, budget, removed)
            )

    def test_restored_staircase_is_cleaned_without_losing_existing_contact(self):
        from pcb.net_routing.geometry import on_segment
        from pcb.net_routing.workflow import _solve_with_terminal_refinement

        contact, endpoint, old_pad = f.site(7, 2), f.site(6, 3.5), f.site(8, 1)
        net = f.Net(
            "SIGNAL",
            (f.Island("a", (contact,)), f.Island("b", (endpoint,))),
            (f.segment((8, 1), (7, 2)),),
            frozenset((old_pad, endpoint)),
        )
        budget = Budget(Limits(seconds=30, smoothing_trials=256))
        backend = f.synthetic_backend(net, budget)
        removed = RemovedCopper(
            tracks=(
                (f.segment((7, 2), (6.5, 2)), mm(0.2)),
                (f.segment((6.5, 2), (6, 2.5)), mm(0.2)),
                (f.segment((6, 2.5), (6, 3.5)), mm(0.2)),
            )
        )
        with patch.object(backend, "paths", return_value=()):
            plans = _solve_with_terminal_refinement(net, (), backend, budget, removed)
        self.assertTrue(plans, budget.failure_details)
        candidate = plans[0]
        self.assertNotEqual(
            candidate.route.segments, tuple(s for s, _ in removed.tracks)
        )
        self.assertGreater(candidate.rework_cost, 0)
        backend.combine(net, (candidate.route,))
        for required in (contact, endpoint):
            self.assertTrue(
                any(
                    s.layer == required.layer and on_segment(required.point, s)
                    for s in candidate.route.segments
                )
            )

    def test_unchanged_legacy_staircase_can_be_a_temporary_restoration_seed(self):
        from pcb.net_routing.geometry import RouteGeometryError

        contact, endpoint, old_pad = f.site(7, 2), f.site(6, 3.5), f.site(8, 1)
        net = f.Net(
            "SIGNAL",
            (f.Island("a", (contact,)), f.Island("b", (endpoint,))),
            (f.segment((8, 1), (7, 2)),),
            frozenset((old_pad, endpoint)),
        )
        removed = RemovedCopper(
            tracks=(
                (f.segment((7, 2), (6.5, 2)), mm(0.2)),
                (f.segment((6.5, 2), (6, 2.5)), mm(0.2)),
                (f.segment((6, 2.5), (6, 3.5)), mm(0.2)),
            )
        )
        backend = f.synthetic_backend(
            net, Budget(Limits(seconds=30, smoothing_trials=0))
        )
        with (
            patch.object(
                backend,
                "refine_complete",
                side_effect=lambda net, prior, route, **kw: route,
            ),
            patch.object(backend, "local_repair", return_value=None),
        ):
            seed = backend.restored_plan(net, (), removed, source_unchanged=True)
            self.assertIsNotNone(seed)
            self.assertFalse(seed.source_modified)
            self.assertEqual(
                seed.route.segments, tuple(segment for segment, _ in removed.tracks)
            )
            self.assertIsNone(
                backend.restored_plan(net, (), removed, source_unchanged=False)
            )
        # Ordinary new routing still rejects a staircase, even when unchanged
        # original geometry may be represented by a temporary seed.
        with self.assertRaises(RouteGeometryError):
            backend.combine(net, (seed.route,))

    def test_exact_restoration_can_keep_an_existing_off_angle_contact(self):
        from pcb.net_routing.geometry import RouteGeometryError

        contact, endpoint, old_pad = (
            f.site(7, 2.000001),
            f.site(6, 2.000001),
            f.site(8, 1),
        )
        net = f.Net(
            "SIGNAL",
            (f.Island("a", (contact,)), f.Island("b", (endpoint,))),
            (f.segment((8, 1), (7, 2.000001)),),
            frozenset((old_pad, endpoint)),
        )
        removed = RemovedCopper(
            tracks=((f.segment((7, 2.000001), (6, 2.000001)), mm(0.2)),)
        )
        backend = f.synthetic_backend(
            net, Budget(Limits(seconds=30, smoothing_trials=0))
        )
        with (
            patch.object(
                backend,
                "refine_complete",
                side_effect=lambda net, prior, route, **kw: route,
            ),
            patch.object(backend, "local_repair", return_value=None),
        ):
            seed = backend.restored_plan(net, (), removed, source_unchanged=True)
            self.assertIsNotNone(seed)
            self.assertFalse(seed.source_modified)
            self.assertEqual(
                seed.route.segments, tuple(segment for segment, _ in removed.tracks)
            )
            self.assertEqual(seed.route.bends, 1)
            self.assertIsNone(
                backend.restored_plan(net, (), removed, source_unchanged=False)
            )
        with self.assertRaises(RouteGeometryError):
            backend.combine(net, (seed.route,))

    def test_off_angle_restoration_does_not_count_a_branch_as_a_bend(self):
        contact, endpoint, old_pad = (
            f.site(7, 2.000001),
            f.site(6, 2.000001),
            f.site(8, 1),
        )
        net = f.Net(
            "SIGNAL",
            (f.Island("a", (contact,)), f.Island("b", (endpoint,))),
            (f.segment((8, 1), (7, 2.000001)), f.segment((7, 2.000001), (7, 3.000001))),
            frozenset((old_pad, endpoint)),
        )
        removed = RemovedCopper(
            tracks=((f.segment((7, 2.000001), (6, 2.000001)), mm(0.2)),)
        )
        backend = f.synthetic_backend(
            net, Budget(Limits(seconds=30, smoothing_trials=0))
        )
        with (
            patch.object(
                backend,
                "refine_complete",
                side_effect=lambda net, prior, route, **kw: route,
            ),
            patch.object(backend, "local_repair", return_value=None),
        ):
            seed = backend.restored_plan(net, (), removed, source_unchanged=True)
        self.assertIsNotNone(seed)
        self.assertEqual(seed.route.bends, 0)

    def test_off_angle_restoration_cannot_cross_an_earlier_new_route(self):
        contact, endpoint, old_pad = (
            f.site(7, 2.000001),
            f.site(6, 2.000001),
            f.site(8, 1),
        )
        net = f.Net(
            "SIGNAL",
            (f.Island("a", (contact,)), f.Island("b", (endpoint,))),
            (f.segment((8, 1), (7, 2.000001)),),
            frozenset((old_pad, endpoint)),
        )
        other = f.make_net("OTHER", [(6.5, 1.5), (6.5, 2.5)])
        backend = f.synthetic_backend(
            net, Budget(Limits(seconds=30, smoothing_trials=0))
        )
        backend.settings.update(f.synthetic_backend(other, backend.budget).settings)
        prior = (NetPlan("OTHER", f.route(f.segment((6.5, 1.5), (6.5, 2.5)))),)
        removed = RemovedCopper(
            tracks=((f.segment((7, 2.000001), (6, 2.000001)), mm(0.2)),)
        )
        with (
            patch.object(
                backend,
                "refine_complete",
                side_effect=lambda net, prior, route, **kw: route,
            ),
            patch.object(backend, "local_repair", return_value=None),
        ):
            self.assertIsNone(
                backend.restored_plan(net, prior, removed, source_unchanged=True)
            )

    def test_unchanged_staircase_seed_still_cannot_cross_a_new_route(self):
        contact, endpoint, old_pad = f.site(7, 2), f.site(6, 3.5), f.site(8, 1)
        net = f.Net(
            "SIGNAL",
            (f.Island("a", (contact,)), f.Island("b", (endpoint,))),
            (f.segment((8, 1), (7, 2)),),
            frozenset((old_pad, endpoint)),
        )
        other = f.make_net("OTHER", [(6.25, 1.8), (6.25, 3.7)])
        backend = f.synthetic_backend(
            net, Budget(Limits(seconds=30, smoothing_trials=0))
        )
        backend.settings.update(f.synthetic_backend(other, backend.budget).settings)
        prior = (NetPlan("OTHER", f.route(f.segment((6.25, 1.8), (6.25, 3.7)))),)
        removed = RemovedCopper(
            tracks=(
                (f.segment((7, 2), (6.5, 2)), mm(0.2)),
                (f.segment((6.5, 2), (6, 2.5)), mm(0.2)),
                (f.segment((6, 2.5), (6, 3.5)), mm(0.2)),
            )
        )
        with (
            patch.object(
                backend,
                "refine_complete",
                side_effect=lambda net, prior, route, **kw: route,
            ),
            patch.object(backend, "local_repair", return_value=None),
        ):
            self.assertIsNone(
                backend.restored_plan(net, prior, removed, source_unchanged=True)
            )

    def test_staircase_repair_cannot_cross_an_earlier_proposal(self):
        from pcb.net_routing.workflow import _solve_with_terminal_refinement

        contact, endpoint, old_pad = f.site(7, 2), f.site(6, 3.5), f.site(8, 1)
        net = f.Net(
            "SIGNAL",
            (f.Island("a", (contact,)), f.Island("b", (endpoint,))),
            (f.segment((8, 1), (7, 2)),),
            frozenset((old_pad, endpoint)),
        )
        other = f.make_net("OTHER", [(6.25, 1.8), (6.25, 3.7)])
        budget = Budget(Limits(seconds=30, smoothing_trials=256))
        backend = f.synthetic_backend(net, budget)
        backend.settings.update(f.synthetic_backend(other, budget).settings)
        prior = (NetPlan("OTHER", f.route(f.segment((6.25, 1.8), (6.25, 3.7)))),)
        removed = RemovedCopper(
            tracks=(
                (f.segment((7, 2), (6.5, 2)), mm(0.2)),
                (f.segment((6.5, 2), (6, 2.5)), mm(0.2)),
                (f.segment((6, 2.5), (6, 3.5)), mm(0.2)),
            )
        )
        with patch.object(backend, "paths", return_value=()):
            self.assertFalse(
                _solve_with_terminal_refinement(
                    net, prior, backend, budget, removed, source_unchanged=True
                )
            )

    def test_staircase_repair_uses_a_local_search_when_shortcuts_fail(self):
        from pcb.net_routing.workflow import _solve_with_terminal_refinement

        contact, endpoint, old_pad = f.site(7, 2), f.site(6, 3.5), f.site(8, 1)
        net = f.Net(
            "SIGNAL",
            (f.Island("a", (contact,)), f.Island("b", (endpoint,))),
            (f.segment((8, 1), (7, 2)),),
            frozenset((old_pad, endpoint)),
        )
        budget = Budget(Limits(seconds=30, smoothing_trials=32))
        backend = f.synthetic_backend(net, budget, step_mm=0.25)
        removed = RemovedCopper(
            tracks=(
                (f.segment((7, 2), (6.5, 2)), mm(0.2)),
                (f.segment((6.5, 2), (6, 2.5)), mm(0.2)),
                (f.segment((6, 2.5), (6, 3.5)), mm(0.2)),
            )
        )
        with patch.object(backend, "paths", return_value=()):
            with patch.object(
                backend,
                "refine_complete",
                side_effect=lambda net, prior, route, **kwargs: route,
            ):
                plans = _solve_with_terminal_refinement(
                    net, (), backend, budget, removed
                )
        self.assertTrue(plans, budget.failure_details)
        self.assertTrue(plans[0].source_modified)
        backend.combine(net, (plans[0].route,))
        self.assertGreater(plans[0].rework_cost, 0)

    def test_local_repair_distinguishes_no_route_from_unexpected_error(self):
        from pcb.route_optimizer import NoGridRoute

        contact, endpoint, old_pad = f.site(7, 2), f.site(6, 3.5), f.site(8, 1)
        net = f.Net(
            "SIGNAL",
            (f.Island("a", (contact,)), f.Island("b", (endpoint,))),
            (f.segment((8, 1), (7, 2)),),
            frozenset((old_pad, endpoint)),
        )
        backend = f.synthetic_backend(net, Budget(Limits(seconds=30)), step_mm=0.25)
        old = f.route(
            f.segment((7, 2), (6.5, 2)),
            f.segment((6.5, 2), (6, 2.5)),
            f.segment((6, 2.5), (6, 3.5)),
        )
        with patch.object(
            backend, "_path", side_effect=NoGridRoute("no route in local grid")
        ):
            self.assertIsNone(backend.local_repair(net, (), old))
        with patch.object(
            backend, "_path", side_effect=ValueError("unexpected endpoint error")
        ):
            with self.assertRaisesRegex(ValueError, "unexpected endpoint error"):
                backend.local_repair(net, (), old)

    def test_repair_checks_only_new_geometry_against_unchanged_source_copper(self):
        from pcb.net_routing.workflow import _solve_with_terminal_refinement
        from pcb.route_optimizer import Obstacle, Point

        contact, endpoint, old_pad = f.site(7, 2), f.site(6, 3.5), f.site(8, 1)
        net = f.Net(
            "SIGNAL",
            (f.Island("a", (contact,)), f.Island("b", (endpoint,))),
            (f.segment((8, 1), (7, 2)),),
            frozenset((old_pad, endpoint)),
        )
        budget = Budget(Limits(seconds=30, smoothing_trials=32))
        backend = f.synthetic_backend(net, budget)
        backend.obstacles = (
            Obstacle(
                Point.from_mm([5.95, 3.35]),
                Point.from_mm([6.05, 3.45]),
                ("F.Cu",),
                "OTHER",
                "pad",
            ),
        )
        removed = RemovedCopper(
            tracks=(
                (f.segment((7, 2), (6.5, 2)), mm(0.2)),
                (f.segment((6.5, 2), (6, 2.5)), mm(0.2)),
                (f.segment((6, 2.5), (6, 3.5)), mm(0.2)),
            )
        )
        repaired = f.route(f.segment((7, 2), (6, 3)), f.segment((6, 3), (6, 3.5)))
        with patch.object(backend, "paths", return_value=()):
            with patch.object(backend, "refine_complete", return_value=repaired):
                plans = _solve_with_terminal_refinement(
                    net, (), backend, budget, removed, source_unchanged=True
                )
        self.assertTrue(plans, budget.failure_details)
        self.assertTrue(plans[0].source_modified)

    def test_repair_retains_new_vias_from_its_candidate(self):
        from pcb.net_routing.workflow import _solve_with_terminal_refinement

        contact, endpoint, old_pad = f.site(7, 2), f.site(6, 3.5), f.site(8, 1)
        net = f.Net(
            "SIGNAL",
            (f.Island("a", (contact,)), f.Island("b", (endpoint,))),
            (f.segment((8, 1), (7, 2)),),
            frozenset((old_pad, endpoint)),
        )
        budget = Budget(Limits(seconds=30, smoothing_trials=32))
        backend = f.synthetic_backend(net, budget, layers=["F.Cu", "B.Cu"])
        removed = RemovedCopper(
            tracks=(
                (f.segment((7, 2), (6.5, 2)), mm(0.2)),
                (f.segment((6.5, 2), (6, 2.5)), mm(0.2)),
                (f.segment((6, 2.5), (6, 3.5)), mm(0.2)),
            )
        )
        first, second = f.site(6.5, 2.5).point, f.site(6, 3).point
        vias = (Via(first, "F.Cu", "B.Cu"), Via(second, "F.Cu", "B.Cu"))
        repaired = f.route(
            f.segment((7, 2), (6.5, 2.5)),
            f.segment((6.5, 2.5), (6, 3), "B.Cu"),
            f.segment((6, 3), (6, 3.5)),
            vias=vias,
        )
        with patch.object(backend, "paths", return_value=()):
            with patch.object(backend, "refine_complete", return_value=repaired):
                plans = _solve_with_terminal_refinement(
                    net, (), backend, budget, removed
                )
        self.assertTrue(plans, budget.failure_details)
        self.assertEqual(set(plans[0].route.vias), set(vias))

    def test_restored_width_is_used_when_routing_a_later_net(self):
        from pcb.net_routing.workflow import _solve_with_terminal_refinement

        net = f.make_net("SIGNAL", [(0, 0), (8, 0)])
        other = f.make_net("OTHER", [(0, 0.45), (8, 0.45)])
        budget = Budget(Limits(smoothing_trials=0, tree_beam=1))
        backend = f.synthetic_backend(net, budget)
        backend.settings.update(f.synthetic_backend(other, budget).settings)
        removed = RemovedCopper(tracks=((f.segment((0, 0), (8, 0)), mm(0.4)),))
        with patch.object(backend, "paths", return_value=()):
            seed = _solve_with_terminal_refinement(net, (), backend, budget, removed)[0]
        setting = backend.settings["OTHER"]
        model = setting.router(backend.envelopes((seed,), setting))
        self.assertFalse(
            model.edge_clear(f.site(0, 0.45).point, f.site(8, 0.45).point, "F.Cu")
        )

    def test_restoration_reuses_a_matching_through_via(self):
        from pcb.net_routing.workflow import _solve_with_terminal_refinement

        a, b = f.site(0, 0, "B.Cu"), f.site(8, 0, "F.Cu")
        net = f.Net(
            "SIGNAL",
            (f.Island("a", (a,)), f.Island("b", (b,))),
            pad_sites=frozenset((a, b)),
        )
        budget = Budget(Limits(smoothing_trials=0, tree_beam=1))
        backend = f.synthetic_backend(net, budget, layers=["B.Cu", "F.Cu"])
        point = f.site(4, 0).point
        via = RemovedVia(
            point, mm(0.3), tuple((layer, mm(0.6)) for layer in COPPER_LAYERS), True
        )
        removed = RemovedCopper(
            tracks=(
                (f.segment((0, 0), (4, 0), "B.Cu"), mm(0.2)),
                (f.segment((4, 0), (8, 0), "F.Cu"), mm(0.2)),
            ),
            vias=(via,),
        )
        with patch.object(backend, "paths", return_value=()):
            plans = _solve_with_terminal_refinement(net, (), backend, budget, removed)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].route.vias[0].at, point)
        self.assertEqual(plans[0].rework_cost, 0)

    def test_original_width_repair_passes_native_drc_and_connectivity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = boards.RouteOptimizerTests.make_board(root)
            board = pcbnew.LoadBoard(str(source))
            first = add_track(board, (2, 2), (5, 2))
            second = add_track(board, (5, 2), (8, 5))
            first.SetWidth(mm(0.4))
            second.SetWidth(mm(0.4))
            pcbnew.SaveBoard(str(source), board)
            (root / "v4.1.kicad_sch").write_text("(kicad_sch (lib_symbols))")
            before = source.read_bytes()
            request = f.NativeNetRoutingTests.config()
            request.update(
                max_islands=3,
                recovery_stage=4,
                rip_up={
                    "SIGNAL": [
                        {"kind": "segment", "uuid": first.m_Uuid.AsString()},
                        {"kind": "segment", "uuid": second.m_Uuid.AsString()},
                    ]
                },
            )
            with patch("pcb.net_routing.backend.NativeBackend.paths", return_value=()):
                result = batch.preview_round(root, [request], workers=1)
            self.assertEqual(
                result["status"],
                "validated_preview",
                {k: result.get(k) for k in ("status", "reason", "attempts")},
            )
            self.assertEqual(result["validation"]["candidate_unconnected"], 0)
            self.assertFalse(result["validation"]["broken_existing_connections"])
            additions = [
                change
                for change in result["changes"]
                if change.get("op") == "insert"
                and change.get("expression", "").startswith("(segment")
            ]
            self.assertTrue(additions)
            self.assertTrue(
                all("(width 0.4)" in change["expression"] for change in additions)
            )
            self.assertEqual(source.read_bytes(), before)

    def test_staircase_repair_passes_native_drc_and_connectivity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = boards.RouteOptimizerTests.make_board(root)
            board = pcbnew.LoadBoard(str(source))
            board.FindFootprintByReference("J1").SetPosition(
                pcbnew.VECTOR2I(mm(8), mm(1))
            )
            board.FindFootprintByReference("J2").SetPosition(
                pcbnew.VECTOR2I(mm(6), mm(3.5))
            )
            add_track(board, (8, 1), (7, 2))
            selected = [
                add_track(board, (7, 2), (6.5, 2)),
                add_track(board, (6.5, 2), (6, 2.5)),
                add_track(board, (6, 2.5), (6, 3.5)),
            ]
            pcbnew.SaveBoard(str(source), board)
            (root / "v4.1.kicad_sch").write_text("(kicad_sch (lib_symbols))")
            before = source.read_bytes()
            request = f.NativeNetRoutingTests.config()
            request["shared"].update(
                width_mm=0.25,
                forbid_staircase=True,
                gentle_turns=True,
            )
            request["search"]["smoothing_trials"] = 256
            request.update(
                max_islands=3,
                recovery_stage=4,
                rip_up={
                    "SIGNAL": [
                        {"kind": "segment", "uuid": track.m_Uuid.AsString()}
                        for track in selected
                    ]
                },
            )
            with patch("pcb.net_routing.backend.NativeBackend.paths", return_value=()):
                result = batch.preview_round(root, [request], workers=1)
            self.assertEqual(
                result["status"],
                "validated_preview",
                {key: result.get(key) for key in ("status", "reason", "attempts")},
            )
            self.assertEqual(result["validation"]["candidate_unconnected"], 0)
            self.assertFalse(result["validation"]["broken_existing_connections"])
            self.assertEqual(source.read_bytes(), before)

    def test_local_replacement_passes_native_drc_and_connectivity(self):
        from pcb.net_routing.backend import NativeBackend

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = boards.RouteOptimizerTests.make_board(root)
            board = pcbnew.LoadBoard(str(source))
            board.FindFootprintByReference("J1").SetPosition(
                pcbnew.VECTOR2I(mm(8), mm(1))
            )
            board.FindFootprintByReference("J2").SetPosition(
                pcbnew.VECTOR2I(mm(6), mm(3.5))
            )
            add_track(board, (8, 1), (7, 2))
            selected = [
                add_track(board, (7, 2), (6.5, 2)),
                add_track(board, (6.5, 2), (6, 2.5)),
                add_track(board, (6, 2.5), (6, 3.5)),
            ]
            pcbnew.SaveBoard(str(source), board)
            (root / "v4.1.kicad_sch").write_text("(kicad_sch (lib_symbols))")
            before = source.read_bytes()
            request = f.NativeNetRoutingTests.config()
            request["shared"].update(
                width_mm=0.25,
                forbid_staircase=True,
                gentle_turns=True,
                step_mm=0.25,
            )
            request["search"]["smoothing_trials"] = 32
            request.update(
                max_islands=3,
                recovery_stage=4,
                rip_up={
                    "SIGNAL": [
                        {"kind": "segment", "uuid": track.m_Uuid.AsString()}
                        for track in selected
                    ]
                },
            )
            with patch.object(NativeBackend, "paths", return_value=()):
                with patch.object(
                    NativeBackend,
                    "refine_complete",
                    autospec=True,
                    side_effect=lambda self, net, prior, route, **kwargs: route,
                ):
                    result = batch.preview_round(root, [request], workers=1)
            self.assertEqual(
                result["status"],
                "validated_preview",
                {key: result.get(key) for key in ("status", "reason", "attempts")},
            )
            self.assertEqual(result["validation"]["candidate_unconnected"], 0)
            self.assertFalse(result["validation"]["broken_existing_connections"])
            self.assertEqual(source.read_bytes(), before)

    def test_original_repair_can_attach_inside_a_pad_away_from_its_center(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = boards.RouteOptimizerTests.make_board(root)
            board = pcbnew.LoadBoard(str(source))
            selected = [
                add_track(board, (2, 2), (5, 2)),
                add_track(board, (5, 2), (7.7, 4.7)),
            ]
            pcbnew.SaveBoard(str(source), board)
            (root / "v4.1.kicad_sch").write_text("(kicad_sch (lib_symbols))")
            before = source.read_bytes()
            request = f.NativeNetRoutingTests.config()
            request.update(
                max_islands=3,
                recovery_stage=4,
                rip_up={
                    "SIGNAL": [
                        {"kind": "segment", "uuid": track.m_Uuid.AsString()}
                        for track in selected
                    ]
                },
            )
            with patch("pcb.net_routing.backend.NativeBackend.paths", return_value=()):
                result = batch.preview_round(root, [request], workers=1)
            self.assertEqual(
                result["status"],
                "validated_preview",
                {key: result.get(key) for key in ("status", "reason", "attempts")},
            )
            self.assertEqual(result["validation"]["candidate_unconnected"], 0)
            self.assertFalse(result["validation"]["broken_existing_connections"])
            self.assertEqual(source.read_bytes(), before)

    def test_repair_can_reach_an_unsampled_existing_track_endpoint(self):
        from dataclasses import replace
        from pcb.net_routing.native import discover
        from pcb.net_routing.rework import include_repair_contacts

        with tempfile.TemporaryDirectory() as directory:
            source = boards.RouteOptimizerTests.make_board(Path(directory))
            board = pcbnew.LoadBoard(str(source))
            track = add_track(board, (2, 2), (4, 2))
            board.GetConnectivity().Build(board)
            native = discover(board, "SIGNAL", ("F.Cu",), Budget(Limits(seconds=30)))
            source_pad = list(board.FindFootprintByReference("J1").Pads())[
                0
            ].m_Uuid.AsString()
            islands = tuple(
                replace(island, sites=(f.site(2, 2),))
                if source_pad in island.pad_ids
                else island
                for island in native.net.islands
            )
            reduced = replace(native.net, islands=islands)
            removed = RemovedCopper(tracks=((f.segment((4, 2), (6, 2)), mm(0.25)),))
            augmented = include_repair_contacts(board, reduced, removed)
            island = next(
                item for item in augmented.islands if source_pad in item.pad_ids
            )
            self.assertIn(f.site(4, 2), island.sites)
            self.assertNotIn(f.site(4, 2), augmented.pad_sites)
            self.assertIsNotNone(track)

    def test_repair_recognizes_a_via_touching_the_side_of_a_track(self):
        from pcb.net_routing.native import discover
        from pcb.net_routing.rework import include_repair_contacts

        with tempfile.TemporaryDirectory() as directory:
            source = boards.RouteOptimizerTests.make_board(Path(directory))
            board = pcbnew.LoadBoard(str(source))
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(pcbnew.VECTOR2I(mm(4), mm(2.025)))
            via.SetWidth(mm(0.6))
            via.SetDrill(mm(0.3))
            via.SetViaType(pcbnew.VIATYPE_THROUGH)
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
            board.Add(via)
            board.GetConnectivity().Build(board)
            native = discover(board, "SIGNAL", ("F.Cu",), Budget(Limits(seconds=30)))
            removed = RemovedCopper(tracks=((f.segment((3, 2), (5, 2)), mm(0.25)),))
            augmented = include_repair_contacts(board, native.net, removed)
            via_id = via.m_Uuid.AsString()
            island = next(
                item for item in augmented.islands if item.identifier == via_id
            )
            self.assertIn(f.site(4, 2), island.sites)
            self.assertNotIn(f.site(4, 2), augmented.pad_sites)

    def test_nearly_horizontal_removed_track_is_replaced_at_an_exact_angle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = boards.RouteOptimizerTests.make_board(root)
            board = pcbnew.LoadBoard(str(source))
            board.FindFootprintByReference("J2").SetPosition(
                pcbnew.VECTOR2I(mm(8), mm(2))
            )
            selected = add_track(board, (2, 2), (8, 2.000015))
            pcbnew.SaveBoard(str(source), board)
            (root / "v4.1.kicad_sch").write_text("(kicad_sch (lib_symbols))")
            before = source.read_bytes()
            request = f.NativeNetRoutingTests.config()
            request["shared"]["width_mm"] = 0.25
            request.update(
                max_islands=3,
                recovery_stage=4,
                rip_up={
                    "SIGNAL": [{"kind": "segment", "uuid": selected.m_Uuid.AsString()}]
                },
            )
            with patch("pcb.net_routing.backend.NativeBackend.paths", return_value=()):
                result = batch.preview_round(root, [request], workers=1)
            self.assertEqual(
                result["status"],
                "validated_preview",
                {key: result.get(key) for key in ("status", "reason", "attempts")},
            )
            self.assertEqual(result["validation"]["candidate_unconnected"], 0)
            self.assertFalse(result["validation"]["broken_existing_connections"])
            self.assertEqual(source.read_bytes(), before)

    def test_near_angle_replacement_costs_rework_and_checks_clearance(self):
        from pcb.net_routing.workflow import _solve_with_terminal_refinement
        from pcb.route_optimizer import Obstacle, Point

        net = f.make_net("SIGNAL", [(0, 0), (8, 0)])
        old = f.segment((0, 0), (8, 0.000015))
        removed = RemovedCopper(off_angle_tracks=((old, mm(0.2)),))
        budget = Budget(Limits(seconds=30, smoothing_trials=0))
        backend = f.synthetic_backend(net, budget)
        with patch.object(backend, "paths", return_value=()):
            plans = _solve_with_terminal_refinement(net, (), backend, budget, removed)
        self.assertTrue(plans)
        self.assertTrue(plans[0].source_modified)
        self.assertGreater(plans[0].rework_cost, 8)
        self.assertEqual(plans[0].route.segments, (f.segment((0, 0), (8, 0)),))
        backend.obstacles = (
            Obstacle(
                Point.from_mm([3.9, -0.1]),
                Point.from_mm([4.1, 0.1]),
                ("F.Cu",),
                "OTHER",
                "pad",
            ),
        )
        with patch.object(backend, "paths", return_value=()):
            self.assertFalse(
                _solve_with_terminal_refinement(
                    net, (), backend, budget, removed, source_unchanged=True
                )
            )

    def test_near_angle_projections_are_exact_and_bounded(self):
        from pcb.net_routing.rework import ExactAngleProjection
        from pcb.route_optimizer import GridRouter

        for a, b in (
            ((0, 0), (4, 0.000015)),
            ((0, 0), (0.000015, 4)),
            ((0, 0), (4, 4.000015)),
            ((0, 0), (-4, 4.000015)),
        ):
            original = f.segment(a, b)
            choices = ExactAngleProjection.projections(original)
            self.assertTrue(choices)
            for choice in choices:
                GridRouter._direction(choice.start, choice.end)
                self.assertLessEqual(abs(choice.start.x - original.start.x), mm(0.05))
                self.assertLessEqual(abs(choice.start.y - original.start.y), mm(0.05))
                self.assertLessEqual(abs(choice.end.x - original.end.x), mm(0.05))
                self.assertLessEqual(abs(choice.end.y - original.end.y), mm(0.05))
        self.assertFalse(ExactAngleProjection.projections(f.segment((0, 0), (4, 0.1))))

    def test_redundant_target_copper_is_removed_and_the_open_target_still_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = boards.RouteOptimizerTests.make_board(root)
            board = pcbnew.LoadBoard(str(source))
            f.NativeNetRoutingTests.add_pad(board, "J3", (4, 4))
            redundant = add_track(board, (2, 2), (4, 4))
            for start, end in (((2, 2), (3, 2)), ((3, 2), (4, 3)), ((4, 3), (4, 4))):
                add_track(board, start, end)
            pcbnew.SaveBoard(str(source), board)
            (root / "v4.1.kicad_sch").write_text("(kicad_sch (lib_symbols))")
            before = source.read_bytes()
            request = f.NativeNetRoutingTests.config()
            request["shared"]["layers"] = ["F.Cu", "B.Cu"]
            request.update(
                max_islands=3,
                recovery_stage=4,
                rip_up={
                    "SIGNAL": [{"kind": "segment", "uuid": redundant.m_Uuid.AsString()}]
                },
            )
            request["search"].update(seconds=30, tree_beam=1)
            result = batch.preview_round(root, [request], workers=1)
            self.assertEqual(
                result["status"],
                "validated_preview",
                {k: result.get(k) for k in ("status", "reason", "attempts")},
            )
            self.assertFalse(result["validation"]["broken_existing_connections"])
            self.assertEqual(result["validation"]["candidate_unconnected"], 0)
            self.assertEqual(source.read_bytes(), before)

    def test_redundant_copper_can_be_removed_without_forcing_neighbor_routing(self):
        for extra_open in (False, True):
            with (
                self.subTest(extra_open=extra_open),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                source = boards.RouteOptimizerTests.make_board(root)
                board = pcbnew.LoadBoard(str(source))
                board.Add(pcbnew.NETINFO_ITEM(board, "NEIGHBOR"))
                f.NativeNetRoutingTests.add_pad(
                    board, "J3", (3, 7), net_name="NEIGHBOR"
                )
                f.NativeNetRoutingTests.add_pad(
                    board, "J4", (7, 7), net_name="NEIGHBOR"
                )
                if extra_open:
                    f.NativeNetRoutingTests.add_pad(
                        board, "J5", (9, 7), net_name="NEIGHBOR"
                    )
                redundant = add_track(board, (3, 7), (7, 7), "NEIGHBOR")
                for start, end in (
                    ((3, 7), (4, 6)),
                    ((4, 6), (6, 6)),
                    ((6, 6), (7, 7)),
                ):
                    add_track(board, start, end, "NEIGHBOR")
                pcbnew.SaveBoard(str(source), board)
                (root / "v4.1.kicad_sch").write_text("(kicad_sch (lib_symbols))")
                before = source.read_bytes()
                request = f.NativeNetRoutingTests.config()
                request["shared"]["layers"] = ["F.Cu", "B.Cu"]
                request["nets"].append({"name": "NEIGHBOR"})
                request.update(
                    max_islands=3,
                    recovery_stage=4,
                    rip_up={
                        "NEIGHBOR": [
                            {"kind": "segment", "uuid": redundant.m_Uuid.AsString()}
                        ]
                    },
                )
                request["search"].update(seconds=30, order_trials=2, tree_beam=1)
                result = batch.preview_round(root, [request], workers=1)
                self.assertEqual(
                    result["status"],
                    "validated_preview",
                    {k: result.get(k) for k in ("status", "reason", "attempts")},
                )
                self.assertFalse(result["validation"]["broken_existing_connections"])
                self.assertEqual(
                    result["validation"]["candidate_unconnected"], int(extra_open)
                )
                self.assertEqual(source.read_bytes(), before)

    def test_equivalent_splits_are_free_but_width_layer_and_detours_are_not(self):
        old = RemovedCopper(tracks=((f.segment((0, 0), (10, 0)), mm(0.2)),))

        def score(route, width=0.2):
            return old.score(
                route,
                width=mm(width),
                size=mm(0.6),
                drill=mm(0.3),
                weights=ReworkWeights(),
            )

        split = f.route(f.segment((10, 0), (4, 0)), f.segment((4, 0), (0, 0)))
        self.assertEqual(score(split).cost, 0)
        partial = score(f.route(f.segment((0, 0), (4, 0))))
        self.assertEqual((partial.tracks, partial.length_mm, partial.cost), (1, 6, 7))
        self.assertEqual(score(split, 0.3).cost, 11)
        self.assertEqual(score(f.route(f.segment((0, 0), (10, 0), "B.Cu"))).cost, 11)

    def test_via_change_cost_uses_dimensions_and_layers_not_uuid(self):
        point = f.site(2, 2).point
        old = RemovedCopper(
            vias=(
                RemovedVia(
                    point,
                    mm(0.3),
                    tuple((layer, mm(0.6)) for layer in COPPER_LAYERS),
                    True,
                ),
            )
        )
        route = f.route(vias=(Via(point, "F.Cu", "B.Cu"),))
        self.assertEqual(
            old.score(
                route,
                width=mm(0.2),
                size=mm(0.6),
                drill=mm(0.3),
                weights=ReworkWeights(),
            ).cost,
            0,
        )
        self.assertEqual(
            old.score(
                route,
                width=mm(0.2),
                size=mm(0.8),
                drill=mm(0.3),
                weights=ReworkWeights(),
            ).cost,
            10,
        )

    def test_edit_penalty_changes_board_candidate_preference(self):
        reuse = BoardPlan((NetPlan("A", f.route(cost=3), 0),))
        replace = BoardPlan((NetPlan("A", f.route(cost=1), 10),))
        self.assertLess(reuse.cost, replace.cost)
        for value in (-1, True, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                ReworkWeights.from_config({"via": value})

    def test_coordinated_repair_is_not_split_or_retried_forever(self):
        request = {
            "nets": [{"name": "A"}, {"name": "B"}],
            "recovery_stage": 4,
            "rip_up": {"B": [{"kind": "segment", "uuid": "old"}]},
        }
        queue = []
        RoundQueue.split_failed(queue, [request], {"A": 2, "B": 2})
        self.assertFalse(queue)

    def test_search_ranks_reuse_above_cheaper_but_disruptive_replacement(self):
        from pcb.net_routing.model import Budget, Limits
        from pcb.net_routing.topology import whole_net

        net = f.make_net("N", [(0, 0), (4, 0)])
        budget = Budget(Limits(seconds=3, net_candidates=2))
        backend = f.synthetic_backend(net, budget)
        chain = ((0, 0), (0, 2), (1, 3), (3, 3), (4, 2), (4, 0))
        old_segments = tuple(f.segment(a, b) for a, b in zip(chain, chain[1:]))
        old = RemovedCopper(
            tracks=tuple((segment, mm(0.2)) for segment in old_segments)
        )

        def penalty(route):
            return old.score(
                route,
                width=mm(0.2),
                size=mm(0.6),
                drill=mm(0.3),
                weights=ReworkWeights(length_mm=10),
            ).cost

        with patch.object(
            backend,
            "paths",
            return_value=(f.route(f.segment((0, 0), (4, 0))), f.route(*old_segments)),
        ):
            plans = whole_net(net, (), backend, budget, rework_cost=penalty)
        self.assertEqual(len(plans), 2)
        self.assertGreater(plans[0].route.cost, plans[1].route.cost)
        self.assertEqual(plans[0].rework_cost, 0)
        self.assertLess(plans[0].cost, plans[1].cost)

    def test_native_capture_reads_via_width_with_explicit_layers(self):
        board = pcbnew.BOARD()
        board.SetCopperLayerCount(4)
        board.Add(pcbnew.NETINFO_ITEM(board, "N"))
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(mm(2), mm(2)))
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetWidth(mm(0.6))
        via.SetDrill(mm(0.3))
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetNetCode(board.FindNet("N").GetNetCode())
        board.Add(via)
        captured = capture_removals(board, {via.m_Uuid.AsString()})["N"]
        self.assertEqual(
            dict(captured.vias[0].sizes),
            dict.fromkeys(("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"), mm(0.6)),
        )

    def test_split_trace_branch_has_valid_replacement_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = boards.RouteOptimizerTests.make_board(root)
            board = pcbnew.LoadBoard(str(source))
            f.NativeNetRoutingTests.add_pad(board, "J3", (8, 2))
            track = add_track(board, (2, 2), (8, 2))
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            route = f.route(f.segment((5, 2), (8, 5)))
            changes, removals = junction_changes(
                board, before, route, "SIGNAL", mm(0.25), mm(0.6), mm(0.3)
            )
            self.assertIn(track.m_Uuid.AsString(), {item["uuid"] for item in removals})
            contract = RouteContract(
                "SIGNAL",
                f.site(5, 2).point,
                "F.Cu",
                f.site(8, 5).point,
                "F.Cu",
                removals=tuple(removals),
            )
            result = validate_routes(
                source, hashlib.sha256(before).hexdigest(), changes, [contract]
            )
            self.assertEqual(result["status"], "no_new_drc_violations", result)
            self.assertFalse(result["broken_existing_connections"])
            self.assertEqual(result["candidate_unconnected"], 0)
            self.assertEqual(source.read_bytes(), before)

    def test_candidate_rejection_is_recoverable_but_infrastructure_failure_is_not(self):
        from verification import test_autoroute_batch as fixture

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture.CombinedBatchTests().project(root)
            request = fixture.CombinedBatchTests().requests()[:1]
            with patch.object(
                batch, "validate_routes", side_effect=CandidateRejected("bad endpoint")
            ):
                result = batch.preview_round(root, request, workers=1)
                self.assertEqual(result["status"], "rejected")
                self.assertIn("bad endpoint", result["reason"])
            with patch.object(
                batch, "validate_routes", side_effect=RuntimeError("DRC process failed")
            ):
                with self.assertRaisesRegex(RuntimeError, "DRC process"):
                    batch.preview_round(root, request, workers=1)

    def test_automatic_neighbor_replacement_is_bounded_and_native_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = boards.RouteOptimizerTests.make_board(root)
            board = pcbnew.LoadBoard(str(source))
            board.Add(pcbnew.NETINFO_ITEM(board, "NEIGHBOR"))
            board.SetCopperLayerCount(4)
            f.NativeNetRoutingTests.add_pad(board, "J3", (5, 1), net_name="NEIGHBOR")
            f.NativeNetRoutingTests.add_pad(board, "J4", (5, 7), net_name="NEIGHBOR")
            track = add_track(board, (5, 1), (5, 7), "NEIGHBOR")
            uid = track.m_Uuid.AsString()
            board.GetConnectivity().Build(board)
            pcbnew.SaveBoard(str(source), board)
            (root / "v4.1.kicad_sch").write_text("(kicad_sch (lib_symbols))")
            before = source.read_bytes()
            request = f.NativeNetRoutingTests.config()
            request["shared"].update(
                width_mm=0.25,
                max_expansions=100000,
                layers=list(enabled_copper_layers(board)),
            )
            request["search"].update(seconds=30, net_candidates=3, order_trials=2)
            request.update(max_islands=3, recovery_stage=4)
            policy = {
                "route": request["shared"],
                "max_pins": 3,
                "recovery": {"placement_candidates": 0},
            }
            compiled = compile_reroutes(board, request, policy, ())
            self.assertEqual(len(compiled["requests"]), 1, compiled)
            repair = compiled["requests"][0]
            self.assertEqual(
                repair["rip_up"], {"NEIGHBOR": [{"kind": "segment", "uuid": uid}]}
            )
            self.assertEqual(
                {item["name"] for item in repair["nets"]}, {"SIGNAL", "NEIGHBOR"}
            )
            result = batch.preview_round(root, [repair], workers=1)
            self.assertEqual(
                result["status"],
                "validated_preview",
                {key: result.get(key) for key in ("status", "reason", "validation")},
            )
            self.assertEqual(result["validation"]["candidate_unconnected"], 0)
            self.assertFalse(result["validation"]["broken_existing_connections"])
            self.assertEqual(source.read_bytes(), before)
            track.SetLocked(True)
            self.assertFalse(compile_reroutes(board, request, policy, ())["requests"])
            track.SetLocked(False)
            self.assertFalse(
                compile_reroutes(board, request, policy, ("NEIGHBOR",))["requests"]
            )

    def test_automatic_recovery_can_replace_the_targets_own_copper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = boards.RouteOptimizerTests.make_board(root)
            board = pcbnew.LoadBoard(str(source))
            board.SetCopperLayerCount(4)
            f.NativeNetRoutingTests.add_pad(board, "J3", (5, 2))
            track = add_track(board, (2, 2), (5, 2))
            uid = track.m_Uuid.AsString()
            board.GetConnectivity().Build(board)
            pcbnew.SaveBoard(str(source), board)
            (root / "v4.1.kicad_sch").write_text("(kicad_sch (lib_symbols))")
            before = source.read_bytes()
            request = f.NativeNetRoutingTests.config()
            request["shared"].update(
                width_mm=0.25,
                max_expansions=100000,
                layers=list(enabled_copper_layers(board)),
            )
            request["search"].update(seconds=30, net_candidates=3, order_trials=2)
            request.update(max_islands=3, recovery_stage=4)
            policy = {
                "route": request["shared"],
                "max_pins": 3,
                "recovery": {"obstruction_trials": 0, "placement_candidates": 0},
            }
            compiled = compile_reroutes(board, request, policy, ())
            self.assertTrue(compiled["requests"], compiled)
            repair = compiled["requests"][0]
            self.assertEqual(
                repair["rip_up"], {"SIGNAL": [{"kind": "segment", "uuid": uid}]}
            )
            self.assertEqual([item["name"] for item in repair["nets"]], ["SIGNAL"])
            result = batch.preview_round(root, [repair], workers=1)
            self.assertEqual(result["status"], "validated_preview", result)
            self.assertEqual(result["validation"]["candidate_unconnected"], 0)
            self.assertFalse(result["validation"]["broken_existing_connections"])
            self.assertEqual(source.read_bytes(), before)
            track.SetLocked(True)
            self.assertFalse(compile_reroutes(board, request, policy, ())["requests"])
            track.SetLocked(False)
            self.assertFalse(
                compile_reroutes(board, request, policy, ("SIGNAL",))["requests"]
            )

    def test_recovery_reserves_complete_target_copper_as_one_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            source = boards.RouteOptimizerTests.make_board(Path(directory))
            board = pcbnew.LoadBoard(str(source))
            board.SetCopperLayerCount(4)
            first = add_track(board, (2, 2), (3, 2))
            second = add_track(board, (7, 5), (8, 5))
            remote = add_track(board, (2, 7), (3, 7))
            board.GetConnectivity().Build(board)
            request = f.NativeNetRoutingTests.config()
            request["shared"].update(layers=list(enabled_copper_layers(board)))
            request.update(max_islands=3, recovery_stage=4)
            policy = {
                "route": request["shared"],
                "max_pins": 3,
                "recovery": {
                    "obstruction_trials": 0,
                    "placement_candidates": 0,
                    "wide_margin_mm": 0.2,
                    "rip_up_items": 12,
                },
            }
            expected = {item.m_Uuid.AsString() for item in (first, second, remote)}
            compiled = compile_reroutes(board, request, policy, ())
            complete = [
                item
                for item in compiled["candidates"]
                if item["selection"] == "whole_target"
            ]
            self.assertEqual(len(complete), 1, compiled)
            self.assertEqual(set(complete[0]["uuids"]), expected)
            remote.SetLocked(True)
            locked = compile_reroutes(board, request, policy, ())
            self.assertFalse(
                any(
                    item["selection"] == "whole_target" for item in locked["candidates"]
                )
            )
            self.assertFalse(
                any(
                    remote.m_Uuid.AsString() in item["uuids"]
                    for item in locked["candidates"]
                )
            )
            remote.SetLocked(False)
            # Whole-net work has a separate cap from local obstruction rip-up.
            policy["recovery"]["rip_up_items"] = 2
            policy["recovery"]["whole_net_rip_up_items"] = 3
            expanded = compile_reroutes(board, request, policy, ())
            complete = [
                item
                for item in expanded["candidates"]
                if item["selection"] == "whole_target"
            ]
            self.assertEqual(set(complete[0]["uuids"]), expected)
            policy["recovery"]["whole_net_rip_up_items"] = 2
            bounded = compile_reroutes(board, request, policy, ())
            self.assertFalse(
                any(
                    item["selection"] == "whole_target"
                    for item in bounded["candidates"]
                )
            )
            policy["recovery"]["whole_net_rip_up_items"] = 0
            disabled = compile_reroutes(board, request, policy, ())
            self.assertFalse(
                any(
                    item["selection"] == "whole_target"
                    for item in disabled["candidates"]
                )
            )
            policy["recovery"]["whole_net_rip_up_items"] = 65
            with self.assertRaises(ValueError):
                compile_reroutes(board, request, policy, ())

    def test_whole_target_recovery_reconnects_and_passes_native_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = boards.RouteOptimizerTests.make_board(root)
            board = pcbnew.LoadBoard(str(source))
            board.SetCopperLayerCount(4)
            add_track(board, (2, 2), (3, 2))
            add_track(board, (7, 5), (8, 5))
            add_track(board, (2, 7), (3, 7))
            board.GetConnectivity().Build(board)
            pcbnew.SaveBoard(str(source), board)
            (root / "v4.1.kicad_sch").write_text("(kicad_sch (lib_symbols))")
            before = source.read_bytes()
            request = f.NativeNetRoutingTests.config()
            request["shared"].update(
                width_mm=0.25,
                max_expansions=100000,
                layers=list(enabled_copper_layers(board)),
            )
            request["search"].update(seconds=30, net_candidates=3, order_trials=2)
            request.update(max_islands=3, recovery_stage=4)
            policy = {
                "route": request["shared"],
                "max_pins": 3,
                "recovery": {
                    "obstruction_trials": 0,
                    "placement_candidates": 0,
                    "wide_margin_mm": 0.2,
                    "rip_up_items": 2,
                    "whole_net_rip_up_items": 3,
                },
            }
            compiled = compile_reroutes(board, request, policy, ())
            index = next(
                i
                for i, item in enumerate(compiled["candidates"])
                if item["selection"] == "whole_target"
            )
            result = batch.preview_round(root, [compiled["requests"][index]], workers=1)
            self.assertEqual(result["status"], "validated_preview", result)
            self.assertEqual(result["validation"]["candidate_unconnected"], 0)
            self.assertFalse(result["validation"]["broken_existing_connections"])
            self.assertEqual(source.read_bytes(), before)

    def test_target_and_neighbor_repair_share_one_transaction(self):
        from pcb.net_routing.workflow import configurations

        with tempfile.TemporaryDirectory() as directory:
            source = boards.RouteOptimizerTests.make_board(Path(directory))
            board = pcbnew.LoadBoard(str(source))
            board.SetCopperLayerCount(4)
            board.Add(pcbnew.NETINFO_ITEM(board, "NEIGHBOR"))
            f.NativeNetRoutingTests.add_pad(board, "J3", (4, 2.7), net_name="NEIGHBOR")
            f.NativeNetRoutingTests.add_pad(board, "J4", (6, 2.7), net_name="NEIGHBOR")
            add_track(board, (2, 2), (5, 2))
            add_track(board, (4, 2.7), (6, 2.7), "NEIGHBOR")
            board.GetConnectivity().Build(board)
            request = f.NativeNetRoutingTests.config()
            request["nets"][0].update(
                width_mm=0.37, via={"size_mm": 0.8, "drill_mm": 0.3}
            )
            request.update(max_islands=3, recovery_stage=4)
            policy = {
                "route": request["shared"],
                "max_pins": 3,
                "recovery": {"obstruction_trials": 0, "placement_candidates": 0},
            }
            compiled = compile_reroutes(board, request, policy, ())
            mixed = [
                item
                for item in compiled["requests"]
                if set(item["rip_up"]) == {"SIGNAL", "NEIGHBOR"}
            ]
            self.assertTrue(mixed, compiled)
            names = [item["name"] for item in mixed[0]["nets"]]
            self.assertEqual(names, ["SIGNAL", "NEIGHBOR"])
            self.assertEqual(mixed[0]["nets"][0]["width_mm"], 0.37)
            self.assertEqual(mixed[0]["nets"][0]["via"]["size_mm"], 0.8)
            configurations(mixed[0])
            request["shared"]["width_mm"] = 0.55
            request["nets"][0].pop("width_mm")
            policy["route"] = {**request["shared"], "width_mm": 0.25}
            compiled = compile_reroutes(board, request, policy, ())
            mixed = [
                item
                for item in compiled["requests"]
                if set(item["rip_up"]) == {"SIGNAL", "NEIGHBOR"}
            ]
            self.assertTrue(mixed, compiled)
            self.assertEqual(mixed[0]["nets"][0]["width_mm"], 0.55)


if __name__ == "__main__":
    unittest.main()
