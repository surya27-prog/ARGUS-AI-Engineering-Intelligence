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
from app.services.call_resolver import CallGraph, SymbolRef, resolve_call_graph
from app.services.graph_keys import (
    external_class_key,
    file_key,
    module_key,
    repo_key,
    symbol_key,
    top_level,
)
from app.services.import_resolver import ResolvedImport, dedupe_edges, resolve_imports
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

    imports = resolve_imports(parsed)
    call_graph = resolve_call_graph(parsed, imports)

    nodes = _node_rows(repo_id, parsed, run, call_graph.unresolved_by_caller)
    nodes["Module"] = _module_rows(repo_id, imports, run)
    # External base classes are :Class nodes this repo never defines, so they
    # are appended to the label the resolved classes already write.
    nodes.setdefault("Class", []).extend(_external_class_rows(repo_id, call_graph, run))
    edges = _contains_rows(repo_id, parsed)
    import_edges = _imports_rows(repo_id, dedupe_edges(imports))
    call_edges = _calls_rows(repo_id, call_graph)
    inherit_edges = _inherits_rows(repo_id, call_graph)

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

        # Imports are written after the nodes because both endpoints have to
        # exist for the MATCH to find them — external :Module nodes included.
        for target_label, rows in import_edges.items():
            for batch in _batched(rows, BATCH_SIZE):
                session.execute_write(_merge_imports, target_label, batch, run)
                relationships_written += len(batch)

        for target_label, rows in call_edges.items():
            for batch in _batched(rows, BATCH_SIZE):
                session.execute_write(_merge_calls, target_label, batch, run)
                relationships_written += len(batch)

        for batch in _batched(inherit_edges, BATCH_SIZE):
            session.execute_write(_merge_inherits, batch, run)
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


def _node_rows(
    repo_id: str,
    parsed: ParsedRepo,
    run: str,
    unresolved: dict[tuple[str, str], int] | None = None,
) -> dict[str, list[dict]]:
    """Turn a parse result into one list of property maps per label."""
    unresolved = unresolved or {}
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
                    # Call sites in this body that resolved to nothing. Zero
                    # when the call-graph pass found none, never absent.
                    "unresolved_calls": unresolved.get(
                        (parsed_file.path, symbol.qualname), 0
                    ),
                }
            else:
                # Day 3 creates `is_external: true` classes for unresolvable
                # bases. Anything written here was found in the repo.
                row["is_external"] = False
            rows[label].append(row)

    return {label: batch for label, batch in rows.items() if batch}


def _module_rows(repo_id: str, imports: Sequence[ResolvedImport], run: str) -> list[dict]:
    """One `:Module` node per distinct external package this repo imports.

    Internal modules deliberately get no node: in Python an internal module *is*
    a file, so `IMPORTS` points straight at the `:File`. See the schema doc.
    """
    rows: dict[str, dict] = {}
    for entry in imports:
        dotted = entry.target_module
        if not dotted or dotted in rows:
            continue
        rows[dotted] = {
            "key": module_key(repo_id, dotted),
            "repo_id": repo_id,
            "dotted_name": dotted,
            "top_level": top_level(dotted),
            "is_external": True,
            "run_id": run,
        }
    return list(rows.values())


def _imports_rows(repo_id: str, imports: Sequence[ResolvedImport]) -> dict[str, list[dict]]:
    """`IMPORTS` edges, grouped by target label so each MATCH hits an index."""
    edges: dict[str, list[dict]] = {"File": [], "Module": []}

    for entry in imports:
        if entry.is_internal:
            target_label = "File"
            target = file_key(repo_id, entry.target_path)
        else:
            target_label = "Module"
            target = module_key(repo_id, entry.target_module)

        edges[target_label].append(
            {
                "source": file_key(repo_id, entry.source_path),
                "target": target,
                "line": entry.line,
                "alias": entry.alias,
                "level": entry.level,
                "is_relative": entry.is_relative,
                "resolution": str(entry.resolution),
            }
        )

    return {label: rows for label, rows in edges.items() if rows}


def _symbol_ref_key(repo_id: str, ref: SymbolRef) -> str:
    return symbol_key(repo_id, ref.module, ref.qualname, ref.path)


def _external_class_rows(repo_id: str, graph: CallGraph, run: str) -> list[dict]:
    """`:Class` nodes for bases that resolve to nothing inside the repo."""
    rows: dict[str, dict] = {}
    for entry in graph.bases:
        name = entry.external_name
        if not name or name in rows:
            continue
        rows[name] = {
            "key": external_class_key(repo_id, name),
            "repo_id": repo_id,
            "module": None,
            "qualname": name,
            "name": name.rsplit(".", 1)[-1],
            "line_start": 0,
            "line_end": 0,
            "is_external": True,
            "run_id": run,
        }
    return list(rows.values())


def _calls_rows(repo_id: str, graph: CallGraph) -> dict[str, list[dict]]:
    """`CALLS` edges — one per caller/callee pair, grouped by the callee's label."""
    rows: dict[str, list[dict]] = {}
    for edge in graph.edges:
        rows.setdefault(edge.callee.label, []).append(
            {
                "source": _symbol_ref_key(repo_id, edge.caller),
                "target": _symbol_ref_key(repo_id, edge.callee),
                "lines": list(edge.lines),
                "count": edge.count,
                "resolution": str(edge.resolution),
                "confidence": edge.confidence,
            }
        )
    return rows


def _inherits_rows(repo_id: str, graph: CallGraph) -> list[dict]:
    """`INHERITS` edges, internal and external bases alike."""
    return [
        {
            "source": _symbol_ref_key(repo_id, entry.subclass),
            "target": (
                _symbol_ref_key(repo_id, entry.base)
                if entry.base is not None
                else external_class_key(repo_id, entry.external_name or "")
            ),
            "position": entry.position,
            "resolution": entry.resolution,
        }
        for entry in graph.bases
    ]


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
# parameterise a label. Every value is either a module constant above or derived
# from a `SymbolKind` enum member — never a string out of the parsed source — so
# there is nothing user-controlled in the query text.


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


def _merge_imports(
    tx: ManagedTransaction, target_label: str, rows: Sequence[dict], run: str
) -> None:
    # `line` is part of the MERGE pattern, not just a property: a file that
    # imports the same target twice is two import statements and stays two
    # edges. Without it the second write would silently overwrite the first's
    # line and alias.
    tx.run(
        f"""
        UNWIND $rows AS row
        MATCH (source:File {{key: row.source}})
        MATCH (target:{target_label} {{key: row.target}})
        MERGE (source)-[r:IMPORTS {{line: row.line}}]->(target)
        SET r.alias = row.alias,
            r.level = row.level,
            r.is_relative = row.is_relative,
            r.resolution = row.resolution,
            r.run_id = $run
        """,
        rows=list(rows),
        run=run,
    )


def _merge_calls(
    tx: ManagedTransaction, target_label: str, rows: Sequence[dict], run: str
) -> None:
    # No line in the MERGE pattern here, unlike IMPORTS: CALLS is aggregated, so
    # one edge per pair is the point and the individual lines ride along as a
    # list property.
    tx.run(
        f"""
        UNWIND $rows AS row
        MATCH (source:Function {{key: row.source}})
        MATCH (target:{target_label} {{key: row.target}})
        MERGE (source)-[r:CALLS]->(target)
        SET r.lines = row.lines,
            r.count = row.count,
            r.resolution = row.resolution,
            r.confidence = row.confidence,
            r.run_id = $run
        """,
        rows=list(rows),
        run=run,
    )


def _merge_inherits(tx: ManagedTransaction, rows: Sequence[dict], run: str) -> None:
    # `position` is in the MERGE pattern because a class can legally inherit the
    # same base twice in different positions, and MRO order is the point of
    # storing it at all.
    tx.run(
        """
        UNWIND $rows AS row
        MATCH (source:Class {key: row.source})
        MATCH (target:Class {key: row.target})
        MERGE (source)-[r:INHERITS {position: row.position}]->(target)
        SET r.resolution = row.resolution,
            r.run_id = $run
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


# Which label a relationship type leaves from, so the count query can filter on
# an indexed repo_id rather than scanning every node.
_SOURCE_LABEL = {"IMPORTS": "File", "CALLS": "Function", "INHERITS": "Class"}


def count_relationships(repository_id: UUID | str, rel_type: str = "IMPORTS") -> dict[str, int]:
    """Relationship count per `resolution` for one repo. Tests and the Day 5 API.

    `CONTAINS` carries no resolution, so it reports under a single `none` key.
    """
    repo_id = str(repository_id)
    source_label = _SOURCE_LABEL.get(rel_type, "File")
    with graph_session() as session:
        records = session.run(
            f"""
            MATCH (n:{source_label} {{repo_id: $repo_id}})-[r:{rel_type}]->()
            RETURN coalesce(r.resolution, 'none') AS resolution, count(r) AS total
            """,
            repo_id=repo_id,
        ).data()
    return {record["resolution"]: record["total"] for record in records}


def _batched(rows: Sequence[dict], size: int) -> Iterator[list[dict]]:
    iterator = iter(rows)
    while batch := list(islice(iterator, size)):
        yield batch
