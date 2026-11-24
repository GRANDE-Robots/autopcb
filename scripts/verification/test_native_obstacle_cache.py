"""test_native_obstacle_cache.py: tests of native obstacle reuse.

Native obstacle reuse must never cross a changed board or rule set.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcb.route_native import NativeObstacleCache, NativeSnapshot


class ObstacleReaderStub:
    """Stands in for the native obstacle reader."""

    @staticmethod
    def read(path, _net, *, assume_refill=False, removals=None):
        """Return a snapshot that is bound to the hash of the board file.

        Inputs:
            path: Board file to hash.
            _net: Net name; unused.
            assume_refill: Ignored.
            removals: Ignored.
        Outputs:
            A NativeSnapshot with the SHA-256 of the file, no obstacles and no ignored
            zones.
        """
        del assume_refill, removals
        return NativeSnapshot(hashlib.sha256(path.read_bytes()).hexdigest(), (), 0)


class NativeObstacleCacheTests(unittest.TestCase):
    """Tests that cached native obstacles never cross a changed board or rule set."""

    def test_reuse_is_bound_to_contents_net_and_refill_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixture.kicad_pcb"
            rules = source.with_suffix(".kicad_pro")
            source.write_bytes(b"board one")
            rules.write_bytes(b"rules one")
            cache = NativeObstacleCache()
            with patch(
                "pcb.route_native.read_native_obstacles",
                side_effect=ObstacleReaderStub.read,
            ) as read:
                first = cache.load(source, "A", assume_refill=True)
                self.assertIs(cache.load(source, "A", assume_refill=True), first)
                self.assertEqual(read.call_count, 1)
                source.write_bytes(b"board two")
                cache.load(source, "A", assume_refill=True)
                rules.write_bytes(b"rules two")
                cache.load(source, "A", assume_refill=True)
                cache.load(source, "B", assume_refill=True)
                cache.load(source, "B", assume_refill=False)
                self.assertEqual(read.call_count, 5)

    def test_mutation_during_compilation_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixture.kicad_pcb"
            source.write_bytes(b"before")

            def mutate(path, net, **options):
                """Read the snapshot, then rewrite the board file as a user edit would."""
                result = ObstacleReaderStub.read(path, net, **options)
                path.write_bytes(b"after")
                return result

            with patch("pcb.route_native.read_native_obstacles", side_effect=mutate):
                with self.assertRaisesRegex(RuntimeError, "changed"):
                    NativeObstacleCache().load(source, "A")

    def test_explicit_removals_are_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixture.kicad_pcb"
            source.write_bytes(b"board")
            cache = NativeObstacleCache()
            removal = [{"kind": "segment", "uuid": "test"}]
            with patch(
                "pcb.route_native.read_native_obstacles",
                side_effect=ObstacleReaderStub.read,
            ) as read:
                cache.load(source, "A", removals=removal)
                cache.load(source, "A", removals=removal)
                self.assertEqual(read.call_count, 2)

    def test_cached_native_search_matches_uncached_search(self):
        from pcb.route_cli import plan
        from verification.test_route_optimizer import RouteOptimizerTests

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            source = RouteOptimizerTests.make_board(project)
            original = source.read_bytes()
            data = RouteOptimizerTests.config()
            data["board_snapshot"] = "pcb"
            expected = plan(data, project)
            cache = NativeObstacleCache()
            self.assertEqual(plan(data, project, native_cache=cache), expected)
            self.assertEqual(plan(data, project, native_cache=cache), expected)
            self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
