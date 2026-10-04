"""Authority for time used in *distributed coordination* (leases, claims, backoff, expiry).

Workers' application clocks can disagree: a worker 40 s behind would create leases that look
expired to everyone else and trigger premature takeovers. Coordination therefore reads ONE clock,
the database's (`clock_timestamp()`), and that is the default of every PostgreSQL adapter. Tests
inject `FixedCoordinationTime` explicitly; nothing falls back to a worker's own clock.

The application `Clock` still governs conversational time ("tomorrow", prompts, the TTL of the
conversation itself); only coordination comparisons go through here (INV-032).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.ports.clock import Clock


class CoordinationTime:
    """PostgreSQL `clock_timestamp()`: the production authority."""

    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    async def now(self, conn: Any | None = None) -> datetime:
        value = await (conn or self._db.pool).fetchval("SELECT clock_timestamp()")
        assert isinstance(value, datetime)
        return value

    def estimate(self, observed_at: datetime, observed_monotonic: float) -> datetime:
        return observed_at + timedelta(seconds=time.monotonic() - observed_monotonic)


class FixedCoordinationTime:
    """TEST authority: a deterministic Clock standing in for the database clock."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    async def now(self, conn: Any | None = None) -> datetime:
        return self._clock.now()

    def estimate(self, observed_at: datetime, observed_monotonic: float) -> datetime:
        return self._clock.now()
