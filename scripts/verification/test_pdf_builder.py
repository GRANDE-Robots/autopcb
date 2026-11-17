"""Check configured PDF builds without modifying project documents."""

from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from utils import build_pdf as pdf


@unittest.skipUnless(
    importlib.util.find_spec("markdown_it"), "PDF tests require markdown-it-py"
)
class PDFBuilderTests(unittest.TestCase):
    def setUp(self):
        qt = mock.patch.object(pdf, "qt_renderer_available", return_value=False)
        qt.start()
        self.addCleanup(qt.stop)

    def test_qt_is_used_when_available(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            source, output = project / "test.md", project / "test.pdf"
            source.write_text("# Test", encoding="utf-8")

            def render(command, **_kwargs):
                self.assertEqual(Path(command[2]).name, "qt_pdf.py")
                Path(command[4]).write_bytes(b"Qt PDF")
                return mock.Mock(returncode=0, stderr="", stdout="")

            with (
                mock.patch.object(pdf, "qt_renderer_available", return_value=True),
                mock.patch.object(pdf, "require_chrome") as chrome,
                mock.patch.object(pdf, "require_tool", return_value="pdfinfo"),
                mock.patch.object(pdf, "render_html", return_value=("Test", "<html/>")),
                mock.patch.object(pdf, "validate_pdf"),
                mock.patch.object(pdf, "run", side_effect=render),
            ):
                pdf.build_pdf(source, output, project=project)
            chrome.assert_not_called()
            self.assertEqual(output.read_bytes(), b"Qt PDF")

    def test_invalid_qt_output_falls_back_without_publishing_it(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            source, output = project / "test.md", project / "test.pdf"
            source.write_text("# Test", encoding="utf-8")
            output.write_bytes(b"old output")

            def render(command, **_kwargs):
                if Path(command[2]).name == "qt_pdf.py":
                    Path(command[4]).write_bytes(b"invalid Qt PDF")
                    return mock.Mock(returncode=0, stderr="", stdout="")
                target = next(
                    arg.split("=", 1)[1]
                    for arg in command
                    if arg.startswith("--print-to-pdf=")
                )
                self.assertFalse(Path(target).exists())
                return mock.Mock(returncode=1, stderr="browser failed", stdout="")

            with (
                mock.patch.object(pdf, "qt_renderer_available", return_value=True),
                mock.patch.object(pdf, "require_chrome", return_value="chrome"),
                mock.patch.object(pdf, "require_tool", return_value="pdfinfo"),
                mock.patch.object(pdf, "render_html", return_value=("Test", "<html/>")),
                mock.patch.object(pdf, "validate_pdf", side_effect=RuntimeError("bad PDF")),
                mock.patch.object(pdf, "run", side_effect=render),
            ):
                with self.assertRaisesRegex(RuntimeError, "browser failed"):
                    pdf.build_pdf(source, output, project=project)
            self.assertEqual(output.read_bytes(), b"old output")

    def test_crlf_input_is_normalized_for_math_rendering(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            source, output = project / "test.md", project / "test.pdf"
            source.write_bytes(b"# Test\r\n\r\n$$\r\nx+y\r\n$$\r\n")

            def render_document(value, *_args):
                self.assertNotIn("\r", value)
                self.assertIn("$$\nx+y\n$$", value)
                return "Test", "<html/>"

            def render(command, **_kwargs):
                target = next(
                    arg.split("=", 1)[1]
                    for arg in command
                    if arg.startswith("--print-to-pdf=")
                )
                Path(target).write_bytes(b"staged PDF")
                return mock.Mock(returncode=0, stderr="", stdout="")

            with (
                mock.patch.object(pdf, "require_chrome", return_value="chrome"),
                mock.patch.object(pdf, "require_tool", return_value="pdfinfo"),
                mock.patch.object(pdf, "render_html", side_effect=render_document),
                mock.patch.object(pdf, "validate_pdf"),
                mock.patch.object(pdf, "run", side_effect=render),
            ):
                pdf.build_pdf(source, output, project=project)
            self.assertEqual(output.read_bytes(), b"staged PDF")

    def test_inline_math_uses_its_actual_svg_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            svg = Path(folder) / "math.svg"
            svg.write_text(
                "<svg viewBox='0 5 8 10'><g id='math-baseline' data-y='12'/></svg>",
                encoding="utf-8",
            )
            self.assertEqual(pdf.inline_depth(svg), 3)
            svg.write_text("<svg viewBox='0 5 8 10'/>", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "baseline"):
                pdf.inline_depth(svg)

    def test_math_does_not_consume_code_or_escaped_currency(self):
        source = "Inline `$price$` and \\$5, but $x^2$ is math.\n\n```py\ntext = '$no$'\n```\n"
        protected, codes = pdf.protect_code(source)
        converted, maths = pdf.protect_math(protected)
        for token, original in codes.items():
            converted = converted.replace(token, original)
        self.assertEqual([value[0] for value in maths.values()], ["x^2"])
        self.assertIn("`$price$`", converted)
        self.assertIn("text = '$no$'", converted)
        self.assertIn("\\$5", converted)

    def test_image_inputs_must_exist_within_the_project(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            source = project / "doc.md"
            image = project / "diagram.svg"
            image.write_text("<svg/>", encoding="utf-8")
            self.assertEqual(
                pdf.image_dependencies("![diagram](diagram.svg)", source, project),
                (image,),
            )
            self.assertEqual(
                pdf.image_dependencies("`![x](missing.svg)`", source, project), ()
            )
            with self.assertRaisesRegex(ValueError, "local"):
                pdf.image_dependencies(
                    "![x](https://example.com/img.png)", source, project
                )
            with self.assertRaisesRegex(ValueError, "missing or outside"):
                pdf.image_dependencies('<img src="../missing.png">', source, project)

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
            with mock.patch.object(pdf, "build_pdf") as build:
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
                target = next(
                    arg.split("=", 1)[1]
                    for arg in command
                    if arg.startswith("--print-to-pdf=")
                )
                Path(target).write_bytes(b"staged PDF")
                output.write_bytes(b"user edited output")
                return mock.Mock(returncode=0, stderr="", stdout="")

            with mock.patch.object(pdf, "require_chrome", return_value="chrome"):
                with mock.patch.object(pdf, "require_tool", return_value="pdfinfo"):
                    with mock.patch.object(
                        pdf, "render_html", return_value=("Test", "<html/>")
                    ):
                        with mock.patch.object(pdf, "validate_pdf"):
                            with mock.patch.object(pdf, "run", side_effect=render):
                                with self.assertRaisesRegex(
                                    RuntimeError, "output changed"
                                ):
                                    pdf.build_pdf(source, output, project=project)
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
                target = next(
                    arg.split("=", 1)[1]
                    for arg in command
                    if arg.startswith("--print-to-pdf=")
                )
                Path(target).write_bytes(b"staged PDF")
                config.write_text("changed", encoding="utf-8")
                return mock.Mock(returncode=0, stderr="", stdout="")

            with mock.patch.object(pdf, "require_chrome", return_value="chrome"):
                with mock.patch.object(pdf, "require_tool", return_value="pdfinfo"):
                    with mock.patch.object(
                        pdf, "render_html", return_value=("Test", "<html/>")
                    ):
                        with mock.patch.object(pdf, "validate_pdf"):
                            with mock.patch.object(pdf, "run", side_effect=render):
                                with self.assertRaisesRegex(
                                    RuntimeError, "inputs changed"
                                ):
                                    pdf.build_pdf(
                                        source,
                                        output,
                                        project=project,
                                        guards={config: digest},
                                    )
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
