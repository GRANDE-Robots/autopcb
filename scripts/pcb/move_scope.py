"""Which footprints routing may move, as set by the placement policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from core.kicad_sexpr import Form, parse

SIDES = {"F.Cu": "top", "B.Cu": "bottom"}
SILKSCREEN = {"top": '"F.SilkS"', "bottom": '"B.SilkS"'}


@dataclass(frozen=True)
class MoveScope:
    """Parts the placement policy lets routing move.

    By default any footprint may move, on either side, with any pad type and
    even if it is locked in KiCad. ``sides``, ``through_hole`` and ``locked``
    narrow that. A side that stays fixed keeps its silkscreen, so a part that
    prints on it stays fixed too.
    """

    sides: frozenset[str] = frozenset(SIDES.values())
    through_hole: bool = True
    locked: bool = True

    @classmethod
    def from_config(cls, raw: object) -> MoveScope:
        if raw is None:
            return cls()
        if not isinstance(raw, dict) or set(raw) - {"sides", "through_hole", "locked"}:
            raise ValueError("placement accepts only sides, through_hole and locked")
        sides = raw.get("sides", sorted(SIDES.values()))
        if (
            not isinstance(sides, list)
            or len(set(sides)) != len(sides)
            or not set(sides) <= set(SIDES.values())
        ):
            raise ValueError("placement sides must list top, bottom or both")
        through_hole, locked = raw.get("through_hole", True), raw.get("locked", True)
        if not isinstance(through_hole, bool) or not isinstance(locked, bool):
            raise ValueError("placement through_hole and locked must be true or false")
        return cls(frozenset(sides), through_hole, locked)

    @staticmethod
    def reference(footprint: Form) -> str | None:
        for prop in footprint.children("property"):
            atoms = prop.atoms()
            if len(atoms) >= 3 and atoms[1].value == "Reference":
                return atoms[2].value
        return None

    def refusal(self, text: str, footprint: Form) -> str | None:
        """Why the footprint parsed from ``text`` may not move, or None if it may."""
        reference = self.reference(footprint) or "A footprint"
        lock = footprint.children("locked")
        if not self.locked and (
            any(atom.value == "locked" for atom in footprint.atoms()[2:])
            or (lock and lock[0].atoms()[-1].value != "no")
        ):
            return f"{reference} is locked, which the placement policy keeps fixed"
        layers = footprint.children("layer")
        side = SIDES.get(layers[0].atoms()[1].value) if layers else None
        if side not in self.sides:
            return f"{reference} is on the {side or 'unknown'} side, which the placement policy keeps fixed"
        block = text[footprint.start : footprint.end]
        for fixed in sorted(set(SIDES.values()) - self.sides):
            if SILKSCREEN[fixed] in block:
                return f"Moving {reference} would alter the {fixed} silkscreen"
        if not self.through_hole and any(
            len(pad.atoms()) > 2
            and pad.atoms()[2].value in {"thru_hole", "np_thru_hole"}
            for pad in footprint.children("pad")
        ):
            return f"{reference} has through-hole pads, which the placement policy keeps fixed"
        return None

    def _footprints(self, text: str) -> dict[str | None, list[Form]]:
        blocks: dict[str | None, list[Form]] = {}
        for footprint in parse(text).children("footprint"):
            blocks.setdefault(self.reference(footprint), []).append(footprint)
        return blocks

    def movable(self, text: str) -> frozenset[str]:
        """References of the footprints in ``text`` that may move."""
        return frozenset(
            reference
            for reference, found in self._footprints(text).items()
            if reference and len(found) == 1 and self.refusal(text, found[0]) is None
        )

    def check(self, text: str, references: Iterable[str]) -> None:
        """Raise unless each reference names exactly one footprint that may move."""
        blocks = self._footprints(text)
        for reference in references:
            found = blocks.get(reference, [])
            if len(found) != 1:
                raise ValueError(f"Footprint {reference!r} is missing or ambiguous")
            reason = self.refusal(text, found[0])
            if reason:
                raise ValueError(reason)
