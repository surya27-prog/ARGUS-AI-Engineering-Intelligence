"""ARGUS repository parser.

Week 1 scope: stage a repository on disk and inventory its source files. The
public surface is `parse_repo()` plus the schema in `parser.models`.
"""

from __future__ import annotations

from pathlib import Path

from parser.extractor import extract_file, extract_source
from parser.ingest import (
    DEFAULT_MAX_SIZE_MB,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_WORKSPACE,
    IngestedRepo,
    IngestError,
    ingest,
)
from parser.models import (
    CallRef,
    FileInfo,
    ImportRef,
    Language,
    Parameter,
    ParameterKind,
    ParsedFile,
    ParsedRepo,
    RepoInventory,
    SkippedPath,
    SkipReason,
    SourceKind,
    Symbol,
    SymbolKind,
)
from parser.walker import (
    IGNORED_DIRS,
    build_inventory,
    extract_symbols,
    inventory_for_path,
    walk_files,
)

__all__ = [
    "DEFAULT_MAX_SIZE_MB",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_WORKSPACE",
    "IGNORED_DIRS",
    "CallRef",
    "FileInfo",
    "ImportRef",
    "IngestError",
    "IngestedRepo",
    "Language",
    "Parameter",
    "ParameterKind",
    "ParsedFile",
    "ParsedRepo",
    "RepoInventory",
    "SkipReason",
    "SkippedPath",
    "SourceKind",
    "Symbol",
    "SymbolKind",
    "analyze_repo",
    "build_inventory",
    "extract_file",
    "extract_source",
    "extract_symbols",
    "ingest",
    "inventory_for_path",
    "parse_repo",
    "walk_files",
]


def parse_repo(
    source: str,
    *,
    workspace_dir: Path | str = DEFAULT_WORKSPACE,
    max_size_mb: int = DEFAULT_MAX_SIZE_MB,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    force: bool = False,
) -> RepoInventory:
    """Stage `source` (git URL, zip, or local directory) and inventory it."""
    repo = ingest(
        source,
        workspace_dir=workspace_dir,
        max_size_mb=max_size_mb,
        timeout_seconds=timeout_seconds,
        force=force,
    )
    return build_inventory(repo)


def analyze_repo(
    source: str,
    *,
    workspace_dir: Path | str = DEFAULT_WORKSPACE,
    max_size_mb: int = DEFAULT_MAX_SIZE_MB,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    force: bool = False,
) -> ParsedRepo:
    """Full pipeline: stage, inventory, then extract symbols from every file."""
    inventory = parse_repo(
        source,
        workspace_dir=workspace_dir,
        max_size_mb=max_size_mb,
        timeout_seconds=timeout_seconds,
        force=force,
    )
    return ParsedRepo(inventory=inventory, files=extract_symbols(inventory))
