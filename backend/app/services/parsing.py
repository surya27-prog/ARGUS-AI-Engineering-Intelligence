"""Runs the parser against an ingested repository and persists what it finds.

This is the seam between the two halves of ARGUS: `parser/` knows nothing about
FastAPI or SQLAlchemy, and the API knows nothing about ASTs.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import JobStatus, ParseJob, ParseStatus, Repository, SourceFile, Symbol
from app.services.cochange import ingest_history
from app.services.graph_writer import write_parsed_repo
from app.services.vector_writer import write_repo_vectors
from parser import IngestError, ParsedRepo, analyze_repo

logger = logging.getLogger(__name__)
settings = get_settings()


@contextmanager
def _stage(job: ParseJob, name: str) -> Iterator[None]:
    """Time one pipeline stage onto `job.stage_ms`.

    Records on the way out whether or not the body raised, so a failed parse
    still says how long the stage that failed had been running — which is the
    case where the timing matters most.
    """
    begin = time.perf_counter()
    try:
        yield
    finally:
        elapsed = int((time.perf_counter() - begin) * 1000)
        # Reassigned rather than mutated: SQLAlchemy does not track in-place
        # changes to a JSONB dict, so `job.stage_ms[name] = x` would not persist.
        job.stage_ms = {**(job.stage_ms or {}), name: elapsed}


def parse_repository(repository_id: UUID, source: str) -> None:
    """Parse `source` and store the result against `repository_id`.

    Runs in a background task, so it owns its session and swallows every
    exception into the job and repository rows — there is no request left to
    raise into. Every run creates a `ParseJob`, including the ones that fail
    immediately: a run that left no trace is indistinguishable from one that
    never started.
    """
    with SessionLocal() as db:
        repository = db.get(Repository, repository_id)
        if repository is None:
            logger.warning("Parse requested for unknown repository %s", repository_id)
            return

        job = ParseJob(
            repository_id=repository.id,
            run_id=uuid4(),
            source=source,
            status=JobStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        db.add(job)
        repository.status = ParseStatus.PARSING
        repository.error_message = None
        db.commit()

        started = time.perf_counter()

        try:
            with _stage(job, "analyze"):
                parsed = analyze_repo(
                    source,
                    workspace_dir=settings.workspace_dir,
                    max_size_mb=settings.max_repo_size_mb,
                    timeout_seconds=settings.parse_timeout_seconds,
                    force=True,
                    # A clone has to bring the commits down before anything can
                    # read them; Week 1's depth-1 clone would leave co-change
                    # with a single commit and nothing to pair.
                    history_depth=settings.history_depth,
                )
        except IngestError as exc:
            _fail(db, repository, job, str(exc), stage="ingest", started=started)
            return
        except Exception as exc:  # noqa: BLE001 - recorded on the row, not raised
            logger.exception("Parse failed for repository %s", repository_id)
            _fail(
                db,
                repository,
                job,
                f"{type(exc).__name__}: {exc}",
                stage="parse",
                started=started,
            )
            return

        job.file_count = parsed.inventory.file_count
        job.symbol_count = parsed.symbol_count
        job.import_count = parsed.import_count
        job.call_count = parsed.call_count
        job.failed_file_count = len(parsed.failed_files)

        try:
            # Stays `parsing` until the graph exists — see below.
            with _stage(job, "store"):
                store_parse_result(db, repository, parsed, mark_complete=False)
        except Exception as exc:  # noqa: BLE001 - recorded on the row, not raised
            logger.exception("Storing the parse failed for repository %s", repository_id)
            _fail(
                db,
                repository,
                job,
                f"{type(exc).__name__}: {exc}",
                stage="store",
                started=started,
            )
            return

        # The graph is a required output from Week 2 on, so a failure here is a
        # failed parse rather than a warning: reporting "complete" for a repo
        # with no graph would make every dependency endpoint return an empty
        # result with nothing to explain why.
        try:
            # The job's run_id is the graph's stamp, which is what lets a row
            # here name the exact subgraph it produced.
            with _stage(job, "graph"):
                result = write_parsed_repo(repository.id, parsed, run_id=str(job.run_id))
        except Exception as exc:  # noqa: BLE001 - recorded on the row, not raised
            logger.exception("Graph write failed for repository %s", repository_id)
            _fail(
                db,
                repository,
                job,
                f"graph write failed — {type(exc).__name__}: {exc}",
                stage="graph",
                started=started,
            )
            return

        job.graph_nodes = result.nodes_written
        job.graph_relationships = result.relationships_written
        job.graph_nodes_deleted = result.nodes_deleted
        job.graph_relationships_deleted = result.relationships_deleted

        # Co-change is an enrichment on top of a graph that is already correct
        # without it, and half the sources that reach here — zips, exported
        # directories — have no history to read at all. So a failure is logged
        # and the parse continues, unlike the graph write above.
        try:
            with _stage(job, "cochange"):
                coupling = ingest_history(repository.id, parsed, run_id=str(job.run_id))
            job.commits_analyzed = coupling.commits_used
            job.cochange_edges = coupling.edges_written
        except Exception:  # noqa: BLE001 - an enrichment, not a required output
            logger.exception("Co-change analysis failed for repository %s", repository_id)

        # Embedding is skipped without credentials rather than failing the
        # parse. Weeks 1-2 are fully useful with no embedding provider — files,
        # symbols and the whole dependency graph — and making a key mandatory
        # would break every one of those flows for anyone who has not set one.
        if settings.has_embedding_credentials:
            try:
                with _stage(job, "vectors"):
                    vectors = write_repo_vectors(
                        repository.id, parsed, run_id=str(job.run_id)
                    )
            except Exception as exc:  # noqa: BLE001 - recorded on the row, not raised
                logger.exception("Embedding failed for repository %s", repository_id)
                _fail(
                    db,
                    repository,
                    job,
                    f"embedding failed — {type(exc).__name__}: {exc}",
                    stage="vectors",
                    started=started,
                )
                return
            job.chunk_count = vectors.chunks_written
            job.embedding_tokens = vectors.input_tokens
            job.embedding_model = vectors.embedding_model
            job.vectors_deleted = vectors.points_deleted
        else:
            logger.info(
                "No embedding credentials; skipping the vector pass for %s", repository_id
            )

        job.status = JobStatus.COMPLETE
        _finish(job, started)

        # `complete` is published last, in the same commit as the finished job.
        # A poller that sees `complete` is guaranteed a written graph and a
        # populated job row behind it.
        repository.status = ParseStatus.COMPLETE
        repository.parsed_at = datetime.now(UTC)
        db.commit()


def store_parse_result(
    db: Session, repository: Repository, parsed: ParsedRepo, *, mark_complete: bool = True
) -> None:
    """Replace a repository's parsed rows with a fresh result, atomically.

    `mark_complete=False` leaves the repository in `parsing`, which is what the
    full pipeline needs: the graph write still has to happen, and a repository
    that says `complete` before its graph exists sends a polling UI straight to
    an empty `/dependents`.
    """
    # Re-parsing is a replace, not a merge: stale paths must not survive.
    db.execute(delete(SourceFile).where(SourceFile.repository_id == repository.id))

    # The inventory carries on-disk metrics the extractor does not; index it
    # once rather than scanning it per file.
    inventory_by_path = {info.path: info for info in parsed.inventory.files}

    for parsed_file in parsed.files:
        source_file = SourceFile(
            repository_id=repository.id,
            path=parsed_file.path,
            module=parsed_file.module,
            language=str(parsed_file.language),
            extension=Path(parsed_file.path).suffix,
            symbol_count=len(parsed_file.symbols),
            import_count=len(parsed_file.imports),
            call_count=len(parsed_file.calls),
            parse_error=parsed_file.error,
        )
        if (info := inventory_by_path.get(parsed_file.path)) is not None:
            source_file.size_bytes = info.size_bytes
            source_file.line_count = info.line_count
            source_file.sha256 = info.sha256

        source_file.symbols = [
            Symbol(
                repository_id=repository.id,
                name=symbol.name,
                qualname=symbol.qualname,
                kind=str(symbol.kind),
                module=symbol.module,
                parent=symbol.parent,
                line_start=symbol.line_start,
                line_end=symbol.line_end,
                docstring=symbol.docstring,
                returns=symbol.returns,
                is_async=symbol.is_async,
                complexity=symbol.complexity,
                decorators=list(symbol.decorators),
                parameters=[asdict(p) for p in symbol.parameters],
                base_classes=list(symbol.base_classes),
            )
            for symbol in parsed_file.symbols
        ]
        db.add(source_file)

    inventory = parsed.inventory
    repository.commit_sha = inventory.commit_sha
    repository.default_branch = inventory.default_branch
    repository.file_count = inventory.file_count
    repository.symbol_count = parsed.symbol_count
    repository.error_message = None
    if mark_complete:
        repository.status = ParseStatus.COMPLETE
        repository.parsed_at = datetime.now(UTC)
    db.commit()


def _fail(
    db: Session,
    repository: Repository,
    job: ParseJob,
    message: str,
    *,
    stage: str,
    started: float,
) -> None:
    """Record a failure on both rows: the repo's current state and the run's history."""
    repository.status = ParseStatus.FAILED
    repository.error_message = message
    job.status = JobStatus.FAILED
    job.error_message = message
    job.failed_stage = stage
    _finish(job, started)
    db.commit()


def _finish(job: ParseJob, started: float) -> None:
    job.finished_at = datetime.now(UTC)
    job.duration_ms = int((time.perf_counter() - started) * 1000)
