"""Risk score: how much should changing this worry you?

Everything the previous days built answers one question each. Impact says what
a change reaches, the graph says how connected a symbol is, the commit log says
what it drags along and how often it moves, and the file layout says whether a
test would notice if it broke. A reviewer holds all five in their head at once.
This module does that arithmetic explicitly, and — because a number nobody can
justify is worse than no number — records every input alongside the result.

The formula, in one line:

    score = 100 x sum(weight_f x factor_f)   for f in the five factors below

Three choices in it are worth defending, because they are the ones that get
questioned:

**Saturating normalisation, not min-max.** Each raw count becomes a factor
through `x / (x + k)`. Min-max against the repo's own maximum would make every
score depend on the single most-connected symbol in the repository: add one
enormous god-module and everything else's risk silently drops. The saturating
curve has no such coupling, it is bounded in [0, 1) by construction, and `k` has
a plain-English reading — at `x == k` the factor is exactly 0.5, so the knee is
a documented judgement rather than an emergent property of the data.

**Coverage is the inverse of test reachability.** Untested code is not neutral;
it is the case where a mistake ships. So the factor is `1 - saturate(tests)`,
which is 1.0 for a symbol nothing tests and falls as test callers appear.

**Missing history renormalises rather than scores zero.** A zip upload has no
commit log, so coupling and churn are unmeasurable. Scoring them zero would
report "low risk" on the strength of data that was never collected, which is the
worst failure mode a risk score can have. Instead their weight is redistributed
across the factors that *were* measured, and the result says so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.core.graph import graph_session
from app.services.graph_queries import MAX_DEPTH, NodeNotFound, serialize_node

logger = logging.getLogger(__name__)

# How far the blast radius is counted for scoring. Two hops, not the impact
# endpoint's three: this runs for every node in the repository at once, and the
# third hop multiplies the work without changing the ranking much — by then
# almost everything reaches almost everything.
DEFAULT_RISK_DEPTH = 2

# Weights. They sum to 1.0, and the ordering is the argument: what a change
# reaches matters most, whether anything would catch the mistake comes next,
# and how the file has behaved historically is a modifier on both.
WEIGHTS: dict[str, float] = {
    "blast": 0.35,
    "coverage": 0.20,
    "centrality": 0.20,
    "coupling": 0.15,
    "churn": 0.10,
}

# The factors that only exist once the commit log has been read. Their weight is
# redistributed when it has not — see the module docstring.
HISTORY_FACTORS = ("coupling", "churn")

# Knees: the raw value at which each factor reaches 0.5. Every one of these is a
# judgement call, which is why they are named constants and not literals buried
# in an expression.
#
# 8 dependents — enough that a change needs a plan, not so many that only the
# god-modules ever score.
K_BLAST = 8.0
# 6 edges in and out combined. Most symbols sit at 1-3.
K_DEGREE = 6.0
# 3 files that habitually move with this one.
K_PARTNERS = 3.0
# 10 commits in the window. On a 500-commit window that is a file being actively
# worked on rather than one that was written once.
K_CHURN = 10.0
# 2 test references. One test is a smoke test; two means someone thought about it.
K_TESTS = 2.0

# Bands, so a UI can colour a score without inventing its own thresholds.
BANDS = ((25.0, "low"), (50.0, "moderate"), (75.0, "high"), (101.0, "critical"))

# What counts as a test file. Anchored at both ends because Neo4j's `=~` is a
# full match, and kept here rather than in the Cypher so the same definition is
# used by the query and by anything that needs to explain the result.
# The flag sits once at the very front and the alternation is wrapped in a
# group: `(?i)` anywhere else is an error in Python's `re`, and an ungrouped
# alternation would let one branch swallow the anchoring.
TEST_PATH_PATTERN = (
    r"(?i)("
    r"(.*/)?tests?/.*"  # anything under a tests/ or test/ directory
    r"|(.*/)?test_[^/]*\.py"  # test_foo.py
    r"|(.*/)?[^/]*_test\.py"  # foo_test.py
    r"|(.*/)?conftest\.py"
    r")"
)


@dataclass(frozen=True, slots=True)
class RiskMetrics:
    """The raw counts a score is computed from. Every one is reported back."""

    dependents: int = 0
    direct_dependents: int = 0
    dependencies: int = 0
    co_partners: int = 0
    max_jaccard: float = 0.0
    change_count: int | None = None
    test_references: int = 0

    @property
    def has_history(self) -> bool:
        """False when the commit log was never read for this repository.

        `change_count` is None rather than 0 exactly so this question has an
        answer — see the co-change writer.
        """
        return self.change_count is not None


@dataclass(frozen=True, slots=True)
class RiskScore:
    """One node's risk, with the whole derivation attached."""

    key: str
    type: str
    display: str
    score: float
    band: str
    factors: dict[str, float]
    weights: dict[str, float]
    metrics: RiskMetrics
    reasons: list[str] = field(default_factory=list)
    node: dict[str, Any] = field(default_factory=dict)


def saturate(value: float, knee: float) -> float:
    """`x / (x + k)` — 0 at zero, 0.5 at the knee, approaching 1 thereafter."""
    if value <= 0:
        return 0.0
    return value / (value + knee)


def factors_for(metrics: RiskMetrics) -> dict[str, float]:
    """The five normalised factors, each in [0, 1]."""
    return {
        "blast": saturate(metrics.dependents, K_BLAST),
        "centrality": saturate(metrics.direct_dependents + metrics.dependencies, K_DEGREE),
        # Two questions, blended: how many files move with this one, and how
        # tightly the strongest of those pairs moves. Count alone would rank a
        # file that has drifted near ten others above one welded to two.
        "coupling": 0.6 * saturate(metrics.co_partners, K_PARTNERS)
        + 0.4 * min(metrics.max_jaccard, 1.0),
        "churn": saturate(metrics.change_count or 0, K_CHURN),
        # Inverted: nothing testing it is the risky end.
        "coverage": 1.0 - saturate(metrics.test_references, K_TESTS),
    }


def weights_for(metrics: RiskMetrics) -> dict[str, float]:
    """The weights actually applied, renormalised when history is missing."""
    if metrics.has_history:
        return dict(WEIGHTS)

    available = {f: w for f, w in WEIGHTS.items() if f not in HISTORY_FACTORS}
    total = sum(available.values())
    return {f: w / total for f, w in available.items()}


def band_for(score: float) -> str:
    return next(name for threshold, name in BANDS if score < threshold)


def score_metrics(metrics: RiskMetrics) -> tuple[float, dict[str, float], dict[str, float]]:
    """Score, factors and applied weights for one set of raw counts."""
    factors = factors_for(metrics)
    weights = weights_for(metrics)
    score = 100.0 * sum(factors[name] * weight for name, weight in weights.items())
    return round(score, 1), {k: round(v, 4) for k, v in factors.items()}, weights


def explain(metrics: RiskMetrics) -> list[str]:
    """Short plain-English notes on what drove the score.

    Deliberately mechanical: these are read off the metrics, not written by a
    model. Saturday's LLM summary takes them as input, and it needs statements
    it cannot have invented.
    """
    reasons: list[str] = []
    if metrics.dependents:
        reasons.append(
            f"{metrics.dependents} symbol(s) depend on it, {metrics.direct_dependents} directly"
        )
    else:
        reasons.append("nothing depends on it")

    if metrics.test_references == 0:
        reasons.append("no test references it")
    else:
        reasons.append(f"referenced by {metrics.test_references} test symbol(s)")

    if not metrics.has_history:
        reasons.append("no commit history available — scored on structure alone")
        return reasons

    if metrics.co_partners:
        reasons.append(
            f"habitually changes with {metrics.co_partners} other file(s) "
            f"(strongest coupling {metrics.max_jaccard:.2f})"
        )
    if metrics.change_count:
        reasons.append(f"touched by {metrics.change_count} commit(s) in the window")
    return reasons


def build_score(node: dict[str, Any], metrics: RiskMetrics) -> RiskScore:
    """Assemble the public result for one node."""
    score, factors, weights = score_metrics(metrics)
    return RiskScore(
        key=node.get("key", ""),
        type=node.get("type", "Unknown"),
        display=node.get("display", ""),
        score=score,
        band=band_for(score),
        factors=factors,
        weights={k: round(v, 4) for k, v in weights.items()},
        metrics=metrics,
        reasons=explain(metrics),
        node=node,
    )


# --- reading the metrics out of the graph ------------------------------------


def score_node(
    repository_id: UUID | str, key: str, *, depth: int = DEFAULT_RISK_DEPTH
) -> RiskScore:
    """Risk for one node. Raises `NodeNotFound`."""
    rows = _metrics(repository_id, key=key, depth=depth)
    if not rows:
        raise NodeNotFound(key)
    node, metrics = rows[0]
    return build_score(node, metrics)


def rank_repository(
    repository_id: UUID | str,
    *,
    node_type: str | None = None,
    depth: int = DEFAULT_RISK_DEPTH,
    limit: int = 20,
) -> tuple[list[RiskScore], int]:
    """The riskiest nodes in a repository, highest first, and how many were scored.

    Every candidate is scored before any is dropped — a ranking cannot be
    computed from a page of it — so `limit` trims the answer, not the work, and
    the count of what was considered is returned alongside it. The traversal is
    what makes this the expensive endpoint; Week 5's performance pass is where
    it gets cached.
    """
    rows = _metrics(repository_id, node_type=node_type, depth=depth)
    scores = [build_score(node, metrics) for node, metrics in rows]
    scores.sort(key=lambda s: s.score, reverse=True)
    logger.info("Risk: scored %d node(s), returning %d", len(scores), min(limit, len(scores)))
    return scores[:limit], len(scores)


def _metrics(
    repository_id: UUID | str,
    *,
    key: str | None = None,
    node_type: str | None = None,
    depth: int = DEFAULT_RISK_DEPTH,
) -> list[tuple[dict[str, Any], RiskMetrics]]:
    """Raw counts per node, in one query.

    Everything here is a count of *distinct nodes*, never of paths: a symbol
    reachable by four routes is one dependent, and `COUNT { ... RETURN DISTINCT
    x }` is what says so.
    """
    if not 1 <= depth <= MAX_DEPTH:
        raise ValueError(f"depth must be between 1 and {MAX_DEPTH}")

    repo_id = str(repository_id)
    label = {"Function": "n:Function", "File": "n:File"}.get(
        node_type or "", "n:Function OR n:File"
    )

    # `depth` is interpolated for the usual reason — Cypher rejects a parameter
    # as a variable-length bound — and is range-checked immediately above.
    cypher = f"""
    MATCH (n)
    WHERE n.repo_id = $repo_id AND ({label})
      AND ($key IS NULL OR n.key = $key)
      AND coalesce(n.is_external, false) = false

    // The file a symbol lives in; a file is its own. Churn and coupling are
    // file-level facts, so a function inherits them from the file it sits in.
    OPTIONAL MATCH (owner:File {{repo_id: $repo_id}})-[:CONTAINS*1..4]->(n)
    WITH n, CASE WHEN n:File THEN n ELSE owner END AS file

    // `CALL (x) {{ }}` rather than `CALL {{ WITH x ... }}`: the latter is
    // deprecated as of the Neo4j 5.26 this project pins, and warns on every run.
    CALL (file) {{
        OPTIONAL MATCH (file)-[r:CO_CHANGED]-(:File)
        RETURN count(r) AS co_partners, coalesce(max(r.jaccard), 0.0) AS max_jaccard
    }}
    // Test coverage, proxied: a symbol is covered to the extent that things in
    // test files reach it. `CONTAINS*0..4` starts at zero so an :File importer
    // counts as its own container — an import from a test file is a reference
    // even though no function in it calls the symbol directly.
    CALL (n) {{
        MATCH (caller)-[:CALLS|IMPORTS]->(n)
        MATCH (caller_file:File)-[:CONTAINS*0..4]->(caller)
        WHERE caller_file.repo_id = $repo_id AND caller_file.path =~ $test_pattern
        RETURN count(DISTINCT caller) AS test_references
    }}

    RETURN n AS node,
           file.change_count AS change_count,
           co_partners, max_jaccard, test_references,
           COUNT {{
               MATCH (other)-[:CALLS|IMPORTS*1..{depth}]->(n)
               WHERE other.repo_id = $repo_id AND other <> n
               RETURN DISTINCT other
           }} AS dependents,
           COUNT {{
               MATCH (other)-[:CALLS|IMPORTS]->(n)
               WHERE other.repo_id = $repo_id AND other <> n
               RETURN DISTINCT other
           }} AS direct_dependents,
           COUNT {{
               MATCH (n)-[:CALLS|IMPORTS]->(other)
               WHERE other.repo_id = $repo_id AND other <> n
               RETURN DISTINCT other
           }} AS dependencies
    """

    with graph_session() as session:
        records = list(
            session.run(
                cypher,
                repo_id=repo_id,
                key=key,
                test_pattern=TEST_PATH_PATTERN,
            )
        )

    return [
        (
            serialize_node(record["node"]),
            RiskMetrics(
                dependents=record["dependents"],
                direct_dependents=record["direct_dependents"],
                dependencies=record["dependencies"],
                co_partners=record["co_partners"],
                max_jaccard=round(record["max_jaccard"], 4),
                change_count=record["change_count"],
                test_references=record["test_references"],
            ),
        )
        for record in records
    ]
