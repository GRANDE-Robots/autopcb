"""Measure selected PCB route segments using a matching board-audit snapshot.

The input folder must contain the board audit's native review and geometry;
results exclude zone paths and do not establish electrical performance.
"""

import argparse
import collections
import heapq
import json
import math
from pathlib import Path

import pcbnew

from core.export_helpers import assert_current_inputs, sha


def load_current_geometry(project, output_dir):
    """Accept geometry only when its recorded board and bytes still match."""
    review_path = output_dir / "pcb_native_review.json"
    geometry_path = output_dir / "pcb_geometry_detail.json"
    review = json.loads(review_path.read_text(encoding="utf-8-sig"))
    source = review.get("source_sha256")
    if (
        review.get("source_unchanged") is not True
        or not isinstance(source, dict)
        or len([name for name in source if name.lower().endswith(".kicad_pcb")]) != 1
    ):
        raise RuntimeError(
            "Stale or incomplete native PCB review; rerun the board audit"
        )
    try:
        assert_current_inputs(project, source, {})
    except (OSError, ValueError, RuntimeError) as error:
        raise RuntimeError("Stale native PCB review; rerun the board audit") from error
    if review.get("geometry_sha256") != sha(geometry_path):
        raise RuntimeError(
            "PCB geometry changed or lacks provenance; rerun the board audit"
        )
    geometry = json.loads(geometry_path.read_text(encoding="utf-8-sig"))
    return review, geometry


def path_specs():
    """List individual net segments; switched USB paths stop on each side of U28."""
    specs = [
        ("TEENSY_HOST_USB_DP", ("J28", "A6"), ("U28", "8")),
        ("TEENSY_HOST_USB_DM", ("J28", "A7"), ("U28", "7")),
        ("TEENSY_HOST_USB_DP", ("J28", "B6"), ("U28", "8")),
        ("TEENSY_HOST_USB_DM", ("J28", "B7"), ("U28", "7")),
        ("TEENSY_USB_DP", ("U28", "1"), ("CA1", "3")),
        ("TEENSY_USB_DM", ("U28", "2"), ("CA1", "2")),
        ("IMU_USB_DP", ("J30", "A6"), ("J9", "3")),
        ("IMU_USB_DM", ("J30", "A7"), ("J9", "6")),
        ("IMU_USB_DP", ("J30", "B6"), ("J9", "3")),
        ("IMU_USB_DM", ("J30", "B7"), ("J9", "6")),
    ]
    for x, a, c in [(1, "J22", "J23"), (2, "J24", "J25")]:
        for sig, sp, ep in [
            ("TX_P", "1", "1"),
            ("TX_N", "2", "2"),
            ("RX_P", "5", "3"),
            ("RX_N", "4", "6"),
        ]:
            specs.append((f"L{x}_ETH_{sig}", (a, sp), (c, ep)))
    for n, a, c in [
        ("LIGHTS_12_RAW", ("J1", "3"), ("Q7", "5")),
        ("LIGHTS_12V", ("Q8", "5"), ("F3", "1")),
        ("LIGHTS_12V", ("Q8", "5"), ("F4", "1")),
        ("TOP_LAMP_PLUS", ("R73", "2"), ("J12", "1")),
        ("FRONT_LAMP_PLUS", ("R83", "2"), ("J12", "3")),
        ("TOP_LAMP_MINUS", ("J12", "2"), ("Q2", "5")),
        ("FRONT_LAMP_MINUS", ("J12", "4"), ("Q3", "5")),
    ]:
        specs.append((n, a, c))
    specs.append(("LIDAR_12_RAW", ("J1", "5"), ("Q5", "5")))
    specs.extend(("LIDAR_12_PROT", ("Q6", "5"), (f"F{i}", "1")) for i in range(5, 9))
    return specs


def load_path_specs(path: Path):
    """Read explicit net and pad pairs for another board or audit scope."""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or set(data) != {"paths"}:
        raise ValueError("Path configuration must contain only a paths list")
    rows = data["paths"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("Path configuration must contain at least one path")
    result = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"net", "from", "to"}:
            raise ValueError("Each path needs net, from and to")
        net, first, last = row["net"], row["from"], row["to"]
        if not isinstance(net, str) or not net.strip():
            raise ValueError("Each path needs a nonempty net name")
        for endpoint in (first, last):
            if (
                not isinstance(endpoint, list)
                or len(endpoint) != 2
                or any(
                    not isinstance(part, str) or not part.strip() for part in endpoint
                )
            ):
                raise ValueError("Each endpoint must be [reference, pad number]")
        if first == last:
            raise ValueError("Path endpoints must be distinct")
        result.append((net, tuple(first), tuple(last)))
    return result


def validate_path_specs(specs, pad_nets):
    """Reject duplicate or stale paths before reporting route lengths."""
    normalized = [(net, *sorted((first, last))) for net, first, last in specs]
    if len(normalized) != len(set(normalized)):
        raise ValueError("Duplicate path specification")
    for net, first, last in specs:
        for endpoint in (first, last):
            actual = pad_nets.get(endpoint)
            if actual != net:
                raise ValueError(
                    f"{endpoint[0]}.{endpoint[1]} is on {actual!r}; expected {net!r}"
                )


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            'Example --paths-file JSON: {"paths": [{"net": "SIGNAL", '
            '"from": ["J1", "1"], "to": ["U1", "2"]}]}'
        ),
    )
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument(
        "--manufacturing", type=Path, required=True, help="generated review folder"
    )
    ap.add_argument(
        "--out", type=Path, required=True, help="existing external board-audit folder"
    )
    ap.add_argument(
        "--paths-file",
        type=Path,
        help="JSON net and endpoint pairs; defaults to Jessie paths",
    )
    a = ap.parse_args()
    P = a.project.resolve()
    M = a.manufacturing.resolve()
    output_dir = a.out.resolve()
    output_file = output_dir / "pcb_paths.json"
    if output_dir.is_relative_to(P) or output_dir.is_relative_to(M):
        raise RuntimeError(
            "Keep PCB path reports outside the project and manufacturing folders"
        )
    if not output_dir.is_dir() or output_file.exists():
        raise RuntimeError(
            "Use the existing audit folder with a new pcb_paths.json output"
        )
    review, d = load_current_geometry(P, output_dir)
    board_name = next(
        name for name in review["source_sha256"] if name.lower().endswith(".kicad_pcb")
    )
    board_path = (P / board_name).resolve()
    if not board_path.is_relative_to(P) or not board_path.is_file():
        raise RuntimeError(
            "PCB review refers to an unavailable or out-of-project board"
        )
    b = pcbnew.LoadBoard(str(board_path))
    # The centreline graph below handles straight segments only.  Check the
    # native board too, so older geometry snapshots cannot hide curved tracks.
    arcs = [item for item in b.GetTracks() if isinstance(item, pcbnew.PCB_ARC)]
    arc_nets = {item.GetNetname() for item in arcs}
    arc_ids = {item.m_Uuid.AsString() for item in arcs}
    tracks = d["tracks"]
    vias = d["vias"]
    pads = d["pads"]
    nativepads = {
        (f.GetReference(), p.GetNumber()): p
        for f in b.GetFootprints()
        for p in f.Pads()
    }

    def dist(a, c):
        return math.hypot(a[0] - c[0], a[1] - c[1])

    def project(p, a, c):
        dx, dy = c[0] - a[0], c[1] - a[1]
        s = dx * dx + dy * dy
        t = max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / s)) if s else 0
        return t, [a[0] + t * dx, a[1] + t * dy]

    def key(p, layer):
        return (round(p[0], 6), round(p[1], 6), layer)

    def path(net, first, last):
        if net in arc_nets:
            return {
                "net": net,
                "first": first,
                "last": last,
                "not_found": None,
                "status": "unsupported",
                "reason": "A track arc exists on this net; curved path lengths are not supported.",
            }
        tt = [t for t in tracks if t["net"] == net]
        vv = [v for v in vias if v["net"] == net]
        pp = [p for p in pads if p["net"] == net]
        pts = collections.defaultdict(set)
        adj = collections.defaultdict(list)

        def add(a, c, w, kind, meta):
            adj[a].append((c, w, kind, meta))
            adj[c].append((a, w, kind, meta))

        for t in tt:
            for p in [t["start"], t["end"]]:
                pts[t["layer"]].add(tuple(p))
        for v in vv:
            for layer in v["layers"]:
                pts[layer].add(tuple(v["at"]))
        for p in pp:
            for layer in p["layers"]:
                pts[layer].add(tuple(p["at"]))
        for t in tt:
            a, c = t["start"], t["end"]
            layer = t["layer"]
            on = [(0, a), (1, c)]
            for q in list(pts[layer]):
                u, r = project(q, a, c)
                # Exactly connected centreline junctions and copper-overlap centres.
                if dist(q, r) < 1e-5:
                    on.append((u, list(q)))
            on = sorted({(round(u, 10), tuple(p)) for u, p in on})
            for (_, a), (_, c) in zip(on, on[1:]):
                add(
                    key(a, layer),
                    key(c, layer),
                    dist(a, c),
                    "track",
                    {
                        "width": t["width"],
                        "layer": layer,
                        "uuid": t["uuid"],
                        "start": a,
                        "end": c,
                    },
                )
        for v in vv:
            q = v["at"]
            ls = v["layers"]
            for layer in ls:
                for p in list(pts[layer]):
                    if dist(q, p) <= v["width"] / 2 + 0.000001:
                        add(
                            key(q, layer),
                            key(p, layer),
                            dist(q, p),
                            "via_pad",
                            v["uuid"],
                        )
            for a, c in zip(ls, ls[1:]):
                add(key(q, a), key(q, c), 0, "via", v["uuid"])
        for p in pp:
            np = nativepads[(p["ref"], p["pin"])]
            q = p["at"]
            ls = p["layers"]
            for layer in ls:
                for r in list(pts[layer]):
                    if np.HitTest(
                        pcbnew.VECTOR2I(round(r[0] * 1e6), round(r[1] * 1e6))
                    ):
                        add(
                            key(q, layer),
                            key(r, layer),
                            dist(q, r),
                            "pad",
                            p["ref"] + "." + p["pin"],
                        )
                for v in vv:
                    if layer in v["layers"] and np.HitTest(
                        pcbnew.VECTOR2I(
                            round(v["at"][0] * 1e6), round(v["at"][1] * 1e6)
                        ),
                        round(v["width"] * 5e5),
                    ):
                        add(
                            key(q, layer),
                            key(v["at"], layer),
                            dist(q, v["at"]),
                            "via_pad_overlap",
                            p["ref"] + "." + p["pin"],
                        )
            if p["drill"] != [0, 0]:
                for a, c in zip(ls, ls[1:]):
                    add(
                        key(q, a), key(q, c), 0, "plated_pad", p["ref"] + "." + p["pin"]
                    )
        sources = [
            key(p["at"], layer)
            for p in pp
            if (p["ref"], p["pin"]) == first
            for layer in p["layers"]
        ]
        targets = {
            key(p["at"], layer)
            for p in pp
            if (p["ref"], p["pin"]) == last
            for layer in p["layers"]
        }
        heap = [(0, x) for x in sources]
        heapq.heapify(heap)
        cost = {x: 0 for x in sources}
        prev = {}
        end = None
        while heap:
            v, x = heapq.heappop(heap)
            if v != cost[x]:
                continue
            if x in targets:
                end = x
                break
            for y, w, k, m in adj[x]:
                nv = v + w
                if nv < cost.get(y, 1e30) - 1e-9:
                    cost[y] = nv
                    prev[y] = (x, w, k, m)
                    heapq.heappush(heap, (nv, y))
        if end is None:
            return {"net": net, "first": first, "last": last, "not_found": True}
        out = []
        e = end
        while e in prev:
            x, w, k, m = prev[e]
            out.append({"kind": k, "length_mm": w, "meta": m, "start": x, "end": e})
            e = x
        out.reverse()
        layerlength = collections.Counter()
        widthlength = collections.Counter()
        for x in out:
            if x["kind"] == "track":
                layerlength[x["meta"]["layer"]] += x["length_mm"]
                widthlength[str(x["meta"]["width"])] += x["length_mm"]
        return {
            "net": net,
            "first": first,
            "last": last,
            "approx_centerline_length_mm": cost[end],
            "layer_track_lengths_mm": dict(layerlength),
            "width_track_lengths_mm": dict(widthlength),
            "unique_route_vias": list(
                dict.fromkeys(x["meta"] for x in out if x["kind"] == "via")
            ),
            "segments": out,
        }

    paths_file = a.paths_file.resolve() if a.paths_file else None
    paths_hash = sha(paths_file) if paths_file else None
    specs = load_path_specs(paths_file) if paths_file else path_specs()
    validate_path_specs(
        specs, {key: pad.GetNetname() for key, pad in nativepads.items()}
    )
    paths = [path(*spec) for spec in specs]
    dup = collections.defaultdict(list)
    for t in tracks:
        if t["uuid"] in arc_ids:
            continue
        dup[
            (
                t["net"],
                t["layer"],
                t["width"],
                *sorted([tuple(t["start"]), tuple(t["end"])]),
            )
        ].append(t["uuid"])
    dups = [
        {
            "net": k[0],
            "layer": k[1],
            "width": k[2],
            "start": k[3],
            "end": k[4],
            "uuids": v,
        }
        for k, v in dup.items()
        if len(v) > 1
    ]
    summary = {
        "source_sha256": review["source_sha256"],
        "geometry_sha256": review["geometry_sha256"],
        "path_spec_sha256": paths_hash,
        "method": (
            "Shortest geometrical centreline graph through native pad "
            "shapes and via centres. Does not model electric delay, "
            "current spreading, branch/stub impedance or zone paths. "
            "Nets with curved tracks are marked unsupported rather than measured as chords. "
            "Duplicate straight track geometry is not counted twice in shortest "
            "paths. Via barrel lengths excluded."
        ),
        "paths": paths,
        "exact_duplicate_track_groups": dups,
        "zone_connection_constants": {
            x: getattr(pcbnew, x)
            for x in dir(pcbnew)
            if x.startswith("ZONE_CONNECTION_")
        },
    }
    current_review, current_geometry = load_current_geometry(P, output_dir)
    if current_review != review or current_geometry != d:
        raise RuntimeError(
            "PCB audit inputs changed during path analysis; discard the result"
        )
    if paths_file and sha(paths_file) != paths_hash:
        raise RuntimeError(
            "Path configuration changed during analysis; discard the result"
        )
    with output_file.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(summary, handle, indent=2)
        handle.write("\n")
    for p in paths:
        print(json.dumps({k: v for k, v in p.items() if k != "segments"}))
    print("DUPLICATES", json.dumps(dups, indent=2))
    print(summary["zone_connection_constants"])


if __name__ == "__main__":
    main()
