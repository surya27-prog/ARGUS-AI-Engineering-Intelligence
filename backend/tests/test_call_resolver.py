"""Call-graph and inheritance resolution. Pure functions, no database.

The fixture repo is deliberately small but covers every tier the schema doc
defines, plus the cases that must produce *no* edge.
"""

from __future__ import annotations

import pytest

from app.services.call_resolver import (
    CONFIDENCE,
    CallResolution,
    UnresolvedReason,
    resolve_call_graph,
)
from app.services.import_resolver import resolve_imports
from parser.models import (
    CallRef,
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


def _sym(
    name: str,
    qualname: str,
    kind: SymbolKind,
    path: str,
    module: str | None,
    parent: str | None = None,
    bases: tuple[str, ...] = (),
) -> Symbol:
    return Symbol(
        name=name,
        qualname=qualname,
        kind=kind,
        file_path=path,
        line_start=1,
        line_end=9,
        module=module,
        parent=parent,
        base_classes=bases,
    )


def _repo(files: tuple[ParsedFile, ...]) -> ParsedRepo:
    inventory = RepoInventory(
        name="fixture",
        root="/tmp/fixture",
        source_kind=SourceKind.LOCAL,
        files=tuple(
            FileInfo(
                path=f.path,
                language=Language.PYTHON,
                extension=".py",
                size_bytes=1,
                line_count=9,
                sha256="x",
                module=f.module,
            )
            for f in files
        ),
    )
    return ParsedRepo(inventory=inventory, files=files)


def _graph(files: tuple[ParsedFile, ...]):
    parsed = _repo(files)
    return resolve_call_graph(parsed, resolve_imports(parsed))


def _edge(graph, caller: str, callee: str):
    matches = [
        e for e in graph.edges if e.caller.qualname == caller and e.callee.qualname == callee
    ]
    assert len(matches) == 1, f"expected one {caller} -> {callee}, got {len(matches)}"
    return matches[0]


# --- local ------------------------------------------------------------------


def test_a_bare_call_to_a_function_in_the_same_file_is_local():
    files = (
        ParsedFile(
            path="app/util.py",
            module="app.util",
            symbols=(
                _sym("helper", "helper", SymbolKind.FUNCTION, "app/util.py", "app.util"),
                _sym("run", "run", SymbolKind.FUNCTION, "app/util.py", "app.util"),
            ),
            calls=(CallRef(callee="helper", line=7, caller="run", file_path="app/util.py"),),
        ),
    )
    edge = _edge(_graph(files), "run", "helper")
    assert edge.resolution is CallResolution.LOCAL
    assert edge.confidence == 1.0


def test_calls_are_aggregated_into_one_edge_with_a_count():
    files = (
        ParsedFile(
            path="app/util.py",
            module="app.util",
            symbols=(
                _sym("helper", "helper", SymbolKind.FUNCTION, "app/util.py", "app.util"),
                _sym("run", "run", SymbolKind.FUNCTION, "app/util.py", "app.util"),
            ),
            calls=tuple(
                CallRef(callee="helper", line=line, caller="run", file_path="app/util.py")
                for line in (7, 12, 12, 20)
            ),
        ),
    )
    edge = _edge(_graph(files), "run", "helper")
    # Two call sites on line 12 collapse: the lines are a set, not a bag.
    assert edge.lines == (7, 12, 20)
    assert edge.count == 3


def test_a_module_level_definition_wins_over_a_nested_one():
    files = (
        ParsedFile(
            path="app/util.py",
            module="app.util",
            symbols=(
                _sym("helper", "helper", SymbolKind.FUNCTION, "app/util.py", "app.util"),
                _sym("run", "run", SymbolKind.FUNCTION, "app/util.py", "app.util"),
                _sym(
                    "helper",
                    "run.helper",
                    SymbolKind.FUNCTION,
                    "app/util.py",
                    "app.util",
                    parent="run",
                ),
            ),
            calls=(CallRef(callee="helper", line=7, caller="run", file_path="app/util.py"),),
        ),
    )
    assert _edge(_graph(files), "run", "helper").resolution is CallResolution.LOCAL


def test_constructing_a_class_is_a_call_edge_to_the_class():
    files = (
        ParsedFile(
            path="app/util.py",
            module="app.util",
            symbols=(
                _sym("Settings", "Settings", SymbolKind.CLASS, "app/util.py", "app.util"),
                _sym("run", "run", SymbolKind.FUNCTION, "app/util.py", "app.util"),
            ),
            calls=(CallRef(callee="Settings", line=7, caller="run", file_path="app/util.py"),),
        ),
    )
    edge = _edge(_graph(files), "run", "Settings")
    assert edge.callee.label == "Class"


# --- imported ---------------------------------------------------------------


def _two_file_repo(calls: tuple[CallRef, ...], imports: tuple[ImportRef, ...]):
    return (
        ParsedFile(
            path="app/core/config.py",
            module="app.core.config",
            symbols=(
                _sym(
                    "get_settings",
                    "get_settings",
                    SymbolKind.FUNCTION,
                    "app/core/config.py",
                    "app.core.config",
                ),
            ),
        ),
        ParsedFile(
            path="app/api.py",
            module="app.api",
            symbols=(_sym("handler", "handler", SymbolKind.FUNCTION, "app/api.py", "app.api"),),
            imports=imports,
            calls=calls,
        ),
    )


def test_a_name_bound_by_an_import_resolves_across_files():
    files = _two_file_repo(
        calls=(CallRef(callee="get_settings", line=5, caller="handler", file_path="app/api.py"),),
        imports=(
            ImportRef(
                module="app.core.config", name="get_settings", line=1, is_from=True
            ),
        ),
    )
    edge = _edge(_graph(files), "handler", "get_settings")
    assert edge.resolution is CallResolution.IMPORTED
    assert edge.confidence == 0.95
    assert edge.callee.path == "app/core/config.py"


def test_an_aliased_import_resolves_to_the_original_name():
    """`from x import get_settings as factory` then `factory()`."""
    files = _two_file_repo(
        calls=(CallRef(callee="factory", line=5, caller="handler", file_path="app/api.py"),),
        imports=(
            ImportRef(
                module="app.core.config",
                name="get_settings",
                alias="factory",
                line=1,
                is_from=True,
            ),
        ),
    )
    assert _edge(_graph(files), "handler", "get_settings").resolution is CallResolution.IMPORTED


def test_a_call_into_a_third_party_package_produces_no_edge():
    files = _two_file_repo(
        calls=(CallRef(callee="get", line=5, caller="handler", file_path="app/api.py"),),
        imports=(ImportRef(module="requests", name="get", line=1, is_from=True),),
    )
    graph = _graph(files)
    assert graph.edges == []
    assert graph.unresolved_by_caller[("app/api.py", "handler")] == 1


# --- attribute_module -------------------------------------------------------


def test_a_module_attribute_call_resolves_through_the_bound_name():
    """`from app.core import config` then `config.get_settings()`."""
    files = _two_file_repo(
        calls=(
            CallRef(callee="config.get_settings", line=5, caller="handler", file_path="app/api.py"),
        ),
        imports=(ImportRef(module="app.core", name="config", line=1, is_from=True),),
    )
    edge = _edge(_graph(files), "handler", "get_settings")
    assert edge.resolution is CallResolution.ATTRIBUTE_MODULE
    assert edge.confidence == 0.90


def test_a_dotted_call_resolves_by_longest_module_prefix():
    """`import app.core.config` binds only `app`, but the full path still resolves."""
    files = _two_file_repo(
        calls=(
            CallRef(
                callee="app.core.config.get_settings",
                line=5,
                caller="handler",
                file_path="app/api.py",
            ),
        ),
        imports=(ImportRef(module="app.core.config", line=1),),
    )
    assert (
        _edge(_graph(files), "handler", "get_settings").resolution
        is CallResolution.ATTRIBUTE_MODULE
    )


def test_a_re_exported_name_is_followed_through_the_package_init():
    """`requests/__init__.py` does `from .api import get`; `requests.get()` works."""
    files = (
        ParsedFile(
            path="pkg/api.py",
            module="pkg.api",
            symbols=(_sym("get", "get", SymbolKind.FUNCTION, "pkg/api.py", "pkg.api"),),
        ),
        ParsedFile(
            path="pkg/__init__.py",
            module="pkg",
            imports=(ImportRef(module="api", name="get", line=1, level=1, is_from=True),),
        ),
        ParsedFile(
            path="tests/test_api.py",
            module=None,
            symbols=(
                _sym("test_it", "test_it", SymbolKind.FUNCTION, "tests/test_api.py", None),
            ),
            imports=(ImportRef(module="pkg", line=1),),
            calls=(
                CallRef(callee="pkg.get", line=4, caller="test_it", file_path="tests/test_api.py"),
            ),
        ),
    )
    edge = _edge(_graph(files), "test_it", "get")
    assert edge.callee.path == "pkg/api.py"
    assert edge.resolution is CallResolution.ATTRIBUTE_MODULE


def test_a_circular_re_export_terminates():
    """Two modules importing each other's name must not loop."""
    files = (
        ParsedFile(
            path="pkg/a.py",
            module="pkg.a",
            imports=(ImportRef(module="pkg.b", name="thing", line=1, is_from=True),),
        ),
        ParsedFile(
            path="pkg/b.py",
            module="pkg.b",
            imports=(ImportRef(module="pkg.a", name="thing", line=1, is_from=True),),
        ),
        ParsedFile(
            path="app.py",
            module=None,
            symbols=(_sym("run", "run", SymbolKind.FUNCTION, "app.py", None),),
            imports=(ImportRef(module="pkg.a", name="thing", line=1, is_from=True),),
            calls=(CallRef(callee="thing", line=3, caller="run", file_path="app.py"),),
        ),
    )
    assert _graph(files).edges == []


def test_an_external_module_attribute_call_produces_no_edge():
    files = _two_file_repo(
        calls=(CallRef(callee="os.path.join", line=5, caller="handler", file_path="app/api.py"),),
        imports=(ImportRef(module="os.path", line=1),),
    )
    assert _graph(files).edges == []


# --- attribute_self ---------------------------------------------------------


def _class_repo(bases: tuple[str, ...] = (), extra: tuple[Symbol, ...] = ()):
    path = "app/models.py"
    return (
        ParsedFile(
            path=path,
            module="app.models",
            symbols=(
                _sym("Repo", "Repo", SymbolKind.CLASS, path, "app.models", bases=bases),
                _sym("save", "Repo.save", SymbolKind.METHOD, path, "app.models", parent="Repo"),
                _sym(
                    "validate",
                    "Repo.validate",
                    SymbolKind.METHOD,
                    path,
                    "app.models",
                    parent="Repo",
                ),
                *extra,
            ),
            calls=(CallRef(callee="self.validate", line=6, caller="Repo.save", file_path=path),),
        ),
    )


def test_a_self_call_resolves_against_the_enclosing_class():
    edge = _edge(_graph(_class_repo()), "Repo.save", "Repo.validate")
    assert edge.resolution is CallResolution.ATTRIBUTE_SELF
    assert edge.confidence == 0.85


def test_a_self_call_finds_a_method_on_an_in_repo_base_class():
    path = "app/models.py"
    files = (
        ParsedFile(
            path=path,
            module="app.models",
            symbols=(
                _sym("Base", "Base", SymbolKind.CLASS, path, "app.models"),
                _sym(
                    "validate",
                    "Base.validate",
                    SymbolKind.METHOD,
                    path,
                    "app.models",
                    parent="Base",
                ),
                _sym("Repo", "Repo", SymbolKind.CLASS, path, "app.models", bases=("Base",)),
                _sym("save", "Repo.save", SymbolKind.METHOD, path, "app.models", parent="Repo"),
            ),
            calls=(CallRef(callee="self.validate", line=6, caller="Repo.save", file_path=path),),
        ),
    )
    edge = _edge(_graph(files), "Repo.save", "Base.validate")
    assert edge.resolution is CallResolution.ATTRIBUTE_SELF


def test_self_resolution_survives_a_cyclic_hierarchy():
    """A malformed hierarchy must not hang the pass."""
    path = "app/models.py"
    files = (
        ParsedFile(
            path=path,
            module="app.models",
            symbols=(
                _sym("A", "A", SymbolKind.CLASS, path, "app.models", bases=("B",)),
                _sym("B", "B", SymbolKind.CLASS, path, "app.models", bases=("A",)),
                _sym("go", "A.go", SymbolKind.METHOD, path, "app.models", parent="A"),
            ),
            calls=(CallRef(callee="self.missing", line=3, caller="A.go", file_path=path),),
        ),
    )
    assert _graph(files).edges == []


def test_a_chained_self_attribute_is_left_unresolved():
    """`self.client.get()` needs the type of `self.client` — out of scope."""
    path = "app/models.py"
    files = (
        ParsedFile(
            path=path,
            module="app.models",
            symbols=(
                _sym("Repo", "Repo", SymbolKind.CLASS, path, "app.models"),
                _sym("save", "Repo.save", SymbolKind.METHOD, path, "app.models", parent="Repo"),
            ),
            calls=(CallRef(callee="self.client.get", line=6, caller="Repo.save", file_path=path),),
        ),
    )
    graph = _graph(files)
    assert graph.edges == []
    assert graph.unresolved_by_caller[(path, "Repo.save")] == 1


def test_self_inside_a_nested_function_still_finds_the_method_s_class():
    path = "app/models.py"
    files = (
        ParsedFile(
            path=path,
            module="app.models",
            symbols=(
                _sym("Repo", "Repo", SymbolKind.CLASS, path, "app.models"),
                _sym("save", "Repo.save", SymbolKind.METHOD, path, "app.models", parent="Repo"),
                _sym(
                    "validate",
                    "Repo.validate",
                    SymbolKind.METHOD,
                    path,
                    "app.models",
                    parent="Repo",
                ),
                _sym(
                    "inner",
                    "Repo.save.inner",
                    SymbolKind.FUNCTION,
                    path,
                    "app.models",
                    parent="Repo.save",
                ),
            ),
            calls=(
                CallRef(
                    callee="self.validate", line=8, caller="Repo.save.inner", file_path=path
                ),
            ),
        ),
    )
    assert _edge(_graph(files), "Repo.save.inner", "Repo.validate").resolution is (
        CallResolution.ATTRIBUTE_SELF
    )


# --- what must produce no edge ----------------------------------------------


def test_a_module_level_call_has_no_function_to_hang_the_edge_on():
    files = (
        ParsedFile(
            path="app/util.py",
            module="app.util",
            symbols=(_sym("helper", "helper", SymbolKind.FUNCTION, "app/util.py", "app.util"),),
            calls=(CallRef(callee="helper", line=3, caller=None, file_path="app/util.py"),),
        ),
    )
    graph = _graph(files)
    assert graph.edges == []
    assert graph.reasons[UnresolvedReason.MODULE_LEVEL] == 1
    # Module-level calls are not a function's unresolved calls.
    assert graph.unresolved_by_caller == {}


def test_unresolved_calls_are_counted_on_the_calling_function():
    files = (
        ParsedFile(
            path="app/util.py",
            module="app.util",
            symbols=(_sym("run", "run", SymbolKind.FUNCTION, "app/util.py", "app.util"),),
            calls=(
                CallRef(callee="nowhere", line=3, caller="run", file_path="app/util.py"),
                CallRef(callee="also_nowhere", line=4, caller="run", file_path="app/util.py"),
            ),
        ),
    )
    graph = _graph(files)
    assert graph.unresolved_by_caller == {("app/util.py", "run"): 2}
    assert graph.unresolved_total == 2


def test_an_ambiguous_bare_name_produces_no_edge():
    """Two nested defs of one name, neither at module level: nothing to choose."""
    path = "app/util.py"
    files = (
        ParsedFile(
            path=path,
            module="app.util",
            symbols=(
                _sym("a", "a", SymbolKind.FUNCTION, path, "app.util"),
                _sym("b", "b", SymbolKind.FUNCTION, path, "app.util"),
                _sym("run", "run", SymbolKind.FUNCTION, path, "app.util"),
                _sym("dup", "a.dup", SymbolKind.FUNCTION, path, "app.util", parent="a"),
                _sym("dup", "b.dup", SymbolKind.FUNCTION, path, "app.util", parent="b"),
            ),
            calls=(CallRef(callee="dup", line=9, caller="run", file_path=path),),
        ),
    )
    graph = _graph(files)
    assert graph.edges == []
    assert graph.reasons[UnresolvedReason.AMBIGUOUS] == 1
    assert graph.unresolved_by_caller[(path, "run")] == 1


# --- inheritance ------------------------------------------------------------


def test_an_unresolvable_base_becomes_an_external_class():
    graph = _graph(_class_repo(bases=("BaseSettings",)))
    external = [b for b in graph.bases if b.external_name]
    assert len(external) == 1
    assert external[0].external_name == "BaseSettings"
    assert external[0].resolution == "external"


def test_a_subscripted_base_drops_its_type_parameters():
    graph = _graph(_class_repo(bases=("Generic[T]",)))
    assert [b.external_name for b in graph.bases] == ["Generic"]


def test_base_position_preserves_mro_order():
    graph = _graph(_class_repo(bases=("First", "Second")))
    assert [(b.position, b.external_name) for b in graph.bases] == [(0, "First"), (1, "Second")]


def test_a_base_imported_from_another_file_resolves_internally():
    files = (
        ParsedFile(
            path="app/base.py",
            module="app.base",
            symbols=(_sym("Base", "Base", SymbolKind.CLASS, "app/base.py", "app.base"),),
        ),
        ParsedFile(
            path="app/models.py",
            module="app.models",
            symbols=(
                _sym(
                    "Repo",
                    "Repo",
                    SymbolKind.CLASS,
                    "app/models.py",
                    "app.models",
                    bases=("Base",),
                ),
            ),
            imports=(ImportRef(module="app.base", name="Base", line=1, is_from=True),),
        ),
    )
    graph = _graph(files)
    assert len(graph.bases) == 1
    assert graph.bases[0].resolution == "internal"
    assert graph.bases[0].base.path == "app/base.py"


@pytest.mark.parametrize(
    ("resolution", "expected"),
    [
        (CallResolution.LOCAL, 1.0),
        (CallResolution.IMPORTED, 0.95),
        (CallResolution.ATTRIBUTE_MODULE, 0.90),
        (CallResolution.ATTRIBUTE_SELF, 0.85),
    ],
)
def test_confidences_match_the_schema_doc(resolution: CallResolution, expected: float):
    assert CONFIDENCE[resolution] == expected
