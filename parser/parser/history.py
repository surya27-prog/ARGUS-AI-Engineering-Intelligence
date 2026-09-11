"""Git history: which files change together.

The static graph says `api.py` imports `service.py`. It cannot say that
`service.py` and `test_service.py` are edited in the same commit nine times out
of ten, or that a config file and a client module are joined at the hip through
a convention no import expresses. That coupling is real — it breaks builds —
and the only place it is written down is the commit log.

So: read `git log --name-status`, and for every pair of files that appear in the
same commit, count how often. The output is a set of weighted pairs the graph
writer turns into `CO_CHANGED` edges.

Three decisions shape the numbers:

**Raw counts are not the signal.** A file touched in 300 commits shares a commit
with everything; support alone would rank the repo's busiest file against every
other file. Each pair therefore also carries a Jaccard index — shared commits
over the union of both files' commits — which asks "when one changes, how often
does the other?" rather than "how big are these two".

**Mass commits are dropped, not down-weighted.** A reformat touching 400 files
would emit ~80,000 pairs, all of them meaningless and all of them outnumbering
the real ones. Commits above `max_files_per_commit` are excluded entirely —
from the pair counts *and* from each file's commit total, since counting them in
the denominator alone would punish files for appearing in a refactor.

**Renames are followed.** `-M` reports `R100 old new`, and history is walked
newest-first, so a rename seen now tells us what every older mention of `old`
should be called. Without it a file that moved last month has its history split
in two and couples with nothing.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from parser.ingest import IngestError, run_git

logger = logging.getLogger(__name__)

# How far back to read. Deep history describes a codebase that no longer exists:
# the pairs that matter are the ones from the era the current files live in.
DEFAULT_MAX_COMMITS = 500

# Above this, a commit is a refactor, a merge of a long branch, or a generated
# file dump — see the module docstring.
DEFAULT_MAX_FILES_PER_COMMIT = 40

# One shared commit is a coincidence. Two is the smallest thing that can be
# called a pattern, and it already cuts the pair count by an order of magnitude.
DEFAULT_MIN_SHARED_COMMITS = 2

DEFAULT_TIMEOUT_SECONDS = 120

# NUL separates commits and unit-separator separates fields, because neither can
# occur in a path or a commit subject. Splitting on newlines instead would break
# on the first commit message that contains a tab.
#
# They are written as git's `%x00`/`%x1f` escapes rather than as literal bytes:
# Windows' CreateProcess rejects an argument containing a NUL outright, so a
# literal separator in the format string fails before git ever runs.
_RECORD = "\x00"
_FIELD = "\x1f"
_FORMAT = "%x00%H%x1f%aI%x1f%an%x1f%s"


class HistoryError(Exception):
    """Raised when a repository's history cannot be read."""


@dataclass(frozen=True, slots=True)
class Commit:
    """One commit and the files it touched, with renames already followed."""

    sha: str
    authored_at: str
    author: str
    subject: str
    paths: tuple[str, ...] = ()

    @property
    def short_sha(self) -> str:
        return self.sha[:8]


@dataclass(frozen=True, slots=True)
class CoChangePair:
    """Two files that have been committed together, and how tightly.

    `left` and `right` are ordered lexicographically rather than by any notion
    of cause: co-change is symmetric, and a canonical order is what stops the
    same pair being counted twice.
    """

    left: str
    right: str
    shared_commits: int
    left_commits: int
    right_commits: int
    jaccard: float
    last_together: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.left, self.right)


@dataclass(frozen=True, slots=True)
class RepoHistory:
    """The co-change picture of a repository over the window that was read."""

    commits_read: int
    commits_used: int
    commits_skipped: int
    file_commits: dict[str, int]
    pairs: tuple[CoChangePair, ...] = ()
    first_commit_at: str | None = None
    last_commit_at: str | None = None

    @property
    def file_count(self) -> int:
        return len(self.file_commits)

    @property
    def pair_count(self) -> int:
        return len(self.pairs)


def has_history(root: Path | str) -> bool:
    """True when `root` looks like a git working tree.

    A zip upload or a plain directory has no history, which is a normal state
    and not an error — the caller skips the pass rather than failing the parse.
    """
    path = Path(root)
    # A worktree or submodule has `.git` as a file pointing elsewhere.
    return (path / ".git").exists()


def read_commits(
    root: Path | str,
    *,
    max_commits: int = DEFAULT_MAX_COMMITS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[Commit, ...]:
    """The last `max_commits` commits, newest first, with their changed paths.

    Merges are excluded: a merge commit's diff repeats every file already
    attributed to the commits it merges, which would double-count whole branches
    of coupling.
    """
    args = [
        "-C",
        str(root),
        "log",
        f"--max-count={max_commits}",
        "--no-merges",
        "--name-status",
        # Follow renames, and report them as R<score> old new.
        "-M",
        f"--pretty=format:{_FORMAT}",
    ]
    try:
        output = run_git(args, timeout_seconds=timeout_seconds, failure="git log failed")
    except IngestError as exc:
        message = str(exc)
        if "does not have any commits yet" in message:
            return ()
        raise HistoryError(message) from exc

    return _parse_log(output)


def _parse_log(output: str) -> tuple[Commit, ...]:
    """Turn `git log --name-status` output into commits, following renames.

    Walked newest-first, which is the order git prints and the order the rename
    map needs: a rename seen here renames every older mention of the old path.
    """
    renames: dict[str, str] = {}
    commits: list[Commit] = []

    for record in output.split(_RECORD):
        if not record.strip():
            continue
        header, _, body = record.partition("\n")
        fields = header.split(_FIELD)
        if len(fields) < 4:
            logger.debug("Skipping unparseable log record: %r", header[:80])
            continue
        sha, authored_at, author, subject = fields[0], fields[1], fields[2], fields[3]

        paths: list[str] = []
        for line in body.splitlines():
            entry = _parse_status_line(line)
            if entry is None:
                continue
            path, renamed_from = entry
            current = _canonical(renames, path)
            if renamed_from is not None:
                renames[renamed_from] = current
            paths.append(current)

        commits.append(
            Commit(
                sha=sha,
                authored_at=authored_at,
                author=author,
                subject=subject,
                # Deduped: a rename plus a modification of the same file in one
                # commit is one file changing, not two.
                paths=tuple(sorted(set(paths))),
            )
        )

    return tuple(commits)


def _parse_status_line(line: str) -> tuple[str, str | None] | None:
    """`M\\tpath` -> (path, None); `R100\\told\\tnew` -> (new, old)."""
    if not line.strip():
        return None
    parts = line.split("\t")
    if len(parts) < 2:
        return None
    status = parts[0]
    if status.startswith(("R", "C")) and len(parts) >= 3:
        return parts[2], parts[1]
    return parts[1], None


def _canonical(renames: dict[str, str], path: str) -> str:
    """Follow a rename chain to the name the file has today.

    The guard is not paranoia: a file renamed away and back (`a` -> `b` -> `a`)
    is a real thing people do, and it makes the map cyclic.
    """
    seen: set[str] = set()
    current = path
    while (nxt := renames.get(current)) is not None and nxt not in seen:
        seen.add(current)
        current = nxt
    return current


def co_change(
    commits: tuple[Commit, ...],
    *,
    paths: set[str] | None = None,
    max_files_per_commit: int = DEFAULT_MAX_FILES_PER_COMMIT,
    min_shared_commits: int = DEFAULT_MIN_SHARED_COMMITS,
    min_jaccard: float = 0.0,
    max_pairs: int = 5000,
) -> RepoHistory:
    """Count how often each pair of files was committed together.

    `paths` restricts the analysis to files that still exist and are worth
    edges — the parsed inventory, in practice. Filtering *before* the counting,
    rather than dropping unwanted pairs afterwards, does two things: a lockfile
    or a changelog touched by every commit stops generating a spurious pair with
    every file in the repo, and the mass-commit cap starts counting the files
    that matter, so a three-file change that also regenerates fifty assets is
    still treated as a three-file change.
    """
    file_commits: Counter[str] = Counter()
    shared: Counter[tuple[str, str]] = Counter()
    last_together: dict[tuple[str, str], str] = {}
    used = skipped = 0

    for commit in commits:
        relevant = sorted(
            commit.paths if paths is None else [p for p in commit.paths if p in paths]
        )
        if not relevant:
            continue
        # Excluded from the file totals as well as the pairs — see the module
        # docstring on mass commits.
        if len(relevant) > max_files_per_commit:
            skipped += 1
            continue

        used += 1
        file_commits.update(relevant)
        for pair in combinations(relevant, 2):
            shared[pair] += 1
            # Newest first, so the first sighting of a pair is its latest.
            last_together.setdefault(pair, commit.authored_at)

    pairs = [
        CoChangePair(
            left=left,
            right=right,
            shared_commits=count,
            left_commits=file_commits[left],
            right_commits=file_commits[right],
            jaccard=round(
                count / (file_commits[left] + file_commits[right] - count),
                4,
            ),
            last_together=last_together.get((left, right)),
        )
        for (left, right), count in shared.items()
        if count >= min_shared_commits
    ]
    pairs = [p for p in pairs if p.jaccard >= min_jaccard]
    pairs.sort(key=lambda p: (p.shared_commits, p.jaccard), reverse=True)

    dates = [c.authored_at for c in commits if c.authored_at]
    history = RepoHistory(
        commits_read=len(commits),
        commits_used=used,
        commits_skipped=skipped,
        file_commits=dict(file_commits),
        pairs=tuple(pairs[:max_pairs]),
        # Newest first, so the ends of the window are the last and first rows.
        last_commit_at=dates[0] if dates else None,
        first_commit_at=dates[-1] if dates else None,
    )
    logger.info(
        "History: %d commits read, %d used, %d skipped as mass changes; %d coupled pairs",
        history.commits_read,
        history.commits_used,
        history.commits_skipped,
        history.pair_count,
    )
    return history


def analyze_history(
    root: Path | str,
    *,
    paths: set[str] | None = None,
    max_commits: int = DEFAULT_MAX_COMMITS,
    max_files_per_commit: int = DEFAULT_MAX_FILES_PER_COMMIT,
    min_shared_commits: int = DEFAULT_MIN_SHARED_COMMITS,
    min_jaccard: float = 0.0,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> RepoHistory:
    """Read `root`'s history and reduce it to co-change pairs. The whole pass."""
    if not has_history(root):
        logger.info("No git history at %s; skipping co-change analysis", root)
        return RepoHistory(commits_read=0, commits_used=0, commits_skipped=0, file_commits={})

    commits = read_commits(root, max_commits=max_commits, timeout_seconds=timeout_seconds)
    return co_change(
        commits,
        paths=paths,
        max_files_per_commit=max_files_per_commit,
        min_shared_commits=min_shared_commits,
        min_jaccard=min_jaccard,
    )


def coupling_for(history: RepoHistory, path: str) -> list[CoChangePair]:
    """Every pair `path` takes part in, strongest first. Handy in a REPL."""
    partners = [p for p in history.pairs if path in (p.left, p.right)]
    partners.sort(key=lambda p: (p.shared_commits, p.jaccard), reverse=True)
    return partners


def partner_index(history: RepoHistory) -> dict[str, list[CoChangePair]]:
    """Pairs grouped by each participating file, for callers that need all of them."""
    index: dict[str, list[CoChangePair]] = defaultdict(list)
    for pair in history.pairs:
        index[pair.left].append(pair)
        index[pair.right].append(pair)
    return dict(index)
