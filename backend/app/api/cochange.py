"""Co-change endpoint — which files this repository changes together.

One endpoint, not two: with `key` it answers "what moves with this file", and
without it "what moves together in this repo at all". Both are the same list of
pairs, so they are the same response shape rather than two that a client has to
tell apart.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Repository
from app.schemas.cochange import CoChangeResponse
from app.services.cochange import coupled_files
from app.services.graph_queries import NodeNotFound, get_node

router = APIRouter(prefix="/repos/{repository_id}", tags=["cochange"])


@router.get("/cochange", response_model=CoChangeResponse)
def get_cochange(
    repository_id: uuid.UUID,
    db: Session = Depends(get_db),
    key: str | None = Query(
        default=None, description="File node key; omit for the whole repository"
    ),
    min_commits: int = Query(default=2, ge=1, le=1000),
    limit: int = Query(default=100, ge=1, le=1000),
) -> CoChangeResponse:
    """Coupled file pairs, strongest first."""
    _require_repository(db, repository_id)

    root = None
    if key is not None:
        # Looked up rather than trusted, so an unknown key is a 404 instead of
        # an empty list that reads as "this file is coupled to nothing".
        try:
            root = get_node(repository_id, key)
        except NodeNotFound as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"No graph node {key!r} in this repository. "
                "Has it been parsed, and is the key from /graph/search?",
            ) from exc

    items = coupled_files(repository_id, key=key, min_commits=min_commits, limit=limit)
    return CoChangeResponse(root=root, items=items, total=len(items))


def _require_repository(db: Session, repository_id: uuid.UUID) -> Repository:
    repository = db.get(Repository, repository_id)
    if repository is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No repository {repository_id}")
    return repository
