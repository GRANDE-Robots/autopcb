# sch

Schematic presentation tools for KiCad schematics: labels, pin names, indentation, symbol style and symbol libraries. Each runs as `python -m sch.<name>` from `scripts/`.

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`__init__.py`](__init__.py) | The schematic presentation tools, run as ``python -m sch.<name>``. | None | `autopcb` (1), `core` (3), `pcb` (1), `release` (1), `review` (1), `verification` (1) |
| [`deduplicate_labels.py`](deduplicate_labels.py) | Remove redundant same-net labels, keeping connectivity. | `schematic_io`, `standardize_block_symbols` | `orient_passive_labels`, `restore_endpoint_labels`, `core` (1) |
| [`layout_sections.py`](layout_sections.py) | Make the one-sheet Jessie schematic read as a section grid. | `schematic_io` | `core` (1) |
| [`normalize_indentation.py`](normalize_indentation.py) | Re-indent KiCad S-expression files by nesting depth. | `schematic_io` | `core` (1) |
| [`normalize_pin_names.py`](normalize_pin_names.py) | Give currently generic J/U terminal text concise, real names. | `schematic_io`, `standardize_block_symbols` | `core` (1) |
| [`normalize_presentation.py`](normalize_presentation.py) | Normalize free-text typography without touching nets. | `schematic_io` | `core` (1) |
| [`orient_passive_labels.py`](orient_passive_labels.py) | Point labels outward on the right of horizontal R/C parts. | `deduplicate_labels`, `schematic_io`, `standardize_block_symbols` | `core` (1) |
| [`prune_dead_stubs.py`](prune_dead_stubs.py) | Remove dead graphical wire stubs and their now-isolated labels. | `schematic_io`, `standardize_block_symbols` | `core` (1) |
| [`repair_no_connects.py`](repair_no_connects.py) | Repair no-connect markers displaced one grid cell from a pin. | `schematic_io`, `standardize_block_symbols` | `core` (1), `pcb/net_routing` (1) |
| [`resolve_label_collisions.py`](resolve_label_collisions.py) | Clear left-facing labels off neighboring block bodies. | `schematic_io`, `standardize_block_symbols` | `core` (1) |
| [`restore_endpoint_labels.py`](restore_endpoint_labels.py) | Tag bare wire ends without duplicating visible labels. | `deduplicate_labels`, `schematic_io`, `standardize_block_symbols` | `core` (1) |
| [`schematic_io.py`](schematic_io.py) | Exact reads, atomic writes and digests of KiCad text files. | None | 13 modules here |
| [`standardize_block_symbols.py`](standardize_block_symbols.py) | Convert J/U/block-D symbols to a one-sided style. | `schematic_io` | 8 modules here, `core` (1), `pcb/net_routing` (1) |
| [`sync_single_symbol.py`](sync_single_symbol.py) | Copy one normalized embedded symbol back to its library. | `schematic_io` | `core` (1) |
| [`sync_symbol_libraries.py`](sync_symbol_libraries.py) | Copy accepted embedded artwork into project libraries. | `schematic_io`, `standardize_block_symbols` | `core` (1) |
