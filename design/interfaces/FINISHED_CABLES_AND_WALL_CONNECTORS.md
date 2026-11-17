# Finished cables and waterproof wall connectors — v4.1

**Current direction (2 November 2026):** Prefer a consistent waterproof connector family across the tray wall. The owner may choose the IMU cable-end connector or an adapter; older intact-end language is superseded. Earlier camera cable options below remain historical candidates while the new wall-family layout is evaluated. See [wall interfaces](WALL_INTERFACES.md) for current candidates and qualification limits.

**Design status:** IMU USB and timing are restored as work in progress. The USB-capable IMU variant, NUC supply voltage under load, 0.5 A fuse behavior, cable, ESD placement, USB link and connector manufacture remain unverified; the board is not approved for use or fabrication.

Source, price and cart observations below come from the earlier 16 October 2026 sourcing pass and were not rechecked during final electrical packaging. Amazon first, then distributors/OEMs. The owner only plugs finished connections together. All crimping, soldering, device-end termination and module preparation belong to the factory. Prices below are observed offers before tax/freight, not quotes or reserved stock.

## Compactness requirement — full installed depth

Owner steering: the proposed connectors are too long. Prefer compact panel couplers and, where the actual mating and cable geometry permit, finished right-angle interior leads. Preserve every factory camera RJ45 end and the no-owner-crimping/soldering requirement. Compare exterior projection, interior projection, mating overlap, plug/boot clearance and minimum cable bend radius separately. A bare socket length or mounting-hole diameter is not an installed envelope. A recess can move projection inside the enclosure but does not eliminate the required volume or bend clearance.

| System | Verified separate-part dimensions | Decision |

|---|---|---|

| [Simply45 S45-WP3201 drawing](https://simplybrands.com/wp-content/uploads/S45-WP3201-TD.pdf) | Drawing labels the separate sealing sleeve **104 mm**; this is not total mated depth. | Four remain staged in FireFold, **mechanical hold / bulky fallback**. Confirm drawing and installed envelope before purchase. |

| [Amphenol LTW / Selectronix SEL58-7P3232](https://selectronix.co.uk/products/ip67-f-f-rj45-coupler-panel-mated-waterproof) plus [SEL58-7A3258](https://selectronix.co.uk/products/ip67-rj45-cbl-housing-13-16-feed-through) | Socket **41.0 mm** overall; separate matching sleeve **56.85 mm Ref**, **25.4 mm** diameter. Split seal for **5.0-6.5 mm** cable. | Promising compact F/F candidate, not a released replacement. Forge nominal6.5mm lies at upper limit. Check actual OD/tolerance, moulded plug clearance, installed data/PoE rating and combined mated geometry. |

| [Switchcraft END series](https://www.switchcraft.com/assets/1/27/end_sealed_rj45.pdf?6927=) | Published cable range **4.6-6.0 mm**. | Excluded for the documented6.5mm camera cable. Smaller appearance does not establish compatibility. |

| [Bulgin PX0842/A USB](https://www.bulgin.com/pdf/pdf.php?sku=PX0842/A) | Coupling-ring diameter **38.1 mm**; matched USB system already identified. | Electrically matched but mechanical compactness still open. No claimed installed length and no automatic substitution with an unsealed ordinary USB adapter. |

The two RJ45 sleeve dimensions compare separate housings only; do not infer a percentage reduction in installed depth. Socket and sleeve overlap when mated; the ordinary inside plug and cable bend add further space. The public drawings do not dimension that entire assembly together.

Space-saving alternative only, **not adopted**: an [icotek KEL-ER split cable-entry frame](https://www.icotek.com/Produkte/PDFs/en_US/KEL-ER.pdf) is17mm high and can seal intact preterminated cables toIP66 using the specified grommets. This removes bulky connector shells at the wall but changes the requested F/F wall unplugging arrangement: disconnection moves inside and cable removal requires opening the split frame. It is not an IP67/68 immersion substitute. Keep the F/F requirement unless the owner chooses this tradeoff.

No cart items were changed in this compactness review. No connector or enclosure cutout is mechanically released from separate-part dimensions alone.

## Staged in Amazon cart — nothing purchased

| Exact item | Qty | Observed unit price | Use |

|---|---:|---:|---|

| [StarTech USB2HAB1 / B000E5BKQE](https://www.amazon.com/dp/B000E5BKQE) | 2 | $6.86 | NUC USB-A to electrical-box inside USB-B; 0.3 m |

| [StarTech USB2AC50CM / B06XSJ7KK5](https://www.amazon.com/dp/B06XSJ7KK5) | 2 | $11.95 | Tray inside USB-A to board J28/J30 USB-C; 0.5 m |

| Factory-fitted CA1 USB2 Micro-B pigtail | 1 | not selected for retail purchase | Direct protected J28-to-Teensy data lead; factory strain relief, VBUS-open check, and fit qualification required |

Final cart check showed those three SKUs at quantities 2/2/1, subtotal **$42.57**. Existing unrelated cart items were preserved. Returned personal orders remain possible lab stock because lab procurement may have replaced them; do not infer absence from return status. No checkout occurred. All five are normal internal cables; their lengths remain subject to physical fit. The IMU passive route still needs the factory length/signal check below.

J30 is the carrier's computer-facing USB-C socket for the IMU path, not the connector on the IMU. The IMU's native nine-pin Fischer cable goes to J9. USB D+/D- then pass through the carrier to J30 and an ordinary A-to-C data lead toward the NUC USB-A host. USB-C does not increase the IMU's data speed. The separate Teensy path is J28 -> protected switch -> factory-fitted CA1 Micro-B pigtail; it has no USB-A adapter.

## USB wall connections — complete stock connector/cable match

For each independent channel (Teensy and IMU):

`NUC A -> USB2HAB1 -> box PX0842/A [inside B / outside A] -> PX0841/AB/2M00 -> tray PX0842/B [outside B / inside A] -> USB2AC50CM -> J28 or J30`.

| Exact part | Base qty | Source | Observed unit price |

|---|---:|---|---:|

| Bulgin PX0842/A | 2 | [Mouser](https://www.mouser.com/en/ProductDetail/Bulgin/PX0842-A) | $26.96 each; staged |

| Bulgin PX0842/B | 2 | [Newark](https://www.newark.com/bulgin-limited/px0842-b/usb-adaptor-2-0-type-b-type-a/dp/97K8110) | $32.76 each; staged |

| Bulgin PX0841/AB/2M00 | 2 | [Newark](https://www.newark.com/bulgin-limited/px0841-ab-2m00/lead-usb-a-b-type-2m-ip68/dp/97K8107) | $48.94 each; staged |

| Bulgin PX0733 cap | 4 | [Newark](https://www.newark.com/bulgin-limited/px0733/sealing-cap-for-px0847-connector/dp/48F2467) | $4.20 each; staged |

The [manufacturer USB system](https://www.bulgin.com/products/pub/media/import/attachments/Standard_USB.pdf) specifies both adapter orientations and double-sealed A/B cables. These are female/female wall adapters with matched outside boots. A gasket and dust cap on an ordinary Amazon USB extension do not prove a connected seal. Install the supplied panel seals and observe drawing limits. Do not substitute an A/A cable for the A/B channel.

## Camera Ethernet — retain the existing RJ45 cable

**User steering: keep each camera cable intact, with its existing RJ45 end going straight to the electrical box.** No cutting, connector replacement or MCU-board detour. The current replacement identity, only if needed, is [Teledyne ACC-01-2102](https://www.teledynevisionsolutions.com/es-es/products/gige-ip-rated-m12-cables/?model=ACC-01-2102), 3 m M12 X8 male-to-RJ45, 6.5 mm cable OD. Four cameras need four electrical-box Ethernet wall locations.

**Documented intact-plug wall candidate — bulky fallback:** [Simply45 S45-WP3201](https://simplybrands.com/product/s45-wp3201/), [FireFold exact $15.95 offer](https://www.firefold.com/products/simply45-10g-rated-waterproof-rj45-panel-mount-coupler-for-stp-utp-cables). F/F shielded panel coupler accepts preterminated patch cords,5.5–8.5mm cableOD,IP68,up to90W standardPoE. The documented Forge6.5mm jacket is in range. Actual moulded plug/boot clearance and panel sealing still need fit checks. [Technical drawing](https://simplybrands.com/wp-content/uploads/S45-WP3201-TD.pdf). Mechanical hold for the owner's compactness requirement: drawing labels a separate104mm sleeve; installed depth unverified. An earlier review recorded four in a FireFold guest cart for **$63.80 before tax/freight**; current cart and purchase status have not been rechecked. Reuse equivalent lab stock first. No exact Amazon offer for this SKU was established. The below Amazon items are conditional cheaper alternatives. No unlimited-depth claim is adopted from marketing.

The cheaper Amazon lead is [Lonlonty B0BJ6NXWYN, exact 2-pack](https://www.amazon.com/dp/B0BJ6NXWYN), $11.99, in stock when checked. It is a female/female wall coupler intended to accept an already-terminated RJ45 plug without crimping or soldering. Its viewed detail did **not** establish cable-OD range or maximum plug body dimensions, so the complete mated seal remains unqualified. Do not order the full wall quantity merely from the IP68 title.

Your purchase markers also show [ELifeApply B0CBLWH4X6](https://www.amazon.com/dp/B0CBLWH4X6), [B0DD7FSZ3J](https://www.amazon.com/dp/B0DD7FSZ3J) and [B08CDD2BRF](https://www.amazon.com/dp/B08CDD2BRF). Reuse these first if their actual inserts fit. B0CBLWH4X6 lists7.5–17.5mm and B08CDD2BRF lists7.5–14.5mm cable diameter, both above documented Forge6.5mm. A correct smaller insert or supplier drawing is needed; tape or a loose gland is not accepted as a finished solution.

The alternative M12/M12 cable and M12-to-RJ45 wall adapter discovered during research are **excluded** following your instruction. They are retained only as rejected research in N07/N08 and have not been added to the cart.

## Ordinary VLP Ethernet data — fully matched premium option

`J23 (or J25) -> ordinary internal patch -> tray PX0833 -> PX0838/2M00 -> box PX0833 -> ordinary patch -> switch`.

- [Bulgin PX0833](https://www.digikey.com/en/products/detail/bulgin/PX0833/1625774): two F/F wall sockets per VLP data channel; $98.39 each, public page showed 1,398. [Primary rating/drawing](https://www.bulgin.com/pdf/pdf.php?sku=PX0833).

- [Bulgin PX0838/2M00](https://www.digikey.com/en/products/detail/bulgin/PX0838-2M00/1625771): one 2 m cable with both sealed ends per channel; $97.70, public page showed 41. [Primary cable specification](https://www.bulgin.com/pdf/pdf.php?sku=PX0838%2F2M00).

- [StarTech C6ASPAT1BK / B015FTW1ES](https://www.amazon.com/dp/B015FTW1ES): $9.95, 1 ft shielded CAT6A internal patch if lab leads are insufficient. Seven internal leads in the base topology, ten in the expansion, plus the actual exterior links. Final lengths must be measured; avoid buying a large arbitrary pack.

This Bulgin option is expensive and bulky but has a specified mating system. It does not fit the existing camera RJ45 as a sealed exterior connection without changing that cable end, so it is not selected for cameras. The revised powered VLP branch now includes a dedicated current limiter (1.277 A nominal; stated static upper corner 1.410 A). This alone does not qualify a wall coupler or startup transients. Keep the powered VLP path as a separately accepted, correctly pin-mapped factory assembly.

## Ouster — provision the interface without guessing the supplied cable

The [Type 3 M12AP/M12XR Y-cable drawing](https://data.ouster.io/downloads/datasheets/datasheet-Ycable-M12AP-M12XR.pdf) exposes Ethernet separately from power/timing. Options include 1 m / 5 m and straight / 90-degree sensor ends; the PDF has no exact orderable cable SKU. Preserve the supplied cable and choose its wall mate after the delivered configuration is known.

[Plinkwirekb B0F93ZNG1L](https://www.amazon.com/dp/B0F93ZNG1L) is an exact Amazon **2PCS** M12 A5 female/female shielded panel-coupler offer, $35.99, in stock when checked. The listing specifies 60 V, 4 A, M16x1.5 nut and 16.5 mm cutout, with an IP67 claim. One pack covers one OS1 plus a spare, or two OS1s. It still needs its contact mapping/mated-seal drawing accepted; it is not a 5 A lamp connector. [Premier FT-A5-FF](https://www.premier-cable.store/products/m12-a-code-5-pin-panel-mountable-coupler) is an $18 ex-VAT alternative with a different 16.1 mm cutout and no published current rating. Do not mix the two mounting drawings.

Factory internal adapter: M12 A5 male -> Micro-Fit 43645-0400, map A5.1 -> AUX.1 (12 V), A5.3 -> AUX.2 (GND), A5.2 -> AUX.3 (PPS), A5.4 -> AUX.4 (NMEA), A5.5 -> common GND with A5.3 according to the OEM Rev05/25/2026 page3 drawing. The fifth contact is duplicate ground, not shield; shield continuity runs through the sensor/Ethernet shells. Page1 says four pins while page3 depicts five positions: confirm the delivered cable SKU/drawing before fabrication. No extra OS1 cable/coupler was added to the cart.

Gmail check on 16 October: the latest message in the inspected Amber/Cameron thread is your 13 October cable question; Amber's last reply is 9 October. Draft quote Q-18501-1 dated 7 October names **OS1-080-128U-A-C-002-BA-000-000**, but does not list a cable or interface box. The separate support case auto-closed on 14 October. [Cable question](https://mail.google.com/mail/u/?authuser=aaronjs%40g.ucla.edu#all/1a1374a4254fcd0c), [closure notice](https://mail.google.com/mail/u/?authuser=aaronjs%40g.ucla.edu#all/1a1395e448570495). No email was sent.

## Camera GPIO — all eight contacts, factory-finished ends

Four complete assemblies are required, independent of Ethernet: camera M8 A8 -> sealed tray wall -> Micro-Fit 43025-0800. All eight physical contacts reach the board even when unused. Use the exact camera pin numbers in the connector contract, not a guessed generic M8 colour convention.

- [Premier FT-A8-FF](https://www.premier-cable.store/products/m12-a-code-8-pin-panel-mountable-coupler): M12 A8 F/F shielded coupler, $24 with Add to cart on the manufacturer page checked17October2026. Electrical ratings, complete mating parts and sealing acceptance remain pending; this is a candidate, not a finished harness.

- [Amazon B0G44NQN96](https://www.amazon.com/dp/B0G44NQN96): exact F/F A8 panel coupler, $59.94 each, in stock / 4–5 day dispatch displayed. Missing adequate electrical/dimensional drawing; conditional alternative, not approved stock assembly.

- [Molex 2147551082](https://www.molex.com/en-us/products/part-detail/2147551082): genuine 300 mm, 18 AWG, dual-row eight-position female/female lead, all eight loaded. Optional factory feedstock only. It still lacks M8/wall mates; not a complete cable to buy and plug into a camera.

- [Teledyne ACC-M8-MA-4-X-02M](https://www.teledynevisionsolutions.com/en-in/products/m8-a-coded-8-pin-circular-connector/?model=ACC-M8-MA-4-X-02M&segment=iis&vertical=machine+vision): OEM cable identity, $60 catalogue; far-end/all-eight loading not established. Do not infer loading from the part-number substring.

The factory must provide finished exterior M8A8-to-M12A8 male/male leads and finished interior M12A8-male-to-43025-0800 leads if the A8 wall coupler is used. Those exact adapters were not found as a qualified retail kit; no owner crimp/solder task is substituted.

## IMU — CA-MP2 cable and Micro-Fit interface

**Owner reports receiving an Xsens CA-MP2-MTi.** The owner may select its tray-end termination or an adapter for J9's Micro-Fit 2x5 mate; no particular assembly is confirmed. Xsens discourages USB on the stock cable termination because its shield is not connected at the Molex end. An alternative termination and the complete USB link remain unqualified. [Xsens MTi manual](https://www.xsens.com/hubfs/Downloads/usermanual/MTi_usermanual.pdf).

The fixed carrier map remains: J9.1 VIN from NUC VBUS through J30/F9; J9.2 GND; J9.3 USB D+; J9.4 factory serial TX intentionally NC; J9.5 factory serial RX intentionally NC; J9.6 USB D−; J9.7 SyncIn; J9.8 ClockSync; J9.9 SyncOut; J9.10 physically blank/NC. The chosen termination or adapter drawing must preserve the approved map. J30 is the separate USB-C outlet to the NUC.

USB D+/D− is intended to be a protected pair, with timing on its own conditioned MCU path; U25 is currently unconnected and does not yet provide protection. Before manufacture/use, qualify the adapter continuity, installed bend/latch clearance, USB enumeration, sustained streaming, IMU voltage, timing polarity and reference-loss behavior. A wall bulkhead or coupler remains a mechanical/whole-link qualification item.

## Remaining power, lamps, sensors and controls

**Current schematic and PCB-placement state (25 October 2026):** J1 is the
reserved 2x7 main-power inlet; J3 is the 2x6 motion/encoder fan-out; J11 is
the 2x3 jog/limit fan-out; J9 is the vertical 2x5 IMU inlet; and CA1 is the
factory-fitted Teensy pigtail landing. Their matching PCB footprints and
front-silkscreen identifiers are placed. This is still not a harness-release
or purchase authorization: continuous routing, parity/DRC closure, connector
derating, enclosure fit, finished factory loom drawings, and physical
qualification remain required before any build.

| Path | Exact retained/selected basis | Finished work still needed |

|---|---|---|

| Board regulated inputs | Schematic and placed PCB J1: Molex 43045-1412 2x7 header with 43025-1400 factory-finished mating loom | J1.1/.8=5 V/GND; .2/.9 and .3/.10=LIGHTS_12_RAW/GND; .4/.11=blocked NC; .5/.12, .6/.13, .7/.14=LIDAR_12_RAW/GND. Never feed battery 29–33.6 V into these rails. The lights and LiDAR nets remain separate board-side protected branches; remaining placement/routing and qualification are still required. |

| Power wall | [Turck 100048191 AD-FK4PT-FS4PT](https://www.turck.com/de/en/shop/connectivity/accessories/100048191), 12 A/63 V T-coded alternative | Female outside / male inside, so not the requested F/F. Factory-finished mates required. No qualified F/F high-current finished kit found. Do not silently change the preference or overload one path with aggregate current. |

| TOP and FRONT lamps | Selected: two [WLBS-40K12-12V](https://www.superbrightleds.com/waterproof-led-light-bar-12vdc-338-lm-ft-dimmable-barrel-connector-and-on-off-switch-3000k-4000k-5000k), 4.5W each | Factory finished 5.5x2.1mm barrel mates, sealed power paths and J12 43645-0500. Preserve independent switched returns and coordinate source-end protection with actual lamp lead/contact ampacity. The5A/channel future envelope needs separate qualification. Nilight is unselected research; do not purchase it for this kit. |

| Ambient TOP/FRONT | Reuse TEMT6000 B0CZRH2DHJ, two of existing pack | Finished optical heads and three-wire sealed leads to 43645-0300: 1=3.3 V,2=analog,3=GND. Different mechanical interface from 12 V Ouster to avoid cross-plugging. Housing/wall adapter still a factory assembly. |

| Arm/control leads | Exact J3 2x6 and J11 2x3 Micro-Fit fan-outs in register | Factory BTS/encoder and jog/limit branched looms. Preserve installed device ends; exact motor encoder, jog and limit mate/level must come from hardware records. No loose screw-terminal kit offered as owner-ready. |

| NUC/sonar power | Reuse 19 V/10 A and 24 V/6 A converters and existing device leads | Factory ends matched to actual NUC/sonar. Exact installed model/mate not established by current evidence; no guessed barrel size or sonar plug ordered. |

| RTC | [Panasonic CR1220 one cell](https://www.amazon.com/dp/B00DKYJEG4), $5.07 | Buy only if missing; no subscription. Prepared Teensy/RTC headers and insertion are factory work. |

| Enclosure | [Allbro ALL-003](https://www.allbro.com/download-catalogue/enclosures/ALL-003-G-Technical-Datasheet.pdf), nominal 350×250×200 mm | US delivered offer/clearance not established. Nominal 350 x 250 x 180 mm Boxco/Bud hinged options are rejected because their complete hardware envelopes exceed the hard 350 x 250 mm limit; they are not substitutes. Allbro also requires OEM-approved mounting within that complete envelope. |

The lab's 12 V/20 A converter is 240 W by nameplate. The prior four-LiDAR + 120 W lighting + fan planning allocation is 209 W / 17.42 A; adding four 12 V/3 A TECs exceeds it. This is a planning envelope, not a hot-enclosure bench result. Do not parallel converter outputs or assign a 4 A signal coupler to a 5 A lamp channel.

## What this delivery closes, and what it does not

Exact internal USB retail SKUs and quantities are staged; USB wall sockets/cables and the premium VLP DATA path have matched part numbers. Existing camera RJ45 cables are preserved. The register now records real F/F M12 candidates, stock contradictions, lab-reuse leads, and rejected replacement routes.

Factory adapters for camera GPIO, full IMU/shield path, ambient heads, power/lamps and installed controls are still assemblies to quote/build, not retail products already obtained. The [finished assembly schedule](HARNESS_BUILD_SCHEDULE.csv) records their ends, exact board pin maps, provisional lengths and unresolved items. Its H-* IDs are project drawing IDs, not invented supplier MPNs. JLC has received the PCB quotation package and a request about finished-harness support. No dedicated cable assembler has yet accepted the finished assembly schedule. Codex made no fabrication release,purchase,email send,physical connection or powered test. The verified Mouser purchase was made separately by the owner.

## Earlier USB sourcing observations

Earlier cart quantities and conditions have not been rechecked. The owner may select the CA-MP2 tray-end termination or an adapter. Measure the installed reach and qualify the complete USB/timing link, including the Xsens stock-cable shielding limitation.

## Historical handoff update — October 2026

An earlier record says JLC received a PCB review package and a finished-harness inquiry; the current status of that inquiry and the remaining harnesses has not been rechecked. The owner reports receiving one CA-MP2-MTi; additional quantity is zero. The owner may select the tray-end termination or an adapter, neither of which is confirmed complete. Its wall passage, cable shield and full USB/timing link still require physical qualification. The earlier USB cart status has not been rechecked. The previously referenced harness-request file is absent from this project; see the retained [OEM interface evidence](OEM_INTERFACE_EVIDENCE.md).

Premier FT-A8-FF now shows $24 and Add to cart on its manufacturer page. This supersedes the earlier availability observation only; it does not establish its loading, sealing, envelope or complete harness acceptance. The selected WLBS lead/barrel requires supplier-coordinated source-end protection based on its ampacity and inrush. Do not assume the carrier 8A fuse protects that small integral lead.
