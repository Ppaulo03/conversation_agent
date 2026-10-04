from __future__ import annotations

from datetime import timedelta

from conversation_agent.adapters.postgres.coordination import CoordinationTime
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.models.runtime import ScheduledEvent
from conversation_agent.ports.clock import Clock
from conversation_agent.ports.coordination import CoordinationClock


class PostgresScheduler:
    """Durable timers: due events live in PostgreSQL, so they survive any restart."""

    def __init__(
        self, db: PostgresDatabase, clock: Clock, coordination: CoordinationClock | None = None
    ) -> None:
        self._time = coordination or CoordinationTime(db)
        self._db = db
        self._clock = clock

    async def schedule(self, event: ScheduledEvent) -> None:
        await self._db.pool.execute(
            """
            INSERT INTO scheduled_events (tenant_id, scheduler_key, event_type, payload, due_at,
                                          status, created_at)
            VALUES ($1, $2, $3, $4, $5, 'PENDING', $6)
            ON CONFLICT (tenant_id, scheduler_key) DO UPDATE
               SET event_type = EXCLUDED.event_type, payload = EXCLUDED.payload,
                   due_at = EXCLUDED.due_at, status = 'PENDING',
                   claimed_by = NULL, claim_expires_at = NULL
            """,
            event.tenant_id,
            event.scheduler_key,
            event.event_type,
            event.payload,
            event.due_at,
            self._clock.now(),
        )

    async def cancel(self, tenant_id: str, scheduler_key: str) -> None:
        await self._db.pool.execute(
            "UPDATE scheduled_events SET status = 'CANCELLED' "
            "WHERE tenant_id = $1 AND scheduler_key = $2 AND status IN ('PENDING', 'CLAIMED')",
            tenant_id,
            scheduler_key,
        )

    async def claim_due(self, owner: str, limit: int, claim_ttl: timedelta) -> list[ScheduledEvent]:
        now = await self._time.now()
        rows = await self._db.pool.fetch(
            """
            WITH picked AS (
                SELECT tenant_id, scheduler_key FROM scheduled_events
                 WHERE (status = 'PENDING' AND due_at <= $1)
                    OR (status = 'CLAIMED' AND claim_expires_at <= $1)
                 ORDER BY due_at LIMIT $2 FOR UPDATE SKIP LOCKED
            )
            UPDATE scheduled_events s
               SET status = 'CLAIMED', claimed_by = $3, claim_expires_at = $4
              FROM picked
             WHERE s.tenant_id = picked.tenant_id AND s.scheduler_key = picked.scheduler_key
            RETURNING s.tenant_id, s.scheduler_key, s.event_type, s.payload, s.due_at
            """,
            now,
            limit,
            owner,
            now + claim_ttl,
        )
        return [
            ScheduledEvent(
                tenant_id=r["tenant_id"],
                scheduler_key=r["scheduler_key"],
                event_type=r["event_type"],
                due_at=r["due_at"],
                payload=r["payload"],
            )
            for r in rows
        ]

    async def complete(self, tenant_id: str, scheduler_key: str, owner: str) -> None:
        await self._db.pool.execute(
            "UPDATE scheduled_events SET status = 'DONE' "
            "WHERE tenant_id = $1 AND scheduler_key = $2 AND claimed_by = $3 "
            "AND status = 'CLAIMED'",
            tenant_id,
            scheduler_key,
            owner,
        )
