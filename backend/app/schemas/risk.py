"""Response shapes for the risk score.

The score is the small part of this payload on purpose. A bare 0-100 number is
unusable in a review — "why?" is the next question every time — so the factors,
the weights actually applied and the raw counts all travel with it, and the
score can be recomputed from them by hand.
"""

from pydantic import BaseModel, Field

from app.schemas.graph import GraphNode


class RiskMetrics(BaseModel):
    """The raw counts, before any normalisation."""

    dependents: int = Field(description="Distinct nodes reaching this one within the depth")
    direct_dependents: int = Field(description="Of those, one hop away")
    dependencies: int = Field(description="Nodes this one reaches directly")
    co_partners: int = Field(description="Files that habitually change with this file")
    max_jaccard: float = Field(description="Strongest co-change coupling on the file")
    change_count: int | None = Field(
        default=None,
        description="Commits touching the file. null means the history was never read, "
        "which is not the same as zero",
    )
    test_references: int = Field(description="Distinct callers or importers in test files")


class RiskItem(BaseModel):
    """One scored node."""

    key: str
    type: str
    display: str
    score: float = Field(description="0-100, higher is riskier")
    band: str = Field(description="low | moderate | high | critical")
    factors: dict[str, float] = Field(description="Each normalised input, in [0, 1]")
    weights: dict[str, float] = Field(
        description="The weights applied to this node — renormalised when no history exists"
    )
    metrics: RiskMetrics
    reasons: list[str] = Field(description="What drove the score, read off the metrics")
    node: GraphNode


class RiskResponse(BaseModel):
    items: list[RiskItem] = Field(description="Ranked by score, highest first")
    total: int
    depth: int = Field(description="Hops the blast-radius input was counted over")
    scored: int = Field(description="Nodes considered before the limit was applied")
