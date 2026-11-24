"""test_safe_zone_fill.py: tests of the safe zone fill read-back.

Focused regression tests use fakes; no live assertion-prone probe is called.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import unittest

from core.safe_zone_fill import SafeZoneFill


class Polys:
    """Fake polygon set that reports a fixed outline count."""

    def __init__(self, count):
        """Store the outline count.

        Inputs:
            count: Number of outlines to report.
        """
        self.count = count

    def OutlineCount(self):
        """Return the stored outline count."""
        return self.count


class FakeZone:
    """Fake zone that exposes its filled polygons only when a fill is present."""

    def __init__(self, present=False, count=1):
        """Set the fill state and start recording accessor calls.

        Inputs:
            present: Whether the zone reports a fill for any layer.
            count: Outline count of the fill.
        """
        self.present, self.count, self.accessor_calls = present, count, []

    def GetLayer(self):
        """Return the layer id 4."""
        return 4

    def GetLayerSet(self):
        """Return the layer ids 0, 2, 4, 6 and 10 that the zone declares."""
        return {0, 2, 4, 6, 10}

    def HasFilledPolysForLayer(self, layer):
        """Return whether the zone reports a fill; the layer is ignored."""
        return self.present

    def GetFilledPolysList(self, layer):
        """Record the layer and return the fill polygons.

        Outputs:
            A polygon set with the configured outline count. Raises AssertionError when
            no fill is present, because the accessor must never be called then.
        """
        self.accessor_calls.append(layer)
        if not self.present:
            raise AssertionError("Unsafe accessor must never be called")
        return Polys(self.count)


class SafeReadbackTests(unittest.TestCase):
    """Tests that the zone fill read-back never calls an unsafe accessor."""

    def test_declared_missing_layer_is_unknown_without_accessor(self):
        z = FakeZone(False)
        record, polys = SafeZoneFill.filled_polys(z, z.GetLayer())
        self.assertEqual(record["status"], "UNKNOWN")
        self.assertIsNone(record["polygon_count"])
        self.assertEqual(z.accessor_calls, [])
        self.assertIsNone(polys)

    def test_valid_filled_layer_is_read(self):
        z = FakeZone(True, 3)
        record, _ = SafeZoneFill.filled_polys(z, 4)
        self.assertEqual(record["status"], "AVAILABLE_NONEMPTY")
        self.assertEqual(record["polygon_count"], 3)
        self.assertEqual(z.accessor_calls, [4])

    def test_available_empty_is_distinct_from_unknown_or_plane_pass(self):
        z = FakeZone(True, 0)
        record, _ = SafeZoneFill.filled_polys(z, 4)
        self.assertEqual(record["status"], "AVAILABLE_EMPTY")
        self.assertEqual(record["polygon_count"], 0)

    def test_absent_predicate_does_not_probe_accessor(self):
        class Unexposed:
            """Zone without a fill predicate whose accessor must not be probed."""

            def GetFilledPolysList(self, layer):
                """Raise AssertionError: the accessor must not be probed."""
                raise AssertionError("Do not probe")

        record, _ = SafeZoneFill.filled_polys(Unexposed(), 4)
        self.assertEqual(record["status"], "UNKNOWN")
        self.assertIsNone(record["polygon_count"])

    def test_predicate_exception_does_not_probe_accessor(self):
        class BrokenPredicate(FakeZone):
            """Zone whose fill predicate raises RuntimeError."""

            def HasFilledPolysForLayer(self, layer):
                """Raise RuntimeError that says there is no map."""
                raise RuntimeError("No map")

        z = BrokenPredicate(True)
        record, _ = SafeZoneFill.filled_polys(z, 4)
        self.assertEqual(record["status"], "UNKNOWN")
        self.assertEqual(z.accessor_calls, [])

    def test_guarded_accessor_exception_is_unknown(self):
        class BrokenAccessor(FakeZone):
            """Zone whose fill accessor raises RuntimeError."""

            def GetFilledPolysList(self, layer):
                """Raise RuntimeError that says the accessor is unavailable."""
                raise RuntimeError("Unavailable")

        record, _ = SafeZoneFill.filled_polys(BrokenAccessor(True), 4)
        self.assertEqual(record["status"], "UNKNOWN")
        self.assertIsNone(record["polygon_count"])

    def test_native_source_missing_fill_is_unknown(self):
        text = (
            '(kicad_pcb\n\t(zone\n\t\t(net "GND")\n\t\t(layers "F.Cu" '
            '"In1.Cu")\n\t\t(uuid "a")\n\t)\n)'
        )
        rows = SafeZoneFill.native_records(text)
        self.assertEqual(rows[0]["source_status"], "UNKNOWN_NO_SAVED_FILL_RECORDS")
        self.assertEqual(rows[0]["stored_polygon_counts"], {})

    def test_native_source_records_only_actual_filled_layers(self):
        text = (
            '(kicad_pcb\n\t(zone\n\t\t(net "GND")\n\t\t(layers "F.Cu" '
            '"In1.Cu")\n\t\t(uuid "a")\n\t\t(filled_polygon (layer '
            '"In1.Cu") (pts (xy 1 1) (xy 2 1) (xy 1 2)))\n\t)\n)'
        )
        rows = SafeZoneFill.native_records(text)
        self.assertEqual(rows[0]["stored_polygon_counts"], {"In1.Cu": 1})
        self.assertNotIn("F.Cu", rows[0]["stored_polygon_counts"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
