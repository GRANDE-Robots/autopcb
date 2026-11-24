"""cli.py: plan whole nets with alternate trees, order trials and bounded backtracking.

This command reads saved files, searches disposable snapshots, and never applies
changes. Use --example to print a starting configuration. Electrical limits
must be supplied separately for the actual circuit before saving a campaign.

The nets array contains objects with a name and optional overrides of shared
route settings. An optional orders array lists complete permutations of those
names; one permutation fixes the order. Otherwise several deterministic orders
are tried. Search limits cap tree/board beams, alternatives, path calls and time.

Optional movable entries use reference, positions_mm and rotations_deg. Only
explicitly listed poses of parts the placement policy lets move are considered,
and all nets on each movable part must be included. Native validation preserves existing connections and
rejects new DRC violations. Nothing locks or groups the resulting tracks.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pcb.route_cli import save_validated_campaign
from pcb.net_routing.native import sha
from pcb.net_routing.workflow import plan_nets


EXAMPLE = {
    "board_snapshot": "pcb",
    "shared": {
        "bounds_mm": [0, 0, 100, 100],
        "step_mm": 0.5,
        "layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
        "width_mm": 0.2,
        "clearance_mm": 0.2,
        "via": {"size_mm": 0.6, "drill_mm": 0.3},
        "weights": {"length": 1.0, "bend": 1.0, "via": 10.0},
        "layer_penalties": {},
        "forbid_staircase": True,
        "gentle_turns": True,
    },
    "nets": [{"name": "PPS_MASTER"}],
    "search": {
        "board_beam": 4,
        "tree_beam": 4,
        "net_candidates": 3,
        "path_candidates": 2,
        "order_trials": 4,
        "path_calls": 400,
        "seconds": 120,
    },
}


class NetPlannerCommand:
    """Command line that plans whole nets on disposable snapshots and never applies them."""

    @staticmethod
    def _parser() -> argparse.ArgumentParser:
        """Build the parser for the planner configuration, validation and campaign options."""
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--project", type=Path, required=True)
        parser.add_argument(
            "--config",
            type=Path,
            help="JSON nets, shared rules, search limits and optional movable poses",
        )
        parser.add_argument(
            "--example",
            action="store_true",
            help="print illustrative settings; dimensions and ratings are not a recommendation",
        )
        parser.add_argument(
            "--validate",
            action="store_true",
            help="run closing native DRC/connectivity on disposable candidates",
        )
        parser.add_argument(
            "--details", action="store_true", help="include segment/via geometry"
        )
        parser.add_argument("--include-campaign-changes", action="store_true")
        parser.add_argument(
            "--save-campaign",
            metavar="NAME",
            help="save a candidate only after native and supplied electrical checks",
        )
        return parser

    def __call__(self, argv: list[str] | None = None) -> int:
        """Plan the nets in a configuration file and print the result as JSON.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 for a planned or example result, and 2 when the planner
            reports no candidate or an OSError, ValueError or RuntimeError stops it
            (the message goes to standard error). A saved campaign needs a complete
            native-validated candidate whose nets are within the supplied limits.
        """
        parser = NetPlannerCommand._parser()
        args = parser.parse_args(argv)
        if args.example:
            print(json.dumps(EXAMPLE, indent=2))
            return 0
        if args.config is None:
            parser.error("--config is required unless using --example")
        if args.save_campaign and not args.validate:
            parser.error("--save-campaign requires --validate")
        try:
            config = args.config.resolve()
            before = sha(config)
            data = json.loads(config.read_text(encoding="utf-8-sig"))
            result = plan_nets(
                data,
                args.project.resolve(),
                validate=args.validate,
                details=args.details,
                include_changes=args.include_campaign_changes
                or bool(args.save_campaign),
            )
            if sha(config) != before:
                raise RuntimeError("Planner configuration changed during search")
            result["config_sha256"] = before
            if args.save_campaign:
                if result["status"] != "validated_sampled_candidate" or any(
                    n["electrical"]["status"] != "within_supplied_limits"
                    for n in result["nets"]
                ):
                    raise ValueError(
                        "Saving requires a complete native-validated candidate and whole-net electrical limits"
                    )
                result["campaign"] = save_validated_campaign(
                    args.project.resolve(),
                    data.get("board_snapshot", "pcb"),
                    args.save_campaign,
                    result["validation"],
                    result["source_sha256"],
                    result["unvalidated_campaign_changes"],
                )
                if not args.include_campaign_changes:
                    del result["unvalidated_campaign_changes"]
            print(json.dumps(result, separators=(",", ":"), allow_nan=False))
            return 2 if result["status"].startswith("no_") else 0
        except (OSError, ValueError, RuntimeError) as error:
            print(f"Net planner: {error}", file=sys.stderr)
            return 2


main = NetPlannerCommand()


if __name__ == "__main__":
    raise SystemExit(main())
