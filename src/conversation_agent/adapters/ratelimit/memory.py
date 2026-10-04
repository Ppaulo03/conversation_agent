from __future__ import annotations

import time
from collections.abc import Callable

from conversation_agent.ports.ratelimit import RateDecision, RateLimit


class InMemoryRateLimiter:
    """Token buckets in process memory: exact for ONE process. Several workers each get their own
    buckets (the effective limit is then per process): use the PostgreSQL limiter when the limit
    must hold across them."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._now = monotonic
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}  # tokens, stamp

    async def acquire(
        self, scope: str, key: str, limit: RateLimit, *, cost: float = 1.0
    ) -> RateDecision:
        now = self._now()
        tokens, stamp = self._buckets.get((scope, key), (limit.capacity, now))
        tokens = min(limit.capacity, tokens + (now - stamp) * limit.refill_per_second)
        if cost > limit.capacity:
            return RateDecision(False, float("inf"))  # it can never fit: a configuration problem
        if tokens >= cost:
            self._buckets[(scope, key)] = (tokens - cost, now)
            return RateDecision(True)
        self._buckets[(scope, key)] = (tokens, now)
        return RateDecision(False, (cost - tokens) / limit.refill_per_second)
