"""compat.py: stand-ins for Python 3.10+ features, since KiCad's macOS bundle ships Python 3.9.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import hashlib
import itertools


class StrictZip:
    """zip(..., strict=True): pair items and refuse iterables of different lengths."""

    _missing = object()

    def __call__(self, *iterables):
        """Yield tuples of one item from each iterable.

        Outputs: A generator. Raises ValueError, when reached, if the iterables
        differ in length.
        """
        for items in itertools.zip_longest(*iterables, fillvalue=self._missing):
            if any(item is self._missing for item in items):
                raise ValueError("strict_zip() arguments have different lengths")
            yield items


class FileSha256:
    """hashlib.file_digest(stream, "sha256").hexdigest() for a path, read in 1 MiB blocks."""

    def __call__(self, path) -> str:
        """Return the hex SHA-256 of the file at ``path``."""
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()


strict_zip = StrictZip()
file_sha256 = FileSha256()
