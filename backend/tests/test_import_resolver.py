"""Import-resolution tests. Pure functions, so no database is involved.

The shape under test is a small package tree:

    app/__init__.py          app
    app/core/__init__.py     app.core
    app/core/config.py       app.core.config
    app/api/health.py        app.api.health
    scripts/tool.py          (outside any package)
"""

from __future__ import annotations

import pytest

from app.services.import_resolver import (
    ImportResolution,
    build_module_index,
    resolve_imports,
)
from parser.models import (
    FileInfo,
    ImportRef,
    Language,
    ParsedFile,
    ParsedRepo,
    RepoInventory,
    SourceKind,
)

_TREE = {
    "app/__init__.py": "app",
    "app/core/__init__.py": "app.core",
    "app/core/config.py": "app.core.config",
    "app/api/__init__.py": "app.api",
    "app/api/health.py": "app.api.health",
    "scripts/tool.py": None,
}


def _repo(imports_by_path: dict[str, tuple[ImportRef, ...]]) -> ParsedRepo:
    files = tuple(
        ParsedFile(
            path=path,
            module=module,
            language=Language.PYTHON,
            imports=imports_by_path.get(path, ()),
        )
        for path, module in _TREE.items()
    )
    inventory = RepoInventory(
        name="fixture",
        root="/tmp/fixture",
        source_kind=SourceKind.LOCAL,
        files=tuple(
            FileInfo(
                path=f.path,
                language=Language.PYTHON,
                extension=".py",
                size_bytes=1,
                line_count=1,
                sha256="x",
                module=f.module,
            )
            for f in files
        ),
    )
    return ParsedRepo(inventory=inventory, files=files)


def _resolve_one(path: str, ref: ImportRef):
    resolved = resolve_imports(_repo({path: (ref,)}))
    assert len(resolved) == 1
    return resolved[0]


def test_module_index_skips_files_outside_a_package():
    index = build_module_index(_repo({}))
    assert index["app.core.config"] == "app/core/config.py"
    assert "scripts/tool.py" not in index.values()


# --- absolute imports --------------------------------------------------------


def test_absolute_module_import_resolves_to_the_file():
    entry = _resolve_one("app/api/health.py", ImportRef(module="app.core.config", line=3))
    assert entry.resolution is ImportResolution.INTERNAL_MODULE
    assert entry.target_path == "app/core/config.py"
    assert not entry.is_relative


def test_from_import_of_a_symbol_resolves_to_the_defining_file():
    entry = _resolve_one(
        "app/api/health.py",
        ImportRef(module="app.core.config", name="Settings", line=4, is_from=True),
    )
    assert entry.resolution is ImportResolution.INTERNAL_SYMBOL
    assert entry.target_path == "app/core/config.py"


def test_from_import_of_a_submodule_beats_the_symbol_reading():
    """`from app.core import config` names a module, not a symbol in `app.core`."""
    entry = _resolve_one(
        "app/api/health.py",
        ImportRef(module="app.core", name="config", line=5, is_from=True),
    )
    assert entry.resolution is ImportResolution.INTERNAL_MODULE
    assert entry.target_path == "app/core/config.py"


def test_third_party_import_is_external():
    entry = _resolve_one("app/api/health.py", ImportRef(module="fastapi", line=1))
    assert entry.resolution is ImportResolution.EXTERNAL
    assert entry.target_module == "fastapi"
    assert entry.target_path is None


def test_stdlib_submodule_keeps_its_dotted_name():
    entry = _resolve_one("app/api/health.py", ImportRef(module="os.path", line=1))
    assert entry.target_module == "os.path"


def test_a_name_that_only_looks_internal_is_external():
    entry = _resolve_one("app/api/health.py", ImportRef(module="app_utils", line=1))
    assert entry.resolution is ImportResolution.EXTERNAL


# --- relative imports --------------------------------------------------------


def test_single_dot_resolves_within_the_current_package():
    """`from .config import x` inside app/core/__init__.py."""
    entry = _resolve_one(
        "app/core/__init__.py",
        ImportRef(module="config", name="Settings", line=2, level=1, is_from=True),
    )
    assert entry.resolution is ImportResolution.RELATIVE
    assert entry.target_path == "app/core/config.py"
    assert entry.is_relative


def test_double_dot_walks_up_to_the_parent_package():
    """`from ..core.config import get_settings` inside app/api/health.py."""
    entry = _resolve_one(
        "app/api/health.py",
        ImportRef(module="core.config", name="get_settings", line=2, level=2, is_from=True),
    )
    assert entry.resolution is ImportResolution.RELATIVE
    assert entry.target_path == "app/core/config.py"
    assert entry.level == 2


def test_bare_relative_import_of_a_sibling_module():
    """`from . import config` inside app/core/config.py's own package."""
    entry = _resolve_one(
        "app/core/config.py",
        ImportRef(module=None, name="config", line=2, level=1, is_from=True),
    )
    assert entry.resolution is ImportResolution.RELATIVE
    assert entry.target_path == "app/core/config.py"


def test_an_init_file_is_its_own_package():
    """One dot inside app/__init__.py means `app`, not `app`'s parent."""
    entry = _resolve_one(
        "app/__init__.py",
        ImportRef(module="core", line=2, level=1, is_from=True),
    )
    assert entry.resolution is ImportResolution.RELATIVE
    assert entry.target_path == "app/core/__init__.py"


def test_over_deep_relative_import_is_external():
    """More dots than package depth — Python would fail, so record the fact."""
    entry = _resolve_one(
        "app/api/health.py",
        ImportRef(module="core", line=2, level=5, is_from=True),
    )
    assert entry.resolution is ImportResolution.EXTERNAL
    assert entry.target_module == ".....core"


def test_relative_import_from_outside_a_package_is_external():
    entry = _resolve_one(
        "scripts/tool.py",
        ImportRef(module="helpers", line=1, level=1, is_from=True),
    )
    assert entry.resolution is ImportResolution.EXTERNAL


def test_relative_import_of_a_missing_module_is_external():
    entry = _resolve_one(
        "app/api/health.py",
        ImportRef(module="nonexistent", line=2, level=2, is_from=True),
    )
    assert entry.resolution is ImportResolution.EXTERNAL


# --- aggregation -------------------------------------------------------------


def test_one_line_importing_two_names_from_one_module_is_one_edge():
    """`from app.core.config import Settings, get_settings` is one dependency."""
    resolved = resolve_imports(
        _repo(
            {
                "app/api/health.py": (
                    ImportRef(module="app.core.config", name="Settings", line=4, is_from=True),
                    ImportRef(
                        module="app.core.config", name="get_settings", line=4, is_from=True
                    ),
                )
            }
        )
    )
    assert len(resolved) == 1


def test_two_names_resolving_to_different_files_stay_two_edges():
    resolved = resolve_imports(
        _repo(
            {
                "app/api/health.py": (
                    ImportRef(module="app.core", name="config", line=4, is_from=True),
                    ImportRef(module="app.core", name="missing", line=4, is_from=True),
                )
            }
        )
    )
    assert len(resolved) == 2


def test_the_same_target_on_two_lines_stays_two_edges():
    resolved = resolve_imports(
        _repo(
            {
                "app/api/health.py": (
                    ImportRef(module="app.core.config", line=1),
                    ImportRef(module="app.core.config", line=9),
                )
            }
        )
    )
    assert len(resolved) == 2


def test_alias_is_carried_onto_the_edge():
    entry = _resolve_one(
        "app/api/health.py",
        ImportRef(module="app.core.config", alias="cfg", line=1),
    )
    assert entry.alias == "cfg"


@pytest.mark.parametrize(
    ("dotted", "expected"),
    [("os.path", "os"), ("urllib3", "urllib3"), ("..core.config", "core")],
)
def test_top_level_names_the_installable_package(dotted: str, expected: str):
    from app.services.graph_keys import top_level

    assert top_level(dotted) == expected
