"""native_command.py: run headless native tools with complete, bounded failure diagnostics.

A failed native operation is never reported as a successful design check: every
failure raises, with the tool's own output attached.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping, Sequence


class NativeCommandError(RuntimeError):
    """A failed native operation is never a successful design check."""


class NativeResourceError(NativeCommandError, MemoryError):
    """The native tool reported exhausted memory or Windows commit capacity."""


class NativeCommand:
    """Runs one headless native tool and requires both success and a fresh output."""

    MEMORY_MARKERS = (
        "bad_alloc",
        "not enough memory",
        "out of memory",
        "error 1455",
        "paging file is too small",
        "cannot allocate memory",
    )

    @staticmethod
    def exit_description(returncode: int) -> str:
        """Keep Windows exception codes readable even when Python returns a signed int."""
        value = returncode & 0xFFFFFFFF
        if os.name == "nt" and value >= 0x80000000:
            return f"exit {returncode} (0x{value:08X})"
        return f"exit {returncode}"

    @staticmethod
    def detail(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
        """Join the distinct, non-empty tool output, keeping its last 4000 characters."""
        parts = []
        for value in (stderr, stdout):
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            if value and value.strip() and value.strip() not in parts:
                parts.append(value.strip())
        return "\n".join(parts)[-4000:] or "The tool returned no diagnostic output."

    def __call__(
        self,
        command: Sequence[str],
        *,
        operation: str,
        output: Path | None = None,
        timeout: float = 180,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run without opening an editor; require both success and a fresh output.

        Output paths must be new, normally inside the caller's temporary snapshot.
        An old report must never be mistaken for the result of the current check.

        Inputs:
            command: Program and arguments.
            operation: Name used in error messages.
            output: File the tool must create, which must not exist beforehand.
            timeout: Seconds before the run is abandoned.
            env: Environment for the process, or None to inherit.
        Outputs:
            The completed process. Raises NativeResourceError for an exhausted
            memory or commit limit and NativeCommandError for every other
            failure: an existing output, a timeout, a failed start, a non-zero
            exit, or a missing output.
        """
        if output is not None and output.exists():
            raise NativeCommandError(
                f"{operation} refused an existing output: {output.name}"
            )
        try:
            result = subprocess.run(
                list(command),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
                check=False,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as error:
            raise NativeCommandError(
                f"{operation} timed out after {timeout:g} seconds. "
                + self.detail(error.stdout, error.stderr)
            ) from error
        except OSError as error:
            resource_failure = (
                getattr(error, "winerror", None) in {8, 14, 1450, 1455}
                or error.errno == 12
            )
            error_type = NativeResourceError if resource_failure else NativeCommandError
            raise error_type(f"{operation} could not start: {error}") from error
        if result.returncode:
            detail = self.detail(result.stdout, result.stderr)
            memory_failure = any(
                text in detail.casefold() for text in self.MEMORY_MARKERS
            ) or (result.returncode & 0xFFFFFFFF) in {0xC0000017, 0xC000012D}
            error_type = NativeResourceError if memory_failure else NativeCommandError
            raise error_type(
                f"{operation} failed ({self.exit_description(result.returncode)}). "
                + detail
            )
        if output is not None and not output.is_file():
            raise NativeCommandError(
                f"{operation} exited successfully but did not produce {output.name}. "
                + self.detail(result.stdout, result.stderr)
            )
        return result


run_checked = NativeCommand()
