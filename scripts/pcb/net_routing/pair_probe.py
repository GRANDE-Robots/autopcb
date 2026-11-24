"""pair_probe.py: try paired routing on a disposable board and report unverified results.

This diagnostic never retains or publishes copper. Native DRC and sampled
reference geometry do not establish differential impedance or electrical safety.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from core.project import native_path

import hashlib
import math
import os
import subprocess
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from core.toolchain import kicad_cli

import pcbnew

from core.kicad_campaign import CampaignPlanner
from core.pcb_io import net_classes
from pcb.freeroute_delta import (
    additive_delta,
    align_proposal_to_existing_vias,
    changes,
    native_item_nets,
    read_copper,
    unconnected_by_net,
)
from pcb.freeroute_preview import (
    _routing_only_session_file,
    FASTROUTE_014_SHA256,
    _alias_deferred_default,
    _defer_selected_nets,
    _dsn_scope,
    _prepare_router_dsn,
)
from pcb.net_routing.fast import RoutingRoundtrip
from pcb.net_routing.native import Snapshot
from pcb.net_routing.protection import ProtectedState
from pcb.net_routing.reference import (
    high_speed_nets,
    reference_conflicts,
    sample_reference,
)
from pcb.net_routing.style import audit_style, normalize_segment_identities
from pcb.route_validation import _drc, _segment_identities
from core.toolchain import RouterCache


@dataclass(frozen=True)
class PairSpec:
    """One discovered differential pair.

    Inputs:
        positive: Name of the positive net.
        negative: Name of the negative net.
        netclass: Net class both nets belong to.
        gap_mm: Configured pair gap of the class in mm.
    """

    positive: str
    negative: str
    netclass: str
    gap_mm: float


class PairDiscovery:
    """Finds complete differential pairs among the critical nets and selects a scope of them."""

    @staticmethod
    def select_pair_scope(
        pairs: tuple[PairSpec, ...], pair_index: int | None
    ) -> tuple[tuple[PairSpec, ...], set[str]]:
        """Select complete discovered pairs; indices are one-based for the CLI.

        Inputs:
            pairs: A nonempty tuple of discovered pairs.
            pair_index: One-based index of the single pair to test, or None for all.
        Outputs:
            The chosen pairs and the set of their net names. Raises ValueError for an
            empty or malformed inventory and for an index outside the inventory.
        """
        if (
            not isinstance(pairs, tuple)
            or not pairs
            or any(not isinstance(item, PairSpec) for item in pairs)
        ):
            raise ValueError("A nonempty inventory of complete pairs is required")
        if pair_index is not None and (
            isinstance(pair_index, bool)
            or not isinstance(pair_index, int)
            or not 1 <= pair_index <= len(pairs)
        ):
            raise ValueError(f"Pair index must be between 1 and {len(pairs)}")
        chosen = pairs if pair_index is None else (pairs[pair_index - 1],)
        return chosen, {
            name for pair in chosen for name in (pair.positive, pair.negative)
        }

    @staticmethod
    def discover_pairs(
        critical: set[str], endpoints: dict, classes: dict, gaps: dict
    ) -> tuple[PairSpec, ...]:
        """Require matching names, shared component endpoints and explicit polarity.

        Inputs:
            critical: The nonempty set of high-speed net names.
            endpoints: For each net, the pin functions it reaches on each component.
            classes: Net class of each net.
            gaps: Pair gap in mm of each net class.
        Outputs:
            One PairSpec per pair, ordered by positive net name. Raises ValueError when
            a mate is missing or unsupported, endpoint or polarity evidence is missing,
            the classes differ or the gap is missing or not positive and finite.
        """
        if not critical or any(
            not isinstance(name, str)
            or not name
            or any(char.isspace() or char in '#"' for char in name)
            for name in critical
        ):
            raise ValueError("An explicit nonempty set of critical nets is required")
        result = []
        seen = set()
        for name in sorted(critical):
            if name in seen:
                continue
            suffix = (
                "_DP" if name.endswith("_DP") else "_P" if name.endswith("_P") else None
            )
            if suffix is None:
                continue
            mate = name[: -len(suffix)] + ("_DM" if suffix == "_DP" else "_N")
            if mate not in critical:
                raise ValueError(f"Differential mate is missing for {name}")
            if name not in endpoints or mate not in endpoints:
                raise ValueError(f"Pair endpoint evidence is missing for {name}")
            shared = set(endpoints[name]) & set(endpoints[mate])
            if len(shared) < 2:
                raise ValueError(f"Pair endpoint evidence is insufficient for {name}")
            if not any(
                any("+" in pin for pin in endpoints[name][ref])
                and any("-" in pin for pin in endpoints[mate][ref])
                for ref in shared
            ):
                raise ValueError(f"Pair polarity evidence is missing for {name}")
            netclass = classes.get(name)
            if (
                not isinstance(netclass, str)
                or not netclass
                or classes.get(mate) != netclass
            ):
                raise ValueError(f"Pair class differs or is missing for {name}")
            gap = gaps.get(netclass)
            if (
                isinstance(gap, bool)
                or not isinstance(gap, (float, int))
                or not math.isfinite(gap)
                or gap <= 0
            ):
                raise ValueError(f"Pair gap is missing or invalid for {name}")
            result.append(PairSpec(name, mate, netclass, float(gap)))
            seen.update((name, mate))
        if seen != critical:
            raise ValueError("Differential mate is missing or has an unsupported name")
        return tuple(result)


class BoardPairs:
    """Discovers the differential pairs on a loaded board.

    The module-level instance _pairs is also used by the DRC cleanup command.
    """

    def __call__(
        self, board, project_bytes: bytes
    ) -> tuple[tuple[PairSpec, ...], set[str]]:
        """Collect endpoints, classes and gaps from a board and discover its pairs.

        Inputs:
            board: The loaded KiCad board.
            project_bytes: Project file bytes that define the high-speed nets.
        Outputs:
            The discovered pairs and the set of high-speed net names.
        """
        names = {str(name) for name in board.GetNetsByName().keys()}
        critical = high_speed_nets(project_bytes, names)
        endpoints = defaultdict(lambda: defaultdict(set))
        for footprint in board.GetFootprints():
            for pad in footprint.Pads():
                name = str(pad.GetNetname())
                if name in critical:
                    endpoints[name][str(footprint.GetReference())].add(
                        str(pad.GetPinFunction())
                    )
        classes = {
            name: str(board.FindNet(name).GetNetClassName()) for name in critical
        }
        definitions = net_classes(board)
        gaps = {
            name: definition.GetDiffPairGap() / 1e6
            for name, definition in definitions.items()
        }
        return PairDiscovery.discover_pairs(
            critical, endpoints, classes, gaps
        ), critical


_pairs = BoardPairs()


class PairScope:
    """Chooses which DSN net classes the router must ignore so only pair nets route."""

    @staticmethod
    def pair_only_classes(design: Path, critical: set[str]) -> tuple[str, ...]:
        """Ignore non-pair classes; reject mixed classes rather than routing them.

        Inputs:
            design: Exported DSN file.
            critical: High-speed net names.
        Outputs:
            The net classes to ignore, with the deferred default class aliased. Raises
            ValueError when a class has no name, a high-speed class includes unrelated
            nets or a high-speed net is absent from the export.
        """
        _, network = _dsn_scope(design.read_text(encoding="utf-8"), "network")
        ignored, covered = [], set()
        for item in network.children("class"):
            atoms = item.atoms()
            if len(atoms) < 2:
                raise ValueError("A DSN net class has no name")
            members = {atom.value for atom in atoms[2:]}
            selected = members & critical
            if selected and members - critical:
                raise ValueError("A high-speed net class includes unrelated nets")
            if selected:
                covered.update(selected)
            else:
                ignored.append(atoms[1].value)
        if covered != critical:
            raise ValueError("Not every high-speed net appears in the exported DSN")
        return _alias_deferred_default(design, tuple(ignored))

    @staticmethod
    def pair_scope_classes(
        design: Path, critical: set[str], active: set[str]
    ) -> tuple[str, ...]:
        """Defer other high-speed pairs without changing their class rules.

        Inputs:
            design: Exported DSN file; the other high-speed nets are split out of it.
            critical: All high-speed net names.
            active: The nonempty subset of nets to route.
        Outputs:
            The distinct net classes the router must ignore. Raises ValueError when the
            active scope is empty or not a subset of the high-speed nets.
        """
        if not active or not active <= critical:
            raise ValueError(
                "The active pair scope must be a nonempty subset of high-speed nets"
            )
        ignored = PairScope.pair_only_classes(design, critical)
        split = _defer_selected_nets(design, tuple(sorted(critical - active)), ignored)
        return tuple(dict.fromkeys((*ignored, *split)))


class PairTrialReport:
    """Summarizes a paired-route trial without certifying any pair."""

    @staticmethod
    def style_delta(
        before, after, before_bytes: bytes, after_bytes: bytes
    ) -> dict[str, int]:
        """Count routing-style issues that the trial introduced.

        Inputs:
            before: Board loaded before the trial.
            after: Board loaded after the trial.
            before_bytes: Saved bytes of the first board.
            after_bytes: Saved bytes of the second board.
        Outputs:
            For each style issue category, the number of new issues; categories with none
            are omitted.
        """
        old = normalize_segment_identities(
            audit_style(before), _segment_identities(before_bytes)
        )
        new = normalize_segment_identities(
            audit_style(after), _segment_identities(after_bytes)
        )
        fields = (
            "non_octilinear",
            "arcs",
            "invalid_bends",
            "redundant_joints",
            "staircases",
            "locked_items",
            "grouped_items",
            "ambiguous",
        )
        return {
            name: count
            for name in fields
            if (count := len(set(getattr(new, name)) - set(getattr(old, name))))
        }

    @staticmethod
    def summarize_pairs(
        pairs: tuple[PairSpec, ...],
        before: dict[str, int],
        after: dict[str, int],
        reference_tracks,
    ) -> tuple[dict, ...]:
        """Attribute trial opens and incomplete reference samples without certifying a pair.

        Inputs:
            pairs: Pairs under test.
            before: Open connections per net before the trial.
            after: Open connections per net after the trial.
            reference_tracks: Sampled reference results for the trial board.
        Outputs:
            One record per pair with its fields, saved and trial open counts and the
            number of trial reference segments with missing samples.
        """
        missing = Counter(
            track.net
            for track in reference_tracks
            if track.missing_samples or not track.continuous_polygon
        )
        return tuple(
            {
                **asdict(pair),
                "saved_opens": before.get(pair.positive, 0)
                + before.get(pair.negative, 0),
                "trial_opens": after.get(pair.positive, 0)
                + after.get(pair.negative, 0),
                "trial_missing_reference_segments": missing[pair.positive]
                + missing[pair.negative],
            }
            for pair in pairs
        )

    @staticmethod
    def introduced_via_pad_overlaps(before, after) -> tuple:
        """Report new unqualified via drills touching copper pads in a trial.

        Inputs:
            before: Board before the trial.
            after: Board after the trial.
        Outputs:
            The sorted overlaps present after the trial and absent before it.
        """
        from pcb.net_routing.via_guard import via_pad_overlaps

        return tuple(
            sorted(set(via_pad_overlaps(after)) - set(via_pad_overlaps(before)))
        )

    @staticmethod
    def reference_blocker_summary(tracks, conflicts) -> dict[str, int]:
        """Count distinct nearby copper items, not proven causes of missing reference.

        Inputs:
            tracks: Sampled reference results.
            conflicts: Copper items near the sampled reference layers.
        Outputs:
            For each blocking net (or <unassigned>), the number of distinct nearby items
            on the reference layer of a high-speed item with missing samples.
        """
        missing = {
            item.item_id: item.reference_layer
            for item in tracks
            if item.missing_samples or not item.continuous_polygon
        }
        items = {
            (item.blocking_net or "<unassigned>", item.blocking_item_id)
            for item in conflicts
            if missing.get(item.high_speed_item_id) == item.reference_layer
        }
        return dict(sorted(Counter(net for net, _ in items).items()))


class PairProbe:
    """Runs an isolated paired-route experiment and reports checks, never a board."""

    @staticmethod
    def probe(
        project: Path,
        *,
        seconds: int = 45,
        progress=None,
        engine: str = "fastroute",
        router_grid_mm: float | None = None,
        pair_index: int | None = None,
    ) -> dict:
        """Run an isolated paired-route experiment and return checks, never a board.

        Inputs:
            project: Project directory; its saved board is copied and never changed.
            seconds: Router time budget in seconds, from 15 to 3600.
            progress: Optional callable that receives stage events.
            engine: fastroute or krt.
            router_grid_mm: Optional KRT search grid in mm; only valid for krt.
            pair_index: One-based index of a single pair to test, or None for all.
        Outputs:
            A diagnostic dictionary: status (diagnostic_only or no_candidate), opens,
            errors and warnings before and after, added copper, new style issues,
            reference findings and limitations, with every verification flag false.
            Raises ValueError for invalid options and RuntimeError when the engine,
            KiCad CLI or trial board is unavailable, changes during the trial or cannot
            attribute every open connection to a net.
        """
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, int)
            or not 15 <= seconds <= 3600
        ):
            raise ValueError("Pair-preview seconds must be an integer from 15 to 3600")
        if engine not in {"fastroute", "krt"}:
            raise ValueError("Unsupported paired-route diagnostic engine")
        if router_grid_mm is not None and (
            engine != "krt"
            or isinstance(router_grid_mm, bool)
            or not isinstance(router_grid_mm, (int, float))
            or not math.isfinite(router_grid_mm)
            or router_grid_mm <= 0
        ):
            raise ValueError("A positive paired-router grid is supported only for KRT")

        def stage(message: str) -> None:
            """Report a progress stage when a progress callable was given."""
            if progress:
                progress({"event": "stage", "message": message})

        project = project.resolve()
        exe, krt_root = RouterCache.fastroute(), RouterCache.krt()
        if engine == "fastroute":
            if (
                not exe.is_file()
                or hashlib.sha256(exe.read_bytes()).hexdigest() != FASTROUTE_014_SHA256
            ):
                raise RuntimeError(
                    "The verified fastroute binary is unavailable or changed"
                )
        else:
            from pcb.net_routing import krt_adapter

            krt_tool = krt_adapter.KrtInstallation.validate_installation(krt_root)
        cli = kicad_cli()
        if not cli.is_file():
            raise RuntimeError("KiCad CLI is required for the pair preview")

        with Snapshot(native_path(project, ".kicad_pcb")) as snapshot:
            stage("Checking the saved board and identifying paired endpoints")
            schematic = (native_path(snapshot.directory, ".kicad_sch")).read_bytes()
            protected = ProtectedState.capture(snapshot.original, schematic)
            baseline = _drc(
                cli,
                snapshot.board_path,
                snapshot.directory / "pair-baseline.json",
                save_refill=True,
                schematic_parity=True,
            )
            baseline_bytes = snapshot.board_path.read_bytes()
            if protected.differences(baseline_bytes, schematic):
                raise RuntimeError("The baseline refill changed protected design items")
            board = pcbnew.LoadBoard(str(snapshot.board_path))
            if board is None:
                raise RuntimeError("KiCad could not read the saved PCB copy")
            all_pairs, all_critical = _pairs(
                board, (native_path(snapshot.directory, ".kicad_pro")).read_bytes()
            )
            pairs, critical = PairDiscovery.select_pair_scope(all_pairs, pair_index)
            before_nets = unconnected_by_net(baseline, native_item_nets(board))
            if before_nets.get("<unknown>", 0):
                raise RuntimeError(
                    "Cannot attribute every baseline open connection to a net"
                )
            before_reference = sample_reference(board, all_critical)
            original = read_copper(snapshot.board_path)
            rejection = None
            if engine == "fastroute":
                design = snapshot.directory / "candidate.dsn"
                _prepare_router_dsn(board, design, optimizer=False)
                ignored = PairScope.pair_scope_classes(design, all_critical, critical)
                pairfile = snapshot.directory / "pairs.txt"
                pairfile.write_text(
                    "".join(
                        f"pair {p.positive} {p.negative} gap={p.gap_mm} skew=0\n"
                        for p in pairs
                    ),
                    encoding="utf-8",
                )
                stage("Measuring the no-routing export/import on a separate copy")
                comparison = RoutingRoundtrip.roundtrip(
                    snapshot.board_path,
                    snapshot.directory,
                    exe,
                    "auto",
                    min(seconds, 120),
                    "fastroute",
                )
                home = snapshot.directory / "pair-router-home"
                home.mkdir()
                env = os.environ.copy()
                for name in (
                    "APPDATA",
                    "LOCALAPPDATA",
                    "XDG_CONFIG_HOME",
                    "XDG_DATA_HOME",
                ):
                    directory = home / name.lower()
                    directory.mkdir()
                    env[name] = str(directory)
                session = snapshot.directory / "paired.ses"
                command = [
                    str(exe),
                    "-de",
                    str(design),
                    "-do",
                    str(session),
                    "-mp",
                    "1",
                    f"--max-time={max(1, seconds - 10)}",
                    "--multi-start=1",
                    "--router.autorouter.max_threads=1",
                    "--router.optimizer.enabled=false",
                    "--router.fanout.enabled=false",
                    "--router.automatic_neckdown=false",
                    "--no-neckdown-classes="
                    + ",".join(sorted({pair.netclass for pair in pairs})),
                    "--router.autorouter.ignore_net_classes=" + ",".join(ignored),
                    "--pairs=" + str(pairfile),
                ]
                stage("Trying paired routing on the private board")
                with (snapshot.directory / "paired.log").open(
                    "w", encoding="utf-8"
                ) as log:
                    try:
                        result = subprocess.run(
                            command,
                            cwd=snapshot.directory,
                            env=env,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            timeout=seconds,
                            check=False,
                            creationflags=subprocess.CREATE_NO_WINDOW
                            if os.name == "nt"
                            else 0,
                        )
                    except subprocess.TimeoutExpired as error:
                        raise RuntimeError(
                            "The paired-route trial exceeded its time budget"
                        ) from error
                if result.returncode or not session.is_file():
                    tail = "\n".join(
                        (snapshot.directory / "paired.log")
                        .read_text(encoding="utf-8", errors="replace")
                        .splitlines()[-6:]
                    )
                    raise RuntimeError(
                        f"Paired routing did not produce a session: {tail}"
                    )
                if not pcbnew.ImportSpecctraSES(
                    board, str(_routing_only_session_file(session))
                ):
                    raise RuntimeError("KiCad could not import the paired-route trial")
                routed = snapshot.directory / "paired-routed.kicad_pcb"
                if not pcbnew.SaveBoard(str(routed), board):
                    raise RuntimeError(
                        "KiCad could not save the temporary paired-route trial"
                    )
                roundtrip = read_copper(comparison)
                proposed = read_copper(routed)
            else:
                stage("Trying KRT paired routing on the private board")
                raw = krt_adapter.KrtPreview.preview(
                    snapshot.directory,
                    krt_root,
                    "auto",
                    tuple(sorted(critical)),
                    seconds,
                    grid_mm=router_grid_mm,
                    differential=True,
                )
                if raw.get("tool") != krt_tool:
                    raise RuntimeError(
                        "The paired-route engine changed during the trial"
                    )
                roundtrip = original
                if raw.get("status") in {"strict_size_rejected", "search_timeout"}:
                    rejection = raw["reason"]
                    proposed = original
                else:
                    routed = snapshot.directory / "paired-routed.kicad_pcb"
                    routed.write_bytes(raw["board"])
                    proposed = read_copper(routed)
            excluded = {
                item.net for item in (*original, *roundtrip, *proposed)
            } - critical
            delta = additive_delta(original, roundtrip, proposed, excluded=excluded)
            delta, alignment = align_proposal_to_existing_vias(delta, original)
            modifications = changes(delta)
            snapshot.board_path.write_bytes(baseline_bytes)
            if modifications:
                snapshot.board_path.write_bytes(
                    CampaignPlanner(snapshot.directory).plan("pcb", modifications).after
                )
                stage(
                    "Checking extracted copper with native KiCad DRC and reference sampling"
                )
                candidate = _drc(
                    cli,
                    snapshot.board_path,
                    snapshot.directory / "pair-candidate.json",
                    save_refill=True,
                    schematic_parity=True,
                )
            else:
                stage("No extractable paired-route proposal was found")
                candidate = baseline
            candidate_bytes = snapshot.board_path.read_bytes()
            protected_changes = protected.differences(candidate_bytes, schematic)
            candidate_board = pcbnew.LoadBoard(str(snapshot.board_path))
            if candidate_board is None:
                raise RuntimeError("KiCad could not read the paired candidate")
            after_nets = unconnected_by_net(
                candidate, native_item_nets(candidate_board)
            )
            if after_nets.get("<unknown>", 0):
                raise RuntimeError(
                    "Cannot attribute every candidate open connection to a net"
                )
            baseline_path = snapshot.directory / "pair-original.kicad_pcb"
            baseline_path.write_bytes(baseline_bytes)
            baseline_board = pcbnew.LoadBoard(str(baseline_path))
            if baseline_board is None:
                raise RuntimeError("KiCad could not reopen the paired baseline")
            style = PairTrialReport.style_delta(
                baseline_board, candidate_board, baseline_bytes, candidate_bytes
            )
            via_pad = PairTrialReport.introduced_via_pad_overlaps(
                baseline_board, candidate_board
            )
            after_reference = sample_reference(candidate_board, all_critical)
            blockers = (
                PairTrialReport.reference_blocker_summary(
                    after_reference.tracks,
                    reference_conflicts(candidate_board, all_critical),
                )
                if modifications
                else {}
            )
            counts = Counter(
                (item.get("severity"), item.get("type"))
                for item in candidate["violations"]
            )
            saved_counts = Counter(
                (item.get("severity"), item.get("type"))
                for item in baseline["violations"]
            )
            snapshot.check()
            if (
                engine == "fastroute"
                and hashlib.sha256(exe.read_bytes()).hexdigest() != FASTROUTE_014_SHA256
            ):
                raise RuntimeError("The paired-route engine changed during the trial")
            if (
                engine == "krt"
                and krt_adapter.KrtInstallation.validate_installation(krt_root)
                != krt_tool
            ):
                raise RuntimeError("The paired-route engine changed during the trial")
            return {
                "status": "diagnostic_only" if modifications else "no_candidate",
                "engine": engine,
                "rejection": rejection,
                "saved": False,
                "electrically_verified": False,
                "pair_geometry_verified": False,
                "impedance_verified": False,
                "pairs": list(
                    PairTrialReport.summarize_pairs(
                        pairs, before_nets, after_nets, after_reference.tracks
                    )
                ),
                "saved_opens": len(baseline["unconnected_items"]),
                "candidate_opens": len(candidate["unconnected_items"]),
                "saved_critical_opens": sum(
                    before_nets.get(name, 0) for name in critical
                ),
                "candidate_critical_opens": sum(
                    after_nets.get(name, 0) for name in critical
                ),
                "saved_other_errors": sum(
                    count
                    for (severity, _), count in saved_counts.items()
                    if severity == "error"
                ),
                "saved_warnings": sum(
                    count
                    for (severity, _), count in saved_counts.items()
                    if severity == "warning"
                ),
                "other_errors": sum(
                    count
                    for (severity, _), count in counts.items()
                    if severity == "error"
                ),
                "warnings": sum(
                    count
                    for (severity, _), count in counts.items()
                    if severity == "warning"
                ),
                "added_traces": len(delta.traces),
                "added_vias": len(delta.vias),
                "skipped_non_octilinear": delta.unsupported,
                "aligned_nets": sorted(alignment),
                "new_style_issues": style,
                "protected_changes": list(protected_changes),
                "new_via_pad_overlaps": [asdict(item) for item in via_pad],
                "reference_before_missing": sum(
                    bool(item.missing_samples) or not item.continuous_polygon
                    for item in before_reference.tracks
                ),
                "reference_after_missing": sum(
                    bool(item.missing_samples) or not item.continuous_polygon
                    for item in after_reference.tracks
                ),
                "reference_unsupported": len(after_reference.unsupported_items),
                "nearby_reference_obstacles": blockers,
                "limitations": ([rejection] if rejection else [])
                + [
                    "Configured gaps are unverified targets; skew tuning was disabled.",
                    "Pair spacing, skew, reference continuity and electrical limits are not certified.",
                    "The temporary candidate was discarded; no PCB design was changed.",
                ],
            }
