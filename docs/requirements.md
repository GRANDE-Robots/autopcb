# GRANDE Jessie v4.1 requirements

These are the user's design and delivery requirements. The completion plan and current evidence are separate files.

## Board and electrical scope

- Four copper layers maximum. Use the existing reviewed F.Cu / In1.Cu / In2.Cu / B.Cu stack. Do not restore the six-layer design. The current approved comparison selected a 100 × 110 mm board; 100 × 100 mm was too cramped. Another outline change requires a concrete fit/cost reason.
- Retain the approved two opposing powered/data RJ45 pairs. Their tails face each other and their cable mouths face outward. Four identical LiDAR AUX power/timing connectors remain. Camera Ethernet goes directly to the network switch.
- Group connectors by function: cameras together, LiDAR together, IMU together, motion together, power/lamps together. Favor uniform cable exits and usable plug/latch access. Preserve the currently approved Ethernet placement.
- LEDs, all J* connectors, Teensy, RTC, large capacitors and test points stay on top. Thin inaccessible circuitry may be on the bottom. Organize both sides by subsystem, using aligned repeated channels and short local loops.
- Retain independent camera triggers, both motor-driver current-sense channels, independent R_EN/L_EN control, protection and hardware rail-valid gating. Do not remove functionality just to lower the open count.
- No owner crimping, soldering, screw terminals or spring terminals. Use ready-made cables or supplier-finished harnesses, keyed connectors and tested cavity maps. The camera's existing RJ45 end stays intact.
- Owner already has headered U3 Teensy 4.1 and U9 Adafruit 3013 RTC. Factory supplies and fits the mating female sockets J16/J17/J18 and everything else on the carrier. Do not buy duplicate modules or quote header preparation/consignment by default.
- Retain the owned CA-MP2-MTi IMU cable. USB reaches the NUC, timing reaches the MCU, other contacts remain correctly protected/service-accessible. Serial levels must never be confused with USB D+/D− or connected directly to incompatible MCU pins.
- J29.1 is intentionally NC. Preserve the stock Teensy VIN/VUSB bridge architecture and the existing host-qualified USB attachment circuit. Do not reconnect VUSB through an additional uncontrolled supply path.
- Keep the accepted future power envelopes: LiDAR 12 A aggregate and 3 A per AUX fused allocation; lighting 10 A aggregate / 5 A per channel. The two purchased WLBS-40K12-12V bars actually draw about 0.38 A each; that does not authorize silently shrinking the future envelope.
- Powered sensor RJ45 carries nonstandard fused 12 V, timing and 100BASE-TX. It is not PoE. AUX and powered-RJ45 loads share each relevant 3 A allocation; jack contact and local eFuse limits remain respected.

## Routing and presentation

- Use sparse nominal 45° routes with few bends, joints and vias. No 90° bends, staircases or gratuitous length tuning. Normal T junctions inside a net are distinct from a degree-two sharp bend.
- Choose width/drill from actual current, available space and fabrication cost. Standard small signal vias are 0.60/0.30 mm. Larger screened power vias are allowed. No new 0.15 mm drills or unqualified via-in-pad.
- Use In1 only for a screened low-speed route when it is materially cleaner than the available signal layers. Keep uninterrupted reference copper beneath Ethernet and USB: no In1 route may enter the verified F-side high-speed keepout, and In2 routing must preserve the reference for B-side high-speed copper.
- Preserve accepted Ethernet/USB pad, track and via geometry unless a specific independently checked replacement is necessary. Do not use obsolete guessed high-speed corridors instead of actual copper/reference geometry.
- Keep decouplers, protection, current-sense and gate loops compact. Kelvin traces leave each shunt pad independently and never carry load current.
- Full-width power mains stay full-width. Short IC-pad necks, header throats, controller feeds and voltage-only test taps need explicit bounded roles and group-scoped rules; never weaken an entire power class to pass DRC.
- Silkscreen must be readable, aligned and close to the relevant part. Use functional connector labels and clear polarity/pin-1 markings. Avoid collisions, buried text and unnecessary prose. Bottom labels need the same care as top labels.
- Keep schematic structure visible, with compact uniform spacing and short consistent label stubs. Horizontal R/C references may be inside the symbol with the value nearby; vertical references and values sit beside their symbol. No long notes blocks on the schematic.
- Reference numbering by subsystem is desirable but optional; do not renumber stable references and break contracts merely for appearance.
- Show the schematic, all four copper layers, both silkscreens and four 3D views: top, bottom, top isometric and bottom isometric. That is 11 images on this four-layer design. The earlier 13-image count belonged to six layers.
- Preserve the ordinary gallery and a standalone HTML embedding the same images byte-for-byte. Use real connector models and correct model transformations.

## Files, checking and external actions

- Final edits belong in the actual canonical v4.1 project. All schematic/PCB/libraries/design/BOM/CPL/Gerber/views must describe the same frozen source.
- Preserve newer user edits. Compare hashes and check GUI locks before publication. Do not reset/clean the project, create a new repository or overwrite canonical files with an old whole branch.
- Keep a small recoverable checkpoint before each accepted phase. Existing local working evidence must survive until the final integrated release is verified. Remove only demonstrably superseded generated delivery files afterward.
- Native DRC, all unconnected items, schematic parity and ignored DRC checks must be zero before manufacturing exports. Configured ERC must be zero; disclose and review expanded advisory categories rather than hiding them.
- Distinguish CAD connectivity, calculations, mechanical fit, factory acceptance and physical testing. Do not call the board perfect or manufacturing-ready merely because a partial routing trial has no clearance errors.
- Fix KiCad save/assertion causes: explicit layer for via.GetWidth, guarded filled-polygon access, isolated native refill, verified ZIP members/CRC and exact PRO/DRU preservation. Do not suppress warnings as a fix.
- Complete an unpaid JLCPCB quote only after final files pass. Compare the least available bare/assembled quantities; published minimum was five bare / two assembled, and one assembled was not confirmed.
- No paid work, review/matching fee, procurement, purchases, consignment processing or production without Aaron's separate approval. Existing carts/RFQs are quotation alternatives, not duplicate purchase authorization.
- JLCPCB acknowledged quotation-only handling. Use owner plug-in modules with the carrier assembly BOM; do not hide required carrier parts as DNP. New supplier messages require the applicable explicit authorization and concrete reviewed wording/attachments.
- Keep notes and updates brief, human and factual. Use a low-token execution log and exact evidence instead of repeatedly rereading the conversation.
