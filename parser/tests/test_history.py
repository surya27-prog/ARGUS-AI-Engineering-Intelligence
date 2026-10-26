"""Co-change analysis tests.

These build real git repositories in a temp directory rather than feeding
canned `git log` text to the parser. The parsing is half the risk here — rename
records, NUL-separated commits, merge exclusion — and a fake log would be
written to match whatever the parser already does, which tests nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from parser.history import (
    Commit,
    HistoryError,
    analyze_history,
    co_change,
    coupling_for,
    has_history,
    partner_index,
    read_commits,
)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An initialised repo with committer identity set, so commits succeed on CI."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "fixture@argus.test")
    git(root, "config", "user.name", "Fixture")
    return root


def commit(root: Path, message: str, files: dict[str, str]) -> str:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        git(root, "add", relative)
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


# --- reading the log ---------------------------------------------------------


def test_a_directory_without_git_has_no_history(tmp_path: Path):
    assert has_history(tmp_path) is False
    history = analyze_history(tmp_path)
    assert history.commits_read == 0
    assert history.pairs == ()


def test_a_repo_with_no_commits_reads_as_empty(repo: Path):
    """`git log` exits non-zero on an unborn HEAD; that is empty, not broken."""
    assert has_history(repo) is True
    assert read_commits(repo) == ()


def test_reading_a_bad_path_raises(tmp_path: Path):
    with pytest.raises(HistoryError):
        read_commits(tmp_path / "not-a-repo-at-all")


def test_commits_come_back_newest_first_with_their_files(repo: Path):
    commit(repo, "first", {"a.py": "1\n"})
    commit(repo, "second", {"b.py": "2\n", "c.py": "3\n"})

    commits = read_commits(repo)

    assert [c.subject for c in commits] == ["second", "first"]
    assert commits[0].paths == ("b.py", "c.py")
    assert commits[1].paths == ("a.py",)
    assert commits[0].author == "Fixture"
    assert commits[0].short_sha == commits[0].sha[:8]


def test_a_commit_subject_with_tabs_does_not_break_parsing(repo: Path):
    """The reason fields are NUL/unit separated rather than whitespace split."""
    commit(repo, "fix\tthe\tthing", {"a.py": "1\n"})

    commits = read_commits(repo)

    assert commits[0].subject == "fix\tthe\tthing"
    assert commits[0].paths == ("a.py",)


def test_max_commits_limits_the_window(repo: Path):
    for i in range(5):
        commit(repo, f"c{i}", {"a.py": f"{i}\n"})

    assert len(read_commits(repo, max_commits=3)) == 3


def test_merge_commits_are_excluded(repo: Path):
    """A merge's diff repeats files already counted on the branch commits."""
    commit(repo, "base", {"a.py": "1\n"})
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "side work", {"side.py": "1\n"})
    git(repo, "checkout", "-q", "main")
    commit(repo, "main work", {"main_only.py": "1\n"})
    git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")

    subjects = [c.subject for c in read_commits(repo)]

    assert "merge side" not in subjects
    assert subjects == ["main work", "side work", "base"]


def test_a_delete_still_counts_as_a_change(repo: Path):
    commit(repo, "add", {"gone.py": "1\n", "keeper.py": "1\n"})
    (repo / "gone.py").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "remove")

    assert read_commits(repo)[0].paths == ("gone.py",)


# --- renames -----------------------------------------------------------------


def test_a_renamed_file_keeps_one_history_under_its_current_name(repo: Path):
    commit(repo, "add", {"old.py": "x = 1\n" * 20})
    commit(repo, "touch", {"old.py": "x = 2\n" * 20, "other.py": "1\n"})
    git(repo, "mv", "old.py", "new.py")
    git(repo, "commit", "-q", "-m", "rename")

    commits = read_commits(repo)

    # Every mention, including the two commits before the rename, reads as new.py.
    assert all("old.py" not in c.paths for c in commits)
    assert [c.paths for c in commits] == [("new.py",), ("new.py", "other.py"), ("new.py",)]


def test_a_rename_chain_resolves_to_the_final_name(repo: Path):
    commit(repo, "add", {"one.py": "x = 1\n" * 20})
    git(repo, "mv", "one.py", "two.py")
    git(repo, "commit", "-q", "-m", "first rename")
    git(repo, "mv", "two.py", "three.py")
    git(repo, "commit", "-q", "-m", "second rename")

    assert {p for c in read_commits(repo) for p in c.paths} == {"three.py"}


def test_a_file_renamed_away_and_back_does_not_loop(repo: Path):
    commit(repo, "add", {"a.py": "x = 1\n" * 20})
    git(repo, "mv", "a.py", "b.py")
    git(repo, "commit", "-q", "-m", "away")
    git(repo, "mv", "b.py", "a.py")
    git(repo, "commit", "-q", "-m", "back")

    assert {p for c in read_commits(repo) for p in c.paths} == {"a.py"}


# --- pair counting -----------------------------------------------------------


def _commits(*groups: tuple[str, ...]) -> tuple[Commit, ...]:
    """Synthetic commits, newest first, for the counting rules themselves."""
    return tuple(
        Commit(
            sha=f"{i:040x}",
            authored_at=f"2026-08-{20 - i:02d}T10:00:00+00:00",
            author="Fixture",
            subject=f"c{i}",
            paths=tuple(sorted(paths)),
        )
        for i, paths in enumerate(groups)
    )


def test_files_committed_together_twice_become_a_pair():
    history = co_change(_commits(("a.py", "b.py"), ("a.py", "b.py")))

    assert history.pair_count == 1
    pair = history.pairs[0]
    assert (pair.left, pair.right) == ("a.py", "b.py")
    assert pair.shared_commits == 2
    assert pair.jaccard == 1.0


def test_a_single_shared_commit_is_below_the_threshold():
    assert co_change(_commits(("a.py", "b.py"))).pairs == ()
    assert co_change(_commits(("a.py", "b.py")), min_shared_commits=1).pair_count == 1


def test_jaccard_discounts_a_file_that_changes_with_everything():
    # b.py rides along with a.py twice, but changes four times in total.
    history = co_change(
        _commits(
            ("a.py", "b.py"),
            ("a.py", "b.py"),
            ("b.py", "c.py"),
            ("b.py", "d.py"),
        ),
        min_shared_commits=1,
    )
    pairs = {(p.left, p.right): p for p in history.pairs}

    ab = pairs[("a.py", "b.py")]
    assert ab.shared_commits == 2
    assert ab.left_commits == 2
    assert ab.right_commits == 4
    # 2 shared / (2 + 4 - 2) union
    assert ab.jaccard == 0.5


def test_the_pair_order_is_canonical_regardless_of_commit_order():
    history = co_change(_commits(("z.py", "a.py"), ("z.py", "a.py")))

    assert (history.pairs[0].left, history.pairs[0].right) == ("a.py", "z.py")


def test_pairs_are_ranked_by_shared_commits():
    history = co_change(
        _commits(
            ("a.py", "b.py"),
            ("a.py", "b.py"),
            ("a.py", "b.py"),
            ("c.py", "d.py"),
            ("c.py", "d.py"),
        )
    )

    assert [(p.left, p.right) for p in history.pairs] == [("a.py", "b.py"), ("c.py", "d.py")]


def test_the_last_time_a_pair_changed_together_is_recorded():
    commits = _commits(("a.py", "b.py"), ("a.py", "b.py"))
    history = co_change(commits)

    # Newest first, so the newest commit's date is the answer.
    assert history.pairs[0].last_together == commits[0].authored_at


def test_the_window_reports_its_own_ends():
    commits = _commits(("a.py",), ("b.py",), ("c.py",))
    history = co_change(commits, min_shared_commits=1)

    assert history.last_commit_at == commits[0].authored_at
    assert history.first_commit_at == commits[-1].authored_at


# --- the noise filters -------------------------------------------------------


def test_a_mass_commit_is_excluded_entirely():
    """A 50-file reformat would otherwise emit 1225 meaningless pairs."""
    mass = tuple(f"f{i}.py" for i in range(50))
    history = co_change(
        _commits(mass, ("a.py", "b.py"), ("a.py", "b.py")),
        max_files_per_commit=40,
    )

    assert history.commits_skipped == 1
    assert history.commits_used == 2
    assert history.pair_count == 1


def test_a_mass_commit_does_not_inflate_a_files_commit_count():
    """Excluded from the denominator too, or files are punished for refactors."""
    mass = tuple(f"f{i}.py" for i in range(50)) + ("a.py", "b.py")
    history = co_change(
        _commits(mass, ("a.py", "b.py"), ("a.py", "b.py")),
        max_files_per_commit=40,
    )

    assert history.file_commits["a.py"] == 2
    assert history.pairs[0].jaccard == 1.0


def test_only_requested_paths_are_counted():
    history = co_change(
        _commits(
            ("a.py", "b.py", "poetry.lock"),
            ("a.py", "b.py", "poetry.lock"),
        ),
        paths={"a.py", "b.py"},
    )

    assert history.file_count == 2
    assert history.pair_count == 1
    assert "poetry.lock" not in history.file_commits


def test_a_file_touched_by_every_commit_couples_with_everything_unfiltered():
    """Which is what filtering prevents: the lockfile pairs are all noise."""
    commits = _commits(
        ("a.py", "b.py", "poetry.lock"),
        ("a.py", "b.py", "poetry.lock"),
    )

    unfiltered = {(p.left, p.right) for p in co_change(commits).pairs}
    filtered = {(p.left, p.right) for p in co_change(commits, paths={"a.py", "b.py"}).pairs}

    assert unfiltered == {
        ("a.py", "b.py"),
        ("a.py", "poetry.lock"),
        ("b.py", "poetry.lock"),
    }
    assert filtered == {("a.py", "b.py")}


def test_filtering_first_keeps_a_small_change_out_of_the_mass_commit_cap():
    """Three source files plus fifty regenerated assets is a three-file change."""
    noisy = ("a.py", "b.py") + tuple(f"assets/{i}.svg" for i in range(50))
    history = co_change(
        _commits(noisy, noisy),
        paths={"a.py", "b.py"},
        max_files_per_commit=40,
    )

    assert history.commits_skipped == 0
    assert history.pair_count == 1


def test_a_minimum_jaccard_drops_weak_coupling():
    commits = _commits(
        ("a.py", "b.py"),
        ("a.py", "b.py"),
        ("b.py", "c.py"),
        ("b.py", "d.py"),
        ("b.py", "e.py"),
    )

    assert co_change(commits, min_jaccard=0.0).pair_count == 1
    assert co_change(commits, min_jaccard=0.9).pair_count == 0


def test_max_pairs_caps_the_result():
    history = co_change(
        _commits(("a.py", "b.py", "c.py"), ("a.py", "b.py", "c.py")),
        max_pairs=2,
    )

    assert history.pair_count == 2


# --- the whole pass ----------------------------------------------------------


def test_analyze_history_couples_a_module_with_its_test(repo: Path):
    commit(repo, "add", {"app/service.py": "1\n", "tests/test_service.py": "1\n"})
    commit(repo, "fix", {"app/service.py": "2\n", "tests/test_service.py": "2\n"})
    commit(repo, "docs", {"README.md": "hi\n"})

    history = analyze_history(repo, paths={"app/service.py", "tests/test_service.py"})

    assert history.commits_used == 2
    assert history.pair_count == 1
    pair = history.pairs[0]
    assert (pair.left, pair.right) == ("app/service.py", "tests/test_service.py")
    assert pair.shared_commits == 2
    assert pair.jaccard == 1.0
    assert pair.last_together is not None


def test_coupling_for_finds_a_files_partners_from_either_side():
    history = co_change(_commits(("a.py", "b.py"), ("a.py", "b.py")))

    assert len(coupling_for(history, "a.py")) == 1
    assert len(coupling_for(history, "b.py")) == 1
    assert coupling_for(history, "nope.py") == []


def test_partner_index_lists_a_pair_under_both_files():
    history = co_change(_commits(("a.py", "b.py"), ("a.py", "b.py")))

    index = partner_index(history)
    assert set(index) == {"a.py", "b.py"}
