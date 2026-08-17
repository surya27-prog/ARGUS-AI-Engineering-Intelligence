"""Semantic search endpoint."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Repository
from app.services.providers import ProviderError
from app.services.retrieval import DEFAULT_TOP_K, MAX_TOP_K, search

router = APIRouter(prefix="/repos/{repository_id}", tags=["search"])


class SearchHitOut(BaseModel):
    score: float
    path: str
    kind: str
    line_start: int
    line_end: int
    text: str
    qualname: str | None = None
    name: str | None = None
    module: str | None = None
    symbol_key: str | None = Field(default=None, description="Neo4j node key, if a symbol")
    citation: str


class SearchResponse(BaseModel):
    query: str
    items: list[SearchHitOut]
    total: int


@router.get("/search", response_model=SearchResponse)
def semantic_search(
    repository_id: uuid.UUID,
    db: Session = Depends(get_db),
    q: str = Query(min_length=1, description="A question about the codebase"),
    top_k: int = Query(default=DEFAULT_TOP_K, ge=1, le=MAX_TOP_K),
    kind: list[str] | None = Query(default=None, description="function | method | class | module"),
) -> SearchResponse:
    if db.get(Repository, repository_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No repository {repository_id}")

    try:
        hits = search(repository_id, q, top_k=top_k, kinds=kind)
    except ProviderError as exc:
        # No embedding provider configured is a 503, not a 500: the repository
        # is fine, the feature just is not available in this deployment.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    items = [SearchHitOut(**vars(h), citation=h.citation) for h in hits]
    return SearchResponse(query=q, items=items, total=len(items))
