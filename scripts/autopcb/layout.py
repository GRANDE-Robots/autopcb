"""layout.py: preview, compare and apply routing for one native project.

Previews run the existing autoroute command in KiCad's Python. A stored preview
is compared with the saved board, and saved only through the guarded publisher.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from autopcb.jobs import inspect, write_json
from autopcb.native import Native, engine_path
from autopcb.policy import RoutingPolicy
from core.paths import ProjectStorage
from core.project import ENTRY_ENV, ProjectPaths


@dataclass(frozen=True)
class Budget:
    """The routing search budget shared by every arm of a comparison."""

    rounds: int
    seconds: int
    memory_gib: float

    def as_dict(self) -> dict:
        """The budget as report fields: rounds, seconds_per_batch and memory_gib."""
        return {
            "rounds": self.rounds,
            "seconds_per_batch": self.seconds,
            "memory_gib": self.memory_gib,
        }


class Layout:
    """Routing preview, comparison and guarded apply for one native project."""

    def __init__(
        self,
        native: Native,
        project: ProjectPaths,
        storage: ProjectStorage | None = None,
    ):
        """Bind the toolchain, project and storage.

        Inputs:
            native: KiCad toolchain that runs the jobs.
            project: Native project to route.
            storage: Where the policy and results live; the project's own storage when None.
        """
        self.native, self.project = native, project
        self.storage = storage or ProjectStorage(project.root)

    @staticmethod
    def check_id(run_id: str) -> str:
        """Check the ID of a saved routing preview.

        Outputs:
            The ID unchanged. Raises ValueError unless it is 32 lowercase hexadecimal
            characters.
        """
        if not re.fullmatch("[0-9a-f]{32}", run_id):
            raise ValueError("Expected the 32-character ID of a saved routing preview")
        return run_id

    def policy(self, *, defaults: Path | None = None) -> dict:
        """Create a conservative starter policy on explicit request only."""
        target = self.storage.config / "autopcb.json"
        if target.exists():
            raise FileExistsError(f"A routing policy already exists: {target}")
        existing = self.storage.config_file("autopcb.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        if existing.is_file():
            values = json.loads(existing.read_text(encoding="utf-8-sig"))
        else:
            geometry = inspect(self.native, self.project)["geometry"]
            path = self.project.settings
            settings = (
                json.loads(path.read_text(encoding="utf-8-sig"))
                if path.is_file()
                else {}
            )
            rules = (
                settings.get("board", {}).get("design_settings", {}).get("rules", {})
            )
            values = RoutingPolicy.compile(
                geometry["copper_layers"], defaults=defaults, minima=rules
            )
        write_json(target, values)
        return {
            "schema_version": 1,
            "kind": "routing_policy",
            "status": "created",
            "entry": str(self.project.entry),
            "policy": str(target),
        }

    def _invoke(self, module: str, arguments: list[str]) -> dict:
        """Run a routing module in KiCad's Python and read its JSON result.

        Inputs:
            module: Dotted module name, run with ``-m`` from the engine folder.
            arguments: Arguments placed after ``--project``; ``--json`` is appended.
        Outputs:
            The JSON object the module printed. Raises RuntimeError with the last line of
            its error output when it printed nothing, and with its error output when the
            result is not an object or the exit status is not 0 or 2. An interrupt
            terminates the child and is raised again.
        """
        source_root = engine_path()
        env = {
            **os.environ,
            "PYTHONPATH": str(source_root),
            "KICAD_CLI": str(self.native.cli),
            ENTRY_ENV: str(self.project.entry),
            **self.storage.environment(self.project.entry),
        }
        with tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(
                [
                    str(self.native.python),
                    "-B",
                    "-m",
                    module,
                    "--project",
                    str(self.project.root),
                    *arguments,
                    "--json",
                ],
                stdout=subprocess.PIPE,
                stderr=stderr,
                env=env,
                cwd=source_root,
                encoding="utf-8",
                errors="replace",
                **(
                    {"creationflags": subprocess.CREATE_NO_WINDOW}
                    if os.name == "nt"
                    else {}
                ),
            )
            try:
                output, _ = process.communicate()
            except KeyboardInterrupt:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
                raise
            stderr.seek(0)
            detail = stderr.read().decode("utf-8", errors="replace").strip()
        if not output.strip():
            message = (
                detail.splitlines()[-1]
                if detail
                else f"Layout process exited {process.returncode}"
            )
            try:
                message = json.loads(message).get("message", message)
            except (ValueError, AttributeError):
                pass
            raise RuntimeError(message)
        result = json.loads(output)
        if not isinstance(result, dict) or process.returncode not in (0, 2):
            raise RuntimeError(
                detail or "The routing worker returned an invalid result"
            )
        return result

    def preview(
        self,
        *,
        rounds: int,
        seconds: int,
        memory_gib: float | None = None,
        resume: str | None = None,
        allow_placement: bool = False,
        renew: str | None = None,
    ) -> dict:
        """Run a bounded routing preview and keep it as a source-bound run.

        Inputs:
            rounds: Search rounds after which the run pauses.
            seconds: Search time of one batch, in seconds.
            memory_gib: Memory limit in GiB; the router's default when None.
            resume: ID of a source-matched run to resume.
            allow_placement: Let the router move parts; otherwise placement is kept.
            renew: ID of a preview to recheck and rebuild in a new run.
        Outputs:
            The router's result plus kind ``layout_preview`` and the path of the run
            record. Raises ValueError when the project has no routing policy or an ID is
            malformed, and RuntimeError when the router did not retain a run ID.
        """
        if not self.storage.config_file("autopcb.json").is_file():
            raise ValueError(
                "Create and review the project routing policy before running layout."
            )
        args = ["run", "--max-rounds", str(rounds), "--seconds-per-batch", str(seconds)]
        if not allow_placement:
            args.append("--keep-placement")
        if memory_gib is not None:
            args.extend(("--memory-gib", str(memory_gib)))
        if resume is not None:
            args.extend(("--resume", self.check_id(resume)))
        if renew is not None:
            args.extend(("--renew", self.check_id(renew)))
        result = self._invoke("pcb.net_routing.autoroute_cli", args)
        if not isinstance(result.get("run_id"), str):
            raise RuntimeError("The router did not retain a source-bound run")
        return {
            **result,
            "schema_version": 1,
            "kind": "layout_preview",
            "record": str(
                self.storage.results / "autoroute" / (result["run_id"] + ".json")
            ),
        }

    def compare(self, run_id: str) -> dict:
        """Read the saved board and a stored preview side by side.

        Outputs:
            A ``layout_compare`` report with the geometry of the saved board (before) and
            of the preview (after). Raises ValueError for a malformed run ID.
        """
        self.check_id(run_id)
        with tempfile.TemporaryDirectory(prefix="autopcb-compare-") as working:
            target = Path(working) / "comparison.json"
            self.native.worker(
                "compare",
                self.project.entry,
                target,
                run_id,
                output=target,
                storage=self.storage,
            )
            geometry = json.loads(target.read_text(encoding="utf-8"))
        return {
            "schema_version": 1,
            "kind": "layout_compare",
            "status": "inspected",
            "run_id": run_id,
            **geometry,
        }

    def apply(self, run_id: str) -> dict:
        """Save a stored preview through the guarded publisher.

        Outputs:
            A ``layout_apply`` report with production_approved False. Raises RuntimeError
            with the publisher's message when the preview was not saved as work in progress.
        """
        self.check_id(run_id)
        with tempfile.TemporaryDirectory(prefix="autopcb-apply-result-") as folder:
            output = Path(folder) / "applied.json"
            self.native.worker(
                "apply",
                self.project.entry,
                output,
                run_id,
                output=output,
                timeout=300,
                storage=self.storage,
            )
            result = json.loads(output.read_text(encoding="utf-8"))
        if result.get("status") != "saved_work_in_progress":
            raise RuntimeError(
                result.get(
                    "error", "The preview was not accepted by the guarded publisher"
                )
            )
        return {
            **result,
            "schema_version": 1,
            "kind": "layout_apply",
            "production_approved": False,
        }
