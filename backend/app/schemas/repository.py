from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import ParseStatus

T = TypeVar("T")


class RepositoryCreate(BaseModel):
    """Request body for ingesting a repository by URL.

    Zip uploads use the multipart form on the same endpoint instead.
    """

    url: str = Field(..., description="Git URL, e.g. https://github.com/psf/requests")
    name: str | None = Field(default=None, description="Overrides the name derived from the URL")

    @field_validator("url")
    @classmethod
    def url_must_look_like_a_repo(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("url must not be empty")
        if not value.startswith(("http://", "https://", "git@", "ssh://", "git://")):
            raise ValueError("url must be an http(s), ssh or git URL")
        return value


class RepositoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    url: str | None
    default_branch: str | None
    commit_sha: str | None
    status: ParseStatus
    error_message: str | None
    file_count: int
    symbol_count: int
    created_at: datetime
    updated_at: datetime
    parsed_at: datetime | None


class SourceFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    path: str
    module: str | None
    language: str
    extension: str
    size_bytes: int
    line_count: int
    symbol_count: int
    import_count: int
    call_count: int
    parse_error: str | None


class ParameterOut(BaseModel):
    name: str
    kind: str = "positional_or_keyword"
    annotation: str | None = None
    default: str | None = None


class SymbolOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    file_id: UUID
    name: str
    qualname: str
    kind: str
    module: str | None
    parent: str | None
    line_start: int
    line_end: int
    docstring: str | None
    returns: str | None
    is_async: bool
    decorators: list[str]
    parameters: list[ParameterOut]
    base_classes: list[str]


class Page(BaseModel, Generic[T]):
    """Envelope for list endpoints — `total` is what makes the UI's paging honest."""

    items: list[T]
    total: int
    limit: int
    offset: int
