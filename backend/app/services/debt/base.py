"""The shape every debt detector produces.

One `Finding` type across five detectors, not five shapes. Week 5 Day 2 has to
rank complexity against dead code against circular imports in a single ordered
report, and a per-detector payload would make that ranking arbitrary.

Two design rules hold across all of them:

**Thresholds are relative to the repository, not absolute.** A 600-line file is
unremarkable in one codebase and the worst offender in another. Every detector
that can be calibrated compares against the repo's own distribution, with an
absolute floor so a small tidy repo does not report its least-tidy tenth as debt.

**Findings carry their evidence.** `metrics` holds the numbers the severity came
from and `why` reads them back in a sentence. A finding a reviewer cannot check
is a finding they will learn to ignore.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DebtKind(StrEnum):
    """What was found. The detector's name, from the reader's point of view."""

    COMPLEXITY = "complexity"
    GOD_FILE = "god_file"
    CIRCULAR_IMPORT = "circular_import"
    DEAD_CODE = "dead_code"
    MISSING_DOCSTRING = "missing_docstring"


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Ordered worst-first, which is the order a report wants.
SEVERITY_ORDER: dict[str, int] = {
    Severity.HIGH: 0,
    Severity.MEDIUM: 1,
    Severity.LOW: 2,
    Severity.INFO: 3,
}


@dataclass(frozen=True, slots=True)
class Finding:
    """One piece of technical debt, with the evidence behind it."""

    kind: DebtKind
    severity: Severity
    # Where. `key` is the graph node key when the subject has one, so the UI can
    # jump from a finding to the graph and the blast radius.
    subject: str
    path: str | None = None
    line_start: int | None = None
    key: str | None = None
    why: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    # How sure the detector is that this is real debt rather than a
    # false positive. Dead code is the reason this field exists: static analysis
    # of a dynamic language cannot see every reference, so the honest output is a
    # confidence, not a verdict.
    confidence: float = 1.0

    @property
    def location(self) -> str:
        if self.path and self.line_start:
            return f"{self.path}:{self.line_start}"
        return self.path or self.subject

    def sort_key(self) -> tuple[int, float, str]:
        """Worst first, then most confident, then stably by subject."""
        return (SEVERITY_ORDER[self.severity], -self.confidence, self.subject)


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile, so the result is always an observed value.

    Interpolating would invent a threshold no file actually has, which makes a
    finding harder to explain than one drawn from the data itself.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return float(ordered[index])


def severity_from_ratio(ratio: float) -> Severity:
    """How far past its threshold something is, as a severity.

    Shared so "twice the threshold" means the same thing in every detector.
    """
    if ratio >= 3.0:
        return Severity.HIGH
    if ratio >= 2.0:
        return Severity.MEDIUM
    if ratio >= 1.0:
        return Severity.LOW
    return Severity.INFO


__all__ = [
    "SEVERITY_ORDER",
    "DebtKind",
    "Finding",
    "Severity",
    "percentile",
    "severity_from_ratio",
]
