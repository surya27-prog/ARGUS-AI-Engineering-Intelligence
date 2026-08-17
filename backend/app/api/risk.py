"""Risk endpoint — the riskiest things in a repository, or one node's score.

One endpoint for both, as with `/cochange`: the single-node answer is a
one-element ranking, and giving it a separate shape would make every client
handle two.
"""

import uuid
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Repository
from app.schemas.risk import RiskResponse
from app.services.graph_queries import MAX_DEPTH, NodeNotFound
from app.services.risk import DEFAULT_RISK_DEPTH, rank_repository, score_node

router = APIRouter(prefix="/repos/{repository_id}", tags=["risk"])


@router.get("/risk", response_model=RiskResponse)
def get_risk(
    repository_id: uuid.UUID,
    db: Session = Depends(get_db),
    key: str | None = Query(default=None, description="Node key; omit to rank the repository"),
    type: str | None = Query(
        default=None,
        pattern="^(Function|File)$",
        description="Restrict the ranking to one node type",
    ),
    depth: int = Query(default=DEFAULT_RISK_DEPTH, ge=1, le=MAX_DEPTH),
    limit: int = Query(default=20, ge=1, le=200),
) -> RiskResponse:
    """Risk scores with their whole derivation attached."""
    _require_repository(db, repository_id)

    if key is not None:
        try:
            items = [score_node(repository_id, key, depth=depth)]
        except NodeNotFound as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"No scorable node {key!r} in this repository. Risk is scored for "
                ":Function and :File nodes; is the key from /graph/search?",
            ) from exc
        scored = 1
    else:
        # `scored` is reported separately from `total` so a caller can see that
        # the ranking considered the whole repository, not just the page it got.
        items, scored = rank_repository(repository_id, node_type=type, depth=depth, limit=limit)

    # `asdict` rather than handing the dataclasses over: it flattens the nested
    # `RiskMetrics` too, which pydantic will not do for a plain dataclass.
    return RiskResponse(
        items=[asdict(item) for item in items],
        total=len(items),
        depth=depth,
        scored=scored,
    )


def _require_repository(db: Session, repository_id: uuid.UUID) -> Repository:
    repository = db.get(Repository, repository_id)
    if repository is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No repository {repository_id}")
    return repository
