"""Rate limiting for the endpoints that cost money to serve.

Most of ARGUS reads its own database, so a caller hammering `/files` wastes only
CPU. Three endpoints are different: `/chat`, `/impact/explain` and `/search` each
send a request to a paid provider, so an accidental loop in a client — or a
refresh held down — spends real money and hits a provider rate limit that then
affects everyone else using the key.

**A token bucket, not a fixed window.** A fixed window lets a caller spend the
whole allowance in the last second of one window and again in the first second of
the next, which is exactly double the intended burst. A bucket refilling
continuously has no edge to exploit, and it allows a genuine short burst — a user
asking three questions quickly is normal, and a limiter that refuses the third is
worse than no limiter.

**In-process, keyed on the client address.** Per-worker rather than global, so
the effective limit scales with worker count; that is honest but imprecise, and
the alternative is Redis for something whose job is stopping accidents. There is
no auth yet, so there is no user to key on — when auth arrives the key should
become the user id, since an IP is shared by everyone behind one office NAT.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

# Distinct client keys tracked. Beyond this the oldest is dropped, which briefly
# forgives whoever fell off the end — acceptable, since the alternative is
# unbounded memory driven by request volume.
MAX_TRACKED_CLIENTS = 4096


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


@dataclass
class RateLimiter:
    """A per-client token bucket.

    `capacity` is the burst a caller may spend at once; `per_minute` is the rate
    it refills at. Capacity above the rate is deliberate — it is what lets three
    quick questions through while still holding the sustained rate down.
    """

    name: str
    per_minute: float
    capacity: float
    _buckets: OrderedDict[str, _Bucket] = field(default_factory=OrderedDict, repr=False)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def check(self, key: str, *, now: float | None = None) -> float | None:
        """Spend a token. Returns None if allowed, or seconds to wait if not."""
        now = time.monotonic() if now is None else now
        refill_per_second = self.per_minute / 60.0

        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self.capacity, updated=now)
                self._buckets[key] = bucket
            else:
                elapsed = max(0.0, now - bucket.updated)
                bucket.tokens = min(self.capacity, bucket.tokens + elapsed * refill_per_second)
                bucket.updated = now

            self._buckets.move_to_end(key)
            while len(self._buckets) > MAX_TRACKED_CLIENTS:
                self._buckets.popitem(last=False)

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return None

            # Ceil to a whole second: Retry-After is an integer header, and
            # rounding down would invite a retry that is refused again.
            missing = 1.0 - bucket.tokens
            return max(1.0, missing / refill_per_second) if refill_per_second else 60.0

    def reset(self, key: str | None = None) -> None:
        """Forget one client, or all of them. For tests."""
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)


def client_key(request: Request) -> str:
    """Who to charge for a request.

    Honours `X-Forwarded-For` because in deployment this sits behind a proxy and
    `request.client.host` would be the proxy for every caller — one shared bucket
    for the world. The left-most entry is the original client. It is
    caller-controlled and therefore spoofable, which is acceptable for a limiter
    whose purpose is stopping accidents rather than resisting an attacker.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def limiter_dependency(limiter: RateLimiter):
    """Turn a limiter into a FastAPI dependency."""

    def dependency(request: Request) -> None:
        retry_after = limiter.check(client_key(request))
        if retry_after is None:
            return
        logger.info("Rate limited %s on %s", limiter.name, request.url.path)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Too many {limiter.name} requests. This endpoint calls a paid "
                f"model, so it is capped at {limiter.per_minute:.0f}/minute. "
                f"Retry in {retry_after:.0f}s."
            ),
            # Without this a client has to guess, and guessing means retrying
            # immediately and being refused again.
            headers={"Retry-After": str(int(retry_after))},
        )

    return dependency


# One limiter per endpoint group rather than one shared: asking a question and
# rendering a graph panel are different activities, and a burst of explanations
# should not lock someone out of chat.
#
# Chat is the most expensive per call and the most naturally paced by a human
# typing, so it is the tightest. Search embeds a short query — cheap, and issued
# as-you-type, so it gets the most headroom.
chat_limiter = RateLimiter(name="chat", per_minute=10, capacity=4)
explain_limiter = RateLimiter(name="impact explanation", per_minute=20, capacity=6)
search_limiter = RateLimiter(name="search", per_minute=60, capacity=15)

ALL_LIMITERS = (chat_limiter, explain_limiter, search_limiter)

require_chat_quota = limiter_dependency(chat_limiter)
require_explain_quota = limiter_dependency(explain_limiter)
require_search_quota = limiter_dependency(search_limiter)


def reset_all() -> None:
    """Clear every bucket. For tests, and for a fresh process."""
    for limiter in ALL_LIMITERS:
        limiter.reset()


__all__ = [
    "ALL_LIMITERS",
    "MAX_TRACKED_CLIENTS",
    "RateLimiter",
    "client_key",
    "limiter_dependency",
    "require_chat_quota",
    "require_explain_quota",
    "require_search_quota",
    "reset_all",
]
