"""Runs the parser against an ingested repository and persists what it finds.

This is the seam between the two halves of ARGUS: `parser/` knows nothing about
FastAPI or SQLAlchemy, and the API knows nothing about ASTs.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import ParseStatus, Repository, SourceFile, Symbol

# The parser is a sibling package, not a published distribution. Week 6 turns it
# into a real editable install; until then the backend puts it on the path here,
# in the one module that imports it.
_PARSER_DIR = Path(__file__).resolve().parents[3] / "parser"
if str(_PARSER_DIR) not in sys.path:
    sys.path.insert(0, str(_PARSER_DIR))

from parser import IngestError, ParsedRepo, analyze_repo  # noqa: E402

logger = logging.getLogger(__name__)
settings = get_settings()


def parse_repository(repository_id: UUID, source: str) -> None:
    """Parse `source` and store the result against `repository_id`.

    Runs in a background task, so it owns its session and swallows every
    exception into the repository row — there is no request left to raise into.
    """
    with SessionLocal() as db:
        repository = db.get(Repository, repository_id)
        if repository is None:
            logger.warning("Parse requested for unknown repository %s", repository_id)
            return

        repository.status = ParseStatus.PARSING
        repository.error_message = None
        db.commit()

        try:
            parsed = analyze_repo(
                source,
                workspace_dir=settings.workspace_dir,
                max_size_mb=settings.max_repo_size_mb,
                timeout_seconds=settings.parse_timeout_seconds,
                force=True,
            )
        except IngestError as exc:
            _fail(db, repository, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - recorded on the row, not raised
            logger.exception("Parse failed for repository %s", repository_id)
            _fail(db, repository, f"{type(exc).__name__}: {exc}")
            return

        store_parse_result(db, repository, parsed)


def store_parse_result(db: Session, repository: Repository, parsed: ParsedRepo) -> None:
    """Replace a repository's parsed rows with a fresh result, atomically."""
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
    repository.status = ParseStatus.COMPLETE
    repository.parsed_at = datetime.now(UTC)
    repository.error_message = None
    db.commit()


def _fail(db: Session, repository: Repository, message: str) -> None:
    repository.status = ParseStatus.FAILED
    repository.error_message = message
    db.commit()
