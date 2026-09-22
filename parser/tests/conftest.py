from __future__ import annotations

from pathlib import Path

import pytest

# A miniature repository covering every case the walker has to decide about:
# packages, a module outside any package, ignored directories, unsupported
# extensions, and a file with no trailing newline.
FIXTURE_TREE: dict[str, str] = {
    "app/__init__.py": "",
    "app/main.py": "import os\n\n\ndef run():\n    return os.getcwd()\n",
    "app/core/__init__.py": "",
    "app/core/config.py": "SETTING = 1\n",
    "app/core/types.pyi": "SETTING: int\n",
    "scripts/oneoff.py": "print('no package here')",  # no trailing newline
    "README.md": "# fixture\n",
    "package.json": "{}\n",
    ".git/config": "[core]\n",
    "node_modules/left-pad/index.js": "module.exports = 1;\n",
    ".venv/lib/site.py": "# should never be parsed\n",
    "app/__pycache__/main.cpython-311.pyc": "\x00\x00",
}


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    """Real .py files used to exercise AST extraction."""
    return FIXTURES_DIR


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Materialize FIXTURE_TREE under a temp directory and return its root."""
    for relative, content in FIXTURE_TREE.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path
