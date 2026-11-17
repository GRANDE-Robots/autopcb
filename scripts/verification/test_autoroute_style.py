"""Independent exact-geometry checks for native tracks, turns and editability."""

from __future__ import annotations

import importlib.util
import unittest

import pcbnew

from pcb.route_optimizer import mm


class RoutingStyleTests(unittest.TestCase):
    def test_dangling_single_layer_via_cannot_hide_an_existing_staircase(self):
        api = self.api()
        board = self.board()
        for start, end in (((0, 0), (1, 1)), ((1, 1), (2, 1)), ((2, 1), (3, 2))):
            self.trace(board, start, end)
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(mm(1), mm(1)))
        via.SetWidth(mm(.6))
        via.SetDrill(mm(.3))
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetNetCode(board.FindNet('N').GetNetCode())
        board.Add(via)
        self.assertEqual(len(api.audit_style(board).staircases), 1)

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec("pcb.net_routing.style"))
        from pcb.net_routing import style

        return style

    def board(self):
        board = pcbnew.BOARD()
        board.Add(pcbnew.NETINFO_ITEM(board, "N"))
        return board

    def trace(self, board, start, end):
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I(mm(start[0]), mm(start[1])))
        track.SetEnd(pcbnew.VECTOR2I(mm(end[0]), mm(end[1])))
        track.SetWidth(mm(0.2))
        track.SetLayer(pcbnew.F_Cu)
        track.SetNetCode(board.FindNet("N").GetNetCode())
        board.Add(track)
        return track

    def test_exact_direction_and_right_angle_bends_are_detected(self):
        api = self.api()
        board = self.board()
        first = self.trace(board, (0, 0), (1, 0))
        self.trace(board, (1, 0), (1, 1))
        wrong = self.trace(board, (3, 0), (4, 1.000001))
        report = api.audit_style(board)
        self.assertIn(wrong.m_Uuid.AsString(), report.non_octilinear)
        self.assertEqual(len(report.invalid_bends), 1)
        self.assertIn(first.m_Uuid.AsString(), report.invalid_bends[0].item_ids)

    def test_branch_junction_is_not_misclassified_as_a_two_segment_bend(self):
        api = self.api()
        board = self.board()
        self.trace(board, (-2, 0), (2, 0))
        self.trace(board, (0, 0), (0, 1))
        self.assertEqual(api.audit_style(board).invalid_bends, ())

    def test_redundant_collinear_joint_is_detected(self):
        api = self.api()
        board = self.board()
        self.trace(board, (0, 0), (1, 0))
        self.trace(board, (1, 0), (2, 0))
        self.assertEqual(len(api.audit_style(board).redundant_joints), 1)

    def test_staircase_windows_are_detected(self):
        api = self.api()
        board = self.board()
        for start, end in (((0, 0), (1, 1)), ((1, 1), (2, 1)), ((2, 1), (3, 2))):
            self.trace(board, start, end)
        self.assertEqual(len(api.audit_style(board).staircases), 1)

    def test_staircase_identifies_only_the_tracks_in_the_actual_window(self):
        api = self.api()
        board = self.board()
        points = ((0, 0), (0, 1), (1, 2), (2, 2), (3, 3), (4, 3), (4, 4), (3, 5))
        tracks = [self.trace(board, first, last) for first, last in zip(points, points[1:])]
        findings = api.audit_style(board).staircases
        self.assertEqual(len(findings), 1)
        self.assertEqual(set(findings[0].item_ids), {item.m_Uuid.AsString() for item in tracks[1:4]})

    def test_staircase_identifies_all_tracks_in_a_collinear_split(self):
        api = self.api()
        board = self.board()
        points = ((0, 0), (1, 1), (1.5, 1), (2, 1), (3, 2), (4, 3))
        tracks = [self.trace(board, first, last) for first, last in zip(points, points[1:])]
        findings = api.audit_style(board).staircases
        self.assertEqual(len(findings), 1)
        self.assertEqual(set(findings[0].item_ids), {item.m_Uuid.AsString() for item in tracks})

    def test_standard_dogleg_survives_rotation_reversal_and_collinear_splits(self):
        api = self.api()
        original = ((0, 0), (1, 0), (2, 1), (2, 3), (3, 4), (4, 4))
        for quarter_turns in range(4):
            points = list(original)
            for _ in range(quarter_turns):
                points = [(-y, x) for x, y in points]
            for reverse in (False, True):
                with self.subTest(rotation=quarter_turns, reverse=reverse):
                    board = self.board()
                    path = list(reversed(points)) if reverse else points
                    for a, b in zip(path, path[1:]):
                        half = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                        self.trace(board, a, half)
                        self.trace(board, half, b)
                    self.assertFalse(api.audit_style(board).staircases)
                    self.assertFalse(api.audit_style(board).invalid_bends)

    def test_locked_and_grouped_tracks_are_detected(self):
        api = self.api()
        board = self.board()
        track = self.trace(board, (0, 0), (1, 0))
        track.SetLocked(True)
        group = pcbnew.PCB_GROUP(board)
        board.Add(group)
        group.AddItem(track)
        report = api.audit_style(board)
        self.assertIn(track.m_Uuid.AsString(), report.locked_items)
        self.assertIn(track.m_Uuid.AsString(), report.grouped_items)

    def test_duplicate_overlapping_segments_are_not_accepted(self):
        api = self.api()
        board = self.board()
        self.trace(board, (0, 0), (1, 0))
        self.trace(board, (1, 0), (0, 0))
        report = api.audit_style(board)
        self.assertFalse(report.passed)
        self.assertTrue(report.redundant_joints or report.ambiguous)

    def test_partial_collinear_overlap_is_not_silently_accepted(self):
        api = self.api()
        board = self.board()
        self.trace(board, (0, 0), (2, 0))
        self.trace(board, (1, 0), (3, 0))
        self.assertFalse(api.audit_style(board).passed)

    def test_staircase_detected_across_collinear_width_transition(self):
        api = self.api()
        board = self.board()
        self.trace(board, (0, 0), (1, 1))
        self.trace(board, (1, 1), (1.5, 1))
        wider = self.trace(board, (1.5, 1), (2, 1))
        wider.SetWidth(mm(0.3))
        self.trace(board, (2, 1), (3, 2))
        self.assertTrue(api.audit_style(board).staircases)

    def test_extending_chain_does_not_relabel_old_staircase_as_new(self):
        from pcb.net_routing.controller import new_style_issues

        api = self.api()
        board = self.board()
        for a, b in (((0, 0), (1, 1)), ((1, 1), (2, 1)), ((2, 1), (3, 2))):
            self.trace(board, a, b)
        before = api.audit_style(board)
        self.trace(board, (3, 2), (4, 2))
        after = api.audit_style(board)
        self.assertEqual(before.staircases, after.staircases)
        self.assertFalse(after.passed)  # The old defect still prevents completion.
        self.assertNotIn("staircases", new_style_issues(before, after))
        self.trace(board, (4, 2), (5, 3))
        self.assertIn("staircases", new_style_issues(after, api.audit_style(board)))

    def test_staircase_identity_survives_reversal_and_width_split(self):
        api = self.api()
        first, second = self.board(), self.board()
        for a, b in (((0, 0), (1, 1)), ((1, 1), (2, 1)), ((2, 1), (3, 2))):
            self.trace(first, a, b)
        for a, b in (((3, 2), (2, 1)), ((2, 1), (1.5, 1)), ((1.5, 1), (1, 1)), ((1, 1), (0, 0))):
            self.trace(second, a, b)
        self.assertEqual(api.audit_style(first).staircases, api.audit_style(second).staircases)


class StandaloneRouteStyleTests(unittest.TestCase):
    def test_segment_identity_only_matches_unambiguous_exact_properties(self):
        from pcb.route_validation import _segment_identities

        def board(*items):
            return ("(kicad_pcb " + " ".join(items) + ")").encode()

        def track(identifier, width=".2", extra=""):
            return (
                f'(segment (start 1 2) (end 3 2) (width {width}) (layer "F.Cu") '
                f'(net "N") {extra} (uuid "{identifier}"))'
            )

        old = _segment_identities(board(track("old")))
        same = _segment_identities(board(track("new")))
        changed = _segment_identities(board(track("new", width=".21")))
        self.assertEqual(old["old"], same["new"])
        self.assertNotEqual(old["old"], changed["new"])
        self.assertEqual(_segment_identities(board(track("a"), track("b"))), {})
        nested = _segment_identities(board(track("new", extra='(custom (uuid "other"))')))
        self.assertNotEqual(old["old"], nested["new"])
        self.assertEqual(_segment_identities(board(track("new").replace('(net "N")', '(net 1)'))), {})

    def test_native_dangling_identity_tracks_endpoint_net_layer_width_and_direction(self):
        import pcbnew

        from pcb.route_optimizer import mm
        from pcb.route_validation import _dangling_track_identities

        def identity(start, end, *, width=.2, layer=pcbnew.F_Cu, net="N"):
            board = pcbnew.BOARD()
            netinfo = pcbnew.NETINFO_ITEM(board, net)
            board.Add(netinfo)
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(mm(start[0]), mm(start[1])))
            track.SetEnd(pcbnew.VECTOR2I(mm(end[0]), mm(end[1])))
            track.SetLayer(layer)
            track.SetWidth(mm(width))
            track.SetNetCode(netinfo.GetNetCode())
            board.Add(track)
            identifier = track.m_Uuid.AsString()
            report = {"violations": [{"type": "track_dangling", "items": [{"uuid": identifier}]}]}
            result = _dangling_track_identities(board, report)[identifier]
            self.assertEqual((track.GetStart().x, track.GetStart().y),
                             (mm(start[0]), mm(start[1])))
            self.assertEqual((track.GetEnd().x, track.GetEnd().y),
                             (mm(end[0]), mm(end[1])))
            return result

        old = set(identity((2, 6), (4, 6)))
        extended = set(identity((2, 6), (8, 6)))
        self.assertEqual(len(old), 2)
        self.assertEqual(len(old & extended), 1)
        self.assertFalse(old & set(identity((2, 6), (2, 8))))
        self.assertFalse(old & set(identity((2, 6), (4, 6), width=.21)))
        self.assertFalse(old & set(identity((2, 6), (4, 6), layer=pcbnew.B_Cu)))
        self.assertFalse(old & set(identity((2, 6), (4, 6), net="OTHER")))

    def test_native_validation_rejects_a_new_staircase(self):
        import hashlib
        import tempfile
        from pathlib import Path

        from pcb.route_optimizer import Point
        from pcb.route_validation import RouteContract, validate_routes
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory(prefix="autopcb-native-style-") as directory:
            source = RouteOptimizerTests.make_board(Path(directory))
            before = source.read_bytes()
            pieces = (((2, 2), (3, 3)), ((3, 3), (5, 3)),
                      ((5, 3), (7, 5)), ((7, 5), (8, 5)))
            changes = []
            for index, (start, end) in enumerate(pieces):
                expression = (
                    f'(segment (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) '
                    f'(width 0.2) (layer "F.Cu") (net "SIGNAL") '
                    f'(uuid "00000000-0000-4000-8000-{index + 1:012d}"))'
                )
                changes.append({"document": "pcb", "target": {"kind": "root"},
                                "op": "insert", "path": [], "expression": expression})
            contract = RouteContract("SIGNAL", Point.from_mm((2, 2)), "F.Cu",
                                     Point.from_mm((8, 5)), "F.Cu")
            evidence = validate_routes(source, hashlib.sha256(before).hexdigest(), changes, [contract])
            self.assertEqual(evidence["status"], "rejected", evidence)
            self.assertIn("staircases", evidence["new_style_issues"])
            self.assertEqual(source.read_bytes(), before)

    def test_native_validation_can_restore_the_same_legacy_staircase(self):
        import hashlib
        import tempfile
        from pathlib import Path

        import pcbnew

        from pcb.net_routing.style import audit_style
        from pcb.route_optimizer import Point, mm
        from pcb.route_validation import RouteContract, validate_routes
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory(prefix="autopcb-restore-style-") as directory:
            source = RouteOptimizerTests.make_board(Path(directory))
            board = pcbnew.LoadBoard(str(source))
            pieces = (((2, 2), (3, 3)), ((3, 3), (5, 3)),
                      ((5, 3), (7, 5)), ((7, 5), (8, 5)))
            removed = []
            for start, end in pieces:
                track = pcbnew.PCB_TRACK(board)
                track.SetStart(pcbnew.VECTOR2I(mm(start[0]), mm(start[1])))
                track.SetEnd(pcbnew.VECTOR2I(mm(end[0]), mm(end[1])))
                track.SetWidth(mm(.2))
                track.SetLayer(pcbnew.F_Cu)
                track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
                board.Add(track)
                removed.append({"kind": "segment", "uuid": track.m_Uuid.AsString()})
            self.assertTrue(audit_style(board).staircases)
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            changes = [{"document": "pcb", "target": item, "op": "remove", "path": []}
                       for item in removed]
            for index, (start, end) in enumerate(pieces):
                expression = (
                    f'(segment (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) '
                    f'(width 0.2) (layer "F.Cu") (net "SIGNAL") '
                    f'(uuid "00000000-0000-4000-8000-{index + 1:012d}"))'
                )
                changes.append({"document": "pcb", "target": {"kind": "root"},
                                "op": "insert", "path": [], "expression": expression})
            contract = RouteContract("SIGNAL", Point.from_mm((2, 2)), "F.Cu",
                                     Point.from_mm((8, 5)), "F.Cu", tuple(removed))
            evidence = validate_routes(source, hashlib.sha256(before).hexdigest(), changes, [contract])
            self.assertEqual(evidence["status"], "no_new_drc_violations", evidence)
            self.assertEqual(evidence["new_style_issues"], [])
            self.assertEqual(source.read_bytes(), before)

    def test_native_validation_can_restore_a_track_at_an_unchanged_off_angle_contact(self):
        import hashlib
        import tempfile
        from pathlib import Path

        import pcbnew

        from pcb.net_routing.style import audit_style
        from pcb.route_optimizer import Point, mm
        from pcb.route_validation import RouteContract, validate_routes
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory(prefix="autopcb-restore-contact-") as directory:
            source = RouteOptimizerTests.make_board(Path(directory))
            board = pcbnew.LoadBoard(str(source))
            tracks = []
            for start, end in (
                ((2, 2), (4, 2.000001)),
                ((4, 2.000001), (5.000001, 2.000001)),
                ((5.000001, 2.000001), (8, 5)),
            ):
                track = pcbnew.PCB_TRACK(board)
                track.SetStart(pcbnew.VECTOR2I(mm(start[0]), mm(start[1])))
                track.SetEnd(pcbnew.VECTOR2I(mm(end[0]), mm(end[1])))
                track.SetWidth(mm(.2))
                track.SetLayer(pcbnew.F_Cu)
                track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
                board.Add(track)
                tracks.append(track)
            self.assertTrue(audit_style(board).non_octilinear)
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            removal = {"kind": "segment", "uuid": tracks[1].m_Uuid.AsString()}
            expression = (
                '(segment (start 4 2.000001) (end 5.000001 2.000001) (width 0.2) '
                '(layer "F.Cu") (net "SIGNAL") '
                '(uuid "00000000-0000-4000-8000-000000000001"))'
            )
            changes = [
                {"document": "pcb", "target": removal, "op": "remove", "path": []},
                {"document": "pcb", "target": {"kind": "root"}, "op": "insert",
                 "path": [], "expression": expression},
            ]
            contract = RouteContract(
                "SIGNAL", Point.from_mm((2, 2)), "F.Cu",
                Point.from_mm((8, 5)), "F.Cu", (removal,),
            )
            evidence = validate_routes(source, hashlib.sha256(before).hexdigest(), changes, [contract])
            self.assertEqual(evidence["status"], "no_new_drc_violations", evidence)
            self.assertEqual(evidence["new_style_issues"], [])
            self.assertEqual(source.read_bytes(), before)

    def test_replacing_identical_copper_keeps_existing_warning_identity(self):
        import hashlib
        import tempfile
        from pathlib import Path

        import pcbnew

        from pcb.route_optimizer import Point, mm
        from pcb.route_validation import RouteContract, validate_routes
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory(prefix="autopcb-restore-warning-") as directory:
            source = RouteOptimizerTests.make_board(Path(directory))
            board = pcbnew.LoadBoard(str(source))
            tracks = []
            for start, end in (((2, 2), (4, 2.000001)), ((4, 2.000001), (8, 2.000001))):
                track = pcbnew.PCB_TRACK(board)
                track.SetStart(pcbnew.VECTOR2I(mm(start[0]), mm(start[1])))
                track.SetEnd(pcbnew.VECTOR2I(mm(end[0]), mm(end[1])))
                track.SetWidth(mm(.2))
                track.SetLayer(pcbnew.F_Cu)
                track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
                board.Add(track)
                tracks.append(track)
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            removal = {"kind": "segment", "uuid": tracks[1].m_Uuid.AsString()}
            expression = (
                '(segment (start 4 2.000001) (end 8 2.000001) (width 0.2) '
                '(layer "F.Cu") (net "SIGNAL") '
                '(uuid "00000000-0000-4000-8000-000000000001"))'
            )
            changes = [
                {"document": "pcb", "target": removal, "op": "remove", "path": []},
                {"document": "pcb", "target": {"kind": "root"}, "op": "insert",
                 "path": [], "expression": expression},
            ]
            contract = RouteContract(
                "SIGNAL", Point.from_mm((2, 2)), "F.Cu",
                Point.from_mm((8, 2.000001)), "F.Cu", (removal,),
            )
            evidence = validate_routes(source, hashlib.sha256(before).hexdigest(), changes, [contract])
            self.assertEqual(evidence["status"], "no_new_drc_violations", evidence["new_violations"])
            self.assertEqual(evidence["candidate_warnings"], 3)
            changed = [changes[0], {**changes[1], "expression": expression.replace("(width 0.2)", "(width 0.21)")}]
            evidence = validate_routes(source, hashlib.sha256(before).hexdigest(), changed, [contract])
            self.assertEqual(evidence["status"], "rejected")
            self.assertTrue(evidence["new_violations"])
            self.assertEqual(source.read_bytes(), before)

    def test_replacing_identical_copper_keeps_existing_bend_identity(self):
        import hashlib
        import tempfile
        from pathlib import Path

        import pcbnew

        from pcb.net_routing.style import audit_style
        from pcb.route_optimizer import Point, mm
        from pcb.route_validation import RouteContract, validate_routes
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory(prefix="autopcb-restore-bend-") as directory:
            source = RouteOptimizerTests.make_board(Path(directory))
            board = pcbnew.LoadBoard(str(source))
            tracks = []
            for start, end in (((2, 2), (4, 2)), ((4, 2), (4, 5)), ((4, 5), (8, 5))):
                track = pcbnew.PCB_TRACK(board)
                track.SetStart(pcbnew.VECTOR2I(mm(start[0]), mm(start[1])))
                track.SetEnd(pcbnew.VECTOR2I(mm(end[0]), mm(end[1])))
                track.SetWidth(mm(.2))
                track.SetLayer(pcbnew.F_Cu)
                track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
                board.Add(track)
                tracks.append(track)
            self.assertTrue(audit_style(board).invalid_bends)
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            removal = {"kind": "segment", "uuid": tracks[1].m_Uuid.AsString()}
            changes = [
                {"document": "pcb", "target": removal, "op": "remove", "path": []},
                {"document": "pcb", "target": {"kind": "root"}, "op": "insert", "path": [],
                 "expression": '(segment (start 4 2) (end 4 5) (width 0.2) (layer "F.Cu") '
                               '(net "SIGNAL") (uuid "00000000-0000-4000-8000-000000000001"))'},
            ]
            contract = RouteContract(
                "SIGNAL", Point.from_mm((2, 2)), "F.Cu", Point.from_mm((8, 5)), "F.Cu", (removal,),
            )
            evidence = validate_routes(source, hashlib.sha256(before).hexdigest(), changes, [contract])
            self.assertEqual(evidence["status"], "no_new_drc_violations", evidence["new_style_issues"])
            self.assertEqual(evidence["new_style_issues"], [])
            self.assertEqual(source.read_bytes(), before)

    def test_moving_the_hidden_second_dangling_endpoint_is_rejected(self):
        import hashlib
        import tempfile
        from pathlib import Path

        import pcbnew

        from pcb.route_optimizer import Point, mm
        from pcb.route_validation import RouteContract, validate_routes
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory(prefix="autopcb-hidden-dangling-") as directory:
            source = RouteOptimizerTests.make_board(Path(directory))
            board = pcbnew.LoadBoard(str(source))
            tracks = []
            for start, end in (((2, 2), (8, 5)), ((2, 6), (4, 6))):
                track = pcbnew.PCB_TRACK(board)
                track.SetStart(pcbnew.VECTOR2I(mm(start[0]), mm(start[1])))
                track.SetEnd(pcbnew.VECTOR2I(mm(end[0]), mm(end[1])))
                track.SetWidth(mm(.2))
                track.SetLayer(pcbnew.F_Cu)
                track.SetNetCode(board.FindNet("SIGNAL").GetNetCode())
                board.Add(track)
                tracks.append(track)
            pcbnew.SaveBoard(str(source), board)
            before = source.read_bytes()
            removal = {"kind": "segment", "uuid": tracks[1].m_Uuid.AsString()}
            changes = [
                {"document": "pcb", "target": removal, "op": "remove", "path": []},
                {"document": "pcb", "target": {"kind": "root"}, "op": "insert", "path": [],
                 "expression": '(segment (start 2 6) (end 5 6) (width 0.2) (layer "F.Cu") '
                               '(net "SIGNAL") (uuid "00000000-0000-4000-8000-000000000001"))'},
            ]
            contract = RouteContract(
                "SIGNAL", Point.from_mm((2, 2)), "F.Cu", Point.from_mm((8, 5)), "F.Cu", (removal,),
            )
            evidence = validate_routes(source, hashlib.sha256(before).hexdigest(), changes, [contract])
            self.assertEqual(evidence["status"], "rejected")
            self.assertTrue(any(item["type"] == "track_dangling" for item in evidence["new_violations"]))
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
