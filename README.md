# AutoPCB

AutoPCB takes design requirements or an existing KiCad project through layout preview, verification, and reproducible fabrication export. The desktop and CLI run the same jobs. Edit the native schematic and board in KiCad.

The [workflow map](docs/workflow.html) shows how board area, routing cost and electrical evidence affect each decision.

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

For a new reference design, omit board dimensions to minimize rectangular board area during placement. The catalog sets the minimum and nominal dimensions. Use `at most 32 x 22 mm` for a tighter ceiling or `35 x 25 mm` for an exact size. The fitter includes footprint bodies, courtyards, pads, drills and printed markings. It can slide catalog-authorized groups together with their orientations intact. The create result gives the outline measured from KiCad and lists any moved parts. Imported boards and previously approved proposals keep their specified outlines. Run Route and Verify to measure connectivity on the resulting board.

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

By default routing may move any footprint that is not locked in KiCad, on either side and with any pad type. A moved part takes its silkscreen with it. Board silkscreen, the outline, the stackup and the schematic stay fixed. Lock a part in KiCad to keep it in place, or narrow the policy: `"placement": {"sides": ["bottom"], "through_hole": false}` moves only bottom-side SMD parts and leaves the front silkscreen unchanged. `placement-study` routes two private copies of the saved design under the same rounds, time and memory, one with fixed placement and one with permitted moves. It applies each result to its copy, verifies it with KiCad, keeps the smallest verified board and reports opens, trace length, vias and moved parts. A slow escalation can spend the whole budget before recovery reaches placement; `"recovery": {"early_rework": true}` in the policy tries copper and placement repairs after a net's first failure.

The ordinary router preserves declared differential copper. Pair seeding currently covers a single two-terminal pair on a new catalog board with parallel pad rows. Existing boards can be routed and reviewed with their saved pair geometry.

A preview with zero measured opens and no queued work has the status `preview_zero_opens`. Pending work can be resumed. An exhausted search with opens reports `stalled` and exits with code 2. Apply saves a validated improvement, and Verify independently checks the saved board. Checked manufacturing export also requires a source-bound electrical review.

In the desktop Layout tab, the saved and preview boards show native pad outlines, drill holes, curved tracks, and front footprint markings. Copper fills are hidden in this view. In Verify, activate a reference-gap row to locate an uncovered sample on the saved board. Separate gaps along one trace have separate rows. A pin label identifies a plated through-hole pad touched at a trace endpoint. The JSON report records the first and last uncovered sampling stations in millimetres measured along the trace from its start. Each gap still requires electrical review.

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

Pull requests that change code run the full suite with KiCad 10 on Windows, macOS and Linux (`.github/workflows/tests.yml`). The manual [Installers workflow](.github/workflows/installers.yml) builds the Windows installer from the selected commit and uploads it with its SHA-256. The installer build uses Inno Setup on Windows, a disk image on macOS, and a Debian package on Linux. Run `qualify` from a fresh install on each target system. It creates both reference designs, verifies every guarded Apply against the saved PCB, and starts another preview when opens fall and rounds remain. A preview with no improvement ends that case's routing search. The report records the host, outline, toolchain, per-pass budget, saved opens and release blockers. The output directory retains the native projects, checks and draft packages. An incomplete qualification exits with status 2. Use `autopcb benchmark --baseline PATH` for a measured comparison under equal rule and resource budgets. The `--refinement` benchmark compares greedy routing with negotiated recovery under equal budgets. The original `scripts/run.py` commands remain available for existing KiCad projects.

To compare compact placement with the original catalog outline, run the same two circuits with identical budgets in fresh output directories.

```bash
autopcb qualify --output results/compact-control --rounds 65 --seconds 24 --memory-gib 2
autopcb qualify --output results/nominal-control --fixed-outlines --rounds 65 --seconds 24 --memory-gib 2
```

### Building and qualifying the Windows installer

Build on Windows with x64 Python, the `desktop-build` dependencies, and Inno Setup 6. The installer is per-user and adds a Start menu shortcut. The installed desktop loads `%LOCALAPPDATA%\AutoPCB\Workspace` by default. Use `--workspace` before `desktop` to select an existing workspace.

```powershell
New-Item -ItemType Directory -Path results/windows-release -Force | Out-Null
python packaging/build_desktop.py --output results/windows-release/installer --iscc 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe' 2>&1 |
    Tee-Object -FilePath results/windows-release/build.log
```

Omit `--iscc` to search for the compiler on PATH and in the standard Inno Setup 6 install locations. The output directory must be new. Compiling the installer leaves the Windows runtime checks pending.

Copy the installer and [the validation script](packaging/check_windows_release.ps1) to a clean Windows 11 x64 VM. Use a non-elevated PowerShell session that permits local scripts, with the same standard user and evidence directory for both phases. Phase Install requires a VM with AutoPCB and KiCad absent. It checks setup, the Start menu entry, the installed CLI, and the missing KiCad diagnostic. Then install KiCad 10.0.6 in its standard location in the VM and run Phase Qualify.

```powershell
.\check_windows_release.ps1 -Phase Install -Installer .\AutoPCB-1.0.0.dev0-windows.exe -EvidenceDir '..\AutoPCB QA'
# Install KiCad 10.0.6 in the VM between these commands.
.\check_windows_release.ps1 -Phase Qualify -Installer .\AutoPCB-1.0.0.dev0-windows.exe -EvidenceDir '..\AutoPCB QA'
```

Phase Qualify runs both reference projects from the installed executable, guides the desktop test, and checks uninstall and workspace retention. The expected checked-export refusal is the missing source-bound electrical review on the disposable boards. Copy back the evidence directory before resetting the VM. If Windows blocks an executable, the report identifies the file and failing step so the build can be reviewed under the VM's normal policy.

For a source preflight on a workstation with PySide6 and a matching KiCad toolchain, run this from the repository root:

`python -B -m unittest discover -s scripts -p test_desktop.py`

The test drives the Qt controls offscreen and uses the real KiCad jobs for a disposable USB board. This covers preview, guarded Apply, native verification and draft export before the separate install and uninstall trial on the VM.

[Algorithm guide](docs/algorithm.pdf) · [Editable documentation](docs/algorithm.md)
