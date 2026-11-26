# Temperature sensor review and bring-up

This review covers the catalog `temperature` design: an MCP9808 I²C temperature sensor (U1, MSOP-8) with 4.7 kΩ pull-ups (R1 on SDA, R2 on SCL), a 100 nF decoupling capacitor (C1) and a 4-pin header (J1: VDD, GND, SDA, SCL), on two layers in a 35 x 25 mm outline. The figures below come from a board generated from the example requirements and routed by AutoPCB with KiCad 10.0.8 on Linux. A 10-round route preview left 3 open connections and resuming it to 15 rounds reached 0. Re-measure a saved board with `verify` before reusing any figure. Datasheet values come from Microchip DS20005095B, the source the catalog names for this part.

This page records software checks, KiCad measurements and calculations. It is not hardware evidence, and it does not replace the source-bound electrical review that a checked export requires.

## What KiCad checked

| Check | Result |
| --- | --- |
| Open connections, generated board before routing | 13 |
| Open connections, saved board after Apply | 0 |
| Native checks on the saved board (`verify`) | Passed, no findings |
| Reference-copper screen | Not applicable, the design has no differential pair |
| Checked export | Blocked until a source-bound electrical review is attached |
| Hardware validation | Pending |

## Routed copper

Every track is 0.25 mm wide, the generated default. The board declares 1.6 mm total thickness and 35 µm copper on both layers. Lengths are the sums of KiCad track segments. Resistance is calculated from the declared copper as ρL/(wt), with ρ = 1.72 x 10⁻⁸ Ω·m at 20 °C. It leaves out pad and via resistance.

| Net | Length | Layers | Vias | Resistance |
| --- | --- | --- | --- | --- |
| GND | 31.80 mm | F.Cu | 0 | 62.5 mΩ |
| SCL | 23.81 mm | F.Cu | 0 | 46.8 mΩ |
| SDA | 21.49 mm | F.Cu | 0 | 42.3 mΩ |
| VDD | 25.27 mm (13.88 mm F.Cu, 11.39 mm B.Cu) | F.Cu and B.Cu | 3 | 49.7 mΩ |

The board has no copper zone. GND is a track on the front layer, and the back layer carries only 11.39 mm of VDD.

## Supply and decoupling

U1 draws 200 µA typical and 400 µA maximum from 2.7 to 5.5 V. The VDD and GND tracks add about 112 mΩ round trip, so 400 µA drops about 45 µV. The datasheet gives a power-supply rejection of −0.1 °C/V, so the drop has no measurable effect on the reading.

C1 is 100 nF, inside the 0.1 to 1 µF the datasheet recommends between VDD and GND, and the datasheet asks for it as close to the pins as possible. Its VDD pad is 4.03 mm in a straight line from U1 pin 8. The supply ripple at U1 has not been measured.

## I²C bus

The three address inputs, pins 5 to 7 (A2, A1, A0), are tied to GND. With the datasheet base code 0011, the 7-bit address is 0x18. ALERT (pin 3) is left open. If ALERT is ever used it needs its own pull-up, because the output is open-drain.

Rise time between the 30 % and 70 % levels is about 0.8473·R·C. With R = 4.7 kΩ:

| Bus speed | Rise-time limit | Largest bus capacitance |
| --- | --- | --- |
| 400 kHz | 300 ns, the MCP9808 interface table | about 75 pF |
| 100 kHz | 1000 ns, I²C-bus specification UM10204 | about 250 pF |

The bus capacitance is the sum of the host, any cable on J1 and the board. The board's tracks are about 22 to 24 mm and add little, but the capacitance has not been measured. Check it on the bench, especially with a cable. With the line held low, a 4.7 kΩ pull-up from 3.3 V carries about 0.70 mA, well under the 3 mA at which the datasheet specifies VOL of 0.4 V.

## Thermal

The sensor reads its own die temperature, which follows the board through its leads. Two points matter for accuracy:

- **Self-heating.** The datasheet gives 211 °C/W for the MSOP-8. At 3.3 V, 200 µA typical gives 0.66 mW and about 0.14 °C. 400 µA maximum gives 1.32 mW and about 0.28 °C. These are calculated, not measured. The datasheet's own worked example uses 3.6 V and 500 µA and names a different package.
- **No ground layer.** Section 6.1 of the datasheet recommends a ground layer under the device pins for good thermal conduction from the board to the die. The generated board has none. The heat path from a host warmer than the room runs through 0.25 mm tracks, and from the air through the package. A ground pour under U1 is a candidate improvement that AutoPCB does not make today.

Heat from a host or cable enters the board at J1, which is 14.3 mm from U1, centre to centre.

## Bench bring-up

Use a DMM, a 3.3 V supply with current measurement, an I²C host (for example a USB-to-I²C adapter) and a reference thermometer with a stated uncertainty. An oscilloscope of at least 100 MHz is optional.

| Step | Method | Pass |
| --- | --- | --- |
| 1. Inspect | Check U1 pins, the 0805 parts and J1 for bridges, voids and orientation. | No defect |
| 2. Continuity | Unpowered, measure J1.1 to U1.8, R1.1, R2.1 and C1.1 (VDD), J1.2 to U1.4, U1.5, U1.6, U1.7 and C1.2 (GND), J1.3 to U1.1 (SDA) and J1.4 to U1.2 (SCL). | Each below 1 Ω |
| 3. Resistance between nets | Unpowered, measure VDD to SDA, VDD to SCL and SDA to SCL, then VDD to GND, SDA to GND and SCL to GND. | VDD to SDA and VDD to SCL about 4.7 kΩ, SDA to SCL about 9.4 kΩ, VDD to GND above 1 MΩ once C1 has charged. Record the others and investigate any below 100 kΩ |
| 4. Idle supply | Apply 3.3 V to J1.1 and J1.2 with the bus idle and measure the supply current. | At most 400 µA |
| 5. Identify | Scan the bus, then read register 0x06. | One device at 0x18, and 0x0054 |
| 6. Read temperature | After 10 minutes at steady room temperature, read register 0x05 and compare it with the reference thermometer placed next to U1. | Difference within 0.5 °C plus the reference uncertainty |
| 7. Bus edges, optional | Probe SDA and SCL at J1 at 400 kHz. | Rise time between 30 % and 70 % at most 300 ns |
| 8. Repeat reads | Read the ambient register 1000 times. | No NACK or bus error |

Record the board serial, host, cable, reference thermometer and each measured value. The 0.5 °C limit is the datasheet maximum accuracy from −20 to +100 °C at 3.3 V.
