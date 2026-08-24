"""Technical-debt report.

One endpoint, two representations. `format=markdown` returns the same scan as a
downloadable document rather than a second endpoint, because the alternative is
two code paths that can disagree about what the repository's debt is.
"""

import re
import uuid
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import ParseStatus, Repository
from app.schemas.debt import DebtResponse
from app.services.debt import ALL_KINDS, DebtKind, Severity, run_detectors
from app.services.debt.report import filter_findings, rollup_by_file, summary_dict, to_markdown

router = APIRouter(prefix="/repos/{repository_id}", tags=["debt"])

# Filenames end up in a Content-Disposition header, so anything that is not
# plainly safe there comes out as a hyphen.
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


@router.get(
    "/debt",
    response_model=DebtResponse,
    responses={200: {"content": {"text/markdown": {}}, "description": "Report"}},
)
def get_debt(
    repository_id: uuid.UUID,
    db: Session = Depends(get_db),
    kind: list[str] | None = Query(
        default=None,
        description="Restrict to these detectors; repeat the parameter for several",
    ),
    min_severity: str | None = Query(
        default=None,
        pattern="^(high|medium|low|info)$",
        description="Drop anything less severe than this",
    ),
    min_confidence: float = Query(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Drop findings the detector is less sure about than this",
    ),
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    format: str = Query(
        default="json",
        pattern="^(json|markdown)$",
        description="`markdown` returns the whole report as a downloadable document",
    ),
):
    """Run every detector and return the findings, ranked.

    The scan is computed per request rather than stored: it is a function of the
    parse, and a stored report would silently describe an older one. Caching it
    is the Day 4 performance pass's problem, not correctness.
    """
    repository = _require_repository(db, repository_id)
    if repository.status != ParseStatus.COMPLETE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Repository {repository.name} is {repository.status}; there is nothing "
            "to scan until a parse has completed",
        )

    kinds = _validate_kinds(kind)
    report = run_detectors(db, repository_id)

    filtered = filter_findings(
        report.findings,
        kinds=kinds,
        min_severity=Severity(min_severity) if min_severity else None,
        min_confidence=min_confidence,
    )

    if format == "markdown":
        # The document is the whole filtered scan, not the page: a report that
        # stops at an arbitrary offset is not a report.
        document = to_markdown(_replace_findings(report, filtered), repository)
        filename = f"{_safe(repository.name)}-debt.md"
        return PlainTextResponse(
            document,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    page = filtered[offset : offset + limit]
    return DebtResponse(
        summary=summary_dict(report, filtered),
        items=[asdict(f) | {"location": f.location} for f in page],
        files=[asdict(entry) for entry in rollup_by_file(filtered)],
        limit=limit,
        offset=offset,
        truncated=len(filtered) > offset + len(page),
    )


def _validate_kinds(kind: list[str] | None) -> set[str] | None:
    if not kind:
        return None
    known = {str(k) for k in ALL_KINDS}
    if unknown := set(kind) - known:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown detector(s): {', '.join(sorted(unknown))}. "
            f"Available: {', '.join(sorted(known))}",
        )
    return set(kind)


def _replace_findings(report, findings):
    """A copy of the scan carrying only the filtered findings.

    The Markdown renderer reads `ran` and `failed` off the report, so the
    filtered document still says which detectors ran — dropping that to pass a
    plain list would lose the part that makes an incomplete report legible.
    """
    from dataclasses import replace

    return replace(report, findings=findings)


def _safe(name: str) -> str:
    cleaned = _UNSAFE_FILENAME.sub("-", name).strip("-.") or "repository"
    return cleaned[:64]


def _require_repository(db: Session, repository_id: uuid.UUID) -> Repository:
    repository = db.get(Repository, repository_id)
    if repository is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No repository {repository_id}")
    return repository


__all__ = ["DebtKind", "router"]
