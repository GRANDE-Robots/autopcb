# Silkscreen standard

Silkscreen is a service aid, not decoration. It must remain readable at normal assembly/maintenance distance, grouped by subsystem, and never overlap copper, pads, component bodies, or another label.

- Top side: external connectors, LEDs, test points, Teensy/RTC sockets, and large capacitors are clearly named and polarity/pin-1 marked.
- Bottom side: reference text follows a consistent grid and orientation; dense parts keep the reference beside the actual body without crossing labels.
- Connector labels state the function where space permits; pin 1 and polarity markings remain beside the mating face.
- Keep functional clusters together: LiDAR, camera, IMU, motion, lamps/power, and service. Do not renumber a stable reference for appearance.

Important service labels: J9 is the received CA-MP2-MTi inlet; J30 is the separate IMU-to-NUC USB outlet; J28/CA1 are the Teensy service path; J4 is power-only; J22/J24 are powered VLP inputs and J23/J25 their data-only returns. JP1/JP2 are LiDAR3/4 NMEA solder selectors, not plug connectors.


## Service markings

The top silkscreen uses TP1 through TP24 and L1 through L12. Probe numbers are the native KiCad references. The LEDs use L only on the silkscreen and keep their diode references in the schematic, BOM and placement file.

| Silkscreen | KiCad | Indicates |
|---|---|---|
| L1 | D12 | Core 5 V |
| L2 | D13 | Field 5 V |
| L3 | D14 | 3.3 V |
| L4 | D15 | TOP PWM command |
| L5 | D16 | FRONT PWM command |
| L6 | D26 | Power window fault (active low) |
| L7 | D27 | PPS activity |
| L8 | D22 | LiDAR 1 fused 12 V |
| L9 | D23 | LiDAR 2 fused 12 V |
| L10 | D24 | LiDAR 3 fused 12 V |
| L11 | D25 | LiDAR 4 fused 12 V |
| L12 | D11 | Protected lamp 12 V |

| Probe | Net |
|---|---|
| TP1 | GND |
| TP2 | 3V3 |
| TP3 | SYS5_CORE |
| TP4 | EXT5_FIELD |
| TP5 | PPS_MASTER |
| TP6 | NMEA_TX_INV_3V3 |
| TP7 | CAM1_TRIG_MCU |
| TP8 | BTS_R_EN_MCU |
| TP9 | PWR_WINDOW_FAULT_N |
| TP10 | LIDAR_12_RAW |
| TP11 | LIDAR_12_PROT |
| TP12 | LIGHTS_12_RAW |
| TP13 | LIGHTS_12V |
| TP14 | EXT5_CONV_RAW |
| TP15 | EXT5_RAW |
| TP16 | TIMING_4V9 |
| TP17 | U3_OE_N |
| TP18 | NMEA_AUX_TX_3V3 |
| TP19 | L3_NMEA |
| TP20 | GND |
| TP21 | BTS_R_IS_RAW |
| TP22 | BTS_R_IS_ADC |
| TP23 | BTS_L_IS_RAW |
| TP24 | BTS_L_IS_ADC |


The gallery in `../../../deliverables/review/` is the current visual reference. Electrical correctness still comes from the native schematic and PCB.
