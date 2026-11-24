"""test_review_publication.py: check review gallery publication.

Review publication preserves an existing gallery on failed updates.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock

from core.paths import long_path
from review import refresh_gallery


class ReviewPublicationTests(unittest.TestCase):
    """Tests for gallery currency checks and for publishing with rollback."""

    def current_gallery(self, root: Path):
        """Write a review gallery that matches its saved board and library.

        Inputs:
            root: existing folder.
        Outputs:
            A tuple (project folder, review folder, board hashes, library file path).
        """
        project = root / "project"
        review = project / "review"
        review.mkdir(parents=True)
        source = project / "v4.1.kicad_pcb"
        source.write_bytes(b"saved board")
        dependency = project / "library.kicad_mod"
        dependency.write_bytes(b"library")
        hashes = {source.name: refresh_gallery.digest(source)}
        manifest = {
            "sha256": hashes,
            "dependency_sha256": {dependency.name: refresh_gallery.digest(dependency)},
        }
        (review / "source.json").write_text(json.dumps(manifest), encoding="utf8")
        for name in refresh_gallery.PDFS:
            (review / name).write_bytes(b"%PDF-" + b"x" * 1024)
        for name in refresh_gallery.PNGS:
            (review / name).write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 1024)
        (review / "index.html").write_text(
            '<section id="x">' * len(refresh_gallery.PNGS), encoding="utf8"
        )
        (review / "index_standalone.html").write_text(
            "data:image/png;base64," * len(refresh_gallery.PNGS), encoding="utf8"
        )
        return project, review, hashes, dependency

    def test_current_gallery_requires_all_views_and_matching_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            project, review, hashes, dependency = self.current_gallery(Path(directory))
            self.assertTrue(
                refresh_gallery.ReviewGallery.is_current(project, review, hashes)
            )
            dependency.write_bytes(b"changed")
            self.assertFalse(
                refresh_gallery.ReviewGallery.is_current(project, review, hashes)
            )
            dependency.write_bytes(b"library")
            (review / "PCB_top.png").unlink()
            self.assertFalse(
                refresh_gallery.ReviewGallery.is_current(project, review, hashes)
            )

    def test_current_gallery_tracks_long_footprint_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            project, review, hashes, _ = self.current_gallery(Path(directory))
            library = project / "autopcb" / "libraries" / ("x" * 100 + ".pretty")
            library.mkdir(parents=True)
            footprint = long_path(library) / ("y" * 120 + ".kicad_mod")
            footprint.write_bytes(b"footprint library")
            try:
                self.assertFalse(
                    refresh_gallery.ReviewGallery.is_current(project, review, hashes)
                )
                manifest_path = review / "source.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                relative = footprint.relative_to(long_path(project)).as_posix()
                manifest["dependency_sha256"][relative] = refresh_gallery.digest(
                    footprint
                )
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                self.assertTrue(
                    refresh_gallery.ReviewGallery.is_current(project, review, hashes)
                )
            finally:
                footprint.unlink()

    def test_changing_dependency_defers_instead_of_rendering(self):
        with tempfile.TemporaryDirectory() as directory:
            project, review, hashes, dependency = self.current_gallery(Path(directory))
            original = refresh_gallery.digest
            calls = 0

            def changing(path):
                """Replace the digest so that the library changes during the check.

                The library digest is true on the first read and different on later
                reads. Every other file keeps its true digest.
                """
                nonlocal calls
                if path == long_path(dependency):
                    calls += 1
                    return original(path) if calls == 1 else "changed-during-check"
                return original(path)

            with (
                mock.patch.object(refresh_gallery, "digest", side_effect=changing),
                self.assertRaisesRegex(RuntimeError, "changed during"),
            ):
                refresh_gallery.ReviewGallery.is_current(project, review, hashes)

    def folders(self, root: Path) -> tuple[Path, Path]:
        """Create a review folder and a fresh folder that each hold a.png and b.png.

        Review files hold the text "old-" and the name, fresh files "new-" and the name.

        Inputs:
            root: existing folder.
        Outputs:
            A tuple (review folder, fresh folder).
        """
        review = root / "review"
        fresh = root / "fresh"
        review.mkdir()
        fresh.mkdir()
        for name in ("a.png", "b.png"):
            (review / name).write_bytes(f"old-{name}".encode())
            (fresh / name).write_bytes(f"new-{name}".encode())
        return review, fresh

    def test_publish_replaces_only_selected_files(self):
        with tempfile.TemporaryDirectory(prefix="jessie-review-test-") as directory:
            review, fresh = self.folders(Path(directory))
            snapshot = refresh_gallery.ReviewSnapshot.capture(
                review, {"a.png", "b.png"}
            )
            snapshot.publish(fresh)
            self.assertEqual((review / "a.png").read_bytes(), b"new-a.png")
            self.assertEqual((review / "b.png").read_bytes(), b"new-b.png")

    def test_external_change_is_not_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="jessie-review-test-") as directory:
            review, fresh = self.folders(Path(directory))
            snapshot = refresh_gallery.ReviewSnapshot.capture(
                review, {"a.png", "b.png"}
            )
            (review / "a.png").write_bytes(b"user edit")
            with self.assertRaisesRegex(RuntimeError, "changed during rendering"):
                snapshot.publish(fresh)
            self.assertEqual((review / "a.png").read_bytes(), b"user edit")
            self.assertEqual((review / "b.png").read_bytes(), b"old-b.png")

    def test_partial_failure_restores_originals(self):
        with tempfile.TemporaryDirectory(prefix="jessie-review-test-") as directory:
            review, fresh = self.folders(Path(directory))
            snapshot = refresh_gallery.ReviewSnapshot.capture(
                review, {"a.png", "b.png"}
            )
            original_write = refresh_gallery.atomic_write

            def fail_second(target: Path, content: bytes) -> None:
                """Replace the atomic write so that b.png fails.

                Writing b.png raises OSError; every other file is written.

                Inputs:
                    target: file to write.
                    content: bytes to write.
                """
                if target.name == "b.png":
                    raise OSError("simulated write failure")
                original_write(target, content)

            with mock.patch(
                "review.refresh_gallery.atomic_write", side_effect=fail_second
            ):
                with self.assertRaisesRegex(OSError, "simulated write failure"):
                    snapshot.publish(fresh)
            self.assertEqual((review / "a.png").read_bytes(), b"old-a.png")
            self.assertEqual((review / "b.png").read_bytes(), b"old-b.png")

    def test_rollback_preserves_intervening_user_edit(self):
        with tempfile.TemporaryDirectory(prefix="jessie-review-test-") as directory:
            review, fresh = self.folders(Path(directory))
            snapshot = refresh_gallery.ReviewSnapshot.capture(
                review, {"a.png", "b.png"}
            )
            original_write = refresh_gallery.atomic_write

            def edit_then_fail(target: Path, content: bytes) -> None:
                """Replace the atomic write so that b.png fails after a user edit.

                Writing b.png first edits a.png as a user would, then raises OSError;
                every other file is written.

                Inputs:
                    target: file to write.
                    content: bytes to write.
                """
                if target.name == "b.png":
                    (review / "a.png").write_bytes(b"intervening user edit")
                    raise OSError("simulated write failure")
                original_write(target, content)

            with mock.patch(
                "review.refresh_gallery.atomic_write", side_effect=edit_then_fail
            ):
                with self.assertRaisesRegex(RuntimeError, "could not safely restore"):
                    snapshot.publish(fresh)
            self.assertEqual((review / "a.png").read_bytes(), b"intervening user edit")


if __name__ == "__main__":
    unittest.main()
