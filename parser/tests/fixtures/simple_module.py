"""Module-level functions, plain imports, straightforward call sites."""

import os
import os.path as ospath
from pathlib import Path


def read_config(path: str, *, encoding: str = "utf-8") -> str:
    """Read a config file and return its contents."""
    resolved = Path(path).expanduser()
    return resolved.read_text(encoding=encoding)


def config_dir() -> str:
    return ospath.dirname(os.environ.get("ARGUS_CONFIG", ""))


async def load_async(path: str) -> str:
    return read_config(path)


def outer(value: int) -> int:
    def inner(n: int) -> int:
        return n * 2

    return inner(value)
