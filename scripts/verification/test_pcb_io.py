"""test_pcb_io.py: check the PCB save guards.

PCB save guards preserve the source and reject stale edits.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from core import pcb_io
from core.pcb_io import BoardSaveGuard, copper_width


class BoardSaveGuardTests(unittest.TestCase):
    """Tests for the guarded board save and the KiCad image handler filter."""

    def test_connectivity_base_wrapper_via_width_requires_explicit_layer(self):
        import pcbnew

        board = pcbnew.BOARD()
        net = pcbnew.NETINFO_ITEM(board, "N")
        board.Add(net)
        via = pcbnew.PCB_VIA(board)
        via.SetNetCode(net.GetNetCode())
        via.SetPosition(pcbnew.VECTOR2I(1000000, 1000000))
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetWidth(600000)
        via.SetDrill(300000)
        board.Add(via)
        track = pcbnew.PCB_TRACK(board)
        track.SetNetCode(net.GetNetCode())
        track.SetLayer(pcbnew.F_Cu)
        track.SetStart(via.GetPosition())
        track.SetEnd(pcbnew.VECTOR2I(2000000, 1000000))
        track.SetWidth(200000)
        board.Add(track)
        connection = board.GetConnectivity()
        connection.Build(board)
        wrapped = next(
            x
            for x in connection.GetConnectedTracks(track)
            if x.Type() == pcbnew.PCB_VIA_T
        )
        self.assertEqual(wrapped.GetClass(), "PCB_VIA")
        with self.assertRaisesRegex(ValueError, "layer is required"):
            copper_width(wrapped)
        self.assertEqual(copper_width(wrapped, layer=pcbnew.F_Cu), 600000)

    def test_kicad_image_handler_filter_preserves_other_warnings(self):
        import wx

        messages = []

        class Collector(wx.Log):
            """wx log target that collects each record as a (level, message) pair."""

            def DoLogRecord(self, level, message, info):
                """Append the level and message to the collected messages."""
                messages.append((level, message))

        quiet = getattr(pcb_io, "quiet_duplicate_image_handlers", None)
        self.assertIsNotNone(quiet, "A scoped KiCad log filter is required")
        collector = Collector()
        previous = wx.Log.SetActiveTarget(collector)
        try:
            with quiet():
                wx.LogDebug("Adding duplicate image handler for 'PNG file'")
                wx.LogWarning("Adding duplicate image handler for 'PNG file'")
                wx.LogWarning("A real KiCad warning")
                wx.Log.FlushActive()
            wx.LogWarning("Warning after the filter is restored")
            wx.Log.FlushActive()
        finally:
            wx.Log.SetActiveTarget(previous)
        self.assertEqual(
            messages,
            [
                (wx.LOG_Warning, "Adding duplicate image handler for 'PNG file'"),
                (wx.LOG_Warning, "A real KiCad warning"),
                (wx.LOG_Warning, "Warning after the filter is restored"),
            ],
        )

    def test_via_width_requires_a_layer_before_calling_kicad(self):
        import pcbnew

        board = pcbnew.BOARD()
        via = pcbnew.PCB_VIA(board)
        track = pcbnew.PCB_TRACK(board)
        track.SetWidth(pcbnew.FromMM(0.2))
        with self.assertRaisesRegex(ValueError, "layer is required"):
            copper_width(via)
        self.assertEqual(
            copper_width(via, layer=pcbnew.F_Cu), via.GetWidth(pcbnew.F_Cu)
        )
        self.assertEqual(copper_width(track), pcbnew.FromMM(0.2))

    def board_file(self, directory: str) -> Path:
        """Write a source board holding b"original" and return its path.

        Inputs:
            directory: existing folder.
        """
        path = Path(directory) / "source.kicad_pcb"
        path.write_bytes(b"original")
        return path

    @staticmethod
    def digest(path: Path) -> str:
        """Return the SHA-256 hex digest of the file."""
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def save_new(path: str, _board: object) -> bool:
        """Stand-in for the native save that writes b"updated" and reports success."""
        Path(path).write_bytes(b"updated")
        return True

    def test_in_place_save_is_atomic_and_source_bound(self):
        with tempfile.TemporaryDirectory(prefix="jessie-pcb-test-") as directory:
            source = self.board_file(directory)
            guard = BoardSaveGuard.capture(source, self.digest(source))
            result = guard.save(object(), source, self.save_new)
            self.assertEqual(source.read_bytes(), b"updated")
            self.assertEqual(result, self.digest(source))
            self.assertEqual(list(Path(directory).glob(".board-save-*")), [])

    def test_external_board_change_is_never_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="jessie-pcb-test-") as directory:
            source = self.board_file(directory)
            guard = BoardSaveGuard.capture(source, self.digest(source))

            def change_source(path: str, _board: object) -> bool:
                """Stand-in for the native save that also edits the source.

                The candidate is written, then the source file is changed as a user
                edit, and success is reported.
                """
                Path(path).write_bytes(b"candidate")
                source.write_bytes(b"user edit")
                return True

            with self.assertRaisesRegex(RuntimeError, "input changed"):
                guard.save(object(), source, change_source)
            self.assertEqual(source.read_bytes(), b"user edit")
            self.assertEqual(list(Path(directory).glob(".board-save-*")), [])

    def test_open_board_lock_blocks_in_place_save(self):
        with tempfile.TemporaryDirectory(prefix="jessie-pcb-test-") as directory:
            source = self.board_file(directory)
            guard = BoardSaveGuard.capture(source, self.digest(source))
            lock = source.with_name(f"~{source.name}.lck")
            lock.write_text("editor open", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "lock"):
                guard.save(object(), source, self.save_new)
            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(lock.read_text(encoding="utf-8"), "editor open")

    def test_board_lock_appearing_during_serialization_blocks_save(self):
        with tempfile.TemporaryDirectory(prefix="jessie-pcb-test-") as directory:
            source = self.board_file(directory)
            guard = BoardSaveGuard.capture(source, self.digest(source))
            lock = source.with_name(f"~{source.name}.lck")

            def open_board(path: str, _board: object) -> bool:
                """Stand-in for the native save that also creates the editor lock.

                The candidate is written, then the editor lock file is created, and
                success is reported.
                """
                Path(path).write_bytes(b"candidate")
                lock.write_text("editor open", encoding="utf-8")
                return True

            with self.assertRaisesRegex(RuntimeError, "lock"):
                guard.save(object(), source, open_board)
            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(list(Path(directory).glob(".board-save-*")), [])

    def test_other_existing_destination_is_never_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="jessie-pcb-test-") as directory:
            source = self.board_file(directory)
            other = Path(directory) / "existing.kicad_pcb"
            other.write_bytes(b"unrelated")
            guard = BoardSaveGuard.capture(source, self.digest(source))
            with self.assertRaises(FileExistsError):
                guard.save(object(), other, self.save_new)
            self.assertEqual(other.read_bytes(), b"unrelated")
            self.assertEqual(source.read_bytes(), b"original")

    def test_failed_serialization_leaves_source_and_no_temp_file(self):
        with tempfile.TemporaryDirectory(prefix="jessie-pcb-test-") as directory:
            source = self.board_file(directory)
            guard = BoardSaveGuard.capture(source, self.digest(source))

            def fail(_path: str, _board: object) -> bool:
                """Stand-in for the native save that raises RuntimeError."""
                raise RuntimeError("serialization failed")

            with self.assertRaisesRegex(RuntimeError, "serialization failed"):
                guard.save(object(), source, fail)
            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(list(Path(directory).glob(".board-save-*")), [])

    def test_kicad_can_save_and_reload_a_synthetic_board(self):
        import pcbnew

        with tempfile.TemporaryDirectory(prefix="jessie-pcb-test-") as directory:
            source = self.board_file(directory)
            guard = BoardSaveGuard.capture(source, self.digest(source))
            guard.save(pcbnew.BOARD(), source, pcbnew.SaveBoard, pcbnew.LoadBoard)
            self.assertEqual(list(Path(directory).glob(".board-save-*")), [])
            self.assertEqual(list(Path(directory).glob(".board-write-*")), [])
            self.assertIsNotNone(pcbnew.LoadBoard(str(source)))

    def test_kicad_sidecars_are_isolated_from_the_project_folder(self):
        with tempfile.TemporaryDirectory(prefix="jessie-pcb-test-") as directory:
            source = self.board_file(directory)
            guard = BoardSaveGuard.capture(source, self.digest(source))

            def save_with_sidecars(path: str, _board: object) -> bool:
                """Stand-in for the native save that also writes KiCad sidecar files.

                The candidate must lie in a private folder below the system temporary
                folder. It is written together with temporary project and settings
                sidecars, and success is reported.
                """
                candidate = Path(path)
                self.assertEqual(
                    candidate.parent.parent, Path(tempfile.gettempdir()).resolve()
                )
                self.assertNotEqual(candidate.parent, source.parent)
                candidate.write_bytes(b"candidate")
                candidate.with_suffix(".kicad_pro").write_bytes(b"temporary project")
                candidate.with_suffix(".kicad_prl").write_bytes(b"temporary settings")
                return True

            guard.save(object(), source, save_with_sidecars, lambda _path: object())
            self.assertEqual(source.read_bytes(), b"candidate")
            self.assertEqual(list(Path(directory).glob(".board-save-*")), [])
            self.assertEqual(list(Path(directory).glob(".board-write-*")), [])

    def test_failed_readback_removes_generated_project_sidecars(self):
        with tempfile.TemporaryDirectory(prefix="jessie-pcb-test-") as directory:
            source = self.board_file(directory)
            guard = BoardSaveGuard.capture(source, self.digest(source))

            def save_with_sidecars(path: str, _board: object) -> bool:
                """Stand-in for the native save that also writes KiCad sidecar files.

                The candidate is written together with temporary project and settings
                sidecars, and success is reported.
                """
                candidate = Path(path)
                candidate.write_bytes(b"candidate")
                candidate.with_suffix(".kicad_pro").write_bytes(b"temporary project")
                candidate.with_suffix(".kicad_prl").write_bytes(b"temporary settings")
                return True

            with self.assertRaisesRegex(RuntimeError, "could not reload"):
                guard.save(object(), source, save_with_sidecars, lambda _path: None)
            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(list(Path(directory).glob(".board-save-*")), [])

    def test_unreadable_candidate_is_not_published(self):
        with tempfile.TemporaryDirectory(prefix="jessie-pcb-test-") as directory:
            source = self.board_file(directory)
            guard = BoardSaveGuard.capture(source, self.digest(source))
            with self.assertRaisesRegex(RuntimeError, "could not reload"):
                guard.save(object(), source, self.save_new, lambda _path: None)
            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(list(Path(directory).glob(".board-save-*")), [])

    def test_changed_schematic_dependency_blocks_board_save(self):
        with tempfile.TemporaryDirectory(prefix="jessie-pcb-test-") as directory:
            source = self.board_file(directory)
            schematic = Path(directory) / "source.kicad_sch"
            schematic.write_bytes(b"schematic")
            guard = BoardSaveGuard.capture(source, self.digest(source), [schematic])
            schematic.write_bytes(b"external edit")
            with self.assertRaisesRegex(RuntimeError, "input changed"):
                guard.save(object(), source, self.save_new)
            self.assertEqual(source.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
