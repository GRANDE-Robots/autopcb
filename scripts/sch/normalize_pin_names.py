#!/usr/bin/env python3
"""normalize_pin_names.py: give currently generic J/U terminal text concise, real names.

This is a presentation-only complement to ``standardize_block_symbols.py``.  It
uses a small, auditable table for the few symbols whose source library supplied
anonymous ``PIN_n``/``Pin_n`` text.  It keeps every pin number, electrical
type, endpoint, net, footprint, component position, and page setting intact.
Only the embedded-symbol terminal text, the matching body's width, and that
symbol's reference/value field anchors may change.  The write is atomic.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from sch.schematic_io import AtomicTextWriter
from sch.standardize_block_symbols import (
    BlockSymbolWriter,
    SExpressionText,
    balanced_end,
    instances,
    source_lib_definitions,
)

TEMP_SUFFIX = ".terminal-names.tmp"

# Names are physical terminal functions, not new net names.  They are based on
# the exact connector/IC function already selected in the source schematic.
TERMINAL_NAMES: dict[str, dict[str, str]] = {
    # The power-only Type-C inlet retains the standard receptacle contact names
    # so its CC contacts read the same way as the two USB data receptacles.
    "Power_Input:USB_C_PowerOnly_16P_Vertical": {"A5": "CC1", "B5": "CC2"},
    "Uniform_Ports:J12_Uniform": {
        "1": "TOP+",
        "2": "TOP-",
        "3": "FRONT+",
        "4": "FRONT-",
    },
    "Uniform_Ports:J13_Uniform": {"1": "3V3", "2": "AMBIENT", "3": "GND"},
    "Uniform_Ports:J14_Uniform": {"1": "3V3", "2": "AMBIENT", "3": "GND"},
    # 74LVC2G08: dual two-input AND gate: 1A/1B/1Y and 2A/2B/2Y.
    "74xGxx:74LVC2G08": {
        "1": "1A",
        "2": "1B",
        "3": "2Y",
        "5": "2A",
        "6": "2B",
        "7": "1Y",
    },
    # INA180A2: output, ground, positive/negative sense inputs, supply.
    "Amplifier_Current:INA180A2": {
        "1": "OUT",
        "2": "GND",
        "3": "IN+",
        "4": "IN-",
        "5": "VS",
    },
}


class TerminalNames:
    """Renames anonymous terminal text of the embedded symbols the table lists."""

    @staticmethod
    def set_pin_name(pin: str, name: str) -> str:
        """Rewrite only one pin's name field, retaining type/number/effects.

        Raises ValueError when the pin has no name field.
        """
        match = re.search(r'\(name\s+"(?:[^"\\]|\\.)*"', pin)
        if not match:
            raise ValueError("pin has no name field")
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        return pin[: match.start()] + f'(name "{escaped}"' + pin[match.end() :]

    @classmethod
    def rename_definition(cls, block: str, aliases: dict[str, str]) -> tuple[str, int]:
        """Rename all matching physical pins across a multi-unit embedded symbol.

        Inputs:
            block: Text of one embedded definition.
            aliases: New terminal name by pin number.
        Outputs:
            The definition with the pins in ``aliases`` renamed and the number of
            pins that changed. Raises ValueError when a listed pin has no name
            field.
        """
        replacements: list[tuple[int, int, str]] = []
        for symbol_start, symbol_end in SExpressionText.direct_blocks(block, "symbol"):
            unit = block[symbol_start:symbol_end]
            for pin_start, pin_end in SExpressionText.direct_blocks(unit, "pin"):
                pin = unit[pin_start:pin_end]
                number = re.search(r'\(number\s+"([^"\\]+)"', pin)
                if not number or number.group(1) not in aliases:
                    continue
                expected = aliases[number.group(1)]
                current = re.search(r'\(name\s+"((?:[^"\\]|\\.)*)"', pin)
                if not current:
                    raise ValueError(f"pin {number.group(1)} has no name field")
                if current.group(1) == expected:
                    continue
                replacements.append(
                    (
                        symbol_start + pin_start,
                        symbol_start + pin_end,
                        cls.set_pin_name(pin, expected),
                    )
                )
        updated = block
        for start, end, replacement in reversed(replacements):
            updated = updated[:start] + replacement + updated[end:]
        return updated, len(replacements)

    @classmethod
    def normalize(cls, source: str) -> tuple[str, dict[str, int], int]:
        """Apply the terminal-name table to a schematic.

        Inputs:
            source: Complete schematic text.
        Outputs:
            The new text, the number of pins renamed per library ID, and the
            number of placed symbols restyled to the widened body. Raises
            ValueError when the source is not a KiCad schematic, a listed
            definition is not embedded, or the edits overlap.
        """
        root_start = source.find("(kicad_sch")
        if root_start < 0:
            raise ValueError("not a KiCad schematic")
        root_end = balanced_end(source, root_start)
        root = source[root_start:root_end]
        _lib_start, _lib_end, definitions = source_lib_definitions(root)
        all_instances = instances(root)
        writer = BlockSymbolWriter()
        replacements: list[tuple[int, int, str]] = []
        counts: dict[str, int] = {}
        touched_instances = 0
        for lib_id, aliases in TERMINAL_NAMES.items():
            definition = definitions.get(lib_id)
            if definition is None:
                raise ValueError(f"missing embedded definition for {lib_id}")
            renamed, count = cls.rename_definition(definition[2], aliases)
            if count == 0:
                continue
            # Rebuild only this existing one-sided definition: its pin endpoints
            # stay on the current grid, while its body expands just enough to hold
            # the newly visible physical terminal text.
            rebuilt, _templates, geometry = writer.generic_definition(
                lib_id, renamed, True
            )
            replacements.append((definition[0], definition[1], rebuilt))
            counts[lib_id] = count
            for instance in all_instances:
                if instance.lib_id != lib_id:
                    continue
                replacements.append(
                    (
                        instance.start,
                        instance.end,
                        writer.generic_instance(
                            instance, geometry, instance.x, instance.y
                        ),
                    )
                )
                touched_instances += 1
        replacements.sort(key=lambda item: (item[0], item[1]))
        for previous, current in zip(replacements, replacements[1:]):
            if current[0] < previous[1]:
                raise ValueError("terminal-name edits overlap")
        for start, end, replacement in reversed(replacements):
            root = root[:start] + replacement + root[end:]
        return (
            source[:root_start] + root + source[root_end:],
            counts,
            touched_instances,
        )


class TerminalNamesCommand:
    """Command-line entry: check or apply the terminal-name table."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Rename generic terminal text in one schematic.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 after printing the renamed-pin, definition and
            instance counts and the pins renamed per library. ``--apply``
            replaces the schematic atomically when anything changed.
        """
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("schematic", type=Path)
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--check", action="store_true")
        mode.add_argument("--apply", action="store_true")
        args = parser.parse_args(argv)
        source = args.schematic.read_text(encoding="utf-8")
        normalized, counts, instances_changed = TerminalNames.normalize(source)
        print(f"renamed_terminal_text={sum(counts.values())}")
        print(f"definitions_changed={len(counts)}")
        print(f"instances_restyled={instances_changed}")
        for lib_id, count in counts.items():
            print(f"{lib_id}: {count}")
        if args.apply and normalized != source:
            AtomicTextWriter(TEMP_SUFFIX).write(args.schematic, normalized)
            print("applied atomically")
        elif args.apply:
            print("already normalized")
        return 0


main = TerminalNamesCommand()


if __name__ == "__main__":
    raise SystemExit(main())
