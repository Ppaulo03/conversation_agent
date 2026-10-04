"""Time authority for distributed coordination (INV-032).

Leases, claims, backoff and expiry are compared across WORKERS, so they must all be read from one
clock. The application `Clock` stays what it was (conversational time: "tomorrow", turn
reference time, confirmation TTL); it never takes part in a coordination comparison.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol


class CoordinationClock(Protocol):
    async def now(self, conn: Any | None = None) -> datetime:
        """The authority's current time (the database clock in production)."""
        ...

    def estimate(self, observed_at: datetime, observed_monotonic: float) -> datetime:
        """Authority time NOW, given an authority reading taken at a known monotonic instant.
        Lets a worker check its lease synchronously without comparing its own wall clock with
        the authority's."""
        ...
