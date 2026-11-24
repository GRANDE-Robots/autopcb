"""audit_pcb_manufacturing.py: compare native PCB geometry with a manufacturing folder.

Writes new source-bound JSON reports only outside the source and manufacturing
folders. This is a consistency check, not a fabrication approval.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import argparse
import collections
import csv
import hashlib
import json
import zipfile
from pathlib import Path

import pcbnew
from core.pcb_io import copper_width


class ReviewFiles:
    """Hash and read the files of a board review."""

    @staticmethod
    def sha(p):
        """Return the SHA-256 hex digest of a file."""
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()

    @staticmethod
    def read(p):
        """Read a CSV file with a header row into one dict per data row."""
        return list(csv.DictReader(Path(p).open(encoding="utf-8-sig", newline="")))


class PcbUnits:
    """Convert pcbnew nanometre values to millimetres."""

    @staticmethod
    def mm(v):
        """Convert a length in nm to mm, rounded to six decimals."""
        return round(v / 1e6, 6)

    @classmethod
    def xy(cls, p):
        """Convert a pcbnew point in nm to an [x, y] list in mm."""
        return [cls.mm(p.x), cls.mm(p.y)]


class ManufacturingAudit:
    """Compare the saved board with the files of a generated manufacturing folder."""

    def __init__(self, project, board, manufacturing, out):
        """Resolve and check the board, settings and output locations.

        Inputs:
            project: Project folder.
            board: Board in the project root; None selects the only board.
            manufacturing: Generated review folder.
            out: New report folder outside the project and manufacturing folders.
        Outputs:
            None. Raises RuntimeError when no unique board exists, when the output
            is inside the project or manufacturing folder or is not a folder, and
            FileExistsError when the reports already exist. Raises ValueError for
            a board outside the project root or without a settings file.
        """
        P = project.resolve()
        M = manufacturing.resolve()
        if board:
            board_path = (board if board.is_absolute() else P / board).resolve()
        else:
            boards = list(P.glob("*.kicad_pcb"))
            if len(boards) != 1:
                raise RuntimeError(
                    "Expected one project board; select it explicitly with --board"
                )
            board_path = boards[0].resolve()
        if (
            board_path.parent != P
            or board_path.suffix.lower() != ".kicad_pcb"
            or not board_path.is_file()
        ):
            raise ValueError("Board must be an existing KiCad PCB in the project root")
        settings_path = board_path.with_suffix(".kicad_pro")
        if not settings_path.is_file():
            raise ValueError("Board has no matching KiCad project settings file")
        output_dir = out.resolve()
        if output_dir.is_relative_to(P) or output_dir.is_relative_to(M):
            raise RuntimeError(
                "Keep board audit output outside the project and manufacturing folders"
            )
        if output_dir.exists() and not output_dir.is_dir():
            raise RuntimeError("Board audit output must be a folder")
        report_path = output_dir / "pcb_native_review.json"
        geometry_path = output_dir / "pcb_geometry_detail.json"
        if report_path.exists() or geometry_path.exists():
            raise FileExistsError(
                "Board audit reports already exist; use a new output folder"
            )
        self.project = P
        self.manufacturing = M
        self.board_path = board_path
        self.settings_path = settings_path
        self.output_dir = output_dir
        self.report_path = report_path
        self.geometry_path = geometry_path

    def native_hashes(self):
        """Hash the saved schematic, board, settings and rules files of the board."""
        return {
            path.name: ReviewFiles.sha(path)
            for path in self.project.glob(f"{self.board_path.stem}.kicad_*")
            if path.suffix in {".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_dru"}
        }

    @staticmethod
    def copper_geometry(b, fs):
        """Read the tracks, vias and pads of a board.

        Inputs:
            b: Loaded pcbnew board.
            fs: Footprints of the board by reference.
        Outputs:
            Three lists of dicts, tracks, vias and pads, with positions and sizes
            in mm.
        """
        tr = []
        vs = []
        pd = []
        for t in b.GetTracks():
            d = {
                "uuid": t.m_Uuid.AsString(),
                "net": t.GetNetname(),
                "layer": b.GetLayerName(t.GetLayer()),
                "at": PcbUnits.xy(t.GetPosition()),
                "width": PcbUnits.mm(copper_width(t, layer=pcbnew.F_Cu)),
            }
            if isinstance(t, pcbnew.PCB_VIA):
                d.update(
                    drill=PcbUnits.mm(t.GetDrillValue()),
                    layers=[b.GetLayerName(i) for i in t.GetLayerSet().CuStack()],
                )
                vs.append(d)
            else:
                d.update(
                    start=PcbUnits.xy(t.GetStart()),
                    end=PcbUnits.xy(t.GetEnd()),
                    length=PcbUnits.mm(t.GetLength()),
                )
                tr.append(d)
        for r, f in fs.items():
            for p in f.Pads():
                pd.append(
                    {
                        "ref": r,
                        "pin": p.GetNumber(),
                        "net": p.GetNetname(),
                        "at": PcbUnits.xy(p.GetPosition()),
                        "size": PcbUnits.xy(p.GetSize()),
                        "drill": PcbUnits.xy(p.GetDrillSize()),
                        "attribute": int(p.GetAttribute()),
                        "layers": [
                            b.GetLayerName(i) for i in p.GetLayerSet().CuStack()
                        ],
                    }
                )
        return tr, vs, pd

    @staticmethod
    def zone_records(b):
        """Describe every zone of a board by net, layers, fill state and thermal relief."""
        zones = []
        for z in b.Zones():
            zones.append(
                {
                    "net": z.GetNetname(),
                    "layers": [b.GetLayerName(i) for i in z.GetLayerSet().CuStack()],
                    "filled": z.IsFilled(),
                    "pad_connection": int(z.GetPadConnection()),
                    "thermal_spoke_width": PcbUnits.mm(z.GetThermalReliefSpokeWidth()),
                    "thermal_gap": PcbUnits.mm(z.GetThermalReliefGap()),
                    "outline_count": z.Outline().OutlineCount(),
                }
            )
        return zones

    @staticmethod
    def group_records(b):
        """Describe every group of a board by name and member UUIDs."""
        return [
            {
                "name": x.GetName(),
                "members": [q.m_Uuid.AsString() for q in x.GetItems()],
            }
            for x in b.Groups()
        ]

    @staticmethod
    def key_nets(tr, vs, pd):
        """Summarize every named copper and pad net.

        Inputs:
            tr: Track dicts from ``copper_geometry``.
            vs: Via dicts from ``copper_geometry``.
            pd: Pad dicts from ``copper_geometry``.
        Outputs:
            Track length per width and layer, total track length (mm), vias and pads
            of each net; generated ``unconnected-`` nets are left out.
        """
        interesting = sorted(
            name
            for name in {item["net"] for item in [*tr, *vs, *pd]}
            if name and not name.startswith("unconnected-")
        )
        nets = {}
        for n in interesting:
            tt = [t for t in tr if t["net"] == n]
            vv = [v for v in vs if v["net"] == n]
            nets[n] = {
                "width_lengths_mm": {
                    str(w): round(sum(t["length"] for t in tt if t["width"] == w), 3)
                    for w in sorted({t["width"] for t in tt})
                },
                "layers": dict(collections.Counter(t["layer"] for t in tt)),
                "total_track_length": round(sum(t["length"] for t in tt), 3),
                "vias": vv,
                "pads": [p for p in pd if p["net"] == n],
            }
        return nets

    def bom_and_cpl(self, fs, ox, oy):
        """Compare the BOM and the placement file with the board footprints.

        Inputs:
            fs: Footprints of the board by reference.
            ox: Auxiliary origin X in mm.
            oy: Auxiliary origin Y in mm.
        Outputs:
            The BOM rows, the placement rows, the BOM references, the placement
            references, the footprints missing from the placement file and the
            references whose position, side or rotation differs from the board.
        """
        bom = ReviewFiles.read(self.manufacturing / "02_BOM_COMPLETE.csv")
        cpl = ReviewFiles.read(self.manufacturing / "03_CPL_COMPLETE.csv")
        br = [r.strip() for row in bom for r in row["Designator"].split(",")]
        cr = [row["Designator"] for row in cpl]
        missing = set(fs) - set(cr)
        cpl_errors = []
        for row in cpl:
            r = row["Designator"]
            if r not in fs:
                cpl_errors.append(r)
                continue
            f = fs[r]
            layer = pcbnew.F_Fab if f.GetLayer() == pcbnew.F_Cu else pcbnew.B_Fab
            gg = [
                x.GetBoundingBox()
                for x in f.GraphicalItems()
                if isinstance(x, pcbnew.PCB_SHAPE) and x.GetLayer() == layer
            ]
            cx, cy = (
                (
                    (min(x.GetX() for x in gg) + max(x.GetRight() for x in gg)) / 2e6,
                    (min(x.GetY() for x in gg) + max(x.GetBottom() for x in gg)) / 2e6,
                )
                if gg
                else tuple(PcbUnits.xy(f.GetPosition()))
            )
            if (
                abs(float(row["Mid X"]) - (cx - ox)) > 1e-5
                or abs(float(row["Mid Y"]) - (oy - cy)) > 1e-5
                or row["Layer"] != ("Top" if f.GetLayer() == pcbnew.F_Cu else "Bottom")
                or abs(
                    (float(row["Rotation"]) - f.GetOrientationDegrees() + 180) % 360
                    - 180
                )
                > 1e-5
            ):
                cpl_errors.append(r)
        return bom, cpl, br, cr, missing, cpl_errors

    def fill_schedule(self, vs, ox, oy):
        """Compare the filled and capped via schedule with the board vias.

        Inputs:
            vs: Via dicts from ``copper_geometry``.
            ox: Auxiliary origin X in mm.
            oy: Auxiliary origin Y in mm.
        Outputs:
            The schedule rows and the rows whose via is missing or whose position,
            drill or land differs by more than 1e-5 mm.
        """
        fill = ReviewFiles.read(
            self.manufacturing / "assembly/EXPLICIT_FILLED_CAPPED_VIAS.csv"
        )
        vby = {v["uuid"]: v for v in vs}
        fill_errors = []
        for row in fill:
            v = vby.get(row["UUID"])
            if (
                not v
                or max(
                    abs(float(row["X mm from datum"]) - (v["at"][0] - ox)),
                    abs(float(row["Y mm from datum"]) - (oy - v["at"][1])),
                    abs(float(row["Drill mm"]) - v["drill"]),
                    abs(float(row["Land mm"]) - v["width"]),
                )
                > 1e-5
            ):
                fill_errors.append(row)
        return fill, fill_errors

    def archive_mismatches(self):
        """List the archived CAM files whose bytes differ from the loose copy."""
        zcheck = []
        with zipfile.ZipFile(self.manufacturing / "01_PCB_GERBERS_DRILLS.zip") as z:
            for name in z.namelist():
                q = self.manufacturing / "gerbers" / Path(name).name
                if q.exists() and hashlib.sha256(
                    z.read(name)
                ).hexdigest() != ReviewFiles.sha(q):
                    zcheck.append(name)
        return zcheck

    @staticmethod
    def silk_legend(b, fs):
        """List the visible silkscreen text of the footprints and of the board.

        Outputs:
            One dict per text with its owner, layer, height, thickness and position
            in mm.
        """
        legend = []
        for f in fs.values():
            for t in [f.Reference(), f.Value(), *f.GraphicalItems()]:
                if (
                    isinstance(t, pcbnew.PCB_TEXT)
                    and t.GetLayer() in [pcbnew.F_SilkS, pcbnew.B_SilkS]
                    and t.IsVisible()
                ):
                    legend.append(
                        {
                            "ref": f.GetReference(),
                            "text": t.GetText(),
                            "layer": b.GetLayerName(t.GetLayer()),
                            "height": PcbUnits.mm(t.GetTextHeight()),
                            "thickness": PcbUnits.mm(t.GetTextThickness()),
                            "at": PcbUnits.xy(t.GetPosition()),
                        }
                    )
        for t in b.GetDrawings():
            if isinstance(t, pcbnew.PCB_TEXT) and t.GetLayer() in [
                pcbnew.F_SilkS,
                pcbnew.B_SilkS,
            ]:
                legend.append(
                    {
                        "ref": "board",
                        "text": t.GetText(),
                        "layer": b.GetLayerName(t.GetLayer()),
                        "height": PcbUnits.mm(t.GetTextHeight()),
                        "thickness": PcbUnits.mm(t.GetTextThickness()),
                        "at": PcbUnits.xy(t.GetPosition()),
                    }
                )
        return legend

    def __call__(self):
        """Audit the board and write the two reports.

        Outputs:
            The report and the key-net summary. Creates the output folder and writes
            ``pcb_native_review.json`` and ``pcb_geometry_detail.json`` into it; a
            file written by this call is removed again when the audit fails. Raises
            RuntimeError when KiCad cannot load the board or when the saved project
            files change during the audit.
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)

        before = self.native_hashes()
        b = pcbnew.LoadBoard(str(self.board_path))
        if b is None:
            raise RuntimeError("KiCad could not load the saved board")
        origin = b.GetDesignSettings().GetAuxOrigin()
        ox = origin.x / 1e6
        oy = origin.y / 1e6

        fs = {f.GetReference(): f for f in b.GetFootprints()}
        tr, vs, pd = self.copper_geometry(b, fs)
        zones = self.zone_records(b)
        g = self.group_records(b)
        pro = json.loads(self.settings_path.read_text(encoding="utf-8-sig"))
        classes = {q["name"]: q for q in pro["net_settings"]["classes"]}
        nets = self.key_nets(tr, vs, pd)

        bom, cpl, br, cr, missing, cpl_errors = self.bom_and_cpl(fs, ox, oy)
        fill, fill_errors = self.fill_schedule(vs, ox, oy)
        zcheck = self.archive_mismatches()
        legend = self.silk_legend(b, fs)
        report = {
            "source_sha256": before,
            "source_unchanged": before == self.native_hashes(),
            "footprints": len(fs),
            "tracks": len(tr),
            "vias": len(vs),
            "nets": len([n for n in b.GetNetsByName() if str(n)]),
            "layer_track_counts": dict(collections.Counter(t["layer"] for t in tr)),
            "layer_track_lengths": {
                layer: round(sum(t["length"] for t in tr if t["layer"] == layer), 3)
                for layer in sorted({t["layer"] for t in tr})
            },
            "via_sizes": {
                str(k): v
                for k, v in collections.Counter(
                    (v["width"], v["drill"]) for v in vs
                ).items()
            },
            "zones": zones,
            "groups": g,
            "key_nets": nets,
            "net_summary_scope": "All named copper and pad nets, excluding generated unconnected-pad nets.",
            "drc_exclusions": pro["board"]["design_settings"]["drc_exclusions"],
            "ignored_checks": [
                k
                for k, v in pro["board"]["design_settings"]["rule_severities"].items()
                if v == "ignore"
            ],
            "netclasses": classes,
            "BOM_CPL": {
                "bomrows": len(bom),
                "bomrefs": len(br),
                "cplrows": len(cr),
                "BOM_minus_CPL": sorted(set(br) - set(cr)),
                "CPL_minus_BOM": sorted(set(cr) - set(br)),
                "duplicates": [r for r, n in collections.Counter(br).items() if n > 1],
                "excluded_native_features": sorted(missing),
                "cpl_position_side_rotation_errors": cpl_errors,
            },
            "fill_schedule_count": len(fill),
            "fill_schedule_errors": fill_errors,
            "zip_vs_loose_CAM_mismatches": zcheck,
            "silk_text_count": len(legend),
            "silk_min_height": min((t["height"] for t in legend), default=None),
            "silk_min_thickness": min((t["thickness"] for t in legend), default=None),
        }
        if not report["source_unchanged"]:
            raise RuntimeError(
                "Saved project changed during board audit; discard the result"
            )
        geometry = {
            "tracks": tr,
            "vias": vs,
            "pads": pd,
            "legend": legend,
            "footprints": {
                r: {
                    "value": f.GetValue(),
                    "at": PcbUnits.xy(f.GetPosition()),
                    "side": b.GetLayerName(f.GetLayer()),
                    "rotation": f.GetOrientationDegrees(),
                    "id": str(f.GetFPID()),
                }
                for r, f in fs.items()
            },
        }
        geometry_bytes = json.dumps(geometry, indent=2).encode("utf-8")
        report["geometry_sha256"] = hashlib.sha256(geometry_bytes).hexdigest()
        report_bytes = json.dumps(report, indent=2).encode("utf-8")
        created = []
        try:
            for target, data in (
                (self.geometry_path, geometry_bytes),
                (self.report_path, report_bytes),
            ):
                with target.open("xb") as handle:
                    created.append((target, data))
                    handle.write(data)
            if before != self.native_hashes():
                raise RuntimeError(
                    "Saved project changed during board audit; discard the result"
                )
        except BaseException:
            for target, data in created:
                if target.is_file() and target.read_bytes() == data:
                    target.unlink()
            raise
        return report, nets


class ManufacturingCommand:
    """Command-line entry: audit a board against its manufacturing folder."""

    def __call__(self, argv=None):
        """Run the audit.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            None; prints the report without its bulky sections, then the key nets.
        """
        ap = argparse.ArgumentParser(description=__doc__)
        ap.add_argument("--project", type=Path, required=True)
        ap.add_argument(
            "--board",
            type=Path,
            help="board in the project root; auto-detected when unique",
        )
        ap.add_argument(
            "--manufacturing", type=Path, required=True, help="generated review folder"
        )
        ap.add_argument(
            "--out", type=Path, required=True, help="new report folder outside project"
        )
        a = ap.parse_args(argv)
        report, nets = ManufacturingAudit(a.project, a.board, a.manufacturing, a.out)()
        print(
            json.dumps(
                {
                    k: v
                    for k, v in report.items()
                    if k not in ["key_nets", "groups", "netclasses", "source_sha256"]
                },
                indent=2,
            )
        )
        print("KEY NETS:")
        for n, d in nets.items():
            print(n, d["width_lengths_mm"], d["layers"], "vias", len(d["vias"]))


main = ManufacturingCommand()

if __name__ == "__main__":
    main()
