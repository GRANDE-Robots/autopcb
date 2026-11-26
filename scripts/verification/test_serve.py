"""test_serve.py: tests of the phone page server.

The phone page: token on every API call, workspace-only projects, one job per project.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from autopcb.serve import JobRunner, PairingCode, Server, ServeApp
from autopcb.workspace import Workspace


class HeldRunner(JobRunner):
    """Record jobs without starting KiCad; finishing is left to the test."""

    def _run(self, job, run_id):
        """Remember the job and the run id instead of running them."""
        self.last = (job, run_id)


class ServeTests(unittest.TestCase):
    """Tests of the phone page: the token, the workspace projects and the job limits."""

    def setUp(self):
        """Create a temporary workspace with one project.

        A held runner and an app with the token ``secret`` serve it.
        """
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        board = root / "results" / "projects" / "Board"
        board.mkdir(parents=True)
        for suffix in (".kicad_pro", ".kicad_pcb", ".kicad_sch"):
            (board / f"Board{suffix}").write_text("", encoding="utf-8")
        self.entry = str((board / "Board.kicad_pro").resolve())
        workspace = Workspace(root)
        self.runner = HeldRunner(workspace)
        self.app = ServeApp(workspace, "secret", self.runner)
        self.auth = {"Authorization": "Bearer secret"}

    def call(self, method, url, body=None, headers=None):
        """Send one request to the app.

        Inputs:
            method: HTTP method.
            url: Request path.
            body: Object sent as JSON, or None for an empty body.
            headers: Request headers; the bearer-token headers when None.
        Outputs:
            The response status and payload.
        """
        status, _, payload = self.app.handle(
            method,
            url,
            headers if headers is not None else self.auth,
            json.dumps(body).encode() if body is not None else b"",
        )
        return status, payload

    def test_page_is_public_but_every_api_call_needs_the_token(self):
        self.assertEqual(self.call("GET", "/", headers={})[0], 200)
        self.assertEqual(self.call("GET", "/api/projects", headers={})[0], 401)
        self.assertEqual(
            self.call(
                "GET", "/api/projects", headers={"Authorization": "Bearer wrong"}
            )[0],
            401,
        )
        status, payload = self.call("GET", "/api/projects")
        self.assertEqual(
            (status, json.loads(payload)["projects"][0]["path"]), (200, self.entry)
        )

    def test_token_in_the_url_is_never_accepted(self):
        self.assertEqual(
            self.call("GET", "/api/projects?token=secret", headers={})[0], 401
        )
        self.assertEqual(
            self.call("GET", "/api/board.svg?token=secret", headers={})[0], 401
        )

    def test_info_is_public_and_says_whether_the_token_is_right(self):
        status, payload = self.call("GET", "/api/info", headers={})
        info = json.loads(payload)
        self.assertEqual((status, info["product"], info["api"]), (200, "AutoPCB", 1))
        self.assertEqual(info["version"], ServeApp.version())
        self.assertFalse(info["authorized"])
        self.assertTrue(json.loads(self.call("GET", "/api/info")[1])["authorized"])
        wrong = {"Authorization": "Bearer wrong"}
        self.assertFalse(
            json.loads(self.call("GET", "/api/info", headers=wrong)[1])["authorized"]
        )

    def test_pairing_link_keeps_the_token_in_the_fragment(self):
        server = Server(self.app, "127.0.0.1", 0)
        self.addCleanup(server.http.server_close)
        self.assertRegex(server.url, r"^http://127\.0\.0\.1:\d+/#token=secret$")

    @unittest.skipUnless(importlib.util.find_spec("segno"), "needs autopcb[phone]")
    def test_pairing_code_draws_the_link(self):
        drawn = PairingCode.draw("http://127.0.0.1:8765/#token=secret")
        self.assertGreater(len(drawn.splitlines()), 10)

    def test_jobs_are_limited_to_known_actions_workspace_projects_and_one_per_project(
        self,
    ):
        self.assertEqual(
            self.call("POST", "/api/jobs", {"action": "rm", "project": self.entry})[0],
            400,
        )
        self.assertEqual(
            self.call(
                "POST", "/api/jobs", {"action": "verify", "project": "/etc/passwd"}
            )[0],
            404,
        )
        self.assertEqual(
            self.call("POST", "/api/jobs", {"action": "apply", "project": self.entry})[
                0
            ],
            400,
        )
        status, payload = self.call(
            "POST", "/api/jobs", {"action": "verify", "project": self.entry}
        )
        self.assertEqual((status, json.loads(payload)["status"]), (202, "running"))
        self.assertEqual(
            self.call("POST", "/api/jobs", {"action": "route", "project": self.entry})[
                0
            ],
            409,
        )
        job, _ = self.runner.last
        self.assertEqual(
            self.runner.command(job, None)[-3:], ["--project", self.entry, "verify"]
        )

    def test_server_answers_over_http_with_the_token(self):
        server = Server(self.app, "127.0.0.1", 0)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        self.addCleanup(server.http.shutdown)
        base = server.url.split("/#")[0]
        with urllib.request.urlopen(
            urllib.request.Request(base + "/api/jobs", headers=self.auth)
        ) as response:
            self.assertEqual(json.loads(response.read()), {"jobs": []})
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(base + "/api/jobs?token=wrong")
        self.assertEqual(refused.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
