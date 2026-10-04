from __future__ import annotations

from datetime import timedelta

from conversation_agent.adapters.postgres.coordination import CoordinationTime
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.rows import OUTBOX_COLUMNS, outbox_from_row
from conversation_agent.core.models.runtime import OutboundMessage, OutboxStatus, SendResult
from conversation_agent.ports.clock import Clock
from conversation_agent.ports.coordination import CoordinationClock

_O_COLUMNS = ", ".join(f"o.{c.strip()}" for c in OUTBOX_COLUMNS.split(","))


class PostgresOutboxStore:
    def __init__(
        self,
        db: PostgresDatabase,
        clock: Clock,
        *,
        max_attempts: int = 5,
        coordination: CoordinationClock | None = None,
    ) -> None:
        self._time = coordination or CoordinationTime(db)
        self._db = db
        self._clock = clock
        self._max_attempts = max_attempts

    async def claim_ready(
        self, owner: str, limit: int, claim_ttl: timedelta
    ) -> list[OutboundMessage]:
        """PENDING-and-due or stale-SENDING rows -> SENDING. A stale SENDING row is a message
        whose sender died mid-flight: it is retried with the *same* payload and idempotency
        key (never regenerated)."""
        now = await self._time.now()
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
                   attempts = o.attempts + 1, updated_at = $1,
                   first_sent_at = COALESCE(o.first_sent_at, $1)
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
        now = await self._time.now()
        status = result.status
        available_at = now
        if (
            status is OutboxStatus.FAILED
            and result.retryable
            and message.attempts < self._max_attempts
        ):
            status, available_at = OutboxStatus.PENDING, now + retry_after
        # The channel's own acceptance time, or NULL. Never the local/db clock: comparing a
        # provider-domain reply time with a local one would fake (in)eligibility (INV-026).
        accepted_at = result.provider_accepted_at if status is OutboxStatus.ACCEPTED else None
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

    async def claim_unknown(
        self, owner: str, limit: int, claim_ttl: timedelta
    ) -> list[OutboundMessage]:
        now = await self._time.now()
        rows = await self._db.pool.fetch(
            f"""
            WITH picked AS (
                SELECT tenant_id, outbox_id FROM outbox_messages
                 WHERE (status = 'UNKNOWN' AND available_at <= $1)
                    OR (status = 'RECONCILING' AND claim_expires_at <= $1)
                 ORDER BY updated_at
                 LIMIT $2 FOR UPDATE SKIP LOCKED
            )
            UPDATE outbox_messages o
               SET status = 'RECONCILING', claim_owner = $3, claim_expires_at = $4,
                   updated_at = $1
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

    async def record_reconciliation(
        self,
        message: OutboundMessage,
        owner: str,
        *,
        found: SendResult | None,
        resend: bool,
        retry_after: timedelta,
        note: str | None = None,
    ) -> None:
        now = await self._time.now()
        if found is not None:
            status, available_at = found.status, now
            accepted_at = found.provider_accepted_at if status is OutboxStatus.ACCEPTED else None
            provider_id = found.provider_message_id
        elif resend:
            status, available_at, accepted_at, provider_id = OutboxStatus.PENDING, now, None, None
        else:
            status, available_at, accepted_at, provider_id = (
                OutboxStatus.UNKNOWN,
                now + retry_after,
                None,
                None,
            )
        await self._db.pool.execute(
            """
            UPDATE outbox_messages
               SET status = $4, provider_message_id = COALESCE($5, provider_message_id),
                   provider_accepted_at = COALESCE($6, provider_accepted_at),
                   available_at = $7, claim_owner = NULL, claim_expires_at = NULL,
                   reconcile_attempts = reconcile_attempts + 1,
                   last_error = COALESCE($8, last_error), updated_at = $9
             WHERE tenant_id = $1 AND outbox_id = $2 AND status = 'RECONCILING'
               AND claim_owner = $3
            """,
            message.tenant_id,
            message.outbox_id,
            owner,
            status.value,
            provider_id,
            accepted_at,
            available_at,
            note,
            now,
        )

    async def get(self, tenant_id: str, outbox_id: str) -> OutboundMessage | None:
        r = await self._db.pool.fetchrow(
            f"SELECT {OUTBOX_COLUMNS} FROM outbox_messages WHERE tenant_id=$1 AND outbox_id=$2",
            tenant_id,
            outbox_id,
        )
        return outbox_from_row(r) if r else None
