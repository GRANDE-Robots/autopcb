"""drc_inventory.py: exhaust KiCad's capped width checks on disposable copies.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import json
from pathlib import Path
import uuid

from core.kicad_sexpr import parse


WIDTH_LIMIT = 199  # KiCad 10.0.6 drc_engine.cpp ERROR_LIMIT.


class WidthReportExpansion:
    """Completes KiCad's capped track-width findings by rechecking disposable copies."""

    def __call__(self, board: Path, report: dict, check) -> dict:
        """Partition width checking only; never suppress a check in saved inputs.

        One temporary group identifies already-reported items for subsequent passes.
        All physical copper, net identities, other checks and native rules stay intact.
        The first unmodified report supplies the other findings. This compensates for
        KiCad's fixed 199-per-type reporting cap without assuming the count is total.
        """
        from pcb.net_routing.native import Snapshot

        batch = [v for v in report["violations"] if v["type"] == "track_width"]
        if len(batch) < WIDTH_LIMIT:
            return report
        found = {}
        with Snapshot(board) as snapshot:
            source = snapshot.original.decode("utf-8")
            root = parse(source)
            identifiers = {
                f.children("uuid")[0].atoms()[1].value
                for f in root.children()
                if f.name in {"segment", "arc"}
            }
            if len(identifiers) > 65536:
                raise RuntimeError("Width inventory exceeds its supported item bound")
            rules_path = snapshot.board_path.with_suffix(".kicad_dru")
            rules = (
                rules_path.read_text(encoding="utf-8")
                if rules_path.is_file()
                else "(version 1)\n"
            )
            name = "autopcb-width-inventory-" + uuid.uuid4().hex
            rules_path.write_text(
                rules + f'\n(rule "{name}" (condition "A.memberOfGroup(\'{name}\')") '
                "(constraint track_width (min 0mm) (max 1000mm)))\n",
                encoding="utf-8",
            )
            output = snapshot.directory / "width-scan.json"
            passes = 1
            while True:
                previous = len(found)
                for finding in batch:
                    items = finding.get("items", [])
                    if len(items) != 1 or items[0].get("uuid") not in identifiers:
                        raise RuntimeError(
                            "Width inventory cannot identify the violating copper"
                        )
                    identifier = items[0]["uuid"]
                    if identifier in found:
                        raise RuntimeError(
                            "Width inventory did not exclude an already checked item"
                        )
                    found[identifier] = finding
                if len(batch) < WIDTH_LIMIT:
                    break
                if len(found) == previous:
                    raise RuntimeError("Width inventory stopped making progress")
                members = " ".join(
                    '"' + identifier + '"' for identifier in sorted(found)
                )
                group_id = str(uuid.UUID(name[-32:]))
                group = f'\n(group "{name}" (uuid "{group_id}") (members {members}))\n'
                candidate = source[: root.end - 1] + group + source[root.end - 1 :]
                snapshot.board_path.write_text(candidate, encoding="utf-8")
                output.unlink(missing_ok=True)
                more = check(snapshot.board_path, output)
                batch = [v for v in more["violations"] if v["type"] == "track_width"]
                passes += 1
            # The intentional temporary DRU/group never reach the supplied board.
        expanded = dict(report)
        expanded["violations"] = [
            v for v in report["violations"] if v["type"] != "track_width"
        ] + [found[key] for key in sorted(found)]
        expanded["width_inventory"] = {
            "complete": True,
            "passes": passes,
            "native_per_pass_limit": WIDTH_LIMIT,
        }
        json.dumps(expanded, allow_nan=False)
        return expanded


expand_width_report = WidthReportExpansion()
