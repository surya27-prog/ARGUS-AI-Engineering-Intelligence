"""Risk score tests.

Split in two on purpose. The formula is pure arithmetic over `RiskMetrics`, so
it is tested directly — every weight, knee and inversion is a claim the doc
makes, and a claim in a doc that no test holds up is a claim that quietly stops
being true. The graph half then only has to prove that the right counts reach
it.
"""

from __future__ import annotations

import re
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app.models import Repository
from app.services.cochange import write_cochange
from app.services.graph_keys import symbol_key
from app.services.graph_writer import delete_repo_graph, write_parsed_repo
from app.services.risk import (
    HISTORY_FACTORS,
    K_TESTS,
    TEST_PATH_PATTERN,
    WEIGHTS,
    RiskMetrics,
    band_for,
    build_score,
    factors_for,
    rank_repository,
    saturate,
    score_metrics,
    score_node,
    weights_for,
)
from parser.history import CoChangePair, RepoHistory
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

# --- the formula -------------------------------------------------------------


def test_saturate_is_zero_at_zero_and_half_at_the_knee():
    assert saturate(0, 8) == 0.0
    assert saturate(8, 8) == 0.5
    assert saturate(24, 8) == 0.75
    assert saturate(-3, 8) == 0.0


def test_saturate_never_reaches_one():
    assert saturate(10_000, 8) < 1.0


def test_the_weights_sum_to_one():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_a_node_with_nothing_measurable_still_scores_the_coverage_penalty():
    """No dependents, no history, no tests. Untested is not the same as safe."""
    score, factors, weights = score_metrics(RiskMetrics(change_count=0))

    assert factors["blast"] == 0.0
    assert factors["coverage"] == 1.0
    assert score == pytest.approx(100 * WEIGHTS["coverage"], abs=0.1)


def test_coverage_is_inverted_so_tested_code_scores_lower():
    untested = factors_for(RiskMetrics(test_references=0))
    thinly = factors_for(RiskMetrics(test_references=int(K_TESTS)))
    well = factors_for(RiskMetrics(test_references=20))

    assert untested["coverage"] == 1.0
    assert thinly["coverage"] == pytest.approx(0.5)
    assert well["coverage"] < 0.15


def test_blast_dominates_the_other_factors():
    """The heaviest weight, because what a change reaches is the headline."""
    assert WEIGHTS["blast"] == max(WEIGHTS.values())

    blasty = score_metrics(RiskMetrics(dependents=40, change_count=0, test_references=99))[0]
    churny = score_metrics(RiskMetrics(change_count=40, test_references=99))[0]
    assert blasty > churny


def test_more_dependents_never_lowers_the_score():
    previous = -1.0
    for dependents in (0, 1, 4, 8, 20, 100):
        score = score_metrics(RiskMetrics(dependents=dependents, change_count=0))[0]
        assert score >= previous
        previous = score


def test_coupling_blends_partner_count_with_the_strongest_pair():
    many_loose = factors_for(RiskMetrics(co_partners=10, max_jaccard=0.1, change_count=1))
    few_tight = factors_for(RiskMetrics(co_partners=2, max_jaccard=1.0, change_count=1))

    # Neither input alone decides it: the loose file wins on count, the tight
    # one on strength, and both land in a comparable band.
    assert 0.4 < many_loose["coupling"] < 0.9
    assert 0.4 < few_tight["coupling"] < 0.9


def test_a_jaccard_above_one_cannot_inflate_the_factor():
    """Defensive: the factor stays bounded even if a bad value is stored."""
    assert factors_for(RiskMetrics(max_jaccard=5.0, change_count=1))["coupling"] <= 1.0


def test_the_score_is_bounded():
    maxed = RiskMetrics(
        dependents=10_000,
        direct_dependents=10_000,
        dependencies=10_000,
        co_partners=500,
        max_jaccard=1.0,
        change_count=10_000,
        test_references=0,
    )
    assert 0.0 <= score_metrics(maxed)[0] <= 100.0
    assert score_metrics(RiskMetrics(change_count=0, test_references=1000))[0] >= 0.0


# --- missing history ---------------------------------------------------------


def test_missing_history_redistributes_its_weight_rather_than_scoring_zero():
    metrics = RiskMetrics(dependents=8, change_count=None)

    weights = weights_for(metrics)

    assert set(weights) == set(WEIGHTS) - set(HISTORY_FACTORS)
    assert sum(weights.values()) == pytest.approx(1.0)


def test_a_repo_without_history_is_not_reported_as_safer():
    """The failure mode this guards: 'low risk' on data never collected."""
    structural = {"dependents": 20, "direct_dependents": 8, "dependencies": 4}

    without = score_metrics(RiskMetrics(**structural, change_count=None))[0]
    with_quiet_history = score_metrics(RiskMetrics(**structural, change_count=0))[0]

    assert without > with_quiet_history


def test_history_present_but_quiet_uses_the_full_weights():
    assert weights_for(RiskMetrics(change_count=0)) == WEIGHTS


# --- bands and explanations --------------------------------------------------


def test_bands_cover_the_whole_range():
    assert band_for(0) == "low"
    assert band_for(24.9) == "low"
    assert band_for(25) == "moderate"
    assert band_for(49.9) == "moderate"
    assert band_for(50) == "high"
    assert band_for(75) == "critical"
    assert band_for(100) == "critical"


def test_the_reasons_state_what_drove_the_score():
    result = build_score(
        {"key": "k", "type": "Function", "display": "run"},
        RiskMetrics(
            dependents=9, direct_dependents=3, co_partners=2, max_jaccard=0.8, change_count=7
        ),
    )

    joined = " ".join(result.reasons)
    assert "9 symbol(s) depend on it, 3 directly" in joined
    assert "no test references it" in joined
    assert "habitually changes with 2 other file(s)" in joined
    assert "7 commit(s)" in joined


def test_a_node_without_history_says_so_instead_of_claiming_no_coupling():
    result = build_score({"key": "k", "type": "File", "display": "a.py"}, RiskMetrics())

    assert any("no commit history" in reason for reason in result.reasons)
    assert not any("habitually changes" in reason for reason in result.reasons)


def test_the_score_can_be_recomputed_from_what_is_returned():
    """The whole point of returning factors and weights: the number is auditable."""
    result = build_score(
        {"key": "k", "type": "Function", "display": "run"},
        RiskMetrics(dependents=12, direct_dependents=4, dependencies=2, change_count=5),
    )

    by_hand = 100 * sum(result.factors[name] * weight for name, weight in result.weights.items())
    assert by_hand == pytest.approx(result.score, abs=0.5)


# --- the test-path pattern ---------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_service.py",
        "backend/tests/test_service.py",
        "test/foo.py",
        "app/test_service.py",
        "app/service_test.py",
        "tests/conftest.py",
        "conftest.py",
    ],
)
def test_test_files_are_recognised(path: str):
    assert re.fullmatch(TEST_PATH_PATTERN, path) is not None


@pytest.mark.parametrize(
    "path",
    [
        "app/service.py",
        "app/latest.py",
        "app/contest.py",
        "app/protest_handler.py",
        "src/testing_utils.py",
    ],
)
def test_ordinary_files_are_not_mistaken_for_tests(path: str):
    assert re.fullmatch(TEST_PATH_PATTERN, path) is None


# --- against the graph -------------------------------------------------------
#
# A four-file repo: a core function everything reaches, a middle layer, an
# entry point, and a test that exercises only the middle layer.

CORE = "app/core/config.py"
SERVICE = "app/service.py"
API = "app/api.py"
TEST = "tests/test_service.py"


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


def _layered_repo() -> ParsedRepo:
    files = (
        ParsedFile(
            path=CORE,
            module="app.core.config",
            symbols=(_sym("get_settings", CORE, "app.core.config"),),
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
            path=API,
            module="app.api",
            symbols=(_sym("handler", API, "app.api"),),
            imports=(ImportRef(module="app.service", name="run", line=1, is_from=True),),
            calls=(CallRef(callee="run", line=6, caller="handler", file_path=API),),
        ),
        ParsedFile(
            path=TEST,
            module="tests.test_service",
            symbols=(_sym("test_run", TEST, "tests.test_service"),),
            imports=(ImportRef(module="app.service", name="run", line=1, is_from=True),),
            calls=(CallRef(callee="run", line=4, caller="test_run", file_path=TEST),),
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
    write_parsed_repo(repository.id, _layered_repo())
    try:
        yield repository
    finally:
        delete_repo_graph(repository.id)


def _key(repo: Repository, module: str, name: str, path: str) -> str:
    return symbol_key(repo.id, module, name, path)


def _run_key(repo: Repository) -> str:
    return _key(repo, "app.service", "run", SERVICE)


def test_the_metrics_come_from_the_graph(graphed: Repository):
    result = score_node(graphed.id, _run_key(graphed), depth=2)

    assert result.metrics.direct_dependents == 2  # handler and test_run
    assert result.metrics.dependencies == 1  # get_settings
    assert result.type == "Function"


def test_a_symbol_called_from_a_test_file_counts_as_covered(graphed: Repository):
    covered = score_node(graphed.id, _run_key(graphed))
    uncovered = score_node(graphed.id, _key(graphed, "app.api", "handler", API))

    assert covered.metrics.test_references == 1
    assert uncovered.metrics.test_references == 0
    assert covered.factors["coverage"] < uncovered.factors["coverage"]


def test_a_leaf_with_no_dependents_scores_below_a_hub(graphed: Repository):
    hub = score_node(graphed.id, _key(graphed, "app.core.config", "get_settings", CORE), depth=2)
    leaf = score_node(graphed.id, _key(graphed, "app.api", "handler", API))

    assert hub.metrics.dependents > leaf.metrics.dependents
    assert hub.score > leaf.score


def test_without_a_history_pass_every_score_says_so(graphed: Repository):
    result = score_node(graphed.id, _run_key(graphed))

    assert result.metrics.change_count is None
    assert set(result.weights) == set(WEIGHTS) - set(HISTORY_FACTORS)


def test_history_raises_the_score_of_a_churny_coupled_file(graphed: Repository):
    before = score_node(graphed.id, _run_key(graphed))

    write_cochange(
        graphed.id,
        RepoHistory(
            commits_read=40,
            commits_used=40,
            commits_skipped=0,
            file_commits={SERVICE: 30, TEST: 25, CORE: 2},
            pairs=(
                CoChangePair(
                    left=SERVICE,
                    right=TEST,
                    shared_commits=20,
                    left_commits=30,
                    right_commits=25,
                    jaccard=0.9,
                    last_together="2026-08-20T10:00:00+00:00",
                ),
            ),
        ),
        run_id="run-history",
    )
    after = score_node(graphed.id, _run_key(graphed))

    assert after.metrics.change_count == 30
    assert after.metrics.co_partners == 1
    assert after.metrics.max_jaccard == 0.9
    assert after.score > before.score


def test_a_function_inherits_the_churn_of_the_file_it_lives_in(graphed: Repository):
    write_cochange(
        graphed.id,
        RepoHistory(
            commits_read=10,
            commits_used=10,
            commits_skipped=0,
            file_commits={SERVICE: 12},
        ),
        run_id="run-history",
    )

    assert score_node(graphed.id, _run_key(graphed)).metrics.change_count == 12


def test_ranking_orders_the_repository_by_score(graphed: Repository):
    items, scored = rank_repository(graphed.id, depth=2, limit=10)

    assert scored >= 4
    assert [i.score for i in items] == sorted((i.score for i in items), reverse=True)


def test_ranking_can_be_restricted_to_files(graphed: Repository):
    items, _ = rank_repository(graphed.id, node_type="File", limit=10)

    assert {item.type for item in items} == {"File"}


def test_the_limit_trims_the_answer_not_the_work(graphed: Repository):
    items, scored = rank_repository(graphed.id, limit=1)

    assert len(items) == 1
    assert scored > 1


def test_an_unknown_node_is_not_scored(graphed: Repository):
    from app.services.graph_queries import NodeNotFound

    with pytest.raises(NodeNotFound):
        score_node(graphed.id, "sym:nope:nope:nope")


# --- the endpoint ------------------------------------------------------------


def test_the_endpoint_ranks_a_repository(client: TestClient, graphed: Repository):
    body = client.get(f"/repos/{graphed.id}/risk", params={"limit": 3}).json()

    assert body["total"] == 3
    assert body["scored"] > 3
    assert body["depth"] == 2
    scores = [item["score"] for item in body["items"]]
    assert scores == sorted(scores, reverse=True)


def test_the_endpoint_returns_one_node_with_its_derivation(client: TestClient, graphed: Repository):
    body = client.get(f"/repos/{graphed.id}/risk", params={"key": _run_key(graphed)}).json()

    assert body["total"] == 1
    item = body["items"][0]
    assert item["display"] == "run"
    assert item["band"] in {"low", "moderate", "high", "critical"}
    assert set(item["factors"]) == set(WEIGHTS)
    assert item["reasons"]
    assert item["node"]["key"] == _run_key(graphed)


def test_the_endpoint_can_filter_by_node_type(client: TestClient, graphed: Repository):
    body = client.get(f"/repos/{graphed.id}/risk", params={"type": "File"}).json()

    assert {item["type"] for item in body["items"]} == {"File"}


def test_an_unknown_type_is_rejected(client: TestClient, graphed: Repository):
    assert client.get(f"/repos/{graphed.id}/risk", params={"type": "Repo"}).status_code == 422


def test_an_unknown_key_404s(client: TestClient, graphed: Repository):
    response = client.get(f"/repos/{graphed.id}/risk", params={"key": "sym:nope:nope:nope"})

    assert response.status_code == 404
    assert "graph/search" in response.json()["detail"]


def test_risk_404s_for_an_unknown_repository(client: TestClient):
    response = client.get("/repos/00000000-0000-0000-0000-000000000000/risk")
    assert response.status_code == 404
