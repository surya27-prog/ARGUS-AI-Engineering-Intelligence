"""Graph query endpoints.

The node key is a query parameter rather than a path segment, which is a
deliberate deviation from the timeline's `/dependencies/{node}`. Keys look like
`file:{uuid}:app/core/config.py` — the slashes would need double-encoding to
survive a path segment, and every client would have to remember to do it.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.cache import graph_cache
from app.core.database import get_db
from app.models import Repository
from app.schemas.graph import (
    GraphResponse,
    NodeSearchResponse,
    TraversalResponse,
)
from app.services.graph_queries import (
    DEFAULT_DEPTH,
    MAX_DEPTH,
    NodeNotFound,
    dependencies,
    dependents,
    get_node,
    repository_graph,
    search_nodes,
)

router = APIRouter(prefix="/repos/{repository_id}", tags=["graph"])


@router.get("/graph", response_model=GraphResponse)
def get_graph(
    repository_id: uuid.UUID,
    db: Session = Depends(get_db),
    view: str = Query(default="files", pattern="^(files|calls)$"),
    limit: int = Query(default=500, ge=1, le=5000),
) -> GraphResponse:
    """The repository's structure, capped for rendering."""
    repository = _require_repository(db, repository_id)
    # Cached per (view, limit): the graph page switches between the two views and
    # four caps, and each combination is a fresh whole-graph read otherwise.
    result, _ = graph_cache.get_or_compute(
        repository_id,
        repository.parsed_at,
        (view, limit),
        lambda: repository_graph(repository_id, view=view, limit=limit),
    )
    return GraphResponse(
        view=view,
        nodes=result.nodes,
        edges=result.edges,
        truncated=result.truncated,
    )


@router.get("/graph/search", response_model=NodeSearchResponse)
def search(
    repository_id: uuid.UUID,
    db: Session = Depends(get_db),
    q: str = Query(min_length=1, description="Substring of a name, path or qualname"),
    limit: int = Query(default=25, ge=1, le=200),
) -> NodeSearchResponse:
    """Find a node's key. The traversal endpoints take one, and nobody types a key."""
    _require_repository(db, repository_id)
    items = search_nodes(repository_id, q, limit=limit)
    return NodeSearchResponse(query=q, items=items, total=len(items))


@router.get("/dependencies", response_model=TraversalResponse)
def get_dependencies(
    repository_id: uuid.UUID,
    db: Session = Depends(get_db),
    key: str = Query(description="Node key, from /graph/search"),
    depth: int = Query(default=DEFAULT_DEPTH, ge=1, le=MAX_DEPTH),
    limit: int = Query(default=200, ge=1, le=1000),
) -> TraversalResponse:
    """What this node needs, downstream, depth-limited."""
    _require_repository(db, repository_id)
    root, items = _traverse(dependencies, repository_id, key, depth, limit)
    return TraversalResponse(
        root=root, depth=depth, direction="dependencies", items=items, total=len(items)
    )


@router.get("/dependents", response_model=TraversalResponse)
def get_dependents(
    repository_id: uuid.UUID,
    db: Session = Depends(get_db),
    key: str = Query(description="Node key, from /graph/search"),
    depth: int = Query(default=DEFAULT_DEPTH, ge=1, le=MAX_DEPTH),
    limit: int = Query(default=200, ge=1, le=1000),
) -> TraversalResponse:
    """What depends on this node — the blast radius Week 4 builds on."""
    _require_repository(db, repository_id)
    root, items = _traverse(dependents, repository_id, key, depth, limit)
    return TraversalResponse(
        root=root, depth=depth, direction="dependents", items=items, total=len(items)
    )


@router.get("/graph/node", response_model=None)
def get_single_node(
    repository_id: uuid.UUID,
    db: Session = Depends(get_db),
    key: str = Query(description="Node key"),
) -> dict:
    _require_repository(db, repository_id)
    try:
        return get_node(repository_id, key)
    except NodeNotFound as exc:
        raise _no_such_node(key) from exc


def _traverse(fn, repository_id: uuid.UUID, key: str, depth: int, limit: int):
    try:
        return fn(repository_id, key, depth=depth, limit=limit)
    except NodeNotFound as exc:
        raise _no_such_node(key) from exc


def _no_such_node(key: str) -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        f"No graph node {key!r} in this repository. "
        "Has it been parsed, and is the key from /graph/search?",
    )


def _require_repository(db: Session, repository_id: uuid.UUID) -> Repository:
    repository = db.get(Repository, repository_id)
    if repository is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No repository {repository_id}")
    return repository
