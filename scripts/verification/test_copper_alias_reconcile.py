"""test_copper_alias_reconcile.py: tests of the copper net-name alias repair.

A net-name repair may not quietly change geometry or other nets.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import unittest

from pcb.net_routing.alias_reconcile import AliasGroup, AliasReconciliation


class CopperAliasTests(unittest.TestCase):
    """Tests that only complete, proven net aliases are retagged."""

    def test_only_complete_proven_aliases_are_retagged(self):
        original = (
            b'(kicad_pcb (footprint (pad "1" (net "Net-(R1-Pad1)"))) '
            b'(segment (net "Net-(R1-Pad1)")) (pad "1" (net "TARGET")))'
        )
        group = AliasGroup("Net-(R1-Pad1)", "TARGET", ("pad-1",), ("track-1",))
        updated = AliasReconciliation.replace_aliases(original, (group,))
        self.assertEqual(updated.count(b'(net "TARGET")'), 3)
        self.assertEqual(
            updated, original.replace(b'(net "Net-(R1-Pad1)")', b'(net "TARGET")')
        )
        self.assertIn(b"Net-(R1-Pad1)", original)

    def test_incomplete_or_ambiguous_alias_evidence_fails_without_a_write(self):
        data = b'(kicad_pcb (pad (net "Net-(R1-Pad1)")))'
        group = AliasGroup("Net-(R1-Pad1)", "TARGET", ("pad",), ("track",))
        with self.assertRaisesRegex(ValueError, "complete"):
            AliasReconciliation.replace_aliases(data, (group,))
        with self.assertRaises(ValueError):
            AliasReconciliation.replace_aliases(
                data, (AliasGroup("OTHER", "TARGET", ("pad",), ()),)
            )
        with self.assertRaises(ValueError):
            AliasReconciliation.replace_aliases(
                data, (AliasGroup("Net-(R1-Pad1)", "Net-(R2-Pad1)", ("pad",), ()),)
            )
        with self.assertRaises(ValueError):
            AliasReconciliation.replace_aliases(
                data,
                (group, AliasGroup("Net-(R2-Pad1)", "TARGET", ("pad",), ("track-2",))),
            )

    def test_normalized_alias_retains_evidence_for_the_unchanged_neighbor_pad(self):
        from pcb.route_validation import stable_connection_items

        data = (
            b'(kicad_pcb (footprint "x" (layer "B.Cu") (uuid "fp") (at 1 2) '
            b'(pad "1" smd rect (net "POWER") (uuid "power-pad")) '
            b'(pad "2" smd rect (net "Net-(R1-Pad2)") (uuid "alias-pad"))) '
            b'(segment (start 0 0) (end 1 0) (width .2) (layer "B.Cu") '
            b'(net "Net-(R1-Pad2)") (uuid "track")))'
        )
        normalized = AliasReconciliation.replace_aliases(
            data, (AliasGroup("Net-(R1-Pad2)", "CONTROL", ("alias-pad",), ("track",)),)
        )
        self.assertNotIn("power-pad", stable_connection_items(data, normalized))
        self.assertEqual(
            stable_connection_items(normalized, normalized).get("power-pad"), "POWER"
        )


if __name__ == "__main__":
    unittest.main()
