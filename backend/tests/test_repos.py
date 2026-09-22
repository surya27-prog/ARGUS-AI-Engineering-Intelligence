"""Endpoint tests for repository ingestion and browsing.

The parse itself is stubbed out here — `TestClient` runs background tasks
synchronously, so a real URL would clone from the network mid-test. Parsing is
covered against real source in `test_parsing.py`.
"""

import io
import uuid
import zipfile

import pytest
from sqlalchemy.orm import Session

from app.api import repos as repos_api
from app.models import ParseStatus, Repository, SourceFile, Symbol


@pytest.fixture
def stub_parse(monkeypatch) -> list[tuple]:
    """Record parse requests instead of running them."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        repos_api, "parse_repository", lambda repo_id, source: calls.append((repo_id, source))
    )
    return calls


@pytest.fixture
def cleanup(db: Session):
    created: list[uuid.UUID] = []
    yield created
    for repo_id in created:
        if (repo := db.get(Repository, repo_id)) is not None:
            db.delete(repo)
    db.commit()


def test_create_repository_accepts_url_and_queues_parse(client, stub_parse, cleanup):
    response = client.post("/repos", json={"url": "https://github.com/psf/requests.git"})

    assert response.status_code == 202
    body = response.json()
    cleanup.append(uuid.UUID(body["id"]))

    assert body["name"] == "requests"
    assert body["status"] == ParseStatus.PENDING
    assert body["file_count"] == 0
    assert stub_parse == [(uuid.UUID(body["id"]), "https://github.com/psf/requests.git")]


def test_create_repository_rejects_non_url(client, stub_parse):
    response = client.post("/repos", json={"url": "not-a-url"})
    assert response.status_code == 422
    assert not stub_parse


def test_create_repository_rejects_empty_url(client, stub_parse):
    assert client.post("/repos", json={"url": "   "}).status_code == 422
    assert not stub_parse


def test_custom_name_overrides_derived_one(client, stub_parse, cleanup):
    body = client.post(
        "/repos", json={"url": "https://github.com/psf/requests", "name": "my-fork"}
    ).json()
    cleanup.append(uuid.UUID(body["id"]))
    assert body["name"] == "my-fork"


def test_upload_requires_a_zip(client, stub_parse):
    response = client.post(
        "/repos/upload", files={"file": ("repo.tar.gz", io.BytesIO(b"nope"), "application/gzip")}
    )
    assert response.status_code == 400
    assert "zip" in response.json()["detail"].lower()
    assert not stub_parse


def test_upload_stages_the_archive_and_queues_parse(client, stub_parse, cleanup, tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("sample/app.py", "def hello():\n    return 1\n")
    buffer.seek(0)

    response = client.post(
        "/repos/upload", files={"file": ("sample.zip", buffer, "application/zip")}
    )

    assert response.status_code == 202
    body = response.json()
    cleanup.append(uuid.UUID(body["id"]))

    assert body["name"] == "sample"
    assert body["url"] is None
    assert len(stub_parse) == 1
    queued_id, archive = stub_parse[0]
    assert queued_id == uuid.UUID(body["id"])
    assert archive.endswith(f"{body['id']}.zip")


def test_get_repository_404s_for_unknown_id(client):
    response = client.get(f"/repos/{uuid.uuid4()}")
    assert response.status_code == 404


def test_list_repositories_is_paginated(client, repository):
    body = client.get("/repos?limit=1").json()
    assert body["limit"] == 1
    assert body["offset"] == 0
    assert body["total"] >= 1
    assert len(body["items"]) <= 1


def test_files_and_symbols_start_empty(client, repository):
    files = client.get(f"/repos/{repository.id}/files").json()
    symbols = client.get(f"/repos/{repository.id}/symbols").json()
    assert files["items"] == []
    assert files["total"] == 0
    assert symbols["items"] == []


def test_file_and_symbol_listing_with_filters(client, db: Session, repository):
    source_file = SourceFile(
        repository_id=repository.id,
        path="app/core/config.py",
        module="app.core.config",
        line_count=50,
        symbol_count=2,
    )
    source_file.symbols = [
        Symbol(
            repository_id=repository.id,
            name="Settings",
            qualname="Settings",
            kind="class",
            module="app.core.config",
            line_start=6,
            line_end=45,
            base_classes=["BaseSettings"],
        ),
        Symbol(
            repository_id=repository.id,
            name="get_settings",
            qualname="get_settings",
            kind="function",
            module="app.core.config",
            line_start=49,
            line_end=50,
            parameters=[],
        ),
    ]
    db.add(source_file)
    db.commit()

    files = client.get(f"/repos/{repository.id}/files").json()
    assert [f["path"] for f in files["items"]] == ["app/core/config.py"]
    assert files["items"][0]["module"] == "app.core.config"

    assert client.get(f"/repos/{repository.id}/files?search=config").json()["total"] == 1
    assert client.get(f"/repos/{repository.id}/files?search=nothing").json()["total"] == 0

    classes = client.get(f"/repos/{repository.id}/symbols?kind=class").json()
    assert [s["qualname"] for s in classes["items"]] == ["Settings"]
    assert classes["items"][0]["base_classes"] == ["BaseSettings"]

    by_file = client.get(
        f"/repos/{repository.id}/symbols?file_id={files['items'][0]['id']}"
    ).json()
    assert by_file["total"] == 2

    found = client.get(f"/repos/{repository.id}/symbols?search=get_set").json()
    assert [s["qualname"] for s in found["items"]] == ["get_settings"]


def test_reparse_rejects_uploaded_repositories(client, repository, stub_parse):
    response = client.post(f"/repos/{repository.id}/reparse")
    assert response.status_code == 409
    assert not stub_parse


def test_reparse_requeues_a_url_repository(client, db: Session, stub_parse, cleanup):
    repo = Repository(
        name="requests", url="https://github.com/psf/requests.git", status=ParseStatus.COMPLETE
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    cleanup.append(repo.id)

    response = client.post(f"/repos/{repo.id}/reparse")
    assert response.status_code == 202
    assert response.json()["status"] == ParseStatus.PENDING
    assert stub_parse == [(repo.id, "https://github.com/psf/requests.git")]


def test_delete_repository_removes_its_rows(client, db: Session, stub_parse):
    body = client.post("/repos", json={"url": "https://github.com/psf/requests"}).json()
    repo_id = uuid.UUID(body["id"])

    source_file = SourceFile(repository_id=repo_id, path="a.py")
    source_file.symbols = [
        Symbol(repository_id=repo_id, name="f", qualname="f", kind="function")
    ]
    db.add(source_file)
    db.commit()
    file_id = source_file.id

    assert client.delete(f"/repos/{repo_id}").status_code == 204
    assert client.get(f"/repos/{repo_id}").status_code == 404
    # ON DELETE CASCADE has to clean up files and symbols too.
    db.expire_all()
    assert db.get(SourceFile, file_id) is None
