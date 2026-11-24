"""audit_pcb_routing_style.py: summarize saved-PCB route lengths, vias and bends.

The measurements do not assess design-rule clearance or electrical performance.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import argparse
import collections
import hashlib
import json
import math
from pathlib import Path

import pcbnew
from core.pcb_io import copper_width


class RoutingStyleAudit:
    """Measure the tracks, vias and bends of a saved board."""

    @staticmethod
    def key(q):
        """Return the (x, y) pair of a pcbnew point in nm."""
        return q.x, q.y

    def __call__(self, board_path):
        """Audit one saved board.

        Inputs:
            board_path: Saved ``.kicad_pcb`` file.
        Outputs:
            The report: board hash, method note, metrics, off-45-degree segments,
            right-angle candidates and collinear merge candidates. Lengths are in
            mm and angles in degrees. Raises RuntimeError when the board file
            changes during the audit.
        """
        board_sha256 = hashlib.sha256(board_path.read_bytes()).hexdigest()
        b = pcbnew.LoadBoard(str(board_path))
        tracks = [t for t in b.GetTracks() if not isinstance(t, pcbnew.PCB_VIA)]
        segments = [track for track in tracks if not isinstance(track, pcbnew.PCB_ARC)]
        vias = [v for v in b.GetTracks() if isinstance(v, pcbnew.PCB_VIA)]
        pads = [q for f in b.GetFootprints() for q in f.Pads()]

        ends = collections.defaultdict(list)
        non45 = []
        for t in segments:
            for q in [t.GetStart(), t.GetEnd()]:
                ends[(t.GetNetCode(), t.GetLayer(), *self.key(q))].append(t)
            s = t.GetStart()
            e = t.GetEnd()
            dx = abs(e.x - s.x)
            dy = abs(e.y - s.y)
            if min(dx, dy) > 2000 and abs(dx - dy) > 2000:
                non45.append(
                    dict(
                        uuid=t.m_Uuid.AsString(),
                        net=t.GetNetname(),
                        layer=b.GetLayerName(t.GetLayer()),
                        length_mm=t.GetLength() / 1e6,
                        angle=math.degrees(math.atan2(dy, dx)),
                    )
                )
        corners = []
        collinear = []
        degree_two_joints = 0
        for k, ts in ends.items():
            if len(ts) != 2:
                continue
            n, layer, x, y = k
            at = pcbnew.VECTOR2I(x, y)
            if any(
                q.GetNetCode() == n and q.IsOnLayer(layer) and q.HitTest(at)
                for q in pads
            ):
                continue
            if any(
                v.GetNetCode() == n
                and math.hypot(v.GetPosition().x - x, v.GetPosition().y - y)
                <= copper_width(v, layer=pcbnew.F_Cu) / 2
                for v in vias
            ):
                continue
            vectors = []
            for t in ts:
                q = t.GetEnd() if self.key(t.GetStart()) == (x, y) else t.GetStart()
                vectors.append((q.x - x, q.y - y))
            u, v = vectors
            d = math.hypot(*u) * math.hypot(*v)
            if not d:
                continue
            degree_two_joints += 1
            angle = math.degrees(
                math.acos(max(-1, min(1, (u[0] * v[0] + u[1] * v[1]) / d)))
            )
            row = dict(
                net=ts[0].GetNetname(),
                layer=b.GetLayerName(layer),
                at_mm=[x / 1e6, y / 1e6],
                angle=angle,
                uuids=[t.m_Uuid.AsString() for t in ts],
                widths_mm=[copper_width(t) / 1e6 for t in ts],
            )
            if abs(angle - 90) < 0.1:
                corners.append(row)
            if abs(angle - 180) < 0.1 and copper_width(ts[0]) == copper_width(ts[1]):
                collinear.append(row)
        if hashlib.sha256(board_path.read_bytes()).hexdigest() != board_sha256:
            raise RuntimeError("PCB changed while checking routing; discard the result")
        layer_lengths = collections.defaultdict(float)
        for track in tracks:
            layer_lengths[b.GetLayerName(track.GetLayer())] += track.GetLength() / 1e6
        metrics = {
            "track_segments": len(tracks),
            "straight_segments": len(segments),
            "arc_segments": len(tracks) - len(segments),
            "track_length_mm": round(sum(layer_lengths.values()), 6),
            "track_length_mm_by_layer": {
                layer: round(length, 6)
                for layer, length in sorted(layer_lengths.items())
            },
            "vias": len(vias),
            "degree_two_joints": degree_two_joints,
        }
        report = {
            "source": str(board_path),
            "sha256": board_sha256,
            "method": (
                "Track length includes straight and arc copper segments, not zones or via barrels. "
                "Joint candidates are degree-two straight-segment endpoints outside same-net pads "
                "and vias. Interior branches and arc junctions require separate review. "
                "These counts do not establish clearance, current capacity, or signal integrity."
            ),
            "metrics": metrics,
            "non_45_segments": non45,
            "right_angle_candidates": corners,
            "collinear_merge_candidates": collinear,
        }
        return report


class RoutingStyleCommand:
    """Command-line entry: print the routing-style measurements of a saved board."""

    def __call__(self, argv=None):
        """Run the audit.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            None; prints the full report with --json, otherwise the metrics and the
            candidate counts. Writes a new report file when --out is given. Raises
            RuntimeError when --out is inside the project folder, exists or has no
            parent folder.
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("board", type=Path)
        parser.add_argument(
            "--out", type=Path, help="optional new report file outside the project"
        )
        parser.add_argument(
            "--json", action="store_true", help="print the complete report"
        )
        args = parser.parse_args(argv)
        p = args.board.resolve()
        out = args.out.resolve() if args.out else None
        if out is not None and (
            out.is_relative_to(p.parent) or out.exists() or not out.parent.is_dir()
        ):
            raise RuntimeError("Report must be a new file outside the PCB project")
        report = RoutingStyleAudit()(p)
        if out is not None:
            with out.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(report, handle, indent=2)
                handle.write("\n")
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(
                json.dumps(
                    {
                        **report["metrics"],
                        "non_45_segments": len(report["non_45_segments"]),
                        "right_angle_candidates": len(report["right_angle_candidates"]),
                        "collinear_merge_candidates": len(
                            report["collinear_merge_candidates"]
                        ),
                    },
                    indent=2,
                )
            )


main = RoutingStyleCommand()

if __name__ == "__main__":
    main()
