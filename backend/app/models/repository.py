import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ParseStatus(str, Enum):
    PENDING = "pending"
    PARSING = "parsing"
    COMPLETE = "complete"
    FAILED = "failed"


class Repository(Base):
    """An ingested repository and the state of its parse job."""

    __tablename__ = "repositories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)

    status: Mapped[ParseStatus] = mapped_column(
        String(20), nullable=False, default=ParseStatus.PENDING
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    file_count: Mapped[int] = mapped_column(default=0, nullable=False)
    symbol_count: Mapped[int] = mapped_column(default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<Repository {self.name} ({self.status})>"
