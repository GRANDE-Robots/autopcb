"""Serve AutoPCB to a phone or any browser that can reach this computer.

KiCad runs only on desktop systems, so a phone drives the jobs here. Every
request needs the access token printed at startup. The default address is
this computer only; pass --host to reach it over a LAN or Tailscale.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from autopcb.native import engine_path
from autopcb.workspace import Workspace

PAGE = Path(__file__).with_name("serve.html")


@dataclass
class Job:
    action: str
    project: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "running"
    started: float = field(default_factory=time.time)
    finished: float | None = None
    report: dict | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "action": self.action,
            "project": self.project,
            "status": self.status,
            "started": self.started,
            "finished": self.finished,
            "report": self.report,
        }


class JobRunner:
    """Run CLI actions in subprocesses, one at a time per project."""

    ACTIONS = {
        "verify": (),
        "route": ("--rounds", "10", "--seconds", "30"),
        "apply": (),
        "export": ("--draft",),
    }

    def __init__(self, workspace: Workspace, extra: tuple[str, ...] = ()):
        self.workspace, self.extra = workspace, extra
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def command(self, job: Job, run_id: str | None) -> list[str]:
        arguments = [
            *self.ACTIONS[job.action],
            *((run_id,) if job.action == "apply" else ()),
        ]
        invocation = [] if getattr(sys, "frozen", False) else ["-B", "-m", "autopcb"]
        return [
            sys.executable,
            *invocation,
            "--workspace",
            str(self.workspace.root),
            *self.extra,
            "--project",
            job.project,
            job.action,
            *arguments,
        ]

    def _run(self, job: Job, run_id: str | None) -> None:
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                filter(None, (str(engine_path()), os.environ.get("PYTHONPATH")))
            ),
        }
        try:
            done = subprocess.run(
                self.command(job, run_id),
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            job.report = (
                json.loads(done.stdout)
                if done.stdout.strip()
                else {"error": done.stderr[-2000:]}
            )
            job.status = job.report.get("status", "failed")
        except (OSError, ValueError) as error:
            job.report, job.status = {"error": str(error)}, "failed"
        job.finished = time.time()

    def start(self, action: str, project: str, run_id: str | None = None) -> Job:
        if action not in self.ACTIONS:
            raise ValueError(f"Unknown action: {action}")
        if action == "apply" and not run_id:
            raise ValueError("Apply needs the run ID of a routing preview")
        with self.lock:
            if any(
                job.project == project and job.finished is None
                for job in self.jobs.values()
            ):
                raise RuntimeError("A job is already running for this project")
            job = Job(action, project)
            self.jobs[job.id] = job
        threading.Thread(target=self._run, args=(job, run_id), daemon=True).start()
        return job


class ServeApp:
    """Answer the phone page's requests; transport-free so it is easy to test."""

    def __init__(
        self,
        workspace: Workspace,
        token: str,
        runner: JobRunner,
        cli: Path | None = None,
    ):
        self.workspace, self.token, self.runner, self.cli = (
            workspace,
            token,
            runner,
            cli,
        )

    def authorized(self, headers, query: dict) -> bool:
        supplied = (
            headers.get("Authorization", "").removeprefix("Bearer ")
            or query.get("token", [""])[0]
        )
        return secrets.compare_digest(supplied, self.token)

    def projects(self) -> list[dict]:
        return [
            {"name": entry.stem, "path": str(entry)}
            for entry in self.workspace.entries()
        ]

    def project(self, path: str) -> Path:
        entry = Path(path).resolve()
        if entry not in self.workspace.entries():
            raise LookupError("That project is not in this workspace")
        return entry

    def board_svg(self, entry: Path) -> bytes:
        from core.toolchain import kicad_cli

        with tempfile.TemporaryDirectory(prefix="autopcb-serve-") as folder:
            target = Path(folder) / "board.svg"
            subprocess.run(
                [
                    str(self.cli or kicad_cli()),
                    "pcb",
                    "export",
                    "svg",
                    "--mode-single",
                    "--layers",
                    "F.Cu,B.Cu,F.Silkscreen,Edge.Cuts",
                    "--page-size-mode",
                    "2",
                    "--exclude-drawing-sheet",
                    "--output",
                    str(target),
                    str(entry.with_suffix(".kicad_pcb")),
                ],
                capture_output=True,
                check=True,
                timeout=120,
            )
            return target.read_bytes()

    def handle(
        self, method: str, url: str, headers, body: bytes
    ) -> tuple[int, str, bytes]:
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        if method == "GET" and parts.path == "/":
            return 200, "text/html; charset=utf-8", PAGE.read_bytes()
        if not self.authorized(headers, query):
            return self.json(401, {"error": "Missing or wrong access token"})
        try:
            if method == "GET" and parts.path == "/api/projects":
                return self.json(200, {"projects": self.projects()})
            if method == "GET" and parts.path == "/api/jobs":
                return self.json(
                    200,
                    {
                        "jobs": [
                            job.as_dict() for job in reversed(self.runner.jobs.values())
                        ]
                    },
                )
            if method == "GET" and parts.path == "/api/board.svg":
                return (
                    200,
                    "image/svg+xml",
                    self.board_svg(self.project(query.get("project", [""])[0])),
                )
            if method == "POST" and parts.path == "/api/jobs":
                request = json.loads(body or b"{}")
                job = self.runner.start(
                    request.get("action", ""),
                    str(self.project(request.get("project", ""))),
                    request.get("run_id"),
                )
                return self.json(202, job.as_dict())
        except LookupError as error:
            return self.json(404, {"error": str(error)})
        except RuntimeError as error:
            return self.json(409, {"error": str(error)})
        except (ValueError, subprocess.SubprocessError) as error:
            return self.json(400, {"error": str(error)})
        return self.json(404, {"error": "Not found"})

    @staticmethod
    def json(status: int, payload: dict) -> tuple[int, str, bytes]:
        return status, "application/json", json.dumps(payload).encode()


class Server:
    """Bind the app to an address and serve until interrupted."""

    def __init__(self, app: ServeApp, host: str, port: int):
        app_ref = app

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                status, kind, payload = app_ref.handle(
                    method, self.path, self.headers, self.rfile.read(length)
                )
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):  # noqa: N802 - http.server naming
                self._reply("GET")

            def do_POST(self):  # noqa: N802 - http.server naming
                self._reply("POST")

            def log_message(self, *_):
                pass

        self.app, self.http = app, ThreadingHTTPServer((host, port), Handler)

    @property
    def url(self) -> str:
        host, port = self.http.server_address[:2]
        return f"http://{host}:{port}/?token={self.app.token}"

    def run(self) -> None:
        try:
            self.http.serve_forever()
        finally:
            self.http.server_close()
