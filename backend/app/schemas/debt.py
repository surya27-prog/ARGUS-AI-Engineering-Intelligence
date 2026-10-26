"""Response shapes for the debt report.

`metrics`, `why` and `confidence` all travel with every finding for the same
reason the risk score carries its factors: a number a reviewer cannot check is a
number they will stop reading.
"""

from typing import Any

from pydantic import BaseModel, Field


class FindingOut(BaseModel):
    kind: str = Field(
        description="complexity | god_file | circular_import | dead_code | missing_docstring"
    )
    severity: str = Field(description="high | medium | low | info")
    subject: str = Field(description="The symbol, file or cycle the finding is about")
    path: str | None = None
    line_start: int | None = None
    key: str | None = Field(
        default=None,
        description="Graph node key when the subject has one, for jumping to the graph",
    )
    why: str = Field(description="The metrics read back as a sentence")
    metrics: dict[str, Any] = Field(
        default_factory=dict, description="The numbers the severity was derived from"
    )
    confidence: float = Field(
        description="Below 1.0 where static analysis cannot be certain — see dead code"
    )
    location: str = Field(description="`path:line` when known, else the subject")


class FileDebtOut(BaseModel):
    path: str
    findings: int
    worst: str
    by_kind: dict[str, int]


class DebtSummary(BaseModel):
    total: int = Field(description="Findings after filtering")
    scanned_total: int = Field(description="Findings before filtering — the whole scan")
    by_kind: dict[str, int]
    by_severity: dict[str, int]
    ran: list[str] = Field(description="Detectors that completed")
    failed: dict[str, str] = Field(
        description="Detectors that raised, and why. A detector that did not run "
        "reports zero findings, which is not the same as a clean result"
    )


class DebtResponse(BaseModel):
    summary: DebtSummary
    items: list[FindingOut] = Field(description="Ranked worst-first, then most confident")
    files: list[FileDebtOut] = Field(description="Which files carry the most debt")
    limit: int
    offset: int
    truncated: bool = Field(description="True when the page hid part of the result")
