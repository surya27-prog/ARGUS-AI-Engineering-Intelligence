"""Parse job history.

`Repository.status` answers "what is the state of this repo right now". It
cannot answer "how long did that take", "which write produced the graph I am
looking at", or "what did the run that failed yesterday say" — a re-parse
overwrites all three. So each run gets its own immutable row.

The load-bearing column is `run_id`. The graph writer stamps every node and
edge it writes with it, so this table is the only bridge between a Postgres
record and the Neo4j subgraph that record produced.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class ParseJob(Base):
    """One parse run: what it read, what it wrote, and how long it took."""

    __tablename__ = "parse_jobs"
    __table_args__ = (
        # The job list is always "this repo, newest first".
        Index("ix_parse_jobs_repo_created", "repository_id", "created_at"),
        Index("ix_parse_jobs_run", "run_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    # Minted here rather than inside the writer, so the row exists and is
    # queryable while the write it names is still running.
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )

    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[JobStatus] = mapped_column(
        String(20), nullable=False, default=JobStatus.PENDING
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which stage failed — `ingest`, `parse`, `store` or `graph`. A failure
    # message alone does not say whether there is usable data behind it.
    failed_stage: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # What the parser found.
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    symbol_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    import_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # What the graph write did. The deleted counts are the stamp-and-sweep
    # result: nonzero means this run removed something a previous run wrote.
    graph_nodes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    graph_relationships: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    graph_nodes_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    graph_relationships_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    repository: Mapped["Repository"] = relationship(back_populates="jobs")  # noqa: F821

    def __repr__(self) -> str:
        return f"<ParseJob {self.run_id} ({self.status})>"
