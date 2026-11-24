"""silkscreen.py: Apply configured silkscreen placement and edge-clearance repairs only.

Moves configured silkscreen text, binds stray reference text to footprint-owned display
fields, pulls silk lines inside the board edge and restores stock footprint silk. Every
candidate is checked to differ from the saved board only on the two silkscreen layers,
to introduce no native DRC finding and to leave the silk finding count no higher. With
--top-alignment only the front connector label text is aligned. With --apply the checked
board and any synchronized local library files are saved, and are restored if the save
fails.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from core.project import native_path

import argparse
from datetime import datetime, timezone
import fnmatch
import json
import math
import os
from pathlib import Path
import re
import tempfile

from core.kicad_campaign import CampaignPlanner
from core.kicad_sexpr import Form, parse, quote
from core.paths import asset_root
from core.toolchain import kicad_cli
from core.pcb_io import BoardSaveGuard, quiet_duplicate_image_handlers
from pcb.route_validation import _canonical, _drc
from core.compat import file_sha256, strict_zip


class SilkscreenProofs:
    """Proofs that an edit changed nothing but silkscreen."""

    @staticmethod
    def without_silk(item):
        """Canonical form of one S-expression item without its silkscreen items.

        Inputs:
            item: A parsed form or atom.
        Outputs:
            A nested tuple of canonical values, or None for a form on F.SilkS or
            B.SilkS.
        """
        if not isinstance(item, Form):
            return _canonical(item)
        layers = item.children("layer")
        if layers and layers[0].atoms()[1].value in {"F.SilkS", "B.SilkS"}:
            return None
        return tuple(
            value
            for child in item.items
            if (value := SilkscreenProofs.without_silk(child)) is not None
        )

    @staticmethod
    def nonsilkscreen(content: bytes) -> tuple:
        """Canonicalize every protected item, omitting only the two silk layers.

        Inputs:
            content: Saved board bytes.
        Outputs:
            A nested tuple that is equal for two boards exactly when they differ only on
            F.SilkS and B.SilkS.
        """
        return SilkscreenProofs.without_silk(parse(content.decode("utf-8-sig")))

    @staticmethod
    def verify_only(before: bytes, after: bytes) -> None:
        """Require that two boards differ only on the silkscreen layers.

        Inputs:
            before, after: Saved board bytes.
        Outputs:
            None. Raises RuntimeError when a non-silkscreen item differs.
        """
        if SilkscreenProofs.nonsilkscreen(before) != SilkscreenProofs.nonsilkscreen(
            after
        ):
            raise RuntimeError("Silkscreen edit changed non-silkscreen design items")

    @staticmethod
    def top_signature(data: bytes) -> tuple:
        """Canonical board content with front text pose and justification left out.

        Inputs:
            data: Saved board bytes.
        Outputs:
            A tuple with each board-level front silkscreen text reduced to its fields
            without ``at`` and without ``justify``, and every other item canonical.
        """
        result = []
        for item in parse(data.decode("utf-8-sig")).items:
            if (
                not isinstance(item, Form)
                or item.name != "gr_text"
                or item.children("layer")[0].atoms()[1].value != "F.SilkS"
            ):
                result.append(_canonical(item))
                continue
            parts = []
            for field in item.items:
                if isinstance(field, Form) and field.name == "at":
                    continue
                if isinstance(field, Form) and field.name == "effects":
                    parts.append(
                        tuple(
                            _canonical(value)
                            for value in field.items
                            if not isinstance(value, Form) or value.name != "justify"
                        )
                    )
                else:
                    parts.append(_canonical(field))
            result.append(tuple(parts))
        return tuple(result)

    @staticmethod
    def verify_top_alignment(before: bytes, after: bytes) -> None:
        """Require that only the pose and justification of board-level front text changed.

        Inputs:
            before, after: Saved board bytes.
        Outputs:
            None. Raises RuntimeError when content, geometry or another layer differs.
        """
        if SilkscreenProofs.top_signature(before) != SilkscreenProofs.top_signature(
            after
        ):
            raise RuntimeError(
                "Top label alignment changed content, geometry or another layer"
            )


class TopLabels:
    """Aligns the front connector label banks of a board from its saved footprints.

    Each bank pairs a connector reference with its caption and places both beside,
    above, below or at a fixed position of the connector bank, at a fixed line pitch.
    """

    def __init__(self, project: Path, policy: dict):
        """Load the board and the front text it will align.

        Inputs:
            project: Folder of the KiCad project.
            policy: The ``top_alignment`` policy: ``line_pitch_mm``, ``gap_mm`` and
                ``banks``.
        Outputs:
            None. Raises ValueError when the line pitch is outside 1.25 to 2.4 mm or the
            gap is outside 0.25 to 1.2 mm.
        """
        import pcbnew

        self.project = project
        self.policy = policy
        self.original = (native_path(project, ".kicad_pcb")).read_bytes()
        self.source = self.original.decode("utf-8-sig")
        self.board = pcbnew.LoadBoard(str(native_path(project, ".kicad_pcb")))
        self.pitch = round(policy["line_pitch_mm"] * 1e6)
        self.gap = round(policy["gap_mm"] * 1e6)
        if not 1250000 <= self.pitch <= 2400000 or not 250000 <= self.gap <= 1200000:
            raise ValueError("Top label spacing is outside the supported range")
        self.text_items = [
            item
            for item in self.board.GetDrawings()
            if isinstance(item, pcbnew.PCB_TEXT) and item.GetLayer() == pcbnew.F_SilkS
        ]
        self.forms = {
            form.children("uuid")[0].atoms()[1].value: form
            for form in parse(self.source).children("gr_text")
        }
        self.used, self.changes, self.moved = set(), [], 0

    @classmethod
    def plan(cls, project: Path, policy: dict) -> tuple[bytes, dict[str, int]]:
        """Plan the label alignment; the entry point run in an isolated native worker.

        Inputs:
            project: Folder of the KiCad project.
            policy: The ``top_alignment`` policy.
        Outputs:
            (board bytes, counts) with ``aligned_labels`` and ``moved_texts``. See
            ``run`` for the failures.
        """
        return cls(project, policy).run()

    def label(self, name, nearby=None):
        """Claim the next unused front silkscreen text with the given content.

        Inputs:
            name: Text content to find.
            nearby: A text item; when given, the closest match, preferring one with the
                same angle, is claimed.
        Outputs:
            The text item. Raises ValueError when there is no match or, without
            ``nearby``, more than one.
        """
        matches = [
            item
            for item in self.text_items
            if str(item.GetText()) == name and item.m_Uuid.AsString() not in self.used
        ]
        if nearby is not None:
            pos = nearby.GetPosition()
            matches.sort(
                key=lambda item: (
                    (item.GetPosition().x - pos.x) ** 2
                    + (item.GetPosition().y - pos.y) ** 2
                )
            )
            matches = [
                item
                for item in matches
                if item.GetTextAngle().AsDegrees() == nearby.GetTextAngle().AsDegrees()
            ] or matches
            matches = matches[:1]
        if len(matches) != 1:
            raise ValueError(f"Missing or ambiguous front label: {name}")
        self.used.add(matches[0].m_Uuid.AsString())
        return matches[0]

    def set_text(self, item, x, y, left):
        # Snap the anchor, not the footprint, to the text layout grid.
        """Queue the edits that put a label at an anchor with the wanted justification.

        The anchor is snapped to the 0.05 mm (50000 nm) text grid and the angle is set
        to 0 degrees.

        Inputs:
            item: The text item.
            x, y: Wanted anchor, in nm.
            left: Left-justify when True, centre when False.
        Outputs:
            None. Raises ValueError when the existing justification is mirrored or
            unsupported.
        """
        x, y = (round(v / 50000) * 50000 for v in (x, y))
        uid = item.m_Uuid.AsString()
        target = {"kind": "gr_text", "uuid": uid}
        at = item.GetPosition()
        if (at.x, at.y, item.GetTextAngle().AsDegrees()) != (x, y, 0):
            self.changes.append(
                {
                    "op": "replace",
                    "target": target,
                    "path": ["at"],
                    "expression": f"(at {x / 1e6:.6f} {y / 1e6:.6f} 0)",
                }
            )
            self.moved += 1
        effects = self.forms[uid].children("effects")[0]
        justification = effects.children("justify")
        desired = ["left"] if left else []
        existing = (
            [atom.value for atom in justification[0].atoms()[1:]]
            if justification
            else []
        )
        if existing != desired:
            if set(existing) - {"left", "right", "top", "bottom"}:
                raise ValueError(
                    "Unexpected mirrored or unsupported label justification"
                )
            raw = self.source[effects.start : effects.end]
            if justification:
                field = justification[0]
                raw = (
                    raw[: field.start - effects.start]
                    + raw[field.end - effects.start :]
                )
            if left:
                raw = raw[:-1] + " (justify left))"
            self.changes.append(
                {
                    "op": "replace",
                    "target": target,
                    "path": ["effects"],
                    "expression": raw,
                }
            )

    @staticmethod
    def ink_width(item):
        """Width of a text's ink with no rotation.

        Inputs:
            item: The text item; its angle is restored before returning.
        Outputs:
            Width of the bounding box in nm.
        """
        import pcbnew

        angle = item.GetTextAngle()
        item.SetTextAngle(pcbnew.EDA_ANGLE(0, pcbnew.DEGREES_T))
        width = item.GetBoundingBox().GetWidth()
        item.SetTextAngle(angle)
        return width

    def run(self) -> tuple[bytes, dict[str, int]]:
        """Place every bank's labels and build the candidate board.

        Outputs:
            (board bytes, counts) with ``aligned_labels`` and ``moved_texts``. Raises
            ValueError for an invalid bank, a missing connector, a missing or ambiguous
            label, or inline labels on a bank that is not above or below, and
            RuntimeError when the candidate changes anything but label pose and
            justification.
        """
        pitch, gap = self.pitch, self.gap
        for bank in self.policy["banks"]:
            mode = bank["side"]
            parts = [
                self.board.FindFootprintByReference(ref) for ref, _ in bank["items"]
            ]
            if (
                not parts
                or any(part is None for part in parts)
                or mode not in {"right", "above", "below", "fixed"}
            ):
                raise ValueError("Invalid top label bank or missing connector")
            boxes = [part.GetBoundingBox(False, False) for part in parts]
            common_x = max(box.GetRight() for box in boxes) + gap
            first_above = min(box.GetY() for box in boxes) - gap - 500000 - pitch
            first_below = max(box.GetBottom() for box in boxes) + gap + 500000
            for (ref, caption), box in strict_zip(bank["items"], boxes):
                title = self.label(ref)
                role = self.label(caption, title)
                if mode == "right":
                    x = common_x
                    y = (
                        box.GetY() + 250000
                        if bank.get("start_near_top")
                        else (box.GetY() + box.GetBottom() - pitch) / 2
                    )
                elif mode == "fixed":
                    x, y = (round(v * 1e6) for v in bank["first_mm"])
                else:
                    x = (box.GetX() + box.GetRight()) / 2
                    y = first_above if mode == "above" else first_below
                y += round(bank.get("vertical_bias_mm", 0) * 1e6)
                if bank.get("inline"):
                    if mode not in {"above", "below"}:
                        raise ValueError("Inline labels need a centered connector bank")
                    first, second = self.ink_width(title), self.ink_width(role)
                    spacer = 400000
                    left = x - (first + second + spacer) / 2
                    self.set_text(title, left, y, True)
                    self.set_text(role, left + first + spacer, y, True)
                else:
                    for index, item in enumerate((title, role)):
                        self.set_text(
                            item, x, y + index * pitch, mode in {"right", "fixed"}
                        )
        candidate = (
            CampaignPlanner(self.project).plan("pcb", self.changes).after
            if self.changes
            else self.original
        )
        SilkscreenProofs.verify_top_alignment(self.original, candidate)
        return candidate, {"aligned_labels": len(self.used), "moved_texts": self.moved}


class StockSilk:
    """Restores stock footprint silkscreen and keeps local libraries in step with the board."""

    @staticmethod
    def by_reference(source: str) -> dict:
        """Footprint forms keyed by reference designator.

        Inputs:
            source: Saved board text.
        Outputs:
            A mapping from reference to the footprint form.
        """
        return {
            p.atoms()[2].value: f
            for f in parse(source).children("footprint")
            for p in f.children("property")
            if p.atoms()[1].value == "Reference"
        }

    @staticmethod
    def top_lines(root) -> list:
        """The F.SilkS ``fp_line`` forms directly under a footprint or library file.

        Inputs:
            root: A parsed footprint form.
        Outputs:
            The matching forms.
        """
        return [
            f
            for f in root.children("fp_line")
            if f.children("layer")[0].atoms()[1].value == "F.SilkS"
        ]

    @staticmethod
    def bounds(footprint) -> tuple:
        """Largest absolute silk line coordinate on each axis of a footprint.

        Inputs:
            footprint: A parsed footprint form.
        Outputs:
            (x, y) extents in mm of the silk lines about the footprint origin.
        """
        points = [
            [float(a.value) for a in point.atoms()[1:]]
            for line in footprint.children("fp_line")
            if "SilkS" in line.children("layer")[0].atoms()[1].value
            for name in ("start", "end")
            for point in line.children(name)
        ]
        return tuple(max(abs(p[axis]) for p in points) for axis in (0, 1))

    @staticmethod
    def restore(content: bytes, bindings: list[dict]) -> bytes:
        """Restore only top-side footprint strokes, leaving pads and models untouched.

        Inputs:
            content: Saved board bytes.
            bindings: Dicts with the footprint ``reference`` and the library ``path`` of
                its stock silk.
        Outputs:
            The board bytes with each bound footprint's F.SilkS lines replaced by the
            stock lines, keeping their uuids. Raises ValueError for a footprint not on
            the top side or whose number of silk lines differs, and RuntimeError if
            anything but silkscreen changes.
        """
        source = content.decode("utf-8")
        parts = StockSilk.by_reference(source)
        edits = []
        for binding in bindings:
            footprint = parts[binding["reference"]]
            if footprint.children("layer")[0].atoms()[1].value != "F.Cu":
                raise ValueError("Stock silk restore currently supports top footprints")
            text = Path(binding["path"]).read_text(encoding="utf-8")

            old, new = StockSilk.top_lines(footprint), StockSilk.top_lines(parse(text))
            if len(old) != len(new):
                raise ValueError(
                    "Stock silk topology differs; review the footprint first"
                )
            for prior, stock in strict_zip(old, new):
                uid = prior.children("uuid")[0].atoms()[1].value
                ids = stock.children("uuid")
                if ids:
                    replacement = (
                        text[stock.start : ids[0].start]
                        + "(uuid "
                        + quote(uid)
                        + ")"
                        + text[ids[0].end : stock.end]
                    )
                else:
                    replacement = (
                        text[stock.start : stock.end - 1]
                        + "\n(uuid "
                        + quote(uid)
                        + "))"
                    )
                edits.append((prior.start, prior.end, replacement))
        for start, end, replacement in sorted(edits, reverse=True):
            source = source[:start] + replacement + source[end:]
        result = source.encode("utf-8")
        SilkscreenProofs.verify_only(content, result)
        return result

    @staticmethod
    def sync_libraries(
        project: Path, before: bytes, after: bytes, policy: dict
    ) -> dict[Path, bytes]:
        """Keep existing local library artwork synchronized after symmetric clipping.

        For each ``library_silk_sync`` binding whose silk extents differ between the two
        boards, clamps the library file's silk lines to the new extents.

        Inputs:
            project: Folder of the KiCad project.
            before, after: Saved board bytes before and after clipping.
            policy: The silkscreen policy with ``library_silk_sync``.
        Outputs:
            A mapping from library file to its new bytes. Raises ValueError when a
            library path is missing or outside the project, and RuntimeError if anything
            but silkscreen changes.
        """

        old = StockSilk.by_reference(before.decode())
        new = StockSilk.by_reference(after.decode())
        result = {}
        for binding in policy.get("library_silk_sync", []):
            reference = binding["reference"]
            if StockSilk.bounds(old[reference]) == StockSilk.bounds(new[reference]):
                continue
            path = (project / binding["path"]).resolve()
            if not path.is_relative_to(project.resolve()) or not path.is_file():
                raise ValueError(
                    "Silkscreen synchronization requires an existing project library"
                )
            source = path.read_text(encoding="utf-8")
            radius = StockSilk.bounds(new[reference])
            edits = []
            for line in parse(source).children("fp_line"):
                if "SilkS" not in line.children("layer")[0].atoms()[1].value:
                    continue
                for name in ("start", "end"):
                    point = line.children(name)[0]
                    coords = [float(a.value) for a in point.atoms()[1:]]
                    desired = [
                        max(-limit, min(value, limit))
                        for value, limit in strict_zip(coords, radius)
                    ]
                    if desired != coords:
                        edits.append(
                            (
                                point.start,
                                point.end,
                                f"({name} {desired[0]:g} {desired[1]:g})",
                            )
                        )
            for start, end, replacement in sorted(edits, reverse=True):
                source = source[:start] + replacement + source[end:]
            content = source.encode("utf-8")
            SilkscreenProofs.verify_only(path.read_bytes(), content)
            result[path] = content
        return result


class BoardLabels:
    """Binds stray reference text to footprint-owned fields and keeps them ordered and clear."""

    @staticmethod
    def order_fields(content: bytes) -> bytes:
        """Keep owned display properties in KiCad's native property order.

        Inputs:
            content: Saved board bytes.
        Outputs:
            The bytes with each footprint's ``Board label`` silkscreen property moved
            after its other properties. Raises ValueError for duplicate owned properties
            or a footprint without its native reference and value properties.
        """
        source = content.decode("utf-8")
        edits = []
        for footprint in parse(source).children("footprint"):
            properties = footprint.children("property")
            labels = [
                p
                for p in properties
                if len(p.atoms()) >= 3
                and p.atoms()[1].value == "Board label"
                and p.children("layer")
                and p.children("layer")[0].atoms()[1].value in ("F.SilkS", "B.SilkS")
            ]
            if not labels:
                continue
            if len(labels) != 1:
                raise ValueError("A footprint has duplicate owned display properties")
            field = labels[0]
            others = [
                p
                for p in properties
                if p is not field
                and len(p.atoms()) >= 3
                and p.atoms()[1].value != "ki_fp_filters"
            ]
            if not others:
                raise ValueError(
                    "An owned display property needs the native reference/value properties"
                )
            position = others[-1].end
            if position <= field.start and not source[position : field.start].strip():
                continue
            edits.extend(
                [
                    (field.start, field.end, ""),
                    (position, position, "\n\t\t" + source[field.start : field.end]),
                ]
            )
        for start, end, replacement in sorted(edits, reverse=True):
            source = source[:start] + replacement + source[end:]
        return source.encode("utf-8")

    @staticmethod
    def attach(board, policy: dict) -> tuple[dict[str, str], set[str]]:
        """Replace stale free text by footprint-owned display fields, without renaming parts.

        Inputs:
            board: The loaded board, edited in memory.
            policy: The silkscreen policy; ``attach_references`` lists the reference
                patterns and ``remove_orphan_references`` allows removing text with no
                footprint.
        Outputs:
            (labels, removed). ``labels`` maps each new field uuid to (footprint uuid,
            replaced text uuid) and ``removed`` holds the uuids of orphan text to
            delete. Both are empty when no pattern is configured. Raises ValueError when
            a field already exists or no mask-clear position is found.
        """
        patterns = policy.get("attach_references", [])
        if not patterns:
            return {}, set()
        parts = {part.GetReference(): part for part in board.GetFootprints()}
        labels, removed = BoardLabels.bind(board, policy, patterns, parts)
        BoardLabels.place(board, parts, labels, removed)
        return labels, removed

    @staticmethod
    def bind(board, policy: dict, patterns: list, parts: dict) -> tuple[dict, set]:
        """Create a ``Board label`` field from each matching free silkscreen text.

        A text such as ``TP7`` or ``F5`` that matches a configured pattern and names an
        existing footprint gets a centred, unrotated field with its layer, size and
        stroke; an unmatched reference is only recorded when orphan removal is enabled.

        Inputs:
            board: The loaded board.
            policy: The silkscreen policy.
            patterns: Reference patterns, matched as shell wildcards.
            parts: Footprints keyed by reference.
        Outputs:
            (labels, removed) as in ``attach``. Raises ValueError when the footprint
            already has the field.
        """
        import pcbnew

        labels, removed = {}, set()
        for text in list(board.GetDrawings()):
            if not isinstance(text, pcbnew.PCB_TEXT) or text.GetLayer() not in (
                pcbnew.F_SilkS,
                pcbnew.B_SilkS,
            ):
                continue
            match = re.fullmatch(r"(TP|F)(0*[1-9][0-9]*)", text.GetText())
            if not match:
                continue
            reference = match[1] + str(int(match[2]))
            if not any(fnmatch.fnmatchcase(reference, pattern) for pattern in patterns):
                continue
            part = parts.get(reference)
            if part is None:
                if policy.get("remove_orphan_references"):
                    removed.add(text.m_Uuid.AsString())
                continue
            if part.GetField("Board label") is not None:
                raise ValueError(
                    "Board-label field already exists beside an unbound duplicate"
                )
            part.SetField("Board label", text.GetText())
            field = part.GetField("Board label")
            field.SetLayer(text.GetLayer())
            field.SetVisible(True)
            field.SetTextSize(text.GetTextSize())
            # PCB_FIELD reloads an omitted zero stroke as KiCad's 0.15 mm default.
            # Write it explicitly so subsequent native saves preserve the field.
            field.SetTextThickness(text.GetTextThickness() or 150000)
            field.SetTextAngle(pcbnew.EDA_ANGLE(0, pcbnew.DEGREES_T))
            field.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
            field.SetVertJustify(pcbnew.GR_TEXT_V_ALIGN_CENTER)
            labels[field.m_Uuid.AsString()] = (
                part.m_Uuid.AsString(),
                text.m_Uuid.AsString(),
            )
            # Keep native source objects alive. Delete free text only in the
            # source-preserving campaign, avoiding SWIG ownership invalidation.
        return labels, removed

    @staticmethod
    def rectangle(box) -> tuple:
        """Corners of a bounding box.

        Inputs:
            box: A KiCad bounding box.
        Outputs:
            (left, top, right, bottom) in nm.
        """
        return box.GetX(), box.GetY(), box.GetRight(), box.GetBottom()

    @staticmethod
    def overlaps(a: tuple, b: tuple, gap: int = 200000) -> bool:
        """Say whether two rectangles overlap or are closer than a gap.

        Inputs:
            a, b: (left, top, right, bottom) rectangles in nm.
            gap: Smallest allowed clearance, in nm.
        Outputs:
            True when they overlap or are closer than the gap.
        """
        return not (
            a[2] + gap <= b[0]
            or b[2] + gap <= a[0]
            or a[3] + gap <= b[1]
            or b[3] + gap <= a[1]
        )

    @staticmethod
    def place(board, parts: dict, labels: dict, removed: set) -> None:
        """Position each new field on a clear spot near its footprint.

        A fuse field (reference starting with F) goes 1.2 mm above the topmost of F5 to
        F8. Any other field takes the first of eight directions at growing distances
        from 1.35 mm to 4.1 mm that keeps 0.25 mm inside the board outline and 0.2 mm
        clear of mask openings and other front silkscreen text.

        Inputs:
            board: The loaded board, edited in memory.
            parts: Footprints keyed by reference.
            labels, removed: The result of ``bind``.
        Outputs:
            None. Raises ValueError when no clear position exists for a field.
        """
        import pcbnew

        rectangle, overlaps = BoardLabels.rectangle, BoardLabels.overlaps
        points = [
            p
            for shape in board.GetDrawings()
            if isinstance(shape, pcbnew.PCB_SHAPE)
            and shape.GetLayer() == pcbnew.Edge_Cuts
            for p in (shape.GetStart(), shape.GetEnd())
        ]
        bounds = (
            min(p.x for p in points),
            min(p.y for p in points),
            max(p.x for p in points),
            max(p.y for p in points),
        )

        masks = [
            rectangle(pad.GetBoundingBox())
            for pad in board.GetPads()
            if pad.IsOnLayer(pcbnew.F_Mask)
        ]
        superseded = removed | {old_uid for _, old_uid in labels.values()}
        obstacles = [
            rectangle(text.GetBoundingBox())
            for text in board.GetDrawings()
            if isinstance(text, pcbnew.PCB_TEXT)
            and text.GetLayer() == pcbnew.F_SilkS
            and text.m_Uuid.AsString() not in superseded
        ]
        field_by_id = {
            field.m_Uuid.AsString(): (part, field)
            for part in board.GetFootprints()
            for field in part.GetFields()
            if field.m_Uuid.AsString() in labels
        }
        fuse_parts = [parts[ref] for ref in ("F5", "F6", "F7", "F8") if ref in parts]
        fuse_y = (
            min(p.GetBoundingBox(False, False).GetY() for p in fuse_parts) - 1200000
            if fuse_parts
            else 0
        )
        for uid in sorted(labels, key=lambda key: field_by_id[key][0].GetReference()):
            part, field = field_by_id[uid]
            center = part.GetPosition()
            if part.GetReference().startswith("F"):
                field.SetPosition(pcbnew.VECTOR2I(center.x, fuse_y))
                obstacles.append(rectangle(field.GetBoundingBox()))
                continue
            candidates = [
                (dx * radius, dy * radius)
                for radius in (
                    1350000,
                    1600000,
                    1850000,
                    2100000,
                    2600000,
                    3100000,
                    3600000,
                    4100000,
                )
                for dx, dy in (
                    (0, -1),
                    (0, 1),
                    (1, 0),
                    (-1, 0),
                    (1, -1),
                    (-1, -1),
                    (1, 1),
                    (-1, 1),
                )
            ]
            for dx, dy in candidates:
                field.SetPosition(pcbnew.VECTOR2I(center.x + dx, center.y + dy))
                box = rectangle(field.GetBoundingBox())
                if (
                    box[0] < bounds[0] + 250000
                    or box[1] < bounds[1] + 250000
                    or box[2] > bounds[2] - 250000
                    or box[3] > bounds[3] - 250000
                ):
                    continue
                if not any(
                    overlaps(box, obstacle) for obstacle in (*masks, *obstacles)
                ):
                    obstacles.append(box)
                    break
            else:
                raise ValueError(
                    "No nearby mask-clear label position for " + part.GetReference()
                )


class SilkscreenPlanner:
    """Plans configured silkscreen moves and edge clipping on a native board."""

    @staticmethod
    def move_texts(
        board, policy: dict, graphics: list, silks: set, edited: dict, counts: dict
    ) -> None:
        """Move the configured silkscreen texts.

        A text may be placed at an absolute position, to the right of a component bank,
        or centred below one.

        Inputs:
            board: The loaded board, edited in memory.
            policy: The silkscreen policy with ``text_positions`` and ``text_groups``.
            graphics: Every board and footprint graphic item.
            silks: The silkscreen layer ids.
            edited: Mapping from uuid to (form kind, edited fields); updated.
            counts: Counters including ``moved_texts``; updated.
        Outputs:
            None. Raises ValueError when a configured item is not silkscreen text or a
            caption component bank is missing.
        """
        import pcbnew

        indexed = {item.m_Uuid.AsString(): item for item in graphics}
        positions = list(policy.get("text_positions", []))
        for group in policy.get("text_groups", []):
            positions.extend(
                {"uuid": uid, "x_mm": group["x_mm"]} for uid in group["items"]
            )
        for move in positions:
            item = indexed.get(move["uuid"])
            if not isinstance(item, pcbnew.PCB_TEXT) or item.GetLayer() not in silks:
                raise ValueError(
                    "Configured label is missing or is not silkscreen text"
                )
            old = item.GetPosition()
            if "right_of" in move:
                parts = [
                    board.FindFootprintByReference(ref) for ref in move["right_of"]
                ]
                if not parts or any(part is None for part in parts):
                    raise ValueError("Caption component bank is missing")
                right = max(
                    part.GetBoundingBox(False, False).GetRight() for part in parts
                )
                desired_left = right + round(move.get("gap_mm", 1.0) * 1e6)
                offset = old.x - item.GetBoundingBox().GetX()
                move = {**move, "x_mm": math.ceil((desired_left + offset) / 250000) / 4}
            if "below_bank" in move:
                parts = [
                    board.FindFootprintByReference(ref) for ref in move["below_bank"]
                ]
                boxes = [part.GetBoundingBox(False, False) for part in parts]
                item.SetTextAngle(pcbnew.EDA_ANGLE(0, pcbnew.DEGREES_T))
                item.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
                item.SetVertJustify(pcbnew.GR_TEXT_V_ALIGN_CENTER)
                center_x = (
                    min(box.GetX() for box in boxes)
                    + max(box.GetRight() for box in boxes)
                ) / 2
                height = item.GetBoundingBox().GetHeight()
                center_y = (
                    max(box.GetBottom() for box in boxes)
                    + move.get("gap_mm", 1.0) * 1e6
                    + height / 2
                )
                move = {
                    **move,
                    "x_mm": round(center_x / 250000) / 4,
                    "y_mm": math.ceil(center_y / 250000) / 4,
                }
                edited[item.m_Uuid.AsString()] = ("gr_text", ("at", "effects"))
            desired = pcbnew.VECTOR2I(
                round(move.get("x_mm", old.x / 1e6) * 1e6),
                round(move.get("y_mm", old.y / 1e6) * 1e6),
            )
            if old != desired:
                item.SetPosition(desired)
                edited.setdefault(item.m_Uuid.AsString(), ("gr_text", ("at",)))
                counts["moved_texts"] += 1

    @staticmethod
    def clamp(point, limits: tuple):
        """Clamp a point into a rectangle.

        Inputs:
            point: A KiCad point, in nm.
            limits: (x_min, y_min, x_max, y_max) in nm.
        Outputs:
            The nearest point inside the limits.
        """
        import pcbnew

        return pcbnew.VECTOR2I(
            max(limits[0], min(point.x, limits[2])),
            max(limits[1], min(point.y, limits[3])),
        )

    @staticmethod
    def clip_edges(
        board, policy: dict, graphics: list, silks: set, edited: dict, counts: dict
    ) -> None:
        """Pull silk line ends inside the board outline margin.

        Lines of a footprint listed in ``library_silk_sync`` are clipped symmetrically
        about the footprint origin; others to the margin inside the outline.

        Inputs:
            board: The loaded board, edited in memory.
            policy: The silkscreen policy with ``edge_margin_mm`` (0.15 to 2 mm, default
                0.25 mm) and ``library_silk_sync``.
            graphics: Every board and footprint graphic item.
            silks: The silkscreen layer ids.
            edited: Mapping from uuid to (form kind, edited fields); updated.
            counts: Counters including ``clipped_lines``; updated.
        Outputs:
            None. Raises ValueError when the outline is missing or not rectilinear, the
            margin is out of range, a footprint origin is too close to the edge, or a
            line would collapse.
        """
        import pcbnew

        outline = [
            x
            for x in board.GetDrawings()
            if isinstance(x, pcbnew.PCB_SHAPE) and x.GetLayer() == pcbnew.Edge_Cuts
        ]
        if not outline or any(
            x.GetShape() not in {pcbnew.SHAPE_T_RECT, pcbnew.SHAPE_T_SEGMENT}
            for x in outline
        ):
            raise ValueError(
                "Automatic silk-edge cleanup currently requires a rectilinear outline"
            )
        points = [p for shape in outline for p in (shape.GetStart(), shape.GetEnd())]
        xmin, xmax = min(p.x for p in points), max(p.x for p in points)
        ymin, ymax = min(p.y for p in points), max(p.y for p in points)
        margin = policy.get("edge_margin_mm", 0.25)
        if not isinstance(margin, (int, float)) or not 0.15 <= margin <= 2:
            raise ValueError("Silkscreen edge margin must be between 0.15 and 2 mm")
        for item in graphics:
            if (
                not isinstance(item, pcbnew.PCB_SHAPE)
                or item.GetLayer() not in silks
                or item.GetShape() != pcbnew.SHAPE_T_SEGMENT
            ):
                continue
            gap = round(margin * 1e6) + (item.GetWidth() + 1) // 2
            limits = (xmin + gap, ymin + gap, xmax - gap, ymax - gap)
            start, end = item.GetStart(), item.GetEnd()
            parent = item.GetParentFootprint()
            balanced = {
                binding["reference"] for binding in policy.get("library_silk_sync", [])
            }
            if parent is not None and parent.GetReference() in balanced:
                center = parent.GetPosition()
                rx = min(center.x - xmin - gap, xmax - gap - center.x)
                ry = min(center.y - ymin - gap, ymax - gap - center.y)
                if rx <= 0 or ry <= 0:
                    raise ValueError("Footprint origin is too close to the board edge")
                limits = (center.x - rx, center.y - ry, center.x + rx, center.y + ry)
            a = SilkscreenPlanner.clamp(start, limits)
            b = SilkscreenPlanner.clamp(end, limits)
            if a != start or b != end:
                if a == b:
                    raise ValueError("Edge cleanup would collapse a silk line")
                item.SetStart(a)
                item.SetEnd(b)
                kind = "gr_line" if item.GetParentFootprint() is None else "fp_line"
                edited[item.m_Uuid.AsString()] = (kind, ("start", "end"))
                counts["clipped_lines"] += 1

    @staticmethod
    def transfer(
        project: Path, board, edited: dict, owned: dict, removed: set
    ) -> bytes:
        """Carry the in-memory silkscreen edits into the saved board source.

        The edited board is serialized to a temporary file and only the edited fields,
        new label fields and removed texts are applied to the saved source as campaign
        changes.

        Inputs:
            project: Folder of the KiCad project.
            board: The edited loaded board.
            edited: Mapping from uuid to (form kind, edited fields).
            owned: New label fields from ``BoardLabels.attach``.
            removed: Uuids of texts to delete.
        Outputs:
            The edited board bytes. Raises RuntimeError when native serialization fails.
        """
        import pcbnew

        with tempfile.TemporaryDirectory(prefix="autopcb-silkscreen-") as directory:
            serialized = Path(directory) / "serialized.kicad_pcb"
            if not pcbnew.SaveBoard(str(serialized), board):
                raise RuntimeError("Native silkscreen serialization failed")
            text = serialized.read_text(encoding="utf-8")
            forms = {
                form.children("uuid")[0].atoms()[1].value: form
                for form in parse(text).descendants()
                if form.children("uuid")
            }
            changes = []
            for uid in removed:
                changes.append(
                    {"op": "remove", "target": {"kind": "gr_text", "uuid": uid}}
                )
            for uid, (parent, old_uid) in owned.items():
                field = forms[uid]
                changes.extend(
                    [
                        {
                            "op": "remove",
                            "target": {"kind": "gr_text", "uuid": old_uid},
                        },
                        {
                            "op": "insert",
                            "target": {"kind": "footprint", "uuid": parent},
                            "expression": text[field.start : field.end],
                        },
                    ]
                )
            for uid, (kind, fields) in edited.items():
                for name in fields:
                    field = forms[uid].children(name)[0]
                    changes.append(
                        {
                            "op": "replace",
                            "target": {"kind": kind, "uuid": uid},
                            "path": [name],
                            "expression": text[field.start : field.end],
                        }
                    )
            return CampaignPlanner(project).plan("pcb", changes).after

    @staticmethod
    def plan(project: Path, policy: dict) -> tuple[bytes, dict[str, int]]:
        """Use native board coordinates, then transfer only selected field edits.

        Inputs:
            project: Folder of the KiCad project.
            policy: The silkscreen policy; ``schema`` must be 1.
        Outputs:
            (board bytes, counts) with ``moved_texts``, ``clipped_lines``,
            ``attached_labels`` and ``removed_orphan_labels``. Raises ValueError for an
            unsupported policy or any rejected edit, and RuntimeError when the candidate
            changes anything but silkscreen.
        """
        import pcbnew

        if policy.get("schema") != 1:
            raise ValueError("Unsupported silkscreen policy")
        original = (native_path(project, ".kicad_pcb")).read_bytes()
        board = pcbnew.LoadBoard(str(native_path(project, ".kicad_pcb")))
        silks = {pcbnew.F_SilkS, pcbnew.B_SilkS}
        graphics = [
            *board.GetDrawings(),
            *(x for f in board.GetFootprints() for x in f.GraphicalItems()),
        ]
        edited, counts = {}, {"moved_texts": 0, "clipped_lines": 0}
        SilkscreenPlanner.move_texts(board, policy, graphics, silks, edited, counts)
        owned, removed = BoardLabels.attach(board, policy)
        counts.update(attached_labels=len(owned), removed_orphan_labels=len(removed))
        SilkscreenPlanner.clip_edges(board, policy, graphics, silks, edited, counts)
        stock = policy.get("stock_silkscreen", [])
        if not edited and not owned and not removed:
            candidate = StockSilk.restore(original, stock)
            candidate = BoardLabels.order_fields(candidate)
            SilkscreenProofs.verify_only(original, candidate)
            return candidate, counts
        candidate = SilkscreenPlanner.transfer(project, board, edited, owned, removed)
        candidate = StockSilk.restore(candidate, stock)
        candidate = BoardLabels.order_fields(candidate)
        SilkscreenProofs.verify_only(original, candidate)
        return candidate, counts


class SilkscreenCleanup:
    """One checked silkscreen cleanup of a project.

    Inputs:
        project: Folder of the KiCad project.
        apply: Save the checked board when True; only preview it when False.
        top_alignment: Align only the front connector label text.
    """

    def __init__(
        self, project: Path, *, apply: bool = False, top_alignment: bool = False
    ):
        """Store the project and the cleanup mode.

        Inputs:
            project: Folder of the KiCad project.
            apply: Save the checked board when True.
            top_alignment: Align only the front connector label text.
        """
        self.project = project
        self.apply = apply
        self.top_alignment = top_alignment

    @staticmethod
    def replace_saved_file(path: Path, expected: bytes, content: bytes) -> None:
        """Replace one owned source after readback, with same-volume atomic staging.

        Inputs:
            path: The file to replace.
            expected: The bytes the file must still hold.
            content: The new bytes.
        Outputs:
            None. Raises RuntimeError when the file no longer holds the expected bytes.
        """
        fd, temporary = tempfile.mkstemp(
            prefix=".silkscreen-", suffix=".tmp", dir=path.parent
        )
        staged = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if path.read_bytes() != expected:
                raise RuntimeError("Source changed before silkscreen publication")
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)

    @staticmethod
    def silk_count(report: dict) -> int:
        """Count the silkscreen findings of a DRC report.

        Inputs:
            report: Native DRC report with its ``violations`` list.
        Outputs:
            Number of findings whose type starts with ``silk_``.
        """
        return sum(v["type"].startswith("silk_") for v in report["violations"])

    @staticmethod
    def write(path, content) -> bool:
        """Write bytes to a file and report success to the save guard.

        Inputs:
            path: Target file.
            content: Bytes to write.
        Outputs:
            True.
        """
        Path(path).write_bytes(content)
        return True

    def publish(self, guard, candidate: bytes, saved_libraries: dict) -> None:
        """Save the library files and the board, undoing the library writes on failure.

        Inputs:
            guard: The board save guard holding the checked dependencies.
            candidate: The checked board bytes.
            saved_libraries: Mapping from library file to (prior bytes, new bytes).
        Outputs:
            None. Any failure restores the written libraries and is re-raised.
        """
        import pcbnew

        guard.verify_unchanged()
        written = []
        try:
            for path, (prior, content) in saved_libraries.items():
                self.replace_saved_file(path, prior, content)
                written.append(path)
            updated = tuple(
                (path, file_sha256(path) if path in saved_libraries else digest)
                for path, digest in guard.fingerprints
            )
            BoardSaveGuard(guard.source, updated).save(
                candidate, guard.source, self.write, pcbnew.LoadBoard
            )
        except BaseException:
            for path in reversed(written):
                prior, content = saved_libraries[path]
                self.replace_saved_file(path, content, prior)
            raise

    def run(self) -> dict:
        """Plan, check and optionally save the silkscreen cleanup.

        Outputs:
            The report. ``status`` is ``unchanged`` when nothing differs, ``checked``
            for a preview and ``saved`` after --apply, with the plan counts, the silk
            finding counts before and after and the open connection count. Raises
            RuntimeError when native DRC finds anything new, the silk findings increase,
            a non-silkscreen item changes or the tools change before publication.
        """
        import pcbnew
        from pcb.net_routing.controller import encode_checkpoint
        from pcb.net_routing.isolation import run_native
        from pcb.net_routing.native import Snapshot
        from pcb.net_routing.runner import _exclusive, _save
        from pcb.width_repair import DrcFindings

        project, apply, top_alignment = self.project, self.apply, self.top_alignment
        assets = asset_root(project)
        policy_path = assets / "config" / "silkscreen.json"
        config = json.loads(policy_path.read_bytes())
        cli = kicad_cli()
        with _exclusive(assets / "results" / "autoroute" / "active.lock"):
            with Snapshot(native_path(project, ".kicad_pcb")) as snapshot:
                dependencies = dict(snapshot.fingerprints)
                dependencies[policy_path] = file_sha256(policy_path)
                for binding in (
                    () if top_alignment else config.get("stock_silkscreen", [])
                ):
                    path = Path(binding["path"]).resolve()
                    dependencies[path] = file_sha256(path)
                guard = BoardSaveGuard(snapshot.source, tuple(dependencies.items()))
                guard.verify_editor_unlocked()
                if top_alignment:
                    candidate, counts = run_native(
                        TopLabels.plan, snapshot.directory, config["top_alignment"]
                    )
                    library_updates = {}
                else:
                    candidate, counts = run_native(
                        SilkscreenPlanner.plan, snapshot.directory, config
                    )
                    library_updates = StockSilk.sync_libraries(
                        snapshot.directory, snapshot.original, candidate, config
                    )
                saved_libraries = {
                    project / path.relative_to(snapshot.directory): (
                        path.read_bytes(),
                        content,
                    )
                    for path, content in library_updates.items()
                }
                if candidate == snapshot.original:
                    return {"status": "unchanged", **counts}
                print(
                    "Checking silkscreen repairs against the unchanged saved copper",
                    flush=True,
                )
                before = _drc(
                    cli,
                    snapshot.board_path,
                    snapshot.directory / "before.json",
                    schematic_parity=True,
                )
                snapshot.board_path.write_bytes(candidate)
                for path, content in library_updates.items():
                    path.write_bytes(content)
                after = _drc(
                    cli,
                    snapshot.board_path,
                    snapshot.directory / "after.json",
                    schematic_parity=True,
                )
                board = pcbnew.LoadBoard(str(snapshot.board_path))
                # Copper and filled-zone definitions are identical, so existing neck
                # markers may be matched independent of native adjacent-item choice.
                nets = frozenset(str(net) for net in board.GetNetsByName() if str(net))
                conflicts = DrcFindings.introduced(before, after, stable_neck_nets=nets)
                if conflicts:
                    raise RuntimeError(
                        "Silkscreen candidate introduced native findings: "
                        + "; ".join(
                            v["description"]
                            + " "
                            + str(
                                [
                                    (i.get("description"), i.get("pos"))
                                    for i in v.get("items", [])
                                ]
                            )
                            for v in conflicts[:4]
                        )
                    )
                result = {
                    "status": "checked",
                    **counts,
                    "silk_before": self.silk_count(before),
                    "silk_after": self.silk_count(after),
                    "opens": len(after["unconnected_items"]),
                }
                if result["silk_after"] > result["silk_before"]:
                    raise RuntimeError(
                        "Silkscreen repair worsened native silk findings"
                    )
                SilkscreenProofs.verify_only(snapshot.original, candidate)
                if top_alignment:
                    SilkscreenProofs.verify_top_alignment(snapshot.original, candidate)
                snapshot.check()
                tools = snapshot.tool_hashes
                if apply:
                    inputs = {
                        p.relative_to(project).as_posix(): value
                        for p, value in dependencies.items()
                        if p.is_relative_to(project)
                    }
                    journal = {
                        "kind": "pre_silkscreen_save",
                        "created_utc": datetime.now(timezone.utc).isoformat(),
                        "manufacturing_approved": False,
                        "library_before": {
                            p.relative_to(project).as_posix(): prior.decode("utf-8")
                            for p, (prior, _) in saved_libraries.items()
                        },
                        "board_before": encode_checkpoint(
                            snapshot.original, inputs, tools, dependencies[policy_path]
                        ),
                    }
                    _save(
                        assets
                        / "results"
                        / "autoroute"
                        / "preimages"
                        / "working-save.json",
                        journal,
                    )
            if apply:
                if Snapshot.tools() != tools:
                    raise RuntimeError("Tools changed before silkscreen publication")
                self.publish(guard, candidate, saved_libraries)
                result["status"] = "saved"
            return result


class SilkscreenCommand:
    """Command-line entry that runs one checked silkscreen cleanup."""

    def __call__(self, argv=None) -> int:
        """Parse the command line, run the cleanup and print its report.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0.
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--project", required=True, type=Path)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--top-alignment",
            action="store_true",
            help="Align only front connector label text",
        )
        args = parser.parse_args(argv)
        with quiet_duplicate_image_handlers():
            result = SilkscreenCleanup(
                args.project.resolve(),
                apply=args.apply,
                top_alignment=args.top_alignment,
            ).run()
        print("Silkscreen:", result["status"])
        for name, value in result.items():
            if name != "status":
                print(f"  {name.replace('_', ' ')}: {value}")
        return 0


main = SilkscreenCommand()

if __name__ == "__main__":
    raise SystemExit(main())
