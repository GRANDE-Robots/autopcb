"""Installed AutoPCB command and its machine-readable job interface."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from autopcb import design, jobs
from autopcb.layout import Budget, Layout
from autopcb.native import Native
from autopcb.workspace import Workspace
from core.project import ProjectPaths


class DesktopWorkspace:
    @staticmethod
    def select(requested: Path | None) -> Path:
        if requested is not None:
            return requested
        if sys.platform != "win32" or not getattr(sys, "frozen", False):
            return Path(".")
        local = Path(
            os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        )
        workspace = local / "AutoPCB" / "Workspace"
        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ValueError(
                f"Could not create the AutoPCB workspace at {workspace}: {error}"
            ) from error
        return workspace


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="autopcb", description="Plan, check, and export native KiCad designs"
    )
    root.add_argument(
        "--workspace", type=Path, help="root for config, data and results"
    )
    root.add_argument("--project", type=Path, help="KiCad file or project directory")
    root.add_argument("--kicad-cli", type=Path)
    root.add_argument("--kicad-python", type=Path)
    commands = root.add_subparsers(dest="action", required=True)
    commands.add_parser(
        "doctor", help="verify a matched KiCad CLI and native worker"
    ).add_argument("--output", type=Path)
    qualification = commands.add_parser(
        "qualify", help="exercise both reference boards on this installation"
    )
    qualification.add_argument(
        "--output", type=Path, required=True, help="new folder for cases and evidence"
    )
    qualification.add_argument("--rounds", type=int, default=20)
    qualification.add_argument("--seconds", type=int, default=10)
    qualification.add_argument("--memory-gib", type=float)
    qualification.add_argument(
        "--fixed-outlines",
        action="store_true",
        help="qualify the nominal catalog sizes as a control for compact placement",
    )
    commands.add_parser("discover", help="list native project entries")
    propose = commands.add_parser(
        "propose", help="design from a requirements text file"
    )
    propose.add_argument(
        "requirements", type=Path, nargs="?", help="defaults to data/requirements.txt"
    )
    propose.add_argument("--catalog", type=Path)
    propose.add_argument("--output", type=Path)
    propose.add_argument("--schematic-draft", type=Path)
    lookup = commands.add_parser(
        "lookup-sources", help="download only the public URLs in a proposal"
    )
    lookup.add_argument("proposal", type=Path)
    lookup.add_argument(
        "--output", type=Path, help="defaults to results/sources/PROPOSAL_ID"
    )
    build = commands.add_parser(
        "create", help="create editable native files from an approved proposal"
    )
    build.add_argument("proposal", type=Path)
    build.add_argument("--approve", required=True, help="exact proposal ID from review")
    destination = build.add_mutually_exclusive_group()
    destination.add_argument("--output", type=Path)
    destination.add_argument("--name", help="project name under results/projects")
    commands.add_parser("inspect", help="read board layout geometry")
    commands.add_parser(
        "verify", help="independent verification without changing the saved design"
    )
    commands.add_parser(
        "configure-layout", help="write a starter routing policy for review"
    )
    route = commands.add_parser("route", help="run a bounded layout preview")
    route.add_argument("--rounds", type=int, default=10)
    route.add_argument("--seconds", type=int, default=30)
    route.add_argument("--memory-gib", type=float)
    reuse = route.add_mutually_exclusive_group()
    reuse.add_argument("--resume")
    reuse.add_argument("--renew")
    route.add_argument("--allow-placement", action="store_true")
    for name, description in (
        ("compare", "inspect the saved and previewed board"),
        ("apply", "explicitly save a validated routing improvement"),
    ):
        commands.add_parser(name, help=description).add_argument("run_id")
    compare_bench = commands.add_parser(
        "benchmark", help="measure two routing arms on identical copies"
    )
    arms = compare_bench.add_mutually_exclusive_group(required=True)
    arms.add_argument(
        "--baseline",
        type=Path,
        help="compare this AutoPCB checkout with the current engine",
    )
    arms.add_argument(
        "--refinement",
        action="store_true",
        help="compare greedy routing with recovery and rework",
    )
    compare_bench.add_argument("--rounds", type=int, default=3)
    compare_bench.add_argument("--seconds", type=int, default=30)
    compare_bench.add_argument("--memory-gib", type=float, default=3)
    study = commands.add_parser(
        "placement-study",
        help="compare fixed placement with permitted moves under one budget",
    )
    study.add_argument("--rounds", type=int, default=10)
    study.add_argument("--seconds", type=int, default=30)
    study.add_argument("--memory-gib", type=float, default=3)
    study.add_argument(
        "--variants",
        type=int,
        default=0,
        help="study this many crowded packings of the board instead of the board itself",
    )
    study.add_argument("--seed", type=int, default=1, help="first packing seed")
    study.add_argument("--output", type=Path)
    export = commands.add_parser(
        "export", help="generate a local manufacturing package"
    )
    export.add_argument("--output", type=Path, help="defaults to results/exports")
    export.add_argument(
        "--draft",
        action="store_true",
        help="label package as draft and include blockers",
    )
    commands.add_parser("desktop", help="start the AutoPCB desktop application")
    serve = commands.add_parser(
        "serve", help="serve a phone-friendly page that runs jobs on this computer"
    )
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to listen on; use a LAN or Tailscale address for a phone",
    )
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--token", help="access token; a random one is generated when omitted"
    )
    campaign = commands.add_parser("campaign", help="run a campaign in the workspace")
    campaign.add_argument("args", nargs=argparse.REMAINDER)
    legacy = commands.add_parser(
        "legacy", help="delegate to the existing run.py actions"
    )
    legacy.add_argument("args", nargs=argparse.REMAINDER)
    return root


def _run(args) -> dict:
    workspace = Workspace(args.workspace or Path("."))
    if args.action == "qualify":
        from autopcb.qualification import qualify

        return qualify(
            args.output,
            cli=args.kicad_cli,
            python=args.kicad_python,
            rounds=args.rounds,
            seconds=args.seconds,
            memory_gib=args.memory_gib,
            fixed_outlines=args.fixed_outlines,
        )
    if args.action == "discover":
        entries = (
            workspace.entries()
            if args.project is None
            else ProjectPaths.entries(
                args.project if args.project.is_dir() else args.project.parent
            )
        )
        return {
            "schema_version": 1,
            "kind": "project_discovery",
            "status": "ok",
            "entries": [str(path) for path in entries],
        }
    if args.action == "propose":
        requirements = args.requirements or workspace.requirements
        default_catalog = workspace.data / "catalog.json"
        selected = args.catalog or (
            default_catalog if default_catalog.is_file() else None
        )
        proposal = design.propose(
            requirements.read_text(encoding="utf-8-sig"), catalog_file=selected
        )
        if args.output:
            jobs.write_json(args.output, proposal)
        else:
            output = (
                workspace.results / "proposals" / (proposal["proposal_id"] + ".json")
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            try:
                jobs.write_json(output, proposal)
            except FileExistsError:
                if json.loads(output.read_text(encoding="utf-8")) != proposal:
                    raise ValueError(
                        f"Stored proposal differs from the current result: {output}"
                    )
        if args.schematic_draft and proposal["status"] == "proposed":
            document, _library, _links = design.schematic(
                proposal, args.schematic_draft.stem
            )
            with args.schematic_draft.open(
                "x", encoding="utf-8", newline="\n"
            ) as handle:
                handle.write(document)
        return proposal
    if args.action == "lookup-sources":
        from autopcb.research import datasheets

        proposal = json.loads(args.proposal.read_text(encoding="utf-8-sig"))
        design.validated(proposal, proposal.get("proposal_id"))
        output = args.output
        if output is None:
            output = workspace.results / "sources" / proposal["proposal_id"]
            output.parent.mkdir(parents=True, exist_ok=True)
        return datasheets(proposal, output)
    native = Native.discover(cli=args.kicad_cli, python=args.kicad_python)
    if args.action == "doctor":
        report = {
            "schema_version": 1,
            "kind": "doctor",
            "status": "ok",
            **native.status(),
        }
        if args.output:
            jobs.write_json(args.output, report)
        return report
    defaults = workspace.config / "routing.defaults.json"
    if args.action == "create":
        proposal = json.loads(args.proposal.read_text(encoding="utf-8-sig"))
        body = design.validated(proposal, args.approve)
        target = args.output or workspace.project_output(args.name or body["design"])
        return jobs.build(
            native,
            proposal,
            args.approve,
            target,
            defaults=defaults,
            storage=workspace.storage(target / f"{target.name}.kicad_pro"),
        )
    project = workspace.project(args.project)
    storage = workspace.storage(project.entry)
    if args.action == "inspect":
        return jobs.inspect(native, project)
    if args.action == "verify":
        report = jobs.verify(native, project, storage=storage)
        target = storage.results / "verification.json"
        jobs.publish_report(target, report)
        return {**report, "report": str(target)}
    if args.action == "configure-layout":
        return Layout(native, project, storage).policy(defaults=defaults)
    if args.action == "route":
        return Layout(native, project, storage).preview(
            rounds=args.rounds,
            seconds=args.seconds,
            memory_gib=args.memory_gib,
            resume=args.resume,
            renew=args.renew,
            allow_placement=args.allow_placement,
        )
    if args.action == "compare":
        return Layout(native, project, storage).compare(args.run_id)
    if args.action == "apply":
        return Layout(native, project, storage).apply(args.run_id)
    if args.action == "benchmark":
        from autopcb.benchmark import Benchmark

        budget = Budget(args.rounds, args.seconds, args.memory_gib)
        if args.refinement:
            return Benchmark.refinement(native, project, budget, storage).run()
        return Benchmark.versions(native, project, args.baseline, budget, storage).run()
    if args.action == "placement-study":
        from autopcb.placement_study import PlacementStudy, VariantStudy

        budget = Budget(args.rounds, args.seconds, args.memory_gib)
        if args.variants:
            seeds = list(range(args.seed, args.seed + args.variants))
            report = VariantStudy(native, project, budget, seeds, storage).run()
        else:
            report = PlacementStudy(native, project, budget, storage).run()
        if args.output:
            jobs.write_json(args.output, report)
        return report
    target = args.output or workspace.export_output(project, args.draft)
    return {
        **jobs.export(native, project, target, draft=args.draft, storage=storage),
        "package": str(target.resolve()),
    }


def main(argv: list[str] | None = None) -> int:
    import signal

    def stop(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    command = parser()
    args = command.parse_args(argv)
    if args.action == "desktop":
        from autopcb.desktop import main as launch

        try:
            workspace = DesktopWorkspace.select(args.workspace)
        except ValueError as error:
            command.error(str(error))
        return launch(
            args.project or workspace,
            args.kicad_cli,
            args.kicad_python,
            workspace=workspace,
        )
    try:
        Native.prepare_windows_cli()
    except OSError as error:
        command.error(f"Could not restore Windows library search paths: {error}")
    args.workspace = args.workspace or Path(".")
    if args.action == "campaign":
        from core.launcher import main as legacy

        try:
            workspace = Workspace(args.workspace)
            project = workspace.project(args.project)
            variables = workspace.storage(project.entry).environment(project.entry)
        except (OSError, ValueError) as error:
            parser().error(str(error))
        previous = {key: os.environ.get(key) for key in variables}
        os.environ.update(variables)
        try:
            prefix = ["--project", str(project.entry)]
            if args.kicad_python:
                prefix.extend(("--kicad-python", str(args.kicad_python)))
            return legacy([*prefix, "campaign", *args.args])
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    if args.action == "legacy":
        from core.launcher import main as legacy

        prefix = ["--project", str(args.project or args.workspace)]
        if args.kicad_python:
            prefix.extend(("--kicad-python", str(args.kicad_python)))
        return legacy([*prefix, *args.args])
    if args.action == "serve":
        import secrets

        from autopcb.serve import JobRunner, ServeApp, Server

        workspace = Workspace(args.workspace)
        extra = tuple(
            item
            for flag, value in (
                ("--kicad-cli", args.kicad_cli),
                ("--kicad-python", args.kicad_python),
            )
            if value
            for item in (flag, str(value))
        )
        server = Server(
            ServeApp(
                workspace,
                args.token or secrets.token_urlsafe(16),
                JobRunner(workspace, extra),
                args.kicad_cli,
            ),
            args.host,
            args.port,
        )
        print(
            f"AutoPCB is serving {workspace.root}. Open this link on your phone:\n{server.url}",
            flush=True,
        )
        try:
            server.run()
        except KeyboardInterrupt:
            pass
        return 0
    try:
        report = _run(args)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        report = {
            "schema_version": 1,
            "kind": args.action,
            "status": "blocked",
            "error": str(error),
        }
    except KeyboardInterrupt:
        report = {"schema_version": 1, "kind": args.action, "status": "cancelled"}
    print(json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False))
    return {
        "blocked": 2,
        "stalled": 2,
        "interrupted": 2,
        "failed": 2,
        "qualification_incomplete": 2,
        "cancelled": 130,
    }.get(report["status"], 0)


def desktop_main() -> int:
    return main(["desktop"])


if __name__ == "__main__":
    raise SystemExit(main())
