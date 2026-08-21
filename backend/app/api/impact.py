"""Impact analysis endpoint — "if I change this, what breaks?".

The node key is a query parameter rather than the timeline's `/impact/{node}`
path segment, for the reason already recorded in `api/graph.py`: keys contain
slashes, and a path segment would force every client to double-encode them.
"""

import uuid
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Repository
from app.schemas.impact import ImpactExplanationResponse, ImpactResponse
from app.services.graph_queries import MAX_DEPTH, NodeNotFound
from app.services.impact import DEFAULT_DECAY, DEFAULT_IMPACT_DEPTH, impact
from app.services.impact_explain import explain_impact
from app.services.providers import ProviderError

router = APIRouter(prefix="/repos/{repository_id}", tags=["impact"])


@router.get("/impact", response_model=ImpactResponse)
def get_impact(
    repository_id: uuid.UUID,
    db: Session = Depends(get_db),
    key: str = Query(description="Node key, from /graph/search"),
    depth: int = Query(default=DEFAULT_IMPACT_DEPTH, ge=1, le=MAX_DEPTH),
    limit: int = Query(default=200, ge=1, le=1000),
    decay: float = Query(
        default=DEFAULT_DECAY,
        gt=0,
        le=1,
        description="Score retained per hop; 1 disables decay",
    ),
) -> ImpactResponse:
    """The ranked blast radius of a change to `key`, with the path to each node."""
    _require_repository(db, repository_id)
    try:
        result = impact(repository_id, key, depth=depth, limit=limit, decay=decay)
    except NodeNotFound as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No graph node {key!r} in this repository. "
            "Has it been parsed, and is the key from /graph/search?",
        ) from exc

    return ImpactResponse(
        root=result.root,
        items=result.items,
        total=len(result.items),
        truncated=result.truncated,
        summary=result.summary,
    )


@router.get("/impact/explain", response_model=ImpactExplanationResponse)
def explain(
    repository_id: uuid.UUID,
    db: Session = Depends(get_db),
    key: str = Query(description="Node key, from /graph/search"),
    depth: int = Query(default=DEFAULT_IMPACT_DEPTH, ge=1, le=MAX_DEPTH),
) -> ImpactExplanationResponse:
    """The same blast radius as `/impact`, explained in prose.

    Not streamed, unlike `/chat`: this is a few hundred tokens rendered beside a
    graph, and a single JSON response is cacheable, re-renderable and far easier
    for the panel to hold than a stream it would have to reassemble.
    """
    repository = _require_repository(db, repository_id)
    try:
        explanation = explain_impact(
            db, repository_id, key, depth=depth, commit_sha=repository.commit_sha
        )
    except NodeNotFound as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No graph node {key!r} in this repository. "
            "Has it been parsed, and is the key from /graph/search?",
        ) from exc
    except ProviderError as exc:
        # The radius itself is fine; only the explanation is unavailable. Say
        # which, so the UI can keep showing the ranked list.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"The blast radius was computed but could not be explained — {exc}",
        ) from exc

    return ImpactExplanationResponse(**asdict(explanation))


def _require_repository(db: Session, repository_id: uuid.UUID) -> Repository:
    repository = db.get(Repository, repository_id)
    if repository is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No repository {repository_id}")
    return repository
