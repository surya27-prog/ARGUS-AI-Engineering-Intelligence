"""Graph writer tests.

The row-building tests are pure and run anywhere. The rest talk to the real
Neo4j from `docker compose`, in the same spirit as the Postgres tests: each uses
its own repo_id and deletes its own subgraph afterwards, so they neither collide
nor need the database wiped between runs.
"""

from __future__ import annotations

from collections.abc import Generator
from uuid import uuid4

import pytest

from app.core.graph import graph_session
from app.services.graph_keys import file_key, module_key, repo_key, symbol_key, top_level
from app.services.graph_writer import (
    _contains_rows,
    _node_rows,
    count_nodes,
    count_relationships,
    delete_repo_graph,
    write_parsed_repo,
)
from parser.models import (
    FileInfo,
    ImportRef,
    Language,
    ParsedFile,
    ParsedRepo,
    RepoInventory,
    SourceKind,
    Symbol,
    SymbolKind,
)


def _file_info(path: str, module: str | None) -> FileInfo:
    return FileInfo(
        path=path,
        language=Language.PYTHON,
        extension=".py",
        size_bytes=100,
        line_count=20,
        sha256=f"sha-{path}",
        module=module,
    )


def _symbol(
    name: str,
    qualname: str,
    kind: SymbolKind,
    path: str,
    module: str | None,
    parent: str | None = None,
) -> Symbol:
    return Symbol(
        name=name,
        qualname=qualname,
        kind=kind,
        file_path=path,
        line_start=1,
        line_end=5,
        module=module,
        parent=parent,
    )


def _sample_repo(*, include_service: bool = True) -> ParsedRepo:
    """Two packaged files plus one script outside any package.

    `include_service=False` returns the same repo with `app/service.py` deleted,
    which is what the sweep test needs.
    """
    files = [
        ParsedFile(
            path="app/models.py",
            module="app.models",
            symbols=(
                _symbol(
                    "Repository", "Repository", SymbolKind.CLASS, "app/models.py", "app.models"
                ),
                _symbol(
                    "save",
                    "Repository.save",
                    SymbolKind.METHOD,
                    "app/models.py",
                    "app.models",
                    parent="Repository",
                ),
            ),
        ),
        ParsedFile(
            path="scripts/tool.py",
            module=None,
            symbols=(
                _symbol("helper", "helper", SymbolKind.FUNCTION, "scripts/tool.py", None),
            ),
        ),
    ]
    if include_service:
        files.insert(
            1,
            ParsedFile(
                path="app/service.py",
                module="app.service",
                symbols=(
                    _symbol("run", "run", SymbolKind.FUNCTION, "app/service.py", "app.service"),
                    _symbol(
                        "inner",
                        "run.inner",
                        SymbolKind.FUNCTION,
                        "app/service.py",
                        "app.service",
                        parent="run",
                    ),
                ),
            ),
        )

    inventory = RepoInventory(
        name="fixture",
        root="/tmp/fixture",
        source_kind=SourceKind.LOCAL,
        files=tuple(_file_info(f.path, f.module) for f in files),
        commit_sha="abc123",
        default_branch="main",
    )
    return ParsedRepo(inventory=inventory, files=tuple(files))


# --- keys and row building (no database) -------------------------------------


def test_symbol_key_falls_back_to_path_outside_a_package():
    """Two unpackaged files defining `helper` must not collapse into one node."""
    repo = "r1"
    first = symbol_key(repo, None, "helper", "scripts/a.py")
    second = symbol_key(repo, None, "helper", "scripts/b.py")
    assert first != second
    assert first == "sym:r1:scripts/a.py:helper"


def test_symbol_key_uses_module_when_the_file_is_packaged():
    assert symbol_key("r1", "app.models", "Repository.save", "app/models.py") == (
        "sym:r1:app.models:Repository.save"
    )


def test_top_level_strips_the_dotted_tail():
    assert top_level("os.path") == "os"
    assert top_level("urllib3") == "urllib3"


def test_node_rows_cover_every_label():
    rows = _node_rows("r1", _sample_repo(), "run-1")
    assert len(rows["Repo"]) == 1
    assert len(rows["File"]) == 3
    assert len(rows["Class"]) == 1
    # Repository.save, run, run.inner, helper — methods share the Function label
    assert len(rows["Function"]) == 4
    assert {row["run_id"] for batch in rows.values() for row in batch} == {"run-1"}


def test_function_rows_carry_the_function_only_properties():
    rows = _node_rows("r1", _sample_repo(), "run-1")
    run = next(row for row in rows["Function"] if row["qualname"] == "run")
    assert run["kind"] == "function"
    assert run["param_count"] == 0
    assert run["unresolved_calls"] == 0
    save = next(row for row in rows["Function"] if row["qualname"] == "Repository.save")
    assert save["kind"] == "method"


def test_file_rows_take_metrics_from_the_inventory():
    rows = _node_rows("r1", _sample_repo(), "run-1")
    models = next(row for row in rows["File"] if row["path"] == "app/models.py")
    assert models["line_count"] == 20
    assert models["sha256"] == "sha-app/models.py"
    assert models["module"] == "app.models"


def test_contains_rows_nest_by_the_enclosing_symbol_label():
    edges = _contains_rows("r1", _sample_repo())

    assert len(edges[("Repo", "File")]) == 3
    # A method hangs off its class, not off the file.
    assert edges[("Class", "Function")] == [
        {
            "parent": symbol_key("r1", "app.models", "Repository", "app/models.py"),
            "child": symbol_key("r1", "app.models", "Repository.save", "app/models.py"),
        }
    ]
    # A nested function hangs off the enclosing function.
    assert edges[("Function", "Function")] == [
        {
            "parent": symbol_key("r1", "app.service", "run", "app/service.py"),
            "child": symbol_key("r1", "app.service", "run.inner", "app/service.py"),
        }
    ]
    assert {"parent": file_key("r1", "app/models.py"), "child": symbol_key(
        "r1", "app.models", "Repository", "app/models.py"
    )} in edges[("File", "Class")]


def test_contains_rows_fall_back_to_the_file_for_an_unextracted_parent():
    """A def inside an `if` block has a parent qualname with no symbol behind it."""
    orphan = ParsedFile(
        path="app/conditional.py",
        module="app.conditional",
        symbols=(
            _symbol(
                "late",
                "guard.late",
                SymbolKind.FUNCTION,
                "app/conditional.py",
                "app.conditional",
                parent="guard",
            ),
        ),
    )
    parsed = ParsedRepo(
        inventory=RepoInventory(
            name="fixture",
            root="/tmp/fixture",
            source_kind=SourceKind.LOCAL,
            files=(_file_info(orphan.path, orphan.module),),
        ),
        files=(orphan,),
    )
    edges = _contains_rows("r1", parsed)
    assert len(edges[("File", "Function")]) == 1


# --- against a real Neo4j ----------------------------------------------------


@pytest.fixture
def repo_id() -> Generator[str, None, None]:
    """A repo_id nothing else is using, swept from the graph afterwards."""
    identifier = str(uuid4())
    try:
        yield identifier
    finally:
        delete_repo_graph(identifier)


def test_write_creates_the_expected_nodes(repo_id: str):
    result = write_parsed_repo(repo_id, _sample_repo())

    assert result.nodes_written == 9  # 1 repo + 3 files + 1 class + 4 functions
    assert result.relationships_written == 8  # 3 CONTAINS from the repo + 5 within files
    assert count_nodes(repo_id) == {
        "Repo": 1,
        "File": 3,
        "Class": 1,
        "Function": 4,
        "Module": 0,
    }


def test_writing_twice_leaves_the_node_count_unchanged(repo_id: str):
    """The Day 2 acceptance criterion: parse twice, node count unchanged."""
    parsed = _sample_repo()
    write_parsed_repo(repo_id, parsed)
    first = count_nodes(repo_id)

    second_result = write_parsed_repo(repo_id, parsed)
    second = count_nodes(repo_id)

    assert first == second
    # A fresh run stamps every node, so the previous run's stamp is gone and
    # there is nothing left to sweep.
    assert second_result.nodes_deleted == 0
    assert second_result.relationships_deleted == 0


def test_relationships_are_not_duplicated_by_a_second_write(repo_id: str):
    parsed = _sample_repo()
    write_parsed_repo(repo_id, parsed)
    write_parsed_repo(repo_id, parsed)

    with graph_session() as session:
        record = session.run(
            "MATCH (:Repo {key: $key})-[r:CONTAINS]->(:File) RETURN count(r) AS total",
            key=repo_key(repo_id),
        ).single()
    assert record["total"] == 3


def test_sweep_removes_a_file_that_disappeared(repo_id: str):
    write_parsed_repo(repo_id, _sample_repo())

    result = write_parsed_repo(repo_id, _sample_repo(include_service=False))

    # app/service.py plus the two functions it contained.
    assert result.nodes_deleted == 3
    assert count_nodes(repo_id)["File"] == 2
    assert count_nodes(repo_id)["Function"] == 2

    with graph_session() as session:
        record = session.run(
            "MATCH (f:File {key: $key}) RETURN count(f) AS total",
            key=file_key(repo_id, "app/service.py"),
        ).single()
    assert record["total"] == 0


def test_two_repos_do_not_share_nodes(repo_id: str):
    other = str(uuid4())
    try:
        write_parsed_repo(repo_id, _sample_repo())
        write_parsed_repo(other, _sample_repo())
        assert count_nodes(repo_id)["File"] == 3
        assert count_nodes(other)["File"] == 3
    finally:
        delete_repo_graph(other)


def test_containment_path_reaches_a_method_from_the_repo(repo_id: str):
    write_parsed_repo(repo_id, _sample_repo())

    with graph_session() as session:
        record = session.run(
            """
            MATCH (:Repo {key: $key})-[:CONTAINS*]->(fn:Function {qualname: 'Repository.save'})
            RETURN count(fn) AS total
            """,
            key=repo_key(repo_id),
        ).single()
    assert record["total"] == 1


# --- imports, against a real Neo4j ------------------------------------------


def _importing_repo(*, keep_external: bool = True) -> ParsedRepo:
    """`app/api/health.py` importing one internal module and one package.

    `keep_external=False` drops the third-party import, which is what the
    import sweep test needs.
    """
    internal = ImportRef(module="app.core.config", name="Settings", line=4, is_from=True)
    relative = ImportRef(module="core.config", name="get_settings", line=5, level=2, is_from=True)
    external = ImportRef(module="fastapi", line=1)

    health_imports = (external, internal, relative) if keep_external else (internal, relative)
    files = (
        ParsedFile(path="app/__init__.py", module="app"),
        ParsedFile(path="app/core/__init__.py", module="app.core"),
        ParsedFile(path="app/core/config.py", module="app.core.config"),
        ParsedFile(path="app/api/__init__.py", module="app.api"),
        ParsedFile(path="app/api/health.py", module="app.api.health", imports=health_imports),
    )
    inventory = RepoInventory(
        name="fixture",
        root="/tmp/fixture",
        source_kind=SourceKind.LOCAL,
        files=tuple(_file_info(f.path, f.module) for f in files),
    )
    return ParsedRepo(inventory=inventory, files=files)


def test_imports_create_edges_to_files_and_modules(repo_id: str):
    write_parsed_repo(repo_id, _importing_repo())

    assert count_nodes(repo_id)["Module"] == 1
    assert count_relationships(repo_id) == {
        "internal_symbol": 1,
        "relative": 1,
        "external": 1,
    }


def test_an_internal_import_points_at_the_defining_file(repo_id: str):
    write_parsed_repo(repo_id, _importing_repo())

    with graph_session() as session:
        record = session.run(
            """
            MATCH (:File {key: $source})-[r:IMPORTS]->(t:File)
            RETURN t.path AS path, r.resolution AS resolution, r.line AS line
            ORDER BY line
            """,
            source=file_key(repo_id, "app/api/health.py"),
        ).data()

    assert record == [
        {"path": "app/core/config.py", "resolution": "internal_symbol", "line": 4},
        {"path": "app/core/config.py", "resolution": "relative", "line": 5},
    ]


def test_the_external_module_node_records_its_installable_name(repo_id: str):
    write_parsed_repo(repo_id, _importing_repo())

    with graph_session() as session:
        record = session.run(
            "MATCH (m:Module {key: $key}) RETURN m.dotted_name AS dotted, "
            "m.top_level AS top, m.is_external AS external",
            key=module_key(repo_id, "fastapi"),
        ).single()

    assert record["dotted"] == "fastapi"
    assert record["top"] == "fastapi"
    assert record["external"] is True


def test_import_edges_are_not_duplicated_by_a_second_write(repo_id: str):
    parsed = _importing_repo()
    write_parsed_repo(repo_id, parsed)
    first = count_relationships(repo_id)

    result = write_parsed_repo(repo_id, parsed)

    assert count_relationships(repo_id) == first
    assert result.relationships_deleted == 0
    assert result.nodes_deleted == 0


def test_sweep_removes_an_import_that_disappeared(repo_id: str):
    write_parsed_repo(repo_id, _importing_repo())

    result = write_parsed_repo(repo_id, _importing_repo(keep_external=False))

    # The :Module node goes with it — nothing imports fastapi any more.
    assert result.nodes_deleted == 1
    assert count_nodes(repo_id)["Module"] == 0
    assert "external" not in count_relationships(repo_id)


def test_delete_repo_graph_removes_everything(repo_id: str):
    write_parsed_repo(repo_id, _sample_repo())
    deleted = delete_repo_graph(repo_id)
    assert deleted == 9
    assert set(count_nodes(repo_id).values()) == {0}
