"""Silkscreen repairs cannot change copper, footprint placement or the schematic."""

from pathlib import Path
import tempfile
import unittest


class SilkscreenTests(unittest.TestCase):
    def test_front_connector_bank_shares_a_column_without_changing_other_artwork(self):
        import pcbnew
        from pcb.silkscreen import plan_top_text, verify_top_text_alignment
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            path = RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(path))
            for number, (label, layer) in enumerate(
                [
                    ("J1", pcbnew.F_SilkS),
                    ("FIRST", pcbnew.F_SilkS),
                    ("J2", pcbnew.F_SilkS),
                    ("SECOND", pcbnew.F_SilkS),
                    ("BOTTOM", pcbnew.B_SilkS),
                ]
            ):
                item = pcbnew.PCB_TEXT(board)
                item.SetText(label)
                item.SetLayer(layer)
                item.SetPosition(pcbnew.VECTOR2I(3_000_000, (2 + number) * 1_000_000))
                item.SetTextAngle(pcbnew.EDA_ANGLE(90, pcbnew.DEGREES_T))
                board.Add(item)
            pcbnew.SaveBoard(str(path), board)
            original = path.read_bytes()
            policy = {
                "line_pitch_mm": 1.6,
                "gap_mm": 0.55,
                "banks": [
                    {"side": "right", "items": [["J1", "FIRST"], ["J2", "SECOND"]]}
                ],
            }
            candidate, _ = plan_top_text(project, policy)
            self.assertEqual(path.read_bytes(), original)
            verify_top_text_alignment(original, candidate)
            with self.assertRaisesRegex(RuntimeError, "changed content"):
                verify_top_text_alignment(
                    original, candidate.replace(b'"BOTTOM"', b'"ALTERED"')
                )
            path.write_bytes(candidate)
            saved = pcbnew.LoadBoard(str(path))
            items = {
                str(t.GetText()): t
                for t in saved.GetDrawings()
                if isinstance(t, pcbnew.PCB_TEXT)
            }
            self.assertEqual(
                len(
                    {items[n].GetPosition().x for n in ("J1", "J2", "FIRST", "SECOND")}
                ),
                1,
            )
            for ref, label in (("J1", "FIRST"), ("J2", "SECOND")):
                self.assertEqual(
                    items[label].GetPosition().y - items[ref].GetPosition().y, 1600000
                )
                self.assertEqual(items[ref].GetTextAngle().AsDegrees(), 0)
            self.assertEqual(plan_top_text(project, policy)[0], candidate)

    def test_stock_artwork_is_repaired_even_when_no_text_or_edge_needs_moving(self):
        import pcbnew
        from core.kicad_sexpr import parse
        from pcb.silkscreen import plan_silkscreen, verify_silkscreen_only
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            source = RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            part = board.FindFootprintByReference("J1")
            line = pcbnew.PCB_SHAPE(part)
            line.SetShape(pcbnew.SHAPE_T_SEGMENT)
            line.SetLayer(pcbnew.F_SilkS)
            line.SetStart(pcbnew.VECTOR2I(3000000, 4000000))
            line.SetEnd(pcbnew.VECTOR2I(4000000, 4000000))
            line.SetWidth(120000)
            part.Add(line)
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            text = original.decode()
            footprint = next(
                f
                for f in parse(text).children("footprint")
                if any(
                    p.atoms()[1].value == "Reference" and p.atoms()[2].value == "J1"
                    for p in f.children("property")
                )
            )
            stroke = footprint.children("fp_line")[0]
            end = stroke.children("end")[0]
            changed = (
                text[stroke.start : end.start]
                + "(end 1 -1.2)"
                + text[end.end : stroke.end]
            )
            library = project / "stock.kicad_mod"
            library.write_text('(footprint "Stock" ' + changed + ")")
            candidate, _ = plan_silkscreen(
                project,
                {
                    "schema": 1,
                    "stock_silkscreen": [{"reference": "J1", "path": str(library)}],
                },
            )
            self.assertNotEqual(candidate, original)
            verify_silkscreen_only(original, candidate)

    def test_restoring_library_silk_keeps_custom_models_and_all_pad_geometry(self):
        from pcb.silkscreen import restore_stock_silk, verify_silkscreen_only

        old = '(fp_line (start 0 0) (end 1 0) (layer "F.SilkS") (uuid "old"))'
        new = '(fp_line (start 0 0.3) (end 1 0.3) (layer "F.SilkS") (uuid "stock"))'
        model = '(model "custom.step" (offset (xyz 0 0 0)))'
        source = (
            '(kicad_pcb (footprint "Stock:J" (layer "F.Cu") (property "Reference" "J3") '
            + old
            + model
            + "))"
        )
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "stock.kicad_mod"
            library.write_text('(footprint "J" ' + new + ")")
            after = restore_stock_silk(
                source.encode(), [{"reference": "J3", "path": str(library)}]
            )
            verify_silkscreen_only(source.encode(), after)
            self.assertIn(model.encode(), after)
            self.assertIn(b'(uuid "old")', after)
            self.assertIn(b"(start 0 0.3)", after)
            library.write_text(
                '(footprint "J" ' + new.replace(' (uuid "stock")', "") + ")"
            )
            after = restore_stock_silk(
                source.encode(), [{"reference": "J3", "path": str(library)}]
            )
            self.assertIn(b'(uuid "old")', after)

    def test_moved_test_point_labels_follow_their_footprint_and_orphan_labels_are_removed(
        self,
    ):
        import pcbnew
        from pcb.silkscreen import plan_silkscreen, verify_silkscreen_only
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            source = RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            part = board.FindFootprintByReference("J1")
            part.SetReference("TP1")
            for content, x in [("TP01", 7), ("TP99", 8)]:
                text = pcbnew.PCB_TEXT(board)
                text.SetText(content)
                text.SetLayer(pcbnew.F_SilkS)
                text.SetTextSize(pcbnew.VECTOR2I(600000, 800000))
                text.SetPosition(pcbnew.VECTOR2I(x * 1000000, 7_000_000))
                board.Add(text)
            pcbnew.SaveBoard(str(source), board)
            from core.kicad_sexpr import parse

            text = source.read_text()
            first = next(
                f
                for f in parse(text).children("footprint")
                if any(
                    p.atoms()[1].value == "Reference" and p.atoms()[2].value == "TP1"
                    for p in f.children("property")
                )
            )
            text = (
                text[: first.end - 1]
                + '(property ki_fp_filters "Pin* Test*")'
                + text[first.end - 1 :]
            )
            source.write_text(text)
            pcbnew.SaveBoard(str(source), pcbnew.LoadBoard(str(source)))
            original = source.read_bytes()
            candidate, counts = plan_silkscreen(
                project,
                {
                    "schema": 1,
                    "attach_references": ["TP*"],
                    "remove_orphan_references": True,
                },
            )
            verify_silkscreen_only(original, candidate)
            source.write_bytes(candidate)
            loaded = pcbnew.LoadBoard(str(source))
            part = loaded.FindFootprintByReference("TP1")
            field = part.GetField("Board label")
            self.assertIsNotNone(field)
            self.assertEqual(field.GetText(), "TP01")
            self.assertEqual(part.GetReference(), "TP1")
            self.assertFalse(
                [x for x in loaded.GetDrawings() if isinstance(x, pcbnew.PCB_TEXT)]
            )
            self.assertEqual(counts["removed_orphan_labels"], 1)
            from core.kicad_sexpr import parse
            from pcb.route_validation import _canonical

            def footprints(text):
                return {
                    f.children("uuid")[0].atoms()[1].value: _canonical(f)
                    for f in parse(text).children("footprint")
                }

            before = footprints(candidate.decode())
            pcbnew.SaveBoard(str(source), loaded)
            after = footprints(source.read_text())
            self.assertEqual(
                before,
                after,
                "Owned label fields must survive a native save without reordering",
            )

    def test_caption_alignment_uses_the_whole_component_bank_not_guessed_coordinates(
        self,
    ):
        import pcbnew
        from pcb.silkscreen import plan_silkscreen
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            source = RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            text = pcbnew.PCB_TEXT(board)
            text.SetText("BANK")
            text.SetLayer(pcbnew.F_SilkS)
            text.SetPosition(pcbnew.VECTOR2I(2_000_000, 4_000_000))
            board.Add(text)
            right = max(
                f.GetBoundingBox(False, False).GetRight() for f in board.GetFootprints()
            )
            pcbnew.SaveBoard(str(source), board)
            candidate, _ = plan_silkscreen(
                project,
                {
                    "schema": 1,
                    "text_positions": [
                        {
                            "uuid": text.m_Uuid.AsString(),
                            "right_of": ["J1", "J2"],
                            "gap_mm": 0.5,
                        }
                    ],
                },
            )
            source.write_bytes(candidate)
            loaded = pcbnew.LoadBoard(str(source))
            moved = next(
                x for x in loaded.GetDrawings() if isinstance(x, pcbnew.PCB_TEXT)
            )
            self.assertGreaterEqual(moved.GetBoundingBox().GetX(), right + 500000)

    def test_library_artwork_follows_balanced_edge_clipping_without_changing_pads(self):
        from pcb.silkscreen import sync_silk_libraries

        before = b'(kicad_pcb (footprint "Stock:D_SMB" (layer "B.Cu") '
        before += b'(property "Reference" "D31") (fp_line (start -3.66 -2.15) (end 2.15 -2.15) (layer "B.SilkS"))))'
        after = before.replace(b"-2.15", b"-1.99")
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            library = project / "stock.kicad_mod"
            pad = '(pad "1" smd rect (at 1 0) (size 2 2) (layers "F.Cu" "F.Mask"))'
            source = (
                '(footprint "D_SMB" '
                + pad
                + ' (fp_line (start -3.66 2.15) (end 2.15 2.15) (layer "F.SilkS")))'
            )
            library.write_text(source, encoding="utf-8")
            changes = sync_silk_libraries(
                project,
                before,
                after,
                {
                    "library_silk_sync": [
                        {"reference": "D31", "path": "stock.kicad_mod"}
                    ]
                },
            )
            self.assertEqual(len(changes), 1)
            self.assertIn(pad.encode(), changes[library])
            self.assertIn(b"(start -3.66 1.99)", changes[library])
            self.assertEqual(library.read_text(), source)

    def test_authorized_text_moves_preserve_copper_and_rotated_footprint_placement(
        self,
    ):
        import pcbnew
        from pcb.silkscreen import plan_silkscreen, verify_silkscreen_only
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            source = RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            text = pcbnew.PCB_TEXT(board)
            text.SetText("USB")
            text.SetLayer(pcbnew.F_SilkS)
            text.SetPosition(pcbnew.VECTOR2I(4_000_000, 4_000_000))
            board.Add(text)
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            policy = {
                "schema": 1,
                "edge_margin_mm": 0.25,
                "text_groups": [{"x_mm": 5.0, "items": [text.m_Uuid.AsString()]}],
                "text_positions": [],
            }
            candidate, counts = plan_silkscreen(project, policy)
            self.assertEqual(counts["moved_texts"], 1)
            verify_silkscreen_only(original, candidate)
            source.write_bytes(candidate)
            loaded = pcbnew.LoadBoard(str(source))
            changed = next(
                x for x in loaded.GetDrawings() if isinstance(x, pcbnew.PCB_TEXT)
            )
            self.assertEqual(
                (changed.GetPosition().x, changed.GetPosition().y),
                (5_000_000, 4_000_000),
            )
            with self.assertRaisesRegex(RuntimeError, "non-silkscreen"):
                verify_silkscreen_only(
                    original, candidate.replace(b'(net "SIGNAL")', b'(net "WRONG")')
                )

    def test_edge_graphics_are_clamped_inside_board_without_moving_the_part(self):
        import pcbnew
        from pcb.silkscreen import plan_silkscreen, verify_silkscreen_only
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            source = RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            part = board.FindFootprintByReference("J1")
            graphic = pcbnew.PCB_SHAPE(part)
            graphic.SetShape(pcbnew.SHAPE_T_SEGMENT)
            graphic.SetLayer(pcbnew.B_SilkS)
            graphic.SetWidth(120000)
            graphic.SetStart(pcbnew.VECTOR2I(1_000_000, 7_900_000))
            graphic.SetEnd(pcbnew.VECTOR2I(2_000_000, 7_900_000))
            part.Add(graphic)
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            candidate, counts = plan_silkscreen(
                project, {"schema": 1, "edge_margin_mm": 0.25}
            )
            self.assertEqual(counts["clipped_lines"], 1)
            verify_silkscreen_only(original, candidate)
            source.write_bytes(candidate)
            loaded = pcbnew.LoadBoard(str(source))
            line = next(
                x
                for x in loaded.FindFootprintByReference("J1").GraphicalItems()
                if x.m_Uuid.AsString() == graphic.m_Uuid.AsString()
            )
            self.assertLessEqual(line.GetStart().y, 7_690_000)


if __name__ == "__main__":
    unittest.main()
