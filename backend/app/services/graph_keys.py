"""The node key formats fixed by docs/architecture/graph-schema.md.

Every write and every lookup goes through these functions, so a key can only be
built one way. Day 3's import resolution and Day 4's call graph reuse them to
point at nodes this day's writer created — a second spelling of a key would
silently create a second node instead of matching the first.
"""

from __future__ import annotations

from uuid import UUID


def repo_key(repo_id: UUID | str) -> str:
    return f"repo:{repo_id}"


def file_key(repo_id: UUID | str, path: str) -> str:
    return f"file:{repo_id}:{path}"


def symbol_key(repo_id: UUID | str, module: str | None, qualname: str, path: str) -> str:
    """`sym:{repo_id}:{scope}:{qualname}`.

    The schema doc allows an empty module for a file outside any package, but
    that makes `helper()` in two unpackaged files share one key and collapse
    into a single node. So the scope falls back to the file path, which is
    unique by construction: `sym:{id}:scripts/tool.py:main`.
    """
    return f"sym:{repo_id}:{module or path}:{qualname}"


def module_key(repo_id: UUID | str, dotted_name: str) -> str:
    return f"mod:{repo_id}:{dotted_name}"


def external_class_key(repo_id: UUID | str, name: str) -> str:
    """A base class ARGUS could not resolve inside the repo.

    `external` is a reserved scope segment rather than an empty one: it can
    never collide with a real module or path, so `BaseSettings` imported into
    ten files is one node, which is what makes "everything inheriting from
    BaseSettings" answerable.
    """
    return f"sym:{repo_id}:external:{name}"


def top_level(dotted_name: str) -> str:
    """`os.path` -> `os` — what you would pip install.

    An over-deep relative import is recorded as external with its literal text,
    so the leading dots have to come off first or `..core.config` would report
    an empty package name.
    """
    return dotted_name.lstrip(".").split(".", 1)[0] or dotted_name
