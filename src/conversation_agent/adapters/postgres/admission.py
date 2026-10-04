from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta

from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.ports.admission import ADMITTED, AdmissionDecision


class InboxBacklogAdmission:
    """Stop taking new inbound messages while the tenant's workers are behind.

    The edge is the right place to push back: a message that is not acknowledged is redelivered by
    the gateway, so shedding costs latency, never data. The backlog is the number (and age) of
    inbox events still READY for a turn; it is read at most once per `cache_seconds` per tenant so
    the check does not become load of its own.
    """

    def __init__(
        self,
        db: PostgresDatabase,
        *,
        max_ready: int = 5000,
        max_oldest_age: timedelta = timedelta(minutes=10),
        retry_after_seconds: int = 30,
        cache_seconds: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._max_ready = max_ready
        self._max_age = max_oldest_age
        self._retry = retry_after_seconds
        self._ttl = cache_seconds
        self._now = monotonic
        self._cache: dict[str, tuple[float, AdmissionDecision]] = {}

    async def admit(self, tenant_id: str, contact_id: str) -> AdmissionDecision:
        cached = self._cache.get(tenant_id)
        if cached is not None and self._now() - cached[0] < self._ttl:
            return cached[1]
        row = await self._db.pool.fetchrow(
            "SELECT count(*) AS ready, "
            "EXTRACT(EPOCH FROM clock_timestamp() - min(received_at))::float8 AS oldest "
            "FROM inbox_events WHERE tenant_id = $1 AND status = 'READY'",
            tenant_id,
        )
        assert row is not None
        oldest = float(row["oldest"] or 0)
        behind = row["ready"] >= self._max_ready or oldest > self._max_age.total_seconds()
        decision = (
            AdmissionDecision(False, 503, self._retry, "inbox_backlog") if behind else ADMITTED
        )
        self._cache[tenant_id] = (self._now(), decision)
        return decision
