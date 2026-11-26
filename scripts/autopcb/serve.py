"""serve.py: serve AutoPCB to a phone or any browser that can reach this computer.

KiCad runs only on desktop systems, so a phone drives the jobs here. Every
request except the page and the version check needs the access token printed
at startup, sent in the Authorization header and nowhere else. The default
address is this computer only; pass --host to reach it over a LAN or Tailscale.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from importlib import metadata
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from autopcb.native import engine_path
from autopcb.workspace import Workspace

PAGE = Path(__file__).with_name("serve.html")


@dataclass
class Job:
    """One CLI action started from the page, with its status, timing and report."""

    action: str
    project: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "running"
    started: float = field(default_factory=time.time)
    finished: float | None = None
    report: dict | None = None

    def as_dict(self) -> dict:
        """The job as the JSON record the page shows."""
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
        """Inputs:
        workspace: Workspace whose projects the jobs run on.
        extra: Command-line options placed before ``--project`` in every job.
        """
        self.workspace, self.extra = workspace, extra
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def command(self, job: Job, run_id: str | None) -> list[str]:
        """The command line that runs a job.

        Inputs:
            job: Job to run.
            run_id: Routing preview to apply; used by the ``apply`` action only.
        Outputs:
            This process's executable, running ``autopcb`` unless frozen, with the
            workspace, extra options, project, action and the action's fixed arguments.
        """
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
        """Run a job's command to completion and store its report.

        Outputs:
            None. The report is the JSON the command printed, or the last 2000 characters
            of its error output when it printed none. The status comes from the report and
            is ``failed`` when it has none or the command cannot run.
        """
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
        """Start a job on its own daemon thread.

        Inputs:
            action: One of ACTIONS.
            project: Project entry path.
            run_id: Routing preview to apply; required by ``apply``.
        Outputs:
            The running job. Raises ValueError for an unknown action or an apply without a
            run ID, and RuntimeError when the project already has a running job.
        """
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
        """Inputs:
        workspace: Workspace to serve.
        token: Access token every request except the page must carry.
        runner: Runs the jobs.
        cli: KiCad CLI for board images; the discovered one when None.
        """
        self.workspace, self.token, self.runner, self.cli = (
            workspace,
            token,
            runner,
            cli,
        )

    def authorized(self, headers) -> bool:
        """Say whether a request carries the access token.

        Inputs:
            headers: Request headers; the token is the Authorization bearer value.
        Outputs:
            True when the bearer value equals the token. A token in the URL is never
            accepted, because URLs are logged and kept in browser history.
        """
        supplied = headers.get("Authorization", "").removeprefix("Bearer ")
        return secrets.compare_digest(supplied, self.token)

    @staticmethod
    def version() -> str:
        """The installed AutoPCB version, or ``unknown`` when it is not installed."""
        try:
            return metadata.version("autopcb")
        except metadata.PackageNotFoundError:
            return "unknown"

    def projects(self) -> list[dict]:
        """The workspace's projects as name and path."""
        return [
            {"name": entry.stem, "path": str(entry)}
            for entry in self.workspace.entries()
        ]

    def project(self, path: str) -> Path:
        """Resolve a path to one of the workspace's project entries.

        Outputs:
            The resolved entry. Raises LookupError when it is not in the workspace.
        """
        entry = Path(path).resolve()
        if entry not in self.workspace.entries():
            raise LookupError("That project is not in this workspace")
        return entry

    def board_svg(self, entry: Path) -> bytes:
        """Render a project's board as an SVG image with the KiCad CLI.

        Outputs:
            The SVG bytes of the copper, silkscreen and edge layers. The CLI is given 120
            s, and its failure is raised.
        """
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
        """Answer one request without any transport.

        Inputs:
            method: ``GET`` or ``POST``.
            url: Request path and query.
            headers: Request headers.
            body: Request body bytes.
        Outputs:
            The status code, content type and body. The page and ``/api/info`` are served
            without a token and every other path needs it (401). ``/api/info`` reports
            the product, version and API level, and whether this request's token is right. A missing project is 404, a busy project 409
            and a bad request 400.
        """
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        if method == "GET" and parts.path == "/":
            return 200, "text/html; charset=utf-8", PAGE.read_bytes()
        if method == "GET" and parts.path == "/api/info":
            return self.json(
                200,
                {
                    "product": "AutoPCB",
                    "version": self.version(),
                    "api": 1,
                    "authorized": self.authorized(headers),
                },
            )
        if not self.authorized(headers):
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
        """A JSON response as a (status, content type, body) triple."""
        return status, "application/json", json.dumps(payload).encode()


class PairingCode:
    """Draw the pairing link as a QR code a phone camera or the AutoPCB app can read."""

    @staticmethod
    def draw(link: str) -> str | None:
        """Render the link as terminal text.

        Inputs:
            link: Pairing link from ``Server.url``.
        Outputs:
            Lines of block characters, or None when the optional ``segno`` package
            (``pip install "autopcb[phone]"``) is not installed.
        """
        try:
            import segno
        except ImportError:
            return None
        buffer = io.StringIO()
        segno.make(link, error="m").terminal(out=buffer, compact=True)
        return buffer.getvalue()


class Server:
    """Bind the app to an address and serve until interrupted."""

    def __init__(self, app: ServeApp, host: str, port: int):
        """Bind the app to an address.

        Inputs:
            app: App that answers the requests.
            host: Address to listen on.
            port: Port to listen on.
        """
        app_ref = app

        class Handler(BaseHTTPRequestHandler):
            """Pass each HTTP request to the app."""

            def _reply(self, method: str) -> None:
                """Pass one request to the app and send its response, never cached."""
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
                """Answer a GET request."""
                self._reply("GET")

            def do_POST(self):  # noqa: N802 - http.server naming
                """Answer a POST request."""
                self._reply("POST")

            def log_message(self, *_):
                """Keep the server quiet."""
                pass

        self.app, self.http = app, ThreadingHTTPServer((host, port), Handler)

    @property
    def url(self) -> str:
        """The pairing link: the address with the token in the fragment.

        A browser never sends the fragment to the server, so it stays out of logs.
        """
        host, port = self.http.server_address[:2]
        return f"http://{host}:{port}/#token={self.app.token}"

    def run(self) -> None:
        """Serve until interrupted, then close the socket."""
        try:
            self.http.serve_forever()
        finally:
            self.http.server_close()
