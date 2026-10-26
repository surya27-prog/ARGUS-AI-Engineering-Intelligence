from __future__ import annotations

import json
from pathlib import Path

import pytest

from parser.cli import main


def test_table_output_lists_files(repo_root: Path, capsys: pytest.CaptureFixture[str]):
    assert main([str(repo_root)]) == 0
    out = capsys.readouterr().out

    assert "app/core/config.py" in out
    assert "app.core.config" in out
    assert "6 files" in out
    assert "ignored_dir" in out


def test_json_output_is_parseable(repo_root: Path, capsys: pytest.CaptureFixture[str]):
    assert main([str(repo_root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["file_count"] == 6
    assert payload["source_kind"] == "local"
    assert {f["path"] for f in payload["files"]} >= {"app/main.py", "app/core/config.py"}


def test_limit_truncates_and_says_so(repo_root: Path, capsys: pytest.CaptureFixture[str]):
    assert main([str(repo_root), "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "... 4 more" in out


def test_show_skipped_lists_paths(repo_root: Path, capsys: pytest.CaptureFixture[str]):
    assert main([str(repo_root), "--show-skipped"]) == 0
    out = capsys.readouterr().out
    assert "node_modules" in out
    assert "unsupported_extension" in out


def test_bad_source_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert main([str(tmp_path / "missing")]) == 1
    assert "error:" in capsys.readouterr().err
