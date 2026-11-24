"""audit_pcb_power_terminals.py: read-only terminal and power-domain audit of a PCB.

This tool deliberately does not route, refill zones, modify board settings, or
write project files.  It checks the actual J1 2x7 inlet mapping and the power
domain terminal assignments that must remain separate while the board is
routed. A JSON contract can replace those project-specific defaults. When
supplied a native KiCad DRC JSON report, it also reports open edges by net.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import pcbnew

EXPECTED_J1 = {
    "1": "EXT5_CONV_RAW",
    "2": "LIGHTS_12_RAW",
    "3": "LIGHTS_12_RAW",
    "4": "LIGHTS_12_RAW",
    "5": "LIDAR_12_RAW",
    "6": "LIDAR_12_RAW",
    "7": "LIDAR_12_RAW",
    "8": "GND",
    "9": "GND",
    "10": "GND",
    "11": "GND",
    "12": "GND",
    "13": "GND",
    "14": "GND",
}

# These are static pin/pad contracts, not a claim that routing is complete.
REQUIRED_TERMINALS = {
    "EXT5_CONV_RAW": [("J1", "1"), ("U23", "1")],
    "LIGHTS_12_RAW": [("J1", "2"), ("J1", "3"), ("J1", "4"), ("U22", "1"), ("Q7", "5")],
    "LIGHTS_12V": [("U22", "7"), ("Q8", "5"), ("F3", "1"), ("F4", "1")],
    "LIDAR_12_RAW": [("J1", "5"), ("J1", "6"), ("J1", "7"), ("U21", "1"), ("Q5", "5")],
    "LIDAR_12_PROT": [
        ("U21", "7"),
        ("Q6", "5"),
        ("F5", "1"),
        ("F6", "1"),
        ("F7", "1"),
        ("F8", "1"),
    ],
    "TOP_LAMP_MINUS": [("J12", "2"), ("Q2", "5")],
    "FRONT_LAMP_MINUS": [("J12", "4"), ("Q3", "5")],
}


class PadNets:
    """Read the net of every pad of a loaded board."""

    def __call__(self, board: pcbnew.BOARD) -> dict[tuple[str, str], str]:
        """Map each (reference, pad number) pair to the name of its net.

        Outputs:
            The net name per pad; an empty name when pads that share a number sit
            on different nets, because such a pair cannot prove a contract.
        """
        nets: dict[tuple[str, str], set[str]] = {}
        for footprint in board.GetFootprints():
            for pad in footprint.Pads():
                nets.setdefault((footprint.GetReference(), pad.GetNumber()), set()).add(
                    pad.GetNetname()
                )
        # Duplicate pad numbers on different nets are ambiguous and cannot prove a contract.
        return {
            key: next(iter(values)) if len(values) == 1 else ""
            for key, values in nets.items()
        }


pad_map = PadNets()


class TerminalContract:
    """Terminal and separation requirements read from a JSON contract."""

    @staticmethod
    def endpoint(value):
        """Check one ``[reference, pad number]`` pair and return it as a tuple."""
        if (
            not isinstance(value, list)
            or len(value) != 2
            or any(not isinstance(item, str) or not item.strip() for item in value)
        ):
            raise ValueError("A terminal must be [reference, pad number]")
        return tuple(value)

    @classmethod
    def load(cls, path: Path) -> tuple[dict[str, list[tuple[str, str]]], list[dict]]:
        """Validate explicit terminal/net identities and named separation requirements.

        Inputs:
            path: JSON file with ``terminals`` and ``separations``.
        Outputs:
            The terminals per net and the separation rules. Raises ValueError when
            the contract is malformed.
        """
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or set(data) != {"terminals", "separations"}:
            raise ValueError("Contract must contain terminals and separations")
        raw_terminals, raw_separations = data["terminals"], data["separations"]
        if not isinstance(raw_terminals, dict) or not raw_terminals:
            raise ValueError("Contract terminals must be a nonempty object")

        terminals: dict[str, list[tuple[str, str]]] = {}
        seen: set[tuple[str, str]] = set()
        for net, points in raw_terminals.items():
            if (
                not isinstance(net, str)
                or not net.strip()
                or not isinstance(points, list)
                or not points
            ):
                raise ValueError(
                    "Each net must have a name and a nonempty terminal list"
                )
            terminals[net] = []
            for point in points:
                pin = cls.endpoint(point)
                if pin in seen:
                    raise ValueError(
                        f"Repeated or conflicting terminal: {pin[0]}.{pin[1]}"
                    )
                seen.add(pin)
                terminals[net].append(pin)
        if not isinstance(raw_separations, list):
            raise ValueError("Contract separations must be a list")
        separations = []
        names = set()
        for rule in raw_separations:
            if not isinstance(rule, dict) or set(rule) not in (
                {"name", "first", "second"},
                {"name", "first", "other_net"},
            ):
                raise ValueError(
                    "A separation needs name, first, and second or other_net"
                )
            name = rule["name"]
            if not isinstance(name, str) or not name.strip() or name in names:
                raise ValueError("Separation names must be nonempty and unique")
            names.add(name)
            first = cls.endpoint(rule["first"])
            if first not in seen:
                raise ValueError("Separation terminals must also have an expected net")
            if "second" in rule:
                second = cls.endpoint(rule["second"])
                if second not in seen or second == first:
                    raise ValueError(
                        "Separation requires two distinct expected terminals"
                    )
                separations.append({"name": name, "first": first, "second": second})
            else:
                other = rule["other_net"]
                if not isinstance(other, str) or not other.strip():
                    raise ValueError("other_net must be a nonempty net name")
                separations.append({"name": name, "first": first, "other_net": other})
        return terminals, separations


class DrcOpenEdges:
    """Count the open connections that a native DRC report names per power net."""

    def __call__(self, path: Path, domains: set[str]) -> Counter[str]:
        """Count native missing-connection edges that name a listed power net.

        Inputs:
            path: KiCad JSON DRC report.
            domains: Power net names to count.
        Outputs:
            Open edges per listed net. Raises ValueError when the report has no
            ``unconnected_items`` list or an edge names no net.
        """
        report = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(report.get("unconnected_items"), list):
            raise ValueError(
                "DRC report has no unconnected_items list; open connections are unknown"
            )
        counts: Counter[str] = Counter()
        for item in report["unconnected_items"]:
            names = {
                match.group(1)
                for endpoint in item.get("items", [])
                for match in [
                    re.search(r"\[([^\]]+)\]", endpoint.get("description", ""))
                ]
                if match
            }
            if not names:
                raise ValueError(
                    "DRC report has an unclassified open edge; per-net counts are unknown"
                )
            for name in names & domains:
                counts[name] += 1
        return counts


drc_open_counts = DrcOpenEdges()


class PowerTerminalAudit:
    """Check the terminal and power-domain contract of a saved board."""

    @staticmethod
    def builtin_separations() -> list[dict]:
        """Return the separation rules of the built-in Jessie contract."""
        return [
            {
                "name": "top_minus_is_not_ground",
                "first": ("J12", "2"),
                "other_net": "GND",
            },
            {
                "name": "front_minus_is_not_ground",
                "first": ("J12", "4"),
                "other_net": "GND",
            },
            {
                "name": "lamp_minus_domains_are_distinct",
                "first": ("J12", "2"),
                "second": ("J12", "4"),
            },
            {
                "name": "raw_lamp_and_lidar_domains_are_distinct",
                "first": ("J1", "2"),
                "second": ("J1", "5"),
            },
        ]

    def __call__(
        self, board_path: Path, contract: Path | None, drc_json: Path | None
    ) -> dict:
        """Audit one saved board against a terminal contract.

        Inputs:
            board_path: Saved ``.kicad_pcb`` file.
            contract: JSON contract that replaces the built-in Jessie contract.
            drc_json: Native KiCad JSON DRC report whose open edges are counted.
        Outputs:
            The report with the file hashes, the terminal mismatches, the domain
            separation results and the static contract failures. Raises
            RuntimeError when KiCad cannot load the board or when the board, the
            contract or the DRC report changes during the audit.
        """
        board_hash = hashlib.sha256(board_path.read_bytes()).hexdigest()
        board = pcbnew.LoadBoard(str(board_path))
        if board is None:
            raise RuntimeError("KiCad could not load the saved board")
        pads = pad_map(board)
        failures: list[str] = []

        contract_hash = None
        if contract:
            contract_hash = hashlib.sha256(contract.read_bytes()).hexdigest()
            terminals, separations = TerminalContract.load(contract)
            j1 = {}
        else:
            terminals = {
                net: list(points) for net, points in REQUIRED_TERMINALS.items()
            }
            j1 = {pin: pads.get(("J1", pin)) for pin in EXPECTED_J1}
            for pin, expected in EXPECTED_J1.items():
                actual = j1[pin]
                if actual != expected:
                    failures.append(
                        f"J1.{pin}: expected {expected or 'NC'}, found {actual or 'NC'}"
                    )
            separations = self.builtin_separations()

        terminal_check: dict[str, list[str]] = {}
        for net, net_terminals in terminals.items():
            mismatches = []
            for reference, pin in net_terminals:
                actual = pads.get((reference, pin))
                if actual != net:
                    mismatches.append(
                        f"{reference}.{pin}: expected {net}, found {actual or 'missing/NC'}"
                    )
            terminal_check[net] = mismatches
            failures.extend(mismatches)

        domain_separation = {}
        for rule in separations:
            first = pads.get(rule["first"])
            second = pads.get(rule["second"]) if "second" in rule else rule["other_net"]
            domain_separation[rule["name"]] = bool(first and second) and first != second
        for name, passed in domain_separation.items():
            if not passed:
                failures.append(name)

        domains = set(terminals)
        drc_hash = (
            hashlib.sha256(drc_json.read_bytes()).hexdigest() if drc_json else None
        )
        open_counts = drc_open_counts(drc_json, domains) if drc_json else None

        report = {
            "pcb": str(board_path),
            "source_sha256": board_hash,
            "contract": str(contract.resolve()) if contract else "Jessie built-in",
            "contract_sha256": contract_hash,
            "j1_mapping": j1,
            "terminal_contract_mismatches": terminal_check,
            "domain_separation": domain_separation,
            "native_drc_open_edges_by_power_domain": (
                {net: open_counts.get(net, 0) for net in sorted(domains)}
                if open_counts is not None
                else None
            ),
            "drc_report_status": "supplied; source-board match not verified"
            if drc_json
            else "not supplied",
            "drc_report_sha256": drc_hash,
            "static_contract_passed": not failures,
            "static_contract_failures": failures,
            "note": (
                "A passing static contract only proves terminal/net identity. "
                "It does not prove current capacity, thermal performance, transient behavior, "
                "or routing/manufacturing readiness."
            ),
        }
        if (
            contract
            and hashlib.sha256(contract.read_bytes()).hexdigest() != contract_hash
        ):
            raise RuntimeError(
                "Terminal contract changed during audit; discard the result"
            )
        if drc_json and hashlib.sha256(drc_json.read_bytes()).hexdigest() != drc_hash:
            raise RuntimeError("DRC report changed during audit; discard the result")
        if hashlib.sha256(board_path.read_bytes()).hexdigest() != board_hash:
            raise RuntimeError("Saved board changed during audit; discard the result")
        return report


class PowerTerminalCommand:
    """Command-line entry: print the terminal contract result of a saved board."""

    def __call__(self, argv: list[str] | None = None) -> int:
        """Run the audit.

        Inputs:
            argv: Command-line arguments; the process arguments when None.
        Outputs:
            Exit status 0 when every static contract check passes, otherwise 2.
        """
        parser = argparse.ArgumentParser(
            description=__doc__,
            epilog=(
                'Example --contract JSON: {"terminals": {"VCC": [["J1", "1"]], '
                '"GND": [["J1", "2"]]}, "separations": [{"name": "supply_vs_ground", '
                '"first": ["J1", "1"], "second": ["J1", "2"]}]}'
            ),
        )
        parser.add_argument("pcb", type=Path, help="live .kicad_pcb source")
        parser.add_argument(
            "--contract", type=Path, help="JSON terminal and separation contract"
        )
        parser.add_argument(
            "--drc-json",
            type=Path,
            help="optional native KiCad JSON DRC report; this script does not run DRC itself",
        )
        parser.add_argument(
            "--json", action="store_true", help="emit machine-readable output"
        )
        args = parser.parse_args(argv)

        report = PowerTerminalAudit()(args.pcb.resolve(), args.contract, args.drc_json)
        failures = report["static_contract_failures"]
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"Static power contract: {'PASS' if not failures else 'FAIL'}")
            for line in failures:
                print(f"  FAIL {line}")
            open_counts = report["native_drc_open_edges_by_power_domain"]
            if open_counts is None:
                print("Native DRC open connections: not checked (no report supplied).")
            else:
                print(
                    "Open edges in supplied DRC report (source-board match not verified):"
                )
                for net in sorted(open_counts):
                    print(f"  {net}: {open_counts[net]}")
            print(report["note"])
        return 0 if not failures else 2


main = PowerTerminalCommand()

if __name__ == "__main__":
    raise SystemExit(main())
