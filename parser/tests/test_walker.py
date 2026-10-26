from __future__ import annotations

import hashlib
from pathlib import Path

from parser.ingest import IngestedRepo
from parser.models import FileInfo, Language, SkippedPath, SkipReason, SourceKind
from parser.walker import build_inventory, inventory_for_path, walk_files


def paths(root: Path) -> set[str]:
    return {e.path for e in walk_files(root) if isinstance(e, FileInfo)}


def test_finds_only_python_sources(repo_root: Path):
    assert paths(repo_root) == {
        "app/__init__.py",
        "app/main.py",
        "app/core/__init__.py",
        "app/core/config.py",
        "app/core/types.pyi",
        "scripts/oneoff.py",
    }


def test_ignores_vendor_and_cache_dirs(repo_root: Path):
    found = paths(repo_root)
    assert not any(p.startswith((".git/", "node_modules/", ".venv/")) for p in found)
    assert not any("__pycache__" in p for p in found)


def test_ignored_dirs_are_recorded_not_silently_dropped(repo_root: Path):
    skipped = {e.path: e.reason for e in walk_files(repo_root) if isinstance(e, SkippedPath)}
    assert skipped[".git"] is SkipReason.IGNORED_DIR
    assert skipped["node_modules"] is SkipReason.IGNORED_DIR
    assert skipped["README.md"] is SkipReason.UNSUPPORTED_EXTENSION


def test_module_path_follows_package_layout(repo_root: Path):
    modules = {e.path: e.module for e in walk_files(repo_root) if isinstance(e, FileInfo)}
    assert modules["app/core/config.py"] == "app.core.config"
    assert modules["app/main.py"] == "app.main"
    # __init__.py names the package itself, not a `.__init__` submodule.
    assert modules["app/core/__init__.py"] == "app.core"
    # scripts/ has no __init__.py, so the file is not importable as a package member.
    assert modules["scripts/oneoff.py"] == "oneoff"


def test_line_count_handles_missing_trailing_newline(repo_root: Path):
    files = {e.path: e for e in walk_files(repo_root) if isinstance(e, FileInfo)}
    assert files["scripts/oneoff.py"].line_count == 1
    assert files["app/main.py"].line_count == 5
    assert files["app/__init__.py"].line_count == 0


def test_sha256_matches_file_contents(repo_root: Path):
    files = {e.path: e for e in walk_files(repo_root) if isinstance(e, FileInfo)}
    expected = hashlib.sha256((repo_root / "app/core/config.py").read_bytes()).hexdigest()
    assert files["app/core/config.py"].sha256 == expected


def test_max_file_bytes_marks_large_files(repo_root: Path):
    (repo_root / "app" / "huge.py").write_text("x = 1\n" * 5000, encoding="utf-8")
    skipped = {
        e.path: e.reason
        for e in walk_files(repo_root, max_file_bytes=100)
        if isinstance(e, SkippedPath)
    }
    assert skipped["app/huge.py"] is SkipReason.TOO_LARGE


def test_symlinks_are_skipped(repo_root: Path):
    link = repo_root / "app" / "linked.py"
    try:
        link.symlink_to(repo_root / "app" / "main.py")
    except (OSError, NotImplementedError):
        return  # Windows without developer mode cannot create symlinks
    skipped = {e.path: e.reason for e in walk_files(repo_root) if isinstance(e, SkippedPath)}
    assert skipped["app/linked.py"] is SkipReason.SYMLINK


def test_build_inventory_totals_and_ordering(repo_root: Path):
    repo = IngestedRepo(root=repo_root, name="fixture", source_kind=SourceKind.LOCAL)
    inventory = build_inventory(repo)

    assert inventory.name == "fixture"
    assert inventory.file_count == 6
    assert inventory.total_lines == sum(f.line_count for f in inventory.files)
    assert [f.path for f in inventory.files] == sorted(f.path for f in inventory.files)
    assert all(f.language is Language.PYTHON for f in inventory.files)
    assert inventory.skipped_by_reason()[SkipReason.IGNORED_DIR] >= 3


def test_inventory_is_json_serializable(repo_root: Path):
    payload = inventory_for_path(repo_root).to_json()
    assert '"language": "python"' in payload
    assert '"file_count": 6' in payload


def test_empty_directory_yields_empty_inventory(tmp_path: Path):
    inventory = inventory_for_path(tmp_path)
    assert inventory.file_count == 0
    assert inventory.total_lines == 0
