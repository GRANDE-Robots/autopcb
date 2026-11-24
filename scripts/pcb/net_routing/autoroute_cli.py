"""autoroute_cli.py: prepare, preview and inspect board-wide AutoPCB runs.

Searches run on private saved-file copies. Publication remains disabled until
independent electrical, high-speed and final completion checks are implemented.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from core.console import Console, ProgressHeartbeat, add_output_arguments
from core.pcb_io import quiet_duplicate_image_handlers
from pcb.net_routing.presentation import (
    RoutingDisplay,
    show_pair_probe,
    show_preparation,
    show_run,
)
from pcb.net_routing.prepare import prepare_project
from pcb.net_routing.runner import read_status, run_project
from pcb.net_routing.resources import limit_resources


class AutorouteCommand:
    """Command line that prepares, previews and inspects board-wide AutoPCB runs."""

    @staticmethod
    def _parser() -> argparse.ArgumentParser:
        """Build the parser with the prepare, verify, run, status and pair-probe commands."""
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--project", type=Path, required=True)
        commands = parser.add_subparsers(dest="command", required=True)
        prepare = commands.add_parser(
            "prepare", help="inventory and check every saved-board net"
        )
        verify = commands.add_parser(
            "verify", help="independently check the saved board"
        )
        run = commands.add_parser(
            "run", help="run resumable, nonpublishing routing previews"
        )
        reuse = run.add_mutually_exclusive_group()
        reuse.add_argument("--resume", help="resume a source-matched run ID")
        reuse.add_argument(
            "--renew",
            help="recheck a preview after code updates and rebuild its queue in a new run",
        )
        run.add_argument(
            "--max-rounds",
            type=int,
            help="pause after this many search rounds; 0 initializes without searching",
        )
        run.add_argument(
            "--seconds-per-batch", type=float, help="override the per-batch search time"
        )
        run.add_argument(
            "--previous-board",
            type=Path,
            help="preimage for an explicitly renewed layer-annotation-only change",
        )
        run.add_argument(
            "--previous-policy",
            type=Path,
            help="preimage for an explicitly renewed soft layer-cost change",
        )
        run.add_argument("--workers", type=int, default=2, choices=(1, 2))
        run.add_argument(
            "--single-net-search",
            action="store_true",
            help="search one net per worker to avoid dividing its time across a group",
        )
        run.add_argument(
            "--keep-placement",
            action="store_true",
            help="preserve every footprint and exclude placement moves from recovery",
        )
        run.add_argument(
            "--search-cells", type=int, help="runtime grid-cell budget, at most 8000000"
        )
        run.add_argument(
            "--search-expansions",
            type=int,
            help="runtime path-expansion budget, at most 2000000",
        )
        run.add_argument(
            "--search-grid-mm",
            type=float,
            help="runtime grid ceiling from 0.025 to 0.5 mm; finer recovery grids are preserved",
        )
        run.add_argument(
            "--search-heuristic-weight",
            type=float,
            help="search greediness from 1 to 20; actual routing costs and hard rules stay unchanged",
        )
        run.add_argument(
            "--memory-gib",
            type=float,
            help="explicit aggregate memory budget, up to 6 GiB; default: half available RAM",
        )
        run.add_argument(
            "--wait-for-memory",
            type=float,
            default=0,
            help="wait at most this many seconds for the launch memory budget",
        )
        run.add_argument(
            "--apply",
            action="store_true",
            help="request publication (currently disabled)",
        )
        status = commands.add_parser("status", help="show a saved preview run")
        status.add_argument("--run", required=True, dest="run_id")
        pair = commands.add_parser(
            "pair-probe", help="test paired routing on a disposable board"
        )
        pair.add_argument(
            "--seconds", type=int, default=45, help="paired router time budget"
        )
        pair.add_argument("--engine", choices=("fastroute", "krt"), default="fastroute")
        pair.add_argument(
            "--router-grid-mm", type=float, help="optional KRT search-grid spacing"
        )
        pair.add_argument(
            "--pair-index",
            type=int,
            help="test one discovered pair by its one-based index",
        )
        pair.add_argument(
            "--memory-gib",
            type=float,
            help="explicit aggregate memory budget, up to 6 GiB",
        )
        pair.add_argument(
            "--run",
            dest="run_id",
            help="record diagnostic time on a current preview plot",
        )
        for command in (prepare, verify, run, status, pair):
            add_output_arguments(command, full_alias=True)
        return parser

    @staticmethod
    def _pair_probe(args, console: Console, display: RoutingDisplay) -> None:
        """Run the paired-routing diagnostic in a worker and show its result.

        Inputs:
            args: Parsed pair-probe arguments.
            console: Console that prints the result.
            display: Progress display that also receives the worker's progress.
        Outputs:
            None. When a run ID was given, the measured search time is recorded on
            that preview even if the diagnostic fails.
        """
        from pcb.net_routing.isolation import run_native
        from pcb.net_routing.pair_probe import PairProbe

        started = time.monotonic()
        try:
            with (
                ProgressHeartbeat(display),
                limit_resources(memory_gib=args.memory_gib, progress=display),
            ):
                result = run_native(
                    PairProbe.probe,
                    args.project,
                    seconds=args.seconds,
                    engine=args.engine,
                    router_grid_mm=args.router_grid_mm,
                    pair_index=args.pair_index,
                    progress=display,
                    forward_progress=True,
                )
        finally:
            if args.run_id:
                from pcb.net_routing.fast import record_unimproved_batch

                record_unimproved_batch(
                    args.project,
                    args.run_id,
                    time.monotonic() - started,
                    progress=display,
                )
        console.result(result, show_pair_probe)

    @staticmethod
    def _run(args, console: Console, display: RoutingDisplay) -> int:
        """Run resumable routing previews under the resource limits and show the result.

        Inputs:
            args: Parsed run arguments.
            console: Console that prints the result.
            display: Progress display.
        Outputs:
            Exit status 0 when the run paused or found zero opens, otherwise 2.
        """
        with (
            ProgressHeartbeat(display),
            limit_resources(
                memory_gib=args.memory_gib,
                wait_seconds=args.wait_for_memory,
                progress=display,
            ) as resource_limits,
        ):
            display({"event": "resources", **resource_limits, "workers": args.workers})
            result = run_project(
                args.project,
                resume=args.resume,
                renew=args.renew,
                max_rounds=args.max_rounds,
                workers=args.workers,
                seconds_per_batch=args.seconds_per_batch,
                progress=display,
                resource_limits=resource_limits,
                previous_board=args.previous_board,
                previous_policy=args.previous_policy,
                single_net_search=args.single_net_search,
                keep_placement=args.keep_placement,
                search_cells=args.search_cells,
                search_expansions=args.search_expansions,
                search_step_mm=args.search_grid_mm,
                search_heuristic_weight=args.search_heuristic_weight,
            )
            console.result(result, show_run)
            return 0 if result["status"] in {"paused", "preview_zero_opens"} else 2

    @quiet_duplicate_image_handlers()
    def __call__(self, argv: list[str] | None = None) -> int:
        """Parse the command line and run the chosen board-wide action.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 on success, and 2 for a failed run, a verification with
            completion issues, or an OSError, ValueError, RuntimeError or
            MemoryError (reported through the console). Publication is refused
            by the argument parser.
        """
        parser = AutorouteCommand._parser()
        args = parser.parse_args(argv)
        console = Console(json_mode=args.json, verbose=args.verbose)
        display = RoutingDisplay(console)
        if args.command == "run" and args.apply:
            parser.error(
                "publication is disabled: electrical, differential-pair and reference-path checks are incomplete"
            )
        try:
            if args.command == "status":
                console.result(read_status(args.project, args.run_id), show_run)
                return 0
            if args.command == "pair-probe":
                AutorouteCommand._pair_probe(args, console, display)
                return 0
            if args.command == "run":
                return AutorouteCommand._run(args, console, display)
            with ProgressHeartbeat(display):
                report = prepare_project(args.project, progress=display)
            console.result(report, show_preparation)
            return 2 if args.command == "verify" and report["completion_issues"] else 0
        except (OSError, ValueError, RuntimeError, MemoryError) as error:
            console.error(error)
            return 2


main = AutorouteCommand()


if __name__ == "__main__":
    raise SystemExit(main())
