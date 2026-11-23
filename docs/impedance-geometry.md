# Differential-pair geometry review

Use the KiCad worker to compare straight routing on a saved board with the width, edge-to-edge gap and adjacent return layers specified for each net class. The command reads the selected project and its saved zone fills. Refill and save the board before running it.

```console
python scripts/run.py --project path/to/controller.kicad_pro pcb impedance --stackup reviewed-stack.json --check-reference
python scripts/run.py --project path/to/controller.kicad_pro pcb impedance --stackup reviewed-stack.json --json
python scripts/run.py --project path/to/controller.kicad_pro pcb impedance --stackup reviewed-stack.json --emit-rules
```

The default input is `STACKUP_CURRENT.json` in the project's data folder. Use the native file to select a family in a directory with multiple KiCad projects. Record the pair names in the existing `autopcb.project.json` fields `high_speed_reviewed` and `differential_pairs`. `reference_net` identifies the return net. Both conductors must belong to the same dedicated native net class. Every net in a configured class must occur in the reviewed list.

The following dimensions illustrate the input format. Obtain targets for the actual manufacturing stack before using them for a board.

```json
{
  "stackup_id": "example",
  "copper_layers": 2,
  "physical_stack_mm": [
    {"layer": "F.Cu", "type": "copper", "thickness": 0.035},
    {"layer": "dielectric 1", "type": "core", "materials": ["FR4"], "thickness": 1.53},
    {"layer": "B.Cu", "type": "copper", "thickness": 0.035}
  ],
  "controlled_impedance": {
    "width_tolerance_mm": 0.002,
    "gap_tolerance_mm": 0.015,
    "sample_step_mm": 0.10,
    "families": [
      {"netclass": "Reviewed", "target_ohms": 90, "layers": [
        {"signal_layers": ["F.Cu"], "reference_layers": [["B.Cu"]], "width_mm": 0.20, "gap_mm": 0.30}
      ]}
    ]
  }
}
```

The command compares the supplied layer order, dielectric names and thicknesses against the native stack. For each conductor it reports total straight length, length with a parallel mate at the specified width and gap, and length with sampled return copper across the pair on every required layer. It reports uncoupled launches, curved segments and undeclared routed layers for review. Via barrels are counted separately. The optional check returns exit code 2 when the sampled geometry is incomplete or the review cannot run. Narrow interruptions can fall between samples. Actual impedance, propagation delay, layer transitions and production acceptance require separate checks.

The rules option prints KiCad routing targets to stdout. An optional `connection_floor_mm` adds minimum track and connection widths for the indicated class and layer. The command does not write a board or install rules.
