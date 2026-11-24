"""Prepare and verify native poses of footprints the placement policy lets move.

Only the selected footprint expression is copied from a disposable KiCad
serialization. The saved design is never rewritten by this module.
"""

from __future__ import annotations

import hashlib
import math
import tempfile
from pathlib import Path
from typing import Any

import pcbnew

from core.kicad_campaign import CampaignForms
from core.kicad_sexpr import parse
from pcb.move_scope import MoveScope
from pcb.route_optimizer import mm


def native_pose_change(
    source: Path,
    reference: str,
    x_mm: float,
    y_mm: float,
    rotation_deg: float,
    scope: MoveScope = MoveScope(),
) -> dict[str, Any]:
    """Return an exact native pose replacement for one footprint the scope lets move."""
    source = source.resolve()
    if not isinstance(reference, str) or not reference:
        raise ValueError("A footprint reference is required")
    if (
        isinstance(rotation_deg, bool)
        or not isinstance(rotation_deg, (int, float))
        or not math.isfinite(rotation_deg)
    ):
        raise ValueError("Rotation must be a finite number of degrees")
    position = pcbnew.VECTOR2I(mm(x_mm), mm(y_mm))
    original = source.read_bytes()
    original_hash = hashlib.sha256(original).hexdigest()
    scope.check(original.decode("utf-8"), (reference,))
    board = pcbnew.LoadBoard(str(source))
    footprint = board.FindFootprintByReference(reference)
    footprint.SetPosition(position)
    footprint.SetOrientationDegrees(float(rotation_deg))
    with tempfile.TemporaryDirectory(prefix="jessie-native-pose-") as directory:
        candidate = Path(directory) / "candidate.kicad_pcb"
        if not pcbnew.SaveBoard(str(candidate), board):
            raise RuntimeError("KiCad could not serialize the candidate footprint pose")
        rendered = candidate.read_text(encoding="utf-8")
    if hashlib.sha256(source.read_bytes()).hexdigest() != original_hash:
        raise RuntimeError("Saved board changed while generating the footprint pose")
    result = CampaignForms.select(
        parse(rendered), {"kind": "footprint", "reference": reference}
    )
    return {
        "document": "pcb",
        "target": {"kind": "footprint", "reference": reference},
        "op": "replace",
        "path": [],
        "expression": rendered[result.start : result.end],
    }


def verify_native_pose_change(
    source: Path, change: dict[str, Any], scope: MoveScope = MoveScope()
) -> None:
    """Reject any replacement other than the native transform of the same part."""
    target = change.get("target")
    if (
        set(change) != {"document", "target", "op", "path", "expression"}
        or change.get("document") != "pcb"
        or change.get("op") != "replace"
        or change.get("path") != []
        or not isinstance(target, dict)
        or set(target) != {"kind", "reference"}
        or target.get("kind") != "footprint"
    ):
        raise ValueError("A native pose replacement needs one footprint reference")
    expression = change.get("expression")
    if not isinstance(expression, str):
        raise ValueError("A native pose replacement needs a footprint expression")
    form = parse(expression)
    if form.name != "footprint":
        raise ValueError("A native pose replacement must contain a footprint")
    atoms = CampaignForms.named_property(form, "Reference").atoms()
    if len(atoms) < 3 or atoms[2].value != target["reference"]:
        raise ValueError("A native pose replacement cannot change the reference")
    at = CampaignForms.one(form.children("at"), "position").atoms()
    if len(at) not in (3, 4):
        raise ValueError("A native pose replacement needs a valid position")
    try:
        x, y = float(at[1].value), float(at[2].value)
        angle = float(at[3].value) if len(at) == 4 else 0.0
    except ValueError as error:
        raise ValueError("A native pose replacement has invalid coordinates") from error
    expected = native_pose_change(source, target["reference"], x, y, angle, scope)
    if expected["expression"] != expression:
        raise ValueError(
            "The footprint replacement is not the exact native pose transform"
        )
