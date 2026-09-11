"""Run every detector and return one ranked list.

The report is the deliverable, not the individual detectors, so this is where the
five become comparable: one severity scale, one ordering, and a summary that says
what was looked for as well as what was found.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.debt.base import DebtKind, Finding, tally
from app.services.debt.graph_detectors import detect_circular_imports, detect_dead_code
from app.services.debt.postgres_detectors import (
    detect_complexity,
    detect_god_files,
    detect_missing_docstrings,
)

logger = logging.getLogger(__name__)

ALL_KINDS: tuple[DebtKind, ...] = (
    DebtKind.COMPLEXITY,
    DebtKind.GOD_FILE,
    DebtKind.CIRCULAR_IMPORT,
    DebtKind.DEAD_CODE,
    DebtKind.MISSING_DOCSTRING,
)


@dataclass(frozen=True, slots=True)
class DebtReport:
    """Every finding, ranked, plus what the run actually managed to do."""

    findings: list[Finding] = field(default_factory=list)
    by_kind: dict[str, int] = field(default_factory=dict)
    by_severity: dict[str, int] = field(default_factory=dict)
    # Detectors that raised. Named rather than swallowed: a report missing its
    # circular-import findings looks identical to a repository with no cycles,
    # and those are very different facts.
    failed: dict[str, str] = field(default_factory=dict)
    ran: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "by_kind": self.by_kind,
            "by_severity": self.by_severity,
            "ran": self.ran,
            "failed": self.failed,
        }


def run_detectors(
    db: Session,
    repository_id: UUID | str,
    *,
    kinds: tuple[DebtKind, ...] = ALL_KINDS,
) -> DebtReport:
    """Run the requested detectors against one repository.

    A detector that raises does not take the report with it — a missing Neo4j
    makes the two graph detectors unavailable, and the three Postgres ones are
    still worth having.
    """
    detectors = {
        DebtKind.COMPLEXITY: lambda: detect_complexity(db, repository_id),
        DebtKind.GOD_FILE: lambda: detect_god_files(db, repository_id),
        DebtKind.CIRCULAR_IMPORT: lambda: detect_circular_imports(repository_id),
        DebtKind.DEAD_CODE: lambda: detect_dead_code(db, repository_id),
        DebtKind.MISSING_DOCSTRING: lambda: detect_missing_docstrings(db, repository_id),
    }

    findings: list[Finding] = []
    failed: dict[str, str] = {}
    ran: list[str] = []

    for kind in kinds:
        detector = detectors.get(kind)
        if detector is None:
            continue
        try:
            found = detector()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            logger.exception("Debt detector %s failed", kind)
            failed[str(kind)] = f"{type(exc).__name__}: {exc}"
            continue
        ran.append(str(kind))
        findings.extend(found)

    findings.sort(key=Finding.sort_key)

    logger.info(
        "Debt scan of %s: %d finding(s) from %d detector(s)%s",
        repository_id,
        len(findings),
        len(ran),
        f", {len(failed)} failed" if failed else "",
    )

    by_kind, by_severity = tally(findings)
    return DebtReport(
        findings=findings,
        by_kind=by_kind,
        by_severity=by_severity,
        failed=failed,
        ran=ran,
    )


__all__ = ["ALL_KINDS", "DebtReport", "run_detectors"]
