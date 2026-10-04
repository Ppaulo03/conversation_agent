from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RateLimit:
    """A token bucket: `capacity` is the burst, `refill_per_second` the sustained rate."""

    capacity: float
    refill_per_second: float

    def __post_init__(self) -> None:
        if self.capacity <= 0 or self.refill_per_second <= 0:
            raise ValueError("a rate limit needs a positive capacity and refill rate")


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after_seconds: float = 0.0  # when denied: how long until the cost would be available


class RateLimiter(Protocol):
    """Rate limit per (scope, key), e.g. ("contact_inbound", "<tenant>/<contact>") or
    ("tool_connection", "<tenant>/<connection>"). A denial is information for the CALLER (shed,
    delay, retry later): the limiter never drops or queues work by itself."""

    async def acquire(
        self, scope: str, key: str, limit: RateLimit, *, cost: float = 1.0
    ) -> RateDecision: ...
