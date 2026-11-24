"""Native objects and failed workers must not outlive a routing transaction."""

import multiprocessing
import os
import time
import unittest
from pathlib import Path
import subprocess
import sys

from pcb.net_routing.isolation import run_native
from pcb.net_routing.checkpoint import UnsupportedRoutingRequest
from core.native_command import NativeResourceError


def _announce(*, progress):
    progress({"event": "stage", "message": "native check"})
    return {"pid": os.getpid(), "payload": "x" * 100000}


def _bad_input():
    raise ValueError("invalid native input")


def _unsupported_repair():
    raise UnsupportedRoutingRequest("repair needs more fragments")


def _crash():
    os._exit(7)


def _exhaust_memory():
    raise NativeResourceError("Native check exhausted the budget")


def _wait_for_cancel(*, progress):
    progress({"event": "stage", "message": "waiting"})
    time.sleep(30)


class NativeIsolationTests(unittest.TestCase):
    def test_large_operation_does_not_replay_an_unimportable_parent_entrypoint(self):
        script = (
            "import sys; from types import SimpleNamespace; "
            "from pcb.net_routing.isolation import run_native; "
            "sys.modules['__main__'].__spec__=SimpleNamespace(name='missing_parent_entrypoint'); "
            "print(run_native(len, b'x' * 1048576))"
        )
        try:
            result = subprocess.run(
                [sys.executable, "-B", "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired:
            self.fail(
                "Native worker startup blocked while sending a large operation to a failed bootstrap"
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1048576")

    def test_events_and_large_results_return_after_worker_exit(self):
        events = []
        result = run_native(_announce, progress=events.append, forward_progress=True)
        self.assertNotEqual(result["pid"], os.getpid())
        self.assertEqual(len(result["payload"]), 100000)
        self.assertEqual(events, [{"event": "stage", "message": "native check"}])
        self.assertNotIn(
            result["pid"], {p.pid for p in multiprocessing.active_children()}
        )

    def test_expected_errors_remain_errors(self):
        with self.assertRaisesRegex(ValueError, "invalid native input"):
            run_native(_bad_input)

    def test_unsupported_repair_remains_an_error_across_the_worker_boundary(self):
        with self.assertRaisesRegex(UnsupportedRoutingRequest, "more fragments"):
            run_native(_unsupported_repair)

    def test_native_crash_cannot_be_accepted_as_a_result(self):
        with self.assertRaisesRegex(RuntimeError, "stopped unexpectedly.*exit 7"):
            run_native(_crash)

    def test_resource_failure_type_survives_process_isolation(self):
        with self.assertRaises(NativeResourceError):
            run_native(_exhaust_memory)

    def test_interruption_reaps_the_owned_worker(self):
        before = {p.pid for p in multiprocessing.active_children()}

        def interrupt(_):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            run_native(_wait_for_cancel, progress=interrupt, forward_progress=True)
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)
