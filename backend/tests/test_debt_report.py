"""Debt report tests — aggregation, filtering, Markdown, and the endpoint.

The aggregation and rendering are pure and tested on hand-built findings, so a
test can state the exact distribution it asserts about. The endpoint runs against
a real repository so the two representations are checked against one scan.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ParseStatus, Repository, SourceFile, Symbol
from app.services.debt import DebtKind, Finding, Severity
from app.services.debt.report import (
    filter_findings,
    rollup_by_file,
    summary_dict,
    to_markdown,
)
from app.services.debt.runner import DebtReport
from app.services.graph_writer import delete_repo_graph, write_parsed_repo
from tests.test_debt import _cyclic_repo

# --- fixtures ----------------------------------------------------------------


def _finding(
    kind: DebtKind,
    severity: Severity,
    subject: str,
    *,
    path: str | None = None,
    confidence: float = 1.0,
) -> Finding:
    return Finding(
        kind=kind,
        severity=severity,
        subject=subject,
        path=path,
        line_start=1 if path else None,
        why=f"{subject} is a problem",
        confidence=confidence,
    )


@pytest.fixture
def findings() -> list[Finding]:
    return [
        _finding(DebtKind.CIRCULAR_IMPORT, Severity.HIGH, "a<->b", path="app/a.py"),
        _finding(DebtKind.COMPLEXITY, Severity.MEDIUM, "big", path="app/big.py"),
        _finding(DebtKind.DEAD_CODE, Severity.MEDIUM, "unused", path="app/big.py", confidence=0.75),
        _finding(DebtKind.MISSING_DOCSTRING, Severity.INFO, "one", path="app/big.py"),
        _finding(DebtKind.MISSING_DOCSTRING, Severity.INFO, "two", path="app/big.py"),
        _finding(DebtKind.MISSING_DOCSTRING, Severity.INFO, "three", path="app/quiet.py"),
    ]


# --- the per-file rollup -----------------------------------------------------


def test_rollup_groups_findings_by_file(findings: list[Finding]):
    rolled = {entry.path: entry for entry in rollup_by_file(findings)}

    assert rolled["app/big.py"].findings == 4
    assert rolled["app/big.py"].by_kind["missing_docstring"] == 2
    assert rolled["app/quiet.py"].findings == 1


def test_rollup_ranks_by_worst_finding_before_count(findings: list[Finding]):
    """One circular import outranks four docstring gaps — that is the order a
    reviewer would pick things up in."""
    paths = [entry.path for entry in rollup_by_file(findings)]

    assert paths[0] == "app/a.py"
    assert paths.index("app/big.py") < paths.index("app/quiet.py")


def test_rollup_reports_each_file_s_worst_severity(findings: list[Finding]):
    rolled = {entry.path: entry.worst for entry in rollup_by_file(findings)}

    assert rolled["app/a.py"] == "high"
    assert rolled["app/big.py"] == "medium"
    assert rolled["app/quiet.py"] == "info"


def test_rollup_ignores_findings_with_no_file(findings: list[Finding]):
    pathless = [*findings, _finding(DebtKind.CIRCULAR_IMPORT, Severity.HIGH, "nowhere")]
    assert len(rollup_by_file(pathless)) == len(rollup_by_file(findings))


def test_rollup_honours_its_limit(findings: list[Finding]):
    assert len(rollup_by_file(findings, limit=1)) == 1


# --- filtering ---------------------------------------------------------------


def test_filter_by_kind(findings: list[Finding]):
    kept = filter_findings(findings, kinds={"missing_docstring"})
    assert {str(f.kind) for f in kept} == {"missing_docstring"}
    assert len(kept) == 3


def test_filter_by_minimum_severity(findings: list[Finding]):
    kept = filter_findings(findings, min_severity=Severity.MEDIUM)
    assert {str(f.severity) for f in kept} == {"high", "medium"}


def test_filter_by_confidence_drops_the_uncertain(findings: list[Finding]):
    """The knob that turns off "might be dead code"."""
    kept = filter_findings(findings, min_confidence=0.9)
    assert all(f.confidence >= 0.9 for f in kept)
    assert "unused" not in {f.subject for f in kept}


def test_filters_compose(findings: list[Finding]):
    kept = filter_findings(
        findings, kinds={"complexity", "dead_code"}, min_severity=Severity.MEDIUM
    )
    assert {f.subject for f in kept} == {"big", "unused"}


def test_summary_reports_filtered_and_scanned_totals(findings: list[Finding]):
    """So a reader can tell a narrow filter from a clean repository."""
    report = DebtReport(findings=findings, ran=["complexity"], failed={})
    filtered = filter_findings(findings, kinds={"complexity"})

    summary = summary_dict(report, filtered)

    assert summary["total"] == 1
    assert summary["scanned_total"] == 6


# --- Markdown ----------------------------------------------------------------


@pytest.fixture
def repo_stub() -> Repository:
    """Detached, not persisted: rendering a document needs a repository's fields,
    not a row, and a pure test should not need Postgres to run."""
    return Repository(
        name="requests",
        commit_sha="8068356288978c4f54661ae6f95afe0e0831885e",
        default_branch="main",
        file_count=37,
        symbol_count=807,
    )


def test_markdown_leads_with_the_repository_and_commit(
    findings: list[Finding], repo_stub: Repository
):
    document = to_markdown(DebtReport(findings=findings, ran=["complexity"]), repo_stub)

    assert document.startswith(f"# Technical debt — {repo_stub.name}")
    assert "8068356" in document
    assert "on `main`" in document
    assert "37 files" in document


def test_markdown_says_what_was_looked_for(findings: list[Finding], repo_stub: Repository):
    report = DebtReport(findings=findings, ran=["complexity", "god_file"])
    document = to_markdown(report, repo_stub)

    assert "## What was looked for" in document
    assert "`complexity`" in document
    assert "`god_file`" in document


def test_markdown_warns_loudly_when_a_detector_did_not_run(
    findings: list[Finding], repo_stub: Repository
):
    """A crashed detector reports zero findings, which reads as a clean result."""
    report = DebtReport(
        findings=findings,
        ran=["complexity"],
        failed={"circular_import": "RuntimeError: neo4j is down"},
    )
    document = to_markdown(report, repo_stub)

    assert "**did not run**" in document
    assert "neo4j is down" in document
    assert "This report is incomplete" in document
    # The warning has to be above the findings, not in a footnote.
    assert document.index("This report is incomplete") < document.index("## Findings")


def test_markdown_groups_findings_by_severity(
    findings: list[Finding], repo_stub: Repository
):
    document = to_markdown(DebtReport(findings=findings, ran=["complexity"]), repo_stub)

    assert "### High (1)" in document
    assert "### Medium (2)" in document
    assert "### Info (3)" in document
    assert document.index("### High") < document.index("### Info")


def test_markdown_marks_uncertain_findings(findings: list[Finding], repo_stub: Repository):
    document = to_markdown(DebtReport(findings=findings, ran=["dead_code"]), repo_stub)

    assert "_(confidence 0.75)_" in document
    # A certain finding gets no annotation.
    assert document.count("confidence 1.00") == 0


def test_markdown_says_when_it_omitted_findings(repo_stub: Repository):
    many = [
        _finding(DebtKind.MISSING_DOCSTRING, Severity.INFO, f"s{i}", path="app/m.py")
        for i in range(50)
    ]
    document = to_markdown(
        DebtReport(findings=many, ran=["missing_docstring"]), repo_stub, per_severity=10
    )

    assert "40 more info findings omitted" in document


def test_markdown_of_a_clean_repository_says_so(repo_stub: Repository):
    document = to_markdown(DebtReport(findings=[], ran=["complexity"]), repo_stub)

    assert "Nothing found" in document
    assert "Every detector ran and reported clean" in document


# --- the endpoint ------------------------------------------------------------


@pytest.fixture
def scanned(db: Session, repository: Repository):
    """A repository with a graph, Postgres rows, and a completed parse."""
    write_parsed_repo(repository.id, _cyclic_repo())
    source = SourceFile(
        repository_id=repository.id, path="app/a.py", module="app.a", line_count=6
    )
    source.symbols = [
        Symbol(
            repository_id=repository.id,
            name="alpha",
            qualname="alpha",
            kind="function",
            module="app.a",
            line_start=1,
            line_end=6,
            complexity=1,
            docstring=None,
        )
    ]
    repository.status = ParseStatus.COMPLETE
    repository.commit_sha = "abc1234def"
    repository.default_branch = "main"
    db.add(source)
    db.commit()
    try:
        yield repository
    finally:
        delete_repo_graph(repository.id)


def test_debt_endpoint_returns_a_ranked_report(client: TestClient, scanned: Repository):
    response = client.get(f"/repos/{scanned.id}/debt")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["total"] >= 1
    assert set(body["summary"]["ran"]) == {str(k) for k in DebtKind}
    assert not body["summary"]["failed"]
    # Every finding carries its evidence.
    for item in body["items"]:
        assert item["why"]
        assert "confidence" in item
        assert "location" in item


def test_debt_endpoint_paginates(client: TestClient, scanned: Repository):
    first = client.get(f"/repos/{scanned.id}/debt?limit=1").json()

    assert len(first["items"]) == 1
    assert first["limit"] == 1
    # `truncated` is what stops a page reading as the whole result.
    assert first["truncated"] == (first["summary"]["total"] > 1)


def test_debt_endpoint_filters_by_kind(client: TestClient, scanned: Repository):
    body = client.get(f"/repos/{scanned.id}/debt?kind=circular_import").json()

    assert {i["kind"] for i in body["items"]} == {"circular_import"}
    # The summary still reports what the whole scan found.
    assert body["summary"]["scanned_total"] >= body["summary"]["total"]


def test_debt_endpoint_rejects_an_unknown_detector(client: TestClient, scanned: Repository):
    response = client.get(f"/repos/{scanned.id}/debt?kind=nonsense")

    assert response.status_code == 422
    assert "nonsense" in response.json()["detail"]


def test_debt_endpoint_409s_before_a_parse_completes(
    client: TestClient, repository: Repository
):
    """Scanning a half-parsed repository would report a clean bill of health."""
    response = client.get(f"/repos/{repository.id}/debt")

    assert response.status_code == 409
    assert "nothing" in response.json()["detail"].lower()


def test_debt_endpoint_404s_for_an_unknown_repository(client: TestClient):
    assert client.get("/repos/00000000-0000-0000-0000-000000000000/debt").status_code == 404


def test_markdown_format_is_a_download(client: TestClient, scanned: Repository):
    response = client.get(f"/repos/{scanned.id}/debt?format=markdown")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment" in response.headers["content-disposition"]
    assert ".md" in response.headers["content-disposition"]
    assert response.text.startswith("# Technical debt")


def test_markdown_download_respects_filters(client: TestClient, scanned: Repository):
    response = client.get(f"/repos/{scanned.id}/debt?format=markdown&kind=circular_import")

    assert "circular_import" in response.text
    assert "missing_docstring" not in response.text


def test_download_filename_is_safe(client: TestClient, db: Session, scanned: Repository):
    """A repository name reaches a Content-Disposition header, so it is scrubbed."""
    scanned.name = 'evil"; rm -rf /'
    db.commit()

    disposition = client.get(f"/repos/{scanned.id}/debt?format=markdown").headers[
        "content-disposition"
    ]

    assert '"' not in disposition.replace('filename="', "").replace('"', "", 1)
    assert "rm" not in disposition or "/" not in disposition
