"""Branch allowances must be proven leaves, not coordinate-based guesses."""

import unittest
from pathlib import Path
import tempfile

from pcb.branch_widths import (
    bounded_leaves,
    check_unchanged_other_rules,
    compile_widths,
)
from core.toolchain import kicad_cli
from core.compat import strict_zip


class BranchWidthTests(unittest.TestCase):
    def test_terminal_selector_survives_native_units_without_matching_another_trace(
        self,
    ):
        import pcbnew
        from pcb.branch_widths import tap_selector
        from pcb.route_validation import _drc
        from pcb.width_repair import required_widths
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory() as folder:
            source = RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))

            def trace(offset):
                item = pcbnew.PCB_TRACK(board)
                item.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
                item.SetLayer(pcbnew.F_Cu)
                item.SetStart(pcbnew.VECTOR2I(62917500, 112645000 + offset))
                item.SetEnd(pcbnew.VECTOR2I(64615000, 112645000 + offset))
                item.SetWidth(400000)
                return item

            tap, trunk = trace(0), trace(1000000)
            for item in (tap, trunk):
                board.Add(item)
            condition = tap_selector(board, tap, (tap, trunk))
            self.assertIsNotNone(condition)
            self.assertIsNone(tap_selector(board, tap, (tap, trunk, trace(1))))
            pcbnew.SaveBoard(str(source), board)
            source.with_suffix(".kicad_dru").write_text(
                '(version 1)\n(rule "trunk" (condition "A.NetName == \'SIGNAL\'") '
                "(constraint track_width (min 0.60mm)))\n"
                f'(rule "tap" (condition "{condition}") (constraint track_width (min 0.30mm)))\n',
                encoding="utf-8",
            )
            report = _drc(kicad_cli(), source, Path(folder) / "native.json")
            self.assertEqual(required_widths(report), {trunk.m_Uuid.AsString(): 600000})

    def test_multiple_upstream_items_at_one_exact_port_are_one_branch_junction(self):
        graph = {
            "source": {"a"},
            "a": {"source", "b", "tap"},
            "b": {"a", "unknown", "tap"},
            "unknown": {"b"},
            "tap": {"a", "b", "sense"},
            "sense": {"tap"},
        }
        ports = {("tap", "a"): (5000000, 2000000), ("tap", "b"): (5000000, 2000000)}
        self.assertEqual(
            bounded_leaves(
                graph,
                {"source", "unknown", "sense"},
                {"sense": 0.001},
                {"tap"},
                contact_ports=ports,
            ),
            {"tap": 0.001},
        )
        ports[("tap", "b")] = (5000000, 4000000)
        self.assertEqual(
            bounded_leaves(
                graph,
                {"source", "unknown", "sense"},
                {"sense": 0.001},
                {"tap"},
                contact_ports=ports,
            ),
            {},
        )
        ports.pop(("tap", "b"))
        self.assertEqual(
            bounded_leaves(
                graph,
                {"source", "unknown", "sense"},
                {"sense": 0.001},
                {"tap"},
                contact_ports=ports,
            ),
            {},
        )

    def test_internal_branch_mesh_uses_only_a_proven_trunk_cut(self):
        graph = {
            "source": {"root"},
            "root": {"source", "load", "feed"},
            "load": {"root"},
            "feed": {"root", "a", "b"},
            "a": {"feed", "sense"},
            "b": {"feed", "sense"},
            "sense": {"a", "b"},
        }
        pads = {"source", "load", "sense"}
        loads = {"sense": 0.01}
        tracks = {"root", "feed", "a", "b"}
        self.assertEqual(bounded_leaves(graph, pads, loads, tracks), {"feed": 0.01})
        self.assertEqual(
            bounded_leaves(graph, pads, loads, tracks, include_downstream=True),
            {"feed": 0.01, "a": 0.01, "b": 0.01},
        )
        graph["b"].add("load")
        graph["load"].add("b")
        self.assertEqual(
            bounded_leaves(graph, pads, loads, tracks, include_downstream=True), {}
        )

    def test_native_contact_port_requires_a_unique_endpoint_contact(self):
        import pcbnew
        from pcb.branch_widths import contact_port

        board = pcbnew.BOARD()

        def track(a, b):
            value = pcbnew.PCB_TRACK(board)
            value.SetStart(pcbnew.VECTOR2I(*a))
            value.SetEnd(pcbnew.VECTOR2I(*b))
            value.SetLayer(pcbnew.F_Cu)
            return value

        spur = track((5000000, 2000000), (5000000, 4000000))
        self.assertEqual(
            contact_port(spur, track((2000000, 2000000), (5000000, 2000000))),
            (5000000, 2000000),
        )
        self.assertEqual(
            contact_port(spur, track((5000000, 2000000), (8000000, 2000000))),
            (5000000, 2000000),
        )
        self.assertIsNone(
            contact_port(spur, track((2000000, 3000000), (8000000, 3000000)))
        )
        self.assertIsNone(
            contact_port(spur, track((5000000, 1000000), (5000000, 3000000)))
        )
        self.assertIsNone(
            contact_port(spur, track((4900000, 1000000), (4900000, 5000000)))
        )

    def test_secondary_contact_requires_a_common_physical_landing(self):
        import pcbnew
        from pcb.branch_widths import common_copper_landing
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory() as folder:
            board = pcbnew.LoadBoard(str(RouteOptimizerTests.make_board(Path(folder))))
            pad = next(iter(board.FindFootprintByReference("J1").Pads()))

            def trace(a, b, width):
                item = pcbnew.PCB_TRACK(board)
                item.SetLayer(pcbnew.F_Cu)
                item.SetNetCode(pad.GetNetCode())
                item.SetWidth(width)
                item.SetStart(pcbnew.VECTOR2I(*a))
                item.SetEnd(pcbnew.VECTOR2I(*b))
                return item

            leaf = trace((2000000, 2000000), (5000000, 2000000), 400000)
            overlap = trace((1500000, 1800000), (2150000, 1800000), 200000)
            self.assertTrue(common_copper_landing(leaf, (pad, overlap)))
            overlap.SetEnd(pcbnew.VECTOR2I(3400000, 1800000))
            self.assertFalse(common_copper_landing(leaf, (pad, overlap)))

            transverse = trace((2050000, 1200000), (2050000, 2800000), 800000)
            self.assertTrue(common_copper_landing(leaf, (transverse,)))
            parallel = trace((1200000, 2050000), (2800000, 2050000), 800000)
            self.assertFalse(common_copper_landing(leaf, (parallel,)))

    def test_rectangular_landing_proves_group_and_rejects_exposed_second_contact(self):
        import pcbnew

        board = pcbnew.BOARD()
        net = pcbnew.NETINFO_ITEM(board, "PWR")
        board.Add(net)

        def point(x, y):
            return pcbnew.VECTOR2I(round(x * 1e6), round(y * 1e6))

        def terminal(reference, x, y):
            part = pcbnew.FOOTPRINT(board)
            part.SetReference(reference)
            part.SetValue("test")
            part.SetPosition(point(x, y))
            pad = pcbnew.PAD(part)
            pad.SetNumber("1")
            pad.SetSize(point(0.6, 0.6))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pcbnew.LSET.FrontMask())
            pad.SetPosition(point(x, y))
            pad.SetNetCode(net.GetNetCode())
            part.Add(pad)
            board.Add(part)

        def rectangle(start, end):
            shape = pcbnew.PCB_SHAPE(board)
            shape.SetShape(pcbnew.SHAPE_T_RECT)
            shape.SetLayer(pcbnew.F_Cu)
            shape.SetStart(point(*start))
            shape.SetEnd(point(*end))
            shape.SetWidth(0)
            shape.SetFillMode(pcbnew.FILL_T_FILLED_SHAPE)
            shape.SetNetCode(net.GetNetCode())
            board.Add(shape)

        def trace(start, end):
            track = pcbnew.PCB_TRACK(board)
            track.SetLayer(pcbnew.F_Cu)
            track.SetStart(point(*start))
            track.SetEnd(point(*end))
            track.SetWidth(1000000)
            track.SetNetCode(net.GetNetCode())
            board.Add(track)
            return track.m_Uuid.AsString()

        terminal("J1", 2, 3)
        terminal("C1", 5.4, 8)
        rectangle((1, 1), (5, 6))
        branch = {trace((4, 2), (5.4, 3.4)): 1.0, trace((5.4, 3.4), (5.4, 8)): 1.0}
        config = {
            "schema": 1,
            "electrically_verified": False,
            "taps": [
                {
                    "net": "PWR",
                    "width_mm": 1.0,
                    "max_branch_a": 0.001,
                    "terminals": [
                        {
                            "reference": "C1",
                            "pin": "1",
                            "value": "test",
                            "max_current_a": 0.001,
                        }
                    ],
                    "track_width_floors_mm": branch,
                }
            ],
        }
        block, counts = compile_widths(board, config)
        self.assertEqual(counts, {"PWR": 2})
        for uid in branch:
            self.assertIn(f"PWR tap {uid}", block)

        # The new copper touches the original and reaches the branch again
        # beyond the original landing. The branch allowance is no longer proven.
        rectangle((4.3, 5.8), (5.3, 6.65))
        with self.assertRaisesRegex(ValueError, "local load path unproven"):
            compile_widths(board, config)

    def test_a_track_with_two_upstream_contacts_cannot_borrow_a_sense_tap_limit(self):
        # Removing this whole copper item isolates the sense terminal, but the
        # item also bridges a high-current upstream loop. A vertex cut alone
        # therefore does not establish its maximum current.
        graph = {
            "source": {"a"},
            "a": {"source", "wide", "b"},
            "b": {"a", "unknown", "wide"},
            "unknown": {"b"},
            "wide": {"a", "b", "sense"},
            "sense": {"wide"},
        }
        self.assertEqual(
            bounded_leaves(
                graph, {"source", "unknown", "sense"}, {"sense": 0.001}, {"wide"}
            ),
            {},
        )

    def test_native_drc_applies_the_last_net_specific_width_minimum(self):
        import pcbnew
        from core.pcb_io import quiet_duplicate_image_handlers
        from pcb.route_validation import _drc
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory() as folder, quiet_duplicate_image_handlers():
            project = Path(folder)
            source = RouteOptimizerTests.make_board(project)
            (project / "v4.1.kicad_pro").write_text("{}", encoding="utf-8")
            board = pcbnew.LoadBoard(str(source))
            for a, b in (((2, 2), (5, 2)), ((5, 2), (8, 5))):
                track = pcbnew.PCB_TRACK(board)
                track.SetStart(pcbnew.VECTOR2I(*(round(v * 1e6) for v in a)))
                track.SetEnd(pcbnew.VECTOR2I(*(round(v * 1e6) for v in b)))
                track.SetWidth(300000)
                track.SetLayer(pcbnew.F_Cu)
                track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
                board.Add(track)
            pcbnew.SaveBoard(str(source), board)
            rules = source.with_suffix(".kicad_dru")
            base = '(version 1)\n(rule "main" (condition "A.Type == \'Track\'") (constraint track_width (min 0.50mm)))\n'
            rules.write_text(base, encoding="utf-8")
            cli = kicad_cli()
            before = _drc(cli, source, project / "before.json")
            rules.write_text(
                base + '(rule "branch" (condition "A.NetName == \'SIGNAL\'") '
                "(constraint track_width (min 0.20mm)))\n",
                encoding="utf-8",
            )
            after = _drc(cli, source, project / "after.json")
            self.assertEqual(
                sum(v["type"] == "track_width" for v in before["violations"]), 2
            )
            self.assertEqual(
                sum(v["type"] == "track_width" for v in after["violations"]), 0
            )

    def test_changed_width_rules_may_reveal_width_findings_but_never_shorts(self):
        before = {"violations": [], "schematic_parity": [], "unconnected_items": []}
        width = {
            "type": "connection_width",
            "severity": "error",
            "items": [{"uuid": "power"}],
        }
        after = {**before, "violations": [width]}
        check_unchanged_other_rules(before, after)
        with self.assertRaises(RuntimeError):
            check_unchanged_other_rules(
                before, {**before, "violations": [{**width, "type": "shorting_items"}]}
            )

    def test_changed_current_setting_component_blocks_rule_compilation(self):
        import pcbnew

        board = pcbnew.BOARD()
        part = pcbnew.FOOTPRINT(board)
        part.SetReference("R147")
        part.SetValue("1k")
        board.Add(part)
        config = {
            "schema": 1,
            "electrically_verified": False,
            "required_components": [{"reference": "R147", "value": "2.61k"}],
        }
        with self.assertRaisesRegex(ValueError, "R147"):
            compile_widths(board, config)

    def test_native_rail_inventory_and_search_compiler_agree(self):
        import pcbnew
        from pcb.net_routing.rules import RoutingRules, read_rules
        from pcb.route_optimizer import Point, Segment, mm
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory() as folder:
            board = pcbnew.LoadBoard(str(RouteOptimizerTests.make_board(Path(folder))))
            for part in board.GetFootprints():
                part.SetValue("known")
            config = {
                "schema": 1,
                "electrically_verified": False,
                "rails": [
                    {
                        "net": "SIGNAL",
                        "width_mm": 0.20,
                        "terminals": [
                            {"reference": ref, "pin": "1", "value": "known"}
                            for ref in ("J1", "J2")
                        ],
                    }
                ],
            }
            block, _ = compile_widths(board, config)
            path = Path(folder) / "rules.kicad_dru"
            path.write_text("(version 1)\n" + block, encoding="utf-8")
            rules = RoutingRules(
                read_rules(path),
                (),
                {"Default": (mm(0.5), mm(0.2))},
                {"SIGNAL": "Default"},
                mm(0.2),
                mm(0.2),
            )
            segment = Segment(Point.from_mm((2, 2)), Point.from_mm((8, 5)), "F.Cu")
            self.assertEqual(
                rules.profile("SIGNAL", mm(0.5), mm(0.2)).width_at(segment), mm(0.20)
            )
            config["rails"][0]["terminals"].pop()
            with self.assertRaisesRegex(ValueError, "terminal inventory changed"):
                compile_widths(board, config)

    def test_only_leaf_path_receives_the_known_load(self):
        graph = {
            "source": {"trunk"},
            "trunk": {"source", "load", "tap1"},
            "load": {"trunk"},
            "tap1": {"trunk", "tap2"},
            "tap2": {"tap1", "sense"},
            "sense": {"tap2"},
        }
        found = bounded_leaves(
            graph,
            {"source", "load", "sense"},
            {"sense": 0.001},
            {"trunk", "tap1", "tap2"},
        )
        self.assertEqual(found, {"tap1": 0.001, "tap2": 0.001})

    def test_independent_groups_keep_each_existing_leaf_width(self):
        import pcbnew
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory() as folder:
            board = pcbnew.LoadBoard(str(RouteOptimizerTests.make_board(Path(folder))))
            board.FindFootprintByReference("J2").SetPosition(
                pcbnew.VECTOR2I(8000000, 2000000)
            )
            other = pcbnew.FOOTPRINT(board)
            other.SetReference("J3")
            other.SetPosition(pcbnew.VECTOR2I(5000000, 5000000))
            pad = pcbnew.PAD(other)
            pad.SetNumber("1")
            pad.SetSize(pcbnew.VECTOR2I(1000000, 1000000))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pcbnew.LSET.FrontMask())
            pad.SetPosition(other.GetPosition())
            net = board.FindNet("SIGNAL")
            pad.SetNetCode(net.GetNetCode())
            other.Add(pad)
            board.Add(other)
            groups = []
            for ref in ("J2", "J3"):
                board.FindFootprintByReference(ref).SetValue("sense")
                groups.append(
                    {
                        "net": "SIGNAL",
                        "width_mm": 0.3,
                        "max_branch_a": 0.01,
                        "preserve_installed_width": True,
                        "terminals": [
                            {
                                "reference": ref,
                                "pin": "1",
                                "value": "sense",
                                "max_current_a": 0.001,
                            }
                        ],
                    }
                )
            traces = []
            for start, end, width in (
                ((2, 2), (5, 2), 800000),
                ((5, 2), (8, 2), 650000),
                ((5, 2), (5, 5), 725000),
            ):
                trace = pcbnew.PCB_TRACK(board)
                trace.SetLayer(pcbnew.F_Cu)
                trace.SetNetCode(net.GetNetCode())
                trace.SetStart(pcbnew.VECTOR2I(*(p * 1000000 for p in start)))
                trace.SetEnd(pcbnew.VECTOR2I(*(p * 1000000 for p in end)))
                trace.SetWidth(width)
                board.Add(trace)
                traces.append(trace)
            block, counts = compile_widths(
                board, {"schema": 1, "electrically_verified": False, "taps": groups}
            )
            self.assertEqual(counts, {"SIGNAL": 2})
            self.assertNotIn(traces[0].m_Uuid.AsString(), block)
            for track, width in strict_zip(traces[1:], ("0.650000", "0.725000")):
                self.assertTrue(
                    any(
                        track.m_Uuid.AsString() in line and f"(min {width}mm)" in line
                        for line in block.splitlines()
                    )
                )
            board.Remove(traces[0])
            _, isolated = compile_widths(
                board, {"schema": 1, "electrically_verified": False, "taps": groups}
            )
            self.assertEqual(isolated, {"SIGNAL": 0})

    def test_shared_tap_sums_all_downstream_loads(self):
        graph = {
            "source": {"feed"},
            "feed": {"source", "a", "b"},
            "a": {"feed"},
            "b": {"feed"},
        }
        self.assertEqual(
            bounded_leaves(
                graph, {"source", "a", "b"}, {"a": 0.002, "b": 0.003}, {"feed"}
            ),
            {"feed": 0.005},
        )

    def test_mesh_or_unknown_downstream_terminal_never_gets_a_leaf_allowance(self):
        mesh = {
            "source": {"a", "b"},
            "a": {"source", "sense"},
            "b": {"source", "sense"},
            "sense": {"a", "b"},
        }
        self.assertEqual(
            bounded_leaves(mesh, {"source", "sense"}, {"sense": 0.001}, {"a", "b"}), {}
        )
        unknown = {
            "source": {"feed"},
            "feed": {"source", "sense", "unknown"},
            "sense": {"feed"},
            "unknown": {"feed"},
        }
        self.assertEqual(
            bounded_leaves(
                unknown, {"source", "sense", "unknown"}, {"sense": 0.001}, {"feed"}
            ),
            {},
        )

    def test_disconnected_sense_island_has_no_demonstrated_source(self):
        graph = {"source": set(), "tap": {"sense"}, "sense": {"tap"}}
        self.assertEqual(
            bounded_leaves(graph, {"source", "sense"}, {"sense": 0}, {"tap"}), {}
        )


if __name__ == "__main__":
    unittest.main()
