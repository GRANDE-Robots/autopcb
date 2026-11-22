"""Stand-ins for Python 3.10+ features: KiCad's macOS bundle ships Python 3.9."""

import itertools


class StrictZip:
    """zip(..., strict=True): pair items and refuse iterables of different lengths."""

    _missing = object()

    def __call__(self, *iterables):
        for items in itertools.zip_longest(*iterables, fillvalue=self._missing):
            if any(item is self._missing for item in items):
                raise ValueError("strict_zip() arguments have different lengths")
            yield items


strict_zip = StrictZip()
