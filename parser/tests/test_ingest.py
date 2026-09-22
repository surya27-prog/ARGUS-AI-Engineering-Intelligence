from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from parser.ingest import (
    IngestError,
    classify_source,
    derive_name,
    directory_size_bytes,
    ingest,
    remove_tree,
)
from parser.models import SourceKind


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("https://github.com/psf/requests.git", SourceKind.GIT),
        ("https://github.com/psf/requests", SourceKind.GIT),
        ("git@github.com:psf/requests.git", SourceKind.GIT),
        ("./archives/requests.zip", SourceKind.ZIP),
        ("/home/me/code/requests", SourceKind.LOCAL),
        ("C:\\code\\requests", SourceKind.LOCAL),
    ],
)
def test_classify_source(source: str, expected: SourceKind):
    assert classify_source(source) is expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("https://github.com/psf/requests.git", "requests"),
        ("git@github.com:psf/requests.git", "requests"),
        ("https://github.com/tiangolo/fastapi/", "fastapi"),
        ("./archives/my repo.zip", "my-repo"),
        ("/home/me/code/argus/", "argus"),
    ],
)
def test_derive_name(source: str, expected: str):
    assert derive_name(source, classify_source(source)) == expected


def test_local_directory_is_used_in_place(repo_root: Path):
    repo = ingest(str(repo_root))
    assert repo.root == repo_root.resolve()
    assert repo.source_kind is SourceKind.LOCAL
    assert repo.name == repo_root.name


def test_local_missing_directory_errors(tmp_path: Path):
    with pytest.raises(IngestError, match="Not a directory"):
        ingest(str(tmp_path / "nope"))


def test_zip_is_extracted_into_workspace(repo_root: Path, tmp_path: Path):
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(repo_root / "app" / "main.py", "sample/app/main.py")

    workspace = tmp_path / "workspace"
    repo = ingest(str(archive), workspace_dir=workspace)

    assert repo.source_kind is SourceKind.ZIP
    assert repo.name == "bundle"
    # GitHub-style archives nest under a single directory; it gets stripped.
    assert repo.root.name == "sample"
    assert (repo.root / "app" / "main.py").is_file()


def test_zip_slip_is_rejected(tmp_path: Path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escaped.py", "pwned = True\n")

    with pytest.raises(IngestError, match="unsafe path"):
        ingest(str(archive), workspace_dir=tmp_path / "workspace")


def test_corrupt_zip_errors(tmp_path: Path):
    archive = tmp_path / "broken.zip"
    archive.write_text("not a zip", encoding="utf-8")

    with pytest.raises(IngestError, match="not a valid zip"):
        ingest(str(archive), workspace_dir=tmp_path / "workspace")


def test_size_limit_is_enforced(repo_root: Path):
    with pytest.raises(IngestError, match="over the"):
        # The fixture tree is a few hundred bytes, so any positive MB limit
        # passes; force the check with a limit of effectively zero.
        ingest(str(repo_root), max_size_mb=0.000001)  # type: ignore[arg-type]


def test_directory_size_counts_files(repo_root: Path):
    assert directory_size_bytes(repo_root) > 0


def test_remove_tree_deletes_read_only_files(tmp_path: Path):
    # Git leaves everything under .git/objects read-only, which is what makes a
    # plain rmtree fail on Windows when re-staging an already-cloned repo.
    tree = tmp_path / "staged"
    (tree / "objects").mkdir(parents=True)
    locked = tree / "objects" / "abc123"
    locked.write_text("packed", encoding="utf-8")
    locked.chmod(stat.S_IREAD)

    remove_tree(tree)

    assert not tree.exists()


def test_force_restages_an_existing_clone(repo_root: Path, tmp_path: Path):
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("app/main.py", "x = 1\n")
    workspace = tmp_path / "workspace"

    first = ingest(str(archive), workspace_dir=workspace)
    stale = first.root / "stale.py"
    stale.write_text("gone soon\n", encoding="utf-8")

    second = ingest(str(archive), workspace_dir=workspace, force=True)

    assert second.root == first.root
    assert not stale.exists()
