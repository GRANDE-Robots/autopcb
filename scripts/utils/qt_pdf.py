"""Render a staged local HTML document with an isolated Qt WebEngine process."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def render(source: Path, output: Path, title: str) -> None:
    """Print to US Letter using the same margins as the document stylesheet."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault(
        "QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-background-networking"
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
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
    layout = QPageLayout(
        QPageSize(QPageSize.PageSizeId.Letter),
        QPageLayout.Orientation.Portrait,
        QMarginsF(16.256, 12.192, 16.256, 14.224),
        QPageLayout.Unit.Millimeter,
    )
    result = {"error": "PDF rendering timed out"}

    def finish(path: str, success: bool) -> None:
        result["error"] = "" if success else f"Could not print PDF: {path}"
        application.quit()

    def loaded(success: bool) -> None:
        if success:
            page.printToPdf(str(output), layout)
        else:
            result["error"] = f"Could not load HTML: {source}"
            application.quit()

    page.pdfPrintingFinished.connect(finish)
    page.loadFinished.connect(loaded)
    QTimer.singleShot(90000, application.quit)
    page.load(QUrl.fromLocalFile(str(source.resolve())))
    application.exec()
    if result["error"]:
        raise RuntimeError(result["error"])
    with fitz.open(output) as document:
        metadata = document.metadata
        metadata["title"] = title
        document.set_metadata(metadata)
        document.saveIncr()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("title")
    args = parser.parse_args()
    render(args.source, args.output, args.title)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as exc:
        print(f"Qt PDF rendering failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
