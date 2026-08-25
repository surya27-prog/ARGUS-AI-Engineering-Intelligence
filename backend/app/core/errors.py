"""One error shape for the whole API, and a correlation id on every 500.

Two problems this fixes.

**A dependency being down looked like a bug.** Neo4j unreachable, Postgres
refusing connections and a missing API key all surfaced as
`500 Internal Server Error` — indistinguishable from ARGUS having a defect, and
useless to anyone deciding whether to retry. They are 503s with a sentence
naming the store, because "the graph database is not reachable" is actionable and
"internal server error" is not.

**An unexpected error left the user nothing to report.** Every 500 now carries a
short `error_id` that is also in the log line, so "it broke, id 7f3a9c21" is
enough to find the traceback. The traceback itself never crosses the wire: it
names file paths, library versions and sometimes query text.

Every response keeps FastAPI's `detail` key so existing clients — including this
project's own `frontend/src/lib/api.ts` — need no change.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from neo4j.exceptions import Neo4jError, ServiceUnavailable
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.services.providers import ProviderError

logger = logging.getLogger(__name__)


def _json(status_code: int, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail, **extra})


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers. Called once, from `main`."""

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        """422 with the field names, not the raw pydantic dump.

        FastAPI's default includes the input value, which for `POST /repos` means
        echoing whatever was submitted straight back — and for a body carrying a
        URL with credentials in it, that is the kind of echo that ends up in a
        log aggregator.
        """
        problems = []
        for error in exc.errors():
            # `loc` is ("body", "url") or ("query", "limit"); the last element is
            # the part a caller can act on.
            location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
            message = error.get("msg", "invalid value")
            problems.append(f"{location}: {message}" if location else message)
        return _json(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "; ".join(problems) or "The request could not be validated",
        )

    @app.exception_handler(ProviderError)
    async def _provider(request: Request, exc: ProviderError) -> JSONResponse:
        logger.warning("Provider %s unavailable: %s", exc.provider, exc)
        return _json(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"The {exc.provider} provider is not available — {exc}",
        )

    @app.exception_handler(ServiceUnavailable)
    async def _neo4j_down(request: Request, exc: ServiceUnavailable) -> JSONResponse:
        logger.error("Neo4j unreachable on %s: %s", request.url.path, exc)
        return _json(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The graph database is not reachable. Dependency, impact and debt "
            "answers need it; files and symbols do not.",
        )

    @app.exception_handler(Neo4jError)
    async def _neo4j_error(request: Request, exc: Neo4jError) -> JSONResponse:
        # A malformed query is ours to fix, so it keeps a 500 and an id rather
        # than being reported as the database's fault.
        error_id = _log_unexpected(request, exc)
        return _json(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "A graph query failed. This is a bug in ARGUS, not in your repository.",
            error_id=error_id,
        )

    @app.exception_handler(OperationalError)
    async def _postgres_down(request: Request, exc: OperationalError) -> JSONResponse:
        logger.error("Postgres unreachable on %s: %s", request.url.path, exc)
        return _json(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The database is not reachable. Try again once it is back.",
        )

    @app.exception_handler(SQLAlchemyError)
    async def _database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        error_id = _log_unexpected(request, exc)
        return _json(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "A database operation failed.",
            error_id=error_id,
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        error_id = _log_unexpected(request, exc)
        return _json(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Something went wrong. Quote error id {error_id} if you report this.",
            error_id=error_id,
        )


def _log_unexpected(request: Request, exc: BaseException) -> str:
    """Log the traceback against a short id, and return the id.

    Eight hex characters: long enough not to collide within a log window, short
    enough that someone can read it off a screen and type it into an issue.
    """
    error_id = uuid.uuid4().hex[:8]
    logger.exception(
        "Unhandled %s on %s %s [error_id=%s]",
        type(exc).__name__,
        request.method,
        request.url.path,
        error_id,
    )
    return error_id


__all__ = ["install_error_handlers"]
