from __future__ import annotations

from pathlib import Path

import pytest

from parser.extractor import extract_file, extract_source
from parser.models import ParameterKind, SymbolKind


@pytest.fixture
def simple(fixtures_dir: Path):
    return extract_file(fixtures_dir / "simple_module.py", module="simple_module")


@pytest.fixture
def classes(fixtures_dir: Path):
    return extract_file(fixtures_dir / "classes_module.py", module="classes_module")


@pytest.fixture
def edges(fixtures_dir: Path):
    return extract_file(fixtures_dir / "edge_cases.py", module="edge_cases")


# --- fixture 1: functions and plain imports ---------------------------------


def test_simple_module_symbols(simple):
    assert simple.ok
    assert [s.qualname for s in simple.symbols] == [
        "read_config",
        "config_dir",
        "load_async",
        "outer",
        "outer.inner",
    ]
    assert all(s.kind is SymbolKind.FUNCTION for s in simple.symbols)


def test_nested_function_records_its_parent(simple):
    inner = next(s for s in simple.symbols if s.name == "inner")
    assert inner.qualname == "outer.inner"
    assert inner.parent == "outer"
    assert inner.module == "simple_module"


def test_signature_and_docstring(simple):
    read_config = simple.symbols[0]
    assert read_config.docstring == "Read a config file and return its contents."
    assert read_config.returns == "str"
    assert [(p.name, p.kind, p.annotation) for p in read_config.parameters] == [
        ("path", ParameterKind.POSITIONAL_OR_KEYWORD, "str"),
        ("encoding", ParameterKind.KEYWORD_ONLY, "str"),
    ]
    assert read_config.parameters[1].default == "'utf-8'"


def test_async_functions_are_flagged(simple):
    assert next(s for s in simple.symbols if s.name == "load_async").is_async
    assert not simple.symbols[0].is_async


def test_plain_imports(simple):
    assert [(i.target, i.bound_name) for i in simple.imports] == [
        ("os", "os"),
        ("os.path", "ospath"),
        ("pathlib.Path", "Path"),
    ]
    assert not any(i.is_relative for i in simple.imports)


def test_calls_are_attributed_to_the_enclosing_function(simple):
    by_callee = {c.callee: c for c in simple.calls}
    assert by_callee["Path"].caller == "read_config"
    assert by_callee["ospath.dirname"].caller == "config_dir"
    assert by_callee["read_config"].caller == "load_async"
    assert by_callee["inner"].caller == "outer"


def test_module_docstring_is_captured(simple):
    assert simple.docstring.startswith("Module-level functions")


# --- fixture 2: classes, inheritance, decorators -----------------------------


def test_class_symbols_and_bases(classes):
    by_name = {s.qualname: s for s in classes.symbols}
    assert by_name["Status"].base_classes == ("StrEnum",)
    assert by_name["Record"].base_classes == ("Base",)
    assert by_name["Base"].base_classes == ()
    assert by_name["Record"].kind is SymbolKind.CLASS


def test_methods_are_distinct_from_functions(classes):
    methods = [s.qualname for s in classes.symbols if s.kind is SymbolKind.METHOD]
    assert methods == ["Base.describe", "Record.label", "Record.build", "Record.refresh"]
    assert not [s for s in classes.symbols if s.kind is SymbolKind.FUNCTION]


def test_nested_class_qualname(classes):
    meta = next(s for s in classes.symbols if s.name == "Meta")
    assert meta.qualname == "Record.Meta"
    assert meta.parent == "Record"
    assert meta.kind is SymbolKind.CLASS


def test_decorators_are_recorded_with_their_arguments(classes):
    by_name = {s.qualname: s for s in classes.symbols}
    assert by_name["Record"].decorators == ("dataclass(frozen=True)",)
    assert by_name["Record.label"].decorators == ("property",)
    assert by_name["Record.build"].decorators == ("staticmethod",)


def test_async_method_and_self_parameter(classes):
    refresh = next(s for s in classes.symbols if s.name == "refresh")
    assert refresh.is_async
    assert [p.name for p in refresh.parameters] == ["self", "force"]
    assert refresh.parameters[1].kind is ParameterKind.KEYWORD_ONLY


def test_method_call_on_self_is_recorded(classes):
    call = next(c for c in classes.calls if c.callee == "self.describe")
    assert call.caller == "Record.refresh"
    assert call.is_attribute_call


def test_decorator_call_belongs_to_the_enclosing_scope(classes):
    # `@dataclass(frozen=True)` runs at module import, not inside Record.
    call = next(c for c in classes.calls if c.callee == "dataclass")
    assert call.caller is None


def test_line_ranges_cover_the_definition(classes):
    record = next(s for s in classes.symbols if s.qualname == "Record")
    source_lines = len(
        (Path(__file__).parent / "fixtures" / "classes_module.py").read_text().splitlines()
    )
    assert record.line_start < record.line_end <= source_lines
    assert record.line_count == record.line_end - record.line_start + 1


# --- fixture 3: relative imports and awkward call sites ----------------------


def test_relative_imports_keep_their_depth(edges):
    by_target = {i.target: i for i in edges.imports}
    assert by_target[".sibling"].level == 1
    assert by_target["..shared"].level == 2
    assert by_target["..core.config.get_settings"].alias == "settings_factory"
    assert by_target["..core.config.get_settings"].bound_name == "settings_factory"
    assert not by_target["functools"].is_relative


def test_multi_name_import_becomes_one_ref_each(edges):
    from_helpers = [i for i in edges.imports if i.module == "helpers"]
    assert [i.name for i in from_helpers] == ["first", "second"]
    assert all(i.level == 1 for i in from_helpers)


def test_every_parameter_kind(edges):
    cached = next(s for s in edges.symbols if s.name == "cached")
    assert [(p.name, p.kind) for p in cached.parameters] == [
        ("a", ParameterKind.POSITIONAL_ONLY),
        ("b", ParameterKind.POSITIONAL_OR_KEYWORD),
        ("c", ParameterKind.POSITIONAL_OR_KEYWORD),
        ("args", ParameterKind.VAR_POSITIONAL),
        ("d", ParameterKind.KEYWORD_ONLY),
        ("e", ParameterKind.KEYWORD_ONLY),
        ("kwargs", ParameterKind.VAR_KEYWORD),
    ]
    assert cached.parameters[2].default == "3"
    assert cached.parameters[4].default is None
    assert cached.parameters[5].default == "5"


def test_calls_inside_comprehensions_are_found(edges):
    callers = {c.callee: c.caller for c in edges.calls}
    assert callers["first"] == "comprehensions"
    assert callers["second"] == "comprehensions"


def test_unnameable_calls_are_dropped_not_crashed(edges):
    unnameable = [c for c in edges.calls if c.caller == "unnameable"]
    assert [c.callee for c in unnameable] == ["len"]


def test_chained_attribute_call(edges):
    assert any(c.callee == "shared.helper.load" for c in edges.calls)


def test_aliased_import_is_recorded_as_written(edges):
    # The call site says `settings_factory()`; mapping that back to
    # `core.config.get_settings` is Week 2's resolution pass.
    assert any(c.callee == "settings_factory" for c in edges.calls)


# --- failure handling --------------------------------------------------------


def test_syntax_error_is_returned_not_raised(fixtures_dir: Path):
    parsed = extract_file(fixtures_dir / "broken_syntax.py")
    assert not parsed.ok
    assert "SyntaxError" in parsed.error
    assert parsed.symbols == ()


def test_missing_file_is_reported(tmp_path: Path):
    parsed = extract_file(tmp_path / "nope.py")
    assert not parsed.ok
    assert "FileNotFoundError" in parsed.error


def test_repo_relative_path_is_used(fixtures_dir: Path):
    parsed = extract_file(fixtures_dir / "simple_module.py", repo_root=fixtures_dir.parent)
    assert parsed.path == "fixtures/simple_module.py"
    assert parsed.symbols[0].file_path == "fixtures/simple_module.py"


def test_empty_source_is_valid(tmp_path: Path):
    parsed = extract_source("", path="empty.py")
    assert parsed.ok
    assert parsed.symbols == ()
    assert parsed.docstring is None


def test_non_utf8_source_parses_via_coding_declaration():
    source = b"# -*- coding: latin-1 -*-\nNAME = 'caf\xe9'\n"
    assert extract_source(source, path="latin.py").ok


def test_classes_and_functions_helpers(classes, simple):
    assert {s.name for s in classes.classes} == {"Status", "Base", "Record", "Meta"}
    assert len(simple.functions) == 5
    assert simple.classes == ()
