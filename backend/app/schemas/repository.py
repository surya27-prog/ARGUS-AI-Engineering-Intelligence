from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import get_settings
from app.core.giturl import GitUrlError, validate_git_url
from app.models import JobStatus, ParseStatus

T = TypeVar("T")


# Control characters plus both path separators. Built from code points rather
# than a string literal because a repository name reaches a filesystem path, a
# graph node key and a Content-Disposition filename.
_FORBIDDEN_NAME_CHARS = frozenset(chr(c) for c in range(32)) | {chr(47), chr(92)}




class RepositoryCreate(BaseModel):

    """Request body for ingesting a repository by URL.



    Zip uploads use the multipart form on the same endpoint instead.

    """



    url: str = Field(..., description="Git URL, e.g. https://github.com/psf/requests")

    name: str | None = Field(default=None, description="Overrides the name derived from the URL")



    @field_validator("url")

    @classmethod

    def url_must_be_cloneable(cls, value: str) -> str:

        """Delegated to `core.giturl`, which explains each refusal.



        The rules live there rather than inline because this string becomes an

        argument to `git clone`, and the reasoning for each check is longer than

        the check.

        """

        settings = get_settings()

        try:

            return validate_git_url(

                value, allow_private_hosts=settings.allow_private_git_hosts

            )

        except GitUrlError as exc:

            # Re-raised as ValueError so pydantic reports it as a 422 field

            # error rather than a 500.

            raise ValueError(str(exc)) from exc



    @field_validator("name")

    @classmethod

    def name_must_be_reasonable(cls, value: str | None) -> str | None:

        """A name reaches directory paths, node keys and a download filename."""

        if value is None:

            return None

        cleaned = value.strip()

        if not cleaned:

            return None

        if len(cleaned) > 255:

            raise ValueError("name must be at most 255 characters")

        # Built from a tuple rather than a string literal: this reaches a

        # filesystem path, a graph node key and a download filename.

        if any(ch in cleaned for ch in _FORBIDDEN_NAME_CHARS):
            raise ValueError("name must not contain slashes or control characters")

        return cleaned





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





class ParseJobOut(BaseModel):

    """One parse run. `run_id` is the stamp on the graph the run produced."""



    model_config = ConfigDict(from_attributes=True)



    id: UUID

    run_id: UUID

    source: str | None

    status: JobStatus

    error_message: str | None

    failed_stage: str | None = Field(

        default=None, description="ingest | parse | store | graph | vectors"

    )



    file_count: int

    symbol_count: int

    import_count: int

    call_count: int

    failed_file_count: int



    graph_nodes: int

    graph_relationships: int

    graph_nodes_deleted: int

    graph_relationships_deleted: int



    chunk_count: int

    embedding_tokens: int

    embedding_model: str | None = Field(

        default=None, description="null when the embedding pass was skipped"

    )

    vectors_deleted: int



    created_at: datetime

    started_at: datetime | None

    finished_at: datetime | None

    duration_ms: int | None

    stage_ms: dict[str, int] = Field(

        default_factory=dict,

        description="Milliseconds per pipeline stage: analyze, store, graph, "

        "cochange, vectors. A total says a parse was slow; this says which stage "

        "was. Empty for runs recorded before the column existed",

    )





class Page(BaseModel, Generic[T]):

    """Envelope for list endpoints — `total` is what makes the UI's paging honest."""



    items: list[T]

    total: int

    limit: int

    offset: int

