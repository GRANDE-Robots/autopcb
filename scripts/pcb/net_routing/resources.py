"""Guard worker memory using a Windows job or a sampled POSIX process tree."""

from __future__ import annotations

import ctypes
import math
import mmap
import os
import subprocess
import threading
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass


GIB = 1024**3
# KiCad 10 loads both PCB and schematic engines for parity checking. This is
# a conservative launch guard for that native workflow, not proof that every
# board can fit. The OS still enforces the selected aggregate commit ceiling.
MIN_NATIVE_BYTES = 2 * GIB
COMMIT_RESERVE_BYTES = GIB // 4


@dataclass(frozen=True)
class AvailableMemory:
    physical: int
    commit: int


def available_memory() -> AvailableMemory:
    """Read available host memory without assuming a Windows runtime."""
    if sys.platform == "linux":
        records = {}
        with open("/proc/meminfo", encoding="ascii") as stream:
            for line in stream:
                name, _, data = line.partition(":")
                if name in ("MemAvailable", "SwapFree"):
                    records[name] = int(data.split()[0]) * 1024
        return AvailableMemory(
            records["MemAvailable"],
            records["MemAvailable"] + records.get("SwapFree", 0),
        )
    if sys.platform == "darwin":
        result = subprocess.check_output(["vm_stat"], text=True, timeout=5)
        first, *rows = result.splitlines()
        page_size = int(first.split("page size of ", 1)[1].split()[0])
        counts = {
            row.partition(":")[0].lower(): row.partition(":")[2].strip().rstrip(".")
            for row in rows
        }
        capacity = (
            sum(
                int(counts.get(key, "0"))
                for key in ("pages free", "pages inactive", "pages speculative")
            )
            * page_size
        )
        return AvailableMemory(capacity, capacity)
    if sys.platform != "win32":
        raise RuntimeError("No routing memory probe is available on this platform")
    from ctypes import wintypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("load", wintypes.DWORD),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_pagefile", ctypes.c_ulonglong),
            ("available_pagefile", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
    kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError(ctypes.get_last_error())
    return AvailableMemory(
        int(status.available_physical), int(status.available_pagefile)
    )


def memory_ceiling(available: int) -> int:
    if isinstance(available, bool) or not isinstance(available, int) or available <= 0:
        raise ValueError("Available memory must be a positive byte count")
    return min(6 * GIB, available // 2)


def job_memory_ceiling(available: int, page_size: int) -> int:
    """Round down because Windows stores job limits in whole memory pages."""
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0:
        raise ValueError("Memory page size must be a positive integer")
    limit = memory_ceiling(available) // page_size * page_size
    if limit == 0:
        raise ValueError("Insufficient available memory for an AutoPCB job")
    return limit


def plan_resources(
    available: AvailableMemory,
    *,
    memory_gib: float | None = None,
    page_size: int = mmap.PAGESIZE,
) -> dict:
    """Select a bounded budget; never silently raise the automatic RAM limit."""
    automatic = job_memory_ceiling(available.physical, page_size)
    if memory_gib is None:
        limit = automatic
    else:
        if (
            isinstance(memory_gib, bool)
            or not isinstance(memory_gib, (int, float))
            or not math.isfinite(memory_gib)
            or not 0 < memory_gib <= 6
        ):
            raise ValueError(
                "--memory-gib must be a finite number greater than zero and at most 6"
            )
        limit = int(memory_gib * GIB) // page_size * page_size
    if limit < MIN_NATIVE_BYTES:
        raise MemoryError(
            f"The selected {limit / GIB:.2f} GiB budget is below the 2 GiB native-check "
            "launch budget. No routing started. Retry when more RAM is available, "
            "use --wait-for-memory 60, or explicitly allow --memory-gib 3."
        )
    if available.commit < limit + COMMIT_RESERVE_BYTES:
        raise MemoryError(
            f"Windows has {available.commit / GIB:.2f} GiB of commit capacity available; "
            f"the {limit / GIB:.2f} GiB routing budget needs another 0.25 GiB of headroom. "
            "No routing started. Retry after memory becomes available."
        )
    return {
        "available_physical_bytes": available.physical,
        "available_commit_bytes": available.commit,
        "job_commit_limit_bytes": limit,
        "budget_mode": "automatic" if memory_gib is None else "explicit",
        "scope": "routing process and child workers",
    }


def _resident_tree() -> int:
    output = subprocess.check_output(
        ["ps", "-ax", "-o", "pid=,ppid=,rss="], text=True, timeout=5
    )
    children = {}
    memory = {}
    for line in output.splitlines():
        process, parent, rss = map(int, line.split())
        children.setdefault(parent, []).append(process)
        memory[process] = rss * 1024
    seen = set()
    stack = [os.getpid()]
    while stack:
        current = stack.pop()
        if current not in seen:
            seen.add(current)
            stack.extend(children.get(current, ()))
    return sum(memory.get(process, 0) for process in seen)


@contextmanager
def _posix_limit(plan: dict):
    """Watch resident memory and stop the coordinator on the next sample."""
    from _thread import interrupt_main

    budget = plan["job_commit_limit_bytes"]
    if _resident_tree() > budget:
        raise MemoryError("AutoPCB is already above its POSIX routing memory budget")
    done = threading.Event()
    failures = []

    def watch():
        try:
            while not done.wait(0.5):
                if _resident_tree() > budget:
                    failures.append(
                        "The sampled routing process tree exceeded its memory budget."
                    )
                    interrupt_main()
                    return
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            failures.append(f"Could not monitor routing process memory: {error}")
            interrupt_main()

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        try:
            yield {
                **plan,
                "scope": "routing process tree",
                "budget_metric": "sampled_resident_bytes",
            }
        except KeyboardInterrupt:
            if not failures:
                raise
        if failures:
            raise MemoryError(failures[0])
    finally:
        done.set()
        thread.join(timeout=6)


@contextmanager
def limit_resources(
    *, memory_gib: float | None = None, wait_seconds: float = 0, progress=None
):
    """Constrain this CLI process and children; fail closed if unavailable.

    Windows uses committed memory. POSIX uses aggregate resident samples.
    """
    if (
        isinstance(wait_seconds, bool)
        or not isinstance(wait_seconds, (int, float))
        or not math.isfinite(wait_seconds)
        or wait_seconds < 0
    ):
        raise ValueError(
            "--wait-for-memory must be a finite, nonnegative number of seconds"
        )
    deadline = time.monotonic() + wait_seconds
    next_message = 0.0
    while True:
        try:
            plan = plan_resources(available_memory(), memory_gib=memory_gib)
            break
        except MemoryError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            if progress and time.monotonic() >= next_message:
                progress(
                    {
                        "event": "stage",
                        "message": f"Waiting for native-check memory; up to {remaining:.0f} seconds remain",
                    }
                )
                next_message = time.monotonic() + 20
            time.sleep(min(5, remaining))
    if sys.platform != "win32":
        with _posix_limit(plan) as guarded:
            yield guarded
        return
    from ctypes import wintypes

    class BasicLimit(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong),
            ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD),
            ("minimum_working_set", ctypes.c_size_t),
            ("maximum_working_set", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "read_ops",
                "write_ops",
                "other_ops",
                "read_bytes",
                "write_bytes",
                "other_bytes",
            )
        ]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimit),
            ("io", IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t),
            ("peak_job_memory", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    limit = plan["job_commit_limit_bytes"]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        settings = ExtendedLimit()
        settings.basic.flags = 0x00000200  # JOB_OBJECT_LIMIT_JOB_MEMORY
        settings.job_memory_limit = limit
        size = ctypes.sizeof(settings)
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(settings), size):
            raise ctypes.WinError(ctypes.get_last_error())
        actual = ExtendedLimit()
        if not kernel.QueryInformationJobObject(
            job, 9, ctypes.byref(actual), size, None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if actual.job_memory_limit != limit or not (actual.basic.flags & 0x00000200):
            raise RuntimeError(
                f"Windows returned job memory limit {actual.job_memory_limit}, requested {limit}, flags {actual.basic.flags:#x}"
            )
        if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
        yield plan
    finally:
        kernel.CloseHandle(job)
