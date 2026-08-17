"""Parse-job history.

The pipeline is driven end to end against the real stores — the fixture repo is
the parser package's own test tree, which is small enough to parse in a test but
real enough to produce symbols, imports and calls.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import JobStatus, ParseJob, ParseStatus, Repository
from app.services.graph_writer import count_nodes, delete_repo_graph
from app.services.parsing import parse_repository

# The parser's own fixture tree: a package, a module outside one, and a file
# that deliberately fails to parse.
FIXTURES = Path(__file__).resolve().parents[2] / "parser" / "tests"


def _jobs(db: Session, repository: Repository) -> list[ParseJob]:
    return list(
        db.scalars(
            select(ParseJob)
            .where(ParseJob.repository_id == repository.id)
            .order_by(ParseJob.created_at.desc())
        ).all()
    )


def test_a_successful_parse_records_one_complete_job(db: Session, repository: Repository):
    try:
        parse_repository(repository.id, str(FIXTURES))
        db.expire_all()

        jobs = _jobs(db, repository)
        assert len(jobs) == 1
        job = jobs[0]
        assert job.status == JobStatus.COMPLETE
        assert job.failed_stage is None
        assert job.file_count > 0
        assert job.symbol_count > 0
        assert job.graph_nodes > 0
        assert job.graph_relationships > 0
        assert job.duration_ms is not None and job.duration_ms >= 0
        assert job.started_at is not None and job.finished_at is not None
    finally:
        delete_repo_graph(repository.id)


def test_the_run_id_names_the_graph_the_job_wrote(db: Session, repository: Repository):
    """The job row is the only bridge from Postgres to the Neo4j subgraph."""
    try:
        parse_repository(repository.id, str(FIXTURES))
        db.expire_all()
        job = _jobs(db, repository)[0]

        from app.core.graph import graph_session

        with graph_session() as session:
            record = session.run(
                "MATCH (n {repo_id: $repo_id}) WHERE n.run_id = $run "
                "RETURN count(n) AS stamped",
                repo_id=str(repository.id),
                run=str(job.run_id),
            ).single()

        total = sum(count_nodes(repository.id).values())
        assert record["stamped"] == total
    finally:
        delete_repo_graph(repository.id)


def test_a_failed_ingest_still_leaves_a_job_naming_the_stage(
    db: Session, repository: Repository
):
    """A run that left no trace is indistinguishable from one that never started."""
    parse_repository(repository.id, str(FIXTURES / "does-not-exist"))
    db.expire_all()

    jobs = _jobs(db, repository)
    assert len(jobs) == 1
    assert jobs[0].status == JobStatus.FAILED
    assert jobs[0].failed_stage == "ingest"
    assert jobs[0].error_message
    # The repository row carries the same failure as its current state.
    assert db.get(Repository, repository.id).status == ParseStatus.FAILED


def test_reparsing_appends_a_job_rather_than_overwriting_one(
    db: Session, repository: Repository
):
    try:
        parse_repository(repository.id, str(FIXTURES))
        parse_repository(repository.id, str(FIXTURES))
        db.expire_all()

        jobs = _jobs(db, repository)
        assert len(jobs) == 2
        assert {j.status for j in jobs} == {JobStatus.COMPLETE}
        # Distinct stamps, so the sweep can tell the runs apart.
        assert jobs[0].run_id != jobs[1].run_id
        # The second run rewrote the same graph and swept nothing.
        assert jobs[0].graph_nodes == jobs[1].graph_nodes
        assert jobs[0].graph_nodes_deleted == 0
    finally:
        delete_repo_graph(repository.id)


def test_a_failed_run_after_a_good_one_keeps_both(db: Session, repository: Repository):
    try:
        parse_repository(repository.id, str(FIXTURES))
        parse_repository(repository.id, str(FIXTURES / "nope"))
        db.expire_all()

        jobs = _jobs(db, repository)
        assert [j.status for j in jobs] == [JobStatus.FAILED, JobStatus.COMPLETE]
        # The successful run's numbers survive the later failure.
        assert jobs[1].graph_nodes > 0
    finally:
        delete_repo_graph(repository.id)


# --- endpoints ---------------------------------------------------------------


def test_jobs_endpoint_lists_newest_first(
    client: TestClient, db: Session, repository: Repository
):
    try:
        parse_repository(repository.id, str(FIXTURES))
        parse_repository(repository.id, str(FIXTURES / "nope"))

        body = client.get(f"/repos/{repository.id}/jobs").json()
        assert body["total"] == 2
        assert body["items"][0]["status"] == "failed"
        assert body["items"][1]["status"] == "complete"
    finally:
        delete_repo_graph(repository.id)


def test_latest_job_is_the_current_run(client: TestClient, repository: Repository):
    try:
        parse_repository(repository.id, str(FIXTURES))
        body = client.get(f"/repos/{repository.id}/jobs/latest").json()
        assert body["status"] == "complete"
        assert body["graph_nodes"] > 0
    finally:
        delete_repo_graph(repository.id)


def test_latest_404s_before_a_repository_has_ever_been_parsed(
    client: TestClient, repository: Repository
):
    response = client.get(f"/repos/{repository.id}/jobs/latest")
    assert response.status_code == 404


def test_jobs_404_for_an_unknown_repository(client: TestClient):
    response = client.get("/repos/00000000-0000-0000-0000-000000000000/jobs")
    assert response.status_code == 404


def test_deleting_a_repository_clears_its_graph(
    client: TestClient, db: Session, repository: Repository
):
    """Postgres cascades its own rows; nothing cascades into Neo4j."""
    parse_repository(repository.id, str(FIXTURES))
    assert sum(count_nodes(repository.id).values()) > 0
    repository_id = repository.id

    assert client.delete(f"/repos/{repository_id}").status_code == 204

    assert sum(count_nodes(repository_id).values()) == 0
    db.expire_all()
    assert db.get(Repository, repository_id) is None
