"""Measure successive routing implementations on isolated copies of the same design."""

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
    """Run a baseline checkout and the current engine under one budget on identical copies."""

    def __init__(self, native: Native, project: ProjectPaths, baseline: Path, budget: Budget,
                 storage: ProjectStorage | None = None):
        baseline = baseline.resolve()
        if not (baseline / "scripts/run.py").is_file():
            raise ValueError("Select an AutoPCB baseline checkout with scripts/run.py")
        self.native, self.budget = native, budget
        self.runners = (("baseline", baseline / "scripts"), ("current", engine_path()))
        self.copy = PrivateCopy(project, storage or ProjectStorage(project.root))

    @staticmethod
    def _peak_rss(process: subprocess.Popen) -> int:
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
                time.sleep(.2)
        except psutil.NoSuchProcess:
            process.wait()
        return peak

    def _measure(self, label: str, scripts: Path) -> dict:
        with self.copy.open() as (working, _), tempfile.TemporaryFile() as output, \
                tempfile.TemporaryFile() as errors:
            env = {**os.environ, "PYTHONPATH": str(scripts), ENTRY_ENV: str(working.entry),
                   "KICAD_CLI": str(self.native.cli)}
            command = [str(self.native.python), "-B", "-m", "pcb.net_routing.autoroute_cli", "--project",
                       str(working.root), "run", "--keep-placement", "--workers", "1",
                       "--max-rounds", str(self.budget.rounds), "--seconds-per-batch", str(self.budget.seconds),
                       "--memory-gib", str(self.budget.memory_gib), "--json"]
            start = time.perf_counter()
            process = subprocess.Popen(command, cwd=scripts, env=env, stdout=output, stderr=errors)
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
                result = {"status": "unavailable", "error": errors.read().decode("utf-8", "replace")[-2400:]}
            record = asset_root(working.root) / "results" / "autoroute" / f"{result.get('run_id', '')}.json"
            samples = result.get("progress_samples", [])
            if record.is_file() and not samples:
                samples = json.loads(record.read_text(encoding="utf-8")).get("progress_samples", [])
        return {"runner": label, "status": result.get("status"), "diagnostic": result.get("error"),
                "wall_seconds": round(wall, 3), "sampled_peak_rss_bytes": peak,
                "saved_opens": result.get("saved_board_opens"), "preview_opens": result.get("best_preview_opens"),
                "final_sample": samples[-1] if samples else None,
                "last_check": result.get("drc") or result.get("last_result", {}).get("validation")}

    def run(self) -> dict:
        results = [self._measure(label, scripts) for label, scripts in self.runners]
        return {"schema_version": 1, "kind": "routing_benchmark",
                "status": "measured" if all(item["preview_opens"] is not None for item in results) else "blocked",
                "same_input_sha256": self.copy.fingerprint, **self.budget.as_dict(),
                "toolchain": self.native.status(), "results": results}
