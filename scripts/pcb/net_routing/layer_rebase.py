"""layer_rebase.py: safely retain a private preview across layer-label and soft-cost changes.

Layer labels are KiCad metadata. Copper, geometry, stackup and hard policy must
remain byte-for-byte unchanged; native revalidation is still mandatory.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy

from core.kicad_sexpr import parse
from pcb.route_optimizer import COPPER_LAYERS


class LayerAnnotations:
    """Reads and rewrites the role annotation of each copper layer in native PCB bytes."""

    @staticmethod
    def roles(board: bytes):
        """Parse the copper layer role annotations of a native board.

        Inputs:
            board: .kicad_pcb file bytes, with or without a UTF-8 byte order mark.
        Outputs:
            (prefix, text, roles): the byte order mark or b"", the decoded text, and
            the atom holding each copper layer's role, keyed by layer name. Raises
            ValueError when board is not bytes, has no single layer list, repeats a
            copper layer, uses a role other than signal, power, mixed or jumper, or
            lacks F.Cu or B.Cu.
        """
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
            if name in result or role.value not in {
                "signal",
                "power",
                "mixed",
                "jumper",
            }:
                raise ValueError("Ambiguous or unsupported copper layer annotation")
            result[name] = role
        if not {"F.Cu", "B.Cu"} <= result.keys():
            raise ValueError("Missing outer copper layers")
        return prefix, text, result

    @staticmethod
    def apply_roles(board: bytes, expected: dict, desired: dict) -> bytes:
        """Rewrite the copper layer roles of a board to the desired values.

        Inputs:
            board: native PCB bytes.
            expected: role value each copper layer must currently have, by layer name.
            desired: (role value, source token) per layer name.
        Outputs:
            Board bytes in which every layer whose role differs has its token
            replaced. Raises ValueError when the board's current roles differ from
            expected.
        """
        prefix, text, actual = LayerAnnotations.roles(board)
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

    @staticmethod
    def rebase(old_board: bytes, new_board: bytes, candidate: bytes) -> bytes:
        """Move a preview only across exact, metadata-only copper layer changes."""
        _, _, old = LayerAnnotations.roles(old_board)
        _, new_text, new = LayerAnnotations.roles(new_board)
        old_values = {name: atom.value for name, atom in old.items()}
        if old.keys() != new.keys():
            raise ValueError("A rebase may change only copper layer annotations")
        desired = {
            name: (atom.value, new_text[atom.start : atom.end])
            for name, atom in new.items()
        }
        if LayerAnnotations.apply_roles(old_board, old_values, desired) != new_board:
            raise ValueError("A rebase may change only copper layer annotations")
        return LayerAnnotations.apply_roles(candidate, old_values, desired)


class LayerPolicy:
    """Judges whether two routing policies differ only in layer order and soft layer cost."""

    @staticmethod
    def compatible(old_policy: object, new_policy: object) -> bool:
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
                    not isinstance(name, str) or name not in COPPER_LAYERS
                    for name in names
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


class CheckpointRebase:
    """Rebinds a saved preview candidate to a board that differs only in layer annotations."""

    def __call__(
        self,
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
            raise ValueError(
                "Current board fingerprint does not match the supplied board"
            )
        try:
            old_config = json.loads(old_policy.decode("utf-8-sig"))
            new_config = json.loads(new_policy.decode("utf-8-sig"))
        except (UnicodeError, ValueError, AttributeError) as error:
            raise ValueError("Invalid previous or current routing policy") from error
        if not LayerPolicy.compatible(old_config, new_config):
            raise ValueError(
                "Routing policy changed beyond layer ordering or soft costs"
            )
        old_inputs = dict(current_inputs)
        old_inputs[boards[0]] = hashlib.sha256(old_board).hexdigest()
        candidate = decode_checkpoint(
            checkpoint,
            old_inputs,
            current_tools,
            hashlib.sha256(old_policy).hexdigest(),
            allow_tool_changes=True,
        )
        return LayerAnnotations.rebase(old_board, new_board, candidate)


rebase_checkpoint = CheckpointRebase()
