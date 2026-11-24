"""assembly_rotation.py: qualified JLC catalog-angle adjustments; native positions/CAD stay intact.

A rotation policy lists the parts whose factory catalog angle differs from the
native angle by a quarter turn, each backed by hashed qualification evidence.
This module validates the policy and applies it to a placement list without
touching the native design.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import json
import math
import re
from pathlib import Path

from core.paths import data_path, data_reference

from core.export_helpers import require, sha

POLICY_FILE = "ASSEMBLY_ROTATION_OFFSETS.json"
SCHEDULE_FIELDS = [
    "Designator",
    "Native rotation deg",
    "Factory rotation deg",
    "Offset deg",
    "Layer",
    "Manufacturer",
    "MPN",
    "LCSC Part #",
    "Footprint",
    "Native geometry SHA256",
    "Qualification evidence",
    "Qualification SHA256",
]


class AssemblyRotation:
    """Validates, loads and applies the assembly rotation policy."""

    @staticmethod
    def number(value, label):
        """Read a finite angle in degrees.

        Outputs:
            The angle as a float. Raises RuntimeError, naming ``label``, for a
            boolean, a non-number or a non-finite value.
        """
        require(not isinstance(value, bool), label + ": boolean is not an angle")
        try:
            result = float(value)
        except (TypeError, ValueError) as error:
            raise RuntimeError(label + ": invalid angle") from error
        require(math.isfinite(result), label + ": nonfinite angle")
        return result

    @classmethod
    def validate(cls, policy):
        """Check a policy's scope, convention and every override.

        Outputs:
            The override list. Raises RuntimeError when the scope or schema is
            wrong, an override is unqualified, repeated, not a nonzero quarter
            turn, lacks a native rotation guard, or lacks source-controlled
            evidence with a SHA-256.
        """
        require(
            policy.get("schema") == 1
            and policy.get("export_scope") == "JLCPCB_FACTORY_CPL",
            "Unsupported assembly rotation policy scope/schema",
        )
        require(
            policy.get("rotation_convention") == "ADD_TO_NATIVE_DEGREES_MOD_360"
            and policy.get("native_geometry_unchanged") is True,
            "Rotation policy must preserve native geometry and an explicit additive convention",
        )
        entries = policy.get("overrides")
        require(
            isinstance(entries, list), "Rotation overrides must be an explicit list"
        )
        refs = [r.get("designator") for r in entries]
        require(
            all(isinstance(r, str) and r for r in refs) and len(refs) == len(set(refs)),
            "Missing/duplicate rotation override reference",
        )
        for rule in entries:
            ref = rule["designator"]
            require(
                rule.get("qualified") is True
                and bool(str(rule.get("qualification_note", "")).strip()),
                ref + ": catalog orientation is not qualified",
            )
            offset = cls.number(rule.get("offset_deg"), ref + " offset")
            require(
                offset % 90 == 0 and offset % 360 != 0,
                ref + ": override must be a nonzero quarter-turn offset",
            )
            for field in (
                "Manufacturer",
                "MPN",
                "LCSC Part #",
                "Footprint",
                "Layer",
                "Rotation",
                "Native_geometry_sha256",
            ):
                require(
                    field in rule.get("expected", {}),
                    ref + ": missing native rotation guard " + field,
                )
            evidence = rule.get("qualification_evidence", {})
            path = Path(str(evidence.get("path", "")))
            require(
                len(path.parts) > 1
                and not path.anchor
                and ".." not in path.parts
                and path.parts[0] in {"data", "design"}
                and bool(
                    re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("sha256", "")))
                ),
                ref + ": missing/unsafe source-controlled qualification evidence",
            )
        return entries

    @classmethod
    def load(cls, project):
        """Load and validate a project's policy and check its evidence files.

        Outputs:
            The policy. Raises RuntimeError when the policy file is missing or
            invalid, or an evidence file is missing or its hash differs.
        """
        project = Path(project)
        path = data_path(project, POLICY_FILE)
        require(
            path.is_file(), f"Source-controlled rotation policy is required at {path}"
        )
        policy = json.loads(path.read_text(encoding="utf-8-sig"))
        for rule in cls.validate(policy):
            e = rule["qualification_evidence"]
            p = data_reference(project, e["path"])
            require(
                p.is_file() and sha(p) == e["sha256"],
                rule["designator"] + ": qualification evidence changed/missing",
            )
        return policy

    @classmethod
    def apply(cls, cpl, parts, geometry, policy):
        """Return copied factory rows, never modify native/system rows or positions.

        Inputs:
            cpl: Placement rows with Designator, Rotation and Layer.
            parts: Exact-part rows by Designator.
            geometry: Native pad-geometry SHA-256 by designator.
            policy: A validated rotation policy.
        Outputs:
            The factory rows, with each overridden rotation offset modulo 360,
            and the schedule of what was changed. Raises RuntimeError for a
            repeated reference, an override outside the factory scope, a stale
            native guard, or an override not applied exactly once.
        """
        rules = {r["designator"]: r for r in cls.validate(policy)}
        refs = [r["Designator"] for r in cpl]
        require(len(refs) == len(set(refs)), "Duplicate factory CPL reference")
        require(
            set(rules).issubset(refs),
            "Rotation override is absent from required factory scope",
        )
        by_ref = {r["Designator"]: r for r in parts}
        result = []
        schedule = []
        for native in cpl:
            ref = native["Designator"]
            row = dict(native)
            if ref in rules:
                rule = rules[ref]
                expected = rule["expected"]
                require(
                    ref in by_ref and ref in geometry,
                    ref + ": missing exact part/native pad geometry",
                )
                actual = {
                    **by_ref[ref],
                    **native,
                    "Native_geometry_sha256": geometry[ref],
                }
                for field, value in expected.items():
                    require(
                        field in actual, ref + ": unsupported native guard " + field
                    )
                    same = (
                        cls.number(actual[field], ref + " native angle") % 360
                        == cls.number(value, ref + " expected angle") % 360
                        if field == "Rotation"
                        else actual[field] == value
                    )
                    require(same, ref + ": stale rotation guard " + field)
                angle = (
                    cls.number(native["Rotation"], ref + " native angle")
                    + cls.number(rule["offset_deg"], ref + " offset")
                ) % 360
                row["Rotation"] = f"{angle:.6f}"
                e = rule["qualification_evidence"]
                schedule.append(
                    {
                        "Designator": ref,
                        "Native rotation deg": native["Rotation"],
                        "Factory rotation deg": row["Rotation"],
                        "Offset deg": rule["offset_deg"],
                        "Layer": native["Layer"],
                        **{
                            k: by_ref[ref][k]
                            for k in ("Manufacturer", "MPN", "LCSC Part #", "Footprint")
                        },
                        "Native geometry SHA256": geometry[ref],
                        "Qualification evidence": e["path"],
                        "Qualification SHA256": e["sha256"],
                    }
                )
            result.append(row)
        require(
            len(schedule) == len(rules),
            "A rotation override was not applied exactly once",
        )
        return result, schedule
