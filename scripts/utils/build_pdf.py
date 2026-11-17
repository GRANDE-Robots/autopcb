#!/usr/bin/env python3
"""Build configured Markdown documents as PDFs using local tools.

Adapted from the AirSpect GRANDE-style documentation renderer.
"""

from __future__ import annotations

import argparse
import hashlib
import html
from html.parser import HTMLParser
import importlib.util
import os
from pathlib import Path

from core.paths import asset_root, default_project
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree


DEFAULT_PROJECT = default_project(__file__)
DISPLAY_MATH = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
INLINE_MATH = re.compile(r"(?<![\\$])\$(?!\$)(.+?)(?<![\\$])\$(?!\$)", re.DOTALL)
PAGE_BREAK_TOKEN = "AIRSPECTPDFPAGEBREAKTOKEN"

STYLE = r"""
@page {
  size: Letter;
  margin: 0.48in 0.64in 0.56in;
  @bottom-center { content: counter(page); font: 8pt Arial, sans-serif; color: #516978; }
}
* { box-sizing: border-box; }
html { color: #172733; background: #fff; }
body {
  margin: 0;
  font-family: "Latin Modern Roman", "DejaVu Serif", Georgia, serif;
  font-size: 9.25pt;
  line-height: 1.26;
}
h1, h2, h3 {
  font-family: "Latin Modern Sans", "DejaVu Sans", Arial, sans-serif;
  color: #173f59;
  line-height: 1.16;
  break-after: avoid-page;
}
h1 {
  margin: 0 0 0.16in;
  padding-bottom: 0.09in;
  border-bottom: 2.2pt solid #2d7890;
  font-size: 22pt;
  letter-spacing: -0.25pt;
}
h1 + p {
  margin: 0.07in 0 0.18in;
  color: #26475a;
  text-align: center;
}
h2 {
  margin: 0.16in 0 0.07in;
  padding-bottom: 0.035in;
  border-bottom: 0.5pt solid #b8cbd6;
  font-size: 14.1pt;
}
h3 { margin: 0.14in 0 0.05in; color: #087b83; font-size: 11.2pt; }
h4 { margin: 0.12in 0 0.05in; font-size: 10pt; break-after: avoid-page; }
p { margin: 0.03in 0 0.04in; orphans: 3; widows: 3; }
p:has(+ .math-display) { break-after: avoid-page; }
a { color: #176780; text-decoration: none; }
strong { color: #102f43; }
ul { margin: 0.06in 0 0.12in 0.23in; padding-left: 0.12in; }
ol { margin: 0.06in 0 0.12in 0.23in; padding: 0; list-style: none; counter-reset: reference; }
ol > li { display: block; position: relative; margin: 0.025in 0; padding-left: 0.16in; counter-increment: reference; }
ol > li::before { content: counter(reference) "."; position: absolute; left: 0; }
table {
  width: 100%;
  margin: 0.1in 0 0.16in;
  border-collapse: collapse;
  table-layout: fixed;
  font-size: 8.15pt;
  line-height: 1.3;
}
thead { display: table-header-group; }
tr { break-inside: avoid; }
th, td {
  padding: 3.6pt 4.6pt;
  border: 0.55pt solid #aabec9;
  text-align: left;
  vertical-align: top;
  overflow-wrap: anywhere;
}
th { color: #123b50; background: #dfeef2; font-weight: 700; }
tbody tr:nth-child(even) td { background: #f7fafb; }
p > img {
  display: block;
  max-width: 100%;
  max-height: 6.15in;
  margin: 0.12in auto 0.08in;
  object-fit: contain;
}
.math-display {
  margin: 0.09in auto 0.13in;
  text-align: center;
  break-inside: avoid;
}
.math-display img { max-width: 96%; max-height: 1.05in; }
.math-inline { display: inline-block; white-space: nowrap; }
.math-inline img { display: block; max-width: 100%; }
.page-break { break-before: page; }
"""


def run(
    command: list[str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one local renderer command without inheriting interactive input."""
    return subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=120,
    )


def require_tool(name: str) -> str:
    """Return a required executable from PATH or raise an actionable error."""
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"required documentation tool is unavailable: {name}")
    return path


def require_chrome() -> str:
    """Resolve a Chromium renderer on Windows or a Unix-like host."""
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chrome",
        "msedge",
    ):
        path = shutil.which(name)
        if path:
            return path
    if os.name == "nt":
        for candidate in (
            Path(os.environ.get("PROGRAMFILES", ""))
            / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES", ""))
            / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", ""))
            / "Microsoft/Edge/Application/msedge.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    raise RuntimeError("required documentation tool is unavailable: Chromium browser")


def qt_renderer_available() -> bool:
    """Check for the optional isolated Qt renderer and PDF metadata writer."""
    try:
        return bool(
            importlib.util.find_spec("PySide6.QtWebEngineCore")
            and importlib.util.find_spec("fitz")
        )
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def protect_code(source: str) -> tuple[str, dict[str, str]]:
    """Keep fenced, indented and inline code out of math tokenization."""
    from markdown_it import MarkdownIt

    protected: dict[str, str] = {}
    lines = source.splitlines(keepends=True)
    code_blocks = [
        token.map
        for token in MarkdownIt("commonmark").parse(source)
        if token.type in ("fence", "code_block") and token.map is not None
    ]
    for start, end in reversed(code_blocks):
        token = f"PDFCODETOKEN{len(protected):04d}END"
        protected[token] = "".join(lines[start:end])
        lines[start:end] = [token + "\n"]

    def replace(match: re.Match[str]) -> str:
        token = f"PDFCODETOKEN{len(protected):04d}END"
        protected[token] = match.group(0)
        return token

    return re.sub(
        r"(?<!`)(`+)(?!`)([\s\S]*?)(?<!`)\1(?!`)", replace, "".join(lines)
    ), protected


def protect_math(source: str) -> tuple[str, dict[str, tuple[str, bool]]]:
    """Replace TeX spans with tokens before Markdown rendering."""
    formulae: dict[str, tuple[str, bool]] = {}

    def replace(display: bool):
        """Create a tokenizing callback for one class of TeX expression."""

        def inner(match: re.Match[str]) -> str:
            """Store one matched formula and return its stable Markdown token."""
            token = f"AIRSPECTMATHTOKEN{len(formulae):04d}END"
            formulae[token] = (match.group(1).strip(), display)
            return f"\n\n{token}\n\n" if display else token

        return inner

    source = DISPLAY_MATH.sub(replace(True), source)
    return INLINE_MATH.sub(replace(False), source), formulae


def latex_svg(
    expression: str, display: bool, directory: Path, latex: str, dvisvgm: str
) -> Path:
    """Render one TeX expression to a cached, local SVG asset."""
    digest = hashlib.sha256(f"{display}:{expression}".encode("utf-8")).hexdigest()[:20]
    svg_path = directory / f"{digest}.svg"
    if svg_path.exists():
        return svg_path
    tex_path = directory / f"{digest}.tex"
    wrapper = (
        r"\[\displaystyle %s\]"
        if display
        else r"\(\special{dvisvgm:raw <g id='math-baseline' data-y='{?y}'/>}%s\)"
    )
    tex_path.write_text(
        "\n".join(
            (
                r"\documentclass[10pt]{article}",
                r"\usepackage{amsmath,amssymb,bm}",
                r"\pagestyle{empty}",
                r"\begin{document}",
                wrapper % expression,
                r"\end{document}",
                "",
            )
        ),
        encoding="utf-8",
    )
    compiled = run(
        [
            latex,
            "-no-shell-escape",
            "-interaction=nonstopmode",
            "-halt-on-error",
            tex_path.name,
        ],
        cwd=directory,
    )
    if compiled.returncode:
        raise RuntimeError(
            f"equation render failed for {expression!r}:\n{(compiled.stdout + compiled.stderr)[-3000:]}"
        )
    converted = run(
        [
            dvisvgm,
            "--no-fonts",
            "--exact-bbox",
            "--verbosity=0",
            f"--output={svg_path}",
            str(tex_path.with_suffix(".dvi")),
        ],
        cwd=directory,
    )
    if converted.returncode or not svg_path.exists():
        raise RuntimeError(
            f"equation SVG conversion failed for {expression!r}:\n{(converted.stdout + converted.stderr)[-3000:]}"
        )
    return svg_path


def inline_depth(svg: Path) -> float:
    """Measure the visible SVG depth below the TeX baseline, in points."""
    root = ElementTree.fromstring(svg.read_text(encoding="utf-8"))
    viewbox = root.get("viewBox", "").split()
    marker = next(
        (item for item in root.iter() if item.get("id") == "math-baseline"), None
    )
    if len(viewbox) != 4 or marker is None or marker.get("data-y") is None:
        raise RuntimeError("Inline equation has no usable TeX baseline")
    try:
        top, height = float(viewbox[1]), float(viewbox[3])
        baseline = float(marker.get("data-y"))
    except ValueError as exc:
        raise RuntimeError("Inline equation has invalid baseline geometry") from exc
    return top + height - baseline


def render_html(source: str, work_dir: Path, source_path: Path) -> tuple[str, str]:
    """Convert the GitHub-readable guide into a self-contained print page."""
    try:
        from markdown_it import MarkdownIt
    except ImportError as exc:
        raise RuntimeError(
            "install the documentation dependency with: python -m pip install markdown-it-py"
        ) from exc

    title_match = re.search(r"^#\s+(.+?)\s*$", source, re.MULTILINE)
    title = title_match.group(1) if title_match else source_path.stem
    code_safe, code_tokens = protect_code(source)
    protected_source, formulae = protect_math(code_safe)
    for token, original in code_tokens.items():
        protected_source = protected_source.replace(token, original)
    # Keep PDF pagination out of the reader-facing Markdown document.
    protected_source = protected_source.replace("<!-- pagebreak -->", PAGE_BREAK_TOKEN)
    body = (
        MarkdownIt("commonmark", {"html": True, "linkify": False, "typographer": True})
        .enable("table")
        .render(protected_source)
    )
    body = body.replace(f"<p>{PAGE_BREAK_TOKEN}</p>", '<div class="page-break"></div>')
    if formulae:
        latex = require_tool("latex")
        dvisvgm = require_tool("dvisvgm")
        math_dir = work_dir / "math"
        math_dir.mkdir()
        for token, (expression, display) in formulae.items():
            svg = latex_svg(expression, display, math_dir, latex, dvisvgm)
            image = f'<img src="{svg.as_uri()}" alt="{html.escape(expression, quote=True)}">'
            replacement = (
                f'<div class="math-display">{image}</div>'
                if display
                else f'<span class="math-inline" style="vertical-align:{-inline_depth(svg):.4f}pt">{image}</span>'
            )
            body = (
                body.replace(f"<p>{token}</p>", replacement)
                if display
                else body.replace(token, replacement)
            )
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<base href="{source_path.parent.resolve().as_uri()}/"><title>{html.escape(title)}</title>
<style>{STYLE}</style></head><body>{body}</body></html>
"""
    return title, page


def validate_pdf(path: Path, expected_title: str, pdfinfo: str) -> None:
    """Confirm a substantial US-letter PDF with the guide title."""
    if not path.is_file() or path.stat().st_size < 10_000:
        raise RuntimeError(f"rendered PDF is missing or unexpectedly small: {path}")
    result = run([pdfinfo, str(path)])
    if result.returncode or "letter" not in result.stdout.lower():
        raise RuntimeError(f"PDF is not US-letter: {path}\n{result.stderr}")
    title_line = next(
        (line for line in result.stdout.splitlines() if line.startswith("Title:")), ""
    )
    if expected_title not in title_line:
        raise RuntimeError(f"PDF metadata title mismatch: {title_line}")


def render_pdf(html_path: Path, staged_pdf: Path, title: str, pdfinfo: str) -> None:
    """Render and validate in isolation, falling back to Chromium if needed."""
    qt_error = None
    if qt_renderer_available():
        try:
            rendered = run(
                [
                    sys.executable,
                    "-B",
                    str(Path(__file__).with_name("qt_pdf.py")),
                    str(html_path),
                    str(staged_pdf),
                    title,
                ]
            )
            if rendered.returncode:
                raise RuntimeError((rendered.stdout + rendered.stderr)[-3000:])
            validate_pdf(staged_pdf, title, pdfinfo)
            return
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            qt_error = exc
            staged_pdf.unlink(missing_ok=True)
            print("Qt PDF rendering failed; trying Chromium.", file=sys.stderr)

    try:
        chrome = require_chrome()
        rendered = run(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--allow-file-access-from-files",
                "--disable-background-networking",
                "--no-first-run",
                f"--user-data-dir={html_path.parent / 'chrome-profile'}",
                "--no-pdf-header-footer",
                f"--print-to-pdf={staged_pdf}",
                html_path.as_uri(),
            ]
        )
        if rendered.returncode:
            raise RuntimeError(
                f"Browser PDF rendering failed:\n{(rendered.stdout + rendered.stderr)[-3000:]}"
            )
        validate_pdf(staged_pdf, title, pdfinfo)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        if qt_error is not None:
            raise RuntimeError(f"Both PDF renderers failed. Qt: {qt_error}\n{exc}") from exc
        raise


def project_path(project: Path, name: object, suffix: str) -> Path:
    """Resolve a configured path without permitting writes outside the project."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Document paths must be nonempty strings")
    result = (project / name).resolve()
    if not result.is_relative_to(project) or result.suffix.lower() != suffix:
        raise ValueError(f"Expected a project-relative {suffix} path: {name}")
    return result


def file_hash(path: Path) -> str:
    """Hash a file without keeping large image assets in memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class _ImageReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"img", "image"}:
            return
        for name, value in attrs:
            if value and name in {"src", "href", "xlink:href"}:
                self.sources.append(value)


def image_dependencies(
    source: str, source_path: Path, project: Path
) -> tuple[Path, ...]:
    """Require local, source-bound images, including images embedded in HTML."""
    try:
        from markdown_it import MarkdownIt
    except ImportError as exc:
        raise RuntimeError("install markdown-it-py for this Python runtime") from exc
    pending = list(MarkdownIt("commonmark", {"html": True}).parse(source))
    references = []
    while pending:
        token = pending.pop()
        pending.extend(token.children or ())
        if token.type == "image":
            references.append(token.attrGet("src"))
        elif token.type in {"html_block", "html_inline"}:
            parser = _ImageReferences()
            parser.feed(token.content)
            references.extend(parser.sources)
    assets = set()
    for name in references:
        if not name:
            raise ValueError("Every image needs a source")
        parsed = urlsplit(html.unescape(name))
        if parsed.scheme == "data" and name.lower().startswith("data:image/"):
            continue
        if parsed.scheme or parsed.netloc or not parsed.path:
            raise ValueError(f"Image must be a local project file: {name}")
        asset = (source_path.parent / unquote(parsed.path)).resolve()
        if not asset.is_relative_to(project) or not asset.is_file():
            raise ValueError(f"Image is missing or outside the project: {name}")
        assets.add(asset)
    return tuple(sorted(assets))


def build_pdf(
    source_path: Path,
    output_path: Path,
    *,
    project: Path | None = None,
    guards: dict[Path, str] | None = None,
) -> None:
    """Build one document from a stable source and atomically replace its PDF."""
    source_bytes = source_path.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    source = source_bytes.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    project = (project or source_path.parent).resolve()
    dependencies = {source_path: source_hash, **(guards or {})}
    dependencies.update(
        {
            path: file_hash(path)
            for path in image_dependencies(source, source_path, project)
        }
    )
    if any(
        not path.is_file() or file_hash(path) != digest
        for path, digest in dependencies.items()
    ):
        raise RuntimeError("Documentation inputs changed while being read")
    previous_output = file_hash(output_path) if output_path.is_file() else None
    pdfinfo = require_tool("pdfinfo")
    with tempfile.TemporaryDirectory(prefix="autopcb-pdf-") as temporary_name:
        work_dir = Path(temporary_name)
        title, page = render_html(source, work_dir, source_path)
        html_path = work_dir / "document.html"
        staged_pdf = work_dir / "document.pdf"
        html_path.write_text(page, encoding="utf-8")
        render_pdf(html_path, staged_pdf, title, pdfinfo)
        if any(
            not path.is_file() or file_hash(path) != digest
            for path, digest in dependencies.items()
        ):
            raise RuntimeError("Documentation inputs changed during rendering")
        if (
            file_hash(output_path) if output_path.is_file() else None
        ) != previous_output:
            raise RuntimeError("PDF output changed during rendering")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".pdf-build-",
                suffix=".tmp",
                dir=output_path.parent,
                delete=False,
            ) as stream:
                temp_path = Path(stream.name)
                with staged_pdf.open("rb") as rendered_pdf:
                    shutil.copyfileobj(rendered_pdf, stream)
            if any(
                not path.is_file() or file_hash(path) != digest
                for path, digest in dependencies.items()
            ):
                raise RuntimeError("Documentation inputs changed before publication")
            if (
                file_hash(output_path) if output_path.is_file() else None
            ) != previous_output:
                raise RuntimeError("PDF output changed before publication")
            os.replace(temp_path, output_path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)


def main(arguments: list[str] | None = None) -> int:
    """Build the Markdown files enumerated in config/pdf_build.json."""
    import json
    from core.console import Console, add_output_arguments

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    parser.add_argument(
        "--config", type=Path, help="project-relative configuration JSON"
    )
    add_output_arguments(parser)
    args = parser.parse_args(arguments)
    console = Console(json_mode=args.json, verbose=args.verbose)
    project = args.project.resolve()
    config_path = project_path(
        project,
        str(
            args.config
            or (asset_root(project) / "config/pdf_build.json").relative_to(project)
        ),
        ".json",
    )
    config_bytes = config_path.read_bytes()
    config_hash = hashlib.sha256(config_bytes).hexdigest()
    config = json.loads(config_bytes.decode("utf-8-sig"))
    if not isinstance(config, dict) or set(config) != {"documents"}:
        raise ValueError("PDF configuration must contain only a documents list")
    entries = config["documents"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("PDF configuration documents must be a nonempty list")
    documents = []
    outputs = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"source", "output"}:
            raise ValueError("Each PDF document needs source and output")
        source = project_path(project, entry["source"], ".md")
        output = project_path(project, entry["output"], ".pdf")
        if not source.is_file():
            raise ValueError(f"Markdown source not found: {source}")
        if output in outputs:
            raise ValueError(f"Duplicate PDF destination: {output}")
        outputs.add(output)
        documents.append((source, output))
    for index, (source, output) in enumerate(documents, 1):
        with console.activity(f"{index}/{len(documents)} | Rendering {source.name} -> {output.name}"):
            build_pdf(source, output, project=project, guards={config_path: config_hash})

    def display(console: Console, result: dict) -> None:
        console.heading("AutoPCB | Documentation PDFs updated")
        for name in result["outputs"]:
            console.line(name, prefix="  ")

    console.result({"status": "rendered", "outputs": [str(output) for _, output in documents]}, display)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"documentation build failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
