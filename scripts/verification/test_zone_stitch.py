"""Geometry and clearance checks for automatic zone access."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pcbnew

from pcb.checkpoints import read_config
from pcb.net_routing.electrical import Branch, Evidence, Quantity, Scenario
from pcb.net_routing.runner import _source_hashes
from pcb.net_routing import zone_stitch
from pcb.net_routing.zone_stitch import (
    StitchDimensions,
    StitchProof,
    candidate_paths,
    candidate_sites,
    StitchAllowance,
    _island_matches,
    verified_pad_limits,
    via_clear_of_planned,
)


class ZoneStitchGeometryTests(unittest.TestCase):
    def test_native_shapes_accept_exact_clearance_without_changing_the_rules(self):
        from pcb.freeroute_delta import (
            Trace,
            Via,
            native_clearance_checker,
            native_shortcut_obstacles,
            shortcut_clearance_checker,
        )
        from pcb.route_optimizer import mm

        board = pcbnew.BOARD()
        board.SetCopperLayerCount(6)
        for name in ("GND", "OTHER"):
            board.Add(pcbnew.NETINFO_ITEM(board, name))
        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference("U1")
        pad = pcbnew.PAD(fp)
        pad.SetNumber("1")
        pad.SetShape(pcbnew.PAD_SHAPE_RECT)
        pad.SetSize(pcbnew.VECTOR2I(mm(0.4), mm(4)))
        pad.SetPosition(pcbnew.VECTOR2I(mm(10.65), mm(10)))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(pcbnew.LSET.FrontMask())
        pad.SetNetCode(board.FindNet("OTHER").GetNetCode())
        fp.Add(pad)
        board.Add(fp)
        profile = SimpleNamespace(
            maximum_clearance=mm(0.2), clearance_at=lambda *args, **kw: mm(0.2)
        )
        trace = Trace("GND", "F.Cu", mm(0.5), (mm(10), mm(10)), (mm(10), mm(11)))
        via = Via("GND", (mm(10), mm(10)), mm(0.5), mm(0.3))
        # Integer bounding envelopes include a safety inflation. The actual
        # native shapes have exactly the required 0.2 mm physical gap.
        self.assertFalse(
            shortcut_clearance_checker(
                native_shortcut_obstacles(board), profile_for=lambda _: profile
            )((trace,))
        )
        check = native_clearance_checker(
            board, clearance=mm(0.2), profile_for=lambda _: profile
        )
        self.assertTrue(check((trace,)))
        self.assertTrue(check((via,)))
        inner = Trace("GND", "In3.Cu", mm(0.5), trace.start, trace.end)
        self.assertTrue(check((inner,)))
        pad.SetPosition(pcbnew.VECTOR2I(mm(10.64), mm(10)))
        changed = native_clearance_checker(
            board, clearance=mm(0.2), profile_for=lambda _: profile
        )
        self.assertFalse(changed((trace,)))
        self.assertFalse(changed((via,)))
        self.assertTrue(changed((inner,)))
        self.assertEqual(len(list(board.GetTracks())), 0)

    def test_native_join_retains_connectivity_gain_and_removes_useless_copper(self):
        from pcb.route_optimizer import mm

        board = pcbnew.BOARD()
        board.SetCopperLayerCount(6)
        net = pcbnew.NETINFO_ITEM(board, "GND")
        board.Add(net)
        for reference, x in (("A", 2), ("B", 5)):
            footprint = pcbnew.FOOTPRINT(board)
            footprint.SetReference(reference)
            pad = pcbnew.PAD(footprint)
            pad.SetNumber("1")
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pcbnew.LSET.FrontMask())
            pad.SetSize(pcbnew.VECTOR2I(mm(0.6), mm(0.6)))
            pad.SetPosition(pcbnew.VECTOR2I(mm(x), mm(2)))
            pad.SetNetCode(net.GetNetCode())
            footprint.Add(pad)
            board.Add(footprint)
        useless = zone_stitch._new_via(
            board,
            net,
            (mm(10), mm(10)),
            StitchDimensions(200000, 200000, 600000, 300000, 250000),
        )
        self.assertFalse(zone_stitch._join_if_improved(board, [useless]))
        self.assertEqual(len(list(board.GetTracks())), 0)
        trace = zone_stitch.Trace(
            "GND", "F.Cu", mm(0.2), (mm(2), mm(2)), (mm(5), mm(2))
        )
        self.assertTrue(
            zone_stitch._join_if_improved(
                board, zone_stitch._native_tracks(board, net, (trace,))
            )
        )
        self.assertEqual(board.GetConnectivity().GetUnconnectedCount(False), 0)

    def test_zone_to_zone_drc_also_searches_the_small_pad_islands(self):
        board, pad, rules = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
        pad.m_Uuid.AsString.return_value = "pad-a"
        pad.GetNetname.return_value = "GND"
        pad.GetAttribute.return_value = pcbnew.PAD_ATTRIB_SMD
        board.GetPads.return_value = [pad]
        board.GetNetsByName.return_value = {"GND": 1}
        board.Zones.return_value = []
        inventory = SimpleNamespace(
            nets=(
                SimpleNamespace(
                    name="GND",
                    zone_ids=("zone",),
                    copper_islands=(("main-pad", "main-track"), ("pad-a",)),
                ),
            )
        )
        allowance = StitchAllowance(2400000, frozenset({"pad-a"}))
        with (
            mock.patch.object(
                zone_stitch,
                "_dimensions",
                return_value=StitchDimensions(500000, 200000, 800000, 400000, 250000),
            ),
            mock.patch.object(zone_stitch, "inventory_board", return_value=inventory),
            mock.patch.object(zone_stitch, "high_speed_nets", return_value={"USB_DP"}),
            mock.patch.object(
                zone_stitch,
                "reference_corridors",
                return_value=SimpleNamespace(unsupported_items=()),
            ),
            mock.patch.object(
                zone_stitch, "native_shortcut_obstacles", return_value=()
            ),
        ):
            result = zone_stitch.propose(
                board,
                {"unconnected_items": [{"items": [{"uuid": "zone"}]}]},
                b"{}",
                rules,
                eligible={"pad-a": allowance},
            )
        self.assertEqual(result["open_pads"], 1)

    def test_island_proof_must_cover_all_current_contributors(self):
        allowed = StitchAllowance(2_000_000, frozenset({"pad-a", "pad-b", "track"}))
        self.assertTrue(_island_matches(allowed, ("track", "pad-a", "pad-b")))
        self.assertFalse(_island_matches(allowed, ("pad-a", "track")))
        self.assertFalse(_island_matches(allowed, ("pad-a", "pad-b", "track", "via")))
        self.assertFalse(_island_matches(2_000_000, ("pad-a", "pad-b", "track")))

    def test_proposal_rejects_a_pad_whose_island_has_an_extra_connection(self):
        board, pad, rules = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
        pad.m_Uuid.AsString.return_value = "pad-a"
        pad.GetNetname.return_value = "GND"
        pad.GetAttribute.return_value = pcbnew.PAD_ATTRIB_SMD
        board.GetPads.return_value = [pad]
        board.GetNetsByName.return_value = {"GND": 1}
        board.Zones.return_value = []
        inventory = SimpleNamespace(
            nets=(SimpleNamespace(name="GND", copper_islands=(("pad-a", "pad-b"),)),)
        )
        report = {"unconnected_items": [{"items": [{"uuid": "pad-a"}]}]}
        dimensions = StitchDimensions(500_000, 200_000, 800_000, 400_000, 250_000)
        with (
            mock.patch.object(zone_stitch, "_dimensions", return_value=dimensions),
            mock.patch.object(zone_stitch, "inventory_board", return_value=inventory),
            mock.patch.object(zone_stitch, "high_speed_nets", return_value={"USB_DP"}),
            mock.patch.object(
                zone_stitch,
                "reference_corridors",
                return_value=SimpleNamespace(obstacles=(), unsupported_items=()),
            ),
            mock.patch.object(
                zone_stitch, "native_shortcut_obstacles", return_value=()
            ),
        ):
            result = zone_stitch.propose(
                board,
                report,
                b"{}",
                rules,
                eligible={"pad-a": StitchAllowance(2_000_000, frozenset({"pad-a"}))},
            )
        self.assertEqual(result["proposed_stitches"], 0)
        self.assertEqual(result["unassessed_pads"], 1)
        board.Add.assert_not_called()

    def test_qualified_pad_is_found_when_drc_names_its_track_instead(self):
        board, pad, rules = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
        pad.m_Uuid.AsString.return_value = "pad-a"
        pad.GetNetname.return_value = "GND"
        pad.GetAttribute.return_value = pcbnew.PAD_ATTRIB_SMD
        board.GetPads.return_value = [pad]
        board.GetNetsByName.return_value = {"GND": 1}
        board.Zones.return_value = []
        inventory = SimpleNamespace(
            nets=(SimpleNamespace(name="GND", copper_islands=(("pad-a", "track-a"),)),)
        )
        report = {"unconnected_items": [{"items": [{"uuid": "track-a"}]}]}
        dimensions = StitchDimensions(500_000, 200_000, 800_000, 400_000, 250_000)
        allowance = StitchAllowance(2_000_000, frozenset({"pad-a", "track-a"}))
        with (
            mock.patch.object(zone_stitch, "_dimensions", return_value=dimensions),
            mock.patch.object(zone_stitch, "inventory_board", return_value=inventory),
            mock.patch.object(zone_stitch, "high_speed_nets", return_value={"USB_DP"}),
            mock.patch.object(
                zone_stitch,
                "reference_corridors",
                return_value=SimpleNamespace(obstacles=(), unsupported_items=()),
            ),
            mock.patch.object(
                zone_stitch, "native_shortcut_obstacles", return_value=()
            ),
        ):
            result = zone_stitch.propose(
                board, report, b"{}", rules, eligible={"pad-a": allowance}
            )
            unqualified = zone_stitch.propose(board, report, b"{}", rules, eligible={})
        self.assertEqual(result["open_pads"], 1)
        self.assertEqual(result["unassessed_pads"], 0)
        self.assertEqual(unqualified["open_pads"], 0)
        board.Add.assert_not_called()

    def test_candidate_sites_are_octilinear_and_outside_the_pad(self):
        sites = candidate_sites(
            (1_000_000, 2_000_000), (500_000, 1_500_000, 1_500_000, 2_500_000), 800_000
        )
        self.assertEqual(len(sites), 8)
        for x, y in sites:
            dx, dy = x - 1_000_000, y - 2_000_000
            self.assertTrue(dx == 0 or dy == 0 or abs(dx) == abs(dy))
            self.assertTrue(abs(dx) >= 1_000_000 or abs(dy) >= 1_000_000)

    def test_planned_vias_obey_both_copper_and_drill_spacing(self):
        dimensions = StitchDimensions(500_000, 200_000, 800_000, 400_000, 250_000)
        self.assertFalse(via_clear_of_planned((900_000, 0), [(0, 0)], dimensions))
        self.assertTrue(via_clear_of_planned((1_000_000, 0), [(0, 0)], dimensions))
        drill_limited = StitchDimensions(100_000, 0, 400_000, 300_000, 700_000)
        self.assertFalse(via_clear_of_planned((900_000, 0), [(0, 0)], drill_limited))

    def test_extended_paths_have_only_valid_chamfers_and_reach_beyond_the_pad(self):
        paths = candidate_paths(
            (0, 0),
            (-100_000, -100_000, 100_000, 100_000),
            400_000,
            step=200_000,
            max_distance=1_000_000,
        )
        self.assertTrue(any(len(path) == 3 for path in paths))
        self.assertTrue(
            any(max(abs(v) for v in path[-1]) == 1_000_000 for path in paths)
        )
        for path in paths:
            vectors = [(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:])]
            for dx, dy in vectors:
                self.assertTrue(
                    (dx or dy) and (dx == 0 or dy == 0 or abs(dx) == abs(dy))
                )
            if len(vectors) == 2:
                first, second = vectors
                dot = first[0] * second[0] + first[1] * second[1]
                cross = first[0] * second[1] - first[1] * second[0]
                self.assertGreater(dot, 0)
                self.assertEqual(abs(cross), dot)

    def test_missing_or_inconsistent_rules_fail_closed(self):
        for values in (
            (0, 200_000, 800_000, 400_000, 250_000),
            (500_000, -1, 800_000, 400_000, 250_000),
            (500_000, 200_000, 400_000, 400_000, 250_000),
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                StitchDimensions(*values)

    def test_pad_access_requires_current_voltage_and_fault_evidence(self):
        dimensions = StitchDimensions(500_000, 200_000, 800_000, 400_000, 250_000)
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            data = b"reviewed capacity and load evidence"
            (project / "evidence.txt").write_bytes(data)
            evidence = Evidence(
                "evidence.txt",
                hashlib.sha256(data).hexdigest(),
                "section 2",
                "verified",
            )
            capacity = Quantity(1, "A", evidence)
            resistance = Quantity(0.01, "ohm", evidence)
            load = Quantity(0.1, "A", evidence)
            drop = Quantity(0.1, "V", evidence)
            branches = (
                Branch("track", "pad", "via", "track", capacity, resistance),
                Branch("via", "via", "plane", "via", capacity, resistance),
            )
            scenarios = tuple(
                Scenario(name, (("plane", load),), drop)
                for name in ("normal", "transient", "fault")
            )
            board_hash = hashlib.sha256(b"board source").hexdigest()
            island = ("pad-id", "other-pad-id", "track-id")
            proof = StitchProof(
                "pad-id", dimensions, 4_000_000, branches, scenarios, island, board_hash
            )

            def assess(proofs, source=board_hash):
                return verified_pad_limits(
                    project, proofs, dimensions, board_sha256=source
                )

            self.assertEqual(assess(()), {})
            # A source label and a matching hash do not verify the numbers or
            # show that the cited values apply to the proposed copper.
            self.assertEqual(assess((proof,)), {})
            self.assertEqual(assess((proof, proof)), {})
            self.assertEqual(
                assess((proof,), hashlib.sha256(b"changed board").hexdigest()), {}
            )
            self.assertEqual(
                assess(
                    (StitchProof("pad-id", dimensions, 4_000_000, branches, scenarios),)
                ),
                {},
            )
            bypass = tuple(
                Scenario(name, (("pad", load),), drop)
                for name in ("normal", "transient", "fault")
            )
            bypass_proof = StitchProof(
                "pad-id", dimensions, 4_000_000, branches, bypass, island, board_hash
            )
            self.assertEqual(assess((bypass_proof,)), {})
            (project / "evidence.txt").write_text("changed")
            self.assertEqual(assess((proof,)), {})
            incomplete = StitchProof(
                "pad-id",
                dimensions,
                4_000_000,
                branches,
                scenarios[:1],
                island,
                board_hash,
            )
            self.assertEqual(assess((incomplete,)), {})

    def test_policy_accepts_zone_evidence_and_fingerprints_its_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            assets = project / "autopcb"
            (assets / "config").mkdir(parents=True)
            source = project / "evidence.txt"
            source.write_text("first")
            policy = {
                "schema": 1,
                "max_pins": 3,
                "zone_stitch": {
                    "proofs": [{"capacity": {"evidence": {"source": "evidence.txt"}}}]
                },
            }
            (assets / "config" / "autopcb.json").write_text(json.dumps(policy))
            self.assertIn(
                "zone_stitch", read_config(assets / "config" / "autopcb.json")[0]
            )
            snapshot = SimpleNamespace(fingerprints={})
            before = _source_hashes(snapshot, project, assets)
            source.write_text("changed")
            after = _source_hashes(snapshot, project, assets)
            self.assertIn("evidence.txt", before)
            self.assertNotEqual(before["evidence.txt"], after["evidence.txt"])


if __name__ == "__main__":
    unittest.main()
