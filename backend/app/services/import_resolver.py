"""Turns the parser's literal `ImportRef`s into resolved graph targets.

The parser records imports exactly as written, because resolving one needs the
whole repository's module table and that only exists once every file has been
walked. This module is where that table gets built and applied.

Nothing here talks to Neo4j: resolution is a pure function of the parse result,
so it is tested without a database and `graph_writer` only has to write rows.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from parser import ImportRef, ParsedFile, ParsedRepo

logger = logging.getLogger(__name__)


class ImportResolution(StrEnum):
    """How an import found its target. Stored on the edge, per the schema doc."""

    INTERNAL_MODULE = "internal_module"
    INTERNAL_SYMBOL = "internal_symbol"
    RELATIVE = "relative"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class ResolvedImport:
    """One `IMPORTS` edge, before it becomes Cypher.

    Exactly one of `target_path` (a file in this repo) and `target_module` (an
    external dotted name) is set — which one is implied by `resolution`.
    """

    source_path: str
    resolution: ImportResolution
    line: int
    level: int
    alias: str | None = None
    target_path: str | None = None
    target_module: str | None = None

    @property
    def is_relative(self) -> bool:
        return self.level > 0

    @property
    def is_internal(self) -> bool:
        return self.target_path is not None


def build_module_index(parsed: ParsedRepo) -> dict[str, str]:
    """Dotted module name -> file path, for every importable file in the repo.

    Files outside any package have no module name and so cannot be an import
    target — which is correct, since Python could not import them either. The
    walker derives `module` by walking up through directories holding an
    `__init__.py`, so a namespace package (no `__init__.py`) is invisible here.
    """
    return {f.module: f.path for f in parsed.files if f.module}


def resolve_imports(parsed: ParsedRepo) -> list[ResolvedImport]:
    """Resolve every import in the repo against its own module table."""
    index = build_module_index(parsed)
    resolved: list[ResolvedImport] = []
    # `from x import a, b` is two ImportRefs on one line. When both land on the
    # same target they are one dependency, and the graph MERGEs on (pair, line)
    # anyway, so collapse them here to keep the counts predictable.
    seen: set[tuple[str, str | None, str | None, int]] = set()

    for parsed_file in parsed.files:
        for ref in parsed_file.imports:
            entry = _resolve_one(ref, parsed_file, index)
            fingerprint = (entry.source_path, entry.target_path, entry.target_module, entry.line)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            resolved.append(entry)

    counts = Counter(str(entry.resolution) for entry in resolved)
    logger.info("Resolved %d imports: %s", len(resolved), dict(counts))
    return resolved


def _resolve_one(
    ref: ImportRef, parsed_file: ParsedFile, index: dict[str, str]
) -> ResolvedImport:
    if ref.is_relative:
        return _resolve_relative(ref, parsed_file, index)
    return _resolve_absolute(ref, parsed_file, index)


def _resolve_absolute(
    ref: ImportRef, parsed_file: ParsedFile, index: dict[str, str]
) -> ResolvedImport:
    """`import x.y` and `from x.y import z`."""
    base = ref.module or ""
    if not base:
        return _external(ref, parsed_file, ref.target or "")

    # `from x.y import z` where z is itself a module is a module import, not a
    # symbol import, so the deeper name is tried first.
    if ref.is_from and ref.name and (path := index.get(f"{base}.{ref.name}")):
        return _internal(ref, parsed_file, path, ImportResolution.INTERNAL_MODULE)

    if path := index.get(base):
        resolution = (
            ImportResolution.INTERNAL_SYMBOL if ref.is_from else ImportResolution.INTERNAL_MODULE
        )
        return _internal(ref, parsed_file, path, resolution)

    return _external(ref, parsed_file, base)


def _resolve_relative(
    ref: ImportRef, parsed_file: ParsedFile, index: dict[str, str]
) -> ResolvedImport:
    """`from .config import x`, `from ..core import y`.

    Resolved against the *importing file's* package, not the repo root.
    """
    base = _relative_base(parsed_file, ref.level)
    if base is None:
        # More dots than package depth, or a file outside any package. Python
        # would fail at runtime; recording it as external keeps the fact rather
        # than dropping the import.
        return _external(ref, parsed_file, ref.target)

    target = f"{base}.{ref.module}" if ref.module else base
    target = target.lstrip(".")
    if not target:
        return _external(ref, parsed_file, ref.target)

    # `from . import config` names a sibling module in `ref.name`, so the
    # deeper candidate has to be tried the same way as for absolute imports.
    if ref.is_from and ref.name and (path := index.get(f"{target}.{ref.name}")):
        return _internal(ref, parsed_file, path, ImportResolution.RELATIVE)

    if path := index.get(target):
        return _internal(ref, parsed_file, path, ImportResolution.RELATIVE)

    return _external(ref, parsed_file, ref.target)


def _relative_base(parsed_file: ParsedFile, level: int) -> str | None:
    """The package `level` dots up from `parsed_file`, or None if that is too far.

    `from ..core.config import x` inside `app/api/health.py` walks up two levels
    from `app.api` to `app`. A package's own `__init__.py` *is* the package, so
    one fewer component comes off it.
    """
    module = parsed_file.module
    if not module:
        return None

    if parsed_file.path.endswith("__init__.py"):
        parts = module.split(".")
    else:
        parts = module.split(".")[:-1]

    # level 1 is the current package, so only the levels beyond it strip parts.
    climb = level - 1
    if climb > len(parts):
        return None
    remaining = parts[: len(parts) - climb]
    return ".".join(remaining)


def _internal(
    ref: ImportRef, parsed_file: ParsedFile, path: str, resolution: ImportResolution
) -> ResolvedImport:
    return ResolvedImport(
        source_path=parsed_file.path,
        resolution=resolution,
        line=ref.line,
        level=ref.level,
        alias=ref.alias,
        target_path=path,
    )


def _external(ref: ImportRef, parsed_file: ParsedFile, dotted: str) -> ResolvedImport:
    return ResolvedImport(
        source_path=parsed_file.path,
        resolution=ImportResolution.EXTERNAL,
        line=ref.line,
        level=ref.level,
        alias=ref.alias,
        target_module=dotted or ref.target,
    )
