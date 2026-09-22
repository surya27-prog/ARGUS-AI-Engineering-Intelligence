"""Response shapes for co-change.

A pair is returned as two whole nodes rather than two keys: the caller is
rendering file names, and making it fetch each node to find out what to display
would be one request per row.
"""

from pydantic import BaseModel, Field

from app.schemas.graph import GraphNode


class CoChangePair(BaseModel):
    """Two files committed together, in canonical (left < right) order.

    The order is storage, not meaning — co-change is symmetric, so neither side
    is the cause of the other.
    """

    left: GraphNode
    right: GraphNode
    commits: int = Field(description="Commits that touched both files")
    jaccard: float = Field(
        description="Shared commits over the union of both files' commits — "
        "1.0 means they never change apart"
    )
    last_together: str | None = Field(
        default=None, description="ISO timestamp of the most recent shared commit"
    )


class CoChangeResponse(BaseModel):
    root: GraphNode | None = Field(
        default=None, description="The file asked about, when `key` was given"
    )
    items: list[CoChangePair] = Field(description="Ranked by shared commits, highest first")
    total: int
