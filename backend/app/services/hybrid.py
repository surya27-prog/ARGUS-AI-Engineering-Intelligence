"""Hybrid retrieval: vector search, then expansion through the call graph.

This is the piece that makes ARGUS more than a code search box.

Similarity retrieves what a question *sounds like*. It cannot retrieve what the
answer *depends on*, because that relationship lives in the code's structure,
not in its wording. Asking "how is a request turned into something sendable"
retrieves the three functions that call `PreparedRequest.prepare` and misses
`prepare` itself — the phrasing matches the callers, and nothing in the
embedding space knows the callee is the actual answer.

So: take the vector hits as seeds, walk one or two CALLS hops out in Neo4j, and
pull those symbols' chunks back in. The graph supplies exactly the relationship
the embedding cannot encode, and the chunker's `symbol_key` is what joins them.

Expanded hits are scored below their seed and labelled `source="graph"`, so an
answer can say where a citation came from and a ranking never silently prefers
a neighbour to a direct match.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from uuid import UUID

from qdrant_client import models

from app.core.config import get_settings
from app.core.vectors import get_qdrant
from app.services.graph_queries import neighbours
from app.services.providers import EmbeddingProvider, get_embedding_provider
from app.services.retrieval import DEFAULT_TOP_K, SearchHit, _hit, search

logger = logging.getLogger(__name__)

# How many vector hits to expand from. Expanding all of them floods the result
# with the neighbourhood of a weak match; the strongest few carry the question.
DEFAULT_SEEDS = 5

# A neighbour's score, as a fraction of the seed that reached it, multiplied by
# the edge's own confidence so a guessed call contributes less than a certain
# one. Deliberately below any plausible direct hit: a neighbour is context, not
# a better answer than something the question actually matched.
EXPANSION_DECAY = 0.6

# How much of the result set is reserved for graph-expanded hits.
#
# Score-based merging alone does not work, and measuring it is what showed why:
# expanded scores land around 0.20 while direct hits sit at 0.33, so at a
# realistic top_k every neighbour is cut and hybrid returns exactly what vector
# returned. Reserving slots is the point of the feature — the structural
# neighbourhood is included *because* it is structurally related, not because
# its cosine score happened to compete.
GRAPH_SLOT_RATIO = 0.4


def hybrid_search(
    repository_id: UUID | str,
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    depth: int = 1,
    seeds: int = DEFAULT_SEEDS,
    provider: EmbeddingProvider | None = None,
) -> list[SearchHit]:
    """Vector hits plus their call-graph neighbourhood, ranked together."""
    # Over-fetch: seeds are taken from a wider vector result than the caller
    # asked for, so expansion has something to work with even when the direct
    # matches already fill top_k.
    vector_hits = search(
        repository_id, query, top_k=max(top_k, seeds * 2), provider=provider
    )
    if not vector_hits:
        return []

    seed_keys = [h.symbol_key for h in vector_hits[:seeds] if h.symbol_key]
    if not seed_keys:
        return vector_hits[:top_k]

    try:
        related = neighbours(repository_id, seed_keys, depth=depth)
    except Exception:  # noqa: BLE001 - degrade to vector-only, never fail the search
        logger.exception("Graph expansion failed; returning vector hits only")
        return vector_hits[:top_k]

    # Anything the vector search already returned keeps its own, higher score.
    found = {h.symbol_key for h in vector_hits if h.symbol_key}
    wanted = {r["key"]: r for r in related if r["key"] not in found}
    if not wanted:
        return vector_hits[:top_k]

    # Rank the neighbourhood by its own relevance to the question, not by the
    # seed that reached it. A seed with thirty callees gives all thirty an
    # almost identical derived score, so slots would go to whichever came back
    # first — measurably arbitrary. Re-querying the same vectors, restricted to
    # the neighbour set, asks the question that actually matters: of the
    # symbols structurally related to the good hits, which are about *this*?
    expanded: list[SearchHit] = []
    for score, payload in _rank_within(
        repository_id, query, list(wanted), provider=provider
    ):
        entry = wanted[payload["symbol_key"]]
        expanded.append(
            replace(
                _hit(score * EXPANSION_DECAY * float(entry["confidence"]), payload),
                source="graph",
                hops=int(entry["hops"]),
                via=entry["via_key"],
            )
        )

    # Reserve slots rather than merging on score alone — see GRAPH_SLOT_RATIO.
    graph_slots = min(len(expanded), max(1, int(top_k * GRAPH_SLOT_RATIO)))
    expanded.sort(key=lambda h: h.score, reverse=True)
    kept_graph = expanded[:graph_slots]
    kept_vector = vector_hits[: top_k - len(kept_graph)]

    merged = sorted(kept_vector + kept_graph, key=lambda h: h.score, reverse=True)
    logger.info(
        "Hybrid: %d vector hits, %d neighbours found, %d kept",
        len(vector_hits),
        len(expanded),
        len(kept_graph),
    )
    return merged


def _rank_within(
    repository_id: UUID | str,
    query: str,
    keys: list[str],
    *,
    provider: EmbeddingProvider | None,
) -> list[tuple[float, dict]]:
    """The neighbour chunks, ordered by similarity to the query.

    `part == 0` keeps one chunk per split symbol — the part holding the
    signature and docstring, which is what explains the symbol.
    """
    if not keys:
        return []

    settings = get_settings()
    client = get_qdrant()
    if not client.collection_exists(settings.qdrant_collection):
        return []

    provider = provider or get_embedding_provider()
    points = client.query_points(
        collection_name=settings.qdrant_collection,
        query=provider.embed_one(query),
        query_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="repo_id", match=models.MatchValue(value=str(repository_id))
                ),
                models.FieldCondition(key="symbol_key", match=models.MatchAny(any=keys)),
                models.FieldCondition(key="part", match=models.MatchValue(value=0)),
            ]
        ),
        limit=len(keys),
        with_payload=True,
    ).points
    return [(p.score, p.payload) for p in points]
