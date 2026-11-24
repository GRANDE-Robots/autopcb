"""inspect_kicad_toolchain.py: report locally available KiCad and routing tools.

The report never opens a design.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.toolchain import inspect_toolchain


class ToolchainCommand:
    """Command-line entry: print the located KiCad and Freerouting tools as JSON."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Run the inspection.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 when the toolchain status is ``ok``, otherwise 1.
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--project", type=Path, required=True)
        parser.add_argument("--cli", type=Path, help="KiCad CLI executable to inspect")
        parser.add_argument(
            "--freerouting-jar", type=Path, help="Freerouting JAR to locate"
        )
        args = parser.parse_args(argv)
        result = inspect_toolchain(cli=args.cli, freerouting_jar=args.freerouting_jar)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result["status"] == "ok" else 1


main = ToolchainCommand()

if __name__ == "__main__":
    raise SystemExit(main())
