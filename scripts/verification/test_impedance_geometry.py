"""test_impedance_geometry.py: check the impedance geometry review.

Coverage regressions for the read-only impedance geometry review.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

NATIVE = importlib.util.find_spec("pcbnew") is not None


@unittest.skipUnless(NATIVE, "KiCad Python required")
class ImpedanceTests(unittest.TestCase):
    """Tests for the impedance geometry review of a native board."""

    def setUp(self):
        """Create a temporary folder with a stackup specification and a project policy.

        The stackup describes a two-layer board with one reviewed 90 ohm net class.
        """
        import pcbnew
        from pcb.impedance import StackProfile, Trace

        self.pcb, self.profile, self.trace = pcbnew, StackProfile, Trace
        ctx = tempfile.TemporaryDirectory()
        self.addCleanup(ctx.cleanup)
        self.root = Path(ctx.name)
        self.board = self.root / "Arbitrary name.kicad_pcb"
        self.settings = self.board.with_suffix(".kicad_pro")
        self.project = self.root / "autopcb.project.json"
        self.spec = self.root / "stack.json"
        self.row = {
            "signal_layers": ["F.Cu"],
            "reference_layers": [["B.Cu"]],
            "width_mm": 0.2,
            "gap_mm": 0.3,
        }
        self.data = {
            "stackup_id": "two-layer-test",
            "copper_layers": 2,
            "physical_stack_mm": [
                {"layer": "F.Cu", "type": "copper", "thickness": 0.035},
                {
                    "layer": "dielectric 1",
                    "type": "core",
                    "thickness": 1.53,
                    "materials": ["FR4"],
                },
                {"layer": "B.Cu", "type": "copper", "thickness": 0.035},
            ],
            "controlled_impedance": {
                "width_tolerance_mm": 0.001,
                "gap_tolerance_mm": 0.001,
                "sample_step_mm": 0.1,
                "families": [
                    {"netclass": "Reviewed", "target_ohms": 90, "layers": [self.row]}
                ],
            },
        }
        self.spec.write_text(json.dumps(self.data))
        self.project.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "high_speed_reviewed": True,
                    "differential_pairs": [["A", "B"]],
                    "reference_net": "RETURN",
                }
            )
        )

    def build_board(self, *, extra=0, with_plane=True, gap=0.3):
        """Save a board with a differential pair, a return net and an optional plane.

        Net A runs 10 mm on F.Cu. Net B runs parallel to A at the given edge gap and is
        10 mm plus extra long. Net RETURN is a 1 mm stub and, with a plane, also a
        filled zone on B.Cu. The project settings are written next to the board and a
        physical stackup is put into the board file.

        Inputs:
            extra: extra length of net B in mm.
            with_plane: add the filled RETURN zone on B.Cu when true.
            gap: edge-to-edge gap between A and B in mm.
        """
        p = self.pcb
        board = p.BOARD()
        for name in ["A", "B", "RETURN"]:
            board.Add(p.NETINFO_ITEM(board, name))
        for net, y, end in [
            ("A", 0, 10),
            ("B", gap + 0.2, 10 + extra),
            ("RETURN", 4, 1),
        ]:
            item = p.PCB_TRACK(board)
            item.SetStart(p.VECTOR2I(0, p.FromMM(y)))
            item.SetEnd(p.VECTOR2I(p.FromMM(end), p.FromMM(y)))
            item.SetWidth(p.FromMM(0.2))
            item.SetLayer(p.F_Cu)
            item.SetNetCode(board.FindNet(net).GetNetCode())
            board.Add(item)
        if with_plane:
            zone = p.ZONE(board)
            zone.SetLayer(p.B_Cu)
            zone.SetNetCode(board.FindNet("RETURN").GetNetCode())
            fill = p.SHAPE_POLY_SET()
            fill.NewOutline()
            zone.Outline().NewOutline()
            for x, y in ((-2, -2), (20, -2), (20, 3), (-2, 3)):
                fill.Append(p.FromMM(x), p.FromMM(y))
                zone.Outline().Append(p.FromMM(x), p.FromMM(y))
            zone.SetFilledPolysList(p.B_Cu, fill)
            zone.SetIsFilled(True)
            board.Add(zone)
        p.SaveBoard(str(self.board), board)
        # SaveBoard only emits a companion project on some KiCad distributions.
        netclass = {
            "bus_width": 12,
            "wire_width": 6,
            "clearance": 0.2,
            "track_width": 0.2,
            "diff_pair_width": 0.2,
            "diff_pair_gap": 0.3,
            "via_diameter": 0.6,
            "via_drill": 0.3,
            "microvia_diameter": 0.3,
            "microvia_drill": 0.1,
        }
        project = {
            "meta": {"filename": self.settings.name, "version": 3},
            "net_settings": {
                "meta": {"version": 5},
                "classes": [
                    {**netclass, "name": "Default"},
                    {**netclass, "name": "Reviewed", "priority": 0},
                ],
                "netclass_patterns": [
                    {"netclass": "Reviewed", "pattern": name} for name in ("A", "B")
                ],
            },
        }
        self.settings.write_text(json.dumps(project), encoding="utf-8")
        # A programmatically created board may omit the physical section on save.
        from core.kicad_sexpr import parse

        src = self.board.read_text(encoding="utf-8")
        (setup,) = parse(src).children("setup")
        physical = (
            '(stackup (layer "F.Cu" (type "copper") (thickness 0.035)) '
            '(layer "dielectric 1" (type "core") (thickness 1.53) (material "FR4")) '
            '(layer "B.Cu" (type "copper") (thickness 0.035)))'
        )
        if setup.children("stackup"):
            (prior,) = setup.children("stackup")
            src = src[: prior.start] + physical + src[prior.end :]
        else:
            pos = setup.atoms()[0].end
            src = src[:pos] + "\n" + physical + src[pos:]
        self.board.write_text(src, encoding="utf-8")

    def run_cli(self):
        """Run the impedance command on the saved board with the stackup file.

        Outputs:
            A tuple (exit code, parsed JSON from stdout or an empty dictionary, stderr
            text).
        """
        scripts = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(scripts / "run.py"),
                "--project",
                str(self.board),
                "--kicad-python",
                sys.executable,
                "pcb",
                "impedance",
                "--stackup",
                str(self.spec),
                "--json",
                "--check-reference",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return (
            result.returncode,
            json.loads(result.stdout) if result.stdout.strip() else {},
            result.stderr,
        )

    def test_complete_symmetrical_match_reads_only_the_selected_family(self):
        self.build_board()
        sources = {
            p: p.read_bytes()
            for p in (self.board, self.settings, self.spec, self.project)
        }
        code, result, error = self.run_cli()
        self.assertEqual(code, 0, error or result)
        self.assertTrue(result["geometry_matches"])
        self.assertFalse(result["electrically_verified"])
        for row in result["pairs"][0]["layers"]["F.Cu"]["nets"].values():
            self.assertAlmostEqual(row["referenced_mm"], 10)
        self.assertEqual(sources, {p: p.read_bytes() for p in sources})

    def test_extra_length_on_either_conductor_is_reported(self):
        self.build_board(extra=0.05)
        code, result, error = self.run_cli()
        self.assertEqual(code, 2, error)
        nets = result["pairs"][0]["layers"]["F.Cu"]["nets"]
        self.assertTrue(nets["A"]["matches"])
        self.assertFalse(nets["B"]["matches"])
        self.assertAlmostEqual(nets["B"]["matched_mm"], 10)

    def test_gap_and_missing_reference_are_distinct_measurements(self):
        for gap, plane, paired in ((0.32, True, 0), (0.3, False, 10)):
            with self.subTest(gap=gap, plane=plane):
                self.build_board(gap=gap, with_plane=plane)
                code, result, _ = self.run_cli()
                self.assertEqual(code, 2)
                net = result["pairs"][0]["layers"]["F.Cu"]["nets"]["A"]
                self.assertAlmostEqual(net["matched_mm"], paired)
                self.assertEqual(net["referenced_mm"], 0)

    def test_empty_scope_and_different_ordered_stack_cannot_pass(self):
        self.build_board()
        self.project.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "high_speed_reviewed": True,
                    "differential_pairs": [],
                }
            )
        )
        code, _, error = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("nonempty", error)
        self.data["physical_stack_mm"][1]["thickness"] = 1.54
        self.spec.write_text(json.dumps(self.data))
        code, _, error = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("physical stack", error)

    def test_rules_and_parallel_overlap_use_physical_dimensions(self):
        self.assertIn("(opt 0.3mm)", self.profile(self.data).rules())
        a = self.trace(0, 0, 1, 0, 10, 0.2)
        b = self.trace(12, -0.5, -1, 0, 9, 0.2)
        self.assertEqual(a.overlap(b, 0.3, 0), (3, 10, -0.5))
        self.assertEqual(a.union_length([(0, 5), (3, 10), (4, 6)]), 10)
        self.row["reference_layers"] = [["F.Cu"]]
        with self.assertRaisesRegex(ValueError, "adjacent"):
            self.profile(self.data)
