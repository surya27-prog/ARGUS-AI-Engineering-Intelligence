"""The parser-to-Postgres seam, exercised against real source on disk.

The backend's own `app/` package is the fixture: it is guaranteed present, small,
and its contents are known.
"""

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ParseStatus, SourceFile, Symbol
from app.services.parsing import parse_repository, store_parse_result

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
def parsed_backend():
    from parser import analyze_repo

    return analyze_repo(str(BACKEND_DIR / "app"))


def test_parser_is_importable_from_the_backend():
    from parser import ParsedRepo, analyze_repo  # noqa: F401


def test_store_parse_result_persists_files_and_symbols(db: Session, repository, parsed_backend):
    store_parse_result(db, repository, parsed_backend)

    assert repository.status == ParseStatus.COMPLETE
    assert repository.parsed_at is not None
    assert repository.file_count == parsed_backend.inventory.file_count
    assert repository.symbol_count == parsed_backend.symbol_count

    paths = set(
        db.scalars(select(SourceFile.path).where(SourceFile.repository_id == repository.id))
    )
    assert "core/config.py" in paths
    assert "main.py" in paths


def test_stored_symbols_keep_their_signatures(db: Session, repository, parsed_backend):
    store_parse_result(db, repository, parsed_backend)

    settings = db.scalar(
        select(Symbol).where(
            Symbol.repository_id == repository.id, Symbol.qualname == "Settings"
        )
    )
    assert settings is not None
    assert settings.kind == "class"
    assert settings.base_classes == ["BaseSettings"]

    health = db.scalar(
        select(Symbol).where(Symbol.repository_id == repository.id, Symbol.qualname == "health")
    )
    assert health is not None
    assert health.kind == "function"
    assert [p["name"] for p in health.parameters] == ["db", "settings"]
    assert health.docstring is not None


def test_file_counts_come_from_both_walker_and_extractor(db: Session, repository, parsed_backend):
    store_parse_result(db, repository, parsed_backend)

    config = db.scalar(
        select(SourceFile).where(
            SourceFile.repository_id == repository.id, SourceFile.path == "core/config.py"
        )
    )
    assert config is not None
    # line_count and sha256 come from the walker, symbol_count from the extractor.
    assert config.line_count > 0
    assert len(config.sha256) == 64
    assert config.symbol_count >= 2
    assert config.import_count >= 2
    assert config.parse_error is None


def test_reparsing_replaces_rather_than_duplicates(db: Session, repository, parsed_backend):
    store_parse_result(db, repository, parsed_backend)
    first = db.scalar(
        select(SourceFile.id).where(
            SourceFile.repository_id == repository.id, SourceFile.path == "main.py"
        )
    )

    store_parse_result(db, repository, parsed_backend)
    rows = db.scalars(
        select(SourceFile.id).where(
            SourceFile.repository_id == repository.id, SourceFile.path == "main.py"
        )
    ).all()

    assert len(rows) == 1
    assert rows[0] != first


def test_parse_failure_is_recorded_on_the_repository(db: Session, repository):
    repository.url = "/definitely/not/a/directory"
    db.commit()

    parse_repository(repository.id, "/definitely/not/a/directory")

    db.expire(repository)
    assert repository.status == ParseStatus.FAILED
    assert "Not a directory" in repository.error_message


def test_parse_of_unknown_repository_is_a_noop():
    from uuid import uuid4

    parse_repository(uuid4(), str(BACKEND_DIR / "app"))
