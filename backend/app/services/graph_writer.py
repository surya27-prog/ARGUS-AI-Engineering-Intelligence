"""Writes a `ParsedRepo` into Neo4j as nodes and containment edges.

Day 2 scope is identity and structure: `:Repo`, `:File`, `:Class`, `:Function`
and the `CONTAINS` tree between them. `IMPORTS` (Day 3), `CALLS` (Day 4) and
`INHERITS` land on top of the same run_id/sweep machinery, which is why the
sweep already removes stale *relationships* that nothing yet writes.

Every write is a `MERGE` on the node key and carries the run's `run_id`; the run
ends by deleting anything in this repo the run did not stamp. That combination
is what makes re-parsing idempotent (node count unchanged) *and* correct
(a deleted file leaves the graph), which `MERGE` alone gives only the first of.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import islice
from uuid import UUID, uuid4

from neo4j import ManagedTransaction, Session

from app.core.graph import ensure_schema, graph_session
from app.services.graph_keys import file_key, repo_key, symbol_key
from parser import ParsedRepo

logger = logging.getLogger(__name__)

# Rows per transaction. Large enough that the fixture repo is one or two round
# trips, small enough that a 10k-file repo never builds one enormous
# transaction in the server's heap.
BATCH_SIZE = 1000

# Labels the sweep visits, each backed by a repo_id index. `:Repo` is
# deliberately absent: it is the root the whole traversal enters through, and a
# write that dies halfway must not be able to take it out.
SWEPT_LABELS: tuple[str, ...] = ("File", "Class", "Function", "Module")

_LABEL_FOR_KIND = {"class": "Class", "function": "Function", "method": "Function"}


@dataclass(frozen=True, slots=True)
class GraphWriteResult:
    """What one parse run did to the graph. Logged, and asserted on in tests."""

    run_id: str
    repo_id: str
    nodes_written: int
    relationships_written: int
    nodes_deleted: int
    relationships_deleted: int


def write_parsed_repo(
    repository_id: UUID | str,
    parsed: ParsedRepo,
    *,
    run_id: str | None = None,
) -> GraphWriteResult:
    """Write `parsed` into the graph under `repository_id`, then sweep the run.

    `run_id` is accepted rather than always minted so Days 3–5 can stamp their
    edges with the same run as the nodes written here and be swept together.
    """
    repo_id = str(repository_id)
    run = run_id or str(uuid4())

    nodes = _node_rows(repo_id, parsed, run)
    edges = _contains_rows(repo_id, parsed)

    ensure_schema()

    nodes_written = 0
    relationships_written = 0
    with graph_session() as session:
        for label, rows in nodes.items():
            for batch in _batched(rows, BATCH_SIZE):
                session.execute_write(_merge_nodes, label, batch)
                nodes_written += len(batch)

        for (parent_label, child_label), rows in edges.items():
            for batch in _batched(rows, BATCH_SIZE):
                session.execute_write(_merge_contains, parent_label, child_label, batch, run)
                relationships_written += len(batch)

        nodes_deleted, relationships_deleted = sweep_run(session, repo_id, run)

    result = GraphWriteResult(
        run_id=run,
        repo_id=repo_id,
        nodes_written=nodes_written,
        relationships_written=relationships_written,
        nodes_deleted=nodes_deleted,
        relationships_deleted=relationships_deleted,
    )
    logger.info(
        "Graph write for repo %s: %d nodes, %d rels; swept %d nodes, %d rels (run %s)",
        repo_id,
        nodes_written,
        relationships_written,
        nodes_deleted,
        relationships_deleted,
        run,
    )
    return result


# --- row building -----------------------------------------------------------


def _node_rows(repo_id: str, parsed: ParsedRepo, run: str) -> dict[str, list[dict]]:
    """Turn a parse result into one list of property maps per label."""
    inventory = parsed.inventory
    rows: dict[str, list[dict]] = {
        "Repo": [
            {
                "key": repo_key(repo_id),
                "repo_id": repo_id,
                "name": inventory.name,
                "url": inventory.url,
                "commit_sha": inventory.commit_sha,
                "default_branch": inventory.default_branch,
                "run_id": run,
            }
        ],
        "File": [],
        "Class": [],
        "Function": [],
    }

    # The inventory carries on-disk metrics the extractor does not; index it
    # once rather than scanning it per file.
    info_by_path = {info.path: info for info in inventory.files}

    for parsed_file in parsed.files:
        info = info_by_path.get(parsed_file.path)
        rows["File"].append(
            {
                "key": file_key(repo_id, parsed_file.path),
                "repo_id": repo_id,
                "path": parsed_file.path,
                "module": parsed_file.module,
                "line_count": info.line_count if info else 0,
                "sha256": info.sha256 if info else None,
                "parse_error": parsed_file.error,
                "run_id": run,
            }
        )

        for symbol in parsed_file.symbols:
            label = _LABEL_FOR_KIND.get(str(symbol.kind))
            if label is None:
                continue
            row = {
                "key": symbol_key(repo_id, symbol.module, symbol.qualname, parsed_file.path),
                "repo_id": repo_id,
                "module": symbol.module,
                "qualname": symbol.qualname,
                "name": symbol.name,
                "line_start": symbol.line_start,
                "line_end": symbol.line_end,
                "run_id": run,
            }
            if label == "Function":
                row |= {
                    "kind": str(symbol.kind),
                    "is_async": symbol.is_async,
                    "param_count": len(symbol.parameters),
                    # Day 4 overwrites this from the call-graph pass; it is
                    # initialised here so the property always exists.
                    "unresolved_calls": 0,
                }
            else:
                # Day 3 creates `is_external: true` classes for unresolvable
                # bases. Anything written here was found in the repo.
                row["is_external"] = False
            rows[label].append(row)

    return {label: batch for label, batch in rows.items() if batch}


def _contains_rows(repo_id: str, parsed: ParsedRepo) -> dict[tuple[str, str], list[dict]]:
    """Containment edges, grouped by endpoint labels so each MATCH hits an index."""
    edges: dict[tuple[str, str], list[dict]] = {}

    def add(parent_label: str, child_label: str, parent: str, child: str) -> None:
        edges.setdefault((parent_label, child_label), []).append(
            {"parent": parent, "child": child}
        )

    root = repo_key(repo_id)
    for parsed_file in parsed.files:
        path = parsed_file.path
        this_file = file_key(repo_id, path)
        add("Repo", "File", root, this_file)

        # A symbol's `parent` is the dotted qualname of its enclosing scope, so
        # the enclosing symbol's label has to be looked up rather than assumed:
        # a class nested in a class and a function nested in a function are both
        # legal Python and both produce CONTAINS edges.
        label_by_qualname = {
            symbol.qualname: _LABEL_FOR_KIND.get(str(symbol.kind))
            for symbol in parsed_file.symbols
        }

        for symbol in parsed_file.symbols:
            child_label = _LABEL_FOR_KIND.get(str(symbol.kind))
            if child_label is None:
                continue
            child = symbol_key(repo_id, symbol.module, symbol.qualname, path)

            if symbol.parent is None:
                add("File", child_label, this_file, child)
                continue

            parent_label = label_by_qualname.get(symbol.parent)
            if parent_label is None:
                # The enclosing scope was not extracted as a symbol — a def
                # inside an `if` block, say. Hang it off the file so it is
                # still reachable rather than dropping it.
                add("File", child_label, this_file, child)
                continue

            add(
                parent_label,
                child_label,
                symbol_key(repo_id, symbol.module, symbol.parent, path),
                child,
            )

    return edges


# --- Cypher -----------------------------------------------------------------
#
# Labels are interpolated into these statements because Cypher cannot
# parameterise a label. Every value comes from the module constants above, never
# from parsed input, so there is nothing user-controlled in the query text.


def _merge_nodes(tx: ManagedTransaction, label: str, rows: Sequence[dict]) -> None:
    tx.run(
        f"""
        UNWIND $rows AS row
        MERGE (n:{label} {{key: row.key}})
        SET n += row
        """,
        rows=list(rows),
    )


def _merge_contains(
    tx: ManagedTransaction,
    parent_label: str,
    child_label: str,
    rows: Sequence[dict],
    run: str,
) -> None:
    tx.run(
        f"""
        UNWIND $rows AS row
        MATCH (parent:{parent_label} {{key: row.parent}})
        MATCH (child:{child_label} {{key: row.child}})
        MERGE (parent)-[r:CONTAINS]->(child)
        SET r.run_id = $run
        """,
        rows=list(rows),
        run=run,
    )


def sweep_run(session: Session, repo_id: str, run: str) -> tuple[int, int]:
    """Delete everything in this repo that `run` did not stamp.

    Relationships are swept before nodes so the counts stay meaningful: a
    `DETACH DELETE` on a stale node would otherwise absorb its edges into the
    node count and hide how many relationships actually went.
    """
    nodes_deleted = 0
    relationships_deleted = 0

    for label in SWEPT_LABELS:
        summary = session.run(
            f"""
            MATCH (n:{label} {{repo_id: $repo_id}})-[r]->()
            WHERE r.run_id IS NULL OR r.run_id <> $run
            DELETE r
            """,
            repo_id=repo_id,
            run=run,
        ).consume()
        relationships_deleted += summary.counters.relationships_deleted

    for label in SWEPT_LABELS:
        summary = session.run(
            f"""
            MATCH (n:{label} {{repo_id: $repo_id}})
            WHERE n.run_id IS NULL OR n.run_id <> $run
            DETACH DELETE n
            """,
            repo_id=repo_id,
            run=run,
        ).consume()
        nodes_deleted += summary.counters.nodes_deleted

    return nodes_deleted, relationships_deleted


def delete_repo_graph(repository_id: UUID | str) -> int:
    """Remove a repository from the graph entirely. Returns nodes deleted."""
    repo_id = str(repository_id)
    deleted = 0
    with graph_session() as session:
        for label in ("Repo", *SWEPT_LABELS):
            summary = session.run(
                f"MATCH (n:{label} {{repo_id: $repo_id}}) DETACH DELETE n",
                repo_id=repo_id,
            ).consume()
            deleted += summary.counters.nodes_deleted
    return deleted


def count_nodes(repository_id: UUID | str) -> dict[str, int]:
    """Node count per label for one repo. Used by tests and the Day 5 API."""
    repo_id = str(repository_id)
    counts: dict[str, int] = {}
    with graph_session() as session:
        for label in ("Repo", *SWEPT_LABELS):
            record = session.run(
                f"MATCH (n:{label} {{repo_id: $repo_id}}) RETURN count(n) AS total",
                repo_id=repo_id,
            ).single()
            counts[label] = record["total"] if record else 0
    return counts


def _batched(rows: Sequence[dict], size: int) -> Iterator[list[dict]]:
    iterator = iter(rows)
    while batch := list(islice(iterator, size)):
        yield batch
