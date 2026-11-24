"""sequence.py: try checkpoints sequentially on a disposable, source-bound board.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from core.project import native_path

from core.pcb_io import enabled_copper_layers

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from core.kicad_campaign import CampaignPlanner
from core.kicad_sexpr import parse
from core.paths import results_path
from pcb import checkpoints
from pcb.net_routing.geometry import aggregate
from pcb.net_routing.model import Site
from pcb.net_routing.native import Snapshot, pad_core, sha
from pcb.route_optimizer import (
    COPPER_LAYERS,
    GridRouter,
    PadCopper,
    Point,
    Route,
    Segment,
    Via,
    Weights,
)
from pcb.route_validation import RouteContract, validate_routes


class SequentialPreview:
    """Replans after each accepted temporary route and keeps a resumable preview
    report."""

    @staticmethod
    def write_report(
        path: Path, report: dict[str, Any], previous: bytes | None
    ) -> bytes:
        """Atomically update only the progress record created by this run."""
        content = (json.dumps(report, indent=2, allow_nan=False) + "\n").encode("utf-8")

        def check_previous() -> None:
            """Raise RuntimeError unless the report on disk is exactly the one this run
            last wrote, or absent when none was written."""
            if path.exists():
                if previous is None or path.read_bytes() != previous:
                    raise RuntimeError(
                        "Sequential preview report was changed externally"
                    )
            elif previous is not None:
                raise RuntimeError("Sequential preview report was removed externally")

        check_previous()
        fd, name = tempfile.mkstemp(prefix=".sequence-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            check_previous()
            os.replace(temporary, path)
            if path.read_bytes() != content:
                raise RuntimeError("Sequential preview report failed readback")
        finally:
            temporary.unlink(missing_ok=True)
        return content

    @staticmethod
    def parse_replay(
        replay: object,
    ) -> tuple[list[dict[str, Any]], list[RouteContract]]:
        """Check stored replay data and rebuild its route contracts.

        Inputs:
            replay: Object with the campaign changes and route contracts of a step.
        Outputs:
            The campaign changes and the route contracts. Raises ValueError when the
            data is malformed.
        """
        if not isinstance(replay, dict) or set(replay) != {"changes", "contracts"}:
            raise ValueError("Accepted preview step is missing replay data")
        changes, raw_contracts = replay["changes"], replay["contracts"]
        if (
            not isinstance(changes, list)
            or any(not isinstance(change, dict) for change in changes)
            or not isinstance(raw_contracts, list)
        ):
            raise ValueError("Invalid sequential preview replay data")
        contracts = []
        for item in raw_contracts:
            if not isinstance(item, dict) or set(item) != {
                "net",
                "start",
                "start_layer",
                "end",
                "end_layer",
                "removals",
            }:
                raise ValueError("Invalid sequential preview route contract")
            if (
                not isinstance(item["net"], str)
                or not item["net"]
                or item["start_layer"] not in COPPER_LAYERS
                or item["end_layer"] not in COPPER_LAYERS
                or not isinstance(item["removals"], list)
                or any(not isinstance(removal, dict) for removal in item["removals"])
            ):
                raise ValueError("Invalid sequential preview route contract")
            contracts.append(
                RouteContract(
                    item["net"],
                    Point.from_mm(item["start"]),
                    item["start_layer"],
                    Point.from_mm(item["end"]),
                    item["end_layer"],
                    tuple(item["removals"]),
                )
            )
        return changes, contracts

    @staticmethod
    def check_seed_geometry(
        shadow: Path, candidate: bytes, changes: list[dict[str, Any]]
    ) -> None:
        """Enforce the route-angle and staircase rules for newly inserted copper."""
        import pcbnew

        inserted: set[str] = set()
        for change in changes:
            if change.get("op") == "insert":
                form = parse(change["expression"])
                inserted.add(form.children("uuid")[0].atoms()[1].value)
        fd, name = tempfile.mkstemp(
            prefix="seed-geometry-", suffix=".kicad_pcb", dir=shadow
        )
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(candidate)
            board = pcbnew.LoadBoard(str(path))
            tracks = list(board.GetTracks())
            new_tracks = [t for t in tracks if t.m_Uuid.AsString() in inserted]
            if len(new_tracks) != len(inserted):
                raise ValueError("Seed copper could not be inspected")
            for net in {t.GetNetname() for t in new_tracks}:
                segments, vias, existing = [], [], []
                for track in tracks:
                    if track.GetNetname() != net:
                        continue
                    is_new = track.m_Uuid.AsString() in inserted
                    if isinstance(track, pcbnew.PCB_VIA):
                        if is_new:
                            pos = track.GetPosition()
                            vias.append(
                                Via(Point(int(pos.x), int(pos.y)), "F.Cu", "B.Cu")
                            )
                        continue
                    if isinstance(track, pcbnew.PCB_ARC):
                        raise ValueError(
                            "Seed geometry cannot yet be checked against same-net arcs"
                        )
                    a, b = track.GetStart(), track.GetEnd()
                    segment = Segment(
                        Point(int(a.x), int(a.y)),
                        Point(int(b.x), int(b.y)),
                        board.GetLayerName(track.GetLayer()),
                    )
                    if is_new:
                        GridRouter._direction(segment.start, segment.end)
                        segments.append(segment)
                    else:
                        existing.append(segment)
                pad_sites = frozenset(
                    Site(Point(int(p.GetPosition().x), int(p.GetPosition().y)), layer)
                    for p in board.GetPads()
                    if p.GetNetname() == net
                    for layer in enabled_copper_layers(board)
                    if p.IsOnLayer(board.GetLayerID(layer))
                )
                try:
                    aggregate(
                        (Route(tuple(segments), tuple(vias), (), 0, 0, 0, 0),),
                        existing=tuple(existing),
                        weights=Weights(),
                        layer_penalties={},
                        guide=(),
                        pad_sites=pad_sites,
                        pad_copper=PadCopper(
                            tuple(
                                box
                                for p in board.GetPads()
                                if p.GetNetname() == net
                                for box in pad_core(
                                    p,
                                    {
                                        layer: board.GetLayerID(layer)
                                        for layer in enabled_copper_layers(board)
                                    },
                                )
                            )
                        ),
                        forbid_staircase=True,
                    )
                except ValueError as error:
                    raise ValueError(
                        f"Seed routing violates geometry rules: {error}"
                    ) from error
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def stage_replay(
        shadow: Path,
        board_path: Path,
        replay: object,
        count: int,
        *,
        expected_hash: str | None = None,
        expected_after: int | None = None,
        allow_neutral: bool = False,
        check_style: bool = False,
    ) -> dict[str, Any]:
        """Revalidate stored copper changes and write the result to a temporary board.

        Inputs:
            shadow: Folder of the disposable snapshot.
            board_path: Temporary board that receives the candidate.
            replay: Stored changes and route contracts.
            count: Open connections the board must have before the step.
            expected_hash: Required candidate SHA-256, or None.
            expected_after: Required open connections after the step, or None.
            allow_neutral: Accept a step that leaves the open count unchanged.
            check_style: Enforce the route-angle and staircase rules on inserted copper
                first.
        Outputs:
            The native validation evidence. Raises RuntimeError when the step no longer
            validates or the written board fails readback.
        """
        changes, contracts = SequentialPreview.parse_replay(replay)
        if check_style:
            # Reject seed geometry precisely and cheaply before native validation.
            SequentialPreview.check_seed_geometry(
                shadow, CampaignPlanner(shadow).plan("pcb", changes).after, changes
            )
        evidence = validate_routes(board_path, sha(board_path), changes, contracts)
        if (
            evidence["status"] != "no_new_drc_violations"
            or evidence["baseline_unconnected"] != count
            or evidence["candidate_unconnected"] > count
            or (not allow_neutral and evidence["candidate_unconnected"] == count)
            or (
                expected_after is not None
                and evidence["candidate_unconnected"] != expected_after
            )
            or (
                expected_hash is not None
                and evidence["candidate_sha256"] != expected_hash
            )
        ):
            raise RuntimeError(
                "Sequential preview step no longer passes KiCad validation"
            )
        candidate = CampaignPlanner(shadow).plan(
            "pcb", [*changes, *evidence.pop("_zone_refill_changes", [])]
        )
        if candidate.result_sha256 != evidence["candidate_sha256"]:
            raise RuntimeError("Replayed preview differs from the validated candidate")
        board_path.write_bytes(candidate.after)
        if sha(board_path) != candidate.result_sha256:
            raise RuntimeError("Replayed temporary board failed readback")
        return evidence

    @staticmethod
    def replay_step(
        shadow: Path,
        board_path: Path,
        step: dict[str, Any],
        count: int,
        *,
        allow_neutral: bool = False,
    ) -> int:
        """Revalidate stored copper changes before reconstructing a preview step."""
        candidate_hash = step.get("candidate_sha256")
        after = step.get("after")
        if (
            step.get("source_sha256") != sha(board_path)
            or step.get("before") != count
            or not isinstance(candidate_hash, str)
            or len(candidate_hash) != 64
            or any(char not in "0123456789abcdef" for char in candidate_hash)
            or isinstance(after, bool)
            or not isinstance(after, int)
            or after < 0
        ):
            raise RuntimeError(
                "Sequential preview step does not match its source board"
            )
        evidence = SequentialPreview.stage_replay(
            shadow,
            board_path,
            step.get("replay"),
            count,
            expected_hash=step.get("candidate_sha256"),
            expected_after=step.get("after"),
            allow_neutral=allow_neutral,
            check_style=allow_neutral,
        )
        return evidence["candidate_unconnected"]

    def __call__(
        self,
        project: Path,
        config: Path,
        *,
        max_attempts: int | None = None,
        resume: Path | None = None,
        seed: Path | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Replan after each accepted temporary route; never alter the saved PCB."""
        project, config = project.resolve(), config.resolve()
        if not config.is_file() or not config.is_relative_to(project):
            raise ValueError("Configuration must be a file inside the project")
        if max_attempts is not None and (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer")
        if seed is not None and resume is not None:
            raise ValueError("A resumed preview already contains its seed")
        config_bytes = config.read_bytes()
        config_hash = hashlib.sha256(config_bytes).hexdigest()
        if sha(config) != config_hash:
            raise RuntimeError("Routing configuration changed while being read")
        source = native_path(project, ".kicad_pcb")
        seed_data = None
        seed_hash = None
        if seed is not None:
            seed = seed.resolve()
            if not seed.is_file() or not seed.is_relative_to(project):
                raise ValueError("A seed must be a file inside the project")
            seed_bytes = seed.read_bytes()
            seed_hash = hashlib.sha256(seed_bytes).hexdigest()
            if sha(seed) != seed_hash:
                raise RuntimeError("Seed changed while being read")
            seed_data = json.loads(seed_bytes.decode("utf-8-sig"))
            if (
                not isinstance(seed_data, dict)
                or set(seed_data) != {"schema", "source_sha256", "changes", "contracts"}
                or seed_data["schema"] != 1
            ):
                raise ValueError("Unsupported routing seed")
        prior = None
        prior_bytes = None
        if resume is not None:
            resume = resume.resolve()
            root = (results_path(project, "autopcb")).resolve()
            if (
                not resume.is_file()
                or not resume.is_relative_to(root)
                or resume.name != "sequence_preview.json"
            ):
                raise ValueError("Resume must name a saved sequential preview report")
            prior_bytes = resume.read_bytes()
            if sha(resume) != hashlib.sha256(prior_bytes).hexdigest():
                raise RuntimeError("Sequential preview report changed while being read")
            prior = json.loads(prior_bytes.decode("utf-8-sig"))
            if (
                not isinstance(prior, dict)
                or prior.get("schema") != 1
                or prior.get("kind") != "sequential_preview"
                or prior.get("applied") is not False
            ):
                raise ValueError("Unsupported sequential preview report")
        steps: list[dict[str, Any]] = []
        accepted = 0
        with Snapshot(source) as snapshot:
            tool_hashes = checkpoints._tools(snapshot)
            shadow = snapshot.directory
            shadow_config = shadow / config.relative_to(project)
            shadow_config.parent.mkdir(parents=True, exist_ok=True)
            shadow_config.write_bytes(config_bytes)

            def check_inputs() -> None:
                """Raise RuntimeError when the snapshot, configuration, tooling or seed
                changed during the preview."""
                snapshot.check()
                if (
                    sha(config) != config_hash
                    or checkpoints._tools(snapshot) != tool_hashes
                    or (seed is not None and sha(seed) != seed_hash)
                ):
                    raise RuntimeError(
                        "Routing configuration or tooling changed during preview"
                    )

            first_count = None
            current_count = None
            report_path = None
            plan_path = None
            report_bytes = None
            skipped: dict[str, Any] = {}
            termination_reason = None
            seed_step: dict[str, Any] | None = None

            def publish(status: str, error: str | None = None) -> dict[str, Any]:
                """Write the progress report and return its summary.

                Inputs:
                    status: Report status such as preview_running.
                    error: Error text to record, or None.
                Outputs:
                    The report without input and tool hashes or replay data. Raises
                    RuntimeError
                    when the output location is not known yet.
                """
                nonlocal report_bytes
                if report_path is None or plan_path is None:
                    raise RuntimeError("Sequential preview has no output location")
                report: dict[str, Any] = {
                    "schema": 1,
                    "kind": "sequential_preview",
                    "status": status,
                    "termination_reason": termination_reason,
                    "source_sha256": snapshot.fingerprints[snapshot.source],
                    "config_sha256": config_hash,
                    "config_path": config.relative_to(project).as_posix(),
                    "input_sha256": checkpoints._inputs(snapshot),
                    "tool_sha256": tool_hashes,
                    "initial_unconnected": first_count,
                    "final_unconnected": current_count,
                    "attempted": len(steps),
                    "accepted": accepted,
                    "steps": steps,
                    "skipped": skipped,
                    "plan": str(plan_path),
                    "report": str(report_path),
                    "applied": False,
                    "note": "Temporary preview only; electrical and physical checks remain required.",
                }
                if resume is not None and prior_bytes is not None:
                    report["resumed_from"] = str(resume)
                    report["resumed_report_sha256"] = hashlib.sha256(
                        prior_bytes
                    ).hexdigest()
                if seed_step is not None:
                    report["seed"] = seed_step
                if error is not None:
                    report["error"] = error
                report_bytes = SequentialPreview.write_report(
                    report_path, report, report_bytes
                )
                summary = {
                    key: value
                    for key, value in report.items()
                    if key not in {"input_sha256", "tool_sha256"}
                }
                summary["steps"] = [
                    {key: value for key, value in item.items() if key != "replay"}
                    for item in steps
                ]
                if seed_step is not None:
                    summary["seed"] = {
                        key: value
                        for key, value in seed_step.items()
                        if key != "replay"
                    }
                return summary

            try:
                status = "preview_completed"
                check_inputs()
                initial_plan, initial_items = checkpoints.create_plan(
                    shadow, shadow_config
                )
                first_count = initial_plan["baseline_unconnected"]
                current_count = first_count
                if seed_data is not None:
                    if (
                        seed_data["source_sha256"]
                        != snapshot.fingerprints[snapshot.source]
                    ):
                        raise RuntimeError("Seed does not match the saved PCB")
                    if progress is not None:
                        progress("Checking the enabling routing change with KiCad.")
                    replay = {
                        "changes": seed_data["changes"],
                        "contracts": seed_data["contracts"],
                    }
                    evidence = SequentialPreview.stage_replay(
                        shadow,
                        snapshot.board_path,
                        replay,
                        current_count,
                        allow_neutral=True,
                        check_style=True,
                    )
                    seed_step = {
                        "before": current_count,
                        "after": evidence["candidate_unconnected"],
                        "source_sha256": snapshot.fingerprints[snapshot.source],
                        "candidate_sha256": evidence["candidate_sha256"],
                        "seed_sha256": seed_hash,
                        "replay": replay,
                        "electrical": "not_assessed",
                        "validation": {
                            key: evidence[key]
                            for key in (
                                "status",
                                "baseline_violations",
                                "candidate_violations",
                                "new_violations",
                                "broken_existing_connections",
                            )
                        },
                    }
                    current_count = evidence["candidate_unconnected"]
                    check_inputs()
                if prior is not None:
                    if (
                        prior.get("source_sha256")
                        != snapshot.fingerprints[snapshot.source]
                        or prior.get("config_sha256") != config_hash
                        or prior.get("config_path")
                        != config.relative_to(project).as_posix()
                        or prior.get("input_sha256") != checkpoints._inputs(snapshot)
                        or prior.get("tool_sha256") != tool_hashes
                        or prior.get("initial_unconnected") != first_count
                        or not isinstance(prior.get("steps"), list)
                    ):
                        raise RuntimeError(
                            "Saved sequential preview inputs or tools changed"
                        )
                    if prior.get("seed") is not None:
                        seed_step = prior["seed"]
                        if (
                            not isinstance(seed_step, dict)
                            or seed_step.get("electrical") != "not_assessed"
                        ):
                            raise ValueError("Invalid saved sequential preview seed")
                        if progress is not None:
                            progress("Rechecking the saved enabling change with KiCad.")
                        current_count = SequentialPreview.replay_step(
                            shadow,
                            snapshot.board_path,
                            seed_step,
                            current_count,
                            allow_neutral=True,
                        )
                        check_inputs()
                    for index, item in enumerate(prior["steps"], 1):
                        if not isinstance(item, dict) or not isinstance(
                            item.get("accepted"), bool
                        ):
                            raise ValueError("Invalid saved sequential preview step")
                        if item["accepted"]:
                            if item.get("status") != "validated_candidate":
                                raise ValueError(
                                    "An accepted preview must have been validated"
                                )
                            if progress is not None:
                                progress(
                                    f"Rechecking saved preview step {index} with KiCad."
                                )
                            current_count = SequentialPreview.replay_step(
                                shadow, snapshot.board_path, item, current_count
                            )
                            accepted += 1
                        steps.append(item)
                        check_inputs()
                        if (
                            resume is None
                            or sha(resume) != hashlib.sha256(prior_bytes).hexdigest()
                        ):
                            raise RuntimeError(
                                "Saved sequential preview changed during replay"
                            )
                    if (
                        prior.get("accepted") != accepted
                        or prior.get("attempted") != len(steps)
                        or prior.get("final_unconnected") != current_count
                    ):
                        raise RuntimeError(
                            "Saved sequential preview progress is inconsistent"
                        )
                    if (
                        resume is None
                        or sha(resume) != hashlib.sha256(prior_bytes).hexdigest()
                    ):
                        raise RuntimeError(
                            "Saved sequential preview changed during replay"
                        )
                plan_path = checkpoints.save_plan(project, initial_plan, initial_items)
                report_path = plan_path.parent / "sequence_preview.json"
                publish("preview_running")
                pending = (
                    (initial_plan, initial_items)
                    if prior is None and seed_step is None
                    else None
                )
                new_attempts = 0
                while True:
                    check_inputs()
                    if pending is None:
                        plan, items = checkpoints.create_plan(shadow, shadow_config)
                    else:
                        plan, items = pending
                        pending = None
                    skipped = plan["skipped"]
                    if plan["baseline_unconnected"] != current_count:
                        raise RuntimeError(
                            "Temporary board connectivity changed unexpectedly"
                        )
                    if not items:
                        termination_reason = "no_configured_checkpoints"
                        break
                    if max_attempts is not None and new_attempts >= max_attempts:
                        status = "preview_paused"
                        termination_reason = "attempt_limit"
                        break
                    destination = checkpoints.save_plan(shadow, plan, items)
                    advanced = False
                    for item in items:
                        if max_attempts is not None and new_attempts >= max_attempts:
                            status = "preview_paused"
                            termination_reason = "attempt_limit"
                            break
                        path = (
                            destination.parent
                            / "checkpoints"
                            / f"{item['number']:04d}.json"
                        )
                        if progress is not None:
                            progress(
                                f"Previewing {item['net']} on the current temporary board."
                            )
                        result = checkpoints.attempt(
                            shadow,
                            path,
                            include_preview_changes=True,
                            progress=progress,
                        )
                        step: dict[str, Any] = {
                            "net": item["net"],
                            "status": result["status"],
                            "accepted": False,
                            "search": result.get("search"),
                            "completed_at": datetime.now()
                            .astimezone()
                            .isoformat(timespec="seconds"),
                        }
                        if result["status"] == "validated_candidate":
                            evidence = result["validation"]
                            if (
                                evidence["baseline_unconnected"] != current_count
                                or evidence["candidate_unconnected"] >= current_count
                            ):
                                raise RuntimeError(
                                    "Candidate was checked against a different board state"
                                )
                            step_source_sha256 = sha(snapshot.board_path)
                            candidate = CampaignPlanner(shadow).plan(
                                "pcb", result["_preview_changes"]
                            )
                            if candidate.result_sha256 != evidence["candidate_sha256"]:
                                raise RuntimeError(
                                    "Temporary candidate differs from its validation"
                                )
                            check_inputs()
                            snapshot.board_path.write_bytes(candidate.after)
                            if sha(snapshot.board_path) != candidate.result_sha256:
                                raise RuntimeError(
                                    "Temporary candidate failed its readback check"
                                )
                            current_count = evidence["candidate_unconnected"]
                            accepted += 1
                            step.update(
                                accepted=True,
                                before=evidence["baseline_unconnected"],
                                after=current_count,
                                source_sha256=step_source_sha256,
                                candidate_sha256=candidate.result_sha256,
                                replay=result["_preview_replay"],
                                validation=evidence,
                                electrical=result["electrical"]["status"],
                                length_mm=result["route"]["length_mm"],
                                bends=result["route"]["bends"],
                                vias=result["route"]["via_count"],
                            )
                            advanced = True
                        steps.append(step)
                        new_attempts += 1
                        check_inputs()
                        publish("preview_running")
                        if progress is not None:
                            progress(
                                f"{item['net']}: {result['status']}; "
                                f"temporary board has {current_count} open connections."
                            )
                        if advanced:
                            break
                    if not advanced:
                        if termination_reason is None:
                            termination_reason = "no_validated_candidate"
                        break
                check_inputs()
                return publish(status)
            except KeyboardInterrupt:
                termination_reason = "interrupted"
                if report_path is not None:
                    publish("preview_interrupted")
                raise
            except Exception as exc:
                termination_reason = "error"
                if report_path is not None:
                    try:
                        publish("preview_error", str(exc))
                    except (OSError, RuntimeError):
                        pass
                raise


preview_sequence = SequentialPreview()
