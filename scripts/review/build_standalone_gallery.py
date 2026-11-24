"""build_standalone_gallery.py: embed every gallery view once in one shareable HTML; preserve the gallery.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import argparse
import base64
import hashlib
import html
import json
import os
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path


class Gallery(HTMLParser):
    """Reads the view sections of a gallery ``index.html``."""

    def __init__(self):
        """Start with no sections and no views."""
        super().__init__()
        self.section = None
        self.heading = False
        self.views = []
        self.title = ""

    def handle_starttag(self, tag, attrs):
        """Track the current section and record each image as (section id, title, source)."""
        a = dict(attrs)
        if tag == "section":
            self.section = a.get("id")
            self.title = ""
        if tag == "h2" and self.section:
            self.heading = True
        if tag == "img" and self.section:
            self.views.append(
                (self.section, self.title or a.get("alt", self.section), a["src"])
            )

    def handle_endtag(self, tag):
        """Leave the heading or section that ends."""
        if tag == "h2":
            self.heading = False
        if tag == "section":
            self.section = None

    def handle_data(self, text):
        """Collect heading text as the title of the current section."""
        if self.heading:
            self.title += text


class StandaloneGallery:
    """Builds one self-contained HTML page from a gallery of PNG views."""

    CSS = (
        "body{background:#eef1f5;color:#102030;font:18px/1.5 "
        "system-ui;max-width:1600px;margin:auto;"
        "padding:20px}img{max-width:100%;"
        "height:auto}.view{display:block;border:0;padding:0;"
        "background:transparent;cursor:zoom-in;"
        "width:100%}h2{margin-top:40px}nav{display:flex;flex-"
        "wrap:wrap;gap:12px}a{color:#086795}section{scroll-margin-"
        "top:16px}dialog{border:0;padding:12px;width:95vw;"
        "height:92vh;max-width:none;max-height:none;"
        "background:#eef1f5}dialog::backdrop{background:#000b}.tool"
        "bar{display:flex;gap:16px;align-items:center;"
        "position:sticky;top:0;background:#eef1f5;"
        "padding:8px}#canvas{height:calc(100% - "
        "68px);overflow:auto;text-align:center}#large{max-"
        "height:100%;object-fit:contain}#large.full{max-"
        "height:none;max-width:none}button{font:inherit}pre{white-"
        "space:pre-wrap;font-size:12px;overflow-"
        "wrap:anywhere}@media "
        "print{nav,.toolbar,dialog{display:none}section{break-"
        "inside:avoid}.view{cursor:auto}}"
    )
    SCRIPT = (
        "const "
        "d=document.querySelector('dialog'),"
        "im=document.querySelector('#large');function "
        "showView(el){im.src=el.src;im.alt=el.alt;"
        "im.classList.remove('full');"
        "document.querySelector('#caption').textContent=el.alt;"
        "d.showModal()}function "
        "toggleZoom(){im.classList.toggle('full')}"
    )

    def __init__(self, folder: Path, status: str):
        """Read the gallery index and its view list.

        Inputs:
            folder: Gallery folder with ``index.html``, the PNG views and
                ``source.json``.
            status: Sentence shown under the title.
        Outputs: None. Raises AssertionError when the index lists no views or a
            view twice.
        """
        self.folder = folder
        self.status = status
        self.index = folder / "index.html"
        self.original = self.index.read_bytes()
        parser = Gallery()
        parser.feed(self.original.decode("utf-8"))
        self.views = parser.views
        assert self.views and len({q[0] for q in self.views}) == len(self.views), (
            "Missing or duplicate gallery views"
        )

    @staticmethod
    def digest(data: bytes) -> str:
        """Return the hex SHA-256 of ``data``."""
        return hashlib.sha256(data).hexdigest()

    def embed_views(self) -> tuple[list, list]:
        """Read every view and encode it as an inline PNG section.

        Outputs:
            The section HTML strings and, for each view, a record with its id,
            title, file name, SHA-256 and size in bytes. Raises AssertionError
            for a view that is not a local PNG file inside the gallery folder.
        """
        bodies = []
        manifest = []
        for ident, title, relative in self.views:
            path = (self.folder / relative).resolve()
            assert path.is_relative_to(self.folder) and path.suffix.lower() == ".png", (
                "Only local native PNG views are embedded"
            )
            data = path.read_bytes()
            assert data.startswith(b"\x89PNG\r\n\x1a\n")
            encoded = base64.b64encode(data).decode("ascii")
            bodies.append(
                '<section id="'
                + html.escape(ident, quote=True)
                + '"><h2>'
                + html.escape(title)
                + (
                    '</h2><button class="view" '
                    "onclick=\"showView(this.querySelector('img'))\" "
                    'aria-label="Enlarge '
                )
                + html.escape(title, quote=True)
                + '"><img loading="lazy" alt="'
                + html.escape(title, quote=True)
                + '" src="data:image/png;base64,'
                + encoded
                + '"></button></section>'
            )
            manifest.append(
                {
                    "view": ident,
                    "title": title,
                    "filename": relative,
                    "sha256": self.digest(data),
                    "bytes": len(data),
                }
            )
        return bodies, manifest

    def native_identity(self) -> dict:
        """Read the native source hashes recorded with the gallery.

        Outputs:
            File name to SHA-256. Raises AssertionError when they do not name
            both a board and a schematic.
        """
        source = json.loads((self.folder / "source.json").read_text(encoding="utf-8"))
        native = source.get(
            "sha256",
            source.get("source_sha256", source.get("native_source_sha256", {})),
        )
        assert (
            native
            and any(name.endswith(".kicad_pcb") for name in native)
            and any(name.endswith(".kicad_sch") for name in native)
        ), "Missing native source identity"
        return native

    def document(self, native: dict, bodies: list) -> str:
        """Compose the page from the embedded sections.

        Inputs:
            native: Native source hashes, shown in the page.
            bodies: Section HTML strings from ``embed_views``.
        Outputs:
            The page HTML.
        """
        title = html.escape(
            Path(next(name for name in native if name.endswith(".kicad_pcb"))).stem
        )
        nav = (
            "<nav>"
            + "".join(
                '<a href="#'
                + html.escape(i, quote=True)
                + '">'
                + html.escape(t)
                + "</a>"
                for i, t, _ in self.views
            )
            + "</nav>"
        )
        return (
            (
                '<!doctype html><html lang="en"><meta '
                'charset="utf-8"><meta name="viewport" '
                'content="width=device-width, '
                'initial-scale=1"><title>'
            )
            + title
            + " — review views</title><style>"
            + self.CSS
            + "</style><h1>"
            + title
            + " — "
            + str(len(self.views))
            + " views</h1><p>"
            + html.escape(self.status)
            + (
                "</p><p>All images are embedded. Share this file on its "
                "own. Select a view to enlarge it.</p>"
            )
            + nav
            + "".join(bodies)
            + "<details><summary>Source identity</summary><pre>"
            + html.escape(json.dumps(native, indent=2))
            + (
                '</pre></details><dialog><div class="toolbar"><button '
                'onclick="d.close()">Close</button><button '
                'onclick="toggleZoom()">Fit / 100%</button><span '
                'id="caption"></span></div><div id="canvas"><img '
                'id="large" alt=""></div></dialog><script>'
            )
            + self.SCRIPT
            + "</script></html>"
        )

    def build(self) -> tuple[str, list, dict]:
        """Build the page and check it against the views and the gallery.

        Outputs:
            The page HTML, the per-view records and the native source hashes.
            Raises AssertionError when an embedded image differs from its view,
            the page links an external file, or the gallery index changed.
        """
        bodies, manifest = self.embed_views()
        native = self.native_identity()
        document = self.document(native, bodies)
        encoded_images = re.findall(
            r'src="data:image/png;base64,([A-Za-z0-9+/=]+)"', document
        )
        assert len(encoded_images) == len(manifest)
        for encoded, row in zip(encoded_images, manifest):
            assert (
                self.digest(base64.b64decode(encoded, validate=True)) == row["sha256"]
            )
        assert not re.search(
            r'(?:src|href)="(?:https?:|file:|[^#d][^\"]*\.(?:png|json|css|js))',
            document,
        ), "Unexpected external dependency"
        assert self.index.read_bytes() == self.original, "Existing gallery changed"
        return document, manifest, native

    @staticmethod
    def write(document: str, out: Path) -> None:
        """Write the page to ``out`` through a temporary file, creating its folder.

        Inputs:
            document: Page HTML.
            out: Destination file.
        """
        out.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="standalone-", suffix=".tmp", dir=out.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(document)
            os.replace(name, out)
        finally:
            if Path(name).exists():
                Path(name).unlink()


class BuildStandaloneGalleryCommand:
    """Command-line entry: write the standalone gallery page and print its summary."""

    def __call__(self, argv=None):
        """Parse the command line, build and write the page, and print a JSON summary.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        """
        ap = argparse.ArgumentParser(description=__doc__)
        ap.add_argument("--views", type=Path, required=True)
        ap.add_argument("--out", type=Path, required=True)
        ap.add_argument(
            "--status",
            default="Review snapshot. Use matching validation before manufacturing.",
        )
        a = ap.parse_args(argv)
        gallery = StandaloneGallery(a.views.resolve(), a.status)
        document, manifest, native = gallery.build()
        StandaloneGallery.write(document, a.out)
        print(
            json.dumps(
                {
                    "standalone": str(a.out.resolve()),
                    "views": len(gallery.views),
                    "embedded_image_bytes": sum(x["bytes"] for x in manifest),
                    "html_bytes": a.out.stat().st_size,
                    "all_embedded_images_match": True,
                    "gallery_unchanged": True,
                    "native_source_sha256": native,
                },
                indent=2,
            )
        )


main = BuildStandaloneGalleryCommand()

if __name__ == "__main__":
    main()
