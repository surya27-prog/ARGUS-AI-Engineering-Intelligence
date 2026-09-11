"""Tests for the parse-scoped cache.

Entirely pure — no Postgres, no Neo4j. The cache's whole job is deciding when an
answer is still valid, and that decision is a function of its key, so it can and
should be tested without either store running.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.core.cache import ParseScopedCache, invalidate_all, risk_cache

PARSED = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _counting():
    """A compute function that records how often it actually ran."""
    calls: list[int] = []

    def compute():
        calls.append(1)
        return f"computed-{len(calls)}"

    return compute, calls


def test_a_second_read_of_the_same_parse_is_a_hit():
    cache: ParseScopedCache[str] = ParseScopedCache("t")
    repo = uuid4()
    compute, calls = _counting()

    first, hit_first = cache.get_or_compute(repo, PARSED, (), compute)
    second, hit_second = cache.get_or_compute(repo, PARSED, (), compute)

    assert len(calls) == 1
    assert (hit_first, hit_second) == (False, True)
    assert first == second


def test_a_re_parse_retires_the_entry():
    """The point of keying on `parsed_at`: nothing has to remember to clear it."""
    cache: ParseScopedCache[str] = ParseScopedCache("t")
    repo = uuid4()
    compute, calls = _counting()

    cache.get_or_compute(repo, PARSED, (), compute)
    _, hit = cache.get_or_compute(repo, PARSED + timedelta(seconds=1), (), compute)

    assert len(calls) == 2
    assert hit is False


def test_variants_are_cached_separately():
    """`/graph?view=files` and `?view=calls` are different answers."""
    cache: ParseScopedCache[str] = ParseScopedCache("t")
    repo = uuid4()
    compute, calls = _counting()

    cache.get_or_compute(repo, PARSED, ("files", 400), compute)
    cache.get_or_compute(repo, PARSED, ("calls", 400), compute)
    _, hit = cache.get_or_compute(repo, PARSED, ("files", 400), compute)

    assert len(calls) == 2
    assert hit is True


def test_repositories_do_not_share_entries():
    cache: ParseScopedCache[str] = ParseScopedCache("t")
    compute, calls = _counting()

    cache.get_or_compute(uuid4(), PARSED, (), compute)
    cache.get_or_compute(uuid4(), PARSED, (), compute)

    assert len(calls) == 2


def test_an_unparsed_repository_is_never_cached():
    """`parsed_at` of None means no parse has completed, so there is nothing
    stable to cache against — storing it would pin a result to a moving target."""
    cache: ParseScopedCache[str] = ParseScopedCache("t")
    repo = uuid4()
    compute, calls = _counting()

    _, first = cache.get_or_compute(repo, None, (), compute)
    _, second = cache.get_or_compute(repo, None, (), compute)

    assert len(calls) == 2
    assert (first, second) == (False, False)
    assert cache.stats().entries == 0


def test_the_oldest_entry_is_evicted_first():
    cache: ParseScopedCache[str] = ParseScopedCache("t", max_entries=2)
    compute, calls = _counting()
    a, b, c = uuid4(), uuid4(), uuid4()

    cache.get_or_compute(a, PARSED, (), compute)
    cache.get_or_compute(b, PARSED, (), compute)
    cache.get_or_compute(c, PARSED, (), compute)  # evicts a

    assert cache.stats().entries == 2
    _, hit_a = cache.get_or_compute(a, PARSED, (), compute)
    _, hit_c = cache.get_or_compute(c, PARSED, (), compute)
    assert hit_a is False
    assert hit_c is True


def test_a_read_makes_an_entry_recent():
    """Otherwise the entry being used most would be the one evicted."""
    cache: ParseScopedCache[str] = ParseScopedCache("t", max_entries=2)
    compute, _ = _counting()
    a, b, c = uuid4(), uuid4(), uuid4()

    cache.get_or_compute(a, PARSED, (), compute)
    cache.get_or_compute(b, PARSED, (), compute)
    cache.get_or_compute(a, PARSED, (), compute)  # a is now the recent one
    cache.get_or_compute(c, PARSED, (), compute)  # so b goes

    _, hit_a = cache.get_or_compute(a, PARSED, (), compute)
    _, hit_b = cache.get_or_compute(b, PARSED, (), compute)
    assert hit_a is True
    assert hit_b is False


def test_invalidate_targets_one_repository():
    cache: ParseScopedCache[str] = ParseScopedCache("t")
    compute, _ = _counting()
    keep, drop = uuid4(), uuid4()

    cache.get_or_compute(keep, PARSED, (), compute)
    cache.get_or_compute(drop, PARSED, (), compute)
    removed = cache.invalidate(drop)

    assert removed == 1
    assert cache.get_or_compute(keep, PARSED, (), compute)[1] is True
    assert cache.get_or_compute(drop, PARSED, (), compute)[1] is False


def test_invalidate_all_clears_every_cache():
    """What `DELETE /repos/{id}` calls: a deleted repository has no next parse
    to retire its entries, so they have to go explicitly."""
    repo = uuid4()
    compute, _ = _counting()
    risk_cache.get_or_compute(repo, PARSED, ("File", 2, 120), compute)

    assert invalidate_all(repo) >= 1
    assert risk_cache.get_or_compute(repo, PARSED, ("File", 2, 120), compute)[1] is False


def test_stats_count_hits_and_misses():
    cache: ParseScopedCache[str] = ParseScopedCache("t")
    repo = uuid4()
    compute, _ = _counting()

    cache.get_or_compute(repo, PARSED, (), compute)
    cache.get_or_compute(repo, PARSED, (), compute)
    cache.get_or_compute(repo, PARSED, (), compute)

    stats = cache.stats()
    assert (stats.hits, stats.misses, stats.entries) == (2, 1, 1)
    assert stats.hit_rate == 2 / 3


def test_hit_rate_of_an_untouched_cache_is_zero_not_an_error():
    assert ParseScopedCache("t").stats().hit_rate == 0.0


def test_concurrent_readers_all_get_a_value():
    """Compute runs outside the lock, so two cold callers may both compute — but
    neither may block indefinitely or come away empty."""
    from concurrent.futures import ThreadPoolExecutor

    cache: ParseScopedCache[str] = ParseScopedCache("t")
    repo = uuid4()

    def compute():
        return "value"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: cache.get_or_compute(repo, PARSED, (), compute), range(16))
        )

    assert all(value == "value" for value, _ in results)
    assert cache.stats().entries == 1
