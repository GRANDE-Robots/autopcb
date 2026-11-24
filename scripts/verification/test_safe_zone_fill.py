"""Focused regression tests use fakes; no live assertion-prone probe is called."""

import unittest

from core.safe_zone_fill import SafeZoneFill


class Polys:
    def __init__(self, count):
        self.count = count

    def OutlineCount(self):
        return self.count


class FakeZone:
    def __init__(self, present=False, count=1):
        self.present, self.count, self.accessor_calls = present, count, []

    def GetLayer(self):
        return 4

    def GetLayerSet(self):
        return {0, 2, 4, 6, 10}

    def HasFilledPolysForLayer(self, layer):
        return self.present

    def GetFilledPolysList(self, layer):
        self.accessor_calls.append(layer)
        if not self.present:
            raise AssertionError("Unsafe accessor must never be called")
        return Polys(self.count)


class SafeReadbackTests(unittest.TestCase):
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
            def GetFilledPolysList(self, layer):
                raise AssertionError("Do not probe")

        record, _ = SafeZoneFill.filled_polys(Unexposed(), 4)
        self.assertEqual(record["status"], "UNKNOWN")
        self.assertIsNone(record["polygon_count"])

    def test_predicate_exception_does_not_probe_accessor(self):
        class BrokenPredicate(FakeZone):
            def HasFilledPolysForLayer(self, layer):
                raise RuntimeError("No map")

        z = BrokenPredicate(True)
        record, _ = SafeZoneFill.filled_polys(z, 4)
        self.assertEqual(record["status"], "UNKNOWN")
        self.assertEqual(z.accessor_calls, [])

    def test_guarded_accessor_exception_is_unknown(self):
        class BrokenAccessor(FakeZone):
            def GetFilledPolysList(self, layer):
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
