"""Report locally available KiCad and routing tools without opening a design."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.toolchain import inspect_toolchain


def main(argv: list[str] | None = None) -> int:
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


if __name__ == "__main__":
    raise SystemExit(main())
