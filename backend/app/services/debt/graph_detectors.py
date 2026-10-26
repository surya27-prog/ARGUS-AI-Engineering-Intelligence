"""Detectors that need the graph: circular imports and dead code.

Neither is answerable from the flat facts. A cycle is a property of the whole
import graph, and "nothing references this" is a statement about every edge in
the repository — which is exactly the kind of question Neo4j exists here to
answer and SQL does badly.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.graph import graph_session
from app.models import SourceFile, Symbol
from app.services.debt.base import DebtKind, Finding, Severity

logger = logging.getLogger(__name__)

# Cycles longer than this are almost always a symptom of the shorter ones inside
# them, and enumerating every rotation of a long cycle buries the real finding.
MAX_CYCLE_LENGTH = 6

# Test files, entry points and plugin hooks are called by machinery ARGUS cannot
# see. A symbol matching these is not reported as dead.
_TEST_PATH = re.compile(r"(^|/)(tests?|testing)(/|$)|(^|/)(test_[^/]*|[^/]*_test)\.py$")
_ENTRY_POINT_NAMES = frozenset({"main", "run", "cli", "app", "handler", "lambda_handler"})

# A decorator is the commonest way a symbol is called by something other than
# code in the repo: a route, a task, a fixture, a hook, an event handler. This is
# the single biggest source of dead-code false positives.
_REGISTERED = re.compile(
    r"\b(route|get|post|put|patch|delete|websocket|"
    r"task|job|command|fixture|hook|register|listener|"
    r"setter|getter|deleter|"
    r"validator|field_validator|model_validator|"
    r"property|cached_property|"
    r"overload|abstractmethod|singledispatch)\b"
)

# Dunders are invoked by the interpreter, never by a call site ARGUS can resolve.
_DUNDER = re.compile(r"^__.*__$")


def detect_circular_imports(
    repository_id: UUID | str, *, max_length: int = MAX_CYCLE_LENGTH
) -> list[Finding]:
    """Import cycles between files in the repository.

    Only internal `IMPORTS` edges — a cycle through an external package is not
    something this repository can fix, and `:Module` nodes are leaves anyway.
    """
    repo_id = str(repository_id)
    # `apoc.nodes.cycles` would need the APOC procedure present on every
    # deployment; a bounded variable-length match needs nothing but Cypher.
    #
    # The `key` comparison is what stops a cycle being returned once per member:
    # of the N rotations of the same loop, only the one starting at the
    # lowest-keyed file passes. Keys are unique by construction, so this is a
    # total order — and unlike Neo4j's `id()`, it is not deprecated.
    cypher = f"""
    MATCH (start:File {{repo_id: $repo_id}})
    MATCH path = (start)-[:IMPORTS*2..{max_length}]->(start)
    WHERE all(other IN nodes(path)[1..-1] WHERE other.key > start.key)
    WITH [n IN nodes(path) | n.path] AS files, length(path) AS hops
    RETURN DISTINCT files, hops
    ORDER BY hops ASC, files ASC
    LIMIT 1000
    """

    with graph_session() as session:
        records = [record.data() for record in session.run(cypher, repo_id=repo_id)]

    findings = []
    reported: list[frozenset[str]] = []
    for record in records:
        files: list[str] = record["files"]
        # nodes(path) repeats the start file at both ends; the loop is implied.
        ring = files[:-1]

        # Only minimal cycles. A long loop that contains a shorter one is a
        # consequence of it, not a separate problem: on `requests` this is the
        # difference between 3 findings and 200, and breaking the short cycle
        # breaks every loop built on top of it. Records arrive shortest-first,
        # so anything already reported is genuinely smaller.
        members = frozenset(ring)
        if any(seen < members for seen in reported):
            continue
        reported.append(members)

        findings.append(
            Finding(
                kind=DebtKind.CIRCULAR_IMPORT,
                severity=Severity.HIGH if record["hops"] == 2 else Severity.MEDIUM,
                subject=" -> ".join(ring),
                path=ring[0] if ring else None,
                line_start=None,
                key=None,
                why=(
                    f"{record['hops']} files import each other in a loop: "
                    f"{' -> '.join(files)}"
                ),
                metrics={"files": ring, "length": record["hops"]},
            )
        )
    return findings


def detect_dead_code(
    db: Session, repository_id: UUID | str, *, limit: int = 500
) -> list[Finding]:
    """Symbols nothing in the repository references.

    This detector is the reason `Finding.confidence` exists. Python resolves
    plenty of calls at runtime — `getattr`, registries, entry points, dependency
    injection, anything driven by a string — and the call resolver already
    records the calls it could not resolve. So a symbol with no incoming edge
    means "no *statically visible* reference", which is weaker than dead, and the
    output says so rather than inviting someone to delete a live handler.

    Candidates come from the graph, which is the only place that knows about
    incoming edges; the decorators that rule most of them out come from
    Postgres, which is where the parser puts them. Neither store holds both.

    **Known blind spot: module-level calls.** `call_resolver` skips any call site
    with no enclosing function, so `_init()` written at module scope creates no
    `CALLS` edge and its target reads as unreferenced. `requests`'
    `status_codes._init` is exactly this case. Fixing it means letting a `:File`
    be the source of a `CALLS` edge, which is a schema change; until then no
    finding here exceeds 0.75 confidence and every one says why.
    """
    repo_id = str(repository_id)
    cypher = """
    MATCH (s {repo_id: $repo_id})
    WHERE (s:Function OR s:Class) AND coalesce(s.is_external, false) = false
    AND NOT EXISTS { MATCH ()-[:CALLS]->(s) }
    AND NOT EXISTS { MATCH ()-[:INHERITS]->(s) }
    RETURN s.key AS key, s.name AS name, s.qualname AS qualname,
           s.kind AS kind, s.module AS module, s.line_start AS line_start,
           s.line_end AS line_end
    LIMIT $limit
    """

    with graph_session() as session:
        records = [record.data() for record in session.run(cypher, repo_id=repo_id, limit=limit)]

    for record in records:
        record["decorators"] = []
        record["path"] = None
    _enrich_from_postgres(db, repo_id, records)

    findings = []
    for record in records:
        verdict = _dead_code_verdict(record)
        if verdict is None:
            continue
        confidence, reason = verdict
        findings.append(
            Finding(
                kind=DebtKind.DEAD_CODE,
                # Never high: a static verdict on a dynamic language does not
                # earn it. The report ranks these below findings that are certain.
                severity=Severity.MEDIUM if confidence >= 0.7 else Severity.LOW,
                subject=record["qualname"] or record["name"] or record["key"],
                path=record["path"],
                line_start=record["line_start"],
                key=record["key"],
                why=reason,
                metrics={
                    "kind": record["kind"],
                    "lines": _lines(record),
                    "decorators": record["decorators"] or [],
                },
                confidence=confidence,
            )
        )
    return findings


def _enrich_from_postgres(
    db: Session, repo_id: str, records: list[dict[str, Any]]
) -> None:
    """Fill in each candidate's decorators and file path, in place.

    One query for the whole batch: a per-candidate lookup would be 500 round
    trips to rule out a handful of routes.
    """
    qualnames = {r["qualname"] for r in records if r.get("qualname")}
    if not qualnames:
        return

    rows = db.execute(
        select(Symbol.module, Symbol.qualname, Symbol.decorators, SourceFile.path)
        .join(SourceFile, Symbol.file_id == SourceFile.id)
        .where(Symbol.repository_id == repo_id, Symbol.qualname.in_(qualnames))
    ).all()

    # Keyed on (module, qualname) to match the graph key's scope, with a
    # qualname-only fallback for symbols in files outside any package, whose
    # graph scope is the file path rather than a module.
    by_scope: dict[tuple[str | None, str], Any] = {}
    by_qualname: dict[str, Any] = {}
    for row in rows:
        by_scope.setdefault((row.module, row.qualname), row)
        by_qualname.setdefault(row.qualname, row)

    for record in records:
        row = by_scope.get((record["module"], record["qualname"])) or by_qualname.get(
            record["qualname"]
        )
        if row is None:
            continue
        record["decorators"] = list(row.decorators or [])
        record["path"] = row.path


def _dead_code_verdict(record: dict[str, Any]) -> tuple[float, str] | None:
    """Confidence that a symbol is really unused, or None to not report it.

    Returning None rather than a low confidence for the clear-cut cases keeps
    the report readable: a test function with no callers is not a finding at all,
    and listing it at 0.1 confidence is noise wearing a number.
    """
    name = record["name"] or ""
    path = record["path"] or ""
    decorators = record["decorators"] or []

    if _DUNDER.match(name):
        return None
    if path and _TEST_PATH.search(path):
        return None
    if name in _ENTRY_POINT_NAMES:
        return None
    if any(_REGISTERED.search(d) for d in decorators):
        return None

    lines = _lines(record)
    # Ceiling of 0.75: a call made at module scope produces no CALLS edge, so
    # nothing here can be certain, however private the symbol looks.
    if name.startswith("_"):
        # A private symbol is still the strongest case: by its own naming it is
        # not part of any external surface.
        return 0.75, (
            f"private {record['kind']}, {lines} lines, no caller inside a function. "
            "A call at module scope would not be visible here"
        )
    if decorators:
        # Decorated but not by anything recognised as a registration — could
        # still be wired up by something ARGUS does not model.
        return 0.5, (
            f"no statically visible caller, but decorated with "
            f"{', '.join(decorators)} — check before deleting"
        )
    return 0.6, (
        f"public {record['kind']}, {lines} lines, no caller or subclass in the "
        "repository — may be public API, or called at module scope"
    )


def _lines(record: dict[str, Any]) -> int:
    start, end = record.get("line_start"), record.get("line_end")
    if start is None or end is None:
        return 0
    return end - start + 1


__all__ = ["MAX_CYCLE_LENGTH", "detect_circular_imports", "detect_dead_code"]
