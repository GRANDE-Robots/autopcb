"""generate_factory_review.py: build a fresh, count-independent manufacturing review set from native CAD.

Run with KiCad Python. Writes only to a new external output folder. All ERC,
DRC, parity and connectivity checks are mandatory. No production release.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import argparse
import collections
import datetime
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from core.paths import asset_root, data_path, default_project, library_directories

import pcbnew

from core.export_helpers import (
    BOM_FIELDS,
    CPL_FIELDS,
    FILES,
    REGISTER_FIELDS,
    BomRows,
    assert_current_inputs,
    factory_input_dir,
    readcsv,
    require,
    sha,
    writecsv,
    writejson,
)
from core.factory_scope import FactoryScope
from core.pcb_io import copper_width
from core.toolchain import kicad_cli


class KicadRunner:
    """Runs KiCad CLI commands for the export and keeps a log of every run."""

    def __init__(self, cli: str, log: Path):
        """Set the CLI and the file that receives the run log.

        Inputs:
            cli: Path of the KiCad command-line program.
            log: JSON file rewritten after every command.
        """
        self.cli = cli
        self.log = log
        self.commands = []

    def __call__(self, argv) -> str:
        """Run one KiCad CLI command and rewrite the run log.

        Inputs:
            argv: Command arguments after the program name.
        Outputs:
            The command's standard output without surrounding whitespace.
            Raises RuntimeError with the command's error output when it exits
            with a nonzero status.
        """
        r = subprocess.run(
            [self.cli, *map(str, argv)],
            capture_output=True,
            text=True,
            encoding="utf8",
            errors="replace",
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        self.commands.append(
            {
                "command": [self.cli, *map(str, argv)],
                "returncode": r.returncode,
                "stdout": r.stdout,
                "stderr": r.stderr,
            }
        )
        writejson(self.log, self.commands)
        require(r.returncode == 0, "KiCad failed: " + str(argv) + "\n" + r.stderr)
        return r.stdout.strip()


class NativeSource:
    """The native board and schematic of a project and the hashes that bind an export to them."""

    def __init__(self, project: Path):
        """Find the one board and one schematic and hash the project files.

        Inputs:
            project: Native project folder.
        Outputs: None. Raises RuntimeError unless the folder holds exactly one
            board and one single-sheet schematic.
        """
        self.project = project
        boards = list(project.glob("*.kicad_pcb"))
        schematics = list(project.glob("*.kicad_sch"))
        require(
            len(boards) == len(schematics) == 1,
            "Expected one native board and one single-sheet schematic",
        )
        self.board_path = boards[0]
        self.schematic_path = schematics[0]
        self.stem = self.board_path.stem
        self.hashes = {
            p.name: sha(p)
            for p in project.glob("*.kicad_*")
            if p.suffix in [".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_dru"]
        }

    def write_erc_configuration(self, manufacturing: Path) -> list:
        """Record the ERC rule severities and require that no ERC exclusion is in force.

        Inputs:
            manufacturing: Export folder; the record goes in ``evidence/``.
        Outputs:
            The sorted names of the ignored rule categories. Raises RuntimeError
            when the project lists ERC exclusions.
        """
        propath = self.project / (self.stem + ".kicad_pro")
        erc_settings = (
            json.loads(propath.read_text(encoding="utf-8-sig")).get("erc", {})
            if propath.exists()
            else {}
        )
        require(
            not erc_settings.get("erc_exclusions", []),
            "ERC exclusions must be explicitly resolved before export",
        )
        erc_ignored = sorted(
            k
            for k, v in erc_settings.get("rule_severities", {}).items()
            if v == "ignore"
        )
        writejson(
            manufacturing / "evidence/ERC_RULE_CONFIGURATION.json",
            {
                "source_sha256": self.hashes,
                "rule_severities": erc_settings.get("rule_severities", {}),
                "ignored_categories": erc_ignored,
                "individual_exclusions": erc_settings.get("erc_exclusions", []),
                "interpretation": (
                    "ERC executes every configured rule; any listed ignored "
                    "categories are disclosed and are not claimed checked"
                ),
            },
        )
        return erc_ignored

    def snapshot_dependencies(self, out: Path) -> dict:
        """Hash the libraries, scripts and data the export reads and record them.

        Inputs:
            out: Export folder that receives ``INPUT_SNAPSHOT.json``.
        Outputs:
            SHA-256 of each dependency, keyed by its path relative to the project.
        """
        P = self.project
        support = asset_root(P)
        inputs = [
            p
            for directory in (
                *library_directories(P),
                support / "scripts",
                data_path(P),
            )
            for p in directory.rglob("*")
            if p.is_file()
            and p.name.lower() not in ("desktop.ini", "thumbs.db", ".ds_store")
            and "__pycache__" not in p.parts
        ]
        inputs += [p for p in [P / "sym-lib-table", P / "fp-lib-table"] if p.exists()]
        deps = {p.relative_to(P).as_posix(): sha(p) for p in inputs}
        writejson(
            out / "INPUT_SNAPSHOT.json",
            {"source_sha256": self.hashes, "dependency_sha256": deps},
        )
        return deps


class NativeChecks:
    """Runs the mandatory native ERC and DRC and exports the schematic netlist."""

    def __call__(self, run: KicadRunner, native: NativeSource, manufacturing: Path):
        """Run ERC, DRC with schematic parity and the netlist export.

        Inputs:
            run: Runner for KiCad CLI commands.
            native: The native board and schematic.
            manufacturing: Export folder; reports go in ``evidence/``.
        Outputs: None. Raises RuntimeError when either report has a finding, or
            a DRC list is missing or not empty.
        """
        M = manufacturing
        run(
            [
                "sch",
                "erc",
                "--format",
                "json",
                "-o",
                M / "evidence/ERC.json",
                native.schematic_path,
            ]
        )
        run(
            [
                "pcb",
                "drc",
                "--format",
                "json",
                "--schematic-parity",
                "--all-track-errors",
                "-o",
                M / "evidence/DRC.json",
                native.board_path,
            ]
        )
        erc = json.loads((M / "evidence/ERC.json").read_text())
        drc = json.loads((M / "evidence/DRC.json").read_text())
        require(
            not [v for s in erc.get("sheets", []) for v in s.get("violations", [])],
            "ERC findings; no factory package generated",
        )
        for key in [
            "violations",
            "unconnected_items",
            "schematic_parity",
            "ignored_checks",
        ]:
            require(key in drc, f"DRC report missing required {key} evidence")
            require(not drc[key], f"DRC {key} is not empty")
        run(
            [
                "sch",
                "export",
                "netlist",
                "--format",
                "kicadxml",
                "-o",
                M / "evidence/NETLIST.xml",
                native.schematic_path,
            ]
        )


class PartCatalog:
    """The purchased parts, owner-stock modules and fabricated features of the native design."""

    MODULE_FIELDS = [
        "Designator",
        "Manufacturer",
        "MPN",
        "Quantity",
        "Status",
        "Factory work",
    ]

    def __init__(self, footprints: dict, components: dict, control: Path):
        """Read the controlled inputs that classify the design's references.

        Inputs:
            footprints: Board footprints by reference.
            components: Netlist components by reference.
            control: Reviewed ``release_inputs`` folder.
        """
        self.footprints = footprints
        self.components = components
        self.old = {
            r["Designator"]: r
            for r in readcsv(control / "FALLBACK_EXACT_PART_REGISTER.csv")
        }
        mods = readcsv(control / "07_REQUIRED_MODULES_NOT_DNP.csv")
        self.module_cfg = {r["Designator"]: r for r in mods}
        featurepath = control / "PCB_FEATURES_NO_PURCHASE.csv"
        self.known_features = (
            {r["Designator"]: r for r in readcsv(featurepath)}
            if featurepath.exists()
            else {}
        )
        self.parts = []
        self.features = []
        self.provenance = []
        self.native_flags = []
        self.module_rows = []
        self.native_fields_by_ref = {}

    def add_feature(self, ref: str, kind: str) -> None:
        """Record a fabricated feature, which is neither purchased nor omitted."""
        self.features.append(
            {
                "Designator": ref,
                "Type": kind,
                "Procurement": "Fabricated feature; no loose part; not an assembly omission",
            }
        )

    def add_part(self, ref: str, f, c, fields: dict, fp: str) -> None:
        """Check one purchased reference against the schematic and PCB and record it.

        Inputs:
            ref: Reference designator.
            f: Its board footprint.
            c: Its netlist component.
            fields: Its schematic fields.
            fp: Footprint name.
        Outputs: None. Raises RuntimeError when the footprint has no schematic
            component, the schematic and PCB disagree on the value, footprint,
            MPN or manufacturer, the footprint is marked DNP, or a module
            declares a quantity other than one or a conflicting MPN or
            manufacturer.
        """
        require(
            c is not None, f"{ref}: nonfabrication PCB footprint absent from schematic"
        )
        require(
            c.findtext("value") == f.GetValue(), f"{ref}: schematic/PCB value mismatch"
        )
        require(
            c.findtext("footprint", "").split(":")[-1] == fp,
            f"{ref}: schematic/PCB footprint mismatch",
        )
        meta = BomRows.exact_metadata(
            fields, self.old.get(ref, {}), ref, self.provenance
        )
        boardfields = {e.GetName(): e.GetText() for e in f.GetFields()}
        for k in ["MPN", "Manufacturer"]:
            if k in boardfields:
                require(
                    boardfields[k] == meta[k],
                    f"{ref}: PCB {k} does not match selected schematic identity",
                )
        ismodule = (
            ref in self.module_cfg
            or "plug-in module" in fields.get("Assembly_Service", "").lower()
        )
        self.native_flags.append(
            dict(
                Designator=ref,
                DNP=f.IsDNP(),
                Native_exclude_BOM=f.IsExcludedFromBOM(),
                Native_exclude_positions=f.IsExcludedFromPosFiles(),
                Included=True,
                Category="Required module" if ismodule else "Required board part",
            )
        )
        require(
            not f.IsDNP(),
            (
                f"{ref}: native DNP flag conflicts with required assembly; "
                f"resolve explicitly, never silently omit"
            ),
        )
        self.native_fields_by_ref[ref] = fields
        part = dict(
            Designator=ref,
            Comment=c.findtext("value"),
            Footprint=fp,
            **meta,
            Quantity="1",
            Sourcing=fields.get(
                "Procurement",
                "Exact identity; inventory and factory acceptance unverified",
            ),
            Assembly=fields.get("Assembly", "Required; never auto-DNP"),
            Side="Bottom" if f.GetLayer() == pcbnew.B_Cu else "Top",
        )
        self.parts.append(part)
        if ismodule:
            cfg = self.module_cfg.get(ref, {})
            require(
                cfg.get("Quantity", "1") == "1",
                f"{ref}: each native module reference represents one placement",
            )
            for k in ["MPN", "Manufacturer"]:
                if cfg.get(k):
                    require(
                        cfg[k] == meta[k],
                        f"{ref}: owner-stock/module preparation {k} conflicts with schematic",
                    )
            self.module_rows.append(
                dict(
                    Designator=ref,
                    Manufacturer=meta["Manufacturer"],
                    MPN=meta["MPN"],
                    Quantity="1",
                    Status=cfg.get(
                        "Status",
                        "REQUIRED MODULE; exact preparation/ownership must be confirmed; not DNP",
                    ),
                    **{
                        "Factory work": cfg.get(
                            "Factory work", fields.get("Assembly", "")
                        )
                    },
                )
            )

    def collect(self) -> None:
        """Classify every reference of the board and netlist and group the purchased parts.

        Fills ``parts``, ``features``, ``module_rows``, ``module_refs``, ``pr``
        (the register rows without modules) and ``bom``. Raises RuntimeError for
        a reference without a footprint, a fabricated feature that is not
        classified, a required module that left the board, an unclassified
        footprint or an empty part list.
        """
        fs, comps = self.footprints, self.components
        for ref in sorted(set(fs) | set(comps), key=BomRows.natural):
            f = fs.get(ref)
            c = comps.get(ref)
            require(f is not None, f"{ref}: schematic has no placed PCB footprint")
            fields = (
                {e.get("name"): e.text or "" for e in c.findall("./fields/field")}
                if c is not None
                else {e.GetName(): e.GetText() for e in f.GetFields()}
            )
            fp = str(f.GetFPID().GetLibItemName())
            kind = BomRows.feature_kind(ref, fp, fields, self.known_features)
            if kind:
                require(
                    ref in self.known_features
                    or (f.IsExcludedFromBOM() and f.IsExcludedFromPosFiles()),
                    (
                        f"{ref}: new fabricated feature requires explicit "
                        f"classification, not silent omission"
                    ),
                )
                self.add_feature(ref, kind)
                continue
            self.add_part(ref, f, c, fields, fp)
        parts, features = self.parts, self.features
        require(
            set(self.module_cfg).issubset({r["Designator"] for r in parts}),
            "A required module vanished from native CAD",
        )
        require(
            set(fs) == {r["Designator"] for r in parts + features},
            "Unclassified footprint / silent omission",
        )
        require(bool(parts), "No purchased parts or modules found")
        self.module_refs = {r["Designator"] for r in self.module_rows}
        self.pr = [r for r in parts if r["Designator"] not in self.module_refs]
        self.bom = BomRows.group(parts)

    def write(self, manufacturing: Path, source: dict) -> None:
        """Write the register, BOM, module, feature, provenance and membership records.

        Inputs:
            manufacturing: Export folder.
            source: Native source hashes recorded in the membership record.
        """
        M = manufacturing
        parts, features = self.parts, self.features
        writecsv(M / FILES["register"], self.pr, REGISTER_FIELDS)
        writecsv(M / FILES["bom"], self.bom, BOM_FIELDS)
        writecsv(
            M / "07_REQUIRED_MODULES_NOT_DNP.csv",
            self.module_rows,
            self.MODULE_FIELDS,
        )
        writecsv(
            M / "assembly/PCB_FEATURES_NO_PURCHASE.csv",
            features,
            ["Designator", "Type", "Procurement"],
        )
        writecsv(
            M / "evidence/METADATA_PROVENANCE.csv",
            self.provenance,
            ["Designator", "Field", "Value", "Source", "Previous"],
        )
        writecsv(
            M / "evidence/NATIVE_ASSEMBLY_FLAGS.csv",
            self.native_flags,
            [
                "Designator",
                "DNP",
                "Native_exclude_BOM",
                "Native_exclude_positions",
                "Included",
                "Category",
            ],
        )
        writejson(
            M / "evidence/PART_MEMBERSHIP.json",
            {
                "source_sha256": source,
                "required_refs": [r["Designator"] for r in parts],
                "required_module_refs": sorted(self.module_refs, key=BomRows.natural),
                "fabricated_feature_refs": [r["Designator"] for r in features],
                "legacy_register_refs_not_in_current_board_parts": sorted(
                    set(self.old) - {r["Designator"] for r in self.pr},
                    key=BomRows.natural,
                ),
                "membership_basis": (
                    "Native schematic plus placed board, explicit fabricated "
                    "features, required module declarations; no hardcoded "
                    "count or new-reference allowlist"
                ),
            },
        )

    def apply_owner_finish(self, manufacturing: Path) -> None:
        """Mark the owner-stock modules with their finishing status and rewrite the module table.

        Inputs:
            manufacturing: Export folder with ``07_OWNER_FINISHING.csv``.
        """
        M = manufacturing
        owner_finish = {
            r["Designator"]: r for r in readcsv(M / "07_OWNER_FINISHING.csv")
        }
        for row in self.module_rows:
            if row["Designator"] in owner_finish:
                finish = owner_finish[row["Designator"]]
                row["Status"] = (
                    "REQUIRED OWNER STOCK; OWNER_INSTALL; excluded only from "
                    "JLC carrier BOM/CPL; never DNP. "
                ) + finish["Acceptance status"]
                row["Factory work"] = (
                    "Outside JLC carrier assembly scope. "
                    + finish["Required finishing"]
                )
        writecsv(
            M / "07_REQUIRED_MODULES_NOT_DNP.csv",
            self.module_rows,
            self.MODULE_FIELDS,
        )


class PlacementTable:
    """The component placement list and body-centre offsets of the purchased parts."""

    @staticmethod
    def center(f):
        """Find a footprint's body centre.

        Inputs:
            f: A board footprint.
        Outputs:
            X and Y in mm and the method used: the Fab-layer envelope when the
            footprint has one, otherwise the footprint anchor.
        """
        layer = pcbnew.B_Fab if f.GetLayer() == pcbnew.B_Cu else pcbnew.F_Fab
        gg = [
            g.GetBoundingBox()
            for g in f.GraphicalItems()
            if isinstance(g, pcbnew.PCB_SHAPE) and g.GetLayer() == layer
        ]
        if gg:
            return (
                (min(g.GetX() for g in gg) + max(g.GetRight() for g in gg)) / 2e6,
                (min(g.GetY() for g in gg) + max(g.GetBottom() for g in gg)) / 2e6,
                "Native Fab geometric envelope; factory orientation/centroid review required",
            )
        return (
            f.GetPosition().x / 1e6,
            f.GetPosition().y / 1e6,
            "Native footprint anchor; no Fab envelope, factory centroid review required",
        )

    def __init__(self, parts, footprints, anchors, ox, oy):
        """Place every purchased part.

        Inputs:
            parts: Purchased part rows with their side.
            footprints: Board footprints by reference.
            anchors: KiCad's native anchor position rows by reference.
            ox: Plot datum X in mm.
            oy: Plot datum Y in mm.
        """
        self.cpl = []
        self.offsets = []
        for r in parts:
            ref = r["Designator"]
            f = footprints[ref]
            cx, cy, method = self.center(f)
            pos = f.GetPosition()
            angle = (
                float(anchors[ref]["Rot"])
                if ref in anchors
                else f.GetOrientationDegrees()
            )
            self.cpl.append(
                dict(
                    Designator=ref,
                    **{"Mid X": f"{cx - ox:.6f}", "Mid Y": f"{oy - cy:.6f}"},
                    Rotation=f"{angle % 360:.6f}",
                    Layer=r["Side"],
                )
            )
            self.offsets.append(
                {
                    "Designator": ref,
                    "Native anchor X mm": pos.x / 1e6 - ox,
                    "Native anchor Y mm": oy - pos.y / 1e6,
                    "Body centre X mm": cx - ox,
                    "Body centre Y mm": oy - cy,
                    "Centre method": method,
                    "Rotation basis": "Native KiCad CCW; common top-view datum",
                }
            )

    def write(self, manufacturing: Path, bom: list, parts: list) -> None:
        """Require the BOM and placements to agree, then write the placement files.

        Inputs:
            manufacturing: Export folder.
            bom: Grouped BOM rows.
            parts: Purchased part rows.
        Outputs: None. Raises RuntimeError when the quantities or references of
            the BOM and placements differ.
        """
        M, cpl = manufacturing, self.cpl
        require(
            sum(r["Quantity"] for r in bom) == len(cpl) == len(parts),
            "BOM/CPL quantity mismatch",
        )
        require(
            {r["Designator"] for r in cpl}
            == {r for row in bom for r in row["Designator"].split(",")},
            "BOM/CPL reference mismatch",
        )
        writecsv(M / FILES["cpl"], cpl, CPL_FIELDS)
        writecsv(
            M / "assembly/COMPONENT_CENTRE_OFFSETS.csv",
            self.offsets,
            list(self.offsets[0]),
        )
        for side in ["Top", "Bottom"]:
            writecsv(
                M / f"assembly/04_CPL_{side.upper()}.csv",
                [r for r in cpl if r["Layer"] == side],
                CPL_FIELDS,
            )


class BoardExports:
    """Runs the native gerber, drill, PDF and IPC exports."""

    def __call__(
        self,
        run: KicadRunner,
        board,
        native: NativeSource,
        manufacturing: Path,
        out: Path,
    ):
        """Export every enabled copper layer and the fixed fabrication layers.

        Inputs:
            run: Runner for KiCad CLI commands.
            board: The loaded native board.
            native: The native board and schematic files.
            manufacturing: Export folder.
            out: Folder that receives the unnormalized IPC netlist.
        Outputs:
            The copper layer names, with the top layer first and the bottom
            layer last, and the full list of exported gerber layers.
        """
        M = manufacturing
        boardpath, schpath = native.board_path, native.schematic_path
        b = board
        # Export all enabled copper, not a fixed number of layers.
        copper = [
            b.GetLayerName(i)
            for i in b.GetEnabledLayers().Seq()
            if pcbnew.IsCopperLayer(i)
        ]
        copper = sorted(
            copper,
            key=lambda n: (
                0
                if n == "F.Cu"
                else 100
                if n == "B.Cu"
                else int(re.search(r"\d+", n).group())
            ),
        )
        layers = copper + [
            "F.Mask",
            "B.Mask",
            "F.SilkS",
            "B.SilkS",
            "F.Paste",
            "B.Paste",
            "Edge.Cuts",
        ]
        run(
            [
                "pcb",
                "export",
                "gerbers",
                "--layers",
                ",".join(layers),
                "--use-drill-file-origin",
                "--no-protel-ext",
                "--subtract-soldermask",
                "--check-zones",
                "-o",
                str(M / "gerbers") + os.sep,
                boardpath,
            ]
        )
        run(
            [
                "pcb",
                "export",
                "drill",
                "--format",
                "excellon",
                "--drill-origin",
                "plot",
                "--excellon-zeros-format",
                "decimal",
                "--excellon-units",
                "mm",
                "--excellon-separate-th",
                "--generate-map",
                "--map-format",
                "pdf",
                "--generate-report",
                "--report-path",
                M / "assembly/DRILL_REPORT.txt",
                "-o",
                str(M / "gerbers") + os.sep,
                boardpath,
            ]
        )
        for side, ls in [
            ("TOP", "F.Fab,F.SilkS,Edge.Cuts"),
            ("BOTTOM", "B.Fab,B.SilkS,Edge.Cuts"),
        ]:
            argv = [
                "pcb",
                "export",
                "pdf",
                "--mode-single",
                "--black-and-white",
                "--sketch-pads-on-fab-layers",
                "--layers",
                ls,
            ]
            if side == "BOTTOM":
                argv += ["--mirror"]
            run(argv + ["-o", M / f"assembly/ASSEMBLY_{side}.pdf", boardpath])
        run(["sch", "export", "pdf", "-o", M / "assembly/SCHEMATIC.pdf", schpath])
        run(
            [
                "pcb",
                "export",
                "ipcd356",
                "-o",
                out / "IPC_NATIVE_UNNORMALIZED_NOT_FOR_FACTORY.txt",
                boardpath,
            ]
        )
        return copper, layers


class ConnectorSchedules:
    """Derives the connector pinout and finished cable schedules from the netlist."""

    def __call__(
        self,
        root,
        comps: dict,
        fs: dict,
        parts: list,
        control: Path,
        manufacturing: Path,
    ):
        """Write the connector pinout, cable schedule and Teensy socket pin map.

        Inputs:
            root: Parsed netlist XML root.
            comps: Netlist components by reference.
            fs: Board footprints by reference.
            parts: Purchased part rows, for header MPN and role.
            control: Reviewed ``release_inputs`` folder with the controlled schedules.
            manufacturing: Export folder.
        Outputs:
            The pinout rows and the net of each (reference, pin). Raises
            RuntimeError when the connector or cavity membership changed, a
            connector is absent from the schematic, or a cable schedule row
            belongs to another header MPN.
        """
        M = manufacturing
        pins = {
            (x.get("ref"), x.get("pin")): n.get("name")
            for n in root.findall("./nets/net")
            for x in n.findall("node")
        }
        ports = readcsv(control / "09_CONNECTOR_PINOUTS.csv")
        port_refs = {r["Connector"] for r in ports}
        require(
            port_refs == {r for r in fs if re.fullmatch(r"J\d+", r)},
            "Connector schedule membership changed; review new/removed connectors explicitly",
        )
        libparts = {
            (x.get("lib"), x.get("part")): x for x in root.findall("./libparts/libpart")
        }
        required_contacts = set()
        for ref in port_refs:
            require(ref in comps, "Connector absent from schematic: " + ref)
            libsrc = comps[ref].find("libsource")
            lib = libparts[(libsrc.get("lib"), libsrc.get("part"))]
            required_contacts.update(
                (ref, x.get("num")) for x in lib.findall("./pins/pin")
            )
            required_contacts.update(
                (ref, x.GetNumber()) for x in fs[ref].Pads() if x.GetNumber()
            )
        require(
            required_contacts == {(r["Connector"], r["Cavity"]) for r in ports},
            "Connector cavity membership changed; review harness contract before export",
        )
        for r in ports:
            require(r["Connector"] in comps, "Connector contract reference missing")
            key = (r["Connector"], r["Cavity"])
            r["Net"] = pins.get(key, "")
            r["Use"] = (
                "Not connected"
                if not r["Net"] or r["Net"].startswith("unconnected-")
                else "Connected"
            )
            r["Header MPN"] = next(
                q["MPN"] for q in parts if q["Designator"] == r["Connector"]
            )
        writecsv(M / "09_CONNECTOR_PINOUTS.csv", ports, list(ports[0]))
        schedule = readcsv(control / "10_FINISHED_CABLE_SCHEDULE.csv")
        require(
            {r["Connector"] for r in schedule} == port_refs,
            "Cable schedule membership differs from connectors",
        )
        for row in schedule:
            ref = row["Connector"]
            part = next(q for q in parts if q["Designator"] == ref)
            require(
                row["Header MPN"] == part["MPN"],
                f"{ref}: cable mating contract belongs to another header MPN",
            )
            row["Role"] = part["Comment"]
        # Procurement, length, and termination remain controlled inputs;
        # they are not inferred from connectivity.
        writecsv(M / "10_FINISHED_CABLE_SCHEDULE.csv", schedule, list(schedule[0]))
        writecsv(
            M / "assembly/TEENSY_SOCKET_PINMAP.csv",
            [r for r in ports if r["Connector"] in ["J16", "J17"]],
            list(ports[0]),
        )
        return ports, pins


class HistoricalEvidence:
    """Indexes the evidence of earlier exports so it is not mistaken for current evidence."""

    def __call__(self, project: Path, manufacturing: Path) -> None:
        """Write ``evidence/HISTORICAL_EVIDENCE_INDEX.json``.

        Inputs:
            project: Native project folder with earlier exports.
            manufacturing: Export folder.
        """
        historical = []
        for p in (project / "deliverables/jlcpcb/evidence").rglob("*"):
            if not p.is_file() or p.name.lower() in (
                "desktop.ini",
                "thumbs.db",
                ".ds_store",
            ):
                continue
            historic_source = None
            if p.suffix == ".json":
                try:
                    historic_source = json.loads(p.read_text(encoding="utf-8-sig")).get(
                        "source_sha256"
                    )
                except (ValueError, AttributeError):
                    pass
            historical.append(
                {
                    "path": p.relative_to(project).as_posix(),
                    "sha256": sha(p),
                    "recorded_source_sha256": historic_source,
                    "status": (
                        "HISTORICAL NOT REUSED; rerun separately against this "
                        "source before current-qualification claims"
                    ),
                }
            )
        writejson(manufacturing / "evidence/HISTORICAL_EVIDENCE_INDEX.json", historical)


class FactoryReview:
    """One build of the manufacturing review set from a native project."""

    def __init__(self, project: Path, out: Path, cli: Path):
        """Resolve the project and the new output folder.

        Inputs:
            project: Native project folder.
            out: New or empty output folder outside the project.
            cli: KiCad command-line program, or None to find one.
        """
        self.project = project.resolve()
        self.out = out.resolve()
        self.manufacturing = self.out / "jlcpcb"
        self.cli_choice = cli

    def prepare(self) -> Path:
        """Require a new output folder outside the project and create its subfolders.

        Outputs:
            The reviewed ``release_inputs`` folder. Raises RuntimeError when the
            output is inside the project or not empty, or the controlled
            inputs are not reviewed.
        """
        P, OUT, M = self.project, self.out, self.manufacturing
        require(not OUT.is_relative_to(P), "Output must be outside source project")
        require(
            not OUT.exists() or not any(OUT.iterdir()),
            "Output must be new/empty to prevent stale files",
        )
        control = factory_input_dir(P)
        OUT.mkdir(parents=True, exist_ok=True)
        for n in ["gerbers", "assembly", "evidence", "sourcing", "views"]:
            (M / n).mkdir(parents=True, exist_ok=True)
        return control

    def load_native(self) -> None:
        """Load the board and the netlist and index their references.

        Fills ``board``, ``footprints``, ``netlist`` and ``components``. Raises
        RuntimeError when the board exceeds the owner's copper layer limit or a
        reference is duplicated.
        """
        P, M = self.project, self.manufacturing
        b = pcbnew.LoadBoard(str(self.native.board_path))
        layer_limit = data_path(P, "BOARD_LAYER_LIMIT.json")
        if layer_limit.exists():
            maximum = int(
                json.loads(layer_limit.read_text(encoding="utf8"))[
                    "maximum_copper_layers"
                ]
            )
            require(
                b.GetCopperLayerCount() <= maximum,
                (
                    f"Board has {b.GetCopperLayerCount()} copper layers; owner "
                    f"limit is {maximum}. No manufacturing export."
                ),
            )
        fl = list(b.GetFootprints())
        fs = {f.GetReference(): f for f in fl}
        require(len(fs) == len(fl), "Duplicate PCB references")
        root = ET.parse(M / "evidence/NETLIST.xml").getroot()
        cl = root.findall("./components/comp")
        comps = {c.get("ref"): c for c in cl}
        require(len(comps) == len(cl), "Duplicate schematic references")
        self.board, self.footprints = b, fs
        self.netlist, self.components = root, comps

    def export_placements(self, run: KicadRunner) -> None:
        """Export the native anchors, place every part and run the owner finishing step.

        Fills ``ox``, ``oy``, ``placements`` and ``scope``.

        Inputs:
            run: Runner for KiCad CLI commands.
        """
        P, M, catalog = self.project, self.manufacturing, self.catalog
        origin = self.board.GetDesignSettings().GetAuxOrigin()
        self.ox = origin.x / 1e6
        self.oy = origin.y / 1e6
        run(
            [
                "pcb",
                "export",
                "pos",
                "--format",
                "csv",
                "--units",
                "mm",
                "--side",
                "both",
                "--use-drill-file-origin",
                "-o",
                M / "assembly/NATIVE_ANCHOR_POSITIONS.csv",
                self.native.board_path,
            ]
        )
        anchors = {
            r["Ref"]: r for r in readcsv(M / "assembly/NATIVE_ANCHOR_POSITIONS.csv")
        }
        self.placements = PlacementTable(
            catalog.parts, self.footprints, anchors, self.ox, self.oy
        )
        self.placements.write(M, catalog.bom, catalog.parts)
        self.scope = FactoryScope.export(
            P,
            M,
            catalog.parts,
            self.placements.cpl,
            self.footprints,
            catalog.module_refs,
            catalog.native_fields_by_ref,
        )
        catalog.apply_owner_finish(M)

    def export_filled_vias(self) -> None:
        """Write the scheduled filled and capped vias.

        Writes ``assembly/EXPLICIT_FILLED_CAPPED_VIAS.csv`` and fills ``vias``,
        ``tracks`` and ``filled``.
        """
        b, ox, oy = self.board, self.ox, self.oy
        self.vias = [t for t in b.GetTracks() if isinstance(t, pcbnew.PCB_VIA)]
        self.tracks = [t for t in b.GetTracks() if not isinstance(t, pcbnew.PCB_VIA)]
        self.filled = []
        for v in self.vias:
            if v.GetFillingMode() == pcbnew.FILLING_MODE_FILLED:
                q = v.GetPosition()
                self.filled.append(
                    {
                        "UUID": v.m_Uuid.AsString(),
                        "X mm from datum": q.x / 1e6 - ox,
                        "Y mm from datum": oy - q.y / 1e6,
                        "Land mm": copper_width(v, layer=pcbnew.F_Cu) / 1e6,
                        "Drill mm": v.GetDrill() / 1e6,
                        "Net": v.GetNetname(),
                        "Process": (
                            "Epoxy fill and copper cap; retain every connector and "
                            "module insertion hole"
                        ),
                    }
                )
        writecsv(
            self.manufacturing / "assembly/EXPLICIT_FILLED_CAPPED_VIAS.csv",
            self.filled,
            [
                "UUID",
                "X mm from datum",
                "Y mm from datum",
                "Land mm",
                "Drill mm",
                "Net",
                "Process",
            ],
        )

    def outline(self) -> list:
        """Measure the native board outline.

        Outputs:
            The width and height of the Edge.Cuts centreline in mm. Raises
            RuntimeError when the outline is open, invalid or empty.
        """
        outline = pcbnew.SHAPE_POLY_SET()
        require(
            self.board.GetBoardPolygonOutlines(outline, False),
            "Open or invalid board outline",
        )
        centreline_box = outline.BBox()
        outline_mm = [
            centreline_box.GetWidth() / 1e6,
            centreline_box.GetHeight() / 1e6,
        ]
        require(all(v > 0 for v in outline_mm), "Empty board outline")
        return outline_mm

    def identity(self, outline_mm: list) -> dict:
        """Compose ``SOURCE_IDENTITY.json``: the counts, hashes and limits of this export.

        Inputs:
            outline_mm: Width and height of the Edge.Cuts centreline in mm.
        Outputs:
            The identity record, with the assembly scope, rotation policy and
            factory upload fields added.
        """
        catalog, scope = self.catalog, self.scope
        parts, features = catalog.parts, catalog.features
        fs, b = self.footprints, self.board
        cpl = self.placements.cpl
        assembly_categories = collections.Counter()
        for part in parts:
            f = fs[part["Designator"]]
            kind = (
                "Required module"
                if part["Designator"] in catalog.module_refs
                else "THT/mixed"
                if any(p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH for p in f.Pads())
                else "SMT"
            )
            assembly_categories[part["Side"] + " " + kind] += 1
        bbox = b.GetBoardEdgesBoundingBox()
        identity = {
            "revision": self.native.stem,
            "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "source_sha256": self.native.hashes,
            "dependency_sha256": self.deps,
            "source_project": str(self.project),
            "kicad_cli": self.cli,
            "kicad_version": self.version,
            "readiness_level": "FACTORY_ENGINEERING_REVIEW_ONLY",
            "files": FILES,
            "board_components": len(catalog.pr),
            "required_plugin_modules": len(catalog.module_rows),
            "bom_group_rows": len(catalog.bom),
            "cpl_rows": len(cpl),
            "placements_by_side": dict(collections.Counter(r["Layer"] for r in cpl)),
            "assembly_categories": dict(assembly_categories),
            "fabricated_feature_types": dict(
                collections.Counter(r["Type"] for r in features)
            ),
            "footprints": len(fs),
            "fabricated_features": len(features),
            "tracks": len(self.tracks),
            "vias": len(self.vias),
            "selectively_filled_capped_vias": len(self.filled),
            "minimum_via_drill_mm": min(
                (v.GetDrill() / 1e6 for v in self.vias), default=None
            ),
            "maximum_filled_via_drill_mm": max(
                (r["Drill mm"] for r in self.filled), default=None
            ),
            "connector_contacts": len(self.ports),
            "pin_net_entries": len(self.pins),
            "nets": len(self.netlist.findall("./nets/net")),
            "native_erc_violations": 0,
            "native_drc_violations": 0,
            "unconnected_items": 0,
            "schematic_parity": 0,
            "ignored_check_categories": self.erc_ignored,
            "ignored_DRC_categories": [],
            "copper_layers": len(self.copper),
            "exported_gerber_layers": self.layers,
            "edge_cuts_bbox_mm": [bbox.GetWidth() / 1e6, bbox.GetHeight() / 1e6],
            "factory_order_released": False,
            "stock_reserved": False,
            "physically_tested": False,
            "datum_mm": [self.ox, self.oy],
            "datum": "X right, Y up, common unmirrored top-view CPL coordinates",
            "exporter_script_sha256": {
                p.name: sha(p) for p in Path(__file__).parent.glob("*.py")
            },
            "current_evidence_limit": (
                "Native rule checks and export consistency only; prior "
                "independent/electrical/visual audits are historical until "
                "rerun"
            ),
        }
        identity.update(edge_cuts_centerline_bbox_mm=outline_mm)
        identity.update(
            factory_cpl_rows=scope["factory_parts"],
            factory_bom_group_rows=scope["factory_bom_groups"],
            owner_install_rows=scope["owner_install_parts"],
            factory_upload_files={
                "bom": "02_BOM_JLCPCB.csv",
                "cpl": "03_CPL_JLCPCB.csv",
            },
            assembly_policy_sha256=scope["policy_sha256"],
        )
        identity.update(
            assembly_rotation_policy_sha256=scope["rotation_policy_sha256"],
            assembly_rotation_offset_count=len(
                scope["qualified_catalog_rotation_offsets"]
            ),
        )
        return identity

    def finish(self, identity: dict) -> None:
        """Normalize the IPC netlist, add the supplements and documents, then bind the evidence.

        Writes ``CURRENT_EVIDENCE_BINDING.json``, ``MANIFEST_SHA256.json`` and
        ``candidate_identity.json``.

        Inputs:
            identity: The record written as ``SOURCE_IDENTITY.json``.
        Outputs: None. Raises RuntimeError when a source or dependency changed
            during the export.
        """
        P, OUT, M = self.project, self.out, self.manufacturing
        source, deps = self.native.hashes, self.deps
        from release.normalize_ipc_netlist import IpcNetlist

        IpcNetlist.normalize(P, OUT)
        from core.fabrication_supplements import FabricationSupplements

        FabricationSupplements.apply(P, M)
        from release.generate_factory_documents import FactoryDocuments

        FactoryDocuments.generate(P, OUT)
        assert_current_inputs(P, source, deps)
        evidence_files = {
            p.relative_to(M).as_posix(): sha(p)
            for p in (M / "evidence").rglob("*")
            if p.is_file()
            and p.name.lower() not in ("desktop.ini", "thumbs.db", ".ds_store")
        }
        writejson(
            M / "evidence/CURRENT_EVIDENCE_BINDING.json",
            {
                "source_sha256": source,
                "dependency_sha256": deps,
                "files": evidence_files,
                "inputs_unchanged_through_export": True,
            },
        )
        manifest = {
            p.relative_to(M).as_posix(): sha(p)
            for p in M.rglob("*")
            if p.is_file()
            and p.name.lower() not in ("desktop.ini", "thumbs.db", ".ds_store")
        }
        writejson(
            M / "MANIFEST_SHA256.json", {"source_sha256": source, "files": manifest}
        )
        writejson(OUT / "candidate_identity.json", identity)

    def build(self) -> dict:
        """Run the native checks and exports and write the whole review set.

        Outputs:
            The identity record. Raises RuntimeError when any mandatory check,
            classification or consistency requirement fails.
        """
        P, OUT, M = self.project, self.out, self.manufacturing
        control = self.prepare()
        self.cli = str(kicad_cli(self.cli_choice))
        self.native = NativeSource(P)
        self.erc_ignored = self.native.write_erc_configuration(M)
        self.deps = self.native.snapshot_dependencies(OUT)
        run = KicadRunner(self.cli, M / "evidence/TOOL_RUNS.json")
        self.version = run(["version"])
        NativeChecks()(run, self.native, M)
        self.load_native()
        self.catalog = PartCatalog(self.footprints, self.components, control)
        self.catalog.collect()
        self.catalog.write(M, self.native.hashes)
        self.export_placements(run)
        self.copper, self.layers = BoardExports()(run, self.board, self.native, M, OUT)
        self.export_filled_vias()
        self.ports, self.pins = ConnectorSchedules()(
            self.netlist,
            self.components,
            self.footprints,
            self.catalog.parts,
            control,
            M,
        )
        HistoricalEvidence()(P, M)
        identity = self.identity(self.outline())
        writejson(M / "SOURCE_IDENTITY.json", identity)
        self.finish(identity)
        return identity


class FactoryReviewCommand:
    """Command-line entry: build the manufacturing review set of a project."""

    def __call__(self, argv=None):
        """Parse the command line, build the review set and print its headline counts.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        """
        ap = argparse.ArgumentParser()
        ap.add_argument("--project", type=Path, default=default_project(__file__))
        ap.add_argument("--out", type=Path, required=True)
        ap.add_argument("--cli", type=Path)
        a = ap.parse_args(argv)
        identity = FactoryReview(a.project, a.out, a.cli).build()
        print(
            json.dumps(
                {
                    k: identity[k]
                    for k in [
                        "board_components",
                        "required_plugin_modules",
                        "bom_group_rows",
                        "cpl_rows",
                        "fabricated_features",
                        "copper_layers",
                        "kicad_version",
                    ]
                },
                indent=2,
            )
        )


main = FactoryReviewCommand()

if __name__ == "__main__":
    main()
