"""Blast radius: what breaks if a symbol changes.

`dependents` in `graph_queries` already answers "what reaches this node". Impact
analysis is that traversal turned into an *answer*: the affected set ranked by
how much a change to the target should worry you, each entry carrying the path
the change would travel along so a reviewer can check the claim instead of
trusting it.

Three things separate this from a plain reverse traversal:

**Per-hop decay.** A direct caller breaks when you change a signature. Its
caller's caller usually does not — the intermediate absorbs the change. So every
hop multiplies the score by `DEFAULT_DECAY`, and the ranking says "look here
first" rather than "these forty nodes are equally implicated".

**Score, not hop count, picks a node's representative path.** The same symbol is
often reachable several ways, and the shortest is not always the strongest: one
guessed call at 0.5 confidence is a weaker story than two certain hops at 0.95
(0.5 against 0.9025 · 0.6 = 0.54). Scoring inside the query and taking the best
row per node means the path shown is the path the score came from.

**The route is returned.** `dependents` returns edge *types*; a blast radius has
to be defensible, and "get_settings → run → handler" is the defence. It is
called `route` rather than `path` because a node already has a `path`: the file
it lives in.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.core.graph import graph_session
from app.services.graph_queries import (
    MAX_DEPTH,
    NodeNotFound,
    get_node,
    serialize_node,
)

logger = logging.getLogger(__name__)

# Further than the general traversal cap is pointless here for a second reason:
# with decay applied, a hop-5 node scores below 0.13 even along certain edges,
# which is noise in a ranked list.
DEFAULT_IMPACT_DEPTH = 3

# What fraction of the risk survives one hop. 0.5 would say a change is half as
# likely to reach the next layer out, which is too pessimistic for a call chain
# that passes values through; 0.8 barely separates hop 1 from hop 3 and the
# ranking stops being useful. 0.6 keeps a certain two-hop path (0.57) above a
# shaky one-hop guess (0.5), which is the ordering a reviewer actually wants.
DEFAULT_DECAY = 0.6

# Same reasoning as graph_queries: containment is structure, not dependency.
# Changing a function does not break its sibling because they share a file.
_TRAVERSED = "CALLS|IMPORTS"


@dataclass(frozen=True, slots=True)
class ImpactResult:
    root: dict[str, Any]
    items: list[dict[str, Any]]
    truncated: bool
    summary: dict[str, Any]


def impact(
    repository_id: UUID | str,
    key: str,
    *,
    depth: int = DEFAULT_IMPACT_DEPTH,
    limit: int = 200,
    decay: float = DEFAULT_DECAY,
) -> ImpactResult:
    """The ranked blast radius of a change to `key`. Raises `NodeNotFound`."""
    if not 1 <= depth <= MAX_DEPTH:
        raise ValueError(f"depth must be between 1 and {MAX_DEPTH}")
    if not 0 < decay <= 1:
        raise ValueError("decay must be greater than 0 and at most 1")

    repo_id = str(repository_id)
    root = get_node(repo_id, key)

    # Interpolated because Cypher rejects a parameter as a variable-length
    # bound; `depth` is an already range-checked int, so nothing
    # attacker-controlled reaches the query text. `decay` is a real parameter.
    cypher = f"""
    MATCH (start {{key: $key}})
    WHERE start.repo_id = $repo_id
    MATCH path = (other)-[:{_TRAVERSED}*1..{depth}]->(start)
    WHERE other.repo_id = $repo_id AND other.key <> $key
    WITH other,
         length(path) AS hops,
         reduce(c = 1.0, r IN relationships(path) | c * coalesce(r.confidence, 1.0)) AS confidence,
         [r IN relationships(path) | type(r)] AS via,
         // Reversed so the route reads the way the change travels: target
         // first, affected node last. The pattern is matched the other way
         // round because that is the direction the edges point.
         reverse([n IN nodes(path) | n.key]) AS route
    WITH other, hops, confidence, via, route,
         confidence * ($decay ^ (hops - 1)) AS score
    ORDER BY score DESC, hops ASC
    // One row per affected node, described by its highest-scoring route — see
    // the module docstring for why that is not simply the shortest one.
    WITH other, collect({{
        hops: hops, confidence: confidence, via: via, route: route, score: score
    }})[0] AS best
    RETURN other, best.hops AS hops, best.confidence AS confidence,
           best.via AS via, best.route AS route, best.score AS score
    ORDER BY score DESC, hops ASC
    LIMIT $limit
    """

    with graph_session() as session:
        records = list(
            session.run(
                cypher,
                key=key,
                repo_id=repo_id,
                decay=decay,
                # One over, so "there is more" is a fact rather than a guess.
                limit=limit + 1,
            )
        )

    truncated = len(records) > limit
    records = records[:limit]

    items = [
        serialize_node(record["other"])
        | {
            "hops": record["hops"],
            "confidence": round(record["confidence"], 4),
            "score": round(record["score"], 4),
            "via": record["via"],
            "route": record["route"],
        }
        for record in records
    ]

    logger.info(
        "Impact of %s: %d affected node(s) within %d hop(s)%s",
        key,
        len(items),
        depth,
        " (capped)" if truncated else "",
    )
    return ImpactResult(
        root=root,
        items=items,
        truncated=truncated,
        summary=summarize(items, depth=depth, decay=decay),
    )


def summarize(items: list[dict[str, Any]], *, depth: int, decay: float) -> dict[str, Any]:
    """Counts a UI can show without walking the item list itself.

    Computed over the returned items, so a truncated result summarises what was
    returned rather than claiming to describe the whole radius — which is why
    `truncated` sits next to it in the response.
    """
    by_hop = Counter(str(item["hops"]) for item in items)
    by_type = Counter(item["type"] for item in items)
    return {
        "total": len(items),
        "depth": depth,
        "decay": decay,
        "direct": by_hop.get("1", 0),
        "max_hops": max((item["hops"] for item in items), default=0),
        "top_score": max((item["score"] for item in items), default=0.0),
        "by_hop": dict(sorted(by_hop.items())),
        "by_type": dict(by_type.most_common()),
    }


__all__ = [
    "DEFAULT_DECAY",
    "DEFAULT_IMPACT_DEPTH",
    "ImpactResult",
    "NodeNotFound",
    "impact",
]
