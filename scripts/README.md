# scripts

The AutoPCB code, one package per job, and `run.py`, the entry point for the original project tools. The folder is the import root: packages import each other as `core.paths`, `pcb.route_cli` and so on.

Run a tool from the repository root with `python scripts/run.py --project . <command>`; `--help` lists the commands. The installed `autopcb` command and the desktop app come from the `autopcb` package.

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`autopcb/`](autopcb/README.md) | The installed `autopcb` command, the desktop app and the phone server: proposals, layout preview and apply, placement study, qualification. | `core`, `pcb`, `release`, `run`, `sch`, `PySide6`, `pcbnew`, `psutil` | `core`, `pcb` |
| [`core/`](core/README.md) | Shared helpers: project and path discovery, guarded writes, native command execution, console output, edit campaigns and the command dispatcher. | `autopcb`, `pcb`, `release`, `review`, `sch`, `utils`, `verification`, `pcbnew`, `wx` | `autopcb`, `pcb`, `release`, `review`, `run`, `utils`, `verification` |
| [`pcb/`](pcb/README.md) | PCB routing and board-editing tools; `net_routing/` is the autoroute engine. | `autopcb`, `core`, `review`, `run`, `sch`, `bga_fanout`, `pcbnew`, `rust_alloc` | `autopcb`, `core`, `release`, `review`, `verification` |
| [`release/`](release/README.md) | Factory review set, factory documents and the technical attachment. | `core`, `pcb`, `sch`, `pcbnew` | `autopcb`, `core` |
| [`review/`](review/README.md) | Review gallery rendering, packaging and routing-progress records. | `core`, `pcb`, `sch`, `PIL`, `pcbnew`, `pypdfium2` | `core`, `pcb` |
| [`run.py`](run.py) | Run AutoPCB project tools without leaving Python caches in the project. | `core/launcher` | Command line, `autopcb`, `pcb` |
| [`sch/`](sch/README.md) | Schematic presentation tools. | None | `autopcb`, `core`, `pcb`, `release`, `review`, `verification` |
| [`utils/`](utils/README.md) | Documentation build tools: Markdown to PDF. | `core`, `PySide6`, `fitz`, `markdown_it` | `core` |
| [`verification/`](verification/README.md) | Unit tests and audit tools, including the code-rules audit and its baseline. | `autopcb`, `core`, `pcb`, `release`, `review`, `run`, `sch`, `utils`, `PIL`, `PySide6`, `pcbnew`, `wx` | `core` |
