"""reference_repair.py: build conservative repair requests from sampled high-speed return gaps.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from pcb.net_routing.controller import make_work_plan
from pcb.net_routing.protection import unqualified_working_nets


class ReferenceRepairCompiler:
    """Turns return-path conflicts into routing requests that rip up only safe blockers."""

    def __call__(
        self, board, reference: dict, policy: dict, *, workers: int = 2
    ) -> dict:
        """Identify removable obstacles by UUID; final native validation is mandatory."""
        critical = set(reference.get("high_speed_nets", []))
        unqualified = unqualified_working_nets(
            board, tuple(policy.get("exclude_nets", ()))
        )
        missing = {
            item["item_id"]
            for item in reference.get("sampled_tracks", [])
            if item.get("missing_samples", 0)
            or not item.get("continuous_polygon", False)
        }
        tracks = {item.m_Uuid.AsString(): item for item in board.GetTracks()}
        selected: dict[str, dict[str, str]] = {}
        blockers = []
        for conflict in reference.get("conservative_conflicts", []):
            if conflict.get("high_speed_item_id") not in missing:
                continue
            uid, net, kind = (
                conflict.get("blocking_item_id"),
                conflict.get("blocking_net"),
                conflict.get("blocking_kind"),
            )
            item = tracks.get(uid)
            if kind not in {"track", "via", "arc"} or item is None:
                blockers.append(f"Reference conflict {uid}: no editable track or via")
            elif (
                not net
                or net in critical
                or net in unqualified
                or net == "GND"
                or net.startswith("unconnected-")
                or item.GetNetname() != net
            ):
                blockers.append(
                    f"Reference conflict {uid}: net requires separate review"
                )
            elif item.IsLocked() or item.GetParentGroup() is not None:
                blockers.append(
                    f"Reference conflict {uid}: copper is locked or grouped"
                )
            else:
                selected.setdefault(net, {})[uid] = {
                    "track": "segment",
                    "via": "via",
                    "arc": "arc",
                }[kind]
        bounds = board.GetBoardEdgesBoundingBox()
        board_window = (
            bounds.GetX() / 1e6,
            bounds.GetY() / 1e6,
            bounds.GetRight() / 1e6,
            bounds.GetBottom() / 1e6,
        )
        margin = policy.get("search_margin_mm") or 8.0
        items = []
        for net in sorted(selected):
            pads = [pad for pad in board.GetPads() if pad.GetNetname() == net]
            copper = [track for track in board.GetTracks() if track.GetNetname() == net]
            if not pads:
                blockers.append(
                    f"Reference conflict on {net}: no schematic-connected pad"
                )
                continue
            boxes = [item.GetBoundingBox() for item in (*pads, *copper)]
            window = (
                max(board_window[0], min(box.GetX() for box in boxes) / 1e6 - margin),
                max(board_window[1], min(box.GetY() for box in boxes) / 1e6 - margin),
                min(
                    board_window[2], max(box.GetRight() for box in boxes) / 1e6 + margin
                ),
                min(
                    board_window[3],
                    max(box.GetBottom() for box in boxes) / 1e6 + margin,
                ),
            )
            items.append(
                {
                    "name": net,
                    "priority": policy.get("priorities", {}).get(net, 100),
                    "window": window,
                    "island_count": 2,
                }
            )
        work = {
            "work": {"items": items},
            "reference_copper": {"high_speed_nets": sorted(critical)},
            "unqualified_nets": sorted(unqualified),
        }
        plan = make_work_plan(work, policy, workers=workers)
        for round_ in plan["rounds"]:
            for request in round_:
                request["rip_up"] = {
                    entry["name"]: [
                        {"kind": kind, "uuid": uid}
                        for uid, kind in sorted(selected[entry["name"]].items())
                    ]
                    for entry in request["nets"]
                }
        return {**plan, "blockers": sorted(set(blockers))}


compile_reference_repairs = ReferenceRepairCompiler()
