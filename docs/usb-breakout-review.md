# USB breakout review and bring-up

This review covers the catalog `usb` design: a USB 2.0 full-speed Type-B receptacle (J1) wired to a 4-pin header (J2) on four layers. The data pair runs on F.Cu over the In1.Cu GND plane. The figures below come from a board generated and routed by AutoPCB with KiCad 10.0.8. Re-measure a saved board with `verify` before reusing them.

## Launch return copper

The reference screen samples every 0.1 mm along each data trace and records where the adjacent plane has no copper. Trace that lies over its own pad is not counted, because it is part of the pad. The coupled run has no missing samples. One gap remains at each through-hole signal pin:

| Launch | Net | Pad and drill | Plane clearance | Exposed trace |
| --- | --- | --- | --- | --- |
| J1.2 | USB_N | 1.4 mm, 0.92 mm | 0.2 mm | 0.19 mm |
| J1.3 | USB_P | 1.4 mm, 0.92 mm | 0.2 mm | 0.30 mm |
| J2.2 | USB_N | 1.7 mm, 1.0 mm | 0.2 mm | 0.20 mm |
| J2.3 | USB_P | 1.7 mm, 1.0 mm | 0.2 mm | 0.20 mm |

Each gap is the ring the GND plane must keep clear around a signal pin. A signal crosses it in under 2 ps at about 167 mm/ns. Full-speed edges take at least 4 ns, about 670 mm of trace, so the discontinuity is far too short to matter. The return current reaches the connector's GND pin within about 5 mm.

Removing the unused inner-layer pad from these pins lets the plane reach under the pad edge and closes most of each gap, but KiCad then reports the footprints as differing from their library. The generated boards keep the library footprints.

Disposition for each launch finding in `checks.reference_copper.findings`:

> Accepted. The exposed trace is the 0.2 mm plane clearance around the connector's own signal pin. It lasts under 2 ps against full-speed edges of at least 4 ns, and the connector GND pin returns the current to the plane within about 5 mm.

## Stackup

The draft stackup puts 0.18 mm of FR-4 (εr 4.3) between F.Cu and In1.Cu. For 0.25 mm traces with a 0.26 mm gap, closed-form microstrip equations give about 56 Ω single-ended and 100 Ω differential without solder mask. USB specifies 90 Ω ±15 %, which is 76.5 to 103.5 Ω. Solder mask lowers the value by a few ohms. Confirm it with the fabricator's field solver for the ordered stackup. The traces are 16.34 mm and 14.39 mm long, a 1.95 mm (12 ps) difference within the 2 mm limit.

## Power path

VBUS runs 16.8 mm on B.Cu at 0.25 mm. With 35 µm copper that is about 33 mΩ, or 17 mV at 500 mA, and IPC-2221 predicts about a 3 °C rise. GND returns through the In1.Cu plane.

## Bench bring-up

Use a DMM, a USB host, an A-to-B cable, a known-good full-speed device wired to the header, and a 10 Ω 5 W resistor. An oscilloscope of at least 200 MHz is optional.

| Step | Method | Pass |
| --- | --- | --- |
| 1. Inspect | Check J1 pins and shield tabs and J2 for bridges, voids and orientation. | No defect |
| 2. Continuity | Unpowered, measure J1.1–J2.1, J1.2–J2.2, J1.3–J2.3, J1.4–J2.4 and shield–J2.4. | Each below 1 Ω |
| 3. Isolation | Unpowered, measure between every pair of VBUS, D−, D+ and GND. | Each above 1 MΩ |
| 4. Idle power | Plug J1 into the host through a USB power meter with nothing on J2. Measure J2.1 to J2.4. | 4.75–5.25 V, 0 mA on the meter |
| 5. Load | Connect the 10 Ω resistor across J2.1 and J2.4. Measure J1.1 to J2.1 and J2.4 to J1.4. | Sum at most 50 mV |
| 6. Enumeration | Wire the device to J2 (D− to J2.2, D+ to J2.3) and plug J1 in 20 times. | Enumerates as full speed every time (`lsusb -t` shows 12M) |
| 7. Traffic | Transfer data for 10 minutes with an end-to-end check, for example a serial loopback or copying a file and comparing SHA-256. | No errors or resets in the host log |
| 8. Edges, optional | Probe D+ and D− at J2 during traffic. | Monotonic edges, 4–20 ns, crossover 1.3–2.0 V |

Record the board serial, host, device, and each measured value.
