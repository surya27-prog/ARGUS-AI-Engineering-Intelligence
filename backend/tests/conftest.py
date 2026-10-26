"""Shared fixtures. These tests talk to the real Postgres from `docker compose`;
each one cleans up the rows it creates rather than mocking the database."""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.main import app
from app.models import ParseStatus, Repository


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def repository(db: Session) -> Generator[Repository, None, None]:
    """A persisted repository row, removed again when the test finishes."""
    repo = Repository(name="fixture-repo", url=None, status=ParseStatus.PENDING)
    db.add(repo)
    db.commit()
    db.refresh(repo)
    repository_id = repo.id
    try:
        yield repo
    finally:
        # Re-fetched rather than deleted directly: a test may have deleted the
        # row itself, and a stale instance would make teardown fail instead of
        # the assertion that matters.
        db.expire_all()
        if (existing := db.get(Repository, repository_id)) is not None:
            db.delete(existing)
            db.commit()
