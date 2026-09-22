"""Impact analysis tests.

Like the graph tests, these run against the real Neo4j and Postgres from
`docker compose`. The fixture is a diamond rather than the chain used in
`test_graph_api`: a chain cannot show a node reachable two ways, and choosing
between two paths is the behaviour this day's ranking is built on.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app.models import Repository
from app.services.graph_keys import file_key, symbol_key
from app.services.graph_writer import delete_repo_graph, write_parsed_repo
from app.services.impact import DEFAULT_DECAY, summarize
from parser.models import (
    CallRef,
    FileInfo,
    ImportRef,
    Language,
    ParsedFile,
    ParsedRepo,
    RepoInventory,
    SourceKind,
    Symbol,
    SymbolKind,
)

# A diamond over four files, all pointing at `get_settings`:
#
#             cli.handler ─┐
#                          ├─> service.run ─> config.get_settings
#   worker.tick ─> ────────┘
#
# and `worker.tick` also calls `get_settings` directly, so it is reachable at
# one hop and at two — which is the case the "best path" rule exists for.
CONFIG = "app/core/config.py"
SERVICE = "app/service.py"
CLI = "app/cli.py"
WORKER = "app/worker.py"


def _sym(name: str, path: str, module: str) -> Symbol:
    return Symbol(
        name=name,
        qualname=name,
        kind=SymbolKind.FUNCTION,
        file_path=path,
        line_start=1,
        line_end=9,
        module=module,
    )


def _diamond_repo() -> ParsedRepo:
    files = (
        ParsedFile(
            path=CONFIG,
            module="app.core.config",
            symbols=(_sym("get_settings", CONFIG, "app.core.config"),),
        ),
        ParsedFile(
            path=SERVICE,
            module="app.service",
            symbols=(_sym("run", SERVICE, "app.service"),),
            imports=(
                ImportRef(module="app.core.config", name="get_settings", line=1, is_from=True),
            ),
            calls=(CallRef(callee="get_settings", line=5, caller="run", file_path=SERVICE),),
        ),
        ParsedFile(
            path=CLI,
            module="app.cli",
            symbols=(_sym("handler", CLI, "app.cli"),),
            imports=(ImportRef(module="app.service", name="run", line=1, is_from=True),),
            calls=(CallRef(callee="run", line=6, caller="handler", file_path=CLI),),
        ),
        ParsedFile(
            path=WORKER,
            module="app.worker",
            symbols=(_sym("tick", WORKER, "app.worker"),),
            imports=(
                ImportRef(module="app.service", name="run", line=1, is_from=True),
                ImportRef(module="app.core.config", name="get_settings", line=2, is_from=True),
            ),
            calls=(
                CallRef(callee="run", line=6, caller="tick", file_path=WORKER),
                CallRef(callee="get_settings", line=7, caller="tick", file_path=WORKER),
            ),
        ),
    )
    inventory = RepoInventory(
        name="fixture",
        root="/tmp/fixture",
        source_kind=SourceKind.LOCAL,
        files=tuple(
            FileInfo(
                path=f.path,
                language=Language.PYTHON,
                extension=".py",
                size_bytes=1,
                line_count=9,
                sha256="x",
                module=f.module,
            )
            for f in files
        ),
    )
    return ParsedRepo(inventory=inventory, files=files)


@pytest.fixture
def graphed(repository: Repository) -> Generator[Repository, None, None]:
    write_parsed_repo(repository.id, _diamond_repo())
    try:
        yield repository
    finally:
        delete_repo_graph(repository.id)


def _key(repo: Repository, module: str, name: str, path: str) -> str:
    return symbol_key(repo.id, module, name, path)


def _settings_key(repo: Repository) -> str:
    return _key(repo, "app.core.config", "get_settings", CONFIG)


def _impact(client: TestClient, repo: Repository, key: str, **params):
    response = client.get(f"/repos/{repo.id}/impact", params={"key": key, **params})
    assert response.status_code == 200, response.text
    return response.json()


# --- the blast radius --------------------------------------------------------


def test_impact_returns_everything_that_reaches_the_target(client: TestClient, graphed: Repository):
    body = _impact(client, graphed, _settings_key(graphed))

    assert body["root"]["qualname"] == "get_settings"
    names = {item["qualname"] for item in body["items"] if item["type"] == "Function"}
    assert names == {"run", "handler", "tick"}


def test_a_leaf_symbol_has_an_empty_blast_radius(client: TestClient, graphed: Repository):
    """Nothing calls `handler`, so changing it breaks nothing."""
    body = _impact(client, graphed, _key(graphed, "app.cli", "handler", CLI))

    assert body["items"] == []
    assert body["total"] == 0
    assert body["summary"]["max_hops"] == 0


def test_direct_callers_outrank_distant_ones(client: TestClient, graphed: Repository):
    body = _impact(client, graphed, _settings_key(graphed))

    ranked = [item["qualname"] for item in body["items"] if item["type"] == "Function"]
    # `run` and `tick` both call get_settings directly; `handler` is two hops out.
    assert ranked.index("handler") > ranked.index("run")
    assert ranked.index("handler") > ranked.index("tick")


def test_items_come_back_ordered_by_score(client: TestClient, graphed: Repository):
    body = _impact(client, graphed, _settings_key(graphed))

    scores = [item["score"] for item in body["items"]]
    assert scores == sorted(scores, reverse=True)


# --- decay -------------------------------------------------------------------


def test_score_is_confidence_decayed_once_per_hop(client: TestClient, graphed: Repository):
    body = _impact(client, graphed, _settings_key(graphed), depth=2)

    by_name = {item["qualname"]: item for item in body["items"]}
    # Every edge here is an `imported` call at 0.95 confidence.
    assert by_name["run"]["hops"] == 1
    assert by_name["run"]["score"] == pytest.approx(0.95, abs=1e-3)
    assert by_name["handler"]["hops"] == 2
    assert by_name["handler"]["score"] == pytest.approx(0.95**2 * DEFAULT_DECAY, abs=1e-3)


def test_decay_of_one_makes_score_equal_confidence(client: TestClient, graphed: Repository):
    body = _impact(client, graphed, _settings_key(graphed), decay=1)

    for item in body["items"]:
        assert item["score"] == pytest.approx(item["confidence"], abs=1e-4)


def test_a_harsher_decay_pushes_distant_nodes_down(client: TestClient, graphed: Repository):
    def handler_score(decay: float) -> float:
        body = _impact(client, graphed, _settings_key(graphed), depth=2, decay=decay)
        return next(i for i in body["items"] if i["qualname"] == "handler")["score"]

    assert handler_score(0.1) < handler_score(0.9)


def test_decay_outside_its_range_is_rejected(client: TestClient, graphed: Repository):
    response = client.get(
        f"/repos/{graphed.id}/impact",
        params={"key": _settings_key(graphed), "decay": 0},
    )
    assert response.status_code == 422


# --- routes ------------------------------------------------------------------


def test_each_node_carries_the_route_the_change_travels(client: TestClient, graphed: Repository):
    body = _impact(client, graphed, _settings_key(graphed), depth=2)

    handler = next(i for i in body["items"] if i["qualname"] == "handler")
    assert handler["route"] == [
        _settings_key(graphed),
        _key(graphed, "app.service", "run", SERVICE),
        _key(graphed, "app.cli", "handler", CLI),
    ]
    assert handler["via"] == ["CALLS", "CALLS"]


def test_a_node_reachable_two_ways_reports_its_strongest_route(
    client: TestClient, graphed: Repository
):
    """`tick` calls get_settings directly and through `run`. The direct route wins."""
    body = _impact(client, graphed, _settings_key(graphed), depth=3)

    tick = next(i for i in body["items"] if i["qualname"] == "tick")
    assert tick["hops"] == 1
    assert tick["route"] == [
        _settings_key(graphed),
        _key(graphed, "app.worker", "tick", WORKER),
    ]


def test_a_node_appears_at_most_once(client: TestClient, graphed: Repository):
    body = _impact(client, graphed, _settings_key(graphed), depth=5)

    keys = [item["key"] for item in body["items"]]
    assert len(keys) == len(set(keys))


def test_the_target_is_never_in_its_own_blast_radius(client: TestClient, graphed: Repository):
    body = _impact(client, graphed, _settings_key(graphed), depth=5)
    assert all(item["key"] != _settings_key(graphed) for item in body["items"])


# --- depth and limits --------------------------------------------------------


def test_depth_one_returns_only_direct_dependents(client: TestClient, graphed: Repository):
    body = _impact(client, graphed, _settings_key(graphed), depth=1)

    names = {item["qualname"] for item in body["items"] if item["type"] == "Function"}
    assert names == {"run", "tick"}
    assert body["summary"]["max_hops"] == 1


def test_the_limit_reports_truncation_rather_than_hiding_it(
    client: TestClient, graphed: Repository
):
    body = _impact(client, graphed, _settings_key(graphed), limit=1)

    assert body["truncated"] is True
    assert body["total"] == 1


def test_depth_beyond_the_cap_is_rejected(client: TestClient, graphed: Repository):
    response = client.get(
        f"/repos/{graphed.id}/impact", params={"key": _settings_key(graphed), "depth": 99}
    )
    assert response.status_code == 422


# --- file-level impact -------------------------------------------------------


def test_impact_on_a_file_follows_importers(client: TestClient, graphed: Repository):
    body = _impact(client, graphed, file_key(graphed.id, CONFIG), depth=2)

    paths = {item["path"] for item in body["items"] if item["type"] == "File"}
    assert paths == {SERVICE, WORKER, CLI}


def test_a_file_keeps_its_own_path_alongside_its_route(client: TestClient, graphed: Repository):
    """`path` is the file on disk, `route` is how the change gets there."""
    body = _impact(client, graphed, file_key(graphed.id, CONFIG), depth=1)

    service = next(i for i in body["items"] if i["path"] == SERVICE)
    assert service["route"] == [
        file_key(graphed.id, CONFIG),
        file_key(graphed.id, SERVICE),
    ]


def test_containment_is_not_impact(client: TestClient, graphed: Repository):
    """A file's own symbols are not "broken by" a change to the file."""
    body = _impact(client, graphed, file_key(graphed.id, CONFIG), depth=3)

    assert all("CONTAINS" not in item["via"] for item in body["items"])
    assert not any(item["qualname"] == "get_settings" for item in body["items"])


# --- summary -----------------------------------------------------------------


def test_the_summary_counts_the_radius_by_hop_and_type(client: TestClient, graphed: Repository):
    body = _impact(client, graphed, _settings_key(graphed), depth=2)
    summary = body["summary"]

    assert summary["total"] == body["total"]
    assert summary["direct"] == summary["by_hop"]["1"]
    assert sum(summary["by_hop"].values()) == body["total"]
    assert sum(summary["by_type"].values()) == body["total"]
    assert summary["top_score"] == max(item["score"] for item in body["items"])


def test_summarize_handles_an_empty_radius():
    summary = summarize([], depth=3, decay=DEFAULT_DECAY)

    assert summary == {
        "total": 0,
        "depth": 3,
        "decay": DEFAULT_DECAY,
        "direct": 0,
        "max_hops": 0,
        "top_score": 0.0,
        "by_hop": {},
        "by_type": {},
    }


# --- errors ------------------------------------------------------------------


def test_an_unknown_node_key_404s_with_a_useful_message(client: TestClient, graphed: Repository):
    response = client.get(f"/repos/{graphed.id}/impact", params={"key": "sym:nope:nope:nope"})
    assert response.status_code == 404
    assert "graph/search" in response.json()["detail"]


def test_impact_404s_for_an_unknown_repository(client: TestClient):
    response = client.get("/repos/00000000-0000-0000-0000-000000000000/impact", params={"key": "x"})
    assert response.status_code == 404


def test_a_node_from_another_repository_is_not_visible(client: TestClient, graphed: Repository):
    response = client.get(
        f"/repos/{graphed.id}/impact",
        params={"key": symbol_key("other-repo", "app.service", "run", SERVICE)},
    )
    assert response.status_code == 404
