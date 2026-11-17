"""Concise saved-board progress and export summaries."""

from core.console import Console, duration


def show_progress(console: Console, result: dict) -> None:
    states = {
        "unchanged": "Current; no files rewritten",
        "recorded": "New measurement recorded",
        "metrics_added": "Measurements added",
        "chart_restored": "Chart restored",
        "redrawn": "Chart redrawn from recorded history",
        "backfilled": "History updated",
    }
    console.heading("AutoPCB | Routing progress")
    console.rows(
        [
            (
                "Progress page",
                states.get(result["status"], result["status"].replace("_", " ")),
            ),
            ("Saved board opens", result.get("open_connections")),
            ("Private preview opens", result.get("preview_opens")),
            ("Other DRC errors", result.get("other_errors")),
            ("DRC warnings", result.get("other_warnings")),
            (
                "Track length",
                f"{result['track_length_mm']:,.2f} mm"
                if result.get("track_length_mm") is not None
                else None,
            ),
            ("Vias", result.get("via_count")),
            ("Track joints", result.get("degree_two_joints")),
            ("Recorded measurements", result.get("points")),
        ]
    )
    if result.get("plot"):
        console.line("Chart: " + result["plot"])
    if console.verbose:
        console.rows(
            [
                ("Symmetry score", result.get("symmetry_score")),
                (
                    "Routing runtime",
                    duration(result["routing_run_seconds"])
                    if result.get("routing_run_seconds") is not None
                    else None,
                ),
                ("Table", result.get("table")),
            ]
        )


def show_gallery(console: Console, result: dict) -> None:
    if result.get("status") == "unchanged":
        console.heading("AutoPCB | Review files are current")
        console.line("Saved design and tracked dependencies match the existing views.")
        console.line("Folder: " + result["review"])
        return
    console.heading("AutoPCB | Review files updated")
    console.rows(
        [
            ("PDFs", result["pdfs_refreshed"]),
            ("PNGs", result["pngs_refreshed"]),
            ("Gallery", "Updated, including standalone view"),
            ("Saved sources", "Unchanged throughout rendering"),
        ]
    )
    console.line("Folder: " + result["review"])
