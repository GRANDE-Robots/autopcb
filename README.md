# AutoPCB

## Overview

AutoPCB takes design requirements or an existing KiCad project through layout preview, verification, and reproducible fabrication export. The desktop and CLI run the same jobs. Edit the native schematic and board in KiCad.

The [workflow map](docs/workflow.html) shows how board area, routing cost and electrical evidence affect each decision. The [algorithm guide](docs/algorithm.pdf) explains the search and its limits, and its [editable source](docs/algorithm.md) stays beside it.

## Responsibilities

- Turn requirements and an offline component catalog into a reviewable proposal and a native KiCad project (`propose`, `create`).
- Preview routing and placement on a private copy of an existing KiCad project, and save only a validated improvement (`route`, `compare`, `apply`, `placement-study`).
- Check the saved board independently with KiCad: ERC, DRC, schematic agreement and sampled reference copper (`verify`).
- Export checked fabrication packages, and draft packages that list their blockers (`export`).
- Qualify an installation on the reference designs and benchmark routing arms under equal budgets (`qualify`, `benchmark`).
- Run the same jobs from the terminal, the desktop app, and a phone or browser (`autopcb serve`).

AutoPCB does not replace editing in KiCad or engineering review. Production approval and hardware bring-up remain separate.

## Requirements

- Python 3.9 or newer.
- KiCad 10 with a Python that can import `pcbnew`. Set `KICAD_CLI` and `KICAD_PYTHON` if the executables need to be selected explicitly. CI installs the current KiCad 10 release on each system without pinning a patch version. The local Linux runs behind this README used KiCad 10.0.8, and the manual Windows installer check is pinned to KiCad 10.0.6.
- Optional extras in `pyproject.toml`: `desktop` (PySide6), `benchmark` (psutil), `review` (Pillow, pypdfium2, markdown-it-py) and `desktop-build` (PySide6, PyInstaller).
- Building the Windows installer also needs Inno Setup 6.

## Installation

```bash
python -m pip install ".[desktop,benchmark]"
autopcb doctor
autopcb-desktop
```

`autopcb doctor` checks that a matched KiCad CLI and native worker are available.

### Build the desktop installer

```bash
python -m pip install ".[desktop-build]"
python packaging/build_desktop.py --output results/distribution --bundle-only
```

Build on Windows with x64 Python, the `desktop-build` dependencies, and Inno Setup 6. The installer is per-user and adds a Start menu shortcut. The installed desktop loads `%LOCALAPPDATA%\AutoPCB\Workspace` by default; an installed Linux build uses `~/.local/share/AutoPCB/Workspace` (or `$XDG_DATA_HOME`) and macOS uses `~/Library/Application Support/AutoPCB/Workspace`. Use `--workspace` before `desktop` to select an existing workspace.

```powershell
New-Item -ItemType Directory -Path results/windows-release -Force | Out-Null
python packaging/build_desktop.py --output results/windows-release/installer --iscc 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe' 2>&1 |
    Tee-Object -FilePath results/windows-release/build.log
```

Omit `--iscc` to search for the compiler on PATH and in the standard Inno Setup 6 install locations. The output directory must be new. Compiling the installer leaves the Windows runtime checks pending.

## Usage

### Workspace

| Folder | What goes there |
| --- | --- |
| `config/` | Routing defaults and policies in `config/projects/<project>/`. Edit campaigns live in `config/campaigns/<project>/`. |
| `data/` | Requirements, a component catalog, and project evidence such as stackup and load budgets in `data/projects/<project>/`. |
| `results/` | Proposals, native files in `results/projects/`, downloaded sources in `results/sources/`, run records in `results/jobs/`, and export packages in `results/exports/`. See [results layout](docs/results-layout.md). |
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
autopcb --project "path/to/board.kicad_pro" placement-study --variants 8 --seed 1 --rounds 20 --seconds 20
autopcb --project "path/to/board.kicad_pro" verify
autopcb --project "path/to/board.kicad_pro" export --draft
```

Use `configure-layout` to create or copy a reviewed policy into `config/projects/<project>/autopcb.json`. AutoPCB combines workspace defaults with native limits when it creates a policy. It can read an existing project-local policy for compatibility. Use `autopcb campaign new --name "Layout review"` to start an edit campaign. Use `--resume RUN_ID` for matching sources or `--renew RUN_ID` to recheck after tool changes. Preview runs against a private copy. Apply checks the files, protected geometry, and editor state before saving a validated improvement.

By default routing may move any footprint, on either side, with any pad type and even if it is locked in KiCad. A moved part takes its silkscreen with it. Board silkscreen, the outline, the stackup and the schematic stay fixed. Narrow the policy to keep parts in place: `"placement": {"locked": false}` leaves KiCad-locked parts where they are, and `"placement": {"sides": ["bottom"], "through_hole": false}` moves only bottom-side SMD parts and leaves the front silkscreen unchanged. `placement-study` routes two private copies of the saved design under the same rounds, time and memory, one with fixed placement and one with permitted moves. It applies each result to its copy, verifies it with KiCad, keeps the smallest verified board and reports opens, trace length, vias and moved parts. With `--variants`, it first packs the movable parts of each copy toward a seeded random point until their courtyards touch, then counts how often fixed placement stalls and moves finish. Each packing reports which nets no route could complete as placed, and which parts block them. A slow escalation can spend the whole budget before recovery reaches placement; `"recovery": {"early_rework": true}` in the policy tries copper and placement repairs after a net's first failure.

The ordinary router preserves declared differential copper. Pair seeding currently covers a single two-terminal pair on a new catalog board with parallel pad rows. Existing boards can be routed and reviewed with their saved pair geometry.

A preview with zero measured opens and no queued work has the status `preview_zero_opens`. Pending work can be resumed. An exhausted search with opens reports `stalled` and exits with code 2. Apply saves a validated improvement, and Verify independently checks the saved board. Checked manufacturing export also requires a source-bound electrical review.

In the desktop Layout tab, the saved and preview boards show native pad outlines, drill holes, curved tracks, and front footprint markings. Copper fills are hidden in this view. In Verify, activate a reference-gap row to locate an uncovered sample on the saved board. Separate gaps along one trace have separate rows. A pin label identifies a plated through-hole pad touched at a trace endpoint. The JSON report records the first and last uncovered sampling stations in millimetres measured along the trace from its start. Each gap still requires electrical review.

`--workspace PATH` selects the location of shared inputs and outputs. If several native projects are available, pass a file with `--project`. The desktop lets you open that file directly. For an imported board, the config and report paths include a project key so same-named boards stay separate. Verification and recovery records go in `results/jobs/<project>/`. Exports default to `results/exports/`. Use `--output` to choose an explicit new destination for an export or project.

### Use AutoPCB from a phone

KiCad runs on Windows, macOS and Linux, so phones drive a computer that has it. On that computer run `autopcb serve --host ADDRESS`, using its LAN or Tailscale address. It prints a pairing link, `http://ADDRESS:8765/#token=...`, and draws the same link as a QR code when the `phone` extra is installed (`python -m pip install ".[phone]"`). Scan the QR code or open the link on an Android phone or iPhone. The page lists the workspace's projects, shows each board, runs verify, route preview, apply and draft export, and shows every job's result. Without `--host` the page is reachable only from the same computer.

The token travels in the link's fragment, which a browser never sends to the server, and in the `Authorization` header of every API call. The server rejects a token in the URL. `GET /api/info` needs no token and reports the product, version, API level and whether the request's token is right, so an app can check a server before it stores anything. Anyone holding the link can run jobs, so share it only over a network you trust: Tailscale encrypts the traffic, plain LAN HTTP does not.

[`apps/android`](apps/android/README.md) and [`apps/ios`](apps/ios/README.md) hold native shells that pair by scanning the code, keep the token in encrypted storage or the Keychain, and open the page in a view locked to the paired address.

### Evidence for a checked export

The `autopcb.project.json` profile uses `schema_version: 1` and an `entry` native filename. For checked export, record `high_speed_reviewed: true` and list each differential pair as two native net names, positive first. Use an empty `differential_pairs` list for a project reviewed as having no pairs. Set `reference_net` if the return has a name other than `GND`. Verify and export independently check ERC, DRC, and schematic agreement, then sample adjacent reference copper on the refilled PCB. The findings include trace locations, layer transitions, and any unreviewed scope.

A checked export requires passing native checks and an `electrical_review` pointer with the report's relative `file` and `sha256`. The report file can live in `data/projects/<project>/`. The version 1 review records the reviewer, the exact `sources` from verification excluding the profile and review file, and a `policy_sha256` of the profile without the review pointer. Compute the policy digest over compact UTF-8 JSON with sorted keys. The `checks` map includes passing `electrical` and applicable `reference_copper` checks, with a method and measurements for each. Use `checks.reference_copper.findings` to map each current finding ID to a documented engineering disposition. Draft exports include the same blockers and raw findings as verification. Production approval and hardware bring-up remain separate. The [USB breakout review](docs/usb-breakout-review.md) gives the launch dispositions and a bench bring-up procedure for the USB reference design.

## Validation

Software checks, KiCad verification and hardware evidence are reported separately and none stands in for another.

| Evidence | What it covers | Where it comes from |
| --- | --- | --- |
| Software checks | The unit and integration suite, the code-rules audit, `ruff format` and `ruff check`. | `python scripts/run.py --project . test` and `check code`. CI runs the suite on Linux, macOS and Windows for each pull request that changes code. |
| KiCad verification | ERC, DRC, schematic agreement and reference-copper sampling on a saved board. | `autopcb verify`, and `autopcb qualify` on the reference designs. Each run records its own results under `results/`, which Git ignores. |
| Hardware evidence | Behavior of a built board. | Pending. The bench bring-up procedure for the USB reference design is in the [USB breakout review](docs/usb-breakout-review.md). |

### Software tests

```bash
python scripts/run.py --project . test
python scripts/run.py --project . docs build-pdf
python -m pip install ".[desktop-build]"
python packaging/build_desktop.py --output results/distribution --bundle-only
autopcb qualify --output results/host-qualification
```

Pull requests that change code run the full suite with KiCad 10 on Windows, macOS and Linux (`.github/workflows/tests.yml`). The manual [Installers workflow](.github/workflows/installers.yml) builds the Windows installer, a Debian package and a macOS disk image from the selected commit and uploads each with its SHA-256. The installer build uses Inno Setup on Windows, a disk image on macOS, and a Debian package on Linux. The Linux job also installs the package and runs [the Linux check](packaging/check_linux_release.sh): `install` checks the files, desktop entry and `doctor`, `qualify` runs both reference projects, and `remove` checks that nothing is left behind. Set `CHECK_KICAD_PYTHON` when the system `python3` is not the interpreter that imports `pcbnew`. Run `qualify` from a fresh install on each target system. It creates both reference designs, verifies every guarded Apply against the saved PCB, and starts another preview when opens fall and rounds remain. A preview with no improvement ends that case's routing search. The report records the host, outline, toolchain, per-pass budget, saved opens and release blockers. The output directory retains the native projects, checks and draft packages. An incomplete qualification exits with status 2. Use `autopcb benchmark --baseline PATH` for a measured comparison under equal rule and resource budgets. The `--refinement` benchmark compares greedy routing with negotiated recovery under equal budgets. The original `scripts/run.py` commands remain available for existing KiCad projects.

`python scripts/run.py --project . check code` audits the class, header, docstring and README rules, including that each folder README's File Structure table lists the files Git tracks in that folder and that this README has the sections above in order. Its baseline of older breaches, `scripts/verification/code_rules_baseline.json`, may only shrink.

### Host qualification

To compare compact placement with the original catalog outline, run the same two circuits with identical budgets in fresh output directories.

```bash
autopcb qualify --output results/compact-control --rounds 65 --seconds 24 --memory-gib 2
autopcb qualify --output results/nominal-control --fixed-outlines --rounds 65 --seconds 24 --memory-gib 2
```

### Windows installer qualification

Copy the installer and [the validation script](packaging/check_windows_release.ps1) to a clean Windows 11 x64 VM. Use a non-elevated PowerShell session that permits local scripts, with the same standard user and evidence directory for both phases. Phase Install requires a VM with AutoPCB and KiCad absent. It checks setup, the Start menu entry, the installed CLI, and the missing KiCad diagnostic. Then install KiCad 10.0.6 in its standard location in the VM and run Phase Qualify.

```powershell
.\check_windows_release.ps1 -Phase Install -Installer .\AutoPCB-1.0.0.dev0-windows.exe -EvidenceDir '..\AutoPCB QA'

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`.git-blame-ignore-revs`](.git-blame-ignore-revs) | The formatting-only commits that `git blame` skips. | None | Git |
| [`.gitattributes`](.gitattributes) | Keeps the controlled release inputs byte-exact. | None | Git |
| [`.github/`](.github/workflows/README.md) | GitHub Actions workflows for tests, installers and the phone apps. | GitHub Actions | GitHub pull requests |
| [`.gitignore`](.gitignore) | Keeps KiCad editor state, Python caches, `results/`, `config/campaigns/` and local snapshots out of Git. | None | Git |
| [`AGENTS.md`](AGENTS.md) | The working rules for code, documentation, checks and Git. | None | Contributors and coding assistants |
| [`LICENSE`](LICENSE) | The proprietary license. | None | Everyone who uses the repository |
| [`apps/`](apps/README.md) | Native Android and iOS shells that pair with `autopcb serve`. | `scripts/autopcb/serve.py` | `.github/workflows/android.yml`, `.github/workflows/ios.yml` |
| [`config/`](config/README.md) | Routing defaults and the PDF build list. | None | `scripts/autopcb`, `scripts/utils` |
| [`data/`](data/README.md) | Project inputs: requirements, catalog and engineering evidence. | None | `scripts/autopcb`, `scripts/core` |
| [`docs/`](docs/README.md) | Product documentation, including the algorithm guide and its PDF. | `scripts/utils` builds the PDF | Readers |
| [`packaging/`](packaging/README.md) | The desktop bundle and installer build, and the Windows release check. | `scripts/` packages | `.github/workflows/installers.yml` |
| [`pyproject.toml`](pyproject.toml) | Package metadata, the `autopcb` and `autopcb-desktop` commands, optional dependency groups and package data. | setuptools | `pip`, the installer build |
| [`ruff.toml`](ruff.toml) | Formatter and linter settings: 88 columns, Python 3.9 target. | `ruff` | `ruff format`, `ruff check`, CI |
| [`scripts/`](scripts/README.md) | All AutoPCB code and its tests, one package per job. | KiCad 10 and its Python | Every command above |

Generated output goes to `results/`, which Git ignores.

## License

AutoPCB and its documentation are proprietary. Copyright (c) 2026 GRANDE Robots and Aaron John Sabu. All rights reserved. See [LICENSE](LICENSE). KiCad is installed separately under its own license.
