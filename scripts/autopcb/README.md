# autopcb

The product: the installed `autopcb` command, the Qt desktop app, and a small server that lets a phone or browser drive the same jobs. Requirements become a reviewable proposal and a native KiCad project; layout preview, apply, verification and export then run as source-bound jobs on private copies of the project.

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`__init__.py`](__init__.py) | The AutoPCB package, shared desktop and command-line workflows. | None | 7 modules here, `core` (1), `pcb` (1), `pcb/net_routing` (1) |
| [`__main__.py`](__main__.py) | Run the AutoPCB command with ``python -m autopcb``. | `cli` | None |
| [`benchmark.py`](benchmark.py) | Measure routing arms on isolated copies of one design. | `core/paths`, `core/project`, `layout`, `native`, `pcb/net_routing/autoroute_cli`, `private_copy`, `run`, `psutil` | `cli` |
| [`catalog.json`](catalog.json) | The offline engineering component catalog that `design.py` reads when a workspace has no `data/catalog.json`. | None | `design` |
| [`cli.py`](cli.py) | The installed AutoPCB command and its machine-readable job interface. | `__init__`, `benchmark`, `core/launcher`, `core/project`, `design`, `desktop`, `jobs`, `layout`, `native`, `placement_study`, 4 more | `__main__` |
| [`crowding.py`](crowding.py) | Pack a board's movable parts toward one point. | `core/kicad_campaign`, `pcb`, `pcb/footprint_pose`, `pcb/move_scope`, `pcb/net_routing/relocation`, `pcb/routability`, `placement`, `pcbnew` | `native_worker`, `verification/test_crowding.py` |
| [`design.py`](design.py) | Turn requirements and a local engineering catalog into a reviewable proposal. | `__init__`, `core/compat`, `core/kicad_sexpr` | `cli`, `jobs`, `qualification` |
| [`desktop.py`](desktop.py) | Local desktop surface for the jobs that the AutoPCB command runs. | `__init__`, `workspace`, `PySide6` | `cli`, `verification/test_desktop.py` |
| [`jobs.py`](jobs.py) | Source-bound jobs shared by the desktop app and the terminal. | `__init__`, `core/compat`, `core/export_helpers`, `core/kicad_sexpr`, `core/paths`, `core/project`, `design`, `native`, `pcb`, `policy`, 1 more | 6 modules here |
| [`layout.py`](layout.py) | Preview, compare and apply routing for one native project. | `core/paths`, `core/project`, `jobs`, `native`, `pcb/net_routing/autoroute_cli`, `policy`, `run` | `benchmark`, `cli`, `native_worker`, `placement_study`, `qualification` |
| [`native.py`](native.py) | Capability-checked, process-isolated adapter to KiCad. | `core/native_command`, `core/paths`, `core/toolchain` | 8 modules here |
| [`native_worker.py`](native_worker.py) | Run one native KiCad operation on files a product job selected. | `core/compat`, `core/kicad_sexpr`, `core/paths`, `core/pcb_io`, `core/project`, `crowding`, `layout`, `pair_seed`, `pcb/checkpoints`, `pcb/net_routing/controller`, 6 more | Command line |
| [`pair_seed.py`](pair_seed.py) | Seed a short, unbranched differential pair while creating a catalog design. | `core/compat`, `core/project`, `native`, `pcb`, `pcb/freeroute_delta`, `pcb/net_routing/protection`, `pcb/net_routing/reference`, `pcb/net_routing/style`, `pcb/route_native`, `pcb/route_optimizer`, 1 more | `native_worker` |
| [`placement.py`](placement.py) | Slide parts together and fit a rectangular outline around them. | `core/compat` | `crowding`, `native_worker` |
| [`placement_study.py`](placement_study.py) | Compare fixed placement with permitted moves under one search budget. | `__init__`, `core/paths`, `core/project`, `jobs`, `layout`, `native`, `private_copy` | `cli`, `verification/test_placement_study.py` |
| [`policy.py`](policy.py) | Compile the routing defaults and native board limits into a project policy. | None | `jobs`, `layout` |
| [`private_copy.py`](private_copy.py) | A private copy of a native project with its reviewed routing inputs. | `core/export_helpers`, `core/paths`, `core/project`, `jobs` | `benchmark`, `placement_study`, `verification/test_private_copy.py` |
| [`qualification.py`](qualification.py) | Exercise the reference projects on the installed native toolchain. | `__init__`, `core/export_helpers`, `core/paths`, `core/project`, `design`, `jobs`, `layout`, `native`, `release`, `workspace` | `cli` |
| [`research.py`](research.py) | Fetch source documents only through an explicit local job. | `jobs` | `cli` |
| [`serve.html`](serve.html) | The page that `serve.py` serves to a phone or browser. | None | `serve` |
| [`serve.py`](serve.py) | Serve AutoPCB to a phone or any browser that can reach this computer. | `__init__`, `core/toolchain`, `native`, `pcb`, `workspace` | `cli`, `verification/test_serve.py` |
| [`workspace.py`](workspace.py) | Standard locations for AutoPCB inputs, policy and generated projects. | `core/paths`, `core/project` | `cli`, `desktop`, `qualification`, `serve` |
