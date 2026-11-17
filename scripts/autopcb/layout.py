"""Delegate routing to the existing preview runner and guarded board publisher."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from autopcb.jobs import write_json
from autopcb.native import Native, engine_path
from autopcb.policy import routing_policy
from core.paths import ProjectStorage
from core.project import ENTRY_ENV, ProjectPaths


def _check_id(run_id: str) -> str:
    if not re.fullmatch("[0-9a-f]{32}", run_id):
        raise ValueError("Expected the 32-character ID of a saved routing preview")
    return run_id


def route_policy(native: Native, project: ProjectPaths, *, defaults: Path | None = None,
                 storage: ProjectStorage | None = None) -> dict:
    """Create a conservative starter policy on explicit request only."""
    from autopcb.jobs import inspect

    storage = storage or ProjectStorage(project.root)
    target = storage.config / "autopcb.json"
    if target.exists():
        raise FileExistsError(f"A routing policy already exists: {target}")
    existing = storage.config_file("autopcb.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    if existing.is_file():
        values = json.loads(existing.read_text(encoding="utf-8-sig"))
    else:
        geometry = inspect(native, project)["geometry"]
        path = project.settings
        settings = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
        definitions = settings.get("board", {}).get("design_settings", {}).get("rules", {})
        values = routing_policy(geometry["copper_layers"], defaults=defaults, minima=definitions)
    write_json(target, values)
    return {"schema_version": 1, "kind": "routing_policy", "status": "created",
            "entry": str(project.entry), "policy": str(target)}


def _invoke(native: Native, project: ProjectPaths, module: str, arguments: list[str],
            storage: ProjectStorage) -> dict:
    source_root = engine_path()
    env = {**os.environ, "PYTHONPATH": str(source_root),
           "KICAD_CLI": str(native.cli), ENTRY_ENV: str(project.entry), **storage.environment(project.entry)}
    with tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            [str(native.python), "-B", "-m", module, "--project", str(project.root), *arguments, "--json"],
            stdout=subprocess.PIPE, stderr=stderr, env=env, cwd=source_root,
            encoding="utf-8", errors="replace",
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
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
        message = detail.splitlines()[-1] if detail else f"Layout process exited {process.returncode}"
        try:
            message = json.loads(message).get("message", message)
        except (ValueError, AttributeError):
            pass
        raise RuntimeError(message)
    result = json.loads(output)
    if not isinstance(result, dict) or process.returncode not in (0, 2):
        raise RuntimeError(detail or "The routing worker returned an invalid result")
    return result


def preview(native: Native, project: ProjectPaths, *, rounds: int, seconds: int,
            memory_gib: float | None = None, resume: str | None = None,
            allow_placement: bool = False, renew: str | None = None, storage: ProjectStorage | None = None) -> dict:
    storage = storage or ProjectStorage(project.root)
    policy = storage.config_file("autopcb.json")
    if not policy.is_file():
        raise ValueError("Create and review the project routing policy before running layout.")
    args = ["run", "--max-rounds", str(rounds), "--seconds-per-batch", str(seconds)]
    if not allow_placement:
        args.append("--keep-placement")
    if memory_gib is not None:
        args.extend(("--memory-gib", str(memory_gib)))
    if resume is not None:
        args.extend(("--resume", _check_id(resume)))
    if renew is not None:
        args.extend(("--renew", _check_id(renew)))
    result = _invoke(native, project, "pcb.net_routing.autoroute_cli", args, storage)
    if not isinstance(result.get("run_id"), str):
        raise RuntimeError("The router did not retain a source-bound run")
    return {**result, "schema_version": 1, "kind": "layout_preview",
            "record": str(storage.results / "autoroute" / (result["run_id"] + ".json"))}


def compare(native: Native, project: ProjectPaths, run_id: str, *, storage: ProjectStorage | None = None) -> dict:
    _check_id(run_id)
    with tempfile.TemporaryDirectory(prefix="autopcb-compare-") as working:
        target = Path(working) / "comparison.json"
        native.worker("compare", project.entry, target, run_id, output=target, storage=storage)
        geometry = json.loads(target.read_text(encoding="utf-8"))
    return {"schema_version": 1, "kind": "layout_compare", "status": "inspected",
            "run_id": run_id, **geometry}


def apply(native: Native, project: ProjectPaths, run_id: str, *, storage: ProjectStorage | None = None) -> dict:
    _check_id(run_id)
    with tempfile.TemporaryDirectory(prefix="autopcb-apply-result-") as folder:
        output = Path(folder) / "applied.json"
        native.worker("apply", project.entry, output, run_id, output=output, timeout=300, storage=storage)
        result = json.loads(output.read_text(encoding="utf-8"))
    if result.get("status") != "saved_work_in_progress":
        raise RuntimeError(result.get("error", "The preview was not accepted by the guarded publisher"))
    return {**result, "schema_version": 1, "kind": "layout_apply", "production_approved": False}
