"""Width repair retains clearance and connectivity rather than hiding violations."""

from pathlib import Path
import tempfile
import unittest


class WidthRepairTests(unittest.TestCase):
    def test_only_an_independently_landed_sense_tip_can_be_removed(self):
        import pcbnew
        from pcb.branch_widths import compile_widths
        from pcb.width_repair import redundant_terminal_ends
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory() as folder:
            board = pcbnew.LoadBoard(str(RouteOptimizerTests.make_board(Path(folder))))
            sense = board.FindFootprintByReference('J2')
            sense.SetValue('sense')
            def add(start, end):
                trace = pcbnew.PCB_TRACK(board)
                trace.SetNetCode(board.FindNet('SIGNAL').GetNetCode())
                trace.SetLayer(pcbnew.F_Cu)
                trace.SetWidth(300000)
                trace.SetStart(pcbnew.VECTOR2I(*start))
                trace.SetEnd(pcbnew.VECTOR2I(*end))
                board.Add(trace)
                return trace
            add((2000000, 2000000), (5000000, 5000000))
            parent = add((5000000, 5000000), (7980000, 5000000))
            tip = add((7980000, 5000000), (8000000, 5020000))
            config = {'schema': 1, 'electrically_verified': False, 'taps': [{
                'net': 'SIGNAL', 'width_mm': .30, 'max_branch_a': .01,
                'terminals': [{'reference': 'J2', 'pin': '1', 'value': 'sense', 'max_current_a': .001}]
            }]}
            rules, _ = compile_widths(board, config)
            uid = tip.m_Uuid.AsString()
            self.assertEqual(redundant_terminal_ends(board, {uid}, rules), {uid})
            self.assertFalse(redundant_terminal_ends(board, {uid}, ''))
            self.assertFalse(redundant_terminal_ends(board, {uid, parent.m_Uuid.AsString()}, rules))
            via = pcbnew.PCB_VIA(board)
            via.SetNetCode(tip.GetNetCode())
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetPosition(tip.GetEnd())
            via.SetWidth(600000)
            via.SetDrill(300000)
            board.Add(via)
            self.assertFalse(redundant_terminal_ends(board, {uid}, rules))

    def test_interior_shift_widens_without_removing_the_old_junctions(self):
        import sys
        import pcbnew
        from core.pcb_io import copper_boundary
        from pcb.route_validation import _drc
        from pcb.width_repair import plan_shifted_widening, required_widths
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            path = RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(path))
            tracks = []
            for a, b, width in (((2, 2), (3, 2), 500000), ((3, 2), (5, 2), 200000),
                                ((5, 2), (8, 5), 500000)):
                track = pcbnew.PCB_TRACK(board)
                track.SetNetCode(board.FindNet('SIGNAL').GetNetCode())
                track.SetLayer(pcbnew.F_Cu)
                track.SetWidth(width)
                track.SetStart(pcbnew.VECTOR2I(*(v * 1000000 for v in a)))
                track.SetEnd(pcbnew.VECTOR2I(*(v * 1000000 for v in b)))
                board.Add(track)
                tracks.append(track)
            uid = tracks[1].m_Uuid.AsString()
            boundary = {item.m_Uuid.AsString() for item in (tracks[0], tracks[2])}
            self.assertEqual(set(copper_boundary(board, (tracks[1],))), boundary)
            pcbnew.SaveBoard(str(path), board)
            path.with_suffix('.kicad_pro').write_text('{}', encoding='utf-8')
            path.with_suffix('.kicad_dru').write_text(
                '(version 1)\n(rule "required" (constraint track_width (min 0.5mm)))\n', encoding='utf-8'
            )
            original = path.read_bytes()
            self.assertIsNone(plan_shifted_widening(project, uid, 500000, (0, 150001)))
            candidate = plan_shifted_widening(project, uid, 500000, (0, 125000))
            self.assertIsNotNone(candidate)
            self.assertEqual(path.read_bytes(), original)
            cli = Path(sys.executable).with_name('kicad-cli.exe')
            before = _drc(cli, path, project / 'before.json')
            path.write_bytes(candidate)
            after = _drc(cli, path, project / 'after.json')
            self.assertIn(uid, required_widths(before))
            self.assertEqual(required_widths(after), {})
            self.assertFalse(after['unconnected_items'])

    def test_coordinated_widening_releases_an_obstructed_track_without_narrowing_neighbor(self):
        import sys
        import pcbnew
        from pcb.net_routing.protection import ProtectedState
        from pcb.route_validation import _drc
        from pcb.width_repair import coupled_neighbors, plan_coupled_widening, required_widths, width_candidate
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            path = RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(path))
            board.Add(pcbnew.NETINFO_ITEM(board, 'WIDE'))
            for name, x in (('J1', 2), ('J2', 8)):
                board.FindFootprintByReference(name).SetPosition(pcbnew.VECTOR2I(x*1000000, 2500000))

            def add(name, a, b):
                item = pcbnew.PCB_TRACK(board)
                item.SetNetCode(board.FindNet(name).GetNetCode())
                item.SetLayer(pcbnew.F_Cu)
                item.SetWidth(200000)
                item.SetStart(pcbnew.VECTOR2I(*(round(v*1e6) for v in a)))
                item.SetEnd(pcbnew.VECTOR2I(*(round(v*1e6) for v in b)))
                board.Add(item)
                return item.m_Uuid.AsString()

            target = add('WIDE', (3,3.55), (7,3.55))
            add('SIGNAL', (2,2.5), (2.6,3.1))
            blocker = add('SIGNAL', (2.6,3.1), (7.4,3.1))
            add('SIGNAL', (7.4,3.1), (8,2.5))
            pcbnew.SaveBoard(str(path), board)
            path.with_suffix('.kicad_pro').write_text('{"net_settings":{}}')
            path.with_suffix('.kicad_dru').write_text('(version 1)\n(rule "width" '
                '(condition "A.NetName == \'WIDE\'") (constraint track_width (min 0.60mm)))\n')
            original = path.read_bytes()
            protection = ProtectedState.capture(original, b'', preserve_all_placement=True)
            path.write_bytes(width_candidate(project, {target: 600000}))
            cli = Path(sys.executable).with_name('kicad-cli.exe')
            direct = _drc(cli, path, project/'obstacles.json')
            self.assertEqual(coupled_neighbors(direct, {target}, {blocker}), {target: (blocker,)})
            self.assertEqual(coupled_neighbors(direct, {target}, set()), {})
            path.write_bytes(original)
            candidate = plan_coupled_widening(project, target, 600000, {blocker: 200000}, 5)
            self.assertIsNotNone(candidate)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(protection.differences(candidate, b''))
            path.write_bytes(candidate)
            after = _drc(cli, path, project/'accepted.json')
            self.assertNotIn(target, required_widths(after))
            self.assertFalse([v for v in after['violations'] if v['type'] in {'clearance','shorting_items'}])
            saved = pcbnew.LoadBoard(str(path))
            self.assertTrue(all(t.GetWidth() >= 200000 for t in saved.GetTracks()))

    def test_width_repair_can_reanchor_inside_the_same_fixed_pad(self):
        import pcbnew
        from pcb.net_routing.protection import ProtectedState
        from pcb.width_repair import plan_width_reroute
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            board.Add(pcbnew.NETINFO_ITEM(board, 'OTHER'))
            part = board.FindFootprintByReference('J2')
            part.SetPosition(pcbnew.VECTOR2I(8000000, 1800000))
            def line(net, a, b, width):
                track = pcbnew.PCB_TRACK(board)
                track.SetNetCode(board.FindNet(net).GetNetCode())
                track.SetLayer(pcbnew.F_Cu)
                track.SetStart(pcbnew.VECTOR2I(*a))
                track.SetEnd(pcbnew.VECTOR2I(*b))
                track.SetWidth(width)
                board.Add(track)
                return track
            thin = line('SIGNAL', (2000000, 1800000), (8000000, 1800000), 200000)
            line('OTHER', (1100000, 1390000), (2600000, 1390000), 150000)
            pcbnew.SaveBoard(str(source), board)
            source.with_suffix('.kicad_dru').write_text('(version 1)\n(rule "main" '
                '(condition "A.NetName == \'SIGNAL\'") (constraint track_width (min 0.60mm)))\n')
            original = source.read_bytes()
            protection = ProtectedState.capture(original, b'', preserve_all_placement=True)
            candidate = plan_width_reroute(project, thin.m_Uuid.AsString(), 600000, 5)
            self.assertIsNotNone(candidate)
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse(protection.differences(candidate, b''))
            source.write_bytes(candidate)
            trial = pcbnew.LoadBoard(str(source))
            routed = [x for x in trial.GetTracks() if x.GetNetname() == 'SIGNAL']
            self.assertTrue(routed and all(x.GetWidth() >= 600000 for x in routed))
            connectivity = trial.GetConnectivity()
            connectivity.Build(trial)
            self.assertEqual({str(p.GetParentFootprint().GetReference())
                              for t in routed for p in connectivity.GetConnectedPads(t)}, {'J1', 'J2'})

    def test_native_obstacles_include_footprint_embedded_rule_areas(self):
        import pcbnew
        from pcb.route_native import read_native_obstacles
        from pcb.route_optimizer import GridRouter, Point, mm
        from pcb.route_validation import _drc
        from verification.test_route_optimizer import RouteOptimizerTests
        import sys

        with tempfile.TemporaryDirectory() as folder:
            source = RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            part = board.FindFootprintByReference('J1')
            zone = pcbnew.ZONE(part)
            zone.SetIsRuleArea(True)
            zone.SetLayer(pcbnew.F_Cu)
            zone.SetDoNotAllowTracks(True)
            zone.SetDoNotAllowVias(True)
            polygon = zone.Outline()
            polygon.NewOutline()
            for x, y in ((3, 1), (5, 1), (5, 3), (3, 3)):
                polygon.Append(x*1000000, y*1000000)
            part.Add(zone)
            pcbnew.SaveBoard(str(source), board)
            original = source.read_bytes()
            native = read_native_obstacles(source, 'SIGNAL', assume_refill=True)
            keepouts = [o for o in native.obstacles if o.kind == 'keepout' and o.boundary_only]
            self.assertEqual(len(keepouts), 1)
            self.assertEqual(keepouts[0].lower.x, 3000000)
            self.assertEqual(keepouts[0].upper.y, 3000000)
            self.assertEqual(keepouts[0].layers, ('F.Cu',))
            grid = GridRouter(Point.from_mm((0, 0)), Point.from_mm((8, 6)), mm(.02),
                              ('F.Cu',), 'SIGNAL', mm(.2), mm(.2), mm(.6), mm(.3), keepouts)
            def edge(y):
                return (Point.from_mm((3.2, y)), Point.from_mm((4.8, y)))
            self.assertFalse(grid.edge_clear(*edge(3.08), 'F.Cu'))
            self.assertTrue(grid.edge_clear(*edge(3.18), 'F.Cu'))
            self.assertEqual(source.read_bytes(), original)
            for height, forbidden in ((3.18, False), (3.08, True)):
                segment = pcbnew.PCB_TRACK(board)
                segment.SetNetCode(board.FindNet('SIGNAL').GetNetCode())
                segment.SetLayer(pcbnew.F_Cu)
                a, b = edge(height)
                segment.SetStart(pcbnew.VECTOR2I(a.x, a.y))
                segment.SetEnd(pcbnew.VECTOR2I(b.x, b.y))
                segment.SetWidth(mm(.2))
                board.Add(segment)
                pcbnew.SaveBoard(str(source), board)
                result = Path(folder)/'native.json'
                result.unlink(missing_ok=True)
                report = _drc(Path(sys.executable).with_name('kicad-cli.exe'), source, result)
                self.assertEqual(any(v['type'] == 'items_not_allowed' for v in report['violations']),
                                 forbidden)
                board.Remove(segment)

    def test_series_run_extends_away_from_connectivity_wrapped_via(self):
        import pcbnew
        from pcb.width_repair import width_chain
        from pcb.branch_widths import contact_port

        board = pcbnew.BOARD()
        net = pcbnew.NETINFO_ITEM(board, 'N')
        board.Add(net)
        via = pcbnew.PCB_VIA(board)
        via.SetNetCode(net.GetNetCode())
        via.SetPosition(pcbnew.VECTOR2I(1000000, 1000000))
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetWidth(600000)
        via.SetDrill(300000)
        board.Add(via)
        tracks = []
        for a, b in (((1, 1), (2, 2)), ((2, 2), (3, 2))):
            track = pcbnew.PCB_TRACK(board)
            track.SetNetCode(net.GetNetCode())
            track.SetLayer(pcbnew.F_Cu)
            track.SetStart(pcbnew.VECTOR2I(*(v*1000000 for v in a)))
            track.SetEnd(pcbnew.VECTOR2I(*(v*1000000 for v in b)))
            track.SetWidth(300000)
            board.Add(track)
            tracks.append(track)
        connection = board.GetConnectivity()
        connection.Build(board)
        wrapped = next(x for x in connection.GetConnectedTracks(tracks[0])
                       if x.Type() == pcbnew.PCB_VIA_T)
        self.assertEqual(contact_port(tracks[0], wrapped), (1000000, 1000000))
        chain, a, b = width_chain(board, tracks[0])
        self.assertEqual({x.m_Uuid.AsString() for x in chain},
                         {x.m_Uuid.AsString() for x in tracks})
        self.assertEqual((a.x, a.y, b.x, b.y), (1000000, 1000000, 3000000, 2000000))

    def test_covered_cleanup_refuses_changed_retained_copper_and_zone_fills(self):
        from pcb.width_repair import verify_covered_edit, ConnectivityRegression
        def trace(uid):
            return f'(segment (start 1 1) (end 2 1) (width .2) (layer "F.Cu") (net "N") (uuid "{uid}"))'
        zone = '(zone (uuid "z") (net "GND") (layer "F.Cu") '
        zone += '(filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 3 0) (xy 3 3))))'
        original = ('(kicad_pcb '+trace('keep')+trace('drop')+zone+')').encode()
        expected = original.replace(trace('drop').encode(),b'')
        verify_covered_edit(original,expected,{'drop'})
        with self.assertRaisesRegex(ConnectivityRegression,'retained copper'):
            verify_covered_edit(original,expected.replace(b'(width .2)',b'(width .1)'),{'drop'})
        with self.assertRaisesRegex(ConnectivityRegression,'filled copper'):
            verify_covered_edit(original,expected.replace(b'(xy 3 3)',b'(xy 2 3)'),{'drop'})
        with self.assertRaisesRegex(ConnectivityRegression,'retained copper'):
            verify_covered_edit(original,original,{'drop'})

    def test_covered_cleanup_selects_only_unlocked_same_layer_pad_contained_copper(self):
        import pcbnew
        from pcb.width_repair import covered_width_tracks
        from verification.test_route_optimizer import RouteOptimizerTests
        with tempfile.TemporaryDirectory() as folder:
            source = RouteOptimizerTests.make_board(Path(folder))
            original = source.read_bytes()
            board = pcbnew.LoadBoard(str(source))
            board.Add(pcbnew.NETINFO_ITEM(board,'OTHER'))
            def trace(net,layer,a,b):
                item = pcbnew.PCB_TRACK(board)
                item.SetNetCode(board.FindNet(net).GetNetCode())
                item.SetLayer(layer)
                item.SetStart(pcbnew.VECTOR2I(*(round(v*1e6) for v in a)))
                item.SetEnd(pcbnew.VECTOR2I(*(round(v*1e6) for v in b)))
                item.SetWidth(200000)
                board.Add(item)
                return item
            inside = trace('SIGNAL',pcbnew.F_Cu,(2,2),(2.1,2))
            outside = trace('SIGNAL',pcbnew.F_Cu,(2.1,2),(3,2))
            back = trace('SIGNAL',pcbnew.B_Cu,(2,2),(2.1,2))
            foreign = trace('OTHER',pcbnew.F_Cu,(2,2),(2.1,2))
            requested = {t.m_Uuid.AsString() for t in (inside,outside,back,foreign)}
            self.assertEqual(covered_width_tracks(board,requested),frozenset({inside.m_Uuid.AsString()}))
            inside.SetLocked(True)
            self.assertFalse(covered_width_tracks(board,requested))
            self.assertEqual(source.read_bytes(),original)

    def test_covered_cleanup_retains_the_witness_and_keeps_exposed_copper(self):
        import pcbnew
        from pcb.width_repair import covered_width_tracks

        board = pcbnew.BOARD()
        board.Add(pcbnew.NETINFO_ITEM(board, 'POWER'))
        net = board.FindNet('POWER').GetNetCode()

        def trace(first, last, layer=pcbnew.F_Cu):
            item = pcbnew.PCB_TRACK(board)
            item.SetNetCode(net)
            item.SetLayer(layer)
            item.SetStart(pcbnew.VECTOR2I(*first))
            item.SetEnd(pcbnew.VECTOR2I(*last))
            item.SetWidth(400000)
            board.Add(item)
            return item

        witness = trace((4000000, 5000000), (6000000, 5000000))
        duplicate = trace((6000000, 5000000), (6000000, 5000000))
        offset = trace((5900000, 5001000), (6000000, 5001000))
        other_layer = trace((6000000, 5000000), (6000000, 5000000), pcbnew.B_Cu)
        exposed = trace((4500000, 5000000), (5500000, 5000000))
        exposed.SetHasSolderMask(True)
        zoned = trace((7400000, 5000000), (8600000, 5000000))
        zone = pcbnew.ZONE(board)
        zone.SetLayer(pcbnew.F_Cu)
        zone.SetNetCode(net)
        polygon = pcbnew.SHAPE_POLY_SET()
        polygon.NewOutline()
        for point in ((7000000, 4000000), (9000000, 4000000), (9000000, 6000000), (7000000, 6000000)):
            polygon.Append(*point)
        zone.SetFilledPolysList(pcbnew.F_Cu, polygon)
        board.Add(zone)
        requested = {item.m_Uuid.AsString() for item in
                     (witness, duplicate, offset, other_layer, exposed, zoned)}
        self.assertEqual(covered_width_tracks(board, requested),
                         {duplicate.m_Uuid.AsString(), zoned.m_Uuid.AsString()})

    def test_anchored_series_crosses_overlapping_chamfers_without_accepting_a_branch(self):
        import pcbnew
        from pcb.width_repair import anchored_series

        board = pcbnew.BOARD()
        board.SetCopperLayerCount(4)
        board.Add(pcbnew.NETINFO_ITEM(board, 'POWER'))
        net = board.FindNet('POWER').GetNetCode()

        def trace(first, last, width):
            item = pcbnew.PCB_TRACK(board)
            item.SetNetCode(net)
            item.SetLayer(pcbnew.In1_Cu)
            item.SetStart(pcbnew.VECTOR2I(*first))
            item.SetEnd(pcbnew.VECTOR2I(*last))
            item.SetWidth(width)
            board.Add(item)
            return item

        corners = ((1000000, 1000000), (1000000, 2000000),
                   (1260000, 2260000), (1260000, 5000000))
        tracks = [trace(first, last, width) for first, last, width in
                  zip(corners, corners[1:], (750000, 750000, 1000000))]
        result = anchored_series(board, tracks[1])
        self.assertIsNotNone(result)
        chain, a, b = result
        self.assertEqual({item.m_Uuid.AsString() for item in chain}, {item.m_Uuid.AsString() for item in tracks})
        self.assertEqual(((a.x, a.y), (b.x, b.y)), (corners[0], corners[-1]))
        trace(corners[1], (0, 2000000), 750000)
        self.assertIsNone(anchored_series(board, tracks[1]))

    def test_anchored_series_matches_small_offsets_only_between_through_vias(self):
        import pcbnew
        from pcb.width_repair import anchored_series

        board = pcbnew.BOARD()
        board.SetCopperLayerCount(4)
        board.Add(pcbnew.NETINFO_ITEM(board, 'POWER'))
        net = board.FindNet('POWER').GetNetCode()
        endpoints = ((3_000_000, 4_000_000), (7_000_000, 4_075_000))
        for point in endpoints:
            via = pcbnew.PCB_VIA(board)
            via.SetNetCode(net)
            via.SetViaType(pcbnew.VIATYPE_THROUGH)
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetPosition(pcbnew.VECTOR2I(*point))
            via.SetWidth(600_000)
            via.SetDrill(300_000)
            board.Add(via)

        tracks = []
        for start, end in ((endpoints[0], (5_000_000, 4_000_000)),
                           ((5_000_000, 4_075_000), endpoints[1])):
            track = pcbnew.PCB_TRACK(board)
            track.SetNetCode(net)
            track.SetLayer(pcbnew.In1_Cu)
            track.SetStart(pcbnew.VECTOR2I(*start))
            track.SetEnd(pcbnew.VECTOR2I(*end))
            track.SetWidth(750_000)
            board.Add(track)
            tracks.append(track)

        chain, start, end = anchored_series(board, tracks[0])
        self.assertEqual(len(chain), 2)
        self.assertEqual(((start.x, start.y), (end.x, end.y)), endpoints)
        tracks[1].SetStart(pcbnew.VECTOR2I(5_120_000, 4_075_000))
        self.assertIsNone(anchored_series(board, tracks[0]))

    def test_layer_repair_reuses_series_end_vias_without_increasing_barrel_distance(self):
        import pcbnew
        from pcb.width_repair import plan_width_reroute, repair_layers
        from pcb.net_routing.protection import ProtectedState
        from core.kicad_campaign import CampaignPlanner
        from pcb.route_native import _configured_copper_thicknesses
        from verification.test_route_optimizer import RouteOptimizerTests
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            board.SetCopperLayerCount(6)
            board.Add(pcbnew.NETINFO_ITEM(board, 'OTHER'))
            def trace(net, a, b, layer, width):
                item = pcbnew.PCB_TRACK(board)
                item.SetNetCode(board.FindNet(net).GetNetCode())
                item.SetLayer(layer)
                item.SetStart(pcbnew.VECTOR2I(*(round(v*1e6) for v in a)))
                item.SetEnd(pcbnew.VECTOR2I(*(round(v*1e6) for v in b)))
                item.SetWidth(width)
                board.Add(item)
                return item
            vias = []
            for x in (3, 7):
                via = pcbnew.PCB_VIA(board)
                via.SetNetCode(board.FindNet('SIGNAL').GetNetCode())
                via.SetViaType(pcbnew.VIATYPE_THROUGH)
                via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
                via.SetPosition(pcbnew.VECTOR2I(x*1000000,4000000))
                via.SetWidth(600000)
                via.SetDrill(300000)
                board.Add(via)
                vias.append(via)
            thin = trace('SIGNAL', (3,4), (7,4), pcbnew.In1_Cu, 200000)
            trace('SIGNAL', (2,4), (3,4), pcbnew.B_Cu, 600000)
            tail = trace('SIGNAL', (7,4), (8,4), pcbnew.B_Cu, 600000)
            trace('OTHER', (4,.5), (4,3.6), pcbnew.In1_Cu, 200000)
            trace('OTHER', (4,4.4), (4,7.5), pcbnew.In1_Cu, 200000)
            # A shortcut must not acquire this unrelated current branch merely
            # because the copper happens to have the same net name.
            guard = trace('SIGNAL', (5,3), (5,5), pcbnew.In2_Cu, 200000)
            original_ids = {t.m_Uuid.AsString() for t in board.GetTracks()}
            pcbnew.SaveBoard(str(source), board)
            layers = ('F.Cu','In1.Cu','In2.Cu','In3.Cu','In4.Cu','B.Cu')
            stack = '(stackup ' + ' '.join(f'(layer "{name}" (type "copper") '
                f'(thickness {0.035 if name in ("F.Cu","B.Cu") else 0.061}))' for name in layers) + ')'
            plan = CampaignPlanner(project).plan('pcb',[{'op':'replace','target':{'kind':'root'},
                'path':['setup','stackup'],'expression':stack}])
            source.write_bytes(plan.after)
            source.with_suffix('.kicad_pro').write_text('{"net_settings":{}}')
            source.with_suffix('.kicad_dru').write_text('(version 1)')
            original = source.read_bytes()
            protected = ProtectedState.capture(original,b'',preserve_all_placement=True)
            import sys
            from pcb.route_validation import _drc
            cli = Path(sys.executable).with_name('kicad-cli.exe')
            before_drc = _drc(cli,source,project/'before.json')
            copper = dict(_configured_copper_thicknesses(original))
            a, b = thin.GetStart(), thin.GetEnd()
            choices = repair_layers(board,(thin,),a,b,copper)
            self.assertEqual(choices[0],'In1.Cu')
            self.assertEqual(set(choices[1:]),{'In2.Cu','In3.Cu','In4.Cu'})
            self.assertEqual(repair_layers(board,(thin,),a,b,{}),('In1.Cu',))
            tail.SetLayer(pcbnew.F_Cu)
            self.assertEqual(repair_layers(board,(thin,),a,b,copper),('In1.Cu',))
            tail.SetLayer(pcbnew.B_Cu)
            candidate = plan_width_reroute(project,thin.m_Uuid.AsString(),600000,8)
            self.assertIsNotNone(candidate)
            self.assertEqual(source.read_bytes(),original)
            self.assertFalse(protected.differences(candidate,b''))
            source.write_bytes(candidate)
            after_drc = _drc(cli,source,project/'after.json')
            for report in (before_drc,after_drc):
                self.assertFalse([v for v in report['violations']
                                  if v['type'] in {'shorting_items','clearance','hole_clearance','items_not_allowed'}])
            routed = pcbnew.LoadBoard(str(source))
            self.assertEqual({v.m_Uuid.AsString() for v in routed.GetTracks() if isinstance(v,pcbnew.PCB_VIA)},
                             {v.m_Uuid.AsString() for v in vias})
            self.assertIn(guard.m_Uuid.AsString(),{t.m_Uuid.AsString() for t in routed.GetTracks()})
            new_tracks = [t for t in routed.GetTracks() if t.m_Uuid.AsString() not in original_ids]
            self.assertTrue(new_tracks)
            self.assertTrue(all(routed.GetLayerName(t.GetLayer()) in {'In3.Cu','In4.Cu'} and t.GetWidth()>=600000
                                for t in new_tracks))
            connectivity = routed.GetConnectivity()
            connectivity.Build(routed)
            first = next(v for v in routed.GetTracks() if v.m_Uuid==vias[0].m_Uuid)
            self.assertIn(vias[1].m_Uuid.AsString(),{x.m_Uuid.AsString() for x in connectivity.GetConnectedItems(first)})

    def test_width_repair_compares_staircase_geometry_without_relabeling_old_defects(self):
        import pcbnew
        from pcb.width_repair import verify_routing_style, ConnectivityRegression
        from pcb.net_routing.style import audit_style
        def board():
            value = pcbnew.BOARD()
            value.Add(pcbnew.NETINFO_ITEM(value, 'SIGNAL'))
            return value
        def trace(value, a, b, width):
            item = pcbnew.PCB_TRACK(value)
            item.SetNetCode(value.FindNet('SIGNAL').GetNetCode())
            item.SetLayer(pcbnew.F_Cu)
            item.SetStart(pcbnew.VECTOR2I(*(round(v*1e6) for v in a)))
            item.SetEnd(pcbnew.VECTOR2I(*(round(v*1e6) for v in b)))
            item.SetWidth(width)
            value.Add(item)
        before, after = board(), board()
        points = ((0, 0), (1, 1), (2, 1), (3, 2))
        for a, b in zip(points, points[1:]):
            trace(before, a, b, 200000)
            trace(after, a, b, 300000)
        old, current = audit_style(before), audit_style(after)
        self.assertTrue(old.staircases)
        self.assertFalse(current.passed)
        verify_routing_style(old, current)
        trace(after, (3, 2), (4, 2), 300000)
        trace(after, (4, 2), (5, 3), 300000)
        with self.assertRaisesRegex(ConnectivityRegression, 'staircases'):
            verify_routing_style(old, audit_style(after))

    def test_reroute_materializes_short_escape_and_wider_distribution(self):
        import pcbnew
        from pcb.width_repair import plan_width_reroute
        from verification.test_route_optimizer import RouteOptimizerTests
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            part = board.FindFootprintByReference('J1')
            corners = ((1, 1), (3, 1), (3, 3), (1, 3))
            for a, b in zip(corners, (*corners[1:], corners[0])):
                line = pcbnew.PCB_SHAPE(part)
                line.SetShape(pcbnew.SHAPE_T_SEGMENT)
                line.SetLayer(pcbnew.F_CrtYd)
                line.SetStart(pcbnew.VECTOR2I(*(round(v * 1e6) for v in a)))
                line.SetEnd(pcbnew.VECTOR2I(*(round(v * 1e6) for v in b)))
                line.SetWidth(50000)
                part.Add(line)
            track = pcbnew.PCB_TRACK(board)
            track.SetNetCode(board.FindNet('SIGNAL').GetNetCode())
            track.SetLayer(pcbnew.F_Cu)
            track.SetStart(pcbnew.VECTOR2I(2000000, 2000000))
            track.SetEnd(pcbnew.VECTOR2I(6000000, 2000000))
            track.SetWidth(200000)
            board.Add(track)
            pcbnew.SaveBoard(str(source), board)
            source.with_suffix('.kicad_pro').write_text('{"net_settings":{}}')
            source.with_suffix('.kicad_dru').write_text('(version 1)\n'
                '(rule "trunk" (condition "A.NetName == \'SIGNAL\'") (constraint track_width (min 0.6mm)))\n'
                '(rule "escape" (condition "A.NetName == \'SIGNAL\' && A.intersectsCourtyard(\'J1\')") '
                '(constraint track_width (min 0.3mm)))')
            original = source.read_bytes()
            candidate = plan_width_reroute(project, track.m_Uuid.AsString(), 300000, 5)
            self.assertIsNotNone(candidate)
            self.assertEqual(source.read_bytes(), original)
            source.write_bytes(candidate)
            routed = pcbnew.LoadBoard(str(source))
            traces = list(routed.GetTracks())
            self.assertTrue(all(t.GetWidth() >= 300000 for t in traces))
            outside = [t for t in traces if max(t.GetStart().x, t.GetEnd().x) > 3500000]
            self.assertTrue(outside)
            self.assertTrue(all(t.GetWidth() >= 600000 for t in outside))

    def test_series_repair_replaces_obsolete_corners_and_preserves_branch_contacts(self):
        import pcbnew
        from pcb.width_repair import plan_width_reroute
        from pcb.net_routing.protection import ProtectedState
        from verification.test_route_optimizer import RouteOptimizerTests
        for branching in (False, True):
            with self.subTest(branching=branching), tempfile.TemporaryDirectory() as folder:
                project = Path(folder)
                source = RouteOptimizerTests.make_board(project)
                board = pcbnew.LoadBoard(str(source))
                def trace(a, b):
                    value = pcbnew.PCB_TRACK(board)
                    value.SetNetCode(board.FindNet('SIGNAL').GetNetCode())
                    value.SetLayer(pcbnew.F_Cu)
                    value.SetStart(pcbnew.VECTOR2I(*(round(v * 1e6) for v in a)))
                    value.SetEnd(pcbnew.VECTOR2I(*(round(v * 1e6) for v in b)))
                    value.SetWidth(200000)
                    board.Add(value)
                    return value
                left = trace((2, 2), (3, 3))
                middle = trace((3, 3), (5, 3))
                right = trace((5, 3), (6, 2))
                branch = trace((3, 3), (3, 5)) if branching else None
                pcbnew.SaveBoard(str(source), board)
                source.with_suffix('.kicad_pro').write_text('{"net_settings":{}}')
                source.with_suffix('.kicad_dru').write_text('(version 1)')
                original = source.read_bytes()
                protected = ProtectedState.capture(original, b'', preserve_all_placement=True)
                candidate = plan_width_reroute(project, middle.m_Uuid.AsString(), 600000, 5)
                self.assertIsNotNone(candidate)
                self.assertEqual(source.read_bytes(), original)
                self.assertFalse(protected.differences(candidate, b''))
                source.write_bytes(candidate)
                routed = pcbnew.LoadBoard(str(source))
                present = {t.m_Uuid.AsString() for t in routed.GetTracks()}
                self.assertNotIn(middle.m_Uuid.AsString(), present)
                self.assertNotIn(right.m_Uuid.AsString(), present)
                if branching:
                    self.assertIn(left.m_Uuid.AsString(), present)
                    self.assertIn(branch.m_Uuid.AsString(), present)
                    connectivity = routed.GetConnectivity()
                    connectivity.Build(routed)
                    pad = list(routed.FindFootprintByReference('J1').Pads())[0]
                    connected = {t.m_Uuid.AsString() for t in connectivity.GetConnectedItems(pad)}
                    self.assertIn(branch.m_Uuid.AsString(), connected)
                else:
                    self.assertNotIn(left.m_Uuid.AsString(), present)
                self.assertFalse(any(isinstance(t, pcbnew.PCB_VIA) for t in routed.GetTracks()))

    def test_neck_aliases_need_the_same_unchanged_copper_region_and_measurement(self):
        from pcb.width_repair import introduced_findings
        def warning(items, value='.1500'):
            return {'type': 'connection_width', 'severity': 'warning',
                    'description': f'minimum connection width .2000 mm; actual {value} mm (B.Cu)',
                    'items': [{'uuid': uid, 'description': 'Track [POWER]'} for uid in items]}
        first, other = warning(['left', 'middle']), warning(['pad', 'right'])
        before = {'violations': [first], 'schematic_parity': [], 'unconnected_items': [], 'ignored_checks': []}
        regions = {uid: 'same unchanged neighborhood' for uid in ('left', 'middle', 'pad', 'right')}
        after = {**before, 'violations': [other]}
        self.assertEqual(introduced_findings(before, after, neck_regions=regions), [])
        self.assertEqual(introduced_findings(before, after), [other])
        changed = warning(['pad', 'right'], '.1000')
        self.assertEqual(introduced_findings(before, {**after, 'violations': [changed]}, neck_regions=regions), [changed])
        self.assertTrue(introduced_findings(before, {**after, 'violations': [other, other]}, neck_regions=regions))
        self.assertTrue(introduced_findings(before, after, neck_regions={**regions, 'pad': 'different', 'right': 'different'}))
        for description in ('min connection width 2.0000 mm; actual .1500 mm', 'unrecognized measurement'):
            prior = {**before, 'violations': [{**first, 'description': description}]}
            candidate = {**after, 'violations': [{**other, 'description': description}]}
            self.assertEqual(introduced_findings(prior, candidate, neck_regions=regions), candidate['violations'])

    def test_neck_regions_exclude_changed_nearby_copper_and_zone_mediated_nets(self):
        import pcbnew
        from pcb.width_repair import unchanged_neck_regions
        from verification.test_route_optimizer import RouteOptimizerTests
        with tempfile.TemporaryDirectory() as folder:
            source = RouteOptimizerTests.make_board(Path(folder))
            board = pcbnew.LoadBoard(str(source))
            near = pcbnew.PCB_TRACK(board)
            near.SetNetCode(board.FindNet('SIGNAL').GetNetCode())
            near.SetLayer(pcbnew.F_Cu)
            near.SetStart(pcbnew.VECTOR2I(2000000, 2000000))
            near.SetEnd(pcbnew.VECTOR2I(4000000, 2000000))
            near.SetWidth(200000)
            board.Add(near)
            far = pcbnew.PCB_TRACK(board)
            far.SetNetCode(near.GetNetCode())
            far.SetLayer(pcbnew.F_Cu)
            far.SetStart(pcbnew.VECTOR2I(7000000, 5000000))
            far.SetEnd(pcbnew.VECTOR2I(8000000, 5000000))
            far.SetWidth(200000)
            board.Add(far)
            pcbnew.SaveBoard(str(source), board)
            before = pcbnew.LoadBoard(str(source))
            far.SetWidth(300000)
            regions = unchanged_neck_regions(before, board)
            self.assertIn(near.m_Uuid.AsString(), regions)
            other = pcbnew.NETINFO_ITEM(board, 'OTHER')
            board.Add(other)
            crossing = pcbnew.PCB_TRACK(board)
            crossing.SetNetCode(other.GetNetCode())
            crossing.SetLayer(pcbnew.F_Cu)
            crossing.SetStart(pcbnew.VECTOR2I(3000000, 2500000))
            crossing.SetEnd(pcbnew.VECTOR2I(3500000, 2500000))
            crossing.SetWidth(200000)
            board.Add(crossing)
            self.assertNotIn(near.m_Uuid.AsString(), unchanged_neck_regions(before, board))
            board.Remove(crossing)
            near.SetWidth(300000)
            self.assertNotIn(near.m_Uuid.AsString(), unchanged_neck_regions(before, board))
            self.assertNotIn(list(board.FindFootprintByReference('J1').Pads())[0].m_Uuid.AsString(),
                             unchanged_neck_regions(before, board))
            zone = pcbnew.ZONE(board)
            zone.SetNetCode(near.GetNetCode())
            zone.SetLayer(pcbnew.F_Cu)
            board.Add(zone)
            self.assertEqual(unchanged_neck_regions(before, board), {})

    def test_reroute_headings_account_for_fixed_neighboring_tracks(self):
        import pcbnew
        from pcb.width_repair import route_headings
        board = pcbnew.BOARD()
        def trace(a, b):
            value = pcbnew.PCB_TRACK(board)
            value.SetStart(pcbnew.VECTOR2I(*a))
            value.SetEnd(pcbnew.VECTOR2I(*b))
            value.SetLayer(pcbnew.F_Cu)
            value.SetWidth(200000)
            board.Add(value)
            return value
        target = trace((2000000, 2000000), (5000000, 2000000))
        trace((1000000, 1000000), (2000000, 2000000))
        trace((5000000, 2000000), (5000000, 4000000))
        self.assertEqual(route_headings(board, target), (frozenset({0, 1, 2}), frozenset({1, 2, 3})))

    def test_detours_cannot_trade_old_width_errors_for_new_undersized_segments(self):
        from pcb.width_repair import introduced_findings
        old = {'type': 'track_width', 'severity': 'error', 'description': 'min width 0.5 mm',
               'items': [{'uuid': 'old'}]}
        before = {'violations': [old], 'unconnected_items': [], 'schematic_parity': [], 'ignored_checks': []}
        new = {**old, 'items': [{'uuid': 'detour'}]}
        self.assertEqual(introduced_findings(before, {**before, 'violations': [new]}, include_widths=True), [new])
        self.assertEqual(introduced_findings(before, before, include_widths=True), [])
        self.assertEqual(introduced_findings(before, {**before, 'violations': []}, include_widths=True), [])

    def test_width_rerouter_avoids_obstructing_copper_without_new_vias_or_part_moves(self):
        import pcbnew
        from pcb.width_repair import plan_width_reroute
        from pcb.net_routing.protection import ProtectedState
        from verification.test_route_optimizer import RouteOptimizerTests
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            source = RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            other = pcbnew.NETINFO_ITEM(board, 'OTHER')
            board.Add(other)
            def track(net, a, b):
                value = pcbnew.PCB_TRACK(board)
                value.SetNetCode(board.FindNet(net).GetNetCode())
                value.SetLayer(pcbnew.F_Cu)
                value.SetStart(pcbnew.VECTOR2I(*(round(x * 1e6) for x in a)))
                value.SetEnd(pcbnew.VECTOR2I(*(round(x * 1e6) for x in b)))
                value.SetWidth(200000)
                board.Add(value)
                return value
            thin = track('SIGNAL', (2, 2), (5, 2))
            track('SIGNAL', (5, 2), (7, 2)).SetWidth(600000)
            track('OTHER', (3.2, 2.45), (3.8, 2.45))
            pcbnew.SaveBoard(str(source), board)
            source.with_suffix('.kicad_pro').write_text('{"net_settings":{}}')
            source.with_suffix('.kicad_dru').write_text('(version 1)')
            original = source.read_bytes()
            protected = ProtectedState.capture(original, b'', preserve_all_placement=True)
            from unittest.mock import patch
            from pcb.route_optimizer import SearchBudgetExceeded
            with patch('pcb.route_optimizer.GridRouter', side_effect=SearchBudgetExceeded('raster budget')):
                self.assertIsNone(plan_width_reroute(project, thin.m_Uuid.AsString(), 600000, 5))
            candidate = plan_width_reroute(project, thin.m_Uuid.AsString(), 600000, 5)
            self.assertIsNotNone(candidate)
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse(protected.differences(candidate, b''))
            source.write_bytes(candidate)
            routed = pcbnew.LoadBoard(str(source))
            added = [t for t in routed.GetTracks() if t.GetNetname() == 'SIGNAL']
            self.assertTrue(all(not isinstance(t, pcbnew.PCB_VIA) and t.GetWidth() >= 600000 for t in added))
            self.assertGreater(len(added), 1)
            from pcb.net_routing.style import audit_style
            self.assertFalse(audit_style(routed).redundant_joints)
            self.assertLessEqual(sum(t.GetLength() for t in added) / 600000, thin.GetLength() / 200000)
            candidate = source.read_bytes()
            source.with_suffix('.kicad_pro').write_text('{"net_settings":{"netclass_patterns":'
                '[{"netclass":"USB_DATA","pattern":"SIGNAL"}]}}')
            self.assertIsNone(plan_width_reroute(project, added[0].m_Uuid.AsString(), 700000, 5))
            self.assertEqual(source.read_bytes(), candidate)

    def test_widening_cannot_reuse_a_revoked_or_changed_branch_flow_proof(self):
        from pcb.width_repair import ConnectivityRegression, verify_tap_proofs
        proof = '(rule "AutoPCB power tap trace" (constraint track_width (min 0.300mm)))'
        verify_tap_proofs(proof, proof + '\n(rule "AutoPCB power tap another" (constraint track_width (min 0.300mm)))')
        with self.assertRaisesRegex(ConnectivityRegression, 'branch'):
            verify_tap_proofs(proof, '')
        with self.assertRaisesRegex(ConnectivityRegression, 'branch'):
            verify_tap_proofs(proof, proof.replace('0.300', '0.200'))

    def test_known_conflicts_are_removed_before_retrying_unattributed_plane_effects(self):
        from pcb.width_repair import reduce_conflicts
        conflicts = [{'items': [{'uuid': 'wide'}, {'uuid': 'pad'}]},
                     {'items': [{'uuid': 'plane'}, {'uuid': 'unmodified'}]}]
        self.assertEqual(reduce_conflicts({'wide': 500000, 'other': 500000},
                                         {'wide': 500000, 'other': 500000}, conflicts),
                         ({'other': 500000}, {'wide'}))

    def test_zone_free_monotone_widths_allow_only_native_neck_marker_reassignment(self):
        from pcb.width_repair import introduced_findings
        def finding(uid, actual='0.1000'):
            return {'type': 'connection_width', 'severity': 'warning',
                    'description': f'minimum connection width 0.2000 mm; actual {actual} mm (B.Cu)',
                    'items': [{'uuid': uid, 'description': 'Track [PWR] on B.Cu'}]}
        before = {'violations': [finding('a')], 'unconnected_items': [], 'schematic_parity': [], 'ignored_checks': []}
        after = {**before, 'violations': [finding('b')]}
        self.assertEqual(introduced_findings(before, after, stable_neck_nets=frozenset({'PWR'})), [])
        self.assertTrue(introduced_findings(before, after))
        self.assertTrue(introduced_findings(before, {**after, 'violations': [finding('b', '0.0500')]},
                                           stable_neck_nets=frozenset({'PWR'})))
        self.assertTrue(introduced_findings(before, {**after, 'violations': [finding('b'), finding('c')]},
                                           stable_neck_nets=frozenset({'PWR'})))

    def test_known_short_is_attributed_before_partitioning_for_disconnected_ground(self):
        from pcb.width_repair import introduced_findings
        before = {'violations': [], 'unconnected_items': [], 'schematic_parity': [], 'ignored_checks': []}
        short = {'type': 'shorting_items', 'severity': 'error', 'items': [{'uuid': 'wide'}]}
        after = {**before, 'violations': [short], 'unconnected_items': [{'items': []}]}
        self.assertEqual(introduced_findings(before, after), [short])

    def test_batch_search_keeps_safe_widenings_when_another_widening_cuts_a_ground_island(self):
        from pcb.width_repair import ConnectivityRegression, select_widenings
        attempted = []
        def check(widths):
            attempted.append(dict(widths))
            if 'bad' in widths:
                raise ConnectivityRegression('ground island disconnected')
            return []
        selected, deferred = select_widenings({'a': 500000, 'bad': 500000, 'c': 500000},
                                              {'a': 500000, 'bad': 500000, 'c': 500000}, check,
                                              max_checks=12, progress=lambda text: None)
        self.assertEqual(selected, {'a': 500000, 'c': 500000})
        self.assertEqual(deferred, {'bad'})
        self.assertLessEqual(len(attempted), 12)

    def test_exhausted_batch_budget_retains_only_the_last_validated_widths(self):
        from pcb.width_repair import ConnectivityRegression, select_widenings
        def check(widths):
            raise ConnectivityRegression('ground island disconnected')
        selected, deferred = select_widenings({'a': 500000, 'b': 500000},
                                              {'a': 500000, 'b': 500000}, check,
                                              max_checks=1, progress=lambda text: None)
        self.assertEqual(selected, {})
        self.assertEqual(deferred, {'a', 'b'})

    def test_native_width_inventory_exceeds_kicads_199_per_type_reporting_limit(self):
        import sys
        import pcbnew
        from core.pcb_io import quiet_duplicate_image_handlers
        from pcb.route_validation import _drc
        from verification.test_route_optimizer import RouteOptimizerTests
        with tempfile.TemporaryDirectory() as folder, quiet_duplicate_image_handlers():
            project = Path(folder)
            source = RouteOptimizerTests.make_board(project)
            board = pcbnew.LoadBoard(str(source))
            for index in range(210):
                track = pcbnew.PCB_TRACK(board)
                x = 2_000_000 + index * 20000
                track.SetStart(pcbnew.VECTOR2I(x, 2_000_000))
                track.SetEnd(pcbnew.VECTOR2I(x + 20000, 2_000_000))
                track.SetWidth(250000)
                track.SetLayer(pcbnew.F_Cu)
                track.SetNetCode(board.FindNet('SIGNAL').GetNetCode())
                board.Add(track)
            pcbnew.SaveBoard(str(source), board)
            source.with_suffix('.kicad_pro').write_text('{}', encoding='utf-8')
            source.with_suffix('.kicad_dru').write_text('(version 1)\n(rule "power" '
                '(condition "A.Type == \'Track\'") (constraint track_width (min 0.5mm)))', encoding='utf-8')
            original, rules = source.read_bytes(), source.with_suffix('.kicad_dru').read_bytes()
            report = _drc(Path(sys.executable).with_name('kicad-cli.exe'), source, project / 'drc.json')
            self.assertEqual(sum(v['type'] == 'track_width' for v in report['violations']), 210)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(source.with_suffix('.kicad_dru').read_bytes(), rules)

    def test_width_edits_preserve_every_byte_except_width_and_reject_reductions(self):
        from pcb.width_repair import width_candidate
        source = b'(kicad_pcb (version 20260206) (segment (start 1 1) (end 3 1) (width 0.25) (layer "F.Cu") (net "PWR") (uuid "trace")))'
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            path = project / 'v4.1.kicad_pcb'
            path.write_bytes(source)
            self.assertEqual(width_candidate(project, {'trace': 500000}),
                             source.replace(b'(width 0.25)', b'(width 0.5)'))
            with self.assertRaisesRegex(ValueError, 'narrow'):
                width_candidate(project, {'trace': 200000})

    def test_unknown_drc_units_and_ambiguous_targets_stop_instead_of_guessing(self):
        from pcb.width_repair import required_widths
        def finding(text, ids=('trace',)):
            return {'type': 'track_width', 'description': text,
                    'items': [{'uuid': uid} for uid in ids]}
        report = {'violations': [finding("Track width (rule 'power' min width 1.0000 mm; actual 0.4 mm)")]}
        self.assertEqual(required_widths(report), {'trace': 1000000})
        report['violations'][0]['description'] = 'Track width (maximum width 1 mm)'
        with self.assertRaisesRegex(ValueError, 'minimum'):
            required_widths(report)
        report['violations'][0] = finding('min width 1 mm', ('trace', 'other'))
        with self.assertRaisesRegex(ValueError, 'one'):
            required_widths(report)

    def test_new_shorts_open_connections_and_degraded_existing_clearance_are_rejected(self):
        from pcb.width_repair import introduced_findings
        old = {'type': 'clearance', 'severity': 'error', 'description': 'gap 0.15',
               'items': [{'uuid': 'a'}, {'uuid': 'b'}]}
        report = {'violations': [old], 'unconnected_items': [], 'schematic_parity': [], 'ignored_checks': []}
        self.assertEqual(introduced_findings(report, report), [])
        changed = {**old, 'description': 'gap 0.05'}
        self.assertEqual(introduced_findings(report, {**report, 'violations': [changed]}), [changed])
        with self.assertRaisesRegex(RuntimeError, 'connectivity'):
            introduced_findings(report, {**report, 'unconnected_items': [{'items': []}]})

    def test_recommendation_falls_back_to_minimum_before_deferring_only_conflicting_tracks(self):
        from pcb.width_repair import reduce_conflicts
        targets = {'wide': 1000000, 'safe': 500000}
        minima = {'wide': 600000, 'safe': 500000}
        conflict = {'items': [{'uuid': 'wide'}, {'uuid': 'pad'}], 'type': 'clearance'}
        self.assertEqual(reduce_conflicts(targets, minima, [conflict]), ({'wide': 600000, 'safe': 500000}, set()))
        self.assertEqual(reduce_conflicts(minima, minima, [conflict]), ({'safe': 500000}, {'wide'}))
        with self.assertRaisesRegex(RuntimeError, 'unattributed'):
            reduce_conflicts(targets, minima, [{'items': [{'uuid': 'other'}]}])


if __name__ == '__main__':
    unittest.main()
