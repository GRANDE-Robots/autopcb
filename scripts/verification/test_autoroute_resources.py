"""Resource budgets must be explicit, bounded, and usable for native checks."""

import unittest
from unittest import mock

from pcb.net_routing.resources import (
    AvailableMemory,
    GIB,
    limit_resources,
    plan_resources,
)


class ResourcePlanTests(unittest.TestCase):
    def test_automatic_budget_remains_half_ram_capped_at_six(self):
        plan = plan_resources(AvailableMemory(5 * GIB, 20 * GIB))
        self.assertEqual(plan["job_commit_limit_bytes"], int(2.5 * GIB))
        self.assertEqual(plan["budget_mode"], "automatic")
        self.assertEqual(
            plan_resources(AvailableMemory(20 * GIB, 20 * GIB))[
                "job_commit_limit_bytes"
            ],
            6 * GIB,
        )

    def test_low_automatic_budget_does_not_launch_or_silently_increase(self):
        with self.assertRaisesRegex(MemoryError, "1.00 GiB.*below"):
            plan_resources(AvailableMemory(2 * GIB, 10 * GIB))

    def test_explicit_budget_can_use_available_commit_without_disabling_the_cap(self):
        plan = plan_resources(AvailableMemory(2 * GIB, 10 * GIB), memory_gib=3)
        self.assertEqual(plan["job_commit_limit_bytes"], 3 * GIB)
        self.assertEqual(plan["budget_mode"], "explicit")

    def test_commit_headroom_and_native_launch_minimum_are_required(self):
        with self.assertRaisesRegex(MemoryError, "commit capacity"):
            plan_resources(AvailableMemory(8 * GIB, 3 * GIB), memory_gib=3)
        with self.assertRaisesRegex(MemoryError, "below"):
            plan_resources(AvailableMemory(8 * GIB, 10 * GIB), memory_gib=1)

    def test_invalid_explicit_budgets_are_not_unbounded(self):
        for value in (True, "3", 0, -1, 7, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                plan_resources(AvailableMemory(8 * GIB, 20 * GIB), memory_gib=value)

    def test_memory_wait_is_bounded_and_never_starts_an_undersized_job(self):
        events = []
        with mock.patch(
            "pcb.net_routing.resources.available_memory",
            return_value=AvailableMemory(2 * GIB, 10 * GIB),
        ):
            with (
                self.assertRaises(MemoryError),
                limit_resources(wait_seconds=0.01, progress=events.append),
            ):
                self.fail("An undersized job was started")
        self.assertTrue(events)

    def test_invalid_wait_cannot_be_an_infinite_loop(self):
        for value in (True, -1, float("inf"), float("nan")):
            with (
                self.subTest(value=value),
                self.assertRaises(ValueError),
                limit_resources(wait_seconds=value),
            ):
                self.fail("An invalid wait started a job")
