"""Detectors that read the flat facts: complexity, file size, docstrings.

All three come from Postgres, and all three are calibrated against the
repository's own distribution rather than a number from a style guide.
"""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import SourceFile, Symbol
from app.services.debt.base import (
    DebtKind,
    Finding,
    Severity,
    percentile,
    severity_from_ratio,
)
from app.services.graph_keys import file_key, symbol_key

# Below this, complexity is not worth reporting however unusual it is for the
# repo. A function with four branches is readable in any codebase.
COMPLEXITY_FLOOR = 10

# Same idea for files. A 300-line module is not a god file even if every other
# file in the repo is 40 lines.
GOD_FILE_LINE_FLOOR = 400
GOD_FILE_SYMBOL_FLOOR = 20

# The distribution point a finding has to clear. p90 rather than p95: at p95 a
# 40-file repository reports two findings, which is not a report.
CALIBRATION = 0.90

# Names Python itself treats as non-public. A missing docstring on `_helper` is a
# choice; on `parse_repository` it is a gap.
_PRIVATE = re.compile(r"^_")

# Dunder methods carry meaning from the data model, not from a docstring.
_DUNDER = re.compile(r"^__.*__$")

# Symbols whose docstring nobody reads, because their name is the documentation.
_SELF_EVIDENT = frozenset({"__init__", "__repr__", "__str__", "main"})


def detect_complexity(
    db: Session, repository_id: UUID | str, *, floor: int = COMPLEXITY_FLOOR
) -> list[Finding]:
    """Functions and methods far more branching than the rest of the repository."""
    rows = db.execute(
        select(
            Symbol.qualname,
            Symbol.module,
            Symbol.complexity,
            Symbol.line_start,
            Symbol.line_end,
            SourceFile.path,
        )
        .join(SourceFile, Symbol.file_id == SourceFile.id)
        .where(
            Symbol.repository_id == repository_id,
            Symbol.kind.in_(("function", "method")),
        )
    ).all()
    if not rows:
        return []

    threshold = max(float(floor), percentile([float(r.complexity) for r in rows], CALIBRATION))

    findings = []
    for row in rows:
        if row.complexity < threshold:
            continue
        ratio = row.complexity / threshold
        findings.append(
            Finding(
                kind=DebtKind.COMPLEXITY,
                severity=severity_from_ratio(ratio),
                subject=row.qualname,
                path=row.path,
                line_start=row.line_start,
                key=symbol_key(repository_id, row.module, row.qualname, row.path),
                why=(
                    f"{row.complexity} independent paths through "
                    f"{row.line_end - row.line_start + 1} lines — the repository's "
                    f"90th percentile is {threshold:.0f}"
                ),
                metrics={
                    "complexity": row.complexity,
                    "lines": row.line_end - row.line_start + 1,
                    "threshold": round(threshold, 1),
                },
            )
        )
    return findings


def detect_god_files(
    db: Session,
    repository_id: UUID | str,
    *,
    line_floor: int = GOD_FILE_LINE_FLOOR,
    symbol_floor: int = GOD_FILE_SYMBOL_FLOOR,
) -> list[Finding]:
    """Files carrying too much: long, and holding many symbols.

    Both conditions, not either. A long file of generated constants is not a
    design problem, and a short file with fifteen tiny functions is fine.
    """
    rows = db.execute(
        select(
            SourceFile.path,
            SourceFile.line_count,
            func.count(Symbol.id).label("symbols"),
        )
        .outerjoin(Symbol, Symbol.file_id == SourceFile.id)
        .where(SourceFile.repository_id == repository_id)
        .group_by(SourceFile.id, SourceFile.path, SourceFile.line_count)
    ).all()
    if not rows:
        return []

    line_threshold = max(
        float(line_floor), percentile([float(r.line_count) for r in rows], CALIBRATION)
    )
    symbol_threshold = max(
        float(symbol_floor), percentile([float(r.symbols) for r in rows], CALIBRATION)
    )

    findings = []
    for row in rows:
        if row.line_count < line_threshold or row.symbols < symbol_threshold:
            continue
        # Severity tracks whichever dimension is worse; a 3000-line file with
        # exactly the threshold symbol count is still a problem.
        ratio = max(row.line_count / line_threshold, row.symbols / symbol_threshold)
        findings.append(
            Finding(
                kind=DebtKind.GOD_FILE,
                severity=severity_from_ratio(ratio),
                subject=row.path,
                path=row.path,
                line_start=1,
                key=file_key(repository_id, row.path),
                why=(
                    f"{row.line_count} lines holding {row.symbols} symbols — past "
                    f"this repository's 90th percentile of {line_threshold:.0f} lines "
                    f"and {symbol_threshold:.0f} symbols"
                ),
                metrics={
                    "lines": row.line_count,
                    "symbols": row.symbols,
                    "line_threshold": round(line_threshold),
                    "symbol_threshold": round(symbol_threshold),
                },
            )
        )
    return findings


def detect_missing_docstrings(db: Session, repository_id: UUID | str) -> list[Finding]:
    """Public symbols with nothing explaining them.

    Reported at `info`/`low` only. A missing docstring is a gap, not a defect,
    and ranking it beside a circular import would make the report useless.
    """
    rows = db.execute(
        select(
            Symbol.qualname,
            Symbol.name,
            Symbol.module,
            Symbol.kind,
            Symbol.line_start,
            Symbol.line_end,
            Symbol.complexity,
            SourceFile.path,
        )
        .join(SourceFile, Symbol.file_id == SourceFile.id)
        .where(
            Symbol.repository_id == repository_id,
            Symbol.docstring.is_(None),
        )
    ).all()

    findings = []
    for row in rows:
        if not _wants_docstring(row.name, row.qualname):
            continue
        lines = row.line_end - row.line_start + 1
        # A long or branching public symbol with no docstring costs a reader
        # more than a two-line one, so it is the only case worth `low`.
        substantial = lines >= 15 or row.complexity >= 5
        findings.append(
            Finding(
                kind=DebtKind.MISSING_DOCSTRING,
                severity=Severity.LOW if substantial else Severity.INFO,
                subject=row.qualname,
                path=row.path,
                line_start=row.line_start,
                key=symbol_key(repository_id, row.module, row.qualname, row.path),
                why=(
                    f"public {row.kind}, {lines} lines, complexity "
                    f"{row.complexity}, no docstring"
                ),
                metrics={"lines": lines, "complexity": row.complexity, "kind": row.kind},
            )
        )
    return findings


def _wants_docstring(name: str, qualname: str) -> bool:
    """Whether a missing docstring on this symbol is worth reporting."""
    if name in _SELF_EVIDENT:
        return False
    if _DUNDER.match(name):
        return False
    # Any private component makes the whole symbol private: a public method on a
    # private class is not part of the public surface.
    return not any(_PRIVATE.match(part) for part in qualname.split("."))


__all__ = [
    "CALIBRATION",
    "COMPLEXITY_FLOOR",
    "GOD_FILE_LINE_FLOOR",
    "GOD_FILE_SYMBOL_FLOOR",
    "detect_complexity",
    "detect_god_files",
    "detect_missing_docstrings",
]
