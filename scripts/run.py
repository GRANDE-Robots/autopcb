#!/usr/bin/env python3
"""run.py: run AutoPCB project tools without leaving Python caches in the project.

Copyright: GRANDE Robots and Aaron John Sabu
"""


class Run:
    """Command-line entry: hands the command line to the launcher."""

    def __call__(self) -> int:
        """Run the launcher with bytecode writing off.

        Outputs: The launcher's exit status.
        """
        import sys

        sys.dont_write_bytecode = True
        from core.launcher import main as launch

        return launch()


main = Run()

if __name__ == "__main__":
    raise SystemExit(main())
