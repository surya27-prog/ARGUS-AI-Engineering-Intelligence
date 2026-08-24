"""Repository ingestion and browsing.

`POST /repos` returns immediately with a `pending` repository and parses in the
background — a real repo takes minutes, which is far longer than any sensible
HTTP timeout. The UI polls `GET /repos/{id}` until the status settles.
"""

import logging
import shutil
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.cache import invalidate_all
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.models import ParseJob, ParseStatus, Repository, SourceFile, Symbol
from app.schemas.repository import (
    Page,
    ParseJobOut,
    RepositoryCreate,
    RepositoryOut,
    SourceFileOut,
    SymbolOut,
)
from app.services.graph_writer import delete_repo_graph
from app.services.parsing import parse_repository
from app.services.vector_writer import delete_repo_vectors

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/repos", tags=["repositories"])


@router.post("", response_model=RepositoryOut, status_code=status.HTTP_202_ACCEPTED)
def create_repository(
    payload: RepositoryCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> Repository:
    """Ingest a repository from a git URL."""
    repository = Repository(
        name=payload.name or _name_from_url(payload.url),
        url=payload.url,
        status=ParseStatus.PENDING,
    )
    db.add(repository)
    db.commit()
    db.refresh(repository)

    background_tasks.add_task(parse_repository, repository.id, payload.url)
    return repository


@router.post("/upload", response_model=RepositoryOut, status_code=status.HTTP_202_ACCEPTED)
def upload_repository(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Zip archive of a repository"),
    name: str | None = Form(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Repository:
    """Ingest a repository from an uploaded zip archive."""
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Upload must be a .zip archive")

    repository = Repository(
        name=name or Path(file.filename or "upload.zip").stem,
        url=None,
        status=ParseStatus.PENDING,
    )
    db.add(repository)
    db.commit()
    db.refresh(repository)

    # Stream to disk rather than reading into memory — uploads can be hundreds
    # of megabytes, and the parser wants a path anyway.
    uploads = Path(settings.workspace_dir).expanduser() / "_uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    archive = uploads / f"{repository.id}.zip"
    with archive.open("wb") as destination:
        shutil.copyfileobj(file.file, destination)

    background_tasks.add_task(parse_repository, repository.id, str(archive))
    return repository


@router.get("", response_model=Page[RepositoryOut])
def list_repositories(
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[RepositoryOut]:
    total = db.scalar(select(func.count()).select_from(Repository)) or 0
    rows = db.scalars(
        select(Repository).order_by(Repository.created_at.desc()).limit(limit).offset(offset)
    ).all()
    return Page[RepositoryOut](
        items=[RepositoryOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{repository_id}", response_model=RepositoryOut)
def get_repository(repository_id: uuid.UUID, db: Session = Depends(get_db)) -> Repository:
    return _require_repository(db, repository_id)


@router.get("/{repository_id}/files", response_model=Page[SourceFileOut])
def list_files(
    repository_id: uuid.UUID,
    db: Session = Depends(get_db),
    search: str | None = Query(default=None, description="Case-insensitive path substring"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> Page[SourceFileOut]:
    _require_repository(db, repository_id)

    conditions = [SourceFile.repository_id == repository_id]
    if search:
        conditions.append(SourceFile.path.ilike(f"%{search}%"))

    total = db.scalar(select(func.count()).select_from(SourceFile).where(*conditions)) or 0
    rows = db.scalars(
        select(SourceFile).where(*conditions).order_by(SourceFile.path).limit(limit).offset(offset)
    ).all()
    return Page[SourceFileOut](
        items=[SourceFileOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{repository_id}/symbols", response_model=Page[SymbolOut])
def list_symbols(
    repository_id: uuid.UUID,
    db: Session = Depends(get_db),
    file_id: uuid.UUID | None = Query(default=None, description="Restrict to one file"),
    kind: str | None = Query(default=None, description="class | function | method"),
    search: str | None = Query(default=None, description="Case-insensitive qualname substring"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> Page[SymbolOut]:
    _require_repository(db, repository_id)

    conditions = [Symbol.repository_id == repository_id]
    if file_id:
        conditions.append(Symbol.file_id == file_id)
    if kind:
        conditions.append(Symbol.kind == kind)
    if search:
        conditions.append(Symbol.qualname.ilike(f"%{search}%"))

    total = db.scalar(select(func.count()).select_from(Symbol).where(*conditions)) or 0
    rows = db.scalars(
        select(Symbol)
        .where(*conditions)
        .order_by(Symbol.module, Symbol.line_start)
        .limit(limit)
        .offset(offset)
    ).all()
    return Page[SymbolOut](
        items=[SymbolOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{repository_id}/jobs", response_model=Page[ParseJobOut])
def list_jobs(
    repository_id: uuid.UUID,
    db: Session = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Page[ParseJobOut]:
    """Parse history, newest first.

    `Repository.status` is overwritten by every re-parse; this is where the run
    that failed yesterday is still readable.
    """
    _require_repository(db, repository_id)

    conditions = [ParseJob.repository_id == repository_id]
    total = db.scalar(select(func.count()).select_from(ParseJob).where(*conditions)) or 0
    rows = db.scalars(
        select(ParseJob)
        .where(*conditions)
        .order_by(ParseJob.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return Page[ParseJobOut](
        items=[ParseJobOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{repository_id}/jobs/latest", response_model=ParseJobOut)
def latest_job(repository_id: uuid.UUID, db: Session = Depends(get_db)) -> ParseJob:
    """The current or most recent run — what a polling UI actually wants."""
    _require_repository(db, repository_id)
    job = db.scalars(
        select(ParseJob)
        .where(ParseJob.repository_id == repository_id)
        .order_by(ParseJob.created_at.desc())
        .limit(1)
    ).first()
    if job is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Repository {repository_id} has never been parsed"
        )
    return job


@router.post("/{repository_id}/reparse", response_model=RepositoryOut, status_code=202)
def reparse_repository(
    repository_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> Repository:
    """Re-run the parser against a repository's original source."""
    repository = _require_repository(db, repository_id)
    if not repository.url:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Uploaded repositories have no source to re-fetch; upload the zip again",
        )

    repository.status = ParseStatus.PENDING
    db.commit()
    db.refresh(repository)

    background_tasks.add_task(parse_repository, repository.id, repository.url)
    return repository


@router.delete("/{repository_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_repository(repository_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    repository = _require_repository(db, repository_id)

    # Postgres cascades its own rows, but nothing cascades into Neo4j or
    # Qdrant. Without this the subgraph and the vectors outlive the repository
    # that owns them, and both are repo-scoped so nothing would ever reach them
    # again. Neither failure blocks the delete.
    try:
        delete_repo_graph(repository_id)
    except Exception:  # noqa: BLE001 - a live graph must not block the delete
        logger.exception("Could not clear the graph for repository %s", repository_id)
    try:
        delete_repo_vectors(repository_id)
    except Exception:  # noqa: BLE001 - same reasoning as the graph above
        logger.exception("Could not clear the vectors for repository %s", repository_id)

    db.delete(repository)
    db.commit()

    # `parsed_at` keying retires stale cache entries by itself, but a deleted
    # repository has no next parse to retire them — so clear it explicitly
    # rather than leaving its scans in memory until they are evicted.
    invalidate_all(repository_id)


def _require_repository(db: Session, repository_id: uuid.UUID) -> Repository:
    repository = db.get(Repository, repository_id)
    if repository is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No repository {repository_id}")
    return repository


def _name_from_url(url: str) -> str:
    tail = url.rstrip("/").split("/")[-1].split(":")[-1]
    return tail.removesuffix(".git") or "repository"
