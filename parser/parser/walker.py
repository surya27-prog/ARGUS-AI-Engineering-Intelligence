"""Walk a staged repository and produce its file inventory.

The walker is deliberately dumb about code: it decides which files are worth
parsing and records enough metadata to identify them. Reading the contents for
symbols is Day 5's job.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

from parser.extractor import extract_file
from parser.ingest import IngestedRepo
from parser.models import (
    FileInfo,
    Language,
    ParsedFile,
    RepoInventory,
    SkippedPath,
    SkipReason,
    SourceKind,
)

# Directories that never contain first-party source but are expensive to walk.
IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "venv",
        ".venv",
        "env",
        ".env",
        "virtualenv",
        "site-packages",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".eggs",
        "build",
        "dist",
        "target",
        "out",
        ".next",
        ".nuxt",
        ".cache",
        "coverage",
        "htmlcov",
        ".idea",
        ".vscode",
        ".terraform",
    }
)

# Week 1 is Python-only by design (see the scope-cut order in TIMELINE.md).
# Adding a language means adding its extensions here plus an AST extractor.
LANGUAGE_BY_EXTENSION: dict[str, Language] = {
    ".py": Language.PYTHON,
    ".pyi": Language.PYTHON,
}

# Generated or vendored single files well past this size are not worth parsing
# and would dominate embedding cost later.
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024

_HASH_CHUNK_BYTES = 65536


def walk_files(
    root: Path,
    *,
    ignored_dirs: frozenset[str] = IGNORED_DIRS,
    extensions: dict[str, Language] | None = None,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> Iterator[FileInfo | SkippedPath]:
    """Yield a `FileInfo` per parseable file and a `SkippedPath` per exclusion.

    Both kinds are yielded from one traversal so callers can report coverage
    without walking the tree twice.
    """
    languages = LANGUAGE_BY_EXTENSION if extensions is None else extensions
    root = root.resolve()

    for path in _iter_paths(root, root, ignored_dirs):
        relative = path.relative_to(root).as_posix()

        if path.is_symlink():
            # Following symlinks risks cycles and escaping the repo root.
            yield SkippedPath(relative, SkipReason.SYMLINK)
            continue
        if path.is_dir():
            yield SkippedPath(relative, SkipReason.IGNORED_DIR)
            continue

        language = languages.get(path.suffix.lower())
        if language is None:
            yield SkippedPath(relative, SkipReason.UNSUPPORTED_EXTENSION)
            continue

        try:
            size = path.stat().st_size
        except OSError:
            yield SkippedPath(relative, SkipReason.UNREADABLE)
            continue

        if max_file_bytes and size > max_file_bytes:
            yield SkippedPath(relative, SkipReason.TOO_LARGE)
            continue

        try:
            line_count, sha256 = _measure(path)
        except OSError:
            yield SkippedPath(relative, SkipReason.UNREADABLE)
            continue

        yield FileInfo(
            path=relative,
            language=language,
            extension=path.suffix.lower(),
            size_bytes=size,
            line_count=line_count,
            sha256=sha256,
            module=_module_path(path, root),
        )


def build_inventory(
    repo: IngestedRepo,
    *,
    ignored_dirs: frozenset[str] = IGNORED_DIRS,
    extensions: dict[str, Language] | None = None,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> RepoInventory:
    """Walk a staged repository into a complete `RepoInventory`."""
    files: list[FileInfo] = []
    skipped: list[SkippedPath] = []

    for entry in walk_files(
        repo.root,
        ignored_dirs=ignored_dirs,
        extensions=extensions,
        max_file_bytes=max_file_bytes,
    ):
        if isinstance(entry, FileInfo):
            files.append(entry)
        else:
            skipped.append(entry)

    files.sort(key=lambda f: f.path)
    skipped.sort(key=lambda s: s.path)

    return RepoInventory(
        name=repo.name,
        root=str(repo.root),
        source_kind=repo.source_kind,
        files=tuple(files),
        skipped=tuple(skipped),
        url=repo.url,
        commit_sha=repo.commit_sha,
        default_branch=repo.default_branch,
    )


def extract_symbols(inventory: RepoInventory) -> tuple[ParsedFile, ...]:
    """Run AST extraction over every file in an inventory.

    Files are extracted independently and failures are captured on the
    `ParsedFile` rather than raised, so one unparseable file cannot cost you the
    rest of the repository.
    """
    root = Path(inventory.root)
    return tuple(
        extract_file(root / file.path, repo_root=root, module=file.module)
        for file in inventory.files
        if file.language is Language.PYTHON
    )


def inventory_for_path(path: Path | str, **kwargs) -> RepoInventory:
    """Convenience wrapper for walking a directory that is already on disk."""
    root = Path(path).expanduser().resolve()
    repo = IngestedRepo(root=root, name=root.name, source_kind=SourceKind.LOCAL)
    return build_inventory(repo, **kwargs)


def _iter_paths(current: Path, root: Path, ignored_dirs: frozenset[str]) -> Iterator[Path]:
    """Depth-first walk that prunes ignored directories instead of descending.

    Pruned directories are yielded once so the caller can record them, which is
    what keeps `IGNORED_DIRS` honest — you can see what a parse chose to skip.
    """
    try:
        entries = sorted(current.iterdir(), key=lambda p: p.name)
    except OSError:
        return

    for entry in entries:
        if entry.is_dir() and not entry.is_symlink():
            if entry.name in ignored_dirs:
                yield entry
                continue
            yield from _iter_paths(entry, root, ignored_dirs)
        else:
            yield entry


def _measure(path: Path) -> tuple[int, str]:
    """Return (line_count, sha256) in a single read of the file."""
    digest = hashlib.sha256()
    lines = 0
    last_byte = b""

    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
            lines += chunk.count(b"\n")
            last_byte = chunk[-1:]

    # A final line without a trailing newline still counts.
    if last_byte and last_byte != b"\n":
        lines += 1

    return lines, digest.hexdigest()


def _module_path(path: Path, root: Path) -> str | None:
    """Derive the dotted import path from package layout.

    Walks up from the file while each directory is a package (`__init__.py`),
    which is what makes `app/core/config.py` resolve to `app.core.config` even
    when the repo root is not itself importable.
    """
    if path.suffix.lower() not in {".py", ".pyi"}:
        return None

    parts = [path.stem] if path.stem != "__init__" else []
    parent = path.parent

    while parent != root and (parent / "__init__.py").exists():
        parts.append(parent.name)
        parent = parent.parent

    if not parts:
        return None
    return ".".join(reversed(parts))
