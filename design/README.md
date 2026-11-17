# Design specifications

This folder contains the active interface references and design evidence for the native v4.1 KiCad project. It is not a fabrication approval.

- CONNECTORS.csv is the primary board-facing map; release_inputs/09_CONNECTOR_PINOUTS.csv is the controlled release copy used by manufacturing tools. The cavity and net maps agree with the saved schematic and PCB, including intentional no-connects.
- SYSTEM_HARNESS.csv retains cable and system notes. Its source hash records the schematic pin/net-name comparison only; PCB parity, cable pinouts, electrical ratings and physical fit remain open.
- TEENSY_PINMAP_CURRENT.csv reflects schematic net names.
- STACKUP_CURRENT.json and BOARD_LAYER_LIMIT.json record the current six-layer design and its quote assumptions. The historical four-layer calculator captures are indexed under historical_four_layer_observations in STACKUP_CURRENT.json.
- interfaces/ and documentation/ retain OEM, cable, integration, lighting and presentation references. Supplier, cart and purchase observations may be historical.
- release_inputs/ contains the controlled release inputs checked against saved v4.1. INPUT_MANIFEST.json records the current checksums and verification scope. Factory, supplier, cable, and physical qualification remain open, and the release is on hold.
- The old eight-pin J3 harness worksheet is archived. Use CONNECTORS.csv and the current harness schedule for the carrier connector assignment.
- fabrication/FACTORY_MODEL_HOLDS.csv retains component checks for reconfirmation. The current six-layer engineering review package lives in deliverables/review. Factory acceptance and the controlled release hold are tracked separately.

The saved schematic and PCB pass native connectivity and parity checks. Electrical, mechanical and factory verification remain open.
