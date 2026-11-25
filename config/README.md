# config

Defaults that ship with the repository. Project policies are written to `config/projects/<project>/autopcb.json`, and edit campaigns to `config/campaigns/<project>/`. Git ignores `config/campaigns/`.

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`pdf_build.json`](pdf_build.json) | The Markdown documents that `docs build-pdf` renders and the PDF each becomes, with paths relative to the support root. It lists `docs/algorithm.md` and `docs/algorithm.pdf`. | None | `scripts/utils/build_pdf.py` |
| [`routing.defaults.json`](routing.defaults.json) | Workspace routing defaults in millimetres: search margin 6.0, step 0.15, track width 0.25, clearance 0.25, via size 0.65 and via drill 0.30, with `schema_version` 1. | None | `scripts/autopcb/cli.py` |
