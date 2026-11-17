"""Source-bound arithmetic for radial branch-current and voltage-drop models.

The source fingerprint only establishes which file was cited; it does not
verify the values, their context, or their relationship to actual copper.
This calculation cannot qualify a route. Meshed networks need another model.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Evidence:
    source: str
    sha256: str
    location: str
    status: str


@dataclass(frozen=True, slots=True)
class Quantity:
    value: float
    unit: str
    evidence: Evidence


@dataclass(frozen=True, slots=True)
class Branch:
    identifier: str
    parent: str
    child: str
    kind: str
    capacity: Quantity
    resistance: Quantity


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    loads: tuple[tuple[str, Quantity], ...]
    max_drop: Quantity


@dataclass(frozen=True, slots=True)
class ScenarioAssessment:
    name: str
    branch_currents_a: dict[str, float]
    load_drops_v: dict[str, float]


@dataclass(frozen=True, slots=True)
class Assessment:
    status: str
    scenarios: tuple[ScenarioAssessment, ...]
    issues: tuple[str, ...]
    electrically_verified: bool = False


def _quantity(item: Quantity, unit: str, label: str) -> None:
    if (
        not isinstance(item, Quantity)
        or item.unit != unit
        or isinstance(item.value, bool)
        or not isinstance(item.value, (int, float))
        or not math.isfinite(item.value)
        or item.value < 0
        or not isinstance(item.evidence, Evidence)
    ):
        raise ValueError(f"Invalid {label}; expected a finite nonnegative {unit} quantity")


def _evidence_issue(project: Path, evidence: Evidence) -> str | None:
    if evidence.status not in {"verified", "design_target"}:
        return f"{evidence.status or 'unknown'} evidence is not sufficient"
    if not evidence.source or not evidence.location or len(evidence.sha256) != 64:
        return "Evidence needs a source, source hash and specific location"
    try:
        int(evidence.sha256, 16)
    except ValueError:
        return "Evidence hash is not SHA-256"
    root = project.resolve()
    source = (root / evidence.source).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        return f"Evidence source is missing or outside the project: {evidence.source}"
    with source.open("rb") as handle:
        actual = hashlib.file_digest(handle, "sha256").hexdigest()
    if actual != evidence.sha256.lower():
        return f"Evidence source changed: {evidence.source}"
    return None


def assess_tree(
    project: Path, root: str, branches: tuple[Branch, ...], scenarios: tuple[Scenario, ...]
) -> Assessment:
    """Sum simultaneous loads on shared branches and assess each root-to-load path."""
    if not isinstance(root, str) or not root or not branches or not scenarios:
        raise ValueError("A root, branches and scenarios are required")
    children: dict[str, list[Branch]] = {}
    parent: dict[str, Branch] = {}
    identifiers = set()
    quantities: list[tuple[str, Quantity, str, bool]] = []
    for branch in branches:
        if (
            not isinstance(branch, Branch)
            or not branch.identifier
            or branch.identifier in identifiers
            or not branch.parent
            or not branch.child
            or branch.parent == branch.child
            or branch.child == root
            or branch.child in parent
            or branch.kind not in {"track", "via"}
        ):
            raise ValueError("Branches must form a uniquely identified directed tree")
        identifiers.add(branch.identifier)
        parent[branch.child] = branch
        children.setdefault(branch.parent, []).append(branch)
        quantities.extend(
            ((f"{branch.identifier} capacity", branch.capacity, "A", True),
             (f"{branch.identifier} resistance", branch.resistance, "ohm", True))
        )

    order: list[Branch] = []
    nodes = {root}
    pending = [root]
    while pending:
        node = pending.pop()
        for branch in children.get(node, ()):
            if branch.child in nodes:
                raise ValueError("Branches must form a directed tree without cycles")
            nodes.add(branch.child)
            order.append(branch)
            pending.append(branch.child)
    if len(order) != len(branches):
        raise ValueError("Branches must form a connected directed tree")

    scenario_names = set()
    for scenario in scenarios:
        if not isinstance(scenario, Scenario) or not scenario.name or scenario.name in scenario_names or not scenario.loads:
            raise ValueError("Each scenario needs a distinct name and loads")
        scenario_names.add(scenario.name)
        quantities.append((f"{scenario.name} maximum drop", scenario.max_drop, "V", False))
        load_nodes = set()
        for node, load in scenario.loads:
            if node not in nodes or node in load_nodes:
                raise ValueError(f"Scenario {scenario.name} has a missing or duplicate load node")
            load_nodes.add(node)
            quantities.append((f"{scenario.name} load {node}", load, "A", False))

    evidence_results: dict[Evidence, str | None] = {}
    rating_issues = set()
    for label, quantity, unit, physical_rating in quantities:
        _quantity(quantity, unit, label)
        if quantity.evidence not in evidence_results:
            evidence_results[quantity.evidence] = _evidence_issue(project, quantity.evidence)
        if physical_rating and quantity.evidence.status != "verified":
            rating_issues.add(f"{label}: physical rating requires verified evidence")
    problems = tuple(sorted(rating_issues | {issue for issue in evidence_results.values() if issue}))
    if problems:
        return Assessment("blocked", (), problems)

    reports = []
    issues = []
    for scenario in scenarios:
        demand = {node: 0.0 for node in nodes}
        for node, load in scenario.loads:
            demand[node] = float(load.value)
        currents = {}
        for branch in reversed(order):
            current = demand[branch.child]
            currents[branch.identifier] = current
            demand[branch.parent] += current
            if current > branch.capacity.value + 1e-12:
                issues.append(f"{scenario.name}: current exceeds capacity on {branch.identifier}")
        drops = {root: 0.0}
        for branch in order:
            drops[branch.child] = drops[branch.parent] + currents[branch.identifier] * branch.resistance.value
        load_drops = {node: drops[node] for node, _ in scenario.loads}
        for node, drop in load_drops.items():
            if drop > scenario.max_drop.value + 1e-12:
                issues.append(f"{scenario.name}: voltage drop exceeds limit at {node}")
        reports.append(ScenarioAssessment(scenario.name, currents, load_drops))

    # Detect source edits made while the calculation was running.
    for evidence in evidence_results:
        issue = _evidence_issue(project, evidence)
        if issue:
            return Assessment("blocked", (), (issue,))
    # A matching source hash and a caller-provided "verified" label do not
    # establish that the numerical inputs appear in the source, apply to this
    # branch, or cover the actual track/via geometry and fault conditions.
    # Keep the useful arithmetic, but do not turn it into routing approval.
    return Assessment("rejected" if issues else "within_input_limits", tuple(reports), tuple(issues))
