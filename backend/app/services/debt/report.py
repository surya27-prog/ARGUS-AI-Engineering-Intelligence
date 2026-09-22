"""Aggregate the findings into a report, and render it as Markdown.

`run_detectors` returns a ranked list, which is the raw material rather than the
report. Two things turn it into one:

**A per-file rollup.** The flat list is ranked correctly but reads as 509
unrelated problems. Debt is not distributed evenly — a handful of files usually
carry most of it, and "these four files hold a third of the findings" is a
sentence someone can act on, where "here are 509 findings" is not.

**Saying what was looked for, not only what was found.** A report listing zero
circular imports because the detector crashed looks exactly like a clean
codebase. So the rendered document names the detectors that ran, and any that
did not, above the findings.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.models import Repository
from app.services.debt.base import SEVERITIES_WORST_FIRST, SEVERITY_ORDER, Finding, Severity
from app.services.debt.runner import DebtReport

# Files listed in the rollup. Enough to show the concentration, short enough to
# read; the full picture is in the findings list itself.
TOP_FILES = 15

# Findings rendered per severity section in Markdown. A document nobody scrolls
# to the end of is not more useful for being complete, and the JSON response
# carries everything.
MARKDOWN_PER_SEVERITY = 40


@dataclass(frozen=True, slots=True)
class FileDebt:
    """How much debt one file carries."""

    path: str
    findings: int
    worst: str
    by_kind: dict[str, int] = field(default_factory=dict)


def rollup_by_file(findings: list[Finding], *, limit: int = TOP_FILES) -> list[FileDebt]:
    """Which files carry the most debt, worst-first.

    Ranked by the severity of a file's worst finding before its count, so one
    circular import outranks twenty missing docstrings — which is the order a
    reviewer would pick things up in.
    """
    grouped: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        if finding.path:
            grouped[finding.path].append(finding)

    rolled = [
        FileDebt(
            path=path,
            findings=len(group),
            worst=str(min(group, key=lambda f: SEVERITY_ORDER[f.severity]).severity),
            by_kind=dict(Counter(str(f.kind) for f in group).most_common()),
        )
        for path, group in grouped.items()
    ]
    rolled.sort(key=lambda f: (SEVERITY_ORDER[Severity(f.worst)], -f.findings, f.path))
    return rolled[:limit]


def filter_findings(
    findings: list[Finding],
    *,
    kinds: set[str] | None = None,
    min_severity: Severity | None = None,
    min_confidence: float = 0.0,
) -> list[Finding]:
    """Narrow a scan without re-running it."""
    ceiling = SEVERITY_ORDER[min_severity] if min_severity else None
    return [
        f
        for f in findings
        if (kinds is None or str(f.kind) in kinds)
        and (ceiling is None or SEVERITY_ORDER[f.severity] <= ceiling)
        and f.confidence >= min_confidence
    ]


def to_markdown(
    report: DebtReport,
    repository: Repository,
    *,
    per_severity: int = MARKDOWN_PER_SEVERITY,
    generated_at: datetime | None = None,
) -> str:
    """The report as a Markdown document, for download or for a PR comment."""
    stamp = (generated_at or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    commit = (repository.commit_sha or "")[:8] or "unknown commit"

    lines = [
        f"# Technical debt — {repository.name}",
        "",
        f"`{commit}` on `{repository.default_branch or '?'}` · "
        f"{repository.file_count} files · {repository.symbol_count} symbols · "
        f"scanned {stamp}",
        "",
        f"**{report.total} findings.** Thresholds are calibrated against this "
        "repository's own distribution, not an absolute style guide, so a finding "
        "means *unusual for this codebase* rather than *over some general limit*.",
        "",
        "## What was looked for",
        "",
    ]

    for kind in report.ran:
        found = report.by_kind.get(kind, 0)
        lines.append(f"- `{kind}` — {found} finding{'s' if found != 1 else ''}")
    for kind, error in report.failed.items():
        # Above the findings, not in a footnote: a missing detector changes how
        # the whole document should be read.
        lines.append(f"- `{kind}` — **did not run**: {error}")
    if report.failed:
        lines += [
            "",
            "> This report is incomplete. A detector that did not run reports zero "
            "findings, which is not the same as a clean result.",
        ]

    lines += ["", "## Summary", "", "| Severity | Findings |", "|---|---|"]
    for severity in SEVERITIES_WORST_FIRST:
        count = report.by_severity.get(str(severity), 0)
        if count:
            lines.append(f"| {severity} | {count} |")

    if files := rollup_by_file(report.findings):
        lines += [
            "",
            "## Files carrying the most debt",
            "",
            "| File | Findings | Worst | Kinds |",
            "|---|---|---|---|",
        ]
        for entry in files:
            kinds = ", ".join(f"{k} ×{v}" for k, v in entry.by_kind.items())
            lines.append(
                f"| `{entry.path}` | {entry.findings} | {entry.worst} | {kinds} |"
            )

    lines += ["", "## Findings", ""]
    if not report.findings:
        lines.append("Nothing found. Every detector ran and reported clean.")

    for severity in SEVERITIES_WORST_FIRST:
        group = [f for f in report.findings if f.severity == severity]
        if not group:
            continue
        lines += ["", f"### {str(severity).title()} ({len(group)})", ""]
        for finding in group[:per_severity]:
            confidence = (
                ""
                if finding.confidence >= 1.0
                else f" _(confidence {finding.confidence:.2f})_"
            )
            lines.append(f"- **{finding.kind}** `{finding.location}`{confidence}")
            lines.append(f"  {finding.subject} — {finding.why}")
        if len(group) > per_severity:
            lines.append(
                f"\n_{len(group) - per_severity} more {severity} findings omitted "
                "from this document; the JSON response has all of them._"
            )

    lines += [
        "",
        "---",
        "",
        "Generated by ARGUS. Dead-code findings carry a confidence below 1.0 "
        "because a call made at module scope is not visible to the call graph — "
        "check before deleting.",
        "",
    ]
    return "\n".join(lines)


def summary_dict(report: DebtReport, findings: list[Finding]) -> dict[str, Any]:
    """Counts for the filtered view, alongside what the whole scan found."""
    return {
        "total": len(findings),
        "scanned_total": report.total,
        "by_kind": dict(Counter(str(f.kind) for f in findings).most_common()),
        "by_severity": {
            str(severity): count
            for severity, count in (
                (s, sum(1 for f in findings if f.severity == s)) for s in SEVERITIES_WORST_FIRST
            )
            if count
        },
        "ran": report.ran,
        "failed": report.failed,
    }


__all__ = [
    "MARKDOWN_PER_SEVERITY",
    "TOP_FILES",
    "FileDebt",
    "filter_findings",
    "rollup_by_file",
    "summary_dict",
    "to_markdown",
]
