"""Graph endpoint tests.

These run against the real Neo4j and Postgres from `docker compose`: the
`repository` fixture supplies a Postgres row, and each test writes a small graph
under that repository's id and sweeps it afterwards.
"""

from __future__ import annotations

import time
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app.models import Repository
from app.services.graph_keys import file_key, symbol_key
from app.services.graph_writer import delete_repo_graph, write_parsed_repo
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

# A three-file chain, so a two-hop traversal has something to reach:
#
#   api.py  -imports->  service.py  -imports->  config.py
#   handler -calls->    run         -calls->    get_settings
#
# and one external import, so :Module nodes appear in the file view.
CONFIG = "app/core/config.py"
SERVICE = "app/service.py"
API = "app/api.py"


def _sym(name: str, qualname: str, kind: SymbolKind, path: str, module: str) -> Symbol:
    return Symbol(
        name=name,
        qualname=qualname,
        kind=kind,
        file_path=path,
        line_start=1,
        line_end=9,
        module=module,
    )


def _chain_repo() -> ParsedRepo:
    files = (
        ParsedFile(
            path=CONFIG,
            module="app.core.config",
            symbols=(
                _sym(
                    "get_settings",
                    "get_settings",
                    SymbolKind.FUNCTION,
                    CONFIG,
                    "app.core.config",
                ),
            ),
        ),
        ParsedFile(
            path=SERVICE,
            module="app.service",
            symbols=(_sym("run", "run", SymbolKind.FUNCTION, SERVICE, "app.service"),),
            imports=(
                ImportRef(
                    module="app.core.config", name="get_settings", line=1, is_from=True
                ),
            ),
            calls=(CallRef(callee="get_settings", line=5, caller="run", file_path=SERVICE),),
        ),
        ParsedFile(
            path=API,
            module="app.api",
            symbols=(_sym("handler", "handler", SymbolKind.FUNCTION, API, "app.api"),),
            imports=(
                ImportRef(module="app.service", name="run", line=1, is_from=True),
                ImportRef(module="fastapi", line=2),
            ),
            calls=(CallRef(callee="run", line=6, caller="handler", file_path=API),),
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
    """A repository row with the chain fixture written into the graph."""
    write_parsed_repo(repository.id, _chain_repo())
    try:
        yield repository
    finally:
        delete_repo_graph(repository.id)


def _handler_key(repo: Repository) -> str:
    return symbol_key(repo.id, "app.api", "handler", API)


def _settings_key(repo: Repository) -> str:
    return symbol_key(repo.id, "app.core.config", "get_settings", CONFIG)


# --- /graph ------------------------------------------------------------------


def test_file_view_returns_files_and_their_imports(client: TestClient, graphed: Repository):
    body = client.get(f"/repos/{graphed.id}/graph").json()

    assert body["view"] == "files"
    assert not body["truncated"]
    paths = {n["path"] for n in body["nodes"] if n["type"] == "File"}
    assert paths == {CONFIG, SERVICE, API}
    assert {e["type"] for e in body["edges"]} == {"IMPORTS"}


def test_file_view_includes_external_module_nodes(client: TestClient, graphed: Repository):
    body = client.get(f"/repos/{graphed.id}/graph").json()

    modules = [n for n in body["nodes"] if n["type"] == "Module"]
    assert [m["display"] for m in modules] == ["fastapi"]
    assert modules[0]["is_external"] is True


def test_call_view_returns_the_call_graph(client: TestClient, graphed: Repository):
    body = client.get(f"/repos/{graphed.id}/graph?view=calls").json()

    assert {e["type"] for e in body["edges"]} == {"CALLS"}
    edges = {(e["source"], e["target"]) for e in body["edges"]}
    assert (_handler_key(graphed), symbol_key(graphed.id, "app.service", "run", SERVICE)) in edges


def test_the_limit_reports_truncation_rather_than_hiding_it(
    client: TestClient, graphed: Repository
):
    body = client.get(f"/repos/{graphed.id}/graph?limit=1").json()

    assert body["truncated"] is True
    assert len(body["edges"]) <= 1


def test_an_unknown_view_is_rejected(client: TestClient, graphed: Repository):
    assert client.get(f"/repos/{graphed.id}/graph?view=nonsense").status_code == 422


def test_graph_404s_for_an_unknown_repository(client: TestClient):
    response = client.get("/repos/00000000-0000-0000-0000-000000000000/graph")
    assert response.status_code == 404


# --- /graph/search -----------------------------------------------------------


def test_search_finds_a_symbol_and_returns_its_key(client: TestClient, graphed: Repository):
    body = client.get(f"/repos/{graphed.id}/graph/search?q=get_settings").json()

    assert body["total"] >= 1
    assert any(item["key"] == _settings_key(graphed) for item in body["items"])


def test_search_matches_a_file_path(client: TestClient, graphed: Repository):
    body = client.get(f"/repos/{graphed.id}/graph/search?q=core/config").json()
    assert any(item["path"] == CONFIG for item in body["items"])


def test_search_is_case_insensitive(client: TestClient, graphed: Repository):
    body = client.get(f"/repos/{graphed.id}/graph/search?q=GET_SETTINGS").json()
    assert body["total"] >= 1


def test_search_treats_regex_metacharacters_as_literal_text(
    client: TestClient, graphed: Repository
):
    """`.*` must match nothing here, not everything."""
    body = client.get(f"/repos/{graphed.id}/graph/search?q=.%2A").json()
    assert body["total"] == 0


# --- /dependencies and /dependents -------------------------------------------


def test_dependencies_reach_two_hops_downstream(client: TestClient, graphed: Repository):
    body = client.get(
        f"/repos/{graphed.id}/dependencies",
        params={"key": _handler_key(graphed), "depth": 2},
    ).json()

    assert body["direction"] == "dependencies"
    assert body["root"]["qualname"] == "handler"
    by_name = {item["qualname"]: item for item in body["items"]}
    assert by_name["run"]["hops"] == 1
    assert by_name["get_settings"]["hops"] == 2


def test_depth_one_stops_at_one_hop(client: TestClient, graphed: Repository):
    body = client.get(
        f"/repos/{graphed.id}/dependencies",
        params={"key": _handler_key(graphed), "depth": 1},
    ).json()

    assert {item["qualname"] for item in body["items"]} == {"run"}


def test_dependents_are_the_same_traversal_reversed(client: TestClient, graphed: Repository):
    body = client.get(
        f"/repos/{graphed.id}/dependents",
        params={"key": _settings_key(graphed), "depth": 2},
    ).json()

    assert body["direction"] == "dependents"
    by_name = {item["qualname"]: item for item in body["items"]}
    assert by_name["run"]["hops"] == 1
    assert by_name["handler"]["hops"] == 2


def test_confidence_decays_per_hop(client: TestClient, graphed: Repository):
    body = client.get(
        f"/repos/{graphed.id}/dependents",
        params={"key": _settings_key(graphed), "depth": 2},
    ).json()

    by_name = {item["qualname"]: item for item in body["items"]}
    # Both hops are `imported` calls at 0.95, so two hops compound.
    assert by_name["run"]["confidence"] == pytest.approx(0.95)
    assert by_name["handler"]["confidence"] == pytest.approx(0.9025)
    assert by_name["handler"]["via"] == ["CALLS", "CALLS"]


def test_a_traversal_from_a_file_follows_imports(client: TestClient, graphed: Repository):
    body = client.get(
        f"/repos/{graphed.id}/dependencies",
        params={"key": file_key(graphed.id, API), "depth": 2},
    ).json()

    paths = {item["path"] for item in body["items"] if item["type"] == "File"}
    assert paths == {SERVICE, CONFIG}


def test_containment_is_not_a_dependency(client: TestClient, graphed: Repository):
    """A file's own symbols must not show up as things it depends on."""
    body = client.get(
        f"/repos/{graphed.id}/dependencies",
        params={"key": file_key(graphed.id, API), "depth": 2},
    ).json()

    assert all("CONTAINS" not in item["via"] for item in body["items"])
    assert not any(item["qualname"] == "handler" for item in body["items"])


def test_an_unknown_node_key_404s_with_a_useful_message(
    client: TestClient, graphed: Repository
):
    response = client.get(
        f"/repos/{graphed.id}/dependencies", params={"key": "sym:nope:nope:nope"}
    )
    assert response.status_code == 404
    assert "graph/search" in response.json()["detail"]


def test_depth_beyond_the_cap_is_rejected(client: TestClient, graphed: Repository):
    response = client.get(
        f"/repos/{graphed.id}/dependencies", params={"key": _handler_key(graphed), "depth": 99}
    )
    assert response.status_code == 422


def test_a_node_from_another_repository_is_not_visible(
    client: TestClient, graphed: Repository
):
    """Keys are repo-scoped; asking for someone else's node is a 404, not a leak."""
    response = client.get(
        f"/repos/{graphed.id}/dependencies",
        params={"key": symbol_key("other-repo", "app.service", "run", SERVICE)},
    )
    assert response.status_code == 404


# --- the day's timing criterion ----------------------------------------------


def test_every_endpoint_answers_well_under_a_second(client: TestClient, graphed: Repository):
    """Day 5 is 'Cypher-backed endpoints return JSON in <1s on the fixture repo'."""
    calls = [
        (f"/repos/{graphed.id}/graph", {}),
        (f"/repos/{graphed.id}/graph", {"view": "calls"}),
        (f"/repos/{graphed.id}/graph/search", {"q": "run"}),
        (f"/repos/{graphed.id}/dependencies", {"key": _handler_key(graphed), "depth": 5}),
        (f"/repos/{graphed.id}/dependents", {"key": _settings_key(graphed), "depth": 5}),
    ]
    for url, params in calls:
        start = time.perf_counter()
        response = client.get(url, params=params)
        elapsed = time.perf_counter() - start
        assert response.status_code == 200, url
        assert elapsed < 1.0, f"{url} took {elapsed:.2f}s"
