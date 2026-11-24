"""Campaigns edit only selected expressions and protect saved sources."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from core.campaign_cli import apply_plan, main
from core.kicad_campaign import CampaignFile, CampaignForms, CampaignPlanner
from core.kicad_sexpr import Form, parse


PCB = """(kicad_pcb
  (version 20260206)
  (footprint "TestPoint"
    (layer "F.Cu")
    (uuid "foot-1")
    (at 1 2 90)
    (property "Reference" "TP1" (at 0 -1) (layer "F.SilkS"))
    (property "Value" "GND" (at 0 1) (layer "F.Fab"))
    (pad "1" smd circle (at 0 0) (net "GND") (uuid "pad-1"))
  )
  (footprint "TestPoint"
    (uuid "foot-2")
    (at 3 4)
    (property "Reference" "TP2")
  )
  (segment (start 1 2) (end 8 9) (net "GND") (uuid "track-1"))
)
""".replace("\n", "\r\n")

SCHEMATIC = """(kicad_sch
  (lib_symbols (symbol "library" (property "Reference" "TP1")))
  (symbol (lib_id "Test:Part") (at 1 2 0)
    (property "Reference" "TP1")
    (property "Footprint" "Old:Footprint" (at 1 3 0))
    (uuid "placed-1"))
)
"""


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jessie-campaign-test-")
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.board = self.project / "v4.1.kicad_pcb"
        self.schematic = self.project / "v4.1.kicad_sch"
        self.board.write_bytes(PCB.encode())
        self.schematic.write_bytes(SCHEMATIC.encode())
        self.planner = CampaignPlanner(self.project)

    def test_select_direct_children_matches_parser_and_rejects_bad_input(self):
        from core.kicad_sexpr import quote, select_direct_children

        source = (
            "\ufeff(root (setup (a 1)) (ignore "
            + quote('text with "(setup)" and \\ and newline\n')
            + ' (setup nested)) ("setup" (b 2)) (setup (c 3)))'
        )
        for text in (source, PCB, "(root (unrelated 1))"):
            with self.subTest(text=text[:40]):
                expected = [
                    text[child.start : child.end]
                    for child in parse(text).children("setup")
                ]
                self.assertEqual(list(select_direct_children(text, "setup")), expected)
        bad = (
            "",
            "root",
            "((bad))",
            "(root ())",
            "(root (a)) trailing",
            "(root) (second)",
            '(root (a "unterminated))',
            "(root (a)",
        )
        for text in bad:
            with self.subTest(text=text), self.assertRaises(ValueError):
                select_direct_children(text, "setup")

    def test_select_direct_children_matches_parser_on_varied_documents(self):
        import random
        from core.kicad_sexpr import quote, select_direct_children

        rng = random.Random(20261101)
        names = ("setup", "layer", "root", "a", "x y", 'quote"name', "\u03bb")
        values = ("a", "(setup)", 'a"b', r"a\\b", "\n", "\ufeff")

        def form(depth=0):
            name = rng.choice(names)
            token = name if all(c.isalnum() or c in "_-" for c in name) else quote(name)
            parts = [token]
            for _ in range(rng.randrange(4)):
                parts.append(
                    form(depth + 1)
                    if depth < 4 and rng.random() < 0.6
                    else quote(rng.choice(values))
                )
            return "(" + " ".join(parts) + ")"

        for index in range(1000):
            source = ("\ufeff" if index % 7 == 0 else "") + form()
            if index % 3 == 0:
                position = rng.randrange(len(source) + 1)
                source = (
                    source[:position]
                    + rng.choice(('"', ")", "(", "x"))
                    + source[position:]
                )
            if index % 7 == 1 and source:
                source = source[: rng.randrange(len(source))]
            try:
                root = parse(source)
            except ValueError:
                with self.assertRaises(ValueError):
                    select_direct_children(source, "setup")
            else:
                expected = tuple(
                    source[child.start : child.end] for child in root.children("setup")
                )
                self.assertEqual(select_direct_children(source, "setup"), expected)

    def test_grid_preserves_unrelated_bytes_and_crlf(self):
        changes = CampaignFile.grid(
            "pcb", "footprint", "TP", 1, 2, 4, (0.0, 0.0), (2.5, 2.5)
        )
        plan = self.planner.plan("pcb", changes)
        expected = PCB.replace("(at 1 2 90)", "(at 0 0 90)", 1).replace(
            "(at 3 4)", "(at 0 2.5)", 1
        )
        self.assertEqual(plan.after, expected.encode())
        self.assertEqual(self.board.read_bytes(), PCB.encode())
        self.assertEqual(len(plan.edits), 2)
        self.assertIn(b"(segment (start 1 2) (end 8 9)", plan.after)
        self.assertIn(b'(pad "1" smd circle (at 0 0)', plan.after)

    def test_grid_column_order_and_overlap_rejection(self):
        changes = CampaignFile.grid(
            "pcb", "footprint", "TP", 1, 24, 4, (0, 0), (2.5, 2.5)
        )
        self.assertEqual(
            [(c["target"]["reference"], c["x"], c["y"]) for c in changes[:5]],
            [
                ("TP1", "0", "0"),
                ("TP2", "0", "2.5"),
                ("TP3", "0", "5"),
                ("TP4", "0", "7.5"),
                ("TP5", "2.5", "0"),
            ],
        )
        self.assertEqual((changes[-1]["x"], changes[-1]["y"]), ("12.5", "7.5"))
        with self.assertRaisesRegex(ValueError, "overlap"):
            CampaignFile.grid("pcb", "footprint", "TP", 1, 5, 4, (0, 0), (0, 2.5))

    def test_grid_row_order(self):
        changes = CampaignFile.grid(
            "pcb", "footprint", "TP", 1, 24, 4, (0, 0), (2.93, 2.93), order="row"
        )
        self.assertEqual(
            [(c["target"]["reference"], c["x"], c["y"]) for c in changes[:7]],
            [
                ("TP1", "0", "0"),
                ("TP2", "2.93", "0"),
                ("TP3", "5.86", "0"),
                ("TP4", "8.79", "0"),
                ("TP5", "11.72", "0"),
                ("TP6", "14.65", "0"),
                ("TP7", "0", "2.93"),
            ],
        )
        self.assertEqual((changes[-1]["x"], changes[-1]["y"]), ("14.65", "8.79"))
        with self.assertRaisesRegex(ValueError, "order"):
            CampaignFile.grid("pcb", "footprint", "TP", 1, 2, 2, (0, 0), (1, 1), "bad")

    def test_pcb_footprint_rotation_cannot_use_text_only_position_edit(self):
        change = CampaignFile.move("pcb", "footprint", "TP1", 1, 2, 180)
        with self.assertRaisesRegex(ValueError, "native KiCad"):
            self.planner.plan("pcb", [change])
        self.assertEqual(self.board.read_bytes(), PCB.encode())

        unchanged = CampaignFile.move("pcb", "footprint", "TP1", 3, 4, 90)
        self.assertIn(b"(at 3 4 90)", self.planner.plan("pcb", [unchanged]).after)

    def test_property_edit_selects_placed_symbol_not_library_definition(self):
        change = {
            "document": "schematic",
            "target": {"kind": "symbol", "reference": "TP1"},
            "op": "set_property",
            "name": "Footprint",
            "value": 'New:"quoted"\\model',
        }
        plan = self.planner.plan("schematic", [change])
        expected = SCHEMATIC.replace('"Old:Footprint"', '"New:\\"quoted\\"\\\\model"')
        self.assertEqual(plan.after, expected.encode())
        self.assertEqual(self.schematic.read_text(), SCHEMATIC)

    def test_uuid_selects_nested_pad_and_replaces_one_expression(self):
        change = {
            "document": "pcb",
            "target": {"kind": "pad", "uuid": "pad-1"},
            "op": "replace",
            "path": ["at"],
            "expression": "(at -0.5 -1)",
        }
        plan = self.planner.plan("pcb", [change])
        self.assertIn(b'(pad "1" smd circle (at -0.5 -1)', plan.after)
        self.assertIn(b"(at 1 2 90)", plan.after)

    def test_batched_uuid_and_reference_lookups_do_not_rescan_the_tree(self):
        root = parse(PCB)
        targets = [
            {"kind": "pad", "uuid": "pad-1"},
            {"kind": "segment", "uuid": "track-1"},
            {"kind": "footprint", "reference": "TP2"},
        ]
        index = CampaignForms.index(root, [{"target": target} for target in targets])
        expected = [CampaignForms.select(root, target) for target in targets]
        with mock.patch.object(
            Form, "descendants", side_effect=AssertionError("unexpected rescan")
        ):
            self.assertEqual(
                [CampaignForms.select(root, target, index) for target in targets],
                expected,
            )

    def test_batched_index_still_rejects_duplicate_uuids(self):
        root = parse('(kicad_pcb (segment (uuid "same")) (segment (uuid "same")))')
        target = {"kind": "segment", "uuid": "same"}
        index = CampaignForms.index(root, [{"target": target}])
        with self.assertRaisesRegex(ValueError, "exactly one"):
            CampaignForms.select(root, target, index)

    def test_index_preserves_missing_and_invalid_selector_errors(self):
        root = parse(PCB)
        targets = [
            {"kind": "segment", "uuid": "missing"},
            {"kind": "pad", "uuid": "pad-1", "reference": "TP1"},
            {"kind": "footprint"},
            {"kind": "root", "reference": "TP1"},
            {"kind": "pad", "uuid": 42},
            {},
        ]
        index = CampaignForms.index(root, [{"target": target} for target in targets])
        for target in targets:
            with self.subTest(target=target):
                with self.assertRaises(ValueError) as original:
                    CampaignForms.select(root, target)
                with self.assertRaises(type(original.exception)) as indexed:
                    CampaignForms.select(root, target, index)
                self.assertEqual(str(indexed.exception), str(original.exception))

    def test_batched_uuid_edits_preserve_other_source_bytes(self):
        changes = [
            {
                "target": {"kind": "segment", "uuid": "track-1"},
                "op": "replace",
                "path": ["start"],
                "expression": "(start 2 3)",
            },
            {
                "target": {"kind": "pad", "uuid": "pad-1"},
                "op": "replace",
                "path": ["at"],
                "expression": "(at 0.5 0)",
            },
        ]
        expected = PCB.replace("(start 1 2)", "(start 2 3)").replace(
            '(pad "1" smd circle (at 0 0)', '(pad "1" smd circle (at 0.5 0)'
        )
        self.assertEqual(self.planner.plan("pcb", changes).after, expected.encode())
        self.assertEqual(self.board.read_bytes(), PCB.encode())

    def test_generic_insert_and_remove(self):
        target = {"kind": "footprint", "reference": "TP2"}
        changes = [
            {
                "op": "insert",
                "target": target,
                "path": [],
                "expression": '(descr "Hi (there)")',
            },
            {"op": "remove", "target": target, "path": ["uuid"]},
        ]
        result = self.planner.plan("pcb", changes).after.decode()
        self.assertIn('(descr "Hi (there)")', result)
        self.assertNotIn('(uuid "foot-2")', result)
        self.assertEqual(parse(result).name, "kicad_pcb")

    def test_multiple_insertions_at_one_position_keep_campaign_order(self):
        changes = [
            {
                "target": {"kind": "root"},
                "op": "insert",
                "expression": '(fixture "first")',
            },
            {
                "target": {"kind": "root"},
                "op": "insert",
                "expression": '(fixture "second")',
            },
        ]
        plan = CampaignPlanner(self.project).plan("pcb", changes)
        self.assertLess(
            plan.after.index(b'(fixture "first")'),
            plan.after.index(b'(fixture "second")'),
        )
        self.assertEqual(self.board.read_bytes(), PCB.encode())

    def test_ambiguous_missing_and_overlapping_targets_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.planner.plan(
                "pcb",
                [{"op": "set_at", "target": {"kind": "footprint"}, "x": 0, "y": 0}],
            )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.planner.plan(
                "pcb",
                [
                    {
                        "op": "set_at",
                        "target": {"kind": "footprint", "reference": "TP99"},
                        "x": 0,
                        "y": 0,
                    }
                ],
            )
        target = {"kind": "footprint", "reference": "TP1"}
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            self.planner.plan(
                "pcb",
                [
                    {"op": "set_at", "target": target, "x": 0, "y": 0},
                    {
                        "op": "replace",
                        "target": target,
                        "path": ["at"],
                        "expression": "(at 4 5)",
                    },
                ],
            )

    def test_invalid_syntax_and_expression_are_rejected(self):
        for text in ('(root "unterminated)', "(root (at 1 2)", "(root) (second)"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse(text)
        change = {
            "op": "replace",
            "target": {"kind": "footprint", "reference": "TP1"},
            "path": ["at"],
            "expression": "(at 3",
        }
        with self.assertRaises(ValueError):
            self.planner.plan("pcb", [change])

    def test_timestamped_campaign_creation_and_add_grid(self):
        store = CampaignFile(self.project)
        with mock.patch("core.kicad_campaign.datetime") as clock:
            clock.now.return_value.astimezone.return_value = datetime(
                2026, 10, 27, 12, 34, 56, tzinfo=timezone.utc
            )
            path = store.create("Test points")
            self.assertEqual(path.name, "20261027_123456.json")
            with self.assertRaises(FileExistsError):
                store.create("Do not overwrite")
        changes = store.grid("pcb", "footprint", "TP", 1, 2, 4, (0, 0), (2.5, 2.5))
        store.append(path.name, changes)
        self.assertEqual(len(json.loads(path.read_text())["changes"]), 2)
        with self.assertRaisesRegex(ValueError, "config/campaigns"):
            store.path("../other.json")

    def test_preview_cli_does_not_touch_either_design(self):
        store = CampaignFile(self.project)
        campaign = store.create("Preview")
        store.append(campaign.name, [store.move("pcb", "footprint", "TP1", 0, 0)])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(["--project", str(self.project), "preview", campaign.name]), 0
            )
        result = json.loads(output.getvalue())
        self.assertEqual(result["sources"][0]["edits"], 1)
        self.assertEqual(self.board.read_bytes(), PCB.encode())
        self.assertEqual(self.schematic.read_bytes(), SCHEMATIC.encode())

    def test_apply_rejects_campaign_changed_since_preview(self):
        store = CampaignFile(self.project)
        campaign = store.create("Changed campaign")
        store.append(campaign.name, [store.move("pcb", "footprint", "TP1", 0, 0)])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(["--project", str(self.project), "preview", campaign.name]), 0
            )
        preview = json.loads(output.getvalue())
        store.append(campaign.name, [store.move("pcb", "footprint", "TP2", 2, 2)])
        with contextlib.redirect_stderr(io.StringIO()):
            result = main(
                [
                    "--project",
                    str(self.project),
                    "apply",
                    campaign.name,
                    "--document",
                    "pcb",
                    "--expected-sha256",
                    preview["sources"][0]["source_sha256"],
                    "--expected-campaign-sha256",
                    preview["campaign_sha256"],
                ]
            )
        self.assertEqual(result, 2)
        self.assertEqual(self.board.read_bytes(), PCB.encode())

    def test_changed_and_open_sources_are_never_overwritten(self):
        change = CampaignFile.move("pcb", "footprint", "TP1", 0, 0)
        plan = self.planner.plan("pcb", [change])
        self.board.write_bytes(PCB.encode() + b" ")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            apply_plan(plan, self.project, plan.source_sha256)
        self.assertEqual(self.board.read_bytes(), PCB.encode() + b" ")
        self.board.write_bytes(PCB.encode())
        lock = self.board.with_name("~v4.1.kicad_pcb.lck")
        lock.write_text("editor")
        with self.assertRaisesRegex(RuntimeError, "lock"):
            apply_plan(plan, self.project, plan.source_sha256)
        self.assertEqual(self.board.read_bytes(), PCB.encode())
        self.assertEqual(lock.read_text(), "editor")

    def test_apply_uses_kicad_to_validate_a_synthetic_board(self):
        import pcbnew

        native = pcbnew.BOARD()
        fp = pcbnew.FOOTPRINT(native)
        fp.SetReference("TP1")
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(1), pcbnew.FromMM(2)))
        native.Add(fp)
        pcbnew.SaveBoard(str(self.board), native)
        plan = self.planner.plan(
            "pcb", [CampaignFile.move("pcb", "footprint", "TP1", 4, 5)]
        )
        result = apply_plan(plan, self.project, plan.source_sha256)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(Path(result["checkpoint"]).read_bytes(), plan.before)
        moved = pcbnew.LoadBoard(str(self.board)).FindFootprintByReference("TP1")
        self.assertEqual(
            (moved.GetPosition().x, moved.GetPosition().y),
            (pcbnew.FromMM(4), pcbnew.FromMM(5)),
        )
        self.assertEqual(self.schematic.read_bytes(), SCHEMATIC.encode())

    def test_cli_applies_the_exact_previewed_campaign(self):
        import pcbnew

        native = pcbnew.BOARD()
        fp = pcbnew.FOOTPRINT(native)
        fp.SetReference("TP1")
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(1), pcbnew.FromMM(2)))
        native.Add(fp)
        pcbnew.SaveBoard(str(self.board), native)
        store = CampaignFile(self.project)
        campaign = store.create("CLI apply")
        store.append(campaign.name, [store.move("pcb", "footprint", "TP1", 4, 5)])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(["--project", str(self.project), "preview", campaign.name]), 0
            )
        preview = json.loads(output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            status = main(
                [
                    "--project",
                    str(self.project),
                    "apply",
                    campaign.name,
                    "--document",
                    "pcb",
                    "--expected-sha256",
                    preview["sources"][0]["source_sha256"],
                    "--expected-campaign-sha256",
                    preview["campaign_sha256"],
                ]
            )
        self.assertEqual(status, 0)
        self.assertEqual(
            pcbnew.LoadBoard(str(self.board))
            .FindFootprintByReference("TP1")
            .GetPosition()
            .x,
            pcbnew.FromMM(4),
        )

    def test_schematic_apply_is_validated_and_does_not_touch_board(self):
        schematic = (
            '(kicad_sch (version 20250114) (generator "eeschema") '
            '(uuid "22b779ab-117b-4041-8f6c-33e51690e119") '
            '(paper "A4") (lib_symbols) (embedded_fonts no))\n'
        )
        self.schematic.write_text(schematic, encoding="utf-8")
        change = {
            "document": "schematic",
            "target": {"kind": "root"},
            "op": "replace",
            "path": ["paper"],
            "expression": '(paper "A3")',
        }
        plan = self.planner.plan("schematic", [change])
        result = apply_plan(plan, self.project, plan.source_sha256)
        self.assertEqual(result["status"], "applied")
        self.assertIn('(paper "A3")', self.schematic.read_text())
        self.assertEqual(Path(result["checkpoint"]).read_bytes(), plan.before)
        self.assertEqual(self.board.read_bytes(), PCB.encode())

    def test_schematic_lock_blocks_write_and_leaves_both_designs_alone(self):
        change = {
            "document": "schematic",
            "target": {"kind": "symbol", "reference": "TP1"},
            "op": "set_property",
            "name": "Footprint",
            "value": "New:Footprint",
        }
        plan = self.planner.plan("schematic", [change])
        lock = self.schematic.with_name("~v4.1.kicad_sch.lck")
        lock.write_text("editor")
        with self.assertRaisesRegex(RuntimeError, "lock"):
            apply_plan(plan, self.project, plan.source_sha256)
        self.assertEqual(self.schematic.read_bytes(), SCHEMATIC.encode())
        self.assertEqual(self.board.read_bytes(), PCB.encode())


if __name__ == "__main__":
    unittest.main()
