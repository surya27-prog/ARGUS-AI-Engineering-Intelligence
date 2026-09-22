"""Qdrant client lifecycle and collection bootstrap.

The third store, alongside `app.core.database` (Postgres) and `app.core.graph`
(Neo4j), and built the same way: lazily, so a missing or unreachable Qdrant
cannot be what stops the API starting.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from qdrant_client import QdrantClient, models

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Payload fields that get an index. Qdrant filters unindexed fields by scanning,
# which is fine at fixture scale and quadratic later; `repo_id` in particular is
# on every single query, since one collection holds every repository.
INDEXED_FIELDS: dict[str, models.PayloadSchemaType] = {
    "repo_id": models.PayloadSchemaType.KEYWORD,
    "path": models.PayloadSchemaType.KEYWORD,
    "kind": models.PayloadSchemaType.KEYWORD,
    "symbol_key": models.PayloadSchemaType.KEYWORD,
    "run_id": models.PayloadSchemaType.KEYWORD,
}


@lru_cache
def get_qdrant() -> QdrantClient:
    settings = get_settings()
    return QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key or None,
        timeout=settings.qdrant_timeout_seconds,
    )


def ensure_collection(dimensions: int) -> None:
    """Create the collection if absent, and refuse to use a mismatched one.

    Vector width is fixed at creation time. A collection built for 1536-wide
    vectors silently rejects 768-wide ones on every write, so the mismatch is
    raised here — where the message can name both numbers — rather than as a
    wall of per-point errors during the first embed.
    """
    client = get_qdrant()
    name = get_settings().qdrant_collection

    if client.collection_exists(name):
        existing = client.get_collection(name).config.params.vectors.size
        if existing != dimensions:
            raise ValueError(
                f"Qdrant collection {name!r} stores {existing}-dimension vectors "
                f"but the embedding provider produces {dimensions}. Either set "
                f"EMBEDDING_DIMENSIONS to match the provider and recreate the "
                f"collection, or point QDRANT_COLLECTION at a new name."
            )
    else:
        client.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(
                size=dimensions,
                # Cosine, because embedding models encode meaning in direction
                # rather than magnitude. Qdrant normalises internally, which
                # also absorbs the ~1e-4 the OpenAI vectors are off unit length.
                distance=models.Distance.COSINE,
            ),
        )
        logger.info("Created Qdrant collection %s (%d dimensions)", name, dimensions)

    for field, schema in INDEXED_FIELDS.items():
        try:
            client.create_payload_index(
                collection_name=name, field_name=field, field_schema=schema
            )
        except Exception:  # noqa: BLE001 - already indexed is the common case
            logger.debug("Payload index on %s already present", field)


def check_connectivity() -> str:
    """`up`, or `down: <reason>` — shaped for /health, never raises."""
    try:
        get_qdrant().get_collections()
    except Exception as exc:  # noqa: BLE001 - surfaced in the payload, not raised
        return f"down: {type(exc).__name__}"
    return "up"


def close_qdrant() -> None:
    """Release the client. Called on app shutdown and by tests."""
    if get_qdrant.cache_info().currsize:
        get_qdrant().close()
        get_qdrant.cache_clear()
