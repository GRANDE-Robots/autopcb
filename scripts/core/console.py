"""console.py: readable, bounded terminal output; raw records are an explicit opt-in.

This presentation layer never changes stored evidence or verification results.
Plain ASCII and append-only lines work in Git Bash, narrow terminals and logs.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import textwrap
import threading
import time
from contextlib import contextmanager
from typing import Callable, TextIO


class Duration:
    """Formats elapsed seconds for progress lines."""

    def __call__(self, seconds: float) -> str:
        """Format seconds as ``MM:SS``, or ``H:MM:SS`` from one hour; negatives count as zero."""
        total = max(0, int(seconds))
        hours, rest = divmod(total, 3600)
        minutes, seconds = divmod(rest, 60)
        return (
            f"{hours}:{minutes:02}:{seconds:02}"
            if hours
            else f"{minutes:02}:{seconds:02}"
        )


class OutputArguments:
    """Adds the shared ``--json`` and ``--verbose`` options to a command line."""

    def __call__(
        self, parser: argparse.ArgumentParser, *, full_alias: bool = False
    ) -> None:
        """Add the options to ``parser``.

        Inputs:
            parser: The parser or subparser to extend.
            full_alias: Also accept ``--full`` for ``--verbose``.
        """
        parser.add_argument(
            "--json",
            action="store_true",
            help="raw result on stdout; progress events on stderr",
        )
        flags = ("-v", "--verbose", "--full") if full_alias else ("-v", "--verbose")
        parser.add_argument(
            *flags,
            dest="verbose",
            action="store_true",
            help="include detailed names and search diagnostics (no internal identifiers)",
        )


class ProgressHeartbeat:
    """Reports a long-running stage without guessing an ETA or percent complete.

    Used as a context manager: a daemon thread emits one heartbeat record each
    ``interval`` seconds until the block exits, and is joined on exit.
    """

    def __init__(self, emit: Callable[[dict], None], *, interval: float = 50.0):
        """Set the receiver and period.

        Inputs:
            emit: Called with each heartbeat record.
            interval: Seconds between heartbeats; must be positive.
        Outputs: None. Raises ValueError for a non-positive interval.
        """
        if interval <= 0:
            raise ValueError("Heartbeat interval must be positive")
        self.emit, self.interval = emit, interval
        self.stop = threading.Event()
        self.started = 0.0
        self.thread = threading.Thread(
            target=self.beat, name="autopcb-progress", daemon=True
        )

    def beat(self) -> None:
        """Emit a heartbeat with the elapsed time until stopped."""
        while not self.stop.wait(self.interval):
            self.emit(
                {
                    "event": "heartbeat",
                    "elapsed_seconds": round(time.monotonic() - self.started, 1),
                }
            )

    def __enter__(self) -> ProgressHeartbeat:
        """Start the heartbeat thread."""
        self.started = time.monotonic()
        self.thread.start()
        return self

    def __exit__(self, *_exception) -> None:
        """Stop the heartbeat thread and wait for it."""
        self.stop.set()
        self.thread.join()


duration = Duration()
add_output_arguments = OutputArguments()


class Console:
    """Keep one coherent output stream across worker and heartbeat messages."""

    INTERNAL_ID = re.compile(
        r"\b[0-9a-fA-F]{64}\b|\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b"
    )
    CONTROL = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]")

    def __init__(
        self,
        *,
        json_mode: bool = False,
        verbose: bool = False,
        stream: TextIO | None = None,
        errors: TextIO | None = None,
        width: int | None = None,
    ) -> None:
        """Choose the output mode and streams.

        Inputs:
            json_mode: Print raw JSON results to ``stream`` and events to ``errors``.
            verbose: Include detailed names and diagnostics.
            stream: Result stream; standard output when None.
            errors: Progress and error stream; standard error when None.
            width: Wrap width in columns, kept between 36 and 100; the terminal
                width when None.
        """
        self.json_mode = json_mode
        self.verbose = verbose
        self.stream = stream if stream is not None else sys.stdout
        self.errors = errors if errors is not None else sys.stderr
        self.width = max(
            36, min(width or shutil.get_terminal_size((88, 24)).columns, 100)
        )
        self.started = time.monotonic()
        self.current_stage = "Working"
        self._lock = threading.RLock()

    @classmethod
    def readable(cls, value: object) -> str:
        """Hide control codes and internal identifiers, even when a native error embeds them."""
        text = cls.CONTROL.sub("", str(value))
        text = cls.INTERNAL_ID.sub("[internal identifier]", text)
        return re.sub(
            r"\b(?:sha256|sha-256|hash(?:es)?)\b", "source check", text, flags=re.I
        )

    def line(
        self, message: object = "", *, prefix: str = "", error: bool = False
    ) -> None:
        """Print a message wrapped to the console width, one prefixed block per paragraph."""
        with self._lock:
            destination = self.errors if error else self.stream
            for paragraph in self.readable(message).splitlines() or [""]:
                lines = textwrap.wrap(
                    paragraph,
                    width=self.width,
                    initial_indent=prefix,
                    subsequent_indent=" " * len(prefix),
                )
                for line in lines or [""]:
                    print(line, file=destination, flush=True)

    def command(self, command: str) -> None:
        """Let the terminal soft-wrap a copyable command without inserting newlines."""
        if "\n" in command or "\r" in command:
            raise ValueError("A terminal command must be one logical line")
        with self._lock:
            print(command, file=self.stream, flush=True)

    def heading(self, title: str) -> None:
        """Print a blank line, the title and an underline."""
        self.line()
        self.line(title)
        self.line("-" * min(self.width, 72))

    def rows(self, values) -> None:
        """Print ``name : value`` rows aligned on the colon, skipping None values.

        Inputs:
            values: Iterable of (name, value) pairs.
        """
        rows = [(str(name), value) for name, value in values if value is not None]
        pad = min(max((len(name) for name, _ in rows), default=0), self.width // 2)
        for name, value in rows:
            self.line(value, prefix=f"  {name:<{pad}} : ")

    def event(self, record: dict) -> None:
        """Report a progress record: raw JSON in JSON mode, else a timed line.

        Inputs:
            record: A dict with an ``event`` name and optional ``message``;
                ``heartbeat`` records repeat the current stage.
        """
        with self._lock:
            if self.json_mode:
                print(json.dumps(record, allow_nan=False), file=self.errors, flush=True)
                return
            if record.get("event") == "heartbeat":
                message = f"Still working: {self.current_stage}"
            else:
                message = record.get("message", record.get("event", "Working"))
                self.current_stage = self.readable(message)
            self.line(message, prefix=f"[{duration(time.monotonic() - self.started)}] ")

    def stage(self, message: str) -> None:
        """Report the start of a named stage."""
        self.event({"event": "stage", "message": message})

    def result(self, record: dict, render: Callable[[Console, dict], None]) -> None:
        """Print the final result: raw JSON in JSON mode, else the ``render`` callback's output."""
        with self._lock:
            if self.json_mode:
                print(
                    json.dumps(record, indent=2, allow_nan=False),
                    file=self.stream,
                    flush=True,
                )
            else:
                render(self, record)

    def error(self, error: object) -> None:
        """Report an error on the error stream, as JSON in JSON mode."""
        with self._lock:
            if self.json_mode:
                print(
                    json.dumps({"event": "error", "message": str(error)}),
                    file=self.errors,
                    flush=True,
                )
            else:
                self.line(error, prefix="ERROR: ", error=True)

    @contextmanager
    def activity(self, message: str):
        """Report a stage and keep heartbeats flowing while the block runs."""
        self.stage(message)
        with ProgressHeartbeat(self.event):
            yield
