"""Endpoint cleanup preserves exact routing headings and electrical connectivity."""

import unittest


class DrcCleanupTests(unittest.TestCase):
    def test_misaligned_via_and_three_track_ends_share_an_exact_axis_intersection(self):
        from pcb.drc_cleanup import center_solution

        tracks = [
            ("a", (110110000, 100640000), (110110000, 98960000)),
            ("b", (110110000, 98960000), (110110000, 96425000)),
            ("c", (110120000, 98850000), (76820000, 98850000)),
        ]
        point, endpoints = center_solution((110120000, 98850000), tracks, 300000)
        self.assertEqual(point, (110110000, 98850000))
        self.assertEqual(endpoints, {"a": "end", "b": "start", "c": "start"})

    def test_short_axis_extension_centers_on_an_existing_diagonal_via(self):
        from pcb.drc_cleanup import center_solution

        point, endpoints = center_solution(
            (26870000, 46020000),
            [
                ("a", (28780000, 47930000), (26870000, 46020000)),
                ("b", (30590000, 46020000), (26910000, 46020000)),
            ],
            300000,
        )
        self.assertEqual(point, (26870000, 46020000))
        self.assertEqual(endpoints["b"], "end")

    def test_nonintersecting_parallel_lines_require_rerouting_instead_of_an_off_angle_snap(
        self,
    ):
        from pcb.drc_cleanup import center_solution

        self.assertIsNone(
            center_solution(
                (1000000, 1000000),
                [
                    ("a", (0, 1000000), (1000000, 1000000)),
                    ("b", (1000000, 1100000), (2000000, 1100000)),
                ],
                300000,
            )
        )


if __name__ == "__main__":
    unittest.main()
