"""Read queries over the knowledge graph.

The write side is `graph_writer`; this is everything the API reads back. The
Cypher started as the draft written into the schema doc on Day 1, which is what
made the one real problem cheap to find: a variable-length pattern cannot take
its bound from a parameter, so `*1..$depth` does not parse. Depth is therefore
interpolated as a literal — and because it is, it is validated as a bounded int
at the router before it ever reaches this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from neo4j.graph import Node

from app.core.graph import graph_session
from app.services.graph_keys import repo_key

logger = logging.getLogger(__name__)

# Traversal depth beyond this stops being an answer and starts being the whole
# graph: every extra hop multiplies the path count, and nobody reads hop six.
MAX_DEPTH = 5
DEFAULT_DEPTH = 2

# Ordered by specificity — a node carries exactly one of these in practice, but
# the order makes the mapping deterministic if that ever changes.
_NODE_LABELS = ("Repo", "File", "Class", "Function", "Module")

# The edge types a dependency traversal follows. CONTAINS is deliberately not
# among them: containment is structure, not dependency, and including it would
# make every symbol in a file "depend on" every sibling.
_TRAVERSED = "CALLS|IMPORTS"


class NodeNotFound(LookupError):
    """No node with that key in this repository."""


@dataclass(frozen=True, slots=True)
class GraphView:
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    truncated: bool


def node_type(labels: frozenset[str] | set[str]) -> str:
    return next((label for label in _NODE_LABELS if label in labels), "Unknown")


def serialize_node(node: Node) -> dict[str, Any]:
    """A Neo4j node as a flat JSON-able dict.

    `display` is what a UI puts on the node: a file shows its path, a symbol its
    qualname, an external package its dotted name.
    """
    props = dict(node)
    kind = node_type(set(node.labels))
    display = (
        props.get("path")
        or props.get("qualname")
        or props.get("dotted_name")
        or props.get("name")
        or props.get("key", "")
    )
    return {
        "key": props.get("key", ""),
        "type": kind,
        "display": display,
        "name": props.get("name"),
        "path": props.get("path"),
        "module": props.get("module"),
        "qualname": props.get("qualname"),
        "kind": props.get("kind"),
        "line_start": props.get("line_start"),
        "line_end": props.get("line_end"),
        "is_external": props.get("is_external"),
        "unresolved_calls": props.get("unresolved_calls"),
    }


def get_node(repository_id: UUID | str, key: str) -> dict[str, Any]:
    """One node by key, scoped to the repository. Raises `NodeNotFound`."""
    repo_id = str(repository_id)
    with graph_session() as session:
        record = session.run(
            "MATCH (n {key: $key}) WHERE n.repo_id = $repo_id RETURN n LIMIT 1",
            key=key,
            repo_id=repo_id,
        ).single()
    if record is None:
        raise NodeNotFound(key)
    return serialize_node(record["n"])


def search_nodes(
    repository_id: UUID | str, query: str, *, limit: int = 25
) -> list[dict[str, Any]]:
    """Find nodes by name, path or qualname.

    The dependency endpoints take a node key, and a key is not something anyone
    types. This is how a caller gets one.
    """
    repo_id = str(repository_id)
    pattern = f"(?i).*{_escape_regex(query)}.*"
    with graph_session() as session:
        records = session.run(
            """
            MATCH (n {repo_id: $repo_id})
            WHERE n.qualname =~ $pattern OR n.path =~ $pattern
               OR n.name =~ $pattern OR n.dotted_name =~ $pattern
            RETURN n
            ORDER BY size(coalesce(n.qualname, n.path, n.name, '')) ASC
            LIMIT $limit
            """,
            repo_id=repo_id,
            pattern=pattern,
            limit=limit,
        )
        # Iterated rather than `.data()`: that helper flattens a Node into a
        # plain dict of its properties, which drops the labels the node type is
        # read from.
        return [serialize_node(record["n"]) for record in records]


def repository_graph(
    repository_id: UUID | str, *, view: str = "files", limit: int = 500
) -> GraphView:
    """The repository's structure, capped so a browser can render it.

    Two views rather than one blob: `files` is the import graph between files,
    `calls` is the call graph between symbols. Returning both at once would
    exceed anything Cytoscape can lay out on a real repository, and the two
    answer different questions anyway.
    """
    repo_id = str(repository_id)
    cypher = _FILE_VIEW if view == "files" else _CALL_VIEW

    with graph_session() as session:
        records = list(
            session.run(
                cypher,
                repo_key=repo_key(repo_id),
                repo_id=repo_id,
                # One over the limit, so "there is more" is a fact rather than a
                # guess from a full page.
                limit=limit + 1,
            )
        )

    truncated = len(records) > limit
    records = records[:limit]

    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    for record in records:
        for node in (record["source"], record["target"]):
            if node is not None:
                serialized = serialize_node(node)
                nodes[serialized["key"]] = serialized
        if record["edge"] is not None:
            edges.append(_serialize_edge(record))

    return GraphView(nodes=list(nodes.values()), edges=edges, truncated=truncated)


_FILE_VIEW = """
MATCH (:Repo {key: $repo_key})-[:CONTAINS]->(source:File)
OPTIONAL MATCH (source)-[edge:IMPORTS]->(target)
RETURN source, edge, target,
       startNode(edge).key AS from_key, endNode(edge).key AS to_key
LIMIT $limit
"""

_CALL_VIEW = """
MATCH (source:Function {repo_id: $repo_id})-[edge:CALLS]->(target)
RETURN source, edge, target,
       startNode(edge).key AS from_key, endNode(edge).key AS to_key
LIMIT $limit
"""


def _serialize_edge(record: dict[str, Any]) -> dict[str, Any]:
    edge = record["edge"]
    props = dict(edge)
    return {
        "type": edge.type,
        "source": record["from_key"],
        "target": record["to_key"],
        "resolution": props.get("resolution"),
        "confidence": props.get("confidence"),
        "count": props.get("count"),
        "line": props.get("line"),
    }


def dependencies(
    repository_id: UUID | str, key: str, *, depth: int = DEFAULT_DEPTH, limit: int = 200
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """What this node depends on, up to `depth` hops away."""
    return _traverse(repository_id, key, depth=depth, limit=limit, reverse=False)


def dependents(
    repository_id: UUID | str, key: str, *, depth: int = DEFAULT_DEPTH, limit: int = 200
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """What depends on this node — the blast radius Week 4 builds on.

    The same traversal as `dependencies` with the arrow flipped, which is
    exactly why these edges are directed.
    """
    return _traverse(repository_id, key, depth=depth, limit=limit, reverse=True)


def _traverse(
    repository_id: UUID | str,
    key: str,
    *,
    depth: int,
    limit: int,
    reverse: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    repo_id = str(repository_id)
    root = get_node(repo_id, key)

    if not 1 <= depth <= MAX_DEPTH:
        raise ValueError(f"depth must be between 1 and {MAX_DEPTH}")

    # Interpolated because Cypher rejects a parameter as a variable-length
    # bound. `depth` is an int already range-checked above, so nothing
    # attacker-controlled reaches the query text.
    pattern = (
        f"(other)-[:{_TRAVERSED}*1..{depth}]->(start)"
        if reverse
        else f"(start)-[:{_TRAVERSED}*1..{depth}]->(other)"
    )

    cypher = f"""
    MATCH (start {{key: $key}})
    WHERE start.repo_id = $repo_id
    MATCH path = {pattern}
    WHERE other.repo_id = $repo_id AND other.key <> $key
    WITH other,
         length(path) AS hops,
         reduce(c = 1.0, r IN relationships(path) | c * coalesce(r.confidence, 1.0)) AS confidence,
         [r IN relationships(path) | type(r)] AS via
    ORDER BY hops ASC, confidence DESC
    // One row per node, not per path: the same symbol reachable five ways is
    // one dependency, described by its strongest shortest path.
    WITH other, collect({{hops: hops, confidence: confidence, via: via}})[0] AS best
    RETURN other, best.hops AS hops, best.confidence AS confidence, best.via AS via
    ORDER BY hops ASC, confidence DESC
    LIMIT $limit
    """

    with graph_session() as session:
        records = list(session.run(cypher, key=key, repo_id=repo_id, limit=limit))

    items = [
        serialize_node(record["other"])
        | {
            "hops": record["hops"],
            "confidence": round(record["confidence"], 4),
            "via": record["via"],
        }
        for record in records
    ]
    return root, items


def _escape_regex(value: str) -> str:
    """Neutralise regex metacharacters in user input before it reaches `=~`."""
    return "".join(f"\\{c}" if c in ".^$*+?()[]{}|\\/" else c for c in value)
