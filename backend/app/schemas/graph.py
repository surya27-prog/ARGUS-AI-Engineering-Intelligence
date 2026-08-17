"""Response shapes for the graph endpoints.

Node fields are deliberately flat and mostly optional: a `:File` has a `path`
and no `qualname`, a `:Module` has neither. One shape the UI can render without
branching on type beats five shapes it has to discriminate.
"""

from pydantic import BaseModel, Field


class GraphNode(BaseModel):
    key: str
    type: str = Field(description="Repo | File | Class | Function | Module")
    display: str = Field(description="What to label the node with")

    name: str | None = None
    path: str | None = None
    module: str | None = None
    qualname: str | None = None
    kind: str | None = Field(default=None, description="function | method, on :Function")
    line_start: int | None = None
    line_end: int | None = None
    is_external: bool | None = None
    unresolved_calls: int | None = None


class GraphEdge(BaseModel):
    type: str = Field(description="CONTAINS | IMPORTS | CALLS | INHERITS")
    source: str
    target: str

    resolution: str | None = None
    confidence: float | None = None
    count: int | None = Field(default=None, description="Call sites, on CALLS")
    line: int | None = Field(default=None, description="Import line, on IMPORTS")


class GraphResponse(BaseModel):
    view: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool = Field(description="True when the cap hid part of the graph")


class RelatedNode(GraphNode):
    """A node reached by traversal, described by its strongest shortest path."""

    hops: int
    confidence: float = Field(description="Product of per-hop confidences")
    via: list[str] = Field(description="Edge types along the path, in order")


class TraversalResponse(BaseModel):
    root: GraphNode
    depth: int
    direction: str = Field(description="dependencies | dependents")
    items: list[RelatedNode]
    total: int


class NodeSearchResponse(BaseModel):
    query: str
    items: list[GraphNode]
    total: int
