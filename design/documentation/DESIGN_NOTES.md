# Current design notes

The schematic stays on one page with centralized component banks and the owner's spacing. Top-side connectors/modules/LEDs/test points remain accessible. Necessary underside changes add dedicated VLP current limiting, actual timing-rail monitoring and lamp gate protection. Local routing and package placement changed deliberately; they are documented in the current audit. No physical qualification is implied by the CAD checks.

Four identical LiDAR power/timing AUX channels remain available, with two VLP powered-RJ45/data-only-RJ45 breakout pairs sharing channels 1/2. Use either AUX or powered RJ45 for a given channel; do not attach two sensors to one fused channel. OS1 Ethernet bypasses this carrier and goes to the switch. Four Forge GPIO harnesses retain all eight contacts; unused contacts remain intentionally unused.

The carrier is intended to provide timing distribution and applicable sensor power and ESD protection. The saved board passes native continuity checks. These functions still require electrical testing. The battery-to-5/12/19/24 V converters remain separate. Battery voltage must not be connected directly to the 5/12 V carrier inlets. General harness ports use the current Micro-Fit family, with USB/RJ45/module sockets retaining their native formats. Finished harnesses and adapters must be supplied complete; current carrier connectors are internal, not waterproof bulkheads.

The owner confirmed on 2026-11-02 that the 5 V converter and 12 V supply are in the box outside the tray and feed the carrier from there. The upstream fuse or current-limit arrangement is not established by that confirmation.

The combined IMU cable connects to J9. Its USB pair passes to J30 and the electrical-box/NUC host; its timing signals remain on the carrier's buffered timing paths. The NUC USB 5 V is the intended IMU supply through J30 and F9, with no intentional connection to the carrier 5 V supply. The installed USB-capable IMU variant, voltage drop, complete cable and physical behavior remain unverified. Xsens discourages USB on the stock CA-MP2-MTi because its shield is not connected at the Molex end; a selected replacement termination is not yet qualified. See MTi30_USB_SYNC_CABLE_RECOMMENDATION.md.

J4, J28 and J30 use the G-Switch GT-USB-7051A (JLCPCB C2843970). The secondary locator now uses a centered 1.00 mm round non-plated drill. Its opening contains the complete nominal 0.65 × 1.00 mm slot. The 0.65 mm round primary locator sets the position, and the four 0.70 mm plated shell drills per connector retain the manufacturer pattern. The shell lands are 1.18 mm on both 1 oz outer layers and 1.22 mm on all four 2 oz inner layers. Their nominal annular rings are 0.240 mm outer and 0.260 mm inner. The native drill and Gerber exports confirmed the three round relief holes and all twelve shell padstacks. Physical fit of the revised locator relief still needs a first article. [G-Switch drawing](https://datasheet.lcsc.com/datasheet/pdf/6f6e8af39754b37a34134efb833fd0f3.pdf?productCode=C2843970) · [JLCPCB fabrication limits](https://jlcpcb.com/capabilities/pcb-capabilities)

J30's four shield holes provide the mechanical attachment and ground connection. Ground-return continuity and transient behavior require qualification. The hidden legacy Footprint_Status text referring to USB4145 predates the GT-USB-7051A selection.

On 2026-11-02, a comparison on the same saved-board snapshot found that the installed KiCad 10.0.1 CLI reported the same DRC errors with the complete custom-rule file as with that file absent; KiCad 10.0.6 produced a different result with the saved rules. Use the tested 10.0.6 toolchain for this design and verify that custom rules are applied. The precise cause of the older CLI's behavior has not been established. A [KiCad issue](https://gitlab.com/kicad/code/kicad/-/issues/24942) separately reports unrecognized track-coordinate properties in 10.0.4; this design uses coordinate-dependent rules. Neither a DRC count nor the custom rules establish electrical or manufacturing qualification.

KiCad Board Setup currently sets an absolute 0.20 mm minimum track width. The smaller 0.125 mm and 0.15 mm local values in the custom rules do not override it; a narrower package escape would require a separately justified, validated rule revision.

The `ETH_DATA` class currently requests a 0.18 mm differential-pair width, below that board-wide minimum. JLCPCB's [copper-weight guide](https://jlcpcb.com/help/article/jlcpcb-copper-weight) lists 0.16 mm for 2 oz FR-4, while its [trace-width article](https://jlcpcb.com/blog/pcb-trace-width-guide) lists 0.15 mm for four-layer 2 oz boards. Those general figures do not establish availability or impedance control for the selected special stack. The board minimum and pair geometry remain unchanged pending a validated, consistent choice.

The historical release_inputs predate the IMU change and are not a current manufacturing package.

## JLCPCB engineering review

The fabrication review uses six-layer FR-4, ENIG, green solder mask, 1 oz outer copper and 2 oz inner copper. The nominal quote selection is 1.6 mm. JLCPCB must confirm the physical stack and finished thickness. [JLCPCB rigid PCB capabilities](https://jlcpcb.com/capabilities/pcb-capabilities) · [JLCPCB impedance calculator guide](https://jlcpcb.com/help/article/user-guide-to-the-jlcpcb-impedance-calculator)

- The current Gerbers confirm 0.254 mm minimum plated-terminal annular ring on the 2 oz inner layers and 0.240 mm minimum on the 1 oz outer layers. The 101 Micro-Fit plated terminals have 0.260 mm nominal rings except J9 pin 1 on B.Cu, where the 0.240 mm ring preserves adjacent USB reference copper. The USB-C shield outer rings increased from 0.200 to 0.240 mm. Four ground vias with 0.30 mm drills now use 0.55 mm lands, giving a board-wide routing-via minimum of 0.125 mm. Two ground stitching vias and the local J30 VBUS jog moved slightly for clearance. Connector drill sizes and centres stayed fixed, and all 42 non-plated holes export without copper pad flashes. [JLCPCB fabrication limits](https://jlcpcb.com/capabilities/pcb-capabilities)
- Ten ESD and eFuse footprints now use 1:1 mask openings. The board checks 0.10 mm mask webs and 0.09 mm mask-to-copper clearance. Footprint silkscreen strokes now measure at least 0.15 mm. Label wording, position and component positions stayed fixed.
- The drill files contain 493 plated and 42 non-plated round drills. The via-treatment schedule calls out 28 epoxy-filled and copper-capped 0.30 mm drills, including TP4. Seven of the 13 larger power vias are open on both sides. Six retain tenting on one or both faces, so factory review must confirm coverage for those six. [JLCPCB via covering](https://jlcpcb.com/help/article/pcb-via-covering)
- The BOM and placement list match 328 catalog placements. The package specifies Standard two-sided SMT and through-hole assembly with factory process rails and fiducials. The required CA1 custom pigtail is a separately quoted factory operation. U3 and U9 are separately fitted plug-in modules. [JLCPCB assembly capabilities](https://jlcpcb.com/capabilities/pcb-assembly-capabilities)

Factory review also needs to accept 72 manufacturer-pattern lands that are 0.20 mm wide across D1, D4, D5, D19–D21, U5, U16 and U28. The return-path update adds ground stitching at Ethernet ESD taps and USB transitions and reroutes the U28 launch. Moving the TOP lamp return and IMU 5 V copper restored an adjacent ground path along the long USB runs at the lamp connector, with their widths and existing power vias preserved. The single-plane ground-coverage screen flags 59 of 168 segments, down from 82 of 162. The remaining sampled gaps are local to launches, transitions and Ethernet receive-side ESD branches. Confirm the accepted stack, the 90 Ω USB and 100 Ω Ethernet routing, physical USB-C and module fit, and both-side placement orientation before order release. [TI example land pattern](https://www.ti.com/lit/ds/symlink/tpd4e02b04.pdf)

The current Gerbers, drills, BOM, CPL and via instructions are in `deliverables/review/JLCPCB_engineering_review.zip`. The controlled release manifest remains on hold.

## Harness and USB instructions moved off the schematic

### Interface Conventions

- Use the carrier cavity map for each finished harness.
- J22 / J24: VLP 12 V + timing + data; NOT Ethernet / PoE.
- J23 / J25: data only. OS1 Ethernet bypasses the carrier.
- AUX / powered RJ share channels 1 / 2: one sensor each.
- JP1 / JP2: 1-2 AUX default; cut before 2-3 COMMON.
- Channels 3 / 4 share the AUX UART; PPS is buffered.
- Carrier cable entry: TOP; general harness is Micro-Fit 3.0.
- Factory supplies complete finished mating harnesses.

### Power / Usb / Timing Notes

- J4: core backup power only; Type-C 1.5 / 3 A advert.
- J28 -> protected data switch -> CA1 factory Micro-B pigtail -> Teensy device; core power needed.
- Retain the stock Teensy VIN/VUSB bridge. CA1.1 is VBUS-open; the host-VBUS-qualified data switch controls attachment. Verify the finished J28/CA1 path before use.
- J30 is the IMU USB-C pass-through to the electrical-box/NUC host; F9 is on the host's 5 V supply to J9. No intentional connection to the carrier 5 V rail is shown.
- J9 remains a keyed 2x5 Micro-Fit 3.0 header for the combined IMU cable. Its factory adapter, cable reach, shielding and connector fit remain unverified.
- J9 carries the IMU supply, USB pair, ground and timing signals; its serial and reserved contacts remain NC. The restored USB routing and electrical behavior are not qualified.
- The current PCB passes the CC1 connectivity check at J28.A5. USB-C operation in both plug orientations still requires testing.
- RTC PPS activity does not establish valid UTC / epoch.
- OS1 Rev08 MIO limits and epoch require OEM qualification.

