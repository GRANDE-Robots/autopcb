# core

The helpers the other packages share: finding a native KiCad project and its support files, guarded and atomic board writes, bounded native-command execution, console output, source-bound edit campaigns for schematics and libraries, and the `launcher` that dispatches the `run.py` commands.

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`__init__.py`](__init__.py) | The helpers every other package shares. | None | `pcb` (1) |
| [`assembly_rotation.py`](assembly_rotation.py) | Qualified JLC catalog-angle adjustments; native positions/CAD stay intact. | `export_helpers`, `paths` | `factory_scope`, `verification/test_assembly_rotation.py` |
| [`campaign_cli.py`](campaign_cli.py) | Create, preview, and apply source-bound KiCad edit campaigns. | `kicad_campaign`, `paths`, `pcb/route_validation`, `pcb_io`, `project`, `sch`, `toolchain`, `pcbnew` | `launcher` |
| [`compat.py`](compat.py) | Stand-ins for Python 3.10+ features, since KiCad's macOS bundle ships Python 3.9. | None | `launcher`, `library_campaign_cli`, `library_edits`, `pcb_io`, `autopcb` (5), `pcb` (12), `pcb/net_routing` (13), `review` (1), `utils` (1), `verification` (2) |
| [`console.py`](console.py) | Readable, bounded terminal output; raw records are an explicit opt-in. | None | `launcher`, `pcb/net_routing` (2), `review` (3), `utils` (1) |
| [`export_helpers.py`](export_helpers.py) | Small deterministic export helpers. No CAD writes or purchases. | `paths` | `assembly_rotation`, `fabrication_supplements`, `factory_scope`, `launcher`, `project`, `autopcb` (3), `release` (5), `review` (3), `verification` (1) |
| [`fabrication_supplements.py`](fabrication_supplements.py) | Add reviewed, native-bound fabrication instructions to the generated CAM ZIP. | `export_helpers`, `paths` | `release` (1) |
| [`factory_scope.py`](factory_scope.py) | Derive a complete system scope and an explicit JLCPCB assembly scope. | `assembly_rotation`, `export_helpers`, `paths`, `pcbnew` | `release` (1), `verification/test_factory_scope.py` |
| [`kicad_campaign.py`](kicad_campaign.py) | Plan precise, source-preserving edits described by JSON campaigns. | `kicad_sexpr`, `paths`, `pcb`, `project` | `campaign_cli`, `library_campaign_cli`, `library_edits`, `library_validation`, `autopcb` (1), `pcb` (9), `pcb/net_routing` (8) |
| [`kicad_sexpr.py`](kicad_sexpr.py) | Read KiCad S-expressions while preserving the original source text. | None | `kicad_campaign`, `library_edits`, `library_validation`, `project`, `autopcb` (3), `pcb` (14), `pcb/net_routing` (10), `verification` (2) |
| [`launcher.py`](launcher.py) | Dispatch AutoPCB checks, review tools, and guarded editing utilities. | `campaign_cli`, `compat`, `console`, `export_helpers`, `library_campaign_cli`, `paths`, `pcb`, `pcb/branch_widths`, `pcb/checkpoints`, `pcb/drag_routed_footprints`, 63 more | `autopcb` (1), `run` (1), `verification/test_launcher.py` |
| [`library_campaign_cli.py`](library_campaign_cli.py) | Preview and explicitly apply source-bound local KiCad library campaigns. | `compat`, `kicad_campaign`, `library_edits`, `library_validation`, `toolchain` | `launcher` |
| [`library_edits.py`](library_edits.py) | Plan library edits and fingerprint their design dependencies. | `compat`, `kicad_campaign`, `kicad_sexpr`, `paths` | `library_campaign_cli` |
| [`library_validation.py`](library_validation.py) | Validate a planned library file using the installed KiCad CLI. | `kicad_campaign`, `kicad_sexpr`, `toolchain` | `library_campaign_cli` |
| [`native_command.py`](native_command.py) | Run headless native tools with complete, bounded failure diagnostics. | None | `autopcb` (1), `pcb` (1), `pcb/net_routing` (1), `verification/test_native_command.py` |
| [`paths.py`](paths.py) | Locate AutoPCB support files beside a native KiCad project. | `autopcb` | 9 modules here, `autopcb` (9), `pcb` (7), `pcb/net_routing` (12), `release` (4), `review` (4), `utils` (1), `verification` (3) |
| [`pcb_io.py`](pcb_io.py) | Guarded, atomic PCB writes without overwriting unrelated files. | `compat`, `pcbnew`, `wx` | `campaign_cli`, `autopcb` (1), `pcb` (14), `pcb/net_routing` (23), `release` (2), `review` (1), `verification` (3), `verification/test_pcb_io.py` |
| [`project.py`](project.py) | Resolve related KiCad documents and fingerprint the native project tree. | `export_helpers`, `kicad_sexpr`, `paths`, `review` | `campaign_cli`, `kicad_campaign`, `launcher`, `autopcb` (10), `pcb` (13), `pcb/net_routing` (15), `review` (2), `verification` (1) |
| [`safe_zone_fill.py`](safe_zone_fill.py) | Guard KiCad's assertion-prone filled-polygon accessor. | None | `verification/test_safe_zone_fill.py` |
| [`toolchain.py`](toolchain.py) | Discover installed KiCad-related capabilities without opening a design. | `pcb`, `sch` | `campaign_cli`, `launcher`, `library_campaign_cli`, `library_validation`, `autopcb` (2), `pcb` (9), `pcb/net_routing` (8), `release` (1), `review` (2), `verification` (2), `verification/test_toolchain.py` |
