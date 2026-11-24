"""test_pcb_paths.py: tests of the route audit path specifications.

The route audit checks current net endpoints, including the USB switch.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import unittest

from verification.audit_pcb_paths import path_specs, validate_path_specs


class PcbPathSpecificationTests(unittest.TestCase):
    """Tests of the built-in path specifications and their endpoint validation."""

    def test_usb_paths_stop_on_each_side_of_the_switch(self):
        usb = [spec for spec in path_specs() if spec[0].startswith("TEENSY_")]
        self.assertEqual(
            set(usb),
            {
                ("TEENSY_HOST_USB_DP", ("J28", "A6"), ("U28", "8")),
                ("TEENSY_HOST_USB_DM", ("J28", "A7"), ("U28", "7")),
                ("TEENSY_HOST_USB_DP", ("J28", "B6"), ("U28", "8")),
                ("TEENSY_HOST_USB_DM", ("J28", "B7"), ("U28", "7")),
                ("TEENSY_USB_DP", ("U28", "1"), ("CA1", "3")),
                ("TEENSY_USB_DM", ("U28", "2"), ("CA1", "2")),
            },
        )
        self.assertEqual(len(usb), 6)

    def test_audit_rejects_stale_missing_or_duplicate_endpoints(self):
        paths = [("SIGNAL", ("J1", "1"), ("U1", "2"))]
        pad_nets = {("J1", "1"): "SIGNAL", ("U1", "2"): "SIGNAL"}
        validate_path_specs(paths, pad_nets)
        with self.assertRaisesRegex(ValueError, "expected"):
            validate_path_specs(paths, {("J1", "1"): "OTHER", ("U1", "2"): "SIGNAL"})
        with self.assertRaisesRegex(ValueError, "expected"):
            validate_path_specs(paths, {("J1", "1"): "SIGNAL"})
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            validate_path_specs(paths + paths, pad_nets)


if __name__ == "__main__":
    unittest.main()
