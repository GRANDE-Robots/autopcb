"""test_factory_scope.py: tests of the required-system partition and native pad safety.

Required-system partition, exact purchasing identity and native pad safety.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import unittest

from core.factory_scope import FactoryScope


class ScopeFixtures:
    """Builds the BOM rows and the assembly policy the scope tests use."""

    @staticmethod
    def part(ref, **overrides):
        """Build one BOM row.

        Inputs:
            ref: Designator.
            overrides: Row fields that replace the defaults.
        Outputs:
            A row with quantity 1, footprint alias_a, manufacturer Exact Maker, MPN
            EXACT and LCSC part C1 unless overridden.
        """
        result = dict(
            Designator=ref,
            Comment="function",
            Footprint="alias_a",
            Manufacturer="Exact Maker",
            MPN="EXACT",
            Quantity=1,
            **{"LCSC Part #": "C1"},
        )
        result.update(overrides)
        return result

    @staticmethod
    def policy():
        """Return an assembly policy that requires every system part.

        Outputs:
            A policy whose default stage is factory assembly by JLCPCB and whose only
            override makes U3 an owner-supplied, owner-installed part.
        """
        return {
            "schema": 1,
            "all_system_parts_required": True,
            "default": {
                "required": True,
                "supply_owner": "JLCPCB",
                "assembly_stage": "FACTORY_ASSEMBLY",
            },
            "overrides": {
                "U3": {
                    "required": True,
                    "supply_owner": "OWNER",
                    "assembly_stage": "OWNER_INSTALL",
                    "expected_manufacturer": "Exact Maker",
                    "expected_mpn": "EXACT",
                    "required_finishing": "Plug the prepared module into fitted sockets.",
                    "acceptance_status": "Required; physical fit unconfirmed.",
                }
            },
        }


class FactoryScopeTests(unittest.TestCase):
    """Tests that the factory scope partitions the required system and consolidates only
    exactly equivalent parts.
    """

    def test_required_owner_part_retained_and_new_factory_part_included(self):
        factory, owner, audit = FactoryScope.partition(
            [
                ScopeFixtures.part("R1"),
                ScopeFixtures.part("U3"),
                ScopeFixtures.part("U99"),
            ],
            ScopeFixtures.policy(),
            ["U3"],
        )
        self.assertEqual({p["Designator"] for p in factory}, {"R1", "U99"})
        self.assertEqual([p["Designator"] for p in owner], ["U3"])
        self.assertTrue(all(p["Required"] and not p["Native_DNP"] for p in audit))

    def test_native_dnp_cannot_be_used_as_owner_scope(self):
        with self.assertRaisesRegex(RuntimeError, "DNP"):
            FactoryScope.partition(
                [ScopeFixtures.part("U3", DNP=True)], ScopeFixtures.policy(), ["U3"]
            )

    def test_native_policy_conflict_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "conflicts"):
            FactoryScope.partition(
                [ScopeFixtures.part("U3")],
                ScopeFixtures.policy(),
                ["U3"],
                {"U3": {"Assembly_Stage": "FACTORY_ASSEMBLY"}},
            )

    def test_owner_policy_cannot_silently_follow_changed_mpn(self):
        with self.assertRaisesRegex(RuntimeError, "different MPN"):
            FactoryScope.partition(
                [ScopeFixtures.part("U3", MPN="DIFFERENT")],
                ScopeFixtures.policy(),
                ["U3"],
            )

    def test_disappeared_policy_ref_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "disappeared"):
            FactoryScope.partition([ScopeFixtures.part("R1")], ScopeFixtures.policy())

    def test_new_module_requires_explicit_policy(self):
        with self.assertRaisesRegex(RuntimeError, "explicit assembly policy"):
            FactoryScope.partition(
                [ScopeFixtures.part("U3"), ScopeFixtures.part("U99")],
                ScopeFixtures.policy(),
                ["U3", "U99"],
            )

    def test_duplicate_system_reference_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Duplicate"):
            FactoryScope.partition(
                [ScopeFixtures.part("U3"), ScopeFixtures.part("U3")],
                ScopeFixtures.policy(),
            )

    def test_comment_and_footprint_alias_do_not_split_exact_equivalent_parts(self):
        parts = [
            ScopeFixtures.part("R1"),
            ScopeFixtures.part("R2", Comment="different function", Footprint="alias_b"),
        ]
        rows, evidence, splits = FactoryScope.consolidate(
            parts, {"R1": "geometry", "R2": "geometry"}
        )
        self.assertEqual(
            (len(rows), rows[0]["Quantity"], rows[0]["Designator"]), (1, 2, "R1,R2")
        )
        self.assertEqual(
            evidence[0]["Equivalent_footprint_aliases"], ["alias_a", "alias_b"]
        )
        self.assertEqual(splits, [])

    def test_distinct_exact_identities_are_never_combined(self):
        for field, value in [
            ("MPN", "OTHER"),
            ("Manufacturer", "Other Maker"),
            ("LCSC Part #", "C2"),
        ]:
            with self.subTest(field=field):
                rows, _, _ = FactoryScope.consolidate(
                    [
                        ScopeFixtures.part("R1"),
                        ScopeFixtures.part("R2", **{field: value}),
                    ],
                    {"R1": "same", "R2": "same"},
                )
                self.assertEqual(len(rows), 2)

    def test_same_mpn_different_pad_geometry_stays_separate(self):
        rows, _, splits = FactoryScope.consolidate(
            [ScopeFixtures.part("R1"), ScopeFixtures.part("R2")],
            {"R1": "padA", "R2": "padB"},
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(splits[0]["Geometry_groups"], 2)

    def test_absent_native_geometry_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Missing native footprint"):
            FactoryScope.consolidate([ScopeFixtures.part("R1")], {})

    def test_duplicate_factory_reference_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Duplicate factory"):
            FactoryScope.consolidate(
                [ScopeFixtures.part("R1"), ScopeFixtures.part("R1")], {"R1": "same"}
            )


class NativeGeometryTests(unittest.TestCase):
    """Tests of the native pad geometry key and the native connection requirements."""

    def footprint(self):
        """Build a board with one footprint that has two SMD pads.

        Outputs:
            The board and the footprint; pads 1 and 2 sit at x = -0.775 mm and x = 0.775
            mm.
        """
        import pcbnew

        board = pcbnew.BOARD()
        fp = pcbnew.FOOTPRINT(board)
        board.Add(fp)
        for number, x in [("1", -775000), ("2", 775000)]:
            pad = pcbnew.PAD(fp)
            pad.SetNumber(number)
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetShape(pcbnew.PAD_SHAPE_ROUNDRECT)
            pad.SetSize(pcbnew.VECTOR2I(900000, 950000))
            pad.SetPosition(pcbnew.VECTOR2I(x, 0))
            layers = pcbnew.LSET()
            for layer in (pcbnew.F_Cu, pcbnew.F_Paste, pcbnew.F_Mask):
                layers.AddLayer(layer)
            pad.SetLayerSet(layers)
            fp.Add(pad)
        return board, fp

    def test_rotation_position_and_bottom_side_do_not_change_local_landing_pattern(
        self,
    ):
        import pcbnew

        board, fp = self.footprint()
        expected = FactoryScope.geometry(fp)
        fp.SetPosition(pcbnew.VECTOR2I(21000000, 32000000))
        fp.SetOrientationDegrees(90)
        self.assertEqual(FactoryScope.geometry(fp), expected)
        fp.Flip(fp.GetPosition(), False)
        self.assertEqual(FactoryScope.geometry(fp), expected)

    def test_pad_number_size_and_mask_difference_prevent_false_equivalence(self):
        board, fp = self.footprint()
        expected = FactoryScope.geometry(fp)
        for alteration in ("number", "mask", "size"):
            import pcbnew

            modified = pcbnew.FOOTPRINT(fp)
            pad = list(modified.Pads())[0]
            if alteration == "number":
                pad.SetNumber("9")
            if alteration == "mask":
                pad.SetLocalSolderMaskMargin(20000)
            if alteration == "size":
                pad.SetSize(pcbnew.VECTOR2I(1000000, 1000000))
            self.assertNotEqual(FactoryScope.geometry(modified), expected)

    def test_owner_stock_bridge_policy_rejects_live_host_vbus_at_ca1(self):
        import pcbnew

        board, fp = self.footprint()
        net = pcbnew.NETINFO_ITEM(board, "HOST_VBUS", 1)
        board.Add(net)
        next(p for p in fp.Pads() if p.GetNumber() == "1").SetNet(net)
        rule = {
            "native_connection_requirements": [
                {"connector": "CA1", "cavity": "1", "condition": "UNCONNECTED"}
            ]
        }
        with self.assertRaisesRegex(RuntimeError, "connection failed"):
            FactoryScope.validate_connections(rule, {"CA1": fp})

    def test_owner_stock_bridge_policy_accepts_unconnected_host_vbus(self):
        board, fp = self.footprint()
        rule = {
            "native_connection_requirements": [
                {"connector": "CA1", "cavity": "1", "condition": "UNCONNECTED"}
            ]
        }
        self.assertTrue(
            FactoryScope.validate_connections(rule, {"CA1": fp})[0]["passed"]
        )


if __name__ == "__main__":
    unittest.main()
