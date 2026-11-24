"""benchmark.py: measure routing arms on isolated copies of one design.

Each arm runs the routing engine from a given scripts folder on a private copy of
the project under one shared budget. The report records wall time, sampled peak
memory and open-connection counts so two engines or two settings can be compared.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import psutil

from autopcb.layout import Budget
from autopcb.native import Native, engine_path
from autopcb.private_copy import PrivateCopy
from core.paths import ProjectStorage, asset_root
from core.project import ENTRY_ENV, ProjectPaths


class Benchmark:
    """Run two routing arms under one budget on identical private copies."""

    # One pass per net: no stage escalation, copper rip-up or placement repair.
    GREEDY = {
        "stages": 0,
        "rip_up_candidates": 0,
        "placement_candidates": 0,
        "early_rework": False,
    }

    def __init__(
        self,
        native: Native,
        project: ProjectPaths,
        budget: Budget,
        arms: tuple,
        storage: ProjectStorage | None = None,
    ):
        """Prepare a private copy of the project for the arms.

        Inputs:
            native: KiCad toolchain that runs each routing process.
            project: Project to copy.
            budget: Rounds, seconds per batch and memory in GiB shared by every arm.
            arms: (label, scripts folder, recovery override or None) triples, run in order.
            storage: Project storage holding the reviewed inputs; the project's own
                storage when None.
        """
        self.native, self.budget, self.arms = native, budget, arms
        self.copy = PrivateCopy(project, storage or ProjectStorage(project.root))

    @classmethod
    def versions(
        cls,
        native: Native,
        project: ProjectPaths,
        baseline: Path,
        budget: Budget,
        storage: ProjectStorage | None = None,
    ) -> Benchmark:
        """Compare a baseline checkout with the current engine."""
        baseline = baseline.resolve()
        if not (baseline / "scripts/run.py").is_file():
            raise ValueError("Select an AutoPCB baseline checkout with scripts/run.py")
        return cls(
            native,
            project,
            budget,
            (
                ("baseline", baseline / "scripts", None),
                ("current", engine_path(), None),
            ),
            storage,
        )

    @classmethod
    def refinement(
        cls,
        native: Native,
        project: ProjectPaths,
        budget: Budget,
        storage: ProjectStorage | None = None,
    ) -> Benchmark:
        """Compare greedy routing with the project's recovery and rework."""
        return cls(
            native,
            project,
            budget,
            (("greedy", engine_path(), cls.GREEDY), ("refined", engine_path(), None)),
            storage,
        )

    @staticmethod
    def _peak_rss(process: subprocess.Popen) -> int:
        """Poll a running process until it exits and report its peak memory.

        Outputs:
            The highest summed resident set size, in bytes, of the process and its
            children, sampled every 0.2 s. When the process vanishes before it can be
            sampled, this waits for it and returns what was sampled so far.
        """
        peak = 0
        try:
            owner = psutil.Process(process.pid)
            while process.poll() is None:
                total = 0
                for item in [owner, *owner.children(recursive=True)]:
                    try:
                        total += item.memory_info().rss
                    except psutil.Error:
                        pass
                peak = max(peak, total)
                time.sleep(0.2)
        except psutil.NoSuchProcess:
            process.wait()
        return peak

    def _measure(self, label: str, scripts: Path, recovery: dict | None) -> dict:
        """Run one arm's routing preview on a fresh private copy and record the outcome.

        Inputs:
            label: Name of the arm in the report.
            scripts: Scripts folder used as PYTHONPATH and working directory.
            recovery: Recovery settings overriding the copy's policy, or None.
        Outputs:
            A record with the arm label, status, diagnostic, wall time in seconds, sampled
            peak memory in bytes, saved and preview open counts, the last progress sample
            and the last check result. When the process prints no JSON the status is
            ``unavailable`` and the diagnostic is the tail of its error output.
        """
        with (
            self.copy.open(recovery) as (working, _),
            tempfile.TemporaryFile() as output,
            tempfile.TemporaryFile() as errors,
        ):
            env = {
                **os.environ,
                "PYTHONPATH": str(scripts),
                ENTRY_ENV: str(working.entry),
                "KICAD_CLI": str(self.native.cli),
            }
            command = [
                str(self.native.python),
                "-B",
                "-m",
                "pcb.net_routing.autoroute_cli",
                "--project",
                str(working.root),
                "run",
                "--keep-placement",
                "--workers",
                "1",
                "--max-rounds",
                str(self.budget.rounds),
                "--seconds-per-batch",
                str(self.budget.seconds),
                "--memory-gib",
                str(self.budget.memory_gib),
                "--json",
            ]
            start = time.perf_counter()
            process = subprocess.Popen(
                command, cwd=scripts, env=env, stdout=output, stderr=errors
            )
            try:
                peak = self._peak_rss(process)
            except BaseException:
                process.terminate()
                process.wait(timeout=10)
                raise
            wall = time.perf_counter() - start
            output.seek(0)
            errors.seek(0)
            try:
                result = json.loads(output.read().decode("utf-8", "replace"))
            except ValueError:
                result = {
                    "status": "unavailable",
                    "error": errors.read().decode("utf-8", "replace")[-2400:],
                }
            record = (
                asset_root(working.root)
                / "results"
                / "autoroute"
                / f"{result.get('run_id', '')}.json"
            )
            samples = result.get("progress_samples", [])
            if record.is_file() and not samples:
                samples = json.loads(record.read_text(encoding="utf-8")).get(
                    "progress_samples", []
                )
        return {
            "runner": label,
            "status": result.get("status"),
            "diagnostic": result.get("error"),
            "wall_seconds": round(wall, 3),
            "sampled_peak_rss_bytes": peak,
            "saved_opens": result.get("saved_board_opens"),
            "preview_opens": result.get("best_preview_opens"),
            "final_sample": samples[-1] if samples else None,
            "last_check": result.get("drc")
            or result.get("last_result", {}).get("validation"),
        }

    def run(self) -> dict:
        """Measure every arm in order.

        Outputs:
            A routing_benchmark report with the shared input hashes, the budget, the
            toolchain and each arm's record. Its status is ``measured`` when every arm
            reported a preview open count and ``blocked`` otherwise.
        """
        results = [self._measure(*arm) for arm in self.arms]
        return {
            "schema_version": 1,
            "kind": "routing_benchmark",
            "status": "measured"
            if all(item["preview_opens"] is not None for item in results)
            else "blocked",
            "same_input_sha256": self.copy.fingerprint,
            **self.budget.as_dict(),
            "toolchain": self.native.status(),
            "results": results,
        }
