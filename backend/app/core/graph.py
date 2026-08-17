"""Neo4j driver lifecycle and the schema bootstrap.

The Postgres twin of this module is `app.core.database`. The one structural
difference: the driver is built lazily rather than at import, because the API
has to start and serve `/health` when the graph is down — importing this module
must never be what fails.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from functools import lru_cache

from neo4j import Driver, GraphDatabase, Session

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Straight out of docs/architecture/graph-schema.md. Every statement is
# `IF NOT EXISTS`, so calling ensure_schema() on every parse costs one round
# trip after the first run. A uniqueness constraint creates its own index, so
# `key` needs no separate index here.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    "CREATE CONSTRAINT repo_key   IF NOT EXISTS FOR (n:Repo)     REQUIRE n.key IS UNIQUE",
    "CREATE CONSTRAINT file_key   IF NOT EXISTS FOR (n:File)     REQUIRE n.key IS UNIQUE",
    "CREATE CONSTRAINT class_key  IF NOT EXISTS FOR (n:Class)    REQUIRE n.key IS UNIQUE",
    "CREATE CONSTRAINT func_key   IF NOT EXISTS FOR (n:Function) REQUIRE n.key IS UNIQUE",
    "CREATE CONSTRAINT module_key IF NOT EXISTS FOR (n:Module)   REQUIRE n.key IS UNIQUE",
    # Sweeping and every repo-scoped query filter on repo_id.
    "CREATE INDEX file_repo   IF NOT EXISTS FOR (n:File)     ON (n.repo_id)",
    "CREATE INDEX class_repo  IF NOT EXISTS FOR (n:Class)    ON (n.repo_id)",
    "CREATE INDEX func_repo   IF NOT EXISTS FOR (n:Function) ON (n.repo_id)",
    "CREATE INDEX module_repo IF NOT EXISTS FOR (n:Module)   ON (n.repo_id)",
    # Symbol lookup by name is how the UI and chat citations enter the graph.
    "CREATE INDEX func_qualname  IF NOT EXISTS FOR (n:Function) ON (n.repo_id, n.qualname)",
    "CREATE INDEX class_qualname IF NOT EXISTS FOR (n:Class)    ON (n.repo_id, n.qualname)",
    "CREATE INDEX file_path      IF NOT EXISTS FOR (n:File)     ON (n.repo_id, n.path)",
)


@lru_cache
def get_driver() -> Driver:
    """The process-wide driver. Cheap to call; the driver pools its own connections."""
    settings = get_settings()
    return GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
        # Same reasoning as the psycopg connect_timeout in app.core.database:
        # the driver's default is 60s, which would hang a request rather than
        # report the graph as down.
        connection_acquisition_timeout=settings.neo4j_timeout_seconds,
        max_connection_lifetime=3600,
    )


@contextmanager
def graph_session() -> Generator[Session, None, None]:
    """A session against the configured database, closed on the way out."""
    session = get_driver().session(database=get_settings().neo4j_database)
    try:
        yield session
    finally:
        session.close()


def ensure_schema() -> None:
    """Create the constraints and indexes. Idempotent, so a parse can just call it."""
    with graph_session() as session:
        for statement in SCHEMA_STATEMENTS:
            session.run(statement)
    logger.debug("Neo4j schema ensured (%d statements)", len(SCHEMA_STATEMENTS))


def check_connectivity() -> str:
    """`up`, or `down: <reason>` — shaped for the /health payload, never raises."""
    try:
        get_driver().verify_connectivity()
    except Exception as exc:  # noqa: BLE001 - surfaced in the payload, not raised
        return f"down: {type(exc).__name__}"
    return "up"


def close_driver() -> None:
    """Release the pooled connections. Called on app shutdown and by tests."""
    if get_driver.cache_info().currsize:
        get_driver().close()
        get_driver.cache_clear()
