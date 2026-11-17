"""Source/identity-bound factory angle corrections without CAD/coordinate edits."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from core.assembly_rotation import POLICY_PATH, apply_offsets, load_policy


def fixture():
    parts = [
        {
            "Designator": r,
            "Manufacturer": "Molex",
            "MPN": "EXACT",
            "LCSC Part #": "C1",
            "Footprint": "socket",
        }
        for r in ("J16", "J17", "R1")
    ]
    cpl = [
        {
            "Designator": r,
            "Mid X": str(x),
            "Mid Y": "30.000000",
            "Rotation": "0.000000",
            "Layer": "Top",
        }
        for r, x in [("J16", 10), ("J17", 20), ("R1", 15)]
    ]
    rule = {
        "designator": "J16",
        "qualified": True,
        "offset_deg": 90,
        "qualification_note": "Pin 1 and catalog landing pattern independently checked.",
        "expected": {
            k: parts[0][k] for k in ("Manufacturer", "MPN", "LCSC Part #", "Footprint")
        },
        "qualification_evidence": {
            "path": "design/rotation_evidence.json",
            "sha256": "0" * 64,
        },
    }
    rule["expected"].update(Layer="Top", Rotation=0, Native_geometry_sha256="shape")
    p = {
        "schema": 1,
        "export_scope": "JLCPCB_FACTORY_CPL",
        "rotation_convention": "ADD_TO_NATIVE_DEGREES_MOD_360",
        "native_geometry_unchanged": True,
        "overrides": [rule],
    }
    return parts, cpl, {r["Designator"]: "shape" for r in parts}, p


class RotationTests(unittest.TestCase):
    def test_only_qualified_angle_changes_without_mutating_native_or_positions(self):
        parts, cpl, geometry, p = fixture()
        original = copy.deepcopy(cpl)
        out, receipt = apply_offsets(cpl, parts, geometry, p)
        self.assertEqual(cpl, original)
        self.assertEqual(out[0]["Rotation"], "90.000000")
        self.assertEqual(out[1:], original[1:])
        self.assertEqual(len(receipt), 1)
        self.assertEqual(
            {k: v for k, v in out[0].items() if k != "Rotation"},
            {k: v for k, v in original[0].items() if k != "Rotation"},
        )

    def test_negative_offset_wraps_without_side_mirroring(self):
        parts, cpl, geometry, p = fixture()
        cpl[0]["Layer"] = "Bottom"
        p["overrides"][0]["expected"]["Layer"] = "Bottom"
        p["overrides"][0]["offset_deg"] = -90
        out, _ = apply_offsets(cpl, parts, geometry, p)
        self.assertEqual(out[0]["Rotation"], "270.000000")
        self.assertEqual(out[0]["Layer"], "Bottom")

    def test_unqualified_ambiguous_or_nonfinite_angles_are_rejected(self):
        for change in (
            {"qualified": False},
            {"qualification_note": ""},
            {"offset_deg": 45},
            {"offset_deg": 0},
            {"offset_deg": float("nan")},
            {"offset_deg": True},
        ):
            parts, cpl, g, p = fixture()
            p["overrides"][0].update(change)
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                apply_offsets(cpl, parts, g, p)

    def test_changed_identity_angle_side_or_geometry_cannot_reuse_offset(self):
        for field in (
            "Manufacturer",
            "MPN",
            "LCSC Part #",
            "Footprint",
            "Layer",
            "Rotation",
            "Native_geometry_sha256",
        ):
            parts, cpl, g, p = fixture()
            p["overrides"][0]["expected"][field] = (
                "90" if field == "Rotation" else "changed"
            )
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(RuntimeError, "stale rotation guard"),
            ):
                apply_offsets(cpl, parts, g, p)

    def test_duplicate_missing_or_owner_only_reference_is_rejected(self):
        for mode in ("duplicate_rule", "missing_ref", "owner_only", "duplicate_cpl"):
            parts, cpl, g, p = fixture()
            if mode == "duplicate_rule":
                p["overrides"] *= 2
            if mode == "missing_ref":
                p["overrides"][0]["designator"] = "J99"
            if mode == "owner_only":
                p["overrides"][0]["designator"] = "U3"
                parts.append({"Designator": "U3"})
            if mode == "duplicate_cpl":
                cpl.append(dict(cpl[0]))
            with self.subTest(mode=mode), self.assertRaises(RuntimeError):
                apply_offsets(cpl, parts, g, p)

    def test_actual_qualification_file_must_match_its_controlled_hash(self):
        _, _, _, p = fixture()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "design").mkdir()
            e = root / "design/rotation_evidence.json"
            e.write_text("qualified pin1 evidence")
            p["overrides"][0]["qualification_evidence"]["sha256"] = hashlib.sha256(
                e.read_bytes()
            ).hexdigest()
            (root / POLICY_PATH).write_text(json.dumps(p))
            self.assertEqual(load_policy(root), p)
            e.write_text("different preview")
            with self.assertRaisesRegex(RuntimeError, "qualification evidence changed"):
                load_policy(root)

    def test_empty_policy_preserves_every_original_factory_row(self):
        parts, cpl, g, p = fixture()
        p["overrides"] = []
        out, schedule = apply_offsets(cpl, parts, g, p)
        self.assertEqual(out, cpl)
        self.assertEqual(schedule, [])


if __name__ == "__main__":
    unittest.main()
