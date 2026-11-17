"""Generate source-bound factory instructions. Run by generate_factory_review after native exports.

All quantities/geometry evidence comes from the just-created handoff. Manufacturing
requirements remain requirements, and previous cart/import evidence is historical.
"""

import itertools
import json
import math
from pathlib import Path

from core.paths import data_path, data_reference, support_input_name

from core.export_helpers import (
    factory_input_dir,
    readcsv,
    require,
    sha,
    writecsv,
    writejson,
)


def generate(project, output):
    P = Path(project)
    OUT = Path(output)
    M = OUT / "jlcpcb"
    identity = json.loads((M / "SOURCE_IDENTITY.json").read_text())
    IPC = json.loads((M / "evidence/IPC_D356_VALIDATION.json").read_text())
    D = json.loads((M / "evidence/NATIVE_DRILL_MAP.json").read_text())
    ER = json.loads((M / "evidence/ERC_RULE_CONFIGURATION.json").read_text())
    for label, evidence in [("IPC", IPC), ("drill", D), ("ERC settings", ER)]:
        require(
            evidence["source_sha256"] == identity["source_sha256"],
            f"{label}: factory document input belongs to a different source",
        )
    stackpath = data_path(P, "STACKUP_CURRENT.json")
    stack = json.loads(stackpath.read_text(encoding="utf-8-sig"))
    require(stack.get("schema") == 1, "Unsupported current stack contract")
    require(
        stack.get("stackup_id")
        and stack.get("calculator")
        and stack.get("dielectric_thicknesses_mm"),
        (
            "Current four-layer stack observations are incomplete; "
            "finish the two-ounce-inner calculator review before export"
        ),
    )
    require(
        stack["inner_copper_oz"] >= stack["minimum_inner_copper_oz"],
        "Inner copper is below the reviewed power-path requirement",
    )
    L = stack["copper_layers"]
    require(
        1 <= L <= stack["maximum_copper_layers"] <= 4
        and identity["copper_layers"] == L,
        "Native layer count differs from the maximum-four-layer stack contract",
    )
    require(
        len(stack["copper_thicknesses_mm"]) == L
        and len(stack["dielectric_thicknesses_mm"]) == L - 1,
        "Stack thickness inventory differs from layer count",
    )
    require(
        stack.get("factory_acceptance") == "PENDING"
        and stack.get("production_released") is False,
        "Stack contract must retain review-only scope",
    )
    for n, h in stack["evidence_files"].items():
        evidence = data_reference(P, n)
        require(evidence.is_file() and sha(evidence) == h, "Stack calculator evidence changed: " + n)
    stack_name = stack["stackup_id"]
    order_mm = stack["nominal_order_thickness_mm"]
    finished_mm = stack["displayed_finished_thickness_mm"]
    finished_tolerance = stack["displayed_finished_thickness_tolerance_percent"]
    copper_weight = (
        f"{stack['outer_copper_oz']:g} oz outer / {stack['inner_copper_oz']:g} oz inner"
    )
    copper_text = "/".join(f"{v:.3f}" for v in stack["copper_thicknesses_mm"])
    dielectric_text = "/".join(f"{v:.4f}" for v in stack["dielectric_thicknesses_mm"])
    cal = stack["calculator"]
    calculator_text = "; ".join(
        (
            f"approximately {r['target_ohm']:.2f}Ω at "
            f"{r['native_width_mm']:.3f}mm width (solver returned "
            f"{r['returned_width_mm']:.4f}mm)"
        )
        for r in cal["inverse_results"]
    )
    writejson(
        M / "evidence/CURRENT_STACKUP_CONTRACT.json",
        {
            "source_sha256": identity["source_sha256"],
            "contract": support_input_name(P, stackpath).as_posix(),
            "contract_sha256": sha(stackpath),
            "stack": stack,
            "factory_approved": False,
            "limits": (
                "Observed calculator inputs/results only; actual routing, "
                "return paths, copper capacity and factory acceptance "
                "require separate review"
            ),
        },
    )
    require(
        D["counts"]["filled_capped_vias"] == identity["selectively_filled_capped_vias"],
        "Native fill schedules disagree",
    )
    fill = readcsv(M / "assembly/EXPLICIT_FILLED_CAPPED_VIAS.csv")
    reg = readcsv(M / identity["files"]["register"])
    mods = readcsv(M / "07_REQUIRED_MODULES_NOT_DNP.csv")
    cpl = readcsv(M / identity["files"]["cpl"])
    require(
        len(fill) == identity["selectively_filled_capped_vias"],
        "Filled/capped process CSV count differs from native census",
    )
    old05 = factory_input_dir(P) / "STACKUP_REFERENCE.md"
    if old05.is_file():
        writejson(
            M / "evidence/RETAINED_STACK_CALCULATOR_EVIDENCE.json",
            {
                "source": str(old05),
                "sha256": sha(old05),
                "status": (
                    "Superseded calculator history only; not the current stack "
                    "or current factory acceptance"
                ),
            },
        )
    coincident = []
    nearest = {}
    for a, b in itertools.combinations(cpl, 2):
        dist = math.hypot(
            float(a["Mid X"]) - float(b["Mid X"]), float(a["Mid Y"]) - float(b["Mid Y"])
        )
        if a["Layer"] != b["Layer"] and dist <= 0.001:
            coincident.append(
                {
                    "Designator A": a["Designator"],
                    "Side A": a["Layer"],
                    "Designator B": b["Designator"],
                    "Side B": b["Layer"],
                    "X mm": a["Mid X"],
                    "Y mm": a["Mid Y"],
                    "Meaning": (
                        "Distinct opposite-side placements; retain both genuine "
                        "centres and side fields"
                    ),
                }
            )
        if a["Layer"] == b["Layer"] and (
            a["Layer"] not in nearest or dist < nearest[a["Layer"]]["distance_mm"]
        ):
            nearest[a["Layer"]] = {
                "references": [a["Designator"], b["Designator"]],
                "distance_mm": dist,
            }
    writecsv(
        M / "assembly/OPPOSITE_SIDE_CENTRE_OVERLAPS.csv",
        coincident,
        ["Designator A", "Side A", "Designator B", "Side B", "X mm", "Y mm", "Meaning"],
    )
    writejson(
        M / "evidence/CPL_CENTRE_REVIEW.json",
        {
            "source_sha256": identity["source_sha256"],
            "opposite_side_coincidences": len(coincident),
            "nearest_same_side_centres": nearest,
            "limits": (
                "Geometry inventory; factory placement centroid/rotation acceptance is separate"
            ),
        },
    )
    usb = []
    for ref in ["J4", "J28", "J30"]:
        holes = [
            r for r in D["records"] if r["reference"] == ref and r["kind"] == "NPTH"
        ]
        require(len(holes) == 2, f"{ref}: expected two GT-USB-7051A locating holes")
        shapes = sorted(
            tuple(
                sorted(
                    [round(r["drill_local_x_mm"], 4), round(r["drill_local_y_mm"], 4)]
                )
            )
            for r in holes
        )
        require(
            shapes == [(0.65, 0.65), (0.65, 1.0)],
            f"{ref}: GT-USB-7051A RevA1 round/oval locating holes not corrected: {shapes}",
        )
        usb.extend(holes)
    writejson(
        M / "evidence/USB_C_LOCATOR_DRILL_CHECK.json",
        {
            "source_sha256": identity["source_sha256"],
            "references": ["J4", "J28", "J30"],
            "native_holes": usb,
            "expected_drawing": (
                "GT-USB-7051A Rev A1, 2024-12-06: left 0.65 mm round, "
                "right 1.00 x 0.65 mm oval along footprint local X; 7.50 "
                "mm centre spacing"
            ),
            "check_scope": (
                "Hole quantity and local drill dimensions checked here; "
                "exact locator position/orientation belongs to independent "
                "part/geometry review"
            ),
            "primary_drawing": "https://www.lcsc.com/datasheet/C2843970.pdf",
        },
    )
    S = json.loads((M / "evidence/ASSEMBLY_SCOPE.json").read_text())
    require(
        S["source_sha256"] == identity["source_sha256"],
        "Assembly scope source mismatch",
    )
    FN = S["factory_parts"]
    FG = S["factory_bom_groups"]
    require(
        S["owner_install_parts"] == len(mods) == identity["required_plugin_modules"],
        "Owner-install module count differs from native and module records",
    )
    N = identity["cpl_rows"]
    B = identity["board_components"]
    U = identity["required_plugin_modules"]
    G = identity["bom_group_rows"]
    F = identity["selectively_filled_capped_vias"]
    features = "; ".join(
        f"{v} {k.lower()}" for k, v in identity["fabricated_feature_types"].items()
    )
    categories = "; ".join(
        f"{v} {k}" for k, v in identity["assembly_categories"].items()
    )
    datum = ",".join(str(v) for v in identity["datum_mm"])
    blank = [r["Designator"] for r in reg if not r["LCSC Part #"]]
    ignored = ", ".join(ER["ignored_categories"]) or "none"
    outline = identity.get("edge_cuts_centerline_bbox_mm")
    require(
        isinstance(outline, list)
        and len(outline) == 2
        and all(isinstance(v, (int, float)) and v > 0 for v in outline),
        "Missing native Edge.Cuts centreline dimensions",
    )
    outline_text = " × ".join(f"{v:g}" for v in outline)

    def mm(value):
        return f"{value:.3f} mm" if value is not None else "none"

    def write(name, body):
        (M / name).write_text(body.strip() + "\n", encoding="utf8")

    write(
        "05_BUILD_AND_ASSEMBLY.md",
        (
            f"""
# GRANDE Jessie v4.1 — fabrication and assembly

**Engineering review / quote only. No production, """
            "purchasing or paid work is released by these files.** Use "
            "the source hashes in SOURCE_IDENTITY.json and the "
            f"complete matched files {identity['files']['bom']} / "
            f"{identity['files']['cpl']}: {N} required placements, "
            f"comprising {B} board parts and {U} owner-stock modules, "
            f"grouped into {G} BOM rows. No automatic DNPs. Fabricated "
            f"""features: {features}.

**JLCPCB carrier upload pair: 02_BOM_JLCPCB.csv / """
            f"03_CPL_JLCPCB.csv — {FN} factory-assembled references in "
            f"{FG} exact-identity/land-pattern BOM groups.** The "
            f"COMPLETE pair above remains the {N}-part system record "
            "and is not the JLC assembly upload. Required owned U3/U9 "
            "remain in that system record and in "
            "07_OWNER_FINISHING.csv; their already fitted male headers "
            "(confirmed by the owner) and later plug-in installation "
            "are a separate required finishing stage. Factory fits "
            "J16/J17/J18 sockets. Neither module is DNP or an "
            "automatically deleted requirement. Scope is derived from "
            "ASSEMBLY_POLICY.json; see "
            f"""evidence/ASSEMBLY_SCOPE.json.

## Fabrication

- Finished outline: {outline_text} mm from the native """
            f"Edge.Cuts centreline, {L} copper layers, nominal order "
            f"selection {order_mm:g} mm FR4, ENIG, green mask, white "
            "legend. The edge-stroke bounding envelope is "
            f"{identity['edge_cuts_bbox_mm'][0]:.5f} × "
            f"{identity['edge_cuts_bbox_mm'][1]:.5f}mm; do not scale "
            f"""CAM.
- Required stack: **{stack_name}, {copper_weight}**. """
            f"Published finished thickness {finished_mm:g}mm "
            f"±{finished_tolerance:g}%; nominal order selection "
            f"{order_mm:g}mm. {stack['layer_function_status']} The "
            "factory must confirm actual laminate, copper and "
            f"""impedance before production.
- Observed calculator stack: copper {copper_text}mm; """
            f"dielectric gaps {dielectric_text}mm. See "
            "STACKUP_CURRENT.json and "
            "evidence/CURRENT_STACKUP_CONTRACT.json. These are "
            f"""recorded calculator observations, not factory approval.
- Minimum native via drill: """
            f"{mm(identity['minimum_via_drill_mm'])}. Require the "
            "small-drill process and **epoxy fill/copper cap at "
            f"exactly {F} scheduled vias**, maximum scheduled drill "
            f"{mm(identity['maximum_filled_via_drill_mm'])}. Use "
            "assembly/EXPLICIT_FILLED_CAPPED_VIAS.csv. Do not fill "
            "connector/module insertion holes. Ink plugging/tenting "
            f"""does not replace solderable caps beneath SMT lands.
- Full native drill schedule: {D["counts"]["holes"]} """
            f"holes, comprising {D['counts']['PTH']} PTH and "
            f"{D['counts']['NPTH']} NPTH, with {D['counts']['slots']} "
            "oval slots. See "
            "assembly/NATIVE_DRILL_AND_SLOT_SCHEDULE.csv. GT-USB-7051A "
            "locating holes are **one 0.65 mm round plus "
            f"""one1.00×0.65mm oval**, not two 0.70 mm round holes.
- Common CAM/CPL datum: native ({datum}) mm; X right, Y """
            "up, common unmirrored top-view coordinates on both sides. "
            f"""Do not independently re-zero or mirror a placement file.
- Factory supplies external panel """
            "rails/fiducials/tooling, quotes a suitable array, and "
            "depanelizes/inspects before shipment. Preserve the "
            "finished outline/mounting holes. Select production-file "
            f"""confirmation before any release.

## Assembly and exact sourcing

Standard PCBA, both sides, SMT plus THT. Current native """
            f"classification: {categories}. The assembler must agree "
            "soldering sequence, underside protection, hidden-joint "
            "inspection and all manual operations. All cable-entry "
            "connectors, service LEDs, test points, large capacitor, "
            "modules and sockets retain their native top-side "
            f"""placement.

Match every manufacturer/MPN and value/package in the """
            "regenerated register. **Previous JLC imports and unpaid "
            "carts belong to an earlier source and are historical "
            "until rematched.** Native LCSC fields currently blank for "
            f"board references: {', '.join(blank) or 'none'}. Blank "
            "catalog fields do not allow omission. A verified catalog "
            "identity is not inventory reservation. Factory confirms "
            "current availability, quantities and attrition for every "
            "required group, including global/consigned supply. No "
            f"""unreviewed substitution.

The COMPLETE CPL preserves native KiCad CCW angles and """
            "real Fab-body centres. The factory CPL applies only the "
            f"{identity.get('assembly_rotation_offset_count', 0)} "
            "explicitly qualified catalog-angle offsets controlled by "
            "ASSEMBLY_ROTATION_OFFSETS.json; see "
            "assembly/ASSEMBLY_ROTATION_OFFSETS.csv for native/factory "
            "angles, exact parts and pin-1 qualification evidence. "
            "Coordinates, side, native footprints and routing are "
            "unchanged. Check pin 1, cathodes, FET orientation, "
            "electrolytic polarity and all connector cavities against "
            "manufacturer drawings. COMPONENT_CENTRE_OFFSETS.csv gives "
            f"anchor offsets. There are {len(coincident)} opposite-side "
            "centre coincidences in OPPOSITE_SIDE_CENTRE_OVERLAPS.csv; "
            "retain both sides and do not invent offsets to bypass an "
            "importer. The factory reviews its actual placement "
            f"""preview.

JP1/JP2 are fabricated copper solder selectors:1–2 AUX is """
            "default. Cut1–2 before bridging2–3. No jumper cap and no "
            f"""three-way bridge.

## Required owner-stock modules and cables

U3 and U9 are owner stock outside the JLC carrier """
            "assembly upload: **do not buy duplicates**. The owner "
            "confirmed compatible male headers are already fitted to "
            "both modules. Quantity allocation, orientation, "
            "engagement, clearance and unpowered continuity remain "
            "pre-power checks; no new header preparation is requested. "
            "U3 requirement is pinned PJRC TEENSY41_PINS (Teensy 4.1 "
            "with pins); the owner confirmed that male headers are "
            "already fitted. Insert the owned module into "
            "factory-fitted J16/J17 after checking orientation, "
            "engagement and the final source-controlled host-VBUS "
            "isolation. No JLC header preparation or owner "
            "soldering/bridge cutting is requested by this carrier "
            "quote. CA1 represents a planned factory-finished shielded "
            "Micro-B pigtail; its installation and complete USB path "
            f"""require verification.

U9 requirement is original Adafruit 3013 RTC. The owner """
            "confirmed male headers are already fitted. Insert the "
            "owned module into factory-fitted J18 after checking the "
            "existing header direction, engagement/IC clearance, "
            "battery holder upward and VIN at J18.1. No JLC header "
            "preparation or owner soldering is requested. Do not "
            "substitute 5188. CR1220 shipping/supply is agreed "
            "separately. 07_OWNER_FINISHING.csv controls required "
            "owner plug-in installation; the system module record "
            "remains in 07_REQUIRED_MODULES_NOT_DNP.csv. **Retain the "
            "stock Teensy VIN/VUSB bridge. CA1.1 is intentionally "
            "isolated from host 5 V; the VBUS-qualified data switch "
            "controls attachment. SYS5_CORE supplies VIN at J16.1.** "
            "Native checks must verify the switch/monitor pin-net map "
            "as well as CA1.1 NC and J16.1 SYS5_CORE; powered USB "
            "qualification remains pending. Use J28 for normal host "
            "service; isolate external VIN before connecting the "
            "Teensy Micro-B directly to another host. No owner "
            "crimping, stripping, contact insertion, screw/spring "
            "termination, solder preparation or depanelization is "
            "intended. The JLC carrier scope fits the sockets only. "
            "Both owned modules already have male headers; the owner "
            "plugs them into the factory-fitted sockets. Final "
            "source-bound USB isolation, quantity allocation, "
            "orientation and physical fit remain pre-power checks; no "
            "additional module preparation is included in this carrier "
            f"""quote.

External finished harnesses/waterproof exterior """
            "female–female wall couplers are separately controlled in "
            "09/10 and their assembly design. Ordinary PCBA alone does "
            "not supply them. Preserve camera Ethernet cables intact. "
            "MTi native USB through CA-MP2 requires a completed "
            "shield/drain and length-controlled finished harness; the "
            "stock unshielded-header USB path is not automatically "
            f"""qualified.

## Controlled impedance and evidence

Observed JLC calculator geometry: {cal["geometry"]}, """
            f"{cal['signal_layer']} referenced to "
            f"{cal['reference_layer']}, {cal['trace_gap_mm']:.3f}mm "
            "pair gap. Approximate nominal results inferred by "
            f"matching the returned width: **{calculator_text}**. The "
            "numerical solver tolerance "
            f"({cal['solver_tolerance_percent']:g}%) is not "
            "manufacturing impedance tolerance. These observations do "
            "not qualify actual route widths/gaps, return planes, "
            "escapes, ESD stubs or transitions. Review final native "
            "geometry and copper current capacity; factory "
            "calculation/coupon acceptance and later link testing "
            f"""remain necessary.

Fresh native ERC reports 0 under its configured rules; """
            f"ignored ERC categories are: {ignored}. They are "
            "disclosed, not claimed passed. Fresh "
            "DRC/parity/unconnected checks are 0 with no ignored DRC "
            "categories. Native IPC validation covers "
            f"{IPC['native_pad_records_checked']} copper pads and "
            f"{IPC['native_via_records_checked']} vias: "
            f"{IPC['total_test_records']} test records, "
            f"{IPC['unique_connected_signal_aliases']} full net "
            f"aliases. {IPC['non_copper_pad_records_excluded']} "
            "non-copper pad objects are excluded from electrical test "
            "contacts. Mask/name normalization preserves native "
            "geometry and is independently read back; see "
            "evidence/IPC_D356_VALIDATION.json. **Independent CAM "
            "parsing and actual bare-board electrical testing are "
            "separate; historical counts/reports are not current "
            f"""results.**

Before sensor hookup: unpowered """
            "population/polarity/continuity checks, then separately "
            "authorized current-limited startup/fault, thermal, "
            "USB/Ethernet, lighting/PWM/camera and PPS/NMEA epoch "
            "tests. Exact OS1 Rev08 MIO electrical limits remain an "
            "OEM integration condition; do not borrow Rev7 levels. No "
            "physical test or flashing is established by this package. "
            "Current native source, library and exported-file hashes "
            f"""are in SOURCE_IDENTITY.json and MANIFEST_SHA256.json.

Sources: [stack """
            "calculator](https://jlcpcb.com/pcb-impedance-calculator), "
            "[PCB "
            "capability](https://jlcpcb.com/capabilities/pcb-"
            "capabilities), [via "
            "covering](https://jlcpcb.com/help/article/pcb-via-"
            "covering), [assembly "
            "capability](https://jlcpcb.com/capabilities/pcb-assembly-"
            "capabilities), "
            "[Teensy](https://www.pjrc.com/store/teensy41_pins.html), "
            f"""[RTC](https://www.adafruit.com/product/3013).
"""
        ),
    )
    write(
        "12_ORDER_CONFIGURATION.md",
        (
            f"""
# Factory configuration for this source

Engineering review/quote only; no order or paid work """
            "approved. Upload only 01_PCB_GERBERS_DRILLS.zip, "
            "02_BOM_JLCPCB.csv and 03_CPL_JLCPCB.csv from this "
            f"manifest: {FN} factory-assembled references/{FG} factory "
            f"BOM groups. The {N} required placements/{G} BOM rows in "
            f"{identity['files']['bom']} / {identity['files']['cpl']} "
            f"are the complete system record: {B} board parts plus {U} "
            "required owner-stock modules. Design/ASSEMBLY_POLICY.json "
            "and evidence/ASSEMBLY_SCOPE.json bind the partition; the "
            "factory pair excludes only required U3/U9 plug-in "
            f"""installation, never DNP.

Required process:100×100mm finished carrier, {L} copper """
            f"layers, nominal order selection {order_mm:g}mm FR4, ENIG, "
            f"green mask/white silk; {stack_name} with {copper_weight} "
            "and USB90Ω/Ethernet100Ω targets. Published finished "
            f"thickness is {finished_mm:g}mm ±{finished_tolerance:g}%. "
            "Actual stack, current capacity and "
            "impedance/coupon/process acceptance remain factory tasks. "
            "File05 records the observed calculator geometry/results "
            f"""and their limits.

Minimum native via drill """
            f"{mm(identity['minimum_via_drill_mm'])}. Selective epoxy "
            f"fill/copper cap at the {F} CSV-listed vias; largest "
            "scheduled drill "
            f"{mm(identity['maximum_filled_via_drill_mm'])}. Keep all "
            "component/connector insertion holes open. Use the "
            "explicit drill/slot schedule and corrected GT-USB-7051A "
            "round/oval locating holes. Select production-file "
            f"""confirmation.

Standard PCBA, both sides, SMT+THT. Current native """
            f"placement classes: {categories}. Factory panelization "
            "with external tooling rails/fiducials, protected "
            "underside during manual/THT work, hidden-joint inspection "
            "and depanelization are required quoted operations. No "
            "fabricated centroid offsets, rotation blanket corrections "
            f"or omitted duplicate-centre records; {len(coincident)} "
            "actual opposite-side coincidences are separately listed. "
            f"Datum ({datum}) mm, common top-view X right / Y up on "
            f"""both sides.

U3/U9 are owner stock, not purchases and not DNP. """
            "07_OWNER_FINISHING.csv and 05 define required owner "
            "plug-in insertion, orientation/engagement checks and "
            "short finished USB cable. JLC fits J16/J17/J18 sockets; "
            "owned modules are plugged in afterward. Both owned "
            "modules already have male headers, confirmed by the owner "
            "on 19 October 2026. Quantity allocation, existing header "
            "direction, orientation/engagement and final source-bound "
            "USB isolation remain pre-power checks. No JLC header "
            "preparation, owner soldering/crimping/termination or "
            f"""unapproved bridge cutting.

This revised source supersedes previous manufacturing """
            "source hashes. Earlier JLC import counts, selected rows, "
            "quote IDs and unpaid sourcing carts are historical "
            "evidence. **No fresh JLC "
            "import/availability/preview/quote acceptance is asserted "
            "by regeneration.** Rematch the complete current BOM and "
            "review every remaining stock/global-sourcing requirement. "
            "Do not check out or release paid work without owner "
            f"""approval.

Use 08_RELEASE_GATES.csv for open acceptance conditions """
            "and 05 for full fabrication, assembly, impedance and "
            "inspection instructions. Source-bound IPC has "
            f"{IPC['total_test_records']} test records; native drill "
            f"schedule has {D['counts']['holes']} holes. Factory "
            "netlist comparison and test reports remain required; "
            f"""source export consistency is not a physical test.
"""
        ),
    )
    gates = [
        (
            "G1",
            "Factory sourcing",
            (
                "Rematch every current exact MPN; accept "
                "quantities/attrition/global or consigned supply. Never "
                "auto-DNP."
            ),
            (
                f"{B} required board references plus {U} owner-stock "
                f"modules;{N}placements / {G} BOM groups. Previous JLC "
                f"import is historical; current availability/preview not "
                f"accepted."
            ),
        ),
        (
            "G2",
            "Fabrication/assembly",
            (
                f"Accept specified {L}-layer stack, small drill, selective "
                f"fill/cap, two-sided SMT+THT, panel/fixture/depanelization "
                f"and actual CPL preview."
            ),
            (
                f"{F} filled/capped vias; {D['counts']['holes']} native "
                f"holes/slots; {len(coincident)} opposite-side centre "
                f"coincidences. File consistency checked; factory process "
                f"acceptance open."
            ),
        ),
        (
            "G3",
            "Owner-stock modules",
            (
                "Retain owned already-headered Teensy/RTC for owner "
                "plug-in installation and finished USB cable. JLC fits the "
                "sockets; no header preparation, bridge cutting or "
                "duplicate module purchase."
            ),
            (
                "Both modules remain required in COMPLETE system BOM/CPL; "
                "excluded only from JLC carrier pair by ASSEMBLY_POLICY. "
                "Male headers confirmed fitted by owner; final "
                "source-bound USB isolation and owner plug-in "
                "orientation/fit remain pre-power checks; see "
                "07_OWNER_FINISHING.csv."
            ),
        ),
        (
            "G4",
            "Mechanical fit",
            (
                "Verify exact connector models/land patterns, locating "
                "slots, plug/latch access, module/socket engagement and "
                "enclosure clearances."
            ),
            (
                "Current native drill dimensions checked; previous model "
                "recovery is source evidence only. Current full "
                "mechanical/visual/physical-fit acceptance separate."
            ),
        ),
        (
            "G5",
            "Finished harnesses",
            (
                "Deliver pinout-controlled finished camera, IMU, LiDAR, "
                "power and waterproof exterior coupler assemblies; no "
                "owner termination."
            ),
            (
                "09/10 schedules regenerated from native connector nets; "
                "physical lengths, mating assemblies and supplier "
                "acceptance remain controlled design."
            ),
        ),
        (
            "G6",
            "Powered prototype",
            (
                "Unpowered QC, limited-power startup/fault/thermal tests, "
                "USB/Ethernet, lighting/PWM and timing epoch checks."
            ),
            "No powered testing, flashing or deployment performed by export.",
        ),
        (
            "G7",
            "OS1 interface",
            (
                "Obtain exact Rev08 MIO electrical "
                "thresholds/current/absolute limits and confirm final "
                "PPS/NMEA configuration/epoch."
            ),
            (
                "Rev08 datasheet confirms power/pins; MIO electrical "
                "limits not published. Rev7 limits not substituted."
            ),
        ),
        (
            "G8",
            "Current design verification",
            (
                "Review current source-bound electrical, SI, thermal, "
                "package, CAM and harness findings before production."
            ),
            (
                "Native ERC/DRC/export consistency recorded; remaining "
                "configured ERC exclusions disclosed. Historical audit "
                "conclusions cannot be automatically promoted."
            ),
        ),
    ]
    writecsv(
        M / "08_RELEASE_GATES.csv",
        [
            dict(
                zip(
                    ["Gate", "Scope", "Required confirmation", "Current state"],
                    r,
                    strict=False,
                )
            )
            for r in gates
        ],
        ["Gate", "Scope", "Required confirmation", "Current state"],
    )
    write(
        "README.md",
        (
            f"""
# Current manufacturing review set

For the JLC carrier assembly quote, upload """
            "01_PCB_GERBERS_DRILLS.zip, 02_BOM_JLCPCB.csv and "
            f"03_CPL_JLCPCB.csv: {FN} factory references / {FG} "
            f"consolidated factory BOM rows. {identity['files']['bom']} "
            f"and {identity['files']['cpl']} retain the complete "
            f"{N}-part required system / {G} original system BOM rows. "
            "Required U3/U9 owner plug-in installation is explicitly "
            "listed in 07_OWNER_FINISHING.csv and derived from "
            "ASSEMBLY_POLICY.json; no native DNP or requirement "
            "deletion. Read 05_BUILD_AND_ASSEMBLY.md, "
            "08_RELEASE_GATES.csv and 12_ORDER_CONFIGURATION.md before "
            "factory review. U3/U9 remain required owner-stock "
            f"""modules; do not buy duplicates or omit them.

SOURCE_IDENTITY.json identifies actual CAD/dependency """
            "hashes. MANIFEST_SHA256.json identifies every generated "
            "manufacturing file. Current evidence is source-bound in "
            "evidence/CURRENT_EVIDENCE_BINDING.json; historical "
            "audit/cart/import evidence is not a current "
            "qualification. No paid work, order, reservation, "
            "production release or physical test is established by "
            f"""this folder.

The final reviewed gallery belongs in views/index.html """
            "and must match these source hashes. Include the "
            "single-page schematic, all enabled copper layers, both "
            "silkscreens, and "
            f"""top/bottom/top-isometric/bottom-isometric 3D views.
"""
        ),
    )
    (OUT / "PROJECT_README.md").write_text(
        (
            f"""# GRANDE Jessie MCU carrier v4.1

Open v4.1.kicad_pro in KiCad 10. This integrated source """
            f"contains {B} required board parts plus {U} owner-stock "
            "modules. Native CAD, libraries and manufacturing exports "
            f"""must share the hashes in jlcpcb/SOURCE_IDENTITY.json.

- [Schematic, PCB and 3D """
            f"""gallery](deliverables/review/index.html)
- [Current manufacturing """
            f"""instructions](jlcpcb/05_BUILD_AND_ASSEMBLY.md)
- [Release conditions](jlcpcb/08_RELEASE_GATES.csv)
- [Current order """
            f"""configuration](jlcpcb/12_ORDER_CONFIGURATION.md)
- [Sourcing register](docs/PROCUREMENT.html)
- [Unpaid carts and quote """
            f"""status](docs/PROCUREMENT_CART_READINESS.md)
- [Connector/harness contact """
            f"""map](jlcpcb/09_CONNECTOR_PINOUTS.csv)
- Run python scripts/run.py --help from the project to """
            f"""see export commands.

Engineering review/quote only. Previous JLC import/cart """
            "evidence predates these design changes until explicitly "
            "refreshed. No paid order or production release is "
            "implied. U3/U9 are owner stock; do not buy duplicates. "
            "See source-bound current evidence and separately recorded "
            f"""factory/physical acceptance conditions.
"""
        ),
        encoding="utf8",
    )
    writejson(
        M / "evidence/FACTORY_DOCUMENT_GENERATION.json",
        {
            "source_sha256": identity["source_sha256"],
            "source_identity_sha256": sha(M / "SOURCE_IDENTITY.json"),
            "counts_derived_from_native": True,
            "documents": [
                "05_BUILD_AND_ASSEMBLY.md",
                "08_RELEASE_GATES.csv",
                "12_ORDER_CONFIGURATION.md",
                "README.md",
            ],
            "prior_import_cart_status": "HISTORICAL UNTIL CURRENT SOURCE REMATCH",
            "factory_order_released": False,
            "owner_stock_module_refs": [r["Designator"] for r in mods],
        },
    )
