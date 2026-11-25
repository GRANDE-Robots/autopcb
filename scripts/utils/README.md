# utils

Documentation build tools. `build_pdf.py` renders the Markdown documents listed in `config/pdf_build.json` to PDF with local tools: `python scripts/run.py --project . docs build-pdf`.

## File Structure

| File | Relevance | Dependencies | Used by |
| --- | --- | --- | --- |
| [`__init__.py`](__init__.py) | The documentation build tools, with no code of their own. | None | None |
| [`build_pdf.py`](build_pdf.py) | Build configured Markdown documents as PDFs with local tools. | `core/compat`, `core/console`, `core/paths`, `markdown_it` | `core` (1) |
| [`qt_pdf.py`](qt_pdf.py) | Print a staged local HTML page to PDF in an isolated Qt WebEngine process. | `PySide6`, `fitz` | Command line |
