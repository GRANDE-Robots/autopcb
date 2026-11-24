"""private_copy.py: a private copy of a native project with its reviewed routing inputs.

The copy lets runs be compared on identical inputs without touching the sources.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import json
import shutil
from contextlib import contextmanager

from autopcb.jobs import IsolatedCopy
from core.export_helpers import sha
from core.paths import ProjectStorage
from core.project import ProjectPaths, SourceSnapshot

EVIDENCE = ("POWER_LOAD_BUDGET.csv", "STACKUP_CURRENT.json")


class PrivateCopy:
    """Copy a design and its reviewed policy to where the routing engine reads them.

    Comparisons across copies stay meaningful only while those inputs are fixed,
    so a change during the run is an error.
    """

    def __init__(
        self,
        project: ProjectPaths,
        storage: ProjectStorage,
        snapshot: SourceSnapshot | None = None,
    ):
        """Record the project and the reviewed inputs the routing engine reads.

        Inputs:
            project: Native project to copy.
            storage: Storage that holds the routing policy and design evidence.
            snapshot: Hashes of the saved sources; captured now when None.
        """
        self.project, self.snapshot = (
            project,
            snapshot or SourceSnapshot.capture(project),
        )
        caller = storage.legacy.relative_to(project.root)
        self.policy = caller / "config/autopcb.json"
        sources = {
            self.policy: storage.config_file("autopcb.json"),
            **{caller / "design" / name: storage.data_file(name) for name in EVIDENCE},
        }
        self.inputs = {name: path for name, path in sources.items() if path.is_file()}
        self.hashes = {name: sha(path) for name, path in self.inputs.items()}

    @property
    def fingerprint(self) -> dict[str, str]:
        """The hash of every source file and of each reviewed input, by relative path."""
        return {
            **self.snapshot.hashes,
            **{name.as_posix(): digest for name, digest in self.hashes.items()},
        }

    @contextmanager
    def open(self, recovery: dict | None = None):
        """Yield the copied project and its storage; the sources stay untouched.

        ``recovery`` overrides recovery settings in the copy's policy only.
        """
        with IsolatedCopy.open(self.project, self.snapshot) as working:
            for name, source in self.inputs.items():
                target = working.root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            if recovery and self.policy in self.inputs:
                policy = working.root / self.policy
                values = json.loads(policy.read_text(encoding="utf-8-sig"))
                values["recovery"] = {**values.get("recovery", {}), **recovery}
                policy.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
            yield working, ProjectStorage(working.root)
        if any(
            sha(self.inputs[name]) != digest for name, digest in self.hashes.items()
        ):
            raise RuntimeError(
                "Routing policy or design evidence changed during the run"
            )
