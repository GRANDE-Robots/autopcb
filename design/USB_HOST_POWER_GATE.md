# Teensy USB host-power gate

This contract applies to U28/U29 and their power and signal nets after the reviewed schematic and PCB are integrated. It replaces any inference that CA1.1 being open alone establishes complete USB host-off isolation. Ground remains shared; this is a data disconnect, not galvanic isolation.

CA1.1 remains open and the owner's fitted Teensy VIN/VUSB bridge remains intact. SYS5_CORE supplies VIN through J16.1. U28, exact TI TS3USB221ARSER / C128396, separates host-side TEENSY_HOST_USB_DP/DM at J28/U24 from device-side TEENSY_USB_DP/DM at CA1/U3. U24 remains on the host side. U28's alternate port is NC, select is GND, and a 10 kÎ© resistor pulls /OE high to default to disconnect.

U29 reuses TPS3702AX33DDCR / C2068673. Its OV output enables U28 only when true host VBUS passes the divided threshold. R152 is RT0603BRD072KL / C728588, 2.00 kΩ; R153 is RT0603BRD0710KL / C95204, 10 kΩ. Both are 0.1%, 25 ppm/°C parts already used on the board. SET is GND, UV is NC, VDD is carrier 3V3. R154 reuses RC0603FR-0710KL / C98220, 10 kΩ. C52/C53 reuse CL10B104KA8NNNC / C1590 for the dedicated 100 nF, 25 V, X7R local bypasses.

The source-backed static screen gives 4.1532–4.2421 V rising enable and 4.1199–4.2294 V falling disable, including the stated threshold, tolerance, opposing temperature drift, leakage and hysteresis assumptions. Consequently a ≤3.6 V data-derived rail cannot sustain enable. This closes the feedback mechanism that could otherwise hold a low-threshold VBUS detector on through U24. The switch specifies ±1 µA off-state and ±2 µA power-off leakage under its published conditions; isolation is bounded, not an assertion of exactly zero current.

The monitor's 19/35 Âµs propagation and 300 Âµs startup values are typical. They are not guaranteed maximum response times. No powered sequencing, USB eye, hot-plug or NUC-off measurements have been made. The complete installed cable route must pass sustained USB2 streaming and repeated attach/detach at its intended mode and rate. Capture host VBUS, /OE and both data lines for:

* Host off, carrier on; host on, carrier off; and both initially off.
* Each possible power-on order, host VBUS loss/restoration and carrier brownout/recovery.
* Hot unplug/replug and host shutdown/sleep states, recording whether that physical port actually removes VBUS.
* Reset and bootloader entry with the carrier connected, verifying that an absent host still sees disconnected data.

Check absence of a sustained host-VBUS plateau, correct threshold hysteresis, enumeration/recovery and the required removal of upstream attachment after VBUS loss. Record source hashes, actual host/cable identity and raw captures. Passing native ERC/DRC is not this hardware acceptance.

Routine host access is through J28 and the factory-fitted CA1 pigtail. A direct Micro-B host cable bypasses the carrier power/data protections; isolate external VIN before that service connection. No owner header soldering, bridge cutting or cable modification is required.

Primary references: [TI switch](https://www.ti.com/lit/ds/symlink/ts3usb221a.pdf), [TI supervisor](https://www.ti.com/lit/ds/symlink/tps3702.pdf), [PJRC schematic](https://www.pjrc.com/store/teensy41.html), [NXP manual](https://www.pjrc.com/teensy/IMXRT1060RM_rev3.pdf), and the [USB 2.0 specification](https://www.usb.org/document-library/usb-20-specification). The authoritative local requirements are this document, `ASSEMBLY_POLICY.json`, the native schematic, and the current routing record; deleted historical calculations are not used as proof.
