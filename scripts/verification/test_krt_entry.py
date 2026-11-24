"""Verify private KRT coordinate transforms do not copy stale native caches."""

import copy
from types import SimpleNamespace
import unittest

from pcb.net_routing.krt_entry import KrtEntry


class Uncopyable:
    def __deepcopy__(self, memo):
        raise TypeError("native obstacle maps cannot be copied")


class KrtEntryTests(unittest.TestCase):
    def test_transform_discards_only_the_stale_cache_on_a_private_shallow_copy(self):
        native = Uncopyable()
        original = SimpleNamespace(geometry=[[1, 2]], _rescue_map_cache={"old": native})
        calls = []

        def transform(board, reference, *, axis=None):
            calls.append((board, reference, axis))
            self.assertFalse(hasattr(board, "_rescue_map_cache"))
            return copy.deepcopy(board)

        result = KrtEntry.uncached_transform(transform)(original, "U7", axis=4)
        self.assertEqual(calls[0][1:], ("U7", 4))
        self.assertIs(original._rescue_map_cache["old"], native)
        result.geometry[0].append(3)
        self.assertEqual(original.geometry, [[1, 2]])

    def test_transform_without_cache_does_not_copy_or_modify_input(self):
        original = SimpleNamespace(geometry=[1])
        self.assertIs(
            KrtEntry.uncached_transform(lambda board: board)(original), original
        )

    def test_transform_wrapper_is_idempotent(self):
        wrapped = KrtEntry.uncached_transform(lambda board: board)
        self.assertIs(KrtEntry.uncached_transform(wrapped), wrapped)


if __name__ == "__main__":
    unittest.main()
