from __future__ import annotations

from datetime import timedelta

from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.rows import OUTBOX_COLUMNS, outbox_from_row
from conversation_agent.core.models.runtime import OutboundMessage, OutboxStatus, SendResult
from conversation_agent.ports.clock import Clock

_O_COLUMNS = ", ".join(f"o.{c.strip()}" for c in OUTBOX_COLUMNS.split(","))


class PostgresOutboxStore:
    def __init__(self, db: PostgresDatabase, clock: Clock, *, max_attempts: int = 5) -> None:
        self._db = db
        self._clock = clock
        self._max_attempts = max_attempts

    async def claim_ready(
        self, owner: str, limit: int, claim_ttl: timedelta
    ) -> list[OutboundMessage]:
        """PENDING-and-due or stale-SENDING rows -> SENDING. A stale SENDING row is a message
        whose sender died mid-flight: it is retried with the *same* payload and idempotency
        key (never regenerated)."""
        now = self._clock.now()
        rows = await self._db.pool.fetch(
            f"""
            WITH picked AS (
                SELECT tenant_id, outbox_id FROM outbox_messages
                 WHERE (status = 'PENDING' AND available_at <= $1)
                    OR (status = 'SENDING' AND claim_expires_at <= $1)
                 ORDER BY created_at, message_index
                 LIMIT $2 FOR UPDATE SKIP LOCKED
            )
            UPDATE outbox_messages o
               SET status = 'SENDING', claim_owner = $3, claim_expires_at = $4,
                   attempts = o.attempts + 1, updated_at = $1
              FROM picked
             WHERE o.tenant_id = picked.tenant_id AND o.outbox_id = picked.outbox_id
            RETURNING {_O_COLUMNS}
            """,
            now,
            limit,
            owner,
            now + claim_ttl,
        )
        return [outbox_from_row(r) for r in rows]

    async def record_result(
        self, message: OutboundMessage, owner: str, result: SendResult, retry_after: timedelta
    ) -> None:
        """Compare-and-set on (status=SENDING, claim_owner): a sender that lost its claim to
        another worker cannot overwrite the newer outcome."""
        now = self._clock.now()
        status = result.status
        available_at = now
        if (
            status is OutboxStatus.FAILED
            and result.retryable
            and message.attempts < self._max_attempts
        ):
            status, available_at = OutboxStatus.PENDING, now + retry_after
        accepted_at = now if status is OutboxStatus.ACCEPTED else None
        await self._db.pool.execute(
            """
            UPDATE outbox_messages
               SET status = $4, provider_message_id = COALESCE($5, provider_message_id),
                   provider_accepted_at = COALESCE($6, provider_accepted_at),
                   available_at = $7, claim_owner = NULL, claim_expires_at = NULL, updated_at = $8
             WHERE tenant_id = $1 AND outbox_id = $2 AND status = 'SENDING' AND claim_owner = $3
            """,
            message.tenant_id,
            message.outbox_id,
            owner,
            status.value,
            result.provider_message_id,
            accepted_at,
            available_at,
            now,
        )

    async def get(self, tenant_id: str, outbox_id: str) -> OutboundMessage | None:
        r = await self._db.pool.fetchrow(
            f"SELECT {OUTBOX_COLUMNS} FROM outbox_messages WHERE tenant_id=$1 AND outbox_id=$2",
            tenant_id,
            outbox_id,
        )
        return outbox_from_row(r) if r else None
