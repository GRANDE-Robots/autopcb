#!/usr/bin/env python3
"""Run Jessie project tools without leaving Python caches in the project."""


def main() -> int:
    import sys

    sys.dont_write_bytecode = True
    from core.launcher import main as launch

    return launch()


if __name__ == "__main__":
    raise SystemExit(main())
