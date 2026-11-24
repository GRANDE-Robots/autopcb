"""export_helpers.py: small deterministic export helpers. No CAD writes or purchases.

Reads and writes the manufacturing tables, checks that controlled inputs have
not changed, builds reproducible archives and derives the bill of materials.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from core.paths import data_path

FILES = {
    "bom": "02_BOM_COMPLETE.csv",
    "cpl": "03_CPL_COMPLETE.csv",
    "register": "06_EXACT_PART_REGISTER.csv",
}
REGISTER_FIELDS = [
    "Designator",
    "Comment",
    "Footprint",
    "Manufacturer",
    "MPN",
    "LCSC Part #",
    "Quantity",
    "Sourcing",
    "Assembly",
    "Side",
]
BOM_FIELDS = [
    "Comment",
    "Designator",
    "Footprint",
    "LCSC Part #",
    "Manufacturer",
    "MPN",
    "Quantity",
]
CPL_FIELDS = ["Designator", "Mid X", "Mid Y", "Rotation", "Layer"]


class Require:
    """Turns a failed condition into a RuntimeError."""

    def __call__(self, test, message):
        """Raise RuntimeError with ``message`` unless ``test`` is true."""
        if not test:
            raise RuntimeError(message)


class Sha:
    """Hashes a file."""

    def __call__(self, path):
        """Return the hex SHA-256 of the file at ``path``."""
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()


require = Require()
sha = Sha()


class ReadCsv:
    """Reads a manufacturing table."""

    def __call__(self, path):
        """Read a UTF-8 CSV file, with or without a BOM, as a list of row dicts."""
        with Path(path).open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))


class WriteCsv:
    """Writes a manufacturing table."""

    def __call__(self, path, rows, fields):
        """Write rows as a UTF-8 CSV with a BOM and Unix line endings.

        Inputs:
            path: Destination; its folder is created.
            rows: Dicts keyed by ``fields``.
            fields: Column names, in order.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)


class WriteJson:
    """Writes a deterministic JSON file."""

    def __call__(self, path, obj):
        """Write ``obj`` as indented, key-sorted JSON ending in a newline."""
        Path(path).write_text(
            json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf8"
        )


readcsv = ReadCsv()
writecsv = WriteCsv()
writejson = WriteJson()


class StableZip:
    """Builds a reproducible ZIP archive and proves what was published."""

    def __call__(self, path, entries):
        """Build/verify a temporary ZIP, then atomically publish and read it back.

        Inputs:
            path: The archive to create or replace.
            entries: (file path, archive name) pairs; names use ``/`` and may
                not be absolute, drive-qualified or contain ``..``.
        Outputs:
            None. The archive has sorted members, fixed timestamps and mode 644.
            Raises RuntimeError for unsafe or duplicate members, a CRC,
            inventory or byte mismatch, or an input that changed while being
            archived. If the published archive fails readback, the previous
            archive is restored.
        """
        path = Path(path)
        entries = sorted(
            [(Path(p), n.replace("\\", "/")) for p, n in entries], key=lambda x: x[1]
        )
        require(
            len({n for _, n in entries}) == len(entries), "Duplicate archive member"
        )
        for _, name in entries:
            require(
                name
                and not Path(name).is_absolute()
                and not Path(name).drive
                and ".." not in Path(name).parts,
                "Unsafe archive member",
            )
        expected = {}
        temporary = None
        previous = None
        published = False
        retain_previous = False

        def temporary_path():
            """Create an empty hidden temporary file beside the archive."""
            with tempfile.NamedTemporaryFile(
                prefix="." + path.name + ".",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as f:
                return Path(f.name)

        def verify(archive):
            """Check an archive's CRCs, inventory and bytes against its inputs."""
            with zipfile.ZipFile(archive) as z:
                require(z.testzip() is None, "Archive CRC failure")
                require(
                    z.namelist() == [n for _, n in entries],
                    "Archive inventory mismatch",
                )
                for p, n in entries:
                    require(
                        hashlib.sha256(z.read(n)).hexdigest() == expected[n],
                        "Archive byte mismatch: " + n,
                    )
                    require(sha(p) == expected[n], "Archive input changed: " + n)

        try:
            temporary = temporary_path()
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
            ) as z:
                for p, name in entries:
                    data = p.read_bytes()
                    expected[name] = hashlib.sha256(data).hexdigest()
                    info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o644 << 16
                    z.writestr(info, data)
            with temporary.open("r+b") as f:
                f.flush()
                os.fsync(f.fileno())
            verify(temporary)
            if path.exists():
                previous = temporary_path()
                shutil.copyfile(path, previous)
                require(
                    sha(previous) == sha(path),
                    "Previous archive changed during publication",
                )
            os.replace(temporary, path)
            temporary = None
            published = True
            verify(path)
        except BaseException:
            if published:
                if previous is not None:
                    try:
                        os.replace(previous, path)
                        previous = None
                    except OSError as error:
                        retain_previous = True
                        raise RuntimeError(
                            "Archive readback failed; previous archive preserved at "
                            + str(previous)
                        ) from error
                else:
                    path.unlink(missing_ok=True)
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if previous is not None and not retain_previous:
                previous.unlink(missing_ok=True)


stable_zip = StableZip()


class ControlledInput:
    """Chooses a controlled input file by its stable name."""

    def __call__(self, folder, stable, legacy_glob):
        """Prefer stable filename; legacy ambiguity must not silently pick a stale register.

        Outputs:
            The stable file when it exists, else the one legacy match, else
            None. Raises RuntimeError when several legacy files match.
        """
        p = Path(folder) / stable
        if p.exists():
            return p
        matches = list(Path(folder).glob(legacy_glob))
        require(len(matches) <= 1, "Ambiguous legacy inputs: " + str(matches))
        return matches[0] if matches else None


class FactoryInputDir:
    """Locates the reviewed, frozen factory inputs of a project."""

    def __call__(self, project):
        """Frozen control inputs are separate from regenerated manufacturing outputs.

        Outputs:
            The ``release_inputs`` folder. Raises RuntimeError when its manifest
            is missing, not marked REVIEWED, or any listed file's hash differs.
        """
        folder = data_path(Path(project), "release_inputs")
        manifest = folder / "INPUT_MANIFEST.json"
        require(
            manifest.exists(),
            (
                "Stage controlled factory inputs first with "
                "stage_release_inputs.py; generated manufacturing files "
                "must not be inputs"
            ),
        )
        document = json.loads(manifest.read_text(encoding="utf8"))
        status = document.get("status")
        require(
            isinstance(status, str) and status.strip().upper() == "REVIEWED",
            "Controlled factory inputs are on hold pending full review; "
            "status must be REVIEWED after the inputs are reviewed",
        )
        for name, digest in document["files"].items():
            require(
                sha(folder / name) == digest,
                "Controlled factory input changed; review/update its manifest explicitly: "
                + name,
            )
        return folder


class AssertCurrentInputs:
    """Proves recorded input hashes still match the files on disk."""

    def __call__(self, project, source, dependencies):
        """Require every recorded project file to exist, stay inside the project and match its hash.

        Inputs:
            project: Project folder.
            source: Relative path to hash, for the primary inputs.
            dependencies: Relative path to hash, for the supporting inputs.
        Outputs:
            None. Raises RuntimeError for a malformed or absolute entry, a path
            outside the project or missing, or a changed file.
        """
        root = Path(project).resolve()
        require(
            isinstance(source, dict) and isinstance(dependencies, dict),
            "Invalid input identity",
        )
        for collection in (source, dependencies):
            for rel, digest in collection.items():
                require(
                    isinstance(rel, str) and isinstance(digest, str),
                    "Invalid input identity",
                )
                relative = Path(rel)
                candidate = (root / relative).resolve()
                require(
                    not relative.is_absolute()
                    and candidate.is_relative_to(root)
                    and candidate.is_file(),
                    "Input identity must name a project file: " + rel,
                )
                require(
                    sha(candidate) == digest, "Source changed during export: " + rel
                )


class AssertFactorySources:
    """Proves the factory source identity is one schematic and one board, unchanged."""

    def __call__(self, project, source, dependencies):
        """Require exactly one schematic and one PCB in ``source``, then check all hashes."""
        require(isinstance(source, dict), "Invalid factory source identity")
        require(
            sum(
                isinstance(name, str) and name.endswith(".kicad_sch") for name in source
            )
            == 1
            and sum(
                isinstance(name, str) and name.endswith(".kicad_pcb") for name in source
            )
            == 1,
            "Factory identity must include one schematic and one PCB",
        )
        assert_current_inputs(project, source, dependencies)


controlled_input = ControlledInput()
factory_input_dir = FactoryInputDir()
assert_current_inputs = AssertCurrentInputs()
assert_factory_sources = AssertFactorySources()


class BomRows:
    """Derives bill-of-materials rows from the schematic and the exact-part register."""

    @staticmethod
    def natural(text):
        """A sort key that orders ``R2`` before ``R10``."""
        return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", text)]

    @staticmethod
    def exact_metadata(fields, fallback, ref, provenance):
        """Native field presence wins, including an intentionally empty field.

        Inputs:
            fields: The part's native schematic fields.
            fallback: The previous register row, used only when a native field
                is absent.
            ref: Reference designator, for messages.
            provenance: List that receives one record per field naming its source.
        Outputs:
            Manufacturer, MPN and LCSC part number. Raises RuntimeError when the
            manufacturer or MPN is blank; nothing is omitted automatically.
        """
        result = {}
        for key, aliases in [
            ("Manufacturer", ["Manufacturer"]),
            ("MPN", ["MPN"]),
            ("LCSC Part #", ["LCSC", "LCSC Part #"]),
        ]:
            present = next((a for a in aliases if a in fields), None)
            if present is not None:
                result[key] = fields[present] or ""
                origin = "schematic:" + present
            elif key in fallback:
                result[key] = fallback[key] or ""
                origin = "legacy register fallback (native field absent)"
            else:
                result[key] = ""
                origin = "absent"
            provenance.append(
                dict(
                    Designator=ref,
                    Field=key,
                    Value=result[key],
                    Source=origin,
                    Previous=fallback.get(key, ""),
                )
            )
        require(
            bool(result["MPN"].strip()),
            f"{ref}: missing/blank MPN; no automatic omission",
        )
        require(
            bool(result["Manufacturer"].strip()), f"{ref}: missing/blank Manufacturer"
        )
        return result

    @staticmethod
    def feature_kind(ref, fp, fields, listed):
        """Only actual fabricated features may be omitted; reference prefix alone is insufficient.

        Outputs:
            ``Fabricated mounting hole``, ``Fabricated test pad`` or ``Fabricated
            copper solder selector``, or None for a part with an MPN or any
            other footprint. Raises RuntimeError when a listed feature changed type.
        """
        if fields.get("MPN", "").strip():
            return None
        source = fields.get("Source_Footprint", "") + " " + fp
        if "MountingHole" in source:
            kind = "Fabricated mounting hole"
        elif "TestPoint_Pad" in source:
            kind = "Fabricated test pad"
        elif "SolderJumper" in source:
            kind = "Fabricated copper solder selector"
        else:
            return None
        if ref in listed:
            require(listed[ref]["Type"] == kind, f"{ref}: fabrication type changed")
        return kind

    @classmethod
    def group(cls, parts):
        """Group parts that share comment, footprint, LCSC number, manufacturer and MPN.

        Outputs:
            One row per group with sorted, comma-joined designators and the
            quantity, ordered by first designator.
        """
        groups = {}
        for r in parts:
            key = tuple(
                r[k]
                for k in ["Comment", "Footprint", "LCSC Part #", "Manufacturer", "MPN"]
            )
            groups.setdefault(key, []).append(r["Designator"])
        result = []
        for key, refs in groups.items():
            result.append(
                dict(
                    zip(
                        ["Comment", "Footprint", "LCSC Part #", "Manufacturer", "MPN"],
                        key,
                    ),
                    Designator=",".join(sorted(refs, key=cls.natural)),
                    Quantity=len(refs),
                )
            )
        return sorted(result, key=lambda r: cls.natural(r["Designator"].split(",")[0]))
