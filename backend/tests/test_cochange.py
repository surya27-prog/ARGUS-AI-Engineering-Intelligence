"""Co-change graph tests.

The counting itself is tested in `parser/tests/test_history.py` against real git
repositories. What is tested here is the seam: that pairs become edges between
the right nodes, that a re-parse replaces them, and that the endpoint reads them
back from either end of an undirected relationship.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app.models import Repository
from app.services.cochange import CoChangeResult, coupled_files, write_cochange
from app.services.graph_keys import file_key
from app.services.graph_writer import delete_repo_graph, write_parsed_repo
from parser.history import CoChangePair, RepoHistory
from parser.models import (
    FileInfo,
    Language,
    ParsedFile,
    ParsedRepo,
    RepoInventory,
    SourceKind,
)

SERVICE = "app/service.py"
TEST = "tests/test_service.py"
CONFIG = "app/core/config.py"
GONE = "app/deleted.py"


def _repo_with_files(*paths: str) -> ParsedRepo:
    files = tuple(ParsedFile(path=path, module=None) for path in paths)
    inventory = RepoInventory(
        name="fixture",
        root="/tmp/fixture",
        source_kind=SourceKind.LOCAL,
        files=tuple(
            FileInfo(
                path=path,
                language=Language.PYTHON,
                extension=".py",
                size_bytes=1,
                line_count=9,
                sha256="x",
            )
            for path in paths
        ),
    )
    return ParsedRepo(inventory=inventory, files=files)


def _history(*pairs: CoChangePair, file_commits: dict[str, int] | None = None) -> RepoHistory:
    return RepoHistory(
        commits_read=10,
        commits_used=10,
        commits_skipped=0,
        file_commits=file_commits or {},
        pairs=pairs,
    )


def _pair(left: str, right: str, *, commits: int = 4, jaccard: float = 0.8) -> CoChangePair:
    return CoChangePair(
        left=left,
        right=right,
        shared_commits=commits,
        left_commits=5,
        right_commits=5,
        jaccard=jaccard,
        last_together="2026-08-20T10:00:00+00:00",
    )


@pytest.fixture
def graphed(repository: Repository) -> Generator[Repository, None, None]:
    """Three files in the graph, no coupling yet."""
    write_parsed_repo(repository.id, _repo_with_files(SERVICE, TEST, CONFIG))
    try:
        yield repository
    finally:
        delete_repo_graph(repository.id)


@pytest.fixture
def coupled(graphed: Repository) -> Repository:
    """…and the service/test pair coupled, plus a weaker service/config pair."""
    write_cochange(
        graphed.id,
        _history(
            _pair(SERVICE, TEST, commits=9, jaccard=0.9),
            _pair(CONFIG, SERVICE, commits=3, jaccard=0.3),
            file_commits={SERVICE: 10, TEST: 9, CONFIG: 4},
        ),
        run_id="run-1",
    )
    return graphed


# --- writing -----------------------------------------------------------------


def test_a_pair_becomes_an_edge_between_the_two_files(coupled: Repository):
    items = coupled_files(coupled.id, key=file_key(coupled.id, SERVICE), min_commits=1)

    partners = {(item["left"]["path"], item["right"]["path"]): item for item in items}
    assert (SERVICE, TEST) in partners
    assert partners[(SERVICE, TEST)]["commits"] == 9
    assert partners[(SERVICE, TEST)]["jaccard"] == 0.9


def test_churn_lands_on_the_file_node(client: TestClient, coupled: Repository):
    """The other half of what the log knows, and Day 3's risk input."""
    body = client.get(
        f"/repos/{coupled.id}/graph/node", params={"key": file_key(coupled.id, SERVICE)}
    ).json()

    assert body["change_count"] == 10


def test_churn_is_absent_rather_than_zero_before_history_is_read(
    client: TestClient, graphed: Repository
):
    """None means "never measured"; 0 would claim the file has never changed."""
    body = client.get(
        f"/repos/{graphed.id}/graph/node", params={"key": file_key(graphed.id, SERVICE)}
    ).json()

    assert body["change_count"] is None


def test_a_path_that_is_not_in_the_graph_writes_no_edge(graphed: Repository):
    """A file deleted since the window opened must not conjure a node."""
    result = write_cochange(
        graphed.id,
        _history(_pair(GONE, SERVICE), file_commits={GONE: 3, SERVICE: 3}),
        run_id="run-x",
    )

    assert result.edges_written == 0
    assert result.files_scored == 1  # SERVICE matched, GONE did not
    assert coupled_files(graphed.id, min_commits=1) == []


def test_an_empty_history_writes_nothing(graphed: Repository):
    result = write_cochange(graphed.id, _history(), run_id="run-empty")

    assert result == CoChangeResult(
        run_id="run-empty",
        repo_id=str(graphed.id),
        commits_read=10,
        commits_used=10,
        commits_skipped=0,
        edges_written=0,
        files_scored=0,
    )


def test_rewriting_the_same_pair_updates_rather_than_duplicates(graphed: Repository):
    write_cochange(graphed.id, _history(_pair(SERVICE, TEST, commits=2)), run_id="run-1")
    write_cochange(graphed.id, _history(_pair(SERVICE, TEST, commits=7)), run_id="run-2")

    items = coupled_files(graphed.id, min_commits=1)

    assert len(items) == 1
    assert items[0]["commits"] == 7


def test_a_reparse_sweeps_coupling_the_new_run_did_not_write(coupled: Repository):
    """The edges carry the run's stamp, so the graph writer's sweep clears them."""
    assert coupled_files(coupled.id, min_commits=1) != []

    write_parsed_repo(coupled.id, _repo_with_files(SERVICE, TEST, CONFIG), run_id="run-2")

    assert coupled_files(coupled.id, min_commits=1) == []


def test_a_result_with_no_commits_reports_itself_as_skipped():
    result = CoChangeResult(
        run_id="r",
        repo_id="x",
        commits_read=0,
        commits_used=0,
        commits_skipped=0,
        edges_written=0,
        files_scored=0,
    )
    assert result.skipped is True


# --- reading -----------------------------------------------------------------


def test_pairs_come_back_strongest_first(coupled: Repository):
    items = coupled_files(coupled.id, min_commits=1)

    assert [item["commits"] for item in items] == [9, 3]


def test_min_commits_filters_weak_coupling(coupled: Repository):
    items = coupled_files(coupled.id, min_commits=5)

    assert [item["commits"] for item in items] == [9]


def test_a_pair_is_returned_once_not_once_per_direction(coupled: Repository):
    items = coupled_files(coupled.id, min_commits=1)

    seen = {frozenset((item["left"]["key"], item["right"]["key"])) for item in items}
    assert len(seen) == len(items)


def test_either_end_of_a_pair_finds_it(coupled: Repository):
    """`config.py` is the left of its pair and `service.py` the right; both must work."""
    from_config = coupled_files(coupled.id, key=file_key(coupled.id, CONFIG), min_commits=1)
    from_service = coupled_files(coupled.id, key=file_key(coupled.id, SERVICE), min_commits=1)

    assert len(from_config) == 1
    assert {(i["left"]["path"], i["right"]["path"]) for i in from_service} == {
        (CONFIG, SERVICE),
        (SERVICE, TEST),
    }


def test_the_limit_caps_the_result(coupled: Repository):
    assert len(coupled_files(coupled.id, min_commits=1, limit=1)) == 1


# --- the endpoint ------------------------------------------------------------


def test_the_endpoint_returns_the_repositorys_coupling(client: TestClient, coupled: Repository):
    body = client.get(f"/repos/{coupled.id}/cochange", params={"min_commits": 1}).json()

    assert body["total"] == 2
    assert body["root"] is None
    first = body["items"][0]
    assert first["left"]["path"] == SERVICE
    assert first["right"]["path"] == TEST
    assert first["commits"] == 9
    assert first["last_together"] == "2026-08-20T10:00:00+00:00"


def test_the_endpoint_scopes_to_one_file_and_echoes_it(client: TestClient, coupled: Repository):
    body = client.get(
        f"/repos/{coupled.id}/cochange",
        params={"key": file_key(coupled.id, TEST), "min_commits": 1},
    ).json()

    assert body["root"]["path"] == TEST
    assert body["total"] == 1


def test_the_default_threshold_hides_one_off_coincidences(client: TestClient, graphed: Repository):
    write_cochange(graphed.id, _history(_pair(SERVICE, TEST, commits=1)), run_id="run-1")

    body = client.get(f"/repos/{graphed.id}/cochange").json()

    assert body["total"] == 0


def test_an_unknown_key_404s_rather_than_returning_nothing(client: TestClient, coupled: Repository):
    """An empty list would read as 'this file is coupled to nothing'."""
    response = client.get(f"/repos/{coupled.id}/cochange", params={"key": "file:nope:nope.py"})

    assert response.status_code == 404
    assert "graph/search" in response.json()["detail"]


def test_cochange_404s_for_an_unknown_repository(client: TestClient):
    response = client.get("/repos/00000000-0000-0000-0000-000000000000/cochange")
    assert response.status_code == 404


def test_a_repository_with_no_history_returns_an_empty_list(
    client: TestClient, graphed: Repository
):
    body = client.get(f"/repos/{graphed.id}/cochange").json()

    assert body == {"root": None, "items": [], "total": 0}
