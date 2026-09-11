"""Semantic search over a repository's embedded chunks.

Embeds the question with the same provider that embedded the code — a query
vector from a different model lands in a different space and ranks noise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from qdrant_client import models

from app.core.config import get_settings
from app.core.vectors import get_qdrant
from app.services.providers import EmbeddingProvider, get_embedding_provider

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 8
MAX_TOP_K = 50


@dataclass(frozen=True, slots=True)
class SearchHit:
    score: float
    path: str
    kind: str
    line_start: int
    line_end: int
    text: str
    qualname: str | None = None
    name: str | None = None
    module: str | None = None
    # The Neo4j node key, which hybrid retrieval expands from.
    symbol_key: str | None = None
    # How this hit was found. `vector` came back from the embedding search;
    # `graph` was pulled in by expanding a vector hit's call neighbourhood, and
    # would not have been retrieved by similarity alone.
    source: str = "vector"
    hops: int = 0
    via: str | None = None

    @property
    def citation(self) -> str:
        return f"{self.path}:{self.line_start}"


def search(
    repository_id: UUID | str,
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    kinds: list[str] | None = None,
    provider: EmbeddingProvider | None = None,
) -> list[SearchHit]:
    """Nearest chunks to `query`, scoped to one repository."""
    if not query.strip():
        return []

    provider = provider or get_embedding_provider()
    settings = get_settings()
    client = get_qdrant()
    if not client.collection_exists(settings.qdrant_collection):
        return []

    must: list[models.FieldCondition] = [
        models.FieldCondition(
            key="repo_id", match=models.MatchValue(value=str(repository_id))
        )
    ]
    if kinds:
        must.append(models.FieldCondition(key="kind", match=models.MatchAny(any=kinds)))

    points = client.query_points(
        collection_name=settings.qdrant_collection,
        query=provider.embed_one(query),
        query_filter=models.Filter(must=must),
        limit=min(top_k, MAX_TOP_K),
        with_payload=True,
    ).points

    return [_hit(point.score, point.payload) for point in points]


def _hit(score: float, payload: dict) -> SearchHit:
    return SearchHit(
        score=score,
        path=payload["path"],
        kind=payload["kind"],
        line_start=payload["line_start"],
        line_end=payload["line_end"],
        text=payload["text"],
        qualname=payload.get("qualname"),
        name=payload.get("name"),
        module=payload.get("module"),
        symbol_key=payload.get("symbol_key"),
    )
