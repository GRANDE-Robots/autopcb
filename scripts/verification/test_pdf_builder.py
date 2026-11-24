"""test_pdf_builder.py: check configured PDF builds without modifying project documents.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from utils import build_pdf as pdf


@unittest.skipUnless(
    importlib.util.find_spec("markdown_it"), "PDF tests require markdown-it-py"
)
class PDFBuilderTests(unittest.TestCase):
    """The PDF builder's renderers, input guards and configuration."""

    def setUp(self):
        """Make every test start without the Qt renderer."""
        qt = mock.patch.object(pdf.RenderTools, "qt_available", return_value=False)
        qt.start()
        self.addCleanup(qt.stop)

    @staticmethod
    def print_target(command) -> str:
        """The PDF path a Chromium command line prints to."""
        return next(
            arg.split("=", 1)[1] for arg in command if arg.startswith("--print-to-pdf=")
        )

    @staticmethod
    def stages(**renderers):
        """Patch the page, tool and validation stages of one build.

        Inputs:
            renderers: Replacement patchers for ``chrome``, ``require``, ``page``
                or ``validate``.
        Outputs: A context manager that applies every patch.
        """
        defaults = {
            "chrome": mock.patch.object(
                pdf.RenderTools, "chrome", return_value="chrome"
            ),
            "require": mock.patch.object(
                pdf.RenderTools, "require", return_value="pdfinfo"
            ),
            "page": mock.patch.object(
                pdf.HtmlPage, "__call__", return_value=("Test", "<html/>")
            ),
            "validate": mock.patch.object(pdf.PdfRenderer, "validate"),
        }
        defaults.update(renderers)
        stack = contextlib.ExitStack()
        for patcher in defaults.values():
            stack.enter_context(patcher)
        return stack

    def test_qt_is_used_when_available(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            source, output = project / "test.md", project / "test.pdf"
            source.write_text("# Test", encoding="utf-8")

            def render(command, **_kwargs):
                """Check the Qt script is the renderer and stage its PDF."""
                self.assertEqual(Path(command[2]).name, "qt_pdf.py")
                Path(command[4]).write_bytes(b"Qt PDF")
                return mock.Mock(returncode=0, stderr="", stdout="")

            with (
                self.stages(
                    chrome=mock.patch.object(pdf.RenderTools, "chrome"),
                ),
                mock.patch.object(pdf.RenderTools, "qt_available", return_value=True),
                mock.patch.object(pdf.RenderTools, "run", side_effect=render),
            ):
                pdf.PdfBuild()(source, output, project=project)
                pdf.RenderTools.chrome.assert_not_called()
            self.assertEqual(output.read_bytes(), b"Qt PDF")

    def test_invalid_qt_output_falls_back_without_publishing_it(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            source, output = project / "test.md", project / "test.pdf"
            source.write_text("# Test", encoding="utf-8")
            output.write_bytes(b"old output")

            def render(command, **_kwargs):
                """Stage an invalid Qt PDF, then fail the Chromium run."""
                if Path(command[2]).name == "qt_pdf.py":
                    Path(command[4]).write_bytes(b"invalid Qt PDF")
                    return mock.Mock(returncode=0, stderr="", stdout="")
                self.assertFalse(Path(self.print_target(command)).exists())
                return mock.Mock(returncode=1, stderr="browser failed", stdout="")

            with (
                self.stages(
                    validate=mock.patch.object(
                        pdf.PdfRenderer, "validate", side_effect=RuntimeError("bad PDF")
                    ),
                ),
                mock.patch.object(pdf.RenderTools, "qt_available", return_value=True),
                mock.patch.object(pdf.RenderTools, "run", side_effect=render),
            ):
                error = io.StringIO()
                with (
                    contextlib.redirect_stderr(error),
                    self.assertRaisesRegex(RuntimeError, "browser failed"),
                ):
                    pdf.PdfBuild()(source, output, project=project)
            self.assertIn("trying Chromium", error.getvalue())
            self.assertEqual(output.read_bytes(), b"old output")

    def test_crlf_input_is_normalized_for_math_rendering(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            source, output = project / "test.md", project / "test.pdf"
            source.write_bytes(b"# Test\r\n\r\n$$\r\nx+y\r\n$$\r\n")

            def render_document(value, *_args):
                """Check the page stage sees normalized line endings."""
                self.assertNotIn("\r", value)
                self.assertIn("$$\nx+y\n$$", value)
                return "Test", "<html/>"

            def render(command, **_kwargs):
                """Stage the Chromium PDF."""
                Path(self.print_target(command)).write_bytes(b"staged PDF")
                return mock.Mock(returncode=0, stderr="", stdout="")

            with (
                self.stages(
                    page=mock.patch.object(
                        pdf.HtmlPage, "__call__", side_effect=render_document
                    ),
                ),
                mock.patch.object(pdf.RenderTools, "run", side_effect=render),
            ):
                pdf.PdfBuild()(source, output, project=project)
            self.assertEqual(output.read_bytes(), b"staged PDF")

    def test_inline_math_uses_its_actual_svg_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            svg = Path(folder) / "math.svg"
            svg.write_text(
                "<svg viewBox='0 5 8 10'><g id='math-baseline' data-y='12'/></svg>",
                encoding="utf-8",
            )
            self.assertEqual(pdf.MathSvg.depth(svg), 3)
            svg.write_text("<svg viewBox='0 5 8 10'/>", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "baseline"):
                pdf.MathSvg.depth(svg)

    def test_math_does_not_consume_code_or_escaped_currency(self):
        source = "Inline `$price$` and \\$5, but $x^2$ is math.\n\n```py\ntext = '$no$'\n```\n"
        protected, codes = pdf.MarkdownProtection.code(source)
        converted, maths = pdf.MarkdownProtection.math(protected)
        for token, original in codes.items():
            converted = converted.replace(token, original)
        self.assertEqual([value[0] for value in maths.values()], ["x^2"])
        self.assertIn("`$price$`", converted)
        self.assertIn("text = '$no$'", converted)
        self.assertIn("\\$5", converted)

    def test_image_inputs_must_exist_within_the_project(self):
        images = pdf.ImageDependencies()
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            source = project / "doc.md"
            image = project / "diagram.svg"
            image.write_text("<svg/>", encoding="utf-8")
            self.assertEqual(
                images("![diagram](diagram.svg)", source, project),
                (image,),
            )
            self.assertEqual(images("`![x](missing.svg)`", source, project), ())
            with self.assertRaisesRegex(ValueError, "local"):
                images("![x](https://example.com/img.png)", source, project)
            with self.assertRaisesRegex(ValueError, "missing or outside"):
                images('<img src="../missing.png">', source, project)

    def test_only_configured_documents_are_selected(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            (project / "config").mkdir()
            (project / "docs").mkdir()
            (project / "docs" / "selected.md").write_text(
                "# Selected", encoding="utf-8"
            )
            (project / "docs" / "unlisted.md").write_text(
                "# Unlisted", encoding="utf-8"
            )
            (project / "config" / "pdf_build.json").write_text(
                '{"documents":[{"source":"docs/selected.md","output":"docs/selected.pdf"}]}',
                encoding="utf-8",
            )
            with mock.patch.object(pdf.PdfBuild, "__call__") as build:
                self.assertEqual(pdf.main(["--project", str(project)]), 0)
            build.assert_called_once()
            self.assertEqual(
                build.call_args.args[:2],
                (project / "docs" / "selected.md", project / "docs" / "selected.pdf"),
            )

    def test_changed_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            source = project / "test.md"
            output = project / "test.pdf"
            source.write_text("# Test", encoding="utf-8")
            output.write_bytes(b"old output")

            def render(command, **_kwargs):
                """Stage the PDF while the user edits the output."""
                Path(self.print_target(command)).write_bytes(b"staged PDF")
                output.write_bytes(b"user edited output")
                return mock.Mock(returncode=0, stderr="", stdout="")

            with (
                self.stages(),
                mock.patch.object(pdf.RenderTools, "run", side_effect=render),
                self.assertRaisesRegex(RuntimeError, "output changed"),
            ):
                pdf.PdfBuild()(source, output, project=project)
            self.assertEqual(output.read_bytes(), b"user edited output")

    def test_changed_config_prevents_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            source = project / "test.md"
            output = project / "test.pdf"
            config = project / "config.json"
            source.write_text("# Test", encoding="utf-8")
            config.write_text("original", encoding="utf-8")
            digest = hashlib.sha256(config.read_bytes()).hexdigest()

            def render(command, **_kwargs):
                """Stage the PDF while the configuration changes."""
                Path(self.print_target(command)).write_bytes(b"staged PDF")
                config.write_text("changed", encoding="utf-8")
                return mock.Mock(returncode=0, stderr="", stdout="")

            with (
                self.stages(),
                mock.patch.object(pdf.RenderTools, "run", side_effect=render),
                self.assertRaisesRegex(RuntimeError, "inputs changed"),
            ):
                pdf.PdfBuild()(source, output, project=project, guards={config: digest})
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
