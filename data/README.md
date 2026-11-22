# Project inputs

Keep requirements and reviewed engineering evidence in `data/`. Use `data/projects/<project>/` for evidence that belongs to one board in a shared workspace.

| Input | Where it belongs |
| --- | --- |
| Project brief | `data/requirements.txt`. See [example requirements](requirements.example.txt). |
| Optional offline component catalog | `data/catalog.json` |
| Stackup and branch load budget | `STACKUP_CURRENT.json` and `POWER_LOAD_BUDGET.csv` in the project's data folder |
| Controlled release inputs | `release_inputs/` with its `INPUT_MANIFEST.json` and all files it lists |
| Assembly decisions and qualifications | `ASSEMBLY_POLICY.json`, `ASSEMBLY_ROTATION_OFFSETS.json`, and the evidence named by those policies |

Record the operating conditions and evidence behind each requirement. Tie reviewed limits and release inputs to the exact native project. Keep generated BOMs, placement files, and fabrication packages in `results/`.

A catalog design can enable area minimization by adding `compact_outline` with a positive `margin_mm` and a list of `movable_groups`. Each group lists parts that may translate together. All other parts retain their catalog poses. An empty list permits trimming around fixed placement. `minimum_mm` and `size_mm` give the lower and upper dimensions for each axis. A user-specified maximum can lower that ceiling. An exact size takes precedence.

## Schematic and silkscreen

Group related circuits and use consistent spacing. Keep reference and value text close to each part. Use wires where they explain local circuit structure, keep labels clear, and preserve component references and pin names.

Label connectors, indicators, and test points by function where space permits. Keep pin-1 and polarity marks beside the relevant part. Check legibility after assembly and inspect silkscreen clearance against exposed pads, mask openings, other text, and board edges.

## Older projects

AutoPCB can read historical `design/` paths. Move each controlled collection, such as `release_inputs/`, as a unit with its manifest and evidence. `data/archive/` is reserved for local snapshots outside the active input search.
