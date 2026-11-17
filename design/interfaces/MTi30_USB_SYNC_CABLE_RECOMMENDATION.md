# MTi-30 factory cable and Micro-Fit adapter

**Design status:** IMU USB and timing are restored as work in progress. The USB-capable IMU variant, NUC supply voltage under load, 0.5 A fuse behavior, cable, ESD placement, USB link and connector manufacture remain unverified; the board is not approved for use or fabrication.

The owner reports having the Xsens CA-MP2-MTi; the 19 October 2026 Mouser shipment notice lists that part. The owner can choose the tray-end connector or an adapter for J9; no particular modification or adapter is confirmed. Xsens documents access to all nine sensor contacts, but does not recommend USB on the stock CA-MP2-MTi because its shield is not connected at the Molex end. Whether a replacement termination can provide suitable shield continuity is unverified; a change of connector alone does not establish a qualified USB link. [Xsens MTi user manual](https://www.xsens.com/hubfs/Downloads/usermanual/MTi_usermanual.pdf).

J9 uses the Molex Micro-Fit 3.0 43045-1012 vertical header with 43025-1000 mating housing. Cavities 1..9 retain the Xsens mapping: VIN, GND, USB D+, serial TX NC, serial RX NC, USB D-, SyncIn, ClockSync, SyncOut. Cavity 10 is physically blank and electrically NC.

J30 remains the NUC-side USB-C outlet. It is intended to carry the USB path from J9 to the NUC; it is not a second IMU connector. U25 is currently unconnected on the PCB, so its ESD protection is not yet effective. Timing stays on the MCU path.

Before use, the factory must verify its adapter drawing/pinout, enclosure bend and latch clearance, USB enumeration and sustained streaming, MTi supply voltage, and timing-polarity/reference-loss behavior.
