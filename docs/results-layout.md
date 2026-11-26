# AutoPCB results layout

This page lists the folders and files AutoPCB writes under a workspace's `results/` directory, which command writes each, and where the output folders you choose end up. File names were observed by running the commands in a throwaway workspace with KiCad 10.0.8 on Linux. The exception is `lookup-sources`, which needs network access and is described from the code. The page describes where software output goes. It makes no claim about hardware or manufacturing.

## Workspace

A workspace is one folder with `config/`, `data/` and `results/` beneath it. It is the folder given with `--workspace`, or the current folder. Commands never write outside it except to the paths you pass with `--output` ([`workspace.py`](../scripts/autopcb/workspace.py), [`paths.py`](../scripts/core/paths.py)).

In this repository `.gitignore` lists `/results/` and `/config/campaigns/`. The patterns are anchored to the repository root, so they cover a workspace only when the workspace is the repository root. `config/projects/`, where the routing policies live, is not ignored.

Several folders are named by a project key. The key is the board file name without its suffix when the board sits at `results/projects/<name>/`. For a board anywhere else it is the sanitized name, a hyphen and 16 hexadecimal digits of a hash of the board path, for example `My-Board-v2-2edcae959b804711`. The same key names `config/projects/<key>/`, `data/projects/<key>/` and `config/campaigns/<key>/`.

```
results/
  proposals/<proposal_id>.json
  sources/<proposal_id>/source-NN.<pdf|html|bin>, sources.json
  projects/<name>/<name>.kicad_pro and the rest of the native project
  jobs/<project>/
    verification.json
    autoroute/<run_id>.json, active.lock, preimages/
  exports/<project>-draft/ and <project>-checked/
```

## What each command writes

| Command | Writes | Location |
| --- | --- | --- |
| `propose` | The proposal | `results/proposals/<proposal_id>.json`, or the file given with `--output` |
| `lookup-sources` | Downloaded source documents | `results/sources/<proposal_id>/`, or the folder given with `--output` |
| `create` | A native project and its routing policy | `results/projects/<name>/` or the folder given with `--output`, and `config/projects/<key>/autopcb.json` |
| `configure-layout` | The routing policy | `config/projects/<key>/autopcb.json` |
| `verify` | The verification report | `results/jobs/<project>/verification.json` |
| `route` | A run record and a lock file | `results/jobs/<project>/autoroute/` |
| `apply` | The board, a recovery copy and the run record | `results/projects/<name>/` board file, and `results/jobs/<project>/autoroute/` |
| `export` | A package folder | `results/exports/<project>-draft/` or `-checked/`, or the folder given with `--output` |
| `qualify` | A complete workspace of its own | The new folder given with `--output` |
| `placement-study` | Nothing in the workspace. The report goes to standard output and, with `--output`, to a file. | The file given with `--output` |
| `benchmark` | Nothing. The command has no `--output`. The report goes to standard output. | None |
| `doctor` | Nothing in the workspace. With `--output`, a JSON report. | The file given with `--output` |
| `discover`, `inspect`, `compare` | Nothing | None |

`serve` and `desktop` run these same commands, so their output lands in the same places. The desktop's Save requirements button also writes `data/requirements.txt`. `campaign` writes edit campaigns to `config/campaigns/<key>/`, outside `results/`.

Every command prints one JSON report to standard output. It exits with 0 for a finished action, 2 for a blocked, stalled, interrupted, failed or incomplete one, and 130 for a cancelled one ([`cli.py`](../scripts/autopcb/cli.py)).

## results/proposals/

`propose` writes `results/proposals/<proposal_id>.json`. The proposal ID is the SHA-256, as 64 hexadecimal digits, of the proposal's canonical JSON text ([`design.py`](../scripts/autopcb/design.py)). Running `propose` again with the same requirements finds the same file and leaves it. If a file with that name holds a different proposal, the command fails with "Stored proposal differs from the current result".

With `--output FILE` the proposal goes to that file instead and nothing is written under `results/proposals/`. The file must not exist and its folder must. `--schematic-draft FILE` also writes a draft schematic to a new file, only when the proposal status is `proposed`.

## results/sources/

`lookup-sources PROPOSAL` downloads the public source URL of each part in a proposal. It writes `results/sources/<proposal_id>/` (or the folder given with `--output`, which must be new under an existing folder) with:

- `source-01.pdf`, `source-02.html` and so on, numbered over the sorted source URLs. The suffix is `.pdf` for a PDF, `.html` for text that starts with `<`, and `.bin` otherwise.
- `sources.json`, the `source_lookup` report with each file name, URL, final URL and SHA-256.

A proposal needs between 1 and 24 distinct sources. If anything fails, the folder is not created ([`research.py`](../scripts/autopcb/research.py)).

## results/projects/\<name\>/

`create` writes the native project here, with `--name NAME` choosing the folder name. The default name is the proposal's design name. A name has 1 to 64 characters, starts with a letter or digit, and uses letters, digits, spaces, dots, hyphens and underscores. It may not end in a space or a dot. The folder must not exist. It is built in a hidden staging folder (`.autopcb-stage-*`) and renamed on success, so a failed build leaves nothing behind and removes the policy it wrote ([`jobs.py`](../scripts/autopcb/jobs.py)).

Observed files, with `<name>` the folder name:

| File | What it is |
| --- | --- |
| `<name>.kicad_pro`, `<name>.kicad_pcb`, `<name>.kicad_sch`, `<name>.kicad_dru` | The native project, board, schematic and design rules. |
| `<name>.kicad_prl` | KiCad's local state file for the project. AutoPCB code does not mention it, and `.gitignore` ignores it. |
| `autopcb.project.json` | The project profile: proposal ID, requirements, supply voltage, copper layer count, differential pairs, assumptions and `high_speed_reviewed`. |
| `design-proposal.json` | A copy of the approved proposal. |
| `fp-lib-table`, `sym-lib-table` | Library tables. |
| `libraries/AutoPCB.kicad_sym` | The generated symbols. |

`apply` later replaces the `.kicad_pcb` file here after its checks pass. `create` writes the policy outside this folder, to `config/projects/<key>/autopcb.json`.

## results/jobs/\<project\>/

This folder holds the records of jobs run on one project. It is `ProjectStorage.results` in [`paths.py`](../scripts/core/paths.py).

### verification.json

`verify` writes `results/jobs/<project>/verification.json` and replaces it in one step on every run. It holds the source hashes, the native ERC and DRC findings, the reference screen, the blockers and the toolchain. Its `status` is `native_passed` or `blocked`. `hardware_validation` is `pending` and `production_approved` is `false`. The command's own output adds a `report` field with the path. `qualify` also writes `initial-verification.json` here, the report taken before routing.

### autoroute/

`route` writes `results/jobs/<project>/autoroute/`. The folder holds:

| Entry | What it is |
| --- | --- |
| `<run_id>.json` | The run record. The ID is 32 lowercase hexadecimal digits. The `route` report names the file in `record`. |
| `active.lock` | A lock file. A run holds it while it writes. A second run, or a repair tool, that still finds it held when its wait time ends stops with "Another AutoPCB run is active". |
| `preimages/working-save.json` | The recovery copy that `apply` writes before it changes the board. |
| `preimages/net-alias-<32 hex digits>.json` | The recovery copy of a net-alias reconciliation, written by `run.py pcb sync-net-aliases`. |

`route --resume RUN_ID` continues the same record. `route --renew RUN_ID` rechecks the preview and rebuilds its queue in a new record with a new ID and leaves the old one. The record is replaced atomically through a temporary file named `.autoroute-*.tmp` in the same folder ([`runner.py`](../scripts/pcb/net_routing/runner.py)).

#### Run record

The record is one JSON file. Its top-level keys include `schema` (1), `run_id`, `status`, `publication_enabled` (always `false`), `keep_placement`, `saved_board_opens`, `best_preview_opens`, `accepted_preview_batches`, `attempted_rounds`, `blocked_nets`, `baseline_blockers`, `queue`, `last_result`, `reference`, `elapsed_seconds`, `checkpoint`, `resource_limits`, `search_limits`, `progress_samples` and `target_attempts`. `status` is one of:

| Status | Meaning |
| --- | --- |
| `running` | A run holds the lock. |
| `paused` | The round limit was reached and work remains in `queue`. |
| `preview_zero_opens` | The best preview has no open connections and no work is queued. |
| `stalled` | The queue is empty and opens remain. `route` exits with 2. |
| `interrupted` | The record said `running` but no process holds the lock. The last saved checkpoint is kept. |

The `route` command exits with 0 for `paused` and `preview_zero_opens` and with 2 otherwise ([`autoroute_cli.py`](../scripts/pcb/net_routing/autoroute_cli.py)).

#### Checkpoints

The `checkpoint` object inside the record is the retained candidate board. It holds `candidate_sha256`, `candidate_zlib_base64` (the whole candidate board, zlib-compressed and base64 encoded, at most 128 MiB before compression), `inputs` (the SHA-256 of every source file), `tools` (the SHA-256 of the tool files) and `policy` (the SHA-256 of the routing policy). A resume refuses a checkpoint whose inputs or policy differ from the files on disk. A renew accepts changed tool hashes and revalidates the board ([`controller.py`](../scripts/pcb/net_routing/controller.py)).

`apply` is the only step that changes the board. It rechecks the files, the protected geometry and the KiCad editor lock, writes `preimages/working-save.json` with `kind` `pre_working_pcb_save` and the previous board as a checkpoint, then saves. The file is replaced on each apply, so it holds the board from just before the latest apply.

## results/exports/

`export` writes a package folder. The default is `results/exports/<project>-draft` with `--draft` and `results/exports/<project>-checked` without it. `--output FOLDER` chooses another new folder. The folder must not exist, and a second export to the same name fails with "Select a new destination under an existing directory". The package is built in a hidden staging folder and renamed on success. Without `--draft` the export stops at any blocker with "Manufacturing export blocked" and creates no folder ([`jobs.py`](../scripts/autopcb/jobs.py)).

Observed contents:

| Entry | What it is |
| --- | --- |
| `gerbers/<board>-<layer>.<ext>` | One KiCad plot per copper layer, mask, paste and silkscreen side, and the board edge, plus `<board>-job.gbrjob`. |
| `drills/<board>.drl` | The Excellon drill file. |
| `bom.csv` | The bill of materials with the fields Reference, Value, Footprint, Manufacturer, MPN and DNP. |
| `placements.csv` | Part positions in mm, without parts marked DNP. |
| `schematic.pdf`, `assembly.pdf` | The schematic and an assembly drawing. |
| `evidence/erc.json`, `evidence/drc.json`, `evidence/reference.json` | The native check reports and the reference screen. |
| `evidence/electrical.json` | Present only when the project's electrical review report is accepted. |
| `manifest.json` | The manifest. |

`manifest.json` has `kind` `manufacturing_package`, `status` of `draft` or `design_checked`, the `blockers`, the `inputs` hashes, `manufacturing_board_sha256`, the `toolchain` and an `artifacts` map from each file's path to its SHA-256. `hardware_validation` is `pending` and `production_approved` is `false`. The manifest does not list itself.

## Output folders you choose

### qualify

`qualify --output FOLDER` creates `FOLDER` and fails if it exists. The folder is a complete workspace, and the command runs both reference designs, `Temperature Sensor` and `USB Breakout`, inside it ([`qualification.py`](../scripts/autopcb/qualification.py)).

```
FOLDER/
  qualification.json
  data/<name>.txt
  config/projects/<name>/autopcb.json
  results/projects/<name>/              as above
  results/jobs/<name>/initial-verification.json, verification.json
  results/jobs/<name>/autoroute/        run records, lock, preimages
  results/exports/<name>-draft/         as above
  results/exports/<name>-checked/       only when the checked export is not blocked
```

`qualification.json` is written when the run starts with `status` `running` and replaced after every stage. The final `status` is `reference_checks_passed`, `qualification_incomplete`, `blocked` or `cancelled`. It records the host, the toolchain, the search budget, each case and its outcome. `hardware_validation` is `pending` and `production_approved` is `false`. The command exits with 2 for `qualification_incomplete` and `blocked`, and 130 for `cancelled`.

### placement-study

The study routes private copies of the board in a temporary folder and removes them afterwards. The report goes to standard output. `--output FILE` also writes it to a new JSON file and fails if the file exists or its folder does not. The report `kind` is `placement_study`, or `placement_variant_study` with `--variants`. `--seed` sets the first packing seed.

### benchmark

`benchmark` has no `--output`. Each arm runs on a private copy in a temporary folder that is removed afterwards. The `routing_benchmark` report with wall time, sampled peak memory in bytes and open counts goes to standard output only.

### Other explicit paths

`propose --output FILE`, `doctor --output FILE` and `placement-study --output FILE` write one new JSON file each. `create --output FOLDER` and `export --output FOLDER` create a new folder. `lookup-sources --output FOLDER` creates a new folder under an existing one. A file or folder that already exists is never overwritten.

## Temporary files

While a command runs you may see these. They are removed or replaced when it finishes.

| Name | Where | Written by |
| --- | --- | --- |
| `.autopcb-stage-*` | Beside the target folder | `create`, `export`, `lookup-sources` |
| `.autopcb-report-*` | Beside the target report | `verify`, `qualify` |
| `.autoroute-*.tmp` | `results/jobs/<project>/autoroute/` | `route` |

## Tools that write beside the project

The tools run through `scripts/run.py` do not use the workspace folders. When no workspace is bound to the process they use the legacy layout, which is `autopcb/` beside the KiCad files if that folder exists, otherwise the project folder itself, followed by `results/`. They write, for example:

- `results/autoroute/<run_id>.json` and `results/autoroute/preimages/` from the autoroute, rework and repair tools.
- `results/autopcb/<YYYYmmdd_HHMMSS_ffffff>/plan.json` and `checkpoints/NNNN.json` from `run.py pcb checkpoints plan` and `run`. A sequential preview adds `sequence_preview.json` beside `plan.json` ([`checkpoints.py`](../scripts/pcb/checkpoints.py), [`sequence.py`](../scripts/pcb/net_routing/sequence.py)).
- `results/width_repair.json` from `run.py pcb repair-widths`.

Reading an earlier result checks the workspace folder first and uses the legacy folder only when it alone has the file.

The release tools in [`scripts/release/`](../scripts/release/) write to a path you pass. `run.py release build --out FOLDER` needs a new or empty folder outside the project and creates `jlcpcb/` in it with `gerbers`, `assembly`, `evidence`, `sourcing` and `views` folders. `run.py release technical --out FILE` writes a new archive and `FILE` with the suffix replaced by `.receipt.json`. `run.py release stage-inputs` writes `data/release_inputs/` under the project's asset folder.
