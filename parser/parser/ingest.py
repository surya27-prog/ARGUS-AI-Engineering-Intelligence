"""Get a repository onto local disk so the walker can read it.

Three sources are supported — a git URL, a zip archive, or a directory that is
already on this machine. Everything lands under the workspace directory except
local directories, which are read in place rather than copied.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from parser.models import SourceKind

DEFAULT_WORKSPACE = Path("./workspace")
DEFAULT_MAX_SIZE_MB = 500
DEFAULT_TIMEOUT_SECONDS = 900

# How many commits a clone brings down. Week 1 cloned at depth 1 because only
# the current tree was needed; Week 4's co-change analysis reads the log, and a
# depth-1 clone has exactly one commit in it, so every pair count would be zero.
# Deep enough to see a real pattern, shallow enough not to fetch a decade.
DEFAULT_HISTORY_DEPTH = 500

_GIT_URL = re.compile(r"^(https?://|git@|ssh://|git://)")
# Repo names become directory names and Neo4j node keys; keep them boring.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class IngestError(Exception):
    """Raised when a repository cannot be staged for parsing."""


@dataclass(frozen=True, slots=True)
class IngestedRepo:
    """A repository staged on disk and ready to walk."""

    root: Path
    name: str
    source_kind: SourceKind
    url: str | None = None
    commit_sha: str | None = None
    default_branch: str | None = None


def classify_source(source: str) -> SourceKind:
    """Decide how `source` should be fetched, without touching the network."""
    if _GIT_URL.match(source) or source.endswith(".git"):
        return SourceKind.GIT
    if source.lower().endswith(".zip"):
        return SourceKind.ZIP
    return SourceKind.LOCAL


def derive_name(source: str, kind: SourceKind) -> str:
    """Human-facing repo name: `owner/thing.git` -> `thing`, `x/y.zip` -> `y`."""
    raw = source.rstrip("/\\")
    if kind is SourceKind.GIT:
        raw = raw.split("/")[-1].split(":")[-1]
        raw = raw.removesuffix(".git")
    else:
        raw = Path(raw).stem if kind is SourceKind.ZIP else Path(raw).name
    name = _UNSAFE_NAME_CHARS.sub("-", raw).strip("-.")
    if not name:
        raise IngestError(f"Could not derive a repository name from {source!r}")
    return name


def directory_size_bytes(root: Path) -> int:
    """Total size of files under `root`, following no symlinks."""
    total = 0
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def ingest(
    source: str,
    *,
    workspace_dir: Path | str = DEFAULT_WORKSPACE,
    max_size_mb: int = DEFAULT_MAX_SIZE_MB,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    force: bool = False,
    history_depth: int = DEFAULT_HISTORY_DEPTH,
) -> IngestedRepo:
    """Stage `source` for parsing and describe where it landed.

    `force` replaces an existing staging directory of the same name; without it
    a previously cloned repo is reused as-is. `history_depth` is how many
    commits a clone fetches — co-change analysis reads them.
    """
    kind = classify_source(source)
    name = derive_name(source, kind)
    workspace = Path(workspace_dir).expanduser().resolve()

    if kind is SourceKind.GIT:
        repo = _clone(source, workspace / name, name, timeout_seconds, force, history_depth)
    elif kind is SourceKind.ZIP:
        repo = _unzip(Path(source), workspace / name, name, force)
    else:
        repo = _use_local(Path(source), name)

    _enforce_size_limit(repo.root, max_size_mb)
    return repo


def _enforce_size_limit(root: Path, max_size_mb: int) -> None:
    if max_size_mb <= 0:
        return
    size_mb = directory_size_bytes(root) / (1024 * 1024)
    if size_mb > max_size_mb:
        raise IngestError(
            f"{root} is {size_mb:.0f} MB, over the {max_size_mb} MB limit "
            f"(raise MAX_REPO_SIZE_MB to parse it anyway)"
        )


def _prepare_dest(dest: Path, force: bool) -> bool:
    """Return True if `dest` is ready to be written, False if it already exists."""
    if dest.exists():
        if not force:
            return False
        remove_tree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    return True


def remove_tree(path: Path) -> None:
    """Delete a directory tree that may contain read-only files.

    Git marks everything under `.git/objects` read-only. On Windows that makes a
    plain `shutil.rmtree` fail with `PermissionError: [WinError 5]`, so a
    re-clone of an already-staged repo dies before it starts.
    """

    def make_writable(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    # `onerror` was renamed to `onexc` in 3.12; the callback arity is the same.
    handler = {"onexc" if sys.version_info >= (3, 12) else "onerror": make_writable}
    shutil.rmtree(path, **handler)


def _clone(
    url: str,
    dest: Path,
    name: str,
    timeout_seconds: int,
    force: bool,
    history_depth: int = DEFAULT_HISTORY_DEPTH,
) -> IngestedRepo:
    if _prepare_dest(dest, force):
        # Still shallow, just not depth 1: the tree is what the parser reads and
        # the commits behind it are what co-change analysis reads.
        run_git(
            ["clone", "--depth", str(history_depth), url, str(dest)],
            timeout_seconds=timeout_seconds,
            failure=f"git clone of {url} failed",
        )
    return IngestedRepo(
        root=dest,
        name=name,
        source_kind=SourceKind.GIT,
        url=url,
        commit_sha=_git_metadata(dest, ["rev-parse", "HEAD"]),
        default_branch=_git_metadata(dest, ["rev-parse", "--abbrev-ref", "HEAD"]),
    )


def run_git(args: list[str], *, timeout_seconds: int, failure: str) -> str:
    """Run one git command and return its stdout. Public so `parser.history` can
    read a staged repo's log through the same error handling."""
    try:
        result = subprocess.run(  # noqa: S603 - args are built here, never shell-interpolated
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise IngestError("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise IngestError(f"{failure}: timed out after {timeout_seconds}s") from exc
    if result.returncode != 0:
        raise IngestError(f"{failure}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def _git_metadata(repo: Path, args: list[str]) -> str | None:
    """Best-effort git lookup — metadata is nice to have, not worth failing over."""
    try:
        return run_git(["-C", str(repo), *args], timeout_seconds=30, failure="git query failed")
    except IngestError:
        return None


def _unzip(archive: Path, dest: Path, name: str, force: bool) -> IngestedRepo:
    if not archive.is_file():
        raise IngestError(f"No such archive: {archive}")
    if _prepare_dest(dest, force):
        try:
            with zipfile.ZipFile(archive) as zf:
                _reject_unsafe_members(zf, archive)
                zf.extractall(dest)
        except zipfile.BadZipFile as exc:
            raise IngestError(f"{archive} is not a valid zip archive") from exc
    return IngestedRepo(
        root=_strip_wrapper_dir(dest),
        name=name,
        source_kind=SourceKind.ZIP,
        url=str(archive),
    )


def _reject_unsafe_members(zf: zipfile.ZipFile, archive: Path) -> None:
    """Refuse zip-slip archives that would write outside the destination."""
    for member in zf.namelist():
        path = Path(member)
        if path.is_absolute() or ".." in path.parts:
            raise IngestError(f"{archive} contains an unsafe path: {member}")


def _strip_wrapper_dir(extracted: Path) -> Path:
    """GitHub zips nest everything under `repo-branch/`; descend into it."""
    entries = list(extracted.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return extracted


def _use_local(path: Path, name: str) -> IngestedRepo:
    root = path.expanduser().resolve()
    if not root.is_dir():
        raise IngestError(f"Not a directory: {path}")
    git_dir = root / ".git"
    return IngestedRepo(
        root=root,
        name=name,
        source_kind=SourceKind.LOCAL,
        commit_sha=_git_metadata(root, ["rev-parse", "HEAD"]) if git_dir.exists() else None,
        default_branch=(
            _git_metadata(root, ["rev-parse", "--abbrev-ref", "HEAD"]) if git_dir.exists() else None
        ),
    )
