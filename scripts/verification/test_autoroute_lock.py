"""Concurrent preview bookkeeping waits without weakening the write lock."""

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pcb.net_routing.runner import _acquire_lock, _exclusive


class LockWaitTests(unittest.TestCase):
    def test_retries_a_busy_lock_then_succeeds(self):
        now = [0.0]
        attempts = []
        messages = []

        def acquire():
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError("busy")

        def advance(seconds):
            now[0] += seconds

        with (
            patch("pcb.net_routing.runner.time.monotonic", side_effect=lambda: now[0]),
            patch("pcb.net_routing.runner.time.sleep", side_effect=advance),
        ):
            _acquire_lock(acquire, wait_seconds=1.0, progress=messages.append)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(messages), 1)

    def test_bounded_wait_fails_if_the_lock_stays_busy(self):
        now = [0.0]

        def busy():
            raise OSError("busy")

        with (
            patch("pcb.net_routing.runner.time.monotonic", side_effect=lambda: now[0]),
            patch(
                "pcb.net_routing.runner.time.sleep",
                side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "Another AutoPCB run is active"):
                _acquire_lock(busy, wait_seconds=0.2)

    def test_invalid_wait_is_rejected(self):
        for value in (-1, float("inf"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _acquire_lock(lambda: None, wait_seconds=value)

    def test_independent_processes_take_turns_on_the_real_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "active.lock"
            ready = Path(temporary) / "ready"
            code = (
                "from pathlib import Path; import sys,time; "
                "from pcb.net_routing.runner import _exclusive; "
                "lock=_exclusive(Path(sys.argv[1])); lock.__enter__(); "
                "Path(sys.argv[2]).write_text('ready'); time.sleep(1); lock.__exit__(None,None,None)"
            )
            child = subprocess.Popen(
                [sys.executable, "-B", "-c", code, str(lock), str(ready)]
            )
            try:
                deadline = time.monotonic() + 8
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready.exists(), "Lock-holder process did not start")
                start = time.monotonic()
                with _exclusive(lock, wait_seconds=4):
                    self.assertGreater(time.monotonic() - start, 0.1)
                self.assertEqual(child.wait(timeout=4), 0)
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=4)


if __name__ == "__main__":
    unittest.main()
