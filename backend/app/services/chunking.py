"""Turns a parsed repository into the units that get embedded.

**Chunk by symbol, not by token window.** A fixed 512-token window slices
through the middle of functions: half a body in one vector, half in the next,
and a docstring separated from the code it describes. Neither piece answers the
question the user asked, and neither can be cited as "this function". A symbol
is what a developer actually asks about, so a symbol is the unit.

Three consequences of that choice, all handled below:

- **Symbols nest.** A class contains its methods, so embedding both the whole
  class and each method stores the same code twice — inflating the index and
  returning the same answer at two ranks. Classes are chunked as an *outline*
  instead: signature, docstring, and the signatures of their methods. Method
  bodies live in their own chunks.
- **Not all code is in a symbol.** Imports, constants and module-level setup
  answer real questions ("what does this module depend on") and belong to no
  function. Those become a module chunk.
- **Some symbols are enormous.** A 2,000-line function exceeds any embedding
  model's input limit, so oversized bodies split into overlapping parts that
  keep their symbol identity.

Nothing here calls an embedding provider or a database — chunking is a pure
function of the parse result plus the files on disk, which is what lets the
strategy be tested exhaustively without a key.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from parser import ParsedFile, ParsedRepo, Symbol, SymbolKind

logger = logging.getLogger(__name__)

# Roughly four characters per token for code. Deliberately an estimate: the
# real count is provider-specific and costs an API call, and this is only used
# to decide when to split, where being approximately right is enough.
CHARS_PER_TOKEN = 4

# text-embedding-3-* accepts 8191 tokens. Splitting well below that leaves room
# for the header and keeps a chunk small enough to be a precise retrieval hit —
# an 8k-token chunk matches everything and pinpoints nothing.
MAX_CHUNK_TOKENS = 1_000
MAX_CHUNK_CHARS = MAX_CHUNK_TOKENS * CHARS_PER_TOKEN

# Overlap between the parts of a split symbol, so a construct straddling the
# boundary survives in at least one part intact.
OVERLAP_LINES = 8

# A chunk shorter than this is a stray brace or a lone `pass` — noise that
# dilutes the index without ever being a useful answer.
MIN_CHUNK_CHARS = 24

_UUID_NAMESPACE = uuid5(NAMESPACE_URL, "argus.chunk")


@dataclass(frozen=True, slots=True)
class CodeChunk:
    """One embeddable unit of a repository."""

    repo_id: str
    path: str
    kind: str
    text: str
    line_start: int
    line_end: int
    module: str | None = None
    qualname: str | None = None
    name: str | None = None
    # The Neo4j node key for the symbol this chunk came from. This single field
    # is what makes Thursday's hybrid retrieval possible: a vector hit lands
    # here, and the key walks straight into the graph to pull callers and
    # callees. Without it the two stores would be unjoinable.
    symbol_key: str | None = None
    part: int = 0
    part_count: int = 1

    @property
    def id(self) -> UUID:
        """A stable point id, so re-embedding overwrites instead of duplicating.

        Derived from location, not content: editing a function's body leaves
        its id alone and replaces its vector. A content hash would orphan the
        old vector on every edit.

        `line_start` is part of the key, and it has to be. A qualname is *not*
        unique within a file — `@typing.overload` gives one function several
        definitions under one name, and on `psf/requests` that silently
        collapsed 846 chunks into 826 points, each overload overwriting the
        real implementation. Keying on position too is safe precisely because
        the writer stamps and sweeps: code that moves gets a new id, and the
        point at the old one is deleted at the end of the same run.
        """
        return uuid5(
            _UUID_NAMESPACE,
            f"{self.repo_id}|{self.path}|{self.qualname or ''}|{self.line_start}|{self.part}",
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def estimated_tokens(self) -> int:
        return max(1, len(self.text) // CHARS_PER_TOKEN)

    @property
    def is_split(self) -> bool:
        return self.part_count > 1


@dataclass(slots=True)
class ChunkingResult:
    chunks: list[CodeChunk] = field(default_factory=list)
    files_read: int = 0
    files_unreadable: int = 0
    skipped_empty: int = 0

    def by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for chunk in self.chunks:
            counts[chunk.kind] = counts.get(chunk.kind, 0) + 1
        return counts

    def __len__(self) -> int:
        return len(self.chunks)


def chunk_repo(repo_id: str, parsed: ParsedRepo) -> ChunkingResult:
    """Chunk every file in a parse result.

    Reads source from `parsed.inventory.root`, so this runs while the repository
    is still staged in the workspace — the parse pipeline chunks in the same
    pass rather than re-staging later.
    """
    root = Path(parsed.inventory.root)
    result = ChunkingResult()

    for parsed_file in parsed.files:
        try:
            lines = (root / parsed_file.path).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            result.files_unreadable += 1
            logger.warning("Could not read %s for chunking", parsed_file.path)
            continue

        result.files_read += 1
        for chunk in _chunk_file(repo_id, parsed_file, lines):
            if len(chunk.text.strip()) < MIN_CHUNK_CHARS:
                result.skipped_empty += 1
                continue
            result.chunks.append(chunk)

    logger.info(
        "Chunked %d files into %d chunks %s (%d unreadable, %d too small)",
        result.files_read,
        len(result.chunks),
        result.by_kind(),
        result.files_unreadable,
        result.skipped_empty,
    )
    return result


def _chunk_file(
    repo_id: str, parsed_file: ParsedFile, lines: list[str]
) -> Iterator[CodeChunk]:
    symbols = parsed_file.symbols
    covered: set[int] = set()

    for symbol in symbols:
        if symbol.kind is SymbolKind.CLASS:
            yield from _class_outline(repo_id, parsed_file, symbol, symbols, lines)
            # Only the outline's lines are claimed — the method bodies inside
            # this class are claimed by their own chunks.
            covered.update(range(symbol.line_start, _outline_end(symbol, symbols) + 1))
        else:
            yield from _symbol_chunks(repo_id, parsed_file, symbol, lines)
            covered.update(range(symbol.line_start, symbol.line_end + 1))

    module_chunk = _module_chunk(repo_id, parsed_file, lines, covered)
    if module_chunk is not None:
        yield module_chunk


def _header(parsed_file: ParsedFile, extra: str = "") -> str:
    """The context line prepended to every chunk.

    The body alone is ambiguous — `def get(self, url)` appears in a hundred
    repositories. The path and module are what make the vector specific to
    *this* codebase, and they are also what the user reads in the citation.
    """
    parts = [f"File: {parsed_file.path}"]
    if parsed_file.module:
        parts.append(f"Module: {parsed_file.module}")
    if extra:
        parts.append(extra)
    return "\n".join(parts)


def _slice(lines: list[str], start: int, end: int) -> str:
    """Lines `start..end` inclusive, 1-indexed as the parser reports them."""
    return "\n".join(lines[max(0, start - 1) : end])


def _symbol_chunks(
    repo_id: str, parsed_file: ParsedFile, symbol: Symbol, lines: list[str]
) -> Iterator[CodeChunk]:
    body = _slice(lines, symbol.line_start, symbol.line_end)
    header = _header(parsed_file, f"{symbol.kind}: {symbol.qualname}")

    if len(body) <= MAX_CHUNK_CHARS:
        yield _build(repo_id, parsed_file, symbol, f"{header}\n\n{body}", symbol.line_start,
                     symbol.line_end)
        return

    # Oversized: split into overlapping line windows, each carrying the same
    # header so every part stays identifiable as the same symbol.
    windows = list(_windows(lines, symbol.line_start, symbol.line_end))
    for index, (start, end) in enumerate(windows):
        part_header = f"{header} (part {index + 1} of {len(windows)})"
        text = f"{part_header}\n\n{_slice(lines, start, end)}"
        yield _build(
            repo_id, parsed_file, symbol, text, start, end,
            part=index, part_count=len(windows),
        )


def _windows(lines: list[str], start: int, end: int) -> Iterator[tuple[int, int]]:
    """Overlapping line ranges covering `start..end`."""
    # Average line length across the symbol, so a file of long lines produces
    # fewer lines per window rather than blowing the character budget.
    span = _slice(lines, start, end)
    line_count = end - start + 1
    avg = max(1, len(span) // max(1, line_count))
    per_window = max(OVERLAP_LINES * 2, MAX_CHUNK_CHARS // avg)

    cursor = start
    while cursor <= end:
        stop = min(end, cursor + per_window - 1)
        yield cursor, stop
        if stop >= end:
            return
        cursor = stop - OVERLAP_LINES + 1


def _outline_end(symbol: Symbol, symbols: tuple[Symbol, ...]) -> int:
    """Where a class outline stops: just before its first member."""
    members = [
        s.line_start
        for s in symbols
        if s.parent == symbol.qualname and s.line_start > symbol.line_start
    ]
    return min(members) - 1 if members else symbol.line_end


def _class_outline(
    repo_id: str,
    parsed_file: ParsedFile,
    symbol: Symbol,
    symbols: tuple[Symbol, ...],
    lines: list[str],
) -> Iterator[CodeChunk]:
    """A class as its signature, docstring, and the signatures of its methods.

    Not the full body: every method is embedded separately, so including their
    bodies here would store the same code twice and return the class and the
    method as two hits for one answer. The signature list keeps the chunk
    answerable for "what can this class do", which the method chunks cannot
    answer individually.
    """
    end = _outline_end(symbol, symbols)
    head = _slice(lines, symbol.line_start, end)

    members = [s for s in symbols if s.parent == symbol.qualname]
    if members:
        signatures = "\n".join(
            f"    {'async ' if m.is_async else ''}def {m.name}("
            f"{', '.join(p.name for p in m.parameters)})"
            for m in members
        )
        head = f"{head}\n\n    # methods:\n{signatures}"

    bases = f"({', '.join(symbol.base_classes)})" if symbol.base_classes else ""
    header = _header(parsed_file, f"class: {symbol.qualname}{bases}")
    yield _build(repo_id, parsed_file, symbol, f"{header}\n\n{head}", symbol.line_start, end)


def _module_chunk(
    repo_id: str, parsed_file: ParsedFile, lines: list[str], covered: set[int]
) -> CodeChunk | None:
    """Everything not inside a symbol: imports, constants, module setup.

    A file whose every line belongs to a symbol produces nothing here — the
    module chunk exists for the code between the definitions, not as a second
    copy of them.
    """
    remaining = [
        (number, text)
        for number, text in enumerate(lines, start=1)
        if number not in covered and text.strip()
    ]
    if not remaining:
        return None

    body = "\n".join(text for _, text in remaining)
    header = _header(parsed_file, "module-level code")
    return CodeChunk(
        repo_id=repo_id,
        path=parsed_file.path,
        kind="module",
        text=f"{header}\n\n{body[:MAX_CHUNK_CHARS]}",
        line_start=remaining[0][0],
        line_end=remaining[-1][0],
        module=parsed_file.module,
        qualname=None,
        name=parsed_file.path.rsplit("/", 1)[-1],
    )


def _build(
    repo_id: str,
    parsed_file: ParsedFile,
    symbol: Symbol,
    text: str,
    line_start: int,
    line_end: int,
    *,
    part: int = 0,
    part_count: int = 1,
) -> CodeChunk:
    # Imported here rather than at module scope to keep chunking free of any
    # dependency on the graph layer — it only needs the key format.
    from app.services.graph_keys import symbol_key

    return CodeChunk(
        repo_id=repo_id,
        path=parsed_file.path,
        kind=str(symbol.kind),
        text=text,
        line_start=line_start,
        line_end=line_end,
        module=symbol.module,
        qualname=symbol.qualname,
        name=symbol.name,
        symbol_key=symbol_key(repo_id, symbol.module, symbol.qualname, parsed_file.path),
        part=part,
        part_count=part_count,
    )
