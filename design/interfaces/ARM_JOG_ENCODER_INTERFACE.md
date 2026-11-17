# Relative arm-jog and limit interface

The schematic shows J11 as a Molex Micro-Fit 3.0 2×3 header (43045-0612): cavity 1 GND, 2 JOG_A_RAW, 3 JOG_B_RAW, 4 LIMIT_LOOP_RAW, 5 GND, and 6 intentionally unconnected. The NC in the limit pin's LIMIT_NC_LOOP name means normally closed; it does not mean the cavity is unconnected.

D6 protects the jog input paths; R69/R56 provide series resistance, R68/R52 provide pull-ups, C6/C5 provide local filtering, and U10 creates the Teensy-side jog signals. Confirm the electrical interface and the actual PCB connectivity before wiring.

The finished cable, connector engagement, encoder direction, disconnect behavior and noise tolerance still need physical verification.
