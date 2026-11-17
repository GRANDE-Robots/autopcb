"""Readable, bounded terminal output; raw records are an explicit opt-in.

This presentation layer never changes stored evidence or verification results.
Plain ASCII and append-only lines work in Git Bash, narrow terminals and logs.
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


_INTERNAL_ID = re.compile(
    r"\b[0-9a-fA-F]{64}\b|\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b"
)
_CONTROL = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]")


def readable(value: object) -> str:
    """Hide internal identifiers even when a native error embeds them."""
    text = _CONTROL.sub("", str(value))
    text = _INTERNAL_ID.sub("[internal identifier]", text)
    return re.sub(
        r"\b(?:sha256|sha-256|hash(?:es)?)\b", "source check", text, flags=re.I
    )


def duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return (
        f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"
    )


def add_output_arguments(
    parser: argparse.ArgumentParser, *, full_alias: bool = False
) -> None:
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


class Console:
    """Keep one coherent output stream across worker and heartbeat messages."""

    def __init__(
        self,
        *,
        json_mode: bool = False,
        verbose: bool = False,
        stream: TextIO | None = None,
        errors: TextIO | None = None,
        width: int | None = None,
    ) -> None:
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

    def line(
        self, message: object = "", *, prefix: str = "", error: bool = False
    ) -> None:
        with self._lock:
            destination = self.errors if error else self.stream
            for paragraph in readable(message).splitlines() or [""]:
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
        self.line()
        self.line(title)
        self.line("-" * min(self.width, 72))

    def rows(self, values) -> None:
        rows = [(str(name), value) for name, value in values if value is not None]
        pad = min(max((len(name) for name, _ in rows), default=0), self.width // 2)
        for name, value in rows:
            self.line(value, prefix=f"  {name:<{pad}} : ")

    def event(self, record: dict) -> None:
        with self._lock:
            if self.json_mode:
                print(json.dumps(record, allow_nan=False), file=self.errors, flush=True)
                return
            if record.get("event") == "heartbeat":
                message = f"Still working: {self.current_stage}"
            else:
                message = record.get("message", record.get("event", "Working"))
                self.current_stage = readable(message)
            self.line(message, prefix=f"[{duration(time.monotonic() - self.started)}] ")

    def stage(self, message: str) -> None:
        self.event({"event": "stage", "message": message})

    def result(self, record: dict, render: Callable[[Console, dict], None]) -> None:
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
        self.stage(message)
        with progress_heartbeat(self.event):
            yield


@contextmanager
def progress_heartbeat(emit, *, interval: float = 50.0):
    """Report a long-running stage without guessing an ETA or percent complete."""
    if interval <= 0:
        raise ValueError("Heartbeat interval must be positive")
    stop = threading.Event()
    started = time.monotonic()

    def heartbeat():
        while not stop.wait(interval):
            emit(
                {
                    "event": "heartbeat",
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                }
            )

    thread = threading.Thread(target=heartbeat, name="autopcb-progress", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
