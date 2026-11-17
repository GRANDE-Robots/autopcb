# Socket CPL quarter-turn review

**Historical evidence:** The native 0° positions below describe an earlier board. The placement guard in ../ASSEMBLY_ROTATION_OFFSETS.json records the later 180° native positions. Recheck the current board and actual factory placement preview before use.

Apply a **+90° factory placement offset to J16 and J17 only**, for the exact XYECONN PM2.54-1-24P-Z / C53244560 sockets. Their native CAD angle stays0°; the physical footprints, contact numbering, wiring and body centres stay fixed.

The actual JLC preview showed horizontal socket models over the vertical hole rows. The manufacturer drawing shows a uniform unkeyed passive1×N row, with no locating pegs or special contact-end numbering shown. For24 positions, the contact span is58.42mm at2.54mm pitch. Both quarter-turn signs overlay the same contact positions; they differ by an electrically/mechanically equivalent180° reversal of this socket. **The catalog model's unique pin1-marker datum has not been proved.**

| Ref | Factory centre X,Y(mm) | Native pin1 X,Y(mm) | Current→corrected factory angle |
|---|---|---|---|
|J16|58.620000, 67.746000|71.620000, 16.044000|0°→90°|
|J17|43.380000, 67.746000|56.380000, 16.044000|0°→90°|

Source drawing: [XYECONN PM2.54-1-NP-Z RevA](https://www.lcsc.com/datasheet/C53244560.pdf), local SHA256 b97fcac7278166a8ba9553484eb0b744cc5d76f674e876e35da4356efed60c33. Exact geometry, source hashes and preview evidence are recorded in SOCKET_CPL_ORIENTATION_REVIEW.json.

After corrected-CPL reimport, verify both models overlay their24 native holes at the unchanged centres. Keep the owner Teensy insertion orientation tied to native VIN/GND pin1 ends; the unkeyed sockets do not prevent reversed module insertion. No native CAD, export or quote was changed by this review.
