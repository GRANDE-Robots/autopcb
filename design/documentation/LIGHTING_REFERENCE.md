# Lighting and ambient sensing — current v4.1



The current PCB design target is **12 V, up to 60 W / 5 A per TOP and FRONT channel**. F3/F4 are 8 A Littelfuse 0451008.MRL fuses. These are design and procurement targets, not a measured continuous operating rating. The previous 24 W / 3 A-fuse notes are superseded.



J12 is [TOP+, TOP switched−, FRONT+, FRONT switched−, reserved]. Keep each negative separate from the other channel, chassis and lamp housing. Use supplier-finished 18 AWG power harnesses and qualified contacts for the 5 A target, with length/temperature/bundle derating checked. The reserved cavity is not a third lamp channel.



Each 10 mΩ shunt dissipates 0.25 W at 5 A; INA180A2 gain 50 produces 2.5 V nominal. The ideal 3.3 V ADC scale is 6.6 A, not a hardware current limit. At 12 V common mode, the INA180 maximum input offset is 500 µV: with a 10 mΩ shunt this is ±50 mA, about 13% of one purchased 380 mA lamp before gain, shunt, ADC and temperature errors. Calibrate zero and gain at the operating temperature; do not claim precise low-load current measurement. The 1 kΩ/1 µF ADC filter has a nominal 159 Hz corner, so PWM current averaging also requires validation. CSD18540Q5B is specified at 3.3 mΩ maximum at 4.5 V gate drive and 25 C: 5 A conduction loss is 0.0825 W before temperature increase. Switching, startup and LED-driver inrush require measurement. The two brightness duties and enables are independent; the selected Teensy PWM pins share their timer frequency.



Retain gate-to-ground pulldowns for reset-off behaviour. Do not add a drain/lamp-minus pulldown: it would create an off-state lamp-current path and can cause ghost lighting. Missing/invalid ambient sensing must not command full brightness.



The purchased EC Buying TEMT6000 five-pack, Amazon B0CZRH2DHJ, supplies the upward TOP and forward FRONT heads. J13/J14 are [3.3 V, analog OUT, GND]. Carry all three wires through the sealed wall path to each board connector. Current board connectors are Micro-Fit 436500315 with matching finished female harnesses; the earlier Ultra-Fit part numbers are superseded. Verify real breakout pin order and output range before connection. Outdoor saturation, optical cover, glare and calibration remain unmeasured.



Two Super Bright LEDs **WLBS-40K12-12V** lamps were purchased by the owner on **19 October 2026**, one TOP and one FRONT: 4000 K, nominal 12 inch, $19.99 each / $39.98 pair. Receipt ORD-015278290 shows $5.36 shipping, $4.10 tax and **$49.44 paid**; estimated delivery is 23-29 October. **Do not purchase duplicate lamps.** The referenced purchase record and receipt are not present in this project folder; verify these historical purchase details against the owner records. The two-lamp load is approximately **9 W / 0.76 A at 12 V**. Each is 12 V, 4.5 W, approximately 380 mA, 330 lm, 120-degree beam and CRI 90+. Manufacturer documentation states 0–100% PWM dimming and IP67 fixture rating. The pair is a near-field prototype candidate, not proof of adequate scene brightness or camera-banding performance. No separate retail dimmer or power supply is needed for this proposed carrier-controlled path.



The owner-provided WLBS Rev V2.0 drawing (2024-12-18) gives **330 mm overall length**, 308 mm body length and 318 mm mounting spacing for the nominal 12-inch model; the lead is 184 mm. Verify the complete mounting envelope and connector/bend clearance. A 330 mm fixture leaves only 20 mm against a 350 mm envelope before those allowances; it does not fit a 250 mm direction. The female 5.5 x 2.1 mm barrel junction needs a finished protected mating harness. Fixture IP67 does not qualify the exposed connection.



The provided sheet does not establish a permitted PWM frequency range or power-cycle behavior of the integrated switch. Check operation, inrush, useful illumination and camera banding at actual exposure settings before qualification. Keep the 60 W/channel design allowance distinct from the selected 4.5 W lamp. Nilight automotive pods remain unqualified alternatives and were not added to carts. The owner lamp order is documented above; no powered test has been performed.





Sources: [TI MOSFET](https://www.ti.com/lit/ds/symlink/csd18540q5b.pdf), [Nilight flood](https://www.nilight.com/products/4inch-18w-1260lm-flood-led-pods), [Nilight spot](https://www.nilight.com/products/4inch-18w-1260lm-spot-led-pods), and retained purchase evidence in CONVERTER_INVENTORY/earlier lighting research. Owner lamp order recorded 19 October 2026; powered qualification remains pending.



Current lamp source: [WLBS-40K12-12V](https://www.superbrightleds.com/waterproof-led-light-bar-12vdc-338-lm-ft-dimmable-barrel-connector-and-on-off-switch-3000k-4000k-5000k+color-natural~4000k+length_in-12~in~1~ft), manufacturer WLBS Series Rev V2.0 specification supplied by the owner.



## Selected lamp lead protection — 20 October 2026

The finished lamp supplier must specify source-end inline protection coordinated with the WLBS integral 184mm lead and barrel contact ampacity and measured/guaranteed inrush, or substantiate that existing protection is adequate. The carrier 8A fuse supports the future larger-load envelope and is not automatically protection for this small fixture lead. No arbitrary fuse value is released without those limits.
