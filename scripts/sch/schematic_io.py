"""schematic_io.py: exact reads, atomic writes and digests of KiCad text files.

The schematic tools share these so that each reads a file byte for byte and
replaces it only through a complete temporary copy.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class ExactText:
    """Reads a text file without translating its line endings."""

    @staticmethod
    def read(path: Path) -> str:
        """Return the UTF-8 text of ``path`` with CRLF and LF kept as stored."""
        with path.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()


class FileDigest:
    """Fingerprints a file so a run can report what it read and wrote."""

    @staticmethod
    def sha256(path: Path) -> str:
        """Return the hexadecimal SHA-256 of the bytes of ``path``."""
        return hashlib.sha256(path.read_bytes()).hexdigest()


class AtomicTextWriter:
    """Replaces a file through a flushed temporary copy beside it.

    Inputs:
        suffix: Text appended to the destination's name for the temporary copy,
            for example ``.labels.tmp``; it names the tool that wrote it.
    """

    def __init__(self, suffix: str) -> None:
        """Remember the temporary-file suffix."""
        self.suffix = suffix

    def write(self, path: Path, content: str) -> None:
        """Write ``content`` as UTF-8 and replace ``path`` with it.

        Inputs:
            path: Destination file; its folder must exist.
            content: Full new text, written without newline translation.
        Outputs:
            None. The temporary copy is flushed and synced before the swap, and
            is removed again if the write or swap fails.
        """
        temporary = path.with_suffix(path.suffix + self.suffix)
        try:
            with temporary.open("w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


class SiblingTempWriter:
    """Replaces a file through an anonymous temporary file in its folder."""

    @staticmethod
    def write(path: Path, content: str) -> None:
        """Write ``content`` as UTF-8 to a new file beside ``path`` and swap it in.

        Inputs:
            path: Destination file; its folder must exist.
            content: Full new text, written without newline translation.
        Outputs:
            None. A failure before the swap leaves the temporary file behind.
        """
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
        ) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        os.replace(temporary, path)
