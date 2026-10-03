"""Authority for time used in *distributed coordination* (leases, claims, backoff, expiry).

Workers' application clocks can disagree: a worker 40 s behind would create leases that look
expired to everyone else and trigger premature takeovers (conversation, execution or
reconciliation). Production therefore uses the database clock (`clock_timestamp()`), one
source for every worker. Tests inject a deterministic `Clock` instead.

The application `Clock` still governs conversational time ("tomorrow", prompts, TTLs of the
conversation itself); only coordination comparisons go through here.
"""

from __future__ import annotations

from datetime import datetime

import asyncpg

from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.ports.clock import Clock


class CoordinationTime:
    def __init__(self, db: PostgresDatabase, clock: Clock | None = None) -> None:
        """`clock=None` -> PostgreSQL `clock_timestamp()` (production default)."""
        self._db = db
        self._clock = clock

    async def now(self, conn: asyncpg.Connection | None = None) -> datetime:
        if self._clock is not None:
            return self._clock.now()
        value = await (conn or self._db.pool).fetchval("SELECT clock_timestamp()")
        assert isinstance(value, datetime)
        return value
