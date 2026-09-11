"""A small per-process cache for results that are a function of one parse.

`/risk` and `/debt` both read the whole repository — every symbol, every edge —
to answer one request, and the dashboard asks for three of them on load. Neither
result changes between parses, so recomputing them per request is pure waste.

**The key is the parse, not the clock.** A TTL would be a guess about how stale
an answer may be, and it would be wrong in both directions: too long and a
re-parsed repository serves the old graph, too short and the cache never helps.
Every completed parse stamps `Repository.parsed_at`, so keying on it means an
entry is valid exactly as long as the parse behind it is current, and a re-parse
invalidates without anyone having to remember to call `invalidate`. It costs no
extra query either — `parsed_at` is on the repository row the endpoint already
loaded to check for a 404.

Deliberately *not* a shared cache. This is per-process, so it is lost on restart
and not shared between workers. That is the right trade for now: the alternative
is Redis in the stack for something that only saves recomputation, and a cold
cache is merely slow rather than wrong. Week 6 can revisit it if deployment shows
it matters.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any, Generic, TypeVar
from uuid import UUID

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Entries per cache. Each holds one repository's answer for one parse, so this is
# a ceiling on repositories-times-variants held at once, not on data size.
DEFAULT_MAX_ENTRIES = 32


@dataclass(frozen=True, slots=True)
class CacheStats:
    hits: int = 0
    misses: int = 0
    entries: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class ParseScopedCache(Generic[T]):
    """LRU keyed on a repository, its parse timestamp, and a variant tuple.

    Thread-safe because FastAPI serves requests on a thread pool and two
    dashboard panels land concurrently — an unguarded `OrderedDict` would be
    mutated from both.
    """

    def __init__(self, name: str, *, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self.name = name
        self.max_entries = max_entries
        self._entries: OrderedDict[tuple, T] = OrderedDict()
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    def _key(
        self, repository_id: UUID | str, parsed_at: datetime | None, variant: tuple
    ) -> tuple:
        # `parsed_at` of None means the repository has never completed a parse.
        # Such a result is not cacheable — there is nothing stable behind it —
        # and `get_or_compute` refuses to store it.
        return (str(repository_id), parsed_at.isoformat() if parsed_at else None, *variant)

    def get_or_compute(
        self,
        repository_id: UUID | str,
        parsed_at: datetime | None,
        variant: tuple,
        compute: Callable[[], T],
    ) -> tuple[T, bool]:
        """Return `(value, was_a_hit)`.

        The flag is returned rather than hidden so an endpoint can report it and
        a test can prove a hit happened instead of inferring it from a timing.
        """
        if parsed_at is None:
            return compute(), False

        key = self._key(repository_id, parsed_at, variant)
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                self._hits += 1
                return self._entries[key], True
            self._misses += 1

        # Computed outside the lock: these take seconds, and holding the lock
        # would serialise every concurrent request behind the first one. Two
        # callers may duplicate the work on a cold cache, which wastes a little
        # and is far better than blocking the second one for the full duration.
        value = compute()

        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                evicted, _ = self._entries.popitem(last=False)
                logger.debug("%s cache evicted %s", self.name, evicted[:2])
        return value, False

    def invalidate(self, repository_id: UUID | str | None = None) -> int:
        """Drop entries for one repository, or all of them. Returns the count.

        Rarely needed — `parsed_at` already retires stale entries — but a deleted
        repository should not sit in memory until it is evicted, and tests need a
        clean slate.
        """
        target = str(repository_id) if repository_id is not None else None
        with self._lock:
            doomed = [k for k in self._entries if target is None or k[0] == target]
            for key in doomed:
                del self._entries[key]
        return len(doomed)

    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(hits=self._hits, misses=self._misses, entries=len(self._entries))


# One instance per expensive read. Separate rather than shared so an eviction in
# one cannot push out the other's entry for the same repository.
risk_cache: ParseScopedCache[Any] = ParseScopedCache("risk")
debt_cache: ParseScopedCache[Any] = ParseScopedCache("debt")
graph_cache: ParseScopedCache[Any] = ParseScopedCache("graph")

ALL_CACHES = (risk_cache, debt_cache, graph_cache)


def invalidate_all(repository_id: UUID | str | None = None) -> int:
    """Clear every cache for one repository, or entirely."""
    return sum(cache.invalidate(repository_id) for cache in ALL_CACHES)


__all__ = [
    "ALL_CACHES",
    "DEFAULT_MAX_ENTRIES",
    "CacheStats",
    "ParseScopedCache",
    "debt_cache",
    "graph_cache",
    "invalidate_all",
    "risk_cache",
]
