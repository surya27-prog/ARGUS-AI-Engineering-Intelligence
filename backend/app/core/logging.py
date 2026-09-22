"""Logging configuration and per-request correlation.

Until now `LOG_LEVEL` was a setting named in five config files and wired to
nothing. The root logger therefore sat at its default, so every
`logger.info(...)` in the codebase was dropped, and the `logger.warning` and
`logger.exception` calls that did get through went out via Python's last-resort
handler — no timestamp, no logger name. That makes the error id from
`core/errors.py` close to useless: "quote error id 7f3a9c21" only helps if the
line carrying it can be found by time.

Two things here:

**One id per request, not per crash.** `errors.py` minted an id only when
something raised. A request id assigned on the way in covers every request, is
returned as `X-Request-ID`, and is what the error handler reports when there is a
failure — so a user can point at *any* request, not only one that 500'd, and the
same string appears in the access line, the application logs and the response.

**JSON in production, plain text in development.** A log aggregator wants one
object per line; a human at a terminal does not. The format follows `APP_ENV`
rather than being a separate setting, because there is no case where you want
JSON on your own machine.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

# The id of the request being served on this task. A ContextVar rather than a
# thread-local because Starlette serves concurrently on one event loop, where a
# thread-local would leak one request's id into another's log lines.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

# Paths that would otherwise fill the log with nothing. A health check every 30
# seconds from the platform, plus whatever the orchestrator adds, is noise that
# makes the real traffic harder to read.
QUIET_PATHS = frozenset({"/health", "/"})


def current_request_id() -> str:
    """The id of the request being handled, or "" outside a request."""
    return request_id_var.get()


class RequestIdFilter(logging.Filter):
    """Attaches the request id to every record, so formatters can use it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    Hand-rolled rather than pulling in a dependency: the fields are fixed and few,
    and a formatter that cannot raise is worth more here than one that is
    configurable — an exception inside logging while handling an exception is a
    genuinely unpleasant thing to debug.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        # Anything the caller attached with `extra=` — the access line's method,
        # path, status and duration arrive this way.
        for key, value in getattr(record, "__dict__", {}).items():
            if key.startswith("argus_"):
                payload[key[len("argus_") :]] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    """Install the root handler. Idempotent — safe to call more than once.

    Uvicorn configures its own loggers before the app is imported, so those are
    pointed at this handler too rather than left formatting differently. Nothing
    is more confusing in a log than two shapes of line for the same request.
    """
    resolved = getattr(logging, level.upper(), logging.INFO)

    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        JsonFormatter()
        if json_output
        else logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s [%(request_id)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    )

    root = logging.getLogger()
    # Replaced rather than appended: calling this twice would otherwise duplicate
    # every line, which is exactly what happens when a reload re-imports the app.
    root.handlers = [handler]
    root.setLevel(resolved)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True

    # SQLAlchemy echoes every statement at INFO when `echo=True`, which at INFO
    # would bury everything else. The engine's own `echo` flag controls whether
    # they are emitted; this keeps them out of the way when they are.
    logging.getLogger("sqlalchemy.engine").setLevel(max(resolved, logging.WARNING))


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, logs one access line, and times the request."""

    def __init__(self, app: ASGIApp, *, logger_name: str = "argus.access") -> None:
        super().__init__(app)
        self.logger = logging.getLogger(logger_name)

    async def dispatch(self, request: Request, call_next) -> Response:
        # An inbound id is honoured so a trace started at a proxy or a frontend
        # stays one trace. Truncated because it is echoed in a header and
        # attacker-controlled length is not something to pass along.
        incoming = request.headers.get("x-request-id", "")
        request_id = (incoming or uuid.uuid4().hex)[:64]
        token = request_id_var.set(request_id)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The exception handlers turn this into a response; this line exists
            # so the access log records the request happened at all, with its
            # duration, rather than the request simply vanishing.
            duration_ms = (time.perf_counter() - started) * 1000
            self.logger.exception(
                "%s %s failed after %.0fms",
                request.method,
                request.url.path,
                duration_ms,
                extra={
                    "argus_method": request.method,
                    "argus_path": request.url.path,
                    "argus_duration_ms": round(duration_ms, 1),
                },
            )
            raise
        finally:
            request_id_var.reset(token)

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id

        if request.url.path not in QUIET_PATHS:
            # A 5xx is logged at ERROR so a log level of WARNING in a quiet
            # production still shows failures; everything else is INFO.
            self.logger.log(
                logging.ERROR if response.status_code >= 500 else logging.INFO,
                "%s %s %d %.0fms",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                extra={
                    "argus_method": request.method,
                    "argus_path": request.url.path,
                    "argus_status": response.status_code,
                    "argus_duration_ms": round(duration_ms, 1),
                },
            )
        return response


__all__ = [
    "QUIET_PATHS",
    "JsonFormatter",
    "RequestContextMiddleware",
    "RequestIdFilter",
    "configure_logging",
    "current_request_id",
    "request_id_var",
]
