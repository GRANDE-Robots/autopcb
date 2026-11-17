"""Run the pinned KRT release with a safe temporary-cache transform fix.

KRT's rescue map contains non-copyable native objects and is invalid after a
coordinate transform.  Drop it from a shallow private board copy before KRT
deep-copies and transforms that board; KRT rebuilds the cache when needed.
The official checkout stays unchanged and all produced copper is unapproved.
"""

from __future__ import annotations

import copy
from functools import wraps
import os
from pathlib import Path
import runpy
import sys


def uncached_transform(transform):
    """Keep the source board/cache untouched while clearing the transformed copy."""
    if getattr(transform, "_autopcb_uncached_transform", False):
        return transform

    @wraps(transform)
    def wrapped(board, *args, **kwargs):
        if not hasattr(board, "_rescue_map_cache"):
            return transform(board, *args, **kwargs)
        temporary = copy.copy(board)
        delattr(temporary, "_rescue_map_cache")
        return transform(temporary, *args, **kwargs)

    wrapped._autopcb_uncached_transform = True
    return wrapped


def main() -> None:
    root = Path(os.environ["AUTOPCB_KRT_ROOT"]).resolve()
    differential = os.environ.get("AUTOPCB_KRT_DIFFERENTIAL", "0")
    if differential not in {"0", "1"}:
        raise ValueError("Invalid KRT differential-router selection")
    source = root / "py_router" / ("route_diff.py" if differential == "1" else "route.py")
    if not source.is_file():
        raise RuntimeError("The pinned KRT entry point is unavailable")
    sys.path.insert(0, str(root / "py_router"))
    sys.path.insert(1, str(root / "rust_router"))
    import rust_alloc  # noqa: F401  # Set allocator policy before importing grid_router.
    from bga_fanout import flip_frame, rotate_frame

    flip_frame.to_front_frame = uncached_transform(flip_frame.to_front_frame)
    rotate_frame.to_axis_aligned_frame = uncached_transform(rotate_frame.to_axis_aligned_frame)
    sys.argv[0] = str(source)
    runpy.run_path(str(source), run_name="__main__")


if __name__ == "__main__":
    main()
