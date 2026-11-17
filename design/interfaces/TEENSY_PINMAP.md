# Teensy interface handoff / v4.1

[The logical-pin schedule](../../design/TEENSY_PINMAP_CURRENT.csv) and [system harness contract](../../design/SYSTEM_HARNESS.csv) define carrier cavities and nets. Existing timing, motor, camera, jog, limit, RTC and lighting assignments are retained. D13/J16.14 is unused; D30 is an output.

The owner confirmed that both owned modules already have male headers compatible with their female carrier sockets. JLCPCB assembles J16/J17/J18; U3 Teensy and U9 RTC remain required owner plug-in installation. No duplicate module purchase, header-preparation quote or owner soldering is required. Check orientation, engagement, clearance and unpowered continuity before energizing the assembled system.

## USB data and power

Normal host communication uses the upward J28 USB-C socket, the carrier protection/data gate, and the **factory-fitted CA1 shielded Micro-B pigtail** directly into the Teensy's fixed Micro-B device socket. There is no USB-A adapter or board-side interior USB mating connector. CA1 is an installation-controlled lead with strain relief, not an owner-serviceable harness.

**CA1.1 is permanently isolated from host VBUS.** This retains the safe power separation: J28/U24 stay on the host side of the VBUS-qualified U28 data switch, while CA1 carries only the protected D+/D− pair, ground, and drain/shield to the Teensy. SYS5_CORE powers Teensy VIN through J16.1 and retains the stock VIN/VUSB bridge. Native checking must verify the switch pin/net map, CA1.1 NC, and J16.1 SYS5_CORE.

The Teensy needs carrier core power, from the qualified external 5 V source or qualified power-only J4 path. A host cable at J28 alone does not power VIN. Use J28 for normal host service. Before connecting the Teensy Micro-B directly to another computer, isolate external VIN so the retained stock bridge cannot join two supplies.

These are source and OEM-design checks. Verify both power-up orders, host power loss/restore, both USB-C orientations, reset/bootloader, enumeration and programming before use. Dynamic isolation, timing and USB signal quality remain separately authorized bench tests.

## Timing and lighting assignments

D20/Serial5 TX at J16.7 feeds JP1/JP2, then U19, for LiDAR 3/4 NMEA. Original D1/inverted Serial1 continues to LiDAR 1/2. Configure family-specific polarity, baud, sentence and PPS epoch before enabling; do not broadcast the Velodyne RTC+1 convention to another sensor family without qualification. D40/J16.17 reads PWR_WINDOW_FAULT_N from the three rail disconnects. These are hardware provisions; deployed firmware or flashing is not established by the source files.

TOP D2 and FRONT D3 PWM share FlexPWM4.2 frequency. D6/D7 are independent enables; D24/D25 are ambient ADC inputs; D26/D39 are current ADC inputs. Default-off, fault latching, current calibration and camera banding tests remain pending. Coordinate firmware implementation with the owner of the active BOSUN tree.
