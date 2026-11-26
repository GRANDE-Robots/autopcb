# AutoPCB documentation

- [Placement, routing and verification map](workflow.html)
- [Algorithm guide](algorithm.pdf)
- [Editable Markdown source](algorithm.md)
- [USB breakout review and bring-up](usb-breakout-review.md)
- [Temperature sensor review and bring-up](temperature-sensor-review.md)
- [Installation and workspace commands](../README.md)

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`algorithm.md`](algorithm.md) | The editable algorithm guide: placement, routing, recovery and verification. | None | `config/pdf_build.json` |
| [`algorithm.pdf`](algorithm.pdf) | The rendered algorithm guide. Rebuild it with `docs build-pdf` whenever `algorithm.md` changes. | `algorithm.md`, `scripts/utils/build_pdf.py` | Readers, root `README.md` |
| [`impedance-geometry.md`](impedance-geometry.md) | The layer-specific geometry review for paired signals. | None | [`data/README.md`](../data/README.md) |
| [`temperature-sensor-review.md`](temperature-sensor-review.md) | Measured KiCad results, calculated supply, bus and self-heating figures, and a bench bring-up procedure for the catalog temperature sensor design. | None | Root `README.md` |
| [`usb-breakout-review.md`](usb-breakout-review.md) | Launch dispositions and a bench bring-up procedure for the catalog USB breakout design. | None | Root `README.md` |
| [`workflow.html`](workflow.html) | A map of how placement, routing and verification decisions are made, with measured results. | None | Root `README.md` |
