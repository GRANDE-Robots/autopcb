"""Reclaim KiCad's native allocations at a bounded process boundary.

KiCad 10 LoadBoard returns non-owning wrappers. Repeated loads accumulate native
boards; forcing their destructors can invalidate other SWIG types. Workers only
handle saved-file snapshots and exit after one operation. No editor is opened.
"""

from __future__ import annotations

import multiprocessing
import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from core.native_command import (
    NativeCommandError,
    NativeResourceError,
    exit_description,
)


_SPAWN_LOCK = threading.RLock()


def _start(process, function) -> None:
    """Pin project imports and avoid replaying an unrelated parent CLI."""
    root = str(Path(__file__).resolve().parents[2])
    main = sys.modules["__main__"]
    missing = object()
    with _SPAWN_LOCK:
        spec, filename = (
            getattr(main, "__spec__", missing),
            getattr(main, "__file__", missing),
        )
        sys.path.insert(0, root)
        try:
            if getattr(function, "__module__", "") != "__main__":
                main.__spec__ = None
                main.__dict__.pop("__file__", None)
            process.start()
        finally:
            sys.path.remove(root)
            for name, value in (("__spec__", spec), ("__file__", filename)):
                if value is missing:
                    main.__dict__.pop(name, None)
                else:
                    setattr(main, name, value)


def _execute(channel, forward_progress: bool) -> None:
    from core.pcb_io import quiet_duplicate_image_handlers

    try:
        # Send no board payload in Process.start(): a child which cannot import
        # its bootstrap can die before draining that pipe and deadlock its
        # parent. recv() drains the framed operation before unpickling it.
        channel.send(("ready", None))
        function, args, kwargs = channel.recv()
        if forward_progress:
            kwargs["progress"] = lambda value: channel.send(("progress", value))
        with quiet_duplicate_image_handlers():
            result = function(*args, **kwargs)
        channel.send(("result", result))
    except BaseException as error:
        channel.send(("error", (type(error).__name__, str(error))))
    finally:
        channel.close()


def _stop(process) -> None:
    """Reap only the worker we created and its headless child checks."""
    if process.is_alive():
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        if process.is_alive():
            process.terminate()
        process.join(timeout=10)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)


def run_native(
    function: Callable,
    *args,
    progress=None,
    forward_progress: bool = False,
    heartbeat=None,
    heartbeat_interval: float = 50.0,
    **kwargs,
):
    """Return plain records only after a fresh native worker exits successfully.

    The Windows routing job limit is inherited by the worker and its children.
    Errors and interruption cannot publish a board or be mistaken for completion.
    """
    if heartbeat is not None and (
        not callable(heartbeat)
        or isinstance(heartbeat_interval, bool)
        or not isinstance(heartbeat_interval, (int, float))
        or not math.isfinite(heartbeat_interval)
        or heartbeat_interval <= 0
    ):
        raise ValueError("A heartbeat needs a callable and positive interval")
    context = multiprocessing.get_context("spawn")
    incoming, outgoing = context.Pipe(duplex=True)
    process = context.Process(
        target=_execute, args=(outgoing, forward_progress), name="autopcb-native-check"
    )
    started = False
    try:
        _start(process, function)
        started = True
        outgoing.close()
        received_result = False
        result = None
        failure = None
        sent = False
        boot_started = time.monotonic()
        last_update = time.monotonic()
        while True:
            if incoming.poll(0.25):
                try:
                    kind, value = incoming.recv()
                except EOFError:
                    break
                if kind == "ready" and not sent:
                    incoming.send((function, args, kwargs))
                    sent = True
                    last_update = time.monotonic()
                elif kind == "progress":
                    if progress:
                        progress(value)
                    last_update = time.monotonic()
                elif kind == "result":
                    received_result, result = True, value
                    break
                elif kind == "error":
                    failure = value
                    break
                else:
                    raise RuntimeError("Native worker returned an invalid message")
            elif not process.is_alive():
                break
            if not sent and time.monotonic() - boot_started > 30:
                raise RuntimeError(
                    "Native worker did not become ready; no operation was sent"
                )
            if heartbeat and time.monotonic() - last_update >= heartbeat_interval:
                heartbeat()
                last_update = time.monotonic()
        process.join(timeout=10)
        if process.is_alive() or process.exitcode != 0:
            detail = (
                "did not exit"
                if process.is_alive()
                else exit_description(process.exitcode)
            )
            raise RuntimeError(
                f"Native worker stopped unexpectedly ({detail}); the saved PCB was not changed"
            )
        if failure:
            name, message = failure
            from pcb.net_routing.checkpoint import UnsupportedRoutingRequest

            error_type = {
                "ValueError": ValueError,
                "OSError": OSError,
                "RuntimeError": RuntimeError,
                "MemoryError": MemoryError,
                "UnsupportedRoutingRequest": UnsupportedRoutingRequest,
                "NativeCommandError": NativeCommandError,
                "NativeResourceError": NativeResourceError,
            }.get(name, RuntimeError)
            raise error_type(message or name)
        if not received_result:
            raise RuntimeError(
                "Native worker returned no result; the saved PCB was not changed"
            )
        return result
    finally:
        incoming.close()
        outgoing.close()
        if started:
            _stop(process)
            process.close()
