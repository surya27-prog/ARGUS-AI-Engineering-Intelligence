"""Logging configuration and request correlation.

Pure: a formatter is a function of its record and the middleware's contract is
observable through `TestClient`, which needs no store for the paths exercised
here.
"""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.logging import (
    JsonFormatter,
    RequestContextMiddleware,
    RequestIdFilter,
    configure_logging,
    current_request_id,
    request_id_var,
)

# --- the setting that used to do nothing -------------------------------------


def test_configure_logging_applies_the_level():
    """`LOG_LEVEL` was named in five config files and wired to nothing, so every
    `logger.info` in the codebase was dropped."""
    try:
        configure_logging("DEBUG")
        assert logging.getLogger().level == logging.DEBUG
        configure_logging("WARNING")
        assert logging.getLogger().level == logging.WARNING
    finally:
        configure_logging("INFO")


def test_an_unknown_level_falls_back_rather_than_raising():
    """A typo in an env var must not stop the process from starting."""
    try:
        configure_logging("NONSENSE")
        assert logging.getLogger().level == logging.INFO
    finally:
        configure_logging("INFO")


def test_calling_twice_does_not_duplicate_handlers():
    """A reload re-imports the app, and appending would double every line."""
    configure_logging("INFO")
    first = len(logging.getLogger().handlers)
    configure_logging("INFO")

    assert len(logging.getLogger().handlers) == first == 1


# --- the JSON formatter ------------------------------------------------------


def _record(**kwargs) -> logging.LogRecord:
    defaults = {
        "name": "argus.test",
        "level": logging.INFO,
        "pathname": __file__,
        "lineno": 1,
        "msg": "hello %s",
        "args": ("world",),
        "exc_info": None,
    }
    return logging.LogRecord(**{**defaults, **kwargs})


def test_json_formatter_emits_one_parseable_object():
    record = _record()
    RequestIdFilter().filter(record)

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "argus.test"
    assert payload["message"] == "hello world"
    assert "ts" in payload


def test_json_formatter_carries_the_extra_fields():
    """The access line's method, path, status and duration arrive via `extra=`."""
    record = _record()
    record.argus_status = 404
    record.argus_path = "/repos/x"
    RequestIdFilter().filter(record)

    payload = json.loads(JsonFormatter().format(record))

    assert payload["status"] == 404
    assert payload["path"] == "/repos/x"


def test_json_formatter_includes_a_traceback_when_there_is_one():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record(exc_info=sys.exc_info())
    RequestIdFilter().filter(record)

    payload = json.loads(JsonFormatter().format(record))

    assert "ValueError: boom" in payload["exception"]


def test_the_filter_supplies_a_placeholder_outside_a_request():
    record = _record()
    RequestIdFilter().filter(record)
    assert record.request_id == "-"


def test_json_formatter_survives_an_unserialisable_value():
    """A formatter that raises while formatting an exception is a bad place to be."""
    record = _record()
    record.argus_thing = object()

    payload = json.loads(JsonFormatter().format(record))

    assert "thing" in payload


# --- the middleware ----------------------------------------------------------


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ok")
    def ok() -> dict[str, str]:
        return {"seen": current_request_id()}

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("deliberate")

    return app


def test_every_response_carries_a_request_id():
    response = TestClient(_app()).get("/ok")

    assert response.status_code == 200
    assert response.headers["x-request-id"]


def test_the_handler_sees_the_same_id_the_client_is_given():
    """Otherwise the id in a log line and the id in the header are two ids."""
    response = TestClient(_app()).get("/ok")

    assert response.json()["seen"] == response.headers["x-request-id"]


def test_an_inbound_request_id_is_honoured():
    """A trace started at a proxy or the frontend stays one trace."""
    response = TestClient(_app()).get("/ok", headers={"X-Request-ID": "upstream-123"})

    assert response.headers["x-request-id"] == "upstream-123"
    assert response.json()["seen"] == "upstream-123"


def test_an_absurdly_long_inbound_id_is_truncated():
    """It is echoed back in a header, so its length is not the caller's choice."""
    response = TestClient(_app()).get("/ok", headers={"X-Request-ID": "x" * 500})

    assert len(response.headers["x-request-id"]) == 64


def test_ids_differ_between_requests():
    client = TestClient(_app())
    first = client.get("/ok").headers["x-request-id"]
    second = client.get("/ok").headers["x-request-id"]

    assert first != second


def test_the_context_is_reset_after_a_request():
    """A leaked id would attach one request's id to the next request's logs."""
    TestClient(_app()).get("/ok")

    assert request_id_var.get() == ""


def test_a_failing_request_is_logged_and_still_raises(caplog):
    client = TestClient(_app(), raise_server_exceptions=False)

    with caplog.at_level(logging.ERROR, logger="argus.access"):
        response = client.get("/boom")

    assert response.status_code == 500
    assert any("failed after" in r.getMessage() for r in caplog.records)


def test_the_access_log_records_status_and_duration(caplog):
    with caplog.at_level(logging.INFO, logger="argus.access"):
        TestClient(_app()).get("/ok")

    record = next(r for r in caplog.records if r.name == "argus.access")
    assert record.argus_status == 200
    assert record.argus_path == "/ok"
    assert record.argus_duration_ms >= 0


def test_health_is_not_logged():
    """A platform health check every 30 seconds would bury the real traffic."""
    from app.core.logging import QUIET_PATHS

    assert "/health" in QUIET_PATHS
