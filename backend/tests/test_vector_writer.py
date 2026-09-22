"""Vector write tests.

These talk to the real Qdrant from `docker compose`, but embed with the offline
`hash` provider — the plumbing under test (chunk identity, payloads, batching,
stamp-and-sweep) is provider-independent, and running it through a paid API
would make the suite cost money and need a key in CI.

Each test writes under its own repo_id and deletes it afterwards, so they share
one collection without colliding.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest
from qdrant_client import models

from app.core.config import Settings, get_settings
from app.core.vectors import ensure_collection, get_qdrant
from app.services.providers.stub import HashEmbeddingProvider
from app.services.vector_writer import (
    count_points,
    delete_repo_vectors,
    write_repo_vectors,
)
from parser import analyze_repo

# Small, so the tests stay fast — the width is irrelevant to what they assert.
DIMENSIONS = 64


@pytest.fixture(scope="module")
def provider() -> HashEmbeddingProvider:
    return HashEmbeddingProvider(Settings(embedding_dimensions=DIMENSIONS))


@pytest.fixture(scope="module", autouse=True)
def collection(provider):
    """A collection of this module's width, separate from the real one."""
    settings = get_settings()
    original = settings.qdrant_collection
    settings.qdrant_collection = "argus_test_chunks"
    client = get_qdrant()
    if client.collection_exists(settings.qdrant_collection):
        client.delete_collection(settings.qdrant_collection)
    ensure_collection(provider.dimensions)
    try:
        yield settings.qdrant_collection
    finally:
        client.delete_collection(settings.qdrant_collection)
        settings.qdrant_collection = original


@pytest.fixture
def repo_id() -> Generator[str, None, None]:
    identifier = str(uuid4())
    try:
        yield identifier
    finally:
        delete_repo_vectors(identifier)


@pytest.fixture(scope="module")
def parsed(tmp_path_factory):
    """The parser package — a real repository, not a synthetic fixture."""
    root = Path(__file__).resolve().parents[2] / "parser" / "parser"
    workspace = tmp_path_factory.mktemp("vectors")
    return analyze_repo(str(root), workspace_dir=str(workspace), force=True)


def _payloads(repo_id: str, collection: str, limit: int = 500) -> list[dict]:
    points, _ = get_qdrant().scroll(
        collection_name=collection,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="repo_id", match=models.MatchValue(value=repo_id))]
        ),
        limit=limit,
        with_payload=True,
    )
    return [p.payload for p in points]


def test_a_repository_is_embedded_and_counted(repo_id, parsed, provider):
    result = write_repo_vectors(repo_id, parsed, provider=provider)

    assert result.chunks_written > 0
    assert result.dimensions == DIMENSIONS
    assert result.embedding_model == "hash-embed"
    assert count_points(repo_id) == result.chunks_written


def test_every_point_carries_what_a_citation_needs(repo_id, parsed, provider, collection):
    write_repo_vectors(repo_id, parsed, provider=provider)
    payload = _payloads(repo_id, collection)[0]

    # The text is stored rather than re-read: the workspace is transient, and
    # an answer citing a chunk has to be able to show it.
    for field in ("repo_id", "path", "kind", "line_start", "line_end", "text", "run_id"):
        assert field in payload, field
    assert payload["repo_id"] == repo_id
    assert payload["text"].startswith("File: ")


def test_symbol_points_carry_the_graph_key(repo_id, parsed, provider, collection):
    """The join Thursday's hybrid retrieval walks into Neo4j."""
    payloads = _payloads(repo_id, collection)
    symbol_points = [p for p in payloads if p["kind"] in ("function", "method", "class")]

    write_repo_vectors(repo_id, parsed, provider=provider)
    symbol_points = [
        p for p in _payloads(repo_id, collection) if p["kind"] in ("function", "method", "class")
    ]
    assert symbol_points
    assert all(p["symbol_key"].startswith(f"sym:{repo_id}:") for p in symbol_points)


def test_module_points_have_no_graph_key(repo_id, parsed, provider, collection):
    write_repo_vectors(repo_id, parsed, provider=provider)
    module_points = [p for p in _payloads(repo_id, collection) if p["kind"] == "module"]
    assert module_points
    assert all(p["symbol_key"] is None for p in module_points)


def test_re_embedding_replaces_rather_than_duplicates(repo_id, parsed, provider):
    """The vector twin of the graph's parse-twice criterion."""
    first = write_repo_vectors(repo_id, parsed, provider=provider)
    after_first = count_points(repo_id)

    second = write_repo_vectors(repo_id, parsed, provider=provider)

    assert count_points(repo_id) == after_first
    assert second.chunks_written == first.chunks_written
    # A fresh run stamps every point, so there is nothing stale to sweep.
    assert second.points_deleted == 0


def test_the_sweep_removes_vectors_of_code_that_disappeared(repo_id, parsed, provider):
    """Otherwise deleted functions keep answering questions about themselves."""
    write_repo_vectors(repo_id, parsed, provider=provider)
    full = count_points(repo_id)

    trimmed = type(parsed)(inventory=parsed.inventory, files=parsed.files[:2])
    result = write_repo_vectors(repo_id, trimmed, provider=provider)

    assert count_points(repo_id) < full
    assert result.points_deleted > 0
    assert count_points(repo_id) == result.chunks_written


def test_two_repositories_do_not_share_points(parsed, provider):
    first, second = str(uuid4()), str(uuid4())
    try:
        write_repo_vectors(first, parsed, provider=provider)
        write_repo_vectors(second, parsed, provider=provider)
        assert count_points(first) == count_points(second) > 0
    finally:
        delete_repo_vectors(first)
        delete_repo_vectors(second)


def test_deleting_a_repository_clears_its_points(parsed, provider):
    repo_id = str(uuid4())
    write_repo_vectors(repo_id, parsed, provider=provider)
    assert count_points(repo_id) > 0

    assert delete_repo_vectors(repo_id) > 0
    assert count_points(repo_id) == 0


def test_a_collection_of_the_wrong_width_is_refused(collection):
    """Qdrant would otherwise reject every write, one point at a time."""
    with pytest.raises(ValueError, match="dimension"):
        ensure_collection(DIMENSIONS + 1)


def test_counting_an_unknown_repository_is_zero_not_an_error():
    assert count_points(str(uuid4())) == 0
