"""Run headless native tools with complete, bounded failure diagnostics."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Sequence


class NativeCommandError(RuntimeError):
    """A failed native operation is never a successful design check."""


class NativeResourceError(NativeCommandError, MemoryError):
    """The native tool reported exhausted memory or Windows commit capacity."""


def exit_description(returncode: int) -> str:
    """Keep Windows exception codes readable even when Python returns a signed int."""
    value = returncode & 0xFFFFFFFF
    if os.name == "nt" and value >= 0x80000000:
        return f"exit {returncode} (0x{value:08X})"
    return f"exit {returncode}"


def _detail(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    parts = []
    for value in (stderr, stdout):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if value and value.strip() and value.strip() not in parts:
            parts.append(value.strip())
    return "\n".join(parts)[-4000:] or "The tool returned no diagnostic output."


def run_checked(
    command: Sequence[str], *, operation: str, output: Path | None = None,
    timeout: float = 180,
) -> subprocess.CompletedProcess[str]:
    """Run without opening an editor; require both success and a fresh output.

    Output paths must be new, normally inside the caller's temporary snapshot.
    An old report must never be mistaken for the result of the current check.
    """
    if output is not None and output.exists():
        raise NativeCommandError(f"{operation} refused an existing output: {output.name}")
    try:
        result = subprocess.run(
            list(command), stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
            check=False, creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as error:
        raise NativeCommandError(
            f"{operation} timed out after {timeout:g} seconds. "
            + _detail(error.stdout, error.stderr)
        ) from error
    except OSError as error:
        resource_failure = getattr(error, "winerror", None) in {8, 14, 1450, 1455} or error.errno == 12
        error_type = NativeResourceError if resource_failure else NativeCommandError
        raise error_type(f"{operation} could not start: {error}") from error
    if result.returncode:
        detail = _detail(result.stdout, result.stderr)
        memory_failure = any(text in detail.casefold() for text in (
            "bad_alloc", "not enough memory", "out of memory", "error 1455",
            "paging file is too small", "cannot allocate memory",
        )) or (result.returncode & 0xFFFFFFFF) in {0xC0000017, 0xC000012D}
        error_type = NativeResourceError if memory_failure else NativeCommandError
        raise error_type(
            f"{operation} failed ({exit_description(result.returncode)}). "
            + detail
        )
    if output is not None and not output.is_file():
        raise NativeCommandError(
            f"{operation} exited successfully but did not produce {output.name}. "
            + _detail(result.stdout, result.stderr)
        )
    return result
