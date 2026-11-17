"""Prepare a bottom-silkscreen field move using KiCad's coordinate conversion.

The move is only a proposal. Check clearances and appearance before applying it.
"""

from __future__ import annotations

from core.project import native_path

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

import pcbnew

from core.kicad_campaign import CampaignFile, CampaignPlanner, _select, digest
from core.kicad_sexpr import parse
from pcb.route_optimizer import mm


def position_change(
    project: Path, reference: str, field_name: str, x_mm: float, y_mm: float
) -> dict[str, Any]:
    """Return a source-bound, native-verified field move in board coordinates."""
    project = project.resolve()
    source = native_path(project, ".kicad_pcb")
    original = source.read_bytes()
    original_hash = hashlib.sha256(original).hexdigest()
    if not isinstance(reference, str) or not reference:
        raise ValueError("A footprint reference is required")
    if not isinstance(field_name, str) or not field_name:
        raise ValueError("A field name is required")
    desired = pcbnew.VECTOR2I(mm(x_mm), mm(y_mm))
    board = pcbnew.LoadBoard(str(source))
    footprint = board.FindFootprintByReference(reference)
    if footprint is None or footprint.GetLayer() != pcbnew.B_Cu:
        raise ValueError("Only a bottom-side footprint may be selected")
    fields = [field for field in footprint.GetFields() if field.GetName() == field_name]
    if len(fields) != 1:
        raise ValueError("The selected footprint field is missing or ambiguous")
    field = fields[0]
    if field.GetLayer() != pcbnew.B_SilkS or not field.IsVisible():
        raise ValueError("Only visible bottom-silkscreen fields may be moved")
    field.SetPosition(desired)
    text_box = field.GetBoundingBox()
    board_box = board.GetBoardEdgesBoundingBox()
    if (
        text_box.GetX() < board_box.GetX()
        or text_box.GetY() < board_box.GetY()
        or text_box.GetX() + text_box.GetWidth()
        > board_box.GetX() + board_box.GetWidth()
        or text_box.GetY() + text_box.GetHeight()
        > board_box.GetY() + board_box.GetHeight()
    ):
        raise ValueError("The field would extend outside the board's bounding box")
    identity = field.m_Uuid.AsString()
    with tempfile.TemporaryDirectory(prefix="jessie-text-position-") as directory:
        temporary = Path(directory)
        serialized = temporary / "serialized.kicad_pcb"
        if not pcbnew.SaveBoard(str(serialized), board):
            raise RuntimeError("KiCad could not serialize the proposed text position")
        native = serialized.read_text(encoding="utf-8")
        node = _select(parse(native), {"kind": "property", "uuid": identity})
        if len(node.atoms()) < 2 or node.atoms()[1].value != field_name:
            raise RuntimeError("KiCad serialized a different field")
        positions = node.children("at")
        if len(positions) != 1 or len(positions[0].atoms()) not in (3, 4):
            raise RuntimeError("KiCad did not serialize a valid field position")
        coordinates = positions[0].atoms()
        change = {
            "document": "pcb",
            "target": {"kind": "property", "uuid": identity},
            "op": "set_at",
            "coordinate_space": "footprint_local",
            "x": coordinates[1].value,
            "y": coordinates[2].value,
        }
        candidate = CampaignPlanner(project).plan("pcb", [change])
        trial = temporary / source.name
        trial.write_bytes(candidate.after)
        checked = pcbnew.LoadBoard(str(trial)).FindFootprintByReference(reference)
        matches = [
            item for item in checked.GetFields() if item.m_Uuid.AsString() == identity
        ]
        if len(matches) != 1 or matches[0].GetPosition() != desired:
            raise RuntimeError(
                "The proposed field position failed native KiCad readback"
            )
    if hashlib.sha256(source.read_bytes()).hexdigest() != original_hash:
        raise RuntimeError("Saved PCB changed while preparing the text move")
    return {
        "status": "preview_only",
        "source_sha256": original_hash,
        "candidate_sha256": candidate.result_sha256,
        "reference": reference,
        "field": field_name,
        "board_position_mm": [x_mm, y_mm],
        "change": change,
        "limitations": "Check the full board outline, silkscreen clearances and appearance before applying.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument(
        "--reference", required=True, help="bottom-side footprint reference"
    )
    parser.add_argument(
        "--field", default="Reference", help="visible bottom-silkscreen field"
    )
    parser.add_argument("--x", type=float, required=True, help="board X position in mm")
    parser.add_argument("--y", type=float, required=True, help="board Y position in mm")
    parser.add_argument(
        "--save-campaign", help="save the proposal under this descriptive name"
    )
    args = parser.parse_args(argv)
    try:
        result = position_change(
            args.project, args.reference, args.field, args.x, args.y
        )
        if args.save_campaign:
            source = native_path(args.project, ".kicad_pcb")
            if digest(source.read_bytes()) != result["source_sha256"]:
                raise RuntimeError("Saved PCB changed before creating the campaign")
            campaign = CampaignFile(args.project).create(
                args.save_campaign, changes=[result["change"]]
            )
            result["campaign"] = {
                "file": str(campaign),
                "campaign_sha256": digest(campaign.read_bytes()),
            }
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Bottom-silkscreen text move failed: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
