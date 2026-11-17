# OS1 Rev08 and MTi-30 interface evidence

Checked 2026-10-16. Read-only review of current official documentation, relevant Ouster correspondence in the UCLA Gmail account, and existing project/device records. No CAD changes, messages, drafts, purchases or powered device tests were performed.

## OS1 identification and correspondence

The replacement sensor is identified in Amber's October 7 draft quotation, Q-18501-1, as **OS1-080-128U-A-C-002-BA-000-000**: OS1 Rev8.0, RGB, 128U, side-mount 1000Base-T, standard base heatsink and halo cap. The email says the quote was awaiting internal approval. Page 1 does not itemize a cable or interface box; that does not establish whether one will be included in the eventual shipment. [OEM quote email](https://mail.google.com/mail/u/?authuser=aaronjs%40g.ucla.edu#all/1a1173ba6892dea3).

The latest technical-response promise found is Amber's October 9 message proposing a connection with Cameron. The user's October 13 follow-up asks for the supplied cable/interface box, cable-end part numbers, pinout and PPS/NMEA electrical specifications. No later answer to those questions was found in the targeted September/October Ouster-domain correspondence search. This is a correspondence result, not proof of shipment contents. [Latest user request](https://mail.google.com/mail/u/?authuser=aaronjs%40g.ucla.edu#all/1a1374a4254fcd0c).

## What current Rev08 sources establish

The current OS1 Rev8 datasheet, revision August 3, 2026, applies to OS1-080-XXX-X-X-002-XX. It specifies nominal 12/24 V operation, a 9–58 V operating range and at most 1 Vpp supply ripple. Consumption at 1024 × 10 is 10–20 W, nominal 15 W, with a 22 W cold-start peak; Ouster recommends at least 30 W continuous supply capacity. It lists PPS, NMEA $GPRMC, PTP and gPTP. The connector drawing identifies separate PWR, GND, MIO0 and MIO1 alongside the four Ethernet pairs. These facts support the intended power/data architecture but do not specify the MIO logic thresholds or prove the carrier's output stage compatible. [Official Rev8 OS1 datasheet](https://data.ouster.io/downloads/datasheets/datasheet-rev8-v4p0-os1.pdf).

The **explicit Rev8 OS1 hardware page says its hardware manual has not yet been published** and directs users to Ouster support with the serial number for electrical specifications. This is stronger evidence than simply failing to find a PDF. The general hardware digital-I/O pages resolve to Rev7; their voltage/current limits cannot qualify this Rev8 unit. [Official Rev8 OS1 hardware page](https://docs.ouster.com/sensor-docs/hardware/rev8/os1.md), [official downloads](https://ouster.com/downloads).

Still unverified for this exact Rev08 model: MIO0/MIO1 input LOW/HIGH thresholds, absolute voltage limits, required source/sink current or input impedance, minimum PPS pulse width, and the authoritative mapping/configuration that assigns PPS and NMEA to the two MIO pins. The current board's planned MIO0/PPS and MIO1/NMEA assignment remains an integration assumption until OEM confirmation or actual-device documentation establishes it. No Rev7 value was substituted.

## Timing configuration evidence, separate from electrical qualification

Official firmware 4.0 documentation supports `TIME_FROM_SYNC_PULSE_IN` with `INPUT_NMEA_UART`, matching PPS polarity, and matching NMEA baud/polarity. The time-synchronization page assumes 1 Hz external PPS. It distinguishes pulse counting from the absolute time supplied by $GPRMC; its default `nmea_leap_seconds=0` yields Unix/UTC-style epoch time. A nonzero correction must be selected deliberately and consistently with the rest of the system. PTP uses the Ethernet link. [Time synchronization](https://docs.ouster.com/sensor-docs/firmware/4.0/time-synchronization.md).

The documented commissioning checks are PPS lock, NMEA lock, decoded GPRMC and valid timestamps through `/api/v1/time`. The MIO API allows channel configuration/state readback. These are useful software checks, not substitutes for the missing Rev8 electrical limits. The firmware 4.0 GPS page still links its wiring guidance to Rev7 hardware. [GPS synchronization](https://docs.ouster.com/sensor-docs/firmware/4.0/gps.md), [MIO query API](https://docs.ouster.com/sensor-docs/firmware/4.0/mio-query.md).

## Finished cable choices

| Official assembly | Exposed connections | Consequence for this build |
|---|---|---|
| M12AP / M12XR Y-cable | Ouster sensor connector; 4-contact A-coded M12 male for power/signals; 8-contact X-coded M12 female for Ethernet | Promising factory-finished route for both timing signals and power, with Ethernet separate. Exact orderable MPN, length/orientation, mating power/signal cavity assignment and compatibility with the quoted unit remain unconfirmed. |
| RJ45R Y-cable | Ouster sensor connector; female RJ45; female 5.5 × 2.1 mm power barrel jack | The published assembly exposes Ethernet and power, with no accessible PPS/NMEA breakout. Do not treat this as the complete cable for the requested PPS/NMEA system. |

The M12 datasheet lists straight/right-angle versions and standard 1/5 m lengths. It rates the sensor connector IP68/69K and the M12 branches IP67 under their specified mating conditions. The RJ45R datasheet lists straight 1/5/10/15 m and right-angle 5 m versions. A board-side finished adapter is still needed to connect an M12 power/signal branch to the carrier without owner termination. Neither datasheet establishes what is included in the quoted shipment. [Official M12 Y-cable datasheet](https://data.ouster.io/downloads/datasheets/datasheet-Ycable-M12AP-M12XR.pdf), [official RJ45R Y-cable datasheet](https://data.ouster.io/downloads/datasheets/datasheet-Ycable-RJ45R.pdf).

No exact cable SKU is qualified for ordering by this research. Choosing a generic M12 lead solely from contact count would leave pin mapping and mating details unresolved.

## Installed MTi-30 variant

Existing project records associate DeviceID **0368319D** and USB VID/PID **2639:0003** with an RS232 Mk5 configuration. This is configuration/history evidence. It does not independently establish the installed product suffix, such as `MTi-30-2A8G4`, or a current physical label/interface readback.

The project purchase inventory contains no Xsens/Movella/MTi model identification. The local `Engineering/Datasheets and Software/IMU` folder contains a general MTi manual and MT Software Suite installers, with no device-specific purchase or label record. The maintained `docs/interfaces/MTi30_USB_SYNC_CABLE_RECOMMENDATION.md` already records this identification limit. No broader personal archive was searched.

The user reports a CA-MP2-MTi and may choose the tray-end connector or an adapter. The Mouser shipment notice lists this cable; it does not identify the installed sensor variant. Xsens documents nine sensor contacts on this cable, but discourages USB use on the stock termination because the shield is not connected at the Molex end. An alternative termination and complete shield/USB behavior are unverified. The MTi manual also says RS422 variants do not provide USB. J9 is the 2x5 Micro-Fit inlet; J30 is the intended NUC-facing USB-C connection. [Official MTi user manual](https://www.xsens.com/hubfs/Downloads/usermanual/MTi_usermanual.pdf).

## Release implication

The exact OS1 sensor identity is now supported by OEM correspondence. Its Rev08 timing-input electrical compatibility and the final finished cable remain open for specific, documented reasons; the pending October 13 question already requests the necessary OEM facts. The installed MTi-30 suffix likewise remains unconfirmed by the inspected local evidence. These gaps cannot be closed by native CAD ERC/DRC, or by treating a published Rev7 interface as equivalent to Rev8.
