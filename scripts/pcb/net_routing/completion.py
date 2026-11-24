"""completion.py: classify board-verification evidence; an unknown is never a pass.

An unknown or invalid measurement is reported as an issue, never as a pass.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VerificationEvidence:
    """Independent measurements of a prepared board; None means not measured.

    Inputs:
        open_connections, errors, warnings, parity_errors, unverified_pads and
            unassigned_copper: Counts from native checks.
        ignored_checks: Names of the checks the DRC ignored.
        coverage_complete, geometry_ok, electrical_ok, reference_copper_ok,
            protected_unchanged and views_current: True only when that check passed.
    """

    open_connections: int | None
    errors: int | None
    warnings: int | None
    parity_errors: int | None
    ignored_checks: tuple[str, ...] | None
    unverified_pads: int | None
    unassigned_copper: int | None
    coverage_complete: bool | None
    geometry_ok: bool | None
    electrical_ok: bool | None
    reference_copper_ok: bool | None
    protected_unchanged: bool | None
    views_current: bool | None


class CompletionIssues:
    """Lists every reason a board cannot yet be called complete."""

    def __call__(self, evidence: VerificationEvidence) -> tuple[str, ...]:
        """An unknown or invalid measurement is never equivalent to a pass."""
        if not isinstance(evidence, VerificationEvidence):
            raise ValueError("Expected independent verification evidence")
        issues = []
        counts = (
            ("open connections", evidence.open_connections),
            ("DRC errors", evidence.errors),
            ("DRC warnings", evidence.warnings),
            ("schematic parity errors", evidence.parity_errors),
            ("unverified pads", evidence.unverified_pads),
            ("unassigned copper", evidence.unassigned_copper),
        )
        for label, value in counts:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                issues.append(f"{label}: unknown or invalid")
            elif value:
                issues.append(f"{label}: {value}")
        if not isinstance(evidence.ignored_checks, tuple) or any(
            not isinstance(item, str) or not item for item in evidence.ignored_checks
        ):
            issues.append("ignored checks: unknown or invalid")
        elif evidence.ignored_checks:
            issues.append(f"ignored checks: {', '.join(evidence.ignored_checks)}")
        checks = (
            ("whole-board net coverage", evidence.coverage_complete),
            ("routing geometry", evidence.geometry_ok),
            ("electrical model", evidence.electrical_ok),
            ("reference copper", evidence.reference_copper_ok),
            ("protected items", evidence.protected_unchanged),
            ("review views", evidence.views_current),
        )
        issues.extend(
            f"{label}: not verified" for label, passed in checks if passed is not True
        )
        return tuple(issues)


completion_issues = CompletionIssues()
