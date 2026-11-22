"""A private copy of a native project with its reviewed routing inputs."""

from __future__ import annotations

import shutil
from contextlib import contextmanager

from autopcb.jobs import isolated
from core.export_helpers import sha
from core.paths import ProjectStorage
from core.project import ProjectPaths, SourceSnapshot

EVIDENCE = ("POWER_LOAD_BUDGET.csv", "STACKUP_CURRENT.json")


class PrivateCopy:
    """Copy a design and its reviewed policy to where the routing engine reads them.

    Comparisons across copies stay meaningful only while those inputs are fixed,
    so a change during the run is an error.
    """

    def __init__(self, project: ProjectPaths, storage: ProjectStorage, snapshot: SourceSnapshot | None = None):
        self.project, self.snapshot = project, snapshot or SourceSnapshot.capture(project)
        caller = storage.legacy.relative_to(project.root)
        sources = {caller / "config/autopcb.json": storage.config_file("autopcb.json"),
                   **{caller / "design" / name: storage.data_file(name) for name in EVIDENCE}}
        self.inputs = {name: path for name, path in sources.items() if path.is_file()}
        self.hashes = {name: sha(path) for name, path in self.inputs.items()}

    @property
    def fingerprint(self) -> dict[str, str]:
        return {**self.snapshot.hashes, **{name.as_posix(): digest for name, digest in self.hashes.items()}}

    @contextmanager
    def open(self):
        """Yield the copied project and its storage; the sources stay untouched."""
        with isolated(self.project, self.snapshot) as working:
            for name, source in self.inputs.items():
                target = working.root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            yield working, ProjectStorage(working.root)
        if any(sha(self.inputs[name]) != digest for name, digest in self.hashes.items()):
            raise RuntimeError("Routing policy or design evidence changed during the run")
