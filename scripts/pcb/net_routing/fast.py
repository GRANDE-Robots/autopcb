"""fast.py: run bounded external-router batches and retain only checked previews.

Each batch routes a private copy with Freerouting, fastroute or KRT, filters the
proposal with native checks, and retains it as a new private preview only when it
independently improves the board. The live PCB is never changed.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from core.project import native_path

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from core.toolchain import kicad_cli

from core.paths import asset_root, config_path, results_path
from core.pcb_io import quiet_duplicate_image_handlers
from pcb.checkpoints import read_config
from pcb.freeroute_delta import (
    filter_proposal,
    native_item_nets,
    router_class,
    unconnected_by_net,
)
from pcb.freeroute_preview import (
    _routing_only_session_file,
    _dsn_scope,
    _positions,
    _restore_bottom_rounding,
    _select_java,
    preview,
)
from pcb.net_routing.controller import decode_checkpoint
from pcb.net_routing.external import retain_candidate
from pcb.net_routing.isolation import run_native
from pcb.net_routing.native import Snapshot
from pcb.net_routing.protection import ProtectedState, unqualified_working_nets
from pcb.net_routing.runner import (
    _check_sources,
    _exclusive,
    _progress_elapsed,
    _publish_progress,
    _run_id,
    _save,
    _source_hashes,
)
from core.toolchain import RouterCache


MAX_REWORK_ATTEMPTS = 128


class FocusScope:
    """Chooses which nets a batch routes and which it defers."""

    @staticmethod
    def focus_open_nets(
        members: set[str],
        classes: dict[str, str],
        opens: dict[str, int],
        focus_classes: tuple[str, ...],
        blocked: set[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Choose only attributable unfinished nets in explicitly requested classes.

        Inputs:
            members: Net names present in the router export.
            classes: Native net class of each net.
            opens: Open connection count of each net.
            focus_classes: Requested net classes; at least one.
            blocked: Net names that must not be routed.
        Outputs:
            The sorted target nets (unfinished, in a requested class, not blocked) and the
            sorted remaining export members to defer. Raises ValueError for no requested
            class, unidentified or invalid open counts, a class that is absent from the
            board or a target missing from the export.
        """
        requested = set(RouterNames.class_names(focus_classes))
        if not requested:
            raise ValueError("At least one focus class is required")
        if opens.get("<unknown>", 0):
            raise ValueError("Cannot focus a batch with unidentified open connections")
        if any(
            isinstance(count, bool) or not isinstance(count, int) or count < 0
            for count in opens.values()
        ):
            raise ValueError("Open-connection counts must be nonnegative integers")
        if any(name not in classes for name, count in opens.items() if count):
            raise ValueError("An open net has no identifiable native class")
        native_classes = {name: router_class(value) for name, value in classes.items()}
        if not requested <= set(native_classes.values()):
            raise ValueError("A requested focus class is not present on the board")
        targets = {
            name
            for name, count in opens.items()
            if count and native_classes[name] in requested and name not in blocked
        }
        if targets - members:
            raise ValueError("An unfinished focused net is absent from the DSN export")
        return tuple(sorted(targets)), tuple(sorted(members - targets))

    @staticmethod
    def protected_export_members(
        board, members: set[str], excluded: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Defer unqualified nets that can actually appear in a router export.

        Inputs:
            board: The loaded board.
            members: Net names present in the router export.
            excluded: Net names the routing policy excludes.
        Outputs:
            The sorted unqualified working nets that are export members.
        """
        return tuple(sorted(unqualified_working_nets(board, excluded) & members))

    @staticmethod
    def _exported_members(board, directory: Path) -> set[str]:
        """List the nets that the router export contains.

        Inputs:
            board: The loaded board.
            directory: Scratch directory for the exported DSN file.
        Outputs:
            The net names in the exported design's net classes. Raises RuntimeError when
            KiCad cannot export the design.
        """
        import pcbnew

        design = directory / "scope.dsn"
        if not pcbnew.ExportSpecctraDSN(board, str(design)):
            raise RuntimeError("KiCad could not inspect the router net classes")
        _, network = _dsn_scope(design.read_text(encoding="utf-8"), "network")
        return {
            atom.value
            for item in network.children("class")
            for atom in item.atoms()[2:]
        }

    @staticmethod
    def automatic_deferred(project: Path, excluded: tuple[str, ...]) -> tuple[str, ...]:
        """Find the protected nets of a private board that a router export would contain.

        Inputs:
            project: Directory of the private board.
            excluded: Net names the routing policy excludes.
        Outputs:
            The sorted protected nets that appear in the export. Raises RuntimeError when
            KiCad cannot read the board or export it.
        """
        import pcbnew

        board = pcbnew.LoadBoard(str(native_path(project, ".kicad_pcb")))
        if board is None:
            raise RuntimeError("KiCad could not read the private preview")
        with tempfile.TemporaryDirectory(prefix="autopcb-safe-scope-") as directory:
            members = FocusScope._exported_members(board, Path(directory))
            return FocusScope.protected_export_members(board, members, excluded)

    @staticmethod
    def focus_deferred(
        project: Path,
        state: dict,
        classes: tuple[str, ...],
        excluded: tuple[str, ...],
        excluded_classes: tuple[str, ...] = (),
    ) -> tuple:
        """Inspect a private board with KiCad before choosing a temporary focused batch.

        Inputs:
            project: Directory of the private board.
            state: The preview run state, with its blocked nets.
            classes: Net classes to focus on.
            excluded: Net names the routing policy excludes.
            excluded_classes: Net classes to leave out of the batch.
        Outputs:
            The target nets and the export members to defer, as from focus_open_nets.
            Raises RuntimeError when the KiCad CLI or board is unavailable and ValueError
            for an invalid blocked-net inventory.
        """
        import pcbnew

        from pcb.route_validation import _drc

        board_path = native_path(project, ".kicad_pcb")
        cli = kicad_cli()
        if not cli.is_file():
            raise RuntimeError("KiCad CLI is required for focused net selection")
        with tempfile.TemporaryDirectory(prefix="autopcb-focus-") as directory:
            scratch = Path(directory)
            report = _drc(
                cli,
                board_path,
                scratch / "focus-drc.json",
                save_refill=True,
                schematic_parity=True,
            )
            board = pcbnew.LoadBoard(str(board_path))
            if board is None:
                raise RuntimeError("KiCad could not read the focused preview")
            opens = unconnected_by_net(report, native_item_nets(board))
            members = FocusScope._exported_members(board, scratch)
            native_classes = {
                str(name): str(board.FindNet(str(name)).GetNetClassName())
                for name in board.GetNetsByName().keys()
                if str(name)
            }
            protected_nets = set(
                FocusScope.protected_export_members(board, members, excluded)
            )
        blocked = state.get("blocked_nets", {})
        if not isinstance(blocked, dict):
            raise ValueError(
                "The retained preview has an invalid blocked-net inventory"
            )
        class_exclusions = {
            name
            for name, value in native_classes.items()
            if router_class(value) in set(excluded_classes)
        }
        return FocusScope.focus_open_nets(
            members,
            native_classes,
            opens,
            classes,
            set(blocked) | set(excluded) | protected_nets | class_exclusions,
        )

    @staticmethod
    def eligible_signal_classes(project: Path) -> tuple[str, ...]:
        """Discover ordinary signal classes without treating protected nets as eligible.

        Inputs:
            project: Directory of the private board.
        Outputs:
            The sorted router classes of the nets that are not protected. Raises
            RuntimeError when KiCad cannot read the board.
        """
        import pcbnew

        board = pcbnew.LoadBoard(str(native_path(project, ".kicad_pcb")))
        if board is None:
            raise RuntimeError("KiCad could not read the private preview")
        protected = unqualified_working_nets(board)
        return tuple(
            sorted(
                {
                    router_class(str(board.FindNet(str(name)).GetNetClassName()))
                    for name in board.GetNetsByName()
                    if str(name) and str(name) not in protected
                }
            )
        )


class RoutingRoundtrip:
    """Measures the router's exporter and importer with routing disabled."""

    @staticmethod
    def roundtrip(
        input_board: Path,
        trial: Path,
        jar: Path,
        java: str,
        timeout: int,
        engine: str = "Freerouting",
    ) -> Path:
        """Measure the same exporter/importer with routing disabled.

        Inputs:
            input_board: Board file the comparison is imported onto.
            trial: Scratch directory that holds candidate.dsn and receives the outputs.
            jar: Router executable or JAR.
            java: Java executable or auto, for Freerouting.
            timeout: Seconds the router may run.
            engine: Freerouting or fastroute.
        Outputs:
            The path of the imported board file. Raises ValueError for another engine and
            RuntimeError when the router fails or times out or KiCad cannot import or save.
        """
        import pcbnew

        home = trial / "roundtrip-home"
        home.mkdir()
        env = os.environ.copy()
        for name in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            directory = home / name.lower()
            directory.mkdir()
            env[name] = str(directory)
        session = trial / "roundtrip.ses"
        if engine == "Freerouting":
            command = [
                _select_java(java, jar),
                f"-Duser.home={home}",
                "-jar",
                str(jar),
                "-de",
                str(trial / "candidate.dsn"),
                "-do",
                str(session),
                f"--user_data_path={home}",
                "--gui.enabled=false",
                "--router.autorouter.enabled=false",
                "--router.optimizer.enabled=false",
                "--router.fanout.enabled=false",
                "-da",
            ]
        elif engine == "fastroute":
            command = [
                str(jar),
                "-de",
                str(trial / "candidate.dsn"),
                "-do",
                str(session),
                "-mp",
                "1",
                "--multi-start=1",
                "--router.autorouter.enabled=false",
                "--router.optimizer.enabled=false",
                "--router.fanout.enabled=false",
                "--router.autorouter.max_threads=1",
            ]
        else:
            raise ValueError("Unsupported routing engine")
        with (trial / "roundtrip.log").open("w", encoding="utf-8") as log:
            try:
                result = subprocess.run(
                    command,
                    cwd=trial,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("The no-routing comparison timed out") from error
        if result.returncode or not session.is_file():
            raise RuntimeError(
                f"No-routing comparison failed; inspect {trial / 'roundtrip.log'}"
            )
        board = pcbnew.LoadBoard(str(input_board))
        if board is None:
            raise RuntimeError("KiCad could not reopen the retained preview")
        positions = _positions(board)
        if not pcbnew.ImportSpecctraSES(
            board, str(_routing_only_session_file(session))
        ):
            raise RuntimeError("KiCad could not import the no-routing comparison")
        _restore_bottom_rounding(board, positions)
        output = trial / "roundtrip.kicad_pcb"
        if not pcbnew.SaveBoard(str(output), board):
            raise RuntimeError("KiCad could not save the no-routing comparison")
        return output


class FilterProgress:
    """Reports a proposal filter's events as readable progress messages."""

    def __init__(self, stage) -> None:
        """Remember the callable that receives the messages.

        Inputs:
            stage: Callable that takes one message string.
        """
        self.stage = stage

    def __call__(self, event: dict) -> None:
        """Turn one filter event into a progress message.

        Inputs:
            event: A filter event dictionary whose event key names its kind.
        """
        stage = self.stage
        if event["event"] == "proposal":
            classes = (
                ", ".join(
                    f"{name}: {count}" for name, count in event["classes"].items()
                )
                or "none"
            )
            stage(f"Proposed copper by class: {classes}")
        elif event["event"] == "repair":
            result = (
                "checking candidate"
                if event["status"] == "candidate"
                else "no path in bounded search"
            )
            stage(f"  Local repair for {event['net']}: {result}")
        elif event["event"] == "repair_retry":
            step = f", {event['step_mm']:g} mm grid" if "step_mm" in event else ""
            stage(
                f"  Retrying {event['net']} ({event['max_expansions']:,} expansions{step})"
            )
        elif event["event"] == "prune":
            stage(
                f"  Removed {event['items']} additional traces in rejected dangling remainders"
            )
        elif event["event"] == "local_simplification":
            stage(
                f"  Simplified local proposal windows by {event['removed_segments']} segments"
            )
        elif event["event"] == "rule_width_materialization":
            stage(
                f"  Restored saved widths on {event['changed_traces']} proposed traces"
            )
        elif event["event"] == "off_angle_repair":
            stage(
                f"  Replaced {event['segments']} off-angle traces with 45-degree candidates"
            )
        elif event["event"] == "native_violation_retry":
            stage(
                "  Retrying without nets implicated in native violations: "
                + ", ".join(event["nets"])
            )
        elif event["event"] == "style_blocker_retry":
            stage(
                "  Retrying without nets with ambiguous geometry: "
                + ", ".join(event["nets"])
            )
        elif event["event"] == "repair_order":
            stage("  Partial repair stalled; retrying shorter sections first")
        elif event["event"] == "prune_net":
            nets = ", ".join(event["nets"])
            action = (
                "reverted to preserve connectivity"
                if event["status"] == "rolled_back"
                else "testing removal"
            )
            stage(f"  Dangling {event.get('kind', 'net')} ({nets}): {action}")
        elif event["event"] == "fallback":
            stage(
                "  Restoring router paths after unsafe shortcuts: "
                + ", ".join(event["nets"])
            )
        elif event["event"] == "salvage":
            stage(
                "  Rechecking the remaining batch without conflicting nets: "
                + ", ".join(event["nets"])
            )
        elif event["event"] == "connectivity_retry":
            stage(
                "  Retesting without part of the batch to preserve existing connections: "
                + ", ".join(event["nets"])
            )
        elif event["event"] == "reference_blocked":
            stage(
                "  Deferred nets crossing local high-speed reference regions: "
                + ", ".join(event["nets"])
            )
        elif event["event"] == "island_regression":
            stage(
                "  Removed proposed nets with new isolated copper: "
                + ", ".join(event["nets"])
            )
        elif event["event"] == "via_alignment":
            stage(
                "  Aligned proposed tracks to existing via coordinates: "
                + ", ".join(event["nets"])
            )
        elif event["event"] == "timeout":
            stage("  Cleanup time budget reached; checking the remaining clean subset")
        else:
            affected = ", ".join(
                f"{name}: {count}" for name, count in event["affected_nets"].items()
            )
            detail = f" ({affected})" if affected else ""
            stage(
                f"Filter pass {event['pass']}: {event['opens']} opens; "
                f"discarded {event['rejected_items']} items{detail}"
            )
            remaining = event.get("remaining_by_net", {})
            if remaining:
                stage(
                    "  Candidate open-pair counts: "
                    + ", ".join(f"{name}: {count}" for name, count in remaining.items())
                )
            if event["drc_reasons"] or event["style_items"]:
                causes = [
                    f"{name}: {count}" for name, count in event["drc_reasons"].items()
                ]
                if event["style_items"]:
                    causes.append(f"geometry: {event['style_items']}")
                stage("  Causes: " + ", ".join(causes))


class FastBatch:
    """Runs one external-router batch and retains an improved preview."""

    @staticmethod
    def _retain_in_worker(
        project: Path,
        run_id: str,
        candidate: Path,
        seconds: float,
        expected: bytes,
        engine: str,
        cache: dict,
        *,
        progress=None,
        router_fingerprint: str | None = None,
    ) -> tuple[dict, dict]:
        """Release KiCad boards after validation while returning the source-bound cache.

        Inputs:
            project: Project directory.
            run_id: Source preview run.
            candidate: Filtered candidate board file.
            seconds: Routing seconds spent on the candidate.
            expected: Bytes the source preview state must still have.
            engine: Router that produced the candidate.
            cache: Cache of the saved board's checks.
            progress: Optional callable that receives status messages.
            router_fingerprint: Tool fingerprint, required for krt.
        Outputs:
            The retention record and the updated cache.
        """
        result = retain_candidate(
            project,
            run_id,
            candidate,
            routing_seconds=seconds,
            progress=progress,
            expected_previous=expected,
            engine=engine,
            router_fingerprint=router_fingerprint,
            saved_checks_cache=cache,
        )
        return result, cache

    @staticmethod
    def run_one(
        project: Path,
        run_id: str,
        *,
        items: int,
        timeout: int,
        jar: Path,
        java: str,
        ignore_net_classes: tuple[str, ...] = (),
        progress=None,
        ignore_nets: tuple[str, ...] = (),
        cleanup_seconds: float = 60.0,
        engine: str = "Freerouting",
        starts: int = 1,
        saved_checks_cache: dict | None = None,
        focus_classes: tuple[str, ...] = (),
        optimizer: bool = False,
        router_python: str = "auto",
        router_grid_mm: float | None = None,
        router_full: bool = False,
        ground_contact_margin_mm: float = 0.5,
        search_pad_neckdown: bool = False,
    ) -> dict:
        """Try one external batch; only a checked improvement gets a new checkpoint.

        Inputs:
            project: Project directory.
            run_id: Source private preview run.
            items: Attempt limit per batch; for krt a net count; 0 selects all eligible.
            timeout: Router time limit in seconds.
            jar: Router executable, JAR or pinned krt directory.
            java: Java executable or auto.
            ignore_net_classes: Net classes deferred in this batch.
            progress: Optional callable that receives status messages.
            ignore_nets: Nets deferred in this batch.
            cleanup_seconds: Cleanup time limit in seconds.
            engine: Freerouting, fastroute or krt.
            starts: Number of routing orders to try (fastroute only).
            saved_checks_cache: Optional cache of the saved board's checks.
            focus_classes: Route only unfinished nets of these classes.
            optimizer: Run the router's optimizer before validation.
            router_python: Interpreter for krt.
            router_grid_mm: krt search grid in mm.
            router_full: Run krt recovery and finishing passes.
            ground_contact_margin_mm: Extra search-only ground-pad margin in mm.
            search_pad_neckdown: Propose smaller pad exits.
        Outputs:
            A dictionary: the retained preview record with the batch time and open
            reduction, or a status such as no_focused_nets, no_safe_proposal or the filter's
            own status. Raises ValueError for invalid options and RuntimeError when
            protected items or the source preview change.
        """
        if not isinstance(search_pad_neckdown, bool):
            raise ValueError("search_pad_neckdown must be boolean")
        if search_pad_neckdown and engine == "krt":
            raise ValueError("Pad-neckdown proposals require fastroute or Freerouting")
        started = time.monotonic()

        def stage(message: str) -> None:
            """Report a status message when a progress callable was given."""
            if progress:
                progress(message)

        project = project.resolve()
        run_id = _run_id(run_id)
        ignore_net_classes = RouterNames.class_names(ignore_net_classes)
        assets = asset_root(project)
        policy_path = config_path(project, "autopcb.json")
        policy, policy_hash = read_config(policy_path)
        ignore_nets = tuple(
            sorted(
                set(RouterNames.net_names(ignore_nets))
                | set(policy.get("exclude_nets", ()))
            )
        )
        state_path = results_path(project, "autoroute") / f"{run_id}.json"
        state_bytes = state_path.read_bytes()
        state = json.loads(state_bytes)
        if state.get("schema") != 1 or state.get("run_id") != run_id:
            raise ValueError("Invalid source preview")
        with (
            Snapshot(native_path(project, ".kicad_pcb")) as snapshot,
            tempfile.TemporaryDirectory(
                prefix="autopcb-external-router-"
            ) as router_folder,
        ):
            inputs = _source_hashes(snapshot, project, assets)
            candidate = decode_checkpoint(
                state["checkpoint"],
                inputs,
                snapshot.tool_hashes,
                policy_hash,
                allow_tool_changes=True,
            )
            schematic = (native_path(snapshot.directory, ".kicad_sch")).read_bytes()
            protected = ProtectedState.capture(snapshot.original, schematic)
            if protected.differences(candidate, schematic):
                raise RuntimeError(
                    "The retained preview changed protected design items"
                )
            snapshot.board_path.write_bytes(candidate)
            actual_focus = focus_classes
            if engine == "krt" and not actual_focus:
                actual_focus = run_native(
                    FocusScope.eligible_signal_classes, snapshot.directory
                )
                if not actual_focus:
                    return {
                        "status": "no_focused_nets",
                        "run_id": run_id,
                        "preview_opens": state["best_preview_opens"],
                    }
            if actual_focus:
                stage("Identifying unfinished nets in the requested classes")
                targets, focused_deferred = run_native(
                    FocusScope.focus_deferred,
                    snapshot.directory,
                    state,
                    actual_focus,
                    ignore_nets,
                    excluded_classes=ignore_net_classes if engine == "krt" else (),
                    heartbeat=lambda: stage("Still identifying unfinished nets"),
                )
                if protected.differences(snapshot.board_path.read_bytes(), schematic):
                    raise RuntimeError(
                        "Preparing the focused batch changed protected design items"
                    )
                if not targets:
                    snapshot.check()
                    return {
                        "status": "no_focused_nets",
                        "run_id": run_id,
                        "preview_opens": state["best_preview_opens"],
                    }
                ignore_nets = focused_deferred
                if engine == "krt" and items and len(targets) > items:
                    ignore_nets = tuple(sorted(set(ignore_nets) | set(targets[items:])))
                    targets = targets[:items]
                stage(
                    f"Focusing on {len(targets)} unfinished nets in {', '.join(actual_focus)}"
                )
            else:
                ignore_nets = run_native(
                    FocusScope.automatic_deferred, snapshot.directory, ignore_nets
                )
            deferred = (
                f"; not routing classes {', '.join(ignore_net_classes)} in this batch"
                if ignore_net_classes
                else ""
            )
            if ignore_nets:
                deferred += f"; {len(ignore_nets)} protected or deferred nets omitted"
            target = (
                f"{len(targets)} eligible nets for up to {timeout} seconds"
                if engine == "krt"
                else f"up to {items} items"
                if items
                else f"all eligible items for up to {max(1, timeout - 10)} seconds"
            )
            stage(f"Routing {target} on a private copy{deferred}")
            if engine == "krt":
                from pcb.net_routing import krt_adapter

                raw = run_native(
                    krt_adapter.KrtPreview.preview,
                    snapshot.directory,
                    jar,
                    router_python,
                    targets,
                    timeout,
                    grid_mm=router_grid_mm,
                    quick=not router_full,
                    salvage_strict=True,
                    reserve_references=True,
                    ground_contact_margin_mm=ground_contact_margin_mm,
                    heartbeat=lambda: stage("Still searching on the private board"),
                )
                if raw.get("search_reservations"):
                    counts = raw["search_reservations"]
                    stage(
                        f"Search used {counts['reference_areas']} local reference regions and "
                        f"{counts['ground_contact_areas']} ground-contact regions"
                    )
                if raw.get("status") == "search_timeout":
                    snapshot.check()
                    if state_path.read_bytes() != state_bytes:
                        raise RuntimeError(
                            "The source preview changed during external routing"
                        )
                    stage(raw["reason"])
                    return {
                        "status": "no_safe_proposal",
                        "run_id": run_id,
                        "preview_opens": state["best_preview_opens"],
                        "reason": raw["reason"],
                    }
                if raw.get("status") == "strict_size_rejected":
                    narrowed = raw.get("narrowed_nets")
                    salvage = raw.get("salvage_board")
                    if not narrowed or not isinstance(salvage, bytes):
                        snapshot.check()
                        if state_path.read_bytes() != state_bytes:
                            raise RuntimeError(
                                "The source preview changed during external routing"
                            )
                        stage(
                            "Router rejected the private proposal under strict size rules"
                        )
                        return {
                            "status": "no_safe_proposal",
                            "run_id": run_id,
                            "preview_opens": state["best_preview_opens"],
                            "repair_blockers": {
                                name: raw["reason"] for name in raw["failed_nets"]
                            },
                        }
                    narrowed = RouterNames.net_names(narrowed)
                    if not set(narrowed) <= set(targets):
                        raise RuntimeError(
                            "The router's strict-size ledger contains an unexpected net"
                        )
                    ignore_nets = tuple(sorted(set(ignore_nets) | set(narrowed)))
                    raw["board"] = salvage
                    stage(
                        f"Discarding {len(narrowed)} narrowed nets; checking the remaining copper"
                    )
                trial = snapshot.directory
                raw_path = trial / "krt-candidate.kicad_pcb"
                if raw_path.exists():
                    raise RuntimeError(
                        "The temporary router candidate path already exists"
                    )
                raw_path.write_bytes(raw["board"])
                roundtrip = snapshot.board_path
            else:
                from pcb.freeroute_preview import routing_pass_budget

                stage(
                    f"External routing budget: up to {routing_pass_budget(timeout)} passes within {timeout} seconds"
                )
                raw = run_native(
                    preview,
                    snapshot.directory,
                    jar,
                    java,
                    routing_pass_budget(timeout),
                    timeout,
                    skip_optimizer=not optimizer,
                    max_items=items or None,
                    ground_contact_margin_mm=ground_contact_margin_mm,
                    search_pad_neckdown=search_pad_neckdown,
                    ignore_net_classes=ignore_net_classes,
                    ignore_nets=ignore_nets,
                    ignore_high_speed=True,
                    starts=starts,
                    defer_native_checks=True,
                    engine=engine,
                    trial_root=Path(router_folder),
                    heartbeat=lambda: stage("Still searching on the private board"),
                )
                trial = Path(raw["trial_board"]).parent
                raw_path = Path(raw["trial_board"])
                stage("Comparing with an unrouted export/import")
                roundtrip = run_native(
                    RoutingRoundtrip.roundtrip,
                    snapshot.board_path,
                    trial,
                    jar,
                    java,
                    min(timeout, 120),
                    engine,
                )
            if optimizer:
                stage(
                    f"Router optimizer status: {raw.get('optimizer_status', 'unknown')}"
                )
            stage("Filtering proposals and checking KiCad geometry")

            filter_progress = FilterProgress(stage)

            filtered = run_native(
                filter_proposal,
                snapshot.directory,
                roundtrip,
                raw_path,
                deferred_classes=ignore_net_classes,
                deferred_nets=ignore_nets,
                progress=filter_progress,
                max_seconds=cleanup_seconds,
                forward_progress=True,
                heartbeat=lambda: stage("Still checking the proposed copper"),
            )
            if filtered.get("width_blockers"):
                stage(
                    f"Skipped {len(filtered['width_blockers'])} nets with unresolved track-width requirements"
                )
            snapshot.check()
            if state_path.read_bytes() != state_bytes:
                raise RuntimeError("The source preview changed during external routing")
            if filtered["status"] != "filtered_preview":
                return {
                    "status": filtered["status"],
                    "run_id": run_id,
                    "preview_opens": state["best_preview_opens"],
                    "dominant_class": filtered.get("dominant_class"),
                    "width_blockers": filtered.get("width_blockers", {}),
                    "repair_blockers": filtered.get("repair_blockers", {}),
                }
            path = trial / "filtered-preview.kicad_pcb"
            if path.exists():
                raise RuntimeError("The filtered candidate path already exists")
            path.write_bytes(filtered["board"])
            stage("Independently verifying and retaining the improved preview")
            cache = dict(saved_checks_cache) if saved_checks_cache is not None else {}
            result, updated_cache = run_native(
                FastBatch._retain_in_worker,
                project,
                run_id,
                path,
                time.monotonic() - started,
                state_bytes,
                engine,
                cache,
                router_fingerprint=raw.get("tool") if engine == "krt" else None,
                progress=stage,
                forward_progress=True,
                heartbeat=lambda: stage("Still verifying the private preview"),
            )
            if saved_checks_cache is not None:
                saved_checks_cache.clear()
                saved_checks_cache.update(updated_cache)
            elapsed = time.monotonic() - started
            improvement = state["best_preview_opens"] - result["preview_opens"]
            return {
                **result,
                "batch_seconds": elapsed,
                "opens_reduced": improvement,
                "opens_per_minute": 60 * improvement / elapsed if elapsed else 0.0,
                "repair_blockers": filtered.get("repair_blockers", {}),
            }


class RetryMemory:
    """Remembers failed searches so an unchanged search is not repeated."""

    @staticmethod
    def fast_retry_signature(
        state: dict, inputs: dict, tools: dict, policy: str, settings: dict
    ) -> str:
        """Bind retry memory to the exact preview, inputs, code and search budget.

        Inputs:
            state: The preview run state.
            inputs: Hashes of the source files.
            tools: Hashes of the routing tools.
            policy: Hash of the routing policy.
            settings: The search settings.
        Outputs:
            A SHA-256 hex digest of all of them.
        """
        data = {
            "candidate": state["checkpoint"]["candidate_sha256"],
            "inputs": inputs,
            "tools": tools,
            "policy": policy,
            "settings": settings,
        }
        return hashlib.sha256(
            json.dumps(
                data, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def rework_attempts(state: dict, key: str) -> tuple[str, ...]:
        """Read bounded failed-plan identities only for an identical search context.

        Inputs:
            state: The preview run state.
            key: The search context signature.
        Outputs:
            The recorded attempt identities when the recorded key matches, otherwise an
            empty tuple. Raises ValueError for an invalid history.
        """
        record = state.get("rework_retry", {})
        if not isinstance(record, dict):
            raise ValueError("Invalid coordinated-repair retry history")
        if record.get("key") != key:
            return ()
        attempts = record.get("attempts")
        if (
            not isinstance(attempts, list)
            or len(attempts) > MAX_REWORK_ATTEMPTS
            or any(
                not isinstance(item, str)
                or len(item) != 64
                or any(c not in "0123456789abcdef" for c in item)
                for item in attempts
            )
            or len(set(attempts)) != len(attempts)
        ):
            raise ValueError("Invalid coordinated-repair attempt identities")
        return tuple(attempts)

    @staticmethod
    def remember_rework_attempt(
        state: dict, inputs: dict, tools: dict, policy: str, receipt: dict
    ) -> None:
        """Record a completed failed search without changing its source identity.

        Inputs:
            state: The preview run state, updated in place.
            inputs: Hashes of the source files.
            tools: Hashes of the routing tools.
            policy: Hash of the routing policy.
            receipt: The failed search's settings, key and attempt identity.
        Outputs:
            None. Raises ValueError for an invalid receipt and RuntimeError when the
            context changed or the attempt budget is exhausted.
        """
        if not isinstance(receipt, dict) or not isinstance(
            receipt.get("settings"), dict
        ):
            raise ValueError("Invalid coordinated-repair receipt")
        key = RetryMemory.fast_retry_signature(
            state, inputs, tools, policy, receipt["settings"]
        )
        if receipt.get("key") != key:
            raise RuntimeError(
                "The repair search context changed before recording its outcome"
            )
        attempt = receipt.get("attempt")
        RetryMemory.rework_attempts(
            {"rework_retry": {"key": key, "attempts": [attempt]}}, key
        )
        attempts = list(RetryMemory.rework_attempts(state, key))
        if attempt not in attempts:
            if len(attempts) == MAX_REWORK_ATTEMPTS:
                raise RuntimeError(
                    "The bounded coordinated-repair attempt budget is exhausted"
                )
            attempts.append(attempt)
        state["rework_retry"] = {"key": key, "attempts": attempts}

    @staticmethod
    def load_fast_retry(project: Path, run_id: str, settings: dict) -> dict:
        """Restore retry memory only when every source and setting still matches.

        Inputs:
            project: Project directory.
            run_id: Source preview run.
            settings: The search settings that produced the memory.
        Outputs:
            The deferred nets and classes, whether the search is exhausted and the preview
            open count, or an empty dictionary when nothing matches. Raises ValueError for
            an invalid preview or history and RuntimeError when the preview changes.
        """
        project = project.resolve()
        assets = asset_root(project)
        policy_path = config_path(project, "autopcb.json")
        _, policy_hash = read_config(policy_path)
        state_path = results_path(project, "autoroute") / f"{_run_id(run_id)}.json"
        with (
            _exclusive(state_path.parent / "active.lock", wait_seconds=55),
            Snapshot(native_path(project, ".kicad_pcb")) as snapshot,
        ):
            before = state_path.read_bytes()
            state = json.loads(before)
            if state.get("schema") != 1 or state.get("run_id") != run_id:
                raise ValueError("Invalid source preview")
            inputs = _source_hashes(snapshot, project, assets)
            decode_checkpoint(
                state["checkpoint"],
                inputs,
                snapshot.tool_hashes,
                policy_hash,
                allow_tool_changes=True,
            )
            record = state.get("fast_retry", {})
            key = RetryMemory.fast_retry_signature(
                state, inputs, snapshot.tool_hashes, policy_hash, settings
            )
            _check_sources(snapshot, project, assets, inputs, policy_path, policy_hash)
            if state_path.read_bytes() != before:
                raise RuntimeError("The preview changed while reading retry history")
            if not isinstance(record, dict) or record.get("key") != key:
                return {}
            nets = RouterNames.net_names(tuple(record.get("deferred_nets", ())))
            classes = RouterNames.class_names(tuple(record.get("deferred_classes", ())))
            exhausted = record.get("exhausted")
            if not isinstance(exhausted, bool):
                raise ValueError("Invalid focused-search retry history")
            return {
                "deferred_nets": nets,
                "deferred_classes": classes,
                "exhausted": exhausted,
                "preview_opens": state["best_preview_opens"],
            }


class UnimprovedBatchRecord:
    """Records a search batch's measured time on a preview that did not improve.

    The module-level instance record_unimproved_batch is the callable used across the package.
    """

    @staticmethod
    def _publish_recorded_progress(
        project: Path, inputs: dict, state_path: Path, state: dict
    ) -> None:
        """Keep an already saved measurement when a progress export fails.

        Inputs:
            project: Project directory.
            inputs: Hashes of the source files.
            state_path: Path of the preview state file.
            state: The updated preview run state.
        Outputs:
            None. Raises RuntimeError that names the recovery command when the plot cannot
            be updated.
        """
        try:
            _publish_progress(project, inputs, state)
        except (OSError, RuntimeError, ValueError) as error:
            try:
                _save(
                    state_path, state
                )  # Preserve the export error when the drive is available.
            except (OSError, RuntimeError, ValueError):
                pass
            raise RuntimeError(
                f"The search-time measurement was saved, but the progress plot could not be updated. "
                f"When the project is available, run python run.py review progress --preview-run {state['run_id']}."
            ) from error

    @staticmethod
    def _elapsed_evidence(approximate: bool, note: str | None) -> str | None:
        """Describe where an elapsed-time measurement came from, if needed.

        Inputs:
            approximate: The time is an interrupted batch's wall time.
            note: Optional explicit description.
        Outputs:
            The stripped note, the approximate-time description, or None. Raises ValueError
            for an empty note.
        """
        if note is not None and (not isinstance(note, str) or not note.strip()):
            raise ValueError("Elapsed-time evidence must be a nonempty description")
        if note is not None:
            return note.strip()
        return (
            "Approximate interrupted-batch wall time; private preview"
            if approximate
            else None
        )

    def __call__(
        self,
        project: Path,
        run_id: str,
        elapsed: float,
        *,
        approximate: bool = False,
        evidence_note: str | None = None,
        progress=None,
        retry: dict | None = None,
        rework_retry: dict | None = None,
    ) -> None:
        """Update the existing preview's measured search time without inventing routes.

        Inputs:
            project: Project directory.
            run_id: Preview run to update.
            elapsed: Search seconds to add; nonnegative and finite.
            approximate: The time is an interrupted batch's wall time.
            evidence_note: Optional explicit description of the measurement.
            progress: Optional callable that receives status messages.
            retry: Optional focused-search retry record to store.
            rework_retry: Optional failed coordinated-repair receipt to store.
        Outputs:
            None. Raises ValueError for an invalid time or record and RuntimeError when
            protected items or the preview change.
        """
        if (
            isinstance(elapsed, bool)
            or not isinstance(elapsed, (int, float))
            or not math.isfinite(elapsed)
            or elapsed < 0
        ):
            raise ValueError("Batch time must be nonnegative and finite")
        evidence = UnimprovedBatchRecord._elapsed_evidence(approximate, evidence_note)
        project = project.resolve()
        assets = asset_root(project)
        policy_path = config_path(project, "autopcb.json")
        _, policy_hash = read_config(policy_path)
        state_path = results_path(project, "autoroute") / f"{_run_id(run_id)}.json"
        with (
            _exclusive(
                state_path.parent / "active.lock", wait_seconds=55, progress=progress
            ),
            Snapshot(native_path(project, ".kicad_pcb")) as snapshot,
        ):
            before = state_path.read_bytes()
            state = json.loads(before)
            if state.get("schema") != 1 or state.get("run_id") != run_id:
                raise ValueError("Invalid source preview")
            inputs = _source_hashes(snapshot, project, assets)
            candidate = decode_checkpoint(
                state["checkpoint"],
                inputs,
                snapshot.tool_hashes,
                policy_hash,
                allow_tool_changes=True,
            )
            schematic = (native_path(snapshot.directory, ".kicad_sch")).read_bytes()
            if ProtectedState.capture(snapshot.original, schematic).differences(
                candidate, schematic
            ):
                raise RuntimeError(
                    "The retained preview changed protected design items"
                )
            state["elapsed_seconds"] += elapsed
            _progress_elapsed(state, state["checkpoint"]["candidate_sha256"])
            if evidence:
                state["progress_samples"][-1]["optimization_evidence"] = evidence
            if retry is not None:
                if (
                    not isinstance(retry, dict)
                    or not isinstance(retry.get("settings"), dict)
                    or not isinstance(retry.get("exhausted"), bool)
                ):
                    raise ValueError("Invalid focused-search retry record")
                nets = RouterNames.net_names(tuple(retry.get("deferred_nets", ())))
                classes = RouterNames.class_names(
                    tuple(retry.get("deferred_classes", ()))
                )
                state["fast_retry"] = {
                    "key": RetryMemory.fast_retry_signature(
                        state,
                        inputs,
                        snapshot.tool_hashes,
                        policy_hash,
                        retry["settings"],
                    ),
                    "deferred_nets": nets,
                    "deferred_classes": classes,
                    "exhausted": retry["exhausted"],
                }
            if rework_retry is not None:
                RetryMemory.remember_rework_attempt(
                    state, inputs, snapshot.tool_hashes, policy_hash, rework_retry
                )
            _check_sources(snapshot, project, assets, inputs, policy_path, policy_hash)
            if state_path.read_bytes() != before:
                raise RuntimeError("The preview changed during progress recording")
            _save(state_path, state)
            UnimprovedBatchRecord._publish_recorded_progress(
                project, inputs, state_path, state
            )


record_unimproved_batch = UnimprovedBatchRecord()


class RouterNames:
    """Validates the net and class names that scope a routing batch."""

    @staticmethod
    def class_names(names: tuple[str, ...]) -> tuple[str, ...]:
        """Normalize distinct net class names to their router classes.

        Inputs:
            names: Tuple of nonempty class names without commas.
        Outputs:
            The router class of each name. Raises ValueError for a non-tuple, an invalid
            name or classes that are not distinct.
        """
        if not isinstance(names, tuple) or any(
            not isinstance(name, str) or not name or "," in name for name in names
        ):
            raise ValueError("Classes must be distinct class names")
        normalized = tuple(router_class(name) for name in names)
        if len(set(normalized)) != len(normalized):
            raise ValueError("Classes must be distinct class names")
        return normalized

    @staticmethod
    def net_names(names: tuple[str, ...]) -> tuple[str, ...]:
        """Require distinct net names without control characters.

        Inputs:
            names: Tuple of nonempty net names.
        Outputs:
            The same tuple. Raises ValueError for a non-tuple, an invalid name or duplicates.
        """
        if (
            not isinstance(names, tuple)
            or any(
                not isinstance(name, str)
                or not name
                or any(ord(char) < 32 for char in name)
                for name in names
            )
            or len(set(names)) != len(names)
        ):
            raise ValueError("Nets must be distinct net names")
        return names


class FastBatches:
    """Runs several external-router batches in a row."""

    @staticmethod
    def run_batches(
        project: Path,
        run_id: str,
        *,
        batches: int,
        items: int,
        timeout: int,
        jar: Path | None = None,
        java: str = "auto",
        initial_deferred: tuple[str, ...] = (),
        initial_deferred_nets: tuple[str, ...] = (),
        progress=None,
        engine: str = "auto",
        cleanup_seconds: float = 60.0,
        starts: int = 1,
        excluded_classes: tuple[str, ...] = (),
        excluded_nets: tuple[str, ...] = (),
        focus_classes: tuple[str, ...] = (),
        retry_exhausted: bool = False,
        optimizer: bool = False,
        router_python: str = "auto",
        router_grid_mm: float | None = None,
        router_full: bool = False,
        ground_contact_margin_mm: float = 0.5,
        search_pad_neckdown: bool = False,
    ) -> dict:
        """Continue until the budget is reached or a batch cannot improve safely.

        Inputs:
            project: Project directory.
            run_id: Source private preview run.
            batches: Maximum number of batches.
            items: Attempt limit per batch; 0 selects all eligible.
            timeout: Router time limit per batch in seconds.
            jar: Router executable, JAR or pinned krt directory.
            java: Java executable or auto.
            initial_deferred: Net classes deferred at the start.
            initial_deferred_nets: Nets deferred at the start.
            progress: Optional callable that receives status messages.
            engine: auto, Freerouting, fastroute or krt.
            cleanup_seconds: Cleanup time limit per batch in seconds.
            starts: Number of routing orders to try (fastroute only).
            excluded_classes: Net classes excluded for the whole run.
            excluded_nets: Nets excluded for the whole run.
            focus_classes: Route only unfinished nets of these classes.
            retry_exhausted: Repeat a focused search that was already exhausted.
            optimizer: Run the router's optimizer before validation.
            router_python: Interpreter for krt.
            router_grid_mm: krt search grid in mm.
            router_full: Run krt recovery and finishing passes.
            ground_contact_margin_mm: Extra search-only ground-pad margin in mm.
            search_pad_neckdown: Propose smaller pad exits.
        Outputs:
            The last batch's record with the run, accepted batch count, deferrals, total
            opens reduced, elapsed seconds and opens per minute, or status deferred_repeat
            when an identical search was exhausted. Raises ValueError for invalid options
            and re-raises a failed batch after recording its time.
        """
        for name, value in (("batches", batches), ("timeout", timeout)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(items, bool) or not isinstance(items, int) or items < 0:
            raise ValueError("items must be a nonnegative integer")
        if (
            isinstance(cleanup_seconds, bool)
            or not isinstance(cleanup_seconds, (int, float))
            or not math.isfinite(cleanup_seconds)
            or cleanup_seconds <= 0
        ):
            raise ValueError("cleanup_seconds must be positive and finite")
        if (
            isinstance(starts, bool)
            or not isinstance(starts, int)
            or not 1 <= starts <= 2
        ):
            raise ValueError("starts must be one or two")
        if not isinstance(retry_exhausted, bool):
            raise ValueError("retry_exhausted must be boolean")
        if not isinstance(optimizer, bool):
            raise ValueError("optimizer must be boolean")
        if not isinstance(router_full, bool):
            raise ValueError("router_full must be boolean")
        if not isinstance(search_pad_neckdown, bool):
            raise ValueError("search_pad_neckdown must be boolean")
        run_id = _run_id(run_id)
        if engine not in {"auto", "Freerouting", "fastroute", "krt"}:
            raise ValueError("Unsupported routing engine")
        rust_binary = RouterCache.fastroute()
        if engine == "auto":
            engine = (
                "fastroute"
                if (jar.suffix.lower() != ".jar" if jar else rust_binary.is_file())
                else "Freerouting"
            )
        if starts > 1 and engine != "fastroute":
            raise ValueError("Multiple starts require fastroute")
        if items == 0 and (engine not in {"fastroute", "krt"} or timeout < 15):
            raise ValueError(
                "A time-bounded full pass requires fastroute or krt and at least 15 seconds"
            )
        if engine == "krt" and optimizer:
            raise ValueError(
                "The krt proposal does not support the external optimizer option"
            )
        if engine == "krt" and search_pad_neckdown:
            raise ValueError("Pad-neckdown proposals require fastroute or Freerouting")
        if router_grid_mm is not None and engine != "krt":
            raise ValueError("--router-grid-mm is only used with --engine krt")
        if router_full and engine != "krt":
            raise ValueError("--router-full is only used with --engine krt")
        if router_grid_mm is not None and (
            isinstance(router_grid_mm, bool)
            or not isinstance(router_grid_mm, (float, int))
            or not math.isfinite(router_grid_mm)
            or router_grid_mm <= 0
        ):
            raise ValueError(
                "The router grid must be a positive finite number of millimetres"
            )
        default = (
            RouterCache.freerouting()
            if engine == "Freerouting"
            else RouterCache.krt()
            if engine == "krt"
            else rust_binary
        )
        jar = (jar or default).resolve()
        if progress:
            progress(f"Routing engine: {engine}")
        accepted = 0
        fixed_classes = RouterNames.class_names(excluded_classes)
        fixed_nets = RouterNames.net_names(excluded_nets)
        focus_classes = RouterNames.class_names(focus_classes)
        deferred = [
            name
            for name in RouterNames.class_names(initial_deferred)
            if name not in fixed_classes
        ]
        deferred_nets = [
            name
            for name in RouterNames.net_names(initial_deferred_nets)
            if name not in fixed_nets
        ]
        if engine == "krt":
            from pcb.net_routing.krt_adapter import KrtInstallation

            router_signature = KrtInstallation.validate_installation(jar)
        else:
            router_signature = (
                hashlib.sha256(jar.read_bytes()).hexdigest()
                if jar.is_file()
                else "missing"
            )
        retry_settings = {
            "engine": engine,
            "router": router_signature,
            "items": items,
            "timeout": timeout,
            "cleanup_seconds": cleanup_seconds,
            "starts": starts,
            "focus": focus_classes,
            "optimizer": optimizer,
            "router_python": router_python if engine == "krt" else None,
            "router_grid_mm": router_grid_mm,
            "router_full": router_full,
            "ground_contact_margin_mm": ground_contact_margin_mm,
            "search_pad_neckdown": search_pad_neckdown,
            "excluded_classes": fixed_classes,
            "excluded_nets": fixed_nets,
            "initial_deferred": tuple(deferred),
            "initial_deferred_nets": tuple(deferred_nets),
        }
        if not retry_exhausted:
            prior = RetryMemory.load_fast_retry(project, run_id, retry_settings)
            if prior.get("exhausted"):
                if progress:
                    progress(
                        "A bounded attempt with these settings found no improvement; "
                        "changed inputs or --retry-exhausted can try again"
                    )
                return {
                    "status": "deferred_repeat",
                    "run_id": run_id,
                    "preview_opens": prior["preview_opens"],
                    "accepted_batches": 0,
                    "total_opens_reduced": 0,
                    "total_elapsed_seconds": 0.0,
                    "overall_opens_per_minute": 0.0,
                }
            deferred.extend(
                name
                for name in prior.get("deferred_classes", ())
                if name not in deferred and name not in fixed_classes
            )
            deferred_nets.extend(
                name
                for name in prior.get("deferred_nets", ())
                if name not in deferred_nets and name not in fixed_nets
            )
            if progress and (
                prior.get("deferred_nets") or prior.get("deferred_classes")
            ):
                progress(
                    "Reusing previous unsuccessful-net deferrals for this unchanged focused search"
                )
        accepted_since_revisit = 0
        started = time.monotonic()
        total_reduced = 0
        saved_checks_cache = {}
        current_items = items
        last = {"status": "not_started", "run_id": run_id}
        for index in range(batches):
            if progress:
                progress(f"Batch {index + 1}/{batches}")
            batch_started = time.monotonic()
            try:
                last = FastBatch.run_one(
                    project,
                    run_id,
                    items=current_items,
                    timeout=timeout,
                    jar=jar,
                    java=java,
                    ignore_net_classes=(*fixed_classes, *deferred),
                    ignore_nets=(*fixed_nets, *deferred_nets),
                    progress=progress,
                    engine=engine,
                    cleanup_seconds=cleanup_seconds,
                    starts=starts,
                    saved_checks_cache=saved_checks_cache,
                    focus_classes=focus_classes,
                    optimizer=optimizer,
                    router_python=router_python,
                    router_grid_mm=router_grid_mm,
                    router_full=router_full,
                    ground_contact_margin_mm=ground_contact_margin_mm,
                    search_pad_neckdown=search_pad_neckdown,
                )
            except Exception:
                try:
                    record_unimproved_batch(
                        project,
                        run_id,
                        time.monotonic() - batch_started,
                        progress=progress,
                    )
                except (OSError, RuntimeError, ValueError) as error:
                    if progress:
                        progress(f"Could not record the failed batch time: {error}")
                raise
            newly_deferred = sorted(
                set(last.get("repair_blockers", {}))
                - set(deferred_nets)
                - set(fixed_nets)
            )
            if newly_deferred:
                deferred_nets.extend(newly_deferred)
                if progress:
                    progress(
                        "Temporarily deferring unsuccessful local repairs: "
                        + ", ".join(newly_deferred)
                    )
            if last["status"] != "private_preview_retained":
                should_continue = False
                if newly_deferred and last["status"] in {
                    "no_improvement",
                    "no_safe_proposal",
                    "filter_stalled",
                }:
                    should_continue = True
                dominant = last.get("dominant_class")
                if (
                    not should_continue
                    and last["status"]
                    in {"no_improvement", "no_safe_proposal", "filter_stalled"}
                    and dominant
                    and dominant not in deferred
                    and dominant not in fixed_classes
                    and (not focus_classes or set(focus_classes) - {dominant})
                ):
                    deferred.append(dominant)
                    if progress:
                        progress(
                            f"No safe improvement; temporarily deferring {dominant} to try other nets"
                        )
                    should_continue = True
                if (
                    not should_continue
                    and last["status"] == "no_safe_proposal"
                    and engine == "fastroute"
                    and current_items > 0
                    and timeout >= 15
                ):
                    current_items = 0
                    if progress and index + 1 < batches:
                        progress(
                            "No eligible short-batch proposal; trying a time-bounded full pass"
                        )
                    elif progress:
                        progress(
                            "Short-batch budget ended; a full pass is available on the next run"
                        )
                    should_continue = True
                retry = {
                    "settings": retry_settings,
                    "deferred_nets": tuple(deferred_nets),
                    "deferred_classes": tuple(deferred),
                    "exhausted": not should_continue,
                }
                record_unimproved_batch(
                    project,
                    run_id,
                    time.monotonic() - batch_started,
                    progress=progress,
                    retry=retry,
                )
                if should_continue:
                    continue
                break
            accepted += 1
            accepted_since_revisit += 1
            current_items = items
            run_id = last["run_id"]
            total_reduced += last.get("opens_reduced", 0)
            if progress and "opens_per_minute" in last:
                progress(
                    f"Retained {last['opens_reduced']} fewer opens in "
                    f"{last['batch_seconds']:.0f} s ({last['opens_per_minute']:.1f} opens/minute)"
                )
                elapsed = time.monotonic() - started
                progress(
                    f"Overall: {total_reduced} fewer opens in {elapsed:.0f} s "
                    f"({60 * total_reduced / elapsed:.1f} opens/minute), including unsuccessful batches"
                )
            if (deferred or deferred_nets) and accepted_since_revisit >= 3:
                deferred.clear()
                deferred_nets.clear()
                accepted_since_revisit = 0
                if progress:
                    progress("Revisiting all net classes")
        elapsed = time.monotonic() - started
        return {
            **last,
            "run_id": run_id,
            "accepted_batches": accepted,
            "temporarily_deferred": deferred,
            "temporarily_deferred_nets": deferred_nets,
            "excluded_classes": fixed_classes,
            "excluded_nets": fixed_nets,
            "focus_classes": focus_classes,
            "total_opens_reduced": total_reduced,
            "total_elapsed_seconds": elapsed,
            "overall_opens_per_minute": 60 * total_reduced / elapsed
            if elapsed
            else 0.0,
        }


class FastCommand:
    """Command line that runs bounded external-router batches."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Run the batches and print the outcome.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 after a result is printed, and 1 when an OSError, RuntimeError,
            ValueError or subprocess error stops it (the message goes to standard error).
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--project", type=Path, required=True)
        parser.add_argument("--run", required=True, help="source private preview run")
        parser.add_argument("--batches", type=int, default=1)
        parser.add_argument(
            "--items",
            type=int,
            default=80,
            help="attempt limit per batch; for krt this limits net count; 0 selects all eligible",
        )
        parser.add_argument("--timeout", type=int, default=180)
        parser.add_argument(
            "--cleanup-seconds",
            type=float,
            default=60.0,
            help="maximum cleanup time per batch before retrying other nets",
        )
        parser.add_argument(
            "--starts",
            type=int,
            choices=(1, 2),
            default=1,
            help="number of routing orders to try (fastroute only)",
        )
        parser.add_argument(
            "--optimizer",
            action="store_true",
            help="request the external router's optimizer before validation",
        )
        parser.add_argument(
            "--jar",
            "--router",
            dest="jar",
            type=Path,
            help="router executable, JAR, or pinned krt installation directory",
        )
        parser.add_argument("--java", default="auto")
        parser.add_argument(
            "--router-python", default="auto", help="Python for the optional krt router"
        )
        parser.add_argument(
            "--router-grid-mm",
            type=float,
            help="krt search-grid spacing in millimetres",
        )
        parser.add_argument(
            "--router-full",
            action="store_true",
            help="run krt recovery and finishing passes before independent validation",
        )
        parser.add_argument(
            "--ground-contact-guard-mm",
            type=float,
            default=0.5,
            help="extra search-only ground-pad margin; native physical rules remain unchanged",
        )
        parser.add_argument(
            "--search-pad-neckdown",
            action="store_true",
            help="propose smaller pad exits; saved width rules and native checks still apply",
        )
        parser.add_argument(
            "--engine",
            choices=("auto", "Freerouting", "fastroute", "krt"),
            default="auto",
        )
        parser.add_argument(
            "--defer-class",
            action="append",
            default=[],
            help="temporarily defer a net class; it will be revisited",
        )
        parser.add_argument(
            "--defer-net",
            action="append",
            default=[],
            help="temporarily defer a blocked net; it will be revisited",
        )
        parser.add_argument(
            "--exclude-class",
            action="append",
            default=[],
            help="exclude a net class for the entire run",
        )
        parser.add_argument(
            "--exclude-net",
            action="append",
            default=[],
            help="exclude a net for the entire run",
        )
        parser.add_argument(
            "--focus-open-class",
            action="append",
            default=[],
            help="temporarily route only unfinished nets in the given native class",
        )
        parser.add_argument(
            "--retry-exhausted",
            action="store_true",
            help="repeat a focused search previously exhausted on identical inputs",
        )
        args = parser.parse_args(argv)
        try:
            with quiet_duplicate_image_handlers():
                result = FastBatches.run_batches(
                    args.project,
                    args.run,
                    batches=args.batches,
                    items=args.items,
                    timeout=args.timeout,
                    jar=args.jar,
                    java=args.java,
                    initial_deferred=tuple(args.defer_class),
                    initial_deferred_nets=tuple(args.defer_net),
                    excluded_classes=tuple(args.exclude_class),
                    excluded_nets=tuple(args.exclude_net),
                    focus_classes=tuple(args.focus_open_class),
                    retry_exhausted=args.retry_exhausted,
                    cleanup_seconds=args.cleanup_seconds,
                    starts=args.starts,
                    optimizer=args.optimizer,
                    router_python=args.router_python,
                    router_grid_mm=args.router_grid_mm,
                    router_full=args.router_full,
                    ground_contact_margin_mm=args.ground_contact_guard_mm,
                    search_pad_neckdown=args.search_pad_neckdown,
                    engine=args.engine,
                    progress=lambda message: print(message, flush=True),
                )
            print(
                f"Result: {result['status']}; accepted batches: {result['accepted_batches']}"
            )
            print(f"Private preview opens: {result.get('preview_opens', 'unknown')}")
            print(
                f"Overall: {result['total_opens_reduced']} fewer opens in {result['total_elapsed_seconds']:.0f} s "
                f"({result['overall_opens_per_minute']:.1f} opens/minute), including unsuccessful batches"
            )
            print(f"Run: {result['run_id']}")
            return 0
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            print(f"Fast routing stopped: {error}", file=sys.stderr)
            return 1


main = FastCommand()


if __name__ == "__main__":
    raise SystemExit(main())
