"""PCB editing commands require explicit, source-bound in-place writes."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pcbnew

from pcb import (
    drag_routed_footprints,
    reconcile_named_nets,
    replace_connector_footprint,
)


class PcbCommandGuardTests(unittest.TestCase):
    def test_reconcile_requires_board_hash_before_loading(self):
        with tempfile.TemporaryDirectory(prefix="jessie-pcb-args-") as directory:
            root = Path(directory)
            board, schematic = root / "board.kicad_pcb", root / "board.kicad_sch"
            board.write_bytes(b"original")
            schematic.write_bytes(b"schematic")
            args = [
                "reconcile",
                "--board",
                str(board),
                "--schematic",
                str(schematic),
                "--apply",
            ]
            with (
                mock.patch.object(sys, "argv", args),
                mock.patch.object(pcbnew, "LoadBoard") as load,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    reconcile_named_nets.main()
            load.assert_not_called()
            self.assertEqual(board.read_bytes(), b"original")

    def test_drag_in_place_requires_board_hash_before_loading(self):
        with tempfile.TemporaryDirectory(prefix="jessie-pcb-args-") as directory:
            board = Path(directory) / "board.kicad_pcb"
            board.write_bytes(b"original")
            args = [
                "drag",
                str(board),
                "--move",
                "J1=1,2",
                "--apply",
                "--output",
                str(board),
                "--allow-in-place",
            ]
            with (
                mock.patch.object(sys, "argv", args),
                mock.patch.object(pcbnew, "LoadBoard") as load,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    drag_routed_footprints.main()
            load.assert_not_called()
            self.assertEqual(board.read_bytes(), b"original")

    def test_connector_in_place_requires_board_hash_before_loading(self):
        with tempfile.TemporaryDirectory(prefix="jessie-pcb-args-") as directory:
            root = Path(directory)
            board = root / "board.kicad_pcb"
            board.write_bytes(b"original")
            args = [
                "connector",
                str(board),
                "--reference",
                "J1",
                "--footprint-library",
                str(root),
                "--footprint-name",
                "candidate",
                "--pad-map",
                "1=1",
                "--apply",
                "--output",
                str(board),
                "--allow-in-place",
            ]
            with (
                mock.patch.object(sys, "argv", args),
                mock.patch.object(pcbnew, "LoadBoard") as load,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    replace_connector_footprint.main()
            load.assert_not_called()
            self.assertEqual(board.read_bytes(), b"original")

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="jessie-pcb-args-") as directory:
            root = Path(directory)
            board, output = root / "board.kicad_pcb", root / "existing.kicad_pcb"
            board.write_bytes(b"original")
            output.write_bytes(b"unrelated")
            cases = [
                (drag_routed_footprints, ["drag", str(board), "--move", "J1=1,2"]),
                (
                    replace_connector_footprint,
                    [
                        "connector",
                        str(board),
                        "--reference",
                        "J1",
                        "--footprint-library",
                        str(root),
                        "--footprint-name",
                        "candidate",
                        "--pad-map",
                        "1=1",
                    ],
                ),
            ]
            for module, args in cases:
                with (
                    self.subTest(command=args[0]),
                    mock.patch.object(
                        sys, "argv", [*args, "--apply", "--output", str(output)]
                    ),
                    mock.patch.object(pcbnew, "LoadBoard") as load,
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    with self.assertRaises(SystemExit):
                        module.main()
                    load.assert_not_called()
                self.assertEqual(output.read_bytes(), b"unrelated")
                self.assertEqual(board.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
