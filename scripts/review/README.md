# review

Renders, packages and records the review gallery and the routing progress from saved KiCad snapshots.

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`__init__.py`](__init__.py) | The review package, which renders, records and publishes the review gallery and routing progress. | None | `presentation`, `refresh_gallery`, `routing_progress`, `core` (2), `pcb/net_routing` (1) |
| [`build_standalone_gallery.py`](build_standalone_gallery.py) | Embed every gallery view once in one shareable HTML; preserve the gallery. | None | `refresh_gallery`, `core` (1) |
| [`geometry_metrics.py`](geometry_metrics.py) | Measure routing geometry from one immutable KiCad board snapshot. | `core/pcb_io`, `pcbnew` | `routing_progress`, `pcb/net_routing` (2) |
| [`package_review.py`](package_review.py) | Package a generated review folder, verifying current source and every file. | `core/export_helpers`, `core/paths` | `core` (1) |
| [`presentation.py`](presentation.py) | Concise saved-board progress and review-gallery summaries for the terminal. | `__init__`, `core/console` | `refresh_gallery`, `routing_progress` |
| [`rasterize_gallery.py`](rasterize_gallery.py) | Render native plots and assemble labelled, phone-friendly view sheets. | `PIL`, `pypdfium2` | `refresh_gallery`, `core` (1) |
| [`refresh_gallery.py`](refresh_gallery.py) | Regenerate the one active review gallery directly from one stable KiCad source pair. | `__init__`, `build_standalone_gallery`, `core/console`, `core/export_helpers`, `core/paths`, `core/project`, `presentation`, `rasterize_gallery`, `render_views` | `render_views`, `routing_progress`, `core` (1), `pcb/net_routing` (1) |
| [`render_views.py`](render_views.py) | Fresh views from one unchanged native source snapshot, using the selected CLI. | `core/export_helpers`, `core/paths`, `core/toolchain`, `pcb`, `refresh_gallery`, `sch`, `pcbnew` | `refresh_gallery`, `core` (1) |
| [`routing_progress.py`](routing_progress.py) | Record and plot KiCad checks and routing geometry from saved snapshots. | `__init__`, `core/compat`, `core/console`, `core/paths`, `core/project`, `core/toolchain`, `geometry_metrics`, `pcb/net_routing/controller`, `pcb/net_routing/native`, `pcb/net_routing/protection`, 6 more | `core` (1), `pcb/net_routing` (1), `verification/test_routing_progress.py` |
