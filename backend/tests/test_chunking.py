"""Chunking strategy tests.

Pure functions over a parse result plus files on disk — no embedding provider,
no Qdrant, no key. The strategy is where retrieval quality is decided, so it
gets tested exhaustively here rather than inferred from search results later.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.chunking import (
    MAX_CHUNK_CHARS,
    CodeChunk,
    chunk_repo,
)
from parser import analyze_repo
from parser.models import (
    FileInfo,
    Language,
    Parameter,
    ParsedFile,
    ParsedRepo,
    RepoInventory,
    SourceKind,
    Symbol,
    SymbolKind,
)

REPO_ID = "11111111-1111-1111-1111-111111111111"

SOURCE = '''"""Module docstring."""

import os
from typing import Any

TIMEOUT = 30


def helper(value: str) -> str:
    """Normalise a value."""
    return value.strip().lower()


class Session:
    """An HTTP session."""

    adapters: dict[str, Any]

    def get(self, url: str) -> str:
        """Issue a GET."""
        return self.request("GET", url)

    def request(self, method: str, url: str) -> str:
        """Issue a request."""
        return f"{method} {url}"
'''


def _symbol(name, qualname, kind, module, start, end, parent=None, params=(), bases=()):
    return Symbol(
        name=name,
        qualname=qualname,
        kind=kind,
        file_path="app/session.py",
        line_start=start,
        line_end=end,
        module=module,
        parent=parent,
        parameters=tuple(Parameter(name=p) for p in params),
        base_classes=bases,
    )


@pytest.fixture
def repo(tmp_path: Path) -> ParsedRepo:
    """The SOURCE above, on disk, with its symbols already extracted."""
    path = tmp_path / "app" / "session.py"
    path.parent.mkdir(parents=True)
    path.write_text(SOURCE, encoding="utf-8")

    parsed_file = ParsedFile(
        path="app/session.py",
        module="app.session",
        language=Language.PYTHON,
        symbols=(
            _symbol(
                "helper", "helper", SymbolKind.FUNCTION, "app.session", 9, 11,
                params=["value"],
            ),
            _symbol("Session", "Session", SymbolKind.CLASS, "app.session", 14, 26),
            _symbol(
                "get", "Session.get", SymbolKind.METHOD, "app.session", 19, 21,
                parent="Session", params=["self", "url"],
            ),
            _symbol(
                "request", "Session.request", SymbolKind.METHOD, "app.session", 23, 25,
                parent="Session", params=["self", "method", "url"],
            ),
        ),
    )
    inventory = RepoInventory(
        name="fixture",
        root=str(tmp_path),
        source_kind=SourceKind.LOCAL,
        files=(
            FileInfo(
                path="app/session.py",
                language=Language.PYTHON,
                extension=".py",
                size_bytes=len(SOURCE),
                line_count=len(SOURCE.splitlines()),
                sha256="x",
                module="app.session",
            ),
        ),
    )
    return ParsedRepo(inventory=inventory, files=(parsed_file,))


def _by_qualname(result, qualname: str) -> CodeChunk:
    matches = [c for c in result.chunks if c.qualname == qualname]
    assert len(matches) == 1, f"expected one chunk for {qualname}, got {len(matches)}"
    return matches[0]


# --- the unit of chunking ----------------------------------------------------


def test_each_function_and_method_becomes_its_own_chunk(repo):
    result = chunk_repo(REPO_ID, repo)
    assert {c.qualname for c in result.chunks if c.kind in ("function", "method")} == {
        "helper",
        "Session.get",
        "Session.request",
    }


def test_a_function_chunk_holds_its_whole_body_and_docstring(repo):
    chunk = _by_qualname(chunk_repo(REPO_ID, repo), "helper")
    assert "def helper(value: str)" in chunk.text
    assert "Normalise a value." in chunk.text
    assert "return value.strip().lower()" in chunk.text


def test_every_chunk_carries_a_path_header(repo):
    """A bare body is ambiguous — `def get(self, url)` is in a hundred repos."""
    for chunk in chunk_repo(REPO_ID, repo).chunks:
        assert chunk.text.startswith("File: app/session.py")
        assert "Module: app.session" in chunk.text


# --- classes: outline, not full body -----------------------------------------


def test_a_class_is_chunked_as_an_outline_not_its_whole_body(repo):
    """Otherwise every method is stored twice and returned at two ranks."""
    chunk = _by_qualname(chunk_repo(REPO_ID, repo), "Session")

    assert "class Session:" in chunk.text
    assert "An HTTP session." in chunk.text
    # The method *bodies* belong to the method chunks.
    assert 'return f"{method} {url}"' not in chunk.text


def test_the_class_outline_lists_its_method_signatures(repo):
    """So "what can this class do" is answerable, which no method chunk is."""
    chunk = _by_qualname(chunk_repo(REPO_ID, repo), "Session")
    assert "def get(self, url)" in chunk.text
    assert "def request(self, method, url)" in chunk.text


def test_a_method_body_appears_in_exactly_one_chunk(repo):
    result = chunk_repo(REPO_ID, repo)
    holders = [c for c in result.chunks if 'return f"{method} {url}"' in c.text]
    assert [c.qualname for c in holders] == ["Session.request"]


# --- module-level code -------------------------------------------------------


def test_code_outside_any_symbol_becomes_a_module_chunk(repo):
    """Imports and constants answer real questions and belong to no function."""
    result = chunk_repo(REPO_ID, repo)
    module_chunks = [c for c in result.chunks if c.kind == "module"]

    assert len(module_chunks) == 1
    text = module_chunks[0].text
    assert "import os" in text
    assert "TIMEOUT = 30" in text
    # Not a second copy of the definitions.
    assert "def helper" not in text


def test_a_file_whose_every_line_is_a_symbol_gets_no_module_chunk(tmp_path):
    source = "def only():\n    return 1\n"
    (tmp_path / "solo.py").write_text(source, encoding="utf-8")
    parsed_file = ParsedFile(
        path="solo.py",
        module="solo",
        symbols=(_symbol("only", "only", SymbolKind.FUNCTION, "solo", 1, 2),),
    )
    parsed = ParsedRepo(
        inventory=RepoInventory(
            name="x", root=str(tmp_path), source_kind=SourceKind.LOCAL,
            files=(FileInfo("solo.py", Language.PYTHON, ".py", 1, 2, "x", "solo"),),
        ),
        files=(parsed_file,),
    )
    assert [c.kind for c in chunk_repo(REPO_ID, parsed).chunks] == ["function"]


# --- identity and the graph join ---------------------------------------------


def test_chunk_ids_are_stable_across_runs(repo):
    """Re-embedding must overwrite, not duplicate."""
    first = {c.id for c in chunk_repo(REPO_ID, repo).chunks}
    second = {c.id for c in chunk_repo(REPO_ID, repo).chunks}
    assert first == second


def test_chunk_ids_are_derived_from_location_not_content(repo, tmp_path):
    """An edited function keeps its id, so its old vector is replaced."""
    before = _by_qualname(chunk_repo(REPO_ID, repo), "helper")

    path = tmp_path / "app" / "session.py"
    path.write_text(SOURCE.replace("value.strip().lower()", "value.upper()"), encoding="utf-8")
    after = _by_qualname(chunk_repo(REPO_ID, repo), "helper")

    assert after.id == before.id
    assert after.sha256 != before.sha256


def test_overloaded_definitions_get_distinct_ids(tmp_path):
    """A qualname is not unique in a file — `@typing.overload` repeats it.

    On psf/requests this silently collapsed 846 chunks into 826 points, each
    overload overwriting the real implementation.
    """
    source = (
        "from typing import overload\n"
        "\n"
        "@overload\n"
        "def parse(v: int) -> int: ...\n"
        "@overload\n"
        "def parse(v: str) -> str: ...\n"
        "def parse(v):\n"
        "    return v\n"
    )
    (tmp_path / "over.py").write_text(source, encoding="utf-8")

    definitions = tuple(
        _symbol("parse", "parse", SymbolKind.FUNCTION, "over", start, end)
        for start, end in ((3, 4), (5, 6), (7, 8))
    )
    parsed = ParsedRepo(
        inventory=RepoInventory(
            name="x", root=str(tmp_path), source_kind=SourceKind.LOCAL,
            files=(FileInfo("over.py", Language.PYTHON, ".py", 1, 8, "x", "over"),),
        ),
        files=(ParsedFile(path="over.py", module="over", symbols=definitions),),
    )

    chunks = [c for c in chunk_repo(REPO_ID, parsed).chunks if c.qualname == "parse"]
    assert len(chunks) == 3
    assert len({c.id for c in chunks}) == 3, "overloads collided on one id"


def test_two_repositories_produce_different_ids_for_the_same_code(repo):
    a = _by_qualname(chunk_repo(REPO_ID, repo), "helper")
    b = _by_qualname(chunk_repo("22222222-2222-2222-2222-222222222222", repo), "helper")
    assert a.id != b.id


def test_symbol_chunks_carry_the_neo4j_node_key(repo):
    """This field is what makes Thursday's hybrid retrieval possible."""
    from app.services.graph_keys import symbol_key

    chunk = _by_qualname(chunk_repo(REPO_ID, repo), "Session.get")
    assert chunk.symbol_key == symbol_key(
        REPO_ID, "app.session", "Session.get", "app/session.py"
    )


def test_module_chunks_have_no_symbol_key(repo):
    """There is no symbol, so there is no graph node to point at."""
    module_chunk = next(c for c in chunk_repo(REPO_ID, repo).chunks if c.kind == "module")
    assert module_chunk.symbol_key is None


# --- oversized symbols -------------------------------------------------------


def test_an_oversized_function_splits_into_overlapping_parts(tmp_path):
    """A 2,000-line function exceeds any embedding model's input limit."""
    body = "\n".join(f"    x{i} = compute({i})" for i in range(4000))
    source = f"def enormous():\n{body}\n"
    (tmp_path / "big.py").write_text(source, encoding="utf-8")

    line_count = len(source.splitlines())
    parsed = ParsedRepo(
        inventory=RepoInventory(
            name="x", root=str(tmp_path), source_kind=SourceKind.LOCAL,
            files=(
                FileInfo("big.py", Language.PYTHON, ".py", len(source), line_count, "x", "big"),
            ),
        ),
        files=(
            ParsedFile(
                path="big.py",
                module="big",
                symbols=(
                    _symbol("enormous", "enormous", SymbolKind.FUNCTION, "big", 1, line_count),
                ),
            ),
        ),
    )

    parts = [c for c in chunk_repo(REPO_ID, parsed).chunks if c.qualname == "enormous"]
    assert len(parts) > 1
    assert all(p.is_split for p in parts)
    assert [p.part for p in parts] == list(range(len(parts)))
    assert all(p.part_count == len(parts) for p in parts)
    # Distinct ids, or the parts would overwrite each other in Qdrant.
    assert len({p.id for p in parts}) == len(parts)
    # Every part stays identifiable as the same symbol.
    assert all("enormous" in p.text for p in parts)


def test_split_parts_overlap_so_nothing_falls_between_them(tmp_path):
    body = "\n".join(f"    line_{i}()" for i in range(3000))
    source = f"def big():\n{body}\n"
    (tmp_path / "b.py").write_text(source, encoding="utf-8")
    line_count = len(source.splitlines())

    parsed = ParsedRepo(
        inventory=RepoInventory(
            name="x", root=str(tmp_path), source_kind=SourceKind.LOCAL,
            files=(FileInfo("b.py", Language.PYTHON, ".py", 1, line_count, "x", "b"),),
        ),
        files=(
            ParsedFile(
                path="b.py", module="b",
                symbols=(_symbol("big", "big", SymbolKind.FUNCTION, "b", 1, line_count),),
            ),
        ),
    )
    parts = [c for c in chunk_repo(REPO_ID, parsed).chunks if c.qualname == "big"]
    for earlier, later in zip(parts, parts[1:], strict=False):
        assert later.line_start <= earlier.line_end, "gap between parts"


def test_ordinary_symbols_are_not_split(repo):
    for chunk in chunk_repo(REPO_ID, repo).chunks:
        assert not chunk.is_split
        assert len(chunk.text) <= MAX_CHUNK_CHARS + 500


# --- robustness --------------------------------------------------------------


def test_a_file_that_cannot_be_read_is_counted_not_fatal(tmp_path):
    parsed = ParsedRepo(
        inventory=RepoInventory(
            name="x", root=str(tmp_path), source_kind=SourceKind.LOCAL,
            files=(FileInfo("gone.py", Language.PYTHON, ".py", 1, 1, "x", "gone"),),
        ),
        files=(ParsedFile(path="gone.py", module="gone"),),
    )
    result = chunk_repo(REPO_ID, parsed)
    assert result.files_unreadable == 1
    assert len(result) == 0


def test_chunking_the_parser_package_produces_sane_counts(tmp_path):
    """A real repository, end to end, with no fixtures involved."""
    root = Path(__file__).resolve().parents[2] / "parser" / "parser"
    parsed = analyze_repo(str(root), workspace_dir=str(tmp_path), force=True)

    result = chunk_repo(REPO_ID, parsed)
    kinds = result.by_kind()

    assert result.files_read == parsed.inventory.file_count
    assert kinds.get("function", 0) > 20
    assert kinds.get("class", 0) > 5
    assert kinds.get("module", 0) > 0
    # Every chunk is embeddable and identifiable.
    assert all(c.text.strip() for c in result.chunks)
    assert len({c.id for c in result.chunks}) == len(result.chunks)
