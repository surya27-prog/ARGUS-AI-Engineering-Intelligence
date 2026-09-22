"""Tech-debt detector tests.

The calibration helpers are pure and tested directly. The Postgres detectors run
against rows built here rather than a parsed repository, so a threshold test can
state the distribution it is asserting about. The graph detectors need a real
graph, so they get a two-file import cycle written through the normal writer.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models import Repository, SourceFile, Symbol
from app.services.debt import (
    DebtKind,
    Finding,
    Severity,
    detect_circular_imports,
    detect_complexity,
    detect_dead_code,
    detect_god_files,
    detect_missing_docstrings,
    percentile,
    run_detectors,
    severity_from_ratio,
)
from app.services.debt.graph_detectors import _dead_code_verdict
from app.services.graph_writer import delete_repo_graph, write_parsed_repo
from parser.models import (
    FileInfo,
    ImportRef,
    Language,
    ParsedFile,
    ParsedRepo,
    RepoInventory,
    SourceKind,
    SymbolKind,
)
from parser.models import Symbol as ParsedSymbol

# --- calibration helpers -----------------------------------------------------


def test_percentile_returns_an_observed_value():
    """Interpolating would invent a threshold no symbol actually has."""
    values = [1.0, 2.0, 3.0, 100.0]
    assert percentile(values, 0.90) in values
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 1.0) == 100.0


def test_percentile_of_nothing_is_zero():
    assert percentile([], 0.9) == 0.0


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (0.5, Severity.INFO),
        (1.0, Severity.LOW),
        (1.9, Severity.LOW),
        (2.0, Severity.MEDIUM),
        (3.0, Severity.HIGH),
        (12.0, Severity.HIGH),
    ],
)
def test_severity_tracks_how_far_past_the_threshold(ratio: float, expected: Severity):
    assert severity_from_ratio(ratio) == expected


def test_findings_sort_worst_first_then_most_confident():
    findings = [
        Finding(DebtKind.DEAD_CODE, Severity.LOW, "certain-low", confidence=1.0),
        Finding(DebtKind.COMPLEXITY, Severity.HIGH, "high"),
        Finding(DebtKind.DEAD_CODE, Severity.LOW, "shaky-low", confidence=0.9),
        Finding(DebtKind.GOD_FILE, Severity.MEDIUM, "medium"),
    ]
    # Severity dominates; within a severity the more confident finding leads.
    assert [f.subject for f in sorted(findings, key=Finding.sort_key)] == [
        "high",
        "medium",
        "certain-low",
        "shaky-low",
    ]


# --- Postgres detectors ------------------------------------------------------


def _file(repo: Repository, path: str, *, lines: int = 10) -> SourceFile:
    return SourceFile(
        repository_id=repo.id, path=path, module=path[:-3].replace("/", "."), line_count=lines
    )


def _symbol(
    repo: Repository,
    qualname: str,
    *,
    kind: str = "function",
    complexity: int = 1,
    lines: int = 5,
    docstring: str | None = "Documented.",
    module: str = "app.mod",
) -> Symbol:
    return Symbol(
        repository_id=repo.id,
        name=qualname.split(".")[-1],
        qualname=qualname,
        kind=kind,
        module=module,
        line_start=1,
        line_end=lines,
        complexity=complexity,
        docstring=docstring,
    )


@pytest.fixture
def flat_repo(db: Session, repository: Repository) -> Repository:
    """One file, ten trivial functions and one monster — a clear distribution."""
    source = _file(repository, "app/mod.py", lines=200)
    source.symbols = [
        *(_symbol(repository, f"trivial_{i}", complexity=1) for i in range(10)),
        _symbol(repository, "monster", complexity=40, lines=200),
    ]
    db.add(source)
    db.commit()
    return repository


def test_complexity_reports_the_outlier_only(db: Session, flat_repo: Repository):
    findings = detect_complexity(db, flat_repo.id)

    assert [f.subject for f in findings] == ["monster"]
    assert findings[0].severity == Severity.HIGH
    assert findings[0].metrics["complexity"] == 40


def test_complexity_respects_the_absolute_floor(db: Session, repository: Repository):
    """In a repo of uniformly simple functions, the least simple is not debt."""
    source = _file(repository, "app/mod.py")
    source.symbols = [_symbol(repository, f"f{i}", complexity=i + 1) for i in range(6)]
    db.add(source)
    db.commit()

    # p90 here is about 6, well under the floor of 10.
    assert detect_complexity(db, repository.id) == []


def test_complexity_reports_nothing_for_an_empty_repository(
    db: Session, repository: Repository
):
    assert detect_complexity(db, repository.id) == []


def test_complexity_only_looks_at_callables(db: Session, repository: Repository):
    """A class's complexity covers its class body, which is not a debt signal."""
    source = _file(repository, "app/mod.py")
    source.symbols = [
        _symbol(repository, "Big", kind="class", complexity=99),
        *(_symbol(repository, f"f{i}", complexity=1) for i in range(10)),
    ]
    db.add(source)
    db.commit()

    assert detect_complexity(db, repository.id) == []


def test_god_file_needs_both_length_and_symbol_count(db: Session, repository: Repository):
    """A long file of constants is not a design problem."""
    long_but_empty = _file(repository, "app/constants.py", lines=5000)
    normal = [_file(repository, f"app/m{i}.py", lines=20) for i in range(9)]
    for source in (long_but_empty, *normal):
        source.symbols = [_symbol(repository, f"{source.path}.f")]
    db.add_all([long_but_empty, *normal])
    db.commit()

    assert detect_god_files(db, repository.id) == []


def test_god_file_is_reported_when_both_hold(db: Session, repository: Repository):
    fat = _file(repository, "app/everything.py", lines=4000)
    fat.symbols = [_symbol(repository, f"f{i}") for i in range(60)]
    small = [_file(repository, f"app/m{i}.py", lines=20) for i in range(9)]
    for source in small:
        source.symbols = [_symbol(repository, f"{source.path}.f")]
    db.add_all([fat, *small])
    db.commit()

    findings = detect_god_files(db, repository.id)

    assert [f.subject for f in findings] == ["app/everything.py"]
    assert findings[0].metrics["lines"] == 4000
    assert findings[0].metrics["symbols"] == 60


def test_missing_docstrings_skips_private_and_dunder(db: Session, repository: Repository):
    source = _file(repository, "app/mod.py")
    source.symbols = [
        _symbol(repository, "public_thing", docstring=None),
        _symbol(repository, "_private", docstring=None),
        _symbol(repository, "Klass._helper", docstring=None),
        _symbol(repository, "_Private.public_method", docstring=None),
        _symbol(repository, "__eq__", kind="method", docstring=None),
        _symbol(repository, "__init__", kind="method", docstring=None),
        _symbol(repository, "documented", docstring="Yes."),
    ]
    db.add(source)
    db.commit()

    assert [f.subject for f in detect_missing_docstrings(db, repository.id)] == ["public_thing"]


def test_missing_docstrings_are_ranked_by_how_much_they_cost(
    db: Session, repository: Repository
):
    """A one-liner without a docstring is info; a branching 40-liner is low."""
    source = _file(repository, "app/mod.py")
    source.symbols = [
        _symbol(repository, "tiny", docstring=None, lines=3, complexity=1),
        _symbol(repository, "substantial", docstring=None, lines=40, complexity=8),
    ]
    db.add(source)
    db.commit()

    by_subject = {f.subject: f.severity for f in detect_missing_docstrings(db, repository.id)}
    assert by_subject == {"tiny": Severity.INFO, "substantial": Severity.LOW}


def test_findings_carry_a_graph_key(db: Session, flat_repo: Repository):
    """So a finding can jump to the graph and its blast radius."""
    finding = detect_complexity(db, flat_repo.id)[0]
    assert finding.key is not None
    assert finding.key.startswith("sym:")
    assert finding.key.endswith(":monster")


# --- graph detectors ---------------------------------------------------------

A_PATH = "app/a.py"
B_PATH = "app/b.py"
C_PATH = "app/c.py"


def _parsed_symbol(name: str, path: str, module: str, kind=SymbolKind.FUNCTION) -> ParsedSymbol:
    return ParsedSymbol(
        name=name,
        qualname=name,
        kind=kind,
        file_path=path,
        line_start=1,
        line_end=6,
        module=module,
    )


def _cyclic_repo() -> ParsedRepo:
    """`a` and `b` import each other; `c` imports `a` and is imported by nobody."""
    files = (
        ParsedFile(
            path=A_PATH,
            module="app.a",
            symbols=(_parsed_symbol("alpha", A_PATH, "app.a"),),
            imports=(ImportRef(module="app.b", name="beta", line=1, is_from=True),),
        ),
        ParsedFile(
            path=B_PATH,
            module="app.b",
            symbols=(_parsed_symbol("beta", B_PATH, "app.b"),),
            imports=(ImportRef(module="app.a", name="alpha", line=1, is_from=True),),
        ),
        ParsedFile(
            path=C_PATH,
            module="app.c",
            symbols=(
                _parsed_symbol("gamma", C_PATH, "app.c"),
                _parsed_symbol("_hidden", C_PATH, "app.c"),
            ),
            imports=(ImportRef(module="app.a", name="alpha", line=1, is_from=True),),
        ),
    )
    inventory = RepoInventory(
        name="cyclic",
        root="/tmp/cyclic",
        source_kind=SourceKind.LOCAL,
        files=tuple(
            FileInfo(
                path=f.path,
                language=Language.PYTHON,
                extension=".py",
                size_bytes=1,
                line_count=6,
                sha256="x",
                module=f.module,
            )
            for f in files
        ),
    )
    return ParsedRepo(inventory=inventory, files=files)


@pytest.fixture
def cyclic(repository: Repository):
    write_parsed_repo(repository.id, _cyclic_repo())
    try:
        yield repository
    finally:
        delete_repo_graph(repository.id)


def test_circular_imports_finds_the_cycle(cyclic: Repository):
    findings = detect_circular_imports(cyclic.id)

    assert len(findings) == 1, [f.subject for f in findings]
    finding = findings[0]
    assert finding.severity == Severity.HIGH
    assert set(finding.metrics["files"]) == {A_PATH, B_PATH}
    assert finding.metrics["length"] == 2


def test_a_cycle_is_reported_once_not_once_per_member(cyclic: Repository):
    """Two files in a loop is one finding, not two rotations of the same one."""
    subjects = [f.subject for f in detect_circular_imports(cyclic.id)]
    assert len(subjects) == len(set(subjects)) == 1


def _nested_cycle_repo() -> ParsedRepo:
    """`a <-> b`, plus a longer `a -> b -> c -> a` loop built on top of it."""
    files = (
        ParsedFile(
            path=A_PATH,
            module="app.a",
            symbols=(_parsed_symbol("alpha", A_PATH, "app.a"),),
            imports=(ImportRef(module="app.b", name="beta", line=1, is_from=True),),
        ),
        ParsedFile(
            path=B_PATH,
            module="app.b",
            symbols=(_parsed_symbol("beta", B_PATH, "app.b"),),
            imports=(
                ImportRef(module="app.a", name="alpha", line=1, is_from=True),
                ImportRef(module="app.c", name="gamma", line=2, is_from=True),
            ),
        ),
        ParsedFile(
            path=C_PATH,
            module="app.c",
            symbols=(_parsed_symbol("gamma", C_PATH, "app.c"),),
            imports=(ImportRef(module="app.a", name="alpha", line=1, is_from=True),),
        ),
    )
    inventory = RepoInventory(
        name="nested",
        root="/tmp/nested",
        source_kind=SourceKind.LOCAL,
        files=tuple(
            FileInfo(
                path=f.path,
                language=Language.PYTHON,
                extension=".py",
                size_bytes=1,
                line_count=6,
                sha256="x",
                module=f.module,
            )
            for f in files
        ),
    )
    return ParsedRepo(inventory=inventory, files=files)


@pytest.fixture
def nested_cycles(repository: Repository):
    write_parsed_repo(repository.id, _nested_cycle_repo())
    try:
        yield repository
    finally:
        delete_repo_graph(repository.id)


def test_only_minimal_cycles_are_reported(nested_cycles: Repository):
    """A long loop containing a shorter one is a consequence of it.

    On `requests` this filter is the difference between 9 findings and 200:
    breaking the two-file cycle breaks every longer loop built over it.
    """
    findings = detect_circular_imports(nested_cycles.id)

    assert len(findings) == 1
    assert set(findings[0].metrics["files"]) == {A_PATH, B_PATH}
    assert findings[0].metrics["length"] == 2


def test_dead_code_finds_uncalled_symbols(db: Session, cyclic: Repository):
    """Nothing in the fixture calls anything, so every symbol is a candidate."""
    findings = detect_dead_code(db, cyclic.id)

    assert {f.subject for f in findings} >= {"gamma", "_hidden"}
    assert all(f.confidence <= 0.75 for f in findings)


def test_dead_code_never_claims_certainty(db: Session, cyclic: Repository):
    """A module-scope call creates no CALLS edge, so nothing here can be certain."""
    findings = detect_dead_code(db, cyclic.id)

    assert findings
    assert all(f.severity != Severity.HIGH for f in findings)
    private = next(f for f in findings if f.subject == "_hidden")
    assert "module scope" in private.why


@pytest.mark.parametrize(
    ("record", "reported"),
    [
        ({"name": "__enter__", "kind": "method"}, False),
        ({"name": "main", "kind": "function"}, False),
        ({"name": "helper", "kind": "function", "path": "tests/test_x.py"}, False),
        ({"name": "helper", "kind": "function", "path": "app/tests/util.py"}, False),
        ({"name": "index", "kind": "function", "decorators": ["app.get('/')"]}, False),
        ({"name": "value", "kind": "method", "decorators": ["property"]}, False),
        ({"name": "fix", "kind": "function", "decorators": ["pytest.fixture"]}, False),
        ({"name": "helper", "kind": "function", "path": "app/real.py"}, True),
        ({"name": "_helper", "kind": "function", "path": "app/real.py"}, True),
        ({"name": "thing", "kind": "function", "decorators": ["lru_cache"]}, True),
    ],
)
def test_dead_code_suppresses_the_known_false_positives(record: dict, reported: bool):
    """Routes, fixtures, properties, entry points and tests are called by
    machinery ARGUS cannot see."""
    full = {"line_start": 1, "line_end": 5, "path": None, "decorators": [], **record}
    assert (_dead_code_verdict(full) is not None) == reported


# --- the runner --------------------------------------------------------------


def test_runner_returns_a_ranked_report(db: Session, cyclic: Repository):
    report = run_detectors(db, cyclic.id)

    assert report.total > 0
    assert set(report.ran) == {str(k) for k in DebtKind}
    assert not report.failed
    keys = [f.sort_key() for f in report.findings]
    assert keys == sorted(keys)


def test_a_failing_detector_is_named_not_swallowed(
    db: Session, repository: Repository, monkeypatch: pytest.MonkeyPatch
):
    """A report missing its cycles looks identical to a repo with none."""
    from app.services.debt import runner

    def explode(*args, **kwargs):
        raise RuntimeError("neo4j is down")

    monkeypatch.setattr(runner, "detect_circular_imports", explode)
    report = run_detectors(db, repository.id)

    assert "circular_import" in report.failed
    assert "neo4j is down" in report.failed["circular_import"]
    assert "circular_import" not in report.ran
    # The Postgres detectors still ran.
    assert "complexity" in report.ran


def test_runner_can_be_asked_for_one_detector(db: Session, flat_repo: Repository):
    report = run_detectors(db, flat_repo.id, kinds=(DebtKind.COMPLEXITY,))

    assert report.ran == ["complexity"]
    assert {str(f.kind) for f in report.findings} == {"complexity"}


def test_report_summary_counts_match_the_findings(db: Session, flat_repo: Repository):
    report = run_detectors(db, flat_repo.id, kinds=(DebtKind.COMPLEXITY,))

    assert sum(report.by_kind.values()) == report.total
    assert sum(report.by_severity.values()) == report.total
