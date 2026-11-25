# release

Builds the factory review set from native CAD: it normalizes the exporter's IPC-D-356 netlist, writes factory instructions and builds the technical attachment, each bound to the sources that produced it.

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`__init__.py`](__init__.py) | The release package, which builds the factory review set, its documents and the technical attachment. | None | `autopcb` (1), `core` (1) |
| [`build_technical_attachment.py`](build_technical_attachment.py) | Build one compact, source-bound JLC review attachment; no CAD or order writes. | `core/export_helpers`, `core/paths` | `core` (1) |
| [`generate_factory_documents.py`](generate_factory_documents.py) | Generate source-bound factory instructions. Run by generate_factory_review after native exports. | `core/export_helpers`, `core/paths` | `generate_factory_review` |
| [`generate_factory_review.py`](generate_factory_review.py) | Build a fresh, count-independent manufacturing review set from native CAD. | `core/export_helpers`, `core/fabrication_supplements`, `core/factory_scope`, `core/paths`, `core/pcb_io`, `core/toolchain`, `generate_factory_documents`, `normalize_ipc_netlist`, `pcb`, `sch`, 1 more | `core` (1) |
| [`normalize_ipc_netlist.py`](normalize_ipc_netlist.py) | Validate and normalize the installed exporter's IPC-D-356 output against native CAD. | `core/export_helpers`, `core/pcb_io`, `pcbnew` | `generate_factory_review` |
| [`stage_release_inputs.py`](stage_release_inputs.py) | One-time explicit migration of controlled inputs out of generated outputs. | `core/export_helpers`, `core/paths` | `core` (1) |
