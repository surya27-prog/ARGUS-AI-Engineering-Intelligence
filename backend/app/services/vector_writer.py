"""Embeds a repository's chunks and writes them to Qdrant.

The vector twin of `graph_writer`, and it borrows that module's re-parse
strategy wholesale: every point carries the parse run's `run_id`, and the run
ends by deleting the points it did not stamp. Upserting alone would leave the
vectors of a deleted function in the index forever, answering questions about
code that no longer exists — the same failure `MERGE` alone has in the graph.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from qdrant_client import models

from app.core.config import get_settings
from app.core.vectors import ensure_collection, get_qdrant
from app.services.chunking import CodeChunk, chunk_repo
from app.services.providers import EmbeddingProvider, get_embedding_provider
from parser import ParsedRepo

logger = logging.getLogger(__name__)

# Chunks per embedding call. The provider batches internally too; this bounds
# how much sits in memory and how much is lost if a call fails partway.
EMBED_BATCH = 128
# Points per upsert. Qdrant accepts far more, but a smaller body keeps the
# request well inside any proxy's limit.
UPSERT_BATCH = 256


@dataclass(frozen=True, slots=True)
class VectorWriteResult:
    run_id: str
    repo_id: str
    chunks_written: int
    points_deleted: int
    embedding_model: str
    dimensions: int
    input_tokens: int

    @property
    def estimated_cost_usd(self) -> float:
        """text-embedding-3-small is $0.02 per million tokens.

        Approximate and provider-specific, but the number people ask for first
        when a pipeline embeds a whole repository on every parse.
        """
        return self.input_tokens / 1_000_000 * 0.02


def write_repo_vectors(
    repository_id: UUID | str,
    parsed: ParsedRepo,
    *,
    run_id: str | None = None,
    provider: EmbeddingProvider | None = None,
) -> VectorWriteResult:
    """Chunk, embed and upsert a repository, then sweep the run."""
    repo_id = str(repository_id)
    run = run_id or str(uuid4())
    provider = provider or get_embedding_provider()

    ensure_collection(provider.dimensions)
    client = get_qdrant()
    collection = get_settings().qdrant_collection

    result = chunk_repo(repo_id, parsed)
    written = 0
    tokens = 0

    for batch in _batched(result.chunks, EMBED_BATCH):
        embedded = provider.embed([chunk.text for chunk in batch])
        tokens += embedded.input_tokens

        points = [
            models.PointStruct(
                id=str(chunk.id),
                vector=vector,
                payload=_payload(chunk, run, provider),
            )
            for chunk, vector in zip(batch, embedded.vectors, strict=True)
        ]
        for window in _batched(points, UPSERT_BATCH):
            client.upsert(collection_name=collection, points=window, wait=True)
            written += len(window)

    deleted = sweep_run(repo_id, run)

    outcome = VectorWriteResult(
        run_id=run,
        repo_id=repo_id,
        chunks_written=written,
        points_deleted=deleted,
        embedding_model=provider.model,
        dimensions=provider.dimensions,
        input_tokens=tokens,
    )
    logger.info(
        "Embedded repo %s: %d chunks %s, %d tokens (~$%.4f); swept %d stale points",
        repo_id,
        written,
        result.by_kind(),
        tokens,
        outcome.estimated_cost_usd,
        deleted,
    )
    return outcome


def _payload(chunk: CodeChunk, run: str, provider: EmbeddingProvider) -> dict:
    """What comes back with a search hit.

    `text` is stored rather than re-read from disk: the workspace is transient,
    and an answer that cites a chunk has to be able to show it. `symbol_key` is
    the join into Neo4j that Thursday's hybrid retrieval walks.
    """
    return {
        "repo_id": chunk.repo_id,
        "path": chunk.path,
        "module": chunk.module,
        "qualname": chunk.qualname,
        "name": chunk.name,
        "kind": chunk.kind,
        "line_start": chunk.line_start,
        "line_end": chunk.line_end,
        "symbol_key": chunk.symbol_key,
        "part": chunk.part,
        "part_count": chunk.part_count,
        "text": chunk.text,
        "sha256": chunk.sha256,
        "run_id": run,
        # Stamped per point so a collection that outlives a provider switch can
        # be audited rather than silently mixing two vector spaces.
        "embedding_model": provider.model,
    }


def _repo_filter(repo_id: str) -> models.Filter:
    return models.Filter(
        must=[models.FieldCondition(key="repo_id", match=models.MatchValue(value=repo_id))]
    )


def sweep_run(repo_id: str, run: str) -> int:
    """Delete this repo's points that the run did not stamp."""
    client = get_qdrant()
    collection = get_settings().qdrant_collection

    stale = models.Filter(
        must=[models.FieldCondition(key="repo_id", match=models.MatchValue(value=repo_id))],
        must_not=[models.FieldCondition(key="run_id", match=models.MatchValue(value=run))],
    )
    before = count_points(repo_id)
    client.delete(
        collection_name=collection,
        points_selector=models.FilterSelector(filter=stale),
        wait=True,
    )
    return max(0, before - count_points(repo_id))


def count_points(repo_id: UUID | str) -> int:
    """How many points this repository has in the collection."""
    settings = get_settings()
    client = get_qdrant()
    if not client.collection_exists(settings.qdrant_collection):
        return 0
    return client.count(
        collection_name=settings.qdrant_collection,
        count_filter=_repo_filter(str(repo_id)),
        exact=True,
    ).count


def delete_repo_vectors(repository_id: UUID | str) -> int:
    """Remove a repository from the collection. Returns points deleted."""
    settings = get_settings()
    client = get_qdrant()
    if not client.collection_exists(settings.qdrant_collection):
        return 0

    repo_id = str(repository_id)
    before = count_points(repo_id)
    client.delete(
        collection_name=settings.qdrant_collection,
        points_selector=models.FilterSelector(filter=_repo_filter(repo_id)),
        wait=True,
    )
    return before


def _batched(items: Sequence, size: int) -> Iterator[list]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])
