"""Safely retain a private preview across layer-label and soft-cost changes.

Layer labels are KiCad metadata. Copper, geometry, stackup and hard policy must
remain byte-for-byte unchanged; native revalidation is still mandatory.
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy

from core.kicad_sexpr import parse
from pcb.route_optimizer import COPPER_LAYERS


def _roles(board: bytes):
    if not isinstance(board, bytes):
        raise ValueError("Expected native PCB bytes")
    prefix = b"\xef\xbb\xbf" if board.startswith(b"\xef\xbb\xbf") else b""
    text = board[len(prefix) :].decode("utf-8")
    root = parse(text)
    sections = root.children("layers")
    if root.name != "kicad_pcb" or len(sections) != 1:
        raise ValueError("Expected one native PCB layer list")
    result = {}
    for form in sections[0].children():
        atoms = form.atoms()
        if len(atoms) < 3 or atoms[1].value not in COPPER_LAYERS:
            continue
        name, role = atoms[1].value, atoms[2]
        if name in result or role.value not in {"signal", "power", "mixed", "jumper"}:
            raise ValueError("Ambiguous or unsupported copper layer annotation")
        result[name] = role
    if not {"F.Cu", "B.Cu"} <= result.keys():
        raise ValueError("Missing outer copper layers")
    return prefix, text, result


def _apply_roles(board: bytes, expected: dict, desired: dict) -> bytes:
    prefix, text, actual = _roles(board)
    if {k: v.value for k, v in actual.items()} != expected:
        raise ValueError(
            "The candidate copper layer annotations do not match the old board"
        )
    edits = [
        (actual[name].start, actual[name].end, token)
        for name, (value, token) in desired.items()
        if actual[name].value != value
    ]
    for start, end, token in sorted(edits, reverse=True):
        text = text[:start] + token + text[end:]
    return prefix + text.encode("utf-8")


def rebase_layer_annotations(
    old_board: bytes, new_board: bytes, candidate: bytes
) -> bytes:
    """Move a preview only across exact, metadata-only copper layer changes."""
    _, _, old = _roles(old_board)
    _, new_text, new = _roles(new_board)
    old_values = {name: atom.value for name, atom in old.items()}
    if old.keys() != new.keys():
        raise ValueError("A rebase may change only copper layer annotations")
    desired = {
        name: (atom.value, new_text[atom.start : atom.end])
        for name, atom in new.items()
    }
    if _apply_roles(old_board, old_values, desired) != new_board:
        raise ValueError("A rebase may change only copper layer annotations")
    return _apply_roles(candidate, old_values, desired)


def compatible_policies(old_policy: object, new_policy: object) -> bool:
    """Accept only layer-order and soft layer-cost changes; reject hard changes."""
    if not isinstance(old_policy, dict) or not isinstance(new_policy, dict):
        return False
    old, new = deepcopy(old_policy), deepcopy(new_policy)
    layers = []
    for policy in (old, new):
        route = policy.get("route")
        if not isinstance(route, dict):
            return False
        names = route.pop("layers", list(COPPER_LAYERS))
        penalties = route.pop("layer_penalties", {})
        if (
            not isinstance(names, list)
            or not names
            or any(
                not isinstance(name, str) or name not in COPPER_LAYERS for name in names
            )
            or len(set(names)) != len(names)
            or not isinstance(penalties, dict)
            or any(
                name not in COPPER_LAYERS
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                for name, value in penalties.items()
            )
        ):
            return False
        layers.append(set(names))
    return layers[0] == layers[1] and old == new


def rebase_checkpoint(
    checkpoint: dict,
    current_inputs: dict,
    current_tools: dict,
    old_board: bytes,
    new_board: bytes,
    old_policy: bytes,
    new_policy: bytes,
) -> bytes:
    """Rebind a preview only when every other source and hard policy is unchanged."""
    from pcb.net_routing.controller import decode_checkpoint

    boards = (
        [
            name
            for name in current_inputs
            if name.endswith(".kicad_pcb") and "/" not in name
        ]
        if isinstance(current_inputs, dict)
        else []
    )
    if (
        len(boards) != 1
        or current_inputs[boards[0]] != hashlib.sha256(new_board).hexdigest()
    ):
        raise ValueError("Current board fingerprint does not match the supplied board")
    try:
        old_config = json.loads(old_policy.decode("utf-8-sig"))
        new_config = json.loads(new_policy.decode("utf-8-sig"))
    except (UnicodeError, ValueError, AttributeError) as error:
        raise ValueError("Invalid previous or current routing policy") from error
    if not compatible_policies(old_config, new_config):
        raise ValueError("Routing policy changed beyond layer ordering or soft costs")
    old_inputs = dict(current_inputs)
    old_inputs[boards[0]] = hashlib.sha256(old_board).hexdigest()
    candidate = decode_checkpoint(
        checkpoint,
        old_inputs,
        current_tools,
        hashlib.sha256(old_policy).hexdigest(),
        allow_tool_changes=True,
    )
    return rebase_layer_annotations(old_board, new_board, candidate)
