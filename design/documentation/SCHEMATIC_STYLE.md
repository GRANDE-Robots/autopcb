# Schematic presentation standard

`v4.1.kicad_sch` is the authoritative circuit. Keep the drawing compact, orderly, and electrically honest.

- Keep each subsystem contiguous; use the same spacing rhythm within similar sections.
- Use short non-zero wires only when they show real local structure. Otherwise use labels directly; merge obviously adjacent duplicate labels.
- Keep reference/value text near its part. Horizontal resistors and capacitors place the reference inside the body when clear, with the value immediately above or below. Vertical parts keep both fields immediately beside the symbol.
- Section headers and captions identify function only. Do not put procurement, qualification, or verbose implementation notes on the schematic.
- Preserve stable references and pin names. Do not rearrange a signal solely for appearance when it hides the actual circuit relationship.
