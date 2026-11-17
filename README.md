# AutoPCB

AutoPCB provides repeatable workflows for KiCad schematics and PCBs. Use it to audit saved designs, plan routes and edits on temporary copies, generate review artifacts, and prepare checked manufacturing exports. KiCad remains the source of the schematic, layout, and design rules.

## Get started

From the directory containing your KiCad project, clone the repository as `autopcb/`.

```bash
git clone https://github.com/GRANDE-Robots/autopcb.git autopcb
python -B autopcb/scripts/run.py --help
python -B autopcb/scripts/run.py status
python -B autopcb/scripts/run.py check tools
```

Keep the saved KiCad project, schematic, board, custom rules, and library tables in the project directory. Keep custom libraries where the KiCad library tables resolve them. When the checkout is named `autopcb`, the launcher defaults to its parent directory. Use `--project` before the command group to select a different project directory.

```bash
python -B autopcb/scripts/run.py --project "/path/to/kicad-project" status
```

### Requirements

| Workflow | Tooling |
| --- | --- |
| Command launcher | Python 3.11 or later. |
| Native board operations | KiCad 10 and its Python runtime with `pcbnew`. Set `KICAD_PYTHON` or pass `--kicad-python /path/to/python` before the command group when needed. |
| Native checks and exports | `kicad-cli` from the KiCad installation. |
| Code and review tools | Ruff for the code audit. Pillow and either pypdfium2 or Poppler for review rasterization. |
| Documentation PDFs | `markdown-it-py`, LaTeX, `dvisvgm`, and `pdfinfo`. The builder prefers PySide6 WebEngine with PyMuPDF and can fall back to Chrome or Edge. |
| Freerouting trials | Java and a Freerouting JAR. Other optional engines have their own setup and command help. |

### Project compatibility

Some commands currently use the fixed `v4.1` basename for native KiCad files. `--project` selects the directory. Check the selected action's help for explicit input arguments if your files use a different name. Check source discovery with `status` before starting a workflow. [Project path implementation](scripts/core/project.py)

The configuration and design data in this checkout describe an existing board. Use them as examples of the required inputs. Keep the routing policy, rules, and release tables synchronized with the project you are working on.

## Set the design inputs

| Input | Define for each board |
| --- | --- |
| Native KiCad files | Saved connectivity, board geometry, layer stack, padstacks, footprints, net classes, and custom rules. |
| `config/autopcb.json` | Routing scope and priorities, allowed layers and via sizes, search settings, and protected items. Configure the nets you intend to route. |
| Electrical constraints | Load cases, voltage drop, current and temperature limits, power branches, and any special pad escapes. Keep the design's budgets and supporting evidence in `design/`. |
| Signal integrity | Differential pair membership, target impedance, width and gap by layer, and required reference copper. Document the actual stackup and its geometry in `design/STACKUP_CURRENT.json` where applicable. |
| Mechanical and assembly inputs | Keepouts, clearances, placement constraints, connector and harness interfaces, BOM identities, assembly side and orientation, and supplier requirements. |
| Release inputs | Reviewed tables and their manifest in `design/release_inputs/`. Reconcile these with the saved KiCad inputs and project-specific release policy. |

[Historical requirements example](docs/requirements.md) · [Local engineering inputs](design/README.md)

## Inspect the saved design

```bash
python -B autopcb/scripts/run.py check erc
python -B autopcb/scripts/run.py check board
python -B autopcb/scripts/run.py check nets
python -B autopcb/scripts/run.py check routing
```

The `check` group includes library and footprint audits, copper island and routing inspections, native board checks, schematic-to-board net comparison, and release consistency. Run a specific action with `--help` for its inputs and options. Check results reflect the saved files and the checks that ran.

## Plan and preview routing

Use a routing scenario for a particular route, a batch, or a set of nets. Relative `--config` paths resolve from the directory where you run the command.

```bash
python -B autopcb/scripts/run.py pcb plan-route --config /path/to/route.json
python -B autopcb/scripts/run.py pcb plan-batch --config /path/to/routes.json --validate
python -B autopcb/scripts/run.py pcb plan-nets --config /path/to/nets.json --validate
```

For a bounded preview, checkpoint routing carries accepted candidates forward on a temporary board. Resume using the sequence file printed by the previous run.

```bash
python -B autopcb/scripts/run.py pcb checkpoints run --sequential --limit 3
python -B autopcb/scripts/run.py pcb checkpoints run --sequential --resume /path/to/sequence_preview.json
```

The board-wide controller prepares the saved design, searches the configured scope, and records private previews. Board-wide publication through `--apply` is currently disabled.

```bash
python -B autopcb/scripts/run.py pcb autoroute prepare
python -B autopcb/scripts/run.py pcb autoroute run --workers 2 --max-rounds 10
python -B autopcb/scripts/run.py pcb autoroute status --run RUN_ID
python -B autopcb/scripts/run.py pcb autoroute verify
```

Use `run --resume RUN_ID` for a source-matched run. Use `run --renew RUN_ID` to revalidate a retained candidate and rebuild its queue after a routing-code change. Start a new run after changing the saved design, its libraries, or routing policy.

Bound the search with `--max-rounds` and `--seconds-per-batch`. A blocked memory check preserves the last accepted candidate. Use `--memory-gib` to set an explicit budget. `--verbose` adds diagnostic detail, and supported `--json` output goes to stdout for capture by other tools.

A search reports a candidate, an unresolved target, or a resource or validation failure. Search bounds and incomplete checks can leave targets unresolved. The [routing formulation](docs/algorithm.md) describes checkpoints, candidate screening, local repair, protection rules, and known limits.

### Differential pairs and external routing

`pcb autoroute pair-probe` runs a paired-routing trial on a disposable board and reports geometry, native DRC, and sampled reference coverage. `pcb freeroute-preview` and related commands support external-engine trials. Inspect the action help for the available engines and resource controls.

When your checkout includes `pcb impedance`, the action compares the saved board with the configured stackup, pair profiles, and explicit USB pin mappings where provided by the profile. It measures overlapping straight runs and sampled ground coverage. Use `--json` for detailed measurements, `--check-reference` to fail when required runs or reference coverage are missing, and `--emit-rules` to print the managed KiCad rule block for review. Assess launches, vias, end-to-end skew, and manufactured impedance separately.

## Plan and review edits

Use `sch` (alias `schematic`) for presentation, label and stub cleanup, and symbol synchronization. Use `pcb` for footprint and silkscreen adjustments and focused copper repairs. Check the action's preview and write options.

| Task | Actions |
| --- | --- |
| Schematic placement and labels | `sch style`, `sch sections`, `sch labels-deduplicate`, and `sch sync-libraries`. |
| Board cleanup | `pcb clean-silkscreen`, `pcb repair-drc`, `pcb zone-stitch`, and `pcb via-prune`. |
| Power copper checks and edits | `pcb branch-widths` and `pcb repair-widths`. Review exceptions at individual branches, terminals, or pad escapes against the project rules. |
| Focused placement | `pcb move`, `pcb move-text`, and `pcb replace-connector`. |
| Source-bound changes | `campaign preview` and `campaign apply` for edits. `library preview` and `library apply` for library changes. |

```bash
python -B autopcb/scripts/run.py sch style --check
python -B autopcb/scripts/run.py campaign new --name "Test point grid"
python -B autopcb/scripts/run.py campaign preview CAMPAIGN_FILE --diff
```

Campaign recipes are generated in `config/campaigns/`. Review the preview before applying an edit. Source-bound write operations check the saved inputs and can block a write if the source changed or KiCad has the file open.

## Review and release

```bash
python -B autopcb/scripts/run.py review refresh
python -B autopcb/scripts/run.py review progress --redraw
python -B autopcb/scripts/run.py check release
```

Review commands generate the configured schematic, copper, silkscreen, and board views for the saved project. `review progress` can record new saved-board measurements. `--redraw` renders existing measurements. Preview measurements have their own records.

The `release` group stages and checks controlled manufacturing inputs and can export Gerbers, drill files, assembly data, and technical attachments. `release build` requires a reviewed input manifest and passing checks. Verify mechanical fit, electrical limits, fabrication rules, and assembly details against the actual design and manufacturer before authorizing production.

## Develop and test

```bash
python -B autopcb/scripts/run.py check code
python -B autopcb/scripts/run.py test
python -B autopcb/scripts/run.py docs build-pdf
```

The test runner uses KiCad Python for native regression tests and a Python with `markdown-it-py` for document tests. Use `test --document-python /path/to/python` to select the document runtime. The PDF builder follows `config/pdf_build.json`.

| Location | Purpose |
| --- | --- |
| `scripts/run.py` | Main command entry point. |
| `scripts/core/` | Command dispatch, project paths, and shared editing and validation logic. |
| `scripts/sch/` and `scripts/pcb/` | Schematic and layout workflows. |
| `scripts/verification/` | Audits and regression tests. |
| `scripts/review/` and `scripts/release/` | Review artifacts and manufacturing exports. |
| `config/`, `design/`, and `docs/` | Tool settings, project inputs, and documentation. |
| `config/campaigns/` and `results/` | Local campaign recipes, previews, reports, and checkpoints, ignored by Git. |
| `../deliverables/review/` | Generated gallery and routing progress in the KiCad project. |

[Algorithm and implementation limits](docs/algorithm.md) · [Documentation index](docs/README.md)
