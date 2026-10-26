"""Semantic search endpoint."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.ratelimit import require_search_quota
from app.models import Repository
from app.services.hybrid import hybrid_search
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
    source: str = Field(description="vector | graph")
    hops: int = Field(description="CALLS hops from the seed, 0 for a direct hit")
    via: str | None = Field(default=None, description="the seed this was expanded from")


class SearchResponse(BaseModel):
    query: str
    mode: str
    items: list[SearchHitOut]
    total: int
    from_graph: int = Field(description="hits similarity alone would not have found")


@router.get(
    "/search",
    response_model=SearchResponse,
    dependencies=[Depends(require_search_quota)],
)
def semantic_search(
    repository_id: uuid.UUID,
    db: Session = Depends(get_db),
    q: str = Query(min_length=1, description="A question about the codebase"),
    top_k: int = Query(default=DEFAULT_TOP_K, ge=1, le=MAX_TOP_K),
    kind: list[str] | None = Query(default=None, description="function | method | class | module"),
    mode: str = Query(default="hybrid", pattern="^(vector|hybrid)$"),
    depth: int = Query(default=1, ge=1, le=2, description="CALLS hops, hybrid only"),
) -> SearchResponse:
    if db.get(Repository, repository_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No repository {repository_id}")

    try:
        if mode == "hybrid" and not kind:
            hits = hybrid_search(repository_id, q, top_k=top_k, depth=depth)
        else:
            # A kind filter and graph expansion contradict each other: the
            # neighbours of a function are usually other kinds.
            hits = search(repository_id, q, top_k=top_k, kinds=kind)
            mode = "vector"
    except ProviderError as exc:
        # No embedding provider configured is a 503, not a 500: the repository
        # is fine, the feature just is not available in this deployment.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    items = [SearchHitOut(**vars(h), citation=h.citation) for h in hits]
    return SearchResponse(
        query=q,
        mode=mode,
        items=items,
        total=len(items),
        from_graph=sum(1 for h in hits if h.source == "graph"),
    )
