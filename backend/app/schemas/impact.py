"""Response shapes for impact analysis.

`ImpactedNode` extends `GraphNode` for the same reason `RelatedNode` does: the
UI renders one node shape everywhere, and impact adds fields to it rather than
inventing a parallel type.
"""

from pydantic import BaseModel, Field

from app.schemas.graph import GraphNode


class ImpactedNode(GraphNode):
    """A node a change to the target could reach."""

    hops: int = Field(description="Edges between the target and this node")
    confidence: float = Field(description="Product of per-hop edge confidences")
    score: float = Field(description="confidence x decay^(hops-1) — the ranking key")
    via: list[str] = Field(description="Edge types along the route, target first")
    route: list[str] = Field(
        description="Node keys from the target to this node, inclusive of both. "
        "Named `route` because `path` is already this node's file path."
    )


class ImpactSummary(BaseModel):
    total: int
    depth: int
    decay: float
    direct: int = Field(description="Nodes one hop out — the ones most likely to break")
    max_hops: int
    top_score: float
    by_hop: dict[str, int] = Field(description="Affected node count per hop distance")
    by_type: dict[str, int] = Field(description="Affected node count per node type")


class ImpactResponse(BaseModel):
    root: GraphNode
    items: list[ImpactedNode] = Field(description="Ranked by score, highest first")
    total: int
    truncated: bool = Field(description="True when the cap hid part of the radius")
    summary: ImpactSummary
