"""Run a pinned KiCadRoutingTools proposal on a disposable project copy."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from pcb.freeroute_preview import _fingerprints, _snapshot, _source_files
from pcb.route_optimizer import COPPER_LAYERS

VERSION = "0.23.7"
COMMIT = "83646c8b56c3571067c359cf5524167abfef4c0f"
WINDOWS_BINARY_SHA256 = "68bf6b96c54f9b18c173c7758cc73f6a9d6a661ecd93a021119246d77daeed3d"
RELEASES = {
    VERSION: (COMMIT, WINDOWS_BINARY_SHA256, "0.23.7"),
    # The published 0.23.8 source still declares the Rust crate version 0.23.7.
    "0.23.8": ("c64c0db25e6cf23afa6769ee05665c7ea22b3ff2",
               "4bc6e1ec3f6145fb75cbfd091619b6ab29b7fa62cc53cb2d2dc7ba54bdfabfc4", "0.23.7"),
}


def _release(root: Path) -> tuple[str, tuple[str, str, str]]:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if version not in RELEASES:
        raise ValueError("KiCadRoutingTools release is not in the verified allowlist")
    return version, RELEASES[version]


def strict_size_rejection(log: str, code: int, targets: tuple[str, ...]) -> dict | None:
    """Recognize the router's explicit strict-size refusal, not other failures."""
    if code != 3 or "--strict-sizes:" not in log:
        return None
    lines = [line.split(":", 1)[1].strip() for line in log.splitlines()
             if line.startswith(("JSON_SUMMARY_MIN:", "JSON_SUMMARY:"))]
    if not lines:
        return None
    try:
        summary = json.loads(lines[-1])
    except (TypeError, ValueError):
        return None
    if not isinstance(summary, dict):
        return None
    failed = summary.get("failed_single")
    failed_pairs = summary.get("failed_diff_pairs")
    if (failed is None and isinstance(failed_pairs, list)
            and all(isinstance(name, str) and name for name in failed_pairs)):
        failed = []  # KRT reports pair base names, not the individual net names.
    if not isinstance(failed, list) or any(not isinstance(name, str) or not name for name in failed):
        return None
    return {"status": "strict_size_rejected", "failed_nets": tuple(sorted(set(failed) & set(targets))),
            "reason": "The router rejected its proposal under strict size rules"}


def strict_size_nets(log: str, targets: tuple[str, ...], summary: dict,
                     net_codes: dict[int, str]) -> tuple[str, ...] | None:
    """Identify every narrowed net using the final ledger and native net codes.

    Missing, conflicting or unscoped identities make salvage unsafe. A caller
    must discard all listed nets and independently validate every other item.
    """
    headlines = re.findall(
        r"--strict-sizes:[^\r\n]*?(\d+) feature\(s\) on (\d+) net\(s\) delivered below",
        log,
    )
    if len(headlines) != 1:
        return None
    expected_count, expected_nets = map(int, headlines[0])
    if (not isinstance(summary, dict) or not isinstance(targets, tuple)
            or any(not isinstance(name, str) or not name for name in targets)
            or not isinstance(net_codes, dict)
            or any(not isinstance(name, str) or not name for name in net_codes.values())
            or len(net_codes) != len(targets) or set(net_codes.values()) != set(targets)
            or any(isinstance(code, bool) or not isinstance(code, int) or code <= 0 for code in net_codes)):
        return None
    rules = summary.get("design_rules")
    if (not isinstance(rules, dict) or rules.get("escalation_policy") != "off"
            or type(rules.get("fab_tier_escalations")) is not int
            or rules["fab_tier_escalations"] != 0
            or rules.get("fab_tier_contexts") != [] or rules.get("ceiling_lowered_classes", []) != []
            or isinstance(rules.get("count"), bool) or not isinstance(rules.get("count"), int)
            or not isinstance(rules.get("nets"), list) or not isinstance(rules.get("narrowed"), list)):
        return None
    rows = rules["narrowed"]
    if (not rows or any(not isinstance(row, dict) or isinstance(row.get("net"), bool)
            or not isinstance(row.get("net"), int) or row["net"] not in net_codes
            or isinstance(row.get("count"), bool) or not isinstance(row.get("count"), int)
            or row["count"] < 1 for row in rows)
            or any(isinstance(code, bool) or not isinstance(code, int) or code not in net_codes
                   for code in rules["nets"])
            or len(set(rules["nets"])) != len(rules["nets"])
            or set(rules["nets"]) != {row["net"] for row in rows}
            or rules["count"] != sum(row["count"] for row in rows)
            or rules["count"] != expected_count or len(rules["nets"]) != expected_nets):
        return None
    for row in rows:
        code, name = row.get("net"), row.get("net_name")
        requested, delivered = row.get("requested"), row.get("delivered")
        if (isinstance(code, bool) or not isinstance(code, int) or code < 0
                or not isinstance(row.get("kind"), str)
                or row["kind"] not in {"track_width", "via_diameter"}
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) or value < 0 for value in (requested, delivered))
                or delivered >= requested or (name is not None and name != net_codes[code])):
            return None
    return tuple(sorted({net_codes[row["net"]] for row in rows}))


def tool_fingerprint(root: Path) -> str:
    """Bind routing and retry records to the executable and its source files."""
    root = Path(root).resolve()
    required = (root / "VERSION", root / "py_router" / "route.py",
                root / "rust_router" / "grid_router.pyd")
    if not all(path.is_file() for path in required):
        raise ValueError("KiCadRoutingTools installation is incomplete")
    files = set(required)
    files.update(path for path in root.glob("*.py") if path.is_file())
    files.update(path for path in (root / "py_router").rglob("*")
                 if path.is_file() and path.suffix.lower() in {".py", ".json", ".toml", ".txt", ".yaml", ".yml"})
    requirements = root / "requirements.txt"
    if requirements.is_file():
        files.add(requirements)
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def validate_installation(root: Path) -> str:
    """Use an allowlisted release and reject modified or incomplete tool sources."""
    root = Path(root).resolve()
    fingerprint = tool_fingerprint(root)
    _version, (commit, binary_sha256, _binary_version) = _release(root)
    binary = root / "rust_router" / "grid_router.pyd"
    if hashlib.sha256(binary.read_bytes()).hexdigest() != binary_sha256:
        raise ValueError("The KiCadRoutingTools binary does not match the pinned release")
    try:
        revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=15, check=True).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("Cannot verify the KiCadRoutingTools source checkout") from error
    if revision != commit or dirty:
        raise ValueError("The KiCadRoutingTools source is not the unmodified pinned release")
    return fingerprint


def build_command(python: Path, root: Path, board: Path, output: Path,
                  targets: tuple[str, ...], *,
                  layers: tuple[str, ...] = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
                  grid_mm: float | None = None, quick: bool = True,
                  differential: bool = False, summary: Path | None = None) -> list[str]:
    """Limit the proposal to explicit nets without relaxing board rules."""
    if (not isinstance(targets, tuple) or not targets
            or any(not isinstance(name, str) or not name or name.startswith("-")
                   or any(c in name for c in "*?[]\x00\r\n") for name in targets)
            or len(set(targets)) != len(targets)):
        raise ValueError("Router targets must be distinct, literal net names")
    if Path(board).resolve() == Path(output).resolve():
        raise ValueError("The router output must differ from its input")
    if summary is not None and Path(summary).resolve() in {Path(board).resolve(), Path(output).resolve()}:
        raise ValueError("The router summary must not overwrite a board")
    if (not isinstance(layers, tuple) or not 2 <= len(layers) <= len(COPPER_LAYERS)
            or any(name not in COPPER_LAYERS for name in layers)
            or len(set(layers)) != len(layers)):
        raise ValueError("The router requires distinct, supported copper layers")
    if (grid_mm is not None and (isinstance(grid_mm, bool) or not isinstance(grid_mm, (int, float))
                                 or not math.isfinite(grid_mm) or grid_mm <= 0)):
        raise ValueError("The router grid must be a positive finite number of millimetres")
    if not isinstance(quick, bool) or not isinstance(differential, bool):
        raise ValueError("The quick-search and differential options must be boolean")
    command = [str(python), "-u", "-B", str(Path(__file__).with_name("krt_entry.py")),
               str(board), str(output), "--nets", *targets,
               "--layers", *layers, "--layer-costs", *("1" for _ in layers),
               "--direction-preference-cost", "0", "--via-cost", "100",
               "--turn-cost", "10000", "--same-net-pad-clearance", "0.01", "--no-fix-drc-settings",
               "--escalation", "off", "--strict-sizes", "--keep-input-copper",
               "--no-stub-layer-swap", "--max-ripup", "0"]
    if differential:
        command.append("--no-gnd-vias")
    if quick:
        command.extend(("--quick", "1"))
    if grid_mm is not None:
        command.extend(("--grid-step", format(grid_mm, "g")))
    if summary is not None:
        command.extend(("--json-out", str(summary)))
    return command


def _python(root: Path, requested: str) -> Path:
    if not isinstance(requested, str) or not requested:
        raise ValueError("The router Python must be an executable path or auto")
    candidates = []
    if requested != "auto":
        candidates.append(requested)
    else:
        if os.environ.get("LOCALAPPDATA"):
            candidates.append(str(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Python" / "Python311" / "python.exe"))
        candidates.extend((sys.executable, shutil.which("python") or ""))
    _version, (_commit, _binary_sha256, binary_version) = _release(root)
    probe = ("import sys,numpy,scipy,shapely;sys.path.insert(0,sys.argv[1]);"
             "import grid_router;assert grid_router.__version__ == sys.argv[2]")
    for candidate in dict.fromkeys(candidates):
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            result = subprocess.run([candidate, "-B", "-c", probe, str(root / "rust_router"), binary_version],
                                    capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return Path(candidate).resolve()
        if "Application Control policy has blocked" in str(result.stderr):
            raise RuntimeError("Windows Application Control blocked a native Python dependency while "
                               "checking KiCadRoutingTools; use an available routing backend")
    raise ValueError("KiCadRoutingTools needs a Python with numpy, scipy and shapely; use --router-python")


def _stop_tree(process: subprocess.Popen) -> None:
    """Stop only the router we launched and any child processes it created."""
    if process.poll() is None and os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, check=False, timeout=15,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def router_net_codes(executable: Path, root: Path, board: Path, env: dict[str, str]) -> dict[int, str]:
    """Read the pinned router's own net IDs; KiCad 10 files may omit numeric IDs."""
    script = (
        "from pathlib import Path;import json,sys;sys.path.insert(0,sys.argv[1]);"
        "from kicad_parser import extract_nets;"
        "nets,_=extract_nets(Path(sys.argv[2]).read_text(encoding='utf-8-sig'));"
        "print(json.dumps({str(code):net.name for code,net in nets.items()}))"
    )
    try:
        result = subprocess.run([str(executable), "-B", "-c", script, str(root / "py_router"), str(board)],
                                env=env, capture_output=True, text=True, timeout=30, check=False,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        mapping = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError("The router's input-net mapping could not be verified") from None
    if (not isinstance(mapping, dict) or any(not isinstance(key, str) or not key.isdecimal()
            or str(int(key)) != key or int(key) <= 0 or not isinstance(name, str) or not name
            for key, name in mapping.items())):
        raise ValueError("The router's input-net mapping is malformed")
    return {int(code): name for code, name in mapping.items()}


def preview(project: Path, root: Path, python: str, targets: tuple[str, ...], timeout: int, *,
            grid_mm: float | None = None, quick: bool = True, differential: bool = False,
            salvage_strict: bool = False, reserve_references: bool = False,
            ground_contact_margin_mm: float = .5) -> dict:
    """Generate copper only; the caller must filter and validate the result."""
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise ValueError("The router timeout must be a positive integer")
    project, root = Path(project).resolve(), Path(root).resolve()
    if (not isinstance(differential, bool) or not isinstance(salvage_strict, bool)
            or not isinstance(reserve_references, bool)):
        raise ValueError("Differential and salvage options must be boolean")
    if differential and salvage_strict:
        raise ValueError("Strict-size salvage is not supported for paired routes")
    if differential and not (root / "py_router" / "route_diff.py").is_file():
        raise ValueError("The pinned KRT differential router is unavailable")
    source = project / "v4.1.kicad_pcb"
    tool = validate_installation(root)
    executable = _python(root, python)
    with tempfile.TemporaryDirectory(prefix="autopcb-krt-") as directory:
        import pcbnew

        trial = Path(directory)
        inputs = _snapshot(source, trial)
        board, output, logfile = trial / source.name, trial / "candidate.kicad_pcb", trial / "router.log"
        summary_path = trial / "router-summary.json"
        native = pcbnew.LoadBoard(str(board))
        if native is None:
            raise RuntimeError("KiCad could not load the private router input")
        layers = tuple(str(native.GetLayerName(layer)) for layer in native.GetEnabledLayers().CuStack())
        original_zones, temporary_zones, reservations = {}, frozenset(), {}
        if reserve_references and not differential and "GND" not in targets:
            from pcb.net_routing.export_reservations import reserved_native_input

            original_zones, temporary_zones, reservations = reserved_native_input(
                board, ground_contact_margin_mm=ground_contact_margin_mm)
        command = build_command(executable, root, board, output, targets, layers=layers,
                                grid_mm=grid_mm, quick=quick, differential=differential,
                                summary=summary_path if salvage_strict else None)
        env = os.environ.copy()
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        env["AUTOPCB_KRT_ROOT"] = str(root)
        env["AUTOPCB_KRT_DIFFERENTIAL"] = "1" if differential else "0"
        env["KICAD_RIP_PREEXISTING"] = "0"
        env["KICAD_FINALIZE_RIP"] = "0"
        with logfile.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _stop_tree(process)
                if _fingerprints(_source_files(source), project) != inputs:
                    raise RuntimeError("The router input changed during the private trial") from None
                if tool_fingerprint(root) != tool:
                    raise RuntimeError("The router installation changed during the private trial") from None
                return {"status": "search_timeout", "tool": tool,
                        "reason": f"KiCadRoutingTools reached its {timeout} second search limit; "
                                  "the partial candidate was discarded."}
            except BaseException:
                _stop_tree(process)
                raise
        log = logfile.read_text(encoding="utf-8", errors="replace")
        rejection = strict_size_rejection(log, code, targets)
        if (code or not output.is_file()) and rejection is None:
            tail = "\n".join(log.splitlines()[-8:])
            raise RuntimeError(f"KiCadRoutingTools did not produce a candidate: {tail}")
        if _fingerprints(_source_files(source), project) != inputs:
            raise RuntimeError("The router input changed during the private trial")
        if tool_fingerprint(root) != tool:
            raise RuntimeError("The router installation changed during the private trial")
        if output.is_file() and temporary_zones:
            from pcb.net_routing.export_reservations import strip_native_reservations

            strip_native_reservations(output, original_zones, temporary_zones)
        if rejection is not None:
            narrowed = None
            if salvage_strict and output.is_file() and summary_path.is_file():
                try:
                    final_summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    nets = [native.FindNet(name) for name in targets]
                    if all(net is not None for net in nets):
                        codes = {code: name for code, name in router_net_codes(executable, root, board, env).items()
                                 if name in targets}
                        narrowed = strict_size_nets(log, targets, final_summary, codes)
                except (OSError, ValueError, TypeError):
                    pass  # Missing or malformed final evidence discards the whole candidate.
                if (_fingerprints(_source_files(source), project) != inputs or tool_fingerprint(root) != tool):
                    raise RuntimeError("The router or its input changed while checking its net mapping")
            if narrowed:
                return {**rejection, "tool": tool, "narrowed_nets": narrowed,
                        "salvage_board": output.read_bytes(), "search_reservations": reservations}
            return {**rejection, "tool": tool}
        return {"status": "unapproved_trial_only", "board": output.read_bytes(), "tool": tool,
                "search_reservations": reservations}
