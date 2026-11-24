"""pcb_io.py: guarded, atomic PCB writes without overwriting unrelated files.

Also reads the native board objects the router and the audits share: copper
widths, copper boundaries, net classes and the enabled copper layers.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.compat import file_sha256


class TypedCopper:
    """Restores the native subtype that KiCad connectivity collections erase."""

    def __call__(self, item: Any) -> Any:
        """Return a via or arc cast to its own class; other items unchanged."""
        import pcbnew

        if isinstance(item, (pcbnew.PCB_VIA, pcbnew.PCB_ARC)):
            return item
        kind = item.Type()
        if kind == pcbnew.PCB_VIA_T:
            return pcbnew.Cast_to_PCB_VIA(item)
        if kind == pcbnew.PCB_ARC_T:
            return pcbnew.Cast_to_PCB_ARC(item)
        return item


typed_copper = TypedCopper()


class CopperWidth:
    """Reads the width of a track, arc or via."""

    def __call__(self, item: Any, *, layer: int | None = None) -> int:
        """Read track width safely; KiCad requires an explicit layer for a via.

        Inputs:
            item: A track, arc or via, possibly without its native subtype.
            layer: Copper layer id; required for a via.
        Outputs:
            Width in nanometres. Raises ValueError for a via without a layer.
        """
        import pcbnew

        item = typed_copper(item)
        if isinstance(item, pcbnew.PCB_VIA):
            if layer is None:
                raise ValueError("A copper layer is required when reading a via width")
            return int(item.GetWidth(layer))
        return int(item.GetWidth())


class CopperBoundary:
    """Finds the copper that touches a chain of tracks from outside it."""

    def __call__(self, board: Any, tracks: Iterable[Any]) -> dict[str, Any]:
        """Identify all physical copper contacts outside a selected track chain.

        Outputs:
            Items by UUID: the tracks, vias and pads connected to the chain
            that are not part of it.
        """
        connectivity = board.GetConnectivity()
        connectivity.Build(board)
        selected = tuple(tracks)
        internal = {item.m_Uuid.AsString() for item in selected}
        return {
            item.m_Uuid.AsString(): typed_copper(item)
            for track in selected
            for item in (
                *connectivity.GetConnectedTracks(track),
                *connectivity.GetConnectedPads(track),
            )
            if item.m_Uuid.AsString() not in internal
        }


class NetClasses:
    """Reads a board's net classes."""

    def __call__(self, board: Any) -> dict[str, Any]:
        """Normalize native wxString keys before ordinary Python dictionary lookup."""
        return {str(name): value for name, value in board.GetAllNetClasses().items()}


class EnabledCopperLayers:
    """Lists a board's enabled copper layers."""

    def __call__(self, board: Any) -> tuple[str, ...]:
        """Return only enabled physical copper layers, in stack order."""
        return tuple(
            str(board.GetLayerName(layer))
            for layer in board.GetEnabledLayers().CuStack()
        )


class DuplicateImageHandlerFilter:
    """Silences KiCad's repeated image-handler notice while a block runs."""

    @contextmanager
    def __call__(self):
        """Filter the duplicate-handler debug notice, retaining other wx logs."""
        import wx

        previous = wx.Log.GetActiveTarget()

        class ImageHandlerFilter(wx.Log):
            """A wx log target that drops the duplicate-handler notice."""

            def DoLogRecord(self, level, message, info):
                """Forward every record except the duplicate image-handler notice."""
                if (
                    level == wx.LOG_Debug
                    and message.startswith("Adding duplicate image handler for '")
                    and message.endswith("'")
                ):
                    return
                previous.LogRecord(level, message, info)

        target = ImageHandlerFilter()
        wx.Log.SetActiveTarget(target)
        try:
            yield
        finally:
            wx.Log.FlushActive()
            wx.Log.SetActiveTarget(previous)


class ProjectSettings:
    """Have KiCad write the complete project file beside a board.

    KiCad 10.0.8 saves boards without project settings and will not save them
    once that board is open, so write them before the board's first save.
    """

    def __init__(self, board: Path):
        """Name the project file that belongs to a board file."""
        self.path = str(Path(board).with_suffix(".kicad_pro"))

    def _read(self) -> Any:
        """The project file's JSON, or None when it is missing or not JSON."""
        try:
            return json.loads(Path(self.path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None

    @property
    def complete(self) -> bool:
        """Whether the project file already holds KiCad's net settings."""
        data = self._read()
        return isinstance(data, dict) and "net_settings" in data

    def write(self) -> None:
        """Have KiCad create the project file unless it is already complete.

        Outputs: None. Raises RuntimeError when KiCad cannot write it.
        """
        import pcbnew

        if self.complete:
            return
        if self._read() is None:
            # KiCad would migrate a non-JSON file as a legacy project, which crashes on Windows.
            Path(self.path).unlink(missing_ok=True)
        manager = pcbnew.GetSettingsManager()
        manager.LoadProject(
            self.path, False
        )  # False for a new project, which KiCad still creates
        project = manager.GetProject(self.path)
        saved = manager.SaveProject(self.path, project)
        manager.UnloadProject(project, False)
        if not saved:
            raise RuntimeError("KiCad failed to write the project settings")


@dataclass(frozen=True)
class BoardSaveGuard:
    """Bind an edit to the board and supporting files it was planned against."""

    source: Path
    fingerprints: tuple[tuple[Path, str], ...]

    @classmethod
    def capture(
        cls,
        source: Path,
        expected_sha256: str,
        dependencies: Iterable[Path] = (),
    ) -> BoardSaveGuard:
        """Record the board and its dependencies as they are now.

        Inputs:
            source: The ``.kicad_pcb`` file the edit was planned against.
            expected_sha256: The digest the plan saw for it.
            dependencies: Other files the plan relied on.
        Outputs:
            The guard. Raises ValueError for a missing or non-PCB source or a
            missing input, and RuntimeError when the board no longer matches
            ``expected_sha256`` or changes while being recorded.
        """
        source = source.resolve()
        if source.suffix.lower() != ".kicad_pcb" or not source.is_file():
            raise ValueError(f"Expected an existing PCB source: {source}")
        observed = file_sha256(source)
        if observed.lower() != expected_sha256.lower():
            raise RuntimeError("PCB source changed; reload and re-plan the edit.")
        paths = [source, *(path.resolve() for path in dependencies)]
        if any(not path.is_file() for path in paths):
            raise ValueError("A required PCB-edit input is missing.")
        fingerprints = tuple((path, file_sha256(path)) for path in paths)
        if fingerprints[0][1] != observed:
            raise RuntimeError("PCB source changed while recording the edit inputs.")
        return cls(source=source, fingerprints=fingerprints)

    def verify_unchanged(self) -> None:
        """Raise RuntimeError when any recorded file changed or vanished."""
        for path, expected in self.fingerprints:
            if not path.is_file() or file_sha256(path) != expected:
                raise RuntimeError(f"PCB-edit input changed; refusing to save: {path}")

    def verify_editor_unlocked(self) -> None:
        """Refuse in-place writes while KiCad has the board open."""
        lock = self.source.with_name(f"~{self.source.name}.lck")
        if lock.exists():
            raise RuntimeError(
                f"KiCad board lock present; refusing to overwrite: {lock}"
            )

    def save(
        self,
        board: Any,
        destination: Path,
        save_board: Callable[[str, Any], object],
        load_board: Callable[[str], object] | None = None,
    ) -> str:
        """Save through a temporary file and never overwrite a different file.

        Inputs:
            board: The edited native board.
            destination: A new ``.kicad_pcb`` path, or the guarded source itself.
            save_board: Writes the board to a path; ``save_board(path, board)``.
            load_board: Reloads a path, to prove the candidate is readable.
        Outputs:
            The SHA-256 of the saved file. Raises RuntimeError when the inputs
            changed, the editor holds the board, or KiCad produces no file,
            an unreadable file or a failed readback, ValueError for a bad
            destination and FileExistsError for another existing file.
        """
        destination = destination.resolve()
        if (
            destination.suffix.lower() != ".kicad_pcb"
            or not destination.parent.is_dir()
        ):
            raise ValueError(
                f"Expected a PCB destination in an existing folder: {destination}"
            )
        if destination != self.source and destination.exists():
            raise FileExistsError(f"Refusing to overwrite another file: {destination}")
        self.verify_unchanged()
        if destination == self.source:
            self.verify_editor_unlocked()
        # KiCad creates project and local-settings sidecars when saving a
        # board. Keep them outside the synced project; only stage the finished
        # PCB bytes on the destination volume for the atomic replacement.
        temp_root = Path(tempfile.gettempdir()).resolve()
        temporary_dir = Path(
            tempfile.mkdtemp(prefix="jessie-pcb-save-", dir=temp_root)
        ).resolve()
        if temporary_dir.parent != temp_root or not temporary_dir.name.startswith(
            "jessie-pcb-save-"
        ):
            raise RuntimeError(f"Unexpected PCB temporary directory: {temporary_dir}")
        temporary = temporary_dir / "candidate.kicad_pcb"
        try:
            result = save_board(str(temporary), board)
            if (
                result is False
                or not temporary.is_file()
                or temporary.stat().st_size == 0
            ):
                raise RuntimeError("KiCad did not produce a nonempty PCB file.")
            if load_board is not None and load_board(str(temporary)) is None:
                raise RuntimeError("KiCad could not reload the saved PCB candidate.")
            content = temporary.read_bytes()
            self.verify_unchanged()
            if destination == self.source:
                self.verify_editor_unlocked()
                fd, staged_name = tempfile.mkstemp(
                    prefix=".board-write-", suffix=".tmp", dir=destination.parent
                )
                staged = Path(staged_name)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    if staged.read_bytes() != content:
                        raise RuntimeError("Staged PCB failed readback.")
                    self.verify_unchanged()
                    self.verify_editor_unlocked()
                    os.replace(staged, destination)
                finally:
                    if staged.exists():
                        staged.unlink()
            else:
                with destination.open("xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            if destination.read_bytes() != content:
                raise RuntimeError(f"Saved PCB failed readback: {destination}")
            return hashlib.sha256(content).hexdigest()
        finally:
            if temporary_dir.exists():
                if temporary_dir.resolve().parent != temp_root:
                    raise RuntimeError(
                        "PCB temporary directory moved outside the temp root."
                    )
                shutil.rmtree(temporary_dir)


copper_width = CopperWidth()
copper_boundary = CopperBoundary()
net_classes = NetClasses()
enabled_copper_layers = EnabledCopperLayers()
quiet_duplicate_image_handlers = DuplicateImageHandlerFilter()
