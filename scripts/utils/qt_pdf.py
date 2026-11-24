"""qt_pdf.py: print a staged local HTML page to PDF in an isolated Qt WebEngine process.

The build runs this file as a separate process so a Qt failure cannot take the
build down with it.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


class QtPdfPrinter:
    """Prints one HTML file to US Letter with the document stylesheet's margins."""

    TIMEOUT_MS = 90000
    MARGINS_MM = (16.256, 12.192, 16.256, 14.224)

    def __call__(self, source: Path, output: Path, title: str) -> None:
        """Print the page and set the PDF title.

        Inputs:
            source: Local HTML file.
            output: PDF to write.
            title: PDF metadata title.
        Outputs:
            None. Raises RuntimeError when the page does not load, print, or
            finish within 90 s.
        """
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault(
            "QTWEBENGINE_CHROMIUM_FLAGS",
            "--disable-gpu --disable-background-networking",
        )
        import fitz
        from PySide6.QtCore import QMarginsF, QTimer, QUrl
        from PySide6.QtGui import QPageLayout, QPageSize
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
        from PySide6.QtWidgets import QApplication

        application = QApplication([])
        page = QWebEnginePage()
        settings = page.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, False)
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False
        )
        layout = QPageLayout(
            QPageSize(QPageSize.PageSizeId.Letter),
            QPageLayout.Orientation.Portrait,
            QMarginsF(*self.MARGINS_MM),
            QPageLayout.Unit.Millimeter,
        )
        result = {"error": "PDF rendering timed out"}

        def finish(path: str, success: bool) -> None:
            """Record whether printing succeeded and stop the event loop."""
            result["error"] = "" if success else f"Could not print PDF: {path}"
            application.quit()

        def loaded(success: bool) -> None:
            """Print once the page has loaded, or stop with an error."""
            if success:
                page.printToPdf(str(output), layout)
            else:
                result["error"] = f"Could not load HTML: {source}"
                application.quit()

        page.pdfPrintingFinished.connect(finish)
        page.loadFinished.connect(loaded)
        QTimer.singleShot(self.TIMEOUT_MS, application.quit)
        page.load(QUrl.fromLocalFile(str(source.resolve())))
        application.exec()
        if result["error"]:
            raise RuntimeError(result["error"])
        with fitz.open(output) as document:
            metadata = document.metadata
            metadata["title"] = title
            document.set_metadata(metadata)
            document.saveIncr()


class QtPdfCommand:
    """Command-line entry: print one HTML file to one PDF."""

    def __call__(self) -> int:
        """Read the source, output and title arguments and print the page.

        Outputs: Exit status 0. Raises OSError or RuntimeError on failure.
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("source", type=Path)
        parser.add_argument("output", type=Path)
        parser.add_argument("title")
        args = parser.parse_args()
        QtPdfPrinter()(args.source, args.output, args.title)
        return 0


main = QtPdfCommand()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as exc:
        print(f"Qt PDF rendering failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
