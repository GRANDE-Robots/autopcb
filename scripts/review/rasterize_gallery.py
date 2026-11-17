"""Render native plots and assemble labelled, phone-friendly view sheets."""

import argparse
import html
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    import pypdfium2 as pdfium
except ModuleNotFoundError:
    pdfium = None
from PIL import Image, ImageChops

# The known full-sheet schematic plot is about 102 million pixels at the
# review resolution.  It is generated locally by KiCad, so allow that bounded
# size without emitting a misleading decompression-bomb warning.
Image.MAX_IMAGE_PIXELS = 160_000_000


def render_pdf(pdf_path, scale, page_number=0):
    """Render one zero-based PDF page using Pdfium when present, otherwise Poppler."""
    if pdfium is not None:
        doc = pdfium.PdfDocument(pdf_path)
        page = doc[page_number]
        try:
            bitmap = page.render(scale=scale)
            try:
                return bitmap.to_pil().convert("RGB")
            finally:
                bitmap.close()
        finally:
            page.close()
            doc.close()
    exe = shutil.which("pdftoppm")
    if not exe:
        raise RuntimeError(
            "Need either pypdfium2 or pdftoppm to rasterize the native PDF plots."
        )
    with tempfile.TemporaryDirectory(prefix="autopcb-gallery-") as td:
        prefix = Path(td) / "page"
        page = str(page_number + 1)
        result = subprocess.run(
            [
                exe,
                "-f",
                page,
                "-l",
                page,
                "-png",
                "-singlefile",
                "-r",
                str(max(1, round(72 * scale))),
                str(pdf_path),
                str(prefix),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise RuntimeError("pdftoppm failed: " + result.stderr.strip())
        with Image.open(str(prefix) + ".png") as image:
            return image.convert("RGB")


def content_bounds(im):
    mask = (
        ImageChops.difference(im, Image.new("RGB", im.size, "white"))
        .convert("L")
        .point(lambda x: 255 if x > 16 else 0)
    )
    return mask.getbbox()


def page_count(pdf_path):
    if pdfium is not None:
        doc = pdfium.PdfDocument(pdf_path)
        try:
            return len(doc)
        finally:
            doc.close()
    exe = shutil.which("pdfinfo")
    if not exe:
        raise RuntimeError("Need either pypdfium2 or pdfinfo to count schematic pages.")
    result = subprocess.run([exe, str(pdf_path)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("pdfinfo failed: " + result.stderr.strip())
    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    raise RuntimeError("pdfinfo did not report a page count.")


def render_single_page_schematic(pdf_path):
    """Render the one native v4.1 sheet without hiding its lower sections."""
    count = page_count(pdf_path)
    if count != 1:
        raise ValueError(
            f"{pdf_path.name}: expected one flattened schematic page, found {count}"
        )
    probe = render_pdf(pdf_path, 1, 0)
    try:
        scale = 12000 / max(probe.size)
    finally:
        probe.close()
    return render_pdf(pdf_path, scale, 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    folder = parser.parse_args().folder
    for p in folder.glob("*.pdf"):
        if p.stem == "schematic":
            im = render_single_page_schematic(p)
            im.save(p.with_suffix(".png"))
            im.close()
            continue
        # A 72 DPI render gives the PDF page size in pixels, avoiding a separate
        # Pdfium-only page-size dependency when the Poppler fallback is in use.
        probe = render_pdf(p, 1)
        scale = (12000 if p.stem == "schematic" else 2300) / max(probe.size)
        probe.close()
        im = render_pdf(p, scale)
        if p.stem != "schematic":
            bounds = content_bounds(im)
            if bounds:
                # Measure the board, then render the PDF again at its required scale.
                # Enlarging the cropped bitmap would not restore plot detail.
                extent = max(bounds[2] - bounds[0], bounds[3] - bounds[1])
                if extent < 2300:
                    scale *= 2300 / extent
                    im.close()
                    im = render_pdf(p, scale)
                    bounds = content_bounds(im)
            if bounds:
                im = im.crop(
                    (
                        max(0, bounds[0] - 30),
                        max(0, bounds[1] - 30),
                        min(im.width, bounds[2] + 30),
                        min(im.height, bounds[3] + 30),
                    )
                )
            if max(im.size) < 2300:
                raise ValueError(f"{p.name}: cropped plot is below 2300 pixels")
        im.save(p.with_suffix(".png"))
        im.close()

    copper = [("PCB_top", "Top copper")]
    for p in sorted(
        folder.glob("PCB_In*.png"), key=lambda p: int(p.stem.removeprefix("PCB_In"))
    ):
        copper.append((p.stem, p.stem.removeprefix("PCB_") + " copper"))
    copper.append(("PCB_bottom", "Bottom copper"))
    silk = [
        ("top_silkscreen", "Top silkscreen"),
        ("bottom_silkscreen", "Bottom silkscreen — underside"),
    ]
    three = [
        ("3D_top", "3D top"),
        ("3D_bottom", "3D bottom"),
        ("3D_isometric_top", "3D isometric top"),
        ("3D_isometric_bottom", "3D isometric bottom"),
    ]
    items = [("schematic", "Schematic")] + copper + silk + three
    body = "".join(
        '<section id="'
        + n
        + '"><h2>'
        + html.escape(t)
        + '</h2><a href="'
        + (n + ".pdf" if n == "schematic" else n + ".png")
        + '"><img loading="lazy" alt="'
        + html.escape(t)
        + '" src="'
        + n
        + '.png"></a></section>'
        for n, t in items
    )
    nav = (
        "<nav>"
        + "".join('<a href="#' + n + '">' + html.escape(t) + "</a>" for n, t in items)
        + "</nav>"
    )
    (folder / "index.html").write_text(
        (
            '<!doctype html><html lang="en"><meta '
            'charset="utf-8"><meta name="viewport" '
            'content="width=device-width, '
            'initial-scale=1"><title>KiCad '
            "views</title><style>body{background:#eef1f5;color:#102030;"
            "font:18px/1.5 "
            "system-ui;max-width:1600px;margin:auto;"
            "padding:20px}img{max-width:100%;height:auto}h2{margin-"
            "top:40px}nav{display:flex;flex-wrap:wrap;"
            "gap:12px}a{color:#086795}section{scroll-margin-top:16px}</"
            "style><h1>KiCad project review</h1><p>All "
        )
        + str(len(items))
        + (
            " native KiCad plots and renders. The schematic preview is "
            "the complete one-page editable circuit; tap it for the "
            "native PDF. Source identity is recorded in <a "
            'href="source.json">source.json</a>. Use matching '
            "validation reports to assess readiness.</p>"
        )
        + nav
        + body
        + "</html>",
        encoding="utf8",
    )
    print(json.dumps({"gallery": str(folder / "index.html"), "views": len(items)}))


if __name__ == "__main__":
    main()
