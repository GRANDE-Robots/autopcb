#!/usr/bin/env python3
"""layout_sections.py: make the one-sheet Jessie schematic read as a section grid.

This is intentionally a presentation-only operation.  It can add or remove a
thin, unfilled frame around each numbered functional section while restoring
the reviewed title anchors that protect the local drawing.  It never edits
symbols, pins, labels, wires, junctions, no-connect markers, or the page
settings/drawing sheet.

The coordinates are a reviewed layout manifest for this sheet, while the
formatting rule is shared: a 50-mil grid, one 100-mil inner title inset, a
single muted-blue stroke, and an explicit gap between frames.  UUID5-derived
frame identities make repeated runs idempotent instead of accumulating artwork.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from sch.schematic_io import SiblingTempWriter

GRID = 1.27  # 50 mil in mm
FRAME_UUID_NAMESPACE = uuid.UUID("762b12c6-c7be-4ef4-9bd6-4b3f6156bfe9")
FRAME_STROKE = "\t\t\t(width 0.254)\n\t\t\t(type solid)\n\t\t\t(color 79 115 171 1)"


@dataclass(frozen=True)
class Section:
    """One numbered functional section: its frame corners and heading anchor.

    Inputs:
        title: Exact heading text of the section.
        x1, y1, x2, y2: Frame corners in mm; (x1, y1) is the upper left.
        heading_x, heading_y: Position in mm of the heading text.
    """

    title: str
    x1: float
    y1: float
    x2: float
    y2: float
    heading_x: float
    heading_y: float

    @property
    def uuid(self) -> str:
        """The stable UUID5 of this section's frame, derived from its title."""
        return str(
            uuid.uuid5(FRAME_UUID_NAMESPACE, f"jessie-section-grid:{self.title}")
        )


# Each boundary lands on the native 50-mil grid.  The manifest deliberately
# leaves a 100-mil channel between neighboring frames so the sheet stays legible
# at overview scale as well as when printed.
SECTIONS: tuple[Section, ...] = (
    Section("1. POWER", 27.94, 24.13, 218.44, 162.56, 31.750, 27.178),
    Section(
        "2. TEENSY 4.1 CONTROL CORE", 220.98, 24.13, 335.28, 110.49, 223.774, 26.924
    ),
    Section(
        "7. FOUR FORGE CAMERA I/O CHANNELS",
        340.36,
        24.13,
        449.58,
        110.49,
        341.630,
        27.178,
    ),
    Section("9. ENCODER", 454.66, 24.13, 502.92, 110.49, 452.628, 26.924),
    Section("10. LIMIT SWITCH", 505.46, 24.13, 551.18, 110.49, 505.714, 26.670),
    Section("12. LIGHTING", 553.72, 24.13, 665.48, 167.64, 554.990, 26.924),
    Section("3. RTC-BASED LOCAL PPS", 27.94, 163.83, 109.22, 231.14, 41.910, 167.894),
    Section("4. MTi-30 SYNC", 111.76, 163.83, 193.04, 231.14, 110.998, 166.878),
    Section(
        "5. LiDAR TIMING | CHANNELS 1 / 2",
        220.98,
        113.03,
        335.28,
        191.77,
        223.774,
        115.824,
    ),
    Section(
        "6. LiDAR TIMING | CHANNELS 3 / 4",
        195.58,
        194.31,
        335.28,
        238.76,
        197.866,
        192.278,
    ),
    Section("8. MOTOR CONTROL", 337.82, 113.03, 449.58, 238.76, 343.154, 115.824),
    Section("11. ARM JOG INPUT", 452.12, 113.03, 551.18, 181.61, 453.898, 115.824),
    Section(
        "23. USB HOST-PRESENCE DATA GATE",
        568.96,
        181.61,
        655.32,
        238.76,
        572.516,
        183.642,
    ),
    Section("13. LOCAL DECOUPLING", 27.94, 241.30, 568.96, 325.12, 33.782, 241.554),
    Section("17. STATUS INDICATORS", 568.96, 241.30, 655.32, 325.12, 570.230, 244.094),
    Section(
        "20. SERVICE TEST POINTS", 751.84, 241.30, 812.80, 325.12, 754.126, 244.094
    ),
    Section(
        "14. TRANSIENT SUPPRESSION", 27.94, 327.66, 129.54, 424.18, 37.592, 328.168
    ),
    Section(
        "15. ELECTROSTATIC DISCHARGE", 132.08, 327.66, 287.02, 454.66, 132.334, 328.168
    ),
    Section(
        "16. CONVERTER-FAULT CONTAINMENT",
        289.56,
        327.66,
        568.96,
        454.66,
        286.004,
        328.168,
    ),
    Section("18. POWER FLAGS", 568.96, 327.66, 655.32, 383.54, 572.516, 330.708),
    Section("19. FUSES", 657.86, 327.66, 749.30, 383.54, 659.130, 330.708),
    Section("21. CONNECTOR BANK", 568.96, 386.08, 812.80, 612.14, 568.960, 388.620),
    Section(
        "22. VLP RJ45 CURRENT LIMITS", 289.56, 457.20, 510.54, 571.50, 292.100, 459.740
    ),
)


class SectionGrid:
    """Plans the section frames and heading anchors of the one-sheet schematic."""

    @staticmethod
    def matching_paren(source: str, start: int) -> int:
        """Return the end offset of the balanced S-expression at *start*.

        Raises ValueError when ``start`` is not an opening parenthesis or the
        expression is not closed.
        """
        if source[start] != "(":
            raise ValueError(f"expected '(' at {start}")
        depth = 0
        quoted = False
        escaped = False
        for index in range(start, len(source)):
            char = source[index]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == '"':
                quoted = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return index + 1
        raise ValueError(f"unbalanced expression at {start}")

    @staticmethod
    def frame_block(section: Section) -> str:
        """Render the unfilled frame rectangle of one section as schematic text."""
        return (
            "\t(rectangle\n"
            f"\t\t(start {section.x1:.3f} {section.y1:.3f})\n"
            f"\t\t(end {section.x2:.3f} {section.y2:.3f})\n"
            "\t\t(stroke\n"
            f"{FRAME_STROKE}\n"
            "\t\t)\n"
            "\t\t(fill\n"
            "\t\t\t(type none)\n"
            "\t\t)\n"
            f"\t\t(uuid {section.uuid})\n"
            "\t)\n"
        )

    @staticmethod
    def validate_frame_manifest() -> None:
        """Check the section manifest for duplicates, bad corners and overlap.

        Raises ValueError naming the first offending section or pair.
        """
        titles = [section.title for section in SECTIONS]
        if len(titles) != len(set(titles)):
            raise ValueError("duplicate section title in manifest")
        for section in SECTIONS:
            values = (section.x1, section.y1, section.x2, section.y2)
            if section.x1 >= section.x2 or section.y1 >= section.y2:
                raise ValueError(f"inverted frame for {section.title}")
            if any(abs(value / GRID - round(value / GRID)) > 0.021 for value in values):
                raise ValueError(f"off-grid frame for {section.title}: {values}")
        for index, left in enumerate(SECTIONS):
            for right in SECTIONS[index + 1 :]:
                overlap_x = min(left.x2, right.x2) - max(left.x1, right.x1)
                overlap_y = min(left.y2, right.y2) - max(left.y1, right.y1)
                if overlap_x > 0 and overlap_y > 0:
                    raise ValueError(f"frame overlap: {left.title} / {right.title}")

    @classmethod
    def find_heading_block(cls, source: str, title: str) -> tuple[int, int, str]:
        """Locate the one top-level ``(text "<title>" ...)`` block of a heading.

        Outputs:
            Start offset, end offset and text of the block. Raises ValueError
            unless exactly one heading has that title.
        """
        pattern = re.compile(r'^\t\(text "' + re.escape(title) + r'"', re.MULTILINE)
        matches = list(pattern.finditer(source))
        if len(matches) != 1:
            raise ValueError(f"expected one heading {title!r}, found {len(matches)}")
        start = matches[0].start() + 1
        end = cls.matching_paren(source, start)
        return start, end, source[start:end]

    @staticmethod
    def align_heading(section: Section, block: str) -> str:
        """Move a heading block to the section's reviewed anchor, keeping its angle.

        Raises ValueError when the block has no ``(at x y angle)`` position.
        """
        target = f"(at {section.heading_x:.3f} {section.heading_y:.3f} 0)"
        at_re = re.compile(r"\(at\s+[-+0-9.eE]+\s+[-+0-9.eE]+\s+[-+0-9.eE]+\)")
        changed, count = at_re.subn(target, block, count=1)
        if count != 1:
            raise ValueError(f"heading {section.title!r} has no usable position")
        return changed

    @classmethod
    def remove_generated_frame(cls, source: str, frame_uuid: str) -> tuple[str, int]:
        """Delete the rectangle this tool generated for ``frame_uuid``, if present.

        Outputs:
            The new source and the number of frames removed (0 or 1). Raises
            ValueError when the UUID is found without a rectangle around it.
        """
        hit = source.find(f"(uuid {frame_uuid})")
        if hit < 0:
            return source, 0
        starts = [
            match.start()
            for match in re.finditer(r"^\t\(rectangle\b", source[:hit], re.MULTILINE)
        ]
        if not starts:
            raise ValueError(f"cannot locate generated rectangle for {frame_uuid}")
        start = starts[-1] + 1
        end = cls.matching_paren(source, start)
        block = source[start:end]
        if f"(uuid {frame_uuid})" not in block:
            raise ValueError(f"UUID {frame_uuid} is not in its candidate rectangle")
        line_start = start - 1
        line_end = end + (1 if end < len(source) and source[end] == "\n" else 0)
        return source[:line_start] + source[line_end:], 1

    @classmethod
    def plan(cls, source: str, *, include_frames: bool) -> tuple[str, int, int]:
        """Rebuild the schematic text with aligned headings and, optionally, frames.

        Inputs:
            source: Complete schematic text.
            include_frames: Draw every section frame; False removes them.
        Outputs:
            The planned text, the number of sections and the number of headings
            aligned. Raises ValueError when the manifest is invalid, a heading is
            missing or ambiguous, or the schematic root has no terminator.
        """
        cls.validate_frame_manifest()
        for section in SECTIONS:
            source, _ = cls.remove_generated_frame(source, section.uuid)

        replacements: list[tuple[int, int, str]] = []
        for section in SECTIONS:
            start, end, block = cls.find_heading_block(source, section.title)
            replacements.append((start, end, cls.align_heading(section, block)))
        for start, end, replacement in sorted(replacements, reverse=True):
            source = source[:start] + replacement + source[end:]

        root_end = source.rstrip().rfind(")")
        if root_end < 0:
            raise ValueError("missing schematic root terminator")
        # Individual frame removal preserves the newline that separated each
        # top-level item.  Normalize only the root tail here, then append the frame
        # group with exactly one delimiter.  This makes check/apply strictly
        # idempotent without ever joining adjacent S-expressions onto one line.
        prefix = source[:root_end].rstrip()
        frames = (
            "".join(cls.frame_block(section) for section in SECTIONS)
            if include_frames
            else ""
        )
        source = prefix + "\n" + frames + source[root_end:]
        return source, len(SECTIONS), len(replacements)


class SectionGridCommand:
    """Command-line entry: check, apply or remove the section frames."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Plan the section grid for one schematic and optionally write it.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 after printing the frame and heading counts and
            whether changes are pending. ``--apply`` and ``--remove`` replace the
            schematic atomically when the plan differs from it.
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("schematic", type=Path)
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument(
            "--check",
            action="store_true",
            help="validate the idempotent frame plan only",
        )
        mode.add_argument(
            "--apply",
            action="store_true",
            help="atomically apply the presentation-only frame plan",
        )
        mode.add_argument(
            "--remove",
            action="store_true",
            help="remove only this tool's frames and restore the reviewed title anchors",
        )
        args = parser.parse_args(argv)

        source = args.schematic.read_text(encoding="utf-8")
        include_frames = not args.remove
        planned, frames, headings = SectionGrid.plan(
            source, include_frames=include_frames
        )
        changed = planned != source
        print(f"section frames: {frames}")
        print(f"section headings aligned: {headings}")
        print(f"presentation changes pending: {'yes' if changed else 'no'}")
        if (args.apply or args.remove) and changed:
            SiblingTempWriter.write(args.schematic, planned)
            print(f"updated: {args.schematic}")
        return 0


main = SectionGridCommand()


if __name__ == "__main__":
    raise SystemExit(main())
