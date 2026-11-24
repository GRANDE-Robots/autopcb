"""krt_entry.py: run the pinned KRT release with a safe temporary-cache transform fix.

KRT's rescue map contains non-copyable native objects and is invalid after a
coordinate transform.  Drop it from a shallow private board copy before KRT
deep-copies and transforms that board; KRT rebuilds the cache when needed.
The official checkout stays unchanged and all produced copper is unapproved.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import copy
from functools import wraps
import os
from pathlib import Path
import runpy
import sys


class KrtEntry:
    """Entry point that patches KRT's coordinate transforms and then runs its router."""

    @staticmethod
    def uncached_transform(transform):
        """Wrap a KRT transform so it never touches the source board's rescue cache.

        Inputs:
            transform: A callable whose first argument is the board.
        Outputs:
            A wrapper that passes a shallow copy without the rescue map cache when
            the board has one, and passes the board itself otherwise. A transform that
            is already wrapped is returned unchanged.
        """
        if getattr(transform, "_autopcb_uncached_transform", False):
            return transform

        @wraps(transform)
        def wrapped(board, *args, **kwargs):
            """Call the transform on a cache-free shallow copy of the board."""
            if not hasattr(board, "_rescue_map_cache"):
                return transform(board, *args, **kwargs)
            temporary = copy.copy(board)
            delattr(temporary, "_rescue_map_cache")
            return transform(temporary, *args, **kwargs)

        wrapped._autopcb_uncached_transform = True
        return wrapped

    def __call__(self) -> None:
        """Run the pinned KRT router script under the patched transforms.

        Inputs:
            Environment variables AUTOPCB_KRT_ROOT (the pinned checkout) and
            AUTOPCB_KRT_DIFFERENTIAL ("0" for single nets or "1" for paired routes).
        Outputs:
            Returns after the router script finishes. Raises ValueError for another
            differential value and RuntimeError when the router script is missing.
        """
        root = Path(os.environ["AUTOPCB_KRT_ROOT"]).resolve()
        differential = os.environ.get("AUTOPCB_KRT_DIFFERENTIAL", "0")
        if differential not in {"0", "1"}:
            raise ValueError("Invalid KRT differential-router selection")
        source = (
            root
            / "py_router"
            / ("route_diff.py" if differential == "1" else "route.py")
        )
        if not source.is_file():
            raise RuntimeError("The pinned KRT entry point is unavailable")
        sys.path.insert(0, str(root / "py_router"))
        sys.path.insert(1, str(root / "rust_router"))
        import rust_alloc  # noqa: F401  # Set allocator policy before importing grid_router.
        from bga_fanout import flip_frame, rotate_frame

        flip_frame.to_front_frame = KrtEntry.uncached_transform(
            flip_frame.to_front_frame
        )
        rotate_frame.to_axis_aligned_frame = KrtEntry.uncached_transform(
            rotate_frame.to_axis_aligned_frame
        )
        sys.argv[0] = str(source)
        runpy.run_path(str(source), run_name="__main__")


main = KrtEntry()


if __name__ == "__main__":
    main()
