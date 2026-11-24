"""normalize_ipc_netlist.py: validate and normalize the installed exporter's IPC-D-356 output against native CAD.

The installed exporter leaves invalid via soldermask integers and truncates names.
Only masks and collision-free signal aliases are corrected; native test geometry
is retained and independently checked. This does not modify the board.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import collections
import csv
import json
import re
from pathlib import Path

import pcbnew

from core.export_helpers import require, sha, stable_zip, writecsv, writejson
from core.pcb_io import copper_width


class IpcNetlist:
    """The IPC-D-356 netlist of one export, checked against the native board and rewritten."""

    TEST_RECORDS = ["317", "327", "367"]
    DRILL_FIELDS = [
        "kind",
        "reference",
        "pad",
        "uuid",
        "net",
        "x_mm",
        "y_mm",
        "drill_local_x_mm",
        "drill_local_y_mm",
        "diameter_mm",
        "slot_travel_mm",
        "native_orientation_deg",
        "filled_capped",
        "purpose",
    ]

    def __init__(self, project, output):
        """Load the export identity, the raw netlist and the native board.

        Inputs:
            project: Native project folder with the board named by the identity.
            output: Export folder that holds ``jlcpcb/``.
        Outputs: None. Raises RuntimeError when assertions are disabled.
        """
        require(__debug__, "IPC validator must run with assertions enabled")
        self.project = Path(project)
        self.out = Path(output)
        self.manufacturing = self.out / "jlcpcb"
        M = self.manufacturing
        self.identity = json.loads((M / "SOURCE_IDENTITY.json").read_text())
        self.source = self.identity["source_sha256"]
        stem = self.identity["revision"]
        self.stem = stem
        self.path = M / "gerbers" / f"{stem}.ipc"
        self.legacy_path = M / "gerbers" / f"{stem}.d356"
        self.rawpath = self.out / "IPC_NATIVE_UNNORMALIZED_NOT_FOR_FACTORY.txt"
        if not self.rawpath.exists():
            self.rawpath.write_bytes(self.legacy_path.read_bytes())
        self.raw = self.rawpath.read_text().splitlines()
        self.board = pcbnew.LoadBoard(str(self.project / (stem + ".kicad_pcb")))
        self.footprints = {f.GetReference(): f for f in self.board.GetFootprints()}
        origin = self.board.GetDesignSettings().GetAuxOrigin()
        self.ox, self.oy = origin.x / 1e6, origin.y / 1e6
        self.vias = [v for v in self.board.GetTracks() if isinstance(v, pcbnew.PCB_VIA)]

    @classmethod
    def normalize(cls, project, output):
        """Validate the native IPC export, write the normalized netlist and its evidence.

        Inputs:
            project: Native project folder.
            output: Export folder with ``jlcpcb/SOURCE_IDENTITY.json`` and the
                exporter's raw ``.d356`` netlist.
        Outputs:
            None; prints a JSON summary of the checked records. Writes the
            normalized ``.ipc`` netlist, the omitted-pad, expected-testpoint and
            drill records, the drill schedule, the gerber archive and the
            validation receipt. Raises AssertionError when a netlist record does
            not match exactly one native pad or via, when copper contacts are
            omitted, or when the rewritten file does not read back to the same
            records, and RuntimeError when assertions are disabled.
        """
        netlist = cls(project, output)
        netlist.read_records()
        netlist.write_omitted_pads()
        netlist.write_normalized()
        netlist.read_back()
        netlist.write_drill_records()
        gerber_entries = netlist.archive_gerbers()
        netlist.write_receipt(gerber_entries)

    def via_item(self, index, px, py, access, xsize, ysize):
        """Find the one native via at a netlist position and check its record.

        Inputs:
            index: Line index in the raw netlist, for failure messages.
            px, py: Position in mm from the plot datum.
            access, xsize, ysize: Fields of the netlist record.
        Outputs:
            The via and its mask value, 1 for a tented top plus 2 for a tented bottom.
        """
        candidates = [
            v
            for v in self.vias
            if abs(v.GetPosition().x / 1e6 - self.ox - px) <= 0.001271
            and abs(self.oy - v.GetPosition().y / 1e6 - py) <= 0.001271
        ]
        assert len(candidates) == 1, (index, px, py, candidates)
        item = candidates[0]
        mask = int(item.IsTented(pcbnew.F_Mask)) + 2 * int(item.IsTented(pcbnew.B_Mask))
        assert item.m_Uuid.AsString() not in self.matched_vias
        self.matched_vias.add(item.m_Uuid.AsString())
        assert (
            access == 0
            and abs(xsize * 0.00254 - copper_width(item, layer=pcbnew.F_Cu) / 1e6)
            <= 0.001271
            and ysize == 0
        )
        return item, mask

    def pad_item(self, line, ref, pin, px, py, access, oldmask):
        """Find the one native pad at a netlist position and check its record.

        Inputs:
            line: The raw netlist line, for failure messages.
            ref: Footprint reference.
            pin: Four-character pin field of the record.
            px, py: Position in mm from the plot datum.
            access: Access side of the record.
            oldmask: Mask value the exporter wrote.
        Outputs:
            The pad and its mask value. A pad whose full name exceeds four
            characters is also added to ``long_pins``.
        """
        b, ox, oy = self.board, self.ox, self.oy
        fs = self.footprints
        assert ref in fs, (ref, line)
        candidates = [
            p
            for p in fs[ref].Pads()
            if p.GetNumber()[:4] == pin
            and abs(p.GetPosition().x / 1e6 - ox - px) <= 0.001271
            and abs(oy - p.GetPosition().y / 1e6 - py) <= 0.001271
        ]
        assert len(candidates) == 1, (ref, pin, px, py, len(candidates))
        item = candidates[0]
        mask = 3
        if item.GetLayerSet().Contains(pcbnew.F_Mask):
            mask &= ~1
        if item.GetLayerSet().Contains(pcbnew.B_Mask):
            mask &= ~2
        key = item.m_Uuid.AsString()
        assert key not in self.matched_pads
        self.matched_pads.add(key)
        assert oldmask == mask, (ref, pin, oldmask, mask)
        assert access in [0, 1, b.GetCopperLayerCount()]
        if len(item.GetNumber()) > 4:
            self.long_pins.append(
                {
                    "Designator": ref,
                    "Native pad name": item.GetNumber(),
                    "IPC four-character pin": pin,
                    "X mm": px,
                    "Y mm": py,
                    "Native net": item.GetNetname(),
                    "Meaning": (
                        "IPC pin field is four characters; native full name "
                        "retained here. Location/net are unchanged."
                    ),
                }
            )
        return item, mask

    def read_records(self):
        """Match every raw netlist test record to one native via or pad.

        Fills ``records``, ``matched_pads``, ``matched_vias``, ``bad_masks`` and
        ``long_pins``. Raises AssertionError for a record that is malformed,
        matches no or several native items, or disagrees with the native mask.
        """
        self.records = []
        self.matched_pads = set()
        self.matched_vias = set()
        self.bad_masks = 0
        self.long_pins = []
        for i, line in enumerate(self.raw):
            if line[:3] not in self.TEST_RECORDS:
                continue
            assert len(line) >= 73
            ref = line[20:26].strip()
            pin = line[27:31].strip()
            m = re.search(
                r"A(\d{2})X([+-]\d{6})Y([+-]\d{6})X(\d{4})Y(\d{4})R(\d{3})S(-?\d+)$",
                line,
            )
            assert m, (i, line)
            access, x, y, xsize, ysize, rot, oldmask = map(int, m.groups())
            px, py = x * 0.00254, y * 0.00254
            if ref == "VIA":
                item, mask = self.via_item(i, px, py, access, xsize, ysize)
            else:
                item, mask = self.pad_item(line, ref, pin, px, py, access, oldmask)
            if oldmask not in range(4):
                self.bad_masks += 1
            net = item.GetNetname()
            assert net.isascii() and len(net) <= 58, net
            self.records.append(
                {
                    "raw": line,
                    "net": net,
                    "ref": ref,
                    "pin": pin,
                    "mask": mask,
                    "x": x,
                    "y": y,
                    "access": access,
                }
            )
        assert len(self.matched_vias) == len(self.vias)

    def write_omitted_pads(self):
        """Record the pads the native export left out and require them to be non-copper.

        Writes ``IPC_NATIVE_OMITTED_PADS.json``. Raises AssertionError when a
        copper contact is missing from the netlist.
        """
        b = self.board
        cu = [i for i in b.GetEnabledLayers().Seq() if pcbnew.IsCopperLayer(i)]
        conductivepads = {
            p.m_Uuid.AsString()
            for f in b.GetFootprints()
            for p in f.Pads()
            if any(p.GetLayerSet().Contains(layer) for layer in cu)
        }
        self.omitted = [
            {
                "ref": f.GetReference(),
                "pin": p.GetNumber(),
                "net": p.GetNetname(),
                "code": p.GetNetCode(),
                "attribute": int(p.GetAttribute()),
                "on_copper": any(p.GetLayerSet().Contains(layer) for layer in cu),
            }
            for f in b.GetFootprints()
            for p in f.Pads()
            if p.m_Uuid.AsString() not in self.matched_pads
        ]
        (self.out / "IPC_NATIVE_OMITTED_PADS.json").write_text(
            json.dumps(self.omitted, indent=2)
        )
        assert self.matched_pads == conductivepads, (
            len(self.matched_pads),
            len(conductivepads),
            len(conductivepads - self.matched_pads),
        )
        assert all(not p["on_copper"] for p in self.omitted), (
            "Native IPC export omitted copper contacts"
        )
        assert 0 <= self.bad_masks <= len(self.vias)

    def write_normalized(self):
        """Write the netlist with corrected masks and collision-free signal aliases.

        Every record is padded to 80 columns; each net gets an ``NNAME<n>`` alias
        that keeps the full native name in a ``P`` parameter. Fills ``nets`` and
        ``aliases``.
        """
        records = self.records
        self.nets = sorted({r["net"] for r in records if r["net"]})
        self.aliases = {s: f"NNAME{i + 1:04d}" for i, s in enumerate(self.nets)}
        aliases = self.aliases
        assert len(set(aliases.values())) == len(self.nets)
        stem = self.stem
        lines = [
            ("C  AutoPCB - validated native geometry; explicit net aliases").ljust(80),
            ("C  PCB SHA256 " + self.source[stem + ".kicad_pcb"]).ljust(80),
            "P  CODE 00".ljust(80),
            "P  UNITS CUST 0".ljust(80),
            "P  arrayDim   N".ljust(80),
        ]
        for net, alias in aliases.items():
            # Conventional P NNAME<n> parameters; the full alias is used in test records.
            lines.append(f"P  {alias} {net}".ljust(80))
        for r in sorted(
            records,
            key=lambda r: (
                aliases.get(r["net"], "N/C"),
                r["ref"],
                r["pin"],
                r["x"],
                r["y"],
            ),
        ):
            line = r["raw"]
            label = aliases.get(r["net"], "N/C")
            core = line[:3] + f"{label:<14}" + line[17 : line.rfind("S")]
            # Column 72 is blank; S and its one-digit mask occupy columns 73-74.
            assert len(core) == 71, (len(core), line)
            normalized = core + f" S{r['mask']}" + 6 * " "
            assert len(normalized) == 80
            lines.append(normalized)
        lines.append("999".ljust(80))
        assert all(len(s) == 80 and s.isascii() for s in lines)
        self.path.write_text("\n".join(lines) + "\n", encoding="ascii")

    def read_back(self):
        """Read the written bytes with a separate fixed-column parser and compare.

        Writes ``IPC_EXPECTED_TESTPOINTS.json``. Raises AssertionError when the
        aliases, record layout or record contents differ from what was matched.
        """
        records, aliases = self.records, self.aliases
        final = self.path.read_text().splitlines()
        defs = {
            s.split(maxsplit=2)[1]: s.split(maxsplit=2)[2].strip()
            for s in final
            if s.startswith("P  NNAME")
        }
        assert defs == {v: k for k, v in aliases.items()}
        check = [s for s in final if s[:3] in self.TEST_RECORDS]
        assert len(check) == len(records)
        assert all(
            s[71] == " " and s[72] == "S" and s[73] in "0123" and s[74:] == " " * 6
            for s in check
        )
        oldtuples = collections.Counter(
            (r["ref"], r["pin"], r["x"], r["y"], r["net"], r["access"]) for r in records
        )
        newtuples = collections.Counter(
            (
                s[20:26].strip(),
                s[27:31].strip(),
                int(s[42:49]),
                int(s[50:57]),
                defs.get(s[3:17].strip(), ""),
                int(s[39:41]),
            )
            for s in check
        )
        assert oldtuples == newtuples
        (self.out / "IPC_EXPECTED_TESTPOINTS.json").write_text(
            json.dumps(
                [
                    {
                        "ref": r["ref"],
                        "pin": r["pin"],
                        "x_decimil": r["x"],
                        "y_decimil": r["y"],
                        "net": r["net"],
                        "access": r["access"],
                        "mask": r["mask"],
                    }
                    for r in records
                ],
                indent=2,
            )
        )

    def write_drill_records(self):
        """Write the native drill and slot schedule and the drill map.

        Writes ``assembly/NATIVE_DRILL_AND_SLOT_SCHEDULE.csv`` and
        ``evidence/NATIVE_DRILL_MAP.json`` beside the netlist and a copy of the
        map in the output folder. Fills ``drills``.
        """
        M, ox, oy = self.manufacturing, self.ox, self.oy
        drills = []
        for f in self.board.GetFootprints():
            for p in f.Pads():
                size = p.GetDrillSize()
                if not size.x or not size.y:
                    continue
                q = p.GetPosition()
                drills.append(
                    {
                        "kind": "NPTH"
                        if p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH
                        else "PTH",
                        "reference": f.GetReference(),
                        "pad": p.GetNumber(),
                        "uuid": p.m_Uuid.AsString(),
                        "net": p.GetNetname(),
                        "x_mm": q.x / 1e6 - ox,
                        "y_mm": oy - q.y / 1e6,
                        "drill_local_x_mm": size.x / 1e6,
                        "drill_local_y_mm": size.y / 1e6,
                        "diameter_mm": min(size.x, size.y) / 1e6,
                        "slot_travel_mm": abs(size.x - size.y) / 1e6,
                        "native_orientation_deg": p.GetOrientationDegrees(),
                        "filled_capped": False,
                        "purpose": "Insertion/mounting hole; do not fill",
                    }
                )
        for v in self.vias:
            q = v.GetPosition()
            filled = v.GetFillingMode() == pcbnew.FILLING_MODE_FILLED
            drills.append(
                {
                    "kind": "PTH",
                    "reference": "VIA",
                    "pad": "",
                    "uuid": v.m_Uuid.AsString(),
                    "net": v.GetNetname(),
                    "x_mm": q.x / 1e6 - ox,
                    "y_mm": oy - q.y / 1e6,
                    "drill_local_x_mm": v.GetDrill() / 1e6,
                    "drill_local_y_mm": v.GetDrill() / 1e6,
                    "diameter_mm": v.GetDrill() / 1e6,
                    "slot_travel_mm": 0,
                    "native_orientation_deg": 0,
                    "filled_capped": filled,
                    "purpose": "Epoxy fill and copper cap"
                    if filled
                    else "Ordinary via; native mask tenting retained",
                }
            )
        drills.sort(
            key=lambda r: (r["kind"], r["reference"], r["pad"], r["x_mm"], r["y_mm"])
        )
        self.drills = drills
        writecsv(
            M / "assembly/NATIVE_DRILL_AND_SLOT_SCHEDULE.csv", drills, self.DRILL_FIELDS
        )
        drillmap = {
            "source_sha256": self.source,
            "datum_mm": [ox, oy],
            "coordinates": (
                "X right, Y up from native plot/drill datum; local drill "
                "sizes and orientations are native KiCad pad attributes"
            ),
            "slot_travel_definition": (
                "Distance between end-circle centres; full oval dimensions "
                "are drill_local_x_mm and drill_local_y_mm"
            ),
            "counts": {
                "holes": len(drills),
                "NPTH": sum(r["kind"] == "NPTH" for r in drills),
                "PTH": sum(r["kind"] == "PTH" for r in drills),
                "slots": sum(r["slot_travel_mm"] > 0 for r in drills),
                "filled_capped_vias": sum(r["filled_capped"] for r in drills),
            },
            "records": drills,
            "evidence_limit": (
                "Native drill inventory and explicit fill flags; "
                "independent Excellon/CAM and factory process review "
                "remain separate"
            ),
        }
        writejson(M / "evidence/NATIVE_DRILL_MAP.json", drillmap)
        writejson(self.out / "NATIVE_DRILL_MAP.json", drillmap)

    def archive_gerbers(self):
        """Remove the legacy netlist, map long pad names and zip the gerber folder.

        Writes ``assembly/IPC_LONG_PAD_NAME_MAP.csv`` when a pad name exceeds
        four characters and ``01_PCB_GERBERS_DRILLS.zip``.

        Outputs:
            Pairs of gerber-folder file and archive name. Raises AssertionError
            for an unexpected file type, a layer count that differs from the
            identity, or a missing drill or netlist file.
        """
        M, legacy_path, long_pins = (
            self.manufacturing,
            self.legacy_path,
            self.long_pins,
        )
        if legacy_path.exists():
            assert legacy_path.resolve().is_relative_to(self.out.resolve())
            legacy_path.unlink()
        if long_pins:
            with (M / "assembly/IPC_LONG_PAD_NAME_MAP.csv").open(
                "w", newline="", encoding="utf8"
            ) as h:
                w = csv.DictWriter(h, fieldnames=list(long_pins[0]))
                w.writeheader()
                w.writerows(long_pins)
        gerber_entries = [
            (p, p.name)
            for p in sorted((M / "gerbers").iterdir())
            if p.is_file() and p.suffix.lower() != ".pdf"
        ]
        assert all(
            p.suffix.lower() in [".gbr", ".drl", ".gbrjob", ".ipc"]
            for p, _ in gerber_entries
        )
        assert len([p for p, _ in gerber_entries if p.suffix == ".gbr"]) == len(
            self.identity["exported_gerber_layers"]
        )
        assert any(p.suffix == ".drl" for p, _ in gerber_entries) and any(
            p.suffix == ".ipc" for p, _ in gerber_entries
        )
        stable_zip(M / "01_PCB_GERBERS_DRILLS.zip", gerber_entries)
        return gerber_entries

    def write_receipt(self, gerber_entries):
        """Write the validation receipt, check the source is unchanged and print a summary.

        Writes ``evidence/IPC_D356_VALIDATION.json`` and a copy in the output
        folder.

        Inputs:
            gerber_entries: Files archived by ``archive_gerbers``.
        Outputs: None. Raises AssertionError when a source file changed.
        """
        M, source, ox, oy = self.manufacturing, self.source, self.ox, self.oy
        receipt = {
            "source_sha256": source,
            "native_design_unchanged": True,
            "native_raw_export_sha256": sha(self.rawpath),
            "normalized_netlist_sha256": sha(self.path),
            "native_via_records_checked": len(self.matched_vias),
            "native_pad_records_checked": len(self.matched_pads),
            "total_test_records": len(self.records),
            "unique_connected_signal_aliases": len(self.nets),
            "invalid_via_mask_fields_corrected": self.bad_masks,
            "corrected_mask_basis": (
                "Native IsTented(F_Mask/B_Mask), matching current official KiCad exporter semantics"
            ),
            "signal_name_basis": (
                "Collision-free P NNAME aliases retain every full native name; no truncation"
            ),
            "record_width_columns": 80,
            "native_geometry_reference_pin_net_access_preserved": True,
            "IPC_four_character_pin_field_long_names_in_separate_map": self.long_pins,
            "coordinate_units": (
                f"0.1 mil (0.00254 mm) per IPC UNITS CUST 0; native plot/drill origin ({ox},{oy}) mm"
            ),
            "coordinate_quantization_error_mm_per_axis_max": 0.00127,
            "physical_or_factory_test_performed": False,
            "primary_format_reference": "https://docs.kicad.org/doxygen/export__d356_8cpp_source.html",
        }
        receipt["non_copper_pad_records_excluded"] = len(self.omitted)
        receipt["gerber_archive_members"] = [n for _, n in gerber_entries]
        (M / "evidence/IPC_D356_VALIDATION.json").write_text(
            json.dumps(receipt, indent=2) + "\n"
        )
        (self.out / "IPC_D356_VALIDATION.json").write_text(
            json.dumps(receipt, indent=2) + "\n"
        )
        assert source == {n: sha(self.project / n) for n in source}
        print(
            json.dumps(
                {
                    k: receipt[k]
                    for k in [
                        "native_via_records_checked",
                        "native_pad_records_checked",
                        "total_test_records",
                        "unique_connected_signal_aliases",
                        "invalid_via_mask_fields_corrected",
                        "native_geometry_reference_pin_net_access_preserved",
                        "record_width_columns",
                    ]
                },
                indent=2,
            )
        )
