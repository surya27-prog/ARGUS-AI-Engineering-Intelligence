"""Parser output schema.

This module is the contract between the parser and everything downstream: the
Neo4j graph writer (Week 2) and the chunker/embedder (Week 3) both build on
these shapes. Treat changes here as breaking — the timeline budgets two days
for a schema change made after Week 1, so add fields rather than renaming them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum


class Language(StrEnum):
    """Languages the parser can extract symbols from."""

    PYTHON = "python"


class SourceKind(StrEnum):
    """How a repository arrived on disk."""

    GIT = "git"
    ZIP = "zip"
    LOCAL = "local"


class SkipReason(StrEnum):
    """Why a path was left out of the inventory."""

    IGNORED_DIR = "ignored_dir"
    UNSUPPORTED_EXTENSION = "unsupported_extension"
    TOO_LARGE = "too_large"
    SYMLINK = "symlink"
    UNREADABLE = "unreadable"


class SymbolKind(StrEnum):
    """The node labels the graph writer creates for extracted symbols."""

    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"


class ParameterKind(StrEnum):
    """Mirrors `inspect.Parameter` so call-signature checks stay possible."""

    POSITIONAL_ONLY = "positional_only"
    POSITIONAL_OR_KEYWORD = "positional_or_keyword"
    VAR_POSITIONAL = "var_positional"
    KEYWORD_ONLY = "keyword_only"
    VAR_KEYWORD = "var_keyword"


@dataclass(frozen=True, slots=True)
class FileInfo:
    """One source file in a repository.

    `path` is the file's identity everywhere downstream — POSIX-separated and
    relative to the repo root, so it is stable across machines and OSes.
    """

    path: str
    language: Language
    extension: str
    size_bytes: int
    line_count: int
    sha256: str
    # Dotted import path (`app.core.config`), derived from package layout by
    # walking up through directories that contain `__init__.py`. Week 2's
    # import resolution keys off this; None when the file sits outside any
    # importable package.
    module: str | None = None


@dataclass(frozen=True, slots=True)
class SkippedPath:
    """A path the walker deliberately ignored, kept so parses stay auditable."""

    path: str
    reason: SkipReason


@dataclass(frozen=True, slots=True)
class RepoInventory:
    """The complete Day-4 parse result: what files exist and what was skipped."""

    name: str
    root: str
    source_kind: SourceKind
    files: tuple[FileInfo, ...] = ()
    skipped: tuple[SkippedPath, ...] = ()
    url: str | None = None
    commit_sha: str | None = None
    default_branch: str | None = None

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_lines(self) -> int:
        return sum(f.line_count for f in self.files)

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    def skipped_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.skipped:
            counts[entry.reason] = counts.get(entry.reason, 0) + 1
        return counts

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["file_count"] = self.file_count
        payload["total_lines"] = self.total_lines
        payload["total_bytes"] = self.total_bytes
        return payload

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


@dataclass(frozen=True, slots=True)
class Parameter:
    """One parameter of a function or method."""

    name: str
    kind: ParameterKind = ParameterKind.POSITIONAL_OR_KEYWORD
    annotation: str | None = None
    default: str | None = None

    @property
    def has_default(self) -> bool:
        return self.default is not None


@dataclass(frozen=True, slots=True)
class Symbol:
    """A class, function or method defined in a file.

    `qualname` is the identity used downstream — dotted from the module root
    (`Repository.__repr__`), so `module + "." + qualname` is unique repo-wide.
    Nested functions flatten to `outer.inner` rather than CPython's
    `outer.<locals>.inner`, which reads better as a graph node key.
    """

    name: str
    qualname: str
    kind: SymbolKind
    file_path: str
    line_start: int
    line_end: int
    module: str | None = None
    parent: str | None = None
    docstring: str | None = None
    decorators: tuple[str, ...] = ()
    # Functions and methods only.
    parameters: tuple[Parameter, ...] = ()
    returns: str | None = None
    is_async: bool = False
    # Classes only — the raw text of each base, resolved to real INHERITS edges
    # in Week 2 once every module's symbols are known.
    base_classes: tuple[str, ...] = ()

    @property
    def line_count(self) -> int:
        return self.line_end - self.line_start + 1


@dataclass(frozen=True, slots=True)
class ImportRef:
    """An `import x` or `from x import y` statement, unresolved.

    Resolution to a real file node happens in Week 2; here the parser only
    records what the source literally said, including relative-import depth.
    """

    module: str | None
    name: str | None = None
    alias: str | None = None
    line: int = 0
    level: int = 0
    is_from: bool = False

    @property
    def is_relative(self) -> bool:
        return self.level > 0

    @property
    def target(self) -> str:
        """Dotted text as written, e.g. `os.path`, `.core.config`, `..models`."""
        base = "." * self.level + (self.module or "")
        if not (self.is_from and self.name):
            return base
        separator = "" if not base or base.endswith(".") else "."
        return f"{base}{separator}{self.name}"

    @property
    def bound_name(self) -> str:
        """The name this import introduces into the module namespace."""
        if self.alias:
            return self.alias
        if self.is_from and self.name:
            return self.name
        return (self.module or "").split(".")[0]


@dataclass(frozen=True, slots=True)
class CallRef:
    """A call site, recorded as written.

    Day 4–5 make no attempt to resolve `callee` to a defined symbol — that is
    Week 2's call-graph pass, which needs the whole repo's symbol table and
    records a confidence per edge. Storing the raw dotted text keeps that pass
    possible without re-parsing.
    """

    callee: str
    line: int
    caller: str | None = None
    file_path: str | None = None
    module: str | None = None
    arg_count: int = 0

    @property
    def is_attribute_call(self) -> bool:
        """True for `obj.method()`, which needs type inference to resolve."""
        return "." in self.callee


@dataclass(frozen=True, slots=True)
class ParsedFile:
    """Everything extracted from one source file.

    A file that fails to parse is still returned, with `error` set — a repo with
    one broken file should produce a partial graph, not no graph.
    """

    path: str
    module: str | None = None
    language: Language = Language.PYTHON
    symbols: tuple[Symbol, ...] = ()
    imports: tuple[ImportRef, ...] = ()
    calls: tuple[CallRef, ...] = ()
    docstring: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def classes(self) -> tuple[Symbol, ...]:
        return tuple(s for s in self.symbols if s.kind is SymbolKind.CLASS)

    @property
    def functions(self) -> tuple[Symbol, ...]:
        return tuple(
            s for s in self.symbols if s.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD)
        )


@dataclass(frozen=True, slots=True)
class ParsedRepo:
    """A repository inventory plus the symbols extracted from every file."""

    inventory: RepoInventory
    files: tuple[ParsedFile, ...] = ()

    @property
    def name(self) -> str:
        return self.inventory.name

    @property
    def symbol_count(self) -> int:
        return sum(len(f.symbols) for f in self.files)

    @property
    def import_count(self) -> int:
        return sum(len(f.imports) for f in self.files)

    @property
    def call_count(self) -> int:
        return sum(len(f.calls) for f in self.files)

    @property
    def failed_files(self) -> tuple[ParsedFile, ...]:
        return tuple(f for f in self.files if not f.ok)

    def to_dict(self) -> dict:
        return {
            "inventory": self.inventory.to_dict(),
            "files": [asdict(f) for f in self.files],
            "symbol_count": self.symbol_count,
            "import_count": self.import_count,
            "call_count": self.call_count,
            "failed_file_count": len(self.failed_files),
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)
