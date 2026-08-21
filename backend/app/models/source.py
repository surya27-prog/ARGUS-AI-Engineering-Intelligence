"""Persisted parser output: one row per source file, one per extracted symbol.

Week 2 moves the *relationships* between these into Neo4j. These tables stay as
the flat source of truth the list endpoints read, so browsing a repository never
depends on the graph being up.
"""

import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class SourceFile(Base):
    """A parseable file found in an ingested repository."""

    __tablename__ = "source_files"
    __table_args__ = (
        # Re-parsing a repo replaces its rows, so a path is unique per repo.
        Index("ix_source_files_repo_path", "repository_id", "path", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )

    path: Mapped[str] = mapped_column(Text, nullable=False)
    module: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str] = mapped_column(String(32), nullable=False, default="python")
    extension: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    symbol_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    import_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Set when the file could not be parsed; the row still exists so the UI can
    # show which files are missing from the analysis and why.
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    symbols: Mapped[list["Symbol"]] = relationship(
        back_populates="file", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"<SourceFile {self.path}>"


class Symbol(Base):
    """A class, function or method extracted from a source file."""

    __tablename__ = "symbols"
    __table_args__ = (
        Index("ix_symbols_repo_qualname", "repository_id", "qualname"),
        Index("ix_symbols_file", "file_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_files.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    qualname: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    module: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent: Mapped[str | None] = mapped_column(Text, nullable=True)

    line_start: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    line_end: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    docstring: Mapped[str | None] = mapped_column(Text, nullable=True)
    returns: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_async: Mapped[bool] = mapped_column(nullable=False, default=False)
    # McCabe complexity of this symbol's own body, from the parser. Stored here
    # rather than on the graph node because the debt detectors read it alongside
    # docstrings and line counts, which are Postgres-side facts.
    complexity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Kept as JSONB rather than child tables: nothing queries inside them yet,
    # and Week 3's chunker wants the whole signature back in one read.
    decorators: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    parameters: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    base_classes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    file: Mapped[SourceFile] = relationship(back_populates="symbols")

    def __repr__(self) -> str:
        return f"<Symbol {self.kind} {self.qualname}>"
