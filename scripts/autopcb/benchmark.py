"""Measure successive routing implementations on isolated copies of the same design."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from autopcb.jobs import isolated
from autopcb.native import Native, engine_path
from core.export_helpers import sha
from core.paths import ProjectStorage, asset_root
from core.project import ENTRY_ENV, ProjectPaths, SourceSnapshot


def benchmark(native: Native, project: ProjectPaths, baseline: Path, *, rounds: int, seconds: int,
              memory_gib: float, storage: ProjectStorage | None = None) -> dict:
    try:
        import psutil
    except ImportError as error:
        raise RuntimeError("The benchmark requires psutil. Install the AutoPCB benchmark extra.") from error

    current = engine_path()
    baseline = baseline.resolve()
    if not (baseline / "scripts/run.py").is_file():
        raise ValueError("Select an AutoPCB baseline checkout with scripts/run.py")
    if not 1 <= rounds <= 500 or not 1 <= seconds <= 1800:
        raise ValueError("Benchmark rounds or seconds are outside the supported range")
    storage = storage or ProjectStorage(project.root)
    caller = storage.legacy.relative_to(project.root)
    supplemental = {
        caller / "config/autopcb.json": storage.config_file("autopcb.json"),
        **{caller / "design" / name: storage.data_file(name)
           for name in ("POWER_LOAD_BUDGET.csv", "STACKUP_CURRENT.json")},
    }
    if not next(iter(supplemental.values())).is_file():
        raise ValueError("Both benchmark runners need the same reviewed routing policy")
    additional = {name: (path, sha(path)) for name, path in supplemental.items() if path.is_file()}
    snapshot = SourceSnapshot.capture(project)
    results = []
    for label, scripts in (("baseline", baseline / "scripts"), ("current", current)):
        with isolated(project, snapshot) as working:
            for name, (source, expected) in additional.items():
                target = working.root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                if sha(target) != expected:
                    raise RuntimeError("Benchmark policy changed while copying")
            # Preserve the asset root selection when the owner keeps its policy
            # under the AutoPCB subfolder.
            lookup = asset_root(working.root)
            with (tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors):
                env = {**os.environ, "PYTHONPATH": str(scripts), ENTRY_ENV: str(working.entry),
                       "KICAD_CLI": str(native.cli)}
                command = [str(native.python), "-B", "-m", "pcb.net_routing.autoroute_cli", "--project",
                           str(working.root), "run", "--keep-placement", "--workers", "1", "--max-rounds", str(rounds),
                           "--seconds-per-batch", str(seconds), "--memory-gib", str(memory_gib), "--json"]
                start = time.perf_counter()
                process = subprocess.Popen(command, cwd=scripts, env=env, stdout=output, stderr=errors)
                peak = 0
                try:
                    owner = psutil.Process(process.pid)
                    while process.poll() is None:
                        count = 0
                        for item in [owner, *owner.children(recursive=True)]:
                            try:
                                count += item.memory_info().rss
                            except psutil.Error:
                                pass
                        peak = max(peak, count)
                        time.sleep(.2)
                except psutil.NoSuchProcess:
                    process.wait()
                except BaseException:
                    process.terminate()
                    process.wait(timeout=10)
                    raise
                wall = time.perf_counter() - start
                output.seek(0)
                errors.seek(0)
                raw = output.read().decode("utf-8", "replace")
                try:
                    result = json.loads(raw)
                except ValueError:
                    result = {"status": "unavailable", "error": errors.read().decode("utf-8", "replace")[-2400:]}
            path = lookup / "results" / "autoroute" / f"{result.get('run_id', '')}.json"
            samples = result.get("progress_samples", [])
            if path.is_file() and not samples:
                samples = json.loads(path.read_text(encoding="utf-8")).get("progress_samples", [])
            results.append({"runner": label, "status": result.get("status"), "diagnostic": result.get("error"),
                            "wall_seconds": round(wall, 3), "sampled_peak_rss_bytes": peak,
                            "saved_opens": result.get("saved_board_opens"),
                            "preview_opens": result.get("best_preview_opens"),
                            "final_sample": samples[-1] if samples else None,
                            "last_check": result.get("drc") or result.get("last_result", {}).get("validation")})
    if any(sha(path) != expected for path, expected in additional.values()):
        raise RuntimeError("Benchmark policy or design evidence changed during the run")
    return {"schema_version": 1, "kind": "routing_benchmark",
            "status": "measured" if all(result["preview_opens"] is not None for result in results) else "blocked",
            "same_input_sha256": {**snapshot.hashes, **{name.as_posix(): digest
                                  for name, (_path, digest) in additional.items()}},
            "rounds": rounds, "seconds_per_batch": seconds, "memory_gib": memory_gib,
            "toolchain": native.status(), "results": results}
