"""Technical-debt detection.

Five detectors over the facts ARGUS already has, split by which store can answer
them: complexity, file size and docstrings come from Postgres; circular imports
and dead code need the graph.

Week 5 Day 2 turns `DebtReport` into an endpoint and an exportable document, so
everything here returns data and prints nothing.
"""

from app.services.debt.base import (
    SEVERITY_ORDER,
    DebtKind,
    Finding,
    Severity,
    percentile,
    severity_from_ratio,
)
from app.services.debt.graph_detectors import detect_circular_imports, detect_dead_code
from app.services.debt.postgres_detectors import (
    detect_complexity,
    detect_god_files,
    detect_missing_docstrings,
)
from app.services.debt.runner import ALL_KINDS, DebtReport, run_detectors

__all__ = [
    "ALL_KINDS",
    "SEVERITY_ORDER",
    "DebtKind",
    "DebtReport",
    "Finding",
    "Severity",
    "detect_circular_imports",
    "detect_complexity",
    "detect_dead_code",
    "detect_god_files",
    "detect_missing_docstrings",
    "percentile",
    "run_detectors",
    "severity_from_ratio",
]
