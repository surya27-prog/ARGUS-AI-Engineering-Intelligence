"""Relative imports, exotic signatures, and calls in awkward positions."""

from __future__ import annotations

import functools
from . import sibling
from .. import shared
from ..core.config import get_settings as settings_factory
from .helpers import first, second


@functools.lru_cache(maxsize=128)
def cached(a, /, b, c=3, *args, d, e=5, **kwargs) -> dict:
    """Every parameter kind in one signature."""
    return {"a": a, "b": b, "c": c, "d": d, "e": e}


def uses_relatives():
    sibling.run()
    shared.helper.load()
    return settings_factory()


def comprehensions(items):
    doubled = [first(x) for x in items]
    mapped = {k: second(v) for k, v in items}
    return doubled, mapped


def unnameable(handlers):
    # Calls with no dotted name to record — these are deliberately dropped.
    handlers[0]()
    (lambda: 1)()
    return len(handlers)


class Mixin:
    def method(self):
        return self.helper()

    def helper(self):
        return functools.reduce(lambda a, b: a + b, [1, 2, 3])
