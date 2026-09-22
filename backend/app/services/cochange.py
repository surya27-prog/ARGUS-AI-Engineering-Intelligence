"""Co-change edges: coupling the static graph cannot see.

`IMPORTS` and `CALLS` are derived from what the code says. They miss the
couplings that live in convention rather than syntax — a module and its test, a
schema and the migration that mirrors it, a client and the config it reads
through three layers of indirection. Those pairs break together anyway, and the
commit log is where they are recorded.

`parser.history` does the counting; this module is the seam that puts the result
into Neo4j:

* A `CO_CHANGED` edge per pair, written in canonical (left < right) order and
  read back undirected — co-change is symmetric, and a direction would invite a
  causal reading that the data does not support.
* `change_count` on each `:File`, straight from the same pass. Churn is the
  other half of what the commit log knows, and Day 3's risk score needs it.

Edges are written only between `:File` nodes that already exist, so a file
deleted last week couples with nothing and a path outside the parsed inventory
never appears. They are stamped with the parse run's `run_id` like everything
else, which makes a re-parse replace them rather than accumulate them.

`CO_CHANGED` is deliberately absent from the traversals in `graph_queries` and
`impact`: "these change together" is evidence about a change, not a dependency,
and letting it into the blast radius would put every test file in every answer.
Day 3's risk score is where it gets its weight.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from app.core.config import get_settings
from app.core.graph import graph_session
from app.services.graph_keys import file_key
from app.services.graph_queries import serialize_node
from parser import ParsedRepo, RepoHistory, analyze_history

logger = logging.getLogger(__name__)

# Same reasoning as the graph writer's: one or two round trips for a normal
# repo, no single enormous transaction for a big one.
BATCH_SIZE = 1000


@dataclass(frozen=True, slots=True)
class CoChangeResult:
    """What one history pass did. Recorded on the parse job."""

    run_id: str
    repo_id: str
    commits_read: int
    commits_used: int
    commits_skipped: int
    edges_written: int
    files_scored: int

    @property
    def skipped(self) -> bool:
        """True when there was no history to read — a zip upload, say."""
        return self.commits_read == 0


def ingest_history(
    repository_id: UUID | str,
    parsed: ParsedRepo,
    *,
    run_id: str | None = None,
) -> CoChangeResult:
    """Read the staged repo's git log and write its coupling into the graph.

    The inventory's paths are passed down as a filter, so the counting only ever
    sees files that made it into the graph — see `parser.history.co_change`.
    """
    settings = get_settings()
    history = analyze_history(
        parsed.inventory.root,
        paths={info.path for info in parsed.inventory.files},
        max_commits=settings.history_max_commits,
        max_files_per_commit=settings.cochange_max_files_per_commit,
        min_shared_commits=settings.cochange_min_commits,
    )
    return write_cochange(repository_id, history, run_id=run_id)


def write_cochange(
    repository_id: UUID | str,
    history: RepoHistory,
    *,
    run_id: str | None = None,
) -> CoChangeResult:
    """Write `history` into the graph as `CO_CHANGED` edges and file churn."""
    repo_id = str(repository_id)
    run = run_id or str(uuid4())

    edge_rows = [
        {
            "source": file_key(repo_id, pair.left),
            "target": file_key(repo_id, pair.right),
            "commits": pair.shared_commits,
            "jaccard": pair.jaccard,
            "left_commits": pair.left_commits,
            "right_commits": pair.right_commits,
            "last_together": pair.last_together,
        }
        for pair in history.pairs
    ]
    churn_rows = [
        {"key": file_key(repo_id, path), "change_count": count}
        for path, count in history.file_commits.items()
    ]

    edges_written = 0
    files_scored = 0
    with graph_session() as session:
        for batch in _batched(churn_rows, BATCH_SIZE):
            files_scored += session.execute_write(_set_churn, batch, repo_id)
        for batch in _batched(edge_rows, BATCH_SIZE):
            edges_written += session.execute_write(_merge_cochange, batch, repo_id, run)

    result = CoChangeResult(
        run_id=run,
        repo_id=repo_id,
        commits_read=history.commits_read,
        commits_used=history.commits_used,
        commits_skipped=history.commits_skipped,
        edges_written=edges_written,
        files_scored=files_scored,
    )
    logger.info(
        "Co-change for repo %s: %d commits used, %d edges over %d files (run %s)",
        repo_id,
        result.commits_used,
        result.edges_written,
        result.files_scored,
        run,
    )
    return result


def _set_churn(tx, rows: list[dict], repo_id: str) -> int:
    """How often each file was committed in the window.

    Written with `MATCH`, not `MERGE`: a path in the log that is not in the
    graph is a file that has since been deleted or was never parsed, and it must
    not conjure a node. The count of matched rows is the return value, so a
    caller can see how much of the history landed.
    """
    result = tx.run(
        """
        UNWIND $rows AS row
        MATCH (f:File {key: row.key})
        WHERE f.repo_id = $repo_id
        SET f.change_count = row.change_count
        RETURN count(f) AS matched
        """,
        rows=rows,
        repo_id=repo_id,
    ).single()
    return result["matched"] if result else 0


def _merge_cochange(tx, rows: list[dict], repo_id: str, run: str) -> int:
    result = tx.run(
        """
        UNWIND $rows AS row
        MATCH (a:File {key: row.source})
        MATCH (b:File {key: row.target})
        WHERE a.repo_id = $repo_id AND b.repo_id = $repo_id
        MERGE (a)-[r:CO_CHANGED]->(b)
        SET r.commits = row.commits,
            r.jaccard = row.jaccard,
            r.left_commits = row.left_commits,
            r.right_commits = row.right_commits,
            r.last_together = row.last_together,
            r.run_id = $run
        RETURN count(r) AS written
        """,
        rows=rows,
        repo_id=repo_id,
        run=run,
    ).single()
    return result["written"] if result else 0


def coupled_files(
    repository_id: UUID | str,
    *,
    key: str | None = None,
    min_commits: int = 1,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Coupled file pairs, strongest first.

    With `key`, only the pairs that file takes part in; without, the repository's
    strongest coupling overall. The match is undirected because the edge's
    direction is a storage convention, not a claim — asking for the partners of
    the right-hand file has to work exactly as well as asking for the left's.
    """
    repo_id = str(repository_id)
    scope = "AND (a.key = $key OR b.key = $key)" if key else ""

    cypher = f"""
    MATCH (a:File {{repo_id: $repo_id}})-[r:CO_CHANGED]-(b:File {{repo_id: $repo_id}})
    WHERE r.commits >= $min_commits {scope}
      // The undirected match returns each edge twice, once from each end. This
      // keeps the canonical orientation and drops the mirror image.
      AND startNode(r) = a
    RETURN a AS left, b AS right, r.commits AS commits, r.jaccard AS jaccard,
           r.last_together AS last_together
    ORDER BY commits DESC, jaccard DESC
    LIMIT $limit
    """

    with graph_session() as session:
        records = list(
            session.run(cypher, repo_id=repo_id, key=key, min_commits=min_commits, limit=limit)
        )

    return [
        {
            "left": serialize_node(record["left"]),
            "right": serialize_node(record["right"]),
            "commits": record["commits"],
            "jaccard": record["jaccard"],
            "last_together": record["last_together"],
        }
        for record in records
    ]


def _batched(rows: list[dict], size: int):
    for start in range(0, len(rows), size):
        yield rows[start : start + size]
