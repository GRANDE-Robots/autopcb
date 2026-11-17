# AutoPCB

AutoPCB takes design requirements or an existing KiCad project through layout preview, verification, and reproducible fabrication export. The desktop and CLI run the same jobs. Edit the native schematic and board in KiCad.

## Install and open

```bash
python -m pip install ".[desktop,benchmark]"
autopcb doctor
autopcb-desktop
```

Install KiCad 10 with a matching Python that can import `pcbnew`. Use `KICAD_CLI` and `KICAD_PYTHON` if the executables need to be selected explicitly.

## Workspace

| Folder | What goes there |
| --- | --- |
| `config/` | Routing defaults and policies in `config/projects/<project>/`. Edit campaigns live in `config/campaigns/<project>/`. |
| `data/` | Requirements, a component catalog, and project evidence such as stackup and load budgets in `data/projects/<project>/`. |
| `results/` | Proposals, native files in `results/projects/`, run records in `results/jobs/`, and export packages in `results/exports/`. |
| `docs/` | Product documentation. |
| `scripts/` | Shared jobs, KiCad adapters, and verification tools. |

Requirements default to `data/requirements.txt`. Use [the example requirements](data/requirements.example.txt) or supply a text file. A custom catalog can go in `data/catalog.json`. See [project inputs](data/README.md) for the expected engineering files and migration from `design/`. Generated results and local campaigns are Git-ignored.

### Start from requirements

```bash
autopcb propose
autopcb create results/proposals/PROPOSAL_ID.json --approve PROPOSAL_ID --name "Control Board"
autopcb verify
```

Review the proposal before choosing its ID. The generated `.kicad_pro`, `.kicad_pcb`, `.kicad_sch`, design rules, and local library references go in `results/projects/Control Board/`. The offline catalog covers an I²C temperature sensor and a full-speed USB Type-B breakout. Missing ratings or requirements stay visible in the proposal. `lookup-sources` explicitly retrieves public manufacturer sources named by an approved proposal.

### Open, preview, and verify a KiCad design

```bash
autopcb discover
autopcb --project "path/to/board.kicad_pro" inspect
autopcb --project "path/to/board.kicad_pro" configure-layout
autopcb --project "path/to/board.kicad_pro" route --rounds 10
autopcb --project "path/to/board.kicad_pro" compare RUN_ID
autopcb --project "path/to/board.kicad_pro" apply RUN_ID
autopcb --project "path/to/board.kicad_pro" verify
autopcb --project "path/to/board.kicad_pro" export --draft
```

Use `configure-layout` to create or copy a reviewed policy into `config/projects/<project>/autopcb.json`. AutoPCB combines workspace defaults with native limits when it creates a policy. It can read an existing project-local policy for compatibility. Use `autopcb campaign new --name "Layout review"` to start an edit campaign. Use `--resume RUN_ID` for matching sources or `--renew RUN_ID` to recheck after tool changes. Preview runs against a private copy. Apply checks the files, protected geometry, and editor state before saving a validated improvement.

`--workspace PATH` selects the location of shared inputs and outputs. If several native projects are available, pass a file with `--project`. The desktop lets you open that file directly. For an imported board, the config and report paths include a project key so same-named boards stay separate. Verification and recovery records go in `results/jobs/<project>/`. Exports default to `results/exports/`. Use `--output` to choose an explicit new destination for an export or project.

### Evidence for a checked export

The optional `autopcb.project.json` profile uses `schema_version: 1`, an `entry` native filename, and an explicit `differential_pairs` list when `high_speed_reviewed` is true. The routing adapter protects those pairs. Independent verification reports ERC, DRC, schematic agreement, and current reference-copper findings.

A checked export requires passing native checks and an `electrical_review` pointer with the report's relative `file` and `sha256`. The report file can live in `data/projects/<project>/`. The version 1 review records the reviewer, the exact `sources` from verification excluding the profile and review file, and a `policy_sha256` of the profile without the review pointer. Compute the policy digest over compact UTF-8 JSON with sorted keys. The `checks` map includes passing `electrical` and applicable `reference_copper` checks, with a method and measurements for each. Draft packages disclose blockers. Production approval and hardware bring-up remain separate.

## Development and packaging

```bash
python scripts/run.py --project . test
python scripts/run.py --project . docs build-pdf
python -m pip install ".[desktop-build]"
python packaging/build_desktop.py --output results/distribution --bundle-only
```

The installer build uses Inno Setup on Windows, a disk image on macOS, and a Debian package on Linux. Build and qualify each on its target system. The development build has native Windows checks. Use `autopcb benchmark --baseline PATH` for a measured comparison under equal rule and resource budgets. The original `scripts/run.py` commands remain available for existing KiCad projects.

[Algorithm guide](docs/algorithm.pdf) · [Editable documentation](docs/algorithm.md)
