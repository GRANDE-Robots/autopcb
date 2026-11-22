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

Requirements default to `data/requirements.txt`. Use [the example requirements](data/requirements.example.txt) or supply a text file. A custom catalog can go in `data/catalog.json`; a catalog part placed with `"side": "bottom"` is generated on the bottom layer. See [project inputs](data/README.md) for the expected engineering files and migration from `design/`. Generated results and local campaigns are Git-ignored.

### Start from requirements

```bash
autopcb propose
autopcb create results/proposals/PROPOSAL_ID.json --approve PROPOSAL_ID --name "Control Board"
autopcb verify
```

Review the proposal before choosing its ID. The generated `.kicad_pro`, `.kicad_pcb`, `.kicad_sch`, design rules, and local library references go in `results/projects/Control Board/`. The offline catalog covers an I²C temperature sensor and a full-speed USB Type-B breakout. The USB layout tries a single-layer differential pair and an adjacent ground plane when creating the draft. Its nominal geometry and the limits of the review appear in the proposal. Missing ratings or requirements stay visible. `lookup-sources` retrieves public manufacturer sources named by an approved proposal.

### Open, preview, and verify a KiCad design

```bash
autopcb discover
autopcb --project "path/to/board.kicad_pro" inspect
autopcb --project "path/to/board.kicad_pro" configure-layout
autopcb --project "path/to/board.kicad_pro" route --rounds 10
autopcb --project "path/to/board.kicad_pro" compare RUN_ID
autopcb --project "path/to/board.kicad_pro" apply RUN_ID
autopcb --project "path/to/board.kicad_pro" placement-study --rounds 20 --seconds 20
autopcb --project "path/to/board.kicad_pro" verify
autopcb --project "path/to/board.kicad_pro" export --draft
```

Use `configure-layout` to create or copy a reviewed policy into `config/projects/<project>/autopcb.json`. AutoPCB combines workspace defaults with native limits when it creates a policy. It can read an existing project-local policy for compatibility. Use `autopcb campaign new --name "Layout review"` to start an edit campaign. Use `--resume RUN_ID` for matching sources or `--renew RUN_ID` to recheck after tool changes. Preview runs against a private copy. Apply checks the files, protected geometry, and editor state before saving a validated improvement.

Top-side footprints and front silkscreen stay fixed; only unlocked bottom-side SMD parts can move. `placement-study` routes two private copies of the saved design under the same rounds, time and memory, one with fixed placement and one with permitted moves. It applies each result to its copy, verifies it with KiCad, keeps the smallest verified board and reports opens, trace length, vias and moved parts. A slow escalation can spend the whole budget before recovery reaches placement; `"recovery": {"early_rework": true}` in the policy tries copper and placement repairs after a net's first failure.

The ordinary router preserves declared differential copper. Pair seeding currently covers a single two-terminal pair on a new catalog board with parallel pad rows. Existing boards can be routed and reviewed with their saved pair geometry.

`--workspace PATH` selects the location of shared inputs and outputs. If several native projects are available, pass a file with `--project`. The desktop lets you open that file directly. For an imported board, the config and report paths include a project key so same-named boards stay separate. Verification and recovery records go in `results/jobs/<project>/`. Exports default to `results/exports/`. Use `--output` to choose an explicit new destination for an export or project.

### Use AutoPCB from a phone

KiCad runs on Windows, macOS and Linux, so phones drive a computer that has it. On that computer run `autopcb serve --host ADDRESS`, using its LAN or Tailscale address, and open the printed link on an Android phone or iPhone. The page lists the workspace's projects, shows each board, runs verify, route preview, apply and draft export, and shows every job's result. The link carries the access token and anyone holding it can run jobs, so share it only over a network you trust: Tailscale encrypts the traffic, plain LAN HTTP does not. Without `--host` the page is reachable only from the same computer.

### Evidence for a checked export

The `autopcb.project.json` profile uses `schema_version: 1` and an `entry` native filename. For checked export, record `high_speed_reviewed: true` and list each differential pair as two native net names, positive first. Use an empty `differential_pairs` list for a project reviewed as having no pairs. Set `reference_net` if the return has a name other than `GND`. Verify and export independently check ERC, DRC, and schematic agreement, then sample adjacent reference copper on the refilled PCB. The findings include trace locations, layer transitions, and any unreviewed scope.

A checked export requires passing native checks and an `electrical_review` pointer with the report's relative `file` and `sha256`. The report file can live in `data/projects/<project>/`. The version 1 review records the reviewer, the exact `sources` from verification excluding the profile and review file, and a `policy_sha256` of the profile without the review pointer. Compute the policy digest over compact UTF-8 JSON with sorted keys. The `checks` map includes passing `electrical` and applicable `reference_copper` checks, with a method and measurements for each. Use `checks.reference_copper.findings` to map each current finding ID to a documented engineering disposition. Draft exports include the same blockers and raw findings as verification. Production approval and hardware bring-up remain separate.

## Development and packaging

```bash
python scripts/run.py --project . test
python scripts/run.py --project . docs build-pdf
python -m pip install ".[desktop-build]"
python packaging/build_desktop.py --output results/distribution --bundle-only
autopcb qualify --output results/host-qualification
```

Pull requests that change code run the full suite with KiCad 10 on Windows, macOS and Linux (`.github/workflows/tests.yml`). The installer build uses Inno Setup on Windows, a disk image on macOS, and a Debian package on Linux. Run `qualify` from a fresh install on each target system. It creates private copies of both reference designs, exercises preview and guarded Apply, and reads the saved projects back through verification and export. The report records the host, toolchain, search budget, remaining opens and release blockers. An incomplete run exits with status 2 and retains the evidence. Use `autopcb benchmark --baseline PATH` for a measured comparison under equal rule and resource budgets, or `--refinement` to compare greedy routing, one pass per net, with the project's recovery and rework on identical copies. The original `scripts/run.py` commands remain available for existing KiCad projects.

[Algorithm guide](docs/algorithm.pdf) · [Editable documentation](docs/algorithm.md)
